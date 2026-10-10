"""
test_evolution.py — 自我进化系统单元测试
覆盖：CalibrationCurve / Bayesian更新 / StrategyEvolver / DB CRUD
"""

import math
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from src.evolution.strategy_evolver import (
    BIN_WIDTH,
    NUM_BINS,
    CalibrationBin,
    CalibrationCurve,
    StrategyEvolver,
)
from src.utils.db import (
    connect,
    get_calibration_bins,
    get_evolution_history,
    get_model_stats,
    init_db,
    insert_calibration_point,
    insert_evolution_log,
    insert_model_performance,
)


@pytest.fixture
def tmp_db(tmp_path):
    """创建临时 SQLite 数据库。"""
    db_path = tmp_path / "test_evolution.db"
    init_db(db_path)
    return db_path


# ====================================================================
# CalibrationBin
# ====================================================================


class TestCalibrationBin:
    def test_calibrated_rate_no_data(self):
        b = CalibrationBin(bin_index=5, total=0, wins=0, midpoint=0.55)
        assert b.calibrated_rate == 0.55  # 无数据→返回桶中点

    def test_calibrated_rate_laplace(self):
        b = CalibrationBin(bin_index=7, total=10, wins=8, midpoint=0.75)
        # (8+1)/(10+2) = 0.75
        assert abs(b.calibrated_rate - 0.75) < 1e-6

    def test_calibrated_rate_all_wins(self):
        b = CalibrationBin(bin_index=9, total=20, wins=20, midpoint=0.95)
        rate = b.calibrated_rate  # (20+1)/(20+2) ≈ 0.9545
        assert 0.90 < rate < 1.0

    def test_calibrated_rate_all_losses(self):
        b = CalibrationBin(bin_index=0, total=20, wins=0, midpoint=0.05)
        rate = b.calibrated_rate  # (0+1)/(20+2) ≈ 0.0455
        assert 0.0 < rate < 0.10


# ====================================================================
# CalibrationCurve
# ====================================================================


class TestCalibrationCurve:
    @staticmethod
    def _identity_curve() -> CalibrationCurve:
        """完美校准曲线（每桶 calibrated=midpoint）。"""
        bins = [
            CalibrationBin(
                bin_index=i,
                total=50,
                wins=int(50 * (i + 0.5) / 10),
                midpoint=(i + 0.5) / 10,
            )
            for i in range(NUM_BINS)
        ]
        return CalibrationCurve(bins=bins, active=True, total_samples=500)

    @staticmethod
    def _overconfident_curve() -> CalibrationCurve:
        """过度自信曲线：AI说80%，实际只有65%。"""
        bins = []
        for i in range(NUM_BINS):
            mid = (i + 0.5) / 10
            # 实际胜率偏向 50%
            actual_rate = mid * 0.7 + 0.15
            wins = int(100 * actual_rate)
            bins.append(CalibrationBin(bin_index=i, total=100, wins=wins, midpoint=mid))
        return CalibrationCurve(bins=bins, active=True, total_samples=1000)

    def test_inactive_returns_raw(self):
        curve = CalibrationCurve(bins=[], active=False, total_samples=0)
        assert curve.correct(0.7) == 0.7
        assert curve.correct(0.1) == 0.1

    def test_identity_curve_close_to_input(self):
        curve = self._identity_curve()
        for i in range(NUM_BINS):
            center = (i + 0.5) / 10
            corrected = curve.correct(center)
            assert abs(corrected - center) < 0.05, f"桶{i}: {center} → {corrected}"

    def test_overconfident_shifts_down(self):
        curve = self._overconfident_curve()
        corrected = curve.correct(0.85)
        assert corrected < 0.85, f"0.85 应被校准下调，实际 {corrected}"

    def test_overconfident_shifts_up_low(self):
        curve = self._overconfident_curve()
        corrected = curve.correct(0.15)
        assert corrected > 0.15, f"0.15 应被校准上调，实际 {corrected}"

    def test_correct_clamps(self):
        bins = [
            CalibrationBin(bin_index=i, total=10, wins=0, midpoint=(i + 0.5) / 10)
            for i in range(NUM_BINS)
        ]
        bins[0] = CalibrationBin(bin_index=0, total=10, wins=0, midpoint=0.05)  # 极低率
        curve = CalibrationCurve(bins=bins, active=True, total_samples=100)
        low = curve.correct(0.01)
        assert low >= 0.01

    def test_bias_direction_overconfident(self):
        curve = self._overconfident_curve()
        # 过度自信 = 实际低于预期 = 负差值
        assert curve.bias_direction() in ("OVERCONFIDENT", "BALANCED")

    def test_bias_direction_unknown(self):
        curve = CalibrationCurve(bins=[], active=False)
        assert curve.bias_direction() == "UNKNOWN"

    def test_summary_inactive(self):
        curve = CalibrationCurve(bins=[], active=False, total_samples=5)
        s = curve.summary()
        assert "未激活" in s
        assert "5" in s

    def test_summary_active(self):
        curve = self._identity_curve()
        s = curve.summary()
        assert "500 样本" in s


