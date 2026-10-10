
var _ctSelectedWallets = {};
var _ctSort = { field: 'secondary_score', dir: 'desc' };
var _ctTaskSort = { field: 'created_at', dir: 'desc' };
var _ctTraders = [];
var _ctTasks = [];
var _ctTagOptions = [];

var CT_STATUS_LABELS = {
  'running': '🟢 运行中',
  'paused':  '⏸️ 已暂停',
  'stopped': '⏹️ 已停止'
};

function initCopytrade() {
  loadCopytradeConfig();
  loadCopyTasks();
}

function loadCopytradeConfig(callback) {
  api('/api/polyhub/config').then(function(cfg) {
    var tags = Array.isArray(cfg.tag_focus) ? cfg.tag_focus : ['Weather', 'Sports', 'Crypto'];
    _ctTagOptions = tags;
    var sel = document.getElementById('ct-tag-select');
    if (sel) {
      sel.innerHTML = '';
      tags.forEach(function(tag) {
        var opt = document.createElement('option');
        opt.value = tag;
        opt.textContent = tag;
        sel.appendChild(opt);
      });
      if (tags.length) sel.value = tags[0];
    }
    var setVal = function(id, v) {
      var el = document.getElementById(id);
      if (el && v !== undefined && v !== null) el.value = v;
    };
    setVal('ct-pnl-min', cfg.pnl_min);
    setVal('ct-trades-min', cfg.trades_min);
    setVal('ct-ev-min', cfg.ev_min);
    setVal('ct-timing-min', cfg.timing_min);
    setVal('ct-min-score', cfg.min_score);
    var cbBots = document.getElementById('ct-filter-bots');
    if (cbBots) cbBots.checked = !!cfg.exclude_bots;
    var tw = document.getElementById('ct-time-range');
    if (tw && cfg.default_time_window) tw.value = cfg.default_time_window;
    if (callback) callback();
  }).catch(function(e) {
    console.warn('[CopyTrade] config加载失败，使用硬编码默认值:', e);
    if (callback) callback();
  });
}

function discoverQuery() {
  var tag = _ctVal('ct-tag-select') || 'Weather';
  var timeRange = _ctVal('ct-time-range') || '30d';
  var pnlMin = parseFloat(_ctVal('ct-pnl-min') || '1000');
  var tradesMin = parseInt(_ctVal('ct-trades-min') || '0');
  var evMin = parseFloat(_ctVal('ct-ev-min') || '0.005');
  var timingMin = parseFloat(_ctVal('ct-timing-min') || '0');
  var filterBots = _ctChecked('ct-filter-bots') ? 1 : 0;
  var minScore = parseFloat(_ctVal('ct-min-score') || '0');

  var params = [
    'tag=' + encodeURIComponent(tag),
    'time_range=' + timeRange,
    'pnl_min=' + pnlMin,
    'trade_count_30_min=' + tradesMin,
    'ev_per_bought_min=' + evMin,
    'timing_score_min=' + timingMin,
    'filter_bots=' + filterBots,
    'min_score=' + minScore,
    'limit=50'
  ].join('&');

  _ctSetLoading('ct-results-body', true);

  api('/api/polyhub/discover?' + params).then(function(d) {
    _ctTraders = d.traders || [];
    var warnEl = document.getElementById('ct-tag-warning');
    if (warnEl) {
      if (d.warning) {
        warnEl.textContent = '⚠️ ' + d.warning;
        warnEl.classList.remove('hidden');
      } else {
        warnEl.classList.add('hidden');
      }
    }
    _renderDiscoverTable();
  }).catch(function(e) {
    _ctShowError('ct-results-container', '发现查询失败: ' + e.message);
  });
}

