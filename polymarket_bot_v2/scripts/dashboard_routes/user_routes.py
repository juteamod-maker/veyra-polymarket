"""
user_routes.py — 用户认证与租户设置路由（Privy + Manual User）

公开端点:
  - GET  /api/user/privy/config
  - POST /api/user/privy/verify
  - GET  /api/user/me
  - POST /api/user/logout

登录后端点:
  - GET  /api/user/pm-wallet/status
  - POST /api/user/pm-wallet/bind
  - GET  /api/user/runtime-control
  - POST /api/user/runtime-control
"""

from __future__ import annotations

import csv
from html import escape as html_escape
import hashlib
import io
import json
import logging
import os
import re
import secrets
import time
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from src.utils.pm_http import pm_session as _pm_session  # T510 P0-1
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from scripts.dashboard_routes._shared import (
    USER_SESSION_DAYS,
    _ensure_user_tables,
    _parse_env_file,
    create_user_session,
    db_connect,
    escape_script_json,
    generate_totp_secret,
    get_request_identity,
    ip_rate_limiter,
    mfa_cookie_sign,
    mfa_cookie_verify,
    revoke_user_session,
    totp_provisioning_uri,
    totp_qr_svg,
    verify_totp,
)
from src.utils.config_loader import get as cfg_get
import asyncio

router = APIRouter()
logger = logging.getLogger("user_routes")

# T911 wave 4 perf: 10s TTL cache for /api/user/pm/metrics. Keyed by lowercased
# wallet_address. Skipped when ?refresh=1.
_PM_METRICS_CACHE: dict[str, tuple[float, dict]] = {}
_PM_METRICS_TTL = 10.0

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# T901: 模板中嵌入 <script> 的 JSON 统一走 escape_script_json，防止 </script> 逃逸 XSS。
templates.env.filters["escape_script_json"] = escape_script_json

_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_DATA_API = "https://data-api.polymarket.com"
_GAMMA_API = "https://gamma-api.polymarket.com"
_POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
_DEFAULT_POLYGON_RPCS = [
    # T290 fix: keep a resilient RPC fallback set.
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
]


def _polygon_rpcs() -> list[str]:
    try:
        cfg = cfg_get("chain.polygon_rpcs")
        if isinstance(cfg, list) and cfg:
            return [str(x) for x in cfg if str(x).startswith("http")]
    except Exception:
        pass
    return list(_DEFAULT_POLYGON_RPCS)


_POLYGON_RPCS = _DEFAULT_POLYGON_RPCS  # backward-compat module attr
# Single source of truth: src.utils.pm_v2_constants.USDC_NATIVE_TOKEN.
# Local alias kept for backwards compat with existing callers in this module.
from src.utils import pm_v2_constants as _pm_v2_consts

_USDC_NATIVE_TOKEN = _pm_v2_consts.USDC_NATIVE_TOKEN
_DATA_API_PAGE_LIMIT = 500
_DATA_API_MAX_OFFSET = 10000


def _safe_float(v, default: float = 0.0) -> float:
    try:
        fv = float(v)
        if fv != fv:
            return default
        return fv
    except Exception:
        return default


def _safe_row_get(row, key, default=None):
    """安全地从 PG Row / dict 中取键，缺失或异常时返回 default。"""
    if row is None:
        return default
    try:
        if hasattr(row, "keys") and key in row.keys():
            return row[key]
    except Exception:
        pass
    try:
        return row[key]
    except Exception:
        return default


def _safe_iso(v):
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            return None
    return str(v)


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _normalize_unix_ts(v, default: int = 0) -> int:
    ts = _safe_int(v, default)
    if ts > 10_000_000_000:
        ts = ts // 1000
    return ts


def _pick_first_number(obj: dict, keys: list[str]) -> float | None:
    if not isinstance(obj, dict):
        return None
    for k in keys:
        v = obj.get(k)
        if v is None:
            continue
        try:
            fv = float(v)
            if fv == fv:
                return fv
        except Exception:
            continue
    return None


def _parse_iso(ts: str) -> datetime | None:
    try:
        if not ts:
            return None
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _ensure_email_verification_table() -> None:
    with db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_email_verifications (
                id SERIAL PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                email TEXT NOT NULL,
                purpose TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 6,
                consumed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_email_verify_wallet_purpose
            ON user_email_verifications(wallet_address, purpose, created_at DESC);
            """)


def _hash_email_code(code: str, salt: str, wallet: str, purpose: str) -> str:
    payload = f"{code}:{salt}:{wallet.lower()}:{purpose}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# T327: SMTP 邮件发送已弃用。原 _load_smtp_runtime_config / _send_email_code
# 整段删除（无任何调用方），通用管理 · API/密钥管理 中 SMTP_* 字段同步移除。
# 邮箱验证码改为返回给前端展示或走第三方推送，不再依赖本地 SMTP。


def _discover_pm_wallet_from_email(email: str) -> str:
    """Best-effort PM wallet lookup by email across known public endpoints."""
    email = str(email or "").strip().lower()
    if not email or "@" not in email:
        return ""

    candidate_endpoints = [
        (f"{_DATA_API}/users", {"email": email}),
        (f"{_GAMMA_API}/users", {"email": email}),
        (f"{_DATA_API}/user", {"email": email}),
        (f"{_GAMMA_API}/user", {"email": email}),
    ]

    for url, params in candidate_endpoints:
        payload = _http_get_json(url, params=params, timeout=8)
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = [payload]
        else:
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in (
                "proxyWallet",
                "proxy_wallet",
                "walletAddress",
                "wallet_address",
                "address",
            ):
                val = str(item.get(key) or "").strip().lower()
                if _ADDR_RE.match(val):
                    return val
    return ""


def _ensure_pm_multi_tenant_tables() -> None:
    with db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_pm_profiles (
                wallet_address TEXT PRIMARY KEY,
                login_wallet TEXT,
                polymarket_wallet TEXT,
                proxy_wallet TEXT,
                linked_wallets_json TEXT,
                discovery_method TEXT,
                discovery_confidence REAL NOT NULL DEFAULT 0,
                profile_json TEXT,
                last_synced_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_pm_equity_snapshots (
                id SERIAL PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                snapshot_ts TEXT NOT NULL,
                polymarket_wallet TEXT,
                portfolio_value REAL NOT NULL DEFAULT 0,
                usdc_balance REAL,
                open_positions_count INTEGER NOT NULL DEFAULT 0,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0,
                raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS user_pm_positions_snapshots (
                id SERIAL PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                snapshot_ts TEXT NOT NULL,
                polymarket_wallet TEXT,
                condition_id TEXT,
                asset TEXT,
                title TEXT,
                outcome TEXT,
                size REAL,
                avg_price REAL,
                initial_value REAL,
                current_value REAL,
                cash_pnl REAL,
                percent_pnl REAL,
                cur_price REAL,
                event_id TEXT,
                slug TEXT,
                event_slug TEXT,
                end_date TEXT,
                raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS user_pm_activity_snapshots (
                id SERIAL PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                snapshot_ts TEXT NOT NULL,
                polymarket_wallet TEXT,
                activity_type TEXT,
                condition_id TEXT,
                asset TEXT,
                title TEXT,
                outcome TEXT,
                size REAL,
                usdc_size REAL,
                price REAL,
                event_id TEXT,
                event_slug TEXT,
                activity_ts INTEGER,
                tx_hash TEXT,
                raw_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_user_pm_equity_wallet_ts
                ON user_pm_equity_snapshots(wallet_address, snapshot_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_user_pm_positions_wallet_ts
                ON user_pm_positions_snapshots(wallet_address, snapshot_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_user_pm_activity_wallet_ts
                ON user_pm_activity_snapshots(wallet_address, snapshot_ts DESC);
            """)


def _http_get_json(url: str, params: dict | None = None, timeout: int = 10):
    try:
        resp = _pm_session().get(url, params=params or {}, timeout=timeout)
        if not resp.ok:
            return None
        return resp.json()
    except Exception:
        return None


def _normalize_value_payload(payload) -> dict:
    if isinstance(payload, list):
        return payload[0] if payload else {}
    if isinstance(payload, dict):
        return payload
    return {}


def _fetch_positions(user_addr: str) -> list[dict]:
    payload = _http_get_json(
        f"{_DATA_API}/positions",
        params={"user": user_addr, "sizeThreshold": 0, "limit": 500},
        timeout=10,
    )
    return payload if isinstance(payload, list) else []


def _fetch_activity(user_addr: str) -> list[dict]:
    payload = _http_get_json(
        f"{_DATA_API}/activity",
        params={"user": user_addr, "limit": 100, "offset": 0},
        timeout=10,
    )
    return payload if isinstance(payload, list) else []


def _fetch_full_activity(user_addr: str, start_ts: int | None = None) -> list[dict]:
    rows: list[dict] = []
    offset = 0

    while offset <= _DATA_API_MAX_OFFSET:
        params = {
            "user": user_addr,
            "limit": _DATA_API_PAGE_LIMIT,
            "offset": offset,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        if start_ts and start_ts > 0:
            params["start"] = max(0, int(start_ts) - 86400)

        payload = _http_get_json(f"{_DATA_API}/activity", params=params, timeout=15)
        batch = payload if isinstance(payload, list) else []
        if not batch:
            break

        rows.extend(batch)
        if len(batch) < _DATA_API_PAGE_LIMIT:
            break
        offset += _DATA_API_PAGE_LIMIT

    return rows


def _fetch_closed_positions_full(user_addr: str, limit: int = 3000) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    page_size = 50

    while len(rows) < limit and offset <= 100000:
        payload = _http_get_json(
            f"{_DATA_API}/closed-positions",
            params={
                "user": user_addr,
                "limit": page_size,
                "offset": offset,
                "sortBy": "TIMESTAMP",
                "sortDirection": "DESC",
            },
            timeout=15,
        )
        batch = payload if isinstance(payload, list) else []
        if not batch:
            break

        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    return rows[:limit]


def _fetch_value(user_addr: str) -> dict:
    payload = _http_get_json(
        f"{_DATA_API}/value",
        params={"user": user_addr},
        timeout=10,
    )
    return _normalize_value_payload(payload)


def _fetch_accounting_snapshot(user_addr: str) -> dict:
    try:
        resp = _pm_session().get(
            f"{_DATA_API}/v1/accounting/snapshot",
            params={"user": user_addr},
            timeout=20,
        )
        if not resp.ok:
            return {}
        with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
            equity_rows: list[dict] = []
            positions_rows: list[dict] = []

            if "equity.csv" in archive.namelist():
                equity_text = archive.read("equity.csv").decode(
                    "utf-8", errors="replace"
                )
                equity_rows = list(csv.DictReader(io.StringIO(equity_text)))
            if "positions.csv" in archive.namelist():
                positions_text = archive.read("positions.csv").decode(
                    "utf-8", errors="replace"
                )
                positions_rows = list(csv.DictReader(io.StringIO(positions_text)))

        equity = equity_rows[0] if equity_rows else {}
        return {
            "cash_balance": _safe_float(equity.get("cashBalance"), 0.0),
            "positions_value": _safe_float(equity.get("positionsValue"), 0.0),
            "equity": _safe_float(equity.get("equity"), 0.0),
            "valuation_time": equity.get("valuationTime") or "",
            "positions_count": len(positions_rows),
            "positions_rows": positions_rows,
        }
    except Exception:
        return {}


def _extract_balance_from_value_payload(value_payload: dict) -> float | None:
    return _pick_first_number(
        value_payload,
        [
            "usdc_balance",
            "usdcBalance",
            "cash",
            "usdc",
            "balance",
            "available",
            "availableBalance",
            "walletBalance",
        ],
    )


def _extract_positions_value_from_value_payload(value_payload: dict) -> float:
    v = _pick_first_number(
        value_payload,
        ["value", "portfolioValue", "positionsValue", "netValue", "nlv"],
    )
    return _safe_float(v, 0.0)


def _estimate_balance_from_config(positions_value: float) -> float:
    live_initial = _safe_float(cfg_get("general.live_initial_deposit") or 0.0, 0.0)
    if live_initial <= 0:
        return 0.0
    return max(0.0, live_initial - max(0.0, positions_value))


def _latest_known_activity_ts(wallet_address: str) -> int:
    _ensure_pm_multi_tenant_tables()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT MAX(activity_ts) AS max_ts FROM user_pm_activity_snapshots WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()
    return _safe_int((dict(row) if row else {}).get("max_ts"), 0)


def _stored_activity_count(wallet_address: str) -> int:
    _ensure_pm_multi_tenant_tables()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM user_pm_activity_snapshots WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()
    return _safe_int((dict(row) if row else {}).get("c"), 0)


def _fetch_public_profile(wallet_addr: str) -> dict:
    payload = _http_get_json(
        f"{_GAMMA_API}/public-profile",
        params={"address": wallet_addr},
        timeout=10,
    )
    return payload if isinstance(payload, dict) else {}


def _fetch_erc20_balance(wallet_addr: str, token_address: str) -> float | None:
    """Read Polygon ERC20 balance via eth_call(balanceOf)."""
    addr = wallet_addr.lower().replace("0x", "")
    if len(addr) != 40:
        return None
    data = "0x70a08231" + ("0" * 24) + addr
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {"to": token_address, "data": data},
            "latest",
        ],
        "id": 1,
    }
    for rpc in _polygon_rpcs():
        try:
            resp = requests.post(rpc, json=payload, timeout=8)
            if not resp.ok:
                continue
            body = resp.json() or {}
            if body.get("error"):
                continue
            result = body.get("result", "0x0")
            if not isinstance(result, str) or not result.startswith("0x"):
                continue
            raw = int(result, 16)
            return raw / 1_000_000.0
        except Exception:
            continue
    return None


def _fetch_usdc_balances(wallet_addr: str) -> dict[str, float | None]:
    # T471 / T882-bugfix: include pUSD (post-2026-04-28 trading collateral),
    # USDC.e (legacy bridged USDC) AND native USDC (Circle), so that any
    # stablecoin a user funded their deposit wallet with is surfaced as
    # tradable balance. The deposit-wallet path auto-routes USDC.e and
    # native USDC into pUSD via CollateralOnramp on first dispatch.
    try:
        from src.utils import pm_v2_constants as _pm_v2  # noqa: PLC0415

        pusd = _fetch_erc20_balance(wallet_addr, _pm_v2.PUSD_TOKEN)
        usdc_e = _fetch_erc20_balance(wallet_addr, _pm_v2.USDC_E_TOKEN)
    except Exception:
        pusd = None
        usdc_e = None
    usdc_native = _fetch_erc20_balance(wallet_addr, _USDC_NATIVE_TOKEN)
    parts = [v for v in (pusd, usdc_e, usdc_native) if v is not None]
    total = sum(parts) if parts else None
    return {
        "pusd": pusd,
        "usdc_e": usdc_e,
        "usdc_native": usdc_native,
        "usdc_total": total,
    }


def _discover_pm_wallet(login_wallet: str, linked_wallets: list[str]) -> dict:
    """Discover the most likely PM wallet (proxy wallet preferred) from login context."""
    checked = []
    seen = set()

    for w in [login_wallet] + list(linked_wallets or []):
        ww = str(w or "").strip().lower()
        if _ADDR_RE.match(ww) and ww not in seen:
            checked.append(ww)
            seen.add(ww)

    profile_candidates = []
    for w in checked:
        prof = _fetch_public_profile(w)
        proxy = str(prof.get("proxyWallet") or "").strip().lower()
        # Deposit Wallet Migration (May 2026): new API users get a deposit wallet
        # instead of a proxy/Safe. The profile may return "depositWallet" for them.
        deposit_wallet_addr = str(prof.get("depositWallet") or "").strip().lower()
        if _ADDR_RE.match(proxy) and proxy not in seen:
            checked.append(proxy)
            seen.add(proxy)
        if prof:
            profile_candidates.append((w, prof, proxy, deposit_wallet_addr))

    best = {
        "pm_wallet": "",
        "proxy_wallet": "",
        "deposit_wallet": "",
        "wallet_type": "safe",  # 'safe' or 'deposit_wallet'
        "method": "none",
        "confidence": 0.0,
        "profile": {},
        "value": {},
        "positions": [],
        "activity": [],
    }

    for cand in checked:
        value = _fetch_value(cand)
        positions = _fetch_positions(cand)
        activity = _fetch_activity(cand)

        score = 0.0
        val_num = _safe_float(value.get("value"), 0.0)
        if val_num > 0:
            score += min(val_num, 1000.0) * 0.05
        if positions:
            score += 50.0 + min(len(positions), 50)
        if activity:
            score += 10.0

        proxy_from_profile = ""
        deposit_wallet_from_profile = ""
        profile_obj = {}
        for src, prof, proxy, dw in profile_candidates:
            if cand in (src, proxy, dw):
                proxy_from_profile = proxy
                deposit_wallet_from_profile = dw
                profile_obj = prof
                break
        if proxy_from_profile and cand == proxy_from_profile:
            score += 35.0
        elif deposit_wallet_from_profile and cand == deposit_wallet_from_profile:
            score += 35.0

        if score > best["confidence"]:
            # T499++ Wave 6B CRITICAL FIX: never fall back proxy_wallet to the
            # EOA. The Polymarket Gnosis Safe (proxyWallet) is a SEPARATE
            # contract address deployed by Polymarket on first contract write.
            # If Polymarket's public profile / Data API does not surface a
            # proxyWallet for this EOA, the user simply does not have a
            # Polymarket Safe deployed yet — we must report that honestly
            # rather than pretending the EOA is the Safe.
            real_proxy = (
                proxy_from_profile
                if (proxy_from_profile and proxy_from_profile != cand)
                else ""
            )
            real_deposit_wallet = (
                deposit_wallet_from_profile
                if (deposit_wallet_from_profile and deposit_wallet_from_profile != cand)
                else ""
            )
            # Determine wallet_type: if only a deposit wallet is present (no proxy),
            # or if the profile explicitly provides depositWallet, classify accordingly.
            if real_deposit_wallet and not real_proxy:
                wtype = "deposit_wallet"
            else:
                wtype = "safe"
            best = {
                "pm_wallet": cand,
                "proxy_wallet": real_proxy,
                "deposit_wallet": real_deposit_wallet,
                "wallet_type": wtype,
                "method": (
                    "public_profile+data_api" if (real_proxy or real_deposit_wallet) else "data_api_only"
                ),
                "confidence": score,
                "profile": profile_obj,
                "value": value,
                "positions": positions,
                "activity": activity,
            }

    if not best["pm_wallet"] and profile_candidates:
        src, prof, proxy, dw = profile_candidates[0]
        # T499++ Wave 6B CRITICAL FIX: same rule — proxy_wallet must be a real,
        # distinct Gnosis Safe address from Polymarket; never fall back to EOA.
        real_proxy = proxy if (proxy and proxy != src) else ""
        real_dw = dw if (dw and dw != src) else ""
        wtype = "deposit_wallet" if (real_dw and not real_proxy) else "safe"
        best = {
            "pm_wallet": real_proxy or real_dw or src,
            "proxy_wallet": real_proxy,
            "deposit_wallet": real_dw,
            "wallet_type": wtype,
            "method": "public_profile_only" if (real_proxy or real_dw) else "public_profile_no_safe",
            "confidence": 5.0,
            "profile": prof,
            "value": {},
            "positions": [],
            "activity": [],
        }

    return best


