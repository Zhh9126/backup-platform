/* AI 智能助手前端模块（可选交付：镜像/离线包含本文件，GitHub 开源版剥离）。 */
/* 依赖：app.js 在 IIFE 尾部暴露 window.__AIAgentDeps = { api, toast, esc }。 */
(function () {
  "use strict";
  const deps = window.__AIAgentDeps || {};
  const api = deps.api;
  const toast = deps.toast || ((m, t) => console.log("[toast]", t, m));
  const esc = deps.esc || ((s) => String(s == null ? "" : s));

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
   *
   * 优先交给 AgentRender（Markdown / 表格 / 状态徽章 / 工具步骤卡），
   * 该渲染层未加载时降级为纯文本转义输出（保证任何情况下消息都能显示）。
   *
   * @param {string} role user | assistant
   * @param {string} content 文本内容（助手内容为 Markdown）
   * @param {string} kind normal | error | confirm | system
   * @param {Array} toolTrace 工具调用轨迹 [{name, args, result, duration_ms}]
   * @param {string} time 展示用时间字符串
   */
  function agentBubbleHtml(role, content, kind = "normal", toolTrace = [], time = "") {
    const R = window.AgentRender;
    if (R && typeof R.bubble === "function") {
      try {
        return R.bubble({ role, content, kind, trace: toolTrace, time: time });
      } catch (e) {
        // 渲染层异常时降级，避免整条消息丢失
        console.error("[agent] 富文本渲染失败，降级为纯文本", e);
      }
    }
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
    box.insertAdjacentHTML("beforeend",
      agentBubbleHtml(role, content, kind, toolTrace, agentTime(new Date().toISOString())));
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
    // 历史消息里工具结果是独立的 role=tool 消息：按出现顺序回填到上一条
    // assistant 的 tool_calls 上，前端才能把「调用 + 结果 + 耗时」渲染成一张步骤卡
    let pending = null;   // {calls: [...], idx: number}
    msgs.forEach((m) => {
      const role = m.role || "assistant";
      if (role === "tool") {
        if (pending && pending.idx < pending.calls.length) {
          pending.calls[pending.idx].result = m.tool_result;
          pending.idx += 1;
          return;
        }
        html.push('<div class="agent-tool-row"><span class="agent-tool-chip">' +
          '<i class="bi bi-tools"></i>' + esc(m.tool_name || "tool") + ' 已执行</span></div>');
        return;
      }
      if (role !== "user" && role !== "assistant") return;
      const trace = Array.isArray(m.tool_calls)
        ? m.tool_calls.map((tc) => ({ name: tc.name, args: tc.args })) : [];
      pending = trace.length ? { calls: trace, idx: 0 } : null;
      if (!String(m.content || "").trim() && !trace.length) return;
      html.push(agentBubbleHtml(role, m.content || "", "normal", trace, agentTime(m.created_at)));
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
        // 复制回答
        const copyBtn = ev.target.closest ? ev.target.closest(".ag-copy") : null;
        if (copyBtn) {
          const bubble = copyBtn.closest(".agent-bubble");
          const content = bubble ? bubble.querySelector(".ag-content") : null;
          const text = content ? content.innerText.trim() : "";
          const done = () => toast("已复制到剪贴板");
          const fail = () => toast("复制失败，请手动选中复制", "danger");
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(fail);
          } else {
            try {
              const ta = document.createElement("textarea");
              ta.value = text;
              document.body.appendChild(ta);
              ta.select();
              document.execCommand("copy");
              ta.remove();
              done();
            } catch (e) { fail(); }
          }
          return;
        }
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

  // ======================= 7 个自治 Agent 面板 =======================
  const AUTO = { run: null, busy: false };
  const aEsc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
  const AA_SEV = { high: "danger", medium: "warning", low: "info", info: "secondary" };
  const AA_SRC = { llm: "已配置大模型解释", local_rules: "本地规则结论",
                   llm_degraded: "模型降级 · 本地规则结论" };

  function autoStatus(text, bad) {
    const el = document.getElementById("autoStatus");
    if (el) { el.textContent = text; el.className = "small " + (bad ? "text-danger" : "text-muted"); }
  }

  function autoSelected() {
    return Array.prototype.map.call(
      document.querySelectorAll("#autoPanel .aa-sel:checked"), (x) => x.value);
  }

  function renderAutoResults(items) {
    const box = document.getElementById("autoExecResult");
    if (!box) return;
    if (!items.length) { box.innerHTML = '<span class="text-muted small">无执行结果</span>'; return; }
    box.innerHTML = '<table class="table table-sm table-hover mb-0"><thead class="table-light">' +
      "<tr><th>发现</th><th>动作</th><th>状态</th><th>回验</th><th>说明</th></tr></thead><tbody>" +
      items.map((x) => {
        const rc = x.recheck || {};
        const badge = { executed: "success", dry_run: "info", failed: "danger",
                        needs_confirm: "warning", verify_code_required: "warning",
                        rejected: "danger", no_session: "danger" }[x.status] || "secondary";
        return "<tr><td class='small'>" + aEsc(x.finding_id) + "</td><td class='small'>" +
          aEsc(x.action || "-") + "</td><td><span class='badge bg-" + badge + "'>" +
          aEsc(x.status) + "</span></td><td class='small'>" + aEsc(rc.status || "-") +
          "</td><td class='small'>" + aEsc(x.message || rc.note || x.error || "-") +
          "</td></tr>";
      }).join("") + "</tbody></table>";
  }

  function renderAuto() {
    const box = document.getElementById("autoPanel");
    if (!box) return;
    if (!AUTO.run) {
      box.innerHTML = '<span class="text-muted small">点击「开始诊断」让 7 个自治 Agent 并行只读体检平台。</span>';
      return;
    }
    const r = AUTO.run, s = r.summary || {};
    const chips = (r.agents || []).map((a) =>
      '<span class="badge bg-' + (a.status === "ok" ? "success" : "secondary") +
      '" title="' + aEsc(a.error || "诊断完成") + '">' + aEsc(a.agent) +
      (a.status === "ok" ? " ✓" : " 跳过") + "</span>").join("");
    const rows = (r.findings || []).map((f) => {
      const can = !!f.action;
      const act = can ? ("可处理：" + (f.auto_fixable ? "低危自动" : "需确认"))
                      : ("建议：" + (f.suggestion || "人工复核"));
      return "<tr" + (can ? "" : " class='table-light'") + ">" +
        "<td>" + (can ? "<input type='checkbox' class='aa-sel' value='" +
          aEsc(f.id) + "'>" : "") + "</td>" +
        "<td><span class='badge bg-" + (AA_SEV[f.severity] || "secondary") + "'>" +
        aEsc(f.severity) + "</span></td>" +
        "<td><b>" + aEsc(f.title) + "</b><div class='text-muted small'>" +
        aEsc(f.detail) + "</div></td>" +
        "<td class='small'>" + aEsc(act) + "</td></tr>";
    }).join("");
    box.innerHTML =
      '<div class="d-flex justify-content-between align-items-center mb-2 flex-wrap gap-2">' +
        "<div><b>诊断批次 " + aEsc(r.id) + "</b> <span class='text-muted small'>" +
        aEsc(r.created_at) + "</span></div>" +
        '<div class="d-flex gap-2">' +
          '<span class="badge bg-secondary">发现 ' + (s.findings_total || 0) + "</span>" +
          '<span class="badge bg-danger">高危 ' + (s.high || 0) + "</span>" +
          '<span class="badge bg-warning text-dark">中危 ' + (s.medium || 0) + "</span>" +
          '<span class="badge bg-info text-dark">可处理 ' + (s.auto_fixable || 0) + "</span>" +
        "</div></div>" +
      '<div class="aa-grid">' + chips + "</div>" +
      '<div class="aa-explain"><b>结论（' +
        aEsc(AA_SRC[r.explanation_source] || r.explanation_source) + "）：</b>\n" +
        aEsc(r.explanation) +
        (r.llm_note ? "\n" + aEsc(r.llm_note) : "") + "</div>" +
      '<div class="table-responsive"><table class="table table-sm table-hover mb-2">' +
        '<thead class="table-light"><tr><th style="width:32px"></th><th>级别</th>' +
        "<th>发现</th><th>处置</th></tr></thead><tbody>" +
        (rows || '<tr><td colspan="4" class="text-muted small">本轮未发现问题</td></tr>') +
        "</tbody></table></div>" +
      '<div class="d-flex gap-2 flex-wrap mb-2">' +
        '<button class="btn btn-sm btn-outline-secondary" data-aa="dry">预演修复（无副作用）</button>' +
        '<button class="btn btn-sm btn-outline-danger" data-aa="exec">确认执行修复</button>' +
        '<button class="btn btn-sm btn-outline-info" data-aa="verify">回验（重新诊断对比）</button>' +
        '<span class="text-muted small align-self-center">默认只预演；高危动作另需操作验证码。平台不自动删除备份、不自动切换主备。</span>' +
      "</div><div id='autoExecResult'></div>";
  }

  async function autoDiagnose() {
    if (AUTO.busy) return;
    const domain = (document.getElementById("autoDomain") || {}).value || "";
    const useLlm = !!(document.getElementById("autoUseLlm") || {}).checked;
    AUTO.busy = true;
    autoStatus("7 个自治 Agent 并行只读诊断中…");
    try {
      const r = await agentApi("/api/agent/autonomous/diagnose", {
        method: "POST", body: { domains: domain ? [domain] : [], use_llm: useLlm }
      });
      if (!r.ok) throw new Error(r.error || "诊断失败");
      AUTO.run = r.run;
      const panel = document.getElementById("autoPanel");
      if (panel) panel.classList.remove("d-none");
      renderAuto();
      autoStatus("诊断完成：发现 " + ((r.run.summary || {}).findings_total || 0) + " 项");
    } catch (e) {
      autoStatus("诊断失败：" + (e.message || e), true);
    } finally { AUTO.busy = false; }
  }

  async function autoExecute(dry, code) {
    if (!AUTO.run || AUTO.busy) return;
    const ids = autoSelected();
    if (!ids.length) { toast("请先勾选要处理的发现", "warning"); return; }
    if (!dry && !window.confirm("确认真实执行这些白名单修复动作？执行后会自动回验。")) return;
    AUTO.busy = true;
    autoStatus(dry ? "预演中（不产生副作用）…" : "执行并回验中…");
    try {
      const r = await agentApi("/api/agent/autonomous/execute", {
        method: "POST",
        body: { run_id: AUTO.run.id, finding_ids: ids, dry_run: dry,
                approved: !dry, verify_code: code || "" }
      });
      const items = r.results || [];
      if (items.some((x) => x.status === "verify_code_required")) {
        const c = window.prompt("该动作需要高危操作验证码，请输入（可在系统设置 → 高危操作防护 获取）：");
        if (c) { AUTO.busy = false; return autoExecute(false, c.trim()); }
      }
      renderAutoResults(items);
      autoStatus(dry ? "预演完成（未执行）" : "执行完成，见回验结果");
    } catch (e) {
      autoStatus("执行失败：" + (e.message || e), true);
    } finally { AUTO.busy = false; }
  }

  async function autoVerify() {
    if (!AUTO.run || AUTO.busy) return;
    AUTO.busy = true; autoStatus("重新诊断并对比上一批次…");
    try {
      const r = await agentApi("/api/agent/autonomous/verify", {
        method: "POST", body: { run_id: AUTO.run.id }
      });
      const box = document.getElementById("autoExecResult");
      if (box) box.innerHTML = r.ok
        ? '<div class="alert alert-secondary small mb-0">回验：已解决 <b>' +
          (r.resolved || []).length + "</b> 项，仍存在 <b>" + (r.persist || []).length +
          "</b> 项，新增 <b>" + (r.new || []).length + "</b> 项。" +
          ((r.resolved || []).length ? "<br>已解决：" + aEsc((r.resolved || []).join(", ")) : "") +
          "</div>"
        : '<div class="alert alert-warning small mb-0">' + aEsc(r.error) + "</div>";
      autoStatus("回验完成");
    } catch (e) { autoStatus("回验失败：" + (e.message || e), true); }
    finally { AUTO.busy = false; }
  }

  function bindAutonomous() {
    const toggle = document.getElementById("autoToggleBtn");
    if (toggle) toggle.addEventListener("click", () => {
      const p = document.getElementById("autoPanel");
      if (p) { p.classList.toggle("d-none"); if (!p.classList.contains("d-none")) renderAuto(); }
    });
    const runBtn = document.getElementById("autoRunBtn");
    if (runBtn) runBtn.addEventListener("click", autoDiagnose);
    const panel = document.getElementById("autoPanel");
    if (panel) panel.addEventListener("click", (e) => {
      const b = e.target && e.target.closest ? e.target.closest("[data-aa]") : null;
      if (!b) return;
      const k = b.getAttribute("data-aa");
      if (k === "dry") autoExecute(true);
      else if (k === "exec") autoExecute(false);
      else if (k === "verify") autoVerify();
    });
  }

  /** AI 智能助手页面入口 */
  async function initAgent() {
    bindAgentEvents();
    bindAutonomous();
    await loadAgentSessions();
    if (AGENT.sessions.length) {
      await selectAgentSession(AGENT.sessions[0].id);
    }
    const input = document.getElementById("agentInput");
    if (input) input.focus();
  }

  window.__AIAgentInit = initAgent;
})();

