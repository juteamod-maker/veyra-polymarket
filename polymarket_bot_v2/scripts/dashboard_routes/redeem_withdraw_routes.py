"""
redeem_withdraw_routes.py — T460: 赎回持仓 + 钱包提现 + Vault 历史

端点:
  POST /api/pm/redeem             赎回已结算的获胜持仓（CTF redeemPositions）
  POST /api/pm/safe-withdraw      从平台钱包提现 USDC 到外部地址
  GET  /api/pm/vault/history      查询 Vault 历史记录（充值 / 提现 / 赎回）
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import struct
import threading
import time
import asyncio  # T590 wave 4
from datetime import datetime, timezone
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature as _decode_dss_signature,
)
from eth_utils import to_checksum_address, keccak as _keccak256
from eth_abi import encode as _abi_encode
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from scripts.dashboard_routes._shared import (
    _ensure_user_tables,
    db_connect,
    get_request_identity,
)
from src.utils.config_loader import get as cfg_get

router = APIRouter()
logger = logging.getLogger("redeem_withdraw_routes")
_PRIVY_USER_AUTH_KEY_CACHE: dict[str, dict[str, Any]] = {}
_PRIVY_USER_AUTH_KEY_CACHE_LOCK = threading.Lock()
_PRIVY_USER_JWT_CACHE: dict[str, dict[str, Any]] = {}
_PRIVY_USER_JWT_CACHE_LOCK = threading.Lock()


class GasBalanceError(RuntimeError):
    """Raised when sender POL balance cannot cover transaction gas."""

    def __init__(
        self,
        *,
        sender: str,
        balance_wei: int,
        required_wei: int,
        gas_price_wei: int,
        gas_limit: int,
    ) -> None:
        self.sender = sender
        self.balance_wei = int(balance_wei)
        self.required_wei = int(required_wei)
        self.gas_price_wei = int(gas_price_wei)
        self.gas_limit = int(gas_limit)
        super().__init__(
            f"Insufficient POL balance for gas: balance={self.balance_wei} required={self.required_wei}"
        )


# ── Polygon 链常量 ──────────────────────────────────────────────────────
_CHAIN_ID = 137  # Polygon mainnet
_DEFAULT_POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://polygon-mainnet.public.blastapi.io",
]

# Polymarket CTF（Gnosis Conditional Tokens Framework）合约地址
_CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# T471: Polymarket CLOB v2 cutover (2026-04-28). Trading collateral migrated
# from USDC.e (bridged) to pUSD (Polymarket USD). The Safe auto-wraps any
# incoming USDC.e via CollateralOnramp, and CTF redeem now pays out pUSD.
# We keep _USDC_E_TOKEN and _USDC_NATIVE_TOKEN as constants for legacy reads
# (deposit-info pending-wrap balance) but never use them as the redeem/withdraw
# default token.
from src.utils import pm_v2_constants as _pm_v2  # noqa: E402

_USDC_NATIVE_TOKEN = _pm_v2.USDC_NATIVE_TOKEN
_USDC_E_TOKEN = _pm_v2.USDC_E_TOKEN
_PUSD_TOKEN = _pm_v2.PUSD_TOKEN
_COLLATERAL_OFFRAMP = _pm_v2.COLLATERAL_OFFRAMP
_COLLATERAL_ONRAMP = _pm_v2.COLLATERAL_ONRAMP


# Withdraw token catalog. After the v2 cutover, "pusd" is the canonical key.
# Legacy aliases (usdc_e / usdce / usdc) are kept so that older browser
# sessions continue to work; they are silently routed to the active trading
# collateral via the PM_USE_V2 toggle.
def _active_collateral() -> tuple[str, str]:
    """Return (contract_address_checksum_lower, label) for the active token."""
    if _pm_v2.use_v2():
        return _PUSD_TOKEN, _pm_v2.PUSD_SYMBOL
    return _USDC_E_TOKEN, "USDC.e"


_WITHDRAW_TOKEN_CATALOG: dict[str, dict[str, str]] = {
    "pusd": {"contract": _PUSD_TOKEN, "label": _pm_v2.PUSD_SYMBOL, "decimals": "6"},
    "usdc_e": {"contract": _USDC_E_TOKEN, "label": "USDC.e", "decimals": "6"},
    "usdce": {"contract": _USDC_E_TOKEN, "label": "USDC.e", "decimals": "6"},
    "usdc.e": {"contract": _USDC_E_TOKEN, "label": "USDC.e", "decimals": "6"},
    "bridged": {"contract": _USDC_E_TOKEN, "label": "USDC.e", "decimals": "6"},
    # "usdc" / "native": route to active collateral (pUSD post-cutover).
    "usdc": {"contract": _PUSD_TOKEN, "label": _pm_v2.PUSD_SYMBOL, "decimals": "6"},
    "usdc_native": {"contract": _USDC_NATIVE_TOKEN, "label": "USDC", "decimals": "6"},
    "native": {"contract": _USDC_NATIVE_TOKEN, "label": "USDC", "decimals": "6"},
    "usdc-native": {"contract": _USDC_NATIVE_TOKEN, "label": "USDC", "decimals": "6"},
}


def _resolve_withdraw_token(token_key: str) -> tuple[str, str] | None:
    key = str(token_key or "usdc_e").strip().lower()
    cfg = _WITHDRAW_TOKEN_CATALOG.get(key)
    if not cfg:
        return None
    return _as_checksum_address(cfg["contract"]), cfg["label"]


# Gamma API
_GAMMA_API = "https://gamma-api.polymarket.com"
_RELAYER_API = "https://relayer-v2.polymarket.com"
_BRIDGE_API = "https://bridge.polymarket.com"
_ZERO_ADDR = "0x0000000000000000000000000000000000000000"

# T918-C: relayer SAFE/WALLET nonce in-process cache.
import time as _nonce_time

_NONCE_CACHE: dict[tuple[str, str], tuple[float, str]] = {}
_NONCE_CACHE_LOCK = threading.Lock()
_NONCE_CACHE_TTL = 10.0

# T884-BRIDGE: Polymarket Bridge API gives every wallet a deterministic
# multi-chain "intake" address. Any USDC/USDT/etc. sent to the EVM intake
# address is automatically bridged + swapped to pUSD and credited to the
# Polymarket wallet. This is the canonical Builder-side flow per
# https://docs.polymarket.com/trading/bridge/deposit and replaces the
# now-invalid CollateralOnramp.wrap() path which only accepts USDC.e.
_BRIDGE_ADDR_CACHE: dict[str, dict[str, Any]] = {}
_BRIDGE_ADDR_CACHE_LOCK = threading.Lock()
_BRIDGE_ADDR_CACHE_TTL = 24 * 3600  # deterministic per wallet, but refresh daily
_ERC20_TRANSFER_TOPIC0 = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4d"
    "f523b3ef"
)
_BRIDGE_ACTIVITY_TOKENS: list[dict[str, Any]] = [
    {"address": _USDC_E_TOKEN, "symbol": "USDC.e", "decimals": 6},
    {"address": _USDC_NATIVE_TOKEN, "symbol": "USDC", "decimals": 6},
    # Legacy Polygon bridged USDC (used by many wallets/bridges).
    {"address": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174", "symbol": "USDC.e", "decimals": 6},
    # Common Polygon Bridge sources
    {"address": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f", "symbol": "USDT", "decimals": 6},
    {"address": "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063", "symbol": "DAI", "decimals": 18},
]
_FIX_NATIVE_USDC_ALLOWED_EOA = "0x62afb2e2e2a7f667729f424fc8c00be6da10e3a3"
_FIX_NATIVE_USDC_BRIDGE_INTAKE = "0x4E559D79A8BB3b131E2311d8899387Ad8a556e18"
_PRIVY_WALLETS_AUTH_URLS = [
    # Match SDK runtime host first.
    "https://auth.privy.io/api/v1/wallets/authenticate",
    # Keep legacy/fallback host for compatibility.
    "https://api.privy.io/v1/wallets/authenticate",
]


def _build_wallets_auth_variants(user_jwt: str) -> list[dict[str, Any]]:
    token = str(user_jwt or "").strip()
    base_headers = {"Content-Type": "application/json"}
    return [
        {
            "name": "body_user_jwt",
            "headers": dict(base_headers),
            "json": {"user_jwt": token},
        },
        {
            "name": "body_access_token",
            "headers": dict(base_headers),
            "json": {"access_token": token},
        },
        {
            "name": "body_identity_token",
            "headers": dict(base_headers),
            "json": {"identity_token": token},
        },
        {
            "name": "body_id_token",
            "headers": dict(base_headers),
            "json": {"id_token": token},
        },
        {
            "name": "bearer_only",
            "headers": {**base_headers, "Authorization": f"Bearer {token}"},
            "json": {},
        },
        {
            "name": "bearer_with_user_jwt",
            "headers": {**base_headers, "Authorization": f"Bearer {token}"},
            "json": {"user_jwt": token},
        },
    ]


def _call_wallets_auth_with_variants(
    *,
    app_id: str,
    app_secret: str,
    user_jwt: str,
    auth_url: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for variant in _build_wallets_auth_variants(user_jwt):
        headers = {"privy-app-id": app_id, **dict(variant.get("headers") or {})}
        payload = dict(variant.get("json") or {})
        try:
            resp = requests.post(
                auth_url,
                headers=headers,
                auth=(app_id, app_secret),
                json=payload,
                timeout=15,
            )
            body = (
                resp.json()
                if "application/json"
                in str(resp.headers.get("Content-Type") or "").lower()
                else {"raw": resp.text[:400]}
            )
            rows.append(
                {
                    "variant": str(variant.get("name") or ""),
                    "status": int(resp.status_code),
                    "body": body,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "variant": str(variant.get("name") or ""),
                    "status": -1,
                    "error": str(exc),
                }
            )
    return rows


def _probe_privy_basic_auth(
    *,
    app_id: str,
    app_secret: str,
    privy_did: str,
) -> list[dict[str, Any]]:
    did = str(privy_did or "").strip()
    if not did:
        return []
    urls = [
        f"https://auth.privy.io/api/v1/users/{did}",
        f"https://api.privy.io/v1/users/{did}",
    ]
    out: list[dict[str, Any]] = []
    for url in urls:
        try:
            resp = requests.get(
                url,
                auth=(app_id, app_secret),
                headers={"privy-app-id": app_id},
                timeout=15,
            )
            body = (
                resp.json()
                if "application/json"
                in str(resp.headers.get("Content-Type") or "").lower()
                else {"raw": resp.text[:400]}
            )
            out.append(
                {
                    "url": url,
                    "status": int(resp.status_code),
                    "body": body,
                }
            )
        except Exception as exc:
            out.append(
                {
                    "url": url,
                    "status": -1,
                    "error": str(exc),
                }
            )
    return out


# 地址正则
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# vault_transactions 表 DDL（在 DB_PATH 对应的 SQLite DB 中）
_VAULT_DDL = """
CREATE TABLE IF NOT EXISTS vault_transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL DEFAULT 'system',
    tx_type     TEXT    NOT NULL,
    tx_hash     TEXT,
    from_addr   TEXT,
    to_addr     TEXT,
    token       TEXT,
    amount      REAL    NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'pending',
    extra_json  TEXT,
    created_at  INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vault_tx_user ON vault_transactions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vault_tx_type ON vault_transactions(tx_type);
