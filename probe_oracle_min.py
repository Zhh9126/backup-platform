# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"E:\备份管理平台\backup_platform")
from core.ssh_hosts import get_host
from core.engines.file import _get_ssh_client, _ssh_exec_pipe, _ssh_exec

HOST = get_host(11, include_secret=True)
client = _get_ssh_client(HOST["host_key"])

def run(label, cmd, timeout=30):
    try:
        out, err, rc = _ssh_exec_pipe(client, cmd, timeout=timeout)
        print(f"--- {label} rc={rc} ---")
        print("OUT:", (out.decode('utf-8','replace') if isinstance(out,bytes) else out)[:800])
        print("ERR:", err[:400])
    except Exception as e:
        print(f"--- {label} EXC ---", repr(e))

# 1) 简单 su
run("su whoami", "bash -lc 'su - oracle -c \"whoami\"'")
# 2) oracle env
run("oracle env", "bash -lc 'su - oracle -c \"echo ORACLE_HOME=$ORACLE_HOME; which rman expdp sqlplus\"'")
# 3) sqlplus 直接命令（非脚本文件）
run("sqlplus select", "bash -lc 'su - oracle -c \"sqlplus -s / as sysdba <<< \\\"SELECT 1 FROM dual;\\\"\"'")
# 4) 把 sql 文件放到 oracle 可读目录再跑
sftp = client.open_sftp()
sql = "SELECT 1 FROM dual;\nEXIT;\n"
with sftp.open("/tmp/bkptest_probe.sql", "w") as f:
    f.write(sql)
# 改为 oracle 可写目录：/u01/app/oracle/backup 下
try:
    sftp.mkdir("/u01/app/oracle/backup")
except Exception:
    pass
with sftp.open("/u01/app/oracle/backup/probe.sql", "w") as f:
    f.write(sql)
try:
    sftp.chmod("/u01/app/oracle/backup/probe.sql", 0o644)
except Exception:
    pass
sftp.close()
run("sqlplus @file", "bash -lc 'su - oracle -c \"sqlplus -s / as sysdba @/u01/app/oracle/backup/probe.sql\"'")
client.close()
print("DONE")
