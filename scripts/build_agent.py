#!/usr/bin/env python3
"""在目标兼容的构建机生成单文件 Agent 和平台侧发布清单。"""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "agents")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Linux 客户端须在 Linux 上构建")
    arch = platform.machine().lower()
    if arch not in ("x86_64", "aarch64"):
        parser.error("不支持的目标架构")
    target = args.output.resolve() / ("linux-" + arch)
    target.mkdir(parents=True, exist_ok=True)
    source = ROOT / "agent" / "backup_agent.py"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--onefile", "--strip", "--name", "aidbm-agent",
                    "--distpath", str(target),
                    "--workpath", str(ROOT / "build" / "agent"),
                    "--specpath", str(ROOT / "build" / "agent"),
                    str(source)], cwd=ROOT, check=True)
    binary = target / "aidbm-agent"
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    with binary.open("rb") as fp:
        digest = hashlib.file_digest(fp, "sha256").hexdigest()
    metadata = {"version": version, "os": "linux", "arch": arch,
                "sha256": digest, "size": binary.stat().st_size,
                "glibc": platform.libc_ver()[1], "format": "ELF-onefile",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    temporary = target / "manifest.json.tmp"
    temporary.write_text(json.dumps(metadata, indent=2) + "\n")
    os.replace(temporary, target / "manifest.json")
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()
