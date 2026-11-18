# -*- coding: utf-8 -*-
"""NAS 存储（群晖 DSM）回归：命名、分类、恢复回推路径语义。

背景（2026-11-13）：
1. 该通道原名为「群晖 NAS（DSM）」，实际能力是 NAS 存储备份，改名并对齐 config 显示名；
2. 原 restore 只解包到平台侧目录，无法回到 NAS（消息里自认 Upload 未实现）；
   现已补齐 FileStation.Upload + 回读大小校验；
3. 回推路径曾把共享根前缀带进恢复目录（/restore/backup_src/docs/a.txt），
   已在 _rel_under_root 中按「单根去前缀、多根保留」处理。
"""
import config
from core.engines import engine_meta_map
from core.engines.synology import SynologyEngine, _rel_under_root


def test_display_name_is_nas_storage():
    assert SynologyEngine.display_name == "NAS 存储（群晖 DSM）"
    assert config.DB_DISPLAY_NAMES.get("synology") == "NAS 存储（群晖 DSM）"


def test_engine_meta_marks_storage_category():
    meta = engine_meta_map().get("synology") or {}
    assert meta.get("category") == "storage"
    assert "恢复" in (meta.get("description") or "")


def test_restore_rel_strips_single_root():
    assert _rel_under_root("backup_src/docs/a.txt", ["/backup_src"]) == "docs/a.txt"
    assert _rel_under_root("backup_src/img/b.bin", ["/backup_src/"]) == "img/b.bin"


def test_restore_rel_keeps_full_path_for_multi_roots():
    rel = "shareA/docs/a.txt"
    assert _rel_under_root(rel, ["/shareA", "/shareB"]) == rel


def test_restore_rel_handles_root_file():
    assert _rel_under_root("backup_src/readme.txt", ["/backup_src"]) == "readme.txt"


def test_restore_without_nas_path_stays_local(monkeypatch, tmp_path):
    """未指定 DSM 恢复路径时保持旧语义（只解包到平台侧），不能报错。"""
    import json
    import tarfile

    pkg = tmp_path / "b.tar.gz"
    man = {"files": [{"path": "/backup_src/docs/a.txt", "sha256": ""}],
           "deleted": [], "parent": None}
    # 包内结构与生产一致：files/<DSM 绝对路径去掉前导 />/...
    payload = tmp_path / "pkg_files" / "backup_src" / "docs"
    payload.mkdir(parents=True)
    (payload / "a.txt").write_text("x", encoding="utf-8")
    with tarfile.open(pkg, "w:gz") as tar:
        tar.add(str(tmp_path / "pkg_files"), arcname="files")

    eng = SynologyEngine.__new__(SynologyEngine)
    eng.task = {"id": 1, "name": "t", "db_name": "/backup_src",
                "host": "127.0.0.1", "port": 5000, "username": "u",
                "password": "p", "extra_options": "{}"}
    eng.storage_root = str(tmp_path)
    eng.task_name = "t"
    eng.task_id = 1
    eng.logger = None
    eng.cfg = eng._cfg()
    eng._load_state = lambda: {"chain": [], "snapshot": {}}
    eng._read_manifest = lambda p: man
    eng._extract = lambda p, t: tarfile.open(p).extractall(t)

    # 只校验「不走 NAS 时不触碰 DSMClient」：此处把客户端换成会爆炸的桩
    import core.engines.synology as syn
    class _Boom:
        def __init__(self, **kw):
            raise AssertionError("未指定 DSM 路径时不应建立 DSM 连接")
    monkeypatch.setattr(syn, "DSMClient", _Boom)
    res = eng.restore(str(pkg), target_path=str(tmp_path / "local"))
    assert res.success, res.message
    assert "未指定 DSM 恢复路径" in res.message
