from __future__ import annotations

import logging
import re
import time
from threading import Lock
from typing import Any

import requests

logger = logging.getLogger("copytrader_v2.official_client")

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

DEFAULT_TIMEOUT = 12


class _TTLCache:
    def __init__(self, ttl_seconds: float = 60.0, max_entries: int = 2048) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            expires, value = entry
            if expires < time.time():
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._store) >= self._max:
                oldest = sorted(self._store.items(), key=lambda kv: kv[1][0])[: self._max // 4]
                for k, _ in oldest:
                    self._store.pop(k, None)
            self._store[key] = (time.time() + self._ttl, value)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


class PolymarketOfficialClient:
    def __init__(
        self,
        cache_ttl_seconds: float = 60.0,
        request_timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 2,
    ) -> None:
        self._timeout = request_timeout
        self._max_retries = max_retries
        self._cache = _TTLCache(ttl_seconds=cache_ttl_seconds)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "VeYra.Trade-CopytraderV2/1.0"})

    def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        cache_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        if cache_key:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.get(
                    url, params=params, timeout=timeout or self._timeout
                )
                if resp.status_code == 429:
                    backoff = min(0.5 * (2**attempt), 4.0)
                    logger.warning("Rate limited %s; backoff %.1fs", url, backoff)
                    time.sleep(backoff)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if cache_key:
                    self._cache.set(cache_key, data)
                return data
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    time.sleep(0.4 * (attempt + 1))
                    continue
        if last_exc:
            logger.warning("GET failed %s: %s", url, last_exc)
        return None

    # ── Wallet endpoints ─────────────────────────────────────────────
    def get_positions(self, address: str, limit: int = 200) -> list[dict]:
        if not address:
            return []
        data = self._get(
            f"{DATA_API}/positions",
            params={"user": address, "limit": limit},
            cache_key=f"pos:{address.lower()}:{limit}",
        )
        if not data:
            return []
        if isinstance(data, list):
            return data
        return data.get("data", []) or []

    def get_value(self, address: str) -> dict[str, Any]:
        if not address:
            return {}
        data = self._get(
            f"{DATA_API}/value",
            params={"user": address},
            cache_key=f"val:{address.lower()}",
        )
        if not data:
            return {}
        if isinstance(data, list):
            return data[0] if data else {}
        return data

    def get_activity(
        self,
        address: str,
        limit: int = 200,
        offset: int = 0,
        types: list[str] | None = None,
    ) -> list[dict]:
        if not address:
            return []
        params: dict[str, Any] = {"user": address, "limit": limit, "offset": offset}
        if types:
            params["type"] = ",".join(types)
        data = self._get(
            f"{DATA_API}/activity",
            params=params,
            cache_key=f"act:{address.lower()}:{limit}:{offset}:{','.join(types or [])}",
        )
        if not data:
            return []
        if isinstance(data, list):
            return data
        return data.get("data", []) or []

    def get_trades(
        self,
        address: str | None = None,
        market: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if address:
            params["user"] = address
        if market:
            params["market"] = market
        cache_key = f"trd:{(address or '').lower()}:{market or ''}:{limit}"
        data = self._get(
            f"{DATA_API}/trades",
            params=params,
            cache_key=cache_key,
        )
        if not data:
            return []
        if isinstance(data, list):
            return data
        return data.get("data", []) or []

    def get_closed_positions(self, address: str, limit: int = 500) -> list[dict]:
        """GET /closed-positions?user={address} — realized PnL, win/loss records."""
        if not address:
            return []
        data = self._get(
            f"{DATA_API}/closed-positions",
            params={"user": address, "limit": limit},
            cache_key=f"cpos:{address.lower()}:{limit}",
        )
        if not data:
            return []
        if isinstance(data, list):
            return data
        return data.get("data", []) or []

    def get_holders(self, condition_id: str, limit: int = 100) -> list[dict]:
        """GET /holders?market={conditionId} — top holders by token position size."""
        if not condition_id:
            return []
        data = self._get(
            f"{DATA_API}/holders",
            params={"market": condition_id, "limit": limit},
            cache_key=f"holders:{condition_id.lower()}:{limit}",
        )
        if not data:
            return []
        if isinstance(data, list):
            return data
        return data.get("data", []) or []

    def get_hot_markets(self, limit: int = 20) -> list[dict]:
        """GET /markets from Gamma API ordered by volume — used for discovery seeding."""
        data = self._get(
            f"{GAMMA_API}/markets",
            params={"limit": limit, "active": "true", "order": "volumeNum", "ascending": "false"},
            cache_key=f"hot_markets:{limit}",
            timeout=20.0,
        )
        if not data:
            return []
        if isinstance(data, list):
            return data
        return data.get("data", []) or []

    # ── Market endpoints ─────────────────────────────────────────────
    def get_market_by_condition(self, condition_id: str) -> dict[str, Any]:
        if not condition_id:
            return {}
        data = self._get(
            f"{GAMMA_API}/markets",
            params={"condition_ids": condition_id},
            cache_key=f"mkt:{condition_id}",
        )
        if not data:
            return {}
        if isinstance(data, list):
            return data[0] if data else {}
        return data

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        if not token_id:
            return {}
        data = self._get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            cache_key=f"book:{token_id}",
        )
        return data or {}

    def get_best_bid_ask(self, token_id: str) -> tuple[float | None, float | None]:
        book = self.get_orderbook(token_id)
        if not book:
            return None, None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        return best_bid, best_ask

    # ── Discovery: derive candidate wallets from market trades ──────
    def get_market_trades(self, condition_id: str, limit: int = 200) -> list[dict]:
        return self.get_trades(market=condition_id, limit=limit)

    def get_leaderboard(self, limit: int = 50) -> list[dict]:
        return self.get_leaderboard_by_category(category="overall", limit=limit)

    def get_data_leaderboard(self, period: str = "30d", limit: int = 100) -> list[dict]:
        """T510 P1-3: Direct read of Polymarket Data API leaderboard.

        Hits ``GET https://data-api.polymarket.com/leaderboard?period={period}&limit={limit}``
        which is much faster than scraping holders across 20 hot markets
        (Phase A normally takes ~8s; this returns in <500ms).

        Returns: list of dicts each containing at least a `proxyWallet` (lower-cased).
        Returns ``[]`` on any error so callers can fall back gracefully.
        """
        period_norm = (period or "30d").strip().lower()
        valid = {"1d", "7d", "30d", "all"}
        if period_norm not in valid:
            period_norm = "30d"
        cap = max(10, min(200, int(limit or 100)))
        cache_key = f"data_lb:{period_norm}:{cap}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        url = f"{DATA_API}/v1/leaderboard"
        try:
            data = self._get(url, params={"period": period_norm, "limit": cap}, timeout=6.0)
        except Exception:
            data = None
        if data is None:
            return []
        rows = data if isinstance(data, list) else (
            data.get("data") or data.get("results") or data.get("leaderboard") or []
            if isinstance(data, dict) else []
        )
        out: list[dict] = []
        if isinstance(rows, list):
            for r in rows:
                if not isinstance(r, dict):
                    continue
                addr = str(
                    r.get("proxyWallet")
                    or r.get("address")
                    or r.get("user")
                    or r.get("wallet")
                    or ""
                ).strip().lower()
                if not (addr.startswith("0x") and len(addr) == 42):
                    continue
                rec = dict(r)
                rec["proxyWallet"] = addr
                out.append(rec)
        if out:
            self._cache.set(cache_key, out)
        return out

    def _extract_next_build_id(self, html: str) -> str:
        m = re.search(r'"buildId":"([^"]+)"', html)
        return m.group(1) if m else ""

    def _extract_next_data_json(self, html: str) -> dict[str, Any]:
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not m:
            return {}
        raw = m.group(1)
        try:
            return requests.models.complexjson.loads(raw)
        except Exception:
            return {}

    def _parse_leaderboard_rows(
        self,
        payload: dict[str, Any],
        category: str,
        sort_key: str,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page_props = payload.get("pageProps") if isinstance(payload, dict) else {}
        dehydrated = page_props.get("dehydratedState") if isinstance(page_props, dict) else {}
        queries = dehydrated.get("queries") if isinstance(dehydrated, dict) else []
        for q in queries or []:
            qk = q.get("queryKey") if isinstance(q, dict) else None
            if not isinstance(qk, list) or len(qk) < 5:
                continue
            if qk[0] != "/leaderboard":
                continue
            if qk[1] != sort_key:
                continue
            if qk[2] != "1d":
                continue
            if str(qk[4]) != str(category):
                continue
            rows = ((q.get("state") or {}).get("data") or []) if isinstance(q, dict) else []
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    addr = str(row.get("proxyWallet") or "").strip().lower()
                    if not (addr.startswith("0x") and len(addr) == 42):
                        continue
                    out.append(row)
            break
        return out

    def get_leaderboard_by_category(
        self,
        category: str = "overall",
        period: str = "today",
        sort_by: str = "profit",
        limit: int = 20,
    ) -> list[dict]:
        cat = (category or "overall").strip().lower()
        if cat in ("any", "all"):
            cat = "overall"
        pd = (period or "today").strip().lower()
        if pd not in ("today",):
            pd = "today"
        sb = (sort_by or "profit").strip().lower()
        if sb not in ("profit", "volume"):
            sb = "profit"

        cache_key = f"pm_lb:{cat}:{pd}:{sb}:{max(1, int(limit or 20))}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Polymarket currently publishes leaderboard data in Next data payloads
        # under /zh/leaderboard/... and /_next/data/<buildId>/zh/leaderboard/... JSON.
        page_url = f"https://polymarket.com/zh/leaderboard/{cat}/{pd}/{sb}"
        try:
            resp = self._session.get(page_url, timeout=max(self._timeout, 15))
            resp.raise_for_status()
            html = resp.text
            payload = self._extract_next_data_json(html)
            rows = self._parse_leaderboard_rows(payload, cat, sb)

            if not rows:
                build_id = self._extract_next_build_id(html)
                if build_id:
                    data_url = f"https://polymarket.com/_next/data/{build_id}/zh/leaderboard/{cat}/{pd}/{sb}.json"
                    data_resp = self._session.get(data_url, timeout=max(self._timeout, 15))
                    if data_resp.ok:
                        data_payload = data_resp.json()
                        rows = self._parse_leaderboard_rows(data_payload, cat, sb)

            seen: set[str] = set()
            wallets: list[dict[str, Any]] = []
            for row in rows:
                addr = str(row.get("proxyWallet") or "").strip().lower()
                if not (addr.startswith("0x") and len(addr) == 42):
                    continue
                if addr in seen:
                    continue
                seen.add(addr)
                wallets.append(
                    {
                        "proxyWallet": addr,
                        "name": row.get("name") or row.get("pseudonym") or "",
                        "pnl": row.get("pnl"),
                        "volume": row.get("volume"),
                        "rank": row.get("rank"),
                        "category": cat,
                        "source": "polymarket_leaderboard",
                    }
                )
                if len(wallets) >= max(1, int(limit or 20)):
                    break

            self._cache.set(cache_key, wallets)
            return wallets
        except Exception as exc:
            logger.warning("[CT-leaderboard] failed category=%s err=%s", cat, exc)
            return []


_default_client: PolymarketOfficialClient | None = None


def get_default_client() -> PolymarketOfficialClient:
    global _default_client
    if _default_client is None:
        _default_client = PolymarketOfficialClient()
    return _default_client