"""


# ── 工具函数 ────────────────────────────────────────────────────────────


def _ensure_vault_table() -> None:
    """确保 vault_transactions 表存在。"""
    try:
        with db_connect() as conn:
            for stmt in _VAULT_DDL.strip().split(";"):
                s = stmt.strip()
                if s:
                    conn.execute(s)
            conn.commit()
    except Exception as exc:
        logger.warning("vault table ensure failed: %s", exc)


def _get_secrets() -> tuple[str, str, str]:
    """Return (private_key, relayer_address, key_name_used)."""
    try:
        from src.secrets import get_secret  # noqa: PLC0415

        key_candidates = (
            "POLY_TX_PRIVATE_KEY",
            "POLY_WITHDRAW_PRIVATE_KEY",
            "POLY_PRIVATE_KEY",
        )
        pk = ""
        used = ""
        for key_name in key_candidates:
            candidate = get_secret(key_name) or ""
            if candidate:
                pk = candidate
                used = key_name
                break
        relayer = get_secret("POLY_RELAYER_ADDRESS") or ""
    except Exception:
        key_candidates = (
            "POLY_TX_PRIVATE_KEY",
            "POLY_WITHDRAW_PRIVATE_KEY",
            "POLY_PRIVATE_KEY",
        )
        pk = ""
        used = ""
        for key_name in key_candidates:
            candidate = os.environ.get(key_name, "")
            if candidate:
                pk = candidate
                used = key_name
                break
        relayer = os.environ.get("POLY_RELAYER_ADDRESS", "")
    if not pk:
        raise EnvironmentError(
            "No signing key configured. Set POLY_TX_PRIVATE_KEY (preferred) or POLY_PRIVATE_KEY"
        )
    return pk.strip(), relayer.strip(), used


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _official_tx_mode() -> bool:
    # Default to official API mode. Legacy direct-chain path requires explicit opt-in.
    return not _truthy_env("PM_ALLOW_LEGACY_DIRECT_TX", default=False)


def _get_user_funder_address(login_wallet: str) -> str:
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT funder_address, polymarket_wallet FROM users WHERE wallet_address = %s LIMIT 1",
                (login_wallet,),
            ).fetchone()
            if row:
                data = dict(row)
                funder = str(data.get("funder_address") or "").strip()
                if funder and _ADDR_RE.match(funder):
                    return _as_checksum_address(funder)
                pm_wallet = str(data.get("polymarket_wallet") or "").strip()
                if pm_wallet and _ADDR_RE.match(pm_wallet):
                    return _as_checksum_address(pm_wallet)
    except Exception as exc:
        logger.warning("resolve funder_address failed for %s: %s", login_wallet, exc)
    return ""


def _load_privy_app_credentials() -> tuple[str, list[str]]:
    app_id = str(os.environ.get("PRIVY_APP_ID") or "").strip().strip('"').strip("'")
    secret_keys = [
        "PRIVY_APP_SECRET",
        "PRIVY_APP_SECRET_ALT",
        "PRIVY_APP_SECRET_BACKUP",
        "PRIVY_APP_SECRET_SECONDARY",
        "PRIVY_APP_SECRET_1",
        "PRIVY_APP_SECRET_2",
        "PRIVY_APP_SECRET_3",
    ]
    app_secrets: list[str] = []
    combined = (
        str(os.environ.get("PRIVY_APP_SECRETS") or "").strip().strip('"').strip("'")
    )
    if combined:
        for part in combined.split(","):
            candidate = str(part or "").strip().strip('"').strip("'")
            if candidate and candidate not in app_secrets:
                app_secrets.append(candidate)
    for key_name in secret_keys:
        raw_value = str(os.environ.get(key_name) or "").strip().strip('"').strip("'")
        if not raw_value:
            continue
        if "," in raw_value:
            for part in raw_value.split(","):
                candidate = str(part or "").strip().strip('"').strip("'")
                if candidate and candidate not in app_secrets:
                    app_secrets.append(candidate)
            continue
        if raw_value and raw_value not in app_secrets:
            app_secrets.append(raw_value)
    if not app_id or not app_secrets:
        raise EnvironmentError("Privy app credentials are not configured")
    return app_id, app_secrets


def _normalize_privy_authorization_key(raw: str) -> str:
    value = str(raw or "").strip().strip('"').strip("'")
    if not value:
        return ""
    if value.startswith("wallet-auth:"):
        # Keep the wallet-auth prefix intact; signature builder parses this format explicitly.
        return value
    # Support envs that store PEM with escaped newlines.
    value = value.replace("\\n", "\n")
    if "BEGIN PRIVATE KEY" in value:
        return value

    # Some deployments store PKCS8 private keys as base64 DER (without PEM wrapper).
    # Try to decode and normalize to PEM before falling back to plain wrapping.
    try:
        compact = "".join(value.split())
        if compact:
            pad = "=" * (-len(compact) % 4)
            decoded = base64.b64decode(compact + pad)
            try:
                key_obj = serialization.load_der_private_key(decoded, password=None)
            except Exception:
                # Privy wallet-auth keys can be raw 32-byte scalars for secp256r1.
                if len(decoded) == 32:
                    key_int = int.from_bytes(decoded, byteorder="big")
                    key_obj = ec.derive_private_key(key_int, ec.SECP256R1())
                else:
                    raise
            pem_bytes = key_obj.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            pem_value = pem_bytes.decode("utf-8").strip()
            if "BEGIN PRIVATE KEY" in pem_value:
                return pem_value
    except Exception:
        pass

    return f"-----BEGIN PRIVATE KEY-----\n{value}\n-----END PRIVATE KEY-----"


def _load_privy_authorization_key_pems() -> list[str]:
    # T479: 优先使用 PRIVY_AUTHORIZATION_KEY_V2 (T471 V2 cutover)；旧 KEY/PRIVATE_KEY 系列保留
    # 仅作为本地开发回退，生产已全面下线。
    key_env_names = [
        "PRIVY_AUTHORIZATION_KEY_V2",
        "PRIVY_AUTHORIZATION_KEY",
        "PRIVY_AUTHORIZATION_KEY_1",
        "PRIVY_AUTHORIZATION_KEY_2",
        "PRIVY_AUTHORIZATION_KEY_3",
        "PRIVY_AUTHORIZATION_PRIVATE_KEY",
        "PRIVY_AUTHORIZATION_PRIVATE_KEY_1",
        "PRIVY_AUTHORIZATION_PRIVATE_KEY_2",
        "PRIVY_AUTHORIZATION_PRIVATE_KEY_3",
        "PRIVY_SERVER_AUTHORIZATION_KEY",
        "PRIVY_SERVER_AUTHORIZATION_KEY_1",
        "PRIVY_SERVER_AUTHORIZATION_KEY_2",
        "PRIVY_SERVER_AUTHORIZATION_KEY_3",
    ]
    keys: list[str] = []
    for env_name in key_env_names:
        raw = str(os.environ.get(env_name) or "").strip()
        if not raw:
            continue
        parts = (
            [raw]
            if ("BEGIN PRIVATE KEY" in raw or raw.startswith("wallet-auth:"))
            else raw.split(",")
        )
        for part in parts:
            normalized = _normalize_privy_authorization_key(part)
            if normalized and normalized not in keys:
                keys.append(normalized)
    if not keys:
        raise EnvironmentError("Privy authorization key is not configured")
    try:
        # T901 安全修复：绝不把授权私钥内容（含前缀）写入日志。
        # 仅记录不可逆指纹与长度，便于排障而不泄露密钥材料。
        fingerprint = hashlib.sha256(keys[0].encode("utf-8")).hexdigest()[:12]
        logger.info(
            "Privy authorization key loaded fingerprint=%s length=%s",
            fingerprint,
            len(keys[0]),
        )
        if not str(keys[0]).startswith("wallet-auth:"):
            logger.warning(
                "Authorization Private Key format may be invalid: expected wallet-auth: prefix"
            )
    except Exception:
        pass
    return keys


def _extract_request_user_jwt(request: Request | None) -> str:
    tokens = _extract_request_user_jwts(request)
    return tokens[0] if tokens else ""


def _extract_request_user_jwts(request: Request | None) -> list[str]:
    if request is None:
        return []
    out: list[str] = []

    def _push(raw: str) -> None:
        token = str(raw or "").strip().strip('"').strip("'")
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token or token.lower() in {"null", "none", "undefined"}:
            return
        if token not in out:
            out.append(token)

    auth = str(request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        _push(auth[7:].strip())
    # Prefer explicit per-request token sources before cookies, mirroring /api/user/privy/verify.
    for key in (
        "privy-token",
        "privy_access_token",
        "privy-id-token",
        "privy_id_token",
    ):
        _push(str(request.headers.get(key) or ""))
    for key in (
        "privy-token",
        "privy_access_token",
        "privy-id-token",
        "privy_id_token",
    ):
        _push(str(request.cookies.get(key) or ""))
    return out


def _extract_request_privy_token_bundle(request: Request | None) -> dict[str, str]:
    if request is None:
        return {"access_token": "", "identity_token": ""}

    auth = str(request.headers.get("authorization") or "").strip()
    access_token = ""
    if auth.lower().startswith("bearer "):
        access_token = auth[7:].strip()

    if not access_token:
        for key in ("privy-token", "privy_access_token"):
            access_token = (
                str(request.headers.get(key) or "").strip().strip('"').strip("'")
            )
            if access_token:
                break
    if not access_token:
        for key in ("privy-token", "privy_access_token"):
            access_token = (
                str(request.cookies.get(key) or "").strip().strip('"').strip("'")
            )
            if access_token:
                break

    identity_token = ""
    for key in ("privy-id-token", "privy_id_token"):
        identity_token = (
            str(request.headers.get(key) or "").strip().strip('"').strip("'")
        )
        if identity_token:
            break
    if not identity_token:
        for key in ("privy-id-token", "privy_id_token"):
            identity_token = (
                str(request.cookies.get(key) or "").strip().strip('"').strip("'")
            )
            if identity_token:
                break

    return {
        "access_token": access_token,
        "identity_token": identity_token,
    }


def _jwt_diag_brief(token: str) -> dict[str, Any]:
    raw = str(token or "").strip().strip('"').strip("'")
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        return {"present": False}
    parts = raw.split(".")
    if len(parts) != 3:
        return {"present": True, "kind": "non_jwt", "len": len(raw)}
    now_ts = int(time.time())
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "==").decode("utf-8"))
    except Exception:
        return {"present": True, "kind": "jwt_parse_error", "len": len(raw)}
    aud = payload.get("aud")
    if isinstance(aud, list):
        aud = ",".join([str(x) for x in aud[:3]])
    exp = payload.get("exp")
    exp_in_sec = None
    if isinstance(exp, (int, float)):
        exp_in_sec = int(exp) - now_ts
    elif isinstance(exp, str) and exp.isdigit():
        exp_in_sec = int(exp) - now_ts
    return {
        "present": True,
        "kind": "jwt",
        "len": len(raw),
        "iss": str(payload.get("iss") or "")[:120],
        "aud": str(aud or "")[:200],
        "sub": str(payload.get("sub") or "")[:120],
        "exp_in_sec": exp_in_sec,
    }


def _cache_verified_user_jwts(
    wallet_address: str, jwts: list[str], ttl_seconds: int = 1800
) -> None:
    wallet = str(wallet_address or "").strip().lower()
    if not _ADDR_RE.match(wallet):
        return
    normalized: list[str] = []
    for raw in list(jwts or []):
        token = str(raw or "").strip().strip('"').strip("'")
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token or token.lower() in {"null", "none", "undefined"}:
            continue
        if token not in normalized:
            normalized.append(token)
    if not normalized:
        return
    expires_at = int(time.time()) + max(60, int(ttl_seconds or 0))
    with _PRIVY_USER_JWT_CACHE_LOCK:
        _PRIVY_USER_JWT_CACHE[wallet] = {
            "tokens": normalized,
            "expires_at": expires_at,
        }


def _get_cached_user_jwts(wallet_address: str) -> list[str]:
    wallet = str(wallet_address or "").strip().lower()
    if not _ADDR_RE.match(wallet):
        return []
    now_ts = int(time.time())
    with _PRIVY_USER_JWT_CACHE_LOCK:
        cached = _PRIVY_USER_JWT_CACHE.get(wallet)
        if not cached:
            return []
        if int(cached.get("expires_at") or 0) <= now_ts:
            _PRIVY_USER_JWT_CACHE.pop(wallet, None)
            return []
        tokens = cached.get("tokens") if isinstance(cached.get("tokens"), list) else []
        return [str(x) for x in tokens if str(x).strip()]


def _load_privy_user_authorization_keys(
    user_jwt: str | list[str] | tuple[str, ...],
    *,
    token_bundle: dict[str, str] | None = None,
) -> list[str]:
    raw_tokens: list[str]
    if isinstance(user_jwt, (list, tuple, set)):
        raw_tokens = [str(x or "") for x in user_jwt]
    else:
        raw_tokens = [str(user_jwt or "")]

    tokens: list[str] = []
    for raw in raw_tokens:
        token = str(raw or "").strip().strip('"').strip("'")
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token or token.lower() in {"null", "none", "undefined"}:
            continue
        if token not in tokens:
            tokens.append(token)

    if not tokens:
        return []

    # Create stable fingerprints without leaking token content.
    import hashlib as _hashlib  # noqa: PLC0415

    now_ts = int(time.time())

    app_id, app_secrets = _load_privy_app_credentials()

    # New-style probe: submit access_token + identity_token together.
    bundle_access = str((token_bundle or {}).get("access_token") or "").strip()
    bundle_identity = str((token_bundle or {}).get("identity_token") or "").strip()
    if bundle_access and bundle_identity:
        for secret_idx, app_secret in enumerate(app_secrets, start=1):
            for auth_url in _PRIVY_WALLETS_AUTH_URLS:
                combo_variants = [
                    {
                        "name": "combo_body_access_identity",
                        "headers": {
                            "privy-app-id": app_id,
                            "Content-Type": "application/json",
                        },
                        "json": {
                            "access_token": bundle_access,
                            "identity_token": bundle_identity,
                        },
                    },
                    {
                        "name": "combo_body_access_id_token",
                        "headers": {
                            "privy-app-id": app_id,
                            "Content-Type": "application/json",
                        },
                        "json": {
                            "access_token": bundle_access,
                            "id_token": bundle_identity,
                        },
                    },
                    {
                        "name": "combo_bearer_access_body_identity",
                        "headers": {
                            "privy-app-id": app_id,
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {bundle_access}",
                        },
                        "json": {"identity_token": bundle_identity},
                    },
                    {
                        "name": "combo_bearer_access_body_id_token",
                        "headers": {
                            "privy-app-id": app_id,
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {bundle_access}",
                        },
                        "json": {"id_token": bundle_identity},
                    },
                ]
                for variant in combo_variants:
                    try:
                        resp = requests.post(
                            auth_url,
                            headers=variant["headers"],
                            auth=(app_id, app_secret),
                            json=variant["json"],
                            timeout=15,
                        )
                        body = (
                            resp.json()
                            if "application/json"
                            in str(resp.headers.get("Content-Type") or "").lower()
                            else {"raw": resp.text[:400]}
                        )
                        if int(resp.status_code) >= 400:
                            last_error = (
                                f"host={auth_url} variant={variant['name']} secret_idx={secret_idx} "
                                f"status={resp.status_code} body={body}"
                            )
                            continue
                        auth_key = (
                            str((body or {}).get("authorization_key") or "")
                            .strip()
                            .strip('"')
                            .strip("'")
                        )
                        if auth_key:
                            return [auth_key]
                    except Exception as exc:
                        last_error = (
                            f"host={auth_url} variant={variant['name']} err={exc}"
                        )

    last_error = ""
    for token_idx, token in enumerate(tokens, start=1):
        token_fingerprint = _hashlib.sha256(token.encode("utf-8")).hexdigest()
        with _PRIVY_USER_AUTH_KEY_CACHE_LOCK:
            cached = _PRIVY_USER_AUTH_KEY_CACHE.get(token_fingerprint)
            if cached and int(cached.get("expires_at") or 0) > now_ts + 30:
                keys = (
                    cached.get("keys") if isinstance(cached.get("keys"), list) else []
                )
                cached_keys = [str(k) for k in keys if str(k).strip()]
                if cached_keys:
                    return cached_keys

        for secret_idx, app_secret in enumerate(app_secrets, start=1):
            for auth_url in _PRIVY_WALLETS_AUTH_URLS:
                variant_rows = _call_wallets_auth_with_variants(
                    app_id=app_id,
                    app_secret=app_secret,
                    user_jwt=token,
                    auth_url=auth_url,
                )
                for row in variant_rows:
                    status = int(row.get("status") or -1)
                    body = row.get("body") if isinstance(row.get("body"), dict) else {}
                    variant = str(row.get("variant") or "")
                    if status >= 400:
                        logger.warning(
                            "wallets/authenticate failed token_idx=%s secret_idx=%s host=%s variant=%s status=%s",
                            token_idx,
                            secret_idx,
                            auth_url,
                            variant,
                            status,
                        )
                        last_error = f"host={auth_url} variant={variant} status={status} body={body}"
                        continue
                    auth_key = str((body or {}).get("authorization_key") or "").strip()
                    if not auth_key:
                        logger.warning(
                            "wallets/authenticate missing authorization_key token_idx=%s secret_idx=%s host=%s variant=%s",
                            token_idx,
                            secret_idx,
                            auth_url,
                            variant,
                        )
                        last_error = f"host={auth_url} variant={variant} missing authorization_key body={body}"
                        continue
                    normalized = str(auth_key).strip().strip('"').strip("'")
                    if not normalized:
                        last_error = f"host={auth_url} variant={variant} authorization_key normalization failed"
                        continue
                    expires_at = int((body or {}).get("expires_at") or 0)
                    if expires_at <= 0:
                        expires_at = now_ts + 600
                    with _PRIVY_USER_AUTH_KEY_CACHE_LOCK:
                        _PRIVY_USER_AUTH_KEY_CACHE[token_fingerprint] = {
                            "keys": [normalized],
                            "expires_at": expires_at,
                        }
                    return [normalized]

    if last_error:
        logger.warning(
            "privy user authorization-key fallback unavailable: %s", last_error
        )
    return []


def _canonicalize_json(value: Any) -> str:
    # RFC 8785 canonical JSON: keys sorted alphabetically (Unicode code point order).
    # This matches the official @privy-io/node SDK which uses the 'canonicalize' npm package.
    # sort_keys=True produces the same order as RFC 8785 for ASCII keys.
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _build_privy_authorization_signature(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    key_pem: str,
    method: str = "POST",
) -> str:
    # Match official @privy-io/node SDK authorization payload format.
    # The signable input is wrapped as {version, method, url, body, headers}.
    body_for_sig: Any = body
    if body_for_sig is None or (
        isinstance(body_for_sig, dict) and len(body_for_sig) == 0
    ):
        body_for_sig = ""
    signed_headers: dict[str, str] = {
        "privy-app-id": str(headers.get("privy-app-id") or ""),
    }
    request_expiry_raw = str(headers.get("privy-request-expiry") or "").strip()
    if request_expiry_raw:
        signed_headers["privy-request-expiry"] = request_expiry_raw
    payload: dict[str, Any] = {
        "version": 1,
        "method": str(method or "POST").upper(),
        "url": str(url or ""),
        "body": body_for_sig,
        "headers": signed_headers,
    }
    canonical = _canonicalize_json(payload)
    payload_bytes = canonical.encode("utf-8")
    key_value = str(key_pem or "").strip().strip('"').strip("'")
    if not key_value:
        raise ValueError("empty Privy authorization key")

    private_key = None
    if key_value.startswith("wallet-auth:"):
        compact = "".join(key_value.split(":", 1)[1].split())
        pad = "=" * (-len(compact) % 4)
        decoded = base64.b64decode(compact + pad)
        try:
            private_key = serialization.load_der_private_key(decoded, password=None)
        except Exception:
            if len(decoded) == 32:
                key_int = int.from_bytes(decoded, byteorder="big")
                private_key = ec.derive_private_key(key_int, ec.SECP256R1())
            else:
                raise
    elif "BEGIN PRIVATE KEY" in key_value:
        private_key = serialization.load_pem_private_key(
            key_value.replace("\\n", "\n").encode("utf-8"),
            password=None,
        )
    else:
        compact = "".join(key_value.split())
        pad = "=" * (-len(compact) % 4)
        decoded = base64.b64decode(compact + pad)
        try:
            private_key = serialization.load_der_private_key(decoded, password=None)
        except Exception:
            private_key = serialization.load_pem_private_key(
                f"-----BEGIN PRIVATE KEY-----\n{key_value}\n-----END PRIVATE KEY-----".encode(
                    "utf-8"
                ),
                password=None,
            )

    # Official SDK sends DER-encoded ECDSA signature as base64.
    der_signature = private_key.sign(payload_bytes, ec.ECDSA(hashes.SHA256()))
    logger.debug("privy auth signature der_len=%s", len(der_signature))
    return base64.b64encode(der_signature).decode("ascii")


def _build_privy_signed_headers(app_id: str, request_expiry_ms: str) -> dict[str, str]:
    return {
        "privy-app-id": app_id,
        "privy-request-expiry": request_expiry_ms,
    }


def _privy_wallet_rpc(
    wallet_id: str, body: dict[str, Any], auth_keys: list[str] | None = None
) -> dict[str, Any]:
    app_id, app_secrets = _load_privy_app_credentials()
    key_pems = _load_privy_authorization_key_pems()
    if auth_keys:
        for key in auth_keys:
            raw_key = str(key or "").strip().strip('"').strip("'")
            if raw_key and raw_key not in key_pems:
                key_pems.append(raw_key)
    signing_keys = [str(k or "").strip() for k in key_pems if str(k or "").strip()]
    url = f"https://api.privy.io/v1/wallets/{wallet_id}/rpc"
    attempts: list[str] = []
    last_error_body: Any = None
    for secret_idx, app_secret in enumerate(app_secrets, start=1):
        if not signing_keys:
            continue
        for key_idx, key_pem in enumerate(signing_keys, start=1):
            request_expiry_ms = str(int(time.time() * 1000) + 60_000)
            signed_headers = _build_privy_signed_headers(app_id, request_expiry_ms)
            signature = _build_privy_authorization_signature(
                url,
                body,
                signed_headers,
                key_pem=key_pem,
                method="POST",
            )
            headers = {
                **signed_headers,
                "Content-Type": "application/json",
                "privy-authorization-signature": signature,
            }
            resp = requests.post(
                url, json=body, headers=headers, auth=(app_id, app_secret), timeout=20
            )
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:500]}
            if resp.status_code < 400:
                if isinstance(data, dict):
                    return data
                raise RuntimeError(f"privy wallet rpc returned unexpected body: {data}")
            attempts.append(f"s{secret_idx}k{key_idx}:{resp.status_code}")
            last_error_body = data
            # Retry auth-style failures only.
            if resp.status_code not in (401, 403):
                raise RuntimeError(
                    f"privy wallet rpc failed status={resp.status_code} body={data}"
                )
    raise RuntimeError(
        "privy wallet rpc failed after credential retries "
        f"attempts={attempts[:12]} last_body={last_error_body}"
    )


def _split_csv_env(raw: str) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").split(","):
        val = str(part or "").strip().strip('"').strip("'")
        if not val or val in out:
            continue
        out.append(val)
    return out


def _load_privy_server_signer_settings() -> tuple[list[str], list[str]]:
    signer_ids: list[str] = []
    for key_name in ("PRIVY_SERVER_SIGNER_ID", "PRIVY_SIGNER_ID"):
        raw_value = str(os.environ.get(key_name) or "").strip().strip('"').strip("'")
        if not raw_value:
            continue
        for candidate in _split_csv_env(raw_value):
            if candidate not in signer_ids:
                signer_ids.append(candidate)
    policy_raw = (
        str(
            os.environ.get("PRIVY_SERVER_SIGNER_POLICY_IDS")
            or os.environ.get("PRIVY_SIGNER_POLICY_IDS")
            or ""
        )
        .strip()
        .strip('"')
        .strip("'")
    )
    return signer_ids, _split_csv_env(policy_raw)


def _privy_patch_wallet(
    wallet_id: str, body: dict[str, Any], auth_keys: list[str] | None = None
) -> dict[str, Any]:
    app_id, app_secrets = _load_privy_app_credentials()
    key_pems = _load_privy_authorization_key_pems()
    if auth_keys:
        for key in auth_keys:
            raw_key = str(key or "").strip().strip('"').strip("'")
            if raw_key and raw_key not in key_pems:
                key_pems.append(raw_key)
    signing_keys = [str(k or "").strip() for k in key_pems if str(k or "").strip()]
    url = f"https://api.privy.io/v1/wallets/{wallet_id}"
    attempts: list[str] = []
    last_error_body: Any = None
    for secret_idx, app_secret in enumerate(app_secrets, start=1):
        if not signing_keys:
            continue
        for key_idx, key_pem in enumerate(signing_keys, start=1):
            request_expiry_ms = str(int(time.time() * 1000) + 60_000)
            signed_headers = _build_privy_signed_headers(app_id, request_expiry_ms)
            signature = _build_privy_authorization_signature(
                url,
                body,
                signed_headers,
                key_pem=key_pem,
                method="PATCH",
            )
            headers = {
                **signed_headers,
                "Content-Type": "application/json",
                "privy-authorization-signature": signature,
            }
            resp = requests.patch(
                url, json=body, headers=headers, auth=(app_id, app_secret), timeout=20
            )
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:500]}
            if resp.status_code < 400:
                if isinstance(data, dict):
                    return data
                raise RuntimeError(
                    f"privy wallet patch returned unexpected body: {data}"
                )
            attempts.append(f"s{secret_idx}k{key_idx}:{resp.status_code}")
            last_error_body = data
            if resp.status_code not in (401, 403):
                raise RuntimeError(
                    f"privy wallet patch failed status={resp.status_code} body={data}"
                )
    raise RuntimeError(
        "privy wallet patch failed after credential retries "
        f"attempts={attempts[:12]} last_body={last_error_body}"
    )


def _privy_ensure_wallet_additional_signer(
    wallet_id: str,
    signer_id: str,
    policy_ids: list[str],
    auth_keys: list[str] | None = None,
) -> dict[str, Any]:
    if not wallet_id or not signer_id:
        return {}
    signer_entry: dict[str, Any] = {
        "signer_id": signer_id,
    }
    if policy_ids:
        # Privy conditional signer policy docs scope signer policies via override_policy_ids.
        signer_entry["override_policy_ids"] = policy_ids
    payload: dict[str, Any] = {"additional_signers": [signer_entry]}
    try:
        result = _privy_patch_wallet(wallet_id, payload, auth_keys=auth_keys)
        logger.info(
            "patch_wallet success wallet_id=%s signer_id=%s override_policy_ids=%s result=%s",
            wallet_id,
            signer_id,
            policy_ids,
            result,
        )
        return result
    except Exception as exc:
        logger.error(
            "patch_wallet failed wallet_id=%s signer_id=%s exc_type=%s err=%s",
            wallet_id,
            signer_id,
            type(exc).__name__,
            exc,
        )
        if policy_ids:
            logger.warning(
                "wallet patch with override_policy_ids failed wallet_id=%s signer_id=%s; retrying without signer policy overrides err=%s",
                wallet_id,
                signer_id,
                exc,
            )
            fallback_payload: dict[str, Any] = {
                "additional_signers": [
                    {
                        "signer_id": signer_id,
                    }
                ]
            }
            try:
                fallback_result = _privy_patch_wallet(
                    wallet_id, fallback_payload, auth_keys=auth_keys
                )
                logger.info(
                    "patch_wallet fallback success wallet_id=%s signer_id=%s result=%s",
                    wallet_id,
                    signer_id,
                    fallback_result,
                )
                return fallback_result
            except Exception as fallback_exc:
                logger.error(
                    "patch_wallet fallback failed wallet_id=%s signer_id=%s exc_type=%s err=%s",
                    wallet_id,
                    signer_id,
                    type(fallback_exc).__name__,
                    fallback_exc,
                )
                raise
        raise


def _privy_get_wallet_snapshot(wallet_id: str) -> dict[str, Any]:
    wallet = str(wallet_id or "").strip()
    if not wallet:
        return {}

    app_id, app_secrets = _load_privy_app_credentials()
    url = f"https://api.privy.io/v1/wallets/{wallet}"
    for app_secret in app_secrets:
        try:
            resp = requests.get(
                url,
                headers={"privy-app-id": app_id},
                auth=(app_id, app_secret),
                timeout=20,
            )
            if resp.status_code >= 400:
                continue
            data = (
                resp.json()
                if "application/json"
                in str(resp.headers.get("Content-Type") or "").lower()
                else {}
            )
            return data if isinstance(data, dict) else {}
        except Exception:
            continue
    return {}


def _privy_wallet_has_additional_signer(wallet_id: str, signer_id: str) -> bool:
    wallet = str(wallet_id or "").strip()
    signer = str(signer_id or "").strip()
    if not wallet or not signer:
        return False

    data = _privy_get_wallet_snapshot(wallet)
    additional_signers = (
        data.get("additional_signers") if isinstance(data, dict) else None
    )
    if not isinstance(additional_signers, list):
        return False
    for item in additional_signers:
        if not isinstance(item, dict):
            continue
        existing = str(item.get("signer_id") or item.get("signerId") or "").strip()
        if existing and existing == signer:
            return True

    return False


def _extract_embedded_wallet_binding_from_accounts(
    linked_accounts: Any,
) -> tuple[str, str]:
    linked = linked_accounts if isinstance(linked_accounts, list) else []
    fallback_addr = ""
    for acc in linked:
        if not isinstance(acc, dict):
            continue
        addr = str(acc.get("address") or "").strip().lower()
        wallet_client_type = (
            str(acc.get("wallet_client_type") or acc.get("walletClientType") or "")
            .strip()
            .lower()
        )
        connector_type = (
            str(acc.get("connector_type") or acc.get("connectorType") or "")
            .strip()
            .lower()
        )
        wallet_id = str(
            acc.get("id") or acc.get("wallet_id") or acc.get("walletId") or ""
        ).strip()
        if _ADDR_RE.match(addr) and not fallback_addr:
            fallback_addr = addr
        # Only treat Privy embedded wallets as valid server-signing owners.
        if (
            wallet_client_type == "privy"
            and connector_type in {"", "embedded"}
            and _ADDR_RE.match(addr)
        ):
            return addr, wallet_id
    return fallback_addr, ""


def _resolve_wallet_id_from_privy_user(privy_user_id: str) -> tuple[str, str]:
    did = str(privy_user_id or "").strip()
    if not did:
        return "", ""

    try:
        app_id, app_secrets = _load_privy_app_credentials()
    except Exception:
        return "", ""

    for app_secret in app_secrets:
        for probe in _probe_privy_basic_auth(
            app_id=app_id, app_secret=app_secret, privy_did=did
        ):
            if (
                int(probe.get("status") or 0) < 200
                or int(probe.get("status") or 0) >= 300
            ):
                continue
            body = probe.get("body") if isinstance(probe, dict) else {}
            payload = (
                body.get("user")
                if isinstance(body, dict) and isinstance(body.get("user"), dict)
                else body
            )
            if not isinstance(payload, dict):
                continue
            linked = (
                payload.get("linked_accounts") or payload.get("linkedAccounts") or []
            )
            _, wallet_id = _extract_embedded_wallet_binding_from_accounts(linked)
            if wallet_id:
                linked_json = ""
                try:
                    linked_json = json.dumps(linked, ensure_ascii=False)
                except Exception:
                    linked_json = ""
                return wallet_id, linked_json

    return "", ""


def _privy_create_embedded_wallet(
    privy_user_id: str,
    *,
    additional_signers: list[dict[str, Any]] | None = None,
) -> tuple[str, str, list[str]]:
    """Create a new Privy embedded Ethereum wallet for an existing Privy user.

    Calls ``POST /v1/wallets`` with ``chain_type=ethereum`` and
    ``owner.user_id=did:privy:...``. When available, it provisions
    ``additional_signers`` at wallet creation time so server-side delegation is
    attached as part of the owner-authorized create request. Returns
    ``(wallet_id, wallet_address, signer_ids)`` on success, or
    ``("", "", [])`` on failure.
    """
    did = str(privy_user_id or "").strip()
    if not did:
        return "", "", []
    if not did.startswith("did:privy:"):
        did = f"did:privy:{did}"
    if not did.startswith("did:privy:") or len(did) <= len("did:privy:"):
        return "", "", []
    try:
        app_id, app_secrets = _load_privy_app_credentials()
    except Exception:
        return "", "", []

    normalized_signers: list[dict[str, Any]] = []
    requested_signer_ids: list[str] = []
    for raw_item in list(additional_signers or []):
        if not isinstance(raw_item, dict):
            continue
        signer_id = str(
            raw_item.get("signer_id") or raw_item.get("signerId") or ""
        ).strip()
        if not signer_id:
            continue
        signer_entry: dict[str, Any] = {"signer_id": signer_id}
        override_policy_ids = raw_item.get("override_policy_ids")
        if isinstance(override_policy_ids, list):
            filtered_policy_ids = [
                str(x).strip() for x in override_policy_ids if str(x).strip()
            ]
            if filtered_policy_ids:
                signer_entry["override_policy_ids"] = filtered_policy_ids
        normalized_signers.append(signer_entry)
        if signer_id not in requested_signer_ids:
            requested_signer_ids.append(signer_id)

    payload: dict[str, Any] = {
        "chain_type": "ethereum",
        "owner": {
            "user_id": did,
        },
    }
    if normalized_signers:
        payload["additional_signers"] = normalized_signers

    last_error = ""
    for app_secret in app_secrets:
        try:
            resp = requests.post(
                "https://api.privy.io/v1/wallets",
                json=payload,
                auth=(app_id, app_secret),
                headers={
                    "privy-app-id": app_id,
                    "Content-Type": "application/json",
                },
                timeout=20,
            )
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:400]}
            if resp.status_code < 300 and isinstance(data, dict):
                wallet_id = str(
                    data.get("id")
                    or data.get("wallet_id")
                    or data.get("walletId")
                    or ""
                ).strip()
                wallet_addr = (
                    str(data.get("address") or data.get("wallet_address") or "")
                    .strip()
                    .lower()
                )
                if wallet_id:
                    response_signer_ids: list[str] = []
                    for item in data.get("additional_signers") or []:
                        if not isinstance(item, dict):
                            continue
                        signer_id = str(
                            item.get("signer_id") or item.get("signerId") or ""
                        ).strip()
                        if signer_id and signer_id not in response_signer_ids:
                            response_signer_ids.append(signer_id)
                    logger.info(
                        "privy_create_embedded_wallet: created wallet_id=%s addr=%s for did=%s signers=%s",
                        wallet_id,
                        wallet_addr,
                        did,
                        response_signer_ids or requested_signer_ids,
                    )
                    return (
                        wallet_id,
                        wallet_addr,
                        response_signer_ids or requested_signer_ids,
                    )
            last_error = f"status={resp.status_code} body={str(data)[:300]}"
        except Exception as exc:
            last_error = str(exc)[:300]

    logger.warning(
        "privy_create_embedded_wallet: failed for did=%s last_error=%s", did, last_error
    )
    return "", "", []


def _get_user_signer_binding(
    login_wallet: str, *, require_wallets: bool = True
) -> dict[str, Any]:
    wallet = str(login_wallet or "").strip().lower()
    if not _ADDR_RE.match(wallet):
        return {}
    try:
        _ensure_user_tables()
        with db_connect() as conn:
            row = conn.execute(
                """
                SELECT main_wallet, funder_address, polymarket_wallet,
                       privy_embedded_wallet_id, privy_signer_status,
                       privy_user_id, privy_linked_accounts_json,
                       wallet_type, signature_type
                FROM users
                WHERE wallet_address = %s
                LIMIT 1
                """,
                (wallet,),
            ).fetchone()
        if not row:
            return {}

        data = dict(row)

        # Prefer a stable canonical user row when the same Privy user has multiple
        # local wallet rows (e.g. EOA row + embedded-wallet row after re-login).
        # This avoids signing with the embedded wallet as "from", which can cause
        # relayer signature mismatch against the user's existing proxy wallet mapping.
        canonical_data: dict[str, Any] | None = None
        try:
            current_wallet_id = str(data.get("privy_embedded_wallet_id") or "").strip()
            current_privy_user_id = str(data.get("privy_user_id") or "").strip()
            if current_privy_user_id and current_wallet_id:
                with db_connect() as conn:
                    canonical_row = conn.execute(
                        """
                        SELECT wallet_address, main_wallet, funder_address, polymarket_wallet,
                               privy_embedded_wallet_id, privy_signer_status, privy_user_id,
                               privy_linked_accounts_json,
                               wallet_type, signature_type
                        FROM users
                        WHERE privy_user_id = %s
                          AND privy_embedded_wallet_id = %s
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (current_privy_user_id, current_wallet_id),
                    ).fetchone()
                if canonical_row:
                    canonical_data = dict(canonical_row)
        except Exception:
            canonical_data = None

        if canonical_data:
            data = canonical_data
        main_wallet = str(data.get("main_wallet") or wallet).strip().lower()
        proxy_wallet = (
            str(data.get("funder_address") or data.get("polymarket_wallet") or "")
            .strip()
            .lower()
        )
        wallet_id = str(data.get("privy_embedded_wallet_id") or "").strip()
        signer_status = str(data.get("privy_signer_status") or "").strip().lower()
        privy_user_id = str(data.get("privy_user_id") or "").strip()

        linked_accounts: list[dict[str, Any]] = []
        raw_linked = data.get("privy_linked_accounts_json")
        if isinstance(raw_linked, list):
            linked_accounts = [x for x in raw_linked if isinstance(x, dict)]
        elif isinstance(raw_linked, str) and raw_linked.strip():
            try:
                parsed = json.loads(raw_linked)
                if isinstance(parsed, list):
                    linked_accounts = [x for x in parsed if isinstance(x, dict)]
            except Exception:
                linked_accounts = []

        embedded_wallet_addr, local_wallet_id = (
            _extract_embedded_wallet_binding_from_accounts(linked_accounts)
        )
        if not wallet_id and local_wallet_id:
            wallet_id = local_wallet_id

        refreshed_linked_json = ""
        if not wallet_id and privy_user_id:
            remote_wallet_id, remote_linked_json = _resolve_wallet_id_from_privy_user(
                privy_user_id
            )
            if remote_wallet_id:
                wallet_id = remote_wallet_id
                refreshed_linked_json = remote_linked_json

        existing_wallet_id = str(data.get("privy_embedded_wallet_id") or "").strip()
        if wallet_id and wallet_id != existing_wallet_id:
            now = datetime.now(timezone.utc).isoformat()
            with db_connect() as conn:
                conn.execute(
                    """
                    UPDATE users
                    SET privy_embedded_wallet_id = %s,
                        privy_linked_accounts_json = CASE
                            WHEN %s <> '' THEN %s
                            ELSE privy_linked_accounts_json
                        END,
                        updated_at = %s
                    WHERE wallet_address = %s
                    """,
                    (
                        wallet_id,
                        refreshed_linked_json,
                        refreshed_linked_json,
                        now,
                        wallet,
                    ),
                )
                conn.commit()

        main_ok = _ADDR_RE.match(main_wallet) is not None
        proxy_ok = _ADDR_RE.match(proxy_wallet) is not None
        if require_wallets and (not main_ok or not proxy_ok):
            return {}

        wallet_type = str(data.get("wallet_type") or "safe").strip().lower() or "safe"
        # T882-FundAudit#9: signature_type must be one of {2 (SAFE/Gnosis), 3
        # (POLY_1271 deposit_wallet)}. A garbage / NULL value silently defaulting
        # to 2 was previously the root cause of deposit_wallet users being routed
        # down the SAFE path ("invalid 'proxyWallet' field"). Validate explicitly
        # and align to wallet_type when mismatched.
        try:
            sig_type = int(data.get("signature_type") or 2)
        except (TypeError, ValueError):
            sig_type = 2
        if sig_type not in (2, 3):
            logger.warning(
                "signer_binding: invalid signature_type=%s for wallet=%s; defaulting to 2",
                sig_type,
                wallet,
            )
            sig_type = 2
        # Normalise: if wallet_type not explicitly 'deposit_wallet', treat as 'safe'
        if wallet_type not in ("safe", "deposit_wallet"):
            wallet_type = "safe"
        # Cross-check: deposit_wallet implies sig_type=3, safe implies sig_type=2.
        # Log if mismatched so we can audit DB drift, but trust wallet_type as the
        # authoritative dispatch key.
        expected_sig = 3 if wallet_type == "deposit_wallet" else 2
        if sig_type != expected_sig:
            logger.warning(
                "signer_binding: wallet_type=%s but signature_type=%s for wallet=%s; aligning sig_type to %s",
                wallet_type,
                sig_type,
                wallet,
                expected_sig,
            )
            sig_type = expected_sig

        return {
            "main_wallet": _as_checksum_address(main_wallet) if main_ok else "",
            "proxy_wallet": _as_checksum_address(proxy_wallet) if proxy_ok else "",
            "privy_embedded_wallet_id": wallet_id,
            "privy_embedded_wallet_address": (
                _as_checksum_address(embedded_wallet_addr)
                if _ADDR_RE.match(str(embedded_wallet_addr or "").lower())
                else ""
            ),
            "privy_signer_status": signer_status,
            "wallet_type": wallet_type,
            "signature_type": sig_type,
        }
    except Exception as exc:
        logger.warning("resolve signer binding failed for %s: %s", login_wallet, exc)
        return {}


