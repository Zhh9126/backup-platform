# -*- coding: utf-8 -*-
"""
同步插件基类与注册表。

设计参考 DataX：
- SourceReader 负责从数据源按批次读取记录；
- SinkWriter 负责将记录写入目标端；
- BasePlugin 同时提供 Reader 与 Writer 能力，并统一数据库类型映射。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class SyncConfig:
    """单次同步配置。"""
    task_id: int = 0
    task_name: str = ""
    source_type: str = "manual"          # managed | manual
    source_task_id: Optional[int] = None

    # 源端连接
    src_db_type: str = ""
    src_host: str = ""
    src_port: int = 0
    src_username: str = ""
    src_password: str = ""
    src_db_name: str = ""                # database/schema
    src_schema: str = ""
    source_table: str = ""
    source_tables_list: List[str] = field(default_factory=list)
    source_where: str = ""

    # 目标端连接
    tgt_db_type: str = ""
    tgt_host: str = ""
    tgt_port: int = 0
    tgt_username: str = ""
    tgt_password: str = ""
    tgt_db_name: str = ""
    tgt_schema: str = ""
    target_table: str = ""

    # 同步策略
    sync_mode: str = "full"              # full | incremental | realtime
    save_mode: str = "append"            # append | overwrite | upsert | create_if_not_exists
    column_mapping: List[Dict[str, Any]] = field(default_factory=list)
    field_ide: str = "origin"            # origin | upper | lower | camel | underscore
    incremental_column: str = ""
    incremental_value: str = ""
    batch_size: int = 1000
    error_threshold: int = 0

    # 实时同步（Flink CDC 预留）
    realtime_enabled: bool = False
    flink_config: Dict[str, Any] = field(default_factory=dict)

    # 全库迁移 / 校验（pg2mysql 风格）
    full_db_migrate: bool = False         # 全库迁移模式（一次同步所有表）
    validate_before_run: bool = False     # 执行前做 Schema 兼容性校验
    verify_after_run: bool = False        # 执行后做迁移数据校验


@dataclass
class ColumnMeta:
    """列元数据。"""
    name: str
    type: str = "STRING"
    nullable: bool = True
    default: Any = None
    max_length: Optional[int] = None
    numeric_precision: Optional[int] = None
    numeric_scale: Optional[int] = None


@dataclass
class ReadResult:
    """Reader 返回的一批记录。"""
    records: List[List[Any]] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    has_more: bool = False
    next_value: Optional[Any] = None  # 增量下次起点


class SourceReader(ABC):
    """源端读取器抽象。"""

    def __init__(self, config: SyncConfig, plugin):
        self.config = config
        self.plugin = plugin

    @abstractmethod
    def connect(self) -> Any:
        """建立连接，返回 connection 对象。"""
        pass

    @abstractmethod
    def list_tables(self) -> List[str]:
        """列出数据库下所有表。"""
        pass

    @abstractmethod
    def list_columns(self, table: str) -> List[ColumnMeta]:
        """返回表列元数据。"""
        pass

    @abstractmethod
    def read_batch(self, cursor: Any) -> ReadResult:
        """读取下一批数据。"""
        pass

    def close(self, cursor: Any = None, conn: Any = None):
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if conn:
                conn.close()
        except Exception:
            pass


class SinkWriter(ABC):
    """目标端写入器抽象。"""

    def __init__(self, config: SyncConfig, plugin):
        self.config = config
        self.plugin = plugin

    @abstractmethod
    def connect(self) -> Any:
        pass

    @abstractmethod
    def prepare_table(self, conn: Any, columns: List[ColumnMeta]) -> None:
        """按 save_mode 准备目标表。"""
        pass

    @abstractmethod
    def write_batch(self, conn: Any, records: List[List[Any]],
                    columns: List[str]) -> int:
        """写入一批记录，返回成功写入行数。"""
        pass

    def close(self, cursor: Any = None, conn: Any = None):
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if conn:
                conn.close()
        except Exception:
            pass


class BasePlugin(ABC):
    """同步插件基类。每个插件为指定数据库类型提供 Reader/Writer。"""

    db_type: str = ""
    default_ports: Dict[str, int] = {}

    @abstractmethod
    def create_reader(self, config: SyncConfig) -> SourceReader:
        pass

    @abstractmethod
    def create_writer(self, config: SyncConfig) -> SinkWriter:
        pass

    @abstractmethod
    def type_to_java(self, db_type: str, value: Any) -> Any:
        """把数据库原生值转为平台统一 Java 类型（int/float/str/bytes/None）。"""
        pass

    @abstractmethod
    def java_to_db(self, java_value: Any, target_type: str) -> Any:
        """把 Java 类型值转为目标数据库可接受的值。"""
        pass

    @abstractmethod
    def quote_identifier(self, name: str) -> str:
        pass

    def normalize_identifier(self, name: str, ide: str = "origin") -> str:
        if not name:
            return name
        if ide == "origin":
            return name
        if ide == "upper":
            return name.upper()
        if ide == "lower":
            return name.lower()
        if ide == "camel":
            parts = name.split("_")
            return parts[0] + "".join(p.capitalize() for p in parts[1:])
        if ide == "underscore":
            import re
            s = re.sub(r"([a-z])([A-Z])", r"\1_\2", name)
            return s.lower()
        return name


class PluginRegistry:
    def __init__(self):
        self._plugins: Dict[str, type] = {}

    def register(self, db_type: str, plugin_cls: type):
        self._plugins[db_type.lower()] = plugin_cls

    def get(self, db_type: str) -> Optional[type]:
        return self._plugins.get(db_type.lower())

    def get_plugin(self, db_type: str) -> Optional["BasePlugin"]:
        """返回插件实例（用于调用 disable_constraints 等实例方法）。"""
        cls = self.get(db_type)
        if not cls:
            raise ValueError(f"不支持的同步数据库类型: {db_type}")
        return cls()

    def create_reader(self, db_type: str, config: SyncConfig) -> SourceReader:
        cls = self.get(db_type)
        if not cls:
            raise ValueError(f"不支持的同步数据库类型: {db_type}")
        plugin = cls()
        return plugin.create_reader(config)

    def create_writer(self, db_type: str, config: SyncConfig) -> SinkWriter:
        cls = self.get(db_type)
        if not cls:
            raise ValueError(f"不支持的同步数据库类型: {db_type}")
        plugin = cls()
        return plugin.create_writer(config)
