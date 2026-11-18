# -*- coding: utf-8 -*-
"""插件体系三项增强回归（2026-11-14）。

1. **引擎选路真正生效**：``use_plugin=off`` 时物理通道被拦（此前只有策略判定，
   引擎照跑）；``auto`` 且无插件时任务在执行入口降级为逻辑备份；
2. **离线插件包**：常用组合打成一个 tar.gz，现场一次导入；导入逐项校验
   sha256，被篡改的包必须拒绝安装；
3. **版本矩阵**：期望版本 vs 各主机已装版本，给出 ok/outdated/missing 与升级建议。
"""
import os

import pytest

from core import plugin_bundle as pb
from core import plugin_policy as pp
from core import plugin_version_matrix as pvm
from core.engines.base import BackupMode
from core.engines.mysql import MySQLEngine


def _task(mode="physical", use_plugin="off"):
    return {"id": 1, "name": "t", "db_type": "mysql", "host": "127.0.0.1",
            "port": 3306, "username": "root", "password": "x",
            "db_name": "db", "backup_mode": mode,
            "extra_options": '{"use_plugin": "%s"}' % use_plugin}


# --------------------------------------------------------------- 引擎选路
def test_off_blocks_physical_channel():
    blocked, why = pp.physical_blocked(_task())
    assert blocked is True
    assert "不使用备份插件" in why


def test_off_downgrades_task_to_logical():
    t = pp.effective_task(_task())
    assert t["backup_mode"] == "logical"
    assert t.get("_plugin_downgraded"), "应记录降级原因"


def test_preflight_blocks_physical_when_plugin_off():
    eng = MySQLEngine(_task(), "/tmp")
    ok, msg = eng.preflight()
    assert ok is False
    assert "不使用备份插件" in msg


def test_logical_mode_not_affected_by_policy():
    t = pp.effective_task(_task(mode="logical"))
    assert t["backup_mode"] == "logical"
    assert not t.get("_plugin_downgraded")


# --------------------------------------------------------- 版本比较与矩阵
def test_version_compare_states():
    assert pvm.compare("8.0.35", "8.0.35")[0] == "ok"
    assert pvm.compare("2.4.29", "8.0.35")[0] == "outdated"
    assert pvm.compare("100.10.0", "100.10.0")[0] == "ok"
    assert pvm.compare("", "1.0")[0] == "missing"
    # 内置工具不比较版本（避免误报 unknown）
    assert pvm.compare("builtin", "builtin")[0] == "ok"
    # 数字段比较：1.2.10 > 1.2.9
    assert pvm.compare("1.2.10", "1.2.9")[0] == "newer"


def test_matrix_shape_and_suggestions_consistent():
    d = pvm.matrix()
    assert {"rows", "summary", "suggestions", "generated_at"} <= set(d)
    assert d["summary"]["ok"] + d["summary"]["missing"] + \
        d["summary"]["outdated"] + d["summary"]["newer"] + \
        d["summary"]["unknown"] == len(d["rows"])
    # 建议必须能对应到具体插件
    ids = {r["plugin"] for r in d["rows"]}
    for s in d["suggestions"]:
        assert s["plugin"] in ids
        assert s.get("command"), "升级/安装建议必须给出可操作命令"


# ------------------------------------------------------------- 离线插件包
def test_bundle_build_inspect_import(tmp_path, monkeypatch):
    # 造一个假离线包，模拟现场已放入 offline_packages/<pid>/
    pkg_dir = tmp_path / "offline" / "zstd"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "zstd-1.5.5.tar.gz").write_bytes(b"FAKE-PACKAGE" * 50)
    monkeypatch.setattr(pb.plugin_installer, "OFFLINE_PKG_DIR", tmp_path / "offline")

    out = str(tmp_path / "bundle.tar.gz")
    res = pb.build(["zstd", "lvm-snapshot"], out_path=out, note="ut")
    assert res["ok"] and os.path.isfile(out)
    assert res["plugin_count"] == 2
    assert res["with_package"] == 1
    # 没离线包的插件要如实列出（不假装能离线装）
    assert any(m["id"] == "lvm-snapshot" for m in res["missing_packages"])

    ins = pb.inspect(out)
    assert ins["ok"] and ins["meta"]["format"] == pb.BUNDLE_FORMAT

    imp = pb.import_bundle(out, install=False)
    assert imp["ok"] and imp["integrity_errors"] == []
    row = [r for r in imp["results"] if r["id"] == "zstd"][0]
    assert row["offline_package"], "离线包应落地到 offline_packages/<pid>/"


def test_bundle_rejects_tampered_package(tmp_path, monkeypatch):
    import tarfile
    pkg_dir = tmp_path / "offline" / "zstd"
    pkg_dir.mkdir(parents=True)
    pkg = pkg_dir / "zstd.tar.gz"
    pkg.write_bytes(b"ORIGINAL" * 40)
    monkeypatch.setattr(pb.plugin_installer, "OFFLINE_PKG_DIR", tmp_path / "offline")
    out = str(tmp_path / "b.tar.gz")
    assert pb.build(["zstd"], out_path=out)["ok"]

    # 解包 → 篡改包内文件 → 重新打包（bundle.json 里的 sha256 保持旧值）
    work = tmp_path / "work"
    work.mkdir()
    with tarfile.open(out) as t:
        t.extractall(work)
    (work / "packages" / "zstd" / "zstd.tar.gz").write_bytes(b"TAMPERED")
    bad = str(tmp_path / "bad.tar.gz")
    with tarfile.open(bad, "w:gz") as t:
        for root, _dirs, files in os.walk(work):
            for n in files:
                full = os.path.join(root, n)
                t.add(full, arcname=os.path.relpath(full, work))

    res = pb.import_bundle(bad, install=False)
    assert res["ok"] is False
    assert any("sha256" in (e.get("reason") or "") for e in res["integrity_errors"])


def test_bundle_rejects_bad_format(tmp_path):
    bogus = tmp_path / "x.tar.gz"
    bogus.write_bytes(b"not a tarball")
    assert pb.inspect(str(bogus))["ok"] is False
    assert pb.import_bundle(str(bogus), install=False)["ok"] is False