def _privy_sign_safe_typed_data(
    wallet_id: str,
    typed_data: dict[str, Any],
    auth_keys: list[str] | None = None,
) -> str:
    rpc_result = _privy_wallet_rpc(
        wallet_id,
        {
            "chain_type": "ethereum",
            "method": "eth_signTypedData_v4",
            "params": {
                "typed_data": {
                    "types": typed_data.get("types") or {},
                    "primary_type": typed_data.get("primaryType"),
                    "domain": typed_data.get("domain") or {},
                    "message": typed_data.get("message") or {},
                }
            },
        },
        auth_keys=auth_keys,
    )
    signature = str((rpc_result.get("data") or {}).get("signature") or "").strip()
    if not signature:
        raise RuntimeError(f"privy typed-data signature missing body={rpc_result}")
    return signature


def _privy_sign_safe_struct_hash(
    wallet_id: str,
    struct_hash: str,
    auth_keys: list[str] | None = None,
) -> str:
    """T560: Sign the SafeTx EIP-712 digest with Privy `personal_sign`.

    Polymarket's `@polymarket/builder-abstract-signer` (both viem and ethers
    flavours) converts the struct-hash hex string to its underlying RAW 32
    bytes before calling `personal_sign`, producing the prefix
    "\x19Ethereum Signed Message:\n32" + <32 bytes>. We must mirror this
    exactly; UTF-8 encoding the hex string would prepend "\n66" + ascii
    and the relayer would silently reject with `bad request`. We pass the
    hex digest with `encoding: hex` so Privy treats it as raw bytes.
    """
    rpc_result = _privy_wallet_rpc(
        wallet_id,
        {
            "chain_type": "ethereum",
            "method": "personal_sign",
            "params": {
                "encoding": "hex",
                "message": str(struct_hash),
            },
        },
        auth_keys=auth_keys,
    )
    signature = str((rpc_result.get("data") or {}).get("signature") or "").strip()
    if not signature:
        raise RuntimeError(f"privy personal_sign signature missing body={rpc_result}")
    return signature


def _pack_safe_signature(raw_sig: str) -> str:
    """Apply Safe `eth_sign` v adjustment (v += 4) and return r||s||v packed hex.

    Mirrors `splitAndPackSig` from `@polymarket/builder-relayer-client`.
    """
    sig = str(raw_sig or "").lower()
    if sig.startswith("0x"):
        sig = sig[2:]
    if len(sig) != 130:
        raise RuntimeError(f"invalid signature length: {len(sig)}")
    r = sig[0:64]
    s = sig[64:128]
    v = int(sig[128:130], 16)
    if v in (0, 1):
        v += 31
    elif v in (27, 28):
        v += 4
    else:
        raise RuntimeError(f"invalid signature v: {v}")
    return "0x" + r + s + format(v, "02x")


def _build_server_side_relayer_payload(
    user_wallet: str,
    to_addr: str,
    data_hex: str,
    user_jwt: str | list[str] | tuple[str, ...] = "",
) -> dict[str, Any]:
    # Route deposit wallet users to the WALLET batch path.
    # Server-side signing for deposit wallets: EIP-712 over Batch struct
    # using the server signer (Privy additionalSigner / enrolled signer).
    binding = _get_user_signer_binding(user_wallet)
    if not binding:
        raise RuntimeError("missing user signer binding")
    if str(binding.get("privy_signer_status") or "").strip().lower() != "enrolled":
        logger.warning(
            "user signer status is not enrolled for %s; continue with backend wallet RPC signing attempt",
            user_wallet,
        )
    wallet_id = str(binding.get("privy_embedded_wallet_id") or "").strip()
    if not wallet_id:
        raise RuntimeError("privy embedded wallet id missing")
    signer_status = str(binding.get("privy_signer_status") or "").strip().lower()
    jwt_candidates: list[str]
    if isinstance(user_jwt, (list, tuple, set)):
        jwt_candidates = [str(x or "") for x in user_jwt]
    else:
        jwt_candidates = [str(user_jwt or "")]
    for cached_token in _get_cached_user_jwts(user_wallet):
        if cached_token not in jwt_candidates:
            jwt_candidates.append(cached_token)
    auth_keys = _load_privy_user_authorization_keys(jwt_candidates)
    signer_ids, policy_ids = _load_privy_server_signer_settings()
    signer_attached = False
    for signer_id in signer_ids:
        try:
            if _privy_wallet_has_additional_signer(wallet_id, signer_id):
                signer_attached = True
                break
        except Exception:
            continue

    if not signer_attached:
        for signer_id in signer_ids:
            try:
                _privy_ensure_wallet_additional_signer(
                    wallet_id, signer_id, policy_ids, auth_keys=auth_keys
                )
                signer_attached = True
                logger.info(
                    "auto-enrolled additional signer for wallet_id=%s signer_id=%s",
                    wallet_id,
                    signer_id,
                )
                _persist_signer_status(user_wallet, "enrolled")
                break
            except Exception as exc:
                logger.warning(
                    "auto signer enrollment via wallet patch failed wallet_id=%s signer_id=%s err=%s",
                    wallet_id,
                    signer_id,
                    exc,
                )

    if signer_status == "enrolled" and not signer_attached:
        _persist_signer_status(user_wallet, "pending")
    proxy_wallet = str(binding.get("proxy_wallet") or "")
    main_wallet = str(binding.get("main_wallet") or "")
    wtype = str(binding.get("wallet_type") or "safe").strip().lower()

    # Deposit Wallet Migration: for deposit_wallet users use WALLET nonce + batch payload.
    if wtype == "deposit_wallet":
        nonce = _get_relayer_nonce(main_wallet, "WALLET")
        deadline = int(time.time()) + 600
        calls = [{"target": _as_checksum_address(to_addr), "value": "0", "data": data_hex}]
        typed_data = _build_deposit_wallet_batch_typed_data(
            deposit_wallet=proxy_wallet,
            nonce=nonce,
            deadline=deadline,
            calls=calls,
        )
        # Server-side sign: Privy signs the Batch EIP-712 digest directly
        wallet_id = str(binding.get("privy_embedded_wallet_id") or "").strip()
        jwt_candidates: list[str] = (
            [str(x or "") for x in user_jwt] if isinstance(user_jwt, (list, tuple, set))
            else [str(user_jwt or "")]
        )
        for cached_token in _get_cached_user_jwts(user_wallet):
            if cached_token not in jwt_candidates:
                jwt_candidates.append(cached_token)
        auth_keys = _load_privy_user_authorization_keys(jwt_candidates)
        raw_sig = _privy_sign_safe_typed_data(wallet_id, typed_data, auth_keys=auth_keys)
        signature = "0x" + raw_sig.lower().removeprefix("0x")
        # Relayer-v2 enforces `from == auth address` on every payload.
        # Ownership of the deposit wallet is proven by the EIP-712 signature
        # over the Batch typed data (verified on-chain via ERC-1271 against
        # the wallet's registered owner) carried in `depositWalletParams`.
        return {
            "type": "WALLET",
            "from": _resolve_relayer_from(main_wallet),
            "to": _pm_v2.DEPOSIT_WALLET_FACTORY,
            "nonce": str(nonce),
            "signature": signature,
            "depositWalletParams": {
                "depositWallet": _as_checksum_address(proxy_wallet),
                "deadline": str(deadline),
                "calls": calls,
            },
            "metadata": "",
        }

    # T550: Polymarket relayer keys SAFE nonce by the OWNER EOA, not the Safe
    # proxy address. Querying with the Safe address always returns "0" which
    # caused every signed Safe tx to be rejected with `bad request` once the
    # Safe accumulated any prior nonces. See `RelayClient.getNonce` in
    # builder-relayer-client SDK (`signerAddress`, not Safe address).
    nonce = _get_relayer_nonce(main_wallet, "SAFE")
    signature_params = _default_signature_params()
    typed_data = _build_safe_typed_data(
        to_addr=to_addr,
        data_hex=data_hex,
        proxy_wallet=proxy_wallet,
        nonce=nonce,
        signature_params=signature_params,
    )
    # T465: Polymarket Safe variant requires `personal_sign(utf8(structHashHex))`
    # with v += 4 packing. Compute the EIP-712 digest server-side and ask Privy
    # to UTF-8 encode it (matching official ethers-v5 `signMessage` behaviour).
    struct_hash = _compute_safe_struct_hash(
        chain_id=int(_CHAIN_ID),
        safe_addr=_as_checksum_address(proxy_wallet),
        to_addr=_as_checksum_address(to_addr),
        value=str(typed_data.get("message", {}).get("value") or "0"),
        data_hex=data_hex,
        operation=int(str(signature_params.get("operation") or "0")),
        safe_tx_gas=int(str(signature_params.get("safeTxnGas") or "0")),
        base_gas=int(str(signature_params.get("baseGas") or "0")),
        gas_price=int(str(signature_params.get("gasPrice") or "0")),
        gas_token=str(signature_params.get("gasToken") or _ZERO_ADDR),
        refund_receiver=str(signature_params.get("refundReceiver") or _ZERO_ADDR),
        nonce=int(str(nonce)),
    )
    raw_sig = _privy_sign_safe_struct_hash(wallet_id, struct_hash, auth_keys=auth_keys)
    signature = _pack_safe_signature(raw_sig)
    # T551: verify locally that the signature actually recovers to the Safe
    # owner EOA before submitting; this isolates "Privy signed with wrong key"
    # from "relayer disagrees with payload shape" classes of bad-request.
    # T560: signer signs RAW 32 bytes; recover with `primitive=` so the
    # eth_account prefix is `"\x19Ethereum Signed Message:\n32"` + bytes.
    try:
        from eth_account import Account  # noqa: PLC0415
        from eth_account.messages import encode_defunct  # noqa: PLC0415

        sh_clean = struct_hash.removeprefix("0x")
        recovered = Account.recover_message(
            encode_defunct(primitive=bytes.fromhex(sh_clean)),
            signature="0x" + raw_sig.lower().removeprefix("0x"),
        )
        ok = recovered.lower() == main_wallet.lower()
        logger.info(
            "[T551 sig-check] recovered=%s main=%s match=%s struct_hash=%s",
            recovered,
            main_wallet,
            ok,
            struct_hash,
        )
    except Exception as exc:
        logger.warning("[T551 sig-check] recover failed err=%s", exc)
    return {
        "from": main_wallet,
        "to": to_addr,
        "proxyWallet": proxy_wallet,
        "data": data_hex,
        "nonce": str(nonce),
        "signature": signature,
        "signatureParams": signature_params,
        "type": "SAFE",
        # T551: SDK always sends `metadata: ""` (or user-provided string).
        # Polymarket relayer-v2 may reject payloads missing this field.
        "metadata": "",
    }


def _resolve_signing_mode(binding: dict[str, Any]) -> str:
    """Decide whether the user must sign client-side or can sign server-side.

    T462: Polymarket Safes are owned by the EOA that originally created the proxy.
    For users who logged in via an external wallet (MetaMask/Coinbase), the Safe
    owner is that external EOA, NOT the Privy embedded wallet. Server-side
    signing with the embedded wallet would always fail relayer signature checks.
    Only when ``main_wallet`` matches the user's Privy embedded wallet address
    is server-side signing valid.
    """

    main_wallet = str(binding.get("main_wallet") or "").strip().lower()
    embedded_addr = (
        str(binding.get("privy_embedded_wallet_address") or "").strip().lower()
    )
    signer_status = str(binding.get("privy_signer_status") or "").strip().lower()
    if (
        main_wallet
        and embedded_addr
        and main_wallet == embedded_addr
        and signer_status == "enrolled"
    ):
        return "server"
    return "client"


def _build_typed_data_skeleton(
    user_wallet: str,
    to_addr: str,
    data_hex: str,
    *,
    value: str = "0",
) -> dict[str, Any]:
    """Build typed-data + payload skeleton + signing mode for a relayer call.

    Routes to deposit wallet WALLET batch path for deposit_wallet users, and
    to the legacy SAFE path for proxy/Safe users.

    Returned dict keys:
      typed_data         EIP-712 typed data the client (or server) signs
      payload_skeleton   relayer payload missing only ``signature``
      signing_address    Address that MUST produce the signature (owner EOA)
      mode               "client" or "server"
      proxy_wallet       Safe proxy address or deposit wallet address
      nonce              Relayer nonce snapshot used in typed_data
      wallet_type        'safe' or 'deposit_wallet'
    """
    # Peek at wallet_type to route to the right signing path.
    binding_peek = _get_user_signer_binding(user_wallet, require_wallets=False)
    if binding_peek and binding_peek.get("wallet_type") == "deposit_wallet":
        return _build_deposit_wallet_typed_data_skeleton(
            user_wallet, to_addr, data_hex, value=value
        )

    binding = _get_user_signer_binding(user_wallet)
    if not binding:
        raise RuntimeError("missing user signer binding")
    main_wallet = str(binding.get("main_wallet") or "").strip()
    proxy_wallet = str(binding.get("proxy_wallet") or "").strip()
    if not _ADDR_RE.match(main_wallet.lower()):
        raise RuntimeError("missing main_wallet for user")
    if not _ADDR_RE.match(proxy_wallet.lower()):
        raise RuntimeError("missing proxy_wallet for user")
    # T550: see _build_server_side_relayer_payload — the relayer's SAFE nonce
    # endpoint is keyed by the owner EOA, not the Safe proxy address.
    nonce = _get_relayer_nonce(main_wallet, "SAFE")
    signature_params = _default_signature_params()
    typed_data = _build_safe_typed_data(
        to_addr=to_addr,
        data_hex=data_hex,
        proxy_wallet=proxy_wallet,
        nonce=nonce,
        signature_params=signature_params,
        value=value,
    )
    # T463: pre-compute the SafeTx EIP-712 digest server-side so the browser
    # wallet only has to do a raw `personal_sign` over 32 bytes — the only
    # signing scheme the Polymarket relayer accepts (Safe eth_sign mode v+4).
    struct_hash = _compute_safe_struct_hash(
        chain_id=int(_CHAIN_ID),
        safe_addr=_as_checksum_address(proxy_wallet),
        to_addr=_as_checksum_address(to_addr),
        value=str(value),
        data_hex=data_hex,
        operation=int(str(signature_params.get("operation") or "0")),
        safe_tx_gas=int(str(signature_params.get("safeTxnGas") or "0")),
        base_gas=int(str(signature_params.get("baseGas") or "0")),
        gas_price=int(str(signature_params.get("gasPrice") or "0")),
        gas_token=str(signature_params.get("gasToken") or _ZERO_ADDR),
        refund_receiver=str(signature_params.get("refundReceiver") or _ZERO_ADDR),
        nonce=int(str(nonce)),
    )
    skeleton = {
        "from": _as_checksum_address(main_wallet),
        "to": _as_checksum_address(to_addr),
        "proxyWallet": _as_checksum_address(proxy_wallet),
        "data": data_hex,
        "nonce": str(nonce),
        "signatureParams": signature_params,
        "type": "SAFE",
        # T553: SDK always sends `metadata: ""` in the relayer submit body.
        # Polymarket relayer-v2 rejects payloads missing this field with
        # `bad request`. Carry it through the client-sign skeleton too.
        "metadata": "",
    }
    return {
        "typed_data": typed_data,
        "struct_hash": struct_hash,
        "signing_method": "personal_sign_struct_hash",
        "payload_skeleton": skeleton,
        "signing_address": _as_checksum_address(main_wallet),
        "mode": _resolve_signing_mode(binding),
        "proxy_wallet": _as_checksum_address(proxy_wallet),
        "nonce": str(nonce),
        "embedded_wallet_address": str(
            binding.get("privy_embedded_wallet_address") or ""
        ),
        "wallet_type": "safe",
    }


