"""
test_logs_and_evolution_fix.py — Phase 38 新功能测试
覆盖：日志筛选 API、进化系统 fallback、calibration from paper_bets
"""

import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from src.utils.db import (connect, get_calibration_bins, get_model_stats,
                          init_db, insert_calibration_point, insert_paper_bet,
                          update_paper_bet_outcome)


def _insert_resolved_bet(
    db_path,
    bet_id: str,
    market_id: str,
    category: str,
    *,
    ai_prob: float = 0.7,
    is_win: int = 1,
    pnl: float = 0.5,
    ai_models: str = "model_a, model_b",
) -> None:
    """插入一条已结算的 paper_bet（先 insert 再 update outcome）。"""
    insert_paper_bet(
        {
            "bet_id": bet_id,
            "ts_entered": time.time() - 3600,
            "market_id": market_id,
            "question": "Test?",
            "category": category,
            "outcome": "YES",
            "confidence": 0.8,
            "ai_probability": ai_prob,
            "market_price": 0.55,
            "ev": ai_prob - 0.55,
            "size_usdc": 1.0,
            "sim_balance_before": 100,
            "sim_balance_after": 99,
            "market_end_date": "2025-12-31",
            "weather_context": "",
            "ai_models": ai_models,
            "cycle_id": 1,
        },
        db_path=db_path,
    )
    update_paper_bet_outcome(
        bet_id=bet_id,
        resolved_outcome="YES",
        resolved_at=time.time() - 60,
        pnl=pnl,
        payout=1.0 + pnl,
        is_win=is_win,
        db_path=db_path,
    )


@pytest.fixture
def tmp_db(tmp_path):
    """创建临时 SQLite 数据库。"""
    db_path = tmp_path / "test_phase38.db"
    init_db(db_path)
    return db_path


# ====================================================================
# 进化系统 fallback 测试
# ====================================================================


class TestCalibrationFallback:
    """calibration_data 为空时从 paper_bets 直接计算。"""

    def test_empty_calibration_returns_10_bins(self, tmp_db):
        """空库仍返回10个桶（midpoint 值）。"""
        bins = get_calibration_bins(category="", min_samples=1, db_path=tmp_db)
        assert len(bins) == 10
        # 所有桶 total=0
        assert all(b["total"] == 0 for b in bins)

    def test_calibration_data_takes_priority(self, tmp_db):
        """有 calibration_data 数据时优先使用。"""
        # 插入 calibration 数据
        for _ in range(5):
            insert_calibration_point(
                category="weather",
                bin_index=3,
                ai_probability=0.35,
                actual_outcome=1,
                db_path=tmp_db,
            )
        bins = get_calibration_bins(category="", min_samples=1, db_path=tmp_db)
        bin3 = next(b for b in bins if b["bin_index"] == 3)
        assert bin3["total"] == 5
        assert bin3["wins"] == 5

    def test_fallback_to_paper_bets(self, tmp_db):
        """calibration_data 为空但 paper_bets 有结算数据时 fallback。"""
        # 插入已结算投注
        for i in range(5):
            _insert_resolved_bet(
                tmp_db,
                f"test_fallback_{i}",
                f"market_{i}",
                "weather",
                ai_prob=0.65,
                is_win=1,
                pnl=0.82,
            )

        # calibration_data 仍然为空
        with connect(tmp_db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) as c FROM calibration_data"
            ).fetchone()["c"]
        assert count == 0

        # get_calibration_bins 应该 fallback 到 paper_bets
        bins = get_calibration_bins(category="", min_samples=1, db_path=tmp_db)
        total = sum(b["total"] for b in bins)
        assert total == 5  # 5个已结算投注

    def test_fallback_filters_by_category(self, tmp_db):
        """fallback 也支持赛道过滤。"""
        for i, cat in enumerate(["weather", "weather", "crypto"]):
            _insert_resolved_bet(
                tmp_db,
                f"cat_test_{i}",
                f"market_cat_{i}",
                cat,
                ai_prob=0.75,
                is_win=1,
                pnl=0.5,
            )

        # 全局: 3条
        bins_all = get_calibration_bins(category="", min_samples=1, db_path=tmp_db)
        assert sum(b["total"] for b in bins_all) == 3

        # weather: 2条
        bins_w = get_calibration_bins(category="weather", min_samples=1, db_path=tmp_db)
        assert sum(b["total"] for b in bins_w) == 2


