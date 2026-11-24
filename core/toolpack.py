# -*- coding: utf-8 -*-
"""ToolPack 推送式免安装工具包（离线自足 + 数据库服务器零安装）。

设计（对齐 xtrabackup 推送范式，见 docs/dbackup-benchmark-offline-design）：
- 平台侧随离线包烘焙工具二进制到 ``<repo>/tools/<group>/bin``（如 mongodb/bin）；
- 远端数据库服务器**从不安装**任何备份工具：远端已有则用远端，远端没有
  就把平台侧二进制 SFTP 推到 ``/tmp/aidbm_tool_<name>_<pid>`` 临时执行，
  命令结束由 shell 追加 ``rm -f`` 清理；
- 架构预检（ELF e_machine）避免 x86_64 推到 aarch64 白跑一次；
- 本机通道暴露 ``local_path(name)`` 给 base._resolve_local_tool 兜底。

本模块只做"解析 + 推送"，不碰具体备份语义（mongodump/xtrabackup 各自构造命令）。
"""
import os
import struct
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_ROOT = os.path.join(ROOT, "tools")
_PUSH_LOCK = threading.Lock()

# ELF e_machine 值（/usr/include/elf.h）
_ELF_MACHINE = {0x3E: "x86_64", 0xB7: "aarch64", 0x28: "arm"}


def local_path(name: str) -> str:
    """在平台随包工具目录里找二进制（优先 tools/<group>/bin/name）。"""
    if not name:
        return ""
    for group in sorted(os.listdir(TOOLS_ROOT)) if os.path.isdir(TOOLS_ROOT) else []:
        candidate = os.path.join(TOOLS_ROOT, group, "bin", name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        candidate = os.path.join(TOOLS_ROOT, group, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # 递归兜底（防止 tools/<group>/<version>/bin/name 这类布局）
    for current, _dirs, files in os.walk(TOOLS_ROOT):
        if name in files:
            candidate = os.path.join(current, name)
            if os.access(candidate, os.X_OK):
                return candidate
    return ""


def elf_arch(path: str) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(20)
        if head[:4] != b"\x7fELF" or len(head) < 20:
            return ""
        return _ELF_MACHINE.get(struct.unpack_from("<H", head, 18)[0], "")
    except Exception:
        return ""


def remote_arch(client) -> str:
    try:
        from core import remote_dump
        r = remote_dump.remote_exec_capture_on_client(client, "uname -m", timeout=10) \
            if hasattr(remote_dump, "remote_exec_capture_on_client") else None
        if r is None:
            from core.engines.file import _ssh_exec_pipe
            out, _err, rc = _ssh_exec_pipe(client, "uname -m", timeout=10)
            return (out or "").strip() if rc == 0 else ""
        return (r.get("stdout") or "").strip()
    except Exception:
        return ""


def push_tool(client, name: str, *, label: str = "") -> tuple:
    """把平台侧工具推到远端 /tmp 临时路径，返回 (remote_path, note)。

    调用方负责在命令末尾 ``rm -f <remote_path>`` 清理。
    抛 RuntimeError 表示不可推送（缺工具/架构不匹配），上层据此如实失败。
    """
    from core import remote_dump
    local = local_path(name)
    if not local:
        raise RuntimeError(
            "平台 ToolPack 缺少 %s（已搜索 %s）；请把离线工具包放入 "
            "tools/<group>/bin/%s" % (name, TOOLS_ROOT, name))
    local_arch = elf_arch(local)
    r_arch = remote_arch(client)
    if local_arch and r_arch and local_arch != r_arch:
        raise RuntimeError(
            "ToolPack %s 架构(%s)与目标服务器(%s)不一致，无法推送执行；"
            "请在平台放置匹配架构的工具包" % (name, local_arch, r_arch))
    with _PUSH_LOCK:
        import time
        remote = "/tmp/aidbm_tool_%s_%d" % (name, int(time.time() * 1000) % 100000000)
        sftp = client.open_sftp()
        try:
            sftp.put(local, remote)
            sftp.chmod(remote, 0o755)
        finally:
            sftp.close()
    note = "ToolPack 推送 %s（%s，临时执行后清理）" % (
        name, label or local_arch or "平台随包")
    return remote, note


def cleanup_suffix(remote_path: str, keep_rc_var: str = "__AIDBM_RC") -> str:
    """生成 shell 清理尾巴：保留原 rc、删工具、退出原 rc。"""
    if not remote_path:
        return ""
    return (" ; %s=$?; rm -f %s; exit $%s"
            % (keep_rc_var, _q(remote_path), keep_rc_var))


def _q(value: str) -> str:
    import shlex
    return shlex.quote(str(value))
