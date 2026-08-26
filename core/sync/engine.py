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
        sync_mode=task.get("sync_mode") or "full",
        save_mode=task.get("save_mode") or "append",
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
        if cfg.source_table:
            return [cfg.source_table]
        return []

    # ---- 核心同步 ----

    def run(self, progress_callback=None) -> Dict[str, Any]:
        """执行同步。支持单表 / 全库迁移。"""
        cfg = self.config
        if cfg.sync_mode == "realtime":
            return {
                "success": False,
                "message": (
                    "实时同步（realtime）需通过 Flink CDC 执行，"
                    "请使用 /api/sync-tasks/<id>/flink-config 获取配置并下发到 Flink 集群。"
                ),
            }
        if cfg.full_db_migrate:
            return self._run_full_migration(progress_callback)

        return self._run_single_table(cfg.source_table, cfg.target_table, progress_callback)

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

            # 写入前：禁用约束（pg2mysql 优化）
            tgt_conn = self.writer.connect()
            self._disable_constraints(tgt_conn)
            self.writer.prepare_table(tgt_conn, cols_meta)

            # 设置 reader 支持 source_table 动态切换
            self.reader.config.source_table = src_table

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
    task = models.get_sync_task(task_id)
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
