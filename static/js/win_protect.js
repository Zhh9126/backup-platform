// -*- coding: utf-8 -*-
// Windows 客户端目录防护（防删除 / 防勒索）：零安装，仅下发 Windows 自带策略。
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const A = (m, u, b) => BKP.api(m, u, b);
  const toast = (m, t) => BKP.toast(m, t || "info", 6000);
  const esc = (s) => BKP.esc(s == null ? "" : String(s));
  let HOSTS = [];

  function body() {
    const raw = ($("wp_paths").value || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
    return {
      host_id: parseInt($("wp_host").value, 10) || 0,
      paths: raw,
      mode: $("wp_mode").value || "cfa",
      allowed_apps: ($("wp_apps").value || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean)
    };
  }
  function valid() {
    const b = body();
    if (!b.host_id) { toast("请先选择一台已纳管的 Windows 主机", "danger"); return null; }
    if (!b.paths.length) { toast("请至少填写一个受保护目录（每行一个）", "danger"); return null; }
    return b;
  }
  function show(data, title) {
    const el = $("wp_result");
    if (!el) return;
    el.textContent = (title ? title + "\n" : "") + JSON.stringify(data, null, 2);
    el.parentElement.classList.remove("d-none");
  }

  window.wpApply = async function () {
    const b = valid(); if (!b) return;
    try {
      const r = await A("POST", "/api/win-protect/apply", b);
      toast("防护已下发（请在下方点「核查状态」确认主机侧生效）", "success");
      show(r.data || r, "下发结果");
      await refresh();
    } catch (e) { toast("下发失败：" + (e.message || e), "danger"); }
  };
  window.wpRemove = async function () {
    const b = valid(); if (!b) return;
    if (!window.confirm("确认解除这些目录的防护？解除后不再受防删除 / 防勒索保护。")) return;
    try {
      await A("POST", "/api/win-protect/remove",
        { host_id: b.host_id, paths: b.paths, mode: b.mode });
      toast("防护已解除", "warning");
      show({ ok: true }, "解除结果");
      await refresh();
    } catch (e) { toast("解除失败：" + (e.message || e), "danger"); }
  };
  window.wpCheck = async function () {
    const b = valid(); if (!b) return;
    try {
      const r = await A("POST", "/api/win-protect/check",
        { host_id: b.host_id, paths: b.paths });
      show(r.data || r, "主机侧真实状态");
    } catch (e) { toast("核查失败：" + (e.message || e), "danger"); }
  };
  window.wpSaveRule = async function () {
    const b = valid(); if (!b) return;
    b.apply_now = $("wp_apply_now").checked;
    try {
      await A("POST", "/api/win-protect/rules", b);
      toast(b.apply_now ? "规则已保存并下发" : "规则已保存（未下发）", "success");
      await refresh();
    } catch (e) { toast("保存失败：" + (e.message || e), "danger"); }
  };
  window.wpDeleteRule = async function (id) {
    if (!window.confirm("删除规则 " + id + "？主机侧防护不会自动解除。")) return;
    try { await A("DELETE", "/api/win-protect/rules/" + id); await refresh(); }
    catch (e) { toast("删除失败：" + (e.message || e), "danger"); }
  };

  async function loadHosts() {
    try {
      const r = await A("GET", "/api/win-protect/hosts");
      HOSTS = r.items || [];
      const sel = $("wp_host");
      if (!sel) return;
      sel.innerHTML = HOSTS.length
        ? HOSTS.map((h) => '<option value="' + h.id + '">' + esc(h.name || h.hostname) +
            "（" + esc(h.hostname) + "）</option>").join("")
        : '<option value="">（尚未纳管 Windows 主机）</option>';
    } catch (e) { toast("加载主机失败：" + (e.message || e), "danger"); }
  }
  const MODE_NAMES = { cfa: "受控文件夹访问", acl_deny_delete: "NTFS 拒绝删除", both: "双重防护" };
  async function refresh() {
    const tb = $("wpRuleTable");
    if (!tb) return;
    try {
      const r = await A("GET", "/api/win-protect/rules");
      const rows = r.items || [];
      tb.innerHTML = rows.length ? rows.map((x) =>
        "<tr><td>" + (x.host_id || "-") + "</td><td><code>" + esc(x.path) + "</code></td>" +
        "<td>" + esc(MODE_NAMES[x.mode] || x.mode) + "</td>" +
        '<td><span class="badge bg-light text-dark border">' + esc(x.status || "unknown") + "</span></td>" +
        '<td class="text-end"><button class="btn btn-sm btn-outline-danger" onclick="wpDeleteRule(' +
        x.id + ')">删除</button></td></tr>').join("")
        : '<tr><td colspan="5" class="text-center text-muted py-3">暂无防护规则</td></tr>';
    } catch (e) { toast("加载规则失败：" + (e.message || e), "danger"); }
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadHosts(); refresh();
  });
})();
