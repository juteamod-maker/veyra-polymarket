"""
dashboard_routes/system.py — 系统管理路由
包含: GET /api/env/list, POST /api/env/update,
      GET /api/risk/status, POST /api/risk/resume,
    GET /api/pm-city-monitor, POST /api/pm-city-sync,
    GET /api/pm-tags/monitor, POST /api/pm-tags/sync
"""

import json
import logging
import os
import re
import shutil
import subprocess
import zlib
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests as http_requests

from src.utils.pm_http import pm_session  # T510 P0-1: unified UA + connection pool
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import (
    _ENV_MANAGEABLE_KEYS,
    _ENV_READONLY_KEYS,
    CONFIG_PATH,
    _mask_value,
    _parse_env_file,
    _write_env_file,
    db_connect,
    get_request_identity,
    logger,
)

router = APIRouter()
_PM_TAGS_URL = "https://gamma-api.polymarket.com/tags"
_PM_EVENTS_URL = "https://gamma-api.polymarket.com/events"
_PM_SPORTS_URL = "https://gamma-api.polymarket.com/sports"
_PM_SOURCE_PREFIX = "pm_route:"
_ROUTE_TAG_SLUG_ALIASES = {
    # 前端 URL 使用 high-temperature，Gamma 事件标签为 highest-temperature。
    "high-temperature": "highest-temperature",
}
_PM_CATEGORY_DEFAULTS = [
    {
        "category_id": 910000001,
        "slug": "esports",
        "label": "Esports",
        "pm_path": "/esports",
    },
    {
        "category_id": 910000002,
        "slug": "crypto",
        "label": "Crypto",
        "pm_path": "/crypto",
    },
    {
        "category_id": 910000003,
        "slug": "earnings",
        "label": "Earnings",
        "pm_path": "/earnings",
    },
    {
        "category_id": 910000004,
        "slug": "geopolitics",
        "label": "Geopolitics",
        "pm_path": "/geopolitics",
    },
    {
        "category_id": 910000005,
        "slug": "sports",
        "label": "Sports",
        "pm_path": "/sports",
    },
    {
        "category_id": 910000006,
        "slug": "weather",
        "label": "Weather",
        "pm_path": "/weather",
    },
    {
        "category_id": 910000007,
        "slug": "economy",
        "label": "Economy",
        "pm_path": "/economy",
    },
    {
        "category_id": 910000008,
        "slug": "mentions",
        "label": "Mentions",
        "pm_path": "/mentions",
    },
    {"category_id": 910000009, "slug": "tech", "label": "Tech", "pm_path": "/tech"},
    {
        "category_id": 910000010,
        "slug": "elections",
        "label": "Elections",
        "pm_path": "/elections",
    },
    {
        "category_id": 910000011,
        "slug": "pop-culture",
        "label": "Pop Culture",
        "pm_path": "/pop-culture",
    },
    {
        "category_id": 910000012,
        "slug": "finance",
        "label": "Finance",
        "pm_path": "/finance",
    },
    {
        "category_id": 910000013,
        "slug": "politics",
        "label": "Politics",
        "pm_path": "/politics",
    },
    {"category_id": 910000014, "slug": "world", "label": "World", "pm_path": "/world"},
]
_PM_CATEGORY_ALIASES = {
    "financial": "finance",
    "economics": "economy",
    "geopolitical": "geopolitics",
    "political": "politics",
    "pop_culture": "pop-culture",
}


def _normalize_category_slug(value: Any) -> str:
    slug = str(value or "").strip().lower().replace("_", "-")
    slug = re.sub(r"[^a-z0-9\-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        return ""
    return _PM_CATEGORY_ALIASES.get(slug, slug)


_RISK_PAUSE_REASONS = {
    "daily",
    "cumulative",
    "balance_floor",
    "manual",
}

_SQL_DANGEROUS_TOKENS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "VACUUM",
    "CALL",
    "DO",
    "MERGE",
}


def _sanitize_readonly_query(raw_query: str) -> tuple[str | None, str | None]:
    """Validate and normalize a single readonly SQL statement."""
    query = str(raw_query or "").strip()
    if not query:
        return None, "query is required"

    # Allow trailing semicolons from common SQL console habits.
    query = re.sub(r";+\s*$", "", query).strip()
    if not query:
        return None, "query is required"

    # Block stacked statements; readonly console executes exactly one statement.
    if ";" in query:
        return None, "禁止多语句执行（仅允许单条只读查询）"

    # Allow comments before query body in SQL console.
    query_no_comments = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
    query_no_comments = re.sub(
        r"(?m)^\s*--.*$",
        " ",
        query_no_comments,
    ).strip()

    upper = query_no_comments.upper().lstrip()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return None, "仅允许 SELECT/WITH 查询（只读）"

    tokens = set(re.findall(r"\b[A-Z_]+\b", upper))
    blocked = sorted(tokens & _SQL_DANGEROUS_TOKENS)
    if blocked:
        return None, f"禁止使用: {', '.join(blocked)}"

    return query, None


def _serialize_row(row: Any, columns: list[str]) -> dict[str, Any]:
    """Normalize sqlite/psycopg row objects into plain dict rows."""
    if row is None:
        return {}

    if isinstance(row, dict):
        return {c: row.get(c) for c in columns}

    # sqlite3.Row / RealDictRow compatible path
    if hasattr(row, "keys"):
        out: dict[str, Any] = {}
        for c in columns:
            try:
                out[c] = row[c]
            except Exception:
                out[c] = None
        return out

    if isinstance(row, (list, tuple)):
        return {c: (row[i] if i < len(row) else None) for i, c in enumerate(columns)}

    if columns:
        return {columns[0]: row}
    return {}


def _normalize_pause_reason(raw: str | None) -> str | None:
    """规范化 pause_reason，避免出现 'daily ' 等脏值。"""
    reason = str(raw or "").strip().lower()
    if not reason:
        return None
    return reason if reason in _RISK_PAUSE_REASONS else reason


def _run_systemctl(action: str, service: str) -> tuple[bool, str]:
    """执行 systemctl 并返回 (是否成功, 诊断信息)。"""
    try:
        proc = subprocess.run(
            ["sudo", "systemctl", action, service],
            timeout=20,
            capture_output=True,
            text=True,
        )
    except Exception as e:
        return False, f"{action} {service} exception: {e}"

    ok = proc.returncode == 0
    details = " | ".join(
        p.strip() for p in [proc.stdout or "", proc.stderr or ""] if p and p.strip()
    )
    if not details:
        details = f"returncode={proc.returncode}"
    return ok, details


def _sync_risk_stop_params_on_resume(config_path: Path) -> dict:
    """恢复时同步停机参数：确保 emergency_freeze 为 false。"""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    risk_cfg = cfg.setdefault("risk", {})
    old_freeze = bool(risk_cfg.get("emergency_freeze", False))
    risk_cfg["emergency_freeze"] = False

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    return {
        "emergency_freeze_old": old_freeze,
        "emergency_freeze_new": False,
    }


# [T151A] POST /api/simulation/reset 已移除 — 模拟数据管理功能不再提供


# ── T150: PM 天气城市结算监控 ─────────────────────────────────────


def _extract_station_from_desc(desc: str) -> tuple[str, str, str]:
    """从 PM event description 提取结算源、站点代码、结算URL。

    支持 3 种结算源:
    - WU:   https://www.wunderground.com/history/daily/.../ICAO
    - NOAA: https://www.weather.gov/wrh/timeseries?site=ICAO
    - HKO:  https://www.weather.gov.hk/en/cis/climat.htm
    """
    # WU 类型
    wu = re.findall(r"https://www\.wunderground\.com/history/daily/[^\s\".]+", desc)
    if wu:
        url = wu[0].rstrip(".")
        parts = url.split("/")
        return "wunderground", parts[-1] if parts else "", url

    # NOAA 类型
    noaa = re.findall(
        r"https://www\.weather\.gov/wrh/timeseries\?site=([A-Z0-9]+)", desc
    )
    if noaa:
        site = noaa[0]
        return "noaa", site, f"https://www.weather.gov/wrh/timeseries?site={site}"

    # HKO 类型
    hko = re.findall(r"https://www\.weather\.gov\.hk/[^\s\".]+", desc)
    if hko:
        return "hko", "", hko[0].rstrip(".")

    return "", "", ""


