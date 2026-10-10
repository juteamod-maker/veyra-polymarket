"""
T239: 止损事件结构化日志系统 — 单元测试
覆盖: DB migration v17, log_stop_loss_event, get_stop_loss_events,
       get_stop_loss_summary, outcome_tracker 集成
"""

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── DB 层测试 ─────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path):
    """创建临时DB并执行所有迁移。"""
    db_path = tmp_path / "test.db"
    from src.utils.db import init_db

    init_db(db_path)
    return db_path


class TestStopLossEventsSchema:
    """schema v17 迁移测试"""

    def test_table_created(self, tmp_db):
        conn = sqlite3.connect(str(tmp_db))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stop_loss_events'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_indexes_created(self, tmp_db):
        conn = sqlite3.connect(str(tmp_db))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_sle_%'"
        )
        indexes = [r[0] for r in cursor.fetchall()]
        assert "idx_sle_ts" in indexes
        assert "idx_sle_cat" in indexes
        assert "idx_sle_trigger" in indexes
        assert "idx_sle_bet" in indexes
        conn.close()

    def test_schema_version_17(self, tmp_db):
        conn = sqlite3.connect(str(tmp_db))
        ver = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert ver >= 17
        conn.close()


class TestLogStopLossEvent:
    """log_stop_loss_event 写入测试"""

    def test_basic_insert(self, tmp_db):
        from src.utils.db import log_stop_loss_event

        log_stop_loss_event(
            bet_id="test123",
            question="Will it rain?",
            category="weather",
            trigger_type="standard_sl",
            entry_price=0.50,
            current_price=0.30,
            return_pct=-0.40,
            threshold_pct=0.40,
            size_usdc=2.0,
            hours_to_expiry=5.0,
            pnl=-0.80,
            conditions={"floor_triggered": False, "standard_triggered": True},
            context={"stoploss_mode": "market"},
            db_path=tmp_db,
        )

        conn = sqlite3.connect(str(tmp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM stop_loss_events WHERE bet_id='test123'"
        ).fetchone()
        assert row is not None
        assert row["category"] == "weather"
        assert row["trigger_type"] == "standard_sl"
        assert abs(row["entry_price"] - 0.50) < 0.001
        assert abs(row["current_price"] - 0.30) < 0.001
        assert abs(row["return_pct"] - (-0.40)) < 0.001
        assert abs(row["threshold_pct"] - 0.40) < 0.001
        assert abs(row["pnl"] - (-0.80)) < 0.001
        assert abs(row["hours_to_expiry"] - 5.0) < 0.1

        conditions = json.loads(row["conditions_json"])
        assert conditions["standard_triggered"] is True
        assert conditions["floor_triggered"] is False
        conn.close()

    def test_insert_without_optional_fields(self, tmp_db):
        from src.utils.db import log_stop_loss_event

        log_stop_loss_event(
            bet_id="test456",
            question="Test question",
            category="sports",
            trigger_type="floor",
            entry_price=0.70,
            current_price=0.05,
            return_pct=-0.93,
            threshold_pct=0.07,
            db_path=tmp_db,
        )

        conn = sqlite3.connect(str(tmp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM stop_loss_events WHERE bet_id='test456'"
        ).fetchone()
        assert row is not None
        assert row["hours_to_expiry"] is None
        assert row["pnl"] is None
        assert row["size_usdc"] == 0.0
        conn.close()

    def test_question_truncation(self, tmp_db):
        from src.utils.db import log_stop_loss_event

        long_question = "A" * 500
        log_stop_loss_event(
            bet_id="trunc",
            question=long_question,
            category="crypto",
            trigger_type="reversal",
            entry_price=0.50,
            current_price=0.20,
            return_pct=-0.60,
            threshold_pct=0.35,
            db_path=tmp_db,
        )

        conn = sqlite3.connect(str(tmp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT question FROM stop_loss_events WHERE bet_id='trunc'"
        ).fetchone()
        assert len(row["question"]) == 200
        conn.close()


class TestGetStopLossEvents:
    """get_stop_loss_events 查询测试"""

    def _insert_events(self, tmp_db, events):
        from src.utils.db import log_stop_loss_event

        for ev in events:
            log_stop_loss_event(db_path=tmp_db, **ev)

    def test_basic_query(self, tmp_db):
        from src.utils.db import get_stop_loss_events

        self._insert_events(
            tmp_db,
            [
                dict(
                    bet_id="a",
                    question="Q1",
                    category="weather",
                    trigger_type="standard_sl",
                    entry_price=0.5,
                    current_price=0.3,
                    return_pct=-0.4,
                    threshold_pct=0.4,
                ),
                dict(
                    bet_id="b",
                    question="Q2",
                    category="sports",
                    trigger_type="floor",
                    entry_price=0.7,
                    current_price=0.05,
                    return_pct=-0.93,
                    threshold_pct=0.07,
                ),
            ],
        )

        events = get_stop_loss_events(limit=10, db_path=tmp_db)
        assert len(events) == 2

    def test_filter_by_category(self, tmp_db):
        from src.utils.db import get_stop_loss_events

        self._insert_events(
            tmp_db,
            [
                dict(
                    bet_id="a",
                    question="Q1",
                    category="weather",
                    trigger_type="standard_sl",
                    entry_price=0.5,
                    current_price=0.3,
                    return_pct=-0.4,
                    threshold_pct=0.4,
                ),
                dict(
                    bet_id="b",
                    question="Q2",
                    category="sports",
                    trigger_type="floor",
                    entry_price=0.7,
                    current_price=0.05,
                    return_pct=-0.93,
                    threshold_pct=0.07,
                ),
            ],
        )

        events = get_stop_loss_events(category="weather", db_path=tmp_db)
        assert len(events) == 1
        assert events[0]["category"] == "weather"

    def test_filter_by_trigger_type(self, tmp_db):
        from src.utils.db import get_stop_loss_events

        self._insert_events(
            tmp_db,
            [
                dict(
                    bet_id="a",
                    question="Q1",
                    category="weather",
                    trigger_type="standard_sl",
                    entry_price=0.5,
                    current_price=0.3,
                    return_pct=-0.4,
                    threshold_pct=0.4,
                ),
                dict(
                    bet_id="b",
                    question="Q2",
                    category="sports",
                    trigger_type="floor",
                    entry_price=0.7,
                    current_price=0.05,
                    return_pct=-0.93,
                    threshold_pct=0.07,
                ),
            ],
        )

        events = get_stop_loss_events(trigger_type="floor", db_path=tmp_db)
        assert len(events) == 1
        assert events[0]["trigger_type"] == "floor"

    def test_limit(self, tmp_db):
        from src.utils.db import get_stop_loss_events

        self._insert_events(
            tmp_db,
            [
                dict(
                    bet_id=f"e{i}",
                    question=f"Q{i}",
                    category="weather",
                    trigger_type="standard_sl",
                    entry_price=0.5,
                    current_price=0.3,
                    return_pct=-0.4,
                    threshold_pct=0.4,
                )
                for i in range(10)
            ],
        )

        events = get_stop_loss_events(limit=3, db_path=tmp_db)
        assert len(events) == 3


class TestGetStopLossSummary:
    """get_stop_loss_summary 汇总测试"""

    def test_summary_structure(self, tmp_db):
        from src.utils.db import get_stop_loss_summary, log_stop_loss_event

        log_stop_loss_event(
            bet_id="s1",
            question="Q1",
            category="weather",
            trigger_type="standard_sl",
            entry_price=0.5,
            current_price=0.3,
            return_pct=-0.4,
            threshold_pct=0.4,
            pnl=-0.80,
            db_path=tmp_db,
        )
        log_stop_loss_event(
            bet_id="s2",
            question="Q2",
            category="sports",
            trigger_type="floor",
            entry_price=0.7,
            current_price=0.05,
            return_pct=-0.93,
            threshold_pct=0.07,
            pnl=-1.50,
            db_path=tmp_db,
        )

        summary = get_stop_loss_summary(days=7, db_path=tmp_db)
        assert summary["days"] == 7
        assert summary["total_events"] == 2
        assert abs(summary["total_pnl"] - (-2.30)) < 0.01
        assert len(summary["by_trigger"]) == 2
        assert len(summary["by_category"]) == 2

    def test_empty_summary(self, tmp_db):
        from src.utils.db import get_stop_loss_summary

        summary = get_stop_loss_summary(days=7, db_path=tmp_db)
        assert summary["total_events"] == 0
        assert summary["total_pnl"] is None or summary["total_pnl"] == 0


# ── Outcome Tracker 集成测试 ────────────────────────────────


class TestOutcomeTrackerStopLossLogging:
    """验证 check_stop_loss_exits 触发止损时会写入结构化日志"""

    def _make_tracker(self):
        from src.simulation.outcome_tracker import OutcomeTracker

        tracker = OutcomeTracker.__new__(OutcomeTracker)
        tracker._armed_ticks = {}
        tracker._fair_cache = {}
        tracker._price_monitor = None
        return tracker

    @patch("src.simulation.outcome_tracker.get")
    def test_stop_loss_logs_event(self, mock_get, tmp_db):
        """触发 floor 止损时应写入 stop_loss_events 表"""
        from src.utils.db import get_stop_loss_events

        # 配置
        def _cfg(key, default=None):
            configs = {
                "stop_loss": {
                    "enabled": True,
                    "floor_price": 0.07,
                    "standard_stop_pct": 0.45,
                    "weather_standard_stop_pct": 0.45,
                    "expiry_stop_hours": 8.0,
                    "expiry_stop_loss_pct": 0.25,
                    "reversal_exit_price": 0.35,
                    "sports_standard_stop_pct": 0.55,
                    "weather_edge_floor_price": 0.03,
                    "stoploss_mode": {"default": "market", "per_category": {}},
                    "liquidity_guard": {"enabled": False},
                    "early_stop": {"enabled": False},
                    "weather_time_weighted_stop": {
                        "enabled": False,
                        "force_stop_pct": 0.80,
                    },
                },
                "sports_live": {
                    "game_window_hours": 4.0,
                    "post_expiry_window_hours": 2.0,
                    "floor_price": 0.10,
                    "standard_stop_pct": 0.45,
                    "reversal_exit_price": 0.15,
                },
                "profit_taking": {"min_hold_minutes": 0},
            }
            return configs.get(key, default)

        mock_get.side_effect = _cfg

        tracker = self._make_tracker()

        # Mock _extract_sell_price 和 _fetch_market_statuses
        tracker._extract_sell_price = MagicMock(return_value=0.03)
        tracker._fetch_market_statuses = MagicMock(
            return_value={"mkt1": {"outcomePrices": '["0.03","0.97"]'}}
        )

        bet = {
            "bet_id": "test_floor",
            "market_id": "mkt1",
            "question": "Will it be sunny?",
            "category": "weather",
            "outcome": "YES",
            "market_price": 0.50,
            "ts_entered": 0,
            "market_end_date": "",
            "size_usdc": 2.0,
            "bet_bucket": "standard",
        }

        # Patch DB path for stop loss event logging
        with patch("src.utils.db._DB_PATH", tmp_db):
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
        assert stops[0]["trigger"] == "floor"

        # 验证事件已写入
        events = get_stop_loss_events(db_path=tmp_db)
        assert len(events) == 1
        ev = events[0]
        assert ev["bet_id"] == "test_floor"
        assert ev["trigger_type"] == "floor"
        assert ev["category"] == "weather"
        assert abs(ev["entry_price"] - 0.50) < 0.001
        assert abs(ev["current_price"] - 0.03) < 0.001

    @patch("src.simulation.outcome_tracker.get")
    def test_stop_loss_logging_failure_does_not_break_stop(self, mock_get):
        """即使日志写入失败，止损仍应正常触发"""

        def _cfg(key, default=None):
            configs = {
                "stop_loss": {
                    "enabled": True,
                    "floor_price": 0.07,
                    "standard_stop_pct": 0.45,
                    "weather_standard_stop_pct": 0.45,
                    "expiry_stop_hours": 8.0,
                    "expiry_stop_loss_pct": 0.25,
                    "reversal_exit_price": 0.35,
                    "sports_standard_stop_pct": 0.55,
                    "weather_edge_floor_price": 0.03,
                    "stoploss_mode": {"default": "market", "per_category": {}},
                    "liquidity_guard": {"enabled": False},
                    "early_stop": {"enabled": False},
                    "weather_time_weighted_stop": {
                        "enabled": False,
                        "force_stop_pct": 0.80,
                    },
                },
                "sports_live": {
                    "game_window_hours": 4.0,
                    "post_expiry_window_hours": 2.0,
                    "floor_price": 0.10,
                    "standard_stop_pct": 0.45,
                    "reversal_exit_price": 0.15,
                },
                "profit_taking": {"min_hold_minutes": 0},
            }
            return configs.get(key, default)

        mock_get.side_effect = _cfg

        tracker = self._make_tracker()
        tracker._extract_sell_price = MagicMock(return_value=0.03)
        tracker._fetch_market_statuses = MagicMock(
            return_value={"mkt1": {"outcomePrices": '["0.03","0.97"]'}}
        )

        bet = {
            "bet_id": "test_no_crash",
            "market_id": "mkt1",
            "question": "Test?",
            "category": "weather",
            "outcome": "YES",
            "market_price": 0.50,
            "ts_entered": 0,
            "market_end_date": "",
            "size_usdc": 2.0,
            "bet_bucket": "standard",
        }

        # 使 log_stop_loss_event 抛出异常
        with patch(
            "src.utils.db.log_stop_loss_event", side_effect=Exception("DB error")
        ):
            stops = tracker.check_stop_loss_exits([bet])

        # 止损仍应正常返回
        assert len(stops) == 1
        assert stops[0]["trigger"] == "floor"


# ── Dashboard API 测试 ──────────────────────────────────────


class TestStopLossAPI:
    """Dashboard API端点测试"""

    def test_events_endpoint_returns_list(self, tmp_db):
        from src.utils.db import log_stop_loss_event

        log_stop_loss_event(
            bet_id="api1",
            question="Q1",
            category="weather",
            trigger_type="standard_sl",
            entry_price=0.5,
            current_price=0.3,
            return_pct=-0.4,
            threshold_pct=0.4,
            db_path=tmp_db,
        )

        from src.utils.db import get_stop_loss_events

        events = get_stop_loss_events(limit=50, db_path=tmp_db)
        assert isinstance(events, list)
        assert len(events) == 1
        assert events[0]["bet_id"] == "api1"

    def test_summary_endpoint_aggregation(self, tmp_db):
        from src.utils.db import get_stop_loss_summary, log_stop_loss_event

        for i in range(5):
            log_stop_loss_event(
                bet_id=f"agg{i}",
                question=f"Q{i}",
                category="weather",
                trigger_type="standard_sl",
                entry_price=0.5,
                current_price=0.3,
                return_pct=-0.4,
                threshold_pct=0.4,
                pnl=-0.5,
                db_path=tmp_db,
            )

        summary = get_stop_loss_summary(days=7, db_path=tmp_db)
        assert summary["total_events"] == 5
        assert abs(summary["total_pnl"] - (-2.5)) < 0.01
