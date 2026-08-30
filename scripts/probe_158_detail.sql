SET PAGESIZE 60 LINESIZE 200
PROMPT --- 归档目的地 ---
SELECT dest_id, status, destination, valid_now FROM v$archive_dest WHERE destination IS NOT NULL OR status != 'INACTIVE';
PROMPT --- 归档参数 ---
SHOW PARAMETER log_archive_dest
SHOW PARAMETER db_recovery
PROMPT --- FRA 占用 ---
SELECT name, space_limit/1048576 AS limit_mb, space_used/1048576 AS used_mb FROM v$recovery_file_dest;
PROMPT --- 归档日志统计 ---
SELECT COUNT(*) cnt, ROUND(SUM(blocks*block_size)/1048576,1) AS size_mb, MIN(first_time) oldest, MAX(first_time) newest FROM v$archived_log;
PROMPT --- Data Guard 配置 ---
SELECT dbid, name, db_unique_name, database_role, open_mode, switchover_status FROM v$database;
PROMPT --- DATA_PUMP_DIR ---
SELECT directory_name, directory_path FROM dba_directories WHERE directory_name IN ('DATA_PUMP_DIR','BACKUP_DIR');
PROMPT --- 表空间 ---
SELECT tablespace_name, ROUND(SUM(bytes)/1048576,0) AS used_mb FROM dba_data_files GROUP BY tablespace_name;
EXIT;
