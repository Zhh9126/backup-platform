"""Reject invalid VM backup chains before upload/restore and preserve recovery evidence."""
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from core.vm.engine import VMBackupEngine
from core.vm.types import VMProviderError

def point(path, rid=1):
    return {"id":rid,"artifact_path":str(path),"remote_path":"/old/artifact.tar",
            "size_bytes":path.stat().st_size,"checksum":hashlib.sha256(path.read_bytes()).hexdigest()}

def test_tampered_later_segment_prevents_all_uploads(tmp_path):
    first,second=tmp_path/"full",tmp_path/"inc"
    first.write_bytes(b"full");second.write_bytes(b"valid")
    chain=[point(first),point(second,2)]
    second.write_bytes(b"wrong")
    provider=SimpleNamespace(upload_artifact=Mock(),extra={})
    engine=VMBackupEngine.__new__(VMBackupEngine)
    with pytest.raises(VMProviderError,match="SHA256"):
        engine._ensure_remote(provider,chain)
    provider.upload_artifact.assert_not_called()

def test_missing_local_copy_does_not_trust_stale_remote(tmp_path):
    provider=SimpleNamespace(upload_artifact=Mock(),extra={})
    engine=VMBackupEngine.__new__(VMBackupEngine)
    with pytest.raises(VMProviderError,match="本地产物缺失"):
        engine._ensure_remote(provider,[{"id":1,"remote_path":"/old/possibly-deleted.img"}])
    provider.upload_artifact.assert_not_called()

def test_empty_and_size_mismatch_are_rejected(tmp_path):
    artifact=tmp_path/"full"
    artifact.touch()
    with pytest.raises(VMProviderError,match="为空"):
        VMBackupEngine._validate_local_artifact({"artifact_path":str(artifact)})
    artifact.write_bytes(b"abc")
    with pytest.raises(VMProviderError,match="大小"):
        VMBackupEngine._validate_local_artifact({"artifact_path":str(artifact),"size_bytes":5})

def test_partial_upload_failure_cleans_only_pushed_artifacts(tmp_path):
    first,second=tmp_path/"full",tmp_path/"inc"
    first.write_bytes(b"full");second.write_bytes(b"incremental")
    provider=SimpleNamespace(upload_artifact=Mock(side_effect=["/tmp/push-full",OSError("disconnected")]),
                             cleanup_artifact=Mock(),extra={})
    engine=VMBackupEngine.__new__(VMBackupEngine)
    with pytest.raises(OSError):
        engine._ensure_remote(provider,[point(first),point(second,2)])
    assert provider.cleanup_artifact.call_count==1
    assert provider.cleanup_artifact.call_args.args[0].path=="/tmp/push-full"
    assert first.exists() and second.exists()