def _ensure_runtime_table() -> None:
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_runtime_controls (
                wallet_address TEXT PRIMARY KEY,
                freeze_auto INTEGER NOT NULL DEFAULT 0,
                auto_trading_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """)


def _extract_wallets_from_privy_user(privy_user: dict) -> tuple[list[str], str]:
    """Return (all_addresses, primary_wallet).

    primary_wallet selection rules (T462):
      1. The first verified EXTERNAL wallet (non-Privy embedded) is preferred,
         because that EOA is the actual owner of the user's on-chain Polymarket
         Safe / proxy. Using it as the primary key keeps the user row stable
         across logins and avoids duplicate user rows being created after a
         Privy embedded wallet is provisioned later.
      2. If the user has no external wallet, fall back to the Privy embedded
         wallet (true embedded-only users such as email/social login).
      3. Order is preserved from Privy's linked_accounts response.
    """
    wallets: list[str] = []
    external_wallet = ""
    embedded_wallet = ""
    # Privy REST API returns camelCase (linkedAccounts); React SDK also camelCase.
    # Accept both conventions defensively.
    linked = privy_user.get("linked_accounts") or privy_user.get("linkedAccounts") or []
    for acc in linked:
        if not isinstance(acc, dict):
            continue
        addr = str(acc.get("address") or "").strip().lower()
        if not _ADDR_RE.match(addr):
            continue
        if addr not in wallets:
            wallets.append(addr)
        wallet_client_type = (
            str(acc.get("wallet_client_type") or acc.get("walletClientType") or "")
            .strip()
            .lower()
        )
        if wallet_client_type == "privy":
            if not embedded_wallet:
                embedded_wallet = addr
        else:
            if not external_wallet:
                external_wallet = addr
    if not wallets:
        # Also check top-level wallet fields
        for field in ("wallet_address", "walletAddress"):
            direct_addr = str(privy_user.get(field) or "").strip().lower()
            if _ADDR_RE.match(direct_addr):
                wallets.append(direct_addr)
                break
    preferred_wallet = (
        external_wallet or embedded_wallet or (wallets[0] if wallets else "")
    )
    return wallets, preferred_wallet


def _extract_embedded_wallet_binding(privy_user: dict) -> tuple[str, str]:
    linked = privy_user.get("linked_accounts") or privy_user.get("linkedAccounts") or []
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
        wallet_id = str(
            acc.get("id") or acc.get("wallet_id") or acc.get("walletId") or ""
        ).strip()
        if _ADDR_RE.match(addr) and not fallback_addr:
            fallback_addr = addr
        if wallet_client_type == "privy" and _ADDR_RE.match(addr):
            return addr, wallet_id
    return fallback_addr, ""


def _split_csv_env(raw: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[;,\n]", str(raw or "")):
        val = str(part or "").strip()
        if val and val not in out:
            out.append(val)
    return out


def _load_privy_server_signer_config() -> dict[str, object]:
    signer_id = str(
        os.environ.get("PRIVY_SERVER_SIGNER_ID")
        or os.environ.get("PRIVY_SIGNER_ID")
        or ""
    ).strip()
    policy_ids = _split_csv_env(
        str(
            os.environ.get("PRIVY_SERVER_SIGNER_POLICY_IDS")
            or os.environ.get("PRIVY_SIGNER_POLICY_IDS")
            or ""
        )
    )
    return {
        "enabled": bool(signer_id),
        "signer_id": signer_id,
        "policy_ids": policy_ids,
    }


def _attempt_server_signer_enrollment(
    *,
    embedded_wallet_id: str,
    user_jwt: str | list[str] | tuple[str, ...],
) -> tuple[str, list[str], str]:
    signer_cfg = _load_privy_server_signer_config()
    signer_id = str(signer_cfg.get("signer_id") or "").strip()
    policy_ids = [
        str(x).strip() for x in (signer_cfg.get("policy_ids") or []) if str(x).strip()
    ]

    if not signer_id:
        return "", [], ""
    wallet_id = str(embedded_wallet_id or "").strip()
    if not wallet_id:
        return "", policy_ids, "missing embedded wallet id"

    if isinstance(user_jwt, (list, tuple, set)):
        jwt_candidates = [_normalize_auth_token(x) for x in user_jwt]
    else:
        jwt_candidates = [_normalize_auth_token(user_jwt)]
    jwt_candidates = [x for x in jwt_candidates if x]
    if not jwt_candidates:
        return "error", policy_ids, "missing user jwt for signer enrollment"

    try:
        from scripts.dashboard_routes import (
            redeem_withdraw_routes as _redeem_routes,
        )  # noqa: PLC0415

        if _redeem_routes._privy_wallet_has_additional_signer(wallet_id, signer_id):
            return "enrolled", policy_ids, ""

        auth_keys = _redeem_routes._load_privy_user_authorization_keys(jwt_candidates)
        _redeem_routes._privy_ensure_wallet_additional_signer(
            wallet_id,
            signer_id,
            policy_ids,
            auth_keys=auth_keys,
        )
        return "enrolled", policy_ids, ""
    except Exception as exc:
        return "error", policy_ids, str(exc)[:500]


DISPLAY_NAME_MIN_LEN = 2
DISPLAY_NAME_MAX_LEN = 40
_DISPLAY_NAME_FORBIDDEN_CHARS = set('<>{}[]()`$;:&|\\/"')
_DISPLAY_NAME_ALLOWED_PUNCT = {" ", ".", "_", "-", "'"}


def _normalize_display_name(raw: str) -> str:
    text = unicodedata.normalize("NFKC", str(raw or ""))
    # Strip control chars and collapse whitespace to a single space.
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _validate_display_name(raw: str, *, allow_empty: bool = True) -> tuple[str, str | None]:
    name = _normalize_display_name(raw)
    if not name:
        return ("", None) if allow_empty else ("", "display name is required")

    if len(name) < DISPLAY_NAME_MIN_LEN or len(name) > DISPLAY_NAME_MAX_LEN:
        return (
            "",
            f"display name must be {DISPLAY_NAME_MIN_LEN}-{DISPLAY_NAME_MAX_LEN} characters",
        )

    if any(ch in _DISPLAY_NAME_FORBIDDEN_CHARS for ch in name):
        return ("", "display name contains forbidden characters")

    for ch in name:
        if ch in _DISPLAY_NAME_ALLOWED_PUNCT:
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("L") or cat.startswith("N"):
            continue
        return (
            "",
            "display name can only contain letters, numbers, spaces, dot, underscore, apostrophe, and dash",
        )

    return name, None


_ONBOARD_I18N_CACHE: dict[str, tuple[float, dict]] = {}
# T875 — separate cache for the dedicated 6-step onboarding pack.
_ONBOARDING_PACK_CACHE: dict[str, tuple[float, dict]] = {}


def _detect_lang(request: Request) -> str:
    raw = (request.cookies.get("veyra_lang") or "").strip()
    supported = {"zh-CN", "en", "fr", "ja", "ko", "th", "vi", "es", "pt"}
    if raw in supported:
        return raw
    return "en"


def _load_i18n_dict(lang: str) -> dict:
    path = FRONTEND_DIR / "static" / "i18n" / f"{lang}.json"
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
        cached = _ONBOARD_I18N_CACHE.get(lang)
        if cached and cached[0] == mtime:
            return cached[1]
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        _ONBOARD_I18N_CACHE[lang] = (mtime, data)
        return data
    except Exception:
        return {}


def _load_onboarding_pack(lang: str) -> dict:
    """T875 — Load per-language pack for the new-user onboarding modal.

    Files live under static/i18n/onboarding/<lang>.json so the main app
    bundle stays slim. Falls back to en.json on any miss.
    """
    base_dir = FRONTEND_DIR / "static" / "i18n" / "onboarding"

    def _read(lang_key: str) -> dict | None:
        path = base_dir / f"{lang_key}.json"
        if not path.exists():
            return None
        try:
            mtime = path.stat().st_mtime
            cached = _ONBOARDING_PACK_CACHE.get(lang_key)
            if cached and cached[0] == mtime:
                return cached[1]
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            _ONBOARDING_PACK_CACHE[lang_key] = (mtime, data)
            return data
        except Exception:
            return None

    pack = _read(lang) or {}
    if not pack:
        pack = _read("en") or {}
    elif lang != "en":
        # Merge with en fallback so missing keys never break the UI.
        en_pack = _read("en") or {}
        merged = dict(en_pack)
        merged.update(pack)
        pack = merged
    return pack


def _needs_onboarding(display_name: str | None, wallet_address: str | None) -> bool:
    name = str(display_name or "").strip()
    wallet = str(wallet_address or "").strip()
    if not wallet:
        return False
    if not name:
        return True
    return name.lower() == wallet.lower()


def _is_wallet_style_name(name: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", str(name or "").strip()))


def _is_display_name_taken(display_name: str, wallet_address: str) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM users
            WHERE LOWER(COALESCE(display_name, '')) = LOWER(?)
              AND LOWER(wallet_address) != LOWER(?)
            LIMIT 1
            """,
            (display_name, wallet_address),
        ).fetchone()
    return bool(row)


def _fetch_current_display_name(wallet_address: str) -> str:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(display_name, '') AS display_name FROM users WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()
    return str((row["display_name"] if row else "") or "").strip()


def _extract_profile_from_privy_user(privy_user: dict) -> dict:
    profile = {
        "display_name": "",
        "avatar_url": "",
        "email": "",
        "email_verified": 0,
    }

    if not isinstance(privy_user, dict):
        return profile

    for key in ("display_name", "displayName", "name"):
        val = str(privy_user.get(key) or "").strip()
        if val:
            sanitized, err = _validate_display_name(val, allow_empty=True)
            if not err and sanitized:
                profile["display_name"] = sanitized
            break

    for key in ("avatar_url", "avatarUrl", "profilePictureUrl", "picture"):
        val = str(privy_user.get(key) or "").strip()
        if val:
            profile["avatar_url"] = val[:500]
            break

    linked = privy_user.get("linked_accounts") or privy_user.get("linkedAccounts") or []
    for acc in linked:
        if not isinstance(acc, dict):
            continue
        typ = str(acc.get("type") or "").lower()
        # T499 Wave 5C: also harvest email surfaced via OAuth providers so
        # _discover_pm_wallet_from_email can match an existing Polymarket
        # account that was created via the same email/google identity.
        # Telegram does not expose an email and is intentionally skipped.
        if typ not in ("email", "google_oauth", "apple_oauth", "github_oauth"):
            continue

        email_val = str(acc.get("email") or acc.get("address") or "").strip()
        if email_val and not profile["email"]:
            profile["email"] = email_val[:255]

        verified_val = acc.get("verified")
        if isinstance(verified_val, bool):
            profile["email_verified"] = 1 if verified_val else 0
        elif isinstance(verified_val, str):
            profile["email_verified"] = 1 if verified_val.lower() == "true" else 0

    return profile


def _load_privy_secrets() -> list[str]:
    """Load one or more Privy app secrets from env for key rotation."""
    secrets: list[str] = []

    # Preferred: single env with multiple secrets, separated by comma/semicolon/newline.
    combined = str(os.environ.get("PRIVY_APP_SECRETS", "") or "").strip()
    if combined:
        for part in re.split(r"[;,\n]", combined):
            s = part.strip()
            if s and s not in secrets:
                secrets.append(s)

    # Compatibility: keep supporting single-secret and common backup/indexed names.
    for key in (
        "PRIVY_APP_SECRET",
        "PRIVY_APP_SECRET_ALT",
        "PRIVY_APP_SECRET_BACKUP",
        "PRIVY_APP_SECRET_SECONDARY",
        "PRIVY_APP_SECRET_1",
        "PRIVY_APP_SECRET_2",
        "PRIVY_APP_SECRET_3",
    ):
        s = str(os.environ.get(key, "") or "").strip()
        if not s:
            continue
        if any(ch in s for ch in ",;\n"):
            for part in re.split(r"[;,\n]", s):
                candidate = part.strip()
                if candidate and candidate not in secrets:
                    secrets.append(candidate)
            continue
        if s and s not in secrets:
            secrets.append(s)

    return secrets


def _get_privy_verification_key():
    """Return the EC public key object for Privy JWT verification (ES256).

    Priority:
    1. File: config/privy_verification_key.pem  (no encoding issues)
    2. Env:  PRIVY_VERIFICATION_KEY             (with \\n normalisation fallback)
    """
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    # 1. Try PEM file first (most reliable, no escape issues)
    pem_file = os.path.join(
        os.path.dirname(__file__), "..", "..", "config", "privy_verification_key.pem"
    )
    pem_file = os.path.normpath(pem_file)
    if os.path.isfile(pem_file):
        with open(pem_file, "rb") as f:
            return load_pem_public_key(f.read())

    # 2. Fall back to env var
    raw = os.environ.get("PRIVY_VERIFICATION_KEY", "").strip()
    if not raw:
        raise ValueError(
            "Privy verification key not found: "
            "place config/privy_verification_key.pem or set PRIVY_VERIFICATION_KEY"
        )
    # Normalise literal \\n sequences to real newlines (handles various .env formats)
    pem = raw.replace("\\n", "\n")
    if not pem.startswith("-----"):
        raise ValueError("PRIVY_VERIFICATION_KEY does not look like a PEM key")
    return load_pem_public_key(pem.encode())


def _verify_privy_jwt(token: str, app_id: str) -> dict:
    """Verify a Privy JWT (id_token or access_token) using the EC verification key (ES256)."""
    import logging

    import jwt as pyjwt
    from jwt.exceptions import InvalidAudienceError

    logger = logging.getLogger("privy_jwt")

    public_key = _get_privy_verification_key()

    try:
        header = pyjwt.get_unverified_header(token)
    except Exception as exc:
        raise ValueError(f"invalid JWT header: {exc}") from exc

    alg = header.get("alg", "ES256")
    if alg not in ("ES256", "ES384", "ES512"):
        logger.warning("Privy JWT unexpected algorithm: %s", alg)

    # Try with audience=app_id first, then without audience validation
    for verify_aud, aud_kwargs in (
        (True, {"audience": app_id}),
        (False, {"options": {"verify_aud": False}}),
    ):
        try:
            decode_kwargs: dict = {
                "algorithms": [alg],
                "options": {"verify_exp": True},
            }
            decode_kwargs.update(aud_kwargs)
            claims = pyjwt.decode(token, public_key, **decode_kwargs)
            if not verify_aud:
                # Manual issuer+audience sanity check
                iss = claims.get("iss", "")
                if iss and "privy" not in iss.lower():
                    raise ValueError(f"Privy JWT unexpected issuer: {iss!r}")
            return claims
        except InvalidAudienceError:
            logger.debug(
                "Privy JWT aud mismatch for app_id=%s, retrying without aud", app_id
            )
            continue
        except Exception:
            raise


def _normalize_auth_token(raw: str) -> str:
    token = str(raw or "").strip().strip('"').strip("'")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token.lower() in {"", "undefined", "null", "none"}:
        return ""
    return token


def _looks_like_jwt(token: str) -> bool:
    if not token or " " in token:
        return False
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def _token_debug_shape(token: str) -> str:
    if not token:
        return "empty"
    return f"len={len(token)},dots={token.count('.')}"


def _token_claim_brief(token: str) -> dict[str, str]:
    token_norm = _normalize_auth_token(token)
    if not _looks_like_jwt(token_norm):
        return {"kind": "non_jwt"}
    try:
        import jwt as _jwt  # noqa: PLC0415

        claims = _jwt.decode(
            token_norm,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
            },
            algorithms=["ES256", "RS256", "HS256"],
        )
        aud = claims.get("aud")
        if isinstance(aud, list):
            aud_text = ",".join([str(x) for x in aud[:3]])
        else:
            aud_text = str(aud or "")
        return {
            "iss": str(claims.get("iss") or "")[:120],
            "aud": aud_text[:200],
            "sub": str(claims.get("sub") or "")[:120],
        }
    except Exception:
        return {"kind": "jwt_parse_error"}


def _fetch_privy_user(privy_did: str, app_id: str, app_secret: str) -> dict:
    """Fetch Privy user record from REST API.

    Privy server API requires:
      - Header: privy-app-id: <app_id>
      - Basic auth: Authorization: Basic base64(app_id:app_secret)
    """
    import logging

    logger = logging.getLogger("privy_api")
    url = f"https://auth.privy.io/api/v1/users/{privy_did}"
    headers = {"privy-app-id": app_id}
    try:
        resp = requests.get(url, auth=(app_id, app_secret), headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.debug(
            "Privy user fetch %s -> %s: %s",
            privy_did,
            resp.status_code,
            resp.text[:200],
        )
    except Exception as exc:
        logger.debug("Privy user fetch error: %s", exc)
    return {}


def _fetch_privy_user_from_token(token: str, app_id: str) -> dict:
    """Fetch current Privy user by auth token via Privy users/me API.

    This path is used as a resilient fallback when local JWT verification fails
    (e.g. token shape drift, key rotation windows, SDK token transport changes).
    """
    import logging

    logger = logging.getLogger("privy_api")
    if not token:
        return {}

    url = "https://auth.privy.io/api/v1/users/me"
    origin = str(os.environ.get("PRIVY_APP_ORIGIN") or "https://veyra.trade").strip()
    header_variants = [
        {
            "privy-app-id": app_id,
            "Origin": origin,
            "Authorization": f"Bearer {token}",
        },
        {
            "privy-app-id": app_id,
            "Origin": origin,
            "privy-token": token,
        },
        {
            "privy-app-id": app_id,
            "Origin": origin,
            "Cookie": f"privy-token={token}",
        },
    ]
    for headers in header_variants:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict):
                    if isinstance(body.get("user"), dict):
                        return body.get("user") or {}
                    return body
            logger.debug(
                "Privy user/me fetch -> %s: %s",
                resp.status_code,
                resp.text[:200],
            )
        except Exception as exc:
            logger.debug("Privy user/me fetch error: %s", exc)
    return {}


def _verify_privy_tokens(
    id_token: str, access_token: str, client_user: dict | None = None
) -> tuple[str, dict]:
    """验证 Privy JWT，返回 (user_id, user_dict)。

    安全铁律：身份信息（钱包/embedded wallet/邮箱）只能取自 Privy 服务端
    返回值，绝不采信客户端提交的 user 对象。client_user 参数仅为向后兼容
    保留，不参与任何身份判定。

    因此本函数总是走 Privy REST API 路径以获得权威用户对象。
    """
    app_id = os.environ.get("PRIVY_APP_ID", "").strip()
    app_secrets = _load_privy_secrets()
    if not app_id or not app_secrets:
        raise ValueError("PRIVY_APP_ID or Privy app secret(s) not configured")

    id_token_norm = _normalize_auth_token(id_token)
    access_token_norm = _normalize_auth_token(access_token)

    token_candidates: list[tuple[str, str]] = []
    # Official Privy backend auth is access-token-first; keep id_token as a legacy fallback only.
    if access_token_norm:
        token_candidates.append(("access_token", access_token_norm))
    if id_token_norm and id_token_norm != access_token_norm:
        token_candidates.append(("id_token", id_token_norm))

    if not token_candidates:
        raise ValueError("missing id_token or access_token")

    # Verify JWT via verification key. Prefer access_token, then fallback to id_token.
    claims: dict | None = None
    token_errors: list[str] = []
    selected_source = ""
    for source, token in token_candidates:
        if not _looks_like_jwt(token):
            token_errors.append(f"{source}:not_jwt({_token_debug_shape(token)})")
            continue
        try:
            claims = _verify_privy_jwt(token, app_id)
            selected_source = source
            break
        except Exception as exc:
            token_errors.append(f"{source}:{exc}")

    if not claims:
        # Fallback: ask Privy to resolve current user directly from auth token.
        # This is robust for non-JWT token shapes and key-rotation windows.
        for source, token in token_candidates:
            user_from_token = _fetch_privy_user_from_token(token, app_id)
            uid_from_token = str(
                user_from_token.get("id")
                or user_from_token.get("did")
                or user_from_token.get("user_id")
                or ""
            ).strip()
            if user_from_token and uid_from_token:
                logger.info("privy token verified via user/me source=%s", source)
                return uid_from_token, user_from_token

        logger.warning(
            "privy token verification failed candidates=%s errors=%s",
            [(k, _token_debug_shape(v)) for k, v in token_candidates],
            token_errors,
        )
        raise ValueError("Invalid JWT token provided")

    logger.debug("privy token verified source=%s", selected_source)

    # Extract user DID from claims: sub is "did:privy:xxx"
    uid = str(claims.get("sub") or claims.get("user_id") or "").strip()
    if not uid:
        raise ValueError("privy token claims missing sub/user_id")

    # T901 安全修复：删除「信任客户端 user 对象」的快速路径。
    # 原实现允许请求体声明任意 linkedAccounts 地址并直接作为 session 钱包，
    # 可导致账户接管与管理员提权（配合 create_user_session 保留既有角色）。
    # 身份必须来自 Privy 服务端，故此处不再使用 client_user。
    del client_user

    # ── 权威路径：从 Privy API 拉取完整用户信息 ──
    user: dict = {}
    for secret in app_secrets:
        try:
            user = _fetch_privy_user(uid, app_id, secret)
            if user:
                break
        except Exception:
            continue

    return uid, user


def _get_or_create_runtime(wallet_address: str) -> dict:
    _ensure_runtime_table()
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT freeze_auto, auto_trading_enabled, updated_at FROM user_runtime_controls WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()
        if row:
            return {
                "freeze_auto": bool(row["freeze_auto"]),
                "auto_trading_enabled": bool(row["auto_trading_enabled"]),
                "updated_at": row["updated_at"],
            }
        conn.execute(
            "INSERT INTO user_runtime_controls(wallet_address, freeze_auto, auto_trading_enabled, updated_at) VALUES(?,0,1,?)",
            (wallet_address, now),
        )
        conn.commit()
    return {"freeze_auto": False, "auto_trading_enabled": True, "updated_at": now}


def _runtime_status_payload(wallet_address: str, runtime: dict) -> dict:
    emergency_freeze = bool(cfg_get("risk.emergency_freeze"))
    maintenance_mode = bool(cfg_get("system.maintenance_mode"))
    freeze_auto = bool(runtime.get("freeze_auto"))
    auto_trading_enabled = bool(runtime.get("auto_trading_enabled", True))
    effective_frozen = (
        maintenance_mode or emergency_freeze or freeze_auto or not auto_trading_enabled
    )
    return {
        "status": "ok",
        "role": "manual_user",
        "wallet_address": wallet_address,
        "freeze_auto": freeze_auto,
        "auto_trading_enabled": auto_trading_enabled,
        "effective_frozen": effective_frozen,
        "emergency_freeze": emergency_freeze,
        "maintenance_mode": maintenance_mode,
        "updated_at": runtime.get("updated_at"),
        # Compatibility fields used by existing V2 user center widgets.
        "auto_bet": auto_trading_enabled,
        "copy_trade": auto_trading_enabled,
        "stop_loss": True,
    }


def _strategy_payload(wallet_address: str, runtime: dict) -> dict:
    st = _runtime_status_payload(wallet_address, runtime)
    auto_enabled = bool(st.get("auto_trading_enabled"))
    return {
        "ok": True,
        "copy_trade_enabled": auto_enabled,
        "auto_sltp_enabled": auto_enabled,
        "notification_email": None,
        "risk_level": "moderate",
        "auto_bet": auto_enabled,
        "copy_trade": auto_enabled,
        "stop_loss": True,
        "telegram_notify": True,
        "trade_notify": True,
        "max_single_bet_pct": _safe_float(cfg_get("risk.max_single_bet_pct"), 0.03),
        "daily_loss_pct": _safe_float(cfg_get("risk.daily_loss_pause_pct"), 0.5),
        "min_ev": _safe_float(cfg_get("signal.min_ev_threshold"), 0.04),
        "dry_run": bool(cfg_get("general.dry_run")),
        "freeze_auto": bool(st.get("freeze_auto")),
        "auto_trading_enabled": auto_enabled,
        "effective_frozen": bool(st.get("effective_frozen")),
        "emergency_freeze": bool(st.get("emergency_freeze")),
        "maintenance_mode": bool(st.get("maintenance_mode")),
        "runtime_updated_at": st.get("updated_at"),
    }


