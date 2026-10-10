"""
crypto_fetcher.py — 加密货币实时数据抓取工具

职责：
  - 从 Polymarket crypto 类市场 question 中解析目标币种和价格
  - 调用 CoinGecko 免费 API 获取：实时价格、24h/7d 变化、波动率
  - 构建 crypto_context 注入 AI Prompt（与 weather_context / sports_context 对齐）

用途：
  SignalEngine._analyze_market() 对 category=="crypto" 调用本模块，
  使 AI 基于真实市场数据（而非纯推理）评估 crypto 市场价格问题。

API 来源：
  CoinGecko (https://api.coingecko.com/api/v3/)
  免费公开端点，无需 API key。限制 30 calls/min。
"""

import logging
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_API_BASE = "https://api.coingecko.com/api/v3"
_FNG_API = "https://api.alternative.me/fng/"
_TIMEOUT = 10
_CACHE_TTL = 120  # 2 分钟缓存（crypto 价格变化快）
_FNG_CACHE_TTL = 1800  # Fear & Greed 30 分钟缓存（变化慢）

# Polymarket 常见 crypto 名 → CoinGecko ID
_COIN_MAP: dict[str, str] = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "ether": "ethereum",
    "solana": "solana",
    "sol": "solana",
    "xrp": "ripple",
    "ripple": "ripple",
    "dogecoin": "dogecoin",
    "doge": "dogecoin",
    "cardano": "cardano",
    "ada": "cardano",
    "bnb": "binancecoin",
    "polygon": "matic-network",
    "matic": "matic-network",
    "avalanche": "avalanche-2",
    "avax": "avalanche-2",
    "polkadot": "polkadot",
    "dot": "polkadot",
    "chainlink": "chainlink",
    "link": "chainlink",
    "litecoin": "litecoin",
    "ltc": "litecoin",
}

# 从 question 中提取价格（如 "$70,000" / "$2,200" / "$100"）
_PRICE_RE = re.compile(r"\$[\d,]+(?:\.\d+)?")

# 缓存: {coin_id: (timestamp, data_dict)}
_cache: dict[str, tuple[float, dict]] = {}
# Fear & Greed 缓存: (timestamp, data_dict)
_fng_cache: tuple[float, dict] = (0.0, {})


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------


def fetch_crypto_context(question: str) -> str:
    """
    从市场 question 中解析币种和目标价格，获取实时 crypto 数据，构建 AI 上下文。

    Args:
        question: Polymarket 市场问题,
                  e.g. "Will the price of Bitcoin be above $70,000 on March 16?"

    Returns:
        格式化的 crypto 数据上下文字符串（可能为空字符串如果获取失败）
    """
    coin_id = _extract_coin(question)
    if not coin_id:
        logger.debug(f"[CryptoFetcher] 无法从 question 中解析币种: {question[:60]}")
        return ""

    target_price = _extract_target_price(question)
    market_data = _get_coin_data(coin_id)
    if not market_data:
        logger.info(f"[CryptoFetcher] {coin_id} 数据获取失败")
        return ""

    return _build_context(coin_id, market_data, target_price, question)


# ---------------------------------------------------------------------------
# 内部方法 — 解析
# ---------------------------------------------------------------------------


def _extract_coin(question: str) -> Optional[str]:
    """从 question 中提取 CoinGecko coin ID。"""
    q_lower = question.lower()
    for keyword, coin_id in _COIN_MAP.items():
        if keyword in q_lower:
            return coin_id
    return None


def _extract_target_price(question: str) -> Optional[float]:
    """从 question 中提取目标价格（美元）。"""
    matches = _PRICE_RE.findall(question)
    if not matches:
        return None
    # 取最大金额（通常问题里有多个价格时取目标价）
    prices = []
    for m in matches:
        try:
            prices.append(float(m.replace("$", "").replace(",", "")))
        except ValueError:
            continue
    return max(prices) if prices else None


# ---------------------------------------------------------------------------
# 内部方法 — API 调用
# ---------------------------------------------------------------------------


