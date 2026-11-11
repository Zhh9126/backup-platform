# -*- coding: utf-8 -*-
"""回归：网络设备创建触发配额超限时必须返回 403，不能因异常类引用错误变 500。

历史上 api/netdev.py 写成 ``except netdev.license_mod.QuotaExceededError``，
依赖 core.netdev 的私有导入，异常发生时反而抛 AttributeError -> 500。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_quota_exception_is_caught_as_403(monkeypatch):
    from app import create_app
    from core.license import QuotaExceededError
    import api.netdev as netdev_api

    def raise_quota(_data):
        raise QuotaExceededError("网络设备配额已达上限")

    monkeypatch.setattr(netdev_api.netdev, "create_device", raise_quota)
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        with c.session_transaction() as s:
            s["user"] = {"id": 1, "username": "admin",
                         "display_name": "admin", "role": "admin"}
        r = c.post("/api/netdev/devices", json={"name": "quota-e2e"})
        assert r.status_code == 403, r.get_data(as_text=True)[:200]
        body = r.get_json()
        assert body.get("quota_exceeded") is True
        assert "配额" in str(body.get("error"))


def test_netdev_module_exposes_quota_exception_directly():
    """api.netdev 必须直接持有异常类，不再依赖模块属性链。"""
    import api.netdev as netdev_api
    assert netdev_api.QuotaExceededError.__name__ == "QuotaExceededError"
