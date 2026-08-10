# -*- coding: utf-8 -*-
"""REST API 蓝图聚合。"""
from flask import Blueprint

api_bp = Blueprint("api", __name__, url_prefix="/api")

from . import tasks, records, restore, system, hosts, sync, inspection, deploy, restore_extras_api, drills, storage, policy, lifecycle, migration, clone, itsm, link, ai_alert, datamining, ai_agent, rt, plugins  # noqa: E402,F401
