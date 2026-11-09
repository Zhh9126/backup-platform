# -*- coding: utf-8 -*-
"""对象存储断点续传：纯逻辑与协议分支回归。

真机/真实 HTTP 的全链路验证见 scripts/object_storage_resume_e2e.py；
本文件锁住不便在 E2E 里构造的边界（服务端忽略 Range、Content-Range 起点
不符、checkpoint 坏行、scope/TTL 失效、0 值配置语义、API 路由契约）。
"""
import hashlib
import json
import os
import time

import pytest

from core.engines.object_storage import ObjectStorageEngine
from core.objectstore import client as osc
from core.objectstore import providers as osp
from core.objectstore import resume as osr

CFG = {
    "provider": "minio", "endpoint": "127.0.0.1:9000", "region": "us-east-1",
    "bucket": "b1", "prefix": "", "include_versions": False,
}


def _entry(stage, rel, data, meta):
    p = os.path.join(stage, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return {"key": meta["key"], "path": rel, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "etag": meta["etag"], "last_modified": meta["last_modified"],
            "version_id": "", "is_latest": True, "seq": 0}


def _meta(etag="e1", size=8, lm="2026-01-01"):
    return {"key": "a.txt", "etag": etag, "size": size,
            "last_modified": lm, "version_id": ""}


# ---------------------------------------------------------------- scope / 复用
def test_scope_digest_is_stable_and_scoped():
    a = osr.scope_digest(1, "t", CFG, "full")
    assert a == osr.scope_digest(1, "t", CFG, "full")
    assert a != osr.scope_digest(2, "t", CFG, "full")
    assert a != osr.scope_digest(1, "t2", CFG, "full")
    assert a != osr.scope_digest(1, "t", dict(CFG, bucket="b2"), "full")
    assert a != osr.scope_digest(1, "t", dict(CFG, prefix="p/"), "full")
    assert a != osr.scope_digest(1, "t", dict(CFG, include_versions=True), "full")
    assert a != osr.scope_digest(1, "t", CFG, "incremental")


def test_meta_matches_quadruple():
    m = _meta()
    e = {"etag": m["etag"], "size": m["size"],
         "last_modified": m["last_modified"], "version_id": ""}
    assert osr.meta_matches(e, m)
    for key, val in (("etag", "e2"), ("size", 9),
                     ("last_modified", "2026-01-02"), ("version_id", "v9")):
        assert not osr.meta_matches(dict(e, **{key: val}), m)


def test_reusable_entry_rejects_changed_source_and_broken_file(tmp_path):
    stage = str(tmp_path / "stage")
    data = b"12345678"
    meta = _meta(size=len(data))
    e = _entry(stage, "objects/a.txt", data, meta)
    assert osr.reusable_entry(stage, e, meta) is not None
    assert osr.reusable_entry(stage, e, dict(meta, etag="changed")) is None
    # 文件被截断 / 篡改后绝不复用
    with open(os.path.join(stage, "objects/a.txt"), "wb") as f:
        f.write(b"broken")
    assert osr.reusable_entry(stage, e, meta) is None
    # 路径穿越防护
    assert osr.reusable_entry(stage, dict(e, path="../../etc/passwd"), meta) is None


def test_load_done_skips_bad_lines_and_last_write_wins(tmp_path):
    stage = str(tmp_path / "stage")
    os.makedirs(stage, exist_ok=True)
    p = osr.done_path(stage)
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"u":"a","e":{"size":1}}\n')
        f.write('{"u":"a","e":{"size":2}}\n')
        f.write('{broken json\n')
        f.write('\n')
        f.write('{"u":"b"}\n')
    done = osr.load_done(stage)
    assert done == {"a": {"size": 2}}


def test_load_valid_rejects_scope_version_and_ttl(tmp_path):
    stage = str(tmp_path / "stage")
    os.makedirs(stage, exist_ok=True)
    scope = osr.scope_digest(1, "t", CFG, "full")
    cp = osr.new_checkpoint(1, "t", CFG, "full", 3)
    osr.save_meta(stage, cp)
    osr.append_done(stage, "a", {"size": 1})
    assert osr.load_valid(stage, scope, 72)[0] == {"a": {"size": 1}}
    assert osr.load_valid(stage, "other-scope", 72) == ({}, {})
    # 0 = 不过期；过期用下面的真实回拨时间验证（极小 TTL 会有时序竞态）
    assert osr.load_valid(stage, scope, 0)[0] == {"a": {"size": 1}}
    cp["created_at"] = time.time() - 100 * 3600
    json.dump(cp, open(osr.checkpoint_path(stage), "w", encoding="utf-8"))
    assert osr.load_valid(stage, scope, 72) == ({}, {})
    cp["created_at"] = time.time()
    cp["version"] = 999
    json.dump(cp, open(osr.checkpoint_path(stage), "w", encoding="utf-8"))
    assert osr.load_valid(stage, scope, 72) == ({}, {})


def test_status_counts_done_and_parts(tmp_path):
    root = str(tmp_path)
    stage = osr.stage_path(root)
    os.makedirs(os.path.join(stage, "objects"), exist_ok=True)
    osr.save_meta(stage, osr.new_checkpoint(1, "t", CFG, "full", 5))
    osr.append_done(stage, "a", {"size": 100})
    with open(os.path.join(stage, "objects", "x" + osr.PART_SUFFIX), "wb") as f:
        f.write(b"z" * 40)
    st = osr.status(root, osr.scope_digest(1, "t", CFG, "full"), 72)
    assert st["valid"] is True and st["done_count"] == 1
    assert st["done_bytes"] == 100 and st["part_count"] == 1
    assert st["part_bytes"] == 40 and st["plan_total"] == 5
    assert st["expired"] is False
    assert osr.status(str(tmp_path / "none"), "")["stage_exists"] is False


