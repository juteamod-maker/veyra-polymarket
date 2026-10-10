"""
V2BuilderAdapter — ExecutionGateway implementation using py-clob-client-v2.

Key V2 changes handled by this adapter:
  1. Keyword-only constructor (positional args removed in V2 SDK).
  2. builderCode injected on every order (from env POLY_BUILDER_CODE or per-order override).
  3. metadata field (bytes32) injected with tracing/attribution string.
  4. create_and_post_order() — combined create+submit (replaces V1 create_order + post_order).
  5. Side enum (Side.BUY / Side.SELL) instead of V1 string constants.
  6. create_or_derive_api_key() — singular (V1 used create_or_derive_api_creds).
  7. pUSD collateral — balance query via get_balance_allowance() still available in V2.

V2 test endpoint: https://clob-v2.polymarket.com
Production URL (post April 28 cutover): https://clob.polymarket.com  (same URL, V2 takes over)

This class is the ONLY place in V2 codebase that imports py_clob_client_v2.
All business code must go through ExecutionGateway.
"""

from __future__ import annotations

import logging
import math
import os
import time
from threading import Lock
from typing import Any

from src.executor.execution_gateway import (
    BalanceInfo,
    ExecutionGateway,
    ExecutionOrderRequest,
    ExecutionOrderResult,
    ExecutionOrderType,
    OrderSide,
)
from src.secrets import get_secret
from src.utils.database import connect as _pg_connect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CHAIN_ID = 137  # Polygon mainnet
_CHAIN_ID_TESTNET = 80002  # Amoy testnet
_ZERO_BYTES32 = "0x" + "0" * 64  # Default empty bytes32

# V2 exchange verifyingContract addresses (EIP-712 domain version "2")
_V2_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
_V2_NEG_RISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"


def _encode_metadata(raw: str | None) -> str:
    """Encode a metadata string to bytes32 hex.

    Rules:
      - If raw is None or empty → return zero bytes32.
      - If raw is already 0x...(64 hex chars) → return as-is.
      - Otherwise: UTF-8 encode, left-pad with zeros to 32 bytes, return 0x hex.
        Strings longer than 32 bytes are truncated (keep last 32 bytes for recency).
    """
    if not raw:
        return _ZERO_BYTES32
    raw = raw.strip()
    if raw.startswith("0x") and len(raw) == 66:
        return raw
    # Encode to bytes, truncate/pad to 32 bytes
    encoded = raw.encode("utf-8")[-32:]
    padded = encoded.rjust(32, b"\x00")
    return "0x" + padded.hex()


