from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from src.services.copytrader_v2.official_client import (
    PolymarketOfficialClient,
    get_default_client,
)

logger = logging.getLogger("copytrader_v2.wallet_analyzer")

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "weather": ["weather", "temperature", "rain", "snow", "hurricane", "storm", "highest", "lowest", "degrees", "forecast"],
    "sports": ["nba", "nfl", "mlb", "nhl", "soccer", "fifa", "uefa", "premier", "champions", "match", "vs", "wins", "tennis", "boxing", "ufc", "final", "playoff", "goal", "points"],
    "crypto": ["btc", "bitcoin", "eth", "ethereum", "sol", "doge", "crypto", "halving", "token", "onchain", "defi"],
    "politics": ["election", "president", "trump", "biden", "vote", "senate", "congress", "primary", "poll", "governor", "mayor", "parliament", "referendum"],
    "entertainment": ["oscar", "grammy", "movie", "song", "album", "tv", "show", "box office", "series"],
    "macro": ["fed", "fomc", "inflation", "cpi", "gdp", "unemployment", "rate", "tariff"],
}


def detect_category(*parts: Any) -> str:
    text = " ".join(str(p or "") for p in parts).lower()
    if not text.strip():
        return "other"
    best, best_hits = "other", 0
    for cat, kws in CATEGORY_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw in text)
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


