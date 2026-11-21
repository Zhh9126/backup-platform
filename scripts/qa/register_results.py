"""Publish existing real evidence and historical QA results to the UI."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from core import db
from core.test_record_import import import_all_history,import_vm,import_pytest_log,import_json_report
parser=argparse.ArgumentParser()
parser.add_argument("--history",action="store_true")
parser.add_argument("--vm-dir")
parser.add_argument("--pytest-log")
parser.add_argument("--category",default="system")
parser.add_argument("--json-report")
parser.add_argument("--title")
parser.add_argument("--level",default="contract")
parser.add_argument("--boundary",default="隔离测试结果，具体边界以报告为准。")
args=parser.parse_args()
db.init_schema()
if args.history: ids=import_all_history()
elif args.json_report: ids=[import_json_report(args.json_report,args.category,args.title,args.level,args.boundary)]
elif args.pytest_log: ids=[import_pytest_log(args.pytest_log,args.category)]
else: ids=import_vm(args.vm_dir)
print("Registered test records:",ids)