class V2BuilderAdapter(ExecutionGateway):
    """ExecutionGateway backed by py-clob-client-v2.

    Thread-safe: uses a lock to protect lazy client initialisation.
    One instance per tenant (single-tenant MVP: one global instance).

    Usage:
        gw = V2BuilderAdapter(host="https://clob-v2.polymarket.com")
        result = gw.place_order(ExecutionOrderRequest(
            token_id="...",
            price=0.45,
            size=10,
            side=OrderSide.BUY,
        ))
    """

    def __init__(
        self,
        host: str,
        *,
        chain_id: int = _CHAIN_ID,
        use_testnet: bool = False,
        funder: str | None = None,
    ) -> None:
        """Initialize a V2BuilderAdapter.

        Args:
            host: CLOB API host (e.g. https://clob.polymarket.com).
            chain_id: Polygon chain id (137 mainnet, 80002 amoy).
            use_testnet: If True, force chain_id to 80002.
            funder: Per-instance Gnosis Safe funder address. When provided,
                overrides the POLY_RELAYER_ADDRESS env var. This enables the
                Wave 4 ClobClientFactory pattern where each user's Safe
                address gets its own adapter instance for signature_type=2
                proxy-wallet ordering.
        """
        if use_testnet:
            chain_id = _CHAIN_ID_TESTNET
        self._host = host
        self._chain_id = chain_id
        self._funder_override = (funder or "").strip().lower() or None
        self._client: Any = None
        self._lock = Lock()
        self._healthy = True

    # T288 Wave 4: expose the resolved funder for diagnostics / factory caching
    @property
    def funder_address(self) -> str | None:
        return self._funder_override

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Lazily initialise and return the py_clob_client_v2 ClobClient.

        Raises ImportError if py-clob-client-v2 is not installed.
        Raises EnvironmentError if required env vars are missing.
        """
        with self._lock:
            if self._client is not None:
                return self._client

            try:
                from py_clob_client_v2 import ApiCreds, ClobClient  # noqa: PLC0415
            except ImportError as exc:
                raise ImportError(
                    "py-clob-client-v2 is not installed. "
                    "Run: pip install py-clob-client-v2"
                ) from exc

            private_key = _require("POLY_PRIVATE_KEY")
            api_key = _require("POLY_API_KEY")
            api_secret = _require("POLY_SECRET")
            api_passphrase = _require("POLY_PASSPHRASE")
            # T288 Wave 4: funder priority
            #   1. constructor override (per-user via factory)
            #   2. POLY_RELAYER_ADDRESS env (system-wide auto-trading)
            #   3. fallback to latest bound dashboard wallet (legacy)
            funder = self._funder_override or _require("POLY_RELAYER_ADDRESS")

            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )

            # V2 constructor uses keyword args only.
            # signature_type=2 = proxy/Gnosis-Safe wallet (same as V1).
            # funder = the address that holds funds (proxy wallet funder).
            self._client = ClobClient(
                host=self._host,
                chain_id=self._chain_id,
                key=private_key,
                creds=creds,
                signature_type=2,
                funder=funder,
            )
            logger.info(
                "V2BuilderAdapter: ClobClient initialised (chain=%s, funder=%s...)",
                self._chain_id,
                funder[:10],
            )
            return self._client

    def _resolve_builder_code(self, override: str | None) -> str:
        """Return builder code to embed on an order.

        Priority: per-order override > POLY_BUILDER_CODE env var > zero bytes32.
        """
        code = override or get_secret("POLY_BUILDER_CODE") or _ZERO_BYTES32
        # Normalise: ensure 0x-prefixed 64-char hex
        code = code.strip()
        if not code.startswith("0x"):
            code = "0x" + code
        if len(code) != 66:
            logger.warning(
                "POLY_BUILDER_CODE has unexpected length (%d chars, expected 66). "
                "Falling back to zero bytes32.",
                len(code),
            )
            code = _ZERO_BYTES32
        return code

    def _build_order_metadata(self, request: ExecutionOrderRequest) -> str:
        """Build the bytes32 metadata string for tracing/attribution.

        Format (semicolon-separated, max 32 bytes total):
          signal_id;trace_id  (or just zeros if not provided)
        """
        if request.metadata:
            return _encode_metadata(request.metadata)
        parts: list[str] = []
        if request.signal_id:
            parts.append(f"s:{request.signal_id[:12]}")
        if request.trace_id:
            parts.append(f"t:{request.trace_id[:12]}")
        raw = ";".join(parts) if parts else ""
        return _encode_metadata(raw)

    # ------------------------------------------------------------------
    # ExecutionGateway implementation
    # ------------------------------------------------------------------

    def place_order(self, request: ExecutionOrderRequest) -> ExecutionOrderResult:
        try:
            from py_clob_client_v2 import OrderArgs  # noqa: PLC0415
            from py_clob_client_v2 import OrderType, PartialCreateOrderOptions, Side

            client = self._get_client()

            side = Side.BUY if request.side == OrderSide.BUY else Side.SELL
            order_type = _map_order_type(request.order_type)

            builder_code = self._resolve_builder_code(request.builder_code)
            metadata_hex = self._build_order_metadata(request)

            # Polymarket CLOB precision rules (T299 真根因修复):
            #   服务端要求 BUY: makerAmount(USDC) <= 2 decimals, takerAmount(shares) <= 4 decimals.
            #   SDK get_order_amounts(BUY): raw_taker = round_down(size, 2dp_shares),
            #                               raw_maker = raw_taker * raw_price (可达 4 dp USDC).
            #   SDK 默认允许 maker 4 dp，但服务端只接受 2 dp → 502.
            # 真修：选取 size 使 P_cents * size_cents 整除 100，maker 自然落在整数 cent.
            #   P_cents = round(price*100); g = gcd(P_cents, 100); size_step_cents = 100//g
            #   size = floor(user_size * 100 // size_step) * size_step / 100  (size 单位 0.01 shares)
            from math import gcd, floor as _floor  # noqa: PLC0415

            user_price = float(request.price)
            user_size = float(request.size)

            # price snap: tick=0.01 → 2 dp（标准 Polymarket tick）
            p_cents = int(round(user_price * 100))
            if p_cents <= 0 or p_cents >= 100:
                return ExecutionOrderResult(
                    success=False,
                    error=f"price_out_of_range: {user_price}",
                    raw_response={"requested_price": user_price},
                )
            snapped_price = p_cents / 100.0

            # size snap: 让 P_cents * size_cents 整除 100
            g = gcd(p_cents, 100)
            size_step_cents = 100 // g  # 最小 size 步长（单位 0.01 shares）
            requested_size_cents = _floor(user_size * 100 + 1e-9)
            final_size_cents = (requested_size_cents // size_step_cents) * size_step_cents
            snapped_size = final_size_cents / 100.0

            if snapped_size <= 0:
                return ExecutionOrderResult(
                    success=False,
                    error=(
                        f"size_too_small_after_precision_snap: "
                        f"price={snapped_price} 要求 size 为 {size_step_cents/100:.2f} 的整数倍"
                    ),
                    raw_response={
                        "requested_size": user_size,
                        "snapped_price": snapped_price,
                        "size_step": size_step_cents / 100.0,
                    },
                )

            # 自检：maker 必须是整数 cent
            maker_cents_check = (p_cents * final_size_cents) % 100
            if maker_cents_check != 0:
                logger.error(
                    "T299 precision check FAILED: P_cents=%d size_cents=%d remainder=%d",
                    p_cents, final_size_cents, maker_cents_check,
                )
                return ExecutionOrderResult(
                    success=False,
                    error="internal_precision_check_failed",
                    raw_response={"P_cents": p_cents, "size_cents": final_size_cents},
                )

            logger.info(
                "T299 precision snap: price %.4f→%.2f, size %.4f→%.2f (step=%.2f, maker=%.2f USDC)",
                user_price, snapped_price, user_size, snapped_size,
                size_step_cents / 100.0, snapped_price * snapped_size,
            )

            order_args = OrderArgs(
                token_id=request.token_id,
                price=snapped_price,
                size=snapped_size,
                side=side,
                builder_code=builder_code,
                metadata=metadata_hex,
            )

            options = PartialCreateOrderOptions(tick_size=request.tick_size)

            logger.debug(
                "V2BuilderAdapter.place_order: token=%s side=%s price=%.4f size=%.2f "
                "type=%s builder=%s... signal_id=%s",
                request.token_id[:12],
                request.side.value,
                request.price,
                request.size,
                order_type,
                builder_code[:12],
                request.signal_id,
            )

            resp = client.create_and_post_order(
                order_args=order_args,
                options=options,
                order_type=order_type,
            )

            if resp and isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("id") or resp.get("order_id")
                status = resp.get("status", "unknown")
                success = status not in ("error", "failed") and order_id is not None
                return ExecutionOrderResult(
                    success=success,
                    order_id=order_id,
                    status=status,
                    raw_response=resp,
                )

            # Unexpected response shape — treat as success if no explicit error
            logger.warning("V2BuilderAdapter: unexpected response shape: %s", resp)
            return ExecutionOrderResult(
                success=True,
                raw_response=resp if isinstance(resp, dict) else {"raw": str(resp)},
            )

        except Exception as exc:
            logger.exception("V2BuilderAdapter.place_order failed: %s", exc)
            self._healthy = False
            return ExecutionOrderResult(success=False, error=str(exc))

    def cancel_order(self, order_id: str) -> bool:
        try:
            client = self._get_client()
            client.cancel(order_id)
            return True
        except Exception as exc:
            logger.exception(
                "V2BuilderAdapter.cancel_order(%s) failed: %s", order_id, exc
            )
            return False

    def cancel_all_orders(self) -> bool:
        try:
            client = self._get_client()
            client.cancel_all()
            return True
        except Exception as exc:
            logger.exception("V2BuilderAdapter.cancel_all_orders failed: %s", exc)
            return False

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        try:
            client = self._get_client()
            return client.get_order(order_id)
        except Exception as exc:
            logger.exception("V2BuilderAdapter.get_order(%s) failed: %s", order_id, exc)
            return None

    def get_open_orders(self) -> list[dict[str, Any]]:
        try:
            from py_clob_client_v2 import OpenOrderParams  # noqa: PLC0415

            client = self._get_client()
            result = client.get_orders(OpenOrderParams())
            return result if isinstance(result, list) else []
        except Exception as exc:
            logger.exception("V2BuilderAdapter.get_open_orders failed: %s", exc)
            return []

    def get_balance(self) -> BalanceInfo:
        try:
            client = self._get_client()
            # V2 balance is in pUSD. signature_type=2 for proxy wallet.
            resp = client.get_balance_allowance(params={"signature_type": 2})
            balance = float(resp.get("balance", 0) or 0)
            return BalanceInfo(collateral_balance=balance, currency="pUSD")
        except Exception as exc:
            logger.exception("V2BuilderAdapter.get_balance failed: %s", exc)
            return BalanceInfo(collateral_balance=0.0, currency="pUSD")

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        try:
            client = self._get_client()
            book = client.get_order_book(token_id)
            return book if isinstance(book, dict) else {}
        except Exception as exc:
            logger.exception(
                "V2BuilderAdapter.get_order_book(%s) failed: %s", token_id, exc
            )
            return {}

    def get_midpoint(self, token_id: str) -> float | None:
        try:
            client = self._get_client()
            mid = client.get_midpoint(token_id)
            return float(mid) if mid is not None else None
        except Exception as exc:
            logger.exception(
                "V2BuilderAdapter.get_midpoint(%s) failed: %s", token_id, exc
            )
            return None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def sdk_version(self) -> str:
        return "v2"

    @property
    def is_healthy(self) -> bool:
        return self._healthy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require(name: str) -> str:
    """Read a required secret; raises EnvironmentError if missing."""
    val = get_secret(name)
    if name == "POLY_RELAYER_ADDRESS" and not val:
        val = _latest_bound_wallet()
    if not val:
        raise EnvironmentError(
            f"Required secret '{name}' is not configured. " "Check your .env file."
        )
    return val


def _latest_bound_wallet() -> str:
    """Fallback: use latest wallet bound in dashboard when relayer env is absent."""
    db_path = os.environ.get("DASHBOARD_DB_PATH", "data/dashboard.db")
    try:
        conn = _pg_connect()
        try:
            row = conn.execute(
                """
                SELECT wallet_address
                FROM wallet_bindings
                WHERE wallet_address IS NOT NULL AND wallet_address != ''
                ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if row and row["wallet_address"]:
                return str(row["wallet_address"]).strip()
        finally:
            conn.close()
    except Exception:
        return ""
    return ""


def _map_order_type(ot: ExecutionOrderType) -> Any:
    """Map ExecutionOrderType enum to py_clob_client_v2.OrderType."""
    from py_clob_client_v2 import OrderType  # noqa: PLC0415

    mapping = {
        ExecutionOrderType.GTC: OrderType.GTC,
        ExecutionOrderType.FOK: OrderType.FOK,
        ExecutionOrderType.FAK: OrderType.FAK,
    }
    return mapping.get(ot, OrderType.GTC)
