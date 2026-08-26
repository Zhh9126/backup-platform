# -*- coding: utf-8 -*-
"""
数据同步引擎：把 Source Reader → 统一类型 → Sink Writer 串起来。

同时负责：
  - 把 sync_tasks 行转换为 SyncConfig，处理 managed 源类型
  - 约束管理：写入前禁用外键/约束检查，写入后恢复（pg2mysql 核心优化）
  - 全库迁移模式：一次同步所有表
  - Schema 校验（pre-check）与迁移后校验（post-verify）

参考：pg2mysql (Go) migrator / validator / verifier。
"""
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from core import db, models
from .plugins import registry
from .plugins.base import SyncConfig
from .schema_compare import (
    CrossDBVerifier,
    MigrationVerifier,
    SchemaBuilder,
    SchemaValidator,
    VerifyResult,
    ValidationResult,
)

logger = logging.getLogger(__name__)


# ======================== 工具函数 ========================

# sync_mode / save_mode 中文别名 -> 引擎标准值（兼容历史数据与直接入库）
_SYNC_MODE_ALIASES = {
    "full": "full", "全量": "full", "全量同步": "full", "全量迁移": "full",
    "incremental": "incremental", "增量": "incremental", "增量同步": "incremental",
    "realtime": "realtime", "实时": "realtime", "实时同步": "realtime",
    "实时同步（Flink CDC）": "realtime", "flink cdc": "realtime", "flink_cdc": "realtime",
}
_SAVE_MODE_ALIASES = {
    "append": "append", "追加": "append", "追加写入": "append", "insert": "append",
    "overwrite": "overwrite", "覆盖": "overwrite", "覆盖写入": "overwrite", "truncate": "overwrite",
    "upsert": "upsert", "更新插入": "upsert", "更新或插入": "upsert",
    "create_if_not_exists": "create_if_not_exists", "不存在则创建": "create_if_not_exists",
}


def _norm_mode(value: str, aliases: Dict[str, str], default: str) -> str:
    """规范化同步/保存模式：支持中文别名，未知值回退默认。"""
    if not value:
        return default
    return aliases.get(str(value).strip().lower(), default)


def _task_to_config(task: Dict[str, Any]) -> SyncConfig:
    """把数据库 sync_tasks 行转为 SyncConfig。"""
    cfg = SyncConfig(
        task_id=task["id"],
        task_name=task.get("name", ""),
        source_type=task.get("source_type") or "manual",
        source_task_id=task.get("source_task_id"),
        src_db_type=task.get("src_db_type", ""),
        src_host=task.get("src_host", ""),
        src_port=task.get("src_port") or 0,
        src_username=task.get("src_username", ""),
        src_password=task.get("src_password", ""),
        src_db_name=task.get("src_db_name", ""),
        src_schema=task.get("src_schema", ""),
        source_table=task.get("source_table", ""),
        source_tables_list=task.get("source_tables_list") or [],
        source_where=task.get("source_where", ""),
        tgt_db_type=task.get("tgt_db_type", ""),
        tgt_host=task.get("tgt_host", ""),
        tgt_port=task.get("tgt_port") or 0,
        tgt_username=task.get("tgt_username", ""),
        tgt_password=task.get("tgt_password", ""),
        tgt_db_name=task.get("tgt_db_name", ""),
        tgt_schema=task.get("tgt_schema", ""),
        target_table=task.get("target_table", ""),
        sync_mode=_norm_mode(task.get("sync_mode"), _SYNC_MODE_ALIASES, "full"),
        save_mode=_norm_mode(task.get("save_mode"), _SAVE_MODE_ALIASES, "append"),
        column_mapping=task.get("column_mapping") or [],
        field_ide=task.get("field_ide") or "origin",
        incremental_column=task.get("incremental_column", ""),
        incremental_value=task.get("incremental_value", ""),
        batch_size=task.get("batch_size") or 1000,
        error_threshold=task.get("error_threshold") or 0,
        realtime_enabled=bool(task.get("realtime_enabled")),
        flink_config=task.get("flink_config") or {},
        full_db_migrate=bool(task.get("full_db_migrate")),
        validate_before_run=bool(task.get("validate_before_run")),
        verify_after_run=bool(task.get("verify_after_run")),
    )
    # managed 源：从 backup_tasks 读取连接信息
    if cfg.source_type == "managed" and cfg.source_task_id:
        src_task = models.get_task(cfg.source_task_id)
        if src_task:
            cfg.src_db_type = src_task.get("db_type", cfg.src_db_type)
            cfg.src_host = src_task.get("host", cfg.src_host)
            cfg.src_port = src_task.get("port", cfg.src_port)
            cfg.src_username = src_task.get("username", cfg.src_username)
            cfg.src_password = src_task.get("password", cfg.src_password)
            cfg.src_db_name = src_task.get("db_name", cfg.src_db_name)
            cfg.src_schema = src_task.get("schema", cfg.src_schema) or cfg.src_db_name
    return cfg


