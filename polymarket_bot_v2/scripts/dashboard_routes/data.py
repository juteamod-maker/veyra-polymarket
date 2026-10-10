"""
dashboard_routes/data.py — 数据查询路由
包含: status, summary, portfolio, bets, tracks, logs, evolution, calibration, liquidity
"""

import asyncio
import json
import logging
import math
import re
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import (
    DB_PATH,
    LOG_DIR,
    ROOT,
    _get_system_info,
    _rows_to_dicts,
    db_connect,
    get,
    get_request_identity,
)
from src.utils.balance_provider import get_portfolio as bp_get_portfolio
from src.utils.pm_http import pm_session  # T510 P0-1: unified UA + connection pool

router = APIRouter()
logger = logging.getLogger("web_dashboard")


# ── T219-K: 通用端点 TTL 缓存 ──
_endpoint_cache: dict[str, tuple[float, object]] = {}  # {key: (ts, result)}


def _cache_get(key: str, ttl: float):
    """返回缓存值或 None。"""
    c = _endpoint_cache.get(key)
    if c and (time.time() - c[0]) < ttl:
        return c[1]
    return None


def _cache_set(key: str, val):
    _endpoint_cache[key] = (time.time(), val)


def _safe_ts(val) -> float:
    """将任意格式的时间戳安全转为 float（防御 DB 中混合类型）。"""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val).timestamp()
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def _infer_order_type(bet: dict) -> str:
    """为历史记录推断下单方式，避免旧数据在 Dashboard 中显示为“—”。"""
    order_type = str(bet.get("order_type") or "").strip().lower()
    if order_type in {"maker", "taker", "simulated"}:
        return order_type

    source = str(bet.get("source") or "").lower()
    if source in {"paper", "simulated"} or "dry" in source:
        return "simulated"

    per_cat = get("order_execution.buy_mode.per_category") or {}
    default_mode = (get("order_execution.buy_mode") or {}).get("default", "taker")
    inferred = str(per_cat.get(bet.get("category") or "", default_mode)).lower()
    return "maker" if inferred == "maker" else "taker"


def _fetch_live_meteo_parts(question: str) -> list[str]:
    """从 Open-Meteo API 获取实时 GFS/ECMWF 预报数据（strategy_params 缺失时的 fallback）。

    带 TTL=600s 内存缓存，避免对每次 Dashboard 请求都重复调用外部 API。
    """
    _ck = question.strip().lower()
    _now = time.time()
    _cached_entry = _live_meteo_cache.get(_ck)
    if _cached_entry and (_now - _cached_entry[0]) < _LIVE_METEO_CACHE_TTL:
        return list(_cached_entry[1])

    parts: list[str] = []
    try:
        # 从 question 提取城市和阈值
        m = re.match(
            r"will the (highest|lowest) temperature in (.+?) be (\d+)°([cf])\s*(or higher|or lower)?\s*on ([a-z]+)\s+(\d+)",
            question.lower().strip().rstrip("?").strip(),
        )
        if not m:
            return parts
        city_name = m.group(2).strip().title()
        threshold = int(m.group(3))
        month_name = m.group(6)
        day = int(m.group(7))
        unit = m.group(4).lower()

        # 查找城市配置
        cfg = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        cities = cfg.get("weather_cities", {}).get("cities", [])
        city_cfg = None
        for c in cities:
            if c.get("name", "").lower() == city_name.lower():
                city_cfg = c
                break
        if not city_cfg:
            return parts

        lat, lon = city_cfg["lat"], city_cfg["lon"]
        tz_str = city_cfg.get("timezone", "UTC")
        month_map = {
            "january": 1,
            "february": 2,
            "march": 3,
            "april": 4,
            "may": 5,
            "june": 6,
            "july": 7,
            "august": 8,
            "september": 9,
            "october": 10,
            "november": 11,
            "december": 12,
        }
        mon = month_map.get(month_name, 4)
        year = datetime.now(timezone.utc).year
        target_date = f"{year:04d}-{mon:02d}-{day:02d}"

        # GFS forecast via Open-Meteo
        gfs_url = (
            f"https://api.open-meteo.com/v1/gfs?"
            f"latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&timezone={tz_str}"
            f"&start_date={target_date}&end_date={target_date}"
        )
        gfs_resp = requests.get(gfs_url, timeout=8)
        gfs_max = None
        if gfs_resp.status_code == 200:
            daily = gfs_resp.json().get("daily", {})
            temps = daily.get("temperature_2m_max", [])
            if temps and temps[0] is not None:
                gfs_max = round(float(temps[0]), 1)
                parts.append(f"GFS峰值={gfs_max}°C")

        # ECMWF forecast via Open-Meteo
        ecmwf_url = (
            f"https://api.open-meteo.com/v1/ecmwf?"
            f"latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&timezone={tz_str}"
            f"&start_date={target_date}&end_date={target_date}"
        )
        ecmwf_resp = requests.get(ecmwf_url, timeout=8)
        ecmwf_max = None
        if ecmwf_resp.status_code == 200:
            daily = ecmwf_resp.json().get("daily", {})
            temps = daily.get("temperature_2m_max", [])
            if temps and temps[0] is not None:
                ecmwf_max = round(float(temps[0]), 1)
                parts.append(f"ECMWF峰值={ecmwf_max}°C")

        if unit == "f":
            parts.append(f"阈值={threshold}°F")
        else:
            parts.append(f"阈值={threshold}°C")

        if gfs_max is not None and ecmwf_max is not None:
            div = abs(gfs_max - ecmwf_max)
            parts.append(f"分歧={div:.1f}°C")

        days_ahead = (
            datetime(year, mon, day, tzinfo=timezone.utc) - datetime.now(timezone.utc)
        ).days
        parts.append(f"D+{max(0, days_ahead)}")
        parts.append("来源=live-forecast(详情页实时)")
    except Exception as e:
        logger.debug(f"[_fetch_live_meteo_parts] error: {e}")
    _live_meteo_cache[_ck] = (_now, list(parts))
    return parts


_live_meteo_cache: dict[str, tuple[float, list]] = {}
_LIVE_METEO_CACHE_TTL = 600  # 10 分钟（天气预报变化慢，避免对每个请求都调 Open-Meteo）

_market_enddate_cache: dict[str, tuple[float, str]] = {}
_MARKET_ENDDATE_TTL = 3600  # 1小时（endDate极少变动）


def _fetch_authoritative_market_end_date(market_id: str) -> str:
    """从 Gamma API 获取权威 endDate，用于修正旧记录/弱回填记录的展示时间。"""
    mid = str(market_id or "").strip()
    if not mid:
        return ""

    now = time.time()
    cached = _market_enddate_cache.get(mid)
    if cached and (now - cached[0]) < _MARKET_ENDDATE_TTL:
        return cached[1]

    try:
        resp = pm_session().get(
            f"https://gamma-api.polymarket.com/markets/{mid}", timeout=8
        )
        if resp.status_code == 200:
            payload = resp.json()
            if isinstance(payload, list):
                payload = payload[0] if payload else {}
            end_date = str(
                (payload or {}).get("endDate")
                or (payload or {}).get("endDateIso")
                or ""
            ).strip()
            _market_enddate_cache[mid] = (now, end_date)
            return end_date
    except Exception as e:
        logger.debug(f"[_fetch_authoritative_market_end_date] {mid}: {e}")

    _market_enddate_cache[mid] = (now, "")
    return ""


def _refresh_market_end_date_for_display(bet: dict) -> None:
    """对旧天气记录做只读修正：优先用 Gamma 权威 endDate 替换空值/日期值/23:59:59Z 猜测值。"""
    end = str(bet.get("market_end_date") or "").strip()
    is_weather = (bet.get("category") or "") == "weather"
    needs_refresh = (
        not end
        or bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", end))
        or (is_weather and end.endswith("T23:59:59Z"))
    )
    if not needs_refresh:
        return

    end_date = _fetch_authoritative_market_end_date(str(bet.get("market_id") or ""))
    if end_date:
        bet["market_end_date"] = end_date


def _batch_refresh_market_end_dates(bets: list[dict]) -> None:
    """T188: 批量并发刷新 market_end_date，替代逐个同步 HTTP 调用。
    T193: 跳过已结算投注（end_date不会再变），获取后持久化到DB。"""
    needs: list[tuple[int, str]] = []
    for i, bet in enumerate(bets):
        # T193: 已结算投注的 end_date 不会改变，跳过刷新
        if bet.get("resolved_outcome") is not None:
            continue
        end = str(bet.get("market_end_date") or "").strip()
        is_weather = (bet.get("category") or "") == "weather"
        if (
            not end
            or bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", end))
            or (is_weather and end.endswith("T23:59:59Z"))
        ):
            mid = str(bet.get("market_id") or "").strip()
            if mid:
                needs.append((i, mid))
    if not needs:
        return

    # 先从缓存中快速满足已知的
    now = time.time()
    pending: list[tuple[int, str]] = []
    for idx, mid in needs:
        cached = _market_enddate_cache.get(mid)
        if cached and (now - cached[0]) < _MARKET_ENDDATE_TTL:
            if cached[1]:
                bets[idx]["market_end_date"] = cached[1]
        else:
            pending.append((idx, mid))
    if not pending:
        return

    # 去重 market_id（同一个 market_id 可能对应多条 bet）
    unique_mids = list({mid for _, mid in pending})
    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(len(unique_mids), 8)) as pool:
        futures = {
            pool.submit(_fetch_authoritative_market_end_date, mid): mid
            for mid in unique_mids
        }
        for future in as_completed(futures, timeout=12):
            mid = futures[future]
            try:
                results[mid] = future.result()
            except Exception:
                results[mid] = ""

    # T193: 写入bet对象 + 持久化到DB（避免下次再请求API）
    persist_updates: list[tuple[str, str]] = []  # (market_id, end_date)
    for idx, mid in pending:
        end_date = results.get(mid, "")
        if end_date:
            bets[idx]["market_end_date"] = end_date
            persist_updates.append((mid, end_date))
    if persist_updates:
        try:
            with db_connect() as conn:
                for mid, ed in persist_updates:
                    conn.execute(
                        "UPDATE paper_bets SET market_end_date = ? WHERE market_id = ? AND (market_end_date IS NULL OR market_end_date = '' OR LENGTH(market_end_date) = 10)",
                        (ed, mid),
                    )
        except Exception:
            pass  # 持久化失败不影响当前请求


