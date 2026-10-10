"""T509 Wave 7D — Ecosystem Navigation routes.

Public route:
    GET /api/ecosystem/list                 → grouped active entries

Admin routes (require admin):
    GET    /api/admin/ecosystem/list        → all entries (incl. disabled)
    POST   /api/admin/ecosystem/upsert      → insert or update single entry
    POST   /api/admin/ecosystem/delete      → delete by id
    GET    /api/admin/builders/leaderboard  → cached PM Builders leaderboard
    POST   /api/admin/builders/sync         → manually trigger PM Builders pull

Background:
    A best-effort once-per-24h pull of PM Builders endpoints into
    `pm_builders_daily` is started lazily on first hit (no extra service).

Sources:
    - https://docs.polymarket.com/api-reference/builders/get-aggregated-builder-leaderboard
    - https://docs.polymarket.com/api-reference/builders/get-daily-builder-volume-time-series
"""

from __future__ import annotations

from datetime import datetime, timezone, date
import asyncio
import json as _json
import threading
import time
import urllib.request
import urllib.error

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts.dashboard_routes._shared import db_connect, get_request_identity

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# DDL bootstrap
# ─────────────────────────────────────────────────────────────────────────────

_TABLES_READY = False
_TABLES_LOCK = threading.Lock()


def _ensure_tables() -> None:
    global _TABLES_READY
    if _TABLES_READY:
        return
    with _TABLES_LOCK:
        if _TABLES_READY:
            return
        with db_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ecosystem_entries (
                    id           SERIAL PRIMARY KEY,
                    slug         TEXT NOT NULL UNIQUE,
                    name         TEXT NOT NULL,
                    category     TEXT NOT NULL DEFAULT 'tools',
                    url          TEXT NOT NULL DEFAULT '',
                    logo_url     TEXT NOT NULL DEFAULT '',
                    description  TEXT NOT NULL DEFAULT '',
                    tags         TEXT NOT NULL DEFAULT '',
                    sort_order   INTEGER NOT NULL DEFAULT 100,
                    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_at   TEXT NOT NULL,
                    created_at   TEXT NOT NULL
                )
                """)
            # T601 W6 — admin 可显式覆盖默认 slug 派生的 i18n_key
            # （让同一术语/品牌名能跨实体复用 key）
            conn.execute(
                "ALTER TABLE ecosystem_entries "
                "ADD COLUMN IF NOT EXISTS name_i18n_key TEXT, "
                "ADD COLUMN IF NOT EXISTS description_i18n_key TEXT"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pm_builders_daily (
                    id              SERIAL PRIMARY KEY,
                    snapshot_date   DATE NOT NULL,
                    builder_address TEXT NOT NULL,
                    builder_name    TEXT NOT NULL DEFAULT '',
                    volume_usd      DOUBLE PRECISION NOT NULL DEFAULT 0,
                    market_count    INTEGER NOT NULL DEFAULT 0,
                    raw_json        TEXT NOT NULL DEFAULT '{}',
                    fetched_at      TEXT NOT NULL,
                    UNIQUE (snapshot_date, builder_address)
                )
                """)
            conn.commit()
        _seed_default_entries()
        _TABLES_READY = True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Defaults — first-run ecosystem seed
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_ENTRIES = [
    # Trading interfaces
    (
        "polymarket",
        "Polymarket",
        "trading",
        "https://polymarket.com",
        "Official prediction-market UI built on Gnosis chain.",
        "core,prediction",
    ),
    (
        "limitless",
        "Limitless",
        "trading",
        "https://limitless.exchange",
        "On-chain perpetuals with high-leverage markets.",
        "perp,defi",
    ),
    (
        "kalshi",
        "Kalshi",
        "trading",
        "https://kalshi.com",
        "CFTC-regulated event contract exchange.",
        "regulated",
    ),
    # Data / Analytics
    (
        "polydata",
        "Polydata",
        "data",
        "https://polydata.org",
        "Polymarket analytics, leaderboards and market intel.",
        "analytics",
    ),
    (
        "dune",
        "Dune Polymarket Dashboards",
        "data",
        "https://dune.com/discover/content/relevant?q=polymarket",
        "Community SQL dashboards for Polymarket on Polygon.",
        "sql,onchain",
    ),
    (
        "polymarket-analytics",
        "Polymarket Analytics",
        "data",
        "https://polymarket.com/markets",
        "Volume, liquidity and OI for live event markets.",
        "market",
    ),
    # Tools
    (
        "py-clob-client",
        "py-clob-client",
        "tools",
        "https://github.com/Polymarket/py-clob-client",
        "Official Python SDK for the Polymarket CLOB.",
        "sdk,python",
    ),
    (
        "clob-client",
        "clob-client (TS)",
        "tools",
        "https://github.com/Polymarket/clob-client",
        "Official TypeScript SDK for the Polymarket CLOB.",
        "sdk,ts",
    ),
    # Wallets / Infra
    (
        "privy",
        "Privy",
        "infra",
        "https://privy.io",
        "Embedded wallet infrastructure used by VeYra & Polymarket.",
        "wallet,auth",
    ),
    (
        "polygon",
        "Polygon PoS",
        "infra",
        "https://polygon.technology",
        "Underlying L2 powering Polymarket settlement.",
        "chain",
    ),
    # Community
    (
        "polymarket-discord",
        "Polymarket Discord",
        "community",
        "https://discord.gg/polymarket",
        "Official community discussion & support.",
        "social",
    ),
    (
        "polymarket-twitter",
        "@Polymarket",
        "community",
        "https://x.com/polymarket",
        "Polymarket on X (Twitter).",
        "social",
    ),
]


