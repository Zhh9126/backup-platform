# -*- coding: utf-8 -*-
"""Agent 执行通道：平台侧 SDK + paramiko 兼容层。

设计：AgentSSHClient / AgentTransport / AgentChannel / AgentSFTP 按
paramiko 同名接口行为实现，因此引擎侧的
  client.get_transport().open_session() → sess.exec_command(...)
  client.open_sftp() → sftp.open/stat/remove/rename/mkdir/put/get
**零改动**即可经 Agent 通道执行（remote_dump._connect 按
ssh_host["transport"]=="agent" 直接返回 AgentSSHClient）。

能力映射（remote 执行器抽象）：
  执行命令   → POST /exec / /exec-stream（stdout 数据流）
  发送/接收文件 → PUT /file（分块+断点续传）/ GET /file（Range + 可选 gzip）
  数据流     → /exec-stream chunked 流（备份大产物流式拉回）
  压缩       → /file?gz=1（gzip 传输层）；命令层可自由组合 gzip/zstd
  加密(合规)  → payload AES-256-GCM（HKDF(token) 派生密钥，GCM 防篡改）
"""
import base64
import hashlib
import http.client
import json
import os
import socket
import threading
import time
import urllib.parse

from core import crypto as _crypto

CHUNK = 512 * 1024


class AgentError(RuntimeError):
    pass