def _collect_weather_meteo_text(bet: dict) -> str:
    """聚合天气详情文本：优先 strategy_params，其次 weather_context，最后实时预报 fallback。"""
    parts: list[str] = []

    def _add(part: str) -> None:
        if part and part not in parts:
            parts.append(part)

    sp_raw = bet.get("strategy_params")
    sp = {}
    if isinstance(sp_raw, dict):
        sp = sp_raw
    elif isinstance(sp_raw, str) and sp_raw.strip():
        try:
            sp = json.loads(sp_raw)
        except (json.JSONDecodeError, TypeError):
            sp = {}

    if sp:
        gfs_mean = sp.get("gfs_mean_c")
        if gfs_mean is not None:
            _add(f"GFS峰值={gfs_mean}°C")
        ecmwf_mean = sp.get("ecmwf_mean_c")
        if ecmwf_mean is not None:
            _add(f"ECMWF峰值={ecmwf_mean}°C")
        threshold = sp.get("threshold_c")
        if threshold is not None:
            _add(f"阈值={threshold}°C")
        gfs_p = sp.get("gfs_probability")
        if gfs_p is not None:
            _add(f"GFS P(YES)={gfs_p:.3f}")
        ecmwf_p = sp.get("ecmwf_probability")
        if ecmwf_p is not None:
            _add(f"ECMWF P(YES)={ecmwf_p:.3f}")
        gfs_std = sp.get("gfs_std_c")
        if gfs_std is not None:
            _add(f"σ={gfs_std}°C")
        div = sp.get("divergence")
        if div is not None:
            _add(f"分歧={div:.1%}")
        days = sp.get("days_ahead")
        if days is not None:
            _add(f"D+{days}")
        gfs_n = sp.get("gfs_members")
        ecmwf_n = sp.get("ecmwf_members")
        if gfs_n is not None or ecmwf_n is not None:
            _add(f"成员数 GFS={gfs_n or '?'}/ECMWF={ecmwf_n or '?'}")
        metar_c = sp.get("metar_temp_c")
        if metar_c is not None:
            _add(f"METAR实测={metar_c}°C")
        src = sp.get("source")
        if src:
            _add(f"来源={src}")
        conf_level = sp.get("confidence_level")
        if conf_level:
            _add(f"置信={conf_level}")

    q = str(bet.get("question") or "")
    # 只有当 strategy_params 里没有 GFS/ECMWF 预报峰值数据时才调用实时 API（fallback）。
    # 有 strategy_params 的新投注直接用已持久化的预报数据，避免每次 Dashboard 请求都调外部 API。
    _has_sp_forecast = bool(
        sp and (sp.get("gfs_mean_c") is not None or sp.get("ecmwf_mean_c") is not None)
    )
    if q and not _has_sp_forecast:
        for part in _fetch_live_meteo_parts(q):
            _add(part)

    if parts:
        return f"🌤️ 气象数据: {', '.join(parts)}"

    wctx = str(bet.get("weather_context") or "").strip()
    if wctx:
        return f"🌤️ 气象数据: {wctx}"

    return ""


def _build_display_reasoning(bet: dict) -> str:
    """为旧投注记录生成可读的推理说明，确保详情页始终有解释文本。"""
    is_weather = (bet.get("category") or "") == "weather"
    existing = str(bet.get("ai_reasoning") or "").strip()
    if existing:
        if is_weather and "🌤️ 气象数据:" not in existing:
            meteo_line = _collect_weather_meteo_text(bet)
            if meteo_line:
                return f"{existing}\n{meteo_line}"
        return existing

    outcome = str(bet.get("outcome") or "").upper()
    ai_prob = float(bet.get("ai_probability") or 0.5)
    market_price = float(bet.get("market_price") or 0.0)
    confidence = float(bet.get("confidence") or 0.0)
    ev = float(bet.get("ev") or 0.0)
    tag = str(
        bet.get("strategy_tag")
        or bet.get("bet_bucket")
        or bet.get("category")
        or "未分类"
    )

    side_prob = (
        ai_prob if outcome == "YES" else (1.0 - ai_prob if outcome == "NO" else ai_prob)
    )

    # 天气 NO 策略增强推理
    is_weather_no = is_weather and outcome == "NO"
    stag = str(bet.get("strategy_tag") or "")
    _weather_prob_missing = (
        is_weather
        and abs(ai_prob - 0.5) < 1e-9
        and not str(bet.get("weather_context") or "").strip()
        and not bet.get("strategy_params")
    )

    lines = []

    if is_weather_no:
        # 天气双NO策略有专门的推理逻辑
        _tag_friendly = {
            "weather_double_no_right": "右翼NO（高于预测峰值）",
            "weather_double_no_left": "左翼NO（低于预测峰值）",
            "weather_double_no_same": "峰值NO（命中概率最高但仍<50%）",
            "weather_right_tail_right": "右尾做空（高于预测峰值）",
            "weather_right_tail_left": "右尾做空-左翼（低于预测峰值）",
            "weather_right_tail_same": "右尾做空-峰值（命中概率最高但仍<50%）",
            "weather_yes_ambush": "YES埋伏（模型高置信命中）",
        }
        friendly = _tag_friendly.get(stag, tag)
        lines.append(f"📐 策略: {friendly}")
        q = str(bet.get("question") or "")
        lines.append(f"📋 市场: {q}")
        if _weather_prob_missing:
            lines.append("模型概率: 原始气象概率未持久化，已改为显示实时气象明细供复核")
        else:
            lines.append(f"模型概率: P(YES)={ai_prob:.3f} → P(NO)={1.0-ai_prob:.3f}")
        if market_price > 0:
            lines.append(
                f"NO入场价: {market_price:.3f} (买入NO花费 {market_price*100:.1f}¢/份)"
            )
            lines.append(
                f"市场隐含P(NO)={market_price:.3f} | 市场隐含P(YES)={1.0-market_price:.3f}"
            )
            if not _weather_prob_missing:
                edge = side_prob - market_price
                lines.append(f"Edge: {edge:+.3f} = 模型P(NO) - 市场P(NO)")
        lines.append(f"记录EV={ev:+.3f} | confidence={confidence:.2f}")
        meteo_line = _collect_weather_meteo_text(bet)
        if meteo_line:
            lines.append(meteo_line)
    else:
        lines.append(f"策略标签: {tag}")
        lines.append(f"模型概率: P(YES)={ai_prob:.3f}")
        lines.append(f"当前方向: {outcome} | 模型侧概率={side_prob:.3f}")
        if market_price > 0:
            market_side = market_price
            lines.append(
                f"市场价格: {market_price:.3f} | 市场隐含P({outcome})={market_side:.3f}"
            )
        lines.append(f"EV={ev:+.3f} | confidence={confidence:.2f}")
        if is_weather:
            if _weather_prob_missing:
                lines.append("模型概率: 原始气象概率未持久化，已补充实时气象明细")
            meteo_line = _collect_weather_meteo_text(bet)
            if meteo_line:
                lines.append(meteo_line)

    return "\n".join(lines)


def _generate_weather_slugs(question: str) -> tuple[str, str]:
    """从天气市场问题文本生成 event_slug 和 market_slug。

    格式示例:
      Q: "Will the highest temperature in Seoul be 16°C on April 11?"
      → event_slug: "highest-temperature-in-seoul-on-april-11-2026"
      → market_slug: "highest-temperature-in-seoul-on-april-11-2026-16c"
    """
    if not question:
        return "", ""
    q = question.lower().strip().rstrip("?").strip()
    # 匹配 "will the highest/lowest temperature in {city} be {temp}°C/°F ... on {month} {day}"
    m = re.match(
        r"will the (highest|lowest) temperature in (.+?) be (\d+)°([cf])\s*(or higher|or lower)?\s*on ([a-z]+)\s+(\d+)",
        q,
    )
    if not m:
        return "", ""
    var = m.group(1)  # highest/lowest
    city = m.group(2).strip()
    temp = m.group(3)
    unit = m.group(4).lower()
    qualifier = (m.group(5) or "").strip().replace(" ", "")  # "orhigher"/"orlower"/""
    month = m.group(6)
    day = m.group(7)
    year = str(datetime.now(timezone.utc).year)

    city_slug = re.sub(r"[^a-z0-9]+", "-", city).strip("-")
    base = f"{var}-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    temp_suffix = f"{temp}{unit}{qualifier}" if qualifier else f"{temp}{unit}"
    return base, f"{base}-{temp_suffix}"


def _enrich_bet_for_display(bet: dict) -> None:
    """补齐 Dashboard 展示字段（旧记录兼容）。"""
    bet["order_type"] = _infer_order_type(bet)
    # T201: 修复 strategy_tag 为空时前端显示 manual/unknown 的问题
    # 场景：chain_sync 仓位的 bet_bucket='manual' 但无 strategy_tag
    stag = bet.get("strategy_tag") or ""
    bucket = bet.get("bet_bucket") or ""
    cat = bet.get("category") or ""
    outcome = str(bet.get("outcome") or "").upper()
    if not stag:
        if cat == "weather" and outcome == "NO":
            bet["strategy_tag"] = "weather_double_no_same"
        elif cat == "weather" and outcome == "YES":
            bet["strategy_tag"] = "weather_yes_ambush"
        elif "temperature" in (bet.get("question") or "").lower():
            # 赛道可能被推断为 unknown 但实际是天气
            if outcome == "NO":
                bet["strategy_tag"] = "weather_double_no_same"
            elif outcome == "YES":
                bet["strategy_tag"] = "weather_yes_ambush"
            if not cat or cat == "unknown":
                bet["category"] = "weather"
    # T201: 修正 chain_sync 仓位的 bet_bucket 从 'manual' 到正确的策略标签
    if bucket == "manual" and bet.get("strategy_tag"):
        bet["bet_bucket"] = bet["strategy_tag"]
    # G: 自动生成缺失的 slug（确保 PM 链接可用）
    if not bet.get("market_slug") and not bet.get("event_slug"):
        cat = bet.get("category") or ""
        q = bet.get("question") or ""
        if cat == "weather" or "temperature" in q.lower():
            e_slug, m_slug = _generate_weather_slugs(q)
            if e_slug:
                bet["event_slug"] = e_slug
            if m_slug:
                bet["market_slug"] = m_slug
    bet["ai_reasoning"] = _build_display_reasoning(bet)


@router.get("/api/status")
async def api_status(request: Request):
    """V2 系统状态。"""
    sys_info = _get_system_info()
    emergency_freeze = bool(get("risk.emergency_freeze"))
    maintenance_mode = bool(get("system.maintenance_mode"))
    maintenance_reason = ""
    user_frozen = False

    if maintenance_mode:
        try:
            with db_connect() as conn:
                r = conn.execute(
                    "SELECT value FROM pause_state WHERE key='maintenance_reason'"
                ).fetchone()
            if r:
                if isinstance(r, dict):
                    maintenance_reason = str(r.get("value") or "")
                elif hasattr(r, "keys") and "value" in r.keys():
                    maintenance_reason = str(r["value"] or "")
                elif isinstance(r, (list, tuple)) and r:
                    maintenance_reason = str(r[0] or "")
        except Exception:
            maintenance_reason = ""

    identity = get_request_identity(request)
    if identity and not identity.get("is_admin"):
        wallet_address = str(identity.get("wallet_address") or "")
        if wallet_address:
            with db_connect() as conn:
                row = conn.execute(
                    "SELECT freeze_auto, auto_trading_enabled FROM user_runtime_controls WHERE wallet_address=?",
                    (wallet_address,),
                ).fetchone()
            if row:
                user_frozen = bool(row["freeze_auto"]) or not bool(
                    row["auto_trading_enabled"]
                )

    return {
        "system": sys_info,
        "dry_run": False,
        "emergency_freeze": emergency_freeze,
        "maintenance_mode": maintenance_mode,
        "maintenance_reason": maintenance_reason,
        "frozen": bool(user_frozen or emergency_freeze),
        "user_frozen": user_frozen,
    }


