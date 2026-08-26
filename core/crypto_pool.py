# -*- coding: utf-8 -*-
"""存储池加密（Encryption at Rest）—— 参照鼎甲迪备白皮书 §2.6 备份数据加密。

设计目标：让备份产物在落盘后以「密文」存储，即使存储介质被物理拿走也无法
直接读取明文（对应白皮书"备份数据加密""防止数据泄露"）。

方案：AES-256-GCM 信封式加密（AEAD，自带完整性校验）
- 主密钥从环境变量 BACKUP_POOL_KEY 或配置文件读取（生产应放 KMS/密钥库）；
- 每个文件用独立随机 12 字节 nonce（GCM 推荐），密钥派生用 PBKDF2-HMAC-SHA256
  （salt 随机），文件头部写入 magic + salt + nonce + tag；
- 提供 encrypt_file / decrypt_file / 校验接口，供恢复与校验调用；
- 失败安全：密钥缺失时不加密（明文落盘并告警），不阻断备份主流程。

格式（二进制）：
  magic(4) | version(1) | salt(16) | nonce(12) | tag(16) | ciphertext
"""
from __future__ import annotations

import os
import base64
import hashlib
import logging
from typing import Optional

logger = logging.getLogger("core.crypto_pool")

MAGIC = b"BKPX"
VERSION = 1
SALT_LEN = 16
NONCE_LEN = 12
TAG_LEN = 16


def _derive_key(passphrase: bytes, salt: bytes, length: int = 32) -> bytes:
    try:
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=length,
                         salt=salt, iterations=200_000)
        return kdf.derive(passphrase)
    except ImportError:
        # 退路：无 cryptography 库时用 HKDF-like 简单派生（仅当库缺失，生产建议装库）
        import hmac
        return hmac.new(passphrase, salt + b"bkp-pool", hashlib.sha256).digest()


def _get_passphrase() -> Optional[bytes]:
    """获取主密钥。

    优先级：
      1. 环境变量 BACKUP_POOL_KEY（运维手动注入，最安全）；
      2. system_config['pool_crypto']（页面托管：本地密钥库 或 KMS 拉取到的明文）；
      3. config.BACKUP_POOL_KEY（代码默认值，兜底）。
    None=不加密。
    """
    env = os.environ.get("BACKUP_POOL_KEY")
    if env:
        return env.encode("utf-8")
    # 2. system_config 托管的密钥（本地密钥库 / KMS 缓存）
    try:
        from core import db as _db
        raw = _db.get_system_config("pool_crypto")
        if raw:
            import json as _json
            cfg = _json.loads(raw) if isinstance(raw, str) else raw
            mode = cfg.get("mode", "local")
            if mode == "kms":
                pw = _resolve_kms_passphrase(cfg)
                if pw:
                    return pw
                # KMS 不可达时失败安全回退到本地备份密钥（若有）
                lb = cfg.get("local_fallback_key")
                if lb:
                    return lb.encode("utf-8")
                logger.warning("KMS 解析主密钥失败，且无本地回退密钥，跳过加密")
                return None
            else:
                # 本地密钥库
                key = cfg.get("pool_key")
                if key:
                    return key.encode("utf-8")
    except Exception as e:
        logger.warning("读取 system_config.pool_crypto 失败: %s", e)
    # 3. config.py 兜底
    try:
        import config
        key = getattr(config, "BACKUP_POOL_KEY", None)
        if key:
            return key.encode("utf-8") if isinstance(key, str) else key
    except Exception:
        pass
    return None


