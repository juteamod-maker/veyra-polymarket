"""
discover_routes.py — T289 Wave 5 + Wave 6

PM 事件发现、详情、下单页的后端 API。
- GET  /api/pm/events            Gamma /events 代理（带 TTL 缓存）
- GET  /api/pm/event/{slug}      单 event 详情（Gamma /events/slug/{slug}）
- GET  /api/pm/orderbook          CLOB /book?token_id=... 代理
- GET  /api/pm/deposit-info      返回用户 funder_address + USDC 充值指引
- GET  /api/pm/balance-check     实时 USDC 余额 + 最低阈值校验
- POST /api/pm/trade/submit      手动下单（走 ClobClientFactory per-user funder）

下单入口改走 Wave 4 中心化工厂（get_user_gateway），保证 signature_type=2 + 每租户 Safe 隔离。
所有下单前强制 _fetch_usdc_balances(funder).usdc_total >= config.trading.min_usdc_to_trade。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
import time as _time_mod  # T510 P0-3 process-level category cache
from datetime import datetime, timezone
from threading import Lock
from typing import Any

# T510 P0-3: /api/pm/categories 进程级缓存
_PM_CATEGORIES_CACHE: dict = {}
_PM_CATEGORIES_TTL = 300

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from scripts.dashboard_routes._shared import (
    db_connect,
    get,
    get_request_identity,
)
from scripts.dashboard_routes._validators import (
    validate_amount,
    validate_condition_id,
    validate_token_id,
    validate_token_id_list,
)

try:
    from src.utils.translator import (
        get_cached as _i18n_get_cached,
        translate_event as _i18n_translate,
    )
except Exception:  # 防御: 翻译模块可选
    _i18n_get_cached = None  # type: ignore
    _i18n_translate = None  # type: ignore

logger = logging.getLogger("web_dashboard")
router = APIRouter()

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_GAMMA_API = "https://gamma-api.polymarket.com"
_CLOB_PUBLIC = "https://clob.polymarket.com"
_DEFAULT_POLYGON_RPCS = [
    # T290 修复：原三个 RPC 全部失效导致 PM 钱包余额读取为 0
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
]


def _polygon_rpcs() -> list[str]:
    try:
        cfg = get("chain.polygon_rpcs")
        if isinstance(cfg, list) and cfg:
            return [str(x) for x in cfg if str(x).startswith("http")]
    except Exception:
        pass
    return list(_DEFAULT_POLYGON_RPCS)


_POLYGON_RPCS = _DEFAULT_POLYGON_RPCS  # backward-compat
# T471: Polymarket CLOB v2 cutover (2026-04-28). Trading collateral is now
# pUSD; USDC.e auto-wraps via the Safe-side CollateralOnramp on receive. We
# keep USDC.e and native USDC reads for transparency on the deposit page.
from src.utils import pm_v2_constants as _pm_v2  # noqa: E402
from src.utils.pm_http import pm_session as _pm_session  # noqa: E402  T510 P0-1

# T579-FU2 — 加载即启动 13 类 PM 标签 + 标签↔事件 同步守护（leader-only）
try:
    from scripts.dashboard_routes import (
        pm_tags_sync as _pm_tags_sync_module,
    )  # noqa: F401, E402
except Exception as _exc_pts_init:  # pragma: no cover
    import logging as _logging_pts

    _logging_pts.getLogger(__name__).warning(
        "pm_tags_sync import failed: %s", _exc_pts_init
    )

_USDC_NATIVE_TOKEN = _pm_v2.USDC_NATIVE_TOKEN
_USDC_E_TOKEN = _pm_v2.USDC_E_TOKEN
_PUSD_TOKEN = _pm_v2.PUSD_TOKEN

# ---------------------------------------------------------------------------
# TTL 缓存
# ---------------------------------------------------------------------------

_events_cache: dict[str, tuple[float, Any]] = {}
_events_cache_lock = Lock()


def _cache_ttl_sec() -> int:
    try:
        return int(get("discover.events_cache_ttl_sec") or 300)
    except Exception:
        return 300


def _search_translation_slug_hits(event_slugs: list[str], q_lower: str) -> set[str]:
    """Return event slugs whose translated text matches keyword across all languages."""
    if not event_slugs or not q_lower:
        return set()
    clean_slugs = [
        str(s or "").strip().lower() for s in event_slugs if str(s or "").strip()
    ]
    if not clean_slugs:
        return set()
    like_q = f"%{q_lower}%"
    placeholders = ",".join(["%s"] * len(clean_slugs))
    sql = (
        "SELECT DISTINCT slug FROM event_translations "
        f"WHERE slug IN ({placeholders}) "
        "AND (LOWER(COALESCE(title,'')) LIKE %s OR LOWER(COALESCE(description,'')) LIKE %s)"
    )
    try:
        with db_connect() as conn:
            rows = conn.execute(sql, tuple(clean_slugs) + (like_q, like_q)).fetchall()
        return {
            str((r or {}).get("slug") or "").strip().lower()
            for r in (rows or [])
            if (r or {}).get("slug")
        }
    except Exception as exc:
        logger.debug("translation keyword search failed: %s", exc)
        return set()


def _friendly_clob_error(raw: str) -> str:
    """T311: 将 PolyApiException 等原始错误转译为人类可读的中文友好提示。"""
    if not raw:
        return "下单失败，请稍后重试"
    s = str(raw)
    sl = s.lower()
    # 常见 CLOB 错误模式 → 中文友好提示
    if "invalid amount for a marketable" in sl and "min size" in sl:
        # e.g. "invalid amount for a marketable BUY order ($0.99), min size: $1"
        # T315: 区分订单 notional 与用户输入金额。该错误是订单价值 (price×size) 不足 $1，
        # 而非用户输入金额不足。提示必须包含实际订单价值，避免“我明明输了$1”的困惑。
        import re as _re

        m = _re.search(r"min size:\s*\$?([\d.]+)", s)
        min_v = m.group(1) if m else "1"
        m2 = _re.search(r"\(\$?([\d.]+)\)", s)
        actual_v = m2.group(1) if m2 else ""
        if actual_v:
            return (
                f"订单价值不足 — PM 要求订单价值 (价格×份数) ≥ ${min_v}，但本单仅 ${actual_v}。"
                f"请适当提高金额或检查当前价格后重试。"
            )
        return f"订单价值不足 — PM 要求订单价值 (价格×份数) ≥ ${min_v}，请适当提高金额后重试"
    if "lower than the minimum" in sl and "size" in sl:
        # e.g. "Size (2) lower than the minimum: 5"
        import re as _re

        m = _re.search(r"minimum:\s*([\d.]+)", s)
        min_v = m.group(1) if m else "5"
        return (
            f"下单份数不足 — Polymarket 要求最小份数 {min_v} shares，请提高份数后重试"
        )
    if "tick size" in sl or "tick_size" in sl:
        return "价格精度不符合市场最小单位（tickSize），请调整价格"
    # T314 追加 C: PM CLOB 价格精度对应的 size 最小整数倍约束错误。原始示例：
    #   "size_too_small_after_precision_snap: price=0.01 要求 size 为 1.00 的整数倍"
    # 触发场景：低价格市场（price ≤ 0.05 时 step 通常 ≥ 1.0）持有零碎份数（如 0.12 share）
    # 时无法主动卖出，因为 PM 平台硬约束 size 必须是 step 的整数倍。
    # 这是平台硬约束，技术上不可绕过。必须给出可操作的解决方案。
    if "size_too_small_after_precision_snap" in sl or (
        "precision_snap" in sl and "size" in sl
    ):
        import re as _re

        m_p = _re.search(r"price\s*=\s*([\d.]+)", sl)
        m_step = _re.search(
            r"size\s*[为=]\s*([\d.]+)\s*的整数倍|multiple of\s*([\d.]+)", s
        )
        price = m_p.group(1) if m_p else "?"
        step = "?"
        if m_step:
            step = m_step.group(1) or m_step.group(2) or "?"
        return (
            f"零碎份数无法卖出 — 当前价格 ${price} 下，Polymarket 要求卖单份数必须是 "
            f"{step} 的整数倍（平台硬约束）。建议：① 等待市场最终结算后系统将自动核销；"
            f"② 或前往 Polymarket 官方页 polymarket.com 手动操作（官方页同样受此约束，"
            f"但可与其他持仓合并处理）；③ 切勿为凑整而加买，可能扩大亏损。"
        )
    # T314 追加: PM 卖出余额不足的微单位错误。原始：
    # "not enough balance / allowance: the balance is not enough -> balance: 121578, order amount: 2000000"
    # 微单位 / 1e6 = 实际 share 数。该错误极常见于刚卖出后 Data API 缓存未刷新就再次点击的情况。
    import re as _re

    m_micro = _re.search(
        r"balance is not enough.*?balance:\s*(\d+).*?order amount:\s*(\d+)", sl
    )
    if m_micro:
        try:
            bal_shares = int(m_micro.group(1)) / 1_000_000
            req_shares = int(m_micro.group(2)) / 1_000_000
            return (
                f"持仓余额不足 — 实际可卖仅 {bal_shares:.4f} 份，本次请求 {req_shares:.4f} 份。"
                f"该份数可能已被刚才的订单卖出（链上数据约 5-30 秒后才会刷新）。"
                f"请刷新页面重新加载持仓后再操作。"
            )
        except Exception:
            pass
    if "insufficient" in sl and ("balance" in sl or "fund" in sl or "allowance" in sl):
        return "钱包余额不足，请先充值 USDC 后重试"
    if "not enough" in sl and ("balance" in sl or "allowance" in sl):
        # 兜底：未匹配微单位 + 未匹配 'order amount' 时
        return (
            "钱包余额或持仓余额不足，请检查后重试（如刚下单请稍等 5-30 秒后刷新页面）"
        )
    if (
        "not enough balance" in sl or "not enough allowance" in sl
    ) and "order amount" in sl:
        # 兜底：未能解析微单位时
        return "持仓余额不足以完成此次卖出 — 持仓可能已被刚才的订单卖出。请刷新页面重新加载持仓后再操作"
    if "not enough shares" in sl or "size exceeds" in sl:
        return "持仓份数不足以完成此次卖出，请检查持仓"
    if "invalid price" in sl:
        return "价格无效，请输入 0.01–0.99 之间的合法价格"
    if "invalid size" in sl:
        return "份数无效，请检查输入"
    if "expired" in sl or "timeout" in sl:
        return "订单超时，请稍后重试"
    if "unauthorized" in sl or "forbidden" in sl:
        return "权限不足，请重新登录或绑定钱包"
    # T884.2: Privy server-signer 无权对 CTFExchange Order 做 EIP-712 签名（需要在
    # Privy Dashboard 给嵌入式钱包加 policy 允许该合约 / 签名方法）。给用户清晰文案。
    if (
        "no valid authorization keys" in sl
        or "user signing keys" in sl
        or "privy wallet rpc failed" in sl
        or "privy_signing_failed" in sl
    ):
        return (
            "下单签名服务暂不可用 — 系统正在为你的嵌入式钱包配置 Polymarket "
            "下单授权（Privy 策略升级中）。提现等其他功能不受影响，请稍后再试。"
        )
    if "missing_signer_binding" in sl or "privy embedded wallet not enrolled" in sl:
        return (
            "嵌入式钱包尚未完成下单授权绑定，请退出后重新登录一次以触发自动绑定。"
        )
    # 兜底：截取关键 error_message 字段
    import re as _re

    m = _re.search(r"['\"]error['\"]\s*:\s*['\"]([^'\"]+)['\"]", s)
    if m:
        return f"下单失败：{m.group(1)}"
    # 长字符串截断
    if len(s) > 160:
        return f"下单失败：{s[:160]}…"
    return f"下单失败：{s}"


def _cache_get(key: str) -> Any | None:
    with _events_cache_lock:
        item = _events_cache.get(key)
        if not item:
            return None
        ts, data = item
        if time.time() - ts > _cache_ttl_sec():
            _events_cache.pop(key, None)
            return None
        return data


def _cache_get_any(key: str) -> tuple[Any | None, float | None]:
    """T496: 返回缓存内容和年龄（秒），即使已过期也返回 — 用于 stale-while-revalidate。"""
    with _events_cache_lock:
        item = _events_cache.get(key)
        if not item:
            return None, None
        ts, data = item
        return data, max(0.0, time.time() - ts)


def _cache_put(key: str, data: Any) -> None:
    with _events_cache_lock:
        _events_cache[key] = (time.time(), data)


# ---------------------------------------------------------------------------
# T496: stale-while-revalidate + 后台预热
# ---------------------------------------------------------------------------

_inflight_refresh: set[str] = set()
_inflight_lock = Lock()


def _swr_threshold_sec() -> int:
    """缓存可继续作为 stale 使用的最大年龄（=ttl 后多少秒内仍可吐 stale 并后台刷新）。"""
    try:
        return int(get("discover.events_swr_extra_sec") or 60)
    except Exception:
        return 60


def _public_cache_headers(max_age: int = 15, swr: int = 60) -> dict[str, str]:
    """公开 GET 接口的 CDN/浏览器缓存头。"""
    return {
        "Cache-Control": f"public, max-age={max_age}, s-maxage={max_age * 2}, stale-while-revalidate={swr}",
        "Vary": "Accept-Encoding",
    }


def _trigger_background_refresh(cache_key: str, refresh_fn) -> None:
    """异步触发同一个 cache_key 的刷新，避免重复请求。"""
    with _inflight_lock:
        if cache_key in _inflight_refresh:
            return
        _inflight_refresh.add(cache_key)

    def _run():
        try:
            refresh_fn()
        except Exception as exc:
            logger.debug("swr refresh failed key=%s: %s", cache_key, exc)
        finally:
            with _inflight_lock:
                _inflight_refresh.discard(cache_key)

    try:
        import threading

        threading.Thread(target=_run, name=f"swr-{cache_key[:24]}", daemon=True).start()
    except Exception:
        with _inflight_lock:
            _inflight_refresh.discard(cache_key)


# T496: 启动后台预热守护线程 — 在模块加载时立刻缓存最常用的 tab=all/hot，
# 并按 TTL-30s 周期刷新，确保线上访问 /discover 永远命中热缓存（毫秒级首屏）。
_warmer_started = False
_warmer_lock = Lock()


def _quantized_end_date_min() -> str:
    """T532 #532 — return a UTC ISO timestamp truncated to the current
    minute. Used as the Gamma `end_date_min` filter across the warmer, the
    SSR injection and the live `/api/pm/events` endpoint so all three share
    the SAME cache_key and SSR actually hits the warmer-populated cache.
    """
    from datetime import datetime as _dt, timezone as _tz

    now = _dt.now(_tz.utc).replace(second=0, microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_warm_payload(
    tab: str,
    limit: int = 18,
    tag_slug: str = "",
    tag_id: int | None = None,
) -> tuple[str, dict]:
    """构造与 pm_events 完全一致的 cache_key 与 payload，用于后台预热。

    T496+ Wave 5A: tag_slug/tag_id 同时支持赛道分类预热（如 pop-culture/sports/crypto），
    保证 /discover?p=<slug> 这类切换也走毫秒级 SSR。
    """
    sort_key = "volume24hr"
    # T529 #529 — also filter at the Gamma source so events whose end_date
    # has already passed (but settlement hasn't run yet so closed=false)
    # never reach the warmer cache. Without this the weather/sport-of-the-day
    # tabs were filled with "Ended" cards that point to dead markets.
    # T532 #532 — quantize to a 60s bucket so the warmer + SSR + pm_events
    # endpoints all hit the SAME cache_key (otherwise each request stamps
    # its own second and cache reuse is zero, and SSR served pre-T529
    # entries with expired weather rows).
    _now_iso = _quantized_end_date_min()
    params: dict[str, Any] = {
        "limit": limit,
        "offset": 0,
        "order": sort_key,
        "ascending": "false",
        "active": "true",
        "closed": "false",
        "end_date_min": _now_iso,
    }
    if tag_slug:
        params["tag_slug"] = tag_slug
    if tag_id is not None:
        params["tag_id"] = tag_id
    cache_key = json.dumps(
        {**params, "search": "", "tab": tab, "lang": "", "slim": 1},
        sort_keys=True,
    )
    resp = _pm_session().get(f"{_GAMMA_API}/events", params=params, timeout=8)
    resp.raise_for_status()
    raw = resp.json()
    events = [_normalise_event(e, slim=True) for e in raw if isinstance(e, dict)]
    has_more = len(events) >= limit
    next_cursor = ""
    if has_more:
        try:
            import base64

            next_cursor = (
                base64.urlsafe_b64encode(json.dumps({"offset": limit}).encode())
                .decode()
                .rstrip("=")
            )
        except Exception:
            next_cursor = ""
    payload = {
        "count": len(events),
        "limit": limit,
        "offset": 0,
        "sort": sort_key,
        "tab": tab,
        "tag_slug": tag_slug or None,
        "tag_id": tag_id,
        "lang": None,
        "events": events,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }
    return cache_key, payload


def _warmer_top_categories(max_n: int = 12) -> list[tuple[str, int | None]]:
    """T496+ Wave 5A: 读取 pm_categories 前 N 个活跃分类用于持续预热。

    pm_tags 中带 tag_id 的同步解析（双保险，与 pm_events 主路径一致）。
    """
    out: list[tuple[str, int | None]] = []
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT slug FROM pm_categories WHERE active=1 ORDER BY label LIMIT ?",
                (max_n,),
            ).fetchall()
            for r in rows:
                slug = str(r["slug"] or "").strip().lower()
                if not slug:
                    continue
                tag_id: int | None = None
                try:
                    trow = conn.execute(
                        "SELECT tag_id FROM pm_tags WHERE slug=? AND active=1 LIMIT 1",
                        (slug,),
                    ).fetchone()
                    if trow:
                        tag_id = int(trow["tag_id"])
                except Exception:
                    pass
                out.append((slug, tag_id))
    except Exception as exc:
        logger.debug("warmer_top_categories failed: %s", exc)
    return out


def _warmer_loop() -> None:
    import time as _t

    # 启动时立刻 warm 一次（5s 后，给上游 + 进程一点缓冲）
    _t.sleep(5)
    while True:
        # 默认 tab：all + hot（无分类）
        for tab in ("all", "hot"):
            try:
                ck, payload = _build_warm_payload(tab, limit=18)
                _cache_put(ck, payload)
            except Exception as exc:
                logger.debug("warmer tab=%s failed: %s", tab, exc)
        # T496+ Wave 5A: 顶部分类同样持续预热（tab=all + 各 slug），
        # 让 /discover?p=<slug> 切换瞬间命中热缓存。
        for slug, tag_id in _warmer_top_categories(max_n=12):
            try:
                ck, payload = _build_warm_payload(
                    "all",
                    limit=18,
                    tag_slug=slug,
                    tag_id=tag_id,
                )
                _cache_put(ck, payload)
            except Exception as exc:
                logger.debug("warmer cat=%s failed: %s", slug, exc)
        # 在 TTL 到期前 30s 重新刷新；最少 30s
        try:
            sleep_sec = max(30, _cache_ttl_sec() - 30)
        except Exception:
            sleep_sec = 60
        _t.sleep(sleep_sec)


def _ensure_warmer_started() -> None:
    global _warmer_started
    with _warmer_lock:
        if _warmer_started:
            return
        # Phase-2: Gunicorn 多 worker 下，只有 leader worker 启动 warmer，
        # 避免上游 Polymarket 被打 N 倍。其它 worker 直接复用 leader 灌入
        # 的 _events_cache（注：不同进程间缓存不共享，但 leader 维护的
        # 上游热数据足够支撑命中率）。
        try:
            from src.utils.process_leader import try_acquire_leader

            if not try_acquire_leader("pm_events_warmer"):
                _warmer_started = True  # 标记本 worker 已"决策完毕"，不再重试
                logger.info(
                    "T496 pm_events warmer skipped on non-leader worker pid=%d",
                    __import__("os").getpid(),
                )
                return
        except Exception as exc:
            logger.warning(
                "warmer leader check failed: %s — falling back to start", exc
            )
        try:
            import threading

            threading.Thread(
                target=_warmer_loop, name="pm-events-warmer", daemon=True
            ).start()
            _warmer_started = True
            logger.info("T496 pm_events warmer thread started")
        except Exception as exc:
            logger.warning("failed to start pm_events warmer: %s", exc)


_ensure_warmer_started()


def _apply_ssr_translations(payload: dict, lang: str) -> dict:
    """T555 — In-place translate event titles + market questions for SSR.

    Reads ONLY from the translator memory/db cache (`_i18n_get_cached`) so
    SSR remains synchronous and millisecond-fast. Missing translations are
    fired off to `_i18n_translate_async` so they're warm for the user's
    next visit / category switch. Returns a SHALLOW-copied payload with a
    deep-copied events list so we never pollute the discover-events warmer
    cache.
    """
    if not payload or not lang or lang == "en" or _i18n_get_cached is None:
        return payload
    try:
        import copy as _copy

        out = dict(payload)
        translated_events: list[dict] = []
        for ev in payload.get("events") or []:
            if not isinstance(ev, dict):
                translated_events.append(ev)
                continue
            ev_copy = _copy.copy(ev)
            ev_slug = ev_copy.get("slug") or ""
            tr = _i18n_get_cached(ev_slug, lang) if ev_slug else None
            markets_tr = (tr or {}).get("markets") or {}
            if tr and tr.get("title"):
                ev_copy["title_src"] = ev_copy.get("title", "")
                ev_copy["title"] = tr["title"]
                ev_copy["i18n_lang"] = lang
            missing_market_slugs: list[str] = []
            new_markets: list = []
            for m in ev_copy.get("markets") or []:
                if not isinstance(m, dict):
                    new_markets.append(m)
                    continue
                m_copy = dict(m)
                m_slug = m_copy.get("slug") or ""
                q_tr = markets_tr.get(m_slug) if m_slug else None
                if q_tr:
                    m_copy["question_src"] = m_copy.get("question", "")
                    m_copy["question"] = q_tr
                elif m_slug and m_copy.get("question"):
                    missing_market_slugs.append(m_slug)
                new_markets.append(m_copy)
            if new_markets:
                ev_copy["markets"] = new_markets
            translated_events.append(ev_copy)
        out["events"] = translated_events
        out["lang"] = lang
        return out
    except Exception as exc:
        logger.debug("_apply_ssr_translations failed: %s", exc)
        return payload


def get_ssr_payload(
    tab: str = "all",
    limit: int = 18,
    p: str = "",
    t: str = "",
    lang: str = "",
) -> dict | None:
    """T496: 给 frontend_routes 的 /discover 页 SSR 嵌入用 — 直接读热缓存，不发起任何外网请求。

    T496+ Wave 5A: 增加 p/t 参数，把分类 slug 也纳入 SSR 注入路径；缓存未命中时
    最多做一次 1.2s 短超时的同步上游拉取（避免页面渲染挂起），失败/超时静默退回 None。

    T555: lang 参数从 cookie 读取，对返回的 events 做 in-memory 翻译（不污染
    上游事件缓存），让中文/其他语种用户首屏即看到本地化标题。

    返回与 /api/pm/events 完全一致的 payload；缓存未命中时返回 None（页面退化为客户端水合）。
    """
    lang_norm = (lang or "").strip()
    try:
        sort_key = "volume24hr"
        # 与 pm_events 主路径一致的 tag 解析
        tag_slug_filter = (t or p or "").strip().lower()
        tag_id_filter: int | None = None
        if tag_slug_filter:
            try:
                with db_connect() as conn:
                    row = conn.execute(
                        "SELECT tag_id FROM pm_tags WHERE slug=? AND active=1 LIMIT 1",
                        (tag_slug_filter,),
                    ).fetchone()
                    if row:
                        tag_id_filter = int(row["tag_id"])
            except Exception:
                pass
        params: dict[str, Any] = {
            "limit": limit,
            "offset": 0,
            "order": sort_key,
            "ascending": "false",
            "active": "true",
            "closed": "false",
            # T532 #532 — must mirror _build_warm_payload exactly so the
            # cache_key matches the warmer-populated entry. Without
            # end_date_min here, SSR misses cache and either falls back to
            # a stale entry (with already-expired events) or pays a 1.2s
            # synchronous refresh on every cold category visit.
            "end_date_min": _quantized_end_date_min(),
        }
        if tag_slug_filter:
            params["tag_slug"] = tag_slug_filter
        if tag_id_filter is not None:
            params["tag_id"] = tag_id_filter
        cache_key = json.dumps(
            {**params, "search": "", "tab": tab, "lang": "", "slim": 1},
            sort_keys=True,
        )
        cached = _cache_get(cache_key)
        if cached is not None:
            return _apply_ssr_translations(cached, lang_norm)
        stale, age = _cache_get_any(cache_key)
        if (
            stale is not None
            and age is not None
            and age <= (_cache_ttl_sec() + _swr_threshold_sec())
        ):
            try:

                def _refresh():
                    try:
                        _, payload = _build_warm_payload(
                            tab,
                            limit=limit,
                            tag_slug=tag_slug_filter,
                            tag_id=tag_id_filter,
                        )
                        _cache_put(cache_key, payload)
                    except Exception:
                        pass

                _trigger_background_refresh(cache_key, _refresh)
            except Exception:
                pass
            return _apply_ssr_translations(stale, lang_norm)
        # T496+ Wave 5A: 真正冷未命中（用户首次进入未预热的分类）→ 同步 1.2s 短超时拉一次。
        # 拉到就同时灌入缓存供后续 navigation 命中；超时则返回 None 让前端退化为水合。
        try:
            resp = _pm_session().get(f"{_GAMMA_API}/events", params=params, timeout=1.2)
            if resp.status_code == 200:
                raw = resp.json() or []
                events = [
                    _normalise_event(e, slim=True) for e in raw if isinstance(e, dict)
                ]
                has_more = len(events) >= limit
                next_cursor = ""
                if has_more:
                    try:
                        import base64

                        next_cursor = (
                            base64.urlsafe_b64encode(
                                json.dumps({"offset": limit}).encode()
                            )
                            .decode()
                            .rstrip("=")
                        )
                    except Exception:
                        next_cursor = ""
                payload = {
                    "count": len(events),
                    "limit": limit,
                    "offset": 0,
                    "sort": sort_key,
                    "tab": tab,
                    "tag_slug": tag_slug_filter or None,
                    "tag_id": tag_id_filter,
                    "lang": None,
                    "events": events,
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                }
                _cache_put(cache_key, payload)
                return _apply_ssr_translations(payload, lang_norm)
        except Exception:
            pass
        return None
    except Exception as exc:
        logger.debug("get_ssr_payload failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def _parse_clob_token_ids(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            return []
    return []


def _parse_outcome_prices(raw) -> list[float]:
    """Parse Gamma `outcomePrices` JSON-string list into floats with full precision."""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        try:
            items = json.loads(raw)
        except Exception:
            return []
    else:
        return []
    out: list[float] = []
    for v in items if isinstance(items, list) else []:
        try:
            out.append(float(v))
        except Exception:
            out.append(0.0)
    return out


# T841 — 占位/模板子市场过滤。Polymarket Gamma 在大型多结果事件
# （如 World Cup、Eurovision、Election Winner 等）会预留若干模板/占位
# 子市场，命名格式如 "Will Team AM win the 2026 FIFA World Cup?"、
# "Will Any Other Team win ..."、"Will Team AI win ..."等。这些市场
# 永远不会被实际启用，价格固定 99.9%/0.1% 且 liquidity/volume 全为 0，
# 在前端显示为"无意义噪声子事件"。我们在 _normalise_event 阶段一次性
# 过滤掉，保证 /api/pm/events 与 /api/pm/event/{slug} 返回结果干净。
_PLACEHOLDER_TITLE_RE = re.compile(
    r"^\s*(?:will\s+)?(?:team\s+[a-z]{1,3}|any\s+other\s+team)\b",
    re.IGNORECASE,
)


def _is_placeholder_market(m: dict) -> bool:
    """Return True if the market is a Gamma placeholder/template (filtered out)."""
    if not isinstance(m, dict):
        return False
    q = str(m.get("question") or "").strip()
    if q and _PLACEHOLDER_TITLE_RE.match(q):
        return True
    # Activity heuristic only when liquidity/volume fields are present in
    # the dict (non-slim payloads). slim payloads don't carry those fields,
    # so we never over-filter on slim by relying purely on the title regex.
    if "liquidity" not in m and "volume" not in m and "volume_24hr" not in m:
        return False
    try:
        liq = float(m.get("liquidity") or 0)
        vol_all = float(m.get("volume") or 0)
        vol_24 = float(m.get("volume_24hr") or 0)
        yes_prob = float(m.get("yes_prob") or 0)
    except Exception:
        return False
    if liq <= 0.0 and vol_all <= 0.0 and vol_24 <= 0.0:
        if yes_prob >= 0.995 or yes_prob <= 0.005:
            return True
    return False


def _normalise_market(m: dict, slim: bool = False) -> dict:
    """Trim a Gamma /markets item to the fields the UI needs.

    T292 D 项：暴露原始 `outcome_prices` 浮点数组（如 [0.165, 0.835]），
    前端用此精确百分比，避免 best_ask 四舍五入失真。
    slim=True 时只保留发现页在用的最小字段集。
    """
    tokens = _parse_clob_token_ids(m.get("clobTokenIds"))
    outcomes = m.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []
    outcome_prices = _parse_outcome_prices(m.get("outcomePrices"))
    yes_prob = outcome_prices[0] if outcome_prices else _safe_float(m.get("bestAsk"))
    if slim:
        return {
            "market_id": str(m.get("id") or ""),
            "question": str(m.get("question") or ""),
            "slug": str(m.get("slug") or ""),
            "event_slug": str(m.get("eventSlug") or ""),
            "outcomes": outcomes if isinstance(outcomes, list) else [],
            "yes_prob": yes_prob,
            "yes_token_id": tokens[0] if len(tokens) > 0 else "",
            "no_token_id": tokens[1] if len(tokens) > 1 else "",
            "best_bid": _safe_float(m.get("bestBid")),
            "best_ask": _safe_float(m.get("bestAsk")),
            "end_date": str(m.get("endDate") or ""),
            "closed": bool(m.get("closed")),
            "image": str(m.get("image") or ""),
            "icon": str(m.get("icon") or ""),
        }
    return {
        "market_id": str(m.get("id") or ""),
        "condition_id": str(m.get("conditionId") or ""),
        "question": str(m.get("question") or ""),
        "slug": str(m.get("slug") or ""),
        "event_slug": str(m.get("eventSlug") or ""),
        "outcomes": outcomes if isinstance(outcomes, list) else [],
        "outcome_prices": outcome_prices,
        "yes_prob": yes_prob,
        "yes_token_id": tokens[0] if len(tokens) > 0 else "",
        "no_token_id": tokens[1] if len(tokens) > 1 else "",
        "best_bid": _safe_float(m.get("bestBid")),
        "best_ask": _safe_float(m.get("bestAsk")),
        "volume_24hr": _safe_float(m.get("volume24hr")),
        "volume": _safe_float(m.get("volume")),
        "liquidity": _safe_float(m.get("liquidity")),
        "end_date": str(m.get("endDate") or ""),
        "active": bool(m.get("active")),
        "closed": bool(m.get("closed")),
        "image": str(m.get("image") or ""),
        "icon": str(m.get("icon") or ""),
    }


def _normalise_event(e: dict, slim: bool = False) -> dict:
    markets = e.get("markets") or []
    if not isinstance(markets, list):
        markets = []
    norm_markets = [
        _normalise_market(m, slim=slim) for m in markets if isinstance(m, dict)
    ]
    # T841 — 过滤 PM 占位/模板子事件（如 "Will Team AM win the 2026 FIFA World Cup?"
    # 与 "Will Any Other Team win ..."），以及完全无活动度的"未交易过"市场。
    norm_markets = [m for m in norm_markets if not _is_placeholder_market(m)]
    out: dict[str, Any] = {
        "event_id": str(e.get("id") or ""),
        "slug": str(e.get("slug") or ""),
        "title": str(e.get("title") or ""),
        "image": str(e.get("image") or ""),
        "icon": str(e.get("icon") or ""),
        "category": str(e.get("category") or ""),
        "end_date": str(e.get("endDate") or ""),
        "start_date": str(e.get("startDate") or ""),
        "volume_24hr": _safe_float(e.get("volume24hr")),
        "volume": _safe_float(e.get("volume")),
        "liquidity": _safe_float(e.get("liquidity")),
        "active": bool(e.get("active")),
        "closed": bool(e.get("closed")),
        "markets_count": len(norm_markets),
        "markets": norm_markets,
    }
    if not slim:
        out["description"] = str(e.get("description") or "")
        out["tags"] = e.get("tags") or []
    return out


def _fetch_erc20_balance(wallet_addr: str, token_address: str) -> float | None:
    """Read Polygon ERC20 balance via eth_call(balanceOf)."""
    if not wallet_addr:
        return None
    addr = wallet_addr.lower().replace("0x", "")
    if len(addr) != 40:
        return None
    data = "0x70a08231" + ("0" * 24) + addr
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": token_address, "data": data}, "latest"],
        "id": 1,
    }
    for rpc in _polygon_rpcs():
        try:
            resp = requests.post(rpc, json=payload, timeout=8)
            if not resp.ok:
                continue
            body = resp.json() or {}
            if body.get("error"):
                continue
            result = body.get("result", "0x0")
            if not isinstance(result, str) or not result.startswith("0x"):
                continue
            return int(result, 16) / 1_000_000.0
        except Exception:
            continue
    return None


def _fetch_usdc_balances(wallet_addr: str) -> dict[str, float | None]:
    # T911 wave 4 perf: short TTL cache keyed by funder address.
    # /api/pm/deposit-info, /api/pm/balance-check and /api/user/pm/metrics all
    # call this on the request path. Each call hits 3 ERC20 balance reads via
    # Polygon RPC (~300-1000ms total when public RPC is throttled). A 8s TTL
    # collapses 3-4 concurrent UI calls into a single chain hit without
    # noticeably staling the displayed balance.
    key = str(wallet_addr or "").lower()
    if key:
        ent = _USDC_BAL_CACHE.get(key)
        now = time.time()
        if ent and (now - ent[0]) < _USDC_BAL_TTL:
            return ent[1]
    usdc_e = _fetch_erc20_balance(wallet_addr, _USDC_E_TOKEN)
    usdc_native = _fetch_erc20_balance(wallet_addr, _USDC_NATIVE_TOKEN)
    pusd = _fetch_erc20_balance(wallet_addr, _PUSD_TOKEN)
    total = 0.0
    has_any = False
    if pusd is not None:
        total += float(pusd)
        has_any = True
    if usdc_e is not None:
        total += float(usdc_e)
        has_any = True
    if usdc_native is not None:
        total += float(usdc_native)
        has_any = True
    out = {
        "pusd": pusd,
        "usdc_e": usdc_e,
        "usdc_native": usdc_native,
        "usdc_total": total if has_any else None,
    }
    if key:
        _USDC_BAL_CACHE[key] = (time.time(), out)
    return out


def _get_user_funder(user_wallet: str) -> str:
    """Read users.funder_address; fall back to polymarket_wallet."""
    key = str(user_wallet or "").strip().lower()
    if key:
        ent = _USER_FUNDER_CACHE.get(key)
        if ent and (time.time() - ent[0]) < _USER_FUNDER_TTL:
            return ent[1]
    with db_connect() as conn:
        row = conn.execute(
            "SELECT funder_address, polymarket_wallet FROM users WHERE wallet_address=?",
            (user_wallet,),
        ).fetchone()
    if not row:
        if key:
            _USER_FUNDER_CACHE[key] = (time.time(), "")
        return ""
    funder = str((row["funder_address"] if row else "") or "").strip().lower()
    if not funder:
        funder = str((row["polymarket_wallet"] if row else "") or "").strip().lower()
    if key:
        _USER_FUNDER_CACHE[key] = (time.time(), funder)
    return funder


def _get_user_wallet_type(user_wallet: str) -> str:
    """Return users.wallet_type ('safe' | 'deposit_wallet'); empty string if unknown."""
    key = str(user_wallet or "").strip().lower()
    if key:
        ent = _WALLET_TYPE_CACHE.get(key)
        if ent and (time.time() - ent[0]) < _WALLET_TYPE_TTL:
            return ent[1]
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT wallet_type FROM users WHERE wallet_address=?",
                (user_wallet,),
            ).fetchone()
    except Exception:
        return ""
    if not row:
        if key:
            _WALLET_TYPE_CACHE[key] = (time.time(), "")
        return ""
    out = str((row["wallet_type"] if row else "") or "").strip().lower()
    if key:
        _WALLET_TYPE_CACHE[key] = (time.time(), out)
    return out


# T883 [FundFlow]: per-process throttle for auto ensure_approvals (5 min/user).
_AUTO_APPROVAL_LAST: dict[str, float] = {}
_AUTO_APPROVAL_TTL = 300.0

# T911 wave 4 perf: short TTL caches for the request-path RPC reads on
# /api/pm/deposit-info, /api/pm/balance-check and /api/user/pm/metrics.
_USDC_BAL_CACHE: dict[str, tuple[float, dict]] = {}
_USDC_BAL_TTL = 8.0
_WALLET_TYPE_CACHE: dict[str, tuple[float, str]] = {}
_WALLET_TYPE_TTL = 60.0
_USER_FUNDER_CACHE: dict[str, tuple[float, str]] = {}
_USER_FUNDER_TTL = 60.0


def _maybe_auto_ensure_approvals(user_wallet: str, funder: str) -> None:
    """Trigger gasless approvals once balance > 0 detected on a deposit_wallet.

    Idempotent: throttled to one call per user per 5 minutes. No-op for SAFE
    users. Runs in a background thread so the calling endpoint is never blocked.
    Logs with [FundFlow] prefix for ops grep.
    """
    user_wallet = str(user_wallet or "").strip().lower()
    if not user_wallet:
        return
    wt = _get_user_wallet_type(user_wallet)
    if wt != "deposit_wallet":
        return
    now = time.time()
    last = _AUTO_APPROVAL_LAST.get(user_wallet, 0.0)
    if (now - last) < _AUTO_APPROVAL_TTL:
        return
    _AUTO_APPROVAL_LAST[user_wallet] = now

    def _worker():
        try:
            from src.utils.pm_relayer_client import ensure_approvals  # noqa: PLC0415
            res = ensure_approvals(funder, user_wallet)
            status = (res or {}).get("status", "?")
            logger.info(
                "[FundFlow] auto ensure_approvals user=%s funder=%s -> status=%s",
                user_wallet[:10], funder[:10], status,
            )
        except Exception as exc:
            logger.warning(
                "[FundFlow] auto ensure_approvals failed user=%s err=%s",
                user_wallet[:10], exc,
            )

    try:
        import threading as _t  # noqa: PLC0415
        _t.Thread(target=_worker, daemon=True, name="auto_ensure_approvals").start()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 公开数据端点（无需登录，用于列表/详情浏览）
# ---------------------------------------------------------------------------

_prefetch_cache: dict = {}
_prefetch_cache_lock = Lock()
_PREFETCH_TTL = 600


@router.get("/api/pm/events/prefetch")
async def pm_events_prefetch() -> JSONResponse:
    """T732 — 返回最多 400 条活跃事件的轻量索引（slug/title/icon/volume/liquidity），
    供前端搜索框在本地过滤，消除每次搜索击键调用 Gamma API 的延迟。
    数据来自已预热的内存缓存，零额外上游请求，TTL 600s。"""
    now = _time_mod.time()
    with _prefetch_cache_lock:
        hit = _prefetch_cache.get("data")
        if hit and (now - hit["ts"]) < _PREFETCH_TTL:
            return JSONResponse({"items": hit["items"], "ts": int(hit["ts"])})

    items: list[dict] = []
    seen: set[str] = set()
    with _events_cache_lock:
        snap = dict(_events_cache)
    for _, entry in snap.items():
        if not isinstance(entry, tuple):
            continue
        payload = entry[1] if len(entry) > 1 else {}
        for ev in (payload or {}).get("events") or []:
            if not isinstance(ev, dict):
                continue
            slug = str(ev.get("slug") or "").strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            items.append(
                {
                    "slug": slug,
                    "title": str(ev.get("title") or ""),
                    "icon": str(ev.get("icon") or ev.get("image") or ""),
                    "volume_24hr": float(
                        ev.get("volume_24hr") or ev.get("volume24hr") or 0
                    ),
                    "liquidity": float(ev.get("liquidity") or 0),
                }
            )
            if len(items) >= 400:
                break
        if len(items) >= 400:
            break

    items.sort(key=lambda x: -x["volume_24hr"])
    with _prefetch_cache_lock:
        _prefetch_cache["data"] = {"ts": now, "items": items}
    return JSONResponse({"items": items, "ts": int(now)})


@router.get("/api/pm/events")
async def pm_events(
    limit: int = 30,
    offset: int = 0,
    cursor: str = "",
    sort: str = "",
    tag_slug: str = "",
    p: str = "",
    t: str = "",
    search: str = "",
    tab: str = "",
    lang: str = "",
    slim: int = 1,
    fast: int = 0,
):
    """T290: Gamma /events 代理 + TTL 缓存。

    支持参数：
    - p: 分类 slug（pm_categories.slug）→ 解析为 tag_id 传给 Gamma
    - t: 标签 slug（pm_tags.slug）→ 解析为 tag_id
    - tab: all/hot/closing/volume/resolved
    - sort: volume24hr/volume/liquidity/endDate/startDate
    - cursor (T335): 由前次响应的 next_cursor 透传，等价于内部 offset+limit
    """
    max_per = _safe_int(get("discover.max_events_per_page") or 30)
    limit = max(5, min(int(limit or max_per), max(max_per, 100)))
    # T335 keyset 分页：cursor 优先于 offset（cursor 是 base64({"offset":N,"sig":"..."}) 简化版）
    if cursor:
        try:
            import base64

            # 补齐 base64 padding（前端 next_cursor 已剔除 '='）
            cursor_b = cursor.encode()
            cursor_b += b"=" * (-len(cursor_b) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(cursor_b).decode())
            offset = max(0, int(decoded.get("offset", 0)))
        except Exception:
            offset = 0
    else:
        offset = max(0, int(offset or 0))

    # tab 决定默认排序（T292-I：删 closing/resolved，新增 new/liquidity）
    sort_default = str(get("discover.default_sort") or "volume24hr")
    tab_norm = (tab or "").strip().lower()
    sort_key = (sort or "").strip()
    if not sort_key:
        if tab_norm in ("hot", "volume"):
            sort_key = "volume24hr"
        elif tab_norm == "new":
            sort_key = "startDate"
        elif tab_norm == "liquidity":
            sort_key = "liquidity"
        elif tab_norm == "total":
            sort_key = "volume"
        else:
            sort_key = sort_default
    valid_sorts = {"volume24hr", "volume", "liquidity", "endDate", "startDate"}
    if sort_key not in valid_sorts:
        sort_key = "volume24hr"

    fast_mode = bool(int(fast or 0))

    # T578 — search 模式必须放大上游 limit。原逻辑 limit=8 上传 + 客户端过滤
    # 会让低热度事件（如 floyd-mayweather-vs-manny-pacquiao-2）根本进不了候选集，
    # 在携带 search 参数时，提升上游拉取限制到 200，避免灯塔效应。
    upstream_limit = limit
    if search:
        upstream_limit = max(limit, 60 if fast_mode else 200)

    params: dict[str, Any] = {
        "limit": upstream_limit,
        "offset": offset,
        "order": sort_key,
        "ascending": "true" if sort_key in ("endDate", "startDate") else "false",
    }
    # T292-I：默认仅返回 active=true & closed=false 事件
    if bool(get("discover.active_only") is not False):
        params["active"] = "true"
    if bool(get("discover.closed_exclude") is not False):
        params["closed"] = "false"
    # T529 #529 — source-side end_date filter (mirrors _build_warm_payload).
    # Drops events that have already expired but haven't been settled yet so
    # they never reach the user as confusing "Ended" cards.
    # T532 #532 — quantized to a 60s bucket so warmer/SSR/pm_events keys
    # collide and SSR cache reuse actually happens.
    try:
        params["end_date_min"] = _quantized_end_date_min()
    except Exception:
        pass

    # 解析 p / t / tag_slug → 优先用 Gamma tag_slug 直传（最可靠），
    # 其次回退到 pm_tags.tag_id（已知 Gamma 数值标签）
    tag_slug_filter: str = ""
    tag_id_filter: int | None = None
    p_norm = (p or "").strip().lower()
    t_norm = (t or tag_slug or "").strip().lower()
    # 用户自定义子标签优先；否则用赛道分类 slug
    chosen_slug = t_norm or p_norm
    if chosen_slug:
        tag_slug_filter = chosen_slug

        # 同时尝试解析 pm_tags 数值 tag_id（如 soccer→100350），双保险传给 Gamma
        # T565b — 此前直接 `with db_connect()` 在 async handler 里同步 IO，
        # 一旦 PG 池被打满会阻塞整个事件循环，全站请求一起卡死。
        def _resolve_tag_id(slug: str) -> int | None:
            try:
                with db_connect() as conn:
                    row = conn.execute(
                        "SELECT tag_id FROM pm_tags WHERE slug=? AND active=1 LIMIT 1",
                        (slug,),
                    ).fetchone()
                    if row:
                        return int(row["tag_id"])
            except Exception as exc:
                logger.warning("tag resolve failed: %s", exc)
            return None

        tag_id_filter = await asyncio.to_thread(_resolve_tag_id, chosen_slug)

    if tag_slug_filter:
        params["tag_slug"] = tag_slug_filter
    if tag_id_filter is not None:
        params["tag_id"] = tag_id_filter

    # T482: 关键字搜索需要 description 兜底，自动关闭 slim
    if search:
        slim = 0

    cache_key = json.dumps(
        {
            **params,
            "search": search,
            "tab": tab_norm,
            "lang": (lang or "").strip(),
            "slim": int(bool(slim)),
        },
        sort_keys=True,
    )

    slim_mode = bool(slim)
    lang_norm = (lang or "").strip()

    def _build_payload_sync() -> dict:
        """T496: 同步执行上游拉取 + 归一 + i18n + payload 构造。供主路径与 SWR 后台刷新复用。"""
        # Phase-2: 12s → 8s（与 PM_HTTP_DEFAULT_TIMEOUT 一致），降低单 worker
        # 在上游慢响应时被长时间挂起的概率
        resp_local = _pm_session().get(f"{_GAMMA_API}/events", params=params, timeout=8)
        resp_local.raise_for_status()
        raw_local = resp_local.json()
        events_local = [
            _normalise_event(e, slim=slim_mode)
            for e in raw_local
            if isinstance(e, dict)
        ]

        if search:
            q = search.strip().lower()
            translation_hits = _search_translation_slug_hits(
                [str((e or {}).get("slug") or "") for e in events_local],
                q,
            )
            events_local = [
                e
                for e in events_local
                if q in e["title"].lower()
                or q in e["description"].lower()
                or q in e["slug"].lower()
                or str((e or {}).get("slug") or "").lower() in translation_hits
            ]
            # fast 模式供头部联想搜索：优先低延迟，跳过额外 slug/detail 探测。
            # T578 — 如果客户端完全未命中，尝试将查询词当作事件 slug 直接调
            # gamma `/events/slug/<q>`。这能兑现低热度但精确事件（如
            # floyd-mayweather-vs-manny-pacquiao-2）的搜索需求。
            if (not fast_mode) and (not events_local) and q:
                try:
                    import re as _re_mod

                    _slug_guess = _re_mod.sub(r"\s+", "-", q).strip("-")
                    if 3 <= len(_slug_guess) <= 200:
                        _r = _pm_session().get(
                            f"{_GAMMA_API}/events/slug/{_slug_guess}", timeout=6
                        )
                        if _r.ok:
                            _ev = _r.json()
                            if isinstance(_ev, dict) and _ev.get("slug"):
                                events_local = [_normalise_event(_ev, slim=slim_mode)]
                except Exception:
                    pass

            # T578-FU2 — 必须支持关键词模糊搜索（"mayweather" 应命中
            # floyd-mayweather-vs-manny-pacquiao-2）。Polymarket 官方提供
            # `/public-search?q=<kw>` 端点做全库模糊匹配，命中率远高于
            # 客户端 substring 过滤。仍未命中时才放弃。
            if (not fast_mode) and q and len(events_local) < (limit or 8):
                try:
                    _ps = _pm_session().get(
                        f"{_GAMMA_API}/public-search",
                        params={
                            "q": q,
                            "limit_per_type": max(int(limit or 8), 12),
                            "events_status": "active",
                        },
                        timeout=6,
                    )
                    if _ps.ok:
                        _pd = _ps.json() or {}
                        _seen = {e.get("slug") for e in events_local}
                        for _ev in _pd.get("events") or []:
                            if not isinstance(_ev, dict):
                                continue
                            _slug = str(_ev.get("slug") or "")
                            if not _slug or _slug in _seen:
                                continue
                            try:
                                events_local.append(
                                    _normalise_event(_ev, slim=slim_mode)
                                )
                                _seen.add(_slug)
                            except Exception:
                                continue
                except Exception:
                    pass

        if lang_norm and lang_norm != "en" and _i18n_get_cached:
            for ev in events_local:
                try:
                    ev_slug = ev.get("slug", "")
                    tr = _i18n_get_cached(ev_slug, lang_norm)
                    markets_tr = (tr or {}).get("markets") or {}
                    if tr and tr.get("title"):
                        ev["title_src"] = ev.get("title", "")
                        ev["title"] = tr["title"]
                        ev["i18n_lang"] = lang_norm
                    missing_market_slugs: list[str] = []
                    for m in ev.get("markets") or []:
                        if not isinstance(m, dict):
                            continue
                        m_slug = m.get("slug") or ""
                        q_tr = markets_tr.get(m_slug)
                        if q_tr:
                            m["question_src"] = m.get("question", "")
                            m["question"] = q_tr
                        elif m_slug and m.get("question"):
                            missing_market_slugs.append(m_slug)
                except Exception:
                    pass

        has_more_local = len(events_local) >= limit
        next_cursor_local = ""
        if has_more_local:
            try:
                import base64

                next_cursor_local = (
                    base64.urlsafe_b64encode(
                        json.dumps({"offset": offset + limit}).encode()
                    )
                    .decode()
                    .rstrip("=")
                )
            except Exception:
                next_cursor_local = ""

        payload_local = {
            "count": len(events_local),
            "limit": limit,
            "offset": offset,
            "sort": sort_key,
            "tab": tab_norm,
            "tag_slug": tag_slug_filter or None,
            "tag_id": tag_id_filter,
            "lang": lang_norm or None,
            "events": events_local,
            "next_cursor": next_cursor_local,
            "has_more": has_more_local,
        }
        _cache_put(cache_key, payload_local)
        return payload_local

    # T496: 1) 新鲜缓存命中 → 立刻返回 + 公共 CDN 缓存头。
    cached = _cache_get(cache_key)
    if cached is not None:
        return JSONResponse(
            {"status": "ok", "cached": True, **cached},
            headers=_public_cache_headers(max_age=15, swr=60),
        )

    # T496: 2) 过期但仍在 SWR 窗口 → 立刻返回 stale + 后台异步刷新（first-byte 毫秒级）。
    stale, age = _cache_get_any(cache_key)
    if (
        stale is not None
        and age is not None
        and age <= (_cache_ttl_sec() + _swr_threshold_sec())
    ):
        _trigger_background_refresh(cache_key, _build_payload_sync)
        return JSONResponse(
            {"status": "ok", "cached": True, "stale": True, **stale},
            headers=_public_cache_headers(max_age=5, swr=60),
        )

    # T496: 3) 真正的冷未命中 → 同步拉取（线程化避免阻塞事件循环）。
    try:
        payload = await asyncio.to_thread(_build_payload_sync)
    except Exception as exc:
        logger.warning("pm_events Gamma fetch failed: %s", exc)
        return JSONResponse(
            {"status": "error", "error": f"upstream fetch failed: {exc}"},
            status_code=502,
        )

    return JSONResponse(
        {"status": "ok", "cached": False, **payload},
        headers=_public_cache_headers(max_age=15, swr=60),
    )


@router.get("/api/pm/categories")
async def pm_categories():
    """T290: 返回所有活跃 PM 分类（用于侧边栏 + 列表页过滤栏）。

    T510 P0-3: 进程级 5 分钟内存缓存，避免每次重复读 PG。
    DB 读耗时 ~5-15ms，命中缓存 <0.5ms。
    """
    global _PM_CATEGORIES_CACHE
    now = _time_mod.time()
    cached = _PM_CATEGORIES_CACHE
    if cached and (now - float(cached.get("ts") or 0)) < _PM_CATEGORIES_TTL:
        return JSONResponse(
            {
                "status": "ok",
                "count": len(cached["categories"]),
                "categories": cached["categories"],
                "cached": True,
            },
            headers=_public_cache_headers(max_age=60, swr=120),
        )
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT category_id, slug, label FROM pm_categories WHERE active=1 ORDER BY label"
            ).fetchall()
        cats = [
            {
                "category_id": int(r["category_id"]),
                "slug": str(r["slug"]),
                "label": str(r["label"]),
            }
            for r in rows
            if str(r["slug"]).lower()
            != "mentions"  # T292-N: 屏蔽"热议"，PM 该类别不返回可投注事件
        ]
    except Exception as exc:
        logger.warning("pm_categories failed: %s", exc)
        cats = []
    _PM_CATEGORIES_CACHE = {"ts": now, "categories": cats}
    # T496: 公共数据，加 60s CDN 缓存头降低重复请求。
    return JSONResponse(
        {"status": "ok", "count": len(cats), "categories": cats, "cached": False},
        headers=_public_cache_headers(max_age=60, swr=120),
    )


def get_categories_payload() -> dict | None:
    """T537 #537 — synchronous accessor used by the SSR layer to inject the
    full categories list into the discover HTML on the very first paint.

    Mirrors the cache used by ``GET /api/pm/categories`` (5-minute TTL); on a
    cold cache we read directly from PG once, populate the cache, and return.
    The intent is to make the category nav appear in the very first frame so
    that switching between ``?p=politics`` / ``?p=crypto`` / ... feels
    instant — it is a public, virtually static list (changes are extremely
    rare) and benefits the same way the sub-tag nav (T508) did.
    """
    global _PM_CATEGORIES_CACHE
    now = _time_mod.time()
    cached = _PM_CATEGORIES_CACHE
    if (
        cached
        and (now - float(cached.get("ts") or 0)) < _PM_CATEGORIES_TTL
        and cached.get("categories")
    ):
        return {
            "status": "ok",
            "count": len(cached["categories"]),
            "categories": cached["categories"],
        }
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT category_id, slug, label FROM pm_categories WHERE active=1 ORDER BY label"
            ).fetchall()
        cats = [
            {
                "category_id": int(r["category_id"]),
                "slug": str(r["slug"]),
                "label": str(r["label"]),
            }
            for r in rows
            if str(r["slug"]).lower() != "mentions"
        ]
    except Exception as exc:
        logger.debug("get_categories_payload failed: %s", exc)
        return None
    _PM_CATEGORIES_CACHE = {"ts": now, "categories": cats}
    return {"status": "ok", "count": len(cats), "categories": cats}


# ---------------------------------------------------------------------------
# T293 H + I: Polymarket Data API 代理（成交历史 / 持仓人榜单 / 用户持仓）
# ---------------------------------------------------------------------------

_DATA_API = "https://data-api.polymarket.com"

# T510 Wave 15 P2-1 — second-tier LRU for /api/pm/market/price_history
# Key: (token_id, range)  →  {"ts": epoch, "ttl": seconds, "payload": dict}
# TTL = candle fidelity / 4 (a 1d range with 60-min candles → 15-min cache).
_PRICE_HISTORY_CACHE: dict[tuple, dict] = {}
_PRICE_HISTORY_LRU_MAX = 256
_PRICE_HISTORY_LOCK = Lock()


def _ph_cache_get(key: tuple) -> dict | None:
    import time as _t

    with _PRICE_HISTORY_LOCK:
        rec = _PRICE_HISTORY_CACHE.get(key)
        if not rec:
            return None
        if (_t.time() - float(rec.get("ts") or 0)) > float(rec.get("ttl") or 0):
            _PRICE_HISTORY_CACHE.pop(key, None)
            return None
        # mark as recently used: move to end
        _PRICE_HISTORY_CACHE.pop(key, None)
        _PRICE_HISTORY_CACHE[key] = rec
        return rec.get("payload")


def _ph_cache_put(key: tuple, payload: dict, ttl: float) -> None:
    import time as _t

    with _PRICE_HISTORY_LOCK:
        _PRICE_HISTORY_CACHE[key] = {
            "ts": _t.time(),
            "ttl": float(ttl),
            "payload": payload,
        }
        # evict oldest if past LRU max
        while len(_PRICE_HISTORY_CACHE) > _PRICE_HISTORY_LRU_MAX:
            _PRICE_HISTORY_CACHE.pop(next(iter(_PRICE_HISTORY_CACHE)))


@router.get("/api/pm/market/price_history")
async def pm_market_price_history(market: str = "", range: str = "1d"):
    """T416: 代理 CLOB prices-history API — 按时间范围返回价格走势。

    market = CLOB token_id (yes_token_id 或 no_token_id)
    range  = 1h | 6h | 1d | 7d | all
    """
    import time as _time

    market = validate_token_id(market, field="market")

    now = int(_time.time())
    # startTs → (seconds back), fidelity (minutes per candle)
    _range_cfg: dict = {
        "1h": (now - 3_600, 1),
        "6h": (now - 21_600, 5),
        "1d": (now - 86_400, 60),
        "7d": (now - 604_800, 240),
        # For "all": use Polymarket launch date (~2020-09-01) with daily fidelity
        # hourly (fidelity=60) returns ~43k points and triggers CLOB 502; daily is fine
        "all": (1_598_918_400, 1440),
    }
    start_ts, fidelity = _range_cfg.get(range, (now - 86_400, 60))

    # T510 Wave 15 P2-1 — LRU lookup before issuing CLOB request
    cache_key = (market, range)
    cached = _ph_cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    params: dict = {"market": market, "fidelity": fidelity}
    if start_ts is not None:
        params["startTs"] = start_ts

    try:
        # Phase-2: 同步 IO 必须在 async def 中走线程池，避免阻塞事件循环
        resp = await asyncio.to_thread(
            lambda: _pm_session().get(
                f"{_CLOB_PUBLIC}/prices-history",
                params=params,
                timeout=8,
            )
        )
        resp.raise_for_status()
        raw = resp.json() or {}
    except Exception as exc:
        logger.warning("pm_market_price_history fetch failed: %s", exc)
        return JSONResponse({"error": f"upstream: {exc}"}, status_code=502)

    history = raw.get("history", [])
    if not isinstance(history, list):
        history = []

    points = []
    for h in history:
        if not isinstance(h, dict):
            continue
        t = h.get("t") or h.get("timestamp")
        p = h.get("p") or h.get("price")
        if t is not None and p is not None:
            try:
                points.append({"t": int(t), "p": float(p)})
            except (TypeError, ValueError):
                continue

    payload = {
        "status": "ok",
        "market": market,
        "range": range,
        "count": len(points),
        "history": points,
    }
    # TTL = candle fidelity (minutes) * 60 / 4
    _ph_cache_put(cache_key, payload, ttl=max(15.0, fidelity * 60.0 / 4.0))
    return {**payload, "cached": False}


@router.get("/api/pm/market/trades")
async def pm_market_trades(market: str = "", limit: int = 30):
    """T293-H: 代理 Polymarket Data API /trades — 单市场成交历史。

    market 应为 condition_id (0x... 64-hex)。
    """
    market = validate_condition_id(market, field="market")
    limit = max(1, min(int(limit or 30), 200))
    try:
        resp = await asyncio.to_thread(
            lambda: _pm_session().get(
                f"{_DATA_API}/trades",
                params={"market": market, "limit": limit, "takerOnly": "false"},
                timeout=8,
            )
        )
        resp.raise_for_status()
        raw = resp.json() or []
    except Exception as exc:
        logger.warning("pm_market_trades fetch failed: %s", exc)
        return JSONResponse({"error": f"upstream: {exc}"}, status_code=502)

    if not isinstance(raw, list):
        raw = []

    trades = []
    for t in raw[:limit]:
        if not isinstance(t, dict):
            continue
        trades.append(
            {
                "timestamp": int(t.get("timestamp") or 0),
                "side": str(t.get("side") or ""),
                "outcome": str(t.get("outcome") or ""),
                "outcome_index": int(t.get("outcomeIndex") or 0),
                "price": _safe_float(t.get("price")),
                "size": _safe_float(t.get("size")),
                "name": str(t.get("name") or t.get("pseudonym") or "Anon"),
                "pseudonym": str(t.get("pseudonym") or ""),
                "proxy_wallet": str(t.get("proxyWallet") or ""),
                "tx_hash": str(t.get("transactionHash") or ""),
            }
        )

    return {"status": "ok", "market": market, "count": len(trades), "trades": trades}


@router.get("/api/pm/market/holders")
async def pm_market_holders(market: str = "", limit: int = 20):
    """T293-H: 代理 Data API /holders — 单市场持仓人榜单。

    market 必须是 condition_id (0x... hex)。返回每个 outcome 的 top N 持仓人。
    """
    market = (market or "").strip()
    if not market:
        return JSONResponse(
            {"error": "market (condition_id) required"}, status_code=400
        )
    limit = max(1, min(int(limit or 20), 100))
    try:
        resp = await asyncio.to_thread(
            lambda: _pm_session().get(
                f"{_DATA_API}/holders",
                params={"market": market, "limit": limit},
                timeout=8,
            )
        )
        resp.raise_for_status()
        raw = resp.json() or []
    except Exception as exc:
        logger.warning("pm_market_holders fetch failed: %s", exc)
        return JSONResponse({"error": f"upstream: {exc}"}, status_code=502)

    out = []
    if isinstance(raw, list):
        for outcome in raw:
            if not isinstance(outcome, dict):
                continue
            holders = outcome.get("holders") or []
            entries = []
            for h in holders[:limit]:
                if not isinstance(h, dict):
                    continue
                entries.append(
                    {
                        "name": str(h.get("name") or h.get("pseudonym") or "Anon"),
                        "pseudonym": str(h.get("pseudonym") or ""),
                        "proxy_wallet": str(h.get("proxyWallet") or ""),
                        "amount": _safe_float(h.get("amount")),
                        "outcome_index": int(h.get("outcomeIndex") or 0),
                    }
                )
            out.append(
                {
                    "token": str(outcome.get("token") or ""),
                    "outcome_index": (entries[0]["outcome_index"] if entries else 0),
                    "holders": entries,
                }
            )

    return {"status": "ok", "market": market, "outcomes": out}


@router.get("/api/pm/user/holdings")
async def pm_user_holdings(request: Request, token_id: str = ""):
    """T292-K + T293-I: 当前登录用户在指定 token_id 的持仓数量。

    若 token_id 为空，返回该用户全部 PM 持仓。
    """
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "")
    funder = _get_user_funder(user_wallet)
    if not funder:
        return JSONResponse({"error": "funder_address not bound"}, status_code=412)

    try:
        resp = await asyncio.to_thread(
            lambda: _pm_session().get(
                f"{_DATA_API}/positions",
                params={"user": funder.lower(), "sizeThreshold": 0},
                timeout=8,
            )
        )
        resp.raise_for_status()
        positions = resp.json() or []
    except Exception as exc:
        logger.warning("pm_user_holdings fetch failed: %s", exc)
        return JSONResponse({"error": f"upstream: {exc}"}, status_code=502)

    if not isinstance(positions, list):
        positions = []

    target_token = (token_id or "").strip()
    if target_token:
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            if str(pos.get("asset") or "") == target_token:
                return {
                    "status": "ok",
                    "funder": funder,
                    "token_id": target_token,
                    "shares": _safe_float(pos.get("size")),
                    "avg_price": _safe_float(pos.get("avgPrice")),
                    "current_value": _safe_float(pos.get("currentValue")),
                    "outcome": str(pos.get("outcome") or ""),
                    "outcome_index": int(pos.get("outcomeIndex") or 0),
                }
        # 未找到 — 用户没仓位
        return {
            "status": "ok",
            "funder": funder,
            "token_id": target_token,
            "shares": 0.0,
            "avg_price": 0.0,
            "current_value": 0.0,
        }

    return {
        "status": "ok",
        "funder": funder,
        "count": len(positions),
        "positions": [
            {
                "token_id": str(p.get("asset") or ""),
                "condition_id": str(p.get("conditionId") or ""),
                "shares": _safe_float(p.get("size")),
                "avg_price": _safe_float(p.get("avgPrice")),
                "current_value": _safe_float(p.get("currentValue")),
                # T540 #540 — surface the live mark from Polymarket Data API
                # (`curPrice`) plus the canonical realized/unrealized P&L
                # numbers so the My-Positions table can render correct
                # values for resolved/redeemable positions where the local
                # CLOB orderbook no longer has tradable quotes.
                "cur_price": _safe_float(p.get("curPrice")),
                "cash_pnl": _safe_float(p.get("cashPnl")),
                "percent_pnl": _safe_float(p.get("percentPnl")),
                "initial_value": _safe_float(p.get("initialValue")),
                "realized_pnl": _safe_float(p.get("realizedPnl")),
                "redeemable": bool(p.get("redeemable") or False),
                "negative_risk": bool(p.get("negativeRisk") or False),
                "outcome": str(p.get("outcome") or ""),
                "outcome_index": int(p.get("outcomeIndex") or 0),
                "title": str(p.get("title") or ""),
                "slug": str(p.get("slug") or ""),
            }
            for p in positions
            if isinstance(p, dict)
        ],
    }


# ---------------------------------------------------------------------------
# T294 — 顶部导航：实时热闹交易流 + 个人交易通知
# ---------------------------------------------------------------------------


@router.get("/api/pm/marquee")
async def pm_marquee(limit: int = 20, min_size: float = 20.0):
    """T294/T310: 全平台公共大额成交流（marquee 数据源）。

    - 代理 Polymarket Data API /trades，按 notional 过滤，按时间倒序。
    - T310: 进程级缓存兜底，永不返回空 trades（服务端阶段）。
      上游失败 / 过滤后为空 → 直接返回上次缓存。
    """
    global _MARQUEE_CACHE  # noqa: PLW0603
    limit = max(5, min(int(limit or 20), 50))
    try:
        # T546 — 同步 requests.get 必须放到线程池，避免高频 marquee 轮询阻塞
        # 整个事件循环导致 /api/pm/balance-check 等并发端点出现 504。
        resp = await asyncio.to_thread(
            lambda: _pm_session().get(
                f"{_DATA_API}/trades",
                params={"limit": max(80, limit * 5)},
                timeout=8,
            )
        )
        resp.raise_for_status()
        raw = resp.json() or []
    except Exception as exc:
        logger.warning("pm_marquee fetch failed: %s", exc)
        if _MARQUEE_CACHE.get("trades"):
            return {
                "status": "ok",
                "count": len(_MARQUEE_CACHE["trades"]),
                "trades": _MARQUEE_CACHE["trades"][:limit],
                "from_cache": True,
            }
        return {"status": "ok", "count": 0, "trades": [], "from_cache": False}

    if not isinstance(raw, list):
        raw = []

    items = []
    seen_tx: set[str] = set()
    for t in raw:
        if not isinstance(t, dict):
            continue
        size = _safe_float(t.get("size"))
        price = _safe_float(t.get("price"))
        notional = size * price
        if notional < float(min_size):
            continue
        tx = str(t.get("transactionHash") or "")
        if tx and tx in seen_tx:
            continue
        if tx:
            seen_tx.add(tx)
        items.append(
            {
                "timestamp": int(t.get("timestamp") or 0),
                "side": str(t.get("side") or ""),
                "outcome": str(t.get("outcome") or ""),
                "price": price,
                "size": size,
                "notional": round(notional, 2),
                "name": str(t.get("name") or t.get("pseudonym") or "Anon"),
                "title": str(t.get("title") or ""),
                "slug": str(t.get("slug") or t.get("eventSlug") or ""),
                "event_slug": str(t.get("eventSlug") or t.get("slug") or ""),
                "icon": str(t.get("icon") or ""),
                "tx_hash": tx,
            }
        )
        if len(items) >= limit:
            break

    items.sort(key=lambda x: x["timestamp"], reverse=True)

    # T310: 服务端兜底 — 过滤后为空时回退到上次缓存
    if not items and _MARQUEE_CACHE.get("trades"):
        return {
            "status": "ok",
            "count": len(_MARQUEE_CACHE["trades"]),
            "trades": _MARQUEE_CACHE["trades"][:limit],
            "from_cache": True,
        }

    if items:
        _MARQUEE_CACHE["trades"] = items[:50]
        _MARQUEE_CACHE["ts"] = int(time.time())

    return {"status": "ok", "count": len(items), "trades": items}


# T310: 进程级 marquee 缓存（永不返回空）
_MARQUEE_CACHE: dict = {"trades": [], "ts": 0}


@router.get("/api/pm/notifications")
async def pm_notifications(request: Request, limit: int = 20):
    """T294: 当前登录用户最近交易通知（成交/部分成交/取消等）。

    数据源：Polymarket Data API /trades?user=<funder> — 仅本人成交记录。
    """
    identity = get_request_identity(request)
    if not identity:
        return {"status": "ok", "count": 0, "notifications": []}

    user_wallet = str(identity.get("wallet_address") or "")
    funder = _get_user_funder(user_wallet)
    if not funder:
        return {"status": "ok", "count": 0, "notifications": []}

    limit = max(5, min(int(limit or 20), 50))
    try:
        resp = await asyncio.to_thread(
            lambda: _pm_session().get(
                f"{_DATA_API}/trades",
                params={"user": funder.lower(), "limit": limit},
                timeout=8,
            )
        )
        resp.raise_for_status()
        raw = resp.json() or []
    except Exception as exc:
        logger.warning("pm_notifications fetch failed: %s", exc)
        return JSONResponse(
            {"error": f"upstream: {exc}", "notifications": []}, status_code=502
        )

    if not isinstance(raw, list):
        raw = []

    notifications = []
    for t in raw[:limit]:
        if not isinstance(t, dict):
            continue
        size = _safe_float(t.get("size"))
        price = _safe_float(t.get("price"))
        notifications.append(
            {
                "timestamp": int(t.get("timestamp") or 0),
                "side": str(t.get("side") or ""),
                "outcome": str(t.get("outcome") or ""),
                "price": price,
                "size": size,
                "notional": round(size * price, 2),
                "title": str(t.get("title") or ""),
                "event_slug": str(t.get("eventSlug") or t.get("slug") or ""),
                "tx_hash": str(t.get("transactionHash") or ""),
                "icon": str(t.get("icon") or ""),
            }
        )

    return {"status": "ok", "count": len(notifications), "notifications": notifications}


def get_tags_payload(p: str = "") -> dict | None:
    """T508 Wave 7C: synchronous tags lookup for SSR injection.

    Reads only from the warm cache; never blocks on upstream Gamma calls
    longer than 1.2s. Returns None when the slug isn't cached yet AND the
    short-timeout upstream call also fails, so the caller can fall back to
    lazy client-side fetch.
    """
    p_norm = (p or "").strip().lower()
    if not p_norm:
        return None
    cached = _cache_get(f"tags:{p_norm}")
    if cached is not None:
        return {"status": "ok", "cached": True, **cached}
    # SWR: serve recently expired entry immediately
    stale, age = _cache_get_any(f"tags:{p_norm}")
    if (
        stale is not None
        and age is not None
        and age <= (_cache_ttl_sec() + _swr_threshold_sec())
    ):
        return {"status": "ok", "cached": True, **stale}
    # Cold path — short blocking upstream call so the very first visit to a
    # category SSR also gets tags inline; failures simply skip injection.
    try:
        resp = _pm_session().get(
            f"{_GAMMA_API}/events",
            params={
                "tag_slug": p_norm,
                "limit": 80,
                "closed": "false",
                "active": "true",
            },
            timeout=1.2,
        )
        if resp.ok:
            evs = resp.json()
            if isinstance(evs, list):
                from collections import Counter

                counter: Counter = Counter()
                label_map: dict = {}
                id_map: dict = {}
                _CAT_SLUGS_LOWER = {
                    "sports",
                    "crypto",
                    "politics",
                    "elections",
                    "geopolitics",
                    "world",
                    "weather",
                    "tech",
                    "economy",
                    "earnings",
                    "finance",
                    "mentions",
                    "pop-culture",
                    "esports",
                }
                _HIDDEN = {"hide-from-new", "hide-from-top", "all", "trending", "new"}
                for ev in evs:
                    if not isinstance(ev, dict):
                        continue
                    for tag in ev.get("tags") or []:
                        if not isinstance(tag, dict):
                            continue
                        slug = str(tag.get("slug") or "").lower()
                        label = str(tag.get("label") or "").strip()
                        if not slug or not label:
                            continue
                        if slug in _CAT_SLUGS_LOWER or slug in _HIDDEN:
                            continue
                        if tag.get("forceHide") is True:
                            continue
                        counter[slug] += 1
                        if slug not in label_map:
                            label_map[slug] = label
                            id_map[slug] = int(tag.get("id") or 0)
                tags = [
                    {
                        "tag_id": id_map.get(s, 0),
                        "slug": s,
                        "label": label_map.get(s, s),
                        "category_slug": p_norm,
                    }
                    for s, _cnt in counter.most_common(20)
                ]
                # T579 — sports 子分类必须强制曝光的小众体育（与 pm_tags 保持一致）
                if p_norm == "sports":
                    _PINNED_SPORTS = (
                        "boxing",
                        "cricket",
                        "hockey",
                        "mma",
                        "ufc",
                        "golf",
                        "formula-1",
                        "f1",
                        "rugby",
                        "darts",
                        "olympics",
                        "horse-racing",
                        "nascar",
                        "wnba",
                        "esports",
                        "league-of-legends",
                        "counter-strike-2",
                    )
                    _existing = {t["slug"] for t in tags}
                    for _ps in _PINNED_SPORTS:
                        if _ps in _existing:
                            continue
                        _lbl = label_map.get(_ps) or _ps.replace("-", " ").title()
                        tags.append(
                            {
                                "tag_id": id_map.get(_ps, 0),
                                "slug": _ps,
                                "label": _lbl,
                                "category_slug": p_norm,
                            }
                        )
                        _existing.add(_ps)
                payload = {"count": len(tags), "tags": tags, "category_slug": p_norm}
                _cache_put(f"tags:{p_norm}", payload)
                return {"status": "ok", "cached": False, **payload}
    except Exception:
        pass
    return None


@router.get("/api/pm/tags")
async def pm_tags(p: str = ""):
    """T290 + T292 B 项：返回 PM 子标签。

    优先策略：
    1. 若提供 `p`（分类 slug，如 sports/crypto），实时调 Gamma `/tags?parent_slug=p`
       并缓存（避免数据库种子滞后），返回该分类下所有子标签。
    2. 不提供 `p` 时，回退到 pm_tags 表（不再用于"全部"分类，避免噪声）。
    """
    p_norm = (p or "").strip().lower()
    if not p_norm:
        return {"status": "ok", "count": 0, "tags": []}

    cache_key = f"tags:{p_norm}"

    # T579-FU2 — 优先读 pm_tags_sync 守护落盘的 24h 内快照（含全部 ~80 标签且含 pinned 体育），
    # 毫秒级返回且节省 Gamma quota；放在 mem cache 之前，避免之前在线聚合的稀疏结果被锁死。
    try:
        from scripts.dashboard_routes import pm_tags_sync as _pts

        _disk = _pts.read_category_tags(p_norm)
        if _disk and isinstance(_disk, list) and len(_disk) > 0:
            _payload = {
                "count": len(_disk),
                "tags": _disk,
                "category_slug": p_norm,
                "source": "snapshot",
            }
            _cache_put(cache_key, _payload)
            return {"status": "ok", "cached": False, **_payload}
    except Exception as _exc:
        logger.debug("pm_tags_sync read failed: %s", _exc)

    cached = _cache_get(cache_key)
    if cached is not None:
        return {"status": "ok", "cached": True, **cached}

    tags: list[dict] = []

    # 步骤 1: 反向聚合 — 拉取该分类下的活跃 events，从 events.tags 字段汇总子标签
    # （Gamma /tags 接口不暴露 parent_tag_id 公开层级，只能通过此方式获得）
    # T579 — 按 volume24hr 高低 + startDate 升序两轮拉取，避免仅顶 100 热门事件占据全部样本
    # 导致小众子标签（如 boxing / cricket / ufc / hockey）从未被计入。
    try:
        evs_collected: list = []
        seen_event_ids: set = set()
        for _params in (
            {
                "tag_slug": p_norm,
                "limit": 500,
                "closed": "false",
                "active": "true",
                "order": "volume24hr",
                "ascending": "false",
            },
            {
                "tag_slug": p_norm,
                "limit": 500,
                "closed": "false",
                "active": "true",
                "order": "startDate",
                "ascending": "true",
            },
        ):
            try:
                _r = _pm_session().get(
                    f"{_GAMMA_API}/events", params=_params, timeout=12
                )
                if not _r.ok:
                    continue
                _d = _r.json()
                if not isinstance(_d, list):
                    continue
                for _ev in _d:
                    if not isinstance(_ev, dict):
                        continue
                    _eid = str(_ev.get("id") or _ev.get("slug") or "")
                    if _eid and _eid in seen_event_ids:
                        continue
                    if _eid:
                        seen_event_ids.add(_eid)
                    evs_collected.append(_ev)
            except Exception:
                continue
        evs = evs_collected
        if isinstance(evs, list) and evs:
            if True:
                from collections import Counter

                counter: Counter = Counter()
                label_map: dict = {}
                id_map: dict = {}
                # 已知系统类别集合（小写），过滤掉作为子标签出现的父类
                _CAT_SLUGS_LOWER = {
                    "sports",
                    "crypto",
                    "politics",
                    "elections",
                    "geopolitics",
                    "world",
                    "weather",
                    "tech",
                    "economy",
                    "earnings",
                    "finance",
                    "mentions",
                    "pop-culture",
                    "esports",
                }
                # 隐藏标签（Gamma 内部用途）
                _HIDDEN = {"hide-from-new", "hide-from-top", "all", "trending", "new"}
                for ev in evs:
                    if not isinstance(ev, dict):
                        continue
                    for t in ev.get("tags") or []:
                        if not isinstance(t, dict):
                            continue
                        slug = str(t.get("slug") or "").lower()
                        label = str(t.get("label") or "").strip()
                        if not slug or not label:
                            continue
                        if slug in _CAT_SLUGS_LOWER or slug in _HIDDEN:
                            continue
                        # 屏蔽 Gamma 内部 forceHide 标签
                        if t.get("forceHide") is True:
                            continue
                        counter[slug] += 1
                        if slug not in label_map:
                            label_map[slug] = label
                            id_map[slug] = int(t.get("id") or 0)
                # T579 — 按出现次数倒序，取前 40 个子标签（从 20 提升，以覆盖 boxing /
                # cricket / ufc / hockey 等小众子标签）。前端可用 More 折叠多余部分。
                for slug, _cnt in counter.most_common(40):
                    tags.append(
                        {
                            "tag_id": id_map.get(slug, 0),
                            "slug": slug,
                            "label": label_map.get(slug, slug),
                            "category_slug": p_norm,
                        }
                    )
                # T579 — sports 子分类必须强制曝光的小众体育（即使样本里只有
                # 几条事件、被 most_common 过滤）。否则 boxing / golf / formula-1 /
                # darts 等永远进不了用户视野。
                if p_norm == "sports":
                    _PINNED_SPORTS = (
                        "boxing",
                        "cricket",
                        "hockey",
                        "mma",
                        "ufc",
                        "golf",
                        "formula-1",
                        "f1",
                        "rugby",
                        "darts",
                        "olympics",
                        "horse-racing",
                        "nascar",
                        "wnba",
                        "esports",
                        "league-of-legends",
                        "counter-strike-2",
                    )
                    _existing_slugs = {t.get("slug") for t in tags}
                    for _ps in _PINNED_SPORTS:
                        if _ps in _existing_slugs:
                            continue
                        # 如果在 counter 中出现过，使用它的 label / id；否则
                        # 用 slug 标题化作为 label。强制 tag_id=0 不影响 SSR
                        # 路由（按 slug 跳转）。
                        _lbl = label_map.get(_ps) or _ps.replace("-", " ").title()
                        tags.append(
                            {
                                "tag_id": id_map.get(_ps, 0),
                                "slug": _ps,
                                "label": _lbl,
                                "category_slug": p_norm,
                            }
                        )
                        _existing_slugs.add(_ps)
    except Exception as exc:
        logger.warning("pm_tags Gamma fetch failed for %s: %s", p_norm, exc)

    # 步骤 2: 数据库回退（仅在 Gamma 聚合无结果时使用）
    if not tags:
        try:
            with db_connect() as conn:
                rows = conn.execute(
                    "SELECT tag_id, slug, label, category_slug FROM pm_tags WHERE active=1 AND category_slug=? ORDER BY label",
                    (p_norm,),
                ).fetchall()
            for r in rows:
                slug = str(r["slug"] or "").lower()
                tags.append(
                    {
                        "tag_id": int(r["tag_id"]),
                        "slug": slug,
                        "label": str(r["label"]),
                        "category_slug": str(r["category_slug"] or p_norm),
                    }
                )
        except Exception as exc:
            logger.warning("pm_tags DB fallback failed: %s", exc)

    # 顺序：保持 Gamma 频次倒序（已 most_common 排好），不再字典排序
    payload = {"count": len(tags), "tags": tags, "category_slug": p_norm}
    _cache_put(cache_key, payload)
    return {"status": "ok", "cached": False, **payload}


# T579-FU2 — tag → events 直接读快照接口
@router.get("/api/pm/tag/{category}/{tag}/events")
async def pm_tag_events_snapshot(category: str, tag: str):
    try:
        from scripts.dashboard_routes import pm_tags_sync as _pts

        snap = _pts.read_tag_events(category, tag)
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)
    if not snap:
        return JSONResponse(
            {
                "status": "miss",
                "category": (category or "").lower(),
                "tag": (tag or "").lower(),
                "events": [],
            },
            status_code=404,
        )
    events = snap.get("events") or []
    return {
        "status": "ok",
        "category": snap.get("category"),
        "tag": snap.get("tag"),
        "label": snap.get("label"),
        "ts": snap.get("ts"),
        "count": len(events),
        "events": events,
    }


# T579-FU2 — 同步守护诊断接口
@router.get("/api/pm/tags/_index")
async def pm_tags_sync_index():
    try:
        from scripts.dashboard_routes import pm_tags_sync as _pts

        idx = _pts.read_index()
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)
    if not idx:
        return {"status": "warming", "categories": {}}
    return {"status": "ok", **idx}


@router.get("/api/pm/event/{slug}")
async def pm_event_detail(slug: str, lang: str = ""):
    """T289 Wave 6: 单 event 详情。

    Wave 9: lang 参数支持注入翻译 (en/zh-CN); en 时若无缓存则按需触发翻译
    (i18n.enable_translation=true 才生效)。
    """
    slug = (slug or "").strip().lower()
    if not slug:
        return JSONResponse({"error": "slug required"}, status_code=400)

    cache_key = f"event:{slug}:{(lang or '').strip() or 'en'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {"status": "ok", "cached": True, "event": cached}

    try:
        resp = await asyncio.to_thread(
            _pm_session().get, f"{_GAMMA_API}/events/slug/{slug}", timeout=12
        )
        if resp.status_code == 404:
            return JSONResponse({"error": "event not found"}, status_code=404)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        logger.warning("pm_event_detail(%s) fetch failed: %s", slug, exc)
        return JSONResponse(
            {"status": "error", "error": f"upstream fetch failed: {exc}"},
            status_code=502,
        )

    if not isinstance(raw, dict):
        return JSONResponse({"error": "unexpected payload"}, status_code=502)

    event = _normalise_event(raw)

    lang_norm = (lang or "").strip()
    if lang_norm and lang_norm != "en" and _i18n_get_cached:
        try:
            # 非阻塞翻译：缓存命中即注入；未命中触发后台异步翻译并标记 i18n_pending=true
            # 前端检测 i18n_pending=true 后轮询 /api/pm/event/.../i18n_status 或定时 refetch
            tr = _i18n_get_cached(event.get("slug", ""), lang_norm)
            markets_tr = (tr or {}).get("markets") or {}
            if tr and tr.get("title"):
                event["title_src"] = event.get("title", "")
                event["title"] = tr["title"]
                event["i18n_lang"] = lang_norm
            # 子市场按 slug 替换
            missing_market_slugs: list[str] = []
            for m in event.get("markets") or []:
                if not isinstance(m, dict):
                    continue
                m_slug = m.get("slug") or ""
                q_tr = markets_tr.get(m_slug)
                if q_tr:
                    m["question_src"] = m.get("question", "")
                    m["question"] = q_tr
                elif m_slug and m.get("question"):
                    missing_market_slugs.append(m_slug)
        except Exception as exc:
            logger.debug(
                "event_detail i18n failed slug=%s lang=%s: %s", slug, lang_norm, exc
            )

    _cache_put(cache_key, event)
    return {"status": "ok", "cached": False, "event": event}


@router.get("/api/pm/orderbook")
async def pm_orderbook(token_id: str = ""):
    """T289 Wave 6: CLOB orderbook 代理（公开端点）。"""
    token_id = validate_token_id(token_id)
    try:
        resp = _pm_session().get(
            f"{_CLOB_PUBLIC}/book", params={"token_id": token_id}, timeout=10
        )
        if resp.status_code == 404:
            return JSONResponse({"error": "book not found"}, status_code=404)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return JSONResponse(
            {"status": "error", "error": f"orderbook fetch failed: {exc}"},
            status_code=502,
        )

    bids = data.get("bids") or []
    asks = data.get("asks") or []

    def _top(levels, k=5):
        out = []
        for lv in levels[:k]:
            try:
                out.append(
                    {
                        "price": float(lv.get("price")),
                        "size": float(lv.get("size")),
                    }
                )
            except Exception:
                continue
        return out

    return {
        "status": "ok",
        "token_id": token_id,
        "min_order_size": data.get("min_order_size"),
        "tick_size": data.get("tick_size"),
        "best_bid": _top(bids, 1)[0] if bids else None,
        "best_ask": _top(asks, 1)[0] if asks else None,
        "bids": _top(bids, 10),
        "asks": _top(asks, 10),
        "timestamp": data.get("timestamp"),
    }


# ---------------------------------------------------------------------------
# T316: 实时价格批量端点（无需登录，用于前端实时刷新）
# ---------------------------------------------------------------------------

# 进程内简易缓存，1.5s TTL，避免高并发下 CLOB 被打爆
_LIVE_PRICE_CACHE: dict = {"ts": 0.0, "key": "", "data": {}}
_LIVE_PRICE_TTL = 1.5  # 秒


@router.get("/api/pm/prices/live")
async def pm_prices_live(token_ids: str = ""):
    """T316: 批量获取多个 token 的实时 BUY(best_ask)/SELL(best_bid) 价格。

    输入：?token_ids=a,b,c （逗号分隔，最多 50 个）
    输出：{ "status":"ok", "ts": 1234567890.5, "stale": false,
              "prices": { token_id: {"best_bid": 0.53, "best_ask": 0.54, "mid": 0.535} } }

    语义说明（重要）：
      - Polymarket CLOB /prices 端点的 BUY = 有人愿买入的最高价 = best_bid
      - SELL = 有人愿卖出的最低价 = best_ask
      - 用户买入时付出的价格 = best_ask
      - 用户卖出时收到的价格 = best_bid
    """
    import time as _time

    ids = validate_token_id_list(token_ids)

    cache_key = ",".join(sorted(ids))
    now = _time.time()
    if (
        _LIVE_PRICE_CACHE.get("key") == cache_key
        and (now - float(_LIVE_PRICE_CACHE.get("ts") or 0)) < _LIVE_PRICE_TTL
    ):
        return {
            "status": "ok",
            "ts": _LIVE_PRICE_CACHE["ts"],
            "stale": False,
            "cached": True,
            "prices": _LIVE_PRICE_CACHE.get("data") or {},
        }

    payload = []
    for t in ids:
        payload.append({"token_id": t, "side": "BUY"})
        payload.append({"token_id": t, "side": "SELL"})
    # T510 Wave 14 P1-4 — try in-process WS bridge first, fall back to CLOB REST
    # for any tokens not currently tracked. WS records carry best_bid/best_ask
    # populated from price_change/best_bid_ask events.
    out: dict = {}
    miss_ids: list[str] = []
    try:
        from src.services import (
            pm_market_ws as _mws,
        )  # local import to keep cold-start light

        ws_max_age = 10.0
        for tid in ids:
            rec = _mws.get_live_price(tid)
            if rec and (now - float(rec.get("ts") or 0)) <= ws_max_age:
                bb = rec.get("bid")
                ba = rec.get("ask")
                bb = float(bb) if bb not in (None, "") else None
                ba = float(ba) if ba not in (None, "") else None
                # T523 P0 sanity guard against the "YES=100% / NO=100%"
                # corruption.  A well-formed binary order book can never
                # have best_bid >= best_ask, nor both pinned to the same
                # extreme.  If the WS record is malformed we discard it
                # and let the CLOB REST fallback re-quote the token.
                _suspect = False
                if bb is not None and ba is not None:
                    if bb > ba:
                        _suspect = True
                    elif bb >= 1.0 and ba >= 1.0:
                        _suspect = True
                    elif bb <= 0.0 and ba <= 0.0:
                        _suspect = True
                if _suspect:
                    miss_ids.append(tid)
                    continue
                mid = None
                if bb is not None and ba is not None:
                    mid = round((bb + ba) / 2.0, 4)
                elif bb is not None:
                    mid = bb
                elif ba is not None:
                    mid = ba
                if bb is not None or ba is not None:
                    out[tid] = {"best_bid": bb, "best_ask": ba, "mid": mid, "src": "ws"}
                else:
                    miss_ids.append(tid)
            else:
                miss_ids.append(tid)
    except Exception:
        miss_ids = list(ids)

    if not miss_ids:
        # Full WS hit — skip CLOB POST entirely.
        _LIVE_PRICE_CACHE["ts"] = now
        _LIVE_PRICE_CACHE["key"] = cache_key
        _LIVE_PRICE_CACHE["data"] = out
        return {
            "status": "ok",
            "ts": now,
            "stale": False,
            "cached": False,
            "ws_hits": len(out),
            "rest_hits": 0,
            "prices": out,
        }

    # Re-issue REST only for misses to keep CLOB load proportional to misses.
    # T523 P3 #523 — register the miss set as WS interest so the next
    # subscription refresh promotes them off REST.
    try:
        from src.services import (
            pm_market_ws as _mws,
        )  # local import (already imported above)

        _mws.register_interest(miss_ids)
    except Exception:
        pass
    payload = []
    for t in miss_ids:
        payload.append({"token_id": t, "side": "BUY"})
        payload.append({"token_id": t, "side": "SELL"})
    try:
        resp = _pm_session().post(f"{_CLOB_PUBLIC}/prices", json=payload, timeout=4)
        resp.raise_for_status()
        raw_map = resp.json() or {}
    except Exception as exc:
        # 优雅降级：返回上次缓存（标记 stale），避免前端崩溃
        if _LIVE_PRICE_CACHE.get("data"):
            return {
                "status": "ok",
                "ts": _LIVE_PRICE_CACHE.get("ts") or 0,
                "stale": True,
                "error": f"upstream failed: {exc}",
                "prices": _LIVE_PRICE_CACHE.get("data") or {},
            }
        return JSONResponse(
            {"status": "error", "error": f"prices fetch failed: {exc}"},
            status_code=502,
        )

    out: dict = out  # keep WS hits already filled above
    for tid in miss_ids:
        side_map = raw_map.get(tid) or {}
        try:
            best_bid = (
                float(side_map.get("BUY"))
                if side_map.get("BUY") not in (None, "")
                else None
            )
        except Exception:
            best_bid = None
        try:
            best_ask = (
                float(side_map.get("SELL"))
                if side_map.get("SELL") not in (None, "")
                else None
            )
        except Exception:
            best_ask = None
        mid = None
        if best_bid is not None and best_ask is not None:
            mid = round((best_bid + best_ask) / 2.0, 4)
        elif best_bid is not None:
            mid = best_bid
        elif best_ask is not None:
            mid = best_ask
        out[tid] = {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "src": "rest",
        }

    _LIVE_PRICE_CACHE["ts"] = now
    _LIVE_PRICE_CACHE["key"] = cache_key
    _LIVE_PRICE_CACHE["data"] = out
    ws_hits = sum(
        1 for v in out.values() if isinstance(v, dict) and v.get("src") == "ws"
    )
    return {
        "status": "ok",
        "ts": now,
        "stale": False,
        "cached": False,
        "ws_hits": ws_hits,
        "rest_hits": len(out) - ws_hits,
        "prices": out,
    }


# ---------------------------------------------------------------------------
# i18n 翻译缓存维护（仅管理员）
# ---------------------------------------------------------------------------


_I18N_DEFAULT_LANGS = [
    {"code": "zh-CN", "label": "简体中文"},
    {"code": "zh-TW", "label": "繁體中文"},
    {"code": "ja", "label": "日本語"},
    {"code": "ko", "label": "한국어"},
    {"code": "es", "label": "Español"},
    {"code": "fr", "label": "Français"},
    {"code": "de", "label": "Deutsch"},
    {"code": "pt", "label": "Português"},
    {"code": "ru", "label": "Русский"},
    {"code": "ar", "label": "العربية"},
    {"code": "vi", "label": "Tiếng Việt"},
    {"code": "tr", "label": "Türkçe"},
    {"code": "en", "label": "English (源语言)"},
]

_I18N_RECOMMENDED_MODELS = [
    "deepseek-v4-flash",
]

# T866: 如果任一轮询 key 已配置则认为 LLM 可用
from src.utils.llm_client import (  # noqa: E402
    llm_api_key_set as _llm_api_key_set,
    llm_key_pool_size as _llm_key_pool_size,
)

# T868: 过期/即将过期/无意义的"日内"事件 slug 不翻译。
# 1) endDate 距今不足 i18n.preheat_min_hours_to_end 小时 → throwaway
# 2) slug 命中"含日期 + 含小时段"模式（如 bitcoin-up-or-down-may-5-2026-8pm-et） → throwaway
_THROWAWAY_SLUG_HOURLY = re.compile(
    r"-(?:1[0-2]|[1-9])(?:am|pm)(?:-[a-z]{2,3})?(?:$|-)",
    re.IGNORECASE,
)
_THROWAWAY_SLUG_MONTH = re.compile(
    r"-(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)-\d{1,2}-\d{4}(?:$|-)",
    re.IGNORECASE,
)


def _is_throwaway_event(ev: dict, min_hours_to_end: float) -> tuple[bool, str]:
    """T868：判断事件是否为日内/即将过期的"无意义"事件，无需消耗翻译预算。

    返回 (是否丢弃, 丢弃原因)。
    """
    slug = (ev.get("slug") or "").strip().lower()
    if not slug:
        return True, "empty-slug"
    # closed 直接丢
    if ev.get("closed") is True:
        return True, "closed"
    # endDate 距今不足阈值 → 丢
    end_raw = (ev.get("endDate") or ev.get("endTime") or "").strip()
    if end_raw:
        try:
            ed = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            hours_left = (ed - now).total_seconds() / 3600.0
            if hours_left <= 0:
                return True, "ended"
            if hours_left < float(min_hours_to_end):
                return True, f"ends-in-{hours_left:.1f}h"
        except Exception:
            pass
    # slug 模式：含小时段（8pm-et / 11am）或显式日期（may-5-2026）→ 日内/即将过期
    if _THROWAWAY_SLUG_HOURLY.search(slug):
        # 同时配合月份/日期才丢，纯小时（如 will-x-do-y-by-5pm）也可能是当天结束 → 仍丢
        return True, "slug-hourly"
    if _THROWAWAY_SLUG_MONTH.search(slug):
        # 含明确日期（month-day-year）的事件多为当天/单日内结算，也丢
        return True, "slug-dated"
    return False, ""


@router.get("/api/pm/i18n/status")
async def pm_i18n_status(request: Request):
    try:
        from src.utils.translator import cache_stats
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)
    import os as _os

    enabled = bool(get("i18n.enable_translation") or False)
    raw_models = get("i18n.translator_models")
    models = []
    if isinstance(raw_models, list):
        models = [str(m).strip() for m in raw_models if str(m).strip()]
    cfg_langs = get("i18n.supported_langs")
    if isinstance(cfg_langs, list) and cfg_langs:
        supported = list(cfg_langs)
    else:
        supported = [d["code"] for d in _I18N_DEFAULT_LANGS]
    return {
        "status": "ok",
        "enabled": enabled,
        "models": list(models),
        "recommended_models": list(_I18N_RECOMMENDED_MODELS),
        "supported_langs": supported,
        "available_langs": _I18N_DEFAULT_LANGS,
        "preheat_top_n": int(get("i18n.preheat_top_n") or 60),
        "preheat_min_volume_24h": float(get("i18n.preheat_min_volume_24h") or 5000),
        "llm_key_set": _llm_api_key_set(),
        "deepseek_key_set": _llm_api_key_set(),
        "deepseek_key_pool_size": _llm_key_pool_size(),
        **cache_stats(),
    }


@router.post("/api/pm/i18n/config")
async def pm_i18n_config(request: Request):
    """T330: 翻译配置可视化管理（admin only via user_session role）。"""
    _ident = get_request_identity(request)
    if not (_ident and _ident.get("is_admin")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid body"}, status_code=400)

    from src.utils.config_loader import load_config, backup_config, _CONFIG_PATH  # type: ignore

    cfg = load_config(force=True)
    i18n_cfg = cfg.setdefault("i18n", {})
    changed = []
    ignored = []
    if "enable_translation" in body:
        i18n_cfg["enable_translation"] = bool(body["enable_translation"])
        changed.append("enable_translation")
    if "translator_models" in body:
        models_raw = body.get("translator_models")
        models = []
        if isinstance(models_raw, list):
            models = [str(m).strip() for m in models_raw if str(m).strip()]
        elif isinstance(models_raw, str):
            models = [
                s.strip()
                for s in models_raw.replace("\r", "\n").replace(",", "\n").split("\n")
                if s.strip()
            ]

        if models:
            i18n_cfg["translator_models"] = models
            changed.append("translator_models")
        else:
            ignored.append("translator_models")
    if "supported_langs" in body and isinstance(body["supported_langs"], list):
        langs = [str(x).strip() for x in body["supported_langs"] if str(x).strip()]
        if langs:
            i18n_cfg["supported_langs"] = langs
            changed.append("supported_langs")
    if "preheat_top_n" in body:
        try:
            i18n_cfg["preheat_top_n"] = max(1, min(int(body["preheat_top_n"]), 500))
            changed.append("preheat_top_n")
        except Exception:
            pass
    if "preheat_min_volume_24h" in body:
        try:
            i18n_cfg["preheat_min_volume_24h"] = max(
                0.0, float(body["preheat_min_volume_24h"])
            )
            changed.append("preheat_min_volume_24h")
        except Exception:
            pass
    if not changed:
        return JSONResponse({"status": "noop", "changed": [], "ignored": ignored})
    try:
        backup_config()
        import json as _json

        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            _json.dump(cfg, f, ensure_ascii=False, indent=2)
        # 触发热加载，使新配置立即生效
        load_config(force=True)
    except Exception as exc:
        return JSONResponse(
            {"status": "error", "error": f"save_config failed: {exc}"}, status_code=500
        )
    return {"status": "ok", "changed": changed, "ignored": ignored, "i18n": i18n_cfg}


@router.post("/api/pm/i18n/clear")
async def pm_i18n_clear(request: Request):
    _ident = get_request_identity(request)
    if not (_ident and _ident.get("is_admin")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    lang = (body.get("lang") if isinstance(body, dict) else None) or None
    try:
        from src.utils.translator import clear_cache
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)
    n = clear_cache(lang)
    return {"status": "ok", "cleared": int(n), "lang": lang or "all"}


# ===== T345: i18n 文件 / DB 行管理（管理员） =====
import os as _os_t345
from pathlib import Path as _Path_t345

_I18N_JSON_DIR = (
    _Path_t345(__file__).resolve().parent.parent / "frontend" / "static" / "i18n"
)


def _t345_check_admin(request: Request):
    try:
        ident = get_request_identity(request)
        if ident and ident.get("is_admin"):
            return True
    except Exception:
        pass
    return False


@router.get("/api/pm/i18n/json/{lang}")
async def pm_i18n_json_get(lang: str, request: Request):
    """T345: 读取前端静态 i18n JSON 文件内容（管理员）。"""
    if not _t345_check_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    safe_lang = "".join(c for c in lang if c.isalnum() or c in ("-", "_"))[:16]
    if not safe_lang:
        return JSONResponse({"error": "invalid lang"}, status_code=400)
    fp = _I18N_JSON_DIR / f"{safe_lang}.json"
    if not fp.exists():
        return JSONResponse(
            {"status": "ok", "lang": safe_lang, "exists": False, "content": ""}
        )
    try:
        return {
            "status": "ok",
            "lang": safe_lang,
            "exists": True,
            "content": fp.read_text(encoding="utf-8"),
            "size": fp.stat().st_size,
        }
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


@router.post("/api/pm/i18n/json/{lang}")
async def pm_i18n_json_save(lang: str, request: Request):
    """T345: 保存前端静态 i18n JSON 文件（管理员）。"""
    import json as _json

    if not _t345_check_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    safe_lang = "".join(c for c in lang if c.isalnum() or c in ("-", "_"))[:16]
    if not safe_lang:
        return JSONResponse({"error": "invalid lang"}, status_code=400)
    body = {}
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    content = (body.get("content") or "").strip() if isinstance(body, dict) else ""
    if not content:
        return JSONResponse({"error": "empty content"}, status_code=400)
    # 校验 JSON 合法性
    try:
        _json.loads(content)
    except Exception as exc:
        return JSONResponse(
            {"status": "error", "error": f"invalid JSON: {exc}"}, status_code=400
        )
    try:
        _I18N_JSON_DIR.mkdir(parents=True, exist_ok=True)
        fp = _I18N_JSON_DIR / f"{safe_lang}.json"
        # 备份旧文件
        if fp.exists():
            bak = _I18N_JSON_DIR / f"{safe_lang}.json.bak"
            try:
                bak.write_text(fp.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass
        fp.write_text(content, encoding="utf-8")
        return {"status": "ok", "lang": safe_lang, "size": fp.stat().st_size}
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


@router.get("/api/pm/i18n/db/rows")
async def pm_i18n_db_rows(request: Request):
    """T345: 浏览 event_translations DB 行（管理员，分页）。"""
    if not _t345_check_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    lang = (request.query_params.get("lang") or "").strip() or None
    try:
        limit = int(request.query_params.get("limit") or 50)
    except Exception:
        limit = 50
    try:
        offset = int(request.query_params.get("offset") or 0)
    except Exception:
        offset = 0
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    try:
        from src.utils.database import db_connect

        with db_connect() as conn:
            if lang:
                total_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM event_translations WHERE lang=%s",
                    (lang,),
                ).fetchone()
                rows = conn.execute(
                    "SELECT slug, lang, title, COALESCE(model,'') AS model, COALESCE(updated_at::text,'') AS updated_at "
                    "FROM event_translations WHERE lang=%s ORDER BY updated_at DESC NULLS LAST LIMIT %s OFFSET %s",
                    (lang, limit, offset),
                ).fetchall()
            else:
                total_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM event_translations"
                ).fetchone()
                rows = conn.execute(
                    "SELECT slug, lang, title, COALESCE(model,'') AS model, COALESCE(updated_at::text,'') AS updated_at "
                    "FROM event_translations ORDER BY updated_at DESC NULLS LAST LIMIT %s OFFSET %s",
                    (limit, offset),
                ).fetchall()
        total = int((total_row or {}).get("c") or 0) if total_row else 0
        out_rows = [dict(r) for r in (rows or [])]
        return {
            "status": "ok",
            "total": total,
            "limit": limit,
            "offset": offset,
            "rows": out_rows,
        }
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


@router.delete("/api/pm/i18n/db/row")
async def pm_i18n_db_row_delete(request: Request):
    """T345: 删除指定 slug+lang 的翻译行（管理员）。"""
    if not _t345_check_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    slug = (request.query_params.get("slug") or "").strip()
    lang = (request.query_params.get("lang") or "").strip()
    if not slug or not lang:
        return JSONResponse({"error": "slug and lang required"}, status_code=400)
    try:
        from src.utils.database import db_connect

        with db_connect() as conn:
            cur = conn.execute(
                "DELETE FROM event_translations WHERE slug=%s AND lang=%s", (slug, lang)
            )
            n = cur.rowcount or 0
        return {"status": "ok", "deleted": int(n), "slug": slug, "lang": lang}
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


# ===== END T345 =====


@router.post("/api/pm/i18n/preheat")
async def pm_i18n_preheat(request: Request):
    """T341: 管理员触发翻译缓存预热 — 后台异步任务，立即返回 task_id 避免 Cloudflare 524。"""
    import os as _os
    import time as _time
    import threading as _threading

    _ident = get_request_identity(request)
    if not (_ident and _ident.get("is_admin")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    lang = ((body.get("lang") if isinstance(body, dict) else None) or "zh-CN").strip()
    if lang == "en":
        return JSONResponse(
            {"status": "error", "error": "en 是源语言，无需预热翻译"}, status_code=400
        )
    limit = int(
        (body.get("limit") if isinstance(body, dict) else 0)
        or get("i18n.preheat_top_n")
        or 60
    )
    limit = max(1, min(limit, 500))
    force = bool((body.get("force") if isinstance(body, dict) else False))
    if not _llm_api_key_set():
        return JSONResponse(
            {"status": "error", "error": "DEEPSEEK_API_KEY 未设置"}, status_code=500
        )

    # 已有任务运行中（同一语种） → 拒绝重复
    existing = _PREHEAT_TASKS.get(lang)
    if existing and existing.get("status") == "running":
        return JSONResponse(
            {
                "status": "already_running",
                "lang": lang,
                "progress": existing,
            },
            status_code=200,
        )

    _PREHEAT_TASKS[lang] = {
        "status": "running",
        "lang": lang,
        "limit": limit,
        "force": force,
        "total": 0,
        "skipped": 0,
        "cached": 0,
        "translated": 0,
        "failed": 0,
        "started_at": _time.time(),
        "finished_at": None,
        "duration_sec": 0,
        "error": None,
    }
    th = _threading.Thread(
        target=_run_preheat_sync, args=(lang, limit, force), daemon=True
    )
    th.start()
    return {
        "status": "started",
        "lang": lang,
        "limit": limit,
        "force": force,
        "message": "预热任务已在后台启动，请通过 /api/pm/i18n/preheat/status 查询进度",
    }


# T341: 全局后台任务进度表（按语种）
_PREHEAT_TASKS: dict = {}


def _run_preheat_sync(lang: str, limit: int, force: bool) -> None:
    """T341: 后台线程执行实际的翻译预热，更新 _PREHEAT_TASKS 进度。"""
    import os as _os
    import time as _time

    state = _PREHEAT_TASKS.get(lang)
    if not state:
        return
    try:
        from src.utils.translator import get_cached, translate_event
    except Exception as exc:
        state["status"] = "error"
        state["error"] = f"translator import failed: {exc}"
        state["finished_at"] = _time.time()
        return

    min_vol = float(get("i18n.preheat_min_volume_24h") or 5000)
    min_hours = float(get("i18n.preheat_min_hours_to_end") or 24)
    excluded = set(
        s.lower() for s in (get("i18n.preheat_excluded_tags") or ["resolved"])
    )
    _os.environ["VEYRA_I18N_FORCE"] = "1"

    try:
        resp = _pm_session().get(
            f"{_GAMMA_API}/events",
            params={
                "limit": limit,
                "offset": 0,
                "order": "volume24hr",
                "ascending": "false",
                "active": "true",
                "closed": "false",
            },
            timeout=20,
        )
        resp.raise_for_status()
        events = [e for e in resp.json() if isinstance(e, dict)]
    except Exception as exc:
        state["status"] = "error"
        state["error"] = f"Gamma fetch failed: {exc}"
        state["finished_at"] = _time.time()
        return

    state["total"] = len(events)
    n_cached = n_translated = n_failed = n_skipped = 0
    for ev in events:
        slug = (ev.get("slug") or "").strip().lower()
        if not slug or not (ev.get("title") or "").strip():
            n_skipped += 1
        elif float(ev.get("volume24hr") or 0) < min_vol:
            n_skipped += 1
        else:
            # T868: throwaway 事件（已结束/即将过期/日内）不翻译
            _drop, _why = _is_throwaway_event(ev, min_hours)
            if _drop:
                logger.debug("preheat skip throwaway slug=%s reason=%s", slug, _why)
                n_skipped += 1
                state["skipped"] = n_skipped
                continue
            tags = ev.get("tags") or []
            skip_tag = any(
                ((t.get("slug") if isinstance(t, dict) else "") or "").lower()
                in excluded
                for t in tags
            )
            if skip_tag:
                n_skipped += 1
            elif not force and get_cached(slug, lang):
                n_cached += 1
            else:
                try:
                    tr = translate_event(
                        slug,
                        lang,
                        ev.get("title") or "",
                        ev.get("description") or "",
                        use_cache=not force,
                    )
                    if tr and tr.get("title"):
                        n_translated += 1
                    else:
                        n_failed += 1
                except Exception as _exc:
                    logger.debug("preheat slug=%s lang=%s failed: %s", slug, lang, _exc)
                    n_failed += 1
                _time.sleep(0.15)
        # 实时更新进度
        state["skipped"] = n_skipped
        state["cached"] = n_cached
        state["translated"] = n_translated
        state["failed"] = n_failed

    state["status"] = "done"
    state["finished_at"] = _time.time()
    state["duration_sec"] = round(state["finished_at"] - state["started_at"], 1)


@router.get("/api/pm/i18n/preheat/status")
async def pm_i18n_preheat_status(request: Request):
    """T341: 查询所有/指定语种的预热任务进度（公开但只返回进度，无敏感信息）。"""
    lang = (request.query_params.get("lang") or "").strip()
    if lang:
        st = _PREHEAT_TASKS.get(lang)
        if not st:
            return {"status": "not_found", "lang": lang}
        return {"status": "ok", "task": st}
    return {"status": "ok", "tasks": _PREHEAT_TASKS}


# ---------------------------------------------------------------------------
# 用户态端点（需要登录）
# ---------------------------------------------------------------------------


@router.get("/api/pm/deposit-info")
async def pm_deposit_info(request: Request):
    """T289 Wave 5: 返回 funder_address + USDC 充值指引参数。"""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "")
    funder = _get_user_funder(user_wallet)
    if not funder:
        # T876-hotfix2: return 200 + status='unbound' instead of 412 so the browser
        # doesn't log a network error for the (expected) deposit-wallet-pending state.
        return {
            "status": "unbound",
            "user_wallet": user_wallet,
            "owner_eoa": user_wallet,
            "funder_address": None,
            "deposit_wallet": None,
            "error": "funder_address not bound",
            "action": "visit /go and complete wallet binding first",
        }

    # T546 — 同步 RPC 余额查询走线程池。
    balances, wallet_type = await asyncio.gather(
        asyncio.to_thread(_fetch_usdc_balances, funder),
        asyncio.to_thread(_get_user_wallet_type, user_wallet),
    )
    # T471: pUSD is the canonical trading collateral post-cutover.
    # USDC.e auto-wraps via CollateralOnramp; native USDC does NOT auto-wrap
    # and requires explicit convert via /api/deposit/usdc-to-pusd.
    pusd_balance = balances.get("pusd")
    pending_wrap = balances.get("usdc_e")
    usdc_native_bal = balances.get("usdc_native")
    # tradable = pUSD + USDC.e (auto-wrapping). Native USDC is NOT counted as
    # tradable here — it must be explicitly converted first. We still include
    # it in usdc_total for display, but trade-gate checks pusd separately.
    tradable_parts = [
        v for v in (pusd_balance, pending_wrap) if v is not None
    ]
    tradable = sum(tradable_parts) if tradable_parts else pusd_balance or 0
    balance = tradable
    min_trade = _safe_float(get("trading.min_usdc_to_trade") or 1.0, 1.0)
    alert_at = _safe_float(get("trading.min_safe_balance_alert") or 5.0, 5.0)

    return {
        "status": "ok",
        "user_wallet": user_wallet,
        # T911 — explicit owner EOA alias so the browser orchestrator never
        # has to guess. Same value as user_wallet (= users.wallet_address),
        # but named after its on-chain role so polymarket_session.js can use
        # it directly as the expected signer for the Privy walletClient.
        "owner_eoa": user_wallet,
        "wallet_type": wallet_type,
        "funder_address": funder,
        # T911 — alias for clarity at SDK layer; this address is what
        # ClobClient.funderAddress and RelayClient.executeDepositWalletBatch
        # must operate on. Hard-coded equal to funder_address (DB truth).
        "deposit_wallet": funder,
        "chain": "Polygon",
        "chain_id": 137,
        "usdc_contracts": {
            "pusd": _PUSD_TOKEN,
            "usdc_e": _USDC_E_TOKEN,
            "usdc_native": _USDC_NATIVE_TOKEN,
        },
        "usdc_symbol": _pm_v2.PUSD_SYMBOL,
        "usdc_decimals": _pm_v2.PUSD_DECIMALS,
        "display_token": _pm_v2.PUSD_SYMBOL,
        "usdc_balance": balance if balance is not None else None,
        "tradable_balance": tradable,
        "pusd_balance": pusd_balance,
        "usdc_e_balance": balances["usdc_e"],
        "usdc_native_balance": balances["usdc_native"],
        "usdc_total_balance": balances["usdc_total"],
        "balance_status": (
            "not_fetched"
            if balance is None
            else (
                "blocked"
                if balance < min_trade
                else ("warning" if balance < alert_at else "ok")
            )
        ),
        "thresholds": {
            "min_usdc_to_trade": min_trade,
            "min_safe_balance_alert": alert_at,
        },
        "notes": [
            "仅通过 Polygon 主网转账 USDC 到上方地址，其他链（ETH/Arb/Base）转入将永久丢失。",
            "请在交易所/钱包提币时务必选择 Polygon 网络（PoS），并使用原生 USDC（0x3c49...3359）。",
            "到账通常 1-3 分钟；若 10 分钟未到账，请通过交易哈希在 polygonscan.com 核查。",
        ],
    }


@router.get("/api/pm/balance-check")
async def pm_balance_check(request: Request):
    """T289 Wave 5: 实时 USDC 余额 + 最低阈值校验（下单前调用）。"""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "")
    funder = _get_user_funder(user_wallet)
    if not funder:
        # T876-hotfix2: 200 + status='unbound' (was 412) — silences console errors on
        # the expected deposit-wallet-pending state without changing the bound-path.
        return {
            "status": "unbound",
            "user_wallet": user_wallet,
            "error": "funder_address not bound",
        }

    # T546 — RPC USDC 余额查询是同步 web3 调用，需中转到线程池
    # 避免阅读 /balance-check 时阐塞事件循环。
    balances = await asyncio.to_thread(_fetch_usdc_balances, funder)
    balance = balances["usdc_total"]

    balances, balance = await asyncio.to_thread(
        _resolve_usdc_total_with_fallback, funder, balances
    )

    min_trade = _safe_float(get("trading.min_usdc_to_trade") or 1.0, 1.0)
    alert_at = _safe_float(get("trading.min_safe_balance_alert") or 5.0, 5.0)

    can_trade = balance is not None and balance >= min_trade
    should_deposit = balance is None or balance < alert_at

    # T883 [FundFlow]: when a deposit_wallet user is detected with on-chain
    # balance > 0, opportunistically trigger gasless approvals via Builder
    # Relayer. Throttled per-user (5 min) and fully async — never blocks the
    # balance-check response. No-op for SAFE users or when relayer creds are
    # missing.
    try:
        if balance is not None and balance > 0:
            _maybe_auto_ensure_approvals(user_wallet, funder)
    except Exception as _exc:
        logger.debug("[FundFlow] auto ensure_approvals skipped: %s", _exc)

    alerts: list[str] = []
    if balance is None:
        alerts.append("无法获取链上余额，请稍后重试或检查 RPC 连接")
    elif balance < min_trade:
        alerts.append(
            f"余额 ${balance:.2f} 低于最低下单门槛 ${min_trade:.2f}，请先充值"
        )
    elif balance < alert_at:
        alerts.append(
            f"余额 ${balance:.2f} 低于建议安全线 ${alert_at:.2f}，建议尽快充值"
        )

    return {
        "status": "ok",
        "funder_address": funder,
        "usdc_balance": balance,
        "usdc_e_balance": balances["usdc_e"],
        "usdc_native_balance": balances["usdc_native"],
        "usdc_total_balance": balance,
        "can_trade": can_trade,
        "should_deposit": should_deposit,
        "thresholds": {
            "min_usdc_to_trade": min_trade,
            "min_safe_balance_alert": alert_at,
            # T313/T315: 同步交易参数给前端避免硬编码
            "min_sell_share_unit": _safe_float(
                get("trading.min_sell_share_unit") or 0.01, 0.01
            ),
            "buy_price_cap": _safe_float(get("trading.buy_price_cap") or 0.99, 0.99),
            "max_order_size_usdc": _safe_float(
                get("trading.max_order_size_usdc") or 500.0, 500.0
            ),
        },
        "alerts": alerts,
    }


@router.post("/api/pm/ensure-approvals")
async def pm_ensure_approvals(request: Request):
    """T878 Wave-C — Trigger gasless ERC-20 approvals via Builder Relayer.

    Soft endpoint: returns the relayer client status. The frontend may
    call this opportunistically (e.g. after a balance > 0 is detected)
    but it never blocks the user flow. When BUILDER_* credentials are
    not configured the response is ``{"status": "no_credentials"}`` and
    the UI must NOT surface a broken affordance — instead it should fall
    back to the normal in-site flow that already works today (Safe path).
    """

    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "").lower()
    funder = _get_user_funder(user_wallet)
    if not funder:
        return {
            "status": "unbound",
            "user_wallet": user_wallet,
            "error": "deposit wallet not yet provisioned",
        }

    from src.utils.pm_relayer_client import ensure_approvals

    result = await asyncio.to_thread(ensure_approvals, funder, user_wallet)
    result.setdefault("deposit_wallet", funder)
    result.setdefault("owner_eoa", user_wallet)
    return result


# ---------------------------------------------------------------------------
# T911-Wave5 — Market metadata server proxy (negRisk + tickSize)
# Browser fetch to clob.polymarket.com/markets is CORS-blocked → silently
# fell back to defaults negRisk=false tickSize=0.01 → CTF Exchange domain
# mismatch on neg_risk markets → CLOB rejects order with "invalid signature".
# This server route resolves token_id → conditionId via gamma, then pulls
# negRisk + tick_size from CLOB. 60s TTL cache. NEVER returns silent defaults
# on failure — returns explicit error so client can refuse to sign.
# ---------------------------------------------------------------------------
_PM_MARKET_META_CACHE: dict[str, tuple[float, dict]] = {}
_PM_MARKET_META_TTL = 60.0


def _resolve_market_meta(token_id: str = "", condition_id: str = "") -> dict:
    """Return {ok, negRisk, tickSize, minSize, conditionId, source}.
    On failure returns {ok: False, error: <reason>}; caller MUST refuse to sign.
    """
    tid = str(token_id or "").strip()
    cid = str(condition_id or "").strip().lower()
    cache_key = f"t={tid}|c={cid}"
    ent = _PM_MARKET_META_CACHE.get(cache_key)
    if ent and (time.time() - ent[0]) < _PM_MARKET_META_TTL:
        return ent[1]
    try:
        # Step 1 — derive conditionId from token_id via gamma if not provided.
        if not cid and tid:
            try:
                gr = requests.get(
                    f"{_GAMMA_API}/markets",
                    params={"clob_token_ids": tid},
                    timeout=8,
                    headers={"User-Agent": "veyra-trade/1.0"},
                )
                if gr.ok:
                    arr = gr.json() or []
                    if isinstance(arr, list) and arr:
                        m0 = arr[0] or {}
                        cid = str(m0.get("conditionId") or m0.get("condition_id") or "").strip().lower()
            except Exception as e:
                logger.warning("[market-meta] gamma resolve failed token=%s: %s", tid[:16], e)
        if not cid:
            out = {"ok": False, "error": "conditionId not resolvable", "token_id": tid[:16]}
            _PM_MARKET_META_CACHE[cache_key] = (time.time(), out)
            return out
        # Step 2 — pull authoritative negRisk + tick from CLOB
        cm = {}
        try:
            cr = requests.get(
                f"{_CLOB_PUBLIC}/markets/{cid}",
                timeout=8,
                headers={"User-Agent": "veyra-trade/1.0"},
            )
            if cr.ok:
                cm = cr.json() or {}
        except Exception as e:
            logger.warning("[market-meta] clob fetch failed cid=%s: %s", cid[:18], e)
        # neg_risk / tick_size keys vary across endpoints
        neg = cm.get("neg_risk")
        if neg is None:
            neg = cm.get("negRisk")
        # Fallback to gamma if CLOB doesn't report it
        if neg is None and tid:
            try:
                gr = requests.get(
                    f"{_GAMMA_API}/markets",
                    params={"clob_token_ids": tid},
                    timeout=8,
                    headers={"User-Agent": "veyra-trade/1.0"},
                )
                if gr.ok:
                    arr = gr.json() or []
                    if isinstance(arr, list) and arr:
                        neg = arr[0].get("negRisk")
            except Exception:
                pass
        if neg is None:
            out = {"ok": False, "error": "negRisk not resolvable", "conditionId": cid}
            _PM_MARKET_META_CACHE[cache_key] = (time.time(), out)
            return out
        tick_raw = cm.get("minimum_tick_size") or cm.get("tick_size") or cm.get("tickSize")
        try:
            tick_str = str(tick_raw) if tick_raw is not None else "0.01"
            float(tick_str)  # validate numeric
        except Exception:
            tick_str = "0.01"
        min_size_raw = cm.get("minimum_order_size") or cm.get("min_order_size") or 5
        try:
            min_size = float(min_size_raw)
        except Exception:
            min_size = 5.0
        out = {
            "ok": True,
            "negRisk": bool(neg),
            "tickSize": tick_str,
            "minSize": min_size,
            "conditionId": cid,
            "source": "clob+gamma",
        }
        _PM_MARKET_META_CACHE[cache_key] = (time.time(), out)
        return out
    except Exception as e:
        logger.error("[market-meta] resolve crashed token=%s cid=%s: %s", tid[:16], cid[:18], e)
        out = {"ok": False, "error": f"resolve crashed: {e}"}
        _PM_MARKET_META_CACHE[cache_key] = (time.time(), out)
        return out


@router.get("/api/pm/market-meta")
async def pm_market_meta(token_id: str = "", condition_id: str = ""):
    """Server-side proxy returning authoritative {negRisk, tickSize, minSize}.
    Browser can't hit clob.polymarket.com directly (CORS), so this avoids
    silent defaults that produce EIP-712 domain mismatch ("invalid signature").
    """
    if not token_id and not condition_id:
        return {"ok": False, "error": "token_id or condition_id required"}
    result = await asyncio.to_thread(_resolve_market_meta, token_id, condition_id)
    return result


# ---------------------------------------------------------------------------
# T906 — Deposit Wallet (POLY_1271) gasless approval provisioning
# ---------------------------------------------------------------------------
import pathlib as _pathlib  # noqa: E402  (module-level import — used below)
# Per https://docs.polymarket.com/trading/deposit-wallets the deposit wallet
# must hold its own on-chain ERC-20 / ERC-1155 approvals before placing a
# POLY_1271 order. Without these the relayer accepts the order but the EVM
# execution reverts and the user sees a "policy_violation" / "insufficient
# allowance" failure. We provision them through a 3-step gasless flow:
#
#   1. /api/pm/trade/provision-approvals/check   — server discovers missing
#      approvals, returns either {status:"ok"} or {status:"needs_provisioning",
#      typed_data, prepare_token}
#   2. Browser signs the typed data with the user's Privy embedded wallet
#      (the deposit wallet's owner EOA).
#   3. /api/pm/trade/provision-approvals/submit — server attaches the
#      signature to a DepositWalletBatchRequest, posts to relayer's /submit,
#      polls until the tx mines, returns the tx hash.

_PROVISION_PREP_DIR = _pathlib.Path("/tmp/pm_provision_prep")  # type: ignore[name-defined]
_PROVISION_PREP_DIR.mkdir(parents=True, exist_ok=True)
_PROVISION_PREP_TTL_SECS = 600
_PROVISION_PREP_CACHE: dict[str, dict] = {}


def _provision_prep_gc(now_ts: float) -> None:
    try:
        for p in _PROVISION_PREP_DIR.glob("*.json"):
            try:
                if (now_ts - p.stat().st_mtime) > _PROVISION_PREP_TTL_SECS:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass
    stale = [
        k for k, v in _PROVISION_PREP_CACHE.items()
        if (now_ts - float(v.get("_ts", 0))) > _PROVISION_PREP_TTL_SECS
    ]
    for k in stale:
        _PROVISION_PREP_CACHE.pop(k, None)


def _provision_prep_write(token: str, data: dict) -> None:
    _PROVISION_PREP_CACHE[token] = data
    target = _PROVISION_PREP_DIR / f"{token}.json"
    tmp = _PROVISION_PREP_DIR / f"{token}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(target)


def _provision_prep_pop(token: str) -> dict | None:
    target = _PROVISION_PREP_DIR / f"{token}.json"
    if target.exists():
        try:
            data = json.loads(target.read_text() or "{}")
            target.unlink(missing_ok=True)
            return data
        except Exception:
            target.unlink(missing_ok=True)
    return _PROVISION_PREP_CACHE.pop(token, None)


@router.post("/api/pm/trade/provision-approvals/check")
async def pm_provision_approvals_check(request: Request):
    """T906: discover missing on-chain approvals + return typed data to sign.

    Always returns 200. Frontend inspects ``status``:
      ``ok``                 — nothing to do; proceed with order.
      ``needs_provisioning`` — sign typed_data with Privy then POST /submit.
      ``no_credentials``     — builder relayer creds missing (operator config).
      ``client_unavailable`` — relayer client init failed.
      ``error``              — discover/get_nonce failed; UI can retry.
    """
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "").lower()
    if not user_wallet:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    wallet_type = _get_user_wallet_type(user_wallet)
    if wallet_type != "deposit_wallet":
        # SAFE/proxy users do not need user-side approvals: their proxy/Safe
        # already holds the allowances (managed by the legacy proxy flow).
        return {"status": "ok", "wallet_type": wallet_type}

    funder = _get_user_funder(user_wallet)
    if not funder:
        return {
            "status": "unbound",
            "user_wallet": user_wallet,
            "error": "deposit wallet not yet provisioned",
        }

    from src.utils.pm_deposit_wallet_provision import (  # noqa: PLC0415
        build_provision_request,
    )

    res = await asyncio.to_thread(
        build_provision_request,
        owner_eoa=user_wallet,
        deposit_wallet=funder,
    )

    status = res.get("status")
    if status != "needs_provisioning":
        # Pass-through: ok / no_credentials / client_unavailable / error.
        return res

    import secrets as _secrets  # noqa: PLC0415

    prepare_token = _secrets.token_urlsafe(24)
    now_ts = time.time()
    _provision_prep_gc(now_ts)
    _provision_prep_write(prepare_token, {
        "_ts": now_ts,
        "user_wallet": user_wallet,
        "owner_eoa": res["owner_eoa"],
        "deposit_wallet": res["deposit_wallet"],
        "nonce": res["nonce"],
        "deadline": res["deadline"],
        "calls": res["calls"],
        "missing": res["missing"],
    })

    logger.info(
        "[Provision] check user=%s deposit_wallet=%s missing=%d tok=%s",
        user_wallet[:10], res["deposit_wallet"][:10], len(res["missing"]),
        prepare_token[:8],
    )

    return {
        "status": "needs_provisioning",
        "prepare_token": prepare_token,
        "expires_in": _PROVISION_PREP_TTL_SECS,
        "typed_data": res["typed_data"],
        "owner_eoa": res["owner_eoa"],
        "deposit_wallet": res["deposit_wallet"],
        "missing": res["missing"],
        "nonce": res["nonce"],
        "deadline": res["deadline"],
        "chain_id": res["chain_id"],
    }


@router.post("/api/pm/trade/provision-approvals/submit")
async def pm_provision_approvals_submit(request: Request):
    """T906: receive user signature, submit to relayer, poll mined."""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "").lower()
    if not user_wallet:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    prepare_token = str(body.get("prepare_token") or "").strip()
    signature_hex = str(body.get("signature") or "").strip()
    if not prepare_token or not signature_hex:
        return JSONResponse(
            {"error": "prepare_token and signature are required"},
            status_code=400,
        )

    now_ts = time.time()
    _provision_prep_gc(now_ts)
    cached = _provision_prep_pop(prepare_token)
    if not cached:
        return JSONResponse(
            {"error": "prepare_token_expired_or_unknown"}, status_code=410
        )
    if str(cached.get("user_wallet") or "").lower() != user_wallet:
        return JSONResponse({"error": "prepare_token_user_mismatch"}, status_code=403)

    from src.utils.pm_deposit_wallet_provision import submit_provision  # noqa: PLC0415

    try:
        result = await asyncio.to_thread(
            submit_provision,
            owner_eoa=cached["owner_eoa"],
            deposit_wallet=cached["deposit_wallet"],
            nonce=cached["nonce"],
            deadline=cached["deadline"],
            calls=cached["calls"],
            user_signature=signature_hex,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[Provision] submit raised: %s", exc)
        return JSONResponse(
            {"status": "error", "error": str(exc)}, status_code=500
        )

    status = result.get("status")
    logger.info(
        "[Provision] submit user=%s deposit_wallet=%s status=%s tx_hash=%s tx_id=%s",
        user_wallet[:10], cached["deposit_wallet"][:10], status,
        str(result.get("tx_hash") or "")[:14], result.get("tx_id"),
    )

    if status == "mined":
        # CLOB caches a snapshot of the on-chain allowance; tell it to refresh
        # so the next order doesn't see stale "0 allowance" state. This is the
        # POLY_1271 sync endpoint per py-clob-client-v2 examples.
        try:
            await asyncio.to_thread(_t906_clob_refresh_allowance, user_wallet)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Provision] CLOB allowance refresh failed: %s", exc)
        return result

    # error / failed / timeout → 502 so frontend can retry
    return JSONResponse(result, status_code=502)


def _t906_clob_refresh_allowance(user_wallet: str) -> None:
    """Tell CLOB to re-read on-chain allowance (POLY_1271 / sig_type=3)."""
    funder = _get_user_funder(user_wallet)
    if not funder:
        return
    try:
        from py_clob_client_v2.clob_types import (  # noqa: PLC0415
            AssetType,
            BalanceAllowanceParams,
        )
        from src.executor.poly1271_clob_ordering import (  # noqa: PLC0415
            get_builder_clob_client,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Provision] CLOB refresh import failed: %s", exc)
        return
    try:
        client = get_builder_clob_client(funder, user_eoa=user_wallet)
        client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        logger.info(
            "[Provision] CLOB allowance refreshed user=%s funder=%s",
            user_wallet[:10], funder[:10],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Provision] CLOB update_balance_allowance failed user=%s err=%s",
            user_wallet[:10], exc,
        )



def _resolve_usdc_total_with_fallback(
    funder: str, balances: dict[str, float | None]
) -> tuple[dict[str, float | None], float | None]:
    """Resolve a reliable tradable USD balance with PM data API fallback."""
    balance = balances.get("usdc_total")

    if balance is None or balance == 0.0:
        try:
            _balance_url = f"{_DATA_API}/value"
            _balance_resp = _pm_session().get(
                _balance_url, params={"user": funder}, timeout=8
            )
            if _balance_resp.ok:
                _val = _balance_resp.json()
                if isinstance(_val, list):
                    _val = _val[0] if _val else {}
                _api_bal = _safe_float(
                    _val.get("usdc_balance")
                    or _val.get("usdcBalance")
                    or _val.get("cash")
                    or _val.get("walletBalance")
                    or _val.get("balance"),
                    0.0,
                )
                if _api_bal > 0:
                    balance = _api_bal
                    balances = {
                        **balances,
                        "usdc_total": balance,
                        "usdc_native": balance,
                    }
        except Exception:
            pass

    return balances, balance


def _ensure_onramp_verifications_table() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS onramp_verifications (
                id SERIAL PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                funder_address TEXT,
                verify_key TEXT NOT NULL,
                trigger TEXT,
                before_balance REAL,
                after_balance REAL,
                delta_balance REAL,
                min_required REAL,
                status TEXT NOT NULL,
                reward_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_onramp_verify_wallet_key ON onramp_verifications(wallet_address, verify_key)"
            )
        except Exception:
            pass
        conn.commit()


@router.post("/api/pm/onramp/verify")
async def pm_onramp_verify(request: Request):
    """Verify onramp completion by server-side balance delta and settle points."""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "").strip().lower()
    funder = _get_user_funder(user_wallet)
    if not funder:
        return JSONResponse(
            {"status": "error", "error": "funder_address not bound"},
            status_code=412,
        )

    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    verify_key = str(payload.get("verify_key") or "").strip().lower()
    if not verify_key:
        verify_key = f"onramp-{int(time.time() * 1000)}"
    trigger = str(payload.get("trigger") or "manual").strip().lower()
    before_balance = _safe_float(payload.get("before_balance"), -1.0)

    balances = await asyncio.to_thread(_fetch_usdc_balances, funder)
    balances, current_balance = await asyncio.to_thread(
        _resolve_usdc_total_with_fallback, funder, balances
    )
    if current_balance is None:
        current_balance = 0.0
    if before_balance < 0:
        before_balance = float(current_balance)
    delta = max(0.0, float(current_balance) - float(before_balance))

    from scripts.dashboard_routes import points_routes as _points

    min_verified = max(
        0.01,
        _safe_float(_points.get_points_config_float("min_verified_deposit_usdc", 1.0), 1.0),
    )

    _ensure_onramp_verifications_table()
    now = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        duplicate = conn.execute(
            """SELECT status, after_balance, delta_balance, reward_json
               FROM onramp_verifications
               WHERE LOWER(wallet_address)=? AND verify_key=?
               LIMIT 1""",
            (user_wallet, verify_key),
        ).fetchone()
    if duplicate:
        reward_json = {}
        try:
            reward_json = json.loads(duplicate["reward_json"] or "{}")
        except Exception:
            reward_json = {}
        return {
            "status": "duplicate",
            "verify_key": verify_key,
            "funder_address": funder,
            "before_balance": before_balance,
            "after_balance": duplicate["after_balance"],
            "delta_balance": duplicate["delta_balance"],
            "min_required": min_verified,
            "reward": reward_json,
        }

    with db_connect() as conn:
        same_after = conn.execute(
            """SELECT id FROM onramp_verifications
               WHERE LOWER(wallet_address)=?
                 AND status='rewarded'
                 AND ABS(COALESCE(after_balance, 0) - ?) < 0.000001
               ORDER BY id DESC LIMIT 1""",
            (user_wallet, float(current_balance)),
        ).fetchone()

    if same_after or delta < min_verified:
        reason = "same_after_balance" if same_after else "delta_below_min"
        with db_connect() as conn:
            conn.execute(
                """INSERT INTO onramp_verifications(
                    wallet_address, funder_address, verify_key, trigger,
                    before_balance, after_balance, delta_balance, min_required,
                    status, reward_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'no_reward', ?, ?)""",
                (
                    user_wallet,
                    funder,
                    verify_key,
                    trigger,
                    float(before_balance),
                    float(current_balance),
                    float(delta),
                    float(min_verified),
                    json.dumps({"reason": reason}, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
        return {
            "status": "ok",
            "verify_key": verify_key,
            "credited": False,
            "reason": reason,
            "funder_address": funder,
            "before_balance": before_balance,
            "after_balance": current_balance,
            "delta_balance": delta,
            "min_required": min_verified,
        }

    user_points = _points._rule_points("onramp_deposit_user", default=0)
    granted_user_points = 0
    if user_points > 0:
        with db_connect() as conn:
            exists_tx = conn.execute(
                """SELECT id FROM point_transactions
                   WHERE LOWER(wallet_address)=? AND source='onramp_deposit' AND ref_id=?
                   LIMIT 1""",
                (user_wallet, verify_key),
            ).fetchone()
        if not exists_tx:
            _points._add_points(
                user_wallet,
                user_points,
                "onramp_deposit",
                f"Verified onramp deposit +${delta:.4f}",
                verify_key,
            )
            granted_user_points = int(user_points)

    referral_rewards = _points.award_referral_on_onramp_deposit(user_wallet, verify_key)
    reward = {
        "user": int(granted_user_points),
        "l1": int(referral_rewards.get("l1", 0)),
        "l2": int(referral_rewards.get("l2", 0)),
        "l3": int(referral_rewards.get("l3", 0)),
    }

    with db_connect() as conn:
        conn.execute(
            """INSERT INTO onramp_verifications(
                wallet_address, funder_address, verify_key, trigger,
                before_balance, after_balance, delta_balance, min_required,
                status, reward_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'rewarded', ?, ?)""",
            (
                user_wallet,
                funder,
                verify_key,
                trigger,
                float(before_balance),
                float(current_balance),
                float(delta),
                float(min_verified),
                json.dumps(reward, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()

    return {
        "status": "ok",
        "verify_key": verify_key,
        "credited": True,
        "funder_address": funder,
        "before_balance": before_balance,
        "after_balance": current_balance,
        "delta_balance": delta,
        "min_required": min_verified,
        "reward": reward,
    }


# ---------------------------------------------------------------------------
# 下单端点（走 ClobClientFactory per-user funder）
# ---------------------------------------------------------------------------


def _runtime_global_block() -> tuple[bool, str]:
    if bool(get("system.maintenance_mode")):
        return True, "system is in maintenance mode"
    if bool(get("risk.emergency_freeze")):
        return True, "global emergency freeze is enabled"
    return False, ""


def _get_user_runtime(user_wallet: str) -> dict:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT freeze_auto, auto_trading_enabled FROM user_runtime_controls WHERE wallet_address=?",
            (user_wallet,),
        ).fetchone()
    if not row:
        return {"freeze_auto": False, "auto_trading_enabled": True}
    return {
        "freeze_auto": bool(row["freeze_auto"]),
        "auto_trading_enabled": bool(row["auto_trading_enabled"]),
    }


def _ensure_manual_order_table() -> None:
    """PostgreSQL DDL — SQLite AUTOINCREMENT 不兼容，使用 SERIAL。"""
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_manual_orders (
                id SERIAL PRIMARY KEY,
                user_wallet TEXT NOT NULL,
                polymarket_wallet TEXT,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                order_type TEXT NOT NULL,
                order_id TEXT,
                status TEXT NOT NULL,
                builder_fee_usdc REAL NOT NULL DEFAULT 0,
                error TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL
            )
            """)
        conn.commit()


