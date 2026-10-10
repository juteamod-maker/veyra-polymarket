"""T510 P2-1 (option A): Polymarket User Channel WebSocket bridge.

Operator-key firehose pattern:
  We hold a single set of CLOB API credentials (POLY_API_KEY / POLY_SECRET /
  POLY_PASSPHRASE), used by every V2 user that places orders through the
  V2 builder/operator. We open one ``/ws/user`` subscription with that
  credential and route incoming ``trade`` / ``order`` events into per-wallet
  in-memory queues (keyed by lowercased ``trade_owner`` / ``maker_address`` /
  ``order_owner``).

  This avoids per-user CLOB key storage entirely and is reversible — if PM's
  user channel turns out to scope events to the API-key owner only (and not
  cross-funder), this module surfaces zero events and we can simply unwire it.

Subscription scope:
  PM caps a user-channel subscription at 100 ``conditionId``s. We subscribe
  to the top-N most-active markets (Gamma ``/events?order=volume24hr``) and
  refresh the subscription every 60 s, identical strategy to ``pm_market_ws``.

Compatibility note (CLOB V2 cutover 2026-04-28 ~11:00 UTC):
  Order schema gains ``timestamp / metadata / builder`` and drops
  ``nonce / feeRateBps / taker``. This bridge is field-agnostic — it just
  forwards the entire payload dict — so cutover does not require code
  changes here.

Public helpers:
    - ``start()``                    — schedule background task once.
    - ``get_events(wallet, since)``  — drain new events for a wallet.
    - ``stats()``                    — observability snapshot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Any, Iterable

import requests

logger = logging.getLogger("pm_user_ws")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
GAMMA_HOT_URL = "https://gamma-api.polymarket.com/events"
REFRESH_SECONDS = 60.0
PING_INTERVAL = 10.0
TOP_N_MARKETS = 100  # PM hard cap on /ws/user
QUEUE_PER_WALLET = 256
MAX_RECONNECT_BACKOFF = 30.0

# wallet (lowercase 0x...) -> deque[event dict]
_USER_EVENTS: dict[str, deque[dict[str, Any]]] = {}
_LOCK = asyncio.Lock()
_STARTED = False
_TASK: asyncio.Task | None = None
_CURRENT_MARKETS: set[str] = set()
_TOTAL_EVENTS = 0
_LAST_EVENT_TS: float = 0.0


# ── public read helpers ──────────────────────────────────────────────

def get_events(wallet: str, since_ts: float = 0.0, max_items: int = 64) -> list[dict[str, Any]]:
    """Return events with ``_received_ts > since_ts`` for ``wallet`` (no drain)."""
    if not wallet:
        return []
    key = wallet.strip().lower()
    q = _USER_EVENTS.get(key)
    if not q:
        return []
    out: list[dict[str, Any]] = []
    for evt in list(q):
        if evt.get("_received_ts", 0) > since_ts:
            out.append(dict(evt))
            if len(out) >= max_items:
                break
    return out


def stats() -> dict[str, Any]:
    return {
        "started": _STARTED,
        "subscribed_markets": len(_CURRENT_MARKETS),
        "tracked_wallets": len(_USER_EVENTS),
        "total_events": _TOTAL_EVENTS,
        "last_event_ts": _LAST_EVENT_TS,
        "ws_url": WS_URL,
    }


# ── internal: market discovery ───────────────────────────────────────

def _fetch_top_condition_ids(limit: int) -> list[str]:
    try:
        resp = requests.get(
            GAMMA_HOT_URL,
            params={
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": max(1, limit),
            },
            headers={"User-Agent": "VeYra-Bot/2.0"},
            timeout=8,
        )
        resp.raise_for_status()
        events = resp.json() or []
    except Exception as exc:
        logger.warning("[pm_user_ws] gamma fetch failed: %s", exc)
        return []
    out: list[str] = []
    seen: set[str] = set()
    for ev in events:
        for m in (ev.get("markets") or []):
            cid = (m.get("conditionId") or "").strip()
            if cid and cid not in seen:
                seen.add(cid)
                out.append(cid)
                if len(out) >= limit:
                    return out
    return out


# ── internal: event routing ──────────────────────────────────────────

_OWNER_FIELDS = (
    "trade_owner",
    "maker_address",
    "order_owner",
    "owner",
    "taker_address",
    "user",
    "wallet",
)


def _extract_wallets(evt: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for f in _OWNER_FIELDS:
        v = evt.get(f)
        if isinstance(v, str) and v.startswith("0x") and len(v) >= 10:
            found.append(v.lower())
    # deep scan one level (e.g. evt['order']['owner'])
    for v in evt.values():
        if isinstance(v, dict):
            for f in _OWNER_FIELDS:
                vv = v.get(f) if hasattr(v, "get") else None
                if isinstance(vv, str) and vv.startswith("0x") and len(vv) >= 10:
                    found.append(vv.lower())
    # dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for w in found:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _enqueue(wallet: str, evt: dict[str, Any]) -> None:
    q = _USER_EVENTS.get(wallet)
    if q is None:
        q = deque(maxlen=QUEUE_PER_WALLET)
        _USER_EVENTS[wallet] = q
    q.append(evt)


def _handle_message(raw: str | bytes) -> None:
    global _TOTAL_EVENTS, _LAST_EVENT_TS
    if not raw:
        return
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="ignore")
        except Exception:
            return
    try:
        data = json.loads(raw)
    except Exception:
        return
    items = data if isinstance(data, list) else [data]
    now = time.time()
    for evt in items:
        if not isinstance(evt, dict):
            continue
        ev_type = (evt.get("event_type") or evt.get("type") or "").lower()
        if ev_type not in ("trade", "order"):
            continue
        rec = dict(evt)
        rec["_received_ts"] = now
        rec["_ev_type"] = ev_type
        wallets = _extract_wallets(evt)
        if not wallets:
            # still bucket into "_unrouted" so we can debug post-cutover whether
            # operator-key receives only its own events
            _enqueue("_unrouted", rec)
        else:
            for w in wallets:
                _enqueue(w, rec)
        _TOTAL_EVENTS += 1
        _LAST_EVENT_TS = now


# ── internal: ws session ─────────────────────────────────────────────

def _build_subscribe(markets: Iterable[str]) -> dict[str, Any] | None:
    api_key = os.environ.get("POLY_API_KEY", "").strip()
    secret = os.environ.get("POLY_SECRET", "").strip()
    passphrase = os.environ.get("POLY_PASSPHRASE", "").strip()
    if not (api_key and secret and passphrase):
        return None
    return {
        "auth": {"apiKey": api_key, "secret": secret, "passphrase": passphrase},
        "markets": list(markets),
        "type": "user",
    }


async def _ping_loop(ws) -> None:
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await ws.send("PING")
            except Exception:
                return
    except asyncio.CancelledError:
        return


async def _refresh_loop(ws) -> None:
    global _CURRENT_MARKETS
    try:
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            new_top = _fetch_top_condition_ids(TOP_N_MARKETS)
            if not new_top:
                continue
            new_set = set(new_top)
            to_add = new_set - _CURRENT_MARKETS
            to_remove = _CURRENT_MARKETS - new_set
            if to_add:
                try:
                    await ws.send(json.dumps({
                        "markets": list(to_add),
                        "operation": "subscribe",
                    }))
                except Exception:
                    return
            if to_remove:
                try:
                    await ws.send(json.dumps({
                        "markets": list(to_remove),
                        "operation": "unsubscribe",
                    }))
                except Exception:
                    return
            _CURRENT_MARKETS = new_set
    except asyncio.CancelledError:
        return


async def _ws_session() -> None:
    global _CURRENT_MARKETS
    import websockets

    markets = _fetch_top_condition_ids(TOP_N_MARKETS)
    if not markets:
        raise RuntimeError("no active markets fetched from gamma")
    sub = _build_subscribe(markets)
    if sub is None:
        raise RuntimeError("operator credentials POLY_API_KEY/SECRET/PASSPHRASE missing")

    async with websockets.connect(
        WS_URL,
        ping_interval=None,
        max_size=2 * 1024 * 1024,
        open_timeout=10,
        close_timeout=5,
    ) as ws:
        await ws.send(json.dumps(sub))
        _CURRENT_MARKETS = set(markets)
        logger.info("[pm_user_ws] connected; subscribed to %d markets", len(markets))

        ping_task = asyncio.create_task(_ping_loop(ws), name="pm_user_ws_ping")
        refresh_task = asyncio.create_task(_refresh_loop(ws), name="pm_user_ws_refresh")
        try:
            async for msg in ws:
                text = msg if isinstance(msg, str) else msg.decode("utf-8", errors="ignore")
                stripped = text.strip()
                if stripped.upper() == "PONG" or stripped == "":
                    continue
                _handle_message(text)
        finally:
            ping_task.cancel()
            refresh_task.cancel()
            for t in (ping_task, refresh_task):
                try:
                    await t
                except Exception:
                    pass


async def _runner() -> None:
    backoff = 1.0
    while True:
        try:
            await _ws_session()
            backoff = 1.0
            logger.info("[pm_user_ws] session ended cleanly; reconnecting")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[pm_user_ws] session error: %s; backoff %.1fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(MAX_RECONNECT_BACKOFF, backoff * 2)


def start() -> None:
    global _STARTED, _TASK
    if _STARTED:
        return
    if not (os.environ.get("POLY_API_KEY") and os.environ.get("POLY_SECRET")
            and os.environ.get("POLY_PASSPHRASE")):
        logger.warning("[pm_user_ws] operator credentials missing; bridge NOT started")
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    _TASK = loop.create_task(_runner(), name="pm_user_ws_runner")
    _STARTED = True
    logger.info("[pm_user_ws] background bridge scheduled")
