# -*- coding: utf-8 -*-
"""目录灾备经 CLI 执行，验证 SQLite WAL、密钥恢复和失败隔离。"""
import json
import os
import sqlite3
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

import config
from core import db


ROOT = Path(__file__).resolve().parents[1]
SERVICE_SECRET = "隔离测试平台密钥"


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    source = tmp_path / "平台 #1.db"
    key_file = tmp_path / "catalog.key"
    key_file.write_bytes(os.urandom(32))
    key_file.chmod(0o600)
    (tmp_path / "auth_secret.json").write_text(
        json.dumps({"secret_key": SERVICE_SECRET}), encoding="utf-8"
    )
    new_instance = tmp_path / "unused-instance"
    env = dict(os.environ, INSTANCE_DIR=str(new_instance), TMPDIR=str(tmp_path))
    env.pop("SECRET_KEY", None)

    def cli(*args, key=key_file, extra_env=None):
        result = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "catalog",
             *map(str, args), "--key-file", str(key)],
            cwd=tmp_path,
            env={**env, **(extra_env or {})},
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert not new_instance.exists()
        return result

    with closing(sqlite3.connect(source)) as conn:
        conn.executescript(db.SCHEMA)
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        conn.execute("PRAGMA wal_autocheckpoint=0")
        monkeypatch.setattr(config, "SECRET_KEY", SERVICE_SECRET)
        conn.execute(
            "INSERT INTO backup_tasks(name, db_type, password) VALUES (?,?,?)",
            ("业务主库", "postgresql", db.encrypt_secret("测试凭据")),
        )
        conn.execute("CREATE TABLE test_stream (data BLOB)")
        conn.execute("INSERT INTO test_stream VALUES (zeroblob(2200000))")
        conn.commit()
        conn.execute("INSERT INTO backup_tasks(name, db_type) VALUES ('未提交', 'postgresql')")
        yield source, cli


def test_catalog_roundtrip_while_wal_writer_is_open(catalog, tmp_path, monkeypatch):
    source, cli = catalog
    package = tmp_path / "catalog.aidbm"
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    assert Path(str(source) + "-wal").stat().st_size > 2_000_000

    created = cli("create", "--source", source, "--output", package)
    assert created.returncode == 0, created.stderr
    assert stat.S_IMODE(package.stat().st_mode) == 0o600
    assert SERVICE_SECRET.encode() not in package.read_bytes()

    verified = cli("verify", package)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["db_bytes"] > 2_000_000

    restored = cli("recover", package, "--directory", recovery)
    assert restored.returncode == 0, restored.stderr
    directory = Path(json.loads(restored.stdout)["directory"])
    assert directory.parent == recovery
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert {p.name for p in directory.iterdir()} == {"meta.db", "auth_secret.json"}
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.iterdir())
    secret = json.loads((directory / "auth_secret.json").read_text())["secret_key"]
    monkeypatch.setattr(config, "SECRET_KEY", secret)

    with closing(sqlite3.connect(directory / "meta.db")) as db_copy:
        rows = db_copy.execute("SELECT name, password FROM backup_tasks").fetchall()
        assert [name for name, _ in rows] == ["业务主库"]
        assert db.decrypt_secret(rows[0][1]) == "测试凭据"
        assert db_copy.execute("SELECT length(data) FROM test_stream").fetchone() == (2200000,)


@pytest.mark.parametrize("damage", ["wrong_key", "tamper", "truncated", "appended"])
def test_catalog_rejects_bad_package_without_recovery_files(catalog, tmp_path, damage):
    source, cli = catalog
    package = tmp_path / "catalog.aidbm"
    assert cli("create", "--source", source, "--output", package).returncode == 0
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    kwargs = {}
    if damage == "wrong_key":
        key = tmp_path / "wrong.key"
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        kwargs["key"] = key
    else:
        data = bytearray(package.read_bytes())
        if damage == "tamper":
            data[len(data) // 2] ^= 1
        elif damage == "truncated":
            del data[-(len(data) // 3):]
        else:
            data.extend(b"extra")
        package.write_bytes(data)

    result = cli("recover", package, "--directory", recovery, **kwargs)
    assert result.returncode == 1
    assert "目录灾备失败" in result.stderr
    assert not list(recovery.iterdir())


def test_catalog_existing_output_and_missing_source_are_safe(catalog, tmp_path):
    source, cli = catalog
    package = tmp_path / "catalog.aidbm"
    package.write_bytes(b"existing backup")
    existing = cli("create", "--source", source, "--output", package)
    assert existing.returncode == 1
    assert package.read_bytes() == b"existing backup"

    missing = tmp_path / "missing.db"
    output = tmp_path / "unused.aidbm"
    result = cli("create", "--source", missing, "--output", output)
    assert result.returncode == 1
    assert not output.exists()
    assert not missing.exists()


def test_catalog_rejects_foreign_sqlite_and_insecure_key(catalog, tmp_path):
    source, cli = catalog
    foreign = tmp_path / "foreign.db"
    with closing(sqlite3.connect(foreign)) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    output = tmp_path / "unused.aidbm"
    result = cli("create", "--source", foreign, "--output", output)
    assert result.returncode == 1
    assert "缺少 AIDBM 目录表" in result.stderr

    (tmp_path / "catalog.key").chmod(0o644)
    result = cli("create", "--source", source, "--output", output)
    assert result.returncode == 1
    assert "chmod 600" in result.stderr
    assert not output.exists()


def test_catalog_concurrent_runs_publish_only_one_package(catalog, tmp_path):
    source, cli = catalog
    package = tmp_path / "catalog.aidbm"

    def create():
        return cli("create", "--source", source, "--output", package).returncode

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(create)
        second = executor.submit(create)
        assert sorted([first.result(), second.result()]) == [0, 1]

    assert cli("verify", package).returncode == 0
    assert not list(tmp_path.glob(".aidbm-catalog-*"))
