"""POLY_1271 (signature_type=3) CLOB ordering for deposit_wallet users.

This module implements the missing leg in the V2 ordering chain: orders whose
``maker`` is a Polymarket Deposit Wallet (ERC-1271 contract). The standard
``py_clob_client_v2.ClobClient.create_and_post_order`` call signs the order
locally with a private key — that path is only valid for ``signature_type``
0/1/2 (EOA / POLY_PROXY / POLY_GNOSIS_SAFE).

For ``signature_type=3`` the maker is the deposit_wallet contract itself; the
``signer`` field is the user's Privy embedded EOA. The signature must be
produced by the EOA's private key (which Polymarket then validates via
``isValidSignature`` on the deposit_wallet contract). We therefore:

  1. Reconstruct the EIP-712 typed-data using the SDK's
     ``ExchangeOrderBuilderV2.build_order_typed_data`` helper (no signing).
  2. Send the typed-data to Privy's server-signer (`eth_signTypedData_v4`) to
     have the user's EOA sign it.
  3. Construct ``SignedOrderV2`` manually with the returned signature.
  4. POST it via the SDK's existing ``client.post_order`` (which only does
     L2 HMAC headers + JSON serialisation — no further signing).

Activation is gated by env ``DEPOSIT_WALLET_ORDERING_LIVE=1``. When the flag
is OFF, callers should keep returning the legacy 501 guard. When ON, the
adapter routes deposit_wallet users through ``place_order_poly1271``.

T883 [FundFlow]: every code path here logs with the prefix ``[FundFlow]`` so
operations can grep relayer/CLOB logs for fund-affecting actions.
"""

from __future__ import annotations

import logging
import os
import time
from math import floor as _floor, gcd
from typing import Any

from src.executor.execution_gateway import (
    ExecutionOrderRequest,
    ExecutionOrderResult,
    OrderSide,
)
from src.utils.pm_v2_constants import (
    CTF_EXCHANGE_V2,
    NEG_RISK_EXCHANGE_V2,
    SIGNATURE_TYPE_POLY_1271,
)

logger = logging.getLogger(__name__)

_ZERO_BYTES32 = "0x" + "0" * 64
_ADDR_RE = __import__("re").compile(r"^0x[0-9a-fA-F]{40}$")


def _normalize_builder_code(raw: str | None) -> str:
    code = str(raw or "").strip() or _ZERO_BYTES32
    if not code.startswith("0x"):
        code = "0x" + code
    if len(code) != 66:
        return _ZERO_BYTES32
    return code.lower()


# ---------------------------------------------------------------------------
# Per-user CLOB credential cache:  {user_eoa_lower: (ApiCreds, expiry_ts)}
# Per deposit wallet migration docs: POLY_ADDRESS must be the user's EOA
# (owner of the deposit_wallet), not the server/builder EOA.
# ---------------------------------------------------------------------------
import threading as _threading  # noqa: E402
_user_creds_cache: dict[str, tuple[Any, float]] = {}
_user_creds_lock = _threading.Lock()
_USER_CREDS_TTL = 3600  # 1 hour