class TestModelStatsFallback:
    """model_performance 为空时从 paper_bets 聚合。"""

    def test_empty_returns_empty(self, tmp_db):
        """无数据时返回空字典。"""
        stats = get_model_stats(recent_n=100, db_path=tmp_db)
        assert stats == {}

    def test_fallback_from_paper_bets(self, tmp_db):
        """model_performance 为空时从 paper_bets 聚合模型统计。"""
        for i in range(3):
            _insert_resolved_bet(
                tmp_db,
                f"model_test_{i}",
                f"market_model_{i}",
                "weather",
                ai_prob=0.7,
                is_win=1,
                pnl=0.5,
                ai_models="gemini-2.0-flash-lite, qwen3-235b",
            )

        stats = get_model_stats(recent_n=100, db_path=tmp_db)
        assert "gemini-2.0-flash-lite" in stats
        assert "qwen3-235b" in stats
        assert stats["gemini-2.0-flash-lite"]["total"] == 3
        assert stats["qwen3-235b"]["total"] == 3
        # 全赢: mae 应该较小 (0.7 predicted vs 1 actual → error=0.3)
        assert stats["gemini-2.0-flash-lite"]["mae"] == pytest.approx(0.3, abs=0.01)


# ====================================================================
# 日志筛选 API（单元级）
# ====================================================================


class TestLogTypePatterns:
    """验证日志类型筛选模式。"""

    def test_import_log_patterns(self):
        """_LOG_TYPE_PATTERNS 应包含所有预定义类型（T295-C 后支持超集）。"""
        from scripts.dashboard_routes.data import _LOG_TYPE_PATTERNS

        # T295-C: 新增 trades/sports/system/errors 四个类型，旧类型向后兼容
        required_types = {
            "trading",
            "signal",
            "scanner",
            "risk",
            "evolution",
            "weather",
            "error",
            "warning",
            "executor",
            "position",
            "reconcile",
        }
        assert required_types.issubset(
            set(_LOG_TYPE_PATTERNS.keys())
        ), f"缺少旧类型: {required_types - set(_LOG_TYPE_PATTERNS.keys())}"

    def test_trading_patterns_match(self):
        """交易日志行应被 trading 或 trades 类型匹配（T295-C 兼容）。"""
        from scripts.dashboard_routes.data import _LOG_TYPE_PATTERNS

        # T295-C: order_executor 归入 trades；trading 保留向后兼容但范围缩小
        trades_patterns = _LOG_TYPE_PATTERNS["trades"]
        lines = [
            "[2025-03-18 15:30:00] INFO paper_trader  PaperBet placed",
            "[2025-03-18 15:30:00] INFO outcome_tracker  ProfitTake triggered",
            "[2025-03-18 15:30:00] INFO order_executor  Order submitted",
        ]
        for line in lines:
            assert any(p in line for p in trades_patterns), f"No match for: {line}"

    def test_signal_patterns_match(self):
        """信号日志行应被 signal 类型匹配。"""
        from scripts.dashboard_routes.data import _LOG_TYPE_PATTERNS

        patterns = _LOG_TYPE_PATTERNS["signal"]
        line = "[2025-03-18 15:30:00] INFO signal_engine  AI信号: YES 0.72"
        assert any(p in line for p in patterns)

    def test_weather_patterns_match(self):
        """天气日志行应被 weather 类型匹配。"""
        from scripts.dashboard_routes.data import _LOG_TYPE_PATTERNS

        patterns = _LOG_TYPE_PATTERNS["weather"]
        lines = [
            "[2025-03-18] INFO weather_fetcher  Fetching from Open-Meteo",
            "[2025-03-18] INFO src.utils.weather_fetcher  GFS ensemble data",
            "[2025-03-18] INFO  wttr.in response: 200",
            # T300-C: CityScanner + StrikeOpt 天气子流程
            "[2026-03-31] INFO src.scanner.market_scanner  [CityScanner] 输入 39 个已评分 strikes",
            "[2026-03-31] INFO src.scanner.market_scanner  [StrikeOpt] 事件 paris-slug: 2 strikes",
            "[2026-03-31] INFO src.scanner.market_scanner  城市NO策略 NO候选: 20°C",
        ]
        for line in lines:
            assert any(p in line for p in patterns), f"No match for: {line}"


