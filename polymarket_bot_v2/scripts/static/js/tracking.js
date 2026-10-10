
var _trkTriggerLabels = {
  'floor':               '🔻 地板止损',
  'live_floor':          '🔻 直播地板',
  'standard_sl':         '📉 标准止损',
  'live_standard_sl':    '📉 直播止损',
  'reversal':            '🔄 翻转止损',
  'live_reversal':       '🔄 直播翻转',
  'expiry_sl':           '⏰ 到期止损',
  'sports_expired':      '⚽ 赛后清仓',
  'effective_resolution':'💀 实质结算',
  'profit_take':         '✅ 止盈',
  'ceiling':             '🚀 天花板止盈',
  'live_ceiling':        '🚀 直播天花板',
  'trailing':            '📈 Trailing止盈',
  'floor_protection':    '🛡️ 地板保护',
  'standard_profit_take':'✅ 标准止盈',
  'quick_exit':          '⚡ 快速止盈',
  'weather_quick':       '⛅ 天气快速止盈',
  'expiry':              '⏰ 到期止盈',
  'live_standard':       '⚽ 直播止盈',
  'weather_rotation_exit':'🔄 天气轮动退出',
  'certainty_sell':      '💯 确定性卖出',
  'legacy_cleanup':      '🧹 遗留清仓',
};
var _trkCatIcons = {
  weather:'🌤', sports:'⚽', crypto:'₿', geopolitics:'🌍',
  finance:'💰', pop_culture:'🎭', tech:'📊', economy:'📊'
};

var _trkCharts = { pie: null, bar: null, line: null };

function _trkDestroyChart(key) {
  if (_trkCharts[key]) {
    try { _trkCharts[key].destroy(); } catch(e) {}
    _trkCharts[key] = null;
  }
}

function fetchTrackingData() {
  var days = parseInt((document.getElementById('trk-days') || {}).value || 30);
  var evtType = (document.getElementById('trk-event-type') || {}).value || '';
  var cat = (document.getElementById('trk-category') || {}).value || '';

  var summaryP = api('/api/tracking/summary?days=' + days);
  var eventsP = api(
    '/api/tracking/events?days=' + days +
    '&event_type=' + encodeURIComponent(evtType) +
    '&category=' + encodeURIComponent(cat) +
    '&limit=200'
  );

  summaryP.then(function(d) {
    _renderTrkSummaryCards(d);
    _renderTrkCharts(d);
  }).catch(function(e) { console.warn('[Tracking] summary error:', e); });

  eventsP.then(function(d) {
    _renderTrkEventTable(d.events || []);
  }).catch(function(e) { console.warn('[Tracking] events error:', e); });
}

function _renderTrkSummaryCards(d) {
  var sl = d.stop_loss || {};
  var pt = d.profit_take || {};
  var combined = d.combined || {};

  var totalExits = combined.total || 0;
  var totalPnl = combined.total_pnl || 0;
  var slCnt = sl.total || 0;
  var ptCnt = pt.total || 0;
  var winRate = combined.win_rate || 0;

  _setTxt('trk-total-exits', totalExits);
  _setTxt('trk-sl-count', slCnt);
  _setTxt('trk-pt-count', ptCnt);

  var pnlEl = document.getElementById('trk-total-pnl');
  if (pnlEl) {
    pnlEl.textContent = (totalPnl >= 0 ? '+' : '') + totalPnl.toFixed(2);
    pnlEl.className = 'text-3xl font-bold ' + (totalPnl >= 0 ? 'text-green' : 'text-red');
  }

  var wrEl = document.getElementById('trk-win-rate');
  if (wrEl) {
    wrEl.textContent = (winRate * 100).toFixed(1) + '%';
    wrEl.className = 'text-3xl font-bold ' + (winRate >= 0.5 ? 'text-green' : 'text-red');
  }

  var slPnlEl = document.getElementById('trk-sl-pnl');
  if (slPnlEl) {
    var sp = sl.total_pnl || 0;
    slPnlEl.textContent = (sp >= 0 ? '+' : '') + sp.toFixed(2);
    slPnlEl.className = 'text-lg font-bold ' + (sp >= 0 ? 'text-green' : 'text-red');
  }
  var ptPnlEl = document.getElementById('trk-pt-pnl');
  if (ptPnlEl) {
    var pp = pt.total_pnl || 0;
    ptPnlEl.textContent = (pp >= 0 ? '+' : '') + pp.toFixed(2);
    ptPnlEl.className = 'text-lg font-bold ' + (pp >= 0 ? 'text-green' : 'text-red');
  }
}

