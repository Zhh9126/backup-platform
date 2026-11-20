# -*- coding: utf-8 -*-
"""平台目录灾备的离线命令行入口。"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from core import catalog_backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{Path(sys.argv[0]).name} catalog",
        description="AIDBM SQLite 元数据库目录灾备",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="创建加密的在线目录快照")
    create.add_argument("--source", type=Path, required=True, help="在用的 SQLite 元数据库文件")
    create.add_argument("--output", type=Path, required=True, help="新灾备包的路径")
    create.add_argument("--auth-secret-file", type=Path,
                        help="平台 auth_secret.json，默认由 SECRET_KEY 或源库同目录提供")

    verify = commands.add_parser("verify", help="检查包认证与 SQLite 数据库完整性")
    verify.add_argument("package", type=Path)

    recover = commands.add_parser("recover", help="校验后提取目录到新的私有目录")
    recover.add_argument("package", type=Path)
    recover.add_argument("--directory", type=Path, required=True, help="恢复目录的现有父目录")

    for subcommand in (create, verify, recover):
        subcommand.add_argument("--key-file", type=Path, required=True,
                                help="仅所有者可访问的 32 字节灾备密钥文件")

    args = parser.parse_args(argv)
    try:
        key = catalog_backup.read_package_key(args.key_file)
        if args.command == "create":
            secret = catalog_backup.read_service_secret(args.source, args.auth_secret_file)
            result = catalog_backup.create_package(args.source, args.output, key, secret)
            result["package"] = str(args.output.resolve())
        elif args.command == "verify":
            result = catalog_backup.verify_package(args.package, key)
        else:
            result = catalog_backup.export_package(args.package, args.directory, key)
    except (catalog_backup.CatalogError, sqlite3.Error, OSError) as exc:
        print(f"目录灾备失败：{exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0
