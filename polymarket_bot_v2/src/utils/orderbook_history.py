"""
orderbook_history.py — Polymarket CLOB 订单簿 & 价格历史工具模块

基于官方文档: https://docs.polymarket.com/api-reference/market-data/get-order-book

官方 CLOB API 端点（无需认证，公开访问）:
  GET /book?token_id=<TOKEN_ID>
        — 当前订单簿快照（bids/asks 列表 + last_trade_price）
  GET /prices-history?market=<TOKEN_ID>&interval=<1h|6h|1d|1w|max>&fidelity=<min>
        — 历史价格走势（[{t: epoch_s, p: float}, ...]）
  GET /spread?token_id=<TOKEN_ID>
        — 当前买卖价差

注: orderbook-history 端点**不存在**于官方 CLOB API，调用返回 404。

【404 行为说明】
  当一个 token 的市场已结算/已关闭时，CLOB 会返回 HTTP 404 Not Found。
  这是 Polymarket 平台的预期行为，不是系统错误：
  - 已结算的 YES/NO token 订单簿会被下线
  - 临近结算时 token 价格通常会趋近 0（输掉方）或 1（赢方）
  - 此时 fetch_orderbook/fetch_prices_history 均可能返回 404
  - 所有调用方对 None 返回值已有 graceful 降级处理
  404 场景记录为 DEBUG（非 WARNING），并通过 tombstone 缓存
  在 5 分钟内屏蔽重复请求，避免无效 API 调用。
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
DEFAULT_TIMEOUT = 15

# 简单内存缓存 {key: (timestamp, data)}
_CACHE: dict[str, tuple[float, object]] = {}
_BOOK_CACHE_TTL = 30  # 订单簿 30 秒（盘口变化较快）
_HISTORY_CACHE_TTL = 300  # 价格历史默认 5 分钟

# T510 P2-2: prices-history 二级 LRU — 按 interval 区分 TTL，取 interval 周期的 1/4 作为缓存寿命。
_HISTORY_TTL_BY_INTERVAL: dict[str, int] = {
    "1h": 60,        # 1h 点 → 1 分钟 TTL
    "6h": 5 * 60,    # 6h → 5 分钟
    "1d": 15 * 60,   # 1d → 15 分钟
    "1w": 30 * 60,   # 1w → 30 分钟
    "max": 60 * 60,  # max → 60 分钟
}
_HISTORY_CACHE_MAX = 512  # 软上限，防止点多起火后无限增长

# Tombstone 缓存：已知 404 的 token，5 分钟内不再请求
# {token_id: dead_ts}  — 防止对已结算市场反复发起无效 API 请求
_TOMBSTONE_CACHE: dict[str, float] = {}
_TOMBSTONE_TTL = 300  # 5 分钟


# ---------------------------------------------------------------------------
# 核心 API 函数
# ---------------------------------------------------------------------------


def fetch_orderbook(token_id: str) -> Optional[dict]:
    """
    获取指定 token 的当前订单簿快照。

    官方端点: GET https://clob.polymarket.com/book?token_id=<TOKEN_ID>

    Args:
        token_id: Polymarket token ID（YES 或 NO token 的 asset ID，纯数字）

    Returns:
        {
            "market": str,                              # condition ID (0x...)
            "asset_id": str,                            # token ID
            "timestamp": str,                           # epoch ms
            "bids": [{"price": str, "size": str}, ...], # 降序
            "asks": [{"price": str, "size": str}, ...], # 升序
            "min_order_size": str,
            "tick_size": str,
            "neg_risk": bool,
            "last_trade_price": str,
        }
        None on failure.
    """
    if not token_id or not str(token_id).strip().lstrip("-").isdigit():
        logger.debug("fetch_orderbook: invalid token_id=%r", token_id)
        return None

    cache_key = f"book:{token_id}"
    now = time.time()

    # Tombstone 检查：已知 404 的 token，5 分钟内跳过请求
    dead_ts = _TOMBSTONE_CACHE.get(token_id)
    if dead_ts is not None and now - dead_ts < _TOMBSTONE_TTL:
        logger.debug(
            "fetch_orderbook: token %s 市场已关闭（tombstone）", str(token_id)[:16]
        )
        return None

    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] < _BOOK_CACHE_TTL:
        return cached[1]  # type: ignore[return-value]

    try:
        resp = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or "bids" not in data:
            logger.warning("fetch_orderbook: unexpected format: %s", type(data))
            return None
        _CACHE[cache_key] = (now, data)
        return data
    except requests.HTTPError as http_err:
        if http_err.response is not None and http_err.response.status_code == 404:
            # 市场已结算/已关闭：CLOB 订单簿被下线，属于预期行为
            _TOMBSTONE_CACHE[token_id] = now
            logger.debug(
                "fetch_orderbook: 市场已关闭/结算 %s (HTTP 404，预期行为)",
                str(token_id)[:16],
            )
        else:
            logger.warning(
                "fetch_orderbook failed for %s: %s", str(token_id)[:16], http_err
            )
        return None
    except requests.RequestException as e:
        logger.warning("fetch_orderbook failed for %s: %s", str(token_id)[:16], e)
        return None


def fetch_prices_history(
    token_id: str,
    interval: str = "1d",
    fidelity: int = 60,
) -> list[dict]:
    """
    获取 token 的历史价格走势。

    官方端点: GET https://clob.polymarket.com/prices-history
                  ?market=<TOKEN_ID>&interval=<interval>&fidelity=<fidelity>

    Args:
        token_id: Polymarket token ID
        interval: 时间范围 "1h" | "6h" | "1d" | "1w" | "max"
        fidelity: 每个数据点的时间间隔（分钟），如 60 = 1 小时/点

    Returns:
        [{"t": epoch_seconds, "p": float}, ...]  时间升序
    """
    if not token_id:
        return []

    valid_intervals = {"1h", "6h", "1d", "1w", "max"}
    if interval not in valid_intervals:
        logger.warning("fetch_prices_history: invalid interval=%r", interval)
        interval = "1d"

    # Tombstone 检查：已知 404 的 token，5 分钟内跳过请求
    now = time.time()
    dead_ts = _TOMBSTONE_CACHE.get(token_id)
    if dead_ts is not None and now - dead_ts < _TOMBSTONE_TTL:
        logger.debug(
            "fetch_prices_history: token %s 市场已关闭（tombstone）", str(token_id)[:16]
        )
        return []

    cache_key = f"hist:{token_id}:{interval}:{fidelity}"
    cached = _CACHE.get(cache_key)
    interval_ttl = _HISTORY_TTL_BY_INTERVAL.get(interval, _HISTORY_CACHE_TTL)
    if cached and now - cached[0] < interval_ttl:
        return cached[1]  # type: ignore[return-value]

    try:
        resp = requests.get(
            f"{CLOB_BASE}/prices-history",
            params={"market": token_id, "interval": interval, "fidelity": fidelity},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        history = data.get("history", [])
        if not isinstance(history, list):
            return []
        _CACHE[cache_key] = (now, history)
        # T510 P2-2: 软上限裁剪 — 仅在超过上限时删 1/4 最旧项
        if len(_CACHE) > _HISTORY_CACHE_MAX:
            try:
                items = sorted(_CACHE.items(), key=lambda kv: kv[1][0])
                for k, _v in items[: max(1, _HISTORY_CACHE_MAX // 4)]:
                    _CACHE.pop(k, None)
            except Exception:
                pass
        return history
    except requests.HTTPError as http_err:
        if http_err.response is not None and http_err.response.status_code == 404:
            _TOMBSTONE_CACHE[token_id] = now
            logger.debug(
                "fetch_prices_history: 市场已关闭/结算 %s (HTTP 404，预期行为)",
                str(token_id)[:16],
            )
        else:
            logger.warning(
                "fetch_prices_history failed for %s: %s", str(token_id)[:16], http_err
            )
        return []
    except requests.RequestException as e:
        logger.warning("fetch_prices_history failed for %s: %s", str(token_id)[:16], e)
        return []


def get_min_order_size(token_id: str) -> Optional[float]:
    """
    从 Orderbook API 获取指定 token 的最小下单量（shares 单位）。

    利用 fetch_orderbook() 的 30 秒缓存，不会额外产生 API 调用。

    Returns:
        最小下单量（float, shares），如 5.0。
        如果 API 失败或字段缺失/异常，返回 None。
    """
    try:
        book = fetch_orderbook(token_id)
        if not book or "min_order_size" not in book:
            return None
        val = float(book["min_order_size"])
        if val <= 0 or val > 10000:
            logger.warning(
                "get_min_order_size: 异常值 %s for token %s", val, str(token_id)[:16]
            )
            return None
        return val
    except (ValueError, TypeError) as exc:
        logger.debug(
            "get_min_order_size parse error for %s: %s", str(token_id)[:16], exc
        )
        return None


def fetch_spread(token_id: str) -> Optional[float]:
    """
    获取 token 当前买卖价差。

    官方端点: GET https://clob.polymarket.com/spread?token_id=<TOKEN_ID>

    Returns:
        价差浮点数 (0.0~1.0)，失败返回 None
    """
    if not token_id:
        return None
    try:
        resp = requests.get(
            f"{CLOB_BASE}/spread",
            params={"token_id": token_id},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        val = data.get("spread")
        return float(val) if val is not None else None
    except (requests.RequestException, ValueError, TypeError) as e:
        logger.debug("fetch_spread failed for %s: %s", str(token_id)[:16], e)
        return None


# ---------------------------------------------------------------------------
# 分析工具
# ---------------------------------------------------------------------------


def analyze_liquidity(book: dict) -> dict:
    """
    从当前订单簿快照分析流动性指标。

    Args:
        book: fetch_orderbook() 返回的订单簿字典

    Returns:
        {
            "count": int,             # buy-side 档位数量
            "spread": float | None,   # 买卖价差（best_ask - best_bid）
            "best_bid": float | None,
            "best_ask": float | None,
            "bid_depth": float,       # 买盘总名义价值（USDC）
            "ask_depth": float,       # 卖盘总名义价值（USDC）
            "last_trade_price": float | None,
        }
    """
    if not book or not isinstance(book, dict):
        return {
            "count": 0,
            "spread": None,
            "best_bid": None,
            "best_ask": None,
            "bid_depth": 0.0,
            "ask_depth": 0.0,
            "effective_bid_depth": 0.0,
            "effective_ask_depth": 0.0,
            "last_trade_price": None,
        }

    bids = book.get("bids", [])
    asks = book.get("asks", [])

    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None

    # CLOB API 返回的 bids 可能按升序排列（lowest first），asks 可能按降序排列。
    # 使用 max/min 确保无论排序方式都能正确取到 best_bid（最高买价）和 best_ask（最低卖价）。
    try:
        if bids:
            best_bid = max(float(b.get("price", 0)) for b in bids)
        if asks:
            best_ask = min(float(a.get("price", 0)) for a in asks)
        if best_bid is not None and best_ask is not None and best_ask > best_bid:
            spread = round(best_ask - best_bid, 4)
    except (ValueError, TypeError, IndexError):
        pass

    bid_depth = sum(
        float(b.get("size", 0)) * float(b.get("price", 0))
        for b in bids
        if b.get("price") and b.get("size")
    )
    ask_depth = sum(
        float(a.get("size", 0)) * float(a.get("price", 0))
        for a in asks
        if a.get("price") and a.get("size")
    )

    # 有效深度：只统计接近中间价 ±20% 范围内的订单，排除极端挂单干扰
    effective_bid_depth = 0.0
    effective_ask_depth = 0.0
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0
        depth_range = mid * 0.20
        low_bound = mid - depth_range
        high_bound = mid + depth_range
        effective_bid_depth = sum(
            float(b.get("size", 0)) * float(b.get("price", 0))
            for b in bids
            if b.get("price") and b.get("size") and float(b["price"]) >= low_bound
        )
        effective_ask_depth = sum(
            float(a.get("size", 0)) * float(a.get("price", 0))
            for a in asks
            if a.get("price") and a.get("size") and float(a["price"]) <= high_bound
        )

    last_trade: Optional[float] = None
    try:
        ltp = book.get("last_trade_price")
        if ltp is not None:
            last_trade = float(ltp)
    except (ValueError, TypeError):
        pass

    return {
        "count": len(bids),
        "spread": spread,
        "best_bid": round(best_bid, 4) if best_bid is not None else None,
        "best_ask": round(best_ask, 4) if best_ask is not None else None,
        "bid_depth": round(bid_depth, 2),
        "ask_depth": round(ask_depth, 2),
        "effective_bid_depth": round(effective_bid_depth, 2),
        "effective_ask_depth": round(effective_ask_depth, 2),
        "last_trade_price": last_trade,
    }


def analyze_price_trend(history: list[dict]) -> dict:
    """
    从价格历史分析价格趋势指标。

    Args:
        history: fetch_prices_history() 返回的 [{t: epoch_s, p: float}, ...] 列表

    Returns:
        {
            "count": int,
            "price_current": float | None,
            "price_oldest": float | None,
            "price_change": float | None,     # current - oldest
            "price_change_pct": float | None,  # (current - oldest)/oldest * 100
            "price_min": float | None,
            "price_max": float | None,
            "trend": "up" | "down" | "flat" | None,
        }
    """
    if not history:
        return {
            "count": 0,
            "price_current": None,
            "price_oldest": None,
            "price_change": None,
            "price_change_pct": None,
            "price_min": None,
            "price_max": None,
            "trend": None,
        }

    prices = [float(p["p"]) for p in history if p.get("p") is not None]
    if not prices:
        return {
            "count": 0,
            "price_current": None,
            "price_oldest": None,
            "price_change": None,
            "price_change_pct": None,
            "price_min": None,
            "price_max": None,
            "trend": None,
        }

    current = prices[-1]
    oldest = prices[0]
    change = round(current - oldest, 4)
    change_pct = round(change / oldest * 100, 2) if oldest > 0 else None

    trend: Optional[str] = None
    if change_pct is not None:
        trend = "up" if change_pct > 1.0 else ("down" if change_pct < -1.0 else "flat")

    return {
        "count": len(prices),
        "price_current": round(current, 4),
        "price_oldest": round(oldest, 4),
        "price_change": change,
        "price_change_pct": change_pct,
        "price_min": round(min(prices), 4),
        "price_max": round(max(prices), 4),
        "trend": trend,
    }
