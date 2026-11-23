# -*- coding: utf-8 -*-
"""极简 HTTP/HTTPS 客户端（仅用标准库 urllib）。

为什么不用 requests：平台最终交付形态是**完全离线环境**，不能引入新的 PyPI
依赖；PVE / vCenter 的 REST 调用只需要 GET/POST/PUT/DELETE + JSON + 自定义头，
urllib 足够，且行为可控（可关闭证书校验、可拿到原始字节流）。
"""
import json as _json
try:
    import ssl
except ImportError:
    ssl = None
import urllib.error
import urllib.parse
import urllib.request


def _ctx(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def request(method: str, url: str, *, headers: dict = None, data=None,
            json_body=None, timeout: int = 30, verify_ssl: bool = False,
            stream_to=None) -> dict:
    """发起 HTTP 请求。

    Args:
        data: 原始 bytes / str（与 json_body 二选一）
        json_body: 会被序列化为 JSON 并自动加 Content-Type
        stream_to: 文件路径，响应直接流式落盘（备份数据面下载）

    Returns:
        {"ok": bool, "status": int, "body": bytes, "json": dict|None,
         "text": str, "error": str}
    """
    hdrs = dict(headers or {})
    body = data
    if json_body is not None:
        body = _json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    if isinstance(body, str):
        body = body.encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
    out = {"ok": False, "status": 0, "body": b"", "json": None, "text": "", "error": ""}
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx(verify_ssl)) as resp:
            out["status"] = int(resp.status or 0)
            if stream_to:
                import os
                os.makedirs(os.path.dirname(stream_to) or ".", exist_ok=True)
                with open(stream_to, "wb") as f:
                    while True:
                        chunk = resp.read(4 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
            else:
                out["body"] = resp.read()
    except urllib.error.HTTPError as e:
        out["status"] = int(e.code or 0)
        try:
            out["body"] = e.read()
        except Exception:
            pass
        out["error"] = "HTTP %s" % out["status"]
    except Exception as e:
        out["error"] = str(e)[:300]
        return out

    out["text"] = out["body"].decode("utf-8", "ignore") if out["body"] else ""
    if out["body"]:
        try:
            out["json"] = _json.loads(out["text"])
        except Exception:
            out["json"] = None
    out["ok"] = 200 <= out["status"] < 300
    return out


def form_encode(params: dict) -> bytes:
    return urllib.parse.urlencode(
        {k: v for k, v in (params or {}).items() if v is not None}
    ).encode("utf-8")
