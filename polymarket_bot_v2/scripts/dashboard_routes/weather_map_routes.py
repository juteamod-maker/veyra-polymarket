"""
dashboard_routes/weather_map_routes.py — T244 专业天气世界地图看板 API
提供 /WeatherMap 所需的天气城市、ECMWF ENS 预报、METAR/TAF、峰值窗口等端点。
"""

import asyncio
import json
import logging
import re
import time
import unicodedata
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import DB_PATH, ROOT, db_connect, get, get_request_identity
from scripts.dashboard_routes.map_routes import (
    _extract_city_from_question,
    _fetch_and_decode_metar,
    _fetch_and_decode_taf,
    _fetch_short_term_trend,
    _get_current_temp,
    _get_weather_cities,
)

router = APIRouter()
logger = logging.getLogger("web_dashboard")

# ── API i18n 翻译映射 ─────────────────────────────────────────────────────
WEATHER_MAP_TRANSLATIONS = {
    # 窗口标签
    "峰值窗口": "Peak Window",
    "谷值窗口": "Trough Window",
    # 判断摘要片段
    "谷值窗口存在云雨或结构压制": "Trough Window: rain/cloud suppression",
    "峰值窗口存在云雨或结构压制": "Peak Window: rain/cloud suppression",
    "存在云雨或结构压制": "rain/cloud suppression present",
    "高云量抑制升温": "high cloud coverage suppresses warming",
    "低能见度/雾气干扰": "low visibility/fog interference",
    "温度路径偏上行": "temperature path trending upward",
    "温度路径偏下行": "temperature path trending downward",
    "谷值窗口进行中，关注低温是否继续下探": "trough window active, monitoring continued temperature decline",
    "当前温度已达或超过预测峰值": "current temperature reached or exceeded forecast peak",
    "峰值窗口进行中，温度仍有上行空间": "peak window active, temperature still has upside",
    "谷值窗口尚未开始，关注夜间降温节奏": "trough window not started, monitor nocturnal cooling pace",
    "峰值窗口尚未开始，关注午后升温": "peak window not started, monitor afternoon warming",
    "谷值窗口已结束": "Trough Window ended",
    "峰值窗口已结束": "Peak Window ended",
    "已结束": "ended",
    "当前气象条件较为平稳，无明显压温或升温因素": "current weather stable, no obvious warming/suppression factors",
    # 置信度
    "高": "High",
    "中": "Medium",
    "低": "Low",
    # 标签片段
    "基准路径已触达": "baseline path reached",
    "高于基准": "above baseline",
    "低于基准": "below baseline",
    "谷值窗口进行中": "Trough Window active",
    "峰值窗口进行中": "Peak Window active",
    "谷值窗口已结束": "Trough Window ended",
    "峰值窗口已结束": "Peak Window ended",
    "等待谷值窗口": "awaiting Trough Window",
    "等待峰值窗口": "awaiting Peak Window",
    "进行中": "active",
    "等待": "awaiting",
    "模型分歧": "model divergence",
    # 信号强度
    "偏强(升温)": "strong warming",
    "偏弱(降温)": "weak cooling",
    "中性": "neutral",
    "压制-强": "suppress-strong",
    "压制-中": "suppress-medium",
    "压制-弱": "suppress-weak",
    "有利升温": "favorable warming",
    "支持升温": "supports warming",
    # 信号标题
    "日内节奏": "Daily Rhythm",
    "边界层结构": "Boundary Layer",
    "TAF云雨扰动": "TAF Rain/Cloud Disturbance",
    # 信号描述模板
    "实测比ECMWF预测高": "observed higher than ECMWF forecast by",
    "实测比ECMWF预测低": "observed lower than ECMWF forecast by",
    "实测与ECMWF预测偏差": "observed ECMWF forecast deviation",
    "路径贴近预测": "path close to forecast",
    "当前时刻无ECMWF对比数据": "no ECMWF comparison data at current hour",
    "实测数据暂缺": "observation data currently unavailable",
    "平均云量": "avg cloud coverage ",
    "高云量显著抑制地面升温": "high cloud coverage notably suppresses surface warming",
    "存在一定云层压温效应": "some cloud suppression effect present",
    "晴朗条件有利于地面辐射增温": "clear conditions favor ground radiation warming",
    "风速较大": "wind speed is high",
    "混合层搅拌降低峰值": "boundary layer mixing reduces peak",
    "中等风速": "moderate wind speed",
    "边界层数据暂缺": "boundary layer data unavailable",
    "TAF预报含雷暴信号，峰值窗口可能受对流天气显著压制": "TAF contains thunderstorm signal, peak window may be suppressed by convective weather",
    "TAF预报含降雨信号，云雨可能压制午后升温": "TAF contains rain signal, clouds may suppress afternoon warming",
    "TAF预报多云到阴天，部分抑制升温但无明显降雨": "TAF shows cloudy/overcast, partial warming suppression, no significant rain",
    "TAF预报晴到少云，有利于午后地面辐射增温": "TAF shows clear to few clouds, favorable for afternoon surface radiation warming",
    "TAF信息已获取，暂未检测到明显压温或升温信号": "TAF data retrieved, no obvious warming/suppression signals detected yet",
    "暂无TAF预报数据": "TAF forecast data not available",
    # short_term metric labels
    "气温趋势": "Temp Trend",
    "当前气温": "Current Temp",
    "露点均值": "Dew Point Avg",
    "风速": "Wind Speed",
    "云量": "Cloud Cover",
    "日照峰值": "Solar Peak",
    "日高低温": "Daily High/Low",
    # wind descriptions
    "静风": "Calm",
    "微风": "Light Breeze",
    "和风": "Gentle Breeze",
    "强风": "Strong Wind",
    "大风": "High Wind",
    "暴风": "Storm",
    # cloud descriptions
    "晴 ☀": "Clear ☀",
    "少云 🌤": "Few Clouds 🌤",
    "多云 ⛅": "Partly Cloudy ⛅",
    "阴 ☁": "Overcast ☁",
    # solar descriptions
    "强日照": "Strong Sunshine",
    "中等日照": "Moderate Sunshine",
    "弱日照": "Weak Sunshine",
    "无日照": "No Sunshine",
    # temp trend suffixes
    "降温 ↓": "cooling ↓",
    "升温 ↑": "warming ↑",
    "平稳 →": "stable →",
}


def _translate_value(value, target_lang: str = "en"):
    if target_lang not in ("en", "zh-CN") or target_lang == "zh-CN":
        return value
    if isinstance(value, str):
        if value in WEATHER_MAP_TRANSLATIONS:
            return WEATHER_MAP_TRANSLATIONS[value]
        for cn, en in sorted(WEATHER_MAP_TRANSLATIONS.items(), key=lambda x: -len(x[0])):
            if cn in value:
                value = value.replace(cn, en)
        return value
    return value


def _translate_dict(obj, target_lang: str = "en", depth: int = 0):
    """递归翻译字典/列表中的所有字符串值。"""
    if target_lang not in ("en", "zh-CN") or target_lang == "zh-CN" or depth > 10:
        return obj
    if isinstance(obj, dict):
        return {k: _translate_dict(v, target_lang, depth + 1) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_translate_dict(item, target_lang, depth + 1) for item in obj]
    elif isinstance(obj, str):
        return _translate_value(obj, target_lang)
    else:
        return obj