# ---------------------------------------------------------------------------
# T884.3 — Browser-side signing for deposit_wallet (sig_type=3) orders
# ---------------------------------------------------------------------------
# Cross-worker filesystem cache for prepared orders awaiting browser signature.
# Uses /tmp/pm_trade_prep/ so all gunicorn workers share the same entries.
import pathlib as _pathlib
import json as _json_mod

_PM_TRADE_PREPARE_DIR = _pathlib.Path("/tmp/pm_trade_prep")
_PM_TRADE_PREPARE_DIR.mkdir(parents=True, exist_ok=True)
_PM_TRADE_PREPARE_TTL_SECS = 300
# Legacy in-memory fallback (not used for new entries; kept for compat)
_PM_TRADE_PREPARE_CACHE: dict[str, dict] = {}
_PM_TRADE_AUTH_CACHE: dict[str, dict] = {}
_PM_TRADE_AUTH_TTL_SECS = 300
# T885 followup-2 (2026-05-07): the dashboard runs with multiple gunicorn workers
# (GUNICORN_WORKERS=3 by default), so the in-memory _PM_TRADE_AUTH_CACHE is not
# shared across workers — prepare on worker A and submit on worker B caused
# auth_token_expired_or_unknown (HTTP 410) for ~2/3 of bets. Mirror the
# PREPARE_DIR pattern: write/pop atomically through a shared filesystem dir.
_PM_TRADE_AUTH_DIR = _pathlib.Path("/tmp/pm_trade_auth")
_PM_TRADE_AUTH_DIR.mkdir(parents=True, exist_ok=True)
_EVM_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _pm_trade_prepare_gc(now_ts: float) -> None:
    # GC filesystem cache
    try:
        for p in _PM_TRADE_PREPARE_DIR.glob("*.json"):
            try:
                if (now_ts - p.stat().st_mtime) > _PM_TRADE_PREPARE_TTL_SECS:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        pass
    # GC legacy in-memory cache (safety)
    expired = [
        k for k, v in _PM_TRADE_PREPARE_CACHE.items()
        if (now_ts - float(v.get("_ts", 0))) > _PM_TRADE_PREPARE_TTL_SECS
    ]
    for k in expired:
        _PM_TRADE_PREPARE_CACHE.pop(k, None)


