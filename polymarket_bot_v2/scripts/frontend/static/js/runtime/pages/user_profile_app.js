(function () {
  "use strict";
  function T(key, fallback) {
    return window.veyraI18n && window.veyraI18n.t ? window.veyraI18n.t(key, fallback) : fallback;
  }
  function $(id) {
    return document.getElementById(id);
  }
  function show(id, visible) {
    var el = $(id);
    if (el) el.style.display = visible ? "" : "none";
  }
  function text(id, val) {
    var el = $(id);
    if (el) el.textContent = String(val == null ? "" : val);
  }
  function escAttr(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  var state = {
    user: {},
    wallet: {
      pm_bound: false,
      pm_address: ""
    },
    editName: "",
    saving: false,
    saveMsg: "",
    loading: true,
    mfa: {
      enabled: false,
      methods: [],
      last_verified_at: null,
      verified_in_session: false,
      busy: false,
      msg: "",
      msgType: "info"
    },
    _mfaRuntimeInitPromise: null,
    _mfaPostEnrollSyncPromise: null
  };
  function mfaMethodsLabel() {
    var map = {
      sms: T("user.mfa.method.sms", "SMS"),
      totp: T("user.mfa.method.totp", "TOTP"),
      passkey: T("user.mfa.method.passkey", "Passkey")
    };
    var arr = Array.isArray(state.mfa.methods) ? state.mfa.methods : [];
    if (!arr.length) return T("user.mfa.method.none", "(none)");
    return arr.map(function (m) {
      return map[String(m).toLowerCase()] || m;
    }).join(" · ");
  }
  function maskAddress(addr) {
    var s = String(addr || "").trim();
    if (!s) return "";
    if (s.length <= 12) return s;
    return s.slice(0, 6) + "..." + s.slice(-4);
  }
  function render() {
    show("up-skel", state.loading);
    show("up-main", !state.loading);
    if (state.loading) return;
    var avatarWrap = $("up-avatar-wrap");
    if (avatarWrap) {
      if (state.user.avatar_url) {
        var av = state.user.avatar_url;
        var src = av.startsWith("<svg") ? "data:image/svg+xml," + encodeURIComponent(av) : av;
        avatarWrap.innerHTML = '<img src="' + escAttr(src) + '" style="width:100%;height:100%;object-fit:cover;border-radius:50%" alt="avatar">';
      } else {
        avatarWrap.innerHTML = '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';
      }
    }
    text("up-display-name", state.user.display_name || state.user.username || T("user.profile.defaultName", "User"));
    text("up-wallet-short", state.user.wallet_short || maskAddress(state.user.wallet_address) || "");
    var roleBadge = $("up-role-badge");
    if (roleBadge) {
      var role = String(state.user.role || "");
      roleBadge.className = "badge " + (role === "admin" || role === "super_admin" ? "badge-warning" : "badge-info");
      roleBadge.textContent = role === "super_admin" ? T("user.role.super_admin", "Super Admin") : role === "admin" ? T("user.role.admin", "Admin") : T("user.role.user", "User");
    }
    show("up-active-badge", !!state.user.is_active);
    var createdEl = $("up-created-at");
    if (createdEl) {
      createdEl.textContent = typeof fmtDate === "function" ? fmtDate(state.user.created_at) : String(state.user.created_at || "");
    }
    var editInput = $("up-edit-name");
    if (editInput) {
      editInput.placeholder = state.user.display_name || T("user.profile.displayNamePlaceholder", "Enter display name");
    }
    var authWalletDisplay = $("up-auth-wallet-display");
    if (authWalletDisplay) {
      authWalletDisplay.textContent = maskAddress(state.user.wallet_address) || T("user.profile.walletUnbound", "No wallet linked");
    }
    var copyAuthBtn = $("up-copy-auth-wallet-btn");
    if (copyAuthBtn) copyAuthBtn.disabled = !state.user.wallet_address;
    text("up-save-msg", state.saveMsg);
    var saveBtn = $("up-save-btn");
    if (saveBtn) saveBtn.disabled = state.saving;
    show("up-save-spinner", state.saving);
    var saveLabel = $("up-save-label");
    if (saveLabel) saveLabel.style.display = state.saving ? "none" : "";
    show("up-wallet-bound", state.wallet.pm_bound);
    show("up-wallet-unbound", !state.wallet.pm_bound);
    if (state.wallet.pm_bound) {
      var pmAddrDisplay = $("up-pm-address-display");
      if (pmAddrDisplay) pmAddrDisplay.textContent = maskAddress(state.wallet.pm_address) || "-";
      var copyPmBtn = $("up-copy-pm-wallet-btn");
      if (copyPmBtn) copyPmBtn.disabled = !state.wallet.pm_address;
    }
    show("up-mfa-enabled-banner", state.mfa.enabled);
    show("up-mfa-disabled-banner", !state.mfa.enabled);
    text("up-mfa-methods-label", state.mfa.enabled ? mfaMethodsLabel() : "");
    show("up-mfa-enroll-wrap", !state.mfa.enabled);
    show("up-mfa-manage-wrap", state.mfa.enabled);
    var mfaEnrollBtn = $("up-mfa-enroll-btn");
    if (mfaEnrollBtn) mfaEnrollBtn.disabled = state.mfa.busy;
    var mfaManageBtn = $("up-mfa-manage-btn");
    if (mfaManageBtn) mfaManageBtn.disabled = state.mfa.busy;
    var mfaMsgWrap = $("up-mfa-msg-wrap");
    if (mfaMsgWrap) {
      if (state.mfa.msg) {
        var bg = state.mfa.msgType === "err" ? "background:rgba(239,68,68,0.10);color:rgb(239,68,68)" : state.mfa.msgType === "ok" ? "background:rgba(34,197,94,0.10);color:rgb(34,197,94)" : "background:rgba(70,95,255,0.10);color:var(--accent)";
        mfaMsgWrap.setAttribute("style", bg);
        mfaMsgWrap.style.display = "";
      } else {
        mfaMsgWrap.style.display = "none";
      }
    }
    text("up-mfa-msg", state.mfa.msg);
  }
  async function refreshMfaStatus() {
    try {
      var s = await vJson("/api/user/mfa/status");
      if (!s || typeof s !== "object") return;
      state.mfa.enabled = !!s.enabled;
      state.mfa.methods = Array.isArray(s.methods) ? s.methods : [];
      state.mfa.last_verified_at = s.last_verified_at || null;
      state.mfa.verified_in_session = !!s.verified_in_session;
    } catch (_) { }
  }
  async function init() {
    if (window.veyraI18n && typeof window.veyraI18n.registerPagePack === 'function') {
      try { await window.veyraI18n.registerPagePack('user'); } catch (_) { }
    }
    state.loading = true;
    render();
    try {
      var me = window.veyraGetMe ? await window.veyraGetMe() : await vJson("/api/user/me");
      if (me) {
        state.user = me;
        state.editName = me.display_name || "";
        var pmAddress = me.polymarket_wallet || me.pm_wallet_address || me.funder_address || "";
        state.wallet.pm_bound = !!pmAddress;
        state.wallet.pm_address = pmAddress;
        state.mfa.enabled = !!me.mfa_enabled;
        state.mfa.methods = Array.isArray(me.mfa_methods) ? me.mfa_methods : [];
        state.mfa.last_verified_at = me.mfa_last_verified_at || null;
        state.mfa.verified_in_session = !!me.mfa_verified_in_session;
      }
      await refreshMfaStatus();
    } finally {
      state.loading = false;
      render();
    }
    if (window.lucide) lucide.createIcons();
  }
  function scheduleMfaPrewarm() {
    if (state._mfaRuntimeInitPromise) return;
    if (window.__veyraPrivySigningRuntime && window.__veyraPrivySigningRuntime._bootPromise) {
      return;
    }
    var run = function () {
      if (window.__veyraPrivySigningRuntime && window.__veyraPrivySigningRuntime._bootPromise) {
        return;
      }
      ensurePrivyMfaRuntime().catch(function () { });
    };
    if (window.requestIdleCallback) {
      window.requestIdleCallback(run, {
        timeout: 2500
      });
      return;
    }
    setTimeout(run, 1200);
  }
  async function ensurePrivyMfaRuntime() {
    if (window.__veyraPrivyMfaRuntime && typeof window.__veyraPrivyMfaRuntime.run === "function") {
      return;
    }
    if (state._mfaRuntimeInitPromise) {
      await state._mfaRuntimeInitPromise;
      return;
    }
    state._mfaRuntimeInitPromise = (async () => {
      var host = document.getElementById("privy-mfa-runtime-root");
      if (!host) {
        throw new Error(T("user.mfa.err.containerMissing", "MFA container not found, please refresh the page."));
      }
      var cfg = await vJson("/api/user/privy/config");
      if (!cfg || !cfg.enabled || !cfg.app_id) {
        throw new Error(T("user.mfa.err.privyNotConfigured", "Privy is not configured. Please ask an administrator to set PRIVY_APP_ID / PRIVY_APP_SECRET."));
      }
      if (!window.__veyraPrivyMfaModulesPromise) {
        var assetVersion = encodeURIComponent(document.documentElement && document.documentElement.getAttribute("data-asset-version") || "0");
        window.__veyraPrivyMfaModulesPromise = import("/v2/static/js/vendor/mfa_runtime_bundle.mjs?v=" + assetVersion);
      }
      var neutralizeWalletConnectOverlays = function () {
        try {
          document.querySelectorAll("w3m-modal, w3m-router, w3m-toast, appkit-modal").forEach(function (node) {
            node.style.pointerEvents = "none";
            node.style.zIndex = "0";
          });
        } catch (_) { }
      };
      var modules = await window.__veyraPrivyMfaModulesPromise;
      var React = modules.React;
      var ReactDOM = modules.ReactDOMClient;
      var PrivyReact = modules.PrivyReact;
      var createElement = React.createElement;
      var useEffect = React.useEffect;
      var createRoot = ReactDOM.createRoot;
      var PrivyProvider = PrivyReact.PrivyProvider;
      var usePrivy = PrivyReact.usePrivy;
      var useMfaEnrollment = PrivyReact.useMfaEnrollment;
      var useMfa = PrivyReact.useMfa;
      var runtime = window.__veyraPrivyMfaRuntime || {};
      runtime.status = runtime.status || {
        ready: false,
        authenticated: false
      };
      window.__veyraPrivyMfaRuntime = runtime;
      function MfaRuntimeInner() {
        var privy = usePrivy();
        var ready = privy.ready, authenticated = privy.authenticated, user = privy.user;
        var mfaEnrollment = useMfaEnrollment();
        var showMfaEnrollmentModal = mfaEnrollment.showMfaEnrollmentModal;
        var unenrollWithSms = mfaEnrollment.unenrollWithSms;
        var unenrollWithTotp = mfaEnrollment.unenrollWithTotp;
        var unenrollWithPasskey = mfaEnrollment.unenrollWithPasskey;
        var mfaHooks = useMfa();
        var mfaInit = mfaHooks.init;
        var mfaCancel = mfaHooks.cancel;
        var promptMfa = mfaHooks.promptMfa, showMfaPromptModal = mfaHooks.showMfaPromptModal;
        var normalizeMethods = function (arr) {
          if (!Array.isArray(arr)) return [];
          var out = [];
          arr.forEach(function (m) {
            var k = String(m || "").toLowerCase();
            if (!k || out.includes(k)) return;
            out.push(k);
          });
          return out;
        };
        var methodsFromUser = function () {
          return normalizeMethods((privy.user || user || {}).mfaMethods || []);
        };
        useEffect(function () {
          runtime.run = async function (action) {
            var waitForPrivyReady = async function () {
              for (var i = 0; i < 80; i++) {
                var status = runtime.status || {};
                if (status.ready && status.authenticated) return;
                await new Promise(function (resolve) {
                  setTimeout(resolve, 250);
                });
              }
              var s = runtime.status || {};
              if (!s.ready) throw new Error(T("user.mfa.err.privyInitTimeout", "Privy init timeout, please refresh and retry."));
              throw new Error(T("user.mfa.err.privySessionNotReady", "Privy session is not ready. Please sign in and retry."));
            };
            var openOfficialVerifyPrompt = async function (method) {
              var promptFn = typeof promptMfa === "function" ? promptMfa : typeof showMfaPromptModal === "function" ? showMfaPromptModal : null;
              if (typeof promptFn !== "function") {
                throw new Error(T("user.mfa.err.sdkNoMfaPrompt", "Current Privy SDK does not support an MFA prompt modal. Please ask an administrator to upgrade."));
              }
              if (method && typeof mfaInit === "function") {
                try {
                  await mfaInit(method);
                } catch (_) {
                  try {
                    await mfaInit({
                      method: method
                    });
                  } catch (_) { }
                }
              }
              await promptFn();
            };
            await waitForPrivyReady();
            if (action === "enroll") {
              if (typeof showMfaEnrollmentModal !== "function") {
                throw new Error(T("user.mfa.err.sdkNoMfaPrompt", "Current Privy SDK does not support an MFA prompt modal. Please ask an administrator to upgrade."));
              }
              try {
                if (typeof mfaCancel === "function") mfaCancel();
              } catch (_) { }
              showMfaEnrollmentModal();
              return {
                ok: true,
                action: "enroll_opened",
                methods: methodsFromUser()
              };
            }
            if (action === "unenroll") {
              var methods = methodsFromUser();
              for (var i = 0; i < methods.length; i++) {
                var mm = methods[i];
                if (mm === "sms" && typeof unenrollWithSms === "function") await unenrollWithSms();
                if (mm === "totp" && typeof unenrollWithTotp === "function") await unenrollWithTotp();
                if (mm === "passkey" && typeof unenrollWithPasskey === "function") await unenrollWithPasskey();
              }
              return {
                ok: true,
                action: "unenroll",
                methods: []
              };
            }
            try {
              if (typeof mfaCancel === "function") mfaCancel();
            } catch (_) { }
            await Promise.race([openOfficialVerifyPrompt(methodsFromUser()[0] || ""), new Promise(function (_, reject) {
              setTimeout(function () {
                reject(new Error(T("user.mfa.err.verifyTimeout", "MFA verification timeout, please retry.")));
              }, 45e3);
            })]);
            return {
              ok: true,
              action: action || "verify",
              methods: methodsFromUser()
            };
          };
          runtime.cancel = function () {
            try {
              if (typeof mfaCancel === "function") mfaCancel();
            } catch (_) { }
          };
          return function () {
            try {
              delete runtime.run;
            } catch (_) {
              runtime.run = undefined;
            }
            try {
              delete runtime.cancel;
            } catch (_) {
              runtime.cancel = undefined;
            }
          };
        }, [ready, authenticated, user, promptMfa, showMfaPromptModal, showMfaEnrollmentModal, mfaInit, unenrollWithSms, unenrollWithTotp, unenrollWithPasskey, mfaCancel]);
        useEffect(function () {
          runtime.status = {
            ready: !!ready,
            authenticated: !!authenticated,
            methods: methodsFromUser()
          };
        }, [ready, authenticated, user]);
        return createElement(React.Fragment, null);
      }
      function MfaRuntimeRoot() {
        return createElement(PrivyProvider, {
          appId: cfg.app_id,
          config: {
            loginMethods: ["email"],
            appearance: {
              theme: "dark",
              accentColor: "#0ea5e9",
              showWalletLoginFirst: false,
              walletChainType: "ethereum-only",
              walletList: ["detected_ethereum_wallets", "metamask", "coinbase_wallet", "rainbow"]
            },
            externalWallets: {
              disableAllExternalWallets: true,
              walletConnect: {
                enabled: false
              }
            },
            embeddedWallets: {
              showWalletUIs: false
            }
          }
        }, createElement(MfaRuntimeInner));
      }
      if (!runtime.root) {
        runtime.root = createRoot(host);
        runtime.root.render(createElement(MfaRuntimeRoot));
        neutralizeWalletConnectOverlays();
        if (!window.__veyraPrivyW3mOverlayObserver) {
          window.__veyraPrivyW3mOverlayObserver = new MutationObserver(neutralizeWalletConnectOverlays);
          window.__veyraPrivyW3mOverlayObserver.observe(document.body, {
            childList: true,
            subtree: true
          });
        }
      }
      for (var i = 0; i < 80; i++) {
        if (typeof runtime.run === "function") return;
        await new Promise(function (resolve) {
          setTimeout(resolve, 50);
        });
      }
      throw new Error(T("user.mfa.err.runtimeInitFailed", "MFA component failed to initialise; please refresh the page."));
    })();
    try {
      await state._mfaRuntimeInitPromise;
    } finally {
      state._mfaRuntimeInitPromise = null;
    }
  }
  async function openMfaPopup(action) {
    if (state.mfa.busy) return;
    state.mfa.busy = true;
    state.mfa.msg = "";
    state.mfa.msgType = "info";
    render();
    try {
      await ensurePrivyMfaRuntime();
      var runtime = window.__veyraPrivyMfaRuntime || {};
      if (typeof runtime.run !== "function") {
        throw new Error(T("user.mfa.err.runtimeNotReady", "MFA component is not ready, please refresh the page."));
      }
      state.mfa.msg = "";
      render();
      var result = await Promise.race([runtime.run(action), new Promise(function (_, reject) {
        setTimeout(function () {
          reject(new Error(T("user.mfa.err.opTimeout", "MFA action timeout, please retry.")));
        }, 12e4);
      })]);
      if (action === "verify") {
        var verifyResp = await vFetch("/api/user/mfa/verify-mark", {
          method: "POST"
        });
        if (!verifyResp || !verifyResp.ok) throw new Error(T("user.mfa.err.verifyMarkFailed", "Failed to mark MFA verification."));
      } else if (action === "enroll") {
        state.mfa.msg = T("user.mfa.flow.enrollOpened", "Privy official MFA window has been opened. Complete setup there, then return to this page.");
        state.mfa.msgType = "info";
        await refreshMfaStatus();
        schedulePostEnrollSync(runtime);
      } else {
        var methods = Array.isArray(result && result.methods) ? result.methods : [];
        var syncResp = await vFetch("/api/user/mfa/sync", {
          method: "POST",
          body: JSON.stringify({
            methods: methods
          })
        });
        if (!syncResp || !syncResp.ok) throw new Error(T("user.mfa.err.syncFailed", "Failed to sync MFA state."));
      }
      if (action !== "enroll") {
        await refreshMfaStatus();
        state.mfa.msg = action === "verify" ? T("user.mfa.success.verifyOk", "MFA verified (valid for 30 minutes).") : T("user.mfa.success.configUpdated", "MFA configuration updated.");
        state.mfa.msgType = "ok";
      }
    } catch (e) {
      state.mfa.msg = T("user.mfa.err.opFailedPrefix", "Action failed:") + " " + (e && e.message ? e.message : String(e));
      state.mfa.msgType = "err";
    } finally {
      state.mfa.busy = false;
      render();
    }
  }
  function normalizeMethods(methods) {
    if (!Array.isArray(methods)) return [];
    var out = [];
    var allow = {
      sms: true,
      totp: true,
      passkey: true
    };
    methods.forEach(function (m) {
      var k = String(m || "").trim().toLowerCase();
      if (!allow[k]) return;
      if (!out.includes(k)) out.push(k);
    });
    return out;
  }
  function runtimeMfaMethods(runtime) {
    var statusMethods = runtime && runtime.status ? runtime.status.methods : [];
    return normalizeMethods(statusMethods || []);
  }
  function schedulePostEnrollSync(runtime) {
    if (state._mfaPostEnrollSyncPromise) return;
    state._mfaPostEnrollSyncPromise = async function () {
      var started = Date.now();
      while (Date.now() - started < 12e4) {
        var methods = runtimeMfaMethods(runtime);
        if (methods.length) {
          var syncResp = await vFetch("/api/user/mfa/sync", {
            method: "POST",
            body: JSON.stringify({
              methods: methods
            })
          });
          if (syncResp && syncResp.ok) {
            await refreshMfaStatus();
            if (state.mfa.enabled) {
              state.mfa.msg = T("user.mfa.success.configUpdated", "MFA configuration updated.");
              state.mfa.msgType = "ok";
              render();
              return;
            }
          }
        } else {
          await refreshMfaStatus();
          if (state.mfa.enabled) {
            state.mfa.msg = T("user.mfa.success.configUpdated", "MFA configuration updated.");
            state.mfa.msgType = "ok";
            render();
            return;
          }
        }
        await new Promise(function (resolve) {
          setTimeout(resolve, 1500);
        });
      }
      await refreshMfaStatus();
      if (!state.mfa.enabled) {
        state.mfa.msg = T("user.mfa.flow.enrollPending", "Enrollment may still be processing. Please wait a moment and click refresh status by reopening this page.");
        state.mfa.msgType = "info";
        render();
      }
    }().finally(function () {
      state._mfaPostEnrollSyncPromise = null;
    });
  }
  async function saveName() {
    var editInput = $("up-edit-name");
    state.editName = editInput ? editInput.value : state.editName;
    var normalizedName = String(state.editName || "").normalize("NFKC").replace(/[\u0000-\u001F\u007F]/g, "").replace(/\s+/g, " ").trim();
    if (!normalizedName) {
      state.saveMsg = T("user.profile.displayNameRequired", "Display name is required.");
      render();
      return;
    }
    if (normalizedName.length < 2 || normalizedName.length > 40) {
      state.saveMsg = T("user.error.invalidNameLength", "Display name must be 2-40 characters.");
      render();
      return;
    }
    if (/[<>{}\[\]()`$;:&|/"\\]/u.test(normalizedName)) {
      state.saveMsg = T("user.error.invalidNameChars", "Display name contains forbidden characters.");
      render();
      return;
    }
    if (!/^[\p{L}\p{N} ._'-]+$/u.test(normalizedName)) {
      state.saveMsg = T("user.error.invalidNameFormat", "Display name can only contain letters, numbers, spaces, dot, underscore, apostrophe, and dash.");
      render();
      return;
    }
    if (editInput) editInput.value = normalizedName;
    state.editName = normalizedName;
    var reservedNames = new Set(["admin", "administrator", "root", "system", "support", "moderator", "mod", "owner", "staff", "user", "guest", "bot", "official", "service", "help", "superadmin", "sysadmin", "manager", "developer", "dev", "tester", "test", "demo", "backup", "internal", "team", "vip", "ceo", "founder", "partner", "affiliate", "api", "trading", "finance", "market", "exchange", "polymarket", "veyra", "veyratrade"]);
    var nameToCheck = normalizedName.toLowerCase();
    var isReserved = reservedNames.has(nameToCheck);
    if (!isReserved) {
      reservedNames.forEach(function (reserved) {
        if (nameToCheck.includes(reserved)) isReserved = true;
      });
    }
    if (isReserved) {
      state.saveMsg = T("user.error.reservedName", "This name contains reserved system keywords. Please choose a different name.");
      render();
      return;
    }
    state.saving = true;
    state.saveMsg = "";
    render();
    var r = await vFetch("/api/user/profile", {
      method: "PATCH",
      body: JSON.stringify({
        display_name: normalizedName
      })
    });
    var d = r ? await r.json().catch(function () {
      return {};
    }) : {};
    state.saving = false;
    state.saveMsg = d.ok ? T("user.profile.savedOk", "Saved") : d.error || T("user.profile.saveFailed", "Save failed");
    if (d.ok) {
      state.user.display_name = normalizedName;
      var headerUser = document.getElementById("header-user");
      if (headerUser) headerUser.textContent = normalizedName;
      var ddUserName = document.getElementById("dropdown-user-name");
      if (ddUserName) ddUserName.textContent = normalizedName;
    }
    render();
  }
  function copyText(val) {
    var raw = String(val || "").trim();
    if (!raw) return;
    navigator.clipboard.writeText(raw).then(function () {
      showToast(T("user.toast.addressCopied", "Address copied"), "success");
    }, function () {
      showToast(T("user.toast.copyFailed", "Copy failed"), "error");
    });
  }
  function bindEvents() {
    var saveBtn = $("up-save-btn");
    if (saveBtn) saveBtn.addEventListener("click", function () {
      saveName();
    });
    var copyAuthBtn = $("up-copy-auth-wallet-btn");
    if (copyAuthBtn) copyAuthBtn.addEventListener("click", function () {
      copyText(state.user.wallet_address);
    });
    var copyPmBtn = $("up-copy-pm-wallet-btn");
    if (copyPmBtn) copyPmBtn.addEventListener("click", function () {
      copyText(state.wallet.pm_address);
    });
    var mfaEnrollBtn = $("up-mfa-enroll-btn");
    if (mfaEnrollBtn) mfaEnrollBtn.addEventListener("click", function () {
      openMfaPopup("enroll");
    });
    var mfaManageBtn = $("up-mfa-manage-btn");
    if (mfaManageBtn) mfaManageBtn.addEventListener("click", function () {
      openMfaPopup("unenroll");
    });
    var editInput = $("up-edit-name");
    if (editInput) {
      editInput.addEventListener("input", function () {
        state.editName = editInput.value;
      });
    }
  }
  function bootUserProfileApp() {
    if (window.__VP_USER_APP_BOOTED__) return;
    window.__VP_USER_APP_BOOTED__ = true;
    render();
    bindEvents();
    init().catch(function (err) {
      try {
        console.error("[user_profile_app] init failed:", err);
      } catch (_) { }
    });
  }
  window.initUserProfileApp = bootUserProfileApp;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootUserProfileApp);
  } else {
    bootUserProfileApp();
  }
})();