class AgentTransport:
    """单主机 Agent HTTP 客户端（keep-alive，线程内使用）。"""

    def __init__(self, host: str, port: int, token: str, timeout: int = 30,
                 label: str = ""):
        self.host = host
        self.port = int(port or 8466)
        self.token = token
        self.timeout = timeout
        self.label = label or ("agent:%s:%s" % (host, self.port))
        self.key = _crypto.derive_key(token)
        self._conn = None
        self.caps = {}
        self._lock = threading.Lock()

    # ---- 连接管理 ----
    def _connection(self, timeout=None) -> http.client.HTTPConnection:
        if self._conn is None:
            self._conn = http.client.HTTPConnection(
                self.host, self.port, timeout=timeout or self.timeout)
        return self._conn

    def _reset(self):
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _request(self, method: str, path: str, body: bytes = None,
                 timeout: int = None, stream: bool = False):
        """返回 (status, headers, body|response)。stream=True 返回 response。

        流式响应（stream=True）独占新连接：长流命令（pg_basebackup 大产物）
        可能长时间无输出，keep-alive 复用的旧连接带着旧 socket timeout，
        会把流掐断（实测 64s 断流循环）——独占连接并使用本次请求的 timeout。
        """
        for attempt in (1, 2):
            try:
                if stream:
                    self._reset()
                conn = self._connection(timeout=timeout)
                headers = {"X-AIDBM-Token": self.token,
                           "Connection": "keep-alive"}
                if body is not None:
                    headers["Content-Length"] = str(len(body))
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                if stream:
                    return resp.status, dict(resp.getheaders()), resp
                data = resp.read()
                return resp.status, dict(resp.getheaders()), data
            except (http.client.HTTPException, socket.error, OSError) as e:
                self._reset()
                if attempt == 2:
                    raise AgentError(
                        "Agent %s 连接失败: %s" % (self.label, e))
                time.sleep(1)

    # ---- 能力探测 ----
    def capabilities(self, refresh: bool = False) -> dict:
        if self.caps and not refresh:
            return self.caps
        st, _h, data = self._request("GET", "/capabilities")
        if st != 200:
            raise AgentError("Agent %s 能力探测失败: HTTP %s" % (self.label, st))
        self.caps = self._open_json(data)
        return self.caps

    def health(self) -> dict:
        st, _h, data = self._request("GET", "/health", timeout=8)
        if st != 200:
            raise AgentError("Agent %s 健康检查失败: HTTP %s" % (self.label, st))
        return self._open_json(data)

    # ---- 加密封包 ----
    def _seal(self, obj) -> bytes:
        # 按 **agent 能力** 选择：agent 报 enc=False（缺 cryptography）时
        # 降级 HMAC-SHA256 认证+完整性模式（170 实测 RHEL 自带 python3.7
        # 无 cryptography）。平台侧永远优先 AES。
        if self.caps.get("enc") and _crypto.has_aes():
            try:
                return json.dumps(_crypto.encrypt_json(self.key, obj)).encode()
            except RuntimeError:
                pass
        body = json.dumps(obj, sort_keys=True).encode()
        sig = _crypto.hmac_sign(self.key, body)
        return json.dumps({**obj, "sig": sig}).encode()

    def _open_json(self, raw: bytes):
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            raise AgentError("Agent %s 响应不是 JSON（token 是否一致？）" % self.label)
        if isinstance(obj, dict) and obj.get("enc") == 1:
            return _crypto.decrypt_json(self.key, obj)
        if isinstance(obj, dict) and "sig" in obj:
            sig = obj.pop("sig")
            body = json.dumps(obj, sort_keys=True).encode()
            if not _crypto.hmac_verify(self.key, body, sig):
                raise AgentError("Agent 响应 HMAC 校验失败")
        return obj

    # ---- 执行命令（同步，小输出） ----
    def exec(self, cmd: str, timeout: int = 600) -> dict:
        st, _h, data = self._request(
            "POST", "/exec", body=self._seal({"cmd": cmd, "timeout": timeout}),
            timeout=timeout + 30)
        if st != 200:
            raise AgentError("Agent exec HTTP %s: %s" % (st, data[:200]))
        r = self._open_json(data)
        r["out"] = base64.b64decode(r.get("out") or "")
        r["err"] = base64.b64decode(r.get("err") or "")
        return r

    # ---- 执行命令（流式：stdout 大产物） ----
    def exec_stream(self, cmd: str, timeout: int = 0):
        """返回迭代器：产出 (kind, data)；kind ∈ stdout|done。
        done 帧携带 {"rc", "err"}。"""
        body = self._seal({"cmd": cmd, "timeout": timeout})
        st, _h, resp = self._request(
            "POST", "/exec-stream", body=body, timeout=timeout or 86400,
            stream=True)
        if st != 200:
            raise AgentError("Agent exec-stream HTTP %s" % st)
        return self._iter_chunked(resp)

    @staticmethod
    def _iter_chunked(resp):
        """HTTP chunked 解码（requests 不可用时手工实现）。"""
        while True:
            line = resp.fp.readline().strip()
            if not line:
                continue
            try:
                size = int(line.split(b";")[0], 16)
            except ValueError:
                raise AgentError("chunked 协议错误: %r" % line[:60])
            if size == 0:
                resp.fp.readline()  # 尾部 CRLF
                return
            data = b""
            while len(data) < size:
                blk = resp.fp.read(size - len(data))
                if not blk:
                    raise AgentError("chunked 流提前结束")
                data += blk
            resp.fp.read(2)  # CRLF
            yield data

    # ---- 文件：上传（分块，覆盖语义） ----
    def file_put(self, local_path: str, remote_path: str,
                 chunk: int = 4 * 1024 * 1024,
                 progress=None, label: str = "") -> int:
        """paramiko sftp.put 语义：**全新覆盖**。

        断点续传不走这里——remote_dump.sftp_put_resumable 用 .part 文件
        + sha 边车管理续传（把目标文件残留 size 当续传 offset 会把上次
        失败的错误内容拼进去，sha 对账失败，140 实测）。
        """
        size = os.path.getsize(local_path)
        offset = 0
        h = hashlib.sha256()
        with open(local_path, "rb") as f:
            f.seek(offset)
            sent = offset
            while True:
                blk = f.read(chunk)
                if not blk:
                    break
                # 首块 truncate=1：清掉远端残留（残留会让 append 错位、
                # sha256 对账失败——140 dbg 实测）；断点续传时 sent=offset>0
                # 走 append，不破坏已传字节
                q = urllib.parse.urlencode({
                    "path": remote_path, "offset": sent,
                    "truncate": "1" if sent == 0 else "0"})
                st, _h, data = self._request(
                    "PUT", "/file?" + q, body=blk, timeout=120)
                if st != 200:
                    raise AgentError("Agent 上传 HTTP %s: %s" % (st, data[:150]))
                sent += len(blk)
                if progress and sent % (16 * 1024 * 1024) < chunk:
                    progress(sent, size)
            h = hashlib.sha256()
        with open(local_path, "rb") as f:
            for blk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(blk)
        # 完整性对账（合规要求：端到端校验）
        st, _h, data = self._request(
            "PUT", "/file?" + urllib.parse.urlencode(
                {"path": remote_path, "offset": size, "truncate": "0",
                 "sha256": h.hexdigest()}),
            body=b"", timeout=60)
        if st != 200:
            raise AgentError("Agent 上传收尾失败: HTTP %s" % st)
        r = self._open_json(data)
        if not r.get("sha_ok", True):
            raise AgentError("Agent 上传 sha256 对账不一致（网络损坏？）")
        return size

    # ---- 文件：下载（Range 续传 + 可选 gzip） ----
    def file_get(self, remote_path: str, local_path: str,
                 progress=None, label: str = "") -> int:
        st, _h, data = self._request(
            "POST", "/fs", body=self._seal({"op": "stat", "path": remote_path}))
        if st != 200:
            raise AgentError("Agent stat 失败: HTTP %s" % st)
        r = self._open_json(data)
        if not r.get("exists"):
            raise AgentError("远端文件不存在: %s" % remote_path)
        remote_size = int(r.get("size") or 0)
        offset = 0
        if os.path.exists(local_path):
            offset = os.path.getsize(local_path)
            if offset > remote_size:
                offset = 0
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        q = urllib.parse.urlencode({"path": remote_path, "offset": offset})
        st, _h, resp = self._request("GET", "/file?" + q, stream=True,
                                     timeout=86400)
        if st != 200:
            raise AgentError("Agent 下载 HTTP %s" % st)
        written = offset
        with open(local_path, "ab" if offset else "wb") as f:
            for data in self._iter_chunked(resp):
                f.write(data)
                written += len(data)
                if progress and written % (16 * 1024 * 1024) < CHUNK:
                    progress(written, remote_size)
        return written

    # ---- 文件系统操作 ----
    def fs(self, op: str, path: str, to: str = "", mode: str = ""):
        payload = {"op": op, "path": path, "to": to}
        if mode:
            payload["mode"] = mode
        st, _h, data = self._request(
            "POST", "/fs", body=self._seal(payload))
        if st != 200:
            raise AgentError("Agent fs.%s HTTP %s" % (op, st))
        return self._open_json(data)


    def open_sftp(self):
        return AgentSFTP(self)

    def open_session(self):
        """paramiko 语义：transport.open_session() → session channel。"""
        return AgentSessionChannel(self)


