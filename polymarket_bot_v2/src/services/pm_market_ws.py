"""T510 P1-1: Polymarket Market Channel WebSocket bridge.

Maintains a single asyncio background task in the dashboard process that:

  * keeps a rolling top-N hot-token subscription set (refreshed every
    ``REFRESH_SECONDS`` from Gamma ``/events?order=volume24hr``);
  * connects to ``wss://ws-subscriptions-clob.polymarket.com/ws/market``;
  * sends the official subscribe payload with ``custom_feature_enabled=true``;
  * consumes ``price_change`` / ``last_trade_price`` / ``best_bid_ask`` /
    ``book`` messages and writes them into an in-memory ``_LIVE_PRICES``
    map keyed by token id;
  * sends ``PING`` every 10 s (per docs.polymarket.com /ws/overview);
  * reconnects with exponential backoff on disconnect.

Public helpers:
    - ``start(loop)``           — schedule the background task once.
    - ``get_live_price(tid)``   — return ``{"price", "ts", "bid", "ask"}``
                                  or ``None`` if not yet seen.
    - ``snapshot()``            — return a shallow copy of all live prices.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import requests

logger = logging.getLogger("pm_market_ws")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_HOT_URL = "https://gamma-api.polymarket.com/events"

REFRESH_SECONDS = 60
PING_INTERVAL = 10.0
TOP_N_TOKENS = 200
MAX_RECONNECT_BACKOFF = 30.0

# Live in-memory snapshot (token_id → {"price","ts","bid","ask"}).
_LIVE_PRICES: dict[str, dict[str, Any]] = {}
_LOCK = asyncio.Lock()
_STARTED = False
_TASK: asyncio.Task | None = None
_CURRENT_ASSETS: set[str] = set()

# T523 P3 #523 — adaptive subscription: tokens REST-quoted by callers but
# not yet on WS get registered as "interest".  The refresh loop unions
# them with the top-N hot tokens so subsequent calls hit WS.  Capped at
# ``MAX_INTEREST_TOKENS`` to keep the subscription set bounded.
MAX_INTEREST_TOKENS = 600
INTEREST_TTL_SECONDS = 1800.0  # 30 min — drop unused interest after this
_INTEREST: dict[str, float] = {}  # token_id → last_request_ts


def register_interest(token_ids) -> None:
    """Mark ``token_ids`` as live-price interest so the refresh loop will
    add them to the WS subscription on the next refresh tick.

    Safe to call from sync paths — only mutates an in-process dict.
    """
    if not token_ids:
        return
    now = time.time()
    if isinstance(token_ids, (str, bytes)):
        token_ids = [token_ids]
    for tid in token_ids:
        s = str(tid).strip()
        if not s:
            continue
        _INTEREST[s] = now
    # Drop oldest if over budget.
    if len(_INTEREST) > MAX_INTEREST_TOKENS:
        for k, _ in sorted(_INTEREST.items(), key=lambda kv: kv[1])[: len(_INTEREST) - MAX_INTEREST_TOKENS]:
            _INTEREST.pop(k, None)


def _active_interest() -> set[str]:
    cutoff = time.time() - INTEREST_TTL_SECONDS
    return {tid for tid, ts in _INTEREST.items() if ts >= cutoff}


def _fetch_top_token_ids(limit: int = TOP_N_TOKENS) -> list[str]:
    """Pull top hot events and flatten their YES/NO clob token ids."""
    try:
        resp = requests.get(
            GAMMA_HOT_URL,
            params={
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": max(20, min(100, int(limit) // 2)),
            },
            timeout=8,
            headers={"User-Agent": "VeYra-Bot/2.0 (+https://veyra.trade)"},
        )
        resp.raise_for_status()
        events = resp.json() or []
    except Exception as exc:
        logger.warning("hot-events fetch failed: %s", exc)
        return []
    out: list[str] = []
    for ev in events if isinstance(events, list) else []:
        for m in (ev.get("markets") or []):
            raw = m.get("clobTokenIds") or m.get("clob_token_ids")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = []
            if isinstance(raw, list):
                for t in raw:
                    s = str(t).strip()
                    if s and s not in out:
                        out.append(s)
                        if len(out) >= limit:
                            return out
    return out[:limit]


def get_live_price(token_id: str) -> dict[str, Any] | None:
    """Return latest live record for ``token_id`` or ``None`` if not tracked."""
    if not token_id:
        return None
    rec = _LIVE_PRICES.get(str(token_id))
    return dict(rec) if rec else None


def snapshot() -> dict[str, dict[str, Any]]:
    """Return a shallow copy of the entire live-price map."""
    return {k: dict(v) for k, v in _LIVE_PRICES.items()}


def stats() -> dict[str, Any]:
    """Quick observability snapshot: subscribed asset count + price count."""
    return {
        "started": _STARTED,
        "subscribed_assets": len(_CURRENT_ASSETS),
        "live_prices": len(_LIVE_PRICES),
        "ws_url": WS_URL,
    }


def _coerce_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def _record(token_id: str, *, price: float | None = None,
            bid: float | None = None, ask: float | None = None) -> None:
    if not token_id:
        return
    now = time.time()
    rec = _LIVE_PRICES.get(token_id) or {}
    if price is not None:
        rec["price"] = price
    if bid is not None:
        rec["bid"] = bid
    if ask is not None:
        rec["ask"] = ask
    rec["ts"] = now
    _LIVE_PRICES[token_id] = rec


def _handle_message(raw: str) -> None:
    if not raw or raw == "PONG":
        return
    try:
        data = json.loads(raw)
    except Exception:
        return
    # Server may emit a single object or a batch list.
    items = data if isinstance(data, list) else [data]
    for evt in items:
        if not isinstance(evt, dict):
            continue
        ev_type = (evt.get("event_type") or evt.get("type") or "").lower()
        token_id = str(evt.get("asset_id") or evt.get("market") or "").strip()
        if not token_id:
            continue
        if ev_type == "price_change":
            # T523 P0 root-cause for "YES/NO=100%" bug:
            # ``price_change`` is an order-book *delta* — each entry only
            # records that a given price level had its size mutated.  It is
            # NOT a "the new top-of-book is X" signal.  Previously we picked
            # max(BUY) / min(SELL) over the changes which would happily latch
            # onto an order-book *clearing* event at price 1.0 (or 0.0) and
            # pin the cached best_bid/best_ask there until the next snapshot.
            # That is exactly what surfaced as 100¢/100% on event_detail.
            #
            # Correct behaviour: ignore price_change for derived bid/ask and
            # only refresh the timestamp (so the record is "still alive").
            # ``best_bid_ask`` and ``book`` events remain the sole source of
            # truth for top-of-book.
            rec = _LIVE_PRICES.get(token_id)
            if rec is not None:
                rec["ts"] = time.time()
                _LIVE_PRICES[token_id] = rec
            continue
        elif ev_type == "last_trade_price":
            _record(token_id, price=_coerce_float(evt.get("price")))
        elif ev_type == "best_bid_ask":
            _record(
                token_id,
                bid=_coerce_float(evt.get("best_bid")),
                ask=_coerce_float(evt.get("best_ask")),
            )
        elif ev_type == "book":
            bids = evt.get("bids") or []
            asks = evt.get("asks") or []
            top_bid = _coerce_float((bids[0] or {}).get("price")) if bids else None
            top_ask = _coerce_float((asks[0] or {}).get("price")) if asks else None
            mid = None
            if top_bid is not None and top_ask is not None:
                mid = (top_bid + top_ask) / 2.0
            elif top_bid is not None:
                mid = top_bid
            elif top_ask is not None:
                mid = top_ask
            _record(token_id, price=mid, bid=top_bid, ask=top_ask)


async def _refresh_assets_loop() -> list[str]:
    """Synchronous call wrapped: fetch top tokens off the loop."""
    return await asyncio.to_thread(_fetch_top_token_ids, TOP_N_TOKENS)


async def _ws_session(asset_ids: list[str]) -> None:
    """One full WS session: subscribe → consume → ping → return on disconnect."""
    import websockets  # local import: only required when WS bridge is enabled

    sub = {
        "assets_ids": asset_ids,
        "type": "market",
        "custom_feature_enabled": True,
    }
    async with websockets.connect(
        WS_URL,
        ping_interval=None,        # we manage PING manually per docs
        max_size=2 * 1024 * 1024,
        open_timeout=10,
        close_timeout=5,
    ) as ws:
        await ws.send(json.dumps(sub))
        global _CURRENT_ASSETS
        _CURRENT_ASSETS = set(asset_ids)
        logger.info("[pm_market_ws] subscribed to %d assets", len(asset_ids))

        async def _ping_loop() -> None:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                try:
                    await ws.send("PING")
                except Exception:
                    return

        async def _refresh_loop() -> None:
            while True:
                await asyncio.sleep(REFRESH_SECONDS)
                fresh = await _refresh_assets_loop()
                if not fresh:
                    continue
                fresh_set = set(fresh)
                # T523 P3 #523 — union with active interest set so REST-quoted
                # tokens get promoted to WS on the next tick. Capped to keep
                # the subscription set bounded.
                interest = _active_interest()
                fresh_set |= interest
                add = fresh_set - _CURRENT_ASSETS
                drop = _CURRENT_ASSETS - fresh_set
                if add:
                    try:
                        await ws.send(json.dumps({
                            "assets_ids": sorted(add),
                            "operation": "subscribe",
                            "custom_feature_enabled": True,
                        }))
                    except Exception:
                        return
                if drop:
                    try:
                        await ws.send(json.dumps({
                            "assets_ids": sorted(drop),
                            "operation": "unsubscribe",
                        }))
                    except Exception:
                        return
                _CURRENT_ASSETS = fresh_set
                logger.info(
                    "[pm_market_ws] subscription refresh +%d -%d total=%d (interest=%d)",
                    len(add), len(drop), len(_CURRENT_ASSETS), len(interest),
                )

        ping_task = asyncio.create_task(_ping_loop())
        refresh_task = asyncio.create_task(_refresh_loop())
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    try:
                        msg = msg.decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                _handle_message(msg)
        finally:
            for t in (ping_task, refresh_task):
                t.cancel()


async def _runner() -> None:
    backoff = 1.0
    while True:
        try:
            assets = await _refresh_assets_loop()
            if not assets:
                logger.warning("[pm_market_ws] no hot assets; retry in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(MAX_RECONNECT_BACKOFF, backoff * 2)
                continue
            backoff = 1.0
            await _ws_session(assets)
            logger.info("[pm_market_ws] session ended cleanly; reconnecting")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[pm_market_ws] session error: %s; backoff %.1fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(MAX_RECONNECT_BACKOFF, backoff * 2)


def start() -> None:
    """Schedule the background WS task once on the running event loop."""
    global _STARTED, _TASK
    if _STARTED:
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    _TASK = loop.create_task(_runner(), name="pm_market_ws_runner")
    _STARTED = True
    logger.info("[pm_market_ws] background bridge scheduled")
