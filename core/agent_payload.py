# -*- coding: utf-8 -*-
"""Agent 执行器黑盒下发（对外不暴露源码）。

原理：平台把执行器源码加密成载荷（agent_payload.enc），目标主机上只落一个
极简 loader + 加密载荷；loader 用 token 派生密钥在内存中解密并 exec，
磁盘上始终没有明文源码。

载荷格式：
- v2（默认，纯标准库）：HMAC-SHA256 作 PRF 的流加密 + HMAC 完整性校验。
  目标机只需 Python3 标准库，契合约定的「零客户端安装、完全离线」。
- v1（兼容旧安装）：AES-256-GCM，仅当目标机装有 cryptography 时可解。

历史缺陷（2026-11-11 现场）：旧 loader 无条件
`from cryptography... import AESGCM`，离线目标机（如 CentOS7 自带 python3.7）
没有该包，systemd 服务启动即失败并 Restart=always 无限重启，
8466 永不监听。现改为默认下发 v2 载荷，loader 对 v1 做条件导入。

诚实边界：v2 不是 AES，也不等效于国密/AES 合规加密；它是「零第三方依赖」
约束下的 HMAC-SHA256 认证流加密（Encrypt-then-MAC），能力自报如实标注。
"""
import base64
import hashlib
import hmac
import json
import os

PAYLOAD_NAME = "agent_payload.enc"
LOADER_NAME = "agent_loader.py"

_STREAM_LABEL = b"aidbm-stream"
_TAG_LABEL = b"aidbm-tag"


def _key(token: str, version: int = 2) -> bytes:
    suffix = ("|aidbm-agent-payload" if version == 1
              else "|aidbm-agent-payload-v2")
    return hashlib.sha256((token + suffix).encode()).digest()


def _stream_xor(key: bytes, data: bytes) -> bytes:
    """HMAC-SHA256 作 PRF 的 XOR 流（自反，加解密同一函数）。"""
    out = bytearray()
    for i in range(0, len(data), 32):
        block = hmac.new(key, _STREAM_LABEL + (i // 32).to_bytes(8, "big"),
                         hashlib.sha256).digest()
        out.extend(a ^ b for a, b in zip(data[i:i + 32], block))
    return bytes(out)


# loader 内不导入任何第三方包；v1 分支才条件导入 cryptography
_LOADER_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AIDBM Backup Agent loader（黑盒启动器，零第三方依赖）。"""
import argparse
import base64
import hashlib
import hmac
import json
import sys


def _key(token, version=2):
    suffix = "|aidbm-agent-payload" if version == 1 else "|aidbm-agent-payload-v2"
    return hashlib.sha256((token + suffix).encode()).digest()


def _stream_xor(key, data):
    out = bytearray()
    for i in range(0, len(data), 32):
        block = hmac.new(key, b"aidbm-stream" + (i // 32).to_bytes(8, "big"),
                         hashlib.sha256).digest()
        out.extend(a ^ b for a, b in zip(data[i:i + 32], block))
    return bytes(out)


def _decrypt(env, token):
    v = int(env.get("v") or 1)
    if v == 2:
        key = _key(token, 2)
        ct = base64.b64decode(env["ct"])
        tag = base64.b64decode(env["tag"])
        want = hmac.new(key, b"aidbm-tag" + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, want):
            raise ValueError("payload integrity check failed")
        return _stream_xor(key, ct).decode("utf-8")
    if v == 1:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(_key(token, 1)).decrypt(
            base64.b64decode(env["n"]), base64.b64decode(env["ct"]),
            None).decode("utf-8")
    raise ValueError("unsupported payload version: %s" % v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--port", type=int, default=8466)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--payload", default="/opt/aidbm-agent/agent_payload.enc")
    ap.add_argument("--allow-dirs", default="")
    args = ap.parse_args()
    with open(args.payload, "rb") as f:
        env = json.loads(f.read().decode("utf-8"))
    code = _decrypt(env, args.token)
    g = {"__name__": "__main__",
         "__AIDBM_ARGS__": {"token": args.token, "port": args.port,
                            "bind": args.bind, "allow_dirs": args.allow_dirs}}
    exec(compile(code, "<aidbm-agent>", "exec"), g)


if __name__ == "__main__":
    main()
'''


def build_payload(agent_src: str, token: str,
                  prefer_aes: bool = False) -> tuple:
    """返回 (loader_py_text, payload_bytes_to_write)。

    prefer_aes=True 且平台装有 cryptography 时生成 v1 AES-GCM 载荷
    （仅用于确认目标机也具备该包的兼容场景）；默认 v2 纯标准库载荷，
    任何 Python3 目标机都能解，不需要在数据库服务器安装任何依赖。
    """
    if prefer_aes and _has_aes():
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = _key(token, 1)
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, agent_src.encode("utf-8"), None)
        env = {"v": 1, "n": base64.b64encode(nonce).decode(),
               "ct": base64.b64encode(ct).decode()}
    else:
        key = _key(token, 2)
        ct = _stream_xor(key, agent_src.encode("utf-8"))
        tag = hmac.new(key, _TAG_LABEL + ct, hashlib.sha256).digest()
        env = {"v": 2, "ct": base64.b64encode(ct).decode(),
               "tag": base64.b64encode(tag).decode()}
    return _LOADER_TEMPLATE, json.dumps(env).encode("utf-8")


def _has_aes() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa
        return True
    except Exception:
        return False


def payload_version(payload_bytes: bytes) -> int:
    """供测试/排障读取载荷版本。"""
    try:
        return int(json.loads(payload_bytes.decode("utf-8")).get("v") or 1)
    except Exception:
        return -1


def fingerprint(agent_src: str) -> str:
    return hashlib.sha256(agent_src.encode()).hexdigest()[:16]