# ---------------------------------------------------------------- 配置解析
def test_task_cfg_resume_defaults_and_zero_values():
    base = {"id": 1, "name": "t", "host": "h", "username": "ak",
            "password": "sk", "db_name": "b", "extra_options": {}}
    cfg = osp.task_cfg(base)
    assert cfg["resume"] is True
    assert cfg["resume_ttl_hours"] == 72.0
    assert cfg["resume_max_retries"] == 3
    zero = dict(base, extra_options={"os_resume_ttl_hours": 0,
                                     "os_resume_max_retries": 0,
                                     "os_resume": "false"})
    cfg0 = osp.task_cfg(zero)
    assert cfg0["resume"] is False
    assert cfg0["resume_ttl_hours"] == 0.0     # 0 = 不过期，不能被默认值吞
    assert cfg0["resume_max_retries"] == 0     # 0 = 不重试


# ---------------------------------------------------------------- HTTP 分支
class _FakeResp:
    def __init__(self, status, data, headers=None):
        self.status = status
        self._data = data
        self._pos = 0
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}

    def read(self, n=-1):
        if n is None or n < 0:
            out, self._pos = self._data[self._pos:], len(self._data)
        else:
            out = self._data[self._pos:self._pos + n]
            self._pos += len(out)
        return out

    def getheader(self, key):
        return self._headers.get(key.lower())


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _cli():
    return osc.ObjectStoreClient("127.0.0.1:9000", "ak", "sk", secure=False)


def test_range_ignored_by_server_rewrites_from_zero(tmp_path, monkeypatch):
    data = bytes(range(256)) * 200
    dest = str(tmp_path / "out.bin")
    with open(osr.part_path(dest), "wb") as f:
        f.write(data[:1000])
    cli, calls, conns = _cli(), [], []

    def fake(method, bucket, key="", query=None, headers=None, **kw):
        calls.append(dict(headers or {}))
        c = _FakeConn()
        conns.append(c)
        return _FakeResp(200, data), c

    monkeypatch.setattr(cli, "_request", fake)
    got = cli.download_to_file("b", "k", dest, resume=True,
                               expected_size=len(data), max_retries=0)
    assert got == len(data)
    assert open(dest, "rb").read() == data       # 不能把全量拼在 1000 字节之后
    assert calls[0].get("range") == "bytes=1000-"
    assert all(c.closed for c in conns)


def test_range_start_mismatch_restarts_without_range(tmp_path, monkeypatch):
    data = b"ABCDEFGH" * 100
    dest = str(tmp_path / "out.bin")
    with open(osr.part_path(dest), "wb") as f:
        f.write(data[:100])
    cli, calls = _cli(), []

    def fake(method, bucket, key="", query=None, headers=None, **kw):
        calls.append(dict(headers or {}))
        if not calls[-1].get("range"):
            return _FakeResp(200, data), _FakeConn()
        # 起点与请求的 offset 不符：必须丢弃 part 从头重下
        return _FakeResp(206, data,
                         {"content-range": "bytes 0-%d/%d" % (len(data) - 1, len(data))}), \
            _FakeConn()

    monkeypatch.setattr(cli, "_request", fake)
    got = cli.download_to_file("b", "k", dest, resume=True,
                               expected_size=len(data), max_retries=2)
    assert got == len(data)
    assert open(dest, "rb").read() == data
    assert len(calls) == 2 and calls[0].get("range") == "bytes=100-"
    assert "range" not in calls[1]


def test_non_resume_path_keeps_original_behaviour(tmp_path, monkeypatch):
    data = b"plain"
    dest = str(tmp_path / "out.bin")
    cli, seen = _cli(), []

    def fake(method, bucket, key="", query=None, headers=None, **kw):
        seen.append(dict(headers or {}))
        return _FakeResp(200, data), _FakeConn()

    monkeypatch.setattr(cli, "_request", fake)
    assert cli.download_to_file("b", "k", dest) == len(data)
    assert open(dest, "rb").read() == data
    assert seen == [{}] and not os.path.exists(osr.part_path(dest))


def test_pack_stage_excludes_checkpoint_files(tmp_path):
    stage = str(tmp_path / "stage")
    os.makedirs(os.path.join(stage, "objects"), exist_ok=True)
    with open(os.path.join(stage, "manifest.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(stage, "objects", "a.bin"), "wb") as f:
        f.write(b"real")
    with open(os.path.join(stage, osr.CHECKPOINT_NAME), "w") as f:
        f.write("{}")
    with open(os.path.join(stage, osr.DONE_NAME), "w") as f:
        f.write("{}\n")
    with open(os.path.join(stage, "objects", "a.bin" + osr.PART_SUFFIX), "wb") as f:
        f.write(b"partial")
    out = str(tmp_path / "out.tar.gz")
    ObjectStorageEngine._pack_stage(stage, out)
    import tarfile
    with tarfile.open(out, "r:*") as t:
        names = t.getnames()
    assert "manifest.json" in names and "objects/a.bin" in names
    assert osr.CHECKPOINT_NAME not in names and osr.DONE_NAME not in names
    assert not any(n.endswith(osr.PART_SUFFIX) for n in names)


def test_api_resume_routes_registered_on_both_prefixes():
    from flask import Flask
    from api import api_bp
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1", name="api_v1")
    rules = {r.rule: set(r.methods) for r in app.url_map.iter_rules()}
    for prefix in ("/api", "/api/v1"):
        key = prefix + "/object-storage/tasks/<int:task_id>/resume-status"
        assert key in rules and "GET" in rules[key]
        key2 = prefix + "/object-storage/tasks/<int:task_id>/resume"
        assert key2 in rules and "DELETE" in rules[key2]
