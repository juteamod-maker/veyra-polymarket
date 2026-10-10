import asyncio
import ast
from datetime import datetime, timezone
import random
import re
import time
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts.dashboard_routes._shared import db_connect, get_request_identity
from src.utils.pm_http import pm_get, GAMMA  # T510 P0-1: unified UA + connection pool

router = APIRouter()

_GAMMA_CACHE: dict = {"ts": 0.0, "data": []}
_GAMMA_LOCK = threading.Lock()
_GAMMA_TTL = 300

_SYNTH_CACHE: dict[str, dict[str, object]] = {}
_SYNTH_LOCK = threading.Lock()
_SYNTH_TTL = 60
_SYNTH_POOL_SIZE = 240
_LANG_RE = re.compile(r"^[A-Za-z0-9-]{2,10}$")


def _fetch_gamma_events() -> list:
    try:
        data = pm_get(
            GAMMA,
            "/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 24,
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=6,
        )
        if data is None:
            return []
        markets = (
            data
            if isinstance(data, list)
            else (data.get("markets") or data.get("data") or [])
        )
        out = []
        for m in markets[:24]:
            if not isinstance(m, dict):
                continue
            title = str(m.get("question") or m.get("title") or "").strip()
            if not title:
                continue
            outcomes = m.get("outcomes") or []
            prices = m.get("outcomePrices") or []
            yes_price: float | None = None
            no_price: float | None = None
            if isinstance(outcomes, list) and isinstance(prices, list):
                for i, o in enumerate(outcomes):
                    label = str(o).strip().upper()
                    try:
                        p = float(prices[i]) if i < len(prices) else None
                    except (TypeError, ValueError):
                        p = None
                    if label == "YES":
                        yes_price = p
                    elif label == "NO":
                        no_price = p
            if yes_price is None and no_price is None:
                try:
                    yes_price = float(m.get("lastTradePrice") or m.get("price") or 0.5)
                except (TypeError, ValueError):
                    yes_price = 0.5
            if yes_price is not None and no_price is None:
                no_price = round(1.0 - yes_price, 4)
            if no_price is not None and yes_price is None:
                yes_price = round(1.0 - no_price, 4)
            try:
                vol = float(m.get("volume24hr") or m.get("volume") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            try:
                liq = float(m.get("liquidity") or m.get("liquidityNum") or 0)
            except (TypeError, ValueError):
                liq = 0.0
            try:
                end_iso = str(m.get("endDate") or m.get("end_date_iso") or "")
                if end_iso:
                    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                    now_dt = datetime.now(timezone.utc)
                    days_left = max(0, (end_dt - now_dt).days)
                else:
                    days_left = None
            except Exception:
                days_left = None
            image_url = str(m.get("image") or m.get("iconUrl") or "").strip() or None
            out.append(
                {
                    "slot_key": "gamma_"
                    + str(m.get("id") or m.get("conditionId") or len(out)),
                    "title": title,
                    "title_i18n_key": "",
                    "sort_order": 200 + len(out),
                    "enabled": True,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "volume_24h": vol,
                    "liquidity": liq,
                    "days_left": days_left,
                    "image_url": image_url,
                    "source": "gamma",
                }
            )
        return out
    except Exception:
        return []


def _gamma_cached() -> list:
    with _GAMMA_LOCK:
        now = time.time()
        if now - _GAMMA_CACHE["ts"] < _GAMMA_TTL:
            return list(_GAMMA_CACHE["data"])
    try:
        fresh = _fetch_gamma_events()
    except Exception:
        fresh = []
    with _GAMMA_LOCK:
        _GAMMA_CACHE["ts"] = time.time()
        _GAMMA_CACHE["data"] = fresh
    return fresh


def _merge_cards(db_cards: list, gamma_cards: list) -> list:
    seen: set = set()
    out: list = []
    for c in db_cards:
        c = dict(c)
        c.setdefault("yes_price", None)
        c.setdefault("no_price", None)
        c.setdefault("volume_24h", None)
        c.setdefault("liquidity", None)
        c.setdefault("days_left", None)
        c.setdefault("image_url", None)
        c["source"] = "db"
        out.append(c)
        seen.add(str(c.get("slot_key") or "").strip().lower())
    for g in gamma_cards:
        key = str(g.get("slot_key") or "").strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(g)
    return out[:30]


def _normalize_lang(raw: str | None) -> str:
    s = str(raw or "").strip()
    if not _LANG_RE.match(s):
        return "en"
    low = s.lower()
    if low.startswith("zh"):
        return "zh-CN"
    if low.startswith("fr"):
        return "fr"
    if low.startswith("ja"):
        return "ja"
    if low.startswith("ko"):
        return "ko"
    if low.startswith("th"):
        return "th"
    if low.startswith("vi"):
        return "vi"
    if low.startswith("es"):
        return "es"
    if low.startswith("pt"):
        return "pt"
    return "en"


def _fetch_synth_titles_from_db(
    lang: str, pool_size: int = _SYNTH_POOL_SIZE
) -> list[str]:
    fetch_limit = max(96, min(pool_size * 6, 1200))
    titles: list[str] = []
    en_strict = lang == "en"

    def _query_lang(conn, lang_code: str):
        return conn.execute(
            """
            SELECT title
            FROM event_translations
            WHERE lang=%s AND title IS NOT NULL AND TRIM(title) <> ''
            ORDER BY updated_at DESC NULLS LAST
            LIMIT %s
            """,
            (lang_code, fetch_limit),
        ).fetchall()

    def _clean_title(raw: object) -> str:
        t = str(raw or "").strip()
        if not t:
            return ""
        if t.startswith("{") and "title" in t:
            try:
                parsed = ast.literal_eval(t)
                if isinstance(parsed, dict):
                    title_val = str(parsed.get("title") or "").strip()
                    if title_val:
                        return title_val
            except Exception:
                pass
        return t

    with db_connect() as conn:
        rows = _query_lang(conn, lang)
        if not rows and lang != "en":
            rows = _query_lang(conn, "en")
        # Avoid zh-CN fallback when user language is English.
        if not rows and lang not in ("en", "zh-CN"):
            rows = _query_lang(conn, "zh-CN")
        if not rows:
            rows = conn.execute(
                """
                SELECT title
                FROM event_translations
                WHERE title IS NOT NULL AND TRIM(title) <> ''
                ORDER BY updated_at DESC NULLS LAST
                LIMIT %s
                """,
                (fetch_limit,),
            ).fetchall()

    seen: set[str] = set()
    for row in rows or []:
        raw = row["title"] if hasattr(row, "keys") else row[0]
        t = _clean_title(raw)
        if not t:
            continue
        if en_strict and any("\u4e00" <= ch <= "\u9fff" for ch in t):
            continue
        tl = t.lower()
        if tl in seen:
            continue
        seen.add(tl)
        titles.append(t)

    if not titles:
        return []

    if len(titles) <= pool_size:
        random.shuffle(titles)
        return titles

    return random.sample(titles, pool_size)


def _get_synth_titles_cached(lang: str) -> list[str]:
    now = time.time()
    with _SYNTH_LOCK:
        hit = _SYNTH_CACHE.get(lang) or {}
        ts = float(hit.get("ts") or 0.0)
        data = hit.get("data") if isinstance(hit.get("data"), list) else []
        if data and (now - ts) < _SYNTH_TTL:
            return list(data)

    fresh = _fetch_synth_titles_from_db(lang)
    with _SYNTH_LOCK:
        _SYNTH_CACHE[lang] = {"ts": now, "data": fresh}
    return fresh


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_admin(request: Request) -> bool:
    ident = get_request_identity(request) or {}
    return bool(ident.get("authenticated") and ident.get("is_admin"))


def _ensure_table() -> None:
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS homepage_cards (
                id SERIAL PRIMARY KEY,
                slot_key TEXT NOT NULL UNIQUE,
                title_i18n_key TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 100,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """)
        conn.execute(
            "ALTER TABLE homepage_cards ADD COLUMN IF NOT EXISTS title_i18n_key TEXT NOT NULL DEFAULT ''"
        )
        conn.execute("ALTER TABLE homepage_cards DROP COLUMN IF EXISTS title_en")
        conn.execute("ALTER TABLE homepage_cards DROP COLUMN IF EXISTS title_zh")
        conn.execute("ALTER TABLE homepage_cards DROP COLUMN IF EXISTS subtitle_en")
        conn.execute("ALTER TABLE homepage_cards DROP COLUMN IF EXISTS subtitle_zh")
        conn.execute(
            "ALTER TABLE homepage_cards DROP COLUMN IF EXISTS subtitle_i18n_key"
        )
        conn.commit()


def _seed_defaults() -> None:
    defaults = [
        ("event_1", "homeevent_1", 10),
        ("event_2", "homeevent_2", 20),
        ("event_3", "homeevent_3", 30),
        ("event_4", "homeevent_4", 40),
    ]
    now = _now_iso()
    with db_connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM homepage_cards").fetchone()
        if int((row["c"] if row else 0) or 0) > 0:
            return
        for slot_key, title_i18n_key, sort_order in defaults:
            conn.execute(
                """
                INSERT INTO homepage_cards
                (slot_key, title_i18n_key, sort_order, enabled, updated_at, created_at)
                VALUES (%s,%s,%s,TRUE,%s,%s)
                """,
                (
                    slot_key,
                    title_i18n_key,
                    sort_order,
                    now,
                    now,
                ),
            )
        conn.commit()


@router.get("/api/public/homepage/cards")
async def public_homepage_cards() -> JSONResponse:
    # T590 — sync DB ops moved off event loop
    def _db():
        _ensure_table()
        _seed_defaults()
        with db_connect() as conn:
            rows = conn.execute("""
                SELECT id, slot_key, title_i18n_key, sort_order, enabled, updated_at
                FROM homepage_cards
                WHERE enabled = TRUE
                ORDER BY sort_order ASC, id ASC
                """).fetchall()
        return [dict(r) for r in rows]

    db_cards = await asyncio.to_thread(_db)
    gamma_cards = _gamma_cached()
    merged = _merge_cards(db_cards, gamma_cards)
    return JSONResponse({"ok": True, "cards": merged})


@router.get("/api/public/homepage/synth-titles")
async def public_homepage_synth_titles(
    lang: str = "en", limit: int = 24
) -> JSONResponse:
    lang_norm = _normalize_lang(lang)
    lim = max(1, min(int(limit or 24), 48))

    pool = await asyncio.to_thread(_get_synth_titles_cached, lang_norm)
    merged = list(pool)
    if lang_norm == "en":
        fallback_langs = ("en",)
    elif lang_norm == "zh-CN":
        fallback_langs = ("en",)
    else:
        fallback_langs = ("en", "zh-CN")

    for fb_lang in fallback_langs:
        if len(merged) >= lim:
            break
        if fb_lang == lang_norm:
            continue
        fb_pool = await asyncio.to_thread(_get_synth_titles_cached, fb_lang)
        if not fb_pool:
            continue
        seen = {str(x).strip().lower() for x in merged}
        for t in fb_pool:
            ts = str(t).strip()
            if not ts:
                continue
            key = ts.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(ts)
            if len(merged) >= lim:
                break
    pool = merged

    if not pool:
        return JSONResponse({"ok": True, "lang": lang_norm, "titles": []})

    out = pool[:lim]
    if len(out) < lim and len(pool) > len(out):
        extra_n = lim - len(out)
        remain = pool[len(out) :]
        if remain:
            out.extend(remain[:extra_n])

    return JSONResponse({"ok": True, "lang": lang_norm, "titles": out})


@router.get("/api/system/homepage/cards")
async def system_homepage_cards(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    def _db():
        _ensure_table()
        _seed_defaults()
        with db_connect() as conn:
            rows = conn.execute("""
                SELECT id, slot_key, title_i18n_key, sort_order, enabled, updated_at
                FROM homepage_cards
                ORDER BY sort_order ASC, id ASC
                """).fetchall()
        return [dict(r) for r in rows]

    cards = await asyncio.to_thread(_db)
    return JSONResponse({"ok": True, "cards": cards})


@router.post("/api/system/homepage/cards/upsert")
async def system_homepage_cards_upsert(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    slot_key = str((body or {}).get("slot_key") or "").strip()
    title_i18n_key = str((body or {}).get("title_i18n_key") or "").strip()
    sort_order = int((body or {}).get("sort_order") or 100)
    enabled = bool((body or {}).get("enabled", True))

    if not slot_key:
        return JSONResponse(
            {"ok": False, "error": "slot_key_required"}, status_code=400
        )
    if not title_i18n_key:
        return JSONResponse(
            {"ok": False, "error": "title_i18n_key_required"}, status_code=400
        )

    now = _now_iso()

    def _db():
        _ensure_table()
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO homepage_cards
                (slot_key, title_i18n_key, sort_order, enabled, updated_at, created_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (slot_key) DO UPDATE SET
                    title_i18n_key = EXCLUDED.title_i18n_key,
                    sort_order = EXCLUDED.sort_order,
                    enabled = EXCLUDED.enabled,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    slot_key,
                    title_i18n_key,
                    sort_order,
                    enabled,
                    now,
                    now,
                ),
            )
            conn.commit()

    await asyncio.to_thread(_db)
    return JSONResponse({"ok": True})


@router.post("/api/system/homepage/cards/delete")
async def system_homepage_cards_delete(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    slot_key = str((body or {}).get("slot_key") or "").strip()
    if not slot_key:
        return JSONResponse(
            {"ok": False, "error": "slot_key_required"}, status_code=400
        )

    def _db():
        _ensure_table()
        with db_connect() as conn:
            conn.execute("DELETE FROM homepage_cards WHERE slot_key = %s", (slot_key,))
            conn.commit()

    await asyncio.to_thread(_db)
    return JSONResponse({"ok": True})
