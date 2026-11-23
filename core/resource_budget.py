"""Unified memory and disk admission checks for large backup jobs."""
import os, shutil

def check(task=None):
    task=task or {}
    mem_min=int(os.environ.get("AIDBM_MIN_FREE_MEMORY_MB","512"))
    disk_min=int(os.environ.get("AIDBM_MIN_FREE_DISK_GB","5"))
    free_mem=None
    try:
        info={}
        for line in open("/proc/meminfo"):
            k,v=line.split(":",1); info[k]=int(v.strip().split()[0])
        free_mem=(info.get("MemAvailable") or info.get("MemFree") or 0)//1024
    except Exception: pass
    try:
        import config
        root=str(config.get_backup_root())
    except Exception: root=str(task.get("backup_root") or ".")
    try: free_disk=shutil.disk_usage(root).free//(1024**3)
    except Exception: free_disk=None
    if free_mem is not None and free_mem < mem_min:
        return {"ok":False,"reason":f"可用内存 {free_mem}MB 低于保护阈值 {mem_min}MB"}
    if free_disk is not None and free_disk < disk_min:
        return {"ok":False,"reason":f"备份盘可用空间 {free_disk}GB 低于保护阈值 {disk_min}GB"}
    return {"ok":True,"free_memory_mb":free_mem,"free_disk_gb":free_disk}
