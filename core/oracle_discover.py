# -*- coding: utf-8 -*-
"""Oracle 实例 / ORACLE_HOME / PDB 清单发现（SSH 只读，零安装）。

对标 zBackup 内嵌的 `/etc/oratab` 解析、11g/19c 双 ORACLE_HOME 自适应、
PDB inventory 与 `pdbInventory` 能力。全部通过 SSH 执行远端自带的
`ps` / `awk` / `sqlplus`，不在数据库服务器安装任何工具。

11g 无 PDB 概念：查询 `v$pdbs` 失败时如实返回 `pdb_supported=False`，
不伪造空清单冒充「无 PDB」。
"""
from __future__ import annotations

import os
import re

from core import remote_dump

_SAFE_SID = re.compile(r"^[A-Za-z0-9_$#]{1,32}$")


class OracleDiscoverError(ValueError):
    """发现失败（SSH 不可达 / 无 Oracle 实例），消息可直接透出。"""


def _client(task: dict):
    ssh = remote_dump.resolve_ssh_host(dict(task or {}))
    if ssh is None:
        raise OracleDiscoverError(
            "未找到可用的 SSH 主机；请纳管主机或在任务高级选项配置 SSH 凭据")
    try:
        return remote_dump._connect_isolated(ssh)
    except Exception as e:
        raise OracleDiscoverError(f"SSH 连接失败: {e}")


def _exec(cli, cmd: str, timeout: int = 30) -> tuple:
    _, out, err = cli.exec_command(cmd, timeout=timeout)
    text = out.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    return text, rc, err.read().decode("utf-8", "replace")


def _oratab(cli) -> dict:
    text, _, _ = _exec(cli, "cat /etc/oratab 2>/dev/null || true", timeout=15)
    homes = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split(":")
        if len(cols) >= 2 and _SAFE_SID.match(cols[0]) and cols[1].startswith("/"):
            homes[cols[0]] = cols[1]
    return homes


def _running_sids(cli) -> list:
    text, _, _ = _exec(
        cli, "ps -eo args 2>/dev/null | grep -oE 'ora_pmon_[A-Za-z0-9_$#]+' "
             "| sed 's/ora_pmon_//' | sort -u", timeout=20)
    return [s.strip() for s in text.splitlines()
            if s.strip() and _SAFE_SID.match(s.strip())]


def _version(cli, home: str) -> str:
    # 优先从 ORACLE_HOME 路径识别（零依赖），再尝试 sqlplus -V
    # ORACLE_HOME 常见 3 段（19.0.0）与 4 段（11.2.0.4）版本目录都要识别
    m = re.search(r"/(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(?=/)", home)
    if m:
        parts = [p for p in m.groups() if p]
        return ".".join(parts[:2]) + f" ({'.'.join(parts)})"
    text, _, _ = _exec(
        cli, f"test -x '{home}/bin/sqlplus' && '{home}/bin/sqlplus' -V "
             "2>/dev/null | head -2 || true", timeout=20)
    return " ".join(text.split())[:120]


def _pdbs(cli, home: str, sid: str, os_user: str = "oracle") -> dict:
    """查询 v$pdbs；11g 或权限不足时如实标注，不伪装成功。"""
    if not _SAFE_SID.match(sid):
        return {"supported": False, "items": [], "note": "SID 不合法"}
    sql = ("set heading off feedback off pagesize 0 linesize 200\n"
           "select name||'|'||open_mode from v$pdbs;\n"
           "exit\n")
    path = f"/tmp/aidbm_pdb_inventory_{sid}.sql"
    cli.exec_command(
        f"cat > {path} <<'AIDBM_SQL'\n{sql}AIDBM_SQL\nchmod 644 {path}", timeout=15)
    cmd = (f"export ORACLE_HOME='{home}' ORACLE_SID='{sid}' "
           f"LD_LIBRARY_PATH='{home}/lib'; "
           f"su - {os_user} -c \"{home}/bin/sqlplus -S / as sysdba @{path}\" "
           f"2>&1 | head -40; rm -f {path}")
    text, rc, _ = _exec(cli, cmd, timeout=45)
    items = []
    for line in text.splitlines():
        line = line.strip()
        if "|" in line and not line.upper().startswith(("ORA-", "SP2-")):
            name, _, mode = line.partition("|")
            if name and not name.startswith("-"):
                items.append({"name": name.strip(), "open_mode": mode.strip()})
    unsupported = any(k in text.upper() for k in
                      ("ORA-00942", "ORA-01034", "SP2-", "NOT AVAILABLE", "ORA-125"))
    return {
        "supported": bool(items) and not unsupported,
        "items": items,
        "note": "" if items else ("11g 或非 CDB / 无查询权限（v$pdbs 不可用）"
                                  if unsupported else "未发现 PDB"),
    }


def discover(task: dict, os_user: str = "oracle", with_pdbs: bool = True) -> dict:
    """返回 {ok, host, instances:[{sid,home,version,running,pdbs}], notes}。"""
    cli = _client(task)
    try:
        homes = _oratab(cli)
        running = set(_running_sids(cli))
        notes = []
        if not homes and not running:
            notes.append("远端未发现 /etc/oratab 记录或运行中的 ora_pmon 进程")
        sids = sorted(set(homes) | running)
        instances = []
        for sid in sids:
            home = homes.get(sid, "")
            if not home:
                notes.append(f"实例 {sid} 在运行但 /etc/oratab 无 ORACLE_HOME，工具路径需手工指定")
            item = {
                "sid": sid, "home": home, "running": sid in running,
                "version": _version(cli, home) if home else "unknown",
                "pdbs": {"supported": False, "items": [], "note": "ORACLE_HOME 未知"},
            }
            if with_pdbs and home and os.path.isabs(home):
                item["pdbs"] = _pdbs(cli, home, sid, os_user)
            instances.append(item)
        return {
            "ok": True,
            "host": str((task or {}).get("host") or ""),
            "instances": instances,
            "instance_count": len(instances),
            "notes": notes,
            "method": "SSH 只读 /etc/oratab + pmon + v$pdbs（零安装，11g 如实标注）",
        }
    finally:
        try:
            cli.close()
        except Exception:
            pass