def _detect_response_lang(request) -> str:
    """Cookie veyra_lang > Accept-Language > zh-CN."""
    try:
        cookies = request.cookies or {}
        cookie_lang = (cookies.get("veyra_lang") or "").strip()
        if cookie_lang in ("en", "zh-CN"):
            return cookie_lang
        accept_lang = request.headers.get("accept-language", "").lower()
        if "en" in accept_lang:
            return "en"
        if "zh" in accept_lang:
            return "zh-CN"
    except Exception:
        pass
    return "zh-CN"  # 默认中文


# ── 缓存 ──────────────────────────────────────────────────────────────
_ECMWF_CACHE: dict = {}  # city_name → {data, ts}
_ECMWF_CACHE_TTL = 300  # 5 分钟
# T545 — per-wallet keyed cache so pin counts never bleed across users.
# Anonymous viewers share the empty key ("") and see zero positions.
_CITIES_CACHE: dict[str, dict] = {}
_CITIES_CACHE_TTL = 60  # 1 分钟
_CITIES_CACHE_SWR = 600  # T505 Wave 6D: serve stale up to 10min while refreshing
_CITIES_REFRESH_INFLIGHT: dict[str, bool] = {}
_CITY_DETAIL_CACHE: dict = {}
_CITY_DETAIL_CACHE_SWR = 1800  # T507 Wave 7A: serve stale city-detail up to 30min
_CITY_DETAIL_REFRESH_INFLIGHT: dict = {}
_CITY_DETAIL_LOCKS: dict = {}
_EVENT_LINK_CACHE: dict = {}


def _slugify_city_for_event(city_name: str) -> str:
    base = unicodedata.normalize("NFKD", str(city_name or ""))
    base = "".join(c for c in base if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")


def _build_city_event_slugs(city_name: str, local_now: datetime) -> tuple[str, str]:
    city_slug = _slugify_city_for_event(city_name)
    month = local_now.strftime("%B").lower()
    day = local_now.day
    year = local_now.year
    high_slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    low_slug = f"lowest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    return high_slug, low_slug


def _resolve_market_links(city_name: str, local_now: datetime) -> dict:
    cache_key = f"{str(city_name or '').lower()}::{local_now.strftime('%Y-%m-%d')}"
    now_ts = time.time()
    cached = _EVENT_LINK_CACHE.get(cache_key)
    if cached and (now_ts - cached["ts"]) < _CITIES_CACHE_TTL:
        return cached["data"]

    high_discover = "https://veyra.trade/discover?p=weather&t=highest-temperature"
    low_discover = "https://veyra.trade/discover?p=weather&t=lowest-temperature"
    high_slug, low_slug = _build_city_event_slugs(city_name, local_now)
    high_event_url = f"https://veyra.trade/event/{high_slug}"
    low_event_url = f"https://veyra.trade/event/{low_slug}"

    exists = set()
    try:
        with db_connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT event_slug FROM paper_bets
                WHERE event_slug IN (?, ?)
                """,
                (high_slug, low_slug),
            )
            rows = cur.fetchall()
            for row in rows:
                slug = row[0] if isinstance(row, tuple) else row["event_slug"]
                if slug:
                    exists.add(str(slug).strip().lower())
    except Exception as e:
        logger.debug(f"[WeatherMap] market link resolve fallback: {e}")

    data = {
        "highest_url": high_event_url if high_slug.lower() in exists else high_discover,
        "lowest_url": low_event_url if low_slug.lower() in exists else low_discover,
        "highest_event_url": high_event_url,
        "lowest_event_url": low_event_url,
        "highest_exists": high_slug.lower() in exists,
        "lowest_exists": low_slug.lower() in exists,
    }
    _EVENT_LINK_CACHE[cache_key] = {"data": data, "ts": now_ts}
    return data


def _position_to_city_name(weather_cities: list, title: str, slug: str, event_slug: str) -> str | None:
    city = _extract_city_from_question(str(title or ""), weather_cities)
    if city:
        return city
    slug_text = " ".join(
        part for part in [str(slug or ""), str(event_slug or "")] if part
    ).replace("-", " ")
    if slug_text:
        return _extract_city_from_question(slug_text, weather_cities)
    return None


def _fetch_latest_user_weather_positions(wallet_filter: str | None = None) -> list[dict]:
    """T545 — 返回最新快照下的天气类 PM 持仓。

    当 ``wallet_filter`` 提供时，仅返回该钱包的仓位。匿名（``None`` 或空
    串）调用者一律得到空列表，避免将全平台仓位累加到某个用户身上。

    T927 fix — 严格排除以下情形，避免把已兑现/已赎回/已解析的老投注当成
    "活跃持仓"误导用户：
      1) ``size`` 已为 0（或 NULL）→ 已全部 sell/redeem。
      2) ``end_date`` 已过期（市场已 resolve）→ 即使 size 仍 > 0 也不显示，
         因为这种情况下一定是底层持仓未刷新（用户多半已 redeem 但快照过期）。
      3) 快照本身年代久远（>72h）→ 强制忽略，避免长期未登录用户的陈旧快照
         继续显示在地图上。
    """
    norm = str(wallet_filter or "").strip().lower()
    if not norm:
        return []
    try:
        with db_connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT p.wallet_address, p.snapshot_ts, p.condition_id, p.title,
                       p.outcome, p.size, p.avg_price, p.cur_price, p.cash_pnl,
                       p.percent_pnl, p.slug, p.event_slug, p.end_date
                FROM user_pm_positions_snapshots p
                INNER JOIN (
                  SELECT wallet_address, MAX(snapshot_ts) AS max_ts
                  FROM user_pm_positions_snapshots
                  WHERE LOWER(wallet_address) = %s
                  GROUP BY wallet_address
                ) latest
                  ON latest.wallet_address = p.wallet_address
                 AND latest.max_ts = p.snapshot_ts
                WHERE COALESCE(p.size, 0) > 0
                  AND LOWER(p.wallet_address) = %s
                  AND (
                    p.title LIKE '%%temperature%%'
                    OR p.slug LIKE '%%temperature%%'
                    OR p.event_slug LIKE '%%temperature%%'
                  )
                """,
                (norm, norm),
            )
            rows = cur.fetchall()

        # T927 — Python-side post-filters (snapshot age + end_date) so we are
        # robust to mixed timestamp formats coming from Polymarket data-api.
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        snapshot_max_age = timedelta(hours=72)

        def _parse_iso(ts: str) -> datetime | None:
            if not ts:
                return None
            try:
                t = str(ts).strip()
                if t.endswith("Z"):
                    t = t[:-1] + "+00:00"
                dt = datetime.fromisoformat(t)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return None

        out = []
        for row in rows:
            if isinstance(row, tuple):
                (
                    wallet_address,
                    snapshot_ts,
                    condition_id,
                    title,
                    outcome,
                    size,
                    avg_price,
                    cur_price,
                    cash_pnl,
                    percent_pnl,
                    slug,
                    event_slug,
                    end_date,
                ) = row
            else:
                wallet_address = row.get("wallet_address")
                snapshot_ts = row.get("snapshot_ts")
                condition_id = row.get("condition_id")
                title = row.get("title")
                outcome = row.get("outcome")
                size = row.get("size")
                avg_price = row.get("avg_price")
                cur_price = row.get("cur_price")
                cash_pnl = row.get("cash_pnl")
                percent_pnl = row.get("percent_pnl")
                slug = row.get("slug")
                event_slug = row.get("event_slug")
                end_date = row.get("end_date")

            # T927 — drop snapshots older than 72h.
            snap_dt = _parse_iso(snapshot_ts)
            if snap_dt is not None and (now_utc - snap_dt) > snapshot_max_age:
                logger.debug(
                    "[WeatherMap T927] dropping stale snapshot wallet=%s cond=%s age=%s",
                    norm, str(condition_id)[:10], (now_utc - snap_dt),
                )
                continue
            # T927 — drop already-resolved markets even if size>0 in stale snapshot.
            end_dt = _parse_iso(end_date)
            if end_dt is not None and end_dt <= now_utc:
                logger.debug(
                    "[WeatherMap T927] dropping resolved market wallet=%s cond=%s end=%s",
                    norm, str(condition_id)[:10], end_dt.isoformat(),
                )
                continue

            out.append(
                {
                    "wallet_address": wallet_address,
                    "snapshot_ts": snapshot_ts,
                    "condition_id": condition_id,
                    "title": title,
                    "outcome": outcome,
                    "size": size,
                    "avg_price": avg_price,
                    "cur_price": cur_price,
                    "cash_pnl": cash_pnl,
                    "percent_pnl": percent_pnl,
                    "slug": slug,
                    "event_slug": event_slug,
                }
            )
        return out
    except Exception as e:
        logger.debug(f"[WeatherMap] snapshot positions unavailable: {e}")
        return []


