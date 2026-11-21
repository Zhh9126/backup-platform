import threading
from unittest.mock import Mock
from core.rt_backup.pitr import PITRRestore
import core.operation_lease as op

def test_same_task_pitr_uses_distinct_lease(monkeypatch):
    calls=[]
    class Lease:
        def __enter__(self): calls.append("acquire"); return lambda: calls.append("ensure")
        def __exit__(self,*a): calls.append("release")
    monkeypatch.setattr(op,"operation_lease",lambda *a,**k: Lease())
    svc=PITRRestore.__new__(PITRRestore)
    svc._restore_locked=lambda *a: {"ok":True}
    assert svc.restore(7,dry_run=True)["ok"]
    assert calls==["acquire","ensure","release"]
