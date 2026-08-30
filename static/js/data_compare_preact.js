// -*- coding: utf-8 -*-
// 数据对比页面（Preact + htm 免构建试点版）：
// - Preact/htm ESM 已本地化（static/vendor/preact/），配合 import map，零构建、离线可用
// - 与原 vanilla 版（data_compare.js）API 完全一致，可随时回退
import { h, render } from "preact";
import { useState, useEffect, useRef, useCallback } from "preact/hooks";
import htm from "htm";

const html = htm.bind(h);
const { api, toast, fmtTime, fmtDuration, statusBadge } = BKP;

// ---------- 工具 ----------
const cronZh = (expr) => {
  const s = (expr || "").trim();
  if (!s) return "—";
  const p = s.split(/\s+/);
  if (p.length !== 5) return s;
  const [minute, hour, dom, month, dow] = p;
  const WEEK = { "0": "日", "7": "日", "1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六" };
  const pad2 = (n) => String(n).padStart(2, "0");
  const hm = (h2, m) => pad2(h2) + ":" + pad2(m);
  if (dom === "*" && month === "*" && dow === "*" && /^\d+$/.test(hour) && /^\d+$/.test(minute))
    return "每天 " + hm(hour, minute) + " 触发";
  if (dom === "*" && month === "*" && dow === "*" && hour === "*" && minute.indexOf("/") === 0)
    return "每 " + minute.slice(1) + " 分钟";
  if (dom === "*" && month === "*" && dow === "*" && hour.indexOf("/") === 0 && minute === "0")
    return "每 " + hour.slice(1) + " 小时";
  if (dom === "*" && month === "*" && /^\d+$/.test(dow) && /^\d+$/.test(hour) && /^\d+$/.test(minute))
    return "每周" + (WEEK[dow] || dow) + " " + hm(hour, minute) + " 触发";
  return "定时：" + s;
};

const scheduleLabel = (t) => {
  const type = t.schedule_type;
  if (type === "manual" || !type) return "手动";
  if (type === "cron") return cronZh(t.cron_expr);
  if (type === "interval") return "每 " + (t.interval_minutes || "?") + " 分钟";
  return "—";
};

const dbTypeZh = (t) => ({ mysql: "MySQL", mariadb: "MariaDB", postgresql: "PostgreSQL", kingbase: "金仓", oracle: "Oracle" }[t] || t || "-");

const StatusBadge = ({ s }) =>
  html`<span dangerouslySetInnerHTML=${{ __html: statusBadge(s || "running") }} />`;

const LastStatus = ({ s }) => !s
  ? html`<span class="badge bg-secondary">未运行</span>`
  : html`<${StatusBadge} s=${s} />`;

const EnableBadge = ({ on }) => on
  ? html`<span class="badge badge-ok">启用</span>`
  : html`<span class="badge bg-secondary">停用</span>`;

const endpointLabel = (t, side) =>
  dbTypeZh(t[side + "_db_type"]) + " " + (t[side + "_host"] || "-") + ":" +
  (t[side + "_port"] || "-") + "/" + (t[side + "_database"] || "-");

const rowStr = (row) => !row ? "（无此行）" : "[" + row.join(", ") + "]";

// 先让 Bootstrap 完成 hide 动画再卸载组件，避免过渡中断
const hideThen = (ref, then) => {
  const el = ref && ref.current;
  const inst = el && window.bootstrap ? bootstrap.Modal.getInstance(el) : null;
  if (inst) {
    el.addEventListener("hidden.bs.modal", () => then(), { once: true });
    inst.hide();
  } else {
    then();
  }
};

// ---------- KPI 卡片 ----------
const StatCard = ({ icon, color, num, label }) => html`
  <div class="col-md-3">
    <div class="card-stat">
      <div class=${"stat-icon " + color}><i class=${"bi bi-" + icon}></i></div>
      <div class="stat-num">${num}</div>
      <div class="stat-label">${label}</div>
    </div>
  </div>`;

