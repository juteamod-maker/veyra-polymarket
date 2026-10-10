"""
crypto_probability_engine.py — 数据驱动的加密货币概率引擎

核心思想（第一性原理）：
  "数据计算概率，AI 验证" — 与天气赛道 GFS Ensemble 同一哲学。
  天气赛道靠 GFS 31-member ensemble 独立算概率，crypto 赛道靠
  Black-Scholes inspired 模型 + VWAP 偏差 + 动量 + Fear&Greed 独立算概率。

概率模型：
  P(S_T > K) = Φ((ln(S/K) + (μ - σ²/2)·T) / (σ·√T))
  - S = 当前价格, K = 目标价格, μ = 漂移率, σ = 波动率, T = 时间(年)
  - Φ = 标准正态 CDF（用 math.erfc 实现，零外部依赖）

特征增强（来自 polyrec/mlmodelpoly 思路，零外部依赖实现）：
  - VWAP 偏差：sparkline 近似，价格 vs 均值位置
  - 动量评分：sparkline 线性回归斜率
  - Fear & Greed 反转信号：极端情绪的逆向调整

安全约束：
  - 概率输出仅为 ADVISORY — AI ensemble 仍做最终决策
  - 数据不足时返回 None，不强制使用
  - 所有参数从 config.json 读取
"""

import logging
import math
import re
from typing import Optional

from src.utils.config_loader import get

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 标准正态 CDF（零外部依赖，用 math.erfc 实现）
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    """标准正态分布 CDF: Φ(x) = 0.5 * erfc(-x / √2)"""
    return 0.5 * math.erfc(-x / math.sqrt(2))


# ---------------------------------------------------------------------------
# 方向与目标价解析
# ---------------------------------------------------------------------------

_ABOVE_PATTERNS = re.compile(
    r"(above|over|exceed|reach|hit|surpass|higher than|at least|or higher|or above|or more)",
    re.IGNORECASE,
)
_BELOW_PATTERNS = re.compile(
    r"(below|under|drop|fall|lower than|at most|or below|or less|or lower)",
    re.IGNORECASE,
)
_BETWEEN_PATTERNS = re.compile(r"between", re.IGNORECASE)
_PRICE_RE = re.compile(r"\$[\d,]+(?:\.\d+)?")


def parse_crypto_threshold(question: str) -> Optional[dict]:
    """
    从 market question 中解析目标价和方向。

    Returns:
        {"target_price": float, "direction": "above"|"below"|"between",
         "target_high": float|None} 或 None
    """
    matches = _PRICE_RE.findall(question)
    if not matches:
        return None

    prices = []
    for m in matches:
        try:
            prices.append(float(m.replace("$", "").replace(",", "")))
        except ValueError:
            continue
    if not prices:
        return None

    if _BETWEEN_PATTERNS.search(question) and len(prices) >= 2:
        return {
            "target_price": min(prices),
            "target_high": max(prices),
            "direction": "between",
        }

    target = max(prices)  # 通常取最大金额作为目标价

    if _BELOW_PATTERNS.search(question):
        return {"target_price": target, "target_high": None, "direction": "below"}
    # 默认视为 above（Polymarket 最常见的问法）
    return {"target_price": target, "target_high": None, "direction": "above"}


# ---------------------------------------------------------------------------
# 特征计算（灵感来自 polyrec/mlmodelpoly VWAP/CVD/波动率思路）
# ---------------------------------------------------------------------------


def _calc_vwap_adjustment(
    sparkline: list[float], current_price: float, max_adj: float
) -> float:
    """
    VWAP 偏差调整 — sparkline 近似 VWAP（时间加权均价）。

    价格 > VWAP → 超买 → 继续上涨概率降低（负调整 for "above" targets）
    价格 < VWAP → 超卖 → 反弹上涨概率升高（正调整 for "above" targets）

    Returns: [-max_adj, +max_adj] 范围的调整值
    """
    if not sparkline or len(sparkline) < 10:
        return 0.0

    vwap = sum(sparkline) / len(sparkline)
    if vwap <= 0:
        return 0.0

    deviation = (current_price - vwap) / vwap  # 正=超买, 负=超卖

    # 超买 → 负调整（不利于继续上行），超卖 → 正调整（利于反弹）
    # 使用反向：对于 "above" 目标，超买是负面信号
    adj = -deviation * max_adj * 5  # 放大因子使 2% 偏差 → 50% max_adj
    return max(-max_adj, min(max_adj, adj))


