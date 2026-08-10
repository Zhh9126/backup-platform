# -*- coding: utf-8 -*-
"""
三级存储复制引擎：备份完成后自动执行 L1→L2→L3 的级联复制。

架构：
  L1 本地（备份第一落点，由引擎直接写入）
    ↓ 备份成功后自动触发
  L2 MinIO 热数据（高频访问、快速恢复）
    ↓ 可选：热数据到期后
  L3 S3 冷数据（长期归档、低成本容灾）

调用入口：scheduler._execute_backup() 在备份成功后调用 replicate_to_tiers()
也可通过 API /api/storage/replicate/<record_id> 手动触发。
"""
import os
import time
import logging
import threading

import core.db as db


_logger = db.get_logger("tier_replication")


def replicate_to_tiers(backup_path: str, task: dict, record_id: int,
                       logger: logging.Logger = None) -> dict:
    """对已完成的备份执行三级复制（根据用户配置的策略）。

    Args:
        backup_path: 本地备份文件绝对路径
        task: 备份任务字典
        record_id: 备份记录 ID（用于更新 storage_tier 字段）
        logger: 日志记录器

    Returns:
        各层级复制结果 {"local": bool, "minio": bool|None, "s3": bool|None}
    """
    log = logger or _logger
    result = {"minio": None, "s3": None, "local": None}
    if not backup_path or not os.path.exists(backup_path):
        log.warning("[TierReplicate] 备份文件不存在，跳过三级复制: %s", backup_path)
        return result

    strategy = _get_replication_strategy(log)

    timing = strategy.get("timing", "immediate")
    if timing != "immediate":
        delay_map = {"delay_5min": 300, "delay_30min": 1800, "delay_1hour": 3600}
        delay_secs = delay_map.get(timing, 0)
        if delay_secs > 0:
            log.info("[TierReplicate] 策略要求延迟 %d 秒后执行", delay_secs)
            time.sleep(delay_secs)

    targets = _get_enabled_targets(log)
    if not targets:
        log.info("[TierReplicate] 无已启用的存储目标，仅保留本地转储 (L1 前置)")
        _update_record_tier(record_id, "local")
        return result

    filename = os.path.basename(backup_path)
    ts = time.strftime("%Y%m%d_%H%M%S")
    object_key_base = f"{task.get('db_type', 'unknown')}/{task.get('id')}_{task.get('name', 'task')}/{ts}__{filename}"

    max_retries = int(strategy.get("max_retries", 3))
    retry_interval = int(strategy.get("retry_interval", 30))

    # L1 MinIO（热数据，第一落点）
    if strategy.get("push_l1_minio"):
        t = next((x for x in targets if x.get("type") == "minio"), None)
        if t:
            ok = _replicate_with_retry(backup_path, t, object_key_base, log,
                                       tier_label="L1-MinIO", max_retries=max_retries,
                                       retry_interval=retry_interval)
            result["minio"] = ok
            if not ok:
                log.warning("[TierReplicate] L1 MinIO 复制失败")
        else:
            log.info("[TierReplicate] 未配置 MinIO 目标，跳过 L1")

    # L2 S3（冷数据）
    if strategy.get("push_l2_s3"):
        t = next((x for x in targets if x.get("type") == "s3"), None)
        if t:
            ok = _replicate_with_retry(backup_path, t, object_key_base, log,
                                       tier_label="L2-S3", max_retries=max_retries,
                                       retry_interval=retry_interval)
            result["s3"] = ok
            if not ok:
                log.warning("[TierReplicate] L2 S3 复制失败")
        else:
            log.info("[TierReplicate] 未配置 S3 目标，跳过 L2")

    # L3 源端本地路径导出
    if strategy.get("push_l3_local"):
        t = next((x for x in targets if x.get("type") == "local"), None)
        if t:
            ok = _replicate_with_retry(backup_path, t, object_key_base, log,
                                       tier_label="L3-Local", max_retries=max_retries,
                                       retry_interval=retry_interval)
            result["local"] = ok
            if not ok:
                log.warning("[TierReplicate] L3 本地导出失败")
        else:
            log.info("[TierReplicate] 未配置本地导出目标，跳过 L3")

    tiers_achieved = [k for k in ("minio", "s3", "local") if result.get(k)]
    final_tier = "+".join(tiers_achieved) if tiers_achieved else "local"
    _update_record_tier(record_id, final_tier)
    log.info("[TierReplicate] 完成: minio=%s s3=%s local=%s → %s",
             result["minio"], result["s3"], result["local"], final_tier)
    return result