// ---------- 任务表 ----------
function TaskTable({ tasks, onRun, onEdit, onDelete }) {
  if (!tasks.length)
    return html`<tbody><tr><td colspan="8" class="text-center text-muted py-3">暂无对比任务，点击右上角新建</td></tr></tbody>`;
  return html`
    <tbody>
      ${tasks.map((t) => {
        const tables = Array.isArray(t.tables) && t.tables.length ? t.tables.length + " 张指定表" : "两端共有表";
        const running = t.last_status === "running";
        return html`
          <tr key=${t.id}>
            <td class="fw-bold">${t.name}</td>
            <td class="small">${endpointLabel(t, "source")}</td>
            <td class="small">${endpointLabel(t, "target")}</td>
            <td class="small">
              ${tables}
              ${t.enable_checksum && html`<div><span class="badge bg-info bg-opacity-10 text-info">含校验和</span></div>`}
            </td>
            <td class="small">${scheduleLabel(t)}</td>
            <td><${EnableBadge} on=${!!t.enabled} /></td>
            <td>
              ${t.last_run_at
                ? html`<div>${fmtTime(t.last_run_at)}</div><${LastStatus} s=${t.last_status} />`
                : html`<span class="text-muted">—</span>`}
            </td>
            <td class="text-end">
              <button class="btn btn-sm btn-outline-primary me-1" disabled=${running}
                      onClick=${() => onRun(t.id)} title="立即对比">
                <i class="bi bi-play-fill"></i> 对比
              </button>
              <button class="btn btn-sm btn-outline-secondary me-1" onClick=${() => onEdit(t.id)} title="编辑">
                <i class="bi bi-pencil"></i>
              </button>
              <button class="btn btn-sm btn-outline-danger" onClick=${() => onDelete(t.id)} title="删除">
                <i class="bi bi-trash"></i>
              </button>
            </td>
          </tr>`;
      })}
    </tbody>`;
}

// ---------- 报告表 ----------
function ReportTable({ reports, onShow }) {
  if (!reports.length)
    return html`<tbody><tr><td colspan="6" class="text-center text-muted py-3">暂无对比报告</td></tr></tbody>`;
  return html`
    <tbody>
      ${reports.map((r) => {
        const s = r.summary_json || {};
        return html`
          <tr key=${r.id}>
            <td class="fw-bold">${r.task_name || "任务 " + r.task_id}</td>
            <td><${StatusBadge} s=${r.status} /></td>
            <td>
              ${(s.tables_total || 0)} /
              <span class="text-success">${s.tables_matched || 0}</span> /
              <span class="text-danger">${s.tables_mismatched || 0}</span> /
              <span class="text-warning">${s.tables_failed || 0}</span>
            </td>
            <td>${fmtDuration(r.duration_sec)}</td>
            <td>${r.created_at ? fmtTime(r.created_at) : "-"}</td>
            <td class="text-end">
              <button class="btn btn-sm btn-outline-primary" onClick=${() => onShow(r.id)}>
                <i class="bi bi-eye"></i> 明细
              </button>
            </td>
          </tr>`;
      })}
    </tbody>`;
}

