# -*- coding: utf-8 -*-
"""Citrix Hypervisor / XenServer Provider —— REST 控制面接入。

 XenAPI JSON-RPC 通常是 POST /jsonrpc，但资产列表也可用 host API 的 REST
 包装；本 provider 走与 rest_api 一致的 GET 列表（可通过 api_base 指向
 现场网关）。数据面（VDI 导出）无真机环境，明确不实现。
"""
from core.vm.providers.rest_api import RestAPIVMProvider
from core.vm.types import VMInfo


class XenServerProvider(RestAPIVMProvider):
    provider_id = "xenserver"
    display_name = "Citrix Hypervisor / XenServer"
    list_path = "/rest/vms"

    def _extract_items(self, data):
        if isinstance(data, dict) and isinstance(data.get("vms"), list):
            return data["vms"]
        return super()._extract_items(data)

    def _map_vm(self, row):
        return VMInfo(
            ref=str(row.get("uuid") or row.get("id") or ""),
            name=str(row.get("name_label") or row.get("name") or ""),
            power_state=str(row.get("power_state") or "unknown"),
            raw=row)
