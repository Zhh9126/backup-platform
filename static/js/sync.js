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

  const MODES = [
    { value: "full", label: "全量同步" },
    { value: "incremental", label: "增量同步" },
    { value: "realtime", label: "实时同步（Flink CDC）" },
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
  document.addEventListener("DOMContentLoaded", function () {
    bindEvents();
    refreshTasks();
    fillDbTypeSelect($("srcDbType"));
    fillDbTypeSelect($("tgtDbType"));
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
      tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">暂无同步任务</td></tr>';
      return;
    }
    tbody.innerHTML = tasks.map(function (t) {
      return `<tr>
        <td>${t.id}</td>
        <td>${esc(t.name)}</td>
        <td>${esc(t.src_db_display || t.src_db_type || "-")} <i class="bi bi-arrow-right"></i> ${esc(t.tgt_db_display || t.tgt_db_type || "-")}</td>
        <td>${esc(t.source_table || "-")} <i class="bi bi-arrow-right"></i> ${esc(t.target_table || "-")}</td>
        <td>${esc(t.sync_mode || "full")} / ${esc(t.save_mode || "append")}</td>
        <td>${statusBadge(t.last_status)}</td>
        <td>${fmtTime(t.last_run_at)}</td>
        <td>${esc(t.message || "")}</td>
        <td>
          <button class="btn btn-sm btn-primary" onclick="SYNC.editTask(${t.id})">编辑</button>
          <button class="btn btn-sm btn-success" onclick="SYNC.runTask(${t.id})">运行</button>
          <button class="btn btn-sm btn-outline-info" onclick="SYNC.validateTask(${t.id})">校验</button>
          <button class="btn btn-sm btn-danger" onclick="SYNC.deleteTask(${t.id})">删除</button>
        </td>
      </tr>`;
    }).join("");
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
    $("batchSize").value = t.batch_size || 1000;
    $("sourceWhere").value = t.source_where || "";
    $("incrementalColumn").value = t.incremental_column || "";
    $("incrementalValue").value = t.incremental_value || "";
    $("errorThreshold").value = t.error_threshold || 0;
    if ($("fullDbMigrate")) $("fullDbMigrate").checked = !!t.full_db_migrate;
    if ($("validateBeforeRun")) $("validateBeforeRun").checked = !!t.validate_before_run;
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

  function onSrcDbTypeChange() {
    const t = $("srcDbType").value;
    const needSchema = t === "postgresql" || t === "kingbase" || t === "oracle";
    $("srcSchemaWrap").style.display = needSchema ? "block" : "none";
    $("srcPort").value = $("srcPort").value || (t === "postgresql" ? 5432 : 3306);
  }

  function onTgtDbTypeChange() {
    const t = $("tgtDbType").value;
    const needSchema = t === "postgresql" || t === "kingbase" || t === "oracle";
    $("tgtSchemaWrap").style.display = needSchema ? "block" : "none";
    $("tgtPort").value = $("tgtPort").value || (t === "postgresql" ? 5432 : 3306);
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
  window.SYNC = {
    editTask: async function (id) {
      try {
        const res = await api("GET", "/api/sync-tasks/" + id);
        if (!res.success) throw new Error(res.message);
        openModal(res.data);
      } catch (e) {
        toast("加载任务失败：" + e.message, "danger");
      }
    },
    runTask: async function (id) {
      try {
        const res = await api("POST", "/api/sync-tasks/" + id + "/run", {});
        toast(res.message, res.success ? "success" : "danger");
        setTimeout(refreshTasks, 500);
      } catch (e) {
        toast("启动失败：" + e.message, "danger");
      }
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
      try {
        const res = await api("POST", "/api/sync-tasks/" + id + "/validate", {});
        if (res.success && res.validated) {
          if (res.passed) {
            toast("Schema 校验通过");
          } else {
            let msg = "Schema 校验不通过：\n";
            if (res.incompatible_columns && res.incompatible_columns.length) {
              msg += res.incompatible_columns.map(function (c) {
                return "  " + c.column + ": " + c.reason + "（" + c.src_type + " → " + c.dst_type + "）";
              }).join("\n");
            }
            if (res.incompatible_row_count > 0) {
              msg += "\n  不兼容行数：" + res.incompatible_row_count;
            }
            toast(msg, "warning");
          }
        } else {
          toast(res.message || "校验失败", "danger");
        }
      } catch (e) {
        toast("校验失败：" + e.message, "danger");
      }
    },
    verifyTask: async function (id) {
      try {
        const res = await api("POST", "/api/sync-tasks/" + id + "/verify", {});
        if (res.success && res.verified) {
          if (res.passed) {
            toast("数据校验通过：所有源行在目标中均存在");
          } else {
            toast("数据校验不通过：" + res.missing_rows + "/" + res.total_source_rows + " 行缺失", "warning");
          }
        } else {
          toast(res.message || "校验失败", "danger");
        }
      } catch (e) {
        toast("校验失败：" + e.message, "danger");
      }
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
