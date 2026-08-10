/**
 * QA 第 2 轮复验 · 前端行为谐调器（真实执行 static/js/app.js）。
 *
 * 目的：B1 第 4 项与 G1 第 5 项的「行为级」取证 —— 不做正则猜测，而是把
 * 真实的 app.js 装进 Node vm，配上最小 DOM/fetch 桩，喂入 Python 探针导出的
 * 真实后端响应体（scripts/_qa_round2_fixture.json），然后走真实代码路径：
 *
 *   场景 A（B1 前端可用）：page=dr-link → DOMContentLoaded → initDrLink()
 *     → window.openLinkModal() → loadLinkSources() + renderLinkSourceList()
 *     断言：#linkNoSource 隐藏（不再恒显「暂无可用数据源」）、两组数据源渲染、
 *           下一步/保存按钮解禁；随后 window.pickLinkSource() 回填主/备站点与路由策略。
 *
 *   场景 B（G1 守护态兜底）：page=rt_timeline → DOMContentLoaded
 *     → initRtTimeline() → rtLoadDaemon() 读到 /api/rt/status {running:false}
 *     断言：#rtStoppedHint 由默认 d-none 变为显形（真正的降级提示，非静默成功）。
 *
 * 运行：node scripts/_qa_probe_drlink_ui.mjs
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const APP_JS = path.join(ROOT, "static", "js", "app.js");
const FIXTURE = path.join(HERE, "_qa_round2_fixture.json");

const fixture = JSON.parse(fs.readFileSync(FIXTURE, "utf-8"));
const appSource = fs.readFileSync(APP_JS, "utf-8");

const results = [];
function check(no, name, ok, evidence) {
  results.push({ no, name, ok: !!ok, evidence });
  console.log(`[${ok ? "PASS" : "FAIL"}] ${no} ${name}\n       证据：${evidence}`);
  return !!ok;
}

/* ---------------- 最小 DOM 桩 ---------------- */
function makeClassList(el) {
  const set = new Set();
  return {
    _set: set,
    add: (...c) => c.forEach((x) => set.add(x)),
    remove: (...c) => c.forEach((x) => set.delete(x)),
    contains: (c) => set.has(c),
    toggle: (c, force) => {
      const on = force === undefined ? !set.has(c) : !!force;
      if (on) set.add(c); else set.delete(c);
      return on;
    },
    get value() { return [...set].join(" "); },
  };
}

function makeEl(id) {
  const el = {
    id, value: "", textContent: "", innerHTML: "", checked: false,
    disabled: false, style: {}, dataset: {}, children: [], _listeners: {},
    addEventListener(t, fn) { (this._listeners[t] ||= []).push(fn); },
    removeEventListener() {},
    dispatchEvent(ev) {
      (this._listeners[(ev && ev.type) || ""] || []).forEach((fn) => fn.call(this, ev));
      return true;
    },
    setAttribute(k, v) { this.dataset[k] = v; },
    getAttribute() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    appendChild(c) { this.children.push(c); return c; },
    insertAdjacentHTML(pos, html) { this.innerHTML += html; },
    closest() { return null; },
    remove() {},
    focus() {}, click() {}, scrollIntoView() {}, reset() {},
  };
  el.classList = makeClassList(el);
  return el;
}

function makeDom(pageName, presetClasses = {}) {
  const registry = new Map();
  const get = (id) => {
    if (!registry.has(id)) {
      const el = makeEl(id);
      // 还原模板里的初始 class（如 d-none 默认隐藏）
      (presetClasses[id] || []).forEach((c) => el.classList.add(c));
      registry.set(id, el);
    }
    return registry.get(id);
  };
  const body = makeEl("body");
  body.dataset.page = pageName;
  const document = {
    body,
    head: makeEl("head"),
    documentElement: makeEl("html"),
    getElementById: (id) => get(id),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => makeEl("_" + tag),
    addEventListener: () => {},
    removeEventListener: () => {},
    cookie: "",
  };
  return { document, registry, get };
}

