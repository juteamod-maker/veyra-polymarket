"""T510 P1-2: Polymarket Sports Channel WebSocket bridge.

Maintains a single asyncio background task that:

  * connects to ``wss://sports-api.polymarket.com/ws`` (no subscribe payload —
    the server starts streaming all active sports events immediately);
  * server sends ``ping`` every 5 s; we MUST respond with ``pong`` within 10 s
    or the connection is closed (per docs.polymarket.com /ws/overview);
  * consumes ``sport_result`` events and writes the latest snapshot per
    ``event_id`` into in-memory ``_LIVE_SPORTS``;
  * reconnects with exponential backoff on disconnect.

Public helpers:
    - ``start()``                  — schedule the background task once.
    - ``get_event(event_id)``      — return latest snapshot or ``None``.
    - ``snapshot()``               — shallow copy of the whole map.
    - ``stats()``                  — observability snapshot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger("pm_sports_ws")

WS_URL = "wss://sports-api.polymarket.com/ws"
MAX_RECONNECT_BACKOFF = 30.0

_LIVE_SPORTS: dict[str, dict[str, Any]] = {}
_STARTED = False
_TASK: asyncio.Task | None = None


def get_event(event_id: str) -> dict[str, Any] | None:
    if not event_id:
        return None
    rec = _LIVE_SPORTS.get(str(event_id))
    return dict(rec) if rec else None


def snapshot() -> dict[str, dict[str, Any]]:
    return {k: dict(v) for k, v in _LIVE_SPORTS.items()}


def stats() -> dict[str, Any]:
    return {
        "started": _STARTED,
        "live_events": len(_LIVE_SPORTS),
        "ws_url": WS_URL,
    }


def _handle_message(raw: str | bytes) -> None:
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
    for evt in items:
        if not isinstance(evt, dict):
            continue
        ev_type = (evt.get("event_type") or evt.get("type") or "sport_result").lower()
        if ev_type != "sport_result":
            continue
        eid = str(
            evt.get("event_id")
            or evt.get("id")
            or evt.get("game_id")
            or ""
        ).strip()
        if not eid:
            continue
        rec = dict(evt)
        rec["_received_ts"] = time.time()
        _LIVE_SPORTS[eid] = rec


async def _ws_session() -> None:
    import websockets

    async with websockets.connect(
        WS_URL,
        ping_interval=None,        # server-driven heartbeat: WE answer its ping
        max_size=2 * 1024 * 1024,
        open_timeout=10,
        close_timeout=5,
    ) as ws:
        logger.info("[pm_sports_ws] connected; awaiting stream")
        async for msg in ws:
            text = msg if isinstance(msg, str) else msg.decode("utf-8", errors="ignore")
            stripped = text.strip().lower()
            if stripped == "ping":
                try:
                    await ws.send("pong")
                except Exception:
                    return
                continue
            _handle_message(text)


async def _runner() -> None:
    backoff = 1.0
    while True:
        try:
            await _ws_session()
            backoff = 1.0
            logger.info("[pm_sports_ws] session ended cleanly; reconnecting")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[pm_sports_ws] session error: %s; backoff %.1fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(MAX_RECONNECT_BACKOFF, backoff * 2)


def start() -> None:
    global _STARTED, _TASK
    if _STARTED:
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    _TASK = loop.create_task(_runner(), name="pm_sports_ws_runner")
    _STARTED = True
    logger.info("[pm_sports_ws] background bridge scheduled")
