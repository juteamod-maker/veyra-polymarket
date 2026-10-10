"""T878-Wave-C — Polymarket Builder Relayer client wrapper.

Lazy-initialized singleton wrapper around
``py_builder_relayer_client.RelayClient`` for **gasless** execution of:

* ERC-20 approvals (USDC.e / pUSD → CTF / NegRisk Exchanges)
* CTF operations (Split / Merge / Redeem)
* (Future) Deposit Wallet on-behalf-of-user deploy when SDK supports it

Configuration (backend env vars, never exposed to frontend):

* ``BUILDER_API_KEY``       — L2 builder API key
* ``BUILDER_SECRET``        — L2 builder secret
* ``BUILDER_PASSPHRASE``    — L2 builder passphrase
* ``BUILDER_CODE``          — (optional) attribution code
* ``BUILDER_RELAYER_HOST``  — (optional) override host

When credentials are missing the helpers return a structured
``{"status": "no_credentials", ...}`` payload so callers (and the
``/api/pm/ensure-approvals`` endpoint) can degrade gracefully instead of
exposing a broken UI affordance to end users.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

POLYGON_CHAIN_ID = 137
DEFAULT_RELAYER_HOST = "https://relayer-v2.polymarket.com/"

_lock = threading.Lock()
_client_singleton: Optional[Any] = None
_init_attempted: bool = False


def _builder_credentials() -> Dict[str, str]:
    """Read BUILDER_* env vars with POLY_* fallback.

    The Polymarket Builder Relayer accepts the same L2 API key triple
    (api_key / secret / passphrase) issued by the CLOB ``/auth/api-key``
    endpoint. Operators typically already have these provisioned as
    ``POLY_API_KEY`` / ``POLY_SECRET`` / ``POLY_PASSPHRASE`` for trading,
    so we accept those as a fallback to avoid forcing duplicate env vars.
    Explicit ``BUILDER_*`` values still take precedence so an operator can
    override per-environment without touching the trading creds.
    """

    def _first(*names: str) -> str:
        for n in names:
            v = str(os.environ.get(n) or "").strip()
            if v:
                return v
        return ""

    return {
        "api_key": _first("BUILDER_API_KEY", "POLY_API_KEY"),
        "secret": _first("BUILDER_SECRET", "POLY_SECRET"),
        "passphrase": _first("BUILDER_PASSPHRASE", "POLY_PASSPHRASE"),
        "code": _first("BUILDER_CODE", "POLY_BUILDER_CODE"),
        "host": str(
            os.environ.get("BUILDER_RELAYER_HOST") or DEFAULT_RELAYER_HOST
        ).strip(),
    }


def credentials_present() -> bool:
    """True iff the three required builder credentials are configured."""

    creds = _builder_credentials()
    return bool(creds["api_key"] and creds["secret"] and creds["passphrase"])


def _signer_private_key() -> str:
    """Operator private key used to sign relayer-bound transactions.

    Currently this is the platform's POLY_PRIVATE_KEY. We accept an
    explicit ``RELAYER_SIGNER_PRIVATE_KEY`` override for environments
    that want a separate key dedicated to the relayer signer role.
    """

    return (
        str(os.environ.get("RELAYER_SIGNER_PRIVATE_KEY") or "").strip()
        or str(os.environ.get("POLY_PRIVATE_KEY") or "").strip()
    )


def get_client() -> Optional[Any]:
    """Return a lazy-initialized RelayClient or ``None`` if creds missing."""

    global _client_singleton, _init_attempted

    if _client_singleton is not None:
        return _client_singleton

    with _lock:
        if _client_singleton is not None:
            return _client_singleton
        if _init_attempted and _client_singleton is None:
            return None  # cached negative — don't retry on every call
        _init_attempted = True

        creds = _builder_credentials()
        if not (creds["api_key"] and creds["secret"] and creds["passphrase"]):
            logger.info(
                "[Wave-C] Builder Relayer credentials not configured "
                "(BUILDER_API_KEY / BUILDER_SECRET / BUILDER_PASSPHRASE or "
                "POLY_API_KEY / POLY_SECRET / POLY_PASSPHRASE); gasless "
                "approvals/CTF helpers disabled."
            )
            return None

        signer_pk = _signer_private_key()
        if not signer_pk:
            logger.info(
                "[Wave-C] Relayer signer private key missing "
                "(POLY_PRIVATE_KEY / RELAYER_SIGNER_PRIVATE_KEY); "
                "gasless helpers disabled."
            )
            return None

        try:
            from py_builder_relayer_client.client import RelayClient  # type: ignore
            from py_builder_signing_sdk.config import BuilderConfig  # type: ignore
            from py_builder_signing_sdk.sdk_types import (  # type: ignore
                BuilderApiKeyCreds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[Wave-C] py-builder-relayer-client import failed: %s", exc
            )
            return None

        try:
            builder_config = BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=creds["api_key"],
                    secret=creds["secret"],
                    passphrase=creds["passphrase"],
                )
            )
            _client_singleton = RelayClient(
                relayer_url=creds["host"],
                chain_id=POLYGON_CHAIN_ID,
                private_key=signer_pk,
                builder_config=builder_config,
            )
            logger.info(
                "[Wave-C] Builder Relayer client initialized host=%s chain=%s",
                creds["host"],
                POLYGON_CHAIN_ID,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[Wave-C] RelayClient init failed: %s", exc, exc_info=True
            )
            _client_singleton = None

    return _client_singleton


def ensure_approvals(
    deposit_wallet: str,
    owner_eoa: str,
) -> Dict[str, Any]:
    """Best-effort gasless approvals provisioning for ``deposit_wallet``.

    Returns a structured status payload. Never raises — callers decide
    whether to surface degradation to the UI.

    Status values:
        ``no_credentials``     — BUILDER_* not configured (no-op, soft).
        ``client_unavailable`` — SDK import/init error (logged).
        ``ok``                 — every (token → spender) allowance is at
                                 the unlimited threshold; no action required.
        ``needs_approval``     — at least one allowance is below threshold;
                                 ``missing`` lists the (token, spender) pairs
                                 that need to be approved. The deposit-wallet
                                 typed-data submit path will include the
                                 missing approve() calls in its batch.
        ``error``              — chain RPC failed.
    """

    client = get_client()
    if client is None:
        if not credentials_present():
            return {"status": "no_credentials"}
        return {"status": "client_unavailable"}

    # --- On-chain READ check -------------------------------------------------
    # We only ever READ here. WRITE provisioning happens inside the user's
    # normal dispatch path (split / merge / redeem / order), where the
    # deposit-wallet typed-data batch already includes a leading approve()
    # call when allowance is insufficient. ensure_approvals is a HINT for
    # the UI to display "授权中…" loading states without surprising the user.
    try:
        from eth_utils import to_checksum_address  # type: ignore
        from web3 import Web3  # type: ignore

        from src.utils.pm_v2_constants import (
            CTF,
            CTF_EXCHANGE_V2,
            NEG_RISK_EXCHANGE_V2,
            PUSD_TOKEN,
            USDC_E_TOKEN,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[Wave-C] ensure_approvals dependency import failed: %s", exc
        )
        return {"status": "client_unavailable", "error": str(exc)}

    rpc_url = (
        str(os.environ.get("BUILDER_RELAYER_RPC_URL") or "").strip()
        or str(os.environ.get("POLYGON_RPC_URL") or "").strip()
        or "https://polygon-rpc.com/"
    )
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 6}))
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"web3 init: {exc}"}

    # Spenders that the deposit wallet needs to authorize for trading,
    # split, merge, redeem on Polymarket V2.
    erc20_abi = [
        {
            "constant": True,
            "inputs": [
                {"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"},
            ],
            "name": "allowance",
            "outputs": [{"name": "", "type": "uint256"}],
            "type": "function",
        }
    ]
    ctf_abi = [
        {
            "constant": True,
            "inputs": [
                {"name": "owner", "type": "address"},
                {"name": "operator", "type": "address"},
            ],
            "name": "isApprovedForAll",
            "outputs": [{"name": "", "type": "bool"}],
            "type": "function",
        }
    ]

    UNLIMITED_THRESHOLD = (1 << 200)  # well beyond any realistic balance
    deposit_addr = to_checksum_address(deposit_wallet)

    erc20_pairs = [
        ("PUSD", PUSD_TOKEN, "CTF_EXCHANGE_V2", CTF_EXCHANGE_V2),
        ("PUSD", PUSD_TOKEN, "NEG_RISK_EXCHANGE_V2", NEG_RISK_EXCHANGE_V2),
        ("USDC_E", USDC_E_TOKEN, "CTF_EXCHANGE_V2", CTF_EXCHANGE_V2),
        ("USDC_E", USDC_E_TOKEN, "NEG_RISK_EXCHANGE_V2", NEG_RISK_EXCHANGE_V2),
    ]
    ctf_operators = [
        ("CTF_EXCHANGE_V2", CTF_EXCHANGE_V2),
        ("NEG_RISK_EXCHANGE_V2", NEG_RISK_EXCHANGE_V2),
    ]

    missing: list[Dict[str, str]] = []
    try:
        for token_name, token_addr, spender_name, spender_addr in erc20_pairs:
            c = w3.eth.contract(
                address=to_checksum_address(token_addr), abi=erc20_abi
            )
            allowance = c.functions.allowance(
                deposit_addr, to_checksum_address(spender_addr)
            ).call()
            if int(allowance) < UNLIMITED_THRESHOLD:
                missing.append(
                    {
                        "kind": "erc20_approve",
                        "token": token_name,
                        "token_address": token_addr,
                        "spender": spender_name,
                        "spender_address": spender_addr,
                        "current_allowance": str(allowance),
                    }
                )

        ctf_contract = w3.eth.contract(
            address=to_checksum_address(CTF), abi=ctf_abi
        )
        for op_name, op_addr in ctf_operators:
            approved = ctf_contract.functions.isApprovedForAll(
                deposit_addr, to_checksum_address(op_addr)
            ).call()
            if not bool(approved):
                missing.append(
                    {
                        "kind": "ctf_set_approval_for_all",
                        "operator": op_name,
                        "operator_address": op_addr,
                        "current_allowance": "false",
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Wave-C] ensure_approvals chain read failed wallet=%s err=%s",
            deposit_wallet,
            exc,
        )
        return {
            "status": "error",
            "error": str(exc),
            "deposit_wallet": deposit_wallet,
            "owner_eoa": owner_eoa,
        }

    if not missing:
        return {
            "status": "ok",
            "deposit_wallet": deposit_wallet,
            "owner_eoa": owner_eoa,
            "checked": len(erc20_pairs) + len(ctf_operators),
        }

    # T881-2: when ``BUILDER_GASLESS_LIVE`` is set the deposit-wallet typed
    # data batch path appends the missing approve()/setApprovalForAll() calls
    # automatically on the user's next dispatch. We surface ``needs_approval``
    # so the UI can show "授权中…" without forcing a separate signature step.
    return {
        "status": "needs_approval",
        "deposit_wallet": deposit_wallet,
        "owner_eoa": owner_eoa,
        "missing": missing,
        "live_execution": str(
            os.environ.get("BUILDER_GASLESS_LIVE") or ""
        ).strip().lower()
        in {"1", "true", "yes", "on"},
    }


__all__ = [
    "credentials_present",
    "ensure_approvals",
    "get_client",
]
