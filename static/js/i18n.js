/* =============================================================
   AIDBM i18n 轻量框架（v1，零构建、完全离线可用）
   - 词典以「中文原文」为 key，映射英文译文；zh 模式原样返回
   - 覆盖：侧边栏菜单（.nav-label 按文本映射）、[data-i18n] 静态文案、
     [data-i18n-ph] 占位符、状态徽章（bkp-core statusBadge 运行时经 T()）
   - 语言存 localStorage("aidbm_lang")，切换按钮 #langToggle 切换后刷新
   - v1 范围：导航 + 数据库备份页 + 通用状态；其余页面逐页补 data-i18n
   ============================================================= */
window.I18N = (function () {
  var EN = {
    // ---- 侧边栏菜单 ----
    "仪表盘": "Dashboard",
    "数据库备份": "Database Backup",
    "文件备份": "File Backup",
    "对象存储备份": "Object Storage",
    "存储管理": "Storage",
    "备份策略": "Backup Policies",
    "无 Agent": "Agentless",
    "备份插件": "Plugins",
    "实时备份": "Realtime Backup",
    "数据恢复": "Restore",
    "恢复校验": "Restore Verification",
    "数据对比": "Data Compare",
    "数据库部署": "DB Deployment",
    "备份记录": "Backup Records",
    "恢复记录": "Restore Records",
    "数据迁移": "Migration",
    "数据同步": "Data Sync",
    "容灾链路": "DR Links",
    "克隆服务": "Clone",
    "虚拟机备份": "VM Backup",
    "运维运营分析": "Operations",
    "巡检": "Inspection",
    "AI 智能助手": "AI Assistant",
    "智能告警": "Alerts",
    "数据价值挖掘": "Data Mining",
    "系统日志": "System Logs",
    "系统设置": "Settings",
    "用户管理": "Users",
    "退出登录": "Logout",
    // ---- 数据库备份页 ----
    "配置数据库连接、备份类型与调度策略": "Configure connections, backup types and schedules",
    "新建任务": "New Task",
    "定期清理": "Retention Cleanup",
    "刷新": "Refresh",
    "导入": "Import",
    "模板": "Template",
    "立即备份": "Backup Now",
    "编辑": "Edit",
    "删除": "Delete",
    "业务系统": "Business System",
    "名称": "Name",
    "类型": "Type",
    "备份 IP": "Backup IP",
    "端口/库": "Port/DB",
    "备份类型": "Backup Type",
    "备份模式": "Backup Mode",
    "调度": "Schedule",
    "状态": "Status",
    "上次运行": "Last Run",
    "操作": "Actions",
    "任务总数": "Tasks",
    "已启用": "Enabled",
    "最近成功": "Last Success",
    "最近失败": "Last Failed",
    "执行中": "Running",
    "还没有备份任务": "No backup tasks yet",
    "点击右上角「新建任务」，一分钟配置第一个数据库备份":
      "Click \"New Task\" on the top right to create your first database backup in a minute",
    // ---- 通用状态 ----
    "成功": "Success",
    "失败": "Failed",
    "运行中": "Running",
    "仿真": "Simulated",
    "未运行": "Never Run",
    "已清理（过期）": "Expired (Cleaned)",
    "已清理（GFS 过期）": "Expired (GFS)",
    "已停用": "Disabled",
    "取消": "Cancel",
    "确认": "Confirm"
  };

  var lang = localStorage.getItem("aidbm_lang") || "zh";

  function t(s) {
    if (s === null || s === undefined) return s;
    s = String(s);
    if (lang === "zh") return s;
    return EN[s] || s;
  }

  function apply(root) {
    root = root || document;
    if (lang !== "en") return;
    root.querySelectorAll("[data-i18n]").forEach(function (el) {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    root.querySelectorAll("[data-i18n-ph]").forEach(function (el) {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-ph")));
    });
    // 侧边栏菜单：按中文文本映射（菜单由 Jinja 渲染，不加 data-i18n）
    root.querySelectorAll(".nav-label").forEach(function (el) {
      var zh = (el.textContent || "").trim();
      if (EN[zh]) el.textContent = EN[zh];
    });
  }

  function setLang(l) {
    lang = l;
    localStorage.setItem("aidbm_lang", l);
    location.reload();
  }

  // 语言切换按钮（侧边栏）
  document.addEventListener("DOMContentLoaded", function () {
    apply();
    var btn = document.getElementById("langToggle");
    if (btn) {
      var label = document.getElementById("langLabel");
      var refresh = function () {
        if (label) label.textContent = (lang === "zh" ? "English" : "中文");
      };
      refresh();
      btn.addEventListener("click", function () {
        setLang(lang === "zh" ? "en" : "zh");
      });
    }
  });

  return {
    t: t,
    apply: apply,
    setLang: setLang,
    get lang() { return lang; }
  };
})();
window.T = function (s) { return window.I18N.t(s); };