class AgentChannel:
    """模拟 paramiko channel： recv/recv_stderr/exit_status 系列。

    实现：/exec-stream —— stdout 为 chunked 流；末尾 JSON 帧
    {"__done":true,"rc","err"(b64)} 转为 exit_status 与 stderr。
    """

    def __init__(self, transport: AgentTransport, cmd: str, timeout: int = 0):
        self.t = transport
        self._it = None
        self._buf = bytearray()
        self._err = bytearray()
        self._rc = None
        self._closed = False
        self._deadline = time.time() + (timeout or 86400 * 2)
        try:
            self._it = transport.exec_stream(cmd, timeout=timeout)
        except AgentError:
            # 连接期失败：伪装 rc=255，让引擎按"通道失败"处理
            self._err += "[agent-channel] ".encode() + str(
                getattr(transport, "label", "")).encode()
            self._rc = 255

    # ---- paramiko channel 兼容 ----
    def gettimeout(self):
        return None

    def settimeout(self, v):
        pass

    def recv_ready(self) -> bool:
        if self._buf:
            return True
        if self._rc is not None:
            return False
        if self._it is None:
            return False
        if time.time() > self._deadline:
            self._rc = 124
            self._err += "[agent-channel] 总超时".encode()
            return True
        try:
            data = next(self._it)
        except StopIteration:
            return self._recv_ready_slow()
        except AgentError as e:
            self._err += str(e).encode()
            self._rc = 255
            return True
        # 解析帧：done 帧是 JSON {"__done":true,"rc":..,"err":b64}
        if data.startswith(b"{") and b"__done" in data[:64]:
            try:
                frame = json.loads(data.decode("utf-8"))
            except Exception:
                frame = None
            if isinstance(frame, dict) and frame.get("__done"):
                self._rc = int(frame.get("rc", 0))
                self._err += base64.b64decode(frame.get("err") or "")
                return True
        self._buf += data
        return True

    def _recv_ready_slow(self):
        return False  # StopIteration 后等待 done 帧（同帧内不会发生）

    def recv(self, n: int = CHUNK) -> bytes:
        while not self._buf:
            if not self.recv_ready():
                return b""
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def recv_stderr_ready(self) -> bool:
        return bool(self._err)

    def recv_stderr(self, n: int = 8192) -> bytes:
        out = bytes(self._err[:n])
        del self._err[:n]
        return out

    def exit_status_ready(self) -> bool:
        return self._rc is not None and not self._buf

    def recv_exit_status(self) -> int:
        while self._rc is None:
            if not self.recv_ready() and not self._buf:
                time.sleep(0.05)
                if self._rc is not None:
                    break
        return self._rc if self._rc is not None else 255

    def sendall(self, data: bytes):
        # 引擎仅在 input_data 场景调用（如 mysql stdin 灌入）；agent 通道
        # 第一版不支持 stdin 灌入（调用方为空场景）
        raise AgentError("agent 通道暂不支持 stdin 灌入")

    def shutdown_write(self):
        pass

    def close(self):
        self._closed = True
        self._it = None
        # 强制断开底层 HTTP 连接：Agent 依靠「断连即杀」（select 检测
        # socket EOF → killpg）终止远端子进程组。仅丢迭代器不会断 TCP，
        # 取消/停止任务时远端大备份会在无人接收下继续空跑占满磁盘
        # （e2e_cancel_kill T3 实测暴露）。
        try:
            self.t._reset()
        except Exception:
            pass


