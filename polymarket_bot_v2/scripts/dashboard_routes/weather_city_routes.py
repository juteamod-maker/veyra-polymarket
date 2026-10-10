"""
dashboard_routes/weather_city_routes.py — 天气城市管理路由
T276: 仪表盘策略TAB城市可视化管理（增删改查）
"""

import json
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import (
    CONFIG_PATH,
    backup_config,
    get_request_identity,
    logger,
)

router = APIRouter()

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _read_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _validate_city(data: dict) -> list[str]:
    """验证城市数据字段，返回错误列表。"""
    errors = []
    if not data.get("name") or not isinstance(data["name"], str):
        errors.append("name: 必填，字符串")
    slug = data.get("slug", "")
    if not slug or not isinstance(slug, str):
        errors.append("slug: 必填，字符串")
    elif not _SLUG_RE.match(slug):
        errors.append("slug: 只允许小写字母、数字和连字符")
    lat = data.get("lat")
    if lat is None or not isinstance(lat, (int, float)):
        errors.append("lat: 必填，数值")
    elif not -90 <= lat <= 90:
        errors.append("lat: 范围 -90 到 90")
    lon = data.get("lon")
    if lon is None or not isinstance(lon, (int, float)):
        errors.append("lon: 必填，数值")
    elif not -180 <= lon <= 180:
        errors.append("lon: 范围 -180 到 180")
    if data.get("icao") and not isinstance(data["icao"], str):
        errors.append("icao: 必须是字符串")
    if data.get("timezone") and not isinstance(data["timezone"], str):
        errors.append("timezone: 必须是字符串")
    if data.get("utc_offset") is not None:
        off = data["utc_offset"]
        if not isinstance(off, (int, float)):
            errors.append("utc_offset: 必须是数值")
        elif not -12 <= off <= 14:
            errors.append("utc_offset: 范围 -12 到 14")
    src = data.get("settlement_source", "wunderground")
    if src not in ("wunderground", "noaa", "hko"):
        errors.append("settlement_source: 必须是 wunderground / noaa / hko")
    return errors


@router.get("/api/weather-cities")
async def list_weather_cities():
    """列出所有天气城市配置。"""
    try:
        cfg = _read_config()
        wc = cfg.get("weather_cities", {})
        cities = wc.get("cities", [])
        return {"status": "ok", "count": len(cities), "cities": cities}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/weather-priority-tz")