function walletQuery() {
  var input = document.getElementById('ct-wallet-input');
  var addr = (input ? input.value.trim() : '').toLowerCase();
  if (!addr) return;
  if (!addr.startsWith('0x') || addr.length < 10) {
    _ctShowError('ct-results-container', '钱包地址格式无效，必须以 0x 开头');
    return;
  }
  var exists = _ctTraders.some(function(t) {
    return (t.user_id || t.address || '').toLowerCase() === addr;
  });
  if (exists) {
    _ctShowError('ct-results-container', '该钱包已在排行中，无需重复查询');
    return;
  }

  _ctSetLoading('ct-results-body', true);
  api('/api/polyhub/trader?user_id=' + encodeURIComponent(addr)).then(function(trader) {
    if (!trader || (!trader.user_id && !trader.address)) {
      _ctShowError('ct-results-container', '未找到该钱包的交易数据');
      return;
    }
    trader._manual_query = true;
    _ctTraders.unshift(trader);
    _renderDiscoverTable();
    if (input) input.value = '';
    var warnEl = document.getElementById('ct-tag-warning');
    if (warnEl) warnEl.classList.add('hidden');
  }).catch(function(e) {
    _ctShowError('ct-results-container', '钱包查询失败: ' + e.message);
  });
}

function _renderDiscoverTable() {
  var sorted = _ctSortArray(_ctTraders, _ctSort.field, _ctSort.dir);
  var tbody = document.getElementById('ct-results-body');
  if (!tbody) return;

  if (!sorted.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="text-center text-gray-400 py-6">暂无符合条件的交易者</td></tr>';
    return;
  }

  tbody.innerHTML = sorted.map(function(t) {
    var addr = (t.user_id || t.address || '').toLowerCase();
    var pnl = parseFloat(t.pnl || 0);
    var ev = parseFloat(t.ev_per_bought || 0);
    var timing = parseFloat(t.timing_score || 50);
    var trades30 = parseInt(t.trade_count_30 || t.trade_count_30d || 0);
    var score = parseFloat(t.secondary_score || 0);
    var checked = !!_ctSelectedWallets[addr];

    var scoreColor = score >= 80 ? 'text-emerald-400' : score >= 60 ? 'text-yellow-400' : 'text-gray-400';
    var pnlColor = pnl >= 0 ? 'text-emerald-400' : 'text-red-400';
    var manualBadge = t._manual_query ? ' <span class="px-1 py-0.5 rounded text-[10px] bg-purple-700 text-purple-200">手动查询</span>' : '';
    var addrHtml = addr
      ? '<a href="https://polymarket.com/' + addr + '" target="_blank" rel="noopener" ' +
          'class="font-mono text-xs text-cyan-300 hover:text-cyan-100 break-all" ' +
          'title="点击在Polymarket查看">' + addr + '</a>' + manualBadge
      : '—';

    return '<tr class="hover:bg-slate-700/30 transition-colors">' +
      '<td class="px-3 py-2"><input type="checkbox"' + (checked ? ' checked' : '') +
        ' onchange="ctToggleSelect(\'' + addr + '\', this.checked)"' +
        ' class="w-4 h-4 accent-cyan-400"></td>' +
      '<td class="px-3 py-2" style="max-width:180px;word-break:break-all">' + addrHtml + '</td>' +
      '<td class="px-3 py-2 text-right ' + pnlColor + '">$' + _ctFmt(pnl, 0) + '</td>' +
      '<td class="px-3 py-2 text-right text-sky-300">' + _ctFmt(ev, 3) + '</td>' +
      '<td class="px-3 py-2 text-right text-purple-300">' + _ctFmt(timing, 1) + '</td>' +
      '<td class="px-3 py-2 text-right text-gray-300">' + trades30 + '</td>' +
      '<td class="px-3 py-2 text-center">' +
        '<span class="px-2 py-0.5 rounded text-xs bg-slate-700 text-gray-300">' + (t.tag || '—') + '</span>' +
      '</td>' +
      '<td class="px-3 py-2 text-right"><span class="font-bold ' + scoreColor + '">' + _ctFmt(score, 1) + '</span></td>' +
      '<td class="px-3 py-2 text-center">' +
        '<button onclick="ctQuickFollow(\'' + addr + '\')" ' +
          'class="px-2 py-1 rounded text-xs bg-cyan-700 hover:bg-cyan-600 text-white">跟投</button>' +
      '</td>' +
    '</tr>';
  }).join('');

  _ctUpdateFloatingBar();
}

