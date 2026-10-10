"""T918-B5 — boundary validators for /api/pm/* endpoints.

Centralized fail-closed validators for path/query parameters originating
from untrusted clients. Designed as additive helpers — callers raise
HTTPException(400) on failure to keep error shape consistent.

Constants are tuned to Polymarket reality:
- token_id: uint256 decimal string, max 78 digits
- batch list: max 50 entries
- amount: Decimal in (0, 1_000_000]
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable

from fastapi import HTTPException

_MAX_TOKEN_DIGITS = 78
_MAX_BATCH = 50
_AMOUNT_MIN = Decimal("0")
_AMOUNT_MAX = Decimal("1000000")


def validate_token_id(value: object, field: str = "token_id") -> str:
    """Return the string token_id if valid, else raise 400."""
    if value is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_token_id", "field": field})
    s = str(value).strip()
    if not s or not s.isdigit() or len(s) > _MAX_TOKEN_DIGITS:
        raise HTTPException(status_code=400, detail={"error": "invalid_token_id", "field": field})
    return s


def validate_token_id_list(raw: object, field: str = "token_ids") -> list[str]:
    """Parse comma- or list-shaped token_ids, enforce ≤ 50 valid uint256 strings."""
    if raw is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_token_ids", "field": field})
    if isinstance(raw, (list, tuple)):
        items: Iterable[str] = (str(x) for x in raw)
    else:
        items = (x for x in str(raw).split(","))
    cleaned: list[str] = []
    for item in items:
        s = item.strip()
        if not s:
            continue
        if not s.isdigit() or len(s) > _MAX_TOKEN_DIGITS:
            raise HTTPException(status_code=400, detail={"error": "invalid_token_id", "field": field})
        cleaned.append(s)
        if len(cleaned) > _MAX_BATCH:
            raise HTTPException(status_code=400, detail={"error": "batch_too_large", "limit": _MAX_BATCH})
    if not cleaned:
        raise HTTPException(status_code=400, detail={"error": "invalid_token_ids", "field": field})
    return cleaned


def validate_condition_id(value: object, field: str = "condition_id") -> str:
    """Return a normalized 0x-prefixed 32-byte hex condition id, else raise 400."""
    if value is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_condition_id", "field": field})
    s = str(value).strip().lower()
    if not s.startswith("0x"):
        s = "0x" + s
    if len(s) != 66:
        raise HTTPException(status_code=400, detail={"error": "invalid_condition_id", "field": field})
    try:
        int(s, 16)
    except ValueError:
        raise HTTPException(status_code=400, detail={"error": "invalid_condition_id", "field": field})
    return s


def validate_amount(value: object, field: str = "amount", *, allow_zero: bool = False) -> Decimal:
    """Parse a Decimal amount, enforce (0, 1_000_000] (or [0, 1_000_000] when allow_zero)."""
    if value is None or str(value).strip() == "":
        raise HTTPException(status_code=400, detail={"error": "invalid_amount", "field": field})
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail={"error": "invalid_amount", "field": field})
    if d != d:
        raise HTTPException(status_code=400, detail={"error": "invalid_amount", "field": field})
    if allow_zero:
        if d < _AMOUNT_MIN or d > _AMOUNT_MAX:
            raise HTTPException(status_code=400, detail={"error": "amount_out_of_range", "field": field})
    else:
        if d <= _AMOUNT_MIN or d > _AMOUNT_MAX:
            raise HTTPException(status_code=400, detail={"error": "amount_out_of_range", "field": field})
    return d


def validate_eth_address(value: object, field: str = "address") -> str:
    """Return checksum-stripped lowercase 0x-prefixed 20-byte address, else raise 400."""
    if value is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_address", "field": field})
    s = str(value).strip().lower()
    if not s.startswith("0x"):
        s = "0x" + s
    if len(s) != 42:
        raise HTTPException(status_code=400, detail={"error": "invalid_address", "field": field})
    try:
        int(s, 16)
    except ValueError:
        raise HTTPException(status_code=400, detail={"error": "invalid_address", "field": field})
    return s
