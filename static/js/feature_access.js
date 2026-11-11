/* 功能项与权限：按需启用 + 权限分离（系统设置页）
 * 数据契约：GET/POST /api/rbac/features -> core/features.py
 * 模板只提供 #faRoot 容器，界面由本脚本渲染，避免模板中文反复过 i18n 门禁。
 */
(function () {
  "use strict";
  var root = document.getElementById("faRoot");
  if (!root || !window.BKP) return;
  var esc = BKP.esc || function (s) { return String(s == null ? "" : s); };
  var api = BKP.api;
  var toast = BKP.toast || function (m) { window.alert(m); };
  var state = null;
  var lastRole = "", lastUser = "";

  function el(id) { return document.getElementById(id); }
  function checkedIds(box) {
    return Array.prototype.slice.call(box.querySelectorAll("input[data-fid]"))
      .filter(function (c) { return c.checked; })
      .map(function (c) { return c.getAttribute("data-fid"); });
  }
  function allIds() { return state.catalog.map(function (f) { return f.id; }); }
  function enabledIds() {
    return state.catalog.filter(function (f) { return state.config.enabled[f.id]; })
      .map(function (f) { return f.id; });
  }
  function selectedForRole(role) {
    var v = state.config.roles[role];
    return Array.isArray(v) ? v : allIds();
  }
  function selectedForUser(user) {
    if (!user) return selectedForRole(el("faRole").value);
    var v = state.config.users[user];
    if (Array.isArray(v)) return v;
    var row = (state.users || []).filter(function (u) { return u.username === user; })[0];
    return selectedForRole(row && row.role ? row.role : el("faRole").value);
  }

  function featureBox(selected) {
    var groups = {}, order = [];
    state.catalog.forEach(function (f) {
      if (!groups[f.group]) { groups[f.group] = []; order.push(f.group); }
      groups[f.group].push(f);
    });
    return order.map(function (g) {
      return '<div class="mb-2"><div class="fw-bold small text-muted mb-1">' +
        esc(g) + '</div><div class="d-flex flex-wrap gap-2">' +
        groups[g].map(function (f) {
          var prot = !!f.protected;
          var on = prot || selected.indexOf(f.id) >= 0;
          return '<label class="form-check form-check-inline small me-2"' +
            (prot ? ' title="系统必需功能项，不可停用"' : '') + '>' +
            '<input class="form-check-input" type="checkbox" data-fid="' + esc(f.id) + '"' +
            (on ? " checked" : "") + (prot ? " disabled" : "") + '> ' +
            esc(f.name) + (prot ? ' <span class="badge bg-secondary">必需</span>' : '') +
            '</label>';
        }).join("") + '</div></div>';
    }).join("");
  }

  function msg(text, ok) {
    var box = el("faMsg");
    if (!box) return;
    box.textContent = text;
    box.className = "alert " + (ok ? "alert-success" : "alert-danger");
  }

  function renderShell() {
    root.innerHTML =
      '<p class="form-text-hint mb-3">勾选本次部署需要的功能项；停用后菜单、页面与 API 同时不可访问。' +
      '再按角色或单个用户分配可用功能项，实现权限分离。未保存任何配置时全部启用，不影响存量部署。</p>' +
      '<div id="faMsg" class="alert d-none"></div>' +
      '<div class="row g-3"><div class="col-lg-7">' +
        '<div class="d-flex justify-content-between align-items-center mb-2 flex-wrap gap-2">' +
          '<h6 class="mb-0">平台功能项（按需启用）</h6><div>' +
          '<button type="button" class="btn btn-sm btn-outline-secondary" id="faAll">全选</button> ' +
          '<button type="button" class="btn btn-sm btn-outline-secondary" id="faMin">仅保留系统项</button>' +
          '</div></div>' +
        '<div id="faPlatform" class="border rounded p-3" style="max-height:320px;overflow:auto"></div>' +
      '</div><div class="col-lg-5">' +
        '<h6>按角色分配</h6>' +
        '<select class="form-select form-select-sm mb-2" id="faRole"></select>' +
        '<div id="faRoleBox" class="border rounded p-3 mb-3" style="max-height:210px;overflow:auto"></div>' +
        '<h6>按用户覆盖（可选）</h6>' +
        '<select class="form-select form-select-sm mb-2" id="faUser"></select>' +
        '<div id="faUserBox" class="border rounded p-3 mb-2" style="max-height:180px;overflow:auto"></div>' +
        '<button type="button" class="btn btn-sm btn-outline-secondary" id="faClearUser">清除该用户覆盖（跟随角色）</button>' +
      '</div></div>' +
      '<div class="mt-3 d-flex gap-2 align-items-center flex-wrap">' +
        '<button type="button" class="btn btn-primary btn-sm" id="faSave">保存功能与权限配置</button> ' +
        '<button type="button" class="btn btn-outline-secondary btn-sm" id="faReset">恢复全部启用</button> ' +
        '<span class="text-muted small" id="faSummary"></span></div>';
  }

  function renderSelectors() {
    el("faRole").innerHTML = state.roles.map(function (r) {
      return '<option value="' + esc(r) + '">' +
        esc((state.role_labels || {})[r] || r) + '</option>';
    }).join("");
    el("faUser").innerHTML = '<option value="">（跟随角色，不单独设置）</option>' +
      (state.users || []).map(function (u) {
        return '<option value="' + esc(u.username) + '">' +
          esc(u.username) + '（' + esc(u.role || "-") + '）</option>';
      }).join("");
  }

  function renderBoxes() {
    el("faPlatform").innerHTML = featureBox(enabledIds());
    el("faRoleBox").innerHTML = featureBox(selectedForRole(el("faRole").value));
    el("faUserBox").innerHTML = featureBox(selectedForUser(el("faUser").value));
    el("faSummary").textContent = "已启用 " + enabledIds().length + "/" +
      state.catalog.length + " 个功能项";
  }

  // 切换下拉前把当前勾选写回内存，避免未保存的修改丢失
  function syncCurrent() {
    if (!state) return;
    if (lastRole) state.config.roles[lastRole] = checkedIds(el("faRoleBox"));
    if (lastUser && el("faUser").value === lastUser) {
      state.config.users[lastUser] = checkedIds(el("faUserBox"));
    }
    lastRole = el("faRole").value;
    lastUser = el("faUser").value;
  }

  function save(text, skipUser) {
    syncCurrent();
    var enabled = {};
    state.catalog.forEach(function (f) {
      var cb = root.querySelector('#faPlatform input[data-fid="' + f.id + '"]');
      enabled[f.id] = cb ? cb.checked : true;
    });
    var user = el("faUser").value;
    if (user && !skipUser) state.config.users[user] = checkedIds(el("faUserBox"));
    api("POST", "/api/rbac/features", {
      enabled: enabled, roles: state.config.roles, users: state.config.users
    }).then(function () {
      toast(text || "功能与权限配置已保存", "success");
      msg("已保存。刷新页面后侧栏菜单按新配置显示。", true);
      renderBoxes();
    }).catch(function (e) { msg("保存失败：" + ((e && e.message) || e), false); });
  }

  function bind() {
    el("faAll").onclick = function () {
      root.querySelectorAll("#faPlatform input[data-fid]").forEach(function (c) {
        if (!c.disabled) c.checked = true;
      });
    };
    el("faMin").onclick = function () {
      root.querySelectorAll("#faPlatform input[data-fid]").forEach(function (c) {
        if (!c.disabled) c.checked = false;
      });
    };
    el("faRole").onchange = function () { syncCurrent(); renderBoxes(); };
    el("faUser").onchange = function () { syncCurrent(); renderBoxes(); };
    el("faSave").onclick = function () { save(); };
    el("faReset").onclick = function () {
      if (!window.confirm("恢复全部功能项启用并清除所有角色/用户分配？")) return;
      state.config = { enabled: {}, roles: {}, users: {} };
      allIds().forEach(function (id) { state.config.enabled[id] = true; });
      lastRole = el("faRole").value; lastUser = el("faUser").value;
      renderBoxes();
      save("已恢复全部启用");
    };
    el("faClearUser").onclick = function () {
      var u = el("faUser").value;
      if (!u) { msg("请先选择要清除覆盖的用户", false); return; }
      delete state.config.users[u];
      renderBoxes();
      save("已清除该用户覆盖", true);
    };
  }

  api("GET", "/api/rbac/features").then(function (d) {
    state = d;
    state.config = state.config || { enabled: {}, roles: {}, users: {} };
    state.config.roles = state.config.roles || {};
    state.config.users = state.config.users || {};
    renderShell();
    renderSelectors();
    renderBoxes();
    lastRole = el("faRole").value;
    lastUser = el("faUser").value;
    bind();
  }).catch(function (e) {
    root.innerHTML = '<div class="alert alert-danger">加载功能项配置失败：' +
      esc((e && e.message) || e) + '</div>';
  });
})();
