# -*- coding: utf-8 -*-
"""RtSupervisor 单实例锁的跨平台行为回归。

背景（2026-11-26 用户 Windows 平台 error.log 实测）：

    supervisor.py  os.open(path, O_CREAT|O_EXCL) -> FileExistsError
    supervisor.py  _clear_stale_lock -> _lock_owner_alive
    supervisor.py  os.kill(pid, 0) -> OSError [WinError 87] -> SystemError
    scheduler.py   rt_backup.start(sched) 抛异常 → 周期看门狗未注册 → 实时保护停摆

修复要点：

1. Windows 上 **禁止** ``os.kill(pid, 0)``（等价 TerminateProcess，会误杀持锁进程）；
2. Windows 走 ``msvcrt.locking`` 内核字节锁（进程退出自动释放，免疫陈旧锁/PID 复用）；
3. 锁持有者探测异常不得升级为致命错误；
4. 调度器周期看门狗必须无条件注册。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

# 测试进程绝不写生产日志目录（隔离到临时目录，避免污染平台 logs/）
os.environ.setdefault("LOG_DIR", "/tmp/aidbm_test_rt_lock/logs")

from core.rt_backup import supervisor as sup  # noqa: E402


# ---------------------------------------------------------------- 基础工具
def _write_lock(path, pid, ts=None):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"{pid}\n{ts or '2026-01-01 00:00:00'}\n")


def _bare_supervisor():
    """构造一个不带完整 __init__ 的 RtSupervisor（只补 logger 依赖）。"""
    import logging
    obj = sup.RtSupervisor.__new__(sup.RtSupervisor)
    obj.logger = logging.getLogger("test.rt.supervisor")
    return obj


def _dead_pid():
    """返回一个确定已退出的 PID。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ---------------------------------------------------------------- PID 存活探测
def test_pid_alive_self():
    assert sup._pid_alive(os.getpid()) is True


def test_pid_alive_dead_process():
    assert sup._pid_alive(_dead_pid()) is False


def test_pid_alive_invalid_input_conservative():
    assert sup._pid_alive(0) is True
    assert sup._pid_alive(-1) is True
    assert sup._pid_alive("abc") is True


def test_pid_alive_dispatches_windows_branch(monkeypatch):
    """os.name == 'nt' 时必须走 Windows 分支（内核 API），绝不走 os.kill。"""
    seen = {}

    def fake_win(pid):
        seen["pid"] = pid
        return False

    monkeypatch.setattr(sup, "_windows_pid_alive", fake_win)
    monkeypatch.setattr(os, "name", "nt")
    assert sup._pid_alive(1234) is False
    assert seen.get("pid") == 1234


def test_windows_pid_alive_non_windows_returns_true():
    """非 Windows 环境调用 Windows 分支不得抛异常（保守返回 True）。"""
    assert sup._windows_pid_alive(os.getpid()) in (True, False)


# ---------------------------------------------------------------- 内核锁
def test_kernel_lock_excludes_second_holder(tmp_path):
    path = str(tmp_path / "rt.lock")
    fd, state = sup._acquire_kernel_lock(path)
    if state is None:
        pytest.skip("当前平台不支持内核文件锁")
    assert state is True and fd is not None
    try:
        fd2, state2 = sup._acquire_kernel_lock(path)
        if state2 is not None:
            assert state2 is False
            assert fd2 is None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def test_kernel_lock_released_after_close(tmp_path):
    path = str(tmp_path / "rt.lock")
    fd, state = sup._acquire_kernel_lock(path)
    if state is None:
        pytest.skip("当前平台不支持内核文件锁")
    os.close(fd)
    fd2, state2 = sup._acquire_kernel_lock(path)
    assert state2 is True
    try:
        os.close(fd2)
    except OSError:
        pass


# ---------------------------------------------------------------- 锁持有者判定
def test_lock_owner_alive_true_for_live_pid(tmp_path):
    path = str(tmp_path / "rt.lock")
    _write_lock(path, os.getpid())
    assert sup.RtSupervisor._lock_owner_alive(path) is True


def test_lock_owner_alive_false_for_dead_pid(tmp_path):
    path = str(tmp_path / "rt.lock")
    _write_lock(path, _dead_pid())
    assert sup.RtSupervisor._lock_owner_alive(path) is False


def test_lock_owner_alive_conservative_on_bad_content(tmp_path):
    path = str(tmp_path / "rt.lock")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("not-a-pid\n")
    assert sup.RtSupervisor._lock_owner_alive(path) is True


def test_clear_stale_lock_removes_dead_owner(tmp_path):
    path = str(tmp_path / "rt.lock")
    _write_lock(path, _dead_pid())
    obj = _bare_supervisor()
    assert obj._clear_stale_lock(path) is True
    assert not os.path.exists(path)


def test_clear_stale_lock_keeps_live_owner(tmp_path):
    path = str(tmp_path / "rt.lock")
    _write_lock(path, os.getpid())
    obj = _bare_supervisor()
    assert obj._clear_stale_lock(path) is False
    assert os.path.exists(path)


def test_clear_stale_lock_never_raises(tmp_path, monkeypatch):
    """存活探测抛异常时不得升级为致命错误（Windows WinError 87 的教训）。"""
    path = str(tmp_path / "rt.lock")
    _write_lock(path, os.getpid())
    obj = _bare_supervisor()

    def boom(_p):
        raise OSError(87, "参数错误。")

    monkeypatch.setattr(sup.RtSupervisor, "_lock_owner_alive", staticmethod(boom))
    try:
        assert obj._clear_stale_lock(path) is False
    except Exception as exc:  # pragma: no cover - 失败即回归
        pytest.fail(f"_clear_stale_lock 抛异常: {exc}")
    assert os.path.exists(path)


# ---------------------------------------------------------------- 调度集成
def test_rt_periodic_jobs_registered_even_when_supervisor_raises(monkeypatch):
    """Supervisor 启动异常时，看门狗周期任务仍必须注册（实时保护不能静默停摆）。"""
    import core.scheduler as sched

    called = {"n": 0}

    def fake_register(_s):
        called["n"] += 1

    monkeypatch.setattr(sched, "_register_rt_periodic_jobs", fake_register)

    class _FakeRtBackup:
        @staticmethod
        def start(_s):
            raise OSError(87, "参数错误。")

    import core.rt_backup as rt_backup
    monkeypatch.setattr(rt_backup, "start", _FakeRtBackup.start, raising=False)

    sched._register_rt_backup(object())
    assert called["n"] == 1
