(function () {
  "use strict";
  var PAGE_PACK = "deposit";
  var PAGE_I18N = {
    lang: "",
    current: {},
    en: {}
  };
  function _lang() {
    return window.veyraI18n && window.veyraI18n.lang || document.documentElement.getAttribute("data-lang") || document.documentElement.lang || "en";
  }
  function _pageText(lang, key) {
    var d = (lang === "en" ? PAGE_I18N.en : PAGE_I18N.current) || {};
    if (d[key] != null && d[key] !== "") return d[key];
    if (PAGE_I18N.en[key] != null && PAGE_I18N.en[key] !== "") return PAGE_I18N.en[key];
    return "";
  }
  function _t(key, fallback) {
    var local = _pageText(_lang(), key);
    if (local) return local;
    return window.veyraI18n && window.veyraI18n.t(key, fallback) || fallback;
  }
  function _esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function _loadPageI18n() {
    var lang = _lang();
    var v = encodeURIComponent(window.__ASSET_V || document.documentElement.getAttribute("data-asset-version") || Date.now());
    var curUrl = "/v2/static/i18n/" + PAGE_PACK + "/" + encodeURIComponent(lang) + ".json?v=" + v;
    var enUrl = "/v2/static/i18n/" + PAGE_PACK + "/en.json?v=" + v;
    return Promise.all([
      fetch(curUrl).then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; }),
      fetch(enUrl).then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; })
    ]).then(function (arr) {
      PAGE_I18N.lang = lang;
      PAGE_I18N.current = arr[0] || {};
      PAGE_I18N.en = arr[1] || {};
      var pageTitle = _pageText(lang, "page.deposit.title") || _pageText("en", "page.deposit.title") || "Deposit Guide";
      document.title = "V e Y r a - " + pageTitle;
    });
  }
  var S = {
    loading: true,
    error: "",
    info: null,
    bridge: null,
    bridgeError: "",
    copyMsg: "",
    onrampBusy: false,
    onrampStatus: "",
    onrampError: "",
    onrampVerifyKey: "",
    onrampBeforeBalance: null,
    onrampWatchTimer: null,
    onrampFocusBound: false,
    _copyTimer: null,
    _i18nBound: false
  };
  var root = null;
  function _getRoot() {
    if (!root) root = document.getElementById("vp-deposit-htmx-slot");
    return root;
  }
  function _bridgeQrSrc(address) {
    return "/api/deposit/bridge-qr.svg?address=" + encodeURIComponent(String(address || ""));
  }
  function render() {
    var r = _getRoot();
    if (!r) return;
    if (S.loading) {
      r.innerHTML = '<style id="vp-deposit-skeleton-style">' + "@keyframes vpDepPulse{0%{opacity:.55}50%{opacity:1}100%{opacity:.55}}" + ".vp-dep-skel{background:linear-gradient(90deg,rgba(148,163,184,.16),rgba(148,163,184,.26),rgba(148,163,184,.16));background-size:240% 100%;animation:vpDepPulse 1.05s ease-in-out infinite}" + "</style>" + '<div class="v-card" style="margin-top:12px">' + '<div class="vp-dep-skel" style="height:15px;width:190px;border-radius:6px"></div>' + '<div class="vp-dep-skel" style="height:30px;width:120px;border-radius:8px;margin-top:10px"></div>' + "</div>" + '<div class="v-card" style="margin-top:12px">' + '<div class="vp-dep-skel" style="height:14px;width:260px;border-radius:6px"></div>' + '<div class="vp-dep-skel" style="height:40px;width:100%;border-radius:8px;margin-top:10px"></div>' + '<div class="vp-dep-skel" style="height:16px;width:180px;border-radius:6px;margin-top:10px"></div>' + "</div>" + '<div class="v-card" style="margin-top:12px;color:var(--text-muted)">' + _esc(_t("deposit.loading", "Loading deposit information...")) + "</div>";
      return;
    }
    if (S.error) {
      r.innerHTML = '<div class="v-card" style="margin-top:12px;border:1px solid #dc2626;background:rgba(220,38,38,0.08);color:#fca5a5">' + _esc(_t("deposit.loadFailedPrefix", "Load failed: ")) + _esc(S.error) + "</div>";
      return;
    }
    if (!S.info) return;
    var d = S.info;
    var pusdBal = d.pusd_balance != null ? Number(d.pusd_balance) : 0;
    var pendingWrapNum = d.usdc_e_balance != null ? Number(d.usdc_e_balance) : 0;
    var bal = d.tradable_balance != null ? Number(d.tradable_balance) : (pusdBal + pendingWrapNum);
    var nativeLegacy = d.usdc_native_balance != null ? d.usdc_native_balance : 0;
    var minTrade = (d.thresholds && d.thresholds.min_usdc_to_trade) != null ? d.thresholds.min_usdc_to_trade : 1;
    var minSafe = (d.thresholds && d.thresholds.min_safe_balance_alert) != null ? d.thresholds.min_safe_balance_alert : 5;
    var balColor = bal < minTrade ? "#ef4444" : bal < minSafe ? "#f59e0b" : "#10b981";
    var balText = "$" + bal.toFixed(2);
    var explorerHref = "https://polygonscan.com/address/" + _esc(d.funder_address || "");
    var isDepositWallet = d.wallet_type === "deposit_wallet";
    var walletTypeBadge = isDepositWallet ? '<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:0.7rem;font-weight:600;background:rgba(16,185,129,0.15);color:#34d399;border:1px solid rgba(16,185,129,0.35);margin-left:8px">' + _esc(_t("deposit.walletType.depositWallet", "Deposit Wallet")) + "</span>" : '<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:0.7rem;font-weight:600;background:rgba(148,163,184,0.1);color:#94a3b8;border:1px solid rgba(148,163,184,0.25);margin-left:8px">' + _esc(_t("deposit.walletType.safe", "Safe Wallet")) + "</span>";
    var funderLabel = isDepositWallet ? _t("deposit.funderLabel.depositWallet", "Your deposit wallet address") : _t("deposit.funderLabel", "Your funding wallet address (funder address)");
    var warningHtml = isDepositWallet ? '<div style="margin-top:10px;padding:10px 12px;background:rgba(239,68,68,0.06);border:1px solid #dc2626;border-radius:6px;font-size:0.82rem;color:#fca5a5">' + _esc(_t("deposit.warning.depositWallet.prefix", "Only pUSD on")) + " " + "<b>" + _esc(_t("deposit.warning.network", "Polygon mainnet")) + "</b> " + _esc(_t("deposit.warning.depositWallet.middle", "(Chain ID 137) is accepted by deposit wallets. Sending USDC.e is not recommended — use pUSD to ensure funds are credited.")) + " " + "<b>" + _esc(_t("deposit.warning.suffix", "permanent loss.")) + "</b>" + "</div>" : '<div style="margin-top:10px;padding:10px 12px;background:rgba(239,68,68,0.06);border:1px solid #dc2626;border-radius:6px;font-size:0.82rem;color:#fca5a5">' + _esc(_t("deposit.warning.prefix", "Only USDC.e or pUSD on")) + " " + "<b>" + _esc(_t("deposit.warning.network", "Polygon mainnet")) + "</b> " + _esc(_t("deposit.warning.middle", "(Chain ID 137) is supported. Sending from other chains (Ethereum / Arbitrum / Base / Solana) will result in")) + " " + "<b>" + _esc(_t("deposit.warning.suffix", "permanent loss.")) + "</b>" + "</div>";
    var convertCardHtml = "";
    var bridgeCardHtml = "";
    if (isDepositWallet) {
      bridgeCardHtml = _renderBridgeCard();
    }
    var quickBuyLabel = _t("Deposit.QuickBuy", "Quick Buy (Fiat)");
    var quickBuyDesc = _t("Deposit.Instructions", "Securely top up with Coinbase. Supports bank cards and Apple Pay.");
    var onrampStateHtml = "";
    if (S.onrampError) {
      onrampStateHtml = '<div style="margin-top:10px;padding:10px 12px;background:rgba(239,68,68,0.08);border:1px solid #dc2626;border-radius:6px;color:#fca5a5;font-size:0.82rem">' + _esc(S.onrampError) + "</div>";
    } else if (S.onrampStatus) {
      onrampStateHtml = '<div style="margin-top:10px;padding:10px 12px;background:rgba(59,130,246,0.08);border:1px solid #3b82f6;border-radius:6px;color:#bfdbfe;font-size:0.82rem">' + _esc(S.onrampStatus) + "</div>";
    }
    var toastHtml = "";
    var pendingWrapHtml = "";
    if (isDepositWallet && pendingWrapNum > 0) {
      pendingWrapHtml = '<div style="font-size:0.74rem;color:#fbbf24;margin-top:6px">' +
        _esc(_t("withdraw.pendingWrap", "Pending wrap: $")) + _esc(pendingWrapNum.toFixed(2)) +
        _esc(_t("withdraw.pendingWrapSuffix", " USDC.e → pUSD (auto via CollateralOnramp on next Safe interaction).")) +
        '</div>' +
        '<div style="font-size:0.74rem;color:var(--text-muted);margin-top:4px">' +
        _esc(_t("deposit.currentBalance", "Current Balance")) + ': $' + _esc(pusdBal.toFixed(2)) + ' pUSD' +
        '</div>';
    }
    var balanceCardHtml = '<div class="v-card" style="margin-top:12px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">' + "<div>" + '<div style="font-size:0.78rem;color:var(--text-muted)">' + _esc(_t("deposit.tradableBalance", "Tradable Balance (pUSD)")) + "</div>" + '<div style="font-size:1.6rem;font-weight:700;color:' + balColor + '">' + _esc(balText) + "</div>" + pendingWrapHtml + "</div>" + '<div style="text-align:right">' + '<div style="font-size:0.72rem;color:var(--text-muted)">' + _esc(_t("deposit.minThresholds", "Min order / Safety line")) + "</div>" + '<div style="font-size:0.88rem">$' + _esc(minTrade.toFixed(2)) + " / $" + _esc(minSafe.toFixed(2)) + "</div>" + '<button id="vp-deposit-refresh" class="v-btn v-btn-outline v-btn-sm" style="margin-top:6px">' + _esc(_t("deposit.refreshBalance", "Refresh balance")) + "</button>" + "</div>" + "</div>";
    var onrampCardHtml = '<div class="v-card" style="margin-top:12px;border:1px solid rgba(16,185,129,0.32);background:linear-gradient(135deg,rgba(16,185,129,0.12),rgba(2,132,199,0.08));">' + '<div style="display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap">' + "<div>" + '<div style="font-size:0.74rem;letter-spacing:.08em;color:var(--text-muted);text-transform:uppercase">' + _esc(_t("deposit.onramp.sectionLabel", "Fiat on-ramp")) + "</div>" + '<div style="font-size:1.1rem;font-weight:700;margin-top:4px">' + _esc(quickBuyLabel) + "</div>" + '<div style="font-size:0.86rem;color:var(--text-secondary);margin-top:6px">' + _esc(quickBuyDesc) + "</div>" + '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">' + '<span style="padding:4px 8px;border:1px solid rgba(148,163,184,.35);border-radius:999px;font-size:.72rem;background:rgba(15,23,42,.28)">' + _esc(_t("deposit.onramp.badge.coinbase", "Coinbase")) + "</span>" + '<span style="padding:4px 8px;border:1px solid rgba(148,163,184,.35);border-radius:999px;font-size:.72rem;background:rgba(15,23,42,.28)">' + _esc(_t("deposit.onramp.badge.visa", "Visa")) + "</span>" + '<span style="padding:4px 8px;border:1px solid rgba(148,163,184,.35);border-radius:999px;font-size:.72rem;background:rgba(15,23,42,.28)">' + _esc(_t("deposit.onramp.badge.mastercard", "Mastercard")) + "</span>" + "</div>" + "</div>" + '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">' + '<button id="vp-onramp-start" class="v-btn v-btn-primary v-btn-sm"' + (S.onrampBusy ? " disabled" : "") + ">" + _esc(S.onrampBusy ? _t("deposit.onramp.opening", "Opening Coinbase...") : _t("deposit.onramp.start", "Buy crypto")) + "</button>" + '<button id="vp-onramp-verify" class="v-btn v-btn-outline v-btn-sm"' + (S.onrampBusy ? " disabled" : "") + ">" + _esc(_t("deposit.onramp.verify", "I have completed payment")) + "</button>" + "</div>" + "</div>" + onrampStateHtml + "</div>";
    var notesCardHtml = '<div class="v-card" style="margin-top:12px">' + '<div style="font-weight:600;margin-bottom:8px">' + _esc(_t("deposit.notes.title", "Notes")) + "</div>" + '<ul style="font-size:0.82rem;color:var(--text-secondary);line-height:1.7;padding-left:22px">' + "<li>" + _esc(_t("deposit.notes.useBridgeOnly", "Always deposit via the bridge address shown above. Sending tokens directly to other addresses or wrong chains will result in lost funds.")) + "</li>" + "<li>" + _esc(_t("deposit.notes.explorerPrefix", "Once credited, on-chain records can be viewed at")) + " " + '<a href="' + explorerHref + '" target="_blank" rel="noopener" style="color:var(--accent)">polygonscan.com</a>' + _esc(_t("deposit.notes.explorerSuffix", ".")) + "</li>" + "<li>" + _esc(_t("deposit.notes.systemAuto", "Our system reads your tradable balance automatically when executing orders; no manual signing is required for trading.")) + "</li>" + "</ul>" + "</div>";
    r.innerHTML = balanceCardHtml + onrampCardHtml + convertCardHtml + bridgeCardHtml + notesCardHtml + toastHtml;
    _bindEvents();
  }
  function _renderBridgeCard() {
    var b = S.bridge || {};
    var addrs = b.addresses || {};
    var evm = String(addrs.evm || "").trim();
    var svm = String(addrs.svm || "").trim();
    var btc = String(addrs.btc || "").trim();
    var tron = String(addrs.tron || "").trim();
    var labelTitle = _t("deposit.bridge.title", "Deposit USDC / USDT (Polygon)");
    var labelDesc = _t("deposit.bridge.desc", "Send supported assets to the Polygon address below — we'll automatically convert and credit pUSD to your wallet (usually under 1 minute).");
    if (!evm && S.bridgeError) {
      return '<div class="v-card" style="margin-top:12px;border:1px solid #dc2626;background:rgba(220,38,38,0.06);color:#fca5a5;font-size:0.85rem">' +
        "<b>" + _esc(labelTitle) + "</b><br>" +
        _esc(_t("deposit.bridge.error", "Could not load bridge addresses: ")) + _esc(S.bridgeError) +
        "</div>";
    }
    if (!evm) return "";
    var polygonHtml =
      '<div style="font-size:0.74rem;letter-spacing:.08em;color:var(--text-muted);text-transform:uppercase">' + _esc(_t("deposit.bridge.polygonLabel", "Polygon (USDC / USDC.e / USDT / DAI)")) + "</div>" +
      '<div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap;margin-top:6px">' +
      '<div style="flex:1;min-width:220px">' +
      '<code style="display:block;font-family:monospace;font-size:0.92rem;word-break:break-all;background:var(--bg-primary);padding:8px 12px;border-radius:6px;color:var(--text-primary);border:1px solid var(--border)">' + _esc(evm) + "</code>" +
      '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px">' +
      '<button class="v-btn v-btn-primary v-btn-sm vp-bridge-copy" data-copy="' + _esc(evm) + '">' + _esc(_t("deposit.copyAddress", "Copy address")) + "</button>" +
      "</div>" +
      "</div>" +
      '<div style="width:118px;height:118px;border:1px solid var(--border);border-radius:8px;background:#fff;padding:6px;display:flex;align-items:center;justify-content:center">' +
      '<img alt="Bridge QR" src="' + _bridgeQrSrc(evm) + '" style="width:100%;height:100%;object-fit:contain" />' +
      "</div>" +
      '</div>';
    return '<div class="v-card" style="margin-top:12px;border:1px solid rgba(56,189,248,0.45);background:linear-gradient(135deg,rgba(56,189,248,0.06),rgba(2,132,199,0.04));">' +
      '<div style="font-size:0.74rem;letter-spacing:.08em;color:#38bdf8;text-transform:uppercase">' + _esc(_t("deposit.bridge.label", "Recommended")) + "</div>" +
      '<div style="font-size:1.05rem;font-weight:700;margin-top:4px">' + _esc(labelTitle) + "</div>" +
      '<div style="font-size:0.85rem;color:var(--text-secondary);margin-top:4px;margin-bottom:10px">' + _esc(labelDesc) + "</div>" +
      polygonHtml +
      "</div>";
  }
  function _bindEvents() {
    var refreshBtn = document.getElementById("vp-deposit-refresh");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", function () {
        fetchInfo();
      });
    }
    var copyBtn = document.getElementById("vp-deposit-copy-addr");
    if (copyBtn) {
      copyBtn.addEventListener("click", function () {
        var text = copyBtn.getAttribute("data-copy") || "";
        _copyText(text);
      });
    }
    var onrampBtn = document.getElementById("vp-onramp-start");
    if (onrampBtn) {
      onrampBtn.addEventListener("click", function () {
        startOnramp();
      });
    }
    var verifyBtn = document.getElementById("vp-onramp-verify");
    if (verifyBtn) {
      verifyBtn.addEventListener("click", function () {
        verifyOnramp("manual_verify_click");
      });
    }
    var bridgeCopyBtns = document.querySelectorAll(".vp-bridge-copy");
    if (bridgeCopyBtns && bridgeCopyBtns.length) {
      for (var i = 0; i < bridgeCopyBtns.length; i++) {
        (function (btn) {
          btn.addEventListener("click", function () {
            _copyText(btn.getAttribute("data-copy") || "");
          });
        })(bridgeCopyBtns[i]);
      }
    }
  }
  function _safeBalanceSnapshot() {
    if (!S.info) return 0;
    if (typeof S.info.usdc_total_balance === "number") return S.info.usdc_total_balance;
    if (typeof S.info.tradable_balance === "number") return S.info.tradable_balance;
    if (typeof S.info.usdc_balance === "number") return S.info.usdc_balance;
    return 0;
  }
  function _findPrivyOnrampInvoker() {
    var sr = window.__veyraPrivySigningRuntime;
    if (!sr) return null;
    if (typeof window.veyraPrivyFundWallet === "function" &&
      sr.status && sr.status.ready) {
      return {
        fn: window.veyraPrivyFundWallet,
        ctx: null,
        gated: true
      };
    }
    if (typeof sr.fundWallet === "function" && sr.status &&
      sr.status.ready) {
      return {
        fn: sr.fundWallet,
        ctx: null,
        gated: false
      };
    }
    return null;
  }
  function _normalizeEvmAddress(value) {
    if (!value) return "";
    if (typeof value === "string") return value.trim();
    if (typeof value === "object") {
      if (typeof value.address === "string") return value.address.trim();
      if (typeof value.walletAddress === "string") return value.walletAddress.trim();
      if (typeof value.value === "string") return value.value.trim();
    }
    return "";
  }
  function ensurePrivyOnrampRuntime() {
    if (_findPrivyOnrampInvoker()) return Promise.resolve();
    if (S._onrampRuntimeInitPromise) return S._onrampRuntimeInitPromise;
    var sr = window.__veyraPrivySigningRuntime;
    var bootPromise = (sr && typeof sr.ensureBoot === "function") ? sr.ensureBoot() : Promise.resolve();
    var sdkPromise = bootPromise.then(function () {
      var sr2 = window.__veyraPrivySigningRuntime;
      if (sr2 && typeof sr2.whenSdkReady === "function") {
        return sr2.whenSdkReady(8000);
      }
      return true;
    });
    S._onrampRuntimeInitPromise = sdkPromise.then(function () {
      return new Promise(function (resolve, reject) {
        var tries = 0;
        var timer = setInterval(function () {
          if (_findPrivyOnrampInvoker()) {
            clearInterval(timer);
            resolve();
            return;
          }
          if (++tries >= 40) {
            clearInterval(timer);
            reject(new Error(_t("deposit.onramp.unavailable", "Privy on-ramp is currently unavailable in this session.")));
          }
        }, 200);
      });
    });
    return S._onrampRuntimeInitPromise.finally(function () {
      S._onrampRuntimeInitPromise = null;
    });
  }
  function _ensurePrivyOnrampReactRuntime() {
    if (S._onrampRuntimeInitPromise) return S._onrampRuntimeInitPromise;
    S._onrampRuntimeInitPromise = async function () {
      var host = document.getElementById("privy-onramp-runtime-root");
      if (!host) {
        host = document.createElement("div");
        host.id = "privy-onramp-runtime-root";
        host.style.display = "none";
        document.body.appendChild(host);
      }
      var cfg = await fetch("/api/user/privy/config", {
        credentials: "include",
        cache: "no-store"
      }).then(function (r) {
        return r.json();
      });
      if (!cfg || !cfg.app_id) {
        throw new Error(_t("deposit.onramp.unavailable", "Privy on-ramp is currently unavailable in this session."));
      }
      if (!window.__veyraPrivyMfaModulesPromise) {
        var rootEl = document.documentElement;
        var assetVersion = rootEl && rootEl.getAttribute("data-asset-version") || "";
        window.__veyraPrivyMfaModulesPromise = import("/v2/static/js/vendor/mfa_runtime_bundle.mjs?v=" + assetVersion);
      }
      var modules = await window.__veyraPrivyMfaModulesPromise;
      var React = modules.React;
      var ReactDOMClient = modules.ReactDOMClient;
      var PrivyReact = modules.PrivyReact;
      if (!React || !ReactDOMClient || !PrivyReact) {
        throw new Error(_t("deposit.onramp.unavailable", "Privy on-ramp is currently unavailable in this session."));
      }
      var createElement = React.createElement;
      var useEffect = React.useEffect;
      var createRoot = ReactDOMClient.createRoot;
      var PrivyProvider = PrivyReact.PrivyProvider;
      var usePrivy = PrivyReact.usePrivy;
      var useFundWallet = PrivyReact.useFundWallet;
      var runtime = window.__veyraPrivyOnrampRuntime || {};
      window.__veyraPrivyOnrampRuntime = runtime;
      function OnrampRuntimeInner() {
        var privy = usePrivy();
        var ready = privy.ready;
        var authenticated = privy.authenticated;
        var user = privy.user;
        var fundApi = useFundWallet();
        var fundWallet = fundApi ? fundApi.fundWallet : null;
        useEffect(function () {
          runtime.start = async function (payload) {
            for (var i = 0; i < 80; i++) {
              if (runtime.status && runtime.status.ready && runtime.status.authenticated) break;
              await new Promise(function (resolve) {
                setTimeout(resolve, 250);
              });
            }
            if (!(runtime.status && runtime.status.ready && runtime.status.authenticated)) {
              throw new Error(_t("deposit.onramp.unavailable", "Privy on-ramp is currently unavailable in this session."));
            }
            if (typeof fundWallet !== "function") {
              throw new Error(_t("deposit.onramp.unavailable", "Privy on-ramp is currently unavailable in this session."));
            }
            var destination = payload && payload.walletAddress || (user && user.wallet && user.wallet.address ? user.wallet.address : "") || "";
            destination = _normalizeEvmAddress(destination);
            if (!/^0x[a-fA-F0-9]{40}$/.test(destination)) {
              throw new Error(_t("deposit.onramp.unavailable", "Privy on-ramp is currently unavailable in this session."));
            }
            return await fundWallet(destination, { asset: "USDC" });
          };
          return function () {
            try {
              delete runtime.start;
            } catch (_) {
              runtime.start = undefined;
            }
          };
        }, [ready, authenticated, user, fundWallet]);
        useEffect(function () {
          runtime.status = {
            ready: !!ready,
            authenticated: !!authenticated
          };
        }, [ready, authenticated]);
        return createElement(React.Fragment, null);
      }
      function OnrampRuntimeRoot() {
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
        }, createElement(OnrampRuntimeInner));
      }
      if (!runtime.root) {
        runtime.root = createRoot(host);
        runtime.root.render(createElement(OnrampRuntimeRoot));
      }
      for (var j = 0; j < 80; j++) {
        if (typeof runtime.start === "function") return;
        await new Promise(function (resolve) {
          setTimeout(resolve, 50);
        });
      }
      throw new Error(_t("deposit.onramp.unavailable", "Privy on-ramp is currently unavailable in this session."));
    }();
    return S._onrampRuntimeInitPromise.finally(function () {
      S._onrampRuntimeInitPromise = null;
    });
  }
  function _bindOnrampReturnHandlers() {
    if (S.onrampFocusBound) return;
    S.onrampFocusBound = true;
    window.addEventListener("focus", function () {
      if (S.onrampVerifyKey) verifyOnramp("focus_return");
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && S.onrampVerifyKey) {
        verifyOnramp("visibility_return");
      }
    });
  }
  function _checkOnboardingGate() {
    return fetch("/api/user/onboard-modal", {
      credentials: "include",
      cache: "no-store"
    }).then(function (res) {
      if (res.status === 200) return true;
      if (res.status === 204) return false;
      return false;
    }).catch(function () {
      return false;
    });
  }
  function startOnramp() {
    if (S.onrampBusy) return;
    S.onrampBusy = true;
    S.onrampError = "";
    S.onrampStatus = _t("deposit.onramp.checking", "Checking account status...");
    render();
    _checkOnboardingGate().then(function (needsOnboarding) {
      if (needsOnboarding) {
        throw new Error(_t("deposit.onramp.onboardingRequired", "Please complete onboarding before using quick buy."));
      }
      var invoker = _findPrivyOnrampInvoker();
      if (!invoker) {
        return ensurePrivyOnrampRuntime().then(function () {
          var rtInvoker = _findPrivyOnrampInvoker();
          if (!rtInvoker) {
            throw new Error(_t("deposit.onramp.unavailable", "Privy on-ramp is currently unavailable in this session."));
          }
          return rtInvoker;
        });
      }
      return invoker;
    }).then(function (invoker) {
      S.onrampBeforeBalance = _safeBalanceSnapshot();
      S.onrampVerifyKey = "or-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
      S.onrampStatus = _t("deposit.onramp.opening", "Opening Coinbase payment window...");
      render();
      return fetch("/api/user/me", {
        credentials: "include",
        cache: "no-store"
      }).then(function (res) {
        return res.json();
      }).catch(function () {
        return {};
      }).then(function (me) {
        var destination = _normalizeEvmAddress(me && (me.main_wallet || me.wallet_address) || S.info && S.info.funder_address || "");
        if (!/^0x[a-fA-F0-9]{40}$/.test(destination)) {
          throw new Error(_t("deposit.onramp.unavailable", "Privy on-ramp is currently unavailable in this session."));
        }
        return Promise.resolve(invoker.fn.call(invoker.ctx, destination, {
          asset: "USDC"
        }));
      });
    }).then(function (maybePopup) {
      _bindOnrampReturnHandlers();
      S.onrampStatus = _t("deposit.onramp.pending", "Payment window opened. Return here after completing the purchase.");
      S.onrampBusy = false;
      render();
      if (S.onrampWatchTimer) {
        clearInterval(S.onrampWatchTimer);
        S.onrampWatchTimer = null;
      }
      if (maybePopup && typeof maybePopup === "object" && "closed" in maybePopup) {
        S.onrampWatchTimer = setInterval(function () {
          try {
            if (maybePopup.closed) {
              clearInterval(S.onrampWatchTimer);
              S.onrampWatchTimer = null;
              verifyOnramp("popup_closed");
            }
          } catch (_e) {
            clearInterval(S.onrampWatchTimer);
            S.onrampWatchTimer = null;
          }
        }, 1200);
      }
      setTimeout(function () {
        if (S.onrampVerifyKey) verifyOnramp("delayed_sync");
      }, 5e3);
    }).catch(function (err) {
      S.onrampBusy = false;
      S.onrampError = String(err && err.message || err || _t("deposit.onramp.failed", "Failed to open on-ramp"));
      S.onrampStatus = "";
      render();
    });
  }
  function verifyOnramp(trigger) {
    if (!S.onrampVerifyKey || S.onrampBusy) return;
    S.onrampBusy = true;
    S.onrampError = "";
    S.onrampStatus = _t("deposit.onramp.verifying", "Verifying deposit on server...");
    render();
    fetch("/api/pm/onramp/verify", {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        verify_key: S.onrampVerifyKey,
        before_balance: S.onrampBeforeBalance,
        trigger: trigger || "manual"
      })
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
        return data;
      });
    }).then(function (data) {
      var credited = !!(data && data.credited);
      if (credited) {
        var rw = data.reward || {};
        var totalPts = Number(rw.user || 0) + Number(rw.l1 || 0) + Number(rw.l2 || 0) + Number(rw.l3 || 0);
        S.onrampStatus = _t("deposit.onramp.verified", "Deposit verified successfully.") + " +" + (data.delta_balance || 0).toFixed(2) + _t("deposit.onramp.creditUnit", " pUSD") + (totalPts > 0 ? " · +" + totalPts + " pts" : "");
        S.onrampVerifyKey = "";
      } else {
        var reason = data.reason || "pending";
        if (reason === "delta_below_min") {
          S.onrampStatus = _t("deposit.onramp.waiting", "Payment submitted. Waiting for final settlement, please retry shortly.");
        } else if (reason === "same_after_balance") {
          S.onrampStatus = _t("deposit.onramp.alreadyChecked", "Current balance already synced.");
          S.onrampVerifyKey = "";
        } else {
          S.onrampStatus = _t("deposit.onramp.pending", "Payment window opened. Return here after completing the purchase.");
        }
      }
      S.onrampBusy = false;
      render();
      fetchInfo();
    }).catch(function (err) {
      S.onrampBusy = false;
      S.onrampError = String(err && err.message || err || _t("deposit.onramp.verifyFailed", "Verification failed"));
      render();
    });
  }
  function _copyText(text) {
    var msgOk = _t("deposit.copied", "Copied to clipboard");
    var msgFb = _t("deposit.copy.fallback", "Copy failed, please select manually");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        _showCopyMsg(msgOk);
      }, function () {
        _showCopyMsg(msgFb);
      });
    } else {
      _showCopyMsg(msgFb);
    }
  }
  function _showCopyMsg(msg) {
    if (S._copyTimer) clearTimeout(S._copyTimer);
    S.copyMsg = msg;
    var el = document.getElementById("vp-deposit-floating-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "vp-deposit-floating-toast";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      el.style.cssText = [
        "position:fixed",
        "top:24px",
        "left:50%",
        "transform:translateX(-50%) translateY(-12px)",
        "z-index:99999",
        "padding:12px 20px",
        "background:rgba(16,185,129,0.96)",
        "color:#0b1220",
        "font-weight:700",
        "font-size:0.92rem",
        "border-radius:10px",
        "box-shadow:0 12px 32px rgba(0,0,0,0.45),0 0 0 1px rgba(16,185,129,0.6)",
        "opacity:0",
        "transition:opacity .18s ease, transform .18s ease",
        "pointer-events:none",
        "max-width:90vw",
        "text-align:center",
        "white-space:pre-wrap"
      ].join(";");
      document.body.appendChild(el);
    }
    el.textContent = String(msg || "");
    requestAnimationFrame(function () {
      el.style.opacity = "1";
      el.style.transform = "translateX(-50%) translateY(0)";
    });
    S._copyTimer = setTimeout(function () {
      S.copyMsg = "";
      if (el && el.parentNode) {
        el.style.opacity = "0";
        el.style.transform = "translateX(-50%) translateY(-12px)";
        setTimeout(function () {
          if (el && el.parentNode && !S.copyMsg) {
            el.parentNode.removeChild(el);
          }
        }, 220);
      }
    }, 2200);
  }
  function fetchInfo() {
    S.loading = true;
    S.error = "";
    render();
    var infoP = fetch("/api/pm/deposit-info", {
      credentials: "include"
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
        return data;
      });
    });
    var bridgeP = fetch("/api/deposit/bridge-addresses?include_status=0", {
      credentials: "include"
    }).then(function (res) {
      return res.json().then(function (data) {
        return { ok: res.ok, data: data };
      });
    }).catch(function (e) {
      return { ok: false, data: { error: String(e && e.message || e) } };
    });
    infoP.then(function (data) {
      if (data && data.status === "unbound") {
        S.info = null;
        S.error = data.error || "wallet not bound";
      } else {
        S.info = data;
      }
      return bridgeP;
    }).then(function (b) {
      if (b && b.ok) {
        S.bridge = b.data;
        S.bridgeError = "";
      } else {
        S.bridge = null;
        S.bridgeError = b && b.data && (b.data.message || b.data.error) || "";
      }
      S.loading = false;
      render();
    }).catch(function (e) {
      S.error = String(e.message || e);
      S.loading = false;
      render();
    });
  }
  function init() {
    if (!document.getElementById("vp-deposit-htmx-slot")) return;
    if (!S._i18nBound) {
      S._i18nBound = true;
      document.addEventListener("i18n:changed", function () {
        _loadPageI18n().finally(function () {
          render();
        });
      });
    }
    _loadPageI18n().finally(function () {
      fetchInfo();
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