# ======================== 同步引擎 ========================

class SyncEngine:
    """单次同步执行器。"""

    def __init__(self, config: SyncConfig):
        self.config = config
        self.reader = registry.create_reader(config.src_db_type, config)
        self.writer = registry.create_writer(config.tgt_db_type, config)

    # ---- 连接测试 ----

    def test_source(self) -> Dict[str, Any]:
        try:
            conn = self.reader.connect()
            self.reader.close(conn=conn)
            return {"success": True, "message": "源端连接成功"}
        except Exception as e:
            logger.exception("test_source failed")
            return {"success": False, "message": f"源端连接失败：{e}"}

    def test_target(self) -> Dict[str, Any]:
        try:
            conn = self.writer.connect()
            self.writer.close(conn=conn)
            return {"success": True, "message": "目标端连接成功"}
        except Exception as e:
            logger.exception("test_target failed")
            return {"success": False, "message": f"目标端连接失败：{e}"}

    # ---- 元数据 ----

    def list_source_tables(self) -> list:
        conn = self.reader.connect()
        try:
            return self.reader.list_tables()
        finally:
            self.reader.close(conn=conn)

    def list_source_columns(self, table: str) -> list:
        conn = self.reader.connect()
        try:
            cols = self.reader.list_columns(table)
            return [{
                "name": c.name,
                "type": c.type,
                "nullable": c.nullable,
                "default": c.default,
                "max_length": c.max_length,
                "numeric_precision": c.numeric_precision,
                "numeric_scale": c.numeric_scale,
                "is_primary": getattr(c, "is_primary", False),
            } for c in cols]
        finally:
            self.reader.close(conn=conn)

    # ---- 全库表列表 ----

    def _get_tables_to_sync(self) -> List[str]:
        """决定要同步哪些表（单表 or 全库）。"""
        cfg = self.config
        if cfg.full_db_migrate:
            tables = self.list_source_tables()
            logger.info("全库迁移：共 %d 张表", len(tables))
            return tables
        if cfg.source_tables_list:
            return [t.strip() for t in cfg.source_tables_list if t and t.strip()]
        if cfg.source_table:
            return [cfg.source_table]
        return []

    # ---- 核心同步 ----

    def run(self, progress_callback=None) -> Dict[str, Any]:
        """执行同步。支持单表 / 多表 / 全库迁移。"""
        cfg = self.config
        if cfg.sync_mode == "realtime":
            return self._run_realtime(progress_callback)
        if cfg.full_db_migrate:
            return self._run_full_migration(progress_callback)

        # 单表 / 多表：统一从 _get_tables_to_sync 解析（source_table 或 source_tables_list）
        tables = self._get_tables_to_sync()
        if not tables:
            return {
                "success": False,
                "message": "未指定要同步的表（source_table / source_tables_list 为空）",
                "total_read": 0,
                "total_write": 0,
                "errors": 0,
                "duration": 0.0,
            }
        if len(tables) == 1:
            return self._run_single_table(tables[0], cfg.target_table or tables[0], progress_callback)

        # 多表：逐个同步并聚合结果
        start = datetime.now()
        total_read = total_write = errors = 0
        per_table = []
        for tbl in tables:
            res = self._run_single_table(tbl, cfg.target_table or tbl, progress_callback)
            per_table.append({
                "table": tbl,
                "success": res.get("success"),
                "message": res.get("message"),
                "total_read": res.get("total_read", 0),
                "total_write": res.get("total_write", 0),
                "errors": res.get("errors", 0),
            })
            total_read += res.get("total_read", 0)
            total_write += res.get("total_write", 0)
            errors += res.get("errors", 0)
        duration = (datetime.now() - start).total_seconds()
        ok = all(t["success"] for t in per_table)
        return {
            "success": ok,
            "message": f"多表同步完成：{len(tables)} 张表，读取 {total_read} 行，写入 {total_write} 行，错误 {errors} 行",
            "total_read": total_read,
            "total_write": total_write,
            "errors": errors,
            "duration": duration,
            "per_table": per_table,
        }

    def _run_single_table(self, src_table: str, dst_table: str,
                          progress_callback=None) -> Dict[str, Any]:
        """同步单张表。"""
        cfg = self.config
        start = datetime.now()
        total_read = 0
        total_write = 0
        errors = 0
        next_value = None

        table = dst_table or src_table

        try:
            src_conn = self.reader.connect()
            src_cur = src_conn.cursor()
            cols_meta = self.reader.list_columns(src_table)

            # 设置表名（reader/writer 共享 config，prepare/write 都读 cfg）
            cfg.source_table = src_table
            cfg.target_table = dst_table or src_table

            # 写入前：禁用约束（pg2mysql 优化）
            tgt_conn = self.writer.connect()
            self._disable_constraints(tgt_conn)
            self.writer.prepare_table(tgt_conn, cols_meta)

            while True:
                result = self.reader.read_batch(src_cur)
                if not result.records:
                    break
                total_read += len(result.records)
                try:
                    written = self.writer.write_batch(tgt_conn, result.records, result.columns)
                    total_write += written
                except Exception as e:
                    logger.exception("write_batch failed")
                    errors += len(result.records)
                    if cfg.error_threshold and errors > cfg.error_threshold:
                        self._enable_constraints(tgt_conn)
                        self.writer.close(conn=tgt_conn)
                        raise RuntimeError(f"错误数超过阈值 {cfg.error_threshold}：{e}") from e

                if result.next_value is not None:
                    next_value = result.next_value
                if not result.has_more:
                    break
                if progress_callback:
                    progress_callback({
                        "table": src_table,
                        "total_read": total_read,
                        "total_write": total_write,
                        "errors": errors,
                    })

            # 写入后：恢复约束
            self._enable_constraints(tgt_conn)

            src_cur.close()
            self.reader.close(conn=src_conn)
            self.writer.close(conn=tgt_conn)

            # 更新增量起始值
            if cfg.sync_mode == "incremental" and cfg.incremental_column and next_value is not None:
                models.update_sync_task(cfg.task_id, {"incremental_value": str(next_value)})

            duration = (datetime.now() - start).total_seconds()
            message = f"同步完成：读取 {total_read} 行，写入 {total_write} 行，错误 {errors} 行"
            return {
                "success": errors == 0 or (cfg.error_threshold and errors <= cfg.error_threshold),
                "message": message,
                "total_read": total_read,
                "total_write": total_write,
                "errors": errors,
                "duration": duration,
                "next_value": next_value,
            }
        except Exception as e:
            logger.exception("sync run failed")
            return {
                "success": False,
                "message": f"同步失败：{e}",
                "total_read": total_read,
                "total_write": total_write,
                "errors": errors,
                "duration": (datetime.now() - start).total_seconds(),
            }

    # ------------------------------------------------------------------ #
    # 实时同步（Binlog CDC，参考 Flink CDC 思路在平台内实现）
    #   全量快照（initial）→ 记录 binlog 位置 → 增量监听 → 事件实时写入目标
    # ------------------------------------------------------------------ #
    def _run_realtime(self, progress_callback=None) -> Dict[str, Any]:
        cfg = self.config
        if cfg.src_db_type != "mysql":
            return {"success": False, "message": "实时同步当前仅支持 MySQL 源（Binlog CDC）"}
        try:
            from pymysqlreplication import BinLogStreamReader
            from pymysqlreplication.row_event import (
                DeleteRowsEvent,
                TableMapEvent,
                UpdateRowsEvent,
                WriteRowsEvent,
            )
        except ImportError:
            return {"success": False,
                    "message": "缺少 mysql-replication 库，请先 pip install mysql-replication"}

        from .realtime_runners import get_stop_event

        try:
            import pymysql
        except ImportError:
            return {"success": False, "message": "缺少 pymysql 库"}

        # 1) 检查源库 binlog 与当前位点
        src_conn = pymysql.connect(
            host=cfg.src_host, port=cfg.src_port or 3306,
            user=cfg.src_username, password=cfg.src_password,
            charset="utf8mb4", connect_timeout=8,
        )
        try:
            with src_conn.cursor() as cur:
                cur.execute("SHOW VARIABLES LIKE 'log_bin'")
                row = cur.fetchone()
                if not row or row[1] not in ("ON", "1"):
                    return {"success": False,
                            "message": "源库未开启 binlog，无法实时同步。"
                                       "请配置 log-bin=mysql-bin 且 binlog-format=ROW 后重启 MySQL"}
                cur.execute("SHOW VARIABLES LIKE 'binlog_format'")
                row = cur.fetchone()
                if not row or row[1] != "ROW":
                    return {"success": False, "message": "源库 binlog_format 必须为 ROW"}
                cur.execute("SHOW MASTER STATUS")
                ms = cur.fetchone()
                if not ms:
                    return {"success": False, "message": "无法获取源库 binlog 位点"}
                log_file, log_pos = ms[0], int(ms[1])
        finally:
            src_conn.close()

        logger.info("[sync#%s] 实时同步：binlog 位点 %s:%s，先做全量快照", cfg.task_id, log_file, log_pos)
        models.update_sync_task(cfg.task_id, {
            "message": f"实时同步启动：全量快照（binlog 位点 {log_file}:{log_pos}）...",
        })

        # 2) 全量快照（initial 语义：先快照，再按位点追增量，upsert 幂等不重不漏）
        # 注意：_run_single_table/_run_full_migration 会修改共享 cfg.source_table/target_table
        # （逐表同步），实时监听阶段必须还原，否则 binlog 事件会被映射到错误的表。
        orig_source_table, orig_target_table = cfg.source_table, cfg.target_table
        tables = cfg.source_tables_list or ([cfg.source_table] if cfg.source_table else [])
        if not tables:
            try:
                reader = registry.create_reader(cfg.src_db_type, cfg)
                tables = reader.list_tables()
            except Exception:
                tables = []
        snapshot = None
        if tables:
            snapshot = self._run_full_migration(progress_callback)
            if not snapshot.get("success") and snapshot.get("errors", 0) > 0:
                return {"success": False,
                        "message": f"实时同步全量快照失败：{snapshot.get('message')}"}
        cfg.source_table, cfg.target_table = orig_source_table, orig_target_table
        snap_msg = f"全量快照完成（{snapshot.get('total_write', 0)} 行）" if snapshot else "无表，仅监听增量"

        # 3) 增量监听（阻塞循环，直到 stop_event 置位）
        models.update_sync_task(cfg.task_id, {
            "status": "running",
            "last_status": "running",
            "message": f"实时同步运行中：{snap_msg}，binlog {log_file}:{log_pos}",
        })
        stop_ev = get_stop_event(cfg.task_id)
        stats = {"insert": 0, "update": 0, "delete": 0, "errors": 0, "started": time.time()}
        last_flush = 0.0
        writer = None
        writer_conn = None
        try:
            stream = BinLogStreamReader(
                connection_settings={
                    "host": cfg.src_host,
                    "port": cfg.src_port or 3306,
                    "user": cfg.src_username,
                    "passwd": cfg.src_password,
                    "charset": "utf8mb4",
                },
                server_id=(cfg.task_id + 10000) % 65535 + 1,
                blocking=True,
                only_events=[WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent, TableMapEvent],
                # 注意：
                # 1) 需 python-mysql-replication <1.0（0.46+）。1.x 在 MySQL 5.7（binlog
                #    无列名 metadata）下表名/列名映射错乱，导致目标端 Unknown column。
                #    0.46 默认即从 information_schema 取列名，无需 use_column_name_cache 参数。
                # 2) TableMapEvent 必须放进 only_events：packet 层会跳过不在 allowed_events
                #    里的事件，若不解析 TableMapEvent，table_map 从不更新，RowsEvent 会
                #    被映射到错误的表（表名错乱）。
                # 3) 不要传 only_schemas：0.46 该参数在 packet 解析层有 bug，多表交错时
                #    同样导致表名错乱。改在事件循环里按 schema 手动过滤。
                log_file=log_file,
                log_pos=log_pos,
                resume_stream=True,
                auto_position=None,
            )
            while not stop_ev.is_set():
                for event in stream:
                    if stop_ev.is_set():
                        break
                    try:
                        # 手动按源库过滤（0.46 only_schemas 有表映射 bug，见上注释）
                        if cfg.src_db_name and getattr(event, "schema", None) != cfg.src_db_name:
                            continue
                        if writer is None:
                            writer = registry.create_writer(cfg.tgt_db_type, cfg)
                            writer_conn = writer.connect()
                            self._disable_constraints(writer_conn)
                        self._apply_binlog_event(writer, writer_conn, event, stats)
                    except Exception as e:  # noqa: BLE001
                        stats["errors"] += 1
                        logger.exception("[sync#%s] 应用 binlog 事件失败", cfg.task_id)
                        # 写连接可能失效，重建
                        try:
                            if writer_conn:
                                writer.close(conn=writer_conn)
                        except Exception:
                            pass
                        writer, writer_conn = None, None
                        if stats["errors"] > 50:
                            raise RuntimeError(f"错误过多({stats['errors']})，实时同步中止: {e}")
                    now = time.time()
                    if now - last_flush >= 1.0:
                        last_flush = now
                        models.update_sync_task(cfg.task_id, {
                            "message": (
                                f"实时同步运行中（{int(now - stats['started'])}s）："
                                f"新增 {stats['insert']} 更新 {stats['update']} "
                                f"删除 {stats['delete']} 错误 {stats['errors']} "
                                f"binlog {log_file}")
                        })
        except RuntimeError as e:
            return {"success": False, "message": str(e)}
        finally:
            try:
                if writer_conn is not None:
                    self._enable_constraints(writer_conn)
            except Exception:
                pass
            try:
                if writer_conn is not None:
                    writer.close(conn=writer_conn)
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

        duration = round(time.time() - stats["started"], 1)
        msg = (f"实时同步已停止（运行 {duration}s）：新增 {stats['insert']} "
               f"更新 {stats['update']} 删除 {stats['delete']} 错误 {stats['errors']}")
        models.update_sync_task(cfg.task_id, {
            "status": "success",
            "last_status": "success",
            "message": msg,
        })
        return {"success": True, "message": msg, "duration": duration, **stats}

    def _apply_binlog_event(self, writer, conn: Any, event: Any, stats: Dict[str, Any]) -> None:
        """把一条 binlog 行事件应用到目标端。"""
        from pymysqlreplication.row_event import (
            DeleteRowsEvent,
            UpdateRowsEvent,
            WriteRowsEvent,
        )
        if isinstance(event, WriteRowsEvent):
            for r in event.rows:
                writer.apply_binlog_row(conn, "insert", event.schema, event.table,
                                        r["values"], None)
            stats["insert"] += len(event.rows)
        elif isinstance(event, UpdateRowsEvent):
            for r in event.rows:
                writer.apply_binlog_row(conn, "update", event.schema, event.table,
                                        r["before_values"], r["after_values"])
            stats["update"] += len(event.rows)
        elif isinstance(event, DeleteRowsEvent):
            for r in event.rows:
                writer.apply_binlog_row(conn, "delete", event.schema, event.table,
                                        r["values"], None)
            stats["delete"] += len(event.rows)

    def _run_full_migration(self, progress_callback=None) -> Dict[str, Any]:
        """全库迁移模式：遍历所有表依次同步（参考 pg2mysql migrator）。"""
        cfg = self.config
        tables = self._get_tables_to_sync()
        if not tables:
            return {"success": False, "message": "源端没有找到表"}

        start = datetime.now()
        results = []
        grand_total_read = 0
        grand_total_write = 0
        grand_errors = 0
        failed_tables = []

        logger.info("全库迁移开始：%d 张表", len(tables))

        for idx, table in enumerate(tables):
            if progress_callback:
                progress_callback({
                    "stage": "table",
                    "table": table,
                    "index": idx + 1,
                    "total": len(tables),
                })

            result = self._run_single_table(table, table, progress_callback)
            results.append({"table": table, **result})

            grand_total_read += result.get("total_read", 0)
            grand_total_write += result.get("total_write", 0)
            grand_errors += result.get("errors", 0)

            if not result.get("success"):
                failed_tables.append(table)
                if cfg.error_threshold and grand_errors > cfg.error_threshold:
                    logger.warning("全库迁移中止：错误数 %d 超过阈值 %d", grand_errors, cfg.error_threshold)
                    break

        duration = (datetime.now() - start).total_seconds()
        success = grand_errors == 0 or (cfg.error_threshold and grand_errors <= cfg.error_threshold)
        message = (
            f"全库迁移：{len(tables)} 张表，读取 {grand_total_read} 行，"
            f"写入 {grand_total_write} 行，错误 {grand_errors} 行"
        )
        if failed_tables:
            message += f"，失败表：{', '.join(failed_tables[:5])}"

        return {
            "success": success,
            "message": message,
            "total_read": grand_total_read,
            "total_write": grand_total_write,
            "errors": grand_errors,
            "duration": duration,
            "tables": results,
            "failed_tables": failed_tables,
        }

    # ---- 约束管理（pg2mysql 核心优化） ----

    def _disable_constraints(self, conn: Any) -> None:
        """写入前禁用约束（外键/触发器）。"""
        try:
            writer_plugin = registry.get_plugin(self.config.tgt_db_type)
            if hasattr(writer_plugin, "disable_constraints"):
                writer_plugin.disable_constraints(conn)
        except Exception as e:
            logger.warning("禁用约束失败（非致命）：%s", e)

    def _enable_constraints(self, conn: Any) -> None:
        """写入后恢复约束。"""
        try:
            writer_plugin = registry.get_plugin(self.config.tgt_db_type)
            if hasattr(writer_plugin, "enable_constraints"):
                writer_plugin.enable_constraints(conn)
        except Exception as e:
            logger.warning("恢复约束失败（非致命）：%s", e)

    # ---- Schema 校验（pre-check） ----

    def validate(self) -> Dict[str, Any]:
        """执行前兼容性校验（pg2mysql Validator）。"""
        cfg = self.config
        try:
            src_conn = self.reader.connect()
            tgt_conn = self.writer.connect()
            try:
                validator = SchemaValidator(
                    src_conn, cfg.src_db_type,
                    tgt_conn, cfg.tgt_db_type,
                )
                result = validator.validate_table(
                    src_table=cfg.source_table,
                    dst_table=cfg.target_table or cfg.source_table,
                    src_schema=cfg.src_schema or cfg.src_db_name,
                    dst_database=cfg.tgt_db_name,
                    dst_schema=cfg.tgt_schema,
                )
                return {
                    "success": True,
                    "validated": True,
                    "table": result.table_name,
                    "passed": result.passed,
                    "incompatible_columns": result.incompatible_columns,
                    "incompatible_row_count": result.incompatible_row_count,
                    "incompatible_row_ids": result.incompatible_row_ids,
                    "message": (
                        "Schema 校验通过" if result.passed
                        else f"Schema 校验不通过：{len(result.incompatible_columns)} 列不兼容，"
                             f"{result.incompatible_row_count} 行数据超限"
                    ),
                }
            finally:
                self.reader.close(conn=src_conn)
                self.writer.close(conn=tgt_conn)
        except Exception as e:
            logger.exception("validate failed")
            return {"success": False, "message": f"校验失败：{e}"}

    def validate_full(self) -> Dict[str, Any]:
        """全库 Schema 校验。"""
        cfg = self.config
        tables = self._get_tables_to_sync()
        if not tables:
            return {"success": False, "message": "没有可校验的表"}

        try:
            src_conn = self.reader.connect()
            tgt_conn = self.writer.connect()
            try:
                validator = SchemaValidator(
                    src_conn, cfg.src_db_type,
                    tgt_conn, cfg.tgt_db_type,
                )
                results = []
                all_pass = True
                for table in tables:
                    r = validator.validate_table(
                        src_table=table, dst_table=table,
                        src_schema=cfg.src_schema or cfg.src_db_name,
                        dst_database=cfg.tgt_db_name,
                        dst_schema=cfg.tgt_schema,
                    )
                    results.append({
                        "table": table,
                        "passed": r.passed,
                        "incompatible_columns": r.incompatible_columns,
                        "incompatible_row_count": r.incompatible_row_count,
                    })
                    if not r.passed:
                        all_pass = False

                return {
                    "success": True,
                    "validated": True,
                    "all_pass": all_pass,
                    "tables": results,
                    "message": "全库 Schema 校验通过" if all_pass else "部分表 Schema 不兼容",
                }
            finally:
                self.reader.close(conn=src_conn)
                self.writer.close(conn=tgt_conn)
        except Exception as e:
            logger.exception("validate_full failed")
            return {"success": False, "message": f"全库校验失败：{e}"}

    # ---- 迁移后校验（post-verify, pg2mysql Verifier） ----

    def verify(self) -> Dict[str, Any]:
        """迁移后逐行校验（pg2mysql EachMissingRow）。"""
        cfg = self.config
        table = cfg.target_table or cfg.source_table
        try:
            src_conn = self.reader.connect()
            tgt_conn = self.writer.connect()
            try:
                # 跨数据库 or 同数据库
                if cfg.src_db_type == cfg.tgt_db_type and cfg.src_db_type in ("mysql", "mariadb"):
                    verifier = MigrationVerifier(
                        src_conn, cfg.src_db_type,
                        tgt_conn, cfg.tgt_db_type,
                    )
                    result = verifier.verify_table(
                        table=table,
                        src_schema=cfg.src_schema or cfg.src_db_name,
                        dst_database=cfg.tgt_db_name,
                    )
                else:
                    verifier = CrossDBVerifier(
                        src_conn, cfg.src_db_type,
                        tgt_conn, cfg.tgt_db_type,
                    )
                    result = verifier.verify_table(
                        table=table,
                        src_schema=cfg.src_schema or cfg.src_db_name,
                        dst_database=cfg.tgt_db_name or cfg.tgt_schema,
                    )

                return {
                    "success": True,
                    "verified": True,
                    "table": result.table_name,
                    "passed": result.success,
                    "total_source_rows": result.total_source_rows,
                    "missing_rows": result.missing_rows,
                    "missing_ids": result.missing_ids[:50] if result.missing_ids else [],
                    "message": (
                        "数据校验通过：所有源行在目标中均存在"
                        if result.success
                        else f"数据校验不通过：{result.missing_rows}/{result.total_source_rows} 行缺失"
                    ),
                }
            finally:
                self.reader.close(conn=src_conn)
                self.writer.close(conn=tgt_conn)
        except Exception as e:
            logger.exception("verify failed")
            return {"success": False, "message": f"校验失败：{e}"}


