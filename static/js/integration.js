/* 接口对接中心（2026-11-25）
 *
 * 页面职责：对接任务 CRUD / 未保存即可测试 / 立即执行 / 采集日志查询与导出。
 * 依赖：BKP.api（bkp-core.js）、BKP.esc、BKP.toast、Pager（pager.js）。
 */
(function () {
  "use strict";
  var IT = {};
  var state = {
    tasks: [],
    editing: null,      // 当前编辑的任务 id（null=新建）
    logTaskId: null,
    logPage: 1,
    logSize: 20,
  };

  function $(id) { return document.getElementById(id); }
  function esc(v) { return (window.BKP && BKP.esc) ? BKP.esc(v) : String(v == null ? "" : v); }
  function toast(msg, kind) {
    if (window.BKP && BKP.toast) BKP.toast(msg, kind || "success");
    else if (kind === "danger") alert(msg);
  }
  function api(method, url, body) { return BKP.api(method, url, body); }
  function v(id) { var el = $(id); return el ? String(el.value || "").trim() : ""; }
  function checked(id) { var el = $(id); return !!(el && el.checked); }
  function fmt(ts) { return ts ? String(ts).replace("T", " ").slice(0, 19) : "-"; }

  // ------------------------------------------------------------------ 加载
  IT.loadAll = async function () {
    await Promise.all([IT.loadTasks(), IT.loadRuns()]);
  };

  IT.loadTasks = async function () {
    var box = $("itTaskRows");
    try {
      var r = await api("GET", "/api/v1/integration/summary");
      state.tasks = r.tasks || [];
      var st = r.stats || {};
      $("it_stat_tasks").textContent = st.tasks || 0;
      $("it_stat_enabled").textContent = st.enabled || 0;
      $("it_stat_today").textContent = st.today_logs || 0;
      $("it_stat_alarms").textContent = st.alarms || 0;
      renderTasks();
      fillTaskSelect();
    } catch (e) {
      box.innerHTML = '<tr><td colspan="9" class="text-danger small">加载失败：' + esc(e.message) + "</td></tr>";
    }
  };

  function scheduleText(t) {
    if (t.schedule_type === "manual") return "仅手动";
    if (t.schedule_type === "interval") return "每 " + (t.interval_minutes || 0) + " 分钟";
    return t.cron || "-";
  }

  function renderTasks() {
    var box = $("itTaskRows");
    if (!state.tasks.length) {
      box.innerHTML = '<tr><td colspan="9" class="text-muted small">' +
        "还没有对接任务。点右上「新建对接任务」，先「测试」看返回再保存。</td></tr>";
      var pagerBox = $("itTaskPager");
      if (pagerBox) pagerBox.innerHTML = "";
      return;
    }
    var pg = Pager.slice("itTasks", state.tasks, 10);
    box.innerHTML = pg.list.map(function (t, i) {
      var dirBadge = t.direction === "push"
        ? '<span class="badge text-bg-warning">推送</span>'
        : '<span class="badge text-bg-info">拉取</span>';
      var statusBadge = t.last_status === "success"
        ? '<span class="badge text-bg-success">正常</span>'
        : (t.last_status === "failed"
          ? '<span class="badge text-bg-danger" title="' + esc(t.last_error) + '">失败</span>'
          : '<span class="badge text-bg-secondary">未运行</span>');
      if (!t.enabled) statusBadge += ' <span class="badge text-bg-secondary">停用</span>';
      return "<tr>" +
        "<td>" + (pg.startNo + i) + "</td>" +
        "<td><div class=\"fw-semibold\">" + esc(t.name) + "</div>" +
        (t.last_error ? '<div class="small text-danger text-truncate" style="max-width:320px" title="' +
          esc(t.last_error) + '">' + esc(t.last_error) + "</div>" : "") + "</td>" +
        "<td>" + dirBadge + "</td>" +
        '<td class="small text-break" style="max-width:330px">' + esc(t.endpoint) + "</td>" +
        '<td class="small">' + esc(scheduleText(t)) + "</td>" +
        "<td>" + statusBadge + "</td>" +
        '<td class="small">' + fmt(t.last_run_at) +
        (t.direction === "pull"
          ? '<div class="text-muted">抓 ' + (t.last_fetched || 0) + " / 入库 " + (t.last_accepted || 0) + "</div>"
          : "") + "</td>" +
        '<td class="small"><span class="text-success">' + (t.success_count || 0) +
        '</span> / <span class="text-danger">' + (t.fail_count || 0) + "</span></td>" +
        '<td class="text-end"><div class="btn-row">' +
        '<button class="btn btn-sm btn-outline-primary" onclick="IT.openTaskModal(' + t.id + ')">编辑</button>' +
        '<button class="btn btn-sm btn-outline-secondary" onclick="IT.testSaved(' + t.id + ')">测试</button>' +
        '<button class="btn btn-sm btn-outline-success" onclick="IT.runTask(' + t.id + ')">立即执行</button>' +
        '<button class="btn btn-sm btn-outline-info" onclick="IT.openLogs(' + t.id + ')">日志</button>' +
        '<button class="btn btn-sm btn-outline-danger" onclick="IT.delTask(' + t.id + ')">删除</button>' +
        "</div></td></tr>";
    }).join("");
    Pager.attach("itTasks", $("itTaskPager"), pg.page, pg.pages, pg.total, renderTasks);
  }

  function fillTaskSelect() {
    var sel = $("it_log_task");
    if (!sel) return;
    var cur = sel.value;
    sel.innerHTML = '<option value="">全部任务</option>' +
      state.tasks.map(function (t) {
        return '<option value="' + t.id + '">' + esc(t.name) + "</option>";
      }).join("");
    if (cur) sel.value = cur;
  }

  IT.loadRuns = async function () {
    var box = $("itRunRows");
    try {
      var r = await api("GET", "/api/v1/integration/runs");
      var rows = r.runs || [];
      if (!rows.length) {
        box.innerHTML = '<tr><td colspan="9" class="text-muted small">暂无运行记录</td></tr>';
        return;
      }
      box.innerHTML = rows.map(function (x, i) {
        var ok = x.status === "success";
        return "<tr>" +
          "<td>" + (i + 1) + "</td>" +
          '<td class="small">' + fmt(x.started_at) + "</td>" +
          "<td>" + esc(x.task_name || ("任务 " + x.task_id)) + "</td>" +
          '<td class="small">' + (x.direction === "push" ? "推送" : "拉取") + "</td>" +
          "<td>" + (ok ? '<span class="badge text-bg-success">成功</span>'
            : '<span class="badge text-bg-danger">失败</span>') + "</td>" +
          '<td class="small">' + (x.http_status || "-") + "</td>" +
          '<td class="small">' + (x.fetched || 0) + " / " + (x.accepted || 0) + " / " + (x.pushed || 0) + "</td>" +
          '<td class="small">' + (x.duration_ms || 0) + " ms</td>" +
          '<td class="small text-break">' + esc(x.message) + "</td>" +
          "</tr>";
      }).join("");
    } catch (e) {
      box.innerHTML = '<tr><td colspan="9" class="text-danger small">加载失败：' + esc(e.message) + "</td></tr>";
    }
  };

  // ------------------------------------------------------------------ 表单
  function setVal(id, value) { var el = $(id); if (el) el.value = value == null ? "" : value; }

  IT.onAuthChange = function () {
    var t = v("it_auth_type");
    $("it_auth_user_box").style.display = t === "basic" ? "" : "none";
    $("it_auth_header_box").style.display = t === "header" ? "" : "none";
    $("it_auth_secret_box").style.display = t === "none" ? "none" : "";
  };

  IT.onDirectionChange = function () {
    var push = v("it_direction") === "push";
    var path = $("it_json_path");
    if (push) {
      if (path) path.value = "";
    } else if (path && !path.value) {
      path.value = "$.data.logs[*]";
    }
  };

  IT.onScheduleChange = function () {
    var t = v("it_schedule_type");
    $("it_cron_box").style.display = t === "cron" ? "" : "none";
    $("it_interval_box").style.display = t === "interval" ? "" : "none";
  };

  IT.applyPreset = function () {
    var origin = window.location.origin;
    setVal("it_endpoint", origin + "/api/v1/integration/log-source?since={{since}}&limit={{limit}}");
    setVal("it_method", "GET");
    setVal("it_auth_type", "bearer");
    setVal("it_auth_secret", "");
    setVal("it_parse_type", "json");
    setVal("it_json_path", "$.data.logs[*]");
    setVal("it_field_map", "");
    setVal("it_include", "");
    setVal("it_exclude", "");
    setVal("it_max_items", "500");
    IT.onAuthChange();
    BKP.toast("已填入本平台日志接口示例：认证方式选 Bearer Token 并填 API Token 即可", "info");
  };

  IT.openTaskModal = async function (id) {
    state.editing = id || null;
    $("itTaskTitle").textContent = id ? "编辑对接任务" : "新建对接任务";
    var t = id ? (state.tasks.filter(function (x) { return x.id === id; })[0] || null) : null;
    if (id) {
      try { var r = await api("GET", "/api/v1/integration-tasks/" + id); t = r.task; } catch (e) { /* 用列表缓存 */ }
    }
    t = t || {};
    setVal("it_name", t.name || "");
    setVal("it_direction", t.direction || "pull");
    setVal("it_endpoint", t.endpoint || "");
    setVal("it_method", t.method || "GET");
    setVal("it_auth_type", t.auth_type || "none");
    setVal("it_auth_user", t.auth_user || "");
    setVal("it_auth_header", t.auth_header || "X-Api-Key");
    setVal("it_auth_secret", "");
    var hint = $("it_auth_secret_hint");
    if (hint) hint.textContent = t.has_secret ? "留空表示沿用已保存的密钥" : "该认证类型必填";
    setVal("it_headers", t.headers || "{}");
    setVal("it_body", t.body || "");
    setVal("it_timeout", t.timeout_sec || 20);
    $("it_verify_tls").checked = !!t.verify_tls;
    setVal("it_parse_type", t.parse_type || "json");
    setVal("it_json_path", t.json_path || "$.data.logs[*]");
    setVal("it_field_map", t.field_map || "");
    setVal("it_include", t.include_keywords || "");
    setVal("it_exclude", t.exclude_keywords || "");
    setVal("it_alarm", t.alarm_keywords || "");
    setVal("it_max_items", t.max_items || 500);
    setVal("it_schedule_type", t.schedule_type || "cron");
    setVal("it_cron", t.cron || "*/5 * * * *");
    setVal("it_interval", t.interval_minutes || 5);
    setVal("it_enabled", String(t.enabled === 0 ? 0 : 1));
    $("it_test_result").innerHTML = "";
    IT.onAuthChange();
    IT.onDirectionChange();
    IT.onScheduleChange();
    var modal = new bootstrap.Modal($("itTaskModal"));
    modal.show();
  };

  function collectForm() {
    var payload = {
      name: v("it_name"),
      direction: v("it_direction"),
      endpoint: v("it_endpoint"),
      method: v("it_method"),
      auth_type: v("it_auth_type"),
      auth_user: v("it_auth_user"),
      auth_header: v("it_auth_header"),
      headers: v("it_headers") || "{}",
      body: v("it_body"),
      timeout_sec: parseInt(v("it_timeout") || "20", 10),
      verify_tls: checked("it_verify_tls") ? 1 : 0,
      parse_type: v("it_parse_type"),
      json_path: v("it_json_path") || "$",
      field_map: v("it_field_map"),
      include_keywords: v("it_include"),
      exclude_keywords: v("it_exclude"),
      alarm_keywords: v("it_alarm"),
      max_items: parseInt(v("it_max_items") || "500", 10),
      schedule_type: v("it_schedule_type"),
      cron: v("it_cron"),
      interval_minutes: parseInt(v("it_interval") || "0", 10),
      enabled: parseInt(v("it_enabled") || "1", 10),
    };
    var secret = v("it_auth_secret");
    if (secret) payload.auth_secret = secret;
    if (state.editing) payload.id = state.editing;
    return payload;
  }

  IT.testCurrent = async function () {
    var box = $("it_test_result");
    var btn = $("itTestBtn");
    var payload = collectForm();
    if (!payload.endpoint) { BKP.toast("请先填写接口地址", "warning"); return; }
    btn.disabled = true;
    box.innerHTML = '<div class="text-muted small">正在请求对端接口…</div>';
    try {
      var r = await api("POST", "/api/v1/integration-tasks/test", payload);
      box.innerHTML = renderTestResult(r);
    } catch (e) {
      box.innerHTML = '<div class="alert alert-danger py-2 px-3 small mb-0">测试失败：' + esc(e.message) + "</div>";
    } finally {
      btn.disabled = false;
    }
  };

  function renderTestResult(r) {
    var head = r.ok
      ? '<span class="badge text-bg-success">测试通过</span>'
      : '<span class="badge text-bg-danger">测试失败</span>';
    var meta = [];
    if (r.http_status) meta.push("HTTP " + r.http_status);
    if (r.duration_ms != null) meta.push(r.duration_ms + " ms");
    if (r.fetched != null) meta.push("解析 " + r.fetched + " 条");
    if (r.matched != null) meta.push("过滤后 " + r.matched + " 条");
    if (r.filtered_out) meta.push("过滤掉 " + r.filtered_out + " 条");
    if (r.alarm_hits) meta.push("告警命中 " + r.alarm_hits + " 条");
    if (r.pushed != null && r.direction === "push") meta.push("推送 " + r.pushed + " 条");
    var html = '<div class="border rounded p-2 bg-light">' +
      '<div class="d-flex flex-wrap gap-2 align-items-center small">' + head +
      '<span class="text-muted">' + esc(meta.join(" · ")) + "</span></div>";
    if (r.url) html += '<div class="small text-muted text-break mt-1">实际请求：' + esc(r.url) + "</div>";
    if (r.error) html += '<div class="small text-danger mt-1">' + esc(r.error) + "</div>";
    if (r.hint) html += '<div class="small text-muted mt-1">' + esc(r.hint) + "</div>";
    if (r.samples && r.samples.length) {
      html += '<div class="small mt-2 fw-semibold">解析样例（前 ' + r.samples.length + " 条）</div>" +
        '<div class="table-responsive"><table class="table table-sm mb-0"><thead class="table-light"><tr>' +
        "<th style=\"width:150px\">时间</th><th style=\"width:80px\">级别</th>" +
        "<th style=\"width:120px\">来源</th><th>内容</th></tr></thead><tbody>" +
        r.samples.map(function (s) {
          return "<tr><td class=\"small\">" + esc(fmt(s.ts || "")) + "</td><td class=\"small\">" +
            esc(s.level || "") + "</td><td class=\"small\">" + esc(s.source || "") +
            "</td><td class=\"small text-break\">" + esc(s.message || "") + "</td></tr>";
        }).join("") + "</tbody></table></div>";
    }
    if (r.raw_preview) {
      html += '<details class="mt-2"><summary class="small">查看原始响应片段</summary>' +
        '<pre class="small mt-1 p-2 bg-white border rounded" style="max-height:240px;overflow:auto;white-space:pre-wrap">' +
        esc(r.raw_preview) + "</pre></details>";
    }
    return html + "</div>";
  }

  IT.saveTask = async function () {
    var payload = collectForm();
    try {
      if (state.editing) {
        await api("PUT", "/api/v1/integration-tasks/" + state.editing, payload);
        toast("已保存");
      } else {
        await api("POST", "/api/v1/integration-tasks", payload);
        toast("已创建");
      }
      var modal = bootstrap.Modal.getInstance($("itTaskModal"));
      if (modal) modal.hide();
      await IT.loadTasks();
    } catch (e) {
      toast("保存失败：" + e.message, "danger");
    }
  };

  // ------------------------------------------------------------------ 执行
  IT.runTask = async function (id) {
    if (!confirm("立即执行一次对接任务（真实请求对端接口）？")) return;
    toast("正在执行…", "info");
    try {
      var r = await api("POST", "/api/v1/integration-tasks/" + id + "/run");
      if (r.ok) {
        toast(r.direction === "push"
          ? ("推送成功 " + (r.pushed || 0) + " 条")
          : ("采集成功：抓取 " + (r.fetched || 0) + " 条，入库 " + (r.inserted || 0) + " 条"));
      } else {
        toast("执行失败：" + r.error, "danger");
      }
    } catch (e) {
      toast("执行失败：" + e.message, "danger");
    }
    await IT.loadTasks();
    await IT.loadRuns();
  };

  IT.testSaved = async function (id) {
    toast("正在测试…", "info");
    try {
      var r = await api("POST", "/api/v1/integration-tasks/test", { id: id });
      IT.openResultModal(r);
    } catch (e) {
      toast("测试失败：" + e.message, "danger");
    }
  };

  IT.openResultModal = function (r) {
    var body = $("itLogBody");
    $("itLogTitle").textContent = "接口测试结果";
    body.innerHTML = renderTestResult(r);
    new bootstrap.Modal($("itLogModal")).show();
  };

  IT.delTask = async function (id) {
    if (!confirm("删除该对接任务？采集到的日志也会一并删除。")) return;
    try {
      await api("DELETE", "/api/v1/integration-tasks/" + id);
      toast("已删除");
      await IT.loadTasks();
      IT.loadLogs(1);
    } catch (e) {
      toast("删除失败：" + e.message, "danger");
    }
  };

  // ------------------------------------------------------------------ 日志
  IT.loadLogs = async function (page) {
    state.logPage = page || state.logPage || 1;
    var box = $("itLogRows");
    var params = new URLSearchParams();
    var tid = $("it_log_task") ? $("it_log_task").value : "";
    if (tid) params.set("task_id", tid);
    var level = v("it_log_level");
    if (level) params.set("level", level);
    var q = v("it_log_q");
    if (q) params.set("q", q);
    if (checked("it_log_alerted")) params.set("alerted", "1");
    params.set("page", state.logPage);
    params.set("size", state.logSize);
    try {
      var r = await api("GET", "/api/v1/integration/logs?" + params.toString());
      var rows = r.logs || [];
      if (!rows.length) {
        box.innerHTML = '<tr><td colspan="6" class="text-muted small">' +
          "暂无采集日志（任务执行成功后会出现在这里）</td></tr>";
      } else {
        var startNo = (state.logPage - 1) * state.logSize + 1;
        box.innerHTML = rows.map(function (x, i) {
          var lv = (x.level || "").toUpperCase();
          var badge = lv === "ERROR" ? "text-bg-danger"
            : (lv === "WARN" ? "text-bg-warning" : "text-bg-secondary");
          return "<tr>" +
            "<td>" + (startNo + i) + "</td>" +
            '<td class="small">' + fmt(x.ts) + "</td>" +
            '<td><span class="badge ' + badge + '">' + esc(lv || "-") + "</span>" +
            (x.alerted ? ' <span class="badge text-bg-danger">告警</span>' : "") + "</td>" +
            '<td class="small">' + esc(x.task_name || "") + "</td>" +
            '<td class="small">' + esc(x.source || "") + "</td>" +
            '<td class="small text-break" title="' + esc(x.raw || "") + '">' + esc(x.message) + "</td>" +
            "</tr>";
        }).join("");
      }
      var total = r.total || 0;
      var pages = Math.max(1, Math.ceil(total / state.logSize));
      renderLogPager(pages, total);
    } catch (e) {
      box.innerHTML = '<tr><td colspan="6" class="text-danger small">加载失败：' + esc(e.message) + "</td></tr>";
    }
  };

  function renderLogPager(pages, total) {
    var box = $("itLogPager");
    if (!box) return;
    if (total <= state.logSize) {
      box.innerHTML = '<div class="small text-muted mt-2">共 ' + total + " 条</div>";
      return;
    }
    var html = '<div class="d-flex align-items-center gap-2 mt-2">' +
      '<button class="btn btn-sm btn-outline-secondary" ' +
      (state.logPage <= 1 ? "disabled" : "") +
      ' onclick="IT.loadLogs(' + (state.logPage - 1) + ')">上一页</button>' +
      '<span class="small">第 ' + state.logPage + " / " + pages + " 页 · 共 " + total + " 条</span>" +
      '<button class="btn btn-sm btn-outline-secondary" ' +
      (state.logPage >= pages ? "disabled" : "") +
      ' onclick="IT.loadLogs(' + (state.logPage + 1) + ')">下一页</button></div>';
    box.innerHTML = html;
  }

  IT.openLogs = function (id) {
    state.logTaskId = id;
    var sel = $("it_log_task");
    if (sel) sel.value = String(id);
    $("itLogTitle").textContent = "采集日志";
    $("itLogBody").innerHTML =
      '<div class="d-flex gap-2 mb-2">' +
      '<button class="btn btn-sm btn-outline-secondary" onclick="IT.reloadModalLogs()">' +
      '<i class="bi bi-arrow-clockwise"></i> 刷新</button></div>' +
      '<div class="table-responsive"><table class="table table-sm"><thead class="table-light"><tr>' +
      '<th style="width:52px">#</th><th style="width:160px">时间</th><th style="width:86px">级别</th>' +
      '<th style="width:120px">来源</th><th>内容</th></tr></thead><tbody id="itLogModalRows">' +
      '<tr><td colspan="5" class="text-muted small">加载中…</td></tr></tbody></table></div>';
    new bootstrap.Modal($("itLogModal")).show();
    IT.reloadModalLogs();
  };

  IT.reloadModalLogs = async function () {
    var tb = $("itLogModalRows");
    if (!tb) return;
    try {
      var r = await api("GET", "/api/v1/integration-tasks/" + state.logTaskId + "/logs?size=100");
      var rows = r.logs || [];
      if (!rows.length) {
        tb.innerHTML = '<tr><td colspan="5" class="text-muted small">暂无日志</td></tr>';
        return;
      }
      tb.innerHTML = rows.map(function (x, i) {
        return "<tr><td>" + (i + 1) + '</td><td class="small">' + fmt(x.ts) +
          '</td><td class="small">' + esc(x.level) + '</td><td class="small">' + esc(x.source || "") +
          '</td><td class="small text-break">' + esc(x.message) + "</td></tr>";
      }).join("");
    } catch (e) {
      tb.innerHTML = '<tr><td colspan="5" class="text-danger small">加载失败：' + esc(e.message) + "</td></tr>";
    }
  };

  IT.exportLogs = function () {
    var params = new URLSearchParams();
    var tid = $("it_log_task") ? $("it_log_task").value : "";
    if (tid) params.set("task_id", tid);
    var level = v("it_log_level");
    if (level) params.set("level", level);
    var q = v("it_log_q");
    if (q) params.set("q", q);
    if (checked("it_log_alerted")) params.set("alerted", "1");
    window.location.href = "/api/v1/integration/logs/export.csv?" + params.toString();
  };

  IT.openSourceHelp = function () {
    var url = window.location.origin + "/api/v1/integration/log-source?since={{since}}&limit={{limit}}";
    $("itHelpUrl").textContent = url;
    new bootstrap.Modal($("itHelpModal")).show();
  };

  // 标签页切换到日志/运行记录时按需加载
  document.addEventListener("DOMContentLoaded", function () {
    var tabLogs = document.querySelector('[data-bs-target="#tabItLogs"]');
    if (tabLogs) tabLogs.addEventListener("shown.bs.tab", function () { IT.loadLogs(1); });
    var tabRuns = document.querySelector('[data-bs-target="#tabItRuns"]');
    if (tabRuns) tabRuns.addEventListener("shown.bs.tab", function () { IT.loadRuns(); });
    IT.onAuthChange();
    IT.loadAll();
  });

  window.IT = IT;
})();
