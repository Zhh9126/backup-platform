# -*- coding: utf-8 -*-
"""Windows 客户端目录防删除 / 防勒索（零安装，SSH + PowerShell）。

为什么不装 agent：AIDBM 的硬性约束是被管服务器零安装。这里只用 Windows
**自带**能力，通过已纳管的 OpenSSH 主机下发配置，不在目标机落任何文件：

1. ``cfa``（推荐）：Defender **受控文件夹访问**（Controlled Folder Access）。
   未授权进程（含勒索软件）不能改写受保护目录，授权业务进程加白名单后
   仍可正常写。对应清单「防止被勒索病毒加密」。
2. ``acl_deny_delete``：``icacls /deny Everyone:(DE,DC)`` 拒绝删除/删除子项，
   适合归档类只读目录。对应清单「防删除保护」。
3. ``both``：两者同时启用。

安全护栏：
- 路径必须绝对（盘符或 UNC），拒绝 ``..``、引号、``&|;`$`` 等 shell 元字符；
- 命令用 PowerShell ``-EncodedCommand``（UTF-16LE base64）传递，避免中文
  路径与多层引号嵌套被 shell 篡改；
- 所有下发 / 解除都写 WARNING 审计日志；连接失败、命令失败如实抛错，
  绝不把「命令没跑成功」包装成「防护已启用」。

诚实边界：
- CFA 需要 Windows 10/Server 2016+ 且 Defender 可用、执行账号为管理员；
  服务器装了第三方杀毒导致 Defender 被接管时，``Get-MpPreference`` 会报错，
  由 check/apply 如实返回失败；
- ACL 拒绝删除会同时限制业务删除（但不阻止写入/改写），因此仅建议用于
  归档目录；防加密请优先用 cfa；
- 这是「降低被加密/被删除概率」的主机侧加固，**不替代备份侧不可变锁定**
  （core/immutability.py）；两者叠加才构成完整防线。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Callable, Iterable

from core import db

__all__ = ["WinProtectError", "MODES", "validate_path", "encode_ps",
           "build_apply_script", "build_remove_script", "build_status_script",
           "parse_status", "apply", "remove", "check", "list_rules",
           "add_rule", "delete_rule", "init_table"]

MODES = ("cfa", "acl_deny_delete", "both")
TABLE = "win_protect_rules"
# Everyone 的 SID：避免不同语言 Windows 上 "Everyone" 名称本地化导致失效
_EVERYONE_SID = "*S-1-1-0"
_BAD_CHARS = re.compile(r'[\"\'&|;`$<>\r\n\t]')
_ABS_WIN = re.compile(r"^[A-Za-z]:[\\/]", re.I)
_UNC = re.compile(r"^\\\\[^\\/]+\\[^\\/]+", re.I)


class WinProtectError(Exception):
    pass


def init_table(conn=None) -> None:
    """幂等建表（不改 SCHEMA 大字符串，避免元库后端方言翻译风险）。"""
    own = conn is None
    conn = conn or db.get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS %s ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "host_id INTEGER NOT NULL, "
            "path TEXT NOT NULL, "
            "mode TEXT NOT NULL DEFAULT 'cfa', "
            "allowed_apps TEXT DEFAULT '[]', "
            "enabled INTEGER DEFAULT 1, "
            "status TEXT DEFAULT 'unknown', "
            "last_check_at TEXT, "
            "note TEXT DEFAULT '', "
            "created_at TEXT)" % TABLE)
        if own:
            conn.commit()
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def validate_path(path: str) -> str:
    """返回规范化后的 Windows 路径；非法输入直接拒绝。"""
    p = (path or "").strip().strip('"').strip("'")
    if not p or len(p) > 240:
        raise WinProtectError("目录路径为空或过长")
    if _BAD_CHARS.search(p):
        raise WinProtectError("目录路径含非法字符（引号/管道/分号/反引号等）")
    if ".." in p.replace("/", "\\").split("\\"):
        raise WinProtectError("目录路径不允许包含 ..")
    p = p.replace("/", "\\").rstrip("\\") or p
    if not (_ABS_WIN.match(p) or _UNC.match(p)):
        raise WinProtectError("目录路径必须是 Windows 绝对路径（如 D:\\data）或 UNC")
    # 禁止把系统盘根目录整个锁死（会直接破坏系统）
    if re.match(r"^[Cc]:\\?$", p, re.I):
        raise WinProtectError("不允许对系统盘根目录启用防护")
    return p


def _ps_quote(paths: Iterable[str]) -> str:
    return ", ".join("'%s'" % p.replace("'", "''") for p in paths)


def encode_ps(script: str) -> str:
    """PowerShell -EncodedCommand 要求 UTF-16LE base64。"""
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def build_apply_script(paths: list[str], mode: str = "cfa",
                       allowed_apps: list[str] | None = None) -> str:
    if mode not in MODES:
        raise WinProtectError("不支持的防护模式: %s" % mode)
    ps = ["$ErrorActionPreference = 'Stop'",
          "$paths = @(%s)" % _ps_quote(paths)]
    if mode in ("cfa", "both"):
        ps += [
            "Set-MpPreference -EnableControlledFolderAccess Enabled",
            "foreach ($p in $paths) { "
            "Add-MpPreference -ControlledFolderAccessProtectedFolders $p -ErrorAction Stop }",
        ]
        for app in (allowed_apps or []):
            a = validate_path(app)
            ps.append("Add-MpPreference -ControlledFolderAccessAllowedApplications "
                      "'%s' -ErrorAction Stop" % a.replace("'", "''"))
        ps.append("Write-Output ('CFA=Enabled PATHS=' + ($paths -join '|'))")
    if mode in ("acl_deny_delete", "both"):
        ps += [
            "foreach ($p in $paths) { "
            "if (-not (Test-Path -LiteralPath $p)) { throw ('目录不存在: ' + $p) }; "
            "icacls $p /deny '%s:(DE,DC)' /T /C | Out-Null; "
            "if ($LASTEXITCODE -ne 0) { throw ('icacls 失败: ' + $p) } }" % _EVERYONE_SID,
            "Write-Output 'ACL=DENY_DELETE_OK'",
        ]
    return "\n".join(ps)


def build_remove_script(paths: list[str], mode: str = "cfa") -> str:
    ps = ["$ErrorActionPreference = 'Stop'",
          "$paths = @(%s)" % _ps_quote(paths)]
    if mode in ("cfa", "both"):
        ps.append("foreach ($p in $paths) { "
                  "Remove-MpPreference -ControlledFolderAccessProtectedFolders $p "
                  "-ErrorAction SilentlyContinue }")
        ps.append("Write-Output 'CFA_FOLDERS_REMOVED'")
    if mode in ("acl_deny_delete", "both"):
        ps.append("foreach ($p in $paths) { "
                  "icacls $p /remove:d '%s' /T /C | Out-Null }" % _EVERYONE_SID)
        ps.append("Write-Output 'ACL_DENY_REMOVED'")
    return "\n".join(ps)


def build_status_script(paths: list[str]) -> str:
    ps = ["$ErrorActionPreference = 'Continue'",
          "$paths = @(%s)" % _ps_quote(paths)]
    ps += [
        "try { $p = Get-MpPreference -ErrorAction Stop; "
        "Write-Output ('CFA=' + $p.EnableControlledFolderAccess); "
        "Write-Output ('CFA_FOLDERS=' + ($p.ControlledFolderAccessProtectedFolders -join '|')); "
        "Write-Output ('CFA_ALLOWED=' + ($p.ControlledFolderAccessAllowedApplications -join '|')) "
        "} catch { Write-Output ('CFA_ERROR=' + $_.Exception.Message) }",
        "foreach ($d in $paths) { "
        "$acl = (icacls $d 2>&1 | Out-String); "
        "Write-Output ('ACL_PATH=' + $d); "
        "Write-Output ('ACL_DENY=' + [int]($acl -match 'DENY')) }",
    ]
    return "\n".join(ps)


def parse_status(output: str) -> dict:
    """解析 check 脚本输出（PowerShell 输出可能是 UTF-8 或 GBK 解码后的文本）。"""
    out = {"cfa": "unknown", "cfa_folders": [], "cfa_allowed": [],
           "acl": {}, "error": ""}
    for raw in (output or "").splitlines():
        line = raw.strip()
        if line.startswith("CFA_ERROR="):
            out["error"] = line.split("=", 1)[1]
        elif line.startswith("CFA_FOLDERS="):
            out["cfa_folders"] = [x for x in line.split("=", 1)[1].split("|") if x]
        elif line.startswith("CFA_ALLOWED="):
            out["cfa_allowed"] = [x for x in line.split("=", 1)[1].split("|") if x]
        elif line.startswith("CFA="):
            out["cfa"] = line.split("=", 1)[1]
        elif line.startswith("ACL_PATH="):
            out["_path"] = line.split("=", 1)[1]
        elif line.startswith("ACL_DENY="):
            p = out.pop("_path", "")
            if p:
                out["acl"][p] = line.split("=", 1)[1].strip() == "1"
    out.pop("_path", None)
    return out


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _exec_windows(host: dict, script: str, exec_fn: Callable | None = None) -> str:
    """在 Windows 主机执行 PowerShell 脚本，返回 stdout 文本。

    ``exec_fn(script) -> str`` 可注入（单元测试用）；缺省走 paramiko SSH。
    Windows OpenSSH 不能套 ``bash -lc``，直接执行 powershell 命令。
    """
    if exec_fn is not None:
        return exec_fn(script)
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(hostname=host.get("hostname"),
                    port=int(host.get("port") or 22),
                    username=host.get("username"),
                    password=host.get("password") or None,
                    timeout=15, look_for_keys=False, allow_agent=False)
        cmd = "powershell -NoProfile -NonInteractive -EncodedCommand %s" % encode_ps(script)
        _in, out, err = cli.exec_command(cmd, timeout=120)
        text = _decode(out.read())
        rc = out.channel.recv_exit_status()
        err_text = _decode(err.read())
        if rc != 0:
            raise WinProtectError("PowerShell 执行失败(rc=%d): %s"
                                  % (rc, (err_text or text)[-500:]))
        return text
    except WinProtectError:
        raise
    except Exception as e:
        raise WinProtectError("连接 Windows 主机失败: %s" % str(e)[:300])
    finally:
        try:
            cli.close()
        except Exception:
            pass


def _host(host_id: int) -> dict:
    from core import ssh_hosts
    h = ssh_hosts.get_host(int(host_id), include_secret=True)
    if not h:
        raise WinProtectError("纳管主机不存在: %s" % host_id)
    if (h.get("os_type") or "linux").lower() != "windows":
        raise WinProtectError("主机 %s 不是 Windows（os_type=%s）"
                              % (host_id, h.get("os_type")))
    return h


def _audit(action: str, host_id: int, paths: list[str], mode: str,
           ok: bool, detail: str = "") -> None:
    try:
        db.add_log("WARNING" if not ok else "INFO", "win_protect",
                   "%s host=%s mode=%s paths=%s ok=%s %s"
                   % (action, host_id, mode, ",".join(paths), ok, detail[:300]))
    except Exception:
        pass


def apply(host_id: int, paths: list[str], mode: str = "cfa",
          allowed_apps: list[str] | None = None,
          exec_fn: Callable | None = None) -> dict:
    ps = [validate_path(p) for p in (paths or [])]
    if not ps:
        raise WinProtectError("至少需要一个受保护目录")
    h = _host(host_id)
    script = build_apply_script(ps, mode, allowed_apps)
    try:
        out = _exec_windows(h, script, exec_fn=exec_fn)
    except Exception as e:
        _audit("apply", host_id, ps, mode, False, str(e))
        raise
    _audit("apply", host_id, ps, mode, True, (out or "")[-200:])
    for p in ps:
        try:
            db.execute("UPDATE %s SET status='applied', last_check_at=? "
                       "WHERE host_id=? AND path=?" % TABLE,
                       (time.strftime("%Y-%m-%d %H:%M:%S"), host_id, p))
        except Exception:
            pass
    return {"ok": True, "host_id": host_id, "paths": ps,
            "mode": mode, "output": (out or "")[-1000:]}


def remove(host_id: int, paths: list[str], mode: str = "cfa",
           exec_fn: Callable | None = None) -> dict:
    ps = [validate_path(p) for p in (paths or [])]
    if not ps:
        raise WinProtectError("至少需要一个目录")
    h = _host(host_id)
    script = build_remove_script(ps, mode)
    try:
        out = _exec_windows(h, script, exec_fn=exec_fn)
    except Exception as e:
        _audit("remove", host_id, ps, mode, False, str(e))
        raise
    _audit("remove", host_id, ps, mode, True, (out or "")[-200:])
    for p in ps:
        try:
            db.execute("UPDATE %s SET status='removed', last_check_at=? "
                       "WHERE host_id=? AND path=?" % TABLE,
                       (time.strftime("%Y-%m-%d %H:%M:%S"), host_id, p))
        except Exception:
            pass
    return {"ok": True, "host_id": host_id, "paths": ps, "output": out}


def check(host_id: int, paths: list[str], exec_fn: Callable | None = None) -> dict:
    ps = [validate_path(p) for p in (paths or [])]
    h = _host(host_id)
    out = _exec_windows(h, build_status_script(ps), exec_fn=exec_fn)
    st = parse_status(out)
    st.update({"ok": True, "host_id": host_id, "paths": ps})
    return st


def list_rules() -> list[dict]:
    try:
        rows = db.query("SELECT * FROM %s ORDER BY id DESC" % TABLE)
    except Exception:
        return []
    out = []
    for r in rows or []:
        d = dict(r)
        try:
            d["allowed_apps"] = json.loads(d.get("allowed_apps") or "[]")
        except Exception:
            d["allowed_apps"] = []
        out.append(d)
    return out


def add_rule(host_id: int, path: str, mode: str = "cfa",
             allowed_apps: list[str] | None = None) -> int:
    p = validate_path(path)
    if mode not in MODES:
        raise WinProtectError("不支持的防护模式: %s" % mode)
    init_table()
    apps = [validate_path(a) for a in (allowed_apps or [])]
    return db.execute(
        "INSERT INTO %s (host_id,path,mode,allowed_apps,enabled,status,created_at) "
        "VALUES (?,?,?,?,1,'unknown',?)" % TABLE,
        (int(host_id), p, mode, json.dumps(apps, ensure_ascii=False),
         time.strftime("%Y-%m-%d %H:%M:%S")))


def delete_rule(rule_id: int) -> None:
    db.execute("DELETE FROM %s WHERE id=?" % TABLE, (int(rule_id),))
