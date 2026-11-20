# -*- coding: utf-8 -*-
"""SQLite 元数据库的在线快照、加密归档与离线恢复。"""
import json
import os
import sqlite3
import stat
import struct
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


_MAGIC = b"AIDBM-CATALOG\x01"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_KEY_BYTES = 32
_BLOCK_BYTES = 1 << 20
_MAX_METADATA_BYTES = 64 << 10
_GCM_MAX_BYTES = (1 << 36) - 32
_REQUIRED_TABLES = {"backup_tasks", "backup_records", "storage_targets", "system_config"}


class CatalogError(RuntimeError):
    """目录快照或灾备包无法使用。"""


def read_package_key(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as fp:
        mode = os.fstat(fp.fileno()).st_mode
        if not stat.S_ISREG(mode):
            raise CatalogError("灾备密钥必须是普通文件")
        if os.name == "posix" and mode & 0o077:
            raise CatalogError("灾备密钥文件须仅允许所有者访问，请设置 chmod 600")
        key = fp.read(_KEY_BYTES + 1)
    if len(key) != _KEY_BYTES:
        raise CatalogError("灾备密钥须为 32 字节的随机二进制文件")
    return key


def read_service_secret(source: Path, secret_file: Path | None) -> str:
    """读取现有密钥，避免启动应用时自动生成新密钥。"""
    secret = os.environ.get("SECRET_KEY") if secret_file is None else None
    if not secret:
        path = secret_file if secret_file is not None else source.parent / "auth_secret.json"
        try:
            with path.open(encoding="utf-8") as fp:
                content = json.loads(fp.read(_MAX_METADATA_BYTES + 1))
            secret = content["secret_key"]
        except (UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise CatalogError("平台密钥文件缺少有效的 secret_key") from exc
    if not isinstance(secret, str) or not secret:
        raise CatalogError("平台密钥文件缺少有效的 secret_key")
    return secret


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def _check_database(path: Path) -> None:
    with closing(_open_readonly(path)) as conn:
        conn.execute("PRAGMA trusted_schema=OFF")
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        if not _REQUIRED_TABLES.issubset(tables):
            raise CatalogError("SQLite 文件缺少 AIDBM 目录表")
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise CatalogError("SQLite 目录完整性检查失败")


def _snapshot(source: Path, target: Path) -> None:
    with closing(_open_readonly(source)) as live, closing(sqlite3.connect(target)) as copy:
        live.backup(copy, pages=256)
        # 一份可搬移的文件包含已提交的 WAL 数据，恢复时无需 sidecar。
        if copy.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise CatalogError("SQLite 快照无法转为独立数据库文件")
    _check_database(target)


def _cipher(key: bytes, header: bytes) -> Cipher:
    if len(key) != _KEY_BYTES:
        raise CatalogError("灾备密钥须为 32 字节")
    return Cipher(algorithms.AES(key), modes.GCM(header[len(_MAGIC):]))


def create_package(source: Path, output: Path, key: bytes, service_secret: str) -> dict:
    """以 SQLite Online Backup API 读取快照，验证后原子发布加密包。"""
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"灾备包已存在：{output}")
    source = source.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="aidbm-catalog-") as work:
        snapshot = Path(work) / "meta.db"
        _snapshot(source, snapshot)
        summary = {
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "db_bytes": snapshot.stat().st_size,
        }
        metadata = json.dumps({**summary, "secret_key": service_secret},
                              ensure_ascii=False).encode("utf-8")
        if not service_secret or len(metadata) > _MAX_METADATA_BYTES:
            raise CatalogError("平台密钥为空或过长")
        if 4 + len(metadata) + summary["db_bytes"] > _GCM_MAX_BYTES:
            raise CatalogError("元数据库超出单个 AES-GCM 目录灾备包的容量上限")
        header = _MAGIC + os.urandom(_NONCE_BYTES)
        encryptor = _cipher(key, header).encryptor()
        encryptor.authenticate_additional_data(header)

        # 仅密文进入归档目标。链接发布确保并发执行时也不会覆盖现有包。
        with tempfile.TemporaryDirectory(prefix=".aidbm-catalog-", dir=output.parent) as stage:
            staged = Path(stage) / "package"
            with open(staged, "xb", opener=_open_private) as out, snapshot.open("rb") as db:
                out.write(header)
                out.write(encryptor.update(struct.pack(">I", len(metadata)) + metadata))
                while chunk := db.read(_BLOCK_BYTES):
                    out.write(encryptor.update(chunk))
                out.write(encryptor.finalize())
                out.write(encryptor.tag)
                out.flush()
                os.fsync(out.fileno())
            os.link(staged, output)
    return summary


