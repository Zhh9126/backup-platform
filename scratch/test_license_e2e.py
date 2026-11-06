# -*- coding: utf-8 -*-
import sys, os
from pathlib import Path
BASE_DIR = str(Path(__file__).resolve().parent.parent)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import urllib.request
import urllib.parse
import json
import http.cookiejar
from core import db

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

# 1. 登录 AIDBM
login_data = urllib.parse.urlencode({'username': 'admin', 'password': 'admin123'}).encode('utf-8')
login_res = opener.open('http://127.0.0.1:8080/login', data=login_data)
print('1. 登录成功, status:', login_res.status)

# 2. 注销可能残留的授权，回到社区免费版
deact_req = urllib.request.Request(
    'http://127.0.0.1:8080/api/system/license/deactivate',
    data=b'{}',
    headers={'Content-Type': 'application/json'}
)
opener.open(deact_req)

# 清理历史测试设备
db.execute("DELETE FROM netdev_devices WHERE name LIKE '配额自检-%'")

# 查询当前网络设备数量
q_res = json.loads(opener.open('http://127.0.0.1:8080/api/system/license').read().decode('utf-8'))['data']
cur_netdev = q_res['quotas']['netdev']['used']
print('2. 社区免费版初始网络设备数量:', cur_netdev, '当前版本:', q_res['license']['edition_label'])

# 补齐到 5 台
for i in range(cur_netdev + 1, 6):
    req = urllib.request.Request(
        'http://127.0.0.1:8080/api/netdev/devices',
        headers={'Content-Type': 'application/json'},
        data=json.dumps({
            'name': f'配额自检-SW{i}',
            'device_type': 'switch',
            'vendor': 'huawei',
            'host': f'10.10.1.{i}',
            'username': 'admin',
            'password': 'pwd',
            'backup_commands': 'display current-configuration',
            'auto_test': False
        }).encode('utf-8')
    )
    res = opener.open(req)
    print(f'   已添加第 {i} 台网络设备 (<=5 台允许): status={res.status}')

# 3. 免费版尝试添加第 6 台网络设备 -> 必须被 403 阻断！
print('3. 尝试在社区免费版中添加第 6 台网络设备...')
blocked = False
try:
    req_6 = urllib.request.Request(
        'http://127.0.0.1:8080/api/netdev/devices',
        headers={'Content-Type': 'application/json'},
        data=json.dumps({
            'name': '配额自检-SW6-应当被拦截',
            'device_type': 'switch',
            'vendor': 'huawei',
            'host': '10.10.1.6',
            'username': 'admin',
            'password': 'pwd',
            'backup_commands': 'display current-configuration',
            'auto_test': False
        }).encode('utf-8')
    )
    opener.open(req_6)
    print('   ❌ 失败：第 6 台未被拦截！')
except urllib.error.HTTPError as err:
    body = json.loads(err.read().decode('utf-8'))
    blocked = True
    print(f'   ✓ 成功拦截！HTTP 状态码: {err.code}, 错误提示: {body.get("error")}')

assert blocked, "第 6 台设备必须被拦截！"

# 4. 在授权系统 (8088) 生成绑定当前物理 MAC 的企业永久授权码
lic_info = json.loads(opener.open('http://127.0.0.1:8080/api/system/license').read().decode('utf-8'))['data']
primary_mac = lic_info['license']['primary_mac']
print('4. 本机物理 MAC 地址:', primary_mac)

gen_req = urllib.request.Request(
    'http://127.0.0.1:8088/api/license/generate',
    headers={'Content-Type': 'application/json'},
    data=json.dumps({'customer': '某某银行总行数据中心', 'mac': primary_mac, 'remark': '企业永久授权'}).encode('utf-8')
)
lic_code = json.loads(urllib.request.urlopen(gen_req).read().decode('utf-8'))['data']['license_code']
print('   发号系统已签发永久授权码:', lic_code[:35], '...')