def _seed_default_entries() -> None:
    now = _now_iso()
    with db_connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM ecosystem_entries").fetchone()
        if int((row["c"] if row else 0) or 0) > 0:
            return
        for i, (slug, name, category, url, desc, tags) in enumerate(_DEFAULT_ENTRIES):
            conn.execute(
                """
                INSERT INTO ecosystem_entries
                (slug, name, category, url, logo_url, description, tags, sort_order, enabled, updated_at, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)
                ON CONFLICT (slug) DO NOTHING
                """,
                (slug, name, category, url, "", desc, tags, (i + 1) * 10, now, now),
            )
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _is_admin(request: Request) -> bool:
    ident = get_request_identity(request) or {}
    return bool(ident.get("authenticated") and ident.get("is_admin"))


def _row_to_entry(r) -> dict:
    # T601 W6 — 用 dict() 包一层避免不同 row factory（RealDict / Row / tuple）
    # 在访问可能不存在的 ALTER TABLE 后新加列时抛 KeyError。
    try:
        d = dict(r)
    except Exception:
        d = r  # type: ignore[assignment]
    return {
        "id": int(d["id"]),
        "slug": d["slug"],
        "name": d["name"],
        "category": d["category"],
        "url": d["url"],
        "logo_url": d["logo_url"],
        "description": d["description"],
        "tags": [t.strip() for t in str(d["tags"] or "").split(",") if t.strip()],
        "sort_order": int(d["sort_order"]),
        "enabled": bool(d["enabled"]),
        "updated_at": d["updated_at"],
        # T601 W6 — admin 可显式覆盖默认 slug 派生的 key
        "name_i18n_key": (d.get("name_i18n_key") if isinstance(d, dict) else "") or "",
        "description_i18n_key": (
            d.get("description_i18n_key") if isinstance(d, dict) else ""
        )
        or "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public list (used by /preview)
# ─────────────────────────────────────────────────────────────────────────────


_PUBLIC_CACHE: dict = {"ts": 0.0, "data": None}
_PUBLIC_TTL = 300


def get_public_payload() -> dict:
    """Return grouped ecosystem payload (cached). Safe to call from SSR."""
    now = time.time()
    cached = _PUBLIC_CACHE.get("data")
    if cached and (now - float(_PUBLIC_CACHE.get("ts") or 0)) < _PUBLIC_TTL:
        return cached
    _ensure_tables()
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT id, slug, name, category, url, logo_url, description, tags,
                   sort_order, enabled, updated_at, name_i18n_key, description_i18n_key
            FROM ecosystem_entries
            WHERE enabled = TRUE
            ORDER BY category ASC, sort_order ASC, id ASC
            """).fetchall()
    groups: dict = {}
    for r in rows:
        e = _row_to_entry(r)
        groups.setdefault(e["category"], []).append(e)
    out = {
        "groups": [
            {"category": k, "entries": v}
            for k, v in sorted(groups.items(), key=lambda kv: kv[0])
        ],
        "count": sum(len(v) for v in groups.values()),
        "generated_at": _now_iso(),
    }
    _PUBLIC_CACHE["ts"] = now
    _PUBLIC_CACHE["data"] = out
    return out


def _bust_public_cache() -> None:
    _PUBLIC_CACHE["ts"] = 0.0
    _PUBLIC_CACHE["data"] = None


@router.get("/api/ecosystem/list")
async def api_ecosystem_list() -> JSONResponse:
    return JSONResponse({"ok": True, **get_public_payload()})


# ─────────────────────────────────────────────────────────────────────────────
# Admin CRUD
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/api/admin/ecosystem/list")
async def admin_ecosystem_list(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    # T590 — sync DB ops moved off event loop
    def _db():
        _ensure_tables()
        with db_connect() as conn:
            rows = conn.execute("""
                SELECT id, slug, name, category, url, logo_url, description, tags,
                       sort_order, enabled, updated_at, name_i18n_key, description_i18n_key
                FROM ecosystem_entries
                ORDER BY category ASC, sort_order ASC, id ASC
                """).fetchall()
        return rows

    rows = await asyncio.to_thread(_db)
    return JSONResponse({"ok": True, "entries": [_row_to_entry(r) for r in rows]})


@router.post("/api/admin/ecosystem/upsert")
async def admin_ecosystem_upsert(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    slug = str((body or {}).get("slug") or "").strip().lower()
    name = str((body or {}).get("name") or "").strip()
    category = str((body or {}).get("category") or "tools").strip().lower() or "tools"
    url = str((body or {}).get("url") or "").strip()
    logo_url = str((body or {}).get("logo_url") or "").strip()
    description = str((body or {}).get("description") or "").strip()
    tags_raw = body.get("tags") if isinstance(body, dict) else ""
    if isinstance(tags_raw, list):
        tags = ",".join(str(t).strip() for t in tags_raw if str(t).strip())
    else:
        tags = str(tags_raw or "").strip()
    try:
        sort_order = int((body or {}).get("sort_order") or 100)
    except Exception:
        sort_order = 100
    enabled = bool((body or {}).get("enabled", True))
    # T601 W6 — 可选显式 i18n key，空字符串 = 使用默认 slug 派生 key
    name_i18n_key = str((body or {}).get("name_i18n_key") or "").strip()
    description_i18n_key = str((body or {}).get("description_i18n_key") or "").strip()

    if not slug or not name:
        return JSONResponse(
            {"ok": False, "error": "slug_and_name_required"}, status_code=400
        )

    now = _now_iso()

    def _db():
        _ensure_tables()
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO ecosystem_entries
                (slug, name, category, url, logo_url, description, tags, sort_order, enabled, updated_at, created_at, name_i18n_key, description_i18n_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (slug) DO UPDATE SET
                    name        = EXCLUDED.name,
                    category    = EXCLUDED.category,
                    url         = EXCLUDED.url,
                    logo_url    = EXCLUDED.logo_url,
                    description = EXCLUDED.description,
                    tags        = EXCLUDED.tags,
                    sort_order  = EXCLUDED.sort_order,
                    enabled     = EXCLUDED.enabled,
                    updated_at  = EXCLUDED.updated_at,
                    name_i18n_key        = EXCLUDED.name_i18n_key,
                    description_i18n_key = EXCLUDED.description_i18n_key
                """,
                (
                    slug,
                    name,
                    category,
                    url,
                    logo_url,
                    description,
                    tags,
                    sort_order,
                    enabled,
                    now,
                    now,
                    name_i18n_key or None,
                    description_i18n_key or None,
                ),
            )
            conn.commit()

    await asyncio.to_thread(_db)
    _bust_public_cache()
    return JSONResponse({"ok": True})


