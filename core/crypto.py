# -*- coding: utf-8 -*-
"""备份通道加密模块（合规性要求）。

- 算法：AES-256-GCM（认证加密，密文+防篡改），密钥经 HKDF-SHA256 从
  通道 token 派生（token 本身不落盘明文出现在配置中的只有密文）。
- 用途：
  1) 平台 ↔ Agent 通道 payload 加密（JSON API 与文件流）；
  2) 备份产物静态加密（可选）：AES-256-GCM 整文件加密，恢复时解密。
- 依赖：cryptography（平台与 agent 同款；agent 侧缺失时自动降级为
  HMAC-SHA256 认证模式 enc=0，并在能力自报中如实标注）。
"""
import base64
import hashlib
import hmac
import os

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_AES = True
except Exception:  # pragma: no cover
    _HAS_AES = False


def has_aes() -> bool:
    return _HAS_AES


def derive_key(token: str, info: bytes = b"aidbm-agent-v1") -> bytes:
    """从通道 token 派生 256 位密钥（HKDF-SHA256）。"""
    salt = b"aidbm-agent-salt-v1"
    prk = hmac.new(salt, token.encode("utf-8"), hashlib.sha256).digest()
    okm = b""
    t = b""
    i = 1
    while len(okm) < 32:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:32]


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s)


def encrypt_bytes(key: bytes, plaintext: bytes, aad: bytes = b"") -> dict:
    """AES-256-GCM 加密。返回 {"enc": 1, "n": nonce, "ct": ciphertext}。"""
    if not _HAS_AES:
        raise RuntimeError("cryptography 未安装，无法启用 AES-256-GCM")
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad or None)
    return {"enc": 1, "n": _b64e(nonce), "ct": _b64e(ct)}


def decrypt_bytes(key: bytes, envelope: dict, aad: bytes = b"") -> bytes:
    """解密 encrypt_bytes 的信封；校验失败抛 InvalidTag（防篡改生效）。"""
    if not _HAS_AES:
        raise RuntimeError("cryptography 未安装，无法解密 AES-256-GCM")
    if envelope.get("enc") != 1:
        raise ValueError("信封格式错误：enc != 1")
    return AESGCM(key).decrypt(_b64d(envelope["n"]), _b64d(envelope["ct"]),
                               aad or None)


def encrypt_json(key: bytes, obj, aad: bytes = b"") -> dict:
    import json as _json
    return encrypt_bytes(key, _json.dumps(obj, ensure_ascii=False).encode("utf-8"), aad)


def decrypt_json(key: bytes, envelope: dict, aad: bytes = b""):
    import json as _json
    return _json.loads(decrypt_bytes(key, envelope, aad))


def hmac_sign(key: bytes, data: bytes) -> str:
    """enc=0 降级模式的完整性签名（HMAC-SHA256）。"""
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def hmac_verify(key: bytes, data: bytes, sig: str) -> bool:
    return hmac.compare_digest(hmac_sign(key, data), sig or "")


def generate_token() -> str:
    """生成通道 token（43 字符 URL 安全随机）。"""
    return _b64e(os.urandom(32)).rstrip("=")


# ---------------- 备份产物静态加密（可选，合规要求） ----------------

_MAGIC = b"AIDBMENC1"


def encrypt_file(src: str, dst: str, key: bytes) -> int:
    """整文件 AES-256-GCM 加密（8MB 分块流式，out=明文块+16B tag）。

    产物格式：MAGIC(9B) + nonce(12B) + 密文块序列。返回密文总字节数。
    """
    nonce = os.urandom(12)
    aes = AESGCM(key)
    total = 0
    with open(src, "rb") as f, open(dst, "wb") as o:
        o.write(_MAGIC)
        o.write(nonce)
        total += len(_MAGIC) + 12
        counter = 0
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            # 每块独立 nonce 前缀（nonce 高 4 字节为块序号，低 8 字节随机根）
            blk_nonce = nonce[:4] + counter.to_bytes(8, "big")
            ct = aes.encrypt(blk_nonce, chunk, None)
            o.write(ct)
            total += len(ct)
            counter += 1
    return total


def decrypt_file(src: str, dst: str, key: bytes) -> int:
    """解密 encrypt_file 产物。返回明文总字节数。校验失败抛异常。"""
    aes = AESGCM(key)
    total = 0
    with open(src, "rb") as f, open(dst, "wb") as o:
        magic = f.read(len(_MAGIC))
        if magic != _MAGIC:
            raise ValueError("产物不是 AIDBM 加密格式（MAGIC 不符）")
        nonce = f.read(12)
        counter = 0
        while True:
            chunk = f.read(8 * 1024 * 1024 + 16)
            if not chunk:
                break
            blk_nonce = nonce[:4] + counter.to_bytes(8, "big")
            o.write(aes.decrypt(blk_nonce, chunk, None))
            counter += 1
            total += len(chunk)
    return total


def is_encrypted_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(_MAGIC)) == _MAGIC
    except OSError:
        return False