def derive_user_clob_creds(
    user_eoa: str,
    privy_wallet_id: str,
    auth_keys: list[str] | None = None,
) -> Any:
    """Derive CLOB API credentials for a user's EOA via Privy server-side signing.

    Per the Deposit Wallet Migration Guide, POLY_ADDRESS must be the user's
    EOA (owner of the deposit_wallet). We use Privy to sign the CLOB L1 auth
    EIP-712 message as the user and call /auth/derive-api-key.

    Returns py_clob_client_v2.clob_types.ApiCreds on success.
    Caches results for 1 hour.
    """
    from polymarket.clob_client_v2 import ApiCreds  # noqa: PLC0415

    cache_key = str(user_eoa or "").strip().lower()
    now = time.time()

    with _user_creds_lock:
        cached = _user_creds_cache.get(cache_key)
        if cached and now < cached[1]:
            return cached[0]

    # Build CLOB L1 auth typed data for the user EOA.
    # ClobAuth EIP-712: domain name="ClobAuthDomain", version="1"
    # struct ClobAuth { address: string; timestamp: string; nonce: string; message: string }
    ts = int(now)
    nonce = 0
    _msg_to_sign = "This message attests that I control the given wallet"
    user_addr = str(user_eoa or "").strip()

    typed_data_l1 = {
        "primaryType": "ClobAuth",
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address", "type": "string"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "string"},
                {"name": "message", "type": "string"},
            ],
        },
        "domain": {
            "name": "ClobAuthDomain",
            "version": "1",
            "chainId": 137,
        },
        "message": {
            "address": user_addr,
            "timestamp": str(ts),
            "nonce": str(nonce),
            "message": _msg_to_sign,
        },
    }

    try:
        from scripts.dashboard_routes.redeem_withdraw_routes import (  # noqa: PLC0415
            _privy_sign_safe_typed_data,
        )
        signature = _privy_sign_safe_typed_data(
            privy_wallet_id, typed_data_l1, auth_keys=auth_keys
        )
    except Exception as exc:
        logger.warning(
            "[FundFlow][poly1271][derive-creds] eth_signTypedData_v4 for L1 auth failed: %s; "
            "trying secp256k1_sign fallback",
            exc,
        )
        # Fallback: compute the digest manually and use secp256k1_sign
        try:
            from eth_account.messages import encode_typed_data as _etd  # noqa: PLC0415
            from scripts.dashboard_routes.redeem_withdraw_routes import (  # noqa: PLC0415
                _privy_wallet_rpc,
            )
            encoded = _etd(full_message=typed_data_l1)
            # encoded.body is the raw 32-byte keccak of the typed data
            digest_hex = "0x" + encoded.body.hex()
            rpc_result = _privy_wallet_rpc(
                privy_wallet_id,
                {
                    "chain_type": "ethereum",
                    "method": "secp256k1_sign",
                    "params": {"hash": digest_hex},
                },
                auth_keys=auth_keys,
            )
            signature = str((rpc_result.get("data") or {}).get("signature") or "").strip()
        except Exception as exc2:
            raise RuntimeError(
                f"[FundFlow][poly1271] cannot sign CLOB L1 auth for {user_addr}: {exc2}"
            ) from exc2

    # Call CLOB /auth/derive-api-key with L1 headers
    import requests as _requests  # noqa: PLC0415
    headers = {
        "POLY_ADDRESS": user_addr,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": str(ts),
        "POLY_NONCE": str(nonce),
    }
    resp = _requests.get(
        "https://clob.polymarket.com/auth/derive-api-key",
        headers=headers,
        timeout=15,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"[FundFlow][poly1271] derive-api-key failed status={resp.status_code} body={resp.text[:300]}"
        )
    data = resp.json()
    creds = ApiCreds(
        api_key=str(data.get("apiKey") or data.get("api_key") or "").strip(),
        api_secret=str(data.get("secret") or "").strip(),
        api_passphrase=str(data.get("passphrase") or "").strip(),
    )
    if not creds.api_key:
        raise RuntimeError(
            f"[FundFlow][poly1271] derive-api-key returned empty apiKey body={data}"
        )

    logger.info(
        "[FundFlow][poly1271][derive-creds] derived CLOB creds for user_eoa=%s api_key=%s",
        user_addr[:10],
        creds.api_key[:8],
    )

    with _user_creds_lock:
        _user_creds_cache[cache_key] = (creds, now + _USER_CREDS_TTL)

    return creds


