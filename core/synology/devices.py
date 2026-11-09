# -*- coding: utf-8 -*-
"""群晖 DSM 设备纳管（表 / CRUD / 探活 / 资产清单 / 场景能力矩阵）。

这是「群晖全面纳管」的设备层地基，与 ``client.py`` 的协议客户端分层：

* ``client.py``  —— 单次 DSM 会话（HTTP Web API），无状态；
* ``devices.py`` —— 把会话凭据、能力探测结果、探活状态**持久化**到元库，
  供页面、API 与备份引擎共同引用。备份任务只存 ``synology_device_id``，
  密码集中在设备表（SM4-GCM 加密），改密码不需要改每个任务。

设计纪律（与 AIDBM 一贯口径一致）：

1. **DSM 侧零安装**：只用 DSM 自带的 Web API，不往 NAS 上放任何 agent/脚本/包。
2. **凭据集中且不回显**：密码入库前 ``db.encrypt_secret``；列表/详情默认不带明文，
   只给 ``has_password``；OTP 是**一次性验证码，绝不落库**，仅探测请求里临时传入。
3. **能力不虚报**：``SYNO.FileStation.*`` 为官方公开 API；Storage / Snapshot /
   SAN Manager / ``SYNO.Core.*`` 是非公开接口，能力发现命中也不等于可用，
   探测失败一律 ``unsupported`` 并给出说明。
4. **块存储边界**：SAN Manager 输出的是裸 LUN，没有文件系统。平台**不直接读裸块**，
   iSCSI 场景应纳管「挂载了 LUN 的业务主机」并备份其挂载点；不承诺块级增量/CBT。
"""
from __future__ import annotations

import json
import time
from typing import Optional

from core import db
from core.synology.client import DSMClient, DSMError

__all__ = [
    "DeviceError", "TABLE", "SCENARIOS", "init_table", "list_devices",
    "get_device", "create_device", "update_device", "delete_device",
    "build_client", "probe_device", "list_shares", "browse_folder",
    "walk_assets", "scenarios", "client_config",
]

TABLE = "synology_devices"

DEVICE_FIELDS = [
    "name", "hostname", "port", "secure", "verify_ssl", "username",
    "password", "session_name", "timeout", "remark", "enabled",
]


class DeviceError(Exception):
    """纳管数据不合法或设备不存在。"""


