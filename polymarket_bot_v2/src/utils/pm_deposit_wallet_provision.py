"""T906 — Polymarket Deposit Wallet (POLY_1271) gasless approval provisioning.

Implements the on-chain approval flow for Polymarket Deposit Wallet users
strictly per the official PM docs at
``https://docs.polymarket.com/trading/deposit-wallets`` and the relayer SDK
``py_builder_relayer_client.builder.deposit_wallet`` which canonicalises the
EIP-712 ``DepositWallet`` Batch domain.

End-to-end flow (server-orchestrated, user-signed):

1. Server computes which (token → spender) ERC-20 allowances are missing and
   which (operator) ERC-1155 ``setApprovalForAll`` flags are missing on the
   user's deposit wallet.
2. Server fetches the relayer ``WALLET`` nonce for the user's owner EOA via
   ``RelayClient.get_nonce(owner_eoa, "WALLET")``.
3. Server builds the EIP-712 typed data for the ``Batch`` primaryType using
   ``DepositWalletCall(target,value,data)`` entries — ABI-encoded calldata for
   ``IERC20.approve`` and ``IConditionalTokens.setApprovalForAll`` — and
   returns it to the browser together with a one-shot prepare token.
4. Browser asks the user's Privy embedded wallet (the deposit wallet's owner
   EOA) to ``eth_signTypedData_v4`` over that typed data.
5. Browser POSTs prepare_token + signature back to the server, which assembles
   a ``DepositWalletBatchRequest`` with the user's signature in place of the
   builder signer's signature, then submits it to the relayer's
   ``SUBMIT_TRANSACTION`` endpoint authenticated with builder L2 headers.
6. Server polls the relayer until the transaction reaches ``STATE_MINED`` (or
   the corresponding fail state) and returns the tx hash to the browser.

This module performs steps (1)/(3) in :func:`build_provision_request` and
step (5)/(6) in :func:`submit_provision`. The endpoint plumbing lives in
``scripts/dashboard_routes/discover_routes.py``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Maxed approval value (uint256 max). Polymarket SDK uses unlimited so users
# do not have to re-approve on every order. We match that.
_UINT256_MAX = (1 << 256) - 1

# Allowance threshold below which we consider the slot "missing" and re-approve.
# Anything ≥ 2**200 is treated as effectively unlimited.
_UNLIMITED_THRESHOLD = 1 << 200

# Default deadline for the relayer batch (60 minutes is well within the
# DepositWallet contract's accepted window and gives the user enough time to
# sign in case of UI delay).
_DEFAULT_DEADLINE_SECS = 60 * 60

# Per the relayer SDK, the relayer's WALLET nonce endpoint expects the owner
# EOA address and ``type=WALLET`` (deposit-wallet batch nonce, not Safe nonce).
_WALLET_NONCE_TYPE = "WALLET"


# ---------------------------------------------------------------------------
# Calldata helpers
# ---------------------------------------------------------------------------

# Function selectors (keccak("approve(address,uint256)")[:4] etc.)
_ERC20_APPROVE_SELECTOR = "0x095ea7b3"  # approve(address,uint256)
_ERC1155_SET_APPROVAL_SELECTOR = "0xa22cb465"  # setApprovalForAll(address,bool)


def _pad_address(addr: str) -> str:
    """Pad a 20-byte address to a 32-byte ABI word (lowercase, no 0x)."""
    a = str(addr or "").lower().replace("0x", "")
    if len(a) != 40:
        raise ValueError(f"invalid address: {addr}")
    return a.rjust(64, "0")


def _pad_uint256(value: int) -> str:
    """Pad an unsigned int to a 32-byte ABI word (lowercase hex, no 0x)."""
    if value < 0:
        raise ValueError("uint256 must be non-negative")
    return hex(value)[2:].rjust(64, "0")


def _encode_erc20_approve(spender: str, amount: int) -> str:
    """ABI-encode ``IERC20.approve(spender, amount)`` calldata (with 0x)."""
    return _ERC20_APPROVE_SELECTOR + _pad_address(spender) + _pad_uint256(amount)


def _encode_erc1155_set_approval_for_all(operator: str, approved: bool) -> str:
    """ABI-encode ``IERC1155.setApprovalForAll(operator, approved)`` calldata."""
    return (
        _ERC1155_SET_APPROVAL_SELECTOR
        + _pad_address(operator)
        + _pad_uint256(1 if approved else 0)
    )


# ---------------------------------------------------------------------------
# On-chain allowance read
# ---------------------------------------------------------------------------

def _make_web3():
    rpc_url = (
        str(os.environ.get("BUILDER_RELAYER_RPC_URL") or "").strip()
        or str(os.environ.get("POLYGON_RPC_URL") or "").strip()
        or "https://polygon-rpc.com/"
    )
    from web3 import Web3  # type: ignore  # noqa: PLC0415

    return Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 8}))


_ERC20_ALLOWANCE_ABI = [
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

_ERC1155_IS_APPROVED_FOR_ALL_ABI = [
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


def _spender_pairs() -> List[Tuple[str, str, str, str]]:
    """Return [(token_name, token_addr, spender_name, spender_addr), ...].

    Includes both pUSD (current default collateral) and USDC.e (legacy
    fallback) for both CTF Exchange V2 and NegRisk Exchange V2.
    """
    from src.utils.pm_v2_constants import (  # noqa: PLC0415
        CTF_EXCHANGE_V2,
        NEG_RISK_EXCHANGE_V2,
        PUSD_TOKEN,
        USDC_E_TOKEN,
    )

    return [
        ("PUSD", PUSD_TOKEN, "CTF_EXCHANGE_V2", CTF_EXCHANGE_V2),
        ("PUSD", PUSD_TOKEN, "NEG_RISK_EXCHANGE_V2", NEG_RISK_EXCHANGE_V2),
        ("USDC_E", USDC_E_TOKEN, "CTF_EXCHANGE_V2", CTF_EXCHANGE_V2),
        ("USDC_E", USDC_E_TOKEN, "NEG_RISK_EXCHANGE_V2", NEG_RISK_EXCHANGE_V2),
    ]


def _ctf_operators() -> List[Tuple[str, str]]:
    from src.utils.pm_v2_constants import (  # noqa: PLC0415
        CTF_EXCHANGE_V2,
        NEG_RISK_EXCHANGE_V2,
    )

    return [
        ("CTF_EXCHANGE_V2", CTF_EXCHANGE_V2),
        ("NEG_RISK_EXCHANGE_V2", NEG_RISK_EXCHANGE_V2),
    ]


def discover_missing_approvals(deposit_wallet: str) -> List[Dict[str, Any]]:
    """Return the list of (token, spender) and (operator) approvals that the
    deposit wallet is missing on chain.

    Each entry is one of:
        {kind: "erc20_approve", token, token_address, spender, spender_address}
        {kind: "ctf_set_approval_for_all", operator, operator_address}

    All 6 reads are issued in parallel via a thread pool. Empirically a
    sequential implementation took ~16s against polygon-rpc.com (the latency
    is dominated by RPC round-trip, not Web3.py overhead). Parallel reads
    bring the total wall time down to ~2-3s, which is the longest single
    RPC call. This is critical for the new T906 pre-flight gate so the
    Privy popup still feels instant after the user clicks Buy.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415
    from eth_utils import to_checksum_address  # type: ignore  # noqa: PLC0415

    from src.utils.pm_v2_constants import CTF  # noqa: PLC0415

    w3 = _make_web3()
    deposit_addr = to_checksum_address(deposit_wallet)
    pairs = _spender_pairs()
    operators = _ctf_operators()

    def _read_allowance(args):
        token_name, token_addr, spender_name, spender_addr = args
        try:
            c = w3.eth.contract(
                address=to_checksum_address(token_addr),
                abi=_ERC20_ALLOWANCE_ABI,
            )
            allowance = int(
                c.functions.allowance(
                    deposit_addr, to_checksum_address(spender_addr)
                ).call()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Provision] allowance read failed token=%s spender=%s err=%s",
                token_name, spender_name, exc,
            )
            allowance = 0
        return ("erc20", token_name, token_addr, spender_name, spender_addr, allowance)

    def _read_isapprovedforall(args):
        op_name, op_addr = args
        try:
            ctf_contract = w3.eth.contract(
                address=to_checksum_address(CTF),
                abi=_ERC1155_IS_APPROVED_FOR_ALL_ABI,
            )
            approved = bool(
                ctf_contract.functions.isApprovedForAll(
                    deposit_addr, to_checksum_address(op_addr)
                ).call()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Provision] isApprovedForAll read failed op=%s err=%s",
                op_name, exc,
            )
            approved = False
        return ("ctf", op_name, op_addr, approved)

    erc20_results: List[Any] = []
    ctf_results: List[Any] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = []
        for p in pairs:
            futs.append(pool.submit(_read_allowance, p))
        for op in operators:
            futs.append(pool.submit(_read_isapprovedforall, op))
        for fut in as_completed(futs):
            r = fut.result()
            if r[0] == "erc20":
                erc20_results.append(r)
            else:
                ctf_results.append(r)

    missing: List[Dict[str, Any]] = []
    # Preserve a deterministic ordering (PUSD/CTFv2 → PUSD/NRv2 → USDC.e/CTFv2 → USDC.e/NRv2 → CTFv2 op → NRv2 op).
    erc20_index = {(r[1], r[3]): r for r in erc20_results}
    for token_name, token_addr, spender_name, spender_addr in pairs:
        r = erc20_index.get((token_name, spender_name))
        if r is None:
            continue
        allowance = int(r[5])
        if allowance < _UNLIMITED_THRESHOLD:
            missing.append({
                "kind": "erc20_approve",
                "token": token_name,
                "token_address": str(token_addr).lower(),
                "spender": spender_name,
                "spender_address": str(spender_addr).lower(),
                "current_allowance": str(allowance),
            })
    ctf_index = {r[1]: r for r in ctf_results}
    for op_name, op_addr in operators:
        r = ctf_index.get(op_name)
        if r is None:
            continue
        if not bool(r[3]):
            missing.append({
                "kind": "ctf_set_approval_for_all",
                "operator": op_name,
                "operator_address": str(op_addr).lower(),
            })

    return missing


