# -*- coding: utf-8 -*-
"""群晖 DSM 适配包：协议客户端 / 设备纳管 / 备份引擎（引擎在 core/engines 下）。"""
from .client import DSMClient, DSMError
from . import devices

__all__ = ["DSMClient", "DSMError", "devices"]
