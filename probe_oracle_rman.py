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

# 检查 su - oracle 下的环境变量
probe_env = "echo ORACLE_HOME=$ORACLE_HOME; echo ORACLE_SID=$ORACLE_SID; echo PATH=$PATH\n"
with sftp.open("/u01/app/oracle/backup/probe_env.sh", "w") as f:
    f.write(probe_env)
try:
    sftp.chmod("/u01/app/oracle/backup/probe_env.sh", 0o644)
except Exception:
    pass

out, err, rc = _ssh_exec_pipe(client, "bash -lc 'su - oracle -c \"bash /u01/app/oracle/backup/probe_env.sh\"'", timeout=30)
print("=== env under su - oracle rc=%s ===" % rc)
print(out.decode("utf-8","replace") if isinstance(out,bytes) else out)
print("ERR:", err)

# rman target / report schema
rman_cmd = "connect target /;\nreport schema;\nexit;\n"
with sftp.open("/u01/app/oracle/backup/probe_rman.cmd", "w") as f:
    f.write(rman_cmd)
try:
    sftp.chmod("/u01/app/oracle/backup/probe_rman.cmd", 0o644)
except Exception:
    pass
sh = "su - oracle -c \"rman target / @/u01/app/oracle/backup/probe_rman.cmd\""
out2, err2, rc2 = _ssh_exec_pipe(client, "bash -lc '%s'" % sh.replace("'", "'\\''"), timeout=120)
print("=== rman target / report schema rc=%s ===" % rc2)
print(out2.decode("utf-8","replace") if isinstance(out2,bytes) else out2)
print("ERR:", err2)
sftp.close()
client.close()
print("DONE")
