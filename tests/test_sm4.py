# -*- coding: utf-8 -*-
"""P0-3 国密 SM4 测试：GB/T 32907 标准向量 + crypto_pool 双算法 + 凭据三格式兼容。"""
import base64
import os
import sys
import tempfile

os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="sm4t_"), "meta.db")
os.environ["BACKUP_ROOT"] = os.environ["META_DB_PATH"].rsplit("/", 1)[0]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

KEY = bytes.fromhex("0123456789abcdeffedcba9876543210")
PT = bytes.fromhex("0123456789abcdeffedcba9876543210")
CT_STD = "681edf34d206965e86b3e94f536e4246"


def test_sm4_standard_vector():
    """GB/T 32907-2016 标准测试向量（cryptography 库 SM4 实现）。"""
    enc = Cipher(algorithms.SM4(KEY), modes.ECB()).encryptor()
    ct = (enc.update(PT) + enc.finalize()).hex()
    assert ct == CT_STD, ct


def test_pool_encrypt_sm4_roundtrip():
    from core import crypto_pool as cp
    d = tempfile.mkdtemp(prefix="sm4rt_")
    p = os.path.join(d, "backup.bin")
    plain = ("AIDBM 国密 SM4 备份产物 ".encode("utf-8")) * 500
    with open(p, "wb") as f:
        f.write(plain)
    r = cp.encrypt_file(p, passphrase=b"pool-key-123", algo="sm4")
    assert r["encrypted"] and r["algo"] == "sm4", r
    assert cp.is_encrypted(p)
    assert cp.decrypt_file(p, passphrase=b"pool-key-123") == plain


def test_pool_aes_v1_backward_compat():
    """v1 头（无算法字节，AES）解密兼容——手工构造 v1 格式。"""
    from core import crypto_pool as cp
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = tempfile.mkdtemp(prefix="sm4v1_")
    p = os.path.join(d, "old.bin")
    plain = b"legacy v1 payload" * 10
    salt, nonce = os.urandom(16), os.urandom(12)
    key = cp._derive_key(b"pool-key-123", salt)
    ct = AESGCM(key).encrypt(nonce, plain, None)
    with open(p, "wb") as f:
        f.write(cp.MAGIC + bytes([1]) + salt + nonce + ct)  # v1 头
    assert cp.decrypt_file(p, passphrase=b"pool-key-123") == plain


def test_secret_sm4_and_compat():
    import core.db as db
    tok = db.encrypt_secret("P@ssw0rd-中文-123")
    assert tok.startswith("sm4v1:"), tok
    assert db.decrypt_secret(tok) == "P@ssw0rd-中文-123"
    # 每次 nonce 随机 → 密文不同（防重放/比对）
    tok2 = db.encrypt_secret("P@ssw0rd-中文-123")
    assert tok != tok2
    assert db.decrypt_secret(tok2) == "P@ssw0rd-中文-123"
    # 历史 enc: 混淆格式兼容读取
    old = "enc:" + base64.b64encode(bytes(
        b ^ db._derive_key(db.config.SECRET_KEY)[i % 32]
        for i, b in enumerate("legacy-pw".encode()))).decode()
    assert db.decrypt_secret(old) == "legacy-pw"
    # 明文兼容
    assert db.decrypt_secret("plain-pw") == "plain-pw"
    # 空值
    assert db.encrypt_secret("") == "" and db.decrypt_secret("") == ""


def test_pool_self_test():
    from core import crypto_pool as cp
    r = cp.self_test()
    assert r.get("ok"), r