class AgentSessionChannel:
    """paramiko 语义：transport.open_session() 返回的对象即是 Channel——
    exec_command(cmd) 在其上执行，recv/recv_stderr/exit_status_* 随后可用。"""

    def __init__(self, t: AgentTransport):
        self.t = t
        self._ch = None

    def exec_command(self, cmd: str, timeout: int = 0):
        # timeout：agent 侧流式执行硬顶（秒，0=不限）——到点 agent killpg
        # 终止进程组并返回 done 帧（rc 为被杀进程返回码，通常 -9）
        self._ch = AgentChannel(self.t, cmd, timeout=timeout)
        return 0  # paramiko channel.exec_command 返回 None/状态；引擎不依赖

    # 以下全部转发内层 AgentChannel
    def recv_ready(self):
        return self._ch.recv_ready()

    def recv(self, n: int = CHUNK):
        return self._ch.recv(n)

    def recv_stderr_ready(self):
        return self._ch.recv_stderr_ready()

    def recv_stderr(self, n: int = 8192):
        return self._ch.recv_stderr(n)

    def exit_status_ready(self):
        return self._ch.exit_status_ready()

    def recv_exit_status(self):
        return self._ch.recv_exit_status()

    def sendall(self, data: bytes):
        self._ch.sendall(data)

    def shutdown_write(self):
        self._ch.shutdown_write()

    def close(self):
        if self._ch:
            self._ch.close()


class AgentPTransport:
    """模拟 paramiko transport：is_active/open_session/get_security_options。"""

    def __init__(self, transport: AgentTransport):
        self.t = transport

    def is_active(self):
        # agent 通道无长连接概念（每请求新建/复用 HTTP），恒视为存活；
        # 不能真实探活——_sftp_session_refresh 会因探活抖动误判断线并用
        # host_key 走 paramiko 重连 8466 端口（banner 错误，170 实测）
        return True

    def set_keepalive(self, interval):
        pass

    # paramiko transport 属性兼容（file._tune_ssh_transport 会写这两个值）
    default_window_size = 0
    default_max_packet_size = 0

    def open_session(self):
        return AgentSessionChannel(self.t)

    def close(self):
        self.t._reset()