def _pm_trade_prepare_write(token: str, data: dict) -> None:
    """Write prepare-cache entry to shared filesystem (all workers)."""
    target = _PM_TRADE_PREPARE_DIR / f"{token}.json"
    tmp = _PM_TRADE_PREPARE_DIR / f"{token}.tmp"
    tmp.write_text(_json_mod.dumps(data))
    tmp.rename(target)


def _pm_trade_prepare_pop(token: str) -> dict | None:
    """Atomically read-and-delete a prepare-cache entry from filesystem."""
    target = _PM_TRADE_PREPARE_DIR / f"{token}.json"
    try:
        data = _json_mod.loads(target.read_text())
        target.unlink(missing_ok=True)
        return data
    except (FileNotFoundError, _json_mod.JSONDecodeError):
        # Fallback: try legacy in-memory cache
        return _PM_TRADE_PREPARE_CACHE.pop(token, None)


def _pm_trade_auth_gc(now_ts: float) -> None:
    # GC shared filesystem cache (cross-worker)
    try:
        for p in _PM_TRADE_AUTH_DIR.glob("*.json"):
            try:
                if (now_ts - p.stat().st_mtime) > _PM_TRADE_AUTH_TTL_SECS:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        pass
    # GC legacy in-memory cache (kept for compat)
    expired = [
        k for k, v in _PM_TRADE_AUTH_CACHE.items()
        if (now_ts - float(v.get("_ts", 0))) > _PM_TRADE_AUTH_TTL_SECS
    ]
    for k in expired:
        _PM_TRADE_AUTH_CACHE.pop(k, None)


