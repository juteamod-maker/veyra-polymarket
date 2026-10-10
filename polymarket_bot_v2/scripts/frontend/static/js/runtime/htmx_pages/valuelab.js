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
  function recText(rec) {
    var t = String(rec || "observe");
    if (t === "strong") return "Strong";
    if (t === "watch") return "Watch";
    if (t === "avoid") return "Avoid";
    return "Observe";
  }
  function buildSkeletonHtml(count) {
    var n = Math.max(3, Number(count || 9));
    var out = [];
    for (var i = 0; i < n; i += 1) {
      out.push('<article class="vl-skeleton-card" aria-hidden="true">' + '<div style="display:flex;justify-content:space-between;gap:8px;align-items:center">' + '  <div class="vl-sk-line vl-sk-head"></div>' + '  <div class="vl-sk-line vl-sk-badge"></div>' + "</div>" + '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">' + '  <div class="vl-sk-line vl-sk-metric"></div>' + '  <div class="vl-sk-line vl-sk-metric"></div>' + '  <div class="vl-sk-line vl-sk-metric"></div>' + "</div>" + '<div class="vl-sk-line vl-sk-cta"></div>' + "</article>");
    }
    return out.join("");
  }
  function setLoading(slot, loading) {
    if (!slot) return;
    if (loading) {
      slot.classList.remove("ready");
      slot.classList.add("loading");
    } else {
      slot.classList.remove("loading");
      slot.classList.add("ready");
    }
  }
  function cardHtml(it) {
    var score = Number(it.value_score || 0);
    var ev = Number(it.ev_pct || 0);
    var implied = Number(it.implied_prob || .5) * 100;
    var scoreClass = score >= 80 ? "is-high" : score < 45 ? "is-low" : "is-neutral";
    var evClass = ev >= 0 ? "is-pos" : "is-neg";
    var rec = String(it.recommendation || "observe");
    var parentTitle = String(it.parent_title || "");
    var href = "/valuelab/" + esc(it.slug || "") + "?analysis_id=" + esc(it.analysis_id || "");
    return "" + '<a class="vl-card ' + scoreClass + '" href="' + href + '">' + '  <div class="vl-card-head"><h3>' + esc(it.title || it.slug || "?") + '</h3><span class="vl-rec vl-rec-' + esc(rec) + '">' + esc(recText(rec)) + "</span></div>" + (parentTitle ? '<div class="vl-card-sub">' + esc(parentTitle) + "</div>" : "") + '  <div class="vl-metrics">' + "    <div><span>Implied Prob</span><b>" + implied.toFixed(1) + "%</b></div>" + "    <div><span>Value Score</span><b>" + score.toFixed(1) + " ± " + Number(it.score_ci || 0).toFixed(1) + "</b></div>" + '    <div><span>+EV</span><b class="' + evClass + '">' + (ev >= 0 ? "+" : "") + ev.toFixed(2) + "%</b></div>" + "  </div>" + "</a>";
  }
  function renderList(slot, items, loading, hasMore, labels) {
    if (!slot) return;
    if (!items || !items.length) {
      slot.innerHTML = '<div class="vl-empty">' + esc(labels && labels.noMatch || "No events match current filters.") + "</div>";
      setLoading(slot, false);
      return;
    }
    var html = items.map(cardHtml).join("");
    if (hasMore || loading) {
      html += "" + '<div class="vl-scroll-sentinel" id="vl-scroll-sentinel">' + '  <span class="vl-loading-pill" role="status" aria-live="polite">' + '    <span class="vl-loading-dot"></span>' + "    <span>" + esc(labels && labels.loadingMore || "正在载入更多内容…") + "</span>" + "  </span>" + "</div>";
    } else {
      html += "" + '<div class="vl-scroll-sentinel">' + '  <span class="vl-loading-pill" role="status" aria-live="polite">' + "    <span>" + esc(labels && labels.allLoaded || "已显示全部内容") + "</span>" + "  </span>" + "</div>";
    }
    slot.innerHTML = html;
    setLoading(slot, false);
  }
  function readSsr() {
    var n = byId("vl-ssr-json");
    if (!n) return null;
    var txt = (n.textContent || "").trim();
    if (!txt) return null;
    try {
      return JSON.parse(txt);
    } catch (_) {
      return null;
    }
  }
  function updateHealthBox(box, data, labels) {
    if (!box || !data) return;
    var grade = (data.data_health || {}).freshness_grade || data.freshness_grade || "D";
    var lag = Number((data.data_health || {}).max_lag_sec || data.max_lag_sec || 0);
    var cls = grade === "A" ? "#34d399" : grade === "B" ? "#93c5fd" : grade === "C" ? "#f59e0b" : "#f87171";
    box.innerHTML = '<b style="color:' + cls + '">' + esc(labels && labels.healthPrefix || "Data freshness") + " " + esc(grade) + "</b> · " + esc(labels && labels.healthLag || "max lag") + ": " + lag + "s";
  }
  function init() {
    var app = byId("vl-app");
    if (!app) return;
    if (window.veyraI18n && typeof window.veyraI18n.registerPagePack === 'function') {
      window.veyraI18n.registerPagePack('valuelab').catch(function () { });
    }
    var slot = byId("vl-list-slot");
    var tabs = byId("vl-tabs");
    var search = byId("vl-search");
    var sortSel = byId("vl-sort");
    var healthBox = byId("vl-health");
    var labels = {};
    try {
      labels = JSON.parse((byId("vl-labels") || {}).textContent || "{}");
    } catch (_) {
      labels = {};
    }
    var state = {
      tab: "crypto",
      q: "",
      sort: "score",
      order: "desc",
      page: 1,
      hasMore: true,
      loading: false,
      items: []
    };
    var io = null;
    var ssr = readSsr();
    var hasSsrItems = !!(ssr && Array.isArray(ssr.items) && ssr.items.length);
    if (ssr && slot && Array.isArray(ssr.items) && ssr.items.length) {
      state.items = ssr.items.slice();
      state.page = Number(ssr.next_page || 2);
      state.hasMore = !!ssr.has_more;
      renderList(slot, state.items, false, state.hasMore, labels);
      updateHealthBox(healthBox, ssr, labels);
    } else if (slot) {
      slot.innerHTML = buildSkeletonHtml(9);
      setLoading(slot, true);
    }
    function buildUrl(page) {
      var qs = new URLSearchParams;
      qs.set("tab", state.tab);
      if (state.q) qs.set("q", state.q);
      qs.set("sort", state.sort);
      qs.set("order", state.order);
      qs.set("page", String(page || 1));
      qs.set("page_size", "18");
      return "/api/valuelab/events?" + qs.toString();
    }
    function disconnectObserver() {
      if (!io) return;
      try {
        io.disconnect();
      } catch (_) { }
      io = null;
    }
    function setupObserver() {
      disconnectObserver();
      if (!slot || !state.hasMore) return;
      var sentinel = byId("vl-scroll-sentinel");
      if (!sentinel) return;
      io = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          if (state.loading || !state.hasMore) return;
          fetchPage(state.page, true);
        });
      }, {
        rootMargin: "900px 0px"
      });
      io.observe(sentinel);
    }
    function fetchPage(page, append) {
      if (state.loading) return;
      state.loading = true;
      if (append && slot) {
        renderList(slot, state.items, true, state.hasMore, labels);
      }
      fetch(buildUrl(page), {
        credentials: "include",
        cache: "no-store"
      }).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (d) {
        if (!d) return;
        var incoming = Array.isArray(d.items) ? d.items : [];
        state.items = append ? state.items.concat(incoming) : incoming;
        state.hasMore = !!d.has_more;
        state.page = Number(d.next_page || Number(page || 1) + 1);
        if (slot) renderList(slot, state.items, false, state.hasMore, labels);
        updateHealthBox(healthBox, d, labels);
        setupObserver();
      }).catch(function () {
        if (slot) renderList(slot, state.items, false, false, labels);
      }).finally(function () {
        state.loading = false;
      });
    }
    function resetAndLoad() {
      state.page = 1;
      state.hasMore = true;
      state.items = [];
      state.loading = false;
      disconnectObserver();
      if (slot) {
        slot.innerHTML = buildSkeletonHtml(9);
        setLoading(slot, true);
      }
      fetchPage(1, false);
      fetch("/api/valuelab/sources/health", {
        credentials: "include",
        cache: "no-store"
      }).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (d) {
        if (d && d.ok) updateHealthBox(healthBox, d, labels);
      }).catch(function () { });
    }
    if (tabs) {
      tabs.addEventListener("click", function (e) {
        var btn = e.target && e.target.closest ? e.target.closest(".vl-tab") : null;
        if (!btn) return;
        state.tab = String(btn.getAttribute("data-tab") || "all");
        var all = tabs.querySelectorAll(".vl-tab");
        for (var i = 0; i < all.length; i += 1) all[i].classList.remove("active");
        btn.classList.add("active");
        resetAndLoad();
      });
    }
    if (search) {
      var timer = 0;
      search.addEventListener("input", function () {
        clearTimeout(timer);
        timer = setTimeout(function () {
          state.q = String(search.value || "").trim();
          resetAndLoad();
        }, 220);
      });
    }
    if (sortSel) {
      sortSel.addEventListener("change", function () {
        state.sort = String(sortSel.value || "score");
        resetAndLoad();
      });
    }
    if (!hasSsrItems) {
      resetAndLoad();
    } else {
      fetch("/api/valuelab/sources/health", {
        credentials: "include",
        cache: "no-store"
      }).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (d) {
        if (d && d.ok) updateHealthBox(healthBox, d, labels);
      }).catch(function () { });
      setupObserver();
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, {
      once: true
    });
  } else {
    init();
  }
})();