class TestFeedEvolution:
    """验证 _feed_evolution 的 backfill 功能。"""

    @patch("scripts.run_paper_trading.db")
    def test_backfill_processes_all_resolved(self, mock_db):
        """backfill=True 时应处理所有历史结算投注。"""
        import scripts.run_paper_trading as rpt

        # Reset the set
        rpt._fed_bet_ids = set()

        mock_db.get_resolved_for_calibration.return_value = [
            {
                "bet_id": "old_bet_1",
                "resolved_at": time.time() - 86400,  # 1天前
                "category": "weather",
                "ai_probability": 0.7,
                "is_win": 1,
                "outcome": "YES",
                "ai_models": "model_a",
            },
            {
                "bet_id": "old_bet_2",
                "resolved_at": time.time() - 3600,  # 1小时前
                "category": "crypto",
                "ai_probability": 0.4,
                "is_win": 0,
                "outcome": "YES",
                "ai_models": "model_b",
            },
        ]

        mock_evolver = MagicMock()
        rpt._feed_evolution(mock_evolver, backfill=True)

        # 两条都应被处理（backfill 不受 600s 限制）
        assert mock_evolver.record_outcome.call_count == 2

    @patch("scripts.run_paper_trading.db")
    def test_non_backfill_skips_old(self, mock_db):
        """backfill=False 时跳过超过 600s 的旧投注。"""
        import scripts.run_paper_trading as rpt

        rpt._fed_bet_ids = set()

        mock_db.get_resolved_for_calibration.return_value = [
            {
                "bet_id": "recent_bet",
                "resolved_at": time.time() - 60,  # 1分钟前
                "category": "weather",
                "ai_probability": 0.7,
                "is_win": 1,
                "outcome": "YES",
                "ai_models": "model_a",
            },
            {
                "bet_id": "old_bet",
                "resolved_at": time.time() - 7200,  # 2小时前
                "category": "crypto",
                "ai_probability": 0.4,
                "is_win": 0,
                "outcome": "YES",
                "ai_models": "model_b",
            },
        ]

        mock_evolver = MagicMock()
        rpt._feed_evolution(mock_evolver, backfill=False)

        # 只处理1分钟前的，跳过2小时前的
        assert mock_evolver.record_outcome.call_count == 1

    @patch("scripts.run_paper_trading.db")
    def test_dedup_prevents_double_feed(self, mock_db):
        """同一 bet_id 不会重复喂入。"""
        import scripts.run_paper_trading as rpt

        rpt._fed_bet_ids = set()

        bet = {
            "bet_id": "dedup_test",
            "resolved_at": time.time() - 10,
            "category": "weather",
            "ai_probability": 0.7,
            "is_win": 1,
            "outcome": "YES",
            "ai_models": "model_a",
        }
        mock_db.get_resolved_for_calibration.return_value = [bet]

        mock_evolver = MagicMock()
        rpt._feed_evolution(mock_evolver, backfill=False)
        rpt._feed_evolution(mock_evolver, backfill=False)

        # 第二次调用不应再次处理
        assert mock_evolver.record_outcome.call_count == 1
        assert "dedup_test" in rpt._fed_bet_ids
