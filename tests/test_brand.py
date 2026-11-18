# -*- coding: utf-8 -*-
"""品牌设置（core/brand.py）单元测试：文件落盘、魔数校验、文字长度、默认回落。"""
import os
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from core import brand as brand_mod  # noqa: E402
from core import db  # noqa: E402


def _png(w=8, h=8):
    raw = b"".join(b"\x00" + b"\x0d\x94\x88" * w for _ in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def setup_module(module):
    module._tmp = tempfile.mkdtemp(prefix="brand_test_")
    module._old_instance = config.INSTANCE_DIR
    config.INSTANCE_DIR = Path(module._tmp)


def teardown_module(module):
    config.INSTANCE_DIR = module._old_instance
    shutil.rmtree(module._tmp, ignore_errors=True)


def test_defaults_when_unset():
    b = brand_mod.get_brand()
    assert b["name"] == brand_mod.DEFAULTS["name"]
    assert b["title"] == brand_mod.DEFAULTS["title"]
    assert b["login_logo"] == "" and b["has_login_logo"] is False


def test_upload_png_and_serve():
    res = brand_mod.save_logo("login", _png())
    assert res["ok"] and res["ext"] == "png"
    p = brand_mod.asset_file("login")
    assert p and p.is_file() and p.read_bytes().startswith(b"\x89PNG")
    url = brand_mod.asset_url("login")
    assert url.startswith("/brand/asset/login?v=")
    assert brand_mod.asset_mime("login") == "image/png"
    assert brand_mod.get_brand()["has_login_logo"] is True


def test_reject_bad_inputs():
    import pytest
    with pytest.raises(ValueError):
        brand_mod.save_logo("login", b"")
    with pytest.raises(ValueError):
        brand_mod.save_logo("nowhere", _png())
    with pytest.raises(ValueError):
        brand_mod.save_logo("login", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    with pytest.raises(ValueError):
        brand_mod.save_logo("login", b"\x89PNG\r\n\x1a\n" + b"\x00" * (3 * 1024 * 1024))


def test_text_roundtrip_and_reset():
    brand_mod.save_text(name="ACME 数据守护", title="ACME 运维中心")
    b = brand_mod.get_brand()
    assert b["name"] == "ACME 数据守护" and b["title"] == "ACME 运维中心"
    brand_mod.reset_text()
    b = brand_mod.get_brand()
    assert b["name"] == brand_mod.DEFAULTS["name"]
    assert b["title"] == brand_mod.DEFAULTS["title"]


def test_text_length_limit():
    import pytest
    with pytest.raises(ValueError):
        brand_mod.save_text(name="字" * (brand_mod.MAX_TEXT_LEN + 1))


def test_reset_logo_removes_file():
    brand_mod.save_logo("sidebar", _png())
    assert brand_mod.asset_file("sidebar") is not None
    res = brand_mod.reset_logo("sidebar")
    assert res["ok"]
    assert brand_mod.asset_file("sidebar") is None
    assert brand_mod.asset_url("sidebar") == ""
