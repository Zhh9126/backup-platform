# -*- coding: utf-8 -*-
"""表级完整性对象迁移单测（索引/唯一/外键/CHECK）。

纯内存：用假连接承载目录查询与 DDL 执行，不依赖任何外部数据库。
真机 E2E 见 /root/e2e/e2e_structure_extras_1122.py。
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="struct_extra_")
os.environ["META_DB_PATH"] = os.path.join(_TMP, "meta.db")
os.environ["BACKUP_ROOT"] = os.path.join(_TMP, "backups")
os.environ["SCHEDULER_ENABLED"] = "0"
os.makedirs(os.environ["BACKUP_ROOT"], exist_ok=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config  # noqa: E402
config.META_DB_PATH = os.environ["META_DB_PATH"]
config.BACKUP_ROOT = os.environ["BACKUP_ROOT"]

from core.sync import structure_extras as se  # noqa: E402
from core.sync.engine import _task_to_config  # noqa: E402


class _Cursor:
    def __init__(self, rows_by_sql, fail_prefix=None):
        self._rows_by_sql = rows_by_sql
        self._fail = fail_prefix
        self._rows = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        for marker, data in self._rows_by_sql.items():
            if marker in sql:
                self._rows = list(data)
                break
        if self._fail and self._fail in sql:
            raise RuntimeError("simulated ddl failure")

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _Conn:
    def __init__(self, rows_by_sql=None, fail_prefix=None):
        self.rows_by_sql = rows_by_sql or {}
        self.fail_prefix = fail_prefix
        self.cursors = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        c = _Cursor(self.rows_by_sql, self.fail_prefix)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_build_ddl_mysql_index_unique_fk_check():
    extras = {
        "supported": True,
        "indexes": [
            {"name": "ix_name", "columns": ["name"], "unique": False},
            {"name": "uk_code", "columns": ["code"], "unique": True},
        ],
        "constraints": [
            {"name": "uq_email", "kind": "unique", "columns": ["email"]},
            {"name": "fk_role", "kind": "foreign", "columns": ["role_id"],
             "ref_table": "roles", "ref_columns": ["id"],
             "on_delete": "CASCADE"},
            {"name": "ck_age", "kind": "check", "check_clause": "age >= 0"},
        ],
    }
    ddl = se.build_ddl("mysql", "users", extras, name_prefix="users")
    joined = "\n".join(ddl)
    assert "CREATE INDEX `users_ix_name` ON `users` (`name`)" in joined
    assert "CREATE UNIQUE INDEX `users_uk_code` ON `users` (`code`)" in joined
    assert "ADD CONSTRAINT `users_uq_email` UNIQUE (`email`)" in joined
    assert "FOREIGN KEY (`role_id`) REFERENCES `roles` (`id`) ON DELETE CASCADE" in joined
    assert "ADD CONSTRAINT `users_ck_age` CHECK (age >= 0)" in joined


def test_build_ddl_pg_reuses_index_definition():
    extras = {
        "supported": True,
        "indexes": [{"name": "ix_old",
                     "definition": "CREATE INDEX ix_old ON public.src USING btree (name)"}],
        "constraints": [{"name": "uq_email", "kind": "unique",
                         "definition": "UNIQUE (email)"}],
    }
    ddl = se.build_ddl("postgresql", "dst", extras, name_prefix="dst")
    assert len(ddl) == 2
    assert 'ON "dst"' in ddl[0]
    assert ddl[0].startswith("CREATE INDEX")
    assert ddl[1] == 'ALTER TABLE "dst" ADD CONSTRAINT "dst_uq_email" UNIQUE (email)'


def test_build_ddl_oracle_and_unsupported():
    extras = {"supported": True,
              "indexes": [{"name": "IX_A", "columns": ["A"], "unique": True}],
              "constraints": [{"name": "CK_A", "kind": "check",
                               "check_clause": "A > 0"}]}
    ddl = se.build_ddl("oracle", "T", extras, name_prefix="T")
    assert "CREATE UNIQUE INDEX" in ddl[0]
    assert "ADD CONSTRAINT" in ddl[1]
    assert se.build_ddl("redis", "t", {"supported": False}) == []


def test_extract_mysql_skips_primary_and_groups_columns():
    conn = _Conn({
        "STATISTICS": [
            ("PRIMARY", 0, "id", 1),
            ("ix_name", 1, "name", 1),
            ("ix_name", 1, "age", 2),
            ("uk_code", 0, "code", 1),
        ],
        "KEY_COLUMN_USAGE": [
            ("fk_role", "role_id", "roles", "id", 1),
        ],
        "CHECK_CONSTRAINTS": [("ck_age", "age >= 0")],
    })
    out = se.extract_structure(conn, "mysql", "users", schema="appdb")
    assert [i["name"] for i in out["indexes"]] == ["ix_name", "uk_code"]
    assert out["indexes"][0]["columns"] == ["name", "age"]
    assert out["indexes"][1]["unique"] is True
    assert out["constraints"][0]["kind"] == "foreign"
    assert out["constraints"][0]["ref_table"] == "roles"
    assert out["constraints"][1]["kind"] == "check"


def test_extract_pg_maps_constraint_kinds():
    conn = _Conn({
        "pg_constraint": [("uq_email", "u", "UNIQUE (email)"),
                          ("fk_role", "f", "FOREIGN KEY (role_id) REFERENCES roles(id)")],
        "indisprimary": [("src_pkey",)],
        "pg_indexes": [("ix_name", "CREATE INDEX ix_name ON public.src USING btree (name)"),
                       ("uq_email", "CREATE UNIQUE INDEX uq_email ON public.src USING btree (email)"),
                       ("src_pkey", "CREATE UNIQUE INDEX src_pkey ON public.src USING btree (id)")],
    })
    out = se.extract_structure(conn, "postgresql", "src", schema="public")
    assert [c["kind"] for c in out["constraints"]] == ["unique", "foreign"]
    assert [i["name"] for i in out["indexes"]] == ["ix_name"]


def test_apply_structure_continues_after_single_failure():
    extras = {"supported": True,
              "indexes": [{"name": "a", "columns": ["x"]},
                          {"name": "b", "columns": ["y"]}],
              "constraints": []}
    conn = _Conn(fail_prefix="`users_a`")
    res = se.apply_structure(conn, "mysql", "users", extras)
    assert res["total"] == 2
    assert len(res["created"]) == 1
    assert len(res["failed"]) == 1
    assert conn.rollbacks == 1
    assert conn.commits == 1


def test_task_to_config_passes_structure_flags():
    cfg = _task_to_config({"id": -1, "src_db_type": "mysql",
                           "tgt_db_type": "postgresql",
                           "migrate_indexes": True, "migrate_constraints": True})
    assert cfg.migrate_indexes is True
    assert cfg.migrate_constraints is True
    cfg2 = _task_to_config({"id": -2, "src_db_type": "mysql"})
    assert cfg2.migrate_indexes is False
    assert cfg2.migrate_constraints is False


def test_config_constants_present():
    assert config.MIG_STRUCTURE_MAX_OBJECTS >= 1
    assert isinstance(config.MIG_STRUCTURE_INDEXES_DEFAULT, bool)
    assert isinstance(config.MIG_STRUCTURE_CONSTRAINTS_DEFAULT, bool)
