# -*- coding: utf-8 -*-
"""群晖 DSM Web API 客户端（仅标准库，离线自足、DSM 侧零安装）。

设计约束（与 AIDBM 一贯纪律一致）：

1. **零第三方依赖**：只用 ``urllib.request`` + ``ssl`` + ``json``，不需要
   synology-api / requests；离线包无需新增 wheel。
2. **DSM 侧零安装**：只调用 DSM 自带的 Web API（HTTP 5000 / HTTPS 5001）。
   不往群晖上装任何 agent、插件或脚本；SAN Manager / rsync / SSH 之类只能
   「启用 DSM 自带服务」，永远不部署文件。
3. **能力发现优先**：DSM 6 / 7 / 7.2 的 API 路径与版本不同，先
   ``SYNO.API.Info.query`` 拿每个 API 的 ``path`` 与 ``maxVersion``，再按实际
   能力调用。只公开声明的 API 才当作稳定契约。
4. **如实标注边界**：``SYNO.FileStation.*`` 有官方 API 指南，稳定可用；
   ``SYNO.Core.*`` / Storage / Snapshot Replication / SAN Manager 属于 DSM
   内部或非公开接口，**只在能力发现命中时尽力探测，失败一律返回
   unsupported，不伪造数据、不假装纳管成功**。
5. **凭据不落日志**：密码只进 POST/GET 参数一次，异常信息只带错误码。

参考（官方）：
- DSM Login Web API Guide：SYNO.API.Info / SYNO.API.Auth（login/logout, sid）
- Synology File Station Official API Guide：List(list_share/list/getinfo)、
  Download(download)、Search、VirtualFolder
- Synology SAN Manager 软件规格：iSCSI Target ≤256 / LUN ≤512 / 单 LUN 快照
  ≤256，CHAP 与相互 CHAP、IQN 访问控制、多重会话
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["DSMError", "DSMClient"]

# SYNO.API.Auth 常见错误码（官方 Login Web API Guide）
_ERR_NO_PERMISSION = 105
_ERR_SESSION_TIMEOUT = 106
_ERR_DUP_LOGIN = 107
_ERR_SID_INVALID = 119
_RELOGIN_CODES = {_ERR_SESSION_TIMEOUT, _ERR_DUP_LOGIN, _ERR_SID_INVALID}

# 官方公开 API（有官方指南），其余一律视为「尽力探测」
_PUBLIC_APIS = {
    "SYNO.API.Info", "SYNO.API.Auth",
    "SYNO.FileStation.Info", "SYNO.FileStation.List",
    "SYNO.FileStation.Search", "SYNO.FileStation.VirtualFolder",
    "SYNO.FileStation.Download", "SYNO.FileStation.Upload",
}


class DSMError(Exception):
    """DSM 调用失败。code 为 SYNO 错误码，api 便于定位是哪一类能力。"""

    def __init__(self, message: str, code: int = 0, api: str = "",
                 detail: str = ""):
        super().__init__(message)
        self.code = code
        self.api = api
        self.detail = detail


class DSMClient:
    """一台 DSM 设备的会话客户端。

    Args:
        host: IP 或域名（不带 scheme）
        port: 5000(HTTP) / 5001(HTTPS)
        secure: 是否 HTTPS
        verify_ssl: 自签名证书环境默认 False
        username/password: DSM 账号（建议专用最小权限账号，不要用默认 admin）
        otp_code: 账号开启两步验证时的一次性验证码
        timeout: 单次请求超时（秒）
    """

    def __init__(self, host: str, port: int = 5000, secure: bool = False,
                 verify_ssl: bool = False, username: str = "",
                 password: str = "", otp_code: str = "", timeout: int = 15,
                 session_name: str = "AIDBM"):
        self.host = (host or "").strip().rstrip("/")
        if "://" in self.host:
            raise DSMError("host 请只填 IP/域名，协议用 secure 参数指定")
        self.port = int(port or (5001 if secure else 5000))
        self.secure = bool(secure)
        self.verify_ssl = bool(verify_ssl)
        self.username = username or ""
        self.password = password or ""
        self.otp_code = otp_code or ""
        self.timeout = int(timeout or 15)
        self.session_name = session_name or "AIDBM"
        self._sid = ""
        self._syno_token = ""
        self._api_info: dict = {}
        self.dsm_major = 0

    # ---------------------------------------------------------------- 底层
    @property
    def base_url(self) -> str:
        return "%s://%s:%d" % ("https" if self.secure else "http",
                               self.host, self.port)

    def _request(self, path: str, params: dict, download: bool = False,
                 headers: dict | None = None):
        url = "%s/webapi/%s?%s" % (
            self.base_url, path.lstrip("/"), urllib.parse.urlencode(params))
        req = urllib.request.Request(url, headers=dict(headers or {}))
        ctx = None
        if self.secure and not self.verify_ssl:
            ctx = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                raw = resp.read()
                if download:
                    hd = dict(resp.headers)
                    hd["_status"] = str(resp.status)
                    return raw, hd
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            raise DSMError("DSM HTTP %d（%s %s）" % (e.code, path, params.get("api", "")),
                           code=e.code, api=str(params.get("api") or ""),
                           detail=body[:300].decode("utf-8", "ignore"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise DSMError("无法连接 DSM %s: %s" % (self.base_url, str(e)[:200]),
                           api=str(params.get("api") or ""), detail=str(e)[:300])
        try:
            return json.loads(raw.decode("utf-8", "replace")), {}
        except Exception:
            raise DSMError("DSM 返回非 JSON 响应", api=str(params.get("api") or ""),
                           detail=raw[:200].decode("utf-8", "ignore"))

    @staticmethod
    def _err_code(res: dict) -> int:
        err = res.get("error") or {}
        try:
            return int(err.get("code") or 0)
        except (TypeError, ValueError):
            return 0

    # ---------------------------------------------------------------- 会话
    def discover(self, query: str = "all") -> dict:
        """SYNO.API.Info：发现本机实际支持的 API、路径与最大版本。"""
        res, _ = self._request("query.cgi", {
            "api": "SYNO.API.Info", "version": 1, "method": "query",
            "query": query})
        if not res.get("success"):
            raise DSMError("能力发现失败: code=%d" % self._err_code(res),
                           code=self._err_code(res), api="SYNO.API.Info")
        self._api_info = res.get("data") or {}
        return self._api_info

    def _spec(self, api: str) -> tuple[str, int]:
        if not self._api_info:
            self.discover()
        info = self._api_info.get(api) or {}
        # 官方缺省路径/版本，用于能力发现未返回该 API 时的稳妥回退
        default_path = "query.cgi" if api == "SYNO.API.Info" else "entry.cgi"
        path = info.get("path") or default_path
        try:
            ver = int(info.get("maxVersion") or (1 if api == "SYNO.API.Info" else 1))
        except (TypeError, ValueError):
            ver = 1
        return path, ver

    def login(self) -> str:
        path, maxv = self._spec("SYNO.API.Auth")
        # DSM 6 的 Auth 最高 6，DSM 7 为 7；两者都接受 format=sid。
        ver = min(maxv, 7) if maxv >= 6 else 6
        params = {
            "api": "SYNO.API.Auth", "version": ver, "method": "login",
            "account": self.username, "passwd": self.password,
            "session": self.session_name, "format": "sid",
        }
        if self.otp_code:
            params["otp_code"] = self.otp_code
        res, _ = self._request(path, params)
        if not res.get("success"):
            raise DSMError("DSM 登录失败: code=%d（检查账号/密码/两步验证/权限）"
                           % self._err_code(res), code=self._err_code(res),
                           api="SYNO.API.Auth")
        data = res.get("data") or {}
        self._sid = data.get("sid") or ""
        self._syno_token = data.get("synotoken") or ""
        if not self._sid:
            raise DSMError("DSM 登录未返回 sid", api="SYNO.API.Auth")
        self.dsm_major = 7 if ver >= 7 else 6
        return self._sid

    def logout(self) -> None:
        if not self._sid:
            return
        try:
            self.call("SYNO.API.Auth", "logout", {"session": self.session_name},
                      retry_auth=False)
        except DSMError:
            pass
        self._sid = ""

    def call(self, api: str, method: str, params: dict | None = None,
             version: int | None = None, retry_auth: bool = True) -> dict:
        if not self._sid:
            self.login()
        path, maxv = self._spec(api)
        p = {"api": api, "version": int(version or maxv), "method": method,
             "_sid": self._sid}
        if self._syno_token:
            p["SynoToken"] = self._syno_token
        p.update({k: v for k, v in (params or {}).items() if v is not None})
        res, _ = self._request(path, p)
        if not res.get("success"):
            code = self._err_code(res)
            if code in _RELOGIN_CODES and retry_auth:
                self._sid = ""
                self.login()
                return self.call(api, method, params, version, retry_auth=False)
            raise DSMError("DSM %s.%s 失败: code=%d" % (api, method, code),
                           code=code, api=api)
        return res.get("data") or {}

    # ---------------------------------------------------------------- 能力
    def capabilities(self) -> dict:
        """纳管体检：哪些能力在这台 DSM 上真实可用（不猜测、不虚报）。"""
        if not self._api_info:
            self.discover()
        present = set(self._api_info)

        def has(*names):
            return any(n in present for n in names)

        out = {
            "api_count": len(present),
            "public_apis": sorted(present & _PUBLIC_APIS),
            "filestation": has("SYNO.FileStation.List"),
            "download": has("SYNO.FileStation.Download"),
            "system_info": has("SYNO.Core.System", "SYNO.DSM.Info"),
            "storage": has("SYNO.Storage.CGI.Storage", "SYNO.Core.Storage"),
            "share_admin": has("SYNO.Core.Share"),
            "snapshot": has("SYNO.Core.Snapshot", "SYNO.Snapshot.Replication"),
            "san_manager": has("SYNO.Core.iSCSI", "SYNO.SANManager"),
            "unofficial": [],
        }
        for key in ("system_info", "storage", "share_admin", "snapshot", "san_manager"):
            if out[key]:
                out["unofficial"].append(key)
        out["note"] = (
            "FileStation 为官方公开 API；system_info/storage/share_admin/"
            "snapshot/san_manager 为 DSM 内部或非公开接口，仅在能力发现命中时"
            "尽力探测，失败一律返回 unsupported，不承诺跨 DSM 版本稳定。")
        return out

    def probe(self) -> dict:
        """一键探活：连接 → 登录 → 系统信息 → 共享文件夹统计。"""
        out = {"ok": False, "host": self.host, "port": self.port,
               "secure": self.secure, "checked_at": time.strftime(
                   "%Y-%m-%dT%H:%M:%S%z")}
        try:
            caps = self.capabilities()
            if not self._sid:
                self.login()
            out["capabilities"] = caps
            out["dsm_version"] = self.system_info().get("version") or ""
            shares = self.list_shares() if caps["filestation"] else []
            out["share_count"] = len(shares)
            out["shares"] = [s.get("name") or s.get("path") for s in shares][:50]
            out["ok"] = True
        except DSMError as e:
            out["error"] = str(e)
            out["error_code"] = e.code
        return out

    def system_info(self) -> dict:
        """尽力获取系统信息；接口不存在时返回空字典（不抛错、不伪造）。"""
        for api, method in (("SYNO.DSM.Info", "getinfo"),
                            ("SYNO.Core.System", "info")):
            if api not in self._api_info:
                continue
            try:
                d = self.call(api, method)
                return {"version": d.get("version") or d.get("firmware_ver") or "",
                        "model": d.get("model") or "",
                        "serial": d.get("serial") or "",
                        "temperature": d.get("temperature"),
                        "ram": d.get("ram"),
                        "source_api": api}
            except DSMError:
                continue
        return {}

    # ---------------------------------------------------------------- 资产
    def list_shares(self) -> list[dict]:
        """官方 API：列出共享文件夹（DSM 文件服务侧的真实对象）。"""
        out, offset, limit = [], 0, 1000
        while True:
            d = self.call("SYNO.FileStation.List", "list_share", {
                "offset": offset, "limit": limit,
                "sort_by": "name", "sort_direction": "asc",
                "additional": json.dumps(["real_path", "size", "time", "perm"]),
            })
            batch = d.get("shares") or []
            out.extend(batch)
            total = int(d.get("total") or 0)
            offset += len(batch)
            if not batch or offset >= total:
                break
        return out

    def list_folder(self, folder_path: str) -> list[dict]:
        """官方 API：单层列举，带 size / mtime（增量判据的数据源）。"""
        out, offset, limit = [], 0, 1000
        while True:
            d = self.call("SYNO.FileStation.List", "list", {
                "folder_path": folder_path, "offset": offset, "limit": limit,
                "sort_by": "name", "sort_direction": "asc", "filetype": "all",
                "additional": json.dumps(["size", "time", "type", "real_path"]),
            })
            batch = d.get("files") or []
            out.extend(batch)
            total = int(d.get("total") or 0)
            offset += len(batch)
            if not batch or offset >= total:
                break
        return out

    def walk_files(self, root_path: str, max_files: int = 200000) -> list[dict]:
        """递归枚举文件，归一化为 ``{path,size,mtime,isdir}``。

        这是 DSM 通道做「全量/增量」的统一清单：增量判据与现有文件引擎一致
        （size + mtime 比对），因此可直接复用 ``core/dedup_source.py`` 的
        文件级源端去重与增量链语义。
        """
        stack, out = [(root_path.rstrip("/") or "/")], []
        seen = 0
        while stack and len(out) < max_files:
            folder = stack.pop()
            try:
                items = self.list_folder(folder)
            except DSMError:
                # 单个子目录无权限：如实跳过并在结果里留痕，不静默吞掉
                out.append({"path": folder, "size": 0, "mtime": 0,
                            "isdir": True, "error": "unreadable"})
                continue
            for it in items:
                path = it.get("path") or ""
                add = it.get("additional") or {}
                tm = add.get("time") or {}
                out.append({
                    "path": path,
                    "size": int(add.get("size") or 0),
                    "mtime": int(tm.get("mtime") or 0),
                    "isdir": bool(it.get("isdir")),
                })
                seen += 1
                if it.get("isdir") and path:
                    stack.append(path)
                if len(out) >= max_files:
                    break
        return out

    # ---------------------------------------------------------------- 下载
    def download(self, path: str, dest: str, resume: bool = False,
                 expected_size: int = 0, part_suffix: str = ".aidbm_part") -> int:
        """官方 API 下载单个文件；resume=True 时用 HTTP Range 续传。

        FileStation.Download 返回文件字节流。部分 DSM 版本/反向代理不支持
        Range（回 200 整包），此时截断重写，绝不拼接出错位文件。
        """
        if not self._sid:
            self.login()
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        part = dest + part_suffix if resume else dest
        offset = os.path.getsize(part) if (resume and os.path.isfile(part)) else 0
        if resume and expected_size and offset > expected_size:
            try:
                os.remove(part)
            except OSError:
                pass
            offset = 0
        if resume and expected_size and offset > 0 and offset == expected_size:
            os.replace(part, dest)
            return offset
        params = {"api": "SYNO.FileStation.Download", "version": 2,
                  "method": "download", "path": json.dumps([path]),
                  "mode": "download", "_sid": self._sid}
        if self._syno_token:
            params["SynoToken"] = self._syno_token
        headers = {"Range": "bytes=%d-" % offset} if offset else {}
        raw, rh = self._request("entry.cgi", params, download=True, headers=headers)
        append = bool(offset and str(rh.get("_status") or "") == "206")
        if offset and not append:
            offset = 0        # 服务端忽略 Range：整包重发
        mode = "ab" if append else "wb"
        with open(part, mode) as f:
            f.write(raw)
        total = os.path.getsize(part)
        if expected_size and total != expected_size:
            raise DSMError("下载后大小不一致（源 %d / 盘 %d）"
                           % (expected_size, total), api="SYNO.FileStation.Download")
        if resume:
            os.replace(part, dest)
        return total

    @staticmethod
    def sha256_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(8 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
