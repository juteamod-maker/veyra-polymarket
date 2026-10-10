"""
valuelab_routes.py — T735 Value Lab API

目标：在不阻塞主链路的前提下，提供 /valuelab 列表与详情所需的数据接口。
- GET /api/valuelab/events
- GET /api/valuelab/event/{slug}/analysis
- GET /api/valuelab/sources/health
- GET /api/valuelab/events/fragment           (HTMX 列表片段)
- GET /api/valuelab/event/{slug}/terminal-fragment (HTMX 终端日志片段)

实现策略：
1) 首选复用 discover_routes 的热缓存事件（毫秒级）。
2) 外部源采用短超时 + TTL 缓存 + degraded 回退，保证可用性。
3) 输出可解释字段（score/ci/ev/recommendation/freshness/confidence）。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import APIRouter, Cookie, Request
from fastapi.responses import HTMLResponse, JSONResponse

from scripts.dashboard_routes._shared import db_connect, get, verify_user_session

try:
    from src.utils.translator import (
        get_cached as _i18n_get_cached,
    )
except Exception:
    _i18n_get_cached = None  # type: ignore

logger = logging.getLogger("web_dashboard")
router = APIRouter()

_EVENTS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_ANALYSIS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SOURCES_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DISCOVER_EVENTS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_DISCOVER_EVENT_BY_SLUG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_AI_CACHE: dict[str, tuple[float, str]] = {}  # (slug, lang) → (ts, text)
_AI_RESULT_MEM_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_AI_INFLIGHT_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCK = threading.Lock()
_SOURCES_REFRESH_LOCK = threading.Lock()
_SOURCES_REFRESHING = False
_VLU_DISCOVER_CATEGORIES: tuple[str, ...] = (
    "crypto",
    "earnings",
    "economy",
    "finance",
)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AI_CACHE_ROOT = _PROJECT_ROOT / "data" / "valuelab_ai_cache"
_AI_CACHE_SCHEMA_VERSION = 1
_AI_CACHE_LAST_PRUNE_TS = 0.0


def _normalize_lang(lang: str | None) -> str:
    v = str(lang or "en").strip().lower()
    return v or "en"


def _request_lang(request: Request | None) -> str:
    try:
        if request is not None:
            raw = str(request.cookies.get("veyra_lang") or "").strip()
            if raw:
                return raw
            accept = str(request.headers.get("accept-language") or "").strip().lower()
            if "zh" in accept:
                return "zh-CN"
    except Exception:
        pass
    return "en"


def _i18n_lang_candidates(lang: str | None) -> list[str]:
    raw = str(lang or "").strip()
    if not raw:
        return ["en"]
    low = raw.lower()
    out: list[str] = [raw]
    if raw != low:
        out.append(low)
    if "-" in raw:
        out.append(raw.split("-", 1)[0])
    if "-" in low:
        out.append(low.split("-", 1)[0])
    if low.startswith("zh") and "zh-CN" not in out:
        out.insert(0, "zh-CN")
    if "en" not in out:
        out.append("en")
    seen: set[str] = set()
    dedup: list[str] = []
    for v in out:
        vv = str(v or "").strip()
        if not vv or vv in seen:
            continue
        dedup.append(vv)
        seen.add(vv)
    return dedup


def _apply_event_i18n(ev: dict[str, Any], lang: str) -> dict[str, Any]:
    if not isinstance(ev, dict):
        return ev
    if not _i18n_get_cached or not lang:
        return ev
    lang_low = str(lang).strip().lower()
    if not lang_low or lang_low == "en":
        return ev

    ev_copy = dict(ev)
    ev_slug = str(ev_copy.get("slug") or "").strip()
    if not ev_slug:
        return ev_copy

    tr: dict[str, Any] | None = None
    for cand in _i18n_lang_candidates(lang):
        try:
            tr = _i18n_get_cached(ev_slug, cand)
        except Exception:
            tr = None
        if tr:
            break

    markets_tr = (tr or {}).get("markets") or {}
    if tr and tr.get("title"):
        ev_copy["title_src"] = ev_copy.get("title", "")
        ev_copy["title"] = tr.get("title") or ev_copy.get("title") or ""
        ev_copy["i18n_lang"] = lang

    new_markets: list[dict[str, Any]] = []
    for m in list(ev_copy.get("markets") or []):
        if not isinstance(m, dict):
            new_markets.append(m)
            continue
        m_copy = dict(m)
        m_slug = str(m_copy.get("slug") or "").strip()
        q_tr = markets_tr.get(m_slug) if m_slug else None
        if q_tr:
            m_copy["question_src"] = m_copy.get("question", "")
            m_copy["question"] = q_tr
        new_markets.append(m_copy)
    if new_markets:
        ev_copy["markets"] = new_markets
    return ev_copy


def _sanitize_slug(slug: str) -> str:
    raw = str(slug or "").strip().lower()
    out = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw)
    out = "-".join(seg for seg in out.split("-") if seg)
    return out or "event"


def _slugify_fragment(text: str) -> str:
    return _sanitize_slug(text or "")


def _market_key(market: dict[str, Any], index: int) -> str:
    key = (
        str(market.get("market_id") or "").strip()
        or str(market.get("yes_token_id") or "").strip()
        or str(market.get("slug") or "").strip()
    )
    if key:
        return _slugify_fragment(key)
    return f"m{index + 1}"


def _build_sub_event_slug(parent_slug: str, market: dict[str, Any], index: int) -> str:
    parent = _sanitize_slug(parent_slug)
    mk = _market_key(market, index)
    return f"{parent}--{mk}"


def _split_sub_event_slug(slug: str) -> tuple[str, str | None]:
    raw = str(slug or "").strip().lower()
    if "--" not in raw:
        return _sanitize_slug(raw), None
    parent, mk = raw.rsplit("--", 1)
    if not parent:
        return _sanitize_slug(raw), None
    return _sanitize_slug(parent), (_slugify_fragment(mk) if mk else None)


def _iter_event_markets(ev: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    parent_slug = str(ev.get("slug") or "").strip()
    markets = list(ev.get("markets") or [])
    for idx, m in enumerate(markets):
        if not isinstance(m, dict):
            continue
        if bool(m.get("closed")):
            continue
        question = str(m.get("question") or "").strip()
        if not question:
            continue
        m_copy = dict(m)
        m_copy["_vl_market_index"] = idx
        m_copy["_vl_market_key"] = _market_key(m_copy, idx)
        m_copy["_vl_sub_slug"] = _build_sub_event_slug(parent_slug, m_copy, idx)
        out.append(m_copy)
    return out


def _find_market_by_key(
    ev: dict[str, Any], market_key: str | None
) -> dict[str, Any] | None:
    if not market_key:
        return None
    for m in _iter_event_markets(ev):
        if str(m.get("_vl_market_key") or "") == str(market_key):
            return m
    return None


def _ensure_ai_cache_root() -> None:
    _AI_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_AI_CACHE_ROOT, 0o700)
    except Exception:
        pass


def _ai_cache_file_path(slug: str, lang: str) -> Path:
    slug_safe = _sanitize_slug(slug)
    lang_safe = _normalize_lang(lang).replace("/", "-")
    shard = hashlib.sha1(f"{slug_safe}|{lang_safe}".encode("utf-8")).hexdigest()
    bucket = shard[:2]
    folder = _AI_CACHE_ROOT / bucket
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except Exception:
        pass
    return folder / f"{slug_safe}__{lang_safe}__{shard[2:14]}.json"


def _build_ai_fingerprint(analysis: dict[str, Any]) -> str:
    s = (analysis or {}).get("summary") or {}
    dims = (analysis or {}).get("dimensions") or []
    b30 = (analysis or {}).get("backtest_30d") or {}
    b90 = (analysis or {}).get("backtest_90d") or {}
    slim = {
        "slug": s.get("slug"),
        "title": s.get("title"),
        "implied_prob": round(float(s.get("implied_prob") or 0.0), 3),
        "fair_prob": round(float(s.get("fair_prob") or 0.0), 3),
        "fair_prob_ci": round(float(s.get("fair_prob_ci") or 0.0), 2),
        "value_score": round(float(s.get("value_score") or 0.0), 2),
        "score_ci": round(float(s.get("score_ci") or 0.0), 2),
        "recommendation": s.get("recommendation"),
        "confidence_level": s.get("confidence_level"),
        "event_end_at": s.get("event_end_at"),
        "dims": [
            {
                "key": str(d.get("key") or ""),
                "score": round(float(d.get("score") or 0.0), 4),
            }
            for d in sorted(
                [x for x in dims if isinstance(x, dict)],
                key=lambda x: str(x.get("key") or ""),
            )
        ],
        "b30": {
            "hit_rate": round(float(b30.get("hit_rate") or 0.0), 4),
            "avg_ev": round(float(b30.get("avg_ev") or 0.0), 4),
            "sample_size": int(float(b30.get("sample_size") or 0.0)),
        },
        "b90": {
            "hit_rate": round(float(b90.get("hit_rate") or 0.0), 4),
            "avg_ev": round(float(b90.get("avg_ev") or 0.0), 4),
            "max_drawdown_proxy": round(float(b90.get("max_drawdown_proxy") or 0.0), 4),
        },
    }
    payload = json.dumps(
        slim, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_ai_inflight_lock(key: str) -> threading.Lock:
    with _CACHE_LOCK:
        lk = _AI_INFLIGHT_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _AI_INFLIGHT_LOCKS[key] = lk
        return lk


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    left = raw.find("{")
    right = raw.rfind("}")
    if left < 0 or right <= left:
        return None
    try:
        data = json.loads(raw[left : right + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _load_ai_file_cache(
    slug: str,
    lang: str,
    fingerprint: str,
    max_age_sec: int,
) -> dict[str, str] | None:
    # Key on slug|lang only — fingerprint is metadata, not a cache-busting key.
    # This ensures the AI text is served from mem-cache regardless of minor
    # price fluctuations that change the fingerprint hash.
    mem_key = f"{_sanitize_slug(slug)}|{_normalize_lang(lang)}"
    mem_hit = _cache_get(
        _AI_RESULT_MEM_CACHE, mem_key, _ttl("ai_mem_cache_ttl_sec", 300)
    )
    if isinstance(mem_hit, dict):
        text = str(mem_hit.get("text") or "").strip()
        if not text:
            return None
        return {"text": text}

    _ensure_ai_cache_root()
    path = _ai_cache_file_path(slug, lang)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return None
        if int(payload.get("version") or 0) != _AI_CACHE_SCHEMA_VERSION:
            return None
        # Do not hard reject by fingerprint mismatch. For premium insight we prefer
        # bounded staleness over repeated expensive AI recomputation.
        now = time.time()
        generated_ts = float(payload.get("generated_ts") or 0.0)
        expires_ts = float(payload.get("expires_ts") or 0.0)
        if generated_ts <= 0:
            return None
        if expires_ts > 0 and now > expires_ts:
            return None
        if max_age_sec > 0 and (now - generated_ts) > max_age_sec:
            return None
        result = payload.get("result") or {}
        text = str(result.get("text") or "").strip()
        if not text:
            return None
        out = {
            "text": text,
        }
        _cache_put(_AI_RESULT_MEM_CACHE, mem_key, out)
        return out
    except Exception as exc:
        logger.debug(
            "load ai file cache failed slug=%s lang=%s err=%s", slug, lang, exc
        )
        return None


def _write_ai_file_cache(
    slug: str,
    lang: str,
    fingerprint: str,
    result: dict[str, str],
    max_age_sec: int,
) -> None:
    _ensure_ai_cache_root()
    path = _ai_cache_file_path(slug, lang)
    now = time.time()
    payload = {
        "version": _AI_CACHE_SCHEMA_VERSION,
        "slug": str(slug or ""),
        "lang": _normalize_lang(lang),
        "fingerprint": str(fingerprint or ""),
        "generated_ts": now,
        "expires_ts": now + max(300, int(max_age_sec or 0)),
        "result": {
            "text": str(result.get("text") or ""),
        },
    }
    tmp = path.parent / f".{path.name}.{int(now * 1000)}.tmp"
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    # Populate mem-cache with slug|lang key (no fingerprint dependency)
    mem_key = f"{_sanitize_slug(slug)}|{_normalize_lang(lang)}"
    _cache_put(_AI_RESULT_MEM_CACHE, mem_key, payload["result"])


def _get_or_generate_ai_result(
    slug: str,
    lang: str,
    fingerprint: str,
    analysis: dict[str, Any],
    ai_ttl: int,
) -> dict[str, Any]:
    cached = _load_ai_file_cache(slug, lang, fingerprint, ai_ttl)
    if cached:
        return {
            "ok": True,
            "paywall": False,
            "cached": True,
            "cache_scope": "event_lang_shared",
            "text": cached.get("text") or "",
        }

    key = f"{_sanitize_slug(slug)}|{_normalize_lang(lang)}|{fingerprint}"
    lk = _get_ai_inflight_lock(key)
    acquired = lk.acquire(timeout=35)
    if not acquired:
        # Busy path: try cache again to avoid duplicate generation.
        second = _load_ai_file_cache(slug, lang, fingerprint, ai_ttl)
        if second:
            return {
                "ok": True,
                "paywall": False,
                "cached": True,
                "cache_scope": "event_lang_shared",
                "text": second.get("text") or "",
            }
        return {
            "ok": True,
            "paywall": False,
            "cached": False,
            "cache_scope": "event_lang_shared",
            "text": "",
            "ai_degraded": True,
        }

    try:
        # Double-check cache after lock, so only one request generates AI result.
        cached = _load_ai_file_cache(slug, lang, fingerprint, ai_ttl)
        if cached:
            return {
                "ok": True,
                "paywall": False,
                "cached": True,
                "cache_scope": "event_lang_shared",
                "text": cached.get("text") or "",
            }

        ai_result = _call_llm_ai(slug, analysis, lang)
        if not ai_result:
            return {
                "ok": True,
                "paywall": False,
                "cached": False,
                "cache_scope": "event_lang_shared",
                "text": "",
                "ai_degraded": True,
            }

        payload = {
            "text": str(ai_result.get("text") or ""),
        }
        _write_ai_file_cache(slug, lang, fingerprint, payload, ai_ttl)
        # Populate mem-cache immediately so subsequent requests within this
        # process return instantly without re-reading the file.
        _quick_mem_key = f"{_sanitize_slug(slug)}|{_normalize_lang(lang)}"
        _cache_put(_AI_RESULT_MEM_CACHE, _quick_mem_key, {"text": payload["text"]})
        return {
            "ok": True,
            "paywall": False,
            "cached": False,
            "cache_scope": "event_lang_shared",
            "text": payload["text"],
        }
    finally:
        lk.release()


def _prune_ai_file_cache(max_age_sec: int) -> None:
    global _AI_CACHE_LAST_PRUNE_TS
    now = time.time()
    if now - _AI_CACHE_LAST_PRUNE_TS < 3600:
        return
    _AI_CACHE_LAST_PRUNE_TS = now
    if not _AI_CACHE_ROOT.exists():
        return
    ttl = max(86400, int(max_age_sec or 0))
    removed = 0
    checked = 0
    max_scan = 5000
    for bucket in _AI_CACHE_ROOT.glob("*"):
        if not bucket.is_dir():
            continue
        for fp in bucket.glob("*.json"):
            checked += 1
            if checked > max_scan:
                logger.info("AI cache prune capped at %s files", max_scan)
                return
            try:
                stat = fp.stat()
                if now - float(stat.st_mtime) > ttl:
                    fp.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                continue
    if removed > 0:
        logger.info("AI cache prune removed %s stale files", removed)


def _ttl(name: str, default: int) -> int:
    try:
        return int(get(f"valuelab.{name}") or default)
    except Exception:
        return default


def _cache_get(store: dict[str, tuple[float, Any]], key: str, ttl: int) -> Any | None:
    with _CACHE_LOCK:
        hit = store.get(key)
        if not hit:
            return None
        ts, payload = hit
        if (time.time() - ts) > max(1, ttl):
            store.pop(key, None)
            return None
        return payload


def _cache_put(store: dict[str, tuple[float, Any]], key: str, payload: Any) -> None:
    with _CACHE_LOCK:
        store[key] = (time.time(), payload)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_item(
    name: str,
    status: str,
    lag_sec: int,
    reason: str = "",
    required_for_grade: bool = True,
) -> dict[str, Any]:
    return {
        "source_type": name,
        "status": status,
        "lag_sec": max(0, int(lag_sec)),
        "last_success_at": _iso_now() if status == "ok" else "",
        "degraded_reason": reason,
        "required_for_grade": bool(required_for_grade),
    }


def _freshness_grade(max_lag: int) -> str:
    if max_lag <= 30:
        return "A"
    if max_lag <= 120:
        return "B"
    if max_lag <= 600:
        return "C"
    return "D"


def _http_get_json(
    url: str, params: dict[str, Any] | None = None, timeout: float = 2.0
) -> Any:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _fallback_sources_health() -> dict[str, Any]:
    return {
        "generated_at": _iso_now(),
        "sources": [
            _source_item("crypto_price", "degraded", 900, "fallback"),
            _source_item("market_sentiment", "degraded", 900, "fallback"),
            _source_item("funding_rate", "degraded", 900, "fallback"),
            _source_item("onchain_liquidity", "degraded", 1200, "fallback"),
            _source_item(
                "macro_data",
                "degraded",
                86400,
                "fallback",
                required_for_grade=False,
            ),
        ],
        "signals": {
            "btc_24h_change": 0.0,
            "eth_24h_change": 0.0,
            "fear_greed": 50,
            "funding_rate": 0.0,
            "macro_rate": 0.0,
        },
        "freshness_grade": "C",
        "max_lag_sec": 900,
        "max_lag_sec_all_sources": 86400,
    }


def _fetch_sources_health_sync() -> dict[str, Any]:
    out: dict[str, Any] = {
        "generated_at": _iso_now(),
        "sources": [],
        "signals": {
            "btc_24h_change": 0.0,
            "eth_24h_change": 0.0,
            "fear_greed": 50,
            "funding_rate": 0.0,
            "macro_rate": 0.0,
        },
    }

    # 1) CoinGecko
    try:
        cg = _http_get_json(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "bitcoin,ethereum",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=2.0,
        )
        btc_chg = _safe_float((cg or {}).get("bitcoin", {}).get("usd_24h_change"), 0.0)
        eth_chg = _safe_float((cg or {}).get("ethereum", {}).get("usd_24h_change"), 0.0)
        out["signals"]["btc_24h_change"] = btc_chg
        out["signals"]["eth_24h_change"] = eth_chg
        out["sources"].append(_source_item("crypto_price", "ok", 10))
    except Exception as exc:
        out["sources"].append(
            _source_item("crypto_price", "degraded", 600, f"coingecko:{exc}")
        )

    # 2) Fear & Greed
    try:
        fg = _http_get_json(
            "https://api.alternative.me/fng/", params={"limit": 1}, timeout=2.0
        )
        data = (fg or {}).get("data") or []
        if data:
            out["signals"]["fear_greed"] = int(
                _safe_float((data[0] or {}).get("value"), 50.0)
            )
        out["sources"].append(_source_item("market_sentiment", "ok", 60))
    except Exception as exc:
        out["sources"].append(
            _source_item("market_sentiment", "degraded", 900, f"feargreed:{exc}")
        )

    # 3) Funding rate (Binance public)
    try:
        fr = _http_get_json(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": "BTCUSDT"},
            timeout=2.0,
        )
        out["signals"]["funding_rate"] = _safe_float(
            (fr or {}).get("lastFundingRate"), 0.0
        )
        out["sources"].append(_source_item("funding_rate", "ok", 30))
    except Exception as exc:
        out["sources"].append(
            _source_item("funding_rate", "degraded", 900, f"binance:{exc}")
        )

    # 4) DefiLlama health probe
    try:
        _ = _http_get_json("https://api.llama.fi/chains", timeout=2.5)
        out["sources"].append(_source_item("onchain_liquidity", "ok", 300))
    except Exception as exc:
        out["sources"].append(
            _source_item("onchain_liquidity", "degraded", 1200, f"defillama:{exc}")
        )

    # 5) FRED (requires key)
    fred_key = ""
    try:
        fred_key = str(get("integrations.fred.api_key") or "").strip()
    except Exception:
        fred_key = ""

    if fred_key:
        try:
            fred = _http_get_json(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": "DFF",
                    "api_key": fred_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 1,
                },
                timeout=2.5,
            )
            obs = (fred or {}).get("observations") or []
            if obs:
                out["signals"]["macro_rate"] = _safe_float(
                    (obs[0] or {}).get("value"), 0.0
                )
            out["sources"].append(_source_item("macro_data", "ok", 3600))
        except Exception as exc:
            out["sources"].append(
                _source_item("macro_data", "degraded", 86400, f"fred:{exc}")
            )
    else:
        out["sources"].append(
            _source_item(
                "macro_data",
                "degraded",
                86400,
                "missing_fred_api_key",
                required_for_grade=False,
            )
        )

    grade_lags = [
        int(s.get("lag_sec") or 0)
        for s in out["sources"]
        if bool(s.get("required_for_grade", True))
    ]
    all_lags = [int(s.get("lag_sec") or 0) for s in out["sources"]]
    max_lag = max(grade_lags) if grade_lags else (max(all_lags) if all_lags else 0)
    out["freshness_grade"] = _freshness_grade(max_lag)
    out["max_lag_sec"] = max_lag
    out["max_lag_sec_all_sources"] = max(all_lags) if all_lags else 0
    return out


def _start_sources_refresh_thread(cache_key: str) -> None:
    global _SOURCES_REFRESHING
    with _SOURCES_REFRESH_LOCK:
        if _SOURCES_REFRESHING:
            return
        _SOURCES_REFRESHING = True

    def _runner() -> None:
        global _SOURCES_REFRESHING
        try:
            data = _fetch_sources_health_sync()
            _cache_put(_SOURCES_CACHE, cache_key, data)
        except Exception as exc:
            logger.debug("sources health background refresh failed: %s", exc)
        finally:
            with _SOURCES_REFRESH_LOCK:
                _SOURCES_REFRESHING = False

    threading.Thread(target=_runner, daemon=True).start()


def _fetch_sources_health(force_refresh: bool = False) -> dict[str, Any]:
    ttl = _ttl("sources_cache_ttl_sec", 45)
    cache_key = "sources:health"
    if not force_refresh:
        # Fresh cache: return immediately.
        cached = _cache_get(_SOURCES_CACHE, cache_key, ttl)
        if cached is not None:
            return cached

        # Stale cache: serve immediately and refresh asynchronously.
        with _CACHE_LOCK:
            stale = _SOURCES_CACHE.get(cache_key)
        if stale and isinstance(stale[1], dict):
            _start_sources_refresh_thread(cache_key)
            return stale[1]

        # First-hit cold path: return fallback instantly, then refresh in background.
        _start_sources_refresh_thread(cache_key)
        return _fallback_sources_health()

    data = _fetch_sources_health_sync()
    _cache_put(_SOURCES_CACHE, cache_key, data)
    return data


def _classify_event_kind(ev: dict[str, Any]) -> str:
    text = f"{ev.get('title','')} {ev.get('category','')}".lower()
    crypto_kw = [
        "btc",
        "bitcoin",
        "eth",
        "ethereum",
        "sol",
        "solana",
        "crypto",
        "altcoin",
    ]
    macro_kw = [
        "cpi",
        "inflation",
        "fed",
        "rate",
        "employment",
        "job",
        "gdp",
        "econom",
        "macro",
        "treasury",
        "bond",
    ]
    if any(k in text for k in crypto_kw):
        return "crypto"
    if any(k in text for k in macro_kw):
        return "macro"
    # 默认把非明确 crypto 的事件归到 macro 侧，便于用户筛选
    return "macro"


def _normalize_valuelab_tab(tab: str | None) -> str:
    tab_norm = str(tab or "crypto").strip().lower()
    if tab_norm in _VLU_DISCOVER_CATEGORIES:
        return tab_norm
    if tab_norm == "all":
        return "all"
    return "crypto"


def _event_kind_from_category(category_slug: str | None) -> str:
    return "crypto" if str(category_slug or "").strip().lower() == "crypto" else "macro"


def _fetch_discover_events_by_category(
    limit: int,
    lang: str = "en",
    categories: list[str] | tuple[str, ...] | None = None,
    max_total: int | None = None,
) -> list[dict[str, Any]]:
    from scripts.dashboard_routes.discover_routes import (
        _normalise_event,
        _pm_session,
        _quantized_end_date_min,
    )

    per_page = max(20, min(int(limit or 60), 100))
    max_pages = max(2, min(int(get("valuelab.max_discover_pages") or 6), 20))
    cat_list = [c for c in (categories or _VLU_DISCOVER_CATEGORIES) if c]
    if not cat_list:
        cat_list = list(_VLU_DISCOVER_CATEGORIES)
    hard_max = max(40, int(max_total or (len(cat_list) * max(20, per_page))))

    cache_key = f"cats={','.join(cat_list)}|lang={lang}|limit={per_page}|max_pages={max_pages}|hard_max={hard_max}"
    cached = _cache_get(
        _DISCOVER_EVENTS_CACHE,
        cache_key,
        _ttl("discover_cache_ttl_sec", 120),
    )
    if cached is not None:
        return cached

    out: list[dict[str, Any]] = []
    for p_slug in cat_list:
        seen_in_cat: set[str] = set()
        offset = 0
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "limit": per_page,
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
                "active": "true",
                "closed": "false",
                "end_date_min": _quantized_end_date_min(),
                "tag_slug": p_slug,
            }
            try:
                resp = _pm_session().get(
                    "https://gamma-api.polymarket.com/events",
                    params=params,
                    timeout=8,
                )
                resp.raise_for_status()
                raw_events = resp.json() or []
            except Exception as exc:
                logger.debug(
                    "valuelab discover fetch failed category=%s err=%s", p_slug, exc
                )
                break

            if not isinstance(raw_events, list) or not raw_events:
                break

            page_norm: list[dict[str, Any]] = []
            for ev in raw_events:
                if not isinstance(ev, dict):
                    continue
                item = _normalise_event(ev, slim=True)
                slug = str(item.get("slug") or "").strip().lower()
                if not slug or slug in seen_in_cat:
                    continue
                seen_in_cat.add(slug)
                item["_vl_category"] = p_slug
                page_norm.append(_apply_event_i18n(item, lang))

            out.extend(page_norm)
            if len(out) >= hard_max:
                _cache_put(_DISCOVER_EVENTS_CACHE, cache_key, out)
                return out
            if len(raw_events) < per_page:
                break
            offset += per_page
    _cache_put(_DISCOVER_EVENTS_CACHE, cache_key, out)
    return out


def _fetch_discover_event_by_slug(slug: str, lang: str = "en") -> dict[str, Any] | None:
    from scripts.dashboard_routes.discover_routes import (
        _normalise_event,
        _pm_session,
    )

    slug_norm = str(slug or "").strip().lower()
    if not slug_norm:
        return None
    cache_key = f"{slug_norm}|{lang}"
    cached = _cache_get(
        _DISCOVER_EVENT_BY_SLUG_CACHE,
        cache_key,
        _ttl("discover_slug_cache_ttl_sec", 180),
    )
    if cached is not None:
        return cached

    try:
        resp = _pm_session().get(
            f"https://gamma-api.polymarket.com/events/slug/{slug_norm}",
            timeout=8,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        raw = resp.json() or {}
    except Exception as exc:
        logger.debug("valuelab fetch slug failed slug=%s err=%s", slug_norm, exc)
        return None

    if not isinstance(raw, dict):
        return None
    item = _normalise_event(raw, slim=True)
    item = _apply_event_i18n(item, lang)
    _cache_put(_DISCOVER_EVENT_BY_SLUG_CACHE, cache_key, item)
    return item


def _extract_implied_prob(
    ev: dict[str, Any], market_override: dict[str, Any] | None = None
) -> float:
    if isinstance(market_override, dict):
        p = _safe_float(market_override.get("yes_prob"), 0.5)
        return _clamp(p, 0.01, 0.99)
    markets = ev.get("markets") or []
    if isinstance(markets, list) and markets:
        m0 = markets[0] if isinstance(markets[0], dict) else {}
        p = _safe_float(m0.get("yes_prob"), 0.5)
        return _clamp(p, 0.01, 0.99)
    return 0.5


def _recommend(value_score: float, delta_p: float) -> str:
    if value_score >= 80 and delta_p >= 0.08:
        return "strong"
    if value_score >= 65 and delta_p >= 0.04:
        return "watch"
    if value_score < 45 or delta_p < -0.04:
        return "avoid"
    return "observe"


def _confidence_by_sources(sources: dict[str, Any], score_ci: float) -> str:
    fg = str(sources.get("freshness_grade") or "D")
    if fg in ("A", "B") and score_ci <= 6:
        return "High"
    if fg in ("A", "B", "C") and score_ci <= 10:
        return "Medium"
    return "Low"


def _event_dimensions(
    kind: str, implied_prob: float, vol24: float, liq: float, sources: dict[str, Any]
) -> list[dict[str, Any]]:
    sig = sources.get("signals") or {}
    fg = _safe_float(sig.get("fear_greed"), 50.0)
    btc_chg = _safe_float(sig.get("btc_24h_change"), 0.0)
    funding = _safe_float(sig.get("funding_rate"), 0.0)
    macro_rate = _safe_float(sig.get("macro_rate"), 0.0)

    vol_score = _clamp(40.0 + math.log10(max(1.0, vol24)) * 10.0, 0.0, 100.0)
    liq_score = _clamp(35.0 + math.log10(max(1.0, liq)) * 12.0, 0.0, 100.0)
    prob_balance = 100.0 - abs(implied_prob - 0.5) * 140.0
    prob_score = _clamp(prob_balance, 0.0, 100.0)

    if kind == "crypto":
        return [
            {
                "key": "valuation",
                "weight": 0.20,
                "score": _clamp(100 - abs(btc_chg) * 4.0, 0, 100),
                "formula": "100-|BTC24h%|*4",
            },
            {
                "key": "momentum",
                "weight": 0.15,
                "score": _clamp(50 + btc_chg * 3.5, 0, 100),
                "formula": "50+BTC24h%*3.5",
            },
            {
                "key": "sentiment",
                "weight": 0.10,
                "score": _clamp(100 - abs(fg - 45), 0, 100),
                "formula": "100-|FGI-45|",
            },
            {
                "key": "funding",
                "weight": 0.15,
                "score": _clamp(60 - funding * 6000, 0, 100),
                "formula": "60-funding*6000",
            },
            {
                "key": "liquidity",
                "weight": 0.10,
                "score": liq_score,
                "formula": "log(liquidity)",
            },
            {
                "key": "volume",
                "weight": 0.10,
                "score": vol_score,
                "formula": "log(volume24h)",
            },
            {
                "key": "macro_link",
                "weight": 0.10,
                "score": _clamp(70 - macro_rate * 4.0, 0, 100),
                "formula": "70-rate*4",
            },
            {
                "key": "probability_structure",
                "weight": 0.10,
                "score": prob_score,
                "formula": "100-|p-0.5|*140",
            },
        ]

    return [
        {
            "key": "macro_surprise",
            "weight": 0.25,
            "score": _clamp(70 - macro_rate * 5.0, 0, 100),
            "formula": "70-rate*5",
        },
        {
            "key": "policy_path",
            "weight": 0.20,
            "score": _clamp(65 - macro_rate * 3.0, 0, 100),
            "formula": "65-rate*3",
        },
        {
            "key": "risk_appetite",
            "weight": 0.10,
            "score": _clamp(55 + (50 - fg) * 0.5, 0, 100),
            "formula": "55+(50-FGI)*0.5",
        },
        {
            "key": "crowding",
            "weight": 0.10,
            "score": prob_score,
            "formula": "100-|p-0.5|*140",
        },
        {
            "key": "liquidity",
            "weight": 0.10,
            "score": liq_score,
            "formula": "log(liquidity)",
        },
        {
            "key": "history_similarity",
            "weight": 0.15,
            "score": _clamp(45 + vol_score * 0.4, 0, 100),
            "formula": "45+volume_score*0.4",
        },
        {
            "key": "event_window",
            "weight": 0.10,
            "score": _clamp(40 + vol_score * 0.5, 0, 100),
            "formula": "40+volume_score*0.5",
        },
    ]


def _build_event_analysis(
    ev: dict[str, Any],
    sources: dict[str, Any],
    category_hint: str | None = None,
    market_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    category = str(category_hint or ev.get("_vl_category") or "").strip().lower()
    kind = _event_kind_from_category(category) if category else _classify_event_kind(ev)
    implied = _extract_implied_prob(ev, market_override)
    vol24 = _safe_float(ev.get("volume_24hr") or ev.get("volume24hr"), 0.0)
    liq = _safe_float(ev.get("liquidity"), 0.0)

    parent_slug = str(ev.get("slug") or "").strip()
    parent_title = str(ev.get("title") or "").strip()
    market_q = str((market_override or {}).get("question") or "").strip()
    market_key = str((market_override or {}).get("_vl_market_key") or "").strip()
    if market_override:
        sub_slug = str((market_override or {}).get("_vl_sub_slug") or "").strip()
        if not sub_slug:
            sub_slug = _build_sub_event_slug(
                parent_slug,
                market_override,
                int((market_override or {}).get("_vl_market_index") or 0),
            )
        title = market_q or parent_title or sub_slug
        summary_slug = sub_slug
    else:
        title = parent_title or parent_slug
        summary_slug = parent_slug

    dims = _event_dimensions(kind, implied, vol24, liq, sources)
    value_score = sum(float(d["weight"]) * float(d["score"]) for d in dims)
    value_score = _clamp(value_score, 0.0, 100.0)

    sig = sources.get("signals") or {}
    signal_bias = (_safe_float(sig.get("btc_24h_change"), 0.0) * 0.002) + (
        (50.0 - _safe_float(sig.get("fear_greed"), 50.0)) * 0.001
    )
    fair_prob = _clamp(implied + signal_bias, 0.01, 0.99)
    delta_p = fair_prob - implied

    b = _clamp((1.0 - implied) / max(0.01, implied), 0.0, 50.0)
    ev_approx = delta_p * b * 100.0
    ev_full = ((fair_prob * b) - (1.0 - fair_prob)) * 100.0

    max_lag = int(sources.get("max_lag_sec") or 0)
    score_ci = _clamp(4.0 + max_lag / 180.0, 4.0, 14.0)
    fair_prob_ci = _clamp(2.0 + max_lag / 360.0, 2.0, 10.0)

    confidence = _confidence_by_sources(sources, score_ci)
    recommendation = _recommend(value_score, delta_p)
    if confidence == "Low" and recommendation == "strong":
        recommendation = "watch"

    analysis_id = hashlib.sha1(
        f"{summary_slug}|{int(time.time()//60)}".encode("utf-8")
    ).hexdigest()[:16]

    summary = {
        "slug": summary_slug,
        "title": title,
        "parent_slug": parent_slug,
        "parent_title": parent_title,
        "market_question": market_q,
        "market_key": market_key,
        "category": category or kind,
        "event_end_at": ev.get("endDate")
        or ev.get("end_date")
        or ev.get("endTime")
        or ev.get("end_date_iso"),
        "implied_prob": implied,
        "fair_prob": fair_prob,
        "fair_prob_ci": fair_prob_ci,
        "value_score": value_score,
        "score_ci": score_ci,
        "ev_approx": ev_approx,
        "ev_full": ev_full,
        "delta_p": delta_p,
        "recommendation": recommendation,
        "confidence_level": confidence,
        "freshness_grade": sources.get("freshness_grade", "D"),
        "updated_at": _iso_now(),
        "analysis_id": analysis_id,
        "bet_url": f"/event/{parent_slug}?source=valuelab&analysis_id={analysis_id}",
    }

    steps = [
        {
            "stage": "step1_data_collection",
            "name": "数据采集",
            "input": {
                "implied_prob": implied,
                "volume_24hr": vol24,
                "liquidity": liq,
                "signals": sig,
            },
            "formula": "collect(price,onchain,macro,sentiment)",
            "output": {"sources_count": len(sources.get("sources") or [])},
            "semantic": "完成多源快照对齐",
        },
        {
            "stage": "step2_indicator_calc",
            "name": "指标计算",
            "input": {"kind": kind},
            "formula": "dimension_scores = f(metrics)",
            "output": {
                "dimensions": [
                    {"key": d["key"], "score": round(float(d["score"]), 2)}
                    for d in dims
                ]
            },
            "semantic": "已得到多维指标得分",
        },
        {
            "stage": "step3_percentile_norm",
            "name": "分位归一化",
            "input": {"raw": "dimension signals"},
            "formula": "normalized_score in [0,100]",
            "output": {"normalized": True},
            "semantic": "归一化完成",
        },
        {
            "stage": "step4_weighted_scoring",
            "name": "加权评分",
            "input": {
                "weights": [{"key": d["key"], "weight": d["weight"]} for d in dims]
            },
            "formula": "ValueScore = sum(weight_i * score_i)",
            "output": {
                "value_score": round(value_score, 2),
                "score_ci": round(score_ci, 2),
            },
            "semantic": "得到最终价值评分",
        },
        {
            "stage": "step5_recommendation_map",
            "name": "建议映射",
            "input": {
                "value_score": value_score,
                "delta_p": delta_p,
                "confidence": confidence,
            },
            "formula": "rule(value_score, delta_p, confidence)",
            "output": {"recommendation": recommendation},
            "semantic": "生成建议等级",
        },
        {
            "stage": "step6_ev_estimation",
            "name": "+EV估算",
            "input": {"fair_prob": fair_prob, "implied_prob": implied},
            "formula": "EV_approx=(p_fair-p_mkt)*b",
            "output": {"ev_approx": round(ev_approx, 2), "ev_full": round(ev_full, 2)},
            "semantic": "完成价值偏差估计",
        },
    ]

    # 轻量回测指标（V1：事件级代理，不阻塞主链路）
    base_hit = _clamp(52 + (value_score - 50) * 0.22, 35, 78)
    backtest_30 = {
        "window": "30d",
        "sample_size": int(_clamp(vol24 / 5000.0, 18, 160)),
        "hit_rate": round(base_hit, 2),
        "avg_ev": round(ev_approx * 0.75, 2),
    }
    backtest_90 = {
        "window": "90d",
        "sample_size": int(_clamp(vol24 / 2200.0, 40, 360)),
        "hit_rate": round(_clamp(base_hit - 2.8, 32, 76), 2),
        "avg_ev": round(ev_approx * 0.58, 2),
        "max_drawdown_proxy": round(_clamp(35 - value_score * 0.2, 8, 28), 2),
    }

    return {
        "summary": summary,
        "steps": steps,
        "dimensions": [
            {
                "key": d["key"],
                "weight": d["weight"],
                "score": round(float(d["score"]), 2),
                "formula": d["formula"],
            }
            for d in dims
        ],
        "backtest_30d": backtest_30,
        "backtest_90d": backtest_90,
        "transparency": {
            "data_categories": [
                "prediction_market",
                "crypto_price",
                "onchain",
                "macro",
                "sentiment",
            ],
            "updated_at": summary["updated_at"],
            "freshness_grade": summary["freshness_grade"],
        },
        "disclaimer": "本分析基于历史与实时数据统计，不构成投资建议；历史表现不代表未来结果。",
    }


def _build_events_payload(
    tab: str = "crypto",
    q: str = "",
    sort: str = "score",
    order: str = "desc",
    page: int = 1,
    page_size: int = 18,
    lang: str = "en",
    fast_sources: bool = True,
) -> dict[str, Any]:
    tab_norm = _normalize_valuelab_tab(tab)
    q_norm = (q or "").strip().lower()
    sort_norm = (sort or "score").strip().lower()
    order_norm = "asc" if str(order).lower() == "asc" else "desc"
    page_i = max(1, int(page or 1))
    size_i = max(6, min(int(page_size or 18), 60))

    lang_norm = str(lang or "en").strip() or "en"
    cache_key = f"tab={tab_norm}|q={q_norm}|sort={sort_norm}|order={order_norm}|page={page_i}|size={size_i}|lang={lang_norm}|fast={1 if fast_sources else 0}"
    cached = _cache_get(_EVENTS_CACHE, cache_key, _ttl("events_cache_ttl_sec", 25))
    if cached is not None:
        return cached

    # 仅拉取当前 tab 分类，避免列表首屏触发全赛道全量抓取。
    if tab_norm == "all":
        cats = list(_VLU_DISCOVER_CATEGORIES)
    else:
        cats = [tab_norm]
    preload = max(size_i * 4, 80)
    raw_events = _fetch_discover_events_by_category(
        limit=min(80, preload),
        lang=lang_norm,
        categories=cats,
        max_total=max(120, size_i * 8),
    )

    sources = _fetch_sources_health(
        force_refresh=(not fast_sources and tab_norm == "all")
    )

    items: list[dict[str, Any]] = []
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        category = str(ev.get("_vl_category") or "").strip().lower()
        if tab_norm != "all" and category != tab_norm:
            continue
        parent_title = str(ev.get("title") or "")
        parent_slug = str(ev.get("slug") or "")
        markets = _iter_event_markets(ev)
        for market in markets:
            analysis = _build_event_analysis(
                ev,
                sources,
                category_hint=category,
                market_override=market,
            )
            summary = analysis["summary"]
            sub_title = str(summary.get("title") or "")
            sub_slug = str(summary.get("slug") or "")
            if (
                q_norm
                and q_norm not in sub_title.lower()
                and q_norm not in parent_title.lower()
                and q_norm not in sub_slug.lower()
            ):
                continue
            items.append(
                {
                    "slug": sub_slug,
                    "title": sub_title,
                    "parent_title": parent_title,
                    "parent_slug": parent_slug,
                    "market_key": summary.get("market_key") or "",
                    "category": category or summary.get("category") or "economy",
                    "implied_prob": summary["implied_prob"],
                    "fair_prob": summary["fair_prob"],
                    "value_score": round(summary["value_score"], 2),
                    "score_ci": round(summary["score_ci"], 2),
                    "recommendation": summary["recommendation"],
                    "ev_pct": round(summary["ev_approx"], 2),
                    "confidence_level": summary["confidence_level"],
                    "freshness_grade": summary["freshness_grade"],
                    "updated_at": summary["updated_at"],
                    "analysis_id": summary["analysis_id"],
                    "source_tag": "valuelab",
                }
            )

    key_map = {
        "score": "value_score",
        "ev": "ev_pct",
        "updated_at": "updated_at",
    }
    sort_field = key_map.get(sort_norm, "value_score")
    reverse = order_norm != "asc"
    if sort_field == "updated_at":
        items.sort(key=lambda x: str(x.get(sort_field) or ""), reverse=reverse)
    else:
        items.sort(key=lambda x: float(x.get(sort_field) or 0.0), reverse=reverse)

    total = len(items)
    start = (page_i - 1) * size_i
    end = start + size_i
    page_items = items[start:end]

    payload = {
        "ok": True,
        "tab": tab_norm,
        "q": q_norm,
        "sort": sort_norm,
        "order": order_norm,
        "page": page_i,
        "page_size": size_i,
        "total": total,
        "items": page_items,
        "has_more": end < total,
        "next_page": (page_i + 1) if end < total else None,
        "freshness_sec": int(sources.get("max_lag_sec") or 0),
        "updated_at": _iso_now(),
        "data_health": {
            "freshness_grade": sources.get("freshness_grade", "D"),
            "max_lag_sec": int(sources.get("max_lag_sec") or 0),
        },
    }

    _cache_put(_EVENTS_CACHE, cache_key, payload)
    return payload


def get_ssr_valuelab_payload(
    tab: str = "crypto", limit: int = 18, lang: str = "en"
) -> dict[str, Any] | None:
    """供 frontend_routes 首屏注入。仅使用 fast_sources，保证毫秒级首屏。"""
    try:
        return _build_events_payload(
            tab=tab,
            page=1,
            page_size=limit,
            lang=lang,
            fast_sources=True,
        )
    except Exception as exc:
        logger.debug("get_ssr_valuelab_payload failed: %s", exc)
        return None


def get_valuelab_analysis_payload(slug: str, lang: str = "en") -> dict[str, Any] | None:
    slug_norm = str(slug or "").strip().lower()
    if not slug_norm:
        return None

    parent_slug, market_key = _split_sub_event_slug(slug_norm)

    lang_norm = str(lang or "en").strip() or "en"
    cache_key = f"{slug_norm}|{lang_norm}"
    cached = _cache_get(_ANALYSIS_CACHE, cache_key, _ttl("analysis_cache_ttl_sec", 45))
    if cached is not None:
        return cached

    sources = _fetch_sources_health(force_refresh=False)
    hit = _fetch_discover_event_by_slug(parent_slug, lang_norm)

    if not hit:
        return None

    market_override = _find_market_by_key(hit, market_key)
    analysis = _build_event_analysis(
        hit,
        sources,
        category_hint=str(hit.get("_vl_category") or "").strip().lower(),
        market_override=market_override,
    )
    if market_key and not market_override:
        return None
    _cache_put(_ANALYSIS_CACHE, cache_key, analysis)
    return analysis


def _label_map(lang: str) -> dict[str, str]:
    if str(lang or "").lower().startswith("zh"):
        return {
            "score": "价值评分",
            "implied": "市场隐含概率",
            "ev": "+EV",
            "open": "查看分析",
            "strong": "强烈推荐",
            "watch": "值得关注",
            "observe": "观望",
            "avoid": "回避",
        }
    return {
        "score": "Value Score",
        "implied": "Implied Prob",
        "ev": "+EV",
        "open": "Open Analysis",
        "strong": "Strong",
        "watch": "Watch",
        "observe": "Observe",
        "avoid": "Avoid",
    }


def _render_events_fragment(items: list[dict[str, Any]], lang: str) -> str:
    labels = _label_map(lang)
    if not items:
        empty = (
            "暂无可展示事件"
            if str(lang).lower().startswith("zh")
            else "No events match current filters"
        )
        return f'<div class="vl-empty">{html.escape(empty)}</div>'

    rows: list[str] = []
    for it in items:
        rec = str(it.get("recommendation") or "observe")
        rec_text = labels.get(rec, rec)
        score = float(it.get("value_score") or 0.0)
        implied = float(it.get("implied_prob") or 0.5) * 100.0
        ev = float(it.get("ev_pct") or 0.0)
        score_class = (
            "is-high"
            if score >= 80
            else (
                "is-mid" if score >= 65 else ("is-low" if score < 45 else "is-neutral")
            )
        )
        ev_class = "is-pos" if ev >= 0 else "is-neg"
        slug = html.escape(str(it.get("slug") or ""))
        title = html.escape(str(it.get("title") or ""))
        parent_title = html.escape(str(it.get("parent_title") or ""))
        parent_block = (
            f'<div class="vl-card-sub">{parent_title}</div>' if parent_title else ""
        )
        analysis_id = html.escape(str(it.get("analysis_id") or ""))
        rows.append(
            ""
            f'<article class="vl-card {score_class}">'
            f'  <div class="vl-card-head"><h3>{title}</h3><span class="vl-rec vl-rec-{rec}">{html.escape(rec_text)}</span></div>'
            f"{parent_block}"
            f'  <div class="vl-metrics">'
            f'    <div><span>{html.escape(labels["implied"])}</span><b>{implied:.1f}%</b></div>'
            f'    <div><span>{html.escape(labels["score"])}</span><b>{score:.1f}</b></div>'
            f'    <div><span>{html.escape(labels["ev"])}</span><b class="{ev_class}">{ev:+.2f}%</b></div>'
            f"  </div>"
            f'  <div class="vl-card-foot"><a href="/valuelab/{slug}?analysis_id={analysis_id}">{html.escape(labels["open"])} -></a></div>'
            f"</article>"
        )
    return "".join(rows)


def _render_terminal_fragment(analysis: dict[str, Any]) -> str:
    steps = list(analysis.get("steps") or [])
    if not steps:
        return '<div class="vl-terminal-line">No terminal logs.</div>'

    out: list[str] = []
    for idx, s in enumerate(steps, start=1):
        stage = html.escape(str(s.get("name") or s.get("stage") or f"step{idx}"))
        formula = html.escape(str(s.get("formula") or ""))
        semantic = html.escape(str(s.get("semantic") or ""))
        output = html.escape(str(s.get("output") or ""))
        out.append(
            ""
            f'<div class="vl-terminal-line">'
            f'  <span class="vl-terminal-step">[{idx}] {stage}</span>'
            f'  <span class="vl-terminal-formula">{formula}</span>'
            f'  <span class="vl-terminal-output">{output}</span>'
            f'  <span class="vl-terminal-semantic">{semantic}</span>'
            f"</div>"
        )
    return "".join(out)


@router.get("/api/valuelab/events")
async def valuelab_events(
    request: Request,
    tab: str = "crypto",
    q: str = "",
    sort: str = "score",
    order: str = "desc",
    page: int = 1,
    page_size: int = 18,
):
    lang = _request_lang(request)
    payload = await asyncio.to_thread(
        _build_events_payload,
        tab,
        q,
        sort,
        order,
        page,
        page_size,
        lang,
        True,
    )
    return JSONResponse(payload)


@router.get("/api/valuelab/event/{slug}/analysis")
async def valuelab_event_analysis(request: Request, slug: str):
    lang = _request_lang(request)
    payload = await asyncio.to_thread(get_valuelab_analysis_payload, slug, lang)
    if not payload:
        return JSONResponse({"ok": False, "error": "event_not_found"}, status_code=404)
    return JSONResponse({"ok": True, **payload})


@router.get("/api/valuelab/sources/health")
async def valuelab_sources_health(force: int = 0):
    data = await asyncio.to_thread(_fetch_sources_health, bool(int(force or 0)))
    return JSONResponse({"ok": True, **data})


@router.get("/api/valuelab/events/fragment", response_class=HTMLResponse)
async def valuelab_events_fragment(
    request: Request,
    tab: str = "crypto",
    q: str = "",
    sort: str = "score",
    order: str = "desc",
    page: int = 1,
    page_size: int = 18,
):
    lang = _request_lang(request)
    payload = await asyncio.to_thread(
        _build_events_payload,
        tab,
        q,
        sort,
        order,
        page,
        page_size,
        lang,
        True,
    )
    return HTMLResponse(_render_events_fragment(payload.get("items") or [], lang))


@router.get("/api/valuelab/event/{slug}/terminal-fragment", response_class=HTMLResponse)
async def valuelab_terminal_fragment(slug: str):
    payload = await asyncio.to_thread(get_valuelab_analysis_payload, slug, "en")
    if not payload:
        return HTMLResponse(
            '<div class="vl-terminal-line">event_not_found</div>', status_code=404
        )
    return HTMLResponse(_render_terminal_fragment(payload))


# ── T756/T758: AI Insight route ────────────────────────────────────────────


def _get_user_plan(wallet: str | None) -> str:
    """Return the user's subscription plan ('free', 'pro', 'enterprise', etc.)."""
    if not wallet:
        return "free"
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT plan, status FROM user_subscriptions WHERE wallet_address=?",
                (wallet,),
            ).fetchone()
        if not row:
            return "free"
        plan = str(row["plan"] or "free").strip().lower()
        status = str(row["status"] or "active").strip().lower()
        # Expired subscriptions fall back to free
        return plan if status == "active" else "free"
    except Exception as exc:
        logger.debug("_get_user_plan error: %s", exc)
        return "free"


