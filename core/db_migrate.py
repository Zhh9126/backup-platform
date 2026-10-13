# -*- coding: utf-8 -*-
"""
一站式数据迁移计划引擎（对标阿里云 DTS / AWS DMS 迁移链路）。

阶段编排（对齐业界一站式迁移语义）：
1. precheck  预检查：源/目标连通性、目标库可写（MySQL 自动建库）、源对象统计
2. migrate   结构迁移 + 全量迁移：目标端按源端 schema 重建表结构并迁移存量数据
             （复用 core.sync.engine：save_mode=create_if_not_exists + full_db_migrate）
3. verify    数据校验：逐表行数比对（源 vs 目标），给出一致性结论
4. report    迁移报告：各阶段结果、行数、耗时汇总

设计原则：
- 全部阶段真实执行，不做任何模拟；
- 增量/不停机场景由「数据同步」模块的 realtime 模式承接（迁移计划完成后可接续）；
- 密码加密存储；执行在后台线程，状态/阶段实时落库供前端轮询。
"""
import json
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

import config
import core.db as db
import core.models as models

_logger = db.get_logger("db_migrate")

# 计划状态
ST_CREATED = "created"
ST_CHECKING = "checking"
ST_MIGRATING = "migrating"
ST_VERIFYING = "verifying"
ST_COMPLETED = "completed"
ST_FAILED = "failed"

_VALID_TYPES = ("structure", "full", "verify")


def _enc_secret(v: str) -> str:
    return db.encrypt_secret(v) if v else ""


def _migratable_types() -> list:
    """可迁移的库型清单 = 同步引擎已注册的插件类型。

    迁移阶段复用 core.sync.engine（结构+全量），因此"能迁移的类型"就是
    "同步插件注册表里的类型"，不在别处再维护一份硬编码列表。
    """
    try:
        from core.sync.plugins import registry
        return registry.available()
    except Exception:  # noqa: BLE001 - 注册表异常时退化为空（调用方给明确报错）
        return []


def _scalar(conn, sql: str):
    """执行单值查询（统计用，连接由调用方管理）。"""
    cur = conn.cursor()
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else 0
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass


def _stats_connect(db_type: str, host: str, port, db: str, user: str, pwd: str):
    """建立「源对象统计」用的直连：原生驱动优先，SQL Server 走 JDBC 兜底。

    SQL Server 没有原生 Python 驱动（pymssql 不可用），只能走平台自带的
    JDBC 通道；其余类型统一走 core.native_conn，避免每个库型各写一段
    import pymysql / psycopg2（旧实现只覆盖 MySQL 与 PG，Oracle/达梦/
    SQL Server 源在预检查里被统计成 0 张表，直接被判「没有可迁移的业务表」，
    迁移根本进不去）。
    """
    from core import native_conn
    if db_type in native_conn.NATIVE_DB_TYPES:
        return native_conn.connect(db_type, host, port, db, user, pwd, timeout=8)
    from core import jdbc
    return jdbc.connect(db_type, host, port, db, user, pwd, timeout=8)


def _dec_secret(v: str) -> str:
    try:
        return db.decrypt_secret(v or "")
    except Exception:
        return ""


def _row_to_dict(row, hide_secret: bool = False) -> dict:
    d = dict(row)
    if hide_secret:
        d.pop("src_password", None)
        d.pop("tgt_password", None)
    else:
        d["src_password"] = _dec_secret(d.get("src_password"))
        d["tgt_password"] = _dec_secret(d.get("tgt_password"))
    for key in ("migrate_types", "phases_json"):
        if key in d and isinstance(d.get(key), str):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                d[key] = [] if key == "migrate_types" else {}
    return d


