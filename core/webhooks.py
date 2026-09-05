# -*- coding: utf-8 -*-
"""
Webhooks 事件中心（M5）：平台关键事件（备份成功/失败、恢复、克隆销毁、
到期销毁等）以 JSON POST 推送到配置的 URL 列表。

- 配置存 system_config：key=webhook_urls（逗号/换行分隔），可选
  webhook_secret（HMAC-SHA256 签名，头部 X-BP-Signature）
- 每次推送最多重试 3 次（指数退避 2s/4s/8s），异步线程不阻塞主流程
- 事件体：{"event": "...", "time": "...", "data": {...}}
"""
import hashlib
import hmac
import json
import threading
import time
import urllib.request

import core.db as db

_logger = None


def _log():
    global _logger
    if _logger is None:
        _logger = db.get_logger("webhooks")
    return _logger


def _urls_and_secret() -> tuple:
    urls = (db.get_system_config("webhook_urls") or "").strip()
    secret = (db.get_system_config("webhook_secret") or "").strip()
    if not urls:
        return [], secret
    lst = [u.strip() for u in urls.replace("\n", ",").split(",") if u.strip()]
    return lst, secret


def emit(event: str, data: dict) -> int:
    """同步推送（内部用），返回成功送达的端点数。"""
    urls, secret = _urls_and_secret()
    if not urls:
        return 0
    body = json.dumps({
        "event": event, "time": db.now_iso(), "data": data,
    }, ensure_ascii=False, default=str).encode("utf-8")
    ok = 0
    for url in urls:
        delays = [0, 2, 4, 8]
        for i, d in enumerate(delays):
            if d:
                time.sleep(d)
            try:
                req = urllib.request.Request(url, data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
                if secret:
                    sig = hmac.new(secret.encode(), body,
                                   hashlib.sha256).hexdigest()
                    req.add_header("X-BP-Signature", "sha256=" + sig)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    if resp.status < 300:
                        ok += 1
                        _log().info("[webhooks] %s -> %s 送达", event, url)
                        break
            except Exception as e:
                if i == len(delays) - 1:
                    _log().warning("[webhooks] %s -> %s 推送失败(重试%d次): %s",
                                   event, url, i, e)
                continue
    return ok


def emit_async(event: str, data: dict) -> None:
    threading.Thread(target=emit, args=(event, data), daemon=True,
                     name=f"webhook-{event}").start()


# ---------------- 恢复/克隆事件挂接辅助 ----------------
def emit_restore(record_id: int, task_name: str, db_type: str,
                 target_db: str, success: bool) -> None:
    emit_async("restore." + ("success" if success else "failure"), {
        "record_id": record_id, "task_name": task_name,
        "db_type": db_type, "target_db": target_db,
    })


def emit_clone(clone_id: int, event: str, extra: dict = None) -> None:
    d = {"clone_id": clone_id}
    if extra:
        d.update(extra)
    emit_async("clone." + event, d)
