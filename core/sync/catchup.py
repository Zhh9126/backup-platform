# -*- coding: utf-8 -*-
"""一次性「全量 + 增量追平」内核（对标 DTS 不停机迁移）。

与常驻 realtime 的区别：本模块跑完一批追平即返回，不启动守护线程；
可被同步任务（sync_tasks）和迁移计划（负 id 的 synthetic task）共同复用，
后者通过 ``on_watermark`` 回调自行决定位点存放位置。

三条不可退让的语义：
1. 交接水位必须在全量快照【开始前】取源端 MAX——快照期间新写入的行
   若大于快照后取的 MAX，会被水位越过而永久漏掉；
2. 水位只按【已成功写入目标端】的记录推进，绝不取读取期间的全局 MAX
   （历史 P0 缺陷：静默丢数）；
3. 追平阶段强制 upsert：重复投递由目标端幂等吸收（至少一次投递语义）。
"""
import time
from typing import Any, Callable, Dict, Optional

from .plugins import registry


def _newer(value, current) -> bool:
    """增量列值比较：数值按数值比，其余按字符串（时间戳字典序等价）。"""
    if current is None:
        return value is not None
    if value is None:
        return False
    try:
        return float(value) > float(current)
    except (TypeError, ValueError):
        return str(value) > str(current)


def snapshot_watermark(cfg: Any, table: str, schema: str = None) -> str:
    """全量快照开始前取源端 MAX(incremental_column) 作为交接位点。

    失败返回空串（退化为从头追平），并由调用方在阶段报告里标注——
    绝不伪造位点。
    """
    col = getattr(cfg, "incremental_column", "")
    if not col or not table:
        return ""
    ref = "%s.%s" % (schema, table) if schema else table
    reader = registry.create_reader(cfg.src_db_type, cfg)
    conn = None
    try:
        conn = reader.connect()
        cur = conn.cursor()
        cur.execute("SELECT MAX(%s) FROM %s" % (col, ref))
        row = cur.fetchone()
        v = row[0] if row else None
        return str(v) if v is not None else ""
    except Exception:
        return ""
    finally:
        reader.close(conn=conn)


