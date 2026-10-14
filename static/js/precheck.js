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
              <div class="modal-body" id="precheckBody"></div>
              <div class="modal-footer py-1">
                <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">关闭</button>
                <button type="button" class="btn btn-sm btn-primary d-none" id="precheckGo">去执行</button>
              </div>
            </div>
          </div>
        </div>`);
    }
    return new bootstrap.Modal(el);
  }

  function renderBody(res) {
    const head = HEAD_BADGE[res.level] || esc(res.level || "");
    const rows = (res.items || []).map((it) => {
      const b = LEVEL_BADGE[it.level] || esc(it.level);
      return `<tr>
        <td class="text-nowrap">${esc(it.item)}</td>
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
    window.BKP.toast("预检中…", "secondary");
    let res;
    try {
      res = await api("POST", `/api/tasks/${taskId}/precheck`);
    } catch (e) {
      window.BKP.toast("预检失败: " + e.message, "danger");
      return;
    }
    const modal = ensureModal();
    document.getElementById("precheckTitle").innerHTML =
      `备份预检 #${esc(String(taskId))} ${HEAD_BADGE[res.level] || ""}`;
    document.getElementById("precheckBody").innerHTML = renderBody(res);
    const go = document.getElementById("precheckGo");
    go.classList.toggle("d-none", !res.ok);
    go.textContent = "立即备份";
    go.onclick = () => {
      modal.hide();
      if (typeof window.runTask === "function") window.runTask(taskId);
      else if (typeof window.runBackupNow === "function") window.runBackupNow(taskId);
    };
    modal.show();
  }

  async function runRestorePrecheck(recordId) {
    window.BKP.toast("恢复预检中…", "secondary");
    let res;
    try {
      res = await api("POST", `/api/records/${recordId}/restore-precheck`, {});
    } catch (e) {
      window.BKP.toast("恢复预检失败: " + e.message, "danger");
      return;
    }
    const modal = ensureModal();
    document.getElementById("precheckTitle").innerHTML =
      `恢复预检 #${esc(String(recordId))} ${HEAD_BADGE[res.level] || ""}`;
    document.getElementById("precheckBody").innerHTML = renderBody(res);
    const go = document.getElementById("precheckGo");
    go.classList.toggle("d-none", !res.ok);
    go.textContent = "去恢复";
    go.onclick = () => {
      modal.hide();
      if (typeof window.openRestoreModal === "function") window.openRestoreModal(recordId);
    };
    modal.show();
  }

  window.BKP.runBackupPrecheck = runBackupPrecheck;
  window.BKP.runRestorePrecheck = runRestorePrecheck;
  window.BKP.renderPrecheckModal = (title, res) => {
    const modal = ensureModal();
    document.getElementById("precheckTitle").innerHTML = title;
    document.getElementById("precheckBody").innerHTML = renderBody(res);
    document.getElementById("precheckGo").classList.add("d-none");
    modal.show();
  };
})();
