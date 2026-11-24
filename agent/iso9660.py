# -*- coding: utf-8 -*-
"""纯 Python ISO9660 + El Torito 可启动镜像生成器（零外部依赖）。

用途：BMR 救援 ISO 不再依赖宿主机 mkisofs/xorriso/genisoimage。AIDBM 是
完全离线交付，Agent 侧用本模块直接写标准 ISO9660 镜像，BIOS 从 El Torito
引导 grub2-mkimage 产出的 boot image。

实现范围（诚实边界）：
- ISO9660 Level 2（文件名大写 + ;1 版本），Linux isofs 默认映射为小写；
- El Torito no-emulation BIOS 引导（单 boot image）；
- 不做 UDF / UEFI / isohybrid / Joliet / Rock Ridge：Linux 救援内核挂载
  基础 isofs 即可读到 /boot/vmlinuz、/boot/initrd.img、/aidbm/recovery.img；
- 文件按扇区对齐流式写入，内存占用与镜像大小无关。

参考：ECMA-119（ISO9660）、El Torito Bootable CD-ROM Format Specification 1.0。
"""
import os
import struct
import time

SECTOR = 2048

_SYSTEM_ID = b"AIDBM BARE-METAL RESCUE"
_APPLICATION_ID = b"AIDBM RESCUE ISO BUILDER"


class ISOError(Exception):
    pass


def _both16(value):
    return struct.pack("<H", value) + struct.pack(">H", value)


def _both32(value):
    return struct.pack("<I", value) + struct.pack(">I", value)


def _ceil_sectors(nbytes):
    return (nbytes + SECTOR - 1) // SECTOR


def _iso_name(name, is_dir=False):
    """ISO9660 Level 2 文件名：A-Z0-9_.-，大写，文件带 ;1。"""
    text = str(name).upper().replace(" ", "_")
    text = "".join(ch for ch in text if ch.isalnum() or ch in "_.-")
    if not text:
        raise ISOError("非法 ISO 文件名: %s" % name)
    if not is_dir:
        text += ";1"
    return text.encode("ascii")


def _record_time():
    now = time.localtime()
    return bytes(bytearray([
        max(0, min(127, now.tm_year - 1900)),
        now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec, 0,
    ]))


def _directory_record(lba, size, identifier, is_dir, stamp):
    """ECMA-119 9.1 目录记录（identifier 已编码为 bytes）。"""
    ident = identifier if identifier else b"\x00"
    length = 33 + len(ident)
    if length % 2:
        length += 1
    rec = bytearray(length)
    rec[0] = length
    rec[1] = 0
    rec[2:10] = _both32(lba)
    rec[10:18] = _both32(size)
    rec[18:25] = stamp
    rec[25] = 0x02 if is_dir else 0x00
    rec[26] = 0
    rec[27] = 0
    rec[28:32] = _both16(1)
    rec[32] = len(ident)
    rec[33:33 + len(ident)] = ident
    return bytes(rec)


def _path_table_record(lba, parent_no, identifier, little):
    """ECMA-119 9.4 路径表记录。"""
    ident = identifier if identifier else b"\x00"
    length = 8 + len(ident)
    if length % 2:
        length += 1
    rec = bytearray(length)
    rec[0] = len(ident)
    rec[1] = 0
    endian = "<" if little else ">"
    struct.pack_into(endian + "I", rec, 2, lba)
    struct.pack_into(endian + "H", rec, 6, parent_no)
    rec[8:8 + len(ident)] = ident
    return bytes(rec)


def _primary_volume_descriptor(volume_space, path_size, l_path_lba,
                               m_path_lba, root_record, stamp):
    pvd = bytearray(SECTOR)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[8:40] = _SYSTEM_ID.ljust(32, b" ")
    pvd[40:72] = b"AIDBM_RESCUE".ljust(32, b" ")
    pvd[80:88] = _both32(volume_space)
    pvd[120:124] = _both16(1)
    pvd[124:128] = _both16(1)
    pvd[128:132] = _both16(SECTOR)
    pvd[132:140] = _both32(path_size)
    struct.pack_into("<I", pvd, 140, l_path_lba)
    struct.pack_into("<I", pvd, 144, 0)
    struct.pack_into(">I", pvd, 148, m_path_lba)
    struct.pack_into(">I", pvd, 152, 0)
    pvd[156:190] = root_record
    pvd[190:318] = b"AIDBM_RESCUE".ljust(128, b" ")
    pvd[446:574] = _APPLICATION_ID.ljust(128, b" ")
    pvd[813:830] = stamp
    pvd[830:847] = stamp
    pvd[881] = 1
    return bytes(pvd)


