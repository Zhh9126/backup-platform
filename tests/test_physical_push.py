# -*- coding: utf-8 -*-
"""MySQL 物理备份远端推送（xtrabackup 免安装）回归测试——离线可跑。

背景（用户现场缺陷）：远端 glibc 过低时，ldd 输出
``/tmp/bk_pushed_xb_xxx: /lib64/ld-linux...: version `GLIBC_2.28' not found``
被旧解析器当成了「缺失库名」，报出「远端缺失动态库 /tmp/bk_pushed_xb_...:」
这种让用户摸不着头脑的消息。本轮修复：分类器区分「可推补库」与「致命不兼容」，
并新增架构预检与三级远程版本探测（5.7 实例不再误推 xtrabackup 8.0）。
"""
import shlex
from types import SimpleNamespace

import pytest

import core.engines.file as fe
from core.engines.mysql import MySQLEngine  # noqa: F401  (导入冒烟)
from core.engines import mysql as m


# ------------------------- _remote_ldd_missing 分类器 -------------------------

class _StubPipe:
    """替换 core.engines.file._ssh_exec_pipe，按脚本返回 (out, err, rc)。"""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, client, shell, timeout=None, input_data=None):
        self.calls.append(shell)
        for key, val in self.script.items():
            if key in shell:
                out, err, rc = val
                return (out.encode() if isinstance(out, str) else out,
                        err.encode() if isinstance(err, str) else err, rc)
        return (b"", b"", 0)


@pytest.fixture()
def patch_pipe(monkeypatch):
    def _install(script):
        stub = _StubPipe(script)
        monkeypatch.setattr(fe, "_ssh_exec_pipe", stub)
        return stub
    return _install


BIN = "/tmp/bk_pushed_xb_123"


def test_ldd_standard_missing_lib():
    stub = patch_pipe_fixture_missing()
    missing, fatal = m.MySQLEngine._remote_ldd_missing(None, BIN)
    assert missing == ["libev.so.4"] and fatal == []


def patch_pipe_fixture_missing():
    s = _StubPipe({BIN: (
        "\tlinux-vdso.so.1 =>  (0x00007fff...)\n"
        f"\tlibev.so.4 => not found\n"
        "\tlibcrypto.so.1.1 => /lib64/libcrypto.so.1.1 (0x...)\n", "", 0)})
    fe._ssh_exec_pipe = s
    return s


def test_ldd_glibc_too_old_is_fatal_not_missing_lib():
    """用户现场回归：GLIBC 版本行不得被解析成「缺失库」路径。"""
    fe._ssh_exec_pipe = _StubPipe({BIN: (
        f"{BIN}: /lib64/ld-linux-x86-64.so.2: version `GLIBC_2.28' not found "
        f"(required by {BIN})\n", "", 1)})
    missing, fatal = m.MySQLEngine._remote_ldd_missing(None, BIN)
    assert missing == []
    assert len(fatal) == 1 and "GLIBC_2.28" in fatal[0]


def test_ldd_bad_interpreter_is_fatal():
    fe._ssh_exec_pipe = _StubPipe({BIN: (
        f"-bash: {BIN}: /lib64/ld-linux-x86-64.so.2: bad ELF interpreter: "
        "No such file or directory\n", "", 126)})
    missing, fatal = m.MySQLEngine._remote_ldd_missing(None, BIN)
    assert missing == [] and fatal


def test_ldd_static_binary_ok():
    fe._ssh_exec_pipe = _StubPipe({BIN: ("not a dynamic executable\n", "", 1)})
    missing, fatal = m.MySQLEngine._remote_ldd_missing(None, BIN)
    assert missing == [] and fatal == []


# ------------------------- 架构检测 -------------------------

def test_elf_arch_x86_64():
    # 平台上必然存在的 ELF 文件
    import shutil
    elf = shutil.which("sh") or "/bin/sh"
    arch = m.MySQLEngine._elf_arch(elf)
    assert arch in ("x86_64", "aarch64", "i686")


# ------------------------- 版本选型 -------------------------

def test_pick_physical_bin_mysql57():
    path, label = m.MySQLEngine._pick_physical_bin("5.7.44-log")
    assert label == "xtrabackup 2.4" and "xtrabackup24" in path


def test_pick_physical_bin_mysql8():
    path, label = m.MySQLEngine._pick_physical_bin("8.0.35")
    assert label == "xtrabackup 8.0"


def test_pick_physical_bin_mariadb():
    path, label = m.MySQLEngine._pick_physical_bin("10.11.6-MariaDB")
    assert label == "mariabackup"


# ------------------------- 远程版本探测（stub） -------------------------

def _mk_engine(tmp_path):
    """最小化 engine 实例（绕开完整任务构造）。"""
    eng = object.__new__(m.MySQLEngine)
    eng.task = {"host": "127.0.0.1", "port": 3306, "username": "root",
                "password": ""}
    eng.logger = __import__("logging").getLogger("test")
    eng.task_name = "t"
    return eng


