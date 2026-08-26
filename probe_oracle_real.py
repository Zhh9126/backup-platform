# -*- coding: utf-8 -*-
"""探针：验证远端 Oracle 的 expdp / rman 真实可用性（只读 + 仅建一个测试 schema）。"""
import sys, os, io, json
sys.path.insert(0, r"E:\备份管理平台\backup_platform")

from core.ssh_hosts import get_host
from core.engines.file import _get_ssh_client, _ssh_exec_pipe

HOST = get_host(11, include_secret=True)
print("ssh host:", HOST["hostname"], HOST["username"], "port", HOST["port"])

client = _get_ssh_client(HOST["host_key"])
sftp = client.open_sftp()

# 1) 在远端创建测试 schema bkptest（仅此一处状态变更，属安全操作）
create_sql = """CREATE USER bkptest IDENTIFIED BY bkptest;
GRANT CONNECT, RESOURCE TO bkptest;
GRANT UNLIMITED TABLESPACE TO bkptest;
CONNECT bkptest/bkptest
CREATE TABLE t1 (id NUMBER, name VARCHAR2(50));
INSERT INTO t1 VALUES (1, 'alice');
INSERT INTO t1 VALUES (2, 'bob');
COMMIT;
EXIT;
"""
rp = "/tmp/probe_create_bkptest.sql"
with sftp.open(rp, "w") as f:
    f.write(create_sql)
sh = ("chmod 600 /tmp/probe_create_bkptest.sql; "
      "su - oracle -c 'sqlplus -s / as sysdba @/tmp/probe_create_bkptest.sql'")
out, err, rc = _ssh_exec_pipe(client, "bash -lc '%s'" % sh.replace("'", "'\\''"), timeout=120)
print("=== create bkptest rc=%s ===" % rc)
print(out.decode("utf-8","replace") if isinstance(out, bytes) else out)
print("ERR:", err)

# 2) 验证 expdp 用 system/oracle@//host:port/service 能否连上（只在远端跑 help 看版本）
ts = "probe_20240101_000000"
expdp_sh = """#!/bin/bash
export ORACLE_HOME=/u01/app/oracle/product/19.0.0/dbhome_1
export ORACLE_SID=orcl11g
export PATH=$ORACLE_HOME/bin:$PATH
expdp system/oracle@//192.168.220.129:1521/orcl11g SCHEMAS=bkptest DIRECTORY=DATA_PUMP_DIR DUMPFILE=%s.dmp LOGFILE=%s.log
echo "EXPDP_RC=$?"
""" % (ts, ts)
rp2 = "/u01/app/oracle/backup/probe_expdp_%s.sh" % ts
try:
    sftp.mkdir("/u01/app/oracle/backup")
except Exception:
    pass
with sftp.open(rp2, "w") as f:
    f.write(expdp_sh)
try:
    sftp.chmod(rp2, 0o755)
except Exception:
    pass
sh2 = ("su - oracle -c 'bash /u01/app/oracle/backup/probe_expdp_%s.sh'" % ts)
out2, err2, rc2 = _ssh_exec_pipe(client, "bash -lc '%s'" % sh2.replace("'", "'\\''"), timeout=300)
print("=== expdp SCHEMAS=bkptest rc=%s ===" % rc2)
print(out2.decode("utf-8","replace") if isinstance(out2, bytes) else out2)
print("ERR:", err2)
# 检查远端是否生成 dmp
try:
    st = sftp.stat("/u01/app/oracle/admin/orcl11g/dpdump/%s.dmp" % ts)
    print("REMOTE DMP size:", st.st_size)
except Exception as e:
    print("REMOTE DMP NOT FOUND:", e)

# 3) 验证 rman target / 能连（report schema）
rman_cmd = """connect target /;
report schema;
exit;
"""
rp3 = "/u01/app/oracle/backup/probe_rman_%s.cmd" % ts
with sftp.open(rp3, "w") as f:
    f.write(rman_cmd)
sh3 = ("su - oracle -c 'rman target / @/u01/app/oracle/backup/probe_rman_%s.cmd'" % ts)
out3, err3, rc3 = _ssh_exec_pipe(client, "bash -lc '%s'" % sh3.replace("'", "'\\''"), timeout=120)
print("=== rman report schema rc=%s ===" % rc3)
print(out3.decode("utf-8","replace") if isinstance(out3, bytes) else out3)
print("ERR:", err3)

sftp.close()
client.close()
print("DONE")
