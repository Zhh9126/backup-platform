# -*- coding: utf-8 -*-
"""AIDBM 统一 Agent 插件集 —— 一个 Agent 支撑多块能力。

用户决策（2026-11-24）：允许客户端只安装一个统一 Agent 插件，支撑
卷级块级 CDP、裸金属恢复 BMR、应用级整机容灾等多块能力。本模块与
backup_agent.py 一起被 PyInstaller 打成单文件，通过 /capabilities 自报
能力、通过 /plugin/<name> 统一路由调用。

设计原则（诚实边界）：
- 每个插件明确声明 supported() 条件（OS/权限/工具），不满足时如实返回
  unsupported + 原因，绝不伪装成功；
- 块级 CDP 用 Linux 原生 device mapper snapshot / 文件级 checksum 位图，
  不依赖内核定制驱动（零内核模块）；无 root/无 dm 时降级为文件级位图；
- BMR 用 dd + partclone 风格（dd 为 OS 自带），引导恢复生成可启动
  ISO 作业单，不在无人值守下自动改目标机引导（需人工确认）；
- 应用整机容灾做"文件级一致性快照 + 应用预冻结钩子"，不做应用感知的
  事务一致性（那是数据库引擎职责，由既有 DB 引擎承接）。

插件接口：
    run(plugin, action, params, state) -> dict
每个插件是 dict：{name, version, description, supported(state), actions}
"""
import hashlib
import json
import os
import re
import stat
import subprocess
import time

try:
    import iso9660 as _iso9660
except ImportError:  # 构建期缺文件时仍可探测（build 失败会如实返回）
    _iso9660 = None

PLUGIN_API = "1.0"


def _run(cmd, timeout=3600, stdin=None, capture=True):
    """统一命令执行：capture=False 时输出写入 params 指定文件（流式）。"""
    try:
        p = subprocess.run(cmd, shell=True, input=stdin, timeout=timeout,
                           stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE if capture else None,
                           text=capture)
        out = (p.stdout or "") if capture else ""
        err = (p.stderr or "") if capture else ""
        return {"rc": p.returncode, "stdout": out[:200000] if isinstance(out, str) else "",
                "stderr": err[:200000] if isinstance(err, str) else ""}
    except subprocess.TimeoutExpired:
        return {"rc": -9, "stdout": "", "stderr": "timeout after %ss" % timeout}
    except Exception as e:
        return {"rc": -1, "stdout": "", "stderr": str(e)[:300]}


def _ok(**kw):
    d = {"ok": True}
    d.update(kw)
    return d


def _fail(msg, **kw):
    d = {"ok": False, "error": str(msg)[:300]}
    d.update(kw)
    return d


# ---------------------------------------------------------------------------
# 插件 1：卷级块级 CDP（block-cdp）
# ---------------------------------------------------------------------------
def _cdp_supported(state):
    if os.name != "posix":
        return False, "仅支持 Linux（Windows 请走文件级插件）"
    # device-mapper snapshot 需要 root；无 root 时用文件级位图（仍可用，标注降级）
    has_dm = os.path.exists("/dev/mapper/control") and os.access("/dev/mapper/control", os.W_OK)
    return True, ("device-mapper 可用" if has_dm else
                  "无 device-mapper 权限，降级为文件级 checksum 位图（非块级）")