# ====================================================================
# Bayesian Update
# ====================================================================


class TestBayesianUpdate:
    def test_no_assessments_returns_prior(self):
        post, lr, ag = StrategyEvolver.bayesian_update(0.5, [])
        assert abs(post - 0.5) < 0.01
        assert abs(lr - 1.0) < 1e-6

    def test_single_model_favors_yes(self):
        """一个模型认为 60%，市场 50% → posterior 偏向 YES。"""
        post, lr, ag = StrategyEvolver.bayesian_update(0.5, [(0.7, 1.0)])
        assert post > 0.5
        assert lr > 1.0

    def test_single_model_favors_no(self):
        post, lr, ag = StrategyEvolver.bayesian_update(0.5, [(0.3, 1.0)])
        assert post < 0.5
        assert lr < 1.0

    def test_two_models_agree(self):
        post, lr, ag = StrategyEvolver.bayesian_update(0.5, [(0.7, 1.0), (0.8, 1.0)])
        assert post > 0.6  # 双模型同意→更强信号
        assert ag > 0.7  # 高一致性

    def test_two_models_disagree(self):
        post, lr, ag = StrategyEvolver.bayesian_update(0.5, [(0.8, 0.9), (0.2, 0.9)])
        assert ag < 0.5  # 低一致性

    def test_low_confidence_dampens(self):
        """低置信度的模型影响应该更小。"""
        full = StrategyEvolver.bayesian_update(0.5, [(0.8, 1.0)])
        half = StrategyEvolver.bayesian_update(0.5, [(0.8, 0.3)])
        assert abs(full[0] - 0.5) > abs(half[0] - 0.5), "低置信度应有更小影响"

    def test_posterior_clamped(self):
        """极端LR不应把posterior推到0或1。"""
        post, _, _ = StrategyEvolver.bayesian_update(0.99, [(0.999, 1.0)])
        assert 0.01 <= post <= 0.99

    def test_market_prior_anchoring(self):
        """市场先验为0.8时，相同AI概率变化应产生较小的后验移动。"""
        post_50, _, _ = StrategyEvolver.bayesian_update(0.5, [(0.7, 1.0)])
        post_80, _, _ = StrategyEvolver.bayesian_update(0.8, [(0.9, 1.0)])
        move_50 = post_50 - 0.5
        move_80 = post_80 - 0.8
        # 0.8 先验的移动幅度应更小（已经偏向一侧）
        assert move_80 < move_50


# ====================================================================
# StrategyEvolver
# ====================================================================