# ── 天气城市列表 + 持仓摘要 ────────────────────────────────────────────


@router.get("/api/weather-map/cities")
async def weather_map_cities(request: Request):
    """返回所有天气城市列表 + 当前用户活跃持仓数量。

    T505 Wave 6D: SWR — fresh hit returns instantly; within stale window
    we still return the previous snapshot and trigger an async refresh so
    the next tick sees fresh data, keeping pin render <1s on every load.

    T545 — 图钉上的“活跃持仓”仅计当前登录用户的 PM 仓位，不再累加全系统机器人
    交易 (paper_bets) 与其他用户仓位。未登录访问者获得全 0 计数。
    """
    identity = get_request_identity(request)
    user_wallet = str((identity or {}).get("wallet_address") or "").strip().lower()
    cache_key = user_wallet  # "" for anonymous
    now = time.time()
    bucket = _CITIES_CACHE.get(cache_key)
    cached = bucket.get("data") if bucket else None
    age = now - (bucket.get("ts") if bucket else 0)
    if cached and age < _CITIES_CACHE_TTL:
        return cached
    if cached and age < _CITIES_CACHE_SWR:
        # Trigger background refresh, return stale immediately.
        if not _CITIES_REFRESH_INFLIGHT.get(cache_key):
            _CITIES_REFRESH_INFLIGHT[cache_key] = True
            try:
                import asyncio as _asyncio
                _asyncio.get_event_loop().run_in_executor(
                    None, _refresh_cities_cache_blocking, user_wallet
                )
            except Exception:
                _CITIES_REFRESH_INFLIGHT.pop(cache_key, None)
        return cached
    return await asyncio.to_thread(_refresh_cities_cache_blocking, user_wallet)


def _refresh_cities_cache_blocking(user_wallet: str | None = None):
    """Build the cities payload synchronously and store it in _CITIES_CACHE.

    T545 — 仅统计传入钱包的 PM 持仓快照；paper_bets (机器人交易) 不再
    计入图钉“活跃持仓”计数。
    """
    cache_key = str(user_wallet or "").strip().lower()
    now = time.time()

    weather_cities = _get_weather_cities()

    city_bets: dict[str, int] = {}
    if cache_key:
        try:
            for pos in _fetch_latest_user_weather_positions(cache_key):
                city = _position_to_city_name(
                    weather_cities,
                    str(pos.get("title") or ""),
                    str(pos.get("slug") or ""),
                    str(pos.get("event_slug") or ""),
                )
                if city:
                    city_bets[city.lower()] = city_bets.get(city.lower(), 0) + 1
        except Exception as e:
            logger.debug(f"[WeatherMap] user position aggregation failed: {e}")

    cities_out = []
    for c in weather_cities:
        name = c.get("name", "")
        enabled = c.get("enabled", True)
        cities_out.append(
            {
                "name": name,
                "lat": c.get("lat"),
                "lon": c.get("lon"),
                "timezone": c.get("timezone", "UTC"),
                "icao": c.get("icao", ""),
                "enabled": enabled,
                "active_bets": city_bets.get(name.lower(), 0),
            }
        )

    result = {"status": "ok", "count": len(cities_out), "cities": cities_out}
    _CITIES_CACHE[cache_key] = {"data": result, "ts": now}
    _CITIES_REFRESH_INFLIGHT.pop(cache_key, None)
    return result


# ── 城市详细数据（ECMWF ENS + METAR + TAF + 持仓 + 峰值窗口） ──────


def _fetch_ecmwf_hourly(lat: float, lon: float, timezone_iana: str) -> dict:
    """获取 Open-Meteo ECMWF IFS 0.25° + GFS 逐小时预报（当日）。"""
    import requests as _req

    try:
        _tz = zoneinfo.ZoneInfo(timezone_iana or "UTC")
    except Exception:
        _tz = zoneinfo.ZoneInfo("UTC")
    local_now = datetime.now(_tz)
    today = local_now.strftime("%Y-%m-%d")

    try:
        resp = _req.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "hourly": "temperature_2m,cloud_cover,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min",
                "models": "ecmwf_ifs025,gfs_seamless",
                "timezone": timezone_iana or "auto",
                "start_date": today,
                "end_date": today,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.debug(f"[WeatherMap] ECMWF IFS025 failed {resp.status_code}")
            return {}
        data = resp.json()
        hourly = data.get("hourly", {})
        daily = data.get("daily", {})

        # Open-Meteo 多模型时字段名带后缀：temperature_2m_ecmwf_ifs025 / temperature_2m_gfs_seamless
        times = hourly.get("time", [])
        ecmwf_temps = hourly.get("temperature_2m_ecmwf_ifs025") or hourly.get(
            "temperature_2m", []
        )
        gfs_temps = hourly.get("temperature_2m_gfs_seamless") or []
        clouds = hourly.get("cloud_cover_ecmwf_ifs025") or hourly.get("cloud_cover", [])
        winds = hourly.get("wind_speed_10m_ecmwf_ifs025") or hourly.get(
            "wind_speed_10m", []
        )

        # 所有24小时都返回（前端区分已过去vs未来）
        hourly_data = []
        gfs_hourly_data = []
        for i, t in enumerate(times):
            htime = t[-5:] if len(t) >= 5 else t  # "HH:MM"
            hourly_data.append(
                {
                    "time": htime,
                    "temp": (
                        round(float(ecmwf_temps[i]), 1)
                        if i < len(ecmwf_temps) and ecmwf_temps[i] is not None
                        else None
                    ),
                    "cloud": (
                        round(float(clouds[i]))
                        if i < len(clouds) and clouds[i] is not None
                        else None
                    ),
                    "wind": (
                        round(float(winds[i]), 1)
                        if i < len(winds) and winds[i] is not None
                        else None
                    ),
                }
            )
            gfs_hourly_data.append(
                {
                    "time": htime,
                    "temp": (
                        round(float(gfs_temps[i]), 1)
                        if i < len(gfs_temps) and gfs_temps[i] is not None
                        else None
                    ),
                }
            )

        d_max = (
            daily.get("temperature_2m_max_ecmwf_ifs025")
            or daily.get("temperature_2m_max", [None])
        )[0]
        d_min = (
            daily.get("temperature_2m_min_ecmwf_ifs025")
            or daily.get("temperature_2m_min", [None])
        )[0]
        gfs_d_max_list = daily.get("temperature_2m_max_gfs_seamless") or []
        gfs_d_max = gfs_d_max_list[0] if gfs_d_max_list else None

        return {
            "hourly": hourly_data,
            "gfs_hourly": gfs_hourly_data,
            "daily_max": round(float(d_max), 1) if d_max is not None else None,
            "daily_min": round(float(d_min), 1) if d_min is not None else None,
            "gfs_daily_max": (
                round(float(gfs_d_max), 1) if gfs_d_max is not None else None
            ),
            "model": "ECMWF IFS 0.25°",
            "date": today,
        }
    except Exception as e:
        logger.debug(f"[WeatherMap] ECMWF fetch error: {e}")
        return {}