def _pm_trade_auth_write(token: str, data: dict) -> None:
    """Write auth-challenge entry to shared filesystem (all gunicorn workers)."""
    target = _PM_TRADE_AUTH_DIR / f"{token}.json"
    tmp = _PM_TRADE_AUTH_DIR / f"{token}.tmp"
    try:
        tmp.write_text(_json_mod.dumps(data))
        tmp.rename(target)
    except OSError:
        # As a last resort fall back to in-memory cache (single-worker safety)
        _PM_TRADE_AUTH_CACHE[token] = data


def _pm_trade_auth_pop(token: str) -> dict | None:
    """Atomically read-and-delete an auth-challenge entry from filesystem."""
    target = _PM_TRADE_AUTH_DIR / f"{token}.json"
    try:
        data = _json_mod.loads(target.read_text())
        target.unlink(missing_ok=True)
        return data
    except (FileNotFoundError, _json_mod.JSONDecodeError):
        # Fallback: legacy in-memory cache (covers entries created before upgrade)
        return _PM_TRADE_AUTH_CACHE.pop(token, None)


def _resolve_operator_api_key_address() -> str:
    addr = str(os.environ.get("RELAYER_API_KEY_ADDRESS") or "").strip().lower()
    if _EVM_ADDR_RE.match(addr):
        return addr
    pk = str(os.environ.get("POLY_PRIVATE_KEY") or "").strip()
    if not pk:
        return ""
    try:
        from eth_account import Account  # noqa: PLC0415

        return str(Account.from_key(pk).address or "").strip().lower()
    except Exception:
        return ""


