"""
wallet_routes.py — V2 钱包绑定路由（Metamask personal_sign）

提供最小闭环：
1) 获取 challenge（地址 + nonce）
2) 验签并绑定到本地 DB
3) 查询已绑定钱包列表
"""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import db_connect
import asyncio

router = APIRouter()

_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_CHALLENGE_TTL_SEC = 300

# in-memory challenge store: address -> payload
_wallet_challenges: dict[str, dict] = {}


def _build_challenge_message(address: str, nonce: str, issued_at: str) -> str:
    return (
        "Veyra.trade Wallet Bind\n"
        f"Address: {address}\n"
        f"Nonce: {nonce}\n"
        f"IssuedAt: {issued_at}\n"
    )


def _ensure_wallet_binding_table() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_bindings (
                wallet_address TEXT PRIMARY KEY,
                bound_at TEXT NOT NULL,
                source_ip TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )


@router.get("/api/wallet/status")
async def wallet_status():
    _ensure_wallet_binding_table()
    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT wallet_address, bound_at, source_ip, updated_at "
                "FROM wallet_bindings ORDER BY updated_at DESC"
            ).fetchall()
        data = [
            {
                "wallet_address": r["wallet_address"],
                "bound_at": r["bound_at"],
                "source_ip": r["source_ip"] or "",
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        return {"status": "ok", "count": len(data), "wallets": data}
    return await asyncio.to_thread(_db)


@router.post("/api/wallet/challenge")
async def wallet_challenge(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    address = str(body.get("address", "")).strip().lower()
    if not _ADDR_RE.match(address):
        return JSONResponse({"error": "invalid wallet address"}, status_code=400)

    nonce = secrets.token_hex(16)
    issued_at = datetime.now(timezone.utc).isoformat()
    message = _build_challenge_message(address, nonce, issued_at)

    _wallet_challenges[address] = {
        "nonce": nonce,
        "issued_at": issued_at,
        "message": message,
        "ts": time.time(),
    }

    return {
        "status": "ok",
        "address": address,
        "nonce": nonce,
        "expires_sec": _CHALLENGE_TTL_SEC,
        "message": message,
    }


@router.post("/api/wallet/verify")
async def wallet_verify(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    address = str(body.get("address", "")).strip().lower()
    signature = str(body.get("signature", "")).strip()

    if not _ADDR_RE.match(address):
        return JSONResponse({"error": "invalid wallet address"}, status_code=400)
    if not signature:
        return JSONResponse({"error": "missing signature"}, status_code=400)

    challenge = _wallet_challenges.get(address)
    if not challenge:
        return JSONResponse({"error": "challenge not found"}, status_code=404)

    if time.time() - float(challenge.get("ts", 0)) > _CHALLENGE_TTL_SEC:
        _wallet_challenges.pop(address, None)
        return JSONResponse({"error": "challenge expired"}, status_code=401)

    message = challenge["message"]
    try:
        recovered = Account.recover_message(
            encode_defunct(text=message), signature=signature
        ).lower()
    except Exception:
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    if recovered != address:
        return JSONResponse({"error": "signature address mismatch"}, status_code=401)

    _ensure_wallet_binding_table()
    now = datetime.now(timezone.utc).isoformat()
    source_ip = request.client.host if request.client else ""
    def _db():
        with db_connect() as conn:
            conn.execute(
                """
            INSERT INTO wallet_bindings(wallet_address, bound_at, source_ip, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                source_ip=excluded.source_ip,
                updated_at=excluded.updated_at
            """,
                (address, now, source_ip, now),
            )
            conn.commit()

        _wallet_challenges.pop(address, None)

        return {
            "status": "ok",
            "wallet_address": address,
            "bound_at": now,
            "message": "wallet bound successfully",
        }
    return await asyncio.to_thread(_db)