function _setTxt(id, val) {
  var el = document.getElementById(id);
  if (el) el.textContent = val;
}

function _chartDefaults() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: '#94a3b8', font: { size: 11 } } },
      tooltip: { callbacks: {} }
    }
  };
}

function _renderTrkCharts(d) {
  _renderTrkPieChart(d);
  _renderTrkBarChart(d);
  _renderTrkLineChart(d);
}

function _renderTrkPieChart(d) {
  var canvas = document.getElementById('trk-pie-chart');
  if (!canvas) return;
  _trkDestroyChart('pie');
  var sl = (d.stop_loss || {}).total || 0;
  var pt = (d.profit_take || {}).total || 0;
  if (sl + pt === 0) { canvas.getContext('2d'); return; }
  var opts = _chartDefaults();
  opts.plugins.legend.position = 'bottom';
  _trkCharts.pie = new Chart(canvas, {
    type: 'doughnut',
    data: {
      labels: ['🛑 止损 (' + sl + ')', '✅ 止盈 (' + pt + ')'],
      datasets: [{
        data: [sl, pt],
        backgroundColor: ['#ef4444cc', '#22c55ecc'],
        borderColor: ['#ef4444', '#22c55e'],
        borderWidth: 2,
        hoverOffset: 6,
      }]
    },
    options: opts
  });
}

function _renderTrkBarChart(d) {
  var canvas = document.getElementById('trk-bar-chart');
  if (!canvas) return;
  _trkDestroyChart('bar');

  var triggers = {};
  ((d.stop_loss || {}).by_trigger || []).forEach(function(r) {
    var lbl = _trkTriggerLabels[r.trigger_type] || r.trigger_type;
    triggers[lbl] = { cnt: r.cnt, pnl: r.total_pnl || 0, type: 'sl' };
  });
  var ptCnt = (d.profit_take || {}).total || 0;
  var ptPnl = (d.profit_take || {}).total_pnl || 0;
  if (ptCnt > 0) {
    triggers['✅ 止盈'] = { cnt: ptCnt, pnl: ptPnl, type: 'pt' };
  }

  var labels = Object.keys(triggers);
  if (labels.length === 0) return;

  var cnts = labels.map(function(l) { return triggers[l].cnt; });
  var pnls = labels.map(function(l) { return triggers[l].pnl; });
  var colors = labels.map(function(l) {
    return triggers[l].type === 'pt' ? '#22c55e99' : '#ef444499';
  });
  var borderColors = labels.map(function(l) {
    return triggers[l].type === 'pt' ? '#22c55e' : '#ef4444';
  });

  var opts = _chartDefaults();
  opts.scales = {
    x: { ticks: { color: '#64748b', font: { size: 9 } }, grid: { color: '#2a2d3a' } },
    y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#2a2d3a' }, beginAtZero: true }
  };
  opts.plugins.legend.display = false;
  opts.plugins.tooltip.callbacks.afterLabel = function(ctx) {
    var lbl = labels[ctx.dataIndex];
    var pnl = triggers[lbl].pnl;
    return 'PnL: ' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2);
  };

  _trkCharts.bar = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [{
        label: '次数',
        data: cnts,
        backgroundColor: colors,
        borderColor: borderColors,
        borderWidth: 1,
        borderRadius: 4,
      }]
    },
    options: opts
  });
}