def _get_coin_data(coin_id: str) -> Optional[dict]:
    """获取币种实时数据（带缓存）。"""
    now = time.time()
    if coin_id in _cache:
        ts, data = _cache[coin_id]
        if now - ts < _CACHE_TTL:
            return data

    try:
        # 单次调用获取：当前价、24h变化、7d变化、ATH、市值
        url = f"{_API_BASE}/coins/{coin_id}"
        params = {
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "true",  # 7d sparkline 数据
        }
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        raw = resp.json()

        market_data = raw.get("market_data") or {}
        current_price_obj = market_data.get("current_price") or {}
        current_price = (
            current_price_obj.get("usd")
            if isinstance(current_price_obj, dict)
            else None
        )
        if current_price is None:
            return None

        def _safe_nested(d: dict, key: str, sub: str):
            obj = d.get(key)
            return obj.get(sub) if isinstance(obj, dict) else None

        data = {
            "current_price": current_price,
            "price_change_24h_pct": market_data.get("price_change_percentage_24h"),
            "price_change_7d_pct": market_data.get("price_change_percentage_7d"),
            "price_change_30d_pct": market_data.get("price_change_percentage_30d"),
            "high_24h": _safe_nested(market_data, "high_24h", "usd"),
            "low_24h": _safe_nested(market_data, "low_24h", "usd"),
            "ath": _safe_nested(market_data, "ath", "usd"),
            "ath_change_pct": _safe_nested(market_data, "ath_change_percentage", "usd"),
            "market_cap_usd": _safe_nested(market_data, "market_cap", "usd"),
            "total_volume_24h": _safe_nested(market_data, "total_volume", "usd"),
        }

        # 7d sparkline → 计算波动率和趋势
        sparkline_obj = market_data.get("sparkline_7d")
        sparkline = (
            sparkline_obj.get("price", []) if isinstance(sparkline_obj, dict) else []
        )
        if sparkline and len(sparkline) >= 10:
            data["sparkline_7d_high"] = max(sparkline)
            data["sparkline_7d_low"] = min(sparkline)
            # 简易波动率 = (max-min) / avg
            avg = sum(sparkline) / len(sparkline)
            data["volatility_7d_pct"] = (
                round((max(sparkline) - min(sparkline)) / avg * 100, 2)
                if avg > 0
                else 0
            )

        _cache[coin_id] = (now, data)
        logger.info(
            f"[CryptoFetcher] {coin_id} 价格=${current_price:,.2f} "
            f"24h={data.get('price_change_24h_pct', 0):+.1f}%"
        )
        return data

    except requests.RequestException as e:
        logger.warning(f"[CryptoFetcher] API 请求失败 {coin_id}: {e}")
        return None
    except (KeyError, ValueError, TypeError) as e:
        logger.warning(f"[CryptoFetcher] 数据解析失败 {coin_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# 内部方法 — Fear & Greed Index
# ---------------------------------------------------------------------------


def _fetch_fear_greed() -> dict:
    """
    获取 Bitcoin Fear & Greed 指数（免费 API，无需 key）。

    Returns:
        {"value": 25, "classification": "Extreme Fear"} 或空 dict
    """
    global _fng_cache
    now = time.time()
    ts, cached = _fng_cache
    if cached and now - ts < _FNG_CACHE_TTL:
        return cached

    try:
        resp = requests.get(_FNG_API, params={"limit": "1"}, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return {}
        entry = data[0]
        result = {
            "value": int(entry.get("value", 0)),
            "classification": entry.get("value_classification", "Unknown"),
        }
        _fng_cache = (now, result)
        logger.debug(
            f"[CryptoFetcher] Fear&Greed: {result['value']} ({result['classification']})"
        )
        return result
    except Exception as e:
        logger.debug(f"[CryptoFetcher] Fear&Greed 获取失败: {e}")
        return {}


# ---------------------------------------------------------------------------
# 内部方法 — 上下文构建
# ---------------------------------------------------------------------------


def _build_context(
    coin_id: str,
    data: dict,
    target_price: Optional[float],
    question: str,
) -> str:
    """构建注入 AI prompt 的 crypto 上下文字符串。"""
    coin_name = coin_id.replace("-", " ").title()
    current = data["current_price"]

    lines = [f"[Real-time Crypto Data — {coin_name}]"]
    lines.append(f"  Current price:  ${current:,.2f}")

    if data.get("high_24h") and data.get("low_24h"):
        lines.append(
            f"  24h range:      ${data['low_24h']:,.2f} — ${data['high_24h']:,.2f}"
        )
    if data.get("price_change_24h_pct") is not None:
        lines.append(f"  24h change:     {data['price_change_24h_pct']:+.2f}%")
    if data.get("price_change_7d_pct") is not None:
        lines.append(f"  7d change:      {data['price_change_7d_pct']:+.2f}%")
    if data.get("price_change_30d_pct") is not None:
        lines.append(f"  30d change:     {data['price_change_30d_pct']:+.2f}%")

    if data.get("sparkline_7d_high") and data.get("sparkline_7d_low"):
        lines.append(
            f"  7d range:       ${data['sparkline_7d_low']:,.2f} — "
            f"${data['sparkline_7d_high']:,.2f}"
        )
    if data.get("volatility_7d_pct"):
        lines.append(f"  7d volatility:  {data['volatility_7d_pct']:.1f}%")

    if data.get("total_volume_24h"):
        vol = data["total_volume_24h"]
        if vol >= 1e9:
            lines.append(f"  24h volume:     ${vol / 1e9:.1f}B")
        else:
            lines.append(f"  24h volume:     ${vol / 1e6:.0f}M")

    # Fear & Greed 指数
    fng = _fetch_fear_greed()
    if fng:
        lines.append(f"  Fear & Greed:   {fng['value']}/100 ({fng['classification']})")

    # 如果能解析出目标价，计算距离
    if target_price and target_price > 0:
        diff_pct = (current - target_price) / target_price * 100
        direction = "above" if current > target_price else "below"
        lines.append(
            f"  Target price:   ${target_price:,.2f} "
            f"(current is {abs(diff_pct):.1f}% {direction})"
        )

        # 判断问题类型并添加分析提示
        q_lower = question.lower()
        if "above" in q_lower or "reach" in q_lower or "hit" in q_lower:
            if current >= target_price:
                lines.append(
                    "  ⚡ Price already ABOVE target — high P(YES) unless reversal"
                )
            elif abs(diff_pct) < 3:
                lines.append("  ⚡ Price VERY CLOSE to target — moderate-high P(YES)")
            elif abs(diff_pct) < 10:
                lines.append(
                    "  ⚡ Price within 10% of target — use volatility to judge"
                )
        elif "below" in q_lower:
            if current <= target_price:
                lines.append(
                    "  ⚡ Price already BELOW target — high P(YES) unless rally"
                )
        elif "between" in q_lower:
            lines.append("  [RANGE question] Check if price likely stays in band")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 公开接口 — 结构化数据（供 crypto_probability_engine 使用）
# ---------------------------------------------------------------------------


def get_crypto_market_data(question: str) -> Optional[dict]:
    """
    从 question 解析币种并返回结构化市场数据（供概率引擎消费）。

    Returns:
        {
            "coin_id": str,
            "current_price": float,
            "price_change_24h_pct": float|None,
            "price_change_7d_pct": float|None,
            "volatility_7d_pct": float,
            "sparkline": list[float]|None,
            "fng_value": int,
            "target_price": float|None,
        } 或 None
    """
    coin_id = _extract_coin(question)
    if not coin_id:
        return None

    data = _get_coin_data(coin_id)
    if not data or not data.get("current_price"):
        return None

    # 获取 sparkline 原始数据
    # sparkline 在 _get_coin_data 中被处理成了 high/low/volatility，
    # 但原始数据在缓存中。我们需要重新从 API 缓存获取。
    sparkline = None
    if coin_id in _cache:
        _, cached = _cache[coin_id]
        # _get_coin_data 存储了完整的 data dict
        # sparkline 需要从原始 API 重新拉取（缓存中没直接存）
        # 但 sparkline_7d_high/low 存在说明有数据
        # 这里我们用 high/low 和 current_price 的间距来估算
    # 我们需要直接缓存 sparkline — 修改 _get_coin_data 太复杂
    # 简易方案：从缓存的 volatility + price 反推合理分布
    sparkline = _fetch_sparkline(coin_id)

    fng = _fetch_fear_greed()
    target_price = _extract_target_price(question)

    return {
        "coin_id": coin_id,
        "current_price": data["current_price"],
        "price_change_24h_pct": data.get("price_change_24h_pct"),
        "price_change_7d_pct": data.get("price_change_7d_pct"),
        "volatility_7d_pct": data.get("volatility_7d_pct", 10.0),
        "sparkline": sparkline,
        "fng_value": fng.get("value", 0) if fng else 0,
        "target_price": target_price,
    }


# ---------------------------------------------------------------------------
# 内部方法 — Sparkline 直取
# ---------------------------------------------------------------------------

_sparkline_cache: dict[str, tuple[float, list[float]]] = {}
_SPARKLINE_CACHE_TTL = 300  # 5 分钟


def _fetch_sparkline(coin_id: str) -> Optional[list[float]]:
    """直接获取 7d sparkline 价格序列（带独立缓存）。"""
    now = time.time()
    if coin_id in _sparkline_cache:
        ts, data = _sparkline_cache[coin_id]
        if now - ts < _SPARKLINE_CACHE_TTL:
            return data

    try:
        url = f"{_API_BASE}/coins/{coin_id}"
        params = {
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "true",
        }
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        raw = resp.json()
        market_data = raw.get("market_data") or {}
        sparkline_obj = market_data.get("sparkline_7d")
        sparkline = (
            sparkline_obj.get("price", []) if isinstance(sparkline_obj, dict) else []
        )
        if sparkline and len(sparkline) >= 10:
            _sparkline_cache[coin_id] = (now, sparkline)
            return sparkline
        return None
    except Exception as e:
        logger.debug(f"[CryptoFetcher] sparkline 获取失败 {coin_id}: {e}")
        return None
