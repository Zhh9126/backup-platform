# -*- coding: utf-8 -*-
"""REST API 型虚拟化平台通用适配器（oVirt/RHEV、OpenStack 系、XenServer）。

诚实边界（重要）：
- 这些平台没有现成真机验证环境，因此**只落地控制面接入**（连接探活 +
  资产发现），数据面备份/恢复一律抛 VMProviderError「当前 provider 仅支持
  资产发现，备份请选用已验证的 PVE/libvirt/ESXi/Hyper-V」——绝不伪造成功；
- 控制面用平台自带的 urllib（离线零依赖），token/密码从 hypervisor 行读取
  （已由引擎解密），不落日志；
- 未来接入数据面时，在本基类上覆盖 backup_full/restore_in_place 即可，
  不需改注册表与 UI。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from core.vm.base import VMProvider
from core.vm.types import ProviderCaps, VMInfo, VMProviderError


class RestAPIVMProvider(VMProvider):
    """通用 REST 控制面 Provider 基类。子类只需定义端点和字段映射。"""

    api_base = ""            # 如 https://host/ovirt-engine/api
    list_path = ""           # 资产列表端点（相对 api_base）
    auth_header = "Authorization"
    auth_prefix = "Bearer "  # token 前缀；Basic 由子类覆写 _auth_value

    def _api_url(self, path):
        base = (self.extra.get("api_base") or self.api_base or "").rstrip("/")
        if not base:
            host = self.hv.get("host") or self.hv.get("hostname") or ""
            port = self.hv.get("port") or 443
            if not host:
                raise VMProviderError("未配置 API 地址（api_base 或 host）")
            base = "https://%s:%s" % (host, port)
        return base + "/" + str(path).lstrip("/")

    def _auth_value(self):
        token = self.hv.get("token_secret") or self.hv.get("token") or ""
        if token:
            return self.auth_prefix + token
        import base64
        user = self.hv.get("username") or ""
        pw = self.hv.get("password") or ""
        return "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()

    def _request(self, path, timeout=20):
        req = urllib.request.Request(
            self._api_url(path),
            headers={self.auth_header: self._auth_value(),
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise VMProviderError("API %s 返回 HTTP %s" % (path, e.code))
        except Exception as e:
            raise VMProviderError("API %s 请求失败: %s" % (path, str(e)[:200]))
        try:
            return json.loads(raw)
        except ValueError:
            raise VMProviderError("API %s 返回非 JSON（可能未启用 API 或认证失败）" % path)

    def connect(self):
        try:
            data = self._request(self.list_path, timeout=15)
        except VMProviderError as e:
            return False, "%s 连接失败: %s" % (self.display_name, e)
        return True, "%s 连接正常（API 可达，返回 %s 条资产）" % (
            self.display_name, len(self._extract_items(data)))

    def version(self):
        try:
            return str(self.extra.get("api_version") or "REST API（未探测版本）")
        except Exception:
            return ""

    def _extract_items(self, data):
        """子类可覆写：从 API 响应中取资产数组。"""
        if isinstance(data, list):
            return data
        for key in ("vm", "vms", "servers", "items", "results", "value"):
            if isinstance(data, dict) and isinstance(data.get(key), list):
                return data[key]
        return []

    def list_vms(self):
        data = self._request(self.list_path)
        out = []
        for row in self._extract_items(data):
            try:
                out.append(self._map_vm(row))
            except Exception:
                continue
        return out

    def _map_vm(self, row):
        """子类覆写：API 对象 -> VMInfo。"""
        return VMInfo(ref=str(row.get("id") or row.get("uuid") or ""),
                      name=str(row.get("name") or ""),
                      power_state=str(row.get("status") or "unknown"),
                      raw=row if isinstance(row, dict) else {})

    def get_vm(self, ref):
        for vm in self.list_vms():
            if vm.ref == ref or vm.name == ref:
                return vm
        raise VMProviderError("未找到虚拟机: %s" % ref)

    def capabilities(self):
        return ProviderCaps(
            incremental=False, change_tracking=False, instant_restore=False,
            clone_to_new_vm=False, file_restore=False, snapshot=False,
            consistency_levels=["crash"],
            notes=(self.display_name +
                   "：当前仅接入控制面（连接探活 + 资产发现）；"
                   "数据面备份/恢复未在本环境真机验证，明确不支持，"
                   "请选择已验证的 PVE/libvirt/ESXi/Hyper-V provider。"))

    # 数据面全部明确拒绝（不静默降级、不造假）
    def _not_implemented(self, action):
        raise VMProviderError(
            "%s provider 当前仅支持连接探活与资产发现，%s 未实现"
            "（无真机验证环境，不提供未经验证的备份/恢复能力）"
            % (self.display_name, action))

    def ensure_change_tracking(self, ref):
        self._not_implemented("变更跟踪启用")

    def backup_full(self, ref, **kwargs):
        self._not_implemented("全量备份")

    def backup_incremental(self, ref, **kwargs):
        self._not_implemented("增量备份")

    def download_artifact(self, artifact, local_path, **kwargs):
        self._not_implemented("产物下载")

    def upload_artifact(self, local_path, **kwargs):
        self._not_implemented("产物上传")

    def cleanup_artifact(self, artifact):
        self._not_implemented("产物清理")

    def synthesize(self, chain, **kwargs):
        self._not_implemented("合成全量")

    def restore_in_place(self, artifact, **kwargs):
        self._not_implemented("原位恢复")

    def clone_to_new_vm(self, artifact, spec, **kwargs):
        self._not_implemented("克隆新 VM")

    def delete_vm(self, ref):
        self._not_implemented("删除 VM")
