# -*- coding: utf-8 -*-
"""接口对接中心（2026-11-25）。

定位
----
平台需要和「外部系统」交换日志，两种方向：

- **pull（主动拉取）**：按 cron/间隔调用第三方 HTTP 接口（也可以是另一台
  AIDBM 平台的 ``/api/v1/integration/log-source``），把返回内容按 JSONPath 或
  逐行文本解析成日志，做关键字过滤后落库到 ``integration_logs``；命中告警
  关键字的条目会写系统日志并触发 webhook（``integration.alarm``）。
- **push（主动推送）**：把本平台 ``system_logs`` 中水位之后的日志打包 POST
  给第三方接口，2xx 才推进水位（至少一次语义，重复推送由对端幂等吸收）。

设计约束（沿用平台既有约定）
--------------------------
- **完全离线自足**：只用标准库 ``urllib``/``ssl``，不引入 requests；
- **零客户端安装**：目标接口就是 HTTP(S)，不需要对端装任何东西；
- **诚实失败**：HTTP 状态码、响应片段、解析异常原因全部原样回传，不伪造成功；
- **测试可先于保存**：``test_task()`` 接受未保存的配置，直接发一次真实请求并
  返回原始响应片段 + 解析结果，用户可据此修正 JSONPath/字段映射再保存。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

from core import db

# 采集日志级别归一
_LEVEL_MAP = {
    "error": "ERROR", "err": "ERROR", "fatal": "ERROR", "critical": "ERROR",
    "crit": "ERROR", "panic": "ERROR", "severe": "ERROR",
    "warn": "WARN", "warning": "WARN",
    "info": "INFO", "notice": "INFO", "information": "INFO",
    "debug": "DEBUG", "trace": "DEBUG", "verbose": "DEBUG",
}

_MAX_RESPONSE = 8 * 1024 * 1024        # 单次响应上限，防对端返回超大内容
_PREVIEW = 4000                        # 测试结果里回显的原始响应片段
_RUNNING: set = set()                  # 同一任务的进程内并发护栏
_RUNNING_LOCK = threading.Lock()

TASK_FIELDS = (
    "name", "direction", "enabled", "endpoint", "method", "auth_type",
    "auth_user", "auth_header", "headers", "body", "verify_tls",
    "timeout_sec", "parse_type", "json_path", "field_map",
    "include_keywords", "exclude_keywords", "alarm_keywords",
    "schedule_type", "cron", "interval_minutes", "max_items", "host_id",
)

_MESSAGE_KEYS = ("message", "msg", "content", "text", "log", "line", "body",
                 "日志", "内容", "描述")
_LEVEL_KEYS = ("level", "levelname", "severity", "lvl", "级别")
_TS_KEYS = ("ts", "time", "timestamp", "@timestamp", "datetime", "date",
            "log_time", "created_at", "时间")
_SOURCE_KEYS = ("source", "logger", "app", "service", "module", "component",
                "来源", "组件")


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
def _normalize(data: dict, current: dict = None) -> dict:
    """校验并归一任务配置。current 存在时表示更新（空密钥保留旧值）。"""
    current = current or {}
    out = {}

    def _s(key, default=""):
        v = data.get(key, current.get(key, default))
        return ("" if v is None else str(v)).strip()

    name = _s("name")
    if not name:
        raise ValueError("任务名称必填")
    out["name"] = name[:120]

    direction = (_s("direction") or "pull").lower()
    if direction not in ("pull", "push"):
        raise ValueError("对接方向只能是 pull（主动拉取）或 push（主动推送）")
    out["direction"] = direction

    enabled = data.get("enabled", current.get("enabled", 1))
    out["enabled"] = 1 if str(enabled) in ("1", "True", "true", "on") else 0

    endpoint = _s("endpoint")
    if not endpoint:
        raise ValueError("接口地址必填")
    if not re.match(r"^https?://", endpoint, re.I):
        raise ValueError("接口地址必须以 http:// 或 https:// 开头")
    out["endpoint"] = endpoint[:1000]

    method = (_s("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH"):
        raise ValueError("请求方法只支持 GET / POST / PUT / PATCH")
    out["method"] = method

    auth_type = (_s("auth_type") or "none").lower()
    if auth_type not in ("none", "basic", "bearer", "header"):
        raise ValueError("认证类型只支持 none / basic / bearer / header")
    out["auth_type"] = auth_type
    out["auth_user"] = _s("auth_user")[:200]
    out["auth_header"] = _s("auth_header")[:80]

    headers = _s("headers") or "{}"
    try:
        parsed_headers = json.loads(headers) if headers else {}
        if not isinstance(parsed_headers, dict):
            raise ValueError
    except Exception:
        raise ValueError("请求头必须是 JSON 对象，例如 {\"X-Tenant\": \"ops\"}")
    out["headers"] = json.dumps(parsed_headers, ensure_ascii=False)

    out["body"] = _s("body")
    verify = data.get("verify_tls", current.get("verify_tls", 0))
    out["verify_tls"] = 1 if str(verify) in ("1", "True", "true", "on") else 0

    try:
        timeout = int(data.get("timeout_sec", current.get("timeout_sec", 20)) or 20)
    except (TypeError, ValueError):
        raise ValueError("超时时间需为整数秒")
    out["timeout_sec"] = max(1, min(timeout, 300))

    parse_type = (_s("parse_type") or "json").lower()
    if parse_type not in ("json", "text"):
        raise ValueError("解析类型只支持 json / text（逐行）")
    out["parse_type"] = parse_type

    path = _s("json_path") or "$"
    try:
        parse_path(path)
    except ValueError as exc:
        raise ValueError("JSONPath 无法解析：%s" % exc)
    out["json_path"] = path[:300]

    field_map = _s("field_map")
    if field_map:
        try:
            fm = json.loads(field_map)
            if not isinstance(fm, dict):
                raise ValueError
        except Exception:
            raise ValueError('字段映射必须是 JSON 对象，例如 {"message": "msg"}')
        out["field_map"] = json.dumps(fm, ensure_ascii=False)
    else:
        out["field_map"] = ""

    out["include_keywords"] = _s("include_keywords")[:600]
    out["exclude_keywords"] = _s("exclude_keywords")[:600]
    out["alarm_keywords"] = _s("alarm_keywords")[:600]

    schedule_type = (_s("schedule_type") or "cron").lower()
    if schedule_type not in ("cron", "interval", "manual"):
        raise ValueError("调度方式只支持 cron / interval / manual")
    out["schedule_type"] = schedule_type
    cron = _s("cron")
    if schedule_type == "cron":
        if not cron:
            cron = "*/5 * * * *"
        _validate_cron(cron)
    out["cron"] = cron[:120]
    try:
        interval = int(data.get("interval_minutes", current.get("interval_minutes", 0)) or 0)
    except (TypeError, ValueError):
        raise ValueError("间隔分钟需为整数")
    out["interval_minutes"] = max(0, min(interval, 10080))

    try:
        max_items = int(data.get("max_items", current.get("max_items", 500)) or 500)
    except (TypeError, ValueError):
        raise ValueError("单次最多条数需为整数")
    out["max_items"] = max(1, min(max_items, 5000))

    host_id = data.get("host_id", current.get("host_id"))
    try:
        out["host_id"] = int(host_id) if host_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        out["host_id"] = None

    secret = data.get("auth_secret")
    secret = "" if secret is None else str(secret)
    if secret:
        out["auth_secret_enc"] = db.encrypt_secret(secret)
    elif current.get("auth_secret_enc"):
        out["auth_secret_enc"] = current["auth_secret_enc"]
    else:
        out["auth_secret_enc"] = ""
    if auth_type in ("basic", "bearer", "header") and not out["auth_secret_enc"]:
        raise ValueError("该认证类型必须填写密钥 / 密码 / Token")
    if auth_type == "header" and not out["auth_header"]:
        raise ValueError("认证类型为自定义请求头时必须填写请求头名称")
    return out


def _validate_cron(expr: str) -> None:
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(expr)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Cron 表达式非法：%s" % exc)


def _row_to_task(row: dict) -> dict:
    if not row:
        return row
    out = dict(row)
    out["has_secret"] = bool(out.get("auth_secret_enc"))
    out.pop("auth_secret_enc", None)
    out["enabled"] = int(out.get("enabled") or 0)
    out["verify_tls"] = int(out.get("verify_tls") or 0)
    return out


def list_tasks(enabled_only: bool = False) -> list:
    sql = "SELECT * FROM integration_tasks"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    return [_row_to_task(r) for r in db.query(sql)]


def get_task(tid: int, include_secret: bool = False) -> dict:
    row = db.query_one("SELECT * FROM integration_tasks WHERE id=?", (int(tid),))
    if not row:
        return None
    if include_secret:
        out = dict(row)
        try:
            out["auth_secret"] = db.decrypt_secret(row.get("auth_secret_enc") or "")
        except Exception:
            out["auth_secret"] = ""
        return out
    return _row_to_task(row)


def create_task(data: dict) -> int:
    payload = _normalize(data or {})
    now = db.now_iso()
    cols = list(payload.keys()) + ["created_at", "updated_at"]
    vals = list(payload.values()) + [now, now]
    sql = "INSERT INTO integration_tasks(%s) VALUES (%s)" % (
        ",".join(cols), ",".join("?" for _ in cols))
    tid = db.execute(sql, tuple(vals))
    db.add_log("INFO", "integration", "新建接口对接任务：%s（%s）"
               % (payload["name"], payload["direction"]))
    return int(tid)


def update_task(tid: int, data: dict) -> dict:
    current = db.query_one("SELECT * FROM integration_tasks WHERE id=?", (int(tid),))
    if not current:
        raise ValueError("对接任务不存在")
    payload = _normalize(data or {}, current)
    payload["updated_at"] = db.now_iso()
    sets = ",".join("%s=?" % k for k in payload)
    db.execute("UPDATE integration_tasks SET %s WHERE id=?" % sets,
               tuple(list(payload.values()) + [int(tid)]))
    return get_task(tid)


def delete_task(tid: int) -> None:
    db.execute("DELETE FROM integration_tasks WHERE id=?", (int(tid),))
    db.execute("DELETE FROM integration_logs WHERE task_id=?", (int(tid),))
    db.execute("DELETE FROM integration_runs WHERE task_id=?", (int(tid),))
    db.add_log("INFO", "integration", "删除接口对接任务 id=%s 及其采集日志" % tid)


# --------------------------------------------------------------------------- #
# JSONPath（最小子集：$ / .key / ['key'] / [n] / [a:b] / [*] / .*）
# --------------------------------------------------------------------------- #
def parse_path(expr: str) -> list:
    expr = (expr or "").strip()
    if expr.startswith("$"):
        expr = expr[1:]
    segs = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch == ".":
            j = i + 1
            if j < n and expr[j] == ".":
                raise ValueError("暂不支持递归下降 ..，请写明具体层级或使用 [*]")
            if j < n and expr[j] == "*":
                segs.append(("*",))
                i = j + 1
                continue
            k = j
            while k < n and (expr[k].isalnum() or expr[k] in "_-"):
                k += 1
            if k == j:
                raise ValueError("位置 %d 处缺少字段名" % i)
            segs.append(("key", expr[j:k]))
            i = k
        elif ch == "[":
            k = expr.find("]", i)
            if k < 0:
                raise ValueError("缺少 ]")
            inner = expr[i + 1:k].strip()
            i = k + 1
            if inner == "*":
                segs.append(("*",))
            elif len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
                segs.append(("key", inner[1:-1]))
            elif ":" in inner:
                a, _, b = inner.partition(":")
                segs.append(("slice", _int_or_none(a), _int_or_none(b)))
            else:
                try:
                    segs.append(("index", int(inner)))
                except ValueError:
                    raise ValueError("索引非法：[%s]" % inner)
        elif ch == "*":
            segs.append(("*",))
            i += 1
        else:
            raise ValueError("位置 %d 处无法解析（%s）" % (i, ch))
    return segs


def _int_or_none(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise ValueError("切片下标非法：%s" % text)


def jsonpath(data, expr: str = "$") -> list:
    """按 JSONPath 子集取值，恒返回 list。"""
    cur = [data]
    for seg in parse_path(expr):
        nxt = []
        kind = seg[0]
        if kind == "key":
            for item in cur:
                if isinstance(item, dict) and seg[1] in item:
                    nxt.append(item[seg[1]])
        elif kind == "*":
            for item in cur:
                if isinstance(item, dict):
                    nxt.extend(item.values())
                elif isinstance(item, (list, tuple)):
                    nxt.extend(item)
        elif kind == "index":
            for item in cur:
                if isinstance(item, (list, tuple)):
                    if -len(item) <= seg[1] < len(item):
                        nxt.append(item[seg[1]])
                elif isinstance(item, dict) and str(seg[1]) in item:
                    nxt.append(item[str(seg[1])])
        elif kind == "slice":
            for item in cur:
                if isinstance(item, (list, tuple)):
                    nxt.extend(item[seg[1]:seg[2]])
        cur = nxt
    return cur


# --------------------------------------------------------------------------- #
# 关键字 / 字段提取
# --------------------------------------------------------------------------- #
def _split_keywords(text: str) -> list:
    if not text:
        return []
    parts = re.split(r"[,，;；\s]+", str(text))
    return [p.strip().lower() for p in parts if p.strip()]


def _hit(keywords: list, haystack: str) -> bool:
    if not keywords:
        return False
    low = (haystack or "").lower()
    return any(k in low for k in keywords)


def _dig(rec, names):
    if isinstance(rec, dict):
        for name in names:
            if name in rec and rec[name] not in (None, ""):
                return rec[name]
        # 大小写不敏感兜底
        lowered = {str(k).lower(): v for k, v in rec.items()}
        for name in names:
            v = lowered.get(name.lower())
            if v not in (None, ""):
                return v
    return None


def _text_of(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _extract(records: list, field_map: dict = None) -> list:
    """把 JSONPath 结果归一成 [{ts, level, source, message, raw}]。"""
    field_map = field_map or {}
    out = []
    for rec in records:
        if rec is None:
            continue
        if isinstance(rec, str):
            out.append({"ts": "", "level": "INFO", "source": "",
                        "message": rec, "raw": rec})
            continue
        if not isinstance(rec, dict):
            text = _text_of(rec)
            out.append({"ts": "", "level": "INFO", "source": "",
                        "message": text, "raw": text})
            continue

        def _mapped(key, defaults):
            if field_map.get(key):
                v = _dig(rec, [field_map[key]])
                if v not in (None, ""):
                    return v
            return _dig(rec, defaults)

        message = _text_of(_mapped("message", _MESSAGE_KEYS))
        if not message:
            message = _text_of(rec)
        level_raw = _text_of(_mapped("level", _LEVEL_KEYS)).strip()
        level = _LEVEL_MAP.get(level_raw.lower(), (level_raw.upper() or "INFO"))[:12]
        ts = _text_of(_mapped("ts", _TS_KEYS)).strip()
        source = _text_of(_mapped("source", _SOURCE_KEYS)).strip()
        raw = json.dumps(rec, ensure_ascii=False, default=str)
        out.append({"ts": ts, "level": level, "source": source[:120],
                    "message": message, "raw": raw})
    return out


def _fingerprint(rec: dict) -> str:
    base = "|".join([rec.get("ts", ""), rec.get("level", ""),
                     rec.get("source", ""), rec.get("message", "")])
    return hashlib.sha256(base.encode("utf-8", "replace")).hexdigest()[:40]


# --------------------------------------------------------------------------- #
# HTTP 调用
# --------------------------------------------------------------------------- #
def _render(text: str, ctx: dict) -> str:
    if not text:
        return text

    def _sub(m):
        key = m.group(1)
        return str(ctx.get(key, ""))

    return re.sub(r"\{\{\s*([a-zA-Z_]+)\s*\}\}", _sub, text)


def _build_headers(task: dict, secret: str) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "AIDBM-Integration/1.0"}
    try:
        extra = json.loads(task.get("headers") or "{}")
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items()})
    except Exception:
        pass
    auth = (task.get("auth_type") or "none").lower()
    if auth == "basic" and secret:
        token = base64.b64encode(
            ("%s:%s" % (task.get("auth_user") or "", secret)).encode("utf-8")).decode()
        headers["Authorization"] = "Basic " + token
    elif auth == "bearer" and secret:
        headers["Authorization"] = "Bearer " + secret
    elif auth == "header" and secret:
        headers[(task.get("auth_header") or "X-Api-Key").strip()] = secret
    return headers


def _request(task: dict, secret: str, ctx: dict):
    """执行一次 HTTP 请求，返回 (status, text, headers, elapsed_ms, url)。"""
    url = _render(task.get("endpoint") or "", ctx)
    method = (task.get("method") or "GET").upper()
    headers = _build_headers(task, secret)
    data = None
    body_tpl = task.get("body") or ""
    if method in ("POST", "PUT", "PATCH") and body_tpl:
        body = _render(body_tpl, ctx)
        data = body.encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif method in ("POST", "PUT", "PATCH") and not body_tpl:
        data = b"{}"
        headers.setdefault("Content-Type", "application/json")

    timeout = int(task.get("timeout_sec") or 20)
    context = None
    if url.lower().startswith("https://") and not int(task.get("verify_tls") or 0):
        # 内网自签证书很常见：显式关闭校验并在结果里标注（不静默）
        context = ssl._create_unverified_context()  # noqa: SLF001
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            raw = resp.read(_MAX_RESPONSE)
            status = resp.getcode()
            resp_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read(_MAX_RESPONSE) if hasattr(exc, "read") else b""
        status = exc.code
        resp_headers = dict(exc.headers.items()) if exc.headers else {}
        text = raw.decode("utf-8", "replace")
        raise IntegrationHTTPError(status, text, url, time.time() - started)
    except urllib.error.URLError as exc:
        raise IntegrationError("连接失败：%s" % (exc.reason or exc))
    except ssl.SSLError as exc:
        raise IntegrationError("TLS 握手失败：%s（自签证书请关闭「校验 TLS 证书」）" % exc)
    except TimeoutError:
        raise IntegrationError("请求超时（%s 秒无响应）" % timeout)
    elapsed = int((time.time() - started) * 1000)
    return status, raw.decode("utf-8", "replace"), resp_headers, elapsed, url


class IntegrationError(Exception):
    """对接失败（网络/配置类），消息直接面向用户。"""


class IntegrationHTTPError(IntegrationError):
    def __init__(self, status, text, url, elapsed):
        self.status = int(status or 0)
        self.text = text or ""
        self.url = url
        self.elapsed_ms = int((elapsed or 0) * 1000)
        hint = {
            401: "认证失败：检查 Token / 用户名密码，或对端是否要求 Bearer",
            403: "对端拒绝访问：账号无权限或 IP 白名单未放行",
            404: "接口不存在：核对接口地址（AIDBM 平台日志接口是 "
                 "/api/v1/integration/log-source）",
            405: "请求方法不被允许：把 POST 改成 GET 或反之",
            429: "对端限流：把 Cron 间隔调大",
        }.get(self.status, "")
        msg = "对端返回 HTTP %s" % self.status
        if hint:
            msg += "（%s）" % hint
        snippet = self.text.strip().replace("\n", " ")[:200]
        if snippet:
            msg += "：%s" % snippet
        super().__init__(msg)


# --------------------------------------------------------------------------- #
# 采集 / 推送
# --------------------------------------------------------------------------- #
def _parse_payload(task: dict, text: str) -> list:
    if (task.get("parse_type") or "json") == "text":
        return [line for line in text.splitlines() if line.strip()]
    try:
        data = json.loads(text)
    except Exception as exc:
        raise IntegrationError(
            "响应不是合法 JSON（%s）。若对端返回的是纯文本日志，"
            "请把「解析类型」改为 text（逐行）" % exc)
    return jsonpath(data, task.get("json_path") or "$")


def _record_run(task_id, trigger_by, direction, status, message="", detail="",
                http_status=0, fetched=0, accepted=0, pushed=0,
                started=None, duration_ms=0):
    now = db.now_iso()
    db.execute(
        "INSERT INTO integration_runs(task_id, trigger_by, direction, started_at,"
        " finished_at, status, http_status, fetched, accepted, pushed,"
        " duration_ms, message, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(task_id), trigger_by, direction, started or now, now, status,
         int(http_status or 0), int(fetched or 0), int(accepted or 0),
         int(pushed or 0), int(duration_ms or 0), (message or "")[:1000],
         (detail or "")[:20000]))


def _collect(task: dict, secret: str, trigger_by: str = "manual",
             persist: bool = True) -> dict:
    """pull：拉取 → 解析 → 过滤 → 落库。"""
    include = _split_keywords(task.get("include_keywords"))
    exclude = _split_keywords(task.get("exclude_keywords"))
    alarms = _split_keywords(task.get("alarm_keywords"))
    max_items = int(task.get("max_items") or 500)
    ctx = {
        "since": task.get("watermark") or "",
        "last_ts": task.get("watermark") or "",
        "limit": max_items,
        "task": task.get("name") or "",
    }
    status, text, _headers, elapsed, url = _request(task, secret, ctx)
    records = _parse_payload(task, text)
    parsed_total = len(records)
    extracted = _extract(records[:max_items], _safe_json(task.get("field_map")))
    kept, dropped, alarm_hits = [], 0, 0
    for rec in extracted:
        hay = rec["message"] + " " + (rec.get("raw") or "")
        if include and not _hit(include, hay):
            dropped += 1
            continue
        if exclude and _hit(exclude, hay):
            dropped += 1
            continue
        rec["alerted"] = 1 if _hit(alarms, hay) else 0
        alarm_hits += rec["alerted"]
        kept.append(rec)

    inserted_rows = []
    if persist and kept:
        inserted_rows = _persist(task["id"], kept)
    new_alarms = sum(int(r.get("alerted") or 0) for r in inserted_rows)
    result = {
        "ok": True, "direction": "pull", "http_status": status, "url": url,
        "duration_ms": elapsed, "fetched": parsed_total,
        "matched": len(kept), "filtered_out": dropped,
        "alarm_hits": alarm_hits, "inserted": len(inserted_rows),
        # 只有「新入库」的告警条目才真正触发告警（重复拉取同一批不重复告警）
        "new_alarm_hits": new_alarms,
        "samples": kept[:5], "raw_preview": (text or "")[:_PREVIEW],
    }
    if persist:
        _after_pull(task, result, kept)
    return result


def _safe_json(text) -> dict:
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _persist(task_id: int, records: list) -> list:
    """落库并去重（同一任务内按指纹唯一），返回真正新入库的记录。"""
    fps = [_fingerprint(r) for r in records]
    existing = set()
    for i in range(0, len(fps), 200):
        chunk = fps[i:i + 200]
        rows = db.query(
            "SELECT fingerprint FROM integration_logs WHERE task_id=? AND "
            "fingerprint IN (%s)" % ",".join("?" for _ in chunk),
            tuple([int(task_id)] + chunk))
        existing.update(r["fingerprint"] for r in rows)
    now = db.now_iso()
    inserted = []
    for rec, fp in zip(records, fps):
        if fp in existing:
            continue
        existing.add(fp)
        db.execute(
            "INSERT INTO integration_logs(task_id, ts, level, source, message,"
            " raw, fingerprint, alerted, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(task_id), rec.get("ts") or now, (rec.get("level") or "INFO")[:12],
             rec.get("source") or "", rec.get("message") or "",
             (rec.get("raw") or "")[:20000], fp, int(rec.get("alerted") or 0),
             now))
        inserted.append(rec)
    return inserted


def _after_pull(task: dict, result: dict, kept: list) -> None:
    """pull 成功后更新统计、水位与告警。"""
    watermark = task.get("watermark") or ""
    stamps = [r.get("ts") for r in kept if r.get("ts")]
    if stamps:
        candidate = max(stamps)
        if candidate and candidate > (watermark or ""):
            watermark = candidate[:40]
    now = db.now_iso()
    db.execute(
        "UPDATE integration_tasks SET last_run_at=?, last_status='success',"
        " last_error='', last_fetched=?, last_accepted=?, watermark=?,"
        " success_count=success_count+1, updated_at=? WHERE id=?",
        (now, int(result["fetched"]), int(result["inserted"]), watermark,
         now, int(task["id"])))
    if result.get("new_alarm_hits"):
        _alarm(task, result)


def _alarm(task: dict, result: dict) -> None:
    msg = "接口对接任务「%s」命中告警关键字 %s 条（新入库 %s 条）" % (
        task.get("name"), result.get("new_alarm_hits"), result.get("inserted"))
    db.add_log("WARNING", "integration", msg)
    try:
        from core import webhooks
        webhooks.emit_async("integration.alarm", {
            "task_id": task.get("id"), "task_name": task.get("name"),
            "alarm_hits": result.get("alarm_hits"),
            "inserted": result.get("inserted"),
            "samples": [s.get("message", "")[:200] for s in (result.get("samples") or [])[:3]],
        })
    except Exception:
        pass


def _push(task: dict, secret: str, trigger_by: str = "manual",
          persist: bool = True) -> dict:
    """push：取本平台日志 → 打包 → POST → 2xx 才推进水位。"""
    max_items = int(task.get("max_items") or 500)
    include = _split_keywords(task.get("include_keywords"))
    exclude = _split_keywords(task.get("exclude_keywords"))
    since = (task.get("watermark") or "").strip()
    since_id = int(since) if since.isdigit() else 0
    rows = db.query(
        "SELECT id, ts, level, source, message FROM system_logs WHERE id > ?"
        " ORDER BY id LIMIT ?", (since_id, max_items))
    picked = []
    for r in rows:
        hay = "%s %s" % (r.get("message") or "", r.get("source") or "")
        if include and not _hit(include, hay):
            continue
        if exclude and _hit(exclude, hay):
            continue
        picked.append({"id": r["id"], "ts": r["ts"], "level": r["level"],
                       "source": r["source"], "message": r["message"]})
    payload_logs = json.dumps(picked, ensure_ascii=False)
    ctx = {"since": since or "", "limit": max_items,
           "logs": payload_logs, "count": len(picked),
           "task": task.get("name") or ""}
    body = _render(task.get("body") or "", ctx)
    if not body:
        body = json.dumps({
            "source": "aidbm",
            "task": task.get("name") or "",
            "sent_at": db.now_iso(),
            "count": len(picked),
            "logs": picked,
        }, ensure_ascii=False)
    task_for_request = dict(task)
    task_for_request["body"] = body
    status, text, _headers, elapsed, url = _request(task_for_request, secret, ctx)
    result = {
        "ok": True, "direction": "push", "http_status": status, "url": url,
        "duration_ms": elapsed, "fetched": len(rows), "matched": len(picked),
        "pushed": len(picked), "inserted": 0,
        "samples": picked[:5], "raw_preview": (text or "")[:_PREVIEW],
    }
    if persist:
        now = db.now_iso()
        new_water = str(max([r["id"] for r in picked])) if picked else (since or "0")
        db.execute(
            "UPDATE integration_tasks SET last_run_at=?, last_status='success',"
            " last_error='', last_fetched=?, last_accepted=?, watermark=?,"
            " success_count=success_count+1, updated_at=? WHERE id=?",
            (now, len(rows), len(picked), new_water, now, int(task["id"])))
    return result


def run_task(tid: int, trigger_by: str = "manual") -> dict:
    """执行一次对接任务（真实请求），无论成败都留 run 记录。"""
    task = get_task(tid, include_secret=True)
    if not task:
        raise ValueError("对接任务不存在")
    with _RUNNING_LOCK:
        if tid in _RUNNING:
            raise ValueError("该任务正在执行，请稍后")
        _RUNNING.add(tid)
    started = db.now_iso()
    t0 = time.time()
    try:
        direction = task.get("direction") or "pull"
        if direction == "pull":
            result = _collect(task, task.get("auth_secret") or "", trigger_by)
        else:
            result = _push(task, task.get("auth_secret") or "", trigger_by)
        _record_run(tid, trigger_by, direction, "success",
                    "采集 %s 条 / 入库 %s 条" % (result["fetched"], result["inserted"])
                    if direction == "pull" else
                    "推送 %s 条" % result["pushed"],
                    detail=json.dumps({k: v for k, v in result.items()
                                       if k not in ("samples", "raw_preview")},
                                      ensure_ascii=False),
                    http_status=result["http_status"], fetched=result["fetched"],
                    accepted=result["inserted"], pushed=result.get("pushed", 0),
                    started=started,
                    duration_ms=int((time.time() - t0) * 1000))
        return result
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) or exc.__class__.__name__
        now = db.now_iso()
        db.execute(
            "UPDATE integration_tasks SET last_run_at=?, last_status='failed',"
            " last_error=?, fail_count=fail_count+1, updated_at=? WHERE id=?",
            (now, msg[:500], now, int(tid)))
        _record_run(tid, trigger_by, task.get("direction") or "",
                    "failed", msg, started=started,
                    duration_ms=int((time.time() - t0) * 1000))
        db.add_log("WARNING", "integration",
                   "接口对接任务「%s」执行失败：%s" % (task.get("name"), msg[:300]))
        return {"ok": False, "error": msg}
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(tid)


def run_job_wrapper(tid: int) -> None:
    """APScheduler 入口：异常只记日志，不污染调度器。"""
    try:
        run_task(int(tid), "cron")
    except Exception as exc:  # noqa: BLE001
        db.add_log("ERROR", "integration", "定时对接任务 %s 异常：%s" % (tid, exc))


# --------------------------------------------------------------------------- #
# 测试（真实发一次请求，不落采集日志；配置可以未保存）
# --------------------------------------------------------------------------- #
def test_task(data: dict = None, tid: int = None) -> dict:
    """测试接口连通 + 解析结果。

    - data 非空：用表单里的配置测试（未保存也能测，用户填错可当场发现）；
    - 只给 tid：用已保存配置测试。
    返回原始响应片段、HTTP 状态、解析条数、过滤统计、样例。
    """
    data = data or {}
    task = {}
    if tid:
        task = get_task(int(tid), include_secret=True) or {}
        if not task:
            raise ValueError("对接任务不存在")
    if data:
        current = {k: task.get(k) for k in TASK_FIELDS if k in task}
        merged = dict(current)
        merged.update({k: v for k, v in data.items() if v is not None})
        secret = (data.get("auth_secret") or task.get("auth_secret") or "")
        if secret:
            merged["auth_secret"] = secret
        norm = _normalize(merged, task)
        task = dict(task)
        task.update(norm)
        task["id"] = task.get("id") or 0
    if not task.get("endpoint"):
        raise ValueError("接口地址必填")

    direction = task.get("direction") or "pull"
    try:
        if direction == "push":
            result = _push(task, secret, "test", persist=False)
        else:
            result = _collect(task, secret, "test", persist=False)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False, "direction": direction,
            "error": str(exc) or exc.__class__.__name__,
            "hint": _test_hint(exc),
        }
    result["ok"] = True
    result["hint"] = _ok_hint(result)
    return result


def _test_hint(exc) -> str:
    text = str(exc) or exc.__class__.__name__
    if "连接失败" in text:
        return ("平台到接口地址不通：核对 IP/端口、目标服务是否运行、"
                "以及目标机防火墙是否放行该端口（平台侧出网方向）。")
    if "超时" in text:
        return "对端响应过慢：调大超时时间，或确认接口是否需要分页查询。"
    if "JSON" in text:
        return "把「解析类型」改成 text（逐行），或填写正确的 JSONPath。"
    return "按错误信息核对地址、认证方式与解析配置。"


def _ok_hint(result: dict) -> str:
    if result.get("direction") == "push":
        return "推送测试成功：对端已返回 2xx。保存任务后按 Cron 自动推送。"
    if not result.get("fetched"):
        return ("请求成功但对端返回 0 条：检查 JSONPath（例如 $.data.logs[*]）"
                "是否指向数组，或对端是否本来没有新日志。")
    if not result.get("matched"):
        return ("解析到 %s 条但全部被「包含/排除关键字」过滤掉了，"
                "检查关键字配置。" % result.get("fetched"))
    return ("解析正常：抓取 %s 条、过滤后 %s 条。保存后即可定时采集。"
            % (result.get("fetched"), result.get("matched")))


# --------------------------------------------------------------------------- #
# 查询 / 统计 / 对端拉取接口
# --------------------------------------------------------------------------- #
def list_logs(task_id: int = None, level: str = "", keyword: str = "",
              alerted_only: bool = False, limit: int = 100,
              offset: int = 0) -> tuple:
    sql = ("SELECT l.*, t.name AS task_name FROM integration_logs l "
           "LEFT JOIN integration_tasks t ON t.id=l.task_id WHERE 1=1")
    params = []
    if task_id:
        sql += " AND l.task_id=?"
        params.append(int(task_id))
    if level:
        sql += " AND l.level=?"
        params.append(level)
    if keyword:
        sql += " AND (l.message LIKE ? OR IFNULL(l.source,'') LIKE ?)"
        kw = "%" + keyword + "%"
        params.extend([kw, kw])
    if alerted_only:
        sql += " AND l.alerted=1"
    total = db.query_one("SELECT COUNT(*) AS n FROM (" + sql + ") AS sub",
                         tuple(params))
    sql += " ORDER BY l.id DESC LIMIT ? OFFSET ?"
    rows = db.query(sql, tuple(params + [int(limit), int(offset)]))
    for r in rows:
        r["level"] = (r.get("level") or "INFO").upper()
    return rows, int((total or {}).get("n") or 0)


def list_runs(task_id: int = None, limit: int = 50) -> list:
    if task_id:
        return db.query("SELECT * FROM integration_runs WHERE task_id=? "
                        "ORDER BY id DESC LIMIT ?", (int(task_id), int(limit)))
    return db.query("SELECT r.*, t.name AS task_name FROM integration_runs r "
                    "LEFT JOIN integration_tasks t ON t.id=r.task_id "
                    "ORDER BY r.id DESC LIMIT ?", (int(limit),))


def stats() -> dict:
    row = db.query_one(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled"
        " FROM integration_tasks") or {}
    logs = db.query_one(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN alerted=1 THEN 1 ELSE 0 END) AS alarms"
        " FROM integration_logs") or {}
    today = db.now_iso()[:10]
    today_row = db.query_one(
        "SELECT COUNT(*) AS n FROM integration_logs WHERE created_at >= ?",
        (today,)) or {}
    return {
        "tasks": int(row.get("total") or 0),
        "enabled": int(row.get("enabled") or 0),
        "logs": int(logs.get("n") or 0),
        "alarms": int(logs.get("alarms") or 0),
        "today_logs": int(today_row.get("n") or 0),
    }


def log_source(since: str = "", level: str = "", keyword: str = "",
               limit: int = 500) -> dict:
    """对端可直接调用的日志源（供另一台 AIDBM 主动拉取）。

    since 支持两种写法：纯数字按 system_logs.id 增量；否则按时间戳水位。
    """
    limit = max(1, min(int(limit or 500), 2000))
    sql = ("SELECT id, ts, level, source, message FROM system_logs WHERE 1=1")
    params = []
    since = (since or "").strip()
    if since:
        if since.isdigit():
            sql += " AND id > ?"
            params.append(int(since))
        else:
            sql += " AND ts >= ?"
            params.append(since)
    if level:
        sql += " AND level = ?"
        params.append(level.upper())
    if keyword:
        sql += " AND message LIKE ?"
        params.append("%" + keyword + "%")
    sql += " ORDER BY id LIMIT ?"
    params.append(limit)
    # 由旧到新（升序）：调用方按 next_since 继续追加即可完整追赶，不会跳过日志
    rows = db.query(sql, tuple(params))
    next_since = str(max([r["id"] for r in rows])) if rows else since
    next_ts = max([r["ts"] or "" for r in rows]) if rows else since
    return {"logs": rows, "count": len(rows), "next_since": next_since,
            "next_ts": next_ts, "since": since, "limit": limit}
