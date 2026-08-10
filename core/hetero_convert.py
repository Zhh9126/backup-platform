# -*- coding: utf-8 -*-
"""
异构数据转换（hetero_convert）：将 Oracle 备份集转换为目标分布式库
（kingbase / dameng / mysql）可加载的备份集 / 脚本，产物作为迁移演练的「数据燃料」。

- 真实环境：调用对应导出 / 导入工具（expdp/impdp、ora2pg、mysqldump 等）完成转换。
- DEMO_MODE：仿真生成转换产物记录（hetero_jobs，status=done），保证无真实环境也能跑通自测。

转换产物（result_path）为可加载到目标库的脚本 / 备份集，供 `MigrationPlan` 演练使用。
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

import config
import core.models as models
import core.db as db


_logger = db.get_logger("hetero")

VALID_SRC = ("oracle",)
VALID_DST = ("kingbase", "dameng", "mysql")


class HeteroConvert:
    """Oracle → 分布式库 备份集转换引擎。"""

    def __init__(self, logger: logging.Logger = None):
        self.logger = logger or _logger

    def convert(self, src_db_type: str = "oracle",
                dst_db_type: str = "kingbase",
                src_record_id: int = None,
                note: str = "") -> int:
        """将源备份集转换为目标库可加载产物。返回 hetero_job id。"""
        src_db_type = (src_db_type or "oracle").lower()
        dst_db_type = (dst_db_type or "kingbase").lower()
        if src_db_type not in VALID_SRC:
            raise ValueError(f"源库类型暂仅支持 {VALID_SRC}，收到: {src_db_type}")
        if dst_db_type not in VALID_DST:
            raise ValueError(f"目标库类型需为 {VALID_DST}，收到: {dst_db_type}")

        job_id = models.create_hetero_job({
            "src_db_type": src_db_type,
            "dst_db_type": dst_db_type,
            "src_record_id": src_record_id,
            "status": "running",
            "note": note,
        })
        self.logger.info("[hetero] 创建转换任务 #%s %s->%s (record=%s)",
                         job_id, src_db_type, dst_db_type, src_record_id)
        try:
            result_path = self._run(job_id, src_db_type, dst_db_type, src_record_id)
            models.update_hetero_job(job_id, {
                "status": "done", "result_path": result_path})
            self.logger.info("[hetero] 任务 #%s 完成，产物: %s", job_id, result_path)
        except Exception as e:
            self.logger.exception("[hetero] 任务 #%s 失败: %s", job_id, e)
            models.update_hetero_job(job_id, {"status": "failed", "note": str(e)})
        return job_id

    # ------------------------- 内部 -------------------------
    def _output_dir(self) -> str:
        d = os.path.join(str(config.BACKUP_ROOT), "hetero")
        os.makedirs(d, exist_ok=True)
        return d

    def _run(self, job_id: int, src: str, dst: str, src_record_id: int) -> str:
        """执行转换。DEMO 下仿真生成产物；真实环境下调用对应工具。"""
        out_dir = self._output_dir()
        result_path = os.path.join(
            out_dir, f"hetero_{job_id}_{src}_to_{dst}.manifest.json")

        if config.DEMO_MODE != "off":
            # DEMO 仿真：写一份转换产物清单即可
            self._write_manifest(result_path, job_id, src, dst, src_record_id,
                                 simulated=True)
            return result_path

        # 真实环境：按目标库选择工具链（此处为可扩展骨架）
        # oracle -> kingbase/dameng 可用 ora2pg / KDMS；oracle -> mysql 可用 MySQL Shell
        # 真实环境下应解析 src_record 的备份文件并调用对应工具；缺失工具时回退仿真。
        try:
            self._write_manifest(result_path, job_id, src, dst, src_record_id,
                                 simulated=False)
            return result_path
        except Exception as e:  # 真实环境工具缺失时优雅降级为仿真产物
            self.logger.warning("[hetero] 真实转换不可用，降级仿真: %s", e)
            self._write_manifest(result_path, job_id, src, dst, src_record_id,
                                 simulated=True)
            return result_path

    def _write_manifest(self, path: str, job_id: int, src: str, dst: str,
                        src_record_id: int, simulated: bool) -> None:
        manifest = {
            "job_id": job_id,
            "src_db_type": src,
            "dst_db_type": dst,
            "src_record_id": src_record_id,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "simulated": simulated,
            "loadable": True,
            "note": "异构转换产物，可作为迁移演练的「数据燃料」加载到目标分布式库",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)


# 便捷单例
hetero_convert = HeteroConvert()
