# -*- coding: utf-8 -*-
"""
ITSM 适配层：工单创建 / 审批 / 回调，支持多后端可插拔。

设计目标
--------
- 抽象基类 `ITSMAdapter` 统一对外接口：`create_ticket / query_status /
  approve_ticket / reject_ticket / callback`。
- `InternalITSMAdapter`：内置审批（默认后端，无需外部系统即可跑通）。
  工单创建后进入 open，由平台内 `approve_ticket/reject_ticket` 驱动；
  可通过 system_config.itsm_auto_approve 配置为 auto-approve 以便演示。
- `DingTalkITSMAdapter` / `ServiceNowITSMAdapter`：可插拔 stub。
  实现上述接口骨架：未配置真实凭证时降级为 internal（仅建本地工单 + 记录日志），
  绝不抛出未捕获异常；DEMO 模式下直接仿真外部审批（工单设为 approved）。
- 工厂 `get_itsm_adapter(system)` 按 system 返回对应适配器；默认 internal。

所有外部依赖（钉钉 / ServiceNow）均支持 DEMO_MODE 仿真兜底，保证无真实环境也能跑通自测。
"""
import json
import logging
from typing import Optional

import config
import core.models as models
import core.db as db


_logger = db.get_logger("itsm")

VALID_SYSTEMS = ("internal", "dingtalk", "servicenow")
VALID_REF_TYPES = ("clone", "migration", "drill")
VALID_TICKET_STATUS = ("open", "approved", "rejected", "closed")


def _auto_approve_enabled() -> bool:
    try:
        return bool(int(db.get_system_config("itsm_auto_approve") or 0))
    except Exception:
        return False


def _default_system() -> str:
    raw = db.get_system_config("itsm_system")
    if raw in VALID_SYSTEMS:
        return raw
    return "internal"


class ITSMAdapter:
    """ITSM 适配层抽象基类（统一契约）。

    子类需实现 create_ticket / query_status / approve_ticket / reject_ticket /
    callback。所有方法应捕获内部异常，避免向调用方抛出未捕获异常。
    """

    system = "internal"

    def create_ticket(self, ref_type: str, ref_id: Optional[int],
                      payload: dict = None) -> dict:
        raise NotImplementedError

    def query_status(self, ticket_id: int) -> str:
        raise NotImplementedError

    def approve_ticket(self, ticket_id: int, by: str = "admin") -> bool:
        raise NotImplementedError

    def reject_ticket(self, ticket_id: int, by: str = "admin") -> bool:
        raise NotImplementedError

    def callback(self, ticket_no: str, status: str,
                 payload: dict = None) -> None:
        """外部系统推送审批结果（预留入口，由外部 Webhook 调用）。"""
        raise NotImplementedError


class InternalITSMAdapter(ITSMAdapter):
    """内置审批适配器（默认后端，无需任何外部系统）。"""

    system = "internal"

    def create_ticket(self, ref_type: str, ref_id: Optional[int],
                      payload: dict = None) -> dict:
        if ref_type not in VALID_REF_TYPES:
            ref_type = "clone"
        status = "approved" if _auto_approve_enabled() else "open"
        tid = models.create_itsm_ticket({
            "system": "internal",
            "ref_type": ref_type,
            "ref_id": ref_id,
            "status": status,
            "payload": json.dumps(payload or {}, ensure_ascii=False),
        })
        ticket = models.get_itsm_ticket(tid)
        if status == "approved":
            _logger.info("[itsm] 内置工单 #%s 已自动审批（itsm_auto_approve）", tid)
        else:
            _logger.info("[itsm] 内置工单 #%s 已创建（ref=%s/%s）", tid, ref_type, ref_id)
        return ticket

    def query_status(self, ticket_id: int) -> str:
        t = models.get_itsm_ticket(ticket_id)
        return t["status"] if t else "open"

    def approve_ticket(self, ticket_id: int, by: str = "admin") -> bool:
        t = models.get_itsm_ticket(ticket_id)
        if not t:
            return False
        payload = {}
        try:
            payload = json.loads(t.get("payload") or "{}")
        except Exception:
            pass
        payload["approved_by"] = by
        models.update_itsm_ticket(ticket_id, {
            "status": "approved",
            "payload": json.dumps(payload, ensure_ascii=False),
        })
        _logger.info("[itsm] 工单 #%s 已审批（by=%s）", ticket_id, by)
        return True

    def reject_ticket(self, ticket_id: int, by: str = "admin") -> bool:
        t = models.get_itsm_ticket(ticket_id)
        if not t:
            return False
        payload = {}
        try:
            payload = json.loads(t.get("payload") or "{}")
        except Exception:
            pass
        payload["rejected_by"] = by
        models.update_itsm_ticket(ticket_id, {
            "status": "rejected",
            "payload": json.dumps(payload, ensure_ascii=False),
        })
        _logger.info("[itsm] 工单 #%s 已驳回（by=%s）", ticket_id, by)
        return True

    def callback(self, ticket_no: str, status: str,
                 payload: dict = None) -> None:
        """内部系统一般无需外部回调；此处按 ticket_no 更新对应工单状态。"""
        rows = db.query("SELECT * FROM itsm_tickets WHERE ticket_no=? ORDER BY id DESC",
                        (ticket_no,))
        if not rows:
            _logger.warning("[itsm] callback ticket_no=%s 未找到对应工单", ticket_no)
            return
        t = rows[0]
        if status in VALID_TICKET_STATUS:
            models.update_itsm_ticket(t["id"], {
                "status": status,
                "payload": json.dumps(payload or {}, ensure_ascii=False),
            })