@router.get("/api/pm-city-monitor")
async def api_pm_city_monitor(request: Request):
    """
    从 Gamma API 拉取所有活跃温度市场，提取结算链接中的站点代码，
    与系统 config weather_cities 对比，返回差异报告。
    支持 WU / NOAA / HKO 三种结算源。
    """
    try:
        # 1. 从 Gamma API 拉取温度事件
        gamma_url = "https://gamma-api.polymarket.com/events"
        pm_cities: dict[str, dict] = {}
        # T221: 从 config 读取 tag_slug，从 weather_event_map 读取（V2 配置路径）
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _cfg = json.load(f)
        _wem_cfg = _cfg.get("weather_event_map", {})
        _tag_slug = _wem_cfg.get("gamma_tag_slug", "daily-temperature")
        offset = 0
        while True:
            resp = pm_session().get(
                gamma_url,
                params={
                    "tag_slug": _tag_slug,
                    "closed": "false",
                    "limit": 100,
                    "offset": offset,
                },
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
            if not events:
                break
            for ev in events:
                slug = ev.get("slug", "")
                desc = ev.get("description", "")
                m = re.match(r"highest-temperature-in-(.+?)-on-(.+)", slug)
                if not m:
                    continue
                city_slug = m.group(1)
                if city_slug in pm_cities:
                    continue
                source, station, settle_url = _extract_station_from_desc(desc)
                pm_cities[city_slug] = {
                    "source": source,
                    "station": station,
                    "settle_url": settle_url,
                    "title": ev.get("title", ""),
                }
            offset += len(events)
            if len(events) < 100:
                break

        # 2. 读取系统 config 中的城市
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        sys_cities_list = cfg.get("weather_cities", {}).get("cities", [])
        sys_cities = {c["slug"]: c for c in sys_cities_list if c.get("slug")}

        # 3. 构建对比结果
        all_slugs = sorted(set(pm_cities) | set(sys_cities))
        results = []
        mismatch_count = 0
        for slug in all_slugs:
            pm = pm_cities.get(slug)
            sc = sys_cities.get(slug)
            pm_source = pm["source"] if pm else ""
            pm_station = pm["station"] if pm else ""
            pm_url = pm["settle_url"] if pm else ""
            sys_source = sc.get("settlement_source", "") if sc else ""
            sys_station = sc.get("icao", "") if sc else ""
            if sys_source == "noaa":
                sys_station = sc.get("noaa_site", sc.get("icao", ""))
            sys_url = sc.get("settlement_url", "") if sc else ""

            status = "match"
            diff_fields = []
            if not pm:
                status = "not_in_pm"
                mismatch_count += 1
            elif not sc:
                status = "not_in_system"
                mismatch_count += 1
            else:
                # 比对 source
                if pm_source and sys_source and pm_source != sys_source:
                    diff_fields.append("source")
                # 比对 station (ICAO/NOAA site)
                if pm_station and sys_station and pm_station != sys_station:
                    diff_fields.append("station")
                # 比对 URL
                if pm_url and sys_url and pm_url != sys_url:
                    diff_fields.append("url")
                if diff_fields:
                    status = "mismatch"
                    mismatch_count += 1

            results.append(
                {
                    "slug": slug,
                    "name": (sc or {}).get("name", slug.replace("-", " ").title()),
                    "pm_source": pm_source,
                    "pm_station": pm_station,
                    "pm_url": pm_url,
                    "sys_source": sys_source,
                    "sys_station": sys_station,
                    "sys_url": sys_url,
                    "status": status,
                    "diff_fields": diff_fields,
                }
            )

        return {
            "status": "ok",
            "pm_count": len(pm_cities),
            "sys_count": len(sys_cities),
            "mismatch_count": mismatch_count,
            "cities": results,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"[PMCityMonitor] 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _geocode_city(city_name: str) -> dict | None:
    """通过 Open-Meteo Geocoding API 查询城市坐标和时区。"""
    try:
        resp = http_requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city_name, "count": 1, "language": "en"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            r = results[0]
            tz_name = r.get("timezone", "UTC")
            # 从 timezone 名称计算 UTC offset（小时）
            utc_offset = 0
            try:
                from datetime import datetime
                from datetime import timezone as dt_tz
                from zoneinfo import ZoneInfo

                now_utc = datetime.now(dt_tz.utc)
                local_offset = now_utc.astimezone(ZoneInfo(tz_name)).utcoffset()
                if local_offset is not None:
                    utc_offset = local_offset.total_seconds() / 3600
                    # 保留整数或半小时精度
                    if utc_offset == int(utc_offset):
                        utc_offset = int(utc_offset)
            except Exception:
                pass
            return {
                "lat": round(r.get("latitude", 0), 4),
                "lon": round(r.get("longitude", 0), 4),
                "timezone": tz_name,
                "utc_offset": utc_offset,
            }
    except Exception as e:
        logger.warning(f"[PMCitySync] geocode failed for {city_name}: {e}")
    return None


def _ensure_pm_categories_table() -> None:
    """确保 PM 赛道类别表存在并预置平台公开赛道。"""
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pm_categories (
                category_id      BIGINT PRIMARY KEY,
                slug             TEXT NOT NULL UNIQUE,
                label            TEXT NOT NULL,
                source           TEXT NOT NULL DEFAULT 'pm_route',
                pm_path          TEXT,
                active           INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pm_categories_slug ON pm_categories(slug);
            CREATE INDEX IF NOT EXISTS idx_pm_categories_active ON pm_categories(active);
            """)
        for row in _PM_CATEGORY_DEFAULTS:
            conn.execute(
                """
                INSERT INTO pm_categories(category_id, slug, label, source, pm_path, active, created_at, updated_at)
                VALUES(?, ?, ?, 'pm_route', ?, 1, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    label=excluded.label,
                    pm_path=excluded.pm_path,
                    active=1,
                    updated_at=excluded.updated_at
                """,
                (
                    int(row["category_id"]),
                    str(row["slug"]),
                    str(row["label"]),
                    str(row["pm_path"]),
                    now,
                    now,
                ),
            )
        conn.commit()


def _active_pm_category_set() -> set[str]:
    try:
        _ensure_pm_categories_table()
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT slug FROM pm_categories WHERE active=1 ORDER BY slug ASC"
            ).fetchall()
        categories = {
            _normalize_category_slug(
                (dict(r).get("slug") if hasattr(r, "keys") else "")
            )
            for r in rows
            if _normalize_category_slug(
                (dict(r).get("slug") if hasattr(r, "keys") else "")
            )
        }
        if categories:
            return categories
    except Exception:
        pass

    return {
        _normalize_category_slug(str(row.get("slug") or ""))
        for row in _PM_CATEGORY_DEFAULTS
        if _normalize_category_slug(str(row.get("slug") or ""))
    }


def _ensure_pm_tags_table() -> None:
    """确保 PM 路由标签缓存表存在。"""
    _ensure_pm_categories_table()
    with db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pm_tags (
                tag_id          BIGINT PRIMARY KEY,
                slug            TEXT NOT NULL,
                label           TEXT NOT NULL,
                parent_tag_id   BIGINT,
                category_slug   TEXT,
                source          TEXT NOT NULL DEFAULT 'pm',
                active          INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pm_tags_slug ON pm_tags(slug);
            CREATE INDEX IF NOT EXISTS idx_pm_tags_label ON pm_tags(label);
            CREATE INDEX IF NOT EXISTS idx_pm_tags_source ON pm_tags(source);
            CREATE INDEX IF NOT EXISTS idx_pm_tags_category_slug ON pm_tags(category_slug);
            """)
        try:
            conn.execute("ALTER TABLE pm_tags ADD COLUMN category_slug TEXT")
        except Exception:
            pass
        conn.commit()


def _safe_int_or_none(v: Any) -> int | None:
    try:
        if v is None or str(v).strip() == "":
            return None
        return int(v)
    except Exception:
        return None


def _synthetic_tag_id(slug: str) -> int:
    base = 900_000_000
    crc = zlib.crc32(str(slug or "").strip().lower().encode("utf-8"))
    return base + (crc % 99_000_000)


def _pm_source_for_query_slug(query_slug: str) -> str:
    s = str(query_slug or "").strip().lower()
    return f"{_PM_SOURCE_PREFIX}{s}" if s else f"{_PM_SOURCE_PREFIX}"


def _query_slug_from_source(source: Any, fallback_slug: str = "") -> str:
    raw = str(source or "").strip().lower()
    if raw.startswith(_PM_SOURCE_PREFIX):
        q = raw[len(_PM_SOURCE_PREFIX) :].strip()
        if q:
            return q
    return str(fallback_slug or "").strip().lower()


def _normalize_pm_route_tag(
    raw: Any,
    *,
    query_slug: str | None = None,
    fallback_label: str | None = None,
    category_slug: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    tag_id = _safe_int_or_none(raw.get("id") or raw.get("tag_id"))
    slug = str(raw.get("slug") or "").strip().lower()
    label = str(raw.get("label") or raw.get("name") or fallback_label or "").strip()
    parent_tag_id = _safe_int_or_none(
        raw.get("parentTagId") or raw.get("parent_tag_id")
    )

    if not slug:
        slug = f"tag-{tag_id}"
    if not label:
        label = slug

    query = str(query_slug or slug).strip().lower() or slug
    category = _normalize_category_slug(
        category_slug
        or raw.get("category_slug")
        or raw.get("categorySlug")
        or raw.get("category")
    )

    return {
        "tag_id": tag_id,
        "slug": slug,
        "label": label,
        "parent_tag_id": parent_tag_id,
        "query_slug": query,
        "category_slug": category,
    }


def _fetch_pm_route_tags_from_gamma() -> list[dict[str, Any]]:
    """拉取可用于事件筛选的 PM 路由标签（URL slug 语义）。"""
    limit = 200
    max_pages = 12
    offset = 0

    tag_map: dict[str, dict[str, Any]] = {}
    tag_event_counts: dict[str, int] = {}
    tag_category_votes: dict[str, dict[str, int]] = defaultdict(dict)
    known_categories = _active_pm_category_set()

    for _ in range(max_pages):
        resp = pm_session().get(
            _PM_EVENTS_URL,
            params={
                "active": "true",
                "closed": "false",
                "include_tag": "true",
                "limit": limit,
                "offset": offset,
            },
            timeout=30,
        )
        resp.raise_for_status()
        events = resp.json()
        if not isinstance(events, list) or not events:
            break

        for event in events:
            tags = event.get("tags") if isinstance(event, dict) else []
            if not isinstance(tags, list):
                continue

            event_category = ""
            if isinstance(event, dict):
                event_category = _normalize_category_slug(event.get("category"))

            normalized_tags: list[dict[str, Any]] = []
            for raw_tag in tags:
                norm = _normalize_pm_route_tag(raw_tag, category_slug=event_category)
                if not norm:
                    continue
                normalized_tags.append(norm)

            if event_category not in known_categories:
                for norm in normalized_tags:
                    candidate = _normalize_category_slug(norm.get("slug"))
                    if candidate in known_categories:
                        event_category = candidate
                        break

            seen_slugs: set[str] = set()
            for norm in normalized_tags:
                slug = norm["slug"]
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)

                norm_category = _normalize_category_slug(norm.get("category_slug"))
                if (
                    norm_category not in known_categories
                    and event_category in known_categories
                ):
                    norm_category = event_category
                norm["category_slug"] = norm_category

                if slug not in tag_map:
                    tag_map[slug] = norm
                else:
                    if (
                        _safe_int_or_none(tag_map[slug].get("tag_id")) is None
                        and _safe_int_or_none(norm.get("tag_id")) is not None
                    ):
                        tag_map[slug]["tag_id"] = norm.get("tag_id")
                    if not str(tag_map[slug].get("label") or "").strip() and norm.get(
                        "label"
                    ):
                        tag_map[slug]["label"] = norm.get("label")
                    if not str(
                        tag_map[slug].get("query_slug") or ""
                    ).strip() and norm.get("query_slug"):
                        tag_map[slug]["query_slug"] = norm.get("query_slug")
                    if (
                        not _normalize_category_slug(tag_map[slug].get("category_slug"))
                        and norm_category
                    ):
                        tag_map[slug]["category_slug"] = norm_category

                if norm_category:
                    bucket = tag_category_votes.setdefault(slug, {})
                    bucket[norm_category] = int(bucket.get(norm_category, 0)) + 1

                tag_event_counts[slug] = int(tag_event_counts.get(slug, 0)) + 1

        if len(events) < limit:
            break
        offset += limit

    sports_tag_id = _safe_int_or_none((tag_map.get("sports") or {}).get("tag_id"))
    try:
        sport_resp = pm_session().get(_PM_SPORTS_URL, timeout=20)
        sport_resp.raise_for_status()
        sports_payload = sport_resp.json()
        if isinstance(sports_payload, list):
            for row in sports_payload:
                if not isinstance(row, dict):
                    continue
                sport_slug = str(row.get("sport") or "").strip().lower()
                if not sport_slug:
                    continue

                if sport_slug not in tag_map:
                    norm = _normalize_pm_route_tag(
                        {
                            "slug": sport_slug,
                            "label": sport_slug.upper(),
                            "parent_tag_id": sports_tag_id,
                        },
                        query_slug=sport_slug,
                        fallback_label=sport_slug.upper(),
                        category_slug="sports",
                    )
                    if norm:
                        tag_map[sport_slug] = norm
                elif (
                    sports_tag_id is not None
                    and _safe_int_or_none(tag_map[sport_slug].get("parent_tag_id"))
                    is None
                ):
                    tag_map[sport_slug]["parent_tag_id"] = sports_tag_id

                if (
                    _normalize_category_slug(tag_map[sport_slug].get("category_slug"))
                    != "sports"
                ):
                    tag_map[sport_slug]["category_slug"] = "sports"
                sport_bucket = tag_category_votes.setdefault(sport_slug, {})
                sport_bucket["sports"] = int(sport_bucket.get("sports", 0)) + 1

                tag_event_counts.setdefault(sport_slug, 0)
    except Exception as e:
        logger.warning(f"[PMTagsSync] sports metadata fetch failed: {e}")

    for route_slug, query_slug in _ROUTE_TAG_SLUG_ALIASES.items():
        base = tag_map.get(query_slug)
        if not base:
            continue
        if route_slug not in tag_map:
            tag_map[route_slug] = {
                "tag_id": None,
                "slug": route_slug,
                "label": route_slug.replace("-", " ").title(),
                "parent_tag_id": base.get("parent_tag_id"),
                "query_slug": query_slug,
                "category_slug": _normalize_category_slug(base.get("category_slug")),
            }
        elif not _normalize_category_slug(tag_map[route_slug].get("category_slug")):
            tag_map[route_slug]["category_slug"] = _normalize_category_slug(
                base.get("category_slug")
            )
        tag_event_counts[route_slug] = int(tag_event_counts.get(query_slug, 0))

    used_ids: set[int] = set()
    out: list[dict[str, Any]] = []
    for slug in sorted(tag_map.keys()):
        row = tag_map[slug]
        tag_id = _safe_int_or_none(row.get("tag_id"))
        if tag_id is None or tag_id <= 0:
            tag_id = _synthetic_tag_id(slug)
        while tag_id in used_ids:
            tag_id += 1
        used_ids.add(tag_id)

        category_slug = _normalize_category_slug(row.get("category_slug"))
        if category_slug not in known_categories:
            votes = tag_category_votes.get(slug, {})
            if votes:
                category_slug = sorted(
                    votes.items(),
                    key=lambda kv: (-int(kv[1]), str(kv[0])),
                )[0][0]
        if category_slug not in known_categories:
            direct_match = _normalize_category_slug(slug)
            if direct_match in known_categories:
                category_slug = direct_match
        if category_slug not in known_categories:
            category_slug = ""

        out.append(
            {
                "tag_id": tag_id,
                "slug": slug,
                "label": str(row.get("label") or slug).strip(),
                "parent_tag_id": _safe_int_or_none(row.get("parent_tag_id")),
                "category_slug": category_slug,
                "query_slug": str(row.get("query_slug") or slug).strip().lower(),
                "active_events_count": int(tag_event_counts.get(slug, 0)),
            }
        )

    out.sort(
        key=lambda t: (
            -int(t.get("active_events_count") or 0),
            str(t.get("slug") or ""),
        )
    )
    return out


@router.post("/api/pm-city-sync")
async def api_pm_city_sync(request: Request):
    """
    根据 PM 数据同步更新系统 config 中城市的结算信息。
    接收: {cities: [{slug, pm_source, pm_station, pm_url, name?, action?}]}
    - action="update"（默认）：更新已有城市的结算信息
    - action="add"：添加系统中缺失的新城市
    """
    try:
        body = await request.json()
        sync_list = body.get("cities", [])
        if not sync_list:
            return JSONResponse({"error": "无待同步城市"}, status_code=400)

        # 读取现有 config
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        cities_list = cfg.get("weather_cities", {}).get("cities", [])
        slug_index = {c["slug"]: i for i, c in enumerate(cities_list) if c.get("slug")}

        updated = []
        added = []
        skipped = []
        for item in sync_list:
            slug = item.get("slug", "")
            pm_source = item.get("pm_source", "")
            pm_station = item.get("pm_station", "")
            pm_url = item.get("pm_url", "")
            action = item.get("action", "update")

            if slug not in slug_index:
                if action == "add":
                    # 新增城市：从 slug 推导城市名，geocoding 获取坐标
                    city_name = item.get("name") or slug.replace("-", " ").title()
                    geo = _geocode_city(city_name)
                    if not geo:
                        skipped.append({"slug": slug, "reason": "geocode_failed"})
                        continue
                    new_city = {
                        "name": city_name,
                        "slug": slug,
                        "lat": geo["lat"],
                        "lon": geo["lon"],
                        "icao": pm_station or "",
                        "timezone": geo["timezone"],
                        "utc_offset": geo.get("utc_offset", 0),
                        "enabled": True,
                        "settlement_source": pm_source or "",
                        "settlement_url": pm_url or "",
                    }
                    if pm_source == "noaa" and pm_station:
                        new_city["noaa_site"] = pm_station
                    cities_list.append(new_city)
                    slug_index[slug] = len(cities_list) - 1
                    added.append({"slug": slug, "name": city_name, "geo": geo})
                    logger.info(
                        f"[PMCitySync] 新增城市: {city_name} ({slug}) @ {geo['lat']},{geo['lon']} tz={geo['timezone']}"
                    )
                else:
                    skipped.append({"slug": slug, "reason": "not_in_system"})
                continue

            idx = slug_index[slug]
            city = cities_list[idx]
            changes = {}
            # 更新 settlement_source
            if pm_source and pm_source != city.get("settlement_source"):
                changes["settlement_source"] = (
                    city.get("settlement_source"),
                    pm_source,
                )
                city["settlement_source"] = pm_source
            # 更新 settlement_url
            if pm_url and pm_url != city.get("settlement_url"):
                changes["settlement_url"] = (city.get("settlement_url"), pm_url)
                city["settlement_url"] = pm_url
            # 更新 station (icao / noaa_site)
            if pm_station:
                if pm_source == "noaa":
                    if pm_station != city.get("noaa_site"):
                        changes["noaa_site"] = (city.get("noaa_site"), pm_station)
                        city["noaa_site"] = pm_station
                    # 同步 icao（NOAA site 就是 ICAO）
                    if pm_station != city.get("icao"):
                        changes["icao"] = (city.get("icao"), pm_station)
                        city["icao"] = pm_station
                elif pm_source == "wunderground":
                    if pm_station != city.get("icao"):
                        changes["icao"] = (city.get("icao"), pm_station)
                        city["icao"] = pm_station
            if changes:
                updated.append({"slug": slug, "changes": changes})

        if not updated and not added:
            return {"status": "ok", "message": "无需同步", "updated": 0, "added": 0}

        # 备份
        backup_dir = Path(CONFIG_PATH).parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"config.json.pre_sync_{ts}"
        shutil.copy2(CONFIG_PATH, backup_path)

        # 写回
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")

        # 摘要
        summary = []
        for u in updated:
            parts = [f"{k}: {v[0]}→{v[1]}" for k, v in u["changes"].items()]
            summary.append(f"{u['slug']}: {', '.join(parts)}")
        for a in added:
            summary.append(
                f"NEW {a['slug']}: {a['name']} @ {a['geo']['lat']},{a['geo']['lon']}"
            )

        logger.info(
            f"[PMCitySync] 已同步 {len(updated)} 更新 + {len(added)} 新增: "
            + "; ".join(summary)
        )

        return {
            "status": "ok",
            "updated": len(updated),
            "added": len(added),
            "skipped": skipped,
            "details": updated,
            "added_details": added,
            "backup": str(backup_path.name),
        }
    except Exception as e:
        logger.error(f"[PMCitySync] 同步失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/pm-categories/list")
async def api_pm_categories_list(active_only: bool = True):
    """返回 PM 赛道类别列表（供 PM 标签监控和交易筛选复用）。"""
    try:
        _ensure_pm_categories_table()

        def _db():
            with db_connect() as conn:
                if active_only:
                    rows = conn.execute("""
                    SELECT category_id, slug, label, pm_path, source, active
                    FROM pm_categories
                    WHERE active=1
                    ORDER BY slug ASC
                    """).fetchall()
                else:
                    rows = conn.execute("""
                    SELECT category_id, slug, label, pm_path, source, active
                    FROM pm_categories
                    ORDER BY active DESC, slug ASC
                    """).fetchall()
            return rows

        rows = await asyncio.to_thread(_db)

        categories = []
        for r in rows:
            rd = dict(r)
            categories.append(
                {
                    "category_id": int(rd.get("category_id") or 0),
                    "slug": _normalize_category_slug(rd.get("slug")),
                    "label": str(rd.get("label") or "").strip(),
                    "pm_path": str(rd.get("pm_path") or "").strip(),
                    "source": str(rd.get("source") or "pm_route"),
                    "active": bool(rd.get("active", 0)),
                }
            )

        return {
            "status": "ok",
            "count": len(categories),
            "categories": categories,
        }
    except Exception as e:
        logger.error(f"[PMCategories] 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/pm-tags/monitor")
async def api_pm_tags_monitor(q: str = "", category: str = ""):
    """对比 PM 路由标签与系统缓存标签，支持关键字+赛道筛选。"""
    try:
        _ensure_pm_tags_table()
        key = str(q or "").strip().lower()
        category_filter = _normalize_category_slug(category)

        def _db_count():
            with db_connect() as conn:
                return conn.execute("SELECT COUNT(*) AS cnt FROM pm_tags").fetchone()

        sys_count_row = await asyncio.to_thread(_db_count)
        sys_count = int((dict(sys_count_row).get("cnt") if sys_count_row else 0) or 0)

        if not key and not category_filter:
            return {
                "status": "ok",
                "pm_count": 0,
                "sys_count": sys_count,
                "mismatch_count": 0,
                "tags": [],
                "needs_query": True,
                "ts": datetime.now(timezone.utc).isoformat(),
            }

        pm_tags = _fetch_pm_route_tags_from_gamma()
        if category_filter:
            pm_tags = [
                t
                for t in pm_tags
                if _normalize_category_slug(t.get("category_slug")) == category_filter
            ]

        def _db_local():
            with db_connect() as conn:
                return conn.execute("""
                SELECT tag_id, slug, label, parent_tag_id, category_slug, source, active, updated_at
                FROM pm_tags
                ORDER BY slug ASC
                """).fetchall()

        local_rows = await asyncio.to_thread(_db_local)

        pm_map = {str(t.get("slug") or "").strip().lower(): t for t in pm_tags}
        local_map: dict[str, dict[str, Any]] = {}
        for r in local_rows:
            rd = dict(r)
            slug = str(rd.get("slug") or "").strip().lower()
            if not slug:
                continue
            if (
                category_filter
                and _normalize_category_slug(rd.get("category_slug")) != category_filter
            ):
                continue
            local_map[slug] = rd

        all_slugs = sorted(set(pm_map) | set(local_map))
        rows: list[dict[str, Any]] = []
        mismatch_count = 0

        for slug in all_slugs:
            pm = pm_map.get(slug)
            local = local_map.get(slug)

            status = "match"
            diff_fields: list[str] = []

            if pm is None:
                status = "not_in_pm"
                mismatch_count += 1
            elif local is None:
                status = "not_in_system"
                mismatch_count += 1
            else:
                if str(pm.get("label") or "") != str(local.get("label") or ""):
                    diff_fields.append("label")
                if _safe_int_or_none(pm.get("parent_tag_id")) != _safe_int_or_none(
                    local.get("parent_tag_id")
                ):
                    diff_fields.append("parent_tag_id")
                pm_query_slug = (
                    str(pm.get("query_slug") or pm.get("slug") or "").strip().lower()
                )
                sys_query_slug = _query_slug_from_source(
                    local.get("source"),
                    str(local.get("slug") or ""),
                )
                if pm_query_slug != sys_query_slug:
                    diff_fields.append("query_slug")
                if _normalize_category_slug(
                    pm.get("category_slug")
                ) != _normalize_category_slug(local.get("category_slug")):
                    diff_fields.append("category_slug")
                if diff_fields:
                    status = "mismatch"
                    mismatch_count += 1

            row = {
                "tag_id": (
                    int(pm.get("tag_id"))
                    if pm and _safe_int_or_none(pm.get("tag_id")) is not None
                    else (
                        int(local.get("tag_id"))
                        if local and _safe_int_or_none(local.get("tag_id")) is not None
                        else 0
                    )
                ),
                "pm_slug": pm.get("slug") if pm else "",
                "pm_label": pm.get("label") if pm else "",
                "pm_parent_tag_id": pm.get("parent_tag_id") if pm else None,
                "pm_category_slug": (
                    _normalize_category_slug(pm.get("category_slug")) if pm else ""
                ),
                "pm_query_slug": (
                    str(pm.get("query_slug") or pm.get("slug") or "") if pm else ""
                ),
                "pm_active_events_count": (
                    int(pm.get("active_events_count") or 0) if pm else 0
                ),
                "sys_slug": local.get("slug") if local else "",
                "sys_label": local.get("label") if local else "",
                "sys_parent_tag_id": local.get("parent_tag_id") if local else None,
                "sys_category_slug": (
                    _normalize_category_slug(local.get("category_slug"))
                    if local
                    else ""
                ),
                "sys_query_slug": (
                    _query_slug_from_source(local.get("source"), local.get("slug"))
                    if local
                    else ""
                ),
                "active": bool(local.get("active", 0)) if local else False,
                "updated_at": local.get("updated_at") if local else None,
                "status": status,
                "diff_fields": diff_fields,
            }

            if key:
                hay = " ".join(
                    [
                        str(row.get("pm_slug") or "").lower(),
                        str(row.get("pm_label") or "").lower(),
                        str(row.get("pm_query_slug") or "").lower(),
                        str(row.get("pm_category_slug") or "").lower(),
                        str(row.get("sys_slug") or "").lower(),
                        str(row.get("sys_label") or "").lower(),
                        str(row.get("sys_query_slug") or "").lower(),
                        str(row.get("sys_category_slug") or "").lower(),
                        str(row.get("tag_id") or ""),
                    ]
                )
                if key not in hay:
                    continue

            rows.append(row)

        return {
            "status": "ok",
            "pm_count": len(pm_map),
            "sys_count": len(local_map),
            "mismatch_count": mismatch_count,
            "tags": rows,
            "needs_query": False,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"[PMTagsMonitor] 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/pm-tags/sync")
async def api_pm_tags_sync(request: Request):
    """同步选中的 PM 路由标签到系统缓存表。"""
    try:
        _ensure_pm_tags_table()
        body = await request.json()
        sync_list = body.get("tags") if isinstance(body, dict) else None
        if not isinstance(sync_list, list) or not sync_list:
            return JSONResponse({"error": "请选择要同步的标签"}, status_code=400)

        fetched = _fetch_pm_route_tags_from_gamma()
        fetched_by_slug = {str(t.get("slug") or "").strip().lower(): t for t in fetched}
        known_categories = _active_pm_category_set()

        normalized_map: dict[str, dict[str, Any]] = {}
        for item in sync_list:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or item.get("pm_slug") or "").strip().lower()
            if not slug:
                continue

            candidate = dict(fetched_by_slug.get(slug) or {})
            if not candidate:
                query_slug = (
                    str(item.get("query_slug") or item.get("pm_query_slug") or slug)
                    .strip()
                    .lower()
                )
                tag_id = _safe_int_or_none(item.get("id") or item.get("tag_id"))
                candidate = {
                    "tag_id": tag_id or _synthetic_tag_id(slug),
                    "slug": slug,
                    "label": str(
                        item.get("label") or item.get("pm_label") or slug
                    ).strip(),
                    "parent_tag_id": _safe_int_or_none(
                        item.get("parent_tag_id") or item.get("pm_parent_tag_id")
                    ),
                    "query_slug": query_slug,
                }

            category_slug = _normalize_category_slug(
                candidate.get("category_slug")
                or item.get("category_slug")
                or item.get("pm_category_slug")
            )
            if category_slug not in known_categories:
                maybe_match = _normalize_category_slug(candidate.get("slug"))
                category_slug = maybe_match if maybe_match in known_categories else ""
            candidate["category_slug"] = category_slug
            normalized_map[slug] = candidate

        normalized = list(normalized_map.values())
        if not normalized:
            return JSONResponse({"error": "无可同步标签"}, status_code=400)

        now = datetime.now(timezone.utc).isoformat()
        inserted = 0
        updated = 0

        def _db_sync():
            nonlocal inserted, updated
            with db_connect() as conn:
                existing_rows = conn.execute(
                    "SELECT tag_id, slug, label, parent_tag_id, category_slug, source FROM pm_tags"
                ).fetchall()
                existing_map = {int(r["tag_id"]): dict(r) for r in existing_rows}

                for tag in normalized:
                    tag_id = int(tag["tag_id"])
                    old = existing_map.get(tag_id)
                    query_slug = (
                        str(tag.get("query_slug") or tag.get("slug") or "")
                        .strip()
                        .lower()
                    )
                    source = _pm_source_for_query_slug(query_slug)
                    category_slug = _normalize_category_slug(tag.get("category_slug"))

                    if old is None:
                        inserted += 1
                    else:
                        old_parent = _safe_int_or_none(old.get("parent_tag_id"))
                        new_parent = _safe_int_or_none(tag.get("parent_tag_id"))
                        old_query_slug = _query_slug_from_source(
                            old.get("source"),
                            old.get("slug"),
                        )
                        old_category = _normalize_category_slug(
                            old.get("category_slug")
                        )
                        if (
                            str(old.get("slug") or "") != str(tag.get("slug") or "")
                            or str(old.get("label") or "")
                            != str(tag.get("label") or "")
                            or old_parent != new_parent
                            or old_query_slug != query_slug
                            or old_category != category_slug
                        ):
                            updated += 1

                    conn.execute(
                        """
                    INSERT INTO pm_tags(tag_id, slug, label, parent_tag_id, category_slug, source, active, created_at, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(tag_id) DO UPDATE SET
                        slug=excluded.slug,
                        label=excluded.label,
                        parent_tag_id=excluded.parent_tag_id,
                        category_slug=excluded.category_slug,
                        source=excluded.source,
                        active=1,
                        updated_at=excluded.updated_at
                    """,
                        (
                            tag_id,
                            tag["slug"],
                            tag["label"],
                            tag.get("parent_tag_id"),
                            category_slug,
                            source,
                            now,
                            now,
                        ),
                    )

                conn.commit()

        await asyncio.to_thread(_db_sync)

        logger.info(
            f"[PMTagsSync] synced={len(normalized)} inserted={inserted} updated={updated} mode=selected"
        )
        return {
            "status": "ok",
            "synced": len(normalized),
            "inserted": inserted,
            "updated": updated,
            "deleted": 0,
            "mode": "selected",
        }
    except Exception as e:
        logger.error(f"[PMTagsSync] 同步失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/pm-tags/clear")
async def api_pm_tags_clear(request: Request):
    """清空系统 PM 路由标签缓存。"""
    try:
        _ensure_pm_tags_table()
        body = await request.json()
        confirm = str((body or {}).get("confirm") or "").strip().upper()
        if confirm != "CLEAR":
            return JSONResponse({"error": "请传入 confirm=CLEAR"}, status_code=400)

        def _db_clear():
            with db_connect() as conn:
                cnt_row = conn.execute("SELECT COUNT(*) AS cnt FROM pm_tags").fetchone()
                deleted = int((dict(cnt_row).get("cnt") if cnt_row else 0) or 0)
                conn.execute("DELETE FROM pm_tags")
                conn.commit()
                return deleted

        deleted = await asyncio.to_thread(_db_clear)

        logger.warning(f"[PMTagsSync] cleared all pm_tags rows={deleted}")
        return {
            "status": "ok",
            "deleted": deleted,
        }
    except Exception as e:
        logger.error(f"[PMTagsSync] 清空失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/pm-tags/events")
async def api_pm_tag_events(slug: str, limit: int = 50, active: bool = True):
    """根据系统标签 slug 拉取对应 PM 事件（用于搜索/推荐过滤验证）。"""
    try:
        key = str(slug or "").strip().lower()
        if not key:
            return JSONResponse({"error": "missing slug"}, status_code=400)

        _ensure_pm_tags_table()
        alias_query_slug = (
            str(_ROUTE_TAG_SLUG_ALIASES.get(key, key) or key).strip().lower()
        )
        alias_source = _pm_source_for_query_slug(alias_query_slug)

        def _db_tag():
            with db_connect() as conn:
                return conn.execute(
                    """
                SELECT slug, label, source
                FROM pm_tags
                WHERE active=1 AND (slug=? OR source=?)
                ORDER BY CASE WHEN slug=? THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                    (key, alias_source, key),
                ).fetchone()

        row = await asyncio.to_thread(_db_tag)

        rowd = dict(row) if row else {}
        query_slug = (
            _query_slug_from_source(rowd.get("source"), rowd.get("slug"))
            if rowd
            else alias_query_slug
        )
        event_limit = max(1, min(int(limit or 50), 200))
        params = {
            "tag_slug": query_slug,
            "active": "true" if active else "false",
            "closed": "false" if active else "true",
            "limit": event_limit,
        }
        resp = pm_session().get(_PM_EVENTS_URL, params=params, timeout=25)
        resp.raise_for_status()
        payload = resp.json()
        events = payload if isinstance(payload, list) else []

        out = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            out.append(
                {
                    "id": str(ev.get("id") or ""),
                    "slug": str(ev.get("slug") or ""),
                    "title": str(ev.get("title") or ""),
                    "start_date": ev.get("startDate"),
                    "end_date": ev.get("endDate"),
                    "volume": ev.get("volume"),
                    "liquidity": ev.get("liquidity"),
                }
            )

        if not rowd and not out:
            return JSONResponse({"error": f"tag '{key}' not found"}, status_code=404)

        return {
            "status": "ok",
            "slug": key,
            "label": str(rowd.get("label") or key),
            "query_slug": query_slug,
            "count": len(out),
            "events": out,
        }
    except Exception as e:
        logger.error(f"[PMTagsEvents] 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/env/list")
async def api_env_list(request: Request):
    """
    返回可管理的 ENV key 列表及脱敏值。
    需要有效 TOTP 会话（中间件已验证）。
    """
    env_data = _parse_env_file()
    items = []
    for key in _ENV_MANAGEABLE_KEYS:
        val = env_data.get(key, "")
        items.append(
            {
                "key": key,
                "masked_value": _mask_value(val),
                "has_value": bool(val),
                "readonly": False,
            }
        )
    for key in sorted(_ENV_READONLY_KEYS):
        val = env_data.get(key, "")
        items.append(
            {
                "key": key,
                "masked_value": _mask_value(val),
                "has_value": bool(val),
                "readonly": True,
            }
        )
    return {"status": "ok", "keys": items}


@router.post("/api/env/update")
async def api_env_update(request: Request):
    """
    更新 .env 文件中的指定 key。
    请求体: {"key": "DEEPSEEK_API_KEY", "value": "sk-xxx"}
    需要有效 TOTP 会话（中间件已验证）。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    key = body.get("key", "").strip()
    value = body.get("value", "")

    if not key:
        return JSONResponse({"error": "missing 'key'"}, status_code=400)
    if key in _ENV_READONLY_KEYS:
        return JSONResponse(
            {"error": f"'{key}' 是只读密钥，不允许通过 Web 修改"},
            status_code=403,
        )
    if key not in _ENV_MANAGEABLE_KEYS:
        return JSONResponse(
            {"error": f"'{key}' 不在可管理密钥列表中"},
            status_code=403,
        )
    if not isinstance(value, str):
        return JSONResponse({"error": "'value' must be a string"}, status_code=400)

    try:
        env_data = _parse_env_file()
        old_masked = _mask_value(env_data.get(key, ""))
        env_data[key] = value
        _write_env_file(env_data)
        new_masked = _mask_value(value)
        logger.warning(f"[EnvUpdate] {key} 已更新: {old_masked} → {new_masked}")
        return {
            "status": "ok",
            "key": key,
            "masked_value": new_masked,
            "message": f"{key} 已更新（重启服务后生效）",
        }
    except Exception as e:
        logger.error(f"[EnvUpdate] 更新 {key} 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ── T39: 风控暂停状态查询与恢复 ──────────────────────────────────────


@router.get("/api/risk/status")
async def api_risk_status():
    """查询当前风控暂停状态（从 DB pause_state 表读取）。"""
    try:

        def _db():
            with db_connect() as conn:
                return conn.execute("SELECT key, value FROM pause_state").fetchall()

        rows = await asyncio.to_thread(_db)
        state = {r["key"]: r["value"] for r in rows}
        paused = state.get("globally_paused", "false").lower() == "true"
        reason = _normalize_pause_reason(state.get("pause_reason", ""))
        paused_cats = [c for c in state.get("paused_cats", "").split(",") if c.strip()]
        return {
            "ok": True,
            "status": "ok",
            "globally_paused": paused,
            "pause_reason": reason,
            "paused_categories": paused_cats,
        }
    except Exception as e:
        logger.error(f"[RiskStatus] 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/risk/resume")
async def api_risk_resume(request: Request):
    """人工恢复风控暂停：清除 DB pause_state 并同步维护参数。

    请求体: {"confirm": "RESUME"} （二次确认防误操作）
    P1-6: 暂停恢复必须人工触发，此按钮符合规则。
    T901: 需要 admin / super_admin 权限（全局风控状态不可由普通用户清除）。
    """
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if body.get("confirm") != "RESUME":
        return JSONResponse(
            {"error": '请发送 {"confirm": "RESUME"} 确认操作'}, status_code=400
        )

    try:
        from src.utils.db import save_pause_state

        def _db_keys():
            with db_connect() as conn:
                return conn.execute("SELECT key FROM pause_state").fetchall()

        rows = await asyncio.to_thread(_db_keys)
        pause_keys = [str(r["key"]) for r in rows]

        # 清除暂停状态与赛道连亏状态（streak_*），避免恢复后立刻二次暂停
        save_pause_state("globally_paused", "false")
        save_pause_state("pause_reason", "")
        save_pause_state("paused_cats", "")
        for key in pause_keys:
            if key.startswith("streak_"):
                save_pause_state(key, "0")

        # 读取当前余额，设为新 peak_nlv，防止重启后 check() 立即重触发暂停
        try:
            current_bal = 0.0

            def _db_bal():
                with db_connect() as conn:
                    return conn.execute(
                        "SELECT usdc_balance FROM balance_log ORDER BY ts DESC LIMIT 1"
                    ).fetchone()

            row = await asyncio.to_thread(_db_bal)
            if row:
                current_bal = float(row["usdc_balance"])
            if current_bal > 0:
                save_pause_state("peak_nlv_override", f"{current_bal:.4f}")
        except Exception:
            pass  # 读余额失败不阻断 resume 流程

        # 同步停机参数组：恢复时强制解除 emergency_freeze
        backup_dir = Path(CONFIG_PATH).parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"config.json.pre_risk_resume_{ts}"
        shutil.copy2(CONFIG_PATH, backup_path)
        sync_result = _sync_risk_stop_params_on_resume(CONFIG_PATH)

        logger.warning("[RiskResume] Dashboard 人工恢复：DB pause_state 已清除")

        return {
            "ok": True,
            "status": "ok",
            "message": "风控暂停已解除，停机参数已同步",
            "synced_stop_params": sync_result,
        }
    except Exception as e:
        logger.error(f"[RiskResume] 恢复失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _ensure_user_runtime_controls_table() -> None:
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_runtime_controls (
                wallet_address TEXT PRIMARY KEY,
                freeze_auto INTEGER NOT NULL DEFAULT 0,
                auto_trading_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """)


@router.get("/api/system/users/list")
async def api_system_users_list(request: Request):
    """超级管理员用户管理：用户列表（支持搜索/角色过滤/分页）。"""
    _ensure_user_runtime_controls_table()
    qp = request.query_params
    q = str(qp.get("q") or "").strip().lower()
    role = str(qp.get("role") or "").strip().lower()
    page = max(1, int(str(qp.get("page") or "1") or "1"))
    per_page = max(1, min(200, int(str(qp.get("per_page") or "50") or "50")))
    offset = (page - 1) * per_page
    now = datetime.now(timezone.utc).isoformat()

    where = []
    params: list = []
    if q:
        where.append(
            "(LOWER(u.wallet_address) LIKE ? OR LOWER(COALESCE(u.display_name,'')) LIKE ? OR LOWER(COALESCE(u.privy_user_id,'')) LIKE ? OR LOWER(COALESCE(u.polymarket_wallet,'')) LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if role:
        where.append("LOWER(COALESCE(u.role,'')) = ?")
        params.append(role)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    def _db():

        with db_connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM users u {where_sql}", params
            ).fetchone()
            total = int((dict(total_row) if total_row else {}).get("c") or 0)

            rows = conn.execute(
                f"""
            SELECT
                u.wallet_address,
                u.role,
                u.display_name,
                u.privy_user_id,
                u.auth_provider,
                u.polymarket_wallet,
                u.created_at,
                u.last_login_at,
                u.source_ip,
                u.updated_at,
                COALESCE(u.gray_dry_run, FALSE) AS gray_dry_run,
                COALESCE(s.active_sessions, 0) AS active_sessions,
                s.last_used_at AS last_session_used_at,
                COALESCE(rc.freeze_auto, 0) AS freeze_auto,
                COALESCE(rc.auto_trading_enabled, 1) AS auto_trading_enabled,
                rc.updated_at AS runtime_updated_at
            FROM users u
            LEFT JOIN (
                SELECT wallet_address, COUNT(*) AS active_sessions, MAX(last_used_at) AS last_used_at
                FROM user_sessions
                WHERE expires_at > ?
                GROUP BY wallet_address
            ) s ON s.wallet_address = u.wallet_address
            LEFT JOIN user_runtime_controls rc ON rc.wallet_address = u.wallet_address
            {where_sql}
            ORDER BY COALESCE(u.last_login_at, u.created_at) DESC, u.wallet_address ASC
            LIMIT ? OFFSET ?
            """,
                [now] + params + [per_page, offset],
            ).fetchall()

        users = []
        for r in rows:
            item = dict(r)
            item["freeze_auto"] = bool(int(item.get("freeze_auto") or 0))
            item["auto_trading_enabled"] = bool(
                int(item.get("auto_trading_enabled") or 0)
            )
            item["gray_dry_run"] = bool(item.get("gray_dry_run") or False)
            users.append(item)

        total_pages = max(1, (total + per_page - 1) // per_page)
        return {
            "status": "ok",
            "users": users,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }

    return await asyncio.to_thread(_db)


@router.post("/api/system/users/runtime")
async def api_system_users_runtime_update(request: Request):
    """更新用户运行控制（freeze_auto / auto_trading_enabled）。"""
    _ensure_user_runtime_controls_table()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    wallet = str(body.get("wallet_address") or "").strip().lower()
    if not wallet:
        return JSONResponse({"error": "missing wallet_address"}, status_code=400)
    if (
        body.get("freeze_auto") is None
        and body.get("auto_trading_enabled") is None
        and body.get("gray_dry_run") is None
    ):
        return JSONResponse(
            {
                "error": "missing runtime fields (freeze_auto/auto_trading_enabled/gray_dry_run)"
            },
            status_code=400,
        )

    def _db():

        with db_connect() as conn:
            urow = conn.execute(
                "SELECT wallet_address FROM users WHERE wallet_address=?", (wallet,)
            ).fetchone()
            if not urow:
                return JSONResponse({"error": "user not found"}, status_code=404)

            cur = conn.execute(
                "SELECT freeze_auto, auto_trading_enabled FROM user_runtime_controls WHERE wallet_address=?",
                (wallet,),
            ).fetchone()
            cur_freeze = int((dict(cur) if cur else {}).get("freeze_auto") or 0)
            cur_auto = int((dict(cur) if cur else {}).get("auto_trading_enabled") or 1)
            next_freeze = (
                1
                if bool(body.get("freeze_auto"))
                else 0 if body.get("freeze_auto") is not None else cur_freeze
            )
            next_auto = (
                1
                if bool(body.get("auto_trading_enabled"))
                else 0 if body.get("auto_trading_enabled") is not None else cur_auto
            )
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
            INSERT INTO user_runtime_controls(wallet_address, freeze_auto, auto_trading_enabled, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                freeze_auto=excluded.freeze_auto,
                auto_trading_enabled=excluded.auto_trading_enabled,
                updated_at=excluded.updated_at
            """,
                (wallet, next_freeze, next_auto, now),
            )

            # T303: 灰度 dry-run 标识写入 users 表（与 user_runtime_controls 解耦，
            # 因 gray_dry_run 用于全局总闸 + 用户级覆盖，不属于 runtime control 范畴）
            gray_changed = body.get("gray_dry_run") is not None
            next_gray = bool(body.get("gray_dry_run")) if gray_changed else None
            if gray_changed:
                conn.execute(
                    "UPDATE users SET gray_dry_run=%s, updated_at=%s WHERE wallet_address=%s",
                    (next_gray, now, wallet),
                )
            conn.commit()

        resp = {
            "status": "ok",
            "wallet_address": wallet,
            "freeze_auto": bool(next_freeze),
            "auto_trading_enabled": bool(next_auto),
        }
        if gray_changed:
            resp["gray_dry_run"] = bool(next_gray)
        return resp

    return await asyncio.to_thread(_db)


@router.post("/api/system/users/revoke-sessions")
async def api_system_users_revoke_sessions(request: Request):
    """撤销指定用户的全部会话。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    wallet = str(body.get("wallet_address") or "").strip().lower()
    if not wallet:
        return JSONResponse({"error": "missing wallet_address"}, status_code=400)

    def _db():

        with db_connect() as conn:
            cur = conn.execute(
                "DELETE FROM user_sessions WHERE wallet_address=?",
                (wallet,),
            )
            conn.commit()
        return {
            "status": "ok",
            "wallet_address": wallet,
            "revoked_sessions": int(cur.rowcount or 0),
        }

    return await asyncio.to_thread(_db)


@router.post("/api/system/users/unbind-pm-wallet")
async def api_system_users_unbind_pm_wallet(request: Request):
    """解绑用户 PM 钱包映射，并清理 PM profile 映射记录。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    wallet = str(body.get("wallet_address") or "").strip().lower()
    if not wallet:
        return JSONResponse({"error": "missing wallet_address"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()

    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT wallet_address FROM users WHERE wallet_address=?", (wallet,)
            ).fetchone()
            if not row:
                return JSONResponse({"error": "user not found"}, status_code=404)

            conn.execute(
                "UPDATE users SET polymarket_wallet=NULL, updated_at=? WHERE wallet_address=?",
                (now, wallet),
            )
            try:
                conn.execute(
                    "DELETE FROM user_pm_profiles WHERE wallet_address=?",
                    (wallet,),
                )
            except Exception:
                pass
            conn.commit()

        return {
            "status": "ok",
            "wallet_address": wallet,
            "message": "PM wallet 已解绑",
        }

    return await asyncio.to_thread(_db)


@router.post("/api/system/users/delete")
async def api_system_users_delete(request: Request):
    """删除用户及会话/运行控制数据（管理员操作）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    wallet = str(body.get("wallet_address") or "").strip().lower()
    confirm = str(body.get("confirm") or "").strip().upper()
    if not wallet:
        return JSONResponse({"error": "missing wallet_address"}, status_code=400)
    if confirm != "DELETE":
        return JSONResponse({"error": "confirm must be DELETE"}, status_code=400)

    identity = get_request_identity(request)
    actor_wallet = str((identity or {}).get("wallet_address") or "").strip().lower()
    actor_role = str((identity or {}).get("role") or "").strip().lower()
    if actor_role == "superadmin":
        actor_role = "super_admin"
    # API key admin path has no user role; treat as highest-level admin operation.
    if not actor_role:
        actor_role = "super_admin"

    def _db():

        with db_connect() as conn:
            urow = conn.execute(
                "SELECT role FROM users WHERE wallet_address=?", (wallet,)
            ).fetchone()
            if not urow:
                return JSONResponse({"error": "user not found"}, status_code=404)
            target_role = str((dict(urow) or {}).get("role") or "").strip().lower()
            if target_role == "superadmin":
                target_role = "super_admin"

            if actor_wallet and wallet == actor_wallet:
                return JSONResponse(
                    {"error": "cannot delete current logged-in user"},
                    status_code=403,
                )

            if target_role == "super_admin":
                return JSONResponse(
                    {"error": "super_admin user cannot be deleted via this endpoint"},
                    status_code=403,
                )

            if target_role == "admin" and actor_role != "super_admin":
                return JSONResponse(
                    {"error": "only super_admin can delete admin users"},
                    status_code=403,
                )

            conn.execute("DELETE FROM user_sessions WHERE wallet_address=?", (wallet,))
            conn.execute(
                "DELETE FROM user_runtime_controls WHERE wallet_address=?", (wallet,)
            )
            for table in (
                "user_pm_profiles",
                "user_pm_equity_snapshots",
                "user_pm_positions_snapshots",
                "user_pm_activity_snapshots",
            ):
                try:
                    conn.execute(
                        f"DELETE FROM {table} WHERE wallet_address=?", (wallet,)
                    )
                except Exception:
                    pass
            cur = conn.execute("DELETE FROM users WHERE wallet_address=?", (wallet,))
            conn.commit()

        return {
            "status": "ok",
            "wallet_address": wallet,
            "deleted": int(cur.rowcount or 0),
        }

    return await asyncio.to_thread(_db)


def _ensure_system_subscription_table() -> None:
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                wallet_address   TEXT PRIMARY KEY,
                plan             TEXT NOT NULL DEFAULT 'free',
                status           TEXT NOT NULL DEFAULT 'active',
                seats            INTEGER NOT NULL DEFAULT 1,
                expires_at       TEXT,
                notes            TEXT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            )
            """)


def _ensure_points_account(wallet_address: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO user_points(wallet_address, balance, total_earned, total_spent, updated_at)
            VALUES(?, 0, 0, 0, ?)
            ON CONFLICT(wallet_address) DO NOTHING
            """,
            (wallet_address, now),
        )
        conn.commit()


def _service_is_active(service: str) -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", service],
            timeout=10,
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and (proc.stdout or "").strip() == "active"
    except Exception:
        return False


@router.get("/api/system/admin/state")
async def api_system_admin_state(request: Request):
    """超级管理员系统状态：服务状态 + 维护模式 + 紧急冻结。
    维护模式（system.maintenance_mode）= 系统级，独立于交易冻结。
    紧急冻结（risk.emergency_freeze）= 用户服务层面，停止所有用户交易。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        system_cfg = cfg.get("system", {}) if isinstance(cfg, dict) else {}
        risk_cfg = cfg.get("risk", {}) if isinstance(cfg, dict) else {}
        # maintenance_mode is now a system-level flag, NOT risk.emergency_freeze
        maintenance_mode = bool(system_cfg.get("maintenance_mode", False))
        # emergency_freeze stays in risk config for bot backward compatibility
        emergency_freeze = bool(risk_cfg.get("emergency_freeze", False))
        return {
            "status": "ok",
            "maintenance_mode": maintenance_mode,
            "emergency_freeze": emergency_freeze,
            "dry_run": False,
            "services": {
                "copytrader": _service_is_active("polymarket-copytrader-v2"),
                "dashboard": _service_is_active("polymarket-dashboard-v2"),
            },
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"[SystemAdminState] 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/system/admin/maintenance")
async def api_system_admin_maintenance(request: Request):
    """超级管理员维护模式开关（系统级，使用 system.maintenance_mode，独立于 risk.emergency_freeze）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    enabled = bool(body.get("enabled"))
    reason = str(body.get("reason") or "manual")
    now = datetime.now(timezone.utc).isoformat()
    backup_dir = Path(CONFIG_PATH).parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup_path = (
        backup_dir
        / f"config.json.pre_maintenance_{now.replace(':', '').replace('-', '')}"
    )
    try:
        shutil.copy2(CONFIG_PATH, backup_path)
    except Exception:
        pass

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # Set system.maintenance_mode (system-level), NOT risk.emergency_freeze (user-level)
        system_cfg = cfg.setdefault("system", {})
        system_cfg["maintenance_mode"] = enabled
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

        def _db_maint():
            with db_connect() as conn:
                conn.execute("""
                CREATE TABLE IF NOT EXISTS pause_state (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
                if enabled:
                    conn.execute(
                        "INSERT INTO pause_state(key,value,updated_at) VALUES('maintenance_mode','true',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (now,),
                    )
                    conn.execute(
                        "INSERT INTO pause_state(key,value,updated_at) VALUES('maintenance_reason',?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (reason, now),
                    )
                else:
                    conn.execute(
                        "INSERT INTO pause_state(key,value,updated_at) VALUES('maintenance_mode','false',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (now,),
                    )
                    conn.execute(
                        "INSERT INTO pause_state(key,value,updated_at) VALUES('maintenance_reason','',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (now,),
                    )
                conn.commit()

        await asyncio.to_thread(_db_maint)

        return {
            "status": "ok",
            "maintenance_mode": enabled,
            "reason": reason if enabled else "",
            "updated_at": now,
        }
    except Exception as e:
        logger.error(f"[SystemMaintenance] 更新失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/system/admin/emergency-freeze-all")
async def api_system_admin_emergency_freeze_all(request: Request):
    """超级管理员紧急冻结所有用户交易（用户服务层面）。
    设置所有用户 user_runtime_controls.freeze_auto=1（DB级别），
    同时设置 risk.emergency_freeze=True 作为 Bot 的系统级安全开关。
    与维护模式解耦：维护模式针对系统级，此操作针对用户交易层。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    enabled = bool(body.get("enabled", True))
    now = datetime.now(timezone.utc).isoformat()

    try:
        # 1. Ensure runtime table exists and apply freeze state to ALL manual users.
        target_freeze = 1 if enabled else 0
        manual_role = "manual_user"

        def _db_freeze():
            with db_connect() as conn:
                conn.execute("""
                CREATE TABLE IF NOT EXISTS user_runtime_controls (
                    wallet_address      TEXT PRIMARY KEY,
                    freeze_auto         INTEGER NOT NULL DEFAULT 0,
                    auto_trading_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at          TEXT NOT NULL
                )
                """)

                # Create missing runtime rows for manual users to ensure full coverage.
                conn.execute(
                    """
                INSERT INTO user_runtime_controls(wallet_address, freeze_auto, auto_trading_enabled, updated_at)
                SELECT u.wallet_address, ?, 1, ?
                FROM users u
                LEFT JOIN user_runtime_controls rc ON rc.wallet_address = u.wallet_address
                WHERE LOWER(COALESCE(u.role, '')) = ?
                  AND rc.wallet_address IS NULL
                """,
                    (target_freeze, now, manual_role),
                )

                # Apply freeze flag to all manual users in one shot.
                conn.execute(
                    """
                UPDATE user_runtime_controls
                SET freeze_auto=?, updated_at=?
                WHERE wallet_address IN (
                    SELECT wallet_address
                    FROM users
                    WHERE LOWER(COALESCE(role, '')) = ?
                )
                """,
                    (target_freeze, now, manual_role),
                )

                row = conn.execute(
                    """
                SELECT COUNT(*) AS c
                FROM user_runtime_controls rc
                JOIN users u ON u.wallet_address = rc.wallet_address
                WHERE LOWER(COALESCE(u.role, '')) = ?
                  AND rc.freeze_auto = ?
                """,
                    (manual_role, target_freeze),
                ).fetchone()
                conn.commit()
                return row

        row = await asyncio.to_thread(_db_freeze)

        frozen_count = int((dict(row) if row else {}).get("c") or 0)

        # 2. Also set risk.emergency_freeze in config.json for bot safety (backward compat)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            risk_cfg = cfg.setdefault("risk", {})
            risk_cfg["emergency_freeze"] = enabled
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as cfg_err:
            logger.warning(
                f"[EmergencyFreezeAll] config.json 更新失败（已忽略）: {cfg_err}"
            )

        action = "冻结" if enabled else "解冻"
        logger.warning(
            f"[EmergencyFreezeAll] 管理员已{action}所有 {frozen_count} 个用户的交易"
        )
        return {
            "status": "ok",
            "enabled": enabled,
            "frozen_users": frozen_count,
            "affected_users": frozen_count,
            "updated_at": now,
        }
    except Exception as e:
        logger.error(f"[EmergencyFreezeAll] 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/system/points/overview")
async def api_system_points_overview(request: Request):
    """超级管理员积分总览。"""

    def _db():
        with db_connect() as conn:
            totals_row = conn.execute("""
            SELECT
                COUNT(*) AS users,
                COALESCE(SUM(balance),0) AS balance_sum,
                COALESCE(SUM(total_earned),0) AS earned_sum,
                COALESCE(SUM(total_spent),0) AS spent_sum
            FROM user_points
            """).fetchone()
            top_rows = conn.execute("""
            SELECT wallet_address, balance, total_earned, updated_at
            FROM user_points
            ORDER BY balance DESC, updated_at DESC
            LIMIT 20
            """).fetchall()

        totals = dict(totals_row) if totals_row else {}
        return {
            "status": "ok",
            "totals": {
                "users": int(totals.get("users") or 0),
                "balance_sum": int(totals.get("balance_sum") or 0),
                "earned_sum": int(totals.get("earned_sum") or 0),
                "spent_sum": int(totals.get("spent_sum") or 0),
            },
            "top_users": [dict(r) for r in top_rows],
        }

    return await asyncio.to_thread(_db)


@router.post("/api/system/points/adjust")
async def api_system_points_adjust(request: Request):
    """超级管理员积分调账（增减）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    wallet = str(body.get("wallet_address") or "").strip().lower()
    delta = int(body.get("delta") or 0)
    reason = str(body.get("reason") or "admin_adjust").strip()[:200]
    if not wallet:
        return JSONResponse({"error": "missing wallet_address"}, status_code=400)
    if delta == 0:
        return JSONResponse({"error": "delta cannot be 0"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    _ensure_points_account(wallet)

    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT balance, total_earned, total_spent FROM user_points WHERE wallet_address=?",
                (wallet,),
            ).fetchone()
            if not row:
                return JSONResponse({"error": "wallet not found"}, status_code=404)

            balance = int(row["balance"] or 0)
            earned = int(row["total_earned"] or 0)
            spent = int(row["total_spent"] or 0)
            next_balance = max(0, balance + delta)
            applied_delta = next_balance - balance
            if applied_delta == 0:
                return JSONResponse({"error": "delta has no effect"}, status_code=400)

            next_earned = earned + (applied_delta if applied_delta > 0 else 0)
            next_spent = spent + (-applied_delta if applied_delta < 0 else 0)

            conn.execute(
                """
            UPDATE user_points
            SET balance=?, total_earned=?, total_spent=?, updated_at=?
            WHERE wallet_address=?
            """,
                (next_balance, next_earned, next_spent, now, wallet),
            )
            conn.execute(
                """
            INSERT INTO point_transactions(wallet_address, type, amount, source, ref_id, description, created_at)
            VALUES(?, ?, ?, 'admin_adjust', NULL, ?, ?)
            """,
                (
                    wallet,
                    "earn" if applied_delta > 0 else "spend",
                    abs(applied_delta),
                    reason,
                    now,
                ),
            )
            conn.commit()

        return {
            "status": "ok",
            "wallet_address": wallet,
            "applied_delta": applied_delta,
            "new_balance": next_balance,
        }

    return await asyncio.to_thread(_db)


# ──────────────────────────────────────────────────────────────────────
# T491B: builder/taker 费率管理（超级管理员）
# ──────────────────────────────────────────────────────────────────────


@router.get("/api/system/fee/config")
async def api_system_fee_config(request: Request):
    """读取当前激活的费率配置 + 最近若干历史。"""

    def _db():
        with db_connect() as conn:
            cur = conn.execute(
                "SELECT id, fee_type, rate_bps, min_fee_usdc, max_fee_usdc, active, effective_at, created_at, updated_at FROM fee_config ORDER BY id DESC LIMIT 20"
            ).fetchall()
            cur_active = next((dict(r) for r in cur if r and (r["active"] or 0)), None)
        return {
            "status": "ok",
            "active": cur_active,
            "history": [dict(r) for r in cur],
            "doc_url": "https://docs.polymarket.com/developers/CLOB/orders/fees",
            "policy": {
                "unit": "bps",
                "default_when_missing": 0,
                "applied_at": "T491A trade settlement (after CLOB fill)",
            },
        }

    return await asyncio.to_thread(_db)


@router.post("/api/system/fee/update")
async def api_system_fee_update(request: Request):
    """超级管理员更新费率：写入新的 active=1 行，并把旧行 active=0。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    fee_type = (
        str(body.get("fee_type") or "builder_taker").strip()[:32] or "builder_taker"
    )
    try:
        rate_bps = int(body.get("rate_bps"))
    except Exception:
        return JSONResponse({"error": "rate_bps must be integer"}, status_code=400)
    if rate_bps < 0 or rate_bps > 1000:
        return JSONResponse(
            {"error": "rate_bps out of range [0..1000]"}, status_code=400
        )
    try:
        min_fee = float(body.get("min_fee_usdc") or 0.0)
        max_fee = float(body.get("max_fee_usdc") or 0.0)
    except Exception:
        min_fee = 0.0
        max_fee = 0.0
    if min_fee < 0 or max_fee < 0 or (max_fee > 0 and max_fee < min_fee):
        return JSONResponse({"error": "invalid min/max fee"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()

    def _db():
        with db_connect() as conn:
            conn.execute(
                "UPDATE fee_config SET active=0, updated_at=? WHERE active=1", (now,)
            )
            ins = conn.execute(
                """INSERT INTO fee_config(fee_type, rate_bps, min_fee_usdc, max_fee_usdc, active, effective_at, created_at, updated_at)
               VALUES(?, ?, ?, ?, 1, ?, ?, ?) RETURNING id""",
                (fee_type, rate_bps, min_fee, max_fee, now, now, now),
            ).fetchone()
            conn.commit()
        return {
            "status": "ok",
            "id": int(ins["id"]) if ins else None,
            "fee_type": fee_type,
            "rate_bps": rate_bps,
            "min_fee_usdc": min_fee,
            "max_fee_usdc": max_fee,
            "effective_at": now,
        }

    return await asyncio.to_thread(_db)


@router.get("/api/system/subscriptions/list")
async def api_system_subscriptions_list(request: Request):
    """超级管理员订阅管理列表。"""
    _ensure_system_subscription_table()

    qp = request.query_params
    q = str(qp.get("q") or "").strip().lower()
    plan = str(qp.get("plan") or "").strip().lower()
    status = str(qp.get("status") or "").strip().lower()
    page = max(1, int(str(qp.get("page") or "1") or "1"))
    per_page = max(1, min(200, int(str(qp.get("per_page") or "50") or "50")))
    offset = (page - 1) * per_page

    where = []
    params: list = []
    if q:
        where.append(
            "(LOWER(u.wallet_address) LIKE ? OR LOWER(COALESCE(u.display_name,'')) LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like])
    if plan:
        where.append("LOWER(COALESCE(s.plan,'free')) = ?")
        params.append(plan)
    if status:
        where.append("LOWER(COALESCE(s.status,'active')) = ?")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    def _db():

        with db_connect() as conn:
            total_row = conn.execute(
                f"""
            SELECT COUNT(*) AS c
            FROM users u
            LEFT JOIN user_subscriptions s ON s.wallet_address=u.wallet_address
            {where_sql}
            """,
                params,
            ).fetchone()
            total = int((dict(total_row) if total_row else {}).get("c") or 0)

            rows = conn.execute(
                f"""
            SELECT
                u.wallet_address,
                u.display_name,
                u.role,
                COALESCE(s.plan,'free') AS plan,
                COALESCE(s.status,'active') AS status,
                COALESCE(s.seats,1) AS seats,
                s.expires_at,
                s.notes,
                s.updated_at,
                u.last_login_at
            FROM users u
            LEFT JOIN user_subscriptions s ON s.wallet_address=u.wallet_address
            {where_sql}
            ORDER BY COALESCE(s.updated_at,u.updated_at,u.created_at) DESC, u.wallet_address ASC
            LIMIT ? OFFSET ?
            """,
                params + [per_page, offset],
            ).fetchall()

            plan_rows = conn.execute("""
            SELECT COALESCE(plan,'free') AS plan, COUNT(*) AS users
            FROM user_subscriptions
            GROUP BY COALESCE(plan,'free')
            ORDER BY users DESC
            """).fetchall()

        total_pages = max(1, (total + per_page - 1) // per_page)
        return {
            "status": "ok",
            "users": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "plan_distribution": [dict(r) for r in plan_rows],
        }

    return await asyncio.to_thread(_db)


@router.post("/api/system/subscriptions/update")
async def api_system_subscriptions_update(request: Request):
    """超级管理员更新用户订阅计划。"""
    _ensure_system_subscription_table()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    wallet = str(body.get("wallet_address") or "").strip().lower()
    plan = str(body.get("plan") or "free").strip().lower()
    status = str(body.get("status") or "active").strip().lower()
    seats = int(body.get("seats") or 1)
    expires_at = body.get("expires_at")
    notes = str(body.get("notes") or "")[:300]

    if not wallet:
        return JSONResponse({"error": "missing wallet_address"}, status_code=400)
    if plan not in {"free", "pro", "enterprise"}:
        return JSONResponse({"error": "invalid plan"}, status_code=400)
    if status not in {"active", "paused", "cancelled"}:
        return JSONResponse({"error": "invalid status"}, status_code=400)
    if seats < 1:
        return JSONResponse({"error": "seats must >= 1"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()

    def _db():
        with db_connect() as conn:
            urow = conn.execute(
                "SELECT wallet_address FROM users WHERE wallet_address=?", (wallet,)
            ).fetchone()
            if not urow:
                return JSONResponse({"error": "user not found"}, status_code=404)

            conn.execute(
                """
            INSERT INTO user_subscriptions(wallet_address, plan, status, seats, expires_at, notes, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                plan=excluded.plan,
                status=excluded.status,
                seats=excluded.seats,
                expires_at=excluded.expires_at,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
                (wallet, plan, status, seats, expires_at, notes, now, now),
            )
            conn.commit()

        return {
            "status": "ok",
            "wallet_address": wallet,
            "plan": plan,
            "subscription_status": status,
            "seats": seats,
        }

    return await asyncio.to_thread(_db)


# ── T278: 管理员只读 DB 查询 ──────────────────────────────────────────
@router.post("/api/system/admin/db-query")
async def api_system_admin_db_query(request: Request):
    """管理员只读 SQL 查询（仅 SELECT）。"""
    try:
        body = await request.json()
        query, err = _sanitize_readonly_query((body or {}).get("query") or "")
        if err:
            return JSONResponse(
                {"error": err},
                status_code=400 if err == "query is required" else 403,
            )

        # T590 — sync DB ops moved off event loop
        def _db():
            with db_connect() as conn:
                cur = conn.execute(query)
                columns_local = (
                    [desc[0] for desc in cur.description] if cur.description else []
                )
                rows_local = [
                    _serialize_row(row, columns_local) for row in cur.fetchall()
                ]
            return columns_local, rows_local

        columns, rows = await asyncio.to_thread(_db)
        return {"columns": columns, "rows": rows, "count": len(rows)}
    except Exception as e:
        logger.error(f"[AdminDBQuery] 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> bool:
    return bool(name and _IDENT_RE.fullmatch(name))


def _q_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _fmt_bytes(num: int | float | None) -> str:
    try:
        n = float(num or 0)
    except Exception:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.2f} {units[i]}"


def _load_table_columns(conn, table: str) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT
            c.column_name,
            c.data_type,
            c.is_nullable,
            c.column_default,
            c.is_identity,
            c.is_generated,
            CASE WHEN pk.column_name IS NOT NULL THEN 1 ELSE 0 END AS is_pk
        FROM information_schema.columns c
        LEFT JOIN (
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
             AND tc.table_name = kcu.table_name
            WHERE tc.table_schema='public'
              AND tc.table_name=?
              AND tc.constraint_type='PRIMARY KEY'
        ) pk ON pk.column_name = c.column_name
        WHERE c.table_schema='public' AND c.table_name=?
        ORDER BY c.ordinal_position
        """,
        (table, table),
    ).fetchall()


@router.get("/api/system/admin/db/tables")
async def api_system_admin_db_tables(request: Request):
    """DBA 表列表概览：行数估计、大小、最近统计时间。"""

    # T590 — sync DB ops moved off event loop
    def _db():
        with db_connect() as conn:
            return conn.execute("""
                SELECT
                    c.relname AS table_name,
                    COALESCE(s.n_live_tup, 0)::BIGINT AS est_rows,
                    COALESCE(pg_total_relation_size(c.oid), 0)::BIGINT AS total_bytes,
                    s.last_analyze,
                    s.last_autoanalyze,
                    s.last_vacuum,
                    s.last_autovacuum
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
                WHERE n.nspname='public' AND c.relkind='r'
                ORDER BY c.relname ASC
                """).fetchall()

    rows = await asyncio.to_thread(_db)

    tables = []
    for r in rows:
        item = dict(r)
        item["size_human"] = _fmt_bytes(item.get("total_bytes") or 0)
        tables.append(item)
    return {"status": "ok", "tables": tables, "count": len(tables)}


@router.get("/api/system/admin/db/table/{table}/schema")
async def api_system_admin_db_table_schema(request: Request, table: str):
    """DBA 表结构详情（字段、主键、类型、默认值）。"""
    if not _safe_ident(table):
        return JSONResponse({"error": "invalid table name"}, status_code=400)

    # T590 — sync DB ops moved off event loop
    def _db():
        with db_connect() as conn:
            exists = conn.execute(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema='public' AND table_name=?
                """,
                (table,),
            ).fetchone()
            if not exists:
                return None
            cols = [dict(r) for r in _load_table_columns(conn, table)]
            indexes = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname='public' AND tablename=?
                    ORDER BY indexname ASC
                    """,
                    (table,),
                ).fetchall()
            ]
            return {"columns": cols, "indexes": indexes}

    result = await asyncio.to_thread(_db)
    if result is None:
        return JSONResponse({"error": "table not found"}, status_code=404)

    return {
        "status": "ok",
        "table": table,
        "columns": result["columns"],
        "indexes": result["indexes"],
    }


@router.get("/api/system/admin/db/table/{table}/rows")
async def api_system_admin_db_table_rows(
    request: Request,
    table: str,
    page: int = 1,
    per_page: int = 50,
    order_by: str = "",
    order_dir: str = "desc",
    search: str = "",
):
    """DBA 图形化浏览行数据（分页/搜索/排序）。"""
    if not _safe_ident(table):
        return JSONResponse({"error": "invalid table name"}, status_code=400)

    page = max(1, int(page or 1))
    per_page = max(1, min(200, int(per_page or 50)))
    offset = (page - 1) * per_page
    dir_sql = "ASC" if str(order_dir).lower() == "asc" else "DESC"
    search_like = f"%{search}%" if search else ""

    # T590 — sync DB ops moved off event loop
    def _db():
        with db_connect() as conn:
            cols = [dict(r) for r in _load_table_columns(conn, table)]
            if not cols:
                return {"_err": "table has no columns"}
            col_names = [c["column_name"] for c in cols]
            ob = order_by
            if not ob or ob not in col_names:
                pk_cols = [
                    c["column_name"] for c in cols if int(c.get("is_pk") or 0) == 1
                ]
                ob = pk_cols[0] if pk_cols else col_names[0]

            table_sql = _q_ident(table)
            order_col_sql = _q_ident(ob)

            count_row = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM {table_sql} t
                WHERE (? = '' OR row_to_json(t)::text ILIKE ?)
                """,
                (search, search_like),
            ).fetchone()
            total = int((dict(count_row) if count_row else {}).get("c") or 0)

            rows_local = conn.execute(
                f"""
                SELECT *
                FROM {table_sql} t
                WHERE (? = '' OR row_to_json(t)::text ILIKE ?)
                ORDER BY {order_col_sql} {dir_sql}
                LIMIT ? OFFSET ?
                """,
                (search, search_like, per_page, offset),
            ).fetchall()
        return {
            "col_names": col_names,
            "rows": rows_local,
            "total": total,
            "order_by": ob,
        }

    res = await asyncio.to_thread(_db)
    if isinstance(res, dict) and res.get("_err"):
        return JSONResponse({"error": res["_err"]}, status_code=400)
    col_names = res["col_names"]
    rows = res["rows"]
    total = res["total"]
    order_by = res["order_by"]

    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "status": "ok",
        "table": table,
        "columns": col_names,
        "rows": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "order_by": order_by,
        "order_dir": dir_sql.lower(),
    }


@router.post("/api/system/admin/db/table/{table}/insert")
async def api_system_admin_db_table_insert(request: Request, table: str):
    """DBA 图形化 CRUD：插入一行。"""
    if not _safe_ident(table):
        return JSONResponse({"error": "invalid table name"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    values = body.get("values") if isinstance(body, dict) else None
    if not isinstance(values, dict) or not values:
        return JSONResponse({"error": "values is required"}, status_code=400)

    def _db():

        with db_connect() as conn:
            cols = [dict(r) for r in _load_table_columns(conn, table)]
            col_map = {c["column_name"]: c for c in cols}
            writable = {
                k: v
                for k, v in values.items()
                if k in col_map
                and str(col_map[k].get("is_generated") or "NEVER") == "NEVER"
                and str(col_map[k].get("is_identity") or "NO") == "NO"
            }
            if not writable:
                return JSONResponse(
                    {"error": "no writable columns in payload"},
                    status_code=400,
                )

            keys = list(writable.keys())
            table_sql = _q_ident(table)
            key_sql = ", ".join(_q_ident(k) for k in keys)
            placeholders = ", ".join(["?"] * len(keys))
            try:
                row = conn.execute(
                    f"INSERT INTO {table_sql} ({key_sql}) VALUES ({placeholders}) RETURNING *",
                    tuple(writable[k] for k in keys),
                ).fetchone()
                conn.commit()
            except Exception as e:
                msg = str(e)
                if "duplicate key value violates unique constraint" in msg:
                    return JSONResponse(
                        {
                            "error": "duplicate primary key / unique key",
                            "details": msg,
                        },
                        status_code=409,
                    )
                return JSONResponse(
                    {
                        "error": "insert failed",
                        "details": msg,
                    },
                    status_code=400,
                )

        return {"status": "ok", "row": dict(row) if row else None}

    return await asyncio.to_thread(_db)


@router.post("/api/system/admin/db/table/{table}/update")
async def api_system_admin_db_table_update(request: Request, table: str):
    """DBA 图形化 CRUD：按主键更新一行。"""
    if not _safe_ident(table):
        return JSONResponse({"error": "invalid table name"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    values = body.get("values") if isinstance(body, dict) else None
    pk = body.get("pk") if isinstance(body, dict) else None
    if not isinstance(values, dict) or not values:
        return JSONResponse({"error": "values is required"}, status_code=400)
    if not isinstance(pk, dict) or not pk:
        return JSONResponse({"error": "pk is required"}, status_code=400)

    def _db():

        with db_connect() as conn:
            cols = [dict(r) for r in _load_table_columns(conn, table)]
            col_map = {c["column_name"]: c for c in cols}
            pk_cols = [c["column_name"] for c in cols if int(c.get("is_pk") or 0) == 1]
            if not pk_cols:
                return JSONResponse(
                    {"error": "table has no primary key"}, status_code=400
                )
            if any(k not in pk for k in pk_cols):
                return JSONResponse(
                    {"error": f"pk must include: {', '.join(pk_cols)}"},
                    status_code=400,
                )

            writable = {
                k: v
                for k, v in values.items()
                if k in col_map
                and k not in pk_cols
                and str(col_map[k].get("is_generated") or "NEVER") == "NEVER"
            }
            if not writable:
                return JSONResponse({"error": "no updatable fields"}, status_code=400)

            table_sql = _q_ident(table)
            set_sql = ", ".join(f"{_q_ident(k)}=?" for k in writable.keys())
            where_sql = " AND ".join(f"{_q_ident(k)}=?" for k in pk_cols)
            params = list(writable[k] for k in writable.keys()) + [
                pk[k] for k in pk_cols
            ]

            try:
                row = conn.execute(
                    f"UPDATE {table_sql} SET {set_sql} WHERE {where_sql} RETURNING *",
                    tuple(params),
                ).fetchone()
                conn.commit()
            except Exception as e:
                return JSONResponse(
                    {
                        "error": "update failed",
                        "details": str(e),
                    },
                    status_code=400,
                )

        if not row:
            return JSONResponse({"error": "row not found"}, status_code=404)
        return {"status": "ok", "row": dict(row)}

    return await asyncio.to_thread(_db)


@router.post("/api/system/admin/db/table/{table}/delete")
async def api_system_admin_db_table_delete(request: Request, table: str):
    """DBA 图形化 CRUD：按主键删除一行。"""
    if not _safe_ident(table):
        return JSONResponse({"error": "invalid table name"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    pk = body.get("pk") if isinstance(body, dict) else None
    if not isinstance(pk, dict) or not pk:
        return JSONResponse({"error": "pk is required"}, status_code=400)

    def _db():

        with db_connect() as conn:
            cols = [dict(r) for r in _load_table_columns(conn, table)]
            pk_cols = [c["column_name"] for c in cols if int(c.get("is_pk") or 0) == 1]
            if not pk_cols:
                return JSONResponse(
                    {"error": "table has no primary key"}, status_code=400
                )
            if any(k not in pk for k in pk_cols):
                return JSONResponse(
                    {"error": f"pk must include: {', '.join(pk_cols)}"},
                    status_code=400,
                )

            table_sql = _q_ident(table)
            where_sql = " AND ".join(f"{_q_ident(k)}=?" for k in pk_cols)
            try:
                row = conn.execute(
                    f"DELETE FROM {table_sql} WHERE {where_sql} RETURNING *",
                    tuple(pk[k] for k in pk_cols),
                ).fetchone()
                conn.commit()
            except Exception as e:
                return JSONResponse(
                    {
                        "error": "delete failed",
                        "details": str(e),
                    },
                    status_code=400,
                )

        if not row:
            return JSONResponse({"error": "row not found"}, status_code=404)
        return {"status": "ok", "deleted": dict(row)}

    return await asyncio.to_thread(_db)


@router.get("/api/system/admin/db/roles")
async def api_system_admin_db_roles(request: Request):
    """DBA 权限控制：数据库角色与表授权概览。"""

    def _db():
        with db_connect() as conn:
            roles = [dict(r) for r in conn.execute("""
                SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin
                FROM pg_roles
                WHERE rolname NOT LIKE 'pg_%'
                ORDER BY rolname
                """).fetchall()]
            grants = [dict(r) for r in conn.execute("""
                SELECT grantee, table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE table_schema='public'
                ORDER BY grantee, table_name, privilege_type
                LIMIT 500
                """).fetchall()]
            cur_user = conn.execute("SELECT current_user AS u").fetchone()

        return {
            "status": "ok",
            "current_user": (dict(cur_user) if cur_user else {}).get("u"),
            "roles": roles,
            "grants": grants,
        }

    return await asyncio.to_thread(_db)


@router.post("/api/system/admin/db/explain")
async def api_system_admin_db_explain(request: Request):
    """DBA 性能优化：EXPLAIN 查询计划（只允许 SELECT）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    query, err = _sanitize_readonly_query((body or {}).get("query") or "")
    if err:
        return JSONResponse(
            {"error": err},
            status_code=400 if err == "query is required" else 403,
        )

    def _db():

        with db_connect() as conn:
            row = conn.execute(f"EXPLAIN (FORMAT JSON) {query}").fetchone()
        plan = None
        if row:
            if isinstance(row, dict):
                values = list(row.values())
                plan = values[0] if values else None
            elif hasattr(row, "keys"):
                keys = list(row.keys())
                plan = row[keys[0]] if keys else None
            elif isinstance(row, (list, tuple)):
                plan = row[0] if row else None
            else:
                plan = row
        return {"status": "ok", "plan": plan}

    return await asyncio.to_thread(_db)


@router.post("/api/system/admin/db/maintenance")
async def api_system_admin_db_maintenance(request: Request):
    """DBA 结构维护/性能优化：VACUUM/ANALYZE/REINDEX。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    action = str((body or {}).get("action") or "").strip().lower()
    table = str((body or {}).get("table") or "").strip()
    if action not in {"vacuum", "vacuum_analyze", "analyze", "reindex"}:
        return JSONResponse(
            {"error": "action must be vacuum/vacuum_analyze/analyze/reindex"},
            status_code=400,
        )
    if table and not _safe_ident(table):
        return JSONResponse({"error": "invalid table name"}, status_code=400)

    if action == "reindex":
        sql = f"REINDEX TABLE {_q_ident(table)}" if table else "REINDEX SCHEMA public"
    elif action == "analyze":
        sql = f"ANALYZE {_q_ident(table)}" if table else "ANALYZE"
    elif action in {"vacuum", "vacuum_analyze"}:
        base = "VACUUM" if action == "vacuum" else "VACUUM ANALYZE"
        sql = f"{base} {_q_ident(table)}" if table else base
    else:
        return JSONResponse({"error": "unsupported action"}, status_code=400)

    def _db():

        with db_connect() as conn:
            must_autocommit = action in {"vacuum", "vacuum_analyze", "reindex"}
            raw_conn = getattr(conn, "raw", None)
            old_autocommit = getattr(raw_conn, "autocommit", None)
            if must_autocommit and old_autocommit is not None:
                raw_conn.autocommit = True
            try:
                conn.execute(sql)
                if not must_autocommit:
                    conn.commit()
            finally:
                if must_autocommit and old_autocommit is not None:
                    raw_conn.autocommit = old_autocommit
        return {"status": "ok", "action": action, "table": table or None}

    return await asyncio.to_thread(_db)


_DB_BACKUP_DIR = Path(CONFIG_PATH).parent.parent / "data" / "db_backups"


@router.get("/api/system/admin/db/backup/list")
async def api_system_admin_db_backup_list(request: Request):
    """DBA 备份恢复：列出 JSON 逻辑备份。"""
    _DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted(_DB_BACKUP_DIR.glob("db_backup_*.json"), reverse=True):
        st = p.stat()
        files.append(
            {
                "filename": p.name,
                "size": st.st_size,
                "size_human": _fmt_bytes(st.st_size),
                "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            }
        )
    return {"status": "ok", "files": files}


@router.post("/api/system/admin/db/backup/create")
async def api_system_admin_db_backup_create(request: Request):
    """DBA 备份恢复：创建逻辑备份（public schema 全表 JSON）。"""
    _DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    file_path = _DB_BACKUP_DIR / f"db_backup_{ts}.json"

    payload: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
    }

    def _db():

        with db_connect() as conn:
            table_rows = conn.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE'
            ORDER BY table_name
            """).fetchall()
            tables = [dict(r).get("table_name") for r in table_rows]
            for t in tables:
                if not _safe_ident(str(t)):
                    continue
                qn = _q_ident(str(t))
                rows = conn.execute(f"SELECT * FROM {qn}").fetchall()
                payload["tables"][str(t)] = {
                    "rows": [dict(r) for r in rows],
                    "count": len(rows),
                }

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str)

        return {
            "status": "ok",
            "filename": file_path.name,
            "path": str(file_path),
            "tables": len(payload["tables"]),
        }

    return await asyncio.to_thread(_db)


@router.post("/api/system/admin/db/backup/restore")
async def api_system_admin_db_backup_restore(request: Request):
    """DBA 备份恢复：从 JSON 逻辑备份恢复（危险操作，需确认）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    filename = str((body or {}).get("filename") or "").strip()
    confirm = str((body or {}).get("confirm") or "").strip().upper()
    if not filename:
        return JSONResponse({"error": "filename is required"}, status_code=400)
    if confirm != "RESTORE":
        return JSONResponse({"error": "confirm must be RESTORE"}, status_code=400)
    if not _safe_ident(filename.replace(".", "_").replace("-", "_")):
        return JSONResponse({"error": "invalid filename"}, status_code=400)

    _DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    file_path = _DB_BACKUP_DIR / filename
    if not file_path.exists():
        return JSONResponse({"error": "backup file not found"}, status_code=404)

    with open(file_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    tables = payload.get("tables") if isinstance(payload, dict) else None
    if not isinstance(tables, dict) or not tables:
        return JSONResponse({"error": "invalid backup content"}, status_code=400)

    restored_tables = []

    def _db():
        with db_connect() as conn:
            for table, info in tables.items():
                if not _safe_ident(table):
                    continue
                rows = info.get("rows") if isinstance(info, dict) else None
                if not isinstance(rows, list):
                    continue
                qn = _q_ident(table)
                cols = [dict(r) for r in _load_table_columns(conn, table)]
                col_names = [c["column_name"] for c in cols]
                conn.execute(f"TRUNCATE TABLE {qn} RESTART IDENTITY CASCADE")
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    keys = [k for k in row.keys() if k in col_names]
                    if not keys:
                        continue
                    key_sql = ", ".join(_q_ident(k) for k in keys)
                    placeholders = ", ".join(["?"] * len(keys))
                    conn.execute(
                        f"INSERT INTO {qn} ({key_sql}) VALUES ({placeholders})",
                        tuple(row.get(k) for k in keys),
                    )
                restored_tables.append({"table": table, "rows": len(rows)})
            conn.commit()

        return {
            "status": "ok",
            "filename": filename,
            "restored_tables": restored_tables,
        }

    return await asyncio.to_thread(_db)


@router.post("/api/system/users/create")
async def api_system_users_create(request: Request):
    """管理员创建用户（图形化 CRUD）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    wallet = str((body or {}).get("wallet_address") or "").strip().lower()
    role = str((body or {}).get("role") or "manual_user").strip().lower()
    display_name = str((body or {}).get("display_name") or "").strip()
    # T287 Wave1: 禁止管理员手动写入 polymarket_wallet。Safe 必须通过 Privy + Builder
    # Relayer 部署后由系统记录。已忽略请求中的 polymarket_wallet 字段。
    polymarket_wallet = ""
    auth_provider = str((body or {}).get("auth_provider") or "manual").strip()
    source_ip = str((body or {}).get("source_ip") or "admin").strip()

    if not wallet:
        return JSONResponse({"error": "missing wallet_address"}, status_code=400)
    if role not in {"manual_user", "admin", "super_admin"}:
        return JSONResponse({"error": "invalid role"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()

    def _db():
        with db_connect() as conn:
            exists = conn.execute(
                "SELECT wallet_address FROM users WHERE wallet_address=?",
                (wallet,),
            ).fetchone()
            if exists:
                return JSONResponse({"error": "user already exists"}, status_code=409)

            conn.execute(
                """
            INSERT INTO users(
                wallet_address, role, display_name, auth_provider,
                polymarket_wallet, source_ip, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    wallet,
                    role,
                    display_name,
                    auth_provider,
                    polymarket_wallet or None,
                    source_ip,
                    now,
                    now,
                ),
            )
            conn.commit()

        return {"status": "ok", "wallet_address": wallet}

    return await asyncio.to_thread(_db)


@router.post("/api/system/users/save-profile")
async def api_system_users_save_profile(request: Request):
    """管理员用户管理原子保存：基础信息 + 订阅 + 运行控制（单事务）。"""
    _ensure_user_runtime_controls_table()
    _ensure_system_subscription_table()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    wallet = str((body or {}).get("wallet_address") or "").strip().lower()
    if not wallet:
        return JSONResponse({"error": "missing wallet_address"}, status_code=400)

    mode = str((body or {}).get("mode") or "update").strip().lower()
    is_create = mode == "create"

    role = str((body or {}).get("role") or "manual_user").strip().lower()
    display_name = str((body or {}).get("display_name") or "").strip()
    # T287 Wave1: 禁止 save-profile 写入 polymarket_wallet（即使管理员）。
    # 字段保留只读展示，写入路径必须走 Privy + 系统 Safe 部署。
    polymarket_wallet = ""
    auth_provider = str((body or {}).get("auth_provider") or "manual").strip()
    source_ip = str((body or {}).get("source_ip") or "admin").strip()

    if role not in {"manual_user", "admin", "super_admin"}:
        return JSONResponse({"error": "invalid role"}, status_code=400)

    sub = body.get("subscription") if isinstance(body, dict) else {}
    if not isinstance(sub, dict):
        sub = {}
    runtime = body.get("runtime") if isinstance(body, dict) else {}
    if not isinstance(runtime, dict):
        runtime = {}

    plan = str(sub.get("plan") or "free").strip().lower()
    subscription_status = str(sub.get("status") or "active").strip().lower()
    seats = int(sub.get("seats") or 1)
    expires_at = sub.get("expires_at")
    notes = str(sub.get("notes") or "")[:300]

    if plan not in {"free", "pro", "enterprise"}:
        return JSONResponse({"error": "invalid plan"}, status_code=400)
    if subscription_status not in {"active", "paused", "cancelled"}:
        return JSONResponse({"error": "invalid subscription status"}, status_code=400)
    if seats < 1:
        return JSONResponse({"error": "seats must >= 1"}, status_code=400)

    auto_trading_enabled = bool(runtime.get("auto_trading_enabled", True))
    freeze_auto = bool(runtime.get("freeze_auto", False))

    now = datetime.now(timezone.utc).isoformat()

    try:
        # T590 wave 4 — wrap save-profile transaction in to_thread; use sentinel for early-return states.
        def _db_save():
            with db_connect() as conn:
                existing = conn.execute(
                    "SELECT wallet_address FROM users WHERE wallet_address=?",
                    (wallet,),
                ).fetchone()

                if is_create:
                    if existing:
                        return "exists"
                    conn.execute(
                        """
                    INSERT INTO users(
                        wallet_address, role, display_name, auth_provider,
                        polymarket_wallet, source_ip, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            wallet,
                            role,
                            display_name,
                            auth_provider,
                            polymarket_wallet or None,
                            source_ip,
                            now,
                            now,
                        ),
                    )
                else:
                    if not existing:
                        return "not_found"
                    # T287 Wave1: 不再在 update 路径写入 polymarket_wallet，避免管理员误操作
                    # 清除/篡改用户已绑定的 Safe 地址。
                    conn.execute(
                        """
                    UPDATE users
                    SET role=?, display_name=?, updated_at=?
                    WHERE wallet_address=?
                    """,
                        (
                            role,
                            display_name,
                            now,
                            wallet,
                        ),
                    )

                conn.execute(
                    """
                INSERT INTO user_subscriptions(wallet_address, plan, status, seats, expires_at, notes, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address) DO UPDATE SET
                    plan=excluded.plan,
                    status=excluded.status,
                    seats=excluded.seats,
                    expires_at=excluded.expires_at,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                    (
                        wallet,
                        plan,
                        subscription_status,
                        seats,
                        expires_at,
                        notes,
                        now,
                        now,
                    ),
                )

                conn.execute(
                    """
                INSERT INTO user_runtime_controls(wallet_address, freeze_auto, auto_trading_enabled, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(wallet_address) DO UPDATE SET
                    freeze_auto=excluded.freeze_auto,
                    auto_trading_enabled=excluded.auto_trading_enabled,
                    updated_at=excluded.updated_at
                """,
                    (
                        wallet,
                        1 if freeze_auto else 0,
                        1 if auto_trading_enabled else 0,
                        now,
                    ),
                )

                conn.commit()
                return "ok"

        result = await asyncio.to_thread(_db_save)
        if result == "exists":
            return JSONResponse({"error": "user already exists"}, status_code=409)
        if result == "not_found":
            return JSONResponse({"error": "user not found"}, status_code=404)
    except Exception as e:
        logger.exception(f"[SystemUsersSaveProfile] 失败: {e}")
        return JSONResponse(
            {
                "error": "save profile failed",
                "details": str(e),
            },
            status_code=400,
        )

    return {
        "status": "ok",
        "wallet_address": wallet,
        "mode": "create" if is_create else "update",
        "role": role,
        "plan": plan,
        "subscription_status": subscription_status,
        "seats": seats,
        "freeze_auto": freeze_auto,
        "auto_trading_enabled": auto_trading_enabled,
        "updated_at": now,
    }


@router.post("/api/system/users/update")
async def api_system_users_update(request: Request):
    """管理员更新用户资料（角色/显示名/PM 钱包）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    wallet = str((body or {}).get("wallet_address") or "").strip().lower()
    if not wallet:
        return JSONResponse({"error": "missing wallet_address"}, status_code=400)

    role = body.get("role")
    display_name = body.get("display_name")
    # T287 Wave1: 忽略 polymarket_wallet 字段，禁止管理员任意写入用户的 PM 钱包。
    polymarket_wallet = None

    updates = []
    params: list[Any] = []
    if role is not None:
        role = str(role).strip().lower()
        if role not in {"manual_user", "admin", "super_admin"}:
            return JSONResponse({"error": "invalid role"}, status_code=400)
        updates.append("role=?")
        params.append(role)
    if display_name is not None:
        updates.append("display_name=?")
        params.append(str(display_name).strip())
    if polymarket_wallet is not None:
        pmw = str(polymarket_wallet).strip().lower()
        updates.append("polymarket_wallet=?")
        params.append(pmw or None)

    if not updates:
        return JSONResponse({"error": "no update fields"}, status_code=400)

    updates.append("updated_at=?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(wallet)

    def _db():

        with db_connect() as conn:
            cur = conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE wallet_address=?",
                tuple(params),
            )
            conn.commit()
            if int(cur.rowcount or 0) <= 0:
                return JSONResponse({"error": "user not found"}, status_code=404)

        return {"status": "ok", "wallet_address": wallet}

    return await asyncio.to_thread(_db)


# ──────────────────────────────────────────────────────────────────────
# T491-Enterprise: 积分系统全栈管理 CRUD（仅 super_admin/admin 可访问，
# 通过 web_dashboard.py 的 /api/system/* 中间件门控）
# ──────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/api/system/points/rules")
async def api_system_points_rules_list(request: Request):
    """列出所有积分规则（含已禁用）。"""

    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT id, action_type, points, description, max_daily, active, created_at, updated_at "
                "FROM point_rules ORDER BY active DESC, action_type ASC"
            ).fetchall()
        return {"status": "ok", "rules": [dict(r) for r in rows]}

    return await asyncio.to_thread(_db)


@router.post("/api/system/points/rules")
async def api_system_points_rules_create(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    action_type = str(body.get("action_type") or "").strip().lower()[:64]
    if not action_type or not re.match(r"^[a-z0-9_\-]+$", action_type):
        return JSONResponse(
            {"error": "action_type required (lowercase alnum/_/-)"}, status_code=400
        )
    try:
        points = int(body.get("points"))
    except Exception:
        return JSONResponse({"error": "points must be integer"}, status_code=400)
    if points < -1_000_000 or points > 1_000_000:
        return JSONResponse({"error": "points out of range"}, status_code=400)
    description = str(body.get("description") or "").strip()[:500]
    try:
        max_daily = int(body.get("max_daily") or 0)
    except Exception:
        max_daily = 0
    if max_daily < 0:
        max_daily = 0
    active = 1 if bool(body.get("active", True)) else 0
    now = _now_iso()

    def _db():
        with db_connect() as conn:
            existing = conn.execute(
                "SELECT id FROM point_rules WHERE action_type=?", (action_type,)
            ).fetchone()
            if existing:
                return JSONResponse(
                    {"error": "action_type already exists"}, status_code=409
                )
            ins = conn.execute(
                "INSERT INTO point_rules(action_type, points, description, max_daily, active, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?) RETURNING id",
                (action_type, points, description, max_daily, active, now, now),
            ).fetchone()
            conn.commit()
        return {"status": "ok", "id": int(ins["id"]) if ins else None}

    return await asyncio.to_thread(_db)


@router.put("/api/system/points/rules/{rule_id}")
async def api_system_points_rules_update(rule_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    sets: list = []
    params: list = []
    if "points" in body:
        try:
            v = int(body["points"])
            if v < -1_000_000 or v > 1_000_000:
                raise ValueError("points out of range")
            sets.append("points=?")
            params.append(v)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    if "description" in body:
        sets.append("description=?")
        params.append(str(body["description"] or "")[:500])
    if "max_daily" in body:
        try:
            v = max(0, int(body["max_daily"] or 0))
            sets.append("max_daily=?")
            params.append(v)
        except Exception:
            return JSONResponse({"error": "max_daily must be integer"}, status_code=400)
    if "active" in body:
        sets.append("active=?")
        params.append(1 if bool(body["active"]) else 0)
    if not sets:
        return JSONResponse({"error": "no update fields"}, status_code=400)
    sets.append("updated_at=?")
    params.append(_now_iso())
    params.append(int(rule_id))

    def _db():
        with db_connect() as conn:
            cur = conn.execute(
                f"UPDATE point_rules SET {', '.join(sets)} WHERE id=?",
                tuple(params),
            )
            conn.commit()
            if int(cur.rowcount or 0) <= 0:
                return JSONResponse({"error": "rule not found"}, status_code=404)
        return {"status": "ok", "id": int(rule_id)}

    return await asyncio.to_thread(_db)


@router.delete("/api/system/points/rules/{rule_id}")
async def api_system_points_rules_delete(rule_id: int, request: Request):
    """软删除：active=0；保留历史审计。"""
    now = _now_iso()

    def _db():
        with db_connect() as conn:
            cur = conn.execute(
                "UPDATE point_rules SET active=0, updated_at=? WHERE id=?",
                (now, int(rule_id)),
            )
            conn.commit()
            if int(cur.rowcount or 0) <= 0:
                return JSONResponse({"error": "rule not found"}, status_code=404)
        return {"status": "ok", "id": int(rule_id), "soft_deleted": True}

    return await asyncio.to_thread(_db)


@router.get("/api/system/points/transactions")
async def api_system_points_transactions(request: Request):
    """积分流水审计：可按钱包过滤、分页。"""
    qp = request.query_params
    wallet = str(qp.get("wallet") or "").strip().lower()
    src = str(qp.get("source") or "").strip()
    page = max(1, int(str(qp.get("page") or "1") or "1"))
    per_page = max(1, min(200, int(str(qp.get("per_page") or "50") or "50")))
    offset = (page - 1) * per_page

    where: list = []
    params: list = []
    if wallet:
        where.append("wallet_address=?")
        params.append(wallet)
    if src:
        where.append("source=?")
        params.append(src)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    def _db():

        with db_connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM point_transactions{where_sql}",
                tuple(params),
            ).fetchone()
            rows = conn.execute(
                f"SELECT id, wallet_address, type, amount, source, ref_id, description, created_at "
                f"FROM point_transactions{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
                tuple(params) + (per_page, offset),
            ).fetchall()
        total = int((total_row or {}).get("c") or 0) if total_row else 0
        return {
            "status": "ok",
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "transactions": [dict(r) for r in rows],
        }

    return await asyncio.to_thread(_db)


@router.get("/api/system/points/users")
async def api_system_points_users(request: Request):
    """积分用户分页（含搜索）。"""
    qp = request.query_params
    q = str(qp.get("q") or "").strip().lower()
    page = max(1, int(str(qp.get("page") or "1") or "1"))
    per_page = max(1, min(200, int(str(qp.get("per_page") or "50") or "50")))
    offset = (page - 1) * per_page
    where: list = []
    params: list = []
    if q:
        where.append("LOWER(wallet_address) LIKE ?")
        params.append(f"%{q}%")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    def _db():
        with db_connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM user_points{where_sql}", tuple(params)
            ).fetchone()
            rows = conn.execute(
                f"SELECT wallet_address, balance, total_earned, total_spent, updated_at "
                f"FROM user_points{where_sql} ORDER BY balance DESC, updated_at DESC LIMIT ? OFFSET ?",
                tuple(params) + (per_page, offset),
            ).fetchall()
        total = int((total_row or {}).get("c") or 0) if total_row else 0
        return {
            "status": "ok",
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "users": [dict(r) for r in rows],
        }

    return await asyncio.to_thread(_db)


# ── 折扣层级 CRUD ────────────────────────────────────────────


@router.get("/api/system/fee/discount-tiers")
async def api_system_fee_tiers_list(request: Request):
    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT id, min_points, discount_pct, description, active "
                "FROM fee_discount_tiers ORDER BY min_points ASC"
            ).fetchall()
        return {"status": "ok", "tiers": [dict(r) for r in rows]}

    return await asyncio.to_thread(_db)


@router.post("/api/system/fee/discount-tiers")
async def api_system_fee_tiers_create(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        min_points = int(body.get("min_points"))
        discount_pct = float(body.get("discount_pct"))
    except Exception:
        return JSONResponse(
            {"error": "min_points int and discount_pct number required"},
            status_code=400,
        )
    if min_points < 0 or discount_pct < 0 or discount_pct > 100:
        return JSONResponse(
            {"error": "min_points>=0 and 0<=discount_pct<=100"}, status_code=400
        )
    description = str(body.get("description") or "").strip()[:500]
    active = 1 if bool(body.get("active", True)) else 0

    def _db():
        with db_connect() as conn:
            ins = conn.execute(
                "INSERT INTO fee_discount_tiers(min_points, discount_pct, description, active) "
                "VALUES(?,?,?,?) RETURNING id",
                (min_points, discount_pct, description, active),
            ).fetchone()
            conn.commit()
        return {"status": "ok", "id": int(ins["id"]) if ins else None}

    return await asyncio.to_thread(_db)


@router.put("/api/system/fee/discount-tiers/{tier_id}")
async def api_system_fee_tiers_update(tier_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    sets: list = []
    params: list = []
    if "min_points" in body:
        try:
            v = int(body["min_points"])
            if v < 0:
                raise ValueError("min_points>=0")
            sets.append("min_points=?")
            params.append(v)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    if "discount_pct" in body:
        try:
            v = float(body["discount_pct"])
            if v < 0 or v > 100:
                raise ValueError("0<=discount_pct<=100")
            sets.append("discount_pct=?")
            params.append(v)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    if "description" in body:
        sets.append("description=?")
        params.append(str(body["description"] or "")[:500])
    if "active" in body:
        sets.append("active=?")
        params.append(1 if bool(body["active"]) else 0)
    if not sets:
        return JSONResponse({"error": "no update fields"}, status_code=400)
    params.append(int(tier_id))

    def _db():
        with db_connect() as conn:
            cur = conn.execute(
                f"UPDATE fee_discount_tiers SET {', '.join(sets)} WHERE id=?",
                tuple(params),
            )
            conn.commit()
            if int(cur.rowcount or 0) <= 0:
                return JSONResponse({"error": "tier not found"}, status_code=404)
        return {"status": "ok", "id": int(tier_id)}

    return await asyncio.to_thread(_db)


@router.delete("/api/system/fee/discount-tiers/{tier_id}")
async def api_system_fee_tiers_delete(tier_id: int, request: Request):
    """软删除（active=0）。"""

    def _db():
        with db_connect() as conn:
            cur = conn.execute(
                "UPDATE fee_discount_tiers SET active=0 WHERE id=?", (int(tier_id),)
            )
            conn.commit()
            if int(cur.rowcount or 0) <= 0:
                return JSONResponse({"error": "tier not found"}, status_code=404)
        return {"status": "ok", "id": int(tier_id), "soft_deleted": True}

    return await asyncio.to_thread(_db)


# ──────────────────────────────────────────────────────────────────────
# T486: 公开数据接口的 HTTP 缓存策略 — 通过装饰头部的 helper 提供
# (实际由 discover_routes 在 hot 路径上调用 add_public_cache 头)
# ──────────────────────────────────────────────────────────────────────


def add_public_cache_headers(
    response: JSONResponse, ttl_seconds: int = 30
) -> JSONResponse:
    """把响应标记为可被 CDN/浏览器短期缓存。供 GET 公开接口复用。"""
    try:
        response.headers["Cache-Control"] = (
            f"public, max-age={int(ttl_seconds)}, s-maxage={int(ttl_seconds)}, stale-while-revalidate=60"
        )
        response.headers["Vary"] = "Accept-Encoding, Accept-Language"
    except Exception:
        pass
    return response