# ---------------------------------------------------------------- 场景矩阵
# 用户要求「各种场景都要考虑到」：把 iSCSI/SAN、Initiator、集群红线、文件协议
# 差异、交付信息模板做成**可查询的产品级说明**，而不是散落在文档里的一句口号。
# 每条都标注 supported 语义：implemented（本轮已落地）/ planned / boundary（不做）。
SCENARIOS = [
    {
        "id": "S1", "name": "DSM 文件服务（SMB / NFS / AFP / FTP / WebDAV）",
        "kind": "文件级", "status": "boundary",
        "summary": "这些是 DSM 向外提供的**文件访问协议**。平台当前不通过 SMB/NFS/WebDAV "
                   "挂载路径备份；本轮通道走 DSM 自带 FileStation Web API（S2），"
                   "群晖/Linux 共享目录也可走已纳管 SSH 主机的文件备份通道。",
        "notes": ["SMB/NFS 挂载点需要平台侧挂载与凭据管理，属另一条通道，未实现不虚报",
                  "iSCSI LUN 是块设备，与 SMB/NFS 的文件级语义完全不同，不能混为一谈"],
    },
    {
        "id": "S2", "name": "FileStation Web API（官方公开）",
        "kind": "文件级", "status": "implemented",
        "summary": "本轮主通道：SYNO.API.Info 能力发现 → SYNO.API.Auth 登录 → "
                   "list_share / list 递归清单 → Download（HTTP Range 断点续传）。"
                   "全量拉全部文件，增量按 size+mtime 差分只拉变化文件，删除记入清单。",
        "notes": ["DSM 侧零安装，平台侧仅用 Python 标准库",
                  "恢复产物落在平台侧目录；推回 DSM 共享目录需 Upload 通道（未实现，如实标注）",
                  "文件级增量，不是块级 CBT"],
    },
    {
        "id": "S3", "name": "SAN Manager / iSCSI Target / LUN",
        "kind": "块级", "status": "boundary",
        "summary": "SAN Manager 输出的是**裸 LUN**（Target + LUN + IQN 访问控制 + "
                   "CHAP / 相互 CHAP + 多重会话 + TCP 3260）。LUN 上没有文件系统，"
                   "文件系统由挂载它的业务主机（Windows/Linux/VMware）管理。",
        "notes": ["平台不直接读裸块，不做块级增量/CBT，不宣称接管 SAN Manager 配置",
                  "正确做法：纳管挂载 LUN 的业务主机，备份其挂载点（文件级）或该主机上的数据库",
                  "VMware ESXi 上 LUN 通常是 VMFS datastore，应走虚拟机备份（/vm）通道"],
    },
    {
        "id": "S4", "name": "Initiator 接入（Windows / Linux / VMware）",
        "kind": "块级", "status": "boundary",
        "summary": "Windows 用 iscsicpl / PowerShell iSCSI cmdlets；Linux 用 "
                   "iscsiadm（open-iscsi）发现并 login；VMware ESXi 用软件 iSCSI "
                   "适配器添加 Target。Multipath/MPIO、存储 VLAN、Jumbo Frame、"
                   "防火墙放行 3260 都由业务主机与网络侧配置。",
        "notes": ["这些配置发生在业务主机，不在 DSM，也不在平台；平台只负责备份结果",
                  "CHAP 凭据由交付信息模板提供，平台不代管 Initiator 配置"],
    },
    {
        "id": "S5", "name": "单 LUN 多主机（集群红线）",
        "kind": "块级", "status": "boundary",
        "summary": "同一个 LUN 同时挂载给多台非集群主机，各自以本地文件系统（NTFS/ext4/XFS）"
                   "写入会**直接损坏数据**。只有使用集群感知文件系统（VMware VMFS、"
                   "OCFS2、GFS2、Windows CSV 等）并配合锁机制才允许多主机共享。",
        "notes": ["这是交付/实施红线，平台侧无法用软件规避",
                  "评估备份方案前必须先确认共享 LUN 上是否确有集群文件系统"],
    },
    {
        "id": "S6", "name": "快照 / Snapshot Replication / Hyper Backup",
        "kind": "设备侧能力", "status": "planned",
        "summary": "DSM 快照、Snapshot Replication、Hyper Backup 属 DSM 内部或非公开接口，"
                   "不同 DSM 版本差异大。当前只做能力发现命中与否的如实展示，"
                   "不调用、不承诺可编排快照，也不把「命中」当作「已纳管快照」。",
        "notes": ["capabilities() 返回的 snapshot/san_manager/storage 仅表示 API 名出现",
                  "需要真实快照编排时按 DSM 版本单独立项验证"],
    },
    {
        "id": "S7", "name": "存储池 / RAID / SHR / 卷 / 扩容",
        "kind": "设备侧能力", "status": "planned",
        "summary": "存储池与卷信息走 SYNO.Storage.CGI.Storage 等非公开接口，"
                   "仅尽力探测；容量告警、扩容、SSD 缓存配置不由平台代管。",
        "notes": ["不做设备侧写操作（平台只读纳管）"],
    },
    {
        "id": "S8", "name": "权限 / ACL / 配额 / 中文与长路径",
        "kind": "文件级", "status": "implemented",
        "summary": "备份按 DSM 返回的 path + size + mtime 还原目录树，文件名按 UTF-8 "
                   "原样保留（含中文）。平台产物不保存 Windows ACL / DSM 权限位、"
                   "共享配额与硬链接语义，恢复后需在目标侧重新授权。",
        "notes": ["目录无权限时清单标记 error=unreadable 并如实跳过，不静默吞掉",
                  "恢复落平台侧目录；权限/属主由目标文件系统决定"],
    },
    {
        "id": "S9", "name": "DSM 6 / 7 / 7.2 版本差异与安全接入",
        "kind": "接入", "status": "implemented",
        "summary": "端口 5000(HTTP) / 5001(HTTPS)，先 SYNO.API.Info.query 拿每个 API 的 "
                   "path 与 maxVersion 再调用，不写死版本。支持自签名证书（verify_ssl 开关）、"
                   "两步验证 OTP（仅探测时临时传入，不落库）、会话过期自动重登。",
        "notes": ["建议使用专用最小权限账号，不要用默认 admin",
                  "OTP 一次性，长期自动化任务请使用未开启 2FA 的专用服务账号"],
    },
    {
        "id": "S10", "name": "部署路线选择（保留 DSM / 刷第三方 OS）",
        "kind": "路线", "status": "implemented",
        "summary": "路线 A（推荐）：保留 DSM，只启用 DSM 自带服务对外提供能力——"
                   "FileStation Web API（平台本轮文件级通道）、SAN Manager 输出 iSCSI LUN、"
                   "或已启用的 SSH。平台零安装纳管，不改设备、不影响原厂能力。"
                   "路线 B（不推荐）：把 NAS 刷成第三方 OS 当普通服务器用——会失去 "
                   "DSM 的 SHR/快照/套件与厂商支持，原有卷可能无法直接识别，且此时"
                   "它已不是「群晖适配」对象，按普通 Linux/Windows 主机纳管即可。",
        "notes": ["路线 B 若已实施，用 ssh_hosts + 文件备份通道，无需群晖专用通道",
                  "平台不代刷固件、不改 DSM 系统配置，纳管只读 + 备份"],
    },
    {
        "id": "S11", "name": "交付信息模板",
        "kind": "交付", "status": "implemented",
        "summary": "实施 iSCSI 场景需业务方提供：DSM 地址与 5000/5001 端口、DSM 版本、"
                   "专用账号、Target IQN、LUN ID/容量、CHAP 用户名与密钥、"
                   "是否启用相互 CHAP、允许多重会话/MPIO、Initiator 侧 IQN、"
                   "业务主机 IP 与 OS、挂载点与文件系统类型、是否集群文件系统、"
                   "存储 VLAN / MTU / 防火墙 3260 放行情况、快照与容量规划。",
        "notes": ["缺任一关键项都不能宣称 iSCSI 场景已纳管",
                  "平台纳管对象是 DSM 与业务主机，不是裸 LUN"],
    },
]

