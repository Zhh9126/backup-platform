# -*- coding: utf-8 -*-
"""专业 NAS 阵列快照差量适配层（对标 PB 级方案 §2，当前只到「探测位」）。

背景
----
PB 级 NAS（数十亿小文件）如果靠 SSH `find+sha256sum` 全树遍历，一轮扫描就要
数小时甚至数天。方案的做法是调**阵列自身的快照差量 API**（NetApp SnapDiff v3 /
Isilon ChangelistCreate / 华为 Dorado diff_session）直接拿"变更文件列表"。

现状（必须如实说）
------------------
平台**没有**任何厂商 NAS 真机，因此本模块只实现：
  1. 阵列**连通性与认证探测**（确认地址、端口、账号是否可用，避免客户现场
     等到配置任务时才发现连不上）；
  2. 明确的**能力契约**（``list_snapshot_changes`` 直接抛 NotImplementedError
     并说明需要哪个 API），不做假实现、不返回伪造的变更清单。

拿到真机后只需要在 ``_VENDORS[vendor]["diff_impl"]`` 里补上实际调用，
上层（文件备份选源）无需改动。
"""
import base64
import json
import socket
try:
    import ssl
except ImportError:
    ssl = None
import urllib.error
import urllib.request

_VENDORS = {
    "netapp": {
        "name": "NetApp ONTAP",
        "scheme": "https",
        "port": 443,
        "probe_path": "/api/storage/volumes?fields=name&max_records=1",
        "diff_api": "SnapDiff v3（RPC）/ REST /api/snapdiff",
        "note": "需开启 ONTAP REST 服务并授予只读 API 角色",
    },
    "isilon": {
        "name": "Dell PowerScale (Isilon)",
        "scheme": "https",
        "port": 8080,
        "probe_path": "/platform/1/protocols/nfs/exports?limit=1",
        "diff_api": "OneFS Platform API：ChangelistCreate + 分页拉取",
        "note": "需 OneFS 8.0+，账号具备 ISI_PRIV_SYNCIQ/快照读权限",
    },
    "dorado": {
        "name": "华为 OceanStor Dorado",
        "scheme": "https",
        "port": 8088,
        "probe_path": "/deviceManager/rest/",
        "diff_api": "deviceManager REST：diff_session（存储芯片加速输出差量）",
        "note": "需先用 /deviceManager/rest/{deviceId}/login 取 token",
    },
}


def vendors() -> dict:
    """支持的厂商与所需权限（给前端下拉/说明用）。"""
    return {k: {kk: vv for kk, vv in v.items() if kk != "diff_impl"}
            for k, v in _VENDORS.items()}


def _ssl_ctx(verify_tls: bool):
    if verify_tls:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE     # 内网自签证书场景（仅探测）
    return ctx


def _http_get(url: str, username: str = "", password: str = "",
              verify_tls: bool = False, timeout: int = 8,
              headers: dict = None):
    req = urllib.request.Request(url, method="GET")
    if username:
        token = base64.b64encode(
            ("%s:%s" % (username, password)).encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_ctx(verify_tls)) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2048).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body
    except socket.timeout:
        return -2, "连接超时（%ss）" % timeout
    except ssl.SSLError as e:
        return -3, "TLS 握手失败: %s" % str(e)[:160]
    except urllib.error.URLError as e:
        return -1, "连接失败: %s" % str(e.reason)[:160]
    except Exception as e:  # noqa: BLE001
        return -1, str(e)[:160]


def probe(vendor: str, host: str, port: int = None, username: str = "",
          password: str = "", verify_tls: bool = False, timeout: int = 8) -> dict:
    """探测阵列连通性与认证（不读取任何业务数据）。

    返回 ``{ok, status, auth_ok, reason, diff_api, note}``：
    - ``ok``：能连上并拿到 2xx；
    - ``auth_ok``：认证通过（401/403 说明地址通但账号权限不对——这对现场
      排障比"连不上"更有信息量）。
    """
    v = _VENDORS.get((vendor or "").lower())
    if not v:
        return {"ok": False, "status": -1,
                "reason": "不支持的阵列类型: %s（支持: %s）"
                          % (vendor, ", ".join(_VENDORS))}
    host = (host or "").strip()
    if not host:
        return {"ok": False, "status": -1, "reason": "阵列地址为空"}
    p = int(port or v["port"])
    url = "%s://%s:%d%s" % (v["scheme"], host, p, v["probe_path"])
    status, body = _http_get(url, username, password, verify_tls, timeout)
    auth_ok = status in (200, 201, 204)
    if status in (401, 403):
        reason = ("已连通但认证失败（HTTP %s）——请核对账号/密码与只读 API 权限"
                  % status)
    elif status in (200, 201, 204):
        reason = "连通且认证通过"
    elif status == -3:
        reason = body
    else:
        reason = "%s（HTTP %s）：%s" % (v["name"], status, str(body)[:120])
    return {
        "ok": auth_ok,
        "status": status,
        "auth_ok": auth_ok,
        "endpoint": url,
        "vendor": v["name"],
        "reason": reason,
        "diff_api": v["diff_api"],
        "note": v["note"],
        "body_head": str(body)[:200],
        "diff_implemented": False,
        "capability": ("仅探测：变更清单（snapshot diff）需拿到真机后按 %s 对接"
                       % v["diff_api"]),
    }


def list_snapshot_changes(vendor: str, *args, **kwargs):
    """获取快照间的变更文件列表（**尚未实现**）。

    拿到真机后在此实现对应厂商的差量 API，并保持返回结构一致：
    ``{"added": [...], "modified": [...], "deleted": [...]}``
    （相对路径，供文件备份按增量拉取）。
    """
    v = _VENDORS.get((vendor or "").lower())
    if not v:
        raise ValueError("不支持的阵列类型: %s" % vendor)
    raise NotImplementedError(
        "NAS 快照差量尚未实现：需要在真机环境对接 %s（%s）。"
        "当前可用的替代方案是 SSH 只读指纹清单（find+sha256sum+stat）"
        "配合文件级源端去重。" % (v["diff_api"], v["name"]))
