/* CDC 实时备份页：捕获流管理 + 事件查看 + 任意时间点回放/回滚 */
/* 注意：BKP.api 的签名是 (method, url, body)。本页一律用完整签名调用，
   早期版本曾按 (url, opt) 调用，导致 url 被当作 HTTP 方法传给 fetch，
   整页请求全部失败（"is not a valid HTTP method"）。 */
(function () {
  var curStream = null;

  function $(id) { return document.getElementById(id); }
  // BKP.api 直接 fetch(url)、不拼任何前缀，而本文件的历史调用写作 '/cdc/xxx'，
  // 真实接口在 '/api/cdc/xxx' —— 整块 CDC 面板因此全部 404。此处统一补齐前缀。
  function api(method, url, body) {
    var u = String(url || '');
    if (u.charAt(0) !== '/') { u = '/' + u; }
    if (u.indexOf('/api/') !== 0) { u = '/api' + u; }
    return BKP.api(method, u, body);
  }

  function statusBadge(s, alive) {
    var map = {
      running: 'badge-run', stopped: 'bg-secondary',
      error: 'badge-fail'
    };
    var label = { running: '运行中', stopped: '已停止', error: '异常' }[s] || s || '-';
    if (s === 'running' && alive === false) { label = '已停止(进程丢失)'; }
    var cls = map[s] || 'bg-secondary';
    return '<span class="badge ' + cls + '">' + label + '</span>';
  }

  function opBadge(op) {
    var m = { INSERT: 'badge-ok', UPDATE: 'badge-run', DELETE: 'badge-fail', DDL: 'badge-sim' };
    return '<span class="badge ' + (m[op] || 'bg-secondary') + '">' + op + '</span>';
  }

  function jsonCell(obj) {
    if (!obj || !Object.keys(obj).length) return '<span class="text-muted">-</span>';
    var parts = [];
    for (var k in obj) {
      var v = obj[k];
      if (v === null) v = 'NULL';
      parts.push(BKP.esc(k) + '=' + BKP.esc(String(v)));
    }
    return '<span class="small">' + parts.join('<br>') + '</span>';
  }

  // ---------------- 流列表 ----------------
  function loadStreams() {
    api('GET', '/cdc/streams').then(function (rows) {
      const pg = Pager.slice('cdcStreams', rows);
      $('streamBody').innerHTML = pg.list.map(function (r) {
        return '<tr>' +
          '<td><b>' + BKP.esc(r.name) + '</b><div class="text-muted small">#' + r.id + ' · ' + BKP.esc(r.purpose || 'cdp') + '</div></td>' +
          '<td>' + BKP.esc((BKP.META.display_names[r.db_type] || r.db_type)) + '</td>' +
          '<td class="small">' + BKP.esc(r.host) + ':' + (r.port || '') + '</td>' +
          '<td>' + BKP.esc(r.db_name || '-') + '</td>' +
          '<td>' + statusBadge(r.status, r.alive) + (r.last_error ? '<div class="text-danger small" title="' + BKP.esc(r.last_error) + '">' + BKP.esc(String(r.last_error).slice(0, 40)) + '</div>' : '') + '</td>' +
          '<td class="text-end">' + (r.events_total || 0) + '</td>' +
          '<td class="small">' + BKP.esc(r.last_event_at || '-') + '</td>' +
          '<td class="small text-muted">' + BKP.esc(JSON.stringify(r.position || {})) + '</td>' +
          '<td class="text-nowrap">' +
          (r.status === 'running'
            ? '<button class="btn btn-sm btn-outline-warning btn-stop" data-id="' + r.id + '">停止</button> '
            : '<button class="btn btn-sm btn-outline-success btn-start" data-id="' + r.id + '">启动</button> ') +
          '<button class="btn btn-sm btn-outline-primary btn-evt" data-id="' + r.id + '" data-name="' + BKP.esc(r.name) + '">事件</button> ' +
          '<button class="btn btn-sm btn-outline-danger btn-del" data-id="' + r.id + '"><i class="bi bi-trash"></i></button>' +
          '</td></tr>';
      }).join('') || '<tr><td colspan="9" class="text-center text-muted py-3">暂无捕获流</td></tr>';
      Pager.attach('cdcStreams', $('streamBody'), pg.page, pg.pages, pg.total, loadStreams);

      Array.prototype.forEach.call(document.querySelectorAll('#streamBody .btn-start'), function (b) {
        b.onclick = function () { act(b.dataset.id, 'start'); };
      });
      Array.prototype.forEach.call(document.querySelectorAll('#streamBody .btn-stop'), function (b) {
        b.onclick = function () { act(b.dataset.id, 'stop'); };
      });
      Array.prototype.forEach.call(document.querySelectorAll('#streamBody .btn-evt'), function (b) {
        b.onclick = function () { curStream = b.dataset.id; $('evtTitle').textContent = '— ' + b.dataset.name; loadEvents(); };
      });
      Array.prototype.forEach.call(document.querySelectorAll('#streamBody .btn-del'), function (b) {
        b.onclick = function () {
          if (!window.confirm('删除该捕获流及其全部事件？')) { return; }
          api('DELETE', '/cdc/streams/' + b.dataset.id).then(function () {
            BKP.toast('已删除'); loadStreams();
          }).catch(function (e) { BKP.toast('删除失败: ' + e.message, 'danger'); });
        };
      });
    }).catch(function (e) { BKP.toast('加载失败: ' + e.message, 'danger'); });
  }

  function act(id, op) {
    api('POST', '/cdc/streams/' + id + '/' + op).then(function (r) {
      BKP.toast(r.message || '操作完成');
      setTimeout(loadStreams, 800);
    }).catch(function (e) { BKP.toast('操作失败: ' + e.message, 'danger'); });
  }

  // ---------------- 事件 ----------------
  function loadEvents() {
    if (!curStream) { BKP.toast('请先选择一个捕获流'); return; }
    var qs = '?limit=100&op=' + encodeURIComponent($('fOp').value) +
      '&table=' + encodeURIComponent($('fTable').value);
    api('GET', '/cdc/streams/' + curStream + '/events' + qs).then(function (r) {
      const pg = Pager.slice('cdcEvents', (r && r.events) || []);
      $('evtBody').innerHTML = pg.list.map(function (e) {
        return '<tr>' +
          '<td class="small text-nowrap">' + BKP.esc(e.event_time || '') + '</td>' +
          '<td>' + opBadge(e.op) + '</td>' +
          '<td class="small">' + BKP.esc(e.table_name || '') + '</td>' +
          '<td>' + jsonCell(e.pk) + '</td>' +
          '<td>' + jsonCell(e.before) + (e.before_partial ? '<span class="badge bg-warning text-dark ms-1" title="前镜像不完整：PG 需 ALTER TABLE ... REPLICA IDENTITY FULL">部分</span>' : '') + '</td>' +
          '<td>' + jsonCell(e.after) + '</td>' +
          '<td class="small text-muted">' + BKP.esc(e.position || '') + '</td></tr>';
      }).join('') || '<tr><td colspan="7" class="text-center text-muted py-3">暂无事件（确认流已启动且在源库执行了写操作）</td></tr>';
      Pager.attach('cdcEvents', $('evtBody'), pg.page, pg.pages, pg.total, loadEvents);
    }).catch(function (e) { BKP.toast('事件加载失败: ' + e.message, 'danger'); });
  }

  // ---------------- 回放 / 回滚 ----------------
  function replay(apply) {
    if (!curStream) { BKP.toast('请先选择一个捕获流'); return; }
    var body = {
      mode: $('rMode').value,
      since: $('rSince').value, until: $('rUntil').value,
      apply: !!apply
    };
    if (apply) {
      var host = $('rTHost').value.trim();
      if (!host) { BKP.toast('应用前请填写目标主机', 'danger'); return; }
      body.target = {
        db_type: $('rTType').value, host: host,
        port: parseInt($('rTPort').value || '0', 10),
        username: $('rTUser').value.trim(), password: $('rTPwd').value,
        db_name: $('rTDb').value.trim()
      };
      body.schema_name = $('rTDb').value.trim();
      if (!window.confirm('确认将 ' + $('rMode').value + ' 变更应用到目标库？该操作会真实写入数据。')) { return; }
      doReplay(body);
    } else {
      doReplay(body);
    }
  }

  function doReplay(body) {
    api('POST', '/cdc/streams/' + curStream + '/replay', body).then(function (r) {
      var box = $('sqlBox');
      box.style.display = 'block';
      box.textContent = (r.sqls || []).join(';\n') + ((r.sqls || []).length ? ';' : '');
      if (body.apply) {
        BKP.toast(r.ok ? ('已应用 ' + r.applied + ' 条') : ('应用失败: ' + (r.errors || []).join('; ')),
          r.ok ? 'dark' : 'danger', 6000);
      } else {
        BKP.toast('已生成 ' + r.sql_count + ' 条 SQL（基于 ' + r.events + ' 个事件）');
      }
    }).catch(function (e) { BKP.toast('失败: ' + e.message, 'danger'); });
  }

  // ---------------- 新建 ----------------
  function create() {
    var body = {
      name: $('nName').value.trim(), db_type: $('nType').value,
      host: $('nHost').value.trim(), port: parseInt($('nPort').value || '0', 10),
      username: $('nUser').value.trim(), password: $('nPwd').value,
      db_name: $('nDb').value.trim(), purpose: $('nPurpose').value,
      include_tables: $('nTables').value.trim()
    };
    if (!body.name || !body.host) { BKP.toast('名称与主机必填', 'danger'); return; }
    api('POST', '/cdc/streams', body).then(function (r) {
      BKP.toast('创建成功，正在启动…');
      return api('POST', '/cdc/streams/' + r.id + '/start');
    }).then(function (r) {
      BKP.toast(r.ok ? '已启动' : ('启动失败: ' + r.message), r.ok ? 'dark' : 'danger', 5000);
      bootstrap.Modal.getInstance($('newModal')).hide();
      loadStreams();
    }).catch(function (e) { BKP.toast('创建失败: ' + e.message, 'danger'); });
  }

  function probe() {
    var body = {
      db_type: $('nType').value, host: $('nHost').value.trim(),
      port: parseInt($('nPort').value || '0', 10),
      username: $('nUser').value.trim(), password: $('nPwd').value,
      db_name: $('nDb').value.trim()
    };
    $('probeMsg').textContent = '检查中…';
    api('POST', '/cdc/probe', body).then(function (r) {
      $('probeMsg').textContent = r.message || '';
      $('probeBox').innerHTML = (r.checks || []).map(function (c) {
        return '<div class="' + (c.ok ? 'text-success' : 'text-danger') + '">' +
          (c.ok ? '✔' : '✘') + ' ' + BKP.esc(c.item) + '：' + BKP.esc(c.message || '') + '</div>';
      }).join('');
    }).catch(function (e) { $('probeMsg').textContent = '检查失败: ' + e.message; });
  }

  // ---------------- 初始化 ----------------
  // 本模块可能被嵌入「实时备份」合并页的 CDC 标签页，需在面板真正可见时
  // 才去拉数据；这里以 #capBox 是否存在判断面板是否在页面上。
  function init() {
    if (!$('capBox')) return;
    api('GET', '/cdc/capabilities').then(function (r) {
      $('capBox').innerHTML = '<i class="bi bi-info-circle"></i> 平台侧客户端：mysqlbinlog ' +
        (r.mysqlbinlog ? '<span class="text-success">可用</span>' : '<span class="text-danger">缺失</span>') +
        ' · pg_recvlogical ' + (r.pg_recvlogical ? '<span class="text-success">可用</span>' : '<span class="text-danger">缺失</span>') +
        ' · 支持类型：' + (r.supported || []).join('/');
    }).catch(function (e) { $('capBox').innerHTML = '<i class="bi bi-exclamation-triangle"></i> 能力检测失败: ' + BKP.esc(e.message); });
    $('btnNew').onclick = function () { bootstrap.Modal.getOrCreateInstance($('newModal')).show(); };
    $('btnCreate').onclick = create;
    $('btnProbe').onclick = probe;
    $('btnRefresh').onclick = loadStreams;
    $('btnLoadEvt').onclick = loadEvents;
    $('btnPreview').onclick = function () { replay(false); };
    $('btnApply').onclick = function () { replay(true); };
    loadStreams();
    // 面板隐藏时不轮询，避免合并页空跑请求
    setInterval(function () {
      var pane = $('tabCdc');
      if (pane && pane.offsetParent === null) return;
      if (document.hidden) return;
      loadStreams();
    }, 10000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
