# -*- coding: utf-8 -*-
"""
实时备份兼容子包（T01 验收用）。

本包是 core/rt_backup/ 的轻量兼容适配层，对外暴露 T01 验收所需的：
  - LogRepository   日志仓库目录管理（实为 core.rt_backup.repo.LogRepository 的别名，
                    额外兼容 init_repo / get_repo / update_size / check_quota /
                    cleanup_expired 等 T01 语义）
  - RecoveryJournal PIT 恢复点日志读写（转发至 core.rt_backup.pit.RecoveryJournal，
                    含 find_chain 链解析）

为避免两套实现长期分裂引发契约不一致，本包的所有真实逻辑均以 core/rt_backup/
为准，并由 test_rt_t01 / test_rt_journal 保证语义对齐，而非独立实现。

典型用法::

    from core.rt import LogRepository, RecoveryJournal
    repo = LogRepository(task_id=7, repo_root="/data/rt_logs/7")
    repo.init_repo()
    jnl = RecoveryJournal()
    rp = jnl.record({"task_id": 7, "rp_kind": "file-inc", ...})
"""
from .log_repo import LogRepository  # noqa: F401
from .journal import RecoveryJournal  # noqa: F401

__all__ = ["LogRepository", "RecoveryJournal"]
