# -*- coding: utf-8 -*-
"""自定义类型映射覆盖（对标 KDTS 类型映射自定义）单测。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
os.environ["META_DB_PATH"] = os.path.join(ROOT, "tests", ".tmp_tm", "meta.db")
os.makedirs(os.path.dirname(os.environ["META_DB_PATH"]), exist_ok=True)

from core import db as _db
_db.init_schema()
from core.sync.engine import _task_to_config
from core.sync.plugins.base import matrix_suggest


def test_override_hit_direct():
    task = {"id": -1, "name": "t", "src_db_type": "mysql", "src_host": "h",
            "tgt_db_type": "postgresql",
            "type_mapping": {"decimal": "NUMBER(10,2)"}}
    cfg = _task_to_config(task)
    out = matrix_suggest(cfg, "postgresql", "decimal(10,2)")
    assert out == "NUMBER(10,2)", out


def test_override_case_insensitive_no_prec():
    task = {"id": -1, "name": "t", "src_db_type": "mysql", "src_host": "h",
            "tgt_db_type": "postgresql", "type_mapping": {"VARCHAR": "CLOB"}}
    cfg = _task_to_config(task)
    assert matrix_suggest(cfg, "postgresql", "varchar(255)") == "CLOB"


def test_no_override_falls_back_matrix():
    task = {"id": -1, "name": "t", "src_db_type": "mysql", "src_host": "h",
            "tgt_db_type": "postgresql"}
    cfg = _task_to_config(task)
    out = matrix_suggest(cfg, "postgresql", "int")
    assert out and out != "NUMBER(10,2)"   # 走默认矩阵（int→INTEGER 等）


def test_empty_mapping_neutral():
    task = {"id": -1, "name": "t", "src_db_type": "mysql", "src_host": "h",
            "tgt_db_type": "postgresql", "type_mapping": {}}
    cfg = _task_to_config(task)
    assert matrix_suggest(cfg, "postgresql", "int")  # 不带映射时行为不变
