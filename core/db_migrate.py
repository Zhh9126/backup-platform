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
                mig = self._phase_migrate(plan, with_structure="structure" in types)
                phases["migrate"] = mig
                self._set(plan_id, phases_json=json.dumps(phases, ensure_ascii=False))
                if not mig["ok"]:
                    self._fail(plan_id, phases, "migrate", mig.get("message", "迁移失败"))
                    return

            # ---------- 阶段 3：数据校验 ----------
            if "verify" in types:
                self._set(plan_id, status=ST_VERIFYING, current_phase="verify")
                ver = self._phase_verify(plan)
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
        # 源对象统计（表数量）
        stats = self._source_stats(plan)
        result["source_tables"] = stats.get("tables", 0)
        result["source_rows"] = stats.get("rows", 0)
        result["checks"].append({
            "item": "源对象统计", "ok": stats.get("tables", 0) > 0,
            "message": f"表 {stats.get('tables', 0)} 张 / 约 {stats.get('rows', 0)} 行"})
        result["ok"] = result["source_tables"] > 0
        if not result["ok"]:
            result["message"] = "源库没有可迁移的业务表"
        result["finished_at"] = db.now_iso()
        return result

    def _ensure_target_db(self, plan: dict) -> tuple:
        """目标库不存在时自动创建（MySQL/MariaDB 目标）。"""
        if plan["tgt_db_type"].lower() not in ("mysql", "mariadb"):
            return False, (f"目标类型 {plan['tgt_db_type']} 不支持自动建库，"
                           "请先手工创建目标库")
        try:
            import pymysql
            conn = pymysql.connect(
                host=plan["tgt_host"], port=int(plan["tgt_port"] or 3306),
                user=plan["tgt_username"], password=plan["tgt_password"],
                connect_timeout=5, charset="utf8mb4")
        except Exception as e:
            return False, f"目标实例连接失败: {e}"
        try:
            name = plan["tgt_db_name"]
            if not re.match(r"^[A-Za-z0-9_$]+$", name):
                return False, "目标库名含特殊字符，请手工创建"
            with conn.cursor() as cur:
                cur.execute("SHOW DATABASES LIKE %s", (name,))
                if cur.fetchone():
                    return True, "目标库已存在"
                cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
                conn.commit()
                return True, f"目标库 {name} 已自动创建"
        except Exception as e:
            return False, f"自动建库失败: {e}"
        finally:
            conn.close()

    @staticmethod
    def _source_stats(plan: dict) -> dict:
        """源库对象统计：表数量与总行数（原生驱动直连）。"""
        db_type = plan["src_db_type"].lower()
        tables, total = 0, 0
        if db_type in ("mysql", "mariadb"):
            import pymysql
            conn = pymysql.connect(
                host=plan["src_host"], port=int(plan["src_port"] or 3306),
                user=plan["src_username"], password=plan["src_password"],
                database=plan["src_db_name"], connect_timeout=5, charset="utf8mb4")
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM information_schema.tables"
                                " WHERE table_schema=%s", (plan["src_db_name"],))
                    tables = int(cur.fetchone()[0])
                    cur.execute("SELECT IFNULL(SUM(table_rows),0)"
                                " FROM information_schema.tables WHERE table_schema=%s",
                                (plan["src_db_name"],))
                    total = int(cur.fetchone()[0])
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
                    cur.execute("SELECT COUNT(*) FROM information_schema.tables"
                                " WHERE table_schema='public' AND table_type='BASE TABLE'")
                    tables = int(cur.fetchone()[0])
                    cur.execute("SELECT IFNULL(SUM(n_live_tup),0) FROM pg_stat_user_tables")
                    total = int(cur.fetchone()[0])
            finally:
                conn.close()
        return {"tables": tables, "rows": total}

    def _phase_migrate(self, plan: dict, with_structure: bool) -> dict:
        """结构迁移 + 全量迁移：复用同步引擎（create_if_not_exists + 全库）。"""
        from core.sync.engine import run_sync_task_with_task
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
            "save_mode": "create_if_not_exists" if with_structure else "append",
            "full_db_migrate": True,
            "validate_before_run": False,
            "verify_after_run": False,
        }
        started = time.monotonic()
        res = run_sync_task_with_task(synthetic_task)
        result = {
            "ok": bool(res.get("success")),
            "phase": "migrate",
            "structure": ("create_if_not_exists" if with_structure else "跳过"),
            "message": res.get("message", ""),
            "total_read": res.get("total_read", 0),
            "total_write": res.get("total_write", 0),
            "tables": res.get("tables", []),
            "duration_sec": round(time.monotonic() - started, 1),
            "started_at": db.now_iso(),
        }
        return result

    def _phase_verify(self, plan: dict) -> dict:
        """数据校验：逐表行数比对（源 vs 目标）。"""
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
        for tb in tables:
            src_cnt = self._count_rows(plan["src_db_type"], plan["src_host"],
                                       plan["src_port"], plan["src_username"],
                                       plan["src_password"], plan["src_db_name"], tb)
            tgt_cnt = self._count_rows(plan["tgt_db_type"], plan["tgt_host"],
                                       plan["tgt_port"], plan["tgt_username"],
                                       plan["tgt_password"], plan["tgt_db_name"], tb)
            match = src_cnt == tgt_cnt
            all_match = all_match and match
            results.append({"table": tb, "source_rows": src_cnt,
                            "target_rows": tgt_cnt, "match": match})

        ok = bool(results) and all_match
        return {
            "ok": ok,
            "phase": "verify",
            "check_type": "row_count_compare",
            "tables_total": len(results),
            "tables_matched": sum(1 for r in results if r["match"]),
            "tables": results,
            "message": ("全部 %d 张表行数一致" % len(results)
                        if ok else "存在行数不一致的表，详见明细"),
            "finished_at": db.now_iso(),
        }

    @staticmethod
    def _count_rows(db_type, host, port, user, password, database, table) -> int:
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


engine = DbMigrationEngine()