class WalletAnalyzer:
    def __init__(self, client: PolymarketOfficialClient | None = None) -> None:
        self._client = client or get_default_client()

    def compute_full_metrics(self, address: str) -> dict[str, Any]:
        """4-dim smart score using closed-positions: profit 40% + win_rate 30% + drawdown_ctrl 20% + activity 10%.

        This is the canonical V2 scoring method. All daemon precompute cycles use this.
        """
        if not address or not address.startswith("0x"):
            return self._empty_metrics(address)
        closed = self._client.get_closed_positions(address, limit=500)
        activity = self._client.get_activity(address, limit=200)
        positions = self._client.get_positions(address, limit=200)
        return self._build_full_metrics(address, closed, activity, positions)

    def compute_metrics(self, address: str) -> dict[str, Any]:
        """Backward-compat shim — delegates to compute_full_metrics."""
        return self.compute_full_metrics(address)

    def _empty_metrics(self, address: str) -> dict[str, Any]:
        return {
            "address": address,
            "score": 0.0,
            "value_usd": 0.0,
            "pnl_realized_usd": 0.0,
            "pnl_unrealized_usd": 0.0,
            "pnl_total_usd": 0.0,
            "trade_count_total": 0,
            "trade_count_30d": 0,
            "win_rate": 0.0,
            "avg_position_size_usd": 0.0,
            "open_positions": 0,
            "category_distribution": {},
            "primary_category": "other",
            "last_active_ts": 0,
            "is_likely_bot": False,
            "data_complete": False,
            "profit_score": 0.0,
            "win_rate_score": 0.0,
            "drawdown_score": 0.0,
            "activity_score": 0.0,
            "max_drawdown_usd": 0.0,
            "closed_positions_count": 0,
        }

    def _build_full_metrics(
        self,
        address: str,
        closed: list[dict],
        activity: list[dict],
        positions: list[dict],
    ) -> dict[str, Any]:
        """4-dim weighted smart score using official Data API closed-positions.

        Dimension weights: profit 40% + win_rate 30% + drawdown_ctrl 20% + activity 10%.
        All dimensions are normalized to [0, 1] before weighting.
        """
        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff_30d = now_ts - 30 * 86400

        # ── Dimension 1: Profit (40%) ──────────────────────────────────
        # Use sum of realizedPnl from closed-positions (official Data API field)
        realized_pnl = sum(_safe_float(c.get("realizedPnl") or 0) for c in closed)
        profit_norm = min(max(realized_pnl, 0) / 50_000.0, 1.0)  # $50k = 1.0

        # ── Dimension 2: Win Rate (30%) ────────────────────────────────
        closed_wins = sum(1 for c in closed if _safe_float(c.get("realizedPnl") or 0) > 0)
        closed_total = len(closed)
        win_rate = (closed_wins / closed_total) if closed_total > 0 else 0.0
        # Normalize: 50% floor → 1.0 at 100%; require ≥10 samples
        win_rate_norm = max(0.0, min((win_rate - 0.5) / 0.5, 1.0)) if closed_total >= 10 else 0.0

        # ── Dimension 3: Drawdown Control (20%) ───────────────────────
        # Use max single realized loss as a proxy for max drawdown
        losses = [abs(_safe_float(c.get("realizedPnl") or 0)) for c in closed if _safe_float(c.get("realizedPnl") or 0) < 0]
        max_single_loss = max(losses) if losses else 0.0
        drawdown_norm = 1.0 - min(max_single_loss / 10_000.0, 1.0)  # $10k loss → 0.0

        # ── Dimension 4: Activity (10%) ────────────────────────────────
        trades_30d = 0
        trades_total = 0
        last_active: int = 0
        timestamps: list[float] = []
        for a in activity:
            if (a.get("type") or "").upper() != "TRADE":
                continue
            ts = _safe_float(a.get("timestamp"))
            if ts > 0:
                timestamps.append(ts)
                last_active = max(last_active, int(ts))
            trades_total += 1
            if ts >= cutoff_30d:
                trades_30d += 1
        activity_norm = min(trades_30d / 100.0, 1.0)  # 100 trades/30d = 1.0

        # ── Composite Smart Score ──────────────────────────────────────
        score = (
            profit_norm * 40.0
            + win_rate_norm * 30.0
            + drawdown_norm * 20.0
            + activity_norm * 10.0
        )

        # Bot penalty: subtract 20 points if detected as bot
        is_bot = self._detect_bot(timestamps, trades_30d)
        if is_bot:
            score = max(0.0, score - 20.0)

        # ── Category detection ─────────────────────────────────────────
        category_counts: Counter[str] = Counter()
        for p in positions:
            cat = detect_category(
                p.get("slug"), p.get("title"), p.get("question"),
                p.get("eventSlug"), p.get("outcome"),
            )
            if cat != "other":
                category_counts[cat] += 1
        for c in closed:
            cat = detect_category(
                c.get("slug"), c.get("title"), None,
                c.get("eventSlug"), c.get("outcome"),
            )
            if cat != "other":
                category_counts[cat] += 1

        primary_category = "other"
        if category_counts:
            primary_category = category_counts.most_common(1)[0][0]

        return {
            "address": address,
            "score": round(score, 2),
            "pnl_realized_usd": round(realized_pnl, 2),
            "pnl_unrealized_usd": 0.0,
            "pnl_total_usd": round(realized_pnl, 2),
            "value_usd": 0.0,
            "win_rate": round(win_rate, 4),
            "trade_count_30d": trades_30d,
            "trade_count_total": (closed_total + trades_total),
            "max_drawdown_usd": round(max_single_loss, 2),
            "open_positions": len(positions),
            "avg_position_size_usd": 0.0,
            "primary_category": primary_category,
            "category_distribution": dict(category_counts),
            "last_active_ts": last_active,
            "is_likely_bot": is_bot,
            "data_complete": True,
            # Score dimension breakdown for UI display
            "profit_score": round(profit_norm, 4),
            "win_rate_score": round(win_rate_norm, 4),
            "drawdown_score": round(drawdown_norm, 4),
            "activity_score": round(activity_norm, 4),
            "closed_positions_count": closed_total,
        }

    def _build_metrics(
        self,
        address: str,
        positions: list[dict],
        value_summary: dict,
        activity: list[dict],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).timestamp()
        cutoff_30d = now - 30 * 86400

        unrealized = 0.0
        position_size_total = 0.0
        category_counts: Counter[str] = Counter()
        position_wins = 0
        position_decided = 0
        for p in positions:
            unrealized += _safe_float(p.get("cashPnl") or p.get("unrealizedPnl"))
            sz = _safe_float(p.get("currentValue") or p.get("size"))
            position_size_total += sz
            pnl_snapshot = _safe_float(p.get("cashPnl") or p.get("realizedPnl") or p.get("percentPnl"))
            has_signal = abs(pnl_snapshot) > 1e-9 or (
                _safe_float(p.get("curPrice") or p.get("price")) > 0
                and _safe_float(p.get("avgPrice") or p.get("avgEntryPrice")) > 0
            )
            if has_signal:
                position_decided += 1
                if pnl_snapshot > 0:
                    position_wins += 1
            cat = detect_category(
                p.get("slug"),
                p.get("title"),
                p.get("question"),
                p.get("eventSlug"),
                p.get("outcome"),
            )
            if cat != "other":
                category_counts[cat] += 1

        trades_30d = 0
        trades_total = 0
        wins = 0
        decided = 0
        last_active = 0
        timestamps: list[float] = []
        for a in activity:
            if (a.get("type") or "").upper() != "TRADE":
                continue
            ts = _safe_float(a.get("timestamp"))
            if ts > 0:
                timestamps.append(ts)
                last_active = max(last_active, int(ts))
            trades_total += 1
            if ts >= cutoff_30d:
                trades_30d += 1
            pnl_field = a.get("pnl") or a.get("realizedPnl")
            if pnl_field is not None:
                decided += 1
                if _safe_float(pnl_field) > 0:
                    wins += 1
            cat = detect_category(
                a.get("slug"),
                a.get("title"),
                a.get("question"),
                a.get("eventSlug"),
                a.get("outcome"),
            )
            if cat != "other":
                category_counts[cat] += 1

        decided_total = decided + position_decided
        wins_total = wins + position_wins
        win_rate = (wins_total / decided_total) if decided_total > 0 else 0.0
        realized = _safe_float(value_summary.get("realizedPnl") or value_summary.get("realized_pnl"))
        value_usd = _safe_float(value_summary.get("value") or value_summary.get("currentValue"))

        avg_size = (position_size_total / max(1, len(positions))) if positions else 0.0

        is_bot = self._detect_bot(timestamps, trades_30d)

        primary_category = "other"
        if category_counts:
            primary_category = category_counts.most_common(1)[0][0]
        category_distribution = dict(category_counts)

        score = self._score(
            pnl_total=realized + unrealized,
            value_usd=value_usd,
            win_rate=win_rate,
            trades_30d=trades_30d,
            decided=decided_total,
            is_bot=is_bot,
            recency_seconds=(now - last_active) if last_active > 0 else 1e9,
            trades_total=trades_total,
        )

        return {
            "address": address,
            "score": round(score, 2),
            "value_usd": round(value_usd, 2),
            "pnl_realized_usd": round(realized, 2),
            "pnl_unrealized_usd": round(unrealized, 2),
            "pnl_total_usd": round(realized + unrealized, 2),
            "trade_count_total": trades_total,
            "trade_count_30d": trades_30d,
            "win_rate": round(win_rate, 4),
            "avg_position_size_usd": round(avg_size, 2),
            "open_positions": len(positions),
            "category_distribution": category_distribution,
            "primary_category": primary_category,
            "last_active_ts": last_active,
            "is_likely_bot": is_bot,
            "data_complete": True,
        }

    def _detect_bot(self, timestamps: list[float], trades_30d: int) -> bool:
        if trades_30d > 800:
            return True
        if len(timestamps) < 20:
            return False
        sorted_ts = sorted(timestamps)
        gaps = [sorted_ts[i] - sorted_ts[i - 1] for i in range(1, len(sorted_ts))]
        if not gaps:
            return False
        avg = sum(gaps) / len(gaps)
        if avg <= 0:
            return False
        var = sum((g - avg) ** 2 for g in gaps) / len(gaps)
        std = math.sqrt(var)
        cv = std / avg if avg > 0 else 0
        return cv < 0.25 and avg < 600

    def _score(
        self,
        pnl_total: float,
        value_usd: float,
        win_rate: float,
        trades_30d: int,
        decided: int,
        is_bot: bool,
        recency_seconds: float,
        trades_total: int = 0,
    ) -> float:
        pnl_score = min(max(pnl_total, 0) / 100_000.0, 1.0) * 20.0
        value_score = min(value_usd / 50_000.0, 1.0) * 10.0
        win_score = 0.0
        if decided >= 10:
            win_score = max(0.0, min((win_rate - 0.5) / 0.3, 1.0)) * 15.0
        activity_score = min(trades_30d / 50.0, 1.0) * 15.0
        richness_score = min(trades_total / 100.0, 1.0) * 20.0
        recency_score = 0.0
        if recency_seconds < 86400 * 7:
            recency_score = 20.0
        elif recency_seconds < 86400 * 30:
            recency_score = 15.0
        elif recency_seconds < 86400 * 90:
            recency_score = 10.0
        elif recency_seconds < 86400 * 365:
            recency_score = 5.0
        sample_bonus = 10.0 if decided >= 30 else (5.0 if decided >= 10 else 0.0)
        bot_penalty = 25.0 if is_bot else 0.0
        return max(0.0, pnl_score + value_score + win_score + activity_score + richness_score + recency_score + sample_bonus - bot_penalty)


def filter_and_rank(
    metrics_list: list[dict[str, Any]],
    *,
    min_pnl_usd: float = 0.0,
    min_trades_30d: int = 0,
    min_win_rate: float = 0.0,
    min_score: float = 0.0,
    category: str | None = None,
    exclude_bots: bool = True,
    sort_by: str = "score",
    sort_desc: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in metrics_list:
        if exclude_bots and m.get("is_likely_bot"):
            continue
        if m.get("pnl_total_usd", 0.0) < min_pnl_usd:
            continue
        if m.get("trade_count_30d", 0) < min_trades_30d:
            continue
        if m.get("win_rate", 0.0) < min_win_rate:
            continue
        if m.get("score", 0.0) < min_score:
            continue
        if category and category != "any":
            if m.get("primary_category") != category and category not in (m.get("category_distribution") or {}):
                continue
        out.append(m)
    out.sort(key=lambda x: x.get(sort_by, 0), reverse=sort_desc)
    return out[:limit]
