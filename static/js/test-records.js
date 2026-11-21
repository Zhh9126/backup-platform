(function () {
  "use strict";
  const root = document.getElementById("testRecordsRoot");
  if (!root) return;
  const $ = id => document.getElementById(id);
  const esc = value => String(value == null ? "" : value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
  const labels = {passed:"通过",partial:"部分通过",failed:"失败",skipped:"跳过",running:"进行中"};
  const levels = {external_real:"外部实机测试",platform_real:"平台实机测试",contract:"接口 / 模拟契约",regression:"自动化回归"};
  const colors = {passed:"success",partial:"warning",failed:"danger",skipped:"secondary",running:"info"};
  let page = 1, pages = 1, timer, requestId = 0;
  const badge = status => '<span class="badge bg-' + (colors[status] || "secondary") + '">' + esc(labels[status] || status) + '</span>';
  async function read(url) {
    const response = await fetch(url,{credentials:"same-origin"});
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || "读取失败");
    return data;
  }
  async function load() {
    const current = ++requestId;
    const query = new URLSearchParams({page:String(page),size:"15",category:$("testRecordCategory") ? $("testRecordCategory").value : root.dataset.category,
      status:$("testRecordStatus").value,q:$("testRecordSearch").value});
    try {
      const result = await read("/api/test-records?" + query);
      if (current !== requestId) return;
      pages = result.pages;
      $("testRecordCounts").innerHTML = Object.keys(labels).map(status => '<span class="border rounded px-3 py-2 small">' + badge(status) + ' <strong>' + (result.counts[status] || 0) + '</strong></span>').join("");
      $("testRecordRows").innerHTML = result.items.length ? result.items.map(row =>
        '<tr><td><strong>' + esc(row.title) + '</strong><div class="text-muted small">' + esc(row.subject) +
        '</div></td><td>' + esc(levels[row.evidence_level] || row.evidence_level) +
        '</td><td>' + badge(row.status) + '</td><td class="small">' + esc(row.finished_at || row.started_at || row.recorded_at) +
        '</td><td class="small" style="max-width:360px;white-space:normal">' + esc(row.summary) +
        '</td><td><button class="btn btn-sm btn-outline-primary" data-test-detail="' + row.id + '">查看证据</button></td></tr>').join("") :
        '<tr><td colspan="6" class="text-muted text-center">暂无匹配的测试记录</td></tr>';
      $("testRecordPage").textContent = "共 " + result.total + " 条 · 第 " + page + " / " + pages + " 页";
      $("testRecordPrev").disabled = page <= 1; $("testRecordNext").disabled = page >= pages;
    } catch (error) {
      if (current === requestId) $("testRecordRows").innerHTML = '<tr><td colspan="6" class="text-danger">' + esc(error.message) + '</td></tr>';
    }
  }
  async function detail(id) {
    $("testRecordDetail").textContent = "正在读取测试记录…";
    bootstrap.Modal.getOrCreateInstance($("testRecordModal")).show();
    try {
      const row = (await read("/api/test-records/" + id)).record;
      const evidence = row.evidence || {};
      $("testRecordDetail").innerHTML = '<h5>' + esc(row.title) + ' ' + badge(row.status) + '</h5><p>' + esc(row.summary) +
        '</p><div class="alert alert-info">' + esc(row.boundary || levels[row.evidence_level]) +
        '</div><p class="small text-muted">测试时间：' + esc(row.started_at) + ' → ' + esc(row.finished_at) +
        ' · 登记时间：' + esc(row.recorded_at) + '</p><h6>校验项</h6><div style="max-height:340px;overflow:auto">' +
        (evidence.checks || []).map(check => '<div class="border-bottom py-2">' + badge(check.status) + ' <strong>' + esc(check.name) + '</strong><div class="small text-muted">' + esc(check.detail || "") + '</div></div>').join("") +
        '</div><h6 class="mt-3">产物与日志</h6>' + (evidence.files || []).map((file,index) =>
          '<div class="border rounded p-2 my-2 small"><div style="overflow-wrap:anywhere">' + esc(file.path) + '</div>' +
          (file.sha256 ? '<div style="overflow-wrap:anywhere">SHA256: ' + esc(file.sha256) + '</div>' : '') +
          (file.size_bytes ? '<div>' + esc(file.size_bytes) + ' bytes</div>' : '') +
          (["log","report"].includes(file.kind) ? '<button class="btn btn-sm btn-link" data-test-evidence="' + id + '/' + index + '">读取日志 / 报告</button>' : '') +
          '</div>').join("") + '<pre id="testRecordLog" class="bg-light p-3 mt-3 small" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre>';
    } catch (error) { $("testRecordDetail").textContent = error.message; }
  }
  $("testRecordRows").addEventListener("click", event => {
    const button = event.target.closest("[data-test-detail]"); if (button) detail(button.dataset.testDetail);
  });
  $("testRecordDetail").addEventListener("click", async event => {
    const button = event.target.closest("[data-test-evidence]"); if (!button) return;
    try {
      const parts = button.dataset.testEvidence.split("/");
      const result = await read("/api/test-records/" + parts[0] + "/evidence/" + parts[1]);
      $("testRecordLog").textContent = result.text + (result.truncated ? "\n[仅显示前 64 KiB]" : "");
    } catch (error) { $("testRecordLog").textContent = error.message; }
  });
  $("testRecordSearch").addEventListener("input", () => { clearTimeout(timer); timer=setTimeout(() => {page=1;load();},250); });
  ["testRecordCategory","testRecordStatus"].forEach(id => {if($(id)) $(id).addEventListener("change",() => {page=1;load();});});
  $("testRecordRefresh").addEventListener("click",load);
  $("testRecordPrev").addEventListener("click",() => {if(page>1) {page--;load();}});
  $("testRecordNext").addEventListener("click",() => {if(page<pages) {page++;load();}});
  window.TestRecords={load};
  load();
})();
