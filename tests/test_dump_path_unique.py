# -*- coding: utf-8 -*-
"""产物命名唯一性回归（2026-11-13 对象存储真机暴露）。

时间戳只到秒，对象存储/文件这类轻量任务一次备份几十毫秒，连续两次备份会生成
同名产物并互相覆盖 —— 表现为「按时间点恢复到第 1 次备份」拿到的是第 2 次的数据。
"""
import os

from core.engines.base import BackupEngine, BackupType


class _E(BackupEngine):
    db_type = "file"
    display_name = "测试"

    def __init__(self, root):
        self.task = {"id": 1, "name": "同名任务"}
        self.storage_root = root
        self.logger = None
        self.task_name = "同名任务"
        self.task_id = 1


def test_same_second_backups_get_distinct_paths(tmp_path):
    e = _E(str(tmp_path))
    p1 = e._new_dump_path(BackupType.FULL, ".tar.gz")
    # 模拟第一次备份已落盘
    os.makedirs(os.path.dirname(p1), exist_ok=True)
    with open(p1, "wb") as f:
        f.write(b"first")
    p2 = e._new_dump_path(BackupType.FULL, ".tar.gz")
    assert p1 != p2, "同一秒内的两次全量产物重名，会互相覆盖"
    assert os.path.dirname(p1) == os.path.dirname(p2)


def test_existing_path_kept_when_no_collision(tmp_path):
    e = _E(str(tmp_path))
    p = e._new_dump_path(BackupType.INCREMENTAL, ".tar.gz")
    assert p.endswith(".tar.gz")
    assert "__同名任务__incremental" in p
