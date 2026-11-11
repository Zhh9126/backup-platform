# -*- coding: utf-8 -*-
"""对象清单：PG 系全实例 tar 列真实库/表，二进制 .dump 不伪造表名。"""
import io
import json
import os
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import object_catalog as oc  # noqa: E402


def _make_tar(path):
    manifest = json.dumps({"databases": [{"name": "biz_a"}, {"name": "biz_b"}]})
    sql = ("CREATE TABLE public.t_order (id int);\n"
           "CREATE TABLE public.t_item (id int);\n")
    with tarfile.open(path, "w:gz") as tf:
        for name, data in (("manifest.json", manifest.encode()),
                           ("dbs/biz_a.sql", sql.encode()),
                           ("dbs/biz_b.dump", b"\x50\x47\x44\x4d\x50\x01\x02")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_pg_family_tar_lists_real_databases_and_tables():
    p = os.path.join(tempfile.mkdtemp(prefix="oc_pg_tar_"), "full.tar.gz")
    _make_tar(p)
    rows = oc._scan_pg_family_tar(p)
    dbs = {r["obj_name"] for r in rows if r["obj_type"] == "DATABASE"}
    tbls = {r["obj_name"] for r in rows if r["obj_type"] == "TABLE"}
    assert {"biz_a", "biz_b"} <= dbs
    assert {"t_order", "t_item"} <= tbls
    # .dump 二进制成员不得凭空造出表名
    assert all(r["obj_name"] not in ("\x50\x47\x44\x4d\x50",) for r in rows)


def test_pg_family_and_mysql_protocol_families_registered():
    for t in ("kingbase", "opengauss", "panwei", "highgo", "uxdb"):
        assert t in oc._PG_FAMILY
    for t in ("tidb", "oceanbase", "mariadb"):
        assert t in oc._MYSQL_FAMILY


def test_scan_dispatch_uses_pg_tar_for_kingbase(monkeypatch):
    p = os.path.join(tempfile.mkdtemp(prefix="oc_dispatch_"), "full.tar.gz")
    _make_tar(p)
    captured = {}

    def fake_replace(record_id, rows):
        captured["rows"] = rows
        return len(rows)

    monkeypatch.setattr(oc, "replace_objects", fake_replace)
    n = oc.scan(1, p, "kingbase")
    assert n >= 4
    assert any(r["obj_name"] == "t_order" for r in captured["rows"])
