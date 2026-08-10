# -*- coding: utf-8 -*-
"""
T06 拉取式（pull）CDC 守护抽象层。

与 T03 的流式守护（``mysqlbinlog --stop-never`` / ``pg_receivewal``）不同，
Oracle LogMiner 与达梦 DM_LOGMNR 都是「驱动连接 + 周期查询」的**无子进程**模型：

    tick() ──▶ 取源端当前位点 to_pos
             ──▶ to_pos > last_pos ? 否 → 本轮无产物
             ──▶ _fetch_changes(from_pos, to_pos) 抽变更
             ──▶ 原子写 live/<engine>_<seq>_<from>_<to>.jsonl
             ──▶ 基类 seal_ready_segments()（seal_all_immediately=True）
             ──▶ repo.seal(kind='db-log') → DbRtCapture 入 RecoveryJournal

:class:`core.cdc.base.CDCDaemon` 的 ``is_alive()`` / ``_is_stalled()`` / ``_kill()``
全部围绕 ``self.proc`` 设计，直接继承会让两个守护各写一遍生命周期逻辑。
因此在**基类之下、具体实现之上**插入本层，统一承载：

- ``_running`` 标志与 ``is_alive()`` 重写（共享知识 #19：不看 ``self.proc``）；
- 「取位点 → 抽变更 → 原子写段 → 交给基类封存」的 ``tick()`` 骨架；
- ``.jsonl`` 段命名、元信息头、原子写（共享知识 #2、#20）；
- 位点结构统一走 ``wal_lsn`` / ``wal_end_lsn`` 两列（共享知识 #18 / CH-T06-2），
  并用 ``position_kind`` 区分 ``scn`` / ``dm_lsn``；
- 单轮抽取上限保护（共享知识 #21：``FETCH_LIMIT`` + ``MAX_SEGMENT_BYTES``）；
- LogMiner 长会话周期重连（风险 R14 / 拍板 Q7：默认每 50 轮）。

子类只需实现 5 个抽象钩子：``_import_driver`` / ``_connect`` / ``_probe_source``
/ ``_current_position_value`` / ``_fetch_changes``。

安全（R17）：段文件保留 ``SQL_REDO`` 原文（PITR 需要），但**日志输出严禁打印
SQL_REDO**，只打印行数与位点区间。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import config
import core.db as db

from .base import CDCDaemon

# 段文件名中不允许出现的字符（位点可能含 '/' 等）
_UNSAFE_NAME_RE = re.compile(r"[^0-9A-Za-z]+")

# 默认每多少轮重连一次 LogMiner 会话（拍板 Q7；0 表示不重连）
_DEFAULT_RECONNECT_ROUNDS = 50


def _reconnect_rounds() -> int:
    """读取 ``RT_LOGMNR_RECONNECT_ROUNDS``（环境变量 > config > 默认 50）。"""
    raw = os.environ.get("RT_LOGMNR_RECONNECT_ROUNDS")
    if raw in (None, ""):
        raw = getattr(config, "RT_LOGMNR_RECONNECT_ROUNDS",
                      _DEFAULT_RECONNECT_ROUNDS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_RECONNECT_ROUNDS
    return max(0, value)


class PollingLogMinerDaemon(CDCDaemon):
    """拉取式 CDC 守护抽象基类（Oracle / Dameng 共用）。

    Attributes:
        POSITION_KEY: 位点主键名，复用 ``recovery_journal.wal_lsn`` 列（CH-T06-2）。
        POSITION_KIND: 位点语义 ``scn`` / ``dm_lsn``，供 UI 加前缀区分。
        POSITION_LABEL: UI 展示前缀（``SCN`` / ``LSN``）。
        POSITION_ROW_KEY: 变更行字典中承载位点的键名。
        SEGMENT_EXT: 逻辑段扩展名，统一 ``.jsonl``（共享知识 #20）。
        FETCH_LIMIT: 单轮最多抽取行数（共享知识 #21）。
        MAX_SEGMENT_BYTES: 单段字节上限，超出即在本轮截断，余量留给下一轮。
    """

    # 拉取式：每轮产物一次写完，天然完整，可立即封存
    seal_all_immediately = True

    POSITION_KEY: str = "wal_lsn"
    POSITION_END_KEY: str = "wal_end_lsn"
    POSITION_KIND: str = "scn"
    POSITION_LABEL: str = "POS"
    POSITION_ROW_KEY: str = "scn"
    SEGMENT_EXT: str = ".jsonl"
    FETCH_LIMIT: int = 5000
    MAX_SEGMENT_BYTES: int = 64 * 1024 * 1024

    # 连接默认值（子类覆写）
    DEFAULT_PORT: int = 0
    DEFAULT_USER: str = ""
    # 驱动候选名，仅用于自检面板展示
    DRIVER_NAMES: Tuple[str, ...] = ()

    def __init__(self, task: dict, rt_config, repo, logger=None) -> None:
        super().__init__(task, rt_config, repo, logger=logger)
        self.port = int(self.task.get("port") or self.DEFAULT_PORT or 0)
        self.username = self.task.get("username") or self.DEFAULT_USER

        self._running: bool = False
        self._conn: Any = None
        self._seq: int = 0
        self._rounds: int = 0
        self._last_pos: str = ""
        self._start_pos: str = ""
        self._pending_from: str = ""
        self._pending_to: str = ""
        self._last_fetch_at: str = ""
        self._reconnect_rounds: int = _reconnect_rounds()

        self.position.setdefault(self.POSITION_KEY, "")
        self.position.setdefault(self.POSITION_END_KEY, "")
        self.position.setdefault("position_kind", self.POSITION_KIND)

    # ------------------------------------------------------------------
    # 能力探测（绝不抛异常 —— 共享知识 #17）
    # ------------------------------------------------------------------
    @classmethod
    def check_client(cls) -> Tuple[bool, str]:
        """拉取式守护无外部命令依赖，只校验 Python 驱动是否可导入。"""
        ok, reason = super().check_client()
        if not ok:
            return False, reason
        try:
            module, driver_reason = cls._import_driver()
        except Exception as exc:  # pragma: no cover —— 防御性兜底
            return False, f"{cls.display_name} 驱动探测异常: {exc}"
        if module is None:
            return False, driver_reason or "数据库驱动不可用"
        return True, ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """建立连接并确定起始位点。

        失败时 **raise RuntimeError**（共享知识 #17：仅 ``start()`` 允许抛），
        由 :meth:`core.rt_backup.db_rt.DbRtCapture.start` 既有 try 捕获并就地降级仿真。

        Returns:
            True 表示已就绪，可以开始 tick。
        """
        state = {}
        try:
            state = self.repo.load_state() or {}
        except Exception:
            state = {}
        self.resume_from(state)

        module, reason = self._import_driver()
        if module is None:
            raise RuntimeError(reason or f"{self.display_name} 驱动不可用")

        if config.DEMO_MODE == "on" or self.task.get("demo_only"):
            # 共享知识 #8：演示模式绝不建立真实数据库连接
            raise RuntimeError("演示模式（DEMO_MODE=on）下不建立真实数据库连接")

        self._conn = self._connect()
        if self._conn is None:
            raise RuntimeError(f"连接 {self.display_name} 失败：驱动未返回连接对象")

        ok, reason = self._probe_source(self._conn)
        if not ok:
            self._close()
            raise RuntimeError(reason or f"{self.display_name} 源端预检未通过")

        if not self._last_pos:
            # 拍板 Q2：首启不回溯历史归档，以当前位点为起点
            self._last_pos = str(self._current_position_value(self._conn) or "")
        if not self._last_pos:
            self._close()
            raise RuntimeError(f"无法获取 {self.display_name} 当前位点")

        self._start_pos = self._last_pos
        self._running = True
        self._rounds = 0
        self.started_at = db.now_iso()
        self._last_fetch_at = self.started_at
        self.last_error = ""
        self._sync_position()

        self.logger.info("[rt.cdc] task=%s %s 启动，起始 %s=%s",
                         self.task_id, self.display_name,
                         self.POSITION_LABEL, self._last_pos)
        db.add_log("info", "rt.cdc",
                   f"任务 {self.task_name} 启动 {self.display_name}"
                   f"（起始 {self.POSITION_LABEL}={self._last_pos}）")
        return True

    def stop(self, timeout: float = 10.0) -> None:
        """停止拉取、强制封存残留段并关闭连接。幂等。"""
        self._running = False
        try:
            self.seal_ready_segments(force=True)
        except Exception as exc:
            self.logger.warning("[rt.cdc] task=%s 停止时封存异常: %s",
                                self.task_id, exc)
        self._close()
        try:
            self.repo.save_state(self.state())
        except Exception:
            pass
        self._close_stderr()

    def is_alive(self) -> bool:
        """共享知识 #19：拉取式守护不依赖 ``self.proc``。"""
        return bool(self._running)

    # ------------------------------------------------------------------
    # 周期驱动
    # ------------------------------------------------------------------
    def tick(self) -> dict:
        """一次周期驱动：抽取变更 → 落盘 → 交给基类封存。

        Returns:
            与基类一致的 ``{'alive','segments','position','error','sealed_bytes'}``。
            抽取异常只记 ``last_error``，**绝不外抛**（supervisor 依赖此契约）。
        """
        if not self._running:
            return {"alive": False, "segments": [],
                    "position": self.current_position(),
                    "error": self.last_error, "sealed_bytes": 0}
        try:
            self._poll_once()
        except Exception as exc:
            self.last_error = f"{self.display_name} 抽取失败: {exc}"
            self.logger.error("[rt.cdc] task=%s %s", self.task_id, self.last_error)
        return super().tick()

    def _poll_once(self) -> str:
        """执行一轮抽取。返回本轮写出的段路径（无产物返回空串）。"""
        self._rounds += 1
        if (self._reconnect_rounds > 0
                and self._rounds % self._reconnect_rounds == 0):
            self.logger.info("[rt.cdc] task=%s 已运行 %s 轮，重连 %s 会话以释放资源",
                             self.task_id, self._rounds, self.display_name)
            self._reconnect()

        conn = self._ensure_conn()
        if conn is None:
            return ""

        to_pos = str(self._current_position_value(conn) or "")
        if not to_pos or not self._pos_gt(to_pos, self._last_pos):
            return ""

        rows, actual_to = self._fetch_changes(conn, self._last_pos, to_pos)
        rows = list(rows or [])
        actual_to = str(actual_to or to_pos)

        if not rows:
            # 源端位点前进但无业务变更（系统 schema 已过滤）：只推进位点
            self._last_pos = actual_to
            self._last_fetch_at = db.now_iso()
            self._sync_position()
            return ""

        path, written, effective_to = self._write_segment(
            rows, self._last_pos, actual_to)
        if not path or written <= 0:
            return ""

        self._pending_from = self._last_pos
        self._pending_to = effective_to
        self._last_pos = effective_to
        self._last_fetch_at = db.now_iso()
        self._sync_position()
        # R17：只打印行数与位点区间，绝不打印 SQL_REDO 内容
        self.logger.info("[rt.cdc] task=%s %s 抽取 %s 行，区间 %s(%s→%s)",
                         self.task_id, self.display_name, written,
                         self.POSITION_LABEL, self._pending_from, effective_to)
        return path

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def _ensure_conn(self):
        """返回可用连接；断开时尝试重连一次，失败记 ``last_error`` 返回 None。"""
        if self._conn is not None:
            return self._conn
        try:
            self._conn = self._connect()
        except Exception as exc:
            self.last_error = f"重连 {self.display_name} 失败: {exc}"
            self.logger.warning("[rt.cdc] task=%s %s", self.task_id, self.last_error)
            self._conn = None
        return self._conn

    def _reconnect(self) -> None:
        """关闭并重建连接（周期性释放 LogMiner 会话资源，风险 R14）。"""
        self._close()
        self._ensure_conn()

    def _close(self) -> None:
        """关闭连接。幂等，绝不抛异常。"""
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            conn.close()
        except Exception as exc:
            self.logger.debug("[rt.cdc] task=%s 关闭连接异常: %s", self.task_id, exc)

    def _query(self, conn, sql: str, params: Optional[tuple] = None) -> List[tuple]:
        """执行查询并返回全部行。异常向上抛，由调用方决定降级策略。"""
        cursor = conn.cursor()
        try:
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            rows = cursor.fetchall()
            return list(rows or [])
        finally:
            try:
                cursor.close()
            except Exception:
                pass

    def _query_one(self, conn, sql: str, params: Optional[tuple] = None):
        """执行查询并返回首行；无结果返回 None。"""
        rows = self._query(conn, sql, params)
        return rows[0] if rows else None

    def _execute(self, conn, sql: str, params: Optional[tuple] = None) -> None:
        """执行无结果集语句（PL/SQL 块等）。"""
        cursor = conn.cursor()
        try:
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
        finally:
            try:
                cursor.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 位点
    # ------------------------------------------------------------------
    def resume_from(self, state: dict) -> None:
        """从续传状态恢复位点。

        取值优先级：``wal_end_lsn``（上次已抽到的终点）> ``POSITION_KEY``
        > ``POSITION_LABEL.lower()``（``scn`` / ``lsn`` 语义别名）> ``POSITION_KIND``。

        Note:
            设计文档给出的顺序以 ``POSITION_KEY``（段起点）优先，这里改为
            ``wal_end_lsn`` 优先——段起点会让重启后重复抽取一整段，
            用终点续传语义更准确，且两者都在同一批键中兜底，无兼容性影响。
        """
        super().resume_from(state)
        position = self.position or {}
        candidates = (self.POSITION_END_KEY, self.POSITION_KEY,
                      str(self.POSITION_LABEL).lower(), self.POSITION_KIND)
        for key in candidates:
            value = position.get(key)
            if value not in (None, ""):
                self._last_pos = str(value)
                break
        if self._last_pos:
            self.logger.info("[rt.cdc] task=%s 续传 %s=%s",
                             self.task_id, self.POSITION_LABEL, self._last_pos)

    def _sync_position(self) -> None:
        """把内部位点同步进 ``self.position``（供基类 state/持久化使用）。"""
        self.position[self.POSITION_KEY] = self._start_pos or ""
        self.position[self.POSITION_END_KEY] = self._last_pos or ""
        self.position[self.POSITION_KIND] = self._last_pos or ""
        self.position["position_kind"] = self.POSITION_KIND

    def current_position(self) -> dict:
        """守护已捕获到的位点。

        Returns:
            ``{'wal_lsn': 段起点, 'wal_end_lsn': 段终点,
            '<scn|dm_lsn>': 段终点, 'position_kind': 语义}``（CH-T06-2 列复用）。
        """
        position = dict(self.position)
        position[self.POSITION_KEY] = self._start_pos or ""
        position[self.POSITION_END_KEY] = self._last_pos or ""
        position[self.POSITION_KIND] = self._last_pos or ""
        position["position_kind"] = self.POSITION_KIND
        return position

    def source_position(self) -> dict:
        """实时查询源端当前位点，用于 lag 估算。失败返回空字典。"""
        if config.DEMO_MODE == "on" or self.task.get("demo_only"):
            return {}
        if not self._running:
            return {}
        conn = self._conn
        if conn is None:
            return {}
        try:
            value = str(self._current_position_value(conn) or "")
        except Exception as exc:
            self.logger.debug("[rt.cdc] task=%s 查询源端位点失败: %s",
                              self.task_id, exc)
            return {}
        if not value:
            return {}
        return {self.POSITION_END_KEY: value, self.POSITION_KIND: value,
                "position_kind": self.POSITION_KIND}

    def lag_seconds(self) -> int:
        """捕获落后秒数。

        MVP 用「最后一次成功抽取时刻 → 现在」的墙钟差：SCN / DM_LSN 是逻辑时钟，
        与物理时间没有固定换算关系（``SCN_TO_TIMESTAMP`` 对过旧 SCN 会抛 ORA-08181）。
        """
        anchor = self._last_fetch_at or self.started_at
        if not anchor:
            return 0
        try:
            from datetime import datetime
            return int(max(0.0, time.time()
                           - datetime.fromisoformat(str(anchor)).timestamp()))
        except (TypeError, ValueError):
            return 0

    def state(self) -> dict:
        """供 ``LogRepository.save_state`` 持久化的守护状态。"""
        payload = super().state()
        payload["position"] = self.current_position()
        payload["position_kind"] = self.POSITION_KIND
        payload["last_position"] = self._last_pos or ""
        payload["rounds"] = self._rounds
        return payload

    def _position_for_segment(self, info: dict) -> dict:
        """本轮段的起止位点（写段时已记录）。"""
        return {
            self.POSITION_KEY: self._pending_from or "",
            self.POSITION_END_KEY: self._pending_to or "",
            self.POSITION_KIND: self._pending_to or "",
            "position_kind": self.POSITION_KIND,
        }

    @staticmethod
    def _pos_value(pos) -> Optional[int]:
        """位点字符串 → int；非纯数字返回 None。"""
        try:
            return int(str(pos).strip())
        except (TypeError, ValueError):
            return None

    @classmethod
    def _pos_gt(cls, left, right) -> bool:
        """``left > right``：两者皆为整数时按整数比，否则按字符串比。"""
        if right in (None, ""):
            return bool(left)
        int_left, int_right = cls._pos_value(left), cls._pos_value(right)
        if int_left is not None and int_right is not None:
            return int_left > int_right
        return str(left) > str(right)

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------
    def _segment_name(self, pos_from: str, pos_to: str) -> str:
        """段文件名：``<engine_key>_<seq>_<from>_<to>.jsonl``。"""
        safe_from = _UNSAFE_NAME_RE.sub("_", str(pos_from or "0"))
        safe_to = _UNSAFE_NAME_RE.sub("_", str(pos_to or "0"))
        return (f"{self.engine_key}_{self._seq:06d}_"
                f"{safe_from}_{safe_to}{self.SEGMENT_EXT}")

    def _write_segment(self, rows: List[dict], pos_from: str,
                       pos_to: str) -> Tuple[str, int, str]:
        """把变更行原子写成一个 ``.jsonl`` 段。

        首行为 ``{"_meta": true, ...}`` 元信息（共享知识 #20），其后每行一条变更。
        超过 ``MAX_SEGMENT_BYTES`` 时本轮截断，余量留给下一轮（共享知识 #21）。

        Args:
            rows: 变更行列表，每行须含 ``POSITION_ROW_KEY`` 位点字段。
            pos_from: 本段起始位点（不含）。
            pos_to: 本段结束位点（含）。

        Returns:
            ``(段绝对路径, 实际写入行数, 实际结束位点)``；无有效行时路径为空串。
        """
        if not rows:
            return "", 0, str(pos_to)

        kind = self.POSITION_KIND
        body_lines: List[bytes] = []
        total = 0
        effective_to = str(pos_to)
        truncated = False
        for row in rows:
            try:
                line = json.dumps(row, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError):
                line = json.dumps({"_unserializable": str(row)[:512]},
                                  ensure_ascii=False).encode("utf-8")
            if body_lines and total + len(line) > self.MAX_SEGMENT_BYTES:
                truncated = True
                break
            body_lines.append(line)
            total += len(line) + 1
            row_pos = row.get(self.POSITION_ROW_KEY)
            if row_pos not in (None, ""):
                effective_to = str(row_pos)
        if not truncated:
            effective_to = str(pos_to)
        if not body_lines:
            return "", 0, str(pos_from)

        self._seq += 1
        meta = {
            "_meta": True,
            "engine": self.engine_key,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "position_kind": kind,
            f"from_{kind}": str(pos_from),
            f"to_{kind}": effective_to,
            "rows": len(body_lines),
            "truncated": truncated,
            "created_at": db.now_iso(),
        }
        header = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        payload = b"\n".join([header] + body_lines) + b"\n"

        name = self._segment_name(pos_from, effective_to)
        path = os.path.join(self.repo.live_dir(), name)

        def _write(tmp_path: str) -> None:
            with open(tmp_path, "wb") as handle:
                handle.write(payload)

        # 共享知识 #2：先写 .tmp 再 os.replace
        self.repo.atomic_write(_write, path, suffix=self.SEGMENT_EXT)
        if truncated:
            self.logger.warning(
                "[rt.cdc] task=%s 单段超过 %s 字节上限，本轮截断至 %s=%s，余量下轮继续",
                self.task_id, self.MAX_SEGMENT_BYTES, self.POSITION_LABEL,
                effective_to)
        return path, len(body_lines), effective_to

    # ------------------------------------------------------------------
    # 抽象钩子（子类必须实现）
    # ------------------------------------------------------------------
    @classmethod
    def _import_driver(cls) -> Tuple[Any, str]:
        """惰性导入数据库驱动。

        Returns:
            ``(module | None, 中文原因)``。**绝不抛异常**（共享知识 #17）。
        """
        raise NotImplementedError

    def _connect(self):
        """建立数据库连接。失败抛异常，由 ``start()`` 转成 RuntimeError。"""
        raise NotImplementedError

    def _probe_source(self, conn) -> Tuple[bool, str]:
        """源端预检（归档模式 / 权限 / 系统包）。

        Returns:
            ``(ok, 中文原因)``。
        """
        raise NotImplementedError

    def _current_position_value(self, conn) -> str:
        """查询源端当前位点（Oracle: CURRENT_SCN；达梦: V$RLOG.CUR_LSN）。"""
        raise NotImplementedError

    def _fetch_changes(self, conn, from_pos: str,
                       to_pos: str) -> Tuple[List[dict], str]:
        """抽取 ``(from_pos, to_pos]`` 区间的变更。

        Returns:
            ``(变更行列表, 实际结束位点)``。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    def describe(self) -> dict:
        """守护自述，补充位点语义供 UI 展示前缀。"""
        info = super().describe()
        info["position_kind"] = self.POSITION_KIND
        info["position_label"] = self.POSITION_LABEL
        info["driver_names"] = list(self.DRIVER_NAMES)
        return info
