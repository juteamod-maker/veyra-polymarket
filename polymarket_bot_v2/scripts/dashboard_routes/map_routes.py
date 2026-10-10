"""
dashboard_routes/map_routes.py — T146 全球事件看板 API
提供 Leaflet.js 地图所需的 Markers、城市投注、实时温度端点。
"""

import asyncio
import json
import logging
import re
import time
import unicodedata
import uuid
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import (
    DB_PATH,
    ROOT,
    db_connect,
    get,
    get_request_identity,
)

router = APIRouter()
logger = logging.getLogger("web_dashboard")

# ── 存储路径 ──────────────────────────────────────────────────────────
MARKERS_FILE = ROOT / "data" / "map_markers.json"

# ── 城市信息缓存（城市名 → {data, ts}，TTL=120s）───────────────────────
_CITYINFO_CACHE: dict = {}
_CITYINFO_CACHE_TTL = 120  # 2 分钟（METAR每小时更新，120秒确保popup展示最新观测）

# ── 时区缓存（lat,lon → IANA timezone，TTL=86400s 永久型）──────────────────
_TIMEZONE_CACHE: dict = {}

# ── Markers 缓存（避免每次请求都做DB查询+regex解析）──────────────────────
_MARKERS_CACHE: dict = {"data": None, "ts": 0}
_MARKERS_CACHE_TTL = 30  # 30 秒


# ── 内部工具 ──────────────────────────────────────────────────────────