def test_remote_version_via_mysqld_in_common_dir(monkeypatch):
    """远端 mysqld 在非 PATH 目录（/usr/local/mysql/bin）：旧实现 command -v
    探不到返回空 → 误推 8.0；新实现走 _resolve_remote_bin 能找到。"""
    eng = _mk_engine(None)
    rd = __import__("core.remote_dump", fromlist=["remote_dump"])
    monkeypatch.setattr(rd, "_resolve_remote_bin",
                        lambda client, tool: "/usr/local/mysql/bin/mysqld")
    fe._ssh_exec_pipe = _StubPipe({
        "/usr/local/mysql/bin/mysqld": (
            "mysqld  Ver 5.7.44 for linux-glibc2.12 on x86_64 (MySQL Community Server)\n",
            "", 0)})
    ver = eng._remote_server_version(client=None)
    assert "5.7.44" in ver


def test_remote_version_fallback_via_client(monkeypatch, tmp_path):
    """远端无服务二进制（docker 部署）→ 推 mysql 客户端连 127.0.0.1 查版本。"""
    eng = _mk_engine(None)
    rd = __import__("core.remote_dump", fromlist=["remote_dump"])
    monkeypatch.setattr(rd, "_resolve_remote_bin", lambda client, tool: None)
    # 伪造本地 mysql 客户端
    fake_cli = tmp_path / "mysql"
    fake_cli.write_bytes(b"\x7fELF fake")
    monkeypatch.setattr(eng, "_resolve_local_tool", lambda tool: str(fake_cli))

    class _FakeSftp:
        def put(self, a, b):
            self.pushed = (a, b)

        def chmod(self, *a):
            pass

        def close(self):
            pass

    pushed = {}

    class _FakeClient:
        def open_sftp(self):
            s = _FakeSftp()
            pushed["sftp"] = s
            return s

    stub = _StubPipe({"SELECT VERSION()": ("5.7.44-log\n", "", 0)})
    monkeypatch.setattr(fe, "_ssh_exec_pipe", stub)
    ver = eng._remote_version_via_client(client=_FakeClient())
    assert ver == "5.7.44-log"
    assert pushed["sftp"].pushed[1].startswith("/tmp/bk_mysql_cli_")
    # 零残留：命令里带 rm -f
    assert "rm -f" in stub.calls[0]
    # 凭据走 MYSQL_PWD + --no-defaults（不进 argv 明文、不被 my.cnf 干扰）
    assert "MYSQL_PWD=" in stub.calls[0] and "--no-defaults" in stub.calls[0]


# ----------------- openEuler 场景回归（用户现场） -----------------

def test_ldd_openeuler_openssl_version_missing_is_pushable():
    """openEuler：/lib64/libssl.so.10 存在但版本不符 → 缺失库 libssl.so.10
    （可由平台内置库推送解决），而非致命。"""
    fe._ssh_exec_pipe = _StubPipe({BIN: (
        f"{BIN}: /lib64/libssl.so.10: version `libssl.so.10' not found "
        f"(required by {BIN})\n"
        f"{BIN}: /lib64/libcrypto.so.10: version `libcrypto.so.10' not found "
        f"(required by {BIN})\n", "", 1)})
    missing, fatal = m.MySQLEngine._remote_ldd_missing(None, BIN)
    assert sorted(missing) == ["libcrypto.so.10", "libssl.so.10"]
    assert fatal == []


def test_ldd_glibc_version_still_fatal():
    fe._ssh_exec_pipe = _StubPipe({BIN: (
        f"{BIN}: /lib64/ld-linux-x86-64.so.2: version `GLIBC_2.28' not found "
        f"(required by {BIN})\n", "", 1)})
    missing, fatal = m.MySQLEngine._remote_ldd_missing(None, BIN)
    assert missing == [] and len(fatal) == 1 and "GLIBC_2.28" in fatal[0]


def test_local_lib_map_bundled_fallback(tmp_path, monkeypatch):
    """本机 ldd 没有的库名 → 从镜像内置库目录 /opt/xtrabackup_libs 补。"""
    fake_dir = tmp_path / "xtrabackup_libs"
    fake_dir.mkdir()
    (fake_dir / "libssl.so.10").write_bytes(b"x" * 8)
    (fake_dir / "libcrypto.so.10").write_bytes(b"y" * 8)
    monkeypatch.setenv("XB_BUNDLED_LIB_DIR", str(fake_dir))
    eng = object.__new__(m.MySQLEngine)
    eng.task = {}
    monkeypatch.setattr(m.MySQLEngine, "_task_tool_path", lambda self: "", raising=False)
    # 隔离真实镜像目录
    import subprocess as _sp
    real_run = _sp.run
    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "bin: not a dynamic executable\n"
            stderr = ""
        return R()
    monkeypatch.setattr(_sp, "run", fake_run)
    mapping = m.MySQLEngine._local_lib_map(eng.__class__ and "/fake/xb")
    assert mapping.get("libssl.so.10", "").endswith("libssl.so.10")
