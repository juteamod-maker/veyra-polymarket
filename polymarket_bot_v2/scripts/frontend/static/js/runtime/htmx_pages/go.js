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
  function escapeHtml(s) {
    return String(s || "").replace(/[&<>]/g, function (c) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;"
      }[c];
    });
  }
  function qs(sel) {
    return document.querySelector(sel);
  }
  function qsa(sel) {
    return Array.prototype.slice.call(document.querySelectorAll(sel));
  }
  async function fetchMarkdown() {
    var lang = window.veyraI18n && window.veyraI18n.lang || document.documentElement.getAttribute("data-lang") || document.documentElement.lang || window.__INIT_LANG__ || "zh-CN";
    var root = qs("#go-page-root");
    var helpSlug = root && root.getAttribute("data-help-slug") || "go";
    var r = await fetch("/api/help-pages/" + encodeURIComponent(helpSlug) + "?lang=" + encodeURIComponent(lang), {
      cache: "no-store"
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    var d = await r.json().catch(function () {
      return {};
    });
    return {
      markdown: String(d.markdown || ""),
      title: String(d.title || "")
    };
  }
  function markdownToHtml(markdown) {
    if (!window.marked) {
      return "<pre>" + escapeHtml(markdown) + "</pre>";
    }
    var raw = window.marked.parse(markdown, {
      gfm: true,
      breaks: false,
      headerIds: true
    });
    return window.DOMPurify ? window.DOMPurify.sanitize(raw) : raw;
  }
  function buildToc(articleEl, tocEl) {
    if (!articleEl || !tocEl) return;
    var heads = articleEl.querySelectorAll("h2, h3");
    var items = [];
    for (var i = 0; i < heads.length; i++) {
      var h = heads[i];
      var id = h.id;
      if (!id) {
        id = "sec-" + i + "-" + (h.textContent || "").trim().toLowerCase().replace(/[^a-z0-9\u4e00-\u9fa5]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
        h.id = id;
      }
      items.push({
        id: id,
        text: (h.textContent || "").trim(),
        level: h.tagName === "H2" ? 2 : 3
      });
    }
    if (!items.length) {
      tocEl.innerHTML = '<div style="opacity:.5;font-size:12px">' + escapeHtml(t("go.toc.empty", "(no headings)")) + "</div>";
      return;
    }
    tocEl.innerHTML = items.map(function (item) {
      var indent = (item.level - 2) * 12;
      return '<a href="#' + item.id + '" ' + 'style="display:block;padding:4px 0 4px ' + indent + "px;color:var(--text-secondary);text-decoration:none;border-left:2px solid transparent;padding-left:" + (indent + 10) + 'px">' + escapeHtml(item.text) + "</a>";
    }).join("");
    qsa("#go-toc a").forEach(function (a) {
      a.addEventListener("click", function () {
        qsa("#go-toc a").forEach(function (el) {
          el.style.color = "var(--text-secondary)";
          el.style.borderLeftColor = "transparent";
        });
        a.style.color = "var(--accent)";
        a.style.borderLeftColor = "var(--accent)";
      });
    });
  }
  async function initGoPageVanilla() {
    var runtime = String(document.documentElement.getAttribute("data-frontend-runtime") || "").toLowerCase();
    if (runtime !== "htmx") {
      window.__VP_HTMX_GO_READY__ = false;
      return;
    }
    if (window.veyraI18n && typeof window.veyraI18n.registerPagePack === 'function') {
      try { await window.veyraI18n.registerPagePack('go'); } catch (_) { }
    }
    var root = qs("#go-page-root");
    var article = qs("#go-article");
    var toc = qs("#go-toc");
    var titleNode = qs("[data-go-title]");
    var refreshBtn = qs('[data-go-btn="refresh"]');
    if (!root || !article || !toc) return;
    async function reload() {
      article.innerHTML = '<div style="opacity:.6">' + escapeHtml(t("go.contentLoading", "Loading content...")) + "</div>";
      try {
        var payload = await fetchMarkdown();
        article.innerHTML = markdownToHtml(payload.markdown);
        if (titleNode && payload.title) titleNode.textContent = payload.title;
        if (payload.title) {
          try {
            document.title = "V e Y r a - " + payload.title;
          } catch (_e) { }
        }
        buildToc(article, toc);
      } catch (e) {
        var err = e && e.message ? e.message : String(e || "Unknown error");
        article.innerHTML = '<div class="msg err" style="padding:12px;border:1px solid #55322d;border-radius:8px;background:#3a1e1c;color:#f3c1ba">' + "<span>" + escapeHtml(t("go.loadFailedPrefix", "Load failed:")) + "</span> " + "<span>" + escapeHtml(err) + "</span>" + "</div>";
      }
    }
    if (refreshBtn) {
      refreshBtn.addEventListener("click", function () {
        reload();
      });
    }
    if (!window.__VP_HTMX_GO_I18N_BOUND__) {
      window.__VP_HTMX_GO_I18N_BOUND__ = true;
      document.addEventListener("i18n:changed", function () {
        if (window.veyraI18nApply) window.veyraI18nApply(document);
        reload();
      });
    }
    await reload();
    window.__VP_HTMX_GO_READY__ = true;
  }
  ready(initGoPageVanilla);
})();