@router.post("/api/admin/ecosystem/delete")
async def admin_ecosystem_delete(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    slug = str((body or {}).get("slug") or "").strip().lower()
    entry_id = (body or {}).get("id")
    if not slug and not entry_id:
        return JSONResponse(
            {"ok": False, "error": "slug_or_id_required"}, status_code=400
        )

    def _db():
        _ensure_tables()
        with db_connect() as conn:
            if slug:
                conn.execute("DELETE FROM ecosystem_entries WHERE slug = %s", (slug,))
            else:
                conn.execute(
                    "DELETE FROM ecosystem_entries WHERE id = %s", (int(entry_id),)
                )
            conn.commit()

    await asyncio.to_thread(_db)
    _bust_public_cache()
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# PM Builders pull (T509 daily fetch)
#
# Endpoints (per docs.polymarket.com):
#   GET https://data-api.polymarket.com/v1/builders/leaderboard?timePeriod=DAY|WEEK|MONTH|ALL&limit=50
#   GET https://data-api.polymarket.com/v1/builders/volume?timePeriod=DAY|WEEK|MONTH|ALL
#
# Response shape (per official spec):
#   { rank, builder, volume, activeUsers, verified, builderLogo }
# ─────────────────────────────────────────────────────────────────────────────

_BUILDERS_LEADERBOARD_URL = "https://data-api.polymarket.com/v1/builders/leaderboard"
_BUILDERS_DAILY_URL = "https://data-api.polymarket.com/v1/builders/volume"
_BUILDERS_LAST_PULL_TS = {"ts": 0.0}
_BUILDERS_PULL_LOCK = threading.Lock()


def _http_json(url: str, timeout: float = 8.0):
    """Tiny wrapper kept for backward compat. Prefer src.utils.pm_http.pm_get."""
    try:
        from src.utils.pm_http import pm_get, DATA  # local import to avoid cycle

        # Strip data-api prefix if matched, otherwise fall back to raw urllib.
        if url.startswith(DATA + "/"):
            return pm_get(DATA, url[len(DATA) :], timeout=timeout)
    except Exception:
        pass
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "VeYra-Bot/2.0 (+https://veyra.trade)",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode())
    except Exception:
        return None


