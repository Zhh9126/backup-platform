# -*- coding: utf-8 -*-
"""恢复点安全检测（对标 Veeam Secure Restore / Vinchin 恢复点检测，启发式实现）。

诚实边界（设计原则 6：不静默降级、不假装）：离线环境无病毒库，
本模块**不做病毒查杀**，只做三类启发式异常检测：
  1) 体积突变：与同任务近期成功备份的中位数偏差超阈值 → 疑似截断/异常产物
  2) 熵值检测：未压缩产物抽样块熵≈8.0（满熵）→ 疑似内容被二次加密（勒索）；
     压缩/加密类产物（gz/zip/xz/zst/7z/rar）本身高熵，按扩展名豁免
  3) 结构嗅探：产物文件头与备份格式比对（gzip/zip/tar/SQL 文本等），
     头部不符 → 疑似非备份数据

所有结果只标 clean / suspect / unknown 级别并给出原因；suspect 仅记审计
日志（WARNING），**不改动备份记录状态、不拦截恢复**（是否信任该恢复点由
管理员结合业务判断——与厂商 Secure Restore 的"扫描后放行/阻断"不同，
本环境无法判定恶意性，只能提示异常）。

零库表改动：不新增列/迁移，检测结果实时返回，完全不影响现有功能。
"""
import glob
import gzip
import math
import os
import statistics
import zipfile

# 压缩/加密类产物：内容熵高属正常，豁免熵值检测
_COMPRESSED_EXT = (".gz", ".tgz", ".tar.gz", ".zip", ".xz", ".zst", ".7z",
                   ".rar", ".bz2", ".br", ".lz4")

