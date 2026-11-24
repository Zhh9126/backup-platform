# -*- coding: utf-8 -*-
"BMR 可启动介质 / 应用一致性钩子库 / 高负载自适应位图 契约回归。"
import os
import sys
import inspect
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "agent"))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

import plugins  # noqa: E402
from core import source_guard  # noqa: E402


def test_bmr_build_boot_media_contract():
    """BMR 可启动 ISO：内置 ISO9660 生成器产出 ISO+sha256，缺镜像如实失败。"""
    import tempfile
    bmr = plugins.PLUGIN_MAP["bmr"]
    assert bmr["version"] == "1.3.0"
    assert "build_boot_media" in bmr["actions"]
    src = inspect.getsource(plugins._bmr_build_boot_media)
    assert "grub2-mkimage" in src and "_iso9660" in src
    assert "sha256" in src
    check = plugins.invoke("bmr", "boot_media_check", {}, None)
    assert check["native_iso_generator"], "内置 ISO 生成器必须可用"
    # 无镜像时诚实失败
    r = plugins.invoke("bmr", "build_boot_media",
                       {"image": "/nonexistent", "out_iso": "/tmp/x.iso"}, None)
    assert not r["ok"]


def test_bmr_builtin_iso_real_build():
    """真生成可启动 ISO：解析 PVD/El Torito，读回引导镜像字节一致。"""
    import struct
    import tempfile
    import shutil
    import iso9660 as iso
    base = tempfile.mkdtemp(prefix="aidbm_bmr_iso_")
    try:
        seed = os.path.join(base, "seed")
        os.makedirs(os.path.join(seed, "boot"))
        with open(os.path.join(seed, "boot", "grub_boot.bin"), "wb") as f:
            f.write(b"AIDBM-BOOT" * 64)
        out = os.path.join(base, "rescue.iso")
        result = iso.build_iso_from_dir(seed, out, boot_rel="boot/grub_boot.bin")
        assert result["ok"] and os.path.getsize(out) > 2048 * 20
        with open(out, "rb") as f:
            f.seek(16 * 2048)
            pvd = f.read(2048)
            assert pvd[0] == 1 and pvd[1:6] == b"CD001"
            f.seek(17 * 2048)
            br = f.read(2048)
            assert b"EL TORITO SPECIFICATION" in br[7:39]
            cat_lba = struct.unpack_from("<I", br, 39)[0]
            f.seek(cat_lba * 2048)
            cat = f.read(2048)
            assert cat[32] == 0x88
            assert sum(struct.unpack_from("<H", cat, i)[0]
                       for i in range(0, 32, 2)) & 0xFFFF == 0
            boot_lba = struct.unpack_from("<I", cat, 40)[0]
            f.seek(boot_lba * 2048)
            assert f.read(len(b"AIDBM-BOOT" * 64)) == b"AIDBM-BOOT" * 64
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_app_hooks_presets():
    """应用一致性钩子库：内置 nginx/redis/mysql/pg/tomcat 等 preset。"""
    hooks = plugins.invoke("app-dr", "list_hooks", {}, None)
    assert hooks["ok"]
    names = {h["name"] for h in hooks["hooks"]}
    assert {"nginx", "redis", "mysql", "postgresql", "tomcat",
            "dameng", "generic"}.issubset(names)
    # preset 模式不要求手写命令
    with mock.patch.object(plugins, "_run",
                           return_value={"rc": 0, "stdout": "OK", "stderr": ""}):
        r = plugins.invoke("app-dr", "freeze", {"preset": "redis"}, None)
        assert r["ok"] and r["frozen"] and r["command_source"] == "preset:redis"
        r = plugins.invoke("app-dr", "thaw", {"preset": "redis"}, None)
        assert r["ok"] and r["thawed"]
    assert plugins.PLUGIN_MAP["app-dr"]["version"] == "1.2.0"


def test_source_guard_cdp_mode_config():
    """高负载自适应：配置含 cdp_mode 开关，evaluate 返回 cdp_bitmap_mode。"""
    cfg = source_guard.get_config()
    assert "source_guard_cdp_mode" in cfg
    src = inspect.getsource(source_guard.evaluate)
    assert "cdp_bitmap_mode" in src
    assert "block-cdp" in src
    assert "无纳管 Agent" in src or "Agent 能力探测失败" in src
    # 低负载时不应触发位图模式（保持 ok）
    with mock.patch.object(source_guard, "sample_cpu",
                           return_value={"cpu_pct": 10.0}):
        v = source_guard.evaluate({"host": "1.2.3.4"}, force=True)
        assert v["level"] == "ok"


def test_throttle_branch_exposes_cdp_reason():
    """throttle 分支必须如实携带 cdp_mode_reason（不虚报 CBT）。"""
    src = inspect.getsource(source_guard.evaluate)
    assert '"cdp_mode_reason": cdp_reason' in src
    assert 'cdp_mode = any(' in src
