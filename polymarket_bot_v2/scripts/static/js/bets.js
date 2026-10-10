
var BetsModule = (function () {
  'use strict';

  function _getCookie(name) {
    var match = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : '';
  }

  function _getPrivyAuthHeaders() {
    var headers = {};
    var accessToken = '';
    var idToken = '';

    try { accessToken = String(localStorage.getItem('privy:pat') || '').trim(); } catch (_) { }
    try { if (!accessToken) accessToken = String(localStorage.getItem('privy:token') || '').trim(); } catch (_) { }
    try { idToken = String(localStorage.getItem('privy:id_token') || '').trim(); } catch (_) { }
    try { if (!idToken) idToken = String(localStorage.getItem('privy:id-token') || '').trim(); } catch (_) { }

    if (!accessToken) accessToken = _getCookie('privy-token');
    if (!accessToken) accessToken = _getCookie('privy_access_token');
    if (!idToken) idToken = _getCookie('privy-id-token');
    if (!idToken) idToken = _getCookie('privy_id_token');

    if (accessToken) headers.Authorization = 'Bearer ' + accessToken;
    if (idToken) headers['privy-id-token'] = idToken;
    return headers;
  }

  function _packSafeSignature(rawSig) {
    var sig = String(rawSig || '').toLowerCase();
    if (sig.indexOf('0x') === 0) sig = sig.slice(2);
    if (sig.length !== 130) {
      throw new Error('Invalid signature length: ' + sig.length);
    }
    var r = sig.slice(0, 64);
    var s = sig.slice(64, 128);
    var v = parseInt(sig.slice(128, 130), 16);
    if (v === 0 || v === 1) v += 31;
    else if (v === 27 || v === 28) v += 4;
    else throw new Error('Invalid signature v: ' + v);
    var vHex = v.toString(16);
    if (vHex.length === 1) vHex = '0' + vHex;
    return '0x' + r + s + vHex;
  }

  function _signSafeStructHash(structHash, signingAddress) {
    var provider = window.ethereum || window.privyEthereumProvider || null;
    if (!provider || typeof provider.request !== 'function') {
      return Promise.reject(new Error('No browser wallet provider available. Please connect MetaMask or another EVM wallet.'));
    }
    var addrLc = String(signingAddress || '').toLowerCase();
    return provider.request({ method: 'eth_requestAccounts' }).then(function (accts) {
      var acct = (accts && accts[0]) ? String(accts[0]).toLowerCase() : '';
      if (acct && addrLc && acct !== addrLc) {
        return Promise.reject(new Error('Connected wallet (' + acct + ') does not match required signer (' + addrLc + '). Please switch your wallet account.'));
      }
      var hex = String(structHash || '');
      if (hex.indexOf('0x') === 0) hex = hex.slice(2);
      if (hex.length !== 64) {
        return Promise.reject(new Error('Server struct_hash must be 32 bytes (64 hex chars), got ' + hex.length));
      }
      var rawHex = '0x' + hex;
      return provider.request({
        method: 'personal_sign',
        params: [rawHex, signingAddress],
      }).then(_packSafeSignature);
    });
  }

  var _state = {
    bets: [],
    page: 1,
    totalPages: 1,
    total: 0,
    sortCol: 'ts_entered',
    sortDir: 'desc',
    isLoading: false,
    lastSignature: '',
    livePriceTimer: null,
    livePriceActive: false,
    abortCtrl: null,
    debounceTimer: null,
    manualUserMode: false,
  };

  var _filterCache = {};
  var _CACHE_TTL = 120000;
  var _CACHE_MAX = 30;

  function _buildCacheKey() {
    return [
      _val('bets-filter-cat'),
      _val('bets-filter-status'),
      _val('bets-filter-perpage') || '25',
      _val('bets-filter-dim'),
      _val('bets-filter-from'),
      _val('bets-filter-to'),
      _val('bets-filter-search'),
      _val('bets-filter-tag')
    ].join('|');
  }

  function _getCached(key, page) {
    var c = _filterCache[key + '@' + page];
    if (c && (Date.now() - c.ts) < _CACHE_TTL) return c;
    return null;
  }

  function _setCache(key, page, data) {
    var keys = Object.keys(_filterCache);
    if (keys.length >= _CACHE_MAX) {
      var oldest = keys[0], oldestTs = Infinity;
      keys.forEach(function (k) {
        if (_filterCache[k].ts < oldestTs) { oldest = k; oldestTs = _filterCache[k].ts; }
      });
      delete _filterCache[oldest];
    }
    _filterCache[key + '@' + page] = {
      bets: data.bets,
      total: data.total,
      totalPages: data.totalPages,
      page: data.page,
      ts: Date.now()
    };
  }

  function _renderTagBadges(tags) {
    if (!tags || !tags.length) return '';
    return tags.map(function (t) {
      return '<span class="bc-badge" style="background:#334155;color:#94a3b8;font-size:10px">🏷️' + escHtml(t) + '</span>';
    }).join('');
  }

  function _getCategoryIcon(cat) {
    var icons = {
      weather: '🌤️', sports: '🏀', crypto: '₿',
      geopolitics: '🌍', finance: '💰', tech: '🔬',
      economy: '📊', earnings: '📈', elections: '🗳️',
      esports: '🎮', mentions: '🏷️', politics: '🏛️',
      world: '🌐', 'pop-culture': '🎭', pop_culture: '🎭'
    };
    return icons[cat] || '📦';
  }

  function _getCategoryColor(cat) {
    var colors = {
      weather: '#06b6d4', sports: '#eab308', crypto: '#f97316',
      geopolitics: '#8b5cf6', finance: '#10b981', tech: '#3b82f6',
      economy: '#f59e0b', earnings: '#ef4444', elections: '#0ea5e9',
      esports: '#6366f1', mentions: '#64748b', politics: '#14b8a6',
      world: '#475569', 'pop-culture': '#ec4899', pop_culture: '#ec4899'
    };
    return colors[cat] || '#64748b';
  }

  function _getStrategyLabel(bucket, tag) {
    var labels = {
      'weather_double_no_right': 'NO-右翼',
      'weather_double_no_left': 'NO-左翼',
      'weather_double_no_same': 'NO-峰值',
      'weather_right_tail_right': 'NO-右尾做空',
      'weather_right_tail_left': 'NO-右尾左翼',
      'weather_right_tail_same': 'NO-右尾峰值',
      'weather_yes_ambush': 'YES埋伏',
      'weather': '天气',
      'pure_coverage': '纯覆盖',
      'extreme_lock': 'EHCL锁定',
      'city_no': 'CityNO',
    };
    var key = tag || bucket || '';
    return labels[key] || (key ? key.replace(/_/g, ' ') : '');
  }

  function _buildPmUrl(b) {
    var mSlug = b.market_slug || '';
    var eSlug = b.event_slug || '';
    function valid(s) { return s && /^[a-z0-9\-]+$/i.test(s); }
    if (valid(eSlug)) return '/event/' + encodeURIComponent(eSlug);
    if (valid(mSlug) && valid(eSlug) && eSlug !== mSlug)
      return 'https://polymarket.com/event/' + encodeURIComponent(eSlug) + '/' + encodeURIComponent(mSlug);
    if (valid(mSlug))
      return 'https://polymarket.com/event/' + encodeURIComponent(mSlug);
    return '';
  }

  function _buildPmExternalUrl(b) {
    var mSlug = b.market_slug || '';
    var eSlug = b.event_slug || '';
    function valid(s) { return s && /^[a-z0-9\-]+$/i.test(s); }
    if (valid(mSlug) && valid(eSlug) && eSlug !== mSlug)
      return 'https://polymarket.com/event/' + encodeURIComponent(eSlug) + '/' + encodeURIComponent(mSlug);
    if (valid(mSlug))
      return 'https://polymarket.com/event/' + encodeURIComponent(mSlug);
    if (valid(eSlug))
      return 'https://polymarket.com/event/' + encodeURIComponent(eSlug);
    return '';
  }

  function _fmtCents(v) {
    if (v == null || isNaN(v)) return '—';
    return (v * 100).toFixed(1) + '¢';
  }

  function _fmtUsd(v) {
    if (v == null || isNaN(v)) return '$0.00';
    return '$' + Math.abs(v).toFixed(2);
  }

  function _fmtPnl(v) {
    if (v == null || isNaN(v)) return '—';
    return (v >= 0 ? '+' : '-') + '$' + Math.abs(v).toFixed(2);
  }

  function _fmtPct(v) {
    if (v == null || isNaN(v)) return '';
    return (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%';
  }

  function _timeAgo(ts) {
    if (!ts) return '—';
    var diff = Date.now() / 1000 - ts;
    if (diff < 0) diff = 0;
    if (diff < 60) return Math.floor(diff) + '秒前';
    if (diff < 3600) return Math.floor(diff / 60) + '分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + '小时前';
    if (diff < 604800) return Math.floor(diff / 86400) + '天前';
    return new Date(ts * 1000).toISOString().slice(5, 10);
  }

  function _timeFull(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
  }

  function _renderCards(bets) {
    var container = document.getElementById('bets-card-list');
    if (!container) return;

    if (_state.isLoading && !bets.length) {
      container.innerHTML = _renderLoadingSkeleton();
      return;
    }

    if (!bets.length) {
      container.innerHTML = _renderEmptyState();
      return;
    }

    var sorted = bets.slice().sort(function (a, b) {
      var va = _getSortVal(a, _state.sortCol);
      var vb = _getSortVal(b, _state.sortCol);
      if (typeof va === 'string') { va = va.toLowerCase(); vb = (vb || '').toLowerCase(); }
      if (va < vb) return _state.sortDir === 'asc' ? -1 : 1;
      if (va > vb) return _state.sortDir === 'asc' ? 1 : -1;
      return 0;
    });

    var sig = _computeSig(sorted);
    if (sig === _state.lastSignature) return;
    _state.lastSignature = sig;

    container.innerHTML = _renderSortHeader() +
      sorted.map(function (b) { return _renderSingleCard(b); }).join('');
  }

  function _renderSortHeader() {
    var __t = (window.veyraI18n && typeof window.veyraI18n.t === 'function') ? window.veyraI18n.t.bind(window.veyraI18n) : function (k, fb) { return fb; };
    var cols = [
      { k: 'ts_entered', label: __t('trades.sort.time', 'Time') },
      { k: 'pnl', label: __t('trades.sort.pnl', 'P&L') },
      { k: 'size_usdc', label: __t('trades.sort.size', 'Size') },
      { k: 'current_price', label: __t('trades.sort.price', 'Price') },
      { k: 'category', label: __t('trades.sort.category', 'Category') }
    ];
    var html = '<div class="bets-sort-header">';
    html += '<span class="bets-sort-label">' + __t('trades.sort.label', 'Sort') + '</span>';
    cols.forEach(function (c) {
      var active = _state.sortCol === c.k;
      var ico = active ? (_state.sortDir === 'asc' ? '▲' : '▼') : '⇅';
      html += '<button type="button" class="bets-sort-btn' + (active ? ' active' : '') +
        '" data-sort="' + c.k + '" onclick="BetsModule.setSort(\'' + c.k + '\')">' +
        c.label + ' <span class="bets-sort-icon">' + ico + '</span></button>';
    });
    html += '</div>';
    return html;
  }

  function _computeSig(bets) {
    return bets.map(function (b) {
      return b.bet_id + '|' + (b.current_price || 0) + '|' + (b.unrealized_pnl || 0) + '|' + (b.pnl || 0) + '|' + (b.resolved_outcome || '');
    }).join(',');
  }

  function _getSortVal(row, col) {
    if (col === 'pnl') {
      if (row.pnl != null) return row.pnl;
      if (row.unrealized_pnl != null) return row.unrealized_pnl;
      return 0;
    }
    return row[col] || 0;
  }

  function _renderSingleCard(b) {
    var cat = b.category || '';
    var icon = _getCategoryIcon(cat);
    var outcome = (b.outcome || 'YES').toUpperCase();
    var isYes = outcome === 'YES';
    var isOpen = !b.resolved_outcome;

    var dirBadge = isYes
      ? '<span class="bc-badge bc-badge-yes">YES</span>'
      : '<span class="bc-badge bc-badge-no">NO</span>';

    var statusHtml = '';
    if (isOpen) {
      if (_state.manualUserMode && b.can_redeem) {
        statusHtml = '<span class="bc-badge bc-badge-win">🏆 可兑换</span>';
      } else {
        statusHtml = '<span class="bc-badge bc-badge-open"><span class="bc-pulse"></span>持仓中</span>';
      }
    } else {
      var et = b.exit_type || 'resolution';
      if (et === 'profit_take' || et === 'near_ceiling' || et === 'trailing_stop' ||
        et === 'quick_exit' || et === 'weather_quick_exit' || et === 'hold_to_settle') {
        statusHtml = '<span class="bc-badge bc-badge-sold">📤 已卖出</span>';
      } else if (et === 'stop_loss' || et === 'floor_stop' || et === 'standard_stop' ||
        et === 'reversal_stop' || et === 'expiry_stop' || et === 'early_stop') {
        statusHtml = '<span class="bc-badge bc-badge-loss">🛑 止损</span>';
      } else if (et === 'manual_exit' || et === 'manual_sell') {
        statusHtml = '<span class="bc-badge ' + (b.pnl >= 0 ? 'bc-badge-win' : 'bc-badge-loss') + '">👋 手动</span>';
      } else if (b.is_win) {
        statusHtml = '<span class="bc-badge bc-badge-win">✅ 盈利</span>';
      } else {
        statusHtml = '<span class="bc-badge bc-badge-loss">❌ 亏损</span>';
      }
    }

    var timeRel = _timeAgo(b.ts_entered);
    var timeFull = _timeFull(b.ts_entered);
    var shortId = (b.bet_id || '').split('-')[0].slice(0, 8);

    var q = b.question || '—';
    var pmUrl = _buildPmUrl(b);
    var isInternal = pmUrl && pmUrl.indexOf('/event/') === 0;
    var pmExternalUrl = _buildPmExternalUrl(b);
    var qHtml = pmUrl
      ? '<a href="' + pmUrl + '"' + (isInternal ? '' : ' target="_blank" rel="noopener"') + ' class="bc-q-link" onclick="event.stopPropagation()">' + escHtml(q) + ' <span class="bc-link-arrow">' + (isInternal ? '→' : '↗') + '</span></a>'
      : '<span>' + escHtml(q) + '</span>';

    var entryPrice = b.market_price || 0;
    var sizeUsd = (b.size_usdc || 0).toFixed(2);
    var shares = b.matched_shares > 0 ? Math.round(b.matched_shares * 10) / 10
      : (entryPrice > 0 ? Math.round((b.size_usdc || 0) / entryPrice * 10) / 10 : 0);

    var cp = b.current_price;
    var cpHtml = '';
    if (isOpen && cp != null) {
      var diff = cp - entryPrice;
      var cpColor = diff > 0.001 ? '#34d399' : (diff < -0.001 ? '#fb7185' : '#94a3b8');
      cpHtml = '<span style="color:' + cpColor + '">' + _fmtCents(cp) + '</span>';
    } else if (!isOpen) {
      cpHtml = '<span style="color:#64748b">—</span>';
    } else {
      cpHtml = '<span style="color:#475569">···</span>';
    }

    var pnlVal, pnlPct;
    if (b.resolved_outcome) {
      pnlVal = b.pnl || 0;
      pnlPct = b.pnl_pct || null;
    } else if (b.unrealized_pnl != null) {
      pnlVal = b.unrealized_pnl;
      pnlPct = b.unrealized_pnl_pct;
    } else {
      pnlVal = null;
      pnlPct = null;
    }
    var pnlColor = pnlVal != null ? (pnlVal >= 0 ? '#34d399' : '#fb7185') : '#eab308';
    var pnlMainHtml = '';
    if (pnlVal != null) {
      pnlMainHtml = '<span style="color:' + pnlColor + '">' + _fmtPnl(pnlVal) + '</span>';
    } else {
      pnlMainHtml = '<span style="color:#eab308">⏳</span>';
    }

    var pnlSub = '';
    if (pnlPct != null) {
      pnlSub = ' <span class="bc-r-pnl-pct" style="color:' + pnlColor + '">' + _fmtPct(pnlPct) + '</span>';
    }

    var actionBtn = '';
    if (!_state.manualUserMode) {
      if (b.resolved_outcome && b.is_win) {
        actionBtn = '<button type="button" class="bc-action-btn bc-btn-redeem" ' +
          'onclick="BetsModule.redeemBet(this); event.stopPropagation(); return false;" ' +
          'data-bet-id="' + escHtml(b.bet_id) + '" data-is-win="1" title="赎回获胜份额，USDC 将划入钱包">' +
          '💰 赢钱</button>';
      }
    } else {
      if (b.can_redeem) {
        actionBtn = '<button type="button" class="bc-action-btn bc-btn-redeem" ' +
          'onclick="BetsModule.redeemBet(this); event.stopPropagation(); return false;" ' +
          'data-bet-id="' + escHtml(b.bet_id) + '" ' +
          'data-is-win="' + (b.is_win ? '1' : '0') + '" ' +
          'data-condition-id="' + escHtml(b.condition_id || '') + '" ' +
          'data-outcome="' + escHtml((b.outcome || 'YES').toUpperCase()) + '" ' +
          'data-market-id="' + escHtml(b.market_id || '') + '" ' +
          'data-size-usdc="' + escHtml(String(b.size_usdc == null ? '' : b.size_usdc)) + '" ' +
          'title="系统内一键兑换获胜份额">💰 兑换</button>';
      }
    }

    return '<div class="bc-item-shell">' +
      '<div class="bc-item" data-bet-id="' + escHtml(b.bet_id) + '">' +
      '<div class="bc-row">' +

      '<div class="bc-r-icon">' + icon + '</div>' +

      '<div class="bc-r-main">' +
      '<div class="bc-r-title">' + qHtml + '</div>' +
      '<div class="bc-r-meta">' + dirBadge + statusHtml +
      '<span class="bc-badge bc-badge-shares">$' + sizeUsd + ' · ' + shares + '份</span>' +
      '<span class="bc-badge bc-badge-time" title="' + timeFull + '">' + timeRel + '</span>' +
      (shortId ? '<span class="bc-badge bc-badge-id">' + shortId + '</span>' : '') +
      _renderTagBadges(b.tags) +
      '</div>' +
      '</div>' +

      '<div class="bc-r-data">' +
      '<div class="bc-r-col">' +
      '<div class="bc-r-entry"><s>' + _fmtCents(entryPrice) + '</s></div>' +
      '</div>' +
      '<div class="bc-r-col">' +
      '<div class="bc-r-live" id="cp-' + escHtml(b.bet_id) + '">' + cpHtml + '</div>' +
      '</div>' +
      '<div class="bc-r-col bc-r-pnl">' +
      '<div><span class="bc-r-pnl-val" id="pnl-' + escHtml(b.bet_id) + '">' + pnlMainHtml + pnlSub + '</span></div>' +
      '</div>' +
      '</div>' +

      '</div>' +
      '</div>' +
      (actionBtn ? '<div class="bc-side-action"><div class="bc-side-action-inner">' +
        actionBtn.replace('bc-action-btn', 'bc-action-btn bc-action-btn-lg') +
        '</div></div>' : '') +
      '</div>';
  }

  function _triggerConfetti() {
    var canvas = document.createElement('canvas');
    canvas.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:9999';
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    document.body.appendChild(canvas);
    var ctx = canvas.getContext('2d');

    var COLORS = ['#34d399', '#fbbf24', '#60a5fa', '#f472b6', '#a78bfa', '#fb923c'];
    var particles = [];
    for (var i = 0; i < 120; i++) {
      particles.push({
        x: Math.random() * canvas.width,
        y: -Math.random() * canvas.height * 0.5,
        vx: (Math.random() - 0.5) * 4,
        vy: 2 + Math.random() * 4,
        rot: Math.random() * Math.PI * 2,
        vrot: (Math.random() - 0.5) * 0.2,
        w: 8 + Math.random() * 8,
        h: 4 + Math.random() * 4,
        color: COLORS[Math.floor(Math.random() * COLORS.length)],
        alpha: 1,
      });
    }
    var start = Date.now();
    function _frame() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      var elapsed = Date.now() - start;
      var done = 0;
      particles.forEach(function (p) {
        p.x += p.vx;
        p.y += p.vy;
        p.rot += p.vrot;
        p.vy += 0.08;
        if (elapsed > 1500) p.alpha = Math.max(0, p.alpha - 0.02);
        if (p.alpha <= 0) { done++; return; }
        ctx.save();
        ctx.globalAlpha = p.alpha;
        ctx.translate(p.x, p.y);
        ctx.rotate(p.rot);
        ctx.fillStyle = p.color;
        ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
        ctx.restore();
      });
      if (done < particles.length) {
        requestAnimationFrame(_frame);
      } else {
        if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
      }
    }
    requestAnimationFrame(_frame);
  }

  function _redeemBet(targetOrBetId, isWin, extraPayload) {
    var btn = null;
    var betId = '';
    var winFlag = !!isWin;
    var payload = {};

    if (targetOrBetId && typeof targetOrBetId === 'object' && targetOrBetId.nodeType === 1) {
      btn = targetOrBetId;
      betId = String(btn.getAttribute('data-bet-id') || '').trim();
      winFlag = String(btn.getAttribute('data-is-win') || '0') === '1';
      payload = {
        bet_id: betId,
      };
      var cid = String(btn.getAttribute('data-condition-id') || '').trim();
      var outcome = String(btn.getAttribute('data-outcome') || '').trim();
      var marketId = String(btn.getAttribute('data-market-id') || '').trim();
      var sizeText = String(btn.getAttribute('data-size-usdc') || '').trim();
      var sizeNum = sizeText === '' ? null : Number(sizeText);
      if (cid) payload.condition_id = cid;
      if (outcome) payload.outcome = outcome;
      if (marketId) payload.market_id = marketId;
      if (sizeNum != null && !isNaN(sizeNum)) payload.size_usdc = sizeNum;
    } else {
      betId = String(targetOrBetId || '').trim();
      payload = { bet_id: betId };
      var btns = document.querySelectorAll('.bc-btn-redeem[data-bet-id]');
      for (var i = 0; i < btns.length; i++) {
        if (String(btns[i].getAttribute('data-bet-id') || '') === betId) {
          btn = btns[i];
          break;
        }
      }
    }

    if (!betId) {
      _notify('danger', 'Invalid bet_id');
      return;
    }

    var originalText = btn ? btn.textContent : '💰 兑换';
    if (btn) {
      btn.disabled = true;
      btn.textContent = '⏳ 处理中…';
    }

    var __t = (window.veyraI18n && typeof window.veyraI18n.t === 'function')
      ? window.veyraI18n.t.bind(window.veyraI18n)
      : function (k, fb) { return fb; };

    if (extraPayload && typeof extraPayload === 'object') {
      if (extraPayload.condition_id) payload.condition_id = extraPayload.condition_id;
      if (extraPayload.outcome) payload.outcome = extraPayload.outcome;
      if (extraPayload.market_id) payload.market_id = extraPayload.market_id;
      if (extraPayload.size_usdc != null) payload.size_usdc = extraPayload.size_usdc;
    }

    fetch('/api/pm/redeem', {
      method: 'POST',
      credentials: 'include',
      headers: Object.assign({ 'Content-Type': 'application/json' }, _getPrivyAuthHeaders()),
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (res.data && res.data.requires_client_sign) {
          var signingMethod = String(res.data.signing_method || '').toLowerCase();
          var skeleton = res.data.payload_skeleton || {};
          if (signingMethod === 'eip712_batch') {
            var typedData = res.data.typed_data;
            if (!typedData) {
              return Promise.reject(new Error('Server did not return typed_data for deposit_wallet client signing'));
            }
            if (typeof window.veyraPrivySignTypedData !== 'function') {
              return Promise.reject(new Error('Privy embedded wallet runtime not ready; please reload the page and retry.'));
            }
            return window.veyraPrivySignTypedData(typedData, {
              expectedSigner: String(res.data.signing_address || '').toLowerCase(),
              requireExact: true,
            }).then(function (sig) {
              var relayerTx = Object.assign({}, skeleton, { signature: sig });
              var resignedPayload = Object.assign({}, payload, { relayer_tx: relayerTx });
              return fetch('/api/pm/redeem', {
                method: 'POST',
                credentials: 'include',
                headers: Object.assign({ 'Content-Type': 'application/json' }, _getPrivyAuthHeaders()),
                body: JSON.stringify(resignedPayload),
              }).then(function (r2) { return r2.json().then(function (d2) { return { ok: r2.ok, data: d2 }; }); });
            });
          }
          var sh = res.data.struct_hash;
          if (!sh) {
            return Promise.reject(new Error('Server did not return struct_hash for client signing'));
          }
          return _signSafeStructHash(sh, res.data.signing_address)
            .then(function (packedSig) {
              var relayerTx = Object.assign({}, skeleton, { signature: packedSig });
              var resignedPayload = Object.assign({}, payload, { relayer_tx: relayerTx });
              return fetch('/api/pm/redeem', {
                method: 'POST',
                credentials: 'include',
                headers: Object.assign({ 'Content-Type': 'application/json' }, _getPrivyAuthHeaders()),
                body: JSON.stringify(resignedPayload),
              }).then(function (r2) { return r2.json().then(function (d2) { return { ok: r2.ok, data: d2 }; }); });
            });
        }
        return res;
      })
      .then(function (res) {
        if (!res.ok || !res.data.success) {
          var errCode = res.data && res.data.error ? String(res.data.error) : '';
          if (errCode === 'RELAYER_PAYLOAD_INVALID' && btn && !btn.dataset.vpRelayerRetried) {
            var rsn = String((res.data.detail && res.data.detail.relayer_reason) || '').toLowerCase();
            var rb = String((res.data.detail && res.data.detail.relayer_body) || '').toLowerCase();
            var nonceLike = (rsn + ' ' + rb).indexOf('nonce') >= 0
              || (rsn + ' ' + rb).indexOf('signature') >= 0;
            if (nonceLike) {
              btn.dataset.vpRelayerRetried = '1';
              setTimeout(function () {
                try { _redeemBet(btn, winFlag, extraPayload); }
                catch (e) { }
              }, 80);
              return;
            }
          }
          var msg = (res.data && (res.data.message || res.data.error)) || __t('redeem.error', '赎回失败，请稍后重试');
          _notify('danger', msg, (res.data && res.data.detail) ? res.data.detail : null);
          if (btn) { btn.disabled = false; btn.textContent = originalText; }
          return;
        }
        if (btn) {
          var shell = btn.closest('.bc-item-shell');
          if (shell && shell.parentNode) {
            shell.parentNode.removeChild(shell);
          } else {
            var actionsEl = btn.closest('.bc-r-actions');
            if (actionsEl) actionsEl.remove(); else btn.remove();
          }
        }
        if (winFlag) _triggerConfetti();
        var tx = res.data.tx_hash || '';
        var msg = __t('redeem.success', '赎回成功！') + (tx ? ('\n🔗 ' + tx.slice(0, 20) + '…') : '');
        _notify('success', msg);
        try {
          if (typeof BetsModule !== 'undefined' && BetsModule.refresh) {
            setTimeout(function () { try { BetsModule.refresh(); } catch (e) { } }, 1500);
          } else if (typeof _fetchManualUserBets === 'function') {
            setTimeout(function () { try { _fetchManualUserBets(true); } catch (e) { } }, 1500);
          }
        } catch (e) { }
      })
      .catch(function (e) {
        if (btn) { btn.disabled = false; btn.textContent = originalText; }
        _notify('danger', __t('redeem.networkError', '网络错误，请重试'));
      });
  }

  function _notify(type, msg, detail) {
    if (window.vp) {
      if (type === 'success' && typeof window.vp.success === 'function') {
        window.vp.success({
          title: 'Redeem',
          body: String(msg || ''),
        });
        return;
      }
      if (type === 'danger' && typeof window.vp.error === 'function') {
        var detailText = '';
        if (typeof detail === 'string') {
          detailText = detail;
        } else if (detail && typeof detail === 'object') {
          if (detail.shortfall_pol != null) {
            detailText = 'Need additional POL for gas: ' + detail.shortfall_pol;
            if (detail.sender) detailText += '\nTop up address: ' + detail.sender;
          } else {
            try { detailText = JSON.stringify(detail); } catch (e) { detailText = ''; }
          }
        }
        window.vp.error({
          status: 500,
          error: String(msg || ''),
          detail: detailText,
        }, {
          title: 'Redeem',
        });
        return;
      }
    }
    console.error('[redeem] modal unavailable:', String(msg || ''));
  }

  function _renderLoadingSkeleton() {
    var skeletons = '';
    for (var i = 0; i < 8; i++) {
      skeletons += '<div class="bc-row bc-skeleton" style="padding:10px 12px;gap:10px">' +
        '<div class="bc-skel-block" style="width:34px;height:34px;border-radius:8px;flex-shrink:0"></div>' +
        '<div style="flex:1;display:flex;flex-direction:column;gap:4px">' +
        '<div class="bc-skel-block" style="width:70%;height:12px"></div>' +
        '<div style="display:flex;gap:4px">' +
        '<div class="bc-skel-block" style="width:30px;height:14px;border-radius:7px"></div>' +
        '<div class="bc-skel-block" style="width:44px;height:14px;border-radius:7px"></div>' +
        '<div class="bc-skel-block" style="width:36px;height:14px;border-radius:7px"></div>' +
        '</div>' +
        '</div>' +
        '<div style="display:flex;gap:12px">' +
        '<div class="bc-skel-block" style="width:40px;height:20px"></div>' +
        '<div class="bc-skel-block" style="width:44px;height:20px"></div>' +
        '<div class="bc-skel-block" style="width:54px;height:20px"></div>' +
        '</div>' +
        '</div>';
    }
    return skeletons;
  }

  function _renderEmptyState() {
    return '<div class="bc-empty">' +
      '<div class="bc-empty-icon">📊</div>' +
      '<div class="bc-empty-title">暂无匹配记录</div>' +
      '<div class="bc-empty-desc">调整筛选条件或等待系统发现新的投注机会</div>' +
      '</div>';
  }

  function _startLivePrices() {
    if (_state.livePriceActive) return;
    _state.livePriceActive = true;
    _pollLivePrices();
    _state.livePriceTimer = setInterval(_pollLivePrices, 3000);
  }

  function _stopLivePrices() {
    _state.livePriceActive = false;
    if (_state.livePriceTimer) {
      clearInterval(_state.livePriceTimer);
      _state.livePriceTimer = null;
    }
  }

  function _pollLivePrices() {
    var activeTab = localStorage.getItem('polymarket_active_tab') || 'overview';
    var isBetsListPage = (window.location.pathname || '').toLowerCase() === '/betslist';
    if (activeTab !== 'bets' && !isBetsListPage) {
      _stopLivePrices();
      return;
    }

    api('/api/bets/live-prices').then(function (d) {
      var prices = d.prices || {};
      var hasUpdate = false;

      for (var betId in prices) {
        var p = prices[betId];
        if (p.cp == null) continue;

        for (var i = 0; i < _state.bets.length; i++) {
          if (_state.bets[i].bet_id === betId) {
            var old = _state.bets[i];
            if (old.current_price !== p.cp || old.unrealized_pnl !== p.upnl) {
              old.current_price = p.cp;
              old.unrealized_pnl = p.upnl;
              old.unrealized_pnl_pct = p.upnl_pct;
              old.market_value = p.mv;
              hasUpdate = true;

              _updateCardPrice(betId, old);
            }
            break;
          }
        }
      }
    }).catch(function () {
    });
  }

  function _updateCardPrice(betId, b) {
    var cpEl = document.getElementById('cp-' + betId);
    var pnlEl = document.getElementById('pnl-' + betId);

    if (cpEl && b.current_price != null) {
      var entry = b.market_price || 0;
      var diff = b.current_price - entry;
      var color = diff > 0.001 ? '#34d399' : (diff < -0.001 ? '#fb7185' : '#94a3b8');
      cpEl.innerHTML = '<span style="color:' + color + '">' + _fmtCents(b.current_price) + '</span>';
      cpEl.classList.add('bc-price-flash');
      setTimeout(function () { cpEl.classList.remove('bc-price-flash'); }, 500);
    }

    if (pnlEl && b.unrealized_pnl != null) {
      var pnlColor = b.unrealized_pnl >= 0 ? '#34d399' : '#fb7185';
      var pnlHtml = '<span style="color:' + pnlColor + '">' + _fmtPnl(b.unrealized_pnl) + '</span>';
      if (b.unrealized_pnl_pct != null) {
        pnlHtml += ' <span class="bc-r-pnl-pct" style="color:' + pnlColor + '">' + _fmtPct(b.unrealized_pnl_pct) + '</span>';
      }
      pnlEl.innerHTML = pnlHtml;
    }
  }

  function _fetchBets(page, isAutoRefresh) {
    if (page < 1) return Promise.resolve();
    _state.page = page;

    if (_state.abortCtrl) {
      try { _state.abortCtrl.abort(); } catch (e) { }
    }
    _state.abortCtrl = typeof AbortController !== 'undefined' ? new AbortController() : null;

    _stopLivePrices();

    var cacheKey = _buildCacheKey();
    var cached = _getCached(cacheKey, page);

    if (cached && !isAutoRefresh) {
      _state.bets = cached.bets;
      _state.total = cached.total;
      _state.totalPages = cached.totalPages;
      _state.page = cached.page;
      _state.isLoading = false;
      _state.lastSignature = '';
      if (typeof lastHistoryBets !== 'undefined') {
        lastHistoryBets = _state.bets;
      }
      _renderCards(_state.bets);
      _renderPagination();

      var container = document.getElementById('bets-card-list');
      if (container) container.classList.add('bc-refreshing');

      return _doFetch(page, cacheKey, true).then(function () {
        if (container) container.classList.remove('bc-refreshing');
      });
    }

    if (!isAutoRefresh) {
      _state.isLoading = true;
      _state.lastSignature = '';
      var container = document.getElementById('bets-card-list');
      if (container) container.innerHTML = _renderLoadingSkeleton();
    }

    return _doFetch(page, cacheKey, false);
  }

  function _doFetch(page, cacheKey, isSilent) {
    var cat = _val('bets-filter-cat');
    var status = _val('bets-filter-status');
    var perPage = _val('bets-filter-perpage') || '25';
    var dim = _val('bets-filter-dim');
    var dateFrom = _val('bets-filter-from');
    var dateTo = _val('bets-filter-to');
    var search = _val('bets-filter-search');

    var url = '/api/bets/history?page=' + page + '&per_page=' + perPage;
    if (cat) url += '&category=' + cat;
    if (status) url += '&status=' + status;
    if (dim) url += '&dimension=' + dim;
    if (dateFrom) url += '&date_from=' + dateFrom;
    if (dateTo) url += '&date_to=' + dateTo;
    if (search) url += '&market_id=' + encodeURIComponent(search);
    var tagF = _val('bets-filter-tag');
    if (tagF) url += '&tag=' + encodeURIComponent(tagF);

    return api(url, { signal: _state.abortCtrl ? _state.abortCtrl.signal : undefined }).then(function (d) {
      var newBets = d.bets || [];
      var newTotal = d.total || 0;
      var newTotalPages = d.total_pages || 1;
      var newPage = d.page || 1;

      _setCache(cacheKey, page, { bets: newBets, total: newTotal, totalPages: newTotalPages, page: newPage });

      if (isSilent) {
        var oldSig = _computeSig(_state.bets);
        var newSig = _computeSig(newBets);
        if (oldSig === newSig && _state.total === newTotal) {
          var hasOpen = newBets.some(function (b) { return !b.resolved_outcome; });
          if (hasOpen) _startLivePrices();
          return;
        }
      }

      _state.bets = newBets;
      _state.total = newTotal;
      _state.totalPages = newTotalPages;
      _state.page = newPage;
      _state.isLoading = false;

      if (typeof lastHistoryBets !== 'undefined') {
        lastHistoryBets = _state.bets;
      }

      _state.lastSignature = '';
      _renderCards(_state.bets);
      _renderPagination();

      var hasOpen = _state.bets.some(function (b) { return !b.resolved_outcome; });
      if (hasOpen) _startLivePrices();

    }).catch(function (e) {
      if (e && e.name === 'AbortError') return;
      if (isSilent) return;
      _state.isLoading = false;
      var container = document.getElementById('bets-card-list');
      if (container) container.innerHTML = '<div class="bc-empty"><div class="bc-empty-icon">⚠️</div><div class="bc-empty-title">数据加载失败</div><div class="bc-empty-desc">请检查网络连接后重试</div></div>';
    });
  }

  function _val(id) {
    var el = document.getElementById(id);
    return el ? el.value : '';
  }

  function _debouncedFetch() {
    if (_state.debounceTimer) clearTimeout(_state.debounceTimer);
    _state.debounceTimer = setTimeout(function () {
      if (_state.manualUserMode) {
        _fetchManualUserBets(1, false);
      } else {
        _fetchBets(1);
      }
    }, 200);
  }

  function _fetchImmediate() {
    if (_state.debounceTimer) clearTimeout(_state.debounceTimer);
    if (_state.manualUserMode) {
      _fetchManualUserBets(1, false);
    } else {
      _fetchBets(1);
    }
  }

  function _t(key, fallback) {
    try {
      if (window.i18n && typeof window.i18n.t === 'function') return window.i18n.t(key, fallback);
    } catch (e) { }
    return fallback;
  }
  function _showRefreshToast(ok, msg) {
    try {
      var t = document.createElement('div');
      t.className = 'bets-refresh-toast ' + (ok ? 'ok' : 'err');
      t.textContent = msg;
      document.body.appendChild(t);
      requestAnimationFrame(function () { t.classList.add('show'); });
      setTimeout(function () {
        t.classList.remove('show');
        setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
      }, 1800);
    } catch (e) { }
  }
  function _refreshNow() {
    if (_state.debounceTimer) clearTimeout(_state.debounceTimer);
    var btn = document.getElementById('bets-refresh-btn');
    var label = btn ? btn.querySelector('span[data-i18n]') : null;
    var origLabel = label ? label.textContent : '';
    if (btn) {
      btn.classList.add('is-refreshing');
      btn.disabled = true;
      if (label) label.textContent = _t('trades.refresh.refreshing', 'Refreshing...');
    }
    var p;
    try {
      p = _state.manualUserMode ? _fetchManualUserBets(1, false) : _fetchBets(1);
    } catch (e) {
      p = Promise.reject(e);
    }
    if (!p || typeof p.then !== 'function') p = Promise.resolve();
    return p.then(function () {
      _showRefreshToast(true, _t('trades.refresh.success', 'Positions refreshed'));
    }, function () {
      _showRefreshToast(false, _t('trades.refresh.failed', 'Refresh failed'));
    }).then(function () {
      if (btn) {
        btn.classList.remove('is-refreshing');
        btn.disabled = false;
        if (label) label.textContent = origLabel || _t('trades.refresh.button', 'Refresh Now');
      }
    });
  }

  function _renderPagination() {
    var info = document.getElementById('bets-page-info');
    var prevBtn = document.getElementById('bets-page-prev');
    var nextBtn = document.getElementById('bets-page-next');
    var pageLabel = document.getElementById('bets-page-label');

    if (info) info.textContent = '共 ' + _state.total + ' 条记录';
    if (pageLabel) pageLabel.textContent = _state.page + ' / ' + _state.totalPages;
    if (prevBtn) prevBtn.disabled = _state.page <= 1;
    if (nextBtn) nextBtn.disabled = _state.page >= _state.totalPages;
  }

  function _setSort(col) {
    if (_state.sortCol === col) {
      _state.sortDir = _state.sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      _state.sortCol = col;
      _state.sortDir = 'desc';
    }
    document.querySelectorAll('.bets-sort-btn').forEach(function (btn) {
      btn.classList.toggle('active', btn.dataset.sort === col);
      var ico = btn.querySelector('.bets-sort-icon');
      if (ico) ico.textContent = btn.dataset.sort === col ? (_state.sortDir === 'asc' ? '▲' : '▼') : '⇅';
    });
    _state.lastSignature = '';
    _renderCards(_state.bets);
  }

  function _applyTimePeriod() {
    var period = _val('bets-filter-period');
    var fromEl = document.getElementById('bets-filter-from');
    var toEl = document.getElementById('bets-filter-to');
    var customRow = document.getElementById('bets-custom-dates');

    if (fromEl) fromEl.value = '';
    if (toEl) toEl.value = '';
    if (customRow) customRow.style.display = 'none';

    if (period === 'today') {
      if (fromEl) fromEl.value = new Date().toISOString().slice(0, 10);
    } else if (period === '7d') {
      var d = new Date(); d.setDate(d.getDate() - 7);
      if (fromEl) fromEl.value = d.toISOString().slice(0, 10);
    } else if (period === '30d') {
      var d = new Date(); d.setDate(d.getDate() - 30);
      if (fromEl) fromEl.value = d.toISOString().slice(0, 10);
    } else if (period === 'custom') {
      if (customRow) customRow.style.display = 'flex';
      return;
    }
    _fetchBets(1);
  }

  function _fetchManualUserBets(page, isAutoRefresh) {
    if (page < 1) page = 1;
    _state.page = page;

    var container = document.getElementById('bets-card-list');
    var shouldShowSkeleton = !isAutoRefresh && (!_state.bets || _state.bets.length === 0);
    if (shouldShowSkeleton && container) {
      container.innerHTML = _renderLoadingSkeleton();
    }

    if (container && !shouldShowSkeleton) container.classList.add('bc-refreshing');

    var status = _val('bets-filter-status');
    var cat = _val('bets-filter-cat');
    var perPage = _val('bets-filter-perpage') || '25';
    var search = _val('bets-filter-search');
    var url = '/api/user/pm/bets?page=' + page + '&per_page=' + encodeURIComponent(perPage) + '&refresh=0';
    if (status) url += '&status=' + encodeURIComponent(status);
    if (cat) url += '&category=' + encodeURIComponent(cat);
    if (search) url += '&market_id=' + encodeURIComponent(search);
    var tagF2 = _val('bets-filter-tag');
    if (tagF2) url += '&tag=' + encodeURIComponent(tagF2);

    return fetch(url, { credentials: 'include' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        _state.bets = (d && d.bets) ? d.bets : [];
        _state.total = (d && d.total) ? d.total : 0;
        _state.totalPages = (d && d.total_pages) ? d.total_pages : 1;
        _state.page = (d && d.page) ? d.page : 1;
        _state.isLoading = false;
        _state.lastSignature = '';

        _renderCards(_state.bets);
        _renderPagination();
        _stopLivePrices();

        var bgUrl = '/api/user/pm/bets?page=' + _state.page + '&per_page=' + encodeURIComponent(perPage) + '&refresh=1';
        if (status) bgUrl += '&status=' + encodeURIComponent(status);
        if (cat) bgUrl += '&category=' + encodeURIComponent(cat);
        if (search) bgUrl += '&market_id=' + encodeURIComponent(search);
        if (tagF2) bgUrl += '&tag=' + encodeURIComponent(tagF2);
        fetch(bgUrl, { credentials: 'include' })
          .then(function (rr) { return rr.json(); })
          .then(function (fresh) {
            if (!fresh || !Array.isArray(fresh.bets)) return;
            var freshSig = _computeSig(fresh.bets || []);
            var curSig = _computeSig(_state.bets || []);
            if (freshSig !== curSig || Number(fresh.total || 0) !== Number(_state.total || 0)) {
              _state.bets = fresh.bets || [];
              _state.total = fresh.total || 0;
              _state.totalPages = fresh.total_pages || 1;
              _state.page = fresh.page || _state.page;
              _state.lastSignature = '';
              _renderCards(_state.bets);
              _renderPagination();
            }
          })
          .catch(function () { });
      })
      .catch(function () {
        _state.isLoading = false;
        if (container && (!_state.bets || _state.bets.length === 0)) {
          container.innerHTML = '<div class="bc-empty"><div class="bc-empty-icon">⚠️</div><div class="bc-empty-title">加载 PM 持仓失败</div></div>';
        }
      })
      .finally(function () {
        if (container) container.classList.remove('bc-refreshing');
      });
  }

  function _onTabActivate() {
    _loadCategoryFilter();
    fetch("/api/user/me", { credentials: "include" })
      .then(function (r) { return r.json(); })
      .then(function (me) {
        var hasPmWallet = !!(me && (me.polymarket_wallet || me.pm_wallet_address));
        var shouldUsePmBets = !!(me && me.authenticated && !me.is_admin && hasPmWallet);
        if (shouldUsePmBets) {
          _state.manualUserMode = true;
          _fetchManualUserBets(_state.page, false);
        } else {
          _state.manualUserMode = false;
          _fetchBets(_state.page);
        }
      })
      .catch(function () {
        _state.manualUserMode = false;
        _fetchBets(_state.page);
      });
  }

  function _prefetchCommonFilters() {
    var perPage = _val('bets-filter-perpage') || '25';
    var statuses = ['open', 'resolved', ''];
    statuses.forEach(function (st) {
      var key = '|' + st + '|' + perPage + '||||';
      if (_getCached(key, 1)) return;
      var url = '/api/bets/history?page=1&per_page=' + perPage;
      if (st) url += '&status=' + st;
      api(url).then(function (d) {
        _setCache(key, 1, {
          bets: d.bets || [], total: d.total || 0,
          totalPages: d.total_pages || 1, page: d.page || 1
        });
      }).catch(function () { });
    });
  }

  function _onTabDeactivate() {
    _stopLivePrices();
  }

  var _CATEGORY_FB = {
    weather: 'Weather', sports: 'Sports', crypto: 'Crypto',
    finance: 'Finance', politics: 'Politics', geopolitics: 'Geopolitics',
    tech: 'Tech', technology: 'Tech', economy: 'Economy',
    pop_culture: 'Pop Culture', culture: 'Pop Culture',
    entertainment: 'Entertainment', esports: 'eSports',
    elections: 'Elections', science: 'Science', other: 'Other'
  };
  function _cnCategory(slug, fallback) {
    var s = String(slug || '').toLowerCase().trim();
    var fb = _CATEGORY_FB[s] || fallback || s;
    if (window.veyraI18n && typeof window.veyraI18n.t === 'function') {
      try { return window.veyraI18n.t('track.name.' + s, fb); } catch (e) { return fb; }
    }
    return fb;
  }

  function _loadCategoryFilter() {
    var sel = document.getElementById('bets-filter-cat');
    if (!sel) return Promise.resolve();

    var current = sel.value || '';
    var __t = (window.veyraI18n && typeof window.veyraI18n.t === 'function') ? window.veyraI18n.t.bind(window.veyraI18n) : function (k, fb) { return fb; };
    return fetch('/api/pm-categories/list', { credentials: 'include' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var categories = Array.isArray(d.categories) ? d.categories : [];
        sel.innerHTML = '<option value="">' + __t('trades.filter.allCategories', 'All categories') + '</option>';
        categories.forEach(function (c) {
          var slug = String(c.slug || '').trim();
          if (!slug) return;
          var label = _cnCategory(slug, String(c.label || slug).trim());
          var opt = document.createElement('option');
          opt.value = slug;
          opt.textContent = label;
          sel.appendChild(opt);
        });
        if (current && categories.some(function (c) { return c.slug === current; })) {
          sel.value = current;
        }
      })
      .catch(function () { });
  }

  function _onCategoryChanged() {
    _fetchImmediate();
  }

  return {
    fetchBets: _fetchBets,
    debouncedFetch: _debouncedFetch,
    fetchImmediate: _fetchImmediate,
    refreshNow: _refreshNow,
    setSort: _setSort,
    applyTimePeriod: _applyTimePeriod,
    onTabActivate: _onTabActivate,
    onTabDeactivate: _onTabDeactivate,
    onCategoryChanged: _onCategoryChanged,
    redeemBet: _redeemBet,
    prevPage: function () {
      if (_state.manualUserMode) return _fetchManualUserBets(_state.page - 1, false);
      _fetchBets(_state.page - 1);
    },
    nextPage: function () {
      if (_state.manualUserMode) return _fetchManualUserBets(_state.page + 1, false);
      _fetchBets(_state.page + 1);
    },
    getState: function () { return _state; },
    fetchPmPositions: _fetchManualUserBets,
  };

})();
