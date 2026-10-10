(function() {
  "use strict";
  function t(key, fallback) {
    if (window.veyraI18n && typeof window.veyraI18n.t === "function") {
      return window.veyraI18n.t(key, fallback) || fallback || "";
    }
    return fallback || "";
  }
  function esc(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function fmtNum(n) {
    var v = parseFloat(n) || 0;
    if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return v.toFixed(0);
  }
  function marqueeAge(ts) {
    return Math.max(0, Math.floor(Date.now() / 1e3 - Number(ts || 0)));
  }
  function marqueeRel(ts) {
    var a = marqueeAge(ts);
    if (a < 60) return a + " " + t("time.secAgo", "s ago");
    if (a < 3600) return Math.floor(a / 60) + " " + t("time.minAgo", "m ago");
    if (a < 86400) return Math.floor(a / 3600) + " " + t("time.hourAgo", "h ago");
    return Math.floor(a / 86400) + " " + t("time.dayAgo", "d ago");
  }
  var state = {
    sidebarOpen: true,
    sidebarMobile: false,
    requiresPm: false,
    _pmBooted: false,
    marqueeItems: [],
    marqueeLoaded: false,
    notifications: [],
    notifLoaded: false,
    notifOpen: false,
    unreadCount: 0,
    lastReadTs: 0,
    langOpen: false,
    userOpen: false,
    _i18nBound: false,
    _marqueeTimer: null,
    _notifTimer: null,
    _pmProbeTimer: null
  };
  var dom = {};
  function $(id) {
    return document.getElementById(id);
  }
  function cls(el, add, toggle) {
    if (!el) return;
    if (toggle === undefined) {
      if (add) el.classList.add(add);
    } else {
      el.classList.toggle(add, toggle);
    }
  }
  function applySidebarState() {
    cls(dom.sidebar, "collapsed", !state.sidebarOpen);
    cls(dom.mainHeader, "sidebar-collapsed", !state.sidebarOpen);
    cls(dom.mainContent, "sidebar-collapsed", !state.sidebarOpen);
    try {
      localStorage.setItem("vp_sidebar_open", state.sidebarOpen ? "1" : "0");
    } catch (_) {}
  }
  function applyMobileState() {
    cls(dom.sidebar, "mobile-open", state.sidebarMobile);
    cls(dom.sidebarOverlay, "show", state.sidebarMobile);
  }
  function buildMarqueeHtml(items) {
    if (!items || !items.length) return "";
    var doubled = items.concat(items);
    return '<div class="header-marquee-track">' + doubled.map(function(it) {
      var age = marqueeAge(it.timestamp);
      var badgeClass = age <= 30 ? "fresh" : it.notional >= 5e3 ? "big" : "";
      var badgeText = age <= 30 ? t("marquee.justNow", "Just now") : it.notional >= 5e3 ? t("marquee.bigTrade", "Big") : marqueeRel(it.timestamp);
      var sideColor = it.side === "BUY" ? "#22c55e" : "#ef4444";
      var sideText = it.side === "BUY" ? t("header.notif.buy", "BUY") : t("header.notif.sell", "SELL");
      var outcomeClass = String(it.outcome || "").toUpperCase() === "YES" ? "yes" : "no";
      var notional = "$" + (parseFloat(it.notional) || 0).toLocaleString(undefined, {
        maximumFractionDigits: 0
      });
      return '<span class="hmq-item">' + '<span class="hmq-badge ' + esc(badgeClass) + '">' + esc(badgeText) + "</span>" + '<span style="color:' + sideColor + '">' + esc(sideText) + "</span>" + "<b>" + esc((it.title || t("marquee.unknownMarket", "?")).slice(0, 36)) + "</b>" + '<span class="hmq-badge ' + esc(outcomeClass) + '">' + esc(it.outcome || "?") + "</span>" + "<span>" + esc(notional) + "</span>" + "</span>";
    }).join("") + "</div>";
  }
  function renderMarquee() {
    if (!dom.marquee) return;
    var empty = dom.marqueeEmpty;
    if (!state.requiresPm) {
      if (empty) {
        empty.textContent = "";
        empty.style.display = "none";
      }
      dom.marquee.innerHTML = "";
      return;
    }
    if (state.marqueeItems.length === 0) {
      if (empty) {
        empty.style.display = "";
        empty.textContent = state.marqueeLoaded ? t("marquee.noData", "—") : t("marquee.loading", "…");
      }
      dom.marquee.innerHTML = "";
    } else {
      if (empty) {
        empty.style.display = "none";
      }
      dom.marquee.innerHTML = buildMarqueeHtml(state.marqueeItems);
    }
  }
  async function fetchMarquee() {
    if (!state.requiresPm) {
      state.marqueeLoaded = true;
      return;
    }
    try {
      var r = await fetch("/api/pm/marquee?limit=20", {
        credentials: "include",
        cache: "no-store"
      });
      if (!r.ok) {
        state.marqueeLoaded = true;
        renderMarquee();
        return;
      }
      var d = await r.json().catch(function() {
        return {};
      });
      if (Array.isArray(d.trades) && d.trades.length) {
        state.marqueeItems = d.trades;
        try {
          localStorage.setItem("vp_marquee_cache", JSON.stringify(d.trades.slice(0, 20)));
        } catch (_) {}
      }
      state.marqueeLoaded = true;
    } catch (_) {
      state.marqueeLoaded = true;
    }
    renderMarquee();
  }
  function isUnread(n) {
    return Number(n.timestamp || 0) > Number(state.lastReadTs || 0);
  }
  function recomputeUnread() {
    state.unreadCount = state.notifications.filter(isUnread).length;
  }
  function renderNotifDot() {
    if (dom.notifDot) dom.notifDot.classList.toggle("on", state.unreadCount > 0);
  }
  function renderNotifDropdown() {
    if (!dom.notifList) return;
    if (state.notifications.length === 0) {
      dom.notifList.innerHTML = "";
      if (dom.notifEmpty) {
        dom.notifEmpty.style.display = "";
        dom.notifEmpty.textContent = state.notifLoaded ? t("header.notif.empty", "No notifications") : t("header.notif.loading", "Loading…");
      }
    } else {
      if (dom.notifEmpty) dom.notifEmpty.style.display = "none";
      dom.notifList.innerHTML = state.notifications.map(function(n) {
        var sideColor = n.side === "BUY" ? "#22c55e" : "#ef4444";
        var sideIcon = n.side === "BUY" ? "↑" : "↓";
        var sideLabel = n.side === "BUY" ? t("header.notif.buy", "BUY") : t("header.notif.sell", "SELL");
        var unread = isUnread(n) ? " unread" : "";
        return '<div class="notif-item' + unread + '">' + '<div class="notif-item-icon" style="color:' + sideColor + '">' + sideIcon + "</div>" + '<div class="notif-item-body">' + '<div class="notif-item-title">' + esc(n.title || t("header.notif.title", "Trade")) + "</div>" + '<div class="notif-item-meta">' + "<span>" + esc(sideLabel + " " + (n.outcome || "")) + "</span>" + "<span>" + esc((parseFloat(n.size) || 0).toFixed(0)) + " · $" + esc((parseFloat(n.notional) || 0).toFixed(2)) + "</span>" + "<span>" + esc(marqueeRel(n.timestamp)) + "</span>" + "</div>" + "</div>" + "</div>";
      }).join("");
    }
    if (dom.notifUnreadWrap) {
      dom.notifUnreadWrap.style.display = state.unreadCount > 0 ? "" : "none";
      dom.notifUnreadWrap.textContent = "· " + state.unreadCount + " " + t("header.notif.unreadSuffix", "unread");
    }
    if (dom.notifClearBtn) {
      dom.notifClearBtn.style.display = state.notifications.length > 0 ? "" : "none";
    }
  }
  function _hasUserSessionCookie() {
    try {
      return /(?:^|;\s*)user_session=/.test(document.cookie || "");
    } catch (_) {
      return false;
    }
  }
  async function fetchNotifications() {
    if (!state.requiresPm) {
      state.notifLoaded = true;
      renderNotifDropdown();
      return;
    }
    if (!_hasUserSessionCookie()) {
      state.notifLoaded = true;
      renderNotifDropdown();
      return;
    }
    try {
      var r = await fetch("/api/pm/notifications?limit=20", {
        credentials: "include",
        cache: "no-store"
      });
      if (!r.ok) {
        state.notifLoaded = true;
        renderNotifDropdown();
        return;
      }
      var d = await r.json().catch(function() {
        return {};
      });
      if (Array.isArray(d.notifications)) {
        state.notifications = d.notifications;
        recomputeUnread();
      }
      state.notifLoaded = true;
    } catch (_) {
      state.notifLoaded = true;
    }
    renderNotifDropdown();
    renderNotifDot();
  }
  function markAllRead() {
    if (state.notifications.length === 0) {
      state.unreadCount = 0;
      renderNotifDot();
      return;
    }
    var maxTs = Math.max.apply(null, state.notifications.map(function(n) {
      return Number(n.timestamp || 0);
    }));
    state.lastReadTs = maxTs;
    try {
      localStorage.setItem("vp_notif_last_read", String(maxTs));
    } catch (_) {}
    state.unreadCount = 0;
    renderNotifDropdown();
    renderNotifDot();
  }
  function toggleNotif() {
    state.notifOpen = !state.notifOpen;
    if (dom.notifDropdown) dom.notifDropdown.classList.toggle("show", state.notifOpen);
    if (state.notifOpen) fetchNotifications();
  }
  var LANG_FLAGS = {
    "zh-CN": "🇨🇳",
    en: "🇺🇸",
    fr: "🇫🇷",
    ja: "🇯🇵",
    ko: "🇰🇷",
    th: "🇹🇭",
    vi: "🇻🇳",
    es: "🇪🇸",
    pt: "🇵🇹"
  };
  function _flagFor(code) {
    if (LANG_FLAGS[code]) return LANG_FLAGS[code];
    var primary = String(code || "").toLowerCase().split(/[-_]/)[0];
    var byPrimary = {
      zh: "🇨🇳",
      en: "🇺🇸",
      fr: "🇫🇷",
      ja: "🇯🇵",
      ko: "🇰🇷",
      th: "🇹🇭",
      vi: "🇻🇳",
      es: "🇪🇸",
      pt: "🇵🇹"
    };
    return byPrimary[primary] || "🌐";
  }
  function renderLangDropdown() {
    if (!dom.langDropdown) return;
    var _KNOWN_LANGS = [ "zh-CN", "en", "zh-TW", "ja", "ko", "es", "fr", "de", "pt" ];
    var supported = window.veyraI18n && window.veyraI18n.supported || _KNOWN_LANGS;
    var labels = window.veyraI18n && window.veyraI18n.labels || {
      "zh-CN": "简体中文",
      en: "English"
    };
    var cur = window.veyraI18n && window.veyraI18n.lang || window.__INIT_LANG__ || "zh-CN";
    try {
      var persisted = localStorage.getItem("veyra_lang") || localStorage.getItem("vp_lang") || "";
      if (persisted && _KNOWN_LANGS.indexOf(persisted) >= 0) cur = persisted;
    } catch (_) {}
    dom.langDropdown.innerHTML = supported.map(function(code) {
      var active = cur === code;
      var flag = _flagFor(code);
      var style = active ? "background:rgba(99,102,241,.12);color:var(--accent)" : "";
      return '<button class="v-dropdown-item" data-lang="' + esc(code) + '" style="' + style + '">' + '<span style="display:inline-block;width:24px;font-size:1.05rem">' + flag + "</span>" + "<span>" + (active ? "✓ " : "") + esc(labels[code] || code) + "</span>" + "</button>";
    }).join("");
    if (dom.langFlagBtn) {
      var flagEl = dom.langFlagBtn.querySelector(".flag-emoji");
      if (flagEl) flagEl.textContent = _flagFor(cur);
    }
  }
  function toggleLang() {
    state.langOpen = !state.langOpen;
    if (dom.langDropdown) dom.langDropdown.classList.toggle("show", state.langOpen);
    if (state.langOpen) renderLangDropdown();
  }
  function applyLang(code) {
    if (window.veyraI18n && typeof window.veyraI18n.setLang === "function") {
      window.veyraI18n.setLang(code);
    }
    state.langOpen = false;
    if (dom.langDropdown) dom.langDropdown.classList.remove("show");
    renderLangDropdown();
  }
  function toggleUser() {
    state.userOpen = !state.userOpen;
    if (dom.userDropdownMenu) dom.userDropdownMenu.classList.toggle("show", state.userOpen);
  }
  function readRequiresPmFlag() {
    return !!(document.body && document.body.getAttribute("data-requires-pm") === "1");
  }
  function bootPmPolling() {
    if (state._pmBooted) return;
    state._pmBooted = true;
    function boot() {
      fetchMarquee();
      fetchNotifications();
      state._marqueeTimer = setInterval(fetchMarquee, 1e4);
      state._notifTimer = setInterval(fetchNotifications, 15e3);
    }
    if (window.veyraDeferNonCritical) window.veyraDeferNonCritical(boot); else boot();
  }
  function initHeaderSearch() {
    var input = document.getElementById("header-search-input");
    var dropdown = document.querySelector(".hsr-dropdown");
    if (!input || !dropdown) return;
    var _timer = null;
    var _query = "";
    var _results = [];
    var _loading = false;
    var _open = false;
    var _inflightCtl = null;
    var _lastRequested = "";
    var _cache = Object.create(null);
    var _CACHE_TTL_MS = 6e4;
    var _prefetch = {
      items: null,
      ts: 0,
      inflight: false
    };
    var _PREFETCH_TTL_MS = 6e5;
    function _loadPrefetch() {
      if (_prefetch.inflight) return;
      if (_prefetch.items && Date.now() - _prefetch.ts < _PREFETCH_TTL_MS) return;
      _prefetch.inflight = true;
      fetch("/api/pm/events/prefetch", {
        credentials: "include",
        cache: "no-store"
      }).then(function(r) {
        return r.ok ? r.json() : {};
      }).then(function(d) {
        if (Array.isArray(d.items)) {
          _prefetch.items = d.items;
          _prefetch.ts = Date.now();
          if (_query.trim().length >= 2) runSearch();
        }
      }).catch(function() {}).finally(function() {
        _prefetch.inflight = false;
      });
    }
    function _prefetchFilter(q) {
      if (!_prefetch.items || !_prefetch.items.length) return null;
      var lq = q.toLowerCase();
      var out = _prefetch.items.filter(function(it) {
        var title = String(it && it.title || "").toLowerCase();
        var slug = String(it && it.slug || "").toLowerCase();
        return title.indexOf(lq) >= 0 || slug.indexOf(lq) >= 0;
      }).slice(0, 8);
      return out;
    }
    function fmtVol(n) {
      return fmtNum(n);
    }
    function pin() {
      if (!dropdown.classList.contains("show")) return;
      var r = input.getBoundingClientRect();
      var w = Math.max(320, r.width);
      var left = Math.min(window.innerWidth - w - 12, Math.max(8, r.left));
      dropdown.style.top = r.bottom + 6 + "px";
      dropdown.style.left = left + "px";
      dropdown.style.width = w + "px";
    }
    window.addEventListener("resize", pin);
    window.addEventListener("scroll", pin, true);
    function render() {
      if (!_open || _query.length < 2) {
        dropdown.classList.remove("show");
        return;
      }
      dropdown.classList.add("show");
      pin();
      var html = "";
      if (_loading && !_results.length) {
        html = '<div class="hsr-status">' + esc(t("header.search.loading", "Searching…")) + "</div>";
      } else if (_results.length === 0) {
        html = '<div class="hsr-status">' + esc(t("header.search.empty", "No matching events")) + "</div>";
      } else {
        html = _results.map(function(it) {
          var img = esc(it.image || it.icon || "/static/VagusPloy.png");
          var href = esc("/event/" + (it.slug || ""));
          var v24 = it.volume24hr || it.volume_24hr || 0;
          var vol = t("header.search.vol24h", "24h Vol") + ": $" + fmtVol(v24);
          var liq = t("header.search.liquidity", "Liquidity") + ": $" + fmtVol(it.liquidity);
          return '<a class="hsr-item" href="' + href + '">' + '<img class="hsr-img" src="' + img + '" loading="lazy" onerror="this.style.visibility=\'hidden\'">' + '<div class="hsr-meta">' + '<div class="hsr-title">' + esc(it.title || "") + "</div>" + '<div class="hsr-stats"><span>' + esc(vol) + "</span><span>" + esc(liq) + "</span></div>" + "</div></a>";
        }).join("");
      }
      var qTrim = (_query || "").trim();
      if (qTrim.length >= 2) {
        var allHref = esc("/search?q=" + encodeURIComponent(qTrim));
        html += '<a class="hsr-item hsr-view-all" href="' + allHref + '" style="justify-content:center;font-weight:600;color:var(--accent,#6366f1);text-decoration:none;border-top:1px solid var(--border);padding:10px;">' + esc(t("search.dropdown.view_all", "View all results →")) + "</a>";
      }
      dropdown.innerHTML = html;
    }
    function _cacheGet(key) {
      var hit = _cache[key];
      if (!hit) return null;
      if (Date.now() - hit.ts > _CACHE_TTL_MS) {
        delete _cache[key];
        return null;
      }
      return hit.items || [];
    }
    function _cachePut(key, items) {
      _cache[key] = {
        ts: Date.now(),
        items: items || []
      };
    }
    function _localPrefixHit(lang, q) {
      var lowerQ = String(q || "").toLowerCase();
      var keys = Object.keys(_cache);
      for (var i = 0; i < keys.length; i++) {
        var k = keys[i];
        if (k.indexOf(lang + ":") !== 0) continue;
        var cachedQ = k.slice(lang.length + 1);
        if (!lowerQ.startsWith(cachedQ) || cachedQ.length < 2) continue;
        var items = _cacheGet(k) || [];
        if (!items.length) continue;
        return items.filter(function(it) {
          var title = String(it && it.title || "").toLowerCase();
          var slug = String(it && it.slug || "").toLowerCase();
          return title.indexOf(lowerQ) >= 0 || slug.indexOf(lowerQ) >= 0;
        }).slice(0, 8);
      }
      return null;
    }
    function runSearch() {
      var q = _query.trim();
      if (q.length < 2) {
        if (_inflightCtl) {
          try {
            _inflightCtl.abort();
          } catch (_) {}
          _inflightCtl = null;
        }
        _results = [];
        _loading = false;
        render();
        return;
      }
      var lang = window.veyraI18n && window.veyraI18n.lang || "en";
      var cacheKey = lang + ":" + q.toLowerCase();
      var quick = _cacheGet(cacheKey);
      if (quick) {
        _results = quick;
        _loading = false;
        render();
        return;
      }
      var prefixHit = _localPrefixHit(lang, q);
      if (prefixHit && prefixHit.length) {
        _results = prefixHit;
        _loading = true;
        render();
      }
      var pHit = _prefetchFilter(q);
      if (pHit && pHit.length) {
        _results = pHit;
        _loading = false;
        render();
        return;
      }
      if (!_prefetch.items && !_prefetch.inflight) {
        _loadPrefetch();
        if (!prefixHit || !prefixHit.length) {
          _loading = true;
          render();
        }
      }
      if (_lastRequested === cacheKey && _inflightCtl) return;
      if (_inflightCtl) {
        try {
          _inflightCtl.abort();
        } catch (_) {}
      }
      _inflightCtl = window.AbortController ? new AbortController : null;
      _lastRequested = cacheKey;
      _loading = true;
      render();
      var url = "/api/pm/search?q=" + encodeURIComponent(q) + "&limit=8&page=1&status=active";
      fetch(url, {
        credentials: "include",
        cache: "no-store",
        signal: _inflightCtl ? _inflightCtl.signal : undefined
      }).then(function(r) {
        return r.ok ? r.json() : {};
      }).then(function(d) {
        if (_query.trim() !== q) return;
        var list = Array.isArray(d.events) ? d.events : Array.isArray(d.items) ? d.items : [];
        _results = list;
        _cachePut(cacheKey, list);
        _loading = false;
        render();
      }).catch(function(err) {
        if (err && err.name === "AbortError") return;
        _loading = false;
        render();
      }).finally(function() {
        if (_lastRequested === cacheKey) {
          _inflightCtl = null;
          _lastRequested = "";
        }
      });
    }
    input.addEventListener("input", function() {
      _query = input.value;
      _open = true;
      clearTimeout(_timer);
      if (_query.length < 2) {
        render();
        return;
      }
      var qlen = _query.trim().length;
      var waitMs = qlen <= 3 ? 80 : qlen <= 8 ? 120 : 180;
      _timer = setTimeout(runSearch, waitMs);
    });
    input.addEventListener("focus", function() {
      _loadPrefetch();
      if (_results.length || _query.length >= 2) {
        _open = true;
        render();
      }
    });
    document.addEventListener("click", function(e) {
      if (!input.contains(e.target) && !dropdown.contains(e.target)) {
        _open = false;
        dropdown.classList.remove("show");
      }
    });
    document.addEventListener("keydown", function(e) {
      if (e.key === "Escape") {
        _open = false;
        dropdown.classList.remove("show");
      }
    });
    input.addEventListener("keydown", function(e) {
      if (e.key !== "Enter") return;
      var q = (input.value || "").trim();
      if (q.length < 1) return;
      e.preventDefault();
      _open = false;
      try {
        dropdown.classList.remove("show");
      } catch (_) {}
      window.location.href = "/search?q=" + encodeURIComponent(q);
    });
  }
  function onOutsideClick(insideEl, cb) {
    document.addEventListener("click", function(e) {
      if (insideEl && !insideEl.contains(e.target)) cb();
    });
  }
  function init() {
    try {
      var saved = localStorage.getItem("vp_sidebar_open");
      if (saved === "0") state.sidebarOpen = false;
    } catch (_) {}
    try {
      state.lastReadTs = parseInt(localStorage.getItem("vp_notif_last_read") || "0") || 0;
    } catch (_) {}
    try {
      var cached = localStorage.getItem("vp_marquee_cache");
      if (cached) {
        var arr = JSON.parse(cached);
        if (Array.isArray(arr) && arr.length) state.marqueeItems = arr;
      }
    } catch (_) {}
    dom.sidebar = $("sidebar");
    dom.sidebarOverlay = $("vp-sidebar-overlay") || document.querySelector(".sidebar-overlay");
    dom.mainHeader = document.querySelector(".main-header");
    dom.mainContent = document.querySelector(".main-content");
    dom.marquee = $("vp-header-marquee");
    dom.marqueeEmpty = $("vp-marquee-empty");
    dom.notifWrap = $("vp-notif-wrap");
    dom.notifBellBtn = $("vp-notif-bell-btn");
    dom.notifDot = $("vp-notif-dot");
    dom.notifDropdown = $("vp-notif-dropdown");
    dom.notifUnreadWrap = $("vp-notif-unread-count");
    dom.notifClearBtn = $("vp-notif-clear-btn");
    dom.notifEmpty = $("vp-notif-empty");
    dom.notifList = $("vp-notif-list");
    dom.langSwitcher = $("vp-lang-switcher");
    dom.langFlagBtn = $("vp-lang-flag-btn");
    dom.langDropdown = $("vp-lang-dropdown");
    dom.userDropdown = $("vp-user-dropdown");
    dom.userAvatarBtn = $("vp-user-avatar-btn");
    dom.userDropdownMenu = $("vp-user-dropdown-menu");
    dom.sidebarToggleBtn = document.querySelector(".sidebar-toggle-btn");
    dom.mobilMenuBtn = document.querySelector(".mobile-menu-btn");
    renderLangDropdown();
    setTimeout(function() {
      renderLangDropdown();
    }, 150);
    setTimeout(function() {
      renderLangDropdown();
    }, 600);
    applySidebarState();
    applyMobileState();
    if (dom.sidebarToggleBtn) {
      dom.sidebarToggleBtn.addEventListener("click", function() {
        state.sidebarOpen = !state.sidebarOpen;
        applySidebarState();
      });
    }
    if (dom.mobilMenuBtn) {
      dom.mobilMenuBtn.addEventListener("click", function() {
        state.sidebarMobile = !state.sidebarMobile;
        applyMobileState();
      });
    }
    if (dom.sidebarOverlay) {
      dom.sidebarOverlay.addEventListener("click", function() {
        state.sidebarMobile = false;
        applyMobileState();
      });
    }
    if (dom.notifBellBtn) {
      dom.notifBellBtn.addEventListener("click", function(e) {
        e.stopPropagation();
        toggleNotif();
      });
    }
    if (dom.notifClearBtn) {
      dom.notifClearBtn.addEventListener("click", function(e) {
        e.stopPropagation();
        markAllRead();
      });
    }
    if (dom.notifWrap) {
      onOutsideClick(dom.notifWrap, function() {
        if (state.notifOpen) {
          state.notifOpen = false;
          if (dom.notifDropdown) dom.notifDropdown.classList.remove("show");
        }
      });
    }
    if (dom.langFlagBtn) {
      dom.langFlagBtn.addEventListener("click", function(e) {
        e.stopPropagation();
        toggleLang();
      });
    }
    if (dom.langSwitcher) {
      dom.langSwitcher.addEventListener("click", function(e) {
        var btn = e.target.closest("[data-lang]");
        if (btn) {
          applyLang(btn.getAttribute("data-lang"));
        }
      });
      onOutsideClick(dom.langSwitcher, function() {
        if (state.langOpen) {
          state.langOpen = false;
          if (dom.langDropdown) dom.langDropdown.classList.remove("show");
        }
      });
    }
    if (dom.userAvatarBtn) {
      dom.userAvatarBtn.addEventListener("click", function(e) {
        e.stopPropagation();
        toggleUser();
      });
    }
    if (dom.userDropdown) {
      onOutsideClick(dom.userDropdown, function() {
        if (state.userOpen) {
          state.userOpen = false;
          if (dom.userDropdownMenu) dom.userDropdownMenu.classList.remove("show");
        }
      });
    }
    if (dom.marquee) {
      dom.marquee.parentElement && dom.marquee.parentElement.addEventListener("click", function() {
        if (state.marqueeItems.length) window.location.href = "/discover";
      });
    }
    state.requiresPm = readRequiresPmFlag();
    if (state.requiresPm) {
      state.marqueeLoaded = false;
      state.notifLoaded = false;
      bootPmPolling();
    } else {
      state.marqueeLoaded = true;
      state.notifLoaded = true;
      renderMarquee();
      renderNotifDropdown();
      renderNotifDot();
      state._pmProbeTimer = setInterval(function() {
        if (state._pmBooted) {
          clearInterval(state._pmProbeTimer);
          state._pmProbeTimer = null;
          return;
        }
        if (readRequiresPmFlag()) {
          state.requiresPm = true;
          state.marqueeLoaded = false;
          state.notifLoaded = false;
          bootPmPolling();
          clearInterval(state._pmProbeTimer);
          state._pmProbeTimer = null;
        }
      }, 400);
    }
    if (!state._i18nBound) {
      state._i18nBound = true;
      document.addEventListener("i18n:changed", function() {
        renderLangDropdown();
        renderMarquee();
        renderNotifDropdown();
        renderNotifDot();
        if (window.veyraI18nApply) window.veyraI18nApply(document);
      });
    }
    initHeaderSearch();
    if (state.marqueeItems.length) renderMarquee();
    if (window.veyraI18nApply) window.veyraI18nApply(document);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
  window.__VP_HEADER_STORE__ = state;
})();