DELIVERY_TEMPLATE = [
    "DSM 地址 / 端口：______（5000 HTTP 或 5001 HTTPS）",
    "DSM 版本 / 型号：______",
    "专用账号 / 密码：______（最小权限，是否启用 2FA：是/否）",
    "Target IQN：______",
    "LUN ID / 容量 / 是否 Thin Provisioning：______",
    "CHAP 用户名 / 密钥 / 相互 CHAP：______",
    "多重会话 / MPIO：______",
    "Initiator IQN（Windows / Linux / ESXi）：______",
    "业务主机 IP / OS / 挂载点 / 文件系统：______",
    "是否集群文件系统（VMFS / CSV / OCFS2 / GFS2）：______",
    "存储 VLAN / MTU / 防火墙 TCP 3260：______",
    "共享文件夹清单与排除规则：______",
    "备份窗口 / RPO / 保留周期：______",
]


def scenarios() -> dict:
    """返回能力/场景矩阵（供页面与 API 展示，避免文档与代码口径漂移）。"""
    return {
        "channels": SCENARIOS,
        "delivery_template": DELIVERY_TEMPLATE,
        "implemented": [s["id"] for s in SCENARIOS if s["status"] == "implemented"],
        "planned": [s["id"] for s in SCENARIOS if s["status"] == "planned"],
        "boundary": [s["id"] for s in SCENARIOS if s["status"] == "boundary"],
        "note": "implemented=本轮已落地并有代码/测试证据；planned=仅能力探测或待立项；"
                "boundary=明确不做的边界（尤其 iSCSI 裸 LUN 不直接读、不承诺块级增量/CBT）。",
    }


