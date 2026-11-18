/* 特殊恢复（无备份救援）：只读扫描 → 按库型识别 → 清单勾选 → SFTP 拉回 + sha256 对账。
   覆盖 Oracle / MySQL / PostgreSQL / 金仓 / openGauss / 达梦 / SQL Server / Redis / MongoDB / SQLite。
   零构建、零依赖（仅用全站已有的 Pager 组件）。 */
(function () {
  "use strict";

  var STATE = { files: [], filtered: [], selected: {}, scan: null };
  var KIND = {
    db_data: "数据文件", db_log: "日志 / WAL / binlog", db_control: "控制文件",
    db_config: "配置文件", backup_artifact: "备份产物", file: "普通文件"
  };
  // 库型徽章配色（未知库型统一灰）
  var DB_COLOR = {
    oracle: "bg-danger", mysql: "bg-primary", mariadb: "bg-primary",
    postgresql: "bg-info text-dark", kingbase: "bg-info text-dark",
    opengauss: "bg-info text-dark", dameng: "bg-warning text-dark",
    mssql: "bg-secondary", redis: "bg-danger", mongodb: "bg-success",
    sqlite: "bg-dark"
  };

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function human(n) {
    n = Number(n || 0);
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }
  function tm(ts) {
    if (!ts) return "-";
    try { return new Date(Number(ts) * 1000).toLocaleString(); }
    catch (e) { return "-"; }
  }
  function status(msg, bad) {
    var el = $("sf_status");
    el.textContent = msg;
    el.className = "small mb-2 " + (bad ? "text-danger" : (msg.indexOf("完成") >= 0 ? "text-success" : "text-muted"));
  }
  function dbBadge(dbType, label) {
    if (!dbType || dbType === "unknown") {
      return '<span class="badge bg-light text-dark border">未识别</span>';
    }
    return '<span class="badge ' + (DB_COLOR[dbType] || "bg-light text-dark border") + '">' +
      esc(label || dbType) + "</span>";
  }
  async function api(method, url, body) {
    var opt = { method: method, headers: { "Content-Type": "application/json" } };
    if (body) opt.body = JSON.stringify(body);
    var r = await fetch(url, opt);
    var d = await r.json().catch(function () { return {}; });
    if (!r.ok || d.ok === false || d.error) {
      throw new Error(d.error || d.message || ("HTTP " + r.status));
    }
    return d;
  }

  function payload() {
    var tid = $("sf_task").value;
    var body = {
      roots: ($("sf_roots").value || "").split(/[\n,]+/)
        .map(function (x) { return x.trim(); }).filter(Boolean),
      max_files: parseInt($("sf_max").value || "5000", 10) || 5000
    };
    if (tid) {
      body.task_id = parseInt(tid, 10);
    } else {
      var host = ($("sf_host").value || "").trim();
      if (!host) throw new Error("请选择备份任务，或填写主机 IP");
      body.host = host;
      body.ssh_port = parseInt($("sf_port").value || "22", 10) || 22;
      body.ssh_user = ($("sf_user").value || "root").trim();
      if ($("sf_pw").value) body.ssh_password = $("sf_pw").value;
    }
    return body;
  }

  function renderVolumes(vols) {
    var el = $("sf_vol");
    if (!vols.length) {
      el.innerHTML = '<tr><td colspan="5" class="text-muted small">远端未返回卷信息（lsblk -J 不可用时会回退 blkid）</td></tr>';
      return;
    }
    el.innerHTML = vols.map(function (v) {
      return "<tr><td>" + esc(v.name || "-") + "</td><td>" + esc(v.type || "-") +
        "</td><td>" + esc(v.size || "-") + "</td><td>" + esc(v.fstype || v.raw || "-") +
        "</td><td>" + esc(v.mountpoint || "-") + "</td></tr>";
    }).join("");
  }

  var SRC_LABEL = { process: "运行进程", oratab: "/etc/oratab", dir: "目录探测" };

  function renderInstances(instances, emptyText) {
    var el = $("sf_inst");
    if (!instances || !instances.length) {
      el.innerHTML = '<tr><td colspan="5" class="text-muted small">' +
        esc(emptyText || "未发现数据库实例（进程未运行且常见数据目录不存在）") + "</td></tr>";
      return;
    }
    el.innerHTML = instances.map(function (it) {
      return "<tr><td>" + dbBadge(it.db_type, it.db_label) + "</td><td>" +
        esc(it.name || "-") + "</td><td class='small' style='word-break:break-all'>" +
        esc(it.datadir || "-") + "</td><td class='small text-nowrap'>" +
        esc(it.port || "-") + "</td><td class='small text-muted'>" +
        esc(SRC_LABEL[it.source] || it.source || "-") + "</td></tr>";
    }).join("");
  }

  function fillDbFilter(files) {
    var sel = $("sf_db");
    var types = {};
    (files || []).forEach(function (f) {
      if (f.db_type && f.db_type !== "unknown") {
        types[f.db_type] = f.db_label || f.db_type;
      }
    });
    var cur = sel.value;
    var opts = Object.keys(types).sort().map(function (k) {
      return '<option value="' + esc(k) + '">' + esc(types[k]) + "</option>";
    }).join("");
    sel.innerHTML = '<option value="">全部库型</option>' + opts;
    if (cur && types[cur]) sel.value = cur;
  }

  function selectedCount() {
    return Object.keys(STATE.selected).filter(function (k) { return STATE.selected[k]; }).length;
  }
  function updateSel() {
    $("sf_sel").textContent = "已选 " + selectedCount() + " 个对象" +
      "（勾选跨页保留；拉回总量上限 2GB，可在结果中查看逐文件 sha256）";
  }

  window.scanFsRenderPage = function () {
    var rows = STATE.filtered;
    var pg = (typeof Pager !== "undefined")
      ? Pager.slice("scanFsFiles", rows, 20)
      : { list: rows, page: 1, pages: 1, total: rows.length };
    $("sf_count").textContent = rows.length;
    $("sf_files").innerHTML = pg.list.length ? pg.list.map(function (f) {
      var checked = STATE.selected[f.path] ? " checked" : "";
      var badge = f.kind === "file"
        ? '<span class="badge bg-light text-dark border">普通文件</span>'
        : '<span class="badge bg-info text-dark">' + esc(KIND[f.kind] || f.kind) + "</span>";
      return '<tr><td><input type="checkbox" class="sf-sel" data-path="' +
        esc(f.path) + '"' + checked + '></td>' +
        '<td class="small" style="word-break:break-all">' + esc(f.path) + "</td>" +
        "<td>" + dbBadge(f.db_type, f.db_label) + "</td>" +
        "<td>" + badge + "</td><td class='text-nowrap'>" + human(f.size) +
        "</td><td class='small text-muted text-nowrap'>" + esc(tm(f.mtime)) + "</td></tr>";
    }).join("") : '<tr><td colspan="6" class="text-muted small">无匹配对象</td></tr>';
    if (typeof Pager !== "undefined") {
      Pager.attach("scanFsFiles", $("sf_filePager"), pg.page, pg.pages,
        pg.total, window.scanFsRenderPage);
    }
    updateSel();
  };

  window.scanFsFilter = function () {
    var kw = ($("sf_kw").value || "").toLowerCase();
    var kind = $("sf_kind").value;
    var db = $("sf_db").value;
    STATE.filtered = STATE.files.filter(function (f) {
      return (!kind || f.kind === kind) &&
        (!db || f.db_type === db) &&
        (!kw || String(f.path).toLowerCase().indexOf(kw) >= 0 ||
          String(KIND[f.kind] || "").toLowerCase().indexOf(kw) >= 0 ||
          String(f.db_label || "").toLowerCase().indexOf(kw) >= 0);
    });
    window.scanFsRenderPage();
  };

  window.scanFsToggleAll = function () {
    var all = STATE.filtered.length > 0 && STATE.filtered.every(function (f) {
      return STATE.selected[f.path];
    });
    STATE.filtered.forEach(function (f) { STATE.selected[f.path] = !all; });
    window.scanFsRenderPage();
  };

  window.scanFsRun = async function () {
    var btn = $("sf_scan_btn");
    btn.disabled = true;
    status("正在通过 SSH 只读扫描远端文件系统…");
    try {
      var body = payload();
      var r = await api("POST", "/api/scan-fs/scan", body);
      STATE.scan = r;
      STATE.files = r.files || [];
      STATE.selected = {};
      renderVolumes(r.volumes || []);
      renderInstances(r.instances || [], "本次扫描未发现实例线索；可直接看下方对象清单");
      fillDbFilter(STATE.files);
      window.scanFsFilter();
      var types = r.db_types || {};
      var typeText = Object.keys(types).map(function (k) {
        return k + " " + types[k];
      }).join("、");
      status("扫描完成：共 " + (r.file_count || 0) + " 个文件" +
        (typeText ? "，识别到 " + typeText : "") +
        "，实例 " + ((r.instances || []).length) + " 个" +
        (r.truncated ? "（已达最大文件数，结果被截断）" : "") +
        "；主机 " + esc(r.host || "-") + "。" + esc(r.method || ""));
    } catch (e) {
      status("扫描失败：" + String(e.message || e), true);
    } finally {
      btn.disabled = false;
    }
  };

  /* 实例清单（全库型）：进程参数 + /etc/oratab + 常见目录兜底 */
  window.scanFsInstances = async function () {
    status("正在探测本机数据库实例（全库型）…");
    try {
      var body = payload();
      var r = await api("POST", "/api/scan-fs/instances", body);
      renderInstances(r.instances || [], "未发现数据库实例（进程未运行且常见数据目录不存在）");
      var types = (r.db_types || []).join("、");
      status("实例清单完成：发现 " + (r.instance_count || 0) + " 个实例" +
        (types ? "（" + types + "）" : "") + "；" + (r.method || ""));
    } catch (e) {
      status("实例探测失败：" + String(e.message || e), true);
    }
  };

  window.scanFsPull = async function () {
    var paths = Object.keys(STATE.selected).filter(function (k) { return STATE.selected[k]; });
    if (!paths.length) { status("请先勾选要拉回的对象", true); return; }
    var box = $("sf_pull");
    box.innerHTML = '<span class="text-primary">正在拉回 ' + paths.length + " 个对象并计算 sha256…</span>";
    try {
      var body = payload();
      body.paths = paths;
      body.dest_dir = "";
      body.max_bytes = 2 * 1024 * 1024 * 1024;
      var r = await api("POST", "/api/scan-fs/pull", body);
      var rows = (r.pulled || []).map(function (p) {
        return "<tr><td class='small' style='word-break:break-all'>" + esc(p.source) +
          "</td><td class='small' style='word-break:break-all'>" + esc(p.local) +
          "</td><td>" + human(p.size) + "</td><td class='small'><code>" +
          esc(p.sha256) + "</code></td></tr>";
      }).join("");
      var fails = (r.failures || []).map(function (f) {
        return "<li>" + esc(f.path) + "：" + esc(f.error) + "</li>";
      }).join("");
      box.innerHTML = '<div class="mb-2"><b>暂存目录：</b><code>' + esc(r.dest) +
        "</code> ｜ 成功 " + (r.pulled_count || 0) + " 个 ｜ 共 " + human(r.bytes) + "</div>" +
        (rows ? '<div class="table-responsive"><table class="table table-sm"><thead class="table-light">' +
          "<tr><th>源路径</th><th>平台暂存路径</th><th>大小</th><th>sha256</th></tr></thead><tbody>" +
          rows + "</tbody></table></div>"
          : '<div class="text-muted small">无成功拉回的对象</div>') +
        (fails ? '<div class="alert alert-warning small mt-2 mb-0"><b>失败项</b><ul class="mb-0">' +
          fails + "</ul></div>" : "");
      status("拉回完成：成功 " + (r.pulled_count || 0) + " 个对象（逐文件 sha256 已对账）");
    } catch (e) {
      box.innerHTML = '<span class="text-danger">拉回失败：' + esc(String(e.message || e)) + "</span>";
      status("拉回失败：" + String(e.message || e), true);
    }
  };

  document.getElementById("sf_files").addEventListener("change", function (e) {
    var t = e.target;
    if (!t || !t.classList || !t.classList.contains("sf-sel")) return;
    STATE.selected[t.getAttribute("data-path")] = !!t.checked;
    updateSel();
  });

  (async function init() {
    try {
      var r = await fetch("/api/tasks");
      var d = await r.json().catch(function () { return []; });
      var list = Array.isArray(d) ? d : (d.items || d.data || []);
      $("sf_task").innerHTML = '<option value="">不使用任务（下方直填主机）</option>' +
        list.map(function (t) {
          return '<option value="' + esc(t.id) + '" title="任务 ID ' + esc(t.id) + '">' +
            esc(t.name || t.task_name || ("任务 " + t.id)) + " · " +
            esc(t.host || "-") + "</option>";
        }).join("");
    } catch (e) { /* 任务列表不可用仍可直填主机 */ }
  })();
})();