def _persist_signer_status(login_wallet: str, status: str) -> None:
    wallet = str(login_wallet or "").strip().lower()
    if not _ADDR_RE.match(wallet):
        return
    try:
        _ensure_user_tables()
        now_ts = int(time.time())
        with db_connect() as conn:
            conn.execute(
                "UPDATE users SET privy_signer_status=%s, updated_at=%s WHERE wallet_address=%s",
                (str(status or "").strip().lower(), now_ts, wallet),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(
            "persist signer status failed wallet=%s status=%s err=%s",
            wallet,
            status,
            exc,
        )


def _log_signer_debug(context: str, user_wallet: str, binding: dict[str, Any]) -> None:
    signer_ids, policy_ids = _load_privy_server_signer_settings()
    logger.info("=== %s DEBUG START ===", str(context or "SIGNER").upper())
    logger.info("User wallet: %s", user_wallet)
    logger.info(
        "Privy Wallet ID: %s", str(binding.get("privy_embedded_wallet_id") or "")
    )
    logger.info("Signer Enrolled: %s", str(binding.get("privy_signer_status") or ""))
    logger.info("Signer IDs from env: %s", signer_ids)
    logger.info("Policy IDs from env: %s", policy_ids)
    logger.info("Main wallet: %s", str(binding.get("main_wallet") or ""))
    logger.info("Proxy wallet: %s", str(binding.get("proxy_wallet") or ""))
    logger.info("=== %s DEBUG END ===", str(context or "SIGNER").upper())


def _build_poly_builder_hmac(
    secret_b64: str,
    timestamp: int,
    method: str,
    request_path: str,
    body: str | None,
) -> str:
    """Reproduce builder-signing-sdk's buildHmacSignature exactly:
    HMAC-SHA256 over `timestamp + method + requestPath + body` using
    base64-decoded secret as the key, then URL-safe base64 encode the
    digest (replace '+' with '-' and '/' with '_', keep '=' padding)."""
    import base64
    import hmac as _hmac
    import hashlib as _hashlib
    msg = f"{timestamp}{method}{request_path}"
    if body:
        msg += body
    # Polymarket secret may be standard or URL-safe base64; pad to multiple of 4.
    s = secret_b64.strip()
    s_padded = s + "=" * ((4 - len(s) % 4) % 4)
    try:
        key = base64.urlsafe_b64decode(s_padded)
    except Exception:
        key = base64.b64decode(s_padded)
    digest = _hmac.new(key, msg.encode("utf-8"), _hashlib.sha256).digest()
    sig_b64 = base64.b64encode(digest).decode("ascii")
    return sig_b64.replace("+", "-").replace("/", "_")


def _get_relayer_headers(
    method: str = "POST",
    request_path: str = "/submit",
    body: str | None = None,
) -> dict[str, str]:
    """Return relayer auth headers.

    Prefer official builder HMAC headers (POLY_BUILDER_*) when configured,
    because that mode allows arbitrary `from` (owner EOA) — relayer
    authenticates the builder via HMAC, then validates ownership via the
    EIP-712 signature carried inside the payload. Fall back to the
    legacy RELAYER_API_KEY / RELAYER_API_KEY_ADDRESS scheme, which forces
    `from == RELAYER_API_KEY_ADDRESS`.
    """
    bk = str(os.environ.get("POLY_BUILDER_API_KEY") or "").strip()
    bs = str(os.environ.get("POLY_BUILDER_SECRET") or "").strip()
    bp = str(os.environ.get("POLY_BUILDER_PASSPHRASE") or "").strip()
    if bk and bs and bp:
        ts = int(time.time())
        sig = _build_poly_builder_hmac(bs, ts, method, request_path, body)
        return {
            "POLY_BUILDER_API_KEY": bk,
            "POLY_BUILDER_PASSPHRASE": bp,
            "POLY_BUILDER_SIGNATURE": sig,
            "POLY_BUILDER_TIMESTAMP": str(ts),
            "Content-Type": "application/json",
        }
    api_key = str(os.environ.get("RELAYER_API_KEY") or "").strip()
    api_addr = str(os.environ.get("RELAYER_API_KEY_ADDRESS") or "").strip()
    if api_key and api_addr and _ADDR_RE.match(api_addr):
        return {
            "RELAYER_API_KEY": api_key,
            "RELAYER_API_KEY_ADDRESS": _as_checksum_address(api_addr),
            "Content-Type": "application/json",
        }
    raise EnvironmentError(
        "Relayer auth requires POLY_BUILDER_* HMAC creds or RELAYER_API_KEY+ADDRESS"
    )


def _relayer_auth_address() -> str:
    """Legacy fallback: returns RELAYER_API_KEY_ADDRESS used as `from` only
    when builder HMAC creds are NOT configured (legacy auth scheme forces
    `from == auth address`). Prefer using owner EOA when HMAC builder
    auth is available."""
    api_addr = str(os.environ.get("RELAYER_API_KEY_ADDRESS") or "").strip()
    if not _ADDR_RE.match(api_addr):
        raise EnvironmentError("RELAYER_API_KEY_ADDRESS missing or invalid")
    return _as_checksum_address(api_addr)


def _has_builder_hmac_creds() -> bool:
    return bool(
        str(os.environ.get("POLY_BUILDER_API_KEY") or "").strip()
        and str(os.environ.get("POLY_BUILDER_SECRET") or "").strip()
        and str(os.environ.get("POLY_BUILDER_PASSPHRASE") or "").strip()
    )


def _resolve_relayer_from(owner_eoa: str) -> str:
    """Resolve the `from` field for a relayer payload.

    With official HMAC builder auth, `from` should be the wallet OWNER
    (the EOA that signed the EIP-712 batch). With legacy auth, the
    relayer enforces `from == RELAYER_API_KEY_ADDRESS`.
    """
    if _has_builder_hmac_creds():
        return _as_checksum_address(owner_eoa)
    return _relayer_auth_address()


def _extract_relayer_payload(body: dict[str, Any]) -> dict[str, Any] | None:
    payload = (
        body.get("relayer_tx") if isinstance(body.get("relayer_tx"), dict) else body
    )
    tx_type = str(payload.get("type") or "").strip().upper()
    if tx_type in ("", "SAFE"):
        required = (
            "from",
            "to",
            "proxyWallet",
            "data",
            "nonce",
            "signature",
            "signatureParams",
            "type",
        )
        if all(k in payload for k in required):
            return {
                "from": payload.get("from"),
                "to": payload.get("to"),
                "proxyWallet": payload.get("proxyWallet"),
                "data": payload.get("data"),
                "nonce": str(payload.get("nonce")),
                "signature": payload.get("signature"),
                "signatureParams": payload.get("signatureParams"),
                "type": payload.get("type") or "SAFE",
                # T553: SDK always sends `metadata` (default empty string).
                # Polymarket relayer-v2 rejects payloads missing this field.
                "metadata": str(payload.get("metadata") or ""),
            }
        return None

    # T886 root fix (2026-05-07): deposit_wallet withdraw uses WALLET batches
    # (`signing_method=eip712_batch`) with payload shape:
    # {type, from, to, nonce, signature, depositWalletParams, metadata}.
    # Prior SAFE-only parser ignored this payload and caused the endpoint to
    # return requires_client_sign repeatedly, blocking all actual withdrawals.
    if tx_type == "WALLET":
        dw = payload.get("depositWalletParams")
        if not isinstance(dw, dict):
            return None
        calls = dw.get("calls")
        if not isinstance(calls, list) or not calls:
            return None
        required_wallet = ("from", "to", "nonce", "signature", "type")
        if not all(k in payload for k in required_wallet):
            return None
        if not dw.get("depositWallet") or not dw.get("deadline"):
            return None
        return {
            "type": "WALLET",
            "from": payload.get("from"),
            "to": payload.get("to"),
            "nonce": str(payload.get("nonce")),
            "signature": payload.get("signature"),
            "depositWalletParams": {
                "depositWallet": dw.get("depositWallet"),
                "deadline": str(dw.get("deadline")),
                "calls": calls,
            },
            "metadata": str(payload.get("metadata") or ""),
        }
    return None


def _relayer_submit(payload: dict[str, Any]) -> dict[str, Any]:
    # T553: defense-in-depth — guarantee `metadata` is present on every
    # outbound relayer submit. The official SDK always sends this field;
    # Polymarket relayer-v2 returns `{"error":"bad request"}` without it.
    if "metadata" not in payload:
        payload = {**payload, "metadata": ""}
    # T557: serialize once so the body bytes used for HMAC match the wire
    # bytes byte-for-byte. requests.post(json=...) would re-serialize and
    # break the HMAC signature.
    body_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    headers = _get_relayer_headers("POST", "/submit", body_str)
    # T553 sig-check: recover the EOA from the supplied signature against the
    # SafeTx struct hash so we can isolate "client signed with wrong key" from
    # "relayer rejected payload shape". Best-effort; never blocks submit.
    try:
        from eth_account import Account  # noqa: PLC0415
        from eth_account.messages import encode_defunct  # noqa: PLC0415

        sp = payload.get("signatureParams") or {}
        struct_hash = _compute_safe_struct_hash(
            chain_id=int(_CHAIN_ID),
            safe_addr=_as_checksum_address(str(payload.get("proxyWallet") or "")),
            to_addr=_as_checksum_address(str(payload.get("to") or "")),
            value=str(sp.get("value") or "0"),
            data_hex=str(payload.get("data") or "0x"),
            operation=int(str(sp.get("operation") or "0")),
            safe_tx_gas=int(str(sp.get("safeTxnGas") or "0")),
            base_gas=int(str(sp.get("baseGas") or "0")),
            gas_price=int(str(sp.get("gasPrice") or "0")),
            gas_token=str(sp.get("gasToken") or _ZERO_ADDR),
            refund_receiver=str(sp.get("refundReceiver") or _ZERO_ADDR),
            nonce=int(str(payload.get("nonce") or "0")),
        )
        sig_hex = str(payload.get("signature") or "").lower().removeprefix("0x")
        if len(sig_hex) == 130:
            r_hex = sig_hex[0:64]
            s_hex = sig_hex[64:128]
            v_packed = int(sig_hex[128:130], 16)
            # Reverse Safe eth_sign packing: pack always emits v in {31,32}
            # (v += 4 from {27,28} or v += 31 from {0,1}). Subtract 4 to get
            # the canonical {27,28} that eth_account expects.
            v_int = v_packed - 4 if v_packed in (31, 32) else v_packed
            raw_sig = "0x" + r_hex + s_hex + format(v_int & 0xFF, "02x")
            # T560: SDK personal-signs the RAW 32 bytes of the struct hash
            # (viem `toBytes`, ethers `arrayify`). Use `primitive=` so the
            # eth_account prefix becomes "\x19Ethereum Signed Message:\n32"
            # + <raw 32 bytes>, matching the relayer's verification path.
            sh_clean = struct_hash.removeprefix("0x")
            recovered = Account.recover_message(
                encode_defunct(primitive=bytes.fromhex(sh_clean)),
                signature=raw_sig,
            )
            from_addr = str(payload.get("from") or "")
            ok = recovered.lower() == from_addr.lower()
            logger.info(
                "[T553 sig-check] recovered=%s from=%s match=%s nonce=%s struct_hash=%s",
                recovered,
                from_addr,
                ok,
                payload.get("nonce"),
                struct_hash,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[T553 sig-check] recover failed err=%s", exc)
    # T559: full outbound body dump for diagnosis. Safe to log because the
    # signature only commits to a one-shot withdraw of a specific amount —
    # there is nothing replay-attackable beyond what the user already signed.
    logger.info("[T559 wire-body] %s", body_str)
    resp = requests.post(
        f"{_RELAYER_API}/submit",
        headers=headers,
        data=body_str.encode("utf-8"),
        timeout=20,
    )
    content_type = str(resp.headers.get("Content-Type") or "")
    body: Any
    if "application/json" in content_type.lower():
        body = resp.json()
    else:
        body = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        # T542/T543 — surface enough payload context to root-cause "bad
        # request" / "Invalid signature" without leaking the signature blob.
        try:
            sig_str = str(payload.get("signature") or "")
            sig_preview = (
                sig_str[:18] + "…" + sig_str[-6:] + f"(len={len(sig_str)})"
                if sig_str
                else "<empty>"
            )
            data_str = str(payload.get("data") or "")
            data_preview = (
                data_str[:42] + "…" + f"(len={len(data_str)})"
                if len(data_str) > 50
                else data_str
            )
            logger.error(
                "[Relayer 4xx] status=%s body=%s | type=%s from=%s to=%s "
                "proxyWallet=%s nonce=%s data=%s sig=%s sigParams=%s",
                resp.status_code,
                body,
                payload.get("type"),
                payload.get("from"),
                payload.get("to"),
                payload.get("proxyWallet"),
                payload.get("nonce"),
                data_preview,
                sig_preview,
                payload.get("signatureParams"),
            )
        except Exception:
            pass
        raise RuntimeError(
            f"relayer submit failed status={resp.status_code} body={body}"
        )
    if isinstance(body, dict):
        return body
    raise RuntimeError(f"relayer submit returned unexpected body: {body}")


def _is_stale_nonce_or_sig_error(exc: Exception) -> bool:
    """T538/T539 — heuristic for "the signature was correct at sign time but
    the relayer's view of the Safe nonce moved on" or "Invalid signature".

    Both classes are recoverable for the SERVER-SIDE signing path (we own
    the signing key and can re-sign immediately with the fresh nonce).
    """
    msg = str(exc).lower()
    if "status=400" not in msg:
        return False
    return any(
        kw in msg
        for kw in ("nonce", "invalid sig", "invalid signature", "signature mismatch")
    )


def _relayer_submit_with_retry(
    build_payload_fn, max_retries: int = 1
) -> dict[str, Any]:
    """Submit ``build_payload_fn()`` to the relayer. On a stale-nonce /
    invalid-signature 400, rebuild (which re-fetches the nonce and re-signs)
    and try once more. Used by the SERVER-SIDE signing branch only."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        payload = build_payload_fn()
        try:
            result = _relayer_submit(payload)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < max_retries and _is_stale_nonce_or_sig_error(exc):
                logger.warning(
                    "relayer submit stale-nonce/sig retry attempt=%d err=%s",
                    attempt + 1,
                    exc,
                )
                # T918-C: drop cached nonces for both EOA-keyed lookups so
                # the next build_payload_fn call fetches a fresh value.
                try:
                    with _NONCE_CACHE_LOCK:
                        _NONCE_CACHE.clear()
                except Exception:
                    pass
                continue
            raise
        else:
            try:
                with _NONCE_CACHE_LOCK:
                    _NONCE_CACHE.clear()
            except Exception:
                pass
            return result
    if last_exc:
        raise last_exc
    raise RuntimeError("relayer submit retry loop exited without result")


def _bridge_create_withdraw_address(
    *,
    source_address: str,
    to_chain_id: str,
    to_token_address: str,
    recipient_address: str,
) -> dict[str, Any]:
    payload = {
        "address": source_address,
        "toChainId": str(to_chain_id),
        "toTokenAddress": to_token_address,
        "recipientAddr": recipient_address,
    }
    resp = requests.post(f"{_BRIDGE_API}/withdraw", json=payload, timeout=20)
    body: Any
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        raise RuntimeError(
            f"bridge withdraw failed status={resp.status_code} body={body}"
        )
    if isinstance(body, dict):
        return body
    raise RuntimeError(f"bridge withdraw returned unexpected body: {body}")


# T884-BRIDGE helpers ────────────────────────────────────────────────────────
def _bridge_builder_headers() -> dict[str, str]:
    """Return X-Builder-Code header for Bridge API attribution."""
    code = (os.getenv("POLY_BUILDER_CODE") or "").strip()
    headers = {"Content-Type": "application/json"}
    if code:
        headers["X-Builder-Code"] = code
    return headers


def _bridge_create_deposit_addresses(polymarket_wallet: str) -> dict[str, Any]:
    """T884-BRIDGE: Get the multi-chain intake addresses for a Polymarket
    wallet. Result is cached per-wallet (24h) since the Bridge API returns
    deterministic addresses derived from the recipient.

    Polymarket Bridge then auto-bridges + swaps + wraps any deposit (USDC,
    USDT, etc.) into pUSD credited to ``polymarket_wallet``. No on-chain
    OnRamp call is needed from our side.
    """
    addr_key = str(polymarket_wallet or "").lower().strip()
    if not _ADDR_RE.match(addr_key):
        raise RuntimeError(f"invalid polymarket wallet for bridge: {polymarket_wallet}")
    now_ts = time.time()
    with _BRIDGE_ADDR_CACHE_LOCK:
        cached = _BRIDGE_ADDR_CACHE.get(addr_key)
        if cached and (now_ts - float(cached.get("_cached_at") or 0)) < _BRIDGE_ADDR_CACHE_TTL:
            return cached["data"]
    payload = {"address": _as_checksum_address(polymarket_wallet)}
    try:
        resp = requests.post(
            f"{_BRIDGE_API}/deposit",
            json=payload,
            headers=_bridge_builder_headers(),
            timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"bridge deposit request failed: {exc}") from exc
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        raise RuntimeError(
            f"bridge deposit failed status={resp.status_code} body={body}"
        )
    if not isinstance(body, dict) or not isinstance(body.get("address"), dict):
        raise RuntimeError(f"bridge deposit unexpected body: {body}")
    addr_map = body["address"]
    data: dict[str, Any] = {
        "polymarket_wallet": _as_checksum_address(polymarket_wallet),
        "evm": addr_map.get("evm"),
        "svm": addr_map.get("svm"),
        "btc": addr_map.get("btc"),
        "tron": addr_map.get("tron"),
        "note": body.get("note"),
    }
    with _BRIDGE_ADDR_CACHE_LOCK:
        _BRIDGE_ADDR_CACHE[addr_key] = {"_cached_at": now_ts, "data": data}
    return data


def _bridge_get_deposit_status(
    intake_evm_address: str,
    extra_watch_addresses: list[str] | None = None,
) -> dict[str, Any]:
    """T884-BRIDGE: Read the status of inbound bridge transactions for one
    intake address. Returns ``{"transactions": [...]}`` (or ``{"error": ...}``).
    """
    if not _ADDR_RE.match(str(intake_evm_address or "").lower()):
        return {"transactions": [], "error": "invalid_address"}
    try:
        resp = requests.get(
            f"{_BRIDGE_API}/status/{_as_checksum_address(intake_evm_address)}",
            headers=_bridge_builder_headers(),
            timeout=10,
        )
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:500]}
        payload: dict[str, Any]
        if resp.status_code >= 400:
            payload = {"transactions": [], "error": str(body)}
        elif isinstance(body, dict):
            payload = body
        else:
            payload = {"transactions": []}

        # Bridge status can lag right after on-chain deposits. Enrich with
        # direct on-chain Transfer logs so users still see recent activity.
        try:
            payload = _bridge_merge_with_onchain_activity(
                payload,
                intake_evm_address,
                extra_watch_addresses=extra_watch_addresses,
            )
        except Exception as enrich_exc:
            logger.warning("[T890-BRIDGE] on-chain activity enrich failed: %s", enrich_exc)
        return payload
    except Exception as exc:
        payload = {"transactions": [], "error": str(exc)}
        try:
            return _bridge_merge_with_onchain_activity(
                payload,
                intake_evm_address,
                extra_watch_addresses=extra_watch_addresses,
            )
        except Exception:
            return payload


def _bridge_topic_for_address(addr: str) -> str:
    raw = str(addr or "").strip().lower().replace("0x", "")
    if len(raw) != 40:
        raise ValueError(f"invalid topic address: {addr}")
    return "0x" + ("0" * 24) + raw


def _bridge_format_amount(amount_raw: int, decimals: int) -> str:
    raw = int(amount_raw or 0)
    d = int(decimals or 0)
    if d <= 0:
        return str(raw)
    base = 10 ** d
    whole = raw // base
    frac = raw % base
    if frac == 0:
        return str(whole)
    frac_s = f"{frac:0{d}d}".rstrip("0")
    # Keep UI concise for activity rows.
    if len(frac_s) > 6:
        frac_s = frac_s[:6].rstrip("0")
    return f"{whole}.{frac_s}" if frac_s else str(whole)


def _bridge_block_timestamp_iso(block_number: int) -> str:
    try:
        raw = _polygon_rpc_call("eth_getBlockByNumber", [hex(int(block_number)), False])
        if not isinstance(raw, dict):
            return ""
        ts_hex = raw.get("timestamp")
        ts = _hex_to_int(ts_hex) if isinstance(ts_hex, str) else 0
        if ts <= 0:
            return ""
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _bridge_recent_onchain_inbound(intake_evm_address: str, max_items: int = 24) -> list[dict[str, Any]]:
    try:
        latest_hex = _polygon_rpc_call("eth_blockNumber", [])
        latest = _hex_to_int(latest_hex)
    except Exception:
        return []
    # Keep scan window tight so eth_getLogs stays responsive on public RPCs.
    from_block = max(0, int(latest) - 20_000)
    to_topic = _bridge_topic_for_address(intake_evm_address)
    out: list[dict[str, Any]] = []
    block_ts_cache: dict[int, str] = {}

    def _fetch_logs_with_fallbacks(filter_obj: dict[str, Any]) -> list[dict[str, Any]]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [filter_obj],
        }
        saw_success = False
        for rpc in _polygon_rpcs():
            try:
                resp = requests.post(rpc, json=payload, timeout=12)
                if resp.status_code >= 400:
                    continue
                data = resp.json() if resp.content else {}
                if isinstance(data, dict) and data.get("error"):
                    continue
                result = data.get("result") if isinstance(data, dict) else None
                if not isinstance(result, list):
                    continue
                saw_success = True
                if result:
                    return result
            except Exception:
                continue
        return [] if saw_success else []

    for token in _BRIDGE_ACTIVITY_TOKENS:
        token_addr = _as_checksum_address(str(token.get("address") or ""))
        symbol = str(token.get("symbol") or "TOKEN")
        decimals = int(token.get("decimals") or 6)
        query = {
            "fromBlock": hex(from_block),
            "toBlock": "latest",
            "address": token_addr,
            "topics": [_ERC20_TRANSFER_TOPIC0, None, to_topic],
        }
        logs = _fetch_logs_with_fallbacks(query)
        if not isinstance(logs, list):
            continue
        for lg in logs:
            if not isinstance(lg, dict):
                continue
            tx_hash = str(lg.get("transactionHash") or "").strip()
            blk = _hex_to_int(str(lg.get("blockNumber") or "0x0"))
            idx = _hex_to_int(str(lg.get("logIndex") or "0x0"))
            amount = _hex_to_int(str(lg.get("data") or "0x0"))
            if amount <= 0 or not tx_hash:
                continue
            ts = block_ts_cache.get(blk)
            if ts is None:
                ts = _bridge_block_timestamp_iso(blk)
                block_ts_cache[blk] = ts
            out.append(
                {
                    "tx_hash": tx_hash,
                    "source_chain": "POLYGON",
                    "source_asset": symbol,
                    "source_amount": _bridge_format_amount(amount, decimals),
                    "status": "PENDING",
                    "created_at": ts,
                    "_sort_block": blk,
                    "_sort_idx": idx,
                }
            )

    out.sort(key=lambda x: (int(x.get("_sort_block") or 0), int(x.get("_sort_idx") or 0)), reverse=True)
    return out[:max_items]


def _bridge_merge_with_onchain_activity(
    payload: dict[str, Any],
    intake_evm_address: str,
    extra_watch_addresses: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = dict(payload or {})
    txs = body.get("transactions")
    if not isinstance(txs, list):
        txs = []

    seen_hashes: set[str] = set()
    for row in txs:
        if not isinstance(row, dict):
            continue
        h = str(
            row.get("tx_hash")
            or row.get("txHash")
            or row.get("transaction_hash")
            or row.get("hash")
            or ""
        ).strip().lower()
        if h:
            seen_hashes.add(h)

    watch_addresses: list[str] = []
    for raw in [intake_evm_address, *(extra_watch_addresses or [])]:
        addr = str(raw or "").strip()
        if not _ADDR_RE.match(addr.lower()):
            continue
        canon = _as_checksum_address(addr)
        if canon not in watch_addresses:
            watch_addresses.append(canon)

    fallback_rows: list[dict[str, Any]] = []
    for addr in watch_addresses:
        fallback_rows.extend(_bridge_recent_onchain_inbound(addr))
    fallback_rows.sort(
        key=lambda x: (int(x.get("_sort_block") or 0), int(x.get("_sort_idx") or 0)),
        reverse=True,
    )
    merged_rows: list[dict[str, Any]] = []
    for row in fallback_rows:
        h = str(row.get("tx_hash") or "").strip().lower()
        if h and h in seen_hashes:
            continue
        merged_rows.append(row)
        if h:
            seen_hashes.add(h)

    body["transactions"] = merged_rows + txs
    body["onchain_enriched"] = True
    return body


def _build_erc20_transfer_calldata(to: str, amount_wei: int) -> str:
    """Build ERC20 transfer(address,uint256) calldata. selector: 0xa9059cbb"""
    selector = bytes.fromhex("a9059cbb")
    encoded = _abi_encode(["address", "uint256"], [_as_checksum_address(to), amount_wei])
    return "0x" + (selector + encoded).hex()


def _get_relayer_nonce(address: str, nonce_type: str = "SAFE") -> str:
    """Fetch relayer nonce with a short in-process cache.

    T918-C Plan B: avoid hammering the relayer for the SAFE nonce lookup that
    typically dominates typed_data prep latency. Cache by (address, type) for
    up to 10 seconds. Successful redeem/withdraw flows must call
    ``_invalidate_relayer_nonce`` to drop stale entries before the next
    interactive sign.
    """
    if not address:
        raise RuntimeError("relayer nonce: address required")
    key = (str(address).strip().lower(), str(nonce_type or "SAFE").strip().upper())
    now = _nonce_time.monotonic()
    with _NONCE_CACHE_LOCK:
        cached = _NONCE_CACHE.get(key)
        if cached and (now - cached[0]) < _NONCE_CACHE_TTL:
            return cached[1]
    resp = requests.get(
        f"{_RELAYER_API}/nonce",
        params={"address": address, "type": nonce_type},
        timeout=15,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        raise RuntimeError(
            f"relayer nonce failed status={resp.status_code} body={data}"
        )
    nonce = str((data or {}).get("nonce") or "").strip()
    if nonce == "":
        raise RuntimeError(f"relayer nonce empty body={data}")
    with _NONCE_CACHE_LOCK:
        _NONCE_CACHE[key] = (_nonce_time.monotonic(), nonce)
        if len(_NONCE_CACHE) > 256:
            oldest = sorted(_NONCE_CACHE.items(), key=lambda kv: kv[1][0])[:32]
            for k, _ in oldest:
                _NONCE_CACHE.pop(k, None)
    return nonce


def _invalidate_relayer_nonce(address: str, nonce_type: str = "SAFE") -> None:
    """Drop the cached nonce for ``(address, nonce_type)`` after a successful submit."""
    if not address:
        return
    key = (str(address).strip().lower(), str(nonce_type or "SAFE").strip().upper())
    with _NONCE_CACHE_LOCK:
        _NONCE_CACHE.pop(key, None)


# ─── T795: Counterfactual Safe address derivation & server-side deployment ────


def _derive_pm_safe_address(eoa_address: str) -> str:
    """T795: Compute deterministic Polymarket Gnosis Safe address from EOA.

    Mirrors deriveSafe() from @polymarket/builder-relayer-client/builder/derive.js:
      salt = keccak256(abi.encode(address))   # address left-padded to 32 bytes
      safe = keccak256(0xff || factory || salt || init_code_hash)[12:]
    """
    eoa_clean = eoa_address.lower().replace("0x", "")
    eoa_padded = bytes.fromhex(eoa_clean).rjust(32, b"\x00")
    salt = _keccak256(primitive=eoa_padded)
    factory_bytes = bytes.fromhex(_pm_v2.SAFE_FACTORY.lower().replace("0x", ""))
    init_hash_bytes = bytes.fromhex(
        _pm_v2.SAFE_INIT_CODE_HASH.lower().replace("0x", "")
    )
    create2_input = b"\xff" + factory_bytes + salt + init_hash_bytes
    result = _keccak256(primitive=create2_input)
    return to_checksum_address("0x" + result[12:].hex())


def _is_pm_safe_deployed(safe_address: str) -> bool:
    """T795: Check if a Polymarket Gnosis Safe has been deployed via relayer."""
    try:
        resp = requests.get(
            f"{_RELAYER_API}/deployed",
            params={"address": safe_address},
            timeout=10,
        )
        if resp.status_code < 400:
            return bool(resp.json().get("deployed", False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[T795] is_pm_safe_deployed err=%s", exc)
    return False


def _deploy_pm_safe_for_user(
    embedded_wallet_id: str,
    eoa_address: str,
    auth_keys: list[str] | None = None,
) -> dict[str, Any]:
    """T795: Server-side deploy of Polymarket Gnosis Safe via relayer.

    Signs the CreateProxy EIP-712 message with the user's Privy embedded wallet
    (server signer) and submits a SAFE_CREATE transaction to POST /submit.

    EIP-712 schema from @polymarket/builder-relayer-client/builder/create.js:
      domain: {name: "Polymarket Contract Proxy Factory", chainId, verifyingContract: SafeFactory}
      types:  CreateProxy [{paymentToken:address}, {payment:uint256}, {paymentReceiver:address}]
      values: paymentToken=0x0, payment=0, paymentReceiver=0x0
    """
    eoa = _as_checksum_address(eoa_address)
    safe_addr = _derive_pm_safe_address(eoa)
    factory_addr = _as_checksum_address(_pm_v2.SAFE_FACTORY)

    typed_data: dict[str, Any] = {
        "domain": {
            "name": "Polymarket Contract Proxy Factory",
            "chainId": _CHAIN_ID,
            "verifyingContract": factory_addr,
        },
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "CreateProxy": [
                {"name": "paymentToken", "type": "address"},
                {"name": "payment", "type": "uint256"},
                {"name": "paymentReceiver", "type": "address"},
            ],
        },
        "primaryType": "CreateProxy",
        "message": {
            "paymentToken": _ZERO_ADDR,
            "payment": 0,
            "paymentReceiver": _ZERO_ADDR,
        },
    }

    signature = _privy_sign_safe_typed_data(
        embedded_wallet_id, typed_data, auth_keys=auth_keys
    )

    relayer_payload: dict[str, Any] = {
        "from": eoa,
        "to": factory_addr,
        "proxyWallet": safe_addr,
        "data": "0x",
        "signature": signature,
        "signatureParams": {
            "paymentToken": _ZERO_ADDR,
            "payment": "0",
            "paymentReceiver": _ZERO_ADDR,
        },
        "type": "SAFE_CREATE",
        "metadata": "",
    }

    logger.info(
        "[T795] deploy_pm_safe_for_user eoa=%s safe=%s wallet_id=%s",
        eoa,
        safe_addr,
        embedded_wallet_id,
    )
    return _relayer_submit(relayer_payload)


def _default_signature_params() -> dict[str, str]:
    return {
        "gasPrice": "0",
        "operation": "0",
        "safeTxnGas": "0",
        "baseGas": "0",
        "gasToken": _ZERO_ADDR,
        "refundReceiver": _ZERO_ADDR,
    }


# T463: EIP-712 SafeTx struct-hash digest — byte-for-byte compatible with the
# official @polymarket/builder-relayer-client SDK (`buildSafeTransactionRequest`
# → `createStructHash`). The relayer expects the user's signature to be a
# `personal_sign` over THIS 32-byte digest, with `v += 4` (Safe `eth_sign`
# mode), packed as `r(32) || s(32) || v(1)`. Returning the digest server-side
# avoids any client-side EIP-712 hashing discrepancies and lets the browser
# wallet simply sign raw bytes.
_SAFETX_TYPEHASH = _keccak256(
    b"SafeTx(address to,uint256 value,bytes data,uint8 operation,"
    b"uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,"
    b"address refundReceiver,uint256 nonce)"
)
_DOMAIN_TYPEHASH = _keccak256(
    b"EIP712Domain(uint256 chainId,address verifyingContract)"
)


def _hex_to_bytes(value: Any) -> bytes:
    s = str(value or "").strip()
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    if not s:
        return b""
    if len(s) % 2 != 0:
        s = "0" + s
    return bytes.fromhex(s)


def _compute_safe_struct_hash(
    *,
    chain_id: int,
    safe_addr: str,
    to_addr: str,
    value: str,
    data_hex: str,
    operation: int,
    safe_tx_gas: int,
    base_gas: int,
    gas_price: int,
    gas_token: str,
    refund_receiver: str,
    nonce: int,
) -> str:
    data_hash = _keccak256(_hex_to_bytes(data_hex))
    inner = b"".join(
        [
            _SAFETX_TYPEHASH,
            _abi_encode(["address"], [to_addr]),
            _abi_encode(["uint256"], [int(value)]),
            data_hash,
            _abi_encode(["uint8"], [int(operation)]),
            _abi_encode(["uint256"], [int(safe_tx_gas)]),
            _abi_encode(["uint256"], [int(base_gas)]),
            _abi_encode(["uint256"], [int(gas_price)]),
            _abi_encode(["address"], [gas_token]),
            _abi_encode(["address"], [refund_receiver]),
            _abi_encode(["uint256"], [int(nonce)]),
        ]
    )
    struct_hash_inner = _keccak256(inner)
    domain_separator = _keccak256(
        _DOMAIN_TYPEHASH
        + _abi_encode(["uint256"], [int(chain_id)])
        + _abi_encode(["address"], [safe_addr])
    )
    digest = _keccak256(b"\x19\x01" + domain_separator + struct_hash_inner)
    return "0x" + digest.hex()


def _build_safe_typed_data(
    *,
    to_addr: str,
    data_hex: str,
    proxy_wallet: str,
    nonce: str,
    signature_params: dict[str, Any] | None = None,
    value: str = "0",
) -> dict[str, Any]:
    params = signature_params or _default_signature_params()
    return {
        "types": {
            "EIP712Domain": [
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "SafeTx": [
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
                {"name": "operation", "type": "uint8"},
                {"name": "safeTxGas", "type": "uint256"},
                {"name": "baseGas", "type": "uint256"},
                {"name": "gasPrice", "type": "uint256"},
                {"name": "gasToken", "type": "address"},
                {"name": "refundReceiver", "type": "address"},
                {"name": "nonce", "type": "uint256"},
            ],
        },
        "domain": {
            "chainId": _CHAIN_ID,
            "verifyingContract": proxy_wallet,
        },
        "primaryType": "SafeTx",
        "message": {
            "to": to_addr,
            "value": str(value),
            "data": data_hex,
            "operation": int(str(params.get("operation") or "0")),
            "safeTxGas": int(str(params.get("safeTxnGas") or "0")),
            "baseGas": int(str(params.get("baseGas") or "0")),
            "gasPrice": int(str(params.get("gasPrice") or "0")),
            "gasToken": str(params.get("gasToken") or _ZERO_ADDR),
            "refundReceiver": str(params.get("refundReceiver") or _ZERO_ADDR),
            "nonce": int(str(nonce)),
        },
    }


def _sender_from_private_key(private_key: str) -> str:
    from eth_account import Account  # noqa: PLC0415

    return str(Account.from_key(private_key).address)


def _polygon_rpcs() -> list[str]:
    """Resolve Polygon RPC list from config first, then env, then built-in fallbacks."""
    merged: list[str] = []
    seen: set[str] = set()

    def _push(url: str) -> None:
        u = str(url or "").strip()
        if not u or not u.startswith("http"):
            return
        # Unauthenticated Ankr public URL is rejected and causes noisy failures.
        # Only keep Ankr if explicit API key/token is embedded in URL.
        if "rpc.ankr.com/polygon" in u and "/polygon/" not in u and "api_key=" not in u:
            return
        if u in seen:
            return
        seen.add(u)
        merged.append(u)

    try:
        cfg = cfg_get("chain.polygon_rpcs")
        if isinstance(cfg, list):
            for item in cfg:
                _push(str(item))
    except Exception:
        pass

    for env_key in (
        "POLYGON_RPC_URL",
        "POLYGON_RPC_HTTP_URL",
        "POLYGON_RPC",
        "ALCHEMY_POLYGON_RPC_URL",
    ):
        _push(os.environ.get(env_key, ""))

    ankr_key = str(os.environ.get("ANKR_API_KEY", "")).strip()
    if ankr_key:
        _push(f"https://rpc.ankr.com/polygon/{ankr_key}")

    for item in _DEFAULT_POLYGON_RPCS:
        _push(item)

    return merged


def _polygon_write_rpcs() -> list[str]:
    """Writable RPC candidates for signed tx broadcast.

    Keep this conservative: exclude known read-only or frequently forbidden public gateways.
    """
    blocked_write_hosts = (
        "rpc-mainnet.maticvigil.com",
        "polygon-mainnet.public.blastapi.io",
        "polygon-rpc.com",
        "polygon.llamarpc.com",
    )
    out: list[str] = []
    for rpc in _polygon_rpcs():
        if any(host in rpc for host in blocked_write_hosts):
            continue
        out.append(rpc)
    return out


def _polygon_rpc_call(method: str, params: list, timeout: int = 10) -> Any:
    """发起 JSON-RPC 请求，自动轮换 RPC 节点。"""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_error = ""
    rpcs = (
        _polygon_write_rpcs() if method == "eth_sendRawTransaction" else _polygon_rpcs()
    )
    for rpc in rpcs:
        try:
            resp = requests.post(rpc, json=payload, timeout=timeout)
            if resp.status_code >= 400:
                last_error = f"{rpc} http={resp.status_code}"
                logger.debug(
                    "RPC %s status=%s body=%s", rpc, resp.status_code, resp.text[:180]
                )
                continue
            data = resp.json()
            if "error" in data:
                last_error = f"{rpc} rpc_error={data['error']}"
                logger.debug("RPC %s error: %s", rpc, data["error"])
                continue
            return data.get("result")
        except Exception as exc:
            last_error = f"{rpc} exc={exc}"
            logger.debug("RPC %s failed: %s", rpc, exc)
    if not rpcs:
        raise RuntimeError("All Polygon RPC nodes failed (no rpc configured)")
    raise RuntimeError(
        "All Polygon RPC nodes failed" + (f": {last_error}" if last_error else "")
    )


def _hex_to_int(h: str) -> int:
    return int(h, 16) if isinstance(h, str) and h.startswith("0x") else int(h or "0", 0)


def _get_nonce(address: str) -> int:
    result = _polygon_rpc_call("eth_getTransactionCount", [address, "latest"])
    return _hex_to_int(result)


def _get_native_balance_wei(address: str) -> int:
    result = _polygon_rpc_call("eth_getBalance", [address, "latest"])
    return _hex_to_int(result)


def _wei_to_pol(value_wei: int) -> float:
    return float(value_wei) / 1_000_000_000_000_000_000


def _get_gas_price() -> int:
    """返回 gas price（wei），略高于网络基础费以确保打包。"""
    result = _polygon_rpc_call("eth_gasPrice", [])
    base = _hex_to_int(result)
    # 加 20% 溢价
    return int(base * 1.2)


def _estimate_gas(tx: dict, fallback_gas: int = 300_000) -> int:
    try:
        result = _polygon_rpc_call("eth_estimateGas", [tx])
        return int(_hex_to_int(result) * 1.3)
    except Exception:
        return int(fallback_gas)  # 保底 gas limit


def _as_checksum_address(addr: str) -> str:
    """Normalize an EVM address to checksum format."""
    return to_checksum_address(str(addr).strip())


def _sign_and_send(
    private_key: str,
    to: str,
    data_hex: str,
    value: int = 0,
    *,
    fallback_gas: int = 300_000,
) -> str:
    """
    使用 eth_account 签名并广播一笔 Polygon 交易。
    返回 tx_hash（0x... hex string）。
    """
    from eth_account import Account  # noqa: PLC0415
    from eth_account.signers.local import LocalAccount  # noqa: PLC0415

    acct: LocalAccount = Account.from_key(private_key)
    sender = acct.address
    to_addr = _as_checksum_address(to)

    nonce = _get_nonce(sender)
    gas_price = _get_gas_price()

    tx_params = {
        "to": to_addr,
        "value": value,
        "data": data_hex,
        "chainId": _CHAIN_ID,
        "nonce": nonce,
        "gasPrice": gas_price,
    }
    gas_limit = _estimate_gas(
        {"from": sender, "to": to_addr, "data": data_hex, "value": hex(value)},
        fallback_gas=fallback_gas,
    )
    tx_params["gas"] = gas_limit

    # Preflight sender gas capacity to avoid opaque downstream RPC errors.
    balance_wei = _get_native_balance_wei(sender)
    required_wei = int(gas_limit * gas_price * 1.10)  # +10% safety buffer
    if balance_wei < required_wei:
        raise GasBalanceError(
            sender=sender,
            balance_wei=balance_wei,
            required_wei=required_wei,
            gas_price_wei=gas_price,
            gas_limit=gas_limit,
        )

    signed = acct.sign_transaction(tx_params)
    raw_hex = "0x" + signed.raw_transaction.hex()

    tx_hash = _polygon_rpc_call("eth_sendRawTransaction", [raw_hex])
    if not tx_hash:
        raise RuntimeError("eth_sendRawTransaction returned empty result")
    return str(tx_hash)


# ── ABI 编码工具 ─────────────────────────────────────────────────────────


def _keccak4(text: str) -> bytes:
    """计算函数选择器（keccak256 前 4 字节）。"""
    from eth_hash.auto import keccak  # noqa: PLC0415

    return keccak(text.encode("utf-8"))[:4]


# ── T549: per-condition collateral probe ───────────────────────────────────
#
# Polymarket's pUSD cutover (2026-04-28) introduced two parallel collateral
# tokens. Positions opened pre-cutover are pinned to USDC.e; positions opened
# post-cutover are pinned to pUSD. The CTF positionId is a function of the
# collateral, so calling redeemPositions with the wrong collateral targets a
# zero-balance positionId and the relayer rejects with `bad request`.
#
# The fix probes CTF on-chain for both candidates and uses the one with a
# non-zero ERC1155 balance for the user's Safe. Falls back to the v2-flag
# default when both probes fail (defensive — preserves prior behaviour).


def _eth_call_polygon(to: str, data: str) -> str | None:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1,
    }
    for rpc in _polygon_rpcs():
        try:
            r = requests.post(rpc, json=payload, timeout=6)
            if not r.ok:
                continue
            body = r.json() or {}
            if body.get("error"):
                continue
            res = body.get("result")
            if isinstance(res, str) and res.startswith("0x"):
                return res
        except Exception:
            continue
    return None


def _ctf_get_collection_id(condition_id_hex: str, index_set: int) -> str | None:
    cid = str(condition_id_hex or "").replace("0x", "").lower()
    if len(cid) != 64:
        return None
    selector = _keccak4("getCollectionId(bytes32,bytes32,uint256)").hex()
    data = "0x" + selector + ("00" * 32) + cid + format(int(index_set), "064x")
    res = _eth_call_polygon(_CTF_CONTRACT, data)
    return res if res and len(res) == 66 else None


def _ctf_get_position_id(collateral_token: str, collection_id_hex: str) -> int | None:
    addr = str(collateral_token or "").lower().replace("0x", "")
    coll = str(collection_id_hex or "").replace("0x", "")
    if len(addr) != 40 or len(coll) != 64:
        return None
    selector = _keccak4("getPositionId(address,bytes32)").hex()
    data = "0x" + selector + ("0" * 24) + addr + coll
    res = _eth_call_polygon(_CTF_CONTRACT, data)
    if res and res.startswith("0x"):
        try:
            return int(res, 16)
        except ValueError:
            return None
    return None


def _ctf_balance_of_1155(account: str, position_id: int) -> int:
    addr = str(account or "").lower().replace("0x", "")
    if len(addr) != 40:
        return 0
    selector = _keccak4("balanceOf(address,uint256)").hex()
    data = "0x" + selector + ("0" * 24) + addr + format(int(position_id), "064x")
    res = _eth_call_polygon(_CTF_CONTRACT, data)
    if res and res.startswith("0x"):
        try:
            return int(res, 16)
        except ValueError:
            return 0
    return 0


def _resolve_redeem_collateral(
    safe_address: str,
    condition_id_hex: str,
    index_set: int,
) -> tuple[str, str, int]:
    """Probe CTF for the collateral token whose positionId actually has balance.

    Returns (token_address, label, balance). Falls back to the active default
    collateral with balance=0 when all probes fail.
    """
    candidates: list[tuple[str, str]] = []
    try:
        candidates.append((_PUSD_TOKEN, _pm_v2.PUSD_SYMBOL))
    except Exception:
        candidates.append((_PUSD_TOKEN, "pUSD"))
    candidates.append((_USDC_E_TOKEN, "USDC.e"))

    coll_id = _ctf_get_collection_id(condition_id_hex, index_set)
    if not coll_id:
        token, label = _active_collateral()
        return token, label, 0
    for token, label in candidates:
        try:
            pos_id = _ctf_get_position_id(token, coll_id)
            if pos_id is None:
                continue
            bal = _ctf_balance_of_1155(safe_address, pos_id)
            if bal > 0:
                logger.info(
                    "[T549] redeem collateral resolved=%s balance=%s positionId=%s safe=%s",
                    label,
                    bal,
                    pos_id,
                    safe_address,
                )
                return token, label, bal
        except Exception as exc:
            logger.debug("[T549] collateral probe failed for %s: %s", label, exc)
            continue
    token, label = _active_collateral()
    logger.warning(
        "[T549] no positive CTF balance found for any collateral; falling back to active=%s safe=%s cid=%s indexSet=%s",
        label,
        safe_address,
        condition_id_hex,
        index_set,
    )
    return token, label, 0


def _build_erc20_transfer_calldata(to: str, amount_wei: int) -> str:
    """
    构建 ERC20 transfer(address, uint256) calldata。
    selector: 0xa9059cbb
    """
    from eth_abi import encode  # noqa: PLC0415

    selector = bytes.fromhex("a9059cbb")
    encoded = encode(["address", "uint256"], [to, amount_wei])
    return "0x" + (selector + encoded).hex()


def _build_deposit_wallet_batch_typed_data(
    deposit_wallet: str,
    nonce: str | int,
    deadline: int,
    calls: list[dict],
) -> dict:
    """Build EIP-712 typed data for a Deposit Wallet WALLET batch.

    Reference: https://docs.polymarket.com/trading/deposit-wallet-migration
    The domain uses the deposit wallet address as verifyingContract (not factory).
    Signature type: normal 65-byte EIP-712 (NOT POLY_1271).
    """
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Call": [
                {"name": "target", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
            ],
            "Batch": [
                {"name": "wallet", "type": "address"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "calls", "type": "Call[]"},
            ],
        },
        "primaryType": "Batch",
        "domain": {
            "name": "DepositWallet",
            "version": "1",
            "chainId": int(_pm_v2.CHAIN_ID_POLYGON),
            "verifyingContract": _as_checksum_address(deposit_wallet),
        },
        "message": {
            "wallet": _as_checksum_address(deposit_wallet),
            "nonce": int(nonce),
            "deadline": int(deadline),
            "calls": [
                {
                    "target": _as_checksum_address(str(c.get("target") or _ZERO_ADDR)),
                    "value": int(str(c.get("value") or "0")),
                    "data": str(c.get("data") or "0x"),
                }
                for c in (calls or [])
            ],
        },
    }


def _build_deposit_wallet_typed_data_skeleton(
    user_wallet: str,
    to_addr: str,
    data_hex: str,
    *,
    value: str = "0",
) -> dict[str, Any]:
    """Build Deposit Wallet WALLET batch typed-data skeleton for client signing.

    For new API users whose Polymarket wallet is a deposit wallet (signature_type=3 / POLY_1271).
    The withdrawal flow submits a WALLET batch to the relayer instead of a SAFE tx.

    Returned dict matches the shape of _build_typed_data_skeleton for drop-in routing.
    """
    binding = _get_user_signer_binding(user_wallet, require_wallets=False)
    if not binding:
        raise RuntimeError("missing user signer binding")
    main_wallet = str(binding.get("main_wallet") or user_wallet).strip()
    # For deposit wallet users, proxy_wallet IS the deposit wallet address
    deposit_wallet = str(binding.get("proxy_wallet") or "").strip()
    if not _ADDR_RE.match(deposit_wallet.lower()):
        raise RuntimeError("deposit wallet address missing or invalid for user")
    if not _ADDR_RE.match(main_wallet.lower()):
        raise RuntimeError("missing main_wallet (EOA) for deposit wallet user")

    # Nonce type=WALLET, keyed by owner EOA (not deposit wallet address)
    nonce = _get_relayer_nonce(main_wallet, "WALLET")
    deadline = int(time.time()) + 600  # 4 minutes ahead

    calls = [
        {
            "target": _as_checksum_address(to_addr),
            "value": str(value),
            "data": data_hex,
        }
    ]
    typed_data = _build_deposit_wallet_batch_typed_data(
        deposit_wallet=deposit_wallet,
        nonce=nonce,
        deadline=deadline,
        calls=calls,
    )

    # Relayer-v2 enforces `from == auth address` on every payload.
    # Ownership of the deposit wallet is proven via the EIP-712 Batch signature.
    skeleton = {
        "type": "WALLET",
        "from": _resolve_relayer_from(main_wallet),
        "to": _pm_v2.DEPOSIT_WALLET_FACTORY,
        "nonce": str(nonce),
        "depositWalletParams": {
            "depositWallet": _as_checksum_address(deposit_wallet),
            "deadline": str(deadline),
            "calls": calls,
        },
        "metadata": "",
    }

    return {
        "typed_data": typed_data,
        "struct_hash": "",  # client computes from typed_data for personal_sign
        "signing_method": "eip712_batch",
        "payload_skeleton": skeleton,
        "signing_address": _as_checksum_address(main_wallet),
        "mode": _resolve_signing_mode(binding),
        "proxy_wallet": _as_checksum_address(deposit_wallet),
        "nonce": str(nonce),
        "embedded_wallet_address": str(
            binding.get("privy_embedded_wallet_address") or ""
        ),
        "wallet_type": "deposit_wallet",
    }


def _build_redeem_positions_calldata(
    collateral_token: str,
    condition_id_hex: str,  # 0x prefixed 32-byte hex
    index_sets: list[int],
) -> str:
    """
    构建 ConditionalTokens.redeemPositions(address,bytes32,bytes32,uint256[]) calldata。

    parentCollectionId 固定为 0x00...00（顶层 condition，Polymarket 约定）。
    """
    from eth_abi import encode  # noqa: PLC0415

    # redeemPositions(address,bytes32,bytes32,uint256[])
    selector = _keccak4("redeemPositions(address,bytes32,bytes32,uint256[])")

    # condition_id 转 bytes32
    cid_clean = condition_id_hex.replace("0x", "")
    if len(cid_clean) != 64:
        raise ValueError(f"condition_id must be 32 bytes hex, got len={len(cid_clean)}")
    cid_bytes = bytes.fromhex(cid_clean)

    parent_collection_id = b"\x00" * 32

    encoded = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [collateral_token, parent_collection_id, cid_bytes, index_sets],
    )
    return "0x" + (selector + encoded).hex()


def _build_neg_risk_redeem_calldata(
    condition_id_hex: str, outcome_index: int, size: float
) -> str:
    """Build NegRiskAdapter.redeemPositions(bytes32,uint256[]) calldata."""
    from eth_abi import encode  # noqa: PLC0415

    cid_clean = str(condition_id_hex or "").replace("0x", "")
    if len(cid_clean) != 64:
        raise ValueError(f"condition_id must be 32 bytes hex, got len={len(cid_clean)}")
    if outcome_index not in (0, 1):
        raise ValueError(f"outcome_index must be 0 or 1, got {outcome_index}")

    selector = _keccak4("redeemPositions(bytes32,uint256[])")
    token_amount = int(max(0.0, float(size or 0.0)) * 1_000_000)
    amounts = [0, 0]
    amounts[int(outcome_index)] = token_amount
    encoded = encode(["bytes32", "uint256[]"], [bytes.fromhex(cid_clean), amounts])
    return "0x" + (selector + encoded).hex()


# ── Gamma API helpers ────────────────────────────────────────────────────


def _resolve_condition_id_from_market(market_id: str) -> str | None:
    """从 Gamma API 根据 market_id 查询 conditionId。"""
    if not market_id:
        return None
    try:
        r = requests.get(
            f"{_GAMMA_API}/markets/{market_id}",
            timeout=8,
            headers={"Accept": "application/json"},
        )
        if r.status_code == 200:
            data = r.json()
            cid = str(data.get("conditionId") or "").strip()
            if cid and cid.startswith("0x"):
                return cid
        # fallback: search by marketId
        r2 = requests.get(
            f"{_GAMMA_API}/markets",
            params={"id": market_id},
            timeout=8,
            headers={"Accept": "application/json"},
        )
        if r2.status_code == 200:
            items = r2.json()
            if isinstance(items, list) and items:
                cid = str(items[0].get("conditionId") or "").strip()
                if cid and cid.startswith("0x"):
                    return cid
    except Exception as exc:
        logger.debug("condition_id resolve failed for %s: %s", market_id, exc)
    return None


def _lookup_bet(bet_id: str) -> dict | None:
    """从 paper_bets 中查询投注记录。"""
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT bet_id, market_id, outcome, is_win, size_usdc, matched_shares "
                "FROM paper_bets WHERE bet_id = %s LIMIT 1",
                (bet_id,),
            ).fetchone()
            if row:
                return dict(row)
    except Exception as exc:
        logger.warning("bet lookup failed for %s: %s", bet_id, exc)
    return None


def _insert_vault_tx(
    tx_type: str,
    tx_hash: str,
    from_addr: str,
    to_addr: str,
    token: str,
    amount: float,
    status: str = "submitted",
    extra: dict | None = None,
    user_id: str = "system",
) -> int:
    """将 vault 交易记录写入 DB，返回 row id。"""
    _ensure_vault_table()
    now = int(time.time())
    try:
        with db_connect() as conn:
            cur = conn.execute(
                """INSERT INTO vault_transactions
                   (user_id, tx_type, tx_hash, from_addr, to_addr, token, amount, status, extra_json, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    user_id,
                    tx_type,
                    tx_hash,
                    from_addr,
                    to_addr,
                    token,
                    amount,
                    status,
                    json.dumps(extra or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
            return cur.lastrowid or 0
    except Exception as exc:
        logger.warning("vault tx insert failed: %s", exc)
        return 0


def _build_tx_error_response(
    exc: Exception, context: str
) -> tuple[dict[str, Any], int]:
    """Convert tx errors to user-facing structured JSON payload."""
    if isinstance(exc, GasBalanceError):
        needed_pol = _wei_to_pol(exc.required_wei)
        have_pol = _wei_to_pol(exc.balance_wei)
        shortfall_pol = max(0.0, needed_pol - have_pol)
        return {
            "error": "INSUFFICIENT_GAS_BALANCE",
            "message": (
                "Insufficient POL balance for gas: "
                f"have {have_pol:.6f}, need at least {needed_pol:.6f}. "
                f"Top up POL to sender {exc.sender}"
            ),
            "detail": {
                "sender": exc.sender,
                "have_pol": round(have_pol, 6),
                "required_pol": round(needed_pol, 6),
                "shortfall_pol": round(shortfall_pol, 6),
                "gas_limit": exc.gas_limit,
                "gas_price_wei": exc.gas_price_wei,
                "context": context,
            },
        }, 422

    msg = str(exc)
    lower_msg = msg.lower()
    if "http=403" in lower_msg or "unauthorized" in lower_msg:
        return {
            "error": "RPC_AUTH_FAILED",
            "message": "Polygon RPC authentication failed on one or more nodes",
            "detail": {"context": context, "raw": msg},
        }, 502

    if "insufficient funds for gas" in lower_msg:
        return {
            "error": "INSUFFICIENT_GAS_BALANCE",
            "message": "Wallet balance is insufficient to cover Polygon gas",
            "detail": {"context": context, "raw": msg},
        }, 422

    if "relayer submit failed status=401" in lower_msg:
        return {
            "error": "RELAYER_AUTH_FAILED",
            "message": "Relayer authentication failed. Check RELAYER_API_KEY / RELAYER_API_KEY_ADDRESS",
            "detail": {"context": context, "raw": msg},
        }, 502

    if "relayer submit failed status=400" in lower_msg:
        # T538/T539 — extract the relayer's structured body so the user
        # actually sees the upstream reason (Invalid signature, Invalid
        # nonce, Insufficient gas, Safe not deployed, ...) instead of an
        # opaque "signature mismatch" string. The raw msg ends with
        # `body={...}` per `_relayer_submit`.
        relayer_body = ""
        try:
            idx = msg.find("body=")
            if idx >= 0:
                relayer_body = msg[idx + 5 :].strip()
        except Exception:
            relayer_body = ""
        # Try to surface a one-line human reason if the body is JSON-ish.
        relayer_reason = ""
        try:
            if relayer_body and relayer_body.startswith("{"):
                import ast as _ast  # noqa: PLC0415

                parsed = _ast.literal_eval(relayer_body)
                if isinstance(parsed, dict):
                    relayer_reason = str(
                        parsed.get("error")
                        or parsed.get("message")
                        or parsed.get("detail")
                        or ""
                    )
        except Exception:
            relayer_reason = ""
        human_msg = (
            "Relayer rejected the transaction: " + relayer_reason
            if relayer_reason
            else "Relayer transaction payload invalid or signature mismatch"
        )
        return {
            "error": "RELAYER_PAYLOAD_INVALID",
            "message": human_msg,
            "detail": {
                "context": context,
                "raw": msg,
                "relayer_body": relayer_body,
                "relayer_reason": relayer_reason,
            },
        }, 422

    if "bridge withdraw failed" in lower_msg:
        return {
            "error": "BRIDGE_WITHDRAW_FAILED",
            "message": "Bridge withdrawal request failed",
            "detail": {"context": context, "raw": msg},
        }, 502

    if "bridge withdraw missing target address" in lower_msg:
        return {
            "error": "BRIDGE_TARGET_ADDRESS_MISSING",
            "message": "Bridge did not return a usable destination EVM address for withdrawal",
            "detail": {"context": context, "raw": msg},
        }, 502

    if "no valid authorization keys or user signing keys available" in lower_msg:
        return {
            "error": "PRIVY_SIGNER_AUTH_UNAVAILABLE",
            "message": (
                "Privy signer authorization is not available for this wallet. "
                "Ensure the configured authorization key is valid in the same Privy app and "
                "the wallet has the configured additional signer/session signer."
            ),
            "detail": {"context": context, "raw": msg},
        }, 412

    if (
        "privy wallet rpc failed status=401" in lower_msg
        or "privy wallet patch failed status=401" in lower_msg
    ):
        return {
            "error": "PRIVY_SIGNER_AUTH_FAILED",
            "message": "Privy signer authorization failed. Verify PRIVY_AUTHORIZATION_PRIVATE_KEY and signer configuration.",
            "detail": {"context": context, "raw": msg},
        }, 412

    if (
        "privy wallet rpc failed after credential retries" in lower_msg
        or "privy wallet patch failed after credential retries" in lower_msg
    ):
        return {
            "error": "PRIVY_SIGNER_AUTH_FAILED",
            "message": "Privy signer authorization failed after key/secret retries. Verify all configured PRIVY auth keys and app secrets belong to the same Privy app as signer and wallet.",
            "detail": {"context": context, "raw": msg},
        }, 412

    return {
        "error": "TX_BROADCAST_FAILED",
        # T886: surface a short summary of the raw exception so users (and support)
        # have something actionable instead of the opaque "Service is temporarily
        # unavailable" front-end fallback.
        "message": (
            "Transaction broadcast failed: " + (msg[:200] if msg else "unknown error")
        ),
        "raw_error": msg,
        "detail": {"context": context, "raw": msg},
    }, 502


def _should_fallback_to_legacy_from_official(exc: Exception) -> bool:
    # Legacy fallback is FORBIDDEN per V2 architecture rules.
    # This function is kept only as a name reference to avoid NameError;
    # it always returns False.
    return False


def _build_signing_context(
    private_key: str, relayer_addr: str, key_name_used: str
) -> dict[str, Any]:
    sender = _sender_from_private_key(private_key)
    relayer = str(relayer_addr or "").strip()
    relayer_lower = relayer.lower() if relayer else ""
    sender_lower = sender.lower()
    mismatch = bool(relayer and relayer_lower != sender_lower)
    return {
        "sender_address": sender,
        "relayer_address": relayer,
        "signing_key": key_name_used,
        "sender_relayer_mismatch": mismatch,
    }


def _server_side_signer_required_response(
    context: str, user_wallet: str
) -> JSONResponse:
    expected_funder = _get_user_funder_address(user_wallet)
    return JSONResponse(
        {
            "error": "SERVER_SIDE_SIGNER_REQUIRED",
            "message": (
                "Server-side signer provisioning is not configured for this user wallet. "
                "Backend-only relayer execution cannot proceed until the wallet is enrolled "
                "with a Privy additional signer/session signer."
            ),
            "detail": {
                "context": context,
                "mode": "official_relayer",
                "user_wallet": user_wallet,
                "expected_funder": expected_funder,
                "missing": [
                    "privy additional_signer or session signer enrollment",
                    "server-side signer authorization for the user wallet",
                ],
                "note": "Client-side business transaction signing has been disabled; redeem and withdraw now require backend signer provisioning.",
            },
        },
        status_code=412,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/api/pm/redeem")
async def api_pm_redeem(request: Request):
    """
    赎回已结算的获胜持仓。

    Body JSON:
      bet_id       str   必填（paper_bets.bet_id）
      condition_id str   可选（0x prefixed bytes32，优先从 body 取，缺失时从 DB+Gamma 解析）
      outcome      str   可选（"YES" / "NO"，缺失时从 DB 取）
    """
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    bet_id = str(body.get("bet_id") or "").strip()
    if not bet_id:
        return JSONResponse({"error": "bet_id required"}, status_code=400)

    caller_wallet = str(identity.get("wallet_address") or "system")

    # 从 DB 取投注记录（系统投注）
    bet = _lookup_bet(bet_id)
    outcome = (
        str(body.get("outcome") or (bet or {}).get("outcome") or "YES").upper().strip()
    )

    # 确定 condition_id（优先请求体，兼容 manual 持仓）
    condition_id = str(body.get("condition_id") or "").strip()
    if not condition_id and bet:
        market_id = str(bet.get("market_id") or "").strip()
        condition_id = _resolve_condition_id_from_market(market_id) or ""
    if not condition_id and not bet:
        market_id = str(body.get("market_id") or "").strip()
        if market_id:
            condition_id = _resolve_condition_id_from_market(market_id) or ""

    if not bet and not condition_id:
        return JSONResponse(
            {
                "error": "Bet not found in system records; condition_id required for manual redeem"
            },
            status_code=422,
        )
    if not condition_id or not condition_id.startswith("0x"):
        return JSONResponse(
            {
                "error": "Cannot resolve condition_id for this bet. "
                "Pass condition_id explicitly in the request body."
            },
            status_code=422,
        )

    negative_risk = bool(body.get("negative_risk") or body.get("negativeRisk") or False)
    try:
        outcome_index = int(
            body.get("outcome_index")
            if body.get("outcome_index") is not None
            else body.get("outcomeIndex")
        )
    except (TypeError, ValueError):
        outcome_index = 0 if outcome == "YES" else 1

    # indexSets: YES=1 (bit0), NO=2 (bit1)
    index_sets = [1] if outcome_index == 0 else [2]
    redeem_to_addr = _CTF_CONTRACT

    try:
        if negative_risk:
            calldata = _build_neg_risk_redeem_calldata(
                condition_id_hex=condition_id,
                outcome_index=outcome_index,
                size=float(
                    body.get("size")
                    or body.get("size_usdc")
                    or (bet or {}).get("matched_shares")
                    or 0
                ),
            )
            redeem_to_addr = _NEG_RISK_ADAPTER
        else:
            # T471 (post-2026-04-28 cutover): collateral is now pUSD, which is
            # what the CTF Exchange v2 settles into. Pre-cutover (PM_USE_V2=0)
            # we fall back to USDC.e for incident-response continuity.
            #
            # T549: legacy positions opened pre-cutover still hold ERC1155
            # balance under USDC.e-derived positionIds, so a global flag is not
            # enough — we probe CTF.balanceOf for both candidates and pick the
            # one with non-zero balance for the user's Safe. Failed probes fall
            # back to the v2-flag default (preserves prior behaviour).
            try:
                _binding_for_collateral = _get_user_signer_binding(caller_wallet) or {}
            except Exception:
                _binding_for_collateral = {}
            _safe_for_probe = str(
                _binding_for_collateral.get("proxy_wallet") or ""
            ).strip()
            if _safe_for_probe:
                collateral_addr, _collateral_label, _coll_bal = (
                    _resolve_redeem_collateral(
                        _safe_for_probe,
                        condition_id,
                        index_sets[0],
                    )
                )
            else:
                collateral_addr, _collateral_label = _active_collateral()
            calldata = _build_redeem_positions_calldata(
                collateral_token=collateral_addr,
                condition_id_hex=condition_id,
                index_sets=index_sets,
            )
    except Exception as exc:
        logger.error("build redeem calldata failed: %s", exc)
        return JSONResponse({"error": f"Calldata build error: {exc}"}, status_code=500)

    if _official_tx_mode():
        relayer_payload = _extract_relayer_payload(body)
        # T538/T539 — track whether this is an internally-built (server-side
        # signed) payload so we can auto-retry on stale-nonce/signature 400s
        # by rebuilding (which re-fetches nonce + re-signs). Client-signed
        # payloads cannot be retried server-side because we don't hold the key.
        server_side_built = False
        if not relayer_payload:
            binding = _get_user_signer_binding(caller_wallet)
            _log_signer_debug("redeem", caller_wallet, binding or {})
            # T914 — deposit_wallet users sign in the browser via the Privy
            # embedded wallet (EIP-712 batch); they do NOT need a backend
            # `privy_embedded_wallet_id` (Privy wallet ID for server-side RPC
            # signing). Only enforce the wallet-id requirement for the legacy
            # SAFE/server-signing path. Without this relaxation, every
            # deposit_wallet redeem returned 412 SERVER_SIDE_SIGNER_REQUIRED.
            wallet_type_peek = (
                str((binding or {}).get("wallet_type") or "").strip().lower()
            )
            if not binding:
                return _server_side_signer_required_response("redeem", caller_wallet)
            if (
                wallet_type_peek != "deposit_wallet"
                and not binding.get("privy_embedded_wallet_id")
            ):
                return _server_side_signer_required_response("redeem", caller_wallet)
            signing_mode = _resolve_signing_mode(binding)
            # T914 — deposit_wallet users always sign client-side.
            if wallet_type_peek == "deposit_wallet":
                signing_mode = "client"
            if signing_mode == "client":
                # External-wallet user: cannot sign server-side. Return typed_data
                # so the frontend can collect a signature from the connected wallet.
                try:
                    prep = _build_typed_data_skeleton(
                        caller_wallet, redeem_to_addr, calldata
                    )
                except Exception as exc:
                    logger.error("redeem typed-data prepare failed: %s", exc)
                    payload, status = _build_tx_error_response(
                        exc, "redeem_prepare_typed_data"
                    )
                    return JSONResponse(payload, status_code=status)
                return JSONResponse(
                    {
                        "success": False,
                        "requires_client_sign": True,
                        "mode": "client_sign_required",
                        "signing_address": prep["signing_address"],
                        "signing_method": prep["signing_method"],
                        "struct_hash": prep["struct_hash"],
                        "proxy_wallet": prep["proxy_wallet"],
                        "nonce": prep["nonce"],
                        "typed_data": prep["typed_data"],
                        "payload_skeleton": prep["payload_skeleton"],
                        "context": "redeem",
                        "condition_id": condition_id,
                        "outcome": outcome,
                        "outcome_index": outcome_index,
                        "negative_risk": negative_risk,
                        "index_sets": index_sets,
                    }
                )
            try:
                relayer_payload = _build_server_side_relayer_payload(
                    caller_wallet,
                    redeem_to_addr,
                    calldata,
                    user_jwt=_extract_request_user_jwts(request),
                )
                server_side_built = True
            except Exception as exc:
                logger.error("redeem server-side payload build failed: %s", exc)
                payload, status = _build_tx_error_response(
                    exc, "redeem_server_side_sign"
                )
                return JSONResponse(payload, status_code=status)
        if relayer_payload:
            try:
                if server_side_built:
                    _captured_user_jwt = _extract_request_user_jwts(request)

                    def _redeem_rebuild():
                        return _build_server_side_relayer_payload(
                            caller_wallet,
                            redeem_to_addr,
                            calldata,
                            user_jwt=_captured_user_jwt,
                        )

                    # First attempt uses the already-built payload; on stale
                    # nonce / sig error we rebuild + re-sign.
                    try:
                        relayer_result = _relayer_submit(relayer_payload)
                    except Exception as first_exc:
                        if not _is_stale_nonce_or_sig_error(first_exc):
                            raise
                        logger.warning(
                            "redeem relayer stale-nonce/sig retry: %s", first_exc
                        )
                        relayer_payload = _redeem_rebuild()
                        relayer_result = _relayer_submit(relayer_payload)
                else:
                    relayer_result = _relayer_submit(relayer_payload)
            except Exception as exc:
                logger.error("redeem relayer submit failed: %s", exc)
                payload, status = _build_tx_error_response(exc, "redeem_official")
                return JSONResponse(payload, status_code=status)
            else:
                tx_hash = str(relayer_result.get("transactionHash") or "")
                tx_id = str(relayer_result.get("transactionID") or "")
                state = str(relayer_result.get("state") or "")
                _insert_vault_tx(
                    tx_type="redeem",
                    tx_hash=tx_hash,
                    from_addr=str(relayer_payload.get("from") or ""),
                    to_addr=str(relayer_payload.get("to") or redeem_to_addr),
                    token="CTF",
                    amount=float(
                        (bet or {}).get("size_usdc") or body.get("size_usdc") or 0
                    ),
                    extra={
                        "mode": "official_relayer",
                        "transaction_id": tx_id,
                        "state": state,
                        "bet_id": bet_id,
                        "condition_id": condition_id,
                        "outcome": outcome,
                        "outcome_index": outcome_index,
                        "negative_risk": negative_risk,
                        "index_sets": index_sets,
                    },
                    user_id=caller_wallet,
                )
                return JSONResponse(
                    {
                        "success": True,
                        "mode": "official_relayer",
                        "transaction_id": tx_id,
                        "tx_hash": tx_hash,
                        "state": state,
                        "relayer": relayer_result,
                        "relayer_tx": relayer_payload,
                        "polygon_scan": (
                            f"https://polygonscan.com/tx/{tx_hash}" if tx_hash else ""
                        ),
                        "condition_id": condition_id,
                        "outcome": outcome,
                        "outcome_index": outcome_index,
                        "negative_risk": negative_risk,
                        "index_sets": index_sets,
                    }
                )

    # 读取私钥并签名广播
    try:
        private_key, relayer_addr, key_name_used = _get_secrets()
    except EnvironmentError as exc:
        return JSONResponse(
            {
                "error": "SIGNING_KEY_NOT_CONFIGURED",
                "message": str(exc),
                "detail": {"context": "redeem"},
            },
            status_code=412,
        )

    sign_ctx = _build_signing_context(private_key, relayer_addr, key_name_used)

    try:
        tx_hash = _sign_and_send(
            private_key=private_key,
            to=redeem_to_addr,
            data_hex=calldata,
            fallback_gas=220_000,
        )
    except Exception as exc:
        logger.error("redeem broadcast failed: %s", exc)
        payload, status = _build_tx_error_response(exc, "redeem")
        return JSONResponse(payload, status_code=status)

    # 记录到 vault
    from eth_account import Account as _Acct  # noqa: PLC0415

    sender_addr = _Acct.from_key(private_key).address
    _insert_vault_tx(
        tx_type="redeem",
        tx_hash=tx_hash,
        from_addr=sender_addr,
        to_addr=redeem_to_addr,
        token="CTF",
        amount=float((bet or {}).get("size_usdc") or body.get("size_usdc") or 0),
        extra={
            "bet_id": bet_id,
            "condition_id": condition_id,
            "outcome": outcome,
            "outcome_index": outcome_index,
            "negative_risk": negative_risk,
            "index_sets": index_sets,
        },
        user_id=caller_wallet,
    )

    logger.info(
        "[T460] Redeem submitted: bet=%s cid=%s tx=%s", bet_id, condition_id, tx_hash
    )
    return JSONResponse(
        {
            "success": True,
            "mode": "legacy",
            "tx_hash": tx_hash,
            "polygon_scan": f"https://polygonscan.com/tx/{tx_hash}",
            "condition_id": condition_id,
            "outcome": outcome,
            "outcome_index": outcome_index,
            "negative_risk": negative_risk,
            "index_sets": index_sets,
            "signing": sign_ctx,
        }
    )


@router.post("/api/pm/redeem/prepare")
async def api_pm_redeem_prepare(request: Request):
    """Return current backend signer readiness for redeem official mode."""
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    caller_wallet = str(identity.get("wallet_address") or "").strip().lower()
    if not _ADDR_RE.match(caller_wallet):
        return JSONResponse(
            {"error": "invalid user wallet in session"}, status_code=422
        )
    binding = _get_user_signer_binding(caller_wallet)
    if not binding:
        return _server_side_signer_required_response("redeem_prepare", caller_wallet)
    # T914 — deposit_wallet users sign client-side and don't need a backend
    # privy_embedded_wallet_id. Treat them as ready.
    wallet_type_peek = str(binding.get("wallet_type") or "").strip().lower()
    if (
        wallet_type_peek != "deposit_wallet"
        and not binding.get("privy_embedded_wallet_id")
    ):
        return _server_side_signer_required_response("redeem_prepare", caller_wallet)
    return {
        "status": "ok",
        "mode": "official_relayer",
        "server_signing_ready": True,
        "main_wallet": binding.get("main_wallet"),
        "proxy_wallet": binding.get("proxy_wallet"),
    }


@router.post("/api/pm/safe-withdraw")
async def api_pm_safe_withdraw(request: Request):
    """
    从平台交易钱包提现 USDC 到外部地址。

        Body JSON:
            to_address   str   目标 EVM 地址（0x...）
            amount_usdc  float USDC 金额（如 10.5）
            token        str   仅支持 "usdc"（原生）
    """
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    caller_wallet = str(identity.get("wallet_address") or "system")

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    to_address = str(body.get("to_address") or body.get("recipientAddr") or "").strip()
    if not _ADDR_RE.match(to_address):
        return JSONResponse({"error": "Invalid to_address"}, status_code=400)
    # T882-FundAudit#1: explicitly reject the burn / zero address before any
    # downstream checksum / relayer dispatch so user funds cannot be lost to
    # 0x000...000 even if a malformed UI sends it.
    if to_address.lower() == _ZERO_ADDR.lower():
        return JSONResponse(
            {"error": "zero_address_not_allowed", "message": "to_address cannot be the zero address"},
            status_code=400,
        )
    try:
        to_address = _as_checksum_address(to_address)
    except Exception:
        return JSONResponse({"error": "Invalid to_address checksum"}, status_code=400)

    try:
        amount_usdc = float(body.get("amount_usdc") or 0)
    except (TypeError, ValueError):
        amount_usdc = 0.0

    token_key = str(body.get("token") or "pusd").strip().lower()
    if token_key not in {"pusd", "p_usd"}:
        return JSONResponse(
            {
                "error": "UNSUPPORTED_TOKEN",
                "message": "Only pUSD withdrawals on Polygon are supported.",
            },
            status_code=422,
        )
    token_contract, token_label = _as_checksum_address(_PUSD_TOKEN), _pm_v2.PUSD_SYMBOL

    # Official mode: direct native USDC transfer via Safe + relayer.
    if _official_tx_mode():
        if amount_usdc <= 0 or amount_usdc > 1_000_000:
            return JSONResponse(
                {"error": "amount_usdc must be between 0 and 1,000,000"},
                status_code=400,
            )

        amount_wei = int(amount_usdc * 1_000_000)
        relayer_payload = _extract_relayer_payload(body)
        # T538/T539 — see redeem path.
        server_side_built = False
        withdraw_calldata = None
        if not relayer_payload:
            binding = _get_user_signer_binding(caller_wallet)
            _log_signer_debug("withdraw", caller_wallet, binding or {})
            if not binding or not binding.get("privy_embedded_wallet_id"):
                return _server_side_signer_required_response("withdraw", caller_wallet)
            withdraw_calldata = _build_erc20_transfer_calldata(
                to=to_address, amount_wei=amount_wei
            )
            signing_mode = _resolve_signing_mode(binding)
            if signing_mode == "client":
                try:
                    prep = _build_typed_data_skeleton(
                        caller_wallet, token_contract, withdraw_calldata
                    )
                except Exception as exc:
                    logger.error("withdraw typed-data prepare failed: %s", exc)
                    payload, status = _build_tx_error_response(
                        exc, "withdraw_prepare_typed_data"
                    )
                    return JSONResponse(payload, status_code=status)
                return JSONResponse(
                    {
                        "success": False,
                        "requires_client_sign": True,
                        "mode": "client_sign_required",
                        "signing_address": prep["signing_address"],
                        "signing_method": prep["signing_method"],
                        "struct_hash": prep["struct_hash"],
                        "proxy_wallet": prep["proxy_wallet"],
                        "nonce": prep["nonce"],
                        "typed_data": prep["typed_data"],
                        "payload_skeleton": prep["payload_skeleton"],
                        "context": "withdraw",
                        "token": token_label,
                        "token_contract": token_contract,
                        "amount_usdc": amount_usdc,
                        "to_address": to_address,
                    }
                )
            try:
                relayer_payload = _build_server_side_relayer_payload(
                    caller_wallet,
                    token_contract,
                    withdraw_calldata,
                    user_jwt=_extract_request_user_jwts(request),
                )
                server_side_built = True
            except Exception as exc:
                logger.error("withdraw server-side payload build failed: %s", exc)
                payload, status = _build_tx_error_response(
                    exc, "withdraw_server_side_sign"
                )
                return JSONResponse(payload, status_code=status)
        if relayer_payload:
            try:
                if server_side_built:
                    _captured_user_jwt = _extract_request_user_jwts(request)
                    _captured_calldata = withdraw_calldata

                    def _withdraw_rebuild():
                        return _build_server_side_relayer_payload(
                            caller_wallet,
                            token_contract,
                            _captured_calldata,
                            user_jwt=_captured_user_jwt,
                        )

                    try:
                        relayer_result = _relayer_submit(relayer_payload)
                    except Exception as first_exc:
                        if not _is_stale_nonce_or_sig_error(first_exc):
                            raise
                        logger.warning(
                            "withdraw relayer stale-nonce/sig retry: %s", first_exc
                        )
                        relayer_payload = _withdraw_rebuild()
                        relayer_result = _relayer_submit(relayer_payload)
                else:
                    relayer_result = _relayer_submit(relayer_payload)
            except Exception as exc:
                logger.error("withdraw relayer submit failed: %s", exc)
                payload, status = _build_tx_error_response(
                    exc, "withdraw_official_relayer"
                )
                return JSONResponse(payload, status_code=status)
            else:
                tx_hash = str(relayer_result.get("transactionHash") or "")
                tx_id = str(relayer_result.get("transactionID") or "")
                state = str(relayer_result.get("state") or "")
                _insert_vault_tx(
                    tx_type="withdraw",
                    tx_hash=tx_hash,
                    from_addr=str(relayer_payload.get("from") or ""),
                    to_addr=str(relayer_payload.get("to") or to_address),
                    token=token_label,
                    amount=amount_usdc,
                    extra={
                        "mode": "official_relayer",
                        "transaction_id": tx_id,
                        "state": state,
                        "recipient_address": to_address,
                        "token_contract": token_contract,
                    },
                    user_id=caller_wallet,
                )
                return JSONResponse(
                    {
                        "success": True,
                        "mode": "official_relayer",
                        "transaction_id": tx_id,
                        "tx_hash": tx_hash,
                        "state": state,
                        "relayer": relayer_result,
                        "relayer_tx": relayer_payload,
                        "polygon_scan": (
                            f"https://polygonscan.com/tx/{tx_hash}" if tx_hash else ""
                        ),
                    }
                )

    # Legacy mode only (explicit opt-in): direct chain transfer.
    if amount_usdc <= 0 or amount_usdc > 1_000_000:
        return JSONResponse(
            {"error": "amount_usdc must be between 0 and 1,000,000"}, status_code=400
        )

    # USDC 精度 6 位
    amount_wei = int(amount_usdc * 1_000_000)

    try:
        private_key, relayer_addr, key_name_used = _get_secrets()
    except EnvironmentError as exc:
        return JSONResponse(
            {
                "error": "SIGNING_KEY_NOT_CONFIGURED",
                "message": str(exc),
                "detail": {"context": "withdraw"},
            },
            status_code=412,
        )

    sign_ctx = _build_signing_context(private_key, relayer_addr, key_name_used)

    calldata = _build_erc20_transfer_calldata(to=to_address, amount_wei=amount_wei)

    try:
        tx_hash = _sign_and_send(
            private_key=private_key,
            to=token_contract,
            data_hex=calldata,
            fallback_gas=120_000,
        )
    except Exception as exc:
        logger.error("withdraw broadcast failed: %s", exc)
        payload, status = _build_tx_error_response(exc, "withdraw")
        return JSONResponse(payload, status_code=status)

    from eth_account import Account as _Acct  # noqa: PLC0415

    sender_addr = _Acct.from_key(private_key).address
    _insert_vault_tx(
        tx_type="withdraw",
        tx_hash=tx_hash,
        from_addr=sender_addr,
        to_addr=to_address,
        token=token_label,
        amount=amount_usdc,
        extra={"token_contract": token_contract, "amount_wei": amount_wei},
        user_id=caller_wallet,
    )

    logger.info(
        "[T460] Withdraw submitted: %.2f %s → %s tx=%s",
        amount_usdc,
        token_label,
        to_address,
        tx_hash,
    )
    return JSONResponse(
        {
            "success": True,
            "tx_hash": tx_hash,
            "polygon_scan": f"https://polygonscan.com/tx/{tx_hash}",
            "amount_usdc": amount_usdc,
            "token": token_label,
            "to_address": to_address,
            "from_address": sender_addr,
            "signing": sign_ctx,
        }
    )


@router.post("/api/privy/force-enroll")
async def api_privy_force_enroll(request: Request):
    """Force-add configured server signer to the current user's Privy wallet."""
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    caller_wallet = str(identity.get("wallet_address") or "").strip().lower()
    if not _ADDR_RE.match(caller_wallet):
        return JSONResponse(
            {"error": "invalid user wallet in session"}, status_code=422
        )

    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}

    binding = _get_user_signer_binding(caller_wallet, require_wallets=False)
    _log_signer_debug("force_enroll", caller_wallet, binding or {})

    wallet_id = str(
        body.get("wallet_id")
        or (binding.get("privy_embedded_wallet_id") if binding else "")
        or ""
    ).strip()
    signer_ids, policy_ids = _load_privy_server_signer_settings()
    if not signer_ids:
        return JSONResponse(
            {
                "error": "PRIVY_SIGNER_SETTINGS_MISSING",
                "message": "No signer id configured in PRIVY_SERVER_SIGNER_ID / PRIVY_SIGNER_ID",
            },
            status_code=412,
        )

    create_additional_signers: list[dict[str, Any]] = []
    for signer_id in signer_ids:
        signer_entry: dict[str, Any] = {"signer_id": signer_id}
        if policy_ids:
            signer_entry["override_policy_ids"] = policy_ids
        create_additional_signers.append(signer_entry)

    created_wallet_signers: list[str] = []

    # Auto-provision: if no embedded wallet, try to create one via Privy server-side wallet API.
    if not wallet_id:
        # Read privy_user_id directly from DB (may not be in binding).
        privy_user_id = ""
        try:
            _ensure_user_tables()

            def _db_read():
                with db_connect() as conn:
                    return conn.execute(
                        "SELECT privy_user_id FROM users WHERE wallet_address = %s LIMIT 1",
                        (caller_wallet,),
                    ).fetchone()

            row = await asyncio.to_thread(_db_read)
            if row:
                privy_user_id = str(
                    row["privy_user_id"] or row[0] if row else ""
                ).strip()
        except Exception as _exc:
            logger.warning(
                "force_enroll: failed to read privy_user_id for %s: %s",
                caller_wallet,
                _exc,
            )

        if privy_user_id:
            logger.info(
                "force_enroll: no embedded wallet found, attempting auto-provision for did=%s",
                privy_user_id,
            )
            created_wallet_id, created_wallet_addr, created_wallet_signers = (
                _privy_create_embedded_wallet(
                    privy_user_id,
                    additional_signers=create_additional_signers,
                )
            )
            if created_wallet_id:
                wallet_id = created_wallet_id
                # Persist newly created wallet to DB.
                try:
                    now = datetime.now(timezone.utc).isoformat()

                    def _db_persist():
                        with db_connect() as conn:
                            conn.execute(
                                """
                            UPDATE users
                            SET privy_embedded_wallet_id = %s,
                                updated_at = %s
                            WHERE wallet_address = %s
                            """,
                                (created_wallet_id, now, caller_wallet),
                            )
                            conn.commit()

                    await asyncio.to_thread(_db_persist)
                    logger.info(
                        "force_enroll: persisted new wallet_id=%s for %s",
                        created_wallet_id,
                        caller_wallet,
                    )
                    # Refresh binding with newly persisted wallet
                    binding = _get_user_signer_binding(
                        caller_wallet, require_wallets=False
                    )
                except Exception as _exc:
                    logger.warning(
                        "force_enroll: failed to persist new wallet_id: %s", _exc
                    )
            else:
                return JSONResponse(
                    {
                        "error": "PRIVY_WALLET_CREATE_FAILED",
                        "message": "Could not create a Privy embedded wallet for this account. Please contact support.",
                    },
                    status_code=412,
                )
        else:
            return JSONResponse(
                {
                    "error": "PRIVY_USER_ID_MISSING",
                    "message": "Cannot provision wallet: no Privy user ID found. Please log out and log in again.",
                },
                status_code=412,
            )

    if created_wallet_signers:
        enrolled_signer = next(
            (sid for sid in signer_ids if sid in created_wallet_signers), ""
        )
        if enrolled_signer:
            _persist_signer_status(caller_wallet, "enrolled")
            return JSONResponse(
                {
                    "success": True,
                    "wallet_id": wallet_id,
                    "main_wallet": binding.get("main_wallet") if binding else None,
                    "proxy_wallet": binding.get("proxy_wallet") if binding else None,
                    "signer_id": enrolled_signer,
                    "policy_ids": policy_ids,
                    "authorization_keys_loaded": 0,
                    "attempts": [],
                    "provisioned_with_signers": True,
                }
            )

    attempts: list[dict[str, str]] = []
    enrolled_signer = ""
    jwt_candidates = _extract_request_user_jwts(request)
    token_bundle = _extract_request_privy_token_bundle(request)
    for cached_token in _get_cached_user_jwts(caller_wallet):
        if cached_token not in jwt_candidates:
            jwt_candidates.append(cached_token)
    auth_keys = _load_privy_user_authorization_keys(
        jwt_candidates, token_bundle=token_bundle
    )
    for signer_id in signer_ids:
        try:
            _privy_ensure_wallet_additional_signer(
                wallet_id, signer_id, policy_ids, auth_keys=auth_keys
            )
            enrolled_signer = signer_id
            break
        except Exception as exc:
            attempts.append({"signer_id": signer_id, "error": str(exc)[:220]})

    if not enrolled_signer:
        _persist_signer_status(caller_wallet, "pending")
        auth_key_error = any(
            "No valid authorization keys or user signing keys available"
            in str(item.get("error") or "")
            for item in attempts
        )
        error_code = (
            "PRIVY_AUTH_KEYS_INVALID" if auth_key_error else "PRIVY_FORCE_ENROLL_FAILED"
        )
        message = (
            "Privy could not authorize signer enrollment for this wallet. The configured key may exist in Privy, but it is not currently authorized as an owner for this wallet."
            if auth_key_error
            else "Failed to enroll signer for this wallet"
        )
        token_diag = {
            "bundle": {
                "access_token": _jwt_diag_brief(
                    token_bundle.get("access_token")
                    if isinstance(token_bundle, dict)
                    else ""
                ),
                "identity_token": _jwt_diag_brief(
                    token_bundle.get("identity_token")
                    if isinstance(token_bundle, dict)
                    else ""
                ),
            },
            "jwt_candidates": [_jwt_diag_brief(t) for t in jwt_candidates[:3]],
        }
        return JSONResponse(
            {
                "error": error_code,
                "message": message,
                "wallet_id": wallet_id,
                "main_wallet": binding.get("main_wallet") if binding else None,
                "proxy_wallet": binding.get("proxy_wallet") if binding else None,
                "authorization_keys_loaded": len(auth_keys),
                "attempts": attempts,
                "token_diag": token_diag,
            },
            status_code=412,
        )

    _persist_signer_status(caller_wallet, "enrolled")
    return JSONResponse(
        {
            "success": True,
            "wallet_id": wallet_id,
            "main_wallet": binding.get("main_wallet") if binding else None,
            "proxy_wallet": binding.get("proxy_wallet") if binding else None,
            "signer_id": enrolled_signer,
            "policy_ids": policy_ids,
            "authorization_keys_loaded": len(auth_keys),
            "attempts": attempts,
        }
    )


@router.get("/api/privy/signer-diagnostics")
async def api_privy_signer_diagnostics(request: Request):
    """Return DB signer status and live Privy wallet signer attachment state."""
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    caller_wallet = str(identity.get("wallet_address") or "").strip().lower()
    if not _ADDR_RE.match(caller_wallet):
        return JSONResponse(
            {"error": "invalid user wallet in session"}, status_code=422
        )

    binding = _get_user_signer_binding(caller_wallet)
    wallet_id = str((binding or {}).get("privy_embedded_wallet_id") or "").strip()
    signer_ids, policy_ids = _load_privy_server_signer_settings()

    if not wallet_id:
        return JSONResponse(
            {
                "success": True,
                "wallet_address": caller_wallet,
                "wallet_id": "",
                "db_signer_status": str(
                    (binding or {}).get("privy_signer_status") or ""
                ),
                "configured_signer_ids": signer_ids,
                "configured_policy_ids": policy_ids,
                "attached_signer_ids": [],
                "matched_configured_signers": [],
            }
        )

    wallet_snapshot = _privy_get_wallet_snapshot(wallet_id)
    attached_signer_ids: list[str] = []
    for item in wallet_snapshot.get("additional_signers") or []:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("signer_id") or item.get("signerId") or "").strip()
        if sid and sid not in attached_signer_ids:
            attached_signer_ids.append(sid)

    matched = [sid for sid in signer_ids if sid in attached_signer_ids]
    return JSONResponse(
        {
            "success": True,
            "wallet_address": caller_wallet,
            "wallet_id": wallet_id,
            "db_signer_status": str((binding or {}).get("privy_signer_status") or ""),
            "configured_signer_ids": signer_ids,
            "configured_policy_ids": policy_ids,
            "attached_signer_ids": attached_signer_ids,
            "matched_configured_signers": matched,
        }
    )


@router.post("/api/privy/wallet-auth-probe")
async def api_privy_wallet_auth_probe(request: Request):
    """Probe Privy wallets/authenticate with current session JWTs and report raw status/body diagnostics."""
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    caller_wallet = str(identity.get("wallet_address") or "").strip().lower()
    if not _ADDR_RE.match(caller_wallet):
        return JSONResponse(
            {"error": "invalid user wallet in session"}, status_code=422
        )

    tokens = _extract_request_user_jwts(request)
    if not tokens:
        tokens = _get_cached_user_jwts(caller_wallet)
    if not tokens:
        return JSONResponse(
            {"success": False, "error": "no privy jwt found in request/session"},
            status_code=400,
        )

    app_id, app_secrets = _load_privy_app_credentials()
    now_ts = int(time.time())

    import hashlib as _hashlib  # noqa: PLC0415

    def _token_meta(raw_token: str) -> dict[str, Any]:
        token = str(raw_token or "").strip()
        parts = token.split(".")
        claims: dict[str, Any] = {}
        header: dict[str, Any] = {}
        if len(parts) == 3:
            try:
                header = json.loads(
                    base64.urlsafe_b64decode(parts[0] + "==").decode("utf-8")
                )
            except Exception:
                header = {}
            try:
                claims = json.loads(
                    base64.urlsafe_b64decode(parts[1] + "==").decode("utf-8")
                )
            except Exception:
                claims = {}
        return {
            "fingerprint": _hashlib.sha256(token.encode("utf-8")).hexdigest()[:20],
            "shape": {
                "len": len(token),
                "dots": token.count("."),
            },
            "header": {
                "alg": str(header.get("alg") or ""),
                "kid": str(header.get("kid") or ""),
            },
            "claims": {
                "iss": str(claims.get("iss") or ""),
                "aud": claims.get("aud"),
                "sub": str(claims.get("sub") or ""),
                "exp": claims.get("exp"),
                "exp_in_sec": (
                    (int(claims.get("exp") or 0) - now_ts)
                    if str(claims.get("exp") or "").isdigit()
                    else None
                ),
            },
        }

    attempts: list[dict[str, Any]] = []
    first_success: dict[str, Any] | None = None
    for token_idx, token in enumerate(tokens, start=1):
        token_meta = _token_meta(token)
        for secret_idx, app_secret in enumerate(app_secrets, start=1):
            for auth_url in _PRIVY_WALLETS_AUTH_URLS:
                variant_rows = _call_wallets_auth_with_variants(
                    app_id=app_id,
                    app_secret=app_secret,
                    user_jwt=token,
                    auth_url=auth_url,
                )
                for row in variant_rows:
                    result = {
                        "host": auth_url,
                        "variant": str(row.get("variant") or ""),
                        "token_idx": token_idx,
                        "secret_idx": secret_idx,
                        "status": int(row.get("status") or -1),
                        "token": token_meta,
                    }
                    if isinstance(row.get("body"), dict):
                        result["body"] = row.get("body")
                    if row.get("error"):
                        result["error"] = str(row.get("error"))
                    attempts.append(result)
                    if int(result.get("status") or -1) < 400:
                        body = (
                            result.get("body")
                            if isinstance(result.get("body"), dict)
                            else {}
                        )
                        if str((body or {}).get("authorization_key") or "").strip():
                            first_success = result
                            break
            if first_success:
                break
        if first_success:
            break

    # Validate app credentials independently of wallets/authenticate payload semantics.
    did = ""
    for item in attempts:
        token_info = item.get("token") if isinstance(item.get("token"), dict) else {}
        claims = (
            token_info.get("claims")
            if isinstance(token_info.get("claims"), dict)
            else {}
        )
        did_candidate = str(claims.get("sub") or "").strip()
        if did_candidate.startswith("did:privy:"):
            did = did_candidate
            break

    credential_probe: list[dict[str, Any]] = []
    if did:
        for idx, app_secret in enumerate(app_secrets, start=1):
            rows = _probe_privy_basic_auth(
                app_id=app_id,
                app_secret=app_secret,
                privy_did=did,
            )
            credential_probe.append(
                {
                    "secret_idx": idx,
                    "did": did,
                    "checks": rows,
                }
            )

    return JSONResponse(
        {
            "success": bool(first_success),
            "app_id": app_id,
            "app_secret_count": len(app_secrets),
            "wallet_address": caller_wallet,
            "attempt_count": len(attempts),
            "attempts": attempts,
            "credential_probe": credential_probe,
            "first_success": first_success,
        },
        status_code=200 if first_success else 412,
    )


@router.get("/api/pm/withdraw-readiness")
async def api_pm_withdraw_readiness(request: Request):
    """Lightweight pre-check: is withdraw possible for the current user?

    Returns ready=true if all conditions are met, otherwise returns a specific
    error_code so the frontend can show an actionable message instead of
    attempting enroll and failing silently.
    """
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    caller_wallet = str(identity.get("wallet_address") or "").strip().lower()
    if not _ADDR_RE.match(caller_wallet):
        return JSONResponse(
            {"error": "invalid user wallet in session"}, status_code=422
        )

    official_mode = _official_tx_mode()

    if not official_mode:
        # Legacy mode: check POLY_PRIVATE_KEY
        try:
            _get_secrets()
            return JSONResponse({"ready": True, "mode": "legacy"})
        except EnvironmentError as exc:
            return JSONResponse(
                {
                    "ready": False,
                    "mode": "legacy",
                    "error_code": "LEGACY_KEY_MISSING",
                    "message": str(exc),
                },
                status_code=412,
            )

    # Official mode checks
    # 1. Relayer credentials
    try:
        _get_relayer_headers()
    except Exception as exc:
        return JSONResponse(
            {
                "ready": False,
                "mode": "official",
                "error_code": "RELAYER_NOT_CONFIGURED",
                "message": "Relayer API key is not configured. Contact platform support.",
                "detail": str(exc),
            },
            status_code=412,
        )

    # 2. Privy app credentials
    app_id, _ = _load_privy_app_credentials()
    if not app_id:
        return JSONResponse(
            {
                "ready": False,
                "mode": "official",
                "error_code": "PRIVY_APP_NOT_CONFIGURED",
                "message": "Privy application credentials are not configured. Contact platform support.",
            },
            status_code=412,
        )

    # 3. Privy server signer
    signer_ids, _ = _load_privy_server_signer_settings()
    if not signer_ids:
        return JSONResponse(
            {
                "ready": False,
                "mode": "official",
                "error_code": "PRIVY_SIGNER_NOT_CONFIGURED",
                "message": "Platform signer is not configured. Contact platform support.",
            },
            status_code=412,
        )

    # 4. Privy authorization key
    try:
        _load_privy_authorization_key_pems()
    except EnvironmentError:
        return JSONResponse(
            {
                "ready": False,
                "mode": "official",
                "error_code": "PRIVY_AUTH_KEY_NOT_CONFIGURED",
                "message": "Platform authorization key is not configured. Contact platform support.",
            },
            status_code=412,
        )

    # 5. User binding / embedded wallet
    binding = _get_user_signer_binding(caller_wallet, require_wallets=False)

    # T880-followup (2026-05-06): SILENT-ONLY READINESS.
    # The product requirement is that /v2/withdraw must NEVER show a
    # "Wallet setup required" banner / modal on page load. The previous
    # behaviour returned 412 USER_WALLET_NOT_BOUND / USER_SIGNER_NOT_ENROLLED
    # for any user lacking a Privy embedded wallet or whose signer was not
    # yet enrolled — that fired on every brand-new account and on every
    # account whose Privy session had drifted, which is exactly what users
    # complained about ("混蛋！...全都静默为用户实现吗？！").
    #
    # We now treat the readiness probe as a PLATFORM-LEVEL health check
    # only (relayer / Privy app / authorization key — those are operator
    # responsibilities). For per-user wallet/signer state we always return
    # ``ready=true`` and surface a non-blocking ``provisioning`` hint so
    # the frontend can transparently auto-enroll on first submit if needed.
    # If the user truly cannot withdraw (e.g. no embedded wallet AND
    # auto-enroll fails), the actual ``/api/pm/safe-withdraw/dispatch``
    # call will return a real, contextual error AT SUBMIT TIME — not as a
    # cold modal on page load.
    payload: dict[str, Any] = {"ready": True, "mode": "official"}

    if binding:
        wt = str(binding.get("wallet_type") or "").lower()
        if wt:
            payload["wallet_type"] = wt
        if binding.get("proxy_wallet"):
            payload["proxy_wallet"] = binding.get("proxy_wallet")
        if binding.get("privy_embedded_wallet_id"):
            payload["wallet_id"] = binding.get("privy_embedded_wallet_id")

        signer_status = str(binding.get("privy_signer_status") or "").strip().lower()
        if wt == "deposit_wallet":
            # deposit_wallet flow signs through Builder Relayer; no Privy enroll needed.
            payload["provisioning"] = "deposit_wallet_ready"
        elif not binding.get("privy_embedded_wallet_id"):
            payload["provisioning"] = "needs_embedded_wallet_silent"
        elif signer_status != "enrolled":
            payload["provisioning"] = "needs_signer_enroll_silent"
        else:
            payload["provisioning"] = "ready"
    else:
        # First-touch user with no binding row yet — dispatch will lazily
        # create whatever's needed. UI stays silent.
        payload["provisioning"] = "needs_first_touch_silent"

    return JSONResponse(payload)


@router.post("/api/pm/safe-withdraw/prepare")
async def api_pm_safe_withdraw_prepare(request: Request):
    """Return current backend signer readiness for withdraw official mode."""
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    caller_wallet = str(identity.get("wallet_address") or "").strip().lower()
    if not _ADDR_RE.match(caller_wallet):
        return JSONResponse(
            {"error": "invalid user wallet in session"}, status_code=422
        )
    binding = _get_user_signer_binding(caller_wallet)
    if not binding or not binding.get("privy_embedded_wallet_id"):
        return _server_side_signer_required_response("withdraw_prepare", caller_wallet)
    return {
        "status": "ok",
        "mode": "official_relayer",
        "server_signing_ready": True,
        "main_wallet": binding.get("main_wallet"),
        "proxy_wallet": binding.get("proxy_wallet"),
    }


@router.get("/api/pm/tx-health")
async def api_pm_tx_health(request: Request):
    """Runtime tx diagnostics for official relayer/bridge mode and legacy fallback."""
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    official_mode = _official_tx_mode()
    health: dict[str, Any] = {
        "ok": True,
        "mode": "official" if official_mode else "legacy",
        "official": {
            "relayer_api": _RELAYER_API,
            "bridge_api": _BRIDGE_API,
            "relayer_api_key_configured": bool(
                str(os.environ.get("RELAYER_API_KEY") or "").strip()
            ),
            "relayer_api_key_address": str(
                os.environ.get("RELAYER_API_KEY_ADDRESS") or ""
            ).strip(),
        },
    }

    if official_mode:
        try:
            _get_relayer_headers()
        except Exception as exc:
            health["ok"] = False
            health["error"] = "RELAYER_AUTH_NOT_CONFIGURED"
            health["message"] = str(exc)
        return JSONResponse(health, status_code=200 if health.get("ok") else 412)

    try:
        private_key, relayer_addr, key_name_used = _get_secrets()
    except EnvironmentError as exc:
        return JSONResponse(
            {
                "ok": False,
                "mode": "legacy",
                "error": "SIGNING_KEY_NOT_CONFIGURED",
                "message": str(exc),
            },
            status_code=412,
        )

    sign_ctx = _build_signing_context(private_key, relayer_addr, key_name_used)
    sender = sign_ctx["sender_address"]
    try:
        bal_wei = _get_native_balance_wei(sender)
        gas_price = _get_gas_price()
        est_limit = 300000
        est_required = int(est_limit * gas_price * 1.10)
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "mode": "legacy",
                "error": "RPC_UNAVAILABLE",
                "message": str(exc),
                "signing": sign_ctx,
            },
            status_code=502,
        )

    have_pol = _wei_to_pol(bal_wei)
    need_pol = _wei_to_pol(est_required)
    health["ok"] = bal_wei >= est_required
    health["signing"] = sign_ctx
    health["gas"] = {
        "sender_pol": round(have_pol, 6),
        "est_required_pol": round(need_pol, 6),
        "shortfall_pol": round(max(0.0, need_pol - have_pol), 6),
        "gas_price_wei": gas_price,
        "est_gas_limit": est_limit,
    }
    return JSONResponse(health)


@router.get("/api/pm/vault/history")
async def api_pm_vault_history(request: Request):
    """
    查询 Vault 交易历史（赎回 + 提现 + 充值）。

    Query params:
      limit  int  最多返回条数（默认 50，上限 200）
      offset int  分页偏移
    """
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    caller_wallet = str(identity.get("wallet_address") or "")
    is_admin = bool(identity.get("is_admin"))

    _ensure_vault_table()

    try:
        limit = max(1, min(200, int(request.query_params.get("limit", "50"))))
        offset = max(0, int(request.query_params.get("offset", "0")))
    except (TypeError, ValueError):
        limit, offset = 50, 0

    try:

        def _db():
            with db_connect() as conn:
                if is_admin:
                    rows = conn.execute(
                        """SELECT id, user_id, tx_type, tx_hash, from_addr, to_addr,
                              token, amount, status, extra_json, created_at
                       FROM vault_transactions
                       ORDER BY created_at DESC
                       LIMIT %s OFFSET %s""",
                        (limit, offset),
                    ).fetchall()
                    total_row = conn.execute(
                        "SELECT COUNT(*) AS c FROM vault_transactions"
                    ).fetchone()
                else:
                    rows = conn.execute(
                        """SELECT id, user_id, tx_type, tx_hash, from_addr, to_addr,
                              token, amount, status, extra_json, created_at
                       FROM vault_transactions
                       WHERE user_id = %s
                       ORDER BY created_at DESC
                       LIMIT %s OFFSET %s""",
                        (caller_wallet, limit, offset),
                    ).fetchall()
                    total_row = conn.execute(
                        "SELECT COUNT(*) AS c FROM vault_transactions WHERE user_id = %s",
                        (caller_wallet,),
                    ).fetchone()
                total = int((total_row or {}).get("c") or 0)
                return rows, total

        rows, total = await asyncio.to_thread(_db)
    except Exception as exc:
        logger.warning("vault history query failed: %s", exc)
        rows = []
        total = 0

    txns = []
    for r in rows:
        row = dict(r)
        try:
            row["extra"] = json.loads(row.pop("extra_json") or "{}")
        except Exception:
            row["extra"] = {}
        ts = int(row.get("created_at") or 0)
        row["created_at_iso"] = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""
        )
        txns.append(row)

    return JSONResponse(
        {"txns": txns, "total": total, "limit": limit, "offset": offset}
    )


