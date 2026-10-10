(function () {
  "use strict";
  function byId(id) {
    return document.getElementById(id);
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[c];
    });
  }
  function num(v, d) {
    var n = Number(v || 0);
    return n.toFixed(Number(d == null ? 2 : d));
  }
  function pct(v) {
    return (Number(v || 0) * 100).toFixed(2) + "%";
  }
  function pp(v) {
    var n = Number(v || 0) * 100;
    return (n >= 0 ? "+" : "") + n.toFixed(2) + "pp";
  }
  function readJsonNode(id) {
    var n = byId(id);
    if (!n) return null;
    var txt = (n.textContent || "").trim();
    if (!txt) return null;
    try {
      return JSON.parse(txt);
    } catch (_) {
      return null;
    }
  }
  function setBar(id, valuePct, textId, textValue) {
    var el = byId(id);
    if (el) el.style.width = Math.max(0, Math.min(100, Number(valuePct || 0))) + "%";
    var txt = byId(textId);
    if (txt) txt.textContent = textValue || "--";
  }
  function describeDriver(score, labels) {
    var s = Number(score || 0);
    if (s >= 72) return labels.driverBiasPos || "Supports current thesis";
    if (s >= 52) return labels.driverBiasMid || "Neutral impact";
    return labels.driverBiasNeg || "Potential drag on thesis";
  }
  function driverTitle(key) {
    var m = {
      valuation: "Valuation Stretch",
      momentum: "Momentum Pulse",
      sentiment: "Sentiment Temperature",
      funding: "Funding Pressure",
      liquidity: "Liquidity Depth",
      volume: "Trading Activity",
      macro_link: "Macro Linkage",
      probability_structure: "Probability Structure",
      macro_surprise: "Macro Surprise",
      policy_path: "Policy Path",
      risk_appetite: "Risk Appetite",
      crowding: "Crowding Risk",
      history_similarity: "History Similarity",
      event_window: "Event Timing"
    };
    return m[key] || String(key || "").replace(/_/g, " ");
  }
  function buildDigest(summary, labels) {
    var edge = Number(summary.fair_prob || 0) - Number(summary.implied_prob || 0);
    var edgeStr = pp(edge);
    var rec = String(summary.recommendation || "observe");
    var tpl;
    if (rec === "strong") {
      tpl = labels && labels.digestStrong || "Current pricing appears to discount this event too much. Model fair value is above market by {edge}, creating a meaningful but still risky entry window.";
    } else if (rec === "watch") {
      tpl = labels && labels.digestWatch || "There is a modest valuation gap of {edge}. This is a watchlist-quality setup: interesting, but not a high-conviction all-in signal.";
    } else if (rec === "avoid") {
      tpl = labels && labels.digestAvoid || "Model fair value sits below current pricing by {edge}. Risk-reward is currently unfavorable for new entries.";
    } else {
      tpl = labels && labels.digestObserve || "Market pricing and model fair value are close ({edge}). The edge is thin, so patience and selective timing matter more than speed.";
    }
    return tpl.replace("{edge}", edgeStr);
  }
  function renderDrivers(node, dims, labels) {
    if (!node) return;
    var arr = Array.isArray(dims) ? dims.slice() : [];
    arr.sort(function (a, b) {
      return Number(b.score || 0) - Number(a.score || 0);
    });
    arr = arr.slice(0, 4);
    node.innerHTML = arr.map(function (d) {
      return "" + '<div class="vld-driver">' + '  <div class="k">' + esc(driverTitle(d.key)) + "</div>" + '  <div class="v">' + num(d.score, 1) + "/100</div>" + '  <div class="d">' + esc(describeDriver(d.score, labels)) + "</div>" + "</div>";
    }).join("");
  }
  function renderScenarios(node, summary, labels) {
    if (!node) return;
    var fair = Number(summary.fair_prob || .5);
    var ci = Number(summary.fair_prob_ci || 3) / 100;
    var bull = Math.min(.99, fair + ci);
    var base = fair;
    var bear = Math.max(.01, fair - ci);
    var items = [{
      title: labels.scenarioBull || "Upside Case",
      prob: bull,
      text: labels.scenarioBullText || "If supportive catalysts continue, probability may drift toward the upper bound and reward early scaled entries."
    }, {
      title: labels.scenarioBase || "Base Case",
      prob: base,
      text: labels.scenarioBaseText || "Most likely path under current data. Use this as primary anchor for position sizing."
    }, {
      title: labels.scenarioBear || "Defensive Case",
      prob: bear,
      text: labels.scenarioBearText || "If risk events hit, fair value can move lower. Keep this scenario in mind for stop decisions."
    }];
    node.innerHTML = items.map(function (it) {
      return "" + '<div class="vld-scenario">' + "  <h3>" + esc(it.title) + "</h3>" + "  <p><b>" + pct(it.prob) + "</b></p>" + "  <p>" + esc(it.text) + "</p>" + "</div>";
    }).join("");
  }
  function renderBacktest(node, b30, b90, labels) {
    if (!node) return;
    node.innerHTML = "" + "<article>" + "  <h3>" + esc(labels.back30 || "30d Validation Window") + "</h3>" + "  <p>" + esc(labels.backtestHitRate || "Hit rate") + ": <b>" + num((b30 || {}).hit_rate, 2) + "%</b></p>" + "  <p>" + esc(labels.backtestAvgEv || "Average EV") + ": <b>" + num((b30 || {}).avg_ev, 2) + "%</b></p>" + "  <p>" + esc(labels.backtestSample || "Sample size") + ": <b>" + num((b30 || {}).sample_size, 0) + "</b></p>" + "</article>" + "<article>" + "  <h3>" + esc(labels.back90 || "90d Stability Window") + "</h3>" + "  <p>" + esc(labels.backtestHitRate || "Hit rate") + ": <b>" + num((b90 || {}).hit_rate, 2) + "%</b></p>" + "  <p>" + esc(labels.backtestAvgEv || "Average EV") + ": <b>" + num((b90 || {}).avg_ev, 2) + "%</b></p>" + "  <p>" + esc(labels.backtestDrawdown || "Drawdown proxy") + ": <b>" + num((b90 || {}).max_drawdown_proxy, 2) + "%</b></p>" + "</article>";
  }
  function renderRisks(node, labels) {
    if (!node) return;
    node.innerHTML = "" + '<div class="vld-risk-item"><b>' + esc(labels.riskTitle1 || "Model Risk") + "</b><p>" + esc(labels.riskText1 || "Any model can be wrong during regime shifts. Keep risk budget fixed.") + "</p></div>" + '<div class="vld-risk-item"><b>' + esc(labels.riskTitle2 || "Liquidity Risk") + "</b><p>" + esc(labels.riskText2 || "When depth is thin, price can jump quickly and increase slippage.") + "</p></div>" + '<div class="vld-risk-item"><b>' + esc(labels.riskTitle3 || "Event Timing Risk") + "</b><p>" + esc(labels.riskText3 || "Unexpected news can invalidate the thesis before your next review window.") + "</p></div>";
  }
  function renderChecklist(node, labels) {
    if (!node) return;
    node.innerHTML = "" + "<li><b>1.</b> " + esc(labels.check1 || "Position size: start small (for example 1%-3% of bankroll), add only after validation.") + "</li>" + "<li><b>2.</b> " + esc(labels.check2 || "Recheck trigger: if market probability moves beyond model band, pause and reassess.") + "</li>" + "<li><b>3.</b> " + esc(labels.check3 || "Execution rule: avoid all-in entries; use staged entries and predefined exit points.") + "</li>";
  }
  function render(payload, labels) {
    var s = (payload || {}).summary || {};
    if (!Object.keys(s).length) return;
    var title = byId("vld-title");
    var digest = byId("vld-digest");
    var score = byId("vld-score");
    var implied = byId("vld-implied");
    var fair = byId("vld-fair");
    var ev = byId("vld-ev");
    var whyIntro = byId("vld-why-intro");
    var bet = byId("vld-bet");
    if (title) title.textContent = String(s.title || s.slug || "Value Lab Analysis");
    if (digest) digest.textContent = buildDigest(s, labels);
    if (score) score.textContent = num(s.value_score, 2) + " ± " + num(s.score_ci, 2);
    if (implied) implied.textContent = pct(s.implied_prob);
    if (fair) fair.textContent = pct(s.fair_prob) + " ± " + num(s.fair_prob_ci, 2) + "%";
    if (ev) {
      var edge = Number(s.ev_approx || 0);
      ev.textContent = (edge >= 0 ? "+" : "") + num(edge, 2) + "%";
      ev.classList.remove("vld-pos", "vld-neg");
      ev.classList.add(edge >= 0 ? "vld-pos" : "vld-neg");
    }
    if (whyIntro) whyIntro.textContent = labels.whyIntro || "We compare current market pricing with model fair value, then convert that gap into beginner-friendly action guidance.";
    if (bet) bet.href = String(s.bet_url || "#");
    var impliedPct = Number(s.implied_prob || .5) * 100;
    var fairPct = Number(s.fair_prob || .5) * 100;
    var edgePp = fairPct - impliedPct;
    setBar("vld-bar-implied", impliedPct, "vld-bar-implied-text", impliedPct.toFixed(2) + "%");
    setBar("vld-bar-fair", fairPct, "vld-bar-fair-text", fairPct.toFixed(2) + "%");
    setBar("vld-bar-edge", Math.min(100, Math.abs(edgePp) * 2.5), "vld-bar-edge-text", (edgePp >= 0 ? "+" : "") + edgePp.toFixed(2) + "pp");
    renderChecklist(byId("vld-checklist"), labels);
    renderDrivers(byId("vld-drivers"), payload.dimensions || [], labels);
    renderScenarios(byId("vld-scenarios"), s, labels);
    renderBacktest(byId("vld-backtest"), payload.backtest_30d || {}, payload.backtest_90d || {}, labels);
    renderRisks(byId("vld-risks"), labels);
  }
  function loadAiInsight(slug, labels) {
    var section = byId("vld-ai-section");
    var loading = byId("vld-ai-loading");
    var textEl = byId("vld-ai-text");
    var paywall = byId("vld-ai-paywall");
    if (!section) return;
    fetch("/api/valuelab/event/" + encodeURIComponent(slug) + "/ai-insight", {
      credentials: "include",
      cache: "no-store"
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      if (!d) {
        if (loading) loading.textContent = labels.aiError || "AI analysis unavailable.";
        return;
      }
      if (loading) loading.style.display = "none";
      if (d.paywall) {
        if (paywall) paywall.style.display = "";
      } else if (d.text) {
        if (textEl) {
          textEl.textContent = d.text;
          textEl.style.display = "";
        }
      } else if (d.ai_degraded) {
        if (textEl) {
          textEl.textContent = labels.aiError || "AI analysis unavailable.";
          textEl.style.display = "";
        }
      } else {
        if (loading) {
          loading.textContent = labels.aiError || "AI analysis unavailable.";
          loading.style.display = "";
        }
      }
    }).catch(function () {
      if (loading) loading.textContent = labels.aiError || "AI analysis unavailable.";
    });
  }
  function init() {
    var app = byId("vld-app");
    if (!app) return;
    if (window.veyraI18n && typeof window.veyraI18n.registerPagePack === 'function') {
      window.veyraI18n.registerPagePack('valuelab').catch(function () { });
    }
    var labels = readJsonNode("vld-labels") || {};
    var ssr = readJsonNode("vld-ssr-json");
    var slug = String(app.getAttribute("data-slug") || "");
    if (ssr) {
      render(ssr, labels);
      if (slug) loadAiInsight(slug, labels);
      return;
    }
    if (!slug) return;
    fetch("/api/valuelab/event/" + encodeURIComponent(slug) + "/analysis", {
      credentials: "include",
      cache: "no-store"
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      if (d && d.ok) {
        render(d, labels);
        loadAiInsight(slug, labels);
      }
    }).catch(function () { });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, {
      once: true
    });
  } else {
    init();
  }
})();