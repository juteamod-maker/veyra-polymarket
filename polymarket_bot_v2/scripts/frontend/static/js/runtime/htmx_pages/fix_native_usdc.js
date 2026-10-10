(function () {
    "use strict";

    var S = {
        loading: true,
        busy: false,
        state: null,
        error: "",
        ok: "",
        txHash: ""
    };

    var _root = null;

    function _getRoot() {
        if (!_root) _root = document.getElementById("vp-fix-native-usdc-slot");
        return _root;
    }

    function _esc(s) {
        return String(s == null ? "" : s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/\"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function _signTypedData(typedData, signingAddress) {
        var provider = window.ethereum || window.privyEthereumProvider || null;
        if (!provider || typeof provider.request !== "function") {
            return Promise.reject(new Error("No browser wallet provider available."));
        }
        var required = String(signingAddress || "").toLowerCase();
        return provider.request({ method: "eth_requestAccounts" }).then(function (accounts) {
            var current = accounts && accounts[0] ? String(accounts[0]).toLowerCase() : "";
            if (!current || !required || current !== required) {
                return Promise.reject(new Error("Connected wallet does not match the authorized signer."));
            }
            var payload = typeof typedData === "string" ? typedData : JSON.stringify(typedData || {});
            return provider.request({
                method: "eth_signTypedData_v4",
                params: [signingAddress, payload]
            });
        });
    }

    function _postJson(url, body) {
        return fetch(url, {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {})
        }).then(function (resp) {
            return resp.json().catch(function () { return {}; }).then(function (json) {
                if (!resp.ok) {
                    var err = new Error(String(json.error || json.message || ("HTTP " + resp.status)));
                    err.payload = json;
                    throw err;
                }
                return json;
            });
        });
    }

    function _loadState() {
        S.loading = true;
        S.error = "";
        return fetch("/api/fix-native-usdc/state", { credentials: "include" })
            .then(function (resp) {
                return resp.json().catch(function () { return {}; }).then(function (json) {
                    if (!resp.ok) {
                        throw new Error(String(json.error || ("HTTP " + resp.status)));
                    }
                    S.state = json;
                    S.loading = false;
                    render();
                });
            })
            .catch(function (err) {
                S.loading = false;
                S.error = String(err && err.message || err);
                render();
            });
    }

    function _submitFlow() {
        if (S.busy) return;
        S.busy = true;
        S.error = "";
        S.ok = "";
        S.txHash = "";
        render();

        _postJson("/api/fix-native-usdc/prepare", {})
            .then(function (prep) {
                return _signTypedData(prep.typed_data, prep.signing_address).then(function (sig) {
                    return _postJson("/api/fix-native-usdc/submit", {
                        signature: sig,
                        nonce: prep.nonce,
                        depositWalletParams: prep.depositWalletParams
                    });
                });
            })
            .then(function (result) {
                S.ok = "执行成功：3 USDC 已提交到 Bridge 自动转换。";
                S.txHash = String(result.tx_hash || "");
                S.busy = false;
                _loadState();
            })
            .catch(function (err) {
                S.busy = false;
                S.error = String(err && err.message || err);
                render();
            });
    }

    function render() {
        var root = _getRoot();
        if (!root) return;

        if (S.loading) {
            root.innerHTML = '<div class="v-card" style="margin-top:12px">Loading...</div>';
            return;
        }

        if (S.error && !S.state) {
            root.innerHTML = '<div class="v-card v-surface-danger" style="margin-top:12px">' + _esc(S.error) + '</div>';
            return;
        }

        var balance = S.state ? String(S.state.native_usdc_display || "0.00") : "0.00";
        var canSubmit = !S.busy && Number(balance) > 0;

        var html = "";
        html += '<div class="v-card" style="margin-top:12px;border:1px solid rgba(245,158,11,.45);background:linear-gradient(145deg,rgba(245,158,11,.13),rgba(245,158,11,.04));">';
        html += '<div style="font-size:1.2rem;font-weight:800">Temporary Native USDC Fix</div>';
        html += '<div style="margin-top:10px;color:var(--text-secondary)">当前 Deposit Wallet native USDC 余额：</div>';
        html += '<div style="margin-top:4px;font-size:2rem;font-weight:800;color:#fbbf24">' + _esc(balance) + ' USDC</div>';
        html += '<div style="margin-top:14px;padding:10px 12px;border-radius:10px;border:1px dashed rgba(255,255,255,.25);color:var(--text-primary)">正在把 3 USDC 转到 Bridge 地址以自动转换为 pUSD</div>';
        html += '<div style="margin-top:16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center">';
        html += '<button id="vp-fix-native-submit" class="v-btn v-btn-primary" ' + (canSubmit ? "" : "disabled") + ' style="font-size:1rem;padding:12px 20px">' + (S.busy ? "处理中..." : "签名并转移 3 USDC 到 Bridge") + '</button>';
        if (S.state && S.state.deposit_wallet) {
            html += '<span style="font-size:.78rem;color:var(--text-muted)">Deposit Wallet: ' + _esc(S.state.deposit_wallet) + '</span>';
        }
        html += '</div>';

        if (S.ok) {
            html += '<div class="v-surface-success" style="margin-top:14px">' + _esc(S.ok) + '</div>';
        }
        if (S.txHash) {
            html += '<div style="margin-top:8px;font-size:.86rem">Tx Hash: <a target="_blank" rel="noopener" href="https://polygonscan.com/tx/' + _esc(S.txHash) + '" style="color:#38bdf8">' + _esc(S.txHash) + '</a></div>';
        }
        if (S.error) {
            html += '<div class="v-surface-danger" style="margin-top:12px">' + _esc(S.error) + '</div>';
        }

        html += '</div>';
        root.innerHTML = html;

        var btn = document.getElementById("vp-fix-native-submit");
        if (btn) {
            btn.addEventListener("click", _submitFlow);
        }
    }

    function init() {
        if (!_getRoot()) return;
        _loadState();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