def _build_api_key_authorize_message(user_wallet: str, api_key_address: str) -> str:
    return (
        "Veyra.Trade API Key Authorization (Type=3)\n"
        f"user_wallet: {str(user_wallet or '').strip().lower()}\n"
        f"api_key_address: {str(api_key_address or '').strip().lower()}\n"
        "scope: polymarket_trade_submit_signed\n"
        "chain: polygon\n"
        "version: 1"
    )


def _build_api_key_authorize_typed_data(
    *,
    user_wallet: str,
    api_key_address: str,
    nonce_hex: str,
    issued_at: int,
) -> dict:
    # T885 fix (2026-05-07): switch from a custom "Authorization" envelope to the
    # canonical Polymarket CLOB ClobAuth typed-data. The browser-side signature
    # over this exact message is what /auth/derive-api-key requires (POLY_SIGNATURE).
    # nonce_hex / api_key_address are intentionally ignored here — they are kept
    # in the cached challenge metadata for backwards compat / audit only.
    #
    # T906 fix (2026-05-10): the previous schema declared `address` and `nonce` as
    # type "string". Polymarket's official CLOB ClobAuth schema (py-clob-client
    # signing/model.py) uses Address() and Uint() — i.e. EIP-712 types "address"
    # and "uint256". With the wrong types the browser-computed EIP-712 hash
    # diverges from the server-side hash and CLOB rejects the L1 headers with
    # 401 "Invalid L1 Request headers". Aligning the schema fixes derive-api-key.
    _ = nonce_hex, api_key_address  # noqa: F841 - retained for cache parity
    return {
        "primaryType": "ClobAuth",
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ],
        },
        "domain": {
            "name": "ClobAuthDomain",
            "version": "1",
            "chainId": 137,
        },
        "message": {
            "address": str(user_wallet or "").strip().lower(),
            "timestamp": str(int(issued_at)),
            "nonce": 0,
            "message": "This message attests that I control the given wallet",
        },
    }