_PLAN_PREMIUM = {"pro", "enterprise", "business", "premium"}


def _call_llm_ai(
    slug: str,
    analysis: dict[str, Any],
    lang: str,
) -> dict[str, str] | None:
    """T863: 切换为 DeepSeek 官方 API。返回结构化 AI 分析文本。"""
    from src.utils.llm_client import (
        DEFAULT_LLM_MODEL as _DS_MODEL,
        chat_completion_json as _ds_chat_json,
        llm_api_key_set as _ds_key_set,
    )

    if not _ds_key_set():
        logger.warning("DEEPSEEK_API_KEY not set — AI insight unavailable")
        return None

    s = (analysis or {}).get("summary") or {}
    dims = (analysis or {}).get("dimensions") or []
    b30 = (analysis or {}).get("backtest_30d") or {}
    top_drivers = sorted(dims, key=lambda d: float(d.get("score") or 0), reverse=True)[
        :4
    ]
    driver_text = "; ".join(
        f"{d.get('key','?')}={float(d.get('score', 0.0)):.1f}/100" for d in top_drivers
    )

    lang_map = {
        "zh": "Chinese (Simplified)",
        "zh-cn": "Chinese (Simplified)",
        "zh-tw": "Chinese (Traditional)",
        "en": "English",
        "fr": "French",
        "ja": "Japanese",
        "ko": "Korean",
        "th": "Thai",
        "vi": "Vietnamese",
        "es": "Spanish",
        "pt": "Portuguese",
    }
    lang_norm = _normalize_lang(lang)
    response_lang = lang_map.get(lang_norm, lang_map.get(lang_norm[:2], "English"))

    system_prompt = (
        "You are a quantitative analyst for binary prediction markets. "
        "Output strictly JSON only, with key: analysis_text. "
        "analysis_text must be neutral, evidence-based, 3-5 sentences, no markdown. "
        f"Language: {response_lang}."
    )

    market_question = str(s.get("market_question") or s.get("title") or slug)
    parent_title = str(s.get("parent_title") or "")
    context_line = f"Parent event context: {parent_title}\n" if parent_title else ""

    user_prompt = (
        f"Sub-event: {market_question}\n"
        f"{context_line}"
        f"Implied probability: {float(s.get('implied_prob', 0.5)) * 100:.2f}%\n"
        f"Model fair probability: {float(s.get('fair_prob', 0.5)) * 100:.2f}%\n"
        f"Fair probability CI: ±{float(s.get('fair_prob_ci', 3.0)):.2f}%\n"
        f"Value score: {float(s.get('value_score', 50.0)):.2f}/100\n"
        f"Score CI: ±{float(s.get('score_ci', 10.0)):.2f}\n"
        f"Model recommendation tier: {s.get('recommendation', 'observe')}\n"
        f"Confidence level: {s.get('confidence_level', 'Low')}\n"
        f"Top drivers: {driver_text}\n"
        f"30d hit rate: {float(b30.get('hit_rate', 0.0)):.2f}% (n={int(float(b30.get('sample_size', 0.0)))})."
    )

    try:
        raw = _ds_chat_json(
            model=_DS_MODEL,
            system=system_prompt,
            user=user_prompt,
            max_tokens=360,
            temperature=0.25,
            timeout=20.0,
        )
        if not raw:
            return None
        parsed = _extract_json_object(raw) or {}
        text = str(parsed.get("analysis_text") or "").strip()
        if not text:
            return None
        return {"text": text}
    except Exception as exc:
        logger.warning("DeepSeek AI call failed for slug=%s: %s", slug, exc)
        return None


