(function () {
  var STORAGE_KEY = "veyra_lang";
  var SUPPORTED = ["zh-CN", "en", "fr", "ja", "ko", "th", "vi", "es", "pt"];
  var DEFAULT = "zh-CN";
  var LABELS = {
    "zh-CN": "简体中文",
    en: "English",
    fr: "Français",
    ja: "日本語",
    ko: "한국어",
    th: "ไทย",
    vi: "Tiếng Việt",
    es: "Español",
    pt: "Português"
  };
  var dict = {};
  var _pagePackCache = {};
  var _registeredPacks = [];

  try {
    if (window.__I18N_PRELOAD__ && typeof window.__I18N_PRELOAD__ === "object") {
      Object.keys(window.__I18N_PRELOAD__).forEach(function (k) {
        dict[k] = window.__I18N_PRELOAD__[k] || {};
      });
    }
  } catch (e) { }

  function detectInitLang() {
    try {
      var saved = localStorage.getItem(STORAGE_KEY);
      if (saved && SUPPORTED.indexOf(saved) !== -1) return saved;
    } catch (e) { }
    try {
      var initLang = window.__INIT_LANG__;
      if (initLang && SUPPORTED.indexOf(initLang) !== -1) return initLang;
    } catch (e) { }
    try {
      var nav = (navigator.language || "").toLowerCase();
      if (nav) {
        for (var i = 0; i < SUPPORTED.length; i++) {
          var s = SUPPORTED[i].toLowerCase();
          if (nav === s || nav.split("-")[0] === s.split("-")[0]) return SUPPORTED[i];
        }
      }
    } catch (e) { }
    return DEFAULT;
  }
  var currentLang = detectInitLang();

  function loadLocale(lang) {
    if (dict[lang] && Object.keys(dict[lang]).length > 0) return Promise.resolve();
    var url = "/v2/static/i18n/" + encodeURIComponent(lang) + ".json?v=" + encodeURIComponent(window.__ASSET_V || Date.now());
    return fetch(url, { cache: "force-cache" })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .catch(function () { return {}; })
      .then(function (d) { dict[lang] = d || {}; });
  }

  function loadPackForLang(packName, lang) {
    var cacheKey = packName + ":" + lang;
    if (_pagePackCache[cacheKey]) return _pagePackCache[cacheKey];
    var av = encodeURIComponent(window.__ASSET_V || "0");
    function _fetchJson(l) {
      var url = "/v2/static/i18n/" + packName + "/" + encodeURIComponent(l) + ".json?v=" + av;
      return fetch(url, { cache: "force-cache" })
        .then(function (r) { return r.ok ? r.json() : {}; })
        .catch(function () { return {}; });
    }
    var fetches = [_fetchJson(lang)];
    if (lang !== "en") fetches.push(_fetchJson("en"));
    var p = Promise.all(fetches).then(function (arr) {
      var curData = arr[0] || {};
      var enData = arr[1] || {};
      if (!dict["en"]) dict["en"] = {};
      Object.keys(enData).forEach(function (k) { dict["en"][k] = enData[k]; });
      if (!dict[lang]) dict[lang] = {};
      Object.keys(curData).forEach(function (k) { dict[lang][k] = curData[k]; });
    });
    _pagePackCache[cacheKey] = p;
    return p;
  }

  function loadPagePack(packName) {
    if (_registeredPacks.indexOf(packName) === -1) _registeredPacks.push(packName);
    return loadPackForLang(packName, currentLang);
  }

  function mergeDynamic(payload) {
    try {
      var langs = (payload && payload.dict) || {};
      Object.keys(langs).forEach(function (lang) {
        if (!dict[lang]) dict[lang] = {};
        var entries = langs[lang] || {};
        Object.keys(entries).forEach(function (k) {
          if (!dict[lang][k]) dict[lang][k] = entries[k];
        });
      });
    } catch (_) { }
  }
  function loadDynamic(lang) {
    var url = "/api/i18n/dynamic" + (lang ? "?lang=" + encodeURIComponent(lang) : "");
    return fetch(url, { credentials: "include", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (d) { if (d && d.ok) mergeDynamic(d); });
  }
  function forceReloadDynamic(lang) {
    var target = lang || currentLang;
    var url = "/api/i18n/dynamic?lang=" + encodeURIComponent(target) + "&_=" + Date.now();
    return fetch(url, { credentials: "include", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (d) {
        if (!d || !d.ok) return;
        try {
          var langs = (d && d.dict) || {};
          Object.keys(langs).forEach(function (lng) {
            if (!dict[lng]) dict[lng] = {};
            var entries = langs[lng] || {};
            Object.keys(entries).forEach(function (k) { dict[lng][k] = entries[k]; });
          });
        } catch (_) { }
        try { applyDomTranslations(); } catch (_) { }
      });
  }
  window.veyraI18nReload = forceReloadDynamic;

  function tCore(key, fallback) {
    var cur = dict[currentLang] || {};
    if (cur[key] !== undefined && cur[key] !== "") return cur[key];
    var en = dict["en"] || {};
    if (en[key] !== undefined && en[key] !== "") return en[key];
    return fallback !== undefined ? fallback : key;
  }

  function setLang(lang) {
    if (SUPPORTED.indexOf(lang) === -1 || lang === currentLang) return Promise.resolve();

    var loads = [loadLocale(lang)];
    if (lang !== "en" && (!dict["en"] || Object.keys(dict["en"]).length === 0)) {
      loads.push(loadLocale("en"));
    }
    _registeredPacks.forEach(function (packName) {
      loads.push(loadPackForLang(packName, lang));
    });

    return Promise.all(loads)
      .then(function () { return loadDynamic(lang); })
      .catch(function () { })
      .then(function () {
        currentLang = lang;
        try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) { }
        try { document.cookie = "veyra_lang=" + lang + "; path=/; max-age=31536000; SameSite=Lax"; } catch (e) { }
        document.documentElement.lang = lang;
        document.documentElement.setAttribute("data-lang", lang);
        applyDomTranslations();
        document.dispatchEvent(new CustomEvent("i18n:changed", { detail: { lang: lang } }));
      });
  }

  function applyDomTranslations(root) {
    var scope = root || document;
    try {
      scope.querySelectorAll("[data-i18n]").forEach(function (el) {
        var key = el.getAttribute("data-i18n");
        if (!key) return;
        var fallback = el.getAttribute("data-i18n-fallback") || el.textContent;
        var v = tCore(key, fallback);
        if (v != null) el.textContent = v;
      });
      scope.querySelectorAll("[data-i18n-attr]").forEach(function (el) {
        var spec = el.getAttribute("data-i18n-attr");
        if (!spec) return;
        spec.split(";").forEach(function (pair) {
          var parts = pair.split(":");
          if (parts.length < 2) return;
          var attr = parts[0].trim();
          var key = parts.slice(1).join(":").trim();
          var v = tCore(key, el.getAttribute(attr) || "");
          if (v != null) el.setAttribute(attr, v);
        });
      });
    } catch (e) { }
  }
  window.veyraI18nApply = applyDomTranslations;

  window.veyraI18n = {
    init: function () {
      var loads = [];
      if (!dict[currentLang] || Object.keys(dict[currentLang]).length === 0) {
        loads.push(loadLocale(currentLang));
      }
      if (currentLang !== "en" && (!dict["en"] || Object.keys(dict["en"]).length === 0)) {
        loads.push(loadLocale("en"));
      }
      return (loads.length ? Promise.all(loads) : Promise.resolve())
        .then(function () { return loadDynamic(currentLang); });
    },
    t: tCore,
    setLang: setLang,
    registerPagePack: loadPagePack,
    get lang() { return currentLang; },
    supported: SUPPORTED,
    labels: LABELS
  };

  window.veyraI18n.init().then(function () {
    document.documentElement.lang = currentLang;
    applyDomTranslations();
  });
  document.addEventListener("DOMContentLoaded", function () {
    applyDomTranslations();
  });
})();