class DbMigrationEngine:
    """一站式数据迁移计划引擎。"""

    def __init__(self, logger: logging.Logger = None):
        self.logger = logger or _logger

    # ------------------------- 计划 CRUD -------------------------
    def create_plan(self, data: dict) -> int:
        """创建迁移计划（创建后不自动执行，调用 run_plan 启动）。"""
        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("迁移计划名称必填")
        for side in ("src", "tgt"):
            for f in ("db_type", "host", "db_name"):
                if not (data.get(f"{side}_{f}") or "").strip():
                    raise ValueError(f"{side} 端 {f} 缺失（src_db_type/src_host/src_db_name"
                                     "/tgt_db_type/tgt_host/tgt_db_name 必填）")
        # 类型必须在同步引擎已注册的插件里（迁移阶段复用同步引擎执行），
        # 否则会在跑到一半时才失败——这里提前挡掉并列出支持的类型。
        supported = _migratable_types()
        for side in ("src", "tgt"):
            t = (data.get(f"{side}_db_type") or "").strip().lower()
            if supported and t not in supported:
                raise ValueError(
                    f"{side} 端数据库类型 {t} 不支持迁移"
                    f"（已支持: {', '.join(supported)}）")
        types = data.get("migrate_types") or ["structure", "full", "verify"]
        if isinstance(types, str):
            types = [t.strip() for t in types.split(",") if t.strip()]
        bad = [t for t in types if t not in _VALID_TYPES]
        if bad:
            raise ValueError(f"迁移内容非法: {bad}（允许 {list(_VALID_TYPES)}）")
        if not types:
            raise ValueError("至少选择一项迁移内容")
        now = db.now_iso()
        pid = db.execute(
            "INSERT INTO db_migration_plans (name, src_db_type, src_host, src_port,"
            " src_username, src_password, src_db_name, tgt_db_type, tgt_host,"
            " tgt_port, tgt_username, tgt_password, tgt_db_name, migrate_types,"
            " status, current_phase, note, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, data["src_db_type"].strip(), data["src_host"].strip(),
             int(data.get("src_port") or 0) or None,
             (data.get("src_username") or "").strip(),
             _enc_secret(data.get("src_password") or ""),
             data["src_db_name"].strip(),
             data["tgt_db_type"].strip(), data["tgt_host"].strip(),
             int(data.get("tgt_port") or 0) or None,
             (data.get("tgt_username") or "").strip(),
             _enc_secret(data.get("tgt_password") or ""),
             data["tgt_db_name"].strip(),
             json.dumps(types), ST_CREATED, None,
             (data.get("note") or "").strip(), now, now))
        return pid

    def list_plans(self, limit: int = 100) -> list:
        rows = db.query("SELECT * FROM db_migration_plans ORDER BY id DESC LIMIT ?",
                        (int(limit),))
        return [_row_to_dict(r, hide_secret=True) for r in rows]

    def get_plan(self, plan_id: int, hide_secret: bool = True) -> Optional[dict]:
        row = db.query_one("SELECT * FROM db_migration_plans WHERE id=?", (int(plan_id),))
        return _row_to_dict(row, hide_secret=hide_secret) if row else None

    def delete_plan(self, plan_id: int) -> bool:
        cur = db.query_one("SELECT status FROM db_migration_plans WHERE id=?",
                           (int(plan_id),))
        if not cur:
            return False
        if cur["status"] in (ST_CHECKING, ST_MIGRATING, ST_VERIFYING):
            raise ValueError("计划正在执行中，无法删除")
        db.execute("DELETE FROM db_migration_plans WHERE id=?", (int(plan_id),))
        return True

    # ------------------------- 执行 -------------------------
    def run_plan(self, plan_id: int) -> dict:
        """异步执行迁移计划（后台线程），立即返回当前状态。"""
        plan = self.get_plan(plan_id, hide_secret=False)
        if not plan:
            raise ValueError(f"迁移计划不存在: {plan_id}")
        if plan["status"] in (ST_CHECKING, ST_MIGRATING, ST_VERIFYING):
            return {"ok": False, "message": "计划正在执行中，请勿重复触发"}
        threading.Thread(target=self._run_all, args=(plan_id,), daemon=True,
                         name=f"db-migrate-{plan_id}").start()
        return {"ok": True, "plan_id": plan_id, "status": ST_CHECKING}

    # ------------------------- 内部：阶段编排 -------------------------
    def _set(self, plan_id: int, **fields) -> None:
        fields["updated_at"] = db.now_iso()
        sets, params = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            params.append(v)
        params.append(plan_id)
        db.execute(f"UPDATE db_migration_plans SET {', '.join(sets)} WHERE id=?",
                   tuple(params))

    def _run_all(self, plan_id: int) -> None:
        plan = self.get_plan(plan_id, hide_secret=False)
        if not plan:
            return
        types = plan.get("migrate_types") or []
        phases: Dict[str, Any] = plan.get("phases_json") or {}
        started = time.monotonic()
        try:
            # ---------- 阶段 1：预检查 ----------
            self._set(plan_id, status=ST_CHECKING, current_phase="precheck",
                      error_msg=None)
            pre = self._phase_precheck(plan)
            phases["precheck"] = pre
            self._set(plan_id, phases_json=json.dumps(phases, ensure_ascii=False))
            if not pre["ok"]:
                self._fail(plan_id, phases, "precheck", pre.get("message", "预检查失败"))
                return

            # ---------- 阶段 2：结构迁移 + 全量迁移 ----------
            if "structure" in types or "full" in types:
                self._set(plan_id, status=ST_MIGRATING, current_phase="migrate")
                # 断点续跑：上次执行失败且留有 manifest → 只补失败表
                prev_manifest = (phases.get("migrate") or {}).get("manifest") or []
                resume = bool(prev_manifest) and plan.get("status") == ST_FAILED
                if resume:
                    self.logger.info("[db_migrate] 计划 #%s 断点续跑：上次已成功 %s 张表将跳过",
                                     plan_id,
                                     sum(1 for m in prev_manifest if m.get("status") == "ok"))
                mig = self._phase_migrate(
                    plan, with_structure="structure" in types,
                    prev_manifest=prev_manifest if resume else None)
                phases["migrate"] = mig
                self._set(plan_id, phases_json=json.dumps(phases, ensure_ascii=False))
                if not mig["ok"]:
                    self._fail(plan_id, phases, "migrate", mig.get("message", "迁移失败"))
                    return

            # ---------- 阶段 3：数据校验 ----------
            if "verify" in types:
                self._set(plan_id, status=ST_VERIFYING, current_phase="verify")
                ver = self._phase_verify(
                    plan, manifest=(phases.get("migrate") or {}).get("manifest"))
                phases["verify"] = ver
                self._set(plan_id, phases_json=json.dumps(phases, ensure_ascii=False))
                if not ver["ok"]:
                    self._fail(plan_id, phases, "verify", ver.get("message", "校验失败"))
                    return

            # ---------- 完成 + 报告 ----------
            duration = round(time.monotonic() - started, 1)
            phases["report"] = {
                "status": "completed",
                "duration_sec": duration,
                "migrate_types": types,
                "precheck": phases.get("precheck", {}),
                "migrate": phases.get("migrate", {}),
                "verify": phases.get("verify", {}),
                "generated_at": db.now_iso(),
            }
            self._set(plan_id, status=ST_COMPLETED, current_phase="report",
                      phases_json=json.dumps(phases, ensure_ascii=False),
                      finished_at=db.now_iso())
            db.add_log("INFO", "db_migrate",
                       f"迁移计划 #{plan_id} 完成（{plan['src_db_type']}"
                       f"{plan['src_db_name']} → {plan['tgt_db_type']}"
                       f"{plan['tgt_db_name']}，耗时 {duration}s）")
            self.logger.info("[db_migrate] 计划 #%s 完成", plan_id)
        except Exception as exc:
            self.logger.exception("[db_migrate] 计划 #%s 执行异常", plan_id)
            self._fail(plan_id, phases, plan.get("current_phase") or "precheck",
                       f"执行异常: {exc}")

    def _fail(self, plan_id: int, phases: dict, phase: str, message: str) -> None:
        phases[phase] = {**(phases.get(phase) or {}), "ok": False, "message": message}
        self._set(plan_id, status=ST_FAILED, current_phase=phase,
                  phases_json=json.dumps(phases, ensure_ascii=False),
                  error_msg=message[:500], finished_at=db.now_iso())
        db.add_log("ERROR", "db_migrate", f"迁移计划 #{plan_id} 在 {phase} 阶段失败: {message}")

    # ------------------------- 阶段实现 -------------------------
    def _phase_precheck(self, plan: dict) -> dict:
        """预检查：源/目标连通性、目标库自动创建（MySQL）、源对象统计。"""
        from core import probe
        result: Dict[str, Any] = {"ok": False, "phase": "precheck",
                                  "checks": [], "started_at": db.now_iso()}
        # 源连通性
        ok, msg = probe.probe_db_connection(
            plan["src_db_type"], plan["src_host"], plan["src_port"],
            plan["src_username"], plan["src_password"], plan["src_db_name"])
        result["checks"].append({"item": "源库连通性", "ok": bool(ok), "message": msg})
        if not ok:
            return result
        # 目标连通性（不指定库，仅实例可达性由后续建库探测覆盖；这里按库探测）
        # 达梦/Oracle 的 tgt_db_name 实为 schema：不能按"库名"探测（实测 137
        # 达梦以 SYSDBA 为库名连接失败 → 误判目标不存在 → 误报"不支持自动
        # 建库"）。改为实例连通 + schema 存在性检查 + 自动建 schema。
        if plan["tgt_db_type"] in ("dameng", "oracle"):
            ok2, msg2, target_db_ready = self._check_schema_target(plan)
            result["checks"].append({"item": "目标 schema 检查", "ok": bool(target_db_ready),
                                     "message": msg2})
            if not target_db_ready:
                return result
        else:
            ok2, msg2 = probe.probe_db_connection(
                plan["tgt_db_type"], plan["tgt_host"], plan["tgt_port"],
                plan["tgt_username"], plan["tgt_password"], plan["tgt_db_name"])
            target_db_ready = bool(ok2)
        if not target_db_ready:
            # 目标库不存在 → MySQL 目标自动创建
            created, create_msg = self._ensure_target_db(plan)
            result["checks"].append({"item": "目标库自动创建", "ok": created,
                                     "message": create_msg})
            if created:
                ok3, msg3 = probe.probe_db_connection(
                    plan["tgt_db_type"], plan["tgt_host"], plan["tgt_port"],
                    plan["tgt_username"], plan["tgt_password"], plan["tgt_db_name"])
                result["checks"].append({"item": "目标库连通性(创建后)", "ok": bool(ok3),
                                         "message": msg3})
                target_db_ready = bool(ok3)
        else:
            result["checks"].append({"item": "目标库连通性", "ok": True, "message": msg2})
        if not target_db_ready:
            return result
        # 源对象统计（表数量/行数）
        stats = self._source_stats(plan)
        result["source_tables"] = stats.get("tables", 0)
        result["source_rows"] = stats.get("rows", 0)
        note = stats.get("note") or ""
        if note:
            # 统计不到（驱动缺失/权限不足/连不上）≠ 源库没有表：如实写明原因，
            # 但不阻断——迁移阶段的表清单由同步引擎按源库实时获取，不依赖这里。
            result["checks"].append({
                "item": "源对象统计", "ok": False,
                "message": f"{note}（不阻断：迁移阶段按源库实际表执行）"})
            result["source_stats_note"] = note
            result["ok"] = True
        else:
            result["checks"].append({
                "item": "源对象统计", "ok": result["source_tables"] > 0,
                "message": f"表 {result['source_tables']} 张 / 约 {result['source_rows']} 行"})
            result["ok"] = result["source_tables"] > 0
            if not result["ok"]:
                result["message"] = "源库没有可迁移的业务表"
        result["finished_at"] = db.now_iso()
        return result

    def _check_schema_target(self, plan: dict) -> tuple:
        """schema 语义目标（达梦/Oracle）的存在性检查与自动创建。

        返回 (ok, message, ready)。达梦：默认库连接 + dba_users 查属主，
        schema 缺失时 CREATE SCHEMA AUTHORIZATION <登录用户>；Oracle：缺
        schema（用户）时 CREATE USER + GRANT（resource）。
        """
        dbtype = plan["tgt_db_type"]
        schema_name = (plan["tgt_db_name"] or "").strip()
        host = plan["tgt_host"]
        port = plan["tgt_port"]
        user = plan["tgt_username"]
        pwd = _dec_secret(plan["tgt_password"] or "")

        conn = None
        try:
            if dbtype == "dameng":
                import dmPython
                conn = dmPython.connect(user=user, password=pwd,
                                        server=host, port=int(port or 5236))
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM dba_users WHERE username=?",
                            (schema_name.upper(),))
                exists = (cur.fetchone() or [0])[0] > 0
                if exists:
                    return True, f"目标 schema {schema_name} 已存在（达梦实例连通）", True
                cur.execute('CREATE SCHEMA "{}" AUTHORIZATION "{}"'.format(
                    schema_name.upper(), (user or "SYSDBA").upper()))
                conn.commit()
                return True, f"目标 schema {schema_name} 不存在，已自动创建（达梦）", True
            else:  # oracle
                import oracledb
                conn = oracledb.connect(user=user, password=pwd,
                                        host=host, port=int(port or 1521),
                                        service_name=schema_name or "orcl")
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM all_users WHERE username=:u",
                            u=schema_name.upper())
                exists = (cur.fetchone() or [0])[0] > 0
                if exists:
                    return True, f"目标 schema {schema_name} 已存在（Oracle 实例连通）", True
                cur.execute('CREATE USER "{}" IDENTIFIED BY "{}"'.format(
                    schema_name.upper(), f"Aidbm#{int(time.time())}"))
                cur.execute('GRANT CONNECT, RESOURCE TO "{}"'.format(schema_name.upper()))
                conn.commit()
                return True, f"目标 schema {schema_name} 不存在，已自动创建（Oracle，初始密码见日志）", True
        except Exception as exc:
            detail = str(exc)[:200]
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            return False, f"{dbtype} 目标 schema 检查失败: {detail}", False
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _ensure_target_db(self, plan: dict) -> tuple:
        """目标库不存在时自动创建。

        - MySQL/MariaDB：``CREATE DATABASE \```name\`` CHARACTER SET utf8mb4``；
        - PostgreSQL/金仓：连维护库（postgres/test）执行 ``CREATE DATABASE "name"``
          （PG 系建库不能在事务块里，``native_conn`` 已置 autocommit）；
        - Oracle/达梦/SQL Server：库/表空间/文件组语义复杂且属生产敏感操作，
          不擅自创建，如实提示"请先手工创建目标库"。

        真实缺陷背景：原先只有 MySQL/MariaDB 支持自动建库，其它类型一律返回
        "不支持自动建库"→预检查直接不过；而界面上这些类型此前又根本选不到。
        """
        tgt_type = (plan.get("tgt_db_type") or "").lower()
        name = plan.get("tgt_db_name") or ""
        host = plan.get("tgt_host") or ""
        port = int(plan.get("tgt_port") or 0) or None
        user = plan.get("tgt_username") or ""
        pwd = plan.get("tgt_password") or ""

        if tgt_type in ("mysql", "mariadb"):
            try:
                import pymysql
                conn = pymysql.connect(
                    host=host, port=port or 3306,
                    user=user, password=pwd,
                    connect_timeout=5, charset="utf8mb4")
            except Exception as e:  # noqa: BLE001
                return False, f"目标实例连接失败: {e}"
            try:
                if not re.match(r"^[A-Za-z0-9_$]+$", name):
                    return False, "目标库名含特殊字符，请手工创建"
                with conn.cursor() as cur:
                    cur.execute("SHOW DATABASES LIKE %s", (name,))
                    if cur.fetchone():
                        return True, "目标库已存在"
                    cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
                    conn.commit()
                    return True, f"目标库 {name} 已自动创建"
            except Exception as e:  # noqa: BLE001
                return False, f"自动建库失败: {e}"
            finally:
                conn.close()

        if tgt_type in ("postgresql", "kingbase"):
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
                return False, "目标库名含特殊字符，请手工创建"
            from core import native_conn
            maint = "postgres" if tgt_type == "postgresql" else "test"
            try:
                conn = native_conn.connect(tgt_type, host, port, maint, user, pwd,
                                           timeout=8)
            except Exception as e:  # noqa: BLE001
                return False, f"目标实例连接失败: {e}"
            try:
                safe = name.replace("'", "''")
                cur = conn.cursor()
                try:
                    # 金仓不同版本的字典表名不同（pg_database / sys_database），
                    # 逐个探测，避免因版本差异误判"库不存在"而重复建库。
                    for sql in (f"SELECT 1 FROM pg_database WHERE datname='{safe}'",
                                f"SELECT 1 FROM sys_database WHERE datname='{safe}'"):
                        try:
                            cur.execute(sql)
                            if cur.fetchone():
                                return True, "目标库已存在"
                        except Exception:  # noqa: BLE001 - 换下一个字典表
                            continue
                finally:
                    try:
                        cur.close()
                    except Exception:  # noqa: BLE001
                        pass
                cur = conn.cursor()
                try:
                    cur.execute(f'CREATE DATABASE "{name}"')
                finally:
                    try:
                        cur.close()
                    except Exception:  # noqa: BLE001
                        pass
                return True, f"目标库 {name} 已自动创建"
            except Exception as e:  # noqa: BLE001
                return False, f"自动建库失败: {e}"
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

        return False, (f"目标类型 {plan.get('tgt_db_type')} 不支持自动建库，"
                       "请先手工创建目标库")

    @staticmethod
    def _source_stats(plan: dict) -> dict:
        """源库对象统计：表数量与总行数（覆盖全部已支持库型）。

        返回 ``{"tables": n, "rows": n, "note": "..."}``；``note`` 非空表示
        "统计不到"（驱动缺失/权限不足/连不上），调用方据此**不阻断**迁移。
        行数取自各库的系统统计信息（information_schema.table_rows /
        pg_stat_user_tables / all_tables.num_rows / sys.partitions），是**采样值**，
        只用于预检查展示与迁移前后对照；迁移结果以 verify 阶段逐表实时
        COUNT(*) 为准。
        """
        db_type = (plan.get("src_db_type") or "").lower()
        db_name = (plan.get("src_db_name") or "").strip()
        if not db_type or not db_name:
            return {"tables": 0, "rows": 0, "note": "源类型或库名缺失"}
        host = plan.get("src_host") or ""
        port = int(plan.get("src_port") or 0) or None
        user = (plan.get("src_username") or "").strip()
        pwd = plan.get("src_password") or ""

        def q(v: str) -> str:
            return (v or "").replace("'", "''")

        # Oracle/达梦按"用户 schema"统计，用户名缺省时用库名
        owner = q(user or db_name)
        if db_type in ("mysql", "mariadb"):
            sqls = (
                "SELECT COUNT(*) FROM information_schema.tables"
                f" WHERE table_schema='{q(db_name)}' AND table_type='BASE TABLE'",
                "SELECT IFNULL(SUM(table_rows),0) FROM information_schema.tables"
                f" WHERE table_schema='{q(db_name)}'")
        elif db_type in ("postgresql", "kingbase"):
            # 排除系统 schema，只数真实业务基表
            sqls = (
                "SELECT COUNT(*) FROM information_schema.tables"
                " WHERE table_schema NOT IN ('pg_catalog','information_schema',"
                "'sys_catalog','sys') AND table_type='BASE TABLE'",
                "SELECT COALESCE(SUM(n_live_tup),0) FROM pg_stat_user_tables")
        elif db_type in ("oracle", "dameng"):
            sqls = (
                f"SELECT COUNT(*) FROM all_tables WHERE owner=UPPER('{owner}')",
                f"SELECT NVL(SUM(num_rows),0) FROM all_tables WHERE owner=UPPER('{owner}')")
        elif db_type == "sqlserver":
            sqls = (
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES"
                f" WHERE TABLE_CATALOG='{q(db_name)}' AND TABLE_TYPE='BASE TABLE'",
                "SELECT ISNULL(SUM(p.rows),0) FROM sys.partitions p"
                " JOIN sys.objects o ON o.object_id=p.object_id"
                " WHERE o.type='U' AND p.index_id IN (0,1)")
        else:
            return {"tables": 0, "rows": 0,
                    "note": f"暂不支持统计该源类型（{db_type}）"}

        try:
            conn = _stats_connect(db_type, host, port, db_name, user, pwd)
        except Exception as e:  # noqa: BLE001 - 连接失败不算致命，转成 note
            return {"tables": 0, "rows": 0, "note": f"源库连接失败: {str(e)[:200]}"}
        try:
            tables = int(_scalar(conn, sqls[0]) or 0)
            rows = int(_scalar(conn, sqls[1]) or 0)
            return {"tables": tables, "rows": rows}
        except Exception as e:  # noqa: BLE001 - 统计 SQL 失败同样不阻断
            return {"tables": 0, "rows": 0, "note": f"源对象统计失败: {str(e)[:200]}"}
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _phase_migrate(self, plan: dict, with_structure: bool,
                       prev_manifest: Optional[list] = None) -> dict:
        """结构迁移 + 全量迁移：复用同步引擎（create_if_not_exists + 全库）。

        断点续跑（对标 influxdb2tsfile --resume）：计划失败后重跑时，依据
        上次 manifest 跳过已成功的表，只补做失败/缺失的表；append 模式下
        失败表可能残留部分写入的行，重迁前先清空目标表，保证单元级幂等。
        """
        from core.sync.engine import run_sync_task_with_task
        skip_tables: list = []
        cleared_tables: list = []
        clear_warnings: list = []
        if prev_manifest:
            for m in prev_manifest:
                if m.get("status") == "ok" and m.get("source_table"):
                    skip_tables.append(m["source_table"])
            if not with_structure:
                # 仅全量（append）模式：失败表目标端可能已有部分行，重迁前清空
                for m in prev_manifest:
                    if m.get("status") != "ok" and m.get("source_table"):
                        ok, msg = self._clear_target_table(plan, m["source_table"])
                        if ok:
                            cleared_tables.append(m["source_table"])
                        else:
                            clear_warnings.append(
                                f"{m['source_table']}: {msg}")
        synthetic_task = {
            "id": -int(plan["id"]),  # 负数 id 避免与真实同步任务混淆（仅日志用）
            "name": f"迁移计划-{plan['name']}",
            "src_db_type": plan["src_db_type"],
            "src_host": plan["src_host"],
            "src_port": plan["src_port"],
            "src_username": plan["src_username"],
            "src_password": plan["src_password"],
            "src_db_name": plan["src_db_name"],
            "tgt_db_type": plan["tgt_db_type"],
            "tgt_host": plan["tgt_host"],
            "tgt_port": plan["tgt_port"],
            "tgt_username": plan["tgt_username"],
            "tgt_password": plan["tgt_password"],
            "tgt_db_name": plan["tgt_db_name"],
            "sync_mode": "full",
            # 迁移=一次性任务：结构+全量阶段按当前字段映射重建目标表
            # （DTS 全量迁移语义），保证列名/类型与映射一致且可重复执行
            "save_mode": "overwrite" if with_structure else "append",
            "full_db_migrate": True,
            "validate_before_run": False,
            "verify_after_run": False,
            "skip_tables": skip_tables,
        }
        started = time.monotonic()
        res = run_sync_task_with_task(synthetic_task)
        # manifest（对标 influxdb2tsfile manifest.json）：逐表记录状态、行数、
        # 错误明细，作为断点续跑依据与迁移产物追溯凭据
        manifest = []
        for t in res.get("tables", []):
            manifest.append({
                "source_table": t.get("table"),
                "target_table": t.get("table"),
                "status": "ok" if t.get("success") else "failed",
                "rows_read": t.get("total_read", 0),
                "rows_written": t.get("total_write", 0),
                "errors": t.get("errors", 0),
                "error_detail": (t.get("error_details") or [])[:3],
            })
        executed = {m["source_table"] for m in manifest}
        for m in (prev_manifest or []):
            if m.get("status") == "ok" and m.get("source_table") not in executed:
                manifest.append({**m, "resumed": True})
        # 达梦目标：迁移完成后逐表收集统计信息（SP_TAB_STAT_INIT），
        # 否则优化器没有统计，执行计划可能严重劣化（信创迁移高频踩坑）
        stats_init = None
        if res.get("success") and (plan.get("tgt_db_type") or "").lower() == "dameng":
            try:
                stats_init = self._collect_dm_table_stats(
                    plan, [m["source_table"] for m in manifest
                           if m.get("status") == "ok"])
            except Exception as e:  # noqa: BLE001 - 统计收集绝不阻断迁移结论
                stats_init = {"executed": False, "reason": str(e)[:200]}
        result = {
            "ok": bool(res.get("success")),
            "phase": "migrate",
            "structure": ("create_if_not_exists" if with_structure else "跳过"),
            "resume": bool(prev_manifest),
            "skipped_tables": skip_tables,
            "cleared_tables": cleared_tables,
            "stats_init": stats_init,
            "manifest": manifest,
            "message": res.get("message", ""),
            "total_read": res.get("total_read", 0),
            "total_write": res.get("total_write", 0),
            "tables": res.get("tables", []),
            "duration_sec": round(time.monotonic() - started, 1),
            "started_at": db.now_iso(),
        }
        if clear_warnings:
            result["clear_warnings"] = clear_warnings
        # 引擎预校验未通过时，把明细透出（此前只显示"N 项失败"，看不到哪项）
        if res.get("precheck_failed") and res.get("precheck"):
            result["precheck"] = res["precheck"]
        return result

    def _phase_verify(self, plan: dict, manifest: Optional[list] = None) -> dict:
        """数据校验：逐表行数比对（源 vs 目标）+ manifest 对照。"""
        db_type = plan["src_db_type"].lower()
        tables = []
        if db_type in ("mysql", "mariadb"):
            import pymysql
            conn = pymysql.connect(
                host=plan["src_host"], port=int(plan["src_port"] or 3306),
                user=plan["src_username"], password=plan["src_password"],
                database=plan["src_db_name"], connect_timeout=5, charset="utf8mb4")
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT table_name FROM information_schema.tables"
                                " WHERE table_schema=%s AND table_type='BASE TABLE'"
                                " ORDER BY table_name", (plan["src_db_name"],))
                    tables = [r[0] for r in cur.fetchall()]
            finally:
                conn.close()
        elif db_type == "postgresql":
            import psycopg2
            conn = psycopg2.connect(
                host=plan["src_host"], port=int(plan["src_port"] or 5432),
                user=plan["src_username"], password=plan["src_password"],
                dbname=plan["src_db_name"], connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT table_name FROM information_schema.tables"
                                " WHERE table_schema='public' AND table_type='BASE TABLE'"
                                " ORDER BY table_name")
                    tables = [r[0] for r in cur.fetchall()]
            finally:
                conn.close()

        results = []
        all_match = True
        # 抽样内容比对：行数一致不能证明内容一致（金额截断、状态映射错位、
        # 时区偏移只有内容级校验能发现）。verify_sample_rows=0 可关闭。
        sample_rows = 100 if plan.get("verify_sample_rows") is None \
            else int(plan.get("verify_sample_rows") or 0)
        sampled = 0
        content_diffs = 0
        manifest_diffs = 0
        m_rows = self._manifest_rows_map(manifest)
        from core.data_compare import _norm_val
        for tb in tables:
            src_cnt = self._count_rows(plan["src_db_type"], plan["src_host"],
                                       plan["src_port"], plan["src_username"],
                                       plan["src_password"], plan["src_db_name"], tb)
            tgt_cnt = self._count_rows(plan["tgt_db_type"], plan["tgt_host"],
                                       plan["tgt_port"], plan["tgt_username"],
                                       plan["tgt_password"], plan["tgt_db_name"], tb)
            match = src_cnt == tgt_cnt
            entry = {"table": tb, "source_rows": src_cnt,
                     "target_rows": tgt_cnt, "match": match}
            # manifest 对照：迁移时每表写入行数 vs 校验时实际行数，不一致说明
            # 迁移后目标端又发生了变化（或存在绕过迁移链路的写入）
            if m_rows:
                mw = m_rows.get(str(tb).lower())
                if mw is not None:
                    entry["manifest_rows"] = mw
                    if mw != tgt_cnt:
                        entry["manifest_diff"] = True
                        manifest_diffs += 1
            if match and sample_rows > 0 and sampled < 50:
                try:
                    src_rows = self._fetch_sample(
                        plan["src_db_type"], plan["src_host"], plan["src_port"],
                        plan["src_username"], plan["src_password"],
                        plan["src_db_name"], tb, sample_rows)
                    tgt_rows = self._fetch_sample(
                        plan["tgt_db_type"], plan["tgt_host"], plan["tgt_port"],
                        plan["tgt_username"], plan["tgt_password"],
                        plan["tgt_db_name"], tb, sample_rows)
                    if src_rows is not None and tgt_rows is not None:
                        sampled += 1
                        diff = self._diff_samples(src_rows, tgt_rows)
                        entry["sample_rows"] = min(sample_rows,
                                                   max(len(src_rows), len(tgt_rows)))
                        entry["sample_diff"] = diff
                        if diff:
                            entry["match"] = False
                            entry["content_mismatch"] = True
                            content_diffs += 1
                except Exception as e:  # 抽样失败不阻断行数校验结论
                    entry["sample_error"] = str(e)[:200]
            all_match = all_match and entry["match"]
            results.append(entry)

        ok = bool(results) and all_match
        if content_diffs:
            msg = (f"{content_diffs} 张表抽样内容不一致（可能因源端持续写入，"
                   "或存在字段截断/映射错位），详见明细")
        elif ok:
            msg = (f"全部 {len(results)} 张表行数一致"
                   + (f"，抽样内容比对 {sampled} 张通过" if sampled else ""))
        else:
            msg = "存在行数或内容不一致的表，详见明细"
        return {
            "ok": ok,
            "phase": "verify",
            "check_type": "row_count_and_sample_compare",
            "tables_total": len(results),
            "tables_matched": sum(1 for r in results if r["match"]),
            "sampled_tables": sampled,
            "content_mismatched": content_diffs,
            "manifest_compared": bool(m_rows),
            "manifest_mismatched": manifest_diffs,
            "tables": results,
            "message": msg,
            "finished_at": db.now_iso(),
        }

    @staticmethod
    def _manifest_rows_map(manifest: Optional[list]) -> dict:
        """manifest → {表名(小写): 迁移时写入行数}（大小写不敏感，兼容达梦/Oracle 大写）。"""
        out = {}
        for m in manifest or []:
            if m.get("status") == "ok" and m.get("source_table"):
                out[str(m["source_table"]).lower()] = int(m.get("rows_written") or 0)
        return out

    @staticmethod
    def _diff_samples(src_rows, tgt_rows) -> int:
        """逐行比对两侧归一化抽样行，返回差异行数。"""
        norm = DbMigrationEngine._norm_sample_rows
        s_rows = norm(src_rows)
        t_rows = norm(tgt_rows)
        diff = 0
        for i in range(max(len(s_rows), len(t_rows))):
            s = s_rows[i] if i < len(s_rows) else None
            t = t_rows[i] if i < len(t_rows) else None
            if s != t:
                diff += 1
        return diff

    @staticmethod
    def _norm_sample_rows(rows):
        """归一化抽样行：bool→0/1（pymysql tinyint(1) 返回 bool，
        与 PG smallint 的 1/0 直比会误报），其余复用数据对比的归一化规则
        （None/bytes/datetime/float 统一成可比对字符串）。"""
        from core.data_compare import _norm_val
        out = []
        for r in rows or []:
            out.append(tuple("1" if v is True else "0" if v is False
                             else _norm_val(v) for v in r))
        return out

    @staticmethod
    def _fetch_sample(db_type, host, port, user, password, database, table, n):
        """按第一列排序取前 n 行（内容校验抽样）。不支持的原生库型返回 None。"""
        from core.data_compare import _get_columns, _sample_sql
        db_type = (db_type or "").lower()
        if db_type in ("mysql", "mariadb"):
            import pymysql
            conn = pymysql.connect(host=host, port=int(port or 3306), user=user,
                                   password=password, database=database,
                                   connect_timeout=5, charset="utf8mb4")
        elif db_type == "postgresql":
            import psycopg2
            conn = psycopg2.connect(host=host, port=int(port or 5432), user=user,
                                    password=password, dbname=database,
                                    connect_timeout=5)
        else:
            return None
        try:
            cur = conn.cursor()
            cols = _get_columns(conn, db_type, database, "", table)
            if not cols:
                return None
            if db_type in ("mysql", "mariadb"):
                ref = "`{}`".format(table.replace("`", ""))
            else:
                ref = '"{}"'.format(table.replace('"', ""))
            cur.execute(_sample_sql(db_type, ref, cols[0], int(n)))
            return cur.fetchall()
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _count_rows(db_type, host, port, user, password, database, table) -> int:
        db_type = (db_type or "").lower()
        # 达梦 / Oracle / 金仓：JDBC 通道（驱动 jar 随离线包内置，零安装）
        if db_type in ("dameng", "oracle", "kingbase"):
            from core import jdbc as _jdbc
            # 达梦/Oracle 未加引号标识符自动转大写，目标表由 writer
            # 按 _upper 建表 → 查询表名必须大写
            tbl = table.replace('"', "").upper()
            q = f'"{tbl}"'
            if database:
                q = f'"{database.replace(chr(34), "")}".' + q
            sql = f"SELECT COUNT(*) FROM {q}"
            conn = _jdbc.connect(db_type, host, int(port or 0),
                                 database or "", user, password or "")
            try:
                cur = conn.cursor()
                cur.execute(sql)
                return int(cur.fetchone()[0])
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        if db_type in ("mysql", "mariadb"):
            import pymysql
            conn = pymysql.connect(host=host, port=int(port or 3306), user=user,
                                   password=password, database=database,
                                   connect_timeout=5, charset="utf8mb4")
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM `{}`".format(table.replace("`", "")))
                    return int(cur.fetchone()[0])
            finally:
                conn.close()
        if db_type == "postgresql":
            import psycopg2
            conn = psycopg2.connect(host=host, port=int(port or 5432), user=user,
                                    password=password, dbname=database,
                                    connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute('SELECT COUNT(*) FROM "{}"'.format(table.replace('"', "")))
                    return int(cur.fetchone()[0])
            finally:
                conn.close()
        return -1


    # ------------------------- 预演 / 断点续跑辅助 -------------------------
    def dry_run(self, plan_id: int) -> dict:
        """迁移预演（对标 influxdb2tsfile discover + --dry-run）。

        只读操作：探查源库表清单与行数（系统统计采样值），给出将执行的
        阶段与写入方式，不读取业务数据、不写目标端。结果存入
        phases_json["dry_run"] 供前端/详情查看，不改变计划状态。
        """
        plan = self.get_plan(plan_id, hide_secret=False)
        if not plan:
            raise ValueError(f"迁移计划不存在: {plan_id}")
        if plan["status"] in (ST_CHECKING, ST_MIGRATING, ST_VERIFYING):
            raise ValueError("计划正在执行中，无法预演")
        types = plan.get("migrate_types") or []
        result: Dict[str, Any] = {
            "ok": False, "phase": "dry_run",
            "src": f"{plan['src_db_type']} {plan['src_host']}:{plan.get('src_port') or '-'}/"
                   f"{plan['src_db_name']}",
            "tgt": f"{plan['tgt_db_type']} {plan['tgt_host']}:{plan.get('tgt_port') or '-'}/"
                   f"{plan['tgt_db_name']}",
            "planned_phases": list(types),
            "write_mode": ("overwrite（结构+全量：目标表按源端 schema 重建，可重复执行）"
                           if "structure" in types else "append（仅全量追加，不清空目标表）"),
            "tables": [], "tables_total": 0, "approx_rows_total": 0,
            "note": ("行数取自源库系统统计信息（采样值，仅供预演评估）；"
                     "结构映射由同步引擎按列元数据自动转换（含主键/自增还原与"
                     "异源类型归一化）"),
            "generated_at": db.now_iso(),
        }
        try:
            tables, note = self._list_source_tables(plan)
            if note:
                result["note"] += f"；{note}"
            result["tables"] = tables
            result["tables_total"] = len(tables)
            result["approx_rows_total"] = sum(int(t.get("approx_rows") or 0) for t in tables)
            result["ok"] = True
            result["message"] = (f"预演完成：{len(tables)} 张表，"
                                 f"约 {result['approx_rows_total']} 行待迁移"
                                 if tables else "未获取到表清单（见 note）")
        except Exception as e:  # noqa: BLE001
            result["message"] = f"预演失败: {e}"
        phases = plan.get("phases_json") or {}
        phases["dry_run"] = result
        self._set(plan_id, phases_json=json.dumps(phases, ensure_ascii=False))
        return result

    def _list_source_tables(self, plan: dict) -> tuple:
        """源库表清单（表名 + 统计行数采样值）。返回 ([{name, approx_rows}], note)。"""
        db_type = (plan.get("src_db_type") or "").lower()
        db_name = (plan.get("src_db_name") or "").strip()
        user = (plan.get("src_username") or "").strip()
        owner = (user or db_name).replace("'", "''").upper()
        qname = db_name.replace("'", "''")
        if db_type in ("mysql", "mariadb"):
            sql = ("SELECT table_name, IFNULL(table_rows,0) FROM information_schema.tables"
                   f" WHERE table_schema='{qname}' AND table_type='BASE TABLE'"
                   " ORDER BY table_name")
        elif db_type in ("postgresql", "kingbase"):
            sql = ("SELECT t.table_name, COALESCE(s.n_live_tup, 0)"
                   " FROM information_schema.tables t"
                   " LEFT JOIN pg_stat_user_tables s ON s.relname = t.table_name"
                   " WHERE t.table_schema NOT IN ('pg_catalog','information_schema',"
                   "'sys_catalog','sys') AND t.table_type='BASE TABLE' ORDER BY 1")
        elif db_type in ("oracle", "dameng"):
            sql = (f"SELECT table_name, NVL(num_rows, 0) FROM all_tables"
                   f" WHERE owner = '{owner}' ORDER BY table_name")
        elif db_type == "sqlserver":
            sql = ("SELECT t.TABLE_NAME, ISNULL(SUM(p.rows), 0)"
                   " FROM INFORMATION_SCHEMA.TABLES t"
                   " JOIN sys.objects o ON o.name = t.TABLE_NAME AND o.type='U'"
                   " LEFT JOIN sys.partitions p ON p.object_id = o.object_id"
                   " AND p.index_id IN (0,1)"
                   f" WHERE t.TABLE_CATALOG='{qname}' AND t.TABLE_TYPE='BASE TABLE'"
                   " GROUP BY t.TABLE_NAME ORDER BY 1")
        else:
            return [], f"暂不支持预览该源类型（{db_type}）的表清单"
        conn = _stats_connect(db_type, plan.get("src_host") or "",
                              int(plan.get("src_port") or 0) or None,
                              db_name, user, plan.get("src_password") or "")
        out = []
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql)
                for r in cur.fetchall():
                    out.append({"name": str(r[0]),
                                "approx_rows": int(r[1] or 0)})
            finally:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return out, ""

    def _clear_target_table(self, plan: dict, table: str) -> tuple:
        """清空目标端单表（断点续跑时失败表重迁前的幂等保障）。

        达梦/Oracle 未加引号标识符按大写存储（与 writer 建表一致）。
        返回 (ok, message)；不支持的类型如实返回原因（调用方记 warning，
        绝不静默跳过——留着部分行重迁会制造重复）。
        """
        db_type = (plan.get("tgt_db_type") or "").lower()
        table = (table or "").replace('"', "").replace("`", "")
        if db_type in ("mysql", "mariadb"):
            ref = "`{}`".format(table.replace("`", ""))
        elif db_type in ("postgresql", "kingbase"):
            ref = '"{}"'.format(table)
        elif db_type in ("dameng", "oracle"):
            tbl = table.upper()
            ref = '"{}"'.format(tbl)
            database = (plan.get("tgt_db_name") or "").replace('"', "")
            if database:
                ref = '"{}".'.format(database) + ref
        else:
            return False, f"目标类型 {db_type} 暂不支持自动清表，请手工清空后重试"
        try:
            conn = _stats_connect(db_type, plan.get("tgt_host") or "",
                                  int(plan.get("tgt_port") or 0) or None,
                                  plan.get("tgt_db_name") or "",
                                  plan.get("tgt_username") or "",
                                  plan.get("tgt_password") or "")
        except Exception as e:  # noqa: BLE001
            return False, f"目标库连接失败: {str(e)[:200]}"
        try:
            cur = conn.cursor()
            try:
                cur.execute(f"DELETE FROM {ref}")
                conn.commit()
                return True, "已清空"
            finally:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            return False, f"清表失败: {str(e)[:200]}"
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _collect_dm_table_stats(self, plan: dict, tables: list) -> dict:
        """达梦目标迁移完成后逐表收集统计信息（CALL SP_TAB_STAT_INIT）。

        best-effort：单表失败记入 failed 列表，不阻断迁移结论（可事后手工
        补执行）；连接失败如实返回原因。
        """
        schema = (plan.get("tgt_username") or plan.get("tgt_db_name") or ""
                  ).replace("'", "").upper()
        if not schema or not tables:
            return {"executed": False, "reason": "缺少模式名或表清单"}
        try:
            conn = _stats_connect("dameng", plan.get("tgt_host") or "",
                                  int(plan.get("tgt_port") or 0) or None,
                                  plan.get("tgt_db_name") or "",
                                  plan.get("tgt_username") or "",
                                  plan.get("tgt_password") or "")
        except Exception as e:  # noqa: BLE001
            # dmPython 缺失（DriverUnavailable）等 → JDBC 兜底（jar 随离线包内置）
            try:
                from core import jdbc
                conn = jdbc.connect("dameng", plan.get("tgt_host") or "",
                                    int(plan.get("tgt_port") or 0) or None,
                                    plan.get("tgt_db_name") or "",
                                    plan.get("tgt_username") or "",
                                    plan.get("tgt_password") or "")
            except Exception as e2:  # noqa: BLE001
                return {"executed": False,
                        "reason": f"目标达梦连接失败: {str(e2)[:200]}"}
        collected, failed = [], []
        try:
            for t in tables:
                tbl = (t or "").replace('"', "").upper()
                if not tbl:
                    continue
                try:
                    cur = conn.cursor()
                    try:
                        cur.execute(f"CALL SP_TAB_STAT_INIT('{schema}', '{tbl}')")
                    finally:
                        try:
                            cur.close()
                        except Exception:  # noqa: BLE001
                            pass
                    collected.append(tbl)
                except Exception as e:  # noqa: BLE001 - 单表失败不阻断
                    failed.append({"table": tbl, "error": str(e)[:150]})
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return {"executed": True, "schema": schema,
                "collected": collected, "failed": failed}


engine = DbMigrationEngine()
