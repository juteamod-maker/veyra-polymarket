
var _qs = new URLSearchParams(window.location.search);
var KEY = '';
var BASE = window.location.origin;
var REFRESH_INTERVAL = 30000;
var STANDALONE_PAGE = !!window.STANDALONE_PAGE;
var STRATEGY_EMBED = !!window.STRATEGY_EMBED;

var totpSession = null;

var STANDALONE_ROUTES = {
  overview: '/Overview',
  bets: '/Betslist',
  betslist: '/Betslist',
  copytrade: '/Copytrader',
  copytraders: '/Copytrader',
  analytics: '/Analysis',
  analysis: '/Analysis',
  ordermonitor: '/Ordermonitor',
  users: '/Users',
  config: '/Strategy',
  strategy: '/Strategy',
  polymarket: '/Polymarket',
  'system-settings': '/System',
  system: '/System',
  security: '/Security',
  tracking: '/Ordermonitor',
  kanban: '/WeatherMap'
};

function _appendKey(url) {
  if (!KEY) return url;
  var sep = url.indexOf('?') >= 0 ? '&' : '?';
  return url + sep + 'key=' + encodeURIComponent(KEY);
}

function __trackName(cat, fb) {
  var key = 'track.name.' + (cat || 'default');
  if (window.veyraI18n && typeof window.veyraI18n.t === 'function') {
    try { return window.veyraI18n.t(key, fb); } catch (e) { return fb; }
  }
  return fb;
}
var TRACK_ICONS = new Proxy({}, {
  get: function (target, prop) {
    var fb = {
      weather: 'Weather', sports: 'Sports', crypto: 'Crypto', geopolitics: 'Geopolitics',
      finance: 'Finance', tech: 'Tech', technology: 'Tech', economy: 'Economy',
      earnings: 'Earnings', elections: 'Elections', esports: 'eSports', mentions: 'Mentions',
      politics: 'Politics', world: 'World', 'pop-culture': 'Pop Culture', pop_culture: 'Pop Culture',
      entertainment: 'Entertainment', science: 'Science', default: 'Other'
    }[prop] || 'Other';
    return __trackName(prop, fb);
  }
});
var TRACK_COLORS = {
  weather: '#06b6d4',
  sports: '#eab308',
  crypto: '#f97316',
  geopolitics: '#8b5cf6',
  finance: '#10b981',
  tech: '#3b82f6',
  economy: '#f59e0b',
  earnings: '#ef4444',
  elections: '#0ea5e9',
  esports: '#6366f1',
  mentions: '#64748b',
  politics: '#14b8a6',
  world: '#475569',
  'pop-culture': '#ec4899',
  pop_culture: '#ec4899',
  default: '#64748b'
};

function switchTab(tabName) {
  if (STANDALONE_PAGE) {
    var target = STANDALONE_ROUTES[tabName];
    if (target) {
      window.location.href = _appendKey(target);
    }
    return;
  }
  if (tabName === 'bets') {
    localStorage.setItem('polymarket_active_tab', 'bets');
    window.location.href = '/Betslist';
    return;
  }
  var prevTab = localStorage.getItem('polymarket_active_tab') || 'overview';
  if (prevTab === 'bets' && tabName !== 'bets' && typeof BetsModule !== 'undefined') {
    BetsModule.onTabDeactivate();
  }
  document.querySelectorAll('.tab-content').forEach(function(el) { el.classList.remove('active'); });
  document.querySelectorAll('.nav-tab').forEach(function(el) { el.classList.remove('active'); });
  var tabEl = document.getElementById('tab-' + tabName);
  var navEl = document.querySelector('.nav-tab[data-tab="' + tabName + '"]');
  if (tabEl) tabEl.classList.add('active');
  if (navEl) navEl.classList.add('active');
  localStorage.setItem('polymarket_active_tab', tabName);
  if (typeof _loadTabData === 'function') {
    _cachedFetchStatus(false);
    _cachedFetchRiskStatus(false);
    _loadTabData(tabName, false);
  } else if (typeof refresh === 'function') {
    refresh();
  }
}

