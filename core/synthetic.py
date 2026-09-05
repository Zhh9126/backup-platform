# -*- coding: utf-8 -*-
"""【已废弃】合成备份统一使用 core/synthesize.py（调度）+
core/engines/base.synthesize_full_for_task（引擎合成链）。

本文件曾为重复实现，保留 shim 仅作转发兼容，勿在此新增逻辑。
"""
from core.synthesize import run_auto_synthesis  # noqa: F401
