(function () {
  "use strict";
  function t(key, fallback) {
    if (window.veyraI18n && typeof window.veyraI18n.t === "function") {
      return window.veyraI18n.t(key, fallback) || fallback || "";
    }
    return fallback || "";
  }
  function _currentLang() {
    try {
      var l = window.veyraI18n && window.veyraI18n.lang || localStorage.getItem("veyra_lang") || "";
      l = String(l || "").trim();
      return l && l !== "en" ? l : "";
    } catch (_) {
      return "";
    }
  }
  function _eventsNeedTranslation(events) {
    var lang = _currentLang();
    if (!lang) return false;
    if (!Array.isArray(events) || events.length === 0) return false;
    for (var i = 0; i < events.length; i++) {
      var ev = events[i] || {};
      if (ev.i18n_lang === lang) continue;
      return true;
    }
    return false;
  }
  function esc(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  var CAT_ICONS = {
    sports: "⚽",
    soccer: "⚽",
    crypto: "₿",
    politics: "🗳️",
    elections: "🗳️",
    geopolitics: "🌍",
    world: "🌎",
    weather: "⛅",
    tech: "💻",
    economy: "💹",
    earnings: "📊",
    finance: "💰",
    "pop-culture": "🎭",
    esports: "🎮"
  };
  var CAT_LABELS = {
    sports: "Sports",
    crypto: "Crypto",
    politics: "Politics",
    elections: "Elections",
    geopolitics: "Geopolitics",
    world: "World",
    weather: "Weather",
    tech: "Tech",
    economy: "Economy",
    earnings: "Earnings",
    finance: "Finance",
    "pop-culture": "Pop Culture",
    esports: "Esports"
  };
  var CAT_I18N_KEYS = {
    sports: "discover.cat.sports",
    crypto: "discover.cat.crypto",
    politics: "discover.cat.politics",
    elections: "discover.cat.elections",
    geopolitics: "discover.cat.geopolitics",
    world: "discover.cat.world",
    weather: "discover.cat.weather",
    tech: "discover.cat.tech",
    economy: "discover.cat.economy",
    earnings: "discover.cat.earnings",
    finance: "discover.cat.finance",
    "pop-culture": "discover.cat.popCulture",
    esports: "discover.cat.esports"
  };
  var S = {
    tab: "all",
    p: "",
    t_tag: "",
    events: [],
    categories: [],
    tags: [],
    loading: false,
    loadingMore: false,
    noMore: false,
    nextCursor: "",
    page: 1,
    limit: 18,
    fillAttempts: 0,
    qbOpen: false,
    qbBalance: null,
    qbMinTrade: 1,
    qbMaxOrder: 500,
    qbMinBuyShares: 1,
    qbHoldings: null,
    qbHoldingsLoading: false,
    qbSide: "BUY",
    qbOrderType: "market",
    qbOutcome: "YES",
    qbAmountUsdc: 0,
    qbShares: 0,
    qbPriceCents: 50,
    qbSharesInt: 0,
    qbMaxActive: false,
    qbError: "",
    qbSubmitting: false,
    qbCtx: {
      token_id_yes: "",
      token_id_no: "",
      event_slug: "",
      market_question: "",
      yes_ask: .5,
      no_ask: .5,
      yes_bid: .5,
      no_bid: .5,
      market_id: ""
    },
    _io: null,
    _qbModalEl: null,
    _prefetchHydrated: false
  };
  function $id(id) {
    return document.getElementById(id);
  }
  function fmt(v) {
    v = Number(v || 0);
    if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return v.toFixed(0);
  }
  function cents(p) {
    var n = Number(p);
    if (!isFinite(n) || n <= 0) return "—";
    if (n >= 1) return "99.9¢";
    if (n >= .99) {
      var hi = Math.floor(n * 1e3) / 10;
      if (hi >= 100) hi = 99.9;
      return hi.toFixed(1) + "¢";
    }
    if (n <= .01) {
      var lo = Math.ceil(n * 1e3) / 10;
      if (lo < .1) lo = .1;
      return lo.toFixed(1) + "¢";
    }
    return Math.round(n * 100) + "¢";
  }
  function _bookSpread(m) {
    if (!m) return 1;
    var ba = Number(m.best_ask), bb = Number(m.best_bid);
    if (!isFinite(ba) || !isFinite(bb)) return 1;
    return ba - bb;
  }
  function yesCellPxM(m) {
    if (!m) return "—";
    var ba = Number(m.best_ask);
    var degenerate = !isFinite(ba) || ba <= 0 || ba >= 1 || _bookSpread(m) >= .25;
    if (degenerate) {
      var yp = Number(m.yes_prob);
      if (isFinite(yp)) return cents(Math.min(.99, Math.max(.01, yp)));
      return "—";
    }
    return cents(ba);
  }
  function noCellPxM(m) {
    if (!m) return "—";
    var bb = Number(m.best_bid);
    var degenerate = !isFinite(bb) || bb <= 0 || bb >= 1 || _bookSpread(m) >= .25;
    if (degenerate) {
      var yp = Number(m.yes_prob);
      if (isFinite(yp)) return cents(1 - Math.min(.99, Math.max(.01, yp)));
      return "—";
    }
    return cents(1 - bb);
  }
  function pctPrecise(p) {
    var n = Math.max(0, Math.min(1, Number(p) || 0));
    var v = n * 100;
    if (v < .1) return "<0.1%";
    if (v > 99.9) return ">99.9%";
    return v.toFixed(1) + "%";
  }
  function formatEnd(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d)) return "";
    var diffH = (d - new Date) / 36e5;
    if (diffH < 0) return t("discover.time.ended", "Ended");
    if (diffH < 24) return t("discover.time.hourPrefix", "⏰ ") + Math.round(diffH) + t("discover.time.hourSuffix", "h left");
    if (diffH < 168) return t("discover.time.dayPrefix", "") + Math.round(diffH / 24) + t("discover.time.daySuffix", "d left");
    return d.toISOString().slice(0, 10);
  }
  function endUrgent(iso) {
    if (!iso) return false;
    var d = new Date(iso);
    if (isNaN(d)) return false;
    return (d - new Date) / 36e5 < 24;
  }
  function isBinary(ev) {
    var ms = ev.markets || [];
    return ms.length === 1 && (ms[0].outcomes || []).length === 2;
  }
  function yesProb(ev) {
    var m = (ev.markets || [])[0];
    if (!m) return .5;
    var v = Number(m.yes_prob != null ? m.yes_prob : m.best_ask) || 0;
    return v > 0 ? Math.min(.999, Math.max(.001, v)) : .5;
  }
  function yesProbOf(m) {
    var v = Number(m.yes_prob != null ? m.yes_prob : m.best_ask) || 0;
    return v > 0 ? Math.min(.999, Math.max(.001, v)) : .5;
  }
  function yesAsk(ev) {
    var m = (ev.markets || [])[0];
    return m ? Number(m.best_ask || 0) : 0;
  }
  function noAsk(ev) {
    var m = (ev.markets || [])[0];
    if (!m) return 0;
    return Math.max(.01, Math.min(.99, 1 - Number(m.best_bid || 0)));
  }
  function tradeableSubMarkets(ev) {
    var ms = ev.markets || [];
    var isMulti = ms.length > 1;
    var now = Date.now();
    var filtered = ms.filter(function (m) {
      if (m.closed) return false;
      if (m.end_date) {
        var ed = new Date(m.end_date).getTime();
        if (!isNaN(ed) && ed < now) return false;
      }
      if (!m.yes_token_id || !m.no_token_id) return false;
      if (isMulti) {
        var yp = yesProbOf(m);
        if (yp <= .005 || yp >= .995) return false;
      }
      return true;
    });
    function _score(m) {
      var yp = yesProbOf(m);
      var dist = Math.abs(yp - .5);
      var midBand = Math.max(.05, 1 - Math.pow(dist / .5, 2));
      var vol = Math.max(0, Number(m.volume_24hr || m.volume24hr || 0));
      var liq = Math.max(0, Number(m.liquidity || 0));
      var volBoost = 1 + .35 * Math.log10(1 + vol);
      var liqBoost = 1 + .2 * Math.log10(1 + liq);
      return midBand * volBoost * liqBoost;
    }
    return filtered.sort(function (a, b) {
      var sa = _score(a), sb = _score(b);
      if (sb !== sa) return sb - sa;
      return yesProbOf(b) - yesProbOf(a);
    });
  }
  function extraOptionCount(ev) {
    return Math.max(0, tradeableSubMarkets(ev).length - 2);
  }
  function buildHref(opts) {
    var u = new URL("/discover", window.location.origin);
    if (opts.p) u.searchParams.set("p", opts.p);
    if (opts.t) u.searchParams.set("t", opts.t);
    if (S.tab && S.tab !== "all") u.searchParams.set("tab", S.tab);
    return u.pathname + u.search;
  }
  function showError(msg) {
    var el = $id("vp-disc-error");
    if (!el) return;
    if (!msg) {
      el.style.display = "none";
      el.innerHTML = "";
      return;
    }
    el.style.display = "";
    el.innerHTML = '<div style="color:#fca5a5">' + esc(t("discover.loadFailed", "Load failed")) + "：" + esc(msg) + "</div>";
  }
  function showSkeleton(show) {
    var el = $id("discover-static-skeleton");
    if (el) el.style.display = show ? "" : "none";
  }
  function setTab(tab) {
    S.tab = tab;
    document.querySelectorAll("[data-vp-disc-tab]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-vp-disc-tab") === tab);
    });
    resetAndFetch();
  }
  var _LS_CATS_KEY = "v2_cats_cache_v1";
  var _LS_CATS_TTL_MS = 24 * 3600 * 1e3;
  function _hydrateCatsFromSsrAndLs() {
    if (Array.isArray(S.categories) && S.categories.length > 0) return;
    try {
      var node = document.getElementById("vp-discover-cats-ssr");
      if (node) {
        var data = JSON.parse(node.textContent || "null");
        if (data && Array.isArray(data.categories) && data.categories.length > 0) {
          S.categories = data.categories.filter(function (c) {
            return String(c && c.slug || "").toLowerCase() !== "mentions";
          });
          return;
        }
      }
    } catch (_) { }
    try {
      var raw = localStorage.getItem(_LS_CATS_KEY);
      if (!raw) return;
      var parsed = JSON.parse(raw);
      if (parsed && parsed.ts && Date.now() - Number(parsed.ts) < _LS_CATS_TTL_MS && Array.isArray(parsed.data) && parsed.data.length > 0) {
        S.categories = parsed.data;
      }
    } catch (_) { }
  }
  function _persistCatsCache() {
    if (!Array.isArray(S.categories) || S.categories.length === 0) return;
    try {
      localStorage.setItem(_LS_CATS_KEY, JSON.stringify({
        ts: Date.now(),
        data: S.categories
      }));
    } catch (_) { }
  }
  _hydrateCatsFromSsrAndLs();
  function renderCategories() {
    var el = $id("vp-disc-cats");
    if (!el) return;
    var html = '<a class="disc-cat-chip' + (!S.p ? " active" : "") + '" href="' + esc(buildHref({
      p: "",
      t: ""
    })) + '">' + esc(t("discover.cat.allCategories", "All Categories")) + "</a>";
    S.categories.forEach(function (cat) {
      var icon = CAT_ICONS[cat.slug] || "🎯";
      var i18nKey = CAT_I18N_KEYS[cat.slug] || "";
      var label = i18nKey ? t(i18nKey, CAT_LABELS[cat.slug] || cat.label || cat.slug) : CAT_LABELS[cat.slug] || cat.label || cat.slug;
      html += '<a class="disc-cat-chip' + (S.p === cat.slug ? " active" : "") + '" href="' + esc(buildHref({
        p: cat.slug,
        t: ""
      })) + '">' + '<span class="disc-cat-icon">' + icon + "</span>" + "<span>" + esc(label) + "</span></a>";
    });
    el.innerHTML = html;
  }
  S._tagsCache = S._tagsCache || {};
  try {
    var _ssrTagsNode = document.getElementById("vp-discover-tags-ssr");
    if (_ssrTagsNode) {
      var _ssrTags = JSON.parse(_ssrTagsNode.textContent || "null");
      if (_ssrTags && Array.isArray(_ssrTags.tags) && _ssrTags.category_slug) {
        S._tagsCache[String(_ssrTags.category_slug).toLowerCase()] = _ssrTags.tags;
      }
    }
  } catch (_) { }
  var _LS_TAGS_KEY = "v2_tags_cache_v1";
  var _LS_TAGS_TTL_MS = 6 * 3600 * 1e3;
  try {
    var _raw = localStorage.getItem(_LS_TAGS_KEY);
    if (_raw) {
      var _parsed = JSON.parse(_raw);
      if (_parsed && typeof _parsed === "object" && _parsed.ts && Date.now() - _parsed.ts < _LS_TAGS_TTL_MS) {
        var _data = _parsed.data || {};
        Object.keys(_data).forEach(function (k) {
          if (!Array.isArray(S._tagsCache[k])) S._tagsCache[k] = _data[k];
        });
      }
    }
  } catch (_) { }
  function _persistTagsCache() {
    try {
      localStorage.setItem(_LS_TAGS_KEY, JSON.stringify({
        ts: Date.now(),
        data: S._tagsCache
      }));
    } catch (_) { }
  }
  function renderTags() {
    var wrap = $id("vp-disc-tags-wrap");
    var bar = $id("vp-disc-tags");
    if (!wrap || !bar) return;
    if (!S.p) {
      wrap.style.display = "none";
      bar.innerHTML = "";
      return;
    }
    var tags = S._tagsCache[S.p];
    if (!Array.isArray(tags)) {
      wrap.style.display = "none";
      return;
    }
    if (!tags.length) {
      wrap.style.display = "block";
      bar.innerHTML = '<span class="disc-tag-chip" style="opacity:.55;cursor:default">' + esc(t("discover.tag.emptyHint", "No sub-tags for this category")) + "</span>";
      return;
    }
    var html = '<a class="disc-tag-chip' + (!S.t_tag ? " active" : "") + '" href="' + esc(buildHref({
      p: S.p,
      t: ""
    })) + '">' + esc(t("discover.tag.all", "All")) + "</a>";
    tags.forEach(function (tg) {
      var slug = String(tg.slug || "").toLowerCase();
      if (!slug) return;
      var label = tg.label || tg.name || slug;
      html += '<a class="disc-tag-chip' + (S.t_tag === slug ? " active" : "") + '" href="' + esc(buildHref({
        p: S.p,
        t: slug
      })) + '">' + esc(label) + "</a>";
    });
    bar.innerHTML = html;
    wrap.style.display = "block";
  }
  function loadTagsForCategory(slug) {
    if (!slug) {
      renderTags();
      return Promise.resolve();
    }
    if (Array.isArray(S._tagsCache[slug])) {
      renderTags();
      return Promise.resolve();
    }
    return fetch("/api/pm/tags?p=" + encodeURIComponent(slug), {
      credentials: "include"
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      if (d && Array.isArray(d.tags)) {
        S._tagsCache[slug] = d.tags;
      } else {
        S._tagsCache[slug] = [];
      }
      renderTags();
      _persistTagsCache();
    }).catch(function () {
      S._tagsCache[slug] = [];
      renderTags();
    });
  }
  function renderEventCard(ev) {
    var binary = isBinary(ev);
    var img = esc(ev.image || ev.icon || "/static/VagusPloy.png");
    var title = esc(ev.title || "");
    var endTxt = esc(formatEnd(ev.end_date));
    var urgent = endUrgent(ev.end_date) ? " urgent" : "";
    var sub = "";
    if (binary) {
      var extras = extraOptionCount(ev);
      sub = extras > 0 ? '<span class="event-card-cat-badge">+' + extras + " " + esc(t("discover.card.extraOptions", "more options")) + "</span>" : '<span class="event-card-cat-badge blank">&nbsp;</span>';
    } else {
      var cnt = extraOptionCount(ev);
      sub = cnt > 0 ? '<span class="event-card-cat-badge">+' + cnt + " " + esc(t("discover.card.extraOptions", "more options")) + "</span>" : '<span class="event-card-cat-badge blank">&nbsp;</span>';
    }
    var midHtml = "";
    if (binary) {
      var yp = yesProb(ev);
      var m0 = (ev.markets || [])[0];
      var yPct = Math.round(yp * 100);
      midHtml = '<div class="event-card-body"><div class="binary-mid">' + '<div class="binary-prob">' + '<div class="bp-pct">' + yPct + "%</div>" + '<div class="bp-label">' + esc(t("discover.card.chance", "chance")) + "</div>" + "</div>" + '<div class="binary-actions">' + '<button class="quick-bet-btn qb-yes" data-vp-qb-ev="' + esc(ev.slug) + '" data-vp-qb-out="YES">' + '<span class="qb-label">' + esc(t("discover.buy.yes", "Buy Yes")) + "</span>" + '<span class="qb-price">' + esc(yesCellPxM(m0)) + "</span></button>" + '<button class="quick-bet-btn qb-no" data-vp-qb-ev="' + esc(ev.slug) + '" data-vp-qb-out="NO">' + '<span class="qb-label">' + esc(t("discover.buy.no", "Buy No")) + "</span>" + '<span class="qb-price">' + esc(noCellPxM(m0)) + "</span></button>" + "</div></div>" + '<div class="prob-bar-wrap">' + '<div class="prob-bar-yes" style="width:' + yPct + '%"></div>' + '<div class="prob-bar-no" style="width:' + (100 - yPct) + '%"></div></div>' + "</div>";
    } else {
      var subs = tradeableSubMarkets(ev).slice(0, 3);
      midHtml = '<div class="event-card-body"><div class="multi-list">';
      if (subs.length === 0) {
        midHtml += '<div style="font-size:.74rem;color:var(--text-muted);padding:6px 8px;background:var(--bg-primary);border-radius:8px">' + esc(t("discover.subMarketsLocked", "All sub-markets locked or ended")) + "</div>";
      } else {
        subs.forEach(function (m) {
          var ypm = yesProbOf(m);
          midHtml += '<div class="multi-item">' + '<div class="multi-item-name">' + esc(m.question) + "</div>" + '<div class="multi-item-prob">' + Math.round(ypm * 100) + "%</div>" + '<div class="multi-mini-btns">' + '<button class="multi-mini-btn yes" data-vp-qb-ev="' + esc(ev.slug) + '" data-vp-qb-out="YES" data-vp-qb-mktid="' + esc(m.market_id || "") + '" title="' + esc(t("discover.buy.yes", "Buy Yes")) + '">' + esc(yesCellPxM(m)) + "</button>" + '<button class="multi-mini-btn no" data-vp-qb-ev="' + esc(ev.slug) + '" data-vp-qb-out="NO" data-vp-qb-mktid="' + esc(m.market_id || "") + '" title="' + esc(t("discover.buy.no", "Buy No")) + '">' + esc(noCellPxM(m)) + "</button>" + "</div></div>";
        });
      }
      midHtml += "</div></div>";
    }
    var liquidity = Number(ev.liquidity || 0);
    void liquidity;
    return '<div class="event-card" data-vp-card-slug="' + esc(ev.slug) + '">' + '<div class="event-card-head">' + '<img class="event-card-img" src="' + img + '" width="38" height="38" decoding="async" loading="lazy" onerror="this.style.visibility=\'hidden\'">' + '<div class="event-card-meta">' + '<div class="event-card-title">' + title + "</div>" + "</div></div>" + midHtml + '<div class="event-card-foot">' + '<span class="event-card-tags">' + sub + '<span class="event-card-end' + urgent + '">' + endTxt + "</span></span>" + '<span class="event-card-vol">$' + esc(fmt(ev.volume_24hr)) + " " + esc(t("discover.card.vol", "Vol.")) + "</span>" + "</div></div>";
  }
  function renderGrid(reset) {
    var grid = $id("vp-disc-grid");
    if (!grid) return;
    if (reset) {
      grid.innerHTML = "";
    }
    var displayed = displayedEvents();
    var start = reset ? 0 : grid.querySelectorAll(".event-card").length;
    var fragment = document.createDocumentFragment();
    for (var i = start; i < displayed.length; i++) {
      var wrapper = document.createElement("div");
      wrapper.innerHTML = renderEventCard(displayed[i]);
      var card = wrapper.firstChild;
      (function (slug) {
        card.addEventListener("click", function (e) {
          if (e.target.closest("button")) return;
          window.location.href = "/event/" + slug;
        });
      })(displayed[i].slug);
      card.querySelectorAll("[data-vp-qb-ev]").forEach(function (btn) {
        btn.addEventListener("click", function (e) {
          e.stopPropagation();
          var evSlug = btn.getAttribute("data-vp-qb-ev");
          var outcome = btn.getAttribute("data-vp-qb-out") || "YES";
          var mktId = btn.getAttribute("data-vp-qb-mktid") || "";
          var evObj = S.events.find(function (x) {
            return x.slug === evSlug;
          });
          if (!evObj) return;
          var mkt = mktId ? (evObj.markets || []).find(function (m) {
            return m.market_id === mktId;
          }) : null;
          openQuickBet(evObj, outcome, mkt);
        });
      });
      fragment.appendChild(card);
    }
    grid.appendChild(fragment);
  }
  function displayedEvents() {
    var arr = S.events || [];
    if (S.noMore || arr.length <= 3) return arr;
    var trimmed = Math.floor(arr.length / 3) * 3;
    return arr.slice(0, trimmed);
  }
  function readPrefetch(url, maxAgeMs) {
    maxAgeMs = maxAgeMs || 18e4;
    try {
      var raw = sessionStorage.getItem("v2_prefetch_payload:" + url);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      var ts = Number(parsed && parsed.ts) || 0;
      if (Date.now() - ts > maxAgeMs) return null;
      return parsed && parsed.data ? parsed.data : null;
    } catch (_) {
      return null;
    }
  }
  function awaitPrefetch(url) {
    try {
      var p = window.__vpPrefetch && window.__vpPrefetch[url];
      if (p && typeof p.then === "function") return p;
    } catch (_) { }
    return null;
  }
  function filterEvents(items) {
    var now = Date.now();
    return items.filter(function (ev) {
      if (ev.closed) return false;
      if (ev.end_date) {
        var ed = new Date(ev.end_date).getTime();
        if (!isNaN(ed) && ed < now) return false;
      }
      if (isBinary(ev)) {
        var m = (ev.markets || [])[0];
        if (!m || !m.yes_token_id || !m.no_token_id) return false;
        return true;
      }
      return tradeableSubMarkets(ev).length > 0;
    });
  }
  async function fetchEvents(reset) {
    if (S.loading || S.loadingMore) return;
    if (!reset && S.noMore) return;
    var targetPage = reset ? 1 : S.page + 1;
    if (reset) {
      S.loading = true;
      S.nextCursor = "";
      S.fillAttempts = 0;
      if (!(S._prefetchHydrated && S.events.length > 0)) {
        showSkeleton(true);
      }
    } else S.loadingMore = true;
    showError("");
    try {
      var params = new URLSearchParams({
        limit: S.limit,
        tab: S.tab
      });
      if (!reset && S.nextCursor) params.set("cursor", S.nextCursor); else params.set("offset", String((targetPage - 1) * S.limit));
      if (S.p) params.set("p", S.p);
      if (S.t_tag) params.set("t", S.t_tag);
      try {
        var lang = localStorage.getItem("veyra_lang") || "";
        if (lang && lang !== "en") params.set("lang", lang);
      } catch (_) { }
      var apiUrl = "/api/pm/events?" + params.toString();
      async function _fetchOnce() {
        var r = await fetch(apiUrl, {
          credentials: "include",
          headers: {
            Accept: "application/json"
          }
        });
        var ct = (r.headers.get("content-type") || "").toLowerCase();
        if (!r.ok) {
          var bodyText = "";
          try {
            bodyText = await r.text();
          } catch (_) { }
          var snippet = bodyText.replace(/\s+/g, " ").slice(0, 120);
          var msg = "HTTP " + r.status;
          if (ct.indexOf("application/json") === 0 && bodyText) {
            try {
              var j = JSON.parse(bodyText);
              msg = j.error || msg;
            } catch (_) { }
          } else if (snippet) {
            msg = msg + (snippet ? " (" + snippet + ")" : "");
          }
          var err = new Error(msg);
          err._transient = r.status >= 500 && r.status < 600 || r.status === 0 || r.status === 408 || r.status === 429;
          throw err;
        }
        if (ct.indexOf("application/json") !== 0) {
          var err2 = new Error("Server returned non-JSON response (" + (ct || "unknown") + ")");
          err2._transient = true;
          throw err2;
        }
        return r.json();
      }
      var data;
      try {
        data = await _fetchOnce();
      } catch (e1) {
        if (e1 && e1._transient) {
          await new Promise(function (r) {
            setTimeout(r, 600);
          });
          data = await _fetchOnce();
        } else {
          throw e1;
        }
      }
      var items = Array.isArray(data.events) ? data.events : [];
      var filtered = filterEvents(items);
      if (reset) S.events = filtered; else S.events = S.events.concat(filtered);
      S.page = targetPage;
      S.nextCursor = data.next_cursor || "";
      if (data.has_more === false) S.noMore = true; else if (!data.next_cursor && items.length < S.limit) S.noMore = true;
      if (reset) showSkeleton(false);
      renderGrid(reset);
      updateSentinel();
    } catch (e) {
      showError(String(e.message || e));
      if (reset) {
        showSkeleton(false);
        S.events = [];
      }
    } finally {
      S.loading = false;
      S.loadingMore = false;
      if (reset) {
        showSkeleton(false);
        S._prefetchHydrated = false;
      }
    }
    if (!S.noMore && S.events.length > 0 && S.events.length % 3 !== 0) {
      S.fillAttempts = (S.fillAttempts || 0) + 1;
      if (S.fillAttempts <= 3) {
        await new Promise(function (r) {
          setTimeout(r, 60);
        });
        await fetchEvents(false);
      }
    } else {
      S.fillAttempts = 0;
    }
  }
  function updateSentinel() {
    var el = $id("vp-disc-sentinel");
    if (!el) return;
    if (S.noMore && S.events.length > 0) {
      el.innerHTML = '<span class="vp-end-pill">' + t("discover.allDisplayed", "— All displayed —").replace(/^—\s*|\s*—$/g, "") + "</span>";
    } else if (S.events.length > 0) {
      var label = t("discover.loadingMore", "Loading more market opportunities…");
      el.innerHTML = '<span class="vp-loading-pill" role="status" aria-live="polite">' + '<span class="vp-loading-dot"></span>' + "<span>" + label + "</span>" + "</span>";
    } else {
      el.textContent = "";
    }
  }
  function setupObserver() {
    if (S._io) try {
      S._io.disconnect();
    } catch (_) { }
    var el = $id("vp-disc-sentinel");
    if (!el) return;
    S._io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting && !S.loading && !S.loadingMore && !S.noMore && S.events.length > 0) {
          fetchEvents(false);
        }
      });
    }, {
      rootMargin: "1200px 0px"
    });
    S._io.observe(el);
  }
  function resetAndFetch() {
    S.events = [];
    S.page = 1;
    S.noMore = false;
    var u = new URL(window.location.href);
    if (S.p) u.searchParams.set("p", S.p); else u.searchParams.delete("p");
    if (S.t_tag) u.searchParams.set("t", S.t_tag); else u.searchParams.delete("t");
    if (S.tab && S.tab !== "all") u.searchParams.set("tab", S.tab); else u.searchParams.delete("tab");
    window.history.replaceState({}, "", u.pathname + u.search);
    fetchEvents(true);
  }
  function qbEffectivePrice() {
    return S.qbOutcome === "YES" ? S.qbCtx.yes_ask : S.qbCtx.no_ask;
  }
  function qbLimitCost() {
    return S.qbSharesInt * S.qbPriceCents / 100;
  }
  function qbSellProceeds() {
    var px = S.qbOutcome === "YES" ? S.qbCtx.yes_bid : S.qbCtx.no_bid;
    return (Number(S.qbSharesInt) || 0) * (Number(px) || 0);
  }
  function activeTokenId() {
    return S.qbOutcome === "YES" ? S.qbCtx.token_id_yes : S.qbCtx.token_id_no;
  }
  function qbCanSubmit() {
    if (S.qbSubmitting) return false;
    if (S.qbSide === "BUY") {
      if (S.qbOrderType === "market") return (Number(S.qbAmountUsdc) || 0) >= S.qbMinBuyShares;
      return (Number(S.qbSharesInt) || 0) >= 1;
    }
    return (Number(S.qbSharesInt) || 0) > 0;
  }
  function buildQbModalHtml() {
    var side = S.qbSide;
    var otype = S.qbOrderType;
    var out = S.qbOutcome;
    var ctx = S.qbCtx;
    var balTxt = S.qbBalance == null ? "…" : S.qbBalance.toFixed(2);
    var holdTxt = S.qbHoldings == null ? "…" : Number(S.qbHoldings).toFixed(2);
    var typeOptions = '<option value="market"' + (otype === "market" ? " selected" : "") + ">" + esc(t("discover.qb.orderType.market", "Market")) + "</option>" + '<option value="limit"' + (otype === "limit" ? " selected" : "") + ">" + esc(t("discover.qb.orderType.limit", "Limit")) + "</option>";
    var bodyHtml = "";
    if (otype === "market") {
      if (side === "BUY") {
        var totalCost = (S.qbShares * qbEffectivePrice()).toFixed(2);
        var estProfit = Math.max(0, S.qbShares * (1 - qbEffectivePrice())).toFixed(2);
        var profitPct = S.qbShares > 0 ? (Math.max(0, S.qbShares * (1 - qbEffectivePrice())) / Math.max(.01, S.qbShares * qbEffectivePrice()) * 100).toFixed(2) : "0.00";
        bodyHtml = '<div class="qb-section">' + '<div class="qb-section-label"><span>' + esc(t("discover.qb.sharesLabel", "Shares")) + "</span>" + '<span class="qb-bal">' + esc(t("discover.qb.balanceLabel", "Balance:")) + ' $<span id="vp-qb-bal-display">' + balTxt + "</span></span></div>" + '<div class="stepper-input">' + '<button id="vp-qb-amt-minus">−</button>' + '<input type="number" id="vp-qb-amt-input" min="' + S.qbMinBuyShares + '" step="1" value="' + (S.qbAmountUsdc || 0) + '">' + '<span class="stepper-suffix"></span>' + '<button id="vp-qb-amt-plus">+</button></div>' + '<div class="qb-quick-amts" style="margin-top:8px">' + '<button class="qb-quick-amt' + (S.qbAmountUsdc === 10 ? " active" : "") + '" data-vp-qb-amt="10">10</button>' + '<button class="qb-quick-amt' + (S.qbAmountUsdc === 50 ? " active" : "") + '" data-vp-qb-amt="50">50</button>' + '<button class="qb-quick-amt' + (S.qbAmountUsdc === 100 ? " active" : "") + '" data-vp-qb-amt="100">100</button>' + '<button class="qb-quick-amt' + (S.qbMaxActive ? " active" : "") + '" id="vp-qb-max-btn"' + (S.qbBalance == null || S.qbBalance < S.qbMinTrade ? " disabled" : "") + ">" + esc(t("discover.qb.maxBtn", "MAX")) + "</button></div></div>" + '<div class="qb-summary">' + '<div class="qb-sum-row"><span>' + esc(t("discover.qb.summary.totalCost", "Total Cost")) + '</span><b class="cost">$' + totalCost + "</b></div>" + '<div class="qb-sum-row"><span>' + esc(t("discover.qb.summary.estProfit", "Est. Profit")) + '</span><b class="profit">$' + estProfit + " (" + profitPct + "%)</b></div></div>";
      } else {
        var proceeds = qbSellProceeds().toFixed(2);
        var sellPx = cents(out === "YES" ? ctx.yes_bid : ctx.no_bid);
        bodyHtml = '<div class="qb-section">' + '<div class="qb-section-label"><span>' + esc(t("discover.qb.sellSharesLabel", "Sell Shares")) + "</span>" + '<span class="qb-bal">' + esc(t("discover.qb.holdingsLabel", "Holdings:")) + ' <span id="vp-qb-hold-display">' + holdTxt + "</span> " + esc(out) + "</span></div>" + '<div class="stepper-input">' + '<button id="vp-qb-shares-minus">−</button>' + '<input type="number" id="vp-qb-shares-input" min="0" step="0.01" value="' + (S.qbSharesInt || 0) + '">' + '<span class="stepper-suffix" style="padding-right:16px"></span>' + '<button id="vp-qb-shares-plus">+</button></div>' + '<div class="qb-share-adjusts" style="margin-top:8px">' + '<button class="qb-share-adj" id="vp-qb-sell25"' + (!S.qbHoldings ? " disabled" : "") + ">25%</button>" + '<button class="qb-share-adj" id="vp-qb-sell50"' + (!S.qbHoldings ? " disabled" : "") + ">50%</button>" + '<button class="qb-share-adj" id="vp-qb-sellmax"' + (!S.qbHoldings ? " disabled" : "") + ">MAX</button></div>" + (S.qbHoldingsLoading ? '<div style="font-size:.74rem;color:var(--text-muted);margin-top:6px">' + esc(t("discover.qb.holdingsLoading", "Loading holdings...")) + "</div>" : "") + "</div>" + '<div class="qb-summary">' + '<div class="qb-sum-row"><span>' + esc(t("discover.qb.summary.estSellPrice", "Est. Sell Price")) + "</span><b>" + esc(sellPx) + "</b></div>" + '<div class="qb-sum-row"><span>' + esc(t("discover.qb.summary.estRecover", "Est. Proceeds")) + '</span><b class="cost">$' + proceeds + "</b></div></div>";
      }
    } else {
      var limitCostV = qbLimitCost().toFixed(2);
      bodyHtml = '<div class="qb-section">' + '<div class="qb-section-label"><span>' + esc(t("discover.qb.limitPriceLabel", "Limit Price")) + "</span></div>" + '<div class="stepper-input">' + '<button id="vp-qb-px-minus">−</button>' + '<input type="number" id="vp-qb-px-input" min="1" max="99" step="1" value="' + S.qbPriceCents + '">' + '<span class="stepper-suffix">¢</span>' + '<button id="vp-qb-px-plus">+</button></div></div>' + '<div class="qb-section">' + '<div class="qb-section-label"><span>' + esc(t("discover.qb.sharesLabel", "Shares")) + "</span>" + (side === "SELL" ? '<span class="qb-bal">' + esc(t("discover.qb.holdingsLabel", "Holdings:")) + " " + holdTxt + "</span>" : "") + "</div>" + '<div class="stepper-input">' + '<button id="vp-qb-shares-minus">−</button>' + '<input type="number" id="vp-qb-shares-input" min="1" step="1" value="' + (S.qbSharesInt || 0) + '">' + '<span class="stepper-suffix" style="padding-right:16px"></span>' + '<button id="vp-qb-shares-plus">+</button></div>' + '<div class="qb-share-adjusts">' + (side === "BUY" ? '<button class="qb-share-adj" id="vp-qb-lm100">-100</button>' + '<button class="qb-share-adj" id="vp-qb-lm10">-10</button>' + '<button class="qb-share-adj" id="vp-qb-lp10">+10</button>' + '<button class="qb-share-adj" id="vp-qb-lp100">+100</button>' + '<button class="qb-share-adj" id="vp-qb-lmax">' + esc(t("discover.qb.maxBtn", "MAX")) + "</button>" : '<button class="qb-share-adj" id="vp-qb-sell25"' + (!S.qbHoldings ? " disabled" : "") + ">25%</button>" + '<button class="qb-share-adj" id="vp-qb-sell50"' + (!S.qbHoldings ? " disabled" : "") + ">50%</button>" + '<button class="qb-share-adj" id="vp-qb-sellmax"' + (!S.qbHoldings ? " disabled" : "") + ">MAX</button>") + "</div></div>" + '<div class="qb-summary">' + (side === "BUY" ? '<div class="qb-sum-row"><span>' + esc(t("discover.qb.summary.totalCost", "Total Cost")) + '</span><b class="cost">$' + limitCostV + "</b></div>" + '<div class="qb-sum-row"><span>' + esc(t("discover.qb.summary.estProfit", "Est. Profit")) + '</span><b class="profit">$' + Math.max(0, S.qbSharesInt - qbLimitCost()).toFixed(2) + "</b></div>" : '<div class="qb-sum-row"><span>' + esc(t("discover.qb.summary.estRecover", "Est. Proceeds")) + '</span><b class="cost">$' + limitCostV + "</b></div>") + "</div>";
    }
    var submitLabel = otype === "market" ? (side === "BUY" ? t("discover.qb.submit.buy", "Buy") : t("discover.qb.submit.sell", "Sell")) + " " + out : side === "BUY" ? t("discover.qb.submit.createBuyOrder", "Create Buy Order") : t("discover.qb.submit.createSellOrder", "Create Sell Order");
    return '<div class="qb-modal-overlay" id="vp-qb-overlay">' + '<div class="qb-modal" style="position:relative">' + '<div class="qb-modal-head">' + '<div class="qb-mode-tabs">' + '<button class="qb-mode-tab' + (side === "BUY" ? " active" : "") + '" id="vp-qb-tab-buy">' + esc(t("discover.qb.tab.buy", "Buy")) + "</button>" + '<button class="qb-mode-tab' + (side === "SELL" ? " active" : "") + '" id="vp-qb-tab-sell">' + esc(t("discover.qb.tab.sell", "Sell")) + "</button>" + "</div>" + '<div class="qb-otype">' + '<select class="qb-otype-select" id="vp-qb-type-sel">' + typeOptions + "</select>" + "</div>" + "</div>" + '<div class="qb-modal-body">' + '<div class="qb-mkt-title">' + esc(ctx.market_question) + "</div>" + '<div class="qb-outcome-row">' + '<button class="qb-outcome-btn' + (out === "YES" ? " yes-active" : "") + '" id="vp-qb-out-yes">' + esc(t("discover.qb.outcome.yes", "Yes")) + ' <span class="qb-out-px">' + esc(cents(ctx.yes_ask)) + "</span></button>" + '<button class="qb-outcome-btn' + (out === "NO" ? " no-active" : "") + '" id="vp-qb-out-no">' + esc(t("discover.qb.outcome.no", "No")) + ' <span class="qb-out-px">' + esc(cents(1 - ctx.yes_ask)) + "</span></button>" + "</div>" + bodyHtml + (S.qbError ? '<div class="qb-error">' + esc(S.qbError) + "</div>" : "") + '<button class="qb-submit" id="vp-qb-submit"' + (!qbCanSubmit() ? " disabled" : "") + ">" + (S.qbSubmitting ? esc(t("discover.qb.submit.submitting", "Submitting...")) : esc(submitLabel)) + "</button>" + "</div>" + "</div></div>";
  }
  function renderQbModal() {
    var existing = $id("vp-qb-overlay");
    if (existing) existing.parentNode.removeChild(existing);
    var wrapper = document.createElement("div");
    wrapper.innerHTML = buildQbModalHtml();
    var modal = wrapper.firstChild;
    document.body.appendChild(modal);
    S._qbModalEl = modal;
    bindQbEvents(modal);
  }
  function bindQbEvents(modal) {
    modal.addEventListener("click", function (e) {
      if (e.target === modal) closeQuickBet();
    });
    var tabBuy = modal.querySelector("#vp-qb-tab-buy");
    var tabSell = modal.querySelector("#vp-qb-tab-sell");
    if (tabBuy) tabBuy.addEventListener("click", function () {
      setSide("BUY");
    });
    if (tabSell) tabSell.addEventListener("click", function () {
      setSide("SELL");
    });
    var typeSel = modal.querySelector("#vp-qb-type-sel");
    if (typeSel) typeSel.addEventListener("change", function () {
      S.qbOrderType = typeSel.value;
      recompute();
      renderQbModal();
    });
    var outYes = modal.querySelector("#vp-qb-out-yes");
    var outNo = modal.querySelector("#vp-qb-out-no");
    if (outYes) outYes.addEventListener("click", function () {
      setOutcome("YES");
    });
    if (outNo) outNo.addEventListener("click", function () {
      setOutcome("NO");
    });
    var amtMinus = modal.querySelector("#vp-qb-amt-minus");
    var amtPlus = modal.querySelector("#vp-qb-amt-plus");
    var amtInput = modal.querySelector("#vp-qb-amt-input");
    if (amtMinus) amtMinus.addEventListener("click", function () {
      adjustAmount(-1);
    });
    if (amtPlus) amtPlus.addEventListener("click", function () {
      adjustAmount(1);
    });
    if (amtInput) amtInput.addEventListener("input", function () {
      S.qbAmountUsdc = Math.max(S.qbMinBuyShares, Math.floor(Number(amtInput.value) || 0));
      S.qbMaxActive = false;
      recompute();
      renderQbModal();
    });
    modal.querySelectorAll("[data-vp-qb-amt]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setAmount(Number(btn.getAttribute("data-vp-qb-amt")));
      });
    });
    var maxBtn = modal.querySelector("#vp-qb-max-btn");
    if (maxBtn) maxBtn.addEventListener("click", setMaxAmount);
    var sharesMinus = modal.querySelector("#vp-qb-shares-minus");
    var sharesPlus = modal.querySelector("#vp-qb-shares-plus");
    var sharesInput = modal.querySelector("#vp-qb-shares-input");
    if (sharesMinus) sharesMinus.addEventListener("click", function () {
      adjustShares(-1);
    });
    if (sharesPlus) sharesPlus.addEventListener("click", function () {
      adjustShares(1);
    });
    if (sharesInput) sharesInput.addEventListener("input", function () {
      var v = parseFloat(sharesInput.value);
      if (!isFinite(v) || v < 0) v = 0;
      S.qbSharesInt = Number(v.toFixed(2));
      recompute();
      renderQbModal();
    });
    var s25 = modal.querySelector("#vp-qb-sell25");
    var s50 = modal.querySelector("#vp-qb-sell50");
    var smax = modal.querySelector("#vp-qb-sellmax");
    if (s25) s25.addEventListener("click", function () {
      setSellPct(.25);
    });
    if (s50) s50.addEventListener("click", function () {
      setSellPct(.5);
    });
    if (smax) smax.addEventListener("click", setSellMax);
    var pxMinus = modal.querySelector("#vp-qb-px-minus");
    var pxPlus = modal.querySelector("#vp-qb-px-plus");
    var pxInput = modal.querySelector("#vp-qb-px-input");
    if (pxMinus) pxMinus.addEventListener("click", function () {
      adjustPriceCents(-1);
    });
    if (pxPlus) pxPlus.addEventListener("click", function () {
      adjustPriceCents(1);
    });
    if (pxInput) pxInput.addEventListener("input", function () {
      var v = parseInt(pxInput.value) || 0;
      if (v < 1) v = 1;
      if (v > 99) v = 99;
      S.qbPriceCents = v;
      recompute();
      renderQbModal();
    });
    var lm100 = modal.querySelector("#vp-qb-lm100");
    if (lm100) lm100.addEventListener("click", function () {
      adjustShares(-100);
    });
    var lm10 = modal.querySelector("#vp-qb-lm10");
    if (lm10) lm10.addEventListener("click", function () {
      adjustShares(-10);
    });
    var lp10 = modal.querySelector("#vp-qb-lp10");
    if (lp10) lp10.addEventListener("click", function () {
      adjustShares(10);
    });
    var lp100 = modal.querySelector("#vp-qb-lp100");
    if (lp100) lp100.addEventListener("click", function () {
      adjustShares(100);
    });
    var lmax = modal.querySelector("#vp-qb-lmax");
    if (lmax) lmax.addEventListener("click", setMaxShares);
    var sub = modal.querySelector("#vp-qb-submit");
    if (sub) sub.addEventListener("click", submitQuickBet);
  }
  function openQuickBet(ev, outcome, mkt) {
    var m = mkt || (ev.markets || [])[0];
    if (!m) {
      vpToast(t("discover.toast.noMarket", "No tradable sub-markets for this event"));
      return;
    }
    if (!m.yes_token_id || !m.no_token_id) {
      vpToast(t("discover.toast.noTokenId", "token_id not available"));
      return;
    }
    var yesAskV = Number(m.best_ask || m.yes_prob || .5);
    var yesBidV = Number(m.best_bid || yesAskV - .02);
    S.qbCtx = {
      token_id_yes: m.yes_token_id,
      token_id_no: m.no_token_id,
      event_slug: ev.slug,
      market_question: m.question,
      yes_ask: Math.max(.01, Math.min(.99, yesAskV)),
      no_ask: Math.max(.01, Math.min(.99, 1 - yesBidV)),
      yes_bid: Math.max(.01, Math.min(.99, yesBidV)),
      no_bid: Math.max(.01, Math.min(.99, 1 - yesAskV)),
      market_id: m.market_id
    };
    S.qbSide = "BUY";
    S.qbOrderType = "market";
    S.qbOutcome = outcome || "YES";
    S.qbAmountUsdc = 0;
    S.qbMaxActive = false;
    S.qbShares = 0;
    S.qbMinBuyShares = 1;
    S.qbHoldings = null;
    S.qbHoldingsLoading = false;
    S.qbPriceCents = Math.max(1, Math.min(99, Math.round((S.qbOutcome === "YES" ? S.qbCtx.yes_ask : S.qbCtx.no_ask) * 100) || 50));
    S.qbSharesInt = 0;
    S.qbError = "";
    S.qbOpen = true;
    renderQbModal();
    refreshMinBuyShares();
  }
  function closeQuickBet() {
    S.qbOpen = false;
    S.qbError = "";
    S.qbSubmitting = false;
    var el = $id("vp-qb-overlay");
    if (el) el.parentNode.removeChild(el);
    S._qbModalEl = null;
  }
  function setSide(s) {
    S.qbSide = s;
    if (s === "SELL") {
      S.qbAmountUsdc = 0;
      S.qbSharesInt = 0;
      refreshHoldings();
    } else {
      S.qbHoldings = null;
      refreshMinBuyShares();
    }
    recompute();
    renderQbModal();
  }
  function setOutcome(o) {
    S.qbOutcome = o;
    S.qbPriceCents = Math.max(1, Math.min(99, Math.round((o === "YES" ? S.qbCtx.yes_ask : S.qbCtx.no_ask) * 100) || 50));
    if (S.qbSide === "SELL") refreshHoldings();
    if (S.qbSide === "BUY") refreshMinBuyShares();
    recompute();
    renderQbModal();
  }
  async function refreshHoldings() {
    S.qbHoldingsLoading = true;
    S.qbHoldings = null;
    if (S._qbModalEl) {
      var hd = S._qbModalEl.querySelector("#vp-qb-hold-display");
      if (hd) hd.textContent = "…";
    }
    try {
      var tok = activeTokenId();
      if (!tok) return;
      var r = await fetch("/api/pm/user/holdings?token_id=" + encodeURIComponent(tok), {
        credentials: "include"
      });
      if (r.ok) {
        var d = await r.json();
        S.qbHoldings = Number(d.shares || 0);
        if (S.qbHoldings > 0 && S.qbSharesInt === 0) S.qbSharesInt = Math.floor(S.qbHoldings);
      } else {
        S.qbHoldings = 0;
      }
    } catch (_) {
      S.qbHoldings = 0;
    }
    S.qbHoldingsLoading = false;
    renderQbModal();
  }
  async function refreshMinBuyShares() {
    try {
      var tok = activeTokenId();
      if (!tok) return;
      var r = await fetch("/api/pm/orderbook?token_id=" + encodeURIComponent(tok), {
        credentials: "include"
      });
      if (!r.ok) return;
      var d = await r.json();
      var minOrder = Math.max(1, Math.ceil(Number(d.min_order_size || 1)));
      S.qbMinBuyShares = minOrder;
      if ((S.qbAmountUsdc || 0) < minOrder) {
        S.qbAmountUsdc = minOrder;
        recompute();
        renderQbModal();
      }
    } catch (_) { }
  }
  function recompute() {
    if (S.qbOrderType === "market") {
      S.qbShares = Math.max(S.qbMinBuyShares, Math.floor(Number(S.qbAmountUsdc) || 0));
    }
  }
  function adjustAmount(delta) {
    var v = (Number(S.qbAmountUsdc) || 0) + delta;
    if (v < S.qbMinBuyShares) v = S.qbMinBuyShares;
    S.qbAmountUsdc = v;
    S.qbMaxActive = false;
    recompute();
    renderQbModal();
  }
  function setAmount(amt) {
    S.qbAmountUsdc = Math.max(S.qbMinBuyShares, Math.floor(Number(amt) || 0));
    S.qbMaxActive = false;
    recompute();
    renderQbModal();
  }
  function setMaxAmount() {
    var px = Math.max(.01, Math.min(.99, qbEffectivePrice() || .5));
    var capUsdc = Math.min(S.qbBalance || 0, S.qbMaxOrder);
    S.qbAmountUsdc = Math.max(S.qbMinBuyShares, Math.floor(capUsdc / px));
    S.qbMaxActive = true;
    recompute();
    renderQbModal();
  }
  function adjustPriceCents(delta) {
    S.qbPriceCents = Math.max(1, Math.min(99, (Number(S.qbPriceCents) || 0) + delta));
    recompute();
    renderQbModal();
  }
  function adjustShares(delta) {
    S.qbSharesInt = Math.max(0, Number(((Number(S.qbSharesInt) || 0) + delta).toFixed(2)));
    recompute();
    renderQbModal();
  }
  function setMaxShares() {
    if (S.qbBalance == null || S.qbPriceCents <= 0) return;
    S.qbSharesInt = Math.max(1, Math.floor(S.qbBalance / (S.qbPriceCents / 100)));
    recompute();
    renderQbModal();
  }
  function setSellPct(pct) {
    if (!S.qbHoldings || S.qbHoldings <= 0) return;
    S.qbSharesInt = Math.max(.01, Number((Number(S.qbHoldings) * pct).toFixed(2)));
    recompute();
    renderQbModal();
  }
  function setSellMax() {
    if (!S.qbHoldings || S.qbHoldings <= 0) return;
    S.qbSharesInt = Number(Number(S.qbHoldings).toFixed(2));
    recompute();
    renderQbModal();
  }
  async function submitQuickBet() {
    if (!qbCanSubmit() || S.qbSubmitting) return;
    S.qbSubmitting = true;
    S.qbError = "";
    renderQbModal();
    try {
      var body;
      if (S.qbOrderType === "market") {
        if (S.qbSide === "BUY") {
          body = {
            token_id: activeTokenId(),
            side: "BUY",
            price: qbEffectivePrice(),
            size: Number(S.qbShares) || 0,
            order_type: "FOK",
            event_slug: S.qbCtx.event_slug,
            market_question: S.qbCtx.market_question
          };
        } else {
          body = {
            token_id: activeTokenId(),
            side: "SELL",
            price: .01,
            size: Number(S.qbSharesInt) || 0,
            order_type: "FOK",
            event_slug: S.qbCtx.event_slug,
            market_question: S.qbCtx.market_question
          };
        }
      } else {
        body = {
          token_id: activeTokenId(),
          side: S.qbSide,
          price: S.qbPriceCents / 100,
          size: S.qbSharesInt,
          order_type: "GTC",
          event_slug: S.qbCtx.event_slug,
          market_question: S.qbCtx.market_question
        };
      }
      try {
        var _session = (window.veyraTrade && typeof window.veyraTrade.getSession === "function") ? window.veyraTrade.getSession() : null;
        if (_session && (await _session.isDepositWalletUser())) {
          body.condition_id = body.condition_id || S.qbCtx.market_id;
          var _sdkRes = await _session.submitOrder(body);
          (function _showOrderSuccessPopup(orderId) {
            var title = t("discover.toast.orderPlacedTitle", "Order placed");
            var bodyText = t("discover.toast.orderSubmitted", "Order submitted:") + " " + (orderId || "?");
            try {
              if (window.vp && typeof window.vp.success === "function") {
                window.vp.success({ title: title, body: bodyText });
                return;
              }
              if (window.vp && typeof window.vp.modal === "function") {
                window.vp.modal({ title: title, body: bodyText, kind: "success" });
                return;
              }
            } catch (_) {  }
            vpToast(bodyText);
          })(_sdkRes && _sdkRes.order_id);
          closeQuickBet();
          setTimeout(function () {
            window.location.href = "/v2/trades";
          }, 800);
          return;
        }
      } catch (_sdkErr) {
        S.qbError = String(_sdkErr && _sdkErr.message || _sdkErr);
        S.qbSubmitting = false;
        renderQbModal();
        return;
      }
      var r = await fetch("/api/pm/trade/submit", {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(body)
      });
      var ct = (r.headers.get("content-type") || "").toLowerCase();
      var d = {};
      if (ct.includes("application/json")) {
        try {
          d = await r.json();
        } catch (_) {
          d = {};
        }
      } else {
        var txt = await r.text().catch(function () {
          return "";
        });
        d = {
          error: t("discover.toast.upstreamError", "Upstream error HTTP") + " " + r.status + (txt ? " — " + txt.slice(0, 80) : "")
        };
      }
      var authorizeApiKeyOnce = async function (authMeta) {
        var authObj = authMeta && authMeta.authorization || {};
        var submitUrl = authObj && authObj.submit_url || authMeta && authMeta.next && authMeta.next.authorize_submit || "/api/pm/trade/authorize-api-key/submit";
        var authToken = String(authObj && authObj.auth_token || "");
        var typedData = authObj && authObj.typed_data || null;
        var apiKeyAddress = String(authObj && authObj.api_key_address || authMeta && authMeta.api_key_address || "").toLowerCase();
        var expectedSigner = String(authObj && authObj.expected_signer || authMeta && authMeta.expected_signer || "").toLowerCase();
        if (!authToken || !typedData || !apiKeyAddress) {
          return {
            ok: false,
            error: t("discover.toast.tradeAuthRequired", "Trade authorization required")
          };
        }
        for (var authWaitI = 0; authWaitI < 40; authWaitI++) {
          if (typeof window.veyraPrivySignTypedData === "function") break;
          await new Promise(function (resolve) {
            setTimeout(resolve, 100);
          });
        }
        if (typeof window.veyraPrivySignTypedData !== "function") {
          return {
            ok: false,
            error: t("pm.trade.privyUnavailable", "Privy embedded wallet is not available in this session. Please re-login and retry.")
          };
        }
        var authSig = await window.veyraPrivySignTypedData(typedData, {
          expectedSigner: expectedSigner
        });
        var authRes = await fetch(submitUrl, {
          method: "POST",
          credentials: "include",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            auth_token: authToken,
            api_key_address: apiKeyAddress,
            signature: authSig
          })
        });
        var authData = await authRes.json().catch(function () {
          return {};
        });
        if (!authRes.ok) {
          return {
            ok: false,
            error: authData && (authData.error || authData.raw_error) || ("HTTP " + authRes.status)
          };
        }
        return {
          ok: true
        };
      };
      if (!r.ok && d && d.error === "usdc_needs_conversion") {
        S.qbError = t("discover.qb.error.needsConversion", "Your USDC needs to be converted to pUSD before trading. Go to the Deposit page to convert.");
        S.qbSubmitting = false;
        renderQbModal();
        setTimeout(function () {
          var errEl = document.querySelector(".qb-error");
          if (errEl) {
            errEl.innerHTML = t("discover.qb.error.needsConversion", "Your USDC needs to be converted to pUSD before trading.") +
              ' <a href="/v2/deposit" style="color:#fbbf24;text-decoration:underline;font-weight:600">' +
              t("discover.qb.error.convertLink", "Convert now →") + "</a>";
          }
        }, 50);
        return;
      }
      if (!r.ok && d && d.error === "api_key_authorization_required") {
        try {
          var authResult = await authorizeApiKeyOnce(d);
          if (!authResult.ok) {
            S.qbError = authResult.error || t("discover.toast.tradeAuthFailed", "Trade authorization failed");
            S.qbSubmitting = false;
            renderQbModal();
            return;
          }
          r = await fetch("/api/pm/trade/submit", {
            method: "POST",
            credentials: "include",
            headers: {
              "Content-Type": "application/json"
            },
            body: JSON.stringify(body)
          });
          ct = (r.headers.get("content-type") || "").toLowerCase();
          d = {};
          if (ct.includes("application/json")) {
            try {
              d = await r.json();
            } catch (_) {
              d = {};
            }
          }
        } catch (authErr) {
          S.qbError = String(authErr && authErr.message || authErr);
          S.qbSubmitting = false;
          renderQbModal();
          return;
        }
      }
      if (r.status === 409 && d && d.status === "client_signing_required") {
        try {
          var provOk = await window.veyraEnsureDepositApprovals(t);
          if (provOk && provOk.error) {
            S.qbError = provOk.error;
            S.qbSubmitting = false;
            renderQbModal();
            return;
          }
          var prepareUrl = d && d.next && d.next.prepare || "/api/pm/trade/prepare-order";
          var prepRes = await fetch(prepareUrl, {
            method: "POST",
            credentials: "include",
            headers: {
              "Content-Type": "application/json"
            },
            body: JSON.stringify(body)
          });
          var prepData = await prepRes.json().catch(function () {
            return {};
          });
          if (!prepRes.ok && prepData && prepData.error === "api_key_authorization_required") {
            var prepAuth = await authorizeApiKeyOnce(prepData);
            if (!prepAuth.ok) {
              S.qbError = prepAuth.error || t("discover.toast.tradeAuthFailed", "Trade authorization failed");
              S.qbSubmitting = false;
              renderQbModal();
              return;
            }
            prepRes = await fetch(prepareUrl, {
              method: "POST",
              credentials: "include",
              headers: {
                "Content-Type": "application/json"
              },
              body: JSON.stringify(body)
            });
            prepData = await prepRes.json().catch(function () {
              return {};
            });
          }
          if (!prepRes.ok || !prepData.typed_data || !prepData.prepare_token) {
            S.qbError = prepData && (prepData.error || prepData.raw_error) || t("discover.toast.tradePrepareFailed", "Trade prepare failed");
            S.qbSubmitting = false;
            renderQbModal();
            return;
          }
          for (var waitI = 0; waitI < 40; waitI++) {
            if (typeof window.veyraPrivySignTypedData === "function") break;
            await new Promise(function (resolve) {
              setTimeout(resolve, 100);
            });
          }
          if (typeof window.veyraPrivySignTypedData !== "function") {
            S.qbError = t("pm.trade.privyUnavailable", "Privy embedded wallet is not available in this session. Please re-login and retry.");
            S.qbSubmitting = false;
            renderQbModal();
            return;
          }
          var signature = await window.veyraPrivySignTypedData(prepData.typed_data, {
            expectedSigner: String(prepData.owner_eoa || prepData.signer_eoa || "").toLowerCase()
          });
          var signedRes = await fetch("/api/pm/trade/submit-signed", {
            method: "POST",
            credentials: "include",
            headers: {
              "Content-Type": "application/json"
            },
            body: JSON.stringify({
              prepare_token: prepData.prepare_token,
              signature: signature
            })
          });
          var signedCt = (signedRes.headers.get("content-type") || "").toLowerCase();
          var signedData = {};
          if (signedCt.includes("application/json")) {
            try {
              signedData = await signedRes.json();
            } catch (_) {
              signedData = {};
            }
          }
          if (!signedRes.ok) {
            S.qbError = signedData && (signedData.error || signedData.raw_error) || "HTTP " + signedRes.status;
            S.qbSubmitting = false;
            renderQbModal();
            return;
          }
          vpToast(t("discover.toast.orderSubmitted", "Order submitted:") + " " + (signedData.order_id || "?"));
          closeQuickBet();
          setTimeout(function () {
            window.location.href = "/v2/trades";
          }, 800);
          return;
        } catch (signErr) {
          S.qbError = String(signErr && signErr.message || signErr);
          S.qbSubmitting = false;
          renderQbModal();
          return;
        }
      }
      if (!r.ok) {
        S.qbError = d.error || "HTTP " + r.status;
        S.qbSubmitting = false;
        renderQbModal();
        return;
      }
      vpToast(t("discover.toast.orderSubmitted", "Order submitted:") + " " + (d.order_id || "?"));
      closeQuickBet();
      setTimeout(function () {
        window.location.href = "/v2/trades";
      }, 800);
    } catch (e) {
      S.qbError = String(e.message || e);
      S.qbSubmitting = false;
      renderQbModal();
    }
  }
  function vpToast(msg) {
    if (window.vp && window.vp.toast) {
      window.vp.toast(msg, "info");
      return;
    }
    console.log("[discover toast]", msg);
  }
  async function init() {
    var root = $id("vp-discover-root");
    if (!root) return;
    if (window.veyraI18n && typeof window.veyraI18n.registerPagePack === 'function') {
      try { await window.veyraI18n.registerPagePack('discover'); } catch (_) { }
    }
    var grid = $id("vp-disc-grid");
    if (grid) grid.removeAttribute("hidden");
    var skel = $id("discover-static-skeleton");
    if (skel) skel.removeAttribute("hidden");
    var u = new URL(window.location.href);
    S.p = u.searchParams.get("p") || "";
    S.t_tag = u.searchParams.get("t") || "";
    S.tab = u.searchParams.get("tab") || "all";
    document.querySelectorAll("[data-vp-disc-tab]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-vp-disc-tab") === S.tab);
      btn.addEventListener("click", function () {
        setTab(btn.getAttribute("data-vp-disc-tab"));
      });
    });
    if (S.p) {
      loadTagsForCategory(S.p);
    }
    if (S.tab === "all" || S.tab == null) {
      try {
        var ssrNode = document.getElementById("vp-discover-ssr");
        if (ssrNode && ssrNode.textContent) {
          var ssrData = JSON.parse(ssrNode.textContent);
          if (ssrData && Array.isArray(ssrData.events) && ssrData.events.length) {
            var ssrFiltered = filterEvents(ssrData.events.slice(0, S.limit));
            if (ssrFiltered.length > 0) {
              S.events = ssrFiltered;
              S.page = 1;
              S.noMore = false;
              S._prefetchHydrated = !_eventsNeedTranslation(ssrFiltered);
              try {
                if (typeof ssrData.next_cursor === "string") S.nextCursor = ssrData.next_cursor;
              } catch (_) { }
              renderGrid(true);
              showSkeleton(false);
              updateSentinel();
            }
          }
        }
      } catch (_) { }
    }
    if (!S.p && !S.t_tag && S.tab === "all") {
      var pre = readPrefetch("/api/pm/events?limit=18&tab=all");
      if (pre && Array.isArray(pre.events)) {
        var preFiltered = filterEvents(pre.events.slice(0, S.limit));
        S.events = preFiltered;
        S.page = 1;
        S.noMore = false;
        S._prefetchHydrated = !_eventsNeedTranslation(preFiltered);
        try {
          if (typeof pre.next_cursor === "string") S.nextCursor = pre.next_cursor;
        } catch (_) { }
        renderGrid(true);
        showSkeleton(false);
        updateSentinel();
      }
    }
    if (Array.isArray(S.categories) && S.categories.length > 0) {
      try {
        renderCategories();
      } catch (_) { }
    }
    var catsPromise = function () {
      var preCat = readPrefetch("/api/pm/categories");
      if (preCat) {
        S.categories = (preCat.categories || []).filter(function (c) {
          return String(c.slug).toLowerCase() !== "mentions";
        });
        renderCategories();
        _persistCatsCache();
        return Promise.resolve();
      }
      var inflight = awaitPrefetch("/api/pm/categories");
      var p = inflight ? inflight : fetch("/api/pm/categories", {
        credentials: "include"
      }).then(function (r) {
        return r.ok ? r.json() : null;
      });
      return p.then(function (cd) {
        if (cd) {
          S.categories = (cd.categories || []).filter(function (c) {
            return String(c.slug).toLowerCase() !== "mentions";
          });
          renderCategories();
          _persistCatsCache();
        }
      }).catch(function () { });
    }();
    var balPromise = function () {
      var prefBal = readPrefetch("/api/pm/balance-check");
      var p = prefBal ? Promise.resolve(prefBal) : fetch("/api/pm/balance-check", {
        credentials: "include"
      }).then(function (r) {
        return r.ok ? r.json() : null;
      });
      return p.then(function (d) {
        if (!d) return;
        S.qbBalance = d.usdc_balance == null ? null : Number(d.usdc_balance);
        S.qbMinTrade = d.thresholds && d.thresholds.min_usdc_to_trade || 1;
      }).catch(function () { });
    }();
    var eventsPromise;
    if (S._prefetchHydrated) {
      eventsPromise = Promise.resolve();
    } else {
      var inflightEvents = awaitPrefetch("/api/pm/events?limit=18&tab=all");
      if (inflightEvents && !S.p && !S.t_tag && S.tab === "all" && !_currentLang()) {
        eventsPromise = inflightEvents.then(function (d) {
          if (d && Array.isArray(d.events)) {
            S.events = filterEvents(d.events.slice(0, S.limit));
            S.page = 1;
            S.noMore = false;
            try {
              if (typeof d.next_cursor === "string") S.nextCursor = d.next_cursor;
            } catch (_) { }
            renderGrid(true);
            showSkeleton(false);
            updateSentinel();
          } else {
            return fetchEvents(true);
          }
        }).catch(function () {
          return fetchEvents(true);
        });
      } else {
        eventsPromise = fetchEvents(true);
      }
    }
    await Promise.all([catsPromise, balPromise, eventsPromise]);
    setupObserver();
    if (!S.noMore && S.events.length > 0) {
      setTimeout(function () {
        if (!S.loading && !S.loadingMore && !S.noMore) {
          fetchEvents(false);
        }
      }, 50);
    }
    document.addEventListener("i18n:changed", function () {
      renderCategories();
      renderTags();
      resetAndFetch();
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      if (window.__vpDiscoverInited) return;
      window.__vpDiscoverInited = true;
      init();
    });
  } else {
    if (!window.__vpDiscoverInited) {
      window.__vpDiscoverInited = true;
      init();
    }
  }
})();
