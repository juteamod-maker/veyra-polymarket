(function () {
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
  function fmtNum(v, dec) {
    var n = parseFloat(v) || 0;
    return n.toLocaleString(undefined, {
      maximumFractionDigits: dec ?? 0,
      minimumFractionDigits: 0
    });
  }
  function fmtPct(v) {
    return (parseFloat(v) || 0).toFixed(2) + "%";
  }
  function fmtCoin(v) {
    return "🪙 " + fmtNum(v, 0);
  }
  function fmtDollar(v) {
    return "$" + fmtNum(v, 2);
  }
  function fmtDate(s) {
    if (!s) return "-";
    if (window.$fmt) return window.$fmt(s);
    try {
      return new Date(s).toLocaleString();
    } catch (_) {
      return s;
    }
  }
  async function vJson(url) {
    try {
      var r = await fetch(url, {
        credentials: "include",
        cache: "no-store"
      });
      if (!r.ok) return null;
      return await r.json().catch(function () {
        return null;
      });
    } catch (_) {
      return null;
    }
  }
  async function vFetch(url, opts) {
    try {
      return await fetch(url, Object.assign({
        credentials: "include"
      }, opts));
    } catch (_) {
      return null;
    }
  }
  var dom = {};
  function $(id) {
    return document.getElementById(id);
  }
  var state = {
    tab: "points",
    points: {
      balance: 0,
      tier: "-",
      total_earned: 0,
      this_month: 0,
      redeemed: 0,
      next_tier_pts: 0,
      history: []
    },
    fees: {
      rate: 0,
      total: 0,
      this_month: 0,
      history: []
    },
    strategy: {},
    ctSettings: {
      enabled: true,
      scan_interval_seconds: 60,
      max_concurrent_follows: 3,
      trade_mode: "taker",
      follow_multiplier: 1,
      min_order_size_buffer_shares: 1,
      min_ev_filter: .002,
      max_price_filter: .85,
      follow_sell_enabled: true,
      trade_lookback_minutes: 30,
      exclude_bots: true,
      min_score: 0
    },
    pointsLoaded: false,
    feesLoaded: false,
    strategyLoaded: false,
    ctSettingsLoaded: false,
    accordionOpen: true,
    ctSaveMsg: "",
    ctSaveOk: true,
    ctSaveTimer: null,
    i18nBound: false
  };
  var LOADING_HTML = '<div class="cfg-loading"><div class="empty-state"><div class="spinner" style="margin:0 auto 12px"></div>' + "<p>" + esc(t("config.loading", "Loading…")) + "</p></div></div>";
  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll("[data-vp-tab-btn]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-vp-tab-btn") === tab);
    });
    ["points", "fees", "strategy"].forEach(function (p) {
      var el = $("vp-config-panel-" + p);
      if (el) el.style.display = p === tab ? "" : "none";
    });
    if (tab === "points" && !state.pointsLoaded) loadPoints();
    if (tab === "fees" && !state.feesLoaded) loadFees();
    if (tab === "strategy" && !state.strategyLoaded) loadStrategy();
  }
  async function loadPoints() {
    var panel = $("vp-config-panel-points");
    if (!panel) return;
    panel.innerHTML = LOADING_HTML;
    var d = await vJson("/api/user/points");
    if (d) {
      state.points = {
        balance: d.balance ?? 0,
        tier: d.tier || "-",
        total_earned: d.total_earned ?? 0,
        this_month: d.this_month ?? 0,
        redeemed: d.total_spent ?? 0,
        next_tier_pts: d.next_tier_pts || 0,
        history: d.recent_transactions || []
      };
    }
    state.pointsLoaded = true;
    renderPoints(panel);
  }
  function renderPoints(panel) {
    var p = state.points;
    var nextTier = Number(p.next_tier_pts || 0);
    var pct = nextTier > 0 ? Math.min(100, p.balance / nextTier * 100) : 100;
    var progressText = nextTier > 0 ? Math.max(0, nextTier - (Number(p.balance) || 0)) : 0;
    panel.innerHTML = '<section class="v-card" style="margin-bottom:14px;overflow:hidden;position:relative">' + '<div style="position:absolute;inset:auto -80px -80px auto;width:220px;height:220px;background:radial-gradient(circle, rgba(245,158,11,0.18), rgba(245,158,11,0));pointer-events:none"></div>' + '<div class="v-card-header" style="align-items:flex-start;gap:8px">' + "<div>" + '<div class="v-card-title" style="font-size:16px">' + esc(t("config.points.balance", "Current Points")) + "</div>" + '<div class="text-xs" style="color:var(--text-muted);margin-top:2px">' + esc(t("config.points.tierPrefix", "Tier: ") + (p.tier || "-")) + "</div>" + "</div>" + '<span class="badge badge-warning">' + esc(p.tier || "-") + "</span>" + "</div>" + '<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:10px;flex-wrap:wrap">' + '<div style="font-size:32px;font-weight:800;line-height:1;color:var(--warning)">' + esc(fmtCoin(p.balance)) + "</div>" + '<div style="text-align:right;color:var(--text-muted);font-size:12px">' + "<div>" + esc(t("config.points.nextTierPrefix", "Next tier: ") + (nextTier > 0 ? fmtCoin(nextTier) : "—")) + "</div>" + '<div style="margin-top:4px">' + esc(t("config.points.remaining", "Remaining") + ": " + fmtCoin(progressText)) + "</div>" + "</div>" + "</div>" + "</section>" + '<div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">' + statCard(t("config.points.totalEarned", "Total Earned"), esc(fmtCoin(p.total_earned)), "", "cfg-tone-info") + statCard(t("config.points.thisMonth", "This Month"), esc(fmtCoin(p.this_month)), "", "cfg-tone-accent") + statCard(t("config.points.redeemed", "Total Redeemed"), esc(fmtCoin(p.redeemed)), "", "cfg-tone-muted") + "</div>" + '<div class="v-card mb-6">' + '<div class="v-card-header">' + '<span class="v-card-title">' + esc(t("config.points.tierProgress", "Tier Progress")) + "</span>" + '<span class="badge badge-warning">' + esc(p.tier || "-") + "</span>" + "</div>" + '<div style="background:rgba(255,255,255,0.08);border-radius:999px;height:10px;overflow:hidden;margin-bottom:10px">' + '<div style="width:' + pct.toFixed(1) + '%;height:100%;background:linear-gradient(90deg,var(--warning),#f97316);border-radius:999px;transition:width 0.5s"></div>' + "</div>" + '<div class="flex justify-between text-xs" style="color:var(--text-muted)">' + "<span>" + esc(fmtCoin(p.balance)) + "</span>" + "<span>" + esc(nextTier > 0 ? t("config.points.nextTierPrefix", "Next tier: ") + fmtCoin(nextTier) : t("config.points.nextTierPrefix", "Next tier: ") + "—") + "</span>" + "</div>" + "</div>" + '<div class="v-card">' + '<div class="v-card-header"><span class="v-card-title">' + esc(t("config.points.history", "Points History")) + "</span></div>" + '<div class="overflow-x-auto"><table class="v-table cfg-table"><thead><tr>' + "<th>" + esc(t("config.col.time", "Time")) + "</th>" + "<th>" + esc(t("config.col.event", "Event")) + "</th>" + "<th>" + esc(t("config.col.delta", "Delta")) + "</th>" + "<th>" + esc(t("config.col.balance", "Balance")) + "</th>" + "</tr></thead><tbody>" + (p.history && p.history.length ? p.history.map(function (item) {
      var dCls = item.delta > 0 ? "text-success" : "text-danger";
      return "<tr>" + '<td class="text-xs" style="color:var(--text-muted)">' + esc(fmtDate(item.created_at)) + "</td>" + "<td>" + esc(item.description || item.event_type || "-") + "</td>" + '<td class="' + dCls + '">' + esc((item.delta > 0 ? "+" : "") + fmtCoin(item.delta)) + "</td>" + "<td>" + esc(fmtCoin(item.balance_after)) + "</td>" + "</tr>";
    }).join("") : '<tr><td colspan="4" class="text-center" style="color:var(--text-muted);padding:32px">' + esc(t("config.points.empty", "No points records")) + "</td></tr>") + "</tbody></table></div></div>";
  }
  async function loadFees() {
    var panel = $("vp-config-panel-fees");
    if (!panel) return;
    panel.innerHTML = LOADING_HTML;
    var d = await vJson("/api/user/fees");
    if (d) state.fees = Object.assign({}, state.fees, d);
    state.feesLoaded = true;
    renderFees(panel);
  }
  function renderFees(panel) {
    var f = state.fees;
    panel.innerHTML = '<section class="v-card" style="margin-bottom:14px">' + '<div class="v-card-header" style="align-items:flex-start;gap:8px">' + "<div>" + '<div class="v-card-title" style="font-size:16px">' + esc(t("config.fees.rate", "Current Rate")) + "</div>" + '<div class="text-xs" style="color:var(--text-muted);margin-top:2px">' + esc(t("config.fees.rateSub", "Charged per trade")) + "</div>" + "</div>" + '<span class="badge badge-warning">' + esc(fmtPct(f.rate)) + "</span>" + "</div>" + '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px">' + '<div class="stat-card cfg-tone-accent"><div class="label">' + esc(t("config.fees.thisMonth", "Fees This Month")) + '</div><div class="value">' + esc(fmtDollar(f.this_month)) + "</div></div>" + '<div class="stat-card cfg-tone-muted"><div class="label">' + esc(t("config.fees.total", "Total Fees")) + '</div><div class="value">' + esc(fmtDollar(f.total)) + "</div></div>" + "</div>" + "</section>" + '<div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">' + statCard(t("config.fees.rate", "Current Rate"), '<span style="color:var(--warning)">' + esc(fmtPct(f.rate)) + "</span>", esc(t("config.fees.rateSub", "Charged per trade")), "cfg-tone-info") + statCard(t("config.fees.thisMonth", "Fees This Month"), esc(fmtDollar(f.this_month)), "", "cfg-tone-accent") + statCard(t("config.fees.total", "Total Fees"), esc(fmtDollar(f.total)), "", "cfg-tone-muted") + "</div>" + '<div class="v-card"><div class="v-card-header"><span class="v-card-title">' + esc(t("config.fees.history", "Fees History")) + "</span></div>" + '<div class="overflow-x-auto"><table class="v-table cfg-table"><thead><tr>' + "<th>" + esc(t("config.col.date", "Date")) + "</th>" + "<th>" + esc(t("config.col.type", "Type")) + "</th>" + "<th>" + esc(t("config.col.amount", "Amount")) + "</th>" + "<th>" + esc(t("config.col.linkedTrade", "Linked Trade")) + "</th>" + "</tr></thead><tbody>" + (f.history && f.history.length ? f.history.map(function (item) {
      return "<tr>" + '<td class="text-xs" style="color:var(--text-muted)">' + esc(fmtDate(item.created_at)) + "</td>" + "<td>" + esc(item.type || "-") + "</td>" + '<td class="text-warning">' + esc(fmtDollar(item.amount)) + "</td>" + '<td class="text-xs" style="color:var(--text-muted)">' + esc(item.bet_id || "-") + "</td>" + "</tr>";
    }).join("") : '<tr><td colspan="4" class="text-center" style="color:var(--text-muted);padding:32px">' + esc(t("config.fees.empty", "No fee records")) + "</td></tr>") + "</tbody></table></div></div>";
  }
  async function loadStrategy() {
    var panel = $("vp-config-panel-strategy");
    if (!panel) return;
    state.strategyLoaded = true;
    await loadCtSettings();
    renderStrategy(panel);
  }
  async function loadCtSettings() {
    state.ctSettingsLoaded = false;
    try {
      var r = await fetch("/api/v2/copytrader/settings", {
        credentials: "include"
      });
      var d = await r.json().catch(function () {
        return null;
      });
      if (d && d.settings) state.ctSettings = Object.assign({}, state.ctSettings, d.settings);
    } catch (_) { }
    state.ctSettingsLoaded = true;
  }
  function renderStrategy(panel) {
    if (!panel) return;
    var ct = state.ctSettings;
    var activeBadge = ct.enabled ? '<span class="badge badge-success">' + esc(t("config.copytrader.active", "Active")) + "</span>" : '<span class="badge badge-danger">' + esc(t("config.copytrader.inactive", "Inactive")) + "</span>";
    var chevron = state.accordionOpen ? "−" : "+";
    panel.innerHTML = '<div class="grid grid-cols-1 gap-6">' + '<div class="v-card" style="border-left:3px solid var(--primary);overflow:hidden">' + '<button type="button" id="vp-ct-accordion-btn" class="w-full flex items-center justify-between" style="background:none;border:0;padding:0;cursor:pointer;width:100%">' + '<div class="v-card-header" style="width:100%;padding:0">' + '<span class="v-card-title" style="display:flex;align-items:center;gap:8px">' + '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>' + esc(t("config.copytrader.title", "Copytrader Strategy")) + "</span>" + '<div style="display:flex;gap:8px;align-items:center">' + activeBadge + '<a href="/Copytrader" class="badge badge-info" style="text-decoration:none;cursor:pointer">' + esc(t("config.copytrader.goto", "Full Manager")) + " &rarr;</a>" + '<span id="vp-ct-chevron" class="badge badge-info">' + chevron + "</span>" + "</div>" + "</div>" + "</button>" + '<div id="vp-ct-body" style="display:' + (state.accordionOpen ? "" : "none") + ';margin-top:14px">' + renderCtBody(ct) + "</div>" + "</div>" + "</div>";
    var accBtn = $("vp-ct-accordion-btn");
    if (accBtn) {
      accBtn.addEventListener("click", function () {
        state.accordionOpen = !state.accordionOpen;
        var body = $("vp-ct-body");
        var chevEl = $("vp-ct-chevron");
        if (body) body.style.display = state.accordionOpen ? "" : "none";
        if (chevEl) chevEl.textContent = state.accordionOpen ? "−" : "+";
      });
    }
    panel.querySelectorAll("[data-vp-ct-toggle]").forEach(function (el) {
      el.addEventListener("click", function () {
        ctToggle(el.getAttribute("data-vp-ct-toggle"));
      });
    });
    panel.querySelectorAll("[data-vp-set-multiplier]").forEach(function (el) {
      el.addEventListener("click", function () {
        setCtMultiplier(Number(el.getAttribute("data-vp-set-multiplier")));
      });
    });
    panel.querySelectorAll("[data-vp-ct-field]").forEach(function (inp) {
      inp.addEventListener("change", function () {
        var key = inp.getAttribute("data-vp-ct-field");
        state.ctSettings[key] = parseFloat(inp.value);
        ctSave();
      });
    });
  }
  function renderCtBody(ct) {
    function toggle(key, label, desc) {
      var active = ct[key] ? " active" : "";
      return '<div class="flex items-center justify-between p-3 rounded-lg" style="background:rgba(16,24,40,0.65);border:1px solid var(--border)">' + '<div><div class="text-sm font-medium">' + esc(label) + "</div>" + '<div class="text-xs mt-0.5" style="color:var(--text-muted)">' + esc(desc) + "</div></div>" + '<div class="v-toggle' + active + '" data-vp-ct-toggle="' + key + '"></div>' + "</div>";
    }
    function numField(key, label, min, max, step) {
      return '<div><label class="text-xs font-medium block mb-1" style="color:var(--text-muted)">' + esc(label) + "</label>" + '<input type="number" class="v-input" style="width:100%;font-size:.82rem" min="' + min + '" max="' + max + '" step="' + step + '" ' + 'value="' + esc(ct[key]) + '" data-vp-ct-field="' + key + '"></div>';
    }
    return '<div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">' + toggle("enabled", t("config.copytrader.enabled", "Daemon Enabled"), t("config.copytrader.enabledDesc", "Start/stop the copy-trade daemon")) + toggle("follow_sell_enabled", t("config.copytrader.followSell", "Follow Sell"), t("config.copytrader.followSellDesc", "Mirror sell orders from tracked wallets")) + toggle("exclude_bots", t("config.copytrader.excludeBots", "Exclude Bots"), t("config.copytrader.excludeBotsDesc", "Filter out algorithmically detected bots")) + "</div>" + '<div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">' + '<div><label class="text-xs font-medium block mb-1" style="color:var(--text-muted)">' + esc(t("config.copytrader.tradeMode", "Buy/Sell Mode")) + "</label>" + '<select class="v-input" style="width:100%;font-size:.82rem" disabled>' + '<option value="taker" selected>Taker</option><option value="maker">Maker</option>' + "</select>" + '<div class="text-xs mt-1" style="color:var(--text-muted)">' + esc(t("config.copytrader.tradeModeLockedHint", "Currently locked to Taker for stable execution")) + "</div>" + "</div>" + '<div><label class="text-xs font-medium block mb-1" style="color:var(--text-muted)">' + esc(t("config.copytrader.followMultiplier", "Follow Multiplier")) + "</label>" + '<input type="number" class="v-input" style="width:100%;font-size:.82rem" min="1" max="1000" step="1" ' + 'value="' + esc(ct.follow_multiplier) + '" data-vp-ct-field="follow_multiplier">' + '<div class="flex gap-2 mt-2">' + '<button class="v-btn v-btn-outline v-btn-sm" type="button" data-vp-set-multiplier="10">10</button>' + '<button class="v-btn v-btn-outline v-btn-sm" type="button" data-vp-set-multiplier="50">50</button>' + '<button class="v-btn v-btn-outline v-btn-sm" type="button" data-vp-set-multiplier="100">100</button>' + "</div>" + "</div>" + numField("max_concurrent_follows", t("config.copytrader.maxFollows", "Max Concurrent Follows"), 1, 20, 1) + numField("min_score", t("config.copytrader.minScore", "Min Wallet Score"), 0, 100, 1) + numField("max_price_filter", t("config.copytrader.maxPrice", "Max Entry Price"), .1, .99, .01) + numField("min_ev_filter", t("config.copytrader.minEv", "Min EV Filter"), 0, .2, .001) + numField("trade_lookback_minutes", t("config.copytrader.lookback", "Lookback (minutes)"), 1, 1440, 5) + numField("scan_interval_seconds", t("config.copytrader.scanInterval", "Scan Interval (s)"), 10, 3600, 10) + "</div>" + '<div id="vp-ct-save-msg" style="margin-top:10px;font-size:.8rem;font-weight:600;min-height:1.2em"></div>';
  }
  async function ctToggle(key) {
    state.ctSettings[key] = !state.ctSettings[key];
    var el = document.querySelector('[data-vp-ct-toggle="' + key + '"]');
    if (el) el.classList.toggle("active", state.ctSettings[key]);
    await ctSave();
  }
  async function ctSave() {
    var ct = state.ctSettings;
    ct.trade_mode = "taker";
    ct.trade_mode_options = ["taker", "maker"];
    ct.trade_mode_locked = true;
    ct.follow_multiplier = Math.max(1, Math.min(1e3, parseInt(ct.follow_multiplier || 1, 10) || 1));
    var msgEl = $("vp-ct-save-msg");
    try {
      var resp = await fetch("/api/v2/copytrader/settings", {
        method: "PUT",
        credentials: "include",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          settings: ct
        })
      });
      var d = await resp.json().catch(function () {
        return null;
      });
      if (resp.ok && d && d.ok) {
        state.ctSaveOk = true;
        if (msgEl) {
          msgEl.textContent = t("config.copytrader.saved", "Saved");
          msgEl.style.color = "var(--success)";
        }
      } else {
        state.ctSaveOk = false;
        if (msgEl) {
          msgEl.textContent = t("config.copytrader.saveFailed", "Save failed");
          msgEl.style.color = "var(--danger)";
        }
      }
    } catch (_) {
      state.ctSaveOk = false;
      if (msgEl) {
        msgEl.textContent = t("config.copytrader.saveFailed", "Save failed");
        msgEl.style.color = "var(--danger)";
      }
    }
    clearTimeout(state.ctSaveTimer);
    state.ctSaveTimer = setTimeout(function () {
      if (msgEl) msgEl.textContent = "";
    }, 3e3);
  }
  function setCtMultiplier(v) {
    state.ctSettings.follow_multiplier = Number(v) || 1;
    var inp = document.querySelector('[data-vp-ct-field="follow_multiplier"]');
    if (inp) inp.value = state.ctSettings.follow_multiplier;
    ctSave();
  }
  function statCard(label, valueHtml, sub, extraClass) {
    return '<div class="stat-card ' + (extraClass || "") + '">' + '<div class="label">' + esc(label) + "</div>" + '<div class="value">' + valueHtml + "</div>" + (sub ? '<div class="sub">' + sub + "</div>" : "") + "</div>";
  }
  function rerenderActiveTab() {
    if (state.tab === "points" && state.pointsLoaded) {
      var pp = $("vp-config-panel-points");
      if (pp) renderPoints(pp);
      return;
    }
    if (state.tab === "fees" && state.feesLoaded) {
      var pf = $("vp-config-panel-fees");
      if (pf) renderFees(pf);
      return;
    }
    if (state.tab === "strategy" && state.strategyLoaded) {
      var ps = $("vp-config-panel-strategy");
      if (ps) renderStrategy(ps);
    }
  }
  function init() {
    dom.root = $("vp-config-root");
    if (!dom.root) return;
    if (window.veyraI18n && typeof window.veyraI18n.registerPagePack === 'function') {
      window.veyraI18n.registerPagePack('config').catch(function () { });
    }
    if (!state.i18nBound) {
      state.i18nBound = true;
      document.addEventListener("i18n:changed", function () {
        if (window.veyraI18nApply) window.veyraI18nApply(document);
        rerenderActiveTab();
      });
    }
    document.querySelectorAll("[data-vp-tab-btn]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setTab(btn.getAttribute("data-vp-tab-btn"));
      });
    });
    ["points", "fees", "strategy"].forEach(function (p) {
      var el = $("vp-config-panel-" + p);
      if (el) el.style.display = p === "points" ? "" : "none";
    });
    loadPoints();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();