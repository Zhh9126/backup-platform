# -*- coding: utf-8 -*-
"""
文件/目录备份引擎 — 支持本地与远程(SSH, 无Agent)两种源。

特性：
- 全量备份：tar.gz 打包源路径下全部文件
- 增量备份：对比源/目标文件列表（size + mtime，容差 5s），仅传输变化/新增文件
- 远程主机：通过 paramiko SSH 执行 find/tar，无需在被备份机器上安装任何 Agent
- 恢复：将 tar.gz 归档解包到指定目标目录

extra_options JSON 结构示例：
{
  "source_type": "local" | "remote",
  "source_paths": ["/data/app", "/etc/nginx"],
  "source_host": "root@192.168.1.100",   // source_type=remote 时必填，指向 ssh_hosts 中的 host_key
  "target_type": "local" | "remote",
  "target_host": "root@192.168.1.200",   // target_type=remote 时必填
  "target_path": "/backup/file_tasks",      // 本地目标目录或远程目标目录
  "exclude_patterns": ["*.tmp", "*.log", "__pycache__"],
  "follow_symlinks": false
}
"""

import io
import os
import json
import shlex
import fnmatch
import time
import tarfile
import tempfile
import threading
import subprocess
import shutil
import hashlib
from typing import List, Dict, Tuple, Optional

import config
import core.db as db


def _safe_write_bytes(archive_path: str, data: bytes, logger=None, retries: int = 3) -> None:
    """安全写入字节：先删旧文件防 Windows 文件锁，重试 3 次应对偶发占用。
    Windows 上将路径归一化为正斜杠以避免反斜杠转义歧义。"""
    # 路径归一化：Windows 反斜杠 → 正斜杠（避免 \t \r 等被误解析为转义符）
    norm_path = archive_path.replace("\\", "/")
    parent = os.path.dirname(norm_path)
    if logger:
        logger.info("[safe_write] 写入路径: %r (norm=%r) 数据大小: %d bytes", archive_path, norm_path, len(data))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(norm_path):
        try:
            os.unlink(norm_path)
            if logger: logger.info("[safe_write] 已删除旧文件")
        except Exception as e:
            if logger:
                logger.warning("[safe_write] 删除旧文件失败（忽略）: %s", e)
    last_err = None
    for attempt in range(retries):
        # 同时尝试正斜杠和反斜杠两种写法（应对 Windows 路径解析差异）
        for path_try in [norm_path, archive_path]:
            try:
                with open(path_try, "wb") as f:
                    f.write(data)
                if logger: logger.info("[safe_write] 写入成功 (path=%s)", path_try)
                return
            except (PermissionError, OSError) as e:
                last_err = e
                if logger:
                    logger.warning("[safe_write] 第 %d 次写入失败 path=%r err=%r", attempt + 1, path_try, e)
        if attempt < retries - 1:
            time.sleep(0.5)
    raise RuntimeError(f"写入归档失败(已重试{retries}次): {last_err}")


def _safe_write_via_temp(archive_path: str, data: bytes, logger=None) -> bool:
    """终极方案：写入临时文件后 os.replace 原子重命名（避开 Windows 句柄问题）。"""
    try:
        import tempfile
        # 在同一目录下创建临时文件
        norm_path = archive_path.replace("\\", "/")
        parent = os.path.dirname(norm_path) or "."
        # 使用 NamedTemporaryFile 写完后用 os.replace 原子改名
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            if os.path.exists(archive_path):
                os.unlink(archive_path)
            # 原子替换（Windows 友好）
            os.replace(tmp_path, archive_path)
            if logger: logger.info("[safe_write_via_temp] 写入成功 via temp")
            return True
        except Exception as e:
            try: os.unlink(tmp_path)
            except: pass
            if logger: logger.error("[safe_write_via_temp] failed: %r", e)
            return False
    except Exception as e:
        if logger: logger.error("[safe_write_via_temp] outer failed: %r", e)
        return False
from core.engines.base import (
    BackupEngine, BackupType, BackupStatus, BackupResult,
)

# mtime 容差秒数（避免文件系统精度差异导致误判）
MTIME_TOLERANCE = 5

# 全局 SSH 连接池（线程安全）
_ssh_pool: Dict[str, object] = {}
_ssh_lock = threading.Lock()


def _get_extra(task: dict) -> dict:
    """从 task['extra_options'] 解析 JSON 配置。"""
    raw = task.get("extra_options") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _get_ssh_client(host_key: str, password: str = None):
    """
    获取或创建 SSH 连接（带连接池复用 + 活性探测）。
    host_key 格式: "user@hostname:port" 或 "user@hostname"
    密码优先从参数取，其次从 ssh_hosts 表查询。

    背景：长时间空闲后，中间防火墙/NAT 会静默断开 TCP，但 paramiko 的
    transport.is_active() 仍可能返回 True，导致复用死连接时 exec_command
    抛出 WinError 10060 等连接超时。本函数在复用前通过轻量 heartbeat
    探测连接是否真正可用，不可用则丢弃重建。
    """
    import paramiko

    with _ssh_lock:
        if host_key in _ssh_pool:
            client = _ssh_pool[host_key]
            t = None
            try:
                t = client.get_transport()
            except Exception:
                pass
            alive = False
            if t is not None and t.is_active():
                try:
                    # 轻量心跳：发一个 IGNORE 包，若底层连接已死会立即抛异常
                    t.send_ignore()
                    alive = True
                except Exception:
                    alive = False
            if alive:
                return client
            # 不可用则移出连接池，后续重建
            try:
                if t is not None:
                    t.close()
                client.close()
            except Exception:
                pass
            _ssh_pool.pop(host_key, None)

    # 解析 host_key
    if ":" in host_key and not host_key.startswith("["):
        parts = host_key.rsplit(":", 1)
        addr = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            port = 22
    else:
        addr = host_key
        port = 22

    if "@" in addr:
        user, hostname = addr.split("@", 1)
    else:
        user, hostname = "root", addr

    # 未传入密码则从 DB 查询
    priv_key = ""
    if not password:
        row = db.query_one(
            "SELECT password, auth_type, private_key FROM ssh_hosts "
            "WHERE host_key=? LIMIT 1",
            (host_key,),
        )
        if row:
            password = db.decrypt_secret(row["password"] or "")
            try:   # 兼容历史表缺列的场景
                priv_key = (row["private_key"] or "").strip()
            except Exception:
                priv_key = ""
        key_file = priv_key if priv_key and os.path.isfile(priv_key) else None
    else:
        key_file = None
    # 无口令时才启用 SSH 公钥/agent 认证（如 ~/.ssh/id_rsa、指定私钥文件）。
    # 有口令时保持原行为，避免对既有纳管主机产生任何影响。
    no_password = not password

    with _ssh_lock:
        existing = _ssh_pool.get(host_key)
        # 已有连接但已断开的，丢弃以避免复用死连接
        if existing:
            try:
                t = existing.get_transport()
                if t is None or not t.is_active():
                    _ssh_pool.pop(host_key, None)
            except Exception:
                _ssh_pool.pop(host_key, None)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # 握手抖动重试：sshd 刚启动尚未就绪 / MaxStartups 瞬时限流 / 中间链路丢包，
    # 都会表现为 "Error reading SSH protocol banner"（EOFError 或 SSHException），
    # 属瞬时故障——192.168.220.168 环境 Oracle 11g 实测出现过，直接导致一次
    # 恢复校验被判失败（重跑即通过）。这里短暂退避后重试，避免把抖动暴露成
    # 任务失败。认证失败（AuthenticationException）不是瞬时故障，直接抛出不重试。
    _auth_exc = paramiko.ssh_exception.AuthenticationException
    _ssh_exc = paramiko.ssh_exception.SSHException
    last_exc = None
    for _attempt in (1, 2, 3):
        try:
            client.connect(
                hostname, port=port, username=user,
                password=password or None, timeout=60,
                key_filename=key_file,
                allow_agent=no_password, look_for_keys=no_password,
            )
            last_exc = None
            break
        except _auth_exc:
            raise
        except (_ssh_exc, OSError, EOFError) as _exc:
            last_exc = _exc
            try:
                client.close()
            except Exception:
                pass
            time.sleep(1.5 * _attempt)
    if last_exc is not None:
        raise last_exc
    # 启用 TCP keepalive + SSH keepalive，降低中间 NAT/防火墙静默断连概率
    try:
        t = client.get_transport()
        if t is not None:
            t.set_keepalive(30)
    except Exception:
        pass
    with _ssh_lock:
        _ssh_pool[host_key] = client
    return client


def _remote_log_start(cmd: str, timeout: int, note: str = "",
                      input_len: int = 0) -> float:
    """远端命令开跑：写系统日志 + 操作日志（命令脱敏）。返回起始时间。"""
    import time as _time
    from core.logging_setup import mask_command
    try:
        import logging as _lg
        _lg.getLogger("engine.file").info(
            "SSH 执行%s: %s", (f"（{note}）" if note else ""), mask_command(cmd))
    except Exception:
        pass
    try:
        from core import oplog
        op = oplog.current()
        if op:
            extra_note = note or "远端 SSH 执行"
            if input_len:
                extra_note += f"，stdin {input_len} 字节"
            op.command(cmd, note=extra_note, timeout=timeout)
    except Exception:
        pass
    return _time.time()


def _remote_log_end(rc, out, err, cost: float, binary: bool = False) -> None:
    """远端命令结束：退出码 + stderr 全文 + stdout 摘要写入操作日志。

    stdout 为二进制流（tar 等）时只记录字节数与文本预览，
    避免把二进制垃圾写进日志；stderr 是排查主战场，全文保留。
    """
    try:
        import logging as _lg
        lg = _lg.getLogger("engine.file")
        if rc not in (0,):
            lg.warning("SSH 执行结束 rc=%s 耗时=%.3fs；stderr: %s",
                       rc, cost, (err or "")[-800:])
        else:
            lg.info("SSH 执行结束 rc=%s 耗时=%.3fs，stdout %d 字节",
                    rc, cost, len(out or b""))
    except Exception:
        pass
    try:
        from core import oplog
        op = oplog.current()
        if not op:
            return
        err_text = err or ""
        if binary:
            size = len(out or b"")
            preview = ""
            if 0 < size <= 8192:
                try:
                    cand = (out or b"").decode("utf-8")
                    if cand.isprintable() or "\n" in cand:
                        preview = cand
                except Exception:
                    preview = ""
            out_text = f"（二进制 stdout {size} 字节）"
            if preview:
                out_text += "\n" + preview
            op.output(rc, out_text, err_text, label="远端 SSH 耗时 %.3fs" % cost)
        else:
            out_text = out if isinstance(out, str) else (
                (out or b"").decode("utf-8", "replace"))
            op.output(rc, out_text, err_text, label="远端 SSH 耗时 %.3fs" % cost)
    except Exception:
        pass


def _ssh_exec(client, cmd: str, timeout: int = 30) -> Tuple[str, str, int]:
    """在已连接的 SSH 客户端上执行命令。"""
    t = client.get_transport()
    if t is None or not t.is_active():
        raise RuntimeError("SSH transport dead")
    t0 = _remote_log_start(cmd, timeout, note="SSH 文本命令")
    _, sout, serr = client.exec_command(cmd, timeout=timeout)
    out = sout.read().decode("utf-8", errors="replace")
    err = serr.read().decode("utf-8", errors="replace")
    rc = sout.channel.recv_exit_status()
    import time as _time
    _remote_log_end(rc, out, err, _time.time() - t0)
    return out, err, rc


# --------------------------------------------------------------------------- #
# SSH 数据通道（大流量 dump / 物理备份 tar 的统一实现）
#
# 旧实现在这里踩过三个性能坑，10GB 级备份会慢到几百 KB/s 且必然超时：
#   1) `out += sess.recv(65536)`：bytes 不可变，每轮追加都要把**已有全部数据**
#      重新拷贝一遍（O(n²)）。累积到 1.4GB 时单次追加要 memcpy 1.4GB，
#      有效吞吐直接掉到几百 KB/s——数据量越大越慢，与用户实测完全吻合。
#   2) 每轮最多读 64KB，且无数据时固定 `sleep(0.05)`：等效限速约 1.3MB/s。
#   3) 全量 dump 先在内存里攒成 bytes 再落盘：10GB 数据 → 内存/交换爆掉。
# 现统一为：可流式落盘（内存恒定）+ 256KB 块 + 2ms 空转 + 传输窗口调优。
# --------------------------------------------------------------------------- #

