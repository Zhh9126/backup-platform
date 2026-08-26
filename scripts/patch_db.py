from pathlib import Path

p = Path(r'e:\备份管理平台\backup_platform\core\db.py')
text = p.read_text(encoding='utf-8')

marker = '''            except Exception:
                    pass  # 表刚建好或无存量数据，忽略

            conn.commit()'''

insert = '''            except Exception:
                    pass  # 表刚建好或无存量数据，忽略

            # 迁移：恢复校验策略与测试报告表
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS restore_verify_policies (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id             INTEGER NOT NULL,
                    name                TEXT,
                    recovery_pool       TEXT DEFAULT '',
                    schedule_type       TEXT DEFAULT 'manual',
                    cron_expr           TEXT,
                    interval_minutes    INTEGER,
                    clone_retention_min INTEGER DEFAULT 30,
                    enabled             INTEGER DEFAULT 1,
                    last_run_at         TEXT,
                    last_status         TEXT,
                    last_report_id      INTEGER,
                    created_at          TEXT,
                    updated_at          TEXT
                );
                CREATE TABLE IF NOT EXISTS restore_test_reports (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    policy_id    INTEGER,
                    task_id      INTEGER,
                    record_id    INTEGER,
                    db_type      TEXT,
                    status       TEXT,
                    duration_sec REAL,
                    message      TEXT,
                    cleaned      INTEGER DEFAULT 0,
                    created_at   TEXT,
                    finished_at  TEXT
                );
            """)
            for col, typedef in [("name", "TEXT"), ("last_report_id", "INTEGER")]:
                try:
                    conn.execute(f"ALTER TABLE restore_verify_policies ADD COLUMN {col} {typedef}")
                except Exception:
                    pass
            for col, typedef in [("finished_at", "TEXT")]:
                try:
                    conn.execute(f"ALTER TABLE restore_test_reports ADD COLUMN {col} {typedef}")
                except Exception:
                    pass

            conn.commit()'''

if marker not in text:
    print('MARKER NOT FOUND')
else:
    text = text.replace(marker, insert, 1)
    p.write_text(text, encoding='utf-8')
    print('OK')
