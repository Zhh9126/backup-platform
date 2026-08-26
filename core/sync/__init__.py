# -*- coding: utf-8 -*-
"""
数据同步引擎（DataX/LinkUp 风格）。

离线批量同步：Source Reader → 统一 Java 类型 → Sink Writer。
实时同步：预留 Flink CDC 集成入口，本平台负责配置下发与状态监控。
"""
from .engine import SyncEngine, run_sync_task
from .plugins import registry

__all__ = ["SyncEngine", "run_sync_task", "registry"]
