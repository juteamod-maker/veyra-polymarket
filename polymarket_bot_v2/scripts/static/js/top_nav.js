
(function () {
  'use strict';

  function _t(key, fallback) {
    if (window.i18n && typeof window.i18n.t === 'function') return window.i18n.t(key, fallback);
    var zh = {
      'nav.overview': '概览', 'nav.betslist': '持仓', 'nav.weathermap': '天气',
      'nav.polymarket': '机会', 'nav.copytraders': '跟投',
      'nav.analysis': '分析', 'nav.ordermonitor': '追踪', 'nav.users': '用户', 'nav.strategy': '配置',
      'nav.manage': '管理',
      'status.checking': '检查中...', 'status.running': '运行中',
      'status.stopped': '已停止', 'status.error': '连接失败',
      'status.dry_run': 'DRY RUN', 'status.live': 'LIVE', 'status.frozen': '冻结',
      'status.editing': '编辑中·刷新已暂停'
    };
    return zh[key] || fallback || key;
  }

  function _svg(path) {
    return '<svg viewBox="0 0 24 24">' + path + '</svg>';
  }

  var NAV_ITEMS = [
    { key: 'overview',    i18nKey: 'nav.overview',    href: '/Overview',    icon: _svg('<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="14" rx="1"/>') },
    { key: 'betslist',    i18nKey: 'nav.betslist',    href: '/Betslist',    icon: _svg('<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>') },
    { key: 'weathermap',  i18nKey: 'nav.weathermap',  href: '/WeatherMap',  icon: _svg('<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>') },
    { key: 'polymarket',  i18nKey: 'nav.polymarket',  href: '/Polymarket',  icon: _svg('<path d="M4 4h16v16H4z"/><path d="M8 14l3-3 2 2 3-4"/>') },
    { key: 'copytraders', i18nKey: 'nav.copytraders', href: '/Copytrader', icon: _svg('<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>') },
    { key: 'analysis',    i18nKey: 'nav.analysis',    href: '/Analysis',    icon: _svg('<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>') },
    { key: 'ordermonitor',i18nKey: 'nav.ordermonitor',href: '/Ordermonitor',icon: _svg('<path d="M3 4h18"/><path d="M3 12h18"/><path d="M3 20h18"/><circle cx="7" cy="4" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="17" cy="20" r="1.5"/>') },
    { key: 'strategy',    i18nKey: 'nav.strategy',    href: '/Strategy',    icon: _svg('<path d="M20 7h-9"/><path d="M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>'), adminOnly: true }
  ];

  var _cachedRole = null;

  function _setRoleUiVisibility(role) {
    var isAdmin = role === 'admin';
    var botBadge = document.getElementById('top-bot-badge');
    var modeBadge = document.getElementById('top-mode-badge');
    var manageBadge = document.getElementById('top-manage-badge');
    if (botBadge) botBadge.style.display = isAdmin ? '' : 'none';
    if (modeBadge) modeBadge.style.display = isAdmin ? '' : 'none';
    if (manageBadge) manageBadge.style.display = isAdmin ? '' : 'none';
  }

  function _applyRoleFilter(role) {
    _cachedRole = role;
    _setRoleUiVisibility(role);
    NAV_ITEMS.forEach(function (item) {
      if (item.adminOnly) {
        var link = document.querySelector('.nav-tabs a[data-admin-only="1"][href^="' + item.href + '"]');
        if (link) link.style.display = (role === 'admin') ? '' : 'none';
      }
    });
  }

  function _fetchUserRole() {
    fetch('/api/user/me' + _buildApiQuery(), { credentials: 'include' })
      .then(function (r) { return r.ok ? r.json() : { authenticated: false }; })
      .then(function (d) {
        if (d && d.authenticated && d.role) {
          _applyRoleFilter(d.role);
        }
      })
      .catch(function () {  });
  }

  function _bindLogout() {
    var btn = document.getElementById('top-logout-btn');
    if (!btn) return;
    btn.addEventListener('click', function () {
      fetch('/api/user/logout', { method: 'POST', credentials: 'include' })
        .finally(function () {
          document.cookie = 'user_session=; Max-Age=0; path=/';
          document.cookie = 'dashboard_key=; Max-Age=0; path=/';
          document.cookie = 'totp_session=; Max-Age=0; path=/';
          window.location.href = '/Index';
        });
    });
  }

  function _buildQuery() {
    var sp = new URLSearchParams(window.location.search || '');
    var out = new URLSearchParams();
    var key = sp.get('key') || '';
    var totp = sp.get('totp_session') || '';
    if (key) out.set('key', key);
    if (totp) out.set('totp_session', totp);
    var q = out.toString();
    return q ? ('?' + q) : '';
  }

  function _buildApiQuery() {
    var sp = new URLSearchParams(window.location.search || '');
    var key = sp.get('key') || '';
    return key ? ('?key=' + encodeURIComponent(key)) : '';
  }

  function _hasAdminKeyInQuery() {
    return !!(new URLSearchParams(window.location.search || '').get('key'));
  }

  function _renderNav(activeKey, options) {
    options = options || {};
    var query = _buildQuery();
    var tabsHtml = NAV_ITEMS.map(function (item) {
      var label = _t(item.i18nKey, item.key);
      var cls = item.key === activeKey ? 'nav-tab active' : 'nav-tab';
      var extra = item.adminOnly ? ' data-admin-only="1" style="display:none"' : '';
      return '<a href="' + item.href + query + '" class="' + cls + '" title="' + label + '"' + extra + '>' +
        item.icon + '<span class="nav-label">' + label + '</span></a>';
    }).join('');

    var refreshPausedId = options.refreshPausedId || '';
    var freezeId = options.freezeBadgeId || 'top-freeze-badge';
    var botId = options.botBadgeId || 'top-bot-badge';
    var modeId = options.modeBadgeId || 'top-mode-badge';
    var showBotMode = _cachedRole === 'admin';
    var showManage = (_cachedRole === 'admin') || (_cachedRole === null && _hasAdminKeyInQuery());

    var refreshPausedHtml = '';
    if (refreshPausedId) {
      refreshPausedHtml = '<span id="' + refreshPausedId + '" style="display:none">' + _t('status.editing') + '</span>';
    }

    return '<div class="nav-brand">' +
      '<img class="nav-brand-logo" src="/static/Veyra.png" alt="Veyra" onerror="this.style.display=\'none\'" />' +
      '</div>' +
      '<nav class="nav-tabs">' + tabsHtml + '</nav>' +
      '<div class="nav-status-area">' +
      refreshPausedHtml +
      '<span id="' + freezeId + '" class="nav-badge nav-badge-freeze" style="display:none">' + _t('status.frozen') + '</span>' +
        '<span id="' + botId + '" class="nav-badge nav-badge-gray" style="display:' + (showBotMode ? '' : 'none') + '">' + _t('status.checking') + '</span>' +
        '<span id="' + modeId + '" class="nav-badge nav-badge-yellow" style="display:' + (showBotMode ? '' : 'none') + '">' + _t('status.dry_run') + '</span>' +
        '<a id="top-manage-badge" href="/System' + query + '" class="nav-badge nav-badge-freeze" style="display:' + (showManage ? '' : 'none') + ';text-decoration:none">' + _t('nav.manage', '管理') + '</a>' +
        '<a href="/Users' + query + '" class="nav-badge nav-badge-gray" style="text-decoration:none">用户中心</a>' +
        '<button id="top-logout-btn" class="nav-badge nav-badge-gray" style="cursor:pointer;border:0">退出</button>' +
      '</div>';
  }

  var _pollTimer = null;

  function _fetchStatus(botId, modeId, freezeId) {
    var q = _buildApiQuery();
    fetch('/api/status' + q)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var b = d.bot || {};
        var badge = document.getElementById(botId);
        if (badge && _cachedRole === 'admin') {
          if (b.running) {
            badge.textContent = _t('status.running');
            badge.className = 'nav-badge nav-badge-green';
          } else {
            badge.textContent = _t('status.stopped');
            badge.className = 'nav-badge nav-badge-red';
          }
        }
        var modeBadge = document.getElementById(modeId);
        if (modeBadge && _cachedRole === 'admin') {
          if (d.dry_run) {
            modeBadge.textContent = _t('status.dry_run');
            modeBadge.className = 'nav-badge nav-badge-yellow';
          } else {
            modeBadge.textContent = _t('status.live');
            modeBadge.className = 'nav-badge nav-badge-red';
          }
        }
        var freezeBadge = document.getElementById(freezeId);
        if (freezeBadge) {
          if (_cachedRole !== 'admin') {
            freezeBadge.style.display = 'none';
            return;
          }
          var isFrozen = !!(d.emergency_freeze || d.frozen);
          if (isFrozen) {
            freezeBadge.style.display = '';
            freezeBadge.textContent = _t('status.frozen');
            freezeBadge.style.opacity = '1';
          } else {
            freezeBadge.style.display = 'none';
          }
        }
      })
      .catch(function () {
        var badge = document.getElementById(botId);
        if (badge && _cachedRole === 'admin') {
          badge.textContent = _t('status.error');
          badge.className = 'nav-badge nav-badge-red';
        }
      });
  }

  window.TopNav = {
    render: function (headerId, activeKey, options) {
      var el = document.getElementById(headerId || 'top-header');
      if (!el) return;
      el.innerHTML = _renderNav(activeKey || '', options || {});
      _fetchUserRole();
      _bindLogout();
    },

    startPolling: function (options) {
      options = options || {};
      var botId    = options.botBadgeId    || 'top-bot-badge';
      var modeId   = options.modeBadgeId   || 'top-mode-badge';
      var freezeId = options.freezeBadgeId || 'top-freeze-badge';
      var interval = options.interval      || 15000;

      _fetchStatus(botId, modeId, freezeId);

      if (_pollTimer) clearInterval(_pollTimer);
      _pollTimer = setInterval(function () {
        _fetchStatus(botId, modeId, freezeId);
      }, interval);
    },

    stopPolling: function () {
      if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
    }
  };

  if (window.i18n && typeof window.i18n.onLangChange === 'function') {
    window.i18n.onLangChange(function () {
      var header = document.getElementById('top-header');
      if (!header) return;
      var activeLink = header.querySelector('.nav-tab.active');
      var activeKey = '';
      if (activeLink) {
        var href = activeLink.getAttribute('href') || '';
        NAV_ITEMS.forEach(function (item) {
          if (href.indexOf(item.href) === 0) activeKey = item.key;
        });
      }
      header.innerHTML = _renderNav(activeKey);
    });
  }
})();
