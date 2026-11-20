"""Disposable HTTP/UI acceptance environment. File and S3 data are real; VM provider is a contract stub."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "build/integration-20261120"
RUN = Path(tempfile.mkdtemp(prefix="recovery-http-", dir=BASE))
for folder in ("instance", "backups", "logs", "tmp"):
    (RUN / folder).mkdir()
os.environ.update(INSTANCE_DIR=str(RUN / "instance"), META_DB_PATH=str(RUN / "instance/meta.db"),
                  META_DB_BACKEND="sqlite", BACKUP_ROOT=str(RUN / "backups"), LOG_DIR=str(RUN / "logs"),
                  TMPDIR=str(RUN / "tmp"), AIDBM_RANDOM_ADMIN_PASSWORD="1",
                  CODEBUDDY_SAFE_DELETE_ENABLED="0", SCHEDULER_ENABLED="0", DEMO_MODE="off")
sys.path.insert(0, str(ROOT))
import requests
from werkzeug.serving import make_server
from app import create_app
from core import db, models
from core.engines.base import BackupResult
import core.vm as vm

OBJECTS = {}
class S3(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_HEAD(self):
        parts = unquote(urlsplit(self.path).path).lstrip("/").split("/", 1)
        if len(parts) == 1 or not parts[1]:
            self.send_response(200); self.end_headers(); return
        data = OBJECTS.get(tuple(parts))
        self.send_response(200 if data is not None else 404)
        if data is not None:
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", '"%s"' % hashlib.md5(data).hexdigest())
        self.end_headers()
    def do_PUT(self):
        assert self.headers.get("Authorization", "").startswith("AWS4-HMAC-SHA256")
        bucket, key = unquote(urlsplit(self.path).path).lstrip("/").split("/", 1)
        OBJECTS[(bucket, key)] = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

def task(kind, name, mode="logical", bucket="", extra=None):
    return db.execute(
        "INSERT INTO backup_tasks(name,db_type,host,port,username,password,db_name,backup_type,backup_mode,enabled,extra_options) "
        "VALUES (?,?,?,?,?,?,?,'full',?,0,?)",
        (name,kind,"127.0.0.1",22,"qa-user",db.encrypt_secret("qa-only"),bucket,mode,json.dumps(extra or {})))

def record(tid, kind, path, mode):
    return models.create_record({"task_id":tid,"db_type":kind,"backup_type":"full","backup_mode":mode,
        "status":"success","started_at":db.now_iso(),"finished_at":db.now_iso(),"backup_path":str(path),
        "checksum":db.sha256_file(str(path)),"size_bytes":path.stat().st_size})

def main():
    args = argparse.ArgumentParser()
    args.add_argument("--serve", action="store_true")
    options = args.parse_args()
    app = create_app()
    s3 = ThreadingHTTPServer(("127.0.0.1", 0), S3)
    threading.Thread(target=s3.serve_forever, daemon=True).start()
    source = RUN / "payload.txt"
    source.write_text("Recovery acceptance data.\n" * 100)
    files = RUN / "backups/files.tar.gz"
    with tarfile.open(files, "w:gz") as archive:
        archive.add(source, arcname="payload.txt")
    file_task = task("file", "验收样例 · 文件目录", "file", str(RUN))
    file_record = record(file_task, "file", files, "file")
    oracle_task = task("oracle", "界面样例 · Oracle 物理备份", "physical", "sample-db")
    oracle_record = record(oracle_task, "oracle", files, "physical")
    stage = RUN / "object-package"
    (stage / "objects").mkdir(parents=True)
    content = {"a.txt":b"first object", "中文/备份.txt":"对象恢复内容".encode()}
    entries = []
    for n,(key, data) in enumerate(content.items()):
        relative = "objects/%d.bin" % n
        (stage / relative).write_bytes(data)
        entries.append({"key":key,"path":relative,"size":len(data),
                        "sha256":hashlib.sha256(data).hexdigest(),"last_modified":db.now_iso()})
    (stage / "manifest.json").write_text(json.dumps({"version":1,"backup_type":"full","bucket":"original",
                                                    "provider":"obs","objects":entries,"deleted":[]}))
    archive_path = RUN / "backups/obs.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(stage / "manifest.json", arcname="manifest.json")
        archive.add(stage / "objects", arcname="objects")
    extra = {"os_provider":"obs","os_addressing":"path","os_secure":False,"os_region":"cn-north-4"}
    object_task = task("object_storage", "验收样例 · OBS 对象", "object", "original", extra)
    target_task = task("object_storage", "验收样例 · 灾备对象连接", "object", "destination", extra)
    for tid in (object_task, target_task):
        db.execute("UPDATE backup_tasks SET host=?,port=? WHERE id=?",
                   ("http://127.0.0.1:%d" % s3.server_port,s3.server_port,tid))
    object_record = record(object_task, "object_storage", archive_path, "object")
    vm_task = task("vm", "界面样例 · VM", "vm")
    vm_record = record(vm_task, "vm", files, "vm")
    hv = db.execute("INSERT INTO vm_hypervisors(name,provider,endpoint) VALUES ('验收平台','pve','https://127.0.0.1:1')")
    protected = db.execute("INSERT INTO vm_protected(hypervisor_id,task_id,vm_ref,vm_name,node) "
                           "VALUES (?,?,?,?,?)", (hv,vm_task,"qa-vm","界面样例 · 应用虚拟机","qa-node"))
    rp = db.execute("INSERT INTO vm_recovery_points(vm_id,task_id,record_id,pit_at,artifact_path,size_bytes) VALUES (?,?,?,?,?,?)",
                    (protected,vm_task,vm_record,db.now_iso(),str(files),files.stat().st_size))
    vm._engine_for = lambda row: type("StubVMEngine", (), {"restore": lambda self,*a,**kw:
        BackupResult(success=True,message="验收桩完成，未操作真实虚拟机",target_ref="qa://new-vm")})()
    server = make_server("0.0.0.0" if options.serve else "127.0.0.1", 18080 if options.serve else 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % server.server_port
    session = requests.Session(); session.trust_env=False
    username,password=(RUN/"instance/initial_admin_password.txt").read_text().strip().split(" / ",1)
    def request(method, path, payload=None, status=200):
        reply=session.request(method,url+path,json=payload,timeout=45)
        assert reply.status_code==status,(path,reply.status_code,reply.text[:600])
        return reply.json()
    result={}
    try:
        assert request("POST","/login",{"username":username,"password":password})["ok"]
        counts=request("GET","/api/recovery/catalog")["counts"]
        assert counts=={"db":1,"file":1,"obj":1,"vm":1},counts
        result["four_type_catalog"]=counts
        file_dest=RUN/"restored-files"
        restored=request("POST","/api/restores",{"record_id":file_record,"target_db":str(file_dest)},201)
        assert restored["status"]=="success",restored
        assert (file_dest/"payload.txt").read_bytes()==source.read_bytes()
        result["real_file_restore"]=True
        restored=request("POST","/api/restores",{"record_id":object_record,"target_task_id":target_task,
            "target_bucket":"destination","target_prefix":"restored","overwrite":"never",
            "apply_deleted":False,"create_bucket":False},201)
        assert restored["status"]=="success",restored
        assert all(OBJECTS[("destination","restored/"+key)]==data for key,data in content.items())
        result["signed_s3_http_obs_configuration_restore"]=True
        created=request("POST","/api/vm/recovery-points/%d/clone"%rp,
                         {"target_name":"qa-recovery","isolate_network":True,"auto_start":False},202)
        assert created["status"]=="running"
        for _ in range(100):
            job=request("GET","/api/vm/jobs/%d"%created["job_id"])
            if job["status"] in ("ready","failed"):break
            time.sleep(.05)
        assert job["status"]=="ready",job
        result["vm_stub_async_job_contract"]=True
        result["vm_is_not_claimed_as_real_hypervisor_restore"]=True
        response=session.get(url+"/restore",timeout=15)
        assert response.status_code==200 and "restore-center.js" in response.text
        for path in ("/static/js/restore-center.js","/static/css/restore-center.css"):
            assert session.get(url+path,timeout=15).status_code==200
        result["page_and_assets"]=True
        (BASE/"recovery-http-e2e.json").write_text(json.dumps(result,indent=2))
        (BASE/"ui-review.json").write_text(json.dumps({"run_dir":str(RUN),"url":"http://192.168.109.128:%d/restore"%server.server_port,
            "record_ids":{"db":oracle_record,"file":file_record,"obj":object_record},"rp_id":rp},indent=2))
        print(json.dumps(result,indent=2),flush=True)
        if options.serve:
            print("UI_REVIEW_READY "+str(RUN),flush=True)
            while True:time.sleep(1)
    finally:
        server.shutdown();server.server_close();s3.shutdown();s3.server_close()
        session.close()

if __name__=="__main__":main()
