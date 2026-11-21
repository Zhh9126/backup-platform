"""Run isolated pytest and always publish its outcome and individual cases to the UI."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import uuid
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
parser=argparse.ArgumentParser()
parser.add_argument("--category",default="system",choices=["vm","database","clone","storage","agent","recovery","system"])
parser.add_argument("--title",default="自动化测试")
args,pytest_args=parser.parse_known_args()
if pytest_args[:1]==["--"]: pytest_args=pytest_args[1:]
run=ROOT/"build"/"test-records"/uuid.uuid4().hex
run.mkdir(parents=True)
env=os.environ.copy()
for field,directory in (("INSTANCE_DIR","instance"),("BACKUP_ROOT","backups"),("LOG_DIR","logs"),
                        ("TMPDIR","tmp"),("RT_LOG_ROOT","rt_logs"),("RT_FILE_ROOT","rt_files")):
    folder=run/directory;folder.mkdir()
    env[field]=str(folder)
env.update(META_DB_PATH=str(run/"instance/meta.db"),META_DB_BACKEND="sqlite",SCHEDULER_ENABLED="false")
started=datetime.now().astimezone().isoformat(timespec="seconds")
report=run/"pytest.xml";log=run/"pytest.log"
with log.open("w") as output:
    result=subprocess.run([sys.executable,"-m","pytest",*pytest_args,"--junitxml="+str(report)],
                          cwd=str(ROOT),env=env,stdout=output,stderr=subprocess.STDOUT)
print(log.read_text(errors="replace")[-6000:])
# Parent uses the platform metadata; child pytest never shares its business DB.
from core import db,test_records
from core.test_record_import import import_junit
db.init_schema()
if report.is_file():
    rid=import_junit(report,args.category,args.title,started_at=started,exit_code=result.returncode)
else:
    rid=test_records.save_run({"source_key":"pytest:"+run.name,"category":args.category,"title":args.title,
        "status":"failed","evidence_level":"regression","started_at":started,
        "finished_at":datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary":"测试进程退出且没有产生 JUnit 报告；退出码 "+str(result.returncode),
        "evidence":{"checks":[],"files":[{"path":str(log.relative_to(ROOT)),"kind":"log"}]}})
print("Published: /test-records (record %s)"%rid)
sys.exit(result.returncode)