def _latest_pm_state(wallet_address: str) -> dict:
    _ensure_pm_multi_tenant_tables()
    with db_connect() as conn:
        profile = conn.execute(
            "SELECT * FROM user_pm_profiles WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()
        eq = conn.execute(
            "SELECT * FROM user_pm_equity_snapshots WHERE wallet_address=? ORDER BY snapshot_ts DESC LIMIT 1",
            (wallet_address,),
        ).fetchone()
        positions = conn.execute(
            "SELECT * FROM user_pm_positions_snapshots WHERE wallet_address=? AND snapshot_ts=(SELECT snapshot_ts FROM user_pm_positions_snapshots WHERE wallet_address=? ORDER BY snapshot_ts DESC LIMIT 1) ORDER BY ABS(current_value) DESC",
            (wallet_address, wallet_address),
        ).fetchall()
    return {
        "profile": dict(profile) if profile else None,
        "equity": dict(eq) if eq else None,
        "positions": [dict(r) for r in positions],
    }


def _sync_user_pm_data(
    wallet_address: str,
    linked_wallets: list[str],
    force: bool = False,
    login_email: str = "",
    embedded_wallet_addr: str = "",
) -> dict:
    _ensure_pm_multi_tenant_tables()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    with db_connect() as conn:
        current_profile = conn.execute(
            "SELECT polymarket_wallet, discovery_method, last_synced_at FROM user_pm_profiles WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()
        user_row = conn.execute(
            "SELECT polymarket_wallet, funder_address FROM users WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()

    if current_profile and not force:
        last_synced = _parse_iso(str(current_profile["last_synced_at"] or ""))
        if last_synced and (now_dt - last_synced) < timedelta(seconds=45):
            state = _latest_pm_state(wallet_address)
            if state.get("profile"):
                return {"status": "ok", "cached": True, **state}

    # T881 (2026-05-06) — DEPOSIT-WALLET-ONLY MODE.
    # ----------------------------------------------------------------
    # Per product directive: every non-super_admin user MUST be
    # provisioned with a deterministic Deposit Wallet (signature_type=3
    # / wallet_type='deposit_wallet'). The legacy public-profile /
    # data-API discovery path was leaking pre-existing Polymarket Safe
    # addresses (wallet_type='safe', signature_type=2) for any EOA that
    # had ever traded on Polymarket directly, which broke the silent
    # provisioning promise of T877 and forced the readiness modal back
    # on /v2/withdraw. We now BYPASS _discover_pm_wallet entirely for
    # non-super_admin users and dispatch straight into the deposit-
    # wallet branch below by zeroing out the discovered pm_wallet.
    #
    # super_admin / admin retain their pre-cutover Safe binding to
    # avoid disturbing the operator's existing balance & position
    # history (the spec marks them as optional). If a future Wave
    # decides to migrate them too, drop the role check here.
    # ----------------------------------------------------------------
    user_role = ""
    try:
        with db_connect() as conn:
            role_row = conn.execute(
                "SELECT role FROM users WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()
            if role_row:
                user_role = str(role_row["role"] or "").strip().lower()
    except Exception:
        user_role = ""

    force_deposit_wallet = user_role not in ("super_admin", "admin")

    if force_deposit_wallet:
        discovered = {
            "pm_wallet": "",
            "proxy_wallet": "",
            "deposit_wallet": "",
            "wallet_type": "deposit_wallet",
            "method": "deposit_wallet_only",
            "confidence": 100.0,
            "profile": {},
            "value": {},
            "positions": [],
            "activity": [],
        }
    else:
        discovered = _discover_pm_wallet(wallet_address, linked_wallets)
    pm_wallet = str(discovered.get("pm_wallet") or "").strip().lower()
    if not _ADDR_RE.match(pm_wallet):
        pm_wallet = ""

    if not pm_wallet and login_email and not force_deposit_wallet:
        email_pm_wallet = _discover_pm_wallet_from_email(login_email)
        if _ADDR_RE.match(email_pm_wallet):
            pm_wallet = email_pm_wallet
            discovered["method"] = "email_lookup"
            discovered["confidence"] = max(
                _safe_float(discovered.get("confidence"), 0.0), 60.0
            )

    existing_user_pm = (
        str((dict(user_row) if user_row else {}).get("polymarket_wallet") or "")
        .strip()
        .lower()
    )
    existing_funder_address = str(
        (dict(user_row) if user_row else {}).get("funder_address") or ""
    ).strip()
    cur_prof = dict(current_profile) if current_profile else {}
    current_method = str(cur_prof.get("discovery_method") or "").strip().lower()
    legacy_auto_bound = bool(
        _ADDR_RE.match(existing_user_pm)
        and existing_user_pm == wallet_address.lower()
        and current_method in {"fallback_login_wallet", "none", "unknown"}
    )

    # T881: when force_deposit_wallet is on, ANY existing pm_wallet that
    # doesn't already point at the deterministic deposit address is treated
    # as legacy and overwritten. This catches the exact failure mode that
    # produced the 412/banner regression (users carrying a leaked Safe).
    if force_deposit_wallet:
        legacy_auto_bound = True
        pm_wallet = ""

    if not pm_wallet and _ADDR_RE.match(existing_user_pm) and not legacy_auto_bound:
        pm_wallet = existing_user_pm
        discovered["method"] = "manual_bound"
        discovered["confidence"] = max(
            _safe_float(discovered.get("confidence"), 0.0), 99.0
        )

    if not pm_wallet:
        # T877 (2026-05-06) — DETERMINISTIC DEPOSIT WALLET (single path)
        # ---------------------------------------------------------------
        # System has no pre-cutover (Safe / signature_type=2) end users
        # remaining; the only legacy account is the operator/super-admin
        # row, which already has polymarket_wallet set and never reaches
        # this branch.
        #
        # Every brand-new user gets a deterministic CREATE2 Deposit
        # Wallet derived locally via Polymarket's official
        # py-builder-relayer-client SDK. PM accepts deposits to the
        # counterfactual address before deployment and auto-deploys the
        # contract on the user's first on-chain interaction.
        # ---------------------------------------------------------------
        from src.utils.pm_deposit_wallet import derive_deposit_wallet_address

        derived_deposit_wallet = derive_deposit_wallet_address(wallet_address)

        with db_connect() as conn:
            if legacy_auto_bound:
                conn.execute(
                    "UPDATE users SET polymarket_wallet=NULL, funder_address=NULL, updated_at=? WHERE wallet_address=?",
                    (now, wallet_address),
                )
            conn.execute(
                """
                INSERT INTO user_pm_profiles(
                    wallet_address, login_wallet, polymarket_wallet, proxy_wallet,
                    linked_wallets_json, discovery_method, discovery_confidence,
                    profile_json, last_synced_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address) DO UPDATE SET
                    login_wallet=excluded.login_wallet,
                    polymarket_wallet=excluded.polymarket_wallet,
                    proxy_wallet=excluded.proxy_wallet,
                    linked_wallets_json=excluded.linked_wallets_json,
                    discovery_method=excluded.discovery_method,
                    discovery_confidence=excluded.discovery_confidence,
                    profile_json=excluded.profile_json,
                    last_synced_at=excluded.last_synced_at,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (
                    wallet_address,
                    wallet_address,
                    derived_deposit_wallet,
                    derived_deposit_wallet,
                    json.dumps(linked_wallets or [], ensure_ascii=False),
                    "create2_derived",
                    100.0,
                    json.dumps(discovered.get("profile") or {}, ensure_ascii=False),
                    now,
                    "",
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE users SET
                    polymarket_wallet=?,
                    main_wallet=COALESCE(NULLIF(main_wallet, ''), ?),
                    funder_address=?,
                    signature_type=3,
                    wallet_type='deposit_wallet',
                    updated_at=?
                WHERE wallet_address=?
                """,
                (
                    derived_deposit_wallet,
                    wallet_address,
                    derived_deposit_wallet,
                    now,
                    wallet_address,
                ),
            )
            conn.commit()

        # T881-bugfix (2026-05-06): Write an equity snapshot for the deposit
        # wallet so /api/user/pm/metrics, the header balance dropdown and the
        # /v2 dashboard surface real chain balances on first hit. Previously
        # this branch returned early with an empty `_latest_pm_state` payload
        # and the user would see "$0.00" forever even after funding the
        # deposit wallet on chain. We read pUSD + native USDC + USDC.e
        # directly from RPC; PM data API is skipped because deposit wallets
        # are not yet indexed by PM until the contract auto-deploys on the
        # user's first dispatch.
        try:
            usdc_balances_chain_dw = _fetch_usdc_balances(derived_deposit_wallet)
            try:
                from src.utils import pm_v2_constants as _pm_v2  # noqa: PLC0415

                usdc_e_dw = _fetch_erc20_balance(
                    derived_deposit_wallet, _pm_v2.USDC_E_TOKEN
                )
            except Exception:
                usdc_e_dw = None
            pusd_dw = usdc_balances_chain_dw.get("pusd")
            usdc_native_dw = usdc_balances_chain_dw.get("usdc_native")
            parts = [
                v for v in (pusd_dw, usdc_native_dw, usdc_e_dw) if v is not None
            ]
            usdc_balance_dw = float(sum(parts)) if parts else 0.0

            with db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_pm_equity_snapshots(
                        wallet_address, snapshot_ts, polymarket_wallet,
                        portfolio_value, usdc_balance, open_positions_count,
                        unrealized_pnl, realized_pnl, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (wallet_address, snapshot_hour) DO UPDATE SET
                        snapshot_ts          = EXCLUDED.snapshot_ts,
                        polymarket_wallet    = EXCLUDED.polymarket_wallet,
                        portfolio_value      = EXCLUDED.portfolio_value,
                        usdc_balance         = EXCLUDED.usdc_balance,
                        open_positions_count = EXCLUDED.open_positions_count,
                        unrealized_pnl       = EXCLUDED.unrealized_pnl,
                        realized_pnl         = EXCLUDED.realized_pnl,
                        raw_json             = EXCLUDED.raw_json
                    """,
                    (
                        wallet_address,
                        now,
                        derived_deposit_wallet,
                        0.0,
                        usdc_balance_dw,
                        0,
                        0.0,
                        0.0,
                        json.dumps(
                            {
                                "source": "deposit_wallet_chain_only",
                                "balance_chain_pusd": pusd_dw,
                                "balance_chain_usdc_native": usdc_native_dw,
                                "balance_chain_usdc_e": usdc_e_dw,
                                "deposit_wallet": derived_deposit_wallet,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                conn.commit()
            logger.info(
                "[DepositWallet][snapshot] wallet=%s deposit=%s "
                "pusd=%s usdc=%s usdc_e=%s total=%s",
                wallet_address,
                derived_deposit_wallet,
                pusd_dw,
                usdc_native_dw,
                usdc_e_dw,
                usdc_balance_dw,
            )
        except Exception as snap_exc:  # noqa: BLE001
            logger.warning(
                "[DepositWallet][snapshot] failed wallet=%s err=%s",
                wallet_address,
                snap_exc,
            )

        logger.info(
            "[T877] derived deposit wallet wallet=%s deposit=%s",
            wallet_address,
            derived_deposit_wallet,
        )

        # T912 (2026-05-11): Sync PM Data API positions+activity for the
        # deposit wallet. The previous deposit-wallet branch returned without
        # ever fetching positions, so even after a successful CLOB trade the
        # /v2/trades page and /v2 dashboard stayed empty (PM Data API was
        # never queried for the deposit wallet). PM Data API supports queries
        # by deposit-wallet address as soon as the contract is deployed (auto-
        # deploys on the user's first on-chain interaction); for brand-new
        # users it returns empty, which is the expected pre-trade state.
        try:
            dw_positions = _fetch_positions(derived_deposit_wallet) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[T912] _fetch_positions failed wallet=%s deposit=%s err=%s",
                wallet_address,
                derived_deposit_wallet,
                exc,
            )
            dw_positions = []
        try:
            dw_activity = _fetch_full_activity(derived_deposit_wallet) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[T912] _fetch_full_activity failed wallet=%s deposit=%s err=%s",
                wallet_address,
                derived_deposit_wallet,
                exc,
            )
            dw_activity = []

        if dw_positions or dw_activity:
            dw_value_num = sum(
                _safe_float(p.get("currentValue"), 0.0) for p in dw_positions
            )
            dw_unrealized = sum(
                _safe_float(p.get("cashPnl"), 0.0) for p in dw_positions
            )
            dw_realized = sum(
                _safe_float(p.get("realizedPnl"), 0.0) for p in dw_positions
            )
            _dw_step = "init"
            try:
                with db_connect() as conn:
                    # Refresh equity snapshot to include positions value
                    _dw_step = "equity_insert"
                    conn.execute(
                        """
                        INSERT INTO user_pm_equity_snapshots(
                            wallet_address, snapshot_ts, polymarket_wallet,
                            portfolio_value, usdc_balance, open_positions_count,
                            unrealized_pnl, realized_pnl, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (wallet_address, snapshot_hour) DO UPDATE SET
                            snapshot_ts          = EXCLUDED.snapshot_ts,
                            polymarket_wallet    = EXCLUDED.polymarket_wallet,
                            portfolio_value      = EXCLUDED.portfolio_value,
                            open_positions_count = EXCLUDED.open_positions_count,
                            unrealized_pnl       = EXCLUDED.unrealized_pnl,
                            realized_pnl         = EXCLUDED.realized_pnl,
                            raw_json             = EXCLUDED.raw_json
                        """,
                        (
                            wallet_address,
                            now,
                            derived_deposit_wallet,
                            dw_value_num,
                            locals().get("usdc_balance_dw", 0.0),
                            len(dw_positions),
                            dw_unrealized,
                            dw_realized,
                            json.dumps(
                                {
                                    "source": "deposit_wallet_pm_data_api",
                                    "positions_count": len(dw_positions),
                                    "activity_count": len(dw_activity),
                                    "deposit_wallet": derived_deposit_wallet,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )

                    # Rebuild positions snapshot for this wallet
                    _dw_step = "positions_delete"
                    conn.execute(
                        "DELETE FROM user_pm_positions_snapshots WHERE wallet_address=?",
                        (wallet_address,),
                    )
                    _dw_step = "positions_insert"
                    for pos in dw_positions:
                        cond_id = pos.get("conditionId")
                        asset_id = pos.get("asset")
                        if not cond_id or not asset_id:
                            continue
                        conn.execute(
                            """
                            INSERT INTO user_pm_positions_snapshots(
                                wallet_address, snapshot_ts, polymarket_wallet,
                                condition_id, asset, title, outcome,
                                size, avg_price, initial_value, current_value,
                                cash_pnl, percent_pnl, cur_price,
                                event_id, slug, event_slug, end_date, raw_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                wallet_address,
                                now,
                                derived_deposit_wallet,
                                cond_id,
                                asset_id,
                                pos.get("title") or pos.get("question"),
                                pos.get("outcome"),
                                _safe_float(pos.get("size")),
                                _safe_float(pos.get("avgPrice")),
                                _safe_float(pos.get("initialValue")),
                                _safe_float(pos.get("currentValue")),
                                _safe_float(pos.get("cashPnl")),
                                _safe_float(pos.get("percentPnl")),
                                _safe_float(pos.get("curPrice")),
                                str(pos.get("eventId") or ""),
                                pos.get("slug"),
                                pos.get("eventSlug"),
                                pos.get("endDate"),
                                json.dumps(pos, ensure_ascii=False),
                            ),
                        )

                    # Append activity rows (dedupe by tx_hash via per-row try)
                    _dw_step = "activity_insert"
                    for act in dw_activity[:2000]:
                        tx_hash = act.get("transactionHash")
                        if not tx_hash:
                            continue
                        try:
                            conn.execute(
                                """
                                INSERT INTO user_pm_activity_snapshots(
                                    wallet_address, snapshot_ts, polymarket_wallet, activity_type,
                                    condition_id, asset, title, outcome, size, usdc_size, price,
                                    event_id, event_slug, activity_ts, tx_hash, raw_json
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT (wallet_address, tx_hash)
                                  WHERE tx_hash IS NOT NULL AND tx_hash <> ''
                                DO NOTHING
                                """,
                                (
                                    wallet_address,
                                    now,
                                    derived_deposit_wallet,
                                    act.get("type"),
                                    act.get("conditionId"),
                                    act.get("asset"),
                                    act.get("title"),
                                    act.get("outcome"),
                                    _safe_float(act.get("size")),
                                    _safe_float(act.get("usdcSize")),
                                    _safe_float(act.get("price")),
                                    str(act.get("eventId") or ""),
                                    act.get("eventSlug"),
                                    _safe_int(act.get("timestamp")),
                                    tx_hash,
                                    json.dumps(act, ensure_ascii=False),
                                ),
                            )
                        except Exception as _act_exc:
                            logger.debug(
                                "[T912] activity row skip tx=%s err=%s",
                                tx_hash,
                                _act_exc,
                            )
                    conn.commit()
                logger.info(
                    "[T912] deposit-wallet PM sync wallet=%s deposit=%s "
                    "positions=%d activity=%d portfolio_value=%.4f",
                    wallet_address,
                    derived_deposit_wallet,
                    len(dw_positions),
                    len(dw_activity),
                    dw_value_num,
                )
            except Exception as sync_exc:  # noqa: BLE001
                logger.warning(
                    "[T912] deposit-wallet PM sync persist failed wallet=%s step=%s err=%s",
                    wallet_address,
                    _dw_step,
                    sync_exc,
                )

        state = _latest_pm_state(wallet_address)
        return {
            "status": "ok",
            "cached": False,
            "needs_pm_binding": False,
            "auto_provisioned": True,
            "pm_wallet_status": "ready",
            **state,
        }

    # Ensure we have current fetch payloads for the selected wallet.
    value_payload = discovered.get("value") or _fetch_value(pm_wallet)
    positions = discovered.get("positions") or _fetch_positions(pm_wallet)
    stored_activity_count = _stored_activity_count(wallet_address)
    known_activity_ts = _latest_known_activity_ts(wallet_address)
    activity = _fetch_full_activity(
        pm_wallet,
        start_ts=(
            known_activity_ts
            if known_activity_ts > 0 and stored_activity_count >= 1000
            else None
        ),
    )
    if not activity:
        activity = discovered.get("activity") or _fetch_activity(pm_wallet)
    accounting = _fetch_accounting_snapshot(pm_wallet)
    usdc_balances_chain = _fetch_usdc_balances(pm_wallet)
    usdc_balance_chain = usdc_balances_chain.get("usdc_total")
    usdc_balance_payload = _extract_balance_from_value_payload(value_payload or {})
    usdc_balance = _safe_float(accounting.get("cash_balance"), 0.0)
    if usdc_balance <= 0.000001 and usdc_balance_chain is not None:
        usdc_balance = usdc_balance_chain
    if usdc_balance <= 0.000001:
        usdc_balance = usdc_balance_payload
    profile_obj = discovered.get("profile") or _fetch_public_profile(pm_wallet)

    value_num = _safe_float(accounting.get("positions_value"), 0.0)
    if value_num <= 0.000001:
        value_num = _extract_positions_value_from_value_payload(value_payload or {})
    unrealized_pnl = sum(_safe_float(p.get("cashPnl"), 0.0) for p in positions)
    realized_pnl = sum(_safe_float(p.get("realizedPnl"), 0.0) for p in positions)
    snapshot_ts = now

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO user_pm_profiles(
                wallet_address, login_wallet, polymarket_wallet, proxy_wallet,
                linked_wallets_json, discovery_method, discovery_confidence,
                profile_json, last_synced_at, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                login_wallet=excluded.login_wallet,
                polymarket_wallet=excluded.polymarket_wallet,
                proxy_wallet=excluded.proxy_wallet,
                linked_wallets_json=excluded.linked_wallets_json,
                discovery_method=excluded.discovery_method,
                discovery_confidence=excluded.discovery_confidence,
                profile_json=excluded.profile_json,
                last_synced_at=excluded.last_synced_at,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                wallet_address,
                wallet_address,
                pm_wallet,
                discovered.get("proxy_wallet")
                or "",  # T499++ Wave 6B: never EOA fallback
                json.dumps(linked_wallets or [], ensure_ascii=False),
                discovered.get("method") or "unknown",
                _safe_float(discovered.get("confidence"), 0.0),
                json.dumps(profile_obj or {}, ensure_ascii=False),
                now,
                "",
                now,
                now,
            ),
        )

        # Keep users table in sync for legacy readers and UI checks.
        # T287 Wave 2: Also populate canonical main_wallet + funder_address columns.
        # Deposit Wallet Migration (May 2026): for deposit_wallet users use signature_type=3
        # and store the deposit wallet as funder_address. Existing Safe/proxy users keep type=2.
        _disc_wallet_type = discovered.get("wallet_type") or "safe"
        _disc_sig_type = 3 if _disc_wallet_type == "deposit_wallet" else 2
        _disc_deposit_wallet = discovered.get("deposit_wallet") or ""
        # funder_address: for deposit_wallet users it's the deposit wallet; otherwise the pm_wallet
        _disc_funder = _disc_deposit_wallet if (_disc_wallet_type == "deposit_wallet" and _disc_deposit_wallet) else pm_wallet
        conn.execute(
            """
            UPDATE users SET
                polymarket_wallet=?,
                main_wallet=COALESCE(NULLIF(main_wallet, ''), ?),
                funder_address=?,
                signature_type=?,
                wallet_type=?,
                updated_at=?
            WHERE wallet_address=?
            """,
            (pm_wallet, wallet_address, _disc_funder, _disc_sig_type, _disc_wallet_type, now, wallet_address),
        )

        # T611: equity 按 (wallet, snapshot_hour) 桶 UPSERT；同小时多次刷新只保留最后一次
        conn.execute(
            """
            INSERT INTO user_pm_equity_snapshots(
                wallet_address, snapshot_ts, polymarket_wallet,
                portfolio_value, usdc_balance, open_positions_count,
                unrealized_pnl, realized_pnl, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (wallet_address, snapshot_hour) DO UPDATE SET
                snapshot_ts          = EXCLUDED.snapshot_ts,
                polymarket_wallet    = EXCLUDED.polymarket_wallet,
                portfolio_value      = EXCLUDED.portfolio_value,
                usdc_balance         = EXCLUDED.usdc_balance,
                open_positions_count = EXCLUDED.open_positions_count,
                unrealized_pnl       = EXCLUDED.unrealized_pnl,
                realized_pnl         = EXCLUDED.realized_pnl,
                raw_json             = EXCLUDED.raw_json
            """,
            (
                wallet_address,
                snapshot_ts,
                pm_wallet,
                value_num,
                usdc_balance,
                len(positions),
                unrealized_pnl,
                realized_pnl,
                json.dumps(
                    {
                        "value": value_payload,
                        "accounting": accounting,
                        "balance_chain": usdc_balance_chain,
                        "balance_chain_usdc_e": usdc_balances_chain.get("usdc_e"),
                        "balance_chain_usdc_native": usdc_balances_chain.get(
                            "usdc_native"
                        ),
                        "balance_payload": usdc_balance_payload,
                        "activity_count": len(activity),
                    },
                    ensure_ascii=False,
                ),
            ),
        )

        # T611: positions 改为「当前快照」表 — 删除该钱包历史所有行，按当前批量重建。
        # 配合 init_pg_schema.sql 中 uq_pm_positions_wallet_cond_asset 唯一索引，确保单仓位单行。
        conn.execute(
            "DELETE FROM user_pm_positions_snapshots WHERE wallet_address=?",
            (wallet_address,),
        )
        for pos in positions:
            cond_id = pos.get("conditionId")
            asset_id = pos.get("asset")
            if not cond_id or not asset_id:
                continue
            conn.execute(
                """
                INSERT INTO user_pm_positions_snapshots(
                    wallet_address, snapshot_ts, polymarket_wallet,
                    condition_id, asset, title, outcome,
                    size, avg_price, initial_value, current_value,
                    cash_pnl, percent_pnl, cur_price,
                    event_id, slug, event_slug, end_date, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (wallet_address, condition_id, asset) DO UPDATE SET
                    snapshot_ts        = EXCLUDED.snapshot_ts,
                    polymarket_wallet  = EXCLUDED.polymarket_wallet,
                    title              = EXCLUDED.title,
                    outcome            = EXCLUDED.outcome,
                    size               = EXCLUDED.size,
                    avg_price          = EXCLUDED.avg_price,
                    initial_value      = EXCLUDED.initial_value,
                    current_value      = EXCLUDED.current_value,
                    cash_pnl           = EXCLUDED.cash_pnl,
                    percent_pnl        = EXCLUDED.percent_pnl,
                    cur_price          = EXCLUDED.cur_price,
                    event_id           = EXCLUDED.event_id,
                    slug               = EXCLUDED.slug,
                    event_slug         = EXCLUDED.event_slug,
                    end_date           = EXCLUDED.end_date,
                    raw_json           = EXCLUDED.raw_json
                """,
                (
                    wallet_address,
                    snapshot_ts,
                    pm_wallet,
                    cond_id,
                    asset_id,
                    pos.get("title") or pos.get("question"),
                    pos.get("outcome"),
                    _safe_float(pos.get("size")),
                    _safe_float(pos.get("avgPrice")),
                    _safe_float(pos.get("initialValue")),
                    _safe_float(pos.get("currentValue")),
                    _safe_float(pos.get("cashPnl")),
                    _safe_float(pos.get("percentPnl")),
                    _safe_float(pos.get("curPrice")),
                    str(pos.get("eventId") or ""),
                    pos.get("slug"),
                    pos.get("eventSlug"),
                    pos.get("endDate"),
                    json.dumps(pos, ensure_ascii=False),
                ),
            )

        # T611: activities 改为「事件日志」append-only — 同一笔 tx_hash 永远只占一行。
        # 没有 tx_hash 的（少量 SPLIT/MERGE/REWARD 等）跳过，避免 dedup 失败。
        for act in activity[:2000]:
            tx_hash = act.get("transactionHash")
            if not tx_hash:
                continue
            conn.execute(
                """
                INSERT INTO user_pm_activity_snapshots(
                    wallet_address, snapshot_ts, polymarket_wallet, activity_type,
                    condition_id, asset, title, outcome, size, usdc_size, price,
                    event_id, event_slug, activity_ts, tx_hash, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (wallet_address, tx_hash)
                  WHERE tx_hash IS NOT NULL AND tx_hash <> ''
                DO NOTHING
                """,
                (
                    wallet_address,
                    snapshot_ts,
                    pm_wallet,
                    act.get("type"),
                    act.get("conditionId"),
                    act.get("asset"),
                    act.get("title"),
                    act.get("outcome"),
                    _safe_float(act.get("size")),
                    _safe_float(act.get("usdcSize")),
                    _safe_float(act.get("price")),
                    str(act.get("eventId") or ""),
                    act.get("eventSlug"),
                    _safe_int(act.get("timestamp")),
                    tx_hash,
                    json.dumps(act, ensure_ascii=False),
                ),
            )
        conn.commit()

    state = _latest_pm_state(wallet_address)
    return {
        "status": "ok",
        "cached": False,
        "needs_pm_binding": False,
        **state,
    }


def _calc_pm_metrics(
    wallet_address: str,
    bets: list[dict] | None = None,
    positions_value: float = 0.0,
    nlv: float = 0.0,
) -> dict:
    _ensure_pm_multi_tenant_tables()
    now = datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    week_start = now - timedelta(days=7)

    with db_connect() as conn:
        rows = conn.execute(
            "SELECT snapshot_ts, portfolio_value FROM user_pm_equity_snapshots WHERE wallet_address=? AND snapshot_ts>=? ORDER BY snapshot_ts ASC",
            (wallet_address, (now - timedelta(days=30)).isoformat()),
        ).fetchall()

    series = [(r["snapshot_ts"], _safe_float(r["portfolio_value"], 0.0)) for r in rows]
    if not series:
        resolved_bets = [b for b in (bets or []) if b.get("resolved_outcome")]
        wins = [b for b in resolved_bets if _safe_float(b.get("pnl"), 0.0) > 0]
        win_rate = (len(wins) / len(resolved_bets)) if resolved_bets else 0.0
        cap_util = round((positions_value / nlv) * 100, 1) if nlv > 1e-9 else 0.0
        return {
            "today_pnl": 0.0,
            "week_pnl": 0.0,
            "sharpe_ratio": 0.0,
            "win_rate": win_rate,
            "sample_days": 0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "capital_utilization": cap_util,
        }

    latest_val = series[-1][1]

    # Daily close series for Sharpe.
    daily = {}
    for ts, v in series:
        dt = _parse_iso(ts)
        if not dt:
            continue
        key = dt.date().isoformat()
        daily[key] = v
    closes = [daily[k] for k in sorted(daily.keys())]
    rets = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        cur = closes[i]
        if prev > 0:
            rets.append((cur / prev) - 1.0)

    sharpe = 0.0
    win_rate = 0.0
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / max(1, (len(rets) - 1))
        std = var**0.5
        if std > 1e-9:
            sharpe = (mean / std) * (365.0**0.5)
        win_rate = sum(1 for x in rets if x > 0) / len(rets)

    resolved_bets = [b for b in (bets or []) if b.get("resolved_outcome")]

    def _resolved_ts_from_bet(bet: dict) -> int:
        ts = _normalize_unix_ts(
            bet.get("resolved_ts") or bet.get("resolved_timestamp"),
            0,
        )
        if ts > 0:
            return ts
        dt = _parse_iso(str(bet.get("resolved_at") or ""))
        if dt:
            return int(dt.timestamp())
        return _normalize_unix_ts(bet.get("ts_entered"), 0)

    day_start_ts = int(day_start.timestamp())
    week_start_ts = int(week_start.timestamp())
    today_pnl = 0.0
    week_pnl = 0.0
    for b in resolved_bets:
        b_ts = _resolved_ts_from_bet(b)
        pnl_val = _safe_float(b.get("pnl"), 0.0)
        if b_ts >= day_start_ts:
            today_pnl += pnl_val
        if b_ts >= week_start_ts:
            week_pnl += pnl_val

    if resolved_bets:
        wins = [b for b in resolved_bets if _safe_float(b.get("pnl"), 0.0) > 0]
        win_rate = len(wins) / len(resolved_bets)

    peak = closes[0] if closes else 0.0
    max_dd = 0.0
    for val in closes:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / peak * 100.0) if peak > 1e-9 else 0.0
    cap_util = round((positions_value / nlv) * 100, 1) if nlv > 1e-9 else 0.0

    return {
        "today_pnl": today_pnl,
        "week_pnl": week_pnl,
        "sharpe_ratio": sharpe,
        "win_rate": win_rate,
        "sample_days": len(closes),
        "max_drawdown": max_dd,
        "max_drawdown_pct": round(max_dd_pct, 2),
        "capital_utilization": cap_util,
    }


def _safe_json_loads(raw_text: str) -> dict:
    if not raw_text:
        return {}
    try:
        obj = json.loads(raw_text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _normalize_ratio(v) -> float:
    ratio = _safe_float(v, 0.0)
    if abs(ratio) > 2.0:
        ratio = ratio / 100.0
    return ratio


def _normalize_category_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-zA-Z0-9]+", " ", ascii_text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalize_tag_token(value: str) -> str:
    token = str(value or "").strip().lower().replace("_", "-")
    token = re.sub(r"[^a-z0-9\-]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    return token


_PM_CATEGORY_ALIASES = {
    "politics": "geopolitics",
    "political": "geopolitics",
    "geopolitical": "geopolitics",
    "world-news": "world",
    "financial": "finance",
    "economics": "economy",
    "pop_culture": "pop-culture",
    "entertainment": "pop-culture",
}
_PM_DEFAULT_CATEGORY = "economy"


def _normalize_category_slug(value: str) -> str:
    token = _normalize_tag_token(str(value or "").strip().lower())
    if not token:
        return ""
    return _PM_CATEGORY_ALIASES.get(token, token)


def _load_pm_category_slugs() -> set[str]:
    categories: set[str] = set()
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT slug FROM pm_categories WHERE active=1 ORDER BY slug ASC"
            ).fetchall()
        for r in rows:
            row = dict(r)
            slug = _normalize_category_slug(str(row.get("slug") or ""))
            if slug:
                categories.add(slug)
    except Exception:
        categories = set()

    if categories:
        return categories

    # Safety fallback to avoid breaking rendering before migration reaches this node.
    return {
        "crypto",
        "earnings",
        "economy",
        "elections",
        "esports",
        "finance",
        "geopolitics",
        "mentions",
        "politics",
        "pop-culture",
        "sports",
        "tech",
        "weather",
        "world",
    }


def _load_pm_tag_category_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    try:
        with db_connect() as conn:
            rows = conn.execute("""
                SELECT slug, category_slug
                FROM pm_tags
                WHERE active=1
                ORDER BY updated_at DESC
                """).fetchall()
        for r in rows:
            row = dict(r)
            slug = _normalize_tag_token(str(row.get("slug") or ""))
            category = _normalize_category_slug(str(row.get("category_slug") or ""))
            if slug and category and slug not in mapping:
                mapping[slug] = category
    except Exception:
        return {}
    return mapping


def _infer_market_category(
    title: str,
    market_slug: str,
    raw: dict,
    category_slugs: set[str] | None = None,
    tag_category_map: dict[str, str] | None = None,
) -> str:
    if category_slugs is None:
        category_slugs = _load_pm_category_slugs()
    if tag_category_map is None:
        tag_category_map = _load_pm_tag_category_map()

    def _pick_direct(*values: object) -> str:
        for value in values:
            cat = _normalize_category_slug(str(value or ""))
            if cat and cat in category_slugs:
                return cat
        return ""

    direct = _pick_direct(
        raw.get("category"),
        raw.get("group"),
        raw.get("groupItemTitle"),
    )
    if direct:
        return direct

    tags = _extract_pm_tags(raw)
    if tags:
        votes: dict[str, int] = {}
        for tag in tags:
            cat = _normalize_category_slug(tag_category_map.get(tag) or "")
            if cat and cat in category_slugs:
                votes[cat] = int(votes.get(cat, 0)) + 1
            direct_from_tag = _normalize_category_slug(tag)
            if direct_from_tag and direct_from_tag in category_slugs:
                votes[direct_from_tag] = int(votes.get(direct_from_tag, 0)) + 1
        if votes:
            return sorted(votes.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))[0][0]

    slug_text = " ".join(
        [
            str(title or ""),
            str(market_slug or ""),
            str(raw.get("eventSlug") or ""),
            str(raw.get("slug") or ""),
        ]
    )
    for token in _normalize_tag_token(slug_text).split("-"):
        cat = _normalize_category_slug(token)
        if cat and cat in category_slugs:
            return cat

    normalized_text = _normalize_category_text(slug_text)
    keyword_rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "weather",
            (
                "weather",
                "temperature",
                "temp",
                "rain",
                "snow",
                "storm",
                "hurricane",
                "forecast",
            ),
        ),
        (
            "crypto",
            (
                "bitcoin",
                "btc",
                "ethereum",
                "eth",
                "crypto",
                "solana",
                "doge",
                "xrp",
                "blockchain",
            ),
        ),
        (
            "sports",
            (
                "championship",
                "playoff",
                "vs",
                "match",
                "game",
                "nba",
                "nfl",
                "mlb",
                "nhl",
            ),
        ),
        (
            "geopolitics",
            (
                "election",
                "president",
                "presidential",
                "senate",
                "parliament",
                "government",
                "war",
                "vote",
            ),
        ),
    )
    for category, keywords in keyword_rules:
        if category not in category_slugs:
            continue
        if any(keyword in normalized_text for keyword in keywords):
            return category

    if _PM_DEFAULT_CATEGORY in category_slugs:
        return _PM_DEFAULT_CATEGORY
    return sorted(category_slugs)[0] if category_slugs else _PM_DEFAULT_CATEGORY


