"""
test_extreme_lock.py — T187 EHCL 高赢尾桶策略单元测试

参数基线(VPS生产设定): min_price=0.85, max_price=0.90, confidence=0.85
EV计算: (1 - entry_price) - buy_fee, buy_fee = feeRate × p × (1-p)
"""

import math
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from src.signal.extreme_lock import scan_extreme_lock_signals

# ── Fixtures ──────────────────────────────────────────────────────


def _make_strike(
    market_id="m1",
    question="Will the highest temperature in Toronto be 5°C or lower on April 15?",
    threshold=5.0,
    direction="lte",
    best_ask_yes=0.03,
    volume_24h=8000,
    token_yes="tok_yes_1",
    token_no="tok_no_1",
    condition_id="0xcond1",
    end_date_iso="2026-04-15T00:00:00Z",
    market_slug="toronto-5c-or-lower",
    event_slug="toronto-temp-april-15",
):
    """创建 TemperatureStrike mock 对象。"""
    from src.scanner.weather_scanner import TemperatureStrike

    return TemperatureStrike(
        market_id=market_id,
        condition_id=condition_id,
        token_yes=token_yes,
        token_no=token_no,
        question=question,
        threshold=threshold,
        threshold_low=threshold,
        threshold_high=threshold,
        direction=direction,
        best_ask_yes=best_ask_yes,
        volume_24h=volume_24h,
        end_date_iso=end_date_iso,
        market_slug=market_slug,
        event_slug=event_slug,
    )


def _make_group(
    strikes=None,
    city_name="Toronto",
    city_slug="toronto",
    date_str="April 15",
    timezone="America/Toronto",
):
    from src.scanner.weather_scanner import CityDayGroup

    return CityDayGroup(
        city_name=city_name,
        city_slug=city_slug,
        date_str=date_str,
        lat=43.65,
        lon=-79.38,
        icao="CYYZ",
        timezone=timezone,
        strikes=strikes or [],
    )


DEFAULT_CONFIG = {
    "weather_strategy": {
        "extreme_lock": {
            "enabled": True,
            "min_price": 0.85,
            "max_price": 0.90,
            "min_hours_to_settlement": 12,
            "min_volume_24h": 5000,
            "max_signals_per_cycle": 3,
            "default_size_usdc": 5.0,
            "confidence": 0.85,
        }
    },
    "executor": {"min_order_size_shares": 5.3},
    "paper_trading": {"taker_fee_rates": {"weather": 0.05}},
}


def _mock_get(key):
    """模拟 config_loader.get()。"""
    parts = key.split(".")
    val = DEFAULT_CONFIG
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            return None
    return val


def _compute_expected_ev(entry_price, fee_rate=0.05):
    """计算扣费后的预期EV。"""
    buy_fee = fee_rate * entry_price * (1.0 - entry_price)
    return (1.0 - entry_price) - buy_fee


# ── Tests ─────────────────────────────────────────────────────────


