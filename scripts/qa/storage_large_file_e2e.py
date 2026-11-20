"""Exercise streaming beyond the former 512 MiB limit, recording peak resident memory."""
import json
from pathlib import Path
import resource
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import chunk_store as cs, container_store as ct
from core.db import sha256_file

def main():
    base = Path(__file__).resolve().parents[2] / "build" / "storage-qa"
    base.mkdir(exist_ok=True, parents=True)
    results = {}
    with tempfile.TemporaryDirectory(prefix="large-", dir=base) as directory:
        root = Path(directory)
        source = root / "source"
        with source.open("wb") as stream:
            stream.truncate(513 * 1024 * 1024)
        opts = dict(min_size=4*1024*1024, avg_size=4*1024*1024, max_size=4*1024*1024)
        digest = sha256_file(str(source))
        for name, backend in (("blobs", cs), ("containers", ct)):
            store = str(root / name)
            first = backend.store_file(str(source), store, chunk_opts=opts)
            second = backend.store_file(str(source), store, chunk_opts=opts)
            assert second["new_blocks"] == 0
            out = root / "restore"
            restored = backend.restore_file(second["manifest_id"], str(out), store)
            assert restored["ok"] and sha256_file(str(out)) == digest
            results[name] = {
                "source_bytes": source.stat().st_size, "chunks": first["chunks"],
                "first_new_blocks": first["new_blocks"], "second_new_blocks": second["new_blocks"],
                "second_reused_blocks": second["reused_blocks"], "restore_sha256": digest,
                "physical_bytes": backend.stats(store)["physical_bytes"],
            }
            out.unlink()
    results["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    results["note"] = "Sparse zero-filled input; validates size and memory behavior, not representative production dedup ratio."
    (base / "large-e2e.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