# ---------------------------------------------------------------- 表
def init_table(conn=None) -> None:
    """幂等建表（独立 init_table，不改 SCHEMA 大字符串，规避元库方言风险）。"""
    own = conn is None
    conn = conn or db.get_conn()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS %s ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, "
            "hostname TEXT NOT NULL, "
            "port INTEGER DEFAULT 5000, "
            "secure INTEGER DEFAULT 0, "
            "verify_ssl INTEGER DEFAULT 0, "
            "username TEXT DEFAULT '', "
            "password TEXT DEFAULT '', "
            "session_name TEXT DEFAULT 'AIDBM', "
            "timeout INTEGER DEFAULT 15, "
            "dsm_version TEXT DEFAULT '', "
            "model TEXT DEFAULT '', "
            "serial TEXT DEFAULT '', "
            "capabilities TEXT DEFAULT '{}', "
            "last_status TEXT DEFAULT 'unknown', "
            "last_check_at TEXT, "
            "last_error TEXT DEFAULT '', "
            "remark TEXT DEFAULT '', "
            "enabled INTEGER DEFAULT 1, "
            "created_at TEXT, "
            "updated_at TEXT, "
            "UNIQUE(hostname, port, secure))" % TABLE)
        if own:
            conn.commit()
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def _row(row: dict | None, include_secret: bool = False) -> dict | None:
    if row is None:
        return None
    r = dict(row)
    raw_pw = r.get("password") or ""
    r["has_password"] = bool(raw_pw)
    r["password"] = db.decrypt_secret(raw_pw) if include_secret else ""
    for k in ("secure", "verify_ssl", "enabled"):
        r[k] = int(r.get(k) or 0)
    try:
        r["capabilities"] = json.loads(r.get("capabilities") or "{}")
    except (ValueError, TypeError):
        r["capabilities"] = {}
    return r


def list_devices(include_secret: bool = False) -> list:
    rows = db.query("SELECT * FROM %s ORDER BY id DESC" % TABLE)
    return [_row(r, include_secret) for r in rows]


def get_device(device_id: int, include_secret: bool = False) -> Optional[dict]:
    row = db.query_one("SELECT * FROM %s WHERE id=?" % TABLE, (int(device_id),))
    return _row(row, include_secret)


def _normalize(data: dict, creating: bool = False) -> dict:
    d = {k: data.get(k) for k in DEVICE_FIELDS if k in data}
    host = str(d.get("hostname") or "").strip()
    if "://" in host:
        raise DeviceError("主机地址请只填 IP/域名，协议由 HTTPS 开关指定")
    if creating and (not str(d.get("name") or "").strip() or not host):
        raise DeviceError("设备名称与主机地址为必填")
    if "hostname" in d:
        d["hostname"] = host
    for k in ("port", "timeout"):
        if k in d:
            try:
                d[k] = int(d[k] or 0) or (5000 if k == "port" else 15)
            except (TypeError, ValueError):
                d[k] = 5000 if k == "port" else 15
    for k in ("secure", "verify_ssl", "enabled"):
        if k in d:
            d[k] = 1 if d[k] in (1, True, "1", "true", "on", "yes") else 0
    if d.get("password"):
        d["password"] = db.encrypt_secret(d["password"])
    else:
        d.pop("password", None)   # 空密码：新建留空 / 更新不覆盖
    return d