function _renderTrkLineChart(d) {
  var canvas = document.getElementById('trk-line-chart');
  if (!canvas) return;
  _trkDestroyChart('line');
  var ts = d.time_series || [];
  if (ts.length === 0) return;

  var labels = ts.map(function(r) { return r.day.slice(5); });
  var slData = ts.map(function(r) { return r.sl_cnt || 0; });
  var ptData = ts.map(function(r) { return r.pt_cnt || 0; });

  var opts = _chartDefaults();
  opts.scales = {
    x: { ticks: { color: '#64748b', font: { size: 9 } }, grid: { color: '#2a2d3a22' } },
    y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#2a2d3a' }, beginAtZero: true }
  };
  opts.plugins.legend.position = 'bottom';

  _trkCharts.line = new Chart(canvas, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: '止损',
          data: slData,
          borderColor: '#ef4444',
          backgroundColor: '#ef444420',
          tension: 0.3,
          fill: true,
          pointRadius: 3,
        },
        {
          label: '止盈',
          data: ptData,
          borderColor: '#22c55e',
          backgroundColor: '#22c55e20',
          tension: 0.3,
          fill: true,
          pointRadius: 3,
        }
      ]
    },
    options: opts
  });
}

var _trkExpandedRows = {};
var _trkAllEvents = [];
var _trkSortKey = 'timestamp';
var _trkSortAsc = false;
var _trkPage = 0;
var _trkPageSize = 25;

function _trkSortData(events) {
  if (!_trkSortKey) return events.slice();
  return events.slice().sort(function(a, b) {
    var av = a[_trkSortKey], bv = b[_trkSortKey];
    if (av == null) av = _trkSortAsc ? '\uffff' : '';
    if (bv == null) bv = _trkSortAsc ? '\uffff' : '';
    var cmp = av < bv ? -1 : av > bv ? 1 : 0;
    return _trkSortAsc ? cmp : -cmp;
  });
}

function _trkSetSort(key) {
  if (_trkSortKey === key) {
    _trkSortAsc = !_trkSortAsc;
  } else {
    _trkSortKey = key;
    _trkSortAsc = key === 'timestamp' ? false : true;
  }
  _trkPage = 0;
  _renderTrkPagedTable();
}

function _renderTrkPagedTable() {
  var sorted = _trkSortData(_trkAllEvents);
  var total = sorted.length;
  var start = _trkPage * _trkPageSize;
  var end = Math.min(start + _trkPageSize, total);
  var pageEvents = sorted.slice(start, end);
  _renderTrkEventRows(pageEvents, start);
  _renderTrkPagination(total);
  ['timestamp','event_type','category','trigger_type','return_pct','pnl'].forEach(function(k) {
    var th = document.getElementById('trk-th-' + k);
    if (!th) return;
    var icon = th.querySelector('.sort-arrow');
    if (!icon) return;
    if (_trkSortKey === k) {
      icon.textContent = _trkSortAsc ? ' ▲' : ' ▼';
      icon.style.color = '#3b82f6';
    } else {
      icon.textContent = ' ⇅';
      icon.style.color = '#475569';
    }
  });
}

function _renderTrkPagination(total) {
  var container = document.getElementById('trk-pagination');
  if (!container) return;
  var totalPages = Math.max(1, Math.ceil(total / _trkPageSize));
  var html = '<div class="flex items-center gap-2 flex-wrap">';
  html += '<span class="text-xs text-gray-500">' + total + ' 条记录</span>';
  html += '<select class="text-xs bg-gray-800 border border-gray-700 rounded px-2 py-1 text-white" onchange="_trkPageSizeChange(this)">';
  [25, 50, 100].forEach(function(sz) {
    html += '<option value="' + sz + '"' + (_trkPageSize === sz ? ' selected' : '') + '>' + sz + '/页</option>';
  });
  html += '</select>';
  html += '<button class="px-2 py-1 text-xs text-gray-400 bg-gray-800 border border-gray-700 rounded hover:text-white disabled:opacity-40" '
    + (_trkPage === 0 ? 'disabled' : 'onclick="_trkGoPage(' + (_trkPage - 1) + ')"') + '>«</button>';
  var lo = Math.max(0, _trkPage - 2);
  var hi = Math.min(totalPages - 1, _trkPage + 2);
  for (var p = lo; p <= hi; p++) {
    html += '<button class="px-2 py-1 text-xs rounded border '
      + (p === _trkPage ? 'bg-accent text-white border-accent' : 'text-gray-400 bg-gray-800 border-gray-700 hover:text-white')
      + '" onclick="_trkGoPage(' + p + ')">' + (p + 1) + '</button>';
  }
  html += '<button class="px-2 py-1 text-xs text-gray-400 bg-gray-800 border border-gray-700 rounded hover:text-white disabled:opacity-40" '
    + (_trkPage >= totalPages - 1 ? 'disabled' : 'onclick="_trkGoPage(' + (_trkPage + 1) + ')"') + '>»</button>';
  html += '</div>';
  container.innerHTML = html;
}