def run_catchup(cfg: Any, reader: Any, writer: Any,
                src_conn: Any, tgt_conn: Any,
                start_watermark: str, table: str,
                target_table: str = None,
                max_rounds: int = 200,
                max_seconds: int = 900,
                on_watermark: Optional[Callable[[str, str], None]] = None,
                progress_callback: Optional[Callable[[dict], None]] = None) -> Dict[str, Any]:
    """从交接水位反复读增量批次写入目标，直到追平或触达上限。

    返回 dict 含 success/caught_up/rounds/rows_read/rows_written/watermark/
    warnings/duration_sec，可直接嵌入阶段报告。
    """
    col = getattr(cfg, "incremental_column", "")
    if not col:
        return {"success": False, "caught_up": False, "rounds": 0,
                "rows_read": 0, "rows_written": 0, "watermark": "",
                "warnings": ["增量追平需要配置增量列（时间戳或自增列）"],
                "duration_sec": 0.0,
                "message": "增量追平失败：未配置 incremental_column"}
    if not table:
        return {"success": False, "caught_up": False, "rounds": 0,
                "rows_read": 0, "rows_written": 0, "watermark": "",
                "warnings": ["增量追平缺少源表名"], "duration_sec": 0.0,
                "message": "增量追平失败：未指定源表"}

    # read_batch 的分页策略由 cfg 驱动：incremental + 非空水位才走
    # `WHERE col > ? ORDER BY col LIMIT n`，否则会走 OFFSET 全表扫描。
    # 这里临时切换配置，结束后在 finally 中原样恢复（绝不污染调用方）。
    old = {
        "sync_mode": cfg.sync_mode,
        "incremental_value": cfg.incremental_value,
        "save_mode": cfg.save_mode,
        "source_table": cfg.source_table,
        "target_table": cfg.target_table,
    }
    cfg.sync_mode = "incremental"
    cfg.incremental_value = str(start_watermark or "")
    cfg.source_table = table
    cfg.target_table = target_table or cfg.target_table or table
    cfg.save_mode = "upsert"

    watermark = cfg.incremental_value
    warnings: list = []
    rounds = total_read = total_write = 0
    caught_up = False
    started = time.monotonic()

    try:
        cur = src_conn.cursor()
        while rounds < max_rounds and (time.monotonic() - started) < max_seconds:
            result = reader.read_batch(cur)
            rows = getattr(result, "records", None) or []
            if not rows:
                caught_up = True
                break
            columns = list(getattr(result, "columns", None) or [])
            if col not in columns:
                return {
                    "success": False, "caught_up": False, "rounds": rounds,
                    "rows_read": total_read, "rows_written": total_write,
                    "watermark": watermark,
                    "warnings": warnings + ["增量列 %s 不在读取结果中" % col],
                    "duration_sec": round(time.monotonic() - started, 2),
                    "message": "增量追平失败：源表缺少增量列 %s" % col,
                }
            col_idx = columns.index(col)

            # 写入前先按当前水位检测推进：避免分页未推进时无限重复同一批
            if not _newer(rows[-1][col_idx], watermark):
                warnings.append("增量位点未推进，已中止以避免重复写入")
                break

            try:
                written = writer.write_batch(tgt_conn, rows, columns)
                tgt_conn.commit()
            except Exception as e:
                try:
                    tgt_conn.rollback()
                except Exception:
                    pass
                return {
                    "success": False, "caught_up": False, "rounds": rounds,
                    "rows_read": total_read, "rows_written": total_write,
                    "watermark": watermark,
                    "warnings": warnings + ["追平写入失败: %s" % str(e)[:200]],
                    "duration_sec": round(time.monotonic() - started, 2),
                    "message": "增量追平失败：%s" % str(e)[:240],
                }
            total_read += len(rows)
            total_write += written
            if written != len(rows):
                # 部分写入无法确定【哪几行】成功：绝不按返回计数推测水位
                # （会越过未写行造成静默丢数）。保持原水位下轮整批重投，
                # 由 upsert 幂等吸收重复——宁可可控重复，不可静默遗漏。
                warnings.append(
                    "第 %d 批写入 %d/%d 行，水位不推进（下轮整批重投）"
                    % (rounds + 1, written, len(rows)))
            else:
                # 水位只取本批记录的最大值（不是读取期间的源端全局 MAX）
                batch_max = None
                for rec in rows:
                    if _newer(rec[col_idx], batch_max):
                        batch_max = rec[col_idx]
                if batch_max is None:
                    warnings.append("第 %d 批无有效增量值，水位未推进" % (rounds + 1))
                else:
                    watermark = str(batch_max)
                    cfg.incremental_value = watermark
                    if on_watermark:
                        try:
                            on_watermark(col, watermark)
                        except Exception as e:
                            warnings.append("位点持久化失败: %s" % str(e)[:160])

            rounds += 1
            if progress_callback:
                progress_callback({"stage": "catchup", "table": table,
                                   "round": rounds, "rows": total_write,
                                   "watermark": watermark})
            if not getattr(result, "has_more", False):
                caught_up = True
                break

        if not caught_up and rounds >= max_rounds:
            warnings.append("达到追平轮次上限 %d，未确认完全追平" % max_rounds)
        if not caught_up and (time.monotonic() - started) >= max_seconds:
            warnings.append("达到追平时间上限 %ss，未确认完全追平" % max_seconds)
    finally:
        for k, v in old.items():
            setattr(cfg, k, v)

    ok = bool(caught_up and not warnings) or bool(
        caught_up and all("写入" not in w for w in warnings))
    msg = ("增量追平完成：%d 轮，追平 %d 行，水位 %s"
           % (rounds, total_write, watermark))
    if warnings:
        msg += "；提示: " + " | ".join(warnings[:5])
    return {
        "success": ok,
        "caught_up": bool(caught_up),
        "rounds": rounds,
        "rows_read": total_read,
        "rows_written": total_write,
        "watermark": watermark,
        "warnings": warnings,
        "duration_sec": round(time.monotonic() - started, 2),
        "message": msg,
    }