// ---------- 任务表单模态框 ----------
function TaskModal({ state, onSave, onClose }) {
  const [f, setF] = useState(state.values);
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) new bootstrap.Modal(ref.current).show();
  }, []);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const sides = ["source", "target"];
  const submit = () => {
    const payload = { ...f, tables: (f.tables || "").split(",").map((s) => s.trim()).filter(Boolean) };
    payload.sample_rows = parseInt(payload.sample_rows, 10) || 0;
    ["source", "target"].forEach((side) => {
      payload[side + "_port"] = parseInt(payload[side + "_port"], 10) || null;
      if (!payload[side + "_password"]) delete payload[side + "_password"];
    });
    if (!payload.source_host || !payload.target_host) { toast("请填写源/目标主机", "danger"); return; }
    hideThen(ref, () => onSave(payload));
  };
  return html`
    <div class="modal fade" id="dcTaskModal" tabindex="-1" ref=${ref}>
      <div class="modal-dialog modal-xl">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title">${state.id ? "编辑对比任务" : "新建对比任务"}</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal" onClick=${() => hideThen(ref, onClose)}></button>
          </div>
          <div class="modal-body">
            <div class="row g-3">
              <div class="col-md-4">
                <label class="form-label">任务名称 *</label>
                <input class="form-control" value=${f.name} onInput=${set("name")} placeholder="例如：恢复库 vs 生产库（每日）" />
              </div>
              <div class="col-md-4">
                <label class="form-label">表清单（逗号分隔，留空=两端共有表）</label>
                <input class="form-control" value=${f.tables} onInput=${set("tables")} placeholder="t1,t2,t3" />
              </div>
              <div class="col-md-2">
                <label class="form-label">抽样行数</label>
                <input class="form-control" type="number" min="0" value=${f.sample_rows} onInput=${set("sample_rows")} />
              </div>
              <div class="col-md-2 d-flex align-items-end pb-1">
                <div class="form-check form-switch">
                  <input class="form-check-input" type="checkbox" id="dc_ck" checked=${f.enable_checksum} onClick=${set("enable_checksum")} />
                  <label class="form-check-label" for="dc_ck">全表校验和</label>
                </div>
              </div>
            </div>
            ${sides.map((side) => html`
              <div key=${side} class=${"row g-3 mt-1 " + (side === "source" ? "border-end" : "")}>
                <div class="col-md-6 border-end">
                  <div class="fw-bold mb-2"><i class=${"bi bi-" + (side === "source" ? "database-fill-gear" : "database-fill-check") + " me-1"}></i>
                    ${side === "source" ? "生产库（源）" : "恢复库（目标）"}</div>
                </div>
                <div class="col-md-6"></div>
                <div class="col-md-3">
                  <label class="form-label">类型 *</label>
                  <select class="form-select" value=${f[side + "_db_type"]} onChange=${set(side + "_db_type")}>
                    <option value="mysql">MySQL / MariaDB</option>
                    <option value="postgresql">PostgreSQL / Kingbase</option>
                    <option value="oracle">Oracle</option>
                  </select>
                </div>
                <div class="col-md-3">
                  <label class="form-label">主机 *</label>
                  <input class="form-control" value=${f[side + "_host"]} onInput=${set(side + "_host")} />
                </div>
                <div class="col-md-2">
                  <label class="form-label">端口</label>
                  <input class="form-control" type="number" value=${f[side + "_port"]} onInput=${set(side + "_port")} />
                </div>
                <div class="col-md-2">
                  <label class="form-label">用户名</label>
                  <input class="form-control" value=${f[side + "_username"]} onInput=${set(side + "_username")} />
                </div>
                <div class="col-md-2">
                  <label class="form-label">密码</label>
                  <input class="form-control" type="password" value=${f[side + "_password"]} onInput=${set(side + "_password")} placeholder="留空不修改" />
                </div>
                <div class="col-md-3">
                  <label class="form-label">数据库 / 服务名</label>
                  <input class="form-control" value=${f[side + "_database"]} onInput=${set(side + "_database")} />
                </div>
                <div class="col-md-3">
                  <label class="form-label">Schema（PG/Oracle）</label>
                  <input class="form-control" value=${f[side + "_schema"]} onInput=${set(side + "_schema")} />
                </div>
              </div>`)}
            <div class="row g-3 mt-1">
              <div class="col-md-4">
                <label class="form-label">调度类型</label>
                <select class="form-select" value=${f.schedule_type} onChange=${set("schedule_type")}>
                  <option value="manual">手动</option>
                  <option value="cron">Cron 表达式</option>
                  <option value="interval">固定间隔(分钟)</option>
                </select>
              </div>
              ${f.schedule_type === "cron" && html`
                <div class="col-md-4">
                  <label class="form-label">Cron 表达式</label>
                  <input class="form-control" value=${f.cron_expr} onInput=${set("cron_expr")} placeholder="0 3 * * 1" />
                </div>`}
              ${f.schedule_type === "interval" && html`
                <div class="col-md-4">
                  <label class="form-label">间隔分钟数</label>
                  <input class="form-control" type="number" min="5" value=${f.interval_minutes} onInput=${set("interval_minutes")} />
                </div>`}
              <div class="col-md-4 d-flex align-items-end pb-1">
                <div class="form-check form-switch">
                  <input class="form-check-input" type="checkbox" id="dc_en" checked=${f.enabled} onClick=${set("enabled")} />
                  <label class="form-check-label" for="dc_en">启用任务</label>
                </div>
              </div>
            </div>
          </div>
          <div class="modal-footer">
            <button class="btn btn-secondary" data-bs-dismiss="modal" onClick=${() => hideThen(ref, onClose)}>取消</button>
            <button class="btn btn-primary" onClick=${submit}>保存</button>
          </div>
        </div>
      </div>
    </div>`;
}