# 5. 在 AIDBM 激活该授权码
act_req = urllib.request.Request(
    'http://127.0.0.1:8080/api/system/license/activate',
    headers={'Content-Type': 'application/json'},
    data=json.dumps({'license_code': lic_code}).encode('utf-8')
)
act_res = opener.open(act_req)
act_data = json.loads(act_res.read().decode('utf-8'))
print('5. 授权激活结果:', act_data['msg'])
print('   当前运行版本:', act_data['data']['edition_label'], '授权客户:', act_data['data']['customer'])

# 6. 解锁后再添加第 6 台设备 -> 必须成功！
print('6. 企业永久版模式下，再次添加第 6 台网络设备...')
req_6_after = urllib.request.Request(
    'http://127.0.0.1:8080/api/netdev/devices',
    headers={'Content-Type': 'application/json'},
    data=json.dumps({
        'name': '配额自检-SW6-解锁后添加',
        'device_type': 'switch',
        'vendor': 'huawei',
        'host': '10.10.1.6',
        'username': 'admin',
        'password': 'pwd',
        'backup_commands': 'display current-configuration',
        'auto_test': False
    }).encode('utf-8')
)
res_6 = opener.open(req_6_after)
print(f'   ✓ 解锁后第 6 台设备添加成功！status={res_6.status}')

# 7. 验证配额接口状态
q_after = json.loads(opener.open('http://127.0.0.1:8080/api/system/license').read().decode('utf-8'))['data']
print('7. 解锁后网络设备配额状态:', q_after['quotas']['netdev'])
print('   is_unlimited:', q_after['quotas']['netdev']['is_unlimited'])

# 8. 测试数据库备份任务配额拦截
print('8. 测试数据库备份任务配额拦截 (社区版限 5 个)...')
# 先注销回免费版
opener.open(urllib.request.Request('http://127.0.0.1:8080/api/system/license/deactivate', data=b'{}', headers={'Content-Type': 'application/json'}))
db.execute("DELETE FROM backup_tasks WHERE name LIKE '自检DB-%'")

q_now = json.loads(opener.open('http://127.0.0.1:8080/api/system/license').read().decode('utf-8'))['data']
cur_db = q_now['quotas']['database']['used']
for i in range(cur_db + 1, 6):
    req = urllib.request.Request(
        'http://127.0.0.1:8080/api/tasks',
        headers={'Content-Type': 'application/json'},
        data=json.dumps({
            'name': f'自检DB-{i}',
            'db_type': 'mysql',
            'biz_system': 'ERP系统',
            'host': '127.0.0.1',
            'port': 3306,
            'username': 'root',
            'password': 'pwd',
            'db_name': f'db_{i}',
            'backup_type': 'full'
        }).encode('utf-8')
    )
    opener.open(req)
    print(f'   已添加第 {i} 个数据库备份任务 (<=5 允许)')

db_blocked = False
try:
    req_db6 = urllib.request.Request(
        'http://127.0.0.1:8080/api/tasks',
        headers={'Content-Type': 'application/json'},
        data=json.dumps({
            'name': '自检DB-6-应当被拦截',
            'db_type': 'mysql',
            'biz_system': 'ERP系统',
            'host': '127.0.0.1',
            'port': 3306,
            'username': 'root',
            'password': 'pwd',
            'db_name': 'db_6',
            'backup_type': 'full'
        }).encode('utf-8')
    )
    opener.open(req_db6)
    print('   ❌ 失败：第 6 个数据库任务未被拦截！')
except urllib.error.HTTPError as err:
    body = json.loads(err.read().decode('utf-8'))
    db_blocked = True
    print(f'   ✓ 数据库任务配额超额成功拦截！HTTP 状态码: {err.code}, 提示: {body.get("error")}')

assert db_blocked, "第 6 个数据库备份任务必须被拦截！"
db.execute("DELETE FROM backup_tasks WHERE name LIKE '自检DB-%'")

# 9. 重新激活企业永久授权，使系统保持在已授权的企业版就绪状态
opener.open(act_req)
print('9. 重新激活永久企业授权成功，系统处于正式企业版就绪状态！')
