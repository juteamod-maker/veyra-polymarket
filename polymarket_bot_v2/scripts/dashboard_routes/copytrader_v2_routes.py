from __future__ import annotations

import hashlib
import json
import math
import zlib
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from scripts.dashboard_routes._shared import (
    db_connect,
    get_request_identity,
    logger,
)
from src.services.copytrader_v2.config import get as copytrader_cfg_get
from src.services.copytrader_v2.official_client import get_default_client
from src.services.copytrader_v2.wallet_analyzer import (
    CATEGORY_KEYWORDS,
    WalletAnalyzer,
    detect_category,
)
import asyncio

router = APIRouter()

_client = get_default_client()
_analyzer = WalletAnalyzer(client=_client)
# Backward-compat alias kept for tests and legacy monkeypatch hooks.
cfg_get = copytrader_cfg_get


def _normalize_wallet_addr(raw: Any) -> str:
    v = str(raw or "").strip().lower()
    if v.startswith("0x") and len(v) == 42:
        return v
    return ""

_LEADERBOARD_CATEGORIES = [
    "overall",
    "politics",
    "sports",
    "crypto",
    "finance",
    "weather",
    "technology",
    "economy",
    "science",
    "world",
    "culture",
    "business",
]


def _require_identity(request: Request) -> dict:
    ident = get_request_identity(request)
    if not ident or not ident.get("authenticated"):
        raise HTTPException(status_code=401, detail="auth_required")
    return ident


def _resolve_owner_wallet(request: Request) -> str:
    ident = _require_identity(request)
    if bool(ident.get("is_admin")):
        pm_wallet = _normalize_wallet_addr(
            ident.get("pm_wallet_address")
            or ident.get("polymarket_wallet")
            or ident.get("funder_address")
            or ident.get("main_wallet")
        )
        if pm_wallet:
            return pm_wallet
        return "__admin__"

    wallet = _normalize_wallet_addr(ident.get("wallet_address"))
    if wallet == "__admin__":
        return "__admin__"
    if not wallet:
        session_token = (request.cookies.get("user_session") or "").strip()
        if session_token:
            digest = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
            return f"session_{digest[:40]}"
        raise HTTPException(status_code=401, detail="no_owner_identity")
    return wallet


