# -*- coding: utf-8 -*-
"""
KingbaseES（人大金仓）WAL 流式捕获守护。

KingbaseES V8 起内核基于 PostgreSQL，**流复制协议与 WAL 段格式完全兼容**，
因此本实现不重复造轮子，直接继承 :class:`core.cdc.pg_wal.PostgresWALDaemon`，
只通过 T06 钩子化重构（CH-T06-1）暴露出来的「6 类属性 + 6 钩子」覆盖引擎差异：

============  ==========================  ==================================
差异点         PostgreSQL                  KingbaseES
============  ==========================  ==================================
默认端口       5432                        54321
默认用户       postgres                    system
默认库名       postgres                    test
接收客户端     pg_receivewal               sys_receivewal / kb_receivewal /
                                          ksy_receivewal / pg_receivewal
SQL 客户端     psql                        ksql / sys_psql / psql
密码环境变量   PGPASSWORD                  KINGBASE_PASSWORD + PGPASSWORD
当前 LSN 函数  pg_current_wal_lsn()        sys_current_wal_lsn() 优先，
                                          回退 pg_current_wal_lsn()
Python 驱动    psycopg2                    ksycopg2 优先，回退 psycopg2
============  ==========================  ==================================

**降级策略**（共享知识 #18：降级永不抛异常）：
- 接收客户端全部缺失 → :meth:`check_client` 返回 ``(False, 原因)``，
  由 :mod:`core.cdc` 工厂降级为 :class:`~core.cdc.simulated.SimulatedCDCDaemon`；
- Python 驱动缺失 → 仅影响精确位点探测，回落命令行客户端；两者都缺失时
  位点显示为空，守护本身照常运行。

封存判据、位点推导、复制槽逻辑、``.partial`` 保护全部沿用 PG 实现，不做改动。
"""
from __future__ import annotations

from typing import Tuple

from .pg_wal import PostgresWALDaemon

#: 惰性导入缓存：(module | None, 原因)。None 表示尚未探测。
_KB_DRIVER = None
_KB_REASON = ""
_KB_PROBED = False


def _import_kingbase_driver():
    """惰性导入 Kingbase Python 驱动（可选依赖，仅用于位点查询增强）。

    探测顺序：``ksycopg2``（金仓官方，随客户端安装包提供，非 PyPI）
    → ``psycopg2``（协议兼容，可 pip 安装）。

    Returns:
        ``(module | None, 原因)``。缺失时给出可操作的安装指引。
    """
    global _KB_DRIVER, _KB_REASON, _KB_PROBED
    if _KB_PROBED:
        return _KB_DRIVER, _KB_REASON

    _KB_PROBED = True
    try:
        import ksycopg2  # type: ignore
        _KB_DRIVER, _KB_REASON = ksycopg2, ""
        return _KB_DRIVER, _KB_REASON
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover - 驱动内部异常
        _KB_DRIVER, _KB_REASON = None, f"ksycopg2 导入失败: {exc}"
        return _KB_DRIVER, _KB_REASON

    try:
        import psycopg2  # type: ignore
        _KB_DRIVER, _KB_REASON = psycopg2, ""
        return _KB_DRIVER, _KB_REASON
    except ImportError as exc:
        _KB_DRIVER = None
        _KB_REASON = (
            f"未安装 ksycopg2 / psycopg2 驱动（{exc}）。"
            "ksycopg2 随 KingbaseES 客户端安装包提供；"
            "或执行 pip install psycopg2-binary 使用协议兼容驱动。")
        return _KB_DRIVER, _KB_REASON
    except Exception as exc:  # pragma: no cover
        _KB_DRIVER, _KB_REASON = None, f"psycopg2 导入失败: {exc}"
        return _KB_DRIVER, _KB_REASON


def reset_driver_cache() -> None:
    """清空驱动探测缓存（单元测试用）。"""
    global _KB_DRIVER, _KB_REASON, _KB_PROBED
    _KB_DRIVER, _KB_REASON, _KB_PROBED = None, "", False


def probe_kingbase_driver() -> dict:
    """自检面板用：Kingbase 驱动可用性。"""
    module, reason = _import_kingbase_driver()
    name = getattr(module, "__name__", "") if module is not None else ""
    return {
        "installed": module is not None,
        "driver": name,
        "reason": reason,
        "hint": "" if module else (
            "ksycopg2 随 KingbaseES 客户端安装包提供（非 PyPI）；"
            "亦可 pip install psycopg2-binary 使用协议兼容驱动"),
    }


class KingbaseWALDaemon(PostgresWALDaemon):
    """KingbaseES 的 WAL 持续接收守护（复用 PG 流复制实现）。"""

    engine_key = "kingbase_wal"
    display_name = "KingbaseES WAL 流式接收"
    # 客户端名在不同金仓版本间不统一，改由 _resolve_client() 按候选列表探测，
    # 这里留空避免基类 required_clients 扫描误判。
    required_clients: list = []
    is_simulated = False
    seal_all_immediately = False

    # ------------------------------------------------------------------
    # 引擎差异点（CH-T06-1 六类属性）
    # ------------------------------------------------------------------
    DEFAULT_PORT: int = 54321
    DEFAULT_USER: str = "system"
    DEFAULT_DB: str = "test"
    #: 金仓 V8 默认把 pg_* 工具改名为 sys_*，部分发行版保留 kb_/ksy_ 前缀
    RECEIVE_CLIENT_CANDIDATES: Tuple[str, ...] = (
        "sys_receivewal", "kb_receivewal", "ksy_receivewal", "pg_receivewal",
    )
    QUERY_CLIENT_CANDIDATES: Tuple[str, ...] = ("ksql", "sys_psql", "psql")
    #: 金仓客户端读 KINGBASE_PASSWORD；兼容层同时认 PGPASSWORD，两个都注入最稳
    PASSWORD_ENV: Tuple[str, ...] = ("KINGBASE_PASSWORD", "PGPASSWORD")
    POSITION_KIND: str = "lsn"
    POSITION_LABEL: str = "LSN"
    SLOT_PREFIX: str = "rt_kb_slot_"
    CURRENT_LSN_SQL: Tuple[str, ...] = (
        "SELECT sys_current_wal_lsn()",
        "SELECT pg_current_wal_lsn()",
    )

    # ------------------------------------------------------------------
    # 钩子 3：驱动导入
    # ------------------------------------------------------------------
    @classmethod
    def _import_driver(cls):
        """ksycopg2 优先，回退 psycopg2。"""
        return _import_kingbase_driver()

    # ------------------------------------------------------------------
    # 能力探测
    # ------------------------------------------------------------------
    @classmethod
    def check_client(cls) -> Tuple[bool, str]:
        """定位金仓流式接收客户端。

        Returns:
            ``(可用, 原因)``。全部候选缺失时给出带安装指引的原因文案。
        """
        name, _reason = cls._resolve_client()
        if name:
            return True, ""
        return False, (
            "缺少 KingbaseES 流复制客户端（已尝试 "
            f"{' / '.join(cls.RECEIVE_CLIENT_CANDIDATES)}）。"
            "请安装 KingbaseES 客户端工具包并把 bin 目录加入 PATH")

    def probe_psycopg2(self) -> dict:
        """自检面板用：沿用统一的驱动探测结果。"""
        return probe_kingbase_driver()

    def describe(self) -> dict:
        """守护自述，补充驱动候选信息（自检面板展示用）。"""
        info = super().describe()
        info["driver_names"] = ["ksycopg2", "psycopg2"]
        info["client_candidates"] = list(self.RECEIVE_CLIENT_CANDIDATES)
        return info
