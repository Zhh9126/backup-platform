"""Loopback real SFTP transfer with injected transport failures. All files stay in build/."""
import errno
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import paramiko
from core import remote_dump as rd

class FileProxy:
    def __init__(self, file, owner, mode):
        self.file, self.owner, self.mode = file, owner, mode
    def __getattr__(self, key):
        return getattr(self.file, key)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.file.close()
    def read(self, size=-1):
        self.owner.reads += 1
        if self.owner.fail_read and self.owner.reads == 2:
            self.owner.fail_read = False
            raise EOFError("injected SFTP read disconnect")
        return self.file.read(size)
    def write(self, data):
        self.owner.writes += 1
        if self.owner.fail_write and self.owner.writes == 2:
            self.owner.fail_write = False
            raise EOFError("injected SFTP write disconnect")
        return self.file.write(data)

class SFTPProxy:
    def __init__(self, sftp, owner):
        self.sftp, self.owner = sftp, owner
    def __getattr__(self, key):
        return getattr(self.sftp, key)
    def open(self, path, mode="r", *args, **kwargs):
        file = self.sftp.open(path, mode, *args, **kwargs)
        if "b" in mode:
            return FileProxy(file, self.owner, mode)
        return file
    def posix_rename(self, old, new):
        if self.owner.fail_publish:
            raise PermissionError(errno.EACCES, "injected denied rename")
        return self.sftp.posix_rename(old, new)

class ClientProxy:
    def __init__(self, client):
        self.client = client
        self.fail_read = self.fail_write = self.fail_publish = False
        self.reads = self.writes = self.opens = 0
    def __getattr__(self, key):
        return getattr(self.client, key)
    def open_sftp(self):
        self.opens += 1
        return SFTPProxy(self.client.open_sftp(), self)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def main():
    base = Path(__file__).resolve().parents[2] / "build" / "storage-qa"
    base.mkdir(parents=True, exist_ok=True)
    results = {}
    with tempfile.TemporaryDirectory(prefix="sftp-e2e-", dir=base) as directory:
        root = Path(directory)
        source = root / "source"
        source.write_bytes(random.Random(31).randbytes(9 * 1024 * 1024))
        real = paramiko.SSHClient()
        real.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        real.connect("127.0.0.1", username="root", password=os.environ["AIDBM_QA_SSH_PASSWORD"],
                     look_for_keys=False, allow_agent=False, timeout=15)
        client = ClientProxy(real)
        try:
            with patch.object(rd, "_retry_wait", lambda *args: None):
                task = {"id": 89001, "extra_options": {"retry_max": 2, "retry_interval": 0}}
                out = root / "pull"
                client.fail_read = True
                pull = rd.sftp_pull_resumable(client, str(source), str(out), task=task,
                                               host_key="", stable_secs=0.01)
                assert pull["resumed"] and client.opens == 2 and sha(out) == sha(source)
                results["pull_read_disconnect_resume"] = True
                part = Path(rd._resume_part_path(str(out), task, "fixed", key=""))
                part.write_bytes(b"WRONG" * 10000)
                rd._meta_write(str(part), {"task_id": task["id"], "host": "", "remote_path": str(source),
                                           "kind": "fixed", "started_at": __import__("time").time(), "offset": part.stat().st_size})
                pull = rd.sftp_pull_resumable(client, str(source), str(out), task=task, stable_secs=0.01)
                assert not pull["resumed"] and sha(out) == sha(source)
                results["pull_wrong_prefix_restart"] = True
                target = root / "push"
                target.write_bytes(b"keep old destination")
                remote_part = Path(str(target) + ".aidbm_part")
                remote_part.write_bytes(b"WRONG" * 10000)
                push = rd.sftp_put_resumable(client, str(source), str(target), retry_interval=0)
                assert not push["resumed"] and sha(target) == sha(source)
                results["push_wrong_prefix_restart"] = True
                remote_part.write_bytes(source.read_bytes()[:128 * 1024])
                client.writes = 0
                client.fail_write = True
                push = rd.sftp_put_resumable(client, str(source), str(target), retry_interval=0)
                assert push["resumed"] and sha(target) == sha(source)
                results["push_write_disconnect_resume"] = True
                target.write_bytes(b"old destination retained")
                client.fail_publish = True
                try:
                    rd.sftp_put_resumable(client, str(source), str(target), retry_interval=0)
                    raise AssertionError("publish should fail")
                except PermissionError:
                    assert target.read_bytes() == b"old destination retained"
                results["publish_failure_preserves_target"] = True
                results["source_bytes"] = source.stat().st_size
                results["source_sha256"] = sha(source)
        finally:
            real.close()
    (base / "sftp-e2e.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