def create_device(data: dict) -> int:
    d = _normalize(data, creating=True)
    dup = db.query_one(
        "SELECT id, name FROM %s WHERE hostname=? AND port=? AND secure=?" % TABLE,
        (d["hostname"], d.get("port") or 5000, d.get("secure") or 0))
    if dup:
        raise DeviceError(
            "纳管记录重复：%s:%s 已登记为「%s」(#%s)，请直接编辑原记录"
            % (d["hostname"], d.get("port") or 5000, dup.get("name"), dup.get("id")))
    now = db.now_iso()
    d.update({"created_at": now, "updated_at": now,
              "last_status": "unknown", "last_error": ""})
    cols = list(d)
    sql = "INSERT INTO %s (%s) VALUES (%s)" % (
        TABLE, ",".join(cols), ",".join("?" * len(cols)))
    try:
        return db.execute(sql, tuple(d.values()))
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise DeviceError("纳管记录重复：同一地址/端口/协议已登记")
        raise


def update_device(device_id: int, data: dict) -> bool:
    if not get_device(device_id):
        return False
    d = _normalize(data)
    if not d:
        return False
    d["updated_at"] = db.now_iso()
    sets = ",".join("%s=?" % k for k in d)
    db.execute("UPDATE %s SET %s WHERE id=?" % (TABLE, sets),
               tuple(d.values()) + (int(device_id),))
    return True


def delete_device(device_id: int) -> bool:
    if not get_device(device_id):
        return False
    db.execute("DELETE FROM %s WHERE id=?" % TABLE, (int(device_id),))
    return True


def _set_probe_state(device_id: int, ok: bool, info: dict, err: str) -> None:
    db.execute(
        "UPDATE %s SET last_status=?, last_check_at=?, last_error=?, "
        "dsm_version=?, model=?, serial=?, capabilities=?, updated_at=? WHERE id=?" % TABLE,
        ("online" if ok else "error", db.now_iso(), (err or "")[:500],
         info.get("version") or "", info.get("model") or "", info.get("serial") or "",
         json.dumps(info.get("capabilities") or {}, ensure_ascii=False),
         db.now_iso(), int(device_id)))


# ---------------------------------------------------------------- 客户端构造
def client_config(device: dict, otp_code: str = "") -> dict:
    """把设备行归一为 DSMClient 构造参数（凭据含明文，仅内部使用）。"""
    return {
        "host": device.get("hostname") or "",
        "port": int(device.get("port") or 5000),
        "secure": bool(device.get("secure")),
        "verify_ssl": bool(device.get("verify_ssl")),
        "username": device.get("username") or "",
        "password": device.get("password") or "",
        "otp_code": otp_code or "",
        "timeout": int(device.get("timeout") or 15),
        "session_name": device.get("session_name") or "AIDBM",
    }


def build_client(device: dict, otp_code: str = "") -> DSMClient:
    return DSMClient(**client_config(device, otp_code))


def _resolve(device_or_id, include_secret=True, otp_code: str = "") -> tuple[dict, DSMClient]:
    dev = get_device(int(device_or_id), include_secret=True) \
        if not isinstance(device_or_id, dict) else dict(device_or_id)
    if not dev:
        raise DeviceError("群晖设备不存在")
    return dev, build_client(dev, otp_code)