function ctSortDiscover(field) {
  if (_ctSort.field === field) {
    _ctSort.dir = _ctSort.dir === 'desc' ? 'asc' : 'desc';
  } else {
    _ctSort.field = field;
    _ctSort.dir = 'desc';
  }
  _renderDiscoverTable();
}

function ctToggleSelect(addr, checked) {
  if (checked) {
    var trader = _ctTraders.find(function(t) {
      return (t.user_id || t.address || '').toLowerCase() === addr;
    });
    _ctSelectedWallets[addr] = trader || { user_id: addr };
  } else {
    delete _ctSelectedWallets[addr];
  }
  _ctUpdateFloatingBar();
}

function ctSelectAll(checked) {
  _ctTraders.forEach(function(t) {
    var addr = (t.user_id || t.address || '').toLowerCase();
    if (checked) {
      _ctSelectedWallets[addr] = t;
    } else {
      delete _ctSelectedWallets[addr];
    }
  });
  document.querySelectorAll('#ct-results-body input[type=checkbox]').forEach(function(cb) {
    cb.checked = checked;
  });
  _ctUpdateFloatingBar();
}

function _ctUpdateFloatingBar() {
  var count = Object.keys(_ctSelectedWallets).length;
  var bar = document.getElementById('ct-floating-bar');
  var countEl = document.getElementById('ct-selected-count');
  if (!bar) return;
  if (count > 0) {
    bar.classList.remove('hidden');
    if (countEl) countEl.textContent = count;
  } else {
    bar.classList.add('hidden');
  }
}

function ctQuickFollow(addr) {
  var trader = _ctTraders.find(function(t) {
    return (t.user_id || t.address || '').toLowerCase() === addr;
  });
  _ctSelectedWallets[addr] = trader || { user_id: addr };
  _ctUpdateFloatingBar();
  document.querySelectorAll('#ct-results-body input[type=checkbox]').forEach(function(cb) {
    var row = cb.closest('tr');
    if (row) {
      var link = row.querySelector('a[href*="' + addr + '"]');
      if (link) cb.checked = true;
    }
  });
  ctShowCreateModal();
}

function ctShowCreateModal() {
  var wallets = Object.keys(_ctSelectedWallets);
  if (!wallets.length) { alert('请先勾选至少一个交易者'); return; }

  var modal = document.getElementById('ct-create-modal');
  var listEl = document.getElementById('ct-modal-wallet-list');
  if (!modal || !listEl) return;

  listEl.innerHTML = wallets.map(function(w) {
    var t = _ctSelectedWallets[w];
    var short = w.slice(0, 8) + '…' + w.slice(-4);
    return '<div class="flex items-center gap-2 text-sm py-1 border-b border-slate-700">' +
      '<span class="font-mono text-cyan-300">' + short + '</span>' +
      '<span class="text-gray-400 ml-auto">分=' + _ctFmt(t.secondary_score || 0, 1) + ' | ' + (t.tag || '—') + '</span>' +
    '</div>';
  }).join('');

  modal.classList.remove('hidden');
}

function ctCloseCreateModal() {
  var modal = document.getElementById('ct-create-modal');
  if (modal) modal.classList.add('hidden');
}