class AgentSFTPFile:
    """模拟 paramiko sfile：读（顺序）/写（缓冲分块）。"""

    def __init__(self, t: AgentTransport, path: str, mode: str):
        self.t = t
        self.path = path
        self.mode = mode
        self._rbuf = bytearray()
        self._rc = None
        self._woffset = None
        self._wbuf = bytearray()
        if "r" in mode:
            st = t.fs("stat", path)
            if not st.get("exists"):
                raise IOError(2, "远端文件不存在: %s" % path)
            self._size = int(st.get("size") or 0)
            self._roffset = 0

    def _flush(self, final: bool = False):
        if not self._wbuf:
            return
        data = bytes(self._wbuf)
        self._wbuf.clear()
        if self._woffset is None:
            st = self.t.fs("stat", self.path)
            self._woffset = int(st.get("size") or 0) if (
                "a" in self.mode and st.get("exists")) else 0
        q = urllib.parse.urlencode({"path": self.path,
                                    "offset": self._woffset,
                                    "truncate": "0"})
        st_code, _h, data_back = self.t._request("PUT", "/file?" + q,
                                                 body=data, timeout=180)
        if st_code != 200:
            raise IOError(5, "Agent 写文件失败: HTTP %s" % st_code)
        self._woffset += len(data)

    def write(self, data: bytes):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._wbuf += data
        if len(self._wbuf) >= 4 * 1024 * 1024:
            self._flush()

    def read(self, n: int = -1) -> bytes:
        if self._rc is not None and not self._rbuf:
            return b""
        while n < 0 or len(self._rbuf) < n:
            if self._rc is not None:
                break
            if self._roffset >= self._size:
                break
            q = urllib.parse.urlencode({"path": self.path,
                                        "offset": self._roffset})
            st_code, _h, resp = self.t._request(
                "GET", "/file?" + q, stream=True, timeout=1800)
            if st_code != 200:
                raise IOError(5, "Agent 读文件失败: HTTP %s" % st_code)
            got = bytearray()
            for data in AgentTransport._iter_chunked(resp):
                got += data
            self._roffset = self._size
            self._rbuf += bytes(got)
            if not self._rbuf:
                break
        if n < 0:
            out = bytes(self._rbuf)
            self._rbuf = bytearray()
            return out
        out = bytes(self._rbuf[:n])
        del self._rbuf[:n]
        return out

    def readlines(self):
        data = self.read()
        return [l + b"\n" for l in data.split(b"\n") if l]

    def seek(self, offset: int, whence: int = 0):
        if "r" in self.mode:
            if whence == 0:
                self._roffset = offset
            elif whence == 1:
                self._roffset += offset
            else:
                self._roffset = self._size + offset
            self._rbuf = bytearray()
            self._rc = None
            # 越界读：seek 超出远端大小 → 后续 read 直接 EOF
            if self._roffset >= self._size:
                self._roffset = self._size
                self._rc = 0
        else:
            if self._woffset is None:
                st = self.t.fs("stat", self.path)
                self._woffset = int(st.get("size") or 0) if (
                    "a" in self.mode and st.get("exists")) else 0
            if whence == 0:
                self._woffset = offset
            elif whence == 1:
                self._woffset += offset
            self._wbuf.clear()

    def tell(self) -> int:
        if "r" in self.mode:
            return self._roffset - len(self._rbuf)
        if self._woffset is None:
            st = self.t.fs("stat", self.path)
            base = int(st.get("size") or 0) if (
                "a" in self.mode and st.get("exists")) else 0
            self._woffset = base
        return self._woffset + len(self._wbuf)

    def readable(self):
        return "r" in self.mode

    def writable(self):
        return "w" in self.mode or "a" in self.mode

    def flush(self):
        self._flush()

    def close(self):
        if "w" in self.mode or "a" in self.mode:
            self._flush(final=True)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class AgentSFTPStat:
    def __init__(self, size):
        self.st_size = size
        self.st_mode = 0o100644


class AgentSFTP:
    """模拟 paramiko sftp 客户端（引擎使用的全部方法子集）。"""

    def __init__(self, t: AgentTransport):
        self.t = t

    def open(self, path: str, mode: str = "r"):
        return AgentSFTPFile(self.t, path, mode)

    def stat(self, path: str):
        st = self.t.fs("stat", path)
        if not st.get("exists"):
            raise IOError(2, "远端不存在: %s" % path)
        return AgentSFTPStat(int(st.get("size") or 0))

    def remove(self, path: str):
        self.t.fs("remove", path)

    def rename(self, src: str, dst: str):
        self.t.fs("rename", src, dst)

    def mkdir(self, path: str):
        self.t.fs("mkdir", path)

    def chmod(self, path: str, mode: int):
        self.t.fs("chmod", path, mode=oct(mode))

    def listdir_attr(self, path: str):
        """模拟 paramiko listdir_attr：返回带 filename/st_size 的属性对象列表。"""
        r = self.t.fs("list", path)
        out = []
        for e in r.get("entries", []):
            st = AgentSFTPStat(int(e.get("size") or 0))
            st.filename = e.get("name")
            out.append(st)
        return out

    def put(self, local_path: str, remote_path: str):
        self.t.file_put(local_path, remote_path)

    def get(self, remote_path: str, local_path: str):
        self.t.file_get(remote_path, local_path)

    def close(self):
        pass


class AgentSSHClient:
    """paramiko SSHClient 兼容外壳。"""

    def __init__(self, transport: AgentTransport):
        self.t = transport

    def get_transport(self):
        return AgentPTransport(self.t)

    def open_sftp(self):
        return AgentSFTP(self.t)

    def close(self):
        self.t._reset()


def connect_agent(host: str, port: int, token: str,
                  timeout: int = 30) -> AgentSSHClient:
    """建立 Agent 通道（健康检查 + 能力自报）。"""
    t = AgentTransport(host, port, token, timeout=timeout)
    t.capabilities()  # 探活 + 记录 enc 能力
    return AgentSSHClient(t)