async def get_weather_priority_tz():
    """返回所有启用天气城市的时区，供 Dashboard 时钟下拉框使用。"""
    try:
        cfg = _read_config()
        cities = cfg.get("weather_cities", {}).get("cities", [])
        result = []
        for c in cities:
            name = c.get("name", "")
            tz = c.get("timezone", "UTC")
            enabled = c.get("enabled", True)
            if name and enabled:
                result.append({"name": name, "timezone": tz})
        # 按城市名排序便于查找
        result.sort(key=lambda x: x["name"])
        return {"status": "ok", "cities": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/weather-cities")
async def add_weather_city(request: Request):
    """新增一个天气城市。需要 admin / super_admin 权限。"""
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    errors = _validate_city(body)
    if errors:
        return JSONResponse(
            {"error": "validation failed", "details": errors}, status_code=400
        )

    try:
        backup_config()
        cfg = _read_config()
        if "weather_cities" not in cfg:
            cfg["weather_cities"] = {"_doc_": "Polymarket天气城市配置", "cities": []}
        cities = cfg["weather_cities"].setdefault("cities", [])

        slug = body["slug"].lower().strip()
        for c in cities:
            if c.get("slug") == slug:
                return JSONResponse({"error": f"slug '{slug}' 已存在"}, status_code=409)

        new_city = {
            "name": body["name"].strip(),
            "slug": slug,
            "lat": float(body["lat"]),
            "lon": float(body["lon"]),
            "icao": body.get("icao", "").strip().upper(),
            "timezone": body.get("timezone", "").strip(),
            "utc_offset": body.get("utc_offset", 0),
            "enabled": body.get("enabled", True),
            "settlement_source": body.get("settlement_source", "wunderground"),
            "settlement_url": body.get("settlement_url", "").strip(),
            "noaa_site": body.get("noaa_site", "").strip().upper(),
        }
        cities.append(new_city)
        _write_config(cfg)

        logger.info(f"[WeatherCityAPI] 新增城市: {new_city['name']} ({slug})")
        return {"status": "ok", "city": new_city, "total": len(cities)}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/api/weather-cities/{slug}")
async def update_weather_city(slug: str, request: Request):
    """更新指定 slug 的天气城市。需要 admin / super_admin 权限。"""
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    errors = _validate_city(body)
    if errors:
        return JSONResponse(
            {"error": "validation failed", "details": errors}, status_code=400
        )

    try:
        backup_config()
        cfg = _read_config()
        cities = cfg.get("weather_cities", {}).get("cities", [])

        target = None
        target_idx = -1
        for i, c in enumerate(cities):
            if c.get("slug") == slug:
                target = c
                target_idx = i
                break

        if target is None:
            return JSONResponse({"error": f"城市 '{slug}' 不存在"}, status_code=404)

        new_slug = body.get("slug", slug).lower().strip()
        if new_slug != slug:
            for c in cities:
                if c.get("slug") == new_slug:
                    return JSONResponse(
                        {"error": f"新 slug '{new_slug}' 已被使用"}, status_code=409
                    )

        updated = {
            "name": body["name"].strip(),
            "slug": new_slug,
            "lat": float(body["lat"]),
            "lon": float(body["lon"]),
            "icao": body.get("icao", "").strip().upper(),
            "timezone": body.get("timezone", "").strip(),
            "utc_offset": body.get("utc_offset", target.get("utc_offset", 0)),
            "enabled": body.get("enabled", True),
            "settlement_source": body.get(
                "settlement_source", target.get("settlement_source", "wunderground")
            ),
            "settlement_url": body.get(
                "settlement_url", target.get("settlement_url", "")
            ).strip(),
            "noaa_site": body.get("noaa_site", target.get("noaa_site", ""))
            .strip()
            .upper(),
        }
        cities[target_idx] = updated
        _write_config(cfg)

        logger.info(f"[WeatherCityAPI] 更新城市: {slug} → {updated}")
        return {"status": "ok", "city": updated}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/api/weather-cities/{slug}")
async def delete_weather_city(slug: str, request: Request):
    """删除指定 slug 的天气城市。需要 admin / super_admin 权限。"""
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    try:
        backup_config()
        cfg = _read_config()
        cities = cfg.get("weather_cities", {}).get("cities", [])

        original_len = len(cities)
        cfg["weather_cities"]["cities"] = [c for c in cities if c.get("slug") != slug]
        new_len = len(cfg["weather_cities"]["cities"])

        if new_len == original_len:
            return JSONResponse({"error": f"城市 '{slug}' 不存在"}, status_code=404)

        _write_config(cfg)
        logger.info(f"[WeatherCityAPI] 删除城市: {slug}")
        return {"status": "ok", "deleted": slug, "remaining": new_len}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.patch("/api/weather-cities/{slug}/toggle")
async def toggle_weather_city(slug: str):
    """切换城市的 enabled 状态。"""
    try:
        backup_config()
        cfg = _read_config()
        cities = cfg.get("weather_cities", {}).get("cities", [])

        for c in cities:
            if c.get("slug") == slug:
                c["enabled"] = not c.get("enabled", True)
                _write_config(cfg)
                logger.info(
                    f"[WeatherCityAPI] 切换城市 {slug} → enabled={c['enabled']}"
                )
                return {"status": "ok", "slug": slug, "enabled": c["enabled"]}

        return JSONResponse({"error": f"城市 '{slug}' 不存在"}, status_code=404)

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
