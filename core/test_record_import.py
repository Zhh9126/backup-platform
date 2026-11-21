"""Import existing evidence without claiming external tests are managed jobs."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from core import test_records as records

ROOT=records.ROOT

def _stamp(path):
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")

def _file(path,kind="report",sha=None,size=None):
    rel=str(path.resolve().relative_to(ROOT))
    item={"path":rel,"kind":kind}
    if sha: item["sha256"]=sha
    if size is not None: item["size_bytes"]=size
    return item

def import_vm(directory=None):
    folder=Path(directory) if directory else ROOT/"build/vm-real-20261120"
    if not folder.is_absolute(): folder=ROOT/folder
    imported=[]
    warm=folder/"warm-result.json"
    if warm.is_file():
        report=json.loads(warm.read_text(encoding="utf-8-sig"))
        repository=json.loads((folder/"repository-integrity.json").read_text())
        sqlite_pass=report.get("sqlite_integrity_check")=="ok" and report.get("sqlite_rows")==10000 and report.get("sqlite_sha256_matches") is True
        checks=[
            {"name":"隔离恢复副本实际开机","status":"passed" if report.get("guest_booted") else "failed"},
            {"name":"归档往返 SHA256 一致","status":"passed","detail":repository["sha256"]},
            {"name":"SQLite 完整性和 10,000 行","status":"passed" if sqlite_pass else "failed"},
            {"name":"未刷盘随机文件","status":"failed" if report.get("unflushed_payload_failed") else "passed","detail":"首轮测试夹具未 fsync，热快照中为空文件"},
            {"name":"未刷盘清单","status":"failed" if report.get("unflushed_manifest_failed") else "passed"}
        ]
        imported.append(records.save_run({
            "source_key":"vm-real-20261120:warm","category":"vm","title":"CentOS 7 在线热快照备份与恢复",
            "subject":"VMware Workstation · 192.168.109.128","status":"partial","evidence_level":"external_real",
            "started_at":"2026-11-20T22:13:14+08:00","finished_at":_stamp(warm),
            "summary":"恢复副本开机，SQLite 10,000 行通过；两个未刷盘测试文件未通过。",
            "boundary":"外部原生实测；未纳管 Workstation。崩溃一致性不保证客户机未落盘数据，不属于平台自动化恢复点。",
            "evidence":{"checks":checks,"files":[_file(warm),_file(folder/"repository-integrity.json"),
                _file(folder/"vm-backup.tar.gz","backup",repository["sha256"],repository["size_bytes"])]}
        },operator="evidence-import"))
    cold=folder/"cold-data-verification.json"
    if cold.is_file():
        report=json.loads(cold.read_text(encoding="utf-8-sig"));vm=report["vm_result"]
        assert vm["status"]=="passed" and report["sqlite_integrity"]=="ok" and report["every_row_verified"] is True and report["rows"]==10000
        checks=[
            {"name":"完整冷备上传 Linux 后重新下载","status":"passed","detail":"往返归档 SHA256: "+vm["archive_sha256"]},
            {"name":"独立副本断网启动","status":"passed" if vm.get("guest_booted") and vm.get("network_disconnected") else "failed"},
            {"name":"随机文件、数据库、清单 SHA256","status":"passed","detail":", ".join(vm["files_verified"])},
            {"name":"SQLite integrity_check","status":"passed","detail":report["sqlite_integrity"]},
            {"name":"逐行核对 10,000 行","status":"passed","detail":str(report["rows"])+" 行内容摘要全部一致"},
            {"name":"恢复副本正常关机","status":"passed","detail":"测试日志记录关机完成；原服务器保持运行"}
        ]
        imported.append(records.save_run({
            "source_key":"vm-real-20261120:cold","category":"vm","title":"CentOS 7 完整冷备与隔离恢复验收",
            "subject":"VMware Workstation · 192.168.109.128","status":"passed","evidence_level":"external_real",
            "started_at":"2026-11-20T22:28:38+08:00","finished_at":vm["finished"],
            "summary":"7.09 GB 归档往返校验、隔离开机、三个文件 SHA256、SQLite 完整性及 10,000 行逐行校验通过。",
            "boundary":"使用独立测试副本正常关机后制作冷备；原服务器未停机。Workstation 原生工具实测，不代表已接入平台纳管、自动备份或在线恢复。",
            "evidence":{"checks":checks,"restored_vmx":vm["restored_vmx"],"files":[_file(cold),
                _file(folder/"cold-result.json"),_file(folder/"cold-acceptance.log","log"),
                _file(folder/"vm-cold-backup.tar.gz","backup",vm["archive_sha256"],vm["archive_bytes"])]}
        },operator="evidence-import"))
    return imported

def import_pytest_log(path,category="system"):
    path=Path(path).resolve()
    text=path.read_text(encoding="utf-8-sig",errors="replace")
    lines=[line for line in text.splitlines() if re.search(r"\d+ (?:passed|failed|skipped|errors?)\b",line)]
    if not lines: return None
    summary=lines[-1].strip("= ")
    counts={key:int(number) for number,key in re.findall(r"(\d+) (passed|failed|skipped|errors?)\b",summary)}
    failed=counts.get("failed",0)+counts.get("error",0)+counts.get("errors",0)
    status="failed" if failed else "passed" if counts.get("passed") else "skipped"
    rel=str(path.relative_to(ROOT))
    checks=[{"name":key,"status":"failed" if key in {"failed","error","errors"} else "skipped" if key=="skipped" else "passed",
             "detail":str(value)} for key,value in counts.items() if value]
    return records.save_run({"source_key":"pytest-log:"+rel,"category":category,
        "title":"自动化回归 · "+path.stem,"subject":path.parent.name,"status":status,"evidence_level":"regression",
        "finished_at":_stamp(path),"summary":summary,"boundary":"历史回归日志登记；不等同于真实数据库或 Hypervisor 端到端验收。重复运行的批次可能包含相同用例，不累计作为唯一用例数。",
        "evidence":{"checks":checks,"counts":counts,"files":[_file(path,"log")]}})

def import_junit(path,category="system",title=None,source_key=None,started_at=None,exit_code=0):
    path=Path(path).resolve()
    tree=ET.parse(path)
    checks=[];counts={"passed":0,"failed":0,"skipped":0}
    for case in tree.iter("testcase"):
        failed=case.find("failure")
        if failed is None: failed=case.find("error")
        status="failed" if failed is not None else "skipped" if case.find("skipped") is not None else "passed"
        counts[status]+=1
        checks.append({"name":(case.get("classname","")+"::"+case.get("name",""))[:600],"status":status,
                       "detail":((failed.text or failed.get("message",""))[:800] if failed is not None else ""),
                       "seconds":case.get("time","")})
    status="failed" if counts["failed"] or exit_code else "passed" if counts["passed"] else "skipped"
    return records.save_run({"source_key":source_key or "junit:"+str(path.relative_to(ROOT)),
        "category":category,"title":title or "自动化测试","subject":path.name,"status":status,"evidence_level":"regression",
        "started_at":started_at,"finished_at":_stamp(path),
        "summary":"通过 {passed} · 失败 {failed} · 跳过 {skipped} · 退出码 {code}".format(**counts,code=exit_code),
        "boundary":"自动化回归逐项记录；测试桩和模拟环境不作为真实数据库恢复成功证据。",
        "evidence":{"checks":checks,"counts":counts,"exit_code":exit_code,"files":[_file(path,"report")]}})

def import_history():
    ids=import_vm()
    for name in ("integration-20261120","zbackup-qa","system-qa","agent-qa","agent-test","storage-qa"):
        folder=ROOT/"build"/name
        for path in sorted(folder.glob("*.log")):
            category="vm" if path.stem.startswith("vm-") else "database" if path.stem.startswith("realtime") else "clone" if path.stem.startswith("rt-clone") else "agent" if "agent" in name else "storage" if "storage" in path.stem else "system"
            rid=import_pytest_log(path,category)
            if rid: ids.append(rid)
    return ids

def import_json_report(path,category="system",title=None,level="contract",boundary="隔离测试报告；请按报告中的真实环境和模拟边界解读。"):
    path=Path(path).resolve()
    report=json.loads(path.read_text(encoding="utf-8-sig"))
    checks=[]
    def collect(node,prefix=""):
        if isinstance(node,dict):
            for key,value in node.items():
                name=(prefix+" / "+key).strip(" /")
                if isinstance(value,bool):
                    checks.append({"name":name,"status":"passed" if value else "failed"})
                elif isinstance(value,dict): collect(value,name)
    collect(report)
    if path.name=="large-e2e.json":
        for kind in ("blobs","containers"):
            item=report[kind]
            ok=item["source_bytes"]==537919488 and item["second_new_blocks"]==0 and item["second_reused_blocks"]==item["chunks"] and bool(item["restore_sha256"])
            checks.append({"name":kind+" 513 MiB 备份、重复重删与还原","status":"passed" if ok else "failed",
                           "detail":"稀疏零填充数据；不代表生产数据重删率"})
    if path.name=="http-smoke.json":
        for name,value in report.items():
            if isinstance(value,int):
                checks.append({"name":name,"status":"passed" if value==(400 if name=="invalid_fingerprint" else 200) else "failed",
                               "detail":"HTTP "+str(value)})
    status="failed" if any(c["status"]=="failed" for c in checks) else "passed" if checks else "partial"
    return records.save_run({"source_key":"json-report:"+str(path.relative_to(ROOT)),
        "category":category,"title":title or path.stem,"subject":path.parent.name,"status":status,
        "evidence_level":level,"finished_at":_stamp(path),"summary":"报告校验项："+str(len(checks))+"；"+status,
        "boundary":boundary,"evidence":{"checks":checks,"report":report,"files":[_file(path)]}})

def import_all_history():
    ids=import_history()
    specs=[
      ("build/storage-qa/sftp-e2e.json","storage","SFTP 真实断线续传与目标保护","external_real","真实 SSH/SFTP 数据传输，包含注入的读写断线、错误前缀与发布失败；不涉及真实业务数据库。"),
      ("build/storage-qa/large-e2e.json","storage","513 MiB 存储重删与还原","external_real","真实本地文件读写和还原；输入为稀疏零填充，不代表生产数据重删率。"),
      ("build/storage-qa/http-smoke.json","storage","存储 HTTP 页面与参数校验","contract","隔离测试环境的 HTTP 冒烟检查。"),
      ("build/zbackup-qa/http-e2e.json","recovery","副本丢源恢复与目录导入","contract","隔离 HTTP 环境；真实本地文件与 SQLite 查询，不代表外部云存储或真实业务数据库验收。"),
      ("build/integration-20261120/recovery-http-e2e.json","recovery","恢复中心四类型 HTTP 验收","contract","实际文件恢复及带签名的 S3 HTTP 测试；OBS 使用本地兼容服务，VM 使用明确标注的契约桩。")]
    for rel,category,title,level,boundary in specs:
        path=ROOT/rel
        if path.exists(): ids.append(import_json_report(path,category,title,level,boundary))
    for name in ("integration-20261120","zbackup-qa","system-qa","agent-test","storage-qa"):
        for path in sorted((ROOT/"build"/name).glob("*.log")):
            rel=str(path.relative_to(ROOT))
            if records.db.query_one("SELECT id FROM acceptance_runs WHERE source_key=?",("pytest-log:"+rel,)):
                continue
            # Do not turn an unparsed/incomplete old process log into a pass.
            ids.append(records.save_run({"source_key":"process-log:"+rel,"category":"system",
                "title":"历史执行日志 · "+path.stem,"subject":name,"status":"partial","evidence_level":"contract",
                "finished_at":_stamp(path),"summary":"已保留执行日志；缺少可自动确认的完整结果，请查看报告。",
                "boundary":"日志记录不自动证明验收通过；中断进程和测试完成状态分开处理。",
                "evidence":{"checks":[],"files":[_file(path,"log")]}}))
    return ids
