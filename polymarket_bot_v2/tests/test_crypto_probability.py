"""
test_crypto_probability.py — Crypto 概率引擎单元测试

测试覆盖：
  - Black-Scholes 核心概率计算
  - 方向解析（above/below/between）
  - VWAP / 动量 / F&G 调整
  - get_crypto_data_signal 集成
  - 边界情况与降级
"""

import math
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _norm_cdf 测试
# ---------------------------------------------------------------------------


class TestNormCdf:
    def test_zero(self):
        from src.utils.crypto_probability_engine import _norm_cdf

        assert abs(_norm_cdf(0) - 0.5) < 1e-10

    def test_positive(self):
        from src.utils.crypto_probability_engine import _norm_cdf

        assert _norm_cdf(1.0) > 0.84
        assert _norm_cdf(1.0) < 0.85

    def test_negative(self):
        from src.utils.crypto_probability_engine import _norm_cdf

        assert _norm_cdf(-1.0) > 0.15
        assert _norm_cdf(-1.0) < 0.16

    def test_extreme_positive(self):
        from src.utils.crypto_probability_engine import _norm_cdf

        assert _norm_cdf(5.0) > 0.999

    def test_extreme_negative(self):
        from src.utils.crypto_probability_engine import _norm_cdf

        assert _norm_cdf(-5.0) < 0.001


# ---------------------------------------------------------------------------
# parse_crypto_threshold 测试
# ---------------------------------------------------------------------------


class TestParseCryptoThreshold:
    def test_above_pattern(self):
        from src.utils.crypto_probability_engine import parse_crypto_threshold

        result = parse_crypto_threshold("Will Bitcoin be above $70,000 on March 28?")
        assert result is not None
        assert result["target_price"] == 70000.0
        assert result["direction"] == "above"

    def test_below_pattern(self):
        from src.utils.crypto_probability_engine import parse_crypto_threshold

        result = parse_crypto_threshold("Will ETH drop below $2,200 by Friday?")
        assert result is not None
        assert result["target_price"] == 2200.0
        assert result["direction"] == "below"

    def test_between_pattern(self):
        from src.utils.crypto_probability_engine import parse_crypto_threshold

        result = parse_crypto_threshold("Will Bitcoin be between $65,000 and $70,000?")
        assert result is not None
        assert result["direction"] == "between"
        assert result["target_price"] == 65000.0
        assert result["target_high"] == 70000.0

    def test_no_price(self):
        from src.utils.crypto_probability_engine import parse_crypto_threshold

        result = parse_crypto_threshold("Will Bitcoin crash?")
        assert result is None

    def test_default_above(self):
        from src.utils.crypto_probability_engine import parse_crypto_threshold

        result = parse_crypto_threshold(
            "Will the price of Bitcoin be $100,000 on April 1?"
        )
        assert result is not None
        assert result["direction"] == "above"  # 默认 above

    def test_reach_pattern(self):
        from src.utils.crypto_probability_engine import parse_crypto_threshold

        result = parse_crypto_threshold("Will SOL reach $200 this week?")
        assert result is not None
        assert result["direction"] == "above"
        assert result["target_price"] == 200.0


# ---------------------------------------------------------------------------
# calculate_threshold_probability 测试
# ---------------------------------------------------------------------------


