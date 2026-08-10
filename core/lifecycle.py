# -*- coding: utf-8 -*-
"""
冷热分级生命周期引擎（LifecycleEngine）。

按策略把备份集在 L1（MinIO 热数据）→ L2（S3 冷数据）间
流转 / 降级 / 到期清理（L3 源端本地路径为复制时的终态导出，不参与生命周期流转）。

策略来源：system_config.lifecycle_config（JSON），缺省使用 DEFAULT_LIFECYCLE_CONFIG。
流转语义：复用 tier_replication 的底层跨存储拷贝能力（_replicate_to_target）
完成物理拷贝，但触发条件是"生命周期驱动"（按龄 / 按量），而非备份成功后的
"复制"。因此与既有三级复制配置/行为互不干扰。
"""
import os
import time
import json
import logging
from datetime import datetime, timezone

import core.db as db
import core.models as models


_logger = db.get_logger("lifecycle")

DEFAULT_LIFECYCLE_CONFIG = {
    "enabled": True,
    "l1_to_l2_days": 7,            # L1 本地 -> L2 热数据（按龄阈值，天）
    "l2_to_l3_days": 30,           # L2 热数据 -> L3 冷数据（按龄阈值，天）
    "capacity_threshold_pct": 85,  # L1 用量超此比例触发下沉（容量阈值）
    "retention_days": 90,          # 超过此龄期视为到期，可清理
    "enable_expiry": True,
}

_CONFIG_KEY = "lifecycle_config"


