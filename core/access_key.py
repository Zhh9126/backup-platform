# -*- coding: utf-8 -*-
"""AccessKey 免密认证（P2，对标鼎甲 E7）。

场景：数据库密码周期轮换环境，外部系统（CI/巡检平台）不适合持有静态
密码/静态 Token——用 AK/SK 对做 HMAC-SHA256 请求签名（同阿里云 AK 语义），
SK 不上网、可随时吊销轮换。

签名协议（简洁优先，离线零依赖）：
    X-AK-Access-Key: <access_key>
    X-AK-Timestamp:  <unix 秒>
    X-AK-Signature:  hex(HMAC_SHA256(secret_key, f"{METHOD}\n{path}\n{timestamp}"))
时间窗 ±300 秒（防重放）。路径用 request.path（不含 query）。
"""
import hashlib
import hmac
import secrets
import time
from datetime import datetime
from typing import Optional

from core import db

_AK_TABLE = """
CREATE TABLE IF NOT EXISTS access_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    access_key  TEXT NOT NULL UNIQUE,
    secret_enc  TEXT NOT NULL,          -- SK 加密存储（验签需原文）
    enabled     INTEGER DEFAULT 1,
    revoked     INTEGER DEFAULT 0,
    created_by  TEXT,
    created_at  TEXT,
    last_used_at TEXT
)
"""


def _ensure_table() -> None:
    try:
        db.query("SELECT 1 FROM access_keys LIMIT 1")
    except Exception:  # noqa: BLE001
        db.execute(_AK_TABLE)


def _sign(secret: str, method: str, path: str, ts: str) -> str:
    msg = f"{method.upper()}\n{path}\n{ts}"
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def create_key(name: str, created_by: str = "") -> dict:
    """生成 AK/SK 对。SK 明文只在返回值出现一次（库里存密文）。"""
    _ensure_table()
    ak = "AK" + secrets.token_hex(8)
    sk = secrets.token_hex(24)
    db.execute(
        "INSERT INTO access_keys (name, access_key, secret_enc, created_by, created_at)"
        " VALUES (?,?,?,?,?)",
        (name or "unnamed", ak, db.encrypt_secret(sk), created_by, db.now_iso()))
    return {"name": name, "access_key": ak, "secret_key": sk}


def list_keys() -> list:
    _ensure_table()
    return db.query(
        "SELECT id, name, access_key, enabled, revoked, created_by, created_at,"
        " last_used_at FROM access_keys ORDER BY id DESC")


def revoke_key(key_id: int) -> bool:
    _ensure_table()
    db.execute("UPDATE access_keys SET revoked=1 WHERE id=?", (int(key_id),))
    return True


def _get_secret(ak: str) -> Optional[str]:
    row = db.query_one(
        "SELECT secret_enc, revoked, enabled FROM access_keys WHERE access_key=?", (ak,))
    if not row or row["revoked"] or not row["enabled"]:
        return None
    try:
        return db.decrypt_secret(row["secret_enc"] or "")
    except Exception:  # noqa: BLE001
        return None


def verify_signature(ak: str, ts: str, sign: str, method: str, path: str,
                     window: int = 300) -> tuple:
    """验签。返回 (ok, err)。时间戳超窗/签名不匹配/已吊销均如实拒绝。"""
    _ensure_table()
    if not ak or not ts or not sign:
        return False, "缺少 AccessKey 签名头（X-AK-Access-Key/X-AK-Timestamp/X-AK-Signature）"
    try:
        ts_i = int(str(ts))
    except (TypeError, ValueError):
        return False, "时间戳非法"
    if abs(time.time() - ts_i) > window:
        return False, f"签名时间戳超出 ±{window} 秒窗口（防重放）"
    secret = _get_secret(ak)
    if secret is None:
        return False, "AccessKey 无效或已吊销"
    want = _sign(secret, method, path, str(ts))
    if not hmac.compare_digest(want, str(sign).lower()):
        return False, "签名不匹配"
    try:
        db.execute("UPDATE access_keys SET last_used_at=? WHERE access_key=?",
                   (db.now_iso(), ak))
    except Exception:  # noqa: BLE001
        pass
    return True, ""