(function() {
  if (STANDALONE_PAGE || STRATEGY_EMBED) {
    return;
  }
  var params = new URLSearchParams(window.location.search);
  var tabParam = params.get('tab');
  if (tabParam && document.getElementById('tab-' + tabParam)) {
    switchTab(tabParam);
  } else {
    var saved = localStorage.getItem('polymarket_active_tab');
    if (saved && saved !== 'kanban' && document.getElementById('tab-' + saved)) {
      switchTab(saved);
    } else {
      window.location.href = '/WeatherMap';
      return;
    }
  }
})();

function openMapPage() {
  window.location.href = '/WeatherMap';
}

var totpPending = false;

function api(path, opts) {
  opts = opts || {};
  var sep = path.includes('?') ? '&' : '?';
  var headers = Object.assign({}, opts.headers || {});
  if (totpSession && !headers['X-TOTP-Session'] && !headers['x-totp-session']) {
    headers['X-TOTP-Session'] = totpSession;
  }
  if (opts.body && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }
  var fetchOpts = { headers: headers };
  if (opts.method) fetchOpts.method = opts.method;
  if (opts.body) fetchOpts.body = opts.body;
  if (opts.signal) fetchOpts.signal = opts.signal;
  return fetch(BASE + path + sep + 'key=' + KEY, fetchOpts).then(function(r) {
    if (r.status === 403) {
      return r.json().then(function(d) {
        if (d.totp_required) { location.reload(); }
        throw new Error('TOTP required');
      }).catch(function(e) {
        if (e.message === 'TOTP required') throw e;
        throw new Error('403');
      });
    }
    if (!r.ok) {
      return r.json().catch(function() { throw new Error(r.status); });
    }
    return r.json();
  });
}

function checkTotp() {
  totpPending = false;
  refresh();
}

function pnlClass(v) { return v >= 0 ? 'pnl-pos' : 'pnl-neg'; }
function pnlStr(v) { return (v >= 0 ? '+' : '') + v.toFixed(2); }
function pctStr(v) { return (v * 100).toFixed(1) + '%'; }
function truncQ(q, n) { n = n || 50; return q && q.length > n ? q.slice(0, n) + '…' : (q || '—'); }

function fmtHeld(min) {
  if (min < 60) return min + 'm';
  var h = Math.floor(min / 60), m = min % 60;
  if (h >= 24) { var d = Math.floor(h / 24); return d + 'd' + (h % 24 > 0 ? (h % 24) + 'h' : ''); }
  return h + 'h' + (m > 0 ? m + 'm' : '');
}

function fmtExpiry(hrs) {
  if (hrs === null || hrs === undefined) return '<span style="color:#64748b;font-size:0.75rem">待获取</span>';
  if (hrs < 0) return '<span class="text-yellow">等待结算</span>';
  if (hrs < 2) return '<span class="text-yellow font-bold">' + Math.round(hrs*60) + 'm</span>';
  return hrs < 12 ? '<span class="text-cyan">' + hrs.toFixed(1) + 'h</span>' : hrs.toFixed(1) + 'h';
}

function fmtTime(ts) {
  if (!ts) return '—';
  var d = new Date(ts * 1000);
  return d.toISOString().slice(5, 16).replace('T', ' ');
}

function fmtTimeRelative(ts) {
  if (!ts) return '—';
  var now = Date.now() / 1000;
  var diff = now - ts;
  if (diff < 0) diff = 0;
  if (diff < 60) return Math.floor(diff) + '秒前';
  if (diff < 3600) return Math.floor(diff / 60) + '分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + '小时前';
  if (diff < 604800) return Math.floor(diff / 86400) + '天前';
  var d = new Date(ts * 1000);
  return d.toISOString().slice(5, 10);
}

function fmtTimeWithRelative(ts) {
  if (!ts) return '—';
  var abs = fmtTime(ts);
  var rel = fmtTimeRelative(ts);
  return '<span title="' + abs + ' UTC">' + rel + '</span>';
}

function fmtTimeFull(ts) {
  if (!ts) return '—';
  var d = new Date(ts * 1000);
  return d.toISOString().slice(0, 19).replace('T', ' ') + ' UTC';
}

function fmtDir(b) {
  var out = (b.outcome || '').toUpperCase();
  if (out === 'YES') return '<span class="badge badge-green">YES</span>';
  if (b.category === 'sports' && out === 'NO') {
    var m = (b.question || '').match(/^(.+?)\s+vs\.?\s+(.+?)$/i);
    if (m) return '<span class="badge badge-red">NO</span> <span class="text-xs text-gray-400">' + m[2].trim().substring(0, 15) + ' 赢</span>';
  }
  return '<span class="badge badge-red">NO</span>';
}

