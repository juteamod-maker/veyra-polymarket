"""
test_residual_analyzer.py — 标准化残差分析器测试
"""

import math
from unittest.mock import patch

import pytest
from src.evolution.strategy_evolver import ResidualGroup, StandardizedResidualAnalyzer


class TestResidualGroup:
    def test_mean_empty(self):
        g = ResidualGroup(key="test")
        assert g.mean == 0
        assert g.std == 0
        assert g.z_score == 0

    def test_mean_positive(self):
        g = ResidualGroup(key="test", count=10, sum_residual=1.0, sum_sq_residual=0.2)
        assert g.mean == pytest.approx(0.1)

    def test_z_score_needs_min_samples(self):
        g = ResidualGroup(key="test", count=3, sum_residual=-0.9, sum_sq_residual=0.3)
        assert g.z_score == 0  # < 5 samples

    def test_z_score_strongly_negative(self):
        # 10 bets, all losing 50% → mean=-0.5, consistent
        g = ResidualGroup(key="test", count=10, sum_residual=-5.0, sum_sq_residual=2.55)
        assert g.z_score < -1.5  # Should be flagged


class TestStandardizedResidualAnalyzer:
    @patch("src.evolution.strategy_evolver.db")
    def test_empty_data(self, mock_db):
        mock_db.get_resolved_for_calibration.return_value = []
        analyzer = StandardizedResidualAnalyzer()
        result = analyzer.analyze()
        assert result["total_bets"] == 0
        assert result["alerts"] == []

    @patch("src.evolution.strategy_evolver.db")
    def test_detects_losing_category(self, mock_db):
        # Weather bets mostly losing (varied amounts for non-zero std)
        bets = []
        for i in range(20):
            pnl = -0.3 - (i % 5) * 0.1  # -0.3 to -0.7, avg ~-0.5
            bets.append(
                {
                    "pnl": pnl,
                    "size_usdc": 1.0,
                    "category": "weather",
                    "market_price": 0.15,
                    "ev": 0.08,
                    "exit_type": "resolution",
                    "is_win": 0,
                    "resolved_outcome": "NO",
                }
            )
        mock_db.get_resolved_for_calibration.return_value = bets
        analyzer = StandardizedResidualAnalyzer()
        result = analyzer.analyze()
        assert result["total_bets"] == 20
        # Should have alerts for weather category
        cat_alerts = [a for a in result["alerts"] if a["dimension"] == "category"]
        assert len(cat_alerts) >= 1
        assert cat_alerts[0]["key"] == "weather"
        assert cat_alerts[0]["z_score"] < -1.5

    @patch("src.evolution.strategy_evolver.db")
    def test_no_alert_for_profitable_group(self, mock_db):
        # Mixed results, slightly positive
        bets = []
        for i in range(30):
            pnl = 0.3 if i % 3 == 0 else -0.1
            bets.append(
                {
                    "pnl": pnl,
                    "size_usdc": 1.0,
                    "category": "crypto",
                    "market_price": 0.50,
                    "ev": 0.10,
                    "exit_type": "resolution",
                    "is_win": 1 if pnl > 0 else 0,
                    "resolved_outcome": "YES" if pnl > 0 else "NO",
                }
            )
        mock_db.get_resolved_for_calibration.return_value = bets
        analyzer = StandardizedResidualAnalyzer()
        result = analyzer.analyze()
        # Slightly profitable group should not trigger alert
        crypto_alerts = [
            a
            for a in result["alerts"]
            if a["dimension"] == "category" and a["key"] == "crypto"
        ]
        assert len(crypto_alerts) == 0

    def test_price_bucket(self):
        assert StandardizedResidualAnalyzer._price_bucket(0.10) == "0.00-0.20"
        assert StandardizedResidualAnalyzer._price_bucket(0.35) == "0.20-0.40"
        assert StandardizedResidualAnalyzer._price_bucket(0.90) == "0.80-1.00"

    def test_ev_bucket(self):
        assert StandardizedResidualAnalyzer._ev_bucket(0.03) == "0.00-0.05"
        assert StandardizedResidualAnalyzer._ev_bucket(0.07) == "0.05-0.10"
        assert StandardizedResidualAnalyzer._ev_bucket(0.25) == "0.20+"
        assert StandardizedResidualAnalyzer._ev_bucket(0.25) == "0.20+"