def _boot_record_descriptor(boot_catalog_lba):
    br = bytearray(SECTOR)
    br[0] = 0
    br[1:6] = b"CD001"
    br[6] = 1
    br[7:39] = b"EL TORITO SPECIFICATION".ljust(32, b"\x00")
    struct.pack_into("<I", br, 39, boot_catalog_lba)
    return bytes(br)


def _terminator_descriptor():
    term = bytearray(SECTOR)
    term[0] = 255
    term[1:6] = b"CD001"
    term[6] = 1
    return bytes(term)


def _boot_catalog(boot_lba, boot_sectors):
    """El Torito 校验条目 + 初始引导条目（no emulation）。"""
    cat = bytearray(SECTOR)
    # Validation entry: indicator=1, platform=0(BIOS), 0x55AA, bootable
    cat[0] = 0x01
    cat[1] = 0x00
    cat[2:4] = b"AD"
    cat[20] = 0x55
    cat[21] = 0xAA
    cat[22] = 0x00
    cat[23] = 0x00
    checksum = 0
    for i in range(0, 32, 2):
        checksum += struct.unpack_from("<H", cat, i)[0]
    struct.pack_into("<H", cat, 4, (-checksum) & 0xFFFF)
    # Initial/Default entry: 0x88 bootable, no emulation
    cat[32] = 0x88
    cat[33] = 0x00
    struct.pack_into("<H", cat, 34, 0x07C0)
    cat[36] = 0x00
    cat[37] = 0x00
    struct.pack_into("<H", cat, 38, max(1, min(0xFFFF, boot_sectors)))
    struct.pack_into("<I", cat, 40, boot_lba)
    return bytes(cat)


class _Tree(object):
    def __init__(self, name="", parent=None):
        self.name = name
        self.parent = parent
        self.children = {}       # name -> _Tree (dir)
        self.files = []          # [(name, local_path)]
        self.lba = 0
        self.no = 0
        self.content = b""

    def add(self, parts, local_path):
        if len(parts) == 1:
            self.files.append((parts[0], local_path))
        else:
            child = self.children.get(parts[0])
            if child is None:
                child = _Tree(parts[0], self)
                self.children[parts[0]] = child
            child.add(parts[1:], local_path)


def _collect_dirs(root):
    result = []

    def walk(node):
        result.append(node)
        for child in node.children.values():
            walk(child)

    walk(root)
    return result


