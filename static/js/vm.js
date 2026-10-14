// -*- coding: utf-8 -*-
// 虚拟机备份页（/vm）：虚拟化平台纳管 → 保护 → 恢复点 → 还原 / 克隆 / 恢复验证
// 约定：所有操作都打真实 API；不支持的能力由后端如实返回错误（前端不隐藏失败，只做提示）。
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => window.BKP.esc(s);
  const human = (n) => window.BKP.humanSize(Number(n) || 0);
  let _hvId = 0;
  let _poller = null;

  const badge = (s, ok) => {
    const map = {
      online: ["bg-success", "在线"], offline: ["bg-danger", "离线"],
      error: ["bg-danger", "异常"], unknown: ["bg-secondary", "未探测"],
      running: ["bg-primary", "运行中"], ready: ["bg-success", "就绪"],
      failed: ["bg-danger", "失败"], pending: ["bg-secondary", "排队中"],
      deleted: ["bg-secondary", "已销毁"], expired: ["bg-warning", "已到期"],
      success: ["bg-success", "成功"]
    };
    const pair = map[s] || ["bg-secondary", s || "-"];
    return '<span class="badge ' + pair[0] + '">' + pair[1] + "</span>";
  };

  const rpBadge = (t) => {
    const map = {
      full: ["bg-primary", "全量"], incremental: ["bg-info", "增量"],
      synthetic_full: ["bg-success", "合成全量"]
    };
    const pair = map[t] || ["bg-secondary", t || "-"];
    return '<span class="badge ' + pair[0] + '">' + pair[1] + "</span>";
  };

  async function loadProviders() {
    try {
      const list = await window.BKP.api("GET", "/api/vm/providers");
      $("hv_provider").innerHTML = "";
      (list || []).forEach((p) => {
        const o = document.createElement("option");
        o.value = p.id;
        o.textContent = p.name + " — " + p.desc;
        o.dataset.desc = p.desc || "";
        $("hv_provider").appendChild(o);
      });
      if (list && list.length) $("hv_provider_hint").textContent = list[0].desc || "";
    } catch (e) {
      window.BKP.toast("加载平台类型失败: " + e.message, "danger");
    }
  }

  // ---------------- 虚拟化平台 ----------------
  async function vmLoadHv() {
    try {
      const rows = await window.BKP.api("GET", "/api/vm/hypervisors");
      const pg = Pager.slice("vmHv", rows);
      $("hvTable").innerHTML = pg.list.map((r) => `
        <tr>
          <td>${r.id}</td>
          <td>${esc(r.name)}</td>
          <td>${esc(r.provider_name || r.provider)}</td>
          <td class="small">${esc(r.endpoint)}</td>
          <td class="small">${esc(r.version || "-")}</td>
          <td>${badge(r.status)}</td>
          <td class="small">${window.BKP.fmtTime(r.last_check_at)}</td>
          <td class="text-end">
            <button class="btn btn-sm btn-outline-primary" onclick="vmDiscover(${r.id})">
              <i class="bi bi-search"></i> 资产发现</button>
            <button class="btn btn-sm btn-outline-danger" onclick="vmDelHv(${r.id})">
              <i class="bi bi-trash3"></i></button>
          </td>
        </tr>`).join("") || '<tr><td colspan="8" class="text-center text-muted py-3">尚未纳管虚拟化平台</td></tr>';
      Pager.attach("vmHv", $("hvTable"), pg.page, pg.pages, pg.total, vmLoadHv);
      const sel = $("rpVmFilter");
      sel.innerHTML = '<option value="">全部虚拟机</option>';
      const vms = await window.BKP.api("GET", "/api/vm/protected");
      (vms || []).forEach((v) => {
        const o = document.createElement("option");
        o.value = v.id;
        o.textContent = v.vm_name || ("VM#" + v.id);
        sel.appendChild(o);
      });
    } catch (e) {
      window.BKP.toast("加载虚拟化平台失败: " + e.message, "danger");
    }
  }
  window.vmLoadHv = vmLoadHv;

  window.vmDelHv = async function (id) {
    if (!confirm("删除该虚拟化平台？已建立的保护任务将保留但无法再备份。")) return;
    try {
      await window.BKP.api("DELETE", "/api/vm/hypervisors/" + id);
      window.BKP.toast("已删除");
      vmLoadAll();
    } catch (e) { window.BKP.toast("删除失败: " + e.message, "danger"); }
  };

  window.vmDiscover = async function (hvId) {
    _hvId = hvId;
    $("discoverTable").innerHTML = '<tr><td colspan="8" class="text-center py-3">正在扫描该平台上的虚拟机…</td></tr>';
    $("discoverCaps").textContent = "正在探测平台能力…";
    bootstrap.Modal.getOrCreateInstance($("discoverModal")).show();
    try {
      const rows = await window.BKP.api("GET", "/api/vm/hypervisors/" + hvId + "/vms");
      const caps = (rows && rows[0] && rows[0].provider_supports_incremental !== undefined)
        ? rows[0].provider_supports_incremental : null;
      $("discoverCaps").textContent =
        "平台能力：" + (caps === null ? "未知" : (caps ? "支持块级增量（永久增量）" : "不支持块级增量（每次全量）"));
      $("discoverTable").innerHTML = (rows || []).map((v) => {
        const ex = (v.excluded_disks || []);
        return `<tr>
          <td><input class="form-check-input" type="checkbox" value="${esc(v.ref)}" data-ref="${esc(v.ref)}"></td>
          <td>${esc(v.name)}</td>
          <td class="small">${esc(v.ref)}</td>
          <td>${badge(v.power_state)}</td>
          <td class="small">${v.cpu || "-"}C / ${v.memory_mb || 0}MB</td>
          <td>${(v.disks || []).length}</td>
          <td>${human(v.total_size_bytes)}</td>
          <td class="small text-danger">${ex.length ? ex.map((d) => esc(d.key + ":" + d.reason)).join("<br>") : "-"}</td>
        </tr>`;
      }).join("") || '<tr><td colspan="8" class="text-center text-muted py-3">未发现虚拟机</td></tr>';
    } catch (e) {
      $("discoverTable").innerHTML = '<tr><td colspan="8" class="text-danger py-3">' + esc(e.message) + "</td></tr>";
      $("discoverCaps").textContent = "扫描失败：" + e.message;
    }
  };

  window.vmProtect = async function () {
    const refs = Array.from(document.querySelectorAll("#discoverTable input[type=checkbox]:checked"))
      .map((el) => el.value);
    if (!refs.length) { window.BKP.toast("请先勾选要保护的虚拟机", "danger"); return; }
    try {
      const res = await window.BKP.api("POST", "/api/vm/protect", {
        hypervisor_id: _hvId, vm_refs: refs,
        backup_interval_min: Number($("pr_interval").value) || 1440,
        consistency: $("pr_consistency").value,
        rpo_target_min: Number($("pr_rpo").value) || 1440,
        retention_days: Number($("pr_days").value) || 30,
        retention_count: Number($("pr_count").value) || 60
      });
      window.BKP.toast(res.message || "已建立保护");
      bootstrap.Modal.getInstance($("discoverModal")).hide();
      vmLoadAll();
    } catch (e) { window.BKP.toast("建立保护失败: " + e.message, "danger"); }
  };

  // ---------------- 受保护虚拟机 ----------------
  async function vmLoadProtected() {
    try {
      const rows = await window.BKP.api("GET", "/api/vm/protected");
      const pg = Pager.slice("vmProtected", rows);
      $("vmTable").innerHTML = pg.list.map((v) => `
        <tr>
          <td>${v.id}</td>
          <td>${esc(v.vm_name || v.vm_ref)}<div class="small text-muted">${esc(v.guest_os || "")}</div></td>
          <td class="small">${esc(v.hypervisor_name || "-")}</td>
          <td>${badge(v.power_state)}</td>
          <td class="small">${v.disk_count || 0} 块 / ${human(v.total_size_bytes)}</td>
          <td class="small">每 ${v.backup_interval_min || "-"} 分钟</td>
          <td class="small">${esc(v.consistency || "crash")}</td>
          <td>${v.rp_count || 0}</td>
          <td class="small">${window.BKP.fmtTime(v.last_rp)}</td>
          <td>${badge(v.last_status || "never")}</td>
          <td class="text-end">
            <button class="btn btn-sm btn-outline-success" onclick="vmBackup(${v.id},'full')"
              title="立即全量"><i class="bi bi-hdd"></i> 全量</button>
            <button class="btn btn-sm btn-outline-primary" onclick="vmBackup(${v.id},'incremental')"
              title="立即增量（不支持时后端会如实回退全量）"><i class="bi bi-lightning"></i> 增量</button>
            <button class="btn btn-sm btn-outline-danger" onclick="vmUnprotect(${v.id})">
              <i class="bi bi-x-lg"></i></button>
          </td>
        </tr>`).join("") || '<tr><td colspan="11" class="text-center text-muted py-3">尚无受保护的虚拟机</td></tr>';
      Pager.attach("vmProtected", $("vmTable"), pg.page, pg.pages, pg.total, vmLoadProtected);
    } catch (e) {
      window.BKP.toast("加载受保护虚拟机失败: " + e.message, "danger");
    }
  }
  window.vmLoadProtected = vmLoadProtected;

  window.vmBackup = async function (id, type) {
    const busy = (t) => '<span class="spinner-border spinner-border-sm"></span> ' + t;
    try {
      window.BKP.toast("已提交" + (type === "full" ? "全量" : "增量") + "备份，后台执行中…");
      const res = await window.BKP.api("POST", "/api/vm/protected/" + id + "/backup",
        { backup_type: type });
      window.BKP.toast(res.message || "备份完成");
      vmLoadAll();
    } catch (e) { window.BKP.toast("备份失败: " + e.message, "danger", 6000); }
  };

  window.vmUnprotect = async function (id) {
    if (!confirm("取消保护？原有恢复点将一并删除（不可恢复）。")) return;
    try {
      await window.BKP.api("DELETE", "/api/vm/protected/" + id);
      window.BKP.toast("已取消保护");
      vmLoadAll();
    } catch (e) { window.BKP.toast("操作失败: " + e.message, "danger"); }
  };

  // ---------------- 恢复点 ----------------
  async function vmLoadRps() {
    const vmId = $("rpVmFilter").value;
    const url = "/api/vm/recovery-points" + (vmId ? ("?vm_id=" + vmId) : "");
    try {
      const rows = await window.BKP.api("GET", url);
      const pg = Pager.slice("vmRps", rows);
      $("rpTable").innerHTML = pg.list.map((r) => `
        <tr>
          <td>${r.id}</td>
          <td>${esc(r.vm_name || ("VM#" + r.vm_id))}</td>
          <td class="small">${window.BKP.fmtTime(r.pit_at)}</td>
          <td>${rpBadge(r.rp_type)}</td>
          <td class="small">${esc(r.consistency || "-")}</td>
          <td>${human(r.size_bytes)}</td>
          <td class="small text-muted" title="${esc(r.checksum || "")}">${(r.checksum || "-").slice(0, 10)}</td>
          <td class="small">${r.verified ? '<span class="badge bg-success">已通过</span>'
            : (r.verify_msg ? '<span class="badge bg-danger" title="' + esc(r.verify_msg) + '">未通过</span>'
              : '<span class="text-muted">未验证</span>')}</td>
          <td class="text-end">
            <button class="btn btn-sm btn-outline-warning" onclick="vmRestore(${r.id})"
              title="原位还原：覆盖原虚拟机"><i class="bi bi-arrow-counterclockwise"></i> 原位还原</button>
            <button class="btn btn-sm btn-outline-primary" onclick="vmOpenClone(${r.id})"
              title="克隆为新虚拟机"><i class="bi bi-layers"></i> 克隆</button>
            <button class="btn btn-sm btn-outline-success" onclick="vmVerify(${r.id})"
              title="自动恢复验证：隔离网络拉起 + 健康检查"><i class="bi bi-patch-check"></i> 验证</button>
            <button class="btn btn-sm btn-outline-danger" onclick="vmDelRp(${r.id})">
              <i class="bi bi-trash3"></i></button>
          </td>
        </tr>`).join("") || '<tr><td colspan="9" class="text-center text-muted py-3">暂无恢复点</td></tr>';
      Pager.attach("vmRps", $("rpTable"), pg.page, pg.pages, pg.total, vmLoadRps);
    } catch (e) {
      window.BKP.toast("加载恢复点失败: " + e.message, "danger");
    }
  }
  window.vmLoadRps = vmLoadRps;

  window.vmDelRp = async function (id) {
    try {
      await window.BKP.api("DELETE", "/api/vm/recovery-points/" + id);
      window.BKP.toast("已删除恢复点");
      vmLoadAll();
    } catch (e) { window.BKP.toast("删除失败: " + e.message, "danger", 6000); }
  };

  window.vmRestore = async function (rpId) {
    if (!confirm("原位还原会覆盖原虚拟机的磁盘，确认继续？")) return;
    try {
      const res = await window.BKP.api("POST", "/api/vm/recovery-points/" + rpId + "/restore", {});
      window.BKP.toast(res.message || "还原作业已提交");
      switchTab("tab-jobs");
      startPolling();
    } catch (e) { window.BKP.toast("还原失败: " + e.message, "danger", 6000); }
  };

  window.vmVerify = async function (rpId) {
    try {
      const res = await window.BKP.api("POST", "/api/vm/recovery-points/" + rpId + "/verify", {});
      window.BKP.toast(res.message || "恢复验证已提交");
      switchTab("tab-jobs");
      startPolling();
    } catch (e) { window.BKP.toast("提交失败: " + e.message, "danger", 6000); }
  };

  window.vmOpenClone = function (rpId) {
    $("cl_rp_id").value = rpId;
    bootstrap.Modal.getOrCreateInstance($("cloneModal")).show();
  };

  window.vmClone = async function () {
    const rpId = $("cl_rp_id").value;
    if (!rpId) return;
    try {
      const res = await window.BKP.api("POST", "/api/vm/recovery-points/" + rpId + "/clone", {
        target_name: $("cl_name").value,
        target_node: $("cl_node").value,
        isolate_network: $("cl_iso").checked,
        network_name: $("cl_net").value,
        auto_start: $("cl_start").value === "1",
        regenerate_mac: $("cl_mac").value === "1",
        live: $("cl_live").checked,
        ttl_hours: Number($("cl_ttl").value) || 0
      });
      window.BKP.toast(res.message || "克隆作业已提交");
      bootstrap.Modal.getInstance($("cloneModal")).hide();
      switchTab("tab-jobs");
      startPolling();
    } catch (e) { window.BKP.toast("克隆失败: " + e.message, "danger", 6000); }
  };

  // ---------------- 作业 ----------------
  async function vmLoadJobs() {
    try {
      const rows = await window.BKP.api("GET", "/api/vm/jobs");
      const modeName = {
        restore_in_place: "原位还原", clone: "克隆新 VM", verify: "恢复验证", delete: "销毁"
      };
      const pg = Pager.slice("vmJobs", rows);
      $("jobTable").innerHTML = pg.list.map((j) => `
        <tr>
          <td>${j.id}</td>
          <td>${esc(j.vm_name || ("VM#" + j.vm_id))}</td>
          <td>${esc(modeName[j.mode] || j.mode)}</td>
          <td class="small">${esc(j.target_name || j.target_ref || "-")}</td>
          <td>${badge(j.status)}</td>
          <td>${j.isolate_network ? '<span class="badge bg-success">是</span>'
            : '<span class="badge bg-warning text-dark">否</span>'}</td>
          <td class="small">${j.ttl_hours ? (j.ttl_hours + " 小时") : "-"}</td>
          <td class="small text-break" style="max-width:320px">${esc(j.message || "-")}</td>
          <td class="text-end">
            ${(j.target_ref && j.status !== "deleted")
        ? `<button class="btn btn-sm btn-outline-danger" onclick="vmDestroy(${j.id})">
                 <i class="bi bi-trash3"></i> 销毁目标 VM</button>` : ""}
          </td>
        </tr>`).join("") || '<tr><td colspan="9" class="text-center text-muted py-3">暂无作业</td></tr>';
      Pager.attach("vmJobs", $("jobTable"), pg.page, pg.pages, pg.total, vmLoadJobs);

      const running = (rows || []).some((j) => j.status === "running");
      if (!running && _poller) { clearInterval(_poller); _poller = null; }
    } catch (e) {
      window.BKP.toast("加载作业失败: " + e.message, "danger");
    }
  }
  window.vmLoadJobs = vmLoadJobs;

  window.vmDestroy = async function (jobId) {
    if (!confirm("销毁该作业产生的目标虚拟机？此操作不可恢复。")) return;
    try {
      const res = await window.BKP.api("POST", "/api/vm/jobs/" + jobId + "/destroy", {});
      window.BKP.toast(res.message || "已销毁");
      vmLoadJobs();
    } catch (e) { window.BKP.toast("销毁失败: " + e.message, "danger"); }
  };

  window.vmReap = async function () {
    try {
      const res = await window.BKP.api("POST", "/api/vm/jobs/reap", {});
      window.BKP.toast("回收完成：" + ((res.reaped || []).length) + " 个，失败 " +
        ((res.failed || []).length) + " 个");
      vmLoadJobs();
    } catch (e) { window.BKP.toast("回收失败: " + e.message, "danger"); }
  };

  // ---------------- 杂项 ----------------
  function switchTab(id) {
    const el = document.querySelector('a[href="#' + id + '"]');
    if (el) bootstrap.Tab.getOrCreateInstance(el).show();
  }

  function startPolling() {
    if (_poller) return;
    _poller = setInterval(vmLoadJobs, 3000);
  }

  window.vmLoadAll = function () {
    vmLoadHv();
    vmLoadProtected();
    vmLoadRps();
    vmLoadJobs();
  };

  async function saveHv() {
    const extra = {};
    if ($("hv_iso").value) extra.isolate_network = $("hv_iso").value;
    if ($("hv_work").value) extra.work_dir = $("hv_work").value;
    if ($("hv_storage").value) {
      const p = $("hv_provider").value;
      if (p === "pve") extra.storage = $("hv_storage").value;
      else if (p === "libvirt_ssh") extra.uri = $("hv_storage").value;
      else extra.storage = $("hv_storage").value;
    }
    try {
      const res = await window.BKP.api("POST", "/api/vm/hypervisors", {
        name: $("hv_name").value, provider: $("hv_provider").value,
        endpoint: $("hv_endpoint").value, username: $("hv_username").value,
        password: $("hv_password").value, verify_ssl: $("hv_ssl").value === "1",
        extra_config: JSON.stringify(extra)
      });
      window.BKP.toast(res.message || "已纳管");
      bootstrap.Modal.getInstance($("hvModal")).hide();
      vmLoadAll();
    } catch (e) { window.BKP.toast("纳管失败: " + e.message, "danger", 6000); }
  }

  async function testHv() {
    try {
      const res = await window.BKP.api("POST", "/api/vm/hypervisors/test", {
        provider: $("hv_provider").value, endpoint: $("hv_endpoint").value,
        username: $("hv_username").value, password: $("hv_password").value,
        verify_ssl: $("hv_ssl").value === "1"
      });
      window.BKP.toast((res.ok ? "连接成功：" : "连接失败：") + (res.message || ""),
        res.ok ? "dark" : "danger", 6000);
    } catch (e) { window.BKP.toast("测试失败: " + e.message, "danger"); }
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadProviders();
    vmLoadAll();
    $("newHvBtn").addEventListener("click", function () {
      bootstrap.Modal.getOrCreateInstance($("hvModal")).show();
    });
    $("hvSaveBtn").addEventListener("click", saveHv);
    $("hvTestBtn").addEventListener("click", testHv);
    $("protectBtn").addEventListener("click", window.vmProtect);
    $("cloneSubmitBtn").addEventListener("click", window.vmClone);
    $("rpVmFilter").addEventListener("change", vmLoadRps);
  });
})();
