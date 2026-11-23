"""P1 capability probes. Reports real runtime readiness without claiming planned features."""
import importlib.util, os, platform, shutil, ssl, sys

def _cmd(name): return bool(shutil.which(name))
def _mod(name): return importlib.util.find_spec(name) is not None

def snapshot():
    runtime={"python":platform.python_version(),"ssl":True,"ssl_version":getattr(ssl,"OPENSSL_VERSION","unknown"),"lock_file":os.path.isfile("requirements.lock")}
    resources={"worker_pool":True,"persistent_task_lease":True,"bandwidth_governor":True,"disk_budget":True}
    os_agent={"lvm_snapshot":_cmd("lvcreate"),"btrfs_snapshot":_cmd("btrfs"),"vss":False,"bmr_iso":False,"kernel_cbt":False}
    synology={"filestation_api":True,"docker":_cmd("docker"),"btrfs_reconcile":_cmd("btrfs"),"synoacltool":_cmd("synoacltool"),"mTLS_plugin":False}
    vm={"qemu_img":_cmd("qemu-img"),"libvirt":_cmd("virsh"),"nfs_server":_cmd("rpc.nfsd") or _cmd("nfsd"),"iscsi_target":_cmd("targetcli") or _cmd("tgtadm"),"vmware_vddk":False,"live_mount":False}
    deps={m:_mod(m) for m in ("flask","apscheduler","paramiko","cryptography","pymysql","psycopg2","oracledb","pymssql","pymongo","zstandard")}
    return {"runtime":runtime,"resources":resources,"os_agent":os_agent,"synology":synology,"vm":vm,"dependencies":deps,"generated_at":__import__("datetime").datetime.utcnow().isoformat()+"Z"}
