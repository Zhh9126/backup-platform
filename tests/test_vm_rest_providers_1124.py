# -*- coding: utf-8 -*-
"""oVirt/OpenStack/XenServer provider 注册与诚实边界契约。"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEBUDDY_SAFE_DELETE_ENABLED", "0")

from core.vm.providers import PROVIDER_IDS, get_provider_class  # noqa: E402
from core.vm.types import VMProviderError  # noqa: E402


def test_providers_registered():
    for pid in ("ovirt", "openstack", "xenserver"):
        assert pid in PROVIDER_IDS
        assert get_provider_class(pid) is not None


def test_capabilities_honest_no_incremental():
    for pid in ("ovirt", "openstack", "xenserver"):
        cls = get_provider_class(pid)
        caps = cls({"host": "127.0.0.1", "port": 443}).capabilities()
        assert caps.incremental is False
        assert caps.snapshot is False
        assert "仅接入控制面" in caps.notes


def test_data_plane_rejects():
    for pid in ("ovirt", "openstack", "xenserver"):
        cls = get_provider_class(pid)
        p = cls({"host": "127.0.0.1", "port": 443})
        for method, args in (("backup_full", ("vm-1",)),
                             ("backup_incremental", ("vm-1",)),
                             ("restore_in_place", (None,))):
            try:
                getattr(p, method)(*args)
                assert False, "%s.%s 未拒绝" % (pid, method)
            except VMProviderError:
                pass


def test_list_vms_parses_api_response():
    cls = get_provider_class("openstack")
    p = cls({"host": "127.0.0.1", "port": 443, "username": "u", "password": "p"})
    payload = {"servers": [
        {"id": "vm-1", "name": "web-01", "status": "ACTIVE",
         "flavor": {"vcpus": 2, "ram": 4096}},
        {"id": "vm-2", "name": "db-01", "status": "SHUTOFF",
         "flavor": {"vcpus": 4, "ram": 8192}},
    ]}
    with mock.patch.object(p, "_request", return_value=payload):
        vms = p.list_vms()
    assert len(vms) == 2
    assert vms[0].ref == "vm-1" and vms[0].cpu == 2 and vms[0].memory_mb == 4096
    assert vms[1].power_state == "SHUTOFF"
