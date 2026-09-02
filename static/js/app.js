// -*- coding: utf-8 -*-
// 数据库备份管理平台 - 前端逻辑（原生 JS + Fetch + Bootstrap）
// UI 遵循 UI_DESIGN_SPEC：Slate + Teal 专业克制体系
// 依赖 bkp-core.js，核心工具函数通过全局 BKP 命名空间提供
(function () {
  "use strict";

  // ---- 引用 BKP 核心工具（已由 bkp-core.js 加载）----
  var $ = BKP.$;
  var $safe = BKP.$safe;
  var api = BKP.api;
  var esc = BKP.esc;
  var fmtTime = BKP.fmtTime;
  var fmtDuration = BKP.fmtDuration;
  var statusBadge = BKP.statusBadge;
  var toast = BKP.toast;
  var fillDbTypeSelect = BKP.fillDbTypeSelect;
  var META = BKP.META;
  var taskModal = null;

  // ---- 备份记录统一展示（四要素）：业务系统 + IP + 备份类型 + 备份时间 ----
  var fmtRecordLabel = function (r) {
    if (!r) return "-";
    var name = r.biz_label || "-";
    var ip = r.host_ip || "-";
    var dt = r.db_type_display || r.db_type || "-";
    var t = fmtTime(r.started_at);
    return esc(name) + " @ " + esc(ip) + " · " + esc(dt) + " · " + t;
  };

  // ---- 表格「业务系统」列：业务系统（带 #任务ID）+ IP 双行 ----
  var fmtBizCell = function (r) {
    if (!r) return "-";
    var name = r.biz_label || "-";
    var ip = r.host_ip || "-";
    return '<div class="fw-bold">' + esc(name) + '</div>' +
           '<div class="small text-muted">' + esc(ip) + '</div>';
  };

  // ---- cron 表达式 → 中文友好描述 ----
  // 支持 5 段：分 时 日 月 周
  var cronZh = function (expr) {
    var s = (expr || "").trim();
    if (!s) return "—";
    var parts = s.split(/\s+/);
    if (parts.length !== 5) return esc(s);
    var minute = parts[0], hour = parts[1], dom = parts[2], month = parts[3], dow = parts[4];
    var WEEK_MAP = { "0": "日", "7": "日", "1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六" };
    var pad2 = function (n) { n = String(n); return n.length < 2 ? "0" + n : n; };
    var hm = function (h, m) { return pad2(h) + ":" + pad2(m); };
    // 每天 HH:MM
    if (dom === "*" && month === "*" && dow === "*" && /^\d+$/.test(hour) && /^\d+$/.test(minute)) {
      return "每天 " + hm(hour, minute) + " 触发";
    }
    // 每 N 分钟
    if (dom === "*" && month === "*" && dow === "*" && hour === "*" && minute.indexOf("/") === 0) {
      return "每 " + minute.slice(1) + " 分钟";
    }
    // 每 N 小时（m=0）
    if (dom === "*" && month === "*" && dow === "*" && hour.indexOf("/") === 0 && minute === "0") {
      return "每 " + hour.slice(1) + " 小时";
    }
    // 每周某天 HH:MM
    if (dom === "*" && month === "*" && /^\d+$/.test(dow) && /^\d+$/.test(hour) && /^\d+$/.test(minute)) {
      return "每周" + (WEEK_MAP[dow] || dow) + " " + hm(hour, minute) + " 触发";
    }
    // 兜底：原样显示（仅中文说明去掉 cron 字符）
    return "定时：" + esc(s);
  };

  // ---- 调度列显示 ----
  var scheduleCell = function (t) {
    if (t.mixed_backup) {
      const full = t.full_schedule_type === "cron" ? "全 " + cronZh(t.full_schedule_expr)
        : t.full_schedule_type === "interval" ? "全 每" + (t.full_schedule_expr || "?") + "分"
        : "全 手动";
      const inc = t.incremental_schedule_type === "cron" ? "增 " + cronZh(t.incremental_schedule_expr)
        : t.incremental_schedule_type === "interval" ? "增 每" + (t.incremental_schedule_expr || "?") + "分"
        : (t.schedule_type === "cron" ? "增 " + cronZh(t.cron_expr)
          : t.schedule_type === "interval" ? "增 每" + (t.interval_minutes || "?") + "分"
          : "增 手动");
      return '<span class="badge bg-primary me-1">组合</span><small class="text-muted">' + esc(full + " / " + inc) + '</small>';
    }
    if (t.schedule_type === "cron") {
      var cronText = cronZh(t.cron_expr);
      // 空 cron 表达式：仅显示"定时"徽标，不显示占位 —
      if (cronText === "—") {
        return '<span class="badge bg-info">定时</span>';
      }
      return '<span class="badge bg-info me-1">定时</span>' +
        '<small class="text-muted">' + cronText + '</small>';
    }
    if (t.schedule_type === "interval") {
      return '<span class="badge bg-info me-1">定时</span>' +
        '<small class="text-muted">每 ' + (t.interval_minutes || "?") + ' 分钟</small>';
    }
    return '<span class="badge bg-secondary">手动</span>';
  };

  // 填充 SSH 主机下拉（数据库任务：优先远程执行 dump）
  const fillSshHostSelect = (sel, hosts = []) => {
    const opts = ['<option value="">— 自动（按数据库地址匹配已纳管主机；远端失败后回退本机）—</option>'];
    (hosts || []).forEach((h) => {
      const label = h.name ? `${esc(h.name)} (${esc(h.host_key)})` : esc(h.host_key);
      opts.push(`<option value="${h.id}">${label}</option>`);
    });
    sel.innerHTML = opts.join("");
  };

  // ------------------------- 二次确认弹窗（7.3） -------------------------
  let confirmModalInst = null;
  let confirmResolver = null;

  function confirmDialog({ title, message, confirmText = "确认", danger = true, warnIcon = true }) {
    return new Promise((resolve) => {
      confirmResolver = resolve;
      $("confirmTitle").innerHTML = (warnIcon ? '<i class="bi bi-exclamation-triangle-fill modal-warn-icon"></i> ' : "") + esc(title);
      $("confirmBody").textContent = message;
      const okBtn = $("confirmOk");
      okBtn.textContent = confirmText;
      okBtn.className = "btn " + (danger ? "btn-danger" : "btn-primary");
      if (!confirmModalInst) {
        const cmEl = document.getElementById("confirmModal");
        if (cmEl) confirmModalInst = new bootstrap.Modal(cmEl);
      }
      confirmModalInst.show();
    });
  }

  function bindConfirmModal() {
    const modal = $("confirmModal");
    if (!modal) return;
    $("confirmOk").onclick = () => {
      const r = confirmResolver; confirmResolver = null;
      confirmModalInst.hide();
      if (r) r(true);
    };
    $("confirmCancel").onclick = () => {
      const r = confirmResolver; confirmResolver = null;
      confirmModalInst.hide();
      if (r) r(false);
    };
    // 遮罩 / Esc 关闭视为取消
    modal.addEventListener("hidden.bs.modal", () => {
      if (confirmResolver) { const r = confirmResolver; confirmResolver = null; r(false); }
    });
  }

  // ------------------------- 侧边栏（7.1） -------------------------
  function bindSidebar() {
    const layout = $("layout");
    const toggle = $("sidebarToggle");
    if (!layout || !toggle) return;

    // 记忆折叠状态
    if (localStorage.getItem("sidebar-collapsed") === "1") {
      layout.classList.add("sidebar-collapsed");
    }
    toggle.onclick = () => {
      const collapsed = layout.classList.toggle("sidebar-collapsed");
      localStorage.setItem("sidebar-collapsed", collapsed ? "1" : "0");
    };
  }

  // ------------------------- 仪表盘 -------------------------
  // 仪表盘 4 维分项（与后端 _calc_health 输出顺序/标签一致）
  const HEALTH_KEYS = [
    { key: "任务覆盖",   max: 30, icon: "bi-list-check" },
    { key: "备份成功率", max: 40, icon: "bi-check2-circle" },
    { key: "调度完备",   max: 20, icon: "bi-calendar-event" },
    { key: "同步延迟",   max: 10, icon: "bi-arrow-repeat" },
  ];
  // 状态键 -> 颜色（与 badge-ok/badge-fail/badge-sim/badge-run 协调）
  const STATUS_COLOR = {
    success:   "#10b981",  // 绿
    simulated: "#3b82f6",  // 蓝
    running:   "#f59e0b",  // 黄
    failed:    "#ef4444",  // 红
    never:     "#94a3b8",  // 灰
  };

  function _parseHealthItem(line) {
    // 形如: "任务覆盖: 30/30 (11 个任务)"
    const m = String(line || "").match(/^([^:]+):\s*(\d+)\/(\d+)(?:\s*\((.+)\))?/);
    if (!m) return null;
    return { label: m[1].trim(), got: +m[2], max: +m[3], tail: m[4] || "" };
  }

  function _healthItemHtml(item) {
    const pct = item.max > 0 ? Math.round((item.got / item.max) * 100) : 0;
    const cls = pct >= 80 ? "hd-good" : pct >= 50 ? "hd-warn" : "hd-bad";
    const meta = item.max
      ? `<div class="hd-meta"><b>${item.got}<span class="text-muted" style="font-size:12px">/${item.max}</span></b>` +
        `<span class="text-muted">${pct}%</span></div>`
      : "";
    return `
      <div class="health-item">
        <div class="hd-label"><i class="bi ${item.icon || 'bi-bar-chart'}"></i>${esc(item.label)}${item.tail ? ` <span class="text-muted">(${esc(item.tail)})</span>` : ""}</div>
        <div class="hd-bar"><span class="${cls}" style="width:${pct}%"></span></div>
        ${meta}
      </div>`;
  }

  function _distRow(name, value, total, color) {
    const pct = total > 0 ? Math.round((value / total) * 100) : 0;
    return `
      <div class="dist-row">
        <span class="dr-name">${esc(name)}</span>
        <div class="dr-bar"><span style="width:${pct}%; background:${color}"></span></div>
        <span class="dr-val"><b>${value}</b> <span class="text-muted">(${pct}%)</span></span>
      </div>`;
  }

  async function initDashboard() {
    const d = await api("GET", "/api/dashboard");

    // 顶部 4 卡
    $("st_db_tasks").textContent = d.db_task_count ?? 0;
    $("st_file_tasks").textContent = d.file_task_count ?? 0;
    $("st_size").textContent = (d.total_size_human && d.total_size_human !== "0 B")
      ? d.total_size_human
      : ((d.total_size_gb != null ? d.total_size_gb.toFixed(2) : "0.00") + " GB");
    const health = d.health_score != null ? d.health_score : 0;
    const healthColor = health >= 80 ? "var(--success)" : health >= 50 ? "var(--warning)" : "var(--error)";
    $("st_health").innerHTML = `<span style="color:${healthColor}">${health}</span><span class="text-muted" style="font-size:14px">/100</span>`;

    // 存储池加密任务数（来自 dashboard 返回）
    $("st_encrypt_enabled").textContent = d.encrypt_pool_tasks != null ? d.encrypt_pool_tasks : "-";

    // 全局重删统计（参照白皮书 §2.4 全局重删）
    try {
      const ds = await api("GET", "/api/dedup/stats");
      const s = ds.stats || {};
      $("st_dedup_ratio").textContent = (s.dedup_ratio_pct != null ? s.dedup_ratio_pct : 0) + "%";
      $("st_dedup_saved").textContent = s.saved_bytes_human != null ? s.saved_bytes_human : "-";
    } catch (e) {
      $("st_dedup_ratio").textContent = "-";
      $("st_dedup_saved").textContent = "-";
    }

    // 健康详情（4 维分项进度条）
    const healthLines = (d.health_details || []).map(_parseHealthItem).filter(Boolean);
    const $hd = $("healthDetails");
    if (healthLines.length) {
      $hd.innerHTML = healthLines.map((it) => {
        // 根据 label 找 icon
        const conf = HEALTH_KEYS.find((k) => k.key === it.label);
        return _healthItemHtml(Object.assign({}, it, { icon: conf ? conf.icon : "bi-bar-chart" }));
      }).join("");
    } else {
      $hd.innerHTML = '<div class="text-muted">暂无健康分项数据</div>';
    }

    // 最近备份记录（中文展示 + 新列顺序）
    const recent = d.recent_records || [];
    $("recentRecords").innerHTML = recent.length
      ? recent.map((r) => {
          const mode = r.backup_mode_display || r.backup_mode || "-";
          const modeCls = r.backup_mode === "physical" ? "mode-chip physical" : "mode-chip";
          return `<tr>
            <td class="col-name" title="#${esc(r.task_id ?? "")}">${esc(r.task_name || "-")}</td>
            <td>${esc(r.biz_system || "-")}</td>
            <td><span class="badge bg-light text-dark border">${esc(r.backup_type_display || r.backup_type || "-")}</span></td>
            <td><code>${esc(r.host_ip || "-")}</code></td>
            <td><span class="${modeCls}">${esc(mode)}</span></td>
            <td>${esc(fmtDuration(r.duration_sec))}</td>
            <td>${esc(r.size_human || "-")}</td>
            <td>${statusBadge(r.status)}</td>
            <td class="col-time">${esc(fmtTime(r.started_at))}</td>
          </tr>`;
        }).join("")
      : '<tr><td colspan="9" class="text-muted text-center py-4">暂无备份记录</td></tr>';

    // 数据库类型分布（中文名 + 百分比条）
    const dbMap = d.db_counter_display || {};
    const dbTotal = Object.values(d.db_counter || {}).reduce((a, b) => a + b, 0) || 1;
    const dbEntries = Object.entries(dbMap);
    $("dbDist").innerHTML = dbEntries.length
      ? dbEntries.map(([k, v]) => _distRow(v, d.db_counter[k], dbTotal, "#3b82f6")).join("")
      : '<div class="dist-empty">暂无数据</div>';

    // 备份状态分布（中文名 + 颜色按状态）
    const stMap = d.status_counter_display || {};
    const stTotal = Object.values(d.status_counter || {}).reduce((a, b) => a + b, 0) || 1;
    const stEntries = Object.entries(stMap);
    $("statusDist").innerHTML = stEntries.length
      ? stEntries.map(([k, v]) => {
          // v 是中文展示名；原值在 d.status_counter[k]
          return _distRow(v, d.status_counter[k], stTotal, STATUS_COLOR[k] || "#94a3b8");
        }).join("")
      : '<div class="dist-empty">暂无数据</div>';
  }

  // ------------------------- 任务管理 -------------------------
  // 切换备份类型：按天调度区始终显示，根据 full/incremental/mixed 显示对应子区
  function refreshScheduleByDayBox() {
    const type = $("t_backup_type")?.value || "full";
    const box = $("t_mixed_schedule_box");
    const fullCol = $("t_full_schedule_col");
    const incCol = $("t_incremental_schedule_col");
    const tip = $("t_schedule_tip");
    if (box) {
      box.style.display = "";
      box.classList.remove("d-none");
    }
    if (fullCol) fullCol.classList.toggle("d-none", type === "incremental");
    if (incCol) incCol.classList.toggle("d-none", type === "full");
    if (tip) {
      if (type === "mixed") {
        tip.textContent = "组合备份：分别设置「全量」与「增量」的运行星期与时间。不勾选任何星期 = 该子任务不自动调度（可手动触发）。";
      } else if (type === "full") {
        tip.textContent = "全量备份：勾选运行星期并设置时间。不勾选任何星期 = 不自动调度（可手动触发）。";
      } else {
        tip.textContent = "增量备份：勾选运行星期并设置时间。不勾选任何星期 = 不自动调度（可手动触发）。";
      }
    }
  }

  function bindTaskFormEvents() {
    // 切换数据库类型时自动填默认端口
    const dbTypeEl = $("t_db_type");
    if (dbTypeEl) {
      dbTypeEl.onchange = () => {
        const p = META.default_ports[dbTypeEl.value];
        if (p) $("t_port").value = p;
      };
    }
    // 切换备份类型：按天调度区显隐由顶层 refreshScheduleByDayBox 处理
    const backupTypeEl = $("t_backup_type");
    if (backupTypeEl) {
      backupTypeEl.onchange = () => {
        refreshScheduleByDayBox();
        refreshDbPickerVisibility();
      };
      refreshScheduleByDayBox();
    }
  }

  // ---------- 组合调度：按天勾选 + 时间 ⇄ cron/days ----------
  function _collectDays(prefix) {
    // prefix: "t_full" / "t_inc" / "f_full" / "f_inc"
    // 读取对应星期 checkbox 容器（id 形如 t_full_days / f_inc_days），返回数字数组(0=周一)
    // 兼容 HTML 实际 ID（t_incremental_days 而非 t_inc_days）。
    // 完全防御式：所有可能返回 undefined 的调用都包 try/catch + 类型检查，
    // 避免任何边角情况让 _collectDays 抛出中断整个 saveTask。
    const candidates = [prefix + "_days", prefix + "_incremental_days", "t_incremental_days"];
    let box = null;
    for (let i = 0; i < candidates.length; i++) {
      box = $(candidates[i]);
      if (box) break;
    }
    if (!box || typeof box.querySelectorAll !== "function") return [];
    let nodes = null;
    try { nodes = box.querySelectorAll('input[type="checkbox"]'); }
    catch (_) { return []; }
    if (!nodes || typeof nodes.length !== "number") return [];
    const out = [];
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      if (n && n.checked) {
        const v = Number(n.value);
        if (!Number.isNaN(v)) out.push(v);
      }
    }
    return out.sort((a, b) => a - b);
  }

  function _buildMixedSub(prefix) {
    // 从「星期勾选 + 时间」生成子调度：type=cron, cron_expr="分 时 * * *", days="0,1,..."
    const days = _collectDays(prefix);
    const time = $(prefix + "_time")?.value || "02:00";
    const [hh, mm] = time.split(":").map((x) => x.padStart(2, "0"));
    if (!days.length) {
      return { type: "none", cron_expr: "", interval_minutes: null, days: "" };
    }
    return {
      type: "cron",
      cron_expr: `${mm} ${hh} * * *`,
      interval_minutes: null,
      days: days.join(","),
    };
  }

  function _fillMixedSub(prefix, task) {
    // 回填：把 task.full_schedule_days / full_schedule_expr 还原为星期勾选 + 时间
    const daysStr = (prefix === "t_full" ? task.full_schedule_days
      : prefix === "t_inc" ? task.incremental_schedule_days
      : prefix === "f_full" ? task.full_schedule_days
      : task.incremental_schedule_days) || "";
    const days = daysStr ? daysStr.split(",").map((s) => s.trim()).filter(Boolean) : [];
    const box = $(prefix + "_days");
    if (box) {
      box.querySelectorAll('input[type="checkbox"]').forEach((c) => {
        c.checked = days.includes(c.value);
      });
    }
    const expr = (prefix === "t_full" ? task.full_schedule_expr
      : prefix === "t_inc" ? task.incremental_schedule_expr
      : prefix === "f_full" ? task.full_schedule_expr
      : task.incremental_schedule_expr) || "2 2 * * *";
    // 从 cron_expr "分 时 * * *" 解析时间（兼容 5 段）
    const parts = expr.split(/\s+/);
    if (parts.length >= 2 && $(prefix + "_time")) {
      $(prefix + "_time").value = `${parts[1].padStart(2, "0")}:${parts[0].padStart(2, "0")}`;
    }
  }

  function openTaskModal(task) {
    if ($("taskForm")) $("taskForm").reset();
    $("t_password").value = "";
    if (task) {
      $("taskModalTitle").textContent = "编辑任务";
      $("t_id").value = task.id;
      // 预填展示值 biz_label（存量任务 biz_system 为空时后端已回退为任务名），
      // 使存量任务在必填约束下仍可直接保存（设计 D2 / D-3 死锁解除）
      $("t_biz_system").value = task.biz_label || "";
      $("t_name").value = task.name || "";
      $("t_db_type").value = task.db_type;
      $("t_host").value = task.host || "";
      $("t_port").value = task.port || "";
      $("t_username").value = task.username || "";
      $("t_db_name").value = task.db_name || "";
      $("t_backup_type").value = task.backup_type || "full";
      $("t_backup_mode").value = task.backup_mode || "logical";
      // 按天调度回填：单任务主 schedule 映射到对应子区；mixed 同时回填两套
      const bt = task.backup_type || "full";
      if (bt === "full") {
        const fake = Object.assign({}, task, { full_schedule_days: "", full_schedule_expr: task.cron_expr || "" });
        _fillMixedSub("t_full", fake);
      } else if (bt === "incremental") {
        const fake = Object.assign({}, task, { incremental_schedule_days: "", incremental_schedule_expr: task.cron_expr || "" });
        _fillMixedSub("t_inc", fake);
      } else {
        _fillMixedSub("t_full", task);
        _fillMixedSub("t_inc", task);
      }
      // 限速 / 压缩级别 / 保留天数 / 加密
      $("t_bandwidth_limit").value = task.bandwidth_limit ?? 0;
      $("t_compress_level").value = task.compress_level ?? 0;
      $("t_retention_days").value = task.retention_days ?? 30;
      $("t_encrypt_pwd").value = task.encrypt_pwd || "";
      $("t_extra_options").value = task.extra_options || "";
      $("t_enabled").checked = !!task.enabled;
      // 恢复 SSH 备份机选择
      try {
        const eo = JSON.parse(task.extra_options || "{}");
        if ($("t_ssh_host")) $("t_ssh_host").value = eo.ssh_host_id || "";
        if ($("t_encrypt_pool")) $("t_encrypt_pool").checked = !!eo.encrypt_pool;
        // 任务级 SSH 凭据回填（免纳管执行通道）
        fillSshCred(eo.ssh_cred, task);
        // 任务级工具路径回填（手动兜底）
        fillToolPath(task.extra_options, task);
        // 自定义脚本回填
        if ($("t_custom_script")) $("t_custom_script").value = eo.custom_script || "";
        if ($("t_custom_restore")) $("t_custom_restore").value = eo.custom_restore_script || "";
        if ($("t_custom_artifact_dir")) $("t_custom_artifact_dir").value = eo.custom_artifact_dir || "";
        if ($("t_custom_timeout")) $("t_custom_timeout").value = eo.custom_timeout || "";
        // 任务级环境变量回填（支持 dict 或字符串两种存储形态）
        if ($("t_env_vars")) {
          const ev = eo.env_vars;
          if (ev && typeof ev === "object") {
            $("t_env_vars").value = Object.entries(ev).map(([k, v]) => `${k}=${v}`).join("\n");
          } else {
            $("t_env_vars").value = ev || "";
          }
        }
        toggleCustomBox();
      } catch (e) {
        if ($("t_ssh_host")) $("t_ssh_host").value = "";
        if ($("t_encrypt_pool")) $("t_encrypt_pool").checked = false;
      }
    } else {
      $("taskModalTitle").textContent = "新建备份任务";
      $("t_id").value = "";
      $("t_backup_mode").value = "logical";
      const p = META.default_ports[$("t_db_type").value];
      if (p) $("t_port").value = p;
      if ($("t_encrypt_pool")) $("t_encrypt_pool").checked = false;
      // 重置自定义脚本字段
      if ($("t_custom_script")) $("t_custom_script").value = "";
      if ($("t_custom_restore")) $("t_custom_restore").value = "";
      if ($("t_custom_artifact_dir")) $("t_custom_artifact_dir").value = "";
      if ($("t_custom_timeout")) $("t_custom_timeout").value = "";
      if ($("t_env_vars")) $("t_env_vars").value = "";
      resetSshCred();
    }
    toggleCustomBox();
    // 数据库选择器（schema/table 多选）：mysql/mariadb/postgresql/kingbase/oracle/dameng 显示
    // 注：oracle/dameng 引擎 list_databases() 返回空（需用户手工指定 schema），
    //     故展示选择器但拉取结果为空属预期，不影响 CDC 守护配置。
    refreshDbPickerVisibility();
    // 若是编辑任务，加载 schemas/tables 复选
    if (task && task.id && ["mysql", "mariadb", "postgresql", "kingbase", "oracle", "dameng"].includes(task.db_type)) {
      loadPickerFromExtra(task.extra_options);
    } else {
      resetPicker();
    }
    // 切回第一个 tab（Bootstrap 5 标准用法）
    try {
      const tabsEl = $("taskTabs");
      if (tabsEl) {
        const firstTab = tabsEl.querySelector('a[data-bs-toggle="tab"]');
        if (firstTab && window.bootstrap && bootstrap.Tab) {
          bootstrap.Tab.getOrCreateInstance(firstTab).show();
        }
      }
    } catch (e) { /* ignore */ }
    // 根据备份类型切换按天调度区显示
    refreshScheduleByDayBox();
    // 防御：确保模态框实例存在（即使 init 阶段因故未创建也能自愈，避免 taskModal 为 null 时抛错）
    if (!taskModal) {
      const el = document.getElementById("taskModal");
      if (el && window.bootstrap && bootstrap.Modal) taskModal = new bootstrap.Modal(el);
    }
    if (taskModal) taskModal.show();
    else console.error("[openTaskModal] taskModal 实例缺失，无法弹出编辑框");
  }

  // 数据库选择器（schema/table 多选）：mysql/mariadb/postgresql/kingbase/oracle/dameng 显示
  // 与 STREAMABLE_ENGINES 保持一致（T06 信创库 CDC 补齐）。
  function refreshDbPickerVisibility() {
    const t = $("t_db_type")?.value;
    const visible = ["mysql", "mariadb", "postgresql", "kingbase", "oracle", "dameng"].includes(t);
    const div = $("t_db_picker");
    if (div) div.style.display = visible ? "" : "none";
  }

  // 重置 picker
  function resetPicker() {
    if ($("t_pick_list")) $("t_pick_list").innerHTML = "";
    if ($("t_pick_count")) $("t_pick_count").textContent = "0";
    if ($("t_pick_mode")) $("t_pick_mode").value = "single";
    if ($("t_schema_only")) $("t_schema_only").checked = false;
    if ($("t_data_only")) $("t_data_only").checked = false;
    if ($("t_include_sys")) $("t_include_sys").checked = false;
    if ($("t_db_picker_status")) $("t_db_picker_status").textContent = "点击按钮连接数据库并拉取";
  }

  // 从 extra_options JSON 加载 picker 状态
  function loadPickerFromExtra(extraStr) {
    resetPicker();
    if (!extraStr) return;
    let eo = {};
    try { eo = typeof extraStr === "string" ? JSON.parse(extraStr) : extraStr; } catch (e) { return; }
    if (eo.schemas && Array.isArray(eo.schemas) && eo.schemas.length) {
      $("t_pick_mode").value = "schemas";
    } else if (eo.tables && Array.isArray(eo.tables) && eo.tables.length) {
      $("t_pick_mode").value = "tables";
    } else if (eo.use_all_db || eo.all_databases) {
      $("t_pick_mode").value = "all";
    } else {
      $("t_pick_mode").value = "single";
    }
    $("t_schema_only").checked = !!eo.schema_only;
    $("t_data_only").checked = !!eo.data_only;
    if ($("t_include_sys")) $("t_include_sys").checked = !!eo.include_system_dbs;
  }

  // 拉取数据库列表
  async function fetchDatabases() {
    const id = $("t_id").value;
    const btn = $("t_db_picker_btn");
    const status = $("t_db_picker_status");
    if (!id) { status.textContent = "请先保存任务（需要 task_id 才能拉取）"; status.className = "form-control form-control-sm text-warning"; return; }
    btn.disabled = true;
    status.textContent = "连接中...";
    status.className = "form-control form-control-sm text-info";
    try {
      const r = await api("GET", `/api/tasks/${id}/list-databases`);
      const sel = $("t_pick_list");
      sel.innerHTML = (r.databases || []).map(d => `<option value="${esc(d)}">${esc(d)}</option>`).join("");
      const viaJdbc = !!r.via_jdbc;
      status.textContent = `拉取到 ${r.databases.length} 个${r.type === "schemas" ? "schema" : "数据库"}（已过滤系统库${viaJdbc ? "，经 JDBC 兜底连接" : ""}）`;
      status.className = "form-control form-control-sm text-success";
      // 若有 eo.schemas/tables，恢复选中
      try {
        const eo = JSON.parse($("t_extra_options").value || "{}");
        const want = new Set((eo.schemas || eo.tables || []));
        if (want.size) {
          Array.from(sel.options).forEach(o => { if (want.has(o.value)) o.selected = true; });
          updatePickCount();
        }
      } catch (e) { /* ignore */ }
    } catch (e) {
      status.textContent = "拉取失败: " + e.message;
      status.className = "form-control form-control-sm text-danger";
    } finally {
      btn.disabled = false;
    }
  }

  // JDBC 连接测试（不依赖 SSH/客户端，通过 JDBC 驱动直连）
  async function jdbcTestConnection() {
    const btn = $("t_jdbc_test_btn");
    const box = $("t_jdbc_test_result");
    const msg = $("t_jdbc_test_msg");
    if (!btn || !box || !msg) return;
    const body = {
      db_type: ($("t_db_type") || {}).value || "",
      host: ($("t_host") || {}).value || "",
      port: ($("t_port") || {}).value || "",
      username: ($("t_username") || {}).value || "",
      password: ($("t_password") || {}).value || "",
      db_name: ($("t_db_name") || {}).value || "",
    };
    box.classList.remove("d-none");
    if (!body.db_type || !body.host) {
      msg.className = "alert alert-warning small py-2 mb-0";
      msg.innerHTML = '<i class="bi bi-exclamation-triangle"></i> <span>请先填写数据库类型、主机地址（端口）后再测试。</span>';
      return;
    }
    btn.disabled = true;
    msg.className = "alert alert-info small py-2 mb-0";
    msg.innerHTML = '<i class="bi bi-plug"></i> <span>JDBC 连接测试中...</span>';
    try {
      const r = await api("POST", "/api/jdbc/test-connection", body);
      const ok = !!r.success;
      msg.className = `alert ${ok ? "alert-success" : "alert-danger"} small py-2 mb-0`;
      const icon = ok ? "bi-check-circle" : "bi-x-circle";
      const ms = r.info && r.info.latency_ms != null ? `（${r.info.latency_ms} ms）` : "";
      msg.innerHTML = `<i class="bi ${icon}"></i> <span>${esc(r.message || "")}${ms}</span>`;
    } catch (e) {
      msg.className = "alert alert-danger small py-2 mb-0";
      msg.innerHTML = `<i class="bi bi-x-circle"></i> <span>JDBC 测试请求失败: ${esc(e.message || e)}</span>`;
    } finally {
      btn.disabled = false;
    }
  }

  // 更新已选数
  function updatePickCount() {
    const sel = $("t_pick_list");
    const cnt = sel ? Array.from(sel.selectedOptions).length : 0;
    if ($("t_pick_count")) $("t_pick_count").textContent = cnt;
  }

  // 把 picker 状态写回 extra_options JSON
  function syncExtraFromPicker() {
    if (!$("t_pick_list") || !$("t_pick_list").options.length) return; // 没拉取过库就不动
    const sel = $("t_pick_list");
    const selected = Array.from(sel.selectedOptions).map(o => o.value);
    const mode = $("t_pick_mode")?.value || "single";
    // 解析现有 JSON
    let eo = {};
    try { eo = JSON.parse($("t_extra_options").value || "{}"); } catch (e) { eo = {}; }
    // 清掉旧字段
    delete eo.schemas; delete eo.tables; delete eo.use_all_db; delete eo.all_databases;
    if ($("t_schema_only").checked) eo.schema_only = true; else delete eo.schema_only;
    if ($("t_data_only").checked) eo.data_only = true; else delete eo.data_only;
    if ($("t_include_sys") && $("t_include_sys").checked) eo.include_system_dbs = true;
    else delete eo.include_system_dbs;
    if (mode === "schemas" && selected.length) eo.schemas = selected;
    else if (mode === "tables" && selected.length) eo.tables = selected;
    else if (mode === "all") eo.use_all_db = true;
    $("t_extra_options").value = JSON.stringify(eo, null, 2);
  }

  // 绑定 picker 按钮（db_picker 模块的事件）
  function bindDbPickerEvents() {
    const btn = $("t_db_picker_btn");
    if (btn && !btn._bound) {
      btn._bound = true;
      btn.onclick = fetchDatabases;
    }
    const jdbcBtn = $("t_jdbc_test_btn");
    if (jdbcBtn && !jdbcBtn._bound) {
      jdbcBtn._bound = true;
      jdbcBtn.onclick = jdbcTestConnection;
    }
    const allBtn = $("t_pick_all");
    if (allBtn && !allBtn._bound) {
      allBtn._bound = true;
      allBtn.onclick = () => {
        const sel = $("t_pick_list");
        if (sel) Array.from(sel.options).forEach(o => o.selected = true);
        updatePickCount();
        syncExtraFromPicker();
      };
    }
    const noneBtn = $("t_pick_none");
    if (noneBtn && !noneBtn._bound) {
      noneBtn._bound = true;
      noneBtn.onclick = () => {
        const sel = $("t_pick_list");
        if (sel) Array.from(sel.options).forEach(o => o.selected = false);
        updatePickCount();
        syncExtraFromPicker();
      };
    }
    const listSel = $("t_pick_list");
    if (listSel && !listSel._bound) {
      listSel._bound = true;
      listSel.onchange = () => { updatePickCount(); syncExtraFromPicker(); };
    }
    const modeSel = $("t_pick_mode");
    if (modeSel && !modeSel._bound) {
      modeSel._bound = true;
      modeSel.onchange = () => syncExtraFromPicker();
    }
    const so = $("t_schema_only");
    if (so && !so._bound) { so._bound = true; so.onchange = () => syncExtraFromPicker(); }
    const dox = $("t_data_only");
    if (dox && !dox._bound) { dox._bound = true; dox.onchange = () => syncExtraFromPicker(); }
    const dbTypeEl = $("t_db_type");
    if (dbTypeEl && !dbTypeEl._pickerBound) {
      dbTypeEl._pickerBound = true;
      const orig = dbTypeEl.onchange;
      dbTypeEl.addEventListener("change", () => refreshDbPickerVisibility());
    }
  }

  // 自定义脚本区块显隐（备份方式 = custom 时展示）
  function toggleCustomBox() {
    const box = $("t_custom_box");
    const sel = $("t_backup_mode");
    if (box && sel) {
      const isCustom = sel.value === "custom";
      box.style.display = isCustom ? "" : "none";
      // 选中「自定义脚本」时定位到脚本编辑区，避免用户找不到输入框
      if (isCustom && box.offsetParent !== null) {
        box.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    }
    // SSH 通道开关绑定（一次性）
    const sc = $("t_ssh_same");
    if (sc && !sc._bound) { sc._bound = true; sc.onchange = toggleSshFields; }
  }
  window.toggleCustomBox = toggleCustomBox;

  // ===================================================================
  // 任务级 SSH 执行通道（免纳管）：凭据写入 extra_options.ssh_cred
  // ===================================================================
  function toggleSshFields() {
    const chkEl = $("t_ssh_same");
    const box = $("t_ssh_fields");
    if (chkEl && box) box.style.display = chkEl.checked ? "" : "none";
  }
  window.toggleSshFields = toggleSshFields;

  function resetSshCred() {
    if ($("t_ssh_same")) $("t_ssh_same").checked = false;
    if ($("t_ssh_addr")) $("t_ssh_addr").value = "";
    if ($("t_ssh_port")) $("t_ssh_port").value = 22;
    if ($("t_ssh_user")) $("t_ssh_user").value = "";
    if ($("t_ssh_pwd")) $("t_ssh_pwd").value = "";
    if ($("t_ssh_state")) $("t_ssh_state").textContent = "";
    if ($("t_tool_path")) $("t_tool_path").value = "";
    toggleSshFields();
  }

  // 任务级工具路径：编辑回填（extra_options.tool_path）
  function fillToolPath(extraOptionsStr) {
    let eo = {};
    try {
      eo = (typeof extraOptionsStr === "string")
        ? JSON.parse(extraOptionsStr || "{}")
        : (extraOptionsStr || {});
    } catch (e) { eo = {}; }
    if ($("t_tool_path")) $("t_tool_path").value = eo.tool_path || "";
  }

  // 任务级工具路径：保存收集（填写则写入 extra_options.tool_path）
  function collectToolPath(eo) {
    const v = ($("t_tool_path") && $("t_tool_path").value.trim()) || "";
    if (v) eo.tool_path = v;
    else delete eo.tool_path;
  }

  function fillSshCred(sc, task) {
    resetSshCred();
    if (!sc) return;
    if ($("t_ssh_same")) $("t_ssh_same").checked = true;
    // 地址与数据库同机时留空（保存时自动带出任务 host）
    const sameHost = !sc.host || sc.host === (task && task.host || "");
    if ($("t_ssh_addr") && !sameHost) $("t_ssh_addr").value = sc.host;
    if ($("t_ssh_port")) $("t_ssh_port").value = sc.port || 22;
    if ($("t_ssh_user")) $("t_ssh_user").value = sc.username || "";
    if ($("t_ssh_state")) {
      $("t_ssh_state").textContent = sc._enc
        ? "已保存加密凭据（密码留空 = 不修改）" : "";
    }
    toggleSshFields();
  }

  function collectSshCred(eo) {
    if (!$("t_ssh_same")) return;
    if ($("t_ssh_same").checked) {
      const prev = eo.ssh_cred || {};
      const pwd = ($("t_ssh_pwd") && $("t_ssh_pwd").value) || "";
      const addr = ($("t_ssh_addr") && $("t_ssh_addr").value.trim())
        || val("t_host") || "";
      eo.ssh_cred = {
        host: addr,
        port: num($("t_ssh_port") ? $("t_ssh_port").value : 22) || 22,
        username: ($("t_ssh_user") && $("t_ssh_user").value.trim()) || "root",
        // 密码留空且之前已加密保存 → 原样保留密文（API 层按 _enc 识别）
        password: pwd || (prev._enc ? prev.password : ""),
      };
      if (!pwd && prev._enc) eo.ssh_cred._enc = 1;
    } else if (eo.ssh_cred) {
      delete eo.ssh_cred;
    }
  }

  async function saveTask() {
    // 全面防御：编辑/新建时，模态框的某些字段可能不在当前 tab/不存在，
    // 任何 $(id) 为 null 都会让 .value 抛 TypeError，导致点保存完全无反应。
    // 这里把整个函数包进 try/catch（错误用 toast 显示，不再静默），
    // 并对所有字段读取做 null 安全。涉及类型 / 业务系统 / 任务名等核心
    // 字段若缺失仍在校验处拦截。
    try {
      const val = (id, fb = "") => {
        const e = $(id);
        return (e && e.value != null) ? e.value : fb;
      };
      const chk = (id) => {
        const e = $(id);
        return !!(e && e.checked);
      };
      const num = (v) => (v === "" || v == null ? null : Number(v));
      const id = val("t_id");
      const backupType = val("t_backup_type", "full");
      const mixed = backupType === "mixed";
      const fullSched = _buildMixedSub("t_full") || { type: "none", cron_expr: "", days: "" };
      const incSched  = _buildMixedSub("t_inc")  || { type: "none", cron_expr: "", days: "" };
      let schedule_type = "none", cron_expr = "", interval_minutes = null;
      if (backupType === "full") {
        schedule_type = fullSched.type || "none";
        cron_expr = fullSched.cron_expr || "";
      } else if (backupType === "incremental") {
        schedule_type = incSched.type || "none";
        cron_expr = incSched.cron_expr || "";
      }
      let eo = {};
      try { eo = JSON.parse(val("t_extra_options", "{}")); } catch (_) { eo = {}; }
      collectSshCred(eo);
      collectToolPath(eo);
      const sshId = val("t_ssh_host");
      if (sshId) eo.ssh_host_id = Number(sshId); else delete eo.ssh_host_id;
      if (chk("t_encrypt_pool")) eo.encrypt_pool = true; else delete eo.encrypt_pool;
      // 自定义脚本（备份方式 = custom 时必填脚本内容）
      const customMode = val("t_backup_mode") === "custom";
      const customScript = ($("t_custom_script") && $("t_custom_script").value) || "";
      if (customMode && !customScript.trim()) {
        toast("自定义脚本模式必须填写备份脚本", "danger"); return;
      }
      if (customScript.trim()) {
        eo.custom_script = customScript;
        if ($("t_custom_restore") && $("t_custom_restore").value.trim())
          eo.custom_restore_script = $("t_custom_restore").value;
        else delete eo.custom_restore_script;
        if ($("t_custom_artifact_dir") && $("t_custom_artifact_dir").value.trim())
          eo.custom_artifact_dir = $("t_custom_artifact_dir").value.trim();
        else delete eo.custom_artifact_dir;
        if ($("t_custom_timeout") && Number($("t_custom_timeout").value))
          eo.custom_timeout = Number($("t_custom_timeout").value);
        else delete eo.custom_timeout;
      } else {
        delete eo.custom_script;
        delete eo.custom_restore_script;
        delete eo.custom_artifact_dir;
        delete eo.custom_timeout;
      }
      // 任务级环境变量（所有数据库类型通用；KEY=VALUE 每行一条）
      const envRaw = ($("t_env_vars") && $("t_env_vars").value) || "";
      const envLines = envRaw.split(/\n|;/).map(s => s.trim())
        .filter(s => s && !s.startsWith("#") && s.includes("="));
      if (envLines.length) eo.env_vars = envLines.join("\n");
      else delete eo.env_vars;
      const data = {
        name: val("t_name"),
        biz_system: val("t_biz_system").trim(),
        db_type: val("t_db_type"),
        host: val("t_host"),
        port: num(val("t_port")),
        username: val("t_username"),
        password: val("t_password"),
        db_name: val("t_db_name"),
        backup_type: backupType,
        backup_mode: val("t_backup_mode") || "logical",
        schedule_type: schedule_type,
        cron_expr: cron_expr,
        interval_minutes: interval_minutes,
        mixed_backup: mixed ? 1 : 0,
        full_schedule_type: mixed ? (fullSched.type || "none") : "none",
        full_schedule_expr: mixed ? (fullSched.cron_expr || "") : "",
        full_schedule_days: mixed ? (fullSched.days || "") : "",
        incremental_schedule_type: mixed ? (incSched.type || "none") : "none",
        incremental_schedule_expr: mixed ? (incSched.cron_expr || "") : "",
        incremental_schedule_days: mixed ? (incSched.days || "") : "",
        bandwidth_limit: num(val("t_bandwidth_limit")) || 0,
        compress_level: num(val("t_compress_level")) || 0,
        retention_days: Number(val("t_retention_days") || 30),
        encrypt_pwd: val("t_encrypt_pwd"),
        extra_options: JSON.stringify(eo),
        enabled: chk("t_enabled") ? 1 : 0,
      };
      if (!data.biz_system) { toast("请填写业务系统", "danger"); return; }
      if (!data.name.trim()) { toast("请填写任务名称", "danger"); return; }
      if (id) { await api("PUT", `/api/tasks/${id}`, data); toast("任务已更新"); }
      else    { await api("POST", "/api/tasks", data); toast("任务已创建"); }
      if (taskModal) taskModal.hide();
      // 注意：刷新列表的失败不应覆盖"已保存"的成功事实。
      // 否则用户明明看到"任务已创建"却立即被"保存失败"覆盖，
      // 而且 DB 里任务其实已经在了。这是误导性错误，必须分离。
      try { await loadTasks(); }
      catch (e) { console.warn("[saveTask] 刷新列表失败（任务已保存）:", e); }
    } catch (e) {
      console.error("[saveTask]", e);
      toast("保存失败: " + (e && e.message ? e.message : e), "danger");
    }
  }

  async function loadTasks() {
    const tasks = (await api("GET", "/api/tasks?db_type_exclude=file")) || [];
    $("taskTable").innerHTML = tasks.map((t) =>
      `<tr>
        <td>${t.id}</td>
        <td>${esc(t.biz_label || "-")}</td>
        <td>${esc(t.name)}</td>
        <td>${esc(t.db_display_name || t.db_type)}</td>
        <td><code>${esc(t.host || "-")}</code></td>
        <td>${esc(t.port || "-")}/${esc(t.db_name || "")}</td>
        <td>${esc(t.backup_type_display || t.backup_type)}</td>
        <td>${esc(t.backup_mode_display || (t.backup_mode === "physical" ? "物理备份" : "逻辑备份"))}</td>
        <td>${scheduleCell(t)}</td>
        <td>${t.enabled ? statusBadge(t.last_status || "never") : '<span class="badge bg-secondary">已停用</span>'}</td>
        <td>${fmtTime(t.last_run_at) || "-"}</td>
        <td class="text-end">
          <button class="btn btn-sm btn-outline-success" onclick="runTask(${t.id})">备份</button>
          <button class="btn btn-sm btn-outline-primary" onclick="editTask(${t.id})">编辑</button>
          <button class="btn btn-sm btn-outline-danger" onclick="delTask(${t.id})">删除</button>
        </td>
      </tr>`).join("") ||
      '<tr><td colspan="11" class="text-muted text-center">暂无任务，点击右上角“新建任务”</td></tr>';
  }

  window.runTask = async (id) => {
    const ok = await confirmDialog({
      title: "确认立即备份？",
      message: "将对该任务执行一次手动备份，确认继续？",
      confirmText: "立即备份",
      danger: false,
    });
    if (!ok) return;
    try {
      const r = await api("POST", `/api/tasks/${id}/run`);
      toast("备份完成：" + r.status);
      await loadTasks();
    } catch (e) { toast(e.message, "danger"); }
  };
  window.editTask = async (id) => {
    const t = await api("GET", `/api/tasks/${id}`);
    openTaskModal(t);
  };
  window.delTask = async (id) => {
    const ok = await confirmDialog({
      title: "确认删除该任务？",
      message: "将删除该任务及其全部备份记录，此操作不可恢复。",
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try { await api("DELETE", `/api/tasks/${id}`); toast("已删除"); await loadTasks(); }
    catch (e) { toast(e.message, "danger"); }
  };

  async function initTasks() {
    // 全面防御：任何元素都可能为 null，全部加保护
    try {
      const dbTypeSel = $("t_db_type");
      if (dbTypeSel) fillDbTypeSelect(dbTypeSel, ["file"]);
      bindTaskFormEvents();
      bindDbPickerEvents();  // 绑定库选择器按钮
      const modalEl = $("taskModal");
      if (modalEl) taskModal = new bootstrap.Modal(modalEl);
      const saveBtn = $("taskSaveBtn");
      if (saveBtn) saveBtn.onclick = saveTask;
      const newBtn = $("newTaskBtn");
      if (newBtn) newBtn.onclick = () => openTaskModal(null);
      try { window.DB_HOSTS = await api("GET", "/api/hosts"); } catch (e) { window.DB_HOSTS = []; }
      if ($("t_ssh_host")) fillSshHostSelect($("t_ssh_host"), window.DB_HOSTS);
    } catch (e) {
      console.error("[initTasks] 错误:", e);
      const box = document.getElementById("jsErrorBox");
      if (box) {
        box.style.display = "block";
        box.textContent = "⚠ initTasks 错误: " + e.message;
      }
    }
    await loadTasks();
  }
  // 页面级刷新
  window.refreshTasks = () => { loadTasks(); };

  // 批量导入 CSV
  window.importTasksCsv = () => {
    const inp = document.createElement("input");
    inp.type = "file"; inp.accept = ".csv";
    inp.onchange = async () => {
      if (!inp.files[0]) return;
      const fd = new FormData(); fd.append("file", inp.files[0]);
      try {
        const res = await fetch("/api/tasks/import", { method: "POST", body: fd });
        const d = await res.json();
        toast(`导入完成：新增 ${d.created} 条，跳过 ${d.skipped} 条${d.errors.length ? "，错误 " + d.errors.length + " 条" : ""}`, d.errors.length ? "warning" : "success");
        if (d.errors.length) console.warn("导入错误:", d.errors);
        await loadTasks();
      } catch (e) { toast("导入失败: " + e.message, "danger"); }
    };
    inp.click();
  };

  // ------------------------- 备份记录 -------------------------
  async function loadRecords() {
    const tid = $("filterTask").value;
    const kw = ($("recordSearch")?.value || "").trim();
    const urlParams = new URLSearchParams(location.search);
    const pid = urlParams.get("policy_id");
    const q = [];
    if (tid) q.push("task_id=" + encodeURIComponent(tid));
    if (kw) q.push("keyword=" + encodeURIComponent(kw));
    if (pid) q.push("policy_id=" + encodeURIComponent(pid));
    const url = "/api/records" + (q.length ? "?" + q.join("&") : "");
    const recs = await api("GET", url);
    $("recordTable").innerHTML = recs.map((r) => {
      // CDC 基线（binlog / wal_lsn）
      let cdc = '-';
      if (r.binlog_file && r.binlog_pos) cdc = `<code class="small">${esc(r.binlog_file)}:${r.binlog_pos}</code>`;
      else if (r.wal_lsn) cdc = `<code class="small">${esc(r.wal_lsn)}</code>`;
      // 校验徽章
      const verifyBadge = r.verified ? '<span class="badge bg-success" title="已自动校验">校验✓</span>' : '';
      // 操作按钮（仅 success 状态可恢复）
      const canRestore = (r.status === "success" || r.status === "simulated");
      const dbType = r.db_type || '';
      // PITR 回放按钮仅对 MySQL/MariaDB/PostgreSQL 开放；kingbase/oracle/dameng 本期不支持
      const isMysqlFamily = dbType === 'mysql' || dbType === 'mariadb' || dbType === 'postgresql';
      const actions = r.backup_path ? [
        `<a class="btn btn-sm btn-outline-secondary" href="/api/records/${r.id}/download">下载</a>`,
      ] : [];
      if (r.status === "failed" || r.status === "error") {
        actions.unshift(`<button class="btn btn-sm btn-danger" onclick="viewRecordLog(${r.id})" title="查看失败详情"><i class="bi bi-exclamation-triangle"></i> 日志</button>`);
      } else if (r.message && r.message.length > 0) {
        actions.unshift(`<button class="btn btn-sm btn-outline-secondary" onclick="viewRecordLog(${r.id})" title="查看详细日志"><i class="bi bi-file-text"></i></button>`);
      }
      if (actions.length === 0) actions.push('<span class="text-muted">-</span>');
      if (canRestore && isMysqlFamily) {
        actions.push(`<button class="btn btn-sm btn-outline-primary" onclick="openPitrModal(${r.id})" title="任意时间点恢复">PITR</button>`);
        actions.push(`<button class="btn btn-sm btn-outline-info" onclick="openObjectModal(${r.id})" title="对象级精准恢复">对象</button>`);
      }
      return `<tr>
        <td>${r.id}</td>
        <td>${fmtBizCell(r)}</td>
        <td><code>${esc(r.host_ip || "-")}</code></td>
        <td>${esc(r.db_type_display || r.db_type || "-")}</td>
        <td>${fmtTime(r.started_at)}</td>
        <td>${esc(fmtDuration(r.duration_sec))}</td>
        <td>${statusBadge(r.status)} ${verifyBadge}</td>
        <td>${esc(r.size_human || "")}</td>
        <td class="text-truncate" style="max-width:150px" title="${esc(r.checksum || "")}">${esc((r.checksum || "").slice(0, 12) || "-")}</td>
        <td>${cdc}</td>
        <td class="text-end">${actions.join(' ')}</td>
      </tr>`;
    }).join("") ||
      '<tr><td colspan="10" class="text-muted text-center">暂无记录</td></tr>';
  }

  // ===== 备份日志查看 =====
  let recordLogModal = null;
  window.viewRecordLog = async (id) => {
    try {
      const r = await api("GET", `/api/records/${id}`);
      if (!r || r.error) { toast("记录不存在", "danger"); return; }
      $("rl_record_id").textContent = `#${r.id}`;
      $("rl_task").textContent = window._taskNames?.[r.task_id] || r.task_id;
      $("rl_backup_type").textContent = r.backup_type_display || r.backup_type || "-";
      $("rl_db_type").textContent = r.db_type_display || r.db_type || "-";
      $("rl_status").innerHTML = statusBadge(r.status);
      $("rl_size").textContent = r.size_human || "0 B";
      $("rl_started_at").textContent = fmtTime(r.started_at) || "-";
      $("rl_finished_at").textContent = fmtTime(r.finished_at) || "-";
      // 错误/结果信息（关键）：失败时显示红色标题
      const msg = r.message || "(无 message 字段)";
      const msgEl = $("rl_message");
      msgEl.textContent = msg;
      // 失败时给 message 区域加红色背景
      if (r.status === "failed" || r.status === "error") {
        msgEl.classList.add("border-danger");
        msgEl.style.backgroundColor = "#fef2f2";
      } else {
        msgEl.classList.remove("border-danger");
        msgEl.style.backgroundColor = "#f8f9fa";
      }
      $("rl_backup_path").textContent = r.backup_path || "(无)";
      $("rl_checksum").textContent = r.checksum || "-";
      // CDC 基线
      let cdc = "-";
      if (r.binlog_file && r.binlog_pos) cdc = `${r.binlog_file}:${r.binlog_pos}`;
      else if (r.wal_lsn) cdc = r.wal_lsn;
      $("rl_cdc").textContent = cdc;
      $("rl_duration").textContent = r.duration_sec ? fmtDuration(r.duration_sec) : "-";
      if (!recordLogModal) {
        recordLogModal = new bootstrap.Modal(document.getElementById("recordLogModal"));
      }
      recordLogModal.show();
    } catch (e) {
      toast("加载日志失败: " + e.message, "danger");
    }
  };

  // ---- PITR / 对象级 / 克隆 模态框 ----
  let pitrModal, objectModal, cloneModal;

  // 加载 SSH 主机到下拉框
  async function fillPitrObjectHostSelects() {
    let hosts = [];
    try { hosts = await api("GET", "/api/hosts"); } catch (e) { hosts = []; }
    const opts = '<option value="">— 选择已纳管 SSH 主机 —</option>' +
      hosts.map(h => `<option value="${esc(h.host_key)}">${esc(h.name || h.host_key)} (${esc(h.hostname)}:${esc(h.port || 22)})</option>`).join("");
    if ($("pitr_target_host")) $("pitr_target_host").innerHTML = opts;
    if ($("object_target_host")) $("object_target_host").innerHTML = opts;
  }
  window.fillPitrObjectHostSelects = fillPitrObjectHostSelects;

  // 解析目标主机：支持纳管主机 + 4 字段直接输入（IP/端口/用户/密码）
  function _resolveTargetHost(modalPrefix) {
    const sel = $(modalPrefix + "_target_host").value;
    const directHost = $(modalPrefix + "_direct_host") ? $(modalPrefix + "_direct_host").value.trim() : "";
    if (directHost) {
      // 直接输入模式：收集所有 4 个字段
      return {
        mode: "direct",
        host: directHost,
        port: Number($(modalPrefix + "_direct_port").value) || 22,
        user: $(modalPrefix + "_direct_user").value.trim() || "root",
        password: $(modalPrefix + "_direct_password").value || "",
      };
    }
    if (sel) return { mode: "managed", host_key: sel };
    return null;
  }

  window.openPitrModal = (id) => {
    $("pitr_record_id").value = id;
    $("pitr_target_time").value = new Date().toISOString().slice(0, 16);
    $("pitr_target_host").value = "";
    $("pitr_direct_host").value = ""; $("pitr_direct_port").value = "22";
    $("pitr_direct_user").value = "root"; $("pitr_direct_password").value = "";
    $("pitr_target_db").value = "";
    fillPitrObjectHostSelects();
    pitrModal.show();
  };

  window.openObjectModal = (id) => {
    $("object_record_id").value = id;
    $("object_name").value = "";
    $("object_target_host").value = "";
    $("object_direct_host").value = ""; $("object_direct_port").value = "22";
    $("object_direct_user").value = "root"; $("object_direct_password").value = "";
    $("object_target_db").value = "";
    fillPitrObjectHostSelects();
    objectModal.show();
  };

  $("pitrAddHostBtn").onclick = () => { window.open("/file_backup", "_blank"); };
  $("objectAddHostBtn").onclick = () => { window.open("/file_backup", "_blank"); };

  $("pitrSubmitBtn").onclick = async () => {
    const id = $("pitr_record_id").value;
    const target_time = $("pitr_target_time").value;
    if (!target_time) { toast("请选择目标时间", "warning"); return; }
    const target = _resolveTargetHost("pitr");
    if (!target) { toast("请选择目标主机或直接输入 IP/账号/密码", "warning"); return; }
    pitrModal.hide();
    try {
      const r = await api("POST", "/api/restores/pitr", {
        record_id: Number(id), target_time: target_time,
        target_host: target,
        target_db: $("pitr_target_db").value,
      });
      toast(r.message || (r.ok ? "PITR 成功" : "PITR 失败"), r.ok ? "success" : "danger");
      if (r.needs_manual_step || r.skipped_replay) console.warn("PITR 详情:", r);
    } catch (e) { toast("PITR 失败: " + e.message, "danger"); }
  };

  $("objectSubmitBtn").onclick = async () => {
    const id = $("object_record_id").value;
    const name = $("object_name").value;
    if (!name) { toast("请输入对象名", "warning"); return; }
    const target = _resolveTargetHost("object");
    if (!target) { toast("请选择目标主机或直接输入 IP/账号/密码", "warning"); return; }
    objectModal.hide();
    try {
      const r = await api("POST", "/api/restores/object", {
        record_id: Number(id), object_name: name,
        target_host: target,
        target_db: $("object_target_db").value,
      });
      toast(r.message || (r.ok ? "对象恢复成功" : "对象恢复失败"), r.ok ? "success" : "danger");
    } catch (e) { toast("对象恢复失败: " + e.message, "danger"); }
  };

  $("cloneSubmitBtn").onclick = async () => {
    const id = $("clone_record_id").value;
    const name = $("clone_name").value;
    if (!name) { toast("请输入实例名", "warning"); return; }
    cloneModal.hide();
    try {
      const r = await api("POST", "/api/vdb/clone", {
        record_id: Number(id), name: name,
        ttl_hours: Number($("clone_ttl").value) || 24,
        note: $("clone_note").value,
      });
      toast(r.message || (r.ok ? "克隆成功" : "克隆失败"), r.ok ? "success" : "danger");
      if (r.ok) setTimeout(() => loadVdbs(), 500);
    } catch (e) { toast("克隆失败: " + e.message, "danger"); }
  };

  async function initRecords() {
    const tasks = await api("GET", "/api/tasks");
    window._taskNames = Object.fromEntries(tasks.map((t) => [t.id, t.name]));
    window._recordDbTypes = Object.fromEntries(tasks.map((t) => [t.id, t.db_type]));
    $("filterTask").innerHTML = '<option value="">全部任务</option>' +
      tasks.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
    // 初始化模态框（直接用 getElementById，避免被 Proxy 替换后传给 Bootstrap）
    const pitrEl = document.getElementById("pitrModal");
    const objectEl = document.getElementById("objectModal");
    const cloneEl = document.getElementById("cloneModal");
    if (pitrEl) pitrModal = new bootstrap.Modal(pitrEl);
    if (objectEl) objectModal = new bootstrap.Modal(objectEl);
    if (cloneEl) cloneModal = new bootstrap.Modal(cloneEl);
    $("filterTask").onchange = loadRecords;
    const rsEl = $("recordSearch");
    if (rsEl) rsEl.addEventListener("input", () => {
      clearTimeout(window._recSearchTimer);
      window._recSearchTimer = setTimeout(loadRecords, 300);
    });

    // 保护策略下钻：若 URL 携带 policy_id，显示过滤条并禁用任务下拉框
    const urlParams = new URLSearchParams(location.search);
    const pid = urlParams.get("policy_id");
    if (pid) {
      try {
        const pol = await api("GET", "/api/policy/" + pid);
        const bar = $("policyFilterBar");
        const nameEl = $("policyFilterName");
        if (bar && nameEl) {
          nameEl.textContent = (pol.name || ("策略 #" + pid)) + "（共 " + (pol.bound_task_count || 0) + " 个任务）";
          bar.classList.remove("d-none");
        }
        $("filterTask").disabled = true;
      } catch (e) {
        toast("加载保护策略信息失败: " + e.message, "danger");
      }
    }
    await loadRecords();

    window.clearPolicyFilter = function () {
      const url = new URL(location.href);
      url.searchParams.delete("policy_id");
      history.replaceState({}, "", url);
      $("filterTask").disabled = false;
      const bar = $("policyFilterBar");
      if (bar) bar.classList.add("d-none");
      loadRecords();
    };
  }
  window.refreshRecords = () => { loadRecords(); };
  window.exportRecords = (fmt) => { window.open("/api/records/export?format=" + (fmt || "csv"), "_blank"); };

  // ------------------------- 数据恢复 -------------------------
  /**
   * 渲染恢复记录表。
   * @param {number} highlightId 需要高亮的恢复记录 ID（本次刚提交的记录），0 表示不高亮。
   */
  async function loadRestores(highlightId) {
    const rs = await api("GET", "/api/restores");
    const hl = Number(highlightId || 0);
    $("restoreTable").innerHTML = rs.map((r) => {
      const cls = (hl && Number(r.id) === hl) ? ' class="restore-row-new"' : "";
      // 收编为统一 fmtBizCell：R2 回退与 #id 语义只维护一处；
      // 同时修复此处原内联拼装把 #id 渲染成「恢复记录 ID」的历史缺陷（现为任务 ID）
      return `<tr${cls}>
        <td>${r.id}</td><td>${r.record_id}</td>
        <td>${fmtBizCell(r)}</td>
        <td>${esc(r.db_type_display || r.db_type || "-")}</td>
        <td>${fmtTime(r.backup_started_at)}</td>
        <td>${esc(r.target_host || "-")}</td><td>${esc(r.target_db || "-")}</td>
        <td>${fmtTime(r.started_at)}</td><td>${statusBadge(r.status)}</td>
        <td>${esc(r.operator || "-")}</td><td>${esc(r.message || "")}</td>
      </tr>`;
    }).join("") ||
      '<tr><td colspan="12" class="text-muted text-center">暂无恢复记录</td></tr>';
  }

  // ------------------------- VDB 测试库管理 -------------------------
  async function loadVdbs() {
    const vdbs = await api("GET", "/api/vdb");
    $("vdbTable").innerHTML = vdbs.map((v) => {
      const conn = `${v.username || 'root'}@${v.host || '127.0.0.1'}:${v.port || 3306}/${v.database_name || ''}`;
      const expiresIn = v.expires_at ? Math.max(0, Math.floor((new Date(v.expires_at) - new Date()) / 1000 / 3600)) : null;
      const expiresBadge = expiresIn === null ? '-' :
        expiresIn <= 0 ? '<span class="badge bg-danger">已过期</span>' :
        expiresIn < 24 ? `<span class="badge bg-warning">${expiresIn}h 后过期</span>` :
        `<span class="badge bg-secondary">${expiresIn}h 后过期</span>`;
      const statusBadge = v.status === "ready" ? '<span class="badge bg-success">运行中</span>'
                        : v.status === "creating" ? '<span class="badge bg-info">创建中</span>'
                        : v.status === "error" ? '<span class="badge bg-danger">异常</span>'
                        : `<span class="badge bg-secondary">${esc(v.status)}</span>`;
      return `<tr>
        <td>${v.id}</td>
        <td><code>${esc(v.name)}</code></td>
        <td>${esc(v.db_type)}</td>
        <td>#${v.source_record_id || '-'}</td>
        <td>${v.port || '-'}</td>
        <td><code class="small">${esc(conn)}</code></td>
        <td>${statusBadge}</td>
        <td>${fmtTime(v.created_at)}</td>
        <td>${v.expires_at || '-'} ${expiresBadge}</td>
        <td class="text-end">
          <button class="btn btn-sm btn-outline-danger" onclick="dropVdb(${v.id})">删除</button>
        </td>
      </tr>`;
    }).join("") || '<tr><td colspan="10" class="text-muted text-center">暂无 VDB，请到【备份记录】页克隆</td></tr>';
  }

  window.dropVdb = async (id) => {
    const ok = await confirmDialog({title:"确认销毁测试库",message:"将立即删除该测试库实例（不可恢复），确认？",confirmText:"删除",danger:true});
    if (!ok) return;
    try {
      const r = await api("DELETE", `/api/vdb/${id}`);
      toast(r.message || (r.ok ? "已删除" : "删除失败"), r.ok ? "success" : "warning");
      await loadVdbs();
    } catch (e) { toast(e.message, "danger"); }
  };
  window.loadVdbs = loadVdbs;

  async function initVdb() {
    await loadVdbs();
  }

  async function initRestore() {
    // 加载所有备份记录 + 任务信息 + SSH 纳管主机
    // /api/records/enriched 不下发 storage_tier / checksum，额外拉一次 /api/records 补齐
    // （两者同源于 backup_records，按 id 建索引即可，无需改后端）
    const [recs, tasks, hosts] = await Promise.all([
      api("GET", "/api/records/enriched"),
      api("GET", "/api/tasks"),
      api("GET", "/api/hosts"),
    ]);
    let rawRecords = [];
    try { rawRecords = await api("GET", "/api/records"); } catch (e) { rawRecords = []; }
    window.RESTORE_RAW = Object.fromEntries((rawRecords || []).map(r => [r.id, r]));
    window.RESTORE_HOSTS = hosts;
    window.RESTORE_TASKS = Object.fromEntries(tasks.map(t => [t.id, t]));
    // 过滤成功的记录
    const ok = recs.filter((r) => r.status === "success" || r.status === "simulated");
    window.RESTORE_RECORDS = ok;
    // 更新统计
    const dbCount = ok.filter(r => r.db_type !== 'file').length;
    const fileCount = ok.filter(r => r.db_type === 'file').length;
    if ($("r_db_count")) $("r_db_count").textContent = dbCount;
    if ($("r_file_count")) $("r_file_count").textContent = fileCount;

    // 填充纳管主机下拉
    const hostOpts = '<option value="">— 选择已纳管 SSH 主机 —</option>' +
      hosts.map(h => `<option value="${h.id}">${esc(h.name||h.host_key)} (${esc(h.hostname)}:${h.port||22})</option>`).join("");
    if ($("r_db_target_host_sel")) $("r_db_target_host_sel").innerHTML = hostOpts;
    if ($("r_file_target_host_sel")) $("r_file_target_host_sel").innerHTML = hostOpts;

    // 填充记录下拉
    renderRestoreRecords();
    onRestoreTypeChange();

    $("restoreForm").onsubmit = null; // 清除旧绑定
    $("restoreForm").onsubmit = (e) => {
      e.preventDefault();
      submitRestore();
    };
    await loadRestores();
  }

  // ===== 提交恢复 =====
  async function submitRestore() {
    const recordId = Number($("r_record").value);
    if (!recordId) { toast("请选择备份记录", "warning"); return; }
    const recType = document.querySelector('input[name="r_type"]:checked')?.value || 'db';

    // 重置上一次的结果状态条
    const resultBar = $("r_result_bar");
    if (resultBar && resultBar.classList) resultBar.classList.add("d-none");

    const body = { record_id: recordId, operator: ($("r_operator")?.value || "").trim() };

    if (recType === 'file') {
      // ---- 文件恢复：总是需要目标主机 + 目标目录 ----
      const dir = ($("r_target_dir")?.value || "").trim();
      if (!dir) { toast("请输入目标恢复目录", "warning"); return; }
      body.target_db = dir;

      const th = _resolveRestoreHost("r_file");
      if (!th) { toast("请选择纳管主机或直接输入 IP/端口/用户/密码", "warning"); return; }
      if (th.mode === "managed") {
        body.target_host_id = Number(th.host_key);
      } else {
        // 直接输入模式：构建 target_host 字符串 + 密码透传
        body.target_host = `${th.user}@${th.host}:${th.port}`;
        body.target_host_user = th.user;
        body.target_host_password = th.password;
      }
    } else {
      // ---- 数据库恢复：分"恢复到源任务"和"跨主机恢复" ----
      const mode = document.querySelector('input[name="r_mode"]:checked')?.value || 'same';
      if (mode === "same") {
        // 恢复到源任务，目标库可选
        body.target_db = ($("r_target_db_same")?.value || "").trim();
      } else {
        // 跨主机恢复
        const dbName = ($("r_target_db_remote")?.value || "").trim();
        body.target_db = dbName;
        const th = _resolveRestoreHost("r_db");
        if (!th) { toast("请选择纳管主机或直接输入 IP/端口/用户/密码", "warning"); return; }
        if (th.mode === "managed") {
          body.target_host_id = Number(th.host_key);
        } else {
          body.target_host = `${th.user}@${th.host}:${th.port}`;
          body.target_host_user = th.user;
          body.target_host_password = th.password;
        }
      }
    }

    // 提交即禁用按钮 + 显示不确定态进度条，避免重复提交
    const btn = $("r_submit_btn");
    const oldHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>恢复中…';
    const prog = $("r_progress_wrap");
    if (prog && prog.classList) prog.classList.remove("d-none");
    $("r_progress_text").textContent = "正在提交恢复请求，请勿关闭页面…";

    try {
      // POST /api/restores 同步执行并回传整条 restore_records 行（含 id / status / message）
      const r = await api("POST", "/api/restores", body) || {};
      const newId = Number(r.id || 0);
      const st = String(r.status || "");
      const okMsg = r.message || "恢复任务已提交，结果见下方恢复记录表";
      const barType = st === "failed" ? "danger" : st === "simulated" ? "warning" : "success";
      const prefix = st === "failed" ? "恢复失败：" : st === "simulated" ? "[仿真] " : "恢复完成：";
      toast(prefix + okMsg, barType === "danger" ? "danger" : "success", 6000);
      restoreResultBar(barType, prefix + okMsg + (newId ? "（恢复记录 #" + newId + "）" : ""));
      if (resultBar && resultBar.classList) resultBar.classList.remove("d-none");
      await loadRestores(newId);
    } catch (e2) {
      toast(e2.message, "danger");
      restoreResultBar("danger", "恢复失败：" + e2.message);
      if (resultBar && resultBar.classList) resultBar.classList.remove("d-none");
    } finally {
      btn.disabled = false;
      btn.innerHTML = oldHtml;
      if (prog && prog.classList) prog.classList.add("d-none");
    }
  }

  // 解析目标主机（恢复页专用）：优先直接输入，回退到纳管主机下拉
  function _resolveRestoreHost(prefix) {
    const sel = $(prefix + "_target_host_sel");
    const selVal = sel ? sel.value : "";
    const directHost = ($(prefix + "_direct_host")?.value || "").trim();
    if (directHost) {
      return {
        mode: "direct",
        host: directHost,
        port: Number($(prefix + "_direct_port")?.value) || 22,
        user: ($(prefix + "_direct_user")?.value || "").trim() || "root",
        password: $(prefix + "_direct_password")?.value || "",
      };
    }
    if (selVal) return { mode: "managed", host_key: selVal };
    return null;
  }

  // 渲染备份记录下拉（带类型过滤 + 关键字搜索）
  function renderRestoreRecords() {
    const records = window.RESTORE_RECORDS || [];
    const recType = document.querySelector('input[name="r_type"]:checked')?.value || 'db';
    // 统一搜索：业务系统名称 / IP 合并为一个搜索框，二者任一匹配即可
    const search = ($("r_search")?.value || "").toLowerCase().trim();
    // 按类型过滤
    let filtered = records.filter(r => {
      if (recType === 'db' && r.db_type === 'file') return false;
      if (recType === 'file' && r.db_type !== 'file') return false;
      return true;
    });
    // 按关键字过滤（业务系统 / IP，使用后端归一化后的 host_ip，避免「本地」被吞）
    if (search) {
      filtered = filtered.filter(r => {
        const name = (r.biz_label || "").toLowerCase();
        const ip = (r.host_ip || "").toLowerCase();
        return name.includes(search) || ip.includes(search);
      });
    }
    const html = filtered.length === 0
      ? '<option value="">无可用备份记录</option>'
      : filtered.map(r => `<option value="${r.id}">${fmtRecordLabel(r)}</option>`).join("");
    if ($("r_record")) $("r_record").innerHTML = html;
  }

  window.filterRestoreRecords = renderRestoreRecords;

  // 切换备份类型（数据库 ↔ 文件）
  window.onRestoreTypeChange = () => {
    const recType = document.querySelector('input[name="r_type"]:checked')?.value || 'db';
    if (recType === 'db') {
      // 数据库：显示模式选择 + 数据库相关字段，隐藏文件字段
      if ($("r_mode_db_row")) $("r_mode_db_row").style.display = "";
      if ($("r_file_row")) $("r_file_row").style.display = "none";
      onRestoreModeChange(); // 刷新 isSame/isRemote
    } else {
      // 文件：隐藏模式选择，只显示目标目录 + 目标主机
      if ($("r_mode_db_row")) $("r_mode_db_row").style.display = "none";
      if ($("r_db_same_row")) $("r_db_same_row").style.display = "none";
      if ($("r_db_remote_row")) $("r_db_remote_row").style.display = "none";
      if ($("r_file_row")) $("r_file_row").style.display = "";
    }
    renderRestoreRecords();
    onRecordChange();
  };

  // 切换恢复模式（仅在数据库类型时有效）
  window.onRestoreModeChange = () => {
    const mode = document.querySelector('input[name="r_mode"]:checked')?.value || 'same';
    if (mode === "same") {
      if ($("r_db_same_row")) $("r_db_same_row").style.display = "";
      if ($("r_db_remote_row")) $("r_db_remote_row").style.display = "none";
    } else {
      if ($("r_db_same_row")) $("r_db_same_row").style.display = "none";
      if ($("r_db_remote_row")) $("r_db_remote_row").style.display = "";
    }
  };

  // 存储层展示映射（backup_records.storage_tier: local | minio | s3 | multi）
  const RESTORE_TIER_LABELS = {
    local: "L1 本地", minio: "L2 热数据 (MinIO)", s3: "L3 冷归档 (S3)", multi: "多层副本",
  };

  /** 人类可读大小。0 / 缺失时返回 "-"。 */
  function restoreSize(bytes) {
    const v = Number(bytes || 0);
    if (v <= 0) return "-";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, x = v;
    while (x >= 1024 && i < units.length - 1) { x /= 1024; i += 1; }
    return (i === 0 ? String(x) : x.toFixed(2)) + " " + units[i];
  }

  /** 选中记录后以卡片展示 5 项关键信息（任务名 / 时间 / 大小 / 校验状态 / 存储层）。 */
  window.onRecordChange = () => {
    const row = $("r_record_info_row");
    const id = Number($("r_record")?.value);
    if (!id) { if (row) row.style.display = "none"; return; }
    const rec = (window.RESTORE_RECORDS || []).find(x => x.id === id);
    if (!rec) { if (row) row.style.display = "none"; return; }

    const raw = (window.RESTORE_RAW || {})[id] || {};
    // 直接使用后端归一化后的 host_ip（规则 R1），不再用正则二次提取——
    // 旧正则只匹配 IPv4，会把「本地」类任务显示为空（P1-1）。
    const ip = rec.host_ip || "-";

    // 四要素对齐：业务系统 + 备份类型 badge（已移除备份方式 badge）
    $("rs_title").innerHTML = esc(rec.biz_label || "未知任务") +
      ' <span class="badge bg-secondary">' + esc(rec.db_type_display || rec.db_type || "-") + '</span>';

    // 校验状态：verified=1 已校验；有 checksum 但未校验 → 未校验；两者皆无 → 无校验和
    const verified = Number(rec.verified || raw.verified || 0) === 1;
    const hasChecksum = !!(raw.checksum || "");
    const vmsg = rec.verify_msg || raw.verify_msg || "";
    $("rs_verified").innerHTML = verified
      ? '<span class="badge badge-ok" title="' + esc(vmsg) + '"><i class="bi bi-shield-check"></i> 已校验</span>'
      : (hasChecksum
        ? '<span class="badge badge-sim" title="' + esc(vmsg) + '"><i class="bi bi-shield-exclamation"></i> 未校验</span>'
        : '<span class="badge bg-secondary"><i class="bi bi-shield-slash"></i> 无校验和</span>');

    $("rs_time").textContent = fmtTime(rec.started_at);
    $("rs_size").textContent = raw.size_human || restoreSize(rec.size_bytes || raw.size_bytes);
    const tier = String(raw.storage_tier || "local");
    $("rs_tier").textContent = RESTORE_TIER_LABELS[tier] || tier;
    $("rs_source").innerHTML = '<code>' + esc(ip || "-") + '</code>' +
      (rec.source_db ? " / " + esc(rec.source_db) : "");
    $("rs_recno").innerHTML = '<code>#' + rec.id + '</code>';

    if (row) row.style.display = "";
  };

  /** 结果状态条：type = success | danger | warning。 */
  function restoreResultBar(type, msg) {
    const bar = $("r_result_bar");
    if (!bar || !bar.classList) return;
    bar.className = "alert py-2 mt-3 alert-" + (type === "success" ? "success" : type === "danger" ? "danger" : "warning");
    const icon = type === "success" ? "bi-check-circle-fill"
      : type === "danger" ? "bi-x-circle-fill" : "bi-exclamation-triangle-fill";
    bar.innerHTML = '<i class="bi ' + icon + ' me-1"></i>' + esc(msg);
  }

  window.loadRestoreForm = async () => { await initRestore(); };

  // ------------------------- 系统设置 -------------------------
  async function initSettings() {
    const sched = await api("GET", "/api/scheduler");
    $("schedInfo").innerHTML = `状态：<b>${sched.running ? "运行中" : "已停止"}</b>　任务数：<b>${sched.jobs.length}</b>` +
      (sched.jobs || []).map((j) =>
        `<div class="text-muted">${esc(j.id)}　下次：${esc(j.next_run || "-")}</div>`).join("");
    $("metaInfo").innerHTML =
      `演示模式 DEMO_MODE：<b>${esc(META.demo_mode)}</b><br>` +
      `调度启用：<b>${META.scheduler_enabled}</b><br>` +
      `支持数据库：${META.db_types.map((t) => esc(META.display_names[t] || t)).join("、")}`;
    $("reloadSchedBtn").onclick = async () => {
      await api("POST", "/api/scheduler/reload");
      toast("调度已重新加载");
      initSettings();
    };
    // 系统日志已迁出到独立菜单「系统日志」页面（/logs）
    // 此处不再加载日志，设置页与日志完全解耦

    // 通知配置
    try {
      const nc = await api("GET", "/api/notify-config");
      $("n_enabled").checked = !!nc.enabled;
      $("n_on_success").checked = !!nc.on_success;
      $("n_on_failure").checked = !!nc.on_failure;
      const email = (nc.channels || []).find((c) => c.type === "email") || {};
      $("n_smtp_host").value = email.smtp_host || "";
      $("n_smtp_port").value = email.smtp_port || 25;
      $("n_smtp_user").value = email.smtp_user || "";
      $("n_from_addr").value = email.from_addr || "";
      $("n_to").value = (email.to || []).join(", ");
      $("n_use_tls").checked = !!email.use_tls;
      // 密码字段提示：已保存 vs 正在修改
      const pwInput = $("n_smtp_password");
      const pwStatus = $("n_pw_status");
      const hasSavedPw = email.smtp_host && email.smtp_user;
      if (pwStatus) {
        pwStatus.textContent = hasSavedPw
          ? "已保存（页面不回显明文，留在 DB 加密）"
          : "尚未保存";
      }
      if (pwInput && pwStatus) {
        pwInput.addEventListener("input", () => {
          if (pwInput.value) {
            pwStatus.textContent = "已修改，保存后生效";
            pwStatus.classList.remove("text-muted");
            pwStatus.classList.add("text-warning");
          } else if (hasSavedPw) {
            pwStatus.textContent = "已保存（页面不回显明文，留在 DB 加密）";
            pwStatus.classList.remove("text-warning");
            pwStatus.classList.add("text-muted");
          }
        });
      }
      $("saveNotifyBtn").onclick = async () => {
        const data = {
          enabled: $("n_enabled").checked,
          on_success: $("n_on_success").checked,
          on_failure: $("n_on_failure").checked,
          channels: [{
            type: "email",
            smtp_host: $("n_smtp_host").value,
            smtp_port: Number($("n_smtp_port").value || 25),
            smtp_user: $("n_smtp_user").value,
            smtp_password: $("n_smtp_password").value,
            from_addr: $("n_from_addr").value,
            to: $("n_to").value,
            use_tls: $("n_use_tls").checked,
          }],
        };
        try { await api("POST", "/api/notify-config", data); toast("通知配置已保存"); }
        catch (e) { toast(e.message, "danger"); }
      };
      // 发送测试邮件
      const testBtn = $("testNotifyBtn");
      if (testBtn) {
        testBtn.onclick = async () => {
          const host = $("n_smtp_host").value.trim();
          const user = $("n_smtp_user").value.trim();
          const to = $("n_to").value.trim();
          const pw = $("n_smtp_password").value;
          if (!host || !user || !to) {
            toast("请先填写 SMTP 主机/用户/收件人", "warning");
            return;
          }
          if (!host.includes(".") || /\s/.test(host)) {
            toast("SMTP 主机格式不对，应为 smtp.xxx.com 形式（不是邮箱地址）", "warning");
            return;
          }
          testBtn.disabled = true;
          const oldText = testBtn.innerHTML;
          testBtn.innerHTML = '<i class="bi bi-hourglass-split"></i> 发送中...';
          try {
            // 先把当前表单内容存一次（确保后端拿到的是用户当前填的）
            await api("POST", "/api/notify-config", {
              enabled: $("n_enabled").checked,
              on_success: $("n_on_success").checked,
              on_failure: $("n_on_failure").checked,
              channels: [{
                type: "email",
                smtp_host: host,
                smtp_port: Number($("n_smtp_port").value || 25),
                smtp_user: user,
                smtp_password: pw,
                from_addr: $("n_from_addr").value,
                to: to,
                use_tls: $("n_use_tls").checked,
              }],
            });
            const r = await api("POST", "/api/notify-config/test", {});
            if (r.ok) {
              toast(r.message || "测试邮件已发送，请检查收件箱（含垃圾邮件）", "success", 6000);
            } else {
              toast("发送失败: " + (r.error || "未知错误"), "danger", 8000);
            }
          } catch (e) {
            const msg = (e && e.message) ? e.message : "发送失败";
            toast("发送失败: " + msg, "danger", 8000);
          } finally {
            testBtn.disabled = false;
            testBtn.innerHTML = oldText;
          }
        };
      }
    } catch (e) { /* 忽略加载失败 */ }

    // AI 预测告警配置
    try {
      const aiCfg = await api("GET", "/api/alerts/config");
      $("ai_cfg_enabled").checked = !!aiCfg.enabled;
      $("ai_cfg_min_level").value = aiCfg.min_risk_level_to_record || "medium";
      $("ai_cfg_notify_on").value = aiCfg.notify_on || "critical";
      $("ai_cfg_interval").value = aiCfg.ai_alert_interval_hours || 6;

      // AI 模型接入配置
      const aiModel = aiCfg.ai_model || {};
      $("ai_model_enabled").checked = !!aiModel.enabled;
      $("ai_model_provider").value = aiModel.provider || "openai";
      $("ai_model_endpoint").value = aiModel.endpoint || "";
      $("ai_model_model_name").value = aiModel.model_name || "";
      $("ai_model_api_key").value = "";  // 不回显密钥
      $("ai_model_local_path").value = aiModel.local_model_path || "";
      $("ai_model_timeout").value = aiModel.request_timeout_sec || 30;
      $("ai_model_max_chars").value = aiModel.max_input_chars || 8000;
      $("ai_model_prompt_template").value = aiModel.prompt_template || "";
      // 密钥状态提示
      $("aiApiKeyStatus").textContent = aiModel.api_key_set ? "已设置（不回显明文）" : "未设置";
      // 厂商切换显示/隐藏
      toggleAiModelProviderFields();
      // 加载模型状态徽章
      try {
        const modelStatus = await api("GET", "/api/alerts/model/status");
        updateAiModelStatusBadge(modelStatus);
        // 缓存 provider_presets 用于快速选择和端点自动填充
        if (modelStatus.provider_presets) {
          window.__AI_PROVIDER_PRESETS = modelStatus.provider_presets;
        }
        // 渲染快速选择链接
        renderAiModelQuickSelect();
      } catch (e2) { /* 忽略 */ }
    } catch (e) { /* AI 配置加载失败忽略 */ }

    $("saveAiConfigBtn").onclick = async function () {
      const payload = {
        enabled: $("ai_cfg_enabled").checked ? true : false,
        min_risk_level_to_record: $("ai_cfg_min_level").value,
        notify_on: $("ai_cfg_notify_on").value,
        ai_alert_interval_hours: Number($("ai_cfg_interval").value) || 6,
      };
      try {
        await api("POST", "/api/alerts/config", payload);
        toast("AI 告警配置已保存", "success");
        $("aiCfgError").classList.add("d-none");
      } catch (e) {
        $("aiCfgError").textContent = e.message;
        $("aiCfgError").classList.remove("d-none");
      }
    };

    // 保存模型配置
    $("saveAiModelConfigBtn").onclick = async function () {
      const payload = {
        ai_model: {
          enabled: $("ai_model_enabled").checked,
          provider: $("ai_model_provider").value,
          endpoint: $("ai_model_endpoint").value,
          api_key: $("ai_model_api_key").value,  // 留空表示不改
          model_name: $("ai_model_model_name").value,
          local_model_path: $("ai_model_local_path").value,
          request_timeout_sec: Number($("ai_model_timeout").value) || 30,
          max_input_chars: Number($("ai_model_max_chars").value) || 8000,
          prompt_template: $("ai_model_prompt_template").value,
        },
      };
      try {
        await api("POST", "/api/alerts/config", payload);
        toast("AI 模型配置已保存", "success");
        $("aiModelCfgError").classList.add("d-none");
        $("ai_model_api_key").value = "";  // 保存后清空密钥输入框
        // 重新加载配置以更新状态
        const aiCfg2 = await api("GET", "/api/alerts/config");
        const aiModel2 = aiCfg2.ai_model || {};
        $("aiApiKeyStatus").textContent = aiModel2.api_key_set ? "已设置（不回显明文）" : "未设置";
        // 更新模型状态徽章
        try {
          const ms = await api("GET", "/api/alerts/model/status");
          updateAiModelStatusBadge(ms);
        } catch (e3) { /* 忽略 */ }
      } catch (e) {
        $("aiModelCfgError").textContent = e.message;
        $("aiModelCfgError").classList.remove("d-none");
      }
    };

    // 测试模型连接
    $("testAiModelBtn").onclick = async function () {
      $("testAiModelBtn").disabled = true;
      const oldHtml = $("testAiModelBtn").innerHTML;
      $("testAiModelBtn").innerHTML = '<i class="bi bi-hourglass-split me-1"></i>测试中...';
      $("aiModelTestResult").classList.add("d-none");
      try {
        const override = {
          endpoint: $("ai_model_endpoint").value,
          model_name: $("ai_model_model_name").value,
          provider: $("ai_model_provider").value,
        };
        // 只有用户输入了新密钥才覆盖，否则后端保留已保存的密钥
        const inputKey = $("ai_model_api_key").value;
        if (inputKey) {
          override.api_key = inputKey;
        }
        const result = await api("POST", "/api/alerts/model/test", override);
        if (result.ok) {
          $("aiModelTestResult").textContent = "连接成功！延迟: " + result.latency_ms + "ms，模型返回: " + (result.sample_response || "(无响应内容)").substring(0, 100);
          $("aiModelTestResult").classList.remove("d-none", "alert-danger", "alert-warning");
          $("aiModelTestResult").classList.add("alert-success");
        } else {
          // 错误分类展示
          const category = result.error_category || "";
          const hint = result.hint || "";
          let icon = "";
          let alertClass = "alert-danger";
          if (category === "auth" || category === "forbidden") {
            icon = "\uD83D\uDD11 ";  // 🔑
            alertClass = "alert-danger";
          } else if (category === "rate_limit" || category === "provider_full") {
            icon = "\u23F3 ";  // ⏳
            alertClass = "alert-warning";
          } else if (category === "timeout") {
            icon = "\u23F1 ";  // ⏱
            alertClass = "alert-warning";
          } else if (category === "network" || category === "endpoint") {
            icon = "\uD83C\uDF10 ";  // 🌐
            alertClass = "alert-danger";
          } else if (category === "server_error") {
            icon = "\uD83D\uDEE1 ";  // 🛡
            alertClass = "alert-warning";
          }
          let msg = icon + "连接失败: " + result.error;
          if (result.latency_ms) msg += "，延迟: " + result.latency_ms + "ms";
          $("aiModelTestResult").textContent = msg;
          $("aiModelTestResult").classList.remove("d-none", "alert-success", "alert-danger", "alert-warning");
          $("aiModelTestResult").classList.add(alertClass);
          // hint 提示行
          const hintEl = $("aiModelTestHint");
          if (hint && hintEl) {
            hintEl.textContent = "\uD83D\uDCA1 " + hint;
            hintEl.classList.remove("d-none");
          } else if (hintEl) {
            hintEl.classList.add("d-none");
          }
        }
        // 保存测试结果到状态徽章
        try {
          const ms = await api("GET", "/api/alerts/model/status");
          updateAiModelStatusBadge(ms);
        } catch (e4) { /* 忽略 */ }
      } catch (e) {
        $("aiModelTestResult").textContent = "测试异常: " + e.message;
        $("aiModelTestResult").classList.remove("d-none", "alert-success");
        $("aiModelTestResult").classList.add("alert-danger");
      } finally {
        $("testAiModelBtn").disabled = false;
        $("testAiModelBtn").innerHTML = oldHtml;
      }
    };
    // 存储池加密密钥 (KMS)
    try { await initPoolCrypto(); } catch (e) { /* 忽略 */ }
  }

  // ------------------------- 存储池加密密钥 (KMS) -------------------------
  window.togglePoolCryptoFields = function () {
    const mode = $("pc_mode") ? $("pc_mode").value : "local";
    if ($("pc_local_fields")) $("pc_local_fields").classList.toggle("d-none", mode !== "local");
    if ($("pc_kms_fields")) $("pc_kms_fields").classList.toggle("d-none", mode !== "kms");
  };

  async function initPoolCrypto() {
    if (!$("pc_mode")) return;
    togglePoolCryptoFields();
    // 加载当前配置
    try {
      const cfg = await api("GET", "/api/pool-crypto");
      $("pc_mode").value = cfg.mode || "local";
      togglePoolCryptoFields();
      if (cfg.mode === "kms") {
        $("pc_kms_provider").value = cfg.kms_provider || "custom";
        $("pc_kms_endpoint").value = cfg.kms_endpoint || "";
        $("pc_kms_key_id").value = cfg.kms_key_id || "";
        $("pc_kms_access_key").value = cfg.kms_access_key || "";
      }
      const badge = $("poolCryptoStatusBadge");
      if (badge) {
        if (cfg.active) {
          badge.textContent = "已启用";
          badge.className = "badge ms-2 bg-success";
        } else {
          badge.textContent = "未启用";
          badge.className = "badge ms-2 bg-secondary";
        }
      }
      const ls = $("pc_local_status");
      if (ls) ls.textContent = cfg.local_key_set ? "已保存主密钥（不回显明文）" : "尚未保存";
    } catch (e) { /* 忽略 */ }

    // 保存
    $("pcSaveBtn").onclick = async () => {
      const mode = $("pc_mode").value;
      const payload = { mode };
      if (mode === "local") {
        payload.pool_key = $("pc_pool_key").value;
      } else {
        payload.kms_provider = $("pc_kms_provider").value;
        payload.kms_endpoint = $("pc_kms_endpoint").value;
        payload.kms_key_id = $("pc_kms_key_id").value;
        payload.kms_access_key = $("pc_kms_access_key").value;
        payload.kms_secret = $("pc_kms_secret").value;
        payload.local_fallback_key = $("pc_local_fallback_key").value;
      }
      const res = $("pc_save_result");
      const errEl = $("pcError");
      try {
        const r = await api("POST", "/api/pool-crypto", payload);
        res.textContent = r.message || "已保存";
        res.className = "form-text ms-2 text-success";
        errEl.classList.add("d-none");
        const badge = $("poolCryptoStatusBadge");
        if (badge) { badge.textContent = "已启用"; badge.className = "badge ms-2 bg-success"; }
        if (r.self_test && !r.self_test.ok) {
          errEl.textContent = "自检提示: " + (r.self_test.error || "未知");
          errEl.classList.remove("d-none");
        }
        // 保存后清空密钥输入框
        $("pc_pool_key").value = "";
        $("pc_kms_secret").value = "";
        $("pc_local_fallback_key").value = "";
      } catch (e) {
        res.textContent = "";
        errEl.textContent = e.message;
        errEl.classList.remove("d-none");
      }
    };

    // 测试 KMS 连通性
    const testBtn = $("pcTestKmsBtn");
    if (testBtn) {
      testBtn.onclick = async () => {
        const out = $("pc_kms_test_result");
        try {
          const r = await api("POST", "/api/pool-crypto/test", {
            kms_provider: $("pc_kms_provider").value,
            kms_endpoint: $("pc_kms_endpoint").value,
            kms_key_id: $("pc_kms_key_id").value,
            kms_access_key: $("pc_kms_access_key").value,
            kms_secret: $("pc_kms_secret").value,
          });
          out.textContent = r.message || "KMS 连通成功";
          out.className = "form-text ms-2 text-success";
        } catch (e) {
          out.textContent = (e.message || "KMS 测试失败");
          out.className = "form-text ms-2 text-danger";
        }
      };
    }
  }
  if (typeof window.initPoolCrypto === "undefined") {
    window.initPoolCrypto = initPoolCrypto;
  }

  // 模型厂商切换：local 隐藏远程字段，其他隐藏本地路径
  // 同时：非 custom/local 自动填充 endpoint，渲染快速选择链接
  window.toggleAiModelProviderFields = function () {
    const provider = $("ai_model_provider") ? $("ai_model_provider").value : "openai";
    const isLocal = provider === "local";
    document.querySelectorAll(".ai-model-remote-field").forEach((el) =>
      el.classList.toggle("d-none", isLocal));
    document.querySelectorAll(".ai-model-local-field").forEach((el) =>
      el.classList.toggle("d-none", !isLocal));
    // 端点自动填充（custom/local 不填充）
    if (!isLocal && provider !== "custom") {
      const presets = window.__AI_PROVIDER_PRESETS || {};
      const preset = presets[provider];
      if (preset && preset.endpoint) {
        $("ai_model_endpoint").value = preset.endpoint;
      }
    }
    // 渲染快速选择
    renderAiModelQuickSelect();
  };

  // 渲染快速选择：根据当前 provider 显示 model_examples 链接
  window.renderAiModelQuickSelect = function () {
    const container = $("aiModelQuickSelect");
    if (!container) return;
    const provider = $("ai_model_provider") ? $("ai_model_provider").value : "openai";
    const presets = window.__AI_PROVIDER_PRESETS || {};
    const preset = presets[provider];
    const examples = (preset && preset.model_examples) || [];
    if (examples.length === 0 || provider === "local") {
      container.innerHTML = '<span class="text-muted small">快速选择：无可用示例</span>';
      return;
    }
    let html = '<span class="text-muted small">快速选择：</span> ';
    examples.forEach((m) => {
      html += '<a href="#" class="small me-2 text-primary" onclick="event.preventDefault(); document.getElementById(\'ai_model_model_name\').value=\'' + m + '\'">' + m + '</a>';
    });
    container.innerHTML = html;
  };

  // 更新模型状态徽章
  function updateAiModelStatusBadge(ms) {
    const badge = $("aiModelStatusBadge");
    if (!badge) return;
    if (!ms) return;
    if (!ms.configured) {
      badge.textContent = "未配置";
      badge.className = "badge bg-secondary ms-2";
    } else if (!ms.enabled) {
      badge.textContent = "已配置(未启用)";
      badge.className = "badge bg-info ms-2";
    } else {
      // 尝试判断是否连通
      const lastTest = ms.last_test;
      if (lastTest && lastTest.ok) {
        badge.textContent = "已连通";
        badge.className = "badge bg-success ms-2";
      } else if (lastTest && !lastTest.ok) {
        badge.textContent = "不可达";
        badge.className = "badge bg-danger ms-2";
      } else {
        badge.textContent = "已启用";
        badge.className = "badge bg-warning ms-2";
      }
    }
  }

  // ------------------------- 文件备份（无 Agent） -------------------------
  let fileTaskModal = null;
  let hostModal = null;
  let FILE_HOSTS = [];

  function bindFileTaskFormEvents() {
    $("f_source_type").onchange = () => {
      document.querySelectorAll(".f-remote-only").forEach((el) =>
        el.classList.toggle("d-none", $("f_source_type").value !== "remote"));
    };
    $("f_target_type").onchange = () => {
      document.querySelectorAll(".f-target-remote").forEach((el) =>
        el.classList.toggle("d-none", $("f_target_type").value !== "remote"));
    };
    // 按天选择调度：根据 full/incremental/mixed 显示对应子区
    function refreshFileScheduleByDayBox() {
      const type = $("f_backup_type")?.value || "full";
      const box = $("f_mixed_schedule_box");
      const fullCol = $("f_full_schedule_col");
      const incCol = $("f_incremental_schedule_col");
      const tip = $("f_schedule_tip");
      if (box) {
        box.style.display = "";
        box.classList.remove("d-none");
      }
      if (fullCol) fullCol.classList.toggle("d-none", type === "incremental");
      if (incCol) incCol.classList.toggle("d-none", type === "full");
      if (tip) {
        if (type === "mixed") {
          tip.textContent = "组合备份：分别设置「全量」与「增量」的运行星期与时间。不勾选任何星期 = 该子任务不自动调度（可手动触发）。";
        } else if (type === "full") {
          tip.textContent = "全量备份：勾选运行星期并设置时间。不勾选任何星期 = 不自动调度（可手动触发）。";
        } else {
          tip.textContent = "增量备份：勾选运行星期并设置时间。不勾选任何星期 = 不自动调度（可手动触发）。";
        }
      }
    }
    $("f_backup_type").onchange = () => {
      refreshFileScheduleByDayBox();
    };
    refreshFileScheduleByDayBox();
  }

  function fillHostSelect(sel, hosts, includeEmpty) {
    let opts = includeEmpty ? '<option value="">（请选择远程主机）</option>' : "";
    opts += (hosts || []).map((h) =>
      `<option value="${esc(h.host_key)}">${esc(h.name)} (${esc(h.host_key)})</option>`).join("");
    sel.innerHTML = opts;
  }

  function openFileTaskModal(task) {
    $("fileTaskForm").reset();
    if (task) {
      $("fileTaskModalTitle").textContent = "编辑任务";
      $("f_id").value = task.id;
      // 同 openTaskModal：预填展示值，存量任务可直接保存（设计 D2）
      $("f_biz_system").value = task.biz_label || "";
      $("f_name").value = task.name || "";
      $("f_backup_type").value = task.backup_type || "full";
      let extra = {};
      try { extra = JSON.parse(task.extra_options || "{}"); } catch (e) { extra = {}; }
      $("f_source_type").value = extra.source_type || "local";
      $("f_source_paths").value = (extra.source_paths || []).map((p) => cleanFilePath(p)).join("\n");
      $("f_target_type").value = extra.target_type || "local";
      $("f_target_path").value = cleanFilePath(extra.target_path || "");
      $("f_exclude_patterns").value = (extra.exclude_patterns || []).join("\n");
      // 按天调度回填：单任务主 schedule 映射到对应子区；mixed 同时回填两套
      const bt = task.backup_type || "full";
      if (bt === "full") {
        const fake = Object.assign({}, task, { full_schedule_days: "", full_schedule_expr: task.cron_expr || "" });
        _fillMixedSub("f_full", fake);
      } else if (bt === "incremental") {
        const fake = Object.assign({}, task, { incremental_schedule_days: "", incremental_schedule_expr: task.cron_expr || "" });
        _fillMixedSub("f_inc", fake);
      } else {
        _fillMixedSub("f_full", task);
        _fillMixedSub("f_inc", task);
      }
      // 限速 / 压缩级别 / 保留天数
      $("f_bandwidth_limit").value = task.bandwidth_limit ?? 0;
      $("f_compress_level").value = task.compress_level ?? 0;
      $("f_retention_days").value = task.retention_days ?? 30;
      $("f_enabled").checked = !!task.enabled;
      $("f_demo_only").checked = !!task.demo_only;
      try {
        const feo = JSON.parse(task.extra_options || "{}");
        if ($("f_encrypt_pool")) $("f_encrypt_pool").checked = !!feo.encrypt_pool;
      } catch (e) {
        if ($("f_encrypt_pool")) $("f_encrypt_pool").checked = false;
      }
      // 回填远程主机选择（值为 host_key）
      setTimeout(() => {
        if (extra.source_host) $("f_remote_host").value = extra.source_host;
        if (extra.target_host) $("f_target_host").value = extra.target_host;
      }, 0);
    } else {
      $("fileTaskModalTitle").textContent = "新建文件备份任务";
      $("f_id").value = "";
      $("f_source_type").value = "local";
      $("f_target_type").value = "local";
      $("f_backup_type").value = "full";
    }
    fillHostSelect($("f_remote_host"), FILE_HOSTS, true);
    fillHostSelect($("f_target_host"), FILE_HOSTS, true);
    $("f_source_type").dispatchEvent(new Event("change"));
    $("f_target_type").dispatchEvent(new Event("change"));
    $("f_backup_type").dispatchEvent(new Event("change"));
    // 防御：确保文件备份模态框实例存在（自愈，避免 fileTaskModal 为 null 时抛错）
    if (!fileTaskModal) {
      const el = document.getElementById("fileTaskModal");
      if (el && window.bootstrap && bootstrap.Modal) fileTaskModal = new bootstrap.Modal(el);
    }
    if (fileTaskModal) fileTaskModal.show();
    else console.error("[openFileTaskModal] fileTaskModal 实例缺失，无法弹出编辑框");
  }

  function cleanFilePath(path) {
    if (!path) return path;
    let s = path.trim();
    for (const prefix of ["本地 :", "本地:", "远程 :", "远程:"]) {
      if (s.startsWith(prefix)) { s = s.slice(prefix.length).trim(); break; }
    }
    return s;
  }

  async function saveFileTask() {
    const id = $("f_id").value;
    const num = (v) => (v === "" || v == null ? null : Number(v));
    const sourceType = $("f_source_type").value;
    const targetType = $("f_target_type").value;
    const extra = {
      source_type: sourceType,
      source_paths: $("f_source_paths").value.split("\n").map((s) => cleanFilePath(s)).filter(Boolean),
      source_host: sourceType === "remote" ? $("f_remote_host").value : "",
      target_type: targetType,
      target_host: targetType === "remote" ? $("f_target_host").value : "",
      target_path: cleanFilePath($("f_target_path").value),
      exclude_patterns: $("f_exclude_patterns").value.split("\n").map((s) => s.trim()).filter(Boolean),
      follow_symlinks: false,
      encrypt_pool: !!($("f_encrypt_pool")?.checked),
    };
    const backupType = $("f_backup_type").value;
    const fMixed = backupType === "mixed";
    const fFull = _buildMixedSub("f_full");
    const fInc = _buildMixedSub("f_inc");
    let schedule_type = "none", cron_expr = "", interval_minutes = null;
    if (backupType === "full") {
      schedule_type = fFull.type;
      cron_expr = fFull.cron_expr;
    } else if (backupType === "incremental") {
      schedule_type = fInc.type;
      cron_expr = fInc.cron_expr;
    }
    const data = {
      name: $("f_name").value,
      biz_system: $("f_biz_system").value.trim(),
      db_type: "file",
      host: sourceType === "remote" ? extra.source_host : "本地",
      backup_type: backupType,
      schedule_type: schedule_type,
      cron_expr: cron_expr,
      interval_minutes: interval_minutes,
      mixed_backup: fMixed ? 1 : 0,
      full_schedule_type: fMixed ? fFull.type : "none",
      full_schedule_expr: fMixed ? fFull.cron_expr : "",
      full_schedule_days: fMixed ? fFull.days : "",
      incremental_schedule_type: fMixed ? fInc.type : "none",
      incremental_schedule_expr: fMixed ? fInc.cron_expr : "",
      incremental_schedule_days: fMixed ? fInc.days : "",
      bandwidth_limit: num($("f_bandwidth_limit").value) || 0,
      compress_level: num($("f_compress_level").value) || 0,
      retention_days: Number($("f_retention_days").value || 30),
      retention_count: 50,
      storage_backend: "local",
      extra_options: JSON.stringify(extra),
      compress: 0,
      demo_only: $("f_demo_only").checked ? 1 : 0,
      enabled: $("f_enabled").checked ? 1 : 0,
    };
    if (!data.biz_system) { toast("请填写业务系统", "danger"); return; }
    if (!data.name) { toast("请填写任务名称", "danger"); return; }
    if (!extra.source_paths.length) { toast("请至少填写一个源路径", "danger"); return; }
    if (!extra.target_path) { toast("请填写目标路径", "danger"); return; }
    if (sourceType === "remote" && !extra.source_host) { toast("请选择源远程主机", "danger"); return; }
    if (targetType === "remote" && !extra.target_host) { toast("请选择目标远程主机", "danger"); return; }
    try {
      if (id) { await api("PUT", `/api/tasks/${id}`, data); toast("任务已更新"); }
      else { await api("POST", "/api/tasks", data); toast("任务已创建"); }
      if (fileTaskModal) fileTaskModal.hide();
      await loadFileTasks();
    } catch (e) { toast(e.message, "danger"); }
  }

  async function loadFileTasks() {
    const tasks = await api("GET", "/api/tasks?db_type=file");
    $("fileTaskTable").innerHTML = tasks.map((t) => {
      let extra = {};
      try { extra = JSON.parse(t.extra_options || "{}"); } catch (e) { extra = {}; }
      const src = extra.source_type === "remote"
        ? `${esc(extra.source_host || "")} : ${esc((extra.source_paths || [])[0] || "")}`
        : `本地 : ${esc((extra.source_paths || [])[0] || "")}`;
      const dst = extra.target_type === "remote"
        ? `${esc(extra.target_host || "")} : ${esc(extra.target_path || "")}`
        : `本地 : ${esc(extra.target_path || "")}`;
      return `<tr>
        <td>${t.id}</td>
        <td>${esc(t.biz_label || "-")}</td>
        <td>${esc(t.name)}</td>
        <td class="text-truncate" style="max-width:200px">${src}</td>
        <td class="text-truncate" style="max-width:200px">${dst}</td>
        <td><code>${esc((extra.source_host) || "-")}</code> → <code>${esc((extra.target_host) || "-")}</code></td>
        <td>${esc(t.backup_type_display || t.backup_type)}</td>
        <td>${scheduleCell(t)}</td>
        <td>${t.enabled ? statusBadge(t.last_status || "never") : '<span class="badge bg-secondary">已停用</span>'}</td>
        <td class="text-end">
          <button class="btn btn-sm btn-outline-success" onclick="runFileTask(${t.id})">备份</button>
          <button class="btn btn-sm btn-outline-primary" onclick="editFileTask(${t.id})">编辑</button>
          <button class="btn btn-sm btn-outline-danger" onclick="delFileTask(${t.id})">删除</button>
        </td>
      </tr>`;
    }).join("") ||
      '<tr><td colspan="9" class="text-muted text-center">暂无文件备份任务，点击右上角“新建任务”</td></tr>';
  }

  window.runFileTask = async (id) => {
    const ok = await confirmDialog({
      title: "确认立即备份？", message: "将对文件备份任务执行一次手动备份（后台异步执行，大目录可能需要几分钟）。",
      confirmText: "立即备份", danger: false,
    });
    if (!ok) return;
    try {
      const r = await api("POST", `/api/tasks/${id}/run`);
      if (r.accepted) {
        toast("备份任务已提交，正在后台执行...", "dark");
        // 立即刷新一次显示 running 状态
        await loadFileTasks();
        // 轮询：每 3 秒查一次，直到状态不再是 running
        const poll = setInterval(async () => {
          try {
            const t = await api("GET", `/api/tasks/${id}`);
            const st = (t.last_status || "").toLowerCase();
            await loadFileTasks(); // 刷新列表
            if (st !== "running" && st !== "never") {
              clearInterval(poll);
              toast("备份完成：" + st, st === "success" || st === "simulated" ? "success" : "danger");
            }
          } catch (e) {
            clearInterval(poll);
          }
        }, 3000);
        // 最多轮询 10 分钟后自动停止
        setTimeout(() => clearInterval(poll), 600000);
      } else {
        toast("备份完成：" + r.status);
        await loadFileTasks();
      }
    } catch (e) { toast(e.message, "danger"); }
  };
  window.editFileTask = async (id) => {
    const t = await api("GET", `/api/tasks/${id}`);
    openFileTaskModal(t);
  };
  window.delFileTask = async (id) => {
    const ok = await confirmDialog({
      title: "确认删除该任务？", message: "将删除该文件备份任务及全部记录，不可恢复。",
      confirmText: "删除", danger: true,
    });
    if (!ok) return;
    try { await api("DELETE", `/api/tasks/${id}`); toast("已删除"); await loadFileTasks(); }
    catch (e) { toast(e.message, "danger"); }
  };

  // ---------------- 主机纳管 ----------------
  function bindHostFormEvents() {
    $("h_auth_type").onchange = () => {
      const v = $("h_auth_type").value;
      document.querySelectorAll(".h-pwd-only").forEach((el) => el.classList.toggle("d-none", v !== "password"));
      document.querySelectorAll(".h-key-only").forEach((el) => el.classList.toggle("d-none", v !== "key"));
    };
  }

  function openHostModal(host) {
    $("hostForm").reset();
    if (host) {
      $("hostModalTitle").textContent = "编辑主机";
      $("h_id").value = host.id;
      $("h_name").value = host.name || "";
      $("h_os_type").value = host.os_type || "linux";
      $("h_hostname").value = host.hostname || "";
      $("h_port").value = host.port || 22;
      $("h_username").value = host.username || "root";
      $("h_auth_type").value = host.auth_type || "password";
      $("h_password").value = "";
      $("h_private_key").value = "";
      $("h_remark").value = host.remark || "";
    } else {
      $("hostModalTitle").textContent = "纳管主机";
      $("h_id").value = "";
      $("h_os_type").value = "linux";
      $("h_port").value = 22;
      $("h_username").value = "root";
      $("h_auth_type").value = "password";
    }
    $("h_auth_type").dispatchEvent(new Event("change"));
    hostModal.show();
  }

  async function saveHost() {
    const id = $("h_id").value;
    const data = {
      name: $("h_name").value,
      os_type: $("h_os_type").value,
      hostname: $("h_hostname").value,
      port: Number($("h_port").value || 22),
      username: $("h_username").value || "root",
      auth_type: $("h_auth_type").value,
      password: $("h_password").value,
      private_key: $("h_private_key").value,
      remark: $("h_remark").value,
    };
    if (!data.name) { toast("请填写主机名称", "danger"); return; }
    if (!data.hostname) { toast("请填写主机地址", "danger"); return; }
    try {
      if (id) { await api("PUT", `/api/hosts/${id}`, data); toast("主机已更新"); }
      else { await api("POST", "/api/hosts", data); toast("主机已纳管"); }
      hostModal.hide();
      await loadHosts();
      await loadFileTasks();
    } catch (e) { toast(e.message, "danger"); }
  }

  async function loadHosts() {
    FILE_HOSTS = await api("GET", "/api/hosts");
    $("hostTable").innerHTML = FILE_HOSTS.map((h) => {
      const status = h.last_status || "unknown";
      const dotCls = status === "ok" ? "ok" : (status === "failed" ? "fail" : "unknown");
      const statusText = status === "ok" ? "正常" : (status === "failed" ? "失败" : "未探测");
      const statusBadge = status === "ok"
        ? '<span class="badge bg-success">正常</span>'
        : (status === "failed"
            ? '<span class="badge bg-danger">失败</span>'
            : '<span class="badge bg-secondary">未探测</span>');
      const dot = `<span class="host-dot ${dotCls}"></span>`;
      return `<tr>
        <td>${h.id}</td>
        <td>${esc(h.name)}</td>
        <td><code>${esc(h.host_key)}</code></td>
        <td>${esc(h.hostname)}:${esc(h.port || 22)}</td>
        <td>${esc(h.auth_type === "key" ? "私钥" : "密码")}</td>
        <td>${dot}${statusBadge}<br><small class="text-muted">${h.last_check_at ? "最近测试 " + esc(fmtTime(h.last_check_at)) : ""}</small></td>
        <td class="text-end">
          <button class="btn btn-sm btn-outline-secondary" onclick="testHost(${h.id})">测试</button>
          <button class="btn btn-sm btn-outline-primary" onclick="editHost(${h.id})">编辑</button>
          <button class="btn btn-sm btn-outline-danger" onclick="delHost(${h.id})">删除</button>
        </td>
      </tr>`;
    }).join("") ||
      '<tr><td colspan="7" class="text-muted text-center">暂无纳管主机，点击右上角"纳管主机"</td></tr>';
  }

  window.testHost = async (id) => {
    toast("正在测试连接…");
    try {
      const r = await api("POST", `/api/hosts/${id}/test`);
      if (r.ok) toast("连接成功：" + (r.banner || ""));
      else toast("连接失败：" + (r.message || ""), "danger");
      await loadHosts();
    } catch (e) { toast(e.message, "danger"); }
  };
  window.editHost = async (id) => {
    const h = await api("GET", `/api/hosts/${id}`);
    openHostModal(h);
  };
  window.delHost = async (id) => {
    const ok = await confirmDialog({
      title: "确认移除该主机？", message: "将从纳管列表移除该主机，已关联的任务不受影响但可能失去远程连接。",
      confirmText: "移除", danger: true,
    });
    if (!ok) return;
    try { await api("DELETE", `/api/hosts/${id}`); toast("已移除"); await loadHosts(); await loadFileTasks(); }
    catch (e) { toast(e.message, "danger"); }
  };

  async function initFileBackup() {
    const ftmEl = document.getElementById("fileTaskModal");
    if (ftmEl) fileTaskModal = new bootstrap.Modal(ftmEl);
    const hmEl = document.getElementById("hostModal");
    if (hmEl) hostModal = new bootstrap.Modal(hmEl);
    bindFileTaskFormEvents();
    bindHostFormEvents();
    const ftsBtn = $("fileTaskSaveBtn");
    if (ftsBtn) ftsBtn.onclick = saveFileTask;
    const hsBtn = $("hostSaveBtn");
    if (hsBtn) hsBtn.onclick = saveHost;
    const nftBtn = $("newFileTaskBtn");
    if (nftBtn) nftBtn.onclick = () => openFileTaskModal(null);
    const nhBtn = $("newHostBtn");
    if (nhBtn) nhBtn.onclick = () => openHostModal(null);
    await loadHosts();
    await loadFileTasks();
  }
  window.refreshFileBackup = async () => { await loadHosts(); await loadFileTasks(); };
  window.importFileTasksCsv = () => {
    const inp = document.createElement("input");
    inp.type = "file"; inp.accept = ".csv";
    inp.onchange = async () => {
      if (!inp.files[0]) return;
      const fd = new FormData(); fd.append("file", inp.files[0]);
      try {
        const res = await fetch("/api/tasks/import", { method: "POST", body: fd });
        const d = await res.json();
        toast(`导入完成：新增 ${d.created} 条，跳过 ${d.skipped} 条${d.errors.length ? "，错误 " + d.errors.length + " 条" : ""}`, d.errors.length ? "warning" : "success");
        await loadFileTasks();
      } catch (e) { toast("导入失败: " + e.message, "danger"); }
    };
    inp.click();
  };

  // ------------------------- 数据同步 -------------------------
  let syncModal = null;

  async function loadSyncTasks() {
    const tasks = await api("GET", "/api/sync/tasks");
    $("syncTaskTable").innerHTML = tasks.map((t) => {
      const src = t.source_type === "managed"
        ? `托管任务：${esc(t.source_task_name || ("#" + t.source_task_id))}`
        : `${esc(t.src_db_display || t.src_db_type)} : ${esc(t.src_host || "")}`;
      const dst = `${esc(t.tgt_db_display || t.tgt_db_type)} : ${esc(t.tgt_host || "")}`;
      return `<tr>
        <td>${t.id}</td><td>${esc(t.name)}</td>
        <td class="text-truncate" style="max-width:180px">${src}</td>
        <td class="text-truncate" style="max-width:180px">${dst}</td>
        <td>${scheduleCell(t)}</td>
        <td>${t.enabled ? statusBadge(t.last_status || "never") : '<span class="badge bg-secondary">已停用</span>'}</td>
        <td>${fmtTime(t.last_run_at) || "-"}</td>
        <td class="text-end">
          <button class="btn btn-sm btn-outline-success" onclick="runSync(${t.id})">同步</button>
          <button class="btn btn-sm btn-outline-primary" onclick="editSync(${t.id})">编辑</button>
          <button class="btn btn-sm btn-outline-danger" onclick="delSync(${t.id})">删除</button>
        </td>
      </tr>`;
    }).join("") ||
      '<tr><td colspan="8" class="text-muted text-center">暂无同步任务，点击右上角“新建同步任务”</td></tr>';
  }

  async function loadSyncRecords() {
    const recs = await api("GET", "/api/sync/records");
    $("syncRecordTable").innerHTML = recs.map((r) =>
      `<tr><td>${r.id}</td><td>${esc(r.sync_name || r.sync_task_id)}</td>
       <td>${fmtTime(r.started_at)}</td><td>${fmtTime(r.finished_at)}</td>
       <td>${statusBadge(r.status)}</td><td>${esc(r.message || "")}</td></tr>`).join("") ||
      '<tr><td colspan="6" class="text-muted text-center">暂无同步记录</td></tr>';
  }

  function bindSyncFormEvents() {
    $("s_source_type").onchange = () => {
      const v = $("s_source_type").value;
      document.querySelectorAll(".s-managed-only").forEach((el) => el.classList.toggle("d-none", v !== "managed"));
      document.querySelectorAll(".s-manual-only").forEach((el) => el.classList.toggle("d-none", v !== "manual"));
    };
    $("s_schedule_type").onchange = () => {
      const v = $("s_schedule_type").value;
      $("s_cron_wrap").classList.toggle("d-none", v !== "cron");
      $("s_interval_wrap").classList.toggle("d-none", v !== "interval");
    };
  }

  async function openSyncModal(task) {
    $("syncForm").reset();
    fillDbTypeSelect($("s_src_db_type"), ["file"]);
    fillDbTypeSelect($("s_tgt_db_type"), ["file"]);
    const dbTasks = await api("GET", "/api/tasks?db_type_exclude=file");
    $("s_source_task_id").innerHTML = dbTasks.map((t) =>
      `<option value="${t.id}">${esc(t.name)} (${esc(t.db_display_name || t.db_type)})</option>`).join("") ||
      '<option value="">无可用已纳管数据库</option>';
    if (task) {
      $("syncModalTitle").textContent = "编辑同步任务";
      $("s_id").value = task.id;
      $("s_name").value = task.name || "";
      $("s_source_type").value = task.source_type || "managed";
      $("s_source_task_id").value = task.source_task_id || "";
      $("s_src_db_type").value = task.src_db_type || "";
      $("s_src_host").value = task.src_host || "";
      $("s_src_port").value = task.src_port || "";
      $("s_src_username").value = task.src_username || "";
      $("s_src_db_name").value = task.src_db_name || "";
      $("s_tgt_db_type").value = task.tgt_db_type || "";
      $("s_tgt_host").value = task.tgt_host || "";
      $("s_tgt_port").value = task.tgt_port || "";
      $("s_tgt_username").value = task.tgt_username || "";
      $("s_tgt_db_name").value = task.tgt_db_name || "";
      $("s_schedule_type").value = task.schedule_type || "none";
      $("s_cron_expr").value = task.cron_expr || "";
      $("s_interval_minutes").value = task.interval_minutes || 60;
      $("s_enabled").checked = !!task.enabled;
    } else {
      $("syncModalTitle").textContent = "新建同步任务";
      $("s_id").value = "";
      $("s_source_type").value = "managed";
      $("s_schedule_type").value = "none";
      $("s_enabled").checked = true;
      $("s_src_db_type").value = "mysql";
      $("s_tgt_db_type").value = "mysql";
    }
    $("s_source_type").dispatchEvent(new Event("change"));
    $("s_schedule_type").dispatchEvent(new Event("change"));
    syncModal.show();
  }

  async function saveSync() {
    const id = $("s_id").value;
    const num = (v) => (v === "" || v == null ? null : Number(v));
    const stype = $("s_source_type").value;
    const data = {
      name: $("s_name").value,
      source_type: stype,
      source_task_id: stype === "managed" ? num($("s_source_task_id").value) : null,
      src_db_type: stype === "manual" ? $("s_src_db_type").value : null,
      src_host: stype === "manual" ? $("s_src_host").value : null,
      src_port: stype === "manual" ? num($("s_src_port").value) : null,
      src_username: stype === "manual" ? $("s_src_username").value : null,
      src_password: stype === "manual" ? $("s_src_password").value : null,
      src_db_name: stype === "manual" ? $("s_src_db_name").value : null,
      tgt_db_type: $("s_tgt_db_type").value,
      tgt_host: $("s_tgt_host").value,
      tgt_port: num($("s_tgt_port").value),
      tgt_username: $("s_tgt_username").value,
      tgt_password: $("s_tgt_password").value,
      tgt_db_name: $("s_tgt_db_name").value,
      schedule_type: $("s_schedule_type").value,
      cron_expr: $("s_cron_expr").value,
      interval_minutes: num($("s_interval_minutes").value),
      enabled: $("s_enabled").checked ? 1 : 0,
    };
    if (!data.name) { toast("请填写同步任务名称", "danger"); return; }
    if (stype === "manual" && (!data.src_db_type || !data.src_host)) { toast("请填写源数据库类型与主机", "danger"); return; }
    if (!data.tgt_db_type || !data.tgt_host) { toast("请填写目标数据库类型与主机", "danger"); return; }
    try {
      let resp;
      if (id) { resp = await api("PUT", `/api/sync/tasks/${id}`, data); toast("同步任务已更新"); }
      else {
        resp = await api("POST", "/api/sync/tasks", data);
        toast("同步任务已创建");
        // 后端对未识别字段的告警（字段名写错会被静默忽略，必须提示）
        if (resp && resp.warnings && resp.warnings.length) {
          toast(resp.warnings.join("；"), "warning");
        }
      }
      syncModal.hide();
      await loadSyncTasks(); await loadSyncRecords();
    } catch (e) { toast(e.message, "danger"); }
  }

  window.runSync = async (id) => {
    const ok = await confirmDialog({ title: "确认立即同步？", message: "将对源库执行一次数据同步到目标库。", confirmText: "立即同步", danger: false });
    if (!ok) return;
    try { const r = await api("POST", `/api/sync/tasks/${id}/run`); toast("同步完成：" + r.status); await loadSyncTasks(); await loadSyncRecords(); }
    catch (e) { toast(e.message, "danger"); }
  };
  window.editSync = async (id) => { const t = await api("GET", `/api/sync/tasks/${id}`); openSyncModal(t); };
  window.delSync = async (id) => {
    const ok = await confirmDialog({ title: "确认删除该同步任务？", message: "将删除该同步任务及全部同步记录，不可恢复。", confirmText: "删除", danger: true });
    if (!ok) return;
    try { await api("DELETE", `/api/sync/tasks/${id}`); toast("已删除"); await loadSyncTasks(); await loadSyncRecords(); }
    catch (e) { toast(e.message, "danger"); }
  };

  async function initSync() {
    const smEl = document.getElementById("syncModal");
    if (smEl) syncModal = new bootstrap.Modal(smEl);
    bindSyncFormEvents();
    const ssBtn = $("syncSaveBtn");
    if (ssBtn) ssBtn.onclick = saveSync;
    const nsBtn = $("newSyncBtn");
    if (nsBtn) nsBtn.onclick = () => openSyncModal(null);
    await loadSyncTasks();
    await loadSyncRecords();
  }

  // ------------------------- 恢复记录 -------------------------
  async function initRestoreRecords() {
    const kw = ($("restoreRecordSearch")?.value || "").trim();
    const url = kw ? "/api/restores?keyword=" + encodeURIComponent(kw) : "/api/restores";
    const rs = await api("GET", url);
    // 保存到全局，方便“查看日志”按钮快速读取
    window.RESTORE_RECORD_ROWS = rs;
    $("restoreRecordsTable").innerHTML = rs.map((r) => {
      // 收编为统一 fmtBizCell（原为内联拼装，缺 #id 且不走 R2）
      return `<tr><td>${r.id}</td><td>${r.record_id}</td>
       <td>${fmtBizCell(r)}</td>
       <td><code>${esc(r.host_ip || "-")}</code></td>
       <td>${esc(r.db_type_display || r.db_type || "-")}</td>
       <td>${fmtTime(r.backup_started_at)}</td>
       <td>${esc(r.target_host || "-")}</td><td>${esc(r.target_db || "-")}</td>
       <td>${fmtTime(r.started_at)}</td><td>${statusBadge(r.status)}</td>
       <td>${esc(r.operator || "-")}</td><td>${esc(r.message || "")}</td>
       <td><button class="btn btn-sm btn-outline-primary" onclick="showRestoreLog(${r.id})">查看</button></td></tr>`;
    }).join("") ||
      '<tr><td colspan="13" class="text-muted text-center">暂无恢复记录</td></tr>';

    const sb = $("restoreRecordSearch");
    if (sb && !sb._wired) {
      sb._wired = true;
      sb.addEventListener("input", () => {
        clearTimeout(window._rrSearchTimer);
        window._rrSearchTimer = setTimeout(initRestoreRecords, 300);
      });
    }
  }

  // 弹窗展示恢复详细日志
  window.showRestoreLog = (id) => {
    const r = (window.RESTORE_RECORD_ROWS || []).find(x => x.id === id);
    const content = r?.detail_log || r?.message || "暂无详细日志";
    const el = $("restoreLogContent");
    if (el) el.textContent = content;
    const modalEl = $("restoreLogModal");
    if (modalEl) {
      const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
      modal.show();
    }
  };

  // ------------------------- 巡检 -------------------------
  const inspBadge = (s) => {
    const m = { pass: ["badge-ok", "正常"], warn: ["badge-sim", "警告"], fail: ["badge-fail", "异常"] };
    const [c, t] = m[s] || ["bg-secondary", s || "-"];
    return `<span class="badge ${c}">${t}</span>`;
  };

  async function loadInspections() {
    const rs = await api("GET", "/api/inspection/records");
    $("inspectionTable").innerHTML = rs.map((r) => {
      const trig = r.triggered_by || "-";
      const trigBadge = trig === "schedule"
        ? '<span class="badge bg-info">定时</span>'
        : (trig === "manual" ? '<span class="badge bg-secondary">手动</span>' : esc(trig));
      return `<tr><td>${r.id}</td><td>${esc(r.task_name || r.task_id)}</td>
       <td>${esc(r.db_type || "-")}</td><td>${inspBadge(r.status)}</td>
       <td class="text-truncate" style="max-width:360px" title="${esc(r.detail || "")}">${esc(r.detail || "")}</td>
       <td>${trigBadge}</td><td>${fmtTime(r.started_at)}</td></tr>`;
    }).join("") ||
      '<tr><td colspan="7" class="text-muted text-center">暂无巡检记录，点击右上角"立即巡检"</td></tr>';
  }

  window.runInspection = async () => {
    try {
      const s = await api("POST", "/api/inspection/run", { triggered_by: "manual" });
      toast(`巡检完成：共 ${s.total} 项，异常 ${s.fail} 项，警告 ${s.warn} 项` +
            (s.fail ? "，已发送告警通知" : ""), s.fail ? "danger" : "dark");
      await loadInspections();
    } catch (e) { toast(e.message, "danger"); }
  };

  // 加载并渲染定时巡检配置
  async function loadInspectionSchedule() {
    try {
      const cfg = await api("GET", "/api/inspection/schedule");
      $("ins_enabled").checked = !!cfg.enabled;
      $("ins_cron").value = cfg.cron || "";
      const nr = cfg.next_run;
      $("ins_next_run").innerHTML = cfg.enabled
        ? (nr ? `<span class="text-success">${fmtTime(nr)}</span>` : '<span class="text-muted">已启用，等待首次触发</span>')
        : '<span class="text-muted">未启用</span>';
    } catch (e) { /* 忽略 */ }
  }

  async function initInspection() {
    $("runInspectionBtn").onclick = window.runInspection;
    const saveBtn = $("insSaveBtn");
    if (saveBtn) {
      saveBtn.onclick = async () => {
        const enabled = $("ins_enabled").checked;
        const cron = $("ins_cron").value.trim();
        try {
          const r = await api("POST", "/api/inspection/schedule", { enabled, cron });
          if (r.ok) {
            toast(r.enabled
              ? `定时巡检已启用，下一次：${fmtTime(r.next_run) || "稍后"}`
              : "已停用定时巡检", "success");
            await loadInspectionSchedule();
          }
        } catch (e) { toast(e.message || "保存失败", "danger", 6000); }
      };
    }
    await loadInspectionSchedule();
    await loadInspections();
  }
  window.refreshInspection = () => { loadInspections(); };
  window.exportInspection = (fmt) => { window.open("/api/inspection/records/export?format=" + (fmt || "csv"), "_blank"); };

  // ------------------------- 数据库部署 -------------------------
  let deployModal, deployLogModal;

  async function loadDeployments() {
    const deps = await api("GET", "/api/deploy");
    $("deployTable").innerHTML = deps.map((d) =>
      `<tr>
        <td>${d.id}</td><td>${esc(d.name)}</td><td>${esc(d.db_type)}</td>
        <td>${esc(d.host_display || (d.host_id ? '#'+d.host_id : d.direct_host) || '-')}</td>
        <td class="text-truncate" style="max-width:150px" title="${esc(d.base_dir||'')}">${esc(d.base_dir || '-')}</td>
        <td>${d.port || '-'}</td>
        <td>${d.progress_pct ? `<div class="progress" style="height:6px"><div class="progress-bar bg-teal" style="width:${d.progress_pct}%"></div></div><small>${d.progress_pct}%</small>` : '-'}</td>
        <td>${statusBadge(d.status)}</td>
        <td>${fmtTime(d.started_at) || '-'}</td>
        <td class="text-end">
          ${d.status === 'running' ? '' : `<button class="btn btn-sm btn-outline-success" onclick="runDeploy(${d.id})">部署</button>`}
          <button class="btn btn-sm btn-outline-info" onclick="viewDeployLog(${d.id},'${esc(d.name)}')">日志</button>
          <button class="btn btn-sm btn-outline-danger" onclick="delDeploy(${d.id})">删除</button>
        </td>
      </tr>`).join("") ||
      '<tr><td colspan="10" class="text-muted text-center">暂无部署，点击右上角"新建部署"</td></tr>';
  }
  window.loadDeployments = loadDeployments;

  window.viewDeployLog = async (id, name) => {
    $("logTitle").textContent = name;
    const el = $("deployLogContent");
    if (!el.dataset.deployId || Number(el.dataset.deployId) !== Number(id)) {
      el.textContent = "加载中...";
      el.dataset.deployId = id;
      el.dataset.lastLogLen = "0";
    }
    try {
      const d = await api("GET", `/api/deploy/${id}/log`);
      const logText = d.log || "(暂无日志)";
      const lastLen = parseInt(el.dataset.lastLogLen || "0", 10);
      if (logText.length !== lastLen) {
        // 只追加新增内容，避免全量替换导致闪烁/滚动复位
        if (lastLen > 0 && logText.startsWith(el.textContent)) {
          const appended = logText.slice(el.textContent.length);
          el.appendChild(document.createTextNode(appended));
        } else {
          el.textContent = logText;
        }
        el.dataset.lastLogLen = String(logText.length);
        // 保持滚动在底部
        el.scrollTop = el.scrollHeight;
      }
      deployLogModal.show();
      // 持续轮询直到状态不再是 running
      clearTimeout(window._deployLogTimer);
      if (d.status === "running") {
        window._deployLogTimer = setTimeout(() => window.viewDeployLog(id, name), 2000);
      } else {
        // 结束后 2 秒再拉一次最终日志
        window._deployLogTimer = setTimeout(() => window.viewDeployLog(id, name), 2000);
      }
    } catch (e) {
      el.textContent = "加载日志失败: " + e.message;
    }
  };

  window.runDeploy = async (id) => {
    const ok = await confirmDialog({title:"确认执行部署",message:"将通过 SSH 在目标主机上安装数据库，可能耗时较长，确认执行？",confirmText:"开始部署",danger:false,warnIcon:true});
    if (!ok) return;
    try {
      const r = await api("POST", `/api/deploy/${id}/run`);
      if (r.accepted) {
        toast("部署已开始，请查看日志监控进度", "success");
        await loadDeployments();
        setTimeout(() => window.viewDeployLog(id, "部署中"), 1000);
      }
    } catch (e) { toast(e.message, "danger"); }
  };

  window.delDeploy = async (id) => {
    const ok = await confirmDialog({title:"确认删除",message:"将删除该部署记录，已安装的数据库实例不会受影响。确认？",confirmText:"删除",danger:true});
    if (!ok) return;
    try { await api("DELETE", `/api/deploy/${id}`); toast("已删除"); await loadDeployments(); }
    catch (e) { toast(e.message, "danger"); }
  };

  function openDeployModal(dep) {
    // 隐藏所有扩展参数面板
    document.querySelectorAll(".d-extra-params").forEach(el => el.style.display = "none");
    if (!dep) {
      $("deployModalTitle").textContent = "新建部署";
      $("d_id").value = ""; $("d_name").value = ""; $("d_host_id").value = "";
      $("d_hostname").value = ""; $("d_root_password").value = "";
      $("d_version").value = "";
      $("d_package_path").value = ""; $("d_package_path_remote").value = "";
      $("d_package_file").value = ""; $("pkgFileInfo").textContent = "";
      _resetPkgProgress();
      $("d_db_type").value = "mysql";
      $("pkgSrcUpload").checked = true;
      $("hostModeManaged").checked = true;
      $("d_direct_host").value = ""; $("d_direct_port").value = 22;
      $("d_direct_user").value = "root"; $("d_direct_password").value = "";
      togglePkgSource();
      toggleHostMode();
      loadDefaultParams();  // 预填所有默认参数
    } else {
      $("deployModalTitle").textContent = "编辑部署";
      $("d_id").value = dep.id; $("d_name").value = dep.name || "";
      $("d_db_type").value = dep.db_type;
      if (dep.direct_host) {
        $("hostModeDirect").checked = true;
        $("d_direct_host").value = dep.direct_host || "";
        $("d_direct_port").value = dep.direct_port || 22;
        $("d_direct_user").value = dep.direct_user || "root";
        $("d_direct_password").value = dep.direct_password || "";
        $("d_host_id").value = "";
      } else {
        $("hostModeManaged").checked = true;
        $("d_host_id").value = dep.host_id || "";
        $("d_direct_host").value = ""; $("d_direct_port").value = 22;
        $("d_direct_user").value = "root"; $("d_direct_password").value = "";
      }
      toggleHostMode();
      $("d_base_dir").value = dep.base_dir || ""; $("d_data_dir").value = dep.data_dir || "";
      $("d_port").value = dep.port || ""; $("d_password").value = "";
      const pp = dep.package_path || "";
      if (pp.startsWith("/tmp/")) {
        $("pkgSrcRemote").checked = true;
        $("d_package_path_remote").value = pp;
        $("d_package_path").value = "";
        $("d_package_file").value = ""; $("pkgFileInfo").textContent = "";
      } else if (pp) {
        $("pkgSrcUpload").checked = true;
        $("d_package_path").value = pp;
        $("d_package_path_remote").value = "";
      } else {
        $("pkgSrcUpload").checked = true;
        $("d_package_path").value = ""; $("d_package_path_remote").value = "";
        $("d_package_file").value = ""; $("pkgFileInfo").textContent = "";
      }
      _resetPkgProgress();
      togglePkgSource();
      // 回填扩展参数
      let cfg = {};
      try { cfg = JSON.parse(dep.config_json || "{}"); } catch (e) {}
      $("d_version").value = cfg.version || "";
      $("d_hostname").value = cfg.hostname || "";
      $("d_root_password").value = "";
      // MYSQL
      $("d_mysql_charset").value = cfg.mysql_charset || "utf8mb4";
      $("d_mysql_max_conn").value = cfg.mysql_max_conn || 200;
      $("d_mysql_buffer").value = cfg.mysql_buffer || 512;
      $("d_mysql_server_id").value = cfg.mysql_server_id || 1;
      $("d_mysql_binlog").value = cfg.mysql_binlog !== undefined ? cfg.mysql_binlog : 1;
      // PG
      $("d_pg_encoding").value = cfg.pg_encoding || "UTF8";
      $("d_pg_max_conn").value = cfg.pg_max_conn || 100;
      $("d_pg_shared_buf").value = cfg.pg_shared_buf || "128MB";
      $("d_pg_wal").value = cfg.pg_wal || "replica";
      $("d_pg_locale").value = cfg.pg_locale || "zh_CN.UTF-8";
      // Oracle
      $("d_ora_sid").value = cfg.ora_sid || "orcl";
      $("d_ora_charset").value = cfg.ora_charset || "AL32UTF8";
      $("d_ora_ncharset").value = cfg.ora_ncharset || "AL16UTF16";
      $("d_ora_mem").value = cfg.ora_mem || 40;
      $("d_ora_cdb").value = cfg.ora_cdb !== undefined ? cfg.ora_cdb : 1;
      $("d_ora_pdb").value = cfg.ora_pdb || "pdb";
      $("d_ora_os_pwd").value = "";
      // Kingbase
      $("d_kb_mode").value = cfg.kb_mode || "pg";
      $("d_kb_encoding").value = cfg.kb_encoding || "UTF8";
      $("d_kb_max_conn").value = cfg.kb_max_conn || 200;
      $("d_kb_shared_buf").value = cfg.kb_shared_buf || "256MB";
      // Dameng
      $("d_dm_instance").value = cfg.dm_instance || "DMSERVER";
      $("d_dm_charset").value = cfg.dm_charset || 1;
      $("d_dm_page").value = cfg.dm_page || 32;
      $("d_dm_case").value = cfg.dm_case !== undefined ? cfg.dm_case : 1;
      $("d_dm_sysdba").value = cfg.dm_sysdba || "Dameng123";
      // Redis
      $("d_redis_maxmem").value = cfg.redis_maxmem || "1gb";
      $("d_redis_evict").value = cfg.redis_evict || "allkeys-lru";
      $("d_redis_aof").value = cfg.redis_aof || "yes";
      $("d_redis_cluster").value = cfg.redis_cluster || "";
      // MongoDB
      $("d_mongo_repl").value = cfg.mongo_repl || "";
      $("d_mongo_cache").value = cfg.mongo_cache || 1;
      $("d_mongo_auth").value = cfg.mongo_auth !== undefined ? cfg.mongo_auth : 1;
      onDeployTypeChange();
    }
    deployModal.show();
  }

  window.loadDefaultParams = () => {
    $("d_id").value && ($("d_id").value = ""); // 重置编辑状态
    const t = $("d_db_type").value;
    const defs = {
      mysql:    {base:"/usr/local/mysql",data:"/data/mysql",port:3306,version:"9.3.0",
                 mysql_charset:"utf8mb4",mysql_max_conn:"200",mysql_buffer:"512",mysql_server_id:"1",mysql_binlog:"1"},
      postgresql:{base:"/usr/local/pgsql",data:"/data/pgsql",port:5432,version:"16",
                 pg_encoding:"UTF8",pg_max_conn:"100",pg_shared_buf:"128MB",pg_wal:"replica",pg_locale:"zh_CN.UTF-8"},
      oracle:   {base:"/u01/app/oracle",data:"/u01/oradata",port:1521,version:"19C",
                 ora_sid:"orcl",ora_charset:"AL32UTF8",ora_ncharset:"AL16UTF16",ora_mem:"40",ora_cdb:"1",ora_pdb:"pdb",ora_os_pwd:"oracle"},
      kingbase: {base:"/opt/Kingbase/ES/V9",data:"/data/kingbase",port:54321,version:"V9",
                 kb_mode:"pg",kb_encoding:"UTF8",kb_max_conn:"200",kb_shared_buf:"256MB"},
      dameng:   {base:"/opt/dmdbms",data:"/data/dmdbms",port:5236,version:"8",
                 dm_instance:"DMSERVER",dm_charset:"1",dm_page:"32",dm_case:"1",dm_sysdba:"Dameng123"},
      redis:    {base:"/usr/local/redis",data:"/data/redis",port:6379,version:"7.2",
                 redis_maxmem:"1gb",redis_evict:"allkeys-lru",redis_aof:"yes",redis_cluster:""},
      mongodb:  {base:"/usr/local/mongodb",data:"/data/mongodb",port:27017,version:"7.0",
                 mongo_repl:"",mongo_cache:"1",mongo_auth:"1"},
    };
    const d = defs[t] || defs.mysql;
    // 核心参数
    $("d_base_dir").value = d.base; $("d_data_dir").value = d.data;
    $("d_port").value = d.port; $("d_version").value = d.version;
    // 高级参数（按类型填充）
    for (const k of Object.keys(d)) {
      if (k === "base" || k === "data" || k === "port" || k === "version") continue;
      const el = $("d_" + k);
      if (el) el.value = d[k];
    }
    onDeployTypeChange();
    toast("已加载 " + t + " 默认参数", "info");
  };

  function _resetPkgProgress() {
    const wrap = $("pkgUploadProgressWrap");
    if (wrap) wrap.classList.add("d-none");
    const bar = $("pkgUploadProgressBar");
    if (bar) { bar.style.width = "0%"; bar.setAttribute("aria-valuenow", "0"); }
    const txt = $("pkgUploadProgressText");
    if (txt) txt.textContent = "上传准备中...";
    const pct = $("pkgUploadProgressPct");
    if (pct) pct.textContent = "0%";
  }
  function _setPkgProgress(pct, text) {
    const wrap = $("pkgUploadProgressWrap");
    if (wrap) wrap.classList.remove("d-none");
    const bar = $("pkgUploadProgressBar");
    if (bar) { bar.style.width = pct + "%"; bar.setAttribute("aria-valuenow", String(pct)); }
    const t = $("pkgUploadProgressText");
    if (t) t.textContent = text || ("已上传 " + pct + "%");
    const p = $("pkgUploadProgressPct");
    if (p) p.textContent = pct + "%";
  }

  window.togglePkgSource = () => {
    const isUpload = $("pkgSrcUpload").checked;
    $("pkgUploadBlock").style.display = isUpload ? "" : "none";
    $("pkgRemoteBlock").style.display = isUpload ? "none" : "";
    _resetPkgProgress();
    if (isUpload) { $("d_package_path_remote").value = ""; }
    else { $("d_package_file").value = ""; $("pkgFileInfo").textContent = ""; $("d_package_path").value = ""; }
  };

  // 切换节点配置：纳管主机 vs 直接输入 IP
  window.toggleHostMode = () => {
    const managed = $("hostModeManaged").checked;
    $("hostManagedGroup").style.display = managed ? "" : "none";
    $("hostDirectGroup").style.display = managed ? "none" : "";
  };

  window.onDeployTypeChange = () => {
    const t = $("d_db_type").value;
    document.querySelectorAll(".d-extra-params").forEach(el => el.style.display = "none");
    // MariaDB 与 MySQL 部署参数基本一致，复用同一参数区
    const map = {mysql:"d_extra_mysql",mariadb:"d_extra_mysql",postgresql:"d_extra_pg",oracle:"d_extra_oracle",
                 kingbase:"d_extra_kingbase",dameng:"d_extra_dameng",
                 redis:"d_extra_redis",mongodb:"d_extra_mongo"};
    const el = document.getElementById(map[t]);
    if (el) el.style.display = "";
  };

  function _collectConfig() {
    // 收集所有扩展参数到 config_json
    return {
      version: $("d_version").value,
      hostname: $("d_hostname").value,
      root_password: $("d_root_password").value,
      // MySQL
      mysql_charset: $("d_mysql_charset").value,
      mysql_max_conn: $("d_mysql_max_conn").value,
      mysql_buffer: $("d_mysql_buffer").value,
      mysql_server_id: $("d_mysql_server_id").value,
      mysql_binlog: $("d_mysql_binlog").value,
      // PG
      pg_encoding: $("d_pg_encoding").value,
      pg_max_conn: $("d_pg_max_conn").value,
      pg_shared_buf: $("d_pg_shared_buf").value,
      pg_wal: $("d_pg_wal").value,
      pg_locale: $("d_pg_locale").value,
      // Oracle
      ora_sid: $("d_ora_sid").value,
      ora_charset: $("d_ora_charset").value,
      ora_ncharset: $("d_ora_ncharset").value,
      ora_mem: $("d_ora_mem").value,
      ora_cdb: $("d_ora_cdb").value,
      ora_pdb: $("d_ora_pdb").value,
      ora_os_pwd: $("d_ora_os_pwd").value,
      // Kingbase
      kb_mode: $("d_kb_mode").value,
      kb_encoding: $("d_kb_encoding").value,
      kb_max_conn: $("d_kb_max_conn").value,
      kb_shared_buf: $("d_kb_shared_buf").value,
      // Dameng
      dm_instance: $("d_dm_instance").value,
      dm_charset: $("d_dm_charset").value,
      dm_page: $("d_dm_page").value,
      dm_case: $("d_dm_case").value,
      dm_sysdba: $("d_dm_sysdba").value,
      // Redis
      redis_maxmem: $("d_redis_maxmem").value,
      redis_evict: $("d_redis_evict").value,
      redis_aof: $("d_redis_aof").value,
      redis_cluster: $("d_redis_cluster").value,
      // MongoDB
      mongo_repl: $("d_mongo_repl").value,
      mongo_cache: $("d_mongo_cache").value,
      mongo_auth: $("d_mongo_auth").value,
    };
  }

  async function initDeploy() {
    try {
      const dmEl = document.getElementById("deployModal");
      if (dmEl) deployModal = new bootstrap.Modal(dmEl);
      const dlmEl = document.getElementById("deployLogModal");
      if (dlmEl) {
        deployLogModal = new bootstrap.Modal(dlmEl);
        dlmEl.addEventListener("hidden.bs.modal", () => {
          clearTimeout(window._deployLogTimer);
          window._deployLogTimer = null;
          const el = $("deployLogContent");
          if (el) {
            delete el.dataset.deployId;
            delete el.dataset.lastLogLen;
          }
        });
      }
      const ndbBtn = $("newDeployBtn");
      if (ndbBtn) ndbBtn.onclick = () => openDeployModal(null);
    } catch (e) {
      console.error("[initDeploy]", e);
    }
    const dFile = $("d_package_file");
    if (dFile) dFile.onchange = () => {
      const file = dFile.files[0];
      const info = $("pkgFileInfo");
      _resetPkgProgress();
      if (!file) { info.textContent = ""; return; }
      const sz = file.size >= 1073741824 ? (file.size/1073741824).toFixed(2)+" GB" :
                   file.size >= 1048576 ? (file.size/1048576).toFixed(2)+" MB" :
                   file.size >= 1024 ? (file.size/1024).toFixed(1)+" KB" : file.size+" B";
      info.textContent = `已选文件: ${file.name} (${sz})`;
      $("d_package_path").value = "";
    };
    // 依赖包上传（如 gcc 离线 RPM）
    const depFile = $("d_dependency_file");
    if (depFile) depFile.onchange = () => {
      const f = depFile.files[0];
      const hint = $("depUploadHint");
      $("d_dependency_path").value = "";
      if (!f) { if (hint) hint.textContent = "支持 .tar.gz/.tgz/.zip 离线依赖包；内容含 *.rpm 时自动 rpm -Uvh 安装"; return; }
      const sz = f.size >= 1073741824 ? (f.size/1073741824).toFixed(2)+" GB" :
                   f.size >= 1048576 ? (f.size/1048576).toFixed(2)+" MB" :
                   (f.size/1024).toFixed(1)+" KB";
      if (hint) hint.textContent = `已选依赖包: ${f.name} (${sz})，点击「上传」暂存`;
    };
    const depUpBtn = $("depUploadBtn");
    if (depUpBtn) depUpBtn.onclick = () => {
      const f = $("d_dependency_file").files[0];
      if (!f) { toast("请先选择依赖包文件", "warning"); return; }
      const fd = new FormData(); fd.append("file", f);
      const xhr = new XMLHttpRequest();
      xhr.onload = () => {
        if (xhr.status === 200) {
          const r = JSON.parse(xhr.responseText);
          $("d_dependency_path").value = r.path || r.url || "";
          const hint = $("depUploadHint");
          if (hint) hint.textContent = `依赖包已暂存: ${f.name}`;
          toast("依赖包已上传暂存", "success");
        } else {
          toast("依赖包上传失败", "danger");
        }
      };
      xhr.onerror = () => toast("依赖包上传失败", "danger");
      xhr.open("POST", "/api/deploy/upload");
      xhr.send(fd);
    };
    const dsbBtn = $("deploySaveBtn");
    if (dsbBtn) dsbBtn.onclick = async () => {
      const id = $("d_id").value;
      if (!$("d_name").value) { toast("请输入部署名称", "warning"); return; }
      const managedMode = $("hostModeManaged").checked;
      if (managedMode) {
        if (!$("d_host_id").value) { toast("请选择目标主机", "warning"); return; }
      } else {
        if (!$("d_direct_host").value.trim()) { toast("请输入目标主机 IP", "warning"); return; }
        if (!$("d_direct_user").value.trim()) { toast("请输入 SSH 用户", "warning"); return; }
      }
      // 1) 如果选择本地上传，先上传到平台暂存（进度显示在安装包区域）
      let pkgPath = $("d_package_path").value;
      if ($("pkgSrcUpload").checked) {
        const file = $("d_package_file").files[0];
        if (!file) { toast("请选择安装包文件", "warning"); return; }
        pkgPath = await new Promise((resolve, reject) => {
          const fd = new FormData(); fd.append("file", file);
          const xhr = new XMLHttpRequest();
          $("deploySaveBtn").disabled = true;
          $("deploySaveBtn").innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>上传中...';
          _setPkgProgress(0, "准备上传...");
          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
              const pct = Math.min(100, Math.round(e.loaded / e.total * 100));
              _setPkgProgress(pct, pct < 100 ? "正在上传安装包..." : "上传完成，准备部署...");
            }
          };
          xhr.onload = () => {
            $("deploySaveBtn").disabled = false;
            $("deploySaveBtn").innerHTML = '保存并部署';
            if (xhr.status >= 200 && xhr.status < 300) {
              try { const d = JSON.parse(xhr.responseText); resolve(d.path); }
              catch (e) { reject(new Error("上传响应解析失败")); }
            } else {
              _setPkgProgress(0, "上传失败");
              try { const d = JSON.parse(xhr.responseText); reject(new Error(d.error || `上传失败 ${xhr.status}`)); }
              catch (e) { reject(new Error(`上传失败 ${xhr.status}`)); }
            }
          };
          xhr.onerror = () => {
            $("deploySaveBtn").disabled = false; $("deploySaveBtn").innerHTML = "保存并部署";
            _setPkgProgress(0, "网络错误");
            reject(new Error("网络错误"));
          };
          xhr.ontimeout = () => {
            $("deploySaveBtn").disabled = false; $("deploySaveBtn").innerHTML = "保存并部署";
            _setPkgProgress(0, "上传超时");
            reject(new Error("上传超时"));
          };
          xhr.open("POST", "/api/deploy/upload");
          xhr.send(fd);
        });
        $("d_package_path").value = pkgPath;
        toast(`安装包已上传: ${file.name} 已暂存`, "success");
      } else {
        pkgPath = $("d_package_path_remote").value.trim();
        if (!pkgPath) { toast("请填写目标主机上的安装包路径", "warning"); return; }
      }

      // 依赖包：若已选文件但未点「上传」，这里自动上传
      let depPath = $("d_dependency_path").value;
      const depFileEl = $("d_dependency_file");
      if (!depPath && depFileEl && depFileEl.files[0]) {
        depPath = await new Promise((resolve) => {
          const fd = new FormData(); fd.append("file", depFileEl.files[0]);
          const xhr = new XMLHttpRequest();
          xhr.onload = () => {
            if (xhr.status === 200) {
              const r = JSON.parse(xhr.responseText);
              $("d_dependency_path").value = r.path || r.url || "";
              resolve($("d_dependency_path").value);
            } else { resolve(""); }
          };
          xhr.onerror = () => resolve("");
          xhr.open("POST", "/api/deploy/upload");
          xhr.send(fd);
        });
      }

      const data = {
        name: $("d_name").value, db_type: $("d_db_type").value,
        host_id: managedMode ? (Number($("d_host_id").value) || null) : null,
        direct_host: managedMode ? null : ($("d_direct_host").value.trim() || null),
        direct_port: Number($("d_direct_port").value) || 22,
        direct_user: $("d_direct_user").value.trim() || "root",
        direct_password: $("d_direct_password").value || "",
        hostname: $("d_hostname").value,
        root_password: $("d_root_password").value,
        base_dir: $("d_base_dir").value, data_dir: $("d_data_dir").value,
        port: Number($("d_port").value) || null,
        password: $("d_password").value,
        package_path: pkgPath,
        dependency_path: depPath || null,
        config_json: JSON.stringify(_collectConfig()),
      };
      if (!data.name) { toast("请输入部署名称", "warning"); return; }
      if (!data.host_id && !data.direct_host) {
        toast("请选择已纳管主机或直接输入 IP/账号/密码", "warning"); return;
      }
      try {
        let depId = id ? Number(id) : null;
        $("deploySaveBtn").disabled = true;
        $("deploySaveBtn").innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>保存中...';
        if (depId) {
          await api("PUT", `/api/deploy/${depId}`, data);
          toast("已更新", "success");
        } else {
          const r = await api("POST", "/api/deploy", data);
          depId = r.id;
          toast("部署创建成功 #" + depId, "success");
        }
        // 保存后立即启动部署
        $("deploySaveBtn").innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>启动部署...';
        const runRes = await api("POST", `/api/deploy/${depId}/run`);
        if (runRes.accepted) {
          toast("部署已开始，请查看日志监控进度", "success");
        }
        deployModal.hide();
        await loadDeployments();
        setTimeout(() => window.viewDeployLog(depId, $("d_name").value || "部署中"), 800);
      } catch (e) {
        toast(e.message, "danger");
      } finally {
        $("deploySaveBtn").disabled = false;
        $("deploySaveBtn").innerHTML = '保存并部署';
      }
    };
    try { window.DEPLOY_HOSTS = await api("GET", "/api/hosts"); } catch (e) { window.DEPLOY_HOSTS = []; }
    $("d_host_id").innerHTML = '<option value="">— 请选择 —</option>' +
      (window.DEPLOY_HOSTS || []).map(h => `<option value="${h.id}">${esc(h.name||h.host_key)} (${esc(h.hostname)})</option>`).join("");
    await loadDeployments();
  }

  // ======================= 实时备份 · PITR 时间轴 (T05) =======================
  const RT = {
    tasks: [],          // 实时保护任务列表
    taskId: 0,          // 当前选中任务
    dbType: "",         // 当前任务引擎（file / mysql / postgresql ...）
    timeline: null,     // 最近一次时间轴聚合结果
    window: null,       // 可恢复窗口
    cursorTs: "",       // 已选时间点
    plan: null,         // 已选时间点的恢复计划
    selectedIdx: -1,    // 已选桶下标
    modal: null,        // 恢复确认模态框
    refreshTimer: null, // 状态轮询定时器
    createModal: null,  // 「创建实时保护任务」模态框（T07）
    candidates: [],     // 可开启实时保护的候选备份任务（rt_enabled != 1）
    deepLinkId: 0,      // ?task_id= 深链带入的任务 ID
  };

  // 本地时区 ISO8601（与后端 datetime.astimezone().isoformat(timespec="seconds") 对齐）
  const rtIso = (d) => {
    const p = (n) => String(n).padStart(2, "0");
    const off = -d.getTimezoneOffset();
    const sign = off >= 0 ? "+" : "-";
    const oh = p(Math.floor(Math.abs(off) / 60));
    const om = p(Math.abs(off) % 60);
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" +
      p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()) + sign + oh + ":" + om;
  };

  const rtBytes = (n) => {
    const v = Number(n || 0);
    if (v <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, x = v;
    while (x >= 1024 && i < units.length - 1) { x /= 1024; i += 1; }
    return (i === 0 ? String(x) : x.toFixed(1)) + " " + units[i];
  };

  const rtDur = (sec) => {
    const s = Math.max(0, Math.floor(Number(sec || 0)));
    if (s < 60) return s + " 秒";
    if (s < 3600) return Math.floor(s / 60) + " 分 " + (s % 60) + " 秒";
    if (s < 86400) return Math.floor(s / 3600) + " 时 " + Math.floor((s % 3600) / 60) + " 分";
    return Math.floor(s / 86400) + " 天 " + Math.floor((s % 86400) / 3600) + " 时";
  };

  const rtKindLabel = (k) => ({
    "base-full": "基准全量", "file-inc": "文件增量",
    "db-full": "库全量", "db-log": "日志段",
  }[k] || k || "-");

  const rtDot = (h) => '<span class="rt-dot ' + esc(h || "unknown") + '"></span>';

  // ---------- 守护状态 ----------
  /**
   * 守护 stopped 兜底提示条显隐（T06）。
   * running === false 时显形；true 或 null（状态未知）时隐藏。
   */
  function rtSyncStoppedHint(running) {
    const bar = document.getElementById("rtStoppedHint");
    if (bar) bar.classList.toggle("d-none", running !== false);
  }

  /** 探测守护进程是否在跑；接口不可用时返回 null（未知，不做降级提示）。 */
  async function rtProbeDaemonRunning() {
    try {
      const st = await api("GET", "/api/rt/status");
      return st ? !!st.running : null;
    } catch (e) { return null; }
  }

  /** 启动守护进程（守护条「启动」与 stopped 提示条「启动守护」共用）。 */
  async function rtStartDaemon() {
    try {
      const res = await api("POST", "/api/rt/control", { action: "start" });
      toast(res.message || "已启动", res.ok ? "dark" : "danger");
    } catch (e) { toast("启动失败：" + e.message, "danger"); }
    await rtLoadDaemon();
  }

  async function rtLoadDaemon() {
    let st = null;
    try { st = await api("GET", "/api/rt/status"); } catch (e) { st = null; }
    if (!st) {
      $("rtDaemonState").textContent = "不可用";
      rtSyncStoppedHint(null);
      return;
    }
    rtSyncStoppedHint(!!st.running);
    $("rtDaemonState").textContent = st.running
      ? "运行中" : (st.enabled ? "已停止" : "总开关关闭 (RT_BACKUP_ENABLED=0)");
    $("rtDaemonDriver").textContent = st.driver || "-";
    $("rtDaemonWorkers").textContent = String(st.worker_count || 0) +
      (st.breach_count ? "（" + st.breach_count + " 个 RPO 超标）" : "");
    $("rtDaemonTick").textContent = String(st.tick_count || 0) + " 次 / " +
      (st.tick_interval_sec || 0) + "s";
    $("rtDaemonHeartbeat").textContent = fmtTime(st.last_tick_at);
  }

  // ---------- 健康统计 ----------
  async function rtLoadHealth() {
    let res = null;
    try { res = await api("GET", "/api/rt/health"); } catch (e) { res = null; }
    if (!res || !res.summary) return;
    const s = res.summary;
    $("rtStatTotal").textContent = String(s.total || 0);
    $("rtStatDist").innerHTML = rtDot("green") + (s.green || 0) + " 绿 &nbsp;" +
      rtDot("yellow") + (s.yellow || 0) + " 黄 &nbsp;" +
      rtDot("red") + (s.red || 0) + " 红 &nbsp;" +
      rtDot("unknown") + (s.unknown || 0) + " 未知";
    $("rtStatCompliance").textContent = (s.rpo_compliance_pct || 0) + "%";
    $("rtStatBreach").textContent = s.breach ? (s.breach + " 个任务 RPO 超标") : "全部达标";
    $("rtStatPoints").textContent = String(s.rp_count_today || 0);
    $("rtStatBytes").textContent = "今日产出 " + (s.bytes_today_human || "0 B");

    const items = res.items || [];
    let cur = null;
    for (let i = 0; i < items.length; i += 1) {
      if (Number(items[i].task_id) === Number(RT.taskId)) { cur = items[i]; break; }
    }
    if (cur) {
      $("rtStatRpo").innerHTML = rtDot(cur.health) + rtDur(cur.rpo_actual_sec);
      $("rtStatRpoTarget").textContent = "目标 " + rtDur(cur.rpo_target_sec) +
        " · 守护 " + (cur.daemon_status || "-") +
        (cur.degrade_reason ? "（" + cur.degrade_reason + "）" : "");
    } else {
      $("rtStatRpo").textContent = "-";
      $("rtStatRpoTarget").textContent = RT.taskId ? "该任务暂无实时状态" : "请选择任务";
    }
  }

  // ---------- 任务分组（T07） ----------
  // 分组规则（设计文档 §2 B）：db_type ∈ {mysql, mariadb, postgresql}
  // 且 rt_mode ∈ {db_cdc, auto, ""} → 数据库·秒级日志 PITR；其余 → 文件·分钟级变更捕获。
  const RT_DB_ENGINES = ["mysql", "mariadb", "postgresql"];
  const RT_GROUP_DB = "数据库 · 秒级日志 PITR";
  const RT_GROUP_FILE = "文件 · 分钟级变更捕获";

  /** 判定任务属于「数据库日志」组还是「文件变更」组。 */
  function rtGroupOf(task) {
    const dbType = String((task && task.db_type) || "").toLowerCase();
    const mode = String((task && task.rt_mode) || "").toLowerCase();
    const isDbEngine = RT_DB_ENGINES.indexOf(dbType) >= 0;
    const isDbMode = mode === "" || mode === "db_cdc" || mode === "auto";
    return (isDbEngine && isDbMode) ? RT_GROUP_DB : RT_GROUP_FILE;
  }

  /** 把 options 数组按 optgroup 标签拼成 select 内容，空组不输出。 */
  function rtBuildOptGroups(groups) {
    return Object.keys(groups).map(function (label) {
      const opts = groups[label];
      if (!opts.length) return "";
      return '<optgroup label="' + esc(label) + '">' + opts.join("") + "</optgroup>";
    }).join("");
  }

  /** 空状态与主体区互斥显隐；同时联动任务级按钮可用性。 */
  function rtToggleEmptyState(isEmpty) {
    // 守护状态条（rtDaemonBar）不隐藏：空态下用户仍可能需要启停守护进程
    const ids = ["rtStatRow", "rtTimelineCard", "rtDetailRow"];
    ids.forEach(function (id) {
      const el = document.getElementById(id);
      if (el) el.classList.toggle("d-none", !!isEmpty);
    });
    const empty = document.getElementById("rtEmptyState");
    if (empty) empty.classList.toggle("d-none", !isEmpty);
    rtSyncTaskButtons();
  }

  /** 未选中任务时禁用任务级操作按钮（立即捕获 / 恢复到此刻）。 */
  function rtSyncTaskButtons() {
    const hasTask = Number(RT.taskId) > 0;
    const trigger = document.getElementById("rtTriggerBtn");
    if (trigger) {
      trigger.disabled = !hasTask;
      trigger.title = hasTask ? "对当前任务立即捕获一次" : "请先选择一个实时保护任务";
    }
    const recover = document.getElementById("rtRecoverBtn");
    if (recover && !hasTask) recover.disabled = true;
  }

  // ---------- 任务列表 ----------
  async function rtLoadTasks() {
    let res = null;
    try { res = await api("GET", "/api/rt/tasks"); } catch (e) { res = null; }
    RT.tasks = (res && res.items) || [];
    const sel = $("rtTaskSelect");
    if (!RT.tasks.length) {
      sel.innerHTML = '<option value="">— 暂无开启实时保护的任务 —</option>';
      RT.taskId = 0;
      RT.dbType = "";
      rtToggleEmptyState(true);
      return;
    }
    rtToggleEmptyState(false);

    // 分组下拉：optgroup + 实际 RPO 标注
    const groups = {};
    groups[RT_GROUP_DB] = [];
    groups[RT_GROUP_FILE] = [];
    RT.tasks.forEach(function (t) {
      const h = t.health || {};
      const flag = h.health === "green" ? "●"
        : (h.health === "yellow" ? "▲" : (h.health === "red" ? "■" : "○"));
      const rpo = (h.rpo_actual_sec === null || h.rpo_actual_sec === undefined)
        ? "RPO 未知" : ("实际 RPO " + rtDur(h.rpo_actual_sec));
      const label = flag + " " + t.name + "（" + (t.db_type || "file") +
        (t.rt_mode ? " · " + t.rt_mode : "") + " · " + rpo + "）";
      groups[rtGroupOf(t)].push(
        '<option value="' + t.id + '">' + esc(label) + "</option>"
      );
    });
    sel.innerHTML = rtBuildOptGroups(groups);

    if (!RT.taskId || !RT.tasks.some((t) => Number(t.id) === Number(RT.taskId))) {
      RT.taskId = Number(RT.tasks[0].id);
    }
    sel.value = String(RT.taskId);
    const picked = RT.tasks.filter((t) => Number(t.id) === Number(RT.taskId))[0];
    RT.dbType = (picked && picked.db_type) || "file";
    rtSyncTaskButtons();
  }

  // ---------- 创建实时保护任务（T07） ----------
  // 后端契约：候选列表复用 GET /api/tasks（前端筛 rt_enabled != 1），
  // 开启动作复用 PUT /api/rt/tasks/<id>/config（T06 在该接口内补 rt_tasks upsert）。
  /** 拉取候选备份任务并填充分组下拉。返回候选条数。 */
  async function rtFillCreateCandidates(preferTaskId) {
    let tasks = [];
    try {
      const res = await api("GET", "/api/tasks");
      tasks = Array.isArray(res) ? res : ((res && res.items) || []);
    } catch (e) { tasks = []; }

    const candidates = tasks.filter(function (t) {
      const enabled = t.enabled === undefined ? true : !!Number(t.enabled);
      return enabled && !Number(t.rt_enabled);
    });
    RT.candidates = candidates;

    const noneEl = $("rtCreateNoCandidate");
    const fieldsEl = $("rtCreateFields");
    const submitBtn = $("rtCreateSubmitBtn");
    if (!candidates.length) {
      $("rtCreateNoCandidateMsg").textContent = tasks.length
        ? "所有备份任务均已开启实时保护，无新的可选任务。"
        : "暂无可开启实时保护的备份任务。";
      noneEl.classList.remove("d-none");
      fieldsEl.classList.add("d-none");
      submitBtn.disabled = true;
      return 0;
    }
    noneEl.classList.add("d-none");
    fieldsEl.classList.remove("d-none");
    submitBtn.disabled = false;

    const groups = {};
    groups[RT_GROUP_DB] = [];
    groups[RT_GROUP_FILE] = [];
    candidates.forEach(function (t) {
      const rpoRaw = t.rt_rpo_target_sec;
      const rpo = (rpoRaw === null || rpoRaw === undefined || rpoRaw === "")
        ? "目标 RPO 未设定" : ("目标 RPO " + rtDur(rpoRaw));
      const addr = t.host ? (t.host + (t.port ? ":" + t.port : "")) : "";
      const label = t.name + "（" + (t.db_type || "file") +
        (addr ? " · " + addr : "") + " · " + rpo + "）";
      groups[rtGroupOf(t)].push(
        '<option value="' + t.id + '">' + esc(label) + "</option>"
      );
    });
    $("rtCreateTaskSel").innerHTML = rtBuildOptGroups(groups);

    // 深链或调用方指定了任务时优先选中
    const want = Number(preferTaskId || 0);
    if (want && candidates.some(function (t) { return Number(t.id) === want; })) {
      $("rtCreateTaskSel").value = String(want);
    }
    rtSyncCreateMode();
    return candidates.length;
  }

  /** 按所选任务的引擎类型给捕获模式/间隔一个合理默认值。 */
  function rtSyncCreateMode() {
    const id = Number($("rtCreateTaskSel").value || 0);
    const t = (RT.candidates || []).filter(function (x) { return Number(x.id) === id; })[0];
    if (!t) return;
    const isDb = rtGroupOf(t) === RT_GROUP_DB;
    $("rtCreateMode").value = isDb ? "db_cdc" : "file_polling";
    $("rtCreateInterval").value = String(isDb ? 60 : 180);
    const rpoRaw = t.rt_rpo_target_sec;
    $("rtCreateRpo").value = String(
      (rpoRaw === null || rpoRaw === undefined || rpoRaw === "") ? (isDb ? 120 : 300) : rpoRaw
    );
  }

  /** 打开创建弹窗。preferTaskId 支持从备份任务页深链带入。 */
  async function rtOpenCreateModal(preferTaskId) {
    $("rtCreateError").classList.add("d-none");
    $("rtCreateError").textContent = "";
    await rtFillCreateCandidates(preferTaskId);
    if (!RT.createModal) {
      const el = document.getElementById("rtCreateModal");
      if (el && window.bootstrap) RT.createModal = new bootstrap.Modal(el);
    }
    if (RT.createModal) RT.createModal.show();
  }

  /** 提交开启：PUT /api/rt/tasks/<id>/config，成功后自动选中并加载时间轴（无需刷新页面）。 */
  async function rtSubmitCreate() {
    const taskId = Number($("rtCreateTaskSel").value || 0);
    const errEl = $("rtCreateError");
    errEl.classList.add("d-none");
    if (!taskId) {
      errEl.textContent = "请先选择一个备份任务";
      errEl.classList.remove("d-none");
      return;
    }
    const interval = Number($("rtCreateInterval").value) || 180;
    const rpo = Number($("rtCreateRpo").value) || 0;
    const payload = {
      rt_enabled: 1,
      rt_mode: $("rtCreateMode").value || "file_polling",
      rt_interval_sec: interval,
    };
    if (rpo > 0) payload.rt_rpo_target_sec = rpo;

    const btn = $("rtCreateSubmitBtn");
    const old = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 开启中…';
    try {
      await api("PUT", "/api/rt/tasks/" + taskId + "/config", payload);
      if (RT.createModal) RT.createModal.hide();
      // T06：守护 stopped 时任务虽已置 rt_enabled=1，但 worker 不会拉起，
      // 不产出恢复点 —— 此处降级为 warning 并显形常驻提示条，杜绝静默成功。
      const daemonRunning = await rtProbeDaemonRunning();
      rtSyncStoppedHint(daemonRunning);
      if (daemonRunning === false) {
        toast("已开启实时保护，但守护进程未启动，暂不会产生恢复点", "warning", 6000);
      } else {
        toast("已开启实时保护，正在加载时间轴…", "success");
      }
      RT.taskId = taskId;
      RT.cursorTs = "";
      RT.plan = null;
      await rtLoadTasks();
      RT.taskId = taskId;
      const sel = $("rtTaskSelect");
      if (sel) sel.value = String(taskId);
      const picked = RT.tasks.filter(function (t) { return Number(t.id) === taskId; })[0];
      RT.dbType = (picked && picked.db_type) || "file";
      await rtLoadHealth();
      await rtLoadTimeline();
    } catch (e) {
      errEl.textContent = "开启失败：" + e.message;
      errEl.classList.remove("d-none");
    } finally {
      btn.disabled = false;
      btn.innerHTML = old;
    }
  }

  // ---------- 时间轴 ----------
  async function rtLoadTimeline() {
    if (!RT.taskId) {
      $("rtAxis").innerHTML = '<div class="text-muted small p-3">请先选择一个实时保护任务</div>';
      $("rtPointTable").innerHTML =
        '<tr><td colspan="6" class="text-center text-muted py-4">请选择任务</td></tr>';
      return;
    }
    try { RT.window = await api("GET", "/api/rt/window?task_id=" + RT.taskId); }
    catch (e) { RT.window = null; }

    const range = Number($("rtRangeSelect").value || 86400);
    const buckets = Number($("rtBucketSelect").value || 120);
    let qs = "task_id=" + RT.taskId + "&buckets=" + buckets;
    if (range > 0) {
      const end = new Date();
      const start = new Date(end.getTime() - range * 1000);
      qs += "&start=" + encodeURIComponent(rtIso(start)) +
        "&end=" + encodeURIComponent(rtIso(end));
    } else if (RT.window && RT.window.earliest) {
      qs += "&start=" + encodeURIComponent(RT.window.earliest) +
        "&end=" + encodeURIComponent(RT.window.latest || rtIso(new Date()));
    }

    let data = null;
    try { data = await api("GET", "/api/rt/timeline?" + qs); }
    catch (e) { toast("加载时间轴失败：" + e.message, "danger"); return; }
    RT.timeline = data;
    RT.selectedIdx = -1;
    rtRenderAxis(data);
    rtRenderPoints(data.points || []);
  }

  function rtRenderAxis(data) {
    const axis = $("rtAxis");
    const buckets = (data && data.buckets) || [];
    $("rtAxisStart").textContent = fmtTime(data && data.start);
    $("rtAxisEnd").textContent = fmtTime(data && data.end);
    $("rtAxisMid").textContent = "共 " + ((data && data.total) || 0) + " 个恢复点 · " +
      rtBytes(data && data.total_bytes) +
      (data && data.gaps && data.gaps.length ? " · " + data.gaps.length + " 处缺口" : "");

    if (!buckets.length) {
      axis.innerHTML = '<div class="text-muted small p-3">该时间范围内没有恢复点</div>';
      return;
    }
    let max = 1;
    for (let i = 0; i < buckets.length; i += 1) {
      max = Math.max(max, Number(buckets[i].count || 0));
    }
    axis.innerHTML = buckets.map((b, i) => {
      const c = Number(b.count || 0);
      const h = c > 0 ? Math.max(6, Math.round((c * 100) / max)) : 0;
      const cls = "rt-bucket" + (b.has_gap ? " gap" : "") + (c > 0 ? "" : " empty");
      const title = fmtTime(b.ts) + " | 恢复点 " + c + " 个 | " + rtBytes(b.bytes);
      return '<div class="' + cls + '" data-idx="' + i + '" title="' + esc(title) +
        '"><i style="height:' + h + '%"></i></div>';
    }).join("");

    const els = axis.querySelectorAll(".rt-bucket");
    Array.prototype.forEach.call(els, (el) => {
      el.addEventListener("click", () => rtSelectBucket(Number(el.dataset.idx)));
    });
    if (RT.selectedIdx >= 0) rtHighlight(RT.selectedIdx);
  }

  function rtHighlight(idx) {
    const els = $("rtAxis").querySelectorAll(".rt-bucket");
    Array.prototype.forEach.call(els, (el) => el.classList.remove("selected"));
    if (idx >= 0 && idx < els.length) els[idx].classList.add("selected");
  }

  function rtSelectBucket(idx) {
    const buckets = (RT.timeline && RT.timeline.buckets) || [];
    if (idx < 0 || idx >= buckets.length) return;
    RT.selectedIdx = idx;
    RT.cursorTs = buckets[idx].ts;
    rtHighlight(idx);
    rtPreview();
  }

  function rtRenderPoints(points) {
    const rows = (points || []).slice().reverse();
    if (!rows.length) {
      $("rtPointTable").innerHTML =
        '<tr><td colspan="6" class="text-center text-muted py-4">该时间范围内没有恢复点</td></tr>';
      return;
    }
    $("rtPointTable").innerHTML = rows.map((p) => {
      const badge = '<span class="rt-chip">' + rtKindLabel(p.rp_kind) + "</span>";
      const sim = p.is_simulated ? ' <span class="rt-chip warn">仿真</span>' : "";
      const verify = p.verified
        ? '<span class="rt-chip">已校验</span>'
        : '<span class="text-muted">-</span>';
      return "<tr>" +
        '<td class="rt-mono">' + fmtTime(p.pit_at) + "</td>" +
        "<td>" + badge + sim + "</td>" +
        '<td class="rt-mono">' + esc(p.position_label || "-") + "</td>" +
        "<td>" + rtBytes(p.size_bytes) + "</td>" +
        "<td>" + verify + "</td>" +
        '<td class="text-end"><button class="btn btn-outline-secondary btn-sm" data-ts="' +
        esc(p.pit_at) + '" type="button">选此点</button></td>' +
        "</tr>";
    }).join("");
    const btns = $("rtPointTable").querySelectorAll("button[data-ts]");
    Array.prototype.forEach.call(btns, (b) => {
      b.addEventListener("click", () => {
        RT.cursorTs = b.dataset.ts;
        RT.selectedIdx = rtIdxOfTs(RT.cursorTs);
        rtHighlight(RT.selectedIdx);
        rtPreview();
      });
    });
  }

  function rtIdxOfTs(ts) {
    const buckets = (RT.timeline && RT.timeline.buckets) || [];
    const target = new Date(ts).getTime();
    let best = -1, bestDiff = Infinity;
    for (let i = 0; i < buckets.length; i += 1) {
      const diff = Math.abs(new Date(buckets[i].ts).getTime() - target);
      if (diff < bestDiff) { bestDiff = diff; best = i; }
    }
    return best;
  }

  // ---------- 恢复计划预览 ----------
  async function rtPreview() {
    if (!RT.taskId || !RT.cursorTs) return;
    $("rtCursorTs").textContent = fmtTime(RT.cursorTs);
    $("rtPlanSummary").textContent = "正在生成恢复计划…";
    $("rtPlanChain").textContent = "";
    $("rtPlanGap").textContent = "";
    $("rtRecoverBtn").disabled = true;

    let res = null;
    try {
      res = await api("POST", "/api/rt/preview",
        { task_id: RT.taskId, target_ts: RT.cursorTs });
    } catch (e) {
      RT.plan = null;
      $("rtPlanSummary").innerHTML =
        '<span class="rt-chip err">计划生成失败</span> ' + esc(e.message);
      return;
    }
    const plan = (res && res.plan) || null;
    RT.plan = plan;
    if (!plan) {
      $("rtPlanSummary").innerHTML = '<span class="rt-chip err">无可用恢复计划</span>';
      return;
    }
    $("rtPlanSummary").innerHTML = (plan.complete
      ? '<span class="rt-chip">链完整</span> '
      : '<span class="rt-chip err">链不完整</span> ') + esc(plan.summary || "");
    $("rtPlanChain").innerHTML = "恢复链 " + (plan.chain_length || 0) + " 个节点 · 合计 " +
      rtBytes(plan.total_bytes) +
      (plan.stop_binlog_file
        ? ' · 停止位点 <span class="rt-mono">' + esc(plan.stop_binlog_file) + ":" +
          (plan.stop_binlog_pos || 0) + "</span>"
        : (plan.stop_lsn
          ? ' · 停止 LSN <span class="rt-mono">' + esc(plan.stop_lsn) + "</span>" : ""));
    $("rtPlanGap").innerHTML = plan.gap_reason
      ? '<span class="rt-chip warn">' + esc(plan.gap_reason) + "</span>" : "";
    $("rtRecoverBtn").disabled = false;
  }

  // ---------- 恢复 ----------
  function rtOpenRecoverModal() {
    if (!RT.plan) { toast("请先在时间轴上选择恢复时间点", "danger"); return; }
    $("rtModalTs").textContent = fmtTime(RT.cursorTs);
    $("rtModalSummary").innerHTML = (RT.plan.complete
      ? '<span class="rt-chip">链完整</span> '
      : '<span class="rt-chip err">链不完整：' + esc(RT.plan.gap_reason || "") + "</span> ") +
      esc(RT.plan.summary || "");
    const isFile = RT.plan.kind === "file";
    $("rtModalFileFields").style.display = isFile ? "" : "none";
    $("rtModalDbFields").style.display = isFile ? "none" : "";
    const chain = RT.plan.chain || [];
    $("rtModalChain").textContent = chain.length
      ? chain.map((p, i) => (i + 1) + ". [" + rtKindLabel(p.rp_kind) + "] " +
          fmtTime(p.pit_at) + "  " + (p.position_label || "") + "  " +
          rtBytes(p.size_bytes)).join("\n") +
        (RT.plan.chain_truncated
          ? "\n…（中间节点已省略，共 " + RT.plan.chain_length + " 个）" : "")
      : "（空）";
    if (RT.modal) RT.modal.show();
  }

  function rtCollectTarget() {
    if (RT.plan && RT.plan.kind === "file") {
      return { target_dir: String($("rtTargetDir").value || "").trim() };
    }
    const t = {};
    const host = String($("rtTargetHost").value || "").trim();
    const port = String($("rtTargetPort").value || "").trim();
    const dbn = String($("rtTargetDb").value || "").trim();
    const user = String($("rtTargetUser").value || "").trim();
    const pwd = String($("rtTargetPwd").value || "");
    const dataDir = String($("rtTargetDataDir").value || "").trim();
    if (host) t.host = host;
    if (port) t.port = Number(port);
    if (dbn) t.db = dbn;
    if (user) t.user = user;
    if (pwd) t.password = pwd;
    if (dataDir) t.data_dir = dataDir;
    return t;
  }

  async function rtDoRecover(dryRun) {
    if (!RT.plan) return;
    const target = rtCollectTarget();
    if (RT.plan.kind === "file" && !dryRun && !target.target_dir) {
      toast("请填写目标目录", "danger");
      return;
    }
    const btn = dryRun ? $("rtDryRunBtn") : $("rtConfirmRecoverBtn");
    const old = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> ' +
      (dryRun ? "校验中…" : "恢复中…");
    try {
      const res = await api("POST", "/api/rt/recover", {
        task_id: RT.taskId,
        target_ts: RT.cursorTs,
        target: target,
        dry_run: !!dryRun,
        force: $("rtForceChk").checked,
      });
      toast((res.simulated ? "[仿真] " : "") + (res.message || "恢复完成"),
        res.ok ? "dark" : "danger", 6000);
      if (res.ok && !dryRun && RT.modal) RT.modal.hide();
    } catch (e) {
      toast("恢复失败：" + e.message, "danger", 6000);
    } finally {
      btn.disabled = false;
      btn.innerHTML = old;
    }
  }

  // ---------- 初始化 ----------
  async function initRtTimeline() {
    const modalEl = document.getElementById("rtRecoverModal");
    if (modalEl && window.bootstrap) RT.modal = new bootstrap.Modal(modalEl);
    const createEl = document.getElementById("rtCreateModal");
    if (createEl && window.bootstrap) RT.createModal = new bootstrap.Modal(createEl);

    // ?task_id= 深链：从告警页 / 备份任务页跳转直达（T07）
    try {
      const qs = new URLSearchParams(window.location.search);
      const deepId = Number(qs.get("task_id") || 0);
      if (deepId > 0) {
        RT.deepLinkId = deepId;
        RT.taskId = deepId;
      }
    } catch (e) { RT.deepLinkId = 0; }

    // 创建实时保护任务入口（工具栏按钮 + 空状态按钮 + 弹窗内联动）
    $("rtCreateBtn").addEventListener("click", () => rtOpenCreateModal(RT.deepLinkId));
    $("rtEmptyCreateBtn").addEventListener("click", () => rtOpenCreateModal(RT.deepLinkId));
    $("rtCreateTaskSel").addEventListener("change", () => rtSyncCreateMode());
    $("rtCreateSubmitBtn").addEventListener("click", () => rtSubmitCreate());

    $("rtTaskSelect").addEventListener("change", async (ev) => {
      RT.taskId = Number(ev.target.value || 0);
      const picked = RT.tasks.filter((t) => Number(t.id) === Number(RT.taskId))[0];
      RT.dbType = (picked && picked.db_type) || "file";
      RT.cursorTs = "";
      RT.plan = null;
      $("rtCursorTs").textContent = "尚未选点";
      $("rtPlanSummary").textContent = "在时间轴上点击任意位置以生成恢复计划。";
      $("rtPlanChain").textContent = "";
      $("rtPlanGap").textContent = "";
      $("rtRecoverBtn").disabled = true;
      rtSyncTaskButtons();
      await rtLoadTimeline();
      await rtLoadHealth();
    });
    $("rtRangeSelect").addEventListener("change", () => rtLoadTimeline());
    $("rtBucketSelect").addEventListener("change", () => rtLoadTimeline());
    $("rtRefreshBtn").addEventListener("click", async () => {
      await rtLoadDaemon();
      await rtLoadTasks();
      await rtLoadHealth();
      await rtLoadTimeline();
      toast("已刷新");
    });
    $("rtReconcileBtn").addEventListener("click", async () => {
      try {
        const res = await api("POST", "/api/rt/control", { action: "reconcile" });
        toast(res.message || "对账完成");
        await rtLoadDaemon();
        await rtLoadHealth();
      } catch (e) { toast("对账失败：" + e.message, "danger"); }
    });
    $("rtStartBtn").addEventListener("click", () => rtStartDaemon());
    const stoppedStartBtn = document.getElementById("rtStoppedHintStartBtn");
    if (stoppedStartBtn) stoppedStartBtn.addEventListener("click", () => rtStartDaemon());
    $("rtStopBtn").addEventListener("click", async () => {
      try {
        const res = await api("POST", "/api/rt/control", { action: "stop" });
        toast(res.message || "已停止");
      } catch (e) { toast("停止失败：" + e.message, "danger"); }
      await rtLoadDaemon();
    });
    $("rtTriggerBtn").addEventListener("click", async () => {
      if (!RT.taskId) { toast("请先选择任务", "danger"); return; }
      try {
        const res = await api("POST", "/api/rt/tasks/" + RT.taskId + "/trigger",
          { reason: "manual" });
        toast(res.message || "已触发一次捕获", res.ok ? "dark" : "danger", 5000);
      } catch (e) { toast("触发失败：" + e.message, "danger"); }
      await rtLoadTimeline();
      await rtLoadHealth();
    });
    $("rtRecoverBtn").addEventListener("click", () => rtOpenRecoverModal());
    $("rtLatestBtn").addEventListener("click", () => {
      const buckets = (RT.timeline && RT.timeline.buckets) || [];
      for (let i = buckets.length - 1; i >= 0; i -= 1) {
        if (Number(buckets[i].count || 0) > 0) { rtSelectBucket(i); return; }
      }
      toast("当前范围内没有恢复点", "danger");
    });
    $("rtDryRunBtn").addEventListener("click", () => rtDoRecover(true));
    $("rtConfirmRecoverBtn").addEventListener("click", () => rtDoRecover(false));

    // 键盘微调选点（← / →）
    document.addEventListener("keydown", (ev) => {
      if (document.body.dataset.page !== "rt_timeline") return;
      if (RT.selectedIdx < 0) return;
      const tag = (ev.target && ev.target.tagName) || "";
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      if (ev.key === "ArrowLeft") { ev.preventDefault(); rtSelectBucket(RT.selectedIdx - 1); }
      else if (ev.key === "ArrowRight") { ev.preventDefault(); rtSelectBucket(RT.selectedIdx + 1); }
    });

    await rtLoadDaemon();
    await rtLoadTasks();
    await rtLoadHealth();
    await rtLoadTimeline();

    // 深链任务尚未开启实时保护 → 直接弹出创建向导并预选该任务
    if (RT.deepLinkId > 0 &&
        !RT.tasks.some((t) => Number(t.id) === Number(RT.deepLinkId))) {
      await rtOpenCreateModal(RT.deepLinkId);
    }

    RT.refreshTimer = setInterval(() => {
      rtLoadDaemon();
      rtLoadHealth();
    }, 15000);
  }

  // ------------------------- 启动 -------------------------
  const page = document.body.dataset.page;
  window.addEventListener("DOMContentLoaded", async () => {
    bindSidebar();
    bindConfirmModal();
    try {
      const meta = await api("GET", "/api/meta");
      // 同步到 BKP.META（fillDbTypeSelect 等函数依赖此全局）
      BKP.META = Object.assign(BKP.META, meta);
      META = BKP.META;
    } catch (e) { /* 未登录等 */ }
    try {
      if (page === "dashboard") await initDashboard();
      else if (page === "tasks") await initTasks();
      else if (page === "records") await initRecords();
      else if (page === "file_backup") await initFileBackup();
      // sync 页面由独立的 static/js/sync.js 管理，不再调用 app.js 中的旧版 initSync，
      // 避免请求 /api/sync/tasks、/api/sync/records 等旧路径造成 404。
      else if (page === "restore_records") await initRestoreRecords();
      else if (page === "restore") await initRestore();
      else if (page === "inspection") await initInspection();
      else if (page === "deploy") await initDeploy();
      else if (page === "vdb") await initVdb();
      else if (page === "drills") await initDrills();
      else if (page === "settings") await initSettings();
      else if (page === "storage") await initStorage();
      else if (page === "protection") await initProtection();
      else if (page === "migration") { await initMigration(); await initDbMigrate(); }
      else if (page === "clone") await initClone();
      else if (page === "dr-link") await initDrLink();
      else if (page === "alert") await initAlert();
      else if (page === "agent") await initAgent();
      else if (page === "datamining") await initDataMining();
      else if (page === "rt_timeline") await initRtTimeline();
    } catch (e) { toast(e.message, "danger"); }
  });

  // 显示"JS 加载成功"指示器（右下角小条）
  setTimeout(() => {
    const box = document.getElementById("jsLoadedBox");
    if (box) box.style.display = "block";
  }, 100);
  console.log("[app.js] DOMContentLoaded handler registered");

  // =======================  (V) =======================
  window.loadDrills = async () => {
    const d = await api("GET", "/api/drills");
    $("drillTable").innerHTML = d.map(dr => {
      const scoreBadge = dr.score >= 90 ? '<span class="badge bg-success">' + dr.score + '</span>'
                       : dr.score >= 60 ? '<span class="badge bg-warning text-dark">' + dr.score + '</span>'
                       : dr.score > 0 ? '<span class="badge bg-danger">' + dr.score + '</span>'
                       : '-';
      return '<tr>' +
        '<td>' + dr.id + '</td>' +
        '<td>' + esc(dr.name) + '</td>' +
        '<td>' + (dr.task_id ? esc(window._taskNames[dr.task_id] || dr.task_id) : '-') + '</td>' +
        '<td>' + esc(dr.drill_type === 'full_recovery' ? '...' : dr.drill_type === 'partial' ? '...' : '...') + '</td>' +
        '<td>' + statusBadge(dr.status) + '</td>' +
        '<td>' + scoreBadge + '</td>' +
        '<td>' + (dr.rto_actual_sec != null ? dr.rto_actual_sec.toFixed(1) + 's' : '-') + '</td>' +
        '<td>' + (dr.rpo_actual_sec != null ? (dr.rpo_actual_sec > 3600 ? (dr.rpo_actual_sec/3600).toFixed(1)+'h' : dr.rpo_actual_sec.toFixed(1)+'s') : '-') + '</td>' +
        '<td>' + (fmtTime(dr.finished_at) || '-') + '</td>' +
        '<td class="text-end">' +
          (dr.status === 'pending' ? '<button class="btn btn-sm btn-outline-success" onclick="runDrill(' + dr.id + ')">...</button>' : '') +
          '<button class="btn btn-sm btn-outline-info" onclick="viewDrillReport(' + dr.id + ')">...</button>' +
          '<button class="btn btn-sm btn-outline-danger" onclick="delDrill(' + dr.id + ')">...</button>' +
        '</td></tr>';
    }).join("") || '<tr><td colspan="10" class="text-muted text-center">...</td></tr>';
  };

  window.runDrill = async (id) => {
    try { await api("POST", "/api/drills/" + id + "/run"); toast("..."); await loadDrills(); setTimeout(loadDrills, 2000); }
    catch (e) { toast(e.message, "danger"); }
  };

  window.viewDrillReport = async (id) => {
    const d = await api("GET", "/api/drills/" + id);
    const report = d.report ? JSON.parse(d.report) : null;
    $("drillReportContent").textContent = report
      ? ':: ' + report.score + '/100 | RTO: ' + (report.rto_sec||0) + 's | RPO: ' + (report.rpo_sec||0)
        + 's\n' + report.backup_count + '  (' + report.simulated_count + ' )\n: ' + (report.issues||[]).join(' | ')
        + '\n: ' + (report.recommendation || '')
      : (d.issues_found || d.report || d.notes || '...');
    const drmEl = document.getElementById("drillReportModal");
    if (drmEl) new bootstrap.Modal(drmEl).show();
  };

  window.delDrill = async (id) => {
    const ok = await confirmDialog({title:"...",message:"...?",confirmText:"...",danger:true});
    if (!ok) return;
    try { await api("DELETE", "/api/drills/" + id); toast("..."); await loadDrills(); }
    catch (e) { toast(e.message, "danger"); }
  };

  let drillModal;
  async function initDrills() {
    const dmEl = document.getElementById("drillModal");
    if (dmEl) drillModal = new bootstrap.Modal(dmEl);
    const tasks = await api("GET", "/api/tasks");
    window._taskNames = Object.fromEntries(tasks.map(t => [t.id, t.name]));
    const taskOpts = tasks.map(t => '<option value="' + t.id + '">' + esc(t.name) + ' (' + esc(t.db_type) + ')</option>').join("");
    $("drill_task_id").innerHTML = taskOpts;
    // 趋势/基线/排程 的任务下拉
    const tf = $("trendTaskFilter"), bt = $("baselineTask"), st = $("schTargets");
    if (tf) tf.innerHTML = '<option value="">全部任务</option>' + taskOpts;
    if (bt) bt.innerHTML = tasks.length ? taskOpts : '<option value="">无可分析任务</option>';
    if (st) st.innerHTML = taskOpts;
    $("newDrillBtn").onclick = () => {
      $("drill_name").value = ""; $("drill_type").value = "full_recovery";
      $("drill_scheduled").value = ""; $("drill_notes").value = "";
      drillModal.show();
    };
    $("drillSaveBtn").onclick = async () => {
      const data = {
        name: $("drill_name").value, task_id: Number($("drill_task_id").value) || null,
        drill_type: $("drill_type").value,
        scheduled_at: $("drill_scheduled").value || null,
        notes: $("drill_notes").value,
      };
      if (!data.name) { toast("请输入演练名称", "warning"); return; }
      try { const r = await api("POST", "/api/drills", data); toast("已创建演练 #" + r.id); drillModal.hide(); await loadDrills(); }
      catch (e) { toast(e.message, "danger"); }
    };
    // 绑定排程按钮
    const saveBtn = $("schSaveBtn"), runBtn = $("schRunBtn");
    if (saveBtn) saveBtn.onclick = saveDrillSchedule;
    if (runBtn) runBtn.onclick = runDrillScheduleNow;
    // 加载趋势 / 基线 / 排程
    await loadDrills();
    loadDrillTrend();
    if (tasks.length) { $("baselineTask").value = tasks[0].id; loadDrillBaseline(); }
    loadDrillSchedule();
  }

  // 格式化时长（秒 → 人类可读）
  function fmtDurShort(sec) {
    if (sec == null) return "-";
    sec = Number(sec);
    if (sec >= 86400) return (sec / 86400).toFixed(1) + "d";
    if (sec >= 3600) return (sec / 3600).toFixed(1) + "h";
    if (sec >= 60) return (sec / 60).toFixed(1) + "m";
    return sec.toFixed(1) + "s";
  }

  window.loadDrillTrend = async () => {
    try {
      const taskId = $("trendTaskFilter") ? ($("trendTaskFilter").value || "") : "";
      const days = $("trendDays") ? ($("trendDays").value || 90) : 90;
      const url = "/api/drills/trend?days=" + encodeURIComponent(days) +
        (taskId ? "&task_id=" + encodeURIComponent(taskId) : "");
      const d = await api("GET", url);
      renderTrendChart(d.points || []);
      const s = d.summary || {};
      $("trendSummary").innerHTML = "样本 " + (s.count || 0) + " 次 · 平均 RTO " +
        fmtDurShort(s.avg_rto) + " · 平均 RPO " + fmtDurShort(s.avg_rpo) +
        " · 平均评分 " + (s.avg_score != null ? s.avg_score : "-");
    } catch (e) { toast("加载趋势失败: " + e.message, "danger"); }
  };

  function renderTrendChart(points) {
    const host = $("trendChart");
    if (!host) return;
    if (!points || !points.length) {
      host.innerHTML = '<div class="text-muted text-center py-5">暂无演练数据，执行演练后将自动生成趋势</div>';
      return;
    }
    const W = 680, H = 240, padL = 44, padR = 16, padT = 14, padB = 26;
    const innerW = W - padL - padR, innerH = H - padT - padB;
    const maxV = Math.max(1, ...points.map(p => Math.max(p.rto || 0, p.rpo || 0)));
    const n = points.length;
    const x = i => padL + (n === 1 ? innerW / 2 : innerW * i / (n - 1));
    const y = v => padT + innerH * (1 - (v || 0) / maxV);
    const line = (key, color) => {
      const pts = points.map((p, i) => x(i).toFixed(1) + "," + y(p[key]).toFixed(1)).join(" ");
      const dots = points.map((p, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(p[key]).toFixed(1)}" r="2.5" fill="${color}"/>`).join("");
      return `<polyline fill="none" stroke="${color}" stroke-width="2" points="${pts}"/>${dots}`;
    };
    const svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none" style="display:block">` +
      `<line x1="${padL}" y1="${padT}" x2="${padL}" y2="${padT + innerH}" stroke="#CBD5E1"/>` +
      `<line x1="${padL}" y1="${padT + innerH}" x2="${W - padR}" y2="${padT + innerH}" stroke="#CBD5E1"/>` +
      `<text x="4" y="${padT + 8}" font-size="10" fill="#64748B">${fmtDurShort(maxV)}</text>` +
      `<text x="4" y="${padT + innerH}" font-size="10" fill="#64748B">0</text>` +
      line("rto", "#0D9488") + line("rpo", "#0F766E") +
      `</svg>`;
    host.innerHTML = svg;
  }

  window.loadDrillBaseline = async () => {
    const host = $("baselineCards");
    if (!host) return;
    const tid = $("baselineTask") ? ($("baselineTask").value || "") : "";
    if (!tid) { host.innerHTML = '<div class="text-muted small">请先选择任务</div>'; return; }
    try {
      const d = await api("GET", "/api/drills/baseline?task_id=" + encodeURIComponent(tid));
      if (!d.ok) { host.innerHTML = '<div class="text-muted small">' + esc(d.error || "无数据") + '</div>'; return; }
      const b = d.baseline || {};
      const verdictBadge = (v) => v === "达标" || v === "达标(近实时)"
        ? '<span class="badge bg-success">达标</span>'
        : v === "超标" ? '<span class="badge bg-danger">超标</span>'
        : '<span class="badge bg-secondary">无数据</span>';
      const card = (title, baseVal, target, unit, verdict) =>
        '<div class="border rounded p-2 mb-2" style="border-color:var(--primary-light)">' +
          '<div class="d-flex justify-content-between align-items-center">' +
            '<span class="small text-muted">' + title + '</span>' + verdictBadge(verdict) +
          '</div>' +
          '<div class="fw-bold" style="color:var(--primary)">' + (baseVal != null ? baseVal : "—") +
            ' <span class="small fw-normal text-muted">基线 / 目标 ' + (target != null ? target : "—") + unit + '</span></div>' +
        '</div>';
      host.innerHTML =
        card("RTO 恢复耗时", fmtDurShort(b.rto ? b.rto.mean : null), fmtDurShort(d.rto_target_sec), "", d.verdict.rto) +
        card("RPO 数据丢失", fmtDurShort(b.rpo ? b.rpo.mean : null), fmtDurShort(d.rpo_target_sec), "", d.verdict.rpo) +
        card("评分基线", b.score ? b.score.mean : "—", "≥60", " 分", d.verdict.score) +
        '<div class="small text-muted">保护等级：' + esc(d.protection_level || "-") + ' · 任务：' + esc(d.task_name || "-") + '</div>';
    } catch (e) { host.innerHTML = '<div class="text-muted small">加载失败: ' + esc(e.message) + '</div>'; }
  };

  window.loadDrillSchedule = async () => {
    try {
      const d = await api("GET", "/api/drills/schedule");
      const en = $("schEnabled"), fr = $("schFrequency"), nr = $("schNextRun"), st = $("schTargets");
      if (en) en.checked = !!d.enabled;
      if (fr) fr.value = d.frequency || "quarterly";
      if (nr) nr.textContent = d.next_run ? fmtTime(d.next_run) : "未设置";
      if (st && Array.isArray(d.target_task_ids)) {
        Array.from(st.options).forEach(o => { o.selected = d.target_task_ids.includes(Number(o.value)); });
      }
    } catch (e) { toast("加载排程失败: " + e.message, "danger"); }
  };

  async function saveDrillSchedule() {
    try {
      const st = $("schTargets");
      const targetIds = st ? Array.from(st.selectedOptions).map(o => Number(o.value)) : [];
      const payload = {
        enabled: $("schEnabled") ? $("schEnabled").checked : false,
        frequency: $("schFrequency") ? $("schFrequency").value : "quarterly",
        target_task_ids: targetIds,
      };
      const r = await api("POST", "/api/drills/schedule", payload);
      toast("排程已保存" + (r.config && r.config.enabled ? "（已启用）" : "（未启用）"));
      loadDrillSchedule();
    } catch (e) { toast("保存失败: " + e.message, "danger"); }
  }

  async function runDrillScheduleNow() {
    try {
      const r = await api("POST", "/api/drills/schedule/run");
      toast("已触发排程演练，执行 " + (r.count || 0) + " 个");
      await loadDrills(); loadDrillTrend(); loadDrillBaseline();
    } catch (e) { toast("执行失败: " + e.message, "danger"); }
  }

  // ==================== 数据价值挖掘（脱敏导出） ====================
  const DM_DEFAULT_COLUMNS = ["id", "name", "phone", "email", "id_card",
    "address", "bank_card", "amount", "created_at"];

  // 运行时状态：记录 / 候选表 / 规则模板 / 当前最终规则
  const DM = {
    sources: [], dbType: "", dbLabel: "",
    tables: [], activeTable: "",
    templates: { minimal: {}, standard: {}, strict: {} },
    activeTemplate: "strict",
    currentRules: {},
  };

  function _dmRuleBadgeHtml(rule) {
    const color = {
      mask: "bg-warning text-dark", hash: "bg-danger",
      fake: "bg-info text-dark", drop: "bg-secondary",
    }[rule] || "bg-light text-dark";
    return '<span class="badge ' + color + '">' + esc(rule) + '</span>';
  }

  function _dmRenderRulePreview() {
    const box = $("dmRulePreview");
    if (!box) return;
    const cols = Array.from($("dmColumns").selectedOptions).map((o) => o.value);
    if (!cols.length) {
      box.innerHTML = '<span class="text-muted">请先选择导出列。</span>';
      return;
    }
    const desc = {
      mask: "部分打码（138****1234）",
      hash: "不可逆哈希（sha256 前16位）",
      drop: "删除该列",
      fake: "仿真替换（保留可读，去除真实信息）",
      none: "不脱敏（保留原值）",
    };
    box.innerHTML = cols.map((c) => {
      const r = DM.currentRules[c] || "none";
      return '<div class="d-flex align-items-center gap-2 border-bottom py-1">' +
        '<code class="text-truncate" style="max-width:160px">' + esc(c) + '</code>' +
        _dmRuleBadgeHtml(r) +
        '<span class="text-muted small">' + (desc[r] || "") + '</span>' +
      '</div>';
    }).join("");
  }

  async function _dmRecalcRules() {
    const cols = Array.from($("dmColumns").selectedOptions).map((o) => o.value);
    if (!cols.length) { DM.currentRules = {}; _dmRenderRulePreview(); return; }
    let userRules = {};
    try {
      const ta = $("dmMaskRules");
      if (ta && ta.value.trim()) userRules = JSON.parse(ta.value);
    } catch (e) { /* 解析失败忽略，走自动 */ }
    try {
      const r = await api("POST", "/api/datamining/preview-rules",
                          { columns: cols, mask_rules: userRules });
      DM.currentRules = {};
      (r.columns || []).forEach((it) => { DM.currentRules[it.column] = it.rule; });
    } catch (e) {
      // 后端不可用时退化为本地 PII 识别
      const pii = { id_card: "mask", idcard: "mask", bank_card: "mask",
        credit_card: "mask", card_no: "mask", phone: "mask", mobile: "mask",
        email: "mask", mail: "mask", id_number: "mask", identity: "mask",
        ssn: "mask", name: "fake", username: "fake", real_name: "fake",
        address: "fake", location: "fake", password: "hash", pwd: "hash",
        secret: "hash", token: "hash" };
      DM.currentRules = {};
      cols.forEach((c) => {
        const cl = c.toLowerCase();
        let r = "none";
        for (const k in pii) { if (cl.indexOf(k) >= 0) { r = pii[k]; break; } }
        DM.currentRules[c] = r;
      });
    }
    _dmRenderRulePreview();
  }

  function _dmApplyTemplate(tpl) {
    const tplRules = DM.templates[tpl] || {};
    const cols = Array.from($("dmColumns").selectedOptions).map((o) => o.value);
    const rules = {};
    cols.forEach((c) => { rules[c] = "none"; });
    Object.keys(tplRules).forEach((k) => {
      const matched = cols.filter((c) => c.toLowerCase().indexOf(k) >= 0);
      matched.forEach((c) => { rules[c] = tplRules[k]; });
    });
    const ta = $("dmMaskRules");
    if (ta) ta.value = JSON.stringify(rules, null, 2);
    DM.activeTemplate = tpl;
    document.querySelectorAll("#dmRuleTemplates button").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-tpl") === tpl);
    });
    const desc = {
      minimal: "最小：仅身份证/银行卡/密码脱敏。",
      standard: "标准：手机/邮箱/身份证/银行卡打码，姓名/地址仿真替换。",
      strict: "严格：所有可能含个人信息列均脱敏；IP/Token 哈希不可逆。",
    }[tpl] || "";
    const descEl = $("dmTplDesc");
    if (descEl) descEl.textContent = desc;
    _dmRecalcRules();
  }

  function _dmRenderTables() {
    const sel = $("dmTable");
    if (!sel) return;
    if (!DM.tables.length) {
      sel.innerHTML = '<option value="">该类型暂无推荐表（可手动添加列）</option>';
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    sel.innerHTML = '<option value="">— 请选择目标表 —</option>' +
      DM.tables.map((t) => '<option value="' + esc(t.name) + '">' + esc(t.name) +
        '（' + t.columns.length + ' 列）</option>').join("");
  }

  function _dmRenderColumns(columns) {
    const sel = $("dmColumns");
    if (!sel) return;
    sel.innerHTML = columns.map((c) =>
      '<option value="' + esc(c) + '" selected>' + esc(c) + '</option>').join("");
  }

  async function _dmOnSourceChange() {
    const src = $("dmSource");
    const id = src && src.value;
    const label = $("dmSchemaLabel");
    if (!id) {
      if (label) label.textContent = "通用";
      DM.tables = []; DM.dbType = ""; DM.dbLabel = "";
      _dmRenderTables();
      return;
    }
    try {
      const sug = await api("GET", "/api/datamining/records/" + id + "/suggest");
      DM.dbType = sug.db_type || "";
      DM.dbLabel = sug.label || "通用（默认列）";
      DM.tables = sug.tables || [];
      if (label) label.textContent = DM.dbLabel;
      _dmRenderTables();
      if (DM.tables.length) {
        $("dmTable").value = DM.tables[0].name;
        _dmRenderColumns(DM.tables[0].columns);
      } else {
        _dmRenderColumns(sug.default || []);
      }
      _dmRecalcRules();
    } catch (e) {
      if (label) label.textContent = "加载失败";
      toast("加载推荐列失败: " + e.message, "danger");
    }
  }

  async function initDataMining() {
    const src = $("dmSource");
    if (src) {
      try {
        const recs = await api("GET", "/api/records");
        DM.sources = recs || [];
        src.innerHTML = '<option value="">— 请选择备份记录 —</option>' +
          DM.sources.map((r) => '<option value="' + r.id + '">' + fmtRecordLabel(r) + '</option>').join("");
      } catch (e) { src.innerHTML = '<option value="">加载失败</option>'; }
    }
    // 加载规则模板
    try {
      const t = await api("GET", "/api/datamining/rule-templates");
      DM.templates = {};
      Object.keys(t || {}).forEach((k) => { DM.templates[k] = (t[k] || {}).rules || {}; });
    } catch (e) { DM.templates = { minimal: {}, standard: {}, strict: {} }; }
    // 绑定交互
    if (src) src.onchange = _dmOnSourceChange;
    const tbl = $("dmTable");
    if (tbl) tbl.onchange = () => {
      const t = DM.tables.find((x) => x.name === tbl.value);
      if (t) _dmRenderColumns(t.columns);
      _dmRecalcRules();
    };
    const cols = $("dmColumns");
    if (cols) cols.onchange = _dmRecalcRules;
    const ta = $("dmMaskRules");
    if (ta) ta.oninput = _dmRecalcRules;
    const addBtn = $("dmAddColumn");
    if (addBtn) addBtn.onclick = () => {
      const name = prompt("请输入新列名：");
      if (!name) return;
      const sel = $("dmColumns");
      const opt = document.createElement("option");
      opt.value = name; opt.textContent = name; opt.selected = true;
      sel.appendChild(opt); _dmRecalcRules();
    };
    const allBtn = $("dmSelectAll");
    if (allBtn) allBtn.onclick = () => {
      Array.from($("dmColumns").options).forEach((o) => { o.selected = true; });
      _dmRecalcRules();
    };
    const clrBtn = $("dmClearColumns");
    if (clrBtn) clrBtn.onclick = () => {
      Array.from($("dmColumns").options).forEach((o) => { o.selected = false; });
      _dmRecalcRules();
    };
    document.querySelectorAll("#dmRuleTemplates button").forEach((b) => {
      b.onclick = () => _dmApplyTemplate(b.getAttribute("data-tpl"));
    });
    const exportBtn = $("dmExportBtn");
    if (exportBtn) exportBtn.onclick = dmExport;
    // 初始化：默认列与"严格"模板
    _dmRenderColumns(DM_DEFAULT_COLUMNS);
    _dmApplyTemplate("strict");
    await loadExports();
  }

  // 保留 genDefaultRules 兼容（部分旧代码可能引用）
  window.genDefaultRules = () => _dmRecalcRules();

  async function dmExport() {
    const src = $("dmSource");
    if (!src || !src.value) { toast("请先选择来源备份记录", "warning"); return; }
    const cols = $("dmColumns");
    const columns = cols ? Array.from(cols.selectedOptions).map((o) => o.value) : [];
    if (!columns.length) { toast("请至少选择一个导出列", "warning"); return; }
    let mask_rules = undefined;
    const ta = $("dmMaskRules");
    if (ta && ta.value.trim()) {
      try { mask_rules = JSON.parse(ta.value); }
      catch (e) { toast("脱敏规则不是合法 JSON", "warning"); return; }
    }
    const rowCount = Math.max(1, Math.min(2000, Number($("dmRowCount").value) || 50));
    const payload = { source_record_id: Number(src.value), columns, mask_rules, row_count: rowCount };
    try {
      const r = await api("POST", "/api/datamining/export", payload);
      toast("脱敏导出成功，文件 #" + r.id + "（" + r.row_count + " 行）");
      await loadExports();
    } catch (e) { toast("导出失败: " + e.message, "danger"); }
  }

  window.loadExports = async () => {
    const tb = $("dmExportTable");
    if (!tb) return;
    try {
      const list = await api("GET", "/api/datamining/exports");
      if (!list.length) {
        tb.innerHTML = '<tr><td colspan="7" class="text-muted text-center">暂无导出记录</td></tr>';
        return;
      }
      tb.innerHTML = list.map(e => {
        const cols = Array.isArray(e.columns) ? e.columns.join(", ") : (e.columns || "-");
        const rules = e.mask_rules && typeof e.mask_rules === "object"
          ? Object.entries(e.mask_rules).map(([k, v]) => k + ":" + v).join(" ") : "-";
        return '<tr>' +
          '<td>' + e.id + '</td>' +
          '<td>' + (e.source_record_id != null ? '#' + e.source_record_id : '-') + '</td>' +
          '<td><span class="text-truncate d-inline-block" style="max-width:220px">' + esc(cols) + '</span></td>' +
          '<td><span class="text-truncate d-inline-block" style="max-width:200px">' + esc(rules) + '</span></td>' +
          '<td>' + (e.row_count != null ? e.row_count : '-') + '</td>' +
          '<td>' + (fmtTime(e.created_at) || '-') + '</td>' +
          '<td class="text-end">' +
            '<a class="btn btn-sm btn-outline-primary me-1" href="/api/datamining/exports/' + e.id + '/download"><i class="bi bi-download"></i> 下载</a>' +
            '<button class="btn btn-sm btn-outline-danger" onclick="delExport(' + e.id + ')"><i class="bi bi-trash"></i></button>' +
          '</td></tr>';
      }).join("");
    } catch (e) { tb.innerHTML = '<tr><td colspan="7" class="text-danger">加载失败: ' + esc(e.message) + '</td></tr>'; }
  };

  window.delExport = async (id) => {
    const ok = await confirmDialog({ title: "删除导出", message: "确认删除该脱敏导出记录及文件？", confirmText: "删除", danger: true });
    if (!ok) return;
    try { await api("DELETE", "/api/datamining/exports/" + id); toast("已删除"); await loadExports(); }
    catch (e) { toast("删除失败: " + e.message, "danger"); }
  };

  // ==================== 存储管理（三级备份体系） ====================
  const TYPE_ICONS = { local: "bi-hdd", minio: "bi-cloud-arrow-up", s3: "bi-cloud-check" };
  const TYPE_BADGES = { local: "bg-secondary", minio: "bg-warning text-dark", s3: "bg-info" };
  const TIER_LABELS = { 1: "L1 本地", 2: "L2 热数据", 3: "L3 冷归档" };
  let storageModal;
  let editingStorageId = null;
  let storageUsage = null;

  // 相对时间（“3 分钟前检查”），借鉴 Databasus 的健康检查时间展示
  const fromNow = (iso) => {
    if (!iso) return "尚未测试";
    const t = new Date(iso.includes("T") ? iso : iso.replace(" ", "T"));
    if (isNaN(t.getTime())) return "已测试";
    const diff = (Date.now() - t.getTime()) / 1000;
    if (diff < 0) return "刚刚检查";
    if (diff < 60) return "刚刚检查";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前检查";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前检查";
    return Math.floor(diff / 86400) + " 天前检查";
  };

  // 存储卡片状态徽章
  const statusPill = (t) => {
    if (!t.enabled) return '<span class="badge bg-secondary">已禁用</span>';
    if (t.last_error) return '<span class="badge bg-danger" title="' + esc(t.last_error) + '">异常</span>';
    if (t.last_test_at) return '<span class="badge bg-success">已连接</span>';
    return '<span class="badge bg-warning text-dark">未测试</span>';
  };

  window.toggleReveal = (id) => {
    const i = $(id);
    i.type = i.type === "password" ? "text" : "password";
  };

  async function loadTargets() {
    try {
      const data = await api("GET", "/api/storage/targets");
      renderTargetList(data.targets || []);
      updateTierOverview(data.targets || []);
    } catch (e) {
      toast("加载存储目标失败: " + e.message, "danger");
    }
    // 加载依赖状态
    try {
      const dep = await api("GET", "/api/storage/types");
      if (!dep.dependencies.minio || !dep.dependencies.s3) {
        $("depWarning").classList.remove("d-none");
        $("depMsg").textContent = "MinIO/S3 SDK 未安装，MinIO 和 S3 存储功能不可用。请执行：pip install minio";
      } else {
        $("depWarning").classList.add("d-none");
      }
    } catch (e) { /* ignore */ }
  }

  // 将目标卡片渲染到对应层级内部（不再有独立列表区）
  function renderTargetList(targets) {
    const emptyEl = $("emptyState");
    if (!targets.length) {
      // 清空各层级的目标区
      for (let tier = 1; tier <= 3; tier++) {
        const tEl = $("tier" + tier + "Targets");
        if (tEl) tEl.innerHTML = "";
      }
      emptyEl.classList.remove("d-none");
      return;
    }
    emptyEl.classList.add("d-none");

    // 按tier分组渲染到对应层级的 tierNTargets 容器
    for (let tier = 1; tier <= 3; tier++) {
      const container = $("tier" + tier + "Targets");
      if (!container) continue;
      const tierTargets = targets.filter(t => Number(t.tier) === tier);
      if (!tierTargets.length) {
        container.innerHTML = '<div class="text-muted small text-center py-2">暂无配置</div>';
        continue;
      }
      container.innerHTML = '<div class="tier-target-list">' +
        tierTargets.map(t => {
          const icon = TYPE_ICONS[t.type] || "bi-hdd";
          const statusOk = t.enabled && !t.last_error;
          return '<div class="tier-target-item' + (!t.enabled ? ' opacity-50' : '') + '">' +
            '<div class="d-flex justify-content-between align-items-start">' +
              '<div class="flex-grow-1 min-w-0">' +
                '<div class="d-flex align-items-center gap-1 mb-1">' +
                  '<strong class="text-truncate">' + esc(t.name) + '</strong>' +
                  (t.is_default ? ' <span class="badge bg-primary" style="font-size:0.6em">默认</span>' : '') +
                  statusPill(t) +
                '</div>' +
                (t.endpoint ? '<div class="small text-muted text-truncate"><i class="bi bi-link-45deg me-1"></i>' + esc(t.endpoint) + '</div>' : '') +
                (t.bucket ? '<div class="small text-muted text-truncate"><i class="bi bi-bucket me-1"></i>' + esc(t.bucket) + '</div>' : '') +
                '<div class="d-flex justify-content-between mt-1">' +
                  (t.last_test_at ? '<small class="text-muted">' + fromNow(t.last_test_at) + '</small>' : '') +
                '</div>' +
              '</div>' +
              '<div class="dropdown ms-2 flex-shrink-0">' +
                '<button type="button" class="btn btn-sm btn-link text-muted p-0" data-bs-toggle="dropdown" aria-expanded="false"><i class="bi bi-three-dots-vertical"></i></button>' +
                '<ul class="dropdown-menu dropdown-menu-end">' +
                  '<li><a class="dropdown-item" href="#" onclick="editStorage(' + t.id + ');return false;"><i class="bi bi-pencil me-1"></i>编辑</a></li>' +
                  '<li><a class="dropdown-item" href="#" onclick="testStorageById(' + t.id + ');return false;"><i class="bi bi-plug me-1"></i>测试连接</a></li>' +
                  (!t.is_default ? '<li><a class="dropdown-item" href="#" onclick="setDefaultStorage(' + t.id + ');return false;"><i class="bi bi-star me-1"></i>设为默认</a></li>' : '') +
                  '<li><hr class="dropdown-divider"></li>' +
                  '<li><a class="dropdown-item text-danger" href="#" onclick="deleteStorage(' + t.id + ',\'' + esc(t.name) + '\');return false;"><i class="bi bi-trash me-1"></i>删除</a></li>' +
                '</ul>' +
              '</div>' +
            '</div>' +
          '</div>';
        }).join("") +
      '</div>';
    }
  }

  function updateTierOverview(targets) {
    for (let tier = 1; tier <= 3; tier++) {
      const el = $("tier" + tier + "Stat");
      if (!el) continue;
      const tierTargets = targets.filter(t => Number(t.tier) === tier);
      const enabled = tierTargets.filter(t => t.enabled).length;
      const errors = tierTargets.filter(t => t.enabled && t.last_error).length;
      el.innerHTML = enabled > 0
        ? '<span class="text-success"><i class="bi bi-check-circle-fill me-1"></i>' + enabled + ' 个目标</span>'
          + (errors ? ' <span class="text-danger"><i class="bi bi-exclamation-triangle me-1"></i>' + errors + ' 个异常</span>' : '')
        : '<span class="text-muted"><i class="bi bi-dash-circle me-1"></i>未配置</span>';
    }
    // L1 本地磁盘用量可视化（TOP 5）
    const uEl = $("tier1Usage");
    if (uEl) {
      if (storageUsage && !storageUsage.error) {
        const pct = storageUsage.used_percent;
        const barCls = pct >= 95 ? "bg-danger" : (pct >= 85 ? "bg-warning" : "bg-success");
        const totalGb = (storageUsage.total_bytes / 1073741824).toFixed(0);
        const usedGb = (storageUsage.used_bytes / 1073741824).toFixed(0);
        uEl.innerHTML = '<div class="progress" style="height:6px">' +
          '<div class="progress-bar ' + barCls + '" style="width:' + pct + '%"></div></div>' +
          '<small class="text-muted">已用 ' + pct + '% · ' + usedGb + '/' + totalGb + ' GB</small>';
      } else {
        uEl.innerHTML = storageUsage && storageUsage.error ? '<small class="text-muted">容量获取失败</small>' : '';
      }
    }
  }

  window.loadTargets = loadTargets;
  window.openStorageModal = () => {
    editingStorageId = null;
    $("storageModalTitle").textContent = "新增存储目标";
    $("storageForm").reset();
    $("st_id").value = "";
    $("stTestBtn").classList.add("d-none");
    $("stTestResult").innerHTML = "";
    onStorageTypeChange();
    storageModal.show();
  };

  window.editStorage = async (id) => {
    try {
      $("stTestResult").innerHTML = "";
      const d = await api("GET", "/api/storage/targets/" + id);
      editingStorageId = id;
      $("storageModalTitle").textContent = "编辑存储目标 - " + d.name;
      $("st_id").value = d.id;
      $("st_name").value = d.name || "";
      $("st_type").value = d.type || "";
      $("st_remark").value = d.remark || "";
      $("st_enabled").checked = !!d.enabled;
      $("st_default").checked = !!d.is_default;
      // 密码不回显，留空表示不修改
      $("st_secret_key").value = "";
      // 按类型填充字段
      onStorageTypeChange();
      if (d.type === "local") $("st_endpoint_local").value = d.endpoint || "./backups";
      else {
        $("st_endpoint_s3").value = d.endpoint || "";
        $("st_region").value = d.region || "";
        $("st_access_key").value = d.access_key || "";
        $("st_bucket").value = d.bucket || "";
        $("st_prefix").value = d.prefix || "";
        if (d.extra_options && typeof d.extra_options === "object") {
          if (d.extra_options.storage_class) $("st_storage_class").value = d.extra_options.storage_class;
          if (d.extra_options.insecure) $("st_insecure").checked = true;
        }
      }
      $("stTestBtn").classList.remove("d-none");
      storageModal.show();
    } catch (e) { toast(e.message, "danger"); }
  };

  window.onStorageTypeChange = () => {
    const type = $("st_type").value;
    document.querySelectorAll(".storage-type-fields").forEach(el => el.classList.add("d-none"));
    if (type === "local") $("st_fields_local").classList.remove("d-none");
    else if (type === "minio" || type === "s3") {
      $("st_fields_s3_compat").classList.remove("d-none");
      if (type === "s3") $("st_fields_s3_extra").classList.remove("d-none");
    }
  };

  window.saveStorage = async () => {
    const name = $("st_name").value.trim();
    const type = $("st_type").value;
    if (!name || !type) { toast("名称和类型为必填项", "warning"); return; }

    // tier 由后端依据 TYPE_META 统一推导（minio=L1 / s3=L2 / local=L3），
    // 此处不再前端硬编码，避免前后端分层不一致。
    const data = { name, type };
    data.remark = $("st_remark").value.trim();
    data.enabled = $("st_enabled").checked ? 1 : 0;
    data.is_default = $("st_default").checked ? 1 : 0;

    if (type === "local") {
      data.endpoint = $("st_endpoint_local").value.trim() || "./backups";
    } else {
      data.endpoint = $("st_endpoint_s3").value.trim();
      data.region = $("st_region").value.trim();
      data.access_key = $("st_access_key").value.trim();
      data.bucket = $("st_bucket").value.trim();
      data.prefix = $("st_prefix").value.trim();
      data.secret_key = $("st_secret_key").value;  // 留空=不修改

      const extraOpts = {};
      if (type === "s3") {
        extraOpts.storage_class = $("st_storage_class").value;
        extraOpts.insecure = $("st_insecure").checked;
      }
      data.extra_options = extraOpts;
    }

    try {
      if (editingStorageId) {
        await api("PUT", "/api/storage/targets/" + editingStorageId, data);
        toast("已更新: " + name);
      } else {
        // 新建时 secret_key 必填（非本地类型）
        if (type !== "local" && !data.secret_key) {
          toast("新建 MinIO/S3 存储需填写 Secret Key", "warning");
          return;
        }
        const r = await api("POST", "/api/storage/targets", data);
        editingStorageId = r.id;
        $("st_id").value = r.id;
        $("stTestBtn").classList.remove("d-none");
        toast("已创建: " + name + " #" + r.id);
      }
      storageModal.hide();
      await loadTargets();
    } catch (e) { toast(e.message, "danger"); }
  };

  window.testStorageConn = async () => {
    const btn = $("stTestBtn");
    const orig = btn.innerHTML;
    const resEl = $("stTestResult");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>测试中...';
    resEl.innerHTML = '<span class="text-muted">正在测试连接...</span>';
    try {
      const body = {};
      const sk = $("st_secret_key").value;
      if (sk) body.secret_key = sk;
      const targetId = $("st_id").value;
      const url = targetId ? "/api/storage/targets/" + targetId + "/test" : null;
      if (!url) { toast("请先保存再测试", "warning"); return; }
      const r = await api("POST", url, body);
      if (r.ok) {
        resEl.innerHTML = '<span class="text-success"><i class="bi bi-check-circle-fill me-1"></i>连接成功' +
          (r.message ? '：' + esc(r.message) : '') + (r.ms != null ? ' · ' + r.ms + 'ms' : '') + '</span>';
        toast("连接成功", "success");
      } else {
        resEl.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>连接失败：' + esc(r.message) + '</span>';
        toast("连接失败: " + r.message, "danger");
      }
    } catch (e) {
      resEl.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>测试异常：' + esc(e.message) + '</span>';
      toast(e.message, "danger");
    }
    finally { btn.disabled = false; btn.innerHTML = orig; }
  };

  window.testStorageById = async (id) => {
    try {
      const r = await api("POST", "/api/storage/targets/" + id + "/test");
      toast(r.ok ? "连接成功: " + r.message : "连接失败: " + r.message, r.ok ? "success" : "danger");
      await loadTargets();
    } catch (e) { toast(e.message, "danger"); }
  };

  window.setDefaultStorage = async (id) => {
    try { await api("POST", "/api/storage/targets/" + id + "/default"); toast("已设为默认"); await loadTargets(); }
    catch (e) { toast(e.message, "danger"); }
  };

  window.deleteStorage = async (id, name) => {
    const ok = await confirmDialog({ title: "删除存储目标", message: "确定删除存储目标「" + name + "」？此操作不可逆。", confirmText: "确认删除", danger: true });
    if (!ok) return;
    try { await api("DELETE", "/api/storage/targets/" + id); toast("已删除: " + name); await loadTargets(); }
    catch (e) { toast(e.message, "danger"); }
  };

  window.showInstallHint = () => {
    alert("安装 MinIO SDK 命令：\n\npip install minio\n\n安装后重启平台即可启用 MinIO 和 S3 存储。");
  };

  // ========== 复制策略配置 ==========
  let replicateConfigModal = null;

  window.openReplicateConfig = async () => {
    if (!replicateConfigModal) {
      const el = document.getElementById("replicateConfigModal");
      if (el) replicateConfigModal = new bootstrap.Modal(el);
    }
    // 加载当前配置
    try {
      const cfg = await api("GET", "/api/storage/replication-config");
      $("rep_l1_minio").checked = !!cfg.push_l1_minio;
      $("rep_l2_s3").checked = !!cfg.push_l2_s3;
      $("rep_l3_local").checked = !!cfg.push_l3_local;
      $("rep_timing").value = cfg.timing || "immediate";
      $("rep_max_retries").value = cfg.max_retries || 3;
      $("rep_retry_interval").value = cfg.retry_interval || 30;
    } catch (e) {
      // 使用默认值（默认三级全开）
      $("rep_l1_minio").checked = true;
      $("rep_l2_s3").checked = true;
      $("rep_l3_local").checked = true;
      $("rep_timing").value = "immediate";
      $("rep_max_retries").value = 3;
      $("rep_retry_interval").value = 30;
    }
    if (replicateConfigModal) replicateConfigModal.show();
  };

  window.saveReplicateConfig = async () => {
    const cfg = {
      push_l1_minio: $("rep_l1_minio").checked ? 1 : 0,
      push_l2_s3: $("rep_l2_s3").checked ? 1 : 0,
      push_l3_local: $("rep_l3_local").checked ? 1 : 0,
      timing: $("rep_timing").value,
      max_retries: Number($("rep_max_retries").value) || 3,
      retry_interval: Number($("rep_retry_interval").value) || 30,
    };
    try {
      await api("POST", "/api/storage/replication-config", cfg);
      toast("复制策略已保存", "success");
      if (replicateConfigModal) replicateConfigModal.hide();
    } catch (e) { toast(e.message, "danger"); }
  };

  async function initStorage() {
    const smEl = document.getElementById("storageModal");
    if (smEl) storageModal = new bootstrap.Modal(smEl);
    const rcEl = document.getElementById("replicateConfigModal");
    if (rcEl) replicateConfigModal = new bootstrap.Modal(rcEl);
    try {
      storageUsage = await api("GET", "/api/storage/usage");
    } catch (e) {
      storageUsage = { error: String(e.message) };
    }
    await loadTargets();
    try { await loadLifecycle(); } catch (e) { /* 生命周期模块未就绪时忽略 */ }
  }

  // =======================  冷热分级生命周期  =======================
  async function loadLifecycle() {
    try {
      const d = await api("GET", "/api/lifecycle");
      const cfg = d.config || {};
      $("lcL1L2").value = cfg.l1_to_l2_days != null ? cfg.l1_to_l2_days : 7;
      $("lcL2L3").value = cfg.l2_to_l3_days != null ? cfg.l2_to_l3_days : 30;
      $("lcCap").value = cfg.capacity_threshold_pct != null ? cfg.capacity_threshold_pct : 85;
      $("lcRetain").value = cfg.retention_days != null ? cfg.retention_days : 90;
      $("lcEnabled").checked = !!cfg.enabled;
      $("lcExpiry").checked = !!cfg.enable_expiry;
      const st = d.status || {};
      const tiers = st.tiers || {};
      const setT = st.set_types || {};
      const fmt = (t) => (t ? (t.count + " 个 · " + (t.human || "0 B")) : "0 个");
      $("lcTier1").innerHTML = '<i class="bi bi-cloud-arrow-up me-1"></i><strong>L1 MinIO 热数据</strong>：' + fmt(tiers["1"]);
      $("lcTier2").innerHTML = '<i class="bi bi-cloud-check me-1"></i><strong>L2 S3 冷数据</strong>：' + fmt(tiers["2"]);
      $("lcTier3").innerHTML = '<i class="bi bi-hdd me-1"></i><strong>L3 源端导出</strong>：' + fmt(tiers["3"]);
      const types = Object.keys(setT).map(k => k + ":" + setT[k]).join(" / ") || "—";
      const msgEl = $("lcMsg");
      if (msgEl) msgEl.innerHTML = '<span class="text-muted">共 ' + (st.total_sets || 0) + ' 个备份集（' + types + '）</span>';
    } catch (e) {
      toast("加载生命周期状态失败: " + e.message, "danger");
    }
  }

  window.loadLifecycle = loadLifecycle;

  window.saveLifecycle = async () => {
    const cfg = {
      enabled: $("lcEnabled").checked ? 1 : 0,
      enable_expiry: $("lcExpiry").checked ? 1 : 0,
      l1_to_l2_days: Number($("lcL1L2").value) || 0,
      l2_to_l3_days: Number($("lcL2L3").value) || 0,
      capacity_threshold_pct: Number($("lcCap").value) || 0,
      retention_days: Number($("lcRetain").value) || 0,
    };
    try {
      await api("POST", "/api/lifecycle/config", cfg);
      toast("生命周期策略已保存", "success");
      await loadLifecycle();
    } catch (e) { toast(e.message, "danger"); }
  };

  window.runLifecycle = async () => {
    try {
      const d = await api("POST", "/api/lifecycle/run");
      const s = (d.summary || {});
      toast("生命周期执行完成：流转 " + (s.moved || 0) + "，清理 " + (s.expired || 0) + "，异常 " + (s.errors || 0), "success");
      await loadLifecycle();
    } catch (e) { toast(e.message, "danger"); }
  };

  // =======================  保护策略 (Protection)  =======================
  let policyModalInst = null, bindModalInst = null, currentPolicyId = null, bindPolicyId = null;

  // 各等级默认策略（与 core/policy.py ProtectionPolicyService.DEFAULTS 保持一致）
  const LEVEL_DEFAULTS = {
    core: {
      rpo: 0, rto: 15,
      backup_strategy: { type: "full", mode: "physical", frequency: "PT15M", incremental: true, parallel: 4, sync_mode: "strong" },
      link_strategy: { replication: "sync", cross_site: true, consistency: "strong" },
      retention: { days: 90, count: 200, lifecycle: { l1_to_l2_days: 1, l2_to_l3_days: 7 } },
    },
    important: {
      rpo: 15, rto: 60,
      backup_strategy: { type: "full", mode: "logical", frequency: "PT1H", incremental: true, parallel: 2, sync_mode: "async" },
      link_strategy: { replication: "async", cross_site: true, consistency: "eventual" },
      retention: { days: 30, count: 100, lifecycle: { l1_to_l2_days: 3, l2_to_l3_days: 15 } },
    },
    general: {
      rpo: 240, rto: 240,
      backup_strategy: { type: "full", mode: "logical", frequency: "P1D", incremental: false, parallel: 1, sync_mode: "async" },
      link_strategy: { replication: "async", cross_site: false, consistency: "eventual" },
      retention: { days: 14, count: 50, lifecycle: { l1_to_l2_days: 7, l2_to_l3_days: 30 } },
    },
  };

  function levelBadge(level) {
    const map = {
      core: ["var(--primary)", "核心"],
      important: ["var(--warning)", "重要"],
      general: ["var(--text-muted)", "一般"],
    };
    const m = map[level] || ["var(--text-muted)", level || "-"];
    return '<span class="badge" style="background:' + m[0] + ';color:var(--text-on-dark)">' + m[1] + '</span>';
  }

  function rpoText(min) {
    min = Number(min) || 0;
    return min === 0 ? "近实时 (0 分)" : (min + " 分钟");
  }

  function rtoText(min) {
    min = Number(min) || 0;
    if (min === 0) return "—";
    if (min >= 60) return (min / 60) + " 小时";
    return min + " 分钟";
  }

  function backupModeText(strategy) {
    if (!strategy || typeof strategy !== "object") return "—";
    const parts = [];
    if (strategy.mode) parts.push(strategy.mode === "physical" ? "物理" : "逻辑");
    if (strategy.type) parts.push(strategy.type);
    return parts.join(" · ") || "—";
  }

  function showPolicyError(msg) {
    const el = $("pFormError");
    el.textContent = msg;
    el.classList.remove("d-none");
  }

  async function loadPolicies() {
    const rows = await api("GET", "/api/policy");
    const tbody = $("policyTable");
    if (!rows || !rows.length) {
      $("emptyState").classList.remove("d-none");
      $("policyCard").classList.add("d-none");
      return;
    }
    $("emptyState").classList.add("d-none");
    $("policyCard").classList.remove("d-none");
    tbody.innerHTML = rows.map(function (p) {
      return '<tr>' +
        '<td>' + p.id + '</td>' +
        '<td>' + esc(p.name) + '</td>' +
        '<td>' + levelBadge(p.level) + '</td>' +
        '<td>' + rpoText(p.rpo_target_min) + '</td>' +
        '<td>' + rtoText(p.rto_target_min) + '</td>' +
        '<td>' + backupModeText(p.backup_strategy) + '</td>' +
        '<td>' + (p.enabled ? '<span class="badge badge-ok">已启用</span>' : '<span class="badge bg-secondary">已停用</span>') + '</td>' +
        '<td>' + (p.bound_task_count || 0) + ' 个</td>' +
        '<td class="text-end">' +
          '<button class="btn btn-sm btn-outline-primary me-1" onclick="viewPolicyRecords(' + p.id + ')">备份记录</button>' +
          '<button class="btn btn-sm btn-outline-secondary me-1" onclick="editPolicy(' + p.id + ')">编辑</button>' +
          '<button class="btn btn-sm btn-outline-info me-1" onclick="openBindModal(' + p.id + ')">绑定</button>' +
          '<button class="btn btn-sm btn-outline-danger" onclick="deletePolicy(' + p.id + ')">删除</button>' +
        '</td>' +
      '</tr>';
    }).join("");
  }

  async function initProtection() {
    const pmEl = document.getElementById("policyModal");
    if (pmEl) policyModalInst = new bootstrap.Modal(pmEl);
    const bmEl = document.getElementById("bindModal");
    if (bmEl) bindModalInst = new bootstrap.Modal(bmEl);

    window.openPolicyModal = function () {
      currentPolicyId = null;
      $("policyModalTitle").textContent = "新建保护策略";
      $("p_id").value = "";
      $("p_name").value = "";
      $("p_level").value = "general";
      $("p_rpo").value = 240;
      $("p_rto").value = 240;
      $("p_backup_strategy").value = "";
      $("p_link_strategy").value = "";
      $("p_retention").value = "";
      $("p_enabled").checked = true;
      $("pFormError").classList.add("d-none");
      policyModalInst.show();
    };

    window.onLevelChange = function () {
      const level = $("p_level").value;
      const d = LEVEL_DEFAULTS[level] || LEVEL_DEFAULTS.general;
      $("p_rpo").value = d.rpo;
      $("p_rto").value = d.rto;
    };

    window.fillPolicyDefaults = function () {
      const level = $("p_level").value;
      const d = LEVEL_DEFAULTS[level] || LEVEL_DEFAULTS.general;
      $("p_rpo").value = d.rpo;
      $("p_rto").value = d.rto;
      $("p_backup_strategy").value = JSON.stringify(d.backup_strategy, null, 2);
      $("p_link_strategy").value = JSON.stringify(d.link_strategy, null, 2);
      $("p_retention").value = JSON.stringify(d.retention, null, 2);
    };

    window.savePolicy = async function () {
      const name = $("p_name").value.trim();
      if (!name) { showPolicyError("策略名称为必填"); return; }
      const payload = {
        name: name,
        level: $("p_level").value,
        rpo_target_min: Number($("p_rpo").value) || 0,
        rto_target_min: Number($("p_rto").value) || 0,
        enabled: $("p_enabled").checked ? 1 : 0,
      };
      for (const f of ["backup_strategy", "link_strategy", "retention"]) {
        const raw = $("p_" + f).value.trim();
        if (raw) {
          try { payload[f] = JSON.parse(raw); }
          catch (e) { showPolicyError(f + " 不是合法 JSON: " + e.message); return; }
        }
      }
      try {
        if (currentPolicyId) {
          await api("PUT", "/api/policy/" + currentPolicyId, payload);
          toast("策略已更新", "success");
        } else {
          await api("POST", "/api/policy", payload);
          toast("策略已创建", "success");
        }
        policyModalInst.hide();
        await loadPolicies();
      } catch (e) { showPolicyError(e.message); }
    };

    window.editPolicy = async function (id) {
      const p = await api("GET", "/api/policy/" + id);
      currentPolicyId = id;
      $("policyModalTitle").textContent = "编辑保护策略";
      $("p_id").value = id;
      $("p_name").value = p.name || "";
      $("p_level").value = p.level || "general";
      $("p_rpo").value = p.rpo_target_min != null ? p.rpo_target_min : 0;
      $("p_rto").value = p.rto_target_min != null ? p.rto_target_min : 0;
      $("p_backup_strategy").value = p.backup_strategy ? JSON.stringify(p.backup_strategy, null, 2) : "";
      $("p_link_strategy").value = p.link_strategy ? JSON.stringify(p.link_strategy, null, 2) : "";
      $("p_retention").value = p.retention ? JSON.stringify(p.retention, null, 2) : "";
      $("p_enabled").checked = !!p.enabled;
      $("pFormError").classList.add("d-none");
      policyModalInst.show();
    };

    window.deletePolicy = async function (id) {
      const ok = await confirmDialog({
        title: "删除保护策略",
        message: "确定删除该策略？关联任务将自动解绑（保护列清空），此操作不可恢复。",
        confirmText: "删除", danger: true,
      });
      if (!ok) return;
      try {
        await api("DELETE", "/api/policy/" + id);
        toast("策略已删除", "success");
        await loadPolicies();
      } catch (e) { toast(e.message, "danger"); }
    };

    window.viewPolicyRecords = function (id) {
      location.href = "/records?policy_id=" + id;
    };

    window.openBindModal = async function (id) {
      bindPolicyId = id;
      const [policy, tasks] = await Promise.all([
        api("GET", "/api/policy/" + id),
        api("GET", "/api/tasks"),
      ]);
      const boundIds = new Set((policy.bound_tasks || []).map(function (t) { return t.id; }));
      $("bindHint").textContent = "勾选要绑定到「" + policy.name + "」的备份任务：";
      const tbody = $("bindTable");
      if (!tasks || !tasks.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="text-muted text-center">暂无备份任务</td></tr>';
      } else {
        tbody.innerHTML = tasks.map(function (t) {
          const checked = boundIds.has(t.id) ? "checked" : "";
          const cur = t.policy_id
            ? (t.policy_id === id ? '<span class="badge badge-ok">本策略</span>'
                                  : '<span class="badge bg-secondary">其他</span>')
            : '<span class="text-muted">未绑定</span>';
          return '<tr>' +
            '<td><input class="form-check-input bind-check" type="checkbox" value="' + t.id + '" ' + checked + '></td>' +
            '<td>' + esc(t.name) + '</td>' +
            '<td><span class="badge bg-info">' + esc(t.db_type) + '</span></td>' +
            '<td>' + cur + '</td>' +
          '</tr>';
        }).join("");
      }
      $("bindSelectAll").checked = false;
      bindModalInst.show();
    };

    window.toggleBindAll = function (checked) {
      document.querySelectorAll(".bind-check").forEach(function (cb) { cb.checked = checked; });
    };

    window.saveBind = async function () {
      const checked = [];
      document.querySelectorAll(".bind-check").forEach(function (cb) {
        if (cb.checked) checked.push(Number(cb.value));
      });
      try {
        const policy = await api("GET", "/api/policy/" + bindPolicyId);
        const prevBound = new Set((policy.bound_tasks || []).map(function (t) { return t.id; }));
        const checkedSet = new Set(checked);
        const toUnbind = [];
        prevBound.forEach(function (id) { if (!checkedSet.has(id)) toUnbind.push(id); });
        if (checked.length) {
          await api("POST", "/api/policy/" + bindPolicyId + "/bind", { task_ids: checked });
        }
        if (toUnbind.length) {
          await api("DELETE", "/api/policy/" + bindPolicyId + "/bind", { task_ids: toUnbind });
        }
        toast("绑定已保存", "success");
        bindModalInst.hide();
        await loadPolicies();
      } catch (e) { toast(e.message, "danger"); }
    };

    await loadPolicies();
  }

  // ======================= 迁移保护（Phase 2） =======================
  async function initMigration() {
    const migrationModalEl = document.getElementById("migrationModal");
    const migrationModalInst = migrationModalEl ? new bootstrap.Modal(migrationModalEl) : null;

    async function loadTasksInto(sel) {
      try {
        const tasks = await api("GET", "/api/tasks");
        sel.innerHTML = '<option value="">请选择备份任务</option>' + (tasks || []).map(function (t) {
          return '<option value="' + t.id + '">' + esc(t.name) + ' (' + esc(t.db_type) + ')</option>';
        }).join("");
      } catch (e) {
        sel.innerHTML = '<option value="">加载任务失败</option>';
      }
    }

    window.loadMigrations = async function () {
      const rows = await api("GET", "/api/migration");
      $("migrationTable").innerHTML = (rows || []).map(function (p) {
        const verified = p.verified
          ? '<span class="badge badge-ok">已校验</span>'
          : '<span class="badge bg-secondary">未校验</span>';
        const golden = p.golden_backup_record_id ? String(p.golden_backup_record_id) : '-';
        const taskLabel = p.task_id ? (esc(p.task_name || ('# ' + p.task_id))) : '-';
        return '<tr>' +
          '<td>' + p.id + '</td>' +
          '<td>' + taskLabel + '</td>' +
          '<td><span class="badge bg-info">' + esc(p.stage || '-') + '</span></td>' +
          '<td>' + golden + '</td>' +
          '<td>' + verified + '</td>' +
          '<td>' + (p.old_retention_days != null ? p.old_retention_days : '-') + '</td>' +
          '<td>' + statusBadge(p.status) + '</td>' +
          '<td>' + esc(p.note || '') + '</td>' +
          '<td class="text-end">' +
            '<button class="btn btn-sm btn-outline-secondary" onclick="verifyMigration(' + p.id + ')">' +
            '<i class="bi bi-check2-circle"></i> 验证</button>' +
          '</td>' +
        '</tr>';
      }).join("") || '<tr><td colspan="9" class="text-muted text-center">暂无迁移计划</td></tr>';
    };

    window.verifyMigration = async function (id) {
      try {
        const r = await api("POST", "/api/migration/" + id + "/verify");
        toast(r.message || ("校验" + (r.verified ? "通过" : "未通过")),
              r.verified ? "success" : "warning");
        await loadMigrations();
      } catch (e) { toast(e.message, "danger"); }
    };

    window.openMigrationModal = async function () {
      await loadTasksInto($("m_task_id"));
      $("m_stage").value = "pre";
      $("m_note").value = "";
      $("m_old_retention_days").value = 30;
      $("m_old_retention_wrap").style.display = "none";
      if (migrationModalInst) migrationModalInst.show();
    };

    window.saveMigration = async function () {
      const task_id = Number($("m_task_id").value);
      if (!task_id) { toast("请选择备份任务", "warning"); return; }
      const payload = {
        task_id: task_id,
        stage: $("m_stage").value,
        old_retention_days: Number($("m_old_retention_days").value) || null,
        note: $("m_note").value,
      };
      try {
        await api("POST", "/api/migration", payload);
        toast("迁移计划已创建并触发 " + payload.stage, "success");
        if (migrationModalInst) migrationModalInst.hide();
        await loadMigrations();
      } catch (e) { toast(e.message, "danger"); }
    };

    // 阶段联动：post 才显示旧库保留天数
    $("m_stage").addEventListener("change", function () {
      $("m_old_retention_wrap").style.display = this.value === "post" ? "" : "none";
    });

    $("newMigrationBtn").addEventListener("click", window.openMigrationModal);
    $("migrationSaveBtn").addEventListener("click", window.saveMigration);

    await loadMigrations();
  }

  // ======================= 一站式数据迁移计划（DTS 对标） =======================
  async function initDbMigrate() {
    const dmModalEl = document.getElementById("dbMigrateModal");
    const dmModalInst = dmModalEl ? new bootstrap.Modal(dmModalEl) : null;
    const dmDetailEl = document.getElementById("dbMigrateDetailModal");
    const dmDetailInst = dmDetailEl ? new bootstrap.Modal(dmDetailEl) : null;

    function statusBadgeDbMigrate(s) {
      const m = {
        created: ["bg-secondary", "已创建"],
        checking: ["badge-run", "预检查中"],
        migrating: ["badge-run", "迁移中"],
        verifying: ["badge-run", "校验中"],
        completed: ["badge-ok", "已完成"],
        failed: ["badge-fail", "失败"],
      };
      const pair = m[s] || ["bg-secondary", s || "-"];
      return '<span class="badge ' + pair[0] + '">' + pair[1] + '</span>';
    }

    function phaseChips(phases, types) {
      const order = ["precheck", "migrate", "verify"];
      const names = { precheck: "预检查", migrate: "结构+全量", verify: "数据校验" };
      return order.map(function (ph) {
        if ((types || []).indexOf(ph === "migrate" ? (ph === "migrate" && types.indexOf("structure") >= 0 ? "structure" : "full") : ph) < 0
            && !(ph === "migrate" && (types || []).indexOf("full") >= 0)
            && !(ph === "migrate" && (types || []).indexOf("structure") >= 0)
            && !(ph === "verify" && (types || []).indexOf("verify") >= 0)
            && !(ph === "precheck")) return "";
        const p = (phases || {})[ph];
        const ok = p && p.ok;
        const cls = !p ? "bg-secondary" : (ok ? "badge-ok" : "badge-fail");
        return '<span class="badge ' + cls + '" style="margin-right:4px">' + names[ph] + '</span>';
      }).join("");
    }

    window.loadDbMigrations = async function () {
      const rows = await api("GET", "/api/db-migrate");
      let running = false;
      $("dbMigrateTable").innerHTML = (rows || []).map(function (p) {
        if (["checking", "migrating", "verifying"].indexOf(p.status) >= 0) running = true;
        const link = '<span class="badge bg-info">' + esc(p.src_db_type) + '</span> ' +
          esc(p.src_host + ':' + (p.src_port || '-') + '/' + p.src_db_name) +
          ' <i class="bi bi-arrow-right"></i> ' +
          '<span class="badge bg-success">' + esc(p.tgt_db_type) + '</span> ' +
          esc(p.tgt_host + ':' + (p.tgt_port || '-') + '/' + p.tgt_db_name);
        const types = (p.migrate_types || []).map(function (t) {
          return { structure: "结构", full: "全量", verify: "校验" }[t] || t;
        }).join("+");
        const detail = '<button class="btn btn-sm btn-outline-primary" onclick="showDbMigrateDetail(' + p.id + ')">' +
          '<i class="bi bi-card-list"></i> 详情/报告</button> ';
        const rerun = '<button class="btn btn-sm btn-outline-secondary" onclick="rerunDbMigrate(' + p.id + ')">' +
          '<i class="bi bi-arrow-repeat"></i> 重新执行</button> ';
        const del = '<button class="btn btn-sm btn-outline-danger" onclick="deleteDbMigrate(' + p.id + ')">' +
          '<i class="bi bi-trash"></i> 删除</button>';
        return '<tr>' +
          '<td>' + p.id + '</td>' +
          '<td>' + esc(p.name) + '</td>' +
          '<td style="max-width:380px">' + link + '</td>' +
          '<td>' + esc(types) + '</td>' +
          '<td>' + statusBadgeDbMigrate(p.status) + '</td>' +
          '<td>' + esc(p.current_phase || '-') + '</td>' +
          '<td>' + phaseChips(p.phases_json, p.migrate_types) + '</td>' +
          '<td class="text-end">' + detail + rerun + del + '</td>' +
        '</tr>';
      }).join("") || '<tr><td colspan="8" class="text-muted text-center">暂无迁移计划</td></tr>';
      if (running) {
        clearTimeout(loadDbMigrations._timer);
        loadDbMigrations._timer = setTimeout(function () {
          if (document.getElementById("dbMigrateTable")) loadDbMigrations();
        }, 3000);
      }
    };

    window.showDbMigrateDetail = async function (id) {
      try {
        const p = await api("GET", "/api/db-migrate/" + id);
        const phases = p.phases_json || {};
        let html = '<div class="mb-2"><span class="badge bg-info">' + esc(p.src_db_type) + '</span> ' +
          esc(p.src_host + ':' + (p.src_port || '-') + '/' + p.src_db_name) +
          ' <i class="bi bi-arrow-right"></i> ' +
          '<span class="badge bg-success">' + esc(p.tgt_db_type) + '</span> ' +
          esc(p.tgt_host + ':' + (p.tgt_port || '-') + '/' + p.tgt_db_name) +
          '　' + statusBadgeDbMigrate(p.status) + '</div>';
        if (p.error_msg) {
          html += '<div class="alert alert-danger py-2 small">' + esc(p.error_msg) + '</div>';
        }
        const names = { precheck: "① 预检查", migrate: "② 结构迁移 + 全量迁移", verify: "③ 数据校验" };
        ["precheck", "migrate", "verify"].forEach(function (ph) {
          const d = phases[ph];
          if (!d) return;
          html += '<div class="border rounded p-2 mb-2 small">' +
            '<div class="fw-bold mb-1">' + names[ph] + ' ' +
            '<span class="badge ' + (d.ok ? 'badge-ok' : 'badge-fail') + '">' + (d.ok ? '通过' : '失败') + '</span></div>';
          if (ph === "precheck") {
            (d.checks || []).forEach(function (c) {
              html += '<div>' + (c.ok ? '✅' : '❌') + ' ' + esc(c.item) + '：' + esc(c.message || '') + '</div>';
            });
            html += '<div>源对象统计：表 ' + (d.source_tables || 0) + ' 张 / 约 ' + (d.source_rows || 0) + ' 行</div>';
          } else if (ph === "migrate") {
            html += '<div>结构迁移：' + esc(d.structure || '-') + '</div>' +
              '<div>读取 ' + (d.total_read || 0) + ' 行 / 写入 ' + (d.total_write || 0) + ' 行，耗时 ' + (d.duration_sec || 0) + 's</div>' +
              '<div class="text-muted">' + esc(d.message || '') + '</div>';
          } else if (ph === "verify") {
            html += '<div>' + esc(d.message || '') + '（一致 ' + (d.tables_matched || 0) + '/' + (d.tables_total || 0) + ' 张）</div>';
            (d.tables || []).forEach(function (t) {
              html += '<div>' + (t.match ? '✅' : '❌') + ' ' + esc(t.table) + '：源 ' + t.source_rows + ' 行 / 目标 ' + t.target_rows + ' 行</div>';
            });
          } else if (ph === "report") {
            html += '<div>迁移耗时 ' + (d.duration_sec || 0) + 's，完成于 ' + esc(d.generated_at || '') + '</div>';
          }
          html += '</div>';
        });
        if (!Object.keys(phases).length) html += '<div class="text-muted">尚未执行</div>';
        $("dbMigrateDetailBody").innerHTML = html;
        if (dmDetailInst) dmDetailInst.show();
      } catch (e) { toast(e.message, "danger"); }
    };

    window.rerunDbMigrate = async function (id) {
      try {
        await api("POST", "/api/db-migrate/" + id + "/run");
        toast("迁移计划已重新执行", "success");
        await loadDbMigrations();
      } catch (e) { toast(e.message, "danger"); }
    };

    window.deleteDbMigrate = async function (id) {
      const ok = await confirmDialog({ title: "删除迁移计划", message: "确认删除该迁移计划？", confirmText: "删除", danger: true });
      if (!ok) return;
      try {
        await api("DELETE", "/api/db-migrate/" + id);
        toast("已删除", "success");
        await loadDbMigrations();
      } catch (e) { toast(e.message, "danger"); }
    };

    window.openDbMigrateModal = function () {
      $("dm_name").value = "";
      $("dm_src_host").value = ""; $("dm_src_port").value = 3306;
      $("dm_src_username").value = "root"; $("dm_src_password").value = "";
      $("dm_src_db_name").value = "";
      $("dm_tgt_host").value = ""; $("dm_tgt_port").value = 3306;
      $("dm_tgt_username").value = "root"; $("dm_tgt_password").value = "";
      $("dm_tgt_db_name").value = "";
      $("dm_note").value = "";
      ["dm_t_structure", "dm_t_full", "dm_t_verify"].forEach(function (id) { $("#" + id).checked = true; });
      if (dmModalInst) dmModalInst.show();
    };

    window.saveDbMigrate = async function () {
      const payload = {
        name: $("dm_name").value.trim(),
        src_db_type: $("dm_src_db_type").value,
        src_host: $("dm_src_host").value.trim(),
        src_port: Number($("dm_src_port").value) || 0,
        src_username: $("dm_src_username").value.trim(),
        src_password: $("dm_src_password").value,
        src_db_name: $("dm_src_db_name").value.trim(),
        tgt_db_type: $("dm_tgt_db_type").value,
        tgt_host: $("dm_tgt_host").value.trim(),
        tgt_port: Number($("dm_tgt_port").value) || 0,
        tgt_username: $("dm_tgt_username").value.trim(),
        tgt_password: $("dm_tgt_password").value,
        tgt_db_name: $("dm_tgt_db_name").value.trim(),
        migrate_types: [],
        note: $("dm_note").value.trim(),
      };
      if ($("dm_t_structure").checked) payload.migrate_types.push("structure");
      if ($("dm_t_full").checked) payload.migrate_types.push("full");
      if ($("dm_t_verify").checked) payload.migrate_types.push("verify");
      if (!payload.name) { toast("请填写计划名称", "warning"); return; }
      if (!payload.src_host || !payload.src_db_name) { toast("请填写源端主机与数据库", "warning"); return; }
      if (!payload.tgt_host || !payload.tgt_db_name) { toast("请填写目标端主机与数据库", "warning"); return; }
      try {
        await api("POST", "/api/db-migrate", payload);
        toast("迁移计划已创建并开始执行", "success");
        if (dmModalInst) dmModalInst.hide();
        await loadDbMigrations();
      } catch (e) { toast(e.message, "danger"); }
    };

    $("newDbMigrateBtn").addEventListener("click", window.openDbMigrateModal);
    $("dbMigrateSaveBtn").addEventListener("click", window.saveDbMigrate);

    await loadDbMigrations();
  }

  // ======================= 克隆服务（Phase 2） =======================
  async function initClone() {
    const cloneModalEl = document.getElementById("cloneModal");
    const cloneModalInst = cloneModalEl ? new bootstrap.Modal(cloneModalEl) : null;
    // 校验结果内存缓存：{cloneId: {ok, message, ts}}，行刷新后仍可回显
    const VERIFY_RESULTS = {};

    function statusBadgeClone(s, title) {
      const m = {
        pending: ["bg-warning text-dark", "排队中"],
        approved: ["badge-run", "拉起中"],
        rejected: ["bg-secondary", "已取消"],
        creating: ["badge-run", "拉起中"],
        failed: ["badge-fail", "拉起失败"],
        ready: ["badge-ok", "就绪"],
        expired: ["bg-secondary", "已过期"],
        deleted: ["bg-secondary", "已销毁"],
      };
      const pair = m[s] || ["bg-secondary", s || "-"];
      return '<span class="badge ' + pair[0] + '"' +
        (title ? ' title="' + esc(title) + '"' : '') + '>' + pair[1] + '</span>';
    }

    // ---- 源备份记录：可搜索下拉 ----
    let CLONE_RECORDS = [];
    async function loadRecordsInto() {
      const input = $("c_source_search"), list = $("c_source_list"),
        hidden = $("c_source_record_id");
      try {
        const recs = await api("GET", "/api/records");
        CLONE_RECORDS = (recs || []).map(function (r) {
          return { id: r.id, label: fmtRecordLabel(r), raw: (r.task_name || "") + " " + (r.db_type || "") + " " + r.id };
        });
      } catch (e) { CLONE_RECORDS = []; }
      hidden.value = "";
      input.value = "";
      renderSourceList("");
    }
    function renderSourceList(kw) {
      const list = $("c_source_list");
      kw = (kw || "").trim().toLowerCase();
      const items = CLONE_RECORDS.filter(function (r) {
        return !kw || r.label.toLowerCase().indexOf(kw) >= 0 || r.raw.toLowerCase().indexOf(kw) >= 0;
      }).slice(0, 80);
      list.innerHTML = items.length ? items.map(function (r) {
        return '<button type="button" class="list-group-item list-group-item-action py-1" ' +
          'style="font-size:.86rem" data-id="' + r.id + '" data-label="' + esc(r.label) + '">' +
          esc(r.label) + '</button>';
      }).join("") : '<span class="list-group-item text-muted py-1" style="font-size:.86rem">无匹配记录</span>';
      list.style.display = "block";
    }
    function bindSourceSearch() {
      const input = $("c_source_search"), list = $("c_source_list"),
        hidden = $("c_source_record_id");
      input.addEventListener("input", function () {
        hidden.value = "";
        renderSourceList(input.value);
      });
      input.addEventListener("focus", function () { renderSourceList(input.value); });
      input.addEventListener("blur", function () {
        setTimeout(function () { list.style.display = "none"; }, 180);
      });
      list.addEventListener("mousedown", function (ev) {
        const btn = ev.target.closest("button[data-id]");
        if (!btn) return;
        hidden.value = btn.dataset.id;
        input.value = btn.dataset.label;
        list.style.display = "none";
      });
    }

    // ---- 目标主机候选：从任务实例 host 聚合 ----
    async function loadHostOptions() {
      const dl = $("c_target_host_options");
      if (!dl) return;
      let hosts = ["127.0.0.1"];
      try {
        const tasks = await api("GET", "/api/tasks");
        (tasks || []).forEach(function (t) {
          const h = (t.host || "").trim();
          if (h && hosts.indexOf(h) < 0) hosts.push(h);
        });
      } catch (e) { /* 忽略，保留默认本机 */ }
      dl.innerHTML = hosts.map(function (h) {
        return '<option value="' + esc(h) + '">' + (h === "127.0.0.1" ? "本机 (127.0.0.1)" : esc(h)) + '</option>';
      }).join("");
    }

    window.loadClones = async function () {
      const rows = await api("GET", "/api/clone");
      let provisioning = false;
      $("cloneTable").innerHTML = (rows || []).map(function (c) {
        // VDB 连接信息：就绪时展示可直接使用的连接串
        let vdb = '-';
        if (c.status === 'ready' && c.vdb_dbname) {
          vdb = '<code title="' + esc((c.vdb_username || '') + '@' + (c.vdb_host || '127.0.0.1') + ':' + (c.vdb_port || '-') + '/' + c.vdb_dbname) + '">' +
            esc((c.vdb_host || '127.0.0.1') + ':' + (c.vdb_port || '-') + '/' + c.vdb_dbname) + '</code>';
        } else if (c.vdb_instance_id) {
          vdb = '#' + c.vdb_instance_id;
        }
        const actions = [];
        if (c.status === 'failed') {
          actions.push('<button class="btn btn-sm btn-outline-primary" onclick="retryClone(' + c.id + ')">' +
            '<i class="bi bi-arrow-clockwise"></i> 重试拉起</button>');
        }
        if (c.status === 'ready') {
          actions.push('<button class="btn btn-sm btn-outline-success" onclick="verifyClone(' + c.id + ')">' +
            '<i class="bi bi-shield-check"></i> 校验</button>');
        }
        if (['ready', 'creating', 'failed', 'expired'].indexOf(c.status) >= 0) {
          actions.push('<button class="btn btn-sm btn-outline-danger" onclick="destroyClone(' + c.id + ')">' +
            '<i class="bi bi-trash"></i> 销毁</button>');
        }
        // 失败原因/备注透出到状态徽章 tooltip
        const note = (c.note || '').trim();
        const badge = statusBadgeClone(c.status,
          c.status === 'failed' && note ? note.split('\n').pop() : (note || ''));
        // 校验结果小字（内存缓存回显）
        const vr = VERIFY_RESULTS[c.id];
        const verifyHtml = vr
          ? '<div style="font-size:.72rem" class="' + (vr.ok ? 'text-success' : 'text-danger') + '">' +
            (vr.ok ? '✓ ' : '✗ ') + esc(vr.message) + '</div>'
          : '';
        if (c.status === 'creating') provisioning = true;
        return '<tr>' +
          '<td>' + c.id + '</td>' +
          '<td>' + (c.task_name ? esc(c.task_name) + ' <span class="text-muted">(记录 ' + c.source_record_id + ')</span>' : (c.source_record_id != null ? c.source_record_id : '-')) + '</td>' +
          '<td>' + (c.source_db_type ? '<span class="badge bg-info">' + esc(c.source_db_type) + '</span>' : '-') + '</td>' +
          '<td>' + esc(c.target_env || '') + '</td>' +
          '<td>' + esc(c.target_host || '127.0.0.1') + '</td>' +
          '<td>' + badge + verifyHtml + '</td>' +
          '<td>' + esc(c.requested_by || '-') + '</td>' +
          '<td>' + esc(c.expires_at || '-') + '</td>' +
          '<td>' + vdb + '</td>' +
          '<td class="text-end">' + (actions.join(' ') || '-') + '</td>' +
        '</tr>';
      }).join("") || '<tr><td colspan="10" class="text-muted text-center">暂无克隆请求</td></tr>';
      // 有克隆正在拉起时每 3 秒自动刷新，直到终态
      if (provisioning) {
        clearTimeout(loadClones._timer);
        loadClones._timer = setTimeout(function () {
          if (document.getElementById("cloneTable")) loadClones();
        }, 3000);
      }
    };

    window.verifyClone = async function (id) {
      try {
        const res = await api("POST", "/api/clone/" + id + "/verify");
        VERIFY_RESULTS[id] = { ok: !!res.ok, message: res.message || (res.ok ? "连接正常" : "校验失败"), ts: Date.now() };
        toast((res.ok ? "校验通过：" : "校验失败：") + (res.message || ""), res.ok ? "success" : "danger");
      } catch (e) {
        VERIFY_RESULTS[id] = { ok: false, message: e.message, ts: Date.now() };
        toast(e.message, "danger");
      }
      await loadClones();
    };
    // failed 重试拉起（复用后端幂等的 approve 直通通道）
    window.retryClone = async function (id) {
      try {
        await api("POST", "/api/clone/" + id + "/approve");
        toast("重新拉起中（列表自动刷新）", "success");
        await loadClones();
      } catch (e) { toast(e.message, "danger"); }
    };
    window.destroyClone = async function (id) {
      const ok = await confirmDialog({ title: "销毁克隆", message: "确认销毁该克隆实例？VDB 将被释放。", confirmText: "销毁", danger: true });
      if (!ok) return;
      try {
        await api("POST", "/api/clone/" + id + "/destroy");
        toast("已销毁", "success");
        await loadClones();
      } catch (e) { toast(e.message, "danger"); }
    };

    window.openCloneModal = async function () {
      await Promise.all([loadRecordsInto(), loadHostOptions()]);
      $("c_target_env").value = "";
      $("c_target_host").value = "127.0.0.1";
      $("c_target_password").value = "";
      $("c_requested_by").value = "";
      $("c_note").value = "";
      if (cloneModalInst) cloneModalInst.show();
    };
    window.saveClone = async function () {
      const source_record_id = Number($("c_source_record_id").value);
      if (!source_record_id) { toast("请选择源备份记录", "warning"); return; }
      const payload = {
        source_record_id: source_record_id,
        target_env: $("c_target_env").value.trim(),
        target_host: $("c_target_host").value.trim() || "127.0.0.1",
        target_password: $("c_target_password").value,
        requested_by: $("c_requested_by").value.trim(),
        note: $("c_note").value,
      };
      if (!payload.target_env) { toast("请填写目标环境", "warning"); return; }
      try {
        await api("POST", "/api/clone", payload);
        toast("克隆已提交，正在后台拉起（列表自动刷新）", "success");
        if (cloneModalInst) cloneModalInst.hide();
        await loadClones();
      } catch (e) { toast(e.message, "danger"); }
    };

    bindSourceSearch();
    $("newCloneBtn").addEventListener("click", window.openCloneModal);
    $("cloneSaveBtn").addEventListener("click", window.saveClone);

    await loadClones();
  }

  // ======================= 容灾链路 HA（Phase 3 / T10） =======================
  let linkModalInst = null;
  // 数据源缓存：{kind, id, name, status, primary_site, dr_site, db_type, rt_mode, rpo_sec}
  let LINK_SOURCES = [];
  let LINK_SELECTED_SRC = null;

  /**
   * 拉取可用数据源。
   * 优先走后端 T09 的 GET /api/disaster-links/sources；该端点未就绪（404/500）时
   * 退化为前端聚合 /api/sync/tasks + /api/rt/tasks，保证 UI 在后端上线前可联调。
   */
  async function loadLinkSources() {
    try {
      const res = await api("GET", "/api/disaster-links/sources");
      const items = (res && res.items) || [];
      LINK_SOURCES = items;
      return items;
    } catch (e) { /* 后端未就绪，走前端降级聚合 */ }

    const items = [];
    try {
      const syncs = await api("GET", "/api/sync/tasks");
      (Array.isArray(syncs) ? syncs : []).forEach(function (s) {
        if (s.enabled !== undefined && !Number(s.enabled)) return;
        items.push({
          kind: "sync_task",
          id: Number(s.id),
          name: s.name || ("同步任务 #" + s.id),
          status: s.last_status || s.status || "never",
          primary_site: (s.src_host || "") + (s.src_port ? ":" + s.src_port : ""),
          dr_site: (s.tgt_host || "") + (s.tgt_port ? ":" + s.tgt_port : ""),
          db_type: s.src_db_type || s.db_type || "",
        });
      });
    } catch (e) { /* 同步任务不可用则跳过该类源 */ }
    try {
      const rt = await api("GET", "/api/rt/tasks");
      ((rt && rt.items) || []).forEach(function (t) {
        const h = t.health || {};
        items.push({
          kind: "rt_task",
          id: Number(t.id),
          name: t.name || ("实时任务 #" + t.id),
          status: h.health || "unknown",
          primary_site: "",
          dr_site: "",
          db_type: t.db_type || "",
          rt_mode: t.rt_mode || "",
          rpo_sec: h.rpo_actual_sec,
        });
      });
    } catch (e) { /* 实时任务不可用则跳过该类源 */ }
    LINK_SOURCES = items;
    return items;
  }

  /** 源任务状态徽章：failed 标红但仍可选（设计裁决⑥）。 */
  function linkSrcStatusBadge(status) {
    const s = String(status || "").toLowerCase();
    if (s === "failed" || s === "red") return '<span class="badge badge-fail">' + esc(status) + '</span>';
    if (s === "running" || s === "success" || s === "green") return '<span class="badge badge-ok">' + esc(status) + '</span>';
    if (s === "yellow" || s === "warn") return '<span class="badge badge-sim">' + esc(status) + '</span>';
    return '<span class="badge bg-secondary">' + esc(status || "-") + '</span>';
  }

  /** 根据源生成默认路由策略（单条，端点取源目标地址）。 */
  function linkDefaultRoutePolicy(src) {
    const endpoint = src.dr_site || src.primary_site || "";
    if (!endpoint) return [];
    return [{ provider: "默认专线", endpoint: endpoint, priority: 1, enabled: true }];
  }

  /** 渲染第一步的数据源单选列表；按 sync_task / rt_task 分组。 */
  function renderLinkSourceList() {
    const wrap = $("linkSourceList");
    const noSrc = $("linkNoSource");
    const nextBtn = $("linkNextBtn");
    const saveBtn = $("saveLinkBtn");
    const manual = $("l_manual_mode").checked;

    if (!LINK_SOURCES.length) {
      wrap.innerHTML = "";
      noSrc.classList.remove("d-none");
      // 无源且未勾选手工模式 → 禁用下一步与保存（PRD：不允许无源建链路）
      nextBtn.disabled = !manual;
      saveBtn.disabled = !manual;
      return;
    }
    noSrc.classList.add("d-none");
    nextBtn.disabled = false;
    saveBtn.disabled = false;

    const groups = [
      { kind: "sync_task", label: "数据同步任务", icon: "bi-arrow-left-right" },
      { kind: "rt_task", label: "实时保护任务", icon: "bi-broadcast" },
    ];
    let html = "";
    groups.forEach(function (g) {
      const list = LINK_SOURCES.filter(function (s) { return s.kind === g.kind; });
      if (!list.length) return;
      html += '<div class="src-group-title"><i class="bi ' + g.icon + ' me-1"></i>' +
        esc(g.label) + '（' + list.length + '）</div>';
      html += list.map(function (s) {
        const key = s.kind + ":" + s.id;
        const sel = (LINK_SELECTED_SRC && (LINK_SELECTED_SRC.kind + ":" + LINK_SELECTED_SRC.id) === key)
          ? " selected" : "";
        const rpo = (s.rpo_sec === null || s.rpo_sec === undefined) ? "" : (" · RPO " + rtDur(s.rpo_sec));
        const sites = (s.primary_site || s.dr_site)
          ? '<code>' + esc(s.primary_site || "—") + '</code> <i class="bi bi-arrow-right"></i> <code>' +
            esc(s.dr_site || "（需手工填写）") + '</code>'
          : '<span class="text-muted">该源未提供备站点地址，需在第二步手工填写</span>';
        return '<label class="src-option' + sel + '" data-src-key="' + esc(key) + '" ' +
          'onclick="pickLinkSource(\'' + esc(s.kind) + '\', ' + Number(s.id) + ')">' +
          '<div class="d-flex align-items-center gap-2">' +
            '<input class="form-check-input mt-0" type="radio" name="l_src_radio" ' +
              (sel ? "checked" : "") + ' value="' + esc(key) + '">' +
            '<span class="src-name">' + esc(s.name) + '</span>' +
            linkSrcStatusBadge(s.status) +
            '<span class="ms-auto text-muted" style="font-size:var(--font-size-xs)">' +
              esc(s.db_type || "-") + (s.rt_mode ? " · " + esc(s.rt_mode) : "") + esc(rpo) +
            '</span>' +
          '</div>' +
          '<div class="src-meta">' + sites + '</div>' +
        '</label>';
      }).join("");
    });
    wrap.innerHTML = html;
  }

  /** 选中数据源 → 记录引用 + 快照回填主/备站点与路由策略（设计裁决②：引用+快照双轨）。 */
  window.pickLinkSource = function (kind, id) {
    const src = LINK_SOURCES.filter(function (s) {
      return s.kind === kind && Number(s.id) === Number(id);
    })[0];
    if (!src) return;
    LINK_SELECTED_SRC = src;
    $("l_source_kind").value = kind;
    $("l_source_id").value = String(id);
    $("l_manual_mode").checked = false;

    // 快照回填（用户仍可在第二步手工改）
    if (!$("l_name").value.trim()) $("l_name").value = src.name + " 容灾链路";
    $("l_primary_site").value = src.primary_site || "";
    $("l_dr_site").value = src.dr_site || "";
    const rp = linkDefaultRoutePolicy(src);
    $("l_route_policy").value = rp.length ? JSON.stringify(rp, null, 2) : "";
    renderLinkSourceList();
  };

  /** 第二步顶部的源摘要条。 */
  function renderLinkSrcSummary() {
    const box = $("linkSrcSummary");
    const kind = $("l_source_kind").value;
    if (!LINK_SELECTED_SRC || kind === "manual") {
      box.innerHTML = '<div class="alert alert-secondary py-2 mb-0" style="font-size:var(--font-size-xs)">' +
        '<i class="bi bi-pencil-square"></i> 手工模式：本链路不关联任何数据源，所有字段需手工填写。</div>';
      return;
    }
    const s = LINK_SELECTED_SRC;
    const kindLabel = s.kind === "sync_task" ? "数据同步任务" : "实时保护任务";
    box.innerHTML = '<div class="alert alert-info py-2 mb-0" style="font-size:var(--font-size-xs)">' +
      '<i class="bi bi-link-45deg"></i> 数据源：<strong>' + esc(s.name) + '</strong>' +
      '（' + esc(kindLabel) + ' #' + s.id + '）' + linkSrcStatusBadge(s.status) +
      '<button type="button" class="btn btn-link btn-sm p-0 ms-2" onclick="pickLinkSource(\'' +
        esc(s.kind) + '\', ' + Number(s.id) + ')">↻ 重新回填站点与路由</button>' +
      '</div>';
  }

  /** 步骤切换。step=1 选源；step=2 参数确认。 */
  window.linkGotoStep = function (step) {
    const s1 = $("linkStep1"), s2 = $("linkStep2");
    const chip1 = $("linkStepChip1"), chip2 = $("linkStepChip2");
    const prevBtn = $("linkPrevBtn"), nextBtn = $("linkNextBtn"), saveBtn = $("saveLinkBtn");
    const errEl = $("linkFormError");
    errEl.classList.add("d-none");

    if (step === 2) {
      const manual = $("l_manual_mode").checked;
      if (!manual && !LINK_SELECTED_SRC) {
        errEl.textContent = "请先选择一个数据源，或勾选「手工模式」";
        errEl.classList.remove("d-none");
        return;
      }
      if (manual) { $("l_source_kind").value = "manual"; $("l_source_id").value = ""; LINK_SELECTED_SRC = null; }
      renderLinkSrcSummary();
      s1.classList.add("d-none"); s2.classList.remove("d-none");
      chip1.className = "link-step-chip done"; chip2.className = "link-step-chip active";
      prevBtn.classList.remove("d-none"); nextBtn.classList.add("d-none"); saveBtn.classList.remove("d-none");
    } else {
      s1.classList.remove("d-none"); s2.classList.add("d-none");
      chip1.className = "link-step-chip active"; chip2.className = "link-step-chip";
      prevBtn.classList.add("d-none"); nextBtn.classList.remove("d-none"); saveBtn.classList.add("d-none");
    }
  };

  function linkStatusBadge(status) {
    const m = {
      active: ["badge-ok", "active 主用"],
      standby: ["bg-secondary", "standby 待命"],
      filling: ["badge-run", "filling 补传中"],
      broken: ["badge-fail", "broken 中断"],
    };
    const pair = m[status] || ["bg-secondary", status || "-"];
    return '<span class="badge ' + pair[0] + '">' + pair[1] + '</span>';
  }

  /**
   * 链路卡片的数据源行。
   * source_kind/source_id 为后端 T09 新增字段；缺失（老库 / 后端未就绪）时整块不渲染。
   * 快照(primary_site/dr_site)与源当前地址不一致时给出「源已变更 ↻重新回填」提示。
   */
  function linkSourceLine(l) {
    const kind = l.source_kind || "manual";
    if (kind === "manual" || !l.source_id) return "";
    const icon = kind === "sync_task" ? "bi-hdd-network" : "bi-broadcast";
    const kindLabel = kind === "sync_task" ? "同步任务" : "实时任务";
    // 优先用后端联查下发的 source_name / source_last_status，缺失时回落本地源缓存
    const cached = LINK_SOURCES.filter(function (s) {
      return s.kind === kind && Number(s.id) === Number(l.source_id);
    })[0] || null;
    const name = l.source_name || (cached && cached.name) || ("#" + l.source_id);
    const st = l.source_last_status || (cached && cached.status) || "";

    let html = '<div class="link-src-line"><i class="bi ' + icon + ' me-1"></i>' +
      '数据源：' + esc(name) + ' <span class="text-muted">(' + esc(kindLabel) + ')</span> ' +
      (st ? linkSrcStatusBadge(st) : "") + '</div>';

    // 差异检测：源当前地址 ≠ 建链路时的快照
    if (cached) {
      const drift = (cached.primary_site && cached.primary_site !== (l.primary_site || "")) ||
        (cached.dr_site && cached.dr_site !== (l.dr_site || ""));
      if (drift) {
        html += '<div class="link-src-stale"><i class="bi bi-exclamation-triangle"></i>源已变更' +
          '<button type="button" onclick="rebindLinkSource(' + Number(l.id) + ')">↻ 重新回填</button></div>';
      }
    }
    return html;
  }

  /** 「源已变更」→ 打开编辑弹窗并直接用源当前地址重新回填第二步。 */
  window.rebindLinkSource = async function (id) {
    await window.editLink(id);
    const kind = $("l_source_kind").value;
    const sid = Number($("l_source_id").value || 0);
    if (kind && kind !== "manual" && sid) {
      window.pickLinkSource(kind, sid);
      window.linkGotoStep(2);
      toast("已按数据源当前地址重新回填，确认后点击保存", "success");
    }
  };

  async function loadLinks() {
    // 数据源缓存供卡片展示源名 / 状态 / 差异检测使用；失败不阻塞链路列表渲染
    try { await loadLinkSources(); } catch (e) { LINK_SOURCES = []; }
    const links = await api("GET", "/api/disaster-links");
    const list = links.links || [];
    const container = $("linkList");
    const empty = $("linkEmpty");
    if (!list.length) {
      container.innerHTML = "";
      empty.classList.remove("d-none");
    } else {
      empty.classList.add("d-none");
      container.innerHTML = list.map(function (l) {
        const routes = Array.isArray(l.route_policy) ? l.route_policy : [];
        const routeRows = routes.map(function (r) {
          return '<tr>' +
            '<td>' + esc(r.provider || '-') + '</td>' +
            '<td><code class="small">' + esc(r.endpoint || '-') + '</code></td>' +
            '<td>' + (r.priority != null ? r.priority : '-') + '</td>' +
            '<td>' + (r.enabled ? '<span class="badge badge-ok">启用</span>' : '<span class="badge bg-secondary">停用</span>') + '</td>' +
            '</tr>';
        }).join("") || '<tr><td colspan="4" class="text-muted text-center">未配置专线</td></tr>';
        const consistency = l.consistency_result
          ? '<span class="badge ' +
            (l.consistency_result === 'pass' ? 'badge-ok' : l.consistency_result === 'warn' ? 'badge-sim' : 'badge-fail') +
            '">' + l.consistency_result + '</span>'
          : '<span class="text-muted">未校验</span>';
        return '<div class="col-md-6 col-xl-4">' +
          '<div class="page-card link-card h-100">' +
            '<div class="d-flex justify-content-between align-items-center">' +
              '<strong>' + esc(l.name) + '</strong>' + linkStatusBadge(l.status) +
            '</div>' +
            '<div class="link-sites mt-2">' +
              '<span><i class="bi bi-hdd-network me-1"></i>' + esc(l.primary_site || '主站点') + '</span>' +
              '<i class="bi bi-arrow-right site-arrow"></i>' +
              '<span><i class="bi bi-hdd-rack me-1"></i>' + esc(l.dr_site || '备站点') + '</span>' +
            '</div>' +
            linkSourceLine(l) +
            '<div class="link-meta">一致性: ' + consistency +
              ' · 最近校验: ' + esc(l.last_consistency_check || '-') + '</div>' +
            '<div class="table-responsive mt-2">' +
              '<table class="table route-table align-middle">' +
                '<thead><tr><th>专线</th><th>端点</th><th>优先级</th><th>状态</th></tr></thead>' +
                '<tbody>' + routeRows + '</tbody>' +
              '</table>' +
            '</div>' +
            '<div class="link-actions">' +
              '<button class="btn btn-sm btn-outline-primary" onclick="selectRoute(' + l.id + ')"><i class="bi bi-signpost-split me-1"></i>选路</button>' +
              '<button class="btn btn-sm btn-outline-info" onclick="fillGap(' + l.id + ')"><i class="bi bi-arrow-down-up me-1"></i>填补</button>' +
              '<button class="btn btn-sm btn-outline-secondary" onclick="checkConsistency(' + l.id + ')"><i class="bi bi-shield-check me-1"></i>校验</button>' +
              '<button class="btn btn-sm btn-outline-secondary" onclick="editLink(' + l.id + ')"><i class="bi bi-pencil me-1"></i>编辑</button>' +
              '<button class="btn btn-sm btn-outline-danger" onclick="deleteLink(' + l.id + ')"><i class="bi bi-trash me-1"></i>删除</button>' +
            '</div>' +
          '</div>' +
        '</div>';
      }).join("");
    }
    const active = list.filter(function (l) { return l.status === 'active'; }).length;
    const broken = list.filter(function (l) { return l.status === 'broken'; }).length;
    $("linkSummary").textContent = '共 ' + list.length + ' 条链路 · 主用 ' + active + ' · 中断 ' + broken;
  }

  async function initDrLink() {
    const el = document.getElementById("linkModal");
    if (el) linkModalInst = new bootstrap.Modal(el);

    // 手工模式勾选联动：勾上后无源也可进入第二步
    $("l_manual_mode").addEventListener("change", function () {
      if (this.checked) {
        LINK_SELECTED_SRC = null;
        $("l_source_kind").value = "manual";
        $("l_source_id").value = "";
      }
      renderLinkSourceList();
    });

    /** 清空表单到「新增」初态。 */
    function resetLinkForm() {
      $("l_id").value = "";
      $("l_name").value = "";
      $("l_status").value = "standby";
      $("l_enabled").value = "1";
      $("l_primary_site").value = "";
      $("l_dr_site").value = "";
      $("l_route_policy").value = "";
      $("l_note").value = "";
      $("l_source_kind").value = "manual";
      $("l_source_id").value = "";
      $("l_manual_mode").checked = false;
      LINK_SELECTED_SRC = null;
      $("linkFormError").classList.add("d-none");
    }

    window.openLinkModal = async function () {
      $("linkModalTitle").textContent = "新增容灾链路";
      resetLinkForm();
      $("linkSteps").classList.remove("d-none");
      window.linkGotoStep(1);
      $("linkSourceList").innerHTML =
        '<div class="text-muted small py-2"><span class="spinner-border spinner-border-sm me-1"></span>正在加载可用数据源…</div>';
      if (linkModalInst) linkModalInst.show();
      await loadLinkSources();
      renderLinkSourceList();
    };

    window.saveLink = async function () {
      const errEl = $("linkFormError");
      const name = $("l_name").value.trim();
      if (!name) { errEl.textContent = "链路名称为必填"; errEl.classList.remove("d-none"); return; }
      let routePolicy = null;
      const rpRaw = $("l_route_policy").value.trim();
      if (rpRaw) {
        try { routePolicy = JSON.parse(rpRaw); }
        catch (e) { errEl.textContent = "路由策略不是合法 JSON: " + e.message; errEl.classList.remove("d-none"); return; }
      }
      const sourceKind = $("l_source_kind").value || "manual";
      const sourceId = Number($("l_source_id").value || 0);
      if (sourceKind !== "manual" && !sourceId) {
        errEl.textContent = "已选择数据源类型但缺少源 ID，请返回第一步重新选择";
        errEl.classList.remove("d-none");
        return;
      }
      const payload = {
        name: name,
        status: $("l_status").value,
        enabled: $("l_enabled").value === "1" ? 1 : 0,
        primary_site: $("l_primary_site").value.trim(),
        dr_site: $("l_dr_site").value.trim(),
        note: $("l_note").value.trim(),
        // T09 后端新增字段；后端未就绪时会被忽略（白名单写入），不影响既有字段落库
        source_kind: sourceKind,
        source_id: sourceKind === "manual" ? null : sourceId,
      };
      if (routePolicy) payload.route_policy = routePolicy;
      const id = $("l_id").value;
      const btn = $("saveLinkBtn");
      const old = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>保存中…';
      try {
        if (id) { await api("PUT", "/api/disaster-links/" + id, payload); toast("链路已更新", "success"); }
        else { await api("POST", "/api/disaster-links", payload); toast("链路已创建", "success"); }
        if (linkModalInst) linkModalInst.hide();
        await loadLinks();
      } catch (e) { errEl.textContent = e.message; errEl.classList.remove("d-none"); }
      finally { btn.disabled = false; btn.innerHTML = old; }
    };

    window.editLink = async function (id) {
      const l = await api("GET", "/api/disaster-links/" + id);
      $("linkModalTitle").textContent = "编辑容灾链路";
      resetLinkForm();
      $("l_id").value = id;
      $("l_name").value = l.name || "";
      $("l_status").value = l.status || "standby";
      $("l_enabled").value = l.enabled ? "1" : "0";
      $("l_primary_site").value = l.primary_site || "";
      $("l_dr_site").value = l.dr_site || "";
      $("l_route_policy").value = l.route_policy ? JSON.stringify(l.route_policy, null, 2) : "";
      $("l_note").value = l.note || "";

      const kind = l.source_kind || "manual";
      $("l_source_kind").value = kind;
      $("l_source_id").value = l.source_id ? String(l.source_id) : "";
      if (linkModalInst) linkModalInst.show();

      if (kind === "manual" || !l.source_id) {
        // 存量 manual 链路：跳过第一步，直接进参数页（兼容 P2 手工模式）
        $("l_manual_mode").checked = true;
        $("linkSteps").classList.add("d-none");
        window.linkGotoStep(2);
        return;
      }
      $("linkSteps").classList.remove("d-none");
      await loadLinkSources();
      LINK_SELECTED_SRC = LINK_SOURCES.filter(function (s) {
        return s.kind === kind && Number(s.id) === Number(l.source_id);
      })[0] || {
        kind: kind, id: Number(l.source_id),
        name: l.source_name || ("#" + l.source_id),
        status: l.source_last_status || "",
        primary_site: l.primary_site || "", dr_site: l.dr_site || "", db_type: "",
      };
      renderLinkSourceList();
      window.linkGotoStep(2);
    };

    window.deleteLink = async function (id) {
      const ok = await confirmDialog({ title: "删除容灾链路", message: "确认删除该容灾链路？此操作不可恢复。", confirmText: "删除", danger: true });
      if (!ok) return;
      try { await api("DELETE", "/api/disaster-links/" + id); toast("链路已删除", "success"); await loadLinks(); }
      catch (e) { toast(e.message, "danger"); }
    };

    window.selectRoute = async function (id) {
      try {
        const r = await api("POST", "/api/disaster-links/" + id + "/select-route");
        if (r.ok) toast("已选路 → " + (r.selected ? r.selected.provider : '-') + " (" + (r.selected ? r.selected.latency_ms : 0) + "ms)", "success");
        else toast(r.error || "选路失败", "warning");
      } catch (e) { toast(e.message, "danger"); }
    };

    window.fillGap = async function (id) {
      try {
        const r = await api("POST", "/api/disaster-links/" + id + "/fill-gap");
        toast("日志填补: " + (r.message || r.result || "完成"), "success");
      } catch (e) { toast(e.message, "danger"); }
    };

    window.checkConsistency = async function (id) {
      try {
        const r = await api("POST", "/api/disaster-links/" + id + "/check-consistency");
        const res = r.result || "-";
        const type = res === "pass" ? "success" : res === "warn" ? "warning" : "danger";
        toast("一致性校验: " + res + (r.match_rate != null ? " (匹配 " + r.match_rate + "%)" : ""), type);
      } catch (e) { toast(e.message, "danger"); }
    };

    await loadLinks();
  }

  // ======================= AI 预测告警（Phase 3 / T05） =======================
  const METRIC_META = {
    backup_fail: "备份失败",
    verify_fail: "数据验证",
    storage_full: "存储将满",
    link_degraded: "链路劣化",
    drill_overdue: "演练超期",
    rpo_breach: "RPO 突破",
  };

  function levelBadgeEl(level) {
    const lvl = level || "low";
    return '<span class="badge level-badge level-' + lvl + '">' + lvl + '</span>';
  }

  // ---------- T05 任务级明细 ----------
  // 后端契约（core/ai_alert.py，T02/T03 产出）：
  //   details.task_details[] 固定 9 键：task_id / task_name / db_type / fail_7d /
  //   fail_30d / last_fail_at / last_error / task_risk_score / suggestion
  //   details.evidence = {task_ids:[int], record_ids:[int]}
  // 老记录 / 后端未就绪时 task_details 缺失 → 不渲染展开钮，整表零回归。

  /** 安全读取某条预测的任务级明细数组。缺失或类型不符时返回空数组。 */
  function predTaskDetails(p) {
    const d = p && p.details;
    if (!d || typeof d !== "object") return [];
    const td = d.task_details;
    return Array.isArray(td) ? td : [];
  }

  /** 数值缺省显示：null/undefined → "-"，0 正常显示。 */
  const numOrDash = (v) => (v === null || v === undefined || v === "" ? "-" : String(v));

  /** 任务风险分着色：复用 RISK_LEVELS 阈值（0-40 low / 40-65 medium / 65-85 high / 85+ critical）。 */
  function taskScoreBadge(score) {
    const s = Number(score || 0);
    const lvl = s >= 85 ? "critical" : s >= 65 ? "high" : s >= 40 ? "medium" : "low";
    return '<span class="badge level-badge level-' + lvl + '">' + Math.round(s) + '</span>';
  }

  /** 渲染任务级明细子表（backup_fail 与 verify_fail 同构，共用此渲染器）。 */
  function taskDetailRowsHtml(details) {
    if (!details.length) {
      return '<tr><td colspan="8" class="text-muted text-center">无任务级明细</td></tr>';
    }
    return details.map(function (t) {
      const err = t.last_error ? String(t.last_error) : "";
      return '<tr>' +
        '<td><strong>' + esc(t.task_name || ("任务 #" + numOrDash(t.task_id))) + '</strong></td>' +
        '<td><span class="badge bg-secondary">' + esc(t.db_type || "-") + '</span></td>' +
        '<td>' + numOrDash(t.fail_7d) + '</td>' +
        '<td>' + numOrDash(t.fail_30d) + '</td>' +
        '<td>' + (t.last_fail_at ? fmtTime(t.last_fail_at) : "-") + '</td>' +
        '<td>' + (err
          ? '<span class="err-text" title="' + esc(err) + '">' + esc(err) + '</span>'
          : '<span class="text-muted">-</span>') + '</td>' +
        '<td>' + taskScoreBadge(t.task_risk_score) + '</td>' +
        '<td>' + esc(t.suggestion || "查看任务日志定位失败原因") + '</td>' +
        '</tr>';
    }).join("");
  }

  /** 展开行（默认 d-none），承载子表 + evidence 机器可读 ID。 */
  function predDetailRowHtml(p, idx, colspan) {
    const details = predTaskDetails(p);
    const ev = (p.details && p.details.evidence) || {};
    const recIds = Array.isArray(ev.record_ids) ? ev.record_ids : [];
    const taskIds = Array.isArray(ev.task_ids) ? ev.task_ids : [];
    const evLine = (recIds.length || taskIds.length)
      ? '<div class="pred-evidence">证据：任务 ID [' + esc(taskIds.join(", ")) + ']' +
        ' · 备份记录 ID [' + esc(recIds.slice(0, 20).join(", ")) +
        (recIds.length > 20 ? " …共 " + recIds.length + " 条" : "") + ']</div>'
      : "";
    return '<tr class="pred-detail-row d-none" id="predDetail_' + idx + '">' +
      '<td colspan="' + colspan + '" class="pred-detail-cell">' +
        '<div class="pred-detail-title"><i class="bi bi-diagram-2 me-1"></i>任务级明细（Top ' +
          details.length + '）</div>' +
        '<div class="table-responsive">' +
          '<table class="table table-sm pred-subtable">' +
            '<thead><tr><th>任务</th><th>类型</th><th>近7天失败</th><th>近30天失败</th>' +
            '<th>最近失败</th><th>原因摘要</th><th>风险分</th><th>建议动作</th></tr></thead>' +
            '<tbody>' + taskDetailRowsHtml(details) + '</tbody>' +
          '</table>' +
        '</div>' + evLine +
      '</td>' +
    '</tr>';
  }

  /** 展开 / 折叠任务级明细。绑定在 alert.html 渲染出的 onclick 上。 */
  window.togglePredDetail = function (idx) {
    const row = document.getElementById("predDetail_" + idx);
    const btn = document.getElementById("predToggle_" + idx);
    if (!row) return;
    const opened = !row.classList.contains("d-none");
    row.classList.toggle("d-none");
    if (btn) {
      btn.classList.toggle("open", !opened);
      btn.setAttribute("aria-expanded", opened ? "false" : "true");
      btn.setAttribute("title", opened ? "展开任务明细" : "折叠任务明细");
    }
  };

  // ---------- T05 数据验证卡片区 ----------
  /** 校验层徽章：checked/failed 为 0 或缺失时降级为「未启用」。 */
  function verifyLayerChip(label, layer) {
    const l = layer || {};
    const checked = Number(l.checked || 0);
    const failed = Number(l.failed || 0);
    if (!checked) {
      return '<span class="verify-layer off"><i class="bi bi-dash-circle"></i>' +
        esc(label) + ' 未启用 / 无样本</span>';
    }
    const cls = failed > 0 ? "badge-fail" : "badge-ok";
    const icon = failed > 0 ? "bi-x-octagon" : "bi-check2-circle";
    return '<span class="verify-layer"><i class="bi ' + icon + '"></i>' +
      esc(label) + ' 抽检 <strong>' + checked + '</strong> 条 · ' +
      '<span class="badge ' + cls + '">' + (failed > 0 ? ("失败 " + failed) : "全部通过") + '</span></span>';
  }

  /** 渲染「备份数据验证」卡片区。pred 为最近一条 verify_fail 预测，null 时显示空态。 */
  function renderVerifyPanel(pred) {
    const emptyEl = $("verifyEmpty");
    const bodyEl = $("verifyBody");
    if (!pred) {
      emptyEl.classList.remove("d-none");
      bodyEl.classList.add("d-none");
      $("verifyUpdatedAt").textContent = "—";
      return;
    }
    emptyEl.classList.add("d-none");
    bodyEl.classList.remove("d-none");
    $("verifyUpdatedAt").textContent = "最近分析：" + fmtTime(pred.predicted_at);

    const d = (pred.details && typeof pred.details === "object") ? pred.details : {};
    const layers = d.layers || {};
    const l1 = layers.l1 || {};
    const l2 = layers.l2 || {};
    const checked = Number(l1.checked || 0) + Number(l2.checked || 0);
    const failed = Number(l1.failed || 0) + Number(l2.failed || 0);
    const passRate = checked > 0 ? ((checked - failed) / checked) * 100 : null;

    $("vfPassRate").textContent = passRate === null ? "—" : passRate.toFixed(1) + "%";
    $("vfPassSub").textContent = checked > 0
      ? ("抽检 " + checked + " 项 · 失败 " + failed + " 项")
      : "本周期无抽检样本";

    const ur = d.unverified_ratio;
    const urPct = (ur === null || ur === undefined) ? null : Number(ur) * 100;
    $("vfUnverified").textContent = urPct === null ? "—" : urPct.toFixed(1) + "%";
    $("vfUnverifiedSub").textContent = urPct === null
      ? "后端未下发 unverified_ratio"
      : (urPct >= 30 ? "超过 30% 阈值，建议回填校验和" : "低于 30% 告警阈值");

    const level = pred.risk_level || "low";
    $("vfLevel").innerHTML = levelBadgeEl(level);
    $("vfScore").textContent = "风险评分 " + Math.round(Number(pred.risk_score || 0)) + " / 100";

    $("vfLastVerified").textContent = d.last_verified_at ? fmtTime(d.last_verified_at) : "—";
    $("vfStale").textContent = d.last_verified_at ? "距今以 7 天为陈旧阈值" : "尚无成功验证记录";

    $("vfLayers").innerHTML =
      verifyLayerChip("L1 完整性（sha256）", l1) +
      verifyLayerChip("L2 可用性（可解压）", l2);

    const td = predTaskDetails(pred);
    $("vfTaskTable").innerHTML = taskDetailRowsHtml(td);
    $("vfTaskWrap").classList.toggle("d-none", td.length === 0);
  }

  async function loadAlerts() {
    const metric = $("predMetricFilter") ? $("predMetricFilter").value : "";
    const params = metric ? ("?metric=" + encodeURIComponent(metric)) : "";
    const [stats, preds] = await Promise.all([
      api("GET", "/api/alerts/stats?days=30"),
      api("GET", "/api/alerts/predictions" + params),
    ]);
    const latest = (stats && stats.latest) || {};
    Object.keys(METRIC_META).forEach(function (m) {
      const info = latest[m];
      const bl = $("bl_" + m), pb = $("pb_" + m), sc = $("sc_" + m);
      if (!info) {
        if (bl) bl.outerHTML = '<span class="badge level-badge level-low" id="bl_' + m + '">low</span>';
        if (pb) { pb.style.width = "0%"; pb.className = "progress-bar pb-low"; }
        if (sc) sc.textContent = "风险评分 —";
        return;
      }
      const level = info.risk_level || "low";
      const score = Math.round(Number(info.risk_score || 0));
      if (bl) bl.outerHTML = '<span class="badge level-badge level-' + level + '" id="bl_' + m + '">' + level + '</span>';
      if (pb) { pb.style.width = score + "%"; pb.className = "progress-bar pb-" + level; }
      if (sc) sc.textContent = "风险评分 " + score + " / 100";
    });
    const rows = (preds && preds.predictions) || [];
    const tbody = $("predTable");
    const COLSPAN = 9; // 展开钮 + 时间/指标/关联任务/预测内容/等级/评分/依据/模型来源
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="' + COLSPAN + '" class="text-muted text-center">暂无预测记录</td></tr>';
    } else {
      tbody.innerHTML = rows.map(function (p, idx) {
        const pc = p.predicted_content || "";
        const basis = Array.isArray(p.basis) ? p.basis : [];
        const basisPreview = basis.length > 0
          ? esc(basis[0]) + (basis.length > 1 ? ' …' : '')
          : '-';
        const modelSource = p.model_source || (p.details && p.details.model_source) || "规则引擎";
        const modelBadgeClass = modelSource === "规则引擎" ? "bg-secondary"
          : modelSource.includes("降级") ? "bg-warning text-dark"
          : modelSource.includes("本地") ? "bg-info"
          : "bg-primary";

        // 「关联任务」列：有 task_details 时给展开钮 + 任务数摘要，否则降级为「—」
        const tds = predTaskDetails(p);
        const hasDetail = tds.length > 0;
        const toggleCell = hasDetail
          ? '<td><button type="button" class="pred-toggle" id="predToggle_' + idx + '"' +
              ' aria-expanded="false" title="展开任务明细"' +
              ' onclick="togglePredDetail(' + idx + ')"><i class="bi bi-chevron-right"></i></button></td>'
          : '<td></td>';
        const topName = hasDetail ? (tds[0].task_name || ("任务 #" + numOrDash(tds[0].task_id))) : "";
        const taskCell = hasDetail
          ? '<td class="small"><a href="#" class="text-decoration-none" ' +
              'onclick="togglePredDetail(' + idx + '); return false;">' +
              esc(topName) + (tds.length > 1 ? ' 等 <strong>' + tds.length + '</strong> 个任务' : '') +
            '</a></td>'
          : '<td class="small text-muted">—</td>';

        const mainRow = '<tr>' +
          toggleCell +
          '<td>' + fmtTime(p.predicted_at) + '</td>' +
          '<td><span class="badge bg-info">' + esc(METRIC_META[p.metric] || p.metric) + '</span></td>' +
          taskCell +
          '<td class="small">' + esc(pc || '-') + '</td>' +
          '<td>' + levelBadgeEl(p.risk_level) + '</td>' +
          '<td>' + Math.round(Number(p.risk_score || 0)) + '</td>' +
          '<td class="small">' +
            (basis.length > 1
              ? '<span class="text-muted">' + basisPreview + '</span> ' +
                '<a href="#" class="text-primary" onclick="showBasis(' + idx + '); return false;">查看全部</a>'
              : '<span class="text-muted">' + basisPreview + '</span>') +
          '</td>' +
          '<td><span class="badge ' + modelBadgeClass + '">' + esc(modelSource) + '</span></td>' +
        '</tr>';
        return hasDetail ? (mainRow + predDetailRowHtml(p, idx, COLSPAN)) : mainRow;
      }).join("");
    }
    // 保存当前 predictions 以供模态框使用
    window._currentPreds = rows;

    // 数据验证卡片区：取最近一条 verify_fail 预测（列表已按时间倒序）。
    // 当筛选器过滤掉了 verify_fail 时单独补一次查询，保证卡片区不受筛选影响。
    let latestVerify = null;
    for (let i = 0; i < rows.length; i += 1) {
      if (rows[i].metric === "verify_fail") { latestVerify = rows[i]; break; }
    }
    if (!latestVerify && metric && metric !== "verify_fail") {
      try {
        const vf = await api("GET", "/api/alerts/predictions?metric=verify_fail&limit=1");
        latestVerify = ((vf && vf.predictions) || [])[0] || null;
      } catch (e) { latestVerify = null; }
    }
    renderVerifyPanel(latestVerify);
    const byMetric = (stats && stats.by_metric) || {};
    let highCrit = 0;
    Object.keys(byMetric).forEach(function (m) {
      highCrit += (byMetric[m].high || 0) + (byMetric[m].critical || 0);
    });
    $("alertSummary").textContent = "近 30 天：高危/严重预测 " + highCrit + " 条";
  }

  window.showBasis = function (idx) {
    const p = (window._currentPreds || [])[idx];
    if (!p) return;
    const pc = p.predicted_content || "";
    const basis = Array.isArray(p.basis) ? p.basis : [];
    $("basisModalTitle").textContent = METRIC_META[p.metric] || p.metric + " — 预测依据";
    $("basisModalBody").innerHTML =
      (pc ? '<p class="mb-2"><strong>预测内容：</strong>' + esc(pc) + '</p>' : '') +
      (basis.length > 0
        ? '<ul class="mb-0">' + basis.map(function (b) { return '<li>' + esc(b) + '</li>'; }).join("") + '</ul>'
        : '<p class="text-muted">无依据记录</p>');
    var modal = new bootstrap.Modal(document.getElementById("basisModal"));
    modal.show();
  };

  async function initAlert() {
    try {
      const cfg = await api("GET", "/api/alerts/config");
      $("cfg_enabled").checked = !!cfg.enabled;
      $("cfg_min_level").value = cfg.min_risk_level_to_record || "medium";
      $("cfg_notify_on").value = cfg.notify_on || "critical";
      $("cfg_interval").value = cfg.ai_alert_interval_hours || 6;
    } catch (e) { /* 配置加载失败忽略 */ }

    window.runAnalysis = async function () {
      try {
        const d = await api("POST", "/api/alerts/run");
        const s = (d.summary || {});
        toast("分析完成：记录 " + (s.recorded || 0) + " 条，触发 critical " + (s.critical_fired || 0) + " 条", "success");
        await loadAlerts();
      } catch (e) { toast(e.message, "danger"); }
    };

    window.saveAlertConfig = async function () {
      const payload = {
        enabled: $("cfg_enabled").checked ? true : false,
        min_risk_level_to_record: $("cfg_min_level").value,
        notify_on: $("cfg_notify_on").value,
        ai_alert_interval_hours: Number($("cfg_interval").value) || 6,
      };
      try {
        await api("POST", "/api/alerts/config", payload);
        toast("AI 告警配置已保存", "success");
      } catch (e) { $("alertCfgError").textContent = e.message; $("alertCfgError").classList.remove("d-none"); }
    };

    await loadAlerts();

    // 暴露给 alert.html inline onclick 使用
    window.loadAlerts = loadAlerts;
  }

  // =======================  AI 智能助手（agent） =======================
  // 后端契约（api/ai_agent.py）：
  //   POST   /api/agent/sessions              {title}            -> {ok, session}
  //   GET    /api/agent/sessions                                 -> {ok, sessions:[]}
  //   DELETE /api/agent/sessions/<id>                            -> {ok}
  //   GET    /api/agent/sessions/<id>/messages                   -> {ok, messages:[]}
  //   POST   /api/agent/chat    {session_id, message}            -> {ok, type, content, tool_trace, pending_confirm}
  //   POST   /api/agent/confirm {session_id, tool_call_id, approved} -> {ok, type, content}
  const AGENT = {
    sessions: [],
    currentId: "",
    sending: false,
    pendingConfirm: null,
    confirmModal: null,
    bound: false,
  };

  /**
   * Agent 专用 JSON fetch 封装（始终携带同源 Cookie）。
   * @param {string} url 请求地址
   * @param {Object} opts fetch 选项，body 可直接传对象
   * @returns {Promise<Object>} 解析后的 JSON（解析失败时返回 {ok:false,error}）
   */
  const agentApi = (url, opts = {}) => {
    const options = Object.assign({ method: "GET" }, opts);
    options.credentials = "same-origin";
    options.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (options.body !== undefined && options.body !== null && typeof options.body !== "string") {
      options.body = JSON.stringify(options.body);
    }
    return fetch(url, options).then((r) =>
      r.json().catch(() => ({ ok: false, error: "响应解析失败（HTTP " + r.status + "）" }))
    );
  };

  /** 会话/消息时间格式化：2026-01-01 12:00 */
  const agentTime = (iso) => (iso ? String(iso).replace("T", " ").slice(0, 16) : "-");

  /** JSON 参数美化（失败时降级为字符串） */
  const agentJson = (obj) => {
    try { return JSON.stringify(obj == null ? {} : obj, null, 2); } catch (e) { return String(obj); }
  };

  /** 滚动消息区到底部 */
  function agentScrollBottom() {
    const box = document.getElementById("agentMessages");
    if (box) box.scrollTop = box.scrollHeight;
  }

  /** 渲染左侧会话列表 */
  function renderAgentSessions() {
    const list = document.getElementById("agentSessionList");
    const foot = document.getElementById("agentSessionFoot");
    if (!list) return;
    if (!AGENT.sessions.length) {
      list.innerHTML = '<div class="agent-session-empty">暂无会话，点击上方「新建对话」开始</div>';
    } else {
      list.innerHTML = AGENT.sessions.map((s) => {
        const active = s.id === AGENT.currentId ? " active" : "";
        const count = Number(s.message_count || 0);
        return '<div class="agent-session-item' + active + '" data-sid="' + esc(s.id) + '">' +
          '<i class="bi bi-chat-left-text si-icon"></i>' +
          '<div class="si-body">' +
            '<div class="si-title" title="' + esc(s.title || "新对话") + '">' + esc(s.title || "新对话") + '</div>' +
            '<div class="si-meta">' + agentTime(s.created_at) + ' · ' + count + ' 条</div>' +
          '</div>' +
          '<button class="si-del" type="button" data-del="' + esc(s.id) + '" title="删除会话">' +
            '<i class="bi bi-trash"></i></button>' +
        '</div>';
      }).join("");
    }
    if (foot) foot.textContent = "共 " + AGENT.sessions.length + " 个会话";
  }

  /** 更新顶部标题栏 */
  function renderAgentHeader() {
    const t = document.getElementById("agentCurrentTitle");
    const m = document.getElementById("agentCurrentMeta");
    const cur = AGENT.sessions.find((s) => s.id === AGENT.currentId);
    if (t) t.textContent = cur ? (cur.title || "新对话") : "未选择会话";
    if (m) {
      m.textContent = cur
        ? (Number(cur.message_count || 0) + " 条消息 · 创建于 " + agentTime(cur.created_at))
        : "请新建或选择一个会话";
    }
  }

  /**
   * 构造一条消息气泡的 HTML。
   * @param {string} role user | assistant
   * @param {string} content 文本内容
   * @param {string} kind normal | error | confirm | system
   * @param {Array} toolTrace 工具调用轨迹
   */
  function agentBubbleHtml(role, content, kind = "normal", toolTrace = []) {
    const isUser = role === "user";
    const bbClass = isUser ? "bb-user"
      : kind === "error" ? "bb-error"
      : kind === "confirm" ? "bb-confirm"
      : kind === "system" ? "bb-system"
      : "bb-ai";
    const avatar = isUser
      ? '<div class="agent-avatar av-user"><i class="bi bi-person"></i></div>'
      : '<div class="agent-avatar av-ai"><i class="bi bi-robot"></i></div>';
    let traceHtml = "";
    const traces = Array.isArray(toolTrace) ? toolTrace : [];
    if (!isUser && traces.length) {
      traceHtml = '<div class="agent-tool-trace">' + traces.map((t) =>
        '<span class="agent-tool-chip" title="' + esc(agentJson(t.args)) + '">' +
        '<i class="bi bi-tools"></i>' + esc(t.name || "tool") + '</span>'
      ).join("") + '</div>';
    }
    const icon = kind === "error" ? '<i class="bi bi-exclamation-octagon me-1"></i>'
      : kind === "confirm" ? '<i class="bi bi-shield-exclamation me-1"></i>' : "";
    return '<div class="agent-row ' + (isUser ? "row-user" : "row-ai") + '">' +
      avatar +
      '<div class="agent-bubble ' + bbClass + '">' + icon + esc(content || "") + traceHtml + '</div>' +
    '</div>';
  }

  /** 追加一条消息到消息区并滚动到底 */
  function appendAgentMessage(role, content, kind = "normal", toolTrace = []) {
    const box = document.getElementById("agentMessages");
    if (!box) return;
    const empty = box.querySelector(".agent-empty");
    if (empty) box.innerHTML = "";
    box.insertAdjacentHTML("beforeend", agentBubbleHtml(role, content, kind, toolTrace));
    agentScrollBottom();
  }

  /** 显示 / 移除打字动画 */
  function agentTyping(show) {
    const box = document.getElementById("agentMessages");
    if (!box) return;
    const old = document.getElementById("agentTypingRow");
    if (old) old.remove();
    if (!show) return;
    const empty = box.querySelector(".agent-empty");
    if (empty) box.innerHTML = "";
    box.insertAdjacentHTML("beforeend",
      '<div class="agent-row row-ai" id="agentTypingRow">' +
        '<div class="agent-avatar av-ai"><i class="bi bi-robot"></i></div>' +
        '<div class="agent-bubble bb-ai"><span class="agent-typing">' +
          '<span></span><span></span><span></span></span></div>' +
      '</div>');
    agentScrollBottom();
  }

  /** 切换发送中状态（禁用输入 / 按钮） */
  function setAgentSending(sending) {
    AGENT.sending = !!sending;
    const input = document.getElementById("agentInput");
    const btn = document.getElementById("agentSendBtn");
    const hint = document.getElementById("agentStatusHint");
    if (input) input.disabled = !!sending;
    if (btn) {
      btn.disabled = !!sending;
      btn.innerHTML = sending
        ? '<span class="spinner-border spinner-border-sm"></span>'
        : '<i class="bi bi-send"></i>';
    }
    if (hint) hint.textContent = sending ? "AI 正在思考…" : "就绪";
    agentTyping(!!sending);
    if (!sending && input) input.focus();
  }

  /** 加载会话列表 */
  async function loadAgentSessions() {
    const d = await agentApi("/api/agent/sessions");
    AGENT.sessions = (d && d.ok && Array.isArray(d.sessions)) ? d.sessions : [];
    if (AGENT.currentId && !AGENT.sessions.some((s) => s.id === AGENT.currentId)) {
      AGENT.currentId = "";
    }
    renderAgentSessions();
    renderAgentHeader();
  }

  /** 加载并渲染指定会话的历史消息 */
  async function loadAgentMessages(sessionId) {
    const box = document.getElementById("agentMessages");
    if (!box) return;
    box.innerHTML = '<div class="agent-empty"><i class="bi bi-hourglass-split"></i>' +
      '<div class="ae-title">正在加载消息…</div></div>';
    const d = await agentApi("/api/agent/sessions/" + encodeURIComponent(sessionId) + "/messages");
    const msgs = (d && d.ok && Array.isArray(d.messages)) ? d.messages : [];
    const html = [];
    msgs.forEach((m) => {
      const role = m.role || "assistant";
      if (role === "tool") {
        html.push('<div class="agent-tool-row"><span class="agent-tool-chip">' +
          '<i class="bi bi-tools"></i>' + esc(m.tool_name || "tool") + ' 已执行</span></div>');
        return;
      }
      if (role !== "user" && role !== "assistant") return;
      const trace = Array.isArray(m.tool_calls)
        ? m.tool_calls.map((tc) => ({ name: tc.name, args: tc.args })) : [];
      if (!String(m.content || "").trim() && !trace.length) return;
      html.push(agentBubbleHtml(role, m.content || "", "normal", trace));
    });
    box.innerHTML = html.length ? html.join("")
      : '<div class="agent-empty"><i class="bi bi-chat-dots"></i>' +
        '<div class="ae-title">这是一个新对话</div><div>在下方输入你的问题开始交流。</div></div>';
    agentScrollBottom();
  }

  /** 选中某个会话 */
  async function selectAgentSession(sessionId) {
    if (!sessionId || AGENT.sending) return;
    AGENT.currentId = sessionId;
    AGENT.pendingConfirm = null;
    renderAgentSessions();
    renderAgentHeader();
    await loadAgentMessages(sessionId);
  }

  /** 新建会话 */
  async function createAgentSession() {
    if (AGENT.sending) return;
    const d = await agentApi("/api/agent/sessions", { method: "POST", body: { title: "新对话" } });
    if (!d || !d.ok || !d.session) {
      toast((d && d.error) || "创建会话失败", "danger");
      return;
    }
    await loadAgentSessions();
    await selectAgentSession(d.session.id);
    const input = document.getElementById("agentInput");
    if (input) input.focus();
  }

  /** 删除会话（带二次确认） */
  async function deleteAgentSession(sessionId) {
    const cur = AGENT.sessions.find((s) => s.id === sessionId);
    const name = cur ? (cur.title || "新对话") : "该会话";
    if (!window.confirm('确定删除会话「' + name + '」及其全部消息吗？此操作不可撤销。')) return;
    const d = await agentApi("/api/agent/sessions/" + encodeURIComponent(sessionId), { method: "DELETE" });
    if (!d || !d.ok) {
      toast((d && d.error) || "删除失败", "danger");
      return;
    }
    toast("会话已删除");
    const wasCurrent = AGENT.currentId === sessionId;
    if (wasCurrent) AGENT.currentId = "";
    await loadAgentSessions();
    if (wasCurrent) {
      const box = document.getElementById("agentMessages");
      if (box) {
        box.innerHTML = '<div class="agent-empty"><i class="bi bi-robot"></i>' +
          '<div class="ae-title">未选择会话</div>' +
          '<div>点击左上角「新建对话」开始新的交流。</div></div>';
      }
      renderAgentHeader();
    }
  }

  /** 打开危险操作确认弹窗 */
  function openAgentConfirm(pending) {
    AGENT.pendingConfirm = pending || null;
    if (!pending) return;
    const tool = document.getElementById("agentConfirmTool");
    const args = document.getElementById("agentConfirmArgs");
    const reason = document.getElementById("agentConfirmReason");
    if (tool) tool.textContent = pending.tool_name || "-";
    if (args) args.textContent = agentJson(pending.args);
    if (reason) reason.textContent = pending.reason || "该操作可能影响生产环境，请确认后继续。";
    const el = document.getElementById("agentConfirmModal");
    if (!el || typeof bootstrap === "undefined") return;
    if (!AGENT.confirmModal) AGENT.confirmModal = new bootstrap.Modal(el);
    AGENT.confirmModal.show();
  }

  /** 统一处理 chat / confirm 接口返回 */
  function handleAgentResponse(resp) {
    if (!resp) {
      appendAgentMessage("assistant", "服务无响应，请稍后重试。", "error");
      return;
    }
    const type = resp.type || (resp.ok === false ? "error" : "answer");
    const trace = Array.isArray(resp.tool_trace) ? resp.tool_trace : [];
    if (type === "error" || resp.ok === false) {
      appendAgentMessage("assistant", resp.content || resp.error || "请求失败", "error", trace);
      return;
    }
    if (type === "confirm_required") {
      appendAgentMessage("assistant",
        resp.content || "该操作需要你的确认。", "confirm", trace);
      openAgentConfirm(resp.pending_confirm || null);
      return;
    }
    if (type === "rejected") {
      appendAgentMessage("assistant", resp.content || "已取消该操作。", "system", trace);
      return;
    }
    appendAgentMessage("assistant", resp.content || "（空回复）", "normal", trace);
  }

  /** 发送消息 */
  async function sendAgentMessage() {
    if (AGENT.sending) return;
    const input = document.getElementById("agentInput");
    const text = input ? String(input.value || "").trim() : "";
    if (!text) return;
    if (!AGENT.currentId) {
      await createAgentSession();
      if (!AGENT.currentId) return;
    }
    if (input) {
      input.value = "";
      input.style.height = "auto";
    }
    appendAgentMessage("user", text, "normal");
    setAgentSending(true);
    try {
      const resp = await agentApi("/api/agent/chat", {
        method: "POST",
        body: { session_id: AGENT.currentId, message: text },
      });
      setAgentSending(false);
      handleAgentResponse(resp);
    } catch (e) {
      setAgentSending(false);
      appendAgentMessage("assistant", "网络错误：" + (e && e.message ? e.message : e), "error");
    }
    await loadAgentSessions();
  }

  /** 提交确认结果 */
  async function submitAgentConfirm(approved) {
    const pending = AGENT.pendingConfirm;
    AGENT.pendingConfirm = null;
    if (AGENT.confirmModal) AGENT.confirmModal.hide();
    if (!pending || !AGENT.currentId) return;
    setAgentSending(true);
    try {
      const resp = await agentApi("/api/agent/confirm", {
        method: "POST",
        body: {
          session_id: AGENT.currentId,
          tool_call_id: pending.tool_call_id || "",
          approved: !!approved,
        },
      });
      setAgentSending(false);
      handleAgentResponse(resp);
    } catch (e) {
      setAgentSending(false);
      appendAgentMessage("assistant", "网络错误：" + (e && e.message ? e.message : e), "error");
    }
    await loadAgentSessions();
  }

  /** 输入框高度自适应 */
  function autoResizeAgentInput(el) {
    if (!el || !el.style) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 168) + "px";
  }

  /** 绑定 agent 页面的全部事件（只绑一次） */
  function bindAgentEvents() {
    if (AGENT.bound) return;
    AGENT.bound = true;

    const newBtn = document.getElementById("agentNewBtn");
    if (newBtn) newBtn.addEventListener("click", () => { createAgentSession(); });

    const refreshBtn = document.getElementById("agentRefreshBtn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", async () => {
        await loadAgentSessions();
        if (AGENT.currentId) await loadAgentMessages(AGENT.currentId);
      });
    }

    // 会话列表：事件委托（选中 / 删除）
    const list = document.getElementById("agentSessionList");
    if (list) {
      list.addEventListener("click", (ev) => {
        const delBtn = ev.target.closest ? ev.target.closest("[data-del]") : null;
        if (delBtn) {
          ev.stopPropagation();
          deleteAgentSession(delBtn.getAttribute("data-del"));
          return;
        }
        const item = ev.target.closest ? ev.target.closest("[data-sid]") : null;
        if (item) selectAgentSession(item.getAttribute("data-sid"));
      });
    }

    // 侧边栏折叠（桌面隐藏 / 移动端抽屉）
    const toggle = document.getElementById("agentSideToggle");
    const wrap = document.getElementById("agentWrap");
    if (toggle && wrap) {
      toggle.addEventListener("click", () => {
        if (window.matchMedia("(max-width: 992px)").matches) {
          wrap.classList.toggle("agent-side-open");
        } else {
          wrap.classList.toggle("agent-side-hidden");
        }
      });
    }

    // 发送
    const sendBtn = document.getElementById("agentSendBtn");
    if (sendBtn) sendBtn.addEventListener("click", () => { sendAgentMessage(); });

    const input = document.getElementById("agentInput");
    if (input) {
      input.addEventListener("input", () => autoResizeAgentInput(input));
      input.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter") return;
        if (ev.shiftKey) return;                 // Shift+Enter 换行
        ev.preventDefault();                     // Enter / Ctrl+Enter 发送
        sendAgentMessage();
      });
    }

    // 空状态快捷提问
    const msgBox = document.getElementById("agentMessages");
    if (msgBox) {
      msgBox.addEventListener("click", (ev) => {
        const tip = ev.target.closest ? ev.target.closest(".agent-tip") : null;
        if (!tip) return;
        const el = document.getElementById("agentInput");
        if (!el) return;
        el.value = tip.getAttribute("data-tip") || tip.textContent || "";
        autoResizeAgentInput(el);
        sendAgentMessage();
      });
    }

    // 确认弹窗
    const okBtn = document.getElementById("agentConfirmApprove");
    if (okBtn) okBtn.addEventListener("click", () => { submitAgentConfirm(true); });
    const noBtn = document.getElementById("agentConfirmReject");
    if (noBtn) noBtn.addEventListener("click", () => { submitAgentConfirm(false); });
  }

  /** AI 智能助手页面入口 */
  async function initAgent() {
    bindAgentEvents();
    await loadAgentSessions();
    if (AGENT.sessions.length) {
      await selectAgentSession(AGENT.sessions[0].id);
    }
    const input = document.getElementById("agentInput");
    if (input) input.focus();
  }
})();
