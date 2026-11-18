# -*- coding: utf-8 -*-
"""Oracle/达梦「段消费模式」行级 CDC 回归（2026-11-14）。

背景：这两个库的行级事件不再独立抓日志，而是消费实时备份已落盘的
LogMiner jsonl 段。段里的字段是 redo/undo/owner/table（平台自产格式），
而解析器原本只认 sql_redo/sql_undo/seg_owner/table_name —— 不做兼容会让
整段事件被静默丢弃；同时 SQL_UNDO 此前未入库，导致 undo 回滚形同虚设。
"""
import json

from core.cdc.rowlevel import SqlRedoRowParser, EventStore, _probe_segment_mode


SEG_LINE = json.dumps({
    "scn": "5827195", "ts": "2026-11-07 01:43:06", "owner": "C##E2E17",
    "table": "T_DEEP", "op": "INSERT",
    "redo": 'insert into "C##E2E17"."T_DEEP"("ID","NAME") values ("1",\'订单-甲\');',
    "undo": 'delete from "C##E2E17"."T_DEEP" where "ID" = \'1\';',
    "xid": "1.2.3",
})


def test_parse_platform_logminer_segment_format():
    """平台 LogMiner 段（redo/undo/owner/table）必须能被解析出事件。"""
    p = SqlRedoRowParser()
    evs = p.feed(SEG_LINE)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["op"] == "INSERT"
    assert ev["table"] == "T_DEEP"
    assert ev["schema"] == "C##E2E17"
    assert "订单-甲" in ev["sql_text"]
    assert "delete from" in ev["sql_undo"].lower()
    assert ev["position"] == "5827195"


def test_parse_external_field_names_still_work():
    """外部喂入用的 sql_redo/seg_owner/table_name 仍需兼容（向后兼容）。"""
    line = json.dumps({"SQL_REDO": "delete from T where id=1;",
                       "SEG_OWNER": "S", "TABLE_NAME": "T", "SCN": "100"})
    evs = SqlRedoRowParser().feed(line)
    assert len(evs) == 1
    assert evs[0]["op"] == "DELETE"
    assert evs[0]["table"] == "T"


def test_undo_sql_used_for_rollback():
    """gen_sql(undo) 必须用 SQL_UNDO 生成撤销语句（修复前直接跳过）。"""
    p = SqlRedoRowParser()
    evs = p.feed(SEG_LINE)
    events = [dict(e, id=1, before={}, after={}) for e in evs]
    redo = EventStore.gen_sql(events, dialect="oracle", mode="redo")
    undo = EventStore.gen_sql(events, dialect="oracle", mode="undo")
    assert len(redo) == 1 and "insert into" in redo[0].lower()
    assert len(undo) == 1 and "delete from" in undo[0].lower()


def test_undo_missing_is_skipped_not_crash():
    """没有 SQL_UNDO 时 undo 应安全跳过，不产出垃圾语句。"""
    line = json.dumps({"redo": "insert into t values (1);", "table": "t"})
    evs = SqlRedoRowParser().feed(line)
    events = [dict(e, id=1) for e in evs]
    assert EventStore.gen_sql(events, mode="undo") == []


def test_non_dml_is_ignored():
    line = json.dumps({"redo": "select * from t", "table": "t"})
    assert SqlRedoRowParser().feed(line) == []


def test_probe_requires_ref_id():
    res = _probe_segment_mode("oracle", 0)
    assert res["ok"] is False
    assert res["mode"] == "segment_consumer"
    assert "ref_id" in res["message"]
