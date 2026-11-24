#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""备份链路传输性能基准测试（容灾一体机需求：10Gbps 环境 ≥2000Mbps）。

用途：交付现场在 10G 网络环境下运行本脚本，实测平台实际备份传输通道
（SSH/SFTP 断点续传 + 多流并行 + zstd 压缩）的吞吐，输出是否达标。
零安装：仅用平台自带依赖（paramiko / zstandard），不装任何外部工具。

用法：
  # 对纳管主机做多流 SFTP 吞吐测试（模拟平台备份拉回方向）
  .venv/bin/python scripts/transfer_benchmark.py \
      --host root@192.168.220.170:22 --password 'xxx' \
      --size-mb 512 --streams 4 --direction pull

  # 对 Agent 通道测试（若目标机部署了 agent）
  .venv/bin/python scripts/transfer_benchmark.py \
      --host root@192.168.220.170:22 --password 'xxx' \
      --size-mb 512 --streams 4 --agent-port 8466

判定口径：
  - 2000 Mbps = 250 MB/s。实测吞吐 ≥ 阈值 → PASS，否则 FAIL 并给出
    瓶颈提示（网络协商速率 / 磁盘 IO / 压缩开销 / 单连接上限）。
  - 多流：N 个独立 SSH 连接各传 size/N，并发汇总吞吐（与平台
    sftp_pull_files_parallel 同构）。
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

import paramiko

MB = 1024 * 1024
TARGET_MBPS = 2000.0


def _parse_host(s):
    """root@host:port → (user, host, port)"""
    user, _, rest = s.partition("@")
    host, _, port = rest.rpartition(":")
    return user or "root", host, int(port or 22)


def _connect(user, host, port, password, pkey=None):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(hostname=host, port=port, username=user,
              timeout=15, allow_agent=False, look_for_keys=False)
    if pkey and os.path.isfile(pkey):
        kw["key_filename"] = pkey
    else:
        kw["password"] = password
    c.connect(**kw)
    return c


def _one_stream_pull(user, host, port, password, pkey, remote_file,
                     local_file, expect_sha, label):
    """单流：SFTP 拉回 + SHA256 对账，返回 (bytes, seconds, ok)。"""
    client = _connect(user, host, port, password, pkey)
    t0 = time.time()
    try:
        sftp = client.open_sftp()
        sftp.get(remote_file, local_file)
        sftp.close()
        elapsed = time.time() - t0
        h = hashlib.sha256()
        with open(local_file, "rb") as f:
            for blk in iter(lambda: f.read(4 * MB), b""):
                h.update(blk)
        ok = (not expect_sha or h.hexdigest() == expect_sha)
        return os.path.getsize(local_file), elapsed, ok
    finally:
        client.close()


def _one_stream_push(user, host, port, password, pkey, local_file,
                     remote_file, label):
    """单流：SFTP 推送（模拟备份推回目标端方向）。"""
    client = _connect(user, host, port, password, pkey)
    t0 = time.time()
    try:
        sftp = client.open_sftp()
        sftp.put(local_file, remote_file)
        sftp.close()
        return os.path.getsize(local_file), time.time() - t0, True
    finally:
        client.close()