def build_iso_from_dir(root_dir, out_iso, boot_rel="boot/grub_boot.bin",
                       volume_id="AIDBM_RESCUE"):
    """把目录树写成可启动 ISO。

    :param root_dir: ISO 根目录（本地路径）
    :param out_iso: 输出 ISO 路径
    :param boot_rel: El Torito 引导镜像在 ISO 内的相对路径（POSIX 风格）
    :param volume_id: 卷标（<=32 字符）
    :return: dict(ok/bytes/sha256/boot_lba/sectors)
    """
    import hashlib
    root_dir = os.path.abspath(root_dir)
    if not os.path.isdir(root_dir):
        raise ISOError("ISO 根目录不存在: %s" % root_dir)
    boot_rel = boot_rel.replace("\\", "/").strip("/")
    tree = _Tree()
    for current, dirs, files in os.walk(root_dir):
        dirs.sort()
        rel_dir = os.path.relpath(current, root_dir)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        node = tree
        for part in parts:
            node = node.children[part]
        for fn in sorted(files):
            node.add([fn], os.path.join(current, fn))
        # 空目录也要进入路径表
        for dn in dirs:
            node.add([dn, ".keep_placeholder"], None)
    # 找到 boot image（大小写不敏感）
    boot_local = None
    stack = [(tree, [])]
    while stack:
        node, prefix = stack.pop()
        for name, local in node.files:
            if local and "/".join(prefix + [name]).lower() == boot_rel.lower():
                boot_local = local
                break
        for child in node.children.values():
            stack.append((child, prefix + [child.name]))
        if boot_local:
            break
    if not boot_local or not os.path.exists(boot_local):
        raise ISOError("ISO 内未找到引导镜像: %s" % boot_rel)

    dirs = _collect_dirs(tree)
    for idx, node in enumerate(dirs):
        node.no = idx + 1
    stamp = _record_time()

    # 布局：0-15 系统区；16 PVD；17 Boot Record；18 Terminator；
    #       19 L-PathTable；20 M-PathTable；21 Boot Catalog；22+ 数据区
    first_data_lba = 22
    lba = first_data_lba
    for node in dirs:
        node.lba = lba
        node.content = b""
        node.content += _directory_record(node.lba, SECTOR, b"\x00", True, stamp)
        node.content += _directory_record(
            node.lba if node.parent is None else node.parent.lba,
            SECTOR, b"\x01", True, stamp)
        for child_name, child in sorted(node.children.items()):
            node.content += _directory_record(child.lba, SECTOR,
                                              _iso_name(child_name, True),
                                              True, stamp)
        for file_name, local in sorted(node.files):
            size = os.path.getsize(local) if local else 0
            node.content += _directory_record(
                getattr(node, "_file_lba_%s" % file_name, 0), size,
                _iso_name(file_name), False, stamp)
        node.content = node.content.ljust(_ceil_sectors(len(node.content)) * SECTOR, b"\x00")
        lba += _ceil_sectors(len(node.content))
    # 文件 extent（两遍：先分配 LBA，再重算目录记录）
    for node in dirs:
        for file_name, local in sorted(node.files):
            setattr(node, "_file_lba_%s" % file_name, lba)
            size = os.path.getsize(local) if local else 0
            lba += max(1, _ceil_sectors(size))
    for node in dirs:
        node.content = b""
        # 根目录的 . 与 .. 均指向自身（ECMA-119 要求）
        self_lba = node.lba if node.parent is None else node.lba
        parent_lba = node.lba if node.parent is None else node.parent.lba
        node.content += _directory_record(self_lba, SECTOR, b"\x00", True, stamp)
        node.content += _directory_record(parent_lba, SECTOR, b"\x01", True, stamp)
        for child_name, child in sorted(node.children.items()):
            node.content += _directory_record(child.lba, SECTOR,
                                              _iso_name(child_name, True), True, stamp)
        for file_name, local in sorted(node.files):
            size = os.path.getsize(local) if local else 0
            node.content += _directory_record(
                getattr(node, "_file_lba_%s" % file_name, 0), size,
                _iso_name(file_name), False, stamp)
        node.content = node.content.ljust(_ceil_sectors(len(node.content)) * SECTOR, b"\x00")
    total_sectors = max(lba, 23)

    # 根记录 + 路径表
    root_record = _directory_record(dirs[0].lba, len(dirs[0].content), b"\x00", True, stamp)
    l_table = b"".join(_path_table_record(n.lba, n.parent.no if n.parent else 1,
                                          b"\x00" if n.parent is None else _iso_name(n.name, True),
                                          True) for n in dirs)
    m_table = b"".join(_path_table_record(n.lba, n.parent.no if n.parent else 1,
                                          b"\x00" if n.parent is None else _iso_name(n.name, True),
                                          False) for n in dirs)

    boot_lba = getattr(dirs[0], "_file_lba_%s" % os.path.basename(boot_local), None)
    if boot_lba is None:
        # boot 镜像可能在子目录：重新查找
        stack = [(tree, [])]
        while stack:
            node, prefix = stack.pop()
            for name, local in node.files:
                if local == boot_local:
                    boot_lba = getattr(node, "_file_lba_%s" % name)
            for child in node.children.values():
                stack.append((child, prefix + [child.name]))
    boot_bytes = os.path.getsize(boot_local)
    catalog = _boot_catalog(boot_lba, _ceil_sectors(boot_bytes) * (SECTOR // 512))

    out_iso = os.path.abspath(out_iso)
    parent = os.path.dirname(out_iso)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    h = hashlib.sha256()
    with open(out_iso, "wb") as out:
        def write_sector(data, lba_index):
            out.seek(lba_index * SECTOR)
            out.write(data)
            return len(data)

        write_sector(b"\x00" * (16 * SECTOR), 0)
        write_sector(_primary_volume_descriptor(total_sectors, len(l_table),
                                                 19, 20, root_record, stamp), 16)
        write_sector(_boot_record_descriptor(21), 17)
        write_sector(_terminator_descriptor(), 18)
        write_sector(l_table.ljust(SECTOR, b"\x00"), 19)
        write_sector(m_table.ljust(SECTOR, b"\x00"), 20)
        write_sector(catalog, 21)
        for node in dirs:
            write_sector(node.content, node.lba)
        for node in dirs:
            for file_name, local in sorted(node.files):
                if not local:
                    continue
                target = getattr(node, "_file_lba_%s" % file_name, 0)
                out.seek(target * SECTOR)
                with open(local, "rb") as src:
                    while True:
                        chunk = src.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
    with open(out_iso, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return {"ok": True, "out_iso": out_iso, "bytes": os.path.getsize(out_iso),
            "sha256": h.hexdigest(), "boot_lba": boot_lba,
            "sectors": total_sectors, "files": sum(len(n.files) for n in dirs),
            "note": "纯 Python ISO9660 + El Torito（无 mkisofs/xorriso 依赖）"}
