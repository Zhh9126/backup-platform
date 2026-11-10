/* 防勒索态势页：零构建、原生 JS，完全离线可用。 */
(function () {
  "use strict";

  const CFG_LABELS = {
    ransomware_auto_scan: "备份成功自动安全检测（结论落库）",
    ransomware_block_suspect_restore: "恢复前安全门（拦截可疑 / 漂移 / 隔离产物）",
    ransomware_auto_quarantine: "可疑产物自动隔离 + 取证锁定 + 告警",
    ransomware_keep_last_clean: "每任务保留最后一个可信恢复点（禁止清理）",
    ransomware_alert_webhook: "安全事件推送 Webhook / 审计日志",
    ransomware_fs_immutable: "启用内核 immutable（chattr +i，需能力可用）",
  };
  const EVENT_NAMES = {
    ransomware_suspect: "检测到可疑特征",
    quarantine: "产物已隔离",
    quarantine_released: "解除隔离",
    integrity_drift: "完整性漂移",
    unsafe_restore_override: "强制恢复",
    unlock: "解除保留锁",
    lock: "保留锁定",
  };

  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function notice(msg, ok) {
    const el = $("rwNotice");
    if (!el) return;
    el.textContent = msg;
    el.className = "alert py-2 small " + (ok ? "alert-success" : "alert-danger");
    clearTimeout(notice._t);
    notice._t = setTimeout(() => { el.className = "alert d-none py-2 small"; }, 6000);
  }

  async function api(method, path, body) {
    const opt = { method, credentials: "include",
                  headers: { "Content-Type": "application/json" } };
    if (body) opt.body = JSON.stringify(body);
    const r = await fetch(path, opt);
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  function renderKpi(s) {
    const cards = [
      ["成功备份", s.total, "不含演示记录"],
      ["保留锁定", s.locked, `锁定率 ${s.lock_rate}%`],
      ["检测正常", s.clean, `${s.unscanned} 条待检测`],
      ["可疑 / 隔离", `${s.suspect} / ${s.quarantined}`, "需人工复核"],
      ["完整性漂移", s.drift, "sha256 与基线不符"],
      ["可信恢复点", s.trusted_points, "每任务至少 1 个"],
    ];
    $("rwKpi").innerHTML = cards.map(([label, val, sub]) => {
      const danger = (label === "可疑 / 隔离" && (s.suspect || s.quarantined)) ||
                     (label === "完整性漂移" && s.drift);
      return `<div class="col-6 col-md-4 col-xl-2"><div class="page-card h-100">
        <div class="small text-muted">${esc(label)}</div>
        <div class="mt-1" style="font-size:1.5rem;font-weight:700;color:${danger ? "var(--danger,#ef4444)" : "var(--text-primary,#0f172a)"}">${esc(val)}</div>
        <div class="small text-muted">${esc(sub)}</div></div></div>`;
    }).join("");
  }

  function renderConfig(s) {
    $("rwConfig").innerHTML = Object.keys(CFG_LABELS).map((k) => `
      <div class="col-md-6">
        <div class="form-check form-switch small">
          <input class="form-check-input rw-cfg" type="checkbox" id="cfg_${k}" data-key="${k}"
                 ${s.config[k] ? "checked" : ""}>
          <label class="form-check-label" for="cfg_${k}">${esc(CFG_LABELS[k])}</label>
        </div>
      </div>`).join("");
    const fs = s.fs_capability || {};
    $("rwFs").innerHTML = `
      <div class="mb-2"><span class="badge ${fs.available ? "bg-success" : "bg-secondary"}">
        ${fs.available ? "能力可用" : "不可用"}</span>
        <span class="ms-2">${fs.enabled ? "已启用" : "未启用"}</span></div>
      <div class="text-muted">${esc(fs.reason || "-")}</div>
      <div class="mt-2">内核级 <code>chattr +i</code> 可阻止 root 之外的误删/改写尝试；
        仅在上方开关开启且文件系统支持时生效，失败会如实记录，绝不伪造成功。</div>
      <div class="mt-2 text-muted">尚未实现：S3 Object Lock / 合规保留（当前 local / minio / s3 /
        tape / sftp 五种后端均无保留期语义）；磁带是顺序介质，不等同于气隙副本编排。</div>`;
  }

  function renderSuspects(s) {
    const rows = s.suspects || [];
    if (!rows.length) {
      $("rwSuspects").innerHTML =
        '<tr><td colspan="7" class="text-muted">暂无可疑或隔离产物</td></tr>';
      return;
    }
    $("rwSuspects").innerHTML = rows.map((r) => `
      <tr>
        <td>${r.id}</td>
        <td class="small">任务 ${esc(r.task_id)}</td>
        <td>${esc(r.db_type || "-")}</td>
        <td><span class="badge bg-danger">${esc(r.security_level || "-")}</span></td>
        <td>${r.quarantined ? '<span class="badge bg-warning">已隔离</span>' : "-"}</td>
        <td>${r.locked ? "是" : "否"}</td>
        <td>
          <button class="btn btn-sm btn-outline-secondary" onclick="rwRescan(${r.id})">重扫</button>
          ${r.quarantined ? `<button class="btn btn-sm btn-outline-warning" onclick="rwRelease(${r.id})">解除隔离</button>` : ""}
        </td>
      </tr>`).join("");
  }

  function renderEvents(s) {
    const rows = s.recent_events || [];
    if (!rows.length) {
      $("rwEvents").innerHTML =
        '<tr><td colspan="3" class="text-muted">暂无安全事件</td></tr>';
      return;
    }
    $("rwEvents").innerHTML = rows.map((e) => `
      <tr>
        <td class="small text-nowrap">${esc((e.at || "").slice(5, 19).replace("T", " "))}</td>
        <td class="small"><span class="badge ${e.level === "critical" ? "bg-danger" : "bg-secondary"}">${esc(EVENT_NAMES[e.event] || e.event)}</span></td>
        <td class="small text-muted">${esc((e.detail || "").slice(0, 90))}</td>
      </tr>`).join("");
  }

  async function load() {
    try {
      const s = await api("GET", "/api/ransomware/summary");
      renderKpi(s);
      renderConfig(s);
      renderSuspects(s);
      renderEvents(s);
    } catch (e) {
      notice("加载态势失败: " + e.message, false);
    }
  }

  window.rwRescan = async function (id) {
    try {
      const r = await api("POST", `/api/ransomware/scan/${id}`, {});
      notice(`记录 #${id} 检测完成：${r.level}${r.quarantined ? "（已自动隔离）" : ""}`, true);
      load();
    } catch (e) { notice("重扫失败: " + e.message, false); }
  };

  window.rwRelease = async function (id) {
    const reason = prompt(`解除记录 #${id} 的隔离？请填写原因（审计必填）：`);
    if (!reason || !reason.trim()) return;
    try {
      await api("POST", `/api/ransomware/release/${id}`, { reason });
      notice(`记录 #${id} 已解除隔离，请重新检测后再恢复`, true);
      load();
    } catch (e) { notice("解除失败: " + e.message, false); }
  };

  document.addEventListener("DOMContentLoaded", function () {
    load();
    $("rwRefresh")?.addEventListener("click", load);
    $("rwSaveCfg")?.addEventListener("click", async () => {
      const body = {};
      document.querySelectorAll(".rw-cfg").forEach((el) => {
        body[el.getAttribute("data-key")] = el.checked ? 1 : 0;
      });
      try {
        await api("POST", "/api/ransomware/config", body);
        notice("防护措施配置已保存，后续任务立即生效", true);
        load();
      } catch (e) { notice("保存失败: " + e.message, false); }
    });
    $("rwScanRecent")?.addEventListener("click", async () => {
      try {
        const r = await api("POST", "/api/ransomware/scan-recent", { limit: 100 });
        notice(`补扫完成：${r.scanned} 条（可疑 ${r.suspect}，正常 ${r.clean}，无法判定 ${r.unknown}）`, true);
        load();
      } catch (e) { notice("补扫失败: " + e.message, false); }
    });
  });
})();