_SSH_CHUNK = 256 * 1024          # 单次 recv 块大小
_SSH_WINDOW_MB = 16              # SSH 传输窗口（paramiko 默认 2MB）
_SSH_PACKET_KB = 128             # SSH 单包上限（paramiko 默认 32KB）


def _tune_ssh_transport(client, window_mb: int = _SSH_WINDOW_MB,
                        packet_kb: int = _SSH_PACKET_KB) -> None:
    """调大 SSH 传输窗口与单包大小，消除大流量下的窗口等待瓶颈。

    paramiko 默认窗口 2MB / 单包 32KB：在高带宽或跨机房（高 RTT）链路上，
    窗口不足会让发送端频繁停下来等 window adjust，实测吞吐被压在个位数
    MB/s。放宽到 16MB / 128KB，由协议协商自动收敛到对端能力。
    """
    try:
        t = client.get_transport()
        if t is None:
            return
        cur_w = int(getattr(t, "default_window_size", 0) or 0)
        cur_p = int(getattr(t, "default_max_packet_size", 0) or 0)
        t.default_window_size = max(cur_w, window_mb * 1024 * 1024)
        t.default_max_packet_size = max(cur_p, packet_kb * 1024)
    except Exception:
        pass


def _ssh_exec_stream(client, cmd: str, *, out_file=None, timeout: int = 0,
                     idle_timeout: int = 0, input_data: bytes = None,
                     note: str = "SSH 数据管道", label: str = "",
                     progress_every: float = 30.0, rate_kbps: int = 0,
                     want_digest: bool = True) -> dict:
    """统一的 SSH 数据通道：执行命令并流式接收 stdout。

    参数：
      out_file      可写文件对象。给定时**边收边写盘**（内存恒定），
                    不给则把 stdout 累积为 bytes 返回（兼容旧调用）。
      timeout       总时长上限（秒）；0 = 不限（大库备份推荐，由任务配置决定）。
      idle_timeout  空闲上限（秒）：连续 N 秒没有任何 stdin/stdout 数据则判失败。
                    0 = 不限。用于区分"大表读取慢"与"真卡死"。
      rate_kbps     平台侧限速（KB/s，0=不限），不依赖远端 pv。
      progress_every 进度日志间隔（秒）。

    返回 dict(out, err, rc, written, cost, sha256, speed_mbps)。
    """
    import time as _time
    t = client.get_transport()
    if not t or not t.is_active():
        raise RuntimeError("SSH transport dead")
    _tune_ssh_transport(client)
    in_len = 0
    if input_data:
        if isinstance(input_data, str):
            input_data = input_data.encode("utf-8")
        in_len = len(input_data)
    t0 = _remote_log_start(cmd, timeout, note=note, input_len=in_len)
    try:
        sess = t.open_session()
        sess.exec_command(cmd)
    except Exception as exc:
        # channel open 被远端拒绝（SSHException "Failure"）：sshd 对同一连接的
        # 并发/累积会话或 MaxStartups 瞬时限流。池化长连接上高频工具探测 +
        # 大流程多组件并发建连时实测触发（168 Oracle 11g E2E）。按对象身份
        # 反查池内 host_key，丢弃旧连接重建后**重试一次**；重试仍失败才抛出。
        if "Failure" not in str(exc):
            raise
        with _ssh_lock:
            _hk = next((k for k, v in _ssh_pool.items() if v is client), None)
            if _hk:
                _ssh_pool.pop(_hk, None)
        try:
            client.close()
        except Exception:
            pass
        if not _hk:
            raise
        client2 = _get_ssh_client(_hk)
        t2 = client2.get_transport()
        if not t2 or not t2.is_active():
            raise
        _tune_ssh_transport(client2)
        sess = t2.open_session()
        sess.exec_command(cmd)
    if input_data:
        sess.sendall(input_data)
        sess.shutdown_write()

    buf = None if out_file is not None else bytearray()
    err = bytearray()
    digest = hashlib.sha256() if want_digest else None
    written = 0
    start = _time.time()
    last_data = start
    last_hb = start
    hb_bytes = 0
    timed_out = ""
    try:
        while True:
            got = False
            if sess.recv_ready():
                chunk = sess.recv(_SSH_CHUNK)
                if chunk:
                    got = True
                    if buf is None:
                        out_file.write(chunk)
                    else:
                        buf += chunk
                    written += len(chunk)
                    if digest is not None:
                        digest.update(chunk)
                    last_data = _time.time()
                    # 平台侧限速（不依赖远端 pv；按"应耗时"补 sleep）
                    if rate_kbps > 0:
                        expect = written / (rate_kbps * 1024.0)
                        drift = expect - (_time.time() - start)
                        if drift > 0.05:
                            _time.sleep(min(drift, 2.0))
            if sess.recv_stderr_ready():
                _e = sess.recv_stderr(8192)
                if _e:
                    err += _e
                    last_data = _time.time()
            now = _time.time()
            # 进度心跳：让运维在大库备份中能看到"还在动、多快"
            if progress_every and now - last_hb >= progress_every:
                seg = written - hb_bytes
                spd = seg / max(now - last_hb, 0.001) / (1024 * 1024)
                try:
                    import logging as _lg
                    _lg.getLogger("engine.file").info(
                        "SSH 传输进度: 已收 %s, 速率 %.1f MB/s, 耗时 %.0fs%s",
                        db.human_size(written), spd, now - start,
                        f" [{label}]" if label else "")
                    from core import oplog
                    op = oplog.current()
                    if op:
                        op.info("SSH 传输进度: 已收 %s（速率 %.1f MB/s，耗时 %.0fs）%s",
                                db.human_size(written), spd, now - start,
                                f" [{label}]" if label else "")
                except Exception:
                    pass
                last_hb = now
                hb_bytes = written
            if sess.exit_status_ready() and not sess.recv_ready():
                break
            if timeout and (now - start) > timeout:
                timed_out = (f"SSH 命令超时（{timeout}s，已收 {db.human_size(written)}）："
                             f"{cmd[:120]}")
                raise RuntimeError(timed_out)
            if idle_timeout and (now - last_data) > idle_timeout:
                timed_out = (f"SSH 数据流空闲超时（{idle_timeout}s 无任何输出，"
                             f"已收 {db.human_size(written)}）：{cmd[:120]}")
                raise RuntimeError(timed_out)
            if not got:
                _time.sleep(0.002)   # 数据充足时不会走到这里
        # 排空剩余缓冲区（exit-status 已到但通道里可能还有尾巴）
        while sess.recv_ready():
            chunk = sess.recv(_SSH_CHUNK)
            if not chunk:
                break
            if buf is None:
                out_file.write(chunk)
            else:
                buf += chunk
            written += len(chunk)
            if digest is not None:
                digest.update(chunk)
        while sess.recv_stderr_ready():
            err += sess.recv_stderr(8192)
        rc = sess.recv_exit_status()
    except Exception as e:
        try:
            sess.close()
        except Exception:
            pass
        cost = _time.time() - t0
        err_text = err.decode("utf-8", errors="replace")
        try:
            from core import oplog
            op = oplog.current()
            if op:
                op.error("SSH 数据通道异常：已收 %s，耗时 %.0fs，stderr 尾部: %s\n%s",
                         db.human_size(written), cost, err_text[-800:],
                         str(e)[:300])
        except Exception:
            pass
        raise
    try:
        sess.close()
    except Exception:
        pass
    cost = _time.time() - t0
    err_text = err.decode("utf-8", errors="replace")
    speed = written / max(cost, 0.001) / (1024 * 1024)
    if buf is None:
        _remote_log_end(rc, f"（流式落盘 {written} 字节，平均 {speed:.1f} MB/s）",
                        err_text, cost, binary=False)
    else:
        _remote_log_end(rc, bytes(buf), err_text, cost, binary=True)
    return {
        "out": (bytes(buf) if buf is not None else None),
        "err": err_text,
        "rc": rc,
        "written": written,
        "cost": round(cost, 3),
        "sha256": (digest.hexdigest() if digest is not None else ""),
        "speed_mbps": round(speed, 2),
    }


def _ssh_exec_pipe(client, cmd: str, input_data: bytes = None, timeout: int = 600,
                   idle_timeout: int = 0):
    """流式管道执行（用于 tar 数据传输）。stdout 保持原始 bytes 以保真二进制。

    timeout: 最大等待秒数，超时抛 RuntimeError。默认 600 秒（10 分钟）；
             传 0 表示不限总时长（仅由空闲超时兜底），大库备份走任务配置。

    兼容旧签名；内部统一走 _ssh_exec_stream（bytearray 累积，消除旧的
    O(n²) 拷贝与固定 50ms sleep 限速）。**10GB 级产物请用
    _ssh_exec_pipe_to_file 直接落盘**（内存恒定 + 可断点续传）。
    """
    r = _ssh_exec_stream(client, cmd, out_file=None, timeout=timeout,
                         idle_timeout=idle_timeout, input_data=input_data,
                         note="SSH 数据管道")
    return r["out"], r["err"], r["rc"]


def _ssh_exec_pipe_to_file(client, cmd: str, out_path: str, *, timeout: int = 0,
                           idle_timeout: int = 0, input_data: bytes = None,
                           label: str = "", rate_kbps: int = 0,
                           resume: bool = True) -> dict:
    """执行命令并把 stdout **流式写入本地文件**（内存恒定，支持断点续传）。

    断点续传：out_path 已存在且 resume=True 时，从已有字节数继续追加。
    返回 dict(written, offset, rc, err, cost, sha256, speed_mbps)；
    续传场景下 sha256 为空（跨段拼接无法增量计算），由调用方对最终文件算整体哈希。
    """
    offset = 0
    if resume and os.path.exists(out_path):
        try:
            offset = os.path.getsize(out_path)
        except OSError:
            offset = 0
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    f = open(out_path, "ab" if offset else "wb")
    try:
        r = _ssh_exec_stream(
            client, cmd, out_file=f, timeout=timeout, idle_timeout=idle_timeout,
            input_data=input_data, note="SSH 数据落盘", label=label,
            rate_kbps=rate_kbps, want_digest=(offset == 0))
        try:
            f.flush()
            os.fsync(f.fileno())
        except Exception:
            pass
    finally:
        f.close()
    r["offset"] = offset + r["written"]
    r["resume_from"] = offset
    return r


# ---------- 文件列表获取 ----------

def _match_excludes(rel: str, excludes) -> bool:
    """判断相对路径是否命中排除规则。

    规则语义（与前端提示一致）：
      - ``*.log``      —— fnmatch 匹配，``*`` 可跨目录（子目录里的 .log 也会命中）
      - ``__pycache__``—— 不含通配符的名称，路径中**任一段**等于它即命中
    """
    rel = (rel or "").replace("\\", "/")
    for pat in (excludes or []):
        pat = (pat or "").strip().replace("\\", "/")
        if not pat:
            continue
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(os.path.basename(rel), pat):
            return True
        if "/" not in pat and "*" not in pat and "?" not in pat:
            if pat in rel.split("/"):
                return True
    return False


def _get_local_file_list(base_path: str, excludes=None) -> Dict[str, Tuple[int, int]]:
    """获取本地路径的文件列表 {相对路径: (大小, mtime)}。

    base_path 可以是目录（递归）或**单个文件**（返回 {文件名: (大小, mtime)}）。
    excludes 必须与打包时使用同一套规则：快照即"归档里到底有什么"，
    否则会出现"包里没有、快照却记着"的假一致，恢复时静默缺文件。
    """
    result = {}
    if os.path.isfile(base_path):
        if not _match_excludes(os.path.basename(base_path), excludes):
            try:
                st = os.stat(base_path)
                result[os.path.basename(base_path)] = (st.st_size, int(st.st_mtime))
            except OSError:
                pass
        return result
    if not os.path.isdir(base_path):
        return result
    for dirpath, _dirs, files in os.walk(base_path):
        for f in files:
            fpath = os.path.join(dirpath, f)
            rel = os.path.relpath(fpath, base_path).replace("\\", "/")
            if _match_excludes(rel, excludes):
                continue
            try:
                st = os.stat(fpath)
                result[rel] = (st.st_size, int(st.st_mtime))
            except OSError:
                pass
    return result