def _open_private(path, flags):
    return os.open(path, flags, 0o600)


def _read_exact(fp, size: int) -> bytes:
    data = fp.read(size)
    if len(data) != size:
        raise CatalogError("目录灾备包不完整")
    return data


def _decode(package: Path, key: bytes, snapshot: Path) -> tuple[dict, str]:
    """认证整个包，然后检查恢复出的 SQLite 文件和密钥信息。"""
    with package.open("rb") as fp:
        header = _read_exact(fp, len(_MAGIC) + _NONCE_BYTES)
        if not header.startswith(_MAGIC):
            raise CatalogError("目录灾备包格式或版本不受支持")
        remaining = os.fstat(fp.fileno()).st_size - len(header) - _TAG_BYTES - 4
        if remaining <= 0:
            raise CatalogError("目录灾备包不完整")
        if remaining + 4 > _GCM_MAX_BYTES:
            raise CatalogError("目录灾备包超出单包容量上限")
        decryptor = _cipher(key, header).decryptor()
        decryptor.authenticate_additional_data(header)
        metadata_size, = struct.unpack(">I", decryptor.update(_read_exact(fp, 4)))
        if not 0 < metadata_size < remaining or metadata_size > _MAX_METADATA_BYTES:
            raise CatalogError("灾备包认证失败，请检查密钥及文件完整性")
        metadata_bytes = decryptor.update(_read_exact(fp, metadata_size))
        remaining -= metadata_size
        with open(snapshot, "xb", opener=_open_private) as db:
            while remaining:
                chunk = _read_exact(fp, min(remaining, _BLOCK_BYTES))
                db.write(decryptor.update(chunk))
                remaining -= len(chunk)
            try:
                db.write(decryptor.finalize_with_tag(_read_exact(fp, _TAG_BYTES)))
            except InvalidTag as exc:
                raise CatalogError("灾备包认证失败，请检查密钥及文件完整性") from exc
        if fp.read(1):
            raise CatalogError("目录灾备包包含多余数据")

    try:
        metadata = json.loads(metadata_bytes)
        secret = metadata["secret_key"]
        db_bytes = metadata["db_bytes"]
        created = metadata["created_utc"]
    except (UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise CatalogError("目录灾备包的元数据无效") from exc
    if not isinstance(secret, str) or not secret or not isinstance(created, str):
        raise CatalogError("目录灾备包的元数据无效")
    if db_bytes != snapshot.stat().st_size:
        raise CatalogError("目录灾备包的数据库大小不一致")
    _check_database(snapshot)
    return {"created_utc": created, "db_bytes": db_bytes}, secret


def verify_package(package: Path, key: bytes) -> dict:
    with tempfile.TemporaryDirectory(prefix="aidbm-catalog-") as work:
        summary, _ = _decode(package, key, Path(work) / "meta.db")
    return summary


def export_package(package: Path, directory: Path, key: bytes) -> dict:
    """校验通过后输出到新的私有目录，保留当前平台的元数据库。"""
    with tempfile.TemporaryDirectory(prefix=".aidbm-catalog-", dir=directory) as work:
        snapshot = Path(work) / "meta.db"
        summary, secret = _decode(package, key, snapshot)
        payload_dir = Path(work) / "recovery"
        payload_dir.mkdir(mode=0o700)
        secret_file = payload_dir / "auth_secret.json"
        with open(secret_file, "x", encoding="utf-8", opener=_open_private) as fp:
            json.dump({"secret_key": secret}, fp, ensure_ascii=False)
            fp.flush()
            os.fsync(fp.fileno())
        with snapshot.open("rb+") as fp:
            os.fsync(fp.fileno())
        snapshot.rename(payload_dir / snapshot.name)

        result = Path(tempfile.mkdtemp(prefix="aidbm-recovery-", dir=directory))
        try:
            payload_dir.replace(result)
        except OSError:
            result.rmdir()
            raise
    return {**summary, "directory": str(result.resolve())}