def _issue_api_key_authorization_challenge(user_wallet: str) -> dict:
    import secrets as _secrets  # noqa: PLC0415

    uw = str(user_wallet or "").strip().lower()
    api_key_address = _resolve_operator_api_key_address()
    now_ts = time.time()
    _pm_trade_auth_gc(now_ts)
    auth_token = _secrets.token_urlsafe(24)
    nonce_hex = "0x" + _secrets.token_hex(32)
    issued_at = int(now_ts)
    typed_data = _build_api_key_authorize_typed_data(
        user_wallet=uw,
        api_key_address=api_key_address,
        nonce_hex=nonce_hex,
        issued_at=issued_at,
    )
    _PM_TRADE_AUTH_CACHE[auth_token] = {
        "_ts": now_ts,
        "user_wallet": uw,
        "api_key_address": api_key_address,
        "nonce": nonce_hex,
        "issued_at": issued_at,
        "typed_data": typed_data,
    }
    # T885 followup-2 (2026-05-07): also persist to shared filesystem so submit
    # handler on a different gunicorn worker can read it.
    _pm_trade_auth_write(auth_token, {
        "_ts": now_ts,
        "user_wallet": uw,
        "api_key_address": api_key_address,
        "nonce": nonce_hex,
        "issued_at": issued_at,
        "typed_data": typed_data,
    })
    return {
        "auth_token": auth_token,
        "typed_data": typed_data,
        "api_key_address": api_key_address,
        "expected_signer": _preferred_user_signer(uw),
        "expires_in": _PM_TRADE_AUTH_TTL_SECS,
        "submit_url": "/api/pm/trade/authorize-api-key/submit",
    }


def _ensure_user_auth_columns() -> None:
    with db_connect() as conn:
        for ddl in (
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS clob_api_key_authorized BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS clob_api_key_authorized_at TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS clob_api_key_address TEXT",
            # T885 fix (2026-05-07): persist per-user CLOB API creds derived via
            # browser-side ClobAuth signature, so submit-signed orders no longer
            # depend on Privy server-side signing (which is blocked by policy).
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS clob_api_key TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS clob_api_secret TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS clob_api_passphrase TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS clob_api_signer_eoa TEXT",
        ):
            try:
                conn.execute(ddl)
            except Exception:
                pass
        conn.commit()


def _ensure_api_key_authorization_table() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_trade_api_key_authorizations (
                id SERIAL PRIMARY KEY,
                user_wallet TEXT NOT NULL,
                api_key_address TEXT NOT NULL,
                signer_wallet TEXT NOT NULL,
                signature_hex TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_wallet, api_key_address)
            )
            """
        )
        conn.commit()


def _get_user_main_wallet(user_wallet: str) -> str:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT main_wallet FROM users WHERE wallet_address=?",
            (str(user_wallet or "").strip().lower(),),
        ).fetchone()
    mw = str((row["main_wallet"] if row else "") or "").strip().lower()
    return mw


def _get_user_allowed_signers(user_wallet: str) -> set[str]:
    allowed: set[str] = set()
    uw = str(user_wallet or "").strip().lower()
    if _EVM_ADDR_RE.match(uw):
        allowed.add(uw)

    with db_connect() as conn:
        row = conn.execute(
            "SELECT main_wallet, linked_wallets_json FROM users WHERE wallet_address=?",
            (uw,),
        ).fetchone()

    if row:
        mw = str((row["main_wallet"] if row else "") or "").strip().lower()
        if _EVM_ADDR_RE.match(mw):
            allowed.add(mw)

        raw_linked = str((row["linked_wallets_json"] if row else "") or "").strip()
        if raw_linked:
            try:
                linked = json.loads(raw_linked)
                if isinstance(linked, list):
                    for item in linked:
                        if isinstance(item, str):
                            addr = item.strip().lower()
                            if _EVM_ADDR_RE.match(addr):
                                allowed.add(addr)
                        elif isinstance(item, dict):
                            addr = str(item.get("address") or "").strip().lower()
                            if _EVM_ADDR_RE.match(addr):
                                allowed.add(addr)
            except Exception:
                pass

    try:
        from scripts.dashboard_routes.redeem_withdraw_routes import (  # noqa: PLC0415
            _get_user_signer_binding,
        )

        binding = _get_user_signer_binding(uw, require_wallets=False) or {}
        for key in ("main_wallet", "proxy_wallet"):
            addr = str(binding.get(key) or "").strip().lower()
            if _EVM_ADDR_RE.match(addr):
                allowed.add(addr)
    except Exception:
        pass

    return allowed


def _preferred_user_signer(user_wallet: str) -> str:
    """T885 fix: always prefer the owner EOA (user_wallet) as the expected signer.

    Reason: deposit_wallet is an EIP-1271 contract wallet — it has no private key.
    All client-side EIP-712 signing must be performed by its owner EOA. The
    contract's isValidSignature() recovers the ECDSA back to the owner EOA on-chain.
    Returning the deposit_wallet here previously caused MetaMask to error out with
    "Required signer wallet 0x3992... is not the active account".
    """
    uw = str(user_wallet or "").strip().lower()
    allowed = _get_user_allowed_signers(user_wallet)
    if not allowed:
        return uw
    if uw in allowed:
        return uw
    # Fallback: any allowed signer (legacy linked wallets)
    return sorted(allowed)[0]


def _get_api_key_authorization_row(user_wallet: str, api_key_address: str) -> dict | None:
    _ensure_api_key_authorization_table()
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT user_wallet, api_key_address, signer_wallet, signature_hex, message,
                   created_at, updated_at
            FROM user_trade_api_key_authorizations
            WHERE user_wallet=? AND api_key_address=?
            LIMIT 1
            """,
            (
                str(user_wallet or "").strip().lower(),
                str(api_key_address or "").strip().lower(),
            ),
        ).fetchone()
    if not row:
        return None
    keys = row.keys() if hasattr(row, "keys") else []
    if keys:
        return {k: row[k] for k in keys}
    return None


def _get_user_clob_creds(user_wallet: str) -> dict | None:
    """T885 fix: read per-user CLOB API creds from DB.

    Returns dict with api_key/api_secret/api_passphrase/signer_eoa, or None.
    These creds are populated by /api/pm/trade/authorize-api-key/submit, which
    calls Polymarket /auth/derive-api-key with the browser-signed ClobAuth.
    """
    _ensure_user_auth_columns()
    uw = str(user_wallet or "").strip().lower()
    try:
        with db_connect() as conn:
            row = conn.execute(
                """
                SELECT clob_api_key, clob_api_secret, clob_api_passphrase, clob_api_signer_eoa
                FROM users WHERE wallet_address=? LIMIT 1
                """,
                (uw,),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    ak = str((row["clob_api_key"] if row else "") or "").strip()
    sc = str((row["clob_api_secret"] if row else "") or "").strip()
    pp = str((row["clob_api_passphrase"] if row else "") or "").strip()
    sg = str((row["clob_api_signer_eoa"] if row else "") or "").strip().lower()
    if not (ak and sc and pp):
        return None
    return {
        "api_key": ak,
        "api_secret": sc,
        "api_passphrase": pp,
        "signer_eoa": sg or uw,
    }


def _is_user_api_key_authorized(user_wallet: str, api_key_address: str) -> bool:
    _ensure_user_auth_columns()
    uw = str(user_wallet or "").strip().lower()
    ak = str(api_key_address or "").strip().lower()
    # T885 fix: require actual derived CLOB creds (apiKey/secret/passphrase),
    # not just a generic authorization signature. Without real creds CLOB will
    # reject submit-signed orders with "Unauthorized/Invalid api key".
    if not _get_user_clob_creds(uw):
        return False
    try:
        with db_connect() as conn:
            row = conn.execute(
                """
                SELECT clob_api_key_authorized, clob_api_key_address
                FROM users
                WHERE wallet_address=?
                LIMIT 1
                """,
                (uw,),
            ).fetchone()
        if row:
            authorized = bool(row["clob_api_key_authorized"])
            addr = str((row["clob_api_key_address"] if row else "") or "").strip().lower()
            if authorized and (not addr or addr == ak):
                return True
    except Exception:
        pass

    legacy = _get_api_key_authorization_row(uw, ak)
    if legacy:
        try:
            with db_connect() as conn:
                conn.execute(
                    """
                    UPDATE users
                    SET clob_api_key_authorized=TRUE,
                        clob_api_key_authorized_at=?,
                        clob_api_key_address=?
                    WHERE wallet_address=?
                    """,
                    (datetime.now(timezone.utc).isoformat(), ak, uw),
                )
                conn.commit()
        except Exception:
            pass
        return True
    return False


def _missing_api_key_authorization_response(user_wallet: str) -> JSONResponse:
    api_key_address = _resolve_operator_api_key_address()
    challenge = _issue_api_key_authorization_challenge(user_wallet)
    return JSONResponse(
        {
            "error": "api_key_authorization_required",
            "status": "silent_authorization_required",
            "hint": "Silent authorization is required once before trading.",
            "api_key_address": api_key_address,
            "expected_signer": challenge["expected_signer"],
            "authorization": challenge,
            "next": {
                "authorize_prepare": "/api/pm/trade/authorize-api-key/prepare",
                "authorize_submit": "/api/pm/trade/authorize-api-key/submit",
            },
        },
        status_code=412,
    )


def _ensure_user_api_key_authorized(user_wallet: str) -> JSONResponse | None:
    api_key_address = _resolve_operator_api_key_address()
    if not _EVM_ADDR_RE.match(api_key_address):
        return JSONResponse(
            {
                "error": "operator_api_key_address_unavailable",
                "hint": "RELAYER_API_KEY_ADDRESS or POLY_PRIVATE_KEY is not configured correctly.",
            },
            status_code=503,
        )
    if _is_user_api_key_authorized(user_wallet, api_key_address):
        return None
    return _missing_api_key_authorization_response(user_wallet)


@router.post("/api/pm/trade/authorize-api-key/prepare")
async def pm_trade_authorize_api_key_prepare(request: Request):
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "").strip().lower()
    if not _EVM_ADDR_RE.match(user_wallet):
        return JSONResponse({"error": "invalid session wallet"}, status_code=400)

    api_key_address = _resolve_operator_api_key_address()
    if _is_user_api_key_authorized(user_wallet, api_key_address):
        return {
            "status": "ok",
            "already_authorized": True,
            "api_key_address": api_key_address,
        }

    challenge = _issue_api_key_authorization_challenge(user_wallet)
    return {
        "status": "signing_required",
        "authorization": challenge,
    }