# ======================== 对外入口 ========================

def run_sync_task(task_id: int, progress_callback=None) -> Dict[str, Any]:
    """外部入口：带 validate/verify 流程。"""
    task = models.get_sync_task(task_id, include_secret=True)
    if not task:
        return {"success": False, "message": "同步任务不存在"}
    return run_sync_task_with_task(task, progress_callback=progress_callback)


def run_sync_task_with_task(task: Dict[str, Any], progress_callback=None) -> Dict[str, Any]:
    """使用已获取 task dict 直接执行同步（含 pre-validate / post-verify）。"""
    cfg = _task_to_config(task)
    engine = SyncEngine(cfg)

    # pre-validate（可选）
    if cfg.validate_before_run:
        logger.info("Pre-check: schema validation for task #%d", cfg.task_id)
        v_result = engine.validate()
        if not v_result.get("passed"):
            return {
                "success": False,
                "message": f'Schema 校验不通过，拒绝同步：{v_result.get("message")}',
                "validate_result": v_result,
            }

    # 执行同步
    sync_result = engine.run(progress_callback=progress_callback)

    # post-verify（可选）
    if cfg.verify_after_run and sync_result.get("success"):
        logger.info("Post-check: migration verification for task #%d", cfg.task_id)
        vf_result = engine.verify()
        sync_result["verify_result"] = vf_result

    return sync_result


