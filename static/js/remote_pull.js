// -*- coding: utf-8 -*-
// 异地容灾「云侧推对象存储 + 平台出站拉取」页面逻辑
// 后端：/api/remote-pull/*（模块同时注册在 /api/v1/remote-pull/*）
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const { api, esc, toast, humanSize } = BKP;
  const BASE = "/api/remote-pull";

  let SOURCES = [];
  let LOGS = [];

  function errMsg(e) { return (e && e.message) ? String(e.message) : "操作失败"; }
  function fail(e) { toast(errMsg(e), "danger"); }
  function fmt(t) { return t ? String(t).replace("T", " ").slice(0, 19) : "-"; }
  function bool(v) { return v === 1 || v === true || v === "1"; }
  function badge(t, cls) { return '<span class="badge ' + (cls || "bg-secondary") + '">' + esc(t) + "</span>"; }
  function modal(id) { return window.bootstrap.Modal.getOrCreateInstance($(id)); }
  function page(key, rows, size, render, anchor) {
    const P = window.Pager;
    if (!P || typeof P.slice !== "function") { render(rows, 1); return; }
    const pg = P.slice(key, rows, size);
    render(pg.list, pg.startNo || 1);
    if ($(anchor)) P.attach(key, $(anchor), pg.page, pg.pages, pg.total,
      () => page(key, rows, size, render, anchor));
  }

  // ------------------------- 来源 -------------------------
  function renderSources(rows, startNo) {
    const tb = $("rpSourceTable");
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="6" class="text-muted">尚未配置拉取来源，点右上角「新增拉取来源」开始</td></tr>';
      return;
    }
    tb.innerHTML = rows.map((s, i) => {
      const st = s.last_status === "online" ? badge("正常", "bg-success")
        : (s.last_status === "error" ? badge("异常", "bg-danger") : badge("未探测", "bg-secondary"));
      return "<tr><td>" + (startNo + i) + "</td>" +
        "<td><strong>" + esc(s.name) + "</strong>" +
          (s.remark ? '<div class="text-muted small">' + esc(s.remark) + "</div>" : "") +
          (s.enabled ? "" : " " + badge("已停用", "bg-warning text-dark")) + "</td>" +
        "<td><code>" + esc(s.endpoint) + "</code>" +
          '<div class="text-muted small">桶 ' + esc(s.bucket) +
          (s.prefix ? " / " + esc(s.prefix) : "") +
          (bool(s.secure) ? " · HTTPS" : " · HTTP") +
          (s.has_secret_key ? " · 密钥已配置" : " · <span class='text-warning'>未配置密钥</span>") +
          "</div></td>" +
        "<td>每 " + esc(s.interval_minutes || 30) + " 分钟" +
          (bool(s.delete_after_pull) ? '<div class="text-muted small">拉取后删远端</div>' : "") + "</td>" +
        "<td>" + st + (s.last_error
          ? '<div class="text-muted small" title="' + esc(s.last_error) + '">' +
            esc(String(s.last_error).slice(0, 32)) + "</div>" : "") +
          (s.last_run_at ? '<div class="text-muted small">' + fmt(s.last_run_at) + "</div>" : "") + "</td>" +
        '<td class="d-flex flex-wrap gap-1">' +
          '<button class="btn btn-sm btn-outline-primary" data-act="test" data-id="' + s.id + '">探活</button>' +
          '<button class="btn btn-sm btn-outline-secondary" data-act="preview" data-id="' + s.id + '">预览</button>' +
          '<button class="btn btn-sm btn-outline-secondary" data-act="pull" data-id="' + s.id + '">拉取</button>' +
          '<button class="btn btn-sm btn-outline-secondary" data-act="edit" data-id="' + s.id + '">编辑</button>' +
          '<button class="btn btn-sm btn-outline-danger" data-act="del" data-id="' + s.id + '">删除</button>' +
        "</td></tr>";
    }).join("");
  }

  async function loadSources() {
    try {
      SOURCES = await api("GET", BASE + "/sources");
      $("rpSourceCount").textContent = String(SOURCES.length);
      $("rpKpiSources").textContent = String(SOURCES.length);
      page("rpSources", SOURCES, 10, renderSources, "rpSourcePager");
      fillSelect();
    } catch (e) { fail(e); }
  }

  function fillSelect() {
    const sel = $("rpOpSource"), cur = sel.value;
    sel.innerHTML = SOURCES.length
      ? SOURCES.map(s => '<option value="' + s.id + '">' + esc(s.name) + "</option>").join("")
      : '<option value="">（请先配置来源）</option>';
    if (cur && SOURCES.some(s => String(s.id) === cur)) sel.value = cur;
  }

  function formPayload() {
    return {
      name: $("rp_name").value.trim(), provider: $("rp_provider").value,
      endpoint: $("rp_endpoint").value.trim(),
      port: parseInt($("rp_port").value, 10) || null,
      region: $("rp_region").value.trim(),
      access_key: $("rp_access_key").value.trim(),
      secret_key: $("rp_secret_key").value,
      bucket: $("rp_bucket").value.trim(), prefix: $("rp_prefix").value.trim(),
      interval_minutes: parseInt($("rp_interval").value, 10) || 30,
      secure: $("rp_secure").checked, verify_ssl: $("rp_verify_ssl").checked,
      enabled: $("rp_enabled").checked, delete_after_pull: $("rp_delete_after").checked,
      remark: $("rp_remark").value.trim(),
    };
  }

  function openModal(s) {
    $("rpModalTitle").textContent = s ? "编辑拉取来源 #" + s.id : "新增拉取来源";
    $("rp_id").value = s ? s.id : "";
    $("rp_name").value = s ? s.name : "";
    $("rp_provider").value = s ? (s.provider || "minio") : "minio";
    $("rp_endpoint").value = s ? (s.endpoint || "") : "";
    $("rp_port").value = s ? (s.port || "") : "";
    $("rp_region").value = s ? (s.region || "us-east-1") : "us-east-1";
    $("rp_access_key").value = s ? (s.access_key || "") : "";
    $("rp_secret_key").value = "";
    $("rp_bucket").value = s ? (s.bucket || "") : "";
    $("rp_prefix").value = s ? (s.prefix || "") : "";
    $("rp_interval").value = s ? (s.interval_minutes || 30) : 30;
    $("rp_secure").checked = s ? bool(s.secure) : false;
    $("rp_verify_ssl").checked = s ? bool(s.verify_ssl) : false;
    $("rp_enabled").checked = s ? bool(s.enabled) : true;
    $("rp_delete_after").checked = s ? bool(s.delete_after_pull) : false;
    $("rp_remark").value = s ? (s.remark || "") : "";
    modal("rpSourceModal").show();
  }

  async function saveSource() {
    const p = formPayload(), id = $("rp_id").value;
    if (!p.name || !p.endpoint || !p.bucket) { toast("名称 / 端点 / 桶名必填", "danger"); return; }
    try {
      if (id) await api("PUT", BASE + "/sources/" + id, p);
      else await api("POST", BASE + "/sources", p);
      modal("rpSourceModal").hide();
      toast("拉取来源已保存");
      await loadSources();
    } catch (e) { fail(e); }
  }

  function showProbe(out) {
    const rows = [
      ["结论", out.ok ? badge("出站可达", "bg-success") : badge("失败", "bg-danger")],
      ["目标桶", esc(out.bucket || "-")],
      ["说明", esc(out.message || out.error || "-")],
      ["桶是否存在", out.bucket_exists === undefined ? "-" :
        (out.bucket_exists ? badge("存在", "bg-success") : badge("不存在", "bg-warning"))],
      ["时间", esc(out.checked_at || "-")],
    ];
    if (out.warning) rows.push(["提示", '<span class="text-warning">' + esc(out.warning) + "</span>"]);
    $("rpProbeBody").innerHTML = '<table class="table table-sm mb-0"><tbody>' +
      rows.map(r => "<tr><th style='width:120px'>" + r[0] + "</th><td>" + r[1] + "</td></tr>").join("") +
      "</tbody></table>";
    modal("rpProbeModal").show();
  }

  async function testSource(id, unsaved) {
    try {
      const out = unsaved ? await api("POST", BASE + "/test", unsaved)
        : await api("POST", BASE + "/sources/" + id + "/test", {});
      showProbe(out);
      if (!unsaved) await loadSources();
    } catch (e) {
      if (e && e.data && e.data.ok === false) showProbe(e.data);
      else fail(e);
    }
  }

  async function delSource(id) {
    if (!window.confirm("确定删除该拉取来源？已登记的异地副本记录不受影响。")) return;
    try { await api("DELETE", BASE + "/sources/" + id); toast("已删除"); await loadSources(); }
    catch (e) { fail(e); }
  }

  // ------------------------- 拉取 / 推送 -------------------------
  function showResult(html) { $("rpResult").innerHTML = html; }

  async function preview(id, silent) {
    try {
      const out = await api("GET", BASE + "/sources/" + id + "/preview?limit=50");
      if (silent) return out;
      const rows = (out.pending || []).map(p =>
        "<li><code>" + esc(p.package_key) + "</code></li>").join("");
      showResult('<div class="alert alert-light border py-2 mb-0"><strong>待拉取 ' +
        out.count + " 个包</strong>（已处理跳过 " + out.skipped_done + " 个）" +
        (rows ? "<ul class='mb-0 small'>" + rows + "</ul>" : "") +
        "<div class='text-muted'>仅列举对象，未下载、未登记。</div></div>");
      return out;
    } catch (e) { if (!silent) fail(e); return null; }
  }

  async function pull(id) {
    try {
      const out = await api("POST", BASE + "/sources/" + id + "/pull", { limit: 50 });
      showResult('<div class="alert ' + (out.failed ? "alert-warning" : "alert-success") +
        ' py-2 mb-0">发现 ' + out.found + " 个包：成功登记 <strong>" + out.pulled +
        "</strong> 条，跳过已处理 " + out.skipped + " 个，失败 " + out.failed + " 个。" +
        (out.record_ids && out.record_ids.length
          ? "<div class='small'>新记录 ID：" + esc(out.record_ids.join(", ")) + "</div>" : "") +
        ((out.details || []).filter(d => d.status === "failed").length
          ? "<div class='small text-danger'>" + esc(out.details.filter(d => d.status === "failed")
            .map(d => d.key + ": " + d.message).join("；").slice(0, 400)) + "</div>" : "") +
        "</div>");
      toast("拉取完成");
      await loadLogs();
    } catch (e) { fail(e); }
  }

  async function pushRecord() {
    const id = $("rpOpSource").value, rid = parseInt($("rpRecordId").value, 10);
    if (!id || !rid) { toast("请选择来源并填写备份记录 ID", "danger"); return; }
    try {
      const out = await api("POST", BASE + "/sources/" + id + "/push/" + rid, {});
      showResult('<div class="alert alert-success py-2 mb-0">已推送到远端：' +
        "<div class='small'><code>" + esc(out.package_key) + "</code></div>" +
        "<div class='small'><code>" + esc(out.manifest_key) + "</code></div>" +
        "<div class='small text-muted'>SHA256 " + esc(out.sha256.slice(0, 16)) +
        "… · " + esc(humanSize(out.size_bytes)) + "；另一端用「立即拉取并登记」接收。</div></div>");
      toast("云侧推送完成");
    } catch (e) { fail(e); }
  }

  // ------------------------- 日志 / 场景 -------------------------
  function renderLogs(rows, startNo) {
    const tb = $("rpLogTable");
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="7" class="text-muted">暂无拉取记录</td></tr>';
      return;
    }
    tb.innerHTML = rows.map((l, i) =>
      "<tr><td>" + (startNo + i) + "</td><td>#" + esc(l.source_id) + "</td>" +
      "<td><code>" + esc(String(l.object_key || "").slice(-56)) + "</code></td>" +
      "<td>" + (l.status === "success" ? badge("已登记", "bg-success") : badge("失败", "bg-danger")) + "</td>" +
      "<td>" + esc(l.message || "-") + "</td>" +
      "<td>" + (l.record_id ? "#" + esc(l.record_id) : "-") + "</td>" +
      "<td>" + fmt(l.created_at) + "</td></tr>").join("");
  }

  async function loadLogs() {
    try {
      LOGS = await api("GET", BASE + "/logs?limit=200");
      $("rpKpiPulled").textContent = String(LOGS.filter(l => l.status === "success").length);
      $("rpKpiFailed").textContent = String(LOGS.filter(l => l.status === "failed").length);
      $("rpKpiLast").textContent = LOGS.length ? fmt(LOGS[0].created_at) : "-";
      page("rpLogs", LOGS, 10, renderLogs, "rpLogPager");
    } catch (e) { fail(e); }
  }

  async function loadScenarios() {
    try {
      const s = await api("GET", BASE + "/scenarios");
      $("rpScenarios").innerHTML =
        '<div class="small mb-2"><strong>链路：</strong><code>' + esc(s.route) + "</code></div>" +
        '<div class="row g-3"><div class="col-md-6"><strong class="small">前提条件</strong><ul class="small text-muted mb-0">' +
        (s.requires || []).map(x => "<li>" + esc(x) + "</li>").join("") + "</ul></div>" +
        '<div class="col-md-6"><strong class="small">不承诺</strong><ul class="small text-muted mb-0">' +
        (s.not_promised || []).map(x => "<li>" + esc(x) + "</li>").join("") + "</ul></div></div>" +
        '<div class="alert alert-light border mt-3 mb-0 small">' + esc(s.offline || "") +
        "；本通道 <strong>不是 P2P 反向隧道</strong>。</div>";
    } catch (e) {
      $("rpScenarios").innerHTML = '<div class="text-danger small">' + esc(errMsg(e)) + "</div>";
    }
  }

  // ------------------------- 绑定 -------------------------
  function bind() {
    $("rpRefreshBtn").addEventListener("click", () => { loadSources(); loadLogs(); });
    $("rpNewBtn").addEventListener("click", () => openModal(null));
    $("rpSaveBtn").addEventListener("click", saveSource);
    $("rpTestFormBtn").addEventListener("click", () => {
      const p = formPayload();
      if (!p.endpoint) { toast("请先填写端点", "danger"); return; }
      if ($("rp_id").value && !p.secret_key) testSource(parseInt($("rp_id").value, 10));
      else testSource(null, p);
    });
    $("rpSourceTable").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      const id = parseInt(b.dataset.id, 10), act = b.dataset.act;
      const s = SOURCES.find(x => x.id === id);
      if (act === "edit") openModal(s);
      else if (act === "test") testSource(id);
      else if (act === "del") delSource(id);
      else if (act === "preview") { $("rpOpSource").value = String(id); preview(id); }
      else if (act === "pull") { $("rpOpSource").value = String(id); pull(id); }
    });
    $("rpPreviewBtn").addEventListener("click", () => {
      const id = $("rpOpSource").value;
      if (id) preview(id); else toast("请先选择来源", "danger");
    });
    $("rpPullBtn").addEventListener("click", () => {
      const id = $("rpOpSource").value;
      if (id) pull(id); else toast("请先选择来源", "danger");
    });
    $("rpPushBtn").addEventListener("click", pushRecord);
  }

  function init() {
    if (!$("rpSourceTable")) return;
    bind(); loadSources(); loadLogs(); loadScenarios();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