def _coerce_builder_rows(payload) -> list:
    """Extract a list of builder dicts from a variety of upstream shapes."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = (
            payload.get("data")
            or payload.get("builders")
            or payload.get("leaderboard")
            or payload.get("results")
            or []
        )
    else:
        rows = []
    out = []
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        addr = (
            str(
                r.get("builder_address")
                or r.get("address")
                or r.get("builder")
                or r.get("wallet")
                or ""
            )
            .strip()
            .lower()
        )
        if not addr:
            continue
        name = str(
            r.get("name") or r.get("builder_name") or r.get("label") or ""
        ).strip()
        try:
            volume = float(
                r.get("volume_usd")
                or r.get("total_volume_usd")
                or r.get("volume")
                or r.get("usd_volume")
                or 0.0
            )
        except Exception:
            volume = 0.0
        try:
            mc = int(
                r.get("market_count") or r.get("markets") or r.get("market_total") or 0
            )
        except Exception:
            mc = 0
        out.append(
            {
                "address": addr,
                "name": name,
                "volume_usd": volume,
                "market_count": mc,
                "raw": r,
            }
        )
    return out


def _pull_builders_now() -> dict:
    """Synchronously pull builder leaderboard. Best effort; never raises."""
    snapshot = date.today().isoformat()
    payload = _http_json(_BUILDERS_LEADERBOARD_URL, timeout=8.0)
    rows = _coerce_builder_rows(payload)
    inserted = 0
    if rows:
        _ensure_tables()
        now = _now_iso()
        with db_connect() as conn:
            for r in rows:
                conn.execute(
                    """
                    INSERT INTO pm_builders_daily
                    (snapshot_date, builder_address, builder_name, volume_usd, market_count, raw_json, fetched_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (snapshot_date, builder_address) DO UPDATE SET
                        builder_name = EXCLUDED.builder_name,
                        volume_usd   = EXCLUDED.volume_usd,
                        market_count = EXCLUDED.market_count,
                        raw_json     = EXCLUDED.raw_json,
                        fetched_at   = EXCLUDED.fetched_at
                    """,
                    (
                        snapshot,
                        r["address"],
                        r["name"],
                        r["volume_usd"],
                        r["market_count"],
                        _json.dumps(r["raw"], ensure_ascii=False)[:8000],
                        now,
                    ),
                )
                inserted += 1
            conn.commit()
    _BUILDERS_LAST_PULL_TS["ts"] = time.time()
    return {
        "ok": bool(rows),
        "snapshot_date": snapshot,
        "rows_upstream": len(rows),
        "rows_written": inserted,
        "upstream_alive": payload is not None,
    }


def _maybe_pull_builders_async() -> None:
    """Lazy daily pull triggered on first admin/list hit. 24h cooldown."""
    last = float(_BUILDERS_LAST_PULL_TS.get("ts") or 0)
    if (time.time() - last) < 86400:
        return
    if not _BUILDERS_PULL_LOCK.acquire(blocking=False):
        return

    def _run():
        try:
            _pull_builders_now()
        finally:
            try:
                _BUILDERS_PULL_LOCK.release()
            except Exception:
                pass

    threading.Thread(target=_run, name="pm-builders-pull", daemon=True).start()


@router.get("/api/admin/builders/leaderboard")
async def admin_builders_leaderboard(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    _ensure_tables()
    _maybe_pull_builders_async()

    def _db():
        with db_connect() as conn:
            latest = conn.execute(
                "SELECT MAX(snapshot_date) AS d FROM pm_builders_daily"
            ).fetchone()
            latest_date = latest["d"] if latest and latest["d"] else None
            rows = []
            if latest_date:
                rows = conn.execute(
                    """
                SELECT snapshot_date, builder_address, builder_name, volume_usd, market_count, fetched_at
                FROM pm_builders_daily
                WHERE snapshot_date = %s
                ORDER BY volume_usd DESC
                LIMIT 100
                """,
                    (latest_date,),
                ).fetchall()
        return JSONResponse(
            {
                "ok": True,
                "snapshot_date": str(latest_date) if latest_date else None,
                "builders": [
                    {
                        "snapshot_date": str(r["snapshot_date"]),
                        "address": r["builder_address"],
                        "name": r["builder_name"],
                        "volume_usd": float(r["volume_usd"] or 0),
                        "market_count": int(r["market_count"] or 0),
                        "fetched_at": r["fetched_at"],
                    }
                    for r in rows
                ],
            }
        )

    return await asyncio.to_thread(_db)


@router.post("/api/admin/builders/sync")
async def admin_builders_sync(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    res = _pull_builders_now()
    return JSONResponse({"ok": True, "result": res})


# ─────────────────────────────────────────────────────────────────────────────
# T509追加 (Wave 8A) — Public Builders Directory
#
# This is the new model that powers /preview. It pulls the official
# leaderboard + time-series endpoints, merges with admin-managed metadata
# (description, website, tags, featured), caches in PG and serves a
# normalized payload to SSR + the public API.
# ─────────────────────────────────────────────────────────────────────────────

_BUILDERS_META_READY = False
_BUILDERS_META_LOCK = threading.Lock()


def _ensure_builders_meta_table() -> None:
    global _BUILDERS_META_READY
    if _BUILDERS_META_READY:
        return
    with _BUILDERS_META_LOCK:
        if _BUILDERS_META_READY:
            return
        with db_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pm_builders_meta (
                    id           SERIAL PRIMARY KEY,
                    builder_key  TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    description  TEXT NOT NULL DEFAULT '',
                    website      TEXT NOT NULL DEFAULT '',
                    tags         TEXT NOT NULL DEFAULT '',
                    featured     BOOLEAN NOT NULL DEFAULT FALSE,
                    sort_weight  INTEGER NOT NULL DEFAULT 100,
                    updated_at   TEXT NOT NULL,
                    created_at   TEXT NOT NULL
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pm_builders_snapshot (
                    id            SERIAL PRIMARY KEY,
                    time_period   TEXT NOT NULL,
                    snapshot_ts   TEXT NOT NULL,
                    rank          INTEGER NOT NULL DEFAULT 0,
                    builder_key   TEXT NOT NULL,
                    builder_name  TEXT NOT NULL DEFAULT '',
                    builder_logo  TEXT NOT NULL DEFAULT '',
                    verified      BOOLEAN NOT NULL DEFAULT FALSE,
                    volume        DOUBLE PRECISION NOT NULL DEFAULT 0,
                    active_users  INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (time_period, builder_key)
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pm_builders_volume_series (
                    id            SERIAL PRIMARY KEY,
                    builder_key   TEXT NOT NULL,
                    dt            TIMESTAMP NOT NULL,
                    volume        DOUBLE PRECISION NOT NULL DEFAULT 0,
                    active_users  INTEGER NOT NULL DEFAULT 0,
                    rank          INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (builder_key, dt)
                )
                """)
            conn.commit()
        _BUILDERS_META_READY = True