def test_sync_connection(task_id: int, side: str = "source") -> Dict[str, Any]:
    task = models.get_sync_task(task_id)
    if not task:
        return {"success": False, "message": "同步任务不存在"}
    cfg = _task_to_config(task)
    engine = SyncEngine(cfg)
    if side == "source":
        return engine.test_source()
    return engine.test_target()


def list_sync_tables(task_id: int) -> Dict[str, Any]:
    task = models.get_sync_task(task_id)
    if not task:
        return {"success": False, "message": "同步任务不存在"}
    cfg = _task_to_config(task)
    engine = SyncEngine(cfg)
    try:
        tables = engine.list_source_tables()
        return {"success": True, "data": tables}
    except Exception as e:
        return {"success": False, "message": f"获取表失败：{e}"}


def list_sync_columns(task_id: int, table: str) -> Dict[str, Any]:
    task = models.get_sync_task(task_id)
    if not task:
        return {"success": False, "message": "同步任务不存在"}
    cfg = _task_to_config(task)
    cfg.source_table = table
    engine = SyncEngine(cfg)
    try:
        cols = engine.list_source_columns(table)
        return {"success": True, "data": cols}
    except Exception as e:
        return {"success": False, "message": f"获取列失败：{e}"}


def validate_sync_task(task_id: int) -> Dict[str, Any]:
    """Schema 校验入口（Pre-check）。"""
    task = models.get_sync_task(task_id)
    if not task:
        return {"success": False, "message": "同步任务不存在"}
    cfg = _task_to_config(task)
    engine = SyncEngine(cfg)
    if cfg.full_db_migrate:
        return engine.validate_full()
    return engine.validate()


