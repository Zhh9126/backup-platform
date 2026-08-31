# -*- coding: utf-8 -*-
"""离线部署依赖导出脚本。

用途：在**有网**的构建机上，一次性导出离线部署所需的三类资产：
1. Python 依赖离线包（wheel/sdist）→ wheelhouse/（含原生直连驱动，
   连接测试/拉库列表/数据对比均无需 Java）
2. 平台可执行包（PyInstaller one-file）→ dist/backup_platform(.exe)
3. JDBC 驱动 jar 清单与状态检查（可选兜底通道）

用法：
    python scripts/export_offline_deps.py            # 全部导出
    python scripts/export_offline_deps.py --pip-only # 仅导出 Python 离线包
    python scripts/export_offline_deps.py --no-build # 跳过 PyInstaller 打包

说明：
- 目标平台 Python 版本/操作系统不同，pip 离线包**不能混用**。
  建议在目标机相同系统（Windows / Linux x64）与相近 Python 版本（3.10-3.14）下构建。
- 离线部署机**无需安装 Java**：直连通道使用纯 Python 驱动（pymysql/psycopg2/oracledb）。
  仅当需要 JDBC 兜底（如 Oracle 11g）时，才安装 JDK/JRE 8+，
  或拷贝到可执行文件同目录的 jdk/、jre/（见 drivers/README.md）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"
WHEELHOUSE = ROOT / "wheelhouse"


def pip_download() -> None:
    WHEELHOUSE.mkdir(exist_ok=True)
    cmd = [
        sys.executable, "-m", "pip", "download",
        "-r", str(REQ),
        "-d", str(WHEELHOUSE),
    ]
    print(">>> " + " ".join(cmd))
    subprocess.run(cmd, check=True)
    n = len(list(WHEELHOUSE.glob("*")))
    print(f"[ok ] Python 离线包 {n} 个 -> {WHEELHOUSE}")


def check_jars() -> None:
    from pathlib import Path as P
    drivers = ROOT / "drivers"
    missing = []
    for jar in drivers.glob("*.jar"):
        sz = jar.stat().st_size
        status = "ok " if sz > 100000 else "BAD"
        print(f"  jar {status} {jar.name} ({sz} bytes)")
        if sz <= 100000:
            missing.append(jar.name)
    if missing:
        print("[warn] 以下驱动 jar 异常，请重新下载：", ", ".join(missing))
    else:
        print("[ok ] JDBC 驱动 jar 就绪（已随可执行文件打包）")


def build_pyinstaller() -> None:
    builder = ROOT / "_pyi_build.py"
    print(">>> python _pyi_build.py")
    subprocess.run([sys.executable, str(builder)], check=True)
    exe = ROOT / "dist"
    print(f"[ok ] 可执行产物 -> {exe}")


def main() -> None:
    pip_only = "--pip-only" in sys.argv
    no_build = "--no-build" in sys.argv
    if not (REQ.exists()):
        print("缺少 requirements.txt")
        sys.exit(1)

    pip_download()
    check_jars()
    if not pip_only and not no_build:
        build_pyinstaller()

    print("\n========== 离线交付清单 ==========")
    print(f"1. Python 离线包 : {WHEELHOUSE}  (需与目标机系统/Python 版本一致)")
    print(f"2. 可执行程序    : {ROOT / 'dist' / ('backup_platform.exe' if sys.platform.startswith('win') else 'backup_platform')}")
    print(f"3. JDBC 驱动 jar : 已内置（drivers/ 目录，可选兜底用）")
    print("4. Java 运行时   : 无需安装（直连走纯 Python 驱动）；仅 JDBC 兜底时才需 JDK/JRE 8+")
    print("目标机安装：pip install --no-index --find-links=<wheelhouse> -r requirements.txt")


if __name__ == "__main__":
    main()
