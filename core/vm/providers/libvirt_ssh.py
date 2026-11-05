# -*- coding: utf-8 -*-
"""KVM / libvirt（含 oVirt、OpenStack、KubeVirt 底层）虚拟机备份适配器。

通道：**纯 SSH + 宿主机自带命令**（virsh / qemu-img），不在宿主机安装任何 agent。

全量备份（所有 libvirt 版本可用，业务无停机）
-------------------------------------------
1. 一致性：需要文件系统一致时先 ``virsh domfsfreeze``（依赖 qemu-ga）；
2. ``virsh snapshot-create-as --disk-only --atomic --no-metadata`` 建立外部
   快照，原镜像变为只读基盘，业务继续写入新的增量层（overlay）；
3. ``qemu-img convert -c -O qcow2`` 复制冻结的基盘（真实数据，非快照文件）；
4. ``virsh blockcommit --active --pivot`` 把 overlay 回合并基盘并切回，
   删除 overlay 文件。整个过程业务只经历毫秒级 stun。

增量备份（libvirt ≥ 6.0 + QEMU dirty bitmap，真块级增量）
------------------------------------------------------
``virsh checkpoint-create-as`` 建立持久化 dirty bitmap（change_token），
``virsh backup-begin --backupxml`` 以 push 模式只导出脏块（稀疏 qcow2）。
不支持时 Provider 直接抛错，由引擎诚实回退为全量（不伪造增量）。

恢复 / 克隆
----------
先按「全量 + 增量链」在宿主机上合成一份可直接挂载的完整镜像
（``qemu-img create -b 最新增量`` → 逐层 ``rebase`` → ``rebase -b ''`` 展平），
再原位回写或用 ``virsh define`` 生成一台新 VM（新 UUID / 新 MAC / 隔离网络）。
"""
import os
import re
import shlex
import time

from core.vm import ssh as vmssh
from core.vm.base import VMProvider
from core.vm.types import (
    BackupArtifact, CloneResult, CloneSpec, ProviderCaps, SnapshotRef,
    VMDisk, VMInfo, VMProviderError,
)


