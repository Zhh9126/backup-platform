# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"E:\备份管理平台\backup_platform")
from core.ssh_hosts import get_host
from core.engines.file import _get_ssh_client, _ssh_exec_pipe

HOST = get_host(11, include_secret=True)
client = _get_ssh_client(HOST["host_key"])
sftp = client.open_sftp()
try:
    sftp.mkdir("/u01/app/oracle/backup")
except Exception:
    pass

ts = "probe2_20240101_000000"
# 创建 bkptest 并插入数据（oracle 可写目录）
create_sql = (
    "CREATE USER bkptest IDENTIFIED BY bkptest;\n"
    "GRANT CONNECT, RESOURCE TO bkptest;\n"
    "GRANT UNLIMITED TABLESPACE TO bkptest;\n"
    "CONNECT bkptest/bkptest\n"
    "CREATE TABLE t1 (id NUMBER, name VARCHAR2(50));\n"
    "INSERT INTO t1 VALUES (1, 'alice');\n"
    "INSERT INTO t1 VALUES (2, 'bob');\n"
    "COMMIT;\n"
    "EXIT;\n"
)
with sftp.open("/u01/app/oracle/backup/probe_create.sql", "w") as f:
    f.write(create_sql)
try:
    sftp.chmod("/u01/app/oracle/backup/probe_create.sql", 0o644)
except Exception:
    pass
sh = "su - oracle -c \"sqlplus -s / as sysdba @/u01/app/oracle/backup/probe_create.sql\""
out, err, rc = _ssh_exec_pipe(client, "bash -lc '%s'" % sh.replace("'", "'\\''"), timeout=120)
print("=== create bkptest rc=%s ===" % rc)
print(out.decode("utf-8","replace") if isinstance(out,bytes) else out)
print("ERR:", err)

# expdp SCHEMAS=bkptest 用 system/oracle@//host:port/service
expdp_sh = (
    "#!/bin/bash\n"
    "export ORACLE_HOME=/u01/app/oracle/product/19.0.0/dbhome_1\n"
    "export ORACLE_SID=orcl11g\n"
    "export PATH=$ORACLE_HOME/bin:$PATH\n"
    "expdp system/oracle@//192.168.220.129:1521/orcl11g SCHEMAS=bkptest "
    "DIRECTORY=DATA_PUMP_DIR DUMPFILE=%s.dmp LOGFILE=%s.log\n"
    "echo EXPDP_RC=$?\n" % (ts, ts)
)
with sftp.open("/u01/app/oracle/backup/probe_expdp.sh", "w") as f:
    f.write(expdp_sh)
try:
    sftp.chmod("/u01/app/oracle/backup/probe_expdp.sh", 0o755)
except Exception:
    pass
sh2 = "su - oracle -c \"bash /u01/app/oracle/backup/probe_expdp.sh\""
out2, err2, rc2 = _ssh_exec_pipe(client, "bash -lc '%s'" % sh2.replace("'", "'\\''"), timeout=300)
print("=== expdp SCHEMAS=bkptest rc=%s ===" % rc2)
print(out2.decode("utf-8","replace") if isinstance(out2,bytes) else out2)
print("ERR:", err2)
try:
    st = sftp.stat("/u01/app/oracle/admin/orcl11g/dpdump/%s.dmp" % ts)
    print("REMOTE DMP size:", st.st_size)
except Exception as e:
    print("REMOTE DMP NOT FOUND:", e)
sftp.close()
client.close()
print("DONE")
