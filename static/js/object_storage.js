// -*- coding: utf-8 -*-
// 对象存储备份页面逻辑（MinIO / 阿里云 OSS / 腾讯云 COS / S3 兼容，桶侧零安装）
//
// 页面边界（与后端契约对齐，逐条都有据可查）：
//   配置校验/探活/列桶/预扫描 → /api/v1/object-storage/*（api/object_storage.py）
//   任务增删改查、立即执行      → /api/tasks（通用任务接口，db_type=object_storage）
//   备份记录                    → /api/records?db_type=object_storage
//   归档内对象清单              → /api/v1/object-storage/records/<id>/objects（分页）
//   恢复（整包 / 对象级）        → POST /api/v1/object-storage/restores
//
// 任务字段映射（core/objectstore/providers.py::task_cfg 的权威定义）：
//   host=端点  port=端口  username=AccessKey  password=SecretKey（平台加密存储）
//   db_name=桶名  extra_options=os_* 细项
//
// 对象级恢复的 keys 语义（core/engines/object_storage.py::restore）：
//   传入的 keys 与 manifest 里对象的 **key 字段**（不含版本号）做集合匹配；
//   勾选任一版本即恢复该 Key 的全部版本，包内按「旧 → 新」串行回放。
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const { api, esc, toast, fmtTime, statusBadge, humanSize } = BKP;

  const V1 = "/api/v1/object-storage";
  const DB_TYPE = "object_storage";
  const OBJ_PAGE = 50;      // 对象清单每页条数
  const RECORD_PAGE = 100;  // 备份记录每页条数

  // ------------------------- 页面状态 -------------------------
  let PROVIDERS = [];          // 厂商预设
  let TASKS = [];              // 对象存储任务
  let TASK_BY_ID = {};         // task_id → task
  let RECORDS = [];            // 备份记录（当前页）
  let RECORD_TOTAL = 0;
  let RECORD_HAS_MORE = false;
  let EDIT_ID = null;          // 正在编辑的任务 id（null=新建）
  let EDIT_EXTRA = {};         // 编辑时保留的原 extra_options（不覆盖 ssh_cred/env_vars 等未知键）
  let RECORD_SEARCH_TIMER = null;
  let RUNNING_TASKS = {};      // task_id → true（立即备份进行中，避免重复点击）

  const OBJ = {                // 对象清单弹窗状态
    recordId: null,
    page: 1,
    total: 0,
    hasMore: false,
    items: [],
    selected: new Set(),       // Set<string>：对象 key
    manifest: null,
  };

  let RESTORE = null;          // {recordId, keys:[] | null, bucket, task}

  // ------------------------- 基础工具 -------------------------
  function errMsg(e) {
    return (e && e.message) ? String(e.message) : "操作失败";
  }

  function modal(id) {
    const el = $(id);
    if (!el) return null;
    return window.bootstrap ? bootstrap.Modal.getOrCreateInstance(el) : null;
  }

  function setBusy(btn, busy) {
    if (!btn) return;
    if (busy) {
      btn.dataset.busyHtml = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    } else {
      btn.disabled = false;
      if (btn.dataset.busyHtml) btn.innerHTML = btn.dataset.busyHtml;
    }
  }

  function scheduleText(t) {
    if (!t) return "手动";
    if (t.schedule_type === "cron" && t.cron_expr) {
      return "cron " + t.cron_expr;
    }
    if (t.schedule_type === "interval" && t.interval_minutes) {
      return "每 " + t.interval_minutes + " 分钟";
    }
    return "手动";
  }

  function badgeCls(status) {
    return { success: "success", failed: "failed", running: "running" }[status] || "never";
  }

  function taskExtra(t) {
    try {
      return JSON.parse(t && t.extra_options || "{}") || {};
    } catch (e) { return {}; }
  }

  // ------------------------- 表单读取 / 预设 -------------------------
  function providerMeta(value) {
    const v = String(value || "").toLowerCase();
    for (const p of PROVIDERS) if (p.value === v) return p;
    return PROVIDERS[0] || {};
  }

  function applyProviderPreset(overwrite) {
    const p = providerMeta($("os_provider").value);
    const hint = $("os_provider_hint");
    if (hint) {
      hint.textContent = p.note || "—";
    }
    const ep = $("os_endpoint");
    if (ep) ep.placeholder = p.endpoint_hint || "192.168.1.10:9000";
    const port = $("os_port");
    if (port) port.placeholder = p.default_port || 9000;
    const region = $("os_region");
    if (region) region.placeholder = p.default_region || "us-east-1";
    if (p.region_required && hint) {
      hint.textContent = (hint.textContent || "") + "　Region 必填，否则签名不匹配。";
    }
    if (overwrite) {
      if (port && !port.value) port.value = p.default_port || "";
      if (region && !region.value) region.value = p.default_region || "";
      const addr = $("os_addressing");
      if (addr && !addr.value && p.addressing) addr.value = p.addressing;
      const secure = $("os_secure");
      if (secure && p.value && p.secure_default) secure.checked = true;
    }
  }

  function cfgFromForm() {
    const provider = ($("os_provider").value || "minio").toLowerCase();
    return {
      provider: provider,
      endpoint: ($("os_endpoint").value || "").trim(),
      port: parseInt($("os_port").value, 10) || null,
      region: ($("os_region").value || "").trim(),
      secure: !!$("os_secure").checked,
      verify_ssl: !!$("os_verify_ssl").checked,
      addressing: ($("os_addressing").value || "").trim(),
      access_key: ($("os_ak").value || "").trim(),
      secret_key: ($("os_sk").value || ""),
    };
  }

  function validateCfg(needBucket) {
    const cfg = cfgFromForm();
    if (!cfg.endpoint) { toast("请填写端点 Endpoint", "danger"); return null; }
    if (needBucket && !cfg.bucket) { toast("请填写桶 Bucket", "danger"); return null; }
    return cfg;
  }

  // ------------------------- 配置卡动作 -------------------------
  async function testConnection() {
    const cfg = cfgFromForm();
    if (!cfg.endpoint) { toast("请先填写端点 Endpoint", "danger"); return; }
    const btn = $("osTestBtn");
    setBusy(btn, true);
    $("osConnStatus").textContent = "正在连接 " + cfg.endpoint + " …";
    try {
      const res = await api("POST", V1 + "/test-connection", cfg);
      $("osConnStatus").textContent = res.message || "连接正常";
      toast(res.message || "连接正常", "success");
      showProbe('<span class="badge bg-success me-1">连通</span> ' + esc(res.message || "连接正常"));
    } catch (e) {
      $("osConnStatus").textContent = "连接失败：" + errMsg(e);
      toast("连接失败：" + errMsg(e), "danger", 6000);
      showProbe('<span class="badge bg-danger me-1">失败</span> ' + esc(errMsg(e)));
    } finally {
      setBusy(btn, false);
    }
  }

  async function listBuckets() {
    const cfg = cfgFromForm();
    if (!cfg.endpoint) { toast("请先填写端点 Endpoint", "danger"); return; }
    const btn = $("osListBucketsBtn");
    setBusy(btn, true);
    try {
      const res = await api("POST", V1 + "/buckets", cfg);
      const items = res.items || [];
      if (!items.length) {
        showProbe("账号可见桶：0 个（密钥有效但没有任何桶）");
        toast("该账号可见 0 个桶", "danger");
        return;
      }
      const names = items.map((b) => (typeof b === "string" ? b : (b.name || ""))).filter(Boolean);
      if (names.length === 1) {
        $("os_bucket").value = names[0];
        toast("已选用桶 " + names[0], "success");
        showProbe("账号可见桶：" + names.map(esc).join("、"));
        return;
      }
      // 多桶：渲染可点击列表，点选填入桶名
      const html = '<div class="small text-muted mb-1">账号可见 ' + names.length +
        ' 个桶，点击选用：</div><div class="d-flex flex-wrap gap-1">' +
        names.map((n) => '<button type="button" class="btn btn-sm btn-outline-secondary py-0" data-os-bucket="' +
          esc(n) + '">' + esc(n) + "</button>").join("") + "</div>";
      showProbe(html);
      toast("可见 " + names.length + " 个桶，请在下方点选", "info");
    } catch (e) {
      toast("列举桶失败：" + errMsg(e), "danger", 6000);
      showProbe('<span class="badge bg-danger me-1">失败</span> ' + esc(errMsg(e)));
    } finally {
      setBusy(btn, false);
    }
  }

  async function previewScan() {
    const cfg = cfgFromForm();
    cfg.bucket = ($("os_bucket").value || "").trim();
    cfg.prefix = ($("os_prefix").value || "").trim();
    cfg.include_versions = !!$("os_versions").checked;
    cfg.limit = 0;
    if (!cfg.endpoint) { toast("请先填写端点 Endpoint", "danger"); return; }
    if (!cfg.bucket) { toast("请先填写桶 Bucket", "danger"); return; }
    const btn = $("osPreviewBtn");
    setBusy(btn, true);
    $("osProbeBox").classList.remove("d-none");
    $("osProbeResult").innerHTML = "预扫描中（对象多时可能较慢）…";
    try {
      const res = await api("POST", V1 + "/preview", cfg);
      const sample = (res.sample_keys || []).map((k) =>
        '<code class="small d-block text-truncate" style="max-width:420px">' + esc(k) + "</code>").join("");
      $("osProbeResult").innerHTML =
        '<div class="d-flex flex-wrap gap-2 align-items-center">' +
        '<span class="badge bg-primary">对象 ' + res.object_count + " 个</span>" +
        '<span class="badge bg-secondary">' + humanSize(res.total_bytes) + "</span>" +
        '<span class="badge bg-light text-dark border">最大单对象 ' + humanSize(res.max_object_bytes) + "</span>" +
        '<span class="text-muted">桶 ' + esc(res.bucket) +
        (res.prefix ? '，前缀 ' + esc(res.prefix) : "") + "</span></div>" +
        (res.object_count === 0
          ? '<div class="text-danger mt-1">桶内没有可备份对象（请确认前缀/桶名）</div>'
          : (sample ? '<div class="mt-1 small text-muted">样例：</div>' + sample : "")) +
        (res.note ? '<div class="text-warning mt-1">' + esc(res.note) + "</div>" : "");
    } catch (e) {
      $("osProbeResult").innerHTML =
        '<span class="badge bg-danger me-1">失败</span> ' + esc(errMsg(e));
      toast("预扫描失败：" + errMsg(e), "danger", 6000);
    } finally {
      setBusy(btn, false);
    }
  }

  function showProbe(html) {
    const box = $("osProbeBox");
    if (!box) return;
    box.classList.remove("d-none");
    $("osProbeResult").innerHTML = html;
  }

  // ------------------------- 任务表单 -------------------------
  function resetForm() {
    EDIT_ID = null;
    EDIT_EXTRA = {};
    const ids = ["os_endpoint", "os_port", "os_region", "os_ak", "os_sk", "os_bucket",
      "os_prefix", "os_task_name", "os_biz_system", "os_schedule_cron",
      "os_restore_bucket_def", "os_restore_prefix_def", "osConnStatus"];
    ids.forEach((id) => { if ($(id)) $(id).value = ""; });
    ["os_versions", "os_secure", "os_verify_ssl", "os_allow_partial"].forEach((id) => {
      if ($(id)) $(id).checked = false;
    });
    $("os_workers").value = 8;
    $("os_retention").value = 30;
    $("os_backup_type").value = "full";
    $("os_schedule").value = "none";
    $("os_schedule_time").value = "02:00";
    $("os_enabled").checked = true;
    $("osCfgMode").textContent = "新建任务";
    $("osCfgMode").className = "badge bg-secondary";
    $("osCancelEditBtn").classList.add("d-none");
    $("os_sk_hint").textContent = "编辑任务时留空表示沿用原密钥";
    syncScheduleBoxes();
    if (PROVIDERS.length) applyProviderPreset(false);
  }

  function syncScheduleBoxes() {
    const mode = $("os_schedule").value;
    $("os_schedule_time_box").classList.toggle("d-none", mode !== "daily" && mode !== "weekly");
    $("os_schedule_cron_box").classList.toggle("d-none", mode !== "cron");
  }

  function cronFromForm() {
    const mode = $("os_schedule").value;
    if (mode === "none") return { schedule_type: null, cron_expr: null };
    if (mode === "cron") {
      const expr = ($("os_schedule_cron").value || "").trim();
      if (!expr) return { error: "请填写 cron 表达式" };
      if (expr.split(/\s+/).length !== 5) return { error: "cron 表达式必须为 5 段（分 时 日 月 周）" };
      return { schedule_type: "cron", cron_expr: expr };
    }
    // daily / weekly
    const parts = ($("os_schedule_time").value || "02:00").split(":");
    const hh = String(parseInt(parts[0], 10) || 0);
    const mm = String(parseInt(parts[1], 10) || 0);
    const dow = mode === "weekly" ? "1" : "*";
    return { schedule_type: "cron", cron_expr: mm + " " + hh + " * * " + dow };
  }

  function taskPayload() {
    const cfg = cfgFromForm();
    const bucket = ($("os_bucket").value || "").trim();
    const name = ($("os_task_name").value || "").trim();
    const biz = ($("os_biz_system").value || "").trim();
    if (!cfg.endpoint) { toast("请填写端点 Endpoint", "danger"); return null; }
    if (!cfg.access_key) { toast("请填写 AccessKey", "danger"); return null; }
    if (!EDIT_ID && !cfg.secret_key) { toast("新建任务必须填写 SecretKey", "danger"); return null; }
    if (!bucket) { toast("请填写桶 Bucket", "danger"); return null; }
    if (!name) { toast("请填写任务名称", "danger"); return null; }
    if (!biz) { toast("请填写业务系统", "danger"); return null; }
    const sched = cronFromForm();
    if (sched.error) { toast(sched.error, "danger"); return null; }

    const osExtra = {
      os_provider: cfg.provider,
      provider: cfg.provider,                       // 兼容老字段（task_cfg 两者都认）
      os_endpoint: cfg.endpoint,
      os_region: cfg.region,
      os_secure: cfg.secure,
      os_verify_ssl: cfg.verify_ssl,
      os_addressing: cfg.addressing,
      os_prefix: ($("os_prefix").value || "").trim(),
      os_include_versions: !!$("os_versions").checked,
      os_workers: parseInt($("os_workers").value, 10) || 8,
      os_allow_partial: !!$("os_allow_partial").checked,
      os_restore_bucket: ($("os_restore_bucket_def").value || "").trim(),
      os_restore_prefix: ($("os_restore_prefix_def").value || "").trim(),
    };
    // 编辑时保留任务原有的其它 extra_options（ssh_cred / env_vars / tool_path …），
    // 以 os_* 新值覆盖同名字段
    const extra = Object.assign({}, EDIT_EXTRA, osExtra);

    const payload = {
      name: name,
      biz_system: biz,
      db_type: DB_TYPE,
      host: cfg.endpoint,
      port: cfg.port,
      username: cfg.access_key,
      db_name: bucket,
      backup_mode: "logical",
      backup_type: $("os_backup_type").value || "full",
      schedule_type: sched.schedule_type,
      cron_expr: sched.cron_expr,
      enabled: $("os_enabled").checked ? 1 : 0,
      retention_days: parseInt($("os_retention").value, 10) || null,
      extra_options: JSON.stringify(extra),
    };
    if (cfg.secret_key) payload.password = cfg.secret_key;  // 编辑时留空 = 沿用原密钥
    return payload;
  }

  async function saveTask() {
    const payload = taskPayload();
    if (!payload) return;
    const btn = $("osSaveTaskBtn");
    setBusy(btn, true);
    try {
      if (EDIT_ID) {
        await api("PUT", "/api/tasks/" + EDIT_ID, payload);
        toast("任务已更新", "success");
      } else {
        await api("POST", "/api/tasks", payload);
        toast("备份任务已创建", "success");
      }
      resetForm();
      await refreshAll();
    } catch (e) {
      toast("保存失败：" + errMsg(e), "danger", 6000);
    } finally {
      setBusy(btn, false);
    }
  }

  function editTask(id) {
    const t = TASK_BY_ID[id];
    if (!t) return;
    resetForm();
    EDIT_ID = t.id;
    EDIT_EXTRA = taskExtra(t);
    $("os_provider").value = (EDIT_EXTRA.os_provider || EDIT_EXTRA.provider || "minio").toLowerCase();
    applyProviderPreset(false);
    $("os_endpoint").value = t.host || "";
    $("os_port").value = t.port || "";
    $("os_region").value = EDIT_EXTRA.os_region || "";
    $("os_addressing").value = EDIT_EXTRA.os_addressing || "";
    $("os_ak").value = t.username || "";
    $("os_bucket").value = t.db_name || "";
    $("os_prefix").value = EDIT_EXTRA.os_prefix || "";
    $("os_versions").checked = !!EDIT_EXTRA.os_include_versions;
    $("os_secure").checked = !!EDIT_EXTRA.os_secure;
    $("os_verify_ssl").checked = !!EDIT_EXTRA.os_verify_ssl;
    $("os_workers").value = EDIT_EXTRA.os_workers || 8;
    $("os_allow_partial").checked = !!EDIT_EXTRA.os_allow_partial;
    $("os_restore_bucket_def").value = EDIT_EXTRA.os_restore_bucket || "";
    $("os_restore_prefix_def").value = EDIT_EXTRA.os_restore_prefix || "";
    $("os_task_name").value = t.name || "";
    $("os_biz_system").value = t.biz_system || "";
    $("os_backup_type").value = t.backup_type || "full";
    $("os_retention").value = t.retention_days != null ? t.retention_days : 30;
    $("os_enabled").checked = !!t.enabled;

    // 调度回填：cron 表达式反解为 每天/每周一/自定义
    let mode = "none";
    const expr = (t.cron_expr || "").trim();
    if (t.schedule_type === "interval" && t.interval_minutes) {
      mode = "cron";
      $("os_schedule_cron").value = "每 " + t.interval_minutes + " 分钟（interval 调度，请改回 cron）";
    } else if (t.schedule_type === "cron" && expr) {
      const seg = expr.split(/\s+/);
      if (seg.length === 5 && seg[2] === "*" && seg[3] === "*" && seg[4] === "*") {
        mode = "daily";
        $("os_schedule_time").value = String(seg[1]).padStart(2, "0") + ":" + String(seg[0]).padStart(2, "0");
      } else if (seg.length === 5 && seg[2] === "*" && seg[3] === "*" && seg[4] === "1") {
        mode = "weekly";
        $("os_schedule_time").value = String(seg[1]).padStart(2, "0") + ":" + String(seg[0]).padStart(2, "0");
      } else {
        mode = "cron";
        $("os_schedule_cron").value = expr;
      }
    }
    $("os_schedule").value = mode;
    syncScheduleBoxes();

    $("osCfgMode").textContent = "编辑任务 #" + t.id;
    $("osCfgMode").className = "badge bg-warning text-dark";
    $("osCancelEditBtn").classList.remove("d-none");
    $("os_sk_hint").textContent = "留空表示沿用原 SecretKey";
    const card = $("osCfgCard");
    if (card && card.scrollIntoView) card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function deleteTask(id) {
    const t = TASK_BY_ID[id];
    const label = t ? ("「" + (t.name || ("#" + id)) + "」") : ("#" + id);
    if (!window.confirm("确认删除备份任务 " + label + " 及其全部备份记录？此操作不可撤销。")) return;
    try {
      await api("DELETE", "/api/tasks/" + id);
      toast("任务已删除", "success");
      if (EDIT_ID === id) resetForm();
      await refreshAll();
    } catch (e) {
      toast("删除失败：" + errMsg(e), "danger", 6000);
    }
  }

  async function runBackup(id) {
    if (RUNNING_TASKS[id]) { toast("该任务正在备份中，请稍候", "info"); return; }
    const t = TASK_BY_ID[id] || {};
    RUNNING_TASKS[id] = true;
    const btn = document.querySelector('[data-os-run="' + id + '"]');
    setBusy(btn, true);
    toast("已触发备份（对象多时可能需要较长时间）…", "info");
    try {
      const rec = await api("POST", "/api/tasks/" + id + "/run",
        { backup_type: t.backup_type || "full" });
      if (rec && rec.accepted) {
        toast("已提交后台执行，稍后可在备份记录中查看", "info", 5000);
      } else {
        const ok = rec && rec.status === "success";
        toast((ok ? "备份成功：" : "备份失败：") + (rec.message || rec.error_msg || ""),
          ok ? "success" : "danger", 8000);
      }
      await refreshAll();
    } catch (e) {
      toast("备份执行失败：" + errMsg(e), "danger", 8000);
      await loadRecords();
      await loadTasks();
    } finally {
      RUNNING_TASKS[id] = false;
      setBusy(btn, false);
    }
  }

  // ------------------------- 任务表 / KPI -------------------------
  async function loadTasks() {
    let rows = [];
    try {
      const res = await api("GET", "/api/tasks?db_type=" + DB_TYPE);
      rows = Array.isArray(res) ? res : (res.items || []);
    } catch (e) {
      toast("加载任务失败：" + errMsg(e), "danger");
      rows = [];
    }
    TASKS = rows;
    TASK_BY_ID = {};
    TASKS.forEach((t) => { TASK_BY_ID[t.id] = t; });
    renderTaskTable();
    renderTaskKpi();
  }

  function renderTaskTable() {
    const tb = $("osTaskTable");
    if (!tb) return;
    if (!TASKS.length) {
      tb.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">' +
        "还没有对象存储任务，先在上方完成连接测试，再「保存为备份任务」。</td></tr>";
      $("osTaskSummary").textContent = "共 0 个任务";
      return;
    }
    const pgT = Pager.slice("osTasks", TASKS);
    tb.innerHTML = pgT.list.map((t) => {
      const ex = taskExtra(t);
      const prefix = ex.os_prefix || "";
      const bucketCell = esc(t.db_name || "-") +
        (prefix ? ' <span class="badge bg-light text-dark border">前缀 ' + esc(prefix) + "</span>" : "");
      const lastCell = t.last_run_at
        ? '<div class="small">' + esc(fmtTime(t.last_run_at)) + "</div>" + statusBadge(t.last_status)
        : '<span class="text-muted small">从未执行</span>';
      const running = !!RUNNING_TASKS[t.id];
      return '<tr>' +
        '<td><div class="fw-bold">' + esc(t.name || ("#" + t.id)) + "</div>" +
        '<div class="small text-muted">' + esc(t.biz_system || "-") +
        ' <span class="badge bg-light text-dark border ms-1">#' + t.id + "</span>" +
        (t.enabled ? "" : ' <span class="badge bg-secondary ms-1">已停用</span>') + "</div></td>" +
        "<td>" + bucketCell + "</td>" +
        '<td class="small">' + esc((t.host || "-") + (t.port ? ":" + t.port : "")) + "</td>" +
        '<td><span class="badge bg-light text-dark border">' + esc(t.backup_type || "full") + "</span>" +
        '<div class="small text-muted mt-1">' + esc(scheduleText(t)) + "</div>" +
        '<div class="small text-muted">保留 ' + (t.retention_days != null ? t.retention_days : "-") + " 天</div></td>" +
        "<td>" + lastCell + "</td>" +
        '<td class="text-end">' +
        '<button class="btn btn-sm btn-outline-primary me-1" data-os-run="' + t.id + '"' +
        (running ? " disabled" : "") + ' title="立即执行一次备份">' +
        (running ? '<span class="spinner-border spinner-border-sm"></span>' : '<i class="bi bi-play-fill"></i> 备份') + "</button>" +
        '<button class="btn btn-sm btn-outline-secondary me-1" data-os-objs="' + t.id + '" title="最近一次成功备份的对象清单">' +
        '<i class="bi bi-list-ul"></i></button>' +
        '<button class="btn btn-sm btn-outline-secondary me-1" data-os-edit="' + t.id + '" title="编辑"><i class="bi bi-pencil"></i></button>' +
        '<button class="btn btn-sm btn-outline-danger" data-os-del="' + t.id + '" title="删除"><i class="bi bi-trash"></i></button>' +
        "</td></tr>";
    }).join("");
    Pager.attach("osTasks", tb, pgT.page, pgT.pages, pgT.total, renderTaskTable);
    $("osTaskSummary").textContent = "共 " + TASKS.length + " 个任务，启用 " +
      TASKS.filter((t) => t.enabled).length + " 个";
  }

  function renderTaskKpi() {
    const buckets = new Set(TASKS.map((t) => t.db_name).filter(Boolean));
    $("osKpiBuckets").textContent = buckets.size;
    $("osKpiTasks").innerHTML = TASKS.length +
      ' <span class="small text-muted">（启用 ' + TASKS.filter((t) => t.enabled).length + "）</span>";
  }

  // ------------------------- 备份记录 -------------------------
  async function loadRecords() {
    const kw = ($("osRecordSearch").value || "").trim();
    let payload = [];
    let total = 0;
    let hasMore = false;
    try {
      const res = await api("GET", "/api/records?db_type=" + DB_TYPE +
        "&page=1&size=" + RECORD_PAGE + (kw ? "&keyword=" + encodeURIComponent(kw) : ""));
      payload = (res && res.items) || [];
      total = res && res.total != null ? res.total : payload.length;
      hasMore = !!(res && res.has_more);
    } catch (e) {
      toast("加载备份记录失败：" + errMsg(e), "danger");
    }
    RECORDS = payload;
    RECORD_TOTAL = total;
    RECORD_HAS_MORE = hasMore;
    renderRecordTable();
    renderRecordKpi();
  }

  function latestSuccessByTask() {
    const map = {};
    for (const r of RECORDS) {
      if (r.status !== "success") continue;
      if (!map[r.task_id]) map[r.task_id] = r;   // 记录按 id 倒序返回，首个即最新
    }
    return map;
  }

  function renderRecordTable() {
    const tb = $("osRecordTable");
    if (!tb) return;
    if (!RECORDS.length) {
      tb.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-4">' +
        "暂无对象存储备份记录（在任务行点「备份」即可生成）。</td></tr>";
      return;
    }
    const latest = latestSuccessByTask();
    tb.innerHTML = RECORDS.map((r) => {
      const task = TASK_BY_ID[r.task_id] || {};
      const ex = taskExtra(task);
      const prefix = ex.os_prefix || "";
      const bucketCell = esc(task.db_name || "-") +
        (prefix ? ' <span class="small text-muted">' + esc(prefix) + "</span>" : "");
      const dur = r.duration_sec ? '<div class="small text-muted">' + fmtDur(r.duration_sec) + "</div>" : "";
      const canOpen = r.status === "success" || latest[r.task_id];
      const objsBtn = '<button class="btn btn-sm btn-outline-secondary me-1" data-rc-objs="' + r.id +
        '"' + (canOpen ? "" : " disabled") + ' title="归档内对象清单"><i class="bi bi-list-ul"></i> 对象</button>';
      const restoreBtn = '<button class="btn btn-sm btn-outline-primary me-1" data-rc-restore="' + r.id +
        '"' + (canOpen ? "" : " disabled") + ' title="整包恢复"><i class="bi bi-arrow-counterclockwise"></i> 恢复</button>';
      const dl = r.backup_path
        ? '<a class="btn btn-sm btn-outline-secondary" href="/api/records/' + r.id + '/download" title="下载产物"><i class="bi bi-download"></i></a>'
        : "";
      return "<tr>" +
        '<td class="small">' + esc(fmtTime(r.started_at)) + dur + "</td>" +
        "<td>" + esc(r.task_name || "-") + "</td>" +
        "<td>" + bucketCell + "</td>" +
        "<td>" + esc(r.backup_type_display || r.backup_type || "-") + "</td>" +
        '<td class="small">' + esc(r.size_human || humanSize(r.size_bytes)) +
        (r.is_simulated ? ' <span class="badge bg-warning text-dark">仿真</span>' : "") + "</td>" +
        "<td>" + statusBadge(r.status) +
        (r.message ? '<div class="small text-muted text-truncate" style="max-width:220px" title="' +
          esc(r.message) + '">' + esc(r.message) + "</div>" : "") + "</td>" +
        '<td class="text-end text-nowrap">' + objsBtn + restoreBtn + dl + "</td>" +
        "</tr>";
    }).join("") +
      (RECORD_HAS_MORE
        ? '<tr><td colspan="7" class="text-center small text-muted py-2">已显示 ' + RECORDS.length +
          " 条 / 共 " + RECORD_TOTAL + " 条，可用上方过滤缩小范围</td></tr>"
        : "");
  }

  function fmtDur(sec) {
    sec = Number(sec) || 0;
    if (sec < 60) return sec.toFixed(1) + "s";
    const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    if (m < 60) return m + "m" + (s ? s + "s" : "");
    return Math.floor(m / 60) + "h" + (m % 60) + "m";
  }

  function renderRecordKpi() {
    const last = RECORDS[0];
    if (!last) {
      $("osKpiLastBackup").textContent = "-";
      $("osKpiLastStatus").innerHTML = "";
      $("osKpiLastSize").textContent = "-";
      $("osKpiLastBucket").textContent = "";
      return;
    }
    const task = TASK_BY_ID[last.task_id] || {};
    $("osKpiLastBackup").textContent = fmtTime(last.started_at);
    $("osKpiLastStatus").innerHTML = statusBadge(last.status);
    $("osKpiLastSize").textContent = last.size_human || humanSize(last.size_bytes);
    $("osKpiLastBucket").textContent = task.db_name || last.task_name || "";
  }

  // ------------------------- 对象清单弹窗 -------------------------
  async function openObjects(recordId) {
    OBJ.recordId = recordId;
    OBJ.page = 1;
    OBJ.selected = new Set();
    OBJ.items = [];
    OBJ.manifest = null;
    const m = modal("osObjectsModal");
    if (m) m.show();
    await loadObjects(1);
  }

  async function loadObjects(page) {
    const tb = $("osObjTable");
    tb.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">加载中…</td></tr>';
    try {
      const res = await api("GET", V1 + "/records/" + OBJ.recordId + "/objects?page=" + page +
        "&size=" + OBJ_PAGE);
      OBJ.items = res.items || [];
      OBJ.total = res.total || 0;
      OBJ.page = res.page || page;
      OBJ.hasMore = !!res.has_more;
      OBJ.manifest = res.manifest || null;
      renderObjects();
    } catch (e) {
      tb.innerHTML = '<tr><td colspan="6" class="text-center text-danger py-4">' +
        esc(errMsg(e)) + "</td></tr>";
      toast("读取对象清单失败：" + errMsg(e), "danger", 6000);
    }
  }

  function renderObjects() {
    const man = OBJ.manifest || {};
    const f = (v) => (v === null || v === undefined || v === "" ? "-" : v);
    $("osObjSubtitle").textContent = "记录 #" + OBJ.recordId +
      (man.bucket ? "　桶 " + man.bucket : "");
    $("osObjSummary").innerHTML =
      '<div class="col-6 col-lg-3"><div class="small text-muted">备份类型</div><div class="fw-semibold">' +
      esc(man.backup_type || "-") + (man.parent ? ' <span class="badge bg-light text-dark border">增量链</span>' : "") + "</div></div>" +
      '<div class="col-6 col-lg-3"><div class="small text-muted">对象数</div><div class="fw-semibold">' +
      f(man.object_count) + (man.deleted_count ? ' <span class="small text-muted">（删除 ' + man.deleted_count + "）</span>" : "") + "</div></div>" +
      '<div class="col-6 col-lg-3"><div class="small text-muted">原容量</div><div class="fw-semibold">' +
      humanSize(man.object_bytes) + "</div></div>" +
      '<div class="col-6 col-lg-3"><div class="small text-muted">生成时间</div><div class="fw-semibold">' +
      esc(fmtTime(man.created_at)) + "</div></div>";

    const kw = ($("osObjSearch").value || "").trim().toLowerCase();
    const rows = OBJ.items.filter((o) => !kw || String(o.key || "").toLowerCase().indexOf(kw) >= 0);
    if (!rows.length) {
      $("osObjTable").innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">' +
        (OBJ.items.length ? "当前页没有匹配的对象" : "该备份没有对象索引") + "</td></tr>";
    } else {
      $("osObjTable").innerHTML = rows.map((o) => {
        const key = String(o.key || "");
        const checked = OBJ.selected.has(key) ? " checked" : "";
        const sha = o.sha256
          ? '<code class="small" title="' + esc(o.sha256) + '">' + esc(String(o.sha256).slice(0, 10)) + "…</code>"
          : '<span class="text-muted">-</span>';
        const ver = o.version_id
          ? '<span title="' + esc(o.version_id) + '">' + esc(String(o.version_id).slice(0, 12)) + "</span>" +
            (o.is_latest === false ? ' <span class="badge bg-light text-dark border">历史</span>' : "")
          : "-";
        return "<tr>" +
          '<td><input class="form-check-input" type="checkbox" data-os-objkey="' + esc(key) + '"' + checked + "></td>" +
          '<td class="text-break"><code class="small">' + esc(key) + "</code></td>" +
          '<td class="small">' + humanSize(o.size) + "</td>" +
          '<td class="small text-break">' + ver + "</td>" +
          '<td class="small">' + esc(o.last_modified ? fmtTime(String(o.last_modified).replace(" ", "T")) : "-") + "</td>" +
          "<td>" + sha + "</td></tr>";
      }).join("");
    }
    const pages = Math.max(1, Math.ceil(OBJ.total / OBJ_PAGE));
    $("osObjPageInfo").textContent = "第 " + OBJ.page + " / " + pages + " 页，共 " + OBJ.total + " 个";
    $("osObjPrev").disabled = OBJ.page <= 1;
    $("osObjNext").disabled = !OBJ.hasMore;
    const selInfo = $("osObjSelectedInfo");
    if (selInfo) selInfo.textContent = "已选 " + OBJ.selected.size + " 个对象";
    $("osObjFooterHint").textContent =
      "勾选按对象 Key 生效：同一 Key 的全部历史版本会一起恢复，包内按「旧 → 新」串行回放。";
    $("osObjRestoreSelected").disabled = OBJ.selected.size === 0;
    const selAll = $("osObjSelectAll");
    if (selAll) selAll.checked = rows.length > 0 && rows.every((o) => OBJ.selected.has(String(o.key || "")));
  }

  // ------------------------- 恢复弹窗 -------------------------
  function openRestore(recordId, keys) {
    const rec = RECORDS.find((r) => r.id === recordId) || {};
    const task = TASK_BY_ID[rec.task_id] || {};
    const ex = taskExtra(task);
    RESTORE = {
      recordId: recordId,
      keys: keys || null,          // null = 整包；[] 也视为整包
      bucket: task.db_name || "",
    };
    $("os_restore_bucket").value = ex.os_restore_bucket || "";
    $("os_restore_prefix").value = ex.os_restore_prefix || "";
    $("os_restore_workers").value = ex.os_workers || 8;
    $("os_restore_overwrite").value = ex.os_restore_overwrite || "if_newer";
    $("os_restore_create_bucket").checked = ex.os_restore_create_bucket !== false;
    $("os_restore_apply_deleted").checked = ex.os_restore_apply_deleted !== false;
    const granular = !!(keys && keys.length);
    $("osRestoreScope").innerHTML =
      "恢复来源：记录 <b>#" + recordId + "</b>（" + esc(rec.task_name || "-") +
      "，" + esc(rec.backup_type_display || rec.backup_type || "-") +
      "，" + esc(rec.size_human || "-") + "）" +
      "<br>恢复范围：" + (granular
        ? "<b>对象级恢复（" + keys.length + " 个 Key）</b>"
        : "<b>整包恢复</b>（按 full → 增量链逐层回放" +
          ($("os_restore_apply_deleted").checked ? "，并回放删除事件" : "") + "）");
    $("osRestoreResultBox").classList.add("d-none");
    const m = modal("osRestoreModal");
    if (m) m.show();
  }

  async function runRestore() {
    if (!RESTORE) return;
    const btn = $("osRestoreRunBtn");
    setBusy(btn, true);
    const body = {
      record_id: RESTORE.recordId,
      keys: (RESTORE.keys && RESTORE.keys.length) ? RESTORE.keys : null,
      target_bucket: ($("os_restore_bucket").value || "").trim() || null,
      target_prefix: ($("os_restore_prefix").value || "").trim() || null,
      overwrite: $("os_restore_overwrite").value || "if_newer",
      workers: parseInt($("os_restore_workers").value, 10) || 8,
      create_bucket: !!$("os_restore_create_bucket").checked,
      apply_deleted: !!$("os_restore_apply_deleted").checked,
    };
    const box = $("osRestoreResultBox");
    box.classList.remove("d-none");
    $("osRestoreResult").className = "alert alert-info mb-0 small";
    $("osRestoreResult").textContent = "恢复执行中（对象多时可能较慢，请勿关闭页面）…";
    try {
      const res = await api("POST", V1 + "/restores", body);
      const ok = !!res.success;
      $("osRestoreResult").className = "alert mb-0 small " + (ok ? "alert-success" : "alert-danger");
      $("osRestoreResult").innerHTML =
        (ok ? '<b>恢复完成</b>' : '<b>恢复失败</b>') + "　" + esc(res.message || "") +
        (res.stderr ? '<div class="small text-muted mt-1">' + esc(String(res.stderr).slice(0, 300)) + "</div>" : "");
      toast(ok ? "恢复完成" : "恢复失败", ok ? "success" : "danger", 6000);
      if (ok) {
        await loadRecords();
        await loadTasks();
      }
    } catch (e) {
      $("osRestoreResult").className = "alert alert-danger mb-0 small";
      $("osRestoreResult").textContent = "恢复失败：" + errMsg(e);
      toast("恢复失败：" + errMsg(e), "danger", 6000);
    } finally {
      setBusy(btn, false);
    }
  }

  // ------------------------- 事件绑定 -------------------------
  function bindEvents() {
    $("osRefreshBtn").addEventListener("click", () => refreshAll());
    $("osNewTaskBtn").addEventListener("click", () => { resetForm(); $("os_endpoint").focus(); });
    $("osCancelEditBtn").addEventListener("click", () => resetForm());

    $("os_provider").addEventListener("change", () => applyProviderPreset(true));
    $("osTestBtn").addEventListener("click", () => testConnection());
    $("osListBucketsBtn").addEventListener("click", () => listBuckets());
    $("osPreviewBtn").addEventListener("click", () => previewScan());
    $("osSaveTaskBtn").addEventListener("click", () => saveTask());
    $("os_schedule").addEventListener("change", syncScheduleBoxes);

    // 点选列举出来的桶
    $("osProbeBox").addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-os-bucket]");
      if (!btn) return;
      $("os_bucket").value = btn.getAttribute("data-os-bucket");
      toast("已选用桶 " + btn.getAttribute("data-os-bucket"), "success");
    });

    // 任务行
    $("osTaskTable").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button");
      if (!btn) return;
      const run = btn.getAttribute("data-os-run");
      const objs = btn.getAttribute("data-os-objs");
      const edit = btn.getAttribute("data-os-edit");
      const del = btn.getAttribute("data-os-del");
      if (run) runBackup(parseInt(run, 10));
      else if (edit) editTask(parseInt(edit, 10));
      else if (del) deleteTask(parseInt(del, 10));
      else if (objs) {
        const rec = latestSuccessByTask()[parseInt(objs, 10)];
        if (rec) openObjects(rec.id);
        else toast("该任务还没有成功备份，无法查看对象清单", "info");
      }
    });

    // 记录行
    $("osRecordTable").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button");
      if (!btn) return;
      const objs = btn.getAttribute("data-rc-objs");
      const rst = btn.getAttribute("data-rc-restore");
      if (objs) openObjects(parseInt(objs, 10));
      else if (rst) openRestore(parseInt(rst, 10), null);
    });

    $("osRecordReloadBtn").addEventListener("click", () => loadRecords());
    $("osRecordSearch").addEventListener("input", () => {
      clearTimeout(RECORD_SEARCH_TIMER);
      RECORD_SEARCH_TIMER = setTimeout(() => loadRecords(), 300);
    });

    // 对象清单
    $("osObjPrev").addEventListener("click", () => { if (OBJ.page > 1) loadObjects(OBJ.page - 1); });
    $("osObjNext").addEventListener("click", () => { if (OBJ.hasMore) loadObjects(OBJ.page + 1); });
    $("osObjSearch").addEventListener("input", () => renderObjects());
    $("osObjSelectAll").addEventListener("change", (ev) => {
      const checked = ev.target.checked;
      const kw = ($("osObjSearch").value || "").trim().toLowerCase();
      OBJ.items.forEach((o) => {
        const key = String(o.key || "");
        if (!kw || key.toLowerCase().indexOf(kw) >= 0) {
          if (checked) OBJ.selected.add(key);
          else OBJ.selected.delete(key);
        }
      });
      renderObjects();
    });
    $("osObjTable").addEventListener("change", (ev) => {
      const cb = ev.target.closest("[data-os-objkey]");
      if (!cb) return;
      const key = cb.getAttribute("data-os-objkey");
      if (cb.checked) OBJ.selected.add(key);
      else OBJ.selected.delete(key);
      renderObjects();
    });
    $("osObjRestoreSelected").addEventListener("click", () => {
      if (!OBJ.selected.size) return;
      openRestore(OBJ.recordId, Array.from(OBJ.selected));
    });
    $("osObjRestoreAll").addEventListener("click", () => openRestore(OBJ.recordId, null));

    // 恢复
    $("osRestoreRunBtn").addEventListener("click", () => runRestore());
  }

  async function refreshAll() {
    await Promise.all([loadTasks(), loadRecords()]);
  }

  // ------------------------- 启动 -------------------------
  async function init() {
    bindEvents();
    resetForm();
    try {
      const res = await api("GET", V1 + "/providers");
      PROVIDERS = res.items || [];
      const sel = $("os_provider");
      sel.innerHTML = PROVIDERS.map((p) =>
        '<option value="' + esc(p.value) + '">' + esc(p.label || p.value) + "</option>").join("");
      sel.value = res.default || (PROVIDERS[0] && PROVIDERS[0].value) || "minio";
      applyProviderPreset(true);
    } catch (e) {
      // 预设拉取失败不阻塞页面：手工填写端点/密钥仍可用
      $("os_provider").innerHTML = '<option value="minio">MinIO（自建）</option>' +
        '<option value="s3">AWS S3 / S3 兼容</option>';
    }
    await refreshAll();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
