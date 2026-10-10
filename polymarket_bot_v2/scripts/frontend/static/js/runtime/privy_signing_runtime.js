(function () {
  "use strict";
  if (window.__veyraPrivySigningRuntime) return;
  var runtime = window.__veyraPrivySigningRuntime = {};
  function _t(key, fb) {
    try {
      if (window.veyraI18n && window.veyraI18n.t) {
        return window.veyraI18n.t(key, fb);
      }
    } catch (_) { }
    return fb;
  }
  function _ensureBufferCompat() {
    if (window.Buffer && typeof window.Buffer.from === "function") return;
    function _asBytes(input, encoding) {
      if (input == null) return new Uint8Array(0);
      if (input instanceof Uint8Array) return new Uint8Array(input);
      if (Array.isArray(input)) return new Uint8Array(input);
      if (typeof input === "string") {
        var enc = String(encoding || "utf8").toLowerCase();
        if (enc === "base64") {
          var bin = atob(input);
          var out = new Uint8Array(bin.length);
          for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
          return out;
        }
        if (enc === "hex") {
          var clean = input.replace(/^0x/, "");
          if (clean.length % 2) clean = "0" + clean;
          var outHex = new Uint8Array(clean.length / 2);
          for (var j = 0; j < clean.length; j += 2) {
            outHex[j / 2] = parseInt(clean.slice(j, j + 2), 16) || 0;
          }
          return outHex;
        }
        return new TextEncoder().encode(input);
      }
      try {
        return new Uint8Array(input);
      } catch (_) {
        return new Uint8Array(0);
      }
    }
    function _attach(bytes) {
      bytes.toString = function (encoding) {
        var enc = String(encoding || "utf8").toLowerCase();
        if (enc === "hex") {
          var parts = [];
          for (var i = 0; i < this.length; i++) {
            parts.push(this[i].toString(16).padStart(2, "0"));
          }
          return parts.join("");
        }
        if (enc === "base64") {
          var bin = "";
          for (var j = 0; j < this.length; j++) bin += String.fromCharCode(this[j]);
          return btoa(bin);
        }
        return new TextDecoder().decode(this);
      };
      var rawSlice = bytes.slice;
      bytes.slice = function (start, end) {
        return _attach(rawSlice.call(this, start, end));
      };
      return bytes;
    }
    window.Buffer = {
      from: function (input, encoding) {
        return _attach(_asBytes(input, encoding));
      },
      alloc: function (size) {
        return _attach(new Uint8Array(Number(size) || 0));
      },
      concat: function (list) {
        var arr = Array.isArray(list) ? list : [];
        var total = 0;
        for (var i = 0; i < arr.length; i++) total += (arr[i] && arr[i].length) || 0;
        var out = new Uint8Array(total);
        var off = 0;
        for (var j = 0; j < arr.length; j++) {
          var part = _asBytes(arr[j]);
          out.set(part, off);
          off += part.length;
        }
        return _attach(out);
      },
      isBuffer: function (v) {
        return v instanceof Uint8Array;
      }
    };
  }
  function _ensureBoot() {
    if (runtime._bootPromise) return runtime._bootPromise;
    runtime._bootPromise = async function () {
      var host = document.getElementById("veyra-privy-signing-root");
      if (!host) {
        host = document.createElement("div");
        host.id = "veyra-privy-signing-root";
        host.style.display = "none";
        document.body.appendChild(host);
      }
      var cfgRes = await fetch("/api/user/privy/config", {
        credentials: "include",
        cache: "no-store"
      });
      var cfg = cfgRes.ok ? await cfgRes.json() : null;
      if (!cfg || !cfg.app_id) {
        throw new Error(_t("pm.trade.privyUnavailable", "Privy embedded wallet is not available in this session. Please re-login and retry."));
      }
      _ensureBufferCompat();
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
        throw new Error(_t("pm.trade.privyUnavailable", "Privy embedded wallet is not available in this session. Please re-login and retry."));
      }
      var createElement = React.createElement;
      var useEffect = React.useEffect;
      var createRoot = ReactDOMClient.createRoot;
      var PrivyProvider = PrivyReact.PrivyProvider;
      var usePrivy = PrivyReact.usePrivy;
      var useWallets = PrivyReact.useWallets;
      var useFundWallet = PrivyReact.useFundWallet;
      function SigningInner() {
        var privy = usePrivy();
        var ready = !!(privy && privy.ready);
        var authenticated = !!(privy && privy.authenticated);
        var walletsApi = useWallets();
        var wallets = walletsApi && walletsApi.wallets || [];
        var fundApi = useFundWallet ? useFundWallet() : null;
        var _fundWallet = fundApi ? fundApi.fundWallet : null;
        useEffect(function () {
          runtime.fundWallet = _fundWallet;
          runtime.status = {
            ready: ready,
            authenticated: authenticated,
            walletCount: wallets.length
          };
          runtime._wallets = wallets;
          runtime.signTypedData = async function (typedData, opts) {
            opts = opts || {};
            var expectedSigner = String(opts.expectedSigner || "").toLowerCase();
            var requireExact = opts.requireExact !== false;
            var embedded = null;
            for (var i = 0; i < 80; i++) {
              var ws = Array.isArray(runtime._wallets) ? runtime._wallets : [];
              if (expectedSigner) {
                embedded = ws.find(function (w) {
                  return w && String(w.address || "").toLowerCase() === expectedSigner;
                }) || null;
              }
              if (!embedded && !expectedSigner) {
                embedded = ws.find(function (w) {
                  return w && w.walletClientType === "privy";
                }) || ws[0] || null;
              }
              if (runtime.status && runtime.status.ready && runtime.status.authenticated && embedded) break;
              await new Promise(function (r) {
                setTimeout(r, 250);
              });
            }
            async function _ensurePolygonChain(eth) {
              try {
                var cid = await eth.request({ method: "eth_chainId" });
                if (String(cid).toLowerCase() === "0x89") return;
              } catch (_) { }
              try {
                await eth.request({ method: "wallet_switchEthereumChain", params: [{ chainId: "0x89" }] });
                return;
              } catch (swErr) {
                if (swErr && (swErr.code === 4902 || (swErr.data && swErr.data.originalError && swErr.data.originalError.code === 4902))) {
                  await eth.request({
                    method: "wallet_addEthereumChain",
                    params: [{
                      chainId: "0x89",
                      chainName: "Polygon",
                      nativeCurrency: { name: "POL", symbol: "POL", decimals: 18 },
                      rpcUrls: ["https://polygon-rpc.com"],
                      blockExplorerUrls: ["https://polygonscan.com"]
                    }]
                  });
                  return;
                }
                throw swErr;
              }
            }
            if (!embedded) {
              var wsNow = Array.isArray(runtime._wallets) ? runtime._wallets : [];
              var addrs = wsNow.map(function (w) { return String((w && w.address) || ""); }).join(",");
              if (expectedSigner && typeof window !== "undefined" && window.ethereum && typeof window.ethereum.request === "function") {
                try {
                  var accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
                  var hit = (accounts || []).find(function (a) { return String(a || "").toLowerCase() === expectedSigner; });
                  if (hit) {
                    await _ensurePolygonChain(window.ethereum);
                    var payload2 = typeof typedData === "string" ? typedData : JSON.stringify(typedData);
                    var sig2 = await window.ethereum.request({
                      method: "eth_signTypedData_v4",
                      params: [hit, payload2]
                    });
                    if (sig2 && typeof sig2 === "string" && sig2.startsWith("0x")) return sig2;
                  } else {
                    throw new Error(
                      "Required signer wallet " + expectedSigner +
                      " is not the active account in MetaMask. Active: [" + (accounts || []).join(",") +
                      "]. Please switch MetaMask account to " + expectedSigner + " and retry."
                    );
                  }
                } catch (e) {
                  if (requireExact) throw e;
                }
              }
              if (expectedSigner && requireExact) {
                throw new Error(
                  "Required signer wallet " + expectedSigner +
                  " is not connected in this session. Available via Privy: [" + addrs + "]. " +
                  "Please connect that wallet (MetaMask) and retry."
                );
              }
              throw new Error(_t("pm.trade.embeddedWalletMissing", "Embedded wallet not ready. Please re-login and retry."));
            }
            if (expectedSigner) {
              var got = String(embedded.address || "").toLowerCase();
              if (got !== expectedSigner && requireExact) {
                if (typeof window !== "undefined" && window.ethereum && typeof window.ethereum.request === "function") {
                  try {
                    var accounts3 = await window.ethereum.request({ method: "eth_requestAccounts" });
                    var hit3 = (accounts3 || []).find(function (a) { return String(a || "").toLowerCase() === expectedSigner; });
                    if (hit3) {
                      await _ensurePolygonChain(window.ethereum);
                      var payload3 = typeof typedData === "string" ? typedData : JSON.stringify(typedData);
                      var sig3 = await window.ethereum.request({
                        method: "eth_signTypedData_v4",
                        params: [hit3, payload3]
                      });
                      if (sig3 && typeof sig3 === "string" && sig3.startsWith("0x")) return sig3;
                    }
                  } catch (e3) {
                    throw e3;
                  }
                }
                throw new Error(
                  "Signer mismatch: expected " + expectedSigner +
                  " but only " + got + " is available. Please connect the required wallet (MetaMask) and retry."
                );
              }
            }
            var provider = await embedded.getEthereumProvider();
            if (!provider || typeof provider.request !== "function") {
              throw new Error(_t("pm.trade.providerUnavailable", "Wallet provider unavailable. Please re-login and retry."));
            }
            try { await _ensurePolygonChain(provider); } catch (_) { }
            var payload = typeof typedData === "string" ? typedData : JSON.stringify(typedData);
            var sig = await provider.request({
              method: "eth_signTypedData_v4",
              params: [String(embedded.address || ""), payload]
            });
            if (!sig || typeof sig !== "string" || !sig.startsWith("0x")) {
              throw new Error("invalid signature from embedded wallet");
            }
            return sig;
          };
          runtime.signMessage = async function (message, opts) {
            opts = opts || {};
            var expectedSigner = String(opts.expectedSigner || "").toLowerCase();
            var embedded = null;
            for (var i = 0; i < 80; i++) {
              var ws = Array.isArray(runtime._wallets) ? runtime._wallets : [];
              if (expectedSigner) {
                embedded = ws.find(function (w) {
                  return w && String(w.address || "").toLowerCase() === expectedSigner;
                }) || null;
              }
              if (!embedded) {
                embedded = ws.find(function (w) {
                  return w && w.walletClientType === "privy";
                }) || ws[0] || null;
              }
              if (runtime.status && runtime.status.ready && runtime.status.authenticated && embedded) break;
              await new Promise(function (r) {
                setTimeout(r, 250);
              });
            }
            if (!embedded) {
              throw new Error(_t("pm.trade.embeddedWalletMissing", "Embedded wallet not ready. Please re-login and retry."));
            }
            var provider = await embedded.getEthereumProvider();
            if (!provider || typeof provider.request !== "function") {
              throw new Error(_t("pm.trade.providerUnavailable", "Wallet provider unavailable. Please re-login and retry."));
            }
            var msg = String(message || "");
            var bytes = new TextEncoder().encode(msg);
            var hex = "0x";
            for (var k = 0; k < bytes.length; k++) {
              hex += bytes[k].toString(16).padStart(2, "0");
            }
            var sig = await provider.request({
              method: "personal_sign",
              params: [hex, String(embedded.address || "")]
            });
            if (!sig || typeof sig !== "string" || !sig.startsWith("0x")) {
              throw new Error("invalid signature from embedded wallet");
            }
            return sig;
          };
        }, [ready, authenticated, wallets.length, _fundWallet]);
        return createElement(React.Fragment, null);
      }
      function SigningRoot() {
        return createElement(PrivyProvider, {
          appId: cfg.app_id,
          config: {
            loginMethods: ["email"],
            appearance: {
              theme: "dark",
              accentColor: "#0ea5e9",
              showWalletLoginFirst: false,
              walletChainType: "ethereum-only"
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
        }, createElement(SigningInner));
      }
      if (!runtime._root) {
        runtime._root = createRoot(host);
        runtime._root.render(createElement(SigningRoot));
      }
      for (var j = 0; j < 80; j++) {
        if (typeof runtime.signTypedData === "function") return;
        await new Promise(function (r) {
          setTimeout(r, 50);
        });
      }
      throw new Error(_t("pm.trade.privyUnavailable", "Privy embedded wallet is not available in this session. Please re-login and retry."));
    }();
    return runtime._bootPromise;
  }
  window.veyraPrivySignTypedData = async function (typedData, opts) {
    await _ensureBoot();
    if (typeof runtime.signTypedData !== "function") {
      throw new Error(_t("pm.trade.privyUnavailable", "Privy embedded wallet is not available in this session. Please re-login and retry."));
    }
    return await runtime.signTypedData(typedData, opts || {});
  };
  window.veyraPrivySignMessage = async function (message, opts) {
    await _ensureBoot();
    if (typeof runtime.signMessage !== "function") {
      throw new Error(_t("pm.trade.privyUnavailable", "Privy embedded wallet is not available in this session. Please re-login and retry."));
    }
    return await runtime.signMessage(message, opts || {});
  };
  async function _whenWalletConnectorReady(maxMs) {
    await _ensureBoot();
    var deadline = Date.now() + (maxMs || 20000);
    while (Date.now() < deadline) {
      var st = runtime.status || {};
      var ws = Array.isArray(runtime._wallets) ? runtime._wallets : [];
      if (st.ready && st.authenticated && ws.length > 0) return true;
      await new Promise(function (r) { setTimeout(r, 200); });
    }
    return false;
  }
  async function _whenSdkReady(maxMs) {
    await _ensureBoot();
    var deadline = Date.now() + (maxMs || 8000);
    while (Date.now() < deadline) {
      var st = runtime.status || {};
      if (st.ready && typeof runtime.fundWallet === "function") return true;
      await new Promise(function (r) { setTimeout(r, 150); });
    }
    var st2 = runtime.status || {};
    return !!(st2.ready && typeof runtime.fundWallet === "function");
  }
  runtime.whenWalletConnectorReady = _whenWalletConnectorReady;
  runtime.whenSdkReady = _whenSdkReady;
  window.veyraPrivyFundWallet = async function (destination, opts) {
    var ok = await _whenSdkReady(8000);
    if (!ok) {
      throw new Error("Privy SDK not ready. Please reload and retry.");
    }
    return await runtime.fundWallet(destination, opts || {});
  };
  window.veyraGetPrivyEmbeddedWallet = async function (opts) {
    opts = opts || {};
    var expectedSigner = String(opts.expectedSigner || "").toLowerCase();
    var strict = !!opts.strict;
    await _ensureBoot();
    await _whenWalletConnectorReady(8000);
    var embedded = null;
    var lastWalletsSnapshot = [];
    for (var i = 0; i < 80; i++) {
      var ws = Array.isArray(runtime._wallets) ? runtime._wallets : [];
      lastWalletsSnapshot = ws;
      if (expectedSigner) {
        embedded = ws.find(function (w) {
          return w && String(w.address || "").toLowerCase() === expectedSigner;
        }) || null;
      }
      if (!embedded && !(expectedSigner && strict)) {
        embedded = ws.find(function (w) {
          return w && w.walletClientType === "privy";
        }) || ws[0] || null;
      }
      if (runtime.status && runtime.status.ready && runtime.status.authenticated && embedded) break;
      if (runtime.status && runtime.status.ready && runtime.status.authenticated && expectedSigner && strict) break;
      await new Promise(function (r) { setTimeout(r, 250); });
    }
    if (embedded) return embedded;

    if (expectedSigner && typeof window !== "undefined" && window.ethereum &&
      typeof window.ethereum.request === "function") {
      try {
        var accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
        var hit = (accounts || []).find(function (a) {
          return String(a || "").toLowerCase() === expectedSigner;
        });
        if (hit) {
          return {
            address: hit,
            walletClientType: "injected",
            connectorType: "injected",
            getEthereumProvider: async function () { return window.ethereum; },
          };
        }
        var active = (accounts || []).join(", ") || "(none)";
        throw new Error(
          "WALLET_BINDING_MISMATCH: your account is bound to " + expectedSigner +
          " but MetaMask is currently active on [" + active + "]. " +
          "Please switch MetaMask to account " + expectedSigner + " and retry."
        );
      } catch (e) {
        if (e && /WALLET_BINDING_MISMATCH/.test(String(e.message || e))) throw e;
      }
    }

    if (expectedSigner && strict) {
      var addrs = lastWalletsSnapshot.map(function (w) { return w && w.address; }).filter(Boolean).join(", ") || "(none)";
      var hasMM = !!(typeof window !== "undefined" && window.ethereum);
      throw new Error(
        "WALLET_BINDING_MISMATCH: expected signer " + expectedSigner +
        " not found in Privy wallets [" + addrs + "]" +
        (hasMM ? " and not unlocked in MetaMask." : " and no MetaMask detected.") +
        " Please connect MetaMask with account " + expectedSigner + " and reload."
      );
    }
    throw new Error(_t("pm.trade.embeddedWalletMissing", "Embedded wallet not ready. Please re-login and retry."));
  };
  runtime.ensureBoot = _ensureBoot;

  window.veyraEnsureDepositApprovals = async function (tFn) {
    var _t2 = function (k, fb) {
      try {
        if (typeof tFn === "function") return tFn(k, fb);
      } catch (_) { }
      return _t(k, fb);
    };
    var checkRes;
    try {
      checkRes = await fetch("/api/pm/trade/provision-approvals/check", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
    } catch (e) {
      return { error: _t2("pm.trade.provisionCheckFailed", "Approval pre-check failed: ") + (e && e.message || e) };
    }
    var checkData = {};
    try { checkData = await checkRes.json(); } catch (_) { checkData = {}; }
    if (!checkRes.ok) {
      return { error: (checkData && (checkData.error || checkData.raw_error)) || ("HTTP " + checkRes.status) };
    }
    var status = String(checkData && checkData.status || "");
    if (status === "ok" || status === "no_credentials" || status === "unbound") {
      return { ok: true, status: status };
    }
    if (status !== "needs_provisioning") {
      return { error: (checkData && (checkData.error || checkData.raw_error)) || _t2("pm.trade.provisionUnknownStatus", "Approval pre-check returned unknown status: ") + status };
    }
    for (var w = 0; w < 40; w++) {
      if (typeof window.veyraPrivySignTypedData === "function") break;
      await new Promise(function (r) { setTimeout(r, 100); });
    }
    if (typeof window.veyraPrivySignTypedData !== "function") {
      return { error: _t2("pm.trade.privyUnavailable", "Privy embedded wallet is not available in this session. Please re-login and retry.") };
    }
    var ownerEoa = String(checkData.owner_eoa || "").toLowerCase();
    var sig;
    try {
      sig = await window.veyraPrivySignTypedData(checkData.typed_data, {
        expectedSigner: ownerEoa
      });
    } catch (e) {
      return { error: _t2("pm.trade.provisionSignFailed", "Approval signing failed: ") + (e && e.message || e) };
    }
    var subRes;
    try {
      subRes = await fetch("/api/pm/trade/provision-approvals/submit", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prepare_token: checkData.prepare_token,
          signature: sig
        })
      });
    } catch (e) {
      return { error: _t2("pm.trade.provisionSubmitFailed", "Approval submission failed: ") + (e && e.message || e) };
    }
    var subData = {};
    try { subData = await subRes.json(); } catch (_) { subData = {}; }
    if (!subRes.ok) {
      return { error: (subData && (subData.error || subData.raw_error)) || ("HTTP " + subRes.status) };
    }
    if (String(subData && subData.status || "") !== "mined") {
      return { error: _t2("pm.trade.provisionNotMined", "Approval transaction not confirmed: ") + (subData && subData.status || "?") };
    }
    return { ok: true, status: "mined", tx_hash: subData.tx_hash };
  };

  function _scheduleWarmup() {
    var run = function () {
      try {
        var p = String(location && location.pathname || "").toLowerCase();
        var needsPrivy = (
          p.indexOf("/discover") >= 0 ||
          p.indexOf("/events") >= 0 ||
          p.indexOf("/portfolio") >= 0 ||
          p.indexOf("/deposit") >= 0 ||
          p.indexOf("/withdraw") >= 0 ||
          p.indexOf("/profile") >= 0 ||
          p.indexOf("/dashboard") >= 0 ||
          p === "/v2" || p === "/v2/" || p === "/"
        );
        if (!needsPrivy) return;
        _ensureBoot().catch(function () { });
      } catch (_) { }
    };
    if (document.readyState === "complete" || document.readyState === "interactive") {
      run();
    } else {
      window.addEventListener("DOMContentLoaded", run, { once: true });
    }
  }
  _scheduleWarmup();
})();
