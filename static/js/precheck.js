/* 备份/恢复 预检测（Precheck）前端组件。
 * 复用 window.BKP（api/esc/toast）。依赖 bootstrap 5 Modal。
 * 入口：
 *   BKP.runBackupPrecheck(taskId)      —— 备份任务「预检」按钮
 *   BKP.runRestorePrecheck(recordId)   —— 备份记录「恢复预检」按钮
 *   BKP.renderPrecheckModal(title, res)—— 结果弹窗（可复用）
 */
(function () {
  const api = (m, u, b) => window.BKP.api(m, u, b);
  const esc = (s) => window.BKP.esc(s);

  const LEVEL_BADGE = {
    pass:   '<span class="badge bg-success">通过</span>',
    warn:   '<span class="badge bg-warning text-dark">警告</span>',
    fail:   '<span class="badge bg-danger">阻断</span>',
    skip:   '<span class="badge bg-secondary">跳过</span>',
    unknown:'<span class="badge bg-secondary">未知</span>',
  };
  const ITEM_NAMES = {
    task_config: "任务配置",
    ssh_channel: "SSH 通道",
    source_connectivity: "源库连通",
    backup_tools: "备份工具",
    physical_note: "物理模式提示",
    rman_dir: "RMAN 备份目录",
    remote_stage: "远端暂存目录",
    data_dir_hygiene: "数据目录卫生",
    bad_block: "坏块检测",
    disk_space: "存储空间",
    artifact: "备份产物",
    integrity: "完整性校验",
    structure: "结构探测",
    target_channel: "恢复目标",
    restore_tools: "恢复工具",
    target_db: "目标库连通",
    restore_space: "恢复空间",
  };

  const HEAD_BADGE = {
    pass:   '<span class="badge bg-success fs-6">可以执行</span>',
    warn:   '<span class="badge bg-warning text-dark fs-6">有风险，可尝试执行</span>',
    fail:   '<span class="badge bg-danger fs-6">存在阻断项，请先修复</span>',
    unknown:'<span class="badge bg-secondary fs-6">无法判定</span>',
  };

  function ensureModal() {
    let el = document.getElementById("precheckModal");
    if (!el) {
      document.body.insertAdjacentHTML("beforeend", `
        <div class="modal fade" id="precheckModal" tabindex="-1">
          <div class="modal-dialog modal-lg">
            <div class="modal-content">
              <div class="modal-header py-2">
                <h6 class="modal-title" id="precheckTitle">预检</h6>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
              </div>
              <div class="modal-body" id="precheckBody">
                <div class="text-center py-4"><div class="spinner-border text-primary"></div>
                <div class="text-muted mt-2">正在准备…</div></div>
              </div>
              <div class="modal-footer py-1">
                <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">关闭</button>
                <button type="button" class="btn btn-sm btn-primary d-none" id="precheckGo">去执行</button>
              </div>
            </div>
          </div>
        </div>`);
    }
    // getOrCreateInstance：同一元素复用单例。反复 new Modal 会在 BS5 里产生
    // 多实例互踩（show/hide 竞态 → 背景遮罩残留 → 后续点击被透明遮罩吃掉，
    // 用户感知就是"点了没反应，多点几次才行"）。
    return bootstrap.Modal.getOrCreateInstance(el);
  }

  /** 把弹窗渲染成"探测中"状态（任何显示路径都先走这里，绝不出现空壳）。 */
  function renderPending(titleText) {
    const t = document.getElementById("precheckTitle");
    const b = document.getElementById("precheckBody");
    if (t) t.innerHTML = titleText + ' <span class="badge bg-secondary">检测中…</span>';
    if (b) b.innerHTML =
      '<div class="text-center py-4"><div class="spinner-border text-primary"></div>' +
      '<div class="text-muted mt-2">正在探测：任务配置 / SSH 通道 / 源库连通 / 备份工具 / 存储空间' +
      '<br><span class="small">远程探测通常需要 5~30 秒，请稍候…</span></div></div>';
    const go = document.getElementById("precheckGo");
    if (go) go.classList.add("d-none");
  }

  // 带硬超时的 API 调用：底层 BKP.api 的 fetch 无超时，探测类请求一旦挂起
  // 就永久 pending（用户感知=点了没反应）。这里用 AbortController 强制 90s。
  async function apiTimeout(method, url, body, ms) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), ms || 90000);
    try {
      const opt = { method: method, signal: ctrl.signal,
                    headers: { "Content-Type": "application/json" } };
      if (body !== undefined) opt.body = JSON.stringify(body);
      const r = await fetch(url, opt);
      const j = await r.json().catch(() => ({}));
      if (!r.ok) {
        const err = new Error(j.message || j.error || ("HTTP " + r.status));
        err.data = j;
        throw err;
      }
      return j;
    } finally { clearTimeout(timer); }
  }

  // 防抖动：90 秒自动过期（探测请求挂死时锁不会永久卡住入口）
  let _inFlightUntil = 0;

  function showToast(msg, kind) {
    try { window.BKP.toast(msg, kind || "secondary"); } catch (e) { /* toast 不可用不阻断 */ }
  }

  function showModalSafe(modal) {
    try { modal.show(); }
    catch (e) { showToast("预检弹窗打开失败（请关闭其它弹窗后重试）", "danger"); }
  }

  function renderBody(res) {
    const head = HEAD_BADGE[res.level] || esc(res.level || "");
    const rows = (res.items || []).map((it) => {
      const b = LEVEL_BADGE[it.level] || esc(it.level);
      const name = ITEM_NAMES[it.item] || it.item;
      return `<tr>
        <td class="text-nowrap">${esc(name)}</td>
        <td class="text-nowrap">${b}</td>
        <td class="small">${esc(it.message)}</td>
      </tr>`;
    }).join("");
    return `
      <div class="d-flex align-items-center gap-2 mb-2">
        ${head}
        <span class="text-muted small">预检=探测非演练：不落盘产物、不写目标库；通过代表当前配置的已知前置条件均满足。</span>
      </div>
      <table class="table table-sm small mb-0">
        <thead><tr><th style="width:130px">检测项</th><th style="width:70px">结果</th><th>说明</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  async function runBackupPrecheck(taskId) {
    // 无论哪条路径，先把弹窗填成"进行中"再显示——弹窗里永远不会有空壳
    const modal = ensureModal();
    const now = Date.now();
    if (now < _inFlightUntil) {
      renderPending("备份预检");
      showModalSafe(modal);
      showToast("上一次预检仍在后台探测（最长 90 秒），请稍候", "info");
      return;
    }
    _inFlightUntil = now + 95000;
    renderPending("备份预检");
    showModalSafe(modal);
    showToast("预检探测中…", "secondary");
    let res;
    try {
      res = await apiTimeout("POST", `/api/tasks/${taskId}/precheck`, undefined, 90000);
    } catch (e) {
      _inFlightUntil = 0;
      const msg = (e && e.name === "AbortError")
        ? "预检探测超时（90 秒）——请检查数据库服务器网络连通性后重试"
        : (e.message || String(e));
      const t = document.getElementById("precheckTitle");
      const b = document.getElementById("precheckBody");
      if (t) t.innerHTML = `备份预检 <span class="badge bg-danger">失败</span>`;
      if (b) b.innerHTML = '<div class="alert alert-danger mb-0">预检失败: ' + esc(msg) + "</div>";
      showToast("预检失败: " + msg, "danger", 6000);
      return;
    }
    _inFlightUntil = 0;
    try {
      const t = document.getElementById("precheckTitle");
      const b = document.getElementById("precheckBody");
      if (t) t.innerHTML =
        `备份预检 · ${esc(res.task_name || String(taskId))} ${HEAD_BADGE[res.level] || ""}`;
      if (b) b.innerHTML = renderBody(res);
      const go = document.getElementById("precheckGo");
      if (go) {
        go.classList.toggle("d-none", !res.ok);
        go.textContent = "立即备份";
        go.onclick = () => {
          modal.hide();
          if (typeof window.runTask === "function") window.runTask(taskId);
          else if (typeof window.runBackupNow === "function") window.runBackupNow(taskId);
        };
      }
      showModalSafe(modal); // 若用户已把"检测中"窗口关掉，结果一到重新弹出到前台
    } catch (renderErr) {
      console.error("[precheck] 渲染异常:", renderErr, "原始响应:", res);
      const b = document.getElementById("precheckBody");
      if (b) b.innerHTML = '<div class="alert alert-warning mb-0 small">结果渲染异常，原始数据：</div>' +
        '<pre class="small mb-0" style="max-height:300px;overflow:auto">' + esc(JSON.stringify(res, null, 2)) + "</pre>";
    }
  }

  async function runRestorePrecheck(recordId) {
    const modal = ensureModal();
    const now = Date.now();
    if (now < _inFlightUntil) {
      renderPending("恢复预检");
      showModalSafe(modal);
      showToast("上一次预检仍在后台探测（最长 90 秒），请稍候", "info");
      return;
    }
    _inFlightUntil = now + 95000;
    renderPending("恢复预检");
    showModalSafe(modal);
    showToast("恢复预检探测中…", "secondary");
    let res;
    try {
      res = await apiTimeout("POST", `/api/records/${recordId}/restore-precheck`, {}, 90000);
    } catch (e) {
      _inFlightUntil = 0;
      const msg = (e && e.name === "AbortError") ? "恢复预检探测超时（90 秒）" : (e.message || String(e));
      const t = document.getElementById("precheckTitle");
      const b = document.getElementById("precheckBody");
      if (t) t.innerHTML = `恢复预检 <span class="badge bg-danger">失败</span>`;
      if (b) b.innerHTML = '<div class="alert alert-danger mb-0">恢复预检失败: ' + esc(msg) + "</div>";
      showToast("恢复预检失败: " + msg, "danger", 6000);
      return;
    }
    _inFlightUntil = 0;
    document.getElementById("precheckTitle").innerHTML =
      `恢复预检 · ${esc(res.biz_label || String(recordId))} ${HEAD_BADGE[res.level] || ""}`;
    document.getElementById("precheckBody").innerHTML = renderBody(res);
    const go = document.getElementById("precheckGo");
    go.classList.toggle("d-none", !res.ok);
    go.textContent = "去恢复";
    go.onclick = () => {
      modal.hide();
      if (typeof window.openRestoreModal === "function") window.openRestoreModal(recordId);
    };
    showModalSafe(modal);
  }

  window.BKP.runBackupPrecheck = runBackupPrecheck;
  window.BKP.runRestorePrecheck = runRestorePrecheck;
  window.BKP.renderPrecheckModal = (title, res) => {
    const modal = ensureModal();
    document.getElementById("precheckTitle").innerHTML = title;
    document.getElementById("precheckBody").innerHTML = renderBody(res);
    document.getElementById("precheckGo").classList.add("d-none");
    showModalSafe(modal);
  };
})();
