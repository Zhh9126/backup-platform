# -*- coding: utf-8 -*-
"""磁带库硬件抽象层：设备发现 / 机械手控制 / 驱动状态 / 介质台账。

设计依据（联网调研 + 用户提供的接入流程）：
- `mtx` 只作用于 **generic SCSI 设备** `/dev/sgX`；`lsscsi -g` 里 `mediumx`
  行末的 `/dev/sgN` 才是机械手控制节点，`/dev/sch0` 不是 mtx 的目标。
- udev 提供 `/dev/tape/by-id/...`、`/dev/tape/by-path/...` 持久化命名，
  优先于 `/dev/nst0` 这类重启后会漂移的节点（systemd 60-persistent-storage-tape.rules）。
- 数据写入必须用**非回绕**设备 `/dev/nst*`，否则每次 tar 结束自动 rewound。
- Docker 必须 `--device` 显式透传（同时创建设备节点 + 放行设备 cgroup），
  单纯 `-v /dev:/dev` 会被 cgroup 拒绝；本模块给出可直接复制的运行参数。
- 解析 `mtx status` 采用 Bareos mtx-changer 的成熟正则口径：
  Data Transfer Element / Storage Element / IMPORT.EXPORT / VolumeTag。

诚实边界：所有真实硬件命令均为只读探测或显式操作；无硬件时只返回
"未发现设备/工具缺失"，绝不仿真出假设备或伪造机械手动作。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

from core import db

_LOG = "tape_library"

# 设备白名单：杜绝把 /dev/sda、/dev/null、任意块设备当磁带覆写
_TAPE_DEV_RE = re.compile(
    r"^/dev/(?:n?st\d+(?:[a-z]{1,2})?|tape/by-(?:id|path)/[^/\s]+)$")
_CHANGER_DEV_RE = re.compile(
    r"^/dev/(?:sg\d+|sch\d+|tape/by-(?:id|path)/[^/\s]+)$")

# 每设备独占锁：tier 线程、pool 链线程、手动 API 不得同时 rewind/eod/fsf
_LOCKS = {}
_LOCK_GUARD = threading.Lock()


def device_lock(device: str):
    with _LOCK_GUARD:
        if device not in _LOCKS:
            _LOCKS[device] = threading.RLock()
        return _LOCKS[device]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def is_safe_tape_device(path: str) -> bool:
    return bool(_TAPE_DEV_RE.match((path or "").strip()))


def is_safe_changer_device(path: str) -> bool:
    return bool(_CHANGER_DEV_RE.match((path or "").strip()))


def _run(cmd: list, timeout: int = 20) -> dict:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"rc": r.returncode, "out": r.stdout or "", "err": r.stderr or ""}
    except FileNotFoundError as e:
        return {"rc": -2, "out": "", "err": f"命令不存在: {e}"}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "out": "", "err": f"命令超时（{timeout}s）"}
    except Exception as e:  # noqa: BLE001
        return {"rc": -3, "out": "", "err": str(e)}


# --------------------------------------------------------------------------
# 解析（纯函数，便于离线单测）
# --------------------------------------------------------------------------

_LSSCSI_RE = re.compile(
    r"^\[(\d+:\d+:\d+:\d+)\]\s+(\S+)\s+(.+?)\s{2,}(.*)$")


def parse_lsscsi(text: str) -> dict:
    """解析 `lsscsi -g` 输出，提取 tape / mediumx 及其 /dev/sgN 节点。"""
    drives, changers, others = [], [], []
    for line in (text or "").splitlines():
        m = _LSSCSI_RE.match(line.strip())
        if not m:
            continue
        addr, kind, ident, tail = m.groups()
        nodes = re.findall(r"/dev/\S+", tail)
        generic = next((n for n in nodes if re.match(r"^/dev/sg\d+$", n)), "")
        node = next((n for n in nodes if "/dev/sg" not in n), "") or generic
        item = {"address": addr, "type": kind.lower(),
                "identity": ident.strip(), "node": node,
                "generic": generic, "nodes": nodes}
        if kind.lower() in ("tape", "sequential-access"):
            drives.append(item)
        elif kind.lower() in ("mediumx", "medium changer", "changer"):
            changers.append(item)
        else:
            others.append(item)
    return {"drives": drives, "changers": changers, "others": others}


# Bareos mtx-changer 的解析口径（Perl 正则移植为 Python）
_DTE_RE = re.compile(
    r"Data Transfer Element\s+(\d+)\s*:\s*(Empty|Full)\s*"
    r"(?:\(Storage Element\s+(\d+)\s+Loaded\))?\s*"
    r"(?::VolumeTag\s*=\s*(\S+))?", re.I)
_SE_RE = re.compile(
    r"Storage Element\s+(\d+)\s*(IMPORT[/.]EXPORT)?\s*:\s*(Empty|Full)"
    r"(?:\s*:VolumeTag\s*=\s*(\S+))?", re.I)
_HEAD_RE = re.compile(r"Storage Changer\s+(\S+):\s*(\d+)\s+Drives?,\s*"
                      r"(\d+)\s+Slots?", re.I)


def parse_mtx_status(text: str) -> dict:
    """解析 `mtx -f /dev/sgN status`：驱动器、槽位、I/E 口、条码。"""
    out = {"drives": [], "slots": [], "import_export": [],
          "summary": {}, "raw_lines": 0}
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        out["raw_lines"] += 1
        h = _HEAD_RE.search(s)
        if h:
            out["summary"] = {"changer": h.group(1),
                              "drives": int(h.group(2)),
                              "slots": int(h.group(3))}
            continue
        m = _DTE_RE.search(s)
        if m:
            out["drives"].append({
                "drive": int(m.group(1)),
                "state": "full" if m.group(2).lower() == "full" else "empty",
                "loaded_slot": int(m.group(3)) if m.group(3) else None,
                "barcode": (m.group(4) or "").strip() or None})
            continue
        m = _SE_RE.search(s)
        if m:
            item = {"slot": int(m.group(1)),
                    "state": "full" if m.group(3).lower() == "full" else "empty",
                    "barcode": (m.group(4) or "").strip() or None}
            (out["import_export"] if m.group(2) else out["slots"]).append(item)
    out["summary"].setdefault("drives", len(out["drives"]))
    out["summary"].setdefault("slots", len(out["slots"]))
    out["summary"]["import_export"] = len(out["import_export"])
    return out


def parse_mt_status(text: str) -> dict:
    """解析 `mt -f /dev/nst0 status` 的关键标志。"""
    s = (text or "").lower()
    return {
        "online": "on-line" in s or "online" in s,
        "write_protected": "wr_prot" in s or "write protected" in s,
        "at_bot": "bot" in s,
        "at_eod": "eod" in s or "eof" in s,
        "file_number": next((int(x) for x in re.findall(r"file number\s*=\s*(-?\d+)", s)), None),
        "block_number": next((int(x) for x in re.findall(r"block number\s*=\s*(-?\d+)", s)), None),
        "raw": (text or "")[:600],
    }


# --------------------------------------------------------------------------
# 设备发现 / 运行时能力探测
# --------------------------------------------------------------------------

def tool_status() -> dict:
    tools = {}
    for name in ("mt", "mtx", "lsscsi", "tar", "mbuffer", "sg_inq", "stinit"):
        path = shutil.which(name)
        tools[name] = {"available": bool(path), "path": path or ""}
    return tools


def in_container() -> bool:
    try:
        if os.path.exists("/.dockerenv"):
            return True
        with open("/proc/1/cgroup", encoding="utf-8", errors="ignore") as f:
            return any(k in f.read() for k in ("docker", "containerd", "kubepods"))
    except OSError:
        return False


def _glob_existing(pattern: str) -> list:
    import glob
    return sorted(p for p in glob.glob(pattern) if os.path.exists(p))


def discover(use_lsscsi: bool = True) -> dict:
    """只读设备发现：lsscsi -g → /dev/tape/by-id → /dev 兜底。"""
    tools = tool_status()
    parsed = {"drives": [], "changers": [], "others": []}
    lsscsi_note = ""
    if use_lsscsi and tools["lsscsi"]["available"]:
        r = _run(["lsscsi", "-g"], timeout=15)
        if r["rc"] == 0:
            parsed = parse_lsscsi(r["out"])
        else:
            lsscsi_note = r["err"] or f"lsscsi rc={r['rc']}"
    else:
        lsscsi_note = "lsscsi 未安装，改用 /dev 节点与 sysfs 兜底发现"

    # 持久化命名优先（udev 60-persistent-storage-tape.rules）
    persistent = _glob_existing("/dev/tape/by-id/*") + _glob_existing("/dev/tape/by-path/*")
    raw_drives = _glob_existing("/dev/nst[0-9]*") + _glob_existing("/dev/st[0-9]*")
    raw_changers = _glob_existing("/dev/sg[0-9]*") + _glob_existing("/dev/sch[0-9]*")

    # sysfs 类型判别：只有 scsi type=1 是磁带机，type=8 是机械手
    typed_sg = []
    for sg in raw_changers:
        name = os.path.basename(sg)
        syspath = f"/sys/class/scsi_generic/{name}/device/type"
        try:
            with open(syspath, encoding="utf-8") as f:
                t = f.read().strip()
        except OSError:
            t = ""
        typed_sg.append({"node": sg, "scsi_type": t,
                         "role": "changer" if t == "8" else
                                 ("tape" if t == "1" else "generic")})

    changers = list(parsed["changers"])
    drives = list(parsed["drives"])
    known = {d.get("generic") for d in changers} | {d.get("node") for d in drives}
    known.discard("")
    for p in persistent:
        if p not in known and is_safe_tape_device(p):
            drives.append({"node": p, "generic": "", "identity": "udev persistent",
                           "address": "", "nodes": [p]})
    for p in raw_drives:
        if p not in known:
            drives.append({"node": p, "generic": "", "identity": "raw device",
                           "address": "", "nodes": [p]})
    for c in typed_sg:
        if c["role"] == "changer" and c["node"] not in known:
            changers.append({"node": c["node"], "generic": c["node"],
                             "identity": f"scsi type {c['scsi_type']}",
                             "address": "", "nodes": [c["node"]]})

    container = in_container()
    result = {
        "drives": drives, "changers": changers, "generic_devices": typed_sg,
        "persistent_links": persistent, "tools": tools,
        "container": container, "lsscsi_note": lsscsi_note,
        "discovered_at": now_iso(),
    }
    result["runtime"] = runtime_hint(result)
    return result


def runtime_hint(disc: dict = None) -> dict:
    """给出容器接入建议（可直接复制的 --device 参数），并如实判定就绪度。"""
    disc = disc if disc is not None else discover(use_lsscsi=False)
    tools = disc.get("tools") or tool_status()
    missing = [n for n, v in tools.items()
               if n in ("mt", "mtx", "lsscsi", "tar") and not v["available"]]
    dev_nodes = sorted({d.get("node") for d in disc.get("drives", []) if d.get("node")}
                       | {c.get("generic") or c.get("node")
                          for c in disc.get("changers", []) if c.get("node")})
    args = " ".join(f"--device {p}:{p}:rwm" for p in dev_nodes)
    ready = bool(dev_nodes) and not missing
    hints = []
    if not dev_nodes:
        hints.append("容器内未发现 /dev/nst* 或 /dev/sg* 设备节点")
    if missing:
        hints.append("镜像缺少工具: " + ", ".join(missing))
    if disc.get("container") and dev_nodes:
        hints.append("确认容器以 --device 透传且设备 cgroup 已授权（单纯 -v /dev 不够）")
    if not any(str(d.get("node", "")).startswith("/dev/tape/by-")
               for d in disc.get("drives", [])):
        hints.append("建议使用 /dev/tape/by-id/... 持久化命名，避免重启后 /dev/nst0 漂移")
    return {
        "ready": ready, "missing_tools": missing,
        "device_nodes": dev_nodes,
        "docker_run_args": args,
        "compose_devices": [f"{p}:{p}:rwm" for p in dev_nodes],
        "hints": hints,
    }


# --------------------------------------------------------------------------
# 机械手 / 驱动控制（显式调用，绝不自动改介质）
# --------------------------------------------------------------------------

class TapeChanger:
    def __init__(self, device: str, timeout: int = 120):
        self.device = (device or "").strip()
        self.timeout = timeout

    def _check(self):
        if not is_safe_changer_device(self.device):
            raise ValueError(f"非法机械手设备路径（只允许 /dev/sgN、/dev/schN、"
                             f"/dev/tape/by-id/*）: {self.device}")
        if not shutil.which("mtx"):
            raise RuntimeError("mtx 未安装（需 mtx / mt-st 包）")
        if not os.path.exists(self.device):
            raise RuntimeError(f"机械手设备不存在: {self.device}")

    def _exec(self, *args) -> dict:
        self._check()
        with device_lock(self.device):
            r = _run(["mtx", "-f", self.device, *[str(a) for a in args]],
                     timeout=self.timeout)
        return {"ok": r["rc"] == 0, "rc": r["rc"],
                "out": r["out"][:4000], "err": r["err"][:1000]}

    def status(self) -> dict:
        r = self._exec("status")
        r["library"] = parse_mtx_status(r["out"]) if r["ok"] else None
        return r

    def inventory(self) -> dict:
        """让带库重新扫描条码（部分库支持 inventory）。"""
        return self._exec("inventory")

    def load(self, slot: int, drive: int = 0) -> dict:
        if int(slot) < 0 or int(drive) < 0:
            raise ValueError("slot / drive 必须为非负整数")
        return self._exec("load", int(slot), int(drive))

    def unload(self, slot: int, drive: int = 0) -> dict:
        return self._exec("unload", int(slot), int(drive))

    def transfer_to_ie(self, slot: int, ie_slot: int) -> dict:
        """把介质移到 Import/Export 口（出库前一步）。"""
        return self._exec("transfer", int(slot), int(ie_slot))

    def transfer_from_ie(self, ie_slot: int, slot: int) -> dict:
        return self._exec("transfer", int(ie_slot), int(slot))


class TapeDrive:
    def __init__(self, device: str, timeout: int = 300):
        self.device = (device or "").strip()
        self.timeout = timeout

    def _check(self):
        if not is_safe_tape_device(self.device):
            raise ValueError(f"非法磁带设备路径（只允许 /dev/nst*、/dev/st*、"
                             f"/dev/tape/by-id/*）: {self.device}")
        if not shutil.which("mt"):
            raise RuntimeError("mt 未安装（需 mt-st 包）")
        if not os.path.exists(self.device):
            raise RuntimeError(f"磁带设备不存在: {self.device}")

    def op(self, op: str, count: int = None) -> dict:
        if op not in ("status", "rewind", "eod", "fsf", "bsf", "offline",
                      "eject", "retension", "erase", "setblk", "tell"):
            raise ValueError(f"不允许的磁带操作: {op}")
        self._check()
        cmd = ["mt", "-f", self.device, op]
        if count is not None:
            cmd.append(str(int(count)))
        with device_lock(self.device):
            r = _run(cmd, timeout=self.timeout)
        return {"ok": r["rc"] == 0, "rc": r["rc"],
                "out": r["out"][:4000], "err": r["err"][:1000]}

    def status(self) -> dict:
        r = self.op("status")
        r["parsed"] = parse_mt_status(r["out"]) if r["ok"] else None
        return r


# --------------------------------------------------------------------------
# 介质 / 写入目录台账（惰性建表，不改主 schema）
# --------------------------------------------------------------------------

def _ensure_tables():
    db.execute(
        "CREATE TABLE IF NOT EXISTS tape_media ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, target_id INTEGER, "
        "barcode TEXT, slot INTEGER, state TEXT DEFAULT 'in_library', "
        "drive INTEGER, write_protected INTEGER DEFAULT 0, worm INTEGER DEFAULT 0, "
        "capacity_bytes INTEGER DEFAULT 0, used_bytes INTEGER DEFAULT 0, "
        "last_written_at TEXT, last_verified_at TEXT, offsite_since TEXT, "
        "note TEXT, created_at TEXT, updated_at TEXT)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS tape_jobs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, target_id INTEGER, record_id INTEGER, "
        "object_key TEXT, media_id INTEGER, media_label TEXT, filemark INTEGER, "
        "size_bytes INTEGER DEFAULT 0, checksum TEXT, verified INTEGER DEFAULT 0, "
        "status TEXT DEFAULT 'writing', error TEXT, operator TEXT, "
        "created_at TEXT, finished_at TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tape_jobs_key "
               "ON tape_jobs(object_key)")


def sync_inventory(target_id: int, library: dict) -> dict:
    """把 mtx status 的槽位/条码同步进介质台账（只增不删，出库状态人工维护）。"""
    _ensure_tables()
    stats = {"created": 0, "updated": 0, "total": 0}
    for s in (library or {}).get("slots", []) + (library or {}).get("import_export", []):
        barcode = s.get("barcode") or f"SLOT-{s.get('slot')}"
        stats["total"] += 1
        row = db.query_one("SELECT id FROM tape_media WHERE target_id=? AND barcode=?",
                           (target_id, barcode))
        if row:
            db.execute("UPDATE tape_media SET slot=?, updated_at=? WHERE id=?",
                       (s.get("slot"), now_iso(), row["id"]))
            stats["updated"] += 1
        else:
            db.execute(
                "INSERT INTO tape_media (target_id, barcode, slot, state, "
                "created_at, updated_at) VALUES (?,?,?,'in_library',?,?)",
                (target_id, barcode, s.get("slot"), now_iso(), now_iso()))
            stats["created"] += 1
    # 已装入驱动器的介质：台账里没有就补一条（mtx 只报 DTE，不重复列该槽位）
    for d in (library or {}).get("drives", []):
        if d.get("state") != "full" or not d.get("barcode"):
            continue
        row = db.query_one("SELECT id FROM tape_media WHERE target_id=? AND barcode=?",
                           (target_id, d["barcode"]))
        if row:
            db.execute("UPDATE tape_media SET state='in_drive', drive=?, "
                       "updated_at=? WHERE id=?",
                       (d.get("drive"), now_iso(), row["id"]))
        else:
            db.execute(
                "INSERT INTO tape_media (target_id, barcode, slot, state, drive, "
                "created_at, updated_at) VALUES (?,?,?,'in_drive',?,?,?)",
                (target_id, d["barcode"], d.get("loaded_slot"),
                 d.get("drive"), now_iso(), now_iso()))
            stats["created"] += 1
    return stats


def media_list(target_id: int = None, state: str = None) -> list:
    _ensure_tables()
    sql = "SELECT * FROM tape_media WHERE 1=1"
    args = []
    if target_id:
        sql += " AND target_id=?"
        args.append(int(target_id))
    if state:
        sql += " AND state=?"
        args.append(state)
    sql += " ORDER BY id DESC"
    return [dict(r) for r in db.query(sql, args)]


def media_update(media_id: int, **fields) -> dict:
    _ensure_tables()
    allowed = {"state", "write_protected", "worm", "slot", "drive",
               "offsite_since", "note", "used_bytes", "last_written_at",
               "last_verified_at", "barcode"}
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            args.append(v)
    if not sets:
        raise ValueError("无可更新字段")
    sets.append("updated_at=?")
    args.extend([now_iso(), int(media_id)])
    db.execute(f"UPDATE tape_media SET {', '.join(sets)} WHERE id=?", args)
    row = db.query_one("SELECT * FROM tape_media WHERE id=?", (int(media_id),))
    if not row:
        raise ValueError(f"介质不存在: {media_id}")
    return dict(row)


def job_start(target_id: int, object_key: str, size_bytes: int,
              checksum: str, media_label: str = "",
              record_id: int = None, operator: str = "") -> int:
    _ensure_tables()
    return db.execute(
        "INSERT INTO tape_jobs (target_id, record_id, object_key, media_label, "
        "size_bytes, checksum, status, operator, created_at) "
        "VALUES (?,?,?,?,?,?,'writing',?,?)",
        (target_id, record_id, object_key, media_label, size_bytes,
         checksum, operator, now_iso()))


def job_finish(job_id: int, ok: bool, verified: bool = False,
               filemark: int = None, error: str = "") -> None:
    _ensure_tables()
    db.execute(
        "UPDATE tape_jobs SET status=?, verified=?, filemark=?, error=?, "
        "finished_at=? WHERE id=?",
        ("success" if ok else "failed", 1 if verified else 0, filemark,
         (error or "")[:1000], now_iso(), int(job_id)))


def job_list(target_id: int = None, limit: int = 100) -> list:
    _ensure_tables()
    sql = "SELECT * FROM tape_jobs"
    args = []
    if target_id:
        sql += " WHERE target_id=?"
        args.append(int(target_id))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    return [dict(r) for r in db.query(sql, args)]


def job_find(object_key: str) -> list:
    """按 object_key 查写入目录（device 模式下 file_exists 的数据来源）。

    平台 object_key 形如 `{db_type}/{tid}_{name}/{ts}__{filename}`，因此除精确
    匹配外，还要兼容仅传文件名回迁时的 `{ts}__{filename}` 尾缀形态。
    """
    _ensure_tables()
    base = os.path.basename(object_key)
    rows = db.query(
        "SELECT * FROM tape_jobs WHERE object_key=? "
        "OR object_key LIKE ? OR object_key LIKE ? "
        "ORDER BY id DESC LIMIT 50",
        (object_key, "%/" + base, "%__" + base))
    return [dict(r) for r in rows]


def media_summary(target_id: int = None) -> dict:
    _ensure_tables()
    rows = media_list(target_id)
    jobs = job_list(target_id, limit=1000)
    return {
        "media_total": len(rows),
        "in_library": sum(1 for r in rows if r["state"] == "in_library"),
        "in_drive": sum(1 for r in rows if r["state"] == "in_drive"),
        "offsite": sum(1 for r in rows if r["state"] == "offsite"),
        "retired": sum(1 for r in rows if r["state"] == "retired"),
        "write_protected": sum(1 for r in rows if r["write_protected"]),
        "worm": sum(1 for r in rows if r["worm"]),
        "jobs_total": len(jobs),
        "jobs_success": sum(1 for j in jobs if j["status"] == "success"),
        "jobs_failed": sum(1 for j in jobs if j["status"] == "failed"),
        "jobs_verified": sum(1 for j in jobs if j["verified"]),
        "bytes_written": sum(j.get("size_bytes") or 0 for j in jobs
                             if j["status"] == "success"),
    }
