"""Persistent acceptance history, separate from managed recovery points."""
import json
import math
from pathlib import Path
import re
from core import db

ROOT = Path(__file__).resolve().parents[1]
STATUSES = {"passed","partial","failed","skipped","running"}
LEVELS = {"external_real","platform_real","contract","regression"}
CATEGORIES = {"vm","database","clone","storage","agent","recovery","system"}

def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS acceptance_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_key TEXT NOT NULL UNIQUE, category TEXT NOT NULL,
        title TEXT NOT NULL, subject TEXT DEFAULT '', status TEXT NOT NULL,
        evidence_level TEXT NOT NULL, started_at TEXT, finished_at TEXT,
        recorded_at TEXT NOT NULL, summary TEXT DEFAULT '', boundary TEXT DEFAULT '',
        evidence_json TEXT NOT NULL, recorded_by TEXT DEFAULT '')""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_acceptance_category_time ON acceptance_runs(category,finished_at,id)")

def _redact(value):
    if isinstance(value,dict):
        return {k: ("[REDACTED]" if re.search(r"password|secret|authorization|access_token|private_key",k,re.I)
                    else _redact(v)) for k,v in value.items()}
    if isinstance(value,list):
        return [_redact(v) for v in value]
    if isinstance(value,str):
        value=re.sub(r"gh[pousr]_[A-Za-z0-9_]+","[REDACTED]",value)
        value=re.sub(r"(?i)(password|token|secret|authorization)(\s*[:=]\s*)([^\s,;]+)",
                     r"\1\2[REDACTED]",value)
        value=re.sub(r"(?i)(-gp\s+)(\S+)",r"\1[REDACTED]",value)
    return value

def save_run(payload, operator="local-cli"):
    data=dict(payload)
    for field,allowed in (("status",STATUSES),("evidence_level",LEVELS),("category",CATEGORIES)):
        if data.get(field) not in allowed:
            raise ValueError("无效测试字段: "+field)
    for key in ("source_key","title"):
        if not str(data.get(key) or "").strip():
            raise ValueError("缺少测试字段: "+key)
    evidence=_redact(data.get("evidence") or {})
    if not isinstance(evidence,dict):
        raise ValueError("evidence 必须是对象")
    encoded=json.dumps(evidence,ensure_ascii=False)
    if len(encoded.encode())>2*1024*1024:
        raise ValueError("单次测试证据不能超过 2 MiB")
    if data["evidence_level"] in {"external_real","platform_real"} and data["status"]=="passed":
        checks=evidence.get("checks") or []
        if not checks or not any(c.get("status")=="passed" for c in checks):
            raise ValueError("实机测试通过必须提供已通过的校验项")
        if any(c.get("status")=="failed" for c in checks):
            raise ValueError("含失败校验项的实机测试不能登记为全部通过")
    row={k: str(data.get(k) or "") for k in
         ("source_key","category","title","subject","status","evidence_level","started_at","finished_at","summary","boundary")}
    row={k:_redact(v) for k,v in row.items()}
    row.update(recorded_at=db.now_iso(),evidence_json=encoded,recorded_by=operator)
    existing=db.query_one("SELECT id FROM acceptance_runs WHERE source_key=?",(row["source_key"],))
    if existing:
        rid=existing["id"]
        cols=[k for k in row if k not in ("source_key","recorded_at")]
        db.execute("UPDATE acceptance_runs SET "+",".join(k+"=?" for k in cols)+" WHERE id=?",
                   tuple(row[k] for k in cols)+(rid,))
        return rid
    try:
        return db.execute("INSERT INTO acceptance_runs ("+",".join(row)+") VALUES ("+
                          ",".join("?" for _ in row)+")",tuple(row.values()))
    except Exception:
        existing=db.query_one("SELECT id FROM acceptance_runs WHERE source_key=?",(row["source_key"],))
        if existing:
            return existing["id"]
        raise

def list_runs(category="",status="",q="",page=1,size=20):
    page=max(1,int(page));size=max(1,min(100,int(size)))
    where=[];args=[]
    if category:
        if category not in CATEGORIES: raise ValueError("未知测试分类")
        where.append("category=?");args.append(category)
    if status:
        if status not in STATUSES: raise ValueError("未知测试状态")
        where.append("status=?");args.append(status)
    if q:
        where.append("(title LIKE ? OR subject LIKE ? OR summary LIKE ?)")
        args.extend(["%"+q[:200]+"%"]*3)
    clause=" WHERE "+" AND ".join(where) if where else ""
    total=db.query_one("SELECT COUNT(*) AS n FROM acceptance_runs"+clause,tuple(args))["n"]
    columns="id,category,title,subject,status,evidence_level,started_at,finished_at,recorded_at,summary,boundary"
    rows=db.query("SELECT "+columns+" FROM acceptance_runs"+clause+
                  " ORDER BY COALESCE(NULLIF(finished_at,''),recorded_at) DESC,id DESC LIMIT ? OFFSET ?",
                  tuple(args)+(size,(page-1)*size))
    counts={s:0 for s in STATUSES}
    count_where=" WHERE category=?" if category else ""
    for r in db.query("SELECT status,COUNT(*) AS n FROM acceptance_runs"+count_where+" GROUP BY status",
                      (category,) if category else ()):
        counts[r["status"]]=r["n"]
    return {"items":rows,"total":total,"page":page,"pages":max(1,math.ceil(total/size)),"counts":counts}

def get_run(run_id):
    row=db.query_one("SELECT * FROM acceptance_runs WHERE id=?",(int(run_id),))
    if not row: return None
    row["evidence"]=json.loads(row.pop("evidence_json"))
    return row

def read_evidence(run_id,index):
    row=get_run(run_id)
    if not row: raise ValueError("测试记录不存在")
    files=row["evidence"].get("files") or []
    if index<0 or index>=len(files): raise ValueError("证据不存在")
    item=files[index]
    if item.get("kind") not in {"log","report"}:
        raise ValueError("备份归档仅展示路径和校验值，不提供网页下载")
    path=(ROOT/item.get("path","")).resolve()
    if not any(path.is_relative_to((ROOT/d).resolve()) for d in ("build","docs")):
        raise ValueError("证据路径不在允许目录内")
    if path.suffix.lower() not in {".log",".json",".xml",".md",".txt"} or not path.is_file():
        raise ValueError("可读证据文件不存在")
    with path.open("r",encoding="utf-8-sig",errors="replace") as f:
        body=f.read(65537)
    return {"text":_redact(body[:65536]),"truncated":len(body)>65536,"name":path.name}
