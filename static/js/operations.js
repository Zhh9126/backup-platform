/* operations.js — 运维运营分析页：超长 / 超频备份统计 + 阈值配置 + Excel 导出 */
(function () {
  "use strict";
  const $    = (id) => document.getElementById(id);
  const api  = (m, u, b) => window.BKP.api(m, u, b);
  const esc  = (s) => window.BKP.esc(s);
  const toast = (msg, type, delay) => window.BKP.toast(msg, type, delay);
  // 防止未初始化到 BKP.META
  let META = (window.BKP && window.BKP.META) || { display_names: {} };

  async function ensureMeta() {
    try {
      const meta = await api("GET", "/api/meta");
      META = Object.assign({ display_names: {} }, meta);
      window.BKP.META = Object.assign(window.BKP.META || {}, meta);
    } catch (e) { /* 忽略 */ }
  }

  const dbTypeLabel = (t) =>
    (META.display_names && META.display_names[t]) || t || "—";

  function fmtDuration(sec) {
    if (sec == null) return "—";
    const s = Math.round(sec);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    if (h > 0) return `${h}h${m}m`;
    if (m > 0) return `${m}m${r}s`;
    return `${r}s`;
  }
  function fmtTime(iso) {
    if (!iso) return "—";
    try { return new Date(iso).toLocaleString("zh-CN", { hour12: false }); }
    catch (e) { return iso; }
  }
  function humanSize(n) {
    if (n == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let v = Number(n), i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  /* ---------- 加载主数据 ---------- */
  async function loadOperations() {
    await ensureMeta();
    let data;
    try {
      data = await api("GET", "/api/records/overrun-stats");
    } catch (e) {
      toast("加载运维运营数据失败：" + e.message, "danger");
      return;
    }
    renderStats(data);
    renderLongRunning(data.long_running || []);
    renderFrequency(data.frequency || []);
  }

  function renderStats(d) {
    $("st_long_running").textContent   = (d.long_running || []).length;
    $("st_frequency").textContent      = (d.frequency || []).length;
    $("st_total_records").textContent  = d.total_records || 0;

    const th = d.thresholds || {};
    const ruleLong = describeLongRule(th);
    const ruleFreq = describeFreqRule(th);
    $("ruleSummaryText").innerHTML =
      `<b>超长判定：</b>${esc(ruleLong)}　|　<b>超频判定：</b>${esc(ruleFreq)}`;
    $("longTableTitle").textContent  = ruleLong;
    $("freqTableTitle").textContent  = ruleFreq;
    $("longTitle").textContent = `超长备份 (${ruleLong})`;
    $("freqTitle").textContent = `超频备份 (${ruleFreq})`;
  }

  function describeLongRule(th) {
    const rule  = th.long_rule || "speed";
    const mins  = th.long_minutes || 60;
    const speed = th.expected_speed_gb_per_hour || 500;
    const tol   = th.speed_tolerance_pct != null ? th.speed_tolerance_pct : 20;
    if (rule === "fixed") return `固定超过 ${mins} 分钟`;
    if (rule === "both")  return `固定>${mins}分钟 或 实际耗时>数据量/${speed}GBh×(1+${tol}%)`;
    return `实际耗时 > 数据量/${speed}GBh × (1+${tol}%)`;
  }
  function describeFreqRule(th) {
    const win  = th.freq_window_minutes || 5;
    const cnt  = th.freq_count || 3;
    return `窗口 ${win} 分钟内同任务备份 ≥ ${cnt} 次`;
  }

  function renderLongRunning(rows) {
    const tb = $("longRunningTbody");
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="9" class="text-center text-muted py-3">暂无超长备份记录</td></tr>`;
      return;
    }
    tb.innerHTML = rows.map(r => `
      <tr>
        <td>${esc(r.id)}</td>
        <td>${fmtTime(r.started_at)}</td>
        <td>${esc(dbTypeLabel(r.db_type))}</td>
        <td>${esc(r.task_name || r.task_id || "—")}</td>
        <td>${esc(r.backup_type || "—")}</td>
        <td>${fmtDuration(r.duration)}</td>
        <td>${humanSize(r.size_bytes)}</td>
        <td>${esc(r.reason || "—")}</td>
        <td>${window.BKP.statusBadge ? window.BKP.statusBadge(r.status) : esc(r.status || "")}</td>
      </tr>`).join("");
  }

  function renderFrequency(rows) {
    const tb = $("frequencyTbody");
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="6" class="text-center text-muted py-3">暂无超频备份记录</td></tr>`;
      return;
    }
    tb.innerHTML = rows.map(g => `
      <tr>
        <td>${esc(g.task_name || g.task_id || "—")}</td>
        <td>${esc(dbTypeLabel(g.db_type))}</td>
        <td><span class="badge bg-danger">${esc(g.count)}</span></td>
        <td>${fmtTime(g.window_start)}</td>
        <td>${fmtTime(g.window_end)}</td>
        <td>${esc((g.record_ids || []).join(", "))}</td>
      </tr>`).join("");
  }

  /* ---------- 阈值 modal ---------- */
  async function openThresholdsModal() {
    await ensureMeta();
    let th = {};
    try { th = await api("GET", "/api/settings/backup-quality-thresholds"); }
    catch (e) { toast("读取阈值失败：" + e.message, "warning"); }
    $("th_long_rule").value      = th.long_rule || "speed";
    $("th_long_minutes").value   = th.long_minutes || 60;
    $("th_expected_speed").value = th.expected_speed_gb_per_hour || 500;
    $("th_tolerance").value      = th.speed_tolerance_pct != null ? th.speed_tolerance_pct : 20;
    $("th_freq_window").value    = th.freq_window_minutes || 5;
    $("th_freq_count").value     = th.freq_count || 3;
    updateThresholdsPreview();
    if (window.bootstrap) {
      const el = $("thresholdsModal");
      let inst = window.bootstrap.Modal.getInstance(el);
      if (!inst) inst = new window.bootstrap.Modal(el);
      inst.show();
    }
  }

  function collectThresholds() {
    return {
      long_rule: $("th_long_rule").value,
      long_minutes: Number($("th_long_minutes").value) || 60,
      expected_speed_gb_per_hour: Number($("th_expected_speed").value) || 500,
      speed_tolerance_pct: Number($("th_tolerance").value) || 0,
      freq_window_minutes: Number($("th_freq_window").value) || 5,
      freq_count: Number($("th_freq_count").value) || 3,
    };
  }
  function updateThresholdsPreview() {
    const th = collectThresholds();
    const longDesc = describeLongRule(th);
    const freqDesc = describeFreqRule(th);
    $("thPreview").innerHTML =
      `<b>超长：</b>${esc(longDesc)}<br><b>超频：</b>${esc(freqDesc)}`;
  }
  async function saveThresholds() {
    const th = collectThresholds();
    try {
      await api("POST", "/api/settings/backup-quality-thresholds", th);
      toast("阈值已保存并应用", "success");
      if (window.bootstrap) {
        const inst = window.bootstrap.Modal.getInstance($("thresholdsModal"));
        if (inst) inst.hide();
      }
      loadOperations();
    } catch (e) {
      toast("保存失败：" + e.message, "danger");
    }
  }

  /* ---------- Excel 导出（无第三方依赖，前端生成 xlsx） ---------- */
  async function exportExcel() {
    let data;
    try {
      data = await api("GET", "/api/records/overrun-stats");
    } catch (e) {
      toast("导出失败：" + e.message, "danger");
      return;
    }
    const wb = buildWorkbook(data);
    const blob = new Blob([s2ab(wb)], {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    a.download = `运维运营分析_${ts}.xlsx`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast("Excel 导出成功", "success");
  }

  /* 生成最小可用 xlsx（多 sheet，仅文本/数值） */
  function escXml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&apos;");
  }
  function colLetter(n) { // 0-based -> A, B, ... Z, AA
    let s = "";
    n += 1;
    while (n > 0) { const r = (n - 1) % 26; s = String.fromCharCode(65 + r) + s; n = Math.floor((n - 1) / 26); }
    return s;
  }
  function sheetXml(rows) {
    let body = "";
    rows.forEach((row, ri) => {
      let cells = "";
      row.forEach((cell, ci) => {
        const ref = colLetter(ci) + (ri + 1);
        let t = "inlineStr", val;
        if (typeof cell === "number") { t = "n"; val = String(cell); }
        else { val = escXml(cell); }
        cells += `<c r="${ref}" t="${t}">${t === "n" ? `<v>${val}</v>` : `<is><t xml:space="preserve">${val}</t></is>`}</c>`;
      });
      body += `<row r="${ri + 1}">${cells}</row>`;
    });
    return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>${body}</sheetData></worksheet>`;
  }
  function buildWorkbook(data) {
    const M = META;
    const dt = (iso) => iso ? new Date(iso).toLocaleString("zh-CN", { hour12: false }) : "";
    const hs = (t) => (M.display_names && M.display_names[t]) || t || "";

    // Sheet1 概览
    const overview = [
      ["指标", "数值"],
      ["超长备份数量", (data.long_running || []).length],
      ["超频备份数量", (data.frequency || []).length],
      ["总备份记录", data.total_records || 0],
      ["超长判定规则", describeLongRule(data.thresholds || {})],
      ["超频判定规则", describeFreqRule(data.thresholds || {})],
      ["导出时间", new Date().toLocaleString("zh-CN", { hour12: false })],
    ];

    // Sheet2 超长备份
    const longHeader = ["ID","备份时间","数据库类型","任务","备份类型","耗时(秒)","大小(字节)","超长原因","状态"];
    const longRows = (data.long_running || []).map(r => [
      r.id, dt(r.started_at), hs(r.db_type), r.task_name || r.task_id || "",
      r.backup_type || "", r.duration != null ? Number(r.duration) : "",
      r.size_bytes != null ? Number(r.size_bytes) : "", r.reason || "", r.status || ""
    ]);
    const longSheet = [longHeader, ...longRows];

    // Sheet3 超频备份
    const freqHeader = ["任务","数据库类型","次数","窗口开始","窗口结束","关联记录"];
    const freqRows = (data.frequency || []).map(g => [
      g.task_name || g.task_id || "", hs(g.db_type), Number(g.count),
      dt(g.window_start), dt(g.window_end), (g.record_ids || []).join(", ")
    ]);
    const freqSheet = [freqHeader, ...freqRows];

    const sheets = [
      { name: "概览", rows: overview },
      { name: "超长备份", rows: longSheet },
      { name: "超频备份", rows: freqSheet },
    ];
    const sheetTags = sheets.map((s, i) => `<sheet name="${escXml(s.name)}" sheetId="${i+1}" r:id="rId${i+1}"/>`).join("");
    const wbXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>${sheetTags}</sheets></workbook>`;
    const contentTypes = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
${sheets.map((_, i) => `<Override PartName="/xl/worksheets/sheet${i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`).join("")}
</Types>`;
    const rootRels = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>`;
    const wbRels = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
${sheets.map((_, i) => `<Relationship Id="rId${i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${i+1}.xml"/>`).join("")}
</Relationships>`;

    const parts = {
      "[Content_Types].xml": contentTypes,
      "_rels/.rels": rootRels,
      "xl/workbook.xml": wbXml,
      "xl/_rels/workbook.xml.rels": wbRels,
    };
    sheets.forEach((s, i) => { parts[`xl/worksheets/sheet${i+1}.xml`] = sheetXml(s.rows); });

    return buildZip(parts);
  }

  /* 极简 ZIP 写入（仅 store，无压缩，Excel 可接受） */
  function buildZip(parts) {
    const enc = new TextEncoder();
    const files = Object.keys(parts).map(name => ({ name, data: enc.encode(parts[name]) }));
    let offset = 0;
    const central = [];
    const chunks = [];
    const u16 = (n) => [n & 0xff, (n >> 8) & 0xff];
    const u32 = (n) => [n & 0xff, (n >> 8) & 0xff, (n >> 16) & 0xff, (n >> 24) & 0xff];
    const nameEnc = new TextEncoder();
    for (const f of files) {
      const nameBytes = nameEnc.encode(f.name);
      const crc = crc32(f.data);
      const local = [
        ...u32(0x04034b50), ...u16(20), ...u16(0), ...u16(0), ...u16(0),
        ...u16(0), ...u32(crc), ...u32(f.data.length), ...u32(f.data.length),
        ...u16(nameBytes.length), ...u16(0),
      ];
      chunks.push(new Uint8Array(local), nameBytes, f.data);
      const localLen = local.length + nameBytes.length + f.data.length;
      const c = [
        ...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(0), ...u16(0),
        ...u16(0), ...u16(0), ...u32(crc), ...u32(f.data.length), ...u32(f.data.length),
        ...u16(nameBytes.length), ...u16(0), ...u16(0), ...u16(0), ...u16(0),
        ...u32(0), ...u32(offset),
      ];
      central.push({ header: new Uint8Array(c), name: nameBytes });
      offset += localLen;
    }
    let centralSize = 0;
    const centralChunks = [];
    for (const item of central) { centralChunks.push(item.header, item.name); centralSize += item.header.length + item.name.length; }
    const end = [
      ...u32(0x06054b50), ...u16(0), ...u16(0),
      ...u16(files.length), ...u16(files.length),
      ...u32(centralSize), ...u32(offset), ...u16(0),
    ];
    const all = [...chunks, ...centralChunks, new Uint8Array(end)];
    // 拼接
    let total = 0; all.forEach(c => total += c.length);
    const out = new Uint8Array(total);
    let pos = 0;
    all.forEach(c => { out.set(c, pos); pos += c.length; });
    return out;
  }
  function crc32(buf) {
    let c = ~0;
    for (let i = 0; i < buf.length; i++) {
      c ^= buf[i];
      for (let k = 0; k < 8; k++) c = (c & 1) ? (c >>> 1) ^ 0xEDB88320 : (c >>> 1);
    }
    return (~c) >>> 0;
  }
  function s2ab(u8) { return u8; } // Blob 直接接受 Uint8Array

  /* ---------- 启动 ---------- */
  document.addEventListener("DOMContentLoaded", loadOperations);
  window.loadOperations = loadOperations;
  window.openThresholdsModal = openThresholdsModal;
  window.saveThresholds = saveThresholds;
  window.updateThresholdsPreview = updateThresholdsPreview;
  window.exportExcel = exportExcel;
})();
