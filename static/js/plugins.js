// -*- coding: utf-8 -*-
// 备份插件页面 JS（参考 dbcheck 插件市场风格）
// 卡片网格 + 已安装/市场分区 + 一键安装/卸载
"use strict";

(function () {
  // ---- bkp-core 别名（避免依赖全局 $ / window.api）----
  const $    = (id) => document.getElementById(id);
  const $$   = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const api  = (m, u, b) => window.BKP.api(m, u, b);
  const esc  = (s) => window.BKP.esc(s);
  const toast = (msg, type, delay) => window.BKP.toast(msg, type, delay);

  // ---- 状态 ----
  let ALL_PLUGINS  = [];           // 全部插件（/api/plugins）
  let META_INFO    = { current_os: "-", package_manager: "-" };
  let CATEGORIES   = [];           // 类别清单（数据库类型）
  let POLLERS      = {};           // pid -> intervalId
  let CURRENT_FILTER = "all";      // 当前 DB 类型筛选
  let CURRENT_SEARCH = "";         // 搜索关键字

  // ===================================================================
  // 初始化
  // ===================================================================
  document.addEventListener("DOMContentLoaded", async () => {
    bindUI();
    // 先确保 BKP.META.display_names 已就绪（与 app.js 解耦）
    try {
      const meta = await api("GET", "/api/meta");
      if (meta && meta.display_names) {
        window.BKP.META = Object.assign(window.BKP.META, meta);
      }
    } catch (e) {
      // 未登录或失败时不影响渲染
    }
    await loadAll();
  });

  function bindUI() {
    const refreshBtn = $("refreshBtn");
    if (refreshBtn) refreshBtn.addEventListener("click", loadAll);

    const batchBtn = $("batchInstallBtn");
    if (batchBtn) batchBtn.addEventListener("click", onBatchInstall);

    const search = $("pluginSearch");
    if (search) {
      search.addEventListener("input", (e) => {
        CURRENT_SEARCH = (e.target.value || "").trim().toLowerCase();
        render();
      });
    }

    // 关闭日志 modal 时停止该插件的轮询
    const logModalEl = $("pluginLogModal");
    if (logModalEl) {
      logModalEl.addEventListener("hidden.bs.modal", () => {
        Object.keys(POLLERS).forEach(stopPolling);
      });
    }
  }

  // ===================================================================
  // 数据加载
  // ===================================================================
  async function loadAll() {
    try {
      // KPI 卡片打加载态
      setStat("statTotal",      "…", "插件总数");
      setStat("statInstalled",   "…", "已安装");
      setStat("statRecommend",   "…", "本机推荐安装");
      setStat("statSupported",   "…", "当前 OS 支持");

      const [listResp, recResp] = await Promise.all([
        api("GET", "/api/plugins"),
        api("GET", "/api/plugins/recommend")
      ]);

      ALL_PLUGINS = (listResp && listResp.plugins) || [];
      META_INFO   = {
        current_os: listResp.current_os || "-",
        package_manager: listResp.package_manager || "未检测到"
      };
      const recCount = (recResp && recResp.count) || 0;

      // 计算 KPI
      const total      = ALL_PLUGINS.length;
      const installed  = ALL_PLUGINS.filter(p => p.installed).length;
      const supported  = ALL_PLUGINS.filter(p => p.os_supported).length;
      setStat("statTotal",     total,        "插件总数");
      setStat("statInstalled",  installed,   "已安装");
      setStat("statRecommend",  recCount,    "本机推荐安装");
      setStat("statSupported",  supported,   META_INFO.current_os + " 支持");

      // 类别筛选 chips
      renderCategoryFilter();

      // 渲染卡片
      render();
    } catch (e) {
      console.error(e);
      toast("加载插件列表失败：" + (e.message || e), "danger");
    }
  }

  function setStat(id, value, label) {
    const el = $(id);
    if (!el) return;
    const num  = el.querySelector(".stat-num");
    const lab  = el.querySelector(".stat-label");
    if (num) num.textContent = value;
    if (lab) lab.textContent = label;
  }

  // ===================================================================
  // 类别筛选（按 supports 数据库类型聚合）
  // ===================================================================
  function renderCategoryFilter() {
    const wrap = $("categoryFilter");
    if (!wrap) return;

    const dbSet = new Set();
    ALL_PLUGINS.forEach(p => (p.db_types || []).forEach(t => dbSet.add(t)));
    const dbTypes = Array.from(dbSet).sort();

    const items = [{ key: "all", label: "全部", count: ALL_PLUGINS.length }];
    dbTypes.forEach(t => {
      const cnt = ALL_PLUGINS.filter(p => (p.db_types || []).includes(t)).length;
      items.push({
        key: t,
        label: window.BKP.META.display_names[t] || t,
        count: cnt
      });
    });

    wrap.innerHTML = items.map(it => `
      <button type="button"
              class="filter-pill ${it.key === CURRENT_FILTER ? "active" : ""}"
              data-key="${esc(it.key)}">
        <i class="bi bi-funnel-fill d-none d-md-inline"></i>
        ${esc(it.label)} <span class="filter-count">${it.count}</span>
      </button>
    `).join("");
    // 绑定
    wrap.querySelectorAll(".filter-pill").forEach(btn => {
      btn.addEventListener("click", () => {
        CURRENT_FILTER = btn.getAttribute("data-key");
        renderCategoryFilter();
        render();
      });
    });
  }

  // ===================================================================
  // 渲染主区（已安装 + 市场 两个分组）
  // ===================================================================
  function render() {
    const installedWrap = $("installedWrap");
    const marketWrap    = $("marketWrap");
    if (!installedWrap || !marketWrap) return;

    const filtered = filterAndSort(ALL_PLUGINS);

    const installedList = filtered.filter(p => p.installed);
    const marketList    = filtered.filter(p => !p.installed);

    // --- 已安装区 ---
    $("installedCount").textContent = installedList.length;
    if (installedList.length === 0) {
      installedWrap.innerHTML = `
        <div class="plugin-empty">
          <i class="bi bi-inboxes"></i>
          <div>暂未安装任何插件。请从下方市场挑选，或点击右上角"一键安装本机所需"。</div>
        </div>`;
    } else {
      installedWrap.innerHTML = installedList.map(renderCard).join("");
      bindCardEvents(installedWrap);
    }

    // --- 市场区 ---
    $("marketCount").textContent = marketList.length;
    if (marketList.length === 0) {
      marketWrap.innerHTML = `
        <div class="plugin-empty">
          <i class="bi bi-check-circle"></i>
          <div>当前筛选下市场无可用插件。</div>
        </div>`;
    } else {
      marketWrap.innerHTML = marketList.map(renderCard).join("");
      bindCardEvents(marketWrap);
    }
  }

  function filterAndSort(rows) {
    let r = rows.slice();
    if (CURRENT_FILTER !== "all") {
      r = r.filter(p => (p.db_types || []).includes(CURRENT_FILTER));
    }
    if (CURRENT_SEARCH) {
      r = r.filter(p => {
        const hay = [p.id, p.name, p.description || "", (p.tags || []).join(" "), (p.db_types || []).join(" ")]
          .join(" ").toLowerCase();
        return hay.indexOf(CURRENT_SEARCH) >= 0;
      });
    }
    // 排序：未装在前 + 本机推荐优先 + 类别+名
    r.sort((a, b) => {
      if (a.installed !== b.installed) return a.installed ? 1 : -1;
      if (!!a.recommended !== !!b.recommended) return a.recommended ? -1 : 1;
      return (a.category + a.name).localeCompare(b.category + b.name);
    });
    return r;
  }

  // ===================================================================
  // 渲染单个插件卡片（dbcheck 风格）
  // ===================================================================
  function renderCard(p) {
    const statusBadge = renderStatusBadge(p);
    const tags = (p.db_types || []).map(t => `
      <span class="plugin-tag plugin-tag-db">${esc(window.BKP.META.display_names[t] || t)}</span>
    `).join("");
    const otherTags = (p.tags || []).slice(0, 3).map(t => `
      <span class="plugin-tag">${esc(t)}</span>
    `).join("");

    // 底部操作按钮
    const actions = renderActions(p);

    // 当前 OS 不可用时给整张卡一个 .disabled 视觉态
    const unavailable = !p.os_supported;

    return `
      <div class="plugin-card ${p.installed ? "is-installed" : "is-market"} ${unavailable ? "is-unavailable" : ""}"
           data-id="${esc(p.id)}">
        <div class="plugin-card-head">
          <div class="plugin-card-title">
            <i class="bi ${esc(p.icon || "bi-plugin")} plugin-card-icon"></i>
            <span class="plugin-card-name">${esc(p.name)}</span>
          </div>
          ${statusBadge}
        </div>

        <div class="plugin-card-desc">${esc(p.description || "—")}</div>

        <div class="plugin-card-tags">
          ${tags}${otherTags}
        </div>

        <div class="plugin-card-foot">
          <div class="plugin-card-meta">
            <span class="plugin-meta-pill"><i class="bi bi-cpu"></i> ${esc((p.required_clients || []).slice(0,2).join(" / ") || "—")}</span>
            ${p.download_url ? `<span class="plugin-meta-pill"><i class="bi bi-download"></i> 离线包</span>` : ""}
            ${p.package_manager_command ? `<span class="plugin-meta-pill"><i class="bi bi-box"></i> ${esc(p.package_manager || "")}</span>` : ""}
            ${p.os_supported_list && p.os_supported_list.length ? `<span class="plugin-meta-pill"><i class="bi bi-ubuntu"></i> ${esc(p.os_supported_list.join("/"))}</span>` : ""}
          </div>
          <div class="plugin-card-actions">${actions}</div>
        </div>

        ${p.last_message ? `<div class="plugin-card-msg">${esc(p.last_message)}</div>` : ""}
      </div>
    `;
  }

  function renderStatusBadge(p) {
    if (p.status === "installing") {
      return `<span class="plugin-badge plugin-badge-running"><span class="spinner-border spinner-border-sm"></span> 安装中</span>`;
    }
    if (p.status === "failed") {
      return `<span class="plugin-badge plugin-badge-failed">失败</span>`;
    }
    if (p.installed) {
      return `<span class="plugin-badge plugin-badge-ok"><i class="bi bi-check-circle-fill"></i> 已安装</span>`;
    }
    if (p.recommended) {
      return `<span class="plugin-badge plugin-badge-recommend"><i class="bi bi-star-fill"></i> 本机推荐</span>`;
    }
    if (!p.os_supported) {
      return `<span class="plugin-badge plugin-badge-muted">未适配本机</span>`;
    }
    return `<span class="plugin-badge plugin-badge-idle">待安装</span>`;
  }

  function renderActions(p) {
    // 已安装：卸载 + 查看日志
    if (p.installed) {
      return `
        <button class="btn btn-outline-secondary btn-sm" data-act="log">
          <i class="bi bi-file-text"></i> 日志
        </button>
        <button class="btn btn-outline-danger btn-sm" data-act="uninstall">
          <i class="bi bi-trash"></i> 卸载
        </button>`;
    }
    // 安装中：查看日志（按钮 disabled）
    if (p.status === "installing") {
      return `
        <button class="btn btn-outline-primary btn-sm" data-act="log">
          <i class="bi bi-file-text"></i> 查看进度
        </button>`;
    }
    // 失败：重试 + 日志
    if (p.status === "failed") {
      return `
        <button class="btn btn-outline-secondary btn-sm" data-act="log">
          <i class="bi bi-file-text"></i> 日志
        </button>
        <button class="btn btn-primary btn-sm" data-act="install">
          <i class="bi bi-arrow-clockwise"></i> 重试安装
        </button>`;
    }
    // 待安装（当前 OS 不支持则禁用）
    if (!p.os_supported) {
      return `
        <button class="btn btn-outline-secondary btn-sm" disabled title="当前 OS 不支持该插件的自动安装">
          <i class="bi bi-slash-circle"></i> 不支持
        </button>`;
    }
    return `
      <button class="btn btn-primary btn-sm" data-act="install">
        <i class="bi bi-download"></i> 一键安装
      </button>`;
  }

  // ===================================================================
  // 卡片事件（事件代理）
  // ===================================================================
  function bindCardEvents(root) {
    root.querySelectorAll(".plugin-card").forEach(card => {
      const pid = card.getAttribute("data-id");
      card.querySelectorAll("[data-act]").forEach(btn => {
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const act = btn.getAttribute("data-act");
          if (act === "install")   onInstall(pid);
          if (act === "uninstall") onUninstall(pid);
          if (act === "log")       showLog(pid);
        });
      });
    });
  }

  // ===================================================================
  // 安装 / 卸载 / 批量
  // ===================================================================
  async function onInstall(pid) {
    try {
      const r = await api("POST", `/api/plugins/${encodeURIComponent(pid)}/install`);
      if (!r.ok) {
        toast(r.message || "安装失败", "danger");
        return;
      }
      toast("已派发安装任务：" + pid, "success");
      // 立即打开日志 modal 让用户看到进度
      showLog(pid);
      // 轮询状态
      startPolling(pid);
    } catch (e) {
      toast("安装失败：" + (e.message || e), "danger");
    }
  }

  async function onUninstall(pid) {
    const p = ALL_PLUGINS.find(x => x.id === pid);
    const label = p ? p.name : pid;
    if (!confirm(`确认卸载插件 "${label}" ？\n\n说明：\n- 仅清理本平台下载的离线安装目录与状态文件。\n- 通过系统包管理器（apt/yum）安装的二进制仍需手动卸载。`)) {
      return;
    }
    try {
      const r = await api("POST", `/api/plugins/${encodeURIComponent(pid)}/uninstall`);
      if (r.ok) {
        toast(r.message || "卸载完成", "success");
      } else {
        toast(r.message || "卸载失败", "danger");
      }
      await loadAll();
    } catch (e) {
      toast("卸载失败：" + (e.message || e), "danger");
    }
  }

  async function onBatchInstall() {
    try {
      // 调用 /api/plugins/recommend -> 一键安装
      const rec = await api("GET", "/api/plugins/recommend");
      const ids = (rec.plugins || []).map(p => p.id);
      if (ids.length === 0) {
        toast("本机暂无可推荐安装的插件（已全部就绪或未配置备份任务）", "dark");
        return;
      }
      if (!confirm(`将为本机推荐安装 ${ids.length} 个插件：\n${ids.join("\n")}\n\n是否继续？`)) {
        return;
      }
      const r = await api("POST", "/api/plugins/batch-install", { ids });
      if (!r.ok) {
        toast(r.error || "派发失败", "danger");
        return;
      }
      const qd = (r.queued || []).length;
      const fl = (r.failed || []).length;
      toast(`已派发 ${qd} 个任务${fl ? "，失败 " + fl + " 个" : ""}`, qd > 0 ? "success" : "danger");
      // 批量轮询
      (r.queued || []).forEach(startPolling);
      await loadAll();
    } catch (e) {
      toast("一键安装失败：" + (e.message || e), "danger");
    }
  }

  // ===================================================================
  // 安装日志 modal + 状态轮询
  // ===================================================================
  async function showLog(pid) {
    const p = ALL_PLUGINS.find(x => x.id === pid) || { id: pid, name: pid };
    $("logTitle").textContent = p.name || pid;
    $("logPluginId").textContent = pid;
    $("logBody").textContent = "加载中…";
    $("logStatus").innerHTML = `<span class="text-muted">—</span>`;
    $("logProgress").style.width = "0%";
    $("logProgress").textContent = "0%";

    const modalEl = $("pluginLogModal");
    const m = bootstrap.Modal.getOrCreateInstance(modalEl);
    m.show();

    await refreshLog(pid);
    startPolling(pid);
  }

  async function refreshLog(pid) {
    try {
      const [stateR, logR] = await Promise.all([
        api("GET",  `/api/plugins/${encodeURIComponent(pid)}/state`),
        api("GET",  `/api/plugins/${encodeURIComponent(pid)}/log`)
      ]);
      const st = (stateR && stateR.state) || {};
      const lg = (logR && logR.log) || "";
      $("logBody").textContent = lg || "(无日志)";
      $("logBody").scrollTop = $("logBody").scrollHeight;

      const status = st.status || "idle";
      const statusText = {
        success:           `<span class="text-success"><i class="bi bi-check-circle-fill"></i> 安装成功</span>`,
        success_with_warn: `<span class="text-warning"><i class="bi bi-exclamation-triangle-fill"></i> 安装成功（有警告）</span>`,
        failed:            `<span class="text-danger"><i class="bi bi-x-circle-fill"></i> 安装失败</span>`,
        running:           `<span class="text-primary"><span class="spinner-border spinner-border-sm"></span> 正在安装</span>`,
        queued:            `<span class="text-secondary"><span class="spinner-border spinner-border-sm"></span> 已入队</span>`,
        manual:            `<span class="text-info"><i class="bi bi-info-circle-fill"></i> 需手工安装</span>`,
        idle:              `<span class="text-muted">空闲</span>`
      }[status] || `<span class="text-muted">${esc(status)}</span>`;
      $("logStatus").innerHTML = statusText + (st.message ? ` <span class="text-muted small">· ${esc(st.message)}</span>` : "");

      const progress = Number(st.progress || 0);
      $("logProgress").style.width = progress + "%";
      $("logProgress").textContent = progress + "%";
      $("logProgress").className = "progress-bar " + (
        status === "success" ? "bg-success" :
        status === "success_with_warn" ? "bg-warning" :
        status === "failed" ? "bg-danger" :
        "bg-primary"
      );

      // 状态终态后停止轮询 + 刷新列表
      if (["success", "success_with_warn", "failed", "manual"].includes(status)) {
        stopPolling(pid);
        await loadAll();
      }
    } catch (e) {
      $("logBody").textContent = "读取失败：" + (e.message || e);
    }
  }

  function startPolling(pid) {
    if (POLLERS[pid]) return;
    POLLERS[pid] = setInterval(() => refreshLog(pid), 1500);
  }

  function stopPolling(pid) {
    if (POLLERS[pid]) {
      clearInterval(POLLERS[pid]);
      delete POLLERS[pid];
    }
  }
})();