# ═══════════════════════════════════════════════════════════════════════════
# T884-CONV: USDC → pUSD conversion via CollateralOnramp
# For deposit_wallet (signature_type=3) users who funded their Safe with
# native USDC but need pUSD to trade on the CLOB.
# ═══════════════════════════════════════════════════════════════════════════


def _fetch_erc20_balance_raw(wallet_addr: str, token_addr: str) -> int:
    """Return raw ERC20 balanceOf(wallet_addr) as integer (smallest unit)."""
    addr = str(wallet_addr or "").lower().replace("0x", "")
    if len(addr) != 40:
        return 0
    selector = _keccak4("balanceOf(address)").hex()
    data = "0x" + selector + "0" * 24 + addr
    res = _eth_call_polygon(token_addr, data)
    if res and res.startswith("0x"):
        try:
            return int(res, 16)
        except ValueError:
            return 0
    return 0


def _build_erc20_approve_calldata(spender: str, amount_wei: int) -> str:
    """Build ERC20 approve(address,uint256) calldata. selector: 0x095ea7b3"""
    from eth_abi import encode  # noqa: PLC0415

    selector = bytes.fromhex("095ea7b3")
    encoded = encode(["address", "uint256"], [_as_checksum_address(spender), amount_wei])
    return "0x" + (selector + encoded).hex()


