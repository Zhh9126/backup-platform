/**
 * AI 智能助手消息渲染层（零依赖、离线可用）
 *
 * 职责：
 *  1. 把助手返回的 Markdown 安全渲染为 HTML：标题 / 列表 / 表格 / 代码块 / 引用 / 加粗 / 链接
 *  2. 把状态词（success / failed / running ...）渲染为彩色徽章，数字与体积右对齐
 *  3. 把工具调用轨迹渲染为可展开的"执行步骤卡"（中文名 + 参数 + 结果摘要 + 耗时 + 原始 JSON）
 *
 * 安全约束：所有文本先 HTML 转义，仅放行渲染器自己生成的标签。
 * 暴露：window.AgentRender
 */
"use strict";
(function () {
  /* ---------------- 状态映射 ---------------- */
  const STATUS_MAP = {
    success: "success", ok: "success", succeeded: "success", 成功: "success", 已完成: "success",
    failed: "failed", fail: "failed", error: "failed", 失败: "failed",
    running: "running", doing: "running", 运行中: "running", 执行中: "running",
    pending: "pending", queued: "pending", waiting: "pending", 待执行: "pending",
    never: "never", none: "never", 未执行: "never", 从未: "never",
    warning: "warn", warn: "warn", 警告: "warn",
    skipped: "skip", skip: "skip", 跳过: "skip",
    high: "high", medium: "medium", low: "low",
    高危: "high", 中危: "medium", 低危: "low",
  };
  const STATUS_TEXT = {
    success: "成功", failed: "失败", running: "执行中",
    pending: "待执行", never: "未执行", warn: "警告", skip: "已跳过",
  };
  /* 工具中文名 / 图标 / 结果摘要口径 */
  const TOOL_META = {
    run_backup_task: { label: "执行备份", icon: "bi-play-circle" },
    list_tasks: { label: "查询备份任务", icon: "bi-list-check" },
    list_recent_records: { label: "查询备份记录", icon: "bi-clock-history" },
    get_storage_usage: { label: "存储用量", icon: "bi-hdd" },
    list_alert_predictions: { label: "AI 风险预测", icon: "bi-graph-up-arrow" },
    get_inspection_report: { label: "巡检报告", icon: "bi-clipboard-check" },
    run_inspection: { label: "执行巡检", icon: "bi-shield-check" },
    autonomous_diagnose: { label: "7 个自治 Agent 诊断", icon: "bi-robot" },
    autonomous_plan: { label: "自治修复计划", icon: "bi-list-check" },
    autonomous_execute: { label: "自治修复执行", icon: "bi-wrench-adjustable" },
    autonomous_verify: { label: "修复回验", icon: "bi-check2-circle" },
    // 自然语言动作层（NL）工具名与 ReAct 工具名不同，未登记会显示裸英文
    list_records: { label: "查询备份记录", icon: "bi-clock-history" },
    list_restores: { label: "查询恢复记录", icon: "bi-arrow-counterclockwise" },
    task_detail: { label: "查看任务详情", icon: "bi-info-circle" },
    record_tables: { label: "查看备份对象清单", icon: "bi-list-ul" },
    job_monitor: { label: "查看任务监控概览", icon: "bi-activity" },
    report_summary: { label: "查看报表中心汇总", icon: "bi-bar-chart" },
    storage_usage: { label: "查看存储用量", icon: "bi-hdd" },
    list_hosts: { label: "列出纳管主机", icon: "bi-hdd-network" },
    alert_predictions: { label: "查看智能告警预测", icon: "bi-graph-up-arrow" },
    inspection_records: { label: "查看巡检记录", icon: "bi-clipboard-check" },
    restore_verify_reports: { label: "查看恢复校验报告", icon: "bi-patch-check" },
    data_compare_reports: { label: "查看数据对比报告", icon: "bi-diff" },
    migration_plans: { label: "查看迁移计划", icon: "bi-arrow-left-right" },
    sync_tasks: { label: "查看同步任务", icon: "bi-arrow-repeat" },
    clone_requests: { label: "查看克隆申请", icon: "bi-copy" },
    drills: { label: "查看演练记录", icon: "bi-lightning" },
    rt_points: { label: "查看实时恢复点", icon: "bi-clock" },
    locked_records: { label: "查看锁定产物", icon: "bi-lock" },
    pool_chains: { label: "查看池复制链", icon: "bi-diagram-3" },
    protection_gaps: { label: "查看保护缺口", icon: "bi-shield-exclamation" },
    license: { label: "查看授权与配额", icon: "bi-key" },
    verify_integrity: { label: "校验记录完整性", icon: "bi-fingerprint" },
    security_check: { label: "恢复点安全检测", icon: "bi-shield-check" },
    backup_precheck: { label: "备份预检", icon: "bi-clipboard2-pulse" },
    restore_precheck: { label: "恢复预检", icon: "bi-clipboard2-check" },
    cleanup_preview: { label: "备份清理预览", icon: "bi-trash3" },
    run_backup: { label: "立即执行备份", icon: "bi-play-circle" },
    quick_backup: { label: "一键备份", icon: "bi-collection-play" },
  };

  /* 调用参数 / 结果列的中文名，避免步骤卡只显示裸字段名 */
  const ARG_LABELS = {
    task_id: "任务 ID", record_id: "记录 ID", task_name: "任务关键字",
    keyword: "关键字", status: "状态", db_type: "库型", limit: "条数",
    days: "天数", enabled: "启用", scope: "范围", target_db: "目标库",
    policy_id: "策略 ID", plan_id: "计划 ID", sync_id: "同步 ID",
    compare_id: "对比 ID", drill_id: "演练 ID", backup_type: "备份类型",
    verify_code: "验证码", task_ids: "任务列表",
  };
  const FIELD_LABELS = {
    id: "ID", task_id: "任务 ID", record_id: "记录 ID", name: "名称",
    task_name: "任务名", db_type: "库型", db_name: "库名", host: "主机",
    host_ip: "主机 IP", status: "状态", last_status: "最近状态",
    backup_type: "备份类型", size_human: "大小", started_at: "开始时间",
    created_at: "创建时间", finished_at: "结束时间", message: "说明",
    path: "路径", source: "来源", source_label: "来源说明",
    path_exists: "备份目录存在", usage_path: "容量统计路径",
    degraded: "容量统计降级", hint: "提示", warning: "警告",
    total_bytes: "总容量", used_bytes: "已用", free_bytes: "可用",
    total_human: "总容量", used_human: "已用", free_human: "可用",
    total_gb: "总容量(GB)", used_gb: "已用(GB)", free_gb: "可用(GB)",
    used_percent: "使用率(%)", score: "评分", level: "级别",
    title: "发现", severity: "级别", item: "检查项",
  };
  const PREFERRED_COLS = [
    "id", "task_id", "record_id", "task_name", "name", "db_type", "db_name",
    "host", "host_ip", "status", "last_status", "backup_type", "size_human",
    "total_human", "used_human", "free_human", "used_percent",
    "score", "level", "severity", "title", "item",
    "created_at", "started_at", "finished_at", "message",
  ];
  const SKIP_FIELD = /^(password|password_hash|secret|secret_key|token|access_key|private_key|verify_code|cookie|authorization|raw|log_output|log_tail|script|custom_script|traceback|stdout|stderr)$/i;
  const ROW_KEYS = ["data", "items", "records", "results", "targets", "messages",
    "policies", "plans", "tasks", "reports", "versions", "points"];
  const MAX_RESULT_ROWS = 8;
  const MAX_RESULT_COLS = 6;
  const MAX_CELL_LEN = 48;

  /* ---------------- 基础工具 ---------------- */
  function escHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function statusKey(text) {
    const t = String(text == null ? "" : text).trim().toLowerCase();
    return STATUS_MAP[t] || null;
  }

  /** 行内渲染：行内代码（状态词转徽章）、加粗、斜体、链接 */
  function inline(text) {
    let t = escHtml(text);
    t = t.replace(/((?:total|used|free|size|growth_7d)_bytes(?:\*\*)?\s*[：:=]\s*)(\d{7,})/gi,
      (m, prefix, num) => prefix + escHtml(humanBytes(num)));
    t = t.replace(/`([^`]+)`/g, (m, code) => {
      const key = statusKey(code);
      if (key) return '<span class="ag-badge ag-badge-' + key + '">' + code.trim() + "</span>";
      return '<code class="ag-code">' + code + "</code>";
    });
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/(^|[\s(（])_([^_\n]+)_(?=[\s)）.,，。]|$)/g, "$1<em>$2</em>");
    t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return t;
  }

  /** 表格单元格：整格即状态词时渲染为徽章，纯数字/体积右对齐 */
  function cell(text) {
    const raw = String(text == null ? "" : text).trim();
    const key = statusKey(raw.replace(/`/g, ""));
    if (key) {
      return '<td class="ag-td-status"><span class="ag-badge ag-badge-' + key + '">' +
        escHtml(raw.replace(/`/g, "")) + "</span></td>";
    }
    const cls = /^[\d.,]+(\s?%|\s?[KMGTP]?B)?$/.test(raw) ? ' class="ag-td-num"' : "";
    return "<td" + cls + ">" + inline(raw) + "</td>";
  }

  function splitRow(line) {
    let s = String(line).trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    return s.split("|").map((c) => c.trim());
  }

  function isTableSeparator(line) {
    return /^\s*\|?[\s:|-]*-[\s:|-]*\|[\s:|-]*$/.test(line) && line.indexOf("-") >= 0;
  }

  function renderTable(head, body) {
    let h = '<div class="ag-table-wrap"><table class="table table-sm ag-table"><thead><tr>';
    head.forEach((c) => { h += "<th>" + inline(c) + "</th>"; });
    h += "</tr></thead><tbody>";
    body.forEach((row) => {
      h += "<tr>";
      for (let i = 0; i < head.length; i += 1) h += cell(row[i]);
      h += "</tr>";
    });
    return h + "</tbody></table></div>";
  }

  /**
   * Markdown → HTML（受控子集）
   * @param {string} src Markdown 文本
   * @returns {string} HTML
   */
  function md(src) {
    const lines = String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let listBuf = [];
    let listType = "";
    let paraBuf = [];
    let i = 0;

    function flushList() {
      if (listBuf.length) {
        out.push("<" + listType + ' class="ag-list">' + listBuf.join("") + "</" + listType + ">");
        listBuf = [];
        listType = "";
      }
    }
    function flushPara() {
      if (paraBuf.length) {
        out.push('<p class="ag-p">' + paraBuf.join("<br>") + "</p>");
        paraBuf = [];
      }
    }
    function flushAll() { flushPara(); flushList(); }

    while (i < lines.length) {
      const line = lines[i];

      // 代码块
      if (/^\s*```/.test(line)) {
        flushAll();
        const buf = [];
        i += 1;
        while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
        i += 1;
        out.push('<pre class="ag-pre"><code>' + escHtml(buf.join("\n")) + "</code></pre>");
        continue;
      }

      // 表格：当前行含 | 且下一行是分隔行
      if (line.indexOf("|") >= 0 && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        flushAll();
        const head = splitRow(line);
        i += 2;
        const body = [];
        while (i < lines.length && lines[i].indexOf("|") >= 0 && lines[i].trim() !== "") {
          body.push(splitRow(lines[i]));
          i += 1;
        }
        out.push(renderTable(head, body));
        continue;
      }

      // 标题
      const h = /^(#{1,4})\s+(.*)$/.exec(line);
      if (h) {
        flushAll();
        const level = Math.min(h[1].length + 2, 6);
        out.push('<h' + level + ' class="ag-h">' + inline(h[2]) + "</h" + level + ">");
        i += 1;
        continue;
      }

      // 分隔线
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        flushAll();
        out.push('<hr class="ag-hr">');
        i += 1;
        continue;
      }

      // 引用
      if (/^\s*>\s?/.test(line)) {
        flushAll();
        const buf = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          buf.push(inline(lines[i].replace(/^\s*>\s?/, "")));
          i += 1;
        }
        out.push('<blockquote class="ag-quote">' + buf.join("<br>") + "</blockquote>");
        continue;
      }

      // 有序列表
      const ol = /^\s*(\d+)[.)]\s+(.*)$/.exec(line);
      if (ol) {
        flushPara();
        if (listType !== "ol") { flushList(); listType = "ol"; }
        listBuf.push("<li>" + inline(ol[2]) + "</li>");
        i += 1;
        continue;
      }

      // 无序列表
      const ul = /^\s*[-*+]\s+(.*)$/.exec(line);
      if (ul) {
        flushPara();
        if (listType !== "ul") { flushList(); listType = "ul"; }
        listBuf.push("<li>" + inline(ul[1]) + "</li>");
        i += 1;
        continue;
      }

      // 空行
      if (!line.trim()) { flushAll(); i += 1; continue; }

      // 普通段落
      flushList();
      paraBuf.push(inline(line));
      i += 1;
    }
    flushAll();
    return out.join("");
  }

  /* ---------------- 工具步骤卡 ---------------- */
  /** 从工具原始结果里提取真实数据行（兼容多种 API 信封） */
  function resultRows(res) {
    if (!res || typeof res !== "object") return [];
    for (let i = 0; i < ROW_KEYS.length; i += 1) {
      const v = res[ROW_KEYS[i]];
      if (Array.isArray(v)) return v;
      if (v && typeof v === "object") {
        for (let j = 0; j < ROW_KEYS.length; j += 1) {
          if (Array.isArray(v[ROW_KEYS[j]])) return v[ROW_KEYS[j]];
        }
      }
    }
    return [];
  }

  function shortCell(v) {
    if (v == null || v === "") return "-";
    if (typeof v === "boolean") return v ? "是" : "否";
    let s = (typeof v === "object") ? JSON.stringify(v) : String(v);
    s = s.replace(/\s+/g, " ");
    return s.length > MAX_CELL_LEN ? s.slice(0, MAX_CELL_LEN - 1) + "…" : s;
  }

  function humanBytes(v) {
    if (v == null || v === "" || typeof v === "boolean") return "";
    const n = Number(v);
    if (!isFinite(n) || n < 0) return "";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let x = n;
    let u = 0;
    while (x >= 1024 && u < units.length - 1) { x /= 1024; u += 1; }
    const human = u === 0 ? Math.round(x) + " B" : x.toFixed(2) + " " + units[u];
    return human + "（" + Math.round(n).toLocaleString("en-US") + " bytes）";
  }

  function displayCell(k, v) {
    const key = String(k || "").toLowerCase();
    if (key.endsWith("_bytes") || key === "bytes") {
      const h = humanBytes(v);
      if (h) return shortCell(h);
    }
    return shortCell(v);
  }

  /** 结果明细表：把真实行渲染成表格（此前只显示「返回 N 条数据」） */
  function resultTable(res) {
    const rows = resultRows(res).filter((r) =>
      r && typeof r === "object" && !Array.isArray(r));
    if (!rows.length) return "";
    const keys = [];
    rows.slice(0, 20).forEach((r) => Object.keys(r).forEach((k) => {
      if (keys.indexOf(k) < 0 && !SKIP_FIELD.test(k) && !k.startsWith("_")
          && r[k] != null && typeof r[k] !== "object") keys.push(k);
    }));
    const cols = PREFERRED_COLS.filter((k) => keys.indexOf(k) >= 0)
      .concat(keys.filter((k) => PREFERRED_COLS.indexOf(k) < 0))
      .slice(0, MAX_RESULT_COLS);
    if (!cols.length) return "";
    const head = cols.map((k) => FIELD_LABELS[k] || k);
    const body = rows.slice(0, MAX_RESULT_ROWS)
      .map((r) => cols.map((k) => displayCell(k, r[k])));
    const total = rows.length > MAX_RESULT_ROWS
      ? "（前 " + MAX_RESULT_ROWS + " 条 / 共 " + rows.length + " 条）"
      : "（共 " + rows.length + " 条）";
    return '<div class="ag-step-json-title">结果明细' + total + "</div>" +
      renderTable(head, body);
  }

  /** 原始结果 JSON：脱敏 + 截断，兑现步骤卡「原始 JSON」的承诺 */
  function compactJson(res) {
    let s;
    try {
      s = JSON.stringify(res, (k, v) => {
        if (/password|secret|token|private_key|verify_code/i.test(k)) return "***";
        if (typeof v === "number" && /(?:_bytes|^bytes$)/i.test(k)) {
          return humanBytes(v) || v;
        }
        return v;
      }, 2);
    } catch (e) { s = String(res); }
    s = s || "{}";
    return s.length > 8000 ? s.slice(0, 8000) + "\n…（已截断）" : s;
  }

  function summarizeArgs(args) {
    if (!args || typeof args !== "object") return "";
    const keys = Object.keys(args).filter((k) =>
      args[k] !== "" && args[k] != null && !SKIP_FIELD.test(k));
    if (!keys.length) return "无参数";
    return keys.slice(0, 4).map((k) => {
      let v = args[k];
      if (Array.isArray(v)) v = v.join(",");
      if (k === "status" && STATUS_TEXT[statusKey(v)]) v = STATUS_TEXT[statusKey(v)];
      return (ARG_LABELS[k] || k) + "：" + v;
    }).join(" · ");
  }

  function scalarSummary(obj) {
    if (!obj || typeof obj !== "object") return "";
    const humanKeys = {};
    Object.keys(obj).forEach((k) => {
      if (/_human$/i.test(k)) humanKeys[k.toLowerCase()] = true;
    });
    const keys = Object.keys(obj).filter((k) => {
      if (SKIP_FIELD.test(k) || k.startsWith("_") || k === "ok" || k === "raw"
          || obj[k] == null || typeof obj[k] === "object") return false;
      const key = String(k).toLowerCase();
      return !(key.endsWith("_bytes") &&
               humanKeys[key.replace(/_bytes$/, "_human")]);
    });
    const priority = keys.filter((k) =>
      /(_human|_gb)$/i.test(k) || k === "used_percent" ||
      k === "path" || k === "source_label");
    const rest = keys.filter((k) => priority.indexOf(k) < 0);
    const hit = priority.concat(rest).slice(0, 8).map((k) =>
      (FIELD_LABELS[k] || k) + "=" + displayCell(k, obj[k]));
    return hit.length ? hit.join("，") : "";
  }

  function summarizeResult(name, res) {
    const d = res && res.data;
    if (res && res.needs_clarify) {
      const n = Array.isArray(res.candidates) ? res.candidates.length : 0;
      return n ? "匹配到 " + n + " 个候选任务，等待你确认执行哪一个"
        : "匹配到多个任务，等待你确认执行哪一个";
    }
    if (res && res.ok === false) return res.error || "执行未成功";
    if (name === "run_backup_task" && d) {
      return "任务「" + (d.task_name || "已删除任务") + "」" +
        (String(d.status || "").toLowerCase() === "success" ? "执行成功" : "执行失败") +
        (d.size_human ? "，产物 " + d.size_human : "") +
        (d.duration_sec ? "，耗时 " + d.duration_sec + " 秒" : "");
    }
    const rows = resultRows(res);
    if (rows.length) {
      const brief = rows.slice(0, 3).map((r) => {
        if (!r || typeof r !== "object") return shortCell(r);
        const n = r.name || r.task_name || r.title || r.id || r.record_id || "记录";
        const s = r.status || r.last_status;
        return shortCell(n) + (s ? "（" + shortCell(s) + "）" : "");
      }).join("、");
      return "返回 " + rows.length + " 条：" + brief +
        (rows.length > 3 ? " …" : "");
    }
    const kv = scalarSummary(d) || scalarSummary(res);
    if (kv) return kv;
    return res && res.message ? String(res.message) : "已完成";
  }

  /**
   * 歧义澄清候选：渲染成可点击按钮。
   *
   * 复用 #agentMessages 上已有的 .agent-tip 点击委托（填入输入框并发送），
   * 因此按钮点击即发送「执行任务 <ID>」，与手打完全同一条链路，无需新逻辑。
   * @param {Object} res 工具结果（含 needs_clarify / candidates）
   */
  function clarifyCands(res) {
    if (!res || !res.needs_clarify) return "";
    const cands = Array.isArray(res.candidates) ? res.candidates : [];
    if (!cands.length) return "";
    const btns = cands.slice(0, 10).map((c) => {
      const id = String(c && c.id != null ? c.id : "");
      const st = statusKey(c && c.last_status);
      const rec = c && c.recommended ? " ag-cand-rec" : "";
      const name = String((c && c.name) || "");
      return '<button type="button" class="agent-tip ag-cand' + rec + '"' +
        ' data-tip="执行任务 ' + escHtml(id) + '"' +
        ' title="' + escHtml("任务 ID " + id + (name ? "：" + name : "")) + '">' +
        '<span class="ag-cand-id">#' + escHtml(id) + "</span>" +
        '<span class="ag-cand-name">' + escHtml(name) + "</span>" +
        (st ? '<span class="ag-cand-st ag-cand-st-' + st + '">' +
          escHtml(STATUS_TEXT[st] || "") + "</span>" : "") +
        "</button>";
    }).join("");
    return '<div class="ag-cands">' +
      '<div class="ag-cands-title"><i class="bi bi-hand-index-thumb me-1"></i>' +
        escHtml(res.clarify_hint || "点击候选直接执行，或回复「执行任务 <ID>」") + "</div>" +
      '<div class="ag-cands-btns">' + btns + "</div>" +
    "</div>";
  }

  /**
   * 渲染工具执行步骤卡
   * @param {Array} traces [{name, args, result, duration_ms, risk_level}]
   */
  function trace(traces) {
    const list = Array.isArray(traces) ? traces.filter((t) => t && (t.name || t.tool_name)) : [];
    if (!list.length) return "";
    const items = list.map((t) => {
      const name = t.name || t.tool_name;
      const meta = TOOL_META[name] || { label: t.label || name, icon: "bi-tools" };
      const res = t.result || {};
      const key = res.needs_clarify ? "warn" : (res.ok === false ? "failed" : "success");
      const dur = t.duration_ms ? t.duration_ms + " ms" : "";
      return '<details class="ag-step ag-step-' + key + '">' +
        "<summary>" +
          '<span class="ag-step-ico"><i class="bi ' + meta.icon + '"></i></span>' +
          '<span class="ag-step-label">' + escHtml(meta.label) + "</span>" +
          '<span class="ag-step-arg">' + escHtml(summarizeArgs(t.args)) + "</span>" +
          '<span class="ag-badge ag-badge-' + key + '">' +
            (STATUS_TEXT[key] || key) + "</span>" +
          (dur ? '<span class="ag-step-time">' + dur + "</span>" : "") +
        "</summary>" +
        '<div class="ag-step-body">' +
          '<div class="ag-step-sum">' + escHtml(summarizeResult(name, res)) + "</div>" +
          clarifyCands(res) +
          resultTable(res) +
          '<div class="ag-step-json-title">调用参数</div>' +
          '<pre class="ag-pre ag-pre-json"><code>' +
            escHtml(compactJson(t.args || {})) + "</code></pre>" +
          '<div class="ag-step-json-title">原始结果</div>' +
          '<pre class="ag-pre ag-pre-json"><code>' +
            escHtml(compactJson(res)) + "</code></pre>" +
        "</div>" +
      "</details>";
    }).join("");
    return '<div class="ag-steps">' + items + "</div>";
  }

  /**
   * 渲染完整消息气泡
   * @param {{role:string, content:string, kind?:string, trace?:Array, time?:string}} opt
   */
  function bubble(opt) {
    const o = opt || {};
    const isUser = o.role === "user";
    const kind = o.kind || "normal";
    const bbClass = isUser ? "bb-user"
      : kind === "error" ? "bb-error"
      : kind === "confirm" ? "bb-confirm"
      : kind === "system" ? "bb-system"
      : "bb-ai";
    const avatar = isUser
      ? '<div class="agent-avatar av-user"><i class="bi bi-person"></i></div>'
      : '<div class="agent-avatar av-ai"><i class="bi bi-robot"></i></div>';

    const icon = kind === "error" ? '<i class="bi bi-exclamation-octagon me-1"></i>'
      : kind === "confirm" ? '<i class="bi bi-shield-exclamation me-1"></i>' : "";

    const raw = String(o.content || "");
    const body = isUser
      ? '<div class="ag-plain">' + escHtml(raw).replace(/\n/g, "<br>") + "</div>"
      : md(raw);

    const tr = trace(o.trace);
    const foot = isUser ? ""
      : '<div class="ag-foot">' +
          (o.time ? '<span class="ag-time">' + escHtml(o.time) + "</span>" : "") +
          '<button type="button" class="ag-copy" title="复制回答"><i class="bi bi-clipboard"></i> 复制</button>' +
        "</div>";

    return '<div class="agent-row ' + (isUser ? "row-user" : "row-ai") + '">' +
      avatar +
      '<div class="agent-bubble ' + bbClass + '">' +
        '<div class="ag-content">' + icon + body + "</div>" +
        tr + foot +
      "</div>" +
    "</div>";
  }

  window.AgentRender = { md: md, trace: trace, bubble: bubble, escHtml: escHtml, statusKey: statusKey };
})();
