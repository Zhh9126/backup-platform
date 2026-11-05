// -*- coding: utf-8 -*-
// 数据同步 - Reader/Writer + 字段映射
(function () {
  "use strict";
  const { api, esc, toast, statusBadge, fmtTime, fillDbTypeSelect } = window.BKP;
  const $ = function (id) { return document.getElementById(id); };

  // 全局状态
  let tasks = [];
  let currentTask = null;   // 编辑中的任务对象
  let srcColumns = [];      // 源表列元数据
  let tgtColumns = [];      // 目标表列元数据
  let mapping = [];         // [{source, target, source_type, target_type}]
  let selectedSource = null;
  let selectedTarget = null;
  let runningTimer = null;  // 运行态任务的列表自动轮询定时器
  let detailTaskId = null;  // 详情弹窗当前任务
  let detailTaskData = null;
  let detailTimer = null;   // 详情弹窗自动刷新定时器

  // 概念区分（业界定义）：迁移=一次性任务（full，跑完即止，支持覆盖写入）；
  // 同步=持续性任务（incremental/realtime，常驻运行保持两端一致）
  const MODES = [
    { value: "full", label: "数据迁移（一次性全量）" },
    { value: "incremental", label: "数据同步（周期增量）" },
    { value: "realtime", label: "数据同步（实时 CDC）" },
  ];
  const SAVE_MODES = [
    { value: "append", label: "追加写入" },
    { value: "overwrite", label: "覆盖写入" },
    { value: "upsert", label: "更新插入" },
    { value: "create_if_not_exists", label: "表不存在则创建" },
  ];
  const IDE_OPTIONS = [
    { value: "origin", label: "保持原样" },
    { value: "upper", label: "全大写" },
    { value: "lower", label: "全小写" },
    { value: "camel", label: "驼峰" },
    { value: "underscore", label: "下划线" },
  ];

  // -------------- 初始化 --------------
  document.addEventListener("DOMContentLoaded", async function () {
    bindEvents();
    refreshTasks();
    // 类型下拉依赖 META：必须先等它就绪再填充，否则会填成空（这正是
    // 「新建同步任务里数据库类型选不了」的根因——此前 META 由 app.js 异步
    // 加载，本脚本同步调用，永远抢在前面拿到空数组）。
    if (window.BKP && BKP.ensureMeta) {
      try { await BKP.ensureMeta(); } catch (e) { /* 已内部兜底 */ }
    }
    // 类型清单取「同步插件注册表」（sync_types）：注册即出现，新增库型
    // 不用改前端；默认选中 MySQL/MariaDB，方便沿用 3306。
    const typeOpts = {
      typesKey: "sync_types",
      onReady: function (el, types) {
        // 初始化时给个默认值，避免用户什么都没选就直接点"测试连接"
        if (types.indexOf("mysql") >= 0) el.value = "mysql";
      }
    };
    fillDbTypeSelect($("srcDbType"), [], typeOpts);
    fillDbTypeSelect($("tgtDbType"), [], typeOpts);
    onSrcDbTypeChange();
    onTgtDbTypeChange();
  });

  function bindEvents() {
    $("btnNew").addEventListener("click", () => openModal());
    $("btnRefresh").addEventListener("click", refreshTasks);
    $("btnSave").addEventListener("click", saveTask);
    $("btnTestSrc").addEventListener("click", () => testConnection("source"));
    $("btnTestTgt").addEventListener("click", () => testConnection("target"));
    $("btnLoadSrcCols").addEventListener("click", loadSourceColumns);
    $("btnLoadTgtCols").addEventListener("click", loadTargetColumns);
    $("btnSameNameMap").addEventListener("click", sameNameMapping);
    $("btnSameRowMap").addEventListener("click", sameRowMapping);
    $("btnClearMap").addEventListener("click", clearMapping);

    $("srcDbType").addEventListener("change", onSrcDbTypeChange);
    $("tgtDbType").addEventListener("change", onTgtDbTypeChange);
    $("syncMode").addEventListener("change", onSyncModeChange);

    window.addEventListener("resize", drawMappingLines);
  }

  // -------------- 任务列表 --------------
  async function refreshTasks() {
    try {
      const res = await api("GET", "/api/sync-tasks");
      if (!res.success) throw new Error(res.message);
      tasks = res.data || [];
      renderTasks();
    } catch (e) {
      toast("加载任务失败：" + e.message, "danger");
    }
  }

  function renderTasks() {
    const tbody = $("taskTableBody");
    if (!tasks.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">暂无同步任务</td></tr>';
      return;
    }
    const pg = Pager.slice("syncPage", tasks, 13);
    tbody.innerHTML = pg.list.map(function (t, ti) {
      // 实时任务运行徽章：进程内 runner 真实状态（rt_running 由后端注入）
      let statusCell;
      if (t.sync_mode === "realtime" && t.rt_running) {
        statusCell = '<span class="badge badge-run">实时运行中</span>';
      } else {
        statusCell = statusBadge(t.status === "running" ? "running" : (t.last_status || t.status));
      }
      // 摘要：实时任务透出水位与监听状态；普通任务显示执行消息
      let msgCell = "";
      if (t.sync_mode === "realtime") {
        const wm = t.realtime_watermark;
        const wmTxt = (wm && wm.col) ? ("水位: " + esc(wm.col) + " = " + esc(wm.value)) : "水位: 未建立";
        msgCell = '<div class="small">' + wmTxt + "</div>" +
          '<div class="small text-muted">' + esc(t.message || "") + "</div>";
      } else {
        msgCell = esc(t.message || "");
      }
      return `<tr>
        <td>${pg.startNo + ti}</td>
        <td>${esc(t.name)}</td>
        <td>${esc(t.src_db_display || t.src_db_type || "-")} <i class="bi bi-arrow-right"></i> ${esc(t.tgt_db_display || t.tgt_db_type || "-")}</td>
        <td>${esc(t.source_table || "-")} <i class="bi bi-arrow-right"></i> ${esc(t.target_table || "-")}</td>
        <td>${(t.sync_mode === "full") ? "迁移·一次性" : "同步·持续"} / ${esc(t.save_mode || "append")}</td>
        <td>${statusCell}</td>
        <td class="small text-nowrap">${fmtTime(t.created_at) || "-"}</td>
        <td class="small text-nowrap">${fmtTime(t.last_run_at) || "-"}</td>
        <td class="small">${msgCell}</td>
        <td>
          <button class="btn btn-sm btn-primary" onclick="SYNC.editTask(${t.id})">编辑</button>
          ${t.sync_mode === "realtime" && t.rt_running
            ? '<button class="btn btn-sm btn-warning" onclick="SYNC.stopTask(' + t.id + ')">停止</button>'
            : '<button class="btn btn-sm btn-success" onclick="SYNC.runTask(' + t.id + ')">运行</button>'}
          <button class="btn btn-sm btn-outline-primary" onclick="SYNC.detailTask(${t.id})">详情</button>
          <button class="btn btn-sm btn-outline-secondary" onclick="SYNC.precheckTask(${t.id})">预检</button>
          <button class="btn btn-sm btn-outline-info" onclick="SYNC.validateTask(${t.id})">校验</button>
          <button class="btn btn-sm btn-danger" onclick="SYNC.deleteTask(${t.id})">删除</button>
        </td>
      </tr>`;
    }).join("");
    Pager.attach("syncPage", tbody, pg.page, pg.pages, pg.total, renderTasks);
    // 运行态任务自动轮询：实时/长任务的状态与进度需要及时反映（3s）
    clearTimeout(runningTimer);
    if (tasks.some(function (t) {
      return (t.status || t.last_status) === "running" ||
        (t.sync_mode === "realtime" && t.rt_running);
    })) {
      runningTimer = setTimeout(refreshTasks, 3000);
    }
  }

  // -------------- Modal / 表单 --------------
  function openModal(task) {
    currentTask = task || null;
    $("modalTitle").textContent = task ? "编辑同步任务" : "新建同步任务";
    resetForm();
    if (task) fillForm(task);
    // 填充同步模式/保存模式/ide 下拉
    fillSelect($("syncMode"), MODES, task ? task.sync_mode : "full");
    fillSelect($("saveMode"), SAVE_MODES, task ? task.save_mode : "append");
    // 概念联动：覆盖写入仅适用于数据迁移（full）；同步类禁用
    const syncModeSel = $("syncMode");
    if (syncModeSel) {
      const toggleOverwrite = function () {
        const isMigration = syncModeSel.value === "full";
        Array.from($("saveMode").options).forEach(function (o) {
          if (o.value === "overwrite") o.disabled = !isMigration;
        });
        if (!isMigration && $("saveMode").value === "overwrite")
          $("saveMode").value = "upsert";
      };
      syncModeSel.addEventListener("change", toggleOverwrite);
      toggleOverwrite();
    }
    fillSelect($("fieldIde"), IDE_OPTIONS, task ? task.field_ide : "origin");
    onSyncModeChange();
    bootstrap.Modal.getOrCreateInstance($("taskModal")).show();
  }

  function resetForm() {
    $("taskForm").reset();
    $("taskId").value = "";
    $("srcSchemaWrap").style.display = "none";
    $("tgtSchemaWrap").style.display = "none";
    srcColumns = [];
    tgtColumns = [];
    mapping = [];
    selectedSource = null;
    selectedTarget = null;
    renderFieldLists();
    drawMappingLines();
  }

  function fillForm(t) {
    $("taskId").value = t.id;
    $("name").value = t.name || "";
    $("sourceType").value = t.source_type || "manual";
    $("sourceTaskId").value = t.source_task_id || "";
    $("srcDbType").value = t.src_db_type || "";
    $("srcHost").value = t.src_host || "";
    $("srcPort").value = t.src_port || "";
    $("srcUsername").value = t.src_username || "";
    $("srcPassword").value = t.src_password || "";
    $("srcDbName").value = t.src_db_name || "";
    $("srcSchema").value = t.src_schema || "";
    $("sourceTable").value = t.source_table || "";
    $("tgtDbType").value = t.tgt_db_type || "";
    $("tgtHost").value = t.tgt_host || "";
    $("tgtPort").value = t.tgt_port || "";
    $("tgtUsername").value = t.tgt_username || "";
    $("tgtPassword").value = t.tgt_password || "";
    $("tgtDbName").value = t.tgt_db_name || "";
    $("tgtSchema").value = t.tgt_schema || "";
    $("targetTable").value = t.target_table || "";
    if ($("excludeTables")) $("excludeTables").value = (t.exclude_tables || []).join(", ");
    if ($("parallelTables")) $("parallelTables").value = String(t.parallel_tables || 1);
    $("batchSize").value = t.batch_size || 1000;
    $("sourceWhere").value = t.source_where || "";
    $("incrementalColumn").value = t.incremental_column || "";
    $("incrementalValue").value = t.incremental_value || "";
    $("errorThreshold").value = t.error_threshold || 0;
    if ($("fullDbMigrate")) $("fullDbMigrate").checked = !!t.full_db_migrate;
    if ($("validateBeforeRun")) $("validateBeforeRun").checked = !!t.validate_before_run;
    if ($("precheckSampleRows")) $("precheckSampleRows").value = (t.precheck_sample_rows !== undefined && t.precheck_sample_rows !== null) ? t.precheck_sample_rows : 20;
    if ($("verifyAfterRun")) $("verifyAfterRun").checked = !!t.verify_after_run;
    $("scheduleType").value = t.schedule_type || "none";
    $("cronExpr").value = t.cron_expr || "";
    $("intervalMinutes").value = t.interval_minutes || "";
    mapping = (t.column_mapping || []).slice();
    onSrcDbTypeChange();
    onTgtDbTypeChange();
  }

  function fillSelect(sel, options, selectedValue) {
    sel.innerHTML = options.map(function (o) {
      return `<option value="${esc(o.value)}"${o.value === selectedValue ? " selected" : ""}>${esc(o.label)}</option>`;
    }).join("");
  }

  // 默认端口以服务端 META.default_ports 为准（各库型差异大：PG 5432、
  // Oracle 1521、达梦 5236、金仓 54321…），不再用三元表达式写死两个值。
  function defaultPort(t) {
    const dp = (window.BKP && BKP.META && BKP.META.default_ports) || {};
    return dp[t] || 3306;
  }

  // 类型切换时联动端口：仅当端口为空、或仍等于「上一个类型的默认端口」
  // （即用户没手改过）才替换，避免覆盖用户填写的自定义端口。
  function applyDefaultPort(portId, type, prevType) {
    const cur = String($(portId).value || "").trim();
    const prevDefault = prevType ? String(defaultPort(prevType)) : "";
    if (!cur || (prevDefault && cur === prevDefault)) {
      $(portId).value = defaultPort(type);
    }
  }

  function schemaNeeded(t) {
    return t === "postgresql" || t === "kingbase" || t === "oracle";
  }

  function onSrcDbTypeChange() {
    const t = $("srcDbType").value;
    $("srcSchemaWrap").style.display = schemaNeeded(t) ? "block" : "none";
    applyDefaultPort("srcPort", t, $("srcDbType").dataset.prevType);
    $("srcDbType").dataset.prevType = t;
  }

  function onTgtDbTypeChange() {
    const t = $("tgtDbType").value;
    $("tgtSchemaWrap").style.display = schemaNeeded(t) ? "block" : "none";
    applyDefaultPort("tgtPort", t, $("tgtDbType").dataset.prevType);
    $("tgtDbType").dataset.prevType = t;
  }

  function onSyncModeChange() {
    const mode = $("syncMode").value;
    $("incrementalWrap").style.display = mode === "incremental" ? "block" : "none";
    $("realtimeWrap").style.display = mode === "realtime" ? "block" : "none";
  }

  // -------------- 字段列表与映射 --------------
  async function loadSourceColumns() {
    if (!currentTask || !currentTask.id) {
      toast("请先保存任务以获取表/列元数据", "warning");
      return;
    }
    const table = $("sourceTable").value.trim();
    if (!table) { toast("请填写源表名", "warning"); return; }
    try {
      const res = await api("GET", "/api/sync-tasks/" + currentTask.id + "/columns?table=" + encodeURIComponent(table));
      if (!res.success) throw new Error(res.message);
      srcColumns = res.data || [];
      renderFieldLists();
      drawMappingLines();
    } catch (e) {
      toast("加载源表列失败：" + e.message, "danger");
    }
  }

  async function loadTargetColumns() {
    if (!currentTask || !currentTask.id) {
      toast("请先保存任务以获取表/列元数据", "warning");
      return;
    }
    const table = $("targetTable").value.trim() || $("sourceTable").value.trim();
    if (!table) { toast("请填写目标表名", "warning"); return; }
    try {
      // 复用源端 Reader 逻辑获取目标列：把 source_table 临时换成目标表
      const res = await api("GET", "/api/sync-tasks/" + currentTask.id + "/columns?table=" + encodeURIComponent(table));
      if (!res.success) throw new Error(res.message);
      tgtColumns = res.data || [];
      renderFieldLists();
      drawMappingLines();
    } catch (e) {
      toast("加载目标表列失败：" + e.message, "danger");
    }
  }

  function renderFieldLists() {
    $("srcFields").innerHTML = srcColumns.map(function (c, idx) {
      const active = selectedSource === c.name ? " active" : "";
      const mapped = mapping.some(function (m) { return m.source === c.name; }) ? " mapped" : "";
      return `<div class="field-item${active}${mapped}" data-side="src" data-name="${esc(c.name)}" data-type="${esc(c.type)}" data-idx="${idx}">
        <div class="field-name">${esc(c.name)}</div>
        <div class="field-type">${esc(c.type)} ${c.is_primary ? '<span class="badge bg-warning text-dark">PK</span>' : ''}</div>
      </div>`;
    }).join("");
    $("tgtFields").innerHTML = tgtColumns.map(function (c, idx) {
      const active = selectedTarget === c.name ? " active" : "";
      const mapped = mapping.some(function (m) { return m.target === c.name; }) ? " mapped" : "";
      return `<div class="field-item${active}${mapped}" data-side="tgt" data-name="${esc(c.name)}" data-type="${esc(c.type)}" data-idx="${idx}">
        <div class="field-name">${esc(c.name)}</div>
        <div class="field-type">${esc(c.type)} ${c.is_primary ? '<span class="badge bg-warning text-dark">PK</span>' : ''}</div>
      </div>`;
    }).join("");
    $("srcFields").querySelectorAll(".field-item").forEach(function (el) {
      el.addEventListener("click", onSrcFieldClick);
    });
    $("tgtFields").querySelectorAll(".field-item").forEach(function (el) {
      el.addEventListener("click", onTgtFieldClick);
    });
  }

  function onSrcFieldClick(ev) {
    const name = ev.currentTarget.dataset.name;
    const type = ev.currentTarget.dataset.type;
    selectedSource = name;
    renderFieldLists();
    tryAddMapping();
  }

  function onTgtFieldClick(ev) {
    const name = ev.currentTarget.dataset.name;
    const type = ev.currentTarget.dataset.type;
    selectedTarget = name;
    renderFieldLists();
    tryAddMapping();
  }

  function tryAddMapping() {
    if (!selectedSource || !selectedTarget) return;
    const src = srcColumns.find(function (c) { return c.name === selectedSource; });
    const tgt = tgtColumns.find(function (c) { return c.name === selectedTarget; });
    // 去重：同一 source/target 只保留最新
    mapping = mapping.filter(function (m) {
      return m.source !== selectedSource && m.target !== selectedTarget;
    });
    mapping.push({
      source: selectedSource,
      target: selectedTarget,
      source_type: src ? src.type : "STRING",
      target_type: tgt ? tgt.type : "STRING",
    });
    selectedSource = null;
    selectedTarget = null;
    renderFieldLists();
    drawMappingLines();
  }

  function sameNameMapping() {
    if (!srcColumns.length || !tgtColumns.length) {
      toast("请先加载源/目标字段", "warning"); return;
    }
    srcColumns.forEach(function (sc) {
      const tc = tgtColumns.find(function (c) { return c.name === sc.name; });
      if (tc) {
        mapping = mapping.filter(function (m) { return m.source !== sc.name && m.target !== tc.name; });
        mapping.push({ source: sc.name, target: tc.name, source_type: sc.type, target_type: tc.type });
      }
    });
    renderFieldLists();
    drawMappingLines();
    toast("同名映射完成");
  }

  function sameRowMapping() {
    if (!srcColumns.length || !tgtColumns.length) {
      toast("请先加载源/目标字段", "warning"); return;
    }
    const len = Math.min(srcColumns.length, tgtColumns.length);
    mapping = [];
    for (let i = 0; i < len; i++) {
      mapping.push({
        source: srcColumns[i].name,
        target: tgtColumns[i].name,
        source_type: srcColumns[i].type,
        target_type: tgtColumns[i].type,
      });
    }
    renderFieldLists();
    drawMappingLines();
    toast("同行映射完成");
  }

  function clearMapping() {
    mapping = [];
    renderFieldLists();
    drawMappingLines();
  }

  function drawMappingLines() {
    const svg = $("mappingSvg");
    const panel = $("mappingPanel");
    if (!panel) return;
    svg.innerHTML = "";
    const panelRect = panel.getBoundingClientRect();
    mapping.forEach(function (m, idx) {
      const srcEl = panel.querySelector('.field-item[data-side="src"][data-name="' + CSS.escape(m.source) + '"]');
      const tgtEl = panel.querySelector('.field-item[data-side="tgt"][data-name="' + CSS.escape(m.target) + '"]');
      if (!srcEl || !tgtEl) return;
      const sRect = srcEl.getBoundingClientRect();
      const tRect = tgtEl.getBoundingClientRect();
      const x1 = sRect.right - panelRect.left;
      const y1 = sRect.top + sRect.height / 2 - panelRect.top;
      const x2 = tRect.left - panelRect.left;
      const y2 = tRect.top + tRect.height / 2 - panelRect.top;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      const cp1x = x1 + (x2 - x1) / 2;
      const cp2x = x1 + (x2 - x1) / 2;
      path.setAttribute("d", `M ${x1} ${y1} C ${cp1x} ${y1}, ${cp2x} ${y2}, ${x2} ${y2}`);
      path.setAttribute("stroke", "#0d6efd");
      path.setAttribute("stroke-width", "2");
      path.setAttribute("fill", "none");
      path.setAttribute("class", "map-line");
      path.style.pointerEvents = "stroke";
      path.addEventListener("click", function () {
        mapping.splice(idx, 1);
        renderFieldLists();
        drawMappingLines();
      });
      svg.appendChild(path);
    });
  }

  // -------------- 保存 / 测试 / 运行 --------------
  function gatherPayload() {
    return {
      name: $("name").value.trim(),
      source_type: $("sourceType").value,
      source_task_id: $("sourceTaskId").value ? parseInt($("sourceTaskId").value) : null,
      src_db_type: $("srcDbType").value,
      src_host: $("srcHost").value.trim(),
      src_port: parseInt($("srcPort").value) || 0,
      src_username: $("srcUsername").value.trim(),
      src_password: $("srcPassword").value,
      src_db_name: $("srcDbName").value.trim(),
      src_schema: $("srcSchema").value.trim(),
      source_table: $("sourceTable").value.trim(),
      tgt_db_type: $("tgtDbType").value,
      tgt_host: $("tgtHost").value.trim(),
      tgt_port: parseInt($("tgtPort").value) || 0,
      tgt_username: $("tgtUsername").value.trim(),
      tgt_password: $("tgtPassword").value,
      tgt_db_name: $("tgtDbName").value.trim(),
      tgt_schema: $("tgtSchema").value.trim(),
      target_table: $("targetTable").value.trim(),
      sync_mode: $("syncMode").value,
      save_mode: $("saveMode").value,
      column_mapping: mapping,
      field_ide: $("fieldIde").value,
      precheck_sample_rows: $("precheckSampleRows") ? (parseInt($("precheckSampleRows").value) || 0) : 20,
      batch_size: parseInt($("batchSize").value) || 1000,
      source_where: $("sourceWhere").value.trim(),
      incremental_column: $("incrementalColumn").value.trim(),
      incremental_value: $("incrementalValue").value.trim(),
      error_threshold: parseInt($("errorThreshold").value) || 0,
      full_db_migrate: $("fullDbMigrate") ? ($("fullDbMigrate").checked ? 1 : 0) : 0,
      validate_before_run: $("validateBeforeRun") ? ($("validateBeforeRun").checked ? 1 : 0) : 0,
      verify_after_run: $("verifyAfterRun") ? ($("verifyAfterRun").checked ? 1 : 0) : 0,
      schedule_type: $("scheduleType").value,
      cron_expr: $("cronExpr").value.trim(),
      interval_minutes: $("intervalMinutes").value ? parseInt($("intervalMinutes").value) : null,
      enabled: 1,
      exclude_tables: $("excludeTables") ? ($("excludeTables").value || "")
        .split(",").map(function (s) { return s.trim(); }).filter(Boolean) : [],
      parallel_tables: $("parallelTables") ? (parseInt($("parallelTables").value) || 1) : 1,
    };
  }

  async function saveTask() {
    const payload = gatherPayload();
    if (!payload.name) { toast("请输入任务名称", "warning"); return; }
    if (!payload.src_db_type || !payload.tgt_db_type) { toast("请选择源/目标数据库类型", "warning"); return; }
    if (!payload.source_table) { toast("请输入源表名", "warning"); return; }
    try {
      let res;
      if (currentTask && currentTask.id) {
        res = await api("PUT", "/api/sync-tasks/" + currentTask.id, payload);
      } else {
        res = await api("POST", "/api/sync-tasks", payload);
        if (res.success && res.data && res.data.id) {
          currentTask = { id: res.data.id };
        }
      }
      if (!res.success) throw new Error(res.message);
      toast("保存成功");
      bootstrap.Modal.getOrCreateInstance($("taskModal")).hide();
      refreshTasks();
    } catch (e) {
      toast("保存失败：" + e.message, "danger");
    }
  }

  async function testConnection(side) {
    if (!currentTask || !currentTask.id) { toast("请先保存任务", "warning"); return; }
    try {
      const res = await api("POST", "/api/sync-tasks/" + currentTask.id + "/test/" + side, {});
      toast(res.message, res.success ? "success" : "danger");
    } catch (e) {
      toast("测试失败：" + e.message, "danger");
    }
  }

  // -------------- 外部暴露 --------------
  // 按钮忙态：慢操作（预检/校验/启动对离线库型要探测 10-30 秒）必须有即时
  // 视觉反馈，否则用户体感"点了没反应"。返回恢复函数；非按钮触发时为空操作。
  function btnBusy(label) {
    var btn = (typeof window.event !== "undefined") && window.event &&
              window.event.target;
    if (!btn || btn.tagName !== "BUTTON") return function () {};
    var orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = label;
    return function () {
      try {
        btn.disabled = false;
        btn.textContent = orig;
      } catch (e) { /* 节点已被重渲染，忽略 */ }
    };
  }

  window.SYNC = {
    editTask: async function (id) {
      var done = btnBusy("加载中…");
      try {
        const res = await api("GET", "/api/sync-tasks/" + id);
        if (!res.success) throw new Error(res.message);
        openModal(res.data);
      } catch (e) {
        toast("加载任务失败：" + e.message, "danger");
      } finally {
        done();
      }
    },
    runTask: async function (id) {
      var done = btnBusy("启动中…");
      toast("正在启动：先执行预检探测，离线库型可能需要 10-30 秒…", "info");
      try {
        // 迁移前预校验：字段/结构不合适直接拒绝启动
        const pc = await api("POST", "/api/sync-tasks/" + id + "/precheck", {});
        if (pc.success && pc.passed === false) {
          SYNC.showPrecheckReport(pc, true);
          refreshTasks();
          return;
        }
        if (pc.success && pc.warn > 0) SYNC.showPrecheckReport(pc, false);
        const res = await api("POST", "/api/sync-tasks/" + id + "/run", {});
        toast(res.message, res.success ? "success" : "danger");
        setTimeout(refreshTasks, 500);
      } catch (e) {
        // run 端点 400 时带 precheck 报告体：弹模态框展示失败明细，
        // 而不是一句 toast 让用户看不到具体原因（2026-11-04 用户反馈）
        const d = e.data || {};
        if (d.precheck) {
          SYNC.showPrecheckReport(d.precheck, true);
          refreshTasks();
          return;
        }
        toast("启动失败：" + e.message, "danger");
      } finally {
        done();
      }
    },
    precheckTask: async function (id) {
      var done = btnBusy("预检中…");
      toast("正在预检：源/目标库连通与结构探测中，离线库型可能需要 10-30 秒…", "info");
      try {
        const pc = await api("POST", "/api/sync-tasks/" + id + "/precheck", {});
        if (!pc.success) throw new Error(pc.message || "预校验执行失败");
        SYNC.showPrecheckReport(pc, pc.passed === false);
      } catch (e) {
        toast("预校验失败：" + e.message, "danger");
      } finally {
        done();
      }
    },
    showPrecheckReport: function (pc, blocked) {
      const esc = window.esc || function (s) { return s; };
      $("precheckTitle").textContent = blocked ? "预校验未通过（已阻止执行）" : "预校验报告";
      const icon = {pass: ["✓", "success"], warn: ["⚠", "warning"], fail: ["✗", "danger"]};
      const kind = function (c) {
        if (c.startsWith("column_compat")) return "结构/类型兼容";
        if (c.startsWith("data_sample")) return "数据级试写";
        if (c === "capacity") return "容量预估";
        if (c === "charset") return "字符集";
        if (c === "fk_integrity") return "外键完整性";
        if (c === "task_kind") return "任务性质";
        if (c === "primary_key") return "主键";
        if (c === "incremental_column") return "增量列";
        return "基础检查";
      };
      let html = "";
      const head = blocked
        ? '<div class="alert alert-danger py-2">预校验未通过，迁移已被阻止</div>'
        : '<div class="alert alert-' + (pc.warn > 0 ? "warning" : "success") + ' py-2">'
          + '预校验通过' + (pc.warn > 0 ? "（含 " + pc.warn + " 项警告，请确认风险）" : "") + '</div>';
      html += head + '<div class="table-responsive"><table class="table table-sm table-bordered mb-0">'
        + '<thead><tr><th style="width:60px">结果</th><th style="width:150px">类别</th><th>说明</th></tr></thead><tbody>';
      (pc.items || []).forEach(function (i) {
        const ic = icon[i.status] || ["·", "secondary"];
        html += '<tr><td class="text-' + ic[1] + '">' + ic[0] + "</td><td>" + esc(kind(i.check))
          + "</td><td>" + esc(i.message || "");
        if ((i.detail || []).length) {
          html += '<div class="small text-muted mt-1">';
          i.detail.forEach(function (d) {
            if (d.big_tables !== undefined) {
              (d.big_tables || []).forEach(function (b) {
                html += "大表: " + esc(b.table) + "（" + (b.rows === null ? "未统计" : b.rows) + " 行 / "
                  + (b.size_mb === null ? "?" : b.size_mb.toFixed(1)) + " MB）<br>"; });
            } else {
              html += "• " + esc((d.table ? d.table + "." : "") + (d.column || "") + " " + (d.message || "")) + "<br>";
            }
          });
          html += "</div>";
        }
        html += "</td></tr>";
      });
      html += "</tbody></table></div>";
      $("precheckBody").innerHTML = html;
      new bootstrap.Modal($("precheckModal")).show();
    },
    deleteTask: async function (id) {
      if (!confirm("确定删除该同步任务？")) return;
      try {
        const res = await api("DELETE", "/api/sync-tasks/" + id);
        if (!res.success) throw new Error(res.message);
        toast("删除成功");
        refreshTasks();
      } catch (e) {
        toast("删除失败：" + e.message, "danger");
      }
    },
    validateTask: async function (id) {
      var done = btnBusy("校验中…");
      toast("正在执行结构校验：探测源/目标表结构，离线库型可能较慢…", "info");
      try {
        const res = await api("POST", "/api/sync-tasks/" + id + "/validate", {});
        if (res.success && res.validated) {
          if (res.passed) {
            SYNC.showResultReport("Schema 结构校验报告",
              '<div class="alert alert-success py-2">结构校验通过：源/目标列类型兼容</div>');
          } else {
            let rows = "";
            (res.incompatible_columns || []).forEach(function (col) {
              rows += '<tr><td>' + esc(col.column) + "</td><td>" + esc(col.src_type) +
                "</td><td>" + esc(col.dst_type) + "</td><td>" + esc(col.reason || "") + "</td></tr>";
            });
            SYNC.showResultReport("Schema 结构校验报告",
              '<div class="alert alert-danger py-2">结构校验不通过：不兼容列 ' +
              (res.incompatible_columns || []).length + " 处，涉及约 " +
              esc(res.incompatible_row_count || 0) + " 行数据</div>" +
              '<table class="table table-sm table-bordered"><thead><tr><th>列</th><th>源类型</th><th>目标类型</th><th>原因</th></tr></thead><tbody>' +
              rows + "</tbody></table>");
          }
        } else {
          SYNC.showResultReport("Schema 结构校验报告",
            '<div class="alert alert-danger py-2">校验执行失败：' + esc(res.message || "未知错误") + "</div>");
        }
      } catch (e) {
        toast("校验失败：" + e.message, "danger");
      } finally {
        done();
      }
    },
    verifyTask: async function (id) {
      try {
        const res = await api("POST", "/api/sync-tasks/" + id + "/verify", {});
        if (res.success && res.verified) {
          if (res.passed) {
            SYNC.showResultReport("数据一致性校验报告",
              '<div class="alert alert-success py-2">数据校验通过：全部 ' +
              esc(res.total_source_rows || 0) + " 行源数据在目标端均存在</div>");
          } else {
            SYNC.showResultReport("数据一致性校验报告",
              '<div class="alert alert-warning py-2">数据校验不通过：源 ' +
              esc(res.total_source_rows || 0) + " 行，目标缺失 " +
              esc(res.missing_rows || 0) + " 行（可能未同步完成或被目标删除）</div>");
          }
        } else {
          SYNC.showResultReport("数据一致性校验报告",
            '<div class="alert alert-danger py-2">校验执行失败：' + esc(res.message || "未知错误") + "</div>");
        }
      } catch (e) {
        toast("校验失败：" + e.message, "danger");
      }
    },
    // 结果报告复用预检模态框展示（替代一闪而过的 toast——用户反馈看不到具体信息）
    showResultReport: function (title, html) {
      $("precheckTitle").textContent = title;
      $("precheckBody").innerHTML = html;
      new bootstrap.Modal($("precheckModal")).show();
    },
    stopTask: async function (id) {
      if (!confirm("确定停止该实时同步任务？")) return;
      try {
        const res = await api("POST", "/api/sync-tasks/" + id + "/stop", {});
        toast(res.message || "已停止", res.success ? "success" : "danger");
        refreshTasks();
      } catch (e) {
        toast("停止失败：" + e.message, "danger");
      }
    },
    // -------------- 同步详情弹窗（执行记录 / 实时状态 / 水位） --------------
    detailTask: async function (id) {
      detailTaskId = id;
      try {
        const res = await api("GET", "/api/sync-tasks/" + id);
        if (!res.success) throw new Error(res.message);
        detailTaskData = res.data;
        await SYNC.renderDetail();
        new bootstrap.Modal($("detailModal")).show();
        // 实时运行中：5s 自动刷新（水位/行数持续变化）
        clearInterval(detailTimer);
        detailTimer = setInterval(function () {
          const modalEl = $("detailModal");
          const isOpen = modalEl.classList.contains("show");
          if (!isOpen) { clearInterval(detailTimer); return; }
          if (detailTaskData && detailTaskData.rt_running) SYNC.renderDetail();
        }, 5000);
      } catch (e) {
        toast("加载详情失败：" + e.message, "danger");
      }
    },
    refreshDetail: function () {
      if (detailTaskId) SYNC.detailTask(detailTaskId);
    },
    renderDetail: async function () {
      const t = detailTaskData || {};
      const esc = window.esc || function (s) { return s; };
      $("detailTitle").textContent = "同步详情 · " + (t.name || ("任务 " + t.id));
      $("detailRtBadge").innerHTML = (t.sync_mode === "realtime")
        ? (t.rt_running
          ? '<span class="badge badge-run">实时运行中</span>'
          : '<span class="badge bg-secondary">实时已停止</span>')
        : "";
      // 概要信息
      const wm = t.realtime_watermark;
      const wmTxt = (wm && wm.col) ? (esc(wm.col) + " = " + esc(wm.value)) : "未建立";
      const info = [
        ["链路", (t.src_db_display || t.src_db_type) + " → " + (t.tgt_db_display || t.tgt_db_type)],
        ["表", (t.source_table || "-") + " → " + (t.target_table || "-")],
        ["模式", (t.sync_mode === "full" ? "迁移·一次性" : (t.sync_mode === "realtime" ? "同步·实时 CDC" : "同步·周期增量")) + " / " + (t.save_mode || "append")],
        ["状态", t.rt_running ? "实时运行中" : (t.last_status || t.status || "-")],
        ["最近运行", fmtTime(t.last_run_at) || "-"],
        ["增量/实时水位", t.sync_mode === "realtime" ? wmTxt : (t.incremental_column || "-")],
        ["启用", t.enabled ? "是" : "否"],
      ];
      let html = '<table class="table table-sm table-bordered mb-3" style="max-width:720px"><tbody>';
      info.forEach(function (kv) {
        html += '<tr><th style="width:140px" class="text-muted">' + kv[0] + "</th><td>" + kv[1] + "</td></tr>";
      });
      html += "</tbody></table>";
      // 最近执行记录
      let recs = [];
      try {
        const rr = await api("GET", "/api/sync-tasks/" + t.id + "/records?limit=50");
        recs = rr.data || [];
      } catch (e) { /* 记录加载失败不阻断 */ }
      recs = recs.slice(0, 50);
      html += '<div class="card-section-title"><i class="bi bi-list-check"></i> 最近执行记录（' + recs.length + ' 条）</div>';
      if (recs.length) {
        html += '<div class="table-responsive"><table class="table table-sm table-hover align-middle mb-0"><thead><tr>' +
          "<th>#</th><th>开始时间</th><th>结束时间</th><th>状态</th><th>读取</th><th>写入</th><th>失败行</th><th>水位</th><th>说明</th>" +
          "</tr></thead><tbody>";
        recs.forEach(function (r, i) {
          html += "<tr><td>" + (i + 1) + "</td>" +
            '<td class="small text-nowrap">' + fmtTime(r.started_at) + "</td>" +
            '<td class="small text-nowrap">' + fmtTime(r.finished_at) + "</td>" +
            "<td>" + statusBadge(r.status) + "</td>" +
            "<td>" + (r.read_rows != null ? r.read_rows : "-") + "</td>" +
            "<td>" + (r.rows_synced != null ? r.rows_synced : "-") + "</td>" +
            '<td class="' + ((r.error_rows || 0) > 0 ? "text-danger fw-bold" : "") + '">' + (r.error_rows || 0) + "</td>" +
            '<td class="small">' + esc(r.watermark || "-") + "</td>" +
            '<td class="small text-muted text-truncate" style="max-width:260px" title="' +
            esc(r.message || "") + '">' + esc(r.message || "-") + "</td></tr>";
        });
        html += "</tbody></table></div>";
      } else {
        html += '<div class="text-muted small py-3">暂无执行记录（点击「运行」后产生）</div>';
      }
      $("detailBody").innerHTML = html;
      $("detailAutoNote").textContent = (t.sync_mode === "realtime" && t.rt_running)
        ? "实时任务运行中，每 5 秒自动刷新" : "";
    },
    flinkConfig: async function (id) {
      try {
        const res = await api("GET", "/api/sync-tasks/" + id + "/flink-config");
        if (res.success && res.data) {
          let text = "=== Flink CDC 配置 ===\n\n-- Source:\n" + res.data.source_ddl + "\n\n-- Sink:\n" + res.data.sink_ddl + "\n\n-- Job:\n" + res.data.insert_sql;
          prompt("Flink CDC 配置（复制以下 SQL）", text);
        } else {
          toast(res.message || "生成失败", "danger");
        }
      } catch (e) {
        toast("生成失败：" + e.message, "danger");
      }
    },
  };
})();
