"""
polymarket_manual_routes.py — Manual User 手动下单与机会查询

说明：
- 下单使用平台统一 CLOB V2 账户（平台归属），并记录 user 归因。
- 用户级冻结/停机门控只作用当前用户，不影响其他租户。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import requests
from src.utils.pm_http import pm_session as _pm_session  # T510 P0-1
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import db_connect, get, get_request_identity
from src.executor.execution_gateway import (
    ExecutionOrderRequest,
    ExecutionOrderType,
    OrderSide,
)
from src.executor.v2_builder_adapter import V2BuilderAdapter
import asyncio

router = APIRouter()
_GAMMA_API = "https://gamma-api.polymarket.com"


def _ensure_manual_order_table() -> None:
    """PostgreSQL DDL — SERIAL 替代 SQLite AUTOINCREMENT。"""
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_manual_orders (
                id SERIAL PRIMARY KEY,
                user_wallet TEXT NOT NULL,
                polymarket_wallet TEXT,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                order_type TEXT NOT NULL,
                order_id TEXT,
                status TEXT NOT NULL,
                builder_fee_usdc REAL NOT NULL DEFAULT 0,
                error TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _ensure_runtime_table() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_runtime_controls (
                wallet_address TEXT PRIMARY KEY,
                freeze_auto INTEGER NOT NULL DEFAULT 0,
                auto_trading_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
        )


def _get_runtime_control(wallet_address: str) -> dict:
    _ensure_runtime_table()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT freeze_auto, auto_trading_enabled FROM user_runtime_controls WHERE wallet_address=?",
            (wallet_address,),
        ).fetchone()
    if not row:
        return {"freeze_auto": False, "auto_trading_enabled": True}
    return {
        "freeze_auto": bool(row["freeze_auto"]),
        "auto_trading_enabled": bool(row["auto_trading_enabled"]),
    }


def _runtime_global_block() -> tuple[bool, str]:
    if bool(get("system.maintenance_mode")):
        return True, "system is in maintenance mode"
    if bool(get("risk.emergency_freeze")):
        return True, "global emergency freeze is enabled"
    return False, ""


def _get_user_pm_wallet(user_wallet: str) -> str:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT polymarket_wallet FROM users WHERE wallet_address=?",
            (user_wallet,),
        ).fetchone()
    return str((row["polymarket_wallet"] if row else "") or "").strip().lower()


def _adapter() -> V2BuilderAdapter:
    host = str(get("polymarket.clob_api_url") or "https://clob.polymarket.com")
    return V2BuilderAdapter(host=host)


@router.get("/api/polymarket/opportunities")
async def list_opportunities(limit: int = 30):
    """从 Gamma API 拉取可交易市场，供手动下单页选择。"""
    try:
        resp = _pm_session().get(
            f"{_GAMMA_API}/markets",
            params={
                "closed": "false",
                "active": "true",
                "limit": max(5, min(limit, 80)),
            },
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return JSONResponse(
            {"error": f"fetch opportunities failed: {exc}"}, status_code=502
        )

    out = []
    for m in data if isinstance(data, list) else []:
        clob_token_ids = m.get("clobTokenIds")
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except Exception:
                clob_token_ids = []
        if not isinstance(clob_token_ids, list) or len(clob_token_ids) < 2:
            continue
        out.append(
            {
                "market_id": str(m.get("id") or ""),
                "condition_id": str(m.get("conditionId") or ""),
                "question": str(m.get("question") or ""),
                "event_slug": str(m.get("eventSlug") or ""),
                "market_slug": str(m.get("slug") or ""),
                "yes_token_id": str(clob_token_ids[0]),
                "no_token_id": str(clob_token_ids[1]),
                "best_ask": m.get("bestAsk"),
                "best_bid": m.get("bestBid"),
                "volume": m.get("volume"),
                "liquidity": m.get("liquidity"),
                "end_date": m.get("endDate"),
            }
        )
    return {"status": "ok", "count": len(out), "markets": out}


@router.post("/api/polymarket/manual-order")
async def place_manual_order(request: Request):
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "")
    blocked, reason = _runtime_global_block()
    if blocked:
        return JSONResponse(
            {
                "error": reason,
                "maintenance_mode": bool(get("system.maintenance_mode")),
                "emergency_freeze": bool(get("risk.emergency_freeze")),
            },
            status_code=403,
        )

    runtime = _get_runtime_control(user_wallet)
    if runtime.get("freeze_auto") or not runtime.get("auto_trading_enabled"):
        return JSONResponse(
            {
                "error": "user trading is frozen or disabled",
                "runtime": runtime,
            },
            status_code=403,
        )

    pm_wallet = _get_user_pm_wallet(user_wallet)
    if not pm_wallet:
        return JSONResponse(
            {"error": "bind your PM wallet in /Users before trading"},
            status_code=400,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    token_id = str(body.get("token_id") or "").strip()
    side_raw = str(body.get("side") or "BUY").upper().strip()
    try:
        price = float(body.get("price"))
        size = float(body.get("size"))
    except Exception:
        return JSONResponse({"error": "price/size must be numeric"}, status_code=400)

    if not token_id:
        return JSONResponse({"error": "token_id required"}, status_code=400)
    if side_raw not in {"BUY", "SELL"}:
        return JSONResponse({"error": "side must be BUY or SELL"}, status_code=400)
    if not (0.001 <= price <= 0.999):
        return JSONResponse(
            {"error": "price must be between 0.001 and 0.999"}, status_code=400
        )
    if size <= 0:
        return JSONResponse({"error": "size must be > 0"}, status_code=400)

    req = ExecutionOrderRequest(
        token_id=token_id,
        price=price,
        size=size,
        side=OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL,
        order_type=ExecutionOrderType.FOK,
        signal_id=f"manual-{user_wallet[:10]}",
        trace_id=f"manual-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
    )

    result = _adapter().place_order(req)
    now = datetime.now(timezone.utc).isoformat()

    _ensure_manual_order_table()
    def _db():
        with db_connect() as conn:
            conn.execute(
                """
            INSERT INTO user_manual_orders(
                user_wallet, polymarket_wallet, token_id, side, price, size,
                order_type, order_id, status, builder_fee_usdc, error, raw_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    user_wallet,
                    pm_wallet,
                    token_id,
                    side_raw,
                    price,
                    size,
                    "FOK",
                    result.order_id,
                    result.status or ("success" if result.success else "error"),
                    0.0,
                    result.error or "",
                    json.dumps(result.raw_response or {}, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()

        if not result.success:
            return JSONResponse(
                {
                    "status": "error",
                    "error": result.error or "order failed",
                    "order_id": result.order_id,
                    "raw": result.raw_response,
                },
                status_code=502,
            )

        return {
            "status": "ok",
            "user_wallet": user_wallet,
            "polymarket_wallet": pm_wallet,
            "order_id": result.order_id,
            "order_status": result.status,
            "raw": result.raw_response,
            "fee_note": "Builder fee attribution is handled by platform API key/builder code on CLOB V2.",
        }
    return await asyncio.to_thread(_db)