class TestCalculateThresholdProbability:
    def test_price_above_target_high_prob(self):
        """当前价格远高于目标价 → P(above) 应该很高"""
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        result = calculate_threshold_probability(
            current_price=72000,
            target_price=65000,
            volatility_7d_pct=8.0,
            change_24h_pct=2.0,
            time_to_settlement_days=1.0,
            direction="above",
        )
        assert result["probability"] > 0.7

    def test_price_below_target_low_prob(self):
        """当前价格远低于目标价 → P(above) 应该很低"""
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        result = calculate_threshold_probability(
            current_price=60000,
            target_price=75000,
            volatility_7d_pct=5.0,
            change_24h_pct=-1.0,
            time_to_settlement_days=1.0,
            direction="above",
        )
        assert result["probability"] < 0.3

    def test_below_direction(self):
        """below 方向：价格低于目标 → P(below) 高"""
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        result = calculate_threshold_probability(
            current_price=60000,
            target_price=65000,
            volatility_7d_pct=8.0,
            change_24h_pct=-2.0,
            time_to_settlement_days=1.0,
            direction="below",
        )
        assert result["probability"] > 0.6

    def test_between_direction(self):
        """between 方向：价格在区间内 → P 应合理"""
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        result = calculate_threshold_probability(
            current_price=67000,
            target_price=65000,
            volatility_7d_pct=8.0,
            change_24h_pct=0.0,
            time_to_settlement_days=2.0,
            direction="between",
            target_high=70000,
        )
        assert 0.1 < result["probability"] < 0.9

    def test_probability_clamped(self):
        """概率应被限制在 [0.02, 0.98]"""
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        result = calculate_threshold_probability(
            current_price=100000,
            target_price=50000,
            volatility_7d_pct=2.0,
            change_24h_pct=5.0,
            time_to_settlement_days=0.1,
            direction="above",
        )
        assert result["probability"] <= 0.98

    def test_confidence_with_sparkline(self):
        """有 sparkline 时 confidence 应该更高"""
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        sparkline = [70000 + i * 10 for i in range(168)]
        result = calculate_threshold_probability(
            current_price=71680,
            target_price=70000,
            volatility_7d_pct=5.0,
            change_24h_pct=1.0,
            time_to_settlement_days=1.0,
            direction="above",
            sparkline=sparkline,
        )
        assert result["confidence"] >= 0.40

    def test_confidence_without_sparkline(self):
        """无 sparkline 时 confidence 应该降低"""
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        result = calculate_threshold_probability(
            current_price=71680,
            target_price=70000,
            volatility_7d_pct=5.0,
            change_24h_pct=1.0,
            time_to_settlement_days=1.0,
            direction="above",
            sparkline=None,
        )
        assert result["confidence"] <= 0.50

    def test_components_present(self):
        """确保返回所有组件"""
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        result = calculate_threshold_probability(
            current_price=70000,
            target_price=70000,
            volatility_7d_pct=8.0,
            change_24h_pct=0.0,
            time_to_settlement_days=1.0,
        )
        comp = result["components"]
        for key in (
            "base_prob",
            "vwap_adj",
            "momentum_adj",
            "fng_adj",
            "sigma_annual",
            "mu_annual",
            "t_years",
            "distance_pct",
        ):
            assert key in comp

    def test_zero_price_safe(self):
        """价格为 0 不崩溃"""
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        result = calculate_threshold_probability(
            current_price=0,
            target_price=70000,
            volatility_7d_pct=8.0,
            change_24h_pct=None,
            time_to_settlement_days=1.0,
        )
        assert result["probability"] == 0.5

    def test_low_volatility_floor(self):
        """极低波动率应被提升到最低值"""
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        result = calculate_threshold_probability(
            current_price=70000,
            target_price=70000,
            volatility_7d_pct=0.001,
            change_24h_pct=0.0,
            time_to_settlement_days=1.0,
        )
        assert result["components"]["sigma_annual"] >= 0.50


# ---------------------------------------------------------------------------
# VWAP / 动量 / F&G 调整测试
# ---------------------------------------------------------------------------


