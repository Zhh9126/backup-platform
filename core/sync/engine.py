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
import os
import re
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta
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

# 水位时间戳可解析格式（回看窗口回拨 / 延迟计算共用，按精度从高到低）
_WATERMARK_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d",
)


def _parse_watermark_ts(value: str):
    """尝试把水位解析为时间戳。返回 (datetime, fmt) 或 None。"""
    v = str(value or "").strip()
    for fmt in _WATERMARK_TS_FORMATS:
        try:
            return datetime.strptime(v, fmt), fmt
        except ValueError:
            continue
    return None


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
        precheck_sample_rows=int(task.get("precheck_sample_rows") or 20),
        skip_tables=list(task.get("skip_tables") or []),
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
        # ---- 迁移前预校验（对标 DTS 预检查）：字段/结构不合适直接拒绝 ----
        if not cfg.skip_precheck:
            from .precheck import run_precheck
            report = run_precheck(cfg)
            if not report.get("passed"):
                msg = (f"预校验未通过（{report.get('fail')} 项失败），"
                       f"已阻止迁移。请修正后重试。")
                logging.getLogger("sync").warning("[precheck] %s", msg)
                return {
                    "success": False,
                    "precheck_failed": True,
                    "precheck": report,
                    "message": msg,
                    "total_read": 0,
                    "total_write": 0,
                    "errors": 0,
                    "duration": 0.0,
                }
            if report.get("warn"):
                logging.getLogger("sync").info(
                    "[precheck] 通过，%s 项警告", report.get("warn"))
        # ---- 概念区分（业界定义）：迁移=一次性任务（full，跑完即止）；
        #      同步=持续性任务（incremental/realtime，常驻保持一致）。
        #      同步任务禁止 overwrite——覆盖写入只对一次性迁移有意义，
        #      对持续同步会破坏两端一致性语义。
        if cfg.sync_mode in ("incremental", "realtime") \
                and cfg.save_mode == "overwrite":
            return {
                "success": False,
                "message": ("同步任务不支持覆盖写入（overwrite）：同步是持续性任务"
                            "（保持两端一致），覆盖写入仅适用于一次性数据迁移"
                            "（full）。请改用 upsert/append，或将任务类型改为迁移。"),
                "total_read": 0, "total_write": 0, "errors": 0, "duration": 0.0,
            }
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
            # 多表模式下 cfg.target_table 是单表语义，不能套用到所有表，
            # 否则每张表都会写入同一目标表（清空他人数据 + 列不匹配全错）
            res = self._run_single_table(tbl, tbl, progress_callback)
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
        error_details: List[str] = []   # 失败明细（脏数据审计：表名+原因+样本量）
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
                    # 零日期消毒：MySQL '0000-00-00' 在达梦/Oracle/PG 全部非法，
                    # 转为 NULL（保持行数与行语义，仅空日期语义归一）
                    from .compat_advisor import sanitize_zero_dates
                    sanitize_zero_dates(result.records, result.columns,
                                        cfg.tgt_db_type)
                    try:
                        written = self.writer.write_batch(
                            tgt_conn, result.records, result.columns)
                    except Exception as werr:
                        # 临时性异常重试（指数退避，最多 1 次重试）：
                        # 先回滚本批半提交行，再重写整批。upsert/overwrite 模式
                        # 重试幂等安全；append 模式无唯一键保护，重试可能造重复，
                        # 故不自动重试（遵循"宁可可控重复，不可恢复遗漏"，但
                        # append 语义下重复不可被目标端识别，交由错误明细披露）。
                        if cfg.save_mode == "append":
                            raise
                        try:
                            tgt_conn.rollback()
                        except Exception:
                            pass
                        time.sleep(1)
                        written = self.writer.write_batch(
                            tgt_conn, result.records, result.columns)
                        logger.warning("[sync#%s] 批次写入失败后重试成功（%s 行）: %s",
                                       cfg.task_id, len(result.records), werr)
                    total_write += written
                except Exception as e:
                    logger.exception("write_batch failed")
                    errors += len(result.records)
                    if len(error_details) < 20:
                        error_details.append(
                            f"[{src_table}] {str(e)[:300]} "
                            f"(本批 {len(result.records)} 行全部跳过)")
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

            # 写入后：提交本表事务（连接 close 不保证 commit，
            # 未提交事务会在断开时被回滚导致"写入成功却无数据"）
            try:
                tgt_conn.commit()
            except Exception:
                pass
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
            if error_details:
                message += "；失败明细: " + " | ".join(error_details[:5])
            return {
                "success": errors == 0 or (cfg.error_threshold and errors <= cfg.error_threshold),
                "message": message,
                "total_read": total_read,
                "total_write": total_write,
                "errors": errors,
                "error_details": error_details,
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
                "error_details": error_details,
                "duration": (datetime.now() - start).total_seconds(),
            }

    # ------------------------------------------------------------------ #
    # 实时同步（Binlog CDC，参考 Flink CDC 思路在平台内实现）
    #   全量快照（initial）→ 记录 binlog 位置 → 增量监听 → 事件实时写入目标
    # ------------------------------------------------------------------ #
    def _run_realtime(self, progress_callback=None) -> Dict[str, Any]:
        cfg = self.config
        # 实时引擎选择：离线环境默认 polling（纯 Python 轮询 CDC，无外部
        # 组件依赖）；MySQL/MariaDB 源（binlog 协议族）且显式配置
        # flink_config.engine="flink" 时才走 Binlog CDC 轨道（在线环境）。
        engine_kind = (cfg.flink_config or {}).get("engine") or "polling"
        if engine_kind == "flink" and cfg.src_db_type in ("mysql", "mariadb"):
            return self._run_realtime_binlog(progress_callback)
        return self._run_realtime_polling(progress_callback)

    def _run_realtime_polling(self, progress_callback=None) -> Dict[str, Any]:
        """轮询式实时同步（离线环境主力方案，纯 Python 零外部依赖）。

        原理：先全量快照（upsert 幂等），然后按增量列（时间戳/自增列）周期
        轮询 `> watermark` 的新增/变更行，upsert 到目标端。
        - 适用所有已注册插件库型（MySQL/PG/Oracle/达梦/SQLServer/金仓）
        - 时延 = poll_interval 秒级；要求表有可比较的增量列
        - 位点（watermark）持久化到 sync_tasks.realtime_watermark，重启续传，
          避免每次重启重做全量快照；快照与增量通过该交接点衔接，不重不漏
        - flink_config.poll_overlap_seconds：时间戳水位回看窗口（默认 0），
          允许少量重叠读取、由目标端 upsert 幂等吸收，消除边界丢数
        - flink_config.reconcile_every_polls：周期行数对账间隔（默认 720 轮，
          0=关闭），检测轮询模式无法捕获的 DELETE
        - 注意：轮询模式天然无法捕获 DELETE；需要删除同步的表请使用
          binlog/WAL/LogMiner 等日志级 CDC 通道，或依赖周期对账 + 数据对比补偿
        """
        cfg = self.config
        if not cfg.incremental_column:
            return {"success": False,
                    "message": "轮询实时同步需要在任务中配置增量列"
                               "（时间戳列如 updated_at 或自增列 id）"}
        from .realtime_runners import get_stop_event

        poll_interval = int((cfg.flink_config or {}).get("poll_interval", 5))
        # 回看窗口（秒）：时间戳水位向前重叠读取，重叠部分由目标端 upsert
        # 幂等吸收，消除同秒多条记录/时间精度不足造成的边界丢数
        overlap = int((cfg.flink_config or {}).get("poll_overlap_seconds", 0) or 0)
        # 1) 断点续传：持久化水位与当前增量列一致时，跳过全量快照直接续传
        #    （水位在每批写入成功后才推进，续传既不丢也不重）
        saved = self._load_persisted_watermark(cfg.task_id)
        if saved and saved.get("col") == cfg.incremental_column and saved.get("value"):
            watermark = str(saved["value"])
            logger.info("[sync#%s] 轮询实时同步：从持久化水位续传（%s >= %s）",
                        cfg.task_id, cfg.incremental_column, watermark)
            models.update_sync_task(cfg.task_id, {
                "message": f"轮询实时同步启动：从上次水位续传"
                           f"（{cfg.incremental_column} > {watermark}）...",
            })
        else:
            # 全量快照（upsert 幂等，不重不漏）
            logger.info("[sync#%s] 轮询实时同步：先执行全量快照（增量列 %s）",
                        cfg.task_id, cfg.incremental_column)
            models.update_sync_task(cfg.task_id, {
                "message": f"轮询实时同步启动：全量快照（增量列 {cfg.incremental_column}）...",
            })
            # 快照：单表任务尊重 cfg.target_table（此前走全库迁移路径，
            # 快照写到了源表名对应的表，target_table 被忽略——真机实测发现）
            if not cfg.full_db_migrate and (cfg.source_table or len(cfg.source_tables_list or []) == 1):
                snap = self._run_single_table(
                    cfg.source_tables_list[0] if cfg.source_tables_list else cfg.source_table,
                    cfg.target_table or (cfg.source_tables_list[0] if cfg.source_tables_list
                                         else cfg.source_table),
                    progress_callback)
            else:
                snap = self._run_full_migration(progress_callback)
            if not snap.get("success"):
                return snap
            # 2) 快照→增量的交接点：以源端当前最大值建立初始水位并立即持久化
            #    （全量读取期间新写入的行 > 该水位，会由增量轮询补齐，不漏）
            _t = (cfg.source_tables_list or [cfg.source_table])[0] \
                if (cfg.source_tables_list or cfg.source_table) else ""
            _ref = f"{cfg.src_schema}.{_t}" if (cfg.src_schema and _t) else (_t or "")
            watermark = (self._max_incremental_value(
                cfg, _ref, cfg.incremental_column) if _ref else "") \
                or (cfg.incremental_value or "")
            self._save_persisted_watermark(cfg.task_id, cfg.incremental_column,
                                           watermark)
        # 至少一次投递语义告警（Checkpoint 管不丢、目标端幂等管不脏）：
        # 轮询实时同步在重启续传/回看窗口时会重复读取同一窗口，目标端必须
        # 幂等才能吸收重复投递；append 模式没有唯一键保护，重复会落重复行。
        if (cfg.save_mode or "append") != "upsert":
            logger.warning(
                "[sync#%s] 轮询实时同步为「至少一次投递」语义：重启续传与回看"
                "窗口会重复读取，目标端需幂等（建议 save_mode=upsert）；"
                "当前 save_mode=%s，重复投递将在目标端产生重复行",
                cfg.task_id, cfg.save_mode)
        src_plugin = registry.get_plugin(cfg.src_db_type)
        tgt_plugin = registry.get_plugin(cfg.tgt_db_type)
        poll_count = 0
        cycles = 0
        # 周期对账：每 N 轮轮询做一次源/目标行数比对，检测轮询模式
        # 无法捕获的 DELETE（目标<源）与重复/脏数据（目标>源）
        reconcile_every = int((cfg.flink_config or {}).get(
            "reconcile_every_polls", 720) or 0)
        logger.info("[sync#%s] 轮询实时同步进入增量循环（间隔 %ss）",
                    cfg.task_id, poll_interval)
        while not stop.is_set():
            stop.wait(poll_interval)
            if stop.is_set():
                break
            reader = None
            try:
                reader = registry.create_reader(cfg.src_db_type, cfg)
                conn = reader.connect()
                cur = conn.cursor()
                table = (cfg.source_tables_list or [cfg.source_table])[0] \
                    if (cfg.source_tables_list or cfg.source_table) else ""
                schema = cfg.src_schema or ""
                table_ref = (f"{schema}.{table}" if schema else table)
                tgt_table = cfg.target_table or table
                tgt_schema = cfg.tgt_schema or ""
                target_ref = (f"{tgt_schema}.{tgt_table}" if tgt_schema else tgt_table)
                col = cfg.incremental_column
                # watermark 内联（跨驱动参数类型不一致：PG int 列与字符串
                # 参数比较会报 operator does not exist）。数字直接字面量，
                # 其他（时间戳）转义单引号后加引号。
                # 回看窗口：时间戳水位向前重叠 overlap 秒，重叠行由 upsert
                # 幂等吸收（允许可控重复，杜绝边界丢数）
                where = ""
                if watermark:
                    qwm = self._shift_watermark_back(watermark, overlap) \
                        if overlap else watermark
                    wm = str(qwm).replace("'", "''")
                    if wm.isdigit():
                        where = f" WHERE {col} > {wm}"
                    else:
                        where = f" WHERE {col} > '{wm}'"
                sql = (f"SELECT * FROM {table_ref}{where} "
                       f"ORDER BY {col}")
                cur.execute(sql)
                # JDBC 通道列名为 java.lang.String，归一化后交给 writer
                columns = [str(d[0]) for d in cur.description]
                rows = cur.fetchmany(cfg.batch_size)
                total = 0
                writer = registry.create_writer(cfg.tgt_db_type, cfg)
                wconn = writer.connect()
                try:
                    while rows:
                        records = [[tgt_plugin.type_to_java(
                            "VARCHAR", v) for v in row] for row in rows]
                        from .compat_advisor import sanitize_zero_dates
                        sanitize_zero_dates(records, columns, cfg.tgt_db_type)
                        # 批次写入临时性异常：回滚本批半提交行后重试一次
                        # （对标 Flink JDBC Sink withMaxRetries；append 模式
                        # 不重试——水位未推进，下一轮从原水位整体重读）
                        try:
                            written = writer.write_batch(wconn, records, columns)
                        except Exception as werr:
                            if (cfg.save_mode or "append") == "append":
                                raise
                            try:
                                wconn.rollback()
                            except Exception:
                                pass
                            time.sleep(1)
                            written = writer.write_batch(wconn, records, columns)
                            logger.warning(
                                "[sync#%s] 批次写入失败后重试成功（%s 行）: %s",
                                cfg.task_id, len(records), werr)
                        total += written
                        rows = cur.fetchmany(cfg.batch_size)
                finally:
                    writer.close(conn=wconn)
                if total:
                    # 先写目标端成功，再推进并持久化水位（断点提交顺序）
                    watermark = self._max_incremental_value(
                        cfg, table_ref, col)
                    poll_count += total
                    self._save_persisted_watermark(cfg.task_id, col, watermark)
                    lag = self._watermark_lag_seconds(watermark)
                    models.update_sync_task(cfg.task_id, {
                        "message": (f"轮询实时同步中：已同步 {poll_count} 行，"
                                    f"水位 {watermark}"
                                    + (f"，延迟约 {lag}s" if lag is not None else "")),
                    })
                    logger.info("[sync#%s] 轮询同步 %d 行，watermark=%s",
                                cfg.task_id, total, watermark)
                reader.close(cur, conn)
                cycles += 1
                if (reconcile_every and cycles % reconcile_every == 0
                        and table_ref and target_ref):
                    diff = self._reconcile_counts(cfg, table_ref, target_ref)
                    if diff:
                        logger.warning("[sync#%s] %s（轮询模式无法捕获 DELETE，"
                                       "建议执行数据对比定位差异，必要时重新全量快照）",
                                       cfg.task_id, diff)
                        models.update_sync_task(cfg.task_id, {
                            "message": f"轮询实时同步中：已同步 {poll_count} 行；{diff}",
                        })
            except Exception as e:
                if getattr(cfg, "debug", False) or os.environ.get("SYNC_DEBUG"):
                    logger.warning("[sync#%s] 轮询异常:\n%s",
                                   cfg.task_id, traceback.format_exc())
                logger.warning("[sync#%s] 轮询异常（将继续重试）: %s",
                               cfg.task_id, e)
                if reader:
                    try:
                        reader.close()
                    except Exception:
                        pass
        models.update_sync_task(cfg.task_id, {
            "message": f"轮询实时同步已停止（累计 {poll_count} 行）",
        })
        return {"success": True,
                "message": f"轮询实时同步已停止（累计同步 {poll_count} 行）"}

    def _max_incremental_value(self, cfg, table_ref: str, col: str) -> str:
        """查询当前增量列最大值（推进 watermark）。"""
        reader = registry.create_reader(cfg.src_db_type, cfg)
        conn = reader.connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT MAX({col}) FROM {table_ref}")
            row = cur.fetchone()
            v = row[0] if row else None
            return str(v) if v is not None else ""
        except Exception:
            return ""
        finally:
            reader.close(conn=conn)

    @staticmethod
    def _load_persisted_watermark(task_id) -> Optional[dict]:
        """读取持久化水位（sync_tasks.realtime_watermark，JSON）。"""
        try:
            row = models.get_sync_task(task_id, include_secret=True)
            raw = (row or {}).get("realtime_watermark")
            if not raw:
                return None
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("col") \
                    and data.get("value") is not None:
                return data
        except Exception:
            logger.debug("[sync#%s] 读取持久化水位失败", task_id, exc_info=True)
        return None

    @staticmethod
    def _save_persisted_watermark(task_id, col: str, value) -> None:
        """持久化水位（写入成功后调用，断点提交顺序：目标先落库再推位点）。"""
        try:
            models.update_sync_task(task_id, {"realtime_watermark": json.dumps(
                {"col": col, "value": str(value)}, ensure_ascii=False)})
        except Exception:
            logger.warning("[sync#%s] 水位持久化失败（不影响本轮数据）",
                           task_id, exc_info=True)

    @staticmethod
    def _shift_watermark_back(value: str, seconds: int) -> str:
        """把时间戳水位回拨 seconds 秒（回看窗口）。

        仅对可解析为时间戳的值生效；自增主键等数值水位原样返回
        （数值重叠会整段重读，交由用户自行权衡）。
        重叠部分由目标端 upsert 幂等吸收：允许少量重复读取，
        但绝不因边界卡得太死而永久漏数。
        """
        parsed = _parse_watermark_ts(value)
        if not parsed:
            return str(value).strip()
        dt, fmt = parsed
        return (dt - timedelta(seconds=int(seconds))).strftime(fmt)

    @staticmethod
    def _watermark_lag_seconds(value) -> Optional[int]:
        """水位为时间戳时计算同步延迟（当前时间 - 水位），秒。

        非时间戳水位（自增主键等）无法计算时间延迟，返回 None。
        用于实时同步可观测：延迟持续增大说明消费速度跟不上写入速度。
        """
        parsed = _parse_watermark_ts(value)
        if not parsed:
            return None
        dt, _ = parsed
        return max(0, int((datetime.now() - dt).total_seconds()))

    def _reconcile_counts(self, cfg, table_ref: str,
                          target_ref: str) -> Optional[str]:
        """源/目标行数对账。返回差异描述（一致返回 None）。

        轮询 CDC 看不见 DELETE，目标行数少于源端即存在静默删除；
        目标多于源端则可能是重复写入或目标端脏数据。
        """
        src_cnt = tgt_cnt = None
        try:
            reader = registry.create_reader(cfg.src_db_type, cfg)
            conn = reader.connect()
            try:
                cur = conn.cursor()
                cur.execute(f"SELECT COUNT(*) FROM {table_ref}")
                src_cnt = int(cur.fetchone()[0])
            finally:
                reader.close(conn=conn)
        except Exception:
            logger.debug("[sync#%s] 对账源端计数失败", cfg.task_id, exc_info=True)
        try:
            writer = registry.create_writer(cfg.tgt_db_type, cfg)
            wconn = writer.connect()
            try:
                wcur = wconn.cursor()
                wcur.execute(f"SELECT COUNT(*) FROM {target_ref}")
                tgt_cnt = int(wcur.fetchone()[0])
            finally:
                writer.close(conn=wconn)
        except Exception:
            logger.debug("[sync#%s] 对账目标端计数失败", cfg.task_id, exc_info=True)
        if src_cnt is None or tgt_cnt is None or src_cnt == tgt_cnt:
            return None
        return f"对账差异：源 {src_cnt} 行 / 目标 {tgt_cnt} 行"

    @staticmethod
    def _interrupt_stream(stream: Any) -> None:
        """打断 BinLogStreamReader 的阻塞读（停止实时同步时立即生效）。

        pymysql 的 Connection.close() 只 close 不 shutdown，Linux 下无法唤醒已
        阻塞在 socket.recv 的线程（实测 stream.close() 后仍卡 40s+）；必须先对
        底层 socket 做 shutdown(SHUT_RDWR)，再 close。
        """
        for conn in (getattr(stream, "_stream_connection", None),
                     getattr(stream, "_ctl_connection", None)):
            sock = getattr(conn, "_sock", None)
            raw = getattr(sock, "_sock", None)
            for s in (sock, raw):
                if s is None:
                    continue
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:  # noqa: BLE001
                    pass
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    def _run_realtime_binlog(self, progress_callback=None) -> Dict[str, Any]:
        cfg = self.config
        if cfg.src_db_type not in ("mysql", "mariadb"):
            # Binlog CDC 依赖 MySQL 协议族的 binlog（MariaDB 为同一协议族，
            # SHOW MASTER STATUS / ROW 事件 / CRC32 校验均兼容）
            return {"success": False,
                    "message": "Binlog CDC 实时同步仅支持 MySQL/MariaDB 源；"
                               "其他库型请使用轮询式实时同步（flink_config.engine=polling）"}
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

            # 停止响应看门狗：停止信号置位后主动关闭 binlog 流以打断阻塞读。
            # 否则 for event in stream 会阻塞到源库产生下一个事件，停止动作
            # 可能延迟数分钟（前端「停止」长时间无响应，实测复现）。
            def _watch_stop() -> None:
                while not stop_ev.wait(1.0):
                    pass
                self._interrupt_stream(stream)

            threading.Thread(target=_watch_stop, name=f"stop-watch-{cfg.task_id}",
                             daemon=True).start()

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
            if stop_ev.is_set():
                logger.info("[sync#%s] 收到停止信号，实时同步退出（%s）", cfg.task_id, e)
            else:
                return {"success": False, "message": str(e)}
        except Exception as e:  # noqa: BLE001
            # 停止时看门狗关闭 stream 会打断阻塞读并抛出异常（连接已关闭等），
            # 属正常停止路径，不应记为失败
            if not stop_ev.is_set():
                logger.exception("[sync#%s] binlog 监听异常终止", cfg.task_id)
                return {"success": False, "message": f"binlog 监听异常: {e}"}
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
        skipped_tables = []
        skip = set(cfg.skip_tables or [])

        logger.info("全库迁移开始：%d 张表（跳过 %d 张）", len(tables), len(skip))

        for idx, table in enumerate(tables):
            if table in skip:
                # 断点续跑：上次迁移已成功的表跳过，只补缺失/失败的表
                skipped_tables.append(table)
                continue
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
        # 有失败表一律判失败：建表/建约束这类结构级错误不产生行级 error 计数，
        # 只看 grand_errors 会把"整表根本没建起来（读 0 写 0）"当成成功，
        # 直到后续校验阶段才抛出更难定位的错误（实测 PG→MySQL 建表失败即如此）。
        success = (not failed_tables) and bool(
            grand_errors == 0 or (cfg.error_threshold and grand_errors <= cfg.error_threshold))
        message = (
            f"全库迁移：执行 {len(results)} 张 / 跳过 {len(skipped_tables)} 张"
            f"（共 {len(tables)} 张），读取 {grand_total_read} 行，"
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
            "skipped_tables": skipped_tables,
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


def _ensure_target_database(cfg: SyncConfig) -> Dict[str, Any]:
    """目标库预检/自动建库（MySQL/MariaDB 目标）。

    目标库不存在时自动 CREATE DATABASE（utf8mb4），消除
    "Unknown database" 同步失败；其他库类型仅预检提示（不自动建）。
    """
    db_type = (cfg.tgt_db_type or "").lower()
    if db_type not in ("mysql", "mariadb"):
        return {"created": False,
                "message": f"目标类型 {db_type or '?'} 不自动建库，请确认目标库已存在"}
    if not cfg.tgt_db_name:
        return {"created": False, "message": "未配置目标库名"}
    try:
        import pymysql
        conn = pymysql.connect(
            host=cfg.tgt_host, port=int(cfg.tgt_port or 3306),
            user=cfg.tgt_username, password=cfg.tgt_password or "",
            connect_timeout=5, charset="utf8mb4")
    except Exception as e:
        return {"created": False, "message": f"目标实例连接失败: {e}"}
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW DATABASES LIKE %s", (cfg.tgt_db_name,))
            if cur.fetchone():
                return {"created": False, "message": "目标库已存在"}
            if not re.match(r"^[A-Za-z0-9_$]+$", cfg.tgt_db_name):
                return {"created": False, "message": "目标库名含特殊字符，请手工建库"}
            cur.execute(f"CREATE DATABASE `{cfg.tgt_db_name}` CHARACTER SET utf8mb4")
            conn.commit()
            logger.info("[sync] 目标库 %s 不存在，已自动创建", cfg.tgt_db_name)
            return {"created": True, "message": f"目标库 {cfg.tgt_db_name} 已自动创建"}
    except Exception as e:
        return {"created": False, "message": f"目标库预检/建库失败: {e}"}
    finally:
        conn.close()


def run_sync_task_with_task(task: Dict[str, Any], progress_callback=None) -> Dict[str, Any]:
    """使用已获取 task dict 直接执行同步（含 pre-validate / post-verify）。"""
    cfg = _task_to_config(task)
    engine = SyncEngine(cfg)

    # 目标库预检/自动建库（MySQL/MariaDB 目标）
    ensure = _ensure_target_database(cfg)
    if ensure.get("message"):
        logger.info("[sync] task #%d 目标库预检: %s", cfg.task_id, ensure["message"])

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