@router.get("/api/summary")
async def api_summary():
    """投注汇总统计。"""
    def _db():
        with db_connect() as conn:
            row = conn.execute(
                """SELECT
                COUNT(*) as total,
                COUNT(CASE WHEN resolved_outcome IS NOT NULL THEN 1 END) as resolved,
                COUNT(CASE WHEN is_win=1 THEN 1 END) as wins,
                COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END), 0) as total_pnl,
                COALESCE(SUM(size_usdc), 0) as total_staked,
                COUNT(CASE WHEN resolved_outcome IS NULL THEN 1 END) as open_count,
                COALESCE(SUM(COALESCE(entry_fee_usdc,0) + COALESCE(exit_fee_usdc,0)), 0) as total_fees
            FROM paper_bets"""
            ).fetchone()
            total = row["total"]
            resolved = row["resolved"]
            wins = row["wins"]
            total_pnl = row["total_pnl"]
            total_staked = row["total_staked"]
            open_count = row["open_count"]
            total_fees = row["total_fees"]

            portfolio = bp_get_portfolio()
            balance = portfolio["balance"]

        return {
            "total": total,
            "resolved": resolved,
            "wins": wins,
            "losses": resolved - wins,
            "win_rate": round(wins / resolved, 4) if resolved > 0 else 0,
            "total_pnl": round(total_pnl, 2),
            "total_staked": round(total_staked, 2),
            "roi": round(total_pnl / total_staked, 4) if total_staked > 0 else 0,
            "open_count": open_count,
            "balance": balance,
            "total_fees": round(total_fees, 4),
        }
    return await asyncio.to_thread(_db)


@router.get("/api/portfolio")
async def api_portfolio():
    """专业级投资组合仪表盘 — Sharpe, 回撤, 胜率, 资金利用率, 权益曲线等。"""
    cached = _cache_get("portfolio", 15)
    if cached is not None:
        return cached
    dry_run = bool(get("general.dry_run"))
    if dry_run:
        initial = float(get("general.paper_trading_initial_balance") or 100.0)
    else:
        # 实盘模式：使用配置的初始充值金额，或从 balance_log 推算
        initial = float(get("general.live_initial_deposit") or 0)
        if initial <= 0:
            try:
                with db_connect() as _conn:
                    row = _conn.execute(
                        "SELECT usdc_balance FROM balance_log ORDER BY id ASC LIMIT 1"
                    ).fetchone()
                    if row:
                        initial = float(row["usdc_balance"])
            except Exception:
                pass
        if initial <= 0:
            initial = float(get("general.live_initial_deposit") or 100.0)
    today_start = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    week_start = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    month_start = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    def _db():

        with db_connect() as conn:
            all_bets = _rows_to_dicts(
                conn.execute(
                    """SELECT bet_id, size_usdc, payout, pnl, is_win,
                      resolved_outcome, resolved_at, category,
                      ts_entered, market_price, exit_price
               FROM paper_bets"""
                ).fetchall()
            )
            resolved = [b for b in all_bets if b.get("resolved_outcome") is not None]
            open_bets = [b for b in all_bets if b.get("resolved_outcome") is None]

            total_deployed = sum(b.get("size_usdc") or 0 for b in open_bets)
            total_returned = sum(b.get("payout") or 0 for b in resolved if b.get("payout"))
            realized_pnl = sum(
                b.get("pnl") or 0 for b in resolved if b.get("pnl") is not None
            )
            portfolio = bp_get_portfolio()
            balance = portfolio["balance"]
            nlv = portfolio["nlv"]
            market_value = portfolio.get("market_value", total_deployed)

            wins = [b for b in resolved if b.get("is_win")]
            losses = [b for b in resolved if not b.get("is_win")]
            win_rate = len(wins) / len(resolved) if resolved else 0
            avg_win = (sum(b.get("pnl") or 0 for b in wins) / len(wins)) if wins else 0
            avg_loss = (
                (sum(b.get("pnl") or 0 for b in losses) / len(losses)) if losses else 0
            )
            best_bet = max((b.get("pnl") or 0 for b in resolved), default=0)
            worst_bet = min((b.get("pnl") or 0 for b in resolved), default=0)
            profit_factor = (
                abs(sum(b.get("pnl") or 0 for b in wins))
                / abs(sum(b.get("pnl") or 0 for b in losses))
                if losses and sum(b.get("pnl") or 0 for b in losses) != 0
                else 0
            )

            sorted_resolved = sorted(resolved, key=lambda b: _safe_ts(b.get("resolved_at")))
            cur_streak = max_win_streak = max_loss_streak = 0
            streak_type = None
            for b in sorted_resolved:
                is_w = b.get("is_win")
                if is_w:
                    if streak_type == "win":
                        cur_streak += 1
                    else:
                        cur_streak = 1
                        streak_type = "win"
                    max_win_streak = max(max_win_streak, cur_streak)
                else:
                    if streak_type == "loss":
                        cur_streak += 1
                    else:
                        cur_streak = 1
                        streak_type = "loss"
                    max_loss_streak = max(max_loss_streak, cur_streak)

            daily_pnl: dict[str, float] = defaultdict(float)
            for b in sorted_resolved:
                ts = _safe_ts(b.get("resolved_at"))
                if ts > 0:
                    day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    daily_pnl[day] += b.get("pnl") or 0

            sorted_days = sorted(daily_pnl.keys())
            daily_returns = [daily_pnl[d] for d in sorted_days]

            sharpe = 0.0
            if len(daily_returns) >= 2:
                mean_r = sum(daily_returns) / len(daily_returns)
                var_r = sum((r - mean_r) ** 2 for r in daily_returns) / (
                    len(daily_returns) - 1
                )
                std_r = math.sqrt(var_r) if var_r > 0 else 0
                sharpe = round((mean_r / std_r) * math.sqrt(365), 2) if std_r > 0 else 0

            cum_pnl = 0.0
            peak = 0.0
            max_dd = 0.0
            equity_curve = []
            for d in sorted_days:
                cum_pnl += daily_pnl[d]
                peak = max(peak, cum_pnl)
                dd = peak - cum_pnl
                max_dd = max(max_dd, dd)
                equity_curve.append(
                    {
                        "date": d,
                        "equity": round(initial + cum_pnl, 2),
                        "pnl": round(cum_pnl, 2),
                    }
                )

            max_dd_pct = round(max_dd / initial * 100, 2) if initial > 0 else 0

            today_pnl = sum(
                b.get("pnl") or 0
                for b in resolved
                if _safe_ts(b.get("resolved_at")) >= today_start
            )
            week_pnl = sum(
                b.get("pnl") or 0
                for b in resolved
                if _safe_ts(b.get("resolved_at")) >= week_start
            )
            month_pnl = sum(
                b.get("pnl") or 0
                for b in resolved
                if _safe_ts(b.get("resolved_at")) >= month_start
            )

            capital_utilization = round(total_deployed / nlv * 100, 1) if nlv > 0 else 0

            # Phase43: 未实现盈亏 = 持仓市值 - 入场成本 - 买入手续费
            # T189-G: 扣除open仓位的买入手续费
            _open_entry_fees = sum(float(b.get("entry_fee_usdc") or 0) for b in open_bets)
            unrealized_pnl = market_value - total_deployed - _open_entry_fees
            total_pnl = realized_pnl + unrealized_pnl

            # Phase54: 收益率基于 bot 追踪的 NLV，而非链上余额
            # 链上余额包含手动交易/充值/提现等未追踪操作，导致收益率失真
            bot_nlv = initial + realized_pnl + unrealized_pnl

        result = {
            "initial_balance": round(initial, 2),
            "balance": round(balance, 2),
            "nlv": round(nlv, 2),
            "total_deployed": round(total_deployed, 2),
            "market_value": round(market_value, 2),
            "total_returned": round(total_returned, 2),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": (
                round((bot_nlv - initial) / initial * 100, 2) if initial > 0 else 0
            ),
            "sharpe_ratio": sharpe,
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": max_dd_pct,
            "profit_factor": round(profit_factor, 2),
            "total_bets": len(all_bets),
            "resolved_count": len(resolved),
            "open_count": len(open_bets),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "best_bet": round(best_bet, 4),
            "worst_bet": round(worst_bet, 4),
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "capital_utilization": capital_utilization,
            "avg_position_size": (
                round(total_deployed / len(open_bets), 2) if open_bets else 0
            ),
            "today_pnl": round(today_pnl, 4),
            "week_pnl": round(week_pnl, 4),
            "month_pnl": round(month_pnl, 4),
            "equity_curve": equity_curve,
        }
        _cache_set("portfolio", result)
        return result
    return await asyncio.to_thread(_db)


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"  # T224: CLOB REST API（实时价格）

# T193: 实时价格改为按 market_id 逐个缓存（避免全局TTL过期后一次性重请求所有市场）
_per_market_price_cache: dict[str, tuple[float, dict]] = (
    {}
)  # {market_id: (ts, prices_info)}
_REALTIME_CACHE_TTL = 8  # T224: 从60s降至8s — CLOB REST 足够快，缩短缓存窗口

# T224: Token ID 长期缓存 — market_id → {"YES": token_id, "NO": token_id}
# token_id 不会改变，缓存在进程生命周期内有效
_token_id_cache: dict[str, dict[str, str]] = {}


def _resolve_token_ids(mid: str, slug: str = "") -> dict[str, str] | None:
    """解析 market_id → YES/NO token_id 映射（Gamma API 一次性查询，长期缓存）。"""
    if mid in _token_id_cache:
        return _token_id_cache[mid]
    try:
        if mid.startswith("0x"):
            _params = {"slug": slug} if slug else {"condition_id": mid}
            resp = pm_session().get(f"{GAMMA_API}/markets", params=_params, timeout=8)
        else:
            resp = pm_session().get(f"{GAMMA_API}/markets/{mid}", timeout=8)
        resp.raise_for_status()
        mdata = resp.json()
        if isinstance(mdata, list):
            mdata = mdata[0] if mdata else None
        if not mdata:
            return None
        clob_ids = mdata.get("clobTokenIds")
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)
        if clob_ids and len(clob_ids) >= 2:
            mapping = {"YES": str(clob_ids[0]), "NO": str(clob_ids[1])}
            _token_id_cache[mid] = mapping
            return mapping
    except Exception as e:
        logger.debug("[RealtimePrice] token_id 解析失败 %s: %s", mid[:16], e)
    return None


def _fetch_clob_sell_price(token_id: str) -> float | None:
    """从 CLOB REST API 获取 token 的最佳卖出价（bestBid）。

    端点: GET /price?token_id=XXX&side=SELL
    返回该 token 在 CLOB 上的卖出可成交价（市场最优买方出价）。
    """
    try:
        resp = pm_session().get(
            f"{CLOB_API}/price",
            params={"token_id": token_id, "side": "SELL"},
            timeout=3,
        )
        if resp.ok:
            price = resp.json().get("price")
            if price is not None:
                return float(price)
    except Exception:
        pass
    return None


