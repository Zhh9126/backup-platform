# -*- coding: utf-8 -*-
"""VMware Workstation 虚拟机备份适配器（Windows 宿主机 + SSH，零安装）。

为什么这样实现
--------------
Workstation 是桌面虚拟化，没有 vCenter/ESXi 的 API Server，也没有 CBT/块级
增量接口。可用的稳定通道只有两条，且都**不需要在被管端安装任何东西**：

* 控制面：Workstation 自带的 ``vmrun.exe``（VIX 命令行）：
  ``list``（运行中 VM）、``start`` / ``stop`` / ``suspend`` / ``snapshot`` /
  ``getGuestIPAddress``；安装路径从注册表 ``InstallPath`` 探测。
* 数据面：同一安装目录自带的 ``7za.exe`` 打包 VM 目录（zip），经 SSH/SFTP
  断点续传拉回平台。

一致性
------
Workstation 没有静默快照接口（``vmrun snapshot`` 会冻结前台，需要人工确认，
不适用于无人值守备份）。可达的最高一致性是「挂起后拷贝」，但挂起会冻结
业务虚拟机，代价高。因此默认采用 **崩溃一致**：排除内存态与锁文件，备份期间
VM 若在运行，磁盘处于「随时可能被写入」的状态，恢复后相当于断电重启。
平台如实声明一致性等级，不伪造文件系统一致。

能力边界（如实声明，不造假）
----------------------------
* 无块级增量（Workstation 无变更跟踪）→ 增量请求会被引擎明确回退为全量。
* 无即时恢复（无 API Server，无法让备份仓库直接开机）。
* 不含内存态：排除 ``*.vmem`` / ``*.vmss`` / ``*.lck`` / ``vmware*.log``。
* 快照为「前台可见」快照，仅作恢复点辅助，不参与备份数据面。
"""
import os
import re
import uuid

from core.vm import ssh as vmssh
from core.vm.base import VMProvider
from core.vm.types import (
    BackupArtifact, CloneResult, CloneSpec, ProviderCaps, SnapshotRef,
    VMDisk, VMInfo, VMProviderError,
)

_PS_BOOT = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "$ErrorActionPreference='Continue';")


def _q(value: str) -> str:
    """PowerShell 单引号字符串字面量（内部单引号翻倍）。"""
    return "'" + str(value or "").replace("'", "''") + "'"


def _norm(path: str) -> str:
    """路径归一化（比较用：统一反斜杠、去尾斜杠、忽略大小写）。"""
    p = str(path or "").strip().strip('"').replace("/", "\\").rstrip("\\")
    return p.lower()


def _dq(value: str) -> str:
    """PowerShell 双引号字符串字面量（Start-Process 的 ArgumentList 数组里
    必须用双引号：单引号里再嵌单引号会被 PowerShell 解析成空参数）。"""
    return '"%s"' % str(value or "").replace('"', '""')


def _wdir(path: str) -> str:
    """Windows 路径的父目录（os.path.dirname 只认 '/', 对 'C:\a\b' 会返回空）。"""
    q = str(path or "").replace("/", "\\").rstrip("\\")
    i = q.rfind("\\")
    return q[:i] if i > 0 else ""


def _wbase(path: str) -> str:
    """Windows 路径的文件名部分。"""
    q = str(path or "").replace("/", "\\").rstrip("\\")
    i = q.rfind("\\")
    return q[i + 1:] if i >= 0 else q


