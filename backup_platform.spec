# -*- mode: python ; coding: utf-8 -*-
"""
跨平台 PyInstaller spec —— Windows / Linux 通用。

用法（无需修改本文件，也不用改任何代码）：
    Windows:  pyinstaller backup_platform.spec
    Linux:    pyinstaller backup_platform.spec

说明：
- 路径全部基于本 spec 文件所在目录（SPECPATH）动态推导，
  因此把整个项目目录拷到任意平台后直接执行即可，无需改动。
- 产物为单文件可执行：dist/backup_platform(.exe)。
- 运行时资源（templates / static / core/plugins / requirements.txt）
  会被打包进可执行文件，启动后由 bootloader 解压到临时目录并自动加载。
- 备份数据 / 数据库 / 日志写入可执行文件所在目录（见 config.py 的 frozen 处理）。
"""
import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve()

a = Analysis(
    [str(ROOT / 'run.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / 'templates'), 'templates'),
        (str(ROOT / 'static'), 'static'),
        (str(ROOT / 'core' / 'plugins'), 'core/plugins'),
        (str(ROOT / 'drivers'), 'drivers'),
        (str(ROOT / 'requirements.txt'), '.'),
    ],
    hiddenimports=[
        'oracledb', 'cx_Oracle', 'ksycopg2', 'psycopg2',
        'dmPython', 'pymysql', 'pymongo', 'redis', 'paramiko',
        'apscheduler', 'watchdog', 'yaml',
        'jpype', 'jaydebeapi',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='backup_platform',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
