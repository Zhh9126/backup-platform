"""Create durable data for a VM recovery acceptance run, beneath this repository."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "build/vm-real-20261120"
TARGET.mkdir(parents=True, exist_ok=True)

def durable_write(path, data):
    temporary = path.with_name(path.name + ".pending")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

durable_write(TARGET / "payload.bin", os.urandom(256 * 1024))
conn = sqlite3.connect(str(TARGET / "canary.db"))
conn.execute("PRAGMA synchronous=FULL")
conn.execute("CREATE TABLE IF NOT EXISTS proof(id INTEGER PRIMARY KEY, value TEXT)")
conn.executemany("INSERT OR REPLACE INTO proof VALUES (?, ?)", [
    (i, hashlib.sha256(("vm-recovery-%s" % i).encode()).hexdigest())
    for i in range(10000)
])
conn.commit()
assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
assert conn.execute("SELECT count(*) FROM proof").fetchone()[0] == 10000
conn.close()
expected = {"test": "vm-real-20261120", "rows": 10000, "files": {
    name: hashlib.sha256((TARGET / name).read_bytes()).hexdigest()
    for name in ("payload.bin", "canary.db")
}}
durable_write(TARGET / "expected.json", json.dumps(expected, indent=2).encode())
if hasattr(os, "sync"):
    os.sync()
print("Durable canary prepared under", TARGET)
