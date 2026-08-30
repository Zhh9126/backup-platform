// -*- coding: utf-8 -*-
// 数据对比页面逻辑：KPI、任务管理、对比报告、立即对比、报告明细
// 独立 IIFE：需自行引用 BKP 核心工具
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const { api, esc, toast, fmtTime, fmtDuration, statusBadge } = BKP;

  const cronZh = function (expr) {
    const s = (expr || "").trim();
    if (!s) return "—";
    const p = s.split(/\s+/);
    if (p.length !== 5) return esc(s);
    const [minute, hour, dom, month, dow] = p;
    const WEEK = { "0": "日", "7": "日", "1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六" };
    const pad2 = (n) => String(n).padStart(2, "0");
    const hm = (h, m) => pad2(h) + ":" + pad2(m);
    if (dom === "*" && month === "*" && dow === "*" && /^\d+$/.test(hour) && /^\d+$/.test(minute))
      return "每天 " + hm(hour, minute) + " 触发";
    if (dom === "*" && month === "*" && dow === "*" && hour === "*" && minute.indexOf("/") === 0)
      return "每 " + minute.slice(1) + " 分钟";
    if (dom === "*" && month === "*" && dow === "*" && hour.indexOf("/") === 0 && minute === "0")
      return "每 " + hour.slice(1) + " 小时";
    if (dom === "*" && month === "*" && /^\d+$/.test(dow) && /^\d+$/.test(hour) && /^\d+$/.test(minute))
      return "每周" + (WEEK[dow] || dow) + " " + hm(hour, minute) + " 触发";
    return "定时：" + esc(s);
  };

  const scheduleLabel = function (t) {
    const type = t.schedule_type;
    if (type === "manual" || !type) return "手动";
    if (type === "cron") return cronZh(t.cron_expr);
    if (type === "interval") return "每 " + (t.interval_minutes || "?") + " 分钟";
    return "—";
  };

  const enableBadge = (enabled) => enabled
    ? '<span class="badge badge-ok">启用</span>'
    : '<span class="badge bg-secondary">停用</span>';

  const lastStatusBadge = function (s) {
    if (!s) return '<span class="badge bg-secondary">未运行</span>';
    return statusBadge(s);
  };

  const dbTypeZh = (t) => ({ mysql: "MySQL", mariadb: "MariaDB", postgresql: "PostgreSQL", kingbase: "金仓", oracle: "Oracle" }[t] || t || "-");
  const endpointLabel = function (t, side) {
    const type = dbTypeZh(t[side + "_db_type"]);
    return type + " " + esc(t[side + "_host"] || "-") + ":" + esc(t[side + "_port"] || "-") +
      "/" + esc(t[side + "_database"] || "-");
  };

  // ---- 主数据加载 ----
  let runningCount = 0;
  function loadStats() {
    return api("GET", "/api/data-compare-stats").then((res) => {
      if (!res.success) return;
      const d = res.data || {};
      $("statTaskCount").textContent = d.task_count || 0;
      $("statSuccessCount").textContent = d.success_count || 0;
      $("statFailedCount").textContent = d.failed_count || 0;
      $("statLastCompare").textContent = d.last_compare_at ? fmtTime(d.last_compare_at) : "—";
      runningCount = d.running_count || 0;
    });
  }

  function loadTasks() {
    return api("GET", "/api/data-compare-tasks").then((res) => {
      const tb = $("taskTable");
      if (!res.success || !res.data.length) {
        tb.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-3">暂无对比任务，点击右上角新建</td></tr>';
        return;
      }
      tb.innerHTML = res.data.map((t) => {
        const tables = Array.isArray(t.tables) && t.tables.length
          ? t.tables.length + " 张指定表" : "两端共有表";
        const running = t.last_status === "running";
        return '<tr>' +
          '<td class="fw-bold">' + esc(t.name) + '</td>' +
          '<td class="small">' + endpointLabel(t, "source") + '</td>' +
          '<td class="small">' + endpointLabel(t, "target") + '</td>' +
          '<td class="small">' + esc(tables) +
            (t.enable_checksum ? '<div><span class="badge bg-info bg-opacity-10 text-info">含校验和</span></div>' : '') + '</td>' +
          '<td class="small">' + esc(scheduleLabel(t)) + '</td>' +
          '<td>' + enableBadge(t.enabled) + '</td>' +
          '<td>' + (t.last_run_at ? fmtTime(t.last_run_at) + "<br>" + lastStatusBadge(t.last_status) : '<span class="text-muted">—</span>') + '</td>' +
          '<td class="text-end">' +
            '<button class="btn btn-sm btn-outline-primary me-1" onclick="runCompare(' + t.id + ')"' + (running ? ' disabled' : '') + '><i class="bi bi-play-fill"></i> 对比</button>' +
            '<button class="btn btn-sm btn-outline-secondary me-1" onclick="openTaskModal(' + t.id + ')"><i class="bi bi-pencil"></i></button>' +
            '<button class="btn btn-sm btn-outline-danger" onclick="deleteTask(' + t.id + ')"><i class="bi bi-trash"></i></button>' +
          '</td></tr>';
      }).join("");
    });
  }

  function loadReports() {
    return api("GET", "/api/data-compare-reports").then((res) => {
      const tb = $("reportTable");
      if (!res.success || !res.data.length) {
        tb.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-3">暂无对比报告</td></tr>';
        return;
      }
      tb.innerHTML = res.data.map((r) => {
        const s = (typeof r.summary_json === "object" && r.summary_json) || {};
        const counts = (s.tables_total || 0) + " / " +
          '<span class="text-success">' + (s.tables_matched || 0) + '</span> / ' +
          '<span class="text-danger">' + (s.tables_mismatched || 0) + '</span> / ' +
          '<span class="text-warning">' + (s.tables_failed || 0) + '</span>';
        return '<tr>' +
          '<td class="fw-bold">' + esc(r.task_name || "任务 " + r.task_id) + '</td>' +
          '<td>' + statusBadge(r.status) + '</td>' +
          '<td>' + counts + '</td>' +
          '<td>' + fmtDuration(r.duration_sec) + '</td>' +
          '<td>' + (r.created_at ? fmtTime(r.created_at) : "-") + '</td>' +
          '<td class="text-end"><button class="btn btn-sm btn-outline-primary" onclick="showReport(' + r.id + ')"><i class="bi bi-eye"></i> 明细</button></td>' +
          '</tr>';
      }).join("");
    });
  }

  function refreshAll() {
    Promise.all([loadStats(), loadTasks(), loadReports()]).catch(() => toast("加载失败", "danger"));
  }

  // ---- 模态框 ----
  function toggleScheduleInputs() {
    const type = $("dc_schedule_type").value;
    $("dcCronInput").style.display = type === "cron" ? "" : "none";
    $("dcIntervalInput").style.display = type === "interval" ? "" : "none";
  }

  function openTaskModal(id) {
    const form = $("dcTaskForm");
    form.reset();
    $("dc_id").value = "";
    $("dcTaskModalTitle").textContent = "新建对比任务";
    $("dc_sample_rows").value = 100;
    $("dc_enabled").checked = true;
    toggleScheduleInputs();

    if (id) {
      $("dcTaskModalTitle").textContent = "编辑对比任务";
      api("GET", "/api/data-compare-tasks/" + id).then((res) => {
        if (!res.success) { toast("加载任务失败", "danger"); return; }
        const t = res.data;
        $("dc_id").value = t.id;
        $("dc_name").value = t.name || "";
        $("dc_tables").value = Array.isArray(t.tables) ? t.tables.join(",") : "";
        $("dc_sample_rows").value = t.sample_rows || 100;
        $("dc_enable_checksum").checked = !!t.enable_checksum;
        ["source", "target"].forEach((side) => {
          $("dc_" + side + "_db_type").value = t[side + "_db_type"] || "mysql";
          $("dc_" + side + "_host").value = t[side + "_host"] || "";
          $("dc_" + side + "_port").value = t[side + "_port"] || "";
          $("dc_" + side + "_username").value = t[side + "_username"] || "";
          $("dc_" + side + "_password").value = "";
          $("dc_" + side + "_database").value = t[side + "_database"] || "";
          $("dc_" + side + "_schema").value = t[side + "_schema"] || "";
        });
        $("dc_schedule_type").value = t.schedule_type || "manual";
        $("dc_cron_expr").value = t.cron_expr || "";
        $("dc_interval_minutes").value = t.interval_minutes || "";
        $("dc_enabled").checked = !!t.enabled;
        toggleScheduleInputs();
      });
    }
    new bootstrap.Modal($("dcTaskModal")).show();
  }

  function saveTask() {
    const id = $("dc_id").value;
    const tables = $("dc_tables").value.split(",").map((s) => s.trim()).filter(Boolean);
    const payload = {
      name: $("dc_name").value.trim(),
      tables: tables,
      sample_rows: parseInt($("dc_sample_rows").value, 10) || 0,
      enable_checksum: $("dc_enable_checksum").checked,
      schedule_type: $("dc_schedule_type").value,
      cron_expr: $("dc_schedule_type").value === "cron" ? $("dc_cron_expr").value.trim() : "",
      interval_minutes: $("dc_schedule_type").value === "interval" ? (parseInt($("dc_interval_minutes").value, 10) || null) : null,
      enabled: $("dc_enabled").checked,
    };
    ["source", "target"].forEach((side) => {
      payload[side + "_db_type"] = $("dc_" + side + "_db_type").value;
      payload[side + "_host"] = $("dc_" + side + "_host").value.trim();
      payload[side + "_port"] = parseInt($("dc_" + side + "_port").value, 10) || null;
      payload[side + "_username"] = $("dc_" + side + "_username").value.trim();
      const pwd = $("dc_" + side + "_password").value;
      if (pwd) payload[side + "_password"] = pwd;
      payload[side + "_database"] = $("dc_" + side + "_database").value.trim();
      payload[side + "_schema"] = $("dc_" + side + "_schema").value.trim();
    });
    if (!payload.source_host || !payload.target_host) {
      toast("请填写源/目标主机", "danger"); return;
    }

    const btn = $("dcSubmitBtn");
    btn.disabled = true;
    const done = id
      ? api("PUT", "/api/data-compare-tasks/" + id, payload)
      : api("POST", "/api/data-compare-tasks", payload);
    done.then((res) => {
      if (res.success) {
        toast(id ? "已更新" : "已创建", "success");
        bootstrap.Modal.getInstance($("dcTaskModal")).hide();
        refreshAll();
      } else {
        toast(res.message || "保存失败", "danger");
      }
    }).catch(() => toast("保存失败", "danger"))
      .finally(() => { btn.disabled = false; });
  }

  // ---- 立即对比（后台执行 + 轮询） ----
  function runCompare(id) {
    toast("对比任务已启动，后台执行中...", "info");
    api("POST", "/api/data-compare-tasks/" + id + "/run").then((res) => {
      if (!res.success) { toast(res.message || "启动失败", "danger"); return; }
      let polls = 0;
      const timer = setInterval(() => {
        polls += 1;
        api("GET", "/api/data-compare-tasks/" + id + "/reports?limit=1").then((r) => {
          const rep = (r.data && r.data[0]) || {};
          if (rep.status !== "running" || polls > 150) {
            clearInterval(timer);
            toast(rep.status === "success" ? "对比完成：数据一致"
              : "对比完成：" + (rep.message || "发现差异或失败"),
              rep.status === "success" ? "success" : "danger");
            refreshAll();
          }
        });
      }, 2000);
      refreshAll();
    }).catch(() => toast("启动异常", "danger"));
  }

  // ---- 报告明细 ----
  const rowStr = (row) => !row ? "（无此行）" : "[" + row.map((v) => esc(v)).join(", ") + "]";
  function showReport(id) {
    api("GET", "/api/data-compare-reports/" + id).then((res) => {
      if (!res.success) { toast("加载报告失败", "danger"); return; }
      const r = res.data;
      const s = r.summary_json || {};
      const tables = r.tables_json || [];
      const body = $("dcReportBody");
      body.innerHTML =
        '<div class="mb-2"><span class="badge ' + (r.status === "success" ? "badge-ok" : "bg-danger") + '">'
        + esc(r.status) + '</span> <span class="text-muted ms-2">'
        + esc(r.message || "") + '</span></div>' +
        '<div class="row g-2 mb-3 text-center">' +
        '<div class="col"><div class="border rounded py-2"><div class="fs-5 fw-bold">' + (s.tables_total || 0) + '</div><div class="small text-muted">对比表数</div></div></div>' +
        '<div class="col"><div class="border rounded py-2 text-success"><div class="fs-5 fw-bold">' + (s.tables_matched || 0) + '</div><div class="small text-muted">一致</div></div></div>' +
        '<div class="col"><div class="border rounded py-2 text-danger"><div class="fs-5 fw-bold">' + (s.tables_mismatched || 0) + '</div><div class="small text-muted">不一致</div></div></div>' +
        '<div class="col"><div class="border rounded py-2 text-warning"><div class="fs-5 fw-bold">' + (s.tables_failed || 0) + '</div><div class="small text-muted">失败</div></div></div>' +
        '<div class="col"><div class="border rounded py-2"><div class="fs-5 fw-bold">' + fmtDuration(r.duration_sec) + '</div><div class="small text-muted">耗时</div></div></div>' +
        '</div>' +
        '<table class="table table-sm table-bordered align-middle mb-0"><thead><tr>' +
        '<th>表</th><th>源行数</th><th>目标行数</th><th>行数</th><th>校验和</th><th>抽样差异</th><th>说明</th>' +
        '</tr></thead><tbody>' +
        tables.map((t) => {
          const ck = t.checksum_match === null ? "—" :
            (t.checksum_match ? '<span class="text-success">一致</span>'
              : '<span class="text-danger">不一致</span>');
          const badge = t.status === "match"
            ? '<span class="badge badge-ok">一致</span>'
            : (t.status === "mismatch" ? '<span class="badge bg-danger">不一致</span>'
              : '<span class="badge bg-warning text-dark">失败</span>');
          let detail = esc(t.message || "");
          if (t.sample_diffs && t.sample_diffs.length) {
            detail += '<details class="mt-1"><summary class="text-primary small">差异明细（前 20）</summary><div class="small">' +
              t.sample_diffs.map((d, i) =>
                '<div class="text-break">#' + (i + 1) + ' 源: ' + rowStr(d.source) +
                ' / 目标: ' + rowStr(d.target) + '</div>').join("") + '</div></details>';
          }
          return '<tr' + (t.status === "mismatch" ? ' class="table-danger"' :
            (t.status === "failed" ? ' class="table-warning"' : '')) + '>' +
            '<td class="fw-bold">' + esc(t.table) + '</td>' +
            '<td>' + (t.source_rows === null ? "—" : t.source_rows) + '</td>' +
            '<td>' + (t.target_rows === null ? "—" : t.target_rows) + '</td>' +
            '<td>' + (t.rows_match === null ? "—" : (t.rows_match
              ? '<span class="text-success">一致</span>' : '<span class="text-danger">不一致</span>')) + '</td>' +
            '<td>' + ck + '</td>' +
            '<td>' + (t.sample_diff_count || 0) + '</td>' +
            '<td>' + badge + '<div class="small text-muted">' + detail + '</div></td>' +
            '</tr>';
        }).join("") +
        '</tbody></table>';
      new bootstrap.Modal($("dcReportModal")).show();
    }).catch(() => toast("加载报告异常", "danger"));
  }

  // ---- 删除 ----
  function deleteTask(id) {
    if (!confirm("确认删除该对比任务及其全部报告？")) return;
    api("DELETE", "/api/data-compare-tasks/" + id).then((res) => {
      toast(res.success ? "已删除" : "删除失败", res.success ? "success" : "danger");
      refreshAll();
    }).catch(() => toast("删除失败", "danger"));
  }

  window.refreshAll = refreshAll;
  window.openTaskModal = openTaskModal;
  window.saveTask = saveTask;
  window.runCompare = runCompare;
  window.showReport = showReport;
  window.deleteTask = deleteTask;

  document.addEventListener("DOMContentLoaded", function () {
    const st = $("dc_schedule_type");
    if (st) st.addEventListener("change", toggleScheduleInputs);
    refreshAll();
  });
})();
