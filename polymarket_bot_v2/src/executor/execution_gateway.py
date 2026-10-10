"""
ExecutionGateway — abstract interface for order execution.

All order submission code in PaperTrader / OrderExecutor must depend on this
interface, not directly on py_clob_client or py_clob_client_v2.

Implementations:
  V2BuilderAdapter  — py-clob-client-v2 with builderCode (active path)
  V1Adapter         — py-clob-client V1 (emergency fallback, read-only after April 28)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionOrderType(str, Enum):
    GTC = "GTC"  # Good-Till-Cancelled (resting limit)
    FOK = "FOK"  # Fill-Or-Kill (market / immediate)
    FAK = "FAK"  # Fill-And-Kill (partial fill allowed)


@dataclass
class ExecutionOrderRequest:
    """Unified order request that flows through the gateway.

    Fields map cleanly to both V1 and V2 SDKs.
    The gateway implementation is responsible for translating to SDK-specific types.
    """

    token_id: str
    price: float
    size: float
    side: OrderSide
    order_type: ExecutionOrderType = ExecutionOrderType.GTC
    tick_size: str = "0.01"

    # V2 attribution / tracing (injected by V2BuilderAdapter)
    builder_code: str | None = None  # bytes32 hex; overrides constructor default
    metadata: str | None = None  # bytes32 hex or raw string (auto-encoded)

    # Idempotency / audit
    signal_id: str | None = None  # global unique signal identifier
    trace_id: str | None = None  # end-to-end trace


@dataclass
class ExecutionOrderResult:
    """Unified result returned from the gateway after an order attempt."""

    success: bool
    order_id: str | None = None
    status: str | None = None  # e.g. "matched", "resting", "error"
    filled_size: float = 0.0
    avg_price: float = 0.0
    error: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass
class BalanceInfo:
    """pUSD / collateral balance returned by the gateway."""

    collateral_balance: float  # pUSD in V2, USDC.e in V1
    total_shares_value: float = 0.0
    currency: str = "pUSD"  # "pUSD" for V2, "USDC.e" for V1


class ExecutionGateway(ABC):
    """Abstract execution gateway.

    All interactions with the CLOB go through this interface.
    Implementations must be stateless enough to be safely shared across threads.
    """

    # --- Order lifecycle ---

    @abstractmethod
    def place_order(self, request: ExecutionOrderRequest) -> ExecutionOrderResult:
        """Submit an order to the CLOB. Returns result synchronously."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order by ID. Returns True on success."""
        ...

    @abstractmethod
    def cancel_all_orders(self) -> bool:
        """Cancel all open orders. Returns True on success."""
        ...

    @abstractmethod
    def get_order(self, order_id: str) -> dict[str, Any] | None:
        """Fetch a single order by ID, or None if not found."""
        ...

    @abstractmethod
    def get_open_orders(self) -> list[dict[str, Any]]:
        """Return list of open orders for the authenticated account."""
        ...

    # --- Account / balance ---

    @abstractmethod
    def get_balance(self) -> BalanceInfo:
        """Return collateral balance information."""
        ...

    # --- Market data (read-only, no auth required) ---

    @abstractmethod
    def get_order_book(self, token_id: str) -> dict[str, Any]:
        """Return the order book for a token."""
        ...

    @abstractmethod
    def get_midpoint(self, token_id: str) -> float | None:
        """Return the current mid-price for a token."""
        ...

    # --- Introspection ---

    @property
    @abstractmethod
    def sdk_version(self) -> str:
        """Return 'v1' or 'v2' to identify which SDK this gateway wraps."""
        ...

    @property
    @abstractmethod
    def is_healthy(self) -> bool:
        """Return True if the gateway can accept orders right now."""
        ...