def _get_city_bets(
    city_name: str,
    weather_cities: list,
    user_wallet: str | None = None,
) -> list[dict]:
    """返回当前用户在该城市的 PM 活跃持仓列表。

    T545 — “🎯 My Positions” 仅展示当前登录用户的仓位；paper_bets (机器人交易)
    与其他用户仓位不再出现于该接口。未登录访问者返回空列表。
    """
    city_norm = str(city_name or "").strip().lower()
    norm_wallet = str(user_wallet or "").strip().lower()
    if not norm_wallet:
        return []
    bets: list[dict] = []
    existing_keys: set[str] = set()
    for pos in _fetch_latest_user_weather_positions(norm_wallet):
        mapped_city = _position_to_city_name(
            weather_cities,
            str(pos.get("title") or ""),
            str(pos.get("slug") or ""),
            str(pos.get("event_slug") or ""),
        )
        if not mapped_city or mapped_city.lower() != city_norm:
            continue
        cond = str(pos.get("condition_id") or "")
        outcome = str(pos.get("outcome") or "").upper()
        k = f"{cond}:{outcome}"
        if k in existing_keys:
            continue
        existing_keys.add(k)
        wallet = str(pos.get("wallet_address") or "")
        pseudo_bet_id = f"pmpos:{wallet}:{cond}:{outcome}".strip(":")
        bets.append(
            {
                "bet_id": pseudo_bet_id,
                "question": pos.get("title") or "",
                "outcome": outcome,
                "entry_price": pos.get("avg_price") or 0,
                "strategy": "manual_user_pm_snapshot",
                "ts_entered": pos.get("snapshot_ts") or "",
                "condition_id": cond,
                "market_slug": pos.get("slug") or "",
                "event_slug": pos.get("event_slug") or "",
                "size_usdc": pos.get("size") or 0,
                "matched_shares": pos.get("size") or 0,
                "current_price": pos.get("cur_price"),
                "unrealized_pnl": pos.get("cash_pnl"),
                "unrealized_pnl_pct": pos.get("percent_pnl"),
                "source": "user_pm_positions_snapshots",
            }
        )

    bets.sort(key=lambda x: str(x.get("ts_entered") or ""), reverse=True)
    return bets


@router.get("/api/weather-map/city-bets/{city_name}")
async def weather_map_city_bets(city_name: str, request: Request):
    """仅返回当前用户在该城市的持仓（供弹窗「我的投注」tab快速刷新）。"""
    weather_cities = _get_weather_cities()
    city_exists = any(
        str(c.get("name", "")).lower() == str(city_name or "").strip().lower()
        for c in weather_cities
    )
    if not city_exists:
        return JSONResponse(
            status_code=404, content={"status": "error", "message": "city not found"}
        )

    identity = get_request_identity(request)
    user_wallet = str((identity or {}).get("wallet_address") or "").strip().lower()
    bets = await asyncio.to_thread(
        _get_city_bets, city_name, weather_cities, user_wallet
    )
    return {
        "status": "ok",
        "city": city_name,
        "bets": bets,
        "active_bets_count": len(bets),
    }


def _fetch_multi_model_max(lat: float, lon: float, timezone_iana: str) -> list[dict]:
    """获取多模型当日最高温预测对比。"""
    import requests as _req

    try:
        _tz = zoneinfo.ZoneInfo(timezone_iana or "UTC")
    except Exception:
        _tz = zoneinfo.ZoneInfo("UTC")
    today = datetime.now(_tz).strftime("%Y-%m-%d")

    models_list = [
        ("ecmwf_ifs025", "ECMWF IFS"),
        ("gfs_seamless", "GFS"),
        ("icon_seamless", "ICON"),
        ("gem_seamless", "GEM"),
        ("jma_seamless", "JMA"),
    ]
    models_param = ",".join(m[0] for m in models_list)

    try:
        resp = _req.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "daily": "temperature_2m_max",
                "models": models_param,
                "timezone": timezone_iana or "auto",
                "start_date": today,
                "end_date": today,
            },
            timeout=12,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        daily = data.get("daily", {})

        results = []
        for api_name, display_name in models_list:
            key = f"temperature_2m_max_{api_name}"
            vals = daily.get(key, [None])
            t_max = vals[0] if vals else None
            if t_max is not None:
                results.append(
                    {
                        "model": display_name,
                        "daily_max": round(float(t_max), 1),
                    }
                )
        return results
    except Exception as e:
        logger.debug(f"[WeatherMap] multi-model fetch error: {e}")
        return []


def _generate_judgment(
    current_temp_c: float | None,
    ecmwf_max: float | None,
    taf_text: str,
    cloud_avg: float | None,
    model_results: list[dict],
    window_label: str,
    window_is_active: bool,
    window_progress: int,
) -> dict:
    """根据气象数据生成专业判断。"""
    tags = []
    confidence = "中"

    # 距基准计算
    deviation = None
    if current_temp_c is not None and ecmwf_max is not None:
        deviation = round(current_temp_c - ecmwf_max, 1)

    # 压温风险分析（TAF关键词）
    taf_lower = (taf_text or "").lower()
    rain_risk = any(kw in taf_lower for kw in ["ra", "ts", "shra", "tsra", "雨", "雷"])
    cloud_risk = any(kw in taf_lower for kw in ["ovc", "bkn", "阴", "密云", "满云"])
    fog_risk = any(kw in taf_lower for kw in ["fg", "br", "雾"])

    # 云量分析
    cloud_suppression = False
    if cloud_avg is not None and cloud_avg > 70:
        cloud_suppression = True

    # 模型分歧度
    divergence = 0.0
    if len(model_results) >= 2:
        temps = [m["daily_max"] for m in model_results]
        divergence = round(max(temps) - min(temps), 1)

    # 置信度判断
    if divergence <= 1.5 and not rain_risk:
        confidence = "高"
    elif divergence > 3.0 or (rain_risk and cloud_risk):
        confidence = "低"

    # 生成判断总结
    parts = []
    if rain_risk or cloud_risk:
        parts.append(f"{window_label}存在云雨或结构压制")
    if cloud_suppression:
        parts.append("高云量抑制升温")
    if fog_risk:
        parts.append("低能见度/雾气干扰")

    if window_is_active:
        if window_label.startswith("谷值"):
            parts.append("谷值窗口进行中，关注低温是否继续下探")
        elif deviation is not None and deviation >= 0:
            parts.append("当前温度已达或超过预测峰值")
        else:
            parts.append("峰值窗口进行中，温度仍有上行空间")
    elif window_progress == 0:
        if window_label.startswith("谷值"):
            parts.append("谷值窗口尚未开始，关注夜间降温节奏")
        else:
            parts.append("峰值窗口尚未开始，关注午后升温")
    else:
        parts.append(f"{window_label}已结束")

    if not parts:
        parts.append("当前气象条件较为平稳，无明显压温或升温因素")

    summary = "，".join(parts) + "。"

    # 标签
    if deviation is not None:
        if abs(deviation) < 0.5:
            tags.append("基准路径已触达")
        elif deviation > 0:
            tags.append(f"高于基准 {deviation:+.1f}°C")
        else:
            tags.append(f"低于基准 {deviation:+.1f}°C")

    if window_is_active:
        tags.append(f"{window_label}进行中 {window_progress}%")
    elif window_progress >= 100:
        tags.append(f"{window_label}已结束")
    else:
        tags.append(f"等待{window_label}")

    if divergence > 0:
        tags.append(f"模型分歧 {divergence:.1f}°C")

    return {
        "summary": summary,
        "confidence": confidence,
        "deviation": deviation,
        "tags": tags,
        "rain_risk": rain_risk,
        "cloud_suppression": cloud_suppression,
        "divergence": divergence,
    }


