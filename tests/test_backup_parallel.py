# -*- coding: utf-8 -*-
"""备份并行能力：平台并发预算 + 单任务引擎并行（core/parallel_plan.py）。

背景：平台 max_concurrent_backups 配得很小时，批量/组合任务的成员只能排队，
drift 被排队时间拉长。优化口径 = 平台并发 × 单任务引擎并行 ≈ 总并行预算，
平台并发被压小时单任务自动提升内部并行。

覆盖：
1. resolve() 的自动放大 / 任务覆盖 / 强制串行 / 硬上限钳制 / 策略上限；
2. BACKUP_PARALLEL_TARGET=0 关闭自动放大（旧行为）；
3. MySQL / PostgreSQL 引擎 _backup_parallel() 读取任务配置；
4. 全实例逻辑备份 parallel>1 时逐库 dump 真实并发（mock dump 进程统计峰值）；
5. 命令构造契约（xtrabackup --parallel / pg_dump -j / 自动切目录格式）静态回归。
"""
import os
import sys
import json
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest

import config
from core import parallel_plan as pp
from core import logical_full as lf


def _task(extra: dict = None, level: str = "core") -> dict:
    return {
        "id": 1, "name": "parallel-unit",
        "extra_options": json.dumps(extra or {}),
        "protection_level": level,
    }


# --------------------------------------------------------------------------
# 1. 并行预算解析
# --------------------------------------------------------------------------
def test_auto_boost_when_platform_limit_small(monkeypatch):
    """平台并发压到 2 路时，core 级任务自动开 4 路引擎并行（8 ÷ 2）。"""
    monkeypatch.setattr(pp, "platform_limit", lambda: 2)
    monkeypatch.setattr(pp, "policy_parallel", lambda t: 4)
    monkeypatch.setattr(config, "BACKUP_PARALLEL_TARGET", 8)
    plan = pp.resolve(_task(level="core"))
    assert plan["workers"] == 4
    assert plan["source"] == "auto"
    assert "引擎并行 4 路" in plan["label"]


def test_no_boost_when_platform_limit_ample(monkeypatch):
    """平台并发已够（≥ 预算）时回归串行，不对源库额外加压。"""
    monkeypatch.setattr(pp, "platform_limit", lambda: 8)
    monkeypatch.setattr(pp, "policy_parallel", lambda t: 4)
    monkeypatch.setattr(config, "BACKUP_PARALLEL_TARGET", 8)
    assert pp.resolve(_task())["workers"] == 1


def test_task_override_wins(monkeypatch):
    """任务显式 engine_parallel 优先于自动推算（并可越过策略上限）。"""
    monkeypatch.setattr(pp, "platform_limit", lambda: 8)
    monkeypatch.setattr(pp, "policy_parallel", lambda t: 1)
    plan = pp.resolve(_task({"engine_parallel": 6}, level="general"))
    assert plan["workers"] == 6
    assert plan["source"] == "task"


def test_task_zero_forces_serial(monkeypatch):
    """任务显式 0 = 强制串行（现场"这台库不能再压"的兜底）。"""
    monkeypatch.setattr(pp, "platform_limit", lambda: 2)
    monkeypatch.setattr(pp, "policy_parallel", lambda t: 4)
    plan = pp.resolve(_task({"engine_parallel": 0}))
    assert plan["workers"] == 1 and plan["source"] == "task"


def test_hard_cap_clamp(monkeypatch):
    """超过硬上限一律钳制，避免打错配置把源库打挂。"""
    monkeypatch.setattr(pp, "platform_limit", lambda: 2)
    monkeypatch.setattr(pp, "policy_parallel", lambda t: 4)
    monkeypatch.setattr(config, "BACKUP_PARALLEL_MAX", 8)
    assert pp.resolve(_task({"engine_parallel": 64}))["workers"] == 8


def test_policy_parallel_is_ceiling(monkeypatch):
    """保护策略的并行度是上限：important(2) 不会因自动推算变成 4。"""
    monkeypatch.setattr(pp, "platform_limit", lambda: 1)
    monkeypatch.setattr(pp, "policy_parallel", lambda t: 2)
    monkeypatch.setattr(config, "BACKUP_PARALLEL_TARGET", 8)
    assert pp.resolve(_task(level="important"))["workers"] == 2


def test_target_zero_disables_auto(monkeypatch):
    """BACKUP_PARALLEL_TARGET=0 关闭自动放大 → 引擎并行恒 1（旧行为）。"""
    monkeypatch.setattr(pp, "platform_limit", lambda: 1)
    monkeypatch.setattr(pp, "policy_parallel", lambda t: 8)
    monkeypatch.setattr(config, "BACKUP_PARALLEL_TARGET", 0)
    assert pp.resolve(_task())["workers"] == 1


def test_label_passthrough():
    assert pp.mode_label(1, 2, "auto") == "单流串行"
    assert "任务指定" in pp.mode_label(3, 2, "task")


# --------------------------------------------------------------------------
# 2. 引擎读取并行度
# --------------------------------------------------------------------------
def test_mysql_engine_reads_task_parallel():
    from core.engines.mysql import MySQLEngine
    obj = object.__new__(MySQLEngine)
    obj.task = _task({"engine_parallel": 3})
    assert obj._backup_parallel() == 3