def _fetch_single_market_gamma(mid: str, slug: str = "") -> tuple[str, dict | None]:
    """获取单个市场的实时价格 — Gamma REST API（有缓存延迟，作为 CLOB 降级方案）。"""
    try:
        if mid.startswith("0x"):
            _params = {"slug": slug} if slug else {"condition_id": mid}
            resp = pm_session().get(
                f"{GAMMA_API}/markets",
                params=_params,
                timeout=8,
            )
        else:
            resp = pm_session().get(f"{GAMMA_API}/markets/{mid}", timeout=8)
        resp.raise_for_status()
        mdata = resp.json()
        if isinstance(mdata, list):
            mdata = mdata[0] if mdata else None
        if not isinstance(mdata, dict):
            return mid, None

        prices_info: dict = {
            "best_bid": None,
            "best_ask": None,
            "outcome_prices": {},
        }
        best_bid = mdata.get("bestBid")
        best_ask = mdata.get("bestAsk")
        if best_bid is not None:
            prices_info["best_bid"] = float(best_bid)
        if best_ask is not None:
            prices_info["best_ask"] = float(best_ask)

        tokens = mdata.get("tokens") or []
        for tok in tokens:
            tok_outcome = (tok.get("outcome") or "").upper()
            tok_price = tok.get("price")
            if tok_outcome and tok_price is not None:
                prices_info["outcome_prices"][tok_outcome] = float(tok_price)

        if not prices_info["outcome_prices"]:
            outcomes_list = mdata.get("outcomes") or []
            if isinstance(outcomes_list, str):
                try:
                    outcomes_list = json.loads(outcomes_list)
                except Exception:
                    outcomes_list = []
            prices_raw = mdata.get("outcomePrices") or []
            if isinstance(prices_raw, str):
                try:
                    prices_raw = json.loads(prices_raw)
                except Exception:
                    prices_raw = []
            for i, o in enumerate(outcomes_list):
                if i < len(prices_raw):
                    try:
                        prices_info["outcome_prices"][str(o).upper()] = float(
                            prices_raw[i]
                        )
                    except (ValueError, TypeError):
                        pass

        return mid, prices_info
    except Exception as e:
        logger.debug(f"[RealtimePrice] Gamma获取失败 {mid[:16]}: {e}")
        return mid, None


def _fetch_single_market(mid: str, slug: str = "") -> tuple[str, dict | None]:
    """获取单个市场的实时价格（供线程池并发调用）。

    T224: 优先使用 CLOB REST API（实时 bestBid），失败则降级为 Gamma API。
    CLOB /price?side=SELL 返回该 token 的最佳卖出价（= 买方最高出价），
    直接对应持仓者的可实现卖出价，无 Gamma 缓存滞后。
    """
    token_ids = _resolve_token_ids(mid, slug)
    if token_ids:
        prices_info: dict = {
            "best_bid": None,
            "best_ask": None,
            "outcome_prices": {},
        }
        clob_ok = False
        for side_label, tok in token_ids.items():
            price = _fetch_clob_sell_price(tok)
            if price is not None:
                prices_info["outcome_prices"][side_label] = price
                if side_label == "YES":
                    prices_info["best_bid"] = price
                clob_ok = True
        if clob_ok:
            return mid, prices_info
    # CLOB 失败 → Gamma 降级
    return _fetch_single_market_gamma(mid, slug)


def _fetch_realtime_prices(bets: list[dict]) -> dict[str, dict]:
    """
    并发批量获取开仓投注的实时市场价格。

    返回: {market_id: {"best_bid": float, "best_ask": float, "outcome_prices": {}}}
    T193: 改为按 market_id 逐个缓存（避免全局TTL过期后一次性请求所有市场）。
    """
    now = time.time()
    market_ids = list({b.get("market_id", "") for b in bets if b.get("market_id")})
    if not market_ids:
        return {}

    # 构建 market_id → slug 映射（slug 优先查询更可靠）
    _slug_map: dict[str, str] = {}
    for b in bets:
        mid = b.get("market_id", "")
        slug = b.get("market_slug", "")
        if mid and slug:
            _slug_map[mid] = slug

    # T193: 从逐市场缓存中获取仍有效的，只请求过期的
    result: dict[str, dict] = {}
    stale_mids: list[str] = []
    for mid in market_ids:
        cached = _per_market_price_cache.get(mid)
        if cached and (now - cached[0]) < _REALTIME_CACHE_TTL:
            result[mid] = cached[1]
        else:
            stale_mids.append(mid)

    if stale_mids:
        with ThreadPoolExecutor(max_workers=min(len(stale_mids), 8)) as executor:
            futures = {
                executor.submit(_fetch_single_market, mid, _slug_map.get(mid, "")): mid
                for mid in stale_mids
            }
            for future in as_completed(futures, timeout=12):
                try:
                    mid, prices_info = future.result()
                    if prices_info:
                        result[mid] = prices_info
                        _per_market_price_cache[mid] = (now, prices_info)
                except Exception as e:
                    logger.debug(f"[RealtimePrice] 并发获取异常: {e}")

    return result


def _get_current_price_for_bet(bet: dict, prices: dict) -> float | None:
    """从实时价格数据中提取特定投注方向的当前价格（卖出视角）。

    T224: 优先使用 CLOB 直接卖出价（outcome_prices 中的值），
    这是通过 CLOB /price?side=SELL 获取的该 token 的真实卖出价。
    若 CLOB 数据不可用则回退到 Gamma bestBid/bestAsk。
    """
    mid = bet.get("market_id", "")
    outcome = (bet.get("outcome") or "").upper()
    pinfo = prices.get(mid)
    if not pinfo:
        return None

    op = pinfo.get("outcome_prices", {})

    # YES 仓位：CLOB YES卖出价 > Gamma bestBid > outcome_prices fallback
    if outcome == "YES":
        # T224: outcome_prices["YES"] 可能是 CLOB 直接卖出价
        clob_yes = op.get("YES")
        if clob_yes is not None:
            return clob_yes
        bid = pinfo.get("best_bid")
        if bid is not None:
            return bid
        return None

    # NO 仓位：CLOB NO卖出价 > Gamma 1-bestAsk > outcome_prices fallback
    if outcome == "NO":
        # T224: outcome_prices["NO"] 是 CLOB NO token 的直接卖出价（最准确）
        clob_no = op.get("NO")
        if clob_no is not None:
            return clob_no
        ask = pinfo.get("best_ask")
        if ask is not None:
            return round(1.0 - ask, 4)
        return None

    # 其他 outcome（如多选题）：直接查 outcome_prices
    return op.get(outcome)


@router.get("/api/bets/open")
async def api_open_bets():
    """当前开仓列表（含实时价格）。"""
    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_bets WHERE resolved_outcome IS NULL ORDER BY ts_entered DESC"
            ).fetchall()
        bets = _rows_to_dicts(rows)
        now = time.time()

        # 批量获取实时价格
        realtime_prices = _fetch_realtime_prices(bets)

        # T188: 批量并发刷新 market_end_date
        _batch_refresh_market_end_dates(bets)

        total_market_value = 0.0
        for b in bets:
            _enrich_bet_for_display(b)
            b["held_minutes"] = int((now - _safe_ts(b.get("ts_entered"))) / 60)
            end = b.get("market_end_date") or ""
            if end:
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    b["hours_to_expiry"] = round(
                        (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600, 2
                    )
                except Exception:
                    b["hours_to_expiry"] = None
            else:
                b["hours_to_expiry"] = None

            # 实时价格与未实现盈亏
            current_price = _get_current_price_for_bet(b, realtime_prices)
            entry_price = float(b.get("market_price") or 0)
            size_usdc = float(b.get("size_usdc") or 0)

            b["current_price"] = (
                round(current_price, 4) if current_price is not None else None
            )
            if current_price is not None and entry_price > 0:
                # 优先使用 CLOB matched_shares（精确），否则从 size_usdc/entry_price 推算
                matched = float(b.get("matched_shares") or 0)
                shares = matched if matched > 0 else size_usdc / entry_price
                market_value = shares * current_price
                # T189-G: 未实现盈亏扣除买入手续费（卖出手续费尚未发生）
                _entry_fee = float(b.get("entry_fee_usdc") or 0)
                b["unrealized_pnl"] = round(market_value - size_usdc - _entry_fee, 4)
                b["unrealized_pnl_pct"] = round(
                    (current_price - entry_price) / entry_price, 4
                )
                b["market_value"] = round(market_value, 4)
                total_market_value += market_value
            else:
                b["unrealized_pnl"] = None
                b["unrealized_pnl_pct"] = None
                b["market_value"] = size_usdc  # 回退到入场金额
                total_market_value += size_usdc

        return {"bets": bets, "total_market_value": round(total_market_value, 4)}
    return await asyncio.to_thread(_db)


@router.get("/api/bets/recent")
async def api_recent_bets(limit: int = Query(30, ge=1, le=200)):
    """最近投注（含已结算）。"""
    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_bets ORDER BY ts_entered DESC LIMIT ?", (limit,)
            ).fetchall()
        bets = _rows_to_dicts(rows)
        for b in bets:
            _enrich_bet_for_display(b)
        return {"bets": bets}
    return await asyncio.to_thread(_db)


# ── T219-perf: /api/bets/history 服务端查询缓存 ──
# 缓存最近查询结果，TTL 5秒，避免快速切换筛选器时重复全表扫描
_history_cache: dict[str, tuple[float, dict]] = {}  # {cache_key: (ts, response)}
_HISTORY_CACHE_TTL = 5  # 秒
_HISTORY_CACHE_MAX = 20  # 最大缓存条目数
# COUNT(*) 缓存（与分页结果分离）：降低 resolved/win/loss 首次切换延迟
_history_count_cache: dict[str, tuple[float, int]] = {}
_HISTORY_COUNT_CACHE_TTL = 30  # 秒
_HISTORY_COUNT_CACHE_MAX = 60


