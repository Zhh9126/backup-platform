# -*- coding: utf-8 -*-
"""
AI 模型密钥加密模块。

采用 XOR + base64 加密方式，与 core/db.py 的 encrypt_secret/decrypt_secret
保持一致的轻量混淆风格（非高强度加密，生产环境请用密钥管理服务）。

加密密钥优先级：环境变量 AI_SECRET_KEY → 环境变量 SECRET_KEY → config.SECRET_KEY。
"""
import base64
import hashlib
import os


def _derive_key(key: str) -> bytes:
    """从密钥字符串派生 SHA-256 哈希作为 XOR 密钥流。"""
    return hashlib.sha256(key.encode("utf-8")).digest()


def _get_secret_key() -> str:
    """获取加密密钥，优先级：AI_SECRET_KEY → SECRET_KEY → config.SECRET_KEY。"""
    key = os.environ.get("AI_SECRET_KEY", "")
    if key:
        return key
    key = os.environ.get("SECRET_KEY", "")
    if key:
        return key
    try:
        import config
        key = getattr(config, "SECRET_KEY", "")
    except ImportError:
        pass
    return key or "ai-secret-default-key"


def encrypt_api_key(plain: str) -> str:
    """加密 API Key：XOR + base64，前缀 'aienc:' 标识。

    Args:
        plain: 明文 API Key

    Returns:
        加密后的字符串，格式 'aienc:<base64>'
    """
    if not plain:
        return ""
    k = _derive_key(_get_secret_key())
    data = plain.encode("utf-8")
    out = bytes(b ^ k[i % len(k)] for i, b in enumerate(data))
    return "aienc:" + base64.b64encode(out).decode("ascii")


def decrypt_api_key(token: str) -> str:
    """解密 API Key。

    Args:
        token: 加密字符串（'aienc:<base64>' 格式）或明文（兼容旧数据）

    Returns:
        解密后的明文 API Key
    """
    if not token:
        return ""
    if token.startswith("aienc:"):
        token = token[6:]
        data = base64.b64decode(token)
        k = _derive_key(_get_secret_key())
        return bytes(b ^ k[i % len(k)] for i, b in enumerate(data)).decode("utf-8", "ignore")
    # 兼容明文（未加密的历史数据）
    return token
