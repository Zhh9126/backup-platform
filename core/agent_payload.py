# -*- coding: utf-8 -*-
"""Agent 执行器黑盒下发（对外不暴露源码）。

原理：平台把执行器源码用 AES-256-GCM 加密成载荷（agent_payload.enc），
目标主机上只落一个**极简 loader**（约 40 行，不含任何备份业务实现）+
加密载荷；loader 启动时用 token 派生密钥在内存中解密并 exec——
磁盘上始终没有明文源码，逆向需要先拿到 token（即已拥有该主机控制权）。
"""
import base64
import hashlib
import json
import os

PAYLOAD_NAME = "agent_payload.enc"
LOADER_NAME = "agent_loader.py"

_LOADER_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AIDBM Backup Agent loader（黑盒启动器）。

磁盘上只有加密载荷 agent_payload.enc；本 loader 用 token 派生密钥在
内存中解密并执行，不落明文。密钥派生：SHA256(token|aidbm-agent-payload)。
"""
import argparse
import base64
import hashlib
import json
import sys


def _key(token: str) -> bytes:
    return hashlib.sha256((token + "|aidbm-agent-payload").encode()).digest()


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
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    code = AESGCM(_key(args.token)).decrypt(
        base64.b64decode(env["n"]), base64.b64decode(env["ct"]), None)
    g = {"__name__": "__main__",
         "__AIDBM_ARGS__": {"token": args.token, "port": args.port,
                            "bind": args.bind, "allow_dirs": args.allow_dirs}}
    exec(compile(code.decode("utf-8"), "<aidbm-agent>", "exec"), g)


if __name__ == "__main__":
    main()
'''


def build_payload(agent_src: str, token: str) -> tuple:
    """返回 (loader_py_text, payload_bytes_to_write)。"""
    if not _has_aes():
        raise RuntimeError("平台缺少 cryptography，无法生成加密载荷")
    key = hashlib.sha256((token + "|aidbm-agent-payload").encode()).digest()
    nonce = os.urandom(12)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ct = AESGCM(key).encrypt(nonce, agent_src.encode("utf-8"), None)
    env = {"v": 1, "n": base64.b64encode(nonce).decode(),
           "ct": base64.b64encode(ct).decode()}
    return _LOADER_TEMPLATE, json.dumps(env).encode("utf-8")


def _has_aes() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa
        return True
    except Exception:
        return False


def fingerprint(agent_src: str) -> str:
    return hashlib.sha256(agent_src.encode()).hexdigest()[:16]