class TestStrategyEvolver:
    def test_init(self, tmp_db):
        evolver = StrategyEvolver(db_path=tmp_db)
        assert not evolver.global_calibration.active
        assert evolver.runtime_overrides == {}

    def test_record_outcome(self, tmp_db):
        evolver = StrategyEvolver(db_path=tmp_db)
        evolver.record_outcome(
            category="weather",
            ai_probability=0.72,
            actual_outcome=1,
            model_votes={"model_a": {"probability": 0.75, "outcome": "YES"}},
        )
        assert evolver._bets_since_last_evolve == 1

    def test_record_outcome_no_direction(self, tmp_db):
        """NO方向也能正确记录。"""
        evolver = StrategyEvolver(db_path=tmp_db)
        evolver.record_outcome(
            category="crypto",
            ai_probability=0.3,
            actual_outcome=0,
            model_votes={"model_b": {"probability": 0.7, "outcome": "NO"}},
        )
        assert evolver._bets_since_last_evolve == 1

    @patch("src.evolution.strategy_evolver.get")
    def test_should_evolve_disabled(self, mock_get, tmp_db):
        mock_get.return_value = {"enabled": False}
        evolver = StrategyEvolver(db_path=tmp_db)
        assert not evolver.should_evolve()

    @patch("src.evolution.strategy_evolver.get")
    def test_should_evolve_not_enough_data(self, mock_get, tmp_db):
        mock_get.return_value = {
            "enabled": True,
            "min_samples": 100,
            "interval_bets": 50,
            "interval_hours": 24,
        }
        evolver = StrategyEvolver(db_path=tmp_db)
        assert not evolver.should_evolve()

    def test_get_calibrated_probability_inactive(self, tmp_db):
        evolver = StrategyEvolver(db_path=tmp_db)
        assert evolver.get_calibrated_probability(0.7, "weather") == 0.7

    def test_get_model_weight_default(self, tmp_db):
        evolver = StrategyEvolver(db_path=tmp_db)
        assert evolver.get_model_weight("nonexistent_model") == 1.0

    def test_get_override_default(self, tmp_db):
        evolver = StrategyEvolver(db_path=tmp_db)
        assert evolver.get_override("nonexistent.key") is None
        assert evolver.get_override("nonexistent.key", 0.5) == 0.5

    def test_get_status(self, tmp_db):
        evolver = StrategyEvolver(db_path=tmp_db)
        status = evolver.get_status()
        assert "enabled" in status
        assert "global_calibration_active" in status
        assert "model_weights" in status
        assert "runtime_overrides" in status

    def test_bounded_adjust_within_limits(self):
        # 小调整：在上下限内
        result = StrategyEvolver._bounded_adjust(0.75, 0.05, (0.4, 0.95))
        assert 0.4 <= result <= 0.95
        assert abs(result - 0.8) < 0.01

    def test_bounded_adjust_max_change(self):
        # 大调整被限制到 20%
        result = StrategyEvolver._bounded_adjust(0.75, 0.50, (0.4, 0.95))
        max_allowed = 0.75 * 0.20  # 0.15
        assert result <= 0.75 + max_allowed + 0.001

    def test_bounded_adjust_respects_bounds(self):
        result = StrategyEvolver._bounded_adjust(0.94, 0.10, (0.4, 0.95))
        assert result <= 0.95

    def test_evolve_with_data(self, tmp_db):
        """喂入足够数据后进化应产生结果（不崩溃）。"""
        evolver = StrategyEvolver(db_path=tmp_db)
        # 模拟50笔过度自信的投注
        for i in range(50):
            insert_calibration_point(
                category="weather",
                bin_index=7,  # 70-80% 桶
                ai_probability=0.75,
                actual_outcome=0 if i % 3 == 0 else 1,  # 实际33%输
                db_path=tmp_db,
            )
            insert_model_performance(
                model_name="test_model",
                category="weather",
                predicted_prob=0.75,
                actual_outcome=0 if i % 3 == 0 else 1,
                db_path=tmp_db,
            )
        evolver._load_calibration()
        evolver._load_model_weights()
        assert evolver.model_weights.get("test_model") is not None


# ====================================================================
# DB CRUD (evolution tables)
# ====================================================================


class TestEvolutionDB:
    def test_insert_calibration_point(self, tmp_db):
        insert_calibration_point("weather", 5, 0.55, 1, "model_a", db_path=tmp_db)
        bins = get_calibration_bins(category="weather", min_samples=1, db_path=tmp_db)
        assert len(bins) == 10
        # 桶5应该有1条记录
        assert bins[5]["total"] == 1
        assert bins[5]["wins"] == 1

    def test_insert_model_performance(self, tmp_db):
        insert_model_performance("model_a", "weather", 0.7, 1, db_path=tmp_db)
        insert_model_performance("model_a", "weather", 0.6, 0, db_path=tmp_db)
        stats = get_model_stats(recent_n=100, db_path=tmp_db)
        assert "model_a" in stats
        assert stats["model_a"]["total"] == 2
        assert 0 < stats["model_a"]["weight"] <= 1.0

    def test_insert_evolution_log(self, tmp_db):
        insert_evolution_log(
            "shrinkage_factor", 0.75, 0.72, "过度自信调整", 50, 0.8, db_path=tmp_db
        )
        history = get_evolution_history(limit=10, db_path=tmp_db)
        assert len(history) == 1
        assert history[0]["param_name"] == "shrinkage_factor"
        assert abs(history[0]["old_value"] - 0.75) < 1e-6

    def test_calibration_bins_empty(self, tmp_db):
        bins = get_calibration_bins(category="", min_samples=5, db_path=tmp_db)
        assert len(bins) == 10
        for b in bins:
            assert b["total"] == 0

    def test_calibration_bins_laplace_smoothing(self, tmp_db):
        # 向桶3插入10个样本（7赢3输）
        for _ in range(7):
            insert_calibration_point("crypto", 3, 0.35, 1, db_path=tmp_db)
        for _ in range(3):
            insert_calibration_point("crypto", 3, 0.32, 0, db_path=tmp_db)

        bins = get_calibration_bins(category="crypto", min_samples=5, db_path=tmp_db)
        # Laplace: (7+1)/(10+2) ≈ 0.6667
        assert abs(bins[3]["calibrated_rate"] - 0.6667) < 0.01

    def test_model_stats_weight(self, tmp_db):
        """MAE越高→权重越低。"""
        for _ in range(10):
            insert_model_performance("good_model", "weather", 0.7, 1, db_path=tmp_db)
            insert_model_performance("bad_model", "weather", 0.7, 0, db_path=tmp_db)
        stats = get_model_stats(recent_n=100, db_path=tmp_db)
        assert stats["good_model"]["weight"] > stats["bad_model"]["weight"]
