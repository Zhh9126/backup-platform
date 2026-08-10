# -*- coding: utf-8 -*-
"""backup_records.checksum 一次性回填运维脚本（UX-20260801 T01）。

背景：
    AI 告警新增的「数据验证」维度（metric=verify_fail）的 L1 完整性校验
    依赖 ``backup_records.checksum``。部分引擎（mongodb / mysql 物理备份 /
    DEMO 仿真）落库时 checksum 为空，导致 L1 无基准可比。
    本脚本扫描 checksum 为空且备份文件仍存在的记录，用文件 sha256 回填。

设计约束：
    * 只读补空，**绝不覆盖**已有 checksum（避免把损坏文件的新哈希写成基准）。
    * 仿真记录（is_simulated=1）默认跳过——它们没有真实文件。
    * 不被任何主流程调用，仅作运维手工执行。

用法::

    # 预演，不写库（推荐先跑）
    python scripts/backfill_checksum.py --dry-run

    # 实际回填，最多处理 200 条
    python scripts/backfill_checksum.py --limit 200

    # 连仿真记录一起处理（一般不需要）
    python scripts/backfill_checksum.py --include-simulated

退出码：0 成功；1 参数错误或执行异常。
"""
import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config  # noqa: E402,F401  （必须先导入以初始化路径/环境）
import core.db as db  # noqa: E402


# 单文件 sha256 的默认体积上限（MB）。超过则跳过，避免运维时段打满磁盘 IO。
DEFAULT_MAX_FILE_MB = 2048


def _parse_args(argv: list) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="backfill_checksum",
        description="回填 backup_records.checksum（sha256），支持预演与限量。")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="只统计与打印，不执行 UPDATE")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理多少条记录（0 表示不限）")
    parser.add_argument("--max-file-mb", type=int, default=DEFAULT_MAX_FILE_MB,
                        help=f"跳过超过该体积的文件，默认 {DEFAULT_MAX_FILE_MB} MB")
    parser.add_argument("--include-simulated", action="store_true", default=False,
                        help="同时处理 is_simulated=1 的仿真记录（默认跳过）")
    parser.add_argument("--verbose", action="store_true", default=False,
                        help="逐条打印处理明细")
    return parser.parse_args(argv)


def _fetch_candidates(include_simulated: bool, limit: int) -> list:
    """查询 checksum 为空且有备份路径的记录。

    Args:
        include_simulated: 是否包含仿真记录。
        limit: 最大条数，<=0 表示不限。

    Returns:
        记录 dict 列表（含 id / task_id / backup_path / size_bytes）。
    """
    sql = ("SELECT id, task_id, backup_path, size_bytes, is_simulated "
           "FROM backup_records "
           "WHERE (checksum IS NULL OR checksum='') "
           "AND backup_path IS NOT NULL AND backup_path<>'' ")
    if not include_simulated:
        sql += "AND COALESCE(is_simulated,0)=0 "
    sql += "ORDER BY id DESC"
    params: tuple = ()
    if limit and limit > 0:
        sql += " LIMIT ?"
        params = (int(limit),)
    try:
        return db.query(sql, params)
    except Exception as exc:
        print(f"[错误] 查询待回填记录失败: {exc}")
        return []


def backfill(dry_run: bool = False, limit: int = 0,
             max_file_mb: int = DEFAULT_MAX_FILE_MB,
             include_simulated: bool = False,
             verbose: bool = False) -> dict:
    """执行回填。

    Args:
        dry_run: 为 True 时不写库。
        limit: 最大处理条数，0 表示不限。
        max_file_mb: 超过该体积（MB）的文件跳过。
        include_simulated: 是否处理仿真记录。
        verbose: 是否逐条打印。

    Returns:
        统计 dict：candidates / updated / skipped_missing / skipped_large /
        failed / sql_executed。
    """
    stats = {
        "candidates": 0,
        "updated": 0,
        "skipped_missing": 0,
        "skipped_large": 0,
        "failed": 0,
        "sql_executed": 0,
    }
    rows = _fetch_candidates(include_simulated, limit)
    stats["candidates"] = len(rows)
    if not rows:
        print("[信息] 没有需要回填的记录（checksum 均已填充或无备份路径）。")
        return stats

    max_bytes = max(1, int(max_file_mb)) * 1024 * 1024
    for row in rows:
        rec_id = int(row.get("id") or 0)
        path = str(row.get("backup_path") or "")
        if not path or not os.path.isfile(path):
            stats["skipped_missing"] += 1
            if verbose:
                print(f"  跳过 #{rec_id}: 文件不存在 {path}")
            continue
        try:
            real_size = os.path.getsize(path)
        except OSError as exc:
            stats["failed"] += 1
            print(f"  失败 #{rec_id}: 读取文件大小失败 {exc}")
            continue
        if real_size > max_bytes:
            stats["skipped_large"] += 1
            if verbose:
                print(f"  跳过 #{rec_id}: 文件 {real_size} 字节 超过上限 {max_bytes}")
            continue
        try:
            digest = db.sha256_file(path)
        except Exception as exc:
            stats["failed"] += 1
            print(f"  失败 #{rec_id}: 计算 sha256 失败 {exc}")
            continue
        if not digest:
            stats["failed"] += 1
            print(f"  失败 #{rec_id}: sha256 结果为空")
            continue
        if dry_run:
            stats["updated"] += 1
            print(f"  [预演] UPDATE backup_records SET checksum='{digest[:16]}…' "
                  f"WHERE id={rec_id}  ({real_size} 字节)")
            continue
        try:
            # 二次防覆盖：仅在 checksum 仍为空时写入（并发安全）
            db.execute(
                "UPDATE backup_records SET checksum=? WHERE id=? "
                "AND (checksum IS NULL OR checksum='')",
                (digest, rec_id))
            stats["updated"] += 1
            stats["sql_executed"] += 1
            if verbose:
                print(f"  回填 #{rec_id}: {digest[:16]}… ({real_size} 字节)")
        except Exception as exc:
            stats["failed"] += 1
            print(f"  失败 #{rec_id}: 写库异常 {exc}")
    return stats


def main(argv: list = None) -> int:
    """脚本入口。返回进程退出码。"""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.limit < 0:
        print("[错误] --limit 不能为负数")
        return 1
    if args.max_file_mb <= 0:
        print("[错误] --max-file-mb 必须为正整数")
        return 1
    try:
        db.init_schema()
    except Exception as exc:
        print(f"[错误] 初始化数据库结构失败: {exc}")
        return 1

    mode = "预演（不写库）" if args.dry_run else "实际执行"
    print(f"=== backup_records.checksum 回填 · {mode} ===")
    try:
        stats = backfill(dry_run=args.dry_run, limit=args.limit,
                         max_file_mb=args.max_file_mb,
                         include_simulated=args.include_simulated,
                         verbose=args.verbose)
    except Exception as exc:
        print(f"[错误] 回填执行异常: {exc}")
        return 1

    print("--- 统计 ---")
    print(f"待回填候选     : {stats['candidates']}")
    print(f"已回填/可回填  : {stats['updated']}")
    print(f"跳过(文件缺失) : {stats['skipped_missing']}")
    print(f"跳过(文件过大) : {stats['skipped_large']}")
    print(f"失败           : {stats['failed']}")
    print(f"实际执行 SQL   : {stats['sql_executed']} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