def _build_onramp_wrap_calldata(asset: str, to: str, amount_wei: int) -> str:
    """Build CollateralOnramp.wrap(address,address,uint256) calldata.
    selector: 0x62355638  (confirmed via bytecode scan of 0x93070a...)
    """
    from eth_abi import encode  # noqa: PLC0415

    selector = bytes.fromhex("62355638")
    encoded = encode(
        ["address", "address", "uint256"],
        [_as_checksum_address(asset), _as_checksum_address(to), amount_wei],
    )
    return "0x" + (selector + encoded).hex()


def _is_deposit_wallet_deployed(deposit_wallet_addr: str) -> bool:
    """Check on-chain if a deposit wallet contract has been deployed.

    Polymarket relayer validates WALLET batches against on-chain existence
    (eth_getCode != 0x). The relayer /deployed endpoint is unreliable for this
    purpose — always use this function for deployment status checks.
    """
    try:
        code = _polygon_rpc_call("eth_getCode", [deposit_wallet_addr, "latest"])
        return bool(code and code != "0x" and len(code) > 4)
    except Exception as exc:
        logger.warning("[Conv] eth_getCode check failed for %s: %s", deposit_wallet_addr, exc)
        return False


def _get_deposit_wallet_owner_eoa(binding: dict) -> str:
    """Return the EOA that is the owner of the deposit wallet (for WALLET batch signing).

    The deposit wallet is derived from main_wallet via CREATE2. The owner is
    main_wallet. Privy embedded wallet is NOT the owner unless the derivation
    was done from embedded_wallet (which the current system does not do).
    """
    return str(binding.get("main_wallet") or binding.get("wallet_address") or "").strip()


