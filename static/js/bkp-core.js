// -*- coding: utf-8 -*-
// 备份管理平台 - 核心 JS 工具库（所有页面共享）
// 通过 window.BKP 命名空间暴露给 app.js 和各功能模块
"use strict";

window.BKP = (function () {
  const BKP = {};

  // ---- 安全的 DOM 获取 ----
  BKP.$ = function (id) {
    const el = document.getElementById(id);
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
        if (p === "addEventListener" || p === "removeEventListener" || p === "setAttribute" || p === "dispatchEvent" || p === "click" || p === "focus" || p === "reset" || p === "show" || p === "hide" || p === "querySelectorAll" || p === "querySelector") return function(){};
        if (typeof p === "string" && /^(on|set|get)/.test(p)) return function(){};
        return t[p];
      },
      set: function(t, p, v) { return true; }
    });
  };

  // ---- 安全属性获取（ES5 兼容）----
  BKP.$safe = function (id) {
    var el = document.getElementById(id);
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
  BKP.META = { db_types: [], display_names: {}, default_ports: {}, demo_mode: "auto", scheduler_enabled: true };

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
      never: ["bg-secondary", "未运行"]
    };
    var pair = m[s] || ["bg-secondary", s || "-"];
    return '<span class="badge ' + pair[0] + '">' + pair[1] + '</span>';
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
  BKP.fillDbTypeSelect = function (sel, exclude) {
    exclude = exclude || [];
    sel.innerHTML = BKP.META.db_types
      .filter(function (t) { return !exclude.includes(t); })
      .map(function (t) { return '<option value="' + t + '">' + BKP.esc(BKP.META.display_names[t] || t) + '</option>'; }).join("");
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