def _builder_key(name: str) -> str:
    return (str(name or "").strip().lower())[:128]


def _coerce_v1_leaderboard(payload) -> list:
    """Normalize official v1/builders/leaderboard rows.

    Accepts list or {data:[...]}; returns list[dict] with shape:
        rank, builder, builderLogo, verified, volume, activeUsers
    """
    if isinstance(payload, dict):
        rows = (
            payload.get("data")
            or payload.get("results")
            or payload.get("builders")
            or []
        )
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    out = []
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = str(r.get("builder") or r.get("name") or "").strip()
        if not name:
            continue
        try:
            rank = int(str(r.get("rank") or 0).strip() or 0)
        except Exception:
            rank = 0
        try:
            volume = float(r.get("volume") or 0)
        except Exception:
            volume = 0.0
        try:
            active = int(r.get("activeUsers") or r.get("active_users") or 0)
        except Exception:
            active = 0
        verified = bool(r.get("verified"))
        logo = str(r.get("builderLogo") or r.get("builder_logo") or "").strip()
        out.append(
            {
                "builder": name,
                "rank": rank,
                "volume": volume,
                "activeUsers": active,
                "verified": verified,
                "builderLogo": logo,
            }
        )
    out.sort(key=lambda x: (x["rank"] if x["rank"] > 0 else 9999, -x["volume"]))
    return out


def _coerce_v1_volume_series(payload) -> list:
    """Normalize v1/builders/volume rows (per-day per-builder)."""
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("results") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    out = []
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = str(r.get("builder") or r.get("name") or "").strip()
        dt = str(r.get("dt") or r.get("date") or "").strip()
        if not name or not dt:
            continue
        try:
            volume = float(r.get("volume") or 0)
        except Exception:
            volume = 0.0
        try:
            active = int(r.get("activeUsers") or r.get("active_users") or 0)
        except Exception:
            active = 0
        try:
            rank = int(str(r.get("rank") or 0).strip() or 0)
        except Exception:
            rank = 0
        out.append(
            {
                "builder": name,
                "dt": dt,
                "volume": volume,
                "activeUsers": active,
                "rank": rank,
            }
        )
    return out


_BUILDERS_PUBLIC_CACHE: dict = {}  # period -> {ts, payload}
_BUILDERS_PUBLIC_TTL = 300  # 5 minutes
_BUILDERS_PUBLIC_LOCK = threading.Lock()
_VALID_PERIODS = ("DAY", "WEEK", "MONTH", "ALL")


def _normalize_period(p: str | None) -> str:
    p = (p or "DAY").upper()
    return p if p in _VALID_PERIODS else "DAY"


def _fetch_v1_leaderboard(period: str, limit: int = 200) -> list:
    """GET official v1 leaderboard via shared pm_http session.

    T511 Wave 11 — PM Data API caps a single response at 50 rows. We page
    through ``offset`` until we've collected ``limit`` rows or upstream
    returns an empty page (whichever first). Empirical totals per
    period: DAY≈128, WEEK≈178, MONTH≈259, ALL≈300.
    """
    try:
        from src.utils.pm_http import pm_get, DATA
    except Exception:
        return []
    page_size = 50
    target = max(int(limit or 0), page_size)
    aggregated: list = []
    offset = 0
    seen_keys: set[str] = set()
    # Hard cap loops to avoid runaway in pathological cases.
    # T523 P1 #520 — bumped from 20 → 40 (max 2000 rows) since meta now
    # grows past 250 and PM occasionally returns long-tails > 1000.
    for _ in range(40):
        if len(aggregated) >= target:
            break
        try:
            payload = pm_get(
                DATA,
                "/v1/builders/leaderboard",
                params={
                    "timePeriod": period,
                    "limit": page_size,
                    "offset": offset,
                },
                timeout=6.0,
            )
        except Exception:
            payload = None
        page = _coerce_v1_leaderboard(payload)
        if not page:
            break
        added = 0
        for row in page:
            key = _builder_key(row.get("builder"))
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            aggregated.append(row)
            added += 1
            if len(aggregated) >= target:
                break
        if added == 0:
            break
        offset += page_size
    aggregated.sort(key=lambda x: (x["rank"] if x["rank"] > 0 else 9999, -x["volume"]))
    return aggregated[:target]