def verify_sync_task(task_id: int) -> Dict[str, Any]:
    """迁移校验入口（Post-verify）。"""
    task = models.get_sync_task(task_id)
    if not task:
        return {"success": False, "message": "同步任务不存在"}
    cfg = _task_to_config(task)
    engine = SyncEngine(cfg)
    return engine.verify()


def generate_flink_config(task_id: int) -> Dict[str, Any]:
    """为 realtime 模式生成 Flink CDC SQL 配置（MySQL/PostgreSQL）。"""
    task = models.get_sync_task(task_id, include_secret=True)
    if not task:
        return {"success": False, "message": "同步任务不存在"}
    cfg = _task_to_config(task)
    if cfg.sync_mode != "realtime":
        return {"success": False, "message": "只有 realtime 模式才需要 Flink CDC 配置"}

    table = cfg.source_table
    target = cfg.target_table or table
    columns = []
    try:
        reader = registry.create_reader(cfg.src_db_type, cfg)
        conn = reader.connect()
        cols = reader.list_columns(table)
        columns = [c.name for c in cols]
        reader.close(conn=conn)
    except Exception as e:
        return {"success": False, "message": f"生成 Flink 配置失败：{e}"}

    db_type = cfg.src_db_type.lower()
    if db_type in ("mysql", "mariadb"):
        source_ddl = (
            f"CREATE TABLE source_{table} (\n" +
            ",\n".join(f"  `{c}` STRING" for c in columns) +
            f"\n) WITH (\n"
            f"  'connector' = 'mysql-cdc',\n"
            f"  'hostname' = '{cfg.src_host}',\n"
            f"  'port' = '{cfg.src_port or 3306}',\n"
            f"  'username' = '{cfg.src_username}',\n"
            f"  'password' = '{cfg.src_password}',\n"
            f"  'database-name' = '{cfg.src_db_name}',\n"
            f"  'table-name' = '{table}',\n"
            f"  'scan.startup.mode' = 'initial'\n);"
        )
    elif db_type == "postgresql":
        source_ddl = (
            f"CREATE TABLE source_{table} (\n" +
            ",\n".join(f'  "{c}" STRING' for c in columns) +
            f"\n) WITH (\n"
            f"  'connector' = 'postgres-cdc',\n"
            f"  'hostname' = '{cfg.src_host}',\n"
            f"  'port' = '{cfg.src_port or 5432}',\n"
            f"  'username' = '{cfg.src_username}',\n"
            f"  'password' = '{cfg.src_password}',\n"
            f"  'database-name' = '{cfg.src_db_name}',\n"
            f"  'schema-name' = '{cfg.src_schema or 'public'}',\n"
            f"  'table-name' = '{table}',\n"
            f"  'decoding.plugin.name' = 'pgoutput',\n"
            f"  'scan.startup.mode' = 'initial'\n);"
        )
    else:
        return {"success": False, "message": f"暂不支持 {db_type} 的 Flink CDC 配置生成"}

    sink_cols = []
    for c in columns:
        mapped = next((m for m in cfg.column_mapping if m.get("source") == c), None)
        sink_cols.append(mapped.get("target", c) if mapped else c)
    sink_ddl = (
        f"CREATE TABLE sink_{target} (\n" +
        ",\n".join(f"  `{c}` STRING" for c in sink_cols) +
        f"\n) WITH (\n"
        f"  'connector' = 'jdbc',\n"
        f"  'url' = 'jdbc:mysql://{cfg.tgt_host}:{cfg.tgt_port or 3306}/{cfg.tgt_db_name}',\n"
        f"  'table-name' = '{target}',\n"
        f"  'username' = '{cfg.tgt_username}',\n"
        f"  'password' = '{cfg.tgt_password}'\n);"
    )
    insert_sql = (
        f"INSERT INTO sink_{target}\nSELECT " +
        ", ".join(f"`{c}`" for c in columns) +
        f"\nFROM source_{table};"
    )
    return {
        "success": True,
        "data": {
            "source_ddl": source_ddl,
            "sink_ddl": sink_ddl,
            "insert_sql": insert_sql,
        },
    }
