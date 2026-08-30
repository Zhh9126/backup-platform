#!/bin/bash
# o1 探针：158 环境与 Oracle 实例状态（只读探测，无状态变更）
echo "===== 1. OS / 资源 ====="
hostname; uname -m
grep -E '^(NAME|VERSION)=' /etc/os-release | head -2
echo "-- CPU/MEM --"
nproc; free -h | head -2
echo "-- DISK --"
df -h | grep -vE 'tmpfs|overlay|shm' | head -12

echo ""
echo "===== 2. Oracle 安装 ====="
echo "-- /etc/oratab --"
cat /etc/oratab 2>/dev/null
echo "-- 进程(pmon/tns) --"
ps -ef | grep -E 'pmon|smon|tnslsnr' | grep -v grep
echo "-- ORACLE_HOME 目录 --"
for oh in /u01/app/oracle/product/*/dbhome* /u01/app/oracle/product/*/*; do
  [ -d "$oh" ] && [ -x "$oh/bin/sqlplus" ] && echo "$oh"
done 2>/dev/null | sort -u
echo "-- 备份工具 --"
for oh in $(awk -F: '/^[^#]/ {print $2}' /etc/oratab 2>/dev/null | sort -u); do
  for t in sqlplus rman expdp impdp exp imp; do
    [ -x "$oh/bin/$t" ] && echo "$oh/bin/$t OK" || echo "$oh/bin/$t MISSING"
  done
done

echo ""
echo "===== 3. 监听器状态 ====="
for oh in $(awk -F: '/^[^#]/ {print $2}' /etc/oratab 2>/dev/null | sort -u); do
  su - oracle -c "export ORACLE_HOME=$oh; \$ORACLE_HOME/bin/lsnrctl status" 2>&1 | grep -E 'LSNRCTL|Listener Parameter|Listening Endpoints|STATUS|Instance|Service' | head -15
done

echo ""
echo "===== 4. 实例状态 ====="
for sid in $(awk -F: '/^[^#].*:Y|^[^#].*:N/ {print $1}' /etc/oratab 2>/dev/null); do
  oh=$(awk -F: -v s=$sid '$1==s {print $2}' /etc/oratab)
  echo "--- SID=$sid ---"
  su - oracle -c "export ORACLE_HOME=$oh ORACLE_SID=$sid; \$ORACLE_HOME/bin/sqlplus -s / as sysdba" <<'EOSQL' 2>&1
SET PAGESIZE 100 LINESIZE 200
SELECT instance_name, status, version, instance_role FROM v$instance;
SELECT name, open_mode, database_role, log_mode, force_logging, dbid, created FROM v$database;
SELECT platform_name FROM v$database;
SELECT protection_mode, protection_level FROM v$archive_dest WHERE status='VALID' AND ROWNUM<=3;
SELECT COUNT(*) AS archivelog_cnt FROM v$archived_log;
SHOW PARAMETER db_name
SHOW PARAMETER db_recovery
SELECT name, total_mb, free_mb FROM v$asm_diskgroup;
EOSQL
  echo "--- PDB ---"
  su - oracle -c "export ORACLE_HOME=$oh ORACLE_SID=$sid; \$ORACLE_HOME/bin/sqlplus -s / as sysdba" <<'EOSQL' 2>&1
SET PAGESIZE 50 LINESIZE 200
SELECT con_id, name, open_mode FROM v$pdbs;
EOSQL
done

echo ""
echo "===== 5. Data Guard / RAC 线索 ====="
ls -d /u01/app/*/grid* /u01/app/grid* 2>/dev/null
ps -ef | grep -E 'crsd|ohasd|cssd' | grep -v grep | head -5
for sid in $(awk -F: '/^[^#].*:Y|^[^#].*:N/ {print $1}' /etc/oratab 2>/dev/null); do
  oh=$(awk -F: -v s=$sid '$1==s {print $2}' /etc/oratab)
  su - oracle -c "export ORACLE_HOME=$oh ORACLE_SID=$sid; \$ORACLE_HOME/bin/sqlplus -s / as sysdba" <<'EOSQL' 2>&1
SET PAGESIZE 30 LINESIZE 200
SELECT database_role, db_unique_name, open_mode FROM v$database;
SELECT status, error FROM v$archive_dest WHERE destination IS NOT NULL;
EOSQL
done
echo "===== PROBE DONE ====="
