#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对象存储备份「全量 / 增量 / 断点续传」真实 E2E。

与 scripts/object_storage_e2e.py 同口径：**不仿真**，所有断言都落到真实
HTTP 请求与真实字节比对上：

* 单对象：预置 ``*.aidbm_part`` → 验证客户端真的发出 ``Range: bytes=N-``、
  服务端真的回 206、续传后产物 SHA256 与源逐字节一致；
* 作业级：注入真实传输中断（抛网络异常 / 用户取消）→ 验证断点保留、
  重跑只下未完成对象、产物内容完整；
* 组合：全量 → 增量（中断续传）→ parent 链恢复回放，目标桶与当前源一致；
* 护栏：源对象变化 / 已完成文件损坏 / TTL 过期 / 关闭续传，都不许复用错字节。

环境说明（如实）：默认目标是本机真实运行的 S3 协议 HTTP 服务（moto_server）。
moto 不校验签名，因此「错误密钥必须失败」这类断言只能在真实 MinIO 上跑，
见 docs/object_storage_e2e_report_20260919.md（32/32）。本脚本不测签名，
只测续传协议与字节正确性。

用法::

    (setsid .venv/bin/python -m moto.server -H 127.0.0.1 -p 9000 >/tmp/moto.log 2>&1 &)
    CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/bin/python \
        scripts/object_storage_resume_e2e.py --endpoint 127.0.0.1:9000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import cancel as cancel_mod                        # noqa: E402
from core.engines.base import BackupType                    # noqa: E402
from core.engines.object_storage import ObjectStorageEngine  # noqa: E402
from core.objectstore import client as osc                  # noqa: E402
from core.objectstore import providers as osp               # noqa: E402
from core.objectstore import resume as osr                 # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
CANCEL_KEY = "t99099:r1"


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  —— " + detail) if detail else ""))
    (PASSED if ok else FAILED).append(name)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_cli(args):
    return osc.ObjectStoreClient(
        args.endpoint, args.ak, args.sk, secure=False, region="us-east-1",
        provider="minio", retries=2)


def make_task(bucket: str, extra: dict, tid: int, args):
    return {
        "id": tid, "name": "obj-resume-e2e", "host": args.endpoint, "port": 0,
        "username": args.ak, "password": args.sk, "db_name": bucket,
        "backup_mode": "logical", "extra_options": dict(extra or {}),
    }


def make_eng(bucket: str, root: str, extra: dict, tid: int, args):
    task = make_task(bucket, extra, tid, args)
    return ObjectStorageEngine(task, root), task


def seed(cli, bucket: str, blobs: dict) -> None:
    if not cli.bucket_exists(bucket):
        cli.create_bucket(bucket)
    tmp = tempfile.mkdtemp(prefix="osr_seed_")
    try:
        for key, content in blobs.items():
            p = os.path.join(tmp, hashlib.md5(key.encode("utf-8")).hexdigest())
            with open(p, "wb") as f:
                f.write(content)
            cli.upload_file(bucket, key, p)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def reset_bucket(cli, bucket: str) -> None:
    """moto 服务常驻：每轮先清空桶，避免上一轮残留对象污染断言。"""
    if not cli.bucket_exists(bucket):
        cli.create_bucket(bucket)
        return
    for item in list(cli.iter_object_versions(bucket)):
        cli.delete_object(bucket, item["key"], item.get("version_id") or "")


def read_tar(tar_path: str):
    """返回 (manifest, {key: bytes}, tar 成员名列表)，全部来自真实产物。"""
    with tarfile.open(tar_path, "r:*") as t:
        names = t.getnames()
        man = json.loads(t.extractfile("manifest.json").read().decode("utf-8"))
        objs = {}
        for o in man["objects"]:
            objs[o["key"]] = t.extractfile(o["path"]).read()
        return man, objs, names