def _fetch_v1_volume_series(period: str = "DAY") -> list:
    try:
        from src.utils.pm_http import pm_get, DATA

        payload = pm_get(
            DATA,
            "/v1/builders/volume",
            params={"timePeriod": period},
            timeout=6.0,
        )
    except Exception:
        payload = None
    return _coerce_v1_volume_series(payload)


def _persist_snapshot(period: str, rows: list) -> None:
    if not rows:
        return
    _ensure_builders_meta_table()
    snap_ts = _now_iso()
    with db_connect() as conn:
        # Wipe old snapshot for this period to avoid stale rows
        conn.execute(
            "DELETE FROM pm_builders_snapshot WHERE time_period = %s", (period,)
        )
        for r in rows:
            conn.execute(
                """
                INSERT INTO pm_builders_snapshot
                (time_period, snapshot_ts, rank, builder_key, builder_name, builder_logo,
                 verified, volume, active_users)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (time_period, builder_key) DO UPDATE SET
                    snapshot_ts  = EXCLUDED.snapshot_ts,
                    rank         = EXCLUDED.rank,
                    builder_name = EXCLUDED.builder_name,
                    builder_logo = EXCLUDED.builder_logo,
                    verified     = EXCLUDED.verified,
                    volume       = EXCLUDED.volume,
                    active_users = EXCLUDED.active_users
                """,
                (
                    period,
                    snap_ts,
                    int(r["rank"]),
                    _builder_key(r["builder"]),
                    r["builder"],
                    r["builderLogo"],
                    bool(r["verified"]),
                    float(r["volume"]),
                    int(r["activeUsers"]),
                ),
            )
        conn.commit()


def _persist_volume_series(rows: list) -> None:
    if not rows:
        return
    _ensure_builders_meta_table()
    with db_connect() as conn:
        for r in rows:
            try:
                conn.execute(
                    """
                    INSERT INTO pm_builders_volume_series
                    (builder_key, dt, volume, active_users, rank)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (builder_key, dt) DO UPDATE SET
                        volume       = EXCLUDED.volume,
                        active_users = EXCLUDED.active_users,
                        rank         = EXCLUDED.rank
                    """,
                    (
                        _builder_key(r["builder"]),
                        r["dt"],
                        float(r["volume"]),
                        int(r["activeUsers"]),
                        int(r["rank"]),
                    ),
                )
            except Exception:
                continue
        conn.commit()


def _load_meta_index() -> dict:
    _ensure_builders_meta_table()
    out = {}
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT builder_key, display_name, description, website, tags, featured, sort_weight, updated_at FROM pm_builders_meta"
        ).fetchall()
    for r in rows:
        out[r["builder_key"]] = {
            "display_name": r["display_name"] or "",
            "description": r["description"] or "",
            "website": r["website"] or "",
            "tags": [t.strip() for t in str(r["tags"] or "").split(",") if t.strip()],
            "featured": bool(r["featured"]),
            "sort_weight": int(r["sort_weight"] or 100),
            "updated_at": r["updated_at"],
        }
    return out


