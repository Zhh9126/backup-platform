# -*- coding: utf-8 -*-
"""Windows 文件备份通道的指纹清单与增量传输（零安装，只用系统自带组件）。

为什么需要这个模块：Linux 通道靠 ``find -printf`` 拿 ``size + mtime`` 基线，
Windows OpenSSH 下没有 find/sha256sum，因此 ``file.py`` 的 Windows 分支原先
**只能全量**（v1 语义），并且刻意不保存快照，避免空清单被增量误判成「全部删除」。

本模块用 Windows 自带 PowerShell 补上指纹清单：

* ``Get-ChildItem -Recurse -File`` → 相对路径 / ``Length`` / ``LastWriteTimeUtc``；
* 可选 ``Get-FileHash -Algorithm SHA256`` → 内容指纹（精确模式，识别
  「mtime 被改写但内容变了」以及时间戳被还原的篡改）；
* 清单以 **UTF-8 → Base64** 输出纯 ASCII，绕开 cmd/PowerShell 控制台代码页
  对中文路径的乱码问题（Windows 控制台显示乱码是代码页假象，用字节核验）；
* 变化文件经 SFTP 逐个拉到平台侧，由平台用标准库 ``tarfile`` 打成增量包，
  不依赖 Windows 上的 tar 命令行参数长度上限；
* 删除清单写进归档根 ``.deleted.txt``，恢复时用 PowerShell 脚本回放。

安全与诚实边界：

1. 不在被管 Windows 主机安装/落任何程序或脚本（命令通过
   ``-EncodedCommand`` 一次性下发；仅 Windows OpenSSH Server 为既有前提）。
2. 文件数超上限、命令失败、清单解析失败一律抛错，由调用方**回退全量**，
   绝不把不完整的清单当成「其余文件都删了」。
3. 精确模式要对每个文件算 SHA256，是 O(总文件量) 的读盘开销；默认关闭，
   默认判据与 Linux 通道一致（size + mtime，容差 5 秒）。
"""
from __future__ import annotations

import base64
import fnmatch
import io
import json
import os
import re
import tarfile
import time
from typing import Dict, List, Optional, Tuple

from core.win_protect import encode_ps

__all__ = [
    "WinFileError", "ps_quote", "build_scan_script", "parse_scan_output",
    "scan_windows", "match_excludes", "pull_changed", "tar_changed",
    "read_deleted_manifest", "build_delete_script", "win_delete_command",
    "merge_hash_changes", "DEFAULT_MAX_FILES",
]

DEFAULT_MAX_FILES = 50000
_META_PREFIX = "__META__"
_FILE_LIMIT = "AIDBM_FILE_LIMIT"
_DELETED = ".deleted.txt"


class WinFileError(Exception):
    """Windows 指纹清单获取 / 传输失败（调用方应回退全量，不得当作空清单）。"""


def ps_quote(value: str) -> str:
    """PowerShell 单引号字符串字面量（内部单引号翻倍）。"""
    return "'" + str(value or "").replace("'", "''") + "'"


def match_excludes(rel: str, excludes) -> bool:
    """与 ``core.engines.file._match_excludes`` 同语义。

    这里刻意复制一份而不是 import：``file.py`` 会 import 本模块，反向
    import 会造成循环。两条实现由单测逐例比对保证不漂移。
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


def build_scan_script(base: str, hash_mode: bool = False,
                      max_files: int = DEFAULT_MAX_FILES) -> str:
    """生成指纹清单 PowerShell 脚本，stdout 为纯 ASCII Base64(UTF-8 TSV)。

    TSV 列：``相对路径 \\t 字节数 \\t Unix 秒 \\t sha256(精确模式才有值)``。
    首行可能是 ``__META__\\tunreadable=<n>``，用于如实标注无权限目录。
    """
    if not base or ".." in str(base).replace("/", "\\").split("\\"):
        raise WinFileError("源路径不合法: %r" % base)
    flag = "$true" if hash_mode else "$false"
    limit = max(1, int(max_files or DEFAULT_MAX_FILES))
    return """$ErrorActionPreference = 'Stop'