class Spy:
    """记录真实下载调用与真实 HTTP 请求（Range 头 / 状态码）。"""

    def __init__(self):
        self.dl_calls: list[str] = []
        self.ranges: list[str] = []
        self.statuses: list[int] = []
        self._orig_req = None
        self._orig_dl = None

    def install(self):
        self._orig_req = osc.ObjectStoreClient._request
        self._orig_dl = osc.ObjectStoreClient.download_to_file
        orig_req, orig_dl, holder = self._orig_req, self._orig_dl, self

        def spy_req(self, method, bucket, key="", query=None, headers=None, **kw):
            hdrs = dict(headers or {})
            if str(method).upper() == "GET" and key and hdrs.get("range"):
                holder.ranges.append(hdrs["range"])
            resp, conn = orig_req(self, method, bucket, key=key, query=query,
                                  headers=headers, **kw)
            if str(method).upper() == "GET" and key and hdrs.get("range"):
                holder.statuses.append(resp.status)
            return resp, conn

        def spy_dl(self, bucket, key, dest, *a, **kw):
            holder.dl_calls.append(key)
            return orig_dl(self, bucket, key, dest, *a, **kw)

        osc.ObjectStoreClient._request = spy_req
        osc.ObjectStoreClient.download_to_file = spy_dl

    def reset(self):
        self.dl_calls, self.ranges, self.statuses = [], [], []

    def restore(self):
        osc.ObjectStoreClient._request = self._orig_req
        osc.ObjectStoreClient.download_to_file = self._orig_dl


class InterruptClient(osc.ObjectStoreClient):
    """在指定对象上注入真实传输异常，或在数据块边界触发用户取消。"""

    fail_on = ""
    cancel_on_chunk = False
    tripped = False          # 网络断连是持续的：首个失败之后的对象也全部失败

    def download_to_file(self, bucket, key, dest, *a, **kw):
        if self.fail_on and (key == self.fail_on or self.tripped):
            if key == self.fail_on:
                self.tripped = True
            raise osc.ObjectStoreError("模拟网络中断: %s" % key)
        if self.cancel_on_chunk:
            orig_cb = kw.get("on_chunk")

            def cb(total):
                if orig_cb:
                    orig_cb(total)
                cancel_mod.cancel(CANCEL_KEY)

            kw["on_chunk"] = cb
        return super().download_to_file(bucket, key, dest, *a, **kw)


def blob_set(big: int = 0) -> dict:
    blobs = {
        "readme.txt": b"resume e2e head\n",
        "logs/app.log": b"\n".join(b"line-%d" % i for i in range(300)) + b"\n",
        "中文/订单.txt": "订单-甲 111.50\n订单-乙 222.00\n".encode("utf-8"),
        "bin/empty.bin": b"",
        "deep/a/b/c/nested.txt": b"deep\n",
        "bin/big.bin": os.urandom(big or 3 * 1024 * 1024),
    }
    return blobs