def get_builder_clob_client(
    deposit_wallet: str,
    user_eoa: str | None = None,
    privy_wallet_id: str | None = None,
    auth_keys: list[str] | None = None,
    user_creds: dict | None = None,
):
    """Create a ClobClient for posting deposit_wallet POLY_1271 orders.

    Per the Deposit Wallet Migration Guide:
    - POLY_ADDRESS (L2 header) = user's EOA (owner of deposit_wallet)
    - POLY_API_KEY (L2 header) = user's CLOB API key derived from their EOA
    - order.maker = order.signer = deposit_wallet address
    - signature_type = 3 (POLY_1271)

    Credential resolution order (T885 fix, 2026-05-07):
    1. ``user_creds`` (dict with api_key/api_secret/api_passphrase) — derived
       once via browser ClobAuth signature and persisted per-user. Preferred.
    2. ``user_eoa`` + ``privy_wallet_id`` → Privy server-side derive (legacy).
       This path is blocked by Privy ``policy_violation`` for embedded wallets
       in the current production environment, so it is only a fallback for
       legacy Privy-only deployments.
    3. Server EOA creds — guaranteed CLOB rejection for deposit_wallet orders;
       only kept so the client object can still be constructed for diagnostics.
    """
    from polymarket.clob_client_v2 import ClobClient, ApiCreds  # noqa: PLC0415
    from polymarket.builder_signing_sdk import BuilderConfig  # noqa: PLC0415

    builder_config = BuilderConfig(
        builderCode=_normalize_builder_code(os.getenv("POLY_BUILDER_CODE")),
    )

    _user_eoa = str(user_eoa or "").strip()
    _privy_wid = str(privy_wallet_id or "").strip()

    # T885 fix path: caller supplied browser-derived per-user creds. Use them
    # directly without touching Privy.
    _uc_key = str((user_creds or {}).get("api_key") or "").strip() if user_creds else ""
    _uc_sec = str((user_creds or {}).get("api_secret") or "").strip() if user_creds else ""
    _uc_pass = str((user_creds or {}).get("api_passphrase") or "").strip() if user_creds else ""
    if _uc_key and _uc_sec and _uc_pass:
        _api_key = _uc_key
        _api_secret = _uc_sec
        _api_passphrase = _uc_pass
        _signer_key = str(os.getenv("POLY_PRIVATE_KEY") or "").strip()
        logger.info(
            "[FundFlow][poly1271][client-init] user_eoa=%s deposit_wallet=%s "
            "api_key=%s builder_code=%s [browser-derived-creds]",
            (_user_eoa or "")[:10],
            str(deposit_wallet or "")[:10],
            _api_key[:8],
            _normalize_builder_code(os.getenv("POLY_BUILDER_CODE"))[:12],
        )
    elif _user_eoa and _privy_wid:
        # Derive per-user CLOB credentials so POLY_ADDRESS = user's EOA
        try:
            user_creds = derive_user_clob_creds(_user_eoa, _privy_wid, auth_keys)
            _api_key = user_creds.api_key
            _api_secret = user_creds.api_secret
            _api_passphrase = user_creds.api_passphrase
            _signer_key = str(os.getenv("POLY_PRIVATE_KEY") or "").strip()
            # For per-user auth we sign L2 HMAC as the SERVER key (0x1538)
            # but POLY_ADDRESS is set to user EOA via custom override below.
            logger.info(
                "[FundFlow][poly1271][client-init] user_eoa=%s deposit_wallet=%s "
                "api_key=%s builder_code=%s [user-creds]",
                _user_eoa[:10],
                str(deposit_wallet or "")[:10],
                _api_key[:8],
                _normalize_builder_code(os.getenv("POLY_BUILDER_CODE"))[:12],
            )
        except Exception as exc:
            logger.error(
                "[FundFlow][poly1271][client-init] derive_user_clob_creds failed for "
                "user_eoa=%s: %s — falling back to server creds (will likely fail)",
                _user_eoa[:10],
                exc,
            )
            _api_key = str(os.getenv("POLY_API_KEY") or "").strip()
            _api_secret = str(os.getenv("POLY_SECRET") or "").strip()
            _api_passphrase = str(os.getenv("POLY_PASSPHRASE") or "").strip()
            _signer_key = str(os.getenv("POLY_PRIVATE_KEY") or "").strip()
    else:
        # No user EOA provided — server creds fallback (will fail for deposit_wallet)
        _api_key = str(os.getenv("POLY_API_KEY") or "").strip()
        _api_secret = str(os.getenv("POLY_SECRET") or "").strip()
        _api_passphrase = str(os.getenv("POLY_PASSPHRASE") or "").strip()
        _signer_key = str(os.getenv("POLY_PRIVATE_KEY") or "").strip()
        logger.warning(
            "[FundFlow][poly1271][client-init] no user_eoa/privy_wallet_id — "
            "using server creds (will be rejected by CLOB for deposit_wallet orders)"
        )

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=_signer_key,
        creds=ApiCreds(
            api_key=_api_key,
            api_secret=_api_secret,
            api_passphrase=_api_passphrase,
        ),
        signature_type=3,
        funder=deposit_wallet,
        builder_config=builder_config,
    )

    # CRITICAL: override signer.address() so that create_level_2_headers emits
    # POLY_ADDRESS = user's EOA (owner of deposit_wallet), not server EOA.
    # CLOB validates: deposit_wallet.owner() == POLY_ADDRESS; without this
    # override the header still contains the server EOA (0x1538) and CLOB
    # rejects with "the order signer address has to be the address of the API KEY".
    if _user_eoa:
        _addr_ref = _user_eoa  # capture for lambda closure
        client.signer.address = lambda: _addr_ref  # type: ignore[method-assign]
        logger.debug(
            "[FundFlow][poly1271][client-init] patched signer.address → user_eoa=%s",
            _user_eoa[:10],
        )

    return client


# Backwards alias for existing imports.
build_builder_clob_client = get_builder_clob_client


def is_live() -> bool:
    """T884 (2026-05-07) — emergency cutover: always live.

    Previously gated by DEPOSIT_WALLET_ORDERING_LIVE env. Per emergency
    production directive, deposit_wallet ordering is now permanently
    enabled; users without working deposit_wallet order flow cannot trade.
    """

    return True


def _map_side(side: OrderSide) -> int:
    # py_clob_client_v2.order_utils.model.side.Side: BUY=0, SELL=1
    return 0 if side == OrderSide.BUY else 1


def _normalize_addr(addr: str) -> str:
    s = str(addr or "").strip()
    if not _ADDR_RE.match(s):
        raise ValueError(f"invalid address: {addr!r}")
    return s.lower()