def _load_markers() -> list:
    """从 JSON 文件加载手动添加的 Markers。"""
    if not MARKERS_FILE.exists():
        return []
    try:
        return json.loads(MARKERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_markers(markers: list) -> None:
    """将手动 Markers 保存到 JSON 文件。"""
    MARKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MARKERS_FILE.write_text(
        json.dumps(markers, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _get_weather_cities() -> list:
    """从 config 获取 44 个天气城市（含 lat/lon/name）。"""
    try:
        cfg = json.loads(
            Path(ROOT / "config" / "config.json").read_text(encoding="utf-8")
        )
        return cfg.get("weather_cities", {}).get("cities", [])
    except Exception:
        return []


# 城市别名映射：config 简称 → 全称列表（用于匹配 Polymarket 问题文本中的完整城市名）
_CITY_ALIASES: dict[str, list[str]] = {
    "nyc": ["new york city", "new york"],
    "la": ["los angeles"],
    "sf": ["san francisco"],
    "dc": ["washington dc", "washington d.c."],
}


def _normalize_unicode(s: str) -> str:
    """去除重音符号，如 São → Sao、Zürich → Zurich。"""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def _extract_city_from_question(question: str, weather_cities: list) -> str | None:
    """
    从 market question 中提取城市名。
    格式：'Will the highest temperature in {CITY} ...'
    返回匹配到的 config 中的城市名，不匹配则返回 None。
    """
    if not question:
        return None
    # 尝试 "in {CITY} (be|exceed|reach|on|hit)"
    m = re.search(
        r"\bin\s+(.+?)\s+(?:be|exceed|reach|on\s+\w|hit)", question, re.IGNORECASE
    )
    extracted = m.group(1).strip() if m else None

    if not extracted:
        return None

    # Unicode 归一化后的提取城市名（去重音）
    extracted_lower = _normalize_unicode(extracted).lower()

    # Pass 1: 精确匹配（Unicode 归一化）
    for city in weather_cities:
        cn_norm = _normalize_unicode(city.get("name", "")).lower()
        if cn_norm == extracted_lower:
            return city["name"]

    # Pass 2: 别名匹配（config 简称 ↔ 问题全称）
    for city in weather_cities:
        cn_norm = _normalize_unicode(city.get("name", "")).lower()
        aliases = _CITY_ALIASES.get(cn_norm, [])
        if extracted_lower in aliases:
            return city["name"]
        # 反向：提取名是简称，config 是全称
        extracted_aliases = _CITY_ALIASES.get(extracted_lower, [])
        if cn_norm in extracted_aliases:
            return city["name"]

    # Pass 3: 宽松部分匹配（归一化后）
    for city in weather_cities:
        cn_norm = _normalize_unicode(city.get("name", "")).lower()
        if cn_norm in extracted_lower or extracted_lower in cn_norm:
            return city["name"]
    return None


def _gen_weather_slug(question: str, kind: str = "market") -> str:
    """从天气问题文本生成 slug 作为缺失时的 fallback。kind='event'|'market'。"""
    if not question:
        return ""
    q = question.lower().strip().rstrip("?").strip()
    m = re.match(
        r"will the (highest|lowest) temperature in (.+?) be (\d+)°([cf])\s*(or higher|or lower)?\s*on ([a-z]+)\s+(\d+)",
        q,
    )
    if not m:
        return ""
    var, city, temp, unit = (
        m.group(1),
        m.group(2).strip(),
        m.group(3),
        m.group(4).lower(),
    )
    qualifier = (m.group(5) or "").strip().replace(" ", "")
    month, day = m.group(6), m.group(7)
    year = str(datetime.now(timezone.utc).year)
    city_slug = re.sub(r"[^a-z0-9]+", "-", city).strip("-")
    base = f"{var}-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    if kind == "event":
        return base
    temp_suffix = f"{temp}{unit}{qualifier}" if qualifier else f"{temp}{unit}"
    return f"{base}-{temp_suffix}"


def _build_auto_markers(weather_cities: list) -> list:
    """
    从 DB paper_bets 中查询全部天气类投注，自动构造城市 Marker。
    每个城市只生成一个 Marker，下辖多个 events。
    """
    city_map: dict[str, dict] = {}

    try:
        with db_connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT bet_id, question, outcome, market_price, exit_price,
                       ts_entered, condition_id, market_slug, event_slug
                FROM paper_bets
                WHERE (category = 'weather'
                       OR (category = 'unknown' AND (
                           strategy_tag LIKE 'weather_%'
                           OR question LIKE '%temperature%'
                           OR question LIKE '%celsius%'
                           OR question LIKE '%fahrenheit%'
                       )))
                  AND resolved_at IS NULL
                ORDER BY ts_entered DESC
                """
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning(f"[MapRoutes] DB query failed: {e}")
        rows = []

    wc_by_name = {c["name"].lower(): c for c in weather_cities}

    for row in rows:
        (
            bet_id,
            question,
            outcome,
            market_price,
            exit_price,
            ts_entered,
            condition_id,
            market_slug,
            event_slug,
        ) = row

        city_name = _extract_city_from_question(str(question or ""), weather_cities)
        if not city_name:
            continue

        city_info = wc_by_name.get(city_name.lower(), {})
        lat = city_info.get("lat")
        lon = city_info.get("lon")
        if lat is None or lon is None:
            continue

        key = city_name.lower()
        if key not in city_map:
            city_map[key] = {
                "id": f"auto_{key.replace(' ', '_')}",
                "city": city_name,
                "lat": lat,
                "lon": lon,
                "category": "weather",
                "timezone": city_info.get("timezone", "UTC"),
                "is_auto": True,
                "events": [],
            }

        city_map[key]["events"].append(
            {
                "id": f"bet_{bet_id}",
                "title": city_name,
                "description": str(question or ""),
                "bet_id": bet_id,
                "is_bet": True,
                "outcome": outcome,
                "market_price": market_price,
                "exit_price": exit_price,
                "ts_entered": ts_entered,
                "market_slug": market_slug or _gen_weather_slug(question, "market"),
                "event_slug": event_slug or _gen_weather_slug(question, "event"),
                "condition_id": condition_id,
            }
        )

    return list(city_map.values())


def _get_current_temp(
    lat: float, lon: float, city_name: str = ""
) -> tuple[float | None, float | None, str]:
    """获取当前实时温度，返回 (°C, °F, source)。

    T233追加: METAR 实时观测数据优先。
    P0: METAR 观测站（复用 fetch_realtime_temperature，3层降级）。
    P1: WU v3 observations/current（metric, temperature字段）。
    P2: Open-Meteo current_weather（模型数据，备用）。
    """
    # ── P0: METAR 实时观测（T233: 与风控决策使用同一数据源） ──
    if city_name:
        try:
            from src.utils.weather_fetcher import fetch_realtime_temperature

            _tc = fetch_realtime_temperature(city_name)
            if _tc is not None:
                _tf = round(_tc * 9 / 5 + 32, 1)
                return (round(_tc, 1), _tf, "METAR")
        except Exception:
            pass

    # ── P1: WU v3 observations/current ──
    try:
        import requests as _req
        from src.utils.weather_fetcher import _WU_API_KEYS  # noqa: PLC2701

        if _WU_API_KEYS and lat and lon:
            _geocode = f"{lat:.4f},{lon:.4f}"
            for _key in _WU_API_KEYS:
                try:
                    _url = (
                        f"https://api.weather.com/v3/wx/observations/current"
                        f"?geocode={_geocode}&language=en-US&units=m&format=json"
                        f"&apiKey={_key}"
                    )
                    _resp = _req.get(_url, timeout=8)
                    if _resp.status_code == 401:
                        continue  # 尝试下一个 key
                    _resp.raise_for_status()
                    _t = _resp.json().get("temperature")
                    if isinstance(_t, (int, float)):
                        _tc = round(float(_t), 1)
                        _tf = round(_tc * 9 / 5 + 32, 1)
                        return (_tc, _tf, "WU")
                except Exception:
                    continue
    except Exception:
        pass

    # ── P2: Open-Meteo current_weather ──
    try:
        import requests as _req

        _resp = _req.get(
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}&current_weather=true",
            timeout=6,
        )
        if _resp.status_code == 200:
            _t = _resp.json().get("current_weather", {}).get("temperature")
            if _t is not None:
                _tc = round(float(_t), 1)
                _tf = round(_tc * 9 / 5 + 32, 1)
                return (_tc, _tf, "Open-Meteo")
    except Exception:
        pass
    return (None, None, "")


# ── METAR / TAF 解码辅助 ──────────────────────────────────────────
_WIND_DIRS_8 = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
_CLOUD_ZH = {
    "FEW": "少云",
    "SCT": "疏云",
    "BKN": "多云",
    "OVC": "阴天",
    "CLR": "晴",
    "SKC": "晴",
    "NSC": "无重要云",
    "NCD": "无云",
}
_WX_ZH = {
    "RA": "雨",
    "SN": "雪",
    "DZ": "毛毛雨",
    "TS": "雷暴",
    "FG": "雾",
    "BR": "轻雾",
    "HZ": "霾",
    "GR": "冰雹",
    "GS": "小冰雹",
    "PL": "冰粒",
    "FU": "烟",
    "DU": "尘",
    "SA": "沙",
    "SQ": "飑",
    "IC": "冰晶",
    "UP": "未知降水",
}


def _wind_compass(deg) -> str:
    if deg is None:
        return "不定"
    try:
        d = int(deg)
        if d == 0:
            return "北"
        return _WIND_DIRS_8[round(d / 45) % 8]
    except (ValueError, TypeError):
        return str(deg)


def _decode_wx_string(wx: str) -> str:
    if not wx:
        return ""
    result = ""
    if wx.startswith("+"):
        result = "强"
        wx = wx[1:]
    elif wx.startswith("-"):
        result = "弱"
        wx = wx[1:]
    if wx.startswith("VC"):
        result += "附近"
        wx = wx[2:]
    i = 0
    while i < len(wx):
        code = wx[i : i + 2]
        result += _WX_ZH.get(code, code)
        i += 2
    return result


def _format_clouds(clouds: list) -> str:
    parts = []
    for c in clouds:
        cover = _CLOUD_ZH.get(c.get("cover", ""), c.get("cover", ""))
        base = c.get("base", "")
        ctype = c.get("type", "")
        text = f"{cover}{base}ft" if base else cover
        if ctype:
            text += f"({ctype})"
        parts.append(text)
    return " | ".join(parts)


def _fetch_and_decode_metar(icao: str, timezone_iana: str = "") -> str:
    """从 aviationweather.gov JSON API 获取 METAR 并解码为简洁专业的中文摘要。"""
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

                parts: list[str] = []
                # 观测时间 → C: 转换为城市当地时间
                rt = obs.get("reportTime", "")
                if rt:
                    try:
                        dt_utc = datetime.fromisoformat(rt.replace("Z", "+00:00"))
                        if timezone_iana:
                            _tz = zoneinfo.ZoneInfo(timezone_iana)
                            dt_local = dt_utc.astimezone(_tz)
                            parts.append(
                                f"观测 {dt_local.day}日"
                                f"{dt_local.hour:02d}:{dt_local.minute:02d} 当地"
                            )
                        else:
                            parts.append(
                                f"观测 {dt_utc.day}日"
                                f"{dt_utc.hour:02d}:{dt_utc.minute:02d}Z"
                            )
                    except Exception:
                        pass
                # 温度 / 露点
                temp = obs.get("temp")
                if temp is not None:
                    tf = round(float(temp) * 9 / 5 + 32, 1)
                    p = f"气温 {temp}°C ({tf}°F)"
                    dewp = obs.get("dewp")
                    if dewp is not None:
                        p += f"，露点 {dewp}°C"
                    parts.append(p)
                # 风
                wspd = obs.get("wspd")
                if wspd is not None:
                    wdir = obs.get("wdir")
                    if wdir == 0 and wspd == 0:
                        parts.append("静风")
                    else:
                        w = f"{_wind_compass(wdir)}风 {wspd}kt"
                        wgst = obs.get("wgst")
                        if wgst:
                            w += f" 阵风{wgst}kt"
                        parts.append(w)
                # 能见度
                visib = obs.get("visib")
                if visib is not None:
                    parts.append(f"能见度 {visib}SM")
                # 云层
                clouds = obs.get("clouds", [])
                if clouds:
                    parts.append(_format_clouds(clouds))
                # 天气现象
                wx = obs.get("wxString")
                if wx:
                    parts.append(_decode_wx_string(wx))
                # 气压 + 飞行类别
                extras = []
                fltcat = obs.get("fltcat")
                if fltcat:
                    extras.append(fltcat)
                altim = obs.get("altim")
                if altim:
                    extras.append(f"{round(float(altim), 1)}hPa")
                if extras:
                    parts.append(" / ".join(extras))

                if parts:
                    return "  |  ".join(parts)

        # 降级：复用交易系统 METAR 链路（aviationweather → metarcentral → NWS raw）。
        from src.utils.weather_fetcher import _fetch_metar_observation  # noqa: PLC2701

        obs = _fetch_metar_observation(icao)
        if not obs:
            return ""

        fallback_parts: list[str] = []
        obs_time = obs.get("obs_time")
        if obs_time:
            fallback_parts.append(f"观测 {obs_time}")
        temp_c = obs.get("temp_c")
        if temp_c is not None:
            temp_f = round(float(temp_c) * 9 / 5 + 32, 1)
            p = f"气温 {temp_c}°C ({temp_f}°F)"
            dewpoint = obs.get("dewpoint_c")
            if dewpoint is not None:
                p += f"，露点 {dewpoint}°C"
            fallback_parts.append(p)
        src = obs.get("source")
        if src:
            fallback_parts.append(f"来源 {src}")
        raw = obs.get("raw")
        if raw:
            fallback_parts.append(str(raw)[:160])

        return "  |  ".join(fallback_parts)
    except Exception as e:
        logger.debug(f"[MapRoutes] METAR decode failed ({icao}): {e}")
        return ""


def _fetch_nws_raw_taf(icao: str) -> str:
    """NWS tgftp TAF 原文降级源。"""
    try:
        import requests as _req

        resp = _req.get(
            f"https://tgftp.nws.noaa.gov/data/forecasts/taf/stations/{icao}.TXT",
            timeout=8,
            headers={
                "User-Agent": "Veyra.trade-Dashboard/1.0",
                "Accept": "text/plain,*/*",
            },
        )
        resp.raise_for_status()
        lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
        if len(lines) < 2:
            return ""
        raw_taf = " ".join(lines[1:]).strip()
        return raw_taf
    except Exception:
        return ""


def _fetch_and_decode_taf(icao: str) -> str:
    """从 aviationweather.gov JSON API 获取 TAF 并解码为人性化的自然语言中文预报。"""
    try:
        import requests as _req

        resp = _req.get(
            f"https://aviationweather.gov/api/data/taf?ids={icao}&format=json",
            timeout=8,
            headers={
                "User-Agent": "Veyra.trade-Dashboard/1.0",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://aviationweather.gov/",
            },
        )
        if resp.status_code != 200 or not resp.text.strip():
            raw_taf = _fetch_nws_raw_taf(icao)
            if raw_taf:
                return f"TAF原文(降级源NWS)：{raw_taf[:280]}"
            return ""
        data = resp.json()
        if not data:
            raw_taf = _fetch_nws_raw_taf(icao)
            if raw_taf:
                return f"TAF原文(降级源NWS)：{raw_taf[:280]}"
            return ""
        taf = data[0]

        fcsts = taf.get("fcsts", [])[:6]
        if not fcsts:
            raw_taf = _fetch_nws_raw_taf(icao)
            if raw_taf:
                return f"TAF原文(降级源NWS)：{raw_taf[:280]}"
            return ""

        # 计算有效期总时长用于分段描述
        vf = taf.get("validTimeFrom", "")
        try:
            dt_from = datetime.fromisoformat(vf.replace("Z", "+00:00"))
        except Exception:
            dt_from = None

        segments: list[str] = []
        tempo_notes: list[str] = []

        for i, fc in enumerate(fcsts):
            change = fc.get("change", "")

            # 时间范围计算
            time_label = ""
            try:
                tf = fc.get("timeFrom", "")
                tt = fc.get("timeTo", "")
                if tf and tt and dt_from:
                    d1 = datetime.fromisoformat(tf.replace("Z", "+00:00"))
                    d2 = datetime.fromisoformat(tt.replace("Z", "+00:00"))
                    h_start = int((d1 - dt_from).total_seconds() / 3600)
                    h_end = int((d2 - dt_from).total_seconds() / 3600)
                    if h_start <= 0:
                        time_label = f"当前至+{h_end}小时"
                    else:
                        time_label = f"+{h_start}至+{h_end}小时"
            except Exception:
                pass

            # 构建自然语言描述
            desc_parts: list[str] = []
            wspd = fc.get("wspd")
            if wspd is not None:
                wdir = fc.get("wdir")
                w = f"{_wind_compass(wdir)}风{wspd}节"
                wgst = fc.get("wgst")
                if wgst:
                    w += f"，阵风{wgst}节"
                desc_parts.append(w)

            visib = fc.get("visib")
            if visib is not None:
                if isinstance(visib, (int, float)) and visib >= 6:
                    desc_parts.append("能见度良好")
                else:
                    desc_parts.append(f"能见度{visib}SM")

            clouds = fc.get("clouds", [])
            if clouds:
                cloud_strs = []
                for c in clouds:
                    cover = _CLOUD_ZH.get(c.get("cover", ""), c.get("cover", ""))
                    base = c.get("base")
                    s = f"{cover}{base}英尺" if base else cover
                    cb = c.get("type", "")
                    if cb:
                        s += f"({cb})"
                    cloud_strs.append(s)
                desc_parts.append("，".join(cloud_strs))

            wx = fc.get("wxString")
            if wx:
                desc_parts.append(_decode_wx_string(wx))

            if not desc_parts:
                continue

            detail = "，".join(desc_parts)

            if change == "TEMPO":
                note = f"期间可能短暂出现：{detail}"
                if time_label:
                    note = f"{time_label}间{note[2:]}"
                tempo_notes.append(note)
            elif change == "BECMG":
                seg = f"逐渐转变为{detail}"
                if time_label:
                    seg = f"{time_label}，{seg}"
                segments.append(seg)
            elif change and change.startswith("FM"):
                seg = detail
                if time_label:
                    seg = f"{time_label}转为{detail}"
                segments.append(seg)
            elif change and change.startswith("PROB"):
                prob = change.replace("PROB", "").strip()
                seg = f"{prob}%概率出现{detail}"
                tempo_notes.append(seg)
            else:
                # 主预报段
                if i == 0:
                    prefix = f"初始时段（{time_label}）" if time_label else "初始时段"
                else:
                    prefix = f"后续时段（{time_label}）" if time_label else "后续时段"
                segments.append(f"{prefix}：{detail}")

        # 生成总体趋势总结
        summary_parts: list[str] = []
        # 分析风力变化
        winds = [
            fc.get("wspd")
            for fc in fcsts
            if fc.get("wspd") is not None and fc.get("change", "") not in ("TEMPO",)
        ]
        if winds:
            if max(winds) <= 10:
                summary_parts.append("风力较弱")
            elif max(winds) >= 25:
                summary_parts.append("风力较强需注意")

        # 分析天气现象
        all_wx = [fc.get("wxString", "") for fc in fcsts if fc.get("wxString")]
        if any("TS" in w for w in all_wx):
            summary_parts.append("有雷暴活动")
        elif any("RA" in w or "SN" in w or "DZ" in w for w in all_wx):
            summary_parts.append("可能出现降水")

        # 分析云层趋势
        main_clouds = [
            fc.get("clouds", [])
            for fc in fcsts
            if fc.get("change", "") not in ("TEMPO",) and fc.get("clouds")
        ]
        if len(main_clouds) >= 2:
            first_max = max(
                (
                    ["FEW", "SCT", "BKN", "OVC"].index(c.get("cover", "FEW"))
                    for c in main_clouds[0]
                ),
                default=0,
            )
            last_max = max(
                (
                    ["FEW", "SCT", "BKN", "OVC"].index(c.get("cover", "FEW"))
                    for c in main_clouds[-1]
                ),
                default=0,
            )
            if last_max > first_max:
                summary_parts.append("云层逐渐增厚")
            elif last_max < first_max:
                summary_parts.append("云层趋于消散")

        if not summary_parts:
            summary_parts.append("天气条件总体平稳")

        result = "；".join(segments)
        if tempo_notes:
            result += "。" + "；".join(tempo_notes)
        result += "。趋势总结：" + "，".join(summary_parts) + "。"

        return result
    except Exception as e:
        logger.debug(f"[MapRoutes] TAF decode failed ({icao}): {e}")
        raw_taf = _fetch_nws_raw_taf(icao)
        if raw_taf:
            return f"TAF原文(降级源NWS)：{raw_taf[:280]}"
        return ""


def _fetch_short_term_trend(lat: float, lon: float) -> dict:
    """从 Open-Meteo Forecast API (ECMWF IFS 0.25°) 获取短期天气趋势。

    返回结构化 dict 供前端表格渲染：{"metrics": [...], "hourly": [...]}.
    使用 ECMWF IFS025 确定性模型 — 全球 0.25° 高精度，含 hourly + daily。
    """
    try:
        import requests as _req

        resp = _req.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "hourly": (
                    "temperature_2m,dewpoint_2m,cloud_cover,"
                    "wind_speed_10m,shortwave_radiation"
                ),
                "daily": "temperature_2m_max,temperature_2m_min",
                "models": "ecmwf_ifs025",
                "timezone": "auto",
                "forecast_days": "1",
            },
            timeout=8,
        )
        if resp.status_code != 200:
            logger.debug(
                f"[MapRoutes] Open-Meteo short-term {resp.status_code} ({lat},{lon})"
            )
            return {}
        data = resp.json()

        hourly = data.get("hourly", {})
        daily = data.get("daily", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        dewpoints = hourly.get("dewpoint_2m", [])
        clouds = hourly.get("cloud_cover", [])
        winds = hourly.get("wind_speed_10m", [])
        rads = hourly.get("shortwave_radiation", [])

        # 过滤 None
        def _safe(lst: list) -> list[float]:
            return [float(v) if v is not None else 0.0 for v in lst]

        temps = _safe(temps)
        dewpoints = _safe(dewpoints)
        clouds = _safe(clouds)
        winds = _safe(winds)
        rads = _safe(rads)

        metrics: list[dict] = []

        # 气温趋势（取 0h, 4h, 8h, 12h 四个采样点）
        if len(temps) >= 10:
            pts = [(0, temps[0])]
            for i in (4, 8, 12):
                if i < len(temps):
                    pts.append((i, temps[i]))
            t_str = " → ".join(f"{t:.0f}°C" for _, t in pts)
            delta = pts[-1][1] - pts[0][1]
            if delta < -2:
                trend = "降温 ↓"
            elif delta > 2:
                trend = "升温 ↑"
            else:
                trend = "平稳 →"
            metrics.append(
                {"icon": "🌡️", "label": "气温趋势", "value": f"{t_str}（{trend}）"}
            )
        elif temps:
            metrics.append(
                {"icon": "🌡️", "label": "当前气温", "value": f"{temps[0]:.1f}°C"}
            )

        # 露点
        if dewpoints:
            n = min(len(dewpoints), 6)
            dp_avg = sum(dewpoints[:n]) / n
            metrics.append(
                {"icon": "💧", "label": "露点均值", "value": f"{dp_avg:.1f}°C"}
            )

        # 风速
        if winds:
            n = min(len(winds), 6)
            w_avg = sum(winds[:n]) / n
            if w_avg < 2:
                w_desc = "静风"
            elif w_avg < 12:
                w_desc = "微风"
            elif w_avg < 30:
                w_desc = "和风"
            elif w_avg < 50:
                w_desc = "强风"
            elif w_avg < 75:
                w_desc = "大风"
            else:
                w_desc = "暴风"
            metrics.append(
                {
                    "icon": "🌬️",
                    "label": "风速",
                    "value": f"{w_desc} ({w_avg:.1f} km/h)",
                }
            )

        # 云量
        if clouds:
            n = min(len(clouds), 6)
            c_avg = sum(clouds[:n]) / n
            if c_avg < 20:
                c_desc = "晴 ☀"
            elif c_avg < 50:
                c_desc = "少云 🌤"
            elif c_avg < 80:
                c_desc = "多云 ⛅"
            else:
                c_desc = "阴 ☁"
            metrics.append(
                {"icon": "☁️", "label": "云量", "value": f"{c_desc} ({c_avg:.0f}%)"}
            )

        # 短波辐射
        if rads:
            r_max = max(rads[: min(len(rads), 12)])
            if r_max > 600:
                r_desc = "强日照"
            elif r_max > 300:
                r_desc = "中等日照"
            elif r_max > 50:
                r_desc = "弱日照"
            else:
                r_desc = "无日照"
            metrics.append(
                {
                    "icon": "☀️",
                    "label": "日照峰值",
                    "value": f"{r_desc} ({r_max:.0f} W/m²)",
                }
            )

        # 日最高/最低温
        d_max_list = daily.get("temperature_2m_max", [])
        d_min_list = daily.get("temperature_2m_min", [])
        if d_max_list and isinstance(d_max_list[0], (int, float)):
            d_max = round(float(d_max_list[0]), 1)
            val = f"{d_max}°C"
            if d_min_list and isinstance(d_min_list[0], (int, float)):
                d_min = round(float(d_min_list[0]), 1)
                val = f"{d_min}°C ~ {d_max}°C"
            metrics.append({"icon": "📈", "label": "日高低温", "value": val})

        # 逐3小时预报表
        hourly_rows = []
        for idx in range(0, min(len(times), 24), 3):
            t_label = times[idx][-5:] if idx < len(times) else ""
            row = {"time": t_label}
            if idx < len(temps):
                row["temp"] = round(temps[idx], 1)
            if idx < len(clouds):
                row["cloud"] = round(clouds[idx])
            if idx < len(winds):
                row["wind"] = round(winds[idx], 1)
            if idx < len(rads):
                row["rad"] = round(rads[idx])
            hourly_rows.append(row)

        return {"metrics": metrics, "hourly": hourly_rows} if metrics else {}
    except Exception as e:
        logger.debug(
            f"[MapRoutes] Open-Meteo short-term fetch failed ({lat},{lon}): {e}"
        )
        return {}


def _get_city_info_sync(
    lat: float,
    lon: float,
    icao: str = "",
    city_name: str = "",
    timezone_iana: str = "",
    settlement_source: str = "",
) -> dict | None:
    """
    获取城市"当地时间当日"实测最高气温（与WU /history/daily/ 页面完全一致）。

    Issue10+11 根因修复：
    - WU /history/daily/ HTML 页面被 Akamai WAF 403 拦截（VPS IP 被封）
    - 改用 WU v1 Historical Observations JSON API（纯 API，无 WAF 问题）
    - 该 API 返回逐小时观测记录，取 max(temp) = WU 页面 "Daily Observations" 最高温度

    数据链（popup专用）：
      P1: WU v1 Historical JSON API → max(observations[].temp)
      P2: Open-Meteo 逐小时实测（本地时区），只统计已过去的整点小时
    """
    try:
        import requests as _req
        from src.utils.weather_fetcher import _WU_API_KEYS  # noqa: PLC2701

        # 1. 用城市本地时区确定"当地今日"日期（避免UTC日期跨日）
        try:
            _tz = zoneinfo.ZoneInfo(timezone_iana or "UTC")
        except Exception:
            _tz = zoneinfo.ZoneInfo("UTC")
        _local_now = datetime.now(_tz)
        _local_date = _local_now.strftime("%Y-%m-%d")  # YYYY-MM-DD
        _api_date = _local_now.strftime("%Y%m%d")  # YYYYMMDD for WU API
        _current_temp_c, _current_temp_f, _temp_source = _get_current_temp(
            lat, lon, city_name
        )

        # T234: METAR/TAF/短期趋势 解码（popup 右栏气象卡片）
        _metar_decoded = _fetch_and_decode_metar(icao, timezone_iana) if icao else ""
        _taf_decoded = _fetch_and_decode_taf(icao) if icao else ""
        _short_term_trend = _fetch_short_term_trend(lat, lon) if lat and lon else ""

        # 2. P1: WU v1 Historical Observations JSON API
        #    端点返回逐小时观测记录（与 WU /history/daily/ 页面数据源相同）
        #    取 max(obs[].temp) = 页面上 "Daily Observations" 中的最高温度
        #    T165: 使用 units=e (华氏度) 获取 WU 原生整数，避免 C→F 转换精度丢失
        #          PM 天气市场以 WU 原生华氏度整数结算，popup 必须与结算源一致
        if lat and lon and _WU_API_KEYS:
            for _key in _WU_API_KEYS:
                try:
                    _api_url = (
                        f"https://api.weather.com/v1/geocode/{lat}/{lon}"
                        f"/observations/historical.json"
                        f"?apiKey={_key}"
                        f"&startDate={_api_date}&endDate={_api_date}"
                        f"&units=e"
                    )
                    _resp = _req.get(_api_url, timeout=6)
                    if _resp.status_code == 401:
                        continue  # 尝试下一个 key
                    _resp.raise_for_status()
                    _obs = _resp.json().get("observations", [])
                    _temps = [
                        o["temp"]
                        for o in _obs
                        if isinstance(o.get("temp"), (int, float))
                    ]
                    if _temps:
                        _max_f = round(float(max(_temps)), 1)
                        _max_c = round((_max_f - 32) * 5 / 9, 1)
                        logger.debug(
                            f"[MapRoutes] {city_name or icao} 今日高="
                            f"{_max_f}°F/{_max_c}°C"
                            f" (WU-v1-historical, {len(_obs)}条观测,"
                            f" 当地日期={_local_date})"
                        )
                        return {
                            "today_max_c": _max_c,
                            "today_max_f": _max_f,
                            "current_temp_c": _current_temp_c,
                            "current_temp_f": _current_temp_f,
                            "temp_source": _temp_source,
                            "timezone": timezone_iana or "UTC",
                            "local_time": _local_now.strftime("%Y-%m-%dT%H:%M"),
                            "utc_offset_s": 0,
                            "source": "wu-v1-historical",
                            "metar_decoded": _metar_decoded,
                            "taf_decoded": _taf_decoded,
                            "seven_timer_trend": _short_term_trend,
                        }
                    break  # API 正常响应但无温度，不再尝试下一 key
                except _req.RequestException as _e:
                    logger.debug(
                        f"[MapRoutes] WU v1 API failed ({city_name},"
                        f" key={_key[:8]}...): {_e}"
                    )
                    continue

        # 3. P2: Open-Meteo 逐小时实测（本地时区），只统计已过去的整点
        _api_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m"
            f"&timezone={timezone_iana or 'UTC'}"
            f"&start_date={_local_date}&end_date={_local_date}"
        )
        _resp = _req.get(_api_url, timeout=8)
        _resp.raise_for_status()
        _data = _resp.json()
        _temps = _data.get("hourly", {}).get("temperature_2m", [])
        _times = _data.get("hourly", {}).get("time", [])
        if _temps and _times:
            _cutoff_naive = _local_now.replace(
                minute=0, second=0, microsecond=0, tzinfo=None
            )
            _valid = [
                float(t)
                for ts, t in zip(_times, _temps)
                if t is not None
                and datetime.strptime(ts, "%Y-%m-%dT%H:%M") <= _cutoff_naive
            ]
            if _valid:
                _max_c = round(max(_valid), 1)
                logger.debug(
                    f"[MapRoutes] {city_name or icao} 今日高={_max_c}°C"
                    f" (Open-Meteo-hourly, 当地日期={_local_date})"
                )
                return {
                    "today_max_c": _max_c,
                    "today_max_f": round(_max_c * 9 / 5 + 32, 1),
                    "current_temp_c": _current_temp_c,
                    "current_temp_f": _current_temp_f,
                    "temp_source": _temp_source,
                    "timezone": timezone_iana or "UTC",
                    "local_time": _local_now.strftime("%Y-%m-%dT%H:%M"),
                    "utc_offset_s": 0,
                    "source": "open-meteo-hourly",
                    "metar_decoded": _metar_decoded,
                    "taf_decoded": _taf_decoded,
                    "seven_timer_trend": _short_term_trend,
                }
    except Exception as e:
        logger.debug(f"[MapRoutes] cityinfo fetch failed ({city_name or icao}): {e}")
    return None


def _get_timezone_sync(lat: float, lon: float) -> str:
    """
    通过 Open-Meteo 获取任意经纬度的 IANA 时区名（如 'Asia/Tokyo'）。
    用于自定义/体育标识的 popup 实时时钟。进程级永久缓存。
    """
    _key = f"{lat:.3f},{lon:.3f}"
    if _key in _TIMEZONE_CACHE:
        return _TIMEZONE_CACHE[_key]
    try:
        import requests as _req

        _resp = _req.get(
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}&timezone=auto&forecast_days=1&daily=temperature_2m_max",
            timeout=6,
        )
        _tz = _resp.json().get("timezone", "UTC") if _resp.status_code == 200 else "UTC"
    except Exception:
        _tz = "UTC"
    _TIMEZONE_CACHE[_key] = _tz
    return _tz


# ── API 端点 ──────────────────────────────────────────────────────────


@router.get("/api/map/markers")
async def get_map_markers():
    """
    返回所有 Markers：
    - 已投注城市 Marker（自动生成，来自 DB weather bets）
    - 手动添加的 Marker（来自 JSON 文件）
    合并时：已投注城市优先，手动 Marker 不重复生成相同城市的坐标点。
    30秒缓存避免频繁DB查询和regex解析。
    """
    now = time.time()
    if (
        _MARKERS_CACHE["data"] is not None
        and (now - _MARKERS_CACHE["ts"]) < _MARKERS_CACHE_TTL
    ):
        return _MARKERS_CACHE["data"]

    weather_cities = _get_weather_cities()
    auto_markers = _build_auto_markers(weather_cities)
    manual_markers = _load_markers()

    # 已投注城市 key set（防止手动 marker 重复覆盖已投注城市）
    auto_city_keys = {m["city"].lower() for m in auto_markers}

    # 天气城市 timezone 查找表（用于回填手动 marker 缺失的 timezone）
    wc_tz = {c["name"].lower(): c.get("timezone", "UTC") for c in weather_cities}

    # 过滤：手动 marker 中与已投注城市同名的，合并其 events 而非重复展示
    merged_manual = []
    for mm in manual_markers:
        mc_key = mm.get("city", "").lower()
        # 回填 timezone（若未存储）：优先从天气城市配置查，找不到保留 null 供前端异步查询
        if not mm.get("timezone"):
            mm["timezone"] = wc_tz.get(mc_key) or None
        if mc_key in auto_city_keys:
            # 找到对应的 auto_marker，追加手动事件
            for am in auto_markers:
                if am["city"].lower() == mc_key:
                    # 合并：追加手动 events（避免重复）
                    existing_ids = {e["id"] for e in am["events"]}
                    for ev in mm.get("events", []):
                        if ev["id"] not in existing_ids:
                            am["events"].append(ev)
        else:
            merged_manual.append(mm)

    all_markers = auto_markers + merged_manual
    result = {"status": "ok", "count": len(all_markers), "markers": all_markers}
    _MARKERS_CACHE["data"] = result
    _MARKERS_CACHE["ts"] = now
    return result


@router.post("/api/map/markers")
async def add_map_marker(body: dict, request: Request):
    """
    添加新 Marker（手动）。
    body: {city, lat, lon, category, events: [{title, description, is_bet}]}
    需要 admin / super_admin 权限（T901）。
    """
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    city = body.get("city", "").strip()
    lat = body.get("lat")
    lon = body.get("lon")
    category = body.get("category", "weather")
    events_raw = body.get("events", [])

    if not city:
        return JSONResponse({"error": "city required"}, status_code=400)
    if lat is None or lon is None:
        return JSONResponse({"error": "lat/lon required"}, status_code=400)

    # 获取时区（先查天气城市配置，再通过 Open-Meteo 查）
    weather_cities = _get_weather_cities()
    wc_tz = {c["name"].lower(): c.get("timezone") for c in weather_cities}
    tz_iana = wc_tz.get(city.lower())
    if not tz_iana:
        # 异步线程查询，避免阻塞
        tz_iana = await asyncio.to_thread(_get_timezone_sync, float(lat), float(lon))

    markers = _load_markers()

    # 检查是否已有该城市的手动 Marker
    existing = next(
        (m for m in markers if m.get("city", "").lower() == city.lower()), None
    )

    new_events = [
        {
            "id": f"ev_{uuid.uuid4().hex[:8]}",
            "title": e.get("title", city),
            "description": e.get("description", ""),
            "bet_id": e.get("bet_id"),
            "is_bet": bool(e.get("is_bet", False)),
        }
        for e in events_raw
    ]

    if existing:
        # 同城市追加新事件（顺便补写 timezone）
        if not existing.get("timezone") and tz_iana:
            existing["timezone"] = tz_iana
        existing_ids = {e["id"] for e in existing.get("events", [])}
        for ev in new_events:
            if ev["id"] not in existing_ids:
                existing.setdefault("events", []).append(ev)
        _save_markers(markers)
        _MARKERS_CACHE["data"] = None  # 使缓存失效
        return {"status": "ok", "action": "merged", "marker": existing}
    else:
        # 全新 Marker
        marker = {
            "id": f"manual_{uuid.uuid4().hex[:8]}",
            "city": city,
            "lat": float(lat),
            "lon": float(lon),
            "category": category,
            "timezone": tz_iana,
            "is_auto": False,
            "events": new_events,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        markers.append(marker)
        _save_markers(markers)
        _MARKERS_CACHE["data"] = None  # 使缓存失效
        return {"status": "ok", "action": "created", "marker": marker}


@router.delete("/api/map/markers/{marker_id}")
async def delete_map_marker(marker_id: str, request: Request):
    """删除手动 Marker（仅允许删除非 auto 的手动 Marker）。需要 admin 权限（T901）。"""
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    markers = _load_markers()
    before = len(markers)
    markers = [m for m in markers if m.get("id") != marker_id]
    if len(markers) == before:
        return JSONResponse(
            {"error": "marker not found or is auto-generated"}, status_code=404
        )
    _save_markers(markers)
    _MARKERS_CACHE["data"] = None  # 使缓存失效
    return {"status": "ok", "deleted": marker_id}


@router.get("/api/map/timezone")
async def get_marker_timezone(lat: float = Query(...), lon: float = Query(...)):
    """根据经纬度返回 IANA 时区名（用于 sports/custom marker popup 实时时钟）。"""
    tz = await asyncio.to_thread(_get_timezone_sync, lat, lon)
    return {"status": "ok", "timezone": tz, "lat": lat, "lon": lon}


@router.delete("/api/map/markers/{marker_id}/events/{event_id}")
async def delete_map_event(marker_id: str, event_id: str, request: Request):
    """删除手动 Marker 中的某个事件（仅允许删除非 is_bet 事件）。

    需要 admin / super_admin 权限（T901）。

    Bug fix: 当手动事件被合并到 auto marker 后，前端的 marker_id 是 auto_xxx，
    但手动事件实际存储在 manual marker 的 JSON 文件中。
    因此当 marker_id 未直接找到时，需遍历所有手动 marker 搜索该 event_id。
    """
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    markers = _load_markers()
    marker = next((m for m in markers if m.get("id") == marker_id), None)

    # 如果按 marker_id 未找到（可能是 auto marker ID），遍历所有手动 marker 查找事件
    if not marker:
        for mm in markers:
            if any(
                e.get("id") == event_id and not e.get("is_bet")
                for e in mm.get("events", [])
            ):
                marker = mm
                break

    if not marker:
        return JSONResponse({"error": "marker not found"}, status_code=404)
    before = len(marker.get("events", []))
    marker["events"] = [
        e
        for e in marker.get("events", [])
        if not (e.get("id") == event_id and not e.get("is_bet"))
    ]
    if len(marker.get("events", [])) == before:
        return JSONResponse(
            {"error": "event not found or is a bet event"}, status_code=404
        )
    _save_markers(markers)
    _MARKERS_CACHE["data"] = None  # 使缓存失效
    return {"status": "ok", "deleted_event": event_id}


@router.get("/api/map/city-bets/{city_name}")
async def get_city_bets(city_name: str):
    """返回指定城市的所有天气投注列表。"""
    weather_cities = _get_weather_cities()

    try:
        with db_connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT bet_id, question, outcome, market_price, exit_price,
                       ts_entered, condition_id, market_slug, exit_type
                FROM paper_bets
                WHERE (category = 'weather'
                       OR (category = 'unknown' AND (
                           strategy_tag LIKE 'weather_%'
                           OR question LIKE '%temperature%'
                           OR question LIKE '%celsius%'
                           OR question LIKE '%fahrenheit%'
                       )))
                  AND resolved_at IS NULL
                ORDER BY ts_entered DESC
                """
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning(f"[MapRoutes] city bets query failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

    bets = []
    for row in rows:
        (
            bet_id,
            question,
            outcome,
            market_price,
            exit_price,
            ts_entered,
            condition_id,
            market_slug,
            exit_type,
        ) = row
        extracted = _extract_city_from_question(str(question or ""), weather_cities)
        if extracted and extracted.lower() == city_name.lower():
            bets.append(
                {
                    "id": bet_id,
                    "question": question,
                    "outcome": outcome,
                    "market_price": market_price,
                    "exit_price": exit_price,
                    "ts_entered": ts_entered,
                    "condition_id": condition_id,
                    "market_slug": market_slug,
                    "exit_type": exit_type,
                }
            )

    return {"status": "ok", "city": city_name, "count": len(bets), "bets": bets}


@router.get("/api/map/cityinfo/{city_name}")
async def get_city_info(city_name: str):
    """获取城市今日实测最高气温（WU Daily Observations，与结算源一致）和 IANA 时区名（内存缓存30min）。"""
    now = time.time()
    cache_key = city_name.lower()

    cached = _CITYINFO_CACHE.get(cache_key)
    if cached and (now - cached["ts"]) < _CITYINFO_CACHE_TTL:
        return {"status": "ok", "city": city_name, "cached": True, **cached["data"]}

    weather_cities = _get_weather_cities()
    wc_by_name = {c["name"].lower(): c for c in weather_cities}
    city_cfg = wc_by_name.get(cache_key, {})
    lat = city_cfg.get("lat")
    lon = city_cfg.get("lon")

    if lat is None or lon is None:
        return JSONResponse(
            {
                "status": "no_data",
                "city": city_name,
                "reason": "city not in weather_cities",
            },
            status_code=404,
        )

    icao = city_cfg.get("icao", "")
    timezone_iana = city_cfg.get("timezone", "UTC")
    settlement_source = city_cfg.get("settlement_source", "wunderground")
    settlement_url = city_cfg.get("settlement_url", "")

    # peak_window: 城市级覆盖 → WeatherEventMap 专用配置 → 默认值
    _pw_start = int(
        city_cfg.get("peak_window_start")
        or get("weather_event_map.peak_window_start")
        or 10
    )
    _pw_end = int(
        city_cfg.get("peak_window_end")
        or get("weather_event_map.peak_window_end")
        or 17
    )

    data = await asyncio.to_thread(
        _get_city_info_sync, lat, lon, icao, city_name, timezone_iana, settlement_source
    )
    if data:
        data["settlement_url"] = settlement_url
        # 计算 is_in_peak_window（基于城市本地时间 local_time: "YYYY-MM-DDTHH:MM"）
        _local_hour = -1
        try:
            _lt = data.get("local_time", "")
            if len(_lt) >= 13:
                _local_hour = int(_lt[11:13])
        except Exception:
            pass
        data["is_in_peak_window"] = (
            _pw_start <= _local_hour < _pw_end if _local_hour >= 0 else False
        )
        data["peak_window_start"] = _pw_start
        data["peak_window_end"] = _pw_end
        _CITYINFO_CACHE[cache_key] = {"data": data, "ts": now}
        return {"status": "ok", "city": city_name, **data}
    return {
        "status": "unavailable",
        "city": city_name,
        "today_max_c": None,
        "timezone": None,
        "settlement_url": settlement_url,
        "is_in_peak_window": False,
    }


@router.get("/api/map/weather-cities")
async def get_weather_cities_for_map():
    """返回所有天气城市列表（用于地图 Marker 下拉选项）。"""
    cities = _get_weather_cities()
    return {
        "status": "ok",
        "count": len(cities),
        "cities": [
            {
                "name": c.get("name"),
                "slug": c.get("slug"),
                "lat": c.get("lat"),
                "lon": c.get("lon"),
                "tz": c.get("tz"),
                "enabled": c.get("enabled", True),
            }
            for c in cities
        ],
    }
