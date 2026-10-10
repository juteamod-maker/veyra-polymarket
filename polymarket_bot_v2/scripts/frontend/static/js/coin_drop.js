(function () {
  "use strict";
  if (window.veyraCoinDrop && window.veyraCoinDrop.__loaded) return;
  var ASSET_VERSION = window.__veyra_asset_version || "0";
  var I18N_BASE = "/v2/static/i18n/coin_drop/";
  var COIN_IMG = "/static/img/coin.png";
  var DEFAULT_LOCALE = "en";
  var SUPPORTED_LOCALES = ["en", "es", "fr", "ja", "ko", "pt", "th", "vi", "zh-CN"];
  var FALLBACK_TEXTS = {
    tooltip: "Click to collect coin (+{pts} pts)",
    tooltipDisabled: "Daily limit reached",
    tooltipSlotCollected: "Already collected here today",
    toastTitle: "Coin collected",
    toastBodyGuest: "You collected {coin_pts} coin points (today {today}/{cap}). Sign in to merge collected coins to your account points.",
    toastBodyRegistered: "You collected {coin_pts} coin points (today {today}/{cap}). Added to your points account.",
    limitReachedTitle: "Daily limit reached",
    limitReachedBody: "You have reached today’s coin limit ({cap}). Come back tomorrow.",
    slotAlreadyTitle: "Already collected here",
    slotAlreadyBody: "You have already collected the coin from this island today. Visit other islands to collect more.",
    firstTimeHint: "Tip: every modal hides one floating gold coin. Click to collect points.",
    dismissBtn: "Got it",
    progressLabel: "Today {today}/{cap}",
    pendingLabel: "Pending {pending} pts",
    statusTitle: "Coin collection status",
    statusToday: "Today {today}/{cap}",
    statusRemaining: "Remaining: {remaining}",
    statusPointsPerCoin: "{pts} points per coin",
    statusPendingGuest: "Pending (guest, will be merged after sign-in): {pending} pts ({coins} coins)",
    statusBalance: "Account points balance: {balance}",
    statusCollectedSlots: "Islands/slots collected today: {count}",
    statusGuestNote: "You are browsing as a guest. Sign in to convert your coin points into account points.",
    statusEmpty: "No coins collected yet today. Open an island modal to find a floating gold coin.",
    helpTitle: "How coin collection works",
    helpIntro: "Explore the Preview map to collect floating gold coins. Each coin = {pts} pt.",
    helpRule1: "Click an island once to open its event card; one floating gold coin will appear inside the card.",
    helpRule2: "Each island gives at most one coin per day. After collecting that island stops dropping coins until the next UTC day.",
    helpRule3: "Daily limit is {cap} coins; once reached no further coin drops appear today.",
    helpRule4: "Guests can collect; points are kept as pending and merged into your account after sign-in.",
    helpRule5: "Click the treasure chest at the lower-right anytime to view your collection status.",
    helpDismissHint: "Press any key or click the map to dismiss; tap ❓ to reopen.",
    helpReopenAria: "Open coin collection help",
    helpLinkAbout: "About",
    helpLinkPrivacy: "Privacy",
    helpLinkTerms: "Terms",
    helpLinkFaq: "FAQ"
  };
  var state = {
    status: null,
    texts: FALLBACK_TEXTS,
    locale: DEFAULT_LOCALE,
    fetchingStatus: false,
    statusPromise: null,
    boundModals: new WeakSet,
    hintShown: false,
    coinSeqInModal: new WeakSet,
    collectedSlots: {},
    chestClickBound: false,
    statusModalOpen: false
  };
  function _log() {
    return;
  }
  function _warn() {
    return;
  }
  function _detectLocale() {
    try {
      if (window.veyraI18n && typeof window.veyraI18n.locale === "string" && window.veyraI18n.locale) {
        return window.veyraI18n.locale;
      }
      var htmlLang = document.documentElement && document.documentElement.lang || "";
      if (htmlLang) return htmlLang;
      var nav = (navigator.language || "en").slice(0, 5);
      return nav;
    } catch (e) {
      return DEFAULT_LOCALE;
    }
  }
  function _normalizeLocale(loc) {
    if (!loc) return DEFAULT_LOCALE;
    if (SUPPORTED_LOCALES.indexOf(loc) >= 0) return loc;
    var short = String(loc).split(/[-_]/)[0].toLowerCase();
    var byShort = {
      en: "en",
      es: "es",
      fr: "fr",
      ja: "ja",
      ko: "ko",
      pt: "pt",
      th: "th",
      vi: "vi",
      zh: "zh-CN"
    };
    return byShort[short] || DEFAULT_LOCALE;
  }
  function _formatText(template, params) {
    if (typeof template !== "string") return "";
    return template.replace(/\{(\w+)\}/g, function (m, k) {
      return params && k in params ? String(params[k]) : m;
    });
  }
  function _t(key, params) {
    var bundle = state.texts || FALLBACK_TEXTS;
    var raw = bundle && bundle[key] || FALLBACK_TEXTS[key] || "";
    return _formatText(raw, params || {});
  }
  function _loadTexts() {
    var loc = _normalizeLocale(_detectLocale());
    state.locale = loc;
    var url = I18N_BASE + loc + ".json?v=" + encodeURIComponent(ASSET_VERSION);
    return fetch(url, {
      credentials: "same-origin"
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (json) {
      if (json && typeof json === "object") {
        var merged = {};
        for (var k in FALLBACK_TEXTS) merged[k] = FALLBACK_TEXTS[k];
        for (var k2 in json) merged[k2] = json[k2];
        state.texts = merged;
      }
    }).catch(function (e) {
      _warn("i18n load failed", loc, e);
    });
  }
  function _refreshStatus() {
    if (state.fetchingStatus && state.statusPromise) return state.statusPromise;
    state.fetchingStatus = true;
    state.statusPromise = fetch("/api/coin_drop/status", {
      credentials: "same-origin"
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (json) {
      state.fetchingStatus = false;
      if (json && json.ok) {
        state.status = json;
        state.collectedSlots = {};
        var arr = json && json.collected_slots || [];
        for (var i = 0; i < arr.length; i++) {
          state.collectedSlots[String(arr[i])] = true;
        }
        try {
          var saltSrc = String(json.wallet || json.guest_token || "");
          if (saltSrc) {
            window.__veyraSeazSalt = saltSrc;
          }
        } catch (e) { }
        if (json.merged && (json.merged.points || 0) > 0) {
          try {
            window.dispatchEvent(new CustomEvent("veyra-points-changed", {
              detail: {
                source: "coin_drop_merge",
                delta: json.merged.points
              }
            }));
          } catch (e) { }
        }
      }
      return state.status;
    }).catch(function (e) {
      state.fetchingStatus = false;
      _warn("status fetch failed", e);
      return null;
    });
    return state.statusPromise;
  }
  function _findChestRect() {
    var sel = ["[data-veyra-chest]", 'img[alt="Treasure chest"]', 'img[alt="treasure chest"]', 'img[src*="Treasurechest"]', 'img[src*="treasurechest"]'];
    for (var i = 0; i < sel.length; i++) {
      var el = document.querySelector(sel[i]);
      if (el) {
        var r = el.getBoundingClientRect();
        if (r && r.width > 0 && r.height > 0) {
          return {
            x: r.left + r.width / 2,
            y: r.top + r.height / 2,
            el: el
          };
        }
      }
    }
    return {
      x: window.innerWidth - 60,
      y: window.innerHeight - 60,
      el: null
    };
  }
  function _ensureToastStack() {
    var s = document.querySelector(".veyra-coin-toast-stack");
    if (s) return s;
    s = document.createElement("div");
    s.className = "veyra-coin-toast-stack";
    document.body.appendChild(s);
    return s;
  }
  function _showToast(opts) {
    var stack = _ensureToastStack();
    var item = document.createElement("div");
    item.className = "veyra-coin-toast";
    var titleHtml = '<div class="vct-title">' + _esc(opts.title || _t("toastTitle")) + "</div>";
    var bodyHtml = '<div class="vct-text">' + _esc(opts.body || "") + "</div>";
    var progressHtml = opts.progress ? '<div class="vct-progress">' + _esc(opts.progress) + "</div>" : "";
    item.innerHTML = titleHtml + bodyHtml + progressHtml + '<div class="vct-actions"><button type="button" class="vct-btn">' + _esc(_t("dismissBtn")) + "</button></div>";
    stack.appendChild(item);
    requestAnimationFrame(function () {
      item.classList.add("show");
    });
    var timer = setTimeout(close, opts.duration || 6e3);
    item.querySelector(".vct-btn").addEventListener("click", function () {
      clearTimeout(timer);
      close();
    });
    function close() {
      item.classList.remove("show");
      setTimeout(function () {
        if (item.parentNode) item.parentNode.removeChild(item);
      }, 240);
    }
  }
  function _esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function _flyCoinToChest(startX, startY, durationMs) {
    return new Promise(function (resolve) {
      var chest = _findChestRect();
      var fly = document.createElement("div");
      fly.className = "veyra-coin-fly";
      fly.style.left = startX - 24 + "px";
      fly.style.top = startY - 24 + "px";
      document.body.appendChild(fly);
      var dx = chest.x - startX;
      var dy = chest.y - startY;
      var midX = dx * .5;
      var midY = dy * .5 - Math.max(60, Math.abs(dy) * .45);
      var dur = durationMs || 1300;
      try {
        fly.animate([{
          transform: "translate(0,0) rotate(0deg) scale(1)",
          opacity: 1
        }, {
          transform: "translate(" + midX + "px," + midY + "px) rotate(220deg) scale(0.9)",
          opacity: 1,
          offset: .55
        }, {
          transform: "translate(" + dx + "px," + dy + "px) rotate(540deg) scale(0.55)",
          opacity: .9
        }], {
          duration: dur,
          easing: "cubic-bezier(.55,.06,.68,1.0)",
          fill: "forwards"
        }).onfinish = function () {
          if (fly.parentNode) fly.parentNode.removeChild(fly);
          if (chest.el) {
            chest.el.classList.remove("veyra-chest-receive");
            void chest.el.offsetWidth;
            chest.el.classList.add("veyra-chest-receive");
            setTimeout(function () {
              try {
                chest.el.classList.remove("veyra-chest-receive");
              } catch (e) { }
            }, 700);
          }
          resolve();
        };
      } catch (e) {
        fly.style.transition = "transform " + dur + "ms cubic-bezier(.55,.06,.68,1.0), opacity " + dur + "ms ease";
        requestAnimationFrame(function () {
          fly.style.transform = "translate(" + dx + "px," + dy + "px) rotate(540deg) scale(0.55)";
          fly.style.opacity = "0.9";
        });
        setTimeout(function () {
          if (fly.parentNode) fly.parentNode.removeChild(fly);
          resolve();
        }, dur + 50);
      }
    });
  }
  function _postCollect(slotId) {
    var body = JSON.stringify({
      slot_id: slotId || ""
    });
    return fetch("/api/coin_drop/collect", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json"
      },
      body: body
    }).then(function (r) {
      return r.json();
    }).catch(function (e) {
      _warn("collect failed", e);
      return null;
    });
  }
  function _renderToastForResult(result) {
    if (!result) return;
    if (!result.ok && result.code === "daily_cap_reached") {
      _showToast({
        title: _t("limitReachedTitle"),
        body: _t("limitReachedBody", {
          cap: result.daily_cap || 10
        }),
        duration: 5e3
      });
      return;
    }
    if (!result.ok && result.code === "slot_already_collected") {
      _showToast({
        title: _t("slotAlreadyTitle"),
        body: _t("slotAlreadyBody"),
        duration: 4500
      });
      return;
    }
    if (!result.ok) return;
    var coinPts = result.points_per_coin || 0;
    var today = result.today_count || 0;
    var cap = result.daily_cap || 10;
    var isGuest = !!result.is_guest;
    var pending = result.pending_points || 0;
    var bodyKey = isGuest ? "toastBodyGuest" : "toastBodyRegistered";
    var body = _t(bodyKey, {
      coin_pts: coinPts,
      today: today,
      cap: cap
    });
    var progress = _t("progressLabel", {
      today: today,
      cap: cap
    });
    if (isGuest && pending > 0) {
      progress += " · " + _t("pendingLabel", {
        pending: pending
      });
    }
    _showToast({
      title: _t("toastTitle"),
      body: body,
      progress: progress,
      duration: 7e3
    });
  }
  function _attachToModal(modalEl, opts) {
    if (!modalEl || state.boundModals.has(modalEl)) return null;
    state.boundModals.add(modalEl);
    opts = opts || {};
    var slotId = String(opts && opts.slot_id || "").trim();
    if (slotId && slotId.indexOf("__") === 0) {
      return null;
    }
    if (slotId && state.collectedSlots && state.collectedSlots[slotId]) {
      return null;
    }
    var bodyEl = modalEl.querySelector(".vp-modal-body") || modalEl;
    if (!bodyEl) return null;
    var prevPos = window.getComputedStyle(bodyEl).position;
    if (prevPos === "static" || !prevPos) {
      bodyEl.style.position = "relative";
    }
    var st = state.status || {};
    var capped = !!st && (st.today_count || 0) >= (st.daily_cap || 10);
    var coin = document.createElement("div");
    coin.className = "veyra-coin" + (capped ? " disabled" : "");
    coin.style.top = opts.top || "14px";
    coin.style.right = opts.right || "14px";
    coin.setAttribute("role", "button");
    coin.setAttribute("aria-label", _t("toastTitle"));
    coin.title = capped ? _t("tooltipDisabled") : _t("tooltip", {
      pts: st.points_per_coin || 10
    });
    var badge = document.createElement("span");
    badge.className = "veyra-coin-badge";
    var remain = Math.max(0, (st.daily_cap || 10) - (st.today_count || 0));
    badge.textContent = remain + "/" + (st.daily_cap || 10);
    coin.appendChild(badge);
    bodyEl.appendChild(coin);
    if (capped) {
      coin.addEventListener("click", function () {
        _showToast({
          title: _t("limitReachedTitle"),
          body: _t("limitReachedBody", {
            cap: st.daily_cap || 10
          }),
          duration: 4e3
        });
      });
      return coin;
    }
    var clicked = false;
    coin.addEventListener("click", function () {
      if (clicked) return;
      clicked = true;
      coin.style.pointerEvents = "none";
      coin.style.animation = "none";
      var rect = coin.getBoundingClientRect();
      var startX = rect.left + rect.width / 2;
      var startY = rect.top + rect.height / 2;
      coin.style.opacity = "0";
      var aborted = false;
      var modalGoneObs = new MutationObserver(function () {
        if (!document.body.contains(modalEl)) {
          aborted = true;
          modalGoneObs.disconnect();
        }
      });
      try {
        modalGoneObs.observe(document.body, {
          childList: true,
          subtree: false
        });
      } catch (e) { }
      _flyCoinToChest(startX, startY, 1300).then(function () {
        modalGoneObs.disconnect();
        if (aborted) {
          _log("modal closed before fall completed → skip collect");
          return;
        }
        return _postCollect(slotId).then(function (result) {
          if (result && result.ok) {
            if (slotId) state.collectedSlots[slotId] = true;
            state.status = {
              ok: true,
              is_guest: result.is_guest,
              today_count: result.today_count,
              daily_cap: result.daily_cap,
              remaining: result.remaining,
              points_per_coin: result.points_per_coin,
              pending_points: result.pending_points,
              pending_coins: result.pending_coins,
              wallet: state.status ? state.status.wallet : "",
              guest_token: result.guest_token,
              collected_slots: Object.keys(state.collectedSlots)
            };
            try {
              window.dispatchEvent(new CustomEvent("veyra-points-changed", {
                detail: {
                  source: "coin_drop",
                  delta: result.awarded_now || 0,
                  is_guest: !!result.is_guest
                }
              }));
            } catch (e) { }
          } else if (result && result.code === "slot_already_collected" && slotId) {
            state.collectedSlots[slotId] = true;
          }
          _renderToastForResult(result);
        });
      });
    });
    return coin;
  }
  function _openStatusModal() {
    if (state.statusModalOpen) return;
    if (!window.vp || typeof window.vp.modal !== "function") {
      var st0 = state.status || {};
      _showToast({
        title: _t("statusTitle"),
        body: _t("statusToday", {
          today: st0.today_count || 0,
          cap: st0.daily_cap || 10
        }),
        duration: 5e3
      });
      return;
    }
    state.statusModalOpen = true;
    _refreshStatus().then(function () {
      try {
        var st = state.status || {};
        var isGuest = !!st.is_guest;
        var today = st.today_count || 0;
        var cap = st.daily_cap || 10;
        var remaining = Math.max(0, st.remaining != null ? st.remaining : cap - today);
        var pts = st.points_per_coin || 10;
        var pending = st.pending_points || 0;
        var pendingCoins = st.pending_coins || 0;
        var balance = st.points_balance != null ? st.points_balance : "—";
        var slotsCount = st.collected_slots && st.collected_slots.length || Object.keys(state.collectedSlots || {}).length || 0;
        var rows = [];
        rows.push('<div class="vct-line"><b>' + _esc(_t("statusToday", {
          today: today,
          cap: cap
        })) + "</b></div>");
        rows.push('<div class="vct-line">' + _esc(_t("statusRemaining", {
          remaining: remaining
        })) + " &middot; " + _esc(_t("statusPointsPerCoin", {
          pts: pts
        })) + "</div>");
        rows.push('<div class="vct-line">' + _esc(_t("statusCollectedSlots", {
          count: slotsCount
        })) + "</div>");
        if (isGuest) {
          rows.push('<div class="vct-line">' + _esc(_t("statusPendingGuest", {
            pending: pending,
            coins: pendingCoins
          })) + "</div>");
          rows.push('<div class="vct-line vct-muted">' + _esc(_t("statusGuestNote")) + "</div>");
        } else {
          rows.push('<div class="vct-line">' + _esc(_t("statusBalance", {
            balance: balance
          })) + "</div>");
        }
        if (today === 0 && pending === 0) {
          rows.push('<div class="vct-line vct-muted" style="margin-top:8px">' + _esc(_t("statusEmpty")) + "</div>");
        }
        var bodyHtml = '<div class="veyra-coin-status-panel">' + rows.join("") + "</div>";
        window.vp.modal({
          type: "info",
          title: _t("statusTitle"),
          bodyHtml: bodyHtml,
          actions: [],
          width: "420px",
          coinDrop: {
            slot_id: "__status_panel__"
          }
        });
      } finally {
        setTimeout(function () {
          state.statusModalOpen = false;
        }, 400);
      }
    });
  }
  function _bindChestClick() {
    if (state.chestClickBound) return true;
    var sel = ["[data-veyra-chest]", 'img[alt="Treasure chest"]', 'img[alt="treasure chest"]', 'img[src*="Treasurechest"]', 'img[src*="treasurechest"]'];
    var el = null;
    for (var i = 0; i < sel.length; i++) {
      el = document.querySelector(sel[i]);
      if (el) break;
    }
    if (!el) return false;
    try {
      el.style.cursor = "pointer";
      if (!el.style.position || el.style.position === "static") {
        el.style.position = "relative";
      }
      el.style.zIndex = "9999";
      el.style.pointerEvents = "auto";
      el.setAttribute("data-veyra-chest-bound", "1");
      el.setAttribute("role", "button");
      el.setAttribute("aria-label", _t("statusTitle"));
      el.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        _openStatusModal();
      });
      state.chestClickBound = true;
      _log("chest click bound for status modal");
      return true;
    } catch (e) {
      _warn("chest bind failed", e);
      return false;
    }
  }
  function _waitForChest() {
    if (_bindChestClick()) return;
    var tries = 0;
    var iv = setInterval(function () {
      tries++;
      if (_bindChestClick() || tries > 80) clearInterval(iv);
    }, 250);
  }
  function _wrapVpModal() {
    if (!window.vp || typeof window.vp.modal !== "function") return false;
    if (window.vp.__coinDropWrapped) return true;
    var orig = window.vp.modal;
    window.vp.modal = function (opts) {
      var p = orig.call(window.vp, opts);
      try {
        var enabled = document.body && document.body.getAttribute("data-coin-drop-enabled") === "1";
        var explicit = !!(opts && opts.coinDrop);
        if (enabled || explicit) {
          var attempts = 0;
          var iv = setInterval(function () {
            attempts++;
            var backdrops = document.querySelectorAll(".vp-modal-backdrop");
            var last = backdrops[backdrops.length - 1];
            if (last && !state.boundModals.has(last)) {
              clearInterval(iv);
              _attachToModal(last, opts && opts.coinDrop || {});
            } else if (attempts > 10) {
              clearInterval(iv);
            }
          }, 30);
        }
      } catch (e) {
        _warn("vp.modal wrap error", e);
      }
      return p;
    };
    window.vp.__coinDropWrapped = true;
    _log("vp.modal wrapped for coin drop");
    return true;
  }
  function _waitForVp() {
    if (_wrapVpModal()) return;
    var tries = 0;
    var iv = setInterval(function () {
      tries++;
      if (_wrapVpModal() || tries > 100) clearInterval(iv);
    }, 50);
  }
  var helpDom = {
    panel: null,
    badge: null,
    dismissed: false,
    listeners: null
  };
  function _helpInjectStylesOnce() {
    if (document.getElementById("veyra-coin-help-style")) return;
    var st = document.createElement("style");
    st.id = "veyra-coin-help-style";
    st.textContent = [".veyra-coin-help-panel{position:fixed;top:88px;right:16px;width:340px;max-width:calc(100vw - 32px);", "  z-index:10000;background:rgba(8,16,30,0.94);color:#e8eefc;", "  border:1px solid rgba(96,165,250,0.45);border-radius:14px;padding:16px 18px;", "  box-shadow:0 10px 32px rgba(0,0,0,0.45);font:13px/1.55 system-ui,-apple-system,sans-serif;", "  pointer-events:auto;animation:veyra-coin-help-in .26s ease-out;}", "@keyframes veyra-coin-help-in{from{transform:translateY(-6px);opacity:0}to{transform:none;opacity:1}}", ".veyra-coin-help-panel h3{margin:0 0 8px;font-size:14px;font-weight:700;color:#fbbf24;display:flex;align-items:center;gap:8px;}", ".veyra-coin-help-panel ul{margin:8px 0 6px;padding-left:18px;}", ".veyra-coin-help-panel li{margin:3px 0;}", ".veyra-coin-help-panel .veyra-coin-help-foot{margin-top:8px;font-size:11px;color:#94a3b8;font-style:italic;}", ".veyra-coin-help-panel .veyra-coin-help-links{margin-top:10px;padding-top:10px;border-top:1px solid rgba(148,163,184,0.18);display:flex;flex-wrap:wrap;gap:8px;}", ".veyra-coin-help-panel .veyra-coin-help-links a{display:inline-flex;align-items:center;justify-content:center;padding:6px 10px;border-radius:999px;border:1px solid rgba(96,165,250,0.28);background:linear-gradient(135deg, rgba(30,41,59,0.94), rgba(15,23,42,0.78));color:#dbeafe;text-decoration:none;font-size:11px;font-weight:700;transition:transform .15s ease,border-color .15s ease;}", ".veyra-coin-help-panel .veyra-coin-help-links a:hover{transform:translateY(-1px);border-color:rgba(251,191,36,0.55);color:#fff7d6;}", ".veyra-coin-help-panel .veyra-coin-help-close{position:absolute;top:6px;right:8px;background:transparent;border:0;color:#94a3b8;font-size:18px;cursor:pointer;padding:2px 6px;line-height:1;}", ".veyra-coin-help-panel .veyra-coin-help-close:hover{color:#fbbf24;}", ".veyra-coin-help-badge{position:fixed;z-index:10000;width:30px;height:30px;border-radius:50%;", "  background:rgba(8,16,30,0.85);border:1px solid rgba(96,165,250,0.55);color:#fbbf24;", "  font:600 16px/28px system-ui,-apple-system,sans-serif;text-align:center;cursor:pointer;", "  pointer-events:auto;box-shadow:0 4px 12px rgba(0,0,0,0.4);transition:transform .15s ease;}", ".veyra-coin-help-badge:hover{transform:scale(1.08);}"].join("\n");
    document.head.appendChild(st);
  }
  function _renderHelpHtml() {
    var pts = state.status && state.status.points_per_coin || 1;
    var cap = state.status && state.status.daily_cap || 10;
    var today = state.status && state.status.today_count || 0;
    function esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
        return {
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;"
        }[c];
      });
    }
    return ['<button type="button" class="veyra-coin-help-close" aria-label="close">×</button>', "<h3>🪙 " + esc(_t("helpTitle")) + "</h3>", "<div>" + esc(_t("helpIntro", {
      pts: pts,
      today: today,
      cap: cap
    })) + "</div>", "<ul>", "  <li>" + esc(_t("helpRule1")) + "</li>", "  <li>" + esc(_t("helpRule2")) + "</li>", "  <li>" + esc(_t("helpRule3", {
      cap: cap
    })) + "</li>", "  <li>" + esc(_t("helpRule4")) + "</li>", "  <li>" + esc(_t("helpRule5")) + "</li>", "</ul>", '<div class="veyra-coin-help-foot">' + esc(_t("helpDismissHint")) + "</div>", '<div class="veyra-coin-help-links">', '  <a href="https://veyra.trade/v2/about">' + esc(_t("helpLinkAbout")) + "</a>", '  <a href="https://veyra.trade/v2/privacy">' + esc(_t("helpLinkPrivacy")) + "</a>", '  <a href="https://veyra.trade/v2/terms">' + esc(_t("helpLinkTerms")) + "</a>", '  <a href="https://veyra.trade/v2/faq">' + esc(_t("helpLinkFaq")) + "</a>", "</div>"].join("");
  }
  function _refreshHelpOverlayTexts() {
    if (helpDom.panel && helpDom.panel.parentNode) {
      helpDom.panel.innerHTML = _renderHelpHtml();
      _bindHelpCloseBtn();
    }
    if (helpDom.badge) {
      helpDom.badge.setAttribute("aria-label", _t("helpReopenAria"));
      helpDom.badge.title = _t("helpReopenAria");
    }
  }
  function _bindHelpCloseBtn() {
    if (!helpDom.panel) return;
    var btn = helpDom.panel.querySelector(".veyra-coin-help-close");
    if (btn) btn.addEventListener("click", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      _collapseHelpToBadge();
    });
  }
  function _attachHelpDismissListeners() {
    if (helpDom.listeners) return;
    var handler = function () {
      _collapseHelpToBadge();
    };
    var events = ["mousedown", "keydown", "wheel", "touchstart"];
    events.forEach(function (ev) {
      window.addEventListener(ev, handler, true);
    });
    helpDom.listeners = {
      handler: handler,
      events: events
    };
  }
  function _detachHelpDismissListeners() {
    if (!helpDom.listeners) return;
    var h = helpDom.listeners.handler;
    helpDom.listeners.events.forEach(function (ev) {
      window.removeEventListener(ev, h, true);
    });
    helpDom.listeners = null;
  }
  function _positionHelpBadge() {
    if (!helpDom.badge) return;
    var anchor = document.querySelector(".absolute.top-4.right-4");
    if (anchor) {
      var rect = anchor.getBoundingClientRect();
      helpDom.badge.style.top = Math.max(8, rect.bottom + 8) + "px";
      helpDom.badge.style.right = Math.max(8, window.innerWidth - rect.right) + "px";
      helpDom.badge.style.left = "";
    } else {
      helpDom.badge.style.top = "96px";
      helpDom.badge.style.right = "16px";
      helpDom.badge.style.left = "";
    }
  }
  function _openHelpOverlay() {
    _helpInjectStylesOnce();
    if (helpDom.panel) {
      helpDom.panel.style.display = "";
      helpDom.panel.innerHTML = _renderHelpHtml();
      _bindHelpCloseBtn();
    } else {
      var el = document.createElement("div");
      el.className = "veyra-coin-help-panel";
      el.setAttribute("role", "dialog");
      el.setAttribute("aria-label", _t("helpTitle"));
      el.style.position = "fixed";
      el.innerHTML = _renderHelpHtml();
      document.body.appendChild(el);
      helpDom.panel = el;
      _bindHelpCloseBtn();
    }
    if (helpDom.badge) helpDom.badge.style.display = "none";
    _attachHelpDismissListeners();
    helpDom.dismissed = false;
  }
  function _collapseHelpToBadge() {
    _detachHelpDismissListeners();
    helpDom.dismissed = true;
    if (helpDom.panel) helpDom.panel.style.display = "none";
    if (!helpDom.badge) {
      _helpInjectStylesOnce();
      var b = document.createElement("button");
      b.type = "button";
      b.className = "veyra-coin-help-badge";
      b.textContent = "?";
      b.setAttribute("aria-label", _t("helpReopenAria"));
      b.title = _t("helpReopenAria");
      b.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        _openHelpOverlay();
      });
      document.body.appendChild(b);
      helpDom.badge = b;
    } else {
      helpDom.badge.style.display = "";
    }
    _positionHelpBadge();
  }
  function _waitForHelpOverlay() {
    var tryShow = function () {
      if (!document.body) return false;
      _openHelpOverlay();
      window.addEventListener("resize", _positionHelpBadge);
      var tries = 0;
      var iv = setInterval(function () {
        tries++;
        if (helpDom.dismissed) {
          _positionHelpBadge();
          clearInterval(iv);
          return;
        }
        if (tries > 60) clearInterval(iv);
      }, 250);
      return true;
    };
    if (!tryShow()) {
      document.addEventListener("DOMContentLoaded", tryShow, {
        once: true
      });
    }
  }
  function init() {
    var enabled = false;
    try {
      enabled = document.body && document.body.getAttribute("data-coin-drop-enabled") === "1";
      if (enabled) _waitForChest();
      if (enabled) _waitForHelpOverlay();
    } catch (e) { }
    if (!enabled) return;
    Promise.all([_loadTexts(), _refreshStatus()]).then(function () {
      _waitForVp();
      try {
        _refreshHelpOverlayTexts();
      } catch (e) { }
    }).catch(function () {
      _waitForVp();
    });
  }
  window.veyraCoinDrop = {
    __loaded: true,
    init: init,
    refreshStatus: _refreshStatus,
    attach: function (modalEl, opts) {
      if (!state.status) {
        return _refreshStatus().then(function () {
          return _attachToModal(modalEl, opts);
        });
      }
      return _attachToModal(modalEl, opts);
    },
    getStatus: function () {
      return state.status;
    },
    getTexts: function () {
      return state.texts;
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
