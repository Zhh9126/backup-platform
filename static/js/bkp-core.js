// -*- coding: utf-8 -*-
// AIDBM（AI 原生智能数据库灾备管理平台）- 核心 JS 工具库（所有页面共享）
// 通过 window.BKP 命名空间暴露给 app.js 和各功能模块
"use strict";

window.BKP = (function () {
  const BKP = {};

  // ---- 安全的 DOM 获取 ----
  // 兼容两种写法：BKP.$("myId") 与 BKP.$("#myId")。
  // 历史实现只认纯 id，传 "#xxx" 时 getElementById 必然取不到元素，
  // 回退的 Proxy 又没有 appendChild，页面就会报 "tb.appendChild is not a function"。
  function _byId(id) {
    const key = String(id == null ? "" : id).trim().replace(/^#/, "");
    return document.getElementById(key);
  }
  BKP.$ = function (id) {
    const el = _byId(id);
    if (el) return el;
    return new Proxy({}, {
      get(t, p) {
        if (p === "value") return "";
        if (p === "textContent" || p === "innerHTML") return "";
        if (p === "checked") return false;
        if (p === "dataset") return {};
        if (p === "style") return {};
        if (p === "classList") return { add: function(){}, remove: function(){}, toggle: function(){}, contains: function(){return false;} };
        if (p === "files") return [];
        if (p === "children" || p === "parentNode") return [];
        if (p === "addEventListener" || p === "removeEventListener" || p === "setAttribute" || p === "dispatchEvent" || p === "click" || p === "focus" || p === "reset" || p === "show" || p === "hide") return function(){};
        // 查询方法必须返回「可继续使用的空值」而不是 function(){}（返回 undefined）：
        // 否则 el.querySelectorAll(...).forEach / el.querySelectorAll(...).length 会抛
        // "Cannot read properties of undefined"，静默打断整个调用链（如 组合任务编辑弹窗打不开）。
        if (p === "querySelectorAll") return function(){ return []; };
        if (p === "querySelector") return function(){ return null; };
        // 文档结构操作方法：元素缺失时静默无操作，避免 "xxx is not a function" 打断整个页面逻辑
        if (p === "appendChild" || p === "append" || p === "insertBefore" || p === "removeChild" || p === "remove" || p === "replaceChildren") return function(){};
        if (p === "closest") return function(){ return null; };
        if (p === "contains") return function(){ return false; };
        if (p === "getAttribute") return function(){ return null; };
        if (typeof p === "string" && /^(on|set|get)/.test(p)) return function(){};
        return t[p];
      },
      set: function(t, p, v) { return true; }
    });
  };

  // ---- 安全属性获取（ES5 兼容）----
  BKP.$safe = function (id) {
    var el = _byId(id);
    if (el) return el;
    return {
      value: "", textContent: "", innerHTML: "", checked: false, dispatchEvent: function(){},
      classList: { toggle: function(){}, add: function(){}, remove: function(){}, contains: function(){return false;} },
      addEventListener: function(){}, removeEventListener: function(){},
      querySelectorAll: function(){return [];}, querySelector: function(){return null;}, setAttribute: function(){},
      style: {}, dataset: {}
    };
  };

  // ---- 全局状态 ----
  // 服务端渲染时已把 META 内联进页面（base.html 的 window.__BKP_META__），
  // 见 app.py 的 context_processor：这样首屏脚本（如 sync.js）立刻就能拿到
  // 数据库类型清单，不再依赖「/api/meta 先返回」这个时序假设。
  BKP.META = { db_types: [], display_names: {}, default_ports: {}, demo_mode: "auto", scheduler_enabled: true };
  if (typeof window !== "undefined" && window.__BKP_META__) {
    try { BKP.META = Object.assign(BKP.META, window.__BKP_META__); } catch (e) { /* ignore */ }
  }

  // ---- 元信息就绪保证 ----
  // 所有依赖 META 的渲染都应先 await 它：首屏已注入时立即返回；缺失时按需拉
  // /api/meta（并发去重），失败也不抛错（避免把页面交互整体打断）。
  BKP.ensureMeta = function () {
    if (BKP.META.db_types && BKP.META.db_types.length) {
      return Promise.resolve(BKP.META);
    }
    if (!BKP._metaPromise) {
      BKP._metaPromise = fetch("/api/meta", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (m) {
          if (m) BKP.META = Object.assign(BKP.META, m);
          return BKP.META;
        })
        .catch(function () { return BKP.META; });
    }
    return BKP._metaPromise;
  };

  // ---- API 调用封装 ----
  BKP.api = async function (method, url, body) {
    var opt = { method: method, headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    var resp = await fetch(url, opt);
    var data = null;
    try { data = await resp.json(); } catch (e) { /* ignore */ }
    if (!resp.ok) throw new Error((data && data.error) || ("请求失败 HTTP " + resp.status));
    return data;
  };

  // ---- HTML 转义 ----
  BKP.esc = function (s) {
    s = String(s == null ? "" : s);
    return s.replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  };

  // ---- 时间格式化 ----
  BKP.fmtTime = function (iso) {
    return iso ? iso.replace("T", " ").slice(0, 19) : "-";
  };

  // ---- 时长格式化（秒 -> HH:MM:SS / MM:SS）----
  BKP.fmtDuration = function (sec) {
    sec = Number(sec) || 0;
    if (sec <= 0) return "0s";
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = Math.floor(sec % 60);
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    if (h > 0) return h + "h" + pad(m) + "m" + pad(s) + "s";
    if (m > 0) return m + "m" + pad(s) + "s";
    return s + "s";
  };

  // ============== RBAC：权限缓存与可见性检查 ==============
  // 调用 BKP.loadPerms() 后 BKP.hasPerm('xxx.yyy') 返回布尔值。
  BKP._perms = null;
  BKP._permsBy = function () {
    if (BKP._perms) return Promise.resolve(BKP._perms);
    return fetch("/api/rbac/me", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (d) { BKP._perms = new Set(d.permissions || []); return BKP._perms; })
      .catch(function () { BKP._perms = new Set(); return BKP._perms; });
  };
  BKP.hasPerm = function (perm) {
    if (!BKP._perms) return false;
    return BKP._perms.has(perm);
  };
  BKP.applyMenuPerms = function (selector) {
    selector = selector || "[data-perm]";
    return BKP._permsBy().then(function () {
      var els = document.querySelectorAll(selector);
      var any = false;
      els.forEach(function (el) {
        var p = el.getAttribute("data-perm");
        if (p && !BKP.hasPerm(p)) {
          el.style.display = "none";
        } else if (p) {
          any = true;
        }
      });
      return any;
    });
  };

  // ---- 文件大小人类可读 ----
  BKP.humanSize = function (n) {
    if (n == null || n === 0) return "0 B";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(1) + " " + units[i];
  };

  // ---- 状态徽章 ----
  BKP.statusBadge = function (s) {
    var m = {
      success: ["badge-ok", "成功"], failed: ["badge-fail", "失败"],
      simulated: ["badge-sim", "仿真"], running: ["badge-run", "运行中"],
      never: ["bg-secondary", "未运行"],
      // 保留策略过期清理 / GFS 过期：留存审计轨迹（记录还在，产物已删）
      expired: ["bg-secondary", "已清理（过期）"],
      expired_cleanup: ["bg-secondary", "已清理（过期）"],
      expired_gfs: ["bg-secondary", "已清理（GFS 过期）"]
    };
    var pair = m[s] || ["bg-secondary", s || "-"];
    var text = (window.I18N && I18N.lang === "en") ? I18N.t(pair[1]) : pair[1];
    return '<span class="badge ' + pair[0] + '">' + text + '</span>';
  };

  // ---- Toast 通知 ----
  BKP.toast = function (msg, type, delay) {
    type = type || "dark";
    delay = delay || 3000;
    var el = BKP.$("toast");
    el.className = "toast align-items-center border-0" + (type === "danger" ? " text-bg-danger" : "");
    BKP.$("toastMsg").textContent = msg;
    var t = new bootstrap.Toast(el, { delay: delay });
    t.show();
  };

  // ---- 填充数据库类型下拉 ----
  // META 还没到时会自己补拉一次再填（历史缺陷：调用点执行得比 META 就绪早，
  // 下拉被填成空，用户「选不了数据库类型」）。options.onReady(sel, types)
  // 便于调用方在类型真正就绪后再设置默认值/联动端口。
  BKP.fillDbTypeSelect = function (sel, exclude, options) {
    if (!sel) return;
    exclude = exclude || [];
    options = options || {};
    var fill = function () {
      var dn = BKP.META.display_names || {};
      // typesKey 用于取不同场景的类型清单：备份任务用 db_types（含 file），
      // 数据同步/数据迁移用 sync_types（= 同步插件注册表，避免出现选不动的类型）
      var pool = BKP.META[options.typesKey || "db_types"] || [];
      if (!pool.length && options.typesKey) pool = BKP.META.db_types || [];
      var types = pool.filter(function (t) {
        return exclude.indexOf(t) < 0;
      });
      if (!types.length) return [];
      sel.innerHTML = types.map(function (t) {
        return '<option value="' + t + '">' + BKP.esc(dn[t] || t) + '</option>';
      }).join("");
      return types;
    };
    var types = fill();
    if (types.length) {
      if (typeof options.onReady === "function") options.onReady(sel, types);
      return;
    }
    BKP.ensureMeta().then(function () {
      var ts = fill();
      if (ts.length && typeof options.onReady === "function") options.onReady(sel, ts);
    });
  };

  return BKP;
})();

// ---- 全局错误捕获 ----
window.addEventListener("error", function (ev) {
  var msg = (ev && ev.error && ev.error.stack) || ev.message || "未知错误";
  console.error("[bkp error]", msg);
  try {
    var box = document.getElementById("jsErrorBox");
    if (box) {
      box.style.display = "block";
      box.textContent = "⚠ JS 错误: " + (typeof msg === "string" ? msg.split("\n")[0] : msg);
    }
  } catch (e) { /* ignore */ }
});