function _trkPageSizeChange(sel) {
  _trkPageSize = parseInt(sel.value);
  _trkPage = 0;
  _renderTrkPagedTable();
}

function _trkGoPage(p) {
  _trkPage = p;
  _renderTrkPagedTable();
}

function _renderTrkEventTable(events) {
  _trkAllEvents = events;
  _trkPage = 0;
  var tbody = document.getElementById('trk-events-body');
  if (!tbody) return;
  if (events.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="text-gray-500 text-center py-6">暂无卖出事件</td></tr>';
    var pg = document.getElementById('trk-pagination');
    if (pg) pg.innerHTML = '';
    _renderTrkSortHeaders();
    return;
  }
  _renderTrkSortHeaders();
  _renderTrkPagedTable();
}

function _renderTrkSortHeaders() {
  var cols = [
    {id:'timestamp', label:'时间'},
    {id:'event_type', label:'类型'},
    {id:null, label:'市场'},
    {id:'category', label:'赛道'},
    {id:'trigger_type', label:'触发策略'},
    {id:null, label:'入场价'},
    {id:null, label:'退场价'},
    {id:'return_pct', label:'回报'},
    {id:'pnl', label:'PnL'},
  ];
  var thead = document.querySelector('#trk-events-table thead tr');
  if (!thead) return;
  thead.innerHTML = cols.map(function(c) {
    if (!c.id) return '<th class="py-2 px-3">' + c.label + '</th>';
    var active = _trkSortKey === c.id;
    var arrow = active ? (_trkSortAsc ? ' ▲' : ' ▼') : ' ⇅';
    var arrowColor = active ? '#3b82f6' : '#475569';
    return '<th id="trk-th-' + c.id + '" class="py-2 px-3 cursor-pointer select-none hover:text-white" onclick="_trkSetSort(\'' + c.id + '\')">'
      + c.label + '<span class="sort-arrow text-xs" style="color:' + arrowColor + '">' + arrow + '</span></th>';
  }).join('');
}

function _renderTrkEventRows(pageEvents, startOffset) {
  var tbody = document.getElementById('trk-events-body');
  if (!tbody) return;
  tbody.innerHTML = pageEvents.map(function(ev, localIdx) {
    var globalIdx = startOffset + localIdx;
    var isSL = ev.event_type === 'stop_loss';
    var ts = (ev.timestamp || '').replace('T', ' ').slice(5, 16);
    var q = escHtml((ev.question || '').slice(0, 40));
    var icon = _trkCatIcons[ev.category] || '📦';
    var trigLabel = _trkTriggerLabels[ev.trigger_type] || ev.trigger_type;
    var ret = ((ev.return_pct || 0) * 100).toFixed(1);
    var pnl = ev.pnl != null ? (ev.pnl >= 0 ? '+' : '') + ev.pnl.toFixed(2) : '-';
    var typeColor = isSL ? 'text-red-400' : 'text-green-400';
    var typeLabel = isSL ? '🛑 止损' : '✅ 止盈';
    var rowId = 'trk-row-' + globalIdx;
    var detailId = 'trk-detail-' + globalIdx;

    return (
      '<tr id="' + rowId + '" class="border-b border-gray-800 hover:bg-gray-800/40 cursor-pointer"' +
        ' onclick="trkToggleDetail(' + globalIdx + ')">' +
        '<td class="py-2 px-3 text-gray-400 whitespace-nowrap text-xs">' + ts + '</td>' +
        '<td class="py-2 px-3 ' + typeColor + ' font-semibold text-xs whitespace-nowrap">' + typeLabel + '</td>' +
        '<td class="py-2 px-3 text-gray-300 text-xs max-w-xs truncate" title="' + escHtml(ev.question||'') + '">' + q + '…</td>' +
        '<td class="py-2 px-3 text-xs">' + icon + ' <span class="text-gray-400">' + escHtml(ev.category||'') + '</span></td>' +
        '<td class="py-2 px-3 text-gray-300 text-xs">' + escHtml(trigLabel) + '</td>' +
        '<td class="py-2 px-3 text-right text-gray-300 text-xs">' + (ev.entry_price||0).toFixed(3) + '</td>' +
        '<td class="py-2 px-3 text-right text-gray-300 text-xs">' + (ev.exit_price||0).toFixed(3) + '</td>' +
        '<td class="py-2 px-3 text-right text-xs ' + (ev.return_pct >= 0 ? 'text-green' : 'text-red') + '">' + ret + '%</td>' +
        '<td class="py-2 px-3 text-right text-xs ' + ((ev.pnl||0) >= 0 ? 'text-green' : 'text-red') + '">' + pnl + '</td>' +
      '</tr>' +
      '<tr id="' + detailId + '" style="display:none" class="bg-gray-900/60">' +
        '<td colspan="9" class="px-4 py-3">' +
          '<div id="' + detailId + '-content"></div>' +
        '</td>' +
      '</tr>'
    );
  }).join('');
}