def _extract_pm_tags(payload: object, fallback_category: str = "") -> list[str]:
    """尽力从 PM 原始 payload 中提取标签 slug（容错，不抛异常）。"""
    tags: list[str] = []
    seen: set[str] = set()

    def _add_tag(value: object) -> None:
        tok = _normalize_tag_token(str(value or ""))
        if not tok or tok in seen:
            return
        if len(tok) < 2 or len(tok) > 64:
            return
        seen.add(tok)
        tags.append(tok)

    def _walk(node: object, parent_key: str = "", depth: int = 0) -> None:
        if depth > 5 or len(tags) >= 12:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                lk = str(k).lower()
                if lk in {
                    "tags",
                    "tag_slugs",
                    "tagslugs",
                    "related_tags",
                    "relatedtags",
                }:
                    _walk(v, lk, depth + 1)
                    continue
                if lk in {"event", "events", "market", "markets", "group"}:
                    _walk(v, lk, depth + 1)
                    continue
                if "tag" in parent_key and lk in {
                    "slug",
                    "tag_slug",
                    "tag",
                    "label",
                    "name",
                }:
                    _add_tag(v)
        elif isinstance(node, list):
            for item in node[:40]:
                if isinstance(item, str) and "tag" in parent_key:
                    _add_tag(item)
                else:
                    _walk(item, parent_key, depth + 1)
        elif isinstance(node, str) and "tag" in parent_key:
            _add_tag(node)

    try:
        _walk(payload)
    except Exception:
        pass

    if fallback_category:
        _add_tag(fallback_category)

    return tags


def _all_pm_activity_rows(wallet_address: str, limit: int = 5000) -> list[dict]:
    _ensure_pm_multi_tenant_tables()
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM user_pm_activity_snapshots WHERE wallet_address=? ORDER BY COALESCE(activity_ts, 0) DESC LIMIT ?",
            (wallet_address, max(limit * 2, limit)),
        ).fetchall()
    uniq: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        item = dict(row)
        key = str(item.get("tx_hash") or "").strip()
        if not key:
            key = "|".join(
                [
                    str(item.get("activity_type") or ""),
                    str(item.get("condition_id") or ""),
                    str(item.get("outcome") or ""),
                    str(item.get("activity_ts") or "0"),
                    str(item.get("size") or "0"),
                    str(item.get("price") or "0"),
                ]
            )
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
        if len(uniq) >= limit:
            break
    return uniq


def _recently_redeemed_keys(
    user_wallet: str, ttl_seconds: int = 1800
) -> set[tuple[str, str]]:
    """T466: positions just redeemed locally are filtered from the live snapshot.

    PM Data API positions take 1-2 minutes to update after the on-chain redeem
    is mined. Without local masking, the user sees the bet "stuck" in 持仓 even
    though the redeem succeeded. We mask `(condition_id_lower, outcome_upper)`
    tuples for ``ttl_seconds`` after the local vault_transactions row is written.
    """
    wallet = str(user_wallet or "").strip().lower()
    if not wallet:
        return set()
    cutoff = int(datetime.now(timezone.utc).timestamp()) - max(0, int(ttl_seconds))
    keys: set[tuple[str, str]] = set()
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT extra_json FROM vault_transactions "
                "WHERE LOWER(user_id)=? AND tx_type='redeem' AND created_at>=?",
                (wallet, cutoff),
            ).fetchall()
        for row in rows or []:
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except Exception:
                continue
            cid = str(extra.get("condition_id") or "").strip().lower()
            outcome = str(extra.get("outcome") or "").strip().upper()
            if cid and outcome:
                keys.add((cid, outcome))
    except Exception as exc:
        logger.warning(
            "recently_redeemed_keys query failed wallet=%s err=%s", wallet, exc
        )
    return keys