function ctConfirmCreateTasks() {
  var wallets = Object.keys(_ctSelectedWallets);
  if (!wallets.length) return;

  var baseUsdc = parseFloat(document.getElementById('ct-modal-base-usdc').value || '10');
  var niched = document.getElementById('ct-modal-niche-only').checked;
  var scoreThresh = parseFloat(document.getElementById('ct-modal-score-thresh').value || '75');

  var catEls = document.querySelectorAll('#ct-modal-categories input[type=checkbox]:checked');
  var categories = Array.from(catEls).map(function(el) { return el.value; });

  var promises = wallets.map(function(wallet) {
    var trader = _ctSelectedWallets[wallet];
    return api('/api/copy-tasks', {
      method: 'POST',
      body: JSON.stringify({
        wallet_address: wallet,
        categories: categories,
        base_usdc: baseUsdc,
        niche_only: niched,
        score_threshold: scoreThresh,
        tag_label: trader ? (trader.tag || '') : ''
      })
    }).catch(function(e) { return { error: e.message }; });
  });

  Promise.all(promises).then(function(results) {
    var ok = results.filter(function(r) { return r && r.ok; }).length;
    var fail = results.length - ok;
    ctCloseCreateModal();
    _ctSelectedWallets = {};
    _ctUpdateFloatingBar();
    var msg = '✅ 成功创建 ' + ok + ' 个跟投任务';
    if (fail) msg += '，' + fail + ' 个失败（钱包已在运行中）';
    _ctToast(msg, fail ? 'warn' : 'ok');
    loadCopyTasks();
  });
}

function loadCopyTasks() {
  api('/api/copy-tasks').then(function(d) {
    _ctTasks = d.tasks || [];
    _renderTaskTable();
  }).catch(function(e) {
    _ctShowError('ct-tasks-container', '任务列表加载失败: ' + e.message);
  });
}

function _renderTaskTable() {
  var sorted = _ctSortArray(_ctTasks, _ctTaskSort.field, _ctTaskSort.dir);
  var tbody = document.getElementById('ct-tasks-body');
  if (!tbody) return;

  if (!sorted.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-center text-gray-400 py-8">暂无跟投任务。在上方发现优质交易者后勾选并创建。</td></tr>';
    return;
  }

  tbody.innerHTML = sorted.map(function(task) {
    var short = task.wallet_address.slice(0, 8) + '…' + task.wallet_address.slice(-4);
    var shortLink = '<a href="https://polymarket.com/profile/' + task.wallet_address + '?tab=activity" ' +
      'target="_blank" rel="noopener" title="在 Polymarket 查看该钱包交易记录" ' +
      'class="text-cyan-300 hover:text-cyan-100 hover:underline font-mono">' + short + '</a>';
    var statusHtml = CT_STATUS_LABELS[task.status] || task.status;
    var categories = [];
    try { categories = JSON.parse(task.categories || '[]'); } catch(e) {}
    var catStr = categories.length ? categories.join(', ') : (task.tag_label || '—');
    var lastActive = task.last_active_at
      ? new Date(task.last_active_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false})
      : '—';
    var errBadge = task.error_count > 0
      ? '<span class="ml-1 px-1 rounded text-xs bg-red-900 text-red-300">err×' + task.error_count + '</span>'
      : '';

    var canPause  = task.status === 'running';
    var canResume = task.status === 'paused';
    var btns =
      (canPause  ? '<button onclick="ctPauseTask(' + task.id + ')" class="px-2 py-1 rounded text-xs bg-yellow-700 hover:bg-yellow-600 mr-1">暂停</button>' : '') +
      (canResume ? '<button onclick="ctResumeTask(' + task.id + ')" class="px-2 py-1 rounded text-xs bg-emerald-700 hover:bg-emerald-600 mr-1">恢复</button>' : '') +
      '<button onclick="ctEditTask(' + task.id + ')" class="px-2 py-1 rounded text-xs bg-blue-700 hover:bg-blue-600 mr-1">编辑</button>' +
      '<button onclick="ctDeleteTask(' + task.id + ')" class="px-2 py-1 rounded text-xs bg-red-800 hover:bg-red-700">删除</button>';

    return '<tr class="hover:bg-slate-700/30 transition-colors">' +
      '<td class="px-3 py-2 text-xs">' + shortLink + errBadge + '</td>' +
      '<td class="px-3 py-2 text-sm">' + statusHtml + '</td>' +
      '<td class="px-3 py-2 text-right text-gray-300">$' + _ctFmt(task.base_usdc, 1) + '</td>' +
      '<td class="px-3 py-2 text-sm text-gray-300">' + catStr + '</td>' +
      '<td class="px-3 py-2 text-right text-gray-300">' + (task.total_signals || 0) + '</td>' +
      '<td class="px-3 py-2 text-xs text-gray-400">' + lastActive + '</td>' +
      '<td class="px-3 py-2 text-sm">' + btns + '</td>' +
    '</tr>';
  }).join('');
}