def _derive_signal_contributions(
    current_temp_c: float | None,
    ecmwf_hourly: list[dict],
    local_hour: int,
    taf_text: str,
    cloud_avg: float | None,
    wind_avg: float | None,
) -> list[dict]:
    """生成信号贡献分析（3列）。"""
    signals = []

    # 1. 日内节奏
    rhythm_strength = "中性"
    rhythm_desc = ""
    if current_temp_c is not None and ecmwf_hourly:
        # 找到当前小时对应的ECMWF预测
        ecmwf_at_hour = None
        for h in ecmwf_hourly:
            try:
                hh = int(h["time"].split(":")[0])
                if hh == local_hour:
                    ecmwf_at_hour = h.get("temp")
                    break
            except (ValueError, KeyError):
                continue
        if ecmwf_at_hour is not None:
            diff = round(current_temp_c - ecmwf_at_hour, 1)
            if diff > 1.0:
                rhythm_strength = "偏强(升温)"
                rhythm_desc = f"实测比ECMWF预测高 {diff:+.1f}°C，温度路径偏上行"
            elif diff < -1.0:
                rhythm_strength = "偏弱(降温)"
                rhythm_desc = f"实测比ECMWF预测低 {diff:+.1f}°C，温度路径偏下行"
            else:
                rhythm_strength = "中性"
                rhythm_desc = f"实测与ECMWF预测偏差 {diff:+.1f}°C，路径贴近预测"
        else:
            rhythm_desc = "当前时刻无ECMWF对比数据"
    else:
        rhythm_desc = "实测数据暂缺"

    signals.append(
        {
            "title": "日内节奏",
            "icon": "📈",
            "strength": rhythm_strength,
            "description": rhythm_desc,
        }
    )

    # 2. 边界层结构
    boundary_strength = "中性"
    boundary_desc = ""
    if cloud_avg is not None:
        if cloud_avg > 80:
            boundary_strength = "压制-强"
            boundary_desc = f"平均云量{cloud_avg:.0f}%，高云量显著抑制地面升温"
        elif cloud_avg > 50:
            boundary_strength = "压制-弱"
            boundary_desc = f"平均云量{cloud_avg:.0f}%，存在一定云层压温效应"
        else:
            boundary_strength = "有利升温"
            boundary_desc = f"平均云量{cloud_avg:.0f}%，晴朗条件有利于地面辐射增温"
    if wind_avg is not None:
        if wind_avg > 20:
            boundary_desc += f"。风速较大({wind_avg:.0f}km/h)，混合层搅拌降低峰值"
        elif wind_avg > 10:
            boundary_desc += f"。中等风速({wind_avg:.0f}km/h)"
    if not boundary_desc:
        boundary_desc = "边界层数据暂缺"

    signals.append(
        {
            "title": "边界层结构",
            "icon": "🌥️",
            "strength": boundary_strength,
            "description": boundary_desc,
        }
    )

    # 3. TAF 云雨扰动
    taf_strength = "中性"
    taf_desc = ""
    taf_lower = (taf_text or "").lower()
    if any(kw in taf_lower for kw in ["ts", "tsra", "雷暴", "雷"]):
        taf_strength = "压制-强"
        taf_desc = "TAF预报含雷暴信号，峰值窗口可能受对流天气显著压制"
    elif any(kw in taf_lower for kw in ["ra", "shra", "雨", "降雨"]):
        taf_strength = "压制-中"
        taf_desc = "TAF预报含降雨信号，云雨可能压制午后升温"
    elif any(kw in taf_lower for kw in ["ovc", "bkn", "阴", "密云"]):
        taf_strength = "压制-弱"
        taf_desc = "TAF预报多云到阴天，部分抑制升温但无明显降雨"
    elif any(kw in taf_lower for kw in ["few", "sct", "少云", "晴"]):
        taf_strength = "支持升温"
        taf_desc = "TAF预报晴到少云，有利于午后地面辐射增温"
    else:
        if taf_text:
            taf_desc = "TAF信息已获取，暂未检测到明显压温或升温信号"
        else:
            taf_desc = "暂无TAF预报数据"

    signals.append(
        {
            "title": "TAF云雨扰动",
            "icon": "🌧️",
            "strength": taf_strength,
            "description": taf_desc,
        }
    )

    return signals


def _fetch_metar_anchor_snapshot(icao: str, timezone_iana: str = "") -> dict:
    """Fetch latest METAR anchor observation from the city ICAO station."""
    if not icao:
        return {}

    try:
        import requests as _req

        resp = _req.get(
            f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json",
            timeout=8,
            headers={
                "User-Agent": "Veyra.trade-Dashboard/1.0",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://aviationweather.gov/",
            },
        )
        if resp.status_code == 200 and resp.text.strip():
            data = resp.json()
            if data:
                obs = data[0]
                temp_c = obs.get("temp")
                report_time = obs.get("reportTime", "")
                if temp_c is not None:
                    temp_c = round(float(temp_c), 1)
                    temp_f = round(temp_c * 9 / 5 + 32, 1)

                    local_dt_str = ""
                    local_hhmm = ""
                    observed_hour = None
                    if report_time:
                        try:
                            dt_utc = datetime.fromisoformat(
                                str(report_time).replace("Z", "+00:00")
                            )
                            if timezone_iana:
                                _tz = zoneinfo.ZoneInfo(timezone_iana)
                                dt_local = dt_utc.astimezone(_tz)
                            else:
                                dt_local = dt_utc
                            local_dt_str = dt_local.strftime("%Y-%m-%d %H:%M:%S")
                            local_hhmm = dt_local.strftime("%H:%M")
                            observed_hour = dt_local.hour
                        except Exception:
                            pass

                    return {
                        "temp_c": temp_c,
                        "temp_f": temp_f,
                        "source": "METAR",
                        "obs_local_time": local_hhmm,
                        "obs_local_datetime": local_dt_str,
                        "obs_report_time": report_time,
                        "obs_local_hour": observed_hour,
                    }
    except Exception as e:
        logger.debug(f"[WeatherMap] METAR snapshot failed ({icao}): {e}")

    return {}