/* ---------------- fetch 桩：喂真实后端响应体 ---------------- */
function makeFetch(routes, log) {
  return async (url, opt = {}) => {
    const method = (opt.method || "GET").toUpperCase();
    log.push(`${method} ${url}`);
    for (const [pattern, payload] of routes) {
      if (pattern instanceof RegExp ? pattern.test(url) : url === pattern) {
        const data = typeof payload === "function" ? payload(url, opt) : payload;
        return { ok: true, status: 200, json: async () => data };
      }
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
}

/* ---------------- 在 vm 中装载真实 app.js ---------------- */
async function loadApp(pageName, routes, presetClasses) {
  const { document, get } = makeDom(pageName, presetClasses);
  const fetchLog = [];
  const winListeners = {};
  const window = {
    document,
    addEventListener(t, fn) { (winListeners[t] ||= []).push(fn); },
    removeEventListener() {},
    location: { search: "", href: "http://localhost/", pathname: "/" },
    localStorage: {
      _d: {}, getItem(k) { return this._d[k] ?? null; },
      setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; },
    },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    history: { replaceState() {}, pushState() {} },
    scrollTo() {},
  };
  const ctx = {
    window, document, console,
    localStorage: window.localStorage,
    sessionStorage: window.localStorage,
    location: window.location,
    history: window.history,
    fetch: makeFetch(routes, fetchLog),
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval,
    URLSearchParams, JSON, Math, Date, Promise, Object, Array, String,
    Number, Boolean, RegExp, Error, Map, Set, Proxy, isNaN, parseInt, parseFloat,
    encodeURIComponent, decodeURIComponent,
    navigator: { userAgent: "qa-harness", clipboard: { writeText: async () => {} } },
    bootstrap: {
      Modal: class { constructor() {} show() {} hide() {}
        static getInstance() { return null; } },
      Tooltip: class { constructor() {} },
      Toast: class { constructor() {} show() {} },
    },
    Chart: class { constructor() {} destroy() {} update() {} },
  };
  ctx.globalThis = ctx;
  ctx.window.window = window;
  vm.createContext(ctx);
  vm.runInContext(appSource, ctx, { filename: "app.js" });

  // 触发 DOMContentLoaded → 走真实页面初始化路径
  for (const fn of winListeners.DOMContentLoaded || []) {
    await fn({ type: "DOMContentLoaded" });
  }
  return { ctx, window, get, fetchLog };
}

/* ================= 场景 A：B1 前端选源与回填 ================= */
console.log("=== 场景 A：/dr-link 新增链路弹窗（真实 app.js 执行）===\n");
{
  const routes = [
    [/\/api\/disaster-links\/sources$/, fixture.sources],
    [/\/api\/disaster-links$/, { ok: true, links: [] }],
    [/\/api\/meta$/, { ok: true, demo_mode: true }],
  ];
  // 模板初始态：linkNoSource 默认带 d-none
  const { window, get, fetchLog } = await loadApp("dr-link", routes,
    { linkNoSource: ["alert", "alert-warning", "d-none"] });

  if (typeof window.openLinkModal !== "function") {
    check("4.3", "initDrLink() 暴露 openLinkModal", false, "未定义，后续断言无法执行");
  } else {
    await window.openLinkModal();

    const list = get("linkSourceList");
    const noSrc = get("linkNoSource");
    const html = list.innerHTML || "";

    check("4.3", "第 1 步 linkStep1 渲染出数据源分组（不再恒显「暂无可用数据源」）",
      !noSrc.classList.contains("d-none") === false
      && html.includes("数据同步任务（2）") && html.includes("实时保护任务（2）"),
      `#linkNoSource 隐藏=${noSrc.classList.contains("d-none")}；`
      + `渲染分组=[${["数据同步任务（2）", "实时保护任务（2）"]
        .filter((t) => html.includes(t)).join(", ")}]；innerHTML ${html.length} 字符`);

    check("4.4", "四个源全部渲染且状态徽章有值、rt 源显示实际 RPO",
      ["北京→上海 订单库同步", "上海→广州 用户库同步", "核心交易库", "风控日志库"]
        .every((n) => html.includes(n))
      && html.includes("badge") && html.includes("RPO"),
      `源名命中=${["北京→上海 订单库同步", "上海→广州 用户库同步", "核心交易库", "风控日志库"]
        .filter((n) => html.includes(n)).length}/4；`
      + `含状态徽章=${html.includes("badge")}；含 RPO 文本=${html.includes("RPO")}`);

    check("4.5", "有源时「下一步」「保存」按钮解禁",
      get("linkNextBtn").disabled === false && get("saveLinkBtn").disabled === false,
      `linkNextBtn.disabled=${get("linkNextBtn").disabled}；`
      + `saveLinkBtn.disabled=${get("saveLinkBtn").disabled}`);

    // ---- 选中同步任务 → 回填 ----
    const syncSrc = (fixture.sources.items || []).find((s) => s.kind === "sync_task");
    window.pickLinkSource("sync_task", syncSrc.id);

    const kind = get("l_source_kind").value;
    const sid = get("l_source_id").value;
    const primary = get("l_primary_site").value;
    const drs = get("l_dr_site").value;
    const rpRaw = get("l_route_policy").value;
    let rp = null, rpOk = false;
    try { rp = JSON.parse(rpRaw); rpOk = Array.isArray(rp) && rp.length > 0; } catch (e) { }

    check("4.6", "pickLinkSource() 回填主站点 / 备站点，且与源值一致",
      kind === "sync_task" && String(sid) === String(syncSrc.id)
      && primary === syncSrc.primary_site && drs === syncSrc.dr_site
      && primary !== "" && drs !== "",
      `l_source_kind=${kind}；l_source_id=${sid}；`
      + `l_primary_site="${primary}"（源值"${syncSrc.primary_site}"）；`
      + `l_dr_site="${drs}"（源值"${syncSrc.dr_site}"）`);

    check("4.7", "pickLinkSource() 回填路由策略（端点取源备站点）",
      rpOk && rp[0].endpoint === syncSrc.dr_site && rp[0].enabled === true,
      rpOk ? `route_policy=${JSON.stringify(rp)}` : `解析失败，原值="${rpRaw}"`);

    check("4.8", "选源后自动取消手工模式，且链路名默认回填",
      get("l_manual_mode").checked === false
      && get("l_name").value.includes(syncSrc.name),
      `l_manual_mode.checked=${get("l_manual_mode").checked}；`
      + `l_name="${get("l_name").value}"`);

    console.log(`\n       [fetch 轨迹] ${fetchLog.join(" | ")}\n`);
  }
}

/* ================= 场景 B：G1 守护 stopped 兜底提示 ================= */
console.log("=== 场景 B：/rt-timeline 守护 stopped 兜底提示（真实 app.js 执行）===\n");
{
  const routes = [
    [/\/api\/rt\/status$/, fixture.rt_status_stopped],   // running:false
    [/\/api\/rt\/tasks$/, { ok: true, items: [] }],
    [/\/api\/rt\/health$/, { ok: true, items: [], summary: {} }],
    [/\/api\/meta$/, { ok: true, demo_mode: true }],
  ];
  const { get, fetchLog } = await loadApp("rt_timeline", routes,
    { rtStoppedHint: ["alert", "alert-warning", "d-none"] });

  const hint = get("rtStoppedHint");
  check("5.5", "守护 running=false 时 #rtStoppedHint 由 d-none 变为显形",
    hint.classList.contains("d-none") === false,
    `初始 class 含 d-none，读到 /api/rt/status {running:false} 后 `
    + `d-none=${hint.classList.contains("d-none")}；当前 class="${hint.classList.value}"；`
    + `守护状态文案="${get("rtDaemonState").textContent}"`);

  check("5.6", "确已实际请求 /api/rt/status（非静态兜底）",
    fetchLog.some((u) => u.includes("/api/rt/status")),
    `fetch 轨迹=${fetchLog.join(" | ")}`);
}

/* ================= 场景 C：反向用例 —— running=true 时提示条保持隐藏 ============ */
console.log("\n=== 场景 C：反向用例 —— 守护 running=true 提示条不得误报 ===\n");
{
  const routes = [
    [/\/api\/rt\/status$/, { ok: true, running: true, enabled: true, driver: "demo" }],
    [/\/api\/rt\/tasks$/, { ok: true, items: [] }],
    [/\/api\/rt\/health$/, { ok: true, items: [], summary: {} }],
    [/\/api\/meta$/, { ok: true, demo_mode: true }],
  ];
  const { get } = await loadApp("rt_timeline", routes,
    { rtStoppedHint: ["alert", "alert-warning", "d-none"] });
  const hint = get("rtStoppedHint");
  check("5.7", "守护 running=true 时提示条保持隐藏（无误报）",
    hint.classList.contains("d-none") === true,
    `d-none=${hint.classList.contains("d-none")}；`
    + `守护状态文案="${get("rtDaemonState").textContent}"`);
}

/* ---------------- 汇总 ---------------- */
const passed = results.filter((r) => r.ok).length;
console.log("\n" + "=".repeat(68));
console.log(`JS 行为侧复验：${passed}/${results.length} 通过`);
results.forEach((r) => console.log(`  ${r.ok ? "✓" : "✗"} ${r.no} ${r.name}`));
console.log("=".repeat(68));
process.exit(passed === results.length ? 0 : 1);
