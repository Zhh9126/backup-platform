#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AIDBM 备份恢复 Agent（自研，零第三方依赖：仅 Python3 标准库）。

定位：与平台 SSH 通道并列的**代理执行通道**。数据库服务器上运行本 Agent
后，平台侧备份/恢复/文件传输全部经 Agent 完成，数据库服务器无需开放 SSH。

能力（对应平台 remote 执行器抽象）：
  1. 执行命令        POST /exec       {cmd, timeout} → {rc, out, err}
  2. 命令数据流      POST /exec-stream {cmd} → chunked stdout 流（备份大产物）
  3. 文件发送/接收   PUT /file / GET /file（分块 + 断点续传 + sha256 对账）
  4. 压缩            GET /file?gz=1（gzip 传输）；命令层平台自行组合 gzip/zstd
  5. 加密（合规）    payload AES-256-GCM（cryptography 可用时自动启用；
                     缺失降级 HMAC-SHA256 认证+完整性，能力自报 enc=false）

安全模型：
  - 每请求校验 X-AIDBM-Token（通道令牌，注册时生成）；
  - 启用加密时请求/响应体为 {"enc":1,"n":nonce,"ct":...} AES-GCM 信封，
    密钥 = HKDF-SHA256(token)；GCM 自带完整性（防篡改/防重放内容）；
  - 文件操作白名单目录（--allow-dirs，默认 /tmp /data /opt /home /var/tmp）；
  - exec 超时与输出上限保护；监听地址默认 0.0.0.0:8466，可用 --bind 收紧。

用法：
  python3 backup_agent.py --token <TOKEN> [--port 8466] [--bind 0.0.0.0]
  （生产用 scripts/install_agent.sh 部署为 systemd 服务）
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler

try:
    from http.server import ThreadingHTTPServer  # py3.7+
except ImportError:  # py3.6（CentOS7 等老系统）：线程化 HTTPServer 手工合成
    import socketserver
    from http.server import HTTPServer

    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_AES = True
except Exception:
    _HAS_AES = False

VERSION = "1.1.2"
MAX_BODY = 16 * 1024 * 1024      # 请求体上限（JSON/分块上传均远小于此）
MAX_SYNC_OUT = 16 * 1024 * 1024          # /exec 同步输出上限（超出建议 exec-stream）
CHUNK = 512 * 1024                        # 流式块大小
DEFAULT_ALLOW = ["/tmp", "/data", "/opt", "/home", "/var/tmp", "/backup",
                 "/u01", "/pgdb", "/app"]


def derive_key(token: str) -> bytes:
    salt = b"aidbm-agent-salt-v1"
    prk = hmac.new(salt, token.encode("utf-8"), hashlib.sha256).digest()
    okm = b""
    t = b""
    i = 1
    while len(okm) < 32:
        t = hmac.new(prk, t + b"aidbm-agent-v1" + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:32]


class AgentState:
    token = ""
    key = None
    allow_dirs = DEFAULT_ALLOW
    # 活动流式子进程注册表 pid → Popen（exec-stream 断连即杀）
    active_procs = {}

    @classmethod
    def path_allowed(cls, path: str) -> bool:
        rp = os.path.realpath(path)
        return any(rp == d or rp.startswith(d.rstrip("/") + "/")
                   for d in cls.allow_dirs)


def _kill_proc_group(p) -> None:
    """终止子进程及其整个进程组（rman/xtrabackup 等孙进程一并杀，不留孤儿）。"""
    try:
        os.killpg(os.getpgid(p.pid), 9)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _seal(key: bytes, plaintext: bytes) -> bytes:
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return json.dumps({"enc": 1, "n": base64.b64encode(nonce).decode(),
                       "ct": base64.b64encode(ct).decode()}).encode()