function trkToggleDetail(globalIdx) {
  var ev = _trkAllEvents[globalIdx];
  if (!ev) return;
  var detailRow = document.getElementById('trk-detail-' + globalIdx);
  if (!detailRow) return;
  var open = detailRow.style.display !== 'none';
  if (open) {
    detailRow.style.display = 'none';
    return;
  }
  var content = document.getElementById('trk-detail-' + globalIdx + '-content');
  if (content) {
    var isSL = ev.event_type === 'stop_loss';
    var conditions = {};
    var context = {};
    try { conditions = JSON.parse(ev.conditions || '{}'); } catch(e) {}
    try { context = JSON.parse(ev.context || '{}'); } catch(e) {}

    var rows = [
      ['策略链路 (policy_trace)', escHtml(_trkTriggerLabels[ev.policy_trace] || ev.policy_trace || '-')],
      ['持仓大小 (USDC)', ev.size_usdc ? ev.size_usdc.toFixed(4) : '-'],
      ['入场价', ev.entry_price ? ev.entry_price.toFixed(6) : '-'],
      ['退场价', ev.exit_price ? ev.exit_price.toFixed(6) : '-'],
      ['回报率', ((ev.return_pct||0)*100).toFixed(2) + '%'],
      ['PnL', ev.pnl != null ? (ev.pnl >= 0 ? '+' : '') + ev.pnl.toFixed(4) : '-'],
    ];

    Object.keys(conditions).forEach(function(k) {
      rows.push(['条件.' + k, escHtml(String(conditions[k]))]);
    });
    Object.keys(context).forEach(function(k) {
      rows.push(['上下文.' + k, escHtml(String(context[k]))]);
    });

    content.innerHTML =
      '<div class="grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-1">' +
      rows.map(function(r) {
        return '<div class="flex flex-col">' +
          '<span class="text-xs text-gray-500 uppercase tracking-wide">' + r[0] + '</span>' +
          '<span class="text-xs text-gray-200 font-medium">' + r[1] + '</span>' +
          '</div>';
      }).join('') +
      '</div>' +
      (ev.question ? '<div class="mt-2 text-xs text-gray-400 border-t border-gray-700 pt-2">' + escHtml(ev.question) + '</div>' : '');
  }
  detailRow.style.display = '';
}

function trkApplyFilters() {
  fetchTrackingData();
}

(function() {
  var _origSwitch = window.switchTab;
  if (typeof _origSwitch === 'function') {
    window.switchTab = function(tabName) {
      _origSwitch(tabName);
      if (tabName === 'tracking') {
        fetchTrackingData();
        if (typeof fetchMakerStats === 'function') fetchMakerStats();
      }
    };
  }
})();
