# -*- coding: utf-8 -*-
"""对象存储厂商预设与「任务 → 客户端」装配。

任务字段复用现有 backup_tasks 的表结构（**不改表、不影响既有功能**）：

===========  ==================================================
任务字段      对象存储语义
===========  ==================================================
host          端点 (endpoint)，可带 ``http://`` / 端口
port          端口（MinIO 常用 9000；留空时随 scheme）
username      AccessKey
password      SecretKey（沿用平台既有的加密存储）
db_name       桶名（backup / restore 默认桶）
extra_options provider/region/secure/verify_ssl/addressing/prefix/
              include_versions/workers/restore_bucket/restore_prefix/...
===========  ==================================================

也就是说：对象存储任务在库里就是一个普通的备份任务，调度、记录、保留策略、
三级存储/复制这些既有链路**零改动**复用。
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse

from .client import ObjectStoreClient

__all__ = ["PROVIDERS", "providers", "provider_meta", "build_client",
           "client_from_task", "key_to_relpath", "relpath_to_key",
           "MANIFEST_NAME", "OBJECTS_DIR", "VERSIONS_DIR"]


MANIFEST_NAME = "manifest.json"
OBJECTS_DIR = "objects"
VERSIONS_DIR = "versions"

#: 单目录名过长时改用哈希目录（ext4/overlay 单名上限 255 字节，
#: 中文 key 百分号编码后会膨胀到 9 字节/字，必须主动规避）
_MAX_SEGMENT = 120


PROVIDERS = [
    {
        "value": "minio",
        "label": "MinIO（自建）",
        "auth": "sigv4",
        "addressing": "path",
        "default_region": "us-east-1",
        "default_port": 9000,
        "secure_default": False,
        "endpoint_hint": "192.168.1.10:9000",
        "region_required": False,
        "note": "默认 path 寻址 + HTTP；签名走标准 SigV4，无需安装任何客户端。",
    },
    {
        "value": "s3",
        "label": "AWS S3 / S3 兼容",
        "auth": "sigv4",
        "addressing": "virtual",
        "default_region": "us-east-1",
        "endpoint_hint": "s3.us-east-1.amazonaws.com",
        "region_required": True,
        "note": "AWS 官方或其他严格 S3 实现；必须填正确 Region，否则签名不匹配。",
    },
    {
        "value": "cos",
        "label": "腾讯云 COS（S3 兼容 API）",
        "auth": "sigv4",
        "addressing": "auto",
        "default_region": "ap-guangzhou",
        "endpoint_hint": "cos.ap-guangzhou.myqcloud.com",
        "region_required": True,
        "note": "腾讯云 COS 提供官方 S3 兼容 API，桶名须为「名称-APPID」完整形式。",
    },
    {
        "value": "oss",
        "label": "阿里云 OSS（原生 V4 签名）",
        "auth": "ossv4",
        "addressing": "virtual",
        "default_region": "cn-hangzhou",
        "endpoint_hint": "oss-cn-hangzhou.aliyuncs.com",
        "region_required": True,
        "note": "OSS 的 V4 与 AWS 不同，按其公开文档实现；Region 填 cn-hangzhou 形式。"
                "（本机无 OSS 资源，未做真实验证，请先点连接测试）",
    },
    {
        "value": "obs",
        "label": "华为云 OBS（S3 兼容模式）",
        "auth": "sigv4",
        "addressing": "virtual",
        "default_region": "cn-north-4",
        "endpoint_hint": "obs.cn-north-4.myhuaweicloud.com",
        "region_required": True,
        "note": "OBS 提供 S3 兼容接口形式；具体以官方最新文档为准，未做真实验证。",
    },
    {
        "value": "ceph",
        "label": "Ceph RGW / 自建网关",
        "auth": "sigv4",
        "addressing": "path",
        "default_region": "us-east-1",
        "endpoint_hint": "192.168.1.20:8080",
        "region_required": False,
        "note": "通用 S3 网关，默认 path 寻址；未知实现建议先做连接测试。",
    },
]


def providers() -> list[dict]:
    return [dict(p) for p in PROVIDERS]


def provider_meta(value: str) -> dict:
    for p in PROVIDERS:
        if p["value"] == (value or "").lower():
            return dict(p)
    return dict(PROVIDERS[0])


# ----------------------------- key ↔ 落盘路径 -----------------------------
def key_to_relpath(key: str, version_id: str = "") -> str:
    """对象 key → 备份包内相对路径（可逆）。

    * 逐段百分号编码：中文/空格/``?`` 等字符在文件系统上安全，且可用
      :func:`relpath_to_key` 完全还原；
    * 任一路径段过长（编码后 > 120 字节）时改用 ``对象key 的 sha256`` 落盘，
      原始 key 仍然完整记录在 manifest 里，不影响保真度。
    """
    segs = [s for s in (key or "").split("/") if s != ""]
    if not segs:
        segs = ["_root_object"]
    quoted = [urllib.parse.quote(s, safe="") for s in segs]
    # 版本对象必须按 version_id 分开落盘：同一 key 的历史版本若共用一条路径，
    # 备份时会**互相覆盖**，包内只剩最后下载的那一个（顺序不确定），恢复时
    # 多个版本上传的是同一份内容，"最新版本"因此随机（实测恢复 v1/v2/v3，
    # 8 次里 3 次读到 v1/v2）。版本后缀取 version_id 摘要，长度可控。
    vsfx = ("@" + hashlib.sha256((version_id or "").encode("utf-8")
                                 ).hexdigest()[:16]) if version_id else ""
    if any(len(s.encode()) > _MAX_SEGMENT for s in quoted) or \
            sum(len(s) for s in quoted) > 900:
        digest = hashlib.sha256((key or "").encode("utf-8")).hexdigest()
        if version_id:
            return "/".join([VERSIONS_DIR, digest[:2], digest + vsfx])
        return "/".join([OBJECTS_DIR, "_long", digest[:2], digest])
    prefix = VERSIONS_DIR if version_id else OBJECTS_DIR
    quoted[-1] = quoted[-1] + vsfx
    return "/".join([prefix] + quoted)


def relpath_to_key(rel: str) -> str:
    """:func:`key_to_relpath` 的逆运算（哈希路径由调用方跳过）。"""
    parts = (rel or "").split("/")
    if parts and parts[0] in (OBJECTS_DIR, VERSIONS_DIR):
        parts = parts[1:]
    if parts and parts[0] == "_long":
        return ""
    if parts:
        # 剥掉版本路径后缀（见 key_to_relpath）：末段形如 "<key段>@<版本摘要>"
        at = parts[-1].rfind("@")
        if at > 0:
            parts[-1] = parts[-1][:at]
    return "/".join(urllib.parse.unquote(p) for p in parts)


# ----------------------------- 装配 -----------------------------
def build_client(cfg: dict) -> ObjectStoreClient:
    """按配置字典构造客户端；缺失项用厂商预设补齐。"""
    provider = (cfg.get("provider") or "minio").lower()
    meta = provider_meta(provider)
    endpoint = (cfg.get("endpoint") or "").strip()
    if not endpoint:
        raise ValueError("缺少 endpoint（对象存储端点）")
    port = cfg.get("port")
    if port and "://" not in endpoint and ":" not in endpoint.split("/")[0]:
        endpoint = "%s:%s" % (endpoint.rstrip("/"), port)
    if "://" not in endpoint:
        endpoint = ("https://" if cfg.get("secure", meta.get("secure_default", False))
                    else "http://") + endpoint
    return ObjectStoreClient(
        endpoint=endpoint,
        access_key=cfg.get("access_key") or "",
        secret_key=cfg.get("secret_key") or "",
        region=(cfg.get("region") or meta.get("default_region") or "us-east-1"),
        secure=None if "://" in endpoint else bool(
            cfg.get("secure", meta.get("secure_default", False))),
        verify_ssl=bool(cfg.get("verify_ssl", False)),
        provider=provider,
        addressing=cfg.get("addressing") or meta.get("addressing") or "auto",
        timeout=int(cfg.get("timeout") or 60),
        retries=int(cfg.get("retries") or 3),
        sign_version=str(cfg.get("sign_version") or "auto").lower(),
    )


def _extra(task: dict) -> dict:
    raw = task.get("extra_options") or ""
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _flag(value, default: bool = False) -> bool:
    """容错解析表单/JSON 里的布尔开关（"0"/"false" 也算关）。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def task_cfg(task: dict) -> dict:
    """把备份任务翻译成对象存储作业配置（含备份/恢复的目标参数）。"""
    ex = _extra(task)
    # 0 是有效语义（TTL=0 不过期 / retries=0 不重试），不能用 `or 默认值` 吃掉
    _ttl_raw = ex.get("os_resume_ttl_hours")
    try:
        ttl_h = 72.0 if _ttl_raw is None or _ttl_raw == "" else float(_ttl_raw)
    except (TypeError, ValueError):
        ttl_h = 72.0
    _retry_raw = ex.get("os_resume_max_retries")
    try:
        retries = 3 if _retry_raw is None or _retry_raw == "" else int(_retry_raw)
    except (TypeError, ValueError):
        retries = 3
    return {
        # 断点续传默认开启：既有任务无需改配置即可获得「中断后重跑接着传」
        "resume": _flag(ex.get("os_resume"), True),
        "resume_ttl_hours": max(0.0, min(ttl_h, 720.0)),
        "resume_max_retries": max(0, min(retries, 10)),
        "provider": (ex.get("os_provider") or ex.get("provider") or "minio"),
        "endpoint": task.get("host") or ex.get("os_endpoint") or "",
        "port": task.get("port"),
        "access_key": task.get("username") or "",
        "secret_key": task.get("password") or "",
        "region": ex.get("os_region") or "",
        "secure": ex.get("os_secure"),
        "verify_ssl": ex.get("os_verify_ssl", False),
        "addressing": ex.get("os_addressing") or "",
        "sign_version": ex.get("os_sign_version") or "auto",
        "bucket": task.get("db_name") or "",
        "prefix": ex.get("os_prefix") or "",
        "include_versions": bool(ex.get("os_include_versions", False)),
        "workers": int(ex.get("os_workers") or 8),
        "restore_bucket": ex.get("os_restore_bucket") or task.get("db_name") or "",
        "restore_prefix": ex.get("os_restore_prefix"),
        "restore_create_bucket": bool(ex.get("os_restore_create_bucket", True)),
        "restore_overwrite": str(ex.get("os_restore_overwrite") or "if_newer"),
        "restore_apply_deleted": bool(ex.get("os_restore_apply_deleted", True)),
    }


def client_from_task(task: dict) -> ObjectStoreClient:
    return build_client(task_cfg(task))


def snapshot_path(output_dir: str) -> str:
    """增量基准快照文件位置（每个任务副本一份，按任务目录隔离）。"""
    return os.path.join(output_dir, ".object_snapshot.json")