def _calc_momentum_score(sparkline: list[float], max_adj: float) -> float:
    """
    动量评分 — sparkline 线性回归斜率（标准化）。

    正斜率=上升趋势（有利于 above），负斜率=下降趋势（有利于 below）。

    Returns: [-max_adj, +max_adj]
    """
    if not sparkline or len(sparkline) < 10:
        return 0.0

    n = len(sparkline)
    # 简易线性回归斜率: slope = Σ(i·y_i) - n·x̄·ȳ) / (Σi² - n·x̄²)
    x_mean = (n - 1) / 2.0
    y_mean = sum(sparkline) / n

    numerator = sum(i * sparkline[i] for i in range(n)) - n * x_mean * y_mean
    denominator = sum(i * i for i in range(n)) - n * x_mean * x_mean

    if abs(denominator) < 1e-12:
        return 0.0

    slope = numerator / denominator

    # 标准化：斜率 / 平均价格 → 每步百分比变化
    if y_mean <= 0:
        return 0.0
    norm_slope = slope / y_mean  # 每步涨跌%

    # 乘以 n 得到总趋势强度，限制在 max_adj 内
    trend_strength = norm_slope * n * 2  # 放大因子
    return max(-max_adj, min(max_adj, trend_strength))


def _calc_fng_adjustment(fng_value: int, max_adj: float) -> float:
    """
    Fear & Greed 反转调整。

    极端恐惧 (< 25) → 正调整（反转买入信号，利于上涨）
    极端贪婪 (> 75) → 负调整（反转卖出信号，利于下跌）
    中性区间 (25-75) → 零调整

    Returns: [-max_adj, +max_adj]
    """
    if fng_value <= 0:
        return 0.0

    _fear_th = int(get("crypto_engine.fng_extreme_fear_threshold") or 25)
    _greed_th = int(get("crypto_engine.fng_extreme_greed_threshold") or 75)
    if fng_value < _fear_th:
        # 极端恐惧 → 正调整（值越低调整越大）
        intensity = (_fear_th - fng_value) / float(_fear_th)
        return intensity * max_adj
    elif fng_value > _greed_th:
        # 极端贪婪 → 负调整（值越高调整越大）
        intensity = (fng_value - _greed_th) / float(100 - _greed_th)
        return -intensity * max_adj
    return 0.0


# ---------------------------------------------------------------------------
# Sparkline-based drift estimation
# ---------------------------------------------------------------------------


def _calc_sparkline_drift(sparkline: list[float]) -> float:
    """Estimate annualized drift from sparkline (hourly prices) via log-return regression.

    Uses the linear regression slope of log-prices, then annualizes (×8760 hours/year).
    This is significantly more stable than naive 24h_change × 365.
    Requires at least 48 data points; callers should verify before calling.
    """
    n = len(sparkline)
    # Convert to log-prices for geometric return estimation
    log_prices = []
    for p in sparkline:
        if p > 0:
            log_prices.append(math.log(p))
        else:
            log_prices.append(log_prices[-1] if log_prices else 0.0)
    if len(log_prices) < 2:
        return 0.0

    # Linear regression slope of log prices: Σ(i·y_i - n·x̄·ȳ) / (Σi² - n·x̄²)
    x_mean = (n - 1) / 2.0
    y_mean = sum(log_prices) / n
    numerator = sum(i * log_prices[i] for i in range(n)) - n * x_mean * y_mean
    denominator = sum(i * i for i in range(n)) - n * x_mean * x_mean
    if abs(denominator) < 1e-12:
        return 0.0

    slope_per_hour = numerator / denominator
    return slope_per_hour * 8760  # annualize


# ---------------------------------------------------------------------------
# 核心概率计算
# ---------------------------------------------------------------------------


