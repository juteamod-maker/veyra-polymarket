(function () {
  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn, {
        once: true
      });
      return;
    }
    fn();
  }
  function t(key, fallback) {
    try {
      if (window.veyraI18n && typeof window.veyraI18n.t === "function") {
        return window.veyraI18n.t(key, fallback);
      }
    } catch (_e) { }
    return fallback;
  }
  function fmtNum(v, dec) {
    var n = parseFloat(v);
    if (isNaN(n)) return "-";
    var digits = typeof dec === "number" ? dec : 2;
    return n.toFixed(digits).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function fmtCoin(v) {
    var n = parseFloat(v);
    if (isNaN(n)) return "-";
    return "🪙 " + n.toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function fmtPoints(v) {
    var n = parseFloat(v);
    if (isNaN(n)) return "🪙 0";
    return "🪙 " + n.toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function fmtDate(v) {
    if (!v) return "-";
    try {
      if (typeof window.fmtDate === "function") return window.fmtDate(v);
    } catch (_e) { }
    var d = new Date(v);
    if (isNaN(d.getTime())) return String(v);
    return d.toLocaleString();
  }
  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[c];
    });
  }
  async function fetchReferral() {
    var r = await fetch("/api/user/referral", {
      credentials: "include",
      cache: "no-store"
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  }
  async function fetchRules() {
    var r = await fetch("/api/points/rules", {
      credentials: "include",
      cache: "no-store"
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  }
  function setText(sel, text) {
    var el = document.querySelector(sel);
    if (el) el.textContent = text;
  }
  function renderTableRows(items) {
    var tbody = document.querySelector("[data-refer-table-body]");
    if (!tbody) return;
    if (!Array.isArray(items) || items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center" style="color:var(--text-muted);padding:32px">' + escapeHtml(t("refer.records.empty", "No referrals yet. Invite a friend to get started.")) + "</td></tr>";
      return;
    }
    tbody.innerHTML = items.map(function (item) {
      var maskedWallet = item.wallet_masked || item.wallet || "-";
      if ((!maskedWallet || maskedWallet === "-") && item.referred_wallet) {
        var full = String(item.referred_wallet || "");
        maskedWallet = full.length > 12 ? full.slice(0, 6) + "..." + full.slice(-4) : full || "-";
      }
      var parentWallet = item.parent_wallet_masked || "-";
      if ((!parentWallet || parentWallet === "-") && item.parent_wallet) {
        var parentFull = String(item.parent_wallet || "");
        parentWallet = parentFull.length > 12 ? parentFull.slice(0, 6) + "..." + parentFull.slice(-4) : parentFull || "-";
      }
      if (Number(item.level || 0) === 1) {
        parentWallet = t("refer.parent.self", "Me");
      }
      var levelNum = Number(item.level || 1);
      var levelText = levelNum === 1 ? t("refer.level.l1", "L1") : levelNum === 2 ? t("refer.level.l2", "L2") : t("refer.level.l3", "L3");
      var levelCls = levelNum === 1 ? "badge-success" : levelNum === 2 ? "badge-info" : "badge-warning";
      var created = fmtDate(item.registered_at || item.created_at || "-");
      var active = !!item.active;
      var statusCls = active ? "badge-success" : "badge-neutral";
      var statusTxt = active ? t("refer.status.active", "Active") : t("refer.status.registered", "Registered");
      var rewardPts = Number(item.reward_pts || 0);
      return "<tr>" + '<td><span class="v-mono">' + escapeHtml(maskedWallet) + "</span></td>" + '<td><span class="v-mono">' + escapeHtml(parentWallet) + "</span></td>" + '<td><span class="badge ' + levelCls + '">' + escapeHtml(levelText) + "</span></td>" + '<td class="text-xs" style="color:var(--text-muted)">' + escapeHtml(created) + "</td>" + '<td><span class="badge ' + statusCls + '">' + escapeHtml(statusTxt) + "</span></td>" + '<td class="text-success">' + escapeHtml(fmtPoints(rewardPts)) + "</td>" + "</tr>";
    }).join("");
  }
  function setupPagination(items, onPageChange) {
    var pager = document.querySelector("[data-refer-pagination]");
    var prev = document.querySelector("[data-refer-page-prev]");
    var next = document.querySelector("[data-refer-page-next]");
    var info = document.querySelector("[data-refer-page-info]");
    if (!pager || !prev || !next || !info) return {
      page: 1,
      totalPages: 1,
      pageSize: 10,
      update: function () { }
    };
    var pageSize = 10;
    var total = Array.isArray(items) ? items.length : 0;
    var totalPages = Math.max(1, Math.ceil(total / pageSize));
    var page = 1;
    function renderPager() {
      info.textContent = page + " / " + totalPages;
      prev.disabled = page <= 1;
      next.disabled = page >= totalPages;
      pager.style.display = total > pageSize ? "flex" : "none";
    }
    prev.onclick = function () {
      if (page <= 1) return;
      page -= 1;
      renderPager();
      onPageChange(page, pageSize);
    };
    next.onclick = function () {
      if (page >= totalPages) return;
      page += 1;
      renderPager();
      onPageChange(page, pageSize);
    };
    renderPager();
    return {
      page: page,
      totalPages: totalPages,
      pageSize: pageSize,
      update: function (nextPage) {
        page = Math.max(1, Math.min(totalPages, Number(nextPage) || 1));
        renderPager();
      }
    };
  }
  function setLoading(show) {
    var loading = document.querySelector("[data-refer-loading]");
    var tbody = document.querySelector("[data-refer-table-body]");
    if (loading) loading.style.display = show ? "" : "none";
    if (tbody) tbody.style.display = show ? "none" : "";
  }
  function bindCopy(refLinkGetter) {
    var btn = document.querySelector('[data-refer-action="copy"]');
    var msg = document.querySelector("[data-refer-copy-msg]");
    var input = document.querySelector("[data-refer-link-input]");
    if (!btn || !msg || !input) return;
    btn.addEventListener("click", async function () {
      var link = refLinkGetter();
      if (!link) return;
      var msgOk = t("refer.copy.success", "Link copied to clipboard");
      var msgFb = t("refer.copy.fallback", "Link copied");
      try {
        await navigator.clipboard.writeText(link);
        msg.textContent = msgOk;
      } catch (_e) {
        input.select();
        document.execCommand("copy");
        msg.textContent = msgFb;
      }
      setTimeout(function () {
        msg.textContent = "";
      }, 3e3);
    });
  }
  async function initReferPageVanilla() {
    var runtime = String(document.documentElement.getAttribute("data-frontend-runtime") || "").toLowerCase();
    if (runtime !== "htmx") {
      window.__VP_HTMX_REFER_READY__ = false;
      return;
    }
    var root = document.getElementById("refer-page-root");
    if (!root) return;
    if (window.veyraI18n && typeof window.veyraI18n.registerPagePack === 'function') {
      try { await window.veyraI18n.registerPagePack('refer'); } catch (_) { }
    }
    var refLink = "";
    var latestData = null;
    bindCopy(function () {
      return refLink;
    });
    setLoading(true);
    function renderFromData(d) {
      var code = d.code || d.referral_code || "";
      var count = Number(d.direct_referrals || d.count || 0);
      var earnings = Number(d.earnings || d.total_reward_points || 0);
      refLink = d.referral_link || "https://veyra.trade/?ref=" + code;
      setText('[data-refer-stat="count"]', fmtNum(count, 0));
      setText('[data-refer-stat="earnings"]', fmtCoin(earnings));
      setText('[data-refer-stat="code"]', code || "-");
      var input = document.querySelector("[data-refer-link-input]");
      if (input) input.value = refLink;
      var allRows = Array.isArray(d.referrals) ? d.referrals : [];
      var levelCounts = d.level_counts || {};
      var l1Count = Number(levelCounts.l1 || allRows.filter(function (x) {
        return Number(x.level || 0) === 1;
      }).length || 0);
      var l2Count = Number(levelCounts.l2 || allRows.filter(function (x) {
        return Number(x.level || 0) === 2;
      }).length || 0);
      var l3Count = Number(levelCounts.l3 || allRows.filter(function (x) {
        return Number(x.level || 0) === 3;
      }).length || 0);
      var totalAllLevels = Number(d.total_referrals_all_levels || l1Count + l2Count + l3Count);
      setText("[data-refer-count-badge]", fmtNum(totalAllLevels, 0) + " " + t("refer.records.peopleSuffix", "people"));
      setText("[data-refer-l1-count]", fmtNum(l1Count, 0));
      setText("[data-refer-l2-count]", fmtNum(l2Count, 0));
      setText("[data-refer-l3-count]", fmtNum(l3Count, 0));
      var pager = setupPagination(allRows, function (page, pageSize) {
        var start = (page - 1) * pageSize;
        renderTableRows(allRows.slice(start, start + pageSize));
      });
      renderTableRows(allRows.slice(0, pager.pageSize));
    }
    try {
      var d = await fetchReferral();
      latestData = d;
      renderFromData(d);
      try {
        var rulesData = await fetchRules();
        var rewards = rulesData && rulesData.referral_rewards || {};
        var l1 = Number(rewards["1"] || rewards[1] || 0);
        var l2 = Number(rewards["2"] || rewards[2] || 0);
        var l3 = Number(rewards["3"] || rewards[3] || 0);
        var referred = Number(rewards.referred || 0);
        setText("[data-refer-l1-bonus]", "+" + fmtPoints(l1));
        setText("[data-refer-l2-bonus]", "+" + fmtPoints(l2));
        setText("[data-refer-l3-bonus]", "+" + fmtPoints(l3));
        setText("[data-refer-referred-bonus]", "+" + fmtPoints(referred));
      } catch (_e) { }
    } catch (e) {
      renderTableRows([]);
      setText("[data-refer-copy-msg]", e && e.message ? e.message : String(e || "error"));
    } finally {
      setLoading(false);
    }
    if (!window.__VP_HTMX_REFER_I18N_BOUND__) {
      window.__VP_HTMX_REFER_I18N_BOUND__ = true;
      document.addEventListener("i18n:changed", function () {
        if (window.veyraI18nApply) window.veyraI18nApply(document);
        if (latestData) renderFromData(latestData);
      });
    }
    window.__VP_HTMX_REFER_READY__ = true;
  }
  ready(initReferPageVanilla);
})();