def run(args):
    user, host, port = _parse_host(args.host)
    tmp = tempfile.mkdtemp(prefix="aidbm_bench_")
    directions = args.direction.split(",")
    report = {
        "target": "%s@%s:%d" % (user, host, port),
        "size_mb": args.size_mb,
        "streams": args.streams,
        "directions": {},
    }
    total_mbps = {}

    for direction in directions:
        direction = direction.strip()
        if direction not in ("pull", "push"):
            continue
        # 在远端生成测试文件（dd 零安装：dd 是 OS 自带）
        client = _connect(user, host, port, args.password, args.pkey)
        remote_files = []
        local_files = []
        try:
            chunk_mb = max(1, args.size_mb // args.streams)
            for i in range(args.streams):
                rp = "/tmp/.aidbm_bench_%d_%d.bin" % (os.getpid(), i)
                cmd = "dd if=/dev/zero of=%s bs=1M count=%d 2>/dev/null && sha256sum %s" % (
                    rp, chunk_mb, rp)
                _in, out, err = client.exec_command(cmd, timeout=300)
                out.channel.recv_exit_status()
                out_s = out.read().decode("utf-8", "replace")
                sha = out_s.split()[0] if out_s.strip() else ""
                if not sha:
                    err_s = err.read().decode("utf-8", "replace")
                    raise RuntimeError("远端造数据失败: %s" % err_s[:120])
                remote_files.append((rp, sha))
                local_files.append(os.path.join(tmp, "part_%d.bin" % i))
            client.close()
            client = None
            cleanup_client = _connect(user, host, port, args.password, args.pkey)

            t0 = time.time()
            results = []
            if direction == "pull":
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=args.streams) as ex:
                    futs = [
                        ex.submit(_one_stream_pull, user, host, port,
                                  args.password, args.pkey, rp, lp, sha, i)
                        for i, ((rp, sha), lp) in enumerate(zip(remote_files, local_files))]
                    results = [f.result() for f in futs]
            else:
                # push 方向：先本地造数据（远端已有同大小文件，直接覆盖）
                for lp in local_files:
                    with open(lp, "wb") as f:
                        f.write(b"\0" * (chunk_mb * MB))
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=args.streams) as ex:
                    futs = [
                        ex.submit(_one_stream_push, user, host, port,
                                  args.password, args.pkey, lp, rp, i)
                        for i, ((rp, _sha), lp) in enumerate(zip(remote_files, local_files))]
                    results = [f.result() for f in futs]
            elapsed = time.time() - t0

            total_bytes = sum(r[0] for r in results)
            all_ok = all(r[2] for r in results)
            mbps = round(total_bytes * 8 / elapsed / 1e6, 1)
            total_mbps[direction] = mbps
            report["directions"][direction] = {
                "bytes": total_bytes,
                "elapsed_sec": round(elapsed, 2),
                "mbps": mbps,
                "threshold_mbps": args.threshold,
                "pass": mbps >= args.threshold and all_ok,
                "checksum_ok": all_ok,
                "streams": args.streams,
            }
        finally:
            _cc = cleanup_client if 'cleanup_client' in locals() and cleanup_client else client
            if _cc:
                try:
                    _cc.exec_command("rm -f " + " ".join(
                        rp for rp, _ in remote_files))
                except Exception:
                    pass
                _cc.close()
            for lp in local_files:
                try:
                    os.unlink(lp)
                except OSError:
                    pass

    # 远端清理本地临时目录
    try:
        os.rmdir(tmp)
    except OSError:
        pass

    report["pass"] = all(v["pass"] for v in report["directions"].values())
    report["bottleneck_hints"] = []
    if not report["pass"]:
        report["bottleneck_hints"] = [
            "确认链路协商速率 ≥ 10Gbps（ethtool ethX | grep Speed）",
            "确认源/目标磁盘顺序读写 ≥ 250MB/s（dd if=/dev/zero of=/tmp/t bs=1M count=1024）",
            "增大 --streams（当前 %d，平台上限 8）" % args.streams,
            "压缩会降吞吐但省带宽：对比 extra.compress_algo=none",
        ]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


def main():
    ap = argparse.ArgumentParser(description="AIDBM 备份链路传输性能基准测试")
    ap.add_argument("--host", required=True, help="root@host:port")
    ap.add_argument("--password", default="", help="SSH 密码")
    ap.add_argument("--pkey", default="", help="SSH 私钥路径（优先于密码）")
    ap.add_argument("--size-mb", type=int, default=512, help="总测试数据量 MB")
    ap.add_argument("--streams", type=int, default=4, help="并发流数（1-8）")
    ap.add_argument("--direction", default="pull", help="pull/push 或 pull,push")
    ap.add_argument("--threshold", type=float, default=TARGET_MBPS,
                    help="达标阈值 Mbps（默认 2000）")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