def _fetch_metar_history(icao: str, timezone_iana: str = "") -> list[dict]:
    """Fetch today's METAR history (last 24h) for plotting on the temperature chart."""
    if not icao:
        return []
    try:
        import requests as _req

        resp = _req.get(
            f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json&hours=24",
            timeout=10,
            headers={
                "User-Agent": "Veyra.trade-Dashboard/1.0",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://aviationweather.gov/",
            },
        )
        if resp.status_code != 200 or not resp.text.strip():
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []

        try:
            _tz = zoneinfo.ZoneInfo(timezone_iana or "UTC")
        except Exception:
            _tz = zoneinfo.ZoneInfo("UTC")
        today_str = datetime.now(_tz).strftime("%Y-%m-%d")

        points: list[dict] = []
        seen_hours: set[int] = set()
        for obs in data:
            temp_c = obs.get("temp")
            report_time = obs.get("reportTime", "")
            if temp_c is None or not report_time:
                continue
            try:
                dt_utc = datetime.fromisoformat(str(report_time).replace("Z", "+00:00"))
                dt_local = dt_utc.astimezone(_tz)
            except Exception:
                continue
            if dt_local.strftime("%Y-%m-%d") != today_str:
                continue
            hour = dt_local.hour
            if hour in seen_hours:
                continue  # keep only the first (most-recent) obs per hour
            seen_hours.add(hour)
            temp_c_val = round(float(temp_c), 1)
            points.append(
                {
                    "hour": hour,
                    "time": f"{hour:02d}:{dt_local.minute:02d}",
                    "temp_c": temp_c_val,
                    "temp_f": round(temp_c_val * 9 / 5 + 32, 1),
                }
            )
        # Sort ascending by hour
        points.sort(key=lambda x: x["hour"])
        return points
    except Exception as e:
        logger.debug(f"[WeatherMap] METAR history failed ({icao}): {e}")
        return []


@router.get("/api/weather-map/city-detail/{city_name}")
async def weather_map_city_detail(city_name: str, request: Request):
    """获取城市完整天气详情：ECMWF预报 + METAR + TAF + 峰值窗口 + 持仓。"""
    target_lang = _detect_response_lang(request)
    city_key = str(city_name or "").strip().lower()
    cache_key = f"{city_key}::{target_lang}"
    now_ts = time.time()
    cached = _CITY_DETAIL_CACHE.get(cache_key)
    if cached and (now_ts - cached["ts"]) < _CITIES_CACHE_TTL:
        return cached["data"]

    # T507 Wave 7A: SWR — within stale window, return previous payload
    # immediately and trigger an async background refresh so the next hit
    # sees fresh data. Keeps overlay open <1s on warm but expired entries.
    if cached and (now_ts - cached["ts"]) < _CITY_DETAIL_CACHE_SWR:
        if not _CITY_DETAIL_REFRESH_INFLIGHT.get(cache_key):
            _CITY_DETAIL_REFRESH_INFLIGHT[cache_key] = True
            try:
                asyncio.create_task(
                    _refresh_city_detail_async(city_name, request, cache_key)
                )
            except Exception:
                _CITY_DETAIL_REFRESH_INFLIGHT.pop(cache_key, None)
        return cached["data"]

    lock = _CITY_DETAIL_LOCKS.get(cache_key)
    if lock is None:
        lock = asyncio.Lock()
        _CITY_DETAIL_LOCKS[cache_key] = lock

    async with lock:
        cached = _CITY_DETAIL_CACHE.get(cache_key)
        now_ts = time.time()
        if cached and (now_ts - cached["ts"]) < _CITIES_CACHE_TTL:
            return cached["data"]

        weather_cities = _get_weather_cities()
        wc_by_name = {c["name"].lower(): c for c in weather_cities}
        city_cfg = wc_by_name.get(city_name.lower())

        if not city_cfg:
            return JSONResponse(
                {"status": "error", "reason": "city not found"}, status_code=404
            )

        lat = city_cfg.get("lat")
        lon = city_cfg.get("lon")
        icao = city_cfg.get("icao", "")
        timezone_iana = city_cfg.get("timezone", "UTC")

        # 峰值窗口（WeatherEventMap 专用配置）
        pw_start_cfg = get("weather_event_map.peak_window_start")
        pw_end_cfg = get("weather_event_map.peak_window_end")
        pw_start = int(10 if pw_start_cfg is None else pw_start_cfg)
        pw_end = int(17 if pw_end_cfg is None else pw_end_cfg)

        # 当地时间
        try:
            _tz = zoneinfo.ZoneInfo(timezone_iana)
        except Exception:
            _tz = zoneinfo.ZoneInfo("UTC")
        local_now = datetime.now(_tz)
        local_hour = local_now.hour
        is_in_peak = pw_start <= local_hour < pw_end

        # 峰值窗口进度
        if is_in_peak:
            total_mins = max((pw_end - pw_start) * 60, 1)
            elapsed_mins = (local_hour - pw_start) * 60 + local_now.minute
            peak_progress = min(round(elapsed_mins / total_mins * 100), 100)
        else:
            peak_progress = 100 if local_hour >= pw_end else 0

        # 谷值窗口（低温时段，WeatherEventMap 专用配置）
        tw_start_cfg = get("weather_event_map.trough_window_start")
        tw_end_cfg = get("weather_event_map.trough_window_end")
        tw_start = int(3 if tw_start_cfg is None else tw_start_cfg)
        tw_end = int(7 if tw_end_cfg is None else tw_end_cfg)
        is_in_trough = tw_start <= local_hour < tw_end
        if is_in_trough:
            tw_total_mins = max((tw_end - tw_start) * 60, 1)
            tw_elapsed_mins = (local_hour - tw_start) * 60 + local_now.minute
            trough_progress = min(round(tw_elapsed_mins / tw_total_mins * 100), 100)
        else:
            trough_progress = 100 if local_hour >= tw_end else 0

        # 当前应展示窗口：优先显示“当前进行中”，否则显示距离现在最近的下一个窗口
        now_minutes = local_hour * 60 + local_now.minute
        mins_to_peak = ((pw_start * 60) - now_minutes) % (24 * 60)
        mins_to_trough = ((tw_start * 60) - now_minutes) % (24 * 60)

        if is_in_peak and not is_in_trough:
            display_type = "peak"
        elif is_in_trough and not is_in_peak:
            display_type = "trough"
        else:
            display_type = "peak" if mins_to_peak <= mins_to_trough else "trough"

        if display_type == "peak":
            display_label = "峰值窗口"
            display_start = pw_start
            display_end = pw_end
            display_active = is_in_peak
            display_progress = peak_progress
        else:
            display_label = "谷值窗口"
            display_start = tw_start
            display_end = tw_end
            display_active = is_in_trough
            display_progress = trough_progress

        # 并行获取数据
        ecmwf_data = await asyncio.to_thread(_fetch_ecmwf_hourly, lat, lon, timezone_iana)
        multi_model = await asyncio.to_thread(
            _fetch_multi_model_max, lat, lon, timezone_iana
        )
        metar_snapshot = await asyncio.to_thread(
            _fetch_metar_anchor_snapshot, icao, timezone_iana
        )
        metar_history = (
            await asyncio.to_thread(_fetch_metar_history, icao, timezone_iana)
            if icao
            else []
        )

        current_temp_c = metar_snapshot.get("temp_c")
        current_temp_f = metar_snapshot.get("temp_f")
        temp_source = metar_snapshot.get("source", "")
        if current_temp_c is None or current_temp_f is None:
            current_temp_c, current_temp_f, temp_source = await asyncio.to_thread(
                _get_current_temp, lat, lon, city_cfg.get("name", "")
            )
        metar_decoded = (
            await asyncio.to_thread(_fetch_and_decode_metar, icao, timezone_iana)
            if icao
            else ""
        )
        taf_decoded = await asyncio.to_thread(_fetch_and_decode_taf, icao) if icao else ""
        short_term = (
            await asyncio.to_thread(_fetch_short_term_trend, lat, lon)
            if lat and lon
            else {}
        )
        bets = await asyncio.to_thread(
            _get_city_bets,
            city_name,
            weather_cities,
            str((get_request_identity(request) or {}).get("wallet_address") or "").strip().lower(),
        )

        # 计算云量和风速均值（从ECMWF小时数据）
        hourly_list = ecmwf_data.get("hourly", [])
        cloud_vals = [h["cloud"] for h in hourly_list if h.get("cloud") is not None]
        wind_vals = [h["wind"] for h in hourly_list if h.get("wind") is not None]
        cloud_avg = round(sum(cloud_vals) / len(cloud_vals), 1) if cloud_vals else None
        wind_avg = round(sum(wind_vals) / len(wind_vals), 1) if wind_vals else None

        # 生成专业气象判断
        judgment = _generate_judgment(
            current_temp_c,
            ecmwf_data.get("daily_max"),
            taf_decoded,
            cloud_avg,
            multi_model,
            display_label,
            display_active,
            display_progress,
        )

        # 信号贡献分析
        signal_contributions = _derive_signal_contributions(
            current_temp_c,
            hourly_list,
            local_hour,
            taf_decoded,
            cloud_avg,
            wind_avg,
        )

        market_links = await asyncio.to_thread(
            _resolve_market_links,
            city_cfg.get("name", city_name),
            local_now,
        )

        result = {
            "status": "ok",
            "city": city_cfg.get("name", city_name),
            "settlement_url": city_cfg.get("settlement_url", ""),
            "lat": lat,
            "lon": lon,
            "timezone": timezone_iana,
            "local_time": local_now.strftime("%Y-%m-%d %H:%M:%S"),
            "local_hour": local_hour,
            # 温度
            "current_temp_c": current_temp_c,
            "current_temp_f": current_temp_f,
            "temp_source": temp_source,
            "metar_observed_time": metar_snapshot.get("obs_local_time", ""),
            "metar_observed_datetime": metar_snapshot.get("obs_local_datetime", ""),
            "metar_observed_hour": metar_snapshot.get("obs_local_hour"),
            "metar_report_time": metar_snapshot.get("obs_report_time", ""),
            # ECMWF 预报
            "ecmwf": ecmwf_data,
            # 峰值窗口
            "peak_window": {
                "start": pw_start,
                "end": pw_end,
                "is_active": is_in_peak,
                "progress": peak_progress,
                "start_time": f"{pw_start:02d}:00",
                "end_time": f"{pw_end:02d}:00",
            },
            # 谷值窗口（低温时段）
            "trough_window": {
                "start": tw_start,
                "end": tw_end,
                "is_active": is_in_trough,
                "progress": trough_progress,
                "start_time": f"{tw_start:02d}:00",
                "end_time": f"{tw_end:02d}:00",
            },
            "window_context": {
                "type": display_type,
                "label": display_label,
                "start": display_start,
                "end": display_end,
                "is_active": display_active,
                "progress": display_progress,
                "start_time": f"{display_start:02d}:00",
                "end_time": f"{display_end:02d}:00",
            },
            # 气象数据
            "metar_decoded": metar_decoded,
            "taf_decoded": taf_decoded,
            "short_term": short_term,
            # Phase 2: 专业气象判断
            "judgment": judgment,
            # Phase 2: 信号贡献
            "signal_contributions": signal_contributions,
            # Phase 2: 多模型对比
            "model_comparison": multi_model,
            # 持仓
            "bets": bets,
            "active_bets_count": len(bets),
            # METAR 当日历史观测序列（用于图表多点绘制）
            "metar_history": metar_history,
            "market_links": market_links,
        }

        if target_lang == "en":
            result = _translate_dict(result, target_lang)

        _CITY_DETAIL_CACHE[cache_key] = {"data": result, "ts": time.time()}
        return result