def _build_wallet_multicall_relayer_payload(
    user_wallet: str,
    calls: list[dict],
    user_jwt: str | list | tuple = "",
) -> dict[str, Any]:
    """Build + server-side-sign a WALLET-batch relayer payload with ≥1 calls.

    For deposit_wallet (signature_type=3) users only. Raises RuntimeError on
    missing binding, wrong wallet_type, or missing Privy wallet_id.

    ARCHITECTURE: The deposit wallet is derived from main_wallet (MetaMask EOA).
    For server-side signing to work, the Privy embedded wallet must be either:
    (a) the same as main_wallet, or (b) a registered session signer on the
    deployed deposit wallet contract. If neither applies, raises RuntimeError
    to trigger the client-side signing fallback.
    """
    binding = _get_user_signer_binding(user_wallet)
    if not binding:
        raise RuntimeError("missing user signer binding")

    wtype = str(binding.get("wallet_type") or "safe").strip().lower()
    if wtype != "deposit_wallet":
        raise RuntimeError("multicall relayer payload only supported for deposit_wallet users")

    proxy_wallet = str(binding.get("proxy_wallet") or "")
    main_wallet = str(binding.get("main_wallet") or "")
    wallet_id = str(binding.get("privy_embedded_wallet_id") or "").strip()
    if not wallet_id:
        raise RuntimeError("privy embedded wallet id missing — client-side signing required")

    # The deposit wallet is derived from main_wallet (MetaMask EOA).
    # Server-side signing uses the Privy embedded wallet.
    # For this to work, the embedded wallet must be a valid signer on the deposit
    # wallet contract (either as owner or session signer). If the deposit wallet
    # is not deployed on-chain yet, signing will fail at the relayer level.
    # Detect this early to give a clear error for fallback routing.
    if not _is_deposit_wallet_deployed(proxy_wallet):
        raise RuntimeError(
            f"deposit wallet {proxy_wallet} not deployed on-chain — "
            "client-side deployment and signing required"
        )

    jwt_candidates: list[str] = (
        [str(x or "") for x in user_jwt]
        if isinstance(user_jwt, (list, tuple, set))
        else [str(user_jwt or "")]
    )
    for cached in _get_cached_user_jwts(user_wallet):
        if cached not in jwt_candidates:
            jwt_candidates.append(cached)
    auth_keys = _load_privy_user_authorization_keys(jwt_candidates)

    # Ensure additional signer is enrolled
    signer_ids, policy_ids = _load_privy_server_signer_settings()
    for signer_id in signer_ids:
        try:
            if not _privy_wallet_has_additional_signer(wallet_id, signer_id):
                _privy_ensure_wallet_additional_signer(
                    wallet_id, signer_id, policy_ids, auth_keys=auth_keys
                )
                _persist_signer_status(user_wallet, "enrolled")
                logger.info(
                    "[T884-CONV] auto-enrolled signer wallet_id=%s signer=%s",
                    wallet_id, signer_id,
                )
        except Exception as enroll_exc:
            logger.warning("[T884-CONV] signer enrollment error: %s", enroll_exc)

    # Owner of deposit wallet = main_wallet (derive(main_wallet) == deposit_wallet)
    owner_eoa = _get_deposit_wallet_owner_eoa(binding)
    nonce = _get_relayer_nonce(owner_eoa, "WALLET")
    deadline = int(time.time()) + 600

    normalized_calls = [
        {
            "target": _as_checksum_address(str(c.get("target") or _ZERO_ADDR)),
            "value": str(c.get("value") or "0"),
            "data": str(c.get("data") or "0x"),
        }
        for c in calls
    ]

    typed_data = _build_deposit_wallet_batch_typed_data(
        deposit_wallet=proxy_wallet,
        nonce=nonce,
        deadline=deadline,
        calls=normalized_calls,
    )

    logger.info(
        "[Convert][prepare] deposit_wallet=%s owner_eoa=%s nonce=%s",
        proxy_wallet, owner_eoa, nonce,
    )
    logger.info(
        "[Convert][prepare] FULL_BATCH_TYPED_DATA: %s",
        json.dumps(typed_data, default=str),
    )

    raw_sig = _privy_sign_safe_typed_data(wallet_id, typed_data, auth_keys=auth_keys)
    signature = "0x" + raw_sig.lower().removeprefix("0x")
    # Relayer-v2 enforces `from == auth address` on every payload.
    # Ownership is proven via the EIP-712 Batch signature (verified via
    # ERC-1271 against the deposit wallet's registered owner).
    return {
        "type": "WALLET",
        "from": _resolve_relayer_from(owner_eoa),
        "to": _pm_v2.DEPOSIT_WALLET_FACTORY,
        "nonce": str(nonce),
        "signature": signature,
        "depositWalletParams": {
            "depositWallet": _as_checksum_address(proxy_wallet),
            "deadline": str(deadline),
            "calls": normalized_calls,
        },
        "metadata": "",
    }


