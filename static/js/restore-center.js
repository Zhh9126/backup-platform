/* Unified recovery workspace. Database/file/object restores retain the existing safety gates;
   VM restores use dedicated recovery point IDs and report asynchronous job state honestly. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const { api, esc, fmtTime, statusBadge } = BKP;
  const names = { db: "数据库", file: "文件与 NAS", obj: "对象存储", vm: "虚拟机" };
  const icons = { db: "database", file: "folder2-open", obj: "cloud-arrow-down", vm: "pc-display" };
  const routes = { db: "/tasks", file: "/file_backup", obj: "/object_storage", vm: "/vm" };
  const modes = { physical: "物理备份", logical: "逻辑备份", custom: "自定义备份", hot_copy: "热拷贝",
    full: "全量", incremental: "增量", differential: "差异", snapshot: "快照", synthetic_full: "合成全量",
    file: "文件备份", object: "对象备份", vm: "虚拟机备份", nas: "NAS 备份" };
  const state = { category: "db", page: 1, pages: 1, items: [], selected: null, tasks: [], hosts: [],
    sequence: 0, busy: false, bound: false, optionsLoaded: false, vmCaps: null, vmPending: false,
    job: null, pollTimer: null, deepLinkHandled: false };
  const tr = (s) => window.I18N && I18N.lang === "en" ? I18N.t(s) : s;
  const val = (id) => ($(id).value || "").trim();
  const show = (id, on) => $(id).classList.toggle("d-none", !on);
  const bytes = (n) => BKP.humanSize ? BKP.humanSize(Number(n || 0)) : String(n || 0);
  const rowsOf = (value) => Array.isArray(value) ? value : ((value && (value.data || value.items || value.tasks)) || []);
  function result(kind, message) {
    const box = $("rcResult");
    box.className = "rc-result " + (kind === "failed" ? "failed" : kind === "running" ? "running" : "");
    box.textContent = message;
  }
  function warning(message) { $("rcLoadWarning").textContent = message; show("rcLoadWarning", !!message); }
  function updateSubmit() { $("rcSubmit").disabled = !state.selected || state.busy || state.vmPending; }
  function emptyRows(message) {
    $("rcRecords").innerHTML = '<div class="rc-empty"><i class="bi bi-' + icons[state.category] +
      '"></i><h3>' + esc(message || tr("暂无可用恢复点")) +
      '</h3><p>' + esc(tr("先完成一次成功备份，再回到这里选择恢复点。")) +
      '</p><a class="btn btn-outline-secondary btn-sm" href="' + routes[state.category] + '">' +
      esc(tr("前往备份管理")) + '</a></div>';
  }
  function renderRows() {
    if (!state.items.length) { emptyRows(val("rcSearch") ? tr("没有匹配的恢复点") : tr("暂无可用恢复点")); return; }
    $("rcRecords").innerHTML = state.items.map((record) =>
      '<button type="button" class="rc-record' + (state.selected && state.selected.key === record.key ? " selected" : "") +
      '" data-rc-key="' + esc(record.key) + '" aria-pressed="' + !!(state.selected && state.selected.key === record.key) +
      '"><span class="rc-record-radio"></span><span class="rc-record-body"><span class="rc-record-title">' +
      esc(record.name || record.task_name || tr("已删除任务")) + '<span class="rc-tag">' +
      esc(record.category === "vm" ? (record.provider || "VM") : record.category === "obj" ?
        String(record.provider || "S3").toUpperCase() : (BKP.META?.displayNames?.[record.db_type] || record.db_type || "")) +
      '</span></span><span class="rc-record-sub"><span><i class="bi bi-clock"></i> ' + esc(fmtTime(record.created_at)) +
      '</span><span>' + esc(record.source || record.source_db || "—") + '</span></span><span class="rc-record-meta">' +
      '<span class="rc-tag" title="' + esc(record.mode_inferred ? tr("历史记录，备份方式按任务配置推断") : "") + '">' +
      esc(tr(modes[record.mode] || record.backup_type || record.mode || "—")) + '</span><span class="rc-tag' +
      (Number(record.verified) ? " checked" : "") + '">' + esc(tr(Number(record.verified) ?
        "产物已校验" : record.checksum_present ? "摘要待校验" : "无摘要基线")) + '</span><span class="rc-muted">#' +
      record.source_id + '</span></span></span><span class="rc-record-size">' + esc(bytes(record.size_bytes)) + '</span></button>'
    ).join("");
  }
  async function loadCatalog(options) {
    const sequence = ++state.sequence;
    const query = new URLSearchParams({ category: state.category, page: state.page, size: 8, q: val("rcSearch") });
    if (options?.record_id) query.set("record_id", options.record_id);
    if (options?.rp_id) query.set("rp_id", options.rp_id);
    $("rcRecordTotal").textContent = tr("正在加载…");
    try {
      const data = await api("GET", "/api/recovery/catalog?" + query.toString());
      if (sequence !== state.sequence) return;
      warning("");
      state.category = data.category;
      state.page = data.page; state.pages = data.pages; state.items = data.items || [];
      document.querySelectorAll("[data-rc-type]").forEach((button) => {
        const active = button.dataset.rcType === state.category;
        button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active));
      });
      Object.keys(names).forEach((type) => { $("rcCount_" + type).textContent = data.counts[type] ?? "—"; });
      $("rcRecordTotal").textContent = String(data.total) + " " + tr("个恢复点");
      $("rcPageText").textContent = String(data.page) + " / " + String(data.pages);
      $("rcPrev").disabled = data.page <= 1; $("rcNext").disabled = data.page >= data.pages;
      if (state.selected && !state.items.some((record) => record.key === state.selected.key)) select(null);
      renderRows();
      if ((options?.record_id || options?.rp_id) && state.items.length) select(state.items[0]);
    } catch (error) {
      if (sequence !== state.sequence) return;
      warning(tr("读取恢复点失败：") + error.message);
      $("rcRecordTotal").textContent = tr("读取失败");
      $("rcRecords").innerHTML = '<div class="rc-empty"><i class="bi bi-exclamation-circle"></i><h3>' +
        esc(tr("恢复点暂时无法读取")) + '</h3><p>' + esc(tr("请点击刷新重试，已有备份不会因此删除。")) + '</p></div>';
      select(null);
    }
  }
  async function loadOptions() {
    const values = await Promise.allSettled([api("GET", "/api/tasks"), api("GET", "/api/hosts")]);
    state.tasks = values[0].status === "fulfilled" ? rowsOf(values[0].value) : [];
    state.hosts = values[1].status === "fulfilled" ? rowsOf(values[1].value) : [];
    state.optionsLoaded = true;
    $("rcHost").innerHTML = '<option value="">' + esc(tr("直接输入主机信息")) + '</option>' +
      state.hosts.map((host) => '<option value="' + host.id + '">' + esc(host.name || host.host_key || host.hostname) + '</option>').join("");
    $("rcObjectConnection").innerHTML = '<option value="">' + esc(tr("使用备份任务的连接")) + '</option>' +
      state.tasks.filter((task) => task.db_type === "object_storage").map((task) =>
        '<option value="' + task.id + '">' + esc(task.name + " · " + task.host) + '</option>').join("");
    if (values.some((value) => value.status === "rejected"))
      warning(tr("部分目标配置未能加载，可刷新后重试。"));
  }
  function resetInputs() {
    $("rcForm").reset(); $("rcHostPort").value = "22"; $("rcVmIsolate").checked = true;
    $("rcVmStart").checked = false; $("rcOverwrite").value = "never";
  }
  function select(record) {
    state.selected = record; state.vmCaps = null; state.vmPending = false;
    show("rcTargetEmpty", !record); show("rcForm", !!record);
    $("rcSelectionDot").classList.toggle("on", !!record);
    if (!record) { updateSubmit(); renderRows(); return; }
    resetInputs();
    $("rcSelectedSummary").innerHTML = '<strong>' + esc(record.name || record.task_name || tr("恢复点")) +
      ' · #' + record.source_id + '</strong><span>' + esc(fmtTime(record.created_at)) + ' · ' +
      esc(bytes(record.size_bytes)) + '</span>';
    show("rcDbFields", record.category === "db"); show("rcFileFields", record.category === "file");
    show("rcObjectFields", record.category === "obj"); show("rcVmFields", record.category === "vm");
    show("rcPitrFields", record.category === "db" && record.db_type === "dameng" && record.mode === "physical");
    $("rcPrecheck").disabled = record.category === "vm";
    $("rcFileMode").innerHTML = record.db_type === "synology"
      ? '<option value="local">' + esc(tr("平台本机")) + '</option><option value="nas">' + esc(tr("原 NAS 共享")) + '</option>'
      : '<option value="local">' + esc(tr("平台本机")) + '</option><option value="remote">' + esc(tr("其他主机")) + '</option>';
    if (record.category === "obj") $("rcBucket").value = record.source_db || "";
    updateTargetFields(); renderRows(); updateSubmit();
    if (record.category === "vm") loadVmCapabilities(record);
  }
  function updateTargetFields() {
    const record = state.selected;
    show("rcHostFields", !!record && ((record.category === "db" && val("rcDbMode") === "remote") ||
      (record.category === "file" && val("rcFileMode") === "remote")));
    show("rcDirectHost", !val("rcHost"));
    show("rcVmCloneFields", val("rcVmMode") === "clone");
  }
  async function loadVmCapabilities(record) {
    state.vmPending = true; updateSubmit();
    $("rcVmCapability").textContent = tr("正在读取平台能力…");
    try {
      const vm = await api("GET", "/api/vm/protected/" + record.vm_id);
      if (state.selected?.key !== record.key) return;
      state.vmCaps = vm.caps && typeof vm.caps.clone_to_new_vm === "boolean" ? vm.caps : null;
      if (!state.vmCaps) throw new Error(vm.caps?.notes || tr("请先确认虚拟化平台能力可用"));
      const clone = $("rcVmMode").querySelector('option[value="clone"]');
      clone.disabled = !state.vmCaps.clone_to_new_vm;
      if (clone.disabled) $("rcVmMode").value = "restore";
      $("rcVmCapability").textContent = state.vmCaps.notes || tr("恢复方式按虚拟化平台能力提供。");
      updateTargetFields();
    } catch (error) {
      if (state.selected?.key !== record.key) return;
      $("rcVmCapability").textContent = tr("平台能力读取失败：") + error.message;
      state.vmCaps = null;
    } finally {
      if (state.selected?.key === record.key) { state.vmPending = false; updateSubmit(); }
    }
  }
  function hostBody(body) {
    if (val("rcHost")) { body.target_host_id = Number(val("rcHost")); return; }
    if (!val("rcHostAddress") || !val("rcHostUser")) throw new Error(tr("请填写目标主机地址和 SSH 用户"));
    const port = Number(val("rcHostPort"));
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error(tr("SSH 端口无效"));
    body.target_host = val("rcHostUser") + "@" + val("rcHostAddress") + ":" + port;
    body.target_host_user = val("rcHostUser"); body.target_host_password = $("rcHostPassword").value;
  }
  function plan() {
    const record = state.selected;
    if (!record) throw new Error(tr("请选择恢复点"));
    const body = { record_id: record.source_id };
    let endpoint = "/api/restores", destination = "", detail = "";
    if (record.category === "db") {
      body.target_db = val("rcDatabase");
      body.confirm_same_db = !body.target_db || body.target_db === record.source_db;
      if (val("rcDbMode") === "remote") hostBody(body);
      if (!$("rcPitrFields").classList.contains("d-none") && val("rcTargetTime")) body.target_time = val("rcTargetTime");
      destination = (body.target_host || (body.target_host_id ? tr("纳管主机") + " #" + body.target_host_id : tr("源数据库实例"))) +
        " / " + (body.target_db || record.source_db || tr("源库"));
      detail = body.confirm_same_db ? tr("本次将按源库名恢复，可能覆盖目标数据库的现有数据。") :
        tr("请确认目标实例和数据库名称，以及所需的原生恢复工具。");
    } else if (record.category === "file") {
      if (!val("rcDirectory")) throw new Error(tr("请输入目标恢复目录"));
      body.target_db = val("rcDirectory");
      if (record.db_type === "synology") {
        body.restore_to_nas = val("rcFileMode") === "nas";
        if (body.restore_to_nas) {
          body.target_nas_path = body.target_db;
          body.target_db = "";
        }
      }
      if (val("rcFileMode") === "remote") hostBody(body);
      destination = (body.target_host || (body.target_host_id ? tr("纳管主机") + " #" + body.target_host_id : tr("平台本机"))) +
        " / " + (body.target_nas_path || body.target_db);
      if (body.target_nas_path) destination = tr("原 NAS 共享") + " / " + body.target_nas_path;
      detail = tr("同名文件可能被覆盖，建议恢复到独立目录。");
    } else if (record.category === "obj") {
      if (!val("rcBucket")) throw new Error(tr("请输入目标桶名称"));
      body.target_bucket = val("rcBucket"); body.target_prefix = val("rcPrefix");
      body.overwrite = val("rcOverwrite"); body.apply_deleted = $("rcApplyDeleted").checked;
      body.create_bucket = $("rcCreateBucket").checked;
      if (val("rcObjectConnection")) body.target_task_id = Number(val("rcObjectConnection"));
      const keys = val("rcKeys").split(/\r?\n|,/).map((key) => key.trim()).filter(Boolean);
      if (keys.length) body.restore_keys = keys;
      destination = body.target_bucket + " / " + (body.target_prefix || tr("原始对象路径"));
      detail = body.apply_deleted ? tr("本次会回放删除标记，请核对目标桶和对象范围。") :
        tr("对象将按所选覆盖策略写入目标桶，未勾选时不会回放删除标记。");
    } else {
      if (!state.vmCaps) throw new Error(tr("请先确认虚拟化平台能力可用"));
      const clone = val("rcVmMode") === "clone";
      if (clone && !state.vmCaps.clone_to_new_vm) throw new Error(tr("当前平台不支持克隆为新虚拟机"));
      if (clone && !val("rcVmName")) throw new Error(tr("请输入新虚拟机名称"));
      endpoint = "/api/vm/recovery-points/" + record.source_id + (clone ? "/clone" : "/restore");
      delete body.record_id;
      if (clone) Object.assign(body, { target_name: val("rcVmName"), target_node: val("rcVmNode"),
        network_name: val("rcVmNetwork"), isolate_network: $("rcVmIsolate").checked,
        auto_start: $("rcVmStart").checked, regenerate_mac: true, live: false });
      destination = clone ? body.target_name + (body.target_node ? " @ " + body.target_node : "") : record.name;
      detail = clone ? ($("rcVmIsolate").checked ? tr("新虚拟机使用隔离网络，启动行为按所选配置执行。请核对网络配置。") :
        tr("未启用网络隔离，请避免与生产虚拟机发生地址冲突。")) :
        tr("原位还原将覆盖原虚拟机，请确认相关业务已停止。");
    }
    return { record, body, endpoint, destination, detail };
  }
  async function confirmation(selection) {
    $("rcConfirmBody").innerHTML = '<div class="rc-selected-summary"><strong>' +
      esc(selection.record.name || tr("恢复点")) + ' · #' + selection.record.source_id +
      '</strong><div>' + esc(tr("恢复时间")) + '：' + esc(fmtTime(selection.record.created_at)) +
      '</div><div>' + esc(tr("目标位置")) + '：' + esc(selection.destination) +
      '</div></div><p class="small mt-3 mb-0">' + esc(selection.detail) + '</p>';
    return new Promise((resolve) => {
      const element = $("rcConfirm"), modal = bootstrap.Modal.getOrCreateInstance(element);
      let accepted = false;
      const accept = () => { accepted = true; modal.hide(); };
      $("rcConfirmGo").addEventListener("click", accept, { once: true });
      element.addEventListener("hidden.bs.modal", () => {
        $("rcConfirmGo").removeEventListener("click", accept); resolve(accepted);
      }, { once: true });
      modal.show();
    });
  }
  async function submit(event) {
    event.preventDefault();
    if (state.busy) return;
    state.busy = true; updateSubmit();
    try {
      const selection = plan();
      if (!await confirmation(selection)) return;
      result("running", tr("正在执行恢复，请勿重复提交。"));
      const execute = (code) => api("POST", selection.endpoint,
        code ? Object.assign({}, selection.body, { verify_code: code }) : selection.body);
      const response = window.verifyGate ? await verifyGate(execute) : await execute();
      if (response.job_id) {
        state.job = response.job_id;
        result("running", tr("虚拟机恢复作业已提交，正在等待后端结果。") + " #" + response.job_id);
        schedulePoll();
      } else if (response.status === "failed" || response.status === "simulated" || response.success === false) {
        result("failed", response.message || tr("恢复未完成，请查看执行结果。"));
      } else if (response.status === "running" || response.status === "pending") {
        result("running", response.message || tr("恢复正在执行"));
      } else {
        result("success", response.message || tr("恢复完成"));
      }
      await loadHistory();
    } catch (error) {
      result("failed", error.message || tr("恢复失败"));
      if (error.data?.precheck && BKP.renderPrecheckModal) BKP.renderPrecheckModal(tr("恢复预检"), error.data.precheck);
    } finally { state.busy = false; updateSubmit(); }
  }
  function schedulePoll() {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    if (!state.job) return;
    state.pollTimer = setTimeout(async () => {
      try {
        const job = await api("GET", "/api/vm/jobs/" + state.job);
        if (job.status === "ready" || job.status === "success") {
          result("success", job.message || tr("虚拟机恢复完成")); state.job = null;
        } else if (["failed", "expired", "deleted", "cancelled"].includes(job.status)) {
          result("failed", job.message || tr("虚拟机恢复未完成")); state.job = null;
        } else result("running", (job.message || tr("虚拟机恢复正在执行")) + " · " + (job.progress || 0) + "%");
        await loadHistory();
        if (state.job) schedulePoll();
      } catch (error) {
        result("running", tr("暂时无法获取作业状态，请刷新恢复作业列表。"));
        state.job = null;
      }
    }, 2000);
  }
  async function loadHistory() {
    const replies = await Promise.allSettled([api("GET", "/api/restores"), api("GET", "/api/vm/jobs?limit=30")]);
    const restores = replies[0].status === "fulfilled" ? rowsOf(replies[0].value).map((row) =>
      ({ name: row.task_name || row.db_name || ("#" + row.record_id), type: row.db_type || "—",
        target: row.target_host || row.target_db || "—", started: row.started_at,
        status: row.status, message: row.message })) : [];
    const jobs = replies[1].status === "fulfilled" ? rowsOf(replies[1].value).map((row) =>
      ({ name: row.vm_name || row.target_name || ("VM #" + row.vm_id), type: "VM",
        target: row.target_name || row.target_node || tr("原虚拟机"), started: row.started_at || row.created_at,
        status: row.status === "ready" ? "success" : row.status, message: row.message })) : [];
    const history = restores.concat(jobs).sort((a, b) => String(b.started || "").replace(" ", "T").localeCompare(String(a.started || "").replace(" ", "T"))).slice(0, 8);
    $("rcHistoryRows").innerHTML = history.length ? history.map((row) => '<tr><td>' + esc(row.name) +
      '</td><td>' + esc(row.type) + '</td><td>' + esc(row.target) + '</td><td>' + esc(fmtTime(row.started)) +
      '</td><td>' + statusBadge(row.status) + '</td><td title="' + esc(row.message || "") + '">' +
      esc((row.message || "—").slice(0, 90)) + '</td></tr>').join("") :
      '<tr><td colspan="6" class="rc-history-empty">' + esc(tr(replies.some((reply) => reply.status === "rejected") ?
        "部分作业记录暂时无法读取，请刷新重试。" : "暂无恢复作业。执行后可在这里查看结果。")) + '</td></tr>';
  }
  function bind() {
    if (state.bound) return; state.bound = true;
    document.querySelectorAll("[data-rc-type]").forEach((button) => button.addEventListener("click", () => {
      state.category = button.dataset.rcType; state.page = 1; select(null); loadCatalog();
    }));
    $("rcRecords").addEventListener("click", (event) => {
      const button = event.target.closest("[data-rc-key]");
      if (button) select(state.items.find((record) => record.key === button.dataset.rcKey));
    });
    let timer;
    $("rcSearch").addEventListener("input", () => {
      clearTimeout(timer); timer = setTimeout(() => { state.page = 1; loadCatalog(); }, 250);
    });
    $("rcSearchClear").addEventListener("click", () => { $("rcSearch").value = ""; state.page = 1; loadCatalog(); });
    $("rcPrev").addEventListener("click", () => { state.page--; loadCatalog(); });
    $("rcNext").addEventListener("click", () => { state.page++; loadCatalog(); });
    ["rcDbMode", "rcFileMode", "rcHost", "rcVmMode"].forEach((id) => $(id).addEventListener("change", updateTargetFields));
    $("rcObjectConnection").addEventListener("change", () => {
      const task = state.tasks.find((task) => String(task.id) === val("rcObjectConnection"));
      $("rcBucket").value = task?.db_name || state.selected?.source_db || "";
    });
    $("rcForm").addEventListener("submit", submit);
    $("rcPrecheck").addEventListener("click", () => {
      if (state.selected && state.selected.category !== "vm") BKP.runRestorePrecheck(state.selected.source_id);
    });
    $("rcRefresh").addEventListener("click", () => window.RestoreCenter.init());
    $("rcHistoryRefresh").addEventListener("click", loadHistory);
    window.addEventListener("beforeunload", () => { if (state.pollTimer) clearTimeout(state.pollTimer); });
  }
  window.RestoreCenter = {
    async init() {
      if (!$("rcRecords")) return;
      bind();
      const params = new URLSearchParams(location.search);
      const deep = !state.deepLinkHandled ? { record_id: params.get("record_id"), rp_id: params.get("rp_id") } : {};
      if (!state.deepLinkHandled && names[params.get("type")]) state.category = params.get("type");
      state.deepLinkHandled = true;
      await Promise.allSettled([loadOptions(), loadCatalog(deep), loadHistory()]);
    }
  };
})();
