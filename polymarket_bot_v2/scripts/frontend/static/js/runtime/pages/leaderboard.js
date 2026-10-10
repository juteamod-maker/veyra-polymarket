(function() {
  "use strict";
  function ready(fn) {
    if (document.readyState !== "loading") return fn();
    document.addEventListener("DOMContentLoaded", fn, {
      once: true
    });
  }
  function escapeHTML(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function(c) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[c];
    });
  }
  function formatInt(v) {
    var n = Number(v) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  }
  function formatCoin(v) {
    return "🪙 " + formatInt(v);
  }
  function relativeJoin(iso, labels) {
    if (!iso) return "";
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var days = Math.floor((Date.now() - t) / 864e5);
    if (days < 1) return labels && labels.joinedToday ? labels.joinedToday : "today";
    if (days < 30) return days + (labels && labels.joinedDayUnit ? labels.joinedDayUnit : "d");
    if (days < 365) return Math.floor(days / 30) + (labels && labels.joinedMonthUnit ? labels.joinedMonthUnit : "mo");
    return Math.floor(days / 365) + (labels && labels.joinedYearUnit ? labels.joinedYearUnit : "y");
  }
  function rankClass(idx) {
    if (idx === 0) return "lb-rank lb-rank-top";
    if (idx === 1) return "lb-rank lb-rank-top lb-rank-2";
    if (idx === 2) return "lb-rank lb-rank-top lb-rank-3";
    return "lb-rank";
  }
  function avatarColor(seed) {
    var text = String(seed || "veyra-user");
    var hash = 0;
    for (var i = 0; i < text.length; i += 1) {
      hash = (hash << 5) - hash + text.charCodeAt(i);
      hash |= 0;
    }
    var hue = Math.abs(hash % 360);
    return "hsl(" + hue + " 64% 45%)";
  }
  function rowClass(idx) {
    if (idx === 0) return "lb-row lb-top1";
    if (idx === 1) return "lb-row lb-top2";
    if (idx === 2) return "lb-row lb-top3";
    return "lb-row";
  }
  ready(function() {
    var labelsNode = document.getElementById("lb-labels-data");
    var listEl = document.getElementById("lb-list");
    var statsUsersEl = document.getElementById("lb-stat-users");
    var statsBalanceEl = document.getElementById("lb-stat-balance");
    var statsSpentEl = document.getElementById("lb-stat-spent");
    var loadingEl = document.getElementById("lb-loading");
    if (!listEl || !labelsNode) return;
    var labels = {};
    try {
      labels = JSON.parse(labelsNode.textContent || "{}");
    } catch (_) {
      labels = {};
    }
    function fillStats(t) {
      if (statsUsersEl) statsUsersEl.textContent = (t.users || 0).toLocaleString();
      if (statsBalanceEl) statsBalanceEl.textContent = formatCoin(t.total_balance || 0);
      if (statsSpentEl) statsSpentEl.textContent = formatCoin(t.total_spent || 0);
    }
    function renderEmpty(msg) {
      listEl.innerHTML = '<div class="lb-empty"><div class="lb-empty-emoji">🏅</div><div>' + escapeHTML(msg || labels.empty || "No leaderboard data yet.") + "</div></div>";
    }
    function renderError(msg) {
      listEl.innerHTML = '<div class="lb-empty"><div class="lb-empty-emoji">⚠️</div><div>' + escapeHTML(msg || labels.error || "Failed to load leaderboard.") + "</div></div>";
    }
    function renderRows(items) {
      if (!items || !items.length) {
        renderEmpty();
        return;
      }
      listEl.innerHTML = items.map(function(u, idx) {
        var displayName = String(u.name || u.wallet_short || u.id || "?");
        var initial = escapeHTML(u.initial || displayName.charAt(0).toUpperCase());
        var avatarHTML = '<div class="lb-avatar" style="background:' + avatarColor(u.id || displayName) + '">' + initial + "</div>";
        var joined = relativeJoin(u.joined_at, labels);
        var subParts = [];
        if (joined) subParts.push(escapeHTML(labels.joined || "joined") + " " + escapeHTML(joined));
        return "" + '<div class="' + rowClass(idx) + '" data-uid="' + escapeHTML(u.id) + '">' + '<div><span class="' + rankClass(idx) + '">' + (u.rank || idx + 1) + "</span></div>" + '<div class="lb-user">' + avatarHTML + '<div class="lb-uname">' + '<div class="lb-uname-main">' + escapeHTML(displayName) + "</div>" + '<div class="lb-uname-sub">' + subParts.join(" · ") + "</div>" + "</div>" + "</div>" + '<div class="lb-metrics">' + '<div class="lb-metric"><div class="lb-m-v lb-pos">' + escapeHTML(formatCoin(u.balance)) + '</div><div class="lb-m-k">' + escapeHTML(labels.balance || "Points") + "</div></div>" + '<div class="lb-metric"><div class="lb-m-v">*****</div><div class="lb-m-k">' + escapeHTML(labels.spent || "Spent") + "</div></div>" + "</div>" + "</div>";
      }).join("");
    }
    function load() {
      if (loadingEl) loadingEl.style.display = "";
      fetch("/api/leaderboard/points?limit=50&_=" + Date.now(), {
        credentials: "include",
        cache: "no-store",
        headers: {
          Accept: "application/json"
        }
      }).then(function(r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }).then(function(d) {
        if (loadingEl) loadingEl.style.display = "none";
        if (!d || !d.ok) {
          renderError(d && d.error);
          return;
        }
        fillStats(d.totals || {});
        renderRows(d.items || []);
      }).catch(function(e) {
        if (loadingEl) loadingEl.style.display = "none";
        renderError(String(e && e.message || e));
      });
    }
    var refreshBtn = document.getElementById("lb-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", function() {
      load();
    });
    load();
  });
})();