/**
 * 数据价值挖掘：资产盘点 / 敏感发现与分级 / 价值评估与治理 / 合规概览 / 脱敏导出。
 *
 * 约定：所有敏感值展示一律使用后端返回的**脱敏样例**，前端不再回显原文，
 * 避免"为治理敏感数据而新做一个泄漏面"。
 */
window.DataminingPage = (function () {
  var BKP = window.BKP;
  var esc = function (s) { return BKP.esc(s); };
  var sz = function (n) { return BKP.humanSize(n); };
  var state = { days: 30, inv: null, compliance: null, sensors: [], taskNames: {} };

  var LEVEL_BADGE = {
    4: '<span class="dm-lv dm-lv-4">L4 重要</span>',
    3: '<span class="dm-lv dm-lv-3">L3 敏感</span>',
    2: '<span class="dm-lv dm-lv-2">L2 内部</span>',
    1: '<span class="dm-lv dm-lv-1">L1 公开</span>',
    0: '<span class="dm-lv dm-lv-0">未识别</span>'
  };
  var COLD_COLOR = { hot: "#ef4444", warm: "#f59e0b", cold: "#0ea5a4", frozen: "#64748b" };
  var COLD_LABEL = { hot: "热", warm: "温", cold: "冷", frozen: "冰封" };

  function lvBadge(l) { return LEVEL_BADGE[l] || LEVEL_BADGE[0]; }

  function setHtml(id, html) {
    var el = document.getElementById(id);
    if (el) el.innerHTML = html;
  }
  function el(id) { return document.getElementById(id); }
  function pct(n) { return (n == null ? 0 : n); }
  function dash(s) { return (s === null || s === undefined || s === "") ? "—" : s; }

  async function api(method, url, body) {
    try { return await BKP.api(method, url, body); }
    catch (e) { BKP.toast("请求失败: " + e.message, "danger"); throw e; }
  }

  // ------------------------------------------------------------------ 通用图表
  /** 横向比例条 */
  function bar(label, value, total, color, right) {
    var w = total > 0 ? Math.max(2, Math.round(100.0 * value / total)) : 0;
    return '<div class="dm-bar-row">' +
      '<div class="dm-bar-label">' + esc(label) + '</div>' +
      '<div class="dm-bar-track"><div class="dm-bar-fill" style="width:' + w + '%;background:' +
      (color || "var(--teal)") + '"></div></div>' +
      '<div class="dm-bar-val">' + (right || "") + '</div>' +
      '</div>';
  }

  /** 近 N 天趋势折线（SVG，零依赖） */
  function trendSvg(points) {
    if (!points || !points.length) return '<div class="small text-secondary">暂无数据</div>';
    var W = 720, H = 180, pad = { l: 46, r: 12, t: 14, b: 22 };
    var max = Math.max.apply(null, points.map(function (p) { return p.bytes; })) || 1;
    var n = points.length;
    var x = function (i) { return pad.l + i * (W - pad.l - pad.r) / Math.max(1, n - 1); };
    var y = function (v) { return H - pad.b - (v / max) * (H - pad.t - pad.b); };
    var line = points.map(function (p, i) { return x(i).toFixed(1) + "," + y(p.bytes).toFixed(1); }).join(" ");
    var area = "M" + x(0).toFixed(1) + "," + (H - pad.b) + " L" + line.split(" ").join(" L") +
      " L" + x(n - 1).toFixed(1) + "," + (H - pad.b) + " Z";
    var grid = [0.25, 0.5, 0.75, 1].map(function (r) {
      var yy = y(max * r).toFixed(1);
      return '<line x1="' + pad.l + '" y1="' + yy + '" x2="' + (W - pad.r) + '" y2="' + yy +
        '" class="dm-grid"/><text x="6" y="' + (parseFloat(yy) + 4) + '" class="dm-axis">' +
        esc(sz(max * r)) + '</text>';
    }).join("");
    var xlab = "";
    var step = Math.max(1, Math.floor(n / 6));
    for (var i = 0; i < n; i += step) {
      xlab += '<text x="' + x(i).toFixed(1) + '" y="' + (H - 6) + '" class="dm-axis" text-anchor="middle">' +
        esc(points[i].date.slice(5)) + '</text>';
    }
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto">' +
      grid + xlab +
      '<path d="' + area + '" fill="rgba(13,148,136,.12)"/>' +
      '<polyline points="' + line + '" fill="none" stroke="var(--teal)" stroke-width="2"/>' +
      points.map(function (p, i) {
        return '<circle cx="' + x(i).toFixed(1) + '" cy="' + y(p.bytes).toFixed(1) +
          '" r="2.5" fill="var(--teal)"><title>' + esc(p.date) + " · " + esc(sz(p.bytes)) +
          " · " + p.count + ' 个批次</title></circle>';
      }).join("") +
      '</svg>';
  }

  /** 环形进度（conic-gradient） */
  function ring(score, id) {
    var c = score >= 85 ? "#16a34a" : (score >= 60 ? "#f59e0b" : "#dc2626");
    return '<div class="dm-ring" style="background:conic-gradient(' + c + ' ' + score +
      '% , rgba(148,163,184,.22) 0)"><span id="' + id + '">' + score + '</span></div>';
  }

  // ------------------------------------------------------------------ 资产盘点
  function renderKpi(s) {
    var growth = (s.growth_pct === null || s.growth_pct === undefined) ? "—" :
      (s.growth_pct > 0 ? "+" : "") + s.growth_pct + "%";
    var cards = [
      { t: "数据资产", v: s.asset_count, s: "备份任务维度" },
      { t: "数据库实例", v: s.instance_count, s: "去重 host:port" },
      { t: "累计备份容量", v: sz(s.total_bytes), s: "成功落盘产物" },
      { t: "统计周期新增", v: sz(s.recent_bytes), s: "环比 " + growth },
      { t: "受保护资产", v: s.protected_assets + "/" + s.asset_count,
        s: "已配置定时保护并启用" },
      { t: "平均价值分", v: s.avg_score, s: "0~100，六维加权" }
    ];
    setHtml("dmKpiRow", cards.map(function (c) {
      return '<div class="col-6 col-md-4 col-xl-2"><div class="page-card text-center py-3">' +
        '<div class="dm-kpi-v">' + esc(c.v) + '</div>' +
        '<div class="dm-kpi-t">' + esc(c.t) + '</div>' +
        '<div class="dm-kpi-s">' + esc(c.s) + '</div>' +
        '</div></div>';
    }).join(""));
  }

  function renderDist() {
    var inv = state.inv;
    // 冷热
    var cd = inv.cold_dist || {};
    var totC = (cd.hot || 0) + (cd.warm || 0) + (cd.cold || 0) + (cd.frozen || 0) || 1;
    setHtml("dmColdDist", ["hot", "warm", "cold", "frozen"].map(function (k) {
      return bar(COLD_LABEL[k] + "数据", cd[k] || 0, totC, COLD_COLOR[k],
        (cd[k] || 0) + " 个");
    }).join(""));
    // 存储层级
    var tiers = inv.tier_dist || [];
    var sumT = tiers.reduce(function (a, b) { return a + b.bytes; }, 0) || 1;
    setHtml("dmTierDist", tiers.length ? tiers.map(function (t) {
      return bar(t.label, t.bytes, sumT, "#0d9488", sz(t.bytes));
    }).join("") : '<div class="small text-secondary">暂无数据</div>');
    // 数据库类型
    var types = (inv.db_type_dist || []).slice(0, 8);
    var sumD = types.reduce(function (a, b) { return a + b.bytes; }, 0) || 1;
    setHtml("dmTypeDist", types.length ? types.map(function (t) {
      return bar((t.db_type || "").toUpperCase() + " · " + t.asset_count + " 资产",
        t.bytes, sumD, "#2f6f8f", sz(t.bytes));
    }).join("") : '<div class="small text-secondary">暂无数据</div>');
    // 实例
    var insts = (inv.instances || []).slice(0, 10);
    var sumI = insts.reduce(function (a, b) { return a + b.bytes; }, 0) || 1;
    setHtml("dmInstanceDist", insts.length ? insts.map(function (i) {
      return bar(i.instance + " · " + i.db_types.join("/").toUpperCase(),
        i.bytes, sumI, "#7c3aed", sz(i.bytes));
    }).join("") : '<div class="small text-secondary">暂无数据</div>');
    // 趋势
    setHtml("dmTrend", trendSvg(inv.trend));
  }

  function gradeBadge(score) {
    var cls = score >= 75 ? "dm-badge-success" : (score >= 50 ? "dm-badge-warn" : "dm-badge-muted");
    return '<span class="dm-badge ' + cls + '">' + score + "</span>";
  }
  function coldBadge(a) {
    return '<span class="dm-badge dm-cold-' + a.cold_level + '">' + esc(a.cold_label) + "</span>";
  }

  function renderAssetTable() {
    var kw = (el("dmAssetSearch").value || "").toLowerCase();
    var rows = (state.inv.assets || []).filter(function (a) {
      if (!kw) return true;
      return (a.name + " " + a.instance + " " + (a.biz_system || "") + " " + a.db_type)
        .toLowerCase().indexOf(kw) >= 0;
    });
    var pgA = Pager.slice("dmAssets", rows);
    setHtml("dmAssetTable", pgA.list.length ? pgA.list.map(function (a) {
      return "<tr>" +
        "<td><b>" + esc(a.name) + "</b>" +
        (a.biz_system ? '<div class="dm-sub">' + esc(a.biz_system) + "</div>" : "") + "</td>" +
        "<td>" + esc((a.db_type || "").toUpperCase()) +
        '<div class="dm-sub">' + esc(a.backup_type) + "</div></td>" +
        "<td>" + esc(a.instance || "—") + '<div class="dm-sub">' + esc(a.database || "") + "</div></td>" +
        '<td class="text-end">' + esc(sz(a.total_bytes)) + "</td>" +
        '<td class="text-end">' + a.record_count + "</td>" +
        "<td>" + esc(BKP.fmtTime(a.last_success_at)) +
        '<div class="dm-sub">' + (a.idle_days === null ? "无成功备份" : a.idle_days + " 天前") + "</div></td>" +
        '<td class="text-end">' + gradeBadge(a.value_score) + "</td>" +
        "<td>" + coldBadge(a) + "</td>" +
        "<td>" + lvBadge(a.sensitive_level) + "</td>" +
        '<td><button class="btn btn-xs btn-outline-secondary dm-do-scan" data-id="' + a.task_id + '">' +
        '<i class="bi bi-shield-check"></i> 扫描</button></td>' +
        "</tr>";
    }).join("") : '<tr><td colspan="10" class="text-center text-secondary py-3">暂无资产数据</td></tr>');
    Pager.attach("dmAssets", document.getElementById("dmAssetTable"), pgA.page, pgA.pages, pgA.total, loadInventory);
  }

  // ------------------------------------------------------------------ 价值评估
  function renderValue() {
    var inv = state.inv;
    var g = inv.grade_dist || {};
    var tot = (g["高价值"] || 0) + (g["中价值"] || 0) + (g["低价值"] || 0) || 1;
    setHtml("dmGradeDist", [
      bar("高价值 ≥75", g["高价值"] || 0, tot, "#16a34a", g["高价值"] || 0),
      bar("中价值 50~74", g["中价值"] || 0, tot, "#f59e0b", g["中价值"] || 0),
      bar("低价值 <50", g["低价值"] || 0, tot, "#94a3b8", g["低价值"] || 0)
    ].join(""));

    // 治理建议
    var acts = (inv.governance && inv.governance.actions) || [];
    setHtml("dmGovernance", acts.length ? acts.map(function (a) {
      return '<div class="dm-act dm-act-' + a.level + '">' +
        '<div class="dm-act-head"><b>' + esc(a.title) + "</b>" +
        (a.impact_bytes ? '<span class="dm-badge dm-badge-success ms-2">预计释放 ' +
          esc(sz(a.impact_bytes)) + "</span>" : "") + "</div>" +
        '<div class="dm-act-body">' + esc(a.reason) + "</div>" +
        '<div class="dm-act-list">' + (a.assets || []).slice(0, 6).map(function (x) {
          return '<span class="dm-chip">' + esc(x.name || x.checksum || ("#" + (x.task_id || ""))) +
            (x.idle_days !== undefined ? " · " + x.idle_days + "天未更新" : "") +
            (x.bytes ? " · " + esc(sz(x.bytes)) : "") + "</span>";
        }).join("") + "</div></div>";
    }).join("") : '<div class="small text-secondary">未发现需要治理的项</div>');

    // 价值明细
    var rows = (inv.assets || []).slice().sort(function (a, b) { return b.value_score - a.value_score; });
    var pgV = Pager.slice("dmValue", rows);
    setHtml("dmValueTable", pgV.list.length ? pgV.list.map(function (a) {
      var d = a.dims || {};
      var dims = [
        ["活跃", d.activity, 25], ["保护", d.protection, 20], ["规模", d.scale, 15],
        ["恢复", d.recoverability, 15], ["业务", d.business, 15], ["合规", d.compliance, 10]
      ];
      var dimHtml = '<div class="dm-dims">' + dims.map(function (x) {
        var w = Math.round(100.0 * (x[1] || 0) / x[2]);
        return '<span class="dm-dim" title="' + x[0] + " " + x[1] + "/" + x[2] + '">' +
          '<span class="dm-dim-fill" style="width:' + w + '%"></span>' +
          '<span class="dm-dim-t">' + x[0] + "</span></span>";
      }).join("") + "</div>";
    Pager.attach("dmValue", document.getElementById("dmValueTable"), pgV.page, pgV.pages, pgV.total, renderValue);
      var advice = [];
      if (a.cold_level === "frozen") advice.push("建议归档或清理");
      else if (a.cold_level === "cold") advice.push("可降级冷存储");
      if (!a.scheduled) advice.push("补定时保护");
      if (a.record_count && !a.drill_count && !a.restore_count) advice.push("补恢复演练");
      if ((a.sensitive_level || 0) >= 3) advice.push("外发前必须脱敏");
      return "<tr><td><b>" + esc(a.name) + "</b></td>" +
        '<td class="text-end">' + gradeBadge(a.value_score) + "</td>" +
        "<td>" + dimHtml + "</td>" +
        "<td>" + coldBadge(a) + "</td>" +
        '<td class="text-end">' + dash(a.idle_days) + "</td>" +
        '<td class="text-end">' + esc(sz(a.total_bytes)) + "</td>" +
        '<td class="small">' + (advice.length ? esc(advice.join(" · ")) :
          '<span class="text-secondary">保持现状</span>') + "</td></tr>";
    }).join("") : '<tr><td colspan="7" class="text-center text-secondary py-3">暂无数据</td></tr>');
  }

  // ------------------------------------------------------------------ 敏感发现
  function renderSensors() {
    setHtml("dmSensorTable", state.sensors.map(function (s) {
      return "<tr><td><b>" + esc(s.label) + "</b>" +
        '<div class="dm-sub">' + esc(s.why) + "</div></td>" +
        "<td>" + lvBadge(s.level) + "</td>" +
        "<td>" + esc(s.category) + "</td>" +
        "<td>" + (s.compliance || []).map(function (c) {
          return '<span class="dm-tag">' + esc(c) + "</span>";
        }).join(" ") + "</td>" +
        "<td>" + (s.validator ? '<span class="dm-badge dm-badge-success">校验算法</span>' :
          '<span class="dm-badge dm-badge-muted">正则</span>') + "</td>" +
        "<td>" + esc(maskLabel(s.mask)) + "</td></tr>";
    }).join(""));
  }
  function maskLabel(m) {
    return { mask: "打码保留首尾", hash: "哈希映射", fake: "仿真替换",
      generalize: "泛化降精度", drop: "直接丢弃", none: "不处理" }[m] || m;
  }

  function findingsHtml(findings) {
    if (!findings || !findings.length) {
      return '<div class="dm-ok"><i class="bi bi-check-circle"></i> 未发现敏感数据</div>';
    }
    return '<div class="table-responsive"><table class="table table-sm align-middle">' +
      '<thead><tr><th>类别</th><th>分级</th><th>合规</th><th class="text-end">命中</th>' +
      '<th class="text-end">置信度</th><th>脱敏样例</th><th>建议</th></tr></thead><tbody>' +
      findings.map(function (f) {
        return "<tr><td><b>" + esc(f.label) + "</b><div class='dm-sub'>" + esc(f.category) +
          "</div></td><td>" + lvBadge(f.level) + "</td><td>" +
          (f.compliance || []).map(function (c) { return '<span class="dm-tag">' + esc(c) + "</span>"; }).join(" ") +
          '</td><td class="text-end">' + f.hits + '</td>' +
          '<td class="text-end">' + Math.round((f.confidence || 0) * 100) + "%</td>" +
          '<td class="dm-mono">' + (f.samples || []).slice(0, 3).map(function (s) {
            return '<span class="dm-sample">' + esc(s) + "</span>";
          }).join(" ") + "</td>" +
          "<td>" + esc(maskLabel(f.suggested_mask)) + "</td></tr>";
      }).join("") + "</tbody></table></div>";
  }

  /** 结果区 + 「生成建议脱敏规则」按钮（把发现→治理闭环接通） */
  function withApply(summary, r, tag) {
    state["last_" + tag] = r;
    return '<div class="dm-scan-sum">' + summary + " · 最高等级 " + lvBadge(r.max_level) +
      " · 风险分 " + r.risk_score + "</div>" +
      ((r.findings || []).length ?
        '<div class="mb-2"><button class="btn btn-sm btn-outline-primary dm-apply" data-tag="' +
        tag + '"><i class="bi bi-magic"></i> 生成建议脱敏规则</button>' +
        '<span class="small text-secondary ms-2">按识别出的敏感类别生成 {类别: 动作} 规则，' +
        "带入「脱敏导出」后可按列名微调</span></div>" : "") +
      findingsHtml(r.findings);
  }

  function applyRules(tag) {
    var r = state["last_" + tag];
    if (!r || !r.findings) { BKP.toast("暂无可用的扫描结果", "warning"); return; }
    var rules = {};
    r.findings.forEach(function (f) { rules[f.type] = f.suggested_mask; });
    var ta = el("dmMaskRules");
    if (ta) ta.value = JSON.stringify(rules, null, 2);
    var btn = document.querySelector('[data-bs-target="#dmExport"]');
    if (btn) btn.click();
    BKP.toast("已将建议规则带入「脱敏导出」，请按列名微调后导出", "success");
  }

  async function trialScan() {
    var txt = el("dmTrialText").value || "";
    if (!txt.trim()) { BKP.toast("请先粘贴需要试扫的文本", "warning"); return; }
    el("dmTrialBtn").disabled = true;
    try {
      var r = await api("POST", "/api/datamining/scan", { text: txt });
      setHtml("dmTrialResult", withApply("扫描 " + sz(r.scanned_chars), r, "trial"));
    } finally { el("dmTrialBtn").disabled = false; }
  }

  async function scanTask(taskId, limit) {
    if (!taskId) { BKP.toast("请先选择资产", "warning"); return; }
    el("dmScanBtn").disabled = true;
    BKP.toast("正在采样扫描备份产物…", "info");
    try {
      var r = await api("POST", "/api/datamining/scan", { task_id: +taskId, limit: +limit });
      setHtml("dmScanResult",
        withApply("完成 " + r.scanned_files + "/" + r.total_files +
          " 份产物 · 命中 " + r.total_hits + " 处", r, "scan"));
      await loadInventory();   // 扫描结果会回填到资产盘点与合规概览
      await loadScans();
    } finally { el("dmScanBtn").disabled = false; }
  }

  async function loadScans() {
    var rows = await api("GET", "/api/datamining/scans");
    var pgS = Pager.slice("dmScans", rows);
    setHtml("dmScanHistory", pgS.list.length ? pgS.list.map(function (r) {
      return "<tr><td>" + esc(BKP.fmtTime(r.scanned_at)) + "</td>" +
        "<td>" + esc(state.taskNames[r.task_id] || ("#" + (r.task_id || "—"))) + "</td>" +
        "<td class='dm-mono small'>" + esc((r.path || "").split("/").pop()) + "</td>" +
        '<td class="text-end">' + esc(sz(r.size_bytes)) + "</td>" +
        "<td>" + lvBadge(r.max_level) + "</td>" +
        '<td class="text-end">' + r.risk_score + "</td>" +
        '<td class="text-end">' + r.hit_count + "</td>" +
        '<td><button class="btn btn-xs btn-outline-danger dm-del-scan" data-id="' + r.id + '">删除</button></td>' +
        "</tr>";
    }).join("") : '<tr><td colspan="8" class="text-center text-secondary py-3">' +
      "尚无扫描记录，可在右侧选择资产执行敏感发现</td></tr>");
    Pager.attach("dmScans", document.getElementById("dmScanHistory"), pgS.page, pgS.pages, pgS.total, loadScans);
  }

  // ------------------------------------------------------------------ 合规概览
  function renderCompliance() {
    var d = state.compliance;
    var ringBox = el("dmCompRing");
    if (ringBox) {
      ringBox.style.background = "conic-gradient(" +
        (d.score >= 85 ? "#16a34a" : (d.score >= 60 ? "#f59e0b" : "#dc2626")) + " " +
        d.score + "%, rgba(148,163,184,.22) 0)";
    }
    setHtml("dmCompScore", d.score);
    setHtml("dmCompGrade", d.grade);

    setHtml("dmCompChecks", (d.checks || []).map(function (c) {
      var badge = { pass: '<span class="dm-badge dm-badge-success">通过</span>',
        warn: '<span class="dm-badge dm-badge-warn">待改进</span>',
        fail: '<span class="dm-badge dm-badge-danger">不合规</span>',
        unknown: '<span class="dm-badge dm-badge-muted">未评估</span>' }[c.status] || "";
      return '<div class="dm-check dm-check-' + c.status + '">' +
        '<div class="dm-check-head"><b>' + esc(c.name) + "</b>" + badge +
        '<span class="dm-tag">' + esc(c.standard) + "</span>" +
        (c.score === null ? "" : '<span class="ms-auto fw-bold">' + c.score + "</span>") +
        "</div>" +
        '<div class="dm-check-detail">' + esc(c.detail) + "</div>" +
        '<div class="dm-check-advice"><i class="bi bi-lightbulb"></i> ' + esc(c.advice) + "</div>" +
        "</div>";
    }).join(""));

    setHtml("dmSensTable", (d.sensitive_assets || []).length ?
      d.sensitive_assets.map(function (a) {
        return "<tr><td><b>" + esc(a.name) + "</b></td><td>" + esc(a.db_type.toUpperCase()) + "</td>" +
          "<td>" + lvBadge(a.sensitive_level) + "</td>" +
          '<td class="text-end">' + a.risk_score + "</td>" +
          "<td>" + (a.protected ? '<span class="dm-badge dm-badge-success">已保护</span>' :
            '<span class="dm-badge dm-badge-danger">未保护</span>') + "</td>" +
          "<td>" + (a.drill_count ? a.drill_count + " 次" : '<span class="text-secondary">无</span>') + "</td>" +
          '<td class="text-end">' + esc(sz(a.total_bytes)) + "</td></tr>";
      }).join("") : '<tr><td colspan="7" class="text-center text-secondary py-3">' +
        "尚未发现 L3 及以上敏感资产，建议先执行敏感发现</td></tr>");

    setHtml("dmOverdue", (d.overdue || []).length ? (d.overdue).map(function (o) {
      return '<div class="dm-row-item"><span>' + esc(o.name) + "</span>" +
        '<span class="dm-muted small">超期 ' + o.overdue_count + " 份 · " +
        esc(sz(o.bytes)) + " · 保留 " + o.retention_days + " 天</span></div>";
    }).join("") : '<div class="small text-secondary">无超期留存</div>');

    setHtml("dmLoose", (d.loose_exports || []).length ? (d.loose_exports).map(function (l) {
      return '<div class="dm-row-item"><span>导出 #' + l.export_id + "</span>" +
        '<span class="dm-muted small">' + (l.columns || []).map(esc).join("、") + "</span></div>";
    }).join("") : '<div class="small text-secondary">无宽松导出规则</div>');
  }

  // ------------------------------------------------------------------ 数据装载
  async function loadInventory() {
    var d = await api("GET", "/api/datamining/inventory?days=" + state.days);
    state.inv = d;
    (d.assets || []).forEach(function (a) { state.taskNames[a.task_id] = a.name; });
    renderKpi(d.summary);
    renderDist();
    renderAssetTable();
    renderValue();
    // 扫描下拉
    var sel = el("dmScanTask");
    if (sel) {
      var keep = sel.value;
      sel.innerHTML = '<option value="">选择资产（备份任务）…</option>' +
        (d.assets || []).filter(function (a) { return a.record_count > 0; }).map(function (a) {
          return '<option value="' + a.task_id + '">' + esc(a.name) +
            " · " + esc(a.db_type.toUpperCase()) + " · " + esc(sz(a.total_bytes)) + "</option>";
        }).join("");
      sel.value = keep;
    }
    setHtml("dmUpdated", "盘点时间 " + new Date().toLocaleString("zh-CN"));
  }

  async function loadCompliance() {
    state.compliance = await api("GET", "/api/datamining/compliance?days=" + state.days);
    renderCompliance();
  }

  // ------------------------------------------------------------------ 事件绑定
  function bind() {
    el("dmRefresh").addEventListener("click", async function () {
      await loadInventory(); await loadCompliance(); await loadScans();
      BKP.toast("盘点完成", "success");
    });
    el("dmDays").addEventListener("change", async function () {
      state.days = +el("dmDays").value;
      await loadInventory(); await loadCompliance();
    });
    el("dmAssetSearch").addEventListener("input", renderAssetTable);
    el("dmTrialBtn").addEventListener("click", trialScan);
    el("dmScanBtn").addEventListener("click", function () {
      scanTask(el("dmScanTask").value, +el("dmScanLimit").value);
    });
    el("dmScanAllBtn").addEventListener("click", async function () {
      var assets = (state.inv.assets || []).filter(function (a) {
        return a.record_count > 0 && !a.sensitive_level;
      });
      if (!assets.length) { BKP.toast("所有有数据的资产均已完成扫描", "info"); return; }
      BKP.toast("将依次扫描 " + assets.length + " 个未覆盖资产", "info");
      for (var i = 0; i < assets.length; i++) {
        try { await api("POST", "/api/datamining/scan", { task_id: assets[i].task_id, limit: 1 }); }
        catch (e) { /* 单个失败不中断 */ }
      }
      await loadInventory(); await loadCompliance(); await loadScans();
      BKP.toast("批量扫描完成", "success");
    });
    document.addEventListener("click", async function (ev) {
      var ap = ev.target.closest(".dm-apply");
      if (ap) { applyRules(ap.dataset.tag); return; }
      var t = ev.target.closest(".dm-do-scan");
      if (t) {
        document.querySelector('[data-bs-target="#dmSensitive"]').click();
        el("dmScanTask").value = t.dataset.id;
        await scanTask(t.dataset.id, 1);
        return;
      }
      var d = ev.target.closest(".dm-del-scan");
      if (d) {
        await api("DELETE", "/api/datamining/scans/" + d.dataset.id);
        await loadScans(); await loadInventory(); await loadCompliance();
      }
    });
  }

  // ------------------------------------------------------------------ 入口
  /** 分段容错：任一数据源异常不影响其余看板渲染 */
  async function safe(name, fn) {
    try { await fn(); }
    catch (e) { console.error("[datamining] " + name + " 失败", e); BKP.toast(name + "加载失败：" + e.message, "danger"); }
  }

  async function init() {
    if (!document.body || document.body.dataset.page !== "datamining") return;
    bind();
    await safe("识别能力", async function () {
      state.sensors = await api("GET", "/api/datamining/sensors");
      renderSensors();
    });
    await safe("资产盘点", loadInventory);
    await safe("扫描历史", loadScans);
    await safe("合规概览", loadCompliance);
  }

  return { init: init };
})();