class LifecycleEngine:
    """冷热分级生命周期引擎。"""

    def __init__(self, logger: logging.Logger = None):
        self.logger = logger or _logger

    # ------------------------- 配置 -------------------------
    def get_config(self) -> dict:
        raw = db.get_system_config(_CONFIG_KEY)
        cfg = dict(DEFAULT_LIFECYCLE_CONFIG)
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except (json.JSONDecodeError, TypeError):
                pass
        return cfg

    def save_config(self, data: dict) -> dict:
        cfg = self.get_config()
        allowed = {"enabled", "l1_to_l2_days", "l2_to_l3_days",
                   "capacity_threshold_pct", "retention_days", "enable_expiry"}
        for k, v in (data or {}).items():
            if k in allowed:
                cfg[k] = v
        # 类型修正，避免脏数据
        for k in ("l1_to_l2_days", "l2_to_l3_days", "retention_days"):
            try:
                cfg[k] = int(cfg[k])
            except (TypeError, ValueError):
                cfg[k] = DEFAULT_LIFECYCLE_CONFIG[k]
        try:
            cfg["capacity_threshold_pct"] = float(cfg["capacity_threshold_pct"])
        except (TypeError, ValueError):
            cfg["capacity_threshold_pct"] = DEFAULT_LIFECYCLE_CONFIG["capacity_threshold_pct"]
        cfg["enabled"] = bool(cfg["enabled"])
        cfg["enable_expiry"] = bool(cfg["enable_expiry"])
        db.set_system_config(_CONFIG_KEY, json.dumps(cfg, ensure_ascii=False))
        return cfg

    # ------------------------- 状态概览 -------------------------
    def get_status(self) -> dict:
        rows = models.list_backup_sets()
        tiers = {1: {"count": 0, "bytes": 0},
                 2: {"count": 0, "bytes": 0},
                 3: {"count": 0, "bytes": 0}}
        set_types = {}
        for r in rows:
            t = int(r.get("storage_tier") or 1)
            if t not in tiers:
                tiers[t] = {"count": 0, "bytes": 0}
            tiers[t]["count"] += 1
            tiers[t]["bytes"] += int(r.get("size_bytes") or 0)
            st = r.get("set_type") or "full"
            set_types[st] = set_types.get(st, 0) + 1
        return {
            "tiers": {
                str(k): {"count": v["count"], "bytes": v["bytes"],
                         "human": db.human_size(v["bytes"])}
                for k, v in tiers.items()
            },
            "set_types": set_types,
            "total_sets": len(rows),
            "l1_usage": self._l1_usage(),
            "config": self.get_config(),
        }

    def _l1_usage(self) -> dict:
        try:
            import shutil
            import config as _cfg
            row = db.query_one(
                "SELECT type, endpoint, extra_options FROM storage_targets "
                "WHERE tier=1 AND enabled=1 ORDER BY is_default DESC, id LIMIT 1")
            # 远程（MinIO/S3）热层：优先从 extra_options.used_pct 读取容量
            if row and row.get("extra_options"):
                try:
                    ex = json.loads(row["extra_options"])
                    if ex.get("used_pct") is not None:
                        return {"used_percent": float(ex["used_pct"]),
                                "path": row.get("endpoint")}
                except Exception:
                    pass
            # 回退：测量本地备份根目录所在磁盘用量（服务端磁盘压力兜底）
            path = os.path.abspath(getattr(_cfg, "BACKUP_ROOT", "./backups"))
            du = shutil.disk_usage(path)
            return {
                "path": path,
                "total_bytes": du.total,
                "used_bytes": du.used,
                "free_bytes": du.free,
                "used_percent": round(du.used / du.total * 100, 1),
            }
        except Exception as e:
            return {"error": str(e)}

    # ------------------------- 单次运行 -------------------------
    def run_once(self) -> dict:
        cfg = self.get_config()
        if not cfg.get("enabled"):
            self.logger.info("[lifecycle] 已禁用，跳过本次运行")
            return {"skipped": True, "reason": "disabled"}
        sets = models.list_backup_sets()
        moved = 0
        expired = 0
        errors = 0
        capacity_forced = 0

        # 容量触发：L1 用量超阈值时，强制下沉最旧的 L1 集
        l1_over = False
        try:
            usage = self._l1_usage()
            if usage and not usage.get("error"):
                l1_over = float(usage.get("used_percent", 0)) >= float(
                    cfg.get("capacity_threshold_pct", 85))
        except Exception:
            l1_over = False

        for s in sets:
            try:
                age_days = self._age_days(s.get("created_at"))
                cur_tier = int(s.get("storage_tier") or 1)
                target_tier = cur_tier
                # 按龄流转：MinIO(1) -> S3(2)；本地导出(3) 是复制时的终态拷贝，不参与流转
                if cur_tier < 2 and age_days >= int(cfg.get("l1_to_l2_days", 7)):
                    target_tier = 2
                # 容量触发：L1 超额时，当前在 L1 的集强制下沉到 L2(S3)
                if l1_over and cur_tier == 1:
                    target_tier = max(target_tier, 2)
                    capacity_forced += 1
                if target_tier > cur_tier:
                    if self._demote(s, cur_tier, target_tier):
                        moved += 1
                    else:
                        errors += 1
                        continue
                # 到期清理
                if bool(cfg.get("enable_expiry")) and age_days >= int(cfg.get("retention_days", 90)):
                    if self._expire(s):
                        expired += 1
            except Exception as e:
                self.logger.warning("[lifecycle] 处理 set#%s 异常: %s", s.get("id"), e)
                errors += 1

        summary = {
            "moved": moved, "expired": expired, "errors": errors,
            "capacity_forced": capacity_forced, "l1_over_capacity": l1_over,
            "total_sets": len(sets), "run_at": db.now_iso(),
        }
        self.logger.info("[lifecycle] 运行完成: %s", summary)
        return summary

    # ------------------------- 内部工具 -------------------------
    def _age_days(self, created_at) -> float:
        if not created_at:
            return 0.0
        try:
            dt = datetime.fromisoformat(created_at)
        except Exception:
            try:
                dt = datetime.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                return 0.0
        now = datetime.now(timezone.utc).astimezone()
        return max(0.0, (now - dt).total_seconds() / 86400.0)

    def _demote(self, bset: dict, cur_tier: int, target_tier: int) -> bool:
        """将备份集从 cur_tier 流转到 target_tier（跨存储拷贝）。"""
        from core import tier_replication
        object_key = bset.get("object_key")
        if not object_key:
            self.logger.warning("[lifecycle] set#%s 无 object_key，跳过流转", bset.get("id"))
            return False
        # 找到目标层级的启用存储目标
        targets = tier_replication._get_enabled_targets(self.logger)
        dest = next((t for t in targets if int(t.get("tier")) == target_tier), None)
        if not dest:
            self.logger.info("[lifecycle] 无 tier=%s 存储目标，set#%s 暂不流转",
                             target_tier, bset.get("id"))
            return False
        # 物化源对象到本地临时文件（跨存储拷贝前需先有本地文件）
        src_path = self._materialize(bset, cur_tier)
        if not src_path:
            return False
        ok = tier_replication._replicate_to_target(
            src_path, dest, object_key, self.logger,
            tier_label=f"L{cur_tier}->L{target_tier}")
        if ok:
            models.update_backup_set(bset["id"], {"storage_tier": target_tier})
            self.logger.info("[lifecycle] set#%s L%s->L%s 完成",
                             bset.get("id"), cur_tier, target_tier)
        # 清理临时文件（仅当是临时拉取、非原始 L1 文件时）
        if src_path and src_path != object_key and os.path.exists(src_path):
            try:
                os.remove(src_path)
            except OSError:
                pass
        return ok

    def _materialize(self, bset: dict, cur_tier: int):
        """把源对象取到本地临时文件路径（供跨存储拷贝）。L1 直接是本地文件。"""
        object_key = bset.get("object_key")
        if cur_tier == 1 and object_key and os.path.isfile(object_key):
            return object_key
        from core import tier_replication
        from core.storage_backends import get_backend
        targets = tier_replication._get_enabled_targets(self.logger)
        src = next((t for t in targets if int(t.get("tier")) == cur_tier), None)
        if not src:
            return None
        try:
            backend = get_backend(src["type"], src, self.logger)
            tmp = os.path.join(
                db.LOG_DIR,
                f"_lc_pull_{bset.get('id')}_{int(time.time() * 1000)}.tmp")
            data = backend.get_file(object_key, dest_path=tmp)
            if data is None and not os.path.exists(tmp):
                return None
            return tmp
        except Exception as e:
            self.logger.warning("[lifecycle] 物化 set#%s 失败: %s", bset.get("id"), e)
            return None

    def _expire(self, bset: dict) -> bool:
        """到期清理：删除各层级对象与备份集元数据。"""
        object_key = bset.get("object_key")
        from core import tier_replication
        from core.storage_backends import get_backend
        targets = tier_replication._get_enabled_targets(self.logger)
        for t in targets:
            try:
                backend = get_backend(t["type"], t, self.logger)
                if object_key and backend.file_exists(object_key):
                    backend.delete_file(object_key)
            except Exception as e:
                self.logger.warning("[lifecycle] 删除 L%s 对象失败: %s",
                                     t.get("tier"), e)
        models.delete_backup_set(bset["id"])
        self.logger.info("[lifecycle] set#%s 已到期清理", bset.get("id"))
        return True