class TestFeatureAdjustments:
    def test_vwap_overbought(self):
        """价格高于 VWAP → 负调整"""
        from src.utils.crypto_probability_engine import _calc_vwap_adjustment

        sparkline = [100.0] * 100
        # 当前价格高于 VWAP
        adj = _calc_vwap_adjustment(sparkline, 110.0, 0.05)
        assert adj < 0  # 超买 → 负调整

    def test_vwap_oversold(self):
        """价格低于 VWAP → 正调整"""
        from src.utils.crypto_probability_engine import _calc_vwap_adjustment

        sparkline = [100.0] * 100
        adj = _calc_vwap_adjustment(sparkline, 90.0, 0.05)
        assert adj > 0  # 超卖 → 正调整

    def test_vwap_neutral(self):
        """价格等于 VWAP → 零调整"""
        from src.utils.crypto_probability_engine import _calc_vwap_adjustment

        sparkline = [100.0] * 100
        adj = _calc_vwap_adjustment(sparkline, 100.0, 0.05)
        assert abs(adj) < 0.001

    def test_vwap_clamped(self):
        """调整值不超过 max_adj"""
        from src.utils.crypto_probability_engine import _calc_vwap_adjustment

        sparkline = [100.0] * 100
        adj = _calc_vwap_adjustment(sparkline, 200.0, 0.05)
        assert abs(adj) <= 0.05

    def test_vwap_empty_sparkline(self):
        """空 sparkline → 零调整"""
        from src.utils.crypto_probability_engine import _calc_vwap_adjustment

        assert _calc_vwap_adjustment([], 100.0, 0.05) == 0.0

    def test_momentum_uptrend(self):
        """上升趋势 → 正调整"""
        from src.utils.crypto_probability_engine import _calc_momentum_score

        sparkline = [100.0 + i * 0.5 for i in range(100)]
        adj = _calc_momentum_score(sparkline, 0.03)
        assert adj > 0

    def test_momentum_downtrend(self):
        """下降趋势 → 负调整"""
        from src.utils.crypto_probability_engine import _calc_momentum_score

        sparkline = [100.0 - i * 0.5 for i in range(100)]
        adj = _calc_momentum_score(sparkline, 0.03)
        assert adj < 0

    def test_momentum_flat(self):
        """平稳价格 → 接近零"""
        from src.utils.crypto_probability_engine import _calc_momentum_score

        sparkline = [100.0] * 100
        adj = _calc_momentum_score(sparkline, 0.03)
        assert abs(adj) < 0.001

    def test_momentum_clamped(self):
        """调整值不超过 max_adj"""
        from src.utils.crypto_probability_engine import _calc_momentum_score

        sparkline = [100.0 + i * 10 for i in range(100)]  # 极端上涨
        adj = _calc_momentum_score(sparkline, 0.03)
        assert abs(adj) <= 0.03

    def test_fng_extreme_fear(self):
        """极端恐惧 → 正调整（反转买入）"""
        from src.utils.crypto_probability_engine import _calc_fng_adjustment

        adj = _calc_fng_adjustment(10, 0.05)
        assert adj > 0

    def test_fng_extreme_greed(self):
        """极端贪婪 → 负调整（反转卖出）"""
        from src.utils.crypto_probability_engine import _calc_fng_adjustment

        adj = _calc_fng_adjustment(90, 0.05)
        assert adj < 0

    def test_fng_neutral(self):
        """中性情绪 → 零调整"""
        from src.utils.crypto_probability_engine import _calc_fng_adjustment

        adj = _calc_fng_adjustment(50, 0.05)
        assert adj == 0.0

    def test_fng_zero(self):
        """F&G 为 0 → 零调整"""
        from src.utils.crypto_probability_engine import _calc_fng_adjustment

        adj = _calc_fng_adjustment(0, 0.05)
        assert adj == 0.0


# ---------------------------------------------------------------------------
# get_crypto_data_signal 集成测试
# ---------------------------------------------------------------------------


class TestGetCryptoDataSignal:
    def test_with_mock_market_data(self):
        from src.utils.crypto_probability_engine import get_crypto_data_signal

        mock_data = {
            "coin_id": "bitcoin",
            "current_price": 72000.0,
            "price_change_24h_pct": 2.0,
            "price_change_7d_pct": 5.0,
            "volatility_7d_pct": 8.0,
            "sparkline": [71000 + i * 10 for i in range(168)],
            "fng_value": 45,
            "target_price": 70000.0,
        }
        with patch("src.utils.config_loader.get") as mock_cfg:
            mock_cfg.side_effect = lambda k: {
                "paper_trading.crypto_vwap_adj_max": 0.05,
                "paper_trading.crypto_momentum_adj_max": 0.03,
                "paper_trading.crypto_fng_adj_max": 0.05,
            }.get(k)

            result = get_crypto_data_signal(
                "Will Bitcoin be above $70,000?",
                market_data=mock_data,
                time_to_settlement_days=1.0,
            )

        assert result is not None
        assert "probability" in result
        assert "direction" in result
        assert "edge" in result
        assert result["direction"] in ("YES", "NO")
        # 价格 72000 > 70000 → 大概率 YES
        assert result["direction"] == "YES"
        assert result["probability"] > 0.5

    def test_no_price_returns_none(self):
        from src.utils.crypto_probability_engine import get_crypto_data_signal

        result = get_crypto_data_signal(
            "Will crypto market crash?",
            market_data={"current_price": 0},
        )
        assert result is None

    def test_no_target_returns_none(self):
        from src.utils.crypto_probability_engine import get_crypto_data_signal

        result = get_crypto_data_signal(
            "Will Bitcoin moon?",
            market_data={"current_price": 70000},
        )
        assert result is None

    def test_edge_calculation(self):
        """edge = |prob - 0.5|"""
        from src.utils.crypto_probability_engine import get_crypto_data_signal

        mock_data = {
            "current_price": 80000.0,
            "price_change_24h_pct": 3.0,
            "volatility_7d_pct": 6.0,
            "sparkline": None,
            "fng_value": 50,
        }
        with patch("src.utils.config_loader.get") as mock_cfg:
            mock_cfg.side_effect = lambda k: {
                "paper_trading.crypto_vwap_adj_max": 0.05,
                "paper_trading.crypto_momentum_adj_max": 0.03,
                "paper_trading.crypto_fng_adj_max": 0.05,
            }.get(k)

            result = get_crypto_data_signal(
                "Will Bitcoin be above $70,000?",
                market_data=mock_data,
                time_to_settlement_days=1.0,
            )

        assert result is not None
        assert abs(result["edge"] - abs(result["probability"] - 0.5)) < 0.001