class WorkstationSSHProvider(VMProvider):
    provider_id = "workstation_ssh"
    display_name = "VMware Workstation (SSH)"

    # 备份时排除的文件（内存态 / 锁 / 日志）
    _EXCLUDE_GLOBS = ("*.vmem", "*.vmss", "*.lck", "vmware*.log",
                      "*.scoreboard")

    def __init__(self, hypervisor: dict, logger=None):
        super().__init__(hypervisor, logger)
        self._client = None
        self._vmrun = ""
        self._sevenzip = ""
        self._temp_dir = ""
        self._work_dir = (self.extra.get("work_dir")
                          or r"%TEMP%\aidbm_vm").strip()

    # ---------------- 通道 ----------------
    def _c(self):
        if self._client is None:
            self._client = vmssh.connect(self.hv)
        return self._client

    def _temp_base(self) -> str:
        """远端用户临时目录（正斜杠绝对路径，SFTP 可直接使用）。"""
        if self._temp_dir:
            return self._temp_dir
        r = vmssh.exec_capture(self._c(), "echo %TEMP%", timeout=30, wrap=False)
        raw = (r.get("out") or "").strip()
        if not raw:
            raise VMProviderError("无法获取远端临时目录（echo %TEMP% 无输出）")
        self._temp_dir = raw.replace("\\", "/").rstrip("/")
        return self._temp_dir

    def _ps(self, script: str, timeout: int = 600) -> dict:
        """在远端执行 PowerShell 脚本并取回 UTF-8 输出。

        走「SFTP 写脚本 → powershell -File → SFTP 读输出文件」，而不是把
        PowerShell 直接塞进 SSH 命令行：Windows OpenSSH 的控制台编码会把
        中文路径/输出转成乱码，写文件再读字节才可靠。
        """
        client = self._c()
        tag = uuid.uuid4().hex[:12]
        base = "%s/aidbm_%s" % (self._temp_base(), tag)
        ps_path = base + ".ps1"
        out_path = base + ".out"
        sftp = client.open_sftp()
        try:
            with sftp.open(ps_path, "w") as f:
                # 必须带 UTF-8 BOM：Windows PowerShell 5.1 的 -File 默认按 ANSI
                # 解析脚本，无 BOM 时中文路径（如「AIDBM测试1」）会被读成乱码，
                # 导致 Test-Path 全部落空（表现为「虚拟机不存在」）。
                f.write((_PS_BOOT + "\n" + script + "\n").encode("utf-8-sig"))
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        cmd = ('powershell -NoProfile -ExecutionPolicy Bypass -File "%s" '
               '> "%s" 2>&1' % (ps_path.replace("/", "\\"), out_path.replace("/", "\\")))
        try:
            r = vmssh.exec_capture(client, cmd, timeout=timeout, wrap=False)
            out = ""
            try:
                sftp = client.open_sftp()
                try:
                    with sftp.open(out_path, "rb") as f:
                        out = f.read().decode("utf-8", "ignore")
                finally:
                    try:
                        sftp.close()
                    except Exception:
                        pass
            except Exception:
                out = ""
            if not out.strip():
                out = (r.get("out") or "") + (r.get("err") or "")
            return {"rc": r.get("rc", 0), "out": out, "err": r.get("err") or ""}
        finally:
            for p in (ps_path, out_path):
                try:
                    vmssh.exec_capture(client, 'del /f "%s" 2>nul'
                                       % p.replace("/", "\\"),
                                       timeout=30, wrap=False)
                except Exception:
                    pass

    def _psv(self, script: str, timeout: int = 600) -> list:
        """执行 PowerShell 并按行返回输出（去空行）。"""
        r = self._ps(script, timeout=timeout)
        return [ln for ln in (r.get("out") or "").splitlines() if ln.strip()]

    def _vmrun_path(self) -> str:
        if self._vmrun:
            return self._vmrun
        cand = (self.extra.get("vmrun_path") or "").strip()
        if cand:
            self._vmrun = cand
            return cand
        lines = self._psv(
            "$p=(Get-ItemProperty 'HKLM:\\SOFTWARE\\WOW6432Node\\VMware, Inc.\\VMware Workstation' "
            "-ErrorAction SilentlyContinue).InstallPath; "
            "if(-not $p){$p=(Get-ItemProperty 'HKLM:\\SOFTWARE\\VMware, Inc.\\VMware Workstation' "
            "-ErrorAction SilentlyContinue).InstallPath}; "
            "if($p){ Write-Output (Join-Path $p 'vmrun.exe') }", timeout=120)
        if lines and lines[0].lower().endswith("vmrun.exe"):
            self._vmrun = lines[0].strip()
            return self._vmrun
        for guess in (r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
                      r"C:\Program Files\VMware\VMware Workstation\vmrun.exe",
                      r"D:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"):
            chk = self._psv('if(Test-Path -LiteralPath "%s"){Write-Output "%s"}'
                            % (guess, guess), timeout=60)
            if chk:
                self._vmrun = chk[0].strip()
                return self._vmrun
        raise VMProviderError("未找到 vmrun.exe（请在纳管参数里指定 vmrun_path，"
                              "或确认该主机已安装 VMware Workstation）")

    def _sevenzip_path(self) -> str:
        if self._sevenzip:
            return self._sevenzip
        base = _wdir(self._vmrun_path())
        cand = base + "\\7za.exe"
        chk = self._psv('if(Test-Path -LiteralPath "%s"){Write-Output "%s"}'
                        % (cand, cand), timeout=60)
        if chk:
            self._sevenzip = chk[0].strip()
            return self._sevenzip
        raise VMProviderError("未找到 7za.exe（Workstation 安装目录自带，缺失说明安装不完整）")

    def _expand_env(self, path: str) -> str:
        """展开 %TEMP% 等环境变量（远端求值，避免平台侧猜测用户目录）。"""
        raw = str(path or "").strip()
        if "%" not in raw:
            return raw
        lines = self._psv("Write-Output ([Environment]::ExpandEnvironmentVariables(%s))"
                          % _q(raw), timeout=60)
        return lines[0].strip() if lines else raw

    # ---------------- 基础 ----------------
    def connect(self):
        try:
            vr = self._vmrun_path()
        except Exception as e:
            return False, str(e)[:300]
        r = self._ps("& %s -T ws list 2>&1" % _q(vr), timeout=120)
        out = r.get("out") or ""
        if "Total running VMs" not in out:
            return False, "vmrun 执行失败: %s" % (out or r.get("err") or "")[:300]
        return True, ("Workstation 连接正常（%s，vmrun=%s）"
                      % (self.version() or "未知版本", vr))

    def version(self) -> str:
        try:
            vr = self._vmrun_path()
        except Exception:
            return ""
        for ln in self._psv("& %s -T ws -v 2>&1 | Select-Object -First 3" % _q(vr), timeout=60):
            if "VMware Workstation" in ln:
                return ln.strip()[:80]
        return ""

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            incremental=False, change_tracking=False, instant_restore=False,
            clone_to_new_vm=True, file_restore=False, snapshot=True,
            consistency_levels=["crash"],
            notes="VMware Workstation（Windows + SSH）：目录级全量备份 + 崩溃一致；"
                  "Workstation 无变更跟踪，暂无块级增量（增量任务会诚实回退全量）；"
                  "备份不含内存态（排除 .vmem/.vmss），恢复后等同冷启动。")

    # ---------------- 资产发现 ----------------
    def _lib_dirs(self) -> list:
        dirs = []
        raw = self.extra.get("library_dirs") or ""
        if isinstance(raw, list):
            dirs = [str(x).strip() for x in raw if str(x).strip()]
        elif isinstance(raw, str) and raw.strip():
            dirs = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
        if dirs:
            return dirs
        lines = self._psv(
            "Get-PSDrive -PSProvider FileSystem | ForEach-Object { $r=$_.Root; "
            "Get-ChildItem -Path (Join-Path $r 'Users') -Directory -ErrorAction SilentlyContinue | "
            "ForEach-Object { $p=Join-Path $_.FullName 'Documents\\Virtual Machines'; "
            "if(Test-Path -LiteralPath $p){ Write-Output $p } } }", timeout=180)
        return [ln.strip() for ln in lines if ln.strip()]

    def _scan_vmx_files(self, dirs=None) -> list:
        """扫描库目录下的 .vmx（按扩展名精确过滤，规避 8.3 短名误匹配）。"""
        dirs = dirs if dirs is not None else self._lib_dirs()
        if not dirs:
            return []
        arr = ",".join(_q(d) for d in dirs)
        script = ("$dirs=@(%s); foreach($d in $dirs){ if(Test-Path -LiteralPath $d){ "
                  "Get-ChildItem -LiteralPath $d -Filter *.vmx -Recurse -Depth 3 "
                  "-ErrorAction SilentlyContinue | "
                  "Where-Object { $_.Extension -eq '.vmx' } | "
                  "ForEach-Object { Write-Output $_.FullName } } }" % arr)
        return [ln.strip() for ln in self._psv(script, timeout=300) if ln.strip()]

    def list_vms(self) -> list:
        dirs = self._lib_dirs()
        if not dirs:
            raise VMProviderError("未找到 Workstation 虚拟机库目录（请在纳管参数里指定 library_dirs）")
        out = []
        for vmx in self._scan_vmx_files(dirs):
            info = self._parse_vmx(vmx)
            if info:
                out.append(info)
        running = {_norm(x) for x in self._running_vms()}
        for vm in out:
            vm.power_state = "running" if _norm(vm.ref) in running else "stopped"
            vm.node = _wdir(vm.ref)
        return out

    def _running_vms(self) -> set:
        try:
            vr = self._vmrun_path()
        except Exception:
            return set()
        lines = self._psv("& %s -T ws list 2>&1" % _q(vr), timeout=120)
        return set(ln.strip() for ln in lines if ln.strip().lower().endswith(".vmx"))

    def _parse_vmx(self, vmx_path: str):
        vmx_path = vmx_path.strip()
        if not vmx_path.lower().endswith(".vmx"):
            return None
        text = self._psv("Get-Content -LiteralPath %s -ErrorAction SilentlyContinue"
                         % _q(vmx_path), timeout=60)
        conf = {}
        for ln in text:
            # 键里可能带冒号（scsi0:0.fileName），必须允许 ':'，否则磁盘解析全空
            m = re.match(r'^([\w.:]+)\s*=\s*"(.*)"\s*$', ln.strip())
            if m:
                conf.setdefault(m.group(1), m.group(2))
        name = conf.get("displayName") or _wbase(vmx_path)[:-4]
        guest = conf.get("guestOS") or ""
        try:
            mem = int(conf.get("memsize") or 0)
        except Exception:
            mem = 0
        try:
            cpu = int(conf.get("numvcpus") or 0)
        except Exception:
            cpu = 0
        return VMInfo(ref=vmx_path, name=name, guest_os=guest,
                      power_state="unknown", cpu=cpu, memory_mb=mem,
                      disks=self._disks_of(vmx_path, conf),
                      raw={"vmx": vmx_path, "dir": _wdir(vmx_path),
                           "display_name": name, "guest_os": guest})

    def _disks_of(self, vmx_path: str, conf: dict) -> list:
        vdir = _wdir(vmx_path)
        disks = []
        seen = set()
        for key, val in conf.items():
            m = re.match(r"^(scsi|sata|nvme|ide)(\d+):(\d+)\.fileName$", key)
            if not m:
                continue
            fname = (val or "").strip()
            if not fname.lower().endswith(".vmdk") or fname in seen:
                continue
            seen.add(fname)
            dev_type = (conf.get("%s%s:%s.deviceType" % (m.group(1), m.group(2),
                                                        m.group(3))) or "").lower()
            if "cdrom" in dev_type:
                disks.append(VMDisk(key=key, path=fname, size_bytes=0,
                                    format="iso", excluded=True,
                                    exclude_reason="光驱 ISO 镜像不计入虚拟磁盘容量"))
                continue
            full = fname if (":" in fname or fname.startswith("\\\\")) \
                else vdir.rstrip("\\") + "\\" + fname
            disks.append(VMDisk(key=key, path=full,
                                 size_bytes=self._disk_total(vdir, fname),
                                 format="vmdk"))
        return disks

    def _disk_total(self, vdir: str, fname: str) -> int:
        """分盘（split extent）总容量：同前缀 -sNNN.vmdk 求和。"""
        own = self._file_size(vdir.rstrip("\\") + "\\" + fname)
        base = fname[:-5] if fname.lower().endswith(".vmdk") else fname
        total = own
        try:
            lines = self._psv(
                "Get-ChildItem -LiteralPath %s -Filter %s -ErrorAction SilentlyContinue | "
                "ForEach-Object { Write-Output $_.Length }"
                % (_q(vdir), _q(base + "-s*.vmdk")), timeout=120)
        except Exception:
            lines = []
        for ln in lines:
            try:
                total = max(total, own + int(ln.strip()))
            except Exception:
                continue
        return total

    def _file_size(self, path: str) -> int:
        lines = self._psv(
            "if(Test-Path -LiteralPath %s){ $i=Get-Item -LiteralPath %s -ErrorAction SilentlyContinue; "
            "if($i){ Write-Output $i.Length } }" % (_q(path), _q(path)), timeout=60)
        try:
            return int(lines[0].strip()) if lines else 0
        except Exception:
            return 0

    def _exists(self, path: str) -> bool:
        lines = self._psv("if(Test-Path -LiteralPath %s){Write-Output 'Y'}" % _q(path),
                          timeout=60)
        return bool(lines)

    def _find_vmx(self, ref: str) -> str:
        """按标识定位 .vmx（支持绝对路径 / 库目录内的文件名或显示名）。"""
        raw = str(ref or "").strip().strip('"')
        if not raw:
            raise VMProviderError("虚拟机标识为空")
        cand = raw.replace("/", "\\")
        if cand.lower().endswith(".vmx") and self._exists(cand):
            return cand
        base = _wbase(cand)
        if base.lower().endswith(".vmx"):
            base = base[:-4]
        for p in self._scan_vmx_files():
            if _wbase(p)[:-4].lower() == base.lower():
                return p
        raise VMProviderError("未找到虚拟机配置文件: %s" % raw)

    # ---------------- 备份 ----------------
    def _stage_dir(self, prefix: str) -> str:
        base = self._expand_env(self._work_dir).replace("/", "\\").rstrip("\\")
        return "%s\\aidbm_%s_%s" % (base, prefix, uuid.uuid4().hex[:10])

    def _exclude_args(self) -> list:
        globs = list(self._EXCLUDE_GLOBS)
        extra = self.extra.get("excludes") or []
        if isinstance(extra, str):
            extra = [x for x in extra.replace(";", ",").split(",") if x.strip()]
        for g in extra:
            g = str(g).strip()
            if g and g not in globs:
                globs.append(g)
        return ["-xr!%s" % g for g in globs]

    def _vmdk_files(self, vmdir: str) -> list:
        """VM 目录下的全部虚拟磁盘文件名（分盘 extent 一并列出）。"""
        lines = self._psv("Get-ChildItem -LiteralPath %s -Filter '*.vmdk' -File "
                          "-ErrorAction SilentlyContinue | "
                          "ForEach-Object { Write-Output $_.Name }" % _q(vmdir), timeout=120)
        return [ln.strip() for ln in lines if ln.strip().lower().endswith(".vmdk")]

    def _zip_entries(self, zip_path: str) -> list:
        """读取远端 zip 的条目名（平台侧还要再校验 sha256，双保险）。"""
        lines = self._psv(
            "Add-Type -AssemblyName System.IO.Compression.FileSystem; "
            "$z=[IO.Compression.ZipFile]::OpenRead(%s); "
            "$z.Entries | ForEach-Object { Write-Output $_.FullName }; "
            "$z.Dispose()" % _q(zip_path), timeout=300)
        return [ln.strip() for ln in lines if ln.strip()]

    def _package(self, vmdir: str, zip_path: str) -> str:
        """用 7za 把 VM 目录打成 zip（排除内存态/锁/日志），返回 7za 输出。"""
        level = str(self.extra.get("compress_level") or "1")
        sz = self._sevenzip_path()
        excludes = " ".join(_q(x) for x in self._exclude_args())
        script = (
            "$sz=%s; $out=%s; $src=%s\n"
            "& $sz a -tzip -mx=%s -y $out $src %s\n"
            "Write-Output ('AIDBM_7ZA_RC=' + $LASTEXITCODE)\n"
            "if(-not (Test-Path -LiteralPath $out)){ Write-Output 'AIDBM_NO_ARCHIVE' }\n"
            "if(Test-Path -LiteralPath $out){ Write-Output ('AIDBM_ARCHIVE_SIZE=' + "
            "(Get-Item -LiteralPath $out).Length) }"
            % (_q(sz), _q(zip_path), _q(vmdir + "\\*"), level, excludes))
        r = self._ps(script, timeout=int(self.extra.get("backup_timeout") or 21600))
        out = r.get("out") or ""
        if "AIDBM_NO_ARCHIVE" in out:
            raise VMProviderError("Workstation 备份失败：7za 未生成产物（%s）"
                                  % (out or r.get("err") or "")[:300])
        return out

    def _wait_suspended(self, vmx: str, wait_sec: int = 120) -> bool:
        """等待虚拟机进入挂起态（.vmss 出现且不再出现在 vmrun 运行列表）。"""
        vmdir = _wdir(vmx)
        for _ in range(max(1, wait_sec // 5)):
            ss = self._psv("Get-ChildItem -LiteralPath %s -Filter '*.vmss' -File "
                           "-Force -ErrorAction SilentlyContinue | "
                           "ForEach-Object { Write-Output $_.Name }" % _q(vmdir), timeout=60)
            running = {_norm(x) for x in self._running_vms()}
            if ss and _norm(vmx) not in running:
                return True
            try:
                self._ps("Start-Sleep -Seconds 5", timeout=30)
            except Exception:
                break
        return False

    def backup_full(self, vm_ref: str, work_dir: str,
                    consistency: str = "crash") -> BackupArtifact:
        vmx = self._find_vmx(vm_ref)
        vmdir = _wdir(vmx)
        running = {_norm(x) for x in self._running_vms()}
        was_running = _norm(vmx) in running
        expect = self._vmdk_files(vmdir)

        stage = self._expand_env(work_dir or self._work_dir)
        run_dir = "%s\\aidbm_%s" % (stage.replace("/", "\\").rstrip("\\"),
                                    uuid.uuid4().hex[:10])
        zip_path = run_dir + "\\vm.zip"
        self._ps("New-Item -ItemType Directory -Force -Path %s | Out-Null"
                 % _q(run_dir), timeout=180)

        out = self._package(vmdir, zip_path)
        entries = self._zip_entries(zip_path)
        have = {_wbase(e).lower() for e in entries}
        missing = [f for f in expect if f.lower() not in have]

        suspend_note = ""
        if missing and was_running:
            # 运行中的 Workstation 虚拟机会独占磁盘文件（7za 读不到 → 产物残缺）。
            # 官方可行的做法是「软挂起」：挂起后释放磁盘句柄，拷完再恢复运行。
            allow = str(self.extra.get("suspend_before_backup", "1")).lower()
            allow = allow not in ("0", "false", "no", "off")
            if allow:
                self._spawn_suspend(vmx)
                if self._wait_suspended(vmx, int(self.extra.get("suspend_wait_sec") or 120)):
                    suspend_note = ("备份期间已自动挂起虚拟机（业务短暂冻结），"
                                    "拷完已恢复运行")
                    try:
                        out = self._package(vmdir, zip_path)
                        entries = self._zip_entries(zip_path)
                        have = {_wbase(e).lower() for e in entries}
                        missing = [f for f in expect if f.lower() not in have]
                    finally:
                        self._spawn_start(vmx)
                        self._wait_running(vmx, int(self.extra.get("start_wait_sec") or 120))
                else:
                    suspend_note = "（尝试自动挂起未成功）"
            else:
                suspend_note = "（未启用自动挂起）"

        if missing:
            raise VMProviderError(
                "Workstation 备份产物不完整：磁盘文件 %s 无法读取——运行中的 "
                "Workstation 虚拟机会独占磁盘文件。请先关闭虚拟机后重试，或在纳管"
                "参数中开启 suspend_before_backup（备份期间自动挂起并恢复）。"
                % ", ".join(missing[:8]))

        size = 0
        m = re.search(r"AIDBM_ARCHIVE_SIZE=(\d+)", out)
        if m:
            size = int(m.group(1))
        if size <= 0:
            size = self._file_size(zip_path)
        if size <= 0:
            raise VMProviderError("Workstation 备份产物为空（7za 未产出有效 zip）：%s"
                                  % out[:200])

        if was_running:
            note = "备份时虚拟机处于运行态，磁盘为崩溃一致（恢复后等同断电重启）"
        else:
            note = "备份时虚拟机已关机，磁盘为静止状态（崩溃一致）"
        if suspend_note:
            note += "；%s" % suspend_note

        return BackupArtifact(
            path=zip_path.replace("\\", "/"), kind="full", size_bytes=size,
            consistency=consistency,
            disks=list(expect),
            extra={"vmx": vmx, "dir": vmdir, "vm_name": _wbase(vmx)[:-4],
                   "was_running": was_running, "remote_dir": run_dir.replace("\\", "/"),
                   "note": note, "provider": self.provider_id,
                   "entries": entries[:200], "file_count": len(entries),
                   "excludes": self._exclude_args()})

    def backup_incremental(self, vm_ref: str, work_dir: str, parent_token: str,
                           consistency: str = "crash") -> BackupArtifact:
        raise VMProviderError(
            "VMware Workstation 无变更跟踪（CBT）接口，不支持块级增量；"
            "请将任务改为全量或缩短全量周期。")

    def download_artifact(self, artifact: BackupArtifact, local_path: str) -> dict:
        res = vmssh.pull_file(self._c(), artifact.path, local_path,
                              label=os.path.basename(artifact.path))
        return {"size": res.get("size") or 0, "sha256": res.get("sha256") or ""}

    def upload_artifact(self, local_path: str, remote_dir: str) -> str:
        rd = str(remote_dir or self._work_dir).replace("/", "\\").rstrip("\\")
        rd = self._expand_env(rd)
        self._ps("New-Item -ItemType Directory -Force -Path %s | Out-Null" % _q(rd),
                 timeout=180)
        remote = rd.replace("\\", "/") + "/" + os.path.basename(local_path)
        sftp = self._c().open_sftp()
        try:
            sftp.put(local_path, remote)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        return remote

    def cleanup_artifact(self, artifact: BackupArtifact) -> None:
        path = str(artifact.path or "").replace("/", "\\")
        if not path:
            return
        try:
            self._ps("Remove-Item -Force -ErrorAction SilentlyContinue %s" % _q(path),
                     timeout=180)
        except Exception:
            pass
        parent = _wdir(path)
        if parent and _wbase(parent).lower().startswith("aidbm_"):
            try:
                self._ps("Remove-Item -Recurse -Force -ErrorAction SilentlyContinue %s"
                         % _q(parent), timeout=300)
            except Exception:
                pass

    # ---------------- 恢复 / 克隆 ----------------
    def _extract(self, archive: str, stage: str) -> str:
        """把 zip 解到暂存目录，返回解包后的 .vmx（找不到返回空串）。"""
        sz = self._sevenzip_path()
        script = (
            "$sz=%s; $out=%s; $arc=%s\n"
            "New-Item -ItemType Directory -Force -Path $out | Out-Null\n"
            "& $sz x -y ('-o' + $out) $arc\n"
            "Write-Output ('AIDBM_7ZA_RC=' + $LASTEXITCODE)\n"
            "$v = Get-ChildItem -LiteralPath $out -Filter *.vmx -Recurse -Depth 2 "
            "-ErrorAction SilentlyContinue | "
            "Where-Object { $_.Extension -eq '.vmx' } | Select-Object -First 1\n"
            "if($v){ Write-Output ('AIDBM_VMX=' + $v.FullName) }"
            % (_q(sz), _q(stage), _q(archive)))
        r = self._ps(script, timeout=int(self.extra.get("restore_timeout") or 21600))
        out = r.get("out") or ""
        m = re.search(r"AIDBM_VMX=(.+)", out)
        vmx = m.group(1).strip() if m else ""
        if not vmx:
            raise VMProviderError("备份产物中未找到 .vmx 配置文件，产物可能不完整：%s"
                                  % (out or r.get("err") or "")[:200])
        return vmx

    def restore_in_place(self, vm_ref: str, artifacts: list,
                         consistency: str = "crash"):
        if not artifacts:
            return False, "缺少备份产物"
        vmx = self._find_vmx(vm_ref)
        vmdir = _wdir(vmx)
        was_running = _norm(vmx) in {_norm(x) for x in self._running_vms()}
        stage = self._stage_dir("restore")
        try:
            staged_vmx = self._extract(artifacts[0], stage)
            if was_running:
                self._ps("& %s -T ws stop %s hard 2>&1" % (_q(self._vmrun_path()), _q(vmx)),
                         timeout=600)
            script = ("$src=%s; $dst=%s\n"
                      "$ErrorActionPreference='Stop'\n"
                      "# ① 先删掉「备份之后才产生」的文件（还原=回到备份时点，而不是叠加）\n"
                      "$keep=@{}\n"
                      "Get-ChildItem -LiteralPath $src -Recurse -File -Force -ErrorAction SilentlyContinue | "
                      "ForEach-Object { $rel=$_.FullName.Substring($src.Length).TrimStart('\\','/'); "
                      "$keep[$rel.ToLower()]=$true }\n"
                      "Get-ChildItem -LiteralPath $dst -Recurse -File -Force -ErrorAction SilentlyContinue | "
                      "ForEach-Object { $rel=$_.FullName.Substring($dst.Length).TrimStart('\\','/'); "
                      "if(-not $keep.ContainsKey($rel.ToLower())){ "
                      "if($rel -notmatch '\\.(vmem|vmss)$'){ "
                      "Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue; "
                      "Write-Output ('AIDBM_REMOVED ' + $rel) } } }\n"
                      "# ② 再用备份内容覆盖\n"
                      "Copy-Item -Path (Join-Path $src '*') -Destination $dst -Recurse -Force\n"
                      "Write-Output 'AIDBM_COPY_OK'"
                      % (_q(stage), _q(vmdir)))
            r = self._ps(script, timeout=int(self.extra.get("restore_timeout") or 21600))
            if "AIDBM_COPY_OK" not in (r.get("out") or ""):
                return False, ("还原覆盖失败: %s"
                               % (r.get("out") or r.get("err") or "")[:300])
            if was_running:
                self._spawn_start(vmx)
                self._wait_running(vmx, int(self.extra.get("start_wait_sec") or 90))
            return True, ("已从备份原位还原 VM「%s」%s"
                          % (_wbase(vmx),
                             "（原为运行态，已重新开机）" if was_running else "（原为关机态，保持关机）"))
        finally:
            try:
                self._ps("Remove-Item -Recurse -Force -ErrorAction SilentlyContinue %s"
                         % _q(stage), timeout=600)
            except Exception:
                pass

    def clone_to_new_vm(self, vm_ref: str, artifacts: list,
                        spec: CloneSpec) -> CloneResult:
        if not artifacts:
            return CloneResult(ok=False, message="缺少备份产物")
        src_vmx = self._find_vmx(vm_ref)
        src_dir = _wdir(src_vmx)
        new_name = spec.target_name or ("%s_clone_%s" % (_wbase(src_vmx)[:-4],
                                                          uuid.uuid4().hex[:6]))
        base_dir = spec.target_node or self.extra.get("clone_dir") or _wdir(src_dir)
        new_dir = os.path.join(base_dir, new_name)
        stage = self._stage_dir("clone")
        try:
            staged_vmx = self._extract(artifacts[0], stage)
            self._ps("New-Item -ItemType Directory -Force -Path %s | Out-Null"
                     % _q(new_dir), timeout=300)
            script = ("$src=%s; $dst=%s\n"
                      "$ErrorActionPreference='Stop'\n"
                      "Copy-Item -Path (Join-Path $src '*') -Destination $dst -Recurse -Force\n"
                      "Write-Output 'AIDBM_COPY_OK'"
                      % (_q(stage), _q(new_dir)))
            r = self._ps(script, timeout=int(self.extra.get("restore_timeout") or 21600))
            if "AIDBM_COPY_OK" not in (r.get("out") or ""):
                return CloneResult(ok=False, message="克隆目录创建失败: %s"
                                   % (r.get("out") or r.get("err") or "")[:300])
            # 改显示名 + 换 BIOS UUID（避免与源 VM 冲突）
            new_vmx = self._psv("Get-ChildItem -LiteralPath %s -Filter *.vmx "
                                "-ErrorAction SilentlyContinue | "
                                "Where-Object { $_.Extension -eq '.vmx' } | "
                                "Select-Object -First 1 | ForEach-Object { $_.FullName }"
                                % _q(new_dir), timeout=120)
            new_vmx = new_vmx[0].strip() if new_vmx else ""
            if new_vmx:
                self._ps("$f=%s; $u=([guid]::NewGuid()).ToString(); "
                         "$t=Get-Content -LiteralPath $f -Encoding UTF8; "
                         "$t = $t -replace '(?m)^displayName\\s*=.*$', ('displayName = \"%s\"' -f %s); "
                         "$t = $t -replace '(?m)^uuid\\.bios\\s*=.*$', ('uuid.bios = \"%s\"' -f $u); "
                         "$t = $t -replace '(?m)^uuid\\.location\\s*=.*$', ('uuid.location = \"%s\"' -f $u); "
                         "Set-Content -LiteralPath $f -Value $t -Encoding UTF8; "
                         "Write-Output 'AIDBM_VMX_PATCHED'"
                         % (_q(new_vmx), _q(new_name), _q(new_name)),
                         timeout=180)
            if spec.auto_start and new_vmx:
                self._spawn_start(new_vmx)
                self._wait_running(new_vmx, int(self.extra.get("start_wait_sec") or 90))
            return CloneResult(ok=True, target_ref=new_vmx, target_name=new_name,
                               ip="",
                               message="已从恢复点克隆为新 VM「%s」（目录 %s）%s"
                                       % (new_name, new_dir,
                                          "，已开机" if spec.auto_start else ""))
        finally:
            try:
                self._ps("Remove-Item -Recurse -Force -ErrorAction SilentlyContinue %s"
                         % _q(stage), timeout=600)
            except Exception:
                pass

    def delete_vm(self, ref: str, node: str = ""):
        try:
            vmx = self._find_vmx(ref)
        except Exception as e:
            return False, str(e)[:200]
        vmdir = _wdir(vmx)
        try:
            self._ps("& %s -T ws stop %s hard 2>&1" % (_q(self._vmrun_path()), _q(vmx)),
                     timeout=600)
        except Exception:
            pass
        r = self._ps("Remove-Item -Recurse -Force -ErrorAction SilentlyContinue %s"
                     % _q(vmdir), timeout=900)
        return True, "已删除虚拟机目录 %s" % vmdir

    # ---------------- 运行时 ----------------
    def power_state(self, vm_ref: str, node: str = "") -> str:
        try:
            vmx = self._find_vmx(vm_ref)
        except Exception:
            return "unknown"
        return "running" if _norm(vmx) in {_norm(x) for x in self._running_vms()} else "stopped"

    def _spawn_vmrun(self, vmx: str, args: list) -> None:
        """异步执行 vmrun 子命令（GUI 模式下 vmrun 不会返回，必须 Start-Process）。

        ArgumentList 用数组 + 双引号：单引号数组里再写单引号路径会被
        PowerShell 解析成空参数（表现为「参数为 Null 或空」）。
        """
        vr = self._vmrun_path()
        arg_ps = ", ".join(["'-T'", "'ws'"] + [_dq(a) for a in args])
        # 必须显式重定向子进程输出：否则子进程会继承 SSH 的 stdout 句柄，
        # 导致 SSH 侧一直等不到 EOF（表现为「命令卡住不返回」）。
        tag = uuid.uuid4().hex[:8]
        so = "%s/aidbm_vmrun_%s.out" % (self._temp_base(), tag)
        se = "%s/aidbm_vmrun_%s.err" % (self._temp_base(), tag)
        self._ps("$al = @(%s); "
                 "$p = Start-Process -FilePath %s -ArgumentList $al "
                 "-WindowStyle Hidden -RedirectStandardOutput %s "
                 "-RedirectStandardError %s -PassThru; "
                 "Write-Output ('AIDBM_SPAWNED=' + $p.Id)"
                 % (arg_ps, _q(vr), _q(so), _q(se)),
                 timeout=120)

    def _spawn_start(self, vmx: str) -> None:
        """异步拉起虚拟机（挂起态 start = 恢复运行）。"""
        self._spawn_vmrun(vmx, ["start", vmx])

    def _spawn_suspend(self, vmx: str) -> None:
        """异步挂起虚拟机（软挂起：释放磁盘句柄，业务短暂冻结）。"""
        self._spawn_vmrun(vmx, ["suspend", vmx, "soft"])

    def _wait_running(self, vmx: str, wait_sec: int = 90) -> bool:
        for _ in range(max(1, wait_sec // 5)):
            if _norm(vmx) in {_norm(x) for x in self._running_vms()}:
                return True
            try:
                self._ps("Start-Sleep -Seconds 5", timeout=30)
            except Exception:
                break
        return _norm(vmx) in {_norm(x) for x in self._running_vms()}

    def start_vm(self, ref: str, node: str = ""):
        """开机：vmrun 的 GUI 模式会**一直阻塞**（VM 由 Workstation GUI 托管），
        所以这里用 Start-Process 异步拉起，再用 `vmrun list` 轮询确认真的起来了。
        """
        vmx = self._find_vmx(ref)
        self._spawn_start(vmx)
        if self._wait_running(vmx, int(self.extra.get("start_wait_sec") or 90)):
            return True, "开机成功（vmrun list 已确认运行中）"
        return False, "已下发开机指令，但在超时窗口内未在 vmrun list 中看到该虚拟机"

    def stop_vm(self, ref: str, node: str = "", force: bool = False):
        vmx = self._find_vmx(ref)
        r = self._ps("& %s -T ws stop %s %s 2>&1"
                     % (_q(self._vmrun_path()), _q(vmx), "hard" if force else "soft"),
                     timeout=900)
        out = (r.get("out") or "") + (r.get("err") or "")
        ok = "Error" not in out and "error" not in out.lower()
        return (ok, "关机成功" if ok else out[:200])

    def guest_ip(self, vm_ref: str, node: str = "") -> str:
        """Workstation 只能靠 VMware Tools 回报 IP，拿不到如实返回空串。"""
        try:
            vmx = self._find_vmx(vm_ref)
        except Exception:
            return ""
        lines = self._psv("& %s -T ws getGuestIPAddress %s 2>&1"
                          % (_q(self._vmrun_path()), _q(vmx)), timeout=120)
        for ln in lines:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", ln)
            if m:
                return m.group(1)
        return ""

    # ---------------- 快照 ----------------
    def create_snapshot(self, vm_ref: str, name: str,
                        consistency: str = "crash") -> SnapshotRef:
        vmx = self._find_vmx(vm_ref)
        r = self._ps("& %s -T ws snapshot %s %s 2>&1"
                     % (_q(self._vmrun_path()), _q(vmx), _q(name)), timeout=1800)
        out = (r.get("out") or "") + (r.get("err") or "")
        if "Error" in out:
            raise VMProviderError("Workstation 创建快照失败: %s" % out[:200])
        return SnapshotRef(id=name, name=name)

    def delete_snapshot(self, vm_ref: str, snap: SnapshotRef) -> None:
        vmx = self._find_vmx(vm_ref)
        self._ps("& %s -T ws deleteSnapshot %s %s 2>&1"
                 % (_q(self._vmrun_path()), _q(vmx), _q(snap.name or snap.id)),
                 timeout=1800)
