# -*- coding: utf-8 -*-
"""MySQL PITR 回放的三个真机缺陷回归（2026-11-13 MySQL170 实测暴露）。

1. 目标库不存在 → ERROR 1049（应先 CREATE DATABASE IF NOT EXISTS）；
2. ROW 格式 binlog 回放必须用 ``mysql --binary-mode``，否则 mysqlbinlog 输出里的
   BINLOG 语句被**静默跳过**（rc=0、一行不写）——表现为「PITR 成功但数据没回来」；
3. 产物带 ``USE 源库`` + ROW 事件固定写源库 → 异名库恢复会把**源库覆盖掉**，
   必须硬拒绝。
"""
import core.restore_extras as rx


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _mk_dump(tmp_path, src="src"):
    p = tmp_path / "dump.sql"
    p.write_text(
        "-- MySQL dump\n-- Host: 127.0.0.1    Database: {}\n"
        "CREATE DATABASE /*!32312 IF NOT EXISTS*/ `{}`;\n"
        "USE `{}`;\n"
        "CREATE TABLE `t1` (id int);\n".format(src, src, src),
        encoding="utf-8")
    return p


def _mk_seg(tmp_path):
    p = tmp_path / "mysql-bin.000001"
    p.write_text("fake-binlog", encoding="utf-8")
    return p


def test_apply_command_uses_binary_mode(tmp_path, monkeypatch):
    """灌 binlog 回放 SQL 的 mysql 调用必须带 --binary-mode。

    注意区分两种 mysql 调用：全量导入（stdin=文件对象）不需要该参数；
    只有把 mysqlbinlog 输出（含 BINLOG '...' 语句）灌进去的那次才必须带。
    """
    binlog_applies = []

    def fake_run(cmd, **kw):
        if cmd[0] == "mysqlbinlog":
            return _R(0, "BINLOG 'AAA';\n", "")
        payload = kw.get("input")
        if cmd[0] == "mysql" and isinstance(payload, str) and "BINLOG" in payload:
            binlog_applies.append(list(cmd))
        return _R(0, "", "")

    monkeypatch.setattr(rx.subprocess, "run", fake_run)
    res = rx.mysql_pitr_restore(
        str(_mk_dump(tmp_path)), "2026-11-13T21:11:56+08:00",
        {"host": "127.0.0.1", "port": 3306, "user": "root", "password": "x",
         "db": "src", "segments": [{"path": str(_mk_seg(tmp_path)),
                                    "start_pos": 4, "stop_pos": None}]})
    assert res.get("ok"), res
    assert binlog_applies, "没有捕获到 binlog 灌库调用"
    assert all("--binary-mode" in c for c in binlog_applies), binlog_applies


def test_target_database_created_before_full_import(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return _R(0, "", "")

    monkeypatch.setattr(rx.subprocess, "run", fake_run)
    res = rx.mysql_pitr_restore(
        str(_mk_dump(tmp_path, "src")), "2026-11-13T21:11:56+08:00",
        {"host": "127.0.0.1", "port": 3306, "user": "root", "password": "x",
         "db": "newdb", "segments": []})
    assert res.get("ok") is False or res.get("ok") is True
    # 无日志段时会走「改写库名」，但无论走哪条路，必须先有 CREATE DATABASE 动作
    creates = [c for c in calls if c[0] == "mysql" and "-e" in c]
    assert any("CREATE DATABASE" in " ".join(c) for c in creates), calls


def test_cross_database_pitr_rejected(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        return _R(0, "BINLOG 'AAA';\n", "")

    monkeypatch.setattr(rx.subprocess, "run", fake_run)
    res = rx.mysql_pitr_restore(
        str(_mk_dump(tmp_path, "src")), "2026-11-13T21:11:56+08:00",
        {"host": "127.0.0.1", "port": 3306, "user": "root", "password": "x",
         "db": "other", "segments": [{"path": str(_mk_seg(tmp_path)),
                                      "start_pos": 4, "stop_pos": None}]})
    assert res.get("ok") is False
    assert "异名恢复不受支持" in (res.get("message") or "")


def test_dump_source_db_parsed(tmp_path):
    p = _mk_dump(tmp_path, "e2e_rt_my")
    assert rx._dump_source_db(str(p)) == "e2e_rt_my"