@router.get("/api/bets/history")
async def api_history_bets(
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=200),
    category: str = Query("", description="Filter by category"),
    status: str = Query(
        "", description="all|open|resolved|win|loss|profit_take|stop_loss"
    ),
    bet_type: str = Query("", description="Filter by bet_type: entry|scale_in"),
    dimension: str = Query("", description="Filter by dimension tag"),
    date_from: str = Query("", description="Start date YYYY-MM-DD (inclusive)"),
    date_to: str = Query("", description="End date YYYY-MM-DD (inclusive)"),
    market_id: str = Query("", description="Filter by market_id (substring match)"),
    tag: str = Query("", description="Filter by bet tag"),
):
    """全量历史投注记录（分页 + 筛选）。"""
    # ── 服务端缓存 — 快速切换筛选器时避免重复DB查询 ──
    _cache_key = f"{page}|{per_page}|{category}|{status}|{bet_type}|{dimension}|{date_from}|{date_to}|{market_id}|{tag}"
    _count_cache_key = f"{category}|{status}|{bet_type}|{dimension}|{date_from}|{date_to}|{market_id}|{tag}"
    _cached = _history_cache.get(_cache_key)
    if _cached and (time.time() - _cached[0]) < _HISTORY_CACHE_TTL:
        return _cached[1]

    _VALID_DIMENSIONS = frozenset(
        ["trending", "liquidity", "volume", "newest", "ending_soon", "competitive"]
    )
    where_clauses = []
    params: list = []

    if category:
        where_clauses.append("category = ?")
        params.append(category)

    if market_id:
        where_clauses.append("(market_id LIKE ? OR question LIKE ? OR bet_id LIKE ?)")
        params.extend([f"%{market_id}%", f"%{market_id}%", f"%{market_id}%"])

    if dimension and dimension in _VALID_DIMENSIONS:
        where_clauses.append("dimension_tags LIKE ?")
        params.append(f"%{dimension}%")

    _join_tag = ""
    if tag:
        _join_tag = (
            " INNER JOIN bet_tags bt ON bt.bet_id = paper_bets.bet_id AND bt.tag = ?"
        )
        params.insert(0, tag)  # JOIN params go first

    if bet_type in ("entry", "scale_in"):
        where_clauses.append("bet_type = ?")
        params.append(bet_type)

    if date_from and re.match(r"^\d{4}-\d{2}-\d{2}$", date_from):
        try:
            dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            where_clauses.append("ts_entered >= ?")
            params.append(dt.timestamp())
        except ValueError:
            pass
    if date_to and re.match(r"^\d{4}-\d{2}-\d{2}$", date_to):
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            dt = dt + timedelta(days=1)
            where_clauses.append("ts_entered < ?")
            params.append(dt.timestamp())
        except ValueError:
            pass

    if status == "open":
        where_clauses.append("resolved_outcome IS NULL")
    elif status == "resolved":
        where_clauses.append("resolved_outcome IS NOT NULL")
    elif status == "win":
        where_clauses.append("is_win = 1")
    elif status == "loss":
        where_clauses.append("is_win = 0 AND resolved_outcome IS NOT NULL")
    elif status == "profit_take":
        where_clauses.append("exit_type = 'profit_take'")
    elif status == "stop_loss":
        where_clauses.append("exit_type = 'stop_loss'")
    elif status == "manual_exit":
        where_clauses.append("exit_type = 'manual_exit'")

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    offset = (page - 1) * per_page

    # 列表页仅查询必要字段，避免 SELECT * 拉取大文本/详情字段拖慢筛选切换
    # 生产库可能因历史版本缺少部分列（如 pnl_pct），这里按实际 schema 动态选择。
    requested_columns = [
        "bet_id",
        "ts_entered",
        "question",
        "category",
        "outcome",
        "size_usdc",
        "market_price",
        "matched_shares",
        "resolved_outcome",
        "is_win",
        "pnl",
        "pnl_pct",
        "exit_type",
        "market_id",
        "market_slug",
        "event_slug",
        "market_end_date",
        "bet_type",
        "strategy_tag",
        "dimension_tags",
    ]
    def _db():

        with db_connect() as conn:
            table_columns = {
                r["name"] for r in conn.execute("PRAGMA table_info(paper_bets)").fetchall()
            }
            select_columns = [c for c in requested_columns if c in table_columns]
            # 理论上不会发生；保底避免空 SELECT 导致 SQL 语法错误
            if not select_columns:
                select_columns = ["bet_id", "ts_entered"]

            now_ts = time.time()
            cached_total = _history_count_cache.get(_count_cache_key)
            if cached_total and (now_ts - cached_total[0]) < _HISTORY_COUNT_CACHE_TTL:
                total = cached_total[1]
            else:
                total = conn.execute(
                    f"SELECT COUNT(*) as c FROM paper_bets{_join_tag}{where_sql}", params
                ).fetchone()["c"]
                _history_count_cache[_count_cache_key] = (now_ts, total)
                if len(_history_count_cache) > _HISTORY_COUNT_CACHE_MAX:
                    _history_count_cache.clear()

            rows = conn.execute(
                f"SELECT {', '.join('paper_bets.' + c for c in select_columns)} FROM paper_bets{_join_tag}{where_sql} ORDER BY paper_bets.ts_entered DESC LIMIT ? OFFSET ?",
                params + [per_page, offset],
            ).fetchall()

        bets = _rows_to_dicts(rows)
        missing_columns = [c for c in requested_columns if c not in select_columns]
        if missing_columns:
            for b in bets:
                for col in missing_columns:
                    b.setdefault(col, None)

        now = time.time()
        for b in bets:
            b["held_minutes"] = int((now - _safe_ts(b.get("ts_entered"))) / 60)
            end = b.get("market_end_date") or ""
            if end:
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    b["hours_to_expiry"] = round(
                        (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600, 2
                    )
                except Exception:
                    b["hours_to_expiry"] = None
            else:
                b["hours_to_expiry"] = None

        # T195: 实时价格 — 仅使用缓存（<1ms），后台刷新过期项
        open_bets = [b for b in bets if b.get("resolved_outcome") is None]
        if open_bets:
            import threading

            _now_rt = time.time()
            # 快速缓存命中（不发 HTTP）
            cached_prices: dict[str, dict] = {}
            stale_bets: list[dict] = []
            for ob in open_bets:
                mid = ob.get("market_id", "")
                cached = _per_market_price_cache.get(mid)
                if cached and (_now_rt - cached[0]) < _REALTIME_CACHE_TTL:
                    cached_prices[mid] = cached[1]
                else:
                    stale_bets.append(ob)

            # 注入已缓存的价格
            for b in open_bets:
                mid = b.get("market_id", "")
                if mid in cached_prices:
                    current_price = _get_current_price_for_bet(b, cached_prices)
                else:
                    current_price = None
                entry_price = float(b.get("market_price") or 0)
                size_usdc = float(b.get("size_usdc") or 0)
                b["current_price"] = (
                    round(current_price, 4) if current_price is not None else None
                )
                if current_price is not None and entry_price > 0:
                    shares = size_usdc / entry_price
                    market_value = shares * current_price
                    _entry_fee2 = float(b.get("entry_fee_usdc") or 0)
                    b["unrealized_pnl"] = round(market_value - size_usdc - _entry_fee2, 4)
                    b["unrealized_pnl_pct"] = round(
                        (current_price - entry_price) / entry_price, 4
                    )
                    b["market_value"] = round(market_value, 4)
                else:
                    b["unrealized_pnl"] = None
                    b["unrealized_pnl_pct"] = None
                    b["market_value"] = None

            # 后台刷新过期的市场价格（下次请求时缓存命中）
            if stale_bets:
                threading.Thread(
                    target=_fetch_realtime_prices,
                    args=(stale_bets,),
                    daemon=True,
                ).start()

        # T214I: 列表API字段瘦身 — 去除卡片渲染不需要的大字段，减少 ~40% 传输量
        # 这些字段只在详情面板 /api/bets/{betId} 中需要
        _LIST_EXCLUDE_FIELDS = {
            "condition_id",
            "strategy_params",
            "weather_context",
            "position_history",
            "sim_balance_before",
            "sim_balance_after",
            "ai_models",
            "confidence",
            "ai_probability",
            "ev",
            "cycle_id",
            "parent_bet_id",
            "source",
            "sell_fail_count",
            "ghost_reopened_at",
            "trailing_peak_price",
            "trailing_floor_price",
            "copy_source_wallet",
            "fee_rate",
            "entry_fee_usdc",
            "exit_fee_usdc",
            "scale_in_count",
            "last_scale_at",
            "order_type",
        }
        slim_bets = [
            {k: v for k, v in b.items() if k not in _LIST_EXCLUDE_FIELDS} for b in bets
        ]

        # T263: 附加标签信息到每个投注
        if slim_bets:
            bet_ids = [b["bet_id"] for b in slim_bets if b.get("bet_id")]
            if bet_ids:
                try:
                    with db_connect() as conn2:
                        placeholders = ",".join("?" * len(bet_ids))
                        tag_rows = conn2.execute(
                            f"SELECT bet_id, tag FROM bet_tags WHERE bet_id IN ({placeholders})",
                            bet_ids,
                        ).fetchall()
                        tag_map: dict[str, list[str]] = {}
                        for tr in tag_rows:
                            tag_map.setdefault(tr["bet_id"], []).append(tr["tag"])
                        for b in slim_bets:
                            b["tags"] = tag_map.get(b.get("bet_id"), [])
                except Exception:
                    for b in slim_bets:
                        b["tags"] = []
            else:
                for b in slim_bets:
                    b["tags"] = []

        result = {
            "bets": slim_bets,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        }

        # ── 服务端缓存写入 ──
        _history_cache[_cache_key] = (time.time(), result)
        # 简单淘汰：超限时清除全部（下次请求重建）
        if len(_history_cache) > _HISTORY_CACHE_MAX:
            _history_cache.clear()

        return result
    return await asyncio.to_thread(_db)


def _compute_aggregate(conn, where_sql: str, params: list) -> dict:
    """Compute aggregate stats for the filtered bet set (all pages)."""
    row = conn.execute(
        f"SELECT COUNT(*) as cnt, "
        f"COALESCE(SUM(size_usdc),0) as total_staked, "
        f"COALESCE(SUM(pnl),0) as total_pnl, "
        f"COALESCE(AVG(ev),0) as avg_ev, "
        f"COALESCE(AVG(market_price),0) as avg_entry, "
        f"SUM(CASE WHEN is_win=1 THEN 1 ELSE 0 END) as wins, "
        f"SUM(CASE WHEN resolved_outcome IS NOT NULL THEN 1 ELSE 0 END) as resolved "
        f"FROM paper_bets{where_sql}",
        params,
    ).fetchone()
    d = dict(row)
    d["wins"] = d["wins"] or 0
    d["resolved"] = d["resolved"] or 0
    d["win_rate"] = round(d["wins"] / d["resolved"], 4) if d["resolved"] > 0 else 0
    d["roi"] = (
        round(d["total_pnl"] / d["total_staked"], 4)
        if (d["total_staked"] or 0) > 0
        else 0
    )
    d["total_staked"] = round(d["total_staked"], 2)
    d["total_pnl"] = round(d["total_pnl"], 2)
    d["avg_ev"] = round(d["avg_ev"], 4)
    d["avg_entry"] = round(d["avg_entry"], 4)
    return d


@router.get("/api/bets/live-prices")
async def api_live_prices():
    """轻量实时价格端点 — 返回 OPEN 仓位的当前价格/P&L，供前端高频轮询。

    T224: 使用 CLOB REST API 替代 Gamma API（消除30-60秒缓存滞后）。
    缓存 TTL 8秒；过期时内联刷新（CLOB 延迟 ~200ms，无需后台线程）。
    """
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT bet_id, market_id, outcome, market_price, size_usdc, "
            "entry_fee_usdc, market_slug FROM paper_bets "
            "WHERE resolved_outcome IS NULL ORDER BY ts_entered DESC"
        ).fetchall()

    if not rows:
        return {"prices": {}, "ts": time.time()}

    bets = [dict(r) for r in rows]
    now = time.time()
    result: dict[str, dict] = {}
    stale_bets: list[dict] = []

    for b in bets:
        mid = b.get("market_id", "")
        bid = b.get("bet_id", "")
        cached = _per_market_price_cache.get(mid) if mid else None

        if cached and (now - cached[0]) < _REALTIME_CACHE_TTL:
            current_price = _get_current_price_for_bet(b, {mid: cached[1]})
            entry_price = float(b.get("market_price") or 0)
            size_usdc = float(b.get("size_usdc") or 0)
            if current_price is not None:
                cp = round(current_price, 4)
                if entry_price > 0:
                    shares = size_usdc / entry_price
                    mv = round(shares * current_price, 4)
                    fee = float(b.get("entry_fee_usdc") or 0)
                    upnl = round(mv - size_usdc - fee, 4)
                    upnl_pct = round((current_price - entry_price) / entry_price, 4)
                else:
                    mv = size_usdc
                    upnl = None
                    upnl_pct = None
                result[bid] = {
                    "cp": cp,
                    "mv": mv,
                    "upnl": upnl,
                    "upnl_pct": upnl_pct,
                }
            else:
                result[bid] = {"cp": None, "mv": None, "upnl": None, "upnl_pct": None}
        else:
            stale_bets.append(b)

    # T224: 内联刷新过期缓存 — CLOB REST 足够快（~200ms/市场，并发），
    # 消除旧方案中后台线程带来的 3-6 秒"null窗口"
    if stale_bets:
        import asyncio

        fresh_prices = await asyncio.to_thread(_fetch_realtime_prices, stale_bets)
        for b in stale_bets:
            mid = b.get("market_id", "")
            bid = b.get("bet_id", "")
            if mid in fresh_prices:
                current_price = _get_current_price_for_bet(b, fresh_prices)
                entry_price = float(b.get("market_price") or 0)
                size_usdc = float(b.get("size_usdc") or 0)
                if current_price is not None:
                    cp = round(current_price, 4)
                    if entry_price > 0:
                        shares = size_usdc / entry_price
                        mv = round(shares * current_price, 4)
                        fee = float(b.get("entry_fee_usdc") or 0)
                        upnl = round(mv - size_usdc - fee, 4)
                        upnl_pct = round((current_price - entry_price) / entry_price, 4)
                    else:
                        mv = size_usdc
                        upnl = None
                        upnl_pct = None
                    result[bid] = {
                        "cp": cp,
                        "mv": mv,
                        "upnl": upnl,
                        "upnl_pct": upnl_pct,
                    }
                else:
                    result[bid] = {
                        "cp": None,
                        "mv": None,
                        "upnl": None,
                        "upnl_pct": None,
                    }
            else:
                result[bid] = {
                    "cp": None,
                    "mv": None,
                    "upnl": None,
                    "upnl_pct": None,
                }

    return {"prices": result, "ts": now}


