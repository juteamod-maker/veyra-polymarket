
(function () {
    'use strict';

    const RELAYER_URL = 'https://relayer-v2.polymarket.com';
    const CLOB_HOST = 'https://clob.polymarket.com';
    const CHAIN_ID = 137;
    const DEPOSIT_WALLET_FACTORY = '0x00000000000Fb5C9ADea0298D729A0CB3823Cc07';

    const PUSD_TOKEN = '0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb';
    const CTF = '0x4d97dcd97ec945f40cf65f87097ace5ea0476045';
    const CTF_EXCHANGE_V2 = '0xe111180000d2663c0091e4f400237545b87b996b';
    const NEG_RISK_EXCHANGE_V2 = '0xe2222d279d744050d28e00520010520000310f59';
    const NEG_RISK_ADAPTER = '0xd91e80cf2e7be2e162c6513ced06f1dd0da35296';

    const MAX_UINT256 = (1n << 256n) - 1n;

    const ERC20_APPROVE_ABI = [{
        type: 'function', name: 'approve', stateMutability: 'nonpayable',
        inputs: [{ name: 'spender', type: 'address' }, { name: 'value', type: 'uint256' }],
        outputs: [{ name: '', type: 'bool' }],
    }];
    const ERC20_ALLOWANCE_ABI = [{
        type: 'function', name: 'allowance', stateMutability: 'view',
        inputs: [{ name: 'owner', type: 'address' }, { name: 'spender', type: 'address' }],
        outputs: [{ name: '', type: 'uint256' }],
    }];
    const ERC1155_SET_APPROVAL_ABI = [{
        type: 'function', name: 'setApprovalForAll', stateMutability: 'nonpayable',
        inputs: [{ name: 'operator', type: 'address' }, { name: 'approved', type: 'bool' }],
        outputs: [],
    }];
    const ERC1155_IS_APPROVED_ABI = [{
        type: 'function', name: 'isApprovedForAll', stateMutability: 'view',
        inputs: [{ name: 'account', type: 'address' }, { name: 'operator', type: 'address' }],
        outputs: [{ name: '', type: 'bool' }],
    }];

    const REQUIRED_APPROVALS = [
        { kind: 'erc20', token: PUSD_TOKEN, spender: CTF_EXCHANGE_V2, label: 'pUSD → CTF Exchange' },
        { kind: 'erc20', token: PUSD_TOKEN, spender: NEG_RISK_EXCHANGE_V2, label: 'pUSD → NegRisk Exchange' },
        { kind: 'erc20', token: PUSD_TOKEN, spender: NEG_RISK_ADAPTER, label: 'pUSD → NegRisk Adapter' },
        { kind: 'erc1155', token: CTF, spender: CTF_EXCHANGE_V2, label: 'CTF → CTF Exchange' },
        { kind: 'erc1155', token: CTF, spender: NEG_RISK_EXCHANGE_V2, label: 'CTF → NegRisk Exchange' },
        { kind: 'erc1155', token: CTF, spender: NEG_RISK_ADAPTER, label: 'CTF → NegRisk Adapter' },
    ];
    const SDK_BUNDLE_URL = '/v2/static/js/runtime/polymarket_sdk_bundle.js';

    function _trace(step, status, msg, extra) {
        try {
            if (typeof window === 'undefined' || window.__pmGuestMode !== false) {
                return;
            }
            try {
                if (status === 'fail') console.error('[veyra-trace]', step, msg, extra || '');
                else console.log('[veyra-trace]', step, status, msg || '', extra || '');
            } catch (_) { }
        } catch (_) { }
    }
    function _errMsg(e) {
        if (!e) return '';
        if (typeof e === 'string') return e;
        var m = (e.message || e.shortMessage || e.reason || e.code || '').toString();
        var stack = (e.stack || '').toString().split('\n').slice(0, 3).join(' | ');
        return (m + ' :: ' + stack).slice(0, 900);
    }

    let _sdkPromise = null;

    function loadSDKs() {
        if (_sdkPromise) return _sdkPromise;
        _sdkPromise = (async () => {
            const m = await import(SDK_BUNDLE_URL);
            return {
                RelayClient: m.RelayClient,
                RelayerTransactionState: m.RelayerTransactionState,
                ClobClient: m.ClobClient,
                SignatureTypeV2: m.SignatureTypeV2,
                AssetType: m.AssetType,
                OrderType: m.OrderType,
                Side: m.Side,
                BuilderConfig: m.BuilderConfig,
                createWalletClient: m.createWalletClient,
                custom: m.custom,
                encodeFunctionData: m.encodeFunctionData,
                polygon: m.polygon,
            };
        })();
        return _sdkPromise;
    }

    function _showMismatchModal(opts) {
        var expected = String(opts && opts.expected || '');
        var actual = String(opts && opts.actual || '(none)');
        var bodyText = 'Your account is bound to EOA:\n  ' + expected + '\n\n' +
            'Currently active wallet:\n  ' + actual + '\n\n' +
            'Action required:\n' +
            '  1. Open MetaMask\n' +
            '  2. Switch to account ' + expected + '\n' +
            '  3. Make sure it is connected to veyra.trade\n' +
            '  4. Reload this page\n\n' +
            'Without this, your deposit wallet funds cannot be signed for from this session.';
        try {
            if (typeof window.veyraShowAlertModal === 'function') {
                window.veyraShowAlertModal({
                    title: 'Wallet Binding Mismatch',
                    body: bodyText,
                });
                return;
            }
        } catch (_) { }
        try {
            var existing = document.getElementById('veyra-wallet-mismatch-modal');
            if (existing) existing.parentNode.removeChild(existing);
            var wrap = document.createElement('div');
            wrap.id = 'veyra-wallet-mismatch-modal';
            wrap.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:99999;display:flex;align-items:center;justify-content:center;';
            wrap.innerHTML =
                '<div style="background:#fff;color:#111;max-width:560px;padding:24px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,0.3);font-family:system-ui,sans-serif;">' +
                '<h3 style="margin:0 0 12px 0;font-size:18px;">Wallet Binding Mismatch</h3>' +
                '<p style="margin:0 0 6px 0;font-size:14px;line-height:1.5;">Your account is bound to:</p>' +
                '<code style="display:block;padding:8px;background:#f3f4f6;border-radius:6px;font-size:12px;word-break:break-all;margin-bottom:8px;">' + expected + '</code>' +
                '<p style="margin:0 0 6px 0;font-size:14px;line-height:1.5;">Currently active wallet:</p>' +
                '<code style="display:block;padding:8px;background:#fef3c7;border-radius:6px;font-size:12px;word-break:break-all;margin-bottom:12px;">' + actual + '</code>' +
                '<div style="font-size:13px;line-height:1.6;color:#374151;background:#f9fafb;padding:12px;border-radius:6px;margin-bottom:16px;">' +
                '<strong>How to fix:</strong><br>' +
                '1. Open MetaMask<br>' +
                '2. Switch to account <code style="font-size:11px;">' + expected.slice(0, 10) + '…' + expected.slice(-6) + '</code><br>' +
                '3. Make sure it is connected to veyra.trade<br>' +
                '4. Reload this page' +
                '</div>' +
                '<div style="text-align:right;display:flex;gap:8px;justify-content:flex-end;">' +
                '<button id="veyra-mismatch-connect" style="padding:8px 16px;background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px;">Connect MetaMask</button>' +
                '<button id="veyra-mismatch-close" style="padding:8px 16px;background:#111;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px;">Close</button>' +
                '</div></div>';
            document.body.appendChild(wrap);
            var closeBtn = document.getElementById('veyra-mismatch-close');
            if (closeBtn) closeBtn.onclick = function () { wrap.parentNode && wrap.parentNode.removeChild(wrap); };
            var connectBtn = document.getElementById('veyra-mismatch-connect');
            if (connectBtn) {
                connectBtn.onclick = async function () {
                    try {
                        if (window.ethereum && typeof window.ethereum.request === 'function') {
                            await window.ethereum.request({ method: 'eth_requestAccounts' });
                            try {
                                await window.ethereum.request({
                                    method: 'wallet_requestPermissions',
                                    params: [{ eth_accounts: {} }],
                                });
                            } catch (_) { }
                            location.reload();
                        } else {
                            alert('MetaMask not detected. Please install MetaMask and reload.');
                        }
                    } catch (e) {
                        alert('Connect failed: ' + (e && e.message || e));
                    }
                };
            }
        } catch (_) { }
    }

    async function getPrivyWalletClient(sdks, expectedSigner) {
        if (typeof window.veyraGetPrivyEmbeddedWallet !== 'function') {
            throw new Error('Privy runtime not loaded');
        }
        const wallet = await window.veyraGetPrivyEmbeddedWallet(
            expectedSigner ? { expectedSigner: expectedSigner, strict: true } : {}
        );
        if (!wallet || !wallet.getEthereumProvider) {
            throw new Error('Privy embedded wallet unavailable');
        }
        if (expectedSigner && String(wallet.address || '').toLowerCase() !== String(expectedSigner).toLowerCase()) {
            _showMismatchModal({ expected: expectedSigner, actual: wallet.address });
            throw new Error(
                'WALLET_BINDING_MISMATCH: backend session is bound to ' +
                expectedSigner + ' but Privy currently exposes ' + wallet.address +
                '. Please reconnect the original owner wallet.'
            );
        }
        const provider = await wallet.getEthereumProvider();

        try {
            const cur = await provider.request({ method: 'eth_chainId' });
            if (String(cur).toLowerCase() !== '0x89') {
                try {
                    await provider.request({
                        method: 'wallet_switchEthereumChain',
                        params: [{ chainId: '0x89' }],
                    });
                } catch (switchErr) {
                    if (switchErr && (switchErr.code === 4902 || /Unrecognized chain/i.test(String(switchErr.message || '')))) {
                        await provider.request({
                            method: 'wallet_addEthereumChain',
                            params: [{
                                chainId: '0x89',
                                chainName: 'Polygon',
                                nativeCurrency: { name: 'POL', symbol: 'POL', decimals: 18 },
                                rpcUrls: ['https://polygon-rpc.com'],
                                blockExplorerUrls: ['https://polygonscan.com'],
                            }],
                        });
                        await provider.request({
                            method: 'wallet_switchEthereumChain',
                            params: [{ chainId: '0x89' }],
                        });
                    } else {
                        throw switchErr;
                    }
                }
            }
        } catch (chainErr) {
            throw new Error('Failed to switch wallet to Polygon: ' + (chainErr && chainErr.message || chainErr));
        }

        const account = wallet.address;
        const walletClient = sdks.createWalletClient({
            account,
            chain: sdks.polygon,
            transport: sdks.custom(provider),
        });
        return { walletClient, ownerEoa: account };
    }

    class PolymarketTradingSession {
        constructor() {
            this._ready = null;
            this._sdks = null;
            this._walletClient = null;
            this._ownerEoa = null;
            this._depositWallet = null;
            this._relayer = null;
            this._clob = null;
            this._creds = null;
            this._builderConfig = null;
        }

        async _init() {
            if (this._sdks) return;
            _trace('init', 'start');
            this._sdks = await loadSDKs();
            _trace('init', 'sdksLoaded');
            const binding = await this.getWalletBinding();
            if (!binding || !binding.user_wallet) {
                _trace('init', 'fail', 'binding missing');
                throw new Error('Wallet binding missing — please complete onboarding at /go.');
            }
            const walletType = String(binding.wallet_type || '').toLowerCase();
            if (walletType === 'safe' || walletType === 'gnosis_safe' || walletType === 'proxy') {
                _trace('init', 'fail', 'legacy_safe_user_unsupported_in_v2', { walletType: walletType });
                _showMismatchModal({
                    expected: binding.owner_eoa || binding.user_wallet || '(unknown)',
                    actual: 'Legacy ' + walletType + ' wallet (signature_type=2). The new Deposit Wallet pipeline requires signature_type=3.',
                });
                throw new Error('LEGACY_SAFE_USER_NOT_SUPPORTED: backend wallet_type=' + walletType + '. Please contact support to migrate to a Deposit Wallet account.');
            }
            if (walletType && walletType !== 'deposit_wallet') {
                _trace('init', 'fail', 'unknown_wallet_type', { walletType: walletType });
                throw new Error('UNKNOWN_WALLET_TYPE: backend returned wallet_type=' + walletType + '. Refusing to proceed.');
            }
            if (walletType === 'deposit_wallet' && !binding.funder_address) {
                _trace('init', 'fail', 'deposit_wallet_funder_missing');
                throw new Error('DEPOSIT_WALLET_FUNDER_MISSING: backend says wallet_type=deposit_wallet but funder_address is empty. Refusing to derive from SDK alone — please re-run onboarding at /go.');
            }
            const expectedSigner = binding.owner_eoa || binding.user_wallet;
            _trace('init', 'binding', '', { expectedSigner: expectedSigner, deposit: binding.deposit_wallet, walletType: walletType });
            try {
                const { walletClient, ownerEoa } = await getPrivyWalletClient(this._sdks, expectedSigner);
                this._walletClient = walletClient;
                this._ownerEoa = ownerEoa;
                _trace('init', 'walletClientOk', '', { ownerEoa: ownerEoa });
            } catch (e) {
                _trace('init', 'fail', _errMsg(e));
                throw e;
            }
            this._builderConfig = new this._sdks.BuilderConfig({
                remoteBuilderConfig: { url: location.origin + '/api/polymarket/sign' },
            });
            this._relayer = new this._sdks.RelayClient(
                RELAYER_URL,
                CHAIN_ID,
                this._walletClient,
                this._builderConfig
            );
            _trace('init', 'ok');
        }

        async getDepositWalletAddress() {
            await this._init();
            if (this._depositWallet) return this._depositWallet;
            const binding = await this.getWalletBinding();
            const dbFunder = String(binding && binding.funder_address || '').toLowerCase();
            if (!dbFunder) {
                _showMismatchModal({
                    expected: this._ownerEoa || '(unknown)',
                    actual: 'backend returned empty funder_address — refusing to fall back to SDK derivation',
                });
                throw new Error('DEPOSIT_WALLET_FUNDER_MISSING: backend funder_address is empty. Refusing to derive from current EOA alone (would risk signing for the wrong vault).');
            }
            let sdkDerived = '';
            try {
                sdkDerived = String(await this._relayer.deriveDepositWalletAddress() || '').toLowerCase();
            } catch (e) {
                _trace('getDepositWalletAddress', 'sdk_derive_failed', _errMsg(e));
            }
            if (sdkDerived && sdkDerived !== dbFunder) {
                _showMismatchModal({
                    expected: this._ownerEoa,
                    actual: this._ownerEoa + ' → derives ' + sdkDerived + ' (expected ' + dbFunder + ')',
                });
                throw new Error(
                    'DEPOSIT_WALLET_MISMATCH: SDK derived ' + sdkDerived +
                    ' from current EOA but backend funder_address is ' + dbFunder +
                    '. Wallet binding is inconsistent — reconnect the original owner wallet.'
                );
            }
            this._depositWallet = dbFunder;
            return this._depositWallet;
        }

        async ensureWalletDeployed() {
            const addr = await this.getDepositWalletAddress();
            const lsKey = 'veyra:wallet_deployed:' + CHAIN_ID + ':' + String(addr || '').toLowerCase();
            try {
                if (window.localStorage && window.localStorage.getItem(lsKey) === '1') {
                    return { deployed: true, address: addr, txHash: null, cached: true };
                }
            } catch (_) { }
            let deployed = false;
            try {
                deployed = await this._relayer.getDeployed(addr);
            } catch (e) {
                deployed = false;
            }
            if (deployed) {
                try { if (window.localStorage) window.localStorage.setItem(lsKey, '1'); } catch (_) { }
                return { deployed: true, address: addr, txHash: null };
            }
            try {
                const r = await this._relayer.deployDepositWallet();
                const confirmed = await r.wait();
                try { if (window.localStorage) window.localStorage.setItem(lsKey, '1'); } catch (_) { }
                return { deployed: true, address: addr, txHash: confirmed && (confirmed.txHash || confirmed.hash) };
            } catch (deployErr) {
                try {
                    const recheck = await this._relayer.getDeployed(addr);
                    if (recheck) {
                        try { if (window.localStorage) window.localStorage.setItem(lsKey, '1'); } catch (_) { }
                        return { deployed: true, address: addr, txHash: null, recovered: true };
                    }
                } catch (_) { }
                throw deployErr;
            }
        }

        async ensureCredentials() {
            if (this._creds) return this._creds;
            await this._init();
            const addr = await this.getDepositWalletAddress();
            const lsKey = 'veyra:clob_creds:' + CHAIN_ID + ':' + String(addr || '').toLowerCase();
            try {
                const cached = window.localStorage && window.localStorage.getItem(lsKey);
                if (cached) {
                    const parsed = JSON.parse(cached);
                    if (parsed && parsed.key && parsed.secret && parsed.passphrase) {
                        this._creds = { key: parsed.key, secret: parsed.secret, passphrase: parsed.passphrase };
                        _trace('ensureCredentials', 'ok', 'cached', { keyPrefix: String(parsed.key).slice(0, 8) });
                        return this._creds;
                    }
                }
            } catch (_) { }
            const tmpClob = new this._sdks.ClobClient({
                host: CLOB_HOST,
                chain: CHAIN_ID,
                signer: this._walletClient,
                signatureType: this._sdks.SignatureTypeV2.POLY_1271,
                funderAddress: addr,
            });
            const creds = await tmpClob.createOrDeriveApiKey();
            this._creds = creds;
            try {
                if (window.localStorage && creds && creds.key && creds.secret && creds.passphrase) {
                    window.localStorage.setItem(lsKey, JSON.stringify({
                        key: creds.key, secret: creds.secret, passphrase: creds.passphrase,
                    }));
                }
            } catch (_) { }
            return creds;
        }

        async getClob() {
            if (this._clob) return this._clob;
            await this._init();
            const addr = await this.getDepositWalletAddress();
            const creds = await this.ensureCredentials();
            this._clob = new this._sdks.ClobClient({
                host: CLOB_HOST,
                chain: CHAIN_ID,
                signer: this._walletClient,
                creds,
                signatureType: this._sdks.SignatureTypeV2.POLY_1271,
                funderAddress: addr,
            });
            return this._clob;
        }

        async syncBalances(tokenID) {
            const clob = await this.getClob();
            await clob.updateBalanceAllowance({ asset_type: this._sdks.AssetType.COLLATERAL });
            if (tokenID) {
                try {
                    await clob.updateBalanceAllowance({
                        asset_type: this._sdks.AssetType.CONDITIONAL,
                        token_id: tokenID,
                    });
                } catch (e) {
                }
            }
        }

        async discoverMissingApprovals() {
            await this._init();
            const addr = await this.getDepositWalletAddress();
            const lsKey = 'veyra:approvals_done:' + CHAIN_ID + ':' + String(addr || '').toLowerCase();
            try {
                const raw = window.localStorage && window.localStorage.getItem(lsKey);
                if (raw) {
                    const cached = JSON.parse(raw);
                    if (cached && cached.ts && (Date.now() - Number(cached.ts) < 30 * 24 * 3600 * 1000)) {
                        _trace('discoverMissingApprovals', 'ok', 'cached', { ts: cached.ts });
                        return [];
                    }
                }
            } catch (_) { }
            const wallet = await window.veyraGetPrivyEmbeddedWallet({
                expectedSigner: this._ownerEoa, strict: true,
            });
            const provider = await wallet.getEthereumProvider();
            const sdks = this._sdks;
            const checks = REQUIRED_APPROVALS.map(async (a) => {
                let needed = false;
                if (a.kind === 'erc20') {
                    const data = sdks.encodeFunctionData({
                        abi: ERC20_ALLOWANCE_ABI, functionName: 'allowance',
                        args: [addr, a.spender],
                    });
                    const hex = await provider.request({
                        method: 'eth_call',
                        params: [{ to: a.token, data }, 'latest']
                    });
                    const cur = (hex && hex !== '0x') ? BigInt(hex) : 0n;
                    needed = cur < (1n << 200n);
                } else {
                    const data = sdks.encodeFunctionData({
                        abi: ERC1155_IS_APPROVED_ABI, functionName: 'isApprovedForAll',
                        args: [addr, a.spender],
                    });
                    const hex = await provider.request({
                        method: 'eth_call',
                        params: [{ to: a.token, data }, 'latest']
                    });
                    needed = !(/^0x0*1$/i.test(hex || ''));
                }
                return needed ? a : null;
            });
            const results = await Promise.all(checks);
            const missing = results.filter(Boolean);
            if (!missing.length) {
                try {
                    if (window.localStorage) {
                        window.localStorage.setItem(lsKey, JSON.stringify({ ts: Date.now() }));
                    }
                } catch (_) { }
            }
            return missing;
        }

        async ensureApprovals() {
            await this._init();
            const missing = await this.discoverMissingApprovals();
            if (!missing.length) return { ok: true, applied: 0, missing: [] };

            const sdks = this._sdks;
            const calls = missing.map((a) => {
                let data;
                if (a.kind === 'erc20') {
                    data = sdks.encodeFunctionData({
                        abi: ERC20_APPROVE_ABI, functionName: 'approve',
                        args: [a.spender, MAX_UINT256],
                    });
                } else {
                    data = sdks.encodeFunctionData({
                        abi: ERC1155_SET_APPROVAL_ABI, functionName: 'setApprovalForAll',
                        args: [a.spender, true],
                    });
                }
                return { target: a.token, value: '0', data };
            });

            const addr = await this.getDepositWalletAddress();
            const deadline = Math.floor(Date.now() / 1000 + 600).toString();
            const r = await this._relayer.executeDepositWalletBatch(calls, addr, deadline);
            const confirmed = await r.wait();
            try {
                const lsKey = 'veyra:approvals_done:' + CHAIN_ID + ':' + String(addr || '').toLowerCase();
                if (window.localStorage) {
                    window.localStorage.setItem(lsKey, JSON.stringify({ ts: Date.now() }));
                }
            } catch (_) { }
            return {
                ok: true,
                applied: missing.length,
                missing: missing.map((m) => m.label),
                txHash: confirmed && (confirmed.txHash || confirmed.hash) || null,
            };
        }

        async placeLimitOrder(args) {
            const { tokenID, price, size, side, negRisk = false, tickSize = '0.01' } = args;
            if (!tokenID) throw new Error('tokenID required');
            const clob = await this.getClob();
            const sideEnum = String(side).toUpperCase() === 'SELL' ? this._sdks.Side.SELL : this._sdks.Side.BUY;
            return await clob.createAndPostOrder(
                { tokenID, price: Number(price), size: Number(size), side: sideEnum },
                { tickSize: String(tickSize), negRisk: Boolean(negRisk) },
                this._sdks.OrderType.GTC
            );
        }

        async placeMarketOrder(args) {
            const { tokenID, price, size, side, negRisk = false, tickSize = '0.01' } = args;
            if (!tokenID) throw new Error('tokenID required');
            const clob = await this.getClob();
            const sideEnum = String(side).toUpperCase() === 'SELL' ? this._sdks.Side.SELL : this._sdks.Side.BUY;
            return await clob.createAndPostOrder(
                { tokenID, price: Number(price), size: Number(size), side: sideEnum },
                { tickSize: String(tickSize), negRisk: Boolean(negRisk) },
                this._sdks.OrderType.FOK
            );
        }

        async ensureReady(tokenID) {
            try { _trace('ensureWalletDeployed', 'start'); const r = await this.ensureWalletDeployed(); _trace('ensureWalletDeployed', 'ok', '', r); }
            catch (e) { _trace('ensureWalletDeployed', 'fail', _errMsg(e)); throw e; }
            try { _trace('ensureCredentials', 'start'); await this.ensureCredentials(); _trace('ensureCredentials', 'ok'); }
            catch (e) { _trace('ensureCredentials', 'fail', _errMsg(e)); throw e; }
            let approvals;
            try { _trace('ensureApprovals', 'start'); approvals = await this.ensureApprovals(); _trace('ensureApprovals', 'ok', '', approvals); }
            catch (e) { _trace('ensureApprovals', 'fail', _errMsg(e)); throw e; }
            try { _trace('syncBalances', 'start'); await this.syncBalances(tokenID); _trace('syncBalances', 'ok'); }
            catch (e) { _trace('syncBalances', 'fail', _errMsg(e)); throw e; }
            return {
                ownerEoa: this._ownerEoa,
                depositWallet: this._depositWallet,
                approvalsApplied: approvals.applied,
                ready: true,
            };
        }

        async getMarketMeta(tokenIdOrCond, conditionIdHint) {
            var tokenID = '';
            var conditionId = '';
            if (conditionIdHint) {
                tokenID = String(tokenIdOrCond || '');
                conditionId = String(conditionIdHint || '');
            } else {
                var v = String(tokenIdOrCond || '');
                if (v.toLowerCase().startsWith('0x')) conditionId = v;
                else tokenID = v;
            }
            if (!tokenID && !conditionId) throw new Error('marketMeta: token_id or condition_id required');
            this._marketMetaCache = this._marketMetaCache || new Map();
            const key = (tokenID || '') + '|' + (conditionId || '').toLowerCase();
            if (this._marketMetaCache.has(key)) return this._marketMetaCache.get(key);
            var url = '/api/pm/market-meta?';
            var qs = [];
            if (tokenID) qs.push('token_id=' + encodeURIComponent(tokenID));
            if (conditionId) qs.push('condition_id=' + encodeURIComponent(conditionId));
            url += qs.join('&');
            const r = await fetch(url, { credentials: 'include', cache: 'no-store' });
            if (!r.ok) throw new Error('marketMeta proxy http=' + r.status);
            const data = await r.json();
            if (!data || data.ok !== true) {
                throw new Error('marketMeta resolve failed: ' + (data && data.error || 'unknown'));
            }
            const meta = {
                negRisk: Boolean(data.negRisk),
                tickSize: String(data.tickSize || '0.01'),
                minSize: Number(data.minSize || 0),
                conditionId: String(data.conditionId || conditionId || ''),
            };
            this._marketMetaCache.set(key, meta);
            return meta;
        }

        async getWalletBinding() {
            if (this._walletBinding) return this._walletBinding;
            if (typeof window === 'undefined' || window.__pmGuestMode !== false) {
                this._walletBinding = { wallet_type: '', funder_address: '', user_wallet: '', owner_eoa: '', deposit_wallet: '', raw: null };
                return this._walletBinding;
            }
            try {
                const r = await fetch('/api/pm/deposit-info', { credentials: 'include', cache: 'no-store' });
                if (r.ok) {
                    const data = await r.json();
                    this._walletBinding = {
                        wallet_type: String(data && data.wallet_type || '').toLowerCase(),
                        funder_address: String(data && data.funder_address || '').toLowerCase(),
                        user_wallet: String(data && data.user_wallet || '').toLowerCase(),
                        owner_eoa: String(data && (data.owner_eoa || data.user_wallet) || '').toLowerCase(),
                        deposit_wallet: String(data && (data.deposit_wallet || data.funder_address) || '').toLowerCase(),
                        raw: data || null,
                    };
                    return this._walletBinding;
                }
            } catch (_) {
            }
            this._walletBinding = { wallet_type: '', funder_address: '', user_wallet: '', owner_eoa: '', deposit_wallet: '', raw: null };
            return this._walletBinding;
        }

        async isDepositWalletUser() {
            const b = await this.getWalletBinding();
            return b && b.wallet_type === 'deposit_wallet';
        }

        async submitOrder(body) {
            const tokenID = String(body && body.token_id || '');
            const conditionId = String(body && body.condition_id || '');
            const side = String(body && body.side || 'BUY').toUpperCase();
            const price = Number(body && body.price);
            const size = Number(body && body.size);
            const orderType = String(body && body.order_type || 'GTC').toUpperCase();
            _trace('submitOrder', 'start', 'invoked', { tokenID: tokenID && tokenID.slice(0, 16) + '…', side, price, size, orderType });
            if (!tokenID) { _trace('submitOrder', 'fail', 'token_id required'); throw new Error('token_id required'); }
            if (!(price > 0)) { _trace('submitOrder', 'fail', 'price required ' + price); throw new Error('price required'); }
            if (!(size > 0)) { _trace('submitOrder', 'fail', 'size required ' + size); throw new Error('size required'); }
            try {
                if (this._ownerEoa && typeof window.veyraGetPrivyEmbeddedWallet === 'function') {
                    const wallet = await window.veyraGetPrivyEmbeddedWallet({
                        expectedSigner: this._ownerEoa, strict: true,
                    });
                    const provider = wallet && (await wallet.getEthereumProvider());
                    if (provider && typeof provider.request === 'function') {
                        const cur = await provider.request({ method: 'eth_chainId' });
                        if (String(cur).toLowerCase() !== '0x89') {
                            try {
                                await provider.request({
                                    method: 'wallet_switchEthereumChain',
                                    params: [{ chainId: '0x89' }],
                                });
                                _trace('submitOrder', 'info', 'chain re-pinned to polygon', { from: cur });
                            } catch (chainErr) {
                                _trace('submitOrder', 'fail', 'chain switch failed', { from: cur, err: _errMsg(chainErr) });
                                throw chainErr;
                            }
                        }
                    }
                }
            } catch (chainOuterErr) {
                _trace('submitOrder', 'fail', 'chain pin failed', { err: _errMsg(chainOuterErr) });
                throw chainOuterErr;
            }
            try { _trace('ensureReady', 'start'); await this.ensureReady(tokenID); _trace('ensureReady', 'ok'); }
            catch (e) { _trace('ensureReady', 'fail', _errMsg(e)); throw e; }
            let meta;
            try { meta = await this.getMarketMeta(tokenID, conditionId); _trace('marketMeta', 'ok', '', meta); }
            catch (e) { _trace('marketMeta', 'fail', _errMsg(e)); throw e; }
            const tickNum = Number(meta.tickSize) || 0.01;
            const priceRounded = Math.round(price / tickNum) * tickNum;
            const priceFinal = Number(priceRounded.toFixed(6));
            if (Math.abs(priceFinal - price) > 1e-9) {
                _trace('priceRound', 'info', '', { from: price, to: priceFinal, tick: meta.tickSize });
            }
            const args = { tokenID, price: priceFinal, size, side, negRisk: meta.negRisk, tickSize: meta.tickSize };
            let result;
            try {
                _trace('placeOrder', 'start', orderType, { negRisk: meta.negRisk, tickSize: meta.tickSize });
                result = (orderType === 'FOK')
                    ? await this.placeMarketOrder(args)
                    : await this.placeLimitOrder(args);
                let raw = '';
                try { raw = JSON.stringify(result); } catch (_) { raw = String(result); }
                _trace('placeOrder', 'ok', '', {
                    orderID: result && (result.orderID || result.orderId || result.order_id || result.id),
                    status: result && result.status,
                    success: result && result.success,
                    errorMsg: result && (result.errorMsg || result.error || result.message),
                    raw: raw && raw.slice(0, 1500),
                });
            } catch (e) {
                let extra = null;
                try {
                    extra = {
                        name: e && e.name,
                        status: e && e.status,
                        data: e && e.data,
                        msg: e && e.message,
                    };
                } catch (_) { }
                _trace('placeOrder', 'fail', _errMsg(e), extra || undefined);
                throw e;
            }
            const orderId = result && (result.orderID || result.orderId || result.order_id || result.id) || null;
            const sdkStatus = result && result.status;
            const sdkErr = result && (result.errorMsg || result.error || result.message);
            const httpFailed = (typeof sdkStatus === 'number') && sdkStatus >= 400;
            const successFalse = result && result.success === false;
            const noOrderId = !orderId;
            if (httpFailed || successFalse || noOrderId) {
                const err = String(sdkErr || (httpFailed ? ('HTTP ' + sdkStatus) : 'order_rejected_no_orderid'));
                let raw = '';
                try { raw = JSON.stringify(result); } catch (_) { raw = String(result); }
                _trace('submitOrder', 'fail', 'rejected: ' + err, { status: sdkStatus, raw: raw && raw.slice(0, 1500) });
                const ex = new Error(err);
                ex.sdkStatus = sdkStatus;
                ex.sdkRaw = result;
                throw ex;
            }
            const status = sdkStatus || (result && result.success ? 'matched' : 'submitted');
            _trace('submitOrder', 'ok', 'order_id=' + orderId, { status });
            return { ok: true, order_id: orderId, status, raw: result };
        }
    }

    let _singleton = null;
    function getSession() {
        if (!_singleton) _singleton = new PolymarketTradingSession();
        return _singleton;
    }

    window.veyraTrade = {
        getSession,
        PolymarketTradingSession,
        _loadSDKs: loadSDKs,
    };

    window.veyraTrade.warmup = async function warmup() {
        try {
            if (typeof window === 'undefined' || window.__pmGuestMode !== false) return;
            if (typeof window.veyraGetPrivyEmbeddedWallet !== 'function') return;
            const sess = getSession();
            await sess._init();
            try { await sess.ensureCredentials(); } catch (_) { }
            try { await sess.discoverMissingApprovals(); } catch (_) { }
        } catch (_) { }
    };
    function _whenAuthed(cb, timeoutMs) {
        try {
            if (window.__pmGuestMode === false) { cb(); return; }
            var done = false;
            var fire = function () { if (done) return; done = true; try { cb(); } catch (_) { } };
            var onEvt = function (e) { if (e && e.detail && e.detail.authed === true) fire(); };
            try { window.addEventListener('pm:auth-resolved', onEvt, { once: false }); } catch (_) { }
            setTimeout(function () {
                try { window.removeEventListener('pm:auth-resolved', onEvt); } catch (_) { }
            }, Math.max(1000, timeoutMs || 8000));
        } catch (_) { }
    }
    try {
        var _kick = function () { _whenAuthed(function () { try { window.veyraTrade.warmup(); } catch (_) { } }, 8000); };
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', _kick);
        } else {
            _kick();
        }
    } catch (_) { }
})();
