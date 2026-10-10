(function () {
  "use strict";

  var PAGE_PACK = "withdraw";
  var PAGE_I18N = {
    current: {},
    en: {}
  };

  function _lang() {
    return (window.veyraI18n && window.veyraI18n.lang) || document.documentElement.getAttribute("data-lang") || document.documentElement.lang || "en";
  }

  function _pageText(key) {
    var cur = PAGE_I18N.current || {};
    if (cur[key] != null && cur[key] !== "") return cur[key];
    var en = PAGE_I18N.en || {};
    if (en[key] != null && en[key] !== "") return en[key];
    return "";
  }

  function _t(key, fallback) {
    var local = _pageText(key);
    if (local) return local;
    return (window.veyraI18n && window.veyraI18n.t(key, fallback)) || fallback;
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
      PAGE_I18N.current = arr[0] || {};
      PAGE_I18N.en = arr[1] || {};
      var pageTitle = _pageText("page.withdraw.title") || "Withdraw";
      document.title = "V e Y r a - " + pageTitle;
    });
  }

  function _esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  var S = {
    loading: true,
    submitting: false,
    balance: 0,
    toAddress: "",
    amount: "",
    error: "",
    txHash: ""
  };

  var root = null;

  function _root() {
    if (!root) root = document.getElementById("vp-withdraw-slot");
    return root;
  }

  function _postJson(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        return { ok: resp.ok, status: resp.status, data: data || {} };
      });
    });
  }

  function _showSuccessModal(txHash) {
    var safeHash = String(txHash || "").trim();
    var hasValidHash = /^0x[a-fA-F0-9]{64}$/.test(safeHash);
    var bodyHtml = _esc(_t("withdraw.success", "Withdrawal submitted successfully."));
    if (hasValidHash) {
      bodyHtml += '<div style="margin-top:8px;font-size:.9rem">Tx Hash: <a target="_blank" rel="noopener" href="https://polygonscan.com/tx/' + _esc(safeHash) + '" style="color:var(--accent)">' + _esc(safeHash) + '</a></div>';
    }
    if (window.vp && typeof window.vp.success === "function") {
      window.vp.success({
        title: _t("withdraw.success", "Withdrawal submitted successfully."),
        bodyHtml: bodyHtml
      }).finally(function () {
        window.location.reload();
      });
    }
  }

  function _packSafeSignature(rawSig) {
    var sig = String(rawSig || "").toLowerCase();
    if (sig.indexOf("0x") === 0) sig = sig.slice(2);
    if (sig.length !== 130) {
      throw new Error("Invalid signature length: " + sig.length);
    }
    var r = sig.slice(0, 64);
    var s = sig.slice(64, 128);
    var v = parseInt(sig.slice(128, 130), 16);
    if (v === 0 || v === 1) v += 31;
    else if (v === 27 || v === 28) v += 4;
    else throw new Error("Invalid signature v: " + v);
    var vHex = v.toString(16);
    if (vHex.length === 1) vHex = "0" + vHex;
    return "0x" + r + s + vHex;
  }

  function _getProvider() {
    return window.ethereum || window.privyEthereumProvider || null;
  }

  function _signSafeStructHash(structHash, signingAddress) {
    var provider = _getProvider();
    if (!provider || typeof provider.request !== "function") {
      return Promise.reject(new Error(_t("withdraw.err.noWalletProvider", "No browser wallet provider available. Please connect MetaMask or another EVM wallet.")));
    }
    var addrLc = String(signingAddress || "").toLowerCase();
    return provider.request({ method: "eth_requestAccounts" }).then(function (accounts) {
      var account = accounts && accounts[0] ? String(accounts[0]).toLowerCase() : "";
      if (account && addrLc && account !== addrLc) {
        return Promise.reject(new Error(_t("withdraw.err.walletMismatch", "Connected wallet does not match required signer. Please switch your wallet account to ") + signingAddress));
      }
      var hex = String(structHash || "");
      if (hex.indexOf("0x") === 0) hex = hex.slice(2);
      if (hex.length !== 64) {
        return Promise.reject(new Error("Server struct_hash must be 32 bytes (64 hex chars), got " + hex.length));
      }
      return provider.request({
        method: "personal_sign",
        params: ["0x" + hex, signingAddress]
      }).then(_packSafeSignature);
    });
  }

  function _signSafeTypedData(typedData, signingAddress) {
    var provider = _getProvider();
    if (!provider || typeof provider.request !== "function") {
      return Promise.reject(new Error(_t("withdraw.err.noWalletProvider", "No browser wallet provider available. Please connect MetaMask or another EVM wallet.")));
    }
    var addrLc = String(signingAddress || "").toLowerCase();
    return provider.request({ method: "eth_requestAccounts" }).then(function (accounts) {
      var account = accounts && accounts[0] ? String(accounts[0]).toLowerCase() : "";
      if (account && addrLc && account !== addrLc) {
        return Promise.reject(new Error(_t("withdraw.err.walletMismatch", "Connected wallet does not match required signer. Please switch your wallet account to ") + signingAddress));
      }
      return provider.request({
        method: "eth_signTypedData_v4",
        params: [signingAddress, typeof typedData === "string" ? typedData : JSON.stringify(typedData || {})]
      });
    });
  }

  function _submitWithClientSignature(addr, amt, resData) {
    var method = String(resData.signing_method || "").toLowerCase();
    var signer = String(resData.signing_address || "");
    var signPromise = (method === "eip712_batch" || resData.typed_data)
      ? _signSafeTypedData(resData.typed_data, signer)
      : _signSafeStructHash(resData.struct_hash, signer);
    return signPromise.then(function (signature) {
      var relayerTx = Object.assign({}, resData.payload_skeleton || {}, {
        signature: signature
      });
      return _postJson("/api/pm/safe-withdraw", {
        to_address: addr,
        amount_usdc: amt,
        token: "pusd",
        relayer_tx: relayerTx
      });
    });
  }

  function _fetchBalance() {
    S.loading = true;
    S.error = "";
    render();
    fetch("/api/pm/deposit-info", { credentials: "include" })
      .then(function (resp) { return resp.json().then(function (d) { return { ok: resp.ok, data: d || {} }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(String(res.data.error || ("HTTP " + res.status)));
        var bal = 0;
        if (res.data && typeof res.data.tradable_balance === "number") bal = Number(res.data.tradable_balance || 0);
        else if (res.data && typeof res.data.pusd_balance === "number") bal = Number(res.data.pusd_balance || 0);
        S.balance = Math.max(0, bal);
        S.loading = false;
        render();
      })
      .catch(function (err) {
        S.loading = false;
        S.error = String(err && err.message || err);
        render();
      });
  }

  function _submit() {
    if (S.submitting) return;
    S.error = "";
    S.txHash = "";

    var addr = String(S.toAddress || "").trim();
    if (!/^0x[a-fA-F0-9]{40}$/.test(addr)) {
      S.error = _t("withdraw.addressInvalid", "Destination address is invalid.");
      render();
      return;
    }

    var amt = Number(S.amount || 0);
    if (!isFinite(amt) || amt <= 0) {
      S.error = _t("withdraw.amountInvalid", "Amount must be greater than 0.");
      render();
      return;
    }

    if (amt > S.balance) {
      S.error = _t("withdraw.amountTooHigh", "Amount exceeds available pUSD balance.");
      render();
      return;
    }

    S.submitting = true;
    render();

    _postJson("/api/pm/safe-withdraw", {
      to_address: addr,
      amount_usdc: amt,
      token: "pusd"
    }).then(function (res) {
      if (res.data && res.data.requires_client_sign) {
        return _submitWithClientSignature(addr, amt, res.data);
      }
      return res;
    }).then(function (res) {
      S.submitting = false;
      if (!res.ok || !res.data || !res.data.success) {
        S.error = String((res.data && (res.data.error || res.data.message || res.data.detail)) || ("HTTP " + res.status));
        render();
        return;
      }
      S.txHash = String(
        res.data.tx_hash ||
        res.data.transactionHash ||
        (res.data.relayer && (res.data.relayer.transactionHash || res.data.relayer.txHash || res.data.relayer.hash)) ||
        ""
      );
      _showSuccessModal(S.txHash);
      S.amount = "";
      render();
      _fetchBalance();
    }).catch(function (err) {
      S.submitting = false;
      S.error = String(err && err.message || err || _t("withdraw.networkError", "Network error, please retry"));
      render();
    });
  }

  function render() {
    var el = _root();
    if (!el) return;

    if (S.loading) {
      el.innerHTML = '<div class="v-card" style="margin-top:12px">' + _esc(_t("withdraw.loading", "Loading...")) + '</div>';
      return;
    }

    var html = "";
    html += '<div class="v-card" style="margin-top:12px">';
    html += '<div style="font-size:0.82rem;color:var(--text-muted)">' + _esc(_t("withdraw.available", "Available pUSD")) + '</div>';
    html += '<div style="margin-top:4px;font-size:2rem;font-weight:800;color:#10b981">$' + _esc(S.balance.toFixed(2)) + '</div>';
    html += '</div>';

    html += '<div class="v-card" style="margin-top:12px">';
    html += '<div style="font-size:1.1rem;font-weight:700">' + _esc(_t("withdraw.titleSimple", "Send Withdrawal")) + '</div>';

    html += '<div style="margin-top:14px;font-size:0.78rem;color:var(--text-muted)">' + _esc(_t("withdraw.destPolygon", "Destination Address (Polygon)")) + '</div>';
    html += '<input id="vp-wd-address" type="text" value="' + _esc(S.toAddress) + '" placeholder="0x..." style="margin-top:6px;width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary);font-family:monospace" />';

    html += '<div style="margin-top:14px;font-size:0.78rem;color:var(--text-muted)">' + _esc(_t("withdraw.amountPusd", "Amount (pUSD)")) + '</div>';
    html += '<div style="display:flex;gap:8px;align-items:center;margin-top:6px">';
    html += '<input id="vp-wd-amount" type="number" min="0" step="0.01" value="' + _esc(S.amount) + '" placeholder="0.00" style="flex:1;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary)" />';
    html += '<button id="vp-wd-max" class="v-btn v-btn-outline v-btn-sm">' + _esc(_t("withdraw.max", "Max")) + '</button>';
    html += '</div>';

    html += '<button id="vp-wd-submit" class="v-btn v-btn-primary" style="margin-top:14px;width:100%;padding:12px 14px;font-size:1rem"' + (S.submitting ? ' disabled' : '') + '>' + _esc(S.submitting ? _t("withdraw.submitting", "Submitting...") : _t("withdraw.send", "Withdraw")) + '</button>';

    html += '<div style="margin-top:12px;padding:10px 12px;border-radius:8px;background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.38);font-size:0.82rem;color:#fcd34d">';
    html += _esc(_t("withdraw.warningPolygonOnly", "Only send to Polygon mainnet addresses. Withdrawals are irreversible."));
    html += '</div>';

    if (S.error) {
      html += '<div class="v-surface-danger" style="margin-top:12px">' + _esc(S.error) + '</div>';
    }
    html += '</div>';

    el.innerHTML = html;

    var addrInput = document.getElementById("vp-wd-address");
    var amtInput = document.getElementById("vp-wd-amount");
    var maxBtn = document.getElementById("vp-wd-max");
    var submitBtn = document.getElementById("vp-wd-submit");

    if (addrInput) {
      addrInput.addEventListener("input", function () {
        S.toAddress = addrInput.value || "";
      });
    }
    if (amtInput) {
      amtInput.addEventListener("input", function () {
        S.amount = amtInput.value || "";
      });
    }
    if (maxBtn) {
      maxBtn.addEventListener("click", function () {
        S.amount = S.balance.toFixed(2);
        render();
      });
    }
    if (submitBtn) {
      submitBtn.addEventListener("click", _submit);
    }
  }

  function init() {
    if (!_root()) return;
    document.addEventListener("i18n:changed", function () {
      _loadPageI18n().finally(function () {
        render();
      });
    });
    _loadPageI18n().finally(function () {
      _fetchBalance();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
