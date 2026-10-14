/* AIDBM 通用列表分页组件。
 *
 * 约定：每个列表一个唯一 key；列表加载函数用 Pager.slice 取当前页数据渲染，
 * 渲染后调 Pager.attach 在表格容器底部挂分页条（上一页/页码/下一页/计数）。
 * 每页默认 10 条（标准化产品规范：任务/记录/日志等列表页统一分页）。
 */
(function () {
  const PAGE_SIZE = 10;
  const state = {}; // key -> {page}

  // 取某列表当前页：返回 {list, page, pages, total}；翻页后自动回调 rerender
  function slice(key, arr, size) {
    const ps = Number(size) > 0 ? Number(size) : PAGE_SIZE;
    const total = (arr || []).length;
    const pages = Math.max(1, Math.ceil(total / ps));
    const st = state[key] || (state[key] = { page: 1 });
    if (st.page > pages) st.page = pages;
    if (st.page < 1) st.page = 1;
    const start = (st.page - 1) * ps;
    return {
      list: (arr || []).slice(start, start + ps),
      page: st.page,
      pages,
      total,
      pageSize: ps,
    };
  }

  // 页码序列（最多 7 个，含省略号）：始终显示首尾与当前页邻域
  function _pageNums(page, pages) {
    if (pages <= 7) {
      return Array.from({ length: pages }, (_, i) => i + 1);
    }
    const set = new Set([1, 2, page - 1, page, page + 1, pages - 1, pages]);
    const nums = [...set].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
    const out = [];
    let prev = 0;
    for (const n of nums) {
      if (n - prev > 1) out.push("…");
      out.push(n);
      prev = n;
    }
    return out;
  }

  // 在 anchor 元素（通常是表格）的父容器底部挂分页条。
  // onChange 为重新加载函数（通常直接调 loadXxx）。
  function attach(key, anchorEl, page, pages, total, onChange) {
    if (!anchorEl || pages <= 0) return;
    const host = anchorEl.closest(".page-card, .card, table") || anchorEl;
    const container = host.parentElement || host;
    let bar = document.getElementById("pager-" + key);
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "pager-" + key;
      bar.className = "d-flex align-items-center justify-content-between flex-wrap gap-2 px-3 py-2 border-top";
      // 防遮挡：分页条相对定位置顶背景，避免被后续兄弟元素盖住下半（首页关注卡实测）
      bar.style.position = "relative";
      bar.style.zIndex = "1";
      bar.style.background = "var(--card-bg, #fff)";
      // 插到表格宿主的末尾（表格与分页条同属一个卡片容器）
      container.appendChild(bar);
    }
    const st = state[key];
    const go = (p) => {
      st.page = p;
      if (typeof onChange === "function") onChange();
    };
    let html = `<span class="text-muted small">共 ${total} 条 · 第 ${page}/${pages} 页 · 每页 ${pageSize} 条</span>`;
    if (pages > 1) {
      html += `<div class="btn-group btn-group-sm" role="group">`;
      html += `<button class="btn btn-outline-secondary" ${page <= 1 ? "disabled" : ""} data-p="${page - 1}">‹ 上一页</button>`;
      for (const n of _pageNums(page, pages)) {
        if (n === "…") {
          html += `<span class="btn btn-outline-secondary border-0 disabled">…</span>`;
        } else {
          html += `<button class="btn ${n === page ? "btn-primary" : "btn-outline-secondary"}" data-p="${n}">${n}</button>`;
        }
      }
      html += `<button class="btn btn-outline-secondary" ${page >= pages ? "disabled" : ""} data-p="${page + 1}">下一页 ›</button>`;
      html += `</div>`;
    }
    bar.innerHTML = html;
    bar.querySelectorAll("button[data-p]").forEach((b) => {
      b.onclick = () => go(Number(b.dataset.p));
    });
  }

  window.Pager = { slice, attach, PAGE_SIZE };
})();