def test_pg_engine_reads_task_parallel():
    from core.engines.postgresql import PostgreSQLEngine
    obj = object.__new__(PostgreSQLEngine)
    obj.task = _task({"engine_parallel": 2})
    assert obj._backup_parallel() == 2


# --------------------------------------------------------------------------
# 3. 全实例逐库并发 dump（真实并发语义，不造假）
# --------------------------------------------------------------------------
def _patch_full_instance(monkeypatch, tmp_path):
    """把枚举/空间/GTID 探测与逐库 dump 全部替身化，只保留并发编排。"""
    state = {"cur": 0, "peak": 0, "calls": 0}
    lock = threading.Lock()

    monkeypatch.setattr(
        lf, "enumerate_databases",
        lambda *a, **k: ("mysql", ["db1", "db2", "db3"], ""))
    monkeypatch.setattr(lf, "estimate_instance_bytes", lambda *a, **k: 0)
    monkeypatch.setattr(lf, "disk_free", lambda p: 10 ** 12)
    monkeypatch.setattr(lf, "_run", lambda *a, **k: (1, "", ""))  # GTID 探测失败→不加参数

    def fake_dump(cmd, env, out):
        with lock:
            state["cur"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.12)          # 让并发窗口真实存在
        with open(out, "w", encoding="utf-8") as f:
            f.write("-- dump\n")
        with lock:
            state["cur"] -= 1
        return 0, "", ""

    monkeypatch.setattr(lf, "_mysqldump_to_file", fake_dump)
    return state


def test_full_instance_parallel_dump(monkeypatch, tmp_path):
    state = _patch_full_instance(monkeypatch, tmp_path)
    out = str(tmp_path / "full.tar.gz")
    man = lf.backup_full_instance(
        "mysql", host="127.0.0.1", port=3306, user="u", password="p",
        dump_tool="/bin/true", out_path=out, query_tool="/bin/true",
        parallel=3)
    assert state["calls"] == 3
    assert state["peak"] == 3, "并行度 3 时应有 3 个 dump 同时进行"
    assert man["parallel_dump"] == 3
    assert man["databases"] == ["db1", "db2", "db3"]
    assert os.path.getsize(out) > 0


def test_full_instance_serial_by_default(monkeypatch, tmp_path):
    state = _patch_full_instance(monkeypatch, tmp_path)
    out = str(tmp_path / "full_serial.tar.gz")
    man = lf.backup_full_instance(
        "mysql", host="127.0.0.1", port=3306, user="u", password="p",
        dump_tool="/bin/true", out_path=out, query_tool="/bin/true")
    assert state["peak"] == 1, "未指定 parallel 时必须保持串行（旧行为）"
    assert man["parallel_dump"] == 1


def test_full_instance_parallel_failure_reported(monkeypatch, tmp_path):
    """并行下任一库失败仍如实整任务失败，并保留该库的真实诊断。"""
    state = {"cur": 0, "peak": 0, "calls": 0}
    lock = threading.Lock()

    monkeypatch.setattr(
        lf, "enumerate_databases",
        lambda *a, **k: ("mysql", ["okdb", "baddb"], ""))
    monkeypatch.setattr(lf, "estimate_instance_bytes", lambda *a, **k: 0)
    monkeypatch.setattr(lf, "disk_free", lambda p: 10 ** 12)
    monkeypatch.setattr(lf, "_run", lambda *a, **k: (1, "", ""))

    def fake_dump(cmd, env, out):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.05)
        with lock:
            state["cur"] -= 1
        if "baddb" in " ".join(cmd):
            return 2, "", "Access denied for user"
        with open(out, "w", encoding="utf-8") as f:
            f.write("-- dump\n")
        return 0, "", ""

    monkeypatch.setattr(lf, "_mysqldump_to_file", fake_dump)
    with pytest.raises(RuntimeError) as ei:
        lf.backup_full_instance(
            "mysql", host="127.0.0.1", port=3306, user="u", password="p",
            dump_tool="/bin/true", out_path=str(tmp_path / "bad.tar.gz"),
            query_tool="/bin/true", parallel=2)
    assert "baddb" in str(ei.value)
    assert "Access denied" in str(ei.value)


# --------------------------------------------------------------------------
# 4. 命令构造契约（静态回归：改坏了要能立刻发现）
# --------------------------------------------------------------------------
def test_command_contract():
    base = os.path.join(ROOT, "core")
    mysql_src = open(os.path.join(base, "engines", "mysql.py"),
                     encoding="utf-8", errors="replace").read()
    # 本机 + 远端物理备份都要带 --parallel
    assert mysql_src.count("--parallel={bk_parallel}") >= 1
    assert "par_flag" in mysql_src and "--parallel=" in mysql_src

    pg_src = open(os.path.join(base, "engines", "postgresql.py"),
                  encoding="utf-8", errors="replace").read()
    assert '"-j", str(bpar)' in pg_src
    assert "已自动切换目录格式 -Fd" in pg_src

    rd_src = open(os.path.join(base, "remote_dump.py"),
                  encoding="utf-8", errors="replace").read()
    assert "-j %d" in rd_src
