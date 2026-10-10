(function () {
  "use strict";
  function _t(key, fb) {
    try {
      if (window.veyraI18n && typeof window.veyraI18n.t === "function") return window.veyraI18n.t(key, fb);
    } catch (_e) { }
    return fb;
  }
  var PATH_WHITELIST = ["/", "", "/index.html", "/Index", "/index", "/Copytrader", "/copytrader", "/preview", "/favicon.ico", "/go", "/v2/go", "/faq", "/v2/faq", "/about", "/v2/about", "/privacy", "/v2/privacy", "/terms", "/v2/terms"];
  var ME_CACHE_KEY = "pm_guard_me_v1";
  var ME_CACHE_TTL_MS = 3e4;
  var SPINNER_DELAY_MS = 700;
  function isWhitelistedPath() {
    var p = (window.location.pathname || "/").replace(/\/+$/, "") || "/";
    if (PATH_WHITELIST.indexOf(p) !== -1) return true;
    if (PATH_WHITELIST.indexOf(window.location.pathname) !== -1) return true;
    if (/^\/(static|v2\/static|favicon)/.test(p)) return true;
    return false;
  }
  function isEventDetailPath() {
    var p = (window.location.pathname || "/").replace(/\/+$/, "") || "/";
    return /^\/(v2\/)?event\/.+/.test(p);
  }
  function readMeCache() {
    try {
      var raw = sessionStorage.getItem(ME_CACHE_KEY);
      if (!raw) return null;
      var obj = JSON.parse(raw);
      if (!obj || typeof obj.ts !== "number") return null;
      if (Date.now() - obj.ts > ME_CACHE_TTL_MS) return null;
      return obj.data || null;
    } catch (_) {
      return null;
    }
  }
  function writeMeCache(data) {
    try {
      sessionStorage.setItem(ME_CACHE_KEY, JSON.stringify({
        ts: Date.now(),
        data: data
      }));
    } catch (_) { }
  }
  function clearMeCache() {
    try {
      sessionStorage.removeItem(ME_CACHE_KEY);
    } catch (_) { }
  }
  async function getMe() {
    if (window.veyraGetMe) {
      try {
        var shared = await window.veyraGetMe();
        if (shared && shared.authenticated) writeMeCache(shared); else clearMeCache();
        return shared;
      } catch (_) { }
    }
    try {
      var r = await fetch("/api/user/me", {
        credentials: "include",
        cache: "no-store"
      });
      if (!r.ok) return {
        __status: r.status
      };
      var data = await r.json();
      if (data && data.authenticated) writeMeCache(data); else clearMeCache();
      return data;
    } catch (_) {
      return null;
    }
  }
  async function getStatus() {
    try {
      var r = await fetch("/api/status", {
        credentials: "include",
        cache: "no-store"
      });
      if (!r.ok) return null;
      return await r.json();
    } catch (_) {
      return null;
    }
  }
  try {
    window.__pmGuardClearCache = clearMeCache;
  } catch (_) { }
  function injectStyles() {
    if (document.getElementById("pm-guard-style")) return;
    var s = document.createElement("style");
    s.id = "pm-guard-style";
    s.textContent = ["#pm-guard-loading{position:fixed;top:16px;right:16px;z-index:10000;background:rgba(8,12,20,0.85);display:flex;align-items:center;justify-content:center;color:#9bb0d6;font-size:12px;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:8px 14px;border-radius:10px;border:1px solid #2a3b59;box-shadow:0 6px 24px rgba(0,0,0,.4);}", "#pm-guard-loading .spinner{width:14px;height:14px;border:2px solid #2a3b59;border-top-color:#5a7bff;border-radius:50%;animation:pmgspin 0.8s linear infinite;margin-right:8px;}", "@keyframes pmgspin{to{transform:rotate(360deg)}}", ".pm-guard-overlay{position:fixed;inset:0;z-index:9999;background:rgba(8,12,20,0.78);display:flex;align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(2px);}", ".pm-guard-card{max-width:560px;width:100%;background:#162031;border:1px solid #2a3b59;border-radius:14px;padding:24px;color:#e6edf7;box-shadow:0 12px 50px rgba(0,0,0,.5);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;}", ".pm-guard-title{font-size:18px;font-weight:700;margin-bottom:10px;display:flex;align-items:center;gap:10px;}", ".pm-guard-desc{font-size:13px;line-height:1.65;color:#c4d0e6;margin-bottom:18px}", ".pm-guard-actions{display:flex;gap:10px;flex-wrap:wrap}", ".pm-guard-btn{display:inline-flex;align-items:center;justify-content:center;padding:10px 16px;border-radius:10px;font-size:13px;font-weight:700;text-decoration:none;cursor:pointer;border:none;transition:opacity .15s;}", ".pm-guard-btn:disabled{opacity:0.55;cursor:wait}", ".pm-guard-btn-primary{background:#5a7bff;color:#fff}", ".pm-guard-btn-primary:hover:not(:disabled){background:#6b8aff}", ".pm-guard-btn-secondary{background:transparent;color:#d3ddf1;border:1px solid #344868}", ".pm-guard-btn-secondary:hover{background:rgba(255,255,255,0.04)}", ".pm-guard-status{margin-top:14px;padding:10px 12px;border-radius:8px;font-size:12px;line-height:1.55;display:none;}", ".pm-guard-status.show{display:block}", ".pm-guard-status.info{background:rgba(90,123,255,0.12);color:#b8c8ff;border:1px solid rgba(90,123,255,0.3)}", ".pm-guard-status.warn{background:rgba(245,158,11,0.12);color:#fbd38d;border:1px solid rgba(245,158,11,0.3)}", ".pm-guard-status.err{background:rgba(239,68,68,0.12);color:#fca5a5;border:1px solid rgba(239,68,68,0.3)}", ".pm-guard-status.ok{background:rgba(34,197,94,0.12);color:#86efac;border:1px solid rgba(34,197,94,0.3)}", ".pm-guard-addr{font-family:ui-monospace,SFMono-Regular,monospace;font-size:11px;background:rgba(255,255,255,0.04);padding:4px 8px;border-radius:6px;display:inline-block;margin-top:6px;word-break:break-all;}", ".maint-guard-overlay{position:fixed;inset:0;z-index:10040;background:rgba(10,14,24,0.72);display:flex;align-items:center;justify-content:center;padding:20px;}", ".maint-guard-card{max-width:560px;width:100%;background:#1a2335;border:1px solid #2f4265;border-radius:14px;padding:22px;color:#e6edf7;box-shadow:0 18px 56px rgba(0,0,0,.5)}", ".maint-guard-title{font-size:18px;font-weight:700;margin-bottom:8px;color:#ffd26d}", ".maint-guard-desc{font-size:13px;line-height:1.65;color:#d2dcef;margin-bottom:16px}", ".maint-guard-actions{display:flex;gap:10px;flex-wrap:wrap}", ".maint-guard-btn{display:inline-flex;align-items:center;justify-content:center;padding:10px 14px;border-radius:10px;font-size:13px;font-weight:700;text-decoration:none;cursor:pointer;border:none}", ".maint-guard-btn-primary{background:#f59e0b;color:#111827}", ".maint-guard-btn-secondary{background:transparent;color:#d3ddf1;border:1px solid #40577f}"].join("");
    document.head.appendChild(s);
  }
  function injectLoadingOverlay() {
    if (document.getElementById("pm-guard-loading")) return;
    injectStyles();
    var div = document.createElement("div");
    div.id = "pm-guard-loading";
    div.innerHTML = '<div class="spinner"></div><span>' + _t("pmGuard.loading.checking", "Checking…") + "</span>";
    (document.body || document.documentElement).appendChild(div);
  }
  function removeLoadingOverlay() {
    var el = document.getElementById("pm-guard-loading");
    if (el) el.remove();
  }
  function setStatus(type, text) {
    var box = document.getElementById("pm-guard-status");
    if (!box) return;
    box.className = "pm-guard-status show " + type;
    box.innerHTML = text;
  }
  function showOverlay(opts) {
    opts = opts || {};
    if (document.getElementById("pm-guard-overlay")) return;
    injectStyles();
    document.documentElement.classList.remove("pm-guard-pending");
    removeLoadingOverlay();
    var titleText = opts.title || _t("pmGuard.overlay.bindTitle", "Please link your Polymarket funding wallet");
    var descText = opts.desc || _t("pmGuard.overlay.bindDesc", "For fund safety, the PM funding wallet is auto-derived and deployed via Gnosis Safe. Click the button below to link it automatically; if you are not logged in, you will be redirected to the login page.");
    var overlay = document.createElement("div");
    overlay.id = "pm-guard-overlay";
    overlay.className = "pm-guard-overlay";
    overlay.innerHTML = ['<div class="pm-guard-card">', '<div class="pm-guard-title">🔐 ' + titleText + "</div>", '<div class="pm-guard-desc">' + descText + "</div>", '<div class="pm-guard-actions">', '<button class="pm-guard-btn pm-guard-btn-primary" id="pm-guard-bind-btn">' + _t("pmGuard.overlay.btnAuthorize", "Authorize wallet linking") + "</button>", '<a class="pm-guard-btn pm-guard-btn-secondary" href="/v2/go">' + _t("pmGuard.overlay.btnGuide", "View guide") + "</a>", "</div>", '<div class="pm-guard-status" id="pm-guard-status"></div>', "</div>"].join("");
    document.body.appendChild(overlay);
    var btn = document.getElementById("pm-guard-bind-btn");
    if (btn) {
      btn.addEventListener("click", async function () {
        if (opts.notAuthed) {
          window.location.href = "/Index";
          return;
        }
        btn.disabled = true;
        btn.textContent = _t("pmGuard.overlay.btnLinking", "Linking…");
        setStatus("info", _t("pmGuard.overlay.statusLinking", "Discovering funding wallet via Polymarket on-chain data, please wait…"));
        try {
          var r = await fetch("/api/pm/bind-wallet", {
            method: "POST",
            credentials: "include",
            headers: {
              "Content-Type": "application/json"
            },
            body: JSON.stringify({
              force: true
            })
          });
          var data = await r.json();
          if (r.ok && data.status === "ok" && data.funder_address) {
            setStatus("ok", _t("pmGuard.overlay.statusOk", "Linked successfully! Funding wallet: ") + (data.funder_address || "—"));
            setTimeout(function () {
              window.location.reload();
            }, 1200);
          } else if (data.status === "needs_funding" || data.needs_pm_binding) {
            setStatus("warn", (data.hint || _t("pmGuard.overlay.statusNeedsFunding", "No deployed Gnosis Safe found. Please first deposit native USDC on Polygon to the system-derived Safe address; the Polymarket contract will auto-deploy on first deposit.")) + (data.funder_address ? '<div class="pm-guard-addr">' + data.funder_address + "</div>" : ""));
            btn.disabled = false;
            btn.textContent = _t("pmGuard.overlay.btnRetry", "Retry linking");
          } else {
            setStatus("err", _t("pmGuard.overlay.statusErrLink", "Linking failed: ") + (data.error || data.hint || "HTTP " + r.status));
            btn.disabled = false;
            btn.textContent = _t("pmGuard.overlay.btnRetry", "Retry linking");
          }
        } catch (e) {
          setStatus("err", _t("pmGuard.overlay.statusErrNet", "Network error: ") + (e && e.message ? e.message : String(e)));
          btn.disabled = false;
          btn.textContent = _t("pmGuard.overlay.btnRetry", "Retry linking");
        }
      });
    }
  }
  function showMaintenanceOverlay(reason) {
    if (document.getElementById("maintenance-guard-overlay")) return;
    injectStyles();
    var message = (reason || "").trim() ? _t("pmGuard.maintenance.descPrefix", "System under maintenance: ") + reason : _t("pmGuard.maintenance.descDefault", "System is under maintenance; some trading features may be temporarily unavailable.");
    var overlay = document.createElement("div");
    overlay.id = "maintenance-guard-overlay";
    overlay.className = "maint-guard-overlay";
    overlay.innerHTML = ['<div class="maint-guard-card">', '<div class="maint-guard-title">' + _t("pmGuard.maintenance.title", "System maintenance notice") + "</div>", '<div class="maint-guard-desc">' + message + "</div>", '<div class="maint-guard-actions">', '<button class="maint-guard-btn maint-guard-btn-primary" id="maint-guard-ok">' + _t("pmGuard.maintenance.btnAck", "Got it") + "</button>", '<a class="maint-guard-btn maint-guard-btn-secondary" href="/v2">' + _t("pmGuard.maintenance.btnDashboard", "Back to dashboard") + "</a>", "</div>", "</div>"].join("");
    document.body.appendChild(overlay);
    var ok = document.getElementById("maint-guard-ok");
    if (ok) ok.addEventListener("click", function () {
      var el = document.getElementById("maintenance-guard-overlay");
      if (el) el.remove();
    });
  }
  function clearGuards() {
    document.documentElement.classList.remove("pm-guard-pending");
    document.documentElement.classList.add("pm-guard-resolved");
    removeLoadingOverlay();
  }
  function _setAuthState(authed) {
    try {
      var v = (authed === true);
      if (window.__pmGuestMode !== !v) {
        window.__pmGuestMode = !v;
        try { window.dispatchEvent(new CustomEvent("pm:auth-resolved", { detail: { authed: v } })); } catch (_) { }
      }
    } catch (_) { }
  }
  function evaluateMe(me, st) {
    var _authed = !!(me && me.authenticated);
    _setAuthState(_authed);
    if (me && me.authenticated && !me.is_admin && st && st.maintenance_mode) {
      showMaintenanceOverlay(st.maintenance_reason || "");
    }
    if (me && me.authenticated && me.is_admin) {
      clearGuards();
      return;
    }
    if (!me || !me.authenticated || me.__status === 401) {
      if (isEventDetailPath()) {
        window.__pmShowLoginRequired = function (opts) {
          var o = opts || {};
          showOverlay({
            notAuthed: true,
            title: o.title || _t("pmGuard.eventGuest.title", "Sign in to place orders"),
            desc: o.desc || _t("pmGuard.eventGuest.desc", "Create an account or sign in to start trading in prediction markets.")
          });
        };
        clearGuards();
        return;
      }
      if (!evaluateMe._retried) {
        evaluateMe._retried = true;
        setTimeout(function () {
          (window.veyraGetMe ? window.veyraGetMe({
            force: true
          }) : Promise.resolve(null)).then(function (fresh) {
            if (fresh && fresh.authenticated) {
              evaluateMe(fresh, st);
            } else {
              clearMeCache();
              showOverlay({
                notAuthed: true,
                title: _t("pmGuard.login.title", "Please log in"),
                desc: _t("pmGuard.login.desc", "This page requires login. Click the button below to go to the login page.")
              });
            }
          }).catch(function () {
            clearMeCache();
            showOverlay({
              notAuthed: true,
              title: _t("pmGuard.login.title", "Please log in"),
              desc: _t("pmGuard.login.desc", "This page requires login. Click the button below to go to the login page.")
            });
          });
        }, 700);
        return;
      }
      clearMeCache();
      showOverlay({
        notAuthed: true,
        title: _t("pmGuard.login.title", "Please log in"),
        desc: _t("pmGuard.login.desc", "This page requires login. Click the button below to go to the login page.")
      });
      return;
    }
    evaluateMe._retried = false;
    var bound = !!(me.funder_address || me.polymarket_wallet || me.pm_wallet_address);
    if (!bound) {
      if (!evaluateMe._bindAttempted) {
        evaluateMe._bindAttempted = true;
        (async function () {
          try {
            var br = await fetch("/api/pm/bind-wallet", {
              method: "POST",
              credentials: "include",
              headers: {
                "Content-Type": "application/json"
              },
              body: JSON.stringify({
                force: false
              })
            });
            if (br.ok) {
              var bd = await br.json();
              if (bd && bd.funder_address) {
                clearMeCache();
                window.location.reload();
                return;
              }
            }
          } catch (_e) { }
          _showNewUserWalletBanner();
          clearGuards();
        })();
        return;
      }
      _showNewUserWalletBanner();
      clearGuards();
      return;
    }
    clearGuards();
  }
  function _showNewUserWalletBanner() {
    if (document.getElementById("pm-new-user-banner")) return;
    injectStyles();
    var d = document.createElement("div");
    d.id = "pm-new-user-banner";
    d.style.cssText = ["position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:9998;", "max-width:480px;width:calc(100% - 32px);background:#162031;", "border:1px solid #2a3b59;border-radius:10px;padding:12px 16px;", "color:#c4d0e6;font-size:13px;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;", "display:flex;align-items:center;gap:12px;box-shadow:0 6px 24px rgba(0,0,0,.4);"].join("");
    var msg = document.createElement("span");
    msg.innerHTML = "💡 " + _t("pmGuard.newUser.notice", 'Your trading wallet is ready to activate. <a href="/v2/deposit" style="color:#5a7bff;text-decoration:none;">Deposit to activate →</a>');
    d.appendChild(msg);
    var closeBtn = document.createElement("button");
    closeBtn.style.cssText = "margin-left:auto;background:none;border:none;color:#8ba0c4;cursor:pointer;font-size:18px;padding:0 4px;line-height:1;";
    closeBtn.textContent = "×";
    closeBtn.addEventListener("click", function () {
      d.remove();
    });
    d.appendChild(closeBtn);
    document.body.appendChild(d);
    setTimeout(function () {
      var el = document.getElementById("pm-new-user-banner");
      if (el) el.remove();
    }, 1e4);
  }
  async function bootstrapGuard() {
    if (isWhitelistedPath()) {
      clearGuards();
      return;
    }
    var cached = readMeCache();
    if (cached && cached.authenticated) {
      evaluateMe(cached, null);
      Promise.all([getMe(), getStatus()]).then(function (arr) {
        var freshMe = arr[0];
        var freshSt = arr[1];
        var stillBound = freshMe && freshMe.authenticated && (freshMe.funder_address || freshMe.polymarket_wallet || freshMe.pm_wallet_address);
        var needRecheck = !stillBound || freshSt && freshSt.maintenance_mode && freshMe && !freshMe.is_admin;
        if (needRecheck) {
          var ov = document.getElementById("pm-guard-overlay");
          if (ov) ov.remove();
          var mt = document.getElementById("maintenance-guard-overlay");
          if (mt) mt.remove();
          evaluateMe(freshMe, freshSt);
        }
      }).catch(function () { });
      return;
    }
    var spinnerTimer = setTimeout(injectLoadingOverlay, SPINNER_DELAY_MS);
    try {
      var results = await Promise.all([getMe(), getStatus()]);
      clearTimeout(spinnerTimer);
      removeLoadingOverlay();
      evaluateMe(results[0], results[1]);
    } catch (_) {
      clearTimeout(spinnerTimer);
      removeLoadingOverlay();
      evaluateMe(null, null);
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrapGuard);
  } else {
    bootstrapGuard();
  }
})();