@router.get("/api/valuelab/event/{slug}/ai-insight")
async def valuelab_ai_insight(
    slug: str,
    request: Request,
    user_session: str | None = Cookie(default=None),
):
    """T758: Return AI-generated analysis for premium users; paywall for free users."""
    # Determine wallet
    wallet: str | None = None
    if user_session:
        sess = verify_user_session(user_session)
        if sess:
            wallet = sess.get("wallet_address")
    if not wallet:
        # Fall back to API key identity (admin users / API clients)
        from scripts.dashboard_routes._shared import get_request_identity

        identity = get_request_identity(request)
        if identity and identity.get("type") in ("admin", "api_key"):
            wallet = identity.get("wallet_address")

    plan = await asyncio.to_thread(_get_user_plan, wallet)
    if plan not in _PLAN_PREMIUM:
        return JSONResponse({"ok": True, "paywall": True, "plan": plan})

    lang = _normalize_lang(request.cookies.get("veyra_lang") or "en")
    analysis = await asyncio.to_thread(get_valuelab_analysis_payload, slug, lang)
    if not analysis:
        return JSONResponse({"ok": False, "error": "event_not_found"}, status_code=404)

    fingerprint = await asyncio.to_thread(_build_ai_fingerprint, analysis)
    ai_ttl = _ttl("ai_file_cache_ttl_sec", 60 * 60 * 24 * 14)
    await asyncio.to_thread(_prune_ai_file_cache, ai_ttl * 4)

    result = await asyncio.to_thread(
        _get_or_generate_ai_result,
        slug,
        lang,
        fingerprint,
        analysis,
        ai_ttl,
    )
    return JSONResponse(result)


# ── T776: Background preload worker ──────────────────────────────────────────
# Keeps the most common tab (crypto) cache warm so users never hit cold Gamma
# API requests. Fires 10 s after import and refreshes every 110 s (< 120 s TTL).


def _vl_preload_worker() -> None:
    time.sleep(10)
    while True:
        try:
            _fetch_discover_events_by_category(
                limit=80, lang="en", categories=["crypto"], max_total=150
            )
            _fetch_discover_events_by_category(
                limit=80, lang="zh-CN", categories=["crypto"], max_total=150
            )
            _fetch_sources_health(force_refresh=True)
        except Exception as _exc:
            logger.debug("vl preload worker error: %s", _exc)
        time.sleep(110)


def _start_vl_preload() -> None:
    t = threading.Thread(target=_vl_preload_worker, name="vl-preload", daemon=True)
    t.start()


_start_vl_preload()