def _snap_price_size(price: float, size: float) -> tuple[float, float, int, int]:
    """Polymarket precision: maker amount must land on integer cents.

    Returns (snapped_price, snapped_size, p_cents, size_step_cents).
    Mirrors V2BuilderAdapter.place_order T299 logic so [FundFlow] paths
    behave identically to legacy SAFE ordering.
    """
    p_cents = int(round(float(price) * 100))
    if p_cents <= 0 or p_cents >= 100:
        raise ValueError(f"price_out_of_range: {price}")
    snapped_price = p_cents / 100.0
    g = gcd(p_cents, 100)
    size_step_cents = 100 // g
    requested_size_cents = _floor(float(size) * 100 + 1e-9)
    final_size_cents = (requested_size_cents // size_step_cents) * size_step_cents
    if final_size_cents <= 0:
        raise ValueError(
            f"size_too_small_after_precision_snap: price={snapped_price} "
            f"requires size multiple of {size_step_cents/100:.2f}"
        )
    snapped_size = final_size_cents / 100.0
    if (p_cents * final_size_cents) % 100 != 0:
        raise ValueError("internal_precision_check_failed")
    return snapped_price, snapped_size, p_cents, size_step_cents


def _build_order_v2(
    *,
    request: ExecutionOrderRequest,
    maker: str,
    signer: str,
    builder_code: str,
    metadata_hex: str,
) -> Any:
    """Construct an unsigned OrderV2 (signatureType=3) ready for typed-data signing.

    Returns py_clob_client_v2.order_utils.model.order_data_v2.OrderV2.
    """
    from py_clob_client_v2.order_utils import generate_order_salt  # noqa: PLC0415
    from py_clob_client_v2.order_utils.model.order_data_v2 import OrderV2  # noqa: PLC0415

    snapped_price, snapped_size, p_cents, _ = _snap_price_size(request.price, request.size)
    side_int = _map_side(request.side)

    # Compute maker/taker amounts in token decimals (6 dp for both pUSD & shares).
    # For BUY: makerAmount = USDC, takerAmount = shares.
    # For SELL: makerAmount = shares, takerAmount = USDC.
    #
    # T906 FIX (2026-05-11): the previous formula
    #   usdc_cents = p_cents * final_size_cents
    #   usdc_units = usdc_cents * 10_000
    # produced amounts 100× too large, because `p_cents * final_size_cents` is
    # (cents/share)·(0.01-shares) = $0.0001-units, not actual cents.  The
    # CLOB server then derived price = makerAmount/takerAmount = 100× the real
    # price (e.g. 20.0 instead of 0.20) and rejected with
    #   400 {"error":"invalid price, price must be greater than 0 and less than 1"}.
    # Correct formula matches py_clob_client builder.py:
    #   raw_maker(BUY) = size · price          (USDC, in dollars)
    #   raw_taker(BUY) = size                  (shares)
    #   token decimals = 6 (both pUSD and shares ERC1155).
    final_size_cents = int(round(snapped_size * 100))
    # value in $0.0001 units; precision check in _snap_price_size guarantees
    # this is divisible by 100, i.e. it represents a whole number of cents.
    usdc_micro = p_cents * final_size_cents
    usdc_units = usdc_micro * 100  # $0.0001-units → 6dp USDC units (×1e6/$1)
    shares_units = final_size_cents * 10_000  # 0.01 shares → 6dp units

    if request.side == OrderSide.BUY:
        maker_amount = usdc_units
        taker_amount = shares_units
    else:
        maker_amount = shares_units
        taker_amount = usdc_units

    return OrderV2(
        salt=str(generate_order_salt()),
        maker=maker,
        signer=signer,
        tokenId=str(request.token_id),
        makerAmount=str(maker_amount),
        takerAmount=str(taker_amount),
        side=side_int,
        signatureType=SIGNATURE_TYPE_POLY_1271,  # int 3, accepted by the API
        timestamp=str(time.time_ns() // 1_000_000),
        metadata=metadata_hex or _ZERO_BYTES32,
        builder=builder_code or _ZERO_BYTES32,
        expiration="0",
    )


def _build_typed_data(order: Any, *, chain_id: int, verifying_contract: str) -> dict:
    """Replicate ExchangeOrderBuilderV2.build_order_typed_data without a signer."""
    from py_clob_client_v2.order_utils.model.ctf_exchange_v2_typed_data import (  # noqa: PLC0415
        CTF_EXCHANGE_V2_DOMAIN_NAME,
        CTF_EXCHANGE_V2_DOMAIN_VERSION,
        CTF_EXCHANGE_V2_ORDER_STRUCT,
        EIP712_DOMAIN,
    )

    def _hex_to_int_bytes32(hex_str: str) -> str:
        # Privy expects EIP-712 bytes32 fields as 0x-prefixed hex strings; the
        # SDK's local signer converts to raw bytes via _hex_to_bytes32 — Privy
        # accepts the hex form directly.
        s = str(hex_str or "").strip()
        if not s.startswith("0x"):
            s = "0x" + s
        # zfill to 66 chars (0x + 64)
        return "0x" + s[2:].zfill(64)

    return {
        "primaryType": "Order",
        "types": {
            "EIP712Domain": EIP712_DOMAIN,
            "Order": CTF_EXCHANGE_V2_ORDER_STRUCT,
        },
        "domain": {
            "name": CTF_EXCHANGE_V2_DOMAIN_NAME,
            "version": CTF_EXCHANGE_V2_DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": verifying_contract,
        },
        "message": {
            "salt": int(order.salt),
            "maker": order.maker,
            "signer": order.signer,
            "tokenId": int(order.tokenId),
            "makerAmount": int(order.makerAmount),
            "takerAmount": int(order.takerAmount),
            "side": int(order.side),
            "signatureType": int(order.signatureType),
            "timestamp": int(order.timestamp),
            "metadata": _hex_to_int_bytes32(order.metadata),
            "builder": _hex_to_int_bytes32(order.builder),
        },
    }


def place_order_poly1271(
    *,
    client: Any,
    request: ExecutionOrderRequest,
    maker_deposit_wallet: str,
    signer_eoa: str,
    privy_wallet_id: str,
    privy_auth_keys: list[str] | None,
    builder_code: str,
    metadata_hex: str,
    neg_risk: bool | None = None,
    chain_id: int = 137,
) -> ExecutionOrderResult:
    """Place a CLOB order with signature_type=3 (POLY_1271 / deposit_wallet).

    Args:
        client: py_clob_client_v2.ClobClient (already initialised).
        request: ExecutionOrderRequest (price/size/side/token_id/...).
        maker_deposit_wallet: deposit_wallet contract address (the maker).
        signer_eoa: user's Privy embedded EOA address (the signer field).
        privy_wallet_id: Privy embedded wallet UUID for server-signer RPC.
        privy_auth_keys: optional Privy authorization keys (PEM JWT list).
        builder_code: bytes32 builder code.
        metadata_hex: bytes32 metadata.
        neg_risk: True for NegRisk markets (different verifying contract).
        chain_id: Polygon mainnet (137) or Amoy testnet (80002).

    Returns: ExecutionOrderResult (success / error / order_id).
    """
    if not is_live():
        return ExecutionOrderResult(
            success=False,
            error="deposit_wallet_ordering_disabled",
            raw_response={
                "hint": "Set DEPOSIT_WALLET_ORDERING_LIVE=1 after testnet validation.",
                "wallet_type": "deposit_wallet",
                "signature_type": SIGNATURE_TYPE_POLY_1271,
            },
        )

    try:
        maker = _normalize_addr(maker_deposit_wallet)
        signer = _normalize_addr(signer_eoa)
    except ValueError as exc:
        return ExecutionOrderResult(success=False, error=f"invalid_address: {exc}")

    if not privy_wallet_id:
        return ExecutionOrderResult(
            success=False,
            error="missing_privy_wallet_id",
            raw_response={"hint": "user must complete Privy embedded wallet enrollment"},
        )

    try:
        # Resolve neg_risk (default to SDK's market metadata if not supplied).
        if neg_risk is None:
            try:
                neg_risk = bool(client.get_neg_risk(request.token_id))
            except Exception:
                neg_risk = False
        verifying_contract = (
            NEG_RISK_EXCHANGE_V2 if neg_risk else CTF_EXCHANGE_V2
        )
        try:
            from eth_utils import to_checksum_address  # noqa: PLC0415

            verifying_contract = to_checksum_address(verifying_contract)
        except Exception:
            pass

        order = _build_order_v2(
            request=request,
            maker=maker,
            signer=signer,
            builder_code=builder_code,
            metadata_hex=metadata_hex,
        )

        typed_data = _build_typed_data(
            order, chain_id=chain_id, verifying_contract=verifying_contract
        )

        logger.info(
            "[FundFlow][poly1271] place_order maker=%s signer=%s token=%s "
            "side=%s price=%.4f size=%.2f neg_risk=%s",
            maker[:10],
            signer[:10],
            str(request.token_id)[:12],
            request.side.value,
            float(request.price),
            float(request.size),
            neg_risk,
        )

        from scripts.dashboard_routes.redeem_withdraw_routes import (  # noqa: PLC0415
            _privy_sign_safe_typed_data,
            _privy_wallet_rpc,
        )

        signature = ""
        primary_exc: Exception | None = None
        try:
            signature = _privy_sign_safe_typed_data(
                privy_wallet_id, typed_data, auth_keys=privy_auth_keys
            )
        except Exception as exc:
            primary_exc = exc
            msg = str(exc).lower()
            if (
                "no valid authorization keys" in msg
                or "user signing keys" in msg
                or "401" in msg
                or "403" in msg
            ):
                logger.warning(
                    "[FundFlow][poly1271] eth_signTypedData_v4 blocked by Privy policy; "
                    "falling back to secp256k1_sign on digest"
                )
                try:
                    from eth_account.messages import encode_typed_data  # noqa: PLC0415

                    encoded = encode_typed_data(full_message=typed_data)
                    digest_bytes = encoded.body
                    digest_hex = "0x" + digest_bytes.hex()
                    rpc_result = _privy_wallet_rpc(
                        privy_wallet_id,
                        {
                            "chain_type": "ethereum",
                            "method": "secp256k1_sign",
                            "params": {"hash": digest_hex},
                        },
                        auth_keys=privy_auth_keys,
                    )
                    sig_field = (rpc_result.get("data") or {}).get("signature") or ""
                    signature = str(sig_field).strip()
                    if signature and not signature.startswith("0x"):
                        signature = "0x" + signature
                except Exception as fallback_exc:
                    logger.error(
                        "[FundFlow][poly1271] secp256k1_sign fallback also failed: %s",
                        fallback_exc,
                    )
                    raise primary_exc from fallback_exc
            else:
                raise
        if not signature or not signature.startswith("0x"):
            return ExecutionOrderResult(
                success=False,
                error="privy_signing_failed",
                raw_response={"signature": signature or ""},
            )

        from py_clob_client_v2 import OrderType  # noqa: PLC0415
        from py_clob_client_v2.order_utils.model.order_data_v2 import (  # noqa: PLC0415
            SignedOrderV2,
        )

        import dataclasses  # noqa: PLC0415

        signed_order = SignedOrderV2(
            **{**dataclasses.asdict(order), "signature": signature}
        )

        logger.info(
            "[FundFlow][poly1271][manual-post] maker=%s signer=%s sig_type=%s token=%s",
            str(order.maker)[:10],
            str(order.signer)[:10],
            str(order.signatureType),
            str(order.tokenId)[:12],
        )

        ot_value = (request.order_type.value if hasattr(request.order_type, "value")
                    else str(request.order_type)).upper()
        order_type = {
            "GTC": OrderType.GTC,
            "FOK": OrderType.FOK,
            "FAK": OrderType.FAK,
            "GTD": OrderType.GTD,
        }.get(ot_value, OrderType.FOK)

        resp = client.post_order(signed_order, order_type=order_type)

        if resp and isinstance(resp, dict):
            order_id = (
                resp.get("orderID") or resp.get("id") or resp.get("order_id")
            )
            status = resp.get("status", "unknown")
            success = status not in ("error", "failed") and order_id is not None
            if success:
                logger.info(
                    "[FundFlow][poly1271] order accepted id=%s status=%s",
                    str(order_id)[:20],
                    status,
                )
            else:
                logger.warning(
                    "[FundFlow][poly1271] order rejected status=%s body=%s",
                    status,
                    str(resp)[:200],
                )
            return ExecutionOrderResult(
                success=success,
                order_id=order_id,
                status=status,
                raw_response=resp,
            )

        logger.warning("[FundFlow][poly1271] unexpected response shape: %r", resp)
        return ExecutionOrderResult(
            success=True,
            raw_response=resp if isinstance(resp, dict) else {"raw": str(resp)},
        )

    except Exception as exc:
        logger.exception("[FundFlow][poly1271] place_order failed: %s", exc)
        return ExecutionOrderResult(success=False, error=str(exc))


# =============================================================================
# T884.3 — Browser-side signing (nested EIP-712 TypedDataSign workflow)
# =============================================================================
#
# Privy server-signer policy currently denies BOTH ``eth_signTypedData_v4``
# and ``secp256k1_sign`` for newly-enrolled embedded wallets, so server-side
# signing of POLY_1271 orders is not feasible. The DepositWallet contract
# (Solady ERC-1271 + custom overrides) verifies signatures via plain ECDSA
# against the user's owner EOA, going through Solady's nested EIP-712
# TypedDataSign workflow:
#
#   final_digest = keccak256(0x1901 || APP_DOMAIN_SEP || hashStruct(TypedDataSign))
#   hashStruct(TypedDataSign) = keccak256(
#       typedDataSignTypehash ||
#       contents(=Order struct hash) ||
#       keccak256("DepositWallet") ||
#       keccak256("1") ||
#       chainId ||
#       depositWalletAddress ||
#       bytes32(0)               // salt
#   )
#   typedDataSignTypehash = keccak256(
#       "TypedDataSign(Order contents,string name,string version,uint256 chainId,address verifyingContract,bytes32 salt)"
#       + "Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)"
#   )
#
# This is producible by a standard ``eth_signTypedData_v4`` invocation when the
# signer is the user's Privy embedded EOA invoked client-side (uses the user's
# own session, not a server signing key). The 65-byte ECDSA signature is
# returned to the backend, which appends Solady's required envelope:
#
#   envelope = APP_DOMAIN_SEP(32) || contents(32) || contentsType(N) || uint16(N)
#
# producing a 65 + 64 + len(contentsType) + 2 byte signature that the contract
# auto-detects as a TypedDataSign nested envelope.
# -----------------------------------------------------------------------------

import hashlib  # noqa: E402

# Order EIP-712 type string (keep in lockstep with
# py_clob_client_v2/order_utils/model/ctf_exchange_v2_typed_data.py).
ORDER_CONTENTS_TYPE = (
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
)

TYPED_DATA_SIGN_TYPE = (
    "TypedDataSign(Order contents,string name,string version,"
    "uint256 chainId,address verifyingContract,bytes32 salt)" + ORDER_CONTENTS_TYPE
)

EIP712_DOMAIN_TYPE = (
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)


def _keccak(data: bytes) -> bytes:
    """keccak-256 (use pycryptodome if available, else eth_utils)."""
    try:
        from eth_utils import keccak as _k  # noqa: PLC0415

        return _k(data)
    except Exception:
        # Fall back to hashlib's keccak (Python 3.6+ via pysha3 only)
        h = hashlib.new("sha3_256")
        h.update(data)
        return h.digest()


def _addr_to_bytes32(addr: str) -> bytes:
    s = str(addr or "").lower().strip()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 40:
        raise ValueError(f"invalid address: {addr!r}")
    return bytes(12) + bytes.fromhex(s)


def _uint_to_bytes32(value: int | str) -> bytes:
    return int(value).to_bytes(32, "big", signed=False)


def _bytes32_field(value: str) -> bytes:
    s = str(value or "").strip()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) > 64:
        raise ValueError(f"bytes32 too long: {value!r}")
    return bytes.fromhex(s.rjust(64, "0"))


def _hash_order_struct(order: Any) -> bytes:
    """keccak256(typehash || encoded fields) for the Order struct."""
    type_hash = _keccak(ORDER_CONTENTS_TYPE.encode("utf-8"))
    encoded = b"".join(
        [
            type_hash,
            _uint_to_bytes32(int(order.salt)),
            _addr_to_bytes32(order.maker),
            _addr_to_bytes32(order.signer),
            _uint_to_bytes32(int(order.tokenId)),
            _uint_to_bytes32(int(order.makerAmount)),
            _uint_to_bytes32(int(order.takerAmount)),
            _uint_to_bytes32(int(order.side)),
            _uint_to_bytes32(int(order.signatureType)),
            _uint_to_bytes32(int(order.timestamp)),
            _bytes32_field(order.metadata),
            _bytes32_field(order.builder),
        ]
    )
    return _keccak(encoded)


def _app_domain_separator(*, chain_id: int, verifying_contract: str) -> bytes:
    """EIP-712 domain separator for the Polymarket CTF Exchange (the app)."""
    type_hash = _keccak(EIP712_DOMAIN_TYPE.encode("utf-8"))
    name_hash = _keccak(b"Polymarket CTF Exchange")
    version_hash = _keccak(b"2")  # CTF_EXCHANGE_V2_DOMAIN_VERSION
    encoded = (
        type_hash
        + name_hash
        + version_hash
        + _uint_to_bytes32(chain_id)
        + _addr_to_bytes32(verifying_contract)
    )
    return _keccak(encoded)


def build_browser_signing_payload(
    *,
    request: ExecutionOrderRequest,
    maker_deposit_wallet: str,
    signer_eoa: str,
    builder_code: str,
    metadata_hex: str,
    neg_risk: bool,
    chain_id: int = 137,
) -> dict:
    """Build the typed-data + envelope materials for browser-side signing.

    ``signer_eoa`` MUST be the deposit_wallet address (same as maker).
    Per POLY_1271 spec: maker == signer == deposit_wallet_address.
    The Builder API Key (POLY_ADDRESS header = server EOA) is the builder's
    own authentication, completely separate from the order signer field.

    Returns a dict with:
      - ``order``: serialised OrderV2 (jsonable strings) — will be reconstructed
        on submit-signed.
      - ``typed_data``: payload for ``eth_signTypedData_v4`` in the browser.
      - ``app_domain_separator``: 32-byte hex (no 0x) — to append after sig.
      - ``contents_hash``: 32-byte hex (no 0x) — Order struct hash.
      - ``contents_type``: ASCII string — appended after contents.
      - ``verifying_contract``: CTF Exchange address used for app domain.
      - ``deposit_wallet``: account address used as TypedDataSign domain.
      - ``signer_eoa``: the server EOA address used as order.signer.
    """
    from eth_utils import to_checksum_address  # noqa: PLC0415

    maker = _normalize_addr(maker_deposit_wallet)
    signer = _normalize_addr(signer_eoa)
    verifying_contract = to_checksum_address(
        NEG_RISK_EXCHANGE_V2 if neg_risk else CTF_EXCHANGE_V2
    )
    deposit_wallet_cs = to_checksum_address(maker)

    order = _build_order_v2(
        request=request,
        maker=maker,
        signer=signer,
        builder_code=builder_code,
        metadata_hex=metadata_hex,
    )

    contents_hash = _hash_order_struct(order)
    app_dom_sep = _app_domain_separator(
        chain_id=chain_id, verifying_contract=verifying_contract
    )

    # Standard EIP-712 typed data — viem/Privy will hash it as:
    #   keccak256(0x1901 || domainSeparator(=app_dom_sep) || hashStruct(TypedDataSign))
    # which is exactly what Solady's TypedDataSign workflow recovers from.
    typed_data = {
        "primaryType": "TypedDataSign",
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TypedDataSign": [
                {"name": "contents", "type": "Order"},
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
                {"name": "salt", "type": "bytes32"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
                {"name": "timestamp", "type": "uint256"},
                {"name": "metadata", "type": "bytes32"},
                {"name": "builder", "type": "bytes32"},
            ],
        },
        "domain": {
            "name": "Polymarket CTF Exchange",
            "version": "2",
            "chainId": chain_id,
            "verifyingContract": verifying_contract,
        },
        "message": {
            "contents": {
                "salt": str(order.salt),
                "maker": maker,
                "signer": signer,
                "tokenId": str(order.tokenId),
                "makerAmount": str(order.makerAmount),
                "takerAmount": str(order.takerAmount),
                "side": int(order.side),
                "signatureType": int(order.signatureType),
                "timestamp": str(order.timestamp),
                "metadata": "0x" + _bytes32_field(order.metadata).hex(),
                "builder": "0x" + _bytes32_field(order.builder).hex(),
            },
            "name": "DepositWallet",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": deposit_wallet_cs,
            "salt": "0x" + ("00" * 32),
        },
    }

    return {
        "order": {
            "salt": str(order.salt),
            "maker": maker,
            "signer": signer,
            "tokenId": str(order.tokenId),
            "makerAmount": str(order.makerAmount),
            "takerAmount": str(order.takerAmount),
            "side": int(order.side),
            "signatureType": int(order.signatureType),
            "timestamp": str(order.timestamp),
            "metadata": "0x" + _bytes32_field(order.metadata).hex(),
            "builder": "0x" + _bytes32_field(order.builder).hex(),
            "expiration": str(order.expiration),
        },
        "typed_data": typed_data,
        "app_domain_separator": app_dom_sep.hex(),
        "contents_hash": contents_hash.hex(),
        "contents_type": ORDER_CONTENTS_TYPE,
        "verifying_contract": verifying_contract,
        "deposit_wallet": deposit_wallet_cs,
        "signer_eoa": signer,
        "neg_risk": neg_risk,
    }


