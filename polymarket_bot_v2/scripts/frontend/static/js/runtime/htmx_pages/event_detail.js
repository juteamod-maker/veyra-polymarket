(function () {
  "use strict";
  function t(key, fallback) {
    if (window.veyraI18n && typeof window.veyraI18n.t === "function") {
      return window.veyraI18n.t(key, fallback) || fallback || "";
    }
    return fallback || "";
  }
  function esc(v) {
    return String(v == null ? "" : v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function fmt(v) {
    var n = Number(v || 0);
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return n.toFixed(0);
  }
  function cents(v) {
    var n = Number(v);
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
  function yesCellPx(m) {
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
  function noCellPx(m) {
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
  function yesCellSellPx(m) {
    if (!m) return "—";
    var bb = Number(m.best_bid);
    var degenerate = !isFinite(bb) || bb <= 0 || bb >= 1 || _bookSpread(m) >= .25;
    if (degenerate) {
      var yp = Number(m.yes_prob);
      if (isFinite(yp)) return cents(Math.min(.99, Math.max(.01, yp)));
      return "—";
    }
    return cents(bb);
  }
  function noCellSellPx(m) {
    if (!m) return "—";
    var ba = Number(m.best_ask);
    var degenerate = !isFinite(ba) || ba <= 0 || ba >= 1 || _bookSpread(m) >= .25;
    if (degenerate) {
      var yp = Number(m.yes_prob);
      if (isFinite(yp)) return cents(1 - Math.min(.99, Math.max(.01, yp)));
      return "—";
    }
    return cents(1 - ba);
  }
  function yesProbOf(m) {
    var v = Number(m && (m.yes_prob != null ? m.yes_prob : m.best_ask)) || 0;
    return v > 0 ? Math.min(.999, Math.max(.001, v)) : .5;
  }
  function formatEnd(dateStr) {
    if (!dateStr) return "";
    try {
      var d = new Date(dateStr);
      if (isNaN(d.getTime())) return "";
      var diffH = (d - Date.now()) / 36e5;
      if (diffH < 0) return "⏹ " + t("event.end.ended", "Ended");
      if (diffH < 24) return "⏰ " + t("event.end.hoursLeft.prefix", "Only ") + Math.round(diffH) + t("event.end.hoursLeft.suffix", " h left");
      if (diffH < 24 * 7) return t("event.end.deadline", "Deadline") + " " + Math.round(diffH / 24) + " " + t("event.end.daysAfter", "days");
      return t("event.end.deadline", "Deadline") + " " + d.toISOString().slice(0, 10);
    } catch (_) {
      return "";
    }
  }
  function copyShare() {
    try {
      var url = window.location.origin + window.location.pathname;
      if (S.referralCode) {
        var u = new URL(url);
        u.searchParams.set("ref", S.referralCode);
        url = u.toString();
      }
      navigator.clipboard.writeText(url).then(function () {
        vpToast(t("event.share.copied", "Link copied!"), "success");
      }).catch(function () {
        vpToast(url, "info");
      });
    } catch (_) { }
  }
  function aiSummary(ev) {
    if (!ev) return "";
    var ms = (ev.markets || []).length;
    var vol = ev.volume_24hr;
    var s = t("event.ai.summaryShort", "This event has {n} sub-options. 24h volume ${vol}.").replace("{n}", ms).replace("{vol}", fmt(vol));
    if (ev.markets && ev.markets.length === 1) {
      var m = ev.markets[0];
      var yp = (Number(m.yes_prob != null ? m.yes_prob : m.best_ask) || 0) * 100;
      s += t("event.ai.binaryPrefix", " Binary event — current YES probability ~ ") + yp.toFixed(1) + t("event.ai.percentSuffix", "%.");
      if (yp >= 70) s += " " + t("event.ai.bullish", "Market leans bullish."); else if (yp <= 30) s += " " + t("event.ai.bearish", "Market leans bearish."); else s += " " + t("event.ai.split", "Market is split.");
    } else if (ms > 1) {
      var sorted = (ev.markets || []).slice().sort(function (a, b) {
        return yesProbOf(b) - yesProbOf(a);
      });
      var top = sorted[0];
      if (top) {
        s += t("event.ai.topPrefix", ' Top option: "') + top.question + t("event.ai.topMid", '" (~ ') + (yesProbOf(top) * 100).toFixed(1) + t("event.ai.topSuffix", "%).");
      }
    }
    s += t("event.ai.disclaimer", " Tip: trade carefully with order book depth and liquidity in mind. This system does not give investment advice.");
    return s;
  }
  function pmDisplayName(o) {
    if (!o) return "-";
    var rawName = o.name && String(o.name).trim();
    var lower = rawName ? rawName.toLowerCase() : "";
    var isFallback = !rawName || lower === "anon" || rawName.indexOf("0x") === 0;
    if (!isFallback) return rawName;
    var w = String(o.proxyWallet || o.proxy_wallet || o.user || "").trim();
    if (!w) return "-";
    if (w.length >= 12) return w.slice(0, 6) + "..." + w.slice(-4);
    return w;
  }
  function pmUserUrl(o) {
    if (!o) return "#";
    var name = o.name && String(o.name).trim() && String(o.name).trim().toLowerCase() !== "anon" ? String(o.name).trim() : "";
    var w = String(o.proxyWallet || o.proxy_wallet || o.user || "");
    var handle = name || w;
    if (!handle) return "#";
    var lang = window.veyraI18n && window.veyraI18n.lang || "en";
    var segment = lang === "zh-CN" ? "zh/" : "";
    return "https://polymarket.com/" + segment + "@" + encodeURIComponent(handle) + "?r=PanTethys999";
  }
  function sideOutcomeBadge(tk) {
    var side = String(tk && tk.side || "").toUpperCase();
    var out = String(tk && tk.outcome || "").trim();
    var isYes = out.toUpperCase() === "YES";
    var sideColor = side === "BUY" ? "#22c55e" : "#ef4444";
    var yesNoBg = isYes ? "rgba(34,197,94,.18)" : "rgba(239,68,68,.18)";
    var yesNoFg = isYes ? "#22c55e" : "#ef4444";
    var pill = '<span style="background:' + yesNoBg + ";color:" + yesNoFg + ';padding:1px 6px;border-radius:8px;font-size:.7rem;font-weight:700;text-transform:uppercase">' + esc(out || "?") + "</span>";
    return '<span style="color:' + sideColor + ';font-weight:600;margin-right:4px">' + esc(side) + "</span>" + pill;
  }
  function holderOutcomeBadge(h) {
    var out = String(h && h.outcome || S.holdersOutcome || "").toUpperCase();
    var isYes = out === "YES" || out === "Y";
    var bg = isYes ? "rgba(34,197,94,.18)" : "rgba(239,68,68,.18)";
    var fg = isYes ? "#22c55e" : "#ef4444";
    var txt = isYes ? "YES" : "NO";
    return '<span style="background:' + bg + ";color:" + fg + ';padding:2px 8px;border-radius:8px;font-size:.7rem;font-weight:700;display:inline-block">' + txt + "</span>";
  }
  function _loadChartJs() {
    if (window.Chart && window.Chart._adapters && window.Chart._adapters._date) return Promise.resolve(window.Chart);
    if (S._chartJsLoading) return S._chartJsLoading;
    var loadScript = function (src) {
      return new Promise(function (resolve, reject) {
        var existing = document.querySelector('script[data-pml-src="' + src + '"]');
        if (existing) {
          if (existing.dataset.pmlLoaded === "1") return resolve();
          existing.addEventListener("load", resolve);
          existing.addEventListener("error", function () {
            reject(new Error("load failed: " + src));
          });
          return;
        }
        var s = document.createElement("script");
        s.src = src;
        s.async = false;
        s.dataset.pmlSrc = src;
        s.onload = function () {
          s.dataset.pmlLoaded = "1";
          resolve();
        };
        s.onerror = function () {
          reject(new Error("load failed: " + src));
        };
        document.head.appendChild(s);
      });
    };
    S._chartJsLoading = loadScript("/v2/static/js/vendor/chart.umd.min.js").then(function () {
      return loadScript("/v2/static/js/vendor/chartjs-adapter-date-fns.bundle.min.js");
    }).then(function () {
      return window.Chart;
    });
    return S._chartJsLoading;
  }
  function _destroyChart() {
    if (S._chartInstance) {
      try {
        S._chartInstance.destroy();
      } catch (_) { }
      S._chartInstance = null;
    }
  }
  function _renderChartCanvas() {
    var canvas = document.getElementById("vp-chart-canvas");
    if (!canvas || !window.Chart) return;
    if (!S.chartPoints || S.chartPoints.length === 0) return;
    _destroyChart();
    try {
      if (window.Chart.getChart) {
        var prev = window.Chart.getChart(canvas);
        if (prev) prev.destroy();
      }
    } catch (_) { }
    var ctx = canvas.getContext("2d");
    var color = S.chartOutcome === "YES" ? "#22c55e" : "#ef4444";
    var fillTop = S.chartOutcome === "YES" ? "rgba(34,197,94,.18)" : "rgba(239,68,68,.18)";
    var data = S.chartPoints.map(function (p) {
      return {
        x: p.x,
        y: p.y
      };
    });
    S._chartInstance = new window.Chart(ctx, {
      type: "line",
      data: {
        datasets: [{
          label: S.chartOutcome,
          data: data,
          borderColor: color,
          backgroundColor: fillTop,
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: .18,
          fill: true,
          stepped: false
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: "index",
          intersect: false
        },
        animation: false,
        plugins: {
          legend: {
            display: false
          },
          tooltip: {
            backgroundColor: "rgba(15,17,26,.95)",
            borderColor: "rgba(139,92,246,.4)",
            borderWidth: 1,
            titleColor: "#e2e8f0",
            bodyColor: "#e2e8f0",
            padding: 8,
            displayColors: false,
            callbacks: {
              title: function (items) {
                if (!items.length) return "";
                return new Date(items[0].parsed.x).toLocaleString();
              },
              label: function (item) {
                return S.chartOutcome + ": " + item.parsed.y.toFixed(1) + "¢";
              }
            }
          }
        },
        scales: {
          x: {
            type: "time",
            time: {
              tooltipFormat: "PP HH:mm",
              displayFormats: {
                minute: "HH:mm",
                hour: "MM-dd HH:mm",
                day: "MM-dd"
              }
            },
            ticks: {
              color: "rgba(148,163,184,.7)",
              maxRotation: 0,
              autoSkip: true,
              maxTicksLimit: 6,
              font: {
                size: 10
              }
            },
            grid: {
              color: "rgba(255,255,255,.04)"
            }
          },
          y: {
            min: 0,
            max: 100,
            ticks: {
              color: "rgba(148,163,184,.7)",
              callback: function (v) {
                return v + "¢";
              },
              font: {
                size: 10
              }
            },
            grid: {
              color: "rgba(255,255,255,.04)"
            }
          }
        }
      }
    });
  }
  function _setupRealtimePrices() {
    try {
      if (S._priceUnsub) {
        S._priceUnsub();
        S._priceUnsub = null;
      }
      if (S._priceTickTimer) {
        clearInterval(S._priceTickTimer);
        S._priceTickTimer = null;
      }
      if (!window.VyRealtimePrice || !S.event || !Array.isArray(S.event.markets)) return;
      var tokens = [];
      S.event.markets.forEach(function (m) {
        if (m && m.yes_token_id) tokens.push(String(m.yes_token_id));
        if (m && m.no_token_id) tokens.push(String(m.no_token_id));
      });
      if (!tokens.length) return;
      S._priceUnsub = window.VyRealtimePrice.subscribe(tokens, function (snap) {
        if (!S.event || !Array.isArray(S.event.markets)) return;
        S.event.markets.forEach(function (m) {
          if (!m) return;
          var yp = m.yes_token_id && snap.prices[String(m.yes_token_id)];
          if (yp) {
            if (yp.best_ask != null) m.best_ask = yp.best_ask;
            if (yp.best_bid != null) m.best_bid = yp.best_bid;
            if (yp.mid != null) {
              var _ba = Number(yp.best_ask), _bb = Number(yp.best_bid);
              var _spread = isFinite(_ba) && isFinite(_bb) ? _ba - _bb : 1;
              if (_spread < .25) m.yes_prob = yp.mid;
            }
          }
        });
        if (S.selectedMarket) {
          var sid = S.selectedMarket.market_id;
          var fresh = S.event.markets.find(function (x) {
            return x && x.market_id === sid;
          });
          if (fresh) S.selectedMarket = fresh;
        }
        S.lastPriceTs = Date.now();
        S.pricesStale = false;
        patchRealtimeDom();
      });
      S._priceTickTimer = setInterval(function () {
        if (document.hidden) return;
        var now = Date.now();
        var stale = S.lastPriceTs > 0 && now - S.lastPriceTs > 3e4;
        if (stale !== S.pricesStale) S.pricesStale = stale;
        patchRealtimeDom();
      }, 1e3);
    } catch (_) { }
  }
  var S = {
    slug: window.__EVENT_SLUG__ || "",
    event: null,
    error: "",
    balance: {
      usdc_balance: null,
      thresholds: {}
    },
    minTrade: 1,
    maxOrder: 500,
    minSellShares: .01,
    minBuyShares: 1,
    buyPriceCap: .99,
    selectedMarket: null,
    selectedMarketId: "",
    outcome: "YES",
    side: "BUY",
    orderType: "market",
    amountUsdc: 0,
    maxActive: false,
    priceCents: 50,
    sharesInt: 0,
    holdings: null,
    holdingsLoading: false,
    tab: "chart",
    orderbook: {
      bids: [],
      asks: [],
      best_bid: null,
      best_ask: null
    },
    trades: [],
    tradesLoading: false,
    holders: [],
    holdersLoading: false,
    holdersOutcome: "YES",
    chartOutcome: "YES",
    chartRange: "1d",
    chartRanges: ["1h", "6h", "1d", "7d", "all"],
    chartPoints: [],
    chartLoading: false,
    chartError: "",
    _chartInstance: null,
    _chartJsLoading: null,
    _chartReqToken: 0,
    _chartPaintToken: 0,
    _i18nBound: false,
    _priceUnsub: null,
    lastPriceTs: 0,
    pricesStale: false,
    referralCode: "",
    obOutcome: "YES",
    obShowAll: false,
    userPositions: [],
    userPositionsLoading: false,
    submitting: false,
    submitMsg: "",
    submitOk: false,
    balanceLoading: false,
    balanceError: false
  };
  function root() {
    return document.getElementById("ev-app-root");
  }
  function vpToast(msg, kind) {
    if (window.vp && vp.toast) {
      vp.toast({
        title: msg
      }, kind || "info");
      return;
    }
    try {
      console.log("[event_detail]", kind || "info", msg);
    } catch (_) { }
  }
  function tradableMarkets(ev) {
    var list = ev && ev.markets || [];
    var now = Date.now();
    var filtered = list.filter(function (m) {
      if (!m || m.closed) return false;
      if (m.end_date) {
        var ed = new Date(m.end_date).getTime();
        if (!isNaN(ed) && ed < now) return false;
      }
      if (!m.yes_token_id || !m.no_token_id) return false;
      var yp = yesProbOf(m);
      if (yp >= .9999 || yp <= 1e-4) return false;
      return true;
    });
    var selectedId = S && S.selectedMarketId ? String(S.selectedMarketId) : "";
    var selected = null, rest = [];
    for (var i = 0; i < filtered.length; i++) {
      var fm = filtered[i];
      var fmid = String(fm && fm.market_id || "");
      if (selectedId && fmid === selectedId) {
        selected = fm;
        continue;
      }
      rest.push(fm);
    }
    function buyYesPrice(m) {
      var p = Number(m && m.best_ask);
      if (isFinite(p) && p > 0 && p < 1) return p;
      return yesProbOf(m);
    }
    function buyNoPrice(m) {
      var b = Number(m && m.best_bid);
      if (isFinite(b) && b > 0 && b < 1) return Math.max(.001, Math.min(.999, 1 - b));
      return Math.max(.001, Math.min(.999, 1 - yesProbOf(m)));
    }
    function fitGap(m) {
      return Math.abs(buyYesPrice(m) - buyNoPrice(m));
    }
    rest.sort(function (a, b) {
      var ga = fitGap(a);
      var gb = fitGap(b);
      if (ga !== gb) return ga - gb;
      var la = Number(a && a.liquidity || 0);
      var lb = Number(b && b.liquidity || 0);
      if (la !== lb) return lb - la;
      var va = Number(a && a.volume_24hr || 0);
      var vb = Number(b && b.volume_24hr || 0);
      return vb - va;
    });
    var ordered = rest;
    if (selected) ordered.unshift(selected);
    return ordered;
  }
  function effectivePrice() {
    if (!S.selectedMarket) return .5;
    if (S.outcome === "YES") return Number(S.selectedMarket.best_ask || .5);
    return Math.max(.01, Math.min(.99, 1 - Number(S.selectedMarket.best_bid || .5)));
  }
  function sellSidePrice() {
    if (!S.selectedMarket) return 0;
    if (S.outcome === "YES") return Number(S.selectedMarket.best_bid || 0);
    return Math.max(0, Math.min(.99, 1 - Number(S.selectedMarket.best_ask || 1)));
  }
  function estShares() {
    return Math.max(S.minBuyShares, Math.floor(Number(S.amountUsdc) || 0));
  }
  function slippageEst() {
    if (!S.selectedMarket) return 0;
    var want = estShares();
    if (want <= 0) return 0;
    var book = S.outcome === "YES" ? S.orderbook.asks || [] : null;
    if (!book || book.length === 0) return 0;
    var remaining = want;
    var totalCost = 0;
    for (var i = 0; i < book.length; i++) {
      var lv = book[i] || {};
      if (remaining <= 0) break;
      var take = Math.min(remaining, Number(lv.size || 0));
      totalCost += take * Number(lv.price || 0);
      remaining -= take;
    }
    if (remaining > 0) return .05;
    var avg = totalCost / want;
    var best = Number(book[0] && book[0].price || .5);
    return best > 0 ? Math.max(0, (avg - best) / best) : 0;
  }
  function limitCost() {
    return (Number(S.sharesInt) || 0) * (Number(S.priceCents) || 0) / 100;
  }
  function activeTokenId() {
    if (!S.selectedMarket) return "";
    return S.outcome === "YES" ? S.selectedMarket.yes_token_id : S.selectedMarket.no_token_id;
  }
  function canSubmit() {
    if (!S.selectedMarket || !activeTokenId()) return false;
    var minSell = Number(S.minSellShares || .01);
    if (S.side === "BUY") {
      if (S.balance.usdc_balance !== null && Number(S.balance.usdc_balance) < Number(S.minTrade || 1)) return false;
    }
    if (S.orderType === "market") {
      if (S.side === "BUY") {
        var shares = Math.floor(Number(S.amountUsdc) || 0);
        if (shares < S.minBuyShares) return false;
        var notional = shares * Math.max(.01, Math.min(S.buyPriceCap || .99, effectivePrice() || .5));
        if (S.balance.usdc_balance !== null && notional > Number(S.balance.usdc_balance)) return false;
        if (notional > Number(S.maxOrder || 500)) return false;
      } else {
        if (S.holdings == null) return false;
        var s = Number(S.sharesInt) || 0;
        if (s < minSell) return false;
        if (s > Number(S.holdings || 0) + 1e-6) return false;
      }
      return true;
    }
    var lsh = Number(S.sharesInt) || 0;
    if (S.side === "SELL") {
      if (S.holdings == null) return false;
      if (lsh < minSell) return false;
      if (lsh > Number(S.holdings || 0) + 1e-6) return false;
    } else {
      if (lsh < 1) return false;
    }
    if (S.priceCents < 1 || S.priceCents > 99) return false;
    var cost = limitCost();
    if (cost < Number(S.minTrade || 1)) return false;
    if (S.side === "BUY") {
      if (S.balance.usdc_balance !== null && cost > Number(S.balance.usdc_balance)) return false;
      if (cost > Number(S.maxOrder || 500)) return false;
    }
    return true;
  }
  function obAsksDisplay() {
    var lim = S.obShowAll ? 20 : 7;
    var raw = (S.orderbook.asks || []).slice().sort(function (a, b) {
      return Number(a.price || 0) - Number(b.price || 0);
    });
    return raw.slice(0, lim);
  }
  function obBidsDisplay() {
    var lim = S.obShowAll ? 20 : 7;
    var raw = (S.orderbook.bids || []).slice().sort(function (a, b) {
      return Number(b.price || 0) - Number(a.price || 0);
    });
    return raw.slice(0, lim);
  }
  async function fetchUserPositions() {
    if (window.__pmGuestMode) {
      S.userPositions = [];
      S.userPositionsLoading = false;
      return;
    }
    S.userPositionsLoading = true;
    render();
    try {
      var r = await fetch("/api/pm/user/holdings", {
        credentials: "include"
      });
      if (!r.ok) {
        S.userPositions = [];
        return;
      }
      var d = await r.json();
      var all = Array.isArray(d.positions) ? d.positions : [];
      var event = S.event;
      var markets = event ? event.markets || [] : [];
      var condIds = {};
      for (var i = 0; i < markets.length; i++) {
        var cid = markets[i].condition_id;
        if (cid) condIds[cid] = markets[i];
      }
      S.userPositions = all.filter(function (p) {
        return p && Number(p.shares || 0) > 0 && condIds[String(p.condition_id || "")];
      });
      if (S.userPositions.length > 0 && event && Array.isArray(event.markets)) {
        var firstPos = S.userPositions[0];
        var heldCidLower = String(firstPos.condition_id || "").toLowerCase();
        var hit = event.markets.find(function (m) {
          return m && String(m.condition_id || "").toLowerCase() === heldCidLower;
        });
        var selectedCidLower = S.selectedMarket ? String(S.selectedMarket.condition_id || "").toLowerCase() : "";
        var heldCondSet = {};
        S.userPositions.forEach(function (p) {
          heldCondSet[String(p.condition_id || "").toLowerCase()] = true;
        });
        var selectedIsHeld = selectedCidLower && heldCondSet[selectedCidLower];
        if (hit && (!S.selectedMarket || !selectedIsHeld)) {
          await selectMarket(hit);
          S.outcome = Number(firstPos.outcome_index || 0) === 0 ? "YES" : "NO";
          setSide("SELL");
        }
      }
    } catch (_) {
      S.userPositions = [];
    } finally {
      S.userPositionsLoading = false;
      render();
    }
  }
  async function loadTrades() {
    if (!S.selectedMarket || !S.selectedMarket.condition_id) {
      S.trades = [];
      S.tradesLoading = false;
      return;
    }
    S.tradesLoading = true;
    render();
    try {
      var r = await fetch("/api/pm/market/trades?market=" + encodeURIComponent(S.selectedMarket.condition_id) + "&limit=30", {
        credentials: "include"
      });
      var d = r.ok ? await r.json() : {
        trades: []
      };
      S.trades = Array.isArray(d.trades) ? d.trades : [];
    } catch (_) {
      S.trades = [];
    } finally {
      S.tradesLoading = false;
      render();
    }
  }
  async function loadHolders() {
    if (!S.selectedMarket || !S.selectedMarket.condition_id) {
      S.holders = [];
      S.holdersLoading = false;
      return;
    }
    S.holdersLoading = true;
    render();
    try {
      var r = await fetch("/api/pm/market/holders?market=" + encodeURIComponent(S.selectedMarket.condition_id) + "&limit=20", {
        credentials: "include"
      });
      var d = r.ok ? await r.json() : {
        holders: []
      };
      var tok = S.holdersOutcome === "YES" ? S.selectedMarket && S.selectedMarket.yes_token_id : S.selectedMarket && S.selectedMarket.no_token_id;
      var outcomes = d.outcomes || d.holders || [];
      if (Array.isArray(outcomes) && outcomes.length > 0 && outcomes[0].holders) {
        var matched = outcomes.find(function (o) {
          return String(o.token || "") === String(tok || "");
        }) || outcomes[0];
        S.holders = Array.isArray(matched.holders) ? matched.holders.slice(0, 20) : [];
      } else {
        S.holders = Array.isArray(outcomes) ? outcomes.slice(0, 20) : [];
      }
    } catch (_) {
      S.holders = [];
    } finally {
      S.holdersLoading = false;
      render();
    }
  }
  function buildChartSvg(points) {
    if (!points || !points.length) return "";
    var w = 720;
    var h = 280;
    var pad = 26;
    var minX = points[0].x;
    var maxX = points[points.length - 1].x;
    var minY = 0;
    var maxY = 100;
    if (maxX <= minX) maxX = minX + 1;
    var pts = points.map(function (p) {
      var x = pad + (p.x - minX) / (maxX - minX) * (w - pad * 2);
      var y = h - pad - (p.y - minY) / (maxY - minY) * (h - pad * 2);
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    var color = S.chartOutcome === "YES" ? "#22c55e" : "#ef4444";
    return '<svg viewBox="0 0 ' + w + " " + h + '" width="100%" height="280" role="img" aria-label="price chart">' + '<rect x="0" y="0" width="' + w + '" height="' + h + '" fill="rgba(15,23,42,.28)" rx="10"/>' + '<line x1="' + pad + '" y1="' + (h - pad) + '" x2="' + (w - pad) + '" y2="' + (h - pad) + '" stroke="rgba(148,163,184,.35)" stroke-width="1"/>' + '<line x1="' + pad + '" y1="' + pad + '" x2="' + pad + '" y2="' + (h - pad) + '" stroke="rgba(148,163,184,.35)" stroke-width="1"/>' + '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>' + '<text x="' + (pad + 2) + '" y="' + (pad - 6) + '" fill="rgba(148,163,184,.8)" font-size="11">100c</text>' + '<text x="' + (pad + 2) + '" y="' + (h - pad + 14) + '" fill="rgba(148,163,184,.8)" font-size="11">0c</text>' + "</svg>";
  }
  async function loadChart() {
    if (!S.selectedMarket) {
      S.chartPoints = [];
      S.chartLoading = false;
      S.chartError = "";
      _destroyChart();
      return;
    }
    var tokenId = S.chartOutcome === "YES" ? S.selectedMarket.yes_token_id || S.selectedMarket.no_token_id : S.selectedMarket.no_token_id || S.selectedMarket.yes_token_id;
    if (!tokenId) {
      S.chartPoints = [];
      S.chartLoading = false;
      S.chartError = "";
      _destroyChart();
      return;
    }
    S._chartReqToken = (S._chartReqToken || 0) + 1;
    var myToken = S._chartReqToken;
    S.chartLoading = true;
    S.chartError = "";
    render();
    try {
      var url = "/api/pm/market/price_history?market=" + encodeURIComponent(tokenId) + "&range=" + encodeURIComponent(S.chartRange);
      var r = await fetch(url, {
        credentials: "include"
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      var d = await r.json();
      var history = d && d.history || [];
      var pts = [];
      for (var i = 0; i < history.length; i++) {
        var h = history[i] || {};
        var ts = Number(h.t || 0);
        if (ts > 0 && ts < 1e12) ts *= 1e3;
        var px = Number(h.p);
        if (!ts || !(px >= 0 && px <= 1)) continue;
        pts.push({
          x: ts,
          y: Math.round(px * 1e3) / 10
        });
      }
      pts.sort(function (a, b) {
        return a.x - b.x;
      });
      if (pts.length === 0 && S.selectedMarket.condition_id) {
        var ranges = {
          "1h": 500,
          "6h": 3e3,
          "1d": 12e3,
          "7d": 4e4,
          all: 8e4
        };
        var limit = ranges[S.chartRange] || 12e3;
        var trUrl = "/api/pm/market/trades?market=" + encodeURIComponent(S.selectedMarket.condition_id) + "&limit=" + limit;
        var tr = await fetch(trUrl, {
          credentials: "include"
        });
        if (tr.ok) {
          var td = await tr.json();
          var trades = td && td.trades || [];
          for (var j = 0; j < trades.length; j++) {
            var t = trades[j] || {};
            var rawTs = Number(t.timestamp || 0);
            var tms = rawTs * (String(t.timestamp || "").length <= 11 ? 1e3 : 1);
            var tp = Number(t.price);
            if (!tms || !(tp >= 0 && tp <= 1)) continue;
            var fallbackShow = S.chartOutcome === "YES" ? tp : 1 - tp;
            pts.push({
              x: tms,
              y: Math.round(fallbackShow * 1e3) / 10
            });
          }
          pts.sort(function (a, b) {
            return a.x - b.x;
          });
          var winMap = {
            "1h": 36e5,
            "6h": 216e5,
            "1d": 864e5,
            "7d": 6048e5,
            all: 0
          };
          var win = winMap[S.chartRange] || 0;
          if (win > 0 && pts.length > 0) {
            var cutoff = pts[pts.length - 1].x - win;
            pts = pts.filter(function (p) {
              return p.x >= cutoff;
            });
          }
        }
      }
      if (myToken !== S._chartReqToken) return;
      S.chartPoints = pts;
    } catch (e) {
      if (myToken !== S._chartReqToken) return;
      S.chartPoints = [];
      S.chartError = String(e && e.message || e);
      _destroyChart();
    } finally {
      if (myToken === S._chartReqToken) {
        S.chartLoading = false;
        render();
        if (S.chartPoints.length > 0 && !S.chartError) {
          _loadChartJs().then(function () {
            if (myToken === S._chartReqToken) _renderChartCanvas();
          }).catch(function (e) {
            if (myToken === S._chartReqToken) {
              S.chartError = "Chart.js load failed";
              render();
            }
          });
        }
      }
    }
  }
  function loadActiveTab() {
    if (S.tab === "chart") {
      loadChart();
      return;
    }
    _destroyChart();
    if (S.tab === "history") {
      loadTrades();
      return;
    }
    if (S.tab === "holders") {
      loadHolders();
    }
  }
  function render() {
    var r = root();
    if (!r) return;
    r.removeAttribute("hidden");
    try {
      document.body.classList.add("ev-app-ready");
    } catch (_) { }
    if (S.error) {
      r.innerHTML = '<div class="v-card" style="border:1px solid #dc2626;background:rgba(220,38,38,.08);color:#fca5a5">' + esc(t("event.loadFailed", "Load failed: ")) + esc(S.error) + "</div>";
      return;
    }
    if (!S.event) {
      r.innerHTML = '<div class="ev-loading-shell"><div class="ev-grid"><div class="skel-wrap"><div class="skel-block" style="height:280px;border-radius:12px"></div><div class="skel-spinner"></div></div><div class="skel-wrap"><div class="skel-block" style="height:380px;border-radius:12px"></div><div class="skel-spinner"></div></div></div></div>';
      return;
    }
    var markets = tradableMarkets(S.event);
    var marketHtml = markets.length ? markets.map(function (m) {
      var active = S.selectedMarketId === m.market_id ? " selected" : "";
      var yes = yesCellPx(m);
      var no = noCellPx(m);
      return '<div class="sub-mkt' + active + '" data-vp-mkt-id="' + esc(m.market_id) + '">' + '<div class="sub-mkt-meta"><div class="sub-mkt-q">' + esc(m.question || "") + "</div>" + '<div class="sub-mkt-stats"><span>YES <span data-vp-mkt-yesp="' + esc(m.market_id) + '">' + (yesProbOf(m) * 100).toFixed(1) + "</span>%</span><span>24h $" + esc(fmt(m.volume_24hr)) + "</span><span>" + esc(t("event.stat.liquidity", "Liquidity")) + " $" + esc(fmt(m.liquidity)) + "</span></div></div>" + '<div class="sub-mkt-buttons">' + '<button class="sub-mkt-btn yes" data-vp-quick-out="YES" data-vp-quick-id="' + esc(m.market_id) + '"><span class="sb-label">' + esc(t("event.buy.yes", "Buy YES")) + '</span><span class="sb-cents" data-vp-mkt-yes="' + esc(m.market_id) + '">' + esc(yes) + "</span></button>" + '<button class="sub-mkt-btn no" data-vp-quick-out="NO" data-vp-quick-id="' + esc(m.market_id) + '"><span class="sb-label">' + esc(t("event.buy.no", "Buy NO")) + '</span><span class="sb-cents" data-vp-mkt-no="' + esc(m.market_id) + '">' + esc(no) + "</span></button>" + "</div></div>";
    }).join("") : "";
    var selectedTitle = S.selectedMarket ? esc(S.selectedMarket.question || "") : esc(t("event.panel.selectFirst", "Please select an option from the left"));
    var _isSell = S.side === "SELL";
    var yesPx = S.selectedMarket ? _isSell ? yesCellSellPx(S.selectedMarket) : yesCellPx(S.selectedMarket) : "-";
    var noPx = S.selectedMarket ? _isSell ? noCellSellPx(S.selectedMarket) : noCellPx(S.selectedMarket) : "-";
    var bal = S.balanceError === "not_logged_in" ? esc(t("event.panel.balance.signIn", "Sign in")) : S.balanceError === "session_expired" ? esc(t("event.panel.balance.session", "Session expired")) : S.balanceError ? esc(t("event.panel.balance.err", "Load error")) : S.balanceLoading ? esc(t("event.panel.balance.loading", "Loading…")) : S.balance.usdc_balance == null ? esc(t("event.panel.balance.loading", "Loading…")) : "$" + Number(S.balance.usdc_balance).toFixed(2);
    var obToggleLabel = S.obShowAll ? esc(t("event.ob.collapse", "Collapse (Top 7)")) : esc(t("event.ob.expand", "Show all (Top 20)"));
    var tabHtml = "";
    if (!S.selectedMarket) {
      tabHtml = '<div class="empty-tab">' + esc(t("event.trades.selectFirst", "Please select an option above")) + "</div>";
    } else if (S.tab === "chart") {
      var chartToolbar = '<div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:8px">' + '<div style="display:flex;gap:6px"><button class="v-btn v-btn-sm ' + (S.chartOutcome === "YES" ? "v-btn-primary" : "v-btn-outline") + '" data-vp-chart-out="YES">YES</button>' + '<button class="v-btn v-btn-sm ' + (S.chartOutcome === "NO" ? "v-btn-primary" : "v-btn-outline") + '" data-vp-chart-out="NO">NO</button></div>' + '<div style="display:flex;gap:6px;flex-wrap:wrap">' + S.chartRanges.map(function (rk) {
        var lb = rk === "1d" ? "24h" : rk;
        var cls = S.chartRange === rk ? "v-btn-primary" : "v-btn-outline";
        return '<button class="v-btn v-btn-sm ' + cls + '" data-vp-chart-range="' + rk + '">' + lb + "</button>";
      }).join("") + "</div></div>";
      if (S.chartLoading) {
        tabHtml = chartToolbar + '<div style="position:relative;height:340px;background:var(--bg-primary);border:1px solid var(--border);border-radius:10px;padding:8px"><div class="skel-wrap" style="height:100%"><div class="skel-spinner" aria-label="loading"></div></div></div>';
      } else if (S.chartError) {
        tabHtml = chartToolbar + '<div style="height:340px;background:var(--bg-primary);border:1px solid var(--border);border-radius:10px;display:flex;align-items:center;justify-content:center;color:#fca5a5;font-size:.8rem">' + esc(S.chartError) + "</div>";
      } else if (!S.chartPoints.length) {
        tabHtml = chartToolbar + '<div style="height:340px;background:var(--bg-primary);border:1px solid var(--border);border-radius:10px;display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:.8rem">' + esc(t("event.chart.empty", "No price history available")) + "</div>";
      } else {
        tabHtml = chartToolbar + '<div style="position:relative;height:340px;background:var(--bg-primary);border:1px solid var(--border);border-radius:10px;padding:8px">' + '<canvas id="vp-chart-canvas" style="width:100%;height:100%"></canvas>' + "</div>" + '<div style="font-size:.7rem;color:var(--text-muted);margin-top:6px;line-height:1.5">' + esc(t("event.chart.help", "💡 Realtime price (¢) reconstructed from on-chain trades. Hover for exact value.")) + "</div>";
      }
    } else if (S.tab === "orderbook") {
      var asks = obAsksDisplay();
      var bids = obBidsDisplay();
      var totalAsk = asks.reduce(function (acc, lv) {
        return acc + Number(lv.size || 0);
      }, 0);
      var totalBid = bids.reduce(function (acc, lv) {
        return acc + Number(lv.size || 0);
      }, 0);
      var askCum = 0;
      var bidCum = 0;
      var asksHtml = asks.map(function (lv) {
        var size = Number(lv.size || 0);
        askCum += size;
        var depth = totalAsk > 0 ? Math.min(100, askCum / totalAsk * 100) : 0;
        return '<div class="ob-row" data-vp-ob-kind="ask" data-vp-ob-price="' + Number(lv.price || 0) + '">' + '<span class="ob-price">' + (Number(lv.price || 0) * 100).toFixed(1) + "</span>" + "<span>" + size.toFixed(0) + "</span>" + "<span>" + askCum.toFixed(0) + "</span>" + '<span class="ob-cell-bar" style="--depth:' + depth.toFixed(1) + '%"></span>' + "</div>";
      }).join("");
      var bidsHtml = bids.map(function (lv) {
        var size = Number(lv.size || 0);
        bidCum += size;
        var depth = totalBid > 0 ? Math.min(100, bidCum / totalBid * 100) : 0;
        return '<div class="ob-row" data-vp-ob-kind="bid" data-vp-ob-price="' + Number(lv.price || 0) + '">' + '<span class="ob-price">' + (Number(lv.price || 0) * 100).toFixed(1) + "</span>" + "<span>" + size.toFixed(0) + "</span>" + "<span>" + bidCum.toFixed(0) + "</span>" + '<span class="ob-cell-bar" style="--depth:' + depth.toFixed(1) + '%"></span>' + "</div>";
      }).join("");
      var bb = Number(S.orderbook.best_bid || 0);
      var ba = Number(S.orderbook.best_ask || 0);
      var spreadStr = bb > 0 && ba > 0 && ba > bb ? '<span class="ob-spread-val">Spread ' + ((ba - bb) * 100).toFixed(1) + "¢</span>" : "";
      tabHtml = '<div class="ob-toolbar">' + '<div class="ob-toggle">' + '<button class="' + (S.obOutcome === "YES" ? "active yes" : "") + '" id="vp-ob-out-yes">YES</button>' + '<button class="' + (S.obOutcome === "NO" ? "active no" : "") + '" id="vp-ob-out-no">NO</button>' + "</div>" + '<button class="v-btn v-btn-outline v-btn-sm" id="vp-ob-toggle">' + obToggleLabel + "</button>" + "</div>" + '<div class="ob-table">' + '<div class="ob-header"><span>' + esc(t("event.ob.col.price", "Price (¢)")) + "</span><span>" + esc(t("event.ob.col.size", "Size")) + "</span><span>" + esc(t("event.ob.col.cum", "Cum")) + "</span></div>" + (asks.length === 0 ? '<div class="ob-empty">' + esc(t("event.ob.empty.asks", "No asks")) + "</div>" : "") + '<div class="ob-section asks">' + asksHtml + "</div>" + '<div class="ob-spread"><span>' + esc(t("event.ob.last", "Last")) + ' <span class="ob-last">' + esc(cents(effectivePrice())) + "</span></span>" + spreadStr + "</div>" + (bids.length === 0 ? '<div class="ob-empty">' + esc(t("event.ob.empty.bids", "No bids")) + "</div>" : "") + '<div class="ob-section bids">' + bidsHtml + "</div>" + "</div>" + '<div style="font-size:.7rem;color:var(--text-muted);margin-top:6px;line-height:1.5">💡 ' + esc(t("event.ob.help.line1", "Prices shown in cents (¢), unit = $0.01.")) + " " + '<span style="color:#fca5a5">' + esc(t("event.ob.help.askLabel", "Red ASK")) + "</span>" + esc(t("event.ob.help.askDesc", " is the price others want to sell at (click = buy now); ")) + '<span style="color:#86efac">' + esc(t("event.ob.help.bidLabel", "Green BID")) + "</span>" + esc(t("event.ob.help.bidDesc", " is the price others want to buy at (click = sell now).")) + "</div>";
    } else if (S.tab === "history") {
      if (S.tradesLoading) {
        tabHtml = '<div class="empty-tab">' + esc(t("event.trades.loading", "Loading trade history...")) + "</div>";
      } else if (!S.trades.length) {
        tabHtml = '<div class="empty-tab">' + esc(t("event.trades.empty", "No trades")) + "</div>";
      } else {
        tabHtml = '<div style="max-height:360px;overflow-y:auto"><div style="display:grid;grid-template-columns:1.6fr .9fr .8fr .9fr;gap:6px;padding:6px 8px;font-size:.72rem;color:var(--text-muted);border-bottom:1px solid var(--border)"><span>' + esc(t("event.trades.col.user", "User / Address")) + "</span><span>" + esc(t("event.trades.col.side", "Side")) + '</span><span style="text-align:right">' + esc(t("event.trades.col.price", "Price")) + '</span><span style="text-align:right">' + esc(t("event.trades.col.shares", "Shares")) + "</span></div>" + S.trades.map(function (tt) {
          var u = pmUserUrl(tt);
          return '<div style="display:grid;grid-template-columns:1.6fr .9fr .8fr .9fr;gap:6px;padding:5px 8px;font-size:.78rem;border-bottom:1px solid rgba(255,255,255,.04);align-items:center">' + '<a href="' + esc(u) + '" target="_blank" rel="noopener" style="color:var(--text-secondary);text-decoration:none;display:inline-flex;align-items:center;gap:4px" title="' + esc(String(tt.proxyWallet || tt.proxy_wallet || "")) + '"><span style="' + (tt.name ? "" : "font-family:monospace") + '">' + esc(pmDisplayName(tt)) + '</span><span style="color:var(--accent);font-size:.7rem">↗</span></a>' + "<span>" + sideOutcomeBadge(tt) + '</span><span style="text-align:right">' + esc(cents(tt.price)) + '</span><span style="text-align:right">' + Number(tt.size || tt.shares || 0).toFixed(0) + "</span></div>";
        }).join("") + "</div>";
      }
    } else if (S.tab === "holders") {
      if (S.holdersLoading) {
        tabHtml = '<div class="empty-tab">' + esc(t("event.holders.loading", "Loading holders...")) + "</div>";
      } else if (!S.holders.length) {
        tabHtml = '<div class="empty-tab">' + esc(t("event.holders.empty", "No holder data")) + "</div>";
      } else {
        tabHtml = '<div style="max-height:360px;overflow-y:auto"><div style="display:grid;grid-template-columns:1.8fr .9fr .9fr;gap:6px;padding:6px 8px;font-size:.72rem;color:var(--text-muted);border-bottom:1px solid var(--border)"><span style="text-align:left">' + esc(t("event.holders.col.user", "User / Address")) + '</span><button data-vp-holders-out="1" style="background:transparent;border:0;color:var(--text-muted);cursor:pointer;font-size:.72rem;text-align:left;padding:0;display:inline-flex;align-items:center;gap:4px" title="' + esc(t("event.holders.toggleTitle.prefix", "Current: ")) + esc(S.holdersOutcome) + esc(t("event.holders.toggleTitle.suffix", " · click to toggle")) + '"><span>' + esc(t("event.holders.col.side", "Side")) + '</span><span style="color:var(--accent);font-size:.7rem">(' + esc(S.holdersOutcome) + ' ⇄)</span></button><span style="text-align:right">' + esc(t("event.holders.col.shares", "Shares")) + "</span></div>" + S.holders.map(function (h) {
          var u = pmUserUrl(h);
          return '<div style="display:grid;grid-template-columns:1.8fr .9fr .9fr;gap:6px;padding:5px 8px;font-size:.78rem;border-bottom:1px solid rgba(255,255,255,.04);align-items:center">' + '<a href="' + esc(u) + '" target="_blank" rel="noopener" style="color:var(--text-secondary);text-decoration:none;display:inline-flex;align-items:center;gap:4px;justify-content:flex-start" title="' + esc(String(h.proxyWallet || h.proxy_wallet || "")) + '"><span style="' + (h.name ? "" : "font-family:monospace") + '">' + esc(pmDisplayName(h)) + '</span><span style="color:var(--accent);font-size:.7rem">↗</span></a>' + "<span>" + holderOutcomeBadge(h) + '</span><span style="text-align:right">' + Number(h.amount || h.shares || 0).toFixed(0) + "</span></div>";
        }).join("") + "</div>";
      }
    } else {
      tabHtml = '<div class="empty-tab">' + esc(t("event.tab.unavailable", "This tab is unavailable")) + "</div>";
    }
    var submitLabel = S.orderType === "market" ? (S.side === "BUY" ? t("event.qb.submit.buy", "Buy ") : t("event.qb.submit.sell", "Sell ")) + S.outcome : t("event.qb.submit.limitCreate", "Create ") + (S.side === "BUY" ? "Buy" : "Sell") + t("event.qb.submit.limitOrderSuffix", " order");
    var body;
    if (!S.selectedMarket) {
      body = '<div style="text-align:center;color:var(--text-muted);padding:26px 10px;font-size:.86rem">' + esc(t("event.panel.selectFirst", "Please select an option from the left")) + "</div>";
    } else if (S.orderType === "market" && S.side === "BUY") {
      body = '<div class="op-section"><div class="op-section-label"><span>' + esc(t("event.qb.shares.label", "Shares")) + '</span><span style="font-size:.72rem;color:var(--text-muted)">' + esc(t("event.qb.balance", "Balance: $")) + bal + (S.balanceError ? ' <button id="vp-ed-bal-retry" style="font-size:.7rem;padding:0 4px;margin-left:4px;cursor:pointer">' + esc(t("event.qb.balance.retry", "Retry")) + "</button>" : "") + "</span></div>" + '<div class="stepper-input"><button id="vp-ed-amt-minus"' + ((Number(S.amountUsdc) || 0) <= Number(S.minBuyShares || 1) ? " disabled" : "") + '>−</button><input id="vp-ed-amt-input" type="number" min="' + Number(S.minBuyShares || 1) + '" step="1" value="' + (Number(S.amountUsdc) || 0) + '"><span class="stepper-suffix"></span><button id="vp-ed-amt-plus">+</button></div>' + '<div class="op-quick-amts" style="margin-top:8px"><button class="op-quick-amt' + (Number(S.amountUsdc) === 10 ? " active" : "") + '" data-vp-amt="10">10</button><button class="op-quick-amt' + (Number(S.amountUsdc) === 50 ? " active" : "") + '" data-vp-amt="50">50</button><button class="op-quick-amt' + (Number(S.amountUsdc) === 100 ? " active" : "") + '" data-vp-amt="100">100</button><button class="op-quick-amt' + (S.maxActive ? " active" : "") + '" id="vp-ed-amt-max"' + (S.balance.usdc_balance == null || Number(S.balance.usdc_balance || 0) < Number(S.minTrade || 1) ? " disabled" : "") + ">" + esc(t("event.qb.max", "Max")) + "</button></div>" + "</div>" + '<div class="op-summary"><div class="op-sum-row"><span>' + esc(t("event.qb.totalCost", "Total cost")) + '</span><b class="cost">$' + (estShares() * effectivePrice()).toFixed(2) + '</b></div><div class="op-sum-row"><span>' + esc(t("event.qb.estProfit", "Est. profit")) + '</span><b class="profit">$' + Math.max(0, estShares() * (1 - effectivePrice())).toFixed(2) + " (" + (Math.max(0, estShares() * (1 - effectivePrice())) / Math.max(.01, estShares() * effectivePrice()) * 100).toFixed(2) + "%)</b></div></div>";
    } else {
      var shareStep = S.side === "SELL" ? "0.01" : "1";
      var shareMin = S.side === "SELL" ? String(S.minSellShares || .01) : "1";
      var sellInfo = S.side === "SELL" ? '<span style="font-size:.72rem;color:var(--text-muted)">' + esc(t("event.qb.position", "Position: ")) + (S.holdings == null ? "-" : Number(S.holdings).toFixed(4)) + "</span>" : "";
      var top = S.orderType === "limit" ? '<div class="op-section"><div class="op-section-label"><span>' + esc(t("event.qb.limitPrice", "Limit price")) + '</span></div><div class="stepper-input"><button id="vp-ed-px-minus">-</button><input id="vp-ed-px-input" type="number" min="1" max="99" step="1" value="' + S.priceCents + '"><span class="stepper-suffix">c</span><button id="vp-ed-px-plus">+</button></div></div>' : "";
      body = top + '<div class="op-section"><div class="op-section-label"><span>' + esc(t("event.qb.shares", "Shares")) + "</span>" + sellInfo + "</div>" + '<div class="stepper-input"><button id="vp-ed-sh-minus">-</button><input id="vp-ed-sh-input" type="number" min="' + shareMin + '" step="' + shareStep + '" value="' + (Number(S.sharesInt) || 0) + '"><span class="stepper-suffix" style="padding-right:16px"></span><button id="vp-ed-sh-plus">+</button></div>' + '<div class="op-share-adjusts"><button class="op-share-adj" data-vp-sh="-10">-10</button><button class="op-share-adj" data-vp-sh="10">+10</button><button class="op-share-adj" data-vp-sh="max">' + esc(t("event.qb.max", "Max")) + "</button></div></div>" + '<div class="op-summary"><div class="op-sum-row"><span>' + esc(S.side === "BUY" ? t("event.qb.totalCost", "Total cost") : t("event.qb.estReceive", "Est. receive")) + '</span><b class="cost">$' + (S.orderType === "limit" ? limitCost() : Number(S.sharesInt) * sellSidePrice()).toFixed(2) + "</b></div></div>";
    }
    var positionsHtml = "";
    if (S.userPositionsLoading) {
      positionsHtml = '<div class="ev-section-card" style="background:rgba(139,92,246,.04)">' + '<div style="padding:12px;text-align:center;font-size:.78rem;color:var(--text-muted)">' + esc(t("event.holding.loading", "Loading my positions...")) + "</div></div>";
    } else if (S.userPositions.length > 0) {
      var posRows = S.userPositions.map(function (p) {
        var condId = String(p.condition_id || "");
        var mkt = null;
        var mktList = tradableMarkets(S.event);
        for (var mi = 0; mi < mktList.length; mi++) {
          if (mktList[mi].condition_id === condId) {
            mkt = mktList[mi];
            break;
          }
        }
        if (!mkt && S.event && Array.isArray(S.event.markets)) {
          for (var mj = 0; mj < S.event.markets.length; mj++) {
            if (String(S.event.markets[mj].condition_id || "") === condId) {
              mkt = S.event.markets[mj];
              break;
            }
          }
        }
        var q = mkt ? esc(mkt.question || "") : esc(p.title || "(unknown)");
        var outIdx = Number(p.outcome_index || 0);
        var outLabel = outIdx === 0 ? "YES" : "NO";
        var outStyle = outIdx === 0 ? "background:rgba(34,197,94,.15);color:#16a34a" : "background:rgba(239,68,68,.15);color:#ef4444";
        var shares = Number(p.shares || 0);
        var avgPx = Number(p.avg_price || 0);
        var apiMark = Number(p.cur_price || 0);
        var apiValue = Number(p.current_value || 0);
        var apiPnl = p.cash_pnl != null ? Number(p.cash_pnl) : null;
        var redeemable = !!p.redeemable;
        var bookMark = mkt ? outIdx === 0 ? Number(mkt.best_ask || 0) : Math.max(.01, Math.min(.99, 1 - Number(mkt.best_bid || 0))) : 0;
        var markPx = apiMark > 0 ? apiMark : bookMark;
        var value = apiValue > 0 ? apiValue : shares * markPx;
        var cost = shares * avgPx;
        var pnl = apiPnl != null ? apiPnl : value - cost;
        var pnlStyle = pnl >= 0 ? "color:#16a34a" : "color:#ef4444";
        var pnlStr = (pnl >= 0 ? "+" : "") + "$" + pnl.toFixed(2);
        var mktId = mkt ? esc(mkt.market_id || "") : "";
        var markCell = redeemable ? '<span style="color:#16a34a;font-weight:600">' + (markPx > 0 ? markPx.toFixed(3) : "1.000") + ' <span style="font-size:.62rem;font-weight:500;padding:0 4px;border-radius:3px;background:rgba(34,197,94,.18)">' + esc(t("event.holding.redeemable", "redeemable")) + "</span></span>" : markPx > 0 ? markPx.toFixed(3) : "—";
        return '<tr style="border-bottom:1px solid rgba(148,163,184,.08);' + (mktId ? "cursor:pointer" : "") + '"' + (mktId ? ' data-vp-pos-mkt="' + mktId + '" data-vp-pos-out="' + outLabel + '"' : "") + ">" + '<td style="padding:8px 10px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + q + ' <span style="margin-left:5px;padding:1px 5px;border-radius:3px;font-size:.65rem;font-weight:600;' + outStyle + '">' + outLabel + "</span></td>" + '<td style="padding:8px 6px;text-align:right;font-variant-numeric:tabular-nums">' + shares.toFixed(4) + "</td>" + '<td style="padding:8px 6px;text-align:right;font-variant-numeric:tabular-nums">' + avgPx.toFixed(3) + "</td>" + '<td style="padding:8px 6px;text-align:right;font-variant-numeric:tabular-nums;color:var(--text-secondary)">' + markCell + "</td>" + '<td style="padding:8px 6px;text-align:right;font-variant-numeric:tabular-nums">$' + value.toFixed(2) + "</td>" + '<td style="padding:8px 10px;text-align:right;font-variant-numeric:tabular-nums;font-weight:600;' + pnlStyle + '">' + pnlStr + "</td>" + "</tr>";
      }).join("");
      positionsHtml = '<div class="ev-section-card">' + '<div class="ev-section-head"><span>📊 ' + esc(t("event.section.userHoldings", "My Positions")) + "</span>" + '<span style="font-size:.72rem;color:var(--text-muted);font-weight:400">' + S.userPositions.length + " " + esc(t("event.holding.outcomeUnit", "outcomes")) + "</span></div>" + '<div class="ev-section-body" style="padding:0">' + '<table style="width:100%;border-collapse:collapse;font-size:.78rem">' + '<thead><tr style="color:var(--text-muted);text-align:left;border-bottom:1px solid var(--border)">' + '<th style="padding:8px 10px;font-weight:500">' + esc(t("event.holding.col.market", "Market / Outcome")) + "</th>" + '<th style="padding:8px 6px;font-weight:500;text-align:right">' + esc(t("event.holding.col.shares", "Shares")) + "</th>" + '<th style="padding:8px 6px;font-weight:500;text-align:right">' + esc(t("event.holding.col.avgPrice", "Avg Price")) + "</th>" + '<th style="padding:8px 6px;font-weight:500;text-align:right">' + esc(t("event.holding.col.markPrice", "Mark")) + "</th>" + '<th style="padding:8px 6px;font-weight:500;text-align:right">' + esc(t("event.holding.col.value", "Value")) + "</th>" + '<th style="padding:8px 10px;font-weight:500;text-align:right">' + esc(t("event.holding.col.pnl", "P&L")) + "</th>" + "</tr></thead><tbody>" + posRows + "</tbody></table>" + '<div style="padding:6px 10px;font-size:.68rem;color:var(--text-muted);border-top:1px solid var(--border)">' + esc(t("event.holding.tip", "Click a row to switch to the sell panel for that market + outcome")) + "</div>" + "</div></div>";
    }
    var aiText = S.event.ai_summary || aiSummary(S.event);
    var aiHtml = '<div class="ev-ai-card"><div class="ev-ai-title">' + esc(t("event.section.aiSummary", "Veyra AI Summary")) + '</div><div class="ev-ai-text">' + esc(aiText) + "</div></div>";
    var descHtml = S.event.description ? '<details class="ev-section-card" style="cursor:pointer"><summary class="ev-section-head" style="cursor:pointer;list-style:none"><span>📄 ' + esc(t("event.section.description", "Event Description")) + '</span><span style="font-size:.72rem;color:var(--text-muted);font-weight:400">' + esc(t("event.expand", "Click to expand")) + '</span></summary><div class="ev-section-body"><div class="ev-desc">' + esc(S.event.description) + "</div></div></details>" : "";
    var endDateStr = formatEnd(S.event.end_date);
    var heroTagsHtml = (endDateStr ? "<span>" + esc(endDateStr) + "</span>" : "") + "<span>· " + esc(t("event.subEvents.prefix", "Total ")) + esc(S.event.markets_count || markets.length) + " " + esc(t("event.subEvents.suffix", "sub-events")) + "</span>";
    var totalMkts = (S.event.markets || []).length;
    var shownMkts = markets.length;
    var marketsCardHtml = shownMkts > 0 ? '<div class="ev-section-card"><div class="ev-section-head"><span>' + esc(t("event.section.options", "Markets")) + '</span><span style="font-size:.72rem;color:var(--text-muted);font-weight:400">' + shownMkts + " / " + totalMkts + " " + esc(t("event.options.unit", "shown")) + '</span></div><div style="max-height:' + (shownMkts > 3 ? "360px" : "auto") + ';overflow-y:auto">' + marketHtml + "</div></div>" : "";
    r.innerHTML = '<div class="ev-hero"><img class="ev-hero-img" src="' + esc(S.event.image || S.event.icon || "/static/VagusPloy.png") + '" width="88" height="88" decoding="async" loading="lazy" onerror="this.style.visibility=\'hidden\'">' + '<div class="ev-hero-meta"><div class="ev-hero-title">' + esc(S.event.title || "") + '</div><div class="ev-hero-tags">' + heroTagsHtml + '</div><div class="ev-stat-row"><div class="ev-stat-item ev-stat-vol"><span>' + esc(t("event.stat.vol24h", "24h Volume")) + "</span><b>$" + esc(fmt(S.event.volume_24hr)) + '</b></div><div class="ev-stat-item"><span>' + esc(t("event.stat.totalVol", "Total Volume")) + "</span><b>$" + esc(fmt(S.event.volume)) + '</b></div><div class="ev-stat-item"><span>' + esc(t("event.stat.liquidity", "Liquidity")) + "</span><b>$" + esc(fmt(S.event.liquidity)) + "</b></div></div></div>" + '<div class="ev-share-card" id="vp-ed-share" role="button" tabindex="0"><div class="ev-share-ico"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg></div><div class="ev-share-text"><span class="ev-share-title">' + esc(t("event.share.title", "Share & Earn")) + '</span><span class="ev-share-sub">' + esc(t("event.share.sub", "Tap to copy a referral link")) + "</span></div></div>" + "</div>" + '<div class="ev-grid"><div style="display:flex;flex-direction:column;gap:14px">' + positionsHtml + marketsCardHtml + '<div class="ev-section-card"><div class="ev-tabs-nav"><button class="ev-tab-btn' + (S.tab === "chart" ? " active" : "") + '" data-vp-tab="chart">📈 ' + esc(t("event.tab.priceChart", "Price Chart")) + '</button><button class="ev-tab-btn' + (S.tab === "orderbook" ? " active" : "") + '" data-vp-tab="orderbook">📖 ' + esc(t("event.tab.orderbook", "Order Book")) + '</button><button class="ev-tab-btn' + (S.tab === "history" ? " active" : "") + '" data-vp-tab="history">📜 ' + esc(t("event.tab.history", "Trade History")) + '</button><button class="ev-tab-btn' + (S.tab === "holders" ? " active" : "") + '" data-vp-tab="holders">👥 ' + esc(t("event.tab.holders", "Holders")) + '</button></div><div class="ev-section-body">' + tabHtml + "</div></div>" + aiHtml + descHtml + "</div>" + '<div class="ev-right-sticky"><div class="order-panel"><div class="op-balance-strip"><div class="bal"><span data-i18n="event.panel.tradableBalance">' + esc(t("event.panel.tradableBalance", "Tradable Balance")) + "</span><b>" + bal + '</b></div><div style="display:flex;gap:6px"><button class="v-btn v-btn-outline v-btn-sm" id="vp-ed-bal-refresh">↻</button><a href="/v2/deposit" class="v-btn v-btn-primary v-btn-sm">' + esc(t("event.panel.deposit", "Deposit")) + "</a></div></div>" + '<div class="op-head-row"><div class="op-tabs"><button class="op-tab' + (S.side === "BUY" ? " active" : "") + '" id="vp-ed-side-buy">' + esc(t("event.panel.tab.buy", "Buy")) + '</button><button class="op-tab' + (S.side === "SELL" ? " active" : "") + '" id="vp-ed-side-sell">' + esc(t("event.panel.tab.sell", "Sell")) + '</button></div><div class="op-otype-row"><select class="op-otype-select" id="vp-ed-otype"><option value="market"' + (S.orderType === "market" ? " selected" : "") + ">" + esc(t("event.panel.orderType.market", "Market")) + '</option><option value="limit"' + (S.orderType === "limit" ? " selected" : "") + ">" + esc(t("event.panel.orderType.limit", "Limit")) + "</option></select></div></div>" + '<div class="op-body"><div class="op-mkt-info">' + selectedTitle + '</div><div style="display:' + (S.lastPriceTs > 0 ? "flex" : "none") + ';align-items:center;gap:6px;font-size:.7rem;margin:4px 0;"><span style="color:' + (S.pricesStale ? "#f59e0b" : "#16a34a") + ';display:inline-flex;align-items:center;gap:3px;"><svg width="8" height="8" viewBox="0 0 8 8"><circle cx="4" cy="4" r="3" fill="currentColor"></circle></svg><span>' + esc(S.pricesStale ? t("event.panel.priceStale", "Price stale") : t("event.panel.realtime", "Live price")) + '</span></span><span style="color:var(--text-muted);" id="vp-ed-price-ts">' + (S.lastPriceTs ? "· " + new Date(S.lastPriceTs).toLocaleTimeString() : "") + '</span></div><div class="op-outcome-row"><button class="op-outcome-btn' + (S.outcome === "YES" ? " yes-active" : "") + '" id="vp-ed-out-yes"><span>' + esc(t("event.panel.outcome.yes", "Yes")) + '</span><span class="qb-out-px" id="vp-out-yes-px">' + esc(yesPx) + '</span></button><button class="op-outcome-btn' + (S.outcome === "NO" ? " no-active" : "") + '" id="vp-ed-out-no"><span>' + esc(t("event.panel.outcome.no", "No")) + '</span><span class="qb-out-px" id="vp-out-no-px">' + esc(noPx) + "</span></button></div>" + body + '<button class="op-submit" id="vp-ed-submit"' + (window.__pmGuestMode ? "" : S.submitting || !canSubmit() ? " disabled" : "") + "><span>" + esc(window.__pmGuestMode ? t("event.qb.signInToTrade", "Sign in to Trade") : S.submitting ? t("event.qb.submitting", "Submitting...") : submitLabel) + "</span></button>" + (S.submitMsg ? '<div class="op-msg ' + (S.submitOk ? "ok" : "err") + '">' + esc(S.submitMsg) + "</div>" : "") + "</div></div></div></div>";
    bindEvents(markets);
    if (S.tab === "chart" && !S.chartLoading && !S.chartError && Array.isArray(S.chartPoints) && S.chartPoints.length > 0) {
      S._chartPaintToken = (S._chartPaintToken || 0) + 1;
      var paintToken = S._chartPaintToken;
      requestAnimationFrame(function () {
        if (paintToken !== S._chartPaintToken) return;
        _loadChartJs().then(function () {
          if (paintToken !== S._chartPaintToken) return;
          _renderChartCanvas();
        }).catch(function () { });
      });
    } else {
      S._chartPaintToken = (S._chartPaintToken || 0) + 1;
    }
  }
  function bindEvents(markets) {
    document.querySelectorAll("[data-vp-tab]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        S.tab = btn.getAttribute("data-vp-tab") || "chart";
        render();
        loadActiveTab();
        requestAnimationFrame(function () {
          try {
            var sec = btn.closest(".ev-section-card");
            var body = sec ? sec.querySelector(".ev-section-body") : null;
            var anchor = body || sec;
            if (anchor && typeof anchor.scrollIntoView === "function") {
              anchor.scrollIntoView({
                block: "end",
                inline: "nearest",
                behavior: "smooth"
              });
            }
          } catch (_) { }
        });
      });
    });
    document.querySelectorAll("[data-vp-chart-out]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        S.chartOutcome = btn.getAttribute("data-vp-chart-out") || "YES";
        loadChart();
      });
    });
    document.querySelectorAll("[data-vp-chart-range]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        S.chartRange = btn.getAttribute("data-vp-chart-range") || "1d";
        loadChart();
      });
    });
    var obToggleBtn = document.getElementById("vp-ob-toggle");
    if (obToggleBtn) {
      obToggleBtn.addEventListener("click", function () {
        S.obShowAll = !S.obShowAll;
        render();
      });
    }
    var obOutYes = document.getElementById("vp-ob-out-yes");
    if (obOutYes) obOutYes.addEventListener("click", function () {
      setObOutcome("YES");
    });
    var obOutNo = document.getElementById("vp-ob-out-no");
    if (obOutNo) obOutNo.addEventListener("click", function () {
      setObOutcome("NO");
    });
    var shareCard = document.getElementById("vp-ed-share");
    if (shareCard) shareCard.addEventListener("click", function () {
      copyShare();
    });
    document.querySelectorAll("[data-vp-pos-mkt]").forEach(function (row) {
      row.addEventListener("click", function () {
        var mktId = row.getAttribute("data-vp-pos-mkt");
        var outLabel = row.getAttribute("data-vp-pos-out") || "YES";
        var m = markets.find(function (x) {
          return String(x.market_id) === String(mktId);
        });
        if (!m) return;
        selectMarket(m).then(function () {
          S.outcome = outLabel;
          setSide("SELL");
        });
      });
    });
    document.querySelectorAll("[data-vp-mkt-id]").forEach(function (el) {
      el.addEventListener("click", function (e) {
        if (e.target.closest("button")) return;
        var id = el.getAttribute("data-vp-mkt-id");
        var m = markets.find(function (x) {
          return String(x.market_id) === String(id);
        });
        if (m) selectMarket(m);
      });
    });
    document.querySelectorAll("[data-vp-quick-id]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.getAttribute("data-vp-quick-id");
        var out = btn.getAttribute("data-vp-quick-out") || "YES";
        var m = markets.find(function (x) {
          return String(x.market_id) === String(id);
        });
        if (!m) return;
        var wasNew = !S.selectedMarket || S.selectedMarketId !== m.market_id;
        if (wasNew) selectMarket(m).then(function () {
          setOutcome(out);
        }); else setOutcome(out);
      });
    });
    var sideBuy = document.getElementById("vp-ed-side-buy");
    var sideSell = document.getElementById("vp-ed-side-sell");
    var outYes = document.getElementById("vp-ed-out-yes");
    var outNo = document.getElementById("vp-ed-out-no");
    var otype = document.getElementById("vp-ed-otype");
    var refreshBal = document.getElementById("vp-ed-bal-refresh");
    var submitBtn = document.getElementById("vp-ed-submit");
    if (sideBuy) sideBuy.addEventListener("click", function () {
      setSide("BUY");
    });
    if (sideSell) sideSell.addEventListener("click", function () {
      setSide("SELL");
    });
    if (outYes) outYes.addEventListener("click", function () {
      setOutcome("YES");
    });
    if (outNo) outNo.addEventListener("click", function () {
      setOutcome("NO");
    });
    if (otype) otype.addEventListener("change", function () {
      S.orderType = otype.value;
      if (S.orderType === "limit" && S.amountUsdc > 0 && S.sharesInt === 0) S.sharesInt = Math.max(1, Math.floor(S.amountUsdc));
      render();
    });
    if (refreshBal) refreshBal.addEventListener("click", function () {
      refreshBalance();
    });
    var retryBal = document.getElementById("vp-ed-bal-retry");
    if (retryBal) retryBal.addEventListener("click", function () {
      refreshBalance();
    });
    if (submitBtn) submitBtn.addEventListener("click", function () {
      submitOrder();
    });
    var aMinus = document.getElementById("vp-ed-amt-minus");
    var aPlus = document.getElementById("vp-ed-amt-plus");
    var aInput = document.getElementById("vp-ed-amt-input");
    var aMax = document.getElementById("vp-ed-amt-max");
    if (aMinus) aMinus.addEventListener("click", function () {
      adjustAmount(-1);
    });
    if (aPlus) aPlus.addEventListener("click", function () {
      adjustAmount(1);
    });
    if (aInput) aInput.addEventListener("input", function () {
      var v = Math.floor(Number(aInput.value) || 0);
      if (v < Number(S.minBuyShares || 1)) v = Number(S.minBuyShares || 1);
      S.amountUsdc = v;
      S.maxActive = false;
      render();
    });
    if (aMax) aMax.addEventListener("click", function () {
      setMaxAmount();
    });
    document.querySelectorAll("[data-vp-amt]").forEach(function (b) {
      b.addEventListener("click", function () {
        S.amountUsdc = Math.max(Number(S.minBuyShares || 1), Math.floor(Number(b.getAttribute("data-vp-amt")) || 0));
        S.maxActive = false;
        render();
      });
    });
    var pMinus = document.getElementById("vp-ed-px-minus");
    var pPlus = document.getElementById("vp-ed-px-plus");
    var pInput = document.getElementById("vp-ed-px-input");
    if (pMinus) pMinus.addEventListener("click", function () {
      S.priceCents = Math.max(1, Math.min(99, Number(S.priceCents || 0) - 1));
      render();
    });
    if (pPlus) pPlus.addEventListener("click", function () {
      S.priceCents = Math.max(1, Math.min(99, Number(S.priceCents || 0) + 1));
      render();
    });
    if (pInput) pInput.addEventListener("input", function () {
      var v = parseInt(pInput.value, 10) || 0;
      if (v < 1) v = 1;
      if (v > 99) v = 99;
      S.priceCents = v;
      render();
    });
    var sMinus = document.getElementById("vp-ed-sh-minus");
    var sPlus = document.getElementById("vp-ed-sh-plus");
    var sInput = document.getElementById("vp-ed-sh-input");
    if (sMinus) sMinus.addEventListener("click", function () {
      adjustShares(-1);
    });
    if (sPlus) sPlus.addEventListener("click", function () {
      adjustShares(1);
    });
    if (sInput) sInput.addEventListener("input", function () {
      var v = parseFloat(sInput.value);
      if (!isFinite(v) || v < 0) v = 0;
      S.sharesInt = Number(v.toFixed(2));
      render();
    });
    document.querySelectorAll("[data-vp-sh]").forEach(function (b) {
      b.addEventListener("click", function () {
        var val = b.getAttribute("data-vp-sh");
        if (val === "max") {
          if (S.side === "SELL") {
            if (S.holdings != null) S.sharesInt = Number(S.holdings || 0);
          } else {
            setMaxShares();
            return;
          }
          render();
          return;
        }
        adjustShares(Number(val) || 0);
      });
    });
    document.querySelectorAll("[data-vp-ob-kind]").forEach(function (row) {
      row.addEventListener("click", function () {
        var kind = row.getAttribute("data-vp-ob-kind");
        var px = Number(row.getAttribute("data-vp-ob-price") || 0);
        if (!px) return;
        S.orderType = "limit";
        S.side = kind === "ask" ? "BUY" : "SELL";
        S.priceCents = Math.max(1, Math.min(99, Math.round(px * 100)));
        render();
      });
    });
    document.querySelectorAll("[data-vp-holders-out]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        S.holdersOutcome = S.holdersOutcome === "YES" ? "NO" : "YES";
        loadHolders();
      });
    });
  }
  function adjustAmount(delta) {
    var v = (Number(S.amountUsdc) || 0) + delta;
    if (v < Number(S.minBuyShares || 1)) v = Number(S.minBuyShares || 1);
    S.amountUsdc = v;
    S.maxActive = false;
    render();
  }
  function adjustShares(delta) {
    var step = S.side === "SELL" ? .01 : 1;
    var d = Math.abs(delta) === 1 ? delta * step : delta;
    S.sharesInt = Math.max(0, Number((Number(S.sharesInt || 0) + d).toFixed(2)));
    render();
  }
  function setMaxShares() {
    if (S.balance.usdc_balance == null || S.priceCents <= 0) return;
    S.sharesInt = Math.max(1, Math.floor(Number(S.balance.usdc_balance || 0) / (S.priceCents / 100)));
    render();
  }
  function setMaxAmount() {
    var px = Math.max(.01, Math.min(S.buyPriceCap || .99, effectivePrice() || .5));
    var capUsdc = Math.min(Number(S.balance.usdc_balance || 0), Number(S.maxOrder || 500));
    var maxShares = Math.floor(capUsdc / px);
    S.amountUsdc = Math.max(Number(S.minBuyShares || 1), maxShares);
    S.maxActive = true;
    render();
  }
  function setSide(s) {
    S.side = s;
    S.submitMsg = "";
    if (s === "SELL") {
      S.amountUsdc = 0;
      S.sharesInt = 0;
      refreshHoldings();
    } else {
      S.holdings = null;
      render();
    }
  }
  function setOutcome(o) {
    S.outcome = o;
    S.obOutcome = o;
    var prevChart = S.chartOutcome;
    var prevHolders = S.holdersOutcome;
    S.chartOutcome = o;
    S.holdersOutcome = o;
    if (S.selectedMarket) {
      var px = o === "YES" ? Number(S.selectedMarket.best_ask || .5) : Math.max(.01, Math.min(.99, 1 - Number(S.selectedMarket.best_bid || .5)));
      S.priceCents = Math.max(1, Math.min(99, Math.round((Number(px) || .5) * 100) || 50));
      fetchOrderbook();
      if (S.side === "SELL") refreshHoldings(); else render();
      if (S.tab === "orderbook") render(); else loadActiveTab();
      if (S.tab !== "chart" && prevChart !== o) loadChart();
      if (S.tab !== "holders" && prevHolders !== o) loadHolders();
    }
  }
  async function selectMarket(m) {
    S.selectedMarket = m;
    S.selectedMarketId = m.market_id;
    S.outcome = "YES";
    S.obOutcome = "YES";
    S.chartOutcome = "YES";
    S.holdersOutcome = "YES";
    S.side = "BUY";
    S.amountUsdc = 0;
    S.maxActive = false;
    S.sharesInt = 0;
    S.holdings = null;
    S.priceCents = Math.max(1, Math.min(99, Math.round((Number(m.best_ask) || .5) * 100) || 50));
    await fetchOrderbook();
    render();
    loadActiveTab();
    requestAnimationFrame(function () {
      try {
        var row = document.querySelector('[data-vp-mkt-id="' + String(m.market_id).replace(/"/g, '\\"') + '"]');
        if (row && typeof row.scrollIntoView === "function") {
          row.scrollIntoView({
            block: "center",
            inline: "nearest",
            behavior: "smooth"
          });
        }
      } catch (_) { }
    });
  }
  async function fetchOrderbook() {
    if (!S.selectedMarket) return;
    var tid = S.obOutcome === "YES" ? S.selectedMarket.yes_token_id : S.selectedMarket.no_token_id;
    if (!tid) return;
    try {
      var res = await fetch("/api/pm/orderbook?token_id=" + encodeURIComponent(tid), {
        credentials: "include"
      });
      var d = await res.json();
      if (!res.ok) return;
      S.orderbook = {
        bids: d.bids || [],
        asks: d.asks || [],
        best_bid: d.best_bid,
        best_ask: d.best_ask
      };
      var minOrder = Math.max(1, Math.ceil(Number(d.min_order_size || 1)));
      S.minBuyShares = minOrder + 1;
      if ((Number(S.amountUsdc) || 0) < S.minBuyShares) S.amountUsdc = S.minBuyShares;
    } catch (_) { }
  }
  async function setObOutcome(o) {
    S.obOutcome = o;
    await fetchOrderbook();
    render();
  }
  async function refreshBalance() {
    if (window.__pmGuestMode) {
      S.balanceLoading = false;
      S.balanceError = "not_logged_in";
      render();
      return;
    }
    S.balanceLoading = true;
    S.balanceError = false;
    try {
      var r = await fetch("/api/pm/balance-check", {
        credentials: "include"
      });
      var d = await r.json();
      if (r.status === 401 || r.status === 403) {
        S.balanceLoading = false;
        S.balanceError = "session_expired";
        render();
        return;
      }
      if (!r.ok) {
        S.balanceLoading = false;
        S.balanceError = d && d.error || "balance_fetch_failed";
        render();
        return;
      }
      S.balance = {
        usdc_balance: d.usdc_balance,
        thresholds: d.thresholds || {}
      };
      S.minTrade = d.thresholds && d.thresholds.min_usdc_to_trade || 1;
      if (d.thresholds) {
        if (d.thresholds.min_sell_share_unit != null) S.minSellShares = Number(d.thresholds.min_sell_share_unit) || .01;
        if (d.thresholds.buy_price_cap != null) S.buyPriceCap = Number(d.thresholds.buy_price_cap) || .99;
        if (d.thresholds.max_order_size_usdc != null) S.maxOrder = Number(d.thresholds.max_order_size_usdc) || S.maxOrder;
      }
      S.balanceLoading = false;
      S.balanceError = false;
      render();
    } catch (_e) {
      S.balanceLoading = false;
      S.balanceError = "network_error";
      render();
    }
  }
  async function refreshHoldings() {
    S.holdingsLoading = true;
    S.holdings = null;
    render();
    try {
      var tok = activeTokenId();
      if (!tok) return;
      var r = await fetch("/api/pm/user/holdings?token_id=" + encodeURIComponent(tok), {
        credentials: "include"
      });
      if (r.ok) {
        var d = await r.json();
        S.holdings = Number(d.shares || 0);
        if (S.holdings > 0 && S.sharesInt === 0) S.sharesInt = Number(S.holdings.toFixed(2));
      } else {
        S.holdings = 0;
      }
    } catch (_) {
      S.holdings = 0;
    } finally {
      S.holdingsLoading = false;
      render();
    }
  }
  async function submitOrder() {
    if (window.__pmGuestMode) {
      if (typeof window.__pmShowLoginRequired === "function") {
        window.__pmShowLoginRequired();
      } else {
        window.location.href = "/Index?next=" + encodeURIComponent(window.location.pathname + window.location.search);
      }
      return;
    }
    S.submitMsg = "";
    S.submitOk = false;
    if (!canSubmit()) {
      S.submitMsg = t("event.alert.checkInputs", "Please check amount, shares, price, balance and position");
      render();
      return;
    }
    if (S.side === "SELL") {
      await refreshHoldings();
      var real = Number(S.holdings || 0);
      var minS = Number(S.minSellShares || .01);
      if (real <= minS) {
        S.submitMsg = t("event.alert.holdingsClearedPrefix", "Holdings cleared (<= ") + minS + t("event.alert.holdingsClearedSuffix", " shares)");
        render();
        return;
      }
      if (Number(S.sharesInt) > real + 1e-6) S.sharesInt = real;
    }
    S.submitting = true;
    render();
    try {
      var body;
      if (S.orderType === "market") {
        if (S.side === "BUY") {
          var cap = Number(S.buyPriceCap || .99);
          var shares = Math.max(S.minBuyShares, Math.ceil(Number(S.amountUsdc || 0)));
          body = {
            token_id: activeTokenId(),
            side: "BUY",
            price: cap,
            size: shares,
            order_type: "FOK",
            condition_id: S.selectedMarket.condition_id,
            market_slug: S.selectedMarket.slug,
            event_slug: S.slug,
            market_question: S.selectedMarket.question
          };
        } else {
          body = {
            token_id: activeTokenId(),
            side: "SELL",
            price: .01,
            size: Number(S.sharesInt) || 0,
            order_type: "FOK",
            condition_id: S.selectedMarket.condition_id,
            market_slug: S.selectedMarket.slug,
            event_slug: S.slug,
            market_question: S.selectedMarket.question
          };
        }
      } else {
        body = {
          token_id: activeTokenId(),
          side: S.side,
          price: Number(S.priceCents) / 100,
          size: Number(S.sharesInt) || 0,
          order_type: "GTC",
          condition_id: S.selectedMarket.condition_id,
          market_slug: S.selectedMarket.slug,
          event_slug: S.slug,
          market_question: S.selectedMarket.question
        };
      }
      try {
        var _session = (window.veyraTrade && typeof window.veyraTrade.getSession === "function") ? window.veyraTrade.getSession() : null;
        if (_session && (await _session.isDepositWalletUser())) {
          var _sdkRes = await _session.submitOrder(body);
          try {
            await fetch("/api/pm/order/record", {
              method: "POST",
              credentials: "include",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                token_id: body.token_id,
                side: body.side,
                price: body.price,
                size: body.size,
                order_type: body.order_type,
                order_id: _sdkRes && _sdkRes.order_id || "",
                status: (_sdkRes && _sdkRes.status) || "submitted",
                error: "",
                raw: _sdkRes && _sdkRes.raw || null,
                condition_id: body.condition_id || "",
                market_slug: body.market_slug || "",
                event_slug: body.event_slug || "",
                market_question: body.market_question || ""
              })
            });
          } catch (_recErr) {  }
          S.submitOk = true;
          S.submitMsg = t("event.alert.submittedMsg", "Order submitted");
          (function _showOrderSuccessPopup(orderId) {
            var title = t("event.alert.success.title", "Order placed");
            var bodyText = t("event.alert.submittedMsg", "Order submitted")
              + (orderId ? ("\n\nOrder ID: " + orderId) : "");
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
            vpToast(title + (orderId ? (": " + orderId) : ""), "success");
          })(_sdkRes.order_id || "");
          await refreshBalance();
          if (S.side === "SELL") {
            S.sharesInt = 0;
            S.holdings = null;
          }
          S.submitting = false;
          render();
          return;
        }
      } catch (_sdkErr) {
        S.submitOk = false;
        S.submitMsg = String(_sdkErr && _sdkErr.message || _sdkErr);
        vpToast(t("event.alert.error.title", "Order failed") + ": " + S.submitMsg, "error");
        S.submitting = false;
        render();
        return;
      }
      var res = await fetch("/api/pm/trade/submit", {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(body)
      });
      var ct = (res.headers.get("content-type") || "").toLowerCase();
      var d = {};
      if (ct.indexOf("application/json") >= 0) {
        try {
          d = await res.json();
        } catch (_) {
          d = {};
        }
      } else {
        var txt = await res.text().catch(function () {
          return "";
        });
        d = {
          error: t("event.alert.upstreamError", "Upstream error HTTP ") + res.status + (txt ? " - " + txt.slice(0, 80) : "")
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
            error: t("event.alert.tradeAuthRequired", "Trade authorization required")
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
      if (!res.ok && d && d.error === "api_key_authorization_required") {
        var authResult = await authorizeApiKeyOnce(d);
        if (!authResult.ok) {
          throw new Error(authResult.error || t("event.alert.tradeAuthFailed", "Trade authorization failed"));
        }
        res = await fetch("/api/pm/trade/submit", {
          method: "POST",
          credentials: "include",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify(body)
        });
        ct = (res.headers.get("content-type") || "").toLowerCase();
        d = {};
        if (ct.indexOf("application/json") >= 0) {
          try {
            d = await res.json();
          } catch (_) {
            d = {};
          }
        }
      }
      if (res.status === 409 && d && d.status === "client_signing_required") {
        var provOk = await window.veyraEnsureDepositApprovals(t);
        if (provOk && provOk.error) {
          throw new Error(provOk.error);
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
            throw new Error(prepAuth.error || t("event.alert.tradeAuthFailed", "Trade authorization failed"));
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
          throw new Error(prepData && (prepData.error || prepData.raw_error) || t("event.alert.tradePrepareFailed", "Trade prepare failed"));
        }
        for (var waitI = 0; waitI < 40; waitI++) {
          if (typeof window.veyraPrivySignTypedData === "function") break;
          await new Promise(function (resolve) {
            setTimeout(resolve, 100);
          });
        }
        if (typeof window.veyraPrivySignTypedData !== "function") {
          throw new Error(t("pm.trade.privyUnavailable", "Privy embedded wallet is not available in this session. Please re-login and retry."));
        }
        var signature = await window.veyraPrivySignTypedData(prepData.typed_data, {
          expectedSigner: String(prepData.signer_eoa || "").toLowerCase()
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
        if (signedCt.indexOf("application/json") >= 0) {
          try {
            signedData = await signedRes.json();
          } catch (_) {
            signedData = {};
          }
        }
        if (!signedRes.ok) {
          throw new Error(signedData && (signedData.error || signedData.raw_error) || "HTTP " + signedRes.status);
        }
        d = signedData;
      } else if (!res.ok) {
        throw new Error(d.error || "HTTP " + res.status);
      }
      S.submitOk = true;
      S.submitMsg = t("event.alert.submittedMsg", "Order submitted");
      vpToast(t("event.alert.success.title", "Order placed"), "success");
      await refreshBalance();
      if (S.side === "SELL") {
        S.sharesInt = 0;
        S.holdings = null;
      }
    } catch (e) {
      S.submitOk = false;
      S.submitMsg = String(e && e.message || e);
      vpToast(t("event.alert.error.title", "Order failed") + ": " + S.submitMsg, "error");
    } finally {
      S.submitting = false;
      render();
    }
  }
  async function fetchEvent() {
    try {
      var url = "/api/pm/event/" + encodeURIComponent(S.slug);
      try {
        var lang = localStorage.getItem("veyra_lang") || "en";
        url += "?lang=" + encodeURIComponent(lang);
      } catch (_) { }
      var res = await fetch(url, {
        credentials: "include"
      });
      var data = await res.json();
      if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
      S.event = data.event || null;
      S.error = "";
      var markets = tradableMarkets(S.event);
      var selectedLoadedBySelect = false;
      if (markets.length > 0) {
        if (!S.selectedMarket) {
          await selectMarket(markets[0]);
          selectedLoadedBySelect = true;
        } else {
          var currentId = String(S.selectedMarket.market_id || "");
          var freshSelected = markets.find(function (m) {
            return String(m.market_id || "") === currentId;
          }) || null;
          if (!freshSelected) {
            await selectMarket(markets[0]);
            selectedLoadedBySelect = true;
          } else {
            S.selectedMarket = freshSelected;
            S.selectedMarketId = freshSelected.market_id;
          }
        }
      } else {
        S.selectedMarket = null;
        S.selectedMarketId = "";
      }
      if (!selectedLoadedBySelect && S.selectedMarket) {
        loadActiveTab();
      }
      _setupRealtimePrices();
    } catch (e) {
      S.error = String(e && e.message || e);
    }
    render();
  }
  async function fetchReferralCode() {
    if (window.__pmGuestMode) return;
    try {
      var res = await fetch("/api/user/referral", {
        credentials: "include",
        cache: "no-store"
      });
      var data = await res.json();
      if (!res.ok) return;
      var code = String(data && (data.code || data.referral_code) || "").trim();
      if (code) S.referralCode = code;
    } catch (_) { }
  }
  async function init() {
    var r = root();
    if (!r) return;
    if (window.veyraI18n && typeof window.veyraI18n.registerPagePack === 'function') {
      try { await window.veyraI18n.registerPagePack('event_detail'); } catch (_) { }
    }
    if (!S._i18nBound) {
      S._i18nBound = true;
      document.addEventListener("i18n:changed", function () {
        fetchEvent();
      });
    }
    await fetchEvent();
    setTimeout(function () {
      fetchReferralCode();
    }, 60);
    setTimeout(function () {
      refreshBalance();
    }, 120);
    setTimeout(function () {
      fetchUserPositions();
    }, 500);
  }
  function patchRealtimeDom() {
    if (!S.event || !Array.isArray(S.event.markets)) return;
    for (var i = 0; i < S.event.markets.length; i++) {
      var m = S.event.markets[i];
      if (!m || !m.market_id) continue;
      var id = String(m.market_id);
      var yesEl = document.querySelector('[data-vp-mkt-yes="' + id + '"]');
      if (yesEl) yesEl.textContent = yesCellPx(m);
      var noEl = document.querySelector('[data-vp-mkt-no="' + id + '"]');
      if (noEl) noEl.textContent = noCellPx(m);
      var ypEl = document.querySelector('[data-vp-mkt-yesp="' + id + '"]');
      if (ypEl) ypEl.textContent = (yesProbOf(m) * 100).toFixed(1);
    }
    if (S.selectedMarket) {
      var _isSell = S.side === "SELL";
      var yesPx = document.getElementById("vp-out-yes-px");
      if (yesPx) yesPx.textContent = _isSell ? yesCellSellPx(S.selectedMarket) : yesCellPx(S.selectedMarket);
      var noPx = document.getElementById("vp-out-no-px");
      if (noPx) noPx.textContent = _isSell ? noCellSellPx(S.selectedMarket) : noCellPx(S.selectedMarket);
    }
    var tsEl = document.getElementById("vp-ed-price-ts");
    if (tsEl) {
      tsEl.textContent = S.lastPriceTs ? "· " + new Date(S.lastPriceTs).toLocaleTimeString() : "";
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