# ---------------------------------------------------------------------------
# Build EIP-712 typed data
# ---------------------------------------------------------------------------

def _missing_to_calls(missing: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert the missing-approvals list into ``Call`` entries for the Batch.

    Each entry: ``{target, value, data}``. We always approve the unlimited
    amount to match Polymarket's default behaviour and avoid re-approval
    friction.
    """
    from src.utils.pm_v2_constants import CTF  # noqa: PLC0415

    calls: List[Dict[str, Any]] = []
    for entry in missing:
        kind = entry.get("kind")
        if kind == "erc20_approve":
            token = str(entry["token_address"]).lower()
            spender = str(entry["spender_address"]).lower()
            calls.append({
                "target": token,
                "value": 0,
                "data": _encode_erc20_approve(spender, _UINT256_MAX),
            })
        elif kind == "ctf_set_approval_for_all":
            operator = str(entry["operator_address"]).lower()
            calls.append({
                "target": str(CTF).lower(),
                "value": 0,
                "data": _encode_erc1155_set_approval_for_all(operator, True),
            })
        else:
            raise ValueError(f"unknown missing approval kind: {kind}")
    return calls


# EIP-712 type definitions for the DepositWallet Batch domain. Mirrors
# ``py_builder_relayer_client.builder.deposit_wallet.DEPOSIT_WALLET_TYPES``.
DEPOSIT_WALLET_TYPES: Dict[str, List[Dict[str, str]]] = {
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
}

DEPOSIT_WALLET_DOMAIN_NAME = "DepositWallet"
DEPOSIT_WALLET_DOMAIN_VERSION = "1"
POLYGON_CHAIN_ID = 137


def build_typed_data(
    deposit_wallet: str,
    nonce: int,
    deadline: int,
    calls: List[Dict[str, Any]],
    chain_id: int = POLYGON_CHAIN_ID,
) -> Dict[str, Any]:
    """Return the EIP-712 typed data dict the browser will sign."""
    deposit_wallet = str(deposit_wallet or "").lower()
    return {
        "primaryType": "Batch",
        "types": DEPOSIT_WALLET_TYPES,
        "domain": {
            "name": DEPOSIT_WALLET_DOMAIN_NAME,
            "version": DEPOSIT_WALLET_DOMAIN_VERSION,
            "chainId": int(chain_id),
            "verifyingContract": deposit_wallet,
        },
        "message": {
            "wallet": deposit_wallet,
            "nonce": int(nonce),
            "deadline": int(deadline),
            "calls": [
                {
                    "target": str(c["target"]).lower(),
                    "value": int(c.get("value") or 0),
                    "data": str(c["data"]),
                }
                for c in calls
            ],
        },
    }


# ---------------------------------------------------------------------------
# Prepare / submit
# ---------------------------------------------------------------------------

def build_provision_request(
    *, owner_eoa: str, deposit_wallet: str
) -> Dict[str, Any]:
    """Step 1+2+3: discover missing approvals, fetch nonce, build typed data.

    Returns a dict the route handler can cache (under a one-shot prepare
    token) and return to the browser:

        {
            "status": "ok"|"needs_provisioning"|"client_unavailable"|"error",
            "missing": [...],            # only when needs_provisioning
            "typed_data": {...},         # only when needs_provisioning
            "calls": [...],              # only when needs_provisioning
            "nonce": "<int>",            # only when needs_provisioning
            "deadline": "<unix>",        # only when needs_provisioning
            "deposit_wallet": ...,
            "owner_eoa": ...,
            "chain_id": 137,
        }
    """
    from src.utils.pm_relayer_client import (  # noqa: PLC0415
        credentials_present,
        get_client,
    )

    owner_eoa = str(owner_eoa or "").lower()
    deposit_wallet = str(deposit_wallet or "").lower()
    if not owner_eoa or not deposit_wallet:
        return {"status": "error", "error": "owner_eoa and deposit_wallet required"}

    if not credentials_present():
        return {"status": "no_credentials"}

    client = get_client()
    if client is None:
        return {"status": "client_unavailable"}

    try:
        missing = discover_missing_approvals(deposit_wallet)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Provision] discover failed: %s", exc)
        return {"status": "error", "error": f"discover failed: {exc}"}

    if not missing:
        return {
            "status": "ok",
            "deposit_wallet": deposit_wallet,
            "owner_eoa": owner_eoa,
        }

    try:
        nonce_payload = client.get_nonce(owner_eoa, _WALLET_NONCE_TYPE)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[Provision] get_nonce failed owner=%s err=%s", owner_eoa[:10], exc)
        return {"status": "error", "error": f"get_nonce failed: {exc}"}

    if not isinstance(nonce_payload, dict) or nonce_payload.get("nonce") is None:
        logger.error("[Provision] invalid nonce payload: %r", nonce_payload)
        return {"status": "error", "error": "invalid nonce payload from relayer"}
    nonce = int(nonce_payload["nonce"])

    deadline = int(time.time()) + _DEFAULT_DEADLINE_SECS
    calls = _missing_to_calls(missing)
    typed_data = build_typed_data(
        deposit_wallet=deposit_wallet,
        nonce=nonce,
        deadline=deadline,
        calls=calls,
    )

    logger.info(
        "[Provision] prepare owner=%s deposit_wallet=%s nonce=%s deadline=%s "
        "missing=%d calls=%d",
        owner_eoa[:10], deposit_wallet[:10], nonce, deadline,
        len(missing), len(calls),
    )

    return {
        "status": "needs_provisioning",
        "deposit_wallet": deposit_wallet,
        "owner_eoa": owner_eoa,
        "missing": missing,
        "calls": calls,
        "nonce": str(nonce),
        "deadline": str(deadline),
        "chain_id": POLYGON_CHAIN_ID,
        "typed_data": typed_data,
    }


def submit_provision(
    *,
    owner_eoa: str,
    deposit_wallet: str,
    nonce: str,
    deadline: str,
    calls: List[Dict[str, Any]],
    user_signature: str,
    poll_max: int = 30,
    poll_interval_secs: float = 2.0,
) -> Dict[str, Any]:
    """Step 5+6: submit the user-signed batch to the relayer; poll mined.

    Returns ``{status: "mined"|"failed"|"timeout"|"error", tx_hash: ..., raw: ...}``.
    """
    from py_builder_relayer_client.endpoints import SUBMIT_TRANSACTION  # type: ignore  # noqa: PLC0415
    from py_builder_relayer_client.http_helpers.helpers import POST, post  # type: ignore  # noqa: PLC0415
    from py_builder_relayer_client.models import (  # type: ignore  # noqa: PLC0415
        DepositWalletCall,
        TransactionType,
    )

    from src.utils.pm_relayer_client import get_client  # noqa: PLC0415

    client = get_client()
    if client is None:
        return {"status": "error", "error": "relayer client unavailable"}

    owner_eoa = str(owner_eoa or "").lower()
    deposit_wallet = str(deposit_wallet or "").lower()
    sig = str(user_signature or "").strip()
    if not sig.startswith("0x") or len(sig) < 132:
        return {"status": "error", "error": "invalid user signature"}

    # Reconstruct DepositWalletCall list from cached calls. We don't trust the
    # browser to send these back — they came from our cached prepare payload.
    sdk_calls: List[Any] = []
    for c in calls:
        sdk_calls.append(
            DepositWalletCall(
                target=str(c["target"]).lower(),
                value=str(int(c.get("value") or 0)),
                data=str(c["data"]),
            )
        )

    # Build the request body manually because the SDK's
    # build_deposit_wallet_batch_request() signs with the builder's signer,
    # whereas we need the user's (Privy) signature here.
    # Per ``DepositWalletBatchRequest.to_dict()`` in the relayer SDK, the
    # POST body wraps deposit-wallet specifics under ``depositWalletParams``.
    txn_request = {
        "type": TransactionType.WALLET.value,
        "from": owner_eoa,
        "to": str(client.contract_config.deposit_wallet_factory).lower(),
        "nonce": str(nonce),
        "signature": sig,
        "depositWalletParams": {
            "depositWallet": deposit_wallet,
            "deadline": str(deadline),
            "calls": [
                {
                    "target": c.target,
                    "value": c.value,
                    "data": c.data,
                }
                for c in sdk_calls
            ],
        },
    }

    builder_headers = client._generate_builder_headers(
        POST, SUBMIT_TRANSACTION, txn_request
    )
    if builder_headers is None:
        return {"status": "error", "error": "builder header generation failed"}

    try:
        resp = post(
            f"{client.relayer_url}{SUBMIT_TRANSACTION}",
            headers=builder_headers,
            data=txn_request,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[Provision] relayer submit raised owner=%s deposit_wallet=%s err=%s",
            owner_eoa[:10], deposit_wallet[:10], exc,
        )
        return {"status": "error", "error": f"relayer submit raised: {exc}"}

    if not resp or not isinstance(resp, dict):
        logger.error("[Relayer 4xx] provision submit returned non-dict: %r", resp)
        return {"status": "error", "error": "relayer returned no payload"}

    tx_id = resp.get("transactionID")
    tx_hash = resp.get("transactionHash")

    if not tx_id:
        logger.error("[Relayer 4xx] provision submit missing transactionID: %r", resp)
        return {
            "status": "error",
            "error": resp.get("error") or "relayer rejected submission",
            "raw": resp,
        }

    logger.info(
        "[Provision] submitted owner=%s deposit_wallet=%s tx_id=%s tx_hash=%s",
        owner_eoa[:10], deposit_wallet[:10], tx_id, str(tx_hash)[:14],
    )

    # Poll until mined (or fail). The relayer reports state via GET_TRANSACTION.
    # State strings observed from docs: STATE_NEW, STATE_PENDING, STATE_MINED,
    # STATE_FAILED.
    for attempt in range(int(poll_max)):
        try:
            txns = client.get_transaction(tx_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Provision] get_transaction failed: %s", exc)
            txns = None
        if txns:
            txn = txns[0] if isinstance(txns, list) else txns
            state = str(txn.get("state") or "").upper() if isinstance(txn, dict) else ""
            current_hash = txn.get("transactionHash") if isinstance(txn, dict) else None
            if state in {"STATE_MINED", "STATE_EXECUTED", "STATE_CONFIRMED"}:
                return {
                    "status": "mined",
                    "tx_id": tx_id,
                    "tx_hash": current_hash or tx_hash,
                    "raw": txn,
                }
            if state in {"STATE_FAILED", "STATE_INVALID"}:
                logger.error(
                    "[Provision] mining failed owner=%s tx_id=%s state=%s hash=%s",
                    owner_eoa[:10], tx_id, state, current_hash,
                )
                return {
                    "status": "failed",
                    "tx_id": tx_id,
                    "tx_hash": current_hash or tx_hash,
                    "state": state,
                    "raw": txn,
                }
        time.sleep(float(poll_interval_secs))

    logger.warning(
        "[Provision] mining timeout owner=%s tx_id=%s after %s polls",
        owner_eoa[:10], tx_id, poll_max,
    )
    return {
        "status": "timeout",
        "tx_id": tx_id,
        "tx_hash": tx_hash,
    }


__all__ = [
    "build_provision_request",
    "submit_provision",
    "discover_missing_approvals",
    "build_typed_data",
]