// ---------- 报告明细模态框 ----------
function ReportModal({ report, onClose }) {
  const ref = useRef(null);
  useEffect(() => { if (ref.current) new bootstrap.Modal(ref.current).show(); }, []);
  if (!report) return null;
  const s = report.summary_json || {};
  const tables = report.tables_json || [];
  return html`
    <div class="modal fade show" id="dcReportModal" tabindex="-1" ref=${ref}>
      <div class="modal-dialog modal-xl">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title">对比报告明细</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal" onClick=${() => hideThen(ref, onClose)}></button>
          </div>
          <div class="modal-body">
            <div class="mb-2">
              <span class=${"badge " + (report.status === "success" ? "badge-ok" : "bg-danger")}>${report.status}</span>
              <span class="text-muted ms-2">${report.message || ""}</span>
            </div>
            <div class="row g-2 mb-3 text-center">
              <div class="col"><div class="border rounded py-2"><div class="fs-5 fw-bold">${s.tables_total || 0}</div><div class="small text-muted">对比表数</div></div></div>
              <div class="col"><div class="border rounded py-2 text-success"><div class="fs-5 fw-bold">${s.tables_matched || 0}</div><div class="small text-muted">一致</div></div></div>
              <div class="col"><div class="border rounded py-2 text-danger"><div class="fs-5 fw-bold">${s.tables_mismatched || 0}</div><div class="small text-muted">不一致</div></div></div>
              <div class="col"><div class="border rounded py-2 text-warning"><div class="fs-5 fw-bold">${s.tables_failed || 0}</div><div class="small text-muted">失败</div></div></div>
              <div class="col"><div class="border rounded py-2"><div class="fs-5 fw-bold">${fmtDuration(report.duration_sec)}</div><div class="small text-muted">耗时</div></div></div>
            </div>
            <table class="table table-sm table-bordered align-middle mb-0">
              <thead><tr>
                <th>表</th><th>源行数</th><th>目标行数</th><th>行数</th><th>校验和</th><th>抽样差异</th><th>说明</th>
              </tr></thead>
              <tbody>
                ${tables.map((t) => html`
                  <tr key=${t.table} class=${t.status === "mismatch" ? "table-danger" : (t.status === "failed" ? "table-warning" : "")}>
                    <td class="fw-bold">${t.table}</td>
                    <td>${t.source_rows === null ? "—" : t.source_rows}</td>
                    <td>${t.target_rows === null ? "—" : t.target_rows}</td>
                    <td>${t.rows_match === null ? "—" : (t.rows_match
                      ? html`<span class="text-success">一致</span>`
                      : html`<span class="text-danger">不一致</span>`)}</td>
                    <td>${t.checksum_match === null ? "—" : (t.checksum_match
                      ? html`<span class="text-success">一致</span>`
                      : html`<span class="text-danger">不一致</span>`)}</td>
                    <td>${t.sample_diff_count || 0}</td>
                    <td>
                      <${StatusBadge} s=${t.status} />
                      <div class="small text-muted text-break">${t.message || ""}
                        ${t.sample_diffs && t.sample_diffs.length > 0 && html`
                          <details class="mt-1">
                            <summary class="text-primary small">差异明细（前 20）</summary>
                            <div class="small">
                              ${t.sample_diffs.map((d, i) => html`
                                <div class="text-break" key=${i}>#${i + 1} 源: ${rowStr(d.source)} / 目标: ${rowStr(d.target)}</div>`)}
                            </div>
                          </details>`}
                      </div>
                    </td>
                  </tr>`)}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>`;
}

// ---------- 主应用 ----------
const emptyForm = {
  name: "", tables: "", sample_rows: 100, enable_checksum: false,
  schedule_type: "manual", cron_expr: "", interval_minutes: "", enabled: true,
  source_db_type: "mysql", source_host: "", source_port: "", source_username: "",
  source_password: "", source_database: "", source_schema: "",
  target_db_type: "mysql", target_host: "", target_port: "", target_username: "",
  target_password: "", target_database: "", target_schema: "",
};

function App() {
  const [stats, setStats] = useState({});
  const [tasks, setTasks] = useState([]);
  const [reports, setReports] = useState([]);
  const [formState, setFormState] = useState(null);   // null | {id, values}
  const [detail, setDetail] = useState(null);

  const refreshAll = useCallback(async () => {
    try {
      const [s, t, r] = await Promise.all([
        api("GET", "/api/data-compare-stats"),
        api("GET", "/api/data-compare-tasks"),
        api("GET", "/api/data-compare-reports"),
      ]);
      setStats(s.success ? s.data : {});
      setTasks(t.success ? t.data : []);
      setReports(r.success ? r.data : []);
    } catch (e) { toast("加载失败", "danger"); }
  }, []);

  useEffect(() => { refreshAll(); }, [refreshAll]);

  // toolbar 静态按钮入口（模板 onclick）
  useEffect(() => {
    window.dcRefresh = refreshAll;
    window.dcCreate = () => setFormState({ id: null, values: { ...emptyForm } });
  }, [refreshAll]);

  const openCreate = () => setFormState({ id: null, values: { ...emptyForm } });
  const openEdit = async (id) => {
    const res = await api("GET", "/api/data-compare-tasks/" + id);
    if (!res.success) { toast("加载任务失败", "danger"); return; }
    const t = res.data;
    const values = { ...emptyForm };
    Object.keys(values).forEach((k) => { if (t[k] !== undefined && t[k] !== null) values[k] = t[k]; });
    values.tables = Array.isArray(t.tables) ? t.tables.join(",") : "";
    values.source_password = "";
    values.target_password = "";
    setFormState({ id, values });
  };

  const saveTask = async (payload) => {
    const id = formState && formState.id;
    const res = id
      ? await api("PUT", "/api/data-compare-tasks/" + id, payload)
      : await api("POST", "/api/data-compare-tasks", payload);
    if (res.success) {
      toast(id ? "已更新" : "已创建", "success");
      setFormState(null);
      refreshAll();
    } else {
      toast(res.message || "保存失败", "danger");
    }
  };

  const runCompare = async (id) => {
    toast("对比任务已启动，后台执行中...", "info");
    const res = await api("POST", "/api/data-compare-tasks/" + id + "/run");
    if (!res.success) { toast(res.message || "启动失败", "danger"); return; }
    let polls = 0;
    const timer = setInterval(async () => {
      polls += 1;
      const r = await api("GET", "/api/data-compare-tasks/" + id + "/reports?limit=1");
      const rep = (r.data && r.data[0]) || {};
      if (rep.status !== "running" || polls > 150) {
        clearInterval(timer);
        toast(rep.status === "success" ? "对比完成：数据一致"
          : "对比完成：" + (rep.message || "发现差异或失败"),
          rep.status === "success" ? "success" : "danger");
        refreshAll();
      }
    }, 2000);
    refreshAll();
  };

  const deleteTask = async (id) => {
    if (!confirm("确认删除该对比任务及其全部报告？")) return;
    const res = await api("DELETE", "/api/data-compare-tasks/" + id);
    toast(res.success ? "已删除" : "删除失败", res.success ? "success" : "danger");
    refreshAll();
  };

  const showReport = async (id) => {
    const res = await api("GET", "/api/data-compare-reports/" + id);
    if (!res.success) { toast("加载报告失败", "danger"); return; }
    setDetail(res.data);
  };

  return html`
    <div>
      <div class="row g-3 mb-3">
        <${StatCard} icon="clipboard-data" color="bg-primary bg-opacity-10 text-primary" num=${stats.task_count || 0} label="对比任务" />
        <${StatCard} icon="check-circle" color="bg-success bg-opacity-10 text-success" num=${stats.success_count || 0} label="对比通过" />
        <${StatCard} icon="x-circle" color="bg-danger bg-opacity-10 text-danger" num=${stats.failed_count || 0} label="对比不一致/失败" />
        <${StatCard} icon="clock-history" color="bg-info bg-opacity-10 text-info" num=${stats.last_compare_at ? fmtTime(stats.last_compare_at) : "—"} label="最近对比" />
      </div>

      <div class="page-card mb-3">
        <div class="d-flex justify-content-between align-items-center px-3 py-2 border-bottom">
          <div class="fw-bold"><i class="bi bi-list-check me-1"></i>对比任务</div>
        </div>
        <div class="table-responsive" style="flex:1">
          <table class="table table-hover align-middle mb-0">
            <thead><tr>
              <th>任务</th><th>生产库（源）</th><th>恢复库（目标）</th><th>对比范围</th>
              <th>调度</th><th>状态</th><th>上次运行</th><th class="text-end">操作</th>
            </tr></thead>
            <${TaskTable} tasks=${tasks} onRun=${runCompare} onEdit=${openEdit} onDelete=${deleteTask} />
          </table>
        </div>
      </div>

      <div class="page-card">
        <div class="d-flex justify-content-between align-items-center px-3 py-2 border-bottom">
          <div class="fw-bold"><i class="bi bi-file-earmark-diff me-1"></i>对比报告</div>
        </div>
        <div class="table-responsive" style="flex:1">
          <table class="table table-hover align-middle mb-0">
            <thead><tr>
              <th>任务</th><th>结果</th><th>表(总/一致/差异/失败)</th><th>耗时</th><th>时间</th><th class="text-end">操作</th>
            </tr></thead>
            <${ReportTable} reports=${reports} onShow=${showReport} />
          </table>
        </div>
      </div>

      ${formState && html`<${TaskModal} state=${formState} onSave=${saveTask} onClose=${() => setFormState(null)} />`}
      ${detail && html`<${ReportModal} report=${detail} onClose=${() => setDetail(null)} />`}
    </div>`;
}

render(html`<${App} />`, document.getElementById("dc-root"));
