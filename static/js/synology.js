// -*- coding: utf-8 -*-
// 群晖 NAS 全面纳管页面逻辑（设备 / 探活 / 资产 / 一键全量+增量任务 / 场景矩阵）
//
// 后端契约：
//   设备 CRUD / 探测 / 资产 / quick-task  → /api/synology/*（api/synology.py）
//   生成的任务走通用 /api/tasks，db_type=synology，凭据按 device_id 引用设备表
//
// 诚实边界在页面上直接可见：SAN Manager 裸 LUN 不直接备份、非公开 API 不承诺、
// FileStation.Upload（推回 DSM）未实现。
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const { api, esc, toast, statusBadge, humanSize } = BKP;
  const BASE = "/api/synology";

  let DEVICES = [];
  let SHARES = [];
  let TASKS = [];
  let SCENARIOS = null;
  const SELECTED = new Set();

  // ------------------------- 工具 -------------------------
  function errMsg(e) { return (e && e.message) ? String(e.message) : "操作失败"; }
  function ok(msg) { toast(msg, "dark"); }
  function fail(e) { toast(errMsg(e), "danger"); }
  function fmt(t) { return t ? String(t).replace("T", " ").slice(0, 19) : "-"; }
  function bool(v) { return v === 1 || v === true || v === "1"; }
  function badge(text, cls) {
    return '<span class="badge ' + (cls || "bg-secondary") + '">' + esc(text) + "</span>";
  }
  function modal(id) {
    const el = $(id);
    if (!el) return null;
    return window.bootstrap.Modal.getOrCreateInstance(el);
  }

  // Pager 是可选公共组件：缺失时退化为全量渲染，页面不因此崩掉
  function page(key, rows, size, render, anchorId) {
    const P = window.Pager;
    if (!P || typeof P.slice !== "function") { render(rows, 1); return; }
    const pg = P.slice(key, rows, size);
    render(pg.list, pg.startNo || 1);
    if ($(anchorId)) {
      P.attach(key, $(anchorId), pg.page, pg.pages, pg.total,
        () => page(key, rows, size, render, anchorId));
    }
  }

  // ------------------------- 设备 -------------------------
  function capBadges(d) {
    const c = d.capabilities || {};
    const out = [];
    out.push(badge(c.filestation ? "FileStation ✓" : "FileStation ✗",
      c.filestation ? "bg-success" : "bg-danger"));
    if (c.public_apis && c.public_apis.length) {
      out.push(badge("公开 API " + c.public_apis.length, "bg-info"));
    }
    if (c.unofficial && c.unofficial.length) {
      out.push(badge("非公开命中 " + c.unofficial.length + "（不调用）", "bg-warning text-dark"));
    }
    if (!Object.keys(c).length) out.push('<span class="text-muted small">未探测</span>');
    return out.join(" ");
  }

  function renderDevices(rows, startNo) {
    const tb = $("synDeviceTable");
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="7" class="text-muted">尚未纳管 DSM 设备，点右上角「纳管 DSM 设备」开始</td></tr>';
      return;
    }
    tb.innerHTML = rows.map((d, i) => {
      const proto = bool(d.secure) ? "HTTPS" : "HTTP";
      const st = d.last_status === "online"
        ? badge("在线", "bg-success")
        : (d.last_status === "error" ? badge("异常", "bg-danger") : badge("未探测", "bg-secondary"));
      const ver = [d.dsm_version, d.model].filter(Boolean).join(" / ") || "-";
      return "<tr>" +
        "<td>" + (startNo + i) + "</td>" +
        "<td><strong>" + esc(d.name) + "</strong>" +
          (d.remark ? '<div class="text-muted small">' + esc(d.remark) + "</div>" : "") +
        "</td>" +
        "<td><code>" + esc(d.hostname) + ":" + esc(d.port) + "</code>" +
          '<div class="text-muted small">' + proto + (bool(d.verify_ssl) ? " · 校验证书" : "") +
          " · " + esc(d.username || "-") + "</div></td>" +
        "<td>" + esc(ver) + "</td>" +
        '<td style="max-width:230px">' + capBadges(d) + "</td>" +
        "<td>" + st + (d.last_error
          ? '<div class="text-muted small" title="' + esc(d.last_error) + '">' +
            esc(String(d.last_error).slice(0, 40)) + "</div>" : "") +
          (d.last_check_at ? '<div class="text-muted small">' + fmt(d.last_check_at) + "</div>" : "") +
        "</td>" +
        '<td class="d-flex flex-wrap gap-1">' +
          '<button class="btn btn-sm btn-outline-primary" data-act="probe" data-id="' + d.id + '">探测</button>' +
          '<button class="btn btn-sm btn-outline-secondary" data-act="assets" data-id="' + d.id + '">资产</button>' +
          '<button class="btn btn-sm btn-outline-secondary" data-act="edit" data-id="' + d.id + '">编辑</button>' +
          '<button class="btn btn-sm btn-outline-danger" data-act="del" data-id="' + d.id + '">删除</button>' +
        "</td></tr>";
    }).join("");
  }

  async function loadDevices() {
    try {
      DEVICES = await api("GET", BASE + "/devices");
      $("synDeviceCount").textContent = String(DEVICES.length);
      $("synKpiDevices").textContent = String(DEVICES.length);
      $("synKpiOnline").textContent = String(DEVICES.filter(d => d.last_status === "online").length);
      page("synDevices", DEVICES, 10, renderDevices, "synDevicePager");
      fillDeviceSelect();
    } catch (e) { fail(e); }
  }

  function fillDeviceSelect() {
    const sel = $("synAssetDevice");
    const cur = sel.value;
    sel.innerHTML = DEVICES.length
      ? DEVICES.map(d => '<option value="' + d.id + '">' + esc(d.name) +
          " (" + esc(d.hostname) + ")</option>").join("")
      : '<option value="">（请先纳管设备）</option>';
    if (cur && DEVICES.some(d => String(d.id) === cur)) sel.value = cur;
  }

  // ------------------------- 设备表单 -------------------------
  function formPayload() {
    return {
      name: $("syn_d_name").value.trim(),
      hostname: $("syn_d_hostname").value.trim(),
      port: parseInt($("syn_d_port").value, 10) || (bool($("syn_d_secure").checked) ? 5001 : 5000),
      timeout: parseInt($("syn_d_timeout").value, 10) || 15,
      secure: $("syn_d_secure").checked,
      verify_ssl: $("syn_d_verify_ssl").checked,
      username: $("syn_d_username").value.trim(),
      password: $("syn_d_password").value,
      otp_code: $("syn_d_otp").value.trim(),
      remark: $("syn_d_remark").value.trim(),
      enabled: $("syn_d_enabled").checked,
    };
  }

  function openDeviceModal(d) {
    $("synDeviceModalTitle").textContent = d ? "编辑 DSM 设备 " + d.id : "纳管 DSM 设备";
    $("syn_d_id").value = d ? d.id : "";
    $("syn_d_name").value = d ? d.name : "";
    $("syn_d_hostname").value = d ? d.hostname : "";
    $("syn_d_port").value = d ? d.port : 5000;
    $("syn_d_timeout").value = d ? d.timeout : 15;
    $("syn_d_secure").checked = d ? bool(d.secure) : false;
    $("syn_d_verify_ssl").checked = d ? bool(d.verify_ssl) : false;
    $("syn_d_username").value = d ? (d.username || "") : "";
    $("syn_d_password").value = "";
    $("syn_d_otp").value = "";
    $("syn_d_remark").value = d ? (d.remark || "") : "";
    $("syn_d_enabled").checked = d ? bool(d.enabled) : true;
    modal("synDeviceModal").show();
  }

  async function saveDevice() {
    const p = formPayload();
    const id = $("syn_d_id").value;
    delete p.otp_code;
    if (!p.name || !p.hostname) { toast("设备名称与 DSM 地址必填", "danger"); return; }
    if (!id && !p.password) { toast("新建设备请填写密码", "danger"); return; }
    try {
      if (id) await api("PUT", BASE + "/devices/" + id, p);
      else await api("POST", BASE + "/devices", p);
      modal("synDeviceModal").hide();
      ok("设备已保存");
      await loadDevices();
    } catch (e) { fail(e); }
  }

  function renderProbe(out) {
    const rows = [];
    rows.push(["结论", out.ok ? badge("探活通过", "bg-success") : badge("失败", "bg-danger")]);
    rows.push(["目标", esc((out.host || "-") + ":" + (out.port || "-")) +
      (out.secure ? " (HTTPS)" : " (HTTP)")]);
    rows.push(["DSM 版本 / 型号", esc([out.dsm_version, out.model].filter(Boolean).join(" / ") || "-")]);
    rows.push(["发现 API 数", esc(out.api_count != null ? out.api_count : "-")]);
    rows.push(["官方公开 API", (out.public_apis || []).map(x => "<code>" + esc(x) + "</code>").join(" ") || "-"]);
    rows.push(["非公开命中", (out.unofficial || []).map(x => badge(x, "bg-warning text-dark")).join(" ") || "无"]);
    rows.push(["共享文件夹", esc(out.share_count != null ? out.share_count : "-") +
      ((out.shares || []).length ? "（" + esc(out.shares.slice(0, 8).join("、")) + "）" : "")]);
    if (out.error) rows.push(["错误", '<span class="text-danger">' + esc(out.error) +
      (out.error_code ? " · code=" + esc(out.error_code) : "") + "</span>"]);
    if (out.note) rows.push(["边界说明", '<span class="text-muted">' + esc(out.note) + "</span>"]);
    if (out.warning) rows.push(["提示", '<span class="text-warning">' + esc(out.warning) + "</span>"]);
    $("synProbeBody").innerHTML =
      '<table class="table table-sm mb-0"><tbody>' +
      rows.map(r => "<tr><th style='width:150px'>" + r[0] + "</th><td>" + r[1] + "</td></tr>").join("") +
      "</tbody></table>" +
      '<div class="alert alert-light border mt-3 mb-0 small">探测走的是真实 DSM Web API。' +
      "「非公开命中」只表示 API 名称出现在能力发现结果里，平台<strong>未调用、不承诺可用</strong>；" +
      "SAN Manager 裸 LUN 不做块级备份。</div>";
    modal("synProbeModal").show();
  }

  async function probe(id, unsaved) {
    try {
      const out = unsaved
        ? await api("POST", BASE + "/test", unsaved)
        : await api("POST", BASE + "/devices/" + id + "/test", {});
      renderProbe(out);
      if (!unsaved) await loadDevices();
    } catch (e) {
      // 后端探测失败返 400，响应体里带着完整诊断，不能只弹一句「请求失败」
      if (e && e.data && e.data.ok === false) renderProbe(e.data);
      else fail(e);
    }
  }

  async function delDevice(id) {
    if (!window.confirm("确定移除该 DSM 纳管记录？引用它的备份任务会阻止删除。")) return;
    async function attempt(body) {
      try { await api("DELETE", BASE + "/devices/" + id, body || {}); return true; }
      catch (e) {
        if (e && e.status === 403 && e.data && e.data.verify_code_required) {
          const vc = await api("GET", "/api/verify-code");
          const code = window.prompt("高危操作验证码：" + (vc.code || "") + "\n请输入 4 位验证码", "");
          if (!code) return false;
          return attempt({ verify_code: code });
        }
        fail(e);
        return false;
      }
    }
    if (await attempt()) { ok("设备已移除"); await loadDevices(); }
  }

  // ------------------------- 资产 -------------------------
  async function loadShares(keepSelection) {
    const id = $("synAssetDevice").value;
    if (!id) { $("synAssetList").innerHTML = '<div class="text-muted small">请先纳管设备</div>'; return; }
    if (!keepSelection) SELECTED.clear();
    try {
      const out = await api("GET", BASE + "/devices/" + id + "/shares");
      SHARES = out.shares || [];
      $("synKpiShares").textContent = String(SHARES.length);
      $("synAssetHint").textContent = "（" + SHARES.length + " 个 · 官方 FileStation API）";
      if (!SHARES.length) {
        $("synAssetList").innerHTML = '<div class="text-muted small">该账号看不到任何共享文件夹，请检查 DSM 权限</div>';
        return;
      }
      $("synAssetList").innerHTML = SHARES.map(s =>
        '<div class="form-check small">' +
        '<input class="form-check-input syn-share" type="checkbox" value="' + esc(s.path) +
        '" id="syn_sh_' + esc(String(s.path).replace(/[^a-zA-Z0-9]/g, "_")) + '"' +
        (SELECTED.has(s.path) ? " checked" : "") + ">" +
        '<label class="form-check-label" for="syn_sh_' + esc(String(s.path).replace(/[^a-zA-Z0-9]/g, "_")) + '">' +
        "<code>" + esc(s.path) + "</code>" +
        (s.real_path ? ' <span class="text-muted">→ ' + esc(s.real_path) + "</span>" : "") +
        (s.size ? " · " + esc(humanSize(s.size)) : "") +
        "</label></div>").join("");
    } catch (e) {
      fail(e);
      $("synAssetList").innerHTML = '<div class="text-danger small">' + esc(errMsg(e)) + "</div>";
    }
  }

  function selectedPaths() {
    return Array.from(document.querySelectorAll(".syn-share:checked")).map(el => el.value);
  }

  async function statAssets() {
    const id = $("synAssetDevice").value;
    const paths = selectedPaths();
    if (!id || !paths.length) { toast("请先勾选至少一个共享文件夹", "danger"); return; }
    try {
      const out = await api("POST", BASE + "/devices/" + id + "/assets",
        { path: paths[0], max_files: 200000 });
      $("synQuickResult").innerHTML =
        '<div class="alert alert-light border py-2 mb-0"><strong>' + esc(out.root) + "</strong>：" +
        out.files + " 个文件 / " + out.dirs + " 个目录 / " + esc(humanSize(out.bytes)) +
        (out.unreadable ? " · <span class='text-warning'>" + out.unreadable + " 个目录无权限已跳过</span>" : "") +
        (out.truncated ? " · <span class='text-warning'>已达枚举上限，实际文件更多</span>" : "") +
        "<br><span class='text-muted'>只读枚举，不下载文件体；增量判据为 size + mtime。</span></div>";
    } catch (e) { fail(e); }
  }

  async function quickTask() {
    const id = $("synAssetDevice").value;
    const paths = selectedPaths();
    if (!id) { toast("请先选择设备", "danger"); return; }
    if (!paths.length) { toast("请勾选要保护的共享文件夹", "danger"); return; }
    const payload = {
      name: $("syn_qt_name").value.trim(),
      biz_system: $("syn_qt_biz").value.trim(),
      paths: paths,
      strategy: $("syn_qt_strategy").value,
      full_cron: $("syn_qt_full_cron").value.trim() || "0 2 * * 0",
      incremental_minutes: parseInt($("syn_qt_inc_min").value, 10) || 60,
      retention_days: parseInt($("syn_qt_retention").value, 10) || 30,
      excludes: $("syn_qt_excludes").value.split(",").map(s => s.trim()).filter(Boolean),
      enabled: $("syn_qt_enabled").checked,
      run_now: $("syn_qt_run_now").checked,
    };
    try {
      const out = await api("POST", BASE + "/devices/" + id + "/quick-task", payload);
      $("synQuickResult").innerHTML =
        '<div class="alert alert-success py-2 mb-0">已创建备份任务 <strong>' + out.id +
        "</strong>（" + (out.strategy === "mixed" ? "全量 " + esc(out.full_cron) +
          " + 每 " + out.incremental_minutes + " 分钟增量" : "仅全量") + "）：" +
        esc(out.paths.join("、")) + "。<br><span class='text-muted'>" + esc(out.note || "") +
        (out.run && out.run.error ? " · 立即执行失败：" + esc(out.run.error) : "") +
        "</span></div>";
      ok("备份任务已生成");
      await loadTasks();
    } catch (e) { fail(e); }
  }

  // ------------------------- 任务 -------------------------
  function renderTasks(rows, startNo) {
    const tb = $("synTaskTable");
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="7" class="text-muted">尚无群晖备份任务，在上方勾选共享文件夹后一键生成</td></tr>';
      return;
    }
    tb.innerHTML = rows.map((t, i) =>
      "<tr><td>" + (startNo + i) + "</td>" +
      "<td><a href='/tasks?focus=" + t.id + "'><strong>" + esc(t.name) + "</strong></a>" +
        '<div class="text-muted small">任务 ID ' + t.id + "</div></td>" +
      "<td>" + esc(t.biz_system || "-") + "</td>" +
      "<td><code>" + esc((t.paths || []).join("、") || "-") + "</code></td>" +
      "<td>" + (t.mixed_backup ? badge("全量+增量", "bg-info") : badge("仅全量", "bg-secondary")) +
        (t.enabled ? "" : " " + badge("已停用", "bg-warning text-dark")) + "</td>" +
      "<td>" + statusBadge(t.last_status || "never") + "</td>" +
      "<td>" + fmt(t.last_run_at) + "</td></tr>").join("");
  }

  async function loadTasks() {
    try {
      TASKS = await api("GET", BASE + "/tasks");
      $("synKpiTasks").textContent = String(TASKS.length);
      page("synTasks", TASKS, 10, renderTasks, "synTaskPager");
    } catch (e) { fail(e); }
  }

  // ------------------------- 场景矩阵 -------------------------
  async function loadScenarios() {
    try {
      SCENARIOS = await api("GET", BASE + "/scenarios");
      const cls = { implemented: "bg-success", planned: "bg-info", boundary: "bg-warning text-dark" };
      const label = { implemented: "已落地", planned: "待立项/仅探测", boundary: "明确边界" };
      let html = '<div class="row g-2">';
      (SCENARIOS.channels || []).forEach(s => {
        html += '<div class="col-md-6"><div class="border rounded p-2 h-100">' +
          '<div class="d-flex justify-content-between align-items-start gap-2 mb-1">' +
          "<strong>" + esc(s.id + " · " + s.name) + "</strong>" +
          badge(label[s.status] || s.status, cls[s.status] || "bg-secondary") + "</div>" +
          '<div class="text-muted small mb-1">' + esc(s.kind) + "</div>" +
          '<div class="small">' + esc(s.summary) + "</div>" +
          '<ul class="small text-muted mb-0 mt-1">' +
          (s.notes || []).map(n => "<li>" + esc(n) + "</li>").join("") +
          "</ul></div></div>";
      });
      html += "</div>";
      html += '<h6 class="mt-3">iSCSI / SAN 交付信息模板</h6><div class="border rounded p-2 small">' +
        (SCENARIOS.delivery_template || []).map(x => "<div>" + esc(x) + "</div>").join("") + "</div>";
      html += '<div class="alert alert-light border mt-3 mb-0 small">' +
        esc(SCENARIOS.note || "") + "</div>";
      $("synScenarios").innerHTML = html;
    } catch (e) {
      $("synScenarios").innerHTML = '<div class="text-danger small">' + esc(errMsg(e)) + "</div>";
    }
  }

  // ------------------------- 绑定 -------------------------
  function bind() {
    $("synRefreshBtn").addEventListener("click", () => {
      loadDevices().then(() => loadShares(true));
      loadTasks();
    });
    $("synNewDeviceBtn").addEventListener("click", () => openDeviceModal(null));
    $("synSaveDeviceBtn").addEventListener("click", saveDevice);
    $("synTestFormBtn").addEventListener("click", () => {
      const p = formPayload();
      if (!p.hostname || !p.username) { toast("请先填写 DSM 地址与账号", "danger"); return; }
      if (!p.password && $("syn_d_id").value) {
        probe(parseInt($("syn_d_id").value, 10));
      } else {
        probe(null, p);
      }
    });
    $("synDeviceTable").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const id = parseInt(btn.dataset.id, 10);
      const act = btn.dataset.act;
      const d = DEVICES.find(x => x.id === id);
      if (act === "edit") openDeviceModal(d);
      else if (act === "probe") probe(id);
      else if (act === "del") delDevice(id);
      else if (act === "assets") {
        $("synAssetDevice").value = String(id);
        loadShares(false);
        $("synAssetDevice").scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
    $("synAssetDevice").addEventListener("change", () => loadShares(false));
    $("synAssetList").addEventListener("change", (e) => {
      if (!e.target.classList.contains("syn-share")) return;
      if (e.target.checked) SELECTED.add(e.target.value);
      else SELECTED.delete(e.target.value);
    });
    $("synQuickTaskBtn").addEventListener("click", quickTask);
    $("synAssetsStatBtn").addEventListener("click", statAssets);
  }

  function init() {
    if (!$("synDeviceTable")) return;
    bind();
    loadDevices().then(() => loadShares(false));
    loadTasks();
    loadScenarios();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