function ctSortTasks(field) {
  if (_ctTaskSort.field === field) {
    _ctTaskSort.dir = _ctTaskSort.dir === 'desc' ? 'asc' : 'desc';
  } else {
    _ctTaskSort.field = field;
    _ctTaskSort.dir = 'desc';
  }
  _renderTaskTable();
}

function ctPauseTask(id) {
  api('/api/copy-tasks/' + id, { method: 'PATCH', body: JSON.stringify({status: 'paused'}) })
    .then(function() { _ctToast('已暂停', 'ok'); loadCopyTasks(); })
    .catch(function(e) { _ctToast('暂停失败: ' + e.message, 'err'); });
}

function ctResumeTask(id) {
  api('/api/copy-tasks/' + id, { method: 'PATCH', body: JSON.stringify({status: 'running'}) })
    .then(function() { _ctToast('已恢复', 'ok'); loadCopyTasks(); })
    .catch(function(e) { _ctToast('恢复失败: ' + e.message, 'err'); });
}

function ctDeleteTask(id) {
  if (!confirm('确定删除该跟投任务？')) return;
  api('/api/copy-tasks/' + id, { method: 'DELETE' })
    .then(function() { _ctToast('已删除', 'ok'); loadCopyTasks(); })
    .catch(function(e) { _ctToast('删除失败: ' + e.message, 'err'); });
}

function ctEditTask(id) {
  var task = _ctTasks.find(function(t) { return t.id === id; });
  if (!task) { _ctToast('找不到任务', 'err'); return; }

  var old = document.getElementById('ct-edit-modal');
  if (old) old.remove();

  var cats = [];
  try { cats = JSON.parse(task.categories || '[]'); } catch(e) {}

  var modal = document.createElement('div');
  modal.id = 'ct-edit-modal';
  modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm';
  modal.innerHTML = [
    '<div class="bg-gray-900 border border-blue-600 rounded-xl p-6 w-96 shadow-2xl">',
      '<h3 class="text-base font-bold text-blue-300 mb-4">✏️ 编辑跟投任务</h3>',
      '<p class="text-xs text-gray-400 font-mono mb-4">' + task.wallet_address + '</p>',
      '<div class="space-y-3">',
        '<div>',
          '<label for="ct-edit-base" class="block text-xs text-gray-400 mb-1">投注金额 base_usdc (USDC)</label>',
          '<input id="ct-edit-base" type="number" min="1" max="100" step="0.5" value="' + (task.base_usdc || 10) + '" ',
          'class="w-full px-3 py-2 text-sm rounded border border-gray-600 bg-gray-800 text-white" />',
        '</div>',
        '<div>',
          '<label for="ct-edit-cats" class="block text-xs text-gray-400 mb-1">跟投赛道（逗号分隔，如: weather,sports）</label>',
          '<input id="ct-edit-cats" type="text" value="' + cats.join(', ') + '" ',
          'class="w-full px-3 py-2 text-sm rounded border border-gray-600 bg-gray-800 text-white" />',
        '</div>',
        '<div>',
          '<label for="ct-edit-score" class="block text-xs text-gray-400 mb-1">最低评分门槛 score_threshold</label>',
          '<input id="ct-edit-score" type="number" min="0" max="100" step="1" value="' + (task.score_threshold || 75) + '" ',
          'class="w-full px-3 py-2 text-sm rounded border border-gray-600 bg-gray-800 text-white" />',
        '</div>',
        '<div class="flex items-center gap-2">',
          '<input id="ct-edit-niche" type="checkbox" ' + (task.niche_only ? 'checked' : '') + ' class="w-4 h-4 text-blue-500" />',
          '<label for="ct-edit-niche" class="text-xs text-gray-300">仅限指定赛道 (niche_only)</label>',
        '</div>',
      '</div>',
      '<div class="flex gap-2 mt-5 justify-end">',
        '<button onclick="document.getElementById(\'ct-edit-modal\').remove()" ',
        'class="px-4 py-2 rounded text-sm bg-gray-700 hover:bg-gray-600 text-white">取消</button>',
        '<button onclick="ctEditTaskSave(' + id + ')" ',
        'class="px-4 py-2 rounded text-sm bg-blue-600 hover:bg-blue-500 text-white font-semibold">保存</button>',
      '</div>',
    '</div>'
  ].join('');

  document.body.appendChild(modal);
  modal.addEventListener('click', function(e) {
    if (e.target === modal) modal.remove();
  });
}