@router.post("/api/pm/trade/authorize-api-key/submit")
@router.post("/api/pm/trade/authorize-api-key")
async def pm_trade_authorize_api_key_submit(request: Request):
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "").strip().lower()
    if not _EVM_ADDR_RE.match(user_wallet):
        return JSONResponse({"error": "invalid session wallet"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    signature_hex = str(body.get("signature") or "").strip()
    auth_token = str(body.get("auth_token") or "").strip()
    api_key_address = str(body.get("api_key_address") or _resolve_operator_api_key_address()).strip().lower()
    if not signature_hex:
        return JSONResponse({"error": "signature is required"}, status_code=400)
    if not auth_token:
        return JSONResponse({"error": "auth_token is required"}, status_code=400)
    if not _EVM_ADDR_RE.match(api_key_address):
        return JSONResponse({"error": "invalid api_key_address"}, status_code=400)

    now_ts = time.time()
    _pm_trade_auth_gc(now_ts)
    cached = _pm_trade_auth_pop(auth_token)
    if not cached:
        return JSONResponse(
            {
                "error": "auth_token_expired_or_unknown",
                "hint": "Re-open the bet panel and retry.",
            },
            status_code=410,
        )
    if str(cached.get("user_wallet") or "").strip().lower() != user_wallet:
        return JSONResponse({"error": "auth_token_user_mismatch"}, status_code=403)

    typed_data = cached.get("typed_data") or {}
    expected_signer = _preferred_user_signer(user_wallet)
    try:
        from eth_account import Account  # noqa: PLC0415
        from eth_account.messages import encode_typed_data  # noqa: PLC0415

        recovered = str(
            Account.recover_message(
                encode_typed_data(full_message=typed_data),
                signature=signature_hex,
            )
            or ""
        ).strip().lower()
    except Exception as exc:
        return JSONResponse(
            {
                "error": "invalid_authorization_signature",
                "raw_error": str(exc),
            },
            status_code=400,
        )

    allowed = _get_user_allowed_signers(user_wallet)
    if recovered not in allowed:
        return JSONResponse(
            {
                "error": "authorization_signer_mismatch",
                "expected_signer": expected_signer,
                "allowed_signers": sorted(list(allowed)),
                "recovered_signer": recovered,
            },
            status_code=403,
        )

    _ensure_api_key_authorization_table()
    _ensure_user_auth_columns()
    now = datetime.now(timezone.utc).isoformat()
    message = _build_api_key_authorize_message(user_wallet, api_key_address)

    # T885 fix (2026-05-07): the same browser ClobAuth signature is now used to
    # call CLOB /auth/derive-api-key with POLY_ADDRESS=user_eoa. The returned
    # apiKey/secret/passphrase are persisted per-user so subsequent submit-signed
    # orders can authenticate as the user without depending on Privy server-side
    # signing (which is blocked by Privy policy_violation for embedded wallets).
    derived_creds: dict | None = None
    derive_error: str | None = None
    # T906 fix (2026-05-10): the official Polymarket SDK calls
    # `createOrDeriveApiKey()` which CREATES new credentials if none exist for
    # the signing EOA, otherwise derives the existing ones. Previously we only
    # called GET /auth/derive-api-key, which returns 400 "Could not derive api
    # key!" for first-time users (those who never had an API key minted).
    # Per https://docs.polymarket.com/api-reference/authentication, the flow is:
    #   POST /auth/api-key   -> create new creds (works for first-time users)
    #   GET  /auth/derive-api-key -> derive deterministic existing creds
    # Both endpoints accept the same L1 headers (POLY_ADDRESS, POLY_SIGNATURE,
    # POLY_TIMESTAMP, POLY_NONCE) signed over the ClobAuth EIP-712 typed-data.
    # We try CREATE first (idempotent for new users) and fall back to DERIVE
    # if CLOB indicates the key already exists.
    try:
        cached_ts = int(cached.get("issued_at") or 0)
        signer_for_l1 = recovered  # ClobAuth.address must match POLY_ADDRESS
        import requests as _requests  # noqa: PLC0415

        _l1_headers = {
            "POLY_ADDRESS": signer_for_l1,
            "POLY_SIGNATURE": signature_hex,
            "POLY_TIMESTAMP": str(cached_ts),
            "POLY_NONCE": "0",
        }

        def _parse_creds(_data: dict) -> dict | None:
            _k = str(_data.get("apiKey") or _data.get("api_key") or "").strip()
            _s = str(_data.get("secret") or _data.get("api_secret") or "").strip()
            _p = str(_data.get("passphrase") or _data.get("api_passphrase") or "").strip()
            if _k and _s and _p:
                return {
                    "api_key": _k,
                    "api_secret": _s,
                    "api_passphrase": _p,
                    "signer_eoa": signer_for_l1,
                }
            return None

        # Step 1: try CREATE (POST /auth/api-key) — works for first-time users.
        _create_resp = _requests.post(
            "https://clob.polymarket.com/auth/api-key",
            headers=_l1_headers,
            timeout=15,
        )
        _create_status = _create_resp.status_code
        _create_body = _create_resp.text[:300]
        if _create_status < 400:
            _data = _create_resp.json() or {}
            derived_creds = _parse_creds(_data)
            if derived_creds:
                logger.info(
                    "[FundFlow][poly1271][authorize-api-key] CREATED CLOB creds for user=%s api_key=%s",
                    user_wallet[:10], derived_creds["api_key"][:8],
                )
            else:
                derive_error = f"create-api-key empty fields body={_data}"
        else:
            # CREATE may fail because the user already has creds — fall through to DERIVE.
            logger.info(
                "[FundFlow][poly1271][authorize-api-key] create-api-key status=%s body=%s; falling back to derive-api-key",
                _create_status, _create_body,
            )

        # Step 2: if CREATE didn't yield creds, try DERIVE (GET /auth/derive-api-key).
        if not derived_creds:
            _resp = _requests.get(
                "https://clob.polymarket.com/auth/derive-api-key",
                headers=_l1_headers,
                timeout=15,
            )
            if _resp.status_code >= 400:
                derive_error = (
                    f"create=({_create_status},{_create_body[:120]}) "
                    f"derive=({_resp.status_code},{_resp.text[:120]})"
                )
                logger.error(
                    "[FundFlow][poly1271][authorize-api-key] CLOB create+derive both failed user=%s signer=%s: %s",
                    user_wallet[:10], signer_for_l1[:10], derive_error,
                )
            else:
                _data = _resp.json() or {}
                derived_creds = _parse_creds(_data)
                if derived_creds:
                    logger.info(
                        "[FundFlow][poly1271][authorize-api-key] DERIVED CLOB creds for user=%s api_key=%s",
                        user_wallet[:10], derived_creds["api_key"][:8],
                    )
                else:
                    derive_error = f"derive-api-key empty fields body={_data}"
    except Exception as _exc:
        derive_error = f"create_or_derive exception: {_exc}"
        logger.exception(
            "[FundFlow][poly1271][authorize-api-key] create_or_derive exception user=%s",
            user_wallet[:10],
        )

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO user_trade_api_key_authorizations(
                user_wallet, api_key_address, signer_wallet, signature_hex, message,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_wallet, api_key_address)
            DO UPDATE SET
                signer_wallet=excluded.signer_wallet,
                signature_hex=excluded.signature_hex,
                message=excluded.message,
                updated_at=excluded.updated_at
            """,
            (
                user_wallet,
                api_key_address,
                recovered,
                signature_hex,
                message,
                now,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE users
            SET clob_api_key_authorized=TRUE,
                clob_api_key_authorized_at=?,
                clob_api_key_address=?
            WHERE wallet_address=?
            """,
            (now, api_key_address, user_wallet),
        )
        if derived_creds:
            conn.execute(
                """
                UPDATE users
                SET clob_api_key=?,
                    clob_api_secret=?,
                    clob_api_passphrase=?,
                    clob_api_signer_eoa=?
                WHERE wallet_address=?
                """,
                (
                    derived_creds["api_key"],
                    derived_creds["api_secret"],
                    derived_creds["api_passphrase"],
                    derived_creds["signer_eoa"],
                    user_wallet,
                ),
            )
        conn.commit()

    logger.info(
        "[FundFlow][poly1271][authorize-api-key] user=%s signer=%s api_key_addr=%s",
        user_wallet[:10],
        recovered[:10],
        api_key_address[:10],
    )
    return {
        "status": "ok",
        "user_wallet": user_wallet,
        "signer_wallet": recovered,
        "api_key_address": api_key_address,
        "clob_creds_derived": bool(derived_creds),
        "clob_derive_error": derive_error,
    }


async def _pm_trade_validate_request(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """Shared validation extracted from pm_trade_submit. Returns (ctx, err_response).

    On success: ctx contains user_wallet, funder, token_id, price, size,
    side_raw, order_type_raw, notional, balance, wallet_type, is_deposit_wallet,
    body.
    """
    identity = get_request_identity(request)
    if not identity:
        return None, JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "")
    blocked, reason = _runtime_global_block()
    if blocked:
        return None, JSONResponse(
            {
                "error": reason,
                "maintenance_mode": bool(get("system.maintenance_mode")),
                "emergency_freeze": bool(get("risk.emergency_freeze")),
            },
            status_code=403,
        )

    runtime = _get_user_runtime(user_wallet)
    if runtime.get("freeze_auto") or not runtime.get("auto_trading_enabled"):
        return None, JSONResponse(
            {"error": "user trading is frozen or disabled", "runtime": runtime},
            status_code=403,
        )

    funder = _get_user_funder(user_wallet)
    if not funder:
        return None, JSONResponse(
            {"error": "funder_address not bound; complete /go wallet binding first"},
            status_code=412,
        )

    wallet_type = _get_user_wallet_type(user_wallet)
    is_deposit_wallet = wallet_type == "deposit_wallet"

    try:
        body = await request.json()
    except Exception:
        return None, JSONResponse({"error": "invalid JSON body"}, status_code=400)

    token_id = str(body.get("token_id") or "").strip()
    side_raw = str(body.get("side") or "BUY").upper().strip()
    order_type_raw = str(body.get("order_type") or "FOK").upper().strip()
    try:
        price = float(body.get("price"))
        size = float(body.get("size"))
    except Exception:
        return None, JSONResponse({"error": "price/size must be numeric"}, status_code=400)

    if not token_id or not token_id.isdigit():
        return None, JSONResponse(
            {"error": "token_id required (numeric string)"}, status_code=400
        )
    if side_raw not in {"BUY", "SELL"}:
        return None, JSONResponse({"error": "side must be BUY or SELL"}, status_code=400)
    if order_type_raw not in {"GTC", "FOK", "FAK"}:
        order_type_raw = "FOK"
    if not (0.001 <= price <= 0.999):
        return None, JSONResponse(
            {"error": "price must be between 0.001 and 0.999"}, status_code=400
        )
    if size <= 0:
        return None, JSONResponse({"error": "size must be > 0"}, status_code=400)

    min_order_size = 1
    try:
        ob_resp = _pm_session().get(
            f"{_CLOB_PUBLIC}/book", params={"token_id": token_id}, timeout=8
        )
        if ob_resp.ok:
            ob = ob_resp.json() or {}
            min_order_size = max(
                1, int(math.ceil(float(ob.get("min_order_size") or 1)))
            )
    except Exception:
        min_order_size = 1

    if side_raw == "BUY":
        min_required = min_order_size + 1
        size = float(math.ceil(size))
        if size < min_required:
            return None, JSONResponse(
                {
                    "error": "size_below_min_order_size",
                    "min_order_size": min_order_size,
                    "min_required_with_buffer": min_required,
                    "hint": "buy size must be integer and >= min_order_size + 1",
                },
                status_code=400,
            )

    notional = price * size
    max_notional = _safe_float(get("trading.max_order_size_usdc") or 500.0, 500.0)
    if notional > max_notional:
        return None, JSONResponse(
            {
                "error": f"order size ${notional:.2f} exceeds max ${max_notional:.2f}",
                "notional": notional,
                "max": max_notional,
            },
            status_code=400,
        )

    balances = _fetch_usdc_balances(funder)
    balance = balances["usdc_total"]
    min_trade = _safe_float(get("trading.min_usdc_to_trade") or 1.0, 1.0)
    if balance is None:
        return None, JSONResponse(
            {"error": "failed to read on-chain USDC balances; retry later"},
            status_code=503,
        )

    # T888: deposit_wallet users cannot trade with native USDC, but USDC.e is
    # auto-wrapped to pUSD on Safe interaction and should be treated as tradable.
    # Therefore only native USDC should trigger conversion-required blocking.
    if is_deposit_wallet:
        pusd_bal = float(balances.get("pusd") or 0)
        usdc_native_bal = float(balances.get("usdc_native") or 0)
        usdc_e_bal = float(balances.get("usdc_e") or 0)
        if pusd_bal < min_trade and usdc_native_bal > 0:
            return None, JSONResponse(
                {
                    "error": "usdc_needs_conversion",
                    "pusd_balance": pusd_bal,
                    "usdc_native_balance": usdc_native_bal,
                    "usdc_e_balance": usdc_e_bal,
                    "usdc_total_balance": balance,
                    "funder_address": funder,
                    "convert_url": "/api/deposit/usdc-to-pusd",
                    "hint": "Your deposit wallet has native USDC and needs conversion to pUSD before trading.",
                },
                status_code=402,
            )

    if balance < min_trade:
        return None, JSONResponse(
            {
                "error": "usdc_balance_below_minimum",
                "usdc_balance": balance,
                "usdc_e_balance": balances["usdc_e"],
                "usdc_native_balance": balances["usdc_native"],
                "min_usdc_to_trade": min_trade,
                "funder_address": funder,
                "hint": "请先在 /v2/deposit 页面充值原生 USDC（Polygon）到上方资金钱包",
            },
            status_code=402,
        )
    if side_raw == "BUY" and balance < notional:
        return None, JSONResponse(
            {
                "error": "insufficient_usdc",
                "required": notional,
                "available": balance,
                "usdc_e_balance": balances["usdc_e"],
                "usdc_native_balance": balances["usdc_native"],
                "funder_address": funder,
            },
            status_code=402,
        )

    return {
        "user_wallet": user_wallet,
        "funder": funder,
        "wallet_type": wallet_type,
        "is_deposit_wallet": is_deposit_wallet,
        "token_id": token_id,
        "side_raw": side_raw,
        "order_type_raw": order_type_raw,
        "price": price,
        "size": size,
        "notional": notional,
        "body": body,
    }, None


@router.post("/api/pm/trade/prepare")
@router.post("/api/pm/trade/prepare-order")
async def pm_trade_prepare(request: Request):
    """T884.3: prepare order + return EIP-712 typed data for browser signing.

    Used by deposit_wallet (sig_type=3) users. EOA users still hit
    /api/pm/trade/submit directly (server-side signing works for sig_type=2).
    """
    ctx, err = await _pm_trade_validate_request(request)
    if err is not None:
        return err
    assert ctx is not None

    if not ctx["is_deposit_wallet"]:
        return JSONResponse(
            {
                "error": "wallet_type_not_deposit_wallet",
                "hint": "Browser signing path is only for deposit_wallet users; "
                        "EOA users should call /api/pm/trade/submit directly.",
                "wallet_type": ctx["wallet_type"],
            },
            status_code=400,
        )

    auth_err = _ensure_user_api_key_authorized(ctx["user_wallet"])
    if auth_err is not None:
        return auth_err

    from src.executor.clob_client_factory import get_gateway_for_funder
    from src.executor.execution_gateway import (
        ExecutionOrderRequest,
        ExecutionOrderType,
        OrderSide,
    )

    try:
        gw = get_gateway_for_funder(ctx["funder"])
    except Exception as exc:
        return JSONResponse({"error": f"gateway init failed: {exc}"}, status_code=500)

    req = ExecutionOrderRequest(
        token_id=ctx["token_id"],
        price=ctx["price"],
        size=ctx["size"],
        side=OrderSide.BUY if ctx["side_raw"] == "BUY" else OrderSide.SELL,
        order_type=ExecutionOrderType[ctx["order_type_raw"]],
        signal_id=f"ui-{ctx['user_wallet'][:10]}-{int(time.time())}",
        trace_id=f"ui-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
    )

    try:
        from src.executor.poly1271_clob_ordering import (  # noqa: PLC0415
            build_browser_signing_payload,
            get_builder_clob_client,
        )

        # CRITICAL: read directly from os.getenv — get_secret("POLY_BUILDER_CODE")
        # returns None (secrets vault doesn't hold POLY_ keys).  Zero builder_code
        # causes CLOB to fall back to old signer-vs-api-key validation and reject.
        _raw_bc = os.getenv("POLY_BUILDER_CODE", "").strip()
        if _raw_bc and not _raw_bc.startswith("0x"):
            _raw_bc = "0x" + _raw_bc
        builder_code = _raw_bc if (len(_raw_bc) == 66) else ("0x" + "0" * 64)
        metadata_hex = gw._build_order_metadata(req)

        # Per Deposit Wallet Migration Guide: POLY_ADDRESS must be user's EOA
        # (owner of deposit_wallet), not the server EOA. Resolve privy_wallet_id
        # for user's EOA so we can derive their CLOB API credentials.
        _user_eoa = str(ctx["user_wallet"]).strip().lower()
        _privy_wallet_id = ""
        try:
            from scripts.dashboard_routes.redeem_withdraw_routes import (  # noqa: PLC0415
                _get_user_signer_binding,
            )
            _binding = _get_user_signer_binding(_user_eoa, require_wallets=False)
            _privy_wallet_id = str(_binding.get("privy_embedded_wallet_id") or "").strip()
        except Exception as _binding_exc:
            logger.warning(
                "[FundFlow][poly1271][prepare] cannot resolve privy_wallet_id for %s: %s",
                _user_eoa[:10], _binding_exc,
            )

        try:
            client = get_builder_clob_client(
                ctx["funder"],
                user_eoa=_user_eoa,
                privy_wallet_id=_privy_wallet_id,
            )
            neg_risk = bool(client.get_neg_risk(ctx["token_id"]))
        except Exception:
            neg_risk = False

        # Per Deposit Wallet spec: maker = signer = deposit_wallet_address
        signer_eoa = ctx["funder"]  # deposit_wallet address

        logger.info(
            "[FundFlow][poly1271][prepare] builder_code=%s signer_eoa(deposit_wallet)=%s user_eoa=%s privy_wid=%s",
            builder_code[:14],
            signer_eoa[:10],
            _user_eoa[:10],
            _privy_wallet_id[:12] if _privy_wallet_id else "MISSING",
        )

        payload = build_browser_signing_payload(
            request=req,
            maker_deposit_wallet=ctx["funder"],
            signer_eoa=signer_eoa,
            builder_code=builder_code,
            metadata_hex=metadata_hex,
            neg_risk=neg_risk,
            chain_id=137,
        )
    except Exception as exc:
        logger.exception("[FundFlow][poly1271][prepare] failed: %s", exc)
        return JSONResponse(
            {
                "status": "error",
                "error": _friendly_clob_error(str(exc)),
                "raw_error": str(exc),
            },
            status_code=400,
        )

    import secrets as _secrets  # noqa: PLC0415

    prepare_token = _secrets.token_urlsafe(24)
    now_ts = time.time()
    _pm_trade_prepare_gc(now_ts)
    _pm_trade_prepare_write(prepare_token, {
        "_ts": now_ts,
        "user_wallet": ctx["user_wallet"],
        "funder": ctx["funder"],
        "token_id": ctx["token_id"],
        "side_raw": ctx["side_raw"],
        "price": ctx["price"],
        "size": ctx["size"],
        "order_type_raw": ctx["order_type_raw"],
        "notional": ctx["notional"],
        "order": payload["order"],
        "app_domain_separator": payload["app_domain_separator"],
        "contents_hash": payload["contents_hash"],
        "contents_type": payload["contents_type"],
        "verifying_contract": payload["verifying_contract"],
        "deposit_wallet": payload["deposit_wallet"],
        "signer_eoa": payload["signer_eoa"],
        "neg_risk": payload["neg_risk"],
        "user_eoa": _user_eoa,
        "privy_wallet_id": _privy_wallet_id,
        "event_slug": str(ctx["body"].get("event_slug") or ""),
        "market_question": str(ctx["body"].get("market_question") or ""),
    })

    logger.info(
        "[FundFlow][poly1271][prepare] user=%s token=%s side=%s price=%.4f "
        "size=%.2f neg_risk=%s tok=%s maker=%s signer=%s sig_type=%s "
        "td.domain.vc=%s td.msg.vc=%s",
        ctx["user_wallet"][:10],
        ctx["token_id"][:12],
        ctx["side_raw"],
        ctx["price"],
        ctx["size"],
        payload["neg_risk"],
        prepare_token[:8],
        str(payload.get("order", {}).get("maker") or "")[:10],
        str(payload.get("order", {}).get("signer") or "")[:10],
        str(payload.get("order", {}).get("signatureType") or ""),
        str(payload.get("typed_data", {}).get("domain", {}).get("verifyingContract") or "")[:10],
        str(payload.get("typed_data", {}).get("message", {}).get("verifyingContract") or "")[:10],
    )

    return {
        "status": "signing_required",
        "prepare_token": prepare_token,
        "expires_in": _PM_TRADE_PREPARE_TTL_SECS,
        "typed_data": payload["typed_data"],
        "signer_eoa": payload["signer_eoa"],
        "owner_eoa": _user_eoa,
        "deposit_wallet": payload["deposit_wallet"],
        "verifying_contract": payload["verifying_contract"],
        "neg_risk": payload["neg_risk"],
    }


@router.post("/api/pm/trade/submit-signed")
async def pm_trade_submit_signed(request: Request):
    """T884.3: complete a deposit_wallet order using browser-side signature."""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "")

    auth_err = _ensure_user_api_key_authorized(user_wallet)
    if auth_err is not None:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    prepare_token = str(body.get("prepare_token") or "").strip()
    signature_hex = str(body.get("signature") or "").strip()
    if not prepare_token or not signature_hex:
        return JSONResponse(
            {"error": "prepare_token and signature are required"},
            status_code=400,
        )

    now_ts = time.time()
    _pm_trade_prepare_gc(now_ts)
    cached = _pm_trade_prepare_pop(prepare_token)
    if not cached:
        return JSONResponse(
            {
                "error": "prepare_token_expired_or_unknown",
                "hint": "Re-open the bet panel — your prepare session has expired.",
            },
            status_code=410,
        )
    if cached.get("user_wallet") != user_wallet:
        # Defensive: cache key collisions across users.
        return JSONResponse(
            {"error": "prepare_token_user_mismatch"},
            status_code=403,
        )

    funder = cached["funder"]
    token_id = cached["token_id"]
    side_raw = cached["side_raw"]
    notional = float(cached.get("notional") or 0.0)

    from src.executor.clob_client_factory import get_gateway_for_funder
    from src.executor.poly1271_clob_ordering import (
        assemble_signed_envelope,
        get_builder_clob_client,
        submit_signed_order_envelope,
    )

    try:
        _ = get_gateway_for_funder(funder)
        _cached_user_eoa = str(cached.get("user_eoa") or cached.get("user_wallet") or "").strip().lower()
        _cached_privy_wid = str(cached.get("privy_wallet_id") or "").strip()
        # T885 fix (2026-05-07): prefer per-user CLOB creds derived via browser
        # ClobAuth signature. Falls back to Privy server-side derivation only if
        # creds are missing (which Privy policy_violation will likely block).
        _user_clob_creds = _get_user_clob_creds(user_wallet)
        client = get_builder_clob_client(
            funder,
            user_eoa=_cached_user_eoa,
            privy_wallet_id=_cached_privy_wid,
            user_creds=_user_clob_creds,
        )
    except Exception as exc:
        return JSONResponse({"error": f"gateway init failed: {exc}"}, status_code=500)

    try:
        full_sig = assemble_signed_envelope(
            signature_hex=signature_hex,
            app_domain_separator_hex=cached["app_domain_separator"],
            contents_hash_hex=cached["contents_hash"],
            contents_type=cached["contents_type"],
        )
    except Exception as exc:
        return JSONResponse(
            {
                "status": "error",
                "error": "envelope_assembly_failed",
                "raw_error": str(exc),
            },
            status_code=400,
        )

    logger.info(
        "[FundFlow][poly1271][submit-signed] user=%s funder=%s token=%s side=%s tok=%s sig_len=%d raw_sig_len=%d",
        user_wallet[:10],
        funder[:10],
        token_id[:12],
        side_raw,
        prepare_token[:8],
        len(full_sig),
        len(signature_hex),
    )

    forced_order = dict(cached.get("order") or {})
    # Per Deposit Wallet + POLY_1271 spec: maker == signer == deposit_wallet_address
    _deposit_wallet = str(cached.get("deposit_wallet") or funder).strip().lower()
    forced_order["maker"] = _deposit_wallet
    forced_order["signer"] = _deposit_wallet  # MUST equal maker (deposit_wallet)
    forced_order["signatureType"] = 3

    # Safety net: ensure builder field carries the real POLY_BUILDER_CODE.
    _env_bc = os.getenv("POLY_BUILDER_CODE", "").strip()
    if _env_bc and not _env_bc.startswith("0x"):
        _env_bc = "0x" + _env_bc
    if _env_bc and len(_env_bc) == 66:
        forced_order["builder"] = _env_bc

    logger.info(
        "[FundFlow][poly1271][submit-signed] FULL_ORDER_PAYLOAD maker=%s signer=%s "
        "sig_type=%s token=%s side=%s builder=%s makerAmount=%s takerAmount=%s "
        "salt=%s expiration=%s",
        str(forced_order.get("maker") or ""),
        str(forced_order.get("signer") or ""),
        str(forced_order.get("signatureType") or ""),
        str(forced_order.get("tokenId") or ""),
        str(forced_order.get("side") or ""),
        str(forced_order.get("builder") or ""),
        str(forced_order.get("makerAmount") or ""),
        str(forced_order.get("takerAmount") or ""),
        str(forced_order.get("salt") or "")[:12],
        str(forced_order.get("expiration") or ""),
    )

    result = submit_signed_order_envelope(
        client=client,
        order_dict=forced_order,
        full_signature_hex=full_sig,
        order_type_str=cached["order_type_raw"],
    )

    now = datetime.now(timezone.utc).isoformat()
    _ensure_manual_order_table()
    _audit_payload = (
        user_wallet,
        funder,
        token_id,
        side_raw,
        cached["price"],
        cached["size"],
        cached["order_type_raw"],
        result.order_id,
        result.status or ("success" if result.success else "error"),
        0.0,
        result.error or "",
        json.dumps(result.raw_response or {}, ensure_ascii=False),
        now,
    )

    def _audit_write_async(payload):
        try:
            with db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_manual_orders(
                        user_wallet, polymarket_wallet, token_id, side, price, size,
                        order_type, order_id, status, builder_fee_usdc, error, raw_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                conn.commit()
        except Exception as exc:
            logger.exception("write user_manual_orders (async) failed: %s", exc)

    try:
        import threading as _t
        _t.Thread(
            target=_audit_write_async,
            args=(_audit_payload,),
            daemon=True,
            name="manual_order_audit",
        ).start()
    except Exception:
        _audit_write_async(_audit_payload)

    if not result.success:
        err_msg = (result.error or "order failed").lower()
        clob_4xx_signals = (
            "invalid amount", "invalid price", "invalid size", "tick size",
            "min order", "minimum", "max order", "maximum",
            "insufficient", "not allowed", "forbidden", "unauthor",
            "size_too_small", "precision",
        )
        if any(sig in err_msg for sig in clob_4xx_signals):
            status_code = 422
        elif "timeout" in err_msg or "connection" in err_msg or "network" in err_msg:
            status_code = 504
        else:
            status_code = 400
        return JSONResponse(
            {
                "status": "error",
                "error": _friendly_clob_error(result.error or "order failed"),
                "raw_error": result.error or "order failed",
                "order_id": result.order_id,
                "raw": result.raw_response,
            },
            status_code=status_code,
        )

    return {
        "status": "ok",
        "user_wallet": user_wallet,
        "funder_address": funder,
        "order_id": result.order_id,
        "order_status": result.status,
        "filled_size": result.filled_size,
        "avg_price": result.avg_price,
        "notional": notional,
        "raw": result.raw_response,
        "settlement": _t491a_settle(
            user_wallet=user_wallet,
            result=result,
            token_id=token_id,
            side=side_raw,
            requested_notional=notional,
        ),
    }


@router.post("/api/pm/trade/submit")
async def pm_trade_submit(request: Request):
    """T289 Wave 5+6: 手动提交订单（走 ClobClientFactory per-user funder）。

    硬性前置校验：
      1. 用户已登录（manual_user）
      2. 系统维护模式/紧急冻结未开启
      3. 用户自身未被冻结 / auto_trading_enabled=true
      4. funder_address 已绑定
      5. funder USDC 余额 ≥ trading.min_usdc_to_trade
      6. size × price ≤ trading.max_order_size_usdc
      7. token_id / price / size 格式合法
    """
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "")
    blocked, reason = _runtime_global_block()
    if blocked:
        return JSONResponse(
            {
                "error": reason,
                "maintenance_mode": bool(get("system.maintenance_mode")),
                "emergency_freeze": bool(get("risk.emergency_freeze")),
            },
            status_code=403,
        )

    runtime = _get_user_runtime(user_wallet)
    if runtime.get("freeze_auto") or not runtime.get("auto_trading_enabled"):
        return JSONResponse(
            {"error": "user trading is frozen or disabled", "runtime": runtime},
            status_code=403,
        )

    funder = _get_user_funder(user_wallet)
    if not funder:
        return JSONResponse(
            {"error": "funder_address not bound; complete /go wallet binding first"},
            status_code=412,
        )

    # T884 (2026-05-07) — emergency full cutover. The 501 staging guard is
    # removed; deposit_wallet users always dispatch through the POLY_1271
    # (signature_type=3) ordering path. Errors are caught downstream and
    # returned as friendly JSON (never bubble up as 500/503).
    _wallet_type = _get_user_wallet_type(user_wallet)
    _is_deposit_wallet = _wallet_type == "deposit_wallet"

    # T884.3 (2026-05-08) — deposit_wallet users MUST go through the
    # prepare → browser-sign → submit-signed path. Privy server-signer policy
    # blocks both eth_signTypedData_v4 and secp256k1_sign for embedded wallets,
    # so server-side signing of POLY_1271 orders is impossible. Reject early
    # with a 409 telling the caller to use the new flow.
    if _is_deposit_wallet:
        auth_err = _ensure_user_api_key_authorized(user_wallet)
        if auth_err is not None:
            return auth_err
        return JSONResponse(
            {
                "status": "client_signing_required",
                "error": "deposit_wallet_requires_browser_signing",
                "next": {
                    "prepare": "/api/pm/trade/prepare-order",
                    "submit_signed": "/api/pm/trade/submit-signed",
                },
                "hint": (
                    "deposit_wallet (sig_type=3) orders must be signed by your "
                    "Privy embedded wallet in the browser. Call /api/pm/trade/prepare "
                    "first, then sign the returned typed_data and POST to "
                    "/api/pm/trade/submit-signed."
                ),
                "wallet_type": _wallet_type,
            },
            status_code=409,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    token_id = str(body.get("token_id") or "").strip()
    side_raw = str(body.get("side") or "BUY").upper().strip()
    order_type_raw = str(body.get("order_type") or "FOK").upper().strip()
    try:
        price = float(body.get("price"))
        size = float(body.get("size"))
    except Exception:
        return JSONResponse({"error": "price/size must be numeric"}, status_code=400)

    if not token_id or not token_id.isdigit():
        return JSONResponse(
            {"error": "token_id required (numeric string)"}, status_code=400
        )
    if side_raw not in {"BUY", "SELL"}:
        return JSONResponse({"error": "side must be BUY or SELL"}, status_code=400)
    if order_type_raw not in {"GTC", "FOK", "FAK"}:
        order_type_raw = "FOK"
    if not (0.001 <= price <= 0.999):
        return JSONResponse(
            {"error": "price must be between 0.001 and 0.999"}, status_code=400
        )
    if size <= 0:
        return JSONResponse({"error": "size must be > 0"}, status_code=400)

    # Dynamic min_order_size guard (never hardcode 5).
    min_order_size = 1
    try:
        ob_resp = _pm_session().get(
            f"{_CLOB_PUBLIC}/book", params={"token_id": token_id}, timeout=8
        )
        if ob_resp.ok:
            ob = ob_resp.json() or {}
            min_order_size = max(
                1, int(math.ceil(float(ob.get("min_order_size") or 1)))
            )
    except Exception:
        min_order_size = 1

    if side_raw == "BUY":
        # Taker fee safety buffer: +1 share to reduce dust/min-size sell lock risk.
        min_required = min_order_size + 1
        size = float(math.ceil(size))
        if size < min_required:
            return JSONResponse(
                {
                    "error": "size_below_min_order_size",
                    "min_order_size": min_order_size,
                    "min_required_with_buffer": min_required,
                    "hint": "buy size must be integer and >= min_order_size + 1",
                },
                status_code=400,
            )

    notional = price * size
    max_notional = _safe_float(get("trading.max_order_size_usdc") or 500.0, 500.0)
    if notional > max_notional:
        return JSONResponse(
            {
                "error": f"order size ${notional:.2f} exceeds max ${max_notional:.2f}",
                "notional": notional,
                "max": max_notional,
            },
            status_code=400,
        )

    # 下单前强制余额校验（SUPREME 2）
    balances = _fetch_usdc_balances(funder)
    balance = balances["usdc_total"]
    min_trade = _safe_float(get("trading.min_usdc_to_trade") or 1.0, 1.0)
    if balance is None:
        return JSONResponse(
            {"error": "failed to read on-chain USDC balances; retry later"},
            status_code=503,
        )
    if balance < min_trade:
        return JSONResponse(
            {
                "error": "usdc_balance_below_minimum",
                "usdc_balance": balance,
                "usdc_e_balance": balances["usdc_e"],
                "usdc_native_balance": balances["usdc_native"],
                "min_usdc_to_trade": min_trade,
                "funder_address": funder,
                "hint": "请先在 /v2/deposit 页面充值原生 USDC（Polygon）到上方资金钱包",
            },
            status_code=402,
        )
    if side_raw == "BUY" and balance < notional:
        return JSONResponse(
            {
                "error": "insufficient_usdc",
                "required": notional,
                "available": balance,
                "usdc_e_balance": balances["usdc_e"],
                "usdc_native_balance": balances["usdc_native"],
                "funder_address": funder,
            },
            status_code=402,
        )

    # 构造订单请求并通过工厂取 per-user gateway
    from src.executor.clob_client_factory import get_gateway_for_funder
    from src.executor.execution_gateway import (
        ExecutionOrderRequest,
        ExecutionOrderType,
        OrderSide,
    )

    try:
        gw = get_gateway_for_funder(funder)
    except Exception as exc:
        return JSONResponse({"error": f"gateway init failed: {exc}"}, status_code=500)

    req = ExecutionOrderRequest(
        token_id=token_id,
        price=price,
        size=size,
        side=OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL,
        order_type=ExecutionOrderType[order_type_raw],
        signal_id=f"ui-{user_wallet[:10]}-{int(time.time())}",
        trace_id=f"ui-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
    )

    if _is_deposit_wallet:
        # T883 sig_type=3 dispatch path. Resolves Privy EOA + wallet_id from
        # the user signer binding, then signs typed data via Privy server-signer.
        try:
            from scripts.dashboard_routes.redeem_withdraw_routes import (  # noqa: PLC0415
                _get_user_signer_binding,
                _load_privy_user_authorization_keys,
                _extract_request_user_jwts,
                _extract_request_privy_token_bundle,
                _get_cached_user_jwts,
            )
            from src.executor.poly1271_clob_ordering import (  # noqa: PLC0415
                build_builder_clob_client,
                place_order_poly1271,
            )

            binding = _get_user_signer_binding(user_wallet, require_wallets=False) or {}
            privy_wallet_id = str(binding.get("privy_embedded_wallet_id") or "").strip()
            signer_eoa = str(binding.get("main_wallet") or user_wallet or "").strip().lower()
            if not privy_wallet_id or not signer_eoa:
                return JSONResponse(
                    {
                        "error": "missing_signer_binding",
                        "hint": "Privy embedded wallet not enrolled for this user",
                        "wallet_type": _wallet_type,
                    },
                    status_code=412,
                )
            try:
                # T884.1: collect user JWTs from request + cache so Privy can
                # mint user signing keys for sig_type=3 deposit_wallet flow.
                jwt_candidates = _extract_request_user_jwts(request) or []
                for cached_token in _get_cached_user_jwts(user_wallet):
                    if cached_token not in jwt_candidates:
                        jwt_candidates.append(cached_token)
                token_bundle = _extract_request_privy_token_bundle(request)
                auth_keys = _load_privy_user_authorization_keys(
                    jwt_candidates, token_bundle=token_bundle
                )
            except Exception as exc:
                logger.warning("[FundFlow][poly1271] auth keys load failed: %s", exc)
                auth_keys = None

            client = build_builder_clob_client(
                funder_address=funder,
                signature_type=3,
                chain_id=137,
                host=_CLOB_PUBLIC,
            )
            builder_code = gw._resolve_builder_code(None)
            metadata_hex = gw._build_order_metadata(req)

            logger.info(
                "[FundFlow][poly1271] dispatch user=%s funder=%s token=%s "
                "side=%s price=%.4f size=%.2f",
                user_wallet[:10], funder[:10], token_id[:12],
                side_raw, price, size,
            )
            result = place_order_poly1271(
                client=client,
                request=req,
                maker_deposit_wallet=funder,
                signer_eoa=signer_eoa,
                privy_wallet_id=privy_wallet_id,
                privy_auth_keys=auth_keys,
                builder_code=builder_code,
                metadata_hex=metadata_hex,
            )
        except Exception as exc:
            logger.exception("[FundFlow][poly1271] dispatch error: %s", exc)
            from src.executor.execution_gateway import ExecutionOrderResult  # noqa: PLC0415
            result = ExecutionOrderResult(success=False, error=f"poly1271_dispatch_error: {exc}")
    else:
        result = gw.place_order(req)
    now = datetime.now(timezone.utc).isoformat()

    # T882-B perf: audit insert was previously synchronous on the request hot
    # path (~30-80ms PG round-trip). Move to a fire-and-forget background
    # thread so the user gets the order result immediately. Failures are
    # logged but do not block the response.
    _ensure_manual_order_table()
    _audit_payload = (
        user_wallet,
        funder,
        token_id,
        side_raw,
        price,
        size,
        order_type_raw,
        result.order_id,
        result.status or ("success" if result.success else "error"),
        0.0,
        result.error or "",
        json.dumps(result.raw_response or {}, ensure_ascii=False),
        now,
    )

    def _audit_write_async(payload):
        try:
            with db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_manual_orders(
                        user_wallet, polymarket_wallet, token_id, side, price, size,
                        order_type, order_id, status, builder_fee_usdc, error, raw_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                conn.commit()
        except Exception as exc:
            logger.exception("write user_manual_orders (async) failed: %s", exc)

    try:
        import threading as _t
        _t.Thread(
            target=_audit_write_async,
            args=(_audit_payload,),
            daemon=True,
            name="manual_order_audit",
        ).start()
    except Exception:
        # If thread spawn fails for any reason, fall back to synchronous insert.
        _audit_write_async(_audit_payload)

    if not result.success:
        err_msg = (result.error or "order failed").lower()
        # T299 — avoid raw 502 (CF replaces with its own error page).
        # Distinguish CLOB business 4xx from true upstream/network failures.
        clob_4xx_signals = (
            "invalid amount",
            "invalid price",
            "invalid size",
            "tick size",
            "min order",
            "minimum",
            "max order",
            "maximum",
            "insufficient",
            "not allowed",
            "forbidden",
            "unauthor",
            "size_too_small",
            "precision",
        )
        if any(sig in err_msg for sig in clob_4xx_signals):
            status_code = 422
        elif "timeout" in err_msg or "connection" in err_msg or "network" in err_msg:
            status_code = 504
        else:
            status_code = 400  # default to user-actionable, not 502
        return JSONResponse(
            {
                "status": "error",
                "error": _friendly_clob_error(result.error or "order failed"),
                "raw_error": result.error or "order failed",
                "order_id": result.order_id,
                "raw": result.raw_response,
            },
            status_code=status_code,
        )

    return {
        "status": "ok",
        "user_wallet": user_wallet,
        "funder_address": funder,
        "order_id": result.order_id,
        "order_status": result.status,
        "filled_size": result.filled_size,
        "avg_price": result.avg_price,
        "notional": notional,
        "raw": result.raw_response,
        "settlement": _t491a_settle(
            user_wallet=user_wallet,
            result=result,
            token_id=token_id,
            side=side_raw,
            requested_notional=notional,
        ),
    }


def _t491a_settle(
    *, user_wallet: str, result, token_id: str, side: str, requested_notional: float
) -> dict:
    """T491A: 在订单提交链路尾端按真实成交额发放积分 + 计费率，幂等。

    优先采用真实 filled_size × avg_price 估算成交额；若 avg_price 缺失退化用挂单 price。
    若 order_id 缺失，则使用 signal_id 等作为 order_ref（仍由 UNIQUE 约束兜底）。
    """
    try:
        from .points_routes import (
            settle_trade_award_points,
        )  # local import to avoid cycle

        filled_size = float(getattr(result, "filled_size", 0) or 0)
        avg_price = float(getattr(result, "avg_price", 0) or 0)
        if filled_size > 0 and avg_price > 0:
            notional = round(filled_size * avg_price, 6)
        else:
            notional = round(float(requested_notional or 0.0), 6)
        order_ref = (getattr(result, "order_id", "") or "").strip()
        if not order_ref:
            order_ref = f"manual:{user_wallet[:12]}:{token_id}:{int(time.time()*1000)}"
        return settle_trade_award_points(
            user_wallet,
            order_ref,
            notional_usd=notional,
            token_id=token_id,
            side=side,
            raw=getattr(result, "raw_response", None) or {},
        )
    except Exception as exc:
        logger.warning("[T491A] settle_trade_award_points failed: %s", exc)
        return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# 用户订单历史（供 /v2/trades 聚合）
# ---------------------------------------------------------------------------


class _RecordOrderBody(BaseModel):
    token_id: str
    side: str
    price: float
    size: float
    order_type: str = "GTC"
    order_id: str | None = ""
    status: str | None = "submitted"
    error: str | None = ""
    raw: dict | None = None
    condition_id: str | None = ""
    market_slug: str | None = ""
    event_slug: str | None = ""
    market_question: str | None = ""


@router.post("/api/pm/order/record")
async def pm_order_record(request: Request, body: _RecordOrderBody):
    """T912 (2026-05-11): persist a browser-SDK-signed order into the same
    user_manual_orders ledger that the legacy server-side path writes to.
    Without this, deposit-wallet users see balance decrease after a successful
    CLOB fill but no row in /v2/trades or /v2 dashboard until PM Data API
    indexes the on-chain match (which can take minutes).

    The browser SDK path (deposit_wallet, sig_type=3) signs and submits to
    CLOB directly; this endpoint is what records the result on our side. It
    also invalidates the per-user PM metrics cache so the dashboard refreshes
    promptly, and best-effort kicks the PM Data API sync so positions page
    reflects the new fill.
    """
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "").lower()
    if not user_wallet:
        return JSONResponse({"error": "no wallet"}, status_code=403)

    # Funder = polymarket_wallet (deposit wallet) for sig_type=3 users.
    funder = ""
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT polymarket_wallet, funder_address FROM users WHERE wallet_address=?",
                (user_wallet,),
            ).fetchone()
            if row:
                d = dict(row)
                funder = (
                    str(d.get("funder_address") or d.get("polymarket_wallet") or "")
                    .strip()
                    .lower()
                )
    except Exception as exc:
        logger.warning("[T912/record] funder lookup failed user=%s err=%s", user_wallet, exc)

    side_raw = (body.side or "BUY").upper()
    order_type_raw = (body.order_type or "GTC").upper()
    status_raw = (body.status or "submitted").lower()
    order_id = (body.order_id or "").strip()
    raw_json = json.dumps(body.raw or {}, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat()

    _ensure_manual_order_table()

    def _write():
        try:
            with db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_manual_orders(
                        user_wallet, polymarket_wallet, token_id, side, price, size,
                        order_type, order_id, status, builder_fee_usdc, error, raw_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_wallet,
                        funder,
                        body.token_id,
                        side_raw,
                        float(body.price),
                        float(body.size),
                        order_type_raw,
                        order_id,
                        status_raw,
                        0.0,
                        body.error or "",
                        raw_json,
                        now,
                    ),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.exception("[T912/record] insert user_manual_orders failed: %s", exc)
            return False

    ok = await asyncio.to_thread(_write)
    if not ok:
        return JSONResponse({"status": "error", "error": "db_write_failed"}, status_code=500)

    # Invalidate per-user metrics cache so /v2 dashboard refreshes immediately.
    try:
        from .user_routes import _PM_METRICS_CACHE  # type: ignore

        for k in list(_PM_METRICS_CACHE.keys()):
            if user_wallet in k:
                _PM_METRICS_CACHE.pop(k, None)
    except Exception:
        pass

    # Best-effort PM Data API sync (force=True) to pick up the new position.
    # This is async via thread; never blocks the response.
    def _kick_sync():
        try:
            from .user_routes import _sync_user_pm_data  # type: ignore

            _sync_user_pm_data(
                user_wallet,
                linked_wallets=[],
                force=True,
            )
        except Exception as exc:
            logger.info("[T912/record] post-trade pm sync skipped: %s", exc)

    try:
        import threading as _t

        _t.Thread(target=_kick_sync, daemon=True, name="t912_post_trade_sync").start()
    except Exception:
        pass

    logger.info(
        "[T912/record] user=%s funder=%s token=%s side=%s price=%s size=%s order_id=%s status=%s",
        user_wallet,
        funder,
        body.token_id,
        side_raw,
        body.price,
        body.size,
        order_id,
        status_raw,
    )

    return {"status": "ok", "order_id": order_id, "recorded_at": now}


@router.get("/api/pm/my-orders")
async def pm_my_orders(request: Request, limit: int = 50):
    """T289 Wave 5: 返回当前用户 user_manual_orders 最近 N 条。"""
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "manual user required"}, status_code=403)

    user_wallet = str(identity.get("wallet_address") or "")
    limit = max(1, min(int(limit or 50), 200))

    _ensure_manual_order_table()

    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                """
            SELECT id, token_id, side, price, size, order_type, order_id,
                   status, error, created_at
            FROM user_manual_orders
            WHERE user_wallet=?
            ORDER BY id DESC LIMIT ?
            """,
                (user_wallet, limit),
            ).fetchall()

        orders = []
        for r in rows or []:
            d = dict(r)
            orders.append(
                {
                    "id": d.get("id"),
                    "token_id": d.get("token_id"),
                    "side": d.get("side"),
                    "price": d.get("price"),
                    "size": d.get("size"),
                    "order_type": d.get("order_type"),
                    "order_id": d.get("order_id"),
                    "status": d.get("status"),
                    "error": d.get("error"),
                    "created_at": d.get("created_at"),
                }
            )

        return {"status": "ok", "count": len(orders), "orders": orders}

    return await asyncio.to_thread(_db)