def _get_remote_file_list(client, remote_path: str, excludes=None
                          ) -> Optional[Dict[str, Tuple[int, int]]]:
    """通过 SSH find 获取远程路径的文件列表（目录递归 / 单文件返回其自身）。"""
    q = shlex.quote(remote_path)
    cmd = (
        f'if [ -d {q} ]; then cd {q} && find . -type f -printf "%p\\t%s\\t%T@\\n" 2>/dev/null; '
        f'elif [ -f {q} ]; then find {q} -maxdepth 0 -type f -printf "%f\\t%s\\t%T@\\n" 2>/dev/null; '
        f'fi; true'
    )
    out, err, rc = _ssh_exec(client, cmd, timeout=30)
    if rc not in (0, 1):
        return None
    result = {}
    for line in (out or "").strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        rel, sz, mt = parts
        rel = rel.lstrip("./")
        if _match_excludes(rel, excludes):
            continue
        try:
            result[rel] = (int(sz), int(float(mt)))
        except (ValueError, TypeError):
            pass
    return result


# ---------- 源路径探测 / 打包命令构造 ----------
#
# 源路径既可能是目录，也可能是**单个文件**（前端是"源路径（每行一个）"）。
# 早先实现一律按目录处理（`cd <路径> && tar -C <路径> -czf - .`）：把文件当目录
# 会得到 `tar: xxx: Not a directory`，而这个错误又被命令里的 `2>/dev/null` 吞掉，
# 最终上报成「源打包失败: 」（冒号后一片空白）——用户完全无法排查。
# 现在的做法：先真实探测每个路径的类型，再按类型生成命令；探不到就明确报错。

SRC_KIND_DIR = "dir"
SRC_KIND_FILE = "file"

_KIND_LABEL = {"dir": "目录", "file": "文件", "other": "特殊文件（不支持）",
               "missing": "不存在"}


def _probe_local_paths(paths: List[str]) -> Dict[str, str]:
    kinds = {}
    for p in paths:
        if os.path.isdir(p):
            kinds[p] = SRC_KIND_DIR
        elif os.path.isfile(p):
            kinds[p] = SRC_KIND_FILE
        elif os.path.exists(p):
            kinds[p] = "other"
        else:
            kinds[p] = "missing"
    return kinds


def _probe_remote_paths(client, paths: List[str]) -> Dict[str, str]:
    """一次 SSH 往返探测所有远程路径的类型（D=目录 F=文件 O=特殊 M=不存在）。"""
    if not paths:
        return {}
    loop = " ".join(shlex.quote(p) for p in paths)
    cmd = (
        "for p in %s; do "
        'if [ -d "$p" ]; then k=D; '
        'elif [ -f "$p" ]; then k=F; '
        'elif [ -e "$p" ]; then k=O; else k=M; fi; '
        "printf '%%s\\t%%s\\n' \"$k\" \"$p\"; done" % loop
    )
    out, err, rc = _ssh_exec(client, cmd, timeout=30)
    if rc != 0 and not (out or "").strip():
        raise RuntimeError(
            "探测源路径失败(rc=%d): %s" % (rc, (err or "").strip() or "无 stderr 输出"))
    mapping = {"D": SRC_KIND_DIR, "F": SRC_KIND_FILE, "O": "other", "M": "missing"}
    kinds = {p: "missing" for p in paths}
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        kinds[parts[1]] = mapping.get(parts[0].strip(), "missing")
    return kinds


def _kind_desc(kinds: Dict[str, str]) -> str:
    return "、".join("%s（%s）" % (p, _KIND_LABEL.get(k, k)) for p, k in kinds.items())


def _tar_fail_msg(stage: str, rc: int, err, cmd: str, extra: str = "") -> str:
    """打包/解包失败的**真实**原因：退出码 + stderr（含磁盘/权限等）+ 实际命令。

    stderr 为空时不再输出空白原因，而是明确说明命令没有给出错误信息，
    避免再次出现「源打包失败: 」这种无法排查的消息。
    """
    e = err or ""
    if isinstance(e, bytes):
        e = e.decode("utf-8", "replace")
    e = e.strip()
    if not e:
        e = ("命令未输出任何错误信息（rc=%d），请检查路径权限、"
             "目标端磁盘空间与 tar 是否可用" % rc)
    msg = "%s失败(rc=%d): %s" % (stage, rc, e[:400])
    if extra:
        msg += " | %s" % extra
    return "%s | 实际命令: %s" % (msg, cmd)


def _build_tar_cmd(pairs, excludes=None) -> str:
    """按路径类型生成远程打包命令（GNU/BSD tar 通用）。

    - 目录：``-C <目录> .``（保持"备份目录内容"的既有语义）
    - 文件：``-C <父目录> <文件名>``（备份文件本身）
    多路径顺序拼接多个 -C。exclude 规则在这里真正生效——此前前端填的排除规则
    从未传给 tar，属于"界面有、实际不生效"的假功能。
    """
    parts = ["tar", "-czf", "-"]
    for ex in (excludes or []):
        ex = (ex or "").strip()
        if ex:
            parts.append("--exclude=%s" % ex)
    for path, kind in pairs:
        p = (path or "").rstrip("/") or "/"
        if kind == SRC_KIND_DIR:
            parts += ["-C", path, "."]
        else:
            parts += ["-C", os.path.dirname(p) or "/", os.path.basename(p)]
    return " ".join(shlex.quote(p) for p in parts)