def _build_manual_pm_bets(
    wallet_address: str,
    status: str,
    search: str,
    category: str,
    tag: str = "",
) -> tuple[str, list[dict]]:
    state = _latest_pm_state(wallet_address)
    profile = state.get("profile") or {}
    pm_wallet = str(profile.get("polymarket_wallet") or "").strip().lower()
    if not _ADDR_RE.match(pm_wallet):
        return "", []

    positions = state.get("positions") or []
    activities = _all_pm_activity_rows(wallet_address)
    closed_positions = _fetch_closed_positions_full(pm_wallet, limit=3000)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    category_slugs = _load_pm_category_slugs()
    tag_category_map = _load_pm_tag_category_map()
    # T466 + T916: mask positions that the user just redeemed locally. PM
    # Data API typically reflects the close within minutes, but we have
    # observed cases where a redeemed position re-appears after 30 min
    # because the indexer treats partial / multi-outcome redeems as still
    # open. A 7-day mask is well within "stale data" tolerance and
    # eliminates the user-visible "already redeemed bet reappears" bug.
    redeemed_mask = _recently_redeemed_keys(wallet_address, ttl_seconds=7 * 86400)

    bets: list[dict] = []

    # OPEN positions are sourced strictly from current positions snapshot to preserve
    # one-record-per-live-position consistency with PM Data API.
    for idx, p in enumerate(positions):
        raw = _safe_json_loads(str(p.get("raw_json") or ""))
        entered_ts = _normalize_unix_ts(
            raw.get("timestamp") or raw.get("createdAtTs"), 0
        )
        if entered_ts <= 0:
            entered_ts = now_ts
        end_date = str(p.get("end_date") or raw.get("endDate") or "")
        end_dt = _parse_iso(end_date)
        matured = bool(end_dt and int(end_dt.timestamp()) <= now_ts - 300)
        cur_price = _safe_float(p.get("cur_price"), 0.0)
        redeemable_flag = bool(
            raw.get("redeemable")
            or raw.get("isRedeemable")
            or raw.get("claimable")
            or raw.get("isClaimable")
        )
        winner_flag = str(
            raw.get("winner") or raw.get("isWinner") or ""
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "winner",
        }
        # Data API open positions may stay in "open" even after market resolution.
        # Expose a stable redeemability hint for frontend status/button rendering.
        can_redeem = bool(
            redeemable_flag or winner_flag or (matured and cur_price >= 0.985)
        )
        # T913: PM Data API's `percentPnl` field has been observed returning
        # values inconsistent with `cashPnl/initialValue` (e.g. -0.746 for a
        # -$0.04 unrealized loss on $5.94 cost basis, which should be
        # ≈ -0.7%, not -74.6%). Recompute deterministically from the two
        # absolute values we already trust. Fall back to PM's reported
        # ratio only when initial_value is unusable.
        cash_pnl_val = _safe_float(p.get("cash_pnl"), 0.0)
        initial_val_for_pct = _safe_float(p.get("initial_value"), 0.0)
        if initial_val_for_pct > 1e-9:
            pnl_ratio = cash_pnl_val / initial_val_for_pct
        else:
            pnl_ratio = _normalize_ratio(p.get("percent_pnl"))
        title = p.get("title") or "Unknown"
        slug = p.get("slug") or ""
        category_name = _infer_market_category(
            title,
            slug,
            raw,
            category_slugs,
            tag_category_map,
        )
        tags = _extract_pm_tags(raw, category_name)
        size = _safe_float(p.get("size"), 0.0)
        initial_value = _safe_float(p.get("initial_value"), 0.0)
        if size <= 1e-9 or initial_value <= 1e-9:
            continue
        # T466: drop positions just redeemed; PM Data API will sync within minutes.
        cond_lc = (
            str(p.get("condition_id") or raw.get("conditionId") or "").strip().lower()
        )
        outcome_uc = str(p.get("outcome") or "").strip().upper()
        if cond_lc and outcome_uc and (cond_lc, outcome_uc) in redeemed_mask:
            continue
        bets.append(
            {
                "bet_id": f"pm-open-{p.get('condition_id') or idx}-{str(p.get('outcome') or '').lower()}",
                "ts_entered": entered_ts,
                "question": title,
                "category": category_name,
                "outcome": p.get("outcome") or "?",
                "size_usdc": initial_value,
                "market_price": _safe_float(p.get("avg_price"), 0.0),
                "current_price": cur_price,
                "matched_shares": size,
                "resolved_outcome": None,
                "resolved_ts": None,
                "resolved_at": None,
                "unrealized_pnl": _safe_float(p.get("cash_pnl"), 0.0),
                "unrealized_pnl_pct": pnl_ratio,
                "pnl": None,
                "pnl_pct": None,
                "is_win": 1 if can_redeem else 0,
                "can_redeem": can_redeem,
                "exit_type": None,
                "market_id": p.get("event_id") or "",
                "condition_id": p.get("condition_id") or raw.get("conditionId") or "",
                "market_slug": p.get("slug") or "",
                "event_slug": p.get("event_slug") or "",
                "market_end_date": end_date,
                "bet_type": "entry",
                "strategy_tag": "manual_pm",
                "dimension_tags": "manual_user",
                "tags": tags,
            }
        )

    # RESOLVED history prefers the official closed-positions endpoint.
    if closed_positions:
        for idx, cp in enumerate(closed_positions):
            avg_price = _safe_float(cp.get("avgPrice"), 0.0)
            total_bought = _safe_float(cp.get("totalBought"), 0.0)
            realized_pnl = _safe_float(cp.get("realizedPnl"), 0.0)
            matched_shares = total_bought / avg_price if avg_price > 1e-9 else 0.0
            ts_entered = _normalize_unix_ts(cp.get("timestamp"), 0)
            if ts_entered <= 0:
                ts_entered = now_ts
            title = cp.get("title") or "Unknown"
            slug = cp.get("slug") or ""
            category_name = _infer_market_category(
                title,
                slug,
                cp,
                category_slugs,
                tag_category_map,
            )
            tags = _extract_pm_tags(cp, category_name)
            pnl_pct = realized_pnl / total_bought if total_bought > 1e-9 else 0.0

            bets.append(
                {
                    "bet_id": f"pm-closed-{cp.get('conditionId') or idx}-{str(cp.get('outcome') or '').lower()}-{ts_entered}",
                    "ts_entered": ts_entered,
                    "question": title,
                    "category": category_name,
                    "outcome": cp.get("outcome") or "?",
                    "size_usdc": total_bought,
                    "market_price": avg_price,
                    "current_price": _safe_float(cp.get("curPrice"), 0.0) or None,
                    "matched_shares": matched_shares,
                    "resolved_outcome": "SOLD",
                    "resolved_ts": ts_entered,
                    "resolved_at": datetime.fromtimestamp(
                        ts_entered, tz=timezone.utc
                    ).isoformat(),
                    "unrealized_pnl": None,
                    "unrealized_pnl_pct": None,
                    "pnl": realized_pnl,
                    "pnl_pct": pnl_pct,
                    "is_win": 1 if realized_pnl >= 0 else 0,
                    "can_redeem": False,
                    "exit_type": "manual_exit",
                    "market_id": cp.get("eventId") or "",
                    "market_slug": slug,
                    "event_slug": cp.get("eventSlug") or "",
                    "market_end_date": cp.get("endDate"),
                    "bet_type": "sold",
                    "strategy_tag": "manual_pm",
                    "dimension_tags": "manual_user",
                    "tags": tags,
                }
            )
    else:
        # Fallback: if closed-positions is unavailable, infer a resolved history from
        # activity snapshots to avoid returning an empty resolved tab.
        sorted_acts = sorted(
            activities,
            key=lambda a: _safe_int(a.get("activity_ts"), 0),
            reverse=True,
        )
        seen_tx = set()
        for idx, act in enumerate(sorted_acts):
            raw = _safe_json_loads(str(act.get("raw_json") or ""))
            activity_type = str(act.get("activity_type") or "").strip().lower()
            side = (
                str(
                    raw.get("side")
                    or raw.get("tradeSide")
                    or raw.get("action")
                    or raw.get("orderSide")
                    or ""
                )
                .strip()
                .lower()
            )
            is_exit = side in {
                "sell",
                "redeem",
                "merge",
                "withdraw",
                "cashout",
            } or activity_type in {
                "redeem",
                "merge",
                "withdraw",
                "cashout",
            }
            if not is_exit:
                continue

            tx_hash = str(act.get("tx_hash") or "").strip()
            if tx_hash and tx_hash in seen_tx:
                continue
            if tx_hash:
                seen_tx.add(tx_hash)

            size = abs(_safe_float(act.get("size"), 0.0))
            usdc_size = abs(_safe_float(act.get("usdc_size"), 0.0))
            price = _safe_float(act.get("price"), 0.0)
            if usdc_size <= 0 and size > 0 and price > 0:
                usdc_size = size * price
            if size <= 0 and usdc_size > 0 and price > 0:
                size = usdc_size / price
            if size <= 1e-9 and usdc_size <= 1e-9:
                continue

            ts_entered = _normalize_unix_ts(act.get("activity_ts"), 0)
            if ts_entered <= 0:
                ts_entered = now_ts
            title = act.get("title") or raw.get("title") or "Unknown"
            slug = raw.get("slug") or ""
            category_name = _infer_market_category(
                title,
                slug,
                raw,
                category_slugs,
                tag_category_map,
            )
            tags = _extract_pm_tags(raw, category_name)
            realized = _safe_float(
                raw.get("cashPnl") or raw.get("realizedPnl") or raw.get("pnl"), 0.0
            )
            pnl_pct = _normalize_ratio(raw.get("percentPnl"))
            if abs(pnl_pct) < 1e-9 and usdc_size > 1e-9 and abs(realized) > 1e-9:
                pnl_pct = realized / usdc_size

            bets.append(
                {
                    "bet_id": f"pm-resolved-{tx_hash or idx}",
                    "ts_entered": ts_entered,
                    "question": title,
                    "category": category_name,
                    "outcome": act.get("outcome") or raw.get("outcome") or "?",
                    "size_usdc": usdc_size,
                    "market_price": price,
                    "current_price": None,
                    "matched_shares": size,
                    "resolved_outcome": "SOLD",
                    "resolved_ts": ts_entered,
                    "resolved_at": datetime.fromtimestamp(
                        ts_entered, tz=timezone.utc
                    ).isoformat(),
                    "unrealized_pnl": None,
                    "unrealized_pnl_pct": None,
                    "pnl": realized,
                    "pnl_pct": pnl_pct,
                    "is_win": 1 if realized >= 0 else 0,
                    "can_redeem": False,
                    "exit_type": "manual_exit",
                    "market_id": act.get("event_id") or raw.get("eventId") or "",
                    "market_slug": slug,
                    "event_slug": act.get("event_slug") or raw.get("eventSlug") or "",
                    "market_end_date": raw.get("endDate"),
                    "bet_type": "sold",
                    "strategy_tag": "manual_pm",
                    "dimension_tags": "manual_user",
                    "tags": tags,
                }
            )

    # T920/T921: Synthesize REDEEM bets from local vault_transactions.
    # PM Data API's /closed-positions endpoint excludes redeemed positions
    # entirely (only SOLD positions appear there) and PM /activity REDEEM
    # rows have empty outcome / size=0 / usdc_size=0, so without this
    # block any bet that the user redeemed via the on-chain CTF
    # redeemPositions call disappears from /v2/trades and from the
    # dashboard's Track Distribution chart. We are the authoritative
    # writer of vault_transactions tx_type='redeem' rows (extra_json
    # carries condition_id + outcome), so this synthesis is exact.
    seen_redeem_keys: set[tuple[str, str]] = set()
    try:
        with db_connect() as conn:
            redeem_rows = conn.execute(
                "SELECT extra_json, created_at FROM vault_transactions "
                "WHERE LOWER(user_id)=? AND tx_type='redeem' "
                "ORDER BY created_at DESC",
                (wallet_address.lower(),),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[T920] vault_transactions REDEEM query failed wallet=%s err=%s",
            wallet_address,
            exc,
        )
        redeem_rows = []

    for r in redeem_rows or []:
        try:
            extra = json.loads(r["extra_json"] or "{}")
        except Exception:
            continue
        cond_lc = str(extra.get("condition_id") or "").strip().lower()
        outcome_uc = str(extra.get("outcome") or "").strip().upper()
        if not cond_lc or not outcome_uc:
            continue
        key = (cond_lc, outcome_uc)
        if key in seen_redeem_keys:
            continue
        seen_redeem_keys.add(key)

        cost_basis = 0.0
        buy_shares = 0.0
        title = "Unknown"
        slug = ""
        end_date = ""
        raw_for_cat: dict = {}
        for act in activities:
            a_cond = (
                str(act.get("condition_id") or "").strip().lower()
            )
            if a_cond != cond_lc:
                continue
            a_raw = _safe_json_loads(str(act.get("raw_json") or ""))
            a_outcome = str(
                act.get("outcome") or a_raw.get("outcome") or ""
            ).strip().upper()
            a_side = str(a_raw.get("side") or "").strip().lower()
            if a_side == "buy" and a_outcome == outcome_uc:
                cost_basis += abs(_safe_float(act.get("usdc_size"), 0.0))
                buy_shares += abs(_safe_float(act.get("size"), 0.0))
                title = act.get("title") or a_raw.get("title") or title
                slug = a_raw.get("slug") or slug
                end_date = a_raw.get("endDate") or end_date
                if not raw_for_cat:
                    raw_for_cat = a_raw

        proceeds = 0.0
        cur_price = 0.0
        for p in positions:
            if (
                str(p.get("condition_id") or "").strip().lower() == cond_lc
                and str(p.get("outcome") or "").strip().upper() == outcome_uc
            ):
                proceeds = max(0.0, _safe_float(p.get("current_value"), 0.0))
                cur_price = _safe_float(p.get("cur_price"), 0.0)
                if cost_basis < 1e-9:
                    cost_basis = _safe_float(p.get("initial_value"), 0.0)
                if buy_shares < 1e-9:
                    buy_shares = _safe_float(p.get("size"), 0.0)
                if title == "Unknown":
                    title = p.get("title") or title
                if not slug:
                    slug = p.get("slug") or ""
                if not end_date:
                    end_date = str(p.get("end_date") or "")
                break

        # Heuristic: if position snapshot unavailable but raw curPrice ≈ 1
        # the user redeemed a winning outcome → proceeds = shares.
        if proceeds < 1e-9 and cur_price >= 0.985 and buy_shares > 1e-9:
            proceeds = buy_shares

        if cost_basis < 1e-9 and buy_shares < 1e-9:
            continue

        realized = proceeds - cost_basis
        pnl_pct = (realized / cost_basis) if cost_basis > 1e-9 else 0.0
        ts_redeem_dt = _parse_iso(str(r["created_at"] or ""))
        ts_redeem = (
            int(ts_redeem_dt.timestamp()) if ts_redeem_dt else now_ts
        )

        category_name = _infer_market_category(
            title, slug, raw_for_cat, category_slugs, tag_category_map
        )
        tags = _extract_pm_tags(raw_for_cat, category_name)

        bets.append(
            {
                "bet_id": f"pm-redeemed-{cond_lc}-{outcome_uc.lower()}",
                "ts_entered": ts_redeem,
                "question": title,
                "category": category_name,
                "outcome": outcome_uc.title(),
                "size_usdc": cost_basis,
                "market_price": (
                    cost_basis / buy_shares if buy_shares > 1e-9 else 0.0
                ),
                "current_price": None,
                "matched_shares": buy_shares,
                "resolved_outcome": "REDEEMED",
                "resolved_ts": ts_redeem,
                "resolved_at": datetime.fromtimestamp(
                    ts_redeem, tz=timezone.utc
                ).isoformat(),
                "unrealized_pnl": None,
                "unrealized_pnl_pct": None,
                "pnl": realized,
                "pnl_pct": pnl_pct,
                "is_win": 1 if realized >= 0 else 0,
                "can_redeem": False,
                "exit_type": "redeem",
                "market_id": "",
                "condition_id": cond_lc,
                "market_slug": slug,
                "event_slug": "",
                "market_end_date": end_date,
                "bet_type": "redeemed",
                "strategy_tag": "manual_pm",
                "dimension_tags": "manual_user",
                "tags": tags,
            }
        )

    status_key = str(status or "").strip().lower()
    if status_key == "open":
        bets = [b for b in bets if not b.get("resolved_outcome")]
    elif status_key == "resolved":
        bets = [b for b in bets if b.get("resolved_outcome")]
    elif status_key in {"win", "profit_take"}:
        bets = [
            b
            for b in bets
            if b.get("resolved_outcome") and _safe_float(b.get("pnl"), 0.0) > 0
        ]
    elif status_key in {"loss", "stop_loss"}:
        bets = [
            b
            for b in bets
            if b.get("resolved_outcome") and _safe_float(b.get("pnl"), 0.0) < 0
        ]
    elif status_key == "manual_exit":
        bets = [b for b in bets if b.get("resolved_outcome")]

    keyword = str(search or "").strip().lower()
    if keyword:
        bets = [
            b
            for b in bets
            if keyword in str(b.get("question") or "").lower()
            or keyword in str(b.get("market_slug") or "").lower()
            or keyword in str(b.get("event_slug") or "").lower()
            or keyword in str(b.get("market_id") or "").lower()
        ]

    cat_key = _normalize_category_slug(str(category or ""))
    if cat_key:
        bets = [
            b
            for b in bets
            if _normalize_category_slug(str(b.get("category") or "")) == cat_key
        ]

    tag_key = _normalize_tag_token(str(tag or ""))
    if tag_key:
        bets = [
            b
            for b in bets
            if tag_key in [_normalize_tag_token(t) for t in (b.get("tags") or []) if t]
        ]

    bets.sort(key=lambda x: _safe_int(x.get("ts_entered"), 0), reverse=True)
    return pm_wallet, bets


@router.get("/api/user/privy/config")
async def privy_config():
    app_id = os.environ.get("PRIVY_APP_ID", "").strip()
    app_secrets = _load_privy_secrets()
    return {
        "enabled": bool(app_id and app_secrets),
        "app_id": app_id,
        "login_methods": ["wallet", "email"],
        "signer": _load_privy_server_signer_config(),
    }


@router.post("/api/user/privy/signer-enrollment")
async def user_privy_signer_enrollment(request: Request):
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    # Ensure signer-related columns exist before updating users table on older DB schema.
    _ensure_user_tables()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    signer_cfg = _load_privy_server_signer_config()
    if not signer_cfg.get("enabled"):
        return JSONResponse(
            {
                "error": "SIGNER_NOT_CONFIGURED",
                "message": "Server signer config is disabled",
            },
            status_code=412,
        )

    wallet_address = str(identity.get("wallet_address") or "").strip().lower()
    if not _ADDR_RE.match(wallet_address):
        return JSONResponse({"error": "invalid wallet in session"}, status_code=422)

    status = str(body.get("status") or "").strip().lower()
    if status not in {"pending", "enrolled", "error"}:
        return JSONResponse({"error": "invalid status"}, status_code=422)

    signer_id = str(body.get("signer_id") or signer_cfg.get("signer_id") or "").strip()
    expected_signer_id = str(signer_cfg.get("signer_id") or "").strip()
    if expected_signer_id and signer_id != expected_signer_id:
        return JSONResponse({"error": "signer_id mismatch"}, status_code=422)

    policy_ids = (
        body.get("policy_ids")
        if isinstance(body.get("policy_ids"), list)
        else signer_cfg.get("policy_ids") or []
    )
    policy_ids = [str(x).strip() for x in policy_ids if str(x).strip()]
    error_text = str(body.get("error") or "").strip()[:500]
    now = datetime.now(timezone.utc).isoformat()

    def _db():

        with db_connect() as conn:
            conn.execute(
                """
            UPDATE users
            SET privy_signer_status=?,
                privy_signer_policies_json=?,
                privy_signer_error=?,
                privy_signer_updated_at=?,
                updated_at=?
            WHERE wallet_address=?
            """,
                (
                    status,
                    json.dumps(policy_ids, ensure_ascii=False),
                    error_text,
                    now,
                    now,
                    wallet_address,
                ),
            )
            conn.commit()

        return {
            "status": "ok",
            "wallet_address": wallet_address,
            "privy_signer_status": status,
            "privy_signer_id": signer_id,
            "privy_signer_policy_ids": policy_ids,
        }

    return await asyncio.to_thread(_db)


