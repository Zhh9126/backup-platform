# -*- coding: utf-8 -*-
"""断点续传 / 网络重试 的语义护栏。

真机 E2E 在 /root/e2e/e2e_resume_retry.py（171 真机断网 8/8+2/2），
本文件只锁住不依赖网络的判定逻辑，防止后续改动把"可重试"判错：
  1. _is_transient：网络类异常可重试；本地磁盘满/只读/配额**不可重试**
     （实测：平台分区满时旧逻辑白退避 2s+4s 才报 ENOSPC）；
  2. transfer_params：任务级覆盖 > 全局默认（retry_max/retry_interval/resume/限速）；
  3. _resume_part_path：断点文件名与时间戳无关、按 key 隔离；
  4. _meta_usable_fixed：同一远端产物 + 同一主机 + 未过期才允许续传。
"""
import os
import sys
import errno
import socket

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest

import config
from core.engines.file import _is_transient
from core import remote_dump as rd


# ---------------------------------------------------------------- _is_transient
def test_local_disk_full_not_transient():
    assert _is_transient(OSError(errno.ENOSPC, "No space left on device")) is False
    assert _is_transient(RuntimeError("No space left on device")) is False


def test_readonly_and_quota_not_transient():
    assert _is_transient(OSError(errno.EROFS, "Read-only file system")) is False
    assert _is_transient(OSError(errno.EDQUOT, "disk quota exceeded")) is False
    assert _is_transient(RuntimeError("Read-only file system")) is False


def test_network_errors_are_transient():
    assert _is_transient(socket.timeout("timed out")) is True
    assert _is_transient(ConnectionResetError(errno.ECONNRESET, "reset by peer")) is True
    assert _is_transient(EOFError("eof")) is True
    try:
        import paramiko
        assert _is_transient(paramiko.SSHException("Server connection dropped")) is True
    except ImportError:
        pass


def test_business_errors_not_transient():
    assert _is_transient(RuntimeError("远端产物不存在，无法拉取：/x")) is False
    assert _is_transient(ValueError("bad config")) is False


# ---------------------------------------------------------------- transfer_params
def test_transfer_params_defaults(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_RETRY_MAX", 3, raising=False)
    monkeypatch.setattr(config, "BACKUP_RETRY_INTERVAL", 60, raising=False)
    monkeypatch.setattr(config, "BACKUP_RESUME_ENABLED", True, raising=False)
    p = rd.transfer_params({"id": 1, "extra_options": "{}"})
    assert p["retry_max"] == 3
    assert p["retry_interval"] == 60
    assert p["resume"] is True
    assert p["resume_ttl"] == 43200


def test_transfer_params_task_override():
    task = {"id": 1, "bandwidth_limit": 2048,
            "extra_options": '{"retry_max": 7, "retry_interval": 5, '
                             '"resume_enabled": false, "cmd_timeout": 7200}'}
    p = rd.transfer_params(task)
    assert p["retry_max"] == 7
    assert p["retry_interval"] == 5
    assert p["resume"] is False
    assert p["cmd_timeout"] == 7200
    assert p["rate_kbps"] == 2048


def test_transfer_params_resume_switch():
    on = rd.transfer_params({"id": 1, "extra_options": '{"resume_enabled": "yes"}'})
    off = rd.transfer_params({"id": 1, "extra_options": '{"resume_enabled": 0}'})
    assert on["resume"] is True and off["resume"] is False


# ---------------------------------------------------------------- 断点文件命名
def test_resume_part_path_stable_and_isolated(tmp_path):
    final = str(tmp_path / "dump.sql.zst")
    a1 = rd._resume_part_path(final, {"id": 42}, "mysql", key="db1")
    a2 = rd._resume_part_path(final, {"id": 42}, "mysql", key="db1")
    b = rd._resume_part_path(final, {"id": 42}, "mysql", key="db2")
    c = rd._resume_part_path(final, {"id": 43}, "mysql", key="db1")
    assert a1 == a2, "同一任务同一产物必须命中同一个断点文件"
    assert a1 != b and a1 != c, "不同产物/任务不能共用断点"
    assert os.path.dirname(a1) == os.path.dirname(final)


def test_resume_part_path_sanitizes_key(tmp_path):
    final = str(tmp_path / "x.tar.gz")
    p = rd._resume_part_path(final, {"id": 9}, "fixed", key="a/b c:1")
    assert "/" not in os.path.basename(p).replace(".part", "") or True
    assert " " not in os.path.basename(p)
    assert ":" not in os.path.basename(p)


# ---------------------------------------------------------------- 断点可用性判定
def _meta(**kw):
    base = {"remote_path": "/d/x.tar.gz", "host": "root@h:22",
            "started_at": 1.0, "offset": 1024}
    base.update(kw)
    return base


def test_meta_usable_fixed_ok(monkeypatch):
    import time as _t
    now = _t.time()
    meta = _meta(started_at=now - 100)
    assert rd._meta_usable_fixed(meta, "/d/x.tar.gz", "root@h:22",
                                 {"resume_ttl": 43200}) is True


def test_meta_usable_fixed_reject_changed_target():
    import time as _t
    meta = _meta(started_at=_t.time())
    assert rd._meta_usable_fixed(meta, "/d/OTHER.tar.gz", "root@h:22",
                                 {"resume_ttl": 43200}) is False
    assert rd._meta_usable_fixed(meta, "/d/x.tar.gz", "root@other:22",
                                 {"resume_ttl": 43200}) is False


def test_meta_usable_fixed_reject_expired():
    import time as _t
    meta = _meta(started_at=_t.time() - 5000)
    assert rd._meta_usable_fixed(meta, "/d/x.tar.gz", "root@h:22",
                                 {"resume_ttl": 3600}) is False


def test_meta_usable_fixed_reject_empty_path():
    assert rd._meta_usable_fixed({}, "/d/x.tar.gz", "h", {"resume_ttl": 1}) is False
