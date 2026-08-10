# -*- coding: utf-8 -*-
"""本地校验脚本：导入新增模块并打印插件列表。"""
import sys
sys.path.insert(0, '.')

from core import plugin_catalog, plugin_installer

# 1) 加载所有插件
rows = plugin_catalog.list_plugins()
print(f"[1] Loaded {len(rows)} plugins")
for r in rows:
    print(f"  - {r['id']:30s} {r['name']:30s} "
          f"installed={r['installed']} os_supported={r['os_supported']}")

# 2) 分类
print("\n[2] Categories:", [c['category'] for c in plugin_catalog.categories()])

# 3) 当前 OS / 包管理器
print("\n[3] OS:", plugin_catalog.detect_os(),
      "PackageManager:", plugin_catalog.detect_package_manager())

# 4) 安装器模块 API 验证（仅验证函数存在，不真触发）
import inspect
assert callable(plugin_installer.install), "install() 缺失"
assert callable(plugin_installer.uninstall), "uninstall() 缺失"
assert callable(plugin_installer.get_state), "get_state() 缺失"
print("\n[4] installer 模块 API 完整")

# 5) 单插件详情
detail = plugin_catalog.get_plugin('percona-xtrabackup-80')
assert detail and detail['required_clients'], "xtrabackup-80 详情缺失"
print(f"\n[5] xtrabackup-80 详情: required={detail['required_clients']} "
      f"os_supported={detail['os_supported']} pkg_mgr={detail['package_manager']}")

print("\nALL OK")