# ---------------------------------------------------------------------------
# crypto_fetcher.get_crypto_market_data 测试
# ---------------------------------------------------------------------------


class TestGetCryptoMarketData:
    @patch("src.utils.crypto_fetcher._fetch_sparkline")
    @patch("src.utils.crypto_fetcher._fetch_fear_greed")
    @patch("src.utils.crypto_fetcher._get_coin_data")
    def test_returns_structured_data(self, mock_coin, mock_fng, mock_spark):
        from src.utils.crypto_fetcher import get_crypto_market_data

        mock_coin.return_value = {
            "current_price": 70000.0,
            "price_change_24h_pct": 1.5,
            "price_change_7d_pct": 3.0,
            "volatility_7d_pct": 7.5,
        }
        mock_fng.return_value = {"value": 55, "classification": "Greed"}
        mock_spark.return_value = [69000 + i for i in range(168)]

        result = get_crypto_market_data("Will Bitcoin be above $70,000?")
        assert result is not None
        assert result["coin_id"] == "bitcoin"
        assert result["current_price"] == 70000.0
        assert result["fng_value"] == 55
        assert result["sparkline"] is not None
        assert len(result["sparkline"]) == 168
        assert result["target_price"] == 70000.0

    @patch("src.utils.crypto_fetcher._get_coin_data")
    def test_unknown_coin_returns_none(self, mock_coin):
        from src.utils.crypto_fetcher import get_crypto_market_data

        result = get_crypto_market_data("Will XYZNONEXIST go up?")
        assert result is None
        mock_coin.assert_not_called()

    @patch("src.utils.crypto_fetcher._get_coin_data")
    def test_api_failure_returns_none(self, mock_coin):
        from src.utils.crypto_fetcher import get_crypto_market_data

        mock_coin.return_value = None
        result = get_crypto_market_data("Will Bitcoin reach $100,000?")
        assert result is None


# ---------------------------------------------------------------------------
# _evaluate_crypto_entry 集成测试
# ---------------------------------------------------------------------------