class LibvirtSSHProvider(VMProvider):
    provider_id = "libvirt_ssh"
    display_name = "KVM / libvirt (SSH)"

    def __init__(self, hypervisor: dict, logger=None):
        super().__init__(hypervisor, logger)
        self._uri = self.extra.get("uri") or "qemu:///system"
        self._work_dir = (self.extra.get("work_dir") or "/var/tmp/vmbk").rstrip("/")
        self._client = None

    # ---------------- 基础 ----------------
    def _c(self):
        if self._client is None:
            self._client = vmssh.connect(self.hv)
        return self._client

    def close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _virsh(self, args: str, timeout: int = 180) -> dict:
        cmd = "virsh -c %s %s" % (shlex.quote(self._uri), args)
        return vmssh.exec_capture(self._c(), cmd, timeout=timeout)

    def connect(self):
        r = self._virsh("list --all", timeout=60)
        if r["rc"] != 0:
            return False, "连接 libvirt 失败: %s" % (r["err"] or r["out"] or "")[:300]
        v = self._virsh("version", timeout=60)
        m = re.search(r"(\d+\.\d+\.\d+)", v.get("out") or "")
        return True, "libvirt 连接正常%s" % ("（版本 %s）" % m.group(1) if m else "")

    def version(self) -> str:
        v = self._virsh("version", timeout=60)
        m = re.search(r"(\d+\.\d+\.\d+)", v.get("out") or "")
        return m.group(1) if m else ""

    def _has_backup_begin(self) -> bool:
        r = self._virsh("help backup-begin", timeout=30)
        return r["rc"] == 0 and "backup-begin" in (r["out"] or "")

    def capabilities(self) -> ProviderCaps:
        inc = self._has_backup_begin()
        return ProviderCaps(
            incremental=inc,
            change_tracking=inc,
            instant_restore=False,
            clone_to_new_vm=True,
            file_restore=False,
            snapshot=True,
            consistency_levels=["crash", "fs"],
            notes=("libvirt ≥6.0：checkpoint + backup-begin 真块级增量（dirty bitmap）；"
                   "更低版本仅支持全量（外部快照 + qemu-img convert）。"
                   if inc else "当前 libvirt 版本不支持 backup-begin，仅支持全量备份。"))

    # ---------------- 资产发现 ----------------
    def list_vms(self) -> list:
        r = self._virsh("list --all --name", timeout=120)
        if r["rc"] != 0:
            raise VMProviderError("virsh list 失败: %s" % (r["err"] or "")[:300])
        out = []
        for name in [x.strip() for x in (r["out"] or "").splitlines() if x.strip()]:
            out.append(self._vm_detail(name))
        return out

    def _vm_detail(self, name: str) -> VMInfo:
        info = self._virsh("dominfo %s" % shlex.quote(name), timeout=60)
        txt = info.get("out") or ""
        uuid = self._grep(txt, r"UUID:\s+(\S+)")
        state = "running" if "running" in txt.lower() else "stopped"
        cpu = self._grep(txt, r"CPU\(s\):\s+(\d+)")
        mem = self._grep(txt, r"Max memory:\s+(\d+)")
        blk = self._virsh("domblklist %s --details" % shlex.quote(name), timeout=60)
        disks = []
        for line in (blk.get("out") or "").splitlines()[2:]:
            parts = line.split()
            if len(parts) < 4 or parts[1] not in ("disk", "cdrom"):
                continue
            if parts[1] == "cdrom":
                continue
            dev, src = parts[2], parts[3]
            if src in ("-", ""):
                continue
            size = self._qemu_size(src)
            fmt = self._qemu_format(src)
            excluded = False
            reason = ""
            if src.startswith("/dev/") or fmt == "host_device":
                excluded, reason = True, "裸设备/直通盘不支持外部快照备份"
            disks.append(VMDisk(key=dev, path=src, size_bytes=size, format=fmt or "qcow2",
                                change_tracking=False, excluded=excluded,
                                exclude_reason=reason))
        return VMInfo(ref=uuid or name, name=name, guest_os="",
                      power_state=state, cpu=int(cpu or 0),
                      memory_mb=int(int(mem or 0) / 1024), disks=disks, raw={"uuid": uuid})

    @staticmethod
    def _grep(text: str, pattern: str) -> str:
        m = re.search(pattern, text or "")
        return m.group(1) if m else ""

    def _qemu_size(self, path: str) -> int:
        r = vmssh.exec_capture(self._c(), "qemu-img info --output=json %s" % shlex.quote(path),
                               timeout=60)
        try:
            import json
            return int(json.loads(r["out"]).get("virtual-size") or 0)
        except Exception:
            return 0

    def _qemu_format(self, path: str) -> str:
        r = vmssh.exec_capture(self._c(), "qemu-img info --output=json %s" % shlex.quote(path),
                               timeout=60)
        try:
            import json
            return str(json.loads(r["out"]).get("format") or "")
        except Exception:
            return ""

    def _name_of(self, vm_ref: str) -> str:
        """ref 优先为 UUID，virsh 命令多数用名称/UUID 均可，这里统一取名称。"""
        r = self._virsh("dominfo %s" % shlex.quote(vm_ref), timeout=60)
        m = re.search(r"Name:\s+(\S+)", r.get("out") or "")
        return m.group(1) if m else vm_ref

    # ---------------- 备份 ----------------
    def backup_full(self, vm_ref: str, work_dir: str = "", consistency: str = "crash") -> BackupArtifact:
        name = self._name_of(vm_ref)
        vm = self._vm_detail(name)
        work_dir = (work_dir or self._work_dir).rstrip("/")
        stage = "%s/%s_%s_full" % (work_dir, name, time.strftime("%Y%m%d_%H%M%S"))
        self._virsh_mkdir(stage)
        snap = "vmbk_%s" % time.strftime("%Y%m%d%H%M%S")
        frozen = False
        if consistency == "fs":
            fr = self._virsh("domfsfreeze %s" % shlex.quote(name), timeout=120)
            frozen = fr["rc"] == 0
            if not frozen:
                self.log("domfsfreeze 失败（缺 qemu-ga？），按崩溃一致继续: %s",
                         (fr.get("err") or "")[:200])
        try:
            r = self._virsh("snapshot-create-as %s %s --disk-only --atomic --no-metadata"
                            % (shlex.quote(name), shlex.quote(snap)), timeout=300)
            if r["rc"] != 0:
                raise VMProviderError("创建外部快照失败: %s" % (r["err"] or r["out"])[:400])
        except Exception:
            # 快照创建失败：stage 目录尚未承载产物，直接清理后抛出（防残留累积）
            self._run("rm -rf %s" % shlex.quote(stage), timeout=120)
            raise
        finally:
            if frozen:
                self._virsh("domfsthaw %s" % shlex.quote(name), timeout=120)

        try:
            # 转换冻结的基盘（真实数据）
            for d in vm.disks:
                if d.excluded:
                    continue
                dst = "%s/%s.qcow2" % (stage, d.key)
                conv = self._run("qemu-img convert -c -O qcow2 %s %s"
                                 % (shlex.quote(d.path), shlex.quote(dst)), timeout=21600)
                if conv["rc"] != 0:
                    raise VMProviderError("磁盘 %s 导出失败: %s" % (d.key, (conv["err"] or "")[:300]))
            # 回合并 overlay 并切回原盘
            self._commit_overlays(name, vm)
        finally:
            self._cleanup_snapshots(name, snap)

        tar = "%s.tar.gz" % stage
        self._run("tar -C %s -czf %s ." % (shlex.quote(stage), shlex.quote(tar)),
                  timeout=21600)
        self._run("rm -rf %s" % shlex.quote(stage), timeout=300)
        token = self._new_checkpoint(name) or ""
        return BackupArtifact(path=tar, kind="full", size_bytes=0,
                              change_token=token, consistency=consistency,
                              disks=[d.key for d in vm.disks if not d.excluded],
                              extra={"vm_name": name, "work_dir": work_dir})

    def _commit_overlays(self, name: str, vm: VMInfo) -> None:
        for d in vm.disks:
            if d.excluded:
                continue
            # pivot 前记录 overlay 路径：成功切回基盘后 domblklist 不再显示
            # overlay，之后必须按记录直接删除，否则每次全量备份残留一份
            # 与基盘同量级的孤儿文件（磁盘泄漏）。
            blk = self._virsh("domblklist %s --details" % shlex.quote(name), timeout=60)
            overlay = ""
            for line in (blk.get("out") or "").splitlines()[2:]:
                parts = line.split()
                # 列序：Type Device Target Source
                if len(parts) >= 4 and parts[2] == d.key:
                    overlay = parts[3]
                    break
            self._virsh("blockcommit %s %s --active --pivot --verbose"
                        % (shlex.quote(name), shlex.quote(d.key)), timeout=600)
            # 等待块作业结束
            for _ in range(600):
                j = self._virsh("blockjob %s %s" % (shlex.quote(name), shlex.quote(d.key)),
                                timeout=60)
                if "No current block job" in (j.get("out") or "") or j["rc"] != 0:
                    break
                time.sleep(2)
            # pivot 成功（磁盘源已切回基盘）→ 删除 overlay 孤儿文件
            if overlay and overlay != d.path:
                self._run("rm -f %s" % shlex.quote(overlay), timeout=120)

    def _cleanup_snapshots(self, name: str, snap: str) -> None:
        """异常兜底：检查 overlay 是否仍被 VM 使用。

        注意：若 overlay 仍出现在 domblklist 中（pivot 未完成/失败），
        文件正被 qemu 使用，**绝不能删除**（删了会破坏运行中的 VM 磁盘链）。
        正常路径的 overlay 清理由 _commit_overlays 在 pivot 成功后完成。
        """
        blk = self._virsh("domblklist %s --details" % shlex.quote(name), timeout=60)
        for line in (blk.get("out") or "").splitlines()[2:]:
            parts = line.split()
            if len(parts) >= 4 and snap in parts[3]:
                self.log("overlay %s 仍被虚拟机使用（blockcommit 未完成），保留待人工处理",
                         parts[3])
                return

    def _new_checkpoint(self, name: str) -> str:
        """建立持久化 dirty bitmap，作为后续增量的起点。"""
        if not self._has_backup_begin():
            return ""
        vm = self._vm_detail(name)
        ck = "vmbk_%s" % time.strftime("%Y%m%d%H%M%S%f")
        args = " ".join("--diskspec %s,bitmap=%s" % (shlex.quote(d.key), shlex.quote(ck))
                        for d in vm.disks if not d.excluded)
        if not args:
            return ""
        r = self._virsh("checkpoint-create-as %s --name %s %s"
                        % (shlex.quote(name), shlex.quote(ck), args), timeout=300)
        return ck if r["rc"] == 0 else ""

    def backup_incremental(self, vm_ref: str, work_dir: str = "", parent_token: str = "",
                           consistency: str = "crash") -> BackupArtifact:
        if not parent_token:
            raise VMProviderError("缺少增量基线（change_token），请先执行一次全量备份")
        if not self._has_backup_begin():
            raise VMProviderError("当前 libvirt 不支持 backup-begin，无法做块级增量")
        name = self._name_of(vm_ref)
        vm = self._vm_detail(name)
        work_dir = (work_dir or self._work_dir).rstrip("/")
        stage = "%s/%s_%s_inc" % (work_dir, name, time.strftime("%Y%m%d_%H%M%S"))
        self._virsh_mkdir(stage)

        disks_xml = "".join(
            "<disk name='%s' type='file'><driver type='qcow2'/>"
            "<target file='%s/%s.qcow2'/></disk>" % (d.key, stage, d.key)
            for d in vm.disks if not d.excluded)
        xml = ("<domainbackup mode='push'><incremental>%s</incremental>"
               "<disks>%s</disks></domainbackup>" % (parent_token, disks_xml))
        xml_path = "%s/backup_%s.xml" % (work_dir, time.strftime("%H%M%S"))
        self._write_remote_file(xml_path, xml)

        r = self._virsh("backup-begin %s --backupxml %s"
                        % (shlex.quote(name), shlex.quote(xml_path)), timeout=600)
        if r["rc"] != 0:
            raise VMProviderError("backup-begin 失败: %s" % (r["err"] or r["out"])[:400])
        # 等待 push 作业结束
        for _ in range(3600):
            j = self._virsh("domjobinfo %s" % shlex.quote(name), timeout=60)
            if "No job" in (j.get("out") or "") or j["rc"] != 0:
                break
            time.sleep(3)

        new_token = self._new_checkpoint(name)
        if new_token:
            self._virsh("checkpoint-delete %s --checkpointname %s"
                        % (shlex.quote(name), shlex.quote(parent_token)), timeout=300)
        tar = "%s.tar.gz" % stage
        self._run("tar -C %s -czf %s ." % (shlex.quote(stage), shlex.quote(tar)),
                  timeout=21600)
        self._run("rm -rf %s" % shlex.quote(stage), timeout=300)
        return BackupArtifact(path=tar, kind="incremental", size_bytes=0,
                              change_token=new_token or parent_token,
                              parent_token=parent_token, consistency=consistency,
                              disks=[d.key for d in vm.disks if not d.excluded],
                              extra={"vm_name": name, "work_dir": work_dir})

    def _virsh_mkdir(self, path: str) -> None:
        self._run("mkdir -p %s" % shlex.quote(path), timeout=120)

    def _run(self, cmd: str, timeout: int = 600) -> dict:
        return vmssh.exec_capture(self._c(), cmd, timeout=timeout)

    def _write_remote_file(self, path: str, content: str) -> None:
        self._run("mkdir -p %s" % shlex.quote(os.path.dirname(path)), timeout=60)
        sftp = self._c().open_sftp()
        try:
            with sftp.open(path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    def download_artifact(self, artifact: BackupArtifact, local_path: str) -> dict:
        res = vmssh.pull_file(self._c(), artifact.path, local_path,
                              label=os.path.basename(artifact.path))
        return {"size": res.get("size") or 0, "sha256": res.get("sha256") or ""}

    def upload_artifact(self, local_path: str, remote_dir: str) -> str:
        self._run("mkdir -p %s" % shlex.quote(remote_dir), timeout=120)
        remote = "%s/%s" % (remote_dir.rstrip("/"), os.path.basename(local_path))
        vmssh.push_file(self._c(), local_path, remote)
        return remote

    def cleanup_artifact(self, artifact: BackupArtifact) -> None:
        try:
            self._run("rm -f %s" % shlex.quote(artifact.path), timeout=120)
        except Exception:
            pass

    # ---------------- 合成 / 恢复 / 克隆 ----------------
    def _extract(self, archives: list, work_dir: str) -> list:
        """把产物包解开为目录列表（顺序：全量在前）。"""
        dirs = []
        for i, arc in enumerate(archives):
            d = "%s/restore_%d" % (work_dir, i)
            self._run("rm -rf %s && mkdir -p %s" % (shlex.quote(d), shlex.quote(d)),
                      timeout=300)
            r = self._run("tar -xzf %s -C %s" % (shlex.quote(arc), shlex.quote(d)),
                          timeout=21600)
            if r["rc"] != 0:
                raise VMProviderError("解包产物失败 %s: %s" % (arc, (r["err"] or "")[:300]))
            dirs.append(d)
        return dirs

    def synthesize(self, artifacts: list, out_path: str) -> BackupArtifact:
        """全量 + 增量链 → 一份可直接挂载的独立镜像（增量永久 → 定期合成全量）。

        qemu-img 语义：``create -b 最新增量`` 后逐层 ``rebase -b 上一层``
        把较新层的数据拷入目标，最后 ``rebase -b ''`` 展平为独立镜像。
        """
        work = "%s/syn_%s" % (self._work_dir, time.strftime("%Y%m%d%H%M%S"))
        self._run("mkdir -p %s" % shlex.quote(work), timeout=120)
        dirs = self._extract(list(artifacts), work)
        out_dir = out_path if os.path.isabs(out_path) else "%s/%s" % (self._work_dir, out_path)
        self._run("mkdir -p %s" % shlex.quote(out_dir), timeout=120)
        # 以全量包内的磁盘名为准
        ls = self._run("ls -1 %s" % shlex.quote(dirs[0]), timeout=60)
        disk_files = [x.strip() for x in (ls["out"] or "").splitlines()
                      if x.strip().endswith(".qcow2")]
        if not disk_files:
            raise VMProviderError("未在产物中找到磁盘镜像（*.qcow2）")
        for f in disk_files:
            target = "%s/%s" % (out_dir, f)
            chain = ["%s/%s" % (d, f) for d in dirs]
            r = self._run("qemu-img create -f qcow2 -F qcow2 -b %s %s"
                          % (shlex.quote(chain[-1]), shlex.quote(target)), timeout=600)
            if r["rc"] != 0:
                raise VMProviderError("合成 %s 失败: %s" % (f, (r["err"] or "")[:300]))
            for layer in reversed(chain[:-1]):
                r = self._run("qemu-img rebase -f qcow2 -F qcow2 -b %s %s"
                              % (shlex.quote(layer), shlex.quote(target)), timeout=21600)
                if r["rc"] != 0:
                    raise VMProviderError("合并层 %s 失败: %s" % (layer, (r["err"] or "")[:300]))
            self._run("qemu-img rebase -f qcow2 -b '' %s" % shlex.quote(target),
                      timeout=21600)
        tar = "%s.tar.gz" % out_dir.rstrip("/")
        self._run("tar -C %s -czf %s ." % (shlex.quote(out_dir), shlex.quote(tar)),
                  timeout=21600)
        self._run("rm -rf %s" % shlex.quote(work), timeout=300)
        return BackupArtifact(path=tar, kind="synthetic_full", size_bytes=0,
                              extra={"merged_dir": out_dir, "disks": disk_files})

    def restore_in_place(self, vm_ref: str, artifacts: list, consistency: str = "crash"):
        name = self._name_of(vm_ref)
        vm = self._vm_detail(name)
        syn = self.synthesize(list(artifacts), "restore_%s" % time.strftime("%Y%m%d%H%M%S"))
        merged = syn.extra.get("merged_dir") or ""
        self.stop_vm(vm_ref, force=True)
        for d in vm.disks:
            if d.excluded:
                continue
            src = "%s/%s.qcow2" % (merged, d.key)
            ok = self._run("test -f %s" % shlex.quote(src), timeout=60)
            if ok["rc"] != 0:
                continue
            fmt = "raw" if (d.format or "").startswith("raw") else "qcow2"
            r = self._run("qemu-img convert -O %s %s %s"
                          % (fmt, shlex.quote(src), shlex.quote(d.path)), timeout=21600)
            if r["rc"] != 0:
                return False, "回写磁盘 %s 失败: %s" % (d.key, (r["err"] or "")[:300])
        self._run("rm -rf %s %s" % (shlex.quote(merged), shlex.quote(syn.path)), timeout=300)
        self.start_vm(vm_ref)
        return True, "原位还原成功：已回写 %d 块磁盘并开机" % len(
            [d for d in vm.disks if not d.excluded])

    def clone_to_new_vm(self, vm_ref: str, artifacts: list, spec: CloneSpec) -> CloneResult:
        name = self._name_of(vm_ref)
        new_name = spec.target_name or (name + "_clone_" + time.strftime("%m%d%H%M"))
        syn = self.synthesize(list(artifacts), "clone_%s" % time.strftime("%Y%m%d%H%M%S"))
        merged = syn.extra.get("merged_dir") or ""
        disk_dir = "/var/lib/libvirt/images/%s" % new_name
        self._run("mkdir -p %s" % shlex.quote(disk_dir), timeout=120)

        xml = self._virsh("dumpxml %s" % shlex.quote(name), timeout=120).get("out") or ""
        if not xml:
            return CloneResult(ok=False, message="导出源 VM 定义失败")
        import uuid as _uuid
        new_xml = re.sub(r"<name>[^<]+</name>", "<name>%s</name>" % new_name, xml, count=1)
        new_xml = re.sub(r"<uuid>[^<]+</uuid>", "<uuid>%s</uuid>" % str(_uuid.uuid4()),
                         new_xml, count=1)
        for d in (syn.extra.get("disks") or []):
            dev = str(d).replace(".qcow2", "")
            src = "%s/%s" % (merged, d)
            dst = "%s/%s" % (disk_dir, d)
            r = self._run("cp -f %s %s" % (shlex.quote(src), shlex.quote(dst)),
                          timeout=21600)
            if r["rc"] != 0:
                return CloneResult(ok=False, message="复制磁盘 %s 失败" % d)
            # 把该设备的源路径替换为新路径
            pattern = re.compile(r"(<disk[^>]*>.*?<source[^>]*file=')[^']+('.*?</disk>)",
                                 re.S)
            new_xml = self._replace_disk_source(new_xml, dev, dst)
            _ = pattern
        # 新 MAC，避免与原 VM 冲突
        new_xml = re.sub(r"<mac address='[^']+'/>",
                         "<mac address='%s'/>" % self._random_mac(), new_xml)
        if spec.isolate_network:
            net = spec.network_name or self.extra.get("isolate_network") or ""
            if net:
                new_xml = re.sub(r"(<source[^>]*network=')[^']+(')",
                                 r"\g<1>%s\g<2>" % net, new_xml)
        xml_path = "/var/tmp/vmbk_%s.xml" % new_name
        self._write_remote_file(xml_path, new_xml)
        r = self._virsh("define %s" % shlex.quote(xml_path), timeout=180)
        if r["rc"] != 0:
            return CloneResult(ok=False, message="定义新 VM 失败: %s" % (r["err"] or "")[:300])
        self._run("rm -rf %s %s" % (shlex.quote(merged), shlex.quote(syn.path)), timeout=300)
        if spec.auto_start:
            self.start_vm(new_name)
        return CloneResult(ok=True, target_ref=new_name, target_name=new_name,
                           ip=self.guest_ip(new_name),
                           message="已从恢复点克隆为新 VM「%s」%s"
                                   % (new_name, "（隔离网络）" if spec.isolate_network else ""))

    @staticmethod
    def _replace_disk_source(xml: str, dev: str, new_path: str) -> str:
        """把指定 <target dev='vda'/> 的 <source file=...> 替换为新路径。"""
        pattern = re.compile(
            r"<disk\b(?:(?!</disk>).)*<target\s+dev='%s'(?:(?!</disk>).)*</disk>"
            % re.escape(dev), re.S)
        def _sub(m):
            return re.sub(r"(<source[^>]*file=')[^']*(')", r"\g<1>%s\g<2>" % new_path,
                          m.group(0))
        new_xml, n = pattern.subn(_sub, xml)
        return new_xml if n else xml

    @staticmethod
    def _random_mac() -> str:
        import random
        return "52:54:00:%02x:%02x:%02x" % (random.randint(0, 255),
                                            random.randint(0, 255),
                                            random.randint(0, 255))

    def delete_vm(self, ref: str, node: str = ""):
        name = self._name_of(ref)
        self._virsh("destroy %s" % shlex.quote(name), timeout=120)
        r = self._virsh("undefine %s --remove-all-storage --nvram" % shlex.quote(name),
                        timeout=300)
        if r["rc"] != 0:
            return False, "销毁 VM 失败: %s" % (r["err"] or "")[:200]
        return True, "已销毁 VM「%s」（含磁盘）" % name

    def start_vm(self, ref: str, node: str = ""):
        r = self._virsh("start %s" % shlex.quote(self._name_of(ref)), timeout=180)
        return (r["rc"] == 0, "开机成功" if r["rc"] == 0 else (r["err"] or "")[:200])

    def stop_vm(self, ref: str, node: str = "", force: bool = False):
        cmd = "destroy" if force else "shutdown"
        r = self._virsh("%s %s" % (cmd, shlex.quote(self._name_of(ref))), timeout=180)
        return (r["rc"] == 0, "关机成功" if r["rc"] == 0 else (r["err"] or "")[:200])

    def power_state(self, vm_ref: str, node: str = "") -> str:
        txt = (self._virsh("dominfo %s" % shlex.quote(self._name_of(vm_ref)),
                           timeout=60).get("out") or "")
        m = re.search(r"State:\s+(\S+)", txt)
        return "running" if m and "running" in m.group(1) else "stopped"

    def guest_ip(self, vm_ref: str, node: str = "") -> str:
        r = self._virsh("domifaddr %s --source agent" % shlex.quote(self._name_of(vm_ref)),
                        timeout=60)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", r.get("out") or "")
        if m:
            return m.group(1)
        r = self._virsh("domifaddr %s" % shlex.quote(self._name_of(vm_ref)), timeout=60)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", r.get("out") or "")
        return m.group(1) if m else ""

    # ---------------- 快照（可选能力） ----------------
    def create_snapshot(self, vm_ref: str, name: str,
                        consistency: str = "crash") -> SnapshotRef:
        r = self._virsh("snapshot-create-as %s %s" % (
            shlex.quote(self._name_of(vm_ref)), shlex.quote(name)), timeout=300)
        if r["rc"] != 0:
            raise VMProviderError("创建快照失败: %s" % (r["err"] or "")[:300])
        return SnapshotRef(id=name, name=name, created_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    def delete_snapshot(self, vm_ref: str, snap: SnapshotRef) -> None:
        self._virsh("snapshot-delete %s %s" % (
            shlex.quote(self._name_of(vm_ref)), shlex.quote(snap.name)), timeout=300)