# ---------------------------------------------------------------- 探测 / 资产
def probe_device(device_or_id, otp_code: str = "", persist: bool = True) -> dict:
    """真实探活：连接 → 登录 → 能力发现 → 系统信息 → 共享文件夹清单。

    ``device_or_id`` 可以是设备表 id，也可以是**尚未保存**的配置字典（页面
    「先测试再保存」场景）。失败如实返回 error，绝不把连不上包装成在线。
    """
    dev, cli = _resolve(device_or_id, otp_code=otp_code)
    out = {
        "ok": False, "device": dev.get("name") or dev.get("hostname"),
        "host": cli.host, "port": cli.port, "secure": cli.secure,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    info: dict = {}
    try:
        caps = cli.capabilities()
        probe = cli.probe()
        info = cli.system_info() or {}
        out["capabilities"] = caps
        out["public_apis"] = caps.get("public_apis") or []
        out["unofficial"] = caps.get("unofficial") or []
        out["api_count"] = caps.get("api_count") or 0
        out["dsm_version"] = info.get("version") or probe.get("dsm_version") or ""
        out["model"] = info.get("model") or ""
        out["serial"] = info.get("serial") or ""
        out["filestation"] = bool(caps.get("filestation"))
        if not caps.get("filestation"):
            out["warning"] = "该 DSM 未暴露 FileStation.List，文件级备份通道不可用"
        if caps.get("unofficial"):
            out["note"] = ("检测到非公开 API：%s；仅表示名称出现，未调用、不承诺可用"
                           % ", ".join(caps["unofficial"]))
        out["share_count"] = probe.get("share_count") or 0
        out["shares"] = probe.get("shares") or []
        out["ok"] = bool(probe.get("ok"))
        if not out["ok"]:
            out["error"] = probe.get("error") or "DSM 探活失败"
            out["error_code"] = probe.get("error_code") or 0
        try:
            cli.logout()
        except DSMError:
            pass
    except DSMError as e:
        out["error"] = str(e)
        out["error_code"] = e.code
    except Exception as e:  # noqa: BLE001 - 探测失败也要如实回写状态
        out["error"] = str(e).strip() or type(e).__name__
    if persist and not isinstance(device_or_id, dict) and out["ok"]:
        _set_probe_state(int(device_or_id), True,
                         {"version": out["dsm_version"], "model": out["model"],
                          "serial": out["serial"],
                          "capabilities": out.get("capabilities") or {}}, "")
    elif persist and not isinstance(device_or_id, dict):
        _set_probe_state(int(device_or_id), False, {}, out.get("error") or "")
    return out


def list_shares(device_or_id, otp_code: str = "") -> list[dict]:
    """官方 FileStation API 的共享文件夹清单，归一化为页面可用结构。"""
    _dev, cli = _resolve(device_or_id, otp_code=otp_code)
    out = []
    for s in cli.list_shares():
        add = s.get("additional") or {}
        tm = add.get("time") or {}
        out.append({
            "name": s.get("name") or s.get("path") or "",
            "path": s.get("path") or "",
            "real_path": add.get("real_path") or "",
            "size": int(add.get("size") or 0),
            "mtime": int(tm.get("mtime") or 0),
            "writable": bool((add.get("perm") or {}).get("isWritable")),
        })
    return out


def browse_folder(device_or_id, folder_path: str, otp_code: str = "") -> list[dict]:
    """单层目录浏览（资产选择用），不递归、不下载。"""
    if not folder_path or ".." in folder_path.split("/"):
        raise DeviceError("目录路径不合法")
    _dev, cli = _resolve(device_or_id, otp_code=otp_code)
    out = []
    for it in cli.list_folder(folder_path):
        add = it.get("additional") or {}
        tm = add.get("time") or {}
        out.append({
            "path": it.get("path") or "",
            "name": it.get("name") or "",
            "isdir": bool(it.get("isdir")),
            "size": int(add.get("size") or 0),
            "mtime": int(tm.get("mtime") or 0),
        })
    return out


def walk_assets(device_or_id, root_path: str, max_files: int = 200000,
                otp_code: str = "") -> dict:
    """递归资产清单统计（只读枚举，不下载文件体）。"""
    _dev, cli = _resolve(device_or_id, otp_code=otp_code)
    files = cli.walk_files(root_path, max_files=max_files)
    regular = [f for f in files if not f.get("isdir")]
    return {
        "root": root_path,
        "entries": len(files),
        "files": len(regular),
        "dirs": sum(1 for f in files if f.get("isdir")),
        "bytes": sum(int(f.get("size") or 0) for f in regular),
        "unreadable": sum(1 for f in files if f.get("error")),
        "truncated": len(files) >= max_files,
        "sample": [{"path": f.get("path"), "size": f.get("size"),
                    "mtime": f.get("mtime")} for f in regular[:20]],
    }
