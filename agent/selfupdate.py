# -*- coding: utf-8 -*-
"""Agent 自更新：装一次即可长期跟随平台版本。

用户口径（2026-11-26）：「**只安装一次**，所有功能都支持，不区分类型」。
因此 Agent 装好之后不应再因为平台升级而要求用户重新安装——本模块让
Agent 自行到平台比对清单，版本变化即下载、校验、原子替换并重启。

设计约束（与 Agent 其余代码一致）：

- **纯标准库**（urllib / tarfile / hashlib），目标机零额外依赖、离线可用；
- 失败**永不拖垮主服务**：任何异常都只记录，不影响备份/恢复通道；
- **不谎报成功**：替换失败、缺文件、校验不符一律中止并保留原文件；
- 重启失败时如实标记「待重启生效」，由平台下一次探活如实展示。

## 启用方式

安装脚本会写入 ``/etc/aidbm-agent/agent.env``：

    AIDBM_PLATFORM_URL=http://<平台地址>:8080
    AIDBM_AGENT_TOKEN=<注册令牌>
    AIDBM_AGENT_SELFUPDATE=1

未设置 ``AIDBM_PLATFORM_URL`` 或 ``AIDBM_AGENT_SELFUPDATE=0`` 时本模块不启动
（离线单机部署、审计要求固定版本时可显式关闭）。
"""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request

VERSION = "1.0.0"

STATE_FILE = "/opt/aidbm-agent/.selfupdate.json"
REQUIRED_FILES = ("backup_agent.py", "plugins.py", "iso9660.py")
DEFAULT_INTERVAL = 3600          # 默认 1 小时比一次
MIN_INTERVAL = 300


def _log(msg):
    sys.stderr.write("[agent-selfupdate] %s\n" % msg)
    sys.stderr.flush()