$base = %s
$hashMode = %s
$maxFiles = %d
$item = Get-Item -LiteralPath $base -ErrorAction Stop
$errs = @()
if ($item.PSIsContainer) {
  $root = $item.FullName.TrimEnd('\\','/')
  $items = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue -ErrorVariable +errs)
} else {
  $root = $item.DirectoryName.TrimEnd('\\','/')
  $items = @($item)
}
$epoch = [datetime]::SpecifyKind([datetime]::Parse('1970-01-01'), [System.DateTimeKind]::Utc)
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine('__META__' + \"`t\" + 'unreadable=' + $errs.Count)
$n = 0
foreach ($f in $items) {
  $n = $n + 1
  if ($n -gt $maxFiles) { throw '%s' }
  $full = $f.FullName
  $rel = $full.Substring($root.Length).TrimStart('\\','/').Replace('\\','/')
  if ($rel.StartsWith('/')) { $rel = $rel.Substring(1) }
  $sec = [int64]($f.LastWriteTimeUtc - $epoch).TotalSeconds
  $sha = ''
  if ($hashMode) {
    try { $sha = (Get-FileHash -LiteralPath $full -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() } catch { $sha = '' }
  }
  [void]$sb.AppendLine($rel + \"`t\" + $f.Length + \"`t\" + $sec + \"`t\" + $sha)
}
$bytes = [System.Text.Encoding]::UTF8.GetBytes($sb.ToString())
[Convert]::ToBase64String($bytes)
""" % (ps_quote(base), flag, limit, _FILE_LIMIT)


def parse_scan_output(stdout: str, excludes=None) -> Tuple[Dict[str, Tuple[int, int]], Dict[str, str], dict]:
    """解析 Base64(UTF-8 TSV) 清单。返回 ``(files, hashes, meta)``。

    ``files`` 与 Linux 通道快照格式一致：``{rel: (size, unix_seconds)}``。
    """
    raw = (stdout or "").strip()
    # PowerShell 可能输出 BOM / 换行；Base64 只取合法字符
    raw = re.sub(r"\s+", "", raw)
    if not raw:
        raise WinFileError("PowerShell 未返回指纹清单（空输出）")
    try:
        text = base64.b64decode(raw).decode("utf-8")
    except Exception as e:
        raise WinFileError("指纹清单 Base64/UTF-8 解码失败: %s" % e)
    files: Dict[str, Tuple[int, int]] = {}
    hashes: Dict[str, str] = {}
    meta = {"unreadable": 0, "total": 0, "excluded": 0}
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith(_META_PREFIX + "\t"):
            for part in line.split("\t")[1:]:
                if part.startswith("unreadable="):
                    try:
                        meta["unreadable"] = int(part.split("=", 1)[1])
                    except ValueError:
                        pass
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rel = parts[0].replace("\\", "/").lstrip("/")
        if not rel or rel.startswith("../") or "/../" in rel:
            continue
        if match_excludes(rel, excludes):
            meta["excluded"] += 1
            continue
        try:
            files[rel] = (int(parts[1]), int(float(parts[2])))
        except (ValueError, TypeError):
            continue
        if len(parts) > 3 and parts[3]:
            hashes[rel] = parts[3].strip().lower()
        meta["total"] += 1
    if not files and meta["total"] == 0 and not text.startswith(_META_PREFIX):
        raise WinFileError("指纹清单格式异常（缺少 __META__ 头）")
    return files, hashes, meta


def _default_exec(client, cmd: str, timeout: int):
    """默认执行器：paramiko 直接跑 powershell，返回 (stdout_bytes, stderr_bytes, rc)。"""
    _in, out, err = client.exec_command(cmd, timeout=timeout)
    stdout = out.read()
    stderr = err.read()
    rc = out.channel.recv_exit_status()
    return stdout, stderr, rc


def scan_windows(client, base: str, excludes=None, hash_mode: bool = False,
                 max_files: int = DEFAULT_MAX_FILES, timeout: int = 900,
                 exec_fn=None) -> Tuple[Dict[str, Tuple[int, int]], Dict[str, str], dict]:
    """在 Windows 主机上取文件指纹清单。失败一律抛 ``WinFileError``。"""
    script = build_scan_script(base, hash_mode=hash_mode, max_files=max_files)
    cmd = "powershell -NoProfile -NonInteractive -EncodedCommand %s" % encode_ps(script)
    fn = exec_fn or (lambda c: _default_exec(client, c, timeout))
    try:
        stdout, stderr, rc = fn(cmd)
    except Exception as e:
        raise WinFileError("PowerShell 指纹清单执行失败: %s" % (str(e).strip() or type(e).__name__))
    out_b, err_b = stdout or b"", stderr or b""
    if isinstance(out_b, bytes):
        out_text = out_b.decode("utf-8", "replace")
    else:
        out_text = str(out_b)
    err_text = err_b.decode("utf-8", "replace") if isinstance(err_b, bytes) else str(err_b)
    if rc != 0 or _FILE_LIMIT in err_text:
        if _FILE_LIMIT in err_text:
            raise WinFileError("文件数超过指纹清单上限 %d（为防误判删除已中止，请回退全量或调大上限）"
                               % max_files)
        raise WinFileError("PowerShell 指纹清单失败(rc=%s): %s" % (rc, err_text.strip()[:300]))
    files, hashes, meta = parse_scan_output(out_text, excludes)
    meta["hash_mode"] = bool(hash_mode)
    return files, hashes, meta


def merge_hash_changes(files: Dict[str, Tuple[int, int]], hashes: Dict[str, str],
                       changed: List[str], prev_hashes: Optional[Dict[str, str]]) -> List[str]:
    """精确模式：把「size+mtime 未变但 SHA256 变了」的文件补进变化集。

    只在本次与上次**都有** hash 时比较；任一侧缺失则跳过（首次启用精确模式
    不做全量重传，避免出现一次假性全量）。
    """
    if not hashes or not prev_hashes:
        return changed
    out = list(changed)
    known = set(changed)
    for rel, sha in hashes.items():
        if rel in known or rel not in files:
            continue
        old = prev_hashes.get(rel)
        if old and old != sha:
            out.append(rel)
    return out


# ---------------------------------------------------------------- 变化文件传输
def pull_changed(client, base: str, changed: List[str], dest_dir: str) -> Tuple[List[str], List[str]]:
    """SFTP 逐个拉取变化文件到平台暂存目录，返回 (成功相对路径, 失败原因列表)。"""
    os.makedirs(dest_dir, exist_ok=True)
    root = str(base).replace("\\", "/").rstrip("/")
    sftp = client.open_sftp()
    ok, failures = [], []
    try:
        for rel in changed:
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                failures.append("%s: 非法的相对路径" % rel)
                continue
            remote = root + "/" + rel
            local = os.path.join(dest_dir, rel)
            real = os.path.realpath(local)
            if not real.startswith(os.path.realpath(dest_dir) + os.sep):
                failures.append("%s: 本地路径越界" % rel)
                continue
            try:
                os.makedirs(os.path.dirname(local), exist_ok=True)
                sftp.get(remote, local)
                ok.append(rel)
            except Exception as e:
                failures.append("%s: %s" % (rel, str(e).strip()[:160] or type(e).__name__))
    finally:
        try:
            sftp.close()
        except Exception:
            pass
    return ok, failures


def tar_changed(dest_dir: str, changed: List[str], deleted: List[str],
                archive_path: str) -> None:
    """平台侧把变化文件打成 tar.gz，删除清单写入归档根 ``.deleted.txt``。"""
    os.makedirs(os.path.dirname(archive_path) or ".", exist_ok=True)
    tmp = archive_path + ".tmp"
    with tarfile.open(tmp, "w:gz") as tf:
        for rel in changed:
            src = os.path.join(dest_dir, rel)
            if os.path.isfile(src):
                tf.add(src, arcname=rel.replace("\\", "/"))
        if deleted:
            payload = ("\n".join(x.replace("\\", "/") for x in deleted) + "\n").encode("utf-8")
            info = tarfile.TarInfo(name=_DELETED)
            info.size = len(payload)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(payload))
    os.replace(tmp, archive_path)


def read_deleted_manifest(archive_path: str) -> List[str]:
    """平台侧读取归档内的删除清单（用于生成 Windows 恢复侧删除命令）。"""
    if not archive_path or not os.path.isfile(archive_path):
        return []
    out = []
    try:
        with tarfile.open(archive_path, "r:*") as tf:
            for name in (_DELETED, ".rt_deleted.txt"):
                try:
                    m = tf.getmember(name)
                except KeyError:
                    continue
                f = tf.extractfile(m)
                if f is None:
                    continue
                for line in f.read().decode("utf-8", "replace").splitlines():
                    line = line.strip().replace("\\", "/")
                    if line and not line.startswith("/") and ".." not in line.split("/"):
                        out.append(line)
    except (OSError, tarfile.TarError):
        return []
    return out


# ---------------------------------------------------------------- 删除回放
def build_delete_script(target_dir: str, names: List[str]) -> str:
    """构造 Windows 目标侧的删除回放脚本（路径护栏 + 计数输出）。"""
    safe = [n.replace("\\", "/") for n in names
            if n and not n.startswith("/") and ".." not in n.replace("\\", "/").split("/")]
    arr = ", ".join(ps_quote(n) for n in safe) or ""
    return """$ErrorActionPreference = 'SilentlyContinue'
$root = %s
$names = @(%s)
$n = 0
$rootFull = [System.IO.Path]::GetFullPath($root)
foreach ($rel in $names) {
  $p = Join-Path $rootFull ($rel -replace '/','\\')
  $full = [System.IO.Path]::GetFullPath($p)
  if (-not $full.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) { continue }
  if (Test-Path -LiteralPath $full -PathType Leaf) {
    Remove-Item -LiteralPath $full -Force
    if ($?) { $n = $n + 1 }
  }
}
'WIN_DELETED_OK ' + $n
""" % (ps_quote(target_dir), arr)


def win_delete_command(target_dir: str, names: List[str]) -> str:
    """返回可直接交给 SSH 执行的 PowerShell 命令（纯 ASCII）。"""
    if not names:
        return ""
    return "powershell -NoProfile -NonInteractive -EncodedCommand %s" % encode_ps(
        build_delete_script(target_dir, names))
