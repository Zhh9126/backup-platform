# -*- coding: utf-8 -*-
"""克隆服务「数据量展示 + 容量视图」单测（纯逻辑，不写正式库）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.db as db  # noqa: E402
from core import clone_preflight  # noqa: E402

FAILURES = []


def ck(name, cond, detail=""):
    if cond:
        print("  PASS  " + name)
    else:
        FAILURES.append(name)
        print("  FAIL  " + name + ((" | " + str(detail)[:200]) if detail else ""))


def test_estimate_sizes():
    print("=== 容量估算 ===")
    rec = {"backup_path": "/tmp/aidbm_test_est.sql", "original_size_bytes": 1024 * 1024 * 100}
    est = clone_preflight.estimate_clone_size(rec, clone_mode="logical",
                                              target_host="127.0.0.1")
    ck("逻辑克隆给出逻辑数据量", est["estimated_data_bytes"] == 100 * 1024 * 1024,
       est.get("estimated_data_human"))
    ck("逻辑克隆给出目标端占用（含膨胀系数）",
       est["estimated_disk_bytes"] == int(100 * 1024 * 1024 * 1.3),
       est.get("estimated_disk_human"))
    ck("给出源产物大小口径", est["source_artifact_human"] != "-", est)
    ck("给出源库原始体量", est["source_original_human"] != "-", est)
    ck("本机目标给出可用空间", bool(est["target_free_human"]), est.get("target_free_human"))

    est2 = clone_preflight.estimate_clone_size(rec, clone_mode="template",
                                               target_host="192.168.1.1")
    ck("活库克隆(COW)目标占用≈0", est2["estimated_disk_bytes"] == 0,
       est2.get("estimated_disk_human"))
    ck("远端目标空间无法探测时不猜", not est2.get("target_free_human"), est2)


def test_decorate_sizes():
    print("=== 派生字段（数据量口径 / 有效期） ===")
    from core.clone_service import CloneService
    row = {"source_size_bytes": 2048, "source_original_size_bytes": 4096,
           "data_size_bytes": 1024, "data_tables": 3, "data_rows": 100,
           "expires_at": "2099-01-01 00:00:00"}
    out = CloneService._decorate_sizes(dict(row))
    ck("源产物大小可读化", out["source_size_human"] not in ("", "0 B"), out)
    ck("源库原始体量可读化", out["source_original_size_human"] not in ("", "0 B"), out)
    ck("实测数据量可读化", out["data_size_human"] not in ("", "0 B"), out)
    ck("有效期倒计时为正", out["ttl_hours"] and out["ttl_hours"] > 0, out.get("ttl_hours"))
    ck("未过期标记为 False", out["expired"] is False, out)

    past = CloneService._decorate_sizes({"expires_at": "2000-01-01 00:00:00"})
    ck("过期克隆标记 expired", past["expired"] is True, past)
    ck("过期克隆倒计时为负", (past["ttl_hours"] or 0) < 0, past.get("ttl_hours"))

    none_exp = CloneService._decorate_sizes({"expires_at": None})
    ck("无到期时间不误判过期", none_exp["expired"] is False, none_exp)
    ck("无到期时间倒计时为 None", none_exp["ttl_hours"] is None, none_exp)


def test_preflight_includes_estimate():
    print("=== 预检返回容量视图 ===")
    rec = {"status": "success", "db_type": "mysql",
           "backup_path": "/tmp/aidbm_test_est.sql",
           "original_size_bytes": 1024}
    pf = clone_preflight.preflight(rec, target_host="127.0.0.1")
    ck("预检返回 estimate 段", isinstance(pf.get("estimate"), dict), pf.get("estimate"))
    ck("estimate 含逻辑数据量", "estimated_data_human" in (pf.get("estimate") or {}),
       pf.get("estimate"))
    ck("检查项含 data_size", any(c.get("item") == "data_size"
                                 for c in pf.get("checks") or []),
       [c.get("item") for c in pf.get("checks") or []])


if __name__ == "__main__":
    test_estimate_sizes()
    test_decorate_sizes()
    test_preflight_includes_estimate()
    print("\n=== 小结 === 失败 %d 项 ===" % len(FAILURES))
    if FAILURES:
        print(FAILURES)
        sys.exit(1)