def _env_int(name, default):
    try:
        return int(str(os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _install_dir():
    """Agent 安装目录（源码模式下即本文件所在目录）。"""
    return os.path.dirname(os.path.abspath(__file__))


def _read_state(path=None):
    try:
        with open(path or STATE_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(data, path=None):
    target = path or STATE_FILE
    try:
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return True
    except Exception as exc:
        _log("写状态文件失败: %s" % exc)
        return False


def _http_get(url, token, timeout=60):
    req = urllib.request.Request(url)
    req.add_header("X-Aidbm-Agent-Token", token or "")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


class SelfUpdater(object):
    """自更新线程：比对 → 下载 → 校验 → 原子替换 → 重启。"""

    def __init__(self, platform_url="", token="", interval=DEFAULT_INTERVAL,
                 state_file=None, install_dir=None):
        self.platform_url = (platform_url or "").rstrip("/")
        self.token = token or ""
        self.interval = max(MIN_INTERVAL, int(interval or DEFAULT_INTERVAL))
        self.state_file = state_file or STATE_FILE
        self.install_dir = install_dir or _install_dir()
        self.stop_event = threading.Event()

    # ---- 平台交互 ----
    def manifest(self):
        url = "%s/api/v1/agents/manifest?token=%s" % (self.platform_url, self.token)
        raw = _http_get(url, self.token, timeout=30)
        data = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(data, dict) or not data.get("sha256"):
            raise ValueError("清单内容无效")
        return data

    def download(self, expect_sha):
        url = "%s/api/v1/agents/download?token=%s" % (self.platform_url, self.token)
        data = _http_get(url, self.token, timeout=180)
        got = _sha256(data)
        if got != expect_sha:
            raise ValueError("下载包摘要不符（期望 %s，实际 %s）" % (expect_sha, got))
        return data

    # ---- 本地替换 ----
    def _apply(self, blob):
        tmpdir = tempfile.mkdtemp(prefix="aidbm-selfupdate-")
        try:
            archive = os.path.join(tmpdir, "agent.tar.gz")
            with open(archive, "wb") as fp:
                fp.write(blob)
            unpack = os.path.join(tmpdir, "unpack")
            os.makedirs(unpack, exist_ok=True)
            with tarfile.open(archive, "r:gz") as tar:
                for member in tar.getmembers():
                    # 只接受普通文件，且必须落在包根目录（防目录穿越）
                    if not member.isfile():
                        continue
                    name = os.path.basename(member.name)
                    if name not in REQUIRED_FILES:
                        continue
                    if member.name != name:
                        continue
                    src = tar.extractfile(member)
                    if src is None:
                        continue
                    with open(os.path.join(unpack, name), "wb") as out:
                        shutil.copyfileobj(src, out)
            missing = [n for n in REQUIRED_FILES
                       if not os.path.isfile(os.path.join(unpack, n))]
            if missing:
                raise ValueError("发布包缺少文件: %s" % ", ".join(missing))
            # 原子替换：先写临时文件再 os.replace（同目录同分区）
            for name in REQUIRED_FILES:
                src = os.path.join(unpack, name)
                dst = os.path.join(self.install_dir, name)
                tmp = dst + ".new"
                shutil.copyfile(src, tmp)
                os.chmod(tmp, 0o600)
                os.replace(tmp, dst)
            return True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 重启 ----
    def _restart(self):
        """尽力重启服务；失败则如实标记「待重启生效」，不假装已生效。"""
        try:
            # 优先用 systemd 定时器把重启排到本进程之外（避免自停自启竞态）
            subprocess.run(
                ["systemd-run", "--on-active=5", "--unit=aidbm-agent-selfupdate",
                 "systemctl", "restart", "aidbm-agent"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15)
            return True, "已排定 5 秒后重启"
        except Exception:
            pass
        try:
            # 兜底：脱离会话的子进程延迟重启（忽略 SIGTERM，避免被 cgroup 一起收走）
            def _child():
                try:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                except Exception:
                    pass
                time.sleep(6)
                try:
                    subprocess.run(["systemctl", "restart", "aidbm-agent"],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=60)
                except Exception:
                    pass
            subprocess.Popen([sys.executable, "-c",
                              "import os,signal,sys,time,subprocess;"
                              "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                              "time.sleep(6);"
                              "subprocess.run(['systemctl','restart','aidbm-agent'])"
                              ], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "已排定延迟重启"
        except Exception as exc:
            return False, "无法自动重启（%s），需人工重启 aidbm-agent" % exc

    # ---- 主循环 ----
    def run_once(self):
        """执行一次检查；返回 (状态, 说明)。状态：updated / current / error。"""
        if not self.platform_url or not self.token:
            return "error", "未配置平台地址或令牌，自更新未启用"
        try:
            manifest = self.manifest()
        except Exception as exc:
            return "error", "读取平台清单失败: %s" % str(exc)[:160]
        want = manifest.get("sha256") or ""
        state = _read_state(self.state_file)
        have = state.get("sha256") or ""
        if want and want == have:
            return "current", "已是最新版本 %s" % (manifest.get("version") or want[:12])
        try:
            blob = self.download(want)
            self._apply(blob)
        except Exception as exc:
            return "error", "更新失败（保留原文件）: %s" % str(exc)[:160]
        ok, note = self._restart()
        new_state = {"sha256": want, "version": manifest.get("version") or "",
                     "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "platform_version": manifest.get("platform_version") or "",
                     "pending_restart": not ok}
        _write_state(new_state, self.state_file)
        if not ok:
            return "updated", "新版本已写入但未重启生效：%s" % note
        return "updated", "已更新到 %s，%s" % (manifest.get("version") or want[:12], note)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                status, note = self.run_once()
                if status != "current":
                    _log("%s: %s" % (status, note))
            except Exception as exc:  # noqa: BLE001
                _log("自更新循环异常: %s" % str(exc)[:160])
            self.stop_event.wait(self.interval)

    def start(self):
        thread = threading.Thread(target=self.loop, name="aidbm-selfupdate",
                                  daemon=True)
        thread.start()
        return thread


def start_from_env():
    """按环境变量启动自更新线程；未启用时返回 None。"""
    if str(os.environ.get("AIDBM_AGENT_SELFUPDATE") or "").strip() in ("0", "false", "no"):
        _log("自更新已显式关闭")
        return None
    platform_url = (os.environ.get("AIDBM_PLATFORM_URL") or "").strip()
    token = (os.environ.get("AIDBM_AGENT_TOKEN") or "").strip()
    if not platform_url or not token:
        _log("未配置 AIDBM_PLATFORM_URL / AIDBM_AGENT_TOKEN，自更新未启用")
        return None
    updater = SelfUpdater(platform_url=platform_url, token=token,
                           interval=_env_int("AIDBM_AGENT_SELFUPDATE_INTERVAL",
                                             DEFAULT_INTERVAL))
    _log("自更新已启用：平台 %s，周期 %ds" % (platform_url, updater.interval))
    return updater.start()
