// -*- coding: utf-8 -*-
// 备份插件页面 JS —— 主机维度版
// 支持目标主机下拉（本机 / SSH 主机），安装/卸载/轮询均带 host_id
"use strict";

(function () {
  // ---- bkp-core 别名 ----
  const $    = (id) => document.getElementById(id);
  const $$   = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const api  = (m, u, b) => window.BKP.api(m, u, b);
  const esc  = (s) => window.BKP.esc(s);
  const toast = (msg, type, delay) => window.BKP.toast(msg, type, delay);

  // ---- 状态 ----
  let ALL_PLUGINS  = [];
  let META_INFO    = { current_os: "-", package_manager: "-" };
  let CATEGORIES   = [];
  let POLLERS      = {};           // `${pid}@${hostId}` -> intervalId
  let CURRENT_FILTER = "all";
  let CURRENT_SEARCH = "";
  let CURRENT_HOST_ID = 0;         // 0 = 本机
  let HOST_LIST    = [];           // 主机列表

  // ===================================================================
  // 初始化
  // ===================================================================
  document.addEventListener("DOMContentLoaded", async () => {
    bindUI();
    try {
      const meta = await api("GET", "/api/meta");
      if (meta && meta.display_names) {
        window.BKP.META = Object.assign(window.BKP.META, meta);
      }
    } catch (e) { /* 未登录或失败时不影响渲染 */ }
    await loadHosts();
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

    const hostSelect = $("hostSelect");
    if (hostSelect) {
      hostSelect.addEventListener("change", async (e) => {
        CURRENT_HOST_ID = parseInt(e.target.value, 10) || 0;
        // 切换主机时清除所有轮询
        Object.keys(POLLERS).forEach(k => stopPollingByKey(k));
        await loadAll();
      });
    }

    // 关闭日志 modal 时停止该插件的轮询
    const logModalEl = $("pluginLogModal");
    if (logModalEl) {
      logModalEl.addEventListener("hidden.bs.modal", () => {
        Object.keys(POLLERS).forEach(k => stopPollingByKey(k));
      });
    }
  }

  // ===================================================================
  // 主机列表加载
  // ===================================================================
  async function loadHosts() {
    try {
      const resp = await api("GET", "/api/plugins/hosts");
      HOST_LIST = (resp && resp.hosts) || [];
      const sel = $("hostSelect");
      if (!sel) return;
      sel.innerHTML = HOST_LIST.map(h => `
        <option value="${h.id}" ${h.id === CURRENT_HOST_ID ? "selected" : ""}>
          ${esc(h.name || h.host_key || "未知")}
        </option>
      `).join("");
    } catch (e) {
      console.error("加载主机列表失败:", e);
    }
  }

  function currentHostName() {
    const h = HOST_LIST.find(x => x.id === CURRENT_HOST_ID);
    if (!h) return "本机";
    if (CURRENT_HOST_ID === 0) return "本机";
    return h.name || h.host_key || "远端主机";
  }

  // ===================================================================
  // 数据加载
  // ===================================================================
  async function loadAll() {
    try {
      setStat("statTotal",      "…", "插件总数");
      setStat("statInstalled",   "…", "已安装");
      setStat("statRecommend",   "…", "推荐安装");
      setStat("statSupported",   "…", "当前 OS 支持");

      const hostParam = CURRENT_HOST_ID ? `?host_id=${CURRENT_HOST_ID}` : "";
      const recParam  = CURRENT_HOST_ID ? `?host_id=${CURRENT_HOST_ID}` : "";

      const [listResp, recResp] = await Promise.all([
        api("GET", `/api/plugins${hostParam}`),
        api("GET", `/api/plugins/recommend${recParam}`)
      ]);

      ALL_PLUGINS = (listResp && listResp.plugins) || [];
      META_INFO   = {
        current_os: (listResp && listResp.current_os) || "-",
        package_manager: (listResp && listResp.package_manager) || "未检测到"
      };
      const recCount = (recResp && recResp.count) || 0;

      const total      = ALL_PLUGINS.length;
      const installed  = ALL_PLUGINS.filter(p => p.installed).length;
      const supported  = ALL_PLUGINS.filter(p => p.os_supported).length;

      const osLabel = CURRENT_HOST_ID ? currentHostName() + " 支持" : (META_INFO.current_os + " 支持");
      setStat("statTotal",     total,        "插件总数");
      setStat("statInstalled",  installed,   "已安装");
      setStat("statRecommend",  recCount,    "推荐安装");
      setStat("statSupported",  supported,   osLabel);

      // 更新已安装区提示文案
      const hint = $("installedHint");
      if (hint) hint.textContent = `${currentHostName()} 已具备的备份客户端，可立即用于备份任务`;

      renderCategoryFilter();
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
  // 类别筛选
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
    wrap.querySelectorAll(".filter-pill").forEach(btn => {
      btn.addEventListener("click", () => {
        CURRENT_FILTER = btn.getAttribute("data-key");
        renderCategoryFilter();
        render();
      });
    });
  }

  // ===================================================================
  // 渲染主区（已安装 + 市场）
  // ===================================================================
  function render() {
    const installedWrap = $("installedWrap");
    const marketWrap    = $("marketWrap");
    if (!installedWrap || !marketWrap) return;

    const filtered = filterAndSort(ALL_PLUGINS);
    const installedList = filtered.filter(p => p.installed);
    const marketList    = filtered.filter(p => !p.installed);

    $("installedCount").textContent = installedList.length;
    if (installedList.length === 0) {
      installedWrap.innerHTML = `
        <div class="plugin-empty">
          <i class="bi bi-inboxes"></i>
          <div>${esc(currentHostName())}暂未安装任何插件。请从下方市场挑选，或点击右上角"一键安装所需"。</div>
        </div>`;
    } else {
      installedWrap.innerHTML = installedList.map(renderCard).join("");
      bindCardEvents(installedWrap);
    }

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
    r.sort((a, b) => {
      if (a.installed !== b.installed) return a.installed ? 1 : -1;
      if (!!a.recommended !== !!b.recommended) return a.recommended ? -1 : 1;
      return (a.category + a.name).localeCompare(b.category + b.name);
    });
    return r;
  }

  // ===================================================================
  // 渲染单个插件卡片
  // ===================================================================
  function renderCard(p) {
    const statusBadge = renderStatusBadge(p);
    const tags = (p.db_types || []).map(t => `
      <span class="plugin-tag plugin-tag-db">${esc(window.BKP.META.display_names[t] || t)}</span>
    `).join("");
    const otherTags = (p.tags || []).slice(0, 3).map(t => `
      <span class="plugin-tag">${esc(t)}</span>
    `).join("");

    const actions = renderActions(p);
    const unavailable = !p.os_supported;
    const hostLabel = CURRENT_HOST_ID ? `安装到 ${esc(currentHostName())}` : "一键安装";

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
      return `<span class="plugin-badge plugin-badge-recommend"><i class="bi bi-star-fill"></i> 推荐</span>`;
    }
    if (!p.os_supported) {
      return `<span class="plugin-badge plugin-badge-muted">未适配</span>`;
    }
    return `<span class="plugin-badge plugin-badge-idle">待安装</span>`;
  }

  function renderActions(p) {
    const hostLabel = CURRENT_HOST_ID ? `安装到${esc(currentHostName())}` : "一键安装";
    if (p.installed) {
      return `
        <button class="btn btn-outline-secondary btn-sm" data-act="log">
          <i class="bi bi-file-text"></i> 日志
        </button>
        <button class="btn btn-outline-danger btn-sm" data-act="uninstall">
          <i class="bi bi-trash"></i> 卸载
        </button>`;
    }
    if (p.status === "installing") {
      return `
        <button class="btn btn-outline-primary btn-sm" data-act="log">
          <i class="bi bi-file-text"></i> 查看进度
        </button>`;
    }
    if (p.status === "failed") {
      return `
        <button class="btn btn-outline-secondary btn-sm" data-act="log">
          <i class="bi bi-file-text"></i> 日志
        </button>
        <button class="btn btn-primary btn-sm" data-act="install">
          <i class="bi bi-arrow-clockwise"></i> 重试
        </button>`;
    }
    if (!p.os_supported) {
      return `
        <button class="btn btn-outline-secondary btn-sm" disabled title="目标主机 OS 不支持该插件的自动安装">
          <i class="bi bi-slash-circle"></i> 不支持
        </button>`;
    }
    return `
      <button class="btn btn-primary btn-sm" data-act="install" title="${esc(hostLabel)}">
        <i class="bi bi-download"></i> ${esc(hostLabel)}
      </button>`;
  }

  // ===================================================================
  // 卡片事件
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
  function hostBody(extra) {
    const body = extra || {};
    if (CURRENT_HOST_ID) body.host_id = CURRENT_HOST_ID;
    return body;
  }

  async function onInstall(pid) {
    try {
      const r = await api("POST", `/api/plugins/${encodeURIComponent(pid)}/install`, hostBody());
      if (!r.ok) {
        toast(r.message || "安装失败", "danger");
        return;
      }
      if (r.installed) {
        toast(r.message || "已安装，无需重复操作", "success");
        await loadAll();
        return;
      }
      toast(`已派发安装任务到${currentHostName()}：${pid}`, "success");
      showLog(pid);
      startPolling(pid);
    } catch (e) {
      toast("安装失败：" + (e.message || e), "danger");
    }
  }

  async function onUninstall(pid) {
    const p = ALL_PLUGINS.find(x => x.id === pid);
    const label = p ? p.name : pid;
    const hostName = currentHostName();
    if (!confirm(`确认从"${hostName}"卸载插件 "${label}" ？\n\n说明：\n- 仅清理平台管理的离线安装目录与状态文件。\n- 通过系统包管理器（apt/yum）安装的二进制仍需手动卸载。`)) {
      return;
    }
    try {
      const r = await api("POST", `/api/plugins/${encodeURIComponent(pid)}/uninstall`, hostBody());
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
      const hostName = currentHostName();
      const hostParam = CURRENT_HOST_ID ? `?host_id=${CURRENT_HOST_ID}` : "";
      const rec = await api("GET", `/api/plugins/recommend${hostParam}`);
      const ids = (rec.plugins || []).map(p => p.id);
      if (ids.length === 0) {
        toast(`${hostName}暂无可推荐安装的插件（已全部就绪或未配置备份任务）`, "dark");
        return;
      }
      if (!confirm(`将为${hostName}推荐安装 ${ids.length} 个插件：\n${ids.join("\n")}\n\n是否继续？`)) {
        return;
      }
      const r = await api("POST", "/api/plugins/batch-install", hostBody({ ids }));
      if (!r.ok) {
        toast(r.error || "派发失败", "danger");
        return;
      }
      const qd = (r.queued || []).length;
      const fl = (r.failed || []).length;
      toast(`已派发 ${qd} 个任务${fl ? "，失败 " + fl + " 个" : ""}`, qd > 0 ? "success" : "danger");
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
      const hostParam = CURRENT_HOST_ID ? `?host_id=${CURRENT_HOST_ID}` : "";
      const [stateR, logR] = await Promise.all([
        api("GET",  `/api/plugins/${encodeURIComponent(pid)}/state${hostParam}`),
        api("GET",  `/api/plugins/${encodeURIComponent(pid)}/log${hostParam}`)
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

      if (["success", "success_with_warn", "failed", "manual"].includes(status)) {
        stopPolling(pid);
        await loadAll();
      }
    } catch (e) {
      $("logBody").textContent = "读取失败：" + (e.message || e);
    }
  }

  function pollerKey(pid) {
    return `${pid}@${CURRENT_HOST_ID}`;
  }

  function startPolling(pid) {
    const key = pollerKey(pid);
    if (POLLERS[key]) return;
    POLLERS[key] = setInterval(() => refreshLog(pid), 1500);
  }

  function stopPolling(pid) {
    const key = pollerKey(pid);
    stopPollingByKey(key);
  }

  function stopPollingByKey(key) {
    if (POLLERS[key]) {
      clearInterval(POLLERS[key]);
      delete POLLERS[key];
    }
  }

  // ===================================================================
  // JDBC 驱动管理（插件市场页内嵌板块）
  // ===================================================================
  async function loadJdbc() {
    try {
      const st = await api("GET", "/api/jdbc/status");
      const el = $("jvmStatus");
      if (el) {
        // 原生直连驱动状态优先展示（无 Java 依赖）
        const nat = st && st.native && st.native.drivers;
        if (nat) {
          const okCnt = Object.values(nat).filter(d => d.available).length;
          const total = Object.keys(nat).length;
          el.className = "small align-self-center " + (okCnt ? "text-success" : "text-warning");
          el.textContent = okCnt
            ? ("原生直连就绪 " + okCnt + "/" + total + " 类驱动")
            : "原生直连驱动缺失";
        }
        // JVM 状态（JDBC 可选兜底通道）
        if (st && st.jvm) {
          const j = st.jvm;
          let ver = "";
          const m = /java-1[0-9]-openjdk-([0-9.]+)/.exec(j.path || "");
          if (m) ver = "Java " + m[1];
          if (j.found && j.started) {
            el.title = "JDBC 兜底通道就绪" + (ver ? " · " + ver : "");
          } else {
            el.title = "JVM 未检测到（不影响使用：直连走原生 Python 驱动，无需 Java）";
          }
        }
      }
    } catch (e) { /* 不影响驱动列表 */ }
    await loadJdbcDrivers();
  }

  async function loadJdbcDrivers() {
    const body = $("jdbcDriverBody");
    if (!body) return;
    try {
      const data = await api("GET", "/api/jdbc/drivers");
      const list = data.drivers || [];
      const cnt = $("jdbcDriverCount");
      if (cnt) cnt.textContent = list.length;
      if (!list.length) {
        body.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-3">暂无驱动，点击右上角"上传驱动 jar"上传</td></tr>';
        return;
      }
      body.innerHTML = list.map((d) => {
        const size = (d.size >= 1048576) ? (d.size / 1048576).toFixed(1) + " MB" : (d.size / 1024).toFixed(1) + " KB";
        const badge = d.registered
          ? '<span class="badge bg-success">已注册</span>'
          : '<span class="badge bg-secondary">未注册</span>';
        return '<tr>'
          + '<td><i class="bi bi-filetype-jar text-info me-1"></i>' + esc(d.name) + "</td>"
          + "<td>" + size + "</td>"
          + '<td class="text-muted">' + (d.mtime_h || "") + "</td>"
          + "<td>" + badge + "</td>"
          + '<td class="text-end">'
          + '<a class="btn btn-sm btn-outline-primary" href="/api/jdbc/drivers/' + encodeURIComponent(d.name) + '/download" download title="下载 jar"><i class="bi bi-download"></i> 下载</a> '
          + '<button class="btn btn-sm btn-outline-danger" data-del="' + encodeURIComponent(d.name) + '" title="删除 jar"><i class="bi bi-trash"></i></button>'
          + "</td></tr>";
      }).join("");
      $$("#jdbcDriverBody button[data-del]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const name = decodeURIComponent(btn.dataset.del);
          if (!confirm("确定删除驱动 " + name + " ?")) return;
          try {
            await api("DELETE", "/api/jdbc/drivers/" + encodeURIComponent(name));
            toast("已删除驱动 " + name, "success");
            await loadJdbcDrivers();
          } catch (e) {
            toast("删除失败：" + (e.message || e), "danger");
          }
        });
      });
    } catch (e) {
      body.innerHTML = '<tr><td colspan="5" class="text-center text-danger py-3">加载失败：' + esc(e.message || e) + "</td></tr>";
    }
  }

  function bindJdbc() {
    const up = $("jdbcUploadInput");
    if (up) {
      up.addEventListener("change", async () => {
        const f = up.files && up.files[0];
        if (!f) return;
        const fd = new FormData();
        fd.append("file", f);
        try {
          const resp = await fetch("/api/jdbc/drivers/upload", { method: "POST", body: fd });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok || !data.success) throw new Error((data && (data.error || data.message)) || "上传失败");
          toast(data.message || "上传成功", "success");
          up.value = "";
          await loadJdbcDrivers();
        } catch (e) {
          toast("上传失败：" + (e.message || e), "danger");
        }
      });
    }
    const ref = $("jdbcRefreshBtn");
    if (ref) ref.addEventListener("click", loadJdbc);
    const tbtn = $("jdbcTestBtn");
    if (tbtn) {
      tbtn.addEventListener("click", async () => {
        const payload = {
          db_type: $("jdbcTestType").value,
          host: $("jdbcTestHost").value || "127.0.0.1",
          port: parseInt($("jdbcTestPort").value || "0", 10) || 0,
          db_name: $("jdbcTestDb").value,
          username: $("jdbcTestUser").value,
          password: $("jdbcTestPwd").value,
        };
        const box = $("jdbcTestResult");
        box.classList.remove("d-none");
        box.className = "small mt-2 alert alert-light border";
        box.textContent = "连接中…";
        try {
          const data = await api("POST", "/api/jdbc/test-connection", payload);
          if (data.success) {
            box.className = "small mt-2 alert alert-success py-2";
            box.textContent = data.message
              + (data.info && data.info.latency_ms != null ? "（" + data.info.latency_ms + "ms）" : "");
          } else {
            box.className = "small mt-2 alert alert-danger py-2";
            box.textContent = data.message || "连接失败";
          }
        } catch (e) {
          box.className = "small mt-2 alert alert-danger py-2";
          box.textContent = "连接失败：" + (e.message || e);
        }
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindJdbc();
    loadJdbc();
  });
})();