async def _refresh_city_detail_async(city_name: str, request: Request, cache_key: str):
    """T507 Wave 7A: background refresh for city-detail SWR.

    Re-invokes the same handler logic by simply calling the FastAPI handler
    coroutine; the inner cache write will populate the latest payload, and
    we always clear the inflight flag in finally.
    """
    try:
        await weather_map_city_detail(city_name, request)
    except Exception as e:
        logger.debug(f"[WeatherMap] SWR refresh city-detail failed for {cache_key}: {e}")
    finally:
        _CITY_DETAIL_REFRESH_INFLIGHT.pop(cache_key, None)


# ── 历史准确率对账 API ─────────────────────────────────────────────────

_HISTORY_CACHE: dict = {}  # city_name → {data, ts}
_HISTORY_CACHE_TTL = 600  # 10 分钟


def _fetch_historical_weather(
    lat: float, lon: float, timezone_iana: str, days: int = 14
) -> dict:
    """获取过去 N 天的实测最高温 + 多模型预测最高温（用于历史对账）。

    实测温度数据源优先级：
      P1: WU v1 Historical Observations JSON API（与 PM 结算源一致）
      P2: Open-Meteo Archive API（ERA5 Land 再分析，作为降级备选）
    """
    from datetime import timedelta as _td

    import requests as _req

    try:
        _tz = zoneinfo.ZoneInfo(timezone_iana or "UTC")
    except Exception:
        _tz = zoneinfo.ZoneInfo("UTC")
    local_now = datetime.now(_tz)
    end_date = (local_now - _td(days=1)).strftime("%Y-%m-%d")
    start_date = (local_now - _td(days=days)).strftime("%Y-%m-%d")

    result: dict = {"days": [], "start_date": start_date, "end_date": end_date}

    # 1. P1: WU v1 Historical Observations JSON API（与 PM 结算源一致）
    actual_temps: dict = {}
    actual_source = "open-meteo"
    try:
        from src.utils.weather_fetcher import _WU_API_KEYS  # noqa: PLC2701

        if _WU_API_KEYS and lat and lon:
            _start_api = (local_now - _td(days=days)).strftime("%Y%m%d")
            _end_api = (local_now - _td(days=1)).strftime("%Y%m%d")
            for _key in _WU_API_KEYS:
                try:
                    _api_url = (
                        f"https://api.weather.com/v1/geocode/{lat}/{lon}"
                        f"/observations/historical.json"
                        f"?apiKey={_key}"
                        f"&startDate={_start_api}&endDate={_end_api}"
                        f"&units=e"
                    )
                    _resp = _req.get(_api_url, timeout=20)
                    if _resp.status_code == 401:
                        continue
                    _resp.raise_for_status()
                    _obs = _resp.json().get("observations", [])
                    # 按本地日期分组，取每日 max temp
                    from collections import defaultdict

                    _daily: dict = defaultdict(list)
                    for o in _obs:
                        if not isinstance(o.get("temp"), (int, float)):
                            continue
                        # obs 有 valid_time_gmt (epoch), 转本地日期
                        _epoch = o.get("valid_time_gmt") or o.get("obs_time", 0)
                        if _epoch:
                            _obs_dt = datetime.fromtimestamp(int(_epoch), tz=_tz)
                            _obs_date = _obs_dt.strftime("%Y-%m-%d")
                        else:
                            continue
                        _daily[_obs_date].append(float(o["temp"]))
                    if _daily:
                        for _d, _temps_list in _daily.items():
                            _max_f = round(max(_temps_list), 1)
                            _min_f = round(min(_temps_list), 1)
                            _max_c = round((_max_f - 32) * 5 / 9, 1)
                            _min_c = round((_min_f - 32) * 5 / 9, 1)
                            actual_temps[_d] = {
                                "max": _max_c,
                                "min": _min_c,
                                "max_f": _max_f,
                                "min_f": _min_f,
                            }
                        actual_source = "wu-v1-historical"
                        logger.debug(
                            f"[WeatherMap] History: WU API returned "
                            f"{len(_daily)} days for {lat},{lon}"
                        )
                    break
                except _req.RequestException as _e:
                    logger.debug(f"[WeatherMap] WU history API error: {_e}")
                    continue
    except ImportError:
        pass

    # P2 fallback: Open-Meteo Archive API
    if not actual_temps:
        try:
            resp = _req.get(
                "https://archive-api.open-meteo.com/v1/archive",
                params={
                    "latitude": f"{lat:.4f}",
                    "longitude": f"{lon:.4f}",
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "timezone": timezone_iana or "auto",
                    "start_date": start_date,
                    "end_date": end_date,
                },
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json().get("daily", {})
                dates = data.get("time", [])
                maxes = data.get("temperature_2m_max", [])
                mins = data.get("temperature_2m_min", [])
                for i, d in enumerate(dates):
                    _mc = (
                        round(float(maxes[i]), 1)
                        if i < len(maxes) and maxes[i] is not None
                        else None
                    )
                    _nc = (
                        round(float(mins[i]), 1)
                        if i < len(mins) and mins[i] is not None
                        else None
                    )
                    actual_temps[d] = {
                        "max": _mc,
                        "min": _nc,
                        "max_f": (
                            round(_mc * 9 / 5 + 32, 1) if _mc is not None else None
                        ),
                        "min_f": (
                            round(_nc * 9 / 5 + 32, 1) if _nc is not None else None
                        ),
                    }
                actual_source = "open-meteo"
        except Exception as e:
            logger.debug(f"[WeatherMap] Historical actual fetch error: {e}")

    # 2. 获取多模型历史预测最高温
    models_list = [
        ("ecmwf_ifs025", "ECMWF"),
        ("gfs_seamless", "GFS"),
        ("icon_seamless", "ICON"),
        ("gem_seamless", "GEM"),
        ("jma_seamless", "JMA"),
    ]
    model_temps: dict = {m[1]: {} for m in models_list}
    try:
        models_param = ",".join(m[0] for m in models_list)
        resp = _req.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "daily": "temperature_2m_max",
                "models": models_param,
                "timezone": timezone_iana or "auto",
                "start_date": start_date,
                "end_date": end_date,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json().get("daily", {})
            dates = data.get("time", [])
            for api_name, display_name in models_list:
                key = f"temperature_2m_max_{api_name}"
                vals = data.get(key, [])
                for i, d in enumerate(dates):
                    if i < len(vals) and vals[i] is not None:
                        model_temps[display_name][d] = round(float(vals[i]), 1)
    except Exception as e:
        logger.debug(f"[WeatherMap] Historical model fetch error: {e}")

    # 3. 合并数据
    all_dates = sorted(actual_temps.keys(), reverse=True)
    best_model = ""
    best_mae = 999.0
    model_maes: dict = {m[1]: [] for m in models_list}
    fusion_maes: list = []

    for d in all_dates:
        actual = actual_temps.get(d, {})
        actual_max = actual.get("max")
        models_for_day = {}
        model_maxes = []
        for _, display_name in models_list:
            v = model_temps[display_name].get(d)
            models_for_day[display_name] = v
            if v is not None:
                model_maxes.append(v)
                if actual_max is not None:
                    model_maes[display_name].append(abs(v - actual_max))

        fusion = round(sum(model_maxes) / len(model_maxes), 1) if model_maxes else None
        if fusion is not None and actual_max is not None:
            fusion_maes.append(abs(fusion - actual_max))

        result["days"].append(
            {
                "date": d,
                "actual_max": actual_max,
                "actual_min": actual.get("min"),
                "actual_max_f": actual.get("max_f"),
                "actual_min_f": actual.get("min_f"),
                "models": models_for_day,
                "fusion": fusion,
            }
        )

    # 4. 计算统计
    fusion_mae = round(sum(fusion_maes) / len(fusion_maes), 1) if fusion_maes else None
    model_stats = []
    for _, display_name in models_list:
        maes = model_maes[display_name]
        mae = round(sum(maes) / len(maes), 1) if maes else None
        if mae is not None and mae < best_mae:
            best_mae = mae
            best_model = display_name
        model_stats.append({"model": display_name, "mae": mae, "samples": len(maes)})

    hits = sum(1 for m in fusion_maes if m < 1.0)
    hit_rate = round(hits / len(fusion_maes) * 100) if fusion_maes else 0

    result["stats"] = {
        "fusion_mae": fusion_mae,
        "best_model": best_model,
        "best_model_mae": best_mae if best_mae < 999 else None,
        "hit_rate": hit_rate,
        "hit_count": hits,
        "total_days": len(fusion_maes),
        "model_stats": model_stats,
        "actual_source": actual_source,
    }

    return result


@router.get("/api/weather-map/city-history/{city_name}")
async def weather_map_city_history(city_name: str):
    """获取城市历史天气对账数据（过去14天实测 vs 模型预测）。"""
    now = time.time()
    cache_key = city_name.lower()
    cached = _HISTORY_CACHE.get(cache_key)
    if cached and (now - cached["ts"]) < _HISTORY_CACHE_TTL:
        return cached["data"]

    weather_cities = _get_weather_cities()
    wc_by_name = {c["name"].lower(): c for c in weather_cities}
    city_cfg = wc_by_name.get(city_name.lower())

    if not city_cfg:
        return JSONResponse(
            {"status": "error", "reason": "city not found"}, status_code=404
        )

    lat = city_cfg.get("lat")
    lon = city_cfg.get("lon")
    timezone_iana = city_cfg.get("timezone", "UTC")

    history = await asyncio.to_thread(
        _fetch_historical_weather, lat, lon, timezone_iana, 14
    )

    result = {
        "status": "ok",
        "city": city_cfg.get("name", city_name),
        "timezone": timezone_iana,
        **history,
    }

    _HISTORY_CACHE[cache_key] = {"data": result, "ts": now}
    return result