def assemble_signed_envelope(
    *,
    signature_hex: str,
    app_domain_separator_hex: str,
    contents_hash_hex: str,
    contents_type: str,
) -> str:
    """Build Solady-compatible nested EIP-712 envelope.

    Layout: ``rsv (65) || appDomainSep (32) || contents (32) || contentsType (N) || uint16(N)``.
    Returns ``0x`` prefixed hex string.
    """
    sig = str(signature_hex or "").strip()
    if sig.startswith("0x"):
        sig = sig[2:]
    if len(sig) != 130:
        raise ValueError(f"invalid signature length: {len(sig)//2}")
    sig_bytes = bytes.fromhex(sig)
    # Solady accepts v in {27,28} — viem normalises to that already.
    v = sig_bytes[64]
    if v in (0, 1):
        sig_bytes = sig_bytes[:64] + bytes([v + 27])
    elif v not in (27, 28):
        raise ValueError(f"unexpected signature v={v}")

    app_sep = bytes.fromhex(str(app_domain_separator_hex).removeprefix("0x"))
    contents = bytes.fromhex(str(contents_hash_hex).removeprefix("0x"))
    if len(app_sep) != 32 or len(contents) != 32:
        raise ValueError("app_domain_separator and contents must be 32 bytes each")
    ct_bytes = contents_type.encode("ascii")
    if len(ct_bytes) >= (1 << 16):
        raise ValueError("contentsType too long for uint16 length suffix")
    suffix_len = len(ct_bytes).to_bytes(2, "big")
    envelope = sig_bytes + app_sep + contents + ct_bytes + suffix_len
    return "0x" + envelope.hex()