# ===========================================================================
# T907 — Polymarket Builder HMAC signing endpoint
# ---------------------------------------------------------------------------
# Per the Polymarket-Privy reference architecture:
#   https://docs.privy.io/recipes/polymarket-guide
#   https://github.com/Polymarket/privy-safe-builder-example
#   https://docs.polymarket.com/builders/overview
#   https://docs.polymarket.com/trading/deposit-wallets
#
# All wallet deployment, batch approval, API-key derivation, and order
# placement now run inside the user's browser via the official TS SDKs:
#   - @polymarket/builder-relayer-client
#   - @polymarket/clob-client-v2
#   - @polymarket/builder-signing-sdk
# Privy's embedded wallet (the user's EOA) provides the signer used by both
# SDKs.  The ONLY server-side responsibility is to expose the BUILDER credentials
# safely via this single HMAC signing endpoint, which mirrors
# `app/api/polymarket/sign/route.ts` from the official Next.js reference.
#
# DO NOT expand this endpoint into doing typed-data construction, order
# orchestration, or relayer submission. The official SDKs handle ALL of that
# in the browser. Server-side reconstruction is what landed us in T906 hell.
# ===========================================================================

@router.post("/api/polymarket/sign")
async def polymarket_builder_sign(request: Request):
    """T907: HMAC-sign a Polymarket Builder request on behalf of a logged-in user.

    Mirrors the reference implementation at
    https://github.com/Polymarket/privy-safe-builder-example/blob/main/app/api/polymarket/sign/route.ts

    Request:
        {"method": "POST", "path": "/submit", "body": {...}, "timestamp": <ms>}

    Response:
        {"POLY_BUILDER_SIGNATURE": "...", "POLY_BUILDER_TIMESTAMP": "...",
         "POLY_BUILDER_API_KEY": "...", "POLY_BUILDER_PASSPHRASE": "..."}

    Auth: requires a valid user session (any logged-in user; the credentials
    returned authenticate Polymarket builder routing, not the user). We deny
    the request to anonymous callers so the HMAC pair is not freely scrapable.

    Note: ``timestamp`` from the SDK is expected in milliseconds. The Python
    ``build_hmac_signature`` does ``str(timestamp)`` so we pass through as-is.
    """
    identity = get_request_identity(request)
    if not identity:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    method = str(payload.get("method") or "").upper().strip()
    path = str(payload.get("path") or "").strip()
    if method not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
        return JSONResponse({"error": "invalid method"}, status_code=400)
    if not path.startswith("/"):
        return JSONResponse({"error": "invalid path"}, status_code=400)

    body = payload.get("body")  # may be None for GET / nonce queries
    incoming_ts = payload.get("timestamp")

    api_key = (
        os.environ.get("POLY_BUILDER_API_KEY")
        or os.environ.get("BUILDER_RELAYER_API_KEY")
        or ""
    ).strip()
    secret = (
        os.environ.get("POLY_BUILDER_SECRET")
        or os.environ.get("BUILDER_RELAYER_API_SECRET")
        or ""
    ).strip()
    passphrase = (
        os.environ.get("POLY_BUILDER_PASSPHRASE")
        or os.environ.get("BUILDER_RELAYER_API_PASSPHRASE")
        or ""
    ).strip()

    if not (api_key and secret and passphrase):
        logger.error(
            "[T907 sign] builder credentials missing api_key=%s secret=%s passphrase=%s",
            bool(api_key), bool(secret), bool(passphrase),
        )
        return JSONResponse(
            {"error": "builder credentials not configured"}, status_code=503
        )

    try:
        from py_builder_signing_sdk.signing.hmac import (  # type: ignore  # noqa: PLC0415
            build_hmac_signature,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[T907 sign] py_builder_signing_sdk import failed: %s", exc)
        return JSONResponse(
            {"error": "signing sdk unavailable"}, status_code=503
        )

    import time as _t  # noqa: PLC0415

    timestamp_ms = (
        int(incoming_ts)
        if isinstance(incoming_ts, (int, float)) or (isinstance(incoming_ts, str) and incoming_ts.isdigit())
        else int(_t.time() * 1000)
    )

    # Per py-builder-signing-sdk reference behaviour: the body field included
    # in the HMAC base string must be the JSON-serialised body (or empty
    # string), with single quotes replaced by double quotes (handled inside
    # build_hmac_signature). The TS SDK serialises with JSON.stringify and we
    # match that here using json.dumps(separators=(",", ":")). NOTE: the JS
    # SDK on the client always sends ``body`` already as the raw JSON string
    # (see RelayClient._post_request / ClobClient internals). To be robust we
    # accept both dict and string.
    body_for_hmac: "str | None"
    if body is None:
        body_for_hmac = None
    elif isinstance(body, str):
        body_for_hmac = body
    else:
        body_for_hmac = json.dumps(body, separators=(",", ":"), sort_keys=False)

    try:
        signature = build_hmac_signature(
            secret, str(timestamp_ms), method, path, body_for_hmac
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[T907 sign] HMAC computation failed: %s", exc)
        return JSONResponse(
            {"error": "signature computation failed"}, status_code=500
        )

    return {
        "POLY_BUILDER_SIGNATURE": signature,
        "POLY_BUILDER_TIMESTAMP": str(timestamp_ms),
        "POLY_BUILDER_API_KEY": api_key,
        "POLY_BUILDER_PASSPHRASE": passphrase,
    }