class TestEvaluateCryptoEntry:
    """测试 paper_trader._evaluate_crypto_entry 方法"""

    def _make_signal(
        self,
        outcome="YES",
        ev=0.10,
        confidence=0.65,
        size_usdc=3.0,
        expected_price=0.55,
    ):
        sig = MagicMock()
        sig.outcome = outcome
        sig.ev = ev
        sig.confidence = confidence
        sig.size_usdc = size_usdc
        sig.expected_price = expected_price
        sig.model_votes = {"model1": {"outcome": outcome}}
        sig.market_id = "test_market_123"
        return sig

    @patch("src.utils.crypto_probability_engine.get_crypto_data_signal")
    def test_data_signal_agrees_passes(self, mock_data_signal):
        from src.simulation.paper_trader import PaperTrader

        mock_data_signal.return_value = {
            "probability": 0.72,
            "direction": "YES",
            "edge": 0.22,
            "confidence": 0.60,
            "components": {},
            "threshold": {"target_price": 70000, "direction": "above"},
        }
        market = {"question": "Will Bitcoin be above $70,000?", "volume24hr": 50000}
        signal = self._make_signal(outcome="YES")
        pt_cfg = {
            "crypto": {"max_size_usdc": 5.0, "min_data_edge": 0.06, "insurance_min_price": 0.88},
            "dynamic_sizing": {"enabled": False},
        }

        result = PaperTrader._evaluate_crypto_entry(market, signal, pt_cfg, 100.0)
        assert result is not None
        size, bucket = result
        assert size > 0
        assert "crypto" in bucket

    @patch("src.utils.crypto_probability_engine.get_crypto_data_signal")
    def test_direction_conflict_rejects(self, mock_data_signal):
        from src.simulation.paper_trader import PaperTrader

        mock_data_signal.return_value = {
            "probability": 0.30,
            "direction": "NO",
            "edge": 0.20,
            "confidence": 0.55,
            "components": {},
            "threshold": {"target_price": 70000, "direction": "above"},
        }
        market = {"question": "Will Bitcoin be above $70,000?", "volume24hr": 50000}
        signal = self._make_signal(outcome="YES")
        pt_cfg = {
            "crypto": {"max_size_usdc": 5.0, "min_data_edge": 0.06, "insurance_min_price": 0.88},
        }

        result = PaperTrader._evaluate_crypto_entry(market, signal, pt_cfg, 100.0)
        assert result is None  # 方向矛盾 → 拒绝

    @patch("src.utils.crypto_probability_engine.get_crypto_data_signal")
    def test_low_edge_rejects(self, mock_data_signal):
        from src.simulation.paper_trader import PaperTrader

        mock_data_signal.return_value = {
            "probability": 0.53,
            "direction": "YES",
            "edge": 0.03,  # 低于门槛 0.06
            "confidence": 0.45,
            "components": {},
            "threshold": {"target_price": 70000, "direction": "above"},
        }
        market = {"question": "Will Bitcoin be above $70,000?", "volume24hr": 50000}
        signal = self._make_signal(outcome="YES")
        pt_cfg = {
            "crypto": {"max_size_usdc": 5.0, "min_data_edge": 0.06, "insurance_min_price": 0.88},
        }

        result = PaperTrader._evaluate_crypto_entry(market, signal, pt_cfg, 100.0)
        assert result is None  # edge 太低 → 拒绝

    @patch("src.utils.crypto_probability_engine.get_crypto_data_signal")
    def test_no_data_signal_fallback(self, mock_data_signal):
        """无法计算数据概率时降级到基础逻辑"""
        from src.simulation.paper_trader import PaperTrader

        mock_data_signal.return_value = None
        market = {"question": "Will crypto market cap exceed $3T?", "volume24hr": 50000}
        signal = self._make_signal(outcome="YES")
        pt_cfg = {
            "crypto": {"max_size_usdc": 5.0, "min_data_edge": 0.06, "insurance_min_price": 0.88},
            "dynamic_sizing": {"enabled": False},
        }

        result = PaperTrader._evaluate_crypto_entry(market, signal, pt_cfg, 100.0)
        assert result is not None
        _, bucket = result
        assert bucket == "crypto_basic"

    @patch("src.utils.crypto_probability_engine.get_crypto_data_signal")
    def test_strong_edge_bonus(self, mock_data_signal):
        """强信号（edge >= 2× 门槛）时加仓"""
        from src.simulation.paper_trader import PaperTrader

        mock_data_signal.return_value = {
            "probability": 0.85,
            "direction": "YES",
            "edge": 0.35,  # >> 0.06 * 2
            "confidence": 0.70,
            "components": {},
            "threshold": {"target_price": 70000, "direction": "above"},
        }
        market = {"question": "Will Bitcoin be above $70,000?", "volume24hr": 50000}
        signal = self._make_signal(outcome="YES", size_usdc=3.0)
        pt_cfg = {
            "crypto": {"max_size_usdc": 5.0, "min_data_edge": 0.06, "insurance_min_price": 0.88},
            "dynamic_sizing": {"enabled": False},
        }

        result = PaperTrader._evaluate_crypto_entry(market, signal, pt_cfg, 100.0)
        assert result is not None
        _, bucket = result
        assert bucket == "crypto_data"

    @patch("src.utils.crypto_probability_engine.get_crypto_data_signal")
    def test_insurance_detection_logged(self, mock_data_signal):
        """Insurance 模式检测（高价 best_ask）"""
        from src.simulation.paper_trader import PaperTrader

        mock_data_signal.return_value = {
            "probability": 0.72,
            "direction": "YES",
            "edge": 0.22,
            "confidence": 0.60,
            "components": {},
            "threshold": {"target_price": 70000, "direction": "above"},
        }
        market = {"question": "Will Bitcoin be above $70,000?", "volume24hr": 50000}
        signal = self._make_signal(
            outcome="YES", expected_price=0.92
        )  # 高价 → insurance
        pt_cfg = {
            "crypto": {"max_size_usdc": 5.0, "min_data_edge": 0.06, "insurance_min_price": 0.88},
            "dynamic_sizing": {"enabled": False},
        }

        # 不崩溃即可，insurance 只是 logging
        result = PaperTrader._evaluate_crypto_entry(market, signal, pt_cfg, 100.0)
        assert result is not None