function fmtMarket(b) {
  var q = b.question || '';
  var mSlug = b.market_slug || '';
  var eSlug = b.event_slug || '';
  var short = truncQ(q, 50);
  var safeQ = q.replace(/"/g, '&quot;').replace(/</g, '&lt;');
  function validSlug(s) { return s && /^[a-z0-9\-]+$/i.test(s); }
  var url = '';
  if (validSlug(eSlug) && validSlug(mSlug) && eSlug !== mSlug) {
    url = 'https://polymarket.com/event/' + encodeURIComponent(eSlug) + '/' + encodeURIComponent(mSlug);
  } else if (validSlug(eSlug)) {
    url = 'https://polymarket.com/event/' + encodeURIComponent(eSlug);
  } else if (validSlug(mSlug)) {
    url = 'https://polymarket.com/event/' + encodeURIComponent(mSlug);
  }
  if (url) {
    return '<a href="' + url + '" target="_blank" rel="noopener" class="market-link" title="' + safeQ + '">' + short + '<span class="link-icon">[Link]</span></a>';
  }
  return '<span title="' + safeQ + '">' + short + '</span>';
}

function fmtBetType(bt) {
  if (bt === 'scale_in') return '<span class="badge badge-scale-in">加仓</span>';
  if (bt === 'sold') return '<span class="badge badge-sold">卖出</span>';
  return '<span class="badge badge-entry">入场</span>';
}

function fmtStatus(b) {
  if (!b.resolved_outcome) {
    return '<span class="status-badge status-open">持仓中</span>';
  }
  var et = b.exit_type || 'resolution';
  if (et === 'manual_exit' || et === 'manual_sell') {
    var cls = b.pnl >= 0 ? 'status-win' : 'status-loss';
    return '<span class="status-badge ' + cls + '">手动卖出</span>';
  }
  if (et === 'profit_take' || et === 'near_ceiling' || et === 'trailing_stop' ||
      et === 'quick_exit' || et === 'weather_quick_exit' || et === 'hold_to_settle') {
    return '<span class="status-badge status-sold">已卖出</span>';
  }
  if (et === 'stop_loss' || et === 'floor_stop' || et === 'standard_stop' ||
      et === 'reversal_stop' || et === 'expiry_stop' || et === 'early_stop') {
    return '<span class="status-badge status-loss">止损</span>';
  }
  if (b.is_win) {
    return '<span class="status-badge status-win">盈利</span>';
  }
  return '<span class="status-badge status-loss">亏损</span>';
}

function fmtOutcomeBadge(b) {
  var out = (b.outcome || 'YES').toUpperCase();
  var price = b.market_price || 0;
  var size = b.size_usdc || 0;
  var shares = b.matched_shares > 0 ? Math.round(b.matched_shares * 10) / 10
    : (price > 0 ? Math.round(size / price * 10) / 10 : 0);
  var isYes = (out === 'YES');
  var color = isYes ? '#22c55e' : '#ef4444';
  var bg = isYes ? 'rgba(34,197,94,0.12)' : 'rgba(239,68,68,0.12)';
  var label = out;
  if (out !== 'YES' && out !== 'NO') {
    label = b.outcome || out;
    color = '#06b6d4';
    bg = 'rgba(6,182,212,0.12)';
  }
  var isChainSyncZero = (b.dimension_tags && b.dimension_tags.indexOf('chain_sync') >= 0)
    && price === 0;
  if (isChainSyncZero) {
    return '<span style="display:inline-flex;align-items:center;gap:4px;padding:1px 8px;' +
      'border-radius:999px;font-size:0.7rem;font-weight:600;color:#f59e0b;background:rgba(245,158,11,0.12)">' +
      label + ' ⏳同步中</span>';
  }
  return '<span style="display:inline-flex;align-items:center;gap:4px;padding:1px 8px;' +
    'border-radius:999px;font-size:0.7rem;font-weight:600;color:' + color + ';background:' + bg + '">' +
    label + ' ' + fmtPriceCents(price) + ' × ' + shares + '份</span>';
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