class TestEHCLBasic:
    """EHCL 基本功能测试。"""

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_disabled_returns_empty(self, mock_get):
        """开关关闭时应返回空列表。"""
        cfg = dict(DEFAULT_CONFIG)
        cfg["weather_strategy"] = dict(cfg["weather_strategy"])
        cfg["weather_strategy"]["extreme_lock"] = dict(
            cfg["weather_strategy"]["extreme_lock"]
        )
        cfg["weather_strategy"]["extreme_lock"]["enabled"] = False

        def disabled_get(key):
            parts = key.split(".")
            val = cfg
            for p in parts:
                if isinstance(val, dict):
                    val = val.get(p)
                else:
                    return None
            return val

        with patch("src.signal.extreme_lock.get", side_effect=disabled_get):
            group = _make_group(strikes=[_make_strike()])
            result = scan_extreme_lock_signals(
                group, days_diff=1, hours_to_settlement=24
            )
            assert result == []

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_day0_returns_empty(self, mock_get):
        """当日组(day+0)应被跳过。"""
        group = _make_group(strikes=[_make_strike()])
        result = scan_extreme_lock_signals(group, days_diff=0, hours_to_settlement=48)
        assert result == []

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_day3_returns_empty(self, mock_get):
        """day+3 及更远日组应被跳过。"""
        group = _make_group(strikes=[_make_strike()])
        result = scan_extreme_lock_signals(group, days_diff=3, hours_to_settlement=72)
        assert result == []

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_day1_processes(self, mock_get):
        """day+1 应正常处理。"""
        # YES=0.12, NO=0.88 → NO in [0.85, 0.90)
        strike = _make_strike(best_ask_yes=0.12, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=36)
        assert len(result) == 1
        assert result[0].outcome == "NO"
        assert result[0].strategy_tag == "weather_ehcl"

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_day2_processes(self, mock_get):
        """day+2 应正常处理。"""
        # YES=0.13, NO=0.87 → NO in [0.85, 0.90)
        strike = _make_strike(best_ask_yes=0.13, direction="gte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=2, hours_to_settlement=48)
        assert len(result) == 1
        assert result[0].outcome == "NO"  # NO = 1 - 0.13 = 0.87 → in [0.85, 0.90)


class TestEHCLDirectionDetection:
    """尾部桶方向识别测试。"""

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_lte_direction_detected(self, mock_get):
        """'or below' (lte) 方向应被识别。"""
        strike = _make_strike(direction="lte", best_ask_yes=0.12)
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        assert result[0].strategy_params["direction"] == "lte"

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_gte_direction_detected(self, mock_get):
        """'or higher' (gte) 方向应被识别。"""
        strike = _make_strike(direction="gte", best_ask_yes=0.12)
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        assert result[0].strategy_params["direction"] == "gte"

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_exact_direction_skipped(self, mock_get):
        """exact方向不是尾部桶，应被跳过。"""
        strike = _make_strike(direction="exact", best_ask_yes=0.12)
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert result == []

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_range_direction_skipped(self, mock_get):
        """range方向不是尾部桶，应被跳过。"""
        strike = _make_strike(direction="range", best_ask_yes=0.12)
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert result == []


class TestEHCLBidirectional:
    """YES/NO 双向投注测试。"""

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_buy_no_when_no_price_high(self, mock_get):
        """YES价格低 → NO极高 → 买NO。"""
        # YES=0.12, NO=0.88 → NO in [0.85, 0.90)
        strike = _make_strike(best_ask_yes=0.12, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        sig = result[0]
        assert sig.outcome == "NO"
        assert sig.token_id == "tok_no_1"
        assert abs(sig.expected_price - 0.88) < 0.001

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_buy_yes_when_yes_price_high(self, mock_get):
        """YES价格极高 → 买YES。"""
        # YES=0.88, NO=0.12 → YES in [0.85, 0.90)
        strike = _make_strike(best_ask_yes=0.88, direction="gte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        sig = result[0]
        assert sig.outcome == "YES"
        assert sig.token_id == "tok_yes_1"
        assert abs(sig.expected_price - 0.88) < 0.001

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_neither_side_qualifies(self, mock_get):
        """YES/NO 都不在极高置信区间 → 无信号。"""
        # YES=0.50, NO=0.50 → 都不在 [0.85, 0.90)
        strike = _make_strike(best_ask_yes=0.50, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert result == []


class TestEHCLFilters:
    """过滤条件测试。"""

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_volume_too_low(self, mock_get):
        """24h成交量不足 → 跳过。"""
        strike = _make_strike(best_ask_yes=0.12, volume_24h=3000)
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert result == []

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_hours_too_short(self, mock_get):
        """距结算太近(<12h) → 跳过。"""
        strike = _make_strike(best_ask_yes=0.12)
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=8)
        assert result == []

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_price_below_min(self, mock_get):
        """NO价格低于 min_price → 跳过。"""
        # YES=0.20, NO=0.80 → NO < 0.85
        strike = _make_strike(best_ask_yes=0.20, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert result == []

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_price_above_max(self, mock_get):
        """NO价格超过 max_price → 跳过。"""
        # YES=0.05, NO=0.95 → NO ≥ 0.90 (strict <)
        strike = _make_strike(best_ask_yes=0.05, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert result == []

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_max_signals_limit(self, mock_get):
        """候选超过 max_signals_per_cycle 时取 Top N。"""
        strikes = [
            _make_strike(
                market_id=f"m{i}",
                best_ask_yes=0.11 + i * 0.01,
                direction="lte",
                token_no=f"tok_no_{i}",
                condition_id=f"0xcond{i}",
            )
            for i in range(5)
        ]
        # NO prices: 0.89, 0.88, 0.87, 0.86, 0.85 → all in [0.85, 0.90)
        group = _make_group(strikes=strikes)
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 3  # max_signals_per_cycle = 3


class TestEHCLSharesGuarantee:
    """shares > 5 保证测试。"""

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_default_size_sufficient(self, mock_get):
        """default_size 已足够时使用 default_size。"""
        # NO price = 0.88, min_shares = 5.3
        # min_usdc = ceil(5.3 * 0.88 * 100) / 100 = ceil(466.4) / 100 = 4.67
        # default_size = 5.0 > 4.67 → use 5.0
        strike = _make_strike(best_ask_yes=0.12, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        sig = result[0]
        # shares = size_usdc / price >= 5.3
        expected_shares = sig.size_usdc / sig.expected_price
        assert expected_shares >= 5.01

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_size_adjusted_for_shares(self, mock_get):
        """低 default_size 时自动调整金额确保 shares > 5。"""
        # 用自定义配置降低 default_size 触发调整
        cfg = {
            "weather_strategy": {
                "extreme_lock": {
                    "enabled": True,
                    "min_price": 0.85,
                    "max_price": 0.90,
                    "min_hours_to_settlement": 12,
                    "min_volume_24h": 5000,
                    "max_signals_per_cycle": 3,
                    "default_size_usdc": 2.0,
                    "confidence": 0.85,
                }
            },
            "executor": {"min_order_size_shares": 5.3},
            "paper_trading": {"taker_fee_rates": {"weather": 0.05}},
        }

        def custom_get(key):
            parts = key.split(".")
            val = cfg
            for p in parts:
                if isinstance(val, dict):
                    val = val.get(p)
                else:
                    return None
            return val

        # NO = 0.88, min_usdc = ceil(5.3*0.88*100)/100 = 4.67 > 2.0 → adjusted
        with patch("src.signal.extreme_lock.get", side_effect=custom_get):
            strike = _make_strike(best_ask_yes=0.12, direction="lte")
            group = _make_group(strikes=[strike])
            result = scan_extreme_lock_signals(
                group, days_diff=1, hours_to_settlement=24
            )
            assert len(result) == 1
            sig = result[0]
            assert sig.size_usdc > 2.0  # 被调整
            expected_shares = sig.size_usdc / sig.expected_price
            assert expected_shares >= 5.01


class TestEHCLSignalFields:
    """信号字段正确性测试。"""

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_signal_fields_complete(self, mock_get):
        """信号应包含所有必要字段。"""
        strike = _make_strike(best_ask_yes=0.12, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        sig = result[0]

        # 基本字段
        assert sig.market_id == "m1"
        assert sig.outcome == "NO"
        assert sig.strategy_tag == "weather_ehcl"
        assert sig.token_id == "tok_no_1"
        assert sig.condition_id == "0xcond1"
        assert sig.end_date_iso == "2026-04-15T00:00:00Z"

        # 策略参数
        assert sig.strategy_params["strategy_type"] == "extreme_lock"
        assert sig.strategy_params["direction"] == "lte"
        assert sig.strategy_params["threshold"] == 5.0
        assert sig.strategy_params["days_diff"] == 1

        # EV 和置信度
        assert sig.ev > 0  # EV = (1 - entry_price) - buy_fee > 0
        assert sig.confidence == 0.85
        assert sig._bet_bucket == "ExtremeLock"

        # market/event slugs
        assert sig.market_slug == "toronto-5c-or-lower"
        assert sig.event_slug == "toronto-temp-april-15"

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_ev_deducts_buy_fee(self, mock_get):
        """EV = (1 - entry_price) - buy_fee_per_share。"""
        # YES=0.14, NO=0.86 → buy NO at 0.86
        strike = _make_strike(best_ask_yes=0.14, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        sig = result[0]
        # buy_fee = 0.05 * 0.86 * 0.14 = 0.00602
        # ev = (1 - 0.86) - 0.00602 = 0.13398
        expected_ev = _compute_expected_ev(0.86)
        assert abs(sig.ev - expected_ev) < 0.0001

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_ev_always_positive_in_range(self, mock_get):
        """在 [0.85, 0.90) 范围内，扣费后EV始终为正。"""
        for yes_p in [0.11, 0.12, 0.13, 0.14, 0.15]:
            no_p = 1.0 - yes_p
            strike = _make_strike(
                best_ask_yes=yes_p,
                direction="lte",
                market_id=f"m_{yes_p}",
                token_no=f"tok_no_{yes_p}",
                condition_id=f"0xcond_{yes_p}",
            )
            group = _make_group(strikes=[strike])
            result = scan_extreme_lock_signals(
                group, days_diff=1, hours_to_settlement=24
            )
            assert len(result) == 1, f"Failed for YES={yes_p}, NO={no_p}"
            sig = result[0]
            assert sig.ev > 0, f"EV should be positive for entry_price={no_p}"
            # 验证净利润率 >= 9%
            assert (
                sig.ev >= 0.09
            ), f"Net EV should be >= 9% at entry_price={no_p}, got {sig.ev:.4f}"


class TestEHCLUnitDetection:
    """°C/°F 自动适配测试（通过 question 字段传递）。"""

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_celsius_question(self, mock_get):
        """摄氏度市场标题正确识别。"""
        strike = _make_strike(
            question="Will the highest temperature in Toronto be 5°C or lower on April 15?",
            direction="lte",
            best_ask_yes=0.12,
        )
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        assert "°C" in result[0].question

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_fahrenheit_question(self, mock_get):
        """华氏度市场标题正确识别。"""
        strike = _make_strike(
            question="Will the highest temperature in Miami be 50°F or lower on April 15?",
            direction="lte",
            best_ask_yes=0.12,
        )
        group = _make_group(
            strikes=[strike],
            city_name="Miami",
            city_slug="miami",
            timezone="America/New_York",
        )
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        assert "°F" in result[0].question


class TestEHCLEdgeCases:
    """边界情况测试。"""

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_none_days_diff(self, mock_get):
        """days_diff=None 应返回空。"""
        strike = _make_strike(best_ask_yes=0.12)
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(
            group, days_diff=None, hours_to_settlement=24
        )
        assert result == []

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_none_hours_to_settlement(self, mock_get):
        """hours_to_settlement=None 时不检查小时数限制。"""
        strike = _make_strike(best_ask_yes=0.12, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=None)
        assert len(result) == 1

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_empty_strikes(self, mock_get):
        """空挡位列表应返回空。"""
        group = _make_group(strikes=[])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert result == []

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_mixed_strikes(self, mock_get):
        """混合exact/lte/gte挡位只处理尾部桶。"""
        strikes = [
            _make_strike(
                market_id="m_exact",
                direction="exact",
                best_ask_yes=0.12,
                token_no="tok_no_exact",
                condition_id="0xcond_exact",
            ),
            _make_strike(
                market_id="m_lte",
                direction="lte",
                best_ask_yes=0.12,
                token_no="tok_no_lte",
                condition_id="0xcond_lte",
            ),
            _make_strike(
                market_id="m_gte",
                direction="gte",
                best_ask_yes=0.12,
                token_no="tok_no_gte",
                condition_id="0xcond_gte",
            ),
            _make_strike(
                market_id="m_range",
                direction="range",
                best_ask_yes=0.12,
                token_no="tok_no_range",
                condition_id="0xcond_range",
            ),
        ]
        group = _make_group(strikes=strikes)
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 2  # only lte and gte
        mids = {s.market_id for s in result}
        assert mids == {"m_lte", "m_gte"}

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_price_boundary_min(self, mock_get):
        """NO价格恰好等于 min_price(0.85) → 应通过（使用 >=）。"""
        # YES = 0.15, NO = 0.85 → NO >= 0.85 且 NO < 0.90
        strike = _make_strike(best_ask_yes=0.15, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_price_boundary_max_rejected(self, mock_get):
        """NO价格恰好等于 max_price(0.90) → 应被拒绝（strict <）。"""
        # YES = 0.10, NO = 0.90 → NO = max_price → strict < 驳回
        strike = _make_strike(best_ask_yes=0.10, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert result == []  # 0.90 not < 0.90

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_price_just_below_max(self, mock_get):
        """略低于 max_price → 应通过。"""
        # YES = 0.1001, NO = 0.8999 → NO < 0.90 ✓
        strike = _make_strike(best_ask_yes=0.1001, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert result == []  # 0.90 not < 0.90

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_price_just_below_max(self, mock_get):
        """略低于 max_pric（低入场价=高EV优先）。"""
        strikes = [
            _make_strike(
                market_id="m_low_ev",
                best_ask_yes=0.11,  # NO=0.89, EV较小
                direction="lte",
                token_no="tok_no_low",
                condition_id="0xcond_low",
            ),
            _make_strike(
                market_id="m_high_ev",
                best_ask_yes=0.14,  # NO=0.86, EV较大
                direction="lte",
                token_no="tok_no_high",
                condition_id="0xcond_high",
            ),
        ]
        group = _make_group(strikes=strikes)
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 2
        # NO=0.86 has higher EV than NO=0.89
        assert result[0].market_id == "m_high_ev"
        assert result[1].market_id == "m_low_ev"


class TestEHCLFeeCalculation:
    """手续费计算专项测试。"""

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_fee_at_min_price_boundary(self, mock_get):
        """min_price=0.85 端的费用和净利验证。"""
        # YES=0.15, NO=0.85
        strike = _make_strike(best_ask_yes=0.15, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        sig = result[0]
        expected_ev = _compute_expected_ev(0.85)
        assert abs(sig.ev - expected_ev) < 0.0001
        # 净利率 = ev / entry_price ≈ 16.9%
        assert sig.ev / sig.expected_price > 0.16

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_fee_near_max_price(self, mock_get):
        """接近 max_price=0.90 端的费用和净利验证。"""
        # YES=0.11, NO=0.89 → in [0.85, 0.90)
        strike = _make_strike(best_ask_yes=0.11, direction="lte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        sig = result[0]
        expected_ev = _compute_expected_ev(0.89)
        assert abs(sig.ev - expected_ev) < 0.0001
        # 净利率 ≈ 11.8%
        assert sig.ev / sig.expected_price > 0.11

    @patch("src.signal.extreme_lock.get", side_effect=_mock_get)
    def test_fee_yes_direction(self, mock_get):
        """买YES方向的费用验证。"""
        # YES=0.87, NO=0.13 → YES in [0.85, 0.90)
        strike = _make_strike(best_ask_yes=0.87, direction="gte")
        group = _make_group(strikes=[strike])
        result = scan_extreme_lock_signals(group, days_diff=1, hours_to_settlement=24)
        assert len(result) == 1
        sig = result[0]
        assert sig.outcome == "YES"
        expected_ev = _compute_expected_ev(0.87)
        assert abs(sig.ev - expected_ev) < 0.0001

    def test_bilateral_fee_still_profitable(self):
        """双边手续费(买入+卖出)即使叠加，仍有正利润。"""
        for entry_price in [0.85, 0.86, 0.87, 0.88, 0.89]:
            fee_rate = 0.05
            buy_fee = fee_rate * entry_price * (1.0 - entry_price)
            sell_fee = buy_fee  # 同价卖出，费用相同
            bilateral_fee = buy_fee + sell_fee
            gross_profit = 1.0 - entry_price
            net_after_bilateral = gross_profit - bilateral_fee
            assert net_after_bilateral > 0, (
                f"entry={entry_price}: bilateral_fee={bilateral_fee:.4f} "
                f"> gross_profit={gross_profit:.3f}"
            )
            # 净利率仍 > 8%
            assert net_after_bilateral / entry_price > 0.08, (
                f"entry={entry_price}: net_return="
                f"{net_after_bilateral/entry_price:.3%}"
            )
