# -*- coding: utf-8 -*-
"""华为 GaussDB（PG/openGauss 血缘商用发行版）备份/恢复引擎。

GaussDB 的服务端由 openGauss 演进，客户端工具链为 ``gs_dump / gs_restore /
gsql / gs_basebackup``（与 openGauss 同名同用法，需 -W 传密码、库名为位置参数）。
因此直接继承 OpenGaussEngine，仅差异化 db_type / 展示名 / 默认端口。

诚实边界：协议兼容层接入，未在华为 GaussDB 生产实例上真机验证；首次使用前
请先跑「预检 + 小库备份 + 恢复校验」，确认 gs_* 工具版本与实例匹配。
"""
from .opengauss import OpenGaussEngine


class GaussDBEngine(OpenGaussEngine):
    """GaussDB 备份/恢复引擎（继承 openGauss 引擎，gs_* 工具族）。"""

    db_type = "gaussdb"
    display_name = "GaussDB（华为）"
    default_port = 8000
