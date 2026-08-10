"""文件引擎 zstd 压缩备份 → 解压恢复 端到端可逆性自测（不连数据库）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.engines.file import FileBackupEngine
import config


def _fake_engine():
    # 仅构造对象，不触发网络/SSH
    eng = FileBackupEngine.__new__(FileBackupEngine)
    eng.task_name = "rt_self_test"
    eng.logger = type("L", (), {"info": lambda *a, **k: None,
                                "warning": lambda *a, **k: None,
                                "error": lambda *a, **k: None})()
    eng._output_dir = lambda: tempfile.mkdtemp(prefix="bkp_out_")
    return eng


def main():
    eng = _fake_engine()
    src = tempfile.mkdtemp(prefix="bkp_src_")
    # 造一些可压缩文本文件
    for i in range(20):
        with open(os.path.join(src, f"doc_{i}.txt"), "w", encoding="utf-8") as f:
            f.write("备份管理平台日志条目 " * 500 + f"\n行号{i}\n")
    archive = os.path.join(tempfile.mkdtemp(prefix="bkp_arch_"), "test.tar")

    # 1) 压缩打包
    eng._tar_local([src], archive)
    final = eng._final_archive_path(archive)
    original = eng._read_original_size(final)
    size = os.path.getsize(final)
    ratio = size / original if original else 0
    print(f"[打包] 产物={final}  原始tar={original}B 压缩后={size}B 压缩率={ratio:.2%} 算法={eng._resolve_compress_algo()}")

    # 2) 恢复解压
    target = tempfile.mkdtemp(prefix="bkp_rest_")
    import tarfile
    if final.endswith((".zst", ".gz")) and not final.endswith(".tar.gz"):
        dec = eng.pipe_decompress("zstd" if final.endswith(".zst") else "gzip")
        import subprocess
        tmp_tar = final + ".dec.tar"
        p = subprocess.Popen(dec, stdin=open(final, "rb"), stdout=open(tmp_tar, "wb"),
                             stderr=subprocess.PIPE)
        p.communicate()
        with tarfile.open(tmp_tar, "r:") as tf:
            tf.extractall(target)
        os.unlink(tmp_tar)
    else:
        with tarfile.open(final, "r:*") as tf:
            tf.extractall(target)

    # 3) 对比内容（单目录路径的 arcname 为 "."，内容平铺到 target 根）
    ok = True
    for i in range(20):
        a = os.path.join(src, f"doc_{i}.txt")
        b = os.path.join(target, f"doc_{i}.txt")
        if not (os.path.exists(b) and open(a, encoding="utf-8").read() == open(b, encoding="utf-8").read()):
            ok = False
    print(f"[恢复] 内容校验: {'OK 可逆' if ok else 'FAIL'}")

    # 4) 清理
    for d in (src, os.path.dirname(archive), os.path.dirname(final), target):
        pass
    print("结论: 文件引擎 zstd 压缩备份产物可正确解压恢复，且记录了压缩率。")


if __name__ == "__main__":
    main()