function ctEditTaskSave(id) {
  var baseVal = parseFloat(document.getElementById('ct-edit-base').value);
  var catsRaw = document.getElementById('ct-edit-cats').value;
  var scoreVal = parseFloat(document.getElementById('ct-edit-score').value);
  var nicheVal = document.getElementById('ct-edit-niche').checked;

  if (isNaN(baseVal) || baseVal < 1) { _ctToast('投注金额需≥1', 'err'); return; }
  if (isNaN(scoreVal)) { _ctToast('评分门槛无效', 'err'); return; }

  var catsList = catsRaw.split(',').map(function(s) { return s.trim().toLowerCase(); }).filter(Boolean);

  var payload = {
    base_usdc: baseVal,
    categories: catsList,
    score_threshold: scoreVal,
    niche_only: nicheVal
  };

  api('/api/copy-tasks/' + id, { method: 'PATCH', body: JSON.stringify(payload) })
    .then(function() {
      _ctToast('任务已更新 ✓', 'ok');
      var modal = document.getElementById('ct-edit-modal');
      if (modal) modal.remove();
      loadCopyTasks();
    })
    .catch(function(e) { _ctToast('保存失败: ' + e.message, 'err'); });
}

function _ctVal(id) {
  var el = document.getElementById(id);
  return el ? el.value : '';
}

function _ctChecked(id) {
  var el = document.getElementById(id);
  return el ? el.checked : false;
}

function _ctFmt(val, decimals) {
  var n = parseFloat(val);
  if (isNaN(n)) return '—';
  return n.toFixed(decimals === undefined ? 2 : decimals);
}

function _ctSetLoading(tbodyId, loading) {
  var tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  if (loading) {
    tbody.innerHTML = '<tr><td colspan="9" class="text-center text-gray-400 py-6">⏳ 查询中…</td></tr>';
  }
}

function _ctShowError(containerId, msg) {
  var el = document.getElementById(containerId);
  if (!el) return;
  var errDiv = el.querySelector('.ct-error-msg');
  if (!errDiv) {
    errDiv = document.createElement('div');
    errDiv.className = 'ct-error-msg text-red-400 text-sm px-4 py-2';
    el.prepend(errDiv);
  }
  errDiv.textContent = '⚠️ ' + msg;
}

function _ctToast(msg, type) {
  if (typeof showToast === 'function') {
    showToast(msg, type === 'err' ? 'error' : type === 'warn' ? 'warning' : 'success');
  } else {
    var el = document.getElementById('ct-toast');
    if (!el) { alert(msg); return; }
    el.textContent = msg;
    el.className = 'fixed bottom-6 left-1/2 -translate-x-1/2 px-4 py-2 rounded-lg text-white text-sm z-50 ' +
      (type === 'err' ? 'bg-red-700' : type === 'warn' ? 'bg-yellow-700' : 'bg-emerald-700');
    el.classList.remove('hidden');
    clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(function() { el.classList.add('hidden'); }, 3000);
  }
}

function _ctSortArray(arr, field, dir) {
  return arr.slice().sort(function(a, b) {
    var va = a[field];
    var vb = b[field];
    if (va == null) va = dir === 'asc' ? Infinity : -Infinity;
    if (vb == null) vb = dir === 'asc' ? Infinity : -Infinity;
    if (typeof va === 'string') va = va.toLowerCase();
    if (typeof vb === 'string') vb = vb.toLowerCase();
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
}
