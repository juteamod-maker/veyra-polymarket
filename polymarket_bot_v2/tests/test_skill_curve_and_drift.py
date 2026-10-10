"""
test_skill_curve_and_drift.py — Tests for Phase 52 changes:
  1. GFS CRPS-based skill score curve (weather_fetcher._gfs_skill_factor)
  2. Sparkline drift estimation (crypto_probability_engine._calc_sparkline_drift)
  3. Coinbase adapter success logging
"""

import math
from unittest.mock import MagicMock, patch

import pytest

# ===========================================================================
# 1. GFS Skill Score Curve Tests
# ===========================================================================


class TestGfsSkillFactor:
    """Test _gfs_skill_factor returns correct empirical skill values."""

    def test_known_days(self):
        from src.utils.weather_fetcher import _gfs_skill_factor

        assert _gfs_skill_factor(0) == pytest.approx(0.98)
        assert _gfs_skill_factor(1) == pytest.approx(0.95)
        assert _gfs_skill_factor(2) == pytest.approx(0.90)
        assert _gfs_skill_factor(3) == pytest.approx(0.82)
        assert _gfs_skill_factor(4) == pytest.approx(0.72)
        assert _gfs_skill_factor(5) == pytest.approx(0.60)
        assert _gfs_skill_factor(6) == pytest.approx(0.50)
        assert _gfs_skill_factor(7) == pytest.approx(0.42)

    def test_beyond_max_returns_last(self):
        from src.utils.weather_fetcher import _gfs_skill_factor

        assert _gfs_skill_factor(10) == pytest.approx(0.42)
        assert _gfs_skill_factor(14) == pytest.approx(0.42)

    def test_below_min_returns_first(self):
        from src.utils.weather_fetcher import _gfs_skill_factor

        # days_ahead should never be negative in practice, but test boundary
        assert _gfs_skill_factor(-1) == pytest.approx(0.98)

    def test_monotonically_decreasing(self):
        from src.utils.weather_fetcher import _gfs_skill_factor

        for d in range(7):
            assert _gfs_skill_factor(d) > _gfs_skill_factor(d + 1)

    @patch("src.utils.weather_fetcher.get")
    def test_custom_config_curve(self, mock_get):
        """Config can override the default skill curve."""
        mock_get.return_value = {"skill_score_curve": {"0": 0.99, "3": 0.85, "7": 0.50}}
        from src.utils.weather_fetcher import _gfs_skill_factor

        # Day 0 and 7 should use config values
        result_0 = _gfs_skill_factor(0)
        assert result_0 == pytest.approx(0.99)
        result_7 = _gfs_skill_factor(7)
        assert result_7 == pytest.approx(0.50)


class TestSkillCurveInConfidence:
    """Test that _calc_member_probability uses skill curve for confidence."""

    def test_confidence_decays_faster_at_day_5(self):
        """Day 5 confidence should be notably lower than old linear model."""
        from src.utils.weather_fetcher import _calc_member_probability

        # 25/31 members agree (high consensus ~0.81)
        values = [30.0] * 25 + [20.0] * 6
        parsed = {
            "direction": "gte",
            "threshold_value": 25,
            "unit": "C",
        }
        result_d1 = _calc_member_probability(values, parsed, days_ahead=1)
        result_d5 = _calc_member_probability(values, parsed, days_ahead=5)

        # Day 5 confidence should be meaningfully lower
        assert result_d1["confidence"] > result_d5["confidence"]
        # Old linear: day 5 horizon_factor = max(0.7, 1-0.15) = 0.85
        # New curve: day 5 = 0.60 → much lower confidence
        assert result_d5["confidence"] < 0.55

    def test_day_0_near_max_confidence(self):
        from src.utils.weather_fetcher import _calc_member_probability

        # All members agree → consensus = 1.0
        values = [30.0] * 31
        parsed = {
            "direction": "gte",
            "threshold_value": 25,
            "unit": "C",
        }
        result = _calc_member_probability(values, parsed, days_ahead=0)
        # 1.0 * 0.98 * 0.95 = 0.931
        assert result["confidence"] == pytest.approx(0.931)


# ===========================================================================
# 2. Sparkline Drift Estimation Tests
# ===========================================================================


class TestCalcSparklineDrift:
    """Test _calc_sparkline_drift log-return regression."""

    def test_flat_sparkline_zero_drift(self):
        from src.utils.crypto_probability_engine import _calc_sparkline_drift

        flat = [100.0] * 168
        drift = _calc_sparkline_drift(flat)
        assert abs(drift) < 0.01

    def test_uptrend_positive_drift(self):
        from src.utils.crypto_probability_engine import _calc_sparkline_drift

        # Gentle uptrend: 100 → ~110 over 168 hours
        up = [100.0 + 0.06 * i for i in range(168)]
        drift = _calc_sparkline_drift(up)
        assert drift > 0

    def test_downtrend_negative_drift(self):
        from src.utils.crypto_probability_engine import _calc_sparkline_drift

        # Gentle downtrend: 100 → ~90 over 168 hours
        down = [100.0 - 0.06 * i for i in range(168)]
        drift = _calc_sparkline_drift(down)
        assert drift < 0

    def test_short_sparkline_works(self):
        from src.utils.crypto_probability_engine import _calc_sparkline_drift

        short = [100.0 + i for i in range(48)]
        drift = _calc_sparkline_drift(short)
        assert drift > 0  # uptrend

    def test_too_short_returns_zero(self):
        from src.utils.crypto_probability_engine import _calc_sparkline_drift

        assert _calc_sparkline_drift([100.0]) == 0.0
        assert _calc_sparkline_drift([]) == 0.0

    def test_drift_clipped_in_calculate_threshold(self):
        """Even extreme sparkline drift gets clipped to [-2.0, 2.0]."""
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        # Extreme price jump sparkline
        extreme = [100.0] * 84 + [200.0] * 84
        result = calculate_threshold_probability(
            current_price=200.0,
            target_price=250.0,
            volatility_7d_pct=50.0,
            change_24h_pct=100.0,
            time_to_settlement_days=7,
            direction="above",
            sparkline=extreme,
        )
        assert result["components"]["mu_annual"] <= 2.0
        assert result["components"]["mu_annual"] >= -2.0


class TestDriftFallback:
    """Test that calculate_threshold_probability falls back properly."""

    def test_uses_24h_when_sparkline_short(self):
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        # Only 20 points → should fall back to 24h change
        result = calculate_threshold_probability(
            current_price=100.0,
            target_price=110.0,
            volatility_7d_pct=10.0,
            change_24h_pct=5.0,
            time_to_settlement_days=7,
            direction="above",
            sparkline=[100.0] * 20,
        )
        # With 24h change of 5%, mu_annual = 0.05 * 365 = 18.25, clipped to 2.0
        assert result["components"]["mu_annual"] == pytest.approx(2.0)

    def test_uses_sparkline_when_sufficient(self):
        from src.utils.crypto_probability_engine import calculate_threshold_probability

        # Flat sparkline → drift ≈ 0, regardless of change_24h_pct
        result = calculate_threshold_probability(
            current_price=100.0,
            target_price=110.0,
            volatility_7d_pct=10.0,
            change_24h_pct=50.0,  # 50% 24h change
            time_to_settlement_days=7,
            direction="above",
            sparkline=[100.0] * 168,  # flat
        )
        # Sparkline drift ≈ 0 should override the 50% 24h change
        assert abs(result["components"]["mu_annual"]) < 0.1