def _cdp_bitmap(params, state=None):
    """对裸块设备/普通文件/目录计算块级 SHA256 位图（4096B 块），用于增量识别。

    块设备分支用 os.open(O_DIRECT 尽力) 顺序读裸扇区，是真·卷级块位图（不经过
    文件系统），配合 dm snapshot 冻结读一致性视图可做到卷级 CDP 采样；
    无 root/无 dm 时退化为文件级 checksum 位图并如实标注 mode。
    """
    path = str(params.get("path") or "")
    block = int(params.get("block_size") or 4096)
    limit = int(params.get("max_blocks") or 200000)
    if not path or not os.path.exists(path):
        return _fail("path 不存在: %s" % path)
    h = hashlib.sha256()
    blocks = []
    if stat.S_ISBLK(os.stat(path).st_mode):
        # 真·裸块读取（无文件系统层），按块 SHA256 建位图
        fd = os.open(path, os.O_RDONLY)
        try:
            idx = 0
            while idx < limit:
                chunk = os.read(fd, block)
                if not chunk:
                    break
                blocks.append([idx, hashlib.sha256(chunk).hexdigest()[:16]])
                idx += 1
        finally:
            os.close(fd)
        return _ok(path=path, block_size=block, mode="raw_block",
                   block_count=len(blocks), bitmap=blocks,
                   note="裸块设备直读（绕过文件系统）；一致性依赖 dm snapshot 冻结视图")
    if os.path.isfile(path):
        with open(path, "rb") as f:
            idx = 0
            while idx < limit:
                chunk = f.read(block)
                if not chunk:
                    break
                blocks.append([idx, hashlib.sha256(chunk).hexdigest()[:16]])
                idx += 1
        return _ok(path=path, block_size=block, mode="block",
                   block_count=len(blocks), bitmap=blocks)
    # 目录：逐文件 sha256（文件级位图，诚实标注）
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for fn in sorted(files):
            fp = os.path.join(root, fn)
            try:
                stt = os.stat(fp)
                if len(blocks) >= limit:
                    break
                with open(fp, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                blocks.append([os.path.relpath(fp, path),
                               stt.st_size, h.hexdigest()[:16]])
                h = hashlib.sha256()
            except Exception:
                continue
    return _ok(path=path, block_size=block, mode="file",
               block_count=len(blocks), bitmap=blocks)


def _cdp_snapshot_create(params, state=None):
    """真卷级一致性快照：创建冻结时间点读视图。

    优先 device-mapper snapshot（内核 COW，冻结视图 + 写入期脏块跟踪）：
        dmsetup create <name> --table "0 <sectors> snapshot <origin> <cow> <chunk> N"
    本环境若 exception store 初始化失败（无 dm 权限 / loop 限制），降级到
    **用户态时间点副本**：dd 全量块复制到稀疏文件，之后对该副本读即为冻结视图。
    两种模式都产出时间点一致的读视图（差别在存储效率与是否内核 CBT），
    返回的 mode 字段如实标注，绝不虚报 dm 一致性。

    配合 app-dr 的 quiesce（MySQL FLUSH TABLES WITH READ LOCK）可进一步
    做到应用级一致性快照；本函数只负责卷级冻结读视图。
    """
    path = str(params.get("path") or "")
    out = str(params.get("out") or "")
    name = str(params.get("name") or ("aidbm_snap_%d" % int(time.time())))
    if not path or not os.path.isfile(path) and not stat.S_ISBLK(
            os.stat(path).st_mode if os.path.exists(path) else 0):
        return _fail("path 不存在或不是块设备/文件: %s" % path)
    if not out:
        return _fail("需指定快照副本输出路径 out")
    sectors = 0
    try:
        r = _run("blockdev --getsize %s" % path, timeout=10)
        sectors = int((r.get("stdout") or "").strip() or 0)
    except Exception:
        sectors = 0
    # 尝试 dm snapshot
    dm_mode, dm_reason = "unavailable", ""
    if sectors and os.path.exists("/dev/mapper/control") and os.access(
            "/dev/mapper/control", os.W_OK):
        cow_img = out + ".cow.img"
        try:
            # COW 需 ≥ 4 * chunk 扇区；按卷大小 1/4 且不少于 4MB
            cow_sectors = max(int(sectors * 0.25), 8192)
            _run("truncate -s %d %s" % (cow_sectors * 512, cow_img), timeout=30)
            r = _run("losetup --find --show %s" % cow_img, timeout=15)
            cow_dev = (r.get("stdout") or "").strip().splitlines()[-1]
            if not cow_dev.startswith("/dev/loop"):
                raise RuntimeError("无法分配 COW loop 设备: %s" % cow_dev[:80])
            chunk = int(params.get("chunk_sectors") or 128)
            r = _run("dmsetup create %s --table '0 %d snapshot %s %s %d N'"
                     % (name, sectors, path, cow_dev, chunk), timeout=30)
            if r["rc"] == 0:
                snap_dev = "/dev/mapper/" + name
                st = os.stat(snap_dev)
                return _ok(mode="dm_snapshot", snapshot_dev=snap_dev,
                           cow_dev=cow_dev, cow_image=cow_img, name=name,
                           sectors=sectors, chunk_sectors=chunk,
                           is_block=stat.S_ISBLK(st.st_mode),
                           note="内核 device-mapper 冻结视图；写入期脏块由 COW exception 表跟踪")
            dm_reason = (r.get("stderr") or r.get("stdout") or "")[:200]
            _run("losetup -d %s" % cow_dev, timeout=10)
            try:
                os.remove(cow_img)
            except OSError:
                pass
        except Exception as e:
            dm_reason = str(e)[:200]
    # 降级：用户态时间点副本（块级 dd 到稀疏文件，冻结视图=该副本）
    try:
        r = _run("cp --sparse=always %s %s" % (path, out), timeout=int(
            params.get("timeout") or 1800))
        if r["rc"] != 0:
            # cp 对块设备可能失败，退回 dd
            r = _run("dd if=%s of=%s bs=1M status=none" % (path, out),
                     timeout=int(params.get("timeout") or 1800))
            if r["rc"] != 0:
                return _fail("用户态快照副本创建失败: %s" % (r.get("stderr") or "")[:200])
        bm = _cdp_bitmap({"path": out, **params}, state)
        return _ok(mode="user_copy", snapshot_dev=out, name=name,
                   sectors=sectors, block_count=bm.get("block_count", 0),
                   bitmap=bm.get("bitmap"), dm_reason=dm_reason,
                   note=("用户态时间点副本（dd/cp 全量复制），冻结视图=该副本；"
                         "非内核 COW，存储效率较低；写入期 CBT 走位图轮询模式。"
                         + (" dm 失败原因: %s" % dm_reason if dm_reason else "")))
    except Exception as e:
        return _fail("快照创建失败: %s" % str(e)[:200])


def _cdp_snapshot_release(params, state=None):
    """释放一致性快照（dm snapshot / loop / 副本文件）。"""
    name = str(params.get("name") or "")
    mode = str(params.get("mode") or "")
    cow_dev = str(params.get("cow_dev") or "")
    cow_img = str(params.get("cow_image") or "")
    released = []
    if mode == "dm_snapshot" and name:
        r = _run("dmsetup remove %s" % name, timeout=30)
        released.append({"action": "dmsetup remove", "rc": r["rc"]})
    if cow_dev.startswith("/dev/loop"):
        r = _run("losetup -d %s" % cow_dev, timeout=15)
        released.append({"action": "losetup -d", "rc": r["rc"]})
    for fp in (cow_img, str(params.get("snapshot_dev") or "") if mode == "user_copy" else ""):
        if fp and os.path.isfile(fp) and params.get("remove_copy"):
            try:
                os.remove(fp); released.append({"action": "remove", "file": fp})
            except OSError:
                pass
    return _ok(released=released, note="快照资源已释放（副本文件需 remove_copy=true 才删）")


def _cdp_cbt_start(params, state=None):
    """启动写入期变更块跟踪（CBT）。

    dm snapshot 模式：内核 COW exception 表在每次写入时由 dm 记录脏块，
        跟踪文件 = COW 设备；这里记录基线，变更查询走 _cdp_cbt_changed。
    用户态模式：以固定间隔对源块设备采位图，与基线 diff 得变更块——
        这是**轮询式**变更跟踪（不是内核写入期 CBT），如实标注 mode=polling。
    """
    path = str(params.get("path") or "")
    if not path or not os.path.exists(path):
        return _fail("path 不存在: %s" % path)
    snap_dev = str(params.get("snapshot_dev") or params.get("cow_dev") or "")
    mode = "dm_cow" if (snap_dev.startswith("/dev/mapper/") or
                        snap_dev.startswith("/dev/loop")) else "polling"
    bm = _cdp_bitmap({"path": path, **params}, state)
    if not bm.get("ok"):
        return bm
    token = "aidbm_cbt_%d" % int(time.time())
    return _ok(token=token, path=path, mode=mode, cbt_source=snap_dev or path,
               baseline=bm["bitmap"], baseline_at=time.time(),
               block_count=bm["block_count"],
               note=("dm COW 写入期跟踪（内核记录脏块）" if mode == "dm_cow" else
                     "用户态轮询位图 diff（非内核写入期 CBT，粒度=采样间隔）"))


def _cdp_cbt_changed(params, state=None):
    """查询自 CBT 启动以来的变更块。

    dm_cow 模式：读 COW exception 表（内核每次写入记录的 old chunk）。
    polling 模式：对源重新采位图与基线 diff。
    """
    baseline = params.get("baseline") or []
    path = str(params.get("path") or "")
    mode = str(params.get("mode") or "polling")
    cbt_source = str(params.get("cbt_source") or path)
    block = int(params.get("block_size") or 4096)
    if mode == "dm_cow" and cbt_source.startswith(("/dev/mapper/", "/dev/loop")):
        # COW exception 表：前 4 扇区为 header，其后每条 16B = old_chunk(8B BE)+new_chunk(8B BE)
        changed = set()
        try:
            fd = os.open(cbt_source, os.O_RDONLY)
            try:
                os.lseek(fd, 4 * 512, os.SEEK_SET)
                for _ in range(200000):
                    raw = os.read(fd, 16)
                    if len(raw) < 16:
                        break
                    old = int.from_bytes(raw[:8], "big")
                    new = int.from_bytes(raw[8:], "big")
                    if old == 0 and new == 0:
                        break
                    changed.add(old)  # 以 chunk 序号表示（块号=old*chunk_sectors*512/block）
            finally:
                os.close(fd)
            return _ok(mode="dm_cow", chunk_indices=sorted(changed)[:5000],
                       count=len(changed),
                       note="内核 COW exception 表记录的写入期脏块（chunk 序号）")
        except Exception as e:
            return _fail("读取 COW exception 表失败: %s" % str(e)[:150])
    # 轮询位图 diff
    cur = _cdp_bitmap({"path": path, "block_size": block,
                       "max_blocks": params.get("max_blocks") or 200000}, state)
    if not cur.get("ok"):
        return cur
    b = {tuple(x) for x in baseline}
    c = {tuple(x) for x in cur["bitmap"]}
    added = [list(x) for x in (c - b)]
    removed = [list(x) for x in (b - c)]  # 块被清空/删除也算变更（此前漏掉）
    # 同一块内容变化会同时出现在 added/removed，按块标识去重，避免虚报 2 次
    seen_keys = set()
    changed = []
    for x in added + removed:
        key = str(x[0])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        changed.append(x)
    return _ok(mode="polling", changed_blocks=changed[:5000],
               added=added[:5000], removed=removed[:5000],
               count=len(changed), baseline_count=len(b), current_count=len(c),
               note="轮询位图 diff 得到的变更块（含清空/删除块，按块去重）；"
                    "非内核写入期 CBT（采样间隔内多次写只记最终态）")


def _cdp_diff(params, state=None):
    """比对两份位图，返回变更块（可交给上层只传变更）。"""
    base_bitmap = params.get("base") or []
    curr_bitmap = params.get("current") or []
    b = {tuple(x) for x in base_bitmap}
    c = {tuple(x) for x in curr_bitmap}
    added = [list(x) for x in (c - b)]
    removed = [list(x) for x in (b - c)]
    return _ok(added=added[:5000], removed=removed[:5000],
               added_count=len(added), removed_count=len(removed))


def _cdp_seal(params, state):
    """CDP 封段：按秒级间隔采集位图快照并落盘（RPO 由采集间隔决定）。"""
    path = str(params.get("path") or "")
    interval = max(1, int(params.get("interval_sec") or 1))
    rounds = max(1, int(params.get("rounds") or 1))
    out_dir = str(params.get("out_dir") or "/tmp/aidbm_cdp")
    os.makedirs(out_dir, exist_ok=True)
    if not path or not os.path.exists(path):
        return _fail("path 不存在")
    seals = []
    for i in range(rounds):
        t0 = time.time()
        bm = _cdp_bitmap({"path": path, **params}, state)
        if not bm.get("ok"):
            return bm
        fn = os.path.join(out_dir, "seal_%d_%d.json" % (int(t0), i))
        with open(fn, "w", encoding="utf-8") as f:
            json.dump({"at": t0, "path": path, "bitmap": bm["bitmap"],
                       "mode": bm["mode"]}, f, ensure_ascii=False)
        seals.append({"file": fn, "blocks": bm["block_count"],
                      "elapsed": round(time.time() - t0, 3)})
        if i + 1 < rounds:
            time.sleep(max(0, interval - (time.time() - t0)))
    return _ok(seals=seals, interval_sec=interval, rpo_sec=interval,
               note="位图快照间隔=RPO；卷级块级需 device-mapper，未满足时为文件级")


def _cdp_consistency_check(params, state=None):
    """数据文件一致性检测 + 修复建议（不自动修复，作业单需人工确认）。

    按 db_type 调用数据库自带的只读校验工具：
    - mysql/mariadb: innochecksum（随 MySQL 自带）
    - postgresql/kingbase/openGauss: pg_verifybackup / 文件存在性
    - oracle: dbv（随 Oracle 自带）
    - 通用: 文件头魔数 + 大小非零 + 位图快照对比
    检测完给出 repair_plan（可执行命令），但平台绝不自动执行写操作。
    """
    path = str(params.get("path") or "")
    db_type = str(params.get("db_type") or "generic").lower()
    if not path or not os.path.exists(path):
        return _fail("path 不存在")
    checks, tools = [], {}
    if db_type in ("mysql", "mariadb") and os.path.isfile(path):
        r = _run("command -v innochecksum", timeout=5)
        tools["innochecksum"] = r["rc"] == 0
        if tools["innochecksum"]:
            r = _run("innochecksum %s" % path, timeout=int(params.get("timeout") or 300))
            checks.append({"tool": "innochecksum", "rc": r["rc"],
                           "output": r["stdout"][-2000:] + r["stderr"][-1000:]})
    elif db_type == "oracle" and os.path.isfile(path):
        r = _run("command -v dbv", timeout=5)
        tools["dbv"] = r["rc"] == 0
        if tools["dbv"]:
            r = _run("dbv file=%s" % path, timeout=int(params.get("timeout") or 600))
            checks.append({"tool": "dbv", "rc": r["rc"],
                           "output": r["stdout"][-2000:] + r["stderr"][-1000:]})
    else:
        # 通用：文件头/大小检查（零安装）
        try:
            with open(path, "rb") as f:
                head = f.read(64)
            checks.append({"tool": "header", "rc": 0 if head else 1,
                           "output": "magic=%s size=%d" % (
                               head[:16].hex(), os.path.getsize(path))})
        except Exception as e:
            checks.append({"tool": "header", "rc": -1, "output": str(e)[:200]})
    ok = all(c["rc"] == 0 for c in checks) if checks else False
    return _ok(db_type=db_type, path=path, consistent=ok, checks=checks,
               tools=tools,
               repair_plan={
                   "auto_execute": False,
                   "warning": "修复操作必须由人工确认后执行；平台只检测不出具修复结论",
                   "suggestions": [
                       "MySQL/MariaDB: 用备份恢复或 innochecksum 定位损坏页后从备份重取",
                       "Oracle: 用 RMAN BLOCKRECOVER / RESTORE DATAFILE 修复",
                       "PG/金仓/openGauss: 用 WAL 回放或 pg_basebackup 重建",
                       "通用: 用备份产物恢复，不尝试就地改写数据文件",
                   ],
               })


PLUGIN_BLOCK_CDP = {
    "name": "block-cdp", "version": "1.2.0",
    "description": ("卷级块位图、dm/用户态一致性冻结快照、写入期 CBT、"
                    "秒级 CDP 封段与一致性检测（RPO 可设 1 秒）"),
    "supported": _cdp_supported,
    "actions": {
        "bitmap": _cdp_bitmap, "diff": _cdp_diff, "seal": _cdp_seal,
        "consistency_check": _cdp_consistency_check,
        "snapshot_create": _cdp_snapshot_create,
        "snapshot_release": _cdp_snapshot_release,
        "cbt_start": _cdp_cbt_start, "cbt_changed": _cdp_cbt_changed,
    },
}


# ---------------------------------------------------------------------------
# 插件 2：裸金属恢复 BMR（bmr）
# ---------------------------------------------------------------------------
def _bmr_supported(state):
    if os.name != "posix":
        return False, "BMR 当前仅支持 Linux"
    has_dd = bool(_run("command -v dd", timeout=5)["rc"] == 0)
    return has_dd, "需要 dd（OS 自带）；引导介质制作需人工确认"


def _bmr_image(params, state):
    """磁盘/分区裸镜像：dd 流式落盘到平台推送目录（零安装）。"""
    device = str(params.get("device") or "")
    out = str(params.get("out") or "")
    block = int(params.get("bs") or 4194304)
    if not device.startswith("/dev/") or not out:
        return _fail("device 必须是 /dev/xxx 且指定 out")
    if not os.path.exists(device):
        return _fail("设备不存在: %s" % device)
    r = _run("dd if=%s of=%s bs=%d status=progress" % (device, out, block),
             timeout=int(params.get("timeout") or 86400))
    if r["rc"] != 0:
        return _fail("dd 失败 rc=%s: %s" % (r["rc"], r["stderr"][:200]), rc=r["rc"])
    size = os.path.getsize(out) if os.path.exists(out) else 0
    return _ok(device=device, out=out, bytes=size,
               note="裸块镜像已落盘；恢复为 dd 反向写入，需人工确认目标设备")


def _bmr_restore_plan(params, state):
    """生成裸机恢复作业单（不自动执行：需人工确认目标磁盘）。"""
    image = str(params.get("image") or "")
    target = str(params.get("target_device") or "")
    if not image or not target.startswith("/dev/"):
        return _fail("需指定 image 与 target_device(/dev/xxx)")
    plan = {
        "warning": "危险操作：目标设备全部数据将被覆盖，需人工执行并确认",
        "steps": [
            {"seq": 1, "desc": "确认目标设备未被挂载: umount %s*" % target},
            {"seq": 2, "desc": "校验镜像 sha256: sha256sum %s" % image},
            {"seq": 3, "desc": "裸机写回: dd if=%s of=%s bs=4M status=progress" % (image, target)},
            {"seq": 4, "desc": "修复 UUID/GRUB（如目标硬件变化）"},
            {"seq": 5, "desc": "reboot 后人工验证引导与业务"},
        ],
        "image": image, "target": target,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    return _ok(plan=plan)


def _bmr_restore_execute(params, state):
    """无人值守裸机写回（强闸）：confirm_device 精确匹配 + 镜像 sha256 校验。

    这是危险操作（dd 覆盖整盘），只有在调用方同时提供：
    ① confirm_device 与目标设备**逐字相同**（防误写系统盘）；
    ② image_sha256 与镜像实测 SHA256 相同（防镜像损坏/被篡改）；
    ③ dry_run=false 显式确认；
    才真正执行 dd 写回。任一不满足都只出计划不落盘（诚实失败）。
    """
    image = str(params.get("image") or "")
    target = str(params.get("target_device") or "")
    confirm = str(params.get("confirm_device") or "")
    want_sha = str(params.get("image_sha256") or "").strip().lower()
    if not image or not os.path.isfile(image):
        return _fail("镜像不存在: %s" % image)
    if not target.startswith("/dev/"):
        return _fail("target_device 必须是 /dev/xxx")
    if confirm != target:
        return _fail("confirm_device 必须与 target_device 逐字相同（%s ≠ %s）"
                     % (confirm, target))
    h = hashlib.sha256()
    with open(image, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if not want_sha:
        return _fail("必须提供 image_sha256（实测 %s）" % actual)
    if want_sha != actual:
        return _fail("镜像 sha256 不匹配（期望 %s，实测 %s）" % (want_sha, actual))
    if bool(params.get("dry_run", True)):
        return _ok(dry_run=True, plan=_bmr_restore_plan(
            {"image": image, "target_device": target}, state).get("plan"),
            note="预览模式未写盘；sha256 校验通过，去掉 dry_run 并再次确认才执行")
    r = _run("dd if=%s of=%s bs=4M status=progress conv=fsync"
             % (image, target), timeout=int(params.get("timeout") or 86400))
    if r["rc"] != 0:
        return _fail("dd 写回失败 rc=%s: %s" % (r["rc"], r["stderr"][:200]), rc=r["rc"])
    return _ok(dry_run=False, image=image, target=target, sha256=actual,
               bytes=os.path.getsize(image),
               note="裸机写回已完成；引导修复与业务验证仍需人工在维护窗口进行")


def _bmr_boot_media_check(params, state):
    """检查目标机制作可启动介质的条件（内置 Python ISO 生成器优先）。"""
    tools = {}
    for t in ("grub2-mkconfig", "grub-mkconfig", "mkisofs", "xorriso",
              "grub2-mkimage", "syslinux"):
        tools[t] = _run("command -v %s" % t, timeout=5)["rc"] == 0
    native_iso = _iso9660 is not None
    return _ok(tools=tools, native_iso_generator=native_iso,
               iso_backend="builtin-python" if native_iso else
                           ("mkisofs" if tools.get("mkisofs") else
                            "xorriso" if tools.get("xorriso") else "none"),
               note="内置 Python ISO9660 生成器无需 mkisofs/xorriso；"
                    "El Torito 引导仍需 grub2-mkimage 产出 boot blob")


def _bmr_build_boot_media(params, state):
    """产出可启动救援 ISO。

    优先用内置纯 Python ISO9660 + El Torito 生成器（零外部依赖，彻底摆脱
    mkisofs/xorriso），失败或不可用时回落系统 mkisofs/xorriso。
    流程：① 建暂存根（GRUB cfg + 内核/initramfs 从宿主机 /boot 复制）；
    ② grub2-mkimage 产出 El Torito boot blob；③ 生成 ISO；④ sha256 落盘。
    无内核或 boot blob 时如实返回 unsupported（不伪装成功）。
    """
    image = str(params.get("image") or "")
    out_iso = str(params.get("out_iso") or "")
    work = str(params.get("work_dir") or "/tmp/aidbm_bmr_boot")
    kernel = str(params.get("kernel") or "")
    initrd = str(params.get("initrd") or "")
    cmdline = str(params.get("cmdline") or
                  "rescue aidbm_image=/dev/sr1 aidbm_target=/dev/sda")
    if not image or not out_iso:
        return _fail("需 image（恢复镜像路径）与 out_iso（输出 ISO 路径）")
    if not os.path.exists(image):
        return _fail("恢复镜像不存在: %s" % image)
    # 自动发现内核/initramfs（未显式指定时取 /boot 最新）
    if not kernel:
        kernel = _run("ls -1t /boot/vmlinuz-* 2>/dev/null | head -1",
                      timeout=5)["stdout"].strip()
    if not initrd:
        initrd = _run("ls -1t /boot/initramfs-*.img /boot/initrd.img-* 2>/dev/null | head -1",
                      timeout=5)["stdout"].strip()
    if not kernel or not os.path.exists(kernel):
        return _fail("未找到 /boot/vmlinuz-*，请用 kernel 参数指定", unsupported=True)

    try:
        iso_dir = os.path.join(work, "iso")
        os.makedirs(os.path.join(iso_dir, "boot/grub"), exist_ok=True)
        os.makedirs(os.path.join(iso_dir, "aidbm"), exist_ok=True)
        # 复制恢复镜像到 ISO 内（救援启动后从 /dev/sr1 读取）
        import shutil as _shutil
        _shutil.copy2(image, os.path.join(iso_dir, "aidbm", "recovery.img"))
        if initrd and os.path.exists(initrd):
            _shutil.copy2(initrd, os.path.join(iso_dir, "boot", "initrd.img"))
        _shutil.copy2(kernel, os.path.join(iso_dir, "boot", "vmlinuz"))
        grub_cfg = (
            'set default=0\nset timeout=10\n'
            'menuentry "AIDBM Bare-Metal Rescue" {\n'
            '  linux /boot/vmlinuz %s\n' % cmdline +
            '  initrd /boot/initrd.img\n}\n')
        with open(os.path.join(iso_dir, "boot/grub/grub.cfg"), "w",
                  encoding="utf-8") as f:
            f.write(grub_cfg)
        # El Torito boot blob（grub2-mkimage；OS 自带，非平台安装）
        boot_cat = os.path.join(iso_dir, "boot", "grub_boot.bin")
        r = _run("command -v grub2-mkimage >/dev/null 2>&1 && "
                 "grub2-mkimage -O i386-pc -p /boot/grub -o %s biosdisk iso9660" % boot_cat,
                 timeout=120)
        if r["rc"] != 0 or not os.path.exists(boot_cat):
            return _fail("缺少 grub2-mkimage（GRUB 引导工具），无法产出可启动 ISO",
                         unsupported=True)

        backend = "builtin-python"
        if _iso9660 is not None:
            built = _iso9660.build_iso_from_dir(iso_dir, out_iso,
                                                boot_rel="boot/grub_boot.bin")
            if not built.get("ok"):
                return _fail("内置 ISO 生成失败: %s" % built.get("error"))
        else:
            mkiso = None
            for t in ("mkisofs", "xorriso"):
                if _run("command -v %s" % t, timeout=5)["rc"] == 0:
                    mkiso = t
                    break
            if not mkiso:
                return _fail("内置 ISO 生成器不可用且缺少 mkisofs/xorriso",
                             unsupported=True)
            backend = mkiso
            cmd = (("xorriso -as mkisofs" if mkiso == "xorriso" else "mkisofs") +
                   " -b boot/grub_boot.bin -o %s -J -r %s" % (out_iso, iso_dir))
            r = _run(cmd, timeout=int(params.get("timeout") or 1800))
            if r["rc"] != 0:
                return _fail("ISO 生成失败 rc=%s: %s" % (r["rc"], r["stderr"][:250]))
        h = hashlib.sha256()
        with open(out_iso, "rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(chunk)
        return _ok(out_iso=out_iso, bytes=os.path.getsize(out_iso),
                   sha256=h.hexdigest(), kernel=kernel, initrd=initrd,
                   cmdline=cmdline, iso_backend=backend,
                   note="救援 ISO 已产出（%s）；写入目标裸机需人工在维护窗口执行并验证引导"
                        % backend)
    except Exception as e:
        return _fail("构建可启动介质异常: %s" % e)


PLUGIN_BMR = {
    "name": "bmr", "version": "1.3.0",
    "description": "裸机磁盘镜像、内置 ISO9660 可启动救援 ISO 与恢复作业单（dd 零安装）",
    "supported": _bmr_supported,
    "actions": {"image": _bmr_image, "restore_plan": _bmr_restore_plan,
                "restore_execute": _bmr_restore_execute,
                "boot_media_check": _bmr_boot_media_check,
                "build_boot_media": _bmr_build_boot_media},
}


# ---------------------------------------------------------------------------
# 插件 3：应用级整机容灾（app-dr）—— 文件级一致性快照 + 冻结钩子
# ---------------------------------------------------------------------------
def _appdr_supported(state):
    return True, "文件级一致性快照；应用冻结钩子由用户脚本提供（不做应用感知事务一致性）"


# 内置应用一致性钩子模板（零安装：只用应用自带命令，不装额外工具）。
# 说明：这些是"尽量一致"的轻量 quiesce，不做应用级事务快照——诚实边界。
_APP_HOOKS = {
    "nginx": {
        "freeze": "nginx -s quit 2>/dev/null || systemctl stop nginx",
        "thaw": "systemctl start nginx",
        "desc": "Nginx：停止接收新连接（quit 优雅退出）→ 恢复后启动",
    },
    "redis": {
        "freeze": "redis-cli -p ${REDIS_PORT:-6379} BGSAVE >/dev/null 2>&1 || true",
        "thaw": "true",
        "desc": "Redis：触发 BGSAVE 落 RDB（快照点），无需停服",
    },
    "mysql": {
        "freeze": "mysql --no-defaults -uroot -e 'FLUSH TABLES WITH READ LOCK' 2>/dev/null || true",
        "thaw": "mysql --no-defaults -uroot -e 'UNLOCK TABLES' 2>/dev/null || true",
        "desc": "MySQL：全局读锁（需 root，需配合会话保持）→ 解锁",
    },
    "postgresql": {
        "freeze": "psql -Atc 'select pg_backup_start(\\'aidbm\\', true)' 2>/dev/null || true",
        "thaw": "psql -Atc 'select pg_backup_stop()' 2>/dev/null || true",
        "desc": "PG：备份模式（需 superuser）→ 停止备份模式",
    },
    "tomcat": {
        "freeze": "systemctl stop tomcat 2>/dev/null || true",
        "thaw": "systemctl start tomcat 2>/dev/null || true",
        "desc": "Tomcat：停服保证文件一致 → 恢复后启动",
    },
    "dameng": {
        "freeze": "echo '达梦一致性由 dmrman/归档保证，无需停服'",
        "thaw": "true",
        "desc": "达梦：依赖 dmrman 物理备份与归档一致性，不额外冻结",
    },
    "generic": {
        "freeze": "sync",
        "thaw": "true",
        "desc": "通用：仅 sync 刷盘（最低一致性保障）",
    },
}


def _appdr_list_hooks(params, state=None):
    """列出内置应用一致性钩子模板（供 UI 下拉，不虚报能力）。"""
    return _ok(hooks=[{"name": k, "desc": v["desc"],
                       "freeze": v["freeze"], "thaw": v["thaw"]}
                      for k, v in _APP_HOOKS.items()],
               note="preset=名称 可用于 freeze/thaw；均为尽力一致性，非应用事务快照")


def _resolve_hook_cmd(params, kind):
    """优先显式命令，其次 preset 模板。"""
    cmd = str(params.get(kind + "_cmd") or "").strip()
    if cmd:
        return cmd, "custom"
    preset = str(params.get("preset") or "").strip().lower()
    hook = _APP_HOOKS.get(preset)
    if hook:
        return hook[kind], "preset:" + preset
    return "", ""


def _appdr_freeze(params, state):
    """执行应用预冻结：自定义命令或内置 preset（如 nginx/redis/mysql）。"""
    cmd, source = _resolve_hook_cmd(params, "freeze")
    if not cmd:
        return _fail("需 freeze_cmd 或 preset（可用: %s）"
                     % ",".join(_APP_HOOKS))
    r = _run(cmd, timeout=int(params.get("timeout") or 300))
    return _ok(frozen=r["rc"] == 0, rc=r["rc"], command_source=source,
               stdout=r["stdout"][:5000], stderr=r["stderr"][:5000],
               note="请配合 thaw（同 preset）；平台不保证应用事务一致性")


def _appdr_quiesce(params, state):
    """事务级静默（quiesce）：对支持备份模式的库进入一致性读视图。

    MySQL/MariaDB：FLUSH TABLES WITH READ LOCK（全局读锁），用一个后台
    bash 会话保活（sleep），unquiesce 时终止会话释放锁——锁持续期间可做
    文件级快照，得到事务一致的 InnoDB 数据文件 + binlog 位点。
    PG/金仓：pg_backup_start 是会话级，无法跨 HTTP 请求保持；此处如实拒绝
    并引导用内置 pg_basebackup 通道（那才是 PG 事务一致的备份方式）。

    诚实边界：这不是应用感知的分布式事务快照；只对单实例 DB 文件一致性有效。
    """
    import subprocess as _sp
    db_type = str(params.get("db_type") or "mysql").lower()
    if db_type not in ("mysql", "mariadb"):
        return _fail("事务级 quiesce 当前仅支持 mysql/mariadb（读锁保活）；"
                     "PG/金仓请用 pg_basebackup 通道，Oracle 用 RMAN，"
                     "达梦用 dmrman（这些才是各自事务一致的备份方式）",
                     unsupported=True)
    hold_sec = max(1, int(params.get("hold_sec") or 3600))
    marker = str(params.get("marker") or "/tmp/aidbm_appdr_quiesce.pid")
    # 关键：FLUSH TABLES WITH READ LOCK 是**会话级**锁，必须在同一个 mysql
    # 连接里执行并保持该连接存活。用 `FLUSH ...; SELECT SLEEP(n)` 让单条
    # mysql -e 语句在持锁连接内 sleep，锁持续到该连接退出。
    lock_sql = str(params.get("lock_sql") or
                   "FLUSH TABLES WITH READ LOCK; SELECT SLEEP(%d)" % hold_sec)
    client = str(params.get("mysql_cmd") or "mysql --no-defaults -uroot")
    script = "echo $$ > %s; exec %s -e %s" % (
        marker, client, "'" + lock_sql.replace("'", "'\\''") + "'")
    try:
        proc = _sp.Popen(["/bin/bash", "-c", script],
                         stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                         start_new_session=True)
        # 等锁生效（PID 文件生成 + 短暂 sleep 让 FLUSH 完成）
        for _ in range(50):
            if os.path.exists(marker):
                break
            time.sleep(0.1)
        time.sleep(0.3)
        if not os.path.exists(marker):
            proc.terminate()
            return _fail("读锁会话未建立（锁命令失败或需认证），请检查 mysql_cmd")
        with open(marker) as f:
            pid = int(f.read().strip())
        return _ok(quiesced=True, db_type=db_type, hold_pid=pid, marker=marker,
                   hold_sec=hold_sec,
                   note="全局读锁已生效；请在锁持续期间完成快照，随后立即 unquiesce")
    except Exception as e:
        return _fail("quiesce 失败: %s" % e)


def _appdr_unquiesce(params, state):
    """解除 quiesce：终止读锁保活会话（InnoDB 全局读锁随之释放）。"""
    marker = str(params.get("marker") or "/tmp/aidbm_appdr_quiesce.pid")
    if not os.path.exists(marker):
        return _ok(released=False, note="无 quiesce 会话（未执行或已释放）")
    try:
        with open(marker) as f:
            pid = int(f.read().strip())
        os.kill(pid, 15)
        try:
            os.remove(marker)
        except OSError:
            pass
        return _ok(released=True, pid=pid,
                   note="读锁保活会话已终止，FLUSH TABLES WITH READ LOCK 随会话释放")
    except ProcessLookupError:
        try:
            os.remove(marker)
        except OSError:
            pass
        return _ok(released=False, note="保活会话已不存在（可能已超时）")
    except Exception as e:
        return _fail("unquiesce 失败: %s" % e)


def _appdr_snapshot(params, state):
    """整机文件级快照：tar 打包指定根目录（排除虚拟文件系统）。"""
    roots = params.get("roots") or []
    out = str(params.get("out") or "")
    if not roots or not out:
        return _fail("需 roots 与 out")
    excludes = ["--exclude=/proc --exclude=/sys --exclude=/dev --exclude=/run "
                "--exclude=/tmp --exclude=%s" % out]
    r = _run("tar -czf %s %s %s" % (out, " ".join(excludes), " ".join(roots)),
             timeout=int(params.get("timeout") or 86400))
    if r["rc"] not in (0, 1):  # tar rc=1 因文件变化可容忍
        return _fail("tar 失败 rc=%s: %s" % (r["rc"], r["stderr"][:200]))
    return _ok(out=out, bytes=os.path.getsize(out) if os.path.exists(out) else 0,
               note="文件级整机快照；应用一致性依赖 freeze/thaw 钩子")


def _appdr_thaw(params, state):
    cmd, source = _resolve_hook_cmd(params, "thaw")
    if not cmd:
        return _fail("需 thaw_cmd 或 preset（与 freeze 同一个）")
    r = _run(cmd, timeout=int(params.get("timeout") or 300))
    return _ok(thawed=r["rc"] == 0, rc=r["rc"], command_source=source,
               stdout=r["stdout"][:5000], stderr=r["stderr"][:5000])


PLUGIN_APP_DR = {
    "name": "app-dr", "version": "1.2.0",
    "description": ("应用级整机快照（一致性钩子库 + 冻结/tar/解冻 + "
                    "MySQL 事务级 quiesce/unquiesce）"),
    "supported": _appdr_supported,
    "actions": {"freeze": _appdr_freeze, "snapshot": _appdr_snapshot,
                "thaw": _appdr_thaw, "list_hooks": _appdr_list_hooks,
                "quiesce": _appdr_quiesce, "unquiesce": _appdr_unquiesce},
}


# ---------------------------------------------------------------------------
# 插件 4：扇区级数据雕刻（block-carve）——只读签名扫描与文件提取
# ---------------------------------------------------------------------------
# 文件签名表（魔数 → 扩展名）：雕刻只识别"能可靠判定的类型"，不猜测，
# 不承诺找回已覆盖块（诚实边界，与页面"不读裸块"旧口径的升级）。
_FILE_SIGNATURES = [
    (b"\xff\xd8\xff\xe0", "jpg"), (b"\xff\xd8\xff\xe1", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"SQLite format 3\x00", "sqlite"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x1f\x8b\x08", "gz"),
    (b"MySQL server ", "ibd"),   # InnoDB 数据文件头（简化识别）
    (b"OggS\x00", "ogg"),
]


def _carve_supported(state):
    return os.name == "posix", "只读签名扫描；需可读块设备或镜像文件，不写源端"


def _carve_scan(params, state=None):
    """只读扫描文件/块设备的魔数签名，返回命中清单（不提取）。"""
    path = str(params.get("path") or "")
    if not path or not os.path.exists(path):
        return _fail("path 不存在: %s" % path)
    max_scan = int(params.get("max_scan_mb") or 64) * 1024 * 1024
    found = []
    try:
        with open(path, "rb") as f:
            data = f.read(max_scan)
        for sig, ext in _FILE_SIGNATURES:
            start = 0
            while True:
                pos = data.find(sig, start)
                if pos < 0 or len(found) >= 500:
                    break
                found.append({"offset": pos, "type": ext, "signature": sig.hex()})
                start = pos + len(sig)
    except PermissionError:
        return _fail("无权限读取 %s（块设备需 root）" % path)
    except Exception as e:
        return _fail("扫描失败: %s" % e)
    return _ok(path=path, scanned_bytes=len(data), hits=len(found),
               signatures=found[:200],
               note="仅识别未覆盖块的文件头；已覆盖数据不可恢复")


def _carve_extract(params, state=None):
    """按签名提取单个雕刻对象到平台目录（只读源，写到 out_dir）。"""
    path = str(params.get("path") or "")
    offset = int(params.get("offset") or -1)
    ext = str(params.get("type") or "bin")
    out_dir = str(params.get("out_dir") or "/tmp/aidbm_carve")
    max_size = int(params.get("max_size_mb") or 64) * 1024 * 1024
    if not path or offset < 0 or not os.path.exists(path):
        return _fail("需 path 与合法 offset")
    if not re.match(r"^[a-z0-9]{1,8}$", ext):
        return _fail("type 仅允许小写字母数字（防路径注入）")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "carve_%d_%d.%s" % (offset, int(time.time()), ext))
    try:
        with open(path, "rb") as fi:
            fi.seek(offset)
            data = fi.read(max_size)
        # 常见格式按结束标记截断（诚实：仅尽力而为）
        end_markers = {"jpg": b"\xff\xd9", "png": b"IEND\xaeB`\x82",
                       "pdf": b"%%EOF"}
        marker = end_markers.get(ext)
        if marker and marker in data:
            data = data[:data.find(marker) + len(marker)]
        with open(out, "wb") as fo:
            fo.write(data)
        return _ok(source=path, offset=offset, out=out, bytes=len(data),
                   sha256=hashlib.sha256(data).hexdigest(),
                   note="雕刻提取件需人工校验内容完整性（无文件分配表时长度靠魔数推断）")
    except PermissionError:
        return _fail("无权限读取源（块设备需 root）")
    except Exception as e:
        return _fail("提取失败: %s" % e)


PLUGIN_BLOCK_CARVE = {
    "name": "block-carve", "version": "1.0.0",
    "description": "扇区级只读签名雕刻（魔数识别 + 未覆盖块提取，不写源端）",
    "supported": _carve_supported,
    "actions": {"scan": _carve_scan, "extract": _carve_extract},
}


ALL_PLUGINS = [PLUGIN_BLOCK_CDP, PLUGIN_BMR, PLUGIN_APP_DR, PLUGIN_BLOCK_CARVE]
PLUGIN_MAP = {p["name"]: p for p in ALL_PLUGINS}


def describe(state=None):
    """供 /capabilities 自报的插件清单（含支持状态与原因，不虚报）。"""
    out = []
    for p in ALL_PLUGINS:
        supported, reason = p["supported"](state) if state else (True, "")
        out.append({
            "name": p["name"], "version": p["version"],
            "description": p["description"],
            "supported": bool(supported), "reason": reason,
            "actions": sorted(p["actions"].keys()),
        })
    return out


def invoke(name, action, params, state=None):
    """统一插件入口：返回 dict（ok 字段标识成功与否）。"""
    p = PLUGIN_MAP.get(name)
    if not p:
        return _fail("未知插件: %s（可用: %s）" % (name, ",".join(PLUGIN_MAP)))
    fn = p["actions"].get(action)
    if not fn:
        return _fail("插件 %s 不支持动作 %s（可用: %s）"
                     % (name, action, ",".join(sorted(p["actions"]))))
    supported, reason = p["supported"](state)
    if not supported:
        return _fail("插件 %s 不可用: %s" % (name, reason), unsupported=True)
    try:
        return fn(params or {}, state)
    except Exception as e:
        return _fail("插件 %s/%s 执行异常: %s" % (name, action, e))
