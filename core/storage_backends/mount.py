# -*- coding: utf-8 -*-
"""外挂存储 / NFS 挂载点后端。

语义：**已经由操作系统挂载好的目录**（NFS / CIFS / USB 移动硬盘 / 本地磁盘阵列）。
平台不负责挂载动作（离线环境禁止平台内执行 mount 特权操作），只校验目录
「存在、可写、是否为挂载点」，然后按本地文件语义读写。

与 ``local`` 的区别：
- ``local`` 用于平台本机任意导出目录（也可作为源端导出）；
- ``mount`` 明确表示这是一块**外挂介质**，测试连接会额外报告是否真实挂载点
  （``os.path.ismount``），便于运维确认 NFS 是否掉线（掉线时目录仍在但为空）。

诚实边界：NFS 掉线后目录可能仍存在但读写失败，测试连接会以读写探测给出结论。
"""
import os
import time
from pathlib import Path

from .local import LocalStorageBackend


class MountStorageBackend(LocalStorageBackend):
    """外挂 / NFS 挂载点存储后端（复用本地文件实现 + 介质校验）。"""

    display_name = "外挂/NFS 挂载点"
    tier = 2

    def _probe(self) -> tuple[bool, str]:
        """目录存在且可写；同时报告是否真实挂载点（NFS 掉线的关键判据）。"""
        root = self.base_path
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = Path(root) / (".aidbm_mount_probe_%d" % int(time.time()))
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as e:
            return False, "挂载点不可写: %s（%s）" % (root, str(e)[:160])
        is_mount = os.path.ismount(str(root))
        note = ("真实挂载点" if is_mount else
                "普通目录（未检测到挂载点，NFS 掉线时会退化成空目录，建议确认 mount 结果）")
        return True, "可写: %s（%s）" % (root, note)

    def test_connection(self) -> tuple:
        return self._probe()