def _is_fix_native_usdc_authorized(identity: dict | None) -> bool:
    if not identity:
        return False
    wallet = str(identity.get("wallet_address") or "").strip().lower()
    return bool(_ADDR_RE.match(wallet) and wallet == _FIX_NATIVE_USDC_ALLOWED_EOA)


def _fix_native_usdc_enabled() -> bool:
    return str(os.environ.get("ENABLE_T892_FIX_NATIVE_USDC_PAGE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _build_fix_native_usdc_call(amount_raw: int) -> dict[str, str]:
    return {
        "target": _as_checksum_address(_USDC_NATIVE_TOKEN),
        "value": "0",
        "data": _build_erc20_transfer_calldata(_FIX_NATIVE_USDC_BRIDGE_INTAKE, amount_raw),
    }


@router.get("/api/fix-native-usdc/state")
async def api_fix_native_usdc_state(request: Request):
    """T892 one-time: read native USDC balance and signer readiness for one user."""
    if not _fix_native_usdc_enabled():
        return JSONResponse({"error": "not_found"}, status_code=404)
    identity = get_request_identity(request)
    if not _is_fix_native_usdc_authorized(identity):
        return JSONResponse({"error": "not_found"}, status_code=404)

    user_wallet = str(identity.get("wallet_address") or "")
    binding = _get_user_signer_binding(user_wallet, require_wallets=True)
    if not binding:
        return JSONResponse({"error": "wallet not bound"}, status_code=412)

    main_wallet = str(binding.get("main_wallet") or "").strip().lower()
    deposit_wallet = str(binding.get("proxy_wallet") or "").strip()
    if not _ADDR_RE.match(deposit_wallet.lower()):
        return JSONResponse({"error": "deposit_wallet address invalid"}, status_code=500)
    if main_wallet != _FIX_NATIVE_USDC_ALLOWED_EOA:
        return JSONResponse({"error": "not_found"}, status_code=404)

    native_raw = _fetch_erc20_balance_raw(deposit_wallet, _USDC_NATIVE_TOKEN)
    native_amount = native_raw / 10**6
    return JSONResponse(
        {
            "success": True,
            "authorized_wallet": _as_checksum_address(_FIX_NATIVE_USDC_ALLOWED_EOA),
            "deposit_wallet": _as_checksum_address(deposit_wallet),
            "bridge_intake": _as_checksum_address(_FIX_NATIVE_USDC_BRIDGE_INTAKE),
            "native_usdc_raw": str(native_raw),
            "native_usdc": round(native_amount, 6),
            "native_usdc_display": f"{native_amount:.2f}",
        }
    )


@router.post("/api/fix-native-usdc/prepare")
async def api_fix_native_usdc_prepare(request: Request):
    """T892 one-time: build typed_data for native USDC -> bridge transfer."""
    if not _fix_native_usdc_enabled():
        return JSONResponse({"error": "not_found"}, status_code=404)
    identity = get_request_identity(request)
    if not _is_fix_native_usdc_authorized(identity):
        return JSONResponse({"error": "not_found"}, status_code=404)

    user_wallet = str(identity.get("wallet_address") or "")
    binding = _get_user_signer_binding(user_wallet, require_wallets=True)
    if not binding:
        return JSONResponse({"error": "wallet not bound"}, status_code=412)

    main_wallet = str(binding.get("main_wallet") or "").strip()
    deposit_wallet = str(binding.get("proxy_wallet") or "").strip()
    if not _ADDR_RE.match(deposit_wallet.lower()):
        return JSONResponse({"error": "deposit_wallet address invalid"}, status_code=500)
    if str(main_wallet).strip().lower() != _FIX_NATIVE_USDC_ALLOWED_EOA:
        return JSONResponse({"error": "not_found"}, status_code=404)

    amount_raw = _fetch_erc20_balance_raw(deposit_wallet, _USDC_NATIVE_TOKEN)
    if amount_raw <= 0:
        return JSONResponse(
            {
                "error": "no_native_usdc",
                "message": "No native USDC found in deposit wallet.",
            },
            status_code=400,
        )

    wallet_deployed = _is_deposit_wallet_deployed(deposit_wallet)
    if not wallet_deployed:
        wc_payload = {
            "type": "WALLET-CREATE",
            "from": _resolve_relayer_from(main_wallet),
            "to": _pm_v2.DEPOSIT_WALLET_FACTORY,
            "metadata": "",
        }
        try:
            _relayer_submit(wc_payload)
        except Exception as wc_exc:
            logger.error("[T892][prepare] WALLET-CREATE failed: %s", wc_exc)
            return JSONResponse(
                {
                    "error": "wallet_deploy_failed",
                    "message": str(wc_exc),
                    "deposit_wallet": deposit_wallet,
                },
                status_code=502,
            )
        return JSONResponse(
            {
                "error": "wallet_deploying",
                "hint": "Deposit wallet deployment in progress. Please retry in ~30 seconds.",
                "deposit_wallet": deposit_wallet,
            },
            status_code=202,
        )

    nonce = _get_relayer_nonce(main_wallet, "WALLET")
    deadline = int(time.time()) + 900
    calls = [_build_fix_native_usdc_call(amount_raw)]
    typed_data = _build_deposit_wallet_batch_typed_data(
        deposit_wallet=deposit_wallet,
        nonce=nonce,
        deadline=deadline,
        calls=calls,
    )
    return JSONResponse(
        {
            "success": True,
            "requires_client_sign": True,
            "signing_address": _as_checksum_address(main_wallet),
            "typed_data": typed_data,
            "nonce": str(nonce),
            "amount_raw": str(amount_raw),
            "amount_usdc": round(amount_raw / 10**6, 6),
            "depositWalletParams": {
                "depositWallet": _as_checksum_address(deposit_wallet),
                "deadline": str(deadline),
                "calls": calls,
            },
        }
    )


@router.post("/api/fix-native-usdc/submit")
async def api_fix_native_usdc_submit(request: Request):
    """T892 one-time: submit user-signed WALLET batch to relayer."""
    if not _fix_native_usdc_enabled():
        return JSONResponse({"error": "not_found"}, status_code=404)
    identity = get_request_identity(request)
    if not _is_fix_native_usdc_authorized(identity):
        return JSONResponse({"error": "not_found"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    user_wallet = str(identity.get("wallet_address") or "")
    binding = _get_user_signer_binding(user_wallet, require_wallets=True)
    if not binding:
        return JSONResponse({"error": "wallet not bound"}, status_code=412)

    main_wallet = str(binding.get("main_wallet") or "").strip()
    deposit_wallet = str(binding.get("proxy_wallet") or "").strip()
    if str(main_wallet).strip().lower() != _FIX_NATIVE_USDC_ALLOWED_EOA:
        return JSONResponse({"error": "not_found"}, status_code=404)

    signature = str(body.get("signature") or "").strip()
    nonce = str(body.get("nonce") or "").strip()
    dw_params = body.get("depositWalletParams") or {}
    calls = dw_params.get("calls") or []
    deadline = str(dw_params.get("deadline") or "")

    if not signature or not nonce or not calls:
        return JSONResponse(
            {"error": "signature, nonce, and depositWalletParams.calls required"},
            status_code=400,
        )

    relayer_payload = {
        "type": "WALLET",
        "from": _resolve_relayer_from(main_wallet),
        "to": _pm_v2.DEPOSIT_WALLET_FACTORY,
        "nonce": nonce,
        "signature": signature,
        "depositWalletParams": {
            "depositWallet": _as_checksum_address(deposit_wallet),
            "deadline": deadline,
            "calls": calls,
        },
        "metadata": "T892-fix-native-usdc-temporary-page",
    }

    try:
        relayer_result = _relayer_submit(relayer_payload)
    except Exception as exc:
        logger.error("[T892][submit] relayer failed: %s", exc)
        payload, status = _build_tx_error_response(exc, "fix_native_usdc_submit")
        return JSONResponse(payload, status_code=status)

    tx_hash = ""
    if isinstance(relayer_result, dict):
        tx_hash = str(
            relayer_result.get("txHash")
            or relayer_result.get("tx_hash")
            or relayer_result.get("transactionHash")
            or relayer_result.get("hash")
            or ""
        ).strip()
    return JSONResponse({"success": True, "tx_hash": tx_hash, "relayer_result": relayer_result})


@router.get("/api/deposit/bridge-qr.svg")
async def api_deposit_bridge_qr_svg(request: Request):
    """Render a same-origin QR SVG to avoid external image CSP blocking."""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "not authenticated"}, status_code=403)

    raw = str(request.query_params.get("address") or "").strip()
    if not _ADDR_RE.match(raw.lower()):
        return JSONResponse({"error": "invalid address"}, status_code=400)

    try:
        import qrcode
        import qrcode.image.svg

        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(raw, image_factory=factory, box_size=8, border=2)
        svg_bytes = img.to_string()
        return Response(content=svg_bytes, media_type="image/svg+xml")
    except Exception as exc:
        logger.warning("[deposit][qr] svg generation failed: %s", exc)
        return JSONResponse({"error": "qr_generation_failed"}, status_code=500)


@router.post("/api/deposit/usdc-to-pusd")
async def api_deposit_usdc_to_pusd(request: Request):
    """Deprecated in T893: conversion flow removed from /v2/withdraw and deposit path."""
    return JSONResponse(
        {
            "error": "deprecated_removed_t893",
            "message": "USDC conversion endpoint has been removed. Use direct pUSD flow.",
        },
        status_code=410,
    )
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "not authenticated"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "")
    binding = _get_user_signer_binding(user_wallet, require_wallets=True)
    if not binding:
        return JSONResponse({"error": "wallet not bound"}, status_code=412)

    wallet_type = str(binding.get("wallet_type") or "").strip().lower()
    if wallet_type != "deposit_wallet":
        return JSONResponse(
            {
                "error": "usdc_to_pusd only available for deposit_wallet users",
                "wallet_type": wallet_type,
            },
            status_code=400,
        )

    deposit_wallet = str(binding.get("proxy_wallet") or "").strip()
    main_wallet = str(binding.get("main_wallet") or "").strip()

    if not _ADDR_RE.match(deposit_wallet.lower()):
        return JSONResponse({"error": "deposit_wallet address invalid"}, status_code=500)

    try:
        body = await request.json()
    except Exception:
        body = {}

    amount_raw = body.get("amount")  # optional user-specified amount (float)
    token_pref = str(body.get("token") or "").strip().lower()
    mode = str(body.get("mode") or "").strip().lower()

    # Read on-chain balances for both native USDC and bridged USDC.e.
    usdc_e_wei = _fetch_erc20_balance_raw(deposit_wallet, _USDC_E_TOKEN)
    usdc_e_float = usdc_e_wei / 10**6
    usdc_native_wei = _fetch_erc20_balance_raw(deposit_wallet, _USDC_NATIVE_TOKEN)
    usdc_native_float = usdc_native_wei / 10**6
    total_convertible_wei = int(usdc_e_wei or 0) + int(usdc_native_wei or 0)
    total_convertible_float = total_convertible_wei / 10**6

    action_kind = "usdc_wrap"

    native_alias = {"native", "usdc_native", "usdc-native", "usdc"}
    bridged_alias = {"usdc_e", "usdce", "usdc.e", "bridged"}

    def _build_wrap_calls(token_addr: str, amount_wei: int) -> list[dict[str, str]]:
        if amount_wei <= 0:
            return []
        approve_data_local = _build_erc20_approve_calldata(_COLLATERAL_ONRAMP, amount_wei)
        wrap_data_local = _build_onramp_wrap_calldata(token_addr, deposit_wallet, amount_wei)
        return [
            {
                "target": _as_checksum_address(token_addr),
                "value": "0",
                "data": approve_data_local,
            },
            {
                "target": _as_checksum_address(_COLLATERAL_ONRAMP),
                "value": "0",
                "data": wrap_data_local,
            },
        ]

    calls: list[dict[str, str]] = []
    converted_native_wei = 0
    converted_usdce_wei = 0

    if mode == "trigger_wrap":
        # T891-CONV: CollateralOnramp only supports USDC.e → pUSD.
        # Native USDC (0x3c499c...) is NOT accepted by CollateralOnramp and will
        # revert. Only include USDC.e in wrap calls. Native USDC balance is
        # returned as metadata so the frontend can show an advisory.
        if usdc_e_wei <= 0:
            return JSONResponse(
                {
                    "error": "no_convertible_usdc",
                    "hint": "No USDC.e balance available to convert. Native USDC cannot be converted via this path — use the bridge intake address instead.",
                    "deposit_wallet": deposit_wallet,
                    "available_usdc_native": usdc_native_float,
                    "available_usdc_e": usdc_e_float,
                },
                status_code=400,
            )
        action_kind = "pending_wrap_trigger"
        converted_native_wei = 0  # native USDC skipped — CollateralOnramp does not support it
        converted_usdce_wei = int(usdc_e_wei or 0)
        calls.extend(_build_wrap_calls(_USDC_E_TOKEN, converted_usdce_wei))
        amount_wei = converted_usdce_wei
        amount_float = amount_wei / 10**6
    else:
        if total_convertible_wei <= 0:
            return JSONResponse(
                {
                    "error": "no_convertible_usdc",
                    "hint": "Deposit wallet has no native USDC or USDC.e to convert.",
                    "deposit_wallet": deposit_wallet,
                    "available_usdc_native": usdc_native_float,
                    "available_usdc_e": usdc_e_float,
                },
                status_code=400,
            )

        if amount_raw is not None:
            try:
                amount_float = float(amount_raw)
            except Exception:
                return JSONResponse({"error": "amount must be numeric"}, status_code=400)
            req_wei = int(amount_float * 10**6)
            if req_wei <= 0:
                return JSONResponse({"error": "amount_out_of_range", "requested": amount_float}, status_code=400)

            if token_pref in native_alias:
                if req_wei > usdc_native_wei:
                    return JSONResponse(
                        {
                            "error": "amount_out_of_range",
                            "available_usdc_native": usdc_native_float,
                            "requested": amount_float,
                            "token": "usdc_native",
                        },
                        status_code=400,
                    )
                converted_native_wei = req_wei
            elif token_pref in bridged_alias:
                if req_wei > usdc_e_wei:
                    return JSONResponse(
                        {
                            "error": "amount_out_of_range",
                            "available_usdc_e": usdc_e_float,
                            "requested": amount_float,
                            "token": "usdc_e",
                        },
                        status_code=400,
                    )
                converted_usdce_wei = req_wei
            else:
                # Auto mode: prioritize native USDC first, then consume USDC.e.
                native_take = min(req_wei, int(usdc_native_wei or 0))
                remainder = req_wei - native_take
                if remainder > int(usdc_e_wei or 0):
                    return JSONResponse(
                        {
                            "error": "amount_out_of_range",
                            "available_total": total_convertible_float,
                            "available_usdc_native": usdc_native_float,
                            "available_usdc_e": usdc_e_float,
                            "requested": amount_float,
                        },
                        status_code=400,
                    )
                converted_native_wei = native_take
                converted_usdce_wei = remainder

            calls.extend(_build_wrap_calls(_USDC_NATIVE_TOKEN, converted_native_wei))
            calls.extend(_build_wrap_calls(_USDC_E_TOKEN, converted_usdce_wei))
            amount_wei = converted_native_wei + converted_usdce_wei
            amount_float = amount_wei / 10**6
            if amount_wei <= 0:
                return JSONResponse(
                    {
                        "error": "amount_out_of_range",
                        "available_total": total_convertible_float,
                        "requested": amount_float,
                    },
                    status_code=400,
                )
        else:
            # T891-CONV: Only wrap USDC.e — CollateralOnramp does NOT support native USDC.
            # Native USDC balance is returned as metadata for frontend advisory.
            converted_native_wei = 0
            converted_usdce_wei = int(usdc_e_wei or 0)
            if converted_usdce_wei <= 0:
                return JSONResponse(
                    {
                        "error": "no_convertible_usdc",
                        "hint": "No USDC.e balance available to convert. Native USDC cannot be converted via this path — use the bridge intake address instead.",
                        "deposit_wallet": deposit_wallet,
                        "available_usdc_native": usdc_native_float,
                        "available_usdc_e": usdc_e_float,
                    },
                    status_code=400,
                )
            calls.extend(_build_wrap_calls(_USDC_E_TOKEN, converted_usdce_wei))
            amount_wei = converted_usdce_wei
            amount_float = amount_wei / 10**6

    logger.info(
        "[T891-CONV] preparing action=%s deposit_wallet=%s amount_total=%.6f native_skipped=%.6f usdce=%.6f amount_wei=%d onramp=%s",
        action_kind,
        deposit_wallet,
        amount_float,
        usdc_native_float,
        converted_usdce_wei / 10**6,
        amount_wei,
        _COLLATERAL_ONRAMP,
    )

    # T891-PERF: Skip server-side signing attempt entirely for deposit wallet
    # conversions. The deposit wallet owner is main_wallet (external EOA), not the
    # Privy embedded wallet. Server-side signing via Privy takes 10-15 seconds and
    # always fails → always falls back to client signing. Go directly to client-sign
    # path to give wallet popup response in < 1 second.
    wallet_deployed = _is_deposit_wallet_deployed(deposit_wallet)
    logger.info(
        "[Convert][prepare] deposit_wallet=%s owner_eoa=%s deployed=%s amount_usdc=%.6f native_skipped=%.6f usdce=%.6f",
        deposit_wallet, main_wallet, wallet_deployed, amount_float,
        usdc_native_float,
        converted_usdce_wei / 10**6,
    )
    if not wallet_deployed:
        wc_payload = {
            "type": "WALLET-CREATE",
            "from": _resolve_relayer_from(main_wallet),
            "to": _pm_v2.DEPOSIT_WALLET_FACTORY,
            "metadata": "",
        }
        try:
            wc_result = _relayer_submit(wc_payload)
            logger.info(
                "[Convert][prepare] WALLET-CREATE submitted: result=%s",
                json.dumps(wc_result, default=str)[:500],
            )
        except Exception as wc_exc:
            logger.error("[Convert][prepare] WALLET-CREATE failed: %s", wc_exc)
            return JSONResponse(
                {
                    "error": "wallet_deploy_failed",
                    "message": str(wc_exc),
                    "deposit_wallet": deposit_wallet,
                },
                status_code=502,
            )
        # Poll until deployed (best-effort, max ~30s)
        for _ in range(15):
            time.sleep(2)
            if _is_deposit_wallet_deployed(deposit_wallet):
                wallet_deployed = True
                break
        if not wallet_deployed:
            return JSONResponse(
                {
                    "error": "wallet_deploying",
                    "hint": "Deposit wallet deployment in progress. Please retry in ~30 seconds.",
                    "deposit_wallet": deposit_wallet,
                },
                status_code=202,
            )

    # Build typed_data for client-side signing and return immediately.
    nonce = _get_relayer_nonce(main_wallet, "WALLET")
    deadline = int(time.time()) + 600
    typed_data = _build_deposit_wallet_batch_typed_data(
        deposit_wallet=deposit_wallet,
        nonce=nonce,
        deadline=deadline,
        calls=calls,
    )
    return JSONResponse(
        {
            "requires_client_sign": True,
            "requires_wallet_deploy": False,
            "signing_address": _as_checksum_address(main_wallet),
            "typed_data": typed_data,
            "nonce": str(nonce),
            "depositWalletParams": {
                "depositWallet": _as_checksum_address(deposit_wallet),
                "deadline": str(deadline),
                "calls": calls,
            },
            "amount_usdc": amount_float,
            "amount_wei": str(amount_wei),
            "amount_usdc_native": converted_native_wei / 10**6,
            "amount_usdc_native_skipped": usdc_native_float,
            "amount_usdc_e": converted_usdce_wei / 10**6,
            "action": action_kind,
        }
    )


@router.post("/api/deposit/usdc-to-pusd/submit")
async def api_deposit_usdc_to_pusd_submit(request: Request):
    """Deprecated in T893: conversion flow removed from /v2/withdraw and deposit path."""
    return JSONResponse(
        {
            "error": "deprecated_removed_t893",
            "message": "USDC conversion endpoint has been removed. Use direct pUSD flow.",
        },
        status_code=410,
    )
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "not authenticated"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    user_wallet = str(identity.get("wallet_address") or "")
    binding = _get_user_signer_binding(user_wallet, require_wallets=True)
    if not binding:
        return JSONResponse({"error": "wallet not bound"}, status_code=412)

    main_wallet = str(binding.get("main_wallet") or "").strip()
    deposit_wallet = str(binding.get("proxy_wallet") or "").strip()

    signature = str(body.get("signature") or "").strip()
    nonce = str(body.get("nonce") or "").strip()
    dw_params = body.get("depositWalletParams") or {}
    calls = dw_params.get("calls") or []
    deadline = str(dw_params.get("deadline") or "")

    if not signature or not nonce or not calls:
        return JSONResponse(
            {"error": "signature, nonce, and depositWalletParams.calls required"},
            status_code=400,
        )

    # Per Polymarket spec: WALLET batches require the deposit wallet to be
    # registered with the relayer. If undeployed, submit WALLET-CREATE first.
    # With HMAC builder auth, `from = owner EOA` is accepted.
    wallet_deployed = _is_deposit_wallet_deployed(deposit_wallet)
    logger.info(
        "[Convert][submit] deposit_wallet=%s owner_eoa=%s deployed=%s nonce=%s",
        deposit_wallet, main_wallet, wallet_deployed, nonce,
    )
    if not wallet_deployed:
        wc_payload = {
            "type": "WALLET-CREATE",
            "from": _resolve_relayer_from(main_wallet),
            "to": _pm_v2.DEPOSIT_WALLET_FACTORY,
            "metadata": "",
        }
        try:
            _relayer_submit(wc_payload)
        except Exception as wc_exc:
            logger.error("[Convert][submit] WALLET-CREATE failed: %s", wc_exc)
        return JSONResponse(
            {
                "error": "wallet_deploying",
                "hint": "Deposit wallet deployment in progress. Please retry in ~30 seconds.",
                "deposit_wallet": deposit_wallet,
            },
            status_code=202,
        )

    # Relayer-v2 enforces `from == auth address` on every payload.
    # Ownership is proven via the EIP-712 Batch signature inside depositWalletParams.
    relayer_payload = {
        "type": "WALLET",
        "from": _resolve_relayer_from(main_wallet),
        "to": _pm_v2.DEPOSIT_WALLET_FACTORY,
        "nonce": nonce,
        "signature": signature,
        "depositWalletParams": {
            "depositWallet": _as_checksum_address(deposit_wallet),
            "deadline": deadline,
            "calls": calls,
        },
        "metadata": "",
    }

    logger.info(
        "[Convert][submit] deposit_wallet=%s owner_eoa=%s nonce=%s",
        deposit_wallet, main_wallet, nonce,
    )
    logger.info("[Convert][submit] wire_payload type=%s from=%s depositWallet=%s",
        relayer_payload.get("type"), relayer_payload.get("from"),
        (relayer_payload.get("depositWalletParams") or {}).get("depositWallet"),
    )

    try:
        relayer_result = _relayer_submit(relayer_payload)
    except Exception as exc:
        logger.error("[T884-CONV] submit relayer failed: %s", exc)
        payload, status = _build_tx_error_response(exc, "usdc_to_pusd_submit")
        return JSONResponse(payload, status_code=status)

    logger.info("[Convert][submit] RELAYER_RESPONSE: %s", json.dumps(relayer_result, default=str))
    logger.info("[T884-CONV] client-signed conversion submitted: deposit_wallet=%s", deposit_wallet)
    return JSONResponse({"success": True, "relayer_result": relayer_result})


@router.get("/api/deposit/bridge-addresses")
async def api_get_bridge_deposit_addresses(request: Request):
    """T884-BRIDGE: Return the Polymarket Bridge intake addresses for the
    current user's deposit wallet, plus current bridge transaction history.

    Users send USDC / USDT / etc. on any supported chain to the matching
    intake address; the Polymarket Bridge backend automatically converts and
    credits pUSD to the deposit wallet. This is the canonical Builder-side
    deposit flow per the official docs and works for native USDC, USDC.e,
    USDT, and other supported assets — no on-chain calls needed from us.
    """
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "not authenticated"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "")
    binding = _get_user_signer_binding(user_wallet, require_wallets=True)
    if not binding:
        return JSONResponse({"error": "wallet not bound"}, status_code=412)

    deposit_wallet = str(binding.get("proxy_wallet") or "").strip()
    if not _ADDR_RE.match(deposit_wallet.lower()):
        return JSONResponse({"error": "deposit_wallet address invalid"}, status_code=500)

    try:
        addrs = await asyncio.to_thread(_bridge_create_deposit_addresses, deposit_wallet)
    except Exception as exc:
        logger.error("[T884-BRIDGE] addresses fetch failed: %s", exc)
        return JSONResponse(
            {"error": "bridge_addresses_unavailable", "message": str(exc)},
            status_code=502,
        )

    status_payload: dict[str, Any] = {"transactions": []}
    include_status = str(request.query_params.get("include_status") or "0").strip().lower()
    should_fetch_status = include_status not in {"0", "false", "no", "off"}
    bridge_evm = str(addrs.get("evm") or "")
    if should_fetch_status and bridge_evm:
        try:
            status_payload = await asyncio.to_thread(
                _bridge_get_deposit_status,
                bridge_evm,
                [deposit_wallet],
            )
        except Exception as exc:
            logger.warning("[T884-BRIDGE] status fetch failed: %s", exc)
            status_payload = {"transactions": [], "error": str(exc)}

    return JSONResponse(
        {
            "deposit_wallet": _as_checksum_address(deposit_wallet),
            "addresses": addrs,
            "status": status_payload,
        }
    )
