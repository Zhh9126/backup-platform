/* 磁带库与介质管理页：零构建原生 JS，完全离线可用。 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const fmt = (n) => {
    n = Number(n || 0);
    return n > 1073741824 ? (n / 1073741824).toFixed(2) + " GB"
      : n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : n + " B";
  };
  const STATE = {
    in_library: ["在库", "bg-success"], in_drive: ["驱动器中", "bg-primary"],
    offsite: ["离线保管", "bg-warning"], retired: ["已退役", "bg-secondary"],
  };

  function notice(msg, ok) {
    const el = $("tapeNotice");
    if (!el) return;
    el.textContent = msg;
    el.className = "alert py-2 small " + (ok ? "alert-success" : "alert-danger");
    clearTimeout(notice._t);
    notice._t = setTimeout(() => { el.className = "alert d-none py-2 small"; }, 7000);
  }

  async function api(method, path, body) {
    const opt = { method, credentials: "include",
                  headers: { "Content-Type": "application/json" } };
    if (body) opt.body = JSON.stringify(body);
    const r = await fetch(path, opt);
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  function renderKpi(s) {
    const cards = [
      ["介质总数", s.media_total, "在库 " + s.in_library + " · 驱动器 " + s.in_drive],
      ["离线保管", s.offsite, "气隙 / 异地介质"],
      ["写保护 / WORM", `${s.write_protected} / ${s.worm}`, "防误写标记"],
      ["写入作业", s.jobs_total, `成功 ${s.jobs_success} · 失败 ${s.jobs_failed}`],
      ["已校验", s.jobs_verified, "回读 sha256 一致"],
      ["累计写入", fmt(s.bytes_written), "按写入目录统计"],
    ];
    $("tapeKpi").innerHTML = cards.map(([a, b, c]) => `
      <div class="col-6 col-md-4 col-xl-2"><div class="page-card h-100">
        <div class="small text-muted">${esc(a)}</div>
        <div class="mt-1" style="font-size:1.4rem;font-weight:700">${esc(b)}</div>
        <div class="small text-muted">${esc(c)}</div>
      </div></div>`).join("");
  }

  function renderDevices(d) {
    const tools = d.tools || {};
    const need = ["mt", "mtx", "lsscsi", "tar"];
    const toolHtml = need.map(k =>
      `<span class="badge ${tools[k] && tools[k].available ? "bg-success" : "bg-secondary"} me-1">${k}</span>`).join("")
      + (tools.mbuffer && tools.mbuffer.available
        ? '<span class="badge bg-success me-1">mbuffer(缓冲可选)</span>' : "");
    const devRows = (d.drives || []).map(x =>
      `<tr><td>磁带机</td><td><code>${esc(x.node)}</code></td><td>${esc(x.identity || "-")}</td><td>${esc(x.generic || "-")}</td></tr>`)
      .concat((d.changers || []).map(x =>
        `<tr><td>机械手</td><td><code>${esc(x.generic || x.node)}</code></td><td>${esc(x.identity || "-")}</td><td>mtx 目标</td></tr>`));
    const rt = d.runtime || {};
    $("tapeDevices").innerHTML = `
      <div class="mb-2">工具链：${toolHtml}</div>
      <div class="mb-2">运行形态：<b>${d.container ? "容器内" : "宿主机进程"}</b> ·
        持久化链接 ${(d.persistent_links || []).length} 个</div>
      ${devRows.length ? `<div class="table-responsive"><table class="table table-sm mb-2">
        <thead><tr><th>类型</th><th>节点</th><th>识别信息</th><th>通用节点</th></tr></thead>
        <tbody>${devRows.join("")}</tbody></table></div>`
        : '<div class="alert alert-warning py-2">未发现磁带机 / 机械手设备节点（无硬件时可用目录模拟目标演练）。</div>'}
      <div class="mb-1"><span class="badge ${rt.ready ? "bg-success" : "bg-warning"}">${rt.ready ? "接入就绪" : "接入未就绪"}</span></div>
      <ul class="mb-0 small text-muted">${(rt.hints || []).map(h => `<li>${esc(h)}</li>`).join("")}</ul>
      ${d.lsscsi_note ? `<div class="small text-muted mt-1">${esc(d.lsscsi_note)}</div>` : ""}`;
    $("tapeRunArgs").innerHTML = rt.docker_run_args
      ? `<label class="form-label small mb-1">建议的容器设备透传参数（按实际节点核对后再用）</label>
         <pre class="small bg-light p-2 mb-0" style="white-space:pre-wrap">${esc(rt.docker_run_args)}</pre>`
      : '<div class="small text-muted">未检测到可透传设备节点，故不生成 --device 参数。</div>';
  }

  async function loadTargets() {
    try {
      const d = await api("GET", "/api/storage/targets");
      const rows = (Array.isArray(d) ? d : (d.items || d.targets || []))
        .filter(t => t.type === "tape");
      $("tapeTargetSel").innerHTML = rows.length
        ? rows.map(t => `<option value="${t.id}">${esc(t.name || t.id)} · ${esc(t.endpoint || "")}</option>`).join("")
        : '<option value="">（未配置磁带目标）</option>';
    } catch (e) { notice("加载磁带目标失败: " + e.message, false); }
  }

  function renderLibrary(lib) {
    if (!lib) return;
    $("tapeDrives").innerHTML = lib.drives.length ? lib.drives.map(d => `
      <tr><td>${d.drive}</td>
        <td><span class="badge ${d.state === "full" ? "bg-primary" : "bg-secondary"}">${d.state === "full" ? "已装带" : "空"}</span></td>
        <td>${d.loaded_slot == null ? "-" : d.loaded_slot}</td><td>${esc(d.barcode || "-")}</td></tr>`).join("")
      : '<tr><td colspan="4" class="text-muted">无驱动器信息</td></tr>';
    const slots = lib.slots.map(s => ({ ...s, kind: "槽位" }))
      .concat(lib.import_export.map(s => ({ ...s, kind: "I/E 口" })));
    $("tapeSlots").innerHTML = slots.length ? slots.map(s => `
      <tr><td>${s.slot}</td>
        <td><span class="badge ${s.state === "full" ? "bg-success" : "bg-secondary"}">${s.state === "full" ? "有介质" : "空"}</span></td>
        <td>${esc(s.barcode || "-")}</td><td>${esc(s.kind)}</td></tr>`).join("")
      : '<tr><td colspan="4" class="text-muted">无槽位信息</td></tr>';
  }

  window.tapeChanger = async function (op) {
    const tid = Number($("tapeTargetSel").value || 0);
    if (!tid) { notice("请先选择磁带目标", false); return; }
    const slot = Number($("tapeSlot").value || 0);
    if (!slot && (op === "load" || op === "unload")) {
      notice("请输入槽位号", false); return;
    }
    if (!confirm(`确认对磁带库执行 ${op}（槽位 ${slot}）？这会真实操作机械手。`)) return;
    try {
      const r = await api("POST", `/api/tape/library/${tid}/load`, {
        op, slot, drive: Number($("tapeDrive").value || 0) });
      notice(`机械手 ${op} 已执行`, true);
      if (r.out) console.log("[tape]", r.out);
      await Promise.all([loadLibrary(), loadMedia()]);
    } catch (e) { notice("机械手操作失败: " + e.message, false); }
  };

  async function loadLibrary() {
    const tid = Number($("tapeTargetSel").value || 0);
    if (!tid) return;
    try {
      const d = await api("GET", `/api/tape/library/${tid}`);
      renderLibrary(d.library);
      await loadMedia();
    } catch (e) {
      $("tapeDrives").innerHTML =
        `<tr><td colspan="4" class="text-muted">${esc(e.message)}</td></tr>`;
      $("tapeSlots").innerHTML =
        '<tr><td colspan="4" class="text-muted">未配置 changer 或 mtx 不可用时如实显示</td></tr>';
    }
  }

  async function loadMedia() {
    try {
      const d = await api("GET", "/api/tape/media");
      const rows = d.items || [];
      $("tapeMedia").innerHTML = rows.length ? rows.map(m => {
        const st = STATE[m.state] || [m.state, "bg-secondary"];
        return `<tr><td>${esc(m.barcode || "-")}</td><td>${m.slot == null ? "-" : m.slot}</td>
          <td><span class="badge ${st[1]}">${esc(st[0])}</span>
            ${m.worm ? '<span class="badge bg-dark ms-1">WORM</span>' : ""}
            ${m.write_protected ? '<span class="badge bg-info ms-1">写保护</span>' : ""}</td>
          <td>${m.offsite_since ? esc(String(m.offsite_since).slice(0, 10)) : "-"}</td>
          <td>
            <button class="btn btn-sm btn-outline-warning" onclick="tapeMediaAction(${m.id},'offsite')">出库</button>
            <button class="btn btn-sm btn-outline-secondary" onclick="tapeMediaAction(${m.id},'return')">归库</button>
            <button class="btn btn-sm btn-outline-danger" onclick="tapeMediaAction(${m.id},'retire')">退役</button>
          </td></tr>`;
      }).join("") : '<tr><td colspan="5" class="text-muted">暂无介质台账（读取带库库存后自动登记）</td></tr>';
    } catch (e) { notice("加载介质台账失败: " + e.message, false); }
  }

  window.tapeMediaAction = async function (id, action) {
    let note = "";
    if (action === "offsite" || action === "retire") {
      note = prompt(action === "offsite"
        ? "出库去向 / 经办人（审计必填）：" : "退役原因（审计必填）：");
      if (!note || !note.trim()) return;
    }
    try {
      await api("POST", `/api/tape/media/${id}/action`, { action, note });
      notice(`介质 #${id} ${action} 已记录`, true);
      await Promise.all([loadMedia(), loadSummary()]);
    } catch (e) { notice("操作失败: " + e.message, false); }
  };

  async function loadJobs() {
    try {
      const d = await api("GET", "/api/tape/jobs");
      const rows = d.items || [];
      $("tapeJobs").innerHTML = rows.length ? rows.map(j => `
        <tr><td class="small text-truncate" style="max-width:200px" title="${esc(j.object_key)}">${esc(j.object_key)}</td>
          <td>${fmt(j.size_bytes)}</td>
          <td><code class="small">${esc((j.checksum || "").slice(0, 10))}</code></td>
          <td><span class="badge ${j.status === "success" ? "bg-success" : j.status === "failed" ? "bg-danger" : "bg-secondary"}">${esc(j.status)}</span>
            ${j.verified ? '<span class="badge bg-info ms-1">已校验</span>' : ""}</td>
          <td><button class="btn btn-sm btn-outline-primary" data-key="${esc(j.object_key)}" onclick="tapeVerify(this.dataset.key)">校验</button></td>
        </tr>`).join("") : '<tr><td colspan="5" class="text-muted">暂无写入作业</td></tr>';
    } catch (e) { notice("加载写入目录失败: " + e.message, false); }
  }

  window.tapeVerify = async function (key) {
    const tid = Number($("tapeTargetSel").value || 0);
    if (!tid) { notice("请先选择磁带目标", false); return; }
    if (!confirm("写后校验会回读磁带（可能 rewind / 顺序扫描带位），确认继续？")) return;
    try {
      const r = await api("POST", "/api/tape/verify",
        { target_id: tid, object_key: key });
      notice(`校验通过：${key}`, true);
      console.log("[tape verify]", r);
      await loadJobs();
    } catch (e) { notice("校验失败: " + e.message, false); }
  };

  async function loadSummary() {
    try { renderKpi(await api("GET", "/api/tape/summary")); }
    catch (e) { notice("统计加载失败: " + e.message, false); }
  }

  async function loadDevices() {
    try { renderDevices(await api("GET", "/api/tape/devices")); }
    catch (e) { notice("设备发现失败: " + e.message, false); }
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadDevices();
    loadSummary();
    loadTargets().then(() => { loadLibrary(); });
    loadMedia();
    loadJobs();
    $("tapeRefreshLib")?.addEventListener("click", loadLibrary);
    $("tapeTargetSel")?.addEventListener("change", loadLibrary);
  });
})();
