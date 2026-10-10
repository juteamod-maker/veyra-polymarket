"""ClobClientFactory — central, multi-tenant ClobClient instantiation.

T288 Wave 4: Centralized factory for ExecutionGateway / V2BuilderAdapter
instances, enabling per-user Gnosis Safe (signature_type=2) ordering.

Usage patterns:

    # System-wide auto-trading (single funder from POLY_RELAYER_ADDRESS env)
    gw = get_system_gateway()
    gw.place_order(...)

    # Per-user ordering (Wave 6: Discover page direct buy)
    gw = get_user_gateway(wallet_address="0xUSER...")
    gw.place_order(...)

    # Direct funder lookup (when you already have the Safe address)
    gw = get_gateway_for_funder(funder_address="0xSAFE...")

The factory caches gateways by lowercased funder address so all subsequent
calls for the same funder reuse the same ClobClient instance (HTTP keep-alive,
EIP-712 domain caching, etc.).

Threading: the cache is protected by a module-level lock. Each
V2BuilderAdapter has its own internal lock for lazy ClobClient init.
"""

from __future__ import annotations

import logging
import os
from threading import Lock

from src.executor.execution_gateway import ExecutionGateway
from src.executor.v2_builder_adapter import V2BuilderAdapter
from src.utils.database import connect as _pg_connect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CLOB_HOST = "https://clob.polymarket.com"

# Module-level cache: funder_lower -> ExecutionGateway
_cache: dict[str, ExecutionGateway] = {}
_cache_lock = Lock()

# Sentinel for the system-wide gateway (uses POLY_RELAYER_ADDRESS)
_SYSTEM_KEY = "__system__"


def _resolve_host() -> str:
    return (os.environ.get("POLY_CLOB_HOST") or DEFAULT_CLOB_HOST).strip()


def _normalise_funder(addr: str | None) -> str:
    return (addr or "").strip().lower()


# ---------------------------------------------------------------------------
# Public factory API
# ---------------------------------------------------------------------------


def get_system_gateway() -> ExecutionGateway:
    """Return the singleton system-wide gateway used by the auto-trading bot.

    Uses POLY_RELAYER_ADDRESS env var as the funder. This is the legacy
    single-tenant path used by `order_executor`, `paper_trader`, etc.
    """
    with _cache_lock:
        gw = _cache.get(_SYSTEM_KEY)
        if gw is not None:
            return gw
        gw = V2BuilderAdapter(host=_resolve_host())
        _cache[_SYSTEM_KEY] = gw
        logger.info("ClobClientFactory: created system gateway (env funder)")
        return gw


def get_gateway_for_funder(funder_address: str) -> ExecutionGateway:
    """Return a gateway bound to a specific Gnosis Safe funder address.

    Subsequent calls with the same funder return the same instance.
    Empty / falsy funder raises ValueError.
    """
    funder = _normalise_funder(funder_address)
    if not funder or not funder.startswith("0x") or len(funder) != 42:
        raise ValueError(f"invalid funder address: {funder_address!r}")

    with _cache_lock:
        gw = _cache.get(funder)
        if gw is not None:
            return gw
        gw = V2BuilderAdapter(host=_resolve_host(), funder=funder)
        _cache[funder] = gw
        logger.info(
            "ClobClientFactory: created per-user gateway for funder %s...%s",
            funder[:6],
            funder[-4:],
        )
        return gw


def get_user_gateway(wallet_address: str) -> ExecutionGateway:
    """Return a gateway for a manual user identified by their EOA wallet.

    Looks up `users.funder_address` (the user's Gnosis Safe / proxy) and
    returns / creates the corresponding gateway.

    Raises:
        LookupError: if the user is not found or has no funder_address bound.
        ValueError: on invalid wallet input.
    """
    wallet = _normalise_funder(wallet_address)
    if not wallet or not wallet.startswith("0x") or len(wallet) != 42:
        raise ValueError(f"invalid wallet address: {wallet_address!r}")

    funder = _lookup_user_funder(wallet)
    if not funder:
        raise LookupError(
            f"user {wallet} has no funder_address bound — call /api/pm/bind-wallet first"
        )
    return get_gateway_for_funder(funder)


def reset_cache() -> None:
    """Drop all cached gateways (test helper / config-reload hook)."""
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _lookup_user_funder(wallet_address: str) -> str | None:
    """Read `users.funder_address` for the given wallet (lowercased)."""
    try:
        with _pg_connect() as conn:
            row = conn.execute(
                "SELECT funder_address FROM users WHERE wallet_address=?",
                (wallet_address,),
            ).fetchone()
            if not row:
                return None
            funder = row["funder_address"] if hasattr(row, "keys") else row[0]
            funder = (funder or "").strip().lower()
            return funder or None
    except Exception as exc:
        logger.exception(
            "ClobClientFactory._lookup_user_funder(%s) failed: %s", wallet_address, exc
        )
        return None