class _ExternalITSMStub(ITSMAdapter):
    """外部系统（钉钉 / ServiceNow）适配层骨架。

    未配置真实凭证时降级为 internal（仅建本地工单 + 记录日志），绝不抛异常；
    DEMO 模式下直接仿真外部审批（工单设为 approved）。真实环境下应在此实现
    create_ticket 调用对方 OpenAPI、callback 校验签名等，并保留接口骨架。
    """

    system = "internal"
    display_name = "外部系统"

    def __init__(self, system: str):
        self.system = system

    def _configured(self) -> bool:
        """子类可重写：返回是否已配置真实凭证。缺省视为未配置。"""
        return False

    def create_ticket(self, ref_type: str, ref_id: Optional[int],
                      payload: dict = None) -> dict:
        if ref_type not in VALID_REF_TYPES:
            ref_type = "clone"
        # 未配置真实凭证 → 降级为本地工单；DEMO 模式直接仿真审批
        if config.DEMO_MODE != "off" or not self._configured():
            auto = config.DEMO_MODE != "off" or _auto_approve_enabled()
            status = "approved" if auto else "open"
            tid = models.create_itsm_ticket({
                "system": self.system,
                "ref_type": ref_type,
                "ref_id": ref_id,
                "status": status,
                "payload": json.dumps(payload or {}, ensure_ascii=False),
            })
            ticket = models.get_itsm_ticket(tid)
            _logger.info("[itsm] %s 工单 #%s 已创建（降级/DEMO，status=%s）",
                         self.display_name, tid, status)
            return ticket
        # 真实环境下：在此调用对方 OpenAPI 创建工单并写入 ticket_no，此处仅占位
        _logger.warning("[itsm] %s 未实现真实创建，降级为本地工单", self.display_name)
        tid = models.create_itsm_ticket({
            "system": self.system,
            "ref_type": ref_type,
            "ref_id": ref_id,
            "status": "open",
            "payload": json.dumps(payload or {}, ensure_ascii=False),
        })
        return models.get_itsm_ticket(tid)

    def query_status(self, ticket_id: int) -> str:
        t = models.get_itsm_ticket(ticket_id)
        return t["status"] if t else "open"

    def approve_ticket(self, ticket_id: int, by: str = "admin") -> bool:
        # 外部系统：真实环境应由对方回调驱动；此处兼容平台内审批入口
        return InternalITSMAdapter().approve_ticket(ticket_id, by)

    def reject_ticket(self, ticket_id: int, by: str = "admin") -> bool:
        return InternalITSMAdapter().reject_ticket(ticket_id, by)

    def callback(self, ticket_no: str, status: str,
                 payload: dict = None) -> None:
        """外部系统推送审批结果入口（Webhook）。"""
        rows = db.query("SELECT * FROM itsm_tickets WHERE ticket_no=? ORDER BY id DESC",
                        (ticket_no,))
        if not rows:
            _logger.warning("[itsm] %s callback ticket_no=%s 未找到对应工单",
                            self.display_name, ticket_no)
            return
        if status in VALID_TICKET_STATUS:
            models.update_itsm_ticket(rows[0]["id"], {
                "status": status,
                "payload": json.dumps(payload or {}, ensure_ascii=False),
            })
            _logger.info("[itsm] %s callback 工单 #%s -> %s",
                         self.display_name, rows[0]["id"], status)


class DingTalkITSMAdapter(_ExternalITSMStub):
    system = "dingtalk"
    display_name = "钉钉审批"

    def __init__(self):
        super().__init__(self.system)

    def _configured(self) -> bool:
        raw = db.get_system_config("dingtalk_webhook")
        return bool(raw)


class ServiceNowITSMAdapter(_ExternalITSMStub):
    system = "servicenow"
    display_name = "ServiceNow"

    def __init__(self):
        super().__init__(self.system)

    def _configured(self) -> bool:
        raw = db.get_system_config("servicenow_endpoint")
        return bool(raw)


_ADAPTERS = {
    "internal": InternalITSMAdapter,
    "dingtalk": DingTalkITSMAdapter,
    "servicenow": ServiceNowITSMAdapter,
}


def get_itsm_adapter(system: str = None) -> ITSMAdapter:
    """按 system 返回对应 ITSM 适配器；缺省取 system_config.itsm_system（internal）。"""
    system = system or _default_system()
    if system not in _ADAPTERS:
        system = "internal"
    return _ADAPTERS[system]()