def _load_snapshot_from_db(period: str) -> list:
    _ensure_builders_meta_table()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT rank, builder_key, builder_name, builder_logo, verified, volume, active_users, snapshot_ts
            FROM pm_builders_snapshot
            WHERE time_period = %s
            ORDER BY (CASE WHEN rank > 0 THEN rank ELSE 99999 END) ASC, volume DESC
            """,
            (period,),
        ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "builder": r["builder_name"],
                "rank": int(r["rank"] or 0),
                "volume": float(r["volume"] or 0),
                "activeUsers": int(r["active_users"] or 0),
                "verified": bool(r["verified"]),
                "builderLogo": r["builder_logo"] or "",
                "snapshot_ts": r["snapshot_ts"],
            }
        )
    return out


def _load_ecosystem_synth_builders() -> list:
    """T512 Wave 10B — Convert admin-managed ecosystem_entries into synthetic
    builder rows that merge into /preview Builders Directory.

    Mapping:
        ecosystem_entries.sort_order  → synthetic rank (#)
        ecosystem_entries.name        → builder name
        ecosystem_entries.logo_url    → builder logo
        ecosystem_entries.description → description column
        ecosystem_entries.url         → website column
        ecosystem_entries.tags        → tags
        volume / active_users         → 0 (empty allowed per spec)
    """
    try:
        _ensure_tables()
    except Exception:
        return []
    out: list = []
    try:
        with db_connect() as conn:
            rows = conn.execute("""
                SELECT id, slug, name, category, url, logo_url, description, tags, sort_order
                FROM ecosystem_entries
                WHERE enabled = TRUE
                ORDER BY sort_order ASC, id ASC
                """).fetchall()
    except Exception:
        return out
    for r in rows:
        slug = str(r["slug"] or "").strip().lower()
        if not slug:
            continue
        synth_key = "eco:" + slug  # namespaced so it never collides with real builders
        out.append(
            {
                "key": synth_key,
                "rank": int(r["sort_order"] or 0),
                "name": str(r["name"] or "").strip() or slug,
                "raw_name": str(r["name"] or "").strip() or slug,
                "logo": str(r["logo_url"] or "").strip(),
                "verified": False,
                "volume": 0.0,
                "active_users": 0,
                "description": str(r["description"] or "").strip(),
                "website": str(r["url"] or "").strip(),
                "tags": [
                    t.strip() for t in str(r["tags"] or "").split(",") if t.strip()
                ],
                "featured": False,
                "sort_weight": int(r["sort_order"] or 100),
                "source": "ecosystem",
                "category": str(r["category"] or "").strip().lower(),
            }
        )
    return out


def _build_directory_payload(period: str) -> dict:
    """Merge live leaderboard + ecosystem entries with admin meta.

    T513 Wave 10C — leaderboard limit raised so we surface the full
    snapshot the way operators expect (PM API returns up to ~50/100).
    T523 P1 #520 — drop the hard-coded 200 cap.  We now paginate up to
    1000 live rows AND merge in any builder we have meta-data for that
    didn't make this period's live cut (so admin-curated entries don't
    silently disappear from the directory).
    """
    period = _normalize_period(period)
    live = _fetch_v1_leaderboard(period, limit=1000)
    if live:
        _persist_snapshot(period, live)
        rows = live
        upstream_alive = True
        snapshot_ts = _now_iso()
    else:
        rows = _load_snapshot_from_db(period)
        upstream_alive = False
        snapshot_ts = rows[0]["snapshot_ts"] if rows else None

    meta = _load_meta_index()
    total_volume = sum(float(r["volume"] or 0) for r in rows)
    verified_n = sum(1 for r in rows if r.get("verified"))
    total_active = sum(int(r["activeUsers"] or 0) for r in rows)

    builders: list = []
    seen_keys: set[str] = set()
    for r in rows:
        key = _builder_key(r["builder"])
        if not key:
            continue
        seen_keys.add(key)
        m = meta.get(key) or {}
        builders.append(
            {
                "key": key,
                "rank": int(r.get("rank") or 0),
                "name": m.get("display_name") or r["builder"],
                "raw_name": r["builder"],
                "logo": r.get("builderLogo") or "",
                "verified": bool(r.get("verified")),
                "volume": float(r.get("volume") or 0),
                "active_users": int(r.get("activeUsers") or 0),
                "description": m.get("description") or "",
                "website": m.get("website") or "",
                "tags": m.get("tags") or [],
                "featured": bool(m.get("featured")),
                "sort_weight": int(m.get("sort_weight") or 100),
                "source": "polymarket",
            }
        )

    # T523 P1 #520 — meta-only builders: surface entries that have admin
    # meta but didn't appear in this period's live leaderboard.  Pull
    # their last-known volume/rank from the snapshot table when available.
    meta_only_keys = [k for k in meta.keys() if k and k not in seen_keys]
    if meta_only_keys:
        snap_rows = _load_snapshot_from_db(period)
        snap_by_key = {_builder_key(s["builder"]): s for s in snap_rows}
        for key in meta_only_keys:
            m = meta.get(key) or {}
            snap = snap_by_key.get(key) or {}
            display = m.get("display_name") or snap.get("builder") or key
            builders.append(
                {
                    "key": key,
                    "rank": int(snap.get("rank") or 0),
                    "name": display,
                    "raw_name": snap.get("builder") or display,
                    "logo": "",
                    "verified": bool(snap.get("verified") or False),
                    "volume": float(snap.get("volume") or 0),
                    "active_users": int(snap.get("activeUsers") or 0),
                    "description": m.get("description") or "",
                    "website": m.get("website") or "",
                    "tags": m.get("tags") or [],
                    "featured": bool(m.get("featured")),
                    "sort_weight": int(m.get("sort_weight") or 100),
                    "source": "polymarket",
                }
            )

    # T512 Wave 10B — merge admin-managed ecosystem entries
    eco_synth = _load_ecosystem_synth_builders()
    builders.extend(eco_synth)

    # Sort: keep PM rank order intact for source=polymarket; ecosystem entries
    # weave in by their explicit rank. Entries with rank<=0 fall to the end.
    def _sort_key(b):
        rk = b.get("rank") or 0
        return (
            0 if rk > 0 else 1,
            rk if rk > 0 else 99999,
            -float(b.get("volume") or 0),
        )

    builders.sort(key=_sort_key)

    featured = [b for b in builders if b.get("featured")]
    featured.sort(
        key=lambda b: (b.get("sort_weight") or 100, -float(b.get("volume") or 0))
    )

    payload = {
        "ok": True,
        "time_period": period,
        "snapshot_ts": snapshot_ts,
        "upstream_alive": upstream_alive,
        "stats": {
            # stats keep tracking real PM builder universe (rows) — synthetic
            # ecosystem entries are navigation aids, not trading volume.
            "total_builders": len(builders),
            "polymarket_builders": len(rows),
            "ecosystem_entries": len(eco_synth),
            "verified_builders": verified_n,
            "total_volume": total_volume,
            "total_active_users": total_active,
            "top_builder": builders[0]["name"] if builders else None,
            "top_builder_volume": (
                float(builders[0].get("volume") or 0) if builders else 0.0
            ),
        },
        "featured": featured,
        "builders": builders,
        "generated_at": _now_iso(),
    }
    return payload


def get_public_builders_payload(period: str = "DAY") -> dict:
    """Cached public builders directory payload (5 min TTL per period)."""
    period = _normalize_period(period)
    now = time.time()
    cached = _BUILDERS_PUBLIC_CACHE.get(period)
    if cached and (now - float(cached.get("ts") or 0)) < _BUILDERS_PUBLIC_TTL:
        return cached["payload"]
    if not _BUILDERS_PUBLIC_LOCK.acquire(blocking=False):
        # Another thread is building; serve stale if any
        if cached:
            return cached["payload"]
    try:
        payload = _build_directory_payload(period)
        _BUILDERS_PUBLIC_CACHE[period] = {"ts": now, "payload": payload}
        return payload
    finally:
        try:
            _BUILDERS_PUBLIC_LOCK.release()
        except Exception:
            pass


def _bust_builders_cache() -> None:
    _BUILDERS_PUBLIC_CACHE.clear()


@router.get("/api/builders/list")
async def api_builders_list(request: Request) -> JSONResponse:
    period = _normalize_period(request.query_params.get("timePeriod"))
    payload = get_public_builders_payload(period)
    return JSONResponse(payload)


@router.get("/api/builders/timeseries")
async def api_builders_timeseries(request: Request) -> JSONResponse:
    builder_raw = (request.query_params.get("builder") or "").strip()
    period = _normalize_period(request.query_params.get("timePeriod"))
    if not builder_raw:
        return JSONResponse({"ok": False, "error": "builder_required"}, status_code=400)
    key = _builder_key(builder_raw)
    # Pull live then persist; serve from DB
    series = _fetch_v1_volume_series(period)
    if series:
        _persist_volume_series(series)
    _ensure_builders_meta_table()

    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                """
            SELECT dt, volume, active_users, rank
            FROM pm_builders_volume_series
            WHERE builder_key = %s
            ORDER BY dt ASC
            LIMIT 365
            """,
                (key,),
            ).fetchall()
        return JSONResponse(
            {
                "ok": True,
                "builder_key": key,
                "time_period": period,
                "points": [
                    {
                        "dt": str(r["dt"]),
                        "volume": float(r["volume"] or 0),
                        "active_users": int(r["active_users"] or 0),
                        "rank": int(r["rank"] or 0),
                    }
                    for r in rows
                ],
            }
        )

    return await asyncio.to_thread(_db)


# ── Admin meta CRUD ──────────────────────────────────────────────────────────


@router.get("/api/admin/builders/meta/list")
async def admin_builders_meta_list(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    payload = get_public_builders_payload(
        _normalize_period(request.query_params.get("timePeriod"))
    )
    return JSONResponse(payload)


@router.post("/api/admin/builders/meta/upsert")
async def admin_builders_meta_upsert(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    raw_key = (body or {}).get("builder_key") or (body or {}).get("builder") or ""
    key = _builder_key(raw_key)
    if not key:
        return JSONResponse(
            {"ok": False, "error": "builder_key_required"}, status_code=400
        )
    display_name = str((body or {}).get("display_name") or "").strip()
    description = str((body or {}).get("description") or "").strip()
    website = str((body or {}).get("website") or "").strip()
    tags_raw = (body or {}).get("tags") or ""
    if isinstance(tags_raw, list):
        tags = ",".join(str(t).strip() for t in tags_raw if str(t).strip())
    else:
        tags = str(tags_raw or "").strip()
    featured = bool((body or {}).get("featured", False))
    try:
        sort_weight = int((body or {}).get("sort_weight") or 100)
    except Exception:
        sort_weight = 100
    _ensure_builders_meta_table()
    now = _now_iso()

    def _db():
        with db_connect() as conn:
            conn.execute(
                """
            INSERT INTO pm_builders_meta
            (builder_key, display_name, description, website, tags, featured, sort_weight, updated_at, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (builder_key) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                description  = EXCLUDED.description,
                website      = EXCLUDED.website,
                tags         = EXCLUDED.tags,
                featured     = EXCLUDED.featured,
                sort_weight  = EXCLUDED.sort_weight,
                updated_at   = EXCLUDED.updated_at
            """,
                (
                    key,
                    display_name,
                    description,
                    website,
                    tags,
                    featured,
                    sort_weight,
                    now,
                    now,
                ),
            )
            conn.commit()
        _bust_builders_cache()
        return JSONResponse({"ok": True})

    return await asyncio.to_thread(_db)


@router.post("/api/admin/builders/meta/delete")
async def admin_builders_meta_delete(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    key = _builder_key((body or {}).get("builder_key") or "")
    if not key:
        return JSONResponse(
            {"ok": False, "error": "builder_key_required"}, status_code=400
        )
    _ensure_builders_meta_table()

    def _db():
        with db_connect() as conn:
            conn.execute("DELETE FROM pm_builders_meta WHERE builder_key = %s", (key,))
            conn.commit()
        _bust_builders_cache()
        return JSONResponse({"ok": True})

    return await asyncio.to_thread(_db)
