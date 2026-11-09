# -*- coding: utf-8 -*-
"""S3 协议客户端（仅标准库 http.client + xml.etree）。

能力::

    test_connection / list_buckets / create_bucket / bucket_exists
    iter_objects          ListObjectsV2（分页续 wildcard token）
    iter_object_versions  ListObjectVersions（含删除标记，用于版本级备份）
    head_object / get_bytes / download_to_file（流式落盘，内存与对象大小无关）
    upload_file（先算 payload SHA256 再流式 PUT）/ delete_object

设计要点（与平台一贯的工程纪律对齐）：
1. **零第三方依赖**：http.client + xml.etree + hashlib/hmac，离线包无需新增 wheel；
2. **流式收发**：下载直接写盘、上传按块发送，内存占用与单个对象大小解耦
   （本机文件备份曾因把整个 dump 读进内存触发 MemoryError，教训必须复用）；
3. **可重试**：连接重置/超时/429/5xx 按指数退避重试，长列举任务不被偶发抖动打断；
4. **明文错误**：HTTP 错误解析 S3 的 <Error><Code>/<Message>，原样抛出，
   不做"友好化"改写（排障需要真实原因）。
"""
from __future__ import annotations

import hashlib
import http.client
import os
import re
import socket
import ssl
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Callable, Iterator, Optional

from . import signers

__all__ = ["ObjectStoreError", "ObjectStoreClient"]

