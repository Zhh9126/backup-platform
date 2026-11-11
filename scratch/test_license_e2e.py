# -*- coding: utf-8 -*-
"""社区免费版 / 企业永久版配额自检（手工运维脚本，需 8080 平台 + 8088 发号系统在线）。

配额上限从 /api/system/license 的 license.quota_limit 动态读取，
不再假设固定 5 个；跑完会清理自检数据并重新激活企业授权。
"""
import sys
from pathlib import Path

BASE_DIR = str(Path(__file__).resolve().parent.parent)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

from core import db

BASE_URL = 'http://127.0.0.1:8080'
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def api(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        BASE_URL + path, data=data,
        headers={'Content-Type': 'application/json'})
    return json.loads(opener.open(req).read().decode('utf-8'))['data']


# 1. 登录 AIDBM
login_data = urllib.parse.urlencode({'username': 'admin', 'password': 'admin123'}).encode('utf-8')
login_res = opener.open(BASE_URL + '/login', data=login_data)
print('1. 登录成功, status:', login_res.status)

# 2. 注销可能残留的授权，回到社区免费版
api('/api/system/license/deactivate', {})
db.execute("DELETE FROM netdev_devices WHERE name LIKE '配额自检-%'")

q_res = api('/api/system/license')
FREE_LIMIT = int(q_res['license']['quota_limit'])
cur_netdev = q_res['quotas']['netdev']['used']
print('2. 社区免费版初始网络设备数量:', cur_netdev,
      '配额上限:', FREE_LIMIT, '版本:', q_res['license']['edition_label'])

# 补齐到配额上限
for i in range(cur_netdev + 1, FREE_LIMIT + 1):
    api('/api/netdev/devices', {
        'name': f'配额自检-SW{i}',
        'device_type': 'switch',
        'vendor': 'huawei',
        'host': f'10.10.1.{i}',
        'username': 'admin',
        'password': 'pwd',
        'backup_commands': 'display current-configuration',
        'auto_test': False,
    })
    print(f'   已添加第 {i} 台网络设备 (<= {FREE_LIMIT} 台允许)')

# 3. 免费版尝试添加第 FREE_LIMIT+1 台 -> 必须被 403 阻断！
over_no = FREE_LIMIT + 1
print(f'3. 尝试在社区免费版中添加第 {over_no} 台网络设备...')
blocked = False
try:
    api('/api/netdev/devices', {
        'name': f'配额自检-SW{over_no}-应当被拦截',
        'device_type': 'switch',
        'vendor': 'huawei',
        'host': '10.10.1.250',
        'username': 'admin',
        'password': 'pwd',
        'backup_commands': 'display current-configuration',
        'auto_test': False,
    })
    print(f'   ❌ 失败：第 {over_no} 台未被拦截！')
except urllib.error.HTTPError as err:
    body = json.loads(err.read().decode('utf-8'))
    blocked = True
    print(f'   ✓ 成功拦截！HTTP 状态码: {err.code}, 错误提示: {body.get("error")}')

assert blocked, f"第 {over_no} 台设备必须被拦截！"

# 4. 在授权系统 (8088) 生成绑定当前物理 MAC 的企业永久授权码
primary_mac = api('/api/system/license')['license']['primary_mac']
print('4. 本机物理 MAC 地址:', primary_mac)

gen_req = urllib.request.Request(
    'http://127.0.0.1:8088/api/license/generate',
    headers={'Content-Type': 'application/json'},
    data=json.dumps({'customer': '某某银行总行数据中心', 'mac': primary_mac,
                     'remark': '企业永久授权'}).encode('utf-8'))
lic_code = json.loads(urllib.request.urlopen(gen_req).read().decode('utf-8'))['data']['license_code']
print('   发号系统已签发永久授权码:', lic_code[:35], '...')

# 5. 在 AIDBM 激活该授权码
act_req = urllib.request.Request(
    BASE_URL + '/api/system/license/activate',
    headers={'Content-Type': 'application/json'},
    data=json.dumps({'license_code': lic_code}).encode('utf-8'))
act_data = json.loads(opener.open(act_req).read().decode('utf-8'))
print('5. 授权激活结果:', act_data['msg'])
print('   当前运行版本:', act_data['data']['edition_label'],
      '授权客户:', act_data['data']['customer'])

# 6. 解锁后再次添加超限设备 -> 必须成功！
print(f'6. 企业永久版模式下，再次添加第 {over_no} 台网络设备...')
res_6 = api('/api/netdev/devices', {
    'name': f'配额自检-SW{over_no}-解锁后添加',
    'device_type': 'switch',
    'vendor': 'huawei',
    'host': '10.10.1.250',
    'username': 'admin',
    'password': 'pwd',
    'backup_commands': 'display current-configuration',
    'auto_test': False,
})
print(f'   ✓ 解锁后第 {over_no} 台设备添加成功！{res_6}')

# 7. 验证配额接口状态
q_after = api('/api/system/license')
print('7. 解锁后网络设备配额状态:', q_after['quotas']['netdev'])
print('   is_unlimited:', q_after['quotas']['netdev']['is_unlimited'])

# 8. 测试数据库备份任务配额拦截
print(f'8. 测试数据库备份任务配额拦截 (社区版每类限 {FREE_LIMIT} 个)...')
api('/api/system/license/deactivate', {})
db.execute("DELETE FROM backup_tasks WHERE name LIKE '自检DB-%'")

cur_db = api('/api/system/license')['quotas']['database']['used']
for i in range(cur_db + 1, FREE_LIMIT + 1):
    api('/api/tasks', {
        'name': f'自检DB-{i}',
        'db_type': 'mysql',
        'biz_system': 'ERP系统',
        'host': '127.0.0.1',
        'port': 3306,
        'username': 'root',
        'password': 'pwd',
        'db_name': f'db_{i}',
        'backup_type': 'full',
    })
    print(f'   已添加第 {i} 个数据库备份任务 (<= {FREE_LIMIT} 允许)')

db_blocked = False
try:
    api('/api/tasks', {
        'name': f'自检DB-{over_no}-应当被拦截',
        'db_type': 'mysql',
        'biz_system': 'ERP系统',
        'host': '127.0.0.1',
        'port': 3306,
        'username': 'root',
        'password': 'pwd',
        'db_name': f'db_{over_no}',
        'backup_type': 'full',
    })
    print(f'   ❌ 失败：第 {over_no} 个数据库任务未被拦截！')
except urllib.error.HTTPError as err:
    body = json.loads(err.read().decode('utf-8'))
    db_blocked = True
    print(f'   ✓ 数据库任务配额超额成功拦截！HTTP 状态码: {err.code}, 提示: {body.get("error")}')

assert db_blocked, f"第 {over_no} 个数据库备份任务必须被拦截！"
db.execute("DELETE FROM backup_tasks WHERE name LIKE '自检DB-%'")

# 9. 重新激活企业永久授权，使系统保持在已授权的企业版就绪状态
opener.open(act_req)
print('9. 重新激活永久企业授权成功，系统处于正式企业版就绪状态！')