@router.get("/api/bets/{bet_id}")
async def api_bet_detail(bet_id: str):
    """单笔投注详情 + 仓位链（加仓子单 / 父单）。"""
    now = time.time()
    def _db():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_bets WHERE bet_id = ?", (bet_id,)
            ).fetchone()
            if not row:
                return JSONResponse({"error": "bet not found"}, status_code=404)
            bet = dict(row)
            _enrich_bet_for_display(bet)
            _refresh_market_end_date_for_display(bet)

            bet["held_minutes"] = int((now - _safe_ts(bet.get("ts_entered"))) / 60)
            end = bet.get("market_end_date") or ""
            if end:
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    bet["hours_to_expiry"] = round(
                        (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600, 2
                    )
                except Exception:
                    bet["hours_to_expiry"] = None
            else:
                bet["hours_to_expiry"] = None

            children = _rows_to_dicts(
                conn.execute(
                    "SELECT bet_id, ts_entered, size_usdc, market_price, ev, bet_type "
                    "FROM paper_bets WHERE parent_bet_id = ? ORDER BY ts_entered",
                    (bet_id,),
                ).fetchall()
            )
            parent = None
            if bet.get("parent_bet_id"):
                p_row = conn.execute(
                    "SELECT bet_id, ts_entered, size_usdc, market_price, ev, bet_type "
                    "FROM paper_bets WHERE bet_id = ?",
                    (bet.get("parent_bet_id"),),
                ).fetchone()
                if p_row:
                    parent = dict(p_row)

            siblings = _rows_to_dicts(
                conn.execute(
                    "SELECT bet_id, ts_entered, size_usdc, market_price, ev, bet_type, "
                    "resolved_outcome, pnl, exit_type "
                    "FROM paper_bets WHERE market_id = ? ORDER BY ts_entered",
                    (bet.get("market_id"),),
                ).fetchall()
            )
            total_position = round(
                sum(s["size_usdc"] or 0 for s in siblings if not s.get("resolved_outcome")),
                2,
            )

            # Phase 57: 事件级关联仓位（同 event_slug 不同市场，如同城市不同温度）
            event_siblings = []
            evt_slug = bet.get("event_slug") or ""
            if evt_slug:
                event_siblings = _rows_to_dicts(
                    conn.execute(
                        "SELECT bet_id, ts_entered, question, size_usdc, market_price, "
                        "outcome, ev, bet_type, resolved_outcome, pnl, exit_type, source "
                        "FROM paper_bets WHERE event_slug = ? AND market_id != ? "
                        "ORDER BY ts_entered",
                        (evt_slug, bet.get("market_id")),
                    ).fetchall()
                )

        # 仅对未结算的持仓注入实时价格（避免对已结算单做无效API调用）
        if not bet.get("resolved_outcome"):
            mid = bet.get("market_id", "")
            slug = bet.get("market_slug", "")
            if mid:
                _, prices_info = _fetch_single_market(mid, slug)
                if prices_info:
                    current_price = _get_current_price_for_bet(bet, {mid: prices_info})
                    if current_price is not None:
                        bet["current_price"] = round(current_price, 4)
                        # 计算未实现盈亏（与 /api/bets/open 对齐）
                        entry_price = float(bet.get("market_price") or 0)
                        size_usdc = float(bet.get("size_usdc") or 0)
                        if entry_price > 0:
                            matched = float(bet.get("matched_shares") or 0)
                            shares = matched if matched > 0 else size_usdc / entry_price
                            market_value = shares * current_price
                            # T189-G: 未实现盈亏扣除买入手续费
                            _entry_fee3 = float(bet.get("entry_fee_usdc") or 0)
                            bet["unrealized_pnl"] = round(
                                market_value - size_usdc - _entry_fee3, 4
                            )
                            bet["unrealized_pnl_pct"] = round(
                                (current_price - entry_price) / entry_price, 4
                            )
                            bet["market_value"] = round(market_value, 4)

        return {
            "bet": bet,
            "children": children,
            "parent": parent,
            "siblings": siblings,
            "event_siblings": event_siblings,
            "total_position_usdc": total_position,
        }
    return await asyncio.to_thread(_db)


@router.get("/api/bets/{bet_id}/price-history")
async def api_bet_price_history(bet_id: str, interval: str = "1d"):
    """投注单的价格走势历史（Polymarket CLOB prices-history）。"""
    from src.utils.orderbook_history import fetch_prices_history

    with db_connect() as conn:
        row = conn.execute(
            "SELECT token_id, outcome FROM paper_bets WHERE bet_id = ?", (bet_id,)
        ).fetchone()
        if not row:
            return JSONResponse({"error": "bet not found"}, status_code=404)
        token_id = row["token_id"]
        if not token_id:
            return {"status": "ok", "history": [], "reason": "no_token_id"}

    history = await asyncio.to_thread(fetch_prices_history, token_id, interval, 60)
    return {"status": "ok", "history": history, "token_id": str(token_id)[:16]}


@router.get("/api/tracks")
async def api_tracks(request: Request):
    """赛道统计。"""
    identity = get_request_identity(request)
    if identity and not identity.get("is_admin"):
        from scripts.dashboard_routes import user_routes as ur

        wallet_address = str(identity.get("wallet_address") or "")
        _, bets = ur._build_manual_pm_bets(wallet_address, "", "", "")
        by_cat: dict[str, dict] = {}
        for bet in bets:
            cat = str(bet.get("category") or "").strip().lower()
            if not cat or cat in {"unknown", "default"}:
                cat = "economy"
            item = by_cat.setdefault(
                cat,
                {
                    "category": cat,
                    "total": 0,
                    "resolved": 0,
                    "wins": 0,
                    "pnl": 0.0,
                    "staked": 0.0,
                    "avg_ev": None,
                },
            )
            item["total"] += 1
            item["staked"] += float(bet.get("size_usdc") or 0.0)
            if bet.get("resolved_outcome"):
                item["resolved"] += 1
                pnl = float(bet.get("pnl") or 0.0)
                item["pnl"] += pnl
                if pnl >= 0:
                    item["wins"] += 1

        tracks = []
        for item in sorted(by_cat.values(), key=lambda row: row["total"], reverse=True):
            item["pnl"] = round(item["pnl"], 2)
            item["staked"] = round(item["staked"], 2)
            item["win_rate"] = (
                round(item["wins"] / item["resolved"], 4) if item["resolved"] > 0 else 0
            )
            item["roi"] = (
                round(item["pnl"] / item["staked"], 4) if item["staked"] > 0 else 0
            )
            tracks.append(item)
        return {"tracks": tracks}

    cached = _cache_get("tracks", 15)
    if cached is not None:
        return cached
    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                """SELECT
                category,
                COUNT(*) as total,
                SUM(CASE WHEN resolved_outcome IS NOT NULL THEN 1 ELSE 0 END) as resolved,
                SUM(CASE WHEN is_win=1 THEN 1 ELSE 0 END) as wins,
                ROUND(CAST(COALESCE(SUM(pnl), 0) AS numeric), 2) as pnl,
                ROUND(CAST(COALESCE(SUM(size_usdc), 0) AS numeric), 2) as staked,
                ROUND(CAST(AVG(ev) AS numeric), 4) as avg_ev
            FROM paper_bets
            GROUP BY category
            ORDER BY total DESC"""
            ).fetchall()
        tracks = []
        for r in rows:
            d = dict(r)
            cat = str(d.get("category") or "").strip().lower()
            if not cat or cat in {"unknown", "default"}:
                d["category"] = "economy"
            d["win_rate"] = round(d["wins"] / d["resolved"], 4) if d["resolved"] > 0 else 0
            d["roi"] = round(d["pnl"] / d["staked"], 4) if d["staked"] > 0 else 0
            tracks.append(d)
        result = {"tracks": tracks}
        _cache_set("tracks", result)
        return result
    return await asyncio.to_thread(_db)


