
(function () {
  'use strict';

  var tracksCache = { ts: 0, payload: null };

  function hasNum(v) {
    return v != null && isFinite(Number(v));
  }

  function n(v) {
    return hasNum(v) ? Number(v) : 0;
  }

  function t(key, fallback) {
    var i18n = window.veyraI18n;
    if (i18n && typeof i18n.t === 'function') {
      try { return i18n.t(key, fallback); } catch (_) { return fallback; }
    }
    return fallback;
  }

  function apiGet(path, opts) {
    if (typeof window.api === 'function') {
      return window.api(path, opts || {});
    }
    var options = opts || {};
    return fetch(path, {
      method: options.method || 'GET',
      credentials: 'include',
      headers: options.headers || {},
      body: options.body
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function pnlClass(v) {
    if (typeof window.pnlClass === 'function') return window.pnlClass(v);
    return Number(v) >= 0 ? 'pnl-pos' : 'pnl-neg';
  }

  function pnlStr(v) {
    if (typeof window.pnlStr === 'function') return window.pnlStr(v);
    var num = n(v);
    return (num >= 0 ? '+' : '') + num.toFixed(2);
  }

  function pctStr(v) {
    if (typeof window.pctStr === 'function') return window.pctStr(v);
    return (n(v) * 100).toFixed(1) + '%';
  }

  function moneyStr(v) {
    return '$' + n(v).toFixed(2);
  }

  function escHtml(v) {
    if (typeof window.escHtml === 'function') return window.escHtml(v);
    return String(v || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function setMetric(id, val) {
    var el = document.getElementById(id);
    if (!el) return;
    var v = n(val);
    el.textContent = pnlStr(v);
    el.className = 'stat-sm ' + pnlClass(v);
  }

  function fmtDrawdown(dd, ddPct) {
    var v = n(dd);
    var p = n(ddPct);
    if (Math.abs(v) < 0.005) v = 0;
    if (Math.abs(p) < 0.005) p = 0;
    return (v > 0 ? '-' : '') + Math.abs(v).toFixed(2) + ' (' + p + '%)';
  }

  function fetchRiskStatus() {
    return apiGet('/api/risk/status').then(function (d) {
      var panel = document.getElementById('risk-pause-panel');
      if (!panel) return;
      if (d && d.globally_paused) {
        panel.style.display = '';
        var reasonEl = document.getElementById('risk-pause-reason');
        var catsEl = document.getElementById('risk-pause-cats');
        if (reasonEl) reasonEl.textContent = t('dashboard.risk.reasonPrefix', 'Reason: ') + (d.pause_reason || t('common.unknown', 'Unknown'));
        if (catsEl) {
          var cats = d.paused_categories;
          catsEl.textContent = (cats && cats.length)
            ? t('dashboard.risk.pausedCategoriesPrefix', 'Paused tracks: ') + cats.join(', ')
            : '';
        }
      } else {
        panel.style.display = 'none';
      }
    }).catch(function () { });
  }

  function doRiskResume() {
    var p = (window.vp && window.vp.confirm)
      ? window.vp.confirm({
        title: t('dashboard.risk.resumeConfirmTitle', 'Resume Risk Control'),
        body: t('dashboard.risk.resumeConfirmBody', 'Confirm to resume trading?'),
        okText: t('dashboard.risk.resumeBtn', 'Resume Trading')
      })
      : Promise.resolve(window.confirm(t('dashboard.risk.resumeConfirmBody', 'Confirm to resume trading?')));

    p.then(function (ok) {
      if (!ok) return;
      var btn = document.getElementById('btn-risk-resume');
      if (btn) {
        btn.disabled = true;
        btn.textContent = t('dashboard.risk.resuming', 'Resuming...');
      }
      return apiGet('/api/risk/resume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: 'RESUME' })
      }).then(function (d) {
        if (d && (d.ok === true || d.status === 'ok')) {
          if (window.vp && window.vp.success) {
            window.vp.success({ title: t('dashboard.risk.resumedTitle', 'Risk Resumed') });
          }
          var panel = document.getElementById('risk-pause-panel');
          if (panel) panel.style.display = 'none';
        } else if (window.vp && window.vp.error) {
          window.vp.error(d || { error: 'resume failed' }, { title: t('dashboard.risk.resumeFailed', 'Resume Failed') });
        }
      }).catch(function (e) {
        if (window.vp && window.vp.error) {
          window.vp.error(e, { title: t('common.requestFailed', 'Request Failed') });
        }
      }).finally(function () {
        if (btn) {
          btn.disabled = false;
          btn.textContent = t('dashboard.risk.resumeBtn', 'Resume Trading');
        }
      });
    });
  }

  function fetchStatus() {
    return apiGet('/api/status').then(function (d) {
      var b = d && d.bot;
      var badge = document.getElementById('bot-badge') || document.getElementById('top-bot-badge');
      if (badge && b) {
        if (b.running) {
          badge.textContent = 'running';
          badge.className = badge.id === 'top-bot-badge' ? 'nav-badge nav-badge-green' : 'badge badge-green';
        } else {
          badge.textContent = 'stopped';
          badge.className = badge.id === 'top-bot-badge' ? 'nav-badge nav-badge-red' : 'badge badge-red';
        }
      }
    }).catch(function () { });
  }

  function applyManualMetrics(m) {
    if (!m || m.status !== 'ok') return;
    var pmNlv = n(m.nlv);
    var pmBal = n(m.balance);
    var pmPos = n(m.positions_value);
    var pmOpen = Number(m.open_count || 0);
    var pmTotalPnl = n(m.total_pnl);
    var pmRealized = n(m.realized_pnl);
    var pmUnrealized = n(m.unrealized_pnl);

    var nlvEl = document.getElementById('s-nlv');
    if (nlvEl) nlvEl.textContent = moneyStr(pmNlv);

    var balEl = document.getElementById('s-balance');
    if (balEl) balEl.textContent = moneyStr(pmBal);

    var depEl = document.getElementById('s-deployed');
    if (depEl) {
      depEl.textContent = 'Deployed: ' + moneyStr(pmPos) + ' · ' + String(pmOpen) + ' positions';
    }

    var totalPnlEl = document.getElementById('s-total-pnl');
    if (totalPnlEl) {
      totalPnlEl.textContent = pnlStr(pmTotalPnl);
      totalPnlEl.className = 'stat-sm ' + pnlClass(pmTotalPnl);
    }

    var bkEl = document.getElementById('s-pnl-breakdown');
    if (bkEl) {
      bkEl.innerHTML = t('dashboard.metric.realized', 'Realized') + ': <span class="' + pnlClass(pmRealized) + '">' + pnlStr(pmRealized) + '</span>' +
        ' · ' + t('dashboard.metric.unrealized', 'Unrealized') + ': <span class="' + pnlClass(pmUnrealized) + '">' + pnlStr(pmUnrealized) + '</span>';
    }

    var roiEl = document.getElementById('s-roi');
    if (roiEl) {
      roiEl.textContent = 'ROI: ' + pctStr(n(m.roi)) + ' · ' +
        t('dashboard.metric.invested', 'Invested') + ': ' + moneyStr(m.total_staked) + ' · ' +
        n(m.resolved_count) + '/' + n(m.total) + ' ' + t('dashboard.metric.settled', 'settled');
    }

    setMetric('m-today-pnl', m.today_pnl);
    setMetric('m-week-pnl', m.week_pnl);

    var sharpeEl = document.getElementById('m-sharpe');
    if (sharpeEl) {
      var sv = n(m.sharpe_ratio);
      sharpeEl.textContent = sv.toFixed(2);
      sharpeEl.className = 'stat-sm ' + (sv >= 0 ? 'text-success' : 'text-danger');
    }

    var wrEl = document.getElementById('m-winrate');
    if (wrEl) {
      var wr = n(m.win_rate);
      wrEl.textContent = pctStr(wr);
      wrEl.className = 'stat-sm ' + (wr >= 0.5 ? 'text-cyan' : 'text-yellow');
    }

    var maxddEl = document.getElementById('m-maxdd');
    if (maxddEl) {
      maxddEl.textContent = fmtDrawdown(n(m.max_drawdown), n(m.max_drawdown_pct));
      maxddEl.className = 'stat-sm text-amber-300';
    }

    var utilEl = document.getElementById('m-util');
    if (utilEl) utilEl.textContent = n(m.capital_utilization) + '%';
  }

  function fetchSummary() {
    var summaryP = apiGet('/api/summary').catch(function () { return {}; });
    var meP = fetch('/api/user/me', { credentials: 'include' })
      .then(function (r) { return r.ok ? r.json() : { authenticated: false }; })
      .catch(function () { return { authenticated: false }; });

    return Promise.all([summaryP, meP]).then(function (results) {
      var d = results[0] || {};
      var me = results[1] || {};
      var isManualUser = !!(me && me.authenticated && me.role === 'manual_user');

      if (isManualUser) {
        return fetch('/api/user/pm/metrics', { credentials: 'include' })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (localMetrics) {
            if (localMetrics) applyManualMetrics(localMetrics);
            return fetch('/api/user/pm/metrics?refresh=1', { credentials: 'include' })
              .then(function (r) { return r.ok ? r.json() : null; })
              .then(function (freshMetrics) {
                if (freshMetrics && (!localMetrics || freshMetrics.last_snapshot_at !== localMetrics.last_snapshot_at)) {
                  applyManualMetrics(freshMetrics);
                }
              }).catch(function () { });
          }).catch(function () { });
      }

      return apiGet('/api/portfolio').catch(function () { return {}; }).then(function (p) {
        var nlvEl = document.getElementById('s-nlv');
        if (nlvEl) nlvEl.textContent = moneyStr(p.nlv);

        var balanceEl = document.getElementById('s-balance');
        if (balanceEl) balanceEl.textContent = moneyStr(p.balance);

        var deployedTpl = 'Deployed: {value} · {n} positions';
        var deployedCostTpl = 'Deployed: {mv} (cost {cost}) · {n} positions';
        var mvText = (p.market_value != null && p.market_value !== p.total_deployed)
          ? deployedCostTpl.replace('{mv}', moneyStr(p.market_value)).replace('{cost}', moneyStr(p.total_deployed)).replace('{n}', String(p.open_count || 0))
          : deployedTpl.replace('{value}', moneyStr(p.total_deployed)).replace('{n}', String(p.open_count || 0));
        var deployedEl = document.getElementById('s-deployed');
        if (deployedEl) deployedEl.textContent = mvText;

        var totalPnl = n(p.total_pnl);
        var totalPnlEl = document.getElementById('s-total-pnl');
        if (totalPnlEl) {
          totalPnlEl.textContent = pnlStr(totalPnl);
          totalPnlEl.className = 'stat-sm ' + pnlClass(totalPnl);
        }

        var realizedPnl = n(p.realized_pnl);
        var unrealizedPnl = n(p.unrealized_pnl);
        var bkEl = document.getElementById('s-pnl-breakdown');
        if (bkEl) {
          bkEl.innerHTML = t('dashboard.metric.realized', 'Realized') + ': <span class="' + pnlClass(realizedPnl) + '">' + pnlStr(realizedPnl) + '</span>' +
            ' · ' + t('dashboard.metric.unrealized', 'Unrealized') + ': <span class="' + pnlClass(unrealizedPnl) + '">' + pnlStr(unrealizedPnl) + '</span>';
        }

        var roiEl = document.getElementById('s-roi');
        if (roiEl) {
          roiEl.textContent = 'ROI: ' + pctStr(n(d.roi)) + ' · ' +
            t('dashboard.metric.invested', 'Invested') + ': ' + moneyStr(d.total_staked) + ' · ' +
            (d.resolved || 0) + '/' + (d.total || 0) + ' ' + t('dashboard.metric.settled', 'settled') +
            (n(d.total_fees) > 0 ? ' · ' + t('dashboard.metric.fees', 'Fees') + ': $' + n(d.total_fees).toFixed(2) : '');
        }

        setMetric('m-today-pnl', p.today_pnl);
        setMetric('m-week-pnl', p.week_pnl);

        var sharpeEl = document.getElementById('m-sharpe');
        if (sharpeEl) {
          var sv = n(p.sharpe_ratio);
          sharpeEl.textContent = sv.toFixed(2);
          sharpeEl.className = 'stat-sm ' + (sv >= 0 ? 'text-success' : 'text-danger');
        }

        var maxddEl = document.getElementById('m-maxdd');
        if (maxddEl) {
          maxddEl.textContent = fmtDrawdown(n(p.max_drawdown), n(p.max_drawdown_pct));
          maxddEl.className = 'stat-sm text-amber-300';
        }

        var wrEl = document.getElementById('m-winrate');
        if (wrEl) {
          var wr = n(p.win_rate);
          wrEl.textContent = pctStr(wr);
          wrEl.className = 'stat-sm ' + (wr >= 0.5 ? 'text-cyan' : 'text-yellow');
        }

        var utilEl = document.getElementById('m-util');
        if (utilEl) utilEl.textContent = n(p.capital_utilization) + '%';
      });
    }).catch(function (e) {
      console.error('fetchSummary error', e);
    });
  }

  function renderTracksPayload(d) {
    var rawTracks = (d && d.tracks) || [];
    var mergedByCat = {};
    rawTracks.forEach(function (row) {
      var cat = String((row && row.category) || '').toLowerCase();
      if (!cat || cat === 'unknown' || cat === 'default') cat = 'economy';
      if (!mergedByCat[cat]) {
        mergedByCat[cat] = {
          category: cat,
          total: 0,
          resolved: 0,
          wins: 0,
          pnl: 0,
          staked: 0,
          win_rate: 0,
          roi: 0
        };
      }
      mergedByCat[cat].total += Number(row.total || 0);
      mergedByCat[cat].resolved += Number(row.resolved || 0);
      mergedByCat[cat].wins += Number(row.wins || 0);
      mergedByCat[cat].pnl += Number(row.pnl || 0);
      mergedByCat[cat].staked += Number(row.staked || 0);
    });

    var tracks = Object.keys(mergedByCat).map(function (cat) {
      var v = mergedByCat[cat];
      v.win_rate = v.resolved > 0 ? v.wins / v.resolved : 0;
      v.roi = v.staked > 0 ? v.pnl / v.staked : 0;
      return v;
    }).sort(function (a, b) { return b.total - a.total; });

    var container = document.getElementById('tracks-container');
    if (!container) return;

    if (!tracks.length) {
      container.innerHTML = '<div class="track-loading">' + t('dashboard.tracks.empty', 'No track data yet') + '</div>';
      return;
    }

    var totalCount = tracks.reduce(function (acc, row) { return acc + Number(row.total || 0); }, 0);
    var maxTotal = Math.max.apply(null, tracks.map(function (row) { return row.total; }).concat([1]));
    var posLbl = t('dashboard.tracks.positions', 'pos');
    var shareLbl = t('dashboard.tracks.share', 'Share');
    var settledLbl = t('dashboard.tracks.settled', 'Settled');
    var winLbl = t('dashboard.tracks.winrate', 'Win Rate');

    container.innerHTML = tracks.map(function (row) {
      var icon = (window.TRACK_ICONS && window.TRACK_ICONS[row.category]) || 'Other';
      var color = (window.TRACK_COLORS && window.TRACK_COLORS[row.category]) || '#64748b';
      var barW = Math.max(4, Math.round((row.total / maxTotal) * 100));
      var share = totalCount > 0 ? ((row.total / totalCount) * 100).toFixed(1) : '0.0';
      var pnlColor = Number(row.pnl || 0) >= 0 ? '#22c55e' : '#ef4444';
      var roiColor = Number(row.roi || 0) >= 0 ? '#22c55e' : '#ef4444';
      var winMarker = Math.max(0, Math.min(100, Math.round(Number(row.win_rate || 0) * 100)));

      return '<div class="track-card">' +
        '<div class="flex items-center justify-between">' +
        '<span class="track-title">' + escHtml(icon) + '</span>' +
        '<span class="text-xs font-mono" style="color:' + pnlColor + '">' + pnlStr(row.pnl) + '</span>' +
        '</div>' +
        '<div class="track-sub">' + row.total + ' ' + posLbl + ' · ' + shareLbl + ' ' + share + '%</div>' +
        '<div class="track-progress-wrap">' +
        '<div class="track-progress-fill" style="width:' + barW + '%;background:' + color + '"></div>' +
        '<div class="track-progress-marker" style="left:' + winMarker + '%" title="' + winLbl + ' ' + (row.resolved > 0 ? pctStr(row.win_rate) : '—') + '"></div>' +
        '</div>' +
        '<div class="track-stats">' +
        '<span>' + settledLbl + ': ' + row.resolved + '</span>' +
        '<span>' + winLbl + ': ' + (row.resolved > 0 ? pctStr(row.win_rate) : '—') + '</span>' +
        '<span style="color:' + roiColor + '">ROI: ' + (row.staked > 0 ? pctStr(row.roi) : '—') + '</span>' +
        '</div></div>';
    }).join('');
  }

  function fetchTracks() {
    var now = Date.now();
    if (tracksCache.payload && now - tracksCache.ts < 60000) {
      renderTracksPayload(tracksCache.payload);
      return Promise.resolve(tracksCache.payload);
    }

    return apiGet('/api/tracks').then(function (d) {
      tracksCache.ts = Date.now();
      tracksCache.payload = d || {};
      renderTracksPayload(tracksCache.payload);
      return tracksCache.payload;
    }).catch(function () { });
  }

  function fetchCalibration() {
    return Promise.resolve();
  }

  function refresh() {
    return Promise.all([
      fetchStatus(),
      fetchRiskStatus(),
      fetchSummary(),
      fetchTracks(),
      fetchCalibration()
    ]);
  }

  window.fetchStatus = fetchStatus;
  window.fetchRiskStatus = fetchRiskStatus;
  window.fetchSummary = fetchSummary;
  window.fetchTracks = fetchTracks;
  window.fetchCalibration = fetchCalibration;
  window.refresh = refresh;
  window.doRiskResume = doRiskResume;
})();
