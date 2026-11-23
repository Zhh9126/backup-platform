"""Persistent multi-instance backup execution queue."""
import os, socket, threading, time
from core import db

OWNER=f"{socket.gethostname()}:{os.getpid()}"
_WAKE=threading.Event()

def ensure_schema():
    db.execute("""CREATE TABLE IF NOT EXISTS task_execution_queue (
      id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, backup_type TEXT DEFAULT 'full',
      operator TEXT DEFAULT '', status TEXT NOT NULL, owner TEXT DEFAULT '',
      queued_at TEXT NOT NULL, started_at TEXT DEFAULT '', finished_at TEXT DEFAULT '',
      lease_until REAL DEFAULT 0, error TEXT DEFAULT ''
    )""")
    try: db.execute("CREATE INDEX IF NOT EXISTS idx_task_queue_status ON task_execution_queue(status, id)")
    except Exception: pass

def enqueue(task_id, backup_type='full', operator=''):
    ensure_schema()
    row=db.query_one("SELECT id,status FROM task_execution_queue WHERE task_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",(int(task_id),))
    if row: return int(row['id']), True
    qid=db.execute("INSERT INTO task_execution_queue(task_id,backup_type,operator,status,queued_at) VALUES(?,?,?,?,?)",(int(task_id),str(backup_type or 'full'),str(operator or ''),'queued',db.now_iso()))
    _WAKE.set(); return int(qid), False

def claim(owner=OWNER, ttl=86400):
    ensure_schema(); now=time.time()
    # reclaim expired workers, then atomically claim one queued row
    db.execute("UPDATE task_execution_queue SET status='queued',owner='',lease_until=0 WHERE status='running' AND lease_until>0 AND lease_until<?",(now,))
    row=db.query_one("SELECT * FROM task_execution_queue WHERE status='queued' ORDER BY id LIMIT 1")
    if not row: return None
    rc=db.execute_rowcount("UPDATE task_execution_queue SET status='running',owner=?,started_at=?,lease_until=? WHERE id=? AND status='queued'",(owner,db.now_iso(),now+ttl,int(row['id'])))
    return db.query_one("SELECT * FROM task_execution_queue WHERE id=?",(int(row['id']),)) if rc else None

def complete(qid, ok=True, error=''):
    db.execute("UPDATE task_execution_queue SET status=?,finished_at=?,error=?,lease_until=0 WHERE id=?",('done' if ok else 'failed',db.now_iso(),str(error or '')[:500],int(qid)))

def wake(): _WAKE.set()
def wait(timeout=2): _WAKE.wait(timeout); _WAKE.clear()