_CHUNK = 8 << 20          # 上传分块大小（同时也是流式下载的分块）
_RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class ObjectStoreError(Exception):
    """对象存储操作失败。携带 status / code / detail 便于上层分类处理。"""

    def __init__(self, message: str, status: int = 0, code: str = "",
                 detail: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _children(el, name: str):
    return [c for c in list(el) if _local(c.tag) == name]


def _child_text(el, name: str, default: str = "") -> str:
    for c in list(el):
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return default


def _parse_error(body: bytes) -> tuple[str, str]:
    """从 S3 错误 XML 中解析 (Code, Message)。"""
    try:
        root = ET.fromstring(body)
        return _child_text(root, "Code", ""), _child_text(root, "Message", "")
    except Exception:
        return "", body[:300].decode("utf-8", "ignore")


class ObjectStoreClient:
    """轻量 S3 协议客户端。

    Args:
        endpoint: 服务端点，形如 ``127.0.0.1:9000`` / ``https://oss-cn-hz.aliyuncs.com``
        access_key / secret_key: 访问密钥
        region: 参与签名的区域（MinIO 任意值均可，常用 ``us-east-1``）
        secure: 是否 HTTPS（endpoint 自带 http:// 时自动降级为 False）
        verify_ssl: 是否校验证书（自签名环境默认 False，与 PVE 通道一致）
        addressing: path | virtual | auto
        timeout / retries: 超时与重试
    """

    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 region: str = "us-east-1", secure: bool = True,
                 verify_ssl: bool = False, provider: str = "s3",
                 addressing: str = "auto", timeout: int = 60, retries: int = 3,
                 sign_version: str = "auto"):
        ep = (endpoint or "").strip()
        if "://" in ep:
            parsed = urllib.parse.urlparse(ep)
            secure = parsed.scheme == "https"
            ep = parsed.netloc or parsed.path
        self.endpoint = ep.rstrip("/")
        self.access_key = access_key or ""
        self.secret_key = secret_key or ""
        self.region = region or "us-east-1"
        self.secure = bool(secure)
        self.verify_ssl = bool(verify_ssl)
        self.provider = (provider or "s3").lower()
        self.addressing = (addressing or "auto").lower()
        self.timeout = int(timeout or 60)
        self.retries = max(0, int(retries or 0))
        self.sign_version = (sign_version or "auto").lower()

    # ------------------------- 内部：签名与连接 -------------------------
    def _signer(self):
        if self.provider in ("oss", "aliyun"):
            if self.sign_version == "v1":
                return None          # V1 走 oss_v1_authorization 单独分支
            return signers.OssV4(self.access_key, self.secret_key, self.region)
        return signers.SigV4(self.access_key, self.secret_key, self.region)

    def _host_and_path(self, bucket: Optional[str], key: str = "") -> tuple[str, str]:
        """按寻址风格返回 (Host, 请求路径)。"""
        key_part = "/" + key.lstrip("/") if key else "/"
        if not bucket:
            return self.endpoint, "/"
        if self.addressing == "virtual" or (
                self.addressing == "auto" and self._virtual_ok()):
            return "%s.%s" % (bucket, self.endpoint), key_part
        return self.endpoint, "/%s%s" % (bucket, key_part)

    def _virtual_ok(self) -> bool:
        host = self.endpoint.split(":")[0]
        # IP 直连（含 IPv6 括号形式）与 localhost 一律使用 path 风格
        if host in ("localhost", "127.0.0.1"):
            return False
        if host.startswith("[") or host.replace(".", "").isdigit():
            return False
        return True

    def _conn(self) -> http.client.HTTPConnection:
        if self.secure:
            ctx = ssl.create_default_context()
            if not self.verify_ssl:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(self.endpoint, timeout=self.timeout,
                                               context=ctx)
        return http.client.HTTPConnection(self.endpoint, timeout=self.timeout)

    def _request(self, method: str, bucket: Optional[str], key: str = "",
                 query: dict | None = None, headers: dict | None = None,
                 body: bytes | None = None,
                 body_sender: Optional[Callable[[http.client.HTTPConnection], None]] = None,
                 payload_sha: str | None = None, timeout: int | None = None,
                 content_length: Optional[int] = None):
        """发起一次签名请求，返回未读取的 HTTPResponse（调用方负责关闭）。"""
        host, path = self._host_and_path(bucket, key)
        # 注意：空串值必须保留 —— S3 的子资源（?versioning=、?versions=）就是空值形式，
        # 之前按“空值过滤”处理会让 PutBucketVersioning 退化成上传空 key 的对象请求。
        query = {k: v for k, v in (query or {}).items() if v is not None}
        body_len = len(body) if body is not None else 0

        hdrs = dict(headers or {})
        hdrs.pop("host", None)
        if self.provider in ("oss", "aliyun") and self.sign_version == "v1":
            import email.utils
            date_str = email.utils.formatdate(usegmt=True)
            canon_res = "/%s/%s" % (bucket or "", key)
            hdrs["date"] = date_str
            hdrs["authorization"] = signers.oss_v1_authorization(
                method, canon_res, hdrs, self.access_key, self.secret_key,
                date_str=date_str)
        else:
            signer = self._signer()
            if payload_sha is None:
                payload_sha = signers.payload_hash(body)
            # 请求体为空且非 GET/HEAD 时（如 PUT 建桶），用空串哈希即可
            hdrs = signer.sign(method, path, query, hdrs, payload_sha, host)
            hdrs["date"] = hdrs.get("date") or email_now()

        if body is not None:
            hdrs["content-length"] = str(len(body))
        elif body_sender is not None:
            # 流式发送必须显式声明长度：内容已提前算好 SHA256 并参与签名
            hdrs["content-length"] = str(int(content_length or 0))
        elif method.upper() in ("PUT", "POST"):
            hdrs["content-length"] = "0"

        # 请求行必须与参与签名的规范化路径严格一致（否则会被判签名不匹配），
        # 中文/空格/+/? 等字符在此处百分号编码后发送，服务端再解码还原。
        url = signers.canonical_path(path) + (
            "?" + signers.canonical_query(query) if query else "")
        last_err = None
        for attempt in range(self.retries + 1):
            conn = None
            try:
                conn = self._conn()
                conn.putrequest(method.upper(), url, skip_host=True,
                                skip_accept_encoding=True)
                for k, v in hdrs.items():
                    if v is None:
                        continue
                    conn.putheader(k, str(v))
                conn.endheaders()
                if body:
                    conn.send(body)
                elif body_sender:
                    body_sender(conn)
                resp = conn.getresponse()
                if resp.status in _RETRY_STATUS and attempt < self.retries:
                    resp.read()
                    conn.close()
                    time.sleep(min(2 ** attempt, 8))
                    continue
                return resp, conn
            except (socket.timeout, TimeoutError, ConnectionError,
                    http.client.HTTPException, ssl.SSLError, OSError) as e:
                last_err = e
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                if attempt >= self.retries:
                    break
                time.sleep(min(2 ** attempt, 8))
        raise ObjectStoreError(
            "对象存储请求失败（%s %s）: %s" % (
                method.upper(), self.endpoint, str(last_err)[:300]),
            detail=str(last_err or "")[:500])

    # ------------------------- 桶级操作 -------------------------
    def list_buckets(self) -> list[dict]:
        resp, conn = self._request("GET", None, "")
        try:
            body = resp.read()
            self._raise_if_error(resp, body)
            root = ET.fromstring(body)
            out = []
            for b in _children(_child_el(root, "Buckets"), "Bucket"):
                out.append({"name": _child_text(b, "Name"),
                            "created_at": _child_text(b, "CreationDate")})
            return out
        finally:
            conn.close()

    def bucket_exists(self, bucket: str) -> bool:
        try:
            resp, conn = self._request("HEAD", bucket, "")
            try:
                return 200 <= resp.status < 400
            finally:
                conn.close()
        except ObjectStoreError:
            return False

    def create_bucket(self, bucket: str) -> bool:
        resp, conn = self._request("PUT", bucket, "")
        try:
            body = resp.read()
            self._raise_if_error(resp, body)
            return True
        finally:
            conn.close()

    def get_bucket_versioning(self, bucket: str) -> dict:
        """查询桶的版本控制状态（业界要求：S3 时间点保护必备前置条件）。"""
        resp, conn = self._request("GET", bucket, "", query={"versioning": ""})
        try:
            body = resp.read()
            self._raise_if_error(resp, body)
            root = ET.fromstring(body)
            return {"status": _child_text(root, "Status"),
                    "mfa_delete": _child_text(root, "MfaDelete")}
        finally:
            conn.close()

    def set_bucket_versioning(self, bucket: str, enabled: bool = True) -> None:
        """启用/暂停桶的版本控制。"""
        body = ('<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                "<Status>%s</Status></VersioningConfiguration>"
                % ("Enabled" if enabled else "Suspended")).encode("utf-8")
        resp, conn = self._request("PUT", bucket, "", query={"versioning": ""},
                                   body=body)
        try:
            data = resp.read()
            self._raise_if_error(resp, data)
        finally:
            conn.close()

    def test_connection(self) -> tuple[bool, str]:
        """连通性探测：列桶成功即可认为端点/密钥/签名均正确。"""
        try:
            buckets = self.list_buckets()
        except ObjectStoreError as e:
            return False, self._human_error(e)
        return True, "连接正常（可见桶 %d 个）" % len(buckets)

    # ------------------------- 对象列举 -------------------------
    def iter_objects(self, bucket: str, prefix: str = "",
                     max_keys: int = 1000) -> Iterator[dict]:
        """ListObjectsV2 分页列举（不含删除标记）。"""
        token = ""
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": str(max_keys)}
            if token:
                query["continuation-token"] = token
            resp, conn = self._request("GET", bucket, "", query=query)
            try:
                body = resp.read()
                self._raise_if_error(resp, body)
                root = ET.fromstring(body)
            finally:
                conn.close()
            for c in _children(root, "Contents"):
                yield self._content_meta(c)
            if _child_text(root, "IsTruncated").lower() != "true":
                return
            token = _child_text(root, "NextContinuationToken")
            if not token:
                return

    def iter_object_versions(self, bucket: str, prefix: str = "",
                             max_keys: int = 1000) -> Iterator[dict]:
        """ListObjectVersions 列举（版本 + 删除标记）。"""
        marker = ""
        while True:
            query = {"versions": "", "prefix": prefix, "max-keys": str(max_keys)}
            if marker:
                query["key-marker"] = marker
            resp, conn = self._request("GET", bucket, "", query=query)
            try:
                body = resp.read()
                self._raise_if_error(resp, body)
                root = ET.fromstring(body)
            finally:
                conn.close()
            next_key = ""
            for c in list(root):
                name = _local(c.tag)
                if name == "Version":
                    item = self._content_meta(c)
                    item["version_id"] = _child_text(c, "VersionId")
                    item["is_latest"] = _child_text(c, "IsLatest").lower() == "true"
                    item["delete_marker"] = False
                    yield item
                elif name == "DeleteMarker":
                    yield {
                        "key": _child_text(c, "Key"),
                        "size": 0, "etag": "",
                        "last_modified": _child_text(c, "LastModified"),
                        "storage_class": "",
                        "version_id": _child_text(c, "VersionId"),
                        "is_latest": _child_text(c, "IsLatest").lower() == "true",
                        "delete_marker": True,
                    }
                elif name == "NextKeyMarker":
                    next_key = (c.text or "").strip()
            if _child_text(root, "IsTruncated").lower() != "true" or not next_key:
                return
            marker = next_key

    @staticmethod
    def _content_meta(el) -> dict:
        return {
            "key": _child_text(el, "Key"),
            "size": int(_child_text(el, "Size") or 0),
            "etag": _child_text(el, "ETag").strip('"'),
            "last_modified": _child_text(el, "LastModified"),
            "storage_class": _child_text(el, "StorageClass"),
            "version_id": "",
            "is_latest": True,
            "delete_marker": False,
        }

    # ------------------------- 对象读写 -------------------------
    def head_object(self, bucket: str, key: str, version_id: str = "") -> dict:
        query = {"versionId": version_id} if version_id else None
        resp, conn = self._request("HEAD", bucket, key, query=query)
        try:
            if resp.status >= 400:
                if resp.status == 404:
                    raise ObjectStoreError("对象不存在: %s" % key, status=404,
                                           code="NoSuchKey")
                raise self._make_error(resp, b"")
            return {
                "size": int(resp.getheader("content-length") or 0),
                "etag": (resp.getheader("etag") or "").strip('"'),
                "last_modified": resp.getheader("last-modified") or "",
                "content_type": resp.getheader("content-type") or "",
                "storage_class": resp.getheader("x-amz-storage-class") or "",
            }
        finally:
            conn.close()

    def get_bytes(self, bucket: str, key: str, version_id: str = "") -> bytes:
        query = {"versionId": version_id} if version_id else None
        resp, conn = self._request("GET", bucket, key, query=query)
        try:
            body = resp.read()
            self._raise_if_error(resp, body)
            return body
        finally:
            conn.close()

    def download_to_file(self, bucket: str, key: str, dest: str,
                         version_id: str = "", resume: bool = False,
                         expected_size: int | None = None,
                         max_retries: int = 3, on_chunk=None) -> int:
        """流式下载到文件，返回**落盘总字节数**（含续传复用的字节）。

        ``resume=True`` 时以 ``dest + .aidbm_part`` 暂存：每次请求前按该文件
        实际大小发 ``Range: bytes=N-``，服务端回 206 且 Content-Range 起点吻合
        才追加；回 200（服务端忽略 Range）则截断从头重写。传输中断会在本次
        调用内按新 offset 继续，未完成的 part 也会被保留到下次备份重跑续传。
        ``on_chunk(total)`` 每个块回调，供上层在块边界检查用户取消。
        """
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        query = {"versionId": version_id} if version_id else None
        if not resume:
            resp, conn = self._request("GET", bucket, key, query=query)
            try:
                if resp.status >= 400:
                    body = resp.read()
                    raise self._make_error(resp, body)
                written = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)
                        if on_chunk:
                            on_chunk(written)
                return written
            finally:
                conn.close()

        from . import resume as resume_mod
        part = resume_mod.part_path(dest)
        attempts = max(0, int(max_retries))
        for attempt in range(attempts + 1):
            offset = os.path.getsize(part) if os.path.isfile(part) else 0
            if expected_size is not None:
                exp = int(expected_size)
                if offset > exp:
                    # 源变小或上轮残件写坏，绝不拼接，丢弃重来
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    offset = 0
                elif offset > 0 and offset == exp:
                    os.replace(part, dest)
                    return offset
            headers = {"range": "bytes=%d-" % offset} if offset else None
            conn = None
            try:
                resp, conn = self._request("GET", bucket, key, query=query,
                                           headers=headers)
                if resp.status >= 400:
                    body = resp.read()
                    raise self._make_error(resp, body)
                append = False
                if offset:
                    if resp.status == 206:
                        cr = resp.getheader("content-range") or ""
                        m = re.match(r"\s*bytes\s+(\d+)\s*-\s*(\d+)", cr)
                        if not m or int(m.group(1)) != offset:
                            # 起点不吻合还继续拼会产出「大小对、内容错位」的假文件
                            resp.read()
                            conn.close()
                            conn = None
                            try:
                                os.remove(part)
                            except OSError:
                                pass
                            continue
                        append = True
                    elif resp.status == 200:
                        offset = 0        # 服务端不支持 Range，整包重发
                    else:
                        raise ObjectStoreError(
                            "Range 续传返回非预期状态 %d" % resp.status,
                            status=resp.status)
                total = offset
                with open(part, "ab" if append else "wb") as f:
                    while True:
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        total += len(chunk)
                        if on_chunk:
                            on_chunk(total)
                total = os.path.getsize(part)
                if expected_size is not None and total != int(expected_size):
                    raise ObjectStoreError(
                        "断点续传后落盘大小不一致（源 %s / 盘 %d，断点 %d）"
                        % (expected_size, total, offset))
                os.replace(part, dest)
                return total
            except (socket.timeout, TimeoutError, ConnectionError,
                    http.client.HTTPException, ssl.SSLError, OSError) as e:
                # part 已写字节保留；下一轮按新的 offset 继续。
                # CancelledError 不在捕获列表，直接穿透给调度器标记 cancelled。
                if attempt >= attempts:
                    kept = os.path.getsize(part) if os.path.isfile(part) else 0
                    raise ObjectStoreError(
                        "对象下载中断（已保留断点 %d 字节，重跑可续传）: %s"
                        % (kept, str(e)[:200]), detail=str(e)[:300])
                time.sleep(min(2 ** attempt, 4))
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
        raise ObjectStoreError("对象下载失败: 重试次数已耗尽")

    @staticmethod
    def sha256_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def upload_file(self, bucket: str, key: str, src: str,
                    content_type: str = "", metadata: dict | None = None,
                    storage_class: str = "") -> dict:
        """上传本地文件到对象存储（先用 SHA256 签名，再按块流式发送）。

        单 PUT 上限取决于服务端（AWS S3 5GB、MinIO 同）；超大对象的分片上传
        （Multipart）尚未实现，遇到上限时引擎会拿到明文错误并如实失败。
        """
        size = os.path.getsize(src)
        payload_sha = self.sha256_file(src)

        def sender(conn):
            with open(src, "rb") as f:
                while True:
                    chunk = f.read(_CHUNK)
                    if not chunk:
                        break
                    conn.send(chunk)

        hdrs = {"content-type": content_type or "application/octet-stream"}
        if storage_class:
            hdrs["x-amz-storage-class"] = storage_class
            hdrs["x-oss-storage-class"] = storage_class
        for k, v in (metadata or {}).items():
            prefix = "x-oss-meta-" if self.provider in ("oss", "aliyun") else "x-amz-meta-"
            hdrs[prefix + k.lower()] = str(v)

        resp, conn = self._request("PUT", bucket, key, headers=hdrs,
                                   body_sender=sender, payload_sha=payload_sha,
                                   timeout=None, content_length=size)
        try:
            body = resp.read()
            self._raise_if_error(resp, body)
            return {"size": size, "etag": (resp.getheader("etag") or "").strip('"'),
                    "sha256": payload_sha}
        finally:
            conn.close()

    def delete_object(self, bucket: str, key: str, version_id: str = "") -> None:
        query = {"versionId": version_id} if version_id else None
        resp, conn = self._request("DELETE", bucket, key, query=query)
        try:
            body = resp.read()
            self._raise_if_error(resp, body)
        finally:
            conn.close()

    # ------------------------- 错误处理 -------------------------
    @staticmethod
    def _make_error(resp, body: bytes) -> ObjectStoreError:
        code, msg = _parse_error(body)
        text = msg or body[:300].decode("utf-8", "ignore")
        return ObjectStoreError("HTTP %s %s: %s" % (resp.status, code, text),
                                status=resp.status, code=code, detail=text)

    def _raise_if_error(self, resp, body: bytes) -> None:
        if resp.status >= 400:
            raise self._make_error(resp, body)

    @staticmethod
    def _human_error(e: ObjectStoreError) -> str:
        """把常见错误码翻译为可行动提示，但**保留原始原因**。"""
        hint = {
            "SignatureDoesNotMatch": "签名不匹配：请核对 SecretKey 与 Region（MinIO 一般为 us-east-1）",
            "InvalidAccessKeyId": "AccessKey 无效或不存在",
            "AccessDenied": "拒绝访问：该密钥可能没有列举/读写权限",
            "NoSuchBucket": "桶不存在",
            "AuthorizationHeaderMalformed": "授权头格式异常：Region 与端点区域不一致",
        }.get(e.code, "")
        return e.args[0] + ("　" + hint if hint else "")


def _child_el(root, name: str):
    for c in list(root):
        if _local(c.tag) == name:
            return c
    return ET.Element(name)


def email_now() -> str:
    import email.utils
    return email.utils.formatdate(usegmt=True)
