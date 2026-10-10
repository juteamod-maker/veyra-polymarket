"""Polymarket CLOB v2 canonical contract addresses (Polygon mainnet, chainId 137).

Single source of truth for the April 28, 2026 cutover. All other modules MUST
import from here instead of hard-coding addresses. Lower-cased everywhere to
keep equality checks deterministic.

Reference: py-clob-client-v2 v1.0.0 (PyPI) ContractConfig + Polymarket ops spec.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------
CHAIN_ID_POLYGON: int = 137

# ---------------------------------------------------------------------------
# Trading collateral
# ---------------------------------------------------------------------------
# pUSD (Polymarket USD) — the v2 trading collateral. The Polymarket Safe auto-
# wraps incoming USDC.e via CollateralOnramp, so user-visible "tradable balance"
# must read from pUSD.balanceOf(safe).
PUSD_TOKEN: str = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
PUSD_TOKEN_IMPL: str = "0x6bbcef9f7ef3b6c592c99e0f206a0de94ad0925f"
PUSD_DECIMALS: int = 6
PUSD_SYMBOL: str = "pUSD"

# Legacy bridged USDC.e — still recognised when present in a Safe so that
# auto-wrap-pending balances are surfaced to the user. NOT the trading token.
USDC_E_TOKEN: str = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
USDC_E_DECIMALS: int = 6

# Native USDC (Circle) — accepted by Polymarket Deposit Wallets and auto-routed
# into pUSD by the CollateralOnramp on the user's first dispatch. Also surfaced
# in the deposit-page hint ("send USDC.e, native USDC, or pUSD").
#
# T882-bugfix (2026-05-06): the previous value `0x3c499c542cef5e3811e1192ce70dbcc03d35c359`
# was a typo (no contract deployed at that address on Polygon). The canonical
# Circle native USDC contract is `0x3c499c542cef5e3811e1192ce70d8cc03d5c3359`,
# which is what every wallet/SDK in the wild — including Privy's MFA bundle
# shipped with this app — uses. The wrong value caused all balance reads via
# `discover_routes._fetch_usdc_balances` and the deposit-page hint payload to
# silently return 0 for any user who funded their deposit wallet with native
# USDC instead of USDC.e or pUSD.
USDC_NATIVE_TOKEN: str = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
USDC_NATIVE_DECIMALS: int = 6

# ---------------------------------------------------------------------------
# Exchange contracts (v2)
# ---------------------------------------------------------------------------
CTF_EXCHANGE_V2: str = "0xe111180000d2663c0091e4f400237545b87b996b"
NEG_RISK_EXCHANGE_V2: str = "0xe2222d279d744050d28e00520010520000310f59"

# Legacy v1 (kept for signature_type metadata + compat):
CTF_EXCHANGE_V1: str = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
NEG_RISK_EXCHANGE_V1: str = "0xc5d563a36ae78145c45a50134d48a1215220f80a"

# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------
CTF_COLLATERAL_ADAPTER: str = "0xada100874d00e3331d00f2007a9c336a65009718"
NEG_RISK_CTF_COLLATERAL_ADAPTER: str = "0xada200001000ef00d07553cee7006808f895c6f1"
NEG_RISK_ADAPTER: str = "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296"

# ---------------------------------------------------------------------------
# Onramp / Offramp / PermissionedRamp (USDC.e <-> pUSD bridges)
# ---------------------------------------------------------------------------
COLLATERAL_ONRAMP: str = "0x93070a847efef7f70739046a929d47a521f5b8ee"
COLLATERAL_OFFRAMP: str = "0x2957922eb93258b93368531d39facca3b4dc5854"
PERMISSIONED_RAMP: str = "0xebc2459ec962869ca4c0bd1e06368272732bcb08"

# ---------------------------------------------------------------------------
# CTF + UMA
# ---------------------------------------------------------------------------
CTF: str = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
UMA_ADAPTER: str = "0x6a9d222616c90fca5754cd1333cfd9b7fb6a4f74"
UMA_ORACLE: str = "0xcb1822859cef82cd2eb4e6276c7916e692995130"

# ---------------------------------------------------------------------------
# Safe / Proxy factories — NEVER overwrite an upstream-registered Safe.
# ---------------------------------------------------------------------------
SAFE_FACTORY: str = "0xaacfeea03eb1561c4e67d661e40682bd20e3541b"
SAFE_INIT_CODE_HASH: str = "0x2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf"
POLYMARKET_PROXY_FACTORY: str = "0xab45c5a4b0c941a2f231c04c3f49182e1a254052"

# ---------------------------------------------------------------------------
# Deposit Wallet factory — new wallet path for new API users (May 2026 rollout).
# Reference: https://docs.polymarket.com/trading/deposit-wallet-migration
# Polygon mainnet (chainId 137). Existing proxy/Safe users are NOT affected.
# ---------------------------------------------------------------------------
DEPOSIT_WALLET_FACTORY: str = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
DEPOSIT_WALLET_IMPLEMENTATION: str = "0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB"

# Signature type constants
SIGNATURE_TYPE_SAFE: int = 2        # Proxy / Gnosis Safe (existing users)
SIGNATURE_TYPE_POLY_1271: int = 3   # Deposit wallet / POLY_1271 (new API users)

# ---------------------------------------------------------------------------
# Toggles
# ---------------------------------------------------------------------------

def use_v2() -> bool:
    """Master toggle for the v2 cutover (default ON post-2026-04-28).

    Set ``PM_USE_V2=0`` to revert to the legacy v1 path during incident response.
    """

    raw = str(os.environ.get("PM_USE_V2", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def privy_authorization_key() -> str:
    """Return the active Privy authorization key.

    During the cutover window operators may stage the new v2 whitelist key in
    ``PRIVY_AUTHORIZATION_KEY_V2`` and flip ``PM_USE_V2=1`` to activate it
    without disturbing the legacy variable.
    """

    if use_v2():
        v2 = str(os.environ.get("PRIVY_AUTHORIZATION_KEY_V2") or "").strip()
        if v2:
            return v2
    return str(os.environ.get("PRIVY_AUTHORIZATION_KEY") or "").strip()


def trading_collateral_token() -> str:
    """Return the address of the trading collateral token (lowercased)."""

    return PUSD_TOKEN if use_v2() else USDC_E_TOKEN


def trading_collateral_symbol() -> str:
    return PUSD_SYMBOL if use_v2() else "USDC.e"


__all__ = [
    "CHAIN_ID_POLYGON",
    "PUSD_TOKEN",
    "PUSD_TOKEN_IMPL",
    "PUSD_DECIMALS",
    "PUSD_SYMBOL",
    "USDC_E_TOKEN",
    "USDC_E_DECIMALS",
    "USDC_NATIVE_TOKEN",
    "USDC_NATIVE_DECIMALS",
    "CTF_EXCHANGE_V2",
    "NEG_RISK_EXCHANGE_V2",
    "CTF_EXCHANGE_V1",
    "NEG_RISK_EXCHANGE_V1",
    "CTF_COLLATERAL_ADAPTER",
    "NEG_RISK_CTF_COLLATERAL_ADAPTER",
    "NEG_RISK_ADAPTER",
    "COLLATERAL_ONRAMP",
    "COLLATERAL_OFFRAMP",
    "PERMISSIONED_RAMP",
    "CTF",
    "UMA_ADAPTER",
    "UMA_ORACLE",
    "SAFE_FACTORY",
    "SAFE_INIT_CODE_HASH",
    "POLYMARKET_PROXY_FACTORY",
    "DEPOSIT_WALLET_FACTORY",
    "DEPOSIT_WALLET_IMPLEMENTATION",
    "SIGNATURE_TYPE_SAFE",
    "SIGNATURE_TYPE_POLY_1271",
    "use_v2",
    "privy_authorization_key",
    "trading_collateral_token",
    "trading_collateral_symbol",
]