# T295-C: 重新整合日志类型 → 7个智能分类（覆盖所有原有类型）
# T300-C: 补充 CityScanner/StrikeOpt → weather, 补充 copytrade/httpx → trades/system
# trades    : 交易执行（原 trading + executor + position + reconcile + copytrade）
# signal    : AI信号（原 signal + scanner，含 BkFollow + Ensemble）
# sports    : 体育专项（OddsAPI + PreScreen + BkFollow 体育日志）
# weather   : 天气专项（GFS + ECMWF + Open-Meteo + CityScanner + StrikeOpt）
# risk      : 风控（原 risk）
# system    : 系统进化（原 evolution + 系统启动 + httpx + web_dashboard）
# errors    : 错误警告（原 error + warning）
_LOG_TYPE_PATTERNS: dict[str, list[str]] = {
    "trades": [
        "paper_trader",
        "outcome_tracker",
        "order_executor",
        "PaperBet",
        "ProfitTake",
        "StopLoss",
        "WriteOff",
        "止盈",
        "止损",
        "核销",
        "sell_position",
        "加仓",
        "OrderExecutor",
        # "下单" 已移除："不下单"(AmbushShadow影子行)含该子串会误匹配 trades
        "卖出",
        "CLOB",
        "fill",
        "reconcile",
        "Reconcile",
        "幽灵",
        "孤儿",
        "orphan",
        "ghost",
        "对账",
        "链上",
        "position_syncer",
        # T295-C: 补全覆盖
        "price_monitor",  # [PriceMonitor] 止损/止盈触发监控
        "PriceMonitor",
        "balance_provider",  # [RealtimeNLV] 余额/NLV 追踪
        "RealtimeNLV",
        "exit_policy",  # [ExitPolicy] 持仓退出决策
        "ExitPolicy",
        "orderbook_history",  # 订单历史
        # T300-C: 补全 copytrade
        "copy_trade_watcher",  # [CopyTrade] 跟单监控
        "copytrade",
        "CopyTrade",
        # T62: 合并原 profit 分类到 trades
        "trailing",
        "ceiling",
        "certainty_sell",
        "profit_take",
        "stop_loss",
        # T130: 冻结跳过信号（注意："下单"已移除，"不下单"含子串会误匹配）
        "冻结跳过",
        # NOTE: "下单" 故意不放这里——"不下单"(AmbushShadow)含该子串会误匹配
        # 真实下单日志由 OrderExecutor/order_executor/CLOB/PaperBet 覆盖
    ],
    "signal": [
        "signal_engine",
        "SignalEngine",
        "ensemble",
        "_call_ai",
        "AI信号",
        "market_scanner",
        "MarketScanner",
        "scanner",
        "BkFollow",
        "Phase0",
        "Phase1",
        "Phase2",
        "Ensemble",
        "OddsBlend",
        # T295-C: 补全覆盖
        "smart_money",  # [SmartMoney] 链上大户持仓信号
        "SmartMoney",
        "live_filters",  # 实盘信号过滤管线
        "odds_fetcher",  # [OddsFetcher] 博彩赔率数据
        "OddsFetcher",
        "crypto_fetcher",  # [CryptoFetcher]
        "crypto_probability",  # [CryptoAgg/CryptoEntry]
        "category_detector",  # 赛道分类
        "BookmakerGap",  # [BookmakerGap] Gap 检测
    ],
    "sports": [
        "T284",
        "T295",
        "BkFollow",
        "OddsBlend",
        "OddsPapi",
        "PreScreen",
        "moneyline",
        "bookmaker",
        "体育",
        "sports",
        "prescreen",
        "Phase0",
        "Phase1-BkFollow",
        "[T284",
        # T295-C: 补全覆盖
        "rapidapi_client",  # [RapidAPI] 体育赛事数据源
        "RapidAPI",
    ],
    "weather": [
        "weather_fetcher",
        "WeatherFetcher",
        "GFS",
        "Open-Meteo",
        "wttr",
        "ECMWF",
        "WeatherEngine",
        "天气",
        "skill_score",
        "CRPS",
        # T295-C: 补全覆盖
        "DualModel",  # [DualModel] GFS+ECMWF 双模型
        "WeatherAWS",  # [WeatherAWS-GFS]
        "WeatherGFS",
        "WeatherECMWF",
        "WeatherBatch",
        "WeatherCircuit",
        "WeatherNOMADS",
        "WUTempObserver",
        "WU-API",
        "NOAA",
        "HKO",
        # 天气扫描子流程
        "CityScanner",  # [CityScanner] 城市维度扫描
        "StrikeOpt",  # [StrikeOpt] 天气 Strike 优化器
        "CityNO",  # 城市NO策略标记
        "city_no",
        "城市NO",  # 中文日志匹配
        "city_scan",
        "strike_opt",
    ],
    "risk": [
        "risk_monitor",
        "RiskMonitor",
        "日亏",
        "累亏",
        "暂停",
        "熔断",
        "circuit",
        "RiskPause",
        "风控",
    ],
    "system": [
        "strategy_evolver",
        "Evolution",
        "evolve",
        "calibration",
        "系统启动",
        "初始化",
        "StrategyEvolver",
        "main",
        "bot_start",
        "scheduler",
        # T295-C: 补全覆盖
        "telegram_bot",  # [TelegramBot] Telegram 通知
        "telegram_notifier",
        "TelegramBot",
        "Telegram",
        "config_loader",  # 配置热加载
        "backtest",  # 回测引擎
        "db.",  # 数据库操作
        # T300-C: 补全缺失模块
        "httpx",  # httpx HTTP 客户端
        "polymarket_bot",  # 主入口模块
        "web_dashboard",  # Dashboard 本身
        "run_paper_trading",  # 运行脚本
    ],
    "errors": [
        " ERROR ",
        "Traceback",
        "Exception",
        "错误",
        " WARNING ",
        "警告",
        "CRITICAL",
        "Failed",
        "timeout",
        "连接失败",
    ],
    # ── T45-C: 新增加密货币 / 扫描 / 止盈止损 独立分类 ──
    "crypto": [
        "crypto_fetcher",
        "CryptoFetcher",
        "crypto_probability",
        "crypto_data_agg",
        "CryptoAgg",
        "CryptoEntry",
        "bitcoin",
        "ethereum",
        "CRYPTO_LIQ_SKIP",
        "CRYPTO_NO_SM_SKIP",
        "CRYPTO_SETTLE_SKIP",
        "crypto",
        "加密",
    ],
    "scanner": [
        "market_scanner",
        "MarketScanner",
        "scanner",
        "CityScanner",
        "StrikeOpt",
        "Tier1",
        "Tier2",
        "Tier3",
        "扫描",
        "拉取",
        "top_markets",
        "category_detector",
        "candidates",
    ],
    "profit": [
        "ProfitTake",
        "StopLoss",
        "stop_loss",
        "profit_take",
        "trailing",
        "ceiling",
        "certainty_sell",
        "exit_policy",
        "ExitPolicy",
        "止盈",
        "止损",
        "WriteOff",
        "核销",
    ],
    # ── 向后兼容：旧端点类型名称仍可用 ──
    "trading": [
        "paper_trader",
        "outcome_tracker",
        "PaperBet",
    ],
    "evolution": ["strategy_evolver", "Evolution", "evolve"],
    "error": [" ERROR ", "Traceback", "Exception", "错误"],
    "warning": [" WARNING ", "警告"],
    "executor": ["order_executor", "OrderExecutor", "下单"],
    "position": ["ProfitTake", "StopLoss", "WriteOff", "止盈", "止损"],
    "reconcile": ["reconcile", "对账", "链上"],
    "maker": [
        "[MakerSubmit]",
        "[MakerFullFill]",
        "[MakerPartialFill]",
        "[MakerTimeout]",
        "[TakerFallback]",
        "[TakerSubmit]",
    ],
    # T129→T130: YES 埋伏影子模式 + 冻结跳过中的 YES 信号筛选
    # 精确匹配：只保留含交易价格/金额的详情行，摘要行/硬屏蔽行/候选行通过 _LOG_TYPE_EXCLUDES 排除
    "yesa": [
        "AmbushShadow",  # 详情行格式: [AmbushShadow] City Date | Question | buy_price=... bet=$...
        "冻结跳过-YesA",  # 冻结状态下的 YES 信号（含实际价格+金额）
    ],
}

# 排除模式：每个 log_type 先按 _LOG_TYPE_PATTERNS 收录，再排除以下字符串命中的行
_LOG_TYPE_EXCLUDES: dict[str, list[str]] = {
    "yesa": [
        "影子模式,不下单",  # [AmbushShadow] 摘要行（无价格/金额，格式: X 个YES埋伏信号(影子模式,不下单)）
    ],
}


