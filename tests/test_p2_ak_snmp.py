# -*- coding: utf-8 -*-
"""AccessKey 免密认证 + SNMP Trap 自测（P2，2026-11）。

覆盖（全部真实 HTTP/UDP 链路，非 mock）：
- AK：创建→SK 一次展示→真实 HMAC 签名调用受保护 API 200→错签 401→
  时间戳超窗 401→吊销后 401
- SNMP：BER 编码→本机 UDP 端口真实收包→手工解析验证 community/trap OID/
  消息文本；配置挂接 emit 事件链路
"""
import hashlib
import hmac
import json
import os
import socket
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="ak_snmp_")
_MODULE_DB = os.path.join(_TMP, "instance", "meta.db")
os.makedirs(os.path.dirname(_MODULE_DB), exist_ok=True)
os.environ["META_DB_PATH"] = _MODULE_DB
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["LOGIN_LOCK_ENABLED"] = "0"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config                                   # noqa: E402
import core.db as db                            # noqa: E402
config.META_DB_PATH = _MODULE_DB
config.BACKUP_ROOT = os.environ["BACKUP_ROOT"]
db.init_schema()                                # noqa: E402

import core.access_key as access_key            # noqa: E402
import core.snmp_trap as snmp_trap              # noqa: E402


def _client():
    from app import create_app
    return create_app().test_client()


def _login(c):
    r = c.post("/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]


# ---------------------------------------------------------------------------
# AccessKey
# ---------------------------------------------------------------------------
def test_ak_lifecycle_and_real_signature():
    c = _client()
    _login(c)
    # 创建（SK 一次展示）
    r = c.post("/api/access-keys", json={"name": "CI 平台"})
    assert r.status_code == 200, r.get_json()
    ak_info = r.get_json()["data"]
    ak, sk = ak_info["access_key"], ak_info["secret_key"]
    assert ak.startswith("AK") and len(sk) >= 32
    # 列表不泄露 SK
    r = c.get("/api/access-keys")
    assert sk not in json.dumps(r.get_json())
    # 真实 HMAC 签名调用受保护 API（session 清空后仅凭签名）
    c2 = _client().application.test_client()
    ts = str(int(time.time()))
    msg = f"GET\n/api/meta\n{ts}"
    sign = hmac.new(sk.encode(), msg.encode(), hashlib.sha256).hexdigest()
    c3 = _client()
    r = c3.get("/api/meta", headers={
        "X-AK-Access-Key": ak, "X-AK-Timestamp": ts, "X-AK-Signature": sign})
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:150])
    # 错签 → 401
    r = c3.get("/api/meta", headers={
        "X-AK-Access-Key": ak, "X-AK-Timestamp": ts,
        "X-AK-Signature": "0" * 64})
    assert r.status_code == 401
    # 时间戳超窗 → 401（防重放）
    old_ts = str(int(time.time()) - 3600)
    sign_old = hmac.new(sk.encode(), f"GET\n/api/meta\n{old_ts}".encode(),
                        hashlib.sha256).hexdigest()
    r = c3.get("/api/meta", headers={
        "X-AK-Access-Key": ak, "X-AK-Timestamp": old_ts, "X-AK-Signature": sign_old})
    assert r.status_code == 401
    # 吊销后 → 401
    rows = [k for k in access_key.list_keys() if k["access_key"] == ak]
    assert rows
    c.post(f"/api/access-keys/{rows[0]['id']}/revoke")
    r = c3.get("/api/meta", headers={
        "X-AK-Access-Key": ak, "X-AK-Timestamp": ts, "X-AK-Signature": sign})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# SNMP：真实 UDP 收包 + BER 解析
# ---------------------------------------------------------------------------
def _udp_recv_once(port, holder, timeout=5.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.settimeout(timeout)
    try:
        data, addr = s.recvfrom(65535)
        holder.append(data)
    except socket.timeout:
        pass
    finally:
        s.close()


def _walk_tlv(buf, i=0):
    """生成 (tag, content) 迭代（本测试专用简易 BER 解析）。"""
    while i < len(buf):
        tag = buf[i]
        i += 1
        ln = buf[i]
        i += 1
        if ln & 0x80:
            n = ln & 0x7F
            ln = int.from_bytes(buf[i:i + n], "big")
            i += n
        yield tag, buf[i:i + ln]
        i += ln


def test_snmp_trap_real_udp_roundtrip():
    port = 30162
    holder = []
    t = threading.Thread(target=_udp_recv_once, args=(port, holder), daemon=True)
    t.start()
    time.sleep(0.3)
    r = snmp_trap.send_trap("127.0.0.1", port, "aidbm_test", "备份任务 t1 失败，请检查",
                            "error", "1.3.6.1.4.1.60000")
    t.join(timeout=6)
    assert r["sent"] is True, r
    assert holder, "UDP 端口未收到 trap 报文"
    pkt = holder[0]
    # 顶层：SEQUENCE(version, community, trapPDU)
    top = list(_walk_tlv(pkt))
    assert top[0][0] == 0x30
    inner = list(_walk_tlv(top[0][1]))
    assert inner[0][0] == 0x02 and inner[0][1] == b"\x01"     # version 1 (v2c)
    assert inner[1][1] == b"aidbm_test"                       # community
    pdu = list(_walk_tlv(inner[2][1]))                        # TRAP-V2 PDU
    varbinds_raw = pdu[3][1]
    vbs = list(_walk_tlv(varbinds_raw))
    texts = []
    oids = []
    for tag, content in vbs:
        assert tag == 0x30
        pair = list(_walk_tlv(content))
        oid_parts = pair[0][1]
        # 解码 OID 主干验证 snmpTrapOID / 消息
        oids.append(oid_parts.hex())
        if pair[1][0] == 0x04:
            texts.append(pair[1][1].decode("utf-8", "replace"))
    assert any("备份任务 t1 失败" in x for x in texts), texts
    # sysUpTime.0 = 1.3.6.1.2.1.1.3.0 → BER 首字节 0x2b 后接 06 01 02 01 01 03 00
    assert any("0601020101" in x for x in oids), oids


def test_snmp_emit_hook_and_config():
    # 配置挂接：启用 + 指向无监听端口 → fire-and-forget 不抛异常
    snmp_trap.set_snmp_config(True, "127.0.0.1", 30163, "public")
    r = snmp_trap.notify_event("[backup.failed] 测试事件", "warning")
    assert "sent" in r   # UDP 无监听也照发（fire-and-forget）
    # 未启用 → 跳过
    snmp_trap.set_snmp_config(False, "127.0.0.1", 30163, "public")
    r = snmp_trap.notify_event("[x] y", "warning")
    assert r["sent"] is False and "未启用" in r["detail"]
    # API 配置读写 + 测试 trap 端点
    c = _client()
    _login(c)
    r = c.post("/api/snmp/config", json={
        "enabled": True, "target": "127.0.0.1", "port": 30164,
        "community": "pub_test"})
    assert r.status_code == 200
    assert snmp_trap.snmp_config()["community"] == "pub_test"
    r = c.post("/api/snmp/test", json={"target": "127.0.0.1", "port": 30164})
    assert r.status_code == 200 and r.get_json()["success"] is True
    snmp_trap.set_snmp_config(False, "", 162, "public")