def _get_enabled_targets(logger: logging.Logger = None) -> list:
    """获取所有启用的非本地存储目标。"""
    try:
        rows = db.query(
            "SELECT * FROM storage_targets WHERE enabled=1 AND type IN ('minio','s3','local') ORDER BY tier"
        )
        targets = []
        for r in rows:
            d = dict(r)
            # 解密 secret_key
            if d.get("secret_key"):
                d["secret_key"] = db.decrypt_secret(d["secret_key"])
            # 解析 extra_options
            if d.get("extra_options"):
                import json
                try:
                    d["extra_options"] = json.loads(d["extra_options"])
                except Exception:
                    pass
            targets.append(d)
        return targets
    except Exception as e:
        if logger:
            logger.error("[TierReplicate] 获取存储目标失败: %s", e)
        return []


def _replicate_to_target(file_path: str, target: dict, object_key_base: str,
                         logger: logging.Logger, tier_label: str = "") -> bool:
    """将文件复制到单个存储目标。"""
    try:
        from core.storage_backends import get_backend
        backend = get_backend(target["type"], target, logger)
        success = backend.save_file(file_path, object_key_base)
        if success:
            logger.info("[TierReplicate][%s] ✅ 已复制到: %s", tier_label, target.get("name"))
        else:
            logger.error("[TierReplicate][%s] ❌ 复制失败: %s", tier_label, target.get("name"))
        # 更新目标的 last_error
        now = db.now_iso()
        db.execute(
            "UPDATE storage_targets SET last_error=?, last_test_at=?, updated_at=? WHERE id=?",
            ("" if success else f"{tier_label} 上传失败", now, now, target["id"]),
        )
        return success
    except Exception as e:
        logger.error("[TierReplicate][%s] 异常: %s — %s", tier_label, target.get("name"), e)
        # 记录错误到目标
        now = db.now_iso()
        db.execute(
            "UPDATE storage_targets SET last_error=?, updated_at=? WHERE id=?",
            (str(e), now, target["id"]),
        )
        return False


def _update_record_tier(record_id: int, tier_value: str) -> None:
    """更新备份记录的存储层级字段。"""
    try:
        db.execute(
            "UPDATE backup_records SET storage_tier=? WHERE id=?",
            (tier_value, record_id),
        )
    except Exception as e:
        _logger.warning("[TierReplicate] 更新 record storage_tier 失败: %s", e)


def replicate_async(backup_path: str, task: dict, record_id: int,
                    logger: logging.Logger = None) -> None:
    """异步执行三级复制（不阻塞备份主流程）。"""
    t = threading.Thread(
        target=replicate_to_tiers,
        args=(backup_path, task, record_id, logger),
        daemon=True,
        name=f"tier-rep-{record_id}",
    )
    t.start()
    if logger:
        logger.info("[TierReplicate] 已提交后台线程异步复制 record=%d", record_id)


def get_replication_status(record_id: int) -> dict:
    """查询某条备份记录的三级复制状态。"""
    row = db.query_one(
        "SELECT id, storage_tier, backup_path, status FROM backup_records WHERE id=?",
        (record_id,),
    )
    if not row:
        return {"error": "记录不存在"}
    return {
        "record_id": record_id,
        "storage_tier": row.get("storage_tier", "local"),
        "status": row.get("status"),
        "tiers": {
            "local": "local" in (row.get("storage_tier") or ""),
            "minio": "minio" in (row.get("storage_tier") or ""),
            "s3": "s3" in (row.get("storage_tier") or ""),
        },
    }


def _get_replication_strategy(logger: logging.Logger = None) -> dict:
    """从 system_config 读取用户配置的复制策略。"""
    import json as _json
    default = {
        "push_l1_minio": 1,
        "push_l2_s3": 1,
        "push_l3_local": 1,
        "timing": "immediate",
        "max_retries": 3,
        "retry_interval": 30,
    }
    try:
        row = db.query_one(
            "SELECT value FROM system_config WHERE key=?", ("replication_strategy",)
        )
        if row and row.get("value"):
            cfg = _json.loads(row["value"])
            for k, v in default.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
    except Exception as e:
        if logger:
            logger.warning("[TierReplicate] 读取复制策略失败，使用默认: %s", e)
    return default


def _replicate_with_retry(file_path: str, target: dict, object_key_base: str,
                          logger: logging.Logger, tier_label: str = "",
                          max_retries: int = 3, retry_interval: int = 30) -> bool:
    """带重试机制的复制到目标。"""
    for attempt in range(1, max_retries + 1):
        ok = _replicate_to_target(file_path, target, object_key_base, logger, tier_label=tier_label)
        if ok:
            return True
        if attempt < max_retries:
            logger.info(
                "[TierReplicate][%s] 第 %d/%d 次失败，%d 秒后重试...",
                tier_label, attempt, max_retries, retry_interval,
            )
            time.sleep(retry_interval)
    return False