def submit_signed_order_envelope(
    *,
    client: Any,
    order_dict: dict,
    full_signature_hex: str,
    order_type_str: str,
) -> ExecutionOrderResult:
    """Reconstruct SignedOrderV2 with browser-supplied envelope and POST to CLOB.

    ``full_signature_hex`` is the output of :func:`assemble_signed_envelope`
    (raw 65-byte ECDSA + Solady envelope).
    """
    from py_clob_client_v2 import OrderType  # noqa: PLC0415
    from py_clob_client_v2.order_utils.model.order_data_v2 import (  # noqa: PLC0415
        SignedOrderV2,
    )

    ot_value = str(order_type_str or "FOK").upper()
    order_type = {
        "GTC": OrderType.GTC,
        "FOK": OrderType.FOK,
        "FAK": OrderType.FAK,
        "GTD": OrderType.GTD,
    }.get(ot_value, OrderType.FOK)

    signed_order = SignedOrderV2(
        salt=str(order_dict["salt"]),
        maker=str(order_dict["maker"]),
        signer=str(order_dict["signer"]),
        tokenId=str(order_dict["tokenId"]),
        makerAmount=str(order_dict["makerAmount"]),
        takerAmount=str(order_dict["takerAmount"]),
        side=int(order_dict["side"]),
        signatureType=int(order_dict["signatureType"]),
        timestamp=str(order_dict["timestamp"]),
        metadata=str(order_dict["metadata"]),
        builder=str(order_dict["builder"]),
        expiration=str(order_dict.get("expiration") or "0"),
        signature=full_signature_hex,
    )

    try:
        resp = client.post_order(signed_order, order_type=order_type)
    except Exception as exc:
        logger.exception("[FundFlow][poly1271] post_order failed: %s", exc)
        return ExecutionOrderResult(success=False, error=str(exc))

    if resp and isinstance(resp, dict):
        order_id = resp.get("orderID") or resp.get("id") or resp.get("order_id")
        status = resp.get("status", "unknown")
        success = status not in ("error", "failed") and order_id is not None
        if success:
            logger.info(
                "[FundFlow][poly1271][browser-sign] accepted id=%s status=%s",
                str(order_id)[:20],
                status,
            )
        else:
            logger.warning(
                "[FundFlow][poly1271][browser-sign] rejected status=%s body=%s",
                status,
                str(resp)[:200],
            )
        return ExecutionOrderResult(
            success=success,
            order_id=order_id,
            status=status,
            raw_response=resp,
        )
    logger.warning(
        "[FundFlow][poly1271][browser-sign] unexpected resp: %r", resp
    )
    return ExecutionOrderResult(
        success=True,
        raw_response=resp if isinstance(resp, dict) else {"raw": str(resp)},
    )


__all__ = [
    "is_live",
    "get_builder_clob_client",
    "build_builder_clob_client",
    "place_order_poly1271",
    "build_browser_signing_payload",
    "assemble_signed_envelope",
    "submit_signed_order_envelope",
    "ORDER_CONTENTS_TYPE",
]