def main() -> int:
    ap = argparse.ArgumentParser(description="对象存储断点续传真实 E2E")
    ap.add_argument("--endpoint", default="127.0.0.1:9000")
    ap.add_argument("--ak", default="minioadmin")
    ap.add_argument("--sk", default="minioadmin")
    args = ap.parse_args()

    cli = make_cli(args)
    work = tempfile.mkdtemp(prefix="osr_work_")
    spy = Spy()
    spy.install()
    try:
        # ---------------- R1 全量基准（无断点也应完全正常） ----------------
        print("\n--- R1 全量备份基准 ---")
        root1 = os.path.join(work, "r1")
        bucket = "aidbm-resume-r1"
        blobs = blob_set()
        reset_bucket(cli, bucket)
        seed(cli, bucket, blobs)
        eng, _ = make_eng(bucket, root1, {"os_workers": 1}, 99101, args)
        spy.reset()
        r = eng.backup(BackupType.FULL)
        check("R1.1 全量备份成功", bool(r.success), r.message[:110])
        man, got, names = read_tar(r.backup_path)
        check("R1.2 6 个对象逐字节一致",
              len(got) == len(blobs) and all(got[k] == v for k, v in blobs.items()),
              "%d/%d" % (len(got), len(blobs)))
        check("R1.3 成功后 stage 已清理",
              not os.path.isdir(osr.stage_path(eng._output_dir())))
        check("R1.4 全量包无 parent", man.get("parent") is None)

        # ---------------- R2 单对象 Range 字节级续传 ----------------
        print("\n--- R2 单对象 HTTP Range 续传 ---")
        root2 = os.path.join(work, "r2")
        bucket2 = "aidbm-resume-r2"
        big = os.urandom(3 * 1024 * 1024)
        reset_bucket(cli, bucket2)
        seed(cli, bucket2, {"big.bin": big})
        eng2, task2 = make_eng(bucket2, root2, {"os_workers": 1}, 99102, args)
        out_dir = eng2._output_dir()
        stage = osr.stage_path(out_dir)
        os.makedirs(os.path.join(stage, osp.OBJECTS_DIR), exist_ok=True)
        cfg2 = osp.task_cfg(task2)
        osr.save_meta(stage, osr.new_checkpoint(
            task2["id"], task2["name"], cfg2, "full", 1))
        rel = osp.key_to_relpath("big.bin", "")
        part = osr.part_path(os.path.join(stage, rel))
        os.makedirs(os.path.dirname(part) or ".", exist_ok=True)
        n = 1_000_000
        with open(part, "wb") as f:
            f.write(big[:n])
        spy.reset()
        r2 = eng2.backup(BackupType.FULL)
        check("R2.1 带断点备份成功", bool(r2.success), r2.message[:110])
        check("R2.2 真的发出 Range: bytes=%d-" % n,
              "bytes=%d-" % n in spy.ranges, str(spy.ranges))
        check("R2.3 服务端真的回 206", 206 in spy.statuses, str(spy.statuses))
        _m2, got2, names2 = read_tar(r2.backup_path)
        check("R2.4 续传产物与源逐字节一致",
              got2.get("big.bin") == big, "sha=%s" % sha(got2.get("big.bin", b""))[:16])
        check("R2.5 part 完成后已消除", not os.path.exists(part))
        check("R2.6 断点元数据不进产物",
              not any((".checkpoint" in x or ".done.jsonl" in x
                       or x.endswith(osr.PART_SUFFIX)) for x in names2),
              str([x for x in names2 if x.startswith(".")][:4]))

        # ---------------- R3 作业级中断 → 复用已完成对象 ----------------
        print("\n--- R3 中断后重跑只下未完成对象 ---")
        root3 = os.path.join(work, "r3")
        bucket3 = "aidbm-resume-r3"
        blobs3 = blob_set()
        reset_bucket(cli, bucket3)
        seed(cli, bucket3, blobs3)
        eng3, _ = make_eng(bucket3, root3, {"os_workers": 1}, 99103, args)
        keys = sorted(blobs3)
        bad = InterruptClient(args.endpoint, args.ak, args.sk, secure=False,
                              region="us-east-1", provider="minio", retries=2)
        bad.fail_on = keys[2]
        eng3._cli = bad
        spy.reset()
        r3a = eng3.backup(BackupType.FULL)
        check("R3.1 中断如实失败且提示断点保留",
              (not r3a.success) and "断点已保留" in (r3a.message or ""),
              r3a.message[:130])
        stage3 = osr.stage_path(eng3._output_dir())
        done3 = osr.load_done(stage3)
        check("R3.2 已完成对象写入 checkpoint", len(done3) == 2,
              "done=%d %s" % (len(done3), sorted(done3)[:3]))
        check("R3.3 stage 未被删除", os.path.isdir(stage3))

        eng3._cli = make_cli(args)
        spy.reset()
        r3b = eng3.backup(BackupType.FULL)
        check("R3.4 重跑成功", bool(r3b.success), r3b.message[:130])
        check("R3.5 只下载未完成 4 个对象", len(spy.dl_calls) == 4,
              "downloads=%d %s" % (len(spy.dl_calls), spy.dl_calls[:6]))
        check("R3.6 成功消息透出复用数量",
              "断点续传复用 2 个对象" in (r3b.message or ""), r3b.message[:130])
        man3, got3, names3 = read_tar(r3b.backup_path)
        check("R3.7 manifest 记录复用统计",
              (man3.get("resume") or {}).get("reused_objects") == 2)
        check("R3.8 6 个对象逐字节一致",
              len(got3) == 6 and all(got3[k] == v for k, v in blobs3.items()))
        check("R3.9 产物不含断点文件",
              not any((".checkpoint" in x or ".done.jsonl" in x
                       or x.endswith(osr.PART_SUFFIX)) for x in names3))
        check("R3.10 成功后 stage 清理", not os.path.isdir(stage3))

        # ---------------- R4 增量 + 中断续传 + 恢复链回放 ----------------
        print("\n--- R4 增量中断续传与恢复链 ---")
        blobs4 = dict(blobs3)
        blobs4["readme.txt"] = b"changed after full\n"
        blobs4["new/added.txt"] = "新增-丙\n".encode("utf-8")
        seed(cli, bucket3, {"readme.txt": blobs4["readme.txt"],
                            "new/added.txt": blobs4["new/added.txt"]})
        cli.delete_object(bucket3, "deep/a/b/c/nested.txt")
        del blobs4["deep/a/b/c/nested.txt"]
        eng4, _ = make_eng(bucket3, root3, {"os_workers": 1}, 99103, args)
        bad4 = InterruptClient(args.endpoint, args.ak, args.sk, secure=False,
                               region="us-east-1", provider="minio", retries=2)
        bad4.fail_on = "readme.txt"
        eng4._cli = bad4
        r4a = eng4.backup(BackupType.INCREMENTAL)
        check("R4.1 增量首次中断如实失败", not r4a.success, r4a.message[:110])
        eng4._cli = make_cli(args)
        spy.reset()
        r4b = eng4.backup(BackupType.INCREMENTAL)
        check("R4.2 增量续传成功", bool(r4b.success), r4b.message[:130])
        man4, got4, _n4 = read_tar(r4b.backup_path)
        check("R4.3 只传变化对象（改1+增1）",
              sorted(got4) == ["new/added.txt", "readme.txt"], str(sorted(got4)))
        check("R4.4 parent 指向全量产物",
              man4.get("parent") == os.path.basename(r3b.backup_path),
              str(man4.get("parent")))
        check("R4.5 增量重跑复用了断点对象",
              (man4.get("resume") or {}).get("reused_objects") == 1,
              str(man4.get("resume")))
        dst = "aidbm-resume-r4-restore"
        reset_bucket(cli, dst)
        rr = eng4.restore(r4b.backup_path, target_bucket=dst,
                          create_bucket=True, apply_deleted=True, workers=1)
        check("R4.6 full→inc 恢复链回放成功", bool(rr.success), rr.message[:130])
        actual = {i["key"]: cli.get_bytes(dst, i["key"])
                  for i in cli.iter_objects(dst)}
        check("R4.7 回放后与当前源一致（含删除）",
              actual == blobs4,
              "缺失=%s 多余=%s" % (sorted(set(blobs4) - set(actual))[:3],
                                   sorted(set(actual) - set(blobs4))[:3]))

        # ---------------- R5 正确性护栏 ----------------
        print("\n--- R5 护栏：变化 / 损坏 / TTL / 关闭续传 ---")
        root5 = os.path.join(work, "r5")
        bucket5 = "aidbm-resume-r5"
        blobs5 = blob_set(200000)
        reset_bucket(cli, bucket5)
        seed(cli, bucket5, blobs5)
        eng5, _ = make_eng(bucket5, root5, {"os_workers": 1}, 99105, args)
        bad5 = InterruptClient(args.endpoint, args.ak, args.sk, secure=False,
                               region="us-east-1", provider="minio", retries=2)
        bad5.fail_on = sorted(blobs5)[2]
        eng5._cli = bad5
        eng5.backup(BackupType.FULL)
        stage5 = osr.stage_path(eng5._output_dir())
        done5 = osr.load_done(stage5)
        # 从实际已完成断点里挑一个对象改写，验证「源变化即单独重下」
        changed_key = sorted(done5)[0] if done5 else "readme.txt"
        if changed_key in done5:
            new_content = ("source overwritten between attempts %s\n" % changed_key).encode("utf-8")
            seed(cli, bucket5, {changed_key: new_content})
            blobs5[changed_key] = new_content
            eng5._cli = make_cli(args)
            spy.reset()
            r5a = eng5.backup(BackupType.FULL)
            check("R5.1 源对象改写后只重下该对象",
                  bool(r5a.success) and changed_key in spy.dl_calls
                  and len([k for k in spy.dl_calls if k == changed_key]) == 1,
                  "downloads=%s" % spy.dl_calls[:6])
            _m5, got5, _ = read_tar(r5a.backup_path)
            check("R5.2 产物用的是改写后的新字节",
                  got5.get(changed_key) == new_content)
        else:
            check("R5.1 源对象改写后只重下该对象", False,
                  "断点为空，无法验证")
            check("R5.2 产物用的是改写后的新字节", False, "前置失败")

        # 文件损坏护栏：制造断点后截断已完成对象
        root5b = os.path.join(work, "r5b")
        bucket5b = "aidbm-resume-r5b"
        reset_bucket(cli, bucket5b)
        seed(cli, bucket5b, blobs5)
        eng5b, _ = make_eng(bucket5b, root5b, {"os_workers": 1}, 99106, args)
        bad5b = InterruptClient(args.endpoint, args.ak, args.sk, secure=False,
                                region="us-east-1", provider="minio", retries=2)
        bad5b.fail_on = sorted(blobs5)[2]
        eng5b._cli = bad5b
        eng5b.backup(BackupType.FULL)
        stage5b = osr.stage_path(eng5b._output_dir())
        done5b = osr.load_done(stage5b)
        victim = sorted(done5b)[0]
        victim_path = os.path.join(stage5b, done5b[victim]["path"])
        with open(victim_path, "wb") as f:
            f.write(b"broken")
        eng5b._cli = make_cli(args)
        spy.reset()
        r5c = eng5b.backup(BackupType.FULL)
        check("R5.3 已落盘文件损坏时重下该对象",
              bool(r5c.success) and victim in spy.dl_calls,
              "victim=%s downloads=%s" % (victim, spy.dl_calls[:6]))

        # TTL 过期护栏
        root5c = os.path.join(work, "r5c")
        bucket5c = "aidbm-resume-r5c"
        reset_bucket(cli, bucket5c)
        seed(cli, bucket5c, blobs5)
        eng5c, _ = make_eng(bucket5c, root5c, {"os_workers": 1}, 99107, args)
        bad5c = InterruptClient(args.endpoint, args.ak, args.sk, secure=False,
                                region="us-east-1", provider="minio", retries=2)
        bad5c.fail_on = sorted(blobs5)[2]
        eng5c._cli = bad5c
        eng5c.backup(BackupType.FULL)
        stage5c = osr.stage_path(eng5c._output_dir())
        cp_path = osr.checkpoint_path(stage5c)
        cp = json.load(open(cp_path, "r", encoding="utf-8"))
        import time as _t
        cp["created_at"] = _t.time() - 100 * 3600
        json.dump(cp, open(cp_path, "w", encoding="utf-8"))
        eng5c._cli = make_cli(args)
        spy.reset()
        r5d = eng5c.backup(BackupType.FULL)
        check("R5.4 TTL 过期后全部重下",
              bool(r5d.success) and len(spy.dl_calls) == len(blobs5),
              "downloads=%d / %d" % (len(spy.dl_calls), len(blobs5)))

        # 关闭续传：行为等同旧版，不复用
        root5e = os.path.join(work, "r5e")
        bucket5e = "aidbm-resume-r5e"
        reset_bucket(cli, bucket5e)
        seed(cli, bucket5e, blobs5)
        eng5e, _ = make_eng(bucket5e, root5e,
                            {"os_workers": 1, "os_resume": False}, 99108, args)
        bad5e = InterruptClient(args.endpoint, args.ak, args.sk, secure=False,
                                region="us-east-1", provider="minio", retries=2)
        bad5e.fail_on = sorted(blobs5)[2]
        eng5e._cli = bad5e
        eng5e.backup(BackupType.FULL)
        eng5e._cli = make_cli(args)
        spy.reset()
        r5e = eng5e.backup(BackupType.FULL)
        check("R5.5 关闭续传后全部重下且不建断点目录",
              bool(r5e.success) and len(spy.dl_calls) == len(blobs5)
              and not os.path.isdir(osr.stage_path(eng5e._output_dir())),
              "downloads=%d" % len(spy.dl_calls))

        # ---------------- R6 用户「停止」→ 断点保留 → 重跑 ----------------
        print("\n--- R6 协作式取消保留断点 ---")
        root6 = os.path.join(work, "r6")
        bucket6 = "aidbm-resume-r6"
        blobs6 = blob_set(400000)
        reset_bucket(cli, bucket6)
        seed(cli, bucket6, blobs6)
        eng6, _ = make_eng(bucket6, root6, {"os_workers": 1}, 99109, args)
        cancel_mod.register(CANCEL_KEY)
        token = cancel_mod.attach(CANCEL_KEY)
        old_chunk = osc._CHUNK
        osc._CHUNK = 65536
        cancelling = InterruptClient(args.endpoint, args.ak, args.sk, secure=False,
                                     region="us-east-1", provider="minio", retries=2)
        cancelling.cancel_on_chunk = True
        eng6._cli = cancelling
        cancelled = False
        try:
            eng6.backup(BackupType.FULL)
        except cancel_mod.CancelledError:
            cancelled = True
        finally:
            osc._CHUNK = old_chunk
            cancel_mod.finish(CANCEL_KEY)
            cancel_mod.detach(token)
        check("R6.1 取消真的中断引擎（抛 CancelledError）", cancelled)
        check("R6.2 取消后断点目录保留",
              os.path.isdir(osr.stage_path(eng6._output_dir())))
        eng6._cli = make_cli(args)
        spy.reset()
        r6 = eng6.backup(BackupType.FULL)
        check("R6.3 重跑从断点继续成功", bool(r6.success), r6.message[:130])
        _m6, got6, _ = read_tar(r6.backup_path)
        check("R6.4 数据仍逐字节完整",
              len(got6) == len(blobs6)
              and all(got6[k] == v for k, v in blobs6.items()))
    finally:
        spy.restore()
        shutil.rmtree(work, ignore_errors=True)

    print("\n================= 汇总 =================\n通过 %d 项，失败 %d 项"
          % (len(PASSED), len(FAILED)))
    for f in FAILED:
        print("  失败：" + f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
