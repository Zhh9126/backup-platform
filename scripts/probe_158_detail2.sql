SET PAGESIZE 60 LINESIZE 200 ECHO OFF FEEDBACK ON
SELECT dest_id, status, destination, valid_now FROM v$archive_dest WHERE destination IS NOT NULL;
SELECT COUNT(*) cnt, ROUND(SUM(blocks*block_size)/1048576,1) AS size_mb, TO_CHAR(MIN(first_time),'YYYY-MM-DD HH24:MI') oldest, TO_CHAR(MAX(first_time),'YYYY-MM-DD HH24:MI') newest FROM v$archived_log;
SELECT directory_name, directory_path FROM dba_directories WHERE directory_name IN ('DATA_PUMP_DIR','BACKUP_DIR');
SELECT tablespace_name, ROUND(SUM(bytes)/1048576,0) AS used_mb FROM dba_data_files GROUP BY tablespace_name;
SELECT member FROM v$logfile WHERE ROWNUM<=4;
SELECT value FROM v$parameter WHERE name='log_archive_dest_1';
SELECT value FROM v$parameter WHERE name='log_archive_dest_2';
EXIT;