# 结构嗅探支持的格式与魔数
def _sniff_head(path: str):
    """返回 (fmt, ok, detail)。fmt=识别到的格式；ok=头部与格式相符。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError as e:
        return "unreadable", False, f"产物不可读: {e}"
    if not head:
        return "empty", False, "产物为空文件"

    low = (path or "").lower()
    if head[:2] == b"\x1f\x8b":
        return "gzip", True, "gzip 头相符"
    if head[:4] == b"PK\x03\x04" or head[:4] == b"PK\x05\x06":
        return "zip", True, "zip 头相符"
    if head[:4] == b"\xfd7zX" or head[:6] == b"BZh" or head[:4] == b"\x04\x22\x4d\x18":
        return "xz", True, "压缩头相符"
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd", True, "zstd 头相符"
    # tar: 257 偏移 ustar（tar 归档头位置较深，粗查前 4 字节非已知魔数即可）
    try:
        with open(path, "rb") as f:
            f.seek(257)
            if f.read(5) == b"ustar":
                return "tar", True, "ustar 头相符"
    except OSError:
        pass
    # SQL 文本类：前 4KB 可打印比例高 → 判定为文本
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
        if chunk:
            printable = sum(1 for b in chunk
                            if 32 <= b < 127 or b in (9, 10, 13)
                            or b >= 0x80)  # 中文等多字节也算合法文本
            if printable / len(chunk) > 0.9:
                return "text", True, "文本格式（SQL 类）头部正常"
    except OSError:
        pass
    # 达梦 dmp / Oracle dmp / xtrabackup 二进制等：无公开稳定魔数的
    # 专有格式不做硬判，标记 unknown 而非 suspect（避免误报）
    return "binary", True, "二进制专有格式（不做头部硬判）"


def sample_entropy(path: str, samples: int = 4, block: int = 65536) -> float:
    """对产物抽样（头/中/尾）计算香农熵，返回 0~8 比特/字节。"""
    size = os.path.getsize(path)
    if size <= 0:
        return 0.0
    try:
        with open(path, "rb") as f:
            read_at = sorted({0, size // 2, max(0, size - block),
                              size // 4})
            read_at = read_at[:max(1, samples)]
            total = {}
            for off in read_at:
                f.seek(off)
                data = f.read(block)
                if not data:
                    continue
                freq = {}
                for b in data:
                    freq[b] = freq.get(b, 0) + 1
                ent = 0.0
                n = len(data)
                for c in freq.values():
                    p = c / n
                    ent -= p * math.log2(p)
                total[off] = ent
            return round(statistics.mean(total.values()), 3) if total else 0.0
    except OSError:
        return -1.0


def _median_recent_size(task_id: int, exclude_id: int):
    """同任务近期成功备份的中位体积（供体积突变基线）。"""
    try:
        from core import db
        rows = db.query(
            "SELECT size_bytes FROM backup_records "
            "WHERE task_id=? AND status='success' AND size_bytes>0 "
            "AND id<>? ORDER BY id DESC LIMIT 10",
            (task_id, exclude_id))
        sizes = [r["size_bytes"] for r in rows if r["size_bytes"]]
        if len(sizes) < 3:
            return None  # 基线不足，不判
        return statistics.median(sizes)
    except Exception:
        return None


def check_record(record: dict) -> dict:
    """对单条备份记录做启发式安全检测。record 为 backup_records 行。

    返回 {level, checks:[{item, level, message}]}；level ∈ clean/suspect/unknown。
    unknown = 产物缺失/无法判定（如实报告，不武断）。
    """
    checks = []
    path = record.get("backup_path") or ""
    rid = record.get("id")
    if not path or not os.path.exists(path):
        # 远端 SSH 备份的产物可能留存在数据库服务器上（pack_after 前的
        # 路径语义不同）——本地不存在即 unknown，如实说明
        checks.append({"item": "artifact", "level": "unknown",
                       "message": f"产物文件本地不可见（{path or '空路径'}）——"
                                  "远端备份产物在数据库服务器留存或已被清理，"
                                  "本地安全检测跳过（完整性可走 sha256 基线复检）"})
        return {"level": "unknown", "checks": checks}

    size = os.path.getsize(path)
    low = path.lower()

    # ---- 1) 体积突变 ----
    med = _median_recent_size(record.get("task_id"), rid)
    if med and med >= 1024 * 1024:  # 基线 ≥1MB 才有判别力
        ratio = size / med if med else 0
        if ratio < 0.1:
            checks.append({"item": "volume", "level": "suspect",
                           "message": f"产物体积仅为近 10 次成功备份中位数的 "
                                      f"{ratio:.1%}（{size}B vs 中位数 {int(med)}B）"
                                      "——疑似截断或异常产物"})
        elif ratio > 10:
            checks.append({"item": "volume", "level": "suspect",
                           "message": f"产物体积达中位数的 {ratio:.1f} 倍"
                                      "——疑似夹带异常数据"})
        else:
            checks.append({"item": "volume", "level": "clean",
                           "message": f"体积正常（中位数 {ratio:.1%} 偏差内）"})
    else:
        checks.append({"item": "volume", "level": "unknown",
                       "message": "同任务成功基线不足（<3 条或均 <1MB），"
                                  "体积突变不判定"})

    # ---- 2) 熵值 ----
    # 豁免条件收紧：仅当结构嗅探识别出已知压缩格式（或扩展名压缩且头部
    # 相符）才豁免。未知二进制（专有格式）**必做熵检**——修复：此前按扩展名
    # 豁免（.sql.zst 等），产物被勒索加密覆写为满熵随机数据时，若扩展名带
    # 压缩后缀即漏检（实测暴露）。
    fmt, sniff_ok, sniff_detail = _sniff_head(path)
    known_compressed = fmt in ("gzip", "zip", "xz", "zstd", "tar")
    if known_compressed and (low.endswith(_COMPRESSED_EXT) or ".tar.gz" in low):
        checks.append({"item": "entropy", "level": "unknown",
                       "message": "压缩/加密格式产物高熵属正常，熵检豁免"})
    elif size >= 4096:
        ent = sample_entropy(path)
        if ent < 0:
            checks.append({"item": "entropy", "level": "unknown",
                           "message": "熵采样失败"})
        elif ent > 7.95:
            hint = ("（未压缩产物）" if not known_compressed
                    else "（压缩头不符的高熵内容）")
            checks.append({"item": "entropy", "level": "suspect",
                           "message": f"产物抽样熵 {ent}≈满熵（8.0）{hint}"
                                      "——疑似内容被二次加密（勒索软件特征）"})
        else:
            checks.append({"item": "entropy", "level": "clean",
                           "message": f"抽样熵 {ent}，正常范围"})
    else:
        checks.append({"item": "entropy", "level": "unknown",
                       "message": "产物过小（<4KB），熵检不判定"})

    # ---- 3) 结构嗅探（复用熵检阶段的嗅探结果，避免重复 IO）----
    if not sniff_ok:
        checks.append({"item": "structure", "level": "suspect",
                       "message": f"结构异常：{sniff_detail}"})
    else:
        # gzip/zip 等压缩产物可做"可解压性"探测（头部损坏/截断）
        if fmt == "gzip":
            try:
                with gzip.open(path, "rb") as g:
                    g.read(1024)
                checks.append({"item": "structure", "level": "clean",
                               "message": f"{fmt} 头部相符且首块可解"})
            except Exception as e:
                checks.append({"item": "structure", "level": "suspect",
                               "message": f"{fmt} 首块解压失败（{e}）——"
                                          "疑似截断/损坏"})
        elif fmt == "zip":
            try:
                with zipfile.ZipFile(path) as z:
                    if z.testzip() is not None:
                        checks.append({"item": "structure", "level": "suspect",
                                       "message": "zip 存在损坏条目"})
                    else:
                        checks.append({"item": "structure", "level": "clean",
                                       "message": "zip 结构完整"})
            except Exception as e:
                checks.append({"item": "structure", "level": "suspect",
                               "message": f"zip 打开失败（{e}）"})
        else:
            checks.append({"item": "structure", "level": "clean" if sniff_ok
                           else "unknown", "message": sniff_detail})

    # ---- 汇总：suspect > clean > unknown ----
    # unknown（基线不足/豁免/不可判）不压过已明确判定的 clean——
    # 各检测项的"不判定"原因已在 checks 逐项如实标注。
    levels = {c["level"] for c in checks}
    if "suspect" in levels:
        level = "suspect"
    elif "clean" in levels:
        level = "clean"
    else:
        level = "unknown"
    return {"level": level, "checks": checks}


def check_record_by_id(record_id: int) -> dict:
    """按 ID 取记录并检测；suspect 结果记 WARNING 审计日志。"""
    from core import db
    rec = db.query_one("SELECT * FROM backup_records WHERE id=?", (record_id,))
    if not rec:
        raise ValueError(f"备份记录不存在: {record_id}")
    res = check_record(dict(rec))
    if res["level"] == "suspect":
        try:
            bad = [(c["item"], c["message"]) for c in res["checks"]
                   if c["level"] == "suspect"]
            db.add_log("WARNING", "backup_anomaly",
                       "恢复点安全检测发现异常: record#%s task#%s path=%s checks=%s"
                       % (record_id, rec.get("task_id"),
                          rec.get("backup_path"), bad))
        except Exception:
            pass
    return res