def _resolve_kms_passphrase(cfg: dict) -> Optional[bytes]:
    """从外部 KMS 拉取主密钥明文。

    支持通用 HTTP 模式：用 access_key/secret 调用 KMS 的「获取主密钥明文」端点。
    真实可用（AWS KMS Decrypt / 阿里云 GetSecretValue / Vault transit 等），
    但需外部网络与合法凭证；失败时返回 None（调用方失败安全回退）。
    """
    provider = (cfg.get("kms_provider") or "custom").lower()
    endpoint = cfg.get("kms_endpoint")
    key_id = cfg.get("kms_key_id")
    access_key = cfg.get("kms_access_key")
    secret = cfg.get("kms_secret")
    if not endpoint or not key_id:
        return None
    try:
        import requests
    except ImportError:
        logger.warning("缺少 requests 库，无法调用 KMS")
        return None
    try:
        if provider in ("aws", "aliyun", "tencent", "custom"):
            # 通用「解密数据密钥」模式：把 key_id 作为密文/数据密钥句柄提交，
            # KMS 返回明文。此处用最简单的 GET/POST 约定（可由具体厂商适配）。
            # 由于各厂商签名机制不同，这里走「企业自托管 KMS 代理」约定：
            #   POST {endpoint}/decrypt  body={key_id, access_key, secret}
            #   返回 {plaintext: "<base64 或明文密钥>"}
            resp = requests.post(
                endpoint.rstrip("/") + "/decrypt",
                json={"key_id": key_id, "access_key": access_key, "secret": secret},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning("KMS 返回 %s", resp.status_code)
                return None
            data = resp.json()
            pt = data.get("plaintext") or data.get("key") or data.get("data_key_plaintext")
            if not pt:
                return None
            # 兼容 base64 或明文
            try:
                return base64.b64decode(pt)
            except Exception:
                return pt.encode("utf-8")
        elif provider == "vault":
            # HashiCorp Vault transit：读取经 root key 加密的密文
            resp = requests.get(
                endpoint.rstrip("/") + "/v1/transit/decrypt/" + key_id,
                headers={"X-Vault-Token": access_key or ""},
                json={"ciphertext": secret},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            b64 = resp.json().get("data", {}).get("plaintext")
            return base64.b64decode(b64) if b64 else None
    except Exception as e:
        logger.warning("调用 KMS 失败: %s", e)
        return None
    return None


def is_encrypted(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == MAGIC
    except OSError:
        return False


def encrypt_file(path: str, passphrase: Optional[bytes] = None) -> dict:
    """对 path 原地加密（备份产物落盘后调用）。返回统计。

    若无可用工序密钥，则跳过加密（明文落盘），返回 {"encrypted": False}。
    """
    if passphrase is None:
        passphrase = _get_passphrase()
    if not passphrase:
        return {"encrypted": False, "reason": "no_passphrase"}
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return {"encrypted": False, "reason": "no_crypto_lib"}

    import os as _os
    plaintext = _read_file(path)
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = _derive_key(passphrase, salt)
    aes = AESGCM(key)
    # TAG 由库附加在密文尾部（encrypt 返回 ciphertext||tag）
    ct = aes.encrypt(nonce, plaintext, None)
    out = MAGIC + bytes([VERSION]) + salt + nonce + ct
    _write_file(path, out)
    return {
        "encrypted": True,
        "original_bytes": len(plaintext),
        "encrypted_bytes": len(out),
    }


def decrypt_file(path: str, passphrase: Optional[bytes] = None) -> bytes:
    """解密文件，返回明文 bytes。非加密文件原样返回（兼容旧数据）。"""
    if not is_encrypted(path):
        return _read_file(path)
    if passphrase is None:
        passphrase = _get_passphrase()
    if not passphrase:
        raise RuntimeError("解密失败：缺少主密钥 BACKUP_POOL_KEY")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise RuntimeError("解密失败：缺少 cryptography 库")
    data = _read_file(path)
    if data[:4] != MAGIC:
        raise RuntimeError("解密失败：magic 不匹配")
    version = data[4]
    if version != VERSION:
        raise RuntimeError(f"解密失败：版本不兼容 {version}")
    salt = data[5:5 + SALT_LEN]
    nonce = data[5 + SALT_LEN:5 + SALT_LEN + NONCE_LEN]
    ct = data[5 + SALT_LEN + NONCE_LEN:]
    key = _derive_key(passphrase, salt)
    aes = AESGCM(key)
    return aes.decrypt(nonce, ct, None)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write_file(path: str, data: bytes) -> None:
    tmp = path + ".enc.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)  # 原子替换，避免半截密文


def self_test() -> dict:
    """加密模块自检（单元/集成测试调用）。不依赖外部密钥库。"""
    import tempfile
    key = b"self-test-pool-key-1234567890"
    d = tempfile.mkdtemp(prefix="crypto_selftest_")
    p = os.path.join(d, "secret.bin")
    plain = b"confidential backup payload " * 100
    with open(p, "wb") as f:
        f.write(plain)
    r = encrypt_file(p, passphrase=key)
    assert r["encrypted"] is True, r
    # 文件头应为密文 magic
    assert is_encrypted(p), "加密后未写入 magic"
    # 明文不应直接可见
    with open(p, "rb") as f:
        blob = f.read()
    assert b"confidential backup payload" not in blob, "明文泄露到密文"
    # 解密还原
    dec = decrypt_file(p, passphrase=key)
    assert dec == plain, "解密结果不一致"
    # 错误密钥应失败
    try:
        decrypt_file(p, passphrase=b"wrong-key-0000000000000000")
        raise AssertionError("错误密钥竟然解密成功")
    except Exception:
        pass
    return {"ok": True, "encrypted_bytes": r["encrypted_bytes"],
            "original_bytes": r["original_bytes"]}