@router.get("/api/maker_stats")
async def api_maker_stats():
    """扫描所有历史日志文件，统计 Maker/Taker 订单执行情况（全量）。"""
    cached = _cache_get("maker_stats", 60)
    if cached is not None:
        return cached
    _MAKER_KEYS = [
        "MakerSubmit",
        "MakerFullFill",
        "MakerPartialFill",
        "MakerTimeout",
        "TakerFallback",
        "TakerSubmit",
    ]

    # 收集所有日志文件：bot.log + bot.log.* + paper_trading.log + paper_trading.log.*
    log_files: list = []
    if LOG_DIR and LOG_DIR.exists():
        for pattern in (
            "bot.log",
            "bot.log.*",
            "paper_trading.log",
            "paper_trading.log.*",
        ):
            log_files.extend(sorted(LOG_DIR.glob(pattern)))
    # 去重
    log_files = sorted(set(log_files), key=lambda p: p.name)

    counts: dict[str, int] = {k: 0 for k in _MAKER_KEYS}
    details: list[dict] = []
    total_lines_scanned = 0

    for lf in log_files:
        try:
            with open(lf, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    total_lines_scanned += 1
                    for key in _MAKER_KEYS:
                        tag = f"[{key}]"
                        if tag in line:
                            counts[key] += 1
                            # 提取时间戳和详情
                            ts = line[:23].strip() if len(line) > 23 else ""
                            details.append(
                                {
                                    "ts": ts,
                                    "type": key,
                                    "line": line.strip()[:300],
                                    "file": lf.name,
                                }
                            )
                            break  # 每行只匹配一个类型
        except Exception:
            continue

    # T179-E: 从日志中提取 market_id/orderID，与 paper_bets 关联获取 bet_id 和问题
    _mid_pattern = re.compile(
        r"\[(MakerSubmit|TakerSubmit|TakerFallback)\]\s+(\S+)\s+(YES|NO)"
    )
    # T207: 从 MakerSubmit 行提取 orderID → market_id 映射，
    # 用于关联 MakerFullFill/PartialFill/Timeout（只有 orderID 没有 market_id）
    _orderid_pattern = re.compile(r"orderID=(\S+)")
    _orderid_to_market: dict[str, tuple[str, str]] = (
        {}
    )  # orderID → (market_id, outcome)

    _market_ids_found: set[str] = set()
    for d in details:
        line = d.get("line", "")
        m = _mid_pattern.search(line)
        if m:
            d["market_id"] = m.group(2)
            d["outcome"] = m.group(3)
            _market_ids_found.add(m.group(2))
            # MakerSubmit: 同时提取 orderID 建立映射
            if d["type"] == "MakerSubmit":
                oid_m = _orderid_pattern.search(line)
                if oid_m:
                    _orderid_to_market[oid_m.group(1)] = (m.group(2), m.group(3))

    # T207: 用 orderID 映射为 FullFill/PartialFill/Timeout 补全 market_id
    for d in details:
        if d.get("market_id"):
            continue  # 已有 market_id
        if d["type"] in ("MakerFullFill", "MakerPartialFill", "MakerTimeout"):
            oid_m = _orderid_pattern.search(d.get("line", ""))
            if oid_m:
                mapped = _orderid_to_market.get(oid_m.group(1))
                if mapped:
                    d["market_id"] = mapped[0]
                    d["outcome"] = mapped[1]
                    _market_ids_found.add(mapped[0])

    _mid_to_bet: dict[str, dict] = {}
    if _market_ids_found:
        placeholders = ",".join("?" for _ in _market_ids_found)
        with db_connect() as conn:
            rows = conn.execute(
                f"SELECT bet_id, market_id, question, outcome, size_usdc, "
                f"market_price, ts_entered "
                f"FROM paper_bets WHERE market_id IN ({placeholders}) "
                f"ORDER BY ts_entered DESC",
                list(_market_ids_found),
            ).fetchall()
            for r in rows:
                key = f"{r['market_id']}_{r['outcome']}"
                if key not in _mid_to_bet:
                    _mid_to_bet[key] = {
                        "bet_id": r["bet_id"],
                        "question": (r["question"] or "")[:80],
                        "size_usdc": r["size_usdc"],
                        "market_price": r["market_price"],
                    }

    for d in details:
        mid = d.get("market_id", "")
        outcome = d.get("outcome", "")
        if mid:
            bet_info = _mid_to_bet.get(f"{mid}_{outcome}")
            if bet_info:
                d["bet_id"] = bet_info["bet_id"]
                d["question"] = bet_info["question"]
                d["size_usdc"] = bet_info["size_usdc"]
                d["market_price"] = bet_info["market_price"]
            else:
                # T207追加: 无 DB 记录（订单全部超时/取消），从日志行提取信息
                d["no_db_record"] = True
                # 从 MakerSubmit/TakerSubmit 日志中解析 cost 和 shares
                _submit_m = re.search(
                    r"shares=([\d.]+)\s+cost=([\d.]+)USDC", d.get("line", "")
                )
                if _submit_m:
                    d["log_shares"] = float(_submit_m.group(1))
                    d["log_cost"] = float(_submit_m.group(2))

    # 按时间排序（最新在前）
    details.sort(key=lambda x: x["ts"], reverse=True)

    total_maker = counts["MakerSubmit"]
    total_taker = counts["TakerSubmit"]
    full_fill_rate = (
        round(counts["MakerFullFill"] / total_maker, 4) if total_maker else 0
    )
    timeout_rate = round(counts["MakerTimeout"] / total_maker, 4) if total_maker else 0
    fallback_rate = (
        round(counts["TakerFallback"] / total_maker, 4) if total_maker else 0
    )

    result = {
        "counts": counts,
        "total_maker_orders": total_maker,
        "total_taker_orders": total_taker,
        "full_fill_rate": full_fill_rate,
        "timeout_rate": timeout_rate,
        "fallback_rate": fallback_rate,
        "scanned_lines": total_lines_scanned,
        "scanned_files": len(log_files),
        "details": details,
    }
    _cache_set("maker_stats", result)
    return result


@router.get("/api/evolution/status")
async def api_evolution_status():
    """自进化分析状态概览。"""
    cached = _cache_get("evolution_status", 30)
    if cached is not None:
        return cached
    from src.utils.db import (
        get_calibration_bins,
        get_evolution_history,
        get_model_stats,
    )

    global_bins = get_calibration_bins(category="", min_samples=1, db_path=DB_PATH)
    total_samples = sum(b["total"] for b in global_bins)
    diffs = [
        b["calibrated_rate"] - b["midpoint"] for b in global_bins if b["total"] >= 3
    ]
    avg_diff = sum(diffs) / len(diffs) if diffs else 0
    if avg_diff < -0.03:
        bias = "OVERCONFIDENT"
    elif avg_diff > 0.03:
        bias = "UNDERCONFIDENT"
    else:
        bias = "BALANCED" if diffs else "UNKNOWN"

    models = get_model_stats(recent_n=200, db_path=DB_PATH)
    recent_evol = get_evolution_history(limit=1, db_path=DB_PATH)
    last_evolve_ts = recent_evol[0]["ts"] if recent_evol else None

    result = {
        "enabled": bool((get("evolution") or {}).get("enabled", True)),
        "global_calibration_active": total_samples
        >= int((get("evolution") or {}).get("min_samples", 30)),
        "global_calibration_samples": total_samples,
        "global_calibration_bias": bias,
        "model_count": len(models),
        "model_weights": {k: v["weight"] for k, v in models.items()},
        "last_evolve_ts": last_evolve_ts,
        "total_adjustments": len(get_evolution_history(limit=9999, db_path=DB_PATH)),
    }
    _cache_set("evolution_status", result)
    return result


@router.get("/api/evolution/calibration")
async def api_evolution_calibration():
    """校准曲线数据（全局 + 各赛道）。"""
    from src.utils.db import get_calibration_bins

    global_bins = get_calibration_bins(category="", min_samples=1, db_path=DB_PATH)
    by_category = {}
    for cat in ["weather", "crypto", "geopolitics", "finance", "tech", "economy"]:
        cat_bins = get_calibration_bins(category=cat, min_samples=1, db_path=DB_PATH)
        cat_total = sum(b["total"] for b in cat_bins)
        if cat_total > 0:
            by_category[cat] = cat_bins
    return {"global": global_bins, "by_category": by_category}


@router.get("/api/evolution/history")
async def api_evolution_history(limit: int = Query(50, ge=1, le=500)):
    """进化参数调整历史。"""
    from src.utils.db import get_evolution_history

    history = get_evolution_history(limit=limit, db_path=DB_PATH)
    return {"history": history, "total": len(history)}


@router.get("/api/evolution/models")
async def api_evolution_models():
    """模型表现统计。"""
    from src.utils.db import get_model_stats

    stats = get_model_stats(recent_n=500, db_path=DB_PATH)
    return {"models": stats}


@router.get("/api/evolution/residuals")
async def api_evolution_residuals():
    """标准化残差分析 — 识别系统性亏损模式。"""
    from src.evolution.strategy_evolver import StandardizedResidualAnalyzer

    analyzer = StandardizedResidualAnalyzer(db_path=DB_PATH)
    return analyzer.analyze()


@router.post("/api/evolution/trigger")
async def api_evolution_trigger(request: Request):
    """从 Dashboard 手动触发一次进化周期（忽略 interval_bets/interval_hours 检查）。

    需要 admin / super_admin 权限（T901）：该操作会重写策略参数，属全局状态变更。
    """
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    import logging

    logger = logging.getLogger("dashboard.evolution")
    try:
        from src.evolution.strategy_evolver import StrategyEvolver

        evolver = StrategyEvolver(db_path=DB_PATH)
        result = evolver.evolve()
        adjustments = result.get("adjustments", []) if isinstance(result, dict) else []
        logger.info("[Dashboard] 手动触发进化完成，调整 %d 项", len(adjustments))
        return {
            "status": "ok",
            "adjustments": len(adjustments),
            "detail": adjustments,
        }
    except Exception as exc:
        logger.exception("[Dashboard] 手动触发进化失败: %s", exc)
        return {"status": "error", "error": str(exc)}


@router.get("/api/calibration")
async def api_calibration():
    """EV 校准 + 每日 P&L。"""
    cached = _cache_get("calibration", 30)
    if cached is not None:
        return cached
    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_bets WHERE resolved_outcome IS NOT NULL ORDER BY resolved_at"
            ).fetchall()
        bets = _rows_to_dicts(rows)

        buckets = [
            ("0.02-0.05", 0.02, 0.05),
            ("0.05-0.10", 0.05, 0.10),
            ("0.10-0.20", 0.10, 0.20),
            ("0.20+", 0.20, 999),
        ]
        calibration = []
        for label, lo, hi in buckets:
            group = [b for b in bets if lo <= (b.get("ev") or 0) < hi]
            if not group:
                calibration.append({"range": label, "count": 0})
                continue
            wins = sum(1 for b in group if b.get("is_win"))
            pnl = sum(b.get("pnl") or 0 for b in group)
            staked = sum(b.get("size_usdc") or 0 for b in group)
            calibration.append(
                {
                    "range": label,
                    "count": len(group),
                    "win_rate": round(wins / len(group), 4),
                    "roi": round(pnl / staked, 4) if staked > 0 else 0,
                }
            )

        daily = defaultdict(lambda: {"pnl": 0, "count": 0, "wins": 0})
        for b in bets:
            ts = _safe_ts(b.get("resolved_at"))
            if ts <= 0:
                continue
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            daily[day]["pnl"] += b.get("pnl") or 0
            daily[day]["count"] += 1
            if b.get("is_win"):
                daily[day]["wins"] += 1

        daily_list = [
            {"date": k, "pnl": round(v["pnl"], 2), "count": v["count"], "wins": v["wins"]}
            for k, v in sorted(daily.items())
        ]
        result = {"calibration": calibration, "daily_pnl": daily_list}
        _cache_set("calibration", result)
        return result
    return await asyncio.to_thread(_db)


# ── 流动性 API ────────────────────────────────────────────────────────


@router.get("/api/liquidity/open-bets")
async def api_liquidity_open_bets():
    """为所有持仓中的市场获取流动性分析。"""
    cached = _cache_get("liquidity_open_bets", 10)
    if cached is not None:
        return cached
    from src.utils.orderbook_history import analyze_liquidity, fetch_orderbook
    def _db():

        with db_connect() as conn:
            rows = conn.execute(
                "SELECT market_id, question, category, outcome, market_price, size_usdc, "
                "ai_probability, ev, ts_entered, condition_id, market_slug "
                "FROM paper_bets WHERE resolved_at IS NULL AND market_id IS NOT NULL "
                "ORDER BY ts_entered DESC"
            ).fetchall()
        bets = _rows_to_dicts(rows)

        results = []
        for b in bets:
            market_id = b.get("market_id")
            if not market_id:
                continue
            liq: dict = {
                "spread": None,
                "best_bid": None,
                "best_ask": None,
                "count": 0,
            }
            try:
                # 优先使用 condition_id 查询（copy_trade 的 market_id 就是 condition_id）
                cond_id = b.get("condition_id") or market_id
                slug = b.get("market_slug") or ""
                resp = None
                # 策略1: 用 slug 查询（最可靠）
                if slug:
                    resp = pm_session().get(
                        "https://gamma-api.polymarket.com/markets",
                        params={"slug": slug},
                        timeout=5,
                    )
                    if resp.ok:
                        data = resp.json()
                        mkt = data[0] if isinstance(data, list) and data else data
                    else:
                        resp = None
                # 策略2: 用 condition_id 查询
                if not resp:
                    resp = pm_session().get(
                        "https://gamma-api.polymarket.com/markets",
                        params={"condition_id": cond_id},
                        timeout=5,
                    )
                    if resp.ok:
                        data = resp.json()
                        mkt = data[0] if isinstance(data, list) and data else data
                    else:
                        mkt = {}
                else:
                    pass  # mkt already set above
                clob_ids = mkt.get("clobTokenIds") if mkt else None
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = json.loads(clob_ids)
                    except (json.JSONDecodeError, ValueError):
                        clob_ids = None
                if clob_ids and isinstance(clob_ids, list):
                    outcome_str = (b.get("outcome") or "YES").upper()
                    token_idx = 0 if outcome_str == "YES" else min(1, len(clob_ids) - 1)
                    token_id = (
                        clob_ids[token_idx] if token_idx < len(clob_ids) else clob_ids[0]
                    )
                    book = fetch_orderbook(token_id)
                    liq = analyze_liquidity(book or {})
            except Exception:
                pass
            results.append(
                {
                    "question": b.get("question") or "",
                    "category": b.get("category") or "",
                    "market_id": b.get("market_id") or "",
                    "market_url": f"https://polymarket.com/event/{b.get('market_slug') or b.get('market_id') or ''}",
                    "outcome": b.get("outcome"),
                    "entry_price": b.get("market_price"),
                    "size_usdc": b.get("size_usdc"),
                    "ai_probability": b.get("ai_probability"),
                    "ev": b.get("ev"),
                    "spread": liq.get("spread"),
                    "best_bid": liq.get("best_bid"),
                    "best_ask": liq.get("best_ask"),
                    "snapshots": liq.get("count", 0),
                }
            )

        result = {"markets": results, "total": len(results)}
        _cache_set("liquidity_open_bets", result)
        return result
    return await asyncio.to_thread(_db)


# ------------------------------------------------------------------
# T239: 止损事件结构化日志 API
# ------------------------------------------------------------------


@router.get("/api/stop-loss/events")
async def api_stop_loss_events(
    limit: int = Query(50, ge=1, le=500),
    category: str = Query(""),
    trigger_type: str = Query(""),
    days: int = Query(0, ge=0),
):
    """查询止损事件列表。"""
    from src.utils.db import get_stop_loss_events

    events = get_stop_loss_events(
        limit=limit, category=category, trigger_type=trigger_type, days=days
    )
    return {"events": events, "total": len(events)}


@router.get("/api/stop-loss/summary")
async def api_stop_loss_summary(days: int = Query(7, ge=1, le=365)):
    """止损事件汇总统计。"""
    from src.utils.db import get_stop_loss_summary

    return get_stop_loss_summary(days=days)


@router.get("/api/city-no/performance")
async def api_city_no_performance():
    """城市NO策略累计表现：总投注/胜率/PnL + 按城市明细。"""
    from src.utils.db import query_city_no_performance

    return query_city_no_performance(db_path=DB_PATH)
