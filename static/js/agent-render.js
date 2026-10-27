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
  };

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
  function summarizeArgs(args) {
    if (!args || typeof args !== "object") return "";
    const keys = Object.keys(args).filter((k) => args[k] !== "" && args[k] != null);
    if (!keys.length) return "无参数";
    return keys.slice(0, 3).map((k) => {
      let v = args[k];
      if (Array.isArray(v)) v = v.join(",");
      return k === "task_name" ? "任务关键字：" + v
        : k === "task_id" ? "任务 ID：" + v
        : k === "keyword" ? "关键字：" + v
        : k === "scope" ? "范围：" + v
        : k + "：" + v;
    }).join(" · ");
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
      return "任务「" + (d.task_name || ("#" + d.task_id)) + "」" +
        (String(d.status || "").toLowerCase() === "success" ? "执行成功" : "执行失败") +
        (d.size_human ? "，产物 " + d.size_human : "") +
        (d.duration_sec ? "，耗时 " + d.duration_sec + " 秒" : "");
    }
    if (Array.isArray(d)) return "返回 " + d.length + " 条数据";
    if (d && typeof d === "object") {
      const keys = ["object_count", "total_gb", "used_percent", "id", "status"];
      const hit = keys.filter((k) => d[k] != null).map((k) => k + "=" + d[k]);
      if (hit.length) return hit.join("，");
    }
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
        ' title="' + escHtml("#" + id + " " + name) + '">' +
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
      const meta = TOOL_META[name] || { label: name, icon: "bi-tools" };
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
          '<div class="ag-step-json-title">调用参数</div>' +
          '<pre class="ag-pre ag-pre-json"><code>' +
            escHtml(JSON.stringify(t.args || {}, null, 2)) + "</code></pre>" +
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
