#!/bin/bash
# ======================================================================
# 开启 137 达梦归档模式（用户已确认同意）
# 用法：本地已配置 SSH 免密时直接 bash scripts/enable_dm_archive_137.sh
#       或复制脚本到 137 执行：scp + bash
# 步骤：1) dm.ini 备份并设 ARCH_INI=1   2) 新建 dmarch.ini
#       3) disql SHUTDOWN IMMEDIATE     4) 重启 dmserver 并验证
# ======================================================================
set -e
export SSH_H='192.168.220.137'
export SSH_U='root'
export SSH_P='Zhh@190226'
PY=${PY:-.venv/bin/python}

$PY - "$SSH_H" "$SSH_U" "$SSH_P" <<'PYEOF'
import sys, paramiko
host, user, pwd = sys.argv[1], sys.argv[2], sys.argv[3]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, 22, user, pwd, timeout=10)
s = c.open_sftp()

step1 = r'''#!/bin/bash
set -e
cp -n /dm/data/PROD/dm.ini /dm/data/PROD/dm.ini.bak_before_arch 2>/dev/null || true
sed -i 's/= 0                     #dmarch.ini/= 1                     #dmarch.ini/' /dm/data/PROD/dm.ini
grep ARCH_INI /dm/data/PROD/dm.ini
mkdir -p /dm/data/PROD/arch && chown dmdba /dm/data/PROD/arch
printf '[ARCHIVE_LOCAL1]\nARCH_TYPE = LOCAL\nARCH_DEST = /dm/data/PROD/arch\nARCH_FILE_SIZE = 128\nARCH_SPACE_LIMIT = 10240\n' > /dm/data/PROD/dmarch.ini
chown dmdba /dm/data/PROD/dmarch.ini
echo "=== step1 config done ==="
'''
with s.open('/tmp/dm_arch_step1.sh', 'w') as f:
    f.write(step1)
i,o,e = c.exec_command('bash /tmp/dm_arch_step1.sh', timeout=60)
print(o.read().decode())
err = e.read().decode()
if err: print('ERR1:', err[:300])

# 优雅关闭（SQL 方式）
with s.open('/tmp/dm_down.sql', 'w') as f:
    f.write("SHUTDOWN IMMEDIATE;\nexit\n")
i,o,e = c.exec_command(
    "timeout 90 /dm/dbms/bin/disql SYSDBA/'\"Ceshi@5235\"'@localhost:5236 \\`/tmp/dm_down.sql",
    timeout=150)
print('SHUTDOWN OUT:', o.read().decode()[-250:])

step3 = r'''#!/bin/bash
sleep 5
ps -eo cmd= | grep '[d]mserver' || echo "instance confirmed stopped"
su - dmdba -c "nohup /dm/dbms/bin/dmserver path=/dm/data/PROD/dm.ini -noconsole >/dm/data/PROD/dmserver_restart.log 2>&1 &"
sleep 20
ps -eo cmd= | grep '[d]mserver' | head -1
printf "SET HEADING OFF\nSELECT 'ARCH_MODE='||ARCH_MODE FROM V\$DATABASE;\nSP_SET_PARA_VALUE(1,'RLOG_APPEND_LOGIC',2);\nSELECT 'RLOG_APPEND_LOGIC='||PARA_VALUE FROM V\$DM_INI WHERE PARA_NAME='RLOG_APPEND_LOGIC';\nexit\n" > /tmp/dm_arch_verify.sql
timeout 40 /dm/dbms/bin/disql SYSDBA/'"Ceshi@5235"'@localhost:5236 \`/tmp/dm_arch_verify.sql
echo "ARCH_DIR:"; ls /dm/data/PROD/arch/ 2>/dev/null | head -3
'''
with s.open('/tmp/dm_arch_step3.sh', 'w') as f:
    f.write(step3)
i,o,e = c.exec_command('bash /tmp/dm_arch_step3.sh', timeout=300)
print(o.read().decode())
err = e.read().decode()
if err: print('ERR3:', err[:300])
c.close()
PYEOF