def calculate_threshold_probability(
    current_price: float,
    target_price: float,
    volatility_7d_pct: float,
    change_24h_pct: Optional[float],
    time_to_settlement_days: float,
    direction: str = "above",
    sparkline: Optional[list[float]] = None,
    fng_value: int = 0,
    vwap_adj_max: float = 0.05,
    momentum_adj_max: float = 0.03,
    fng_adj_max: float = 0.05,
    target_high: Optional[float] = None,
) -> dict:
    """
    Black-Scholes inspired 概率计算 + 特征增强。

    Args:
        current_price: 当前价格
        target_price: 目标价格（下界或单一目标）
        volatility_7d_pct: 7天波动率百分比 (e.g. 8.5 = 8.5%)
        change_24h_pct: 24小时涨跌幅百分比
        time_to_settlement_days: 距离结算天数
        direction: "above" / "below" / "between"
        sparkline: 7天价格序列（168个点）
        fng_value: Fear & Greed 指数 (0-100)
        vwap_adj_max: VWAP调整最大幅度
        momentum_adj_max: 动量调整最大幅度
        fng_adj_max: FNG调整最大幅度
        target_high: "between" 模式的上界

    Returns:
        {"probability": float, "confidence": float, "components": dict}
    """
    if current_price <= 0 or target_price <= 0:
        return {"probability": 0.5, "confidence": 0.3, "components": {}}

    # === 年化参数 ===
    # 波动率: 7d_pct → 年化 (×√(365/7))
    sigma_annual = (volatility_7d_pct / 100.0) * math.sqrt(365 / 7)
    _min_vol = float(get("crypto_engine.min_annual_volatility") or 0.50)
    if sigma_annual < 0.01:
        sigma_annual = _min_vol  # 最低年化波动率（crypto不可能更低）

    # 漂移率: 优先用 7-day sparkline 线性回归斜率（≥48点），fallback to 24h change
    mu_annual = 0.0
    if sparkline and len(sparkline) >= 48:
        mu_annual = _calc_sparkline_drift(sparkline)
    elif change_24h_pct is not None:
        mu_annual = (change_24h_pct / 100.0) * 365  # 年化
    # 限制漂移率，防止极端外推
    mu_annual = max(-2.0, min(2.0, mu_annual))

    # 时间: 天 → 年
    t_years = max(time_to_settlement_days / 365.0, 1 / (365 * 24))  # 至少1小时

    # === Black-Scholes 核心 ===
    s_over_k = current_price / target_price
    if s_over_k <= 0:
        base_prob = 0.5
    else:
        d = (math.log(s_over_k) + (mu_annual - 0.5 * sigma_annual**2) * t_years) / (
            sigma_annual * math.sqrt(t_years)
        )
        base_prob = _norm_cdf(d)  # P(S_T > K)

    if direction == "below":
        base_prob = 1.0 - base_prob
    elif direction == "between" and target_high and target_high > target_price:
        # P(K1 < S_T < K2) = P(S_T > K1) - P(S_T > K2)
        d_high = (
            math.log(current_price / target_high)
            + (mu_annual - 0.5 * sigma_annual**2) * t_years
        ) / (sigma_annual * math.sqrt(t_years))
        prob_above_high = _norm_cdf(d_high)
        base_prob = base_prob - prob_above_high

    # === 特征增强调整（polyrec/mlmodelpoly 思路） ===
    vwap_adj = _calc_vwap_adjustment(sparkline or [], current_price, vwap_adj_max)
    momentum_adj = _calc_momentum_score(sparkline or [], momentum_adj_max)
    fng_adj = _calc_fng_adjustment(fng_value, fng_adj_max)

    # 方向修正：对 below 方向，VWAP/momentum 信号需要反转
    if direction == "below":
        vwap_adj = -vwap_adj
        momentum_adj = -momentum_adj
        fng_adj = -fng_adj

    final_prob = base_prob + vwap_adj + momentum_adj + fng_adj
    final_prob = max(0.02, min(0.98, final_prob))  # 硬限 2%-98%

    # === Confidence 计算 ===
    # 数据越多、距离越远（方向明确）→ 信心越高
    distance_pct = abs(current_price - target_price) / target_price
    _ct = get("crypto_engine.confidence_tiers") or {}
    _conf_base = float(get("crypto_engine.confidence_base") or 0.45)
    _far_pct = float(_ct.get("far_distance_pct", 0.10))
    _far_conf = float(_ct.get("far_confidence", 0.55))
    _vfar_pct = float(_ct.get("very_far_distance_pct", 0.20))
    _vfar_conf = float(_ct.get("very_far_confidence", 0.65))
    conf_base = _conf_base
    if distance_pct > _far_pct:
        conf_base = _far_conf
    if distance_pct > _vfar_pct:
        conf_base = _vfar_conf
    # 高波动率 → 降信心
    if sigma_annual > 1.5:
        conf_base -= 0.05
    # sparkline 缺失 → 降信心
    if not sparkline or len(sparkline) < 10:
        conf_base -= 0.10
    confidence = max(0.30, min(0.80, conf_base))

    components = {
        "base_prob": round(base_prob, 4),
        "vwap_adj": round(vwap_adj, 4),
        "momentum_adj": round(momentum_adj, 4),
        "fng_adj": round(fng_adj, 4),
        "sigma_annual": round(sigma_annual, 3),
        "mu_annual": round(mu_annual, 3),
        "t_years": round(t_years, 6),
        "distance_pct": round(distance_pct, 4),
    }

    logger.info(
        f"[CryptoProb] P={final_prob:.3f} conf={confidence:.2f} "
        f"base={base_prob:.3f} vwap={vwap_adj:+.3f} "
        f"mom={momentum_adj:+.3f} fng={fng_adj:+.3f} "
        f"σ={sigma_annual:.2f} μ={mu_annual:.2f}"
    )

    return {
        "probability": round(final_prob, 4),
        "confidence": round(confidence, 2),
        "components": components,
    }


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def get_crypto_data_signal(
    question: str,
    market_data: Optional[dict] = None,
    time_to_settlement_days: float = 1.0,
) -> Optional[dict]:
    """
    从 question + 市场数据计算数据驱动的概率信号。

    Args:
        question: Polymarket 市场问题
        market_data: crypto_fetcher 返回的数据 dict（如为 None 则自动获取）
        time_to_settlement_days: 距离结算天数

    Returns:
        {"probability": float, "direction": str, "edge": float,
         "confidence": float, "components": dict} 或 None
    """
    from src.utils.config_loader import get as cfg_get

    # 解析目标方向和价格
    threshold = parse_crypto_threshold(question)
    if not threshold:
        logger.debug("[CryptoProb] 无法从 question 解析目标价")
        return None

    # 获取市场数据
    if market_data is None:
        from src.utils.crypto_fetcher import get_crypto_market_data

        market_data = get_crypto_market_data(question)
    if not market_data:
        logger.debug("[CryptoProb] 无法获取市场数据")
        return None

    current_price = market_data.get("current_price")
    if not current_price or current_price <= 0:
        return None

    # 从 config 读取调整幅度参数
    vwap_max = float(cfg_get("paper_trading.crypto.vwap_adj_max") or 0.05)
    momentum_max = float(cfg_get("paper_trading.crypto.momentum_adj_max") or 0.03)
    fng_max = float(cfg_get("paper_trading.crypto.fng_adj_max") or 0.05)

    volatility = market_data.get("volatility_7d_pct", 10.0)

    result = calculate_threshold_probability(
        current_price=current_price,
        target_price=threshold["target_price"],
        volatility_7d_pct=volatility,
        change_24h_pct=market_data.get("price_change_24h_pct"),
        time_to_settlement_days=time_to_settlement_days,
        direction=threshold["direction"],
        sparkline=market_data.get("sparkline"),
        fng_value=market_data.get("fng_value", 0),
        vwap_adj_max=vwap_max,
        momentum_adj_max=momentum_max,
        fng_adj_max=fng_max,
        target_high=threshold.get("target_high"),
    )

    prob = result["probability"]
    data_direction = "YES" if prob > 0.5 else "NO"
    # Edge = 数据概率距离中性(0.5)的距离
    edge = abs(prob - 0.5)

    return {
        "probability": prob,
        "direction": data_direction,
        "edge": edge,
        "confidence": result["confidence"],
        "components": result["components"],
        "threshold": threshold,
    }