@router.post("/api/user/privy/verify")
async def user_privy_verify(request: Request):
    # Ensure users table has latest signer/mfa columns before any SELECT/UPSERT.
    _ensure_user_tables()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    # Official primary auth: Authorization: Bearer <accessToken>
    # Extract this FIRST so it takes precedence as the access_token candidate.
    _authz_header = str(request.headers.get("authorization", "")).strip()
    _bearer_token = (
        _authz_header[7:].strip() if _authz_header.lower().startswith("bearer ") else ""
    )

    id_token = str(body.get("id_token", "")).strip()
    if not id_token:
        id_token = str(body.get("identity_token", "")).strip()
    if not id_token:
        id_token = str(request.headers.get("privy-id-token", "")).strip()
    if not id_token:
        id_token = str(request.cookies.get("privy-id-token", "")).strip()
    if not id_token and _bearer_token:
        id_token = _bearer_token  # last fallback for id_token

    # access_token: prefer Authorization: Bearer header (official Privy pattern) over body
    access_token = _bearer_token or str(body.get("access_token", "")).strip()
    if not access_token:
        access_token = str(request.cookies.get("privy-token", "")).strip()
    if not access_token:
        access_token = str(request.headers.get("privy-token", "")).strip()

    # Privy verify diagnostics: only log metadata (length/shape), never log secret value.
    app_id_env = str(os.environ.get("PRIVY_APP_ID", "") or "")
    app_secret_env = str(os.environ.get("PRIVY_APP_SECRET", "") or "")
    app_secret_alt_env = str(os.environ.get("PRIVY_APP_SECRET_ALT", "") or "")
    logger.warning("=== PRIVY VERIFY DEBUG ===")
    logger.warning(
        "PRIVY_APP_ID from env: %s",
        (app_id_env[:20] + "...") if app_id_env else "MISSING",
    )
    logger.warning(
        "PRIVY_APP_SECRET length raw=%s stripped=%s alt_raw=%s alt_stripped=%s",
        len(app_secret_env),
        len(app_secret_env.strip()),
        len(app_secret_alt_env),
        len(app_secret_alt_env.strip()),
    )
    logger.warning(
        "Received token length access=%s id=%s bearer=%s",
        len(access_token or ""),
        len(id_token or ""),
        len(_bearer_token or ""),
    )
    logger.warning(
        "Token aud access=%s id=%s",
        _token_claim_brief(access_token).get("aud", "无法解析"),
        _token_claim_brief(id_token).get("aud", "无法解析"),
    )
    logger.warning("=============================")

    client_user = body.get("user") if isinstance(body.get("user"), dict) else {}

    try:
        try:
            privy_user_id, verified_user = _verify_privy_tokens(
                id_token, access_token, client_user
            )
        except TypeError:
            # Backward compatibility for tests/patches that monkeypatch a 2-arg verifier.
            privy_user_id, verified_user = _verify_privy_tokens(
                id_token,
                access_token,
            )
    except Exception as exc:
        return JSONResponse(
            {
                "error": f"privy verify failed: {exc}",
                "diagnostics": {
                    "id_token_shape": _token_debug_shape(
                        _normalize_auth_token(id_token)
                    ),
                    "access_token_shape": _token_debug_shape(
                        _normalize_auth_token(access_token)
                    ),
                },
            },
            status_code=401,
        )

    # Merge verified API user + client-side user object for maximum wallet coverage
    if verified_user:
        # Enrich API response with linkedAccounts from client if API didn't return them
        if not verified_user.get("linkedAccounts") and not verified_user.get(
            "linked_accounts"
        ):
            verified_user = {**verified_user, **client_user}
        source_user = verified_user
    else:
        source_user = client_user

    wallets, primary_wallet = _extract_wallets_from_privy_user(source_user)
    embedded_wallet_addr, embedded_wallet_id = _extract_embedded_wallet_binding(
        source_user
    )
    profile = _extract_profile_from_privy_user(source_user)

    if not primary_wallet:
        # Last resort: wallet_address sent explicitly by frontend
        fallback = str(body.get("wallet_address", "")).strip().lower()
        if _ADDR_RE.match(fallback):
            primary_wallet = fallback
            wallets = [fallback]

    # T462: stable user identity. If a row already exists for this Privy DID,
    # always reuse its wallet_address as the primary key, regardless of how the
    # current Privy linked_accounts list orders things. This prevents a second
    # user row from being inserted whenever a new linked account (e.g. a freshly
    # provisioned Privy embedded wallet) becomes the natural primary candidate.
    if privy_user_id:
        try:
            with db_connect() as _conn_lookup:
                existing_row = _conn_lookup.execute(
                    "SELECT wallet_address FROM users WHERE privy_user_id=%s ORDER BY created_at ASC LIMIT 1",
                    (privy_user_id,),
                ).fetchone()
            if existing_row:
                existing_wallet = (
                    str(
                        (
                            existing_row["wallet_address"]
                            if hasattr(existing_row, "keys")
                            else existing_row[0]
                        )
                        or ""
                    )
                    .strip()
                    .lower()
                )
                if _ADDR_RE.match(existing_wallet):
                    if existing_wallet != primary_wallet:
                        logger.info(
                            "verify: reusing existing primary wallet for did=%s existing=%s candidate=%s",
                            privy_user_id,
                            existing_wallet,
                            primary_wallet,
                        )
                    primary_wallet = existing_wallet
                    if existing_wallet not in wallets:
                        wallets.insert(0, existing_wallet)
        except Exception as _exc:
            logger.warning("verify: existing-user lookup by did failed: %s", _exc)

    if not primary_wallet:
        # If user is authenticated but has no wallet, auto-create a session keyed by DID
        # This supports email/social logins where wallet may be provisioned separately
        return JSONResponse(
            {
                "error": "no wallet linked in Privy account",
                "privy_user_id": privy_user_id,
            },
            status_code=400,
        )

    # Preflight wallets/authenticate during verify for observability.
    # Keep login flow non-blocking here and let redeem/withdraw runtime handle final signer validation.
    try:
        from scripts.dashboard_routes import (
            redeem_withdraw_routes as _redeem_routes,
        )  # noqa: PLC0415

        preflight_keys = _redeem_routes._load_privy_user_authorization_keys(
            [
                access_token,
                id_token,
            ]
        )
        if not preflight_keys:
            logger.warning(
                "verify wallets/authenticate preflight failed (degraded-continue) wallet=%s access_shape=%s id_shape=%s access_claim=%s id_claim=%s",
                primary_wallet,
                _token_debug_shape(_normalize_auth_token(access_token)),
                _token_debug_shape(_normalize_auth_token(id_token)),
                _token_claim_brief(access_token),
                _token_claim_brief(id_token),
            )
        else:
            _redeem_routes._cache_verified_user_jwts(
                primary_wallet,
                [access_token, id_token],
                ttl_seconds=1800,
            )
            logger.info(
                "verify wallets/authenticate preflight ok wallet=%s access_shape=%s id_shape=%s",
                primary_wallet,
                _token_debug_shape(_normalize_auth_token(access_token)),
                _token_debug_shape(_normalize_auth_token(id_token)),
            )
    except Exception as exc:
        logger.warning(
            "verify wallets/authenticate preflight exception (degraded-continue) wallet=%s err=%s access_claim=%s id_claim=%s",
            primary_wallet,
            exc,
            _token_claim_brief(access_token),
            _token_claim_brief(id_token),
        )

    # Cache verified JWT candidates for short-lived server-side reuse in redeem/withdraw paths.
    try:
        if "_redeem_routes" in locals():
            _redeem_routes._cache_verified_user_jwts(
                primary_wallet,
                [access_token, id_token],
                ttl_seconds=1800,
            )
    except Exception:
        pass

    with db_connect() as conn:
        try:
            mfa_row = conn.execute(
                "SELECT mfa_enabled, mfa_methods, privy_signer_status, privy_signer_policies_json, totp_enabled, totp_secret FROM users WHERE wallet_address=?",
                (primary_wallet,),
            ).fetchone()
        except Exception as exc:
            # Backward compatibility: some production DBs may lag signer columns.
            # Do not fail login; retry with legacy columns only.
            logger.warning(
                "users signer columns unavailable in verify path; fallback query: %s",
                exc,
            )
            mfa_row = conn.execute(
                "SELECT mfa_enabled, mfa_methods FROM users WHERE wallet_address=?",
                (primary_wallet,),
            ).fetchone()

    mfa_enabled = bool(_safe_row_get(mfa_row, "mfa_enabled")) if mfa_row else False
    # T902: 服务端 TOTP 是前台登录唯一可强制校验的 MFA 因子。
    # 仅当用户完成 TOTP 注册（totp_enabled=true）时登录门禁才生效。
    totp_enabled = bool(_safe_row_get(mfa_row, "totp_enabled")) if mfa_row else False
    totp_secret = str(_safe_row_get(mfa_row, "totp_secret") or "").strip()
    mfa_methods = []
    if mfa_row:
        methods_raw = _safe_row_get(mfa_row, "mfa_methods")
        if isinstance(methods_raw, str):
            try:
                mfa_methods = json.loads(methods_raw)
            except Exception:
                mfa_methods = []
        elif isinstance(methods_raw, list):
            mfa_methods = methods_raw
    mfa_methods = _normalize_mfa_methods(mfa_methods)
    signer_status = (
        str(_safe_row_get(mfa_row, "privy_signer_status") or "").strip().lower()
    )
    signer_policy_ids = []
    signer_policies_raw = _safe_row_get(mfa_row, "privy_signer_policies_json")
    if isinstance(signer_policies_raw, str) and signer_policies_raw:
        try:
            signer_policy_ids = json.loads(signer_policies_raw)
        except Exception:
            signer_policy_ids = []

    # T902 安全修复：不再采信请求体中的 mfa_verified 布尔值（可被客户端任意声明，
    # 等于没有 MFA）。登录门禁只认两类凭据：
    #   1) 服务端 TOTP 校验通过后签发的 mfa_verified cookie；或
    #   2) 本请求携带的 mfa_code（服务端当场校验 TOTP）。
    mfa_verified_in_session = mfa_cookie_verify(
        request.cookies.get(_MFA_VERIFY_COOKIE, ""), primary_wallet
    )
    mfa_code = str(body.get("mfa_code") or "").strip()

    if totp_enabled and not mfa_verified_in_session:
        if not mfa_code or not totp_secret or not verify_totp(totp_secret, mfa_code):
            logger.warning(
                "login MFA gate denied wallet=%s ip=%s",
                primary_wallet,
                request.client.host if request.client else "",
            )
            return JSONResponse(
                {
                    "error": "mfa_required",
                    "message": "账户已开启 MFA，请先完成二次验证后再登录。",
                    "methods": ["totp"],
                    "mfa_type": "totp",
                },
                status_code=403,
            )
        mfa_verified_in_session = True

    # 若用户没有头像，生成基于钱包地址的 blockies 风格几何图案 SVG
    if not profile["avatar_url"]:
        profile["avatar_url"] = _generate_blockies_svg(primary_wallet)

    now = datetime.now(timezone.utc).isoformat()
    source_ip = request.client.host if request.client else ""
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users(
                wallet_address, role, privy_user_id, auth_provider, linked_wallets_json,
                privy_embedded_wallet_id, privy_linked_accounts_json,
                display_name, avatar_url, email, email_verified,
                created_at, last_login_at, source_ip, updated_at
            )
            VALUES(%s, 'manual_user', %s, 'privy', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(wallet_address) DO UPDATE SET
                privy_user_id=excluded.privy_user_id,
                auth_provider='privy',
                linked_wallets_json=excluded.linked_wallets_json,
                privy_embedded_wallet_id=excluded.privy_embedded_wallet_id,
                privy_linked_accounts_json=excluded.privy_linked_accounts_json,
                display_name=CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE COALESCE(users.display_name, '') END,
                avatar_url=CASE WHEN excluded.avatar_url != '' THEN excluded.avatar_url ELSE COALESCE(users.avatar_url, '') END,
                email=CASE WHEN excluded.email != '' THEN excluded.email ELSE COALESCE(users.email, '') END,
                email_verified=CASE WHEN excluded.email_verified IN (0,1) THEN excluded.email_verified ELSE COALESCE(users.email_verified, 0) END,
                last_login_at=excluded.last_login_at,
                source_ip=excluded.source_ip,
                updated_at=excluded.updated_at
            """,
            (
                primary_wallet,
                privy_user_id,
                json.dumps(wallets, ensure_ascii=False),
                embedded_wallet_id,
                json.dumps(
                    source_user.get("linked_accounts")
                    or source_user.get("linkedAccounts")
                    or [],
                    ensure_ascii=False,
                ),
                profile["display_name"],
                profile["avatar_url"],
                profile["email"],
                profile["email_verified"],
                now,
                now,
                source_ip,
                now,
            ),
        )
        if totp_enabled and mfa_verified_in_session:
            conn.execute(
                "UPDATE users SET mfa_last_verified_at=?, updated_at=? WHERE wallet_address=?",
                (now, now, primary_wallet),
            )
        conn.commit()

    signer_cfg = _load_privy_server_signer_config()
    signer_jwt_candidates = [
        _normalize_auth_token(access_token),
        _normalize_auth_token(id_token),
    ]
    if (
        signer_cfg.get("enabled")
        and embedded_wallet_id
        and str(signer_status or "").strip().lower() != "enrolled"
    ):
        enroll_status, enroll_policy_ids, enroll_error = (
            _attempt_server_signer_enrollment(
                embedded_wallet_id=embedded_wallet_id,
                user_jwt=signer_jwt_candidates,
            )
        )
        if enroll_status:
            signer_status = enroll_status
            signer_policy_ids = enroll_policy_ids
            with db_connect() as conn:
                conn.execute(
                    """
                    UPDATE users
                    SET privy_signer_status=?,
                        privy_signer_policies_json=?,
                        privy_signer_error=?,
                        privy_signer_updated_at=?,
                        updated_at=?
                    WHERE wallet_address=?
                    """,
                    (
                        enroll_status,
                        json.dumps(enroll_policy_ids, ensure_ascii=False),
                        enroll_error,
                        now,
                        now,
                        primary_wallet,
                    ),
                )
                conn.commit()
            if enroll_error:
                logger.warning(
                    "verify-time signer enrollment failed wallet=%s wallet_id=%s err=%s",
                    primary_wallet,
                    embedded_wallet_id,
                    enroll_error,
                )
            else:
                logger.info(
                    "verify-time signer enrollment succeeded wallet=%s wallet_id=%s",
                    primary_wallet,
                    embedded_wallet_id,
                )

    token = create_user_session(
        wallet_address=primary_wallet,
        role="manual_user",
        source_ip=source_ip,
    )

    _get_or_create_runtime(primary_wallet)
    pm_binding_required = True
    auto_provisioned = False
    try:
        # Auto-discover PM proxy wallet from Privy-linked wallets and sync tenant data.
        sync_state = _sync_user_pm_data(
            primary_wallet,
            wallets,
            force=True,
            login_email=profile.get("email", ""),
            embedded_wallet_addr=embedded_wallet_addr or "",
        )
        pm_binding_required = bool(sync_state.get("needs_pm_binding", False))
        auto_provisioned = bool(sync_state.get("auto_provisioned", False))
    except Exception:
        pass

    # T793: Award registration points for new users (idempotent via point_transactions check).
    # T794: Process referral code for new users if provided in request body.
    try:
        from scripts.dashboard_routes.points_routes import (  # noqa: PLC0415
            _add_points,
            _process_referral_chain,
            _rule_points,
        )

        with db_connect() as _pts_conn:
            _existing_reg_tx = _pts_conn.execute(
                "SELECT id FROM point_transactions WHERE wallet_address=? AND source='registration' LIMIT 1",
                (primary_wallet,),
            ).fetchone()

        if not _existing_reg_tx:
            # New user — strict rules only, no hardcoded fallback.
            _reg_bonus = int(_rule_points("signup", default=0) or 0)
            if _reg_bonus > 0:
                _add_points(
                    primary_wallet,
                    _reg_bonus,
                    "registration",
                    "New user registration bonus",
                    "",
                )
                logger.info(
                    "T793: awarded %d registration points wallet=%s",
                    _reg_bonus,
                    primary_wallet,
                )
            else:
                logger.info(
                    "T793: signup rule inactive/missing, skip registration bonus wallet=%s",
                    primary_wallet,
                )

            # T794: Process referral code if new user brought a ref_code.
            _ref_code = (
                str(body.get("ref_code") or body.get("referral_code") or "")
                .strip()
                .upper()
            )
            if _ref_code:
                try:
                    with db_connect() as _ref_check_conn:
                        _already_referred = _ref_check_conn.execute(
                            "SELECT id FROM user_referrals WHERE LOWER(referred_wallet)=? LIMIT 1",
                            (primary_wallet,),
                        ).fetchone()
                    if not _already_referred:
                        _ref_chain = _process_referral_chain(primary_wallet, _ref_code)
                        if _ref_chain:
                            logger.info(
                                "T794: referral relation bound code=%s wallet=%s levels=%d",
                                _ref_code,
                                primary_wallet,
                                len(_ref_chain),
                            )
                except Exception as _ref_exc:
                    logger.warning(
                        "T794: referral processing failed wallet=%s code=%s err=%s",
                        primary_wallet,
                        _ref_code,
                        _ref_exc,
                    )
    except Exception as _pts_exc:
        logger.warning(
            "T793: registration points/referral failed wallet=%s err=%s",
            primary_wallet,
            _pts_exc,
        )

    # 查询实际 role（可能是 super_admin）并在首个登录钩子写入 onboarding 需求。
    with db_connect() as conn:
        role_row = conn.execute(
            "SELECT role, COALESCE(display_name, '') AS display_name FROM users WHERE wallet_address=?",
            (primary_wallet,),
        ).fetchone()
    actual_role = (role_row["role"] if role_row else "manual_user") or "manual_user"
    current_display_name = str((role_row["display_name"] if role_row else "") or "")
    try:
        request.session["needs_onboarding"] = _needs_onboarding(
            current_display_name,
            primary_wallet,
        )
        request.session["onboarding_wallet"] = primary_wallet.lower()
    except Exception:
        pass

    # T795: Attempt server-side Safe deployment for users whose Privy embedded wallet
    # has been enrolled with the server signer. Fire-and-forget (never blocks login).
    safe_deployed = False
    safe_address = ""
    if (
        embedded_wallet_id
        and embedded_wallet_addr
        and str(signer_status or "").strip().lower() == "enrolled"
    ):
        try:
            with db_connect() as _safe_conn:
                _safe_row = _safe_conn.execute(
                    "SELECT funder_address FROM users WHERE wallet_address=?",
                    (primary_wallet,),
                ).fetchone()
            safe_address = str(
                (_safe_row["funder_address"] if _safe_row else None) or ""
            ).strip()
            from scripts.dashboard_routes import (  # noqa: PLC0415
                redeem_withdraw_routes as _redeem_routes,
            )

            # Only deploy if Safe not yet deployed on-chain.
            if safe_address and not _redeem_routes._is_pm_safe_deployed(safe_address):
                _redeem_routes._deploy_pm_safe_for_user(
                    embedded_wallet_id,
                    embedded_wallet_addr,
                )
                safe_deployed = True
                logger.info(
                    "[T795] Safe deployed successfully wallet=%s safe=%s",
                    primary_wallet,
                    safe_address,
                )
            elif safe_address:
                safe_deployed = True  # already deployed
        except Exception as _deploy_exc:
            logger.warning(
                "[T795] Safe deploy attempt failed wallet=%s err=%s",
                primary_wallet,
                _deploy_exc,
            )

    resp = JSONResponse(
        {
            "status": "ok",
            "wallet_address": primary_wallet,
            "role": actual_role,
            "privy_user_id": privy_user_id,
            "privy_embedded_wallet_address": embedded_wallet_addr or primary_wallet,
            "privy_embedded_wallet_id": embedded_wallet_id,
            "privy_signer_status": signer_status,
            "privy_signer_id": (
                signer_cfg.get("signer_id") if signer_cfg.get("enabled") else ""
            ),
            "privy_signer_policy_ids": signer_policy_ids,
            "linked_wallets": wallets,
            "pm_binding_required": pm_binding_required,
            "auto_provisioned": auto_provisioned,
            "safe_address": safe_address,
            "safe_deployed": safe_deployed,
        }
    )
    resp.set_cookie(
        "user_session",
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=USER_SESSION_DAYS * 86400,
        path="/",
    )
    if id_token:
        resp.set_cookie(
            "privy-id-token",
            id_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=12 * 3600,
            path="/",
        )
    if access_token:
        resp.set_cookie(
            "privy-token",
            access_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=12 * 3600,
            path="/",
        )
    # Clear stale admin cookies so manual-user pages don't get trapped in admin+TOTP flow.
    resp.delete_cookie("dashboard_key", path="/")
    resp.delete_cookie("totp_session", path="/")
    if totp_enabled and mfa_verified_in_session:
        resp.set_cookie(
            key=_MFA_VERIFY_COOKIE,
            value=mfa_cookie_sign(primary_wallet),
            max_age=_MFA_VERIFY_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
    else:
        resp.delete_cookie(_MFA_VERIFY_COOKIE, path="/")
    # 登录成功后清理该 IP 的临时限流记录，避免历史误封导致后续页面 403。
    try:
        source_ip = request.client.host if request.client else ""
        if source_ip:
            ip_rate_limiter.clear_ip(source_ip)
    except Exception:
        pass
    return resp


@router.get("/api/user/me")
async def user_me(request: Request):
    # Ensure users table has latest columns before querying profile/signer fields.
    _ensure_user_tables()

    identity = get_request_identity(request)
    if not identity:
        # T498 Wave 5A: 即便未授权也返回 SWR 头，避免每次跳转都打 origin
        return JSONResponse(
            {"authenticated": False, "role": None},
            headers={
                "Cache-Control": "private, max-age=10, stale-while-revalidate=30",
                "Vary": "Cookie",
            },
        )

    # T487: 管理员（admin / super_admin）通过 Privy 登录时也持有真实 wallet_address，
    # 必须与 manual_user 走同一份资料/PM 绑定/runtime 链路；只有历史的 dashboard_key
    # 兜底身份会显示为虚拟 "__admin__"，那时才回旧的精简响应。
    raw_wallet = str(identity.get("wallet_address") or "").strip()
    if identity.get("is_admin") and raw_wallet.lower() == "__admin__":
        return JSONResponse(
            {
                "authenticated": True,
                "role": identity.get("role", "admin"),
                "wallet_address": "__admin__",
                "display_name": "管理员",
                "avatar_url": "",
                "email": "",
                "email_verified": True,
                "polymarket_wallet": None,
                "pm_wallet_address": None,
                "linked_wallets": [],
                "is_admin": True,
            },
            headers={
                "Cache-Control": "private, max-age=15, stale-while-revalidate=60",
                "Vary": "Cookie",
            },
        )

    wallet_address = raw_wallet

    # T611 — sync DB ops moved off event loop; user_me 由 header 高频轮询，
    # 直接同步执行会阻塞 worker 线程，连带拖累并发的 admin / DBA 请求。
    def _fetch_user_row():
        with db_connect() as conn:
            try:
                return conn.execute(
                    "SELECT privy_user_id, privy_embedded_wallet_id, privy_linked_accounts_json, privy_signer_status, privy_signer_policies_json, privy_signer_error, privy_signer_updated_at, polymarket_wallet, main_wallet, funder_address, signature_type, safe_deployed_at, linked_wallets_json, display_name, avatar_url, email, email_verified, mfa_enabled, mfa_methods, mfa_last_verified_at, created_at FROM users WHERE wallet_address=?",
                    (wallet_address,),
                ).fetchone()
            except Exception as exc:
                logger.warning(
                    "users signer columns unavailable in /api/user/me; fallback query: %s",
                    exc,
                )
                return conn.execute(
                    "SELECT privy_user_id, polymarket_wallet, main_wallet, funder_address, signature_type, safe_deployed_at, linked_wallets_json, display_name, avatar_url, email, email_verified, mfa_enabled, mfa_methods, mfa_last_verified_at, created_at FROM users WHERE wallet_address=?",
                    (wallet_address,),
                ).fetchone()

    row = await asyncio.to_thread(_fetch_user_row)

    linked_wallets = []
    if row and row["linked_wallets_json"]:
        try:
            linked_wallets = json.loads(row["linked_wallets_json"])
        except Exception:
            linked_wallets = []

    privy_linked_accounts = []
    if row and _safe_row_get(row, "privy_linked_accounts_json"):
        try:
            privy_linked_accounts = json.loads(
                _safe_row_get(row, "privy_linked_accounts_json")
            )
        except Exception:
            privy_linked_accounts = []

    privy_signer_policy_ids = []
    if row and _safe_row_get(row, "privy_signer_policies_json"):
        try:
            privy_signer_policy_ids = json.loads(
                _safe_row_get(row, "privy_signer_policies_json")
            )
        except Exception:
            privy_signer_policy_ids = []

    runtime = _get_or_create_runtime(wallet_address)
    _ensure_pm_multi_tenant_tables()

    def _fetch_pm_prof():
        with db_connect() as conn:
            return conn.execute(
                "SELECT polymarket_wallet, proxy_wallet, discovery_method, discovery_confidence, last_synced_at FROM user_pm_profiles WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()

    pm_prof = await asyncio.to_thread(_fetch_pm_prof)

    # T287 Wave 3.6 (F): Privy MFA \u72b6\u6001
    mfa_enabled = bool(_safe_row_get(row, "mfa_enabled"))
    mfa_methods_raw = _safe_row_get(row, "mfa_methods")
    if isinstance(mfa_methods_raw, str):
        try:
            mfa_methods = json.loads(mfa_methods_raw)
        except Exception:
            mfa_methods = []
    elif isinstance(mfa_methods_raw, list):
        mfa_methods = mfa_methods_raw
    else:
        mfa_methods = []
    mfa_verified_at = _safe_iso(_safe_row_get(row, "mfa_last_verified_at"))
    # session \u5185 MFA \u9a8c\u8bc1\u72b6\u6001\uff1a\u8bfb cookie \u4e2d\u7684\u6807\u8bb0
    mfa_session_token = request.cookies.get("mfa_verified", "")
    mfa_verified_in_session = (
        bool(mfa_session_token) and mfa_session_token == wallet_address
    )

    pm_wallet_for_ui = (_safe_row_get(row, "polymarket_wallet") or "") or (
        _safe_row_get(row, "funder_address") or ""
    )

    return JSONResponse(
        {
            "authenticated": True,
            "role": identity.get("role", "manual_user"),
            "wallet_address": wallet_address,
            "privy_user_id": (row["privy_user_id"] if row else None),
            "privy_embedded_wallet_id": (
                _safe_row_get(row, "privy_embedded_wallet_id")
            ),
            "privy_signer_status": (_safe_row_get(row, "privy_signer_status")),
            "privy_signer_policy_ids": privy_signer_policy_ids,
            "privy_signer_error": (_safe_row_get(row, "privy_signer_error")),
            "privy_signer_updated_at": _safe_iso(
                _safe_row_get(row, "privy_signer_updated_at")
            ),
            "display_name": (row["display_name"] if row else None),
            "avatar_url": (row["avatar_url"] if row else None),
            "email": (row["email"] if row else None),
            "created_at": _safe_iso(_safe_row_get(row, "created_at")),
            "email_verified": bool(row["email_verified"]) if row else False,
            "polymarket_wallet": (pm_wallet_for_ui or None),
            "pm_wallet_address": (
                pm_wallet_for_ui or None
            ),  # alias for frontend compat
            # T287 Wave 2: 双地址绑定字段（main_wallet=Privy EOA 签名钱包；funder_address=Gnosis Safe 资金钱包）
            "main_wallet": (_safe_row_get(row, "main_wallet")),
            "funder_address": (_safe_row_get(row, "funder_address")),
            "signature_type": (_safe_row_get(row, "signature_type") or 2),
            # Deposit Wallet Migration (May 2026): wallet_type distinguishes 'safe' vs 'deposit_wallet'
            "wallet_type": (_safe_row_get(row, "wallet_type") or "safe"),
            "safe_deployed_at": _safe_iso(_safe_row_get(row, "safe_deployed_at")),
            "linked_wallets": linked_wallets,
            "privy_linked_accounts": privy_linked_accounts,
            "is_admin": identity.get("is_admin", False),
            "capabilities": {
                "automation_pipeline": False,
            },
            "pm_profile": dict(pm_prof) if pm_prof else None,
            "runtime": runtime,
            # T287 Wave 3.6 (F): Privy MFA
            "mfa_enabled": mfa_enabled,
            "mfa_methods": mfa_methods,
            "mfa_last_verified_at": mfa_verified_at,
            "mfa_verified_in_session": mfa_verified_in_session,
        },
        headers={
            # T498 Wave 5A: 浏览器侧 SWR — 在 15s 内任何页面跳转都直接命中本地缓存，
            # 60s SWR 窗口内可立刻渲染顶部导航 + 后台静默刷新；按 Cookie 隔离用户。
            "Cache-Control": "private, max-age=15, stale-while-revalidate=60",
            "Vary": "Cookie",
        },
    )


def _mask_wallet(addr: str) -> str:
    s = str(addr or "").strip()
    if len(s) < 10:
        return s
    return f"{s[:6]}...{s[-4:]}"


@router.get("/api/user/topbar-fragment", response_class=HTMLResponse)
async def user_topbar_fragment(request: Request):
    identity = get_request_identity(request)
    if not identity:
        return Response(status_code=401)
    wallet_address = str(identity.get("wallet_address") or "").strip().lower()
    if not wallet_address or wallet_address == "__admin__":
        return Response(status_code=401)

    display_name = _fetch_current_display_name(wallet_address)
    if not display_name:
        display_name = _mask_wallet(wallet_address)

    fragment = (
        '<span id="header-user" class="user-name" hx-swap-oob="outerHTML">'
        + html_escape(display_name)
        + "</span>"
    )
    return HTMLResponse(fragment)


@router.get("/api/user/onboard-modal", response_class=HTMLResponse)
async def user_onboard_modal(request: Request):
    """T875 — Lazy-mount the 6-step onboarding modal.

    Returns 204 unless the session is flagged needs_onboarding=True for a
    real manual user. The new modal is a single all-steps payload; client
    JS handles step navigation and the Skip-to-step-6 jump.
    """
    identity = get_request_identity(request)
    if not identity:
        return Response(status_code=401)

    wallet_address = str(identity.get("wallet_address") or "").strip().lower()
    role = str(identity.get("role") or "").strip().lower()
    if role != "manual_user" or not wallet_address or wallet_address == "__admin__":
        return Response(status_code=204)

    display_name = _fetch_current_display_name(wallet_address)
    needs = _needs_onboarding(display_name, wallet_address)
    try:
        request.session["needs_onboarding"] = needs
        request.session["onboarding_wallet"] = wallet_address
    except Exception:
        pass
    if not needs:
        return Response(status_code=204)

    lang = _detect_lang(request)
    pack = _load_onboarding_pack(lang)
    try:
        from .frontend_routes import _asset_version  # type: ignore
        asset_v = _asset_version()
    except Exception:
        asset_v = "0"
    ctx = {
        "request": request,
        "lang": lang,
        "i18n": pack,
        "asset_version": asset_v,
        # JSON inlined into <script id="ob-i18n-data" type="application/json">
        # so the client controller can render validation messages without
        # a second round-trip. Tags inside string values are JSON-escaped
        # by json.dumps already; we additionally swap </ to <\/ so a stray
        # </script> in any future translation cannot break out of the tag.
        "i18n_json": json.dumps(pack, ensure_ascii=False).replace("</", "<\\/"),
        "value": display_name if display_name and display_name.lower() != wallet_address else "",
    }
    return templates.TemplateResponse(request, "fragments/onboarding.html", ctx)


@router.post("/api/user/onboard-submit")
async def user_onboard_submit(request: Request):
    """T875 — Single-shot finalize for the 6-step modal.

    Accepts display_name + terms_accepted in one POST. Returns JSON so
    the client can map error_key → localized message via the inlined
    i18n pack. On success returns {"ok": true} and the client closes
    the modal + soft-reloads.
    """
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"ok": False, "error_key": "validation.network"}, status_code=401)

    wallet_address = str(identity.get("wallet_address") or "").strip().lower()
    role = str(identity.get("role") or "").strip().lower()
    if role != "manual_user" or not wallet_address or wallet_address == "__admin__":
        return JSONResponse({"ok": False, "error_key": "validation.network"}, status_code=403)

    form = await request.form()

    # Terms checkbox is mandatory.
    accepted = str(form.get("terms_accepted") or "").strip().lower() in {
        "1", "true", "on", "yes",
    }
    if not accepted:
        return JSONResponse(
            {"ok": False, "error_key": "validation.terms",
             "error": "Please accept the terms to continue."},
            status_code=400,
        )

    # Display name validation pipeline.
    display_name_raw = str(form.get("display_name") or "")
    display_name, err = _validate_display_name(display_name_raw, allow_empty=False)
    error_key = None
    if err:
        # Map server-side error string to a stable i18n key.
        low = err.lower()
        if "required" in low:
            error_key = "validation.required"
        elif "characters" in low and ("2" in low or "40" in low):
            # length check (covers both too-short and too-long)
            stripped = display_name_raw.strip()
            error_key = "validation.tooShort" if len(stripped) < 2 else "validation.tooLong"
        elif "forbidden" in low or "only contain" in low:
            error_key = "validation.invalidChars"
        else:
            error_key = "validation.invalidChars"
    elif _is_wallet_style_name(display_name):
        error_key = "validation.walletStyle"
    elif is_username_reserved(display_name):
        error_key = "validation.reserved"
    elif _is_display_name_taken(display_name, wallet_address):
        error_key = "validation.taken"

    if error_key:
        return JSONResponse(
            {"ok": False, "error_key": error_key, "error": err or error_key},
            status_code=400,
        )

    now = datetime.now(timezone.utc).isoformat()

    def _update_name():
        with db_connect() as conn:
            conn.execute(
                "UPDATE users SET display_name=?, updated_at=? WHERE wallet_address=?",
                (display_name, now, wallet_address),
            )
            conn.commit()

    await asyncio.to_thread(_update_name)

    try:
        request.session["needs_onboarding"] = False
        request.session.pop("onboarding_wallet", None)
    except Exception:
        pass

    return JSONResponse({"ok": True, "display_name": display_name})


@router.post("/api/user/logout")
async def user_logout(request: Request):
    try:
        request.session.clear()
    except Exception:
        pass

    identity = get_request_identity(request)
    wallet_address = str((identity or {}).get("wallet_address") or "").strip().lower()
    role = str((identity or {}).get("role") or "").strip().lower()

    token = request.cookies.get("user_session", "")
    if token:
        revoke_user_session(token)

    # 安全优先：手动用户登出时，同步撤销该钱包的全部 user_session，避免旧会话残留继续访问。
    if role == "manual_user" and wallet_address.startswith("0x"):
        try:
            with db_connect() as conn:
                conn.execute(
                    "DELETE FROM user_sessions WHERE wallet_address=?",
                    (wallet_address,),
                )
                conn.commit()
        except Exception as e:
            logger.warning(
                "[UserLogout] revoke all sessions failed for %s: %s", wallet_address, e
            )

    resp = JSONResponse({"status": "ok", "message": "logged out"})
    resp.delete_cookie("user_session", path="/")
    resp.delete_cookie("dashboard_key", path="/")
    resp.delete_cookie("totp_session", path="/")
    resp.delete_cookie("mfa_verified", path="/")
    return resp


# =============================================================================
# T287 Wave 3.6 (F): Privy Policy-Based MFA
# =============================================================================
# 设计：
# - V2 用户前端为 HTMX + 普通 HTML，无 Privy React SDK；Privy MFA 必须在 Index 页（React）触发
# - 前端 user.html 点击「开启/关闭 MFA」 → 弹 popup 至 /Index?mfa_action=enroll&method=xxx
# - Index 页 React 组件接收 query → 调 useMfaEnrollment 弹出 Privy 默认 modal
# - Privy modal 完成后 → Index 页拉最新 user.mfaMethods → POST /api/user/mfa/sync
# - Index 页 window.opener.postMessage('privy_mfa_done', origin) → user.html 关闭 popup + 刷新
# - 「验证 MFA」（敏感操作前）→ POST /api/user/mfa/verify-mark 写 30min cookie
_MFA_AVAILABLE_METHODS = ["sms", "totp", "passkey"]
_MFA_VERIFY_COOKIE = "mfa_verified"
_MFA_VERIFY_TTL_SECONDS = 30 * 60


def _normalize_mfa_methods(raw) -> list[str]:
    """规范化 mfa_methods 列表：仅保留白名单方法、去重。"""
    if not isinstance(raw, list):
        return []
    out = []
    for m in raw:
        s = str(m or "").strip().lower()
        if s in _MFA_AVAILABLE_METHODS and s not in out:
            out.append(s)
    return out


@router.get("/api/user/mfa/status")
async def mfa_status(request: Request):
    """读取当前用户的 MFA 状态。"""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)
    wallet_address = str(identity.get("wallet_address") or "")

    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT mfa_enabled, mfa_methods, mfa_last_verified_at, totp_enabled, totp_secret, totp_verified_at FROM users WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()
        if not row:
            return {
                "enabled": False,
                "methods": [],
                "last_verified_at": None,
                "available": _MFA_AVAILABLE_METHODS,
                "verified_in_session": False,
                "totp_enrolled": False,
                "totp_enabled": False,
            }
        methods_raw = _safe_row_get(row, "mfa_methods")
        if isinstance(methods_raw, str):
            try:
                methods = json.loads(methods_raw)
            except Exception:
                methods = []
        elif isinstance(methods_raw, list):
            methods = methods_raw
        else:
            methods = []
        methods = _normalize_mfa_methods(methods)
        totp_enrolled = bool(_safe_row_get(row, "totp_enabled"))
        mfa_verified_in_session = mfa_cookie_verify(
            request.cookies.get(_MFA_VERIFY_COOKIE, ""), wallet_address
        )
        return {
            "enabled": bool(_safe_row_get(row, "mfa_enabled")),
            "methods": methods,
            "last_verified_at": _safe_iso(_safe_row_get(row, "mfa_last_verified_at")),
            "available": _MFA_AVAILABLE_METHODS,
            "verified_in_session": mfa_verified_in_session,
            "totp_enrolled": totp_enrolled,
            "totp_enabled": totp_enabled,
            "totp_verified_at": _safe_iso(_safe_row_get(row, "totp_verified_at")),
        }

    return await asyncio.to_thread(_db)


@router.post("/api/user/mfa/sync")
async def mfa_sync(request: Request):
    """T902 起仅作为兼容回调存在，不再据此翻转 mfa_enabled。

    历史原因：该端点原先由 Index 页在 Privy enroll/unenroll 后回调，把客户端
    上报的 methods 列表直接写入 mfa_enabled —— 这是客户端可伪造的状态。前台登录
    MFA 已改为服务端 TOTP（/api/user/mfa/enroll/*），此端点只保留 methods 缓存展示。
    """
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)
    wallet_address = str(identity.get("wallet_address") or "")

    try:
        body = await request.json()
    except Exception:
        body = {}
    methods = _normalize_mfa_methods(body.get("methods") or [])
    now_iso = datetime.now(timezone.utc).isoformat()

    def _db():
        with db_connect() as conn:
            conn.execute(
                "UPDATE users SET mfa_methods=?, mfa_updated_at=?, updated_at=? WHERE wallet_address=?",
                (json.dumps(methods), now_iso, now_iso, wallet_address),
            )
        logger.info("[MFA] sync(write-only-methods) wallet=%s methods=%s", wallet_address, methods)
        return {"status": "ok", "enabled": False, "methods": methods, "totp_managed": True}

    return await asyncio.to_thread(_db)


@router.post("/api/user/mfa/enroll/start")
async def mfa_enroll_start(request: Request):
    """T902：开始 TOTP 注册。返回 otpauth URI、内联 SVG 二维码与 secret。

    secret 仅在此响应中返回一次，客户端需保留以便在 enroll/confirm 回传校验。
    """
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)
    wallet_address = str(identity.get("wallet_address") or "")

    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT totp_enabled, totp_secret FROM users WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()
        if row and bool(_safe_row_get(row, "totp_enabled")):
            return JSONResponse({"error": "totp_already_enabled"}, status_code=409)
        secret = generate_totp_secret()
        uri = totp_provisioning_uri(secret, wallet_address)
        return {
            "status": "ok",
            "secret": secret,
            "otpauth_uri": uri,
            "qr_svg": totp_qr_svg(uri),
        }

    return await asyncio.to_thread(_db)


@router.post("/api/user/mfa/enroll/confirm")
async def mfa_enroll_confirm(request: Request):
    """T902：校验 TOTP 码并完成注册。Body: {"secret": ..., "code": "123456"}"""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)
    wallet_address = str(identity.get("wallet_address") or "")

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    secret = str(body.get("secret") or "").strip()
    code = str(body.get("code") or "").strip()

    if not verify_totp(secret, code):
        return JSONResponse({"error": "invalid_totp"}, status_code=401)

    now_iso = datetime.now(timezone.utc).isoformat()

    def _db():
        with db_connect() as conn:
            conn.execute(
                "UPDATE users SET totp_secret=?, totp_enabled=TRUE, mfa_enabled=TRUE, totp_verified_at=?, mfa_last_verified_at=?, mfa_updated_at=?, updated_at=? WHERE wallet_address=?",
                (secret, now_iso, now_iso, now_iso, now_iso, wallet_address),
            )
        resp = JSONResponse({"status": "ok", "enrolled": True})
        resp.set_cookie(
            key=_MFA_VERIFY_COOKIE,
            value=mfa_cookie_sign(wallet_address),
            max_age=_MFA_VERIFY_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        logger.info("[MFA] enroll-confirm wallet=%s", wallet_address)
        return resp

    return await asyncio.to_thread(_db)


@router.post("/api/user/mfa/disable")
async def mfa_disable(request: Request):
    """T902：校验当前 TOTP 码后关闭 MFA。Body: {"code": "123456"}"""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)
    wallet_address = str(identity.get("wallet_address") or "")

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    code = str(body.get("code") or "").strip()

    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT totp_secret, totp_enabled FROM users WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()
        if not row or not bool(_safe_row_get(row, "totp_enabled")):
            return JSONResponse({"error": "not_enrolled"}, status_code=400)
        if not verify_totp(str(_safe_row_get(row, "totp_secret") or ""), code):
            return JSONResponse({"error": "invalid_totp"}, status_code=401)
        with db_connect() as conn:
            conn.execute(
                "UPDATE users SET totp_secret=NULL, totp_enabled=FALSE, mfa_enabled=FALSE, updated_at=? WHERE wallet_address=?",
                (datetime.now(timezone.utc).isoformat(), wallet_address),
            )
        resp = JSONResponse({"status": "ok", "disabled": True})
        resp.delete_cookie(_MFA_VERIFY_COOKIE, path="/")
        logger.info("[MFA] disable wallet=%s", wallet_address)
        return resp

    return await asyncio.to_thread(_db)


@router.post("/api/user/mfa/verify-mark")
async def mfa_verify_mark(request: Request):
    """T902：校验服务端 TOTP 码（Body: {"code": "123456"}），通过后签发 30min 签名 cookie。

    原先该端点接受无凭据的 POST 即写 cookie，等于把 MFA 门禁交给了客户端。
    """
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)
    wallet_address = str(identity.get("wallet_address") or "")

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    code = str(body.get("code") or "").strip()
    now_iso = datetime.now(timezone.utc).isoformat()

    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT totp_secret, totp_enabled FROM users WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()
        if not row or not bool(_safe_row_get(row, "totp_enabled")):
            return JSONResponse({"error": "totp_not_enrolled"}, status_code=400)
        if not verify_totp(str(_safe_row_get(row, "totp_secret") or ""), code):
            return JSONResponse({"error": "invalid_totp"}, status_code=401)

        with db_connect() as conn:
            conn.execute(
                "UPDATE users SET mfa_last_verified_at=?, totp_verified_at=?, updated_at=? WHERE wallet_address=?",
                (now_iso, now_iso, now_iso, wallet_address),
            )

        resp = JSONResponse({"status": "ok", "verified_at": now_iso})
        resp.set_cookie(
            key=_MFA_VERIFY_COOKIE,
            value=mfa_cookie_sign(wallet_address),
            max_age=_MFA_VERIFY_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )

        logger.info("[MFA] verify-mark wallet=%s ttl=%ss", wallet_address, _MFA_VERIFY_TTL_SECONDS)
        return resp

    return await asyncio.to_thread(_db)


def require_mfa(request: Request):
    """FastAPI dependency：敏感操作前要求 MFA 已验证（30min 内）。

    用法（Wave 4 装载到下单端点）：
        @router.post("/api/orders/place", dependencies=[Depends(require_mfa)])

    本轮仅定义、暂不强制启用，保持向后兼容。
    """
    identity = get_request_identity(request)
    if not identity:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="login required")
    if identity.get("is_admin"):
        return identity  # admin 暂不强制 MFA
    wallet_address = str(identity.get("wallet_address") or "")
    with db_connect() as conn:
        row = conn.execute(
            "SELECT mfa_enabled FROM users WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()
    if not row or not _safe_row_get(row, "mfa_enabled"):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403,
            detail={
                "error": "mfa_required",
                "message": "敏感操作要求先开启 MFA。请前往 /v2/user 启用多因素验证。",
                "action": "enroll",
            },
        )
    mfa_session_token = request.cookies.get(_MFA_VERIFY_COOKIE, "")
    if not mfa_cookie_verify(mfa_session_token, wallet_address):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403,
            detail={
                "error": "mfa_verify_required",
                "message": "敏感操作要求 MFA 二次验证（30 分钟内有效）。",
                "action": "verify",
            },
        )
    return identity


@router.get("/api/user/pm-wallet/status")
async def pm_wallet_status(request: Request):
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    wallet_address = str(identity.get("wallet_address") or "")

    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT linked_wallets_json, email FROM users WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()
        linked_wallets = []
        if row and row["linked_wallets_json"]:
            try:
                linked_wallets = json.loads(row["linked_wallets_json"])
            except Exception:
                linked_wallets = []

        login_email = str((dict(row) if row else {}).get("email") or "").strip()
        state = _sync_user_pm_data(
            wallet_address,
            linked_wallets,
            force=False,
            login_email=login_email,
        )
        profile = state.get("profile") or {}
        equity = state.get("equity") or {}
        positions = state.get("positions") or []

        pm_wallet = str(profile.get("polymarket_wallet") or "").strip().lower()
        if not _ADDR_RE.match(pm_wallet):
            return {
                "status": "ok",
                "bound": False,
                "wallet_address": wallet_address,
                "polymarket_wallet": None,
                "discovery_method": profile.get("discovery_method") or "none",
            }

        positions_value = _safe_float(equity.get("portfolio_value"), 0.0)
        balance_value = _safe_float(equity.get("usdc_balance"), 0.0)
        raw_json = _safe_json_loads(str(equity.get("raw_json") or ""))
        if balance_value <= 0.000001:
            payload_balance = _extract_balance_from_value_payload(
                raw_json.get("value") or {}
            )
            balance_value = _safe_float(payload_balance, 0.0)
        if balance_value <= 0.000001:
            balance_value = _estimate_balance_from_config(positions_value)

        portfolio_value = {
            "user": pm_wallet,
            "value": positions_value,
            "usdc_balance": balance_value,
            "balance": balance_value,
        }
        norm_positions = [
            {
                "proxyWallet": p.get("polymarket_wallet") or pm_wallet,
                "conditionId": p.get("condition_id"),
                "asset": p.get("asset"),
                "title": p.get("title"),
                "outcome": p.get("outcome"),
                "size": _safe_float(p.get("size"), 0.0),
                "avgPrice": _safe_float(p.get("avg_price"), 0.0),
                "initialValue": _safe_float(p.get("initial_value"), 0.0),
                "currentValue": _safe_float(p.get("current_value"), 0.0),
                "cashPnl": _safe_float(p.get("cash_pnl"), 0.0),
                "percentPnl": _safe_float(p.get("percent_pnl"), 0.0),
                "curPrice": _safe_float(p.get("cur_price"), 0.0),
                "eventId": p.get("event_id"),
                "slug": p.get("slug"),
                "eventSlug": p.get("event_slug"),
                "endDate": p.get("end_date"),
            }
            for p in positions
        ]

        return {
            "status": "ok",
            "bound": True,
            "wallet_address": wallet_address,
            "polymarket_wallet": pm_wallet,
            "proxy_wallet": profile.get("proxy_wallet")
            or "",  # T499++ Wave 6B: never EOA fallback
            "discovery_method": profile.get("discovery_method") or "unknown",
            "discovery_confidence": _safe_float(
                profile.get("discovery_confidence"), 0.0
            ),
            "portfolio_value": portfolio_value,
            "open_positions_count": _safe_int(
                equity.get("open_positions_count"), len(norm_positions)
            ),
            "open_positions": norm_positions[:20],
            "value_error": profile.get("last_error") or "",
            "last_synced_at": profile.get("last_synced_at"),
        }

    return await asyncio.to_thread(_db)


_BIND_DEPRECATED_PAYLOAD = {
    "error": "manual_pm_wallet_binding_disabled",
    "code": "T287_WAVE1",
    "message": (
        "为修复跨用户数据风险，手动输入 PM 钱包绑定已永久关闭。"
        "请通过 Privy 授权登录，由系统自动派生与部署 Gnosis Safe (signatureType=2) 作为您的 Polymarket 钱包。"
    ),
    "next_action": "privy_authorize",
    "docs": "Polymarket 安全钱包绑定与 CLOB V2 交易集成方案 (T287)",
}


def _log_blocked_binding_attempt(
    request: Request,
    *,
    wallet_address: str,
    endpoint: str,
    submitted_payload: dict[str, object] | None,
) -> None:
    """记录被拦截的手动绑定尝试，便于审计。"""
    try:
        peer_ip = request.client.host if request.client else "-"
    except Exception:
        peer_ip = "-"
    submitted_addr = ""
    if submitted_payload:
        submitted_addr = str(
            submitted_payload.get("polymarket_wallet")
            or submitted_payload.get("pm_wallet_address")
            or ""
        )
    logger.warning(
        "[T287] 拒绝手动 PM 钱包绑定 endpoint=%s user=%s submitted=%s peer_ip=%s",
        endpoint,
        wallet_address[:14] or "-",
        submitted_addr[:14] or "-",
        peer_ip,
    )


@router.post("/api/user/pm-wallet/bind")
async def pm_wallet_bind(request: Request):
    """T287 Wave1: 已下线。任何手动绑定 PM 钱包请求一律拒绝并记录审计日志。"""
    identity = get_request_identity(request)
    wallet_address = str((identity or {}).get("wallet_address") or "")
    body: dict[str, object] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    _log_blocked_binding_attempt(
        request,
        wallet_address=wallet_address,
        endpoint="/api/user/pm-wallet/bind",
        submitted_payload=body,
    )
    return JSONResponse(_BIND_DEPRECATED_PAYLOAD, status_code=410)


@router.post("/api/pm/bind-wallet")
async def pm_bind_wallet_v2(request: Request):
    """T287 Wave 2 — 系统化 PM 钱包绑定（Privy 主钱包 + 链上 Safe 资金钱包）。

    流程：
      1. 校验当前 user_session（manual_user）；
      2. 取出 Privy 已链接钱包列表；
      3. 触发 _sync_user_pm_data(force=True) 通过 Polymarket Data API 发现已部署
         的 Gnosis Safe (proxy_wallet) → 同步写入 users.main_wallet / funder_address
         以及 user_pm_profiles 表；
      4. 返回当前绑定状态（main_wallet, funder_address, signature_type）。

    本端点不接受任何手动地址输入；所有地址均由系统从 Privy + 链上数据派生。
    Builder Relayer gasless Safe 部署 / CLOB 凭证派生 在 Wave 2.5 引入；
    当前阶段 funder_address 来自 Polymarket Data API 已发现的代理钱包，
    若 Privy 主钱包尚未在 Polymarket 链上有任何活动，会返回 needs_funding=true。
    """
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    wallet_address = str(identity.get("wallet_address") or "").strip().lower()
    if not _ADDR_RE.match(wallet_address):
        return JSONResponse({"error": "invalid session wallet"}, status_code=400)

    # 安全：本端点不读取 body 中任何地址参数，仅读取可选的 force 标志
    force = False
    try:
        body = await request.json()
        force = bool(body.get("force"))
    except Exception:
        pass

    with db_connect() as conn:
        row = conn.execute(
            "SELECT linked_wallets_json, email FROM users WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()
    linked_wallets = []
    if row and _safe_row_get(row, "linked_wallets_json"):
        try:
            linked_wallets = json.loads(_safe_row_get(row, "linked_wallets_json"))
        except Exception:
            linked_wallets = []
    login_email = str(_safe_row_get(row, "email") or "").strip()

    try:
        state = _sync_user_pm_data(
            wallet_address,
            linked_wallets,
            force=True if force else True,
            login_email=login_email,
        )
    except Exception as exc:
        logger.exception(
            "pm_bind_wallet_v2 sync failed for %s: %s", wallet_address, exc
        )
        return JSONResponse({"error": f"discovery failed: {exc}"}, status_code=502)

    profile = state.get("profile") or {}
    pm_wallet = str(profile.get("polymarket_wallet") or "").strip().lower()
    needs_binding = bool(state.get("needs_pm_binding", False))

    # 重新读取 users 行，返回最新双地址
    with db_connect() as conn:
        u_row = conn.execute(
            "SELECT main_wallet, funder_address, signature_type, polymarket_wallet, safe_deployed_at FROM users WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()

    main_wallet = _safe_row_get(u_row, "main_wallet") or wallet_address
    funder_address = _safe_row_get(u_row, "funder_address") or pm_wallet or None
    sig_type = _safe_row_get(u_row, "signature_type") or 2

    # 安全审计：绑定事件记录
    try:
        logger.info(
            "T287_WAVE2_BIND wallet=%s main=%s funder=%s sig_type=%s needs_binding=%s",
            wallet_address,
            main_wallet,
            funder_address,
            sig_type,
            needs_binding,
        )
    except Exception:
        pass

    return {
        "status": "ok" if (funder_address and not needs_binding) else "needs_funding",
        "wallet_address": wallet_address,
        "main_wallet": main_wallet,
        "funder_address": funder_address,
        "signature_type": sig_type,
        "polymarket_wallet": pm_wallet,
        "needs_pm_binding": needs_binding,
        "safe_deployed_at": _safe_iso(_safe_row_get(u_row, "safe_deployed_at")),
        "discovery_method": (state.get("discovered") or {}).get("method")
        or profile.get("discovery_method")
        or "data_api",
        "hint": (
            "尚未发现已部署的 Gnosis Safe。请向你的 Privy 主钱包推导出的 Safe 地址充入原生 USDC（Polygon），"
            "Polymarket 合约会在首次资金到账时自动部署 Safe。"
            if needs_binding
            else "已成功绑定。可继续使用 Dashboard、交易等服务。"
        ),
    }


@router.get("/api/user/runtime-control")
async def runtime_control_status(request: Request):
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if identity.get("is_admin"):
        return {
            "status": "ok",
            "role": "admin",
            "global_mode": True,
        }

    wallet_address = str(identity.get("wallet_address") or "")
    runtime = _get_or_create_runtime(wallet_address)
    return _runtime_status_payload(wallet_address, runtime)


@router.post("/api/user/runtime-control")
async def runtime_control_update(request: Request):
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    wallet_address = str(identity.get("wallet_address") or "")
    try:
        body = await request.json()
    except Exception:
        body = {}

    if body.get("freeze_auto") is not None:
        return JSONResponse(
            {"error": "freeze_auto is admin-managed only"},
            status_code=403,
        )

    if "auto_trading_enabled" in body:
        target_auto = bool(body.get("auto_trading_enabled"))
    elif "auto_bet" in body:
        target_auto = bool(body.get("auto_bet"))
    else:
        return JSONResponse(
            {"error": "missing auto_trading_enabled/auto_bet"},
            status_code=400,
        )

    _ensure_runtime_table()
    now = datetime.now(timezone.utc).isoformat()

    def _db():
        with db_connect() as conn:
            conn.execute(
                """
            INSERT INTO user_runtime_controls(wallet_address, freeze_auto, auto_trading_enabled, updated_at)
            VALUES(?, 0, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                auto_trading_enabled=excluded.auto_trading_enabled,
                updated_at=excluded.updated_at
            """,
                (wallet_address, 1 if target_auto else 0, now),
            )
            conn.commit()

        runtime = _get_or_create_runtime(wallet_address)
        out = _runtime_status_payload(wallet_address, runtime)
        out["updated"] = True
        return out

    return await asyncio.to_thread(_db)


@router.get("/api/user/pm/bets")
async def pm_bets(request: Request):
    """Manual-user Betslist unified data source with status filtering and paging."""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    wallet_address = str(identity.get("wallet_address") or "")

    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT linked_wallets_json FROM users WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()
        linked_wallets = []
        if row and row["linked_wallets_json"]:
            try:
                linked_wallets = json.loads(row["linked_wallets_json"])
            except Exception:
                linked_wallets = []

        qp = request.query_params
        refresh = str(qp.get("refresh") or "0") in {"1", "true", "True"}

        # Local-first: default returns latest local snapshot; optional refresh pulls upstream.
        state = _latest_pm_state(wallet_address)
        if refresh or not (state.get("profile") and state.get("equity")):
            _sync_user_pm_data(wallet_address, linked_wallets, force=False)

        page = max(1, _safe_int(qp.get("page"), 1))
        per_page = _safe_int(qp.get("per_page"), 25)
        per_page = min(200, max(1, per_page))
        status = str(qp.get("status") or "")
        search = str(qp.get("market_id") or "")
        category = str(qp.get("category") or "")
        tag = str(qp.get("tag") or "")

        pm_wallet, bets = _build_manual_pm_bets(
            wallet_address, status, search, category, tag
        )
        total = len(bets)
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        start = (page - 1) * per_page
        end = start + per_page

        return {
            "status": "ok",
            "bound": bool(pm_wallet),
            "polymarket_wallet": pm_wallet or None,
            "bets": bets[start:end],
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }

    return await asyncio.to_thread(_db)


@router.get("/api/user/pm/positions")
async def pm_positions(request: Request):
    """获取用户PM钱包在Polymarket的所有持仓（来自Data API）。"""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    wallet_address = str(identity.get("wallet_address") or "")

    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT linked_wallets_json FROM users WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()
        linked_wallets = []
        if row and row["linked_wallets_json"]:
            try:
                linked_wallets = json.loads(row["linked_wallets_json"])
            except Exception:
                linked_wallets = []

        state = _sync_user_pm_data(wallet_address, linked_wallets, force=False)
        profile = state.get("profile") or {}
        pm_wallet = str(profile.get("polymarket_wallet") or "").strip().lower()
        positions = state.get("positions") or []

        if not _ADDR_RE.match(pm_wallet):
            return {
                "status": "ok",
                "bound": False,
                "positions": [],
                "message": "PM wallet not bound",
            }

        return {
            "status": "ok",
            "bound": True,
            "polymarket_wallet": pm_wallet,
            "positions": [
                {
                    "proxyWallet": p.get("polymarket_wallet") or pm_wallet,
                    "conditionId": p.get("condition_id"),
                    "asset": p.get("asset"),
                    "title": p.get("title"),
                    "outcome": p.get("outcome"),
                    "size": _safe_float(p.get("size"), 0.0),
                    "avgPrice": _safe_float(p.get("avg_price"), 0.0),
                    "initialValue": _safe_float(p.get("initial_value"), 0.0),
                    "currentValue": _safe_float(p.get("current_value"), 0.0),
                    "cashPnl": _safe_float(p.get("cash_pnl"), 0.0),
                    "percentPnl": _safe_float(p.get("percent_pnl"), 0.0),
                    "curPrice": _safe_float(p.get("cur_price"), 0.0),
                    "eventId": p.get("event_id"),
                    "slug": p.get("slug"),
                    "eventSlug": p.get("event_slug"),
                    "endDate": p.get("end_date"),
                }
                for p in positions
            ],
            "count": len(positions),
        }

    return await asyncio.to_thread(_db)


@router.get("/api/user/pm/portfolio")
async def pm_portfolio(request: Request):
    """获取用户PM钱包的组合价值和余额（来自Data API）。"""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    wallet_address = str(identity.get("wallet_address") or "")

    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT linked_wallets_json FROM users WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()
        linked_wallets = []
        if row and row["linked_wallets_json"]:
            try:
                linked_wallets = json.loads(row["linked_wallets_json"])
            except Exception:
                linked_wallets = []

        state = _sync_user_pm_data(wallet_address, linked_wallets, force=False)
        profile = state.get("profile") or {}
        equity = state.get("equity") or {}
        pm_wallet = str(profile.get("polymarket_wallet") or "").strip().lower()

        if not _ADDR_RE.match(pm_wallet):
            return {
                "status": "ok",
                "bound": False,
                "value": None,
                "balance": None,
                "message": "PM wallet not bound",
            }

        positions_value = _safe_float(equity.get("portfolio_value"), 0.0)
        balance_value = _safe_float(equity.get("usdc_balance"), 0.0)
        raw_json = _safe_json_loads(str(equity.get("raw_json") or ""))
        if balance_value <= 0.000001:
            payload_balance = _extract_balance_from_value_payload(
                raw_json.get("value") or {}
            )
            balance_value = _safe_float(payload_balance, 0.0)
        if balance_value <= 0.000001:
            balance_value = _estimate_balance_from_config(positions_value)

        value_data = {
            "user": pm_wallet,
            "value": positions_value,
        }
        balance_data = {
            "user": pm_wallet,
            "usdc_balance": balance_value,
            "balance": balance_value,
        }

        return {
            "status": "ok",
            "bound": True,
            "polymarket_wallet": pm_wallet,
            "value": value_data,
            "balance": balance_data,
        }

    return await asyncio.to_thread(_db)


@router.get("/api/user/pm/metrics")
async def pm_metrics(request: Request):
    """Tenant-level PM performance metrics from persisted snapshots."""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    wallet_address = str(identity.get("wallet_address") or "")
    qp = request.query_params
    refresh = str(qp.get("refresh") or "0") in {"1", "true", "True"}

    # T911 wave 4 perf: 10s TTL cache. The metrics build chains through DB +
    # snapshot reads + an opportunistic _sync_user_pm_data refresh, totalling
    # well over 1s on cold cache. The UI polls this every few seconds, so a
    # short cache makes the dashboard feel instant without staling values.
    cache_key = wallet_address.lower()
    if cache_key and not refresh:
        ent = _PM_METRICS_CACHE.get(cache_key)
        if ent and (time.time() - ent[0]) < _PM_METRICS_TTL:
            return ent[1]

    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT linked_wallets_json FROM users WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()
        linked_wallets = []
        if row and row["linked_wallets_json"]:
            try:
                linked_wallets = json.loads(row["linked_wallets_json"])
            except Exception:
                linked_wallets = []

        qp_inner = request.query_params
        refresh_inner = str(qp_inner.get("refresh") or "0") in {"1", "true", "True"}
        state = _latest_pm_state(wallet_address)
        if refresh_inner or not (state.get("profile") and state.get("equity")):
            _sync_user_pm_data(wallet_address, linked_wallets, force=False)
            state = _latest_pm_state(wallet_address)
        equity = state.get("equity") or {}
        pm_wallet, bets = _build_manual_pm_bets(wallet_address, "", "", "")

        positions_value = _safe_float(equity.get("portfolio_value"), 0.0)
        raw_json = _safe_json_loads(str(equity.get("raw_json") or ""))
        usdc_balance = _safe_float(equity.get("usdc_balance"), 0.0)
        if usdc_balance <= 0.000001:
            from_payload = _extract_balance_from_value_payload(
                raw_json.get("value") or {}
            )
            usdc_balance = _safe_float(from_payload, 0.0)
        if usdc_balance <= 0.000001:
            usdc_balance = _estimate_balance_from_config(positions_value)

        nlv = positions_value + usdc_balance

        if nlv <= 0.000001:
            fallback_nlv = _safe_float(
                cfg_get("general.live_initial_deposit") or 0.0, 0.0
            )
            if fallback_nlv > 0:
                nlv = fallback_nlv
                usdc_balance = fallback_nlv
                positions_value = 0.0

        metrics = _calc_pm_metrics(
            wallet_address,
            bets=bets,
            positions_value=positions_value,
            nlv=nlv,
        )
        resolved_bets = [b for b in bets if b.get("resolved_outcome")]
        open_bets = [b for b in bets if not b.get("resolved_outcome")]
        realized_pnl = sum(_safe_float(b.get("pnl"), 0.0) for b in resolved_bets)
        unrealized_pnl = sum(
            _safe_float(b.get("unrealized_pnl"), 0.0) for b in open_bets
        )
        total_pnl = realized_pnl + unrealized_pnl
        total_staked = sum(_safe_float(b.get("size_usdc"), 0.0) for b in bets)
        roi = (total_pnl / total_staked) if total_staked > 1e-9 else 0.0

        return {
            "status": "ok",
            "wallet_address": wallet_address,
            "polymarket_wallet": pm_wallet,
            "nlv": nlv,
            "balance": usdc_balance,
            "positions_value": positions_value,
            "open_count": len(open_bets),
            "resolved_count": len(resolved_bets),
            "total": len(bets),
            "total_staked": total_staked,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,
            "roi": roi,
            **metrics,
            "last_snapshot_at": equity.get("snapshot_ts"),
        }

    out = await asyncio.to_thread(_db)
    if cache_key and isinstance(out, dict):
        _PM_METRICS_CACHE[cache_key] = (time.time(), out)
    return out


# ═══════════════════════════════════════════════════════════════
# 辅助：Blockies 风格几何头像生成器（无外部依赖，纯 Python）
# ═══════════════════════════════════════════════════════════════


def _generate_blockies_svg(address: str) -> str:
    """基于钱包地址生成 Blockies 风格的 5×5 对称几何 SVG 头像。"""
    import hashlib

    addr = address.lower().replace("0x", "")
    h = hashlib.sha256(addr.encode("utf-8")).digest()

    # 生成 5×5 格子（左右对称：3 列决定 5 列）
    grid: list[bool] = []
    for i in range(15):
        grid.append((h[i % len(h)] % 2) == 1)

    def _hsl(h_off: int) -> str:
        hue = round((h[h_off % len(h)] / 255.0) * 360)
        sat = round(40 + (h[(h_off + 1) % len(h)] / 255.0) * 40)
        lig = round(30 + (h[(h_off + 2) % len(h)] / 255.0) * 25)
        return f"hsl({hue},{sat}%,{lig}%)"

    fg = _hsl(0)
    bg = _hsl(3)
    size = 8
    canvas = 5 * size

    cells: list[str] = []
    for row in range(5):
        for col in range(5):
            idx = row * 3 + min(col, 4 - col)
            if grid[idx]:
                x, y = col * size, row * size
                cells.append(
                    f'<rect x="{x}" y="{y}" width="{size}" height="{size}" fill="{fg}"/>'
                )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas}" height="{canvas}" '
        f'viewBox="0 0 {canvas} {canvas}">'
        f'<rect width="{canvas}" height="{canvas}" fill="{bg}"/>'
        + "".join(cells)
        + "</svg>"
    )


# ═══════════════════════════════════════════════════════════════
# 用户个人资料管理端点
# ═══════════════════════════════════════════════════════════════


# T406: Reserved username check
RESERVED_USERNAMES = {
    "admin",
    "administrator",
    "root",
    "system",
    "support",
    "moderator",
    "mod",
    "owner",
    "staff",
    "user",
    "guest",
    "bot",
    "official",
    "service",
    "help",
    "superadmin",
    "sysadmin",
    "manager",
    "developer",
    "dev",
    "tester",
    "test",
    "demo",
    "backup",
    "internal",
    "team",
    "vip",
    "ceo",
    "founder",
    "partner",
    "affiliate",
    "api",
    "trading",
    "finance",
    "market",
    "exchange",
    "polymarket",
    "veyra",
    "veyratrade",
}

RESERVED_USERNAME_SUBSTRINGS = {
    # Simplified Chinese
    "管理员",
    "系统",
    "官方",
    "客服",
    "审核",
    "版主",
    "超管",
    "管理员账号",
    # Traditional Chinese
    "管理員",
    "系統",
    "官方帳號",
    # Japanese
    "管理者",
    "運営",
    "公式",
    "サポート",
    "モデレーター",
    # Korean
    "관리자",
    "운영자",
    "공식",
    "고객지원",
    "모더레이터",
}


def _normalize_username_for_reserved_check(name: str) -> str:
    s = unicodedata.normalize("NFKC", str(name or "")).lower().strip()
    # Remove common separators so variants like "admin_user" are still blocked.
    s = re.sub(r"[\s_\-\.]+", "", s)
    return s


def is_username_reserved(name):
    """Check if username contains reserved keywords (case-insensitive, fuzzy match)."""
    if not name:
        return False
    normalized = _normalize_username_for_reserved_check(name)
    if normalized in RESERVED_USERNAMES:
        return True
    for reserved in RESERVED_USERNAMES:
        if reserved in normalized:
            return True
    for reserved in RESERVED_USERNAME_SUBSTRINGS:
        if reserved in normalized:
            return True
    return False


@router.patch("/api/user/profile")
async def user_profile_update(request: Request):
    """更新用户昵称/头像（普通用户）。"""
    identity = get_request_identity(request)
    if not identity or identity.get("wallet_address") == "__admin__":
        return JSONResponse({"error": "manual user required"}, status_code=403)

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    wallet_address = str(identity["wallet_address"])
    display_name, display_name_err = _validate_display_name(
        str(body.get("display_name") or ""), allow_empty=False
    )

    if display_name_err:
        return JSONResponse(
            {
                "error": display_name_err,
                "error_key": "user.error.invalidName",
            },
            status_code=400,
        )

    # T406: Validate against reserved usernames
    if display_name and is_username_reserved(display_name):
        return JSONResponse(
            {
                "error": "该名称包含系统保留字，不允许使用。请换一个名称。",
                "error_key": "user.error.reservedName",
            },
            status_code=400,
        )

    avatar_url = str(body.get("avatar_url") or "").strip()[:4000]
    now = datetime.now(timezone.utc).isoformat()

    def _db():

        with db_connect() as conn:
            conn.execute(
                """UPDATE users SET
               display_name=CASE WHEN ? != '' THEN ? ELSE COALESCE(display_name, '') END,
               avatar_url=CASE WHEN ? != '' THEN ? ELSE COALESCE(avatar_url, '') END,
               updated_at=?
               WHERE wallet_address=?""",
                (
                    display_name,
                    display_name,
                    avatar_url,
                    avatar_url,
                    now,
                    wallet_address,
                ),
            )
            conn.commit()
        return {"ok": True, "display_name": display_name}

    return await asyncio.to_thread(_db)


@router.post("/api/user/wallet/bind")
async def user_wallet_bind(request: Request):
    """T287 Wave1: 已下线。绑定 PM 钱包必须通过 Privy + 系统派生 Safe 流程。"""
    identity = get_request_identity(request)
    wallet_address = str((identity or {}).get("wallet_address") or "")
    body: dict[str, object] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    _log_blocked_binding_attempt(
        request,
        wallet_address=wallet_address,
        endpoint="/api/user/wallet/bind",
        submitted_payload=body,
    )
    return JSONResponse(_BIND_DEPRECATED_PAYLOAD, status_code=410)


@router.post("/api/user/wallet/unbind")
async def user_wallet_unbind(request: Request):
    """T287 Wave1: 解绑 PM 钱包同样需要走 Privy 流程；普通用户接口已下线。"""
    identity = get_request_identity(request)
    wallet_address = str((identity or {}).get("wallet_address") or "")
    _log_blocked_binding_attempt(
        request,
        wallet_address=wallet_address,
        endpoint="/api/user/wallet/unbind",
        submitted_payload=None,
    )
    payload = dict(_BIND_DEPRECATED_PAYLOAD)
    payload["message"] = (
        "用户自助解绑已下线。如确需解绑请联系管理员通过 /api/system/users/unbind-pm-wallet 处理。"
    )
    return JSONResponse(payload, status_code=410)


@router.post("/api/user/email/verify")
async def user_email_verify(request: Request):
    """T288 Wave 4-after: 自建邮箱验证已下线（统一由 Privy MFA 接管）。"""
    return JSONResponse(
        {
            "error": "deprecated",
            "message": "邮箱验证已统一由 Privy MFA 接管，请使用 /v2/user 中的 MFA 流程。",
        },
        status_code=410,
    )


@router.post("/api/user/email/verify/confirm")
async def user_email_verify_confirm(request: Request):
    """T288 Wave 4-after: 自建邮箱验证已下线（统一由 Privy MFA 接管）。"""
    return JSONResponse(
        {
            "error": "deprecated",
            "message": "邮箱验证已统一由 Privy MFA 接管，请使用 /v2/user 中的 MFA 流程。",
        },
        status_code=410,
    )


@router.get("/api/user/strategy")
async def user_strategy_get(request: Request):
    """获取用户策略偏好（对接真实 runtime 管控状态）。"""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if identity.get("is_admin"):
        runtime = {
            "freeze_auto": bool(cfg_get("risk.emergency_freeze")),
            "auto_trading_enabled": not bool(cfg_get("risk.emergency_freeze")),
            "updated_at": None,
        }
        return _strategy_payload("__admin__", runtime)

    wallet_address = str(identity.get("wallet_address") or "")
    runtime = _get_or_create_runtime(wallet_address)
    return _strategy_payload(wallet_address, runtime)


@router.post("/api/user/strategy")
async def user_strategy_set(request: Request):
    """保存用户策略偏好（仅自动交易总开关生效）。"""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if identity.get("is_admin"):
        return {"ok": True, "updated": False, "message": "admin profile is read-only"}

    wallet_address = str(identity.get("wallet_address") or "")
    try:
        body = await request.json()
    except Exception:
        body = {}

    target_auto = None
    if "auto_trading_enabled" in body:
        target_auto = bool(body.get("auto_trading_enabled"))
    elif "auto_bet" in body:
        target_auto = bool(body.get("auto_bet"))

    updated = False
    if target_auto is not None:
        _ensure_runtime_table()
        now = datetime.now(timezone.utc).isoformat()
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO user_runtime_controls(wallet_address, freeze_auto, auto_trading_enabled, updated_at)
                VALUES(?, 0, ?, ?)
                ON CONFLICT(wallet_address) DO UPDATE SET
                    auto_trading_enabled=excluded.auto_trading_enabled,
                    updated_at=excluded.updated_at
                """,
                (wallet_address, 1 if target_auto else 0, now),
            )
            conn.commit()
        updated = True

    runtime = _get_or_create_runtime(wallet_address)
    payload = _strategy_payload(wallet_address, runtime)
    payload["updated"] = updated
    return payload


# ═══════════════════════════════════════════════════════════════
# 超级管理员端点
# ═══════════════════════════════════════════════════════════════


@router.get("/api/admin/users")
async def admin_list_users(request: Request):
    """列出所有用户（仅管理员）。"""
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    _ensure_user_tables()

    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                """SELECT wallet_address, role, display_name, email, email_verified,
               created_at, last_login_at, source_ip
               FROM users ORDER BY created_at DESC LIMIT 200"""
            ).fetchall()
        return {"ok": True, "users": [dict(r) for r in rows], "total": len(rows)}

    return await asyncio.to_thread(_db)


@router.post("/api/admin/users/{wallet}/promote")
async def admin_promote_user(wallet: str, request: Request):
    """提升/修改用户角色（仅管理员）。"""
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    new_role = str(body.get("role") or "super_admin").strip()
    if new_role not in ("manual_user", "admin", "super_admin"):
        return JSONResponse({"error": f"invalid role: {new_role}"}, status_code=400)

    wallet = wallet.strip().lower()
    if not _ADDR_RE.match(wallet):
        return JSONResponse({"error": "invalid wallet address"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()

    def _db():
        with db_connect() as conn:
            result = conn.execute(
                "UPDATE users SET role=?, updated_at=? WHERE wallet_address=?",
                (new_role, now, wallet),
            )
            # 同步更新该用户所有活跃 session 的角色
            conn.execute(
                "UPDATE user_sessions SET role=? WHERE wallet_address=?",
                (new_role, wallet),
            )
            conn.commit()
        return {"ok": True, "wallet_address": wallet, "role": new_role}

    return await asyncio.to_thread(_db)