def _resolve_self_wallet(request: Request) -> str:
    """Resolve the canonical self wallet used for self-follow blocking.

    Prefer funder/Safe wallet from users table, then polymarket/main/session wallets.
    """
    ident = _require_identity(request)
    if bool(ident.get("is_admin")):
        return _normalize_wallet_addr(
            ident.get("pm_wallet_address")
            or ident.get("polymarket_wallet")
            or ident.get("funder_address")
            or ident.get("main_wallet")
        )

    login_wallet = _normalize_wallet_addr(ident.get("wallet_address"))
    if login_wallet:
        try:
            with db_connect() as conn:
                row = conn.execute(
                    "SELECT funder_address, polymarket_wallet, main_wallet FROM users WHERE wallet_address=?",
                    (login_wallet,),
                ).fetchone()
            if row:
                funder = _normalize_wallet_addr(row["funder_address"])
                if funder:
                    return funder
                pm_wallet = _normalize_wallet_addr(row["polymarket_wallet"])
                if pm_wallet:
                    return pm_wallet
                main_wallet = _normalize_wallet_addr(row["main_wallet"])
                if main_wallet:
                    return main_wallet
        except Exception:
            pass

    return (
        _normalize_wallet_addr(ident.get("funder_address"))
        or _normalize_wallet_addr(ident.get("polymarket_wallet"))
        or _normalize_wallet_addr(ident.get("pm_wallet_address"))
        or _normalize_wallet_addr(ident.get("main_wallet"))
        or login_wallet
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_json_ts(value: Any) -> Any:
    """Convert DB datetime values to JSON-safe ISO8601 strings."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.isoformat()
    return value


def _normalize_category(raw: str | None) -> str:
    cat = (raw or "overall").strip().lower()
    if cat in {"any", ""}:
        return "overall"
    if cat in {"all", "aggregate", "aggregated"}:
        return "all"
    alias = {
        "tech": "technology",
        "pop-culture": "culture",
        "pop_culture": "culture",
        "macro": "economy",
    }
    return alias.get(cat, cat)


def _store_cached_metrics(address: str, metrics: dict[str, Any]) -> None:
    """Persist newly computed analytics metrics into the wallet pool table."""
    if not address:
        return
    with db_connect() as conn:
        conn.execute(
            "UPDATE copytrader_v2_wallet_pool SET last_metrics_json=?, score=?, primary_category=?, "
            "is_likely_bot=?, pnl_realized_usd=?, win_rate_f=?, trade_count_30d=?, max_drawdown_usd=?, "
            "last_scored_at=CURRENT_TIMESTAMP WHERE address=?",
            (
                json.dumps(metrics),
                float(metrics.get("score") or 0.0),
                metrics.get("primary_category") or "other",
                bool(metrics.get("is_likely_bot")),
                float(metrics.get("pnl_realized_usd") or 0.0),
                float(metrics.get("win_rate") or 0.0),
                int(metrics.get("trade_count_30d") or 0),
                float(metrics.get("max_drawdown_usd") or 0.0),
                address,
            ),
        )
        conn.commit()


def _load_user_exclude_bots(owner_wallet_address: str) -> bool:
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT settings_json FROM copytrader_v2_settings WHERE owner_wallet_address=?",
                (owner_wallet_address,),
            ).fetchone()
        if not row or not row["settings_json"]:
            return True
        parsed = json.loads(row["settings_json"])
        if not isinstance(parsed, dict):
            return True
        return bool(parsed.get("exclude_bots", True))
    except Exception:
        return True


# ── Wallet discovery / analytics ──────────────────────────────────────


@router.get("/api/v2/copytrader/leaderboard/status")
async def leaderboard_status(request: Request) -> JSONResponse:
    """Pool health: total wallets, last scored timestamp, approximate next refresh."""
    _require_identity(request)
    def _db():
        with db_connect() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM copytrader_v2_wallet_pool"
            ).fetchone()
            scored_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM copytrader_v2_wallet_pool WHERE last_scored_at IS NOT NULL"
            ).fetchone()
            freshness_row = conn.execute(
                "SELECT MAX(last_scored_at) as latest FROM copytrader_v2_wallet_pool"
            ).fetchone()
        refresh_interval = int(copytrader_cfg_get("leaderboard_refresh_interval_seconds") or 600)
        return JSONResponse(content={
            "total_wallets": int(total_row["cnt"] if total_row else 0),
            "scored_wallets": int(scored_row["cnt"] if scored_row else 0),
            "cache_updated_at": _to_json_ts(freshness_row["latest"] if freshness_row else None),
            "refresh_interval_seconds": refresh_interval,
        })
    return await asyncio.to_thread(_db)


@router.get("/api/v2/copytrader/wallets/search")
async def wallets_search(
    request: Request,
    category: str = Query("overall"),
    sort_by: str = Query("score"),
    sort_dir: str = Query("desc"),
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    min_pnl_usd: float = Query(0.0),
    min_trades_30d: int = Query(0, ge=0),
    min_win_rate: float = Query(0.0, ge=0.0, le=1.0),
    exclude_bots: bool | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
) -> JSONResponse:
    """Pure DB read — data is pre-computed by the background daemon every 10 min."""
    ident = get_request_identity(request)
    is_authenticated = bool(ident and ident.get("authenticated"))
    owner_wallet_address = _resolve_owner_wallet(request) if is_authenticated else "__public__"
    cat = _normalize_category(category)
    if is_authenticated:
        exclude_bots_effective = (
            _load_user_exclude_bots(owner_wallet_address)
            if exclude_bots is None
            else bool(exclude_bots)
        )
    else:
        # Anonymous prefetch should never expose bot-like wallets by default.
        exclude_bots_effective = True if exclude_bots is None else bool(exclude_bots)

    conditions: list[str] = ["score > 0"]
    params: list[Any] = []
    if cat not in {"overall", "all"}:
        conditions.append("primary_category = ?")
        params.append(cat)
    if min_score > 0:
        conditions.append("score >= ?")
        params.append(float(min_score))
    if min_pnl_usd > 0:
        conditions.append("pnl_realized_usd >= ?")
        params.append(float(min_pnl_usd))
    if min_trades_30d > 0:
        conditions.append("trade_count_30d >= ?")
        params.append(int(min_trades_30d))
    if min_win_rate > 0:
        conditions.append("win_rate_f >= ?")
        params.append(float(min_win_rate))
    if exclude_bots_effective:
        conditions.append("(is_likely_bot IS NOT TRUE)")

    where = " AND ".join(conditions)

    sort_col = {
        "score": "score",
        "pnl_total_usd": "pnl_realized_usd",
        "pnl_realized_usd": "pnl_realized_usd",
        "pnl": "pnl_realized_usd",
        "win_rate": "win_rate_f",
        "trade_count_30d": "trade_count_30d",
        "trades_30d": "trade_count_30d",
        # T502 Wave 7B: drawdown lives in JSON metrics — extract for ORDER BY.
        "drawdown": "((last_metrics_json::jsonb->>'max_drawdown_usd')::float)",
        "max_drawdown_usd": "((last_metrics_json::jsonb->>'max_drawdown_usd')::float)",
    }.get(sort_by, "score")
    sort_direction = "ASC" if str(sort_dir or "").lower() == "asc" else "DESC"

    offset = (page - 1) * limit
    def _db():

        with db_connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM copytrader_v2_wallet_pool WHERE {where}",
                tuple(params),
            ).fetchone()
            total = int(total_row["cnt"] if total_row else 0)

            rows = conn.execute(
                f"SELECT address, source, last_metrics_json, score, primary_category, "
                f"is_likely_bot, last_scored_at, pnl_realized_usd, win_rate_f, trade_count_30d "
                f"FROM copytrader_v2_wallet_pool WHERE {where} "
                f"ORDER BY {sort_col} {sort_direction} NULLS LAST LIMIT ? OFFSET ?",
                tuple(params) + (limit, offset),
            ).fetchall()

            freshness_row = conn.execute(
                "SELECT MAX(last_scored_at) as latest FROM copytrader_v2_wallet_pool"
            ).fetchone()

        wallets: list[dict[str, Any]] = []
        for row in rows:
            try:
                metrics = json.loads(row["last_metrics_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metrics = {}
            wallet: dict[str, Any] = {
                "address": row["address"],
                "score": float(row["score"] or 0),
                "primary_category": row["primary_category"] or "other",
                "is_likely_bot": bool(row["is_likely_bot"]),
                "source": row["source"] or "holders",
                "last_scored_at": _to_json_ts(row["last_scored_at"]),
                "pnl_realized_usd": float(row["pnl_realized_usd"] or metrics.get("pnl_realized_usd") or 0),
                "pnl_total_usd": float(metrics.get("pnl_total_usd") or row["pnl_realized_usd"] or 0),
                "win_rate": float(row["win_rate_f"] or metrics.get("win_rate") or 0),
                "trade_count_30d": int(row["trade_count_30d"] or metrics.get("trade_count_30d") or 0),
                "trade_count_total": int(metrics.get("trade_count_total") or 0),
                "max_drawdown_usd": float(metrics.get("max_drawdown_usd") or 0),
                "closed_positions_count": int(metrics.get("closed_positions_count") or 0),
                "open_positions": int(metrics.get("open_positions") or 0),
                "category_distribution": metrics.get("category_distribution") or {},
                "data_complete": bool(row["last_scored_at"]),
                "profit_score": float(metrics.get("profit_score") or 0),
                "win_rate_score": float(metrics.get("win_rate_score") or 0),
                "drawdown_score": float(metrics.get("drawdown_score") or 0),
                "activity_score": float(metrics.get("activity_score") or 0),
            }
            wallets.append(wallet)

        pages = max(1, math.ceil(total / limit)) if total else 1
        cache_updated_at = _to_json_ts(freshness_row["latest"] if freshness_row else None)

        return JSONResponse(
            content={
                "wallets": wallets,
                "count": len(wallets),
                "total": total,
                "page": page,
                "page_size": limit,
                "pages": pages,
                "cache_updated_at": cache_updated_at,
                "source": "precomputed_cache",
                "exclude_bots": exclude_bots_effective,
                "mode": "cache",
            }
        )
    return await asyncio.to_thread(_db)


@router.get("/api/v2/copytrader/wallets/{address}/analytics")
async def wallet_analytics(request: Request, address: str) -> JSONResponse:
    """Return analytics metrics. Serves from cache when available (millisecond response).
    Falls back to live compute_full_metrics() for wallets not yet in the pool."""
    _require_identity(request)
    if not address.startswith("0x"):
        raise HTTPException(status_code=400, detail="invalid_address")

    # Try cache first
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT last_metrics_json, score, primary_category, is_likely_bot, last_scored_at "
                "FROM copytrader_v2_wallet_pool WHERE address=?",
                (address.lower(),),
            ).fetchone()
        if row and row["last_scored_at"] and row["last_metrics_json"]:
            metrics = json.loads(row["last_metrics_json"])
            if isinstance(metrics, dict) and metrics.get("data_complete"):
                metrics["address"] = address.lower()
                return JSONResponse(content=metrics)
    except Exception:
        pass

    # Cache miss: compute live and persist
    metrics = _analyzer.compute_full_metrics(address)
    if metrics.get("data_complete"):
        try:
            # Upsert into pool so future requests are instant
            with db_connect() as conn:
                conn.execute(
                    "INSERT INTO copytrader_v2_wallet_pool (address, source, last_metrics_json, score, "
                    "primary_category, is_likely_bot, pnl_realized_usd, win_rate_f, trade_count_30d, "
                    "max_drawdown_usd, last_scored_at) VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(address) DO UPDATE SET last_metrics_json=excluded.last_metrics_json, "
                    "score=excluded.score, primary_category=excluded.primary_category, "
                    "is_likely_bot=excluded.is_likely_bot, pnl_realized_usd=excluded.pnl_realized_usd, "
                    "win_rate_f=excluded.win_rate_f, trade_count_30d=excluded.trade_count_30d, "
                    "max_drawdown_usd=excluded.max_drawdown_usd, last_scored_at=CURRENT_TIMESTAMP",
                    (
                        address.lower(), "manual",
                        json.dumps(metrics),
                        float(metrics.get("score", 0)),
                        metrics.get("primary_category", "other"),
                        bool(metrics.get("is_likely_bot")),
                        float(metrics.get("pnl_realized_usd", 0)),
                        float(metrics.get("win_rate", 0)),
                        int(metrics.get("trade_count_30d", 0)),
                        float(metrics.get("max_drawdown_usd", 0)),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("[copytrader_v2] analytics cache store failed %s: %s", address[:12], exc)
    return JSONResponse(content=metrics)


@router.get("/api/v2/copytrader/wallets/{address}/closed-positions")
async def wallet_closed_positions(
    request: Request,
    address: str,
    limit: int = Query(200, ge=1, le=500),
) -> JSONResponse:
    """Return closed-positions (realized PnL history) for a wallet via official Data API."""
    _require_identity(request)
    if not address.startswith("0x"):
        raise HTTPException(status_code=400, detail="invalid_address")
    rows = _client.get_closed_positions(address, limit=limit)
    enriched = []
    for r in rows:
        item = dict(r)
        item["category"] = detect_category(
            r.get("slug"),
            r.get("title"),
            None,
            r.get("eventSlug"),
            r.get("outcome"),
        )
        item["realized_pnl"] = float(r.get("realizedPnl") or 0.0)
        enriched.append(item)
    total_realized = sum(x["realized_pnl"] for x in enriched)
    wins = sum(1 for x in enriched if x["realized_pnl"] > 0)
    losses = sum(1 for x in enriched if x["realized_pnl"] < 0)
    return JSONResponse(content={
        "closed_positions": enriched,
        "count": len(enriched),
        "total_realized_pnl": round(total_realized, 2),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(enriched), 4) if enriched else 0.0,
    })


@router.get("/api/v2/copytrader/wallets/{address}/positions")
async def wallet_positions(request: Request, address: str) -> JSONResponse:
    _require_identity(request)
    if not address.startswith("0x"):
        raise HTTPException(status_code=400, detail="invalid_address")
    rows = _client.get_positions(address, limit=200)
    enriched = []
    for r in rows:
        item = dict(r)
        item["category"] = detect_category(
            r.get("slug"),
            r.get("title"),
            r.get("question"),
            r.get("eventSlug"),
            r.get("outcome"),
        )
        enriched.append(item)
    return JSONResponse(
        content={"positions": enriched, "count": len(enriched)}
    )


@router.get("/api/v2/copytrader/wallets/{address}/activity")
async def wallet_activity(
    request: Request,
    address: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    _require_identity(request)
    if not address.startswith("0x"):
        raise HTTPException(status_code=400, detail="invalid_address")
    rows = _client.get_activity(address, limit=limit, offset=offset)
    enriched = []
    for r in rows:
        item = dict(r)
        item["category"] = detect_category(
            r.get("slug"),
            r.get("title"),
            r.get("question"),
            r.get("eventSlug"),
            r.get("outcome"),
        )
        enriched.append(item)
    return JSONResponse(
        content={"activity": enriched, "count": len(enriched)}
    )


@router.get("/api/v2/copytrader/categories")
async def categories_list(request: Request) -> JSONResponse:
    system_categories = sorted(set(CATEGORY_KEYWORDS.keys()) | {"other"})
    official_categories = [
        "sports",
        "politics",
        "crypto",
        "weather",
        "business",
        "finance",
        "macro",
        "technology",
        "science",
        "world",
        "culture",
        "entertainment",
        "other",
    ]
    observed: list[str] = []
    merged = []
    for c in official_categories + system_categories + observed:
        if c not in merged:
            merged.append(c)
    return JSONResponse(
        content={
            "categories": merged,
            "system_categories": system_categories,
            "official_categories": official_categories,
            "observed_categories": observed,
        }
    )


# ── Per-user tasks CRUD ───────────────────────────────────────────────


class TaskCreate(BaseModel):
    wallet_address: str
    wallet_label: str = ""
    categories: list[str] = Field(default_factory=list)
    base_usdc: float = 5.0
    max_per_event: int = 1
    min_score: float = 60.0
    max_price: float = 0.85
    follow_sell: bool = True
    notes: str = ""

    @field_validator("wallet_label", "notes", mode="before")
    @classmethod
    def _coerce_nullable_str(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)


class TaskPatch(BaseModel):
    status: str | None = None
    base_usdc: float | None = None
    categories: list[str] | None = None
    min_score: float | None = None
    max_price: float | None = None
    follow_sell: bool | None = None
    notes: str | None = None
    wallet_label: str | None = None

    @field_validator("wallet_label", "notes", mode="before")
    @classmethod
    def _coerce_patch_nullable_str(cls, v: Any) -> str | None:
        if v is None:
            return ""
        return str(v)


@router.get("/api/v2/copytrader/tasks")
async def tasks_list(request: Request) -> JSONResponse:
    owner_wallet_address = _resolve_owner_wallet(request)
    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT * FROM copytrader_v2_tasks WHERE owner_wallet_address=? ORDER BY created_at DESC",
                (owner_wallet_address,),
            ).fetchall()
        tasks: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for k, v in list(item.items()):
                if isinstance(v, datetime):
                    item[k] = v.isoformat()
            tasks.append(item)
        return JSONResponse(content={"tasks": tasks, "count": len(tasks)})
    return await asyncio.to_thread(_db)


@router.post("/api/v2/copytrader/tasks")
async def tasks_create(request: Request, payload: TaskCreate) -> JSONResponse:
    owner_wallet_address = _resolve_owner_wallet(request)
    addr = payload.wallet_address.strip().lower()
    if not addr.startswith("0x") or len(addr) != 42:
        raise HTTPException(status_code=400, detail="invalid_address")
    self_wallet = _resolve_self_wallet(request)
    wallet_lc = (owner_wallet_address or "").strip().lower()
    if (self_wallet and addr == self_wallet) or (wallet_lc.startswith("0x") and addr == wallet_lc):
        raise HTTPException(status_code=400, detail="self_follow_not_allowed")
    hard_limit = float(copytrader_cfg_get("hard_max_bet_usdc") or 25.0)
    base = max(1.0, min(payload.base_usdc, hard_limit))
    def _db():
        with db_connect() as conn:
            existing = conn.execute(
                "SELECT id FROM copytrader_v2_tasks WHERE owner_wallet_address=? AND wallet_address=? "
                "AND status IN ('running','paused')",
                (owner_wallet_address, addr),
            ).fetchone()
            if existing:
                raise HTTPException(
                    status_code=409, detail=f"duplicate_task id={existing['id']}"
                )
            cur = conn.execute(
                "INSERT INTO copytrader_v2_tasks "
                "(owner_wallet_address, wallet_address, wallet_label, categories, base_usdc, max_per_event, "
                "min_score, max_price, follow_sell, status, notes) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (
                    owner_wallet_address,
                    addr,
                    payload.wallet_label,
                    json.dumps(payload.categories),
                    base,
                    int(payload.max_per_event),
                    float(payload.min_score),
                    float(payload.max_price),
                    bool(payload.follow_sell),
                    "running",
                    payload.notes,
                ),
            )
            new_id = cur.fetchone()["id"]
            conn.commit()
        return JSONResponse(content={"ok": True, "id": new_id})
    return await asyncio.to_thread(_db)


@router.patch("/api/v2/copytrader/tasks/{task_id}")
async def tasks_patch(request: Request, task_id: int, payload: TaskPatch) -> JSONResponse:
    owner_wallet_address = _resolve_owner_wallet(request)
    fields: dict[str, Any] = {}
    if payload.status is not None:
        if payload.status not in ("running", "paused", "stopped"):
            raise HTTPException(status_code=400, detail="invalid_status")
        fields["status"] = payload.status
    if payload.base_usdc is not None:
        hard = float(copytrader_cfg_get("hard_max_bet_usdc") or 25.0)
        fields["base_usdc"] = max(1.0, min(payload.base_usdc, hard))
    if payload.categories is not None:
        fields["categories"] = json.dumps(payload.categories)
    if payload.min_score is not None:
        fields["min_score"] = float(payload.min_score)
    if payload.max_price is not None:
        fields["max_price"] = float(payload.max_price)
    if payload.follow_sell is not None:
        fields["follow_sell"] = bool(payload.follow_sell)
    if payload.notes is not None:
        fields["notes"] = payload.notes
    if payload.wallet_label is not None:
        fields["wallet_label"] = payload.wallet_label
    if not fields:
        return JSONResponse(content={"ok": True, "updated": 0})
    fields["updated_at"] = _utc_now()
    sets = ", ".join(f"{k}=?" for k in fields)
    def _db():
        with db_connect() as conn:
            cur = conn.execute(
                f"UPDATE copytrader_v2_tasks SET {sets} WHERE id=? AND owner_wallet_address=?",
                list(fields.values()) + [task_id, owner_wallet_address],
            )
            conn.commit()
        return JSONResponse(content={"ok": True, "updated": int(cur.rowcount or 0)})
    return await asyncio.to_thread(_db)


@router.delete("/api/v2/copytrader/tasks/{task_id}")
async def tasks_delete(request: Request, task_id: int) -> JSONResponse:
    owner_wallet_address = _resolve_owner_wallet(request)
    def _db():
        with db_connect() as conn:
            cur = conn.execute(
                "DELETE FROM copytrader_v2_tasks WHERE id=? AND owner_wallet_address=?",
                (task_id, owner_wallet_address),
            )
            conn.commit()
        return JSONResponse(content={"ok": True, "deleted": int(cur.rowcount or 0)})
    return await asyncio.to_thread(_db)


# ── Per-user settings ────────────────────────────────────────────────


@router.get("/api/v2/copytrader/settings")
async def settings_get(request: Request) -> JSONResponse:
    owner_wallet_address = _resolve_owner_wallet(request)
    defaults = {
        "enabled": True,
        "scan_interval_seconds": 60,
        "max_concurrent_follows": 3,
        "trade_mode": "taker",
        "trade_mode_options": ["taker", "maker"],
        "trade_mode_locked": True,
        "follow_multiplier": 1,
        "min_order_size_buffer_shares": 1,
        "min_ev_filter": 0.002,
        "max_price_filter": 0.85,
        "follow_sell_enabled": True,
        "trade_lookback_minutes": 30,
        "exclude_bots": True,
    }
    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT settings_json FROM copytrader_v2_settings WHERE owner_wallet_address=?",
                (owner_wallet_address,),
            ).fetchone()
        settings = defaults
        if row and row["settings_json"]:
            try:
                stored = json.loads(row["settings_json"])
                if isinstance(stored, dict):
                    settings = {**defaults, **stored}
            except (TypeError, json.JSONDecodeError):
                pass
        # Deprecated fields may still exist in persisted rows for backward compatibility;
        # do not expose them to current V2 UI/runtime settings consumers.
        settings.pop("category_size_multiplier", None)
        settings.pop("default_base_usdc", None)
        return JSONResponse(content={"settings": settings, "defaults": defaults})
    return await asyncio.to_thread(_db)


class SettingsPut(BaseModel):
    settings: dict[str, Any] | None = None
    enabled: bool | None = None
    scan_interval_seconds: int | None = None
    max_concurrent_follows: int | None = None
    trade_mode: str | None = None
    trade_mode_options: list[str] | None = None
    trade_mode_locked: bool | None = None
    follow_multiplier: int | None = None
    min_order_size_buffer_shares: int | None = None
    min_ev_filter: float | None = None
    max_price_filter: float | None = None
    follow_sell_enabled: bool | None = None
    trade_lookback_minutes: int | None = None
    exclude_bots: bool | None = None

    model_config = {"extra": "allow"}


@router.put("/api/v2/copytrader/settings")
async def settings_put(request: Request, payload: SettingsPut) -> JSONResponse:
    owner_wallet_address = _resolve_owner_wallet(request)
    if payload.settings is not None:
        s = dict(payload.settings)
    else:
        flat = payload.model_dump(exclude_none=True, exclude={"settings"})
        extra = payload.model_extra or {}
        s = {**flat, **extra}
    if "trade_mode" in s:
        mode = str(s.get("trade_mode") or "taker").lower().strip()
        s["trade_mode"] = "taker" if mode not in {"taker", "maker"} else mode
    if "trade_mode_options" in s:
        s["trade_mode_options"] = ["taker", "maker"]
    if "trade_mode_locked" in s:
        s["trade_mode_locked"] = True
    if "follow_multiplier" in s:
        s["follow_multiplier"] = max(1, min(1000, int(s["follow_multiplier"])))
    if "min_order_size_buffer_shares" in s:
        s["min_order_size_buffer_shares"] = max(0, min(20, int(s["min_order_size_buffer_shares"])))
    # Enforce current product policy: display maker option but lock runtime to taker.
    s["trade_mode"] = "taker"
    s["trade_mode_options"] = ["taker", "maker"]
    s["trade_mode_locked"] = True
    # Keep deprecated default_base_usdc for backward compatibility with old clients/tests;
    # runtime sizing no longer depends on this field.
    s.pop("category_size_multiplier", None)
    uid = zlib.crc32(owner_wallet_address.encode("utf-8")) & 0x7FFFFFFF
    def _db():
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO copytrader_v2_settings (user_id, owner_wallet_address, settings_json, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT (owner_wallet_address) DO UPDATE SET "
                "settings_json=EXCLUDED.settings_json, updated_at=EXCLUDED.updated_at",
                (uid, owner_wallet_address, json.dumps(s), _utc_now()),
            )
            conn.commit()
        return JSONResponse(content={"ok": True})
    return await asyncio.to_thread(_db)


# ── Health / status ───────────────────────────────────────────────────


@router.get("/api/v2/copytrader/health")
async def health(request: Request) -> JSONResponse:
    ident = _require_identity(request)
    session_wallet = _normalize_wallet_addr(ident.get("wallet_address"))
    if session_wallet == "__admin__":
        session_wallet = ""
    owner_wallet = ""
    try:
        owner_wallet = _resolve_owner_wallet(request)
    except Exception:
        owner_wallet = session_wallet
    self_wallet = _resolve_self_wallet(request)
    def _db():
        with db_connect() as conn:
            running = conn.execute(
                "SELECT COUNT(*) AS c FROM copytrader_v2_tasks WHERE status='running'"
            ).fetchone()
            task_count = 0
            if owner_wallet and owner_wallet != "__admin__":
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM copytrader_v2_tasks WHERE owner_wallet_address=?",
                    (owner_wallet,),
                ).fetchone()
                task_count = int(row["c"] if row else 0)
        return JSONResponse(
            content={
                "running_tasks": int(running["c"] if running else 0),
                "session_wallet": session_wallet,
                "self_wallet": self_wallet,
                "canonical_self_wallet": self_wallet,
                "self_wallet_task_count": task_count,
                "data_source": "polymarket_official_api",
                "endpoints": ["data-api", "gamma-api", "clob"],
            }
        )
    return await asyncio.to_thread(_db)