class FileBackupEngine(BackupEngine):
    db_type = "file"
    display_name = "文件/目录备份"
    required_clients = []  # 标准库 + 可选 paramiko

    def __init__(self, task: dict, storage_root: str, logger=None):
        super().__init__(task, storage_root, logger)
        # 快照命名空间（设计文档 R4）：
        #   ""   —— 普通调度任务，沿用历史布局 file_snapshots/<md5>/
        #   "rt" —— 准 CDP 实时任务，落在 file_snapshots/rt/<md5>/
        # 实时捕获频率远高于普通增量，两者共用基准会互相污染，必须隔离。
        self.snapshot_namespace: str = ""

    @staticmethod
    def _clean_path(path: str) -> str:
        """清理前端可能带上的 '本地 : ' / '远程 : ' 显示前缀，返回干净路径。"""
        if not path:
            return path
        path = path.strip()
        for prefix in ("本地 :", "本地:", "远程 :", "远程:"):
            if path.startswith(prefix):
                path = path[len(prefix):].strip()
                break
        return path

    def _source_paths(self) -> List[str]:
        """返回源路径列表（自动清理显示前缀）。"""
        raw = self.extra.get("source_paths", []) or []
        return [self._clean_path(p) for p in raw if p and self._clean_path(p)]

    def _excludes(self) -> List[str]:
        return self.extra.get("exclude_patterns", [])

    def _parse_target(self) -> dict:
        """解析目标信息。返回 {"type": "local|remote", "path": "...", "host": "..."}"""
        return {
            "type": self.extra.get("target_type", "local"),
            "path": self._clean_path(self.extra.get("target_path", "")),
            "host": self.extra.get("target_host", ""),
        }

    def _parse_source(self) -> dict:
        """解析源信息。"""
        return {
            "type": self.extra.get("source_type", "local"),
            "paths": self._source_paths(),
            "host": self.extra.get("source_host", ""),
        }

    def _resolve_src_pairs(self, src: dict) -> List[Tuple[str, str]]:
        """探测源路径类型（带缓存），返回 [(路径, 'dir'|'file')]。

        探测是**真实执行**的（远程走一次 SSH `-d/-f` 判定），任何路径不存在或类型
        不支持都直接报错：宁可失败得明明白白，也不打出一个空包冒充成功。
        """
        paths = list(src.get("paths") or [])
        src_type = src.get("type")
        key = (src_type, tuple(paths), src.get("host"))
        cache = getattr(self, "_src_pairs_cache", None)
        if cache and cache[0] == key:
            return cache[1]

        where = "备份平台本机" if src_type == "local" else ("主机 %s" % src.get("host"))
        if src_type == "local":
            kinds = _probe_local_paths(paths)
        else:
            kinds = _probe_remote_paths(_get_ssh_client(src["host"]), paths)

        bad = {p: k for p, k in kinds.items() if k in ("missing", "other")}
        if bad:
            raise RuntimeError(
                "%s 上不可用的源路径: %s。请修正任务里的源路径"
                "（每行一个，目录或单个文件均可）" % (where, _kind_desc(bad)))

        pairs = [(p, kinds[p]) for p in paths]
        self.logger.info("[%s] 源路径判定(%s): %s", self.task_name, where,
                         "，".join("%s=%s" % (p, k) for p, k in pairs))
        self._src_pairs_cache = (key, pairs)
        return pairs

    # ---------------- 备份主流程 ----------------

    def backup(self, backup_type: BackupType) -> BackupResult:
        if self.task.get("demo_only"):
            return self._simulate_backup(backup_type, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_backup(backup_type, "DEMO_MODE=on 强制仿真")

        src = self._parse_source()
        dst = self._parse_target()

        t0 = time.time()

        # 打包/传输过程中的异常统一收敛成"失败结果 + 真实原因"：
        # 抛到上层只会显示成笼统的"执行异常"，而真实原因（tar 的 stderr、
        # 磁盘写满、权限拒绝等）恰恰是用户唯一能据以排查的信息。
        try:
            if backup_type == BackupType.INCREMENTAL:
                result = self._incremental_transfer(src, dst)
            else:
                result = self._full_transfer(src, dst)
        except Exception as e:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                duration_sec=round(time.time() - t0, 2),
                message="文件备份失败: %s" % (str(e) or type(e).__name__))

        duration = round(time.time() - t0, 2)
        result.duration_sec = duration

        # 镜像备份模式（对标 AXUS「镜像备份/快速恢复/紧急使用」）：
        # 在既有打包+快照链路之上，把产物解包为与源一致的目录树，并合并到
        # mirror_latest 视图（可直接复制/挂载即恢复）。开关失败安全。
        if (self.extra.get("file_mirror") and result.success
                and getattr(result, "backup_path", None)
                and os.path.isfile(result.backup_path)):
            try:
                _dst = self._parse_target()
                _out_dir = (_dst.get("path")
                            if _dst.get("type") == "local" and _dst.get("path")
                            else self._output_dir())
                result = self._mirror_postprocess(result, _out_dir, backup_type)
            except Exception as me:  # noqa: BLE001 —— 镜像化失败保留原 tar 产物
                self.logger.warning("[%s] 镜像化失败(保留 tar 产物): %s",
                                    self.task_name, me)

        # 落盘后统一后处理：存储池加密(§2.6) + 全局重删(§2.4)，均失败安全
        self._post_process(result)
        return result

    def _mirror_postprocess(self, result: BackupResult, out_dir: str,
                            backup_type: BackupType) -> BackupResult:
        """把 tar 产物镜像化为目录树（源结构一致）并合并 mirror_latest。

        - 全量：解包到 mirror_full_<ts>/；增量：解包到 mirror_inc_<ts>/
          （仅变化文件，相对路径与源一致——复制该目录覆盖回源即增量恢复）；
        - mirror_latest/：始终维护与源一致的最新视图（增量应用 .deleted.txt），
          供「紧急挂载直接使用」；
        - 产物 backup_path 改指本次镜像目录；message 附 latest 路径。
        """
        kind = "full" if backup_type != BackupType.INCREMENTAL else "inc"
        ts = self._timestamp()
        out_dir = out_dir or self._output_dir()
        mirror_dir = os.path.join(out_dir, f"mirror_{kind}_{ts}")
        os.makedirs(mirror_dir, exist_ok=True)
        # 产物可能已被二次压缩（.tar.gz.zst，zstd 管道压缩），先还原为 tar
        src_arch = result.backup_path
        tmp_tar = None
        try:
            if src_arch.endswith(".zst"):
                import zstandard
                tmp_tar = src_arch + ".tmp.tar"
                dctx = zstandard.ZstdDecompressor()
                with open(src_arch, "rb") as fi, open(tmp_tar, "wb") as fo:
                    dctx.copy_stream(fi, fo)
                src_arch = tmp_tar
            with tarfile.open(src_arch, "r:*") as tf:
                tf.extractall(mirror_dir)  # noqa: S202 —— 平台自产归档，非不可信输入
        finally:
            if tmp_tar and os.path.exists(tmp_tar):
                try:
                    os.remove(tmp_tar)
                except OSError:
                    pass
        # 计算镜像树大小与文件数
        n_files, total = 0, 0
        for root, _dirs, files in os.walk(mirror_dir):
            for f in files:
                p = os.path.join(root, f)
                try:
                    total += os.path.getsize(p)
                    n_files += 1
                except OSError:
                    pass

        # 合并到 latest 视图
        latest = os.path.join(out_dir, "mirror_latest")
        os.makedirs(latest, exist_ok=True)
        for root, _dirs, files in os.walk(mirror_dir):
            rel = os.path.relpath(root, mirror_dir)
            dst_root = latest if rel == "." else os.path.join(latest, rel)
            os.makedirs(dst_root, exist_ok=True)
            for f in files:
                if f == ".deleted.txt":
                    continue
                srcp = os.path.join(root, f)
                shutil.copy2(srcp, os.path.join(dst_root, f))
        # 增量：应用删除清单，保持 latest 与源一致
        del_man = os.path.join(mirror_dir, ".deleted.txt")
        if os.path.isfile(del_man):
            with open(del_man, encoding="utf-8", errors="replace") as df:
                for line in df:
                    relp = line.strip().lstrip("/")
                    if not relp:
                        continue
                    victim = os.path.join(latest, relp)
                    if os.path.isfile(victim):
                        try:
                            os.remove(victim)
                        except OSError:
                            pass

        result.backup_path = mirror_dir
        result.size_bytes = total
        result.checksum = ""
        base_msg = (result.message or "").split(" | ")[0]
        result.message = (f"{base_msg} | 镜像模式: {n_files} 文件 "
                          f"{db.human_size(total)}（目录树与源一致，可复制/挂载即恢复）"
                          f" | 最新视图: {latest}")
        self.logger.info("[%s] 镜像化完成: %s（latest=%s）",
                         self.task_name, mirror_dir, latest)
        return result

    def _post_process(self, result: BackupResult) -> None:
        """落盘后统一后处理：存储池加密 → 全局重删。任一环节失败不影响主流程。"""
        self._apply_pool_encryption(result)
        self._apply_global_dedup(result)

    def _apply_pool_encryption(self, result: BackupResult) -> None:
        """存储池加密（鼎甲迪备 §2.6 备份数据加密 / 防泄露）。

        仅当任务开启 encrypt_pool 且配置了主密钥(BACKUP_POOL_KEY)时加密；
        缺密钥或缺库则跳过（明文落盘并告警），绝不阻断备份。
        """
        try:
            if not self.extra.get("encrypt_pool"):
                return
            from core import crypto_pool as cp
            path = getattr(result, "backup_path", None)
            if not path or not isinstance(path, str) or not os.path.isfile(path):
                return
            if cp.is_encrypted(path):
                return  # 已加密，避免重复
            if not os.path.abspath(path).startswith(os.path.abspath(self.storage_root)):
                return
            r = cp.encrypt_file(path)
            if r.get("encrypted"):
                self.logger.info("[%s] 存储池加密完成: %s",
                                 self.task_name, db.human_size(r["encrypted_bytes"]))
                if result.message:
                    result.message += " | 已加密存储"
            else:
                self.logger.warning("[%s] 存储池加密跳过: %s",
                                    self.task_name, r.get("reason"))
        except Exception as e:  # 加密失败不影响备份主流程
            self.logger.warning("[%s] 存储池加密跳过: %s", self.task_name, e)

    def _apply_global_dedup(self, result: BackupResult) -> None:
        """对本次备份产物做全局切片重删（非阻塞、失败安全）。"""
        try:
            from core import global_dedup as gd
            path = getattr(result, "backup_path", None)
            if not path or not isinstance(path, str) or not os.path.isfile(path):
                return
            # 仅对本地产物重删；远端产物不在本机落盘，跳过
            if not os.path.abspath(path).startswith(os.path.abspath(self.storage_root)):
                return
            res = gd.dedup_file(path, task_id=self.task.get("id"), set_id=None)
            saved = int(res.get("saved_bytes") or 0)
            if saved > 0:
                # 把重删节省追加到结果提示（BackupResult 无 extra 字段，安全附加）
                self.logger.info("[%s] 全局重删节省 %s 字节",
                                 self.task_name, db.human_size(saved))
                if result.message:
                    result.message += f" | 全局重删节省 {db.human_size(saved)}"
        except Exception as e:  # 重删失败不影响备份主流程
            self.logger.warning("[%s] 全局重删跳过: %s", self.task_name, e)

    def _full_transfer(self, src: dict, dst: dict) -> BackupResult:
        """全量打包传输（原子写入，避免 Windows 防病毒/句柄锁导致空文件）。"""
        self.logger.info("[%s] 文件全量备份: %s -> %s", self.task_name, src, dst)

        src_type = src["type"]
        dst_type = dst["type"]
        paths = src["paths"]

        if not paths:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="未配置源路径(source_paths)",
            )

        # 先真实探测每个源路径的类型（目录/文件/不存在），再决定打包方式。
        # 绝不在"路径不存在"时默默打出空包——那会让备份显示成功却什么都没备份到。
        try:
            pairs = self._resolve_src_pairs(src)
        except RuntimeError as e:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="源路径检查未通过: %s" % (str(e) or type(e).__name__),
            )

        # 目标为本地且指定了路径时，归档直接落到用户配置的本地目录，方便查找
        if dst_type == "local" and dst.get("path"):
            out_dir = dst["path"]
            self.logger.info("[%s] 使用用户指定本地目标目录: %s", self.task_name, out_dir)
        else:
            out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        ts = self._timestamp()
        archive_name = f"{ts}__{self.task_name}__full.tar.gz"
        archive_path = os.path.join(out_dir, archive_name)

        # 快照必须等于"归档里真实有什么"：用与打包**同一套**排除规则，
        # 否则会出现"包里没有、快照却记着"的假一致，恢复时静默缺文件。
        # （多源任务仍以第一个源作为快照基准，与既有增量语义一致。）
        base = pairs[0][0] if pairs else "/"
        sf = {}
        if src_type == "local":
            sf = _get_local_file_list(base, self._excludes())
        else:
            try:
                client = _get_ssh_client(src["host"])
                sf = _get_remote_file_list(client, base, self._excludes()) or {}
            except Exception as e:
                self.logger.warning("[%s] 获取远程源文件列表失败: %s",
                                    self.task_name, str(e) or type(e).__name__)

        # 根据源/目标组合选择打包器。本地源→本地目标走自管理(先进压缩)的
        # _tar_local；其余组合走原有原子写入包装（系统 tar 压缩）。
        if src_type == "local" and dst_type == "local":
            self._tar_local(paths, archive_path, self._excludes())
        else:
            def _writer(tmp_path: str):
                if src_type == "local" and dst_type == "remote":
                    self._tar_local_to_remote(paths, dst, tmp_path)
                elif src_type == "remote" and dst_type == "local":
                    self._tar_remote_to_local(src, tmp_path)
                elif src_type == "remote" and dst_type == "remote":
                    self._tar_remote_to_remote(src, dst, tmp_path)
                else:
                    raise ValueError(f"不支持的源/目标组合: {src_type}->{dst_type}")
            self._atomic_write_archive(_writer, archive_path)

        final = self._final_archive_path(archive_path)
        size = os.path.getsize(final) if os.path.exists(final) else 0
        original = self._read_original_size(final)
        ratio = round(size / original, 6) if (original and size) else 0.0
        algo = self._resolve_compress_algo()
        checksum = db.sha256_file(final) if size > 0 else ""
        # 保存快照作为下次增量基准，并记录本次全量归档路径供恢复链使用
        self._save_snapshot(sf, full_path=final)

        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=final, size_bytes=size,
            original_size_bytes=original, compress_algo=algo, compress_ratio=ratio,
            checksum=checksum,
            message=f"全量备份完成 | {len(paths)} 个源 | {db.human_size(size)}",
        )

    # ---------- 增量快照 ----------

    def _snapshot_path(self, namespace: str = None) -> str:
        """返回该任务文件状态快照的存储路径（按源配置哈希，供同一源的多任务共享基准）。

        Args:
            namespace: 快照命名空间。``None`` 表示取实例默认
                :attr:`snapshot_namespace`；空串沿用历史布局
                ``file_snapshots/<md5>/``；非空（如 ``"rt"``）则落到
                ``file_snapshots/rt/<md5>/``，实现实时任务与普通任务的基准隔离。

        Returns:
            snapshot.json 的绝对路径（父目录已创建）。
        """
        ns = self.snapshot_namespace if namespace is None else (namespace or "")
        src_cfg = self._parse_source()
        key = json.dumps(src_cfg, sort_keys=True, ensure_ascii=False)
        h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
        parts = [self.storage_root, "file_snapshots"]
        if ns:
            parts.append(str(ns))
        parts.append(h)
        base = os.path.join(*parts)
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "snapshot.json")

    def _load_snapshot(self, namespace: str = None) -> Optional[Dict[str, Tuple[int, int]]]:
        """加载上次成功备份后保存的文件快照（兼容旧格式）。"""
        path = self._snapshot_path(namespace)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            # 新格式 {"files": {...}, "last_full_path": "..."}
            files = data.get("files") if isinstance(data.get("files"), dict) else data
            if not isinstance(files, dict):
                return None
            return {k: (int(v[0]), int(v[1])) for k, v in files.items()}
        except Exception as e:
            self.logger.warning("[%s] 加载快照失败: %s", self.task_name, e)
            return None

    def _load_snapshot_meta(self, namespace: str = None) -> dict:
        """加载快照完整元数据，包括 last_full_path。"""
        path = self._snapshot_path(namespace)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            if "files" in data:
                return data
            # 旧格式：只有 files，没有 meta
            return {"files": data}
        except Exception as e:
            self.logger.warning("[%s] 加载快照元数据失败: %s", self.task_name, e)
            return {}

    def _save_snapshot(self, snapshot: Dict[str, Tuple[int, int]], full_path: str = None,
                       namespace: str = None) -> None:
        """保存当前源文件状态快照，并在全量备份时记录对应的全量归档路径。"""
        path = self._snapshot_path(namespace)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            meta = self._load_snapshot_meta(namespace)
            meta["files"] = snapshot
            # 记下本次快照使用的排除规则：下次增量据此判断"规则是否变化"，
            # 避免把规则变化误当成大量文件被删除。
            meta["excludes"] = [str(x) for x in (self._excludes() or [])]
            if full_path:
                meta["last_full_path"] = full_path
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        except Exception as e:
            self.logger.warning("[%s] 保存快照失败: %s", self.task_name, e)

    def _diff_against_snapshot(self, sf: Dict[str, Tuple[int, int]], snapshot: Dict[str, Tuple[int, int]]) -> Tuple[List[str], List[str]]:
        """对比当前源文件列表与快照，返回 (changed, deleted)。"""
        changed, deleted = [], []
        for rel, (sz, mt) in sf.items():
            if rel not in snapshot:
                changed.append(rel)
            else:
                ssz, smt = snapshot[rel]
                if ssz != sz or abs(mt - smt) > MTIME_TOLERANCE:
                    changed.append(rel)
        for rel in snapshot:
            if rel not in sf:
                deleted.append(rel)
        return changed, deleted

    # ---------- 增量备份主流程 ----------

    def _incremental_transfer(self, src: dict, dst: dict) -> BackupResult:
        """增量备份：基于上次快照仅打包变化文件，不污染用户目标目录。"""
        self.logger.info("[%s] 文件增量备份: %s -> %s", self.task_name, src, dst)

        src_type = src["type"]
        dst_type = dst["type"]

        # 源路径真实探测（不存在/类型不支持直接失败，不做"空包成功"）
        try:
            pairs = self._resolve_src_pairs(src)
        except RuntimeError as e:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="源路径检查未通过: %s" % (str(e) or type(e).__name__))
        base = pairs[0][0] if pairs else "/"
        ex = self._excludes()

        # 1) 获取源文件列表（与打包同一套排除规则，保证"快照 == 归档里有什么"）
        if src_type == "local":
            sf = _get_local_file_list(base, ex)
        else:
            client = _get_ssh_client(src["host"])
            sf = _get_remote_file_list(client, base, ex)
            if sf is None:
                self.logger.warning("[%s] 远程源列表获取失败，回退全量", self.task_name)
                return self._full_transfer(src, dst)

        # 2) 加载上次快照作为基准（无快照则回退全量）
        snapshot = self._load_snapshot()
        if not snapshot:
            self.logger.info("[%s] 无历史快照，回退到全量备份", self.task_name)
            return self._full_transfer(src, dst)

        # 3) 计算差异
        changed, deleted = self._diff_against_snapshot(sf, snapshot)

        # 排除规则一旦变化（含"旧快照还没有排除规则"），旧快照里那些被排除的文件
        # 会被 diff 误判成"已删除"，进而写进 .deleted.txt 在恢复时真被删掉。
        # 规则变化不是文件删除：此处只按新规则重建基准，不做删除判定。
        old_ex = [str(x) for x in (self._load_snapshot_meta().get("excludes") or [])]
        if old_ex != [str(x) for x in (ex or [])]:
            if deleted:
                self.logger.warning(
                    "[%s] 排除规则已变化（旧=%s 新=%s），本次不判定删除文件"
                    "（原本会误判 %d 个），仅按新规则重建快照基准",
                    self.task_name, old_ex or "无", ex or "无", len(deleted))
            deleted = []
        self.logger.info(
            "[%s] 增量对比: 总计=%d 变化=%d 删除=%d",
            self.task_name, len(sf), len(changed), len(deleted),
        )

        if not changed and not deleted:
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                message="无变化文件，跳过传输",
            )

        # 4) 确定归档目录：本地目标与全量保持一致，直接放到用户配置的目标目录根下
        if dst_type == "local" and dst.get("path"):
            out_dir = dst["path"]
            self.logger.info("[%s] 使用用户指定本地目标目录: %s", self.task_name, out_dir)
        else:
            out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        ts = self._timestamp()
        archive_name = f"{ts}__{self.task_name}__inc.tar.gz"
        archive_path = os.path.join(out_dir, archive_name)

        # 5) 生成仅含变化文件的增量归档（原子写入）。
        #    删除清单以 .deleted.txt 写入归档根部，恢复/合成时据此应用删除。
        try:
            if src_type == "local":
                self._tar_files_with_deleted(base, changed, deleted, archive_path)
            else:
                self._tar_remote_files(changed, src, archive_path)
                self._append_deleted_manifest(archive_path, deleted)

            # 远程目标时，把生成的归档也推送过去
            if dst_type == "remote":
                self._upload_file_to_remote(self._final_archive_path(archive_path), dst)
        except Exception as e:
            self.logger.error("[%s] 增量传输异常: %s", self.task_name, e)
            return BackupResult(success=False, status=BackupStatus.FAILED, message=str(e))

        # 6) 保存新快照（作为下次增量基准）
        self._save_snapshot(sf)

        final = self._final_archive_path(archive_path)
        size = os.path.getsize(final) if os.path.exists(final) else 0
        original = self._read_original_size(final)
        ratio = round(size / original, 6) if (original and size) else 0.0
        algo = self._resolve_compress_algo()
        checksum = db.sha256_file(final) if size > 0 else ""
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=final, size_bytes=size,
            original_size_bytes=original, compress_algo=algo, compress_ratio=ratio,
            checksum=checksum,
            message=f"增量备份完成 | 变化={len(changed)} 删除={len(deleted)} | {db.human_size(size)}",
        )

    # ---------- 准 CDP 实时捕获复用入口（core/rt_backup/file_rt.py 调用） ----------

    def list_source_files(self) -> Optional[Dict[str, Tuple[int, int]]]:
        """列出源根目录当前文件状态 ``{相对路径: (大小, mtime)}``。

        本地源恒返回字典（目录不存在时为空字典）；远程源在 SSH 失败时返回
        ``None``，由调用方判定为「本轮扫描不可信」而不是「文件全被删了」。
        """
        src = self._parse_source()
        paths = src.get("paths") or []
        base = paths[0] if paths else ""
        if not base:
            return {}
        if src.get("type") == "local":
            return _get_local_file_list(base)
        try:
            client = _get_ssh_client(src.get("host", ""))
        except Exception as e:
            self.logger.warning("[%s] 连接远程源失败: %s", self.task_name, e)
            return None
        try:
            return _get_remote_file_list(client, base)
        except Exception as e:
            self.logger.warning("[%s] 获取远程源文件列表失败: %s", self.task_name, e)
            return None

    def has_base_snapshot(self) -> bool:
        """当前命名空间下是否已存在可用的增量基准（快照 + 全量归档）。"""
        meta = self._load_snapshot_meta()
        if not isinstance(meta.get("files"), dict) or not meta["files"]:
            # 空目录也算有效基准，只要 last_full_path 归档还在
            if "files" not in meta:
                return False
        full_path = meta.get("last_full_path") or ""
        return bool(full_path) and os.path.exists(full_path)

    def ensure_base_full(self, out_dir: str = "", force: bool = False) -> BackupResult:
        """确保存在基准全量：无快照/无全量归档时立即做一次全量。

        Args:
            out_dir: 基准归档落盘目录。实时任务传 ``LogRepository.base_dir()``，
                使基准与增量同处实时仓库、便于统一 prune 与上云；
                留空则沿用任务自身的目标目录（与普通全量一致）。
            force: 忽略已有基准强制重做（用于「重建基准」运维动作）。

        Returns:
            BackupResult。已存在基准且未 force 时返回 ``success=True`` 且
            ``message`` 标注「复用」，``backup_path`` 指向既有全量归档。
        """
        meta = self._load_snapshot_meta()
        existing_full = meta.get("last_full_path") or ""
        if not force and existing_full and os.path.exists(existing_full) \
                and isinstance(meta.get("files"), dict):
            size = os.path.getsize(existing_full)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=existing_full, size_bytes=size,
                checksum=meta.get("last_full_checksum", ""),
                message=f"复用既有基准全量 | {db.human_size(size)}",
            )

        src = self._parse_source()
        if not src.get("paths"):
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="未配置源路径(source_paths)",
            )
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            dst = {"type": "local", "path": out_dir, "host": ""}
        else:
            dst = self._parse_target()

        t0 = time.time()
        result = self._full_transfer(src, dst)
        result.duration_sec = round(time.time() - t0, 2)
        # _full_transfer 内部已 _save_snapshot(sf, full_path=...)，此处补记校验和
        if result.success and result.checksum:
            try:
                snap_meta = self._load_snapshot_meta()
                snap_meta["last_full_checksum"] = result.checksum
                path = self._snapshot_path()
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(snap_meta, f, ensure_ascii=False)
            except Exception as e:
                self.logger.warning("[%s] 记录基准校验和失败（已忽略）: %s", self.task_name, e)
        return result

    def capture_increment(self, out_dir: str = "", tag: str = "",
                          changed: List[str] = None, deleted: List[str] = None,
                          source_files: Dict[str, Tuple[int, int]] = None) -> BackupResult:
        """执行一次增量捕获：仅打包变化文件到 ``out_dir``，并提交新基准快照。

        与 :meth:`_incremental_transfer` 共用同一套 diff / 打包 / 快照语义，
        差别仅在于：**不写用户目标目录、不做远程推送**——实时增量一律先落
        本地实时仓库，再由三级存储异步上云。

        Args:
            out_dir: 增量归档输出目录，缺省 ``self._output_dir()``。
            tag: 归档名时间戳前缀，缺省当前时刻。
            changed: 预先算好的变更相对路径列表（来自 Watcher，避免二次扫描）。
            deleted: 预先算好的删除相对路径列表。
            source_files: 预先扫描好的源文件状态，用于提交新快照。

        Returns:
            BackupResult。无变化时 ``success=True`` 且 ``backup_path=""``。
        """
        src = self._parse_source()
        paths = src.get("paths") or []
        if not paths:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="未配置源路径(source_paths)")
        base = paths[0]

        # 1) 源文件状态：优先用 Watcher 传入的扫描结果
        sf = source_files
        if sf is None:
            sf = self.list_source_files()
        if sf is None:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="源文件列表获取失败（远程不可达），本轮跳过")

        # 2) 基准快照
        snapshot = self._load_snapshot()
        if snapshot is None:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="无基准快照，需先执行 ensure_base_full()")

        # 3) 差异（Watcher 已算过则直接采用，保证与其上报的批次一致）
        if changed is None or deleted is None:
            changed, deleted = self._diff_against_snapshot(sf, snapshot)
        changed = list(changed or [])
        deleted = list(deleted or [])

        if not changed and not deleted:
            return BackupResult(success=True, status=BackupStatus.SUCCESS,
                                backup_path="", size_bytes=0,
                                message="无变化文件，跳过传输")

        # 4) 归档
        out_dir = out_dir or self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        ts = tag or self._timestamp()
        archive_path = os.path.join(out_dir, f"{ts}__{self.task_name}__inc.tar.gz")

        # 全部是删除时没有实体文件可打包，仍生成一个空 tar 以承载「删除」这一事实，
        # 保证恢复链上每个恢复点都有可校验的产物（R9 要求非空，故写入清单条目）。
        try:
            if changed:
                if src.get("type") == "local":
                    self._tar_files_with_deleted(base, changed, deleted, archive_path)
                else:
                    self._tar_remote_files(changed, src, archive_path)
                    self._append_deleted_manifest(archive_path, deleted)
            else:
                self._tar_manifest_only(archive_path, deleted)
        except Exception as e:
            self.logger.error("[%s] 实时增量打包失败: %s", self.task_name, e)
            return BackupResult(success=False, status=BackupStatus.FAILED, message=str(e))

        final = self._final_archive_path(archive_path)
        size = os.path.getsize(final) if os.path.exists(final) else 0
        if size <= 0:
            # 空包绝不入 journal（R9），直接清理并按失败上报
            try:
                if os.path.exists(final):
                    os.unlink(final)
            except OSError:
                pass
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="增量归档为空，已丢弃")

        # 5) 提交新基准（打包成功后才提交，失败时保持旧基准以便下轮重试）
        self._save_snapshot(sf)

        original = self._read_original_size(final)
        ratio = round(size / original, 6) if (original and size) else 0.0
        algo = self._resolve_compress_algo()
        return BackupResult(
            success=True, status=BackupStatus.SUCCESS,
            backup_path=final, size_bytes=size,
            original_size_bytes=original, compress_algo=algo, compress_ratio=ratio,
            checksum=db.sha256_file(final),
            message=(f"实时增量完成 | 变化={len(changed)} 删除={len(deleted)} | "
                     f"{db.human_size(size)}"),
        )

    def _tar_manifest_only(self, archive_path: str, deleted: List[str]) -> bool:
        """仅含删除清单的归档（无新增/修改文件时使用）。

        清单以 ``.rt_deleted.txt`` 写入归档根，恢复时被 :meth:`_restore_filter`
        正常释放，供人工核对；不参与文件覆盖，因此不会破坏恢复结果。
        """
        payload = "\n".join((d or "").replace("\\", "/") for d in (deleted or []))
        payload = (payload + "\n").encode("utf-8")

        def _write(tmp_path: str) -> None:
            with tarfile.open(tmp_path, "w:gz") as tf:
                info = tarfile.TarInfo(name=".rt_deleted.txt")
                info.size = len(payload)
                info.mtime = int(time.time())
                tf.addfile(info, io.BytesIO(payload))

        self._atomic_write_archive(_write, archive_path)
        return True

    # ---------------- 各种传输组合实现 ----------------

    def _atomic_write_archive(self, writer, archive_path: str) -> None:
        """原子写入归档：先写到同目录临时文件，完成后 os.replace 替换目标。
        避免 Windows 防病毒/WinRAR 扫描导致目标文件被锁时写出空包。"""
        parent = os.path.dirname(archive_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".tar.gz", dir=parent)
        try:
            os.close(fd)
            writer(tmp_path)
            # 关闭文件句柄后再替换，避免 Windows 占用
            if os.path.exists(archive_path):
                os.unlink(archive_path)
            os.replace(tmp_path, archive_path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
            raise

    def _tar_local(self, paths: List[str], archive_path: str, excludes=None):
        """本地多路径 → 本地归档（先进压缩：zstd，回退 gzip）。

        先以未压缩 tar 写入临时文件，再用 zstd（或 gzip 回退）流式压缩为
        ``<archive>.zst``，同时记录未压缩的 tar 字节数供计算压缩率。
        产物直接落盘到 ``archive_path``（含后缀），self 调用方用
        :meth:`_final_archive_path` 取真实路径。

        paths 中的元素可以是目录（备份目录内容）或**单个文件**（备份文件本身）；
        excludes 在此真正生效（此前前端填了排除规则却永远不会被使用）。
        """
        algo = self._resolve_compress_algo()
        suffix = "" if algo == "none" else (".zst" if algo == "zstd" else ".gz")
        final_path = archive_path + suffix
        parent = os.path.dirname(archive_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_tar = tempfile.mkstemp(prefix=".tmp_", suffix=".tar", dir=parent)
        os.close(fd)

        def _ex_flt(ti):
            # 命中排除规则返回 None = 该条目不写入归档（连目录条目一起跳过）
            if _match_excludes(ti.name.replace("\\", "/"), excludes):
                return None
            return ti

        try:
            single = len(paths) == 1
            common = os.path.commonpath(paths) if len(paths) > 1 else (paths[0] if paths else "/")
            with tarfile.open(tmp_tar, "w") as tf:
                for p in paths:
                    if not os.path.exists(p):
                        # 静默跳过 = 备份成功但少了用户配置的内容（假成功），必须报错
                        raise RuntimeError("本地源路径不存在: %s" % p)
                    if os.path.isfile(p):
                        arc = os.path.basename(p)
                    elif single:
                        arc = "."
                    else:
                        arc = (os.path.relpath(p, common) if common != "/"
                               else os.path.basename(p))
                    tf.add(p, arcname=arc, filter=_ex_flt)
            original = os.path.getsize(tmp_tar)
            if os.path.exists(final_path):
                os.unlink(final_path)
            self._compress_file(tmp_tar, final_path, algo)
            self._stash_original_size(final_path, original)
        finally:
            if os.path.exists(tmp_tar):
                os.unlink(tmp_tar)

    def _tar_files(self, base_path: str, rel_files: List[str], archive_path: str) -> bool:
        """把 base_path 下指定相对路径的文件打包成归档（仅含这些文件，保持相对路径）。

        先进压缩：zstd（回退 gzip）。先未压缩 tar，再压缩并记录原始大小。
        """
        base_path = base_path.replace("\\", "/")
        algo = self._resolve_compress_algo()
        suffix = "" if algo == "none" else (".zst" if algo == "zstd" else ".gz")
        final_path = archive_path + suffix
        parent = os.path.dirname(archive_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_tar = tempfile.mkstemp(prefix=".tmp_", suffix=".tar", dir=parent)
        os.close(fd)
        try:
            with tarfile.open(tmp_tar, "w") as tf:
                for rel in rel_files:
                    rel_norm = rel.replace("\\", "/")
                    full = os.path.join(base_path, rel_norm)
                    if os.path.exists(full) and os.path.isfile(full):
                        tf.add(full, arcname=rel_norm)
            original = os.path.getsize(tmp_tar)
            if os.path.exists(final_path):
                os.unlink(final_path)
            self._compress_file(tmp_tar, final_path, algo)
            self._stash_original_size(final_path, original)
        finally:
            if os.path.exists(tmp_tar):
                os.unlink(tmp_tar)
        return True

    def _tar_remote_files(self, changed: List[str], src: dict, archive_path: str) -> bool:
        """远程源 → 本地增量归档：通过 tar -T 仅打包变化文件（原子写入）。"""
        client = _get_ssh_client(src["host"])
        pairs = self._resolve_src_pairs(src)
        base = pairs[0][0] if pairs else "/"
        ex = self._excludes()
        if pairs and pairs[0][1] == SRC_KIND_FILE:
            # 源就是单个文件：它本身是最小单位，整体打包（无需 -T 列表）
            cmd = _build_tar_cmd(pairs, ex)
            payload = None
        else:
            parts = ["tar", "-czf", "-"]
            for e in (ex or []):
                if (e or "").strip():
                    parts.append("--exclude=%s" % e.strip())
            parts += ["-C", base, "-T", "-"]
            cmd = " ".join(shlex.quote(p) for p in parts)
            payload = "\n".join(changed).encode("utf-8")
        data, err, rc = _ssh_exec_pipe(client, cmd, input_data=payload)
        if rc != 0:
            raise RuntimeError(_tar_fail_msg("远程打包", rc, err, cmd))
        if not _safe_write_via_temp(archive_path, data, self.logger):
            raise RuntimeError("本地写入增量归档失败")
        return True

    # ---------- 删除清单：写入增量归档 / 恢复时应用 ----------

    _MANIFEST_NAMES = (".deleted.txt", ".rt_deleted.txt")

    @staticmethod
    def _add_text_entry(tf, name: str, lines: List[str]) -> None:
        """向打开的 tar 中写入一个文本条目（每行一条）。"""
        payload = "\n".join((ln or "").replace("\\", "/") for ln in lines)
        payload = (payload + "\n").encode("utf-8")
        info = tarfile.TarInfo(name=name)
        info.size = len(payload)
        info.mtime = int(time.time())
        tf.addfile(info, io.BytesIO(payload))

    def _tar_files_with_deleted(self, base_path: str, rel_files: List[str],
                                deleted: List[str], archive_path: str) -> bool:
        """打包变化文件，并把删除清单(.deleted.txt)一并写入归档（本地源路径）。"""
        base_path = base_path.replace("\\", "/")
        algo = self._resolve_compress_algo()
        suffix = "" if algo == "none" else (".zst" if algo == "zstd" else ".gz")
        final_path = archive_path + suffix
        parent = os.path.dirname(archive_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_tar = tempfile.mkstemp(prefix=".tmp_", suffix=".tar", dir=parent)
        os.close(fd)
        try:
            with tarfile.open(tmp_tar, "w") as tf:
                for rel in rel_files:
                    rel_norm = rel.replace("\\", "/")
                    full = os.path.join(base_path, rel_norm)
                    if os.path.exists(full) and os.path.isfile(full):
                        tf.add(full, arcname=rel_norm)
                if deleted:
                    self._add_text_entry(tf, ".deleted.txt", deleted)
            original = os.path.getsize(tmp_tar)
            if os.path.exists(final_path):
                os.unlink(final_path)
            self._compress_file(tmp_tar, final_path, algo)
            self._stash_original_size(final_path, original)
        finally:
            if os.path.exists(tmp_tar):
                os.unlink(tmp_tar)
        return True

    def _append_deleted_manifest(self, archive_path: str, deleted: List[str]) -> None:
        """把删除清单写入 gzip 增量归档（远程源产物固定为 tar.gz）。

        tarfile 不支持 gzip 的追加模式，故采用「重写」：解出原成员后连同
        .deleted.txt 一起重打包，先写临时文件再原子替换。
        """
        if not deleted or not os.path.exists(archive_path):
            return
        parent = os.path.dirname(archive_path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".tar.gz", dir=parent)
        os.close(fd)
        try:
            with tarfile.open(archive_path, "r:*") as src, \
                    tarfile.open(tmp, "w:gz") as dst:
                for m in src.getmembers():
                    if m.name in self._MANIFEST_NAMES:
                        continue
                    f = src.extractfile(m)
                    if f is not None:
                        dst.addfile(m, f)
                    else:
                        dst.addfile(m)
                self._add_text_entry(dst, ".deleted.txt", deleted)
            os.replace(tmp, archive_path)
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
            self.logger.debug("[%s] 追加删除清单失败: %s", self.task_name, e)

    def _read_deleted_manifest(self, archive_path: str) -> List[str]:
        """从增量归档读取删除清单（.deleted.txt / .rt_deleted.txt）。"""
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                for name in self._MANIFEST_NAMES:
                    try:
                        m = tf.getmember(name)
                    except KeyError:
                        continue
                    f = tf.extractfile(m)
                    if not f:
                        continue
                    text = f.read().decode("utf-8", "replace")
                    return [ln.strip() for ln in text.splitlines() if ln.strip()]
        except Exception as e:
            self.logger.debug("[%s] 读取删除清单失败: %s", self.task_name, e)
        return []

    def _apply_deleted(self, target: str, deleted: List[str]) -> None:
        """把删除清单应用到目标目录（文件/空目录/符号链接）。"""
        for rel in deleted or []:
            p = os.path.join(target, rel.replace("\\", "/"))
            try:
                if os.path.islink(p):
                    os.unlink(p)
                elif os.path.isfile(p):
                    os.unlink(p)
                elif os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    continue
                self.logger.info("[%s] 应用增量删除: %s", self.task_name, rel)
            except OSError as e:
                self.logger.warning("[%s] 删除失败 %s: %s", self.task_name, rel, e)

    def _upload_file_to_remote(self, local_path: str, dst: dict):
        """把本地归档上传到远程目标目录。"""
        client = _get_ssh_client(dst["host"])
        remote_dir = dst["path"]
        name = os.path.basename(local_path)
        _ssh_exec(client, f'mkdir -p {shlex.quote(remote_dir)}')
        sftp = client.open_sftp()
        try:
            sftp.put(local_path, f"{remote_dir}/{name}")
        finally:
            sftp.close()

    def _tar_local_to_remote(self, paths: List[str], dst: dict, archive_path: str):
        """本地 → 远程：先打 tar 再通过 SSH pipe 传过去解压。"""
        fd, flist = tempfile.mkstemp(suffix=".txt", text=True)
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(paths) + "\n")
            cmd = ["tar", "-czf", "-"]
            for ex in (self._excludes() or []):
                if (ex or "").strip():
                    cmd.append("--exclude=%s" % ex.strip())
            cmd += ["-C", "/", "-T", flist]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            data, err = proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(_tar_fail_msg(
                    "本地打包", proc.returncode, err, " ".join(cmd)))
            # 写本地存档副本（原子写入）
            if not _safe_write_via_temp(archive_path, data, self.logger):
                raise RuntimeError("本地写入归档副本失败")
            # 传到远程解压
            client = _get_ssh_client(dst["host"])
            dcmd = (f'mkdir -p {shlex.quote(dst["path"])} && '
                    f'tar -C {shlex.quote(dst["path"])} -xzf -')
            _, e, rc = _ssh_exec_pipe(client, dcmd, input_data=data)
            if rc != 0:
                raise RuntimeError(_tar_fail_msg("远程解压", rc, e, dcmd,
                                                 extra="目标: %s:%s" % (dst["host"], dst["path"])))
        finally:
            os.unlink(flist)

    def _tar_remote_to_local(self, src: dict, archive_path: str):
        """远程 → 本地：SSH 端 tar 打包 → **流式落盘**到本地归档。

        全程不把产物读进内存：此前是整包 bytes 回内存，大目录会把平台内存撑爆
        （表现为 MemoryError，而它的 str() 是空字符串，只会留下一个空白原因）。
        """
        self.logger.info("[%s] [1/3] 连接远程主机: %s", self.task_name, src["host"])
        client = _get_ssh_client(src["host"])
        pairs = self._resolve_src_pairs(src)
        cmd = _build_tar_cmd(pairs, self._excludes())

        self.logger.info("[%s] [2/3] 远程打包中: %s", self.task_name, cmd)
        t0 = time.time()
        r = _ssh_exec_pipe_to_file(client, cmd, archive_path, timeout=1800,
                                   label=self.task_name, resume=False)
        elapsed = round(time.time() - t0, 1)
        self.logger.info("[%s] SSH tar 完成, 耗时=%ss, rc=%s, size=%s",
                         self.task_name, elapsed, r.get("rc"),
                         db.human_size(r.get("written") or 0))
        if r.get("rc") != 0:
            try:
                if os.path.exists(archive_path):
                    os.unlink(archive_path)
            except OSError:
                pass
            raise RuntimeError(_tar_fail_msg(
                "远程打包", r.get("rc") or -1, r.get("err"), cmd,
                extra="源主机: %s" % src["host"]))
        self.logger.info("[%s] [3/3] 归档落盘完成: %s", self.task_name, archive_path)

    def _tar_remote_to_remote(self, src: dict, dst: dict, archive_path: str):
        """远程 → 远程：源打包 → 本机临时文件（流式）→ SFTP 上传 → 目标解压。

        两处关键约束：
        1. **不把整包读进内存**（此前 `tar stdout` 全量 bytes + 再整包传目标，
           大目录会撑爆平台内存）；
        2. 本机中转文件放在**归档同目录**而非 /tmp —— /tmp 常是小分区/tmpfs，
           大包会直接写满报 ENOSPC。
        """
        pairs = self._resolve_src_pairs(src)
        cmd = _build_tar_cmd(pairs, self._excludes())
        client_src = _get_ssh_client(src["host"])
        dst_dir = (dst.get("path") or "/").rstrip("/") or "/"

        parent = os.path.dirname(archive_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_arc = tempfile.mkstemp(prefix=".tmp_up_", suffix=".tar.gz", dir=parent)
        os.close(fd)
        remote_tmp = ""
        try:
            # ---- 1) 源端打包，流式落到本机临时文件 ----
            self.logger.info("[%s] [1/4] 源端打包: %s", self.task_name, cmd)
            r = _ssh_exec_pipe_to_file(client_src, cmd, tmp_arc, timeout=1800,
                                       label=self.task_name, resume=False)
            if r.get("rc") != 0:
                raise RuntimeError(_tar_fail_msg(
                    "源打包", r.get("rc") or -1, r.get("err"), cmd,
                    extra="源主机: %s" % src["host"]))
            size = os.path.getsize(tmp_arc)
            self.logger.info("[%s] [2/4] 源端打包完成: %s",
                             self.task_name, db.human_size(size))

            # ---- 2) 上传到目标主机（SFTP 流式，不经内存） ----
            client_dst = _get_ssh_client(dst["host"])
            q_dir = shlex.quote(dst_dir)
            _, e, rc = _ssh_exec(client_dst, "mkdir -p %s" % q_dir, timeout=60)
            if rc != 0:
                raise RuntimeError(_tar_fail_msg(
                    "创建目标目录", rc, e, "mkdir -p %s" % dst_dir,
                    extra="目标主机: %s" % dst["host"]))
            remote_tmp = "%s/.bp_upload_%d_%d.tar.gz" % (dst_dir, int(time.time()),
                                                         os.getpid())
            self.logger.info("[%s] [3/4] 上传到目标主机 %s:%s",
                             self.task_name, dst["host"], remote_tmp)
            sftp = client_dst.open_sftp()
            try:
                with open(tmp_arc, "rb") as fh:
                    sftp.putfo(fh, remote_tmp)
            finally:
                sftp.close()

            # ---- 3) 目标端解压并清理临时包 ----
            xcmd = ("tar -C %s -xzf %s && rm -f %s"
                    % (q_dir, shlex.quote(remote_tmp), shlex.quote(remote_tmp)))
            _, e2, rc2 = _ssh_exec(client_dst, xcmd, timeout=1800)
            if rc2 != 0:
                raise RuntimeError(_tar_fail_msg(
                    "目标解压", rc2, e2, xcmd,
                    extra="目标: %s:%s" % (dst["host"], dst_dir)))
            remote_tmp = ""

            # ---- 4) 本地留档（原子替换，保持既有"本机也存一份"的语义） ----
            if os.path.exists(archive_path):
                os.unlink(archive_path)
            os.replace(tmp_arc, archive_path)
            self.logger.info("[%s] [4/4] 本地留档: %s", self.task_name, archive_path)
        finally:
            try:
                if os.path.exists(tmp_arc):
                    os.unlink(tmp_arc)
            except OSError:
                pass
            if remote_tmp:
                try:
                    _ssh_exec(_get_ssh_client(dst["host"]),
                              "rm -f %s" % shlex.quote(remote_tmp), timeout=30)
                except Exception:
                    pass

    # ---------------- 恢复 ----------------

    def restore(self, backup_path: str, **kwargs) -> BackupResult:
        if self.task.get("demo_only"):
            return self._simulate_restore(backup_path, "任务标记为演示(demo_only)")
        if config.DEMO_MODE == "on":
            return self._simulate_restore(backup_path, "DEMO_MODE=on 强制仿真")

        # 0) 跨主机恢复：按恢复链(全量+增量) SFTP 推送 → 远程依次解压 → 应用删除清单
        target_host_info = kwargs.get("target_host_info")
        if target_host_info:
            target_dir = kwargs.get("target_db") or kwargs.get("target_host") or "/tmp/restore"
            return self._try_file_cross_host_restore(
                backup_path, target_host_info, target_dir, kwargs)

        target = kwargs.get("target_db") or kwargs.get("target_host") or ""
        if not target:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="恢复失败：未指定目标目录（使用 target_db 字段）",
            )

        # 0.5) 镜像备份目录产物：树结构与源一致 → 直接复制回目标即恢复
        #      （AXUS「快速恢复：复制文件夹即恢复」语义；latest 视图亦可整体复制）
        if os.path.isdir(backup_path):
            try:
                os.makedirs(target, exist_ok=True)
                n = 0
                for root, _dirs, files in os.walk(backup_path):
                    rel = os.path.relpath(root, backup_path)
                    dst_root = target if rel == "." else os.path.join(target, rel)
                    os.makedirs(dst_root, exist_ok=True)
                    for f in files:
                        if f == ".deleted.txt":
                            continue
                        shutil.copy2(os.path.join(root, f),
                                     os.path.join(dst_root, f))
                        n += 1
                self.logger.info("[%s] 镜像恢复完成: %s → %s（%d 文件）",
                                 self.task_name, backup_path, target, n)
                return BackupResult(
                    success=True, status=BackupStatus.SUCCESS,
                    backup_path=backup_path,
                    message=f"镜像恢复完成: {n} 个文件已复制到 {target}"
                            f"（目录结构与源一致）",
                )
            except Exception as e:  # noqa: BLE001
                self.logger.error("[%s] 镜像恢复失败: %s", self.task_name, e)
                return BackupResult(
                    success=False, status=BackupStatus.FAILED,
                    message=f"镜像恢复失败: {str(e) or type(e).__name__}")

        # 1) 构建恢复链：增量恢复必须先回全量，再按时间顺序应用增量
        #    PITR 场景由 PITRRestore 传入 chain_override（journal 精确解析结果），
        #    绕开对 backup_records 的模糊扫描，避免高频实时增量被 LIMIT 截断。
        chain = self._build_restore_chain(backup_path,
                                          chain_override=kwargs.get("chain_override"))
        if not chain:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                message="恢复失败：未找到可恢复的备份链",
            )

        self.logger.info(
            "[%s] 文件恢复链: %s -> %s (共 %d 个归档)",
            self.task_name, chain, target, len(chain),
        )
        try:
            os.makedirs(target, exist_ok=True)
            for item in chain:
                self.logger.info("[%s] 解压归档: %s", self.task_name, item)
                self._extract_archive(item, target)
                # 应用增量删除清单（.deleted.txt），保证恢复结果与源最终状态一致
                deleted = self._read_deleted_manifest(item)
                if deleted:
                    self._apply_deleted(target, deleted)
            types = ",".join(
                "增量" if "_inc" in os.path.basename(p) else "全量" for p in chain
            )
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=backup_path,
                message=f"已恢复至 {target} | 链: {types}",
            )
        except Exception as e:
            self.logger.error("[%s] 文件恢复失败: %s", self.task_name, e)
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path,
                message=f"文件恢复失败: {e}",
            )

    def _try_file_cross_host_restore(self, backup_path: str, target_host_info: dict,
                                     target_dir: str, kwargs: dict = None) -> BackupResult:
        """文件跨主机链式恢复：SFTP 推送恢复链(全量+增量) → 远程依次解压 → 应用删除清单。"""
        from core import cross_host
        from core import db as _db
        kwargs = kwargs or {}
        try:
            chain = self._build_restore_chain(
                backup_path, chain_override=kwargs.get("chain_override"))
        except Exception as e:
            self.logger.warning("[%s] 构建恢复链失败，仅恢复当前归档: %s", self.task_name, e)
            chain = []
        if not chain:
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, message="恢复失败：未找到可恢复的备份链",
            )
        target = dict(target_host_info)
        target["password"] = _db.decrypt_secret(target.get("password") or "")

        def log(msg):
            self.logger.info("[%s] %s", self.task_name, msg)

        client = None
        remote_tmp_files = []
        try:
            client = cross_host._build_ssh(target)
            for idx, item in enumerate(chain, 1):
                base = os.path.basename(item)
                remote_tmp = (f"/tmp/bk_restore_{time.strftime('%Y%m%d%H%M%S')}_{idx}_{base}")
                log(f"SFTP 上传 {idx}/{len(chain)}: {item} -> {remote_tmp}")
                cross_host._sftp_upload(client, item, remote_tmp, log)
                remote_tmp_files.append(remote_tmp)
                cmd = (f"mkdir -p '{target_dir}' && tar -xzf '{remote_tmp}' -C '{target_dir}' "
                       f"--exclude='.deleted.txt' --exclude='.rt_deleted.txt' "
                       f"&& echo 'OK {idx} {base}'")
                _o, _e, rc = cross_host._remote_exec_logged(client, cmd, timeout=3600, log=log)
                if rc != 0:
                    raise RuntimeError(
                        f"远程解压失败(rc={rc}): {(_e or b'').decode('utf-8', 'replace')[:200]}")
                # 应用增量删除清单（若归档含 .deleted.txt）
                del_cmd = self._remote_deleted_cmd(remote_tmp, target_dir)
                if del_cmd:
                    _o2, _e2, rc2 = cross_host._remote_exec_logged(
                        client, del_cmd, timeout=600, log=log)
                    if rc2 != 0:
                        log(f"远程应用删除清单警告(rc={rc2}): "
                            f"{(_e2 or b'').decode('utf-8', 'replace')[:200]}")
            types = ",".join(
                "增量" if "_inc" in os.path.basename(p) else "全量" for p in chain)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=backup_path,
                message=f"已恢复至 {target.get('hostname')}:{target_dir} | 链: {types}",
            )
        except Exception as e:
            self.logger.error("[%s] 文件跨主机恢复失败: %s", self.task_name, e)
            return BackupResult(
                success=False, status=BackupStatus.FAILED,
                backup_path=backup_path, message=f"跨主机恢复失败: {e}",
            )
        finally:
            if client:
                try:
                    if remote_tmp_files:
                        rm_cmd = " ".join(f"'{p}'" for p in remote_tmp_files)
                        cross_host._remote_exec_logged(
                            client, f"rm -f {rm_cmd} 2>/dev/null; true",
                            timeout=120, log=lambda x: None)
                except Exception:
                    pass
                try:
                    client.close()
                except Exception:
                    pass

    @staticmethod
    def _remote_deleted_cmd(remote_arc: str, target_dir: str) -> str:
        """构造远程命令：从增量归档读取删除清单并在目标目录删除（base64 内嵌 Python 脚本）。"""
        import base64
        script = (
            "import os, tarfile\n"
            f"arc = {remote_arc!r}\n"
            f"d = {target_dir!r}\n"
            "try:\n"
            "    tf = tarfile.open(arc, 'r:*')\n"
            "except Exception:\n"
            "    raise SystemExit(0)\n"
            "deleted = 0\n"
            "for name in ('.deleted.txt', '.rt_deleted.txt'):\n"
            "    try:\n"
            "        m = tf.getmember(name)\n"
            "    except KeyError:\n"
            "        continue\n"
            "    data = tf.extractfile(m).read().decode('utf-8', 'replace')\n"
            "    for line in data.splitlines():\n"
            "        line = line.strip()\n"
            "        if not line:\n"
            "            continue\n"
            "        p = os.path.join(d, line.replace('\\\\', os.sep))\n"
            "        try:\n"
            "            if os.path.islink(p) or os.path.isfile(p):\n"
            "                os.unlink(p)\n"
            "            elif os.path.isdir(p):\n"
            "                os.rmdir(p)\n"
            "            else:\n"
            "                continue\n"
            "            deleted += 1\n"
            "            print('del', line)\n"
            "        except OSError:\n"
            "            pass\n"
            "print('DELETED_COUNT', deleted)\n"
        )
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        return f"echo {b64} | base64 -d | python3 -"

    def _build_restore_chain(self, backup_path: str,
                             chain_override: List[str] = None) -> List[str]:
        """根据备份路径构建恢复链。
        - 全量 -> [full]
        - 增量 -> [full, inc1, inc2, ..., selected_inc]（按 started_at 排序）
        支持跨任务：只要源路径相同，即可找到对应全量基准。
        若找不到库记录（如手动传入路径），直接返回 [backup_path]。

        Args:
            backup_path: 目标归档路径。
            chain_override: 由上层（PITRRestore）解析好的精确链。非空时直接采用，
                仅做「文件存在性」过滤，不再查询 backup_records。
        """
        if chain_override:
            chain = []
            for item in chain_override:
                if not item:
                    continue
                if os.path.exists(item) and item not in chain:
                    chain.append(item)
                elif not os.path.exists(item):
                    self.logger.warning("[%s] 恢复链缺失归档，已跳过: %s",
                                        self.task_name, item)
            if chain:
                return chain
            self.logger.warning("[%s] chain_override 全部缺失，回退自动解析",
                                self.task_name)

        if not backup_path or not os.path.exists(backup_path):
            return []
        norm = os.path.abspath(backup_path).replace("\\", "/")

        # 1) 尝试从快照直接拿到 last_full_path（最可靠，尤其跨任务场景）
        meta = self._load_snapshot_meta()
        last_full_from_snap = meta.get("last_full_path")

        # 2) 从数据库找到当前记录
        rec = db.query_one(
            "SELECT * FROM backup_records WHERE backup_path=? AND db_type='file' ORDER BY id DESC LIMIT 1",
            (norm,),
        )
        if not rec:
            # 退化成普通单文件恢复
            if last_full_from_snap and last_full_from_snap != backup_path and os.path.exists(last_full_from_snap):
                return [last_full_from_snap, backup_path]
            return [backup_path]

        # 当前是 full 就直接返回
        if rec.get("backup_type") == "full":
            return [backup_path]

        cur_started = rec.get("started_at") or ""
        cur_task_id = rec.get("task_id")

        # 3) 找全量基准：优先用快照里的 last_full_path，再按源路径匹配
        full_path = None
        full_started = ""
        if last_full_from_snap and os.path.exists(last_full_from_snap):
            full_path = last_full_from_snap
            # 尝试从数据库拿 started_at
            fr = db.query_one(
                "SELECT started_at FROM backup_records WHERE backup_path=? AND db_type='file' AND backup_type='full'",
                (last_full_from_snap.replace("\\", "/"),),
            )
            full_started = fr.get("started_at") or "" if fr else ""

        if not full_path and cur_task_id:
            # 同一任务内找最近的 full
            full_rec = db.query_one(
                "SELECT * FROM backup_records WHERE task_id=? AND db_type='file' "
                "AND backup_type='full' AND (started_at <= ? OR ? = '') "
                "AND backup_path IS NOT NULL AND backup_path != '' "
                "ORDER BY started_at DESC, id DESC LIMIT 1",
                (cur_task_id, cur_started, cur_started),
            )
            if full_rec:
                full_path = full_rec["backup_path"]
                full_started = full_rec.get("started_at") or ""

        if not full_path:
            # 按源路径匹配：解析当前任务源路径，在历史 full 记录中找相同源的最近一条
            src_key = self._source_config_key()
            if src_key:
                candidates = db.query(
                    "SELECT * FROM backup_records WHERE db_type='file' AND backup_type='full' "
                    "AND (started_at <= ? OR ? = '') AND backup_path IS NOT NULL AND backup_path != '' "
                    "ORDER BY started_at DESC, id DESC LIMIT 200",
                    (cur_started, cur_started),
                )
                for c in candidates:
                    if self._same_source(c.get("extra_options"), src_key):
                        full_path = c["backup_path"]
                        full_started = c.get("started_at") or ""
                        break

        if not full_path:
            # 没有全量基准，只能单独恢复这个增量（可能不完整）
            return [backup_path]

        # 4) 收集 full 之后到当前记录之间的所有增量（跨任务、同源）
        src_key = self._source_config_key()
        inc_rows = db.query(
            "SELECT * FROM backup_records WHERE db_type='file' AND backup_type='incremental' "
            "AND (started_at >= ? OR ? = '') AND (started_at <= ? OR ? = '') "
            "AND backup_path IS NOT NULL AND backup_path != '' "
            "ORDER BY started_at ASC, id ASC",
            (full_started, full_started, cur_started, cur_started),
        )
        chain = [full_path]
        for r in inc_rows:
            bp = r.get("backup_path")
            if bp and os.path.exists(bp) and self._same_source(r.get("extra_options"), src_key):
                if bp not in chain:
                    chain.append(bp)
        # 确保当前记录也在链中
        if chain[-1] != backup_path and os.path.exists(backup_path):
            chain.append(backup_path)
        return chain

    def _source_config_key(self) -> Tuple[str, ...]:
        """返回源配置标识（源类型 + 清理后的源路径/主机），用于跨任务匹配同源备份。"""
        src = self._parse_source()
        paths = sorted(src.get("paths") or [])
        host = src.get("host", "")
        return (src.get("type", "local"), host, tuple(paths))

    def _same_source(self, extra_options, target_key: Tuple[str, ...]) -> bool:
        """判断 extra_options 是否与目标源配置相同。"""
        if not extra_options:
            return False
        try:
            if isinstance(extra_options, str):
                extra = json.loads(extra_options)
            else:
                extra = extra_options
            src_type = extra.get("source_type", "local")
            host = extra.get("source_host", "")
            paths = sorted(self._clean_path(p) for p in (extra.get("source_paths") or []) if self._clean_path(p))
            return (src_type, host, tuple(paths)) == target_key
        except Exception:
            return False

    # ---------- 先进压缩辅助 ----------
    def _compress_file(self, src_path: str, dst_path: str, algo: str) -> None:
        """把未压缩 tar(src_path) 用 zstd/gzip 流式压缩为 dst_path（可逆）。

        开启限速（bandwidth_limit>0）且系统存在 pv 时，压缩前先用 pv 限速读取
        源文件，近似控制整体备份吞吐（落盘带宽）。
        """
        comp = self.pipe_compress(algo)
        pv = self._pv_throttle()
        if pv:
            # pv -L <bytes> src_path | comp  （限速读取 → 压缩落盘）
            cmd = pv + [src_path] + comp
            with open(dst_path, "wb") as fout:
                proc = subprocess.Popen(cmd, stdout=fout, stderr=subprocess.PIPE)
                _, err = proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"压缩失败({algo}): {err.decode('utf-8','replace')[:300]}")
        else:
            with open(src_path, "rb") as fin, open(dst_path, "wb") as fout:
                proc = subprocess.Popen(comp, stdin=fin, stdout=fout,
                                         stderr=subprocess.PIPE)
                _, err = proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(f"压缩失败({algo}): {err.decode('utf-8','replace')[:300]}")

    def _stash_original_size(self, archive_path: str, original: int) -> None:
        """把未压缩原始大小暂存到 <archive>.orig.size，供落库时读取压缩率。"""
        try:
            with open(archive_path + ".orig.size", "w", encoding="utf-8") as f:
                f.write(str(int(original)))
        except OSError:
            pass

    def _read_original_size(self, archive_path: str) -> int:
        """读取暂存的原始大小；不存在返回 0。"""
        p = archive_path + ".orig.size"
        try:
            if os.path.exists(p):
                return int(open(p, "r", encoding="utf-8").read().strip() or 0)
        except (OSError, ValueError):
            pass
        return 0

    def _final_archive_path(self, base_archive_path: str) -> str:
        """返回真正落盘的归档路径（带 .zst/.gz 后缀）。"""
        for suf in (".zst", ".gz", ""):
            cand = base_archive_path + suf
            if os.path.exists(cand):
                return cand
        return base_archive_path

    @staticmethod
    def _restore_filter(member, path=""):
        """恢复时过滤危险路径（防止路径穿越）与增量删除清单文件。Python 3.12+ tarfile 会传 2 个参数。"""
        mpath = member.name
        if mpath.startswith("/") or ".." in mpath:
            return None
        if os.path.basename(mpath) in (".deleted.txt", ".rt_deleted.txt"):
            return None
        return member

    # ---------- 合成全量（鼎甲迪备 §3.2 永久增量 / CDM 合成） ----------
    def synthesize_full(self, sets: list = None, target_storage_tier: int = None,
                        target_record_id: int = None) -> BackupResult:
        """把「全量 + 一串增量」合并为一份新的完整归档（合成全量）。

        文件备份的合成全量 = 按链顺序解压每个归档到临时目录，后写的同名文件
        覆盖先前的（增量语义），最后把临时目录重新打成一份 tar(.zst/.gz)。
        产物经 verify_record 校验后可直接作为新的全量基准即时恢复，中间增量
        副本由生命周期策略按 chain_status='merged' 回收，实现「一次全备永久增备」。

        返回 BackupResult：success 表示合成成功；simulated 恒为 False（真实合并）。
        """
        import shutil
        import tarfile

        sets = sets or self.list_sets()
        if not sets:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="无可用备份集用于合成")
        # 链头（full/synthetic_full）+ 其增量（parent_set_id 指向链头）
        base = next((s for s in sets
                     if s.get("set_type") in ("full", "synthetic_full")), None)
        if not base:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="找不到合成基准(full)")
        chain = [base] + [s for s in sets
                          if s.get("parent_set_id") == base["id"]
                          and s.get("set_type") == "incremental"]
        chain = [c for c in chain if c.get("object_key")
                 and os.path.isfile(c["object_key"])]
        if len(chain) < 2:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message="增量链不足，无需合成")

        ts = self._timestamp()
        out_dir = self._output_dir()
        os.makedirs(out_dir, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=".syn_")
        try:
            # 1) 按链顺序解压到临时目录（增量覆盖全量，并应用增量删除清单）
            for c in chain:
                self._extract_archive(c["object_key"], tmp)
                self._apply_deleted(tmp, self._read_deleted_manifest(c["object_key"]))
            # 2) 重新打包成合成全量
            final = os.path.join(out_dir, f"{ts}__{self.task_name}__syn_full.tar")
            with tarfile.open(final, "w") as tf:
                for root, _dirs, files in os.walk(tmp):
                    for fn in files:
                        fp = os.path.join(root, fn)
                        arc = os.path.relpath(fp, tmp)
                        tf.add(fp, arcname=arc)
            # 3) 先进压缩（zstd 回退 gzip）
            algo = self._resolve_compress_algo()
            suffix = "" if algo == "none" else (".zst" if algo == "zstd" else ".gz")
            final_path = final + suffix
            self._compress_file(final, final_path, algo)
            self._stash_original_size(final_path, self._dir_size(tmp))
            size = os.path.getsize(final_path)
            checksum = db.sha256_file(final_path)
            return BackupResult(
                success=True, status=BackupStatus.SUCCESS,
                backup_path=final_path, size_bytes=size,
                original_size_bytes=self._dir_size(tmp),
                compress_algo=algo, checksum=checksum,
                simulated=False,
                message=f"合成全量完成（合并 {len(chain)-1} 个增量）| {db.human_size(size)}")
        except Exception as e:
            return BackupResult(success=False, status=BackupStatus.FAILED,
                                message=f"合成全量失败: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _extract_archive(self, archive_path: str, dest: str) -> None:
        """解压 .tar / .tar.gz / .tar.zst 到 dest（复用恢复路径的解压逻辑）。"""
        import tarfile
        import subprocess
        os.makedirs(dest, exist_ok=True)
        if archive_path.endswith((".zst", ".gz")) and not archive_path.endswith(".tar.gz"):
            algo = "zstd" if archive_path.endswith(".zst") else "gzip"
            dec = self.pipe_decompress(algo)
            fd, tmp_tar = tempfile.mkstemp(suffix=".tar")
            os.close(fd)
            proc = subprocess.Popen(dec, stdin=open(archive_path, "rb"),
                                    stdout=open(tmp_tar, "wb"),
                                    stderr=subprocess.PIPE)
            _, err = proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"解压失败: {err.decode('utf-8','replace')[:200]}")
            with tarfile.open(tmp_tar, "r:") as tf:
                tf.extractall(dest, filter=self._restore_filter)
            try:
                os.unlink(tmp_tar)
            except OSError:
                pass
        else:
            with tarfile.open(archive_path, "r:*") as tf:
                tf.extractall(dest, filter=self._restore_filter)

    @staticmethod
    def _dir_size(path: str) -> int:
        total = 0
        for root, _d, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
        return total

    def list_databases(self) -> List[str]:
        """文件引擎无需列举库，返回空。"""
        return []