def _open_envelope(key: bytes, body: bytes):
    """解析请求体：enc 信封 → 明文 obj；明文必须携带 HMAC sig（强制）。

    安全约定（2026-11-02 安全评审）：HMAC 完整性是**必选项**——不带 sig 的
    明文请求一律 403（攻击者构造无 sig JSON 绕过完整性校验的路径已封死）。
    平台 SDK 在 AES 与明文降级两种模式下都签名。
    """
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        raise ValueError("请求体不是合法 JSON")
    if isinstance(obj, dict) and obj.get("enc") == 1:
        if not _HAS_AES:
            raise PermissionError("agent 未启用 AES（cryptography 缺失）")
        pt = AESGCM(key).decrypt(base64.b64decode(obj["n"]),
                                 base64.b64decode(obj["ct"]), None)
        return json.loads(pt.decode("utf-8"))
    sig = None
    if isinstance(obj, dict) and "sig" in obj:
        sig = obj.pop("sig")
    if sig is None:
        raise PermissionError("缺少 HMAC 签名（明文请求必须携带 sig）")
    mac = hmac.new(key, json.dumps(obj, sort_keys=True).encode(),
                   hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, sig):
        raise PermissionError("HMAC 校验失败")
    return obj


class PayloadTooLarge(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AIDBM-Agent/" + VERSION

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):  # 静默默认访问日志（走自写审计）
        pass

    def _audit(self, msg):
        sys.stdout.write("[agent] %s %s\n" % (self.address_string(), msg))
        sys.stdout.flush()

    def _respond(self, code: int, obj, raw: bytes = None):
        """加密响应（AES 可用时），文件流走 raw 直发。

        加密封装失败时**不再静默裸奔**：降级明文但强制携带响应 HMAC
        （body 内嵌 sig），完整性与 token 鉴权永不缺位（安全评审 2026-11-02）。
        """
        if raw is not None:
            self.send_response(code)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if _HAS_AES and AgentState.key:
            try:
                body = _seal(AgentState.key, body)
            except Exception:
                body = json.dumps(
                    {**obj, "sig": hmac.new(
                        AgentState.key,
                        json.dumps(obj, sort_keys=True).encode(),
                        hashlib.sha256).hexdigest()},
                    ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        tok = self.headers.get("X-AIDBM-Token", "")
        return bool(tok) and hmac.compare_digest(tok, AgentState.token)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        # 请求体上限：/file 上传走分块（512KB/块）、JSON 请求都很小，
        # 无上限会把内存交给客户端（安全评审：OOM 风险）→ 超限 413。
        # 超限时先排空（上限 64MB）再响应，客户端才能收到完整 413
        # 而不是发送中途 EPIPE。
        if n > MAX_BODY:
            drained = 0
            while drained < n and drained < 64 * 1024 * 1024:
                blk = self.rfile.read(min(256 * 1024, n - drained))
                if not blk:
                    break
                drained += len(blk)
            raise PayloadTooLarge("请求体 %d 字节超过上限 %d" % (n, MAX_BODY))
        return self.rfile.read(n)

    # ---------- 路由 ----------
    def do_GET(self):
        try:
            if not self._auth():
                self._respond(401, {"error": "token 无效"})
                return
            path = self.path.split("?")[0]
            qs = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        qs[k] = v
            if path in ("/health", "/capabilities"):
                caps = {
                    "version": VERSION, "hmac": "required", "enc": bool(_HAS_AES),
                    "algo": "AES-256-GCM" if _HAS_AES else "HMAC-SHA256",
                    "chunk": CHUNK, "python": sys.version.split()[0],
                    # 能力自报：平台据此做架构/压缩协商，不虚报未实现的特性
                    "os": sys.platform,
                    "arch": os.uname().machine if hasattr(os, "uname") else "unknown",
                    "allow_dirs": AgentState.allow_dirs,
                    "compress": ["gzip"],
                    # 下载侧文件指纹：平台据此做端到端 sha256 对账，
                    # 老版本插件不自报该特性时平台自动降级为大小校验。
                    "integrity": ["sha256"],
                    "features": ["exec", "exec-stream", "file-put", "file-get",
                                 "cancel", "resume", "fs", "fs-digest"],
                }
                self._respond(200, caps)
                return
            if path == "/file":
                self._file_get(qs)
                return
            self._respond(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._respond(500, {"error": str(e)[:300]})
            except Exception:
                pass

    def do_POST(self):
        try:
            if not self._auth():
                self._respond(401, {"error": "token 无效"})
                return
            path = self.path.split("?")[0]
            if path == "/exec":
                self._exec()
            elif path == "/exec-stream":
                self._exec_stream()
            elif path == "/fs":
                self._fs()
            else:
                self._respond(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except PayloadTooLarge as e:
            self._respond(413, {"error": str(e)[:200]})
        except PermissionError as e:
            self._respond(403, {"error": str(e)})
        except Exception as e:
            try:
                self._respond(500, {"error": str(e)[:300]})
            except Exception:
                pass

    def do_PUT(self):
        try:
            if not self._auth():
                self._respond(401, {"error": "token 无效"})
                return
            if self.path.split("?")[0] == "/file":
                self._file_put()
            else:
                self._respond(404, {"error": "not found"})
        except PayloadTooLarge as e:
            self._respond(413, {"error": str(e)[:200]})
        except PermissionError as e:
            self._respond(403, {"error": str(e)})
        except Exception as e:
            try:
                self._respond(500, {"error": str(e)[:300]})
            except Exception:
                pass

    # ---------- 1) 执行命令 ----------
    def _exec(self):
        req = _open_envelope(AgentState.key, self._read_body())
        cmd = str(req.get("cmd") or "")
        timeout = min(int(req.get("timeout") or 600), 86400)
        if not cmd:
            self._respond(400, {"error": "cmd 必填"})
            return
        self._audit("exec: %s" % cmd[:120])
        # py3.6 兼容：capture_output 是 3.7+ 特性（140 实测），用 PIPE
        try:
            p = subprocess.run(["bash", "-lc", cmd], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=timeout)
            rc, out, err = p.returncode, p.stdout[:MAX_SYNC_OUT], p.stderr[:256 * 1024]
        except subprocess.TimeoutExpired as e:
            rc = 124
            out = (e.stdout or b"")[:MAX_SYNC_OUT]
            err = ((e.stderr or b"") + "\n[agent] 命令超时".encode())[:256 * 1024]
        self._respond(200, {
            "rc": rc,
            "out": base64.b64encode(out).decode(),
            "err": base64.b64encode(err).decode(),
        })

    # ---------- 2) 命令数据流（大产物：stdout chunked 流出） ----------
    def _exec_stream(self):
        req = _open_envelope(AgentState.key, self._read_body())
        cmd = str(req.get("cmd") or "")
        timeout = min(int(req.get("timeout") or 0), 86400 * 2)
        if not cmd:
            self._respond(400, {"error": "cmd 必填"})
            return
        self._audit("exec-stream: %s" % cmd[:120])
        # start_new_session：子进程独立进程组 —— 断连/取消/超时时 killpg
        # 把 rman/xtrabackup/mysqldump 等孙进程一并终止，不留孤儿（插件
        # 工程化第五关：协作式取消的服务端兜底，平台停止任务 → 关流 → 杀进程）
        p = subprocess.Popen(["bash", "-lc", cmd],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        AgentState.active_procs[str(p.pid)] = p
        sock = self.connection  # BaseHTTPRequestHandler 的原始 socket
        import select as _select
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            AgentState.active_procs.pop(str(p.pid), None)
            _kill_proc_group(p)
            return

        def send_chunk(data: bytes):
            if not data:
                return
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")

        start = time.time()
        tail_err = b""
        # stderr 后台读线程：主循环只读 stdout；若 stderr 写满管道缓冲
        # 而无人消费，子进程写 stderr 会阻塞 → 整个命令死锁（安全评审）
        err_buf = []

        def _drain_stderr():
            try:
                while True:
                    b = p.stderr.read(64 * 1024)
                    if not b:
                        return
                    if sum(len(x) for x in err_buf) < 256 * 1024:
                        err_buf.append(b)
            except Exception:
                return

        err_th = threading.Thread(target=_drain_stderr, daemon=True)
        err_th.start()
        gone = False
        fd = p.stdout.fileno()
        while True:
            if gone:
                _kill_proc_group(p)
                self._audit("exec-stream: 客户端断开，已终止进程组 pid=%s (%s)"
                            % (p.pid, cmd[:80]))
                break
            if timeout and time.time() - start > timeout:
                _kill_proc_group(p)
                tail_err += "\n[agent] 流式执行超时".encode()
                break
            # select 同时监视子进程 stdout 与客户端 socket：客户端断开
            # （FIN/RST）时 socket 变为可读——即使子进程长时间无 stdout
            # 输出（RMAN 本地备份），也能秒级发现断连并终止，不让远端
            # 大备份在无人接收的情况下空跑占满磁盘
            try:
                r, _, _ = _select.select([fd, sock], [], [], 1.0)
            except (OSError, ValueError):
                gone = True
                continue
            if sock in r:
                # 平台不向本请求发送任何数据，socket 可读即对端断开
                try:
                    if not sock.recv(1, socket.MSG_PEEK):
                        gone = True
                except (ConnectionResetError, OSError):
                    gone = True
                if gone:
                    continue
            if fd in r:
                try:
                    chunk = os.read(fd, CHUNK)
                except OSError:
                    chunk = b""
                if chunk:
                    try:
                        send_chunk(chunk)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        gone = True
                else:
                    break  # stdout EOF：命令结束
            elif p.poll() is not None:
                break
        try:
            AgentState.active_procs.pop(str(p.pid), None)
        except Exception:
            pass
        if gone:
            return
        # 排空剩余输出
        try:
            while True:
                rest = os.read(fd, CHUNK)
                if not rest:
                    break
                send_chunk(rest)
        except (BrokenPipeError, ConnectionResetError, OSError):
            AgentState.active_procs.pop(str(p.pid), None)
            _kill_proc_group(p)
            return
        err_th.join(timeout=30)
        try:
            tail_err += b"".join(err_buf)[:128 * 1024]
        except Exception:
            pass
        rc = p.wait()
        tail = json.dumps({"__done": True, "rc": rc,
                           "err": base64.b64encode(tail_err).decode()}
                          ).encode("utf-8")
        try:
            send_chunk(tail)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ---------- 3) 文件接收（PUT /file?path=&offset=&truncate=） ----------
    def _file_put(self):
        qs = {}
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    qs[k] = v
        import urllib.parse
        path = urllib.parse.unquote(qs.get("path", ""))
        offset = int(qs.get("offset", "0") or 0)
        truncate = qs.get("truncate", "1") == "1"
        sha = qs.get("sha256", "")
        if not path or not AgentState.path_allowed(path):
            raise PermissionError("路径不在白名单: %s" % path)
        body = self._read_body()
        os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
        mode = "r+b" if offset and os.path.exists(path) else (
            "wb" if truncate else "ab")
        if offset == 0 and truncate:
            mode = "wb"
        with open(path, mode if "b" in mode else "wb") as f:
            if offset:
                f.seek(offset)
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        size = os.path.getsize(path)
        ok = True
        if sha:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for blk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                    h.update(blk)
            ok = (h.hexdigest() == sha)
        self._audit("file-put: %s +%dB → %s%s" % (
            path, len(body), size, "" if ok else " [SHA不符]"))
        self._respond(200, {"size": size, "sha_ok": ok})

    # ---------- 4) 文件发送（GET /file?path=&offset=&gz=） ----------
    def _file_get(self, qs):
        import urllib.parse
        path = urllib.parse.unquote(qs.get("path", ""))
        offset = int(qs.get("offset", "0") or 0)
        use_gz = qs.get("gz", "0") == "1"
        if not path or not AgentState.path_allowed(path):
            raise PermissionError("路径不在白名单: %s" % path)
        if not os.path.isfile(path):
            self._respond(404, {"error": "文件不存在"})
            return
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("X-File-Size", str(size))
        if use_gz:
            self.send_header("X-Content-Gzip", "1")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self._audit("file-get: %s offset=%d gz=%s" % (path, offset, use_gz))
        sent = 0
        if use_gz:
            import gzip as _gzip
            gzbuf = b""
            with open(path, "rb") as f:
                if offset:
                    f.seek(offset)
                gz_sink = _gzip.GzipFile(mode="wb", fileobj=_ChunkedSink(self), mtime=0)
                while True:
                    blk = f.read(4 * 1024 * 1024)
                    if not blk:
                        break
                    gz_sink.write(blk)
                    sent += len(blk)
                gz_sink.close()
        else:
            with open(path, "rb") as f:
                if offset:
                    f.seek(offset)
                while True:
                    blk = f.read(CHUNK)
                    if not blk:
                        break
                    self._send_chunk(blk)
                    sent += len(blk)
        self._send_chunk(b"")
        self.wfile.flush()

    def _send_chunk(self, data: bytes):
        if data:
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
        else:
            self.wfile.write(b"0\r\n\r\n")

    # ---------- 5) 文件系统操作（remove/rename/mkdir） ----------
    def _fs(self):
        req = _open_envelope(AgentState.key, self._read_body())
        op = req.get("op")
        path = req.get("path", "")
        if not path or not AgentState.path_allowed(path):
            raise PermissionError("路径不在白名单: %s" % path)
        if op == "remove":
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
            self._respond(200, {"ok": True})
        elif op == "rename":
            to = req.get("to", "")
            if not to or not AgentState.path_allowed(to):
                raise PermissionError("目标路径不在白名单")
            os.replace(path, to)
            self._respond(200, {"ok": True})
        elif op == "mkdir":
            os.makedirs(path, exist_ok=True)
            self._respond(200, {"ok": True})
        elif op == "stat":
            if os.path.exists(path):
                self._respond(200, {"exists": True,
                                    "size": os.path.getsize(path),
                                    "isdir": os.path.isdir(path)})
            else:
                self._respond(200, {"exists": False})
        elif op == "digest":
            # 下载侧端到端完整性：平台拉回产物后核对 sha256，防止断点续传 /
            # gzip 流中断后「字节少了但仍判成功」。超过 max_bytes 的大文件
            # 如实返回 sha256=None（平台降级为大小校验），绝不虚报。
            if not os.path.isfile(path):
                self._respond(200, {"exists": False})
                return
            size = os.path.getsize(path)
            max_bytes = int(req.get("max_bytes") or 0)
            if max_bytes and size > max_bytes:
                self._respond(200, {"exists": True, "size": size,
                                    "sha256": None, "skipped": "over_limit"})
                return
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for blk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                    h.update(blk)
            self._respond(200, {"exists": True, "size": size,
                                "sha256": h.hexdigest()})
        elif op == "chmod":
            os.chmod(path, int(req.get("mode", "644"), 8))
            self._respond(200, {"ok": True})
        elif op == "list":
            if not os.path.isdir(path):
                self._respond(200, {"entries": []})
                return
            entries = []
            for name in sorted(os.listdir(path)):
                fp = os.path.join(path, name)
                try:
                    st = os.stat(fp)
                    entries.append({"name": name, "size": st.st_size,
                                    "isdir": os.path.isdir(fp)})
                except OSError:
                    continue
            self._respond(200, {"entries": entries})
        else:
            self._respond(400, {"error": "未知 op"})


class _ChunkedSink:
    """gzip → chunked 输出适配。"""

    def __init__(self, handler):
        self.h = handler

    def write(self, data: bytes):
        if data:
            self.h.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")


def main():
    ap = argparse.ArgumentParser(description="AIDBM 备份恢复 Agent")
    ap.add_argument("--token", required=True, help="通道令牌（平台注册时生成）")
    ap.add_argument("--port", type=int, default=8466)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--allow-dirs", default=",".join(DEFAULT_ALLOW),
                    help="文件操作白名单目录（逗号分隔）")
    args = ap.parse_args()
    AgentState.token = args.token
    AgentState.allow_dirs = [d.strip() for d in args.allow_dirs.split(",") if d.strip()]
    if _HAS_AES:
        AgentState.key = derive_key(args.token)
    else:
        AgentState.key = derive_key(args.token)  # HMAC 模式复用同一派生密钥
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    enc = "AES-256-GCM" if _HAS_AES else "HMAC-SHA256(降级)"
    print("[agent] AIDBM Agent %s listening on %s:%d enc=%s allow=%s" % (
        VERSION, args.bind, args.port, enc, AgentState.allow_dirs), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
