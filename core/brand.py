# -*- coding: utf-8 -*-
"""品牌设置（登录页 Logo / 平台侧栏 Logo / 品牌名称 / 首页大标题）。

设计要点：
- 图片落 ``INSTANCE_DIR/brand/<kind>.<ext>``（持久化目录，Docker 卷/冻结包都保留），
  不写 static/（PyInstaller 冻结后 static 只读，且升级会覆盖）。
- 元数据存 system_config（SQLite/PG/MySQL 任一后端都跟随元数据库），
  记录文件名与更新时间戳，URL 带 ``?v=<ts>`` 防浏览器缓存旧图。
- 只接受 PNG/JPEG/WEBP 且按魔数校验（不接受 SVG：内联脚本会绕过 CSP 造成 XSS）；
  单张 ≤ 2MB。
- 文字限 32 字（与界面计数一致），空值表示回落默认。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional, Tuple

import config
from core import db

# 默认值：未做任何自定义时，页面与升级前完全一致
DEFAULTS = {
    "name": "AIDBM",       # 侧栏品牌名称
    "title": "仪表盘",      # 首页大标题
}

MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT_LEN = 32
KINDS = ("login", "sidebar")
KIND_LABELS = {"login": "登录页 Logo", "sidebar": "页面左上角 Logo"}

# (扩展名, mime, 魔数判定)
_SNIFFERS = (
    ("png", "image/png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    ("jpg", "image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    ("webp", "image/webp",
     lambda b: len(b) > 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
)
_MIME = {ext: mime for ext, mime, _ in _SNIFFERS}


# ---------------------------------------------------------------------------
# 路径与读取
# ---------------------------------------------------------------------------

def brand_dir() -> Path:
    d = Path(config.INSTANCE_DIR) / "brand"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cfg(key: str, default: str = "") -> str:
    try:
        v = db.get_system_config(key)
    except Exception:  # noqa: BLE001 - 元数据库尚未就绪时回落默认
        return default
    return default if v is None else str(v)


def _sniff(raw: bytes) -> Optional[Tuple[str, str]]:
    """按魔数判定图片类型，返回 (ext, mime)；不支持的类型返回 None。"""
    for ext, mime, test in _SNIFFERS:
        try:
            if test(raw):
                return ext, mime
        except Exception:  # noqa: BLE001
            continue
    return None


def asset_file(kind: str) -> Optional[Path]:
    """当前生效的图片文件路径（未设置或文件丢失返回 None）。"""
    if kind not in KINDS:
        return None
    fname = _cfg("brand_%s_logo" % kind)
    if not fname:
        return None
    p = brand_dir() / os.path.basename(fname)
    return p if p.is_file() else None


def asset_url(kind: str) -> str:
    """模板/API 用的可访问 URL（带版本戳；未设置返回空串）。"""
    if kind not in KINDS or not _cfg("brand_%s_logo" % kind):
        return ""
    ver = _cfg("brand_%s_logo_at" % kind) or "0"
    return "/brand/asset/%s?v=%s" % (kind, ver)


def asset_mime(kind: str) -> str:
    p = asset_file(kind)
    if not p:
        return "application/octet-stream"
    return _MIME.get(p.suffix.lstrip(".").lower(), "application/octet-stream")


def get_brand() -> dict:
    """一次性返回模板与设置页所需的全部品牌信息。"""
    has_login = bool(asset_file("login"))
    has_sidebar = bool(asset_file("sidebar"))
    return {
        "name": _cfg("brand_name") or DEFAULTS["name"],
        "title": _cfg("brand_title") or DEFAULTS["title"],
        "login_logo": asset_url("login") if has_login else "",
        "sidebar_logo": asset_url("sidebar") if has_sidebar else "",
        "has_login_logo": has_login,
        "has_sidebar_logo": has_sidebar,
        "defaults": dict(DEFAULTS),
        "max_bytes": MAX_BYTES,
        "max_text_len": MAX_TEXT_LEN,
    }


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def save_logo(kind: str, raw: bytes) -> dict:
    """保存上传的 Logo。非法输入抛 ValueError（API 转 400）。"""
    if kind not in KINDS:
        raise ValueError("未知的 Logo 位置: %s（可选 %s）" % (kind, " / ".join(KINDS)))
    if not raw:
        raise ValueError("上传内容为空")
    if len(raw) > MAX_BYTES:
        raise ValueError("图片超过 %d MB 上限（当前 %.2f MB）"
                         % (MAX_BYTES // 1024 // 1024, len(raw) / 1024.0 / 1024.0))
    sniffed = _sniff(raw)
    if not sniffed:
        raise ValueError("不支持的图片格式（仅支持 PNG / JPG / WEBP）")
    ext, _mime = sniffed

    d = brand_dir()
    fname = "%s.%s" % (kind, ext)
    # 换格式时清掉旧扩展名的残留，保证一个位置只有一份文件
    for old in list(d.iterdir()):
        if old.is_file() and old.stem == kind and old.suffix.lstrip(".").lower() != ext:
            try:
                old.unlink()
            except Exception:  # noqa: BLE001
                pass
    tmp = d / (fname + ".tmp")
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, d / fname)

    db.set_system_config("brand_%s_logo" % kind, fname)
    db.set_system_config("brand_%s_logo_at" % kind, str(int(time.time())))
    return {"ok": True, "kind": kind, "ext": ext, "bytes": len(raw),
            "url": asset_url(kind),
            "message": "%s 已更新（%s，%.0f KB）"
                       % (KIND_LABELS[kind], ext.upper(), len(raw) / 1024.0)}


def reset_logo(kind: str) -> dict:
    """恢复默认（删除自定义图片，回落内置图标）。"""
    if kind not in KINDS:
        raise ValueError("未知的 Logo 位置: %s" % kind)
    p = asset_file(kind)
    if p:
        try:
            p.unlink()
        except Exception as e:  # noqa: BLE001
            raise ValueError("删除图片失败: %s" % e)
    db.set_system_config("brand_%s_logo" % kind, "")
    db.set_system_config("brand_%s_logo_at" % kind, str(int(time.time())))
    return {"ok": True, "kind": kind, "message": "%s 已恢复默认" % KIND_LABELS[kind]}


def save_text(name=None, title=None) -> dict:
    """保存品牌文字（None=不改；空串=恢复默认）。"""
    changed = []
    for key, label, cfg_key, default in (
            ("name", "侧栏品牌名称", "brand_name", DEFAULTS["name"]),
            ("title", "首页大标题", "brand_title", DEFAULTS["title"])):
        val = {"name": name, "title": title}[key]
        if val is None:
            continue
        val = str(val).strip()
        if len(val) > MAX_TEXT_LEN:
            raise ValueError("%s 超过 %d 个字符上限（当前 %d）"
                             % (label, MAX_TEXT_LEN, len(val)))
        db.set_system_config(cfg_key, val)
        changed.append("%s → %s" % (label, val or default))
    if not changed:
        raise ValueError("没有需要保存的内容")
    return {"ok": True, "changed": changed,
            "message": "已保存：" + "；".join(changed)}


def reset_text() -> dict:
    """恢复默认品牌文字。"""
    db.set_system_config("brand_name", "")
    db.set_system_config("brand_title", "")
    return {"ok": True,
            "message": "品牌文字已恢复默认（%s / %s）"
                       % (DEFAULTS["name"], DEFAULTS["title"])}
