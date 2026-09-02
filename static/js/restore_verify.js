// -*- coding: utf-8 -*-
// 恢复校验页面逻辑：KPI、策略管理、测试报告、立即校验、清理
// 独立 IIFE：需自行引用 BKP 核心工具
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const { api, esc, toast, fmtTime, fmtDuration, statusBadge } = BKP;

  // cron 表达式中文描述（与 app.js 同逻辑，独立文件自带）
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

  const scheduleLabel = function (p) {
    const type = p.schedule_type;
    if (type === "manual") return "手动";
    if (type === "cron") return cronZh(p.cron_expr);
    if (type === "interval") return "每 " + (p.interval_minutes || "?") + " 分钟";
    return "—";
  };

  const enableBadge = function (enabled) {
    return enabled
      ? '<span class="badge badge-ok">启用</span>'
      : '<span class="badge bg-secondary">停用</span>';
  };

  const lastStatusBadge = function (s) {
    if (!s) return '<span class="badge bg-secondary">未运行</span>';
    return statusBadge(s);
  };

  // ---- 主数据加载 ----
  function loadStats() {
    return api("GET", "/api/restore-verify-stats").then((res) => {
      if (!res.success) return;
      const d = res.data || {};
      $("statPolicyCount").textContent = d.policy_count || 0;
      $("statSuccessCount").textContent = d.success_count || 0;
      $("statFailedCount").textContent = d.failed_count || 0;
      $("statLastTest").textContent = d.last_test_at ? fmtTime(d.last_test_at) : "—";
    });
  }

  function loadPolicies() {
    return api("GET", "/api/restore-verify-policies").then((res) => {
      const tb = $("policyTable");
      if (!res.success || !res.data.length) {
        tb.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-3">暂无恢复校验策略，点击右上角新建</td></tr>';
        return;
      }
      tb.innerHTML = res.data.map((p) => {
        const inst = p.instance_name || p.task_name || "任务 " + p.task_id;
        return '<tr>' +
          '<td><div class="fw-bold">' + esc(inst) + '</div>' +
          '<div class="small text-muted">' + esc(p.db_type || "-") + '</div></td>' +
          '<td>' + esc(p.recovery_pool || "—") + '</td>' +
          '<td>' + esc(scheduleLabel(p)) + '</td>' +
          '<td>' + (p.clone_retention_min || 0) + '</td>' +
          '<td>' + enableBadge(p.enabled) + '</td>' +
          '<td>' + (p.last_run_at ? fmtTime(p.last_run_at) + "<br>" + lastStatusBadge(p.last_status) : '<span class="text-muted">—</span>') + '</td>' +
          '<td class="text-end">' +
            '<button class="btn btn-sm btn-outline-primary me-1" onclick="runVerify(' + p.id + ')"><i class="bi bi-play-fill"></i> 校验</button>' +
            '<button class="btn btn-sm btn-outline-secondary me-1" onclick="openPolicyModal(' + p.id + ')"><i class="bi bi-pencil"></i></button>' +
            '<button class="btn btn-sm btn-outline-danger" onclick="deletePolicy(' + p.id + ')"><i class="bi bi-trash"></i></button>' +
          '</td></tr>';
      }).join("");
    });
  }

  function loadReports() {
    return api("GET", "/api/restore-test-reports").then((res) => {
      const tb = $("reportTable");
      if (!res.success || !res.data.length) {
        tb.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-3">暂无恢复测试报告</td></tr>';
        return;
      }
      tb.innerHTML = res.data.map((r) => {
        const inst = r.instance_name || r.task_name || "任务 " + r.task_id;
        return '<tr>' +
          '<td><div class="fw-bold">' + esc(inst) + '</div>' +
          '<div class="small text-muted">' + esc(r.policy_name || "-") + '</div></td>' +
          '<td>' + statusBadge(r.status) + '</td>' +
          '<td>' + fmtDuration(r.duration_sec) + '</td>' +
          '<td>' + (r.cleaned ? '<span class="badge badge-ok">已清理</span>' : '<span class="badge bg-warning text-dark">待清理</span>') + '</td>' +
          '<td>' + (r.created_at ? fmtTime(r.created_at) : "-") + '</td>' +
          '<td class="text-break">' + esc(r.message || "-") +
            (r.cleaned ? "" : ' <button class="btn btn-sm btn-link p-0" onclick="cleanReport(' + r.id + ')">标记清理</button>') +
          '</td></tr>';
      }).join("");
    });
  }

  function refreshAll() {
    Promise.all([loadStats(), loadPolicies(), loadReports()]).catch(() => toast("加载失败", "danger"));
  }

  // ---- 任务下拉 ----
  let tasksCache = [];
  function loadTasks() {
    return api("GET", "/api/tasks").then((res) => {
      tasksCache = Array.isArray(res) ? res : (res.data || res.tasks || []);
      const sel = $("vp_task_id");
      sel.innerHTML = '<option value="">请选择备份任务</option>' +
        tasksCache.map((t) =>
          '<option value="' + t.id + '">' + esc((t.instance_name || t.name || "任务") +
          (t.db_type ? " (" + t.db_type + ")" : "")) + '</option>'
        ).join("");
    });
  }

  // ---- 模态框 ----
  function toggleScheduleInputs() {
    const type = $("vp_schedule_type").value;
    $("cronInput").style.display = type === "cron" ? "" : "none";
    $("intervalInput").style.display = type === "interval" ? "" : "none";
  }

  function openPolicyModal(id) {
    const form = $("verifyPolicyForm");
    form.reset();
    $("vp_id").value = "";
    $("verifyPolicyModalTitle").textContent = "新建校验策略";
    $("vp_clone_retention_min").value = 30;
    $("vp_enabled").checked = true;
    toggleScheduleInputs();

    const ready = tasksCache.length ? Promise.resolve() : loadTasks();
    ready.then(() => {
      if (id) {
        $("verifyPolicyModalTitle").textContent = "编辑校验策略";
        return api("GET", "/api/restore-verify-policies/" + id).then((res) => {
          if (!res.success) { toast("加载策略失败", "danger"); return; }
          const p = res.data;
          $("vp_id").value = p.id;
          $("vp_name").value = p.name || "";
          $("vp_task_id").value = p.task_id || "";
          $("vp_recovery_pool").value = p.recovery_pool || "";
          $("vp_schedule_type").value = p.schedule_type || "manual";
          $("vp_cron_expr").value = p.cron_expr || "";
          $("vp_interval_minutes").value = p.interval_minutes || "";
          $("vp_clone_retention_min").value = p.clone_retention_min || 30;
          $("vp_enabled").checked = !!p.enabled;
          toggleScheduleInputs();
        });
      }
    }).then(() => {
      const modal = new bootstrap.Modal($("verifyPolicyModal"));
      modal.show();
    });
  }

  function saveVerifyPolicy() {
    const id = $("vp_id").value;
    const payload = {
      name: $("vp_name").value.trim(),
      task_id: parseInt($("vp_task_id").value, 10) || 0,
      recovery_pool: $("vp_recovery_pool").value.trim(),
      schedule_type: $("vp_schedule_type").value,
      cron_expr: $("vp_schedule_type").value === "cron" ? $("vp_cron_expr").value.trim() : "",
      interval_minutes: $("vp_schedule_type").value === "interval" ? (parseInt($("vp_interval_minutes").value, 10) || null) : null,
      clone_retention_min: parseInt($("vp_clone_retention_min").value, 10) || 0,
      enabled: $("vp_enabled").checked,
    };
    if (!payload.task_id) { toast("请选择备份任务", "danger"); return; }

    const btn = $("vpSubmitBtn");
    btn.disabled = true;
    const done = id
      ? api("PUT", "/api/restore-verify-policies/" + id, payload)
      : api("POST", "/api/restore-verify-policies", payload);
    done.then((res) => {
      if (res.success) {
        toast(id ? "已更新" : "已创建", "success");
        bootstrap.Modal.getInstance($("verifyPolicyModal")).hide();
        refreshAll();
      } else {
        toast(res.message || "保存失败", "danger");
      }
    }).catch(() => toast("保存失败", "danger"))
      .finally(() => { btn.disabled = false; });
  }

  // ---- 立即校验 ----
  function runVerify(id) {
    toast("正在执行恢复校验...", "info");
    api("POST", "/api/restore-verify-policies/" + id + "/test").then((res) => {
      if (res.success) {
        const d = res.data || {};
        toast(d.success ? "校验通过" : "校验失败：" + (d.message || ""), d.success ? "success" : "danger");
      } else {
        toast(res.message || "校验失败", "danger");
      }
      refreshAll();
    }).catch(() => toast("校验执行异常", "danger"));
  }

  // ---- 删除 / 清理 ----
  function deletePolicy(id) {
    if (!confirm("确认删除该恢复校验策略及其测试报告？")) return;
    api("DELETE", "/api/restore-verify-policies/" + id).then((res) => {
      toast(res.success ? "已删除" : "删除失败", res.success ? "success" : "danger");
      refreshAll();
    }).catch(() => toast("删除失败", "danger"));
  }

  function cleanReport(id) {
    api("POST", "/api/restore-test-reports/" + id + "/clean").then((res) => {
      toast(res.success ? "已标记清理" : "失败", res.success ? "success" : "danger");
      loadReports();
    }).catch(() => toast("操作失败", "danger"));
  }

  // ---- 绑定到全局（模板内联 onclick 需要）----
  window.refreshAll = refreshAll;
  window.openPolicyModal = openPolicyModal;
  window.saveVerifyPolicy = saveVerifyPolicy;
  window.runVerify = runVerify;
  window.deletePolicy = deletePolicy;
  window.cleanReport = cleanReport;

  // ---- 初始化 ----
  document.addEventListener("DOMContentLoaded", function () {
    const st = $("vp_schedule_type");
    if (st) st.addEventListener("change", toggleScheduleInputs);
    loadTasks().catch(() => {}).finally(refreshAll);
  });
})();
