"""
T240: 多赛道统一回测引擎 — 单元测试
覆盖: run_backtest 时间模式, 赛道过滤, 全局统计, 赛道统计,
       信号质量, 止损质量, 空结果, _classify_bets, API 路由
"""

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_db(tmp_path):
    """创建临时DB并执行所有迁移。"""
    db_path = tmp_path / "test.db"
    from src.utils.db import init_db

    init_db(db_path)
    return db_path


def _insert_bet(db_path, **overrides):
    """向 paper_bets 插入一条测试投注。"""
    now_ts = time.time()
    defaults = {
        "bet_id": f"test_{now_ts}_{id(overrides)}",
        "ts_entered": now_ts,
        "market_id": "mid_001",
        "question": "Test question?",
        "category": "weather",
        "outcome": "YES",
        "confidence": 0.7,
        "ai_probability": 0.65,
        "market_price": 0.50,
        "ev": 0.08,
        "size_usdc": 1.0,
        "sim_balance_before": 100.0,
        "sim_balance_after": 99.0,
        "exit_type": "resolution",
        "is_win": 1,
        "pnl": 0.50,
        "exit_price": 1.0,
        "bet_bucket": "standard",
        "ai_models": "qwen3-235b,gemini-2.5-flash",
    }
    defaults.update(overrides)
    conn = sqlite3.connect(str(db_path))
    cols = list(defaults.keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    conn.execute(
        f"INSERT INTO paper_bets ({col_names}) VALUES ({placeholders})",
        list(defaults.values()),
    )
    conn.commit()
    conn.close()


def _seed_mixed_bets(db_path, count=20):
    """插入混合赛道、混合结果的测试数据。"""
    now = time.time()
    categories = ["weather", "sports", "crypto", "finance", "weather", "sports"]
    exit_types = [
        "resolution",
        "stop_loss",
        "profit_take",
        "resolution",
        "resolution",
        "stop_loss",
    ]
    is_wins = [1, 0, 0, 1, 1, 0]
    pnls = [0.50, -0.30, -0.80, 0.60, 0.40, -0.50]

    for i in range(count):
        idx = i % len(categories)
        _insert_bet(
            db_path,
            bet_id=f"mix_{i}",
            ts_entered=now - (count - i) * 60,  # 1分钟间隔
            market_id=f"mid_{i:03d}",
            question=f"Question {i}?",
            category=categories[idx],
            outcome="YES" if is_wins[idx] else "NO",
            exit_type=exit_types[idx],
            is_win=is_wins[idx],
            pnl=pnls[idx],
            ev=0.05 + i * 0.005,
            confidence=0.6 + (i % 4) * 0.05,
            size_usdc=1.0 + i * 0.1,
            ai_models="qwen3-235b,gemini-2.5-flash",
        )


# ═══════════════════════════════════════════════════════════════
# run_backtest 基本功能
# ═══════════════════════════════════════════════════════════════


class TestRunBacktestBasic:
    """run_backtest 基本调用。"""

    def test_empty_db_returns_zero(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        result = run_backtest(mode="days", value=7, db_path=tmp_db)
        assert result["query"]["total_bets"] == 0
        assert result["global"]["total"] == 0
        assert result["by_category"] == {}

    def test_days_mode(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _seed_mixed_bets(tmp_db, count=10)
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        assert result["query"]["mode"] == "days"
        assert result["query"]["total_bets"] == 10

    def test_hours_mode(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _seed_mixed_bets(tmp_db, count=6)
        result = run_backtest(mode="hours", value=1, db_path=tmp_db)
        assert result["query"]["mode"] == "hours"
        assert result["query"]["total_bets"] == 6

    def test_range_mode(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _seed_mixed_bets(tmp_db, count=6)
        now = datetime.now(timezone.utc)
        start_str = (now - timedelta(days=1)).isoformat()
        end_str = (now + timedelta(days=1)).isoformat()
        result = run_backtest(
            mode="range", start=start_str, end=end_str, db_path=tmp_db
        )
        assert result["query"]["mode"] == "range"
        assert result["query"]["total_bets"] == 6

    def test_range_swap_dates(self, tmp_db):
        """end < start 时自动交换。"""
        from src.backtest.backtest_engine import run_backtest

        _seed_mixed_bets(tmp_db, count=4)
        now = datetime.now(timezone.utc)
        result = run_backtest(
            mode="range",
            start=(now + timedelta(days=1)).isoformat(),
            end=(now - timedelta(days=1)).isoformat(),
            db_path=tmp_db,
        )
        assert result["query"]["total_bets"] == 4


class TestTrackFilter:
    """赛道过滤测试。"""

    def test_filter_weather_only(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _seed_mixed_bets(tmp_db, count=12)
        result = run_backtest(mode="days", value=1, track="weather", db_path=tmp_db)
        # 12 条中 weather 有 index 0,4,6,10 → 4 条
        for cat in result.get("by_category", {}):
            assert cat == "weather"
        assert result["query"]["total_bets"] > 0

    def test_filter_nonexistent_track(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _seed_mixed_bets(tmp_db, count=6)
        result = run_backtest(mode="days", value=1, track="esports", db_path=tmp_db)
        assert result["query"]["total_bets"] == 0

    def test_empty_track_returns_all(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _seed_mixed_bets(tmp_db, count=12)
        result = run_backtest(mode="days", value=1, track="", db_path=tmp_db)
        assert result["query"]["total_bets"] == 12


# ═══════════════════════════════════════════════════════════════
# 全局统计
# ═══════════════════════════════════════════════════════════════


class TestGlobalStats:
    """全局统计计算。"""

    def test_win_rate_calculation(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _seed_mixed_bets(tmp_db, count=6)
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        g = result["global"]
        assert g["total"] == 6
        assert g["closed"] == 6  # all have exit_type
        assert g["wins"] > 0
        assert g["losses"] > 0
        assert 0 < g["win_rate"] < 100
        assert isinstance(g["total_pnl"], (int, float))
        assert isinstance(g["roi_pct"], (int, float))

    def test_pnl_sums_match(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _insert_bet(
            tmp_db,
            bet_id="a1",
            pnl=1.0,
            is_win=1,
            exit_type="resolution",
            size_usdc=2.0,
        )
        _insert_bet(
            tmp_db,
            bet_id="a2",
            pnl=-0.5,
            is_win=0,
            exit_type="stop_loss",
            size_usdc=1.0,
        )
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        g = result["global"]
        assert g["total_pnl"] == pytest.approx(0.5, abs=0.01)
        assert g["wins"] == 1
        assert g["losses"] == 1

    def test_by_exit_type_breakdown(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _insert_bet(tmp_db, bet_id="r1", exit_type="resolution")
        _insert_bet(tmp_db, bet_id="s1", exit_type="stop_loss")
        _insert_bet(tmp_db, bet_id="p1", exit_type="profit_take")
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        by_exit = result["global"]["by_exit_type"]
        assert by_exit.get("resolution", 0) >= 1
        assert by_exit.get("stop_loss", 0) >= 1
        assert by_exit.get("profit_take", 0) >= 1


# ═══════════════════════════════════════════════════════════════
# 赛道统计
# ═══════════════════════════════════════════════════════════════


class TestCategoryStats:
    """赛道统计。"""

    def test_multi_category_breakdown(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _insert_bet(tmp_db, bet_id="w1", category="weather", pnl=0.5, is_win=1)
        _insert_bet(tmp_db, bet_id="s1", category="sports", pnl=-0.3, is_win=0)
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        cats = result["by_category"]
        assert "weather" in cats
        assert "sports" in cats
        assert cats["weather"]["total"] == 1
        assert cats["sports"]["total"] == 1
        assert cats["weather"]["total_pnl"] == pytest.approx(0.5, abs=0.01)

    def test_category_win_rate(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        for i in range(5):
            _insert_bet(
                tmp_db,
                bet_id=f"w{i}",
                category="weather",
                pnl=0.5 if i < 3 else -0.3,
                is_win=1 if i < 3 else 0,
            )
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        assert result["by_category"]["weather"]["win_rate"] == 60.0


# ═══════════════════════════════════════════════════════════════
# 信号质量
# ═══════════════════════════════════════════════════════════════


class TestSignalQuality:
    """信号质量分析。"""

    def test_ev_bands(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _insert_bet(tmp_db, bet_id="e1", ev=0.03, pnl=0.2, is_win=1)  # 0-5%
        _insert_bet(tmp_db, bet_id="e2", ev=0.07, pnl=-0.1, is_win=0)  # 5-10%
        _insert_bet(tmp_db, bet_id="e3", ev=0.12, pnl=0.5, is_win=1)  # 10-15%
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        by_ev = result["signal_quality"]["by_ev"]
        assert len(by_ev) == 3
        labels = [b["range"] for b in by_ev]
        assert "0-5%" in labels
        assert "5-10%" in labels
        assert "10-15%" in labels

    def test_confidence_bands(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _insert_bet(tmp_db, bet_id="c1", confidence=0.55, pnl=0.1, is_win=1)
        _insert_bet(tmp_db, bet_id="c2", confidence=0.75, pnl=-0.2, is_win=0)
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        by_conf = result["signal_quality"]["by_confidence"]
        assert len(by_conf) >= 1

    def test_model_stats(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _insert_bet(
            tmp_db,
            bet_id="m1",
            ai_models="qwen3-235b,gemini-2.5-flash",
            pnl=0.3,
            is_win=1,
        )
        _insert_bet(tmp_db, bet_id="m2", ai_models="qwen3-235b", pnl=-0.1, is_win=0)
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        by_model = result["signal_quality"]["by_model"]
        model_names = [m["name"] for m in by_model]
        assert "qwen3-235b" in model_names

    def test_empty_signal_quality(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        sq = result["signal_quality"]
        assert sq["by_ev"] == []
        assert sq["by_confidence"] == []


# ═══════════════════════════════════════════════════════════════
# 止损质量 (fast mode)
# ═══════════════════════════════════════════════════════════════


class TestStopQuality:
    """止损质量分析。"""

    def test_stop_loss_stats(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _insert_bet(
            tmp_db,
            bet_id="sl1",
            exit_type="stop_loss",
            pnl=-0.3,
            is_win=0,
            category="weather",
        )
        _insert_bet(
            tmp_db,
            bet_id="sl2",
            exit_type="stop_loss",
            pnl=-0.5,
            is_win=0,
            category="sports",
        )
        _insert_bet(
            tmp_db,
            bet_id="r1",
            exit_type="resolution",
            pnl=0.8,
            is_win=1,
            category="weather",
        )
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        sq = result["stop_quality"]
        assert sq["total_stops"] == 2
        assert sq["total_pnl"] == pytest.approx(-0.8, abs=0.01)
        assert sq["deep_analysis"] is None  # fast mode

    def test_no_stops_returns_empty(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _insert_bet(tmp_db, bet_id="r1", exit_type="resolution")
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        sq = result["stop_quality"]
        assert sq["total_stops"] == 0

    def test_stop_by_category(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _insert_bet(
            tmp_db,
            bet_id="sl1",
            exit_type="stop_loss",
            pnl=-0.3,
            is_win=0,
            category="weather",
        )
        _insert_bet(
            tmp_db,
            bet_id="sl2",
            exit_type="stop_loss",
            pnl=-0.5,
            is_win=0,
            category="weather",
        )
        _insert_bet(
            tmp_db,
            bet_id="sl3",
            exit_type="stop_loss",
            pnl=-0.2,
            is_win=0,
            category="sports",
        )
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        by_cat = result["stop_quality"]["by_category"]
        cat_names = [c["category"] for c in by_cat]
        assert "weather" in cat_names
        assert "sports" in cat_names
        weather_cat = next(c for c in by_cat if c["category"] == "weather")
        assert weather_cat["count"] == 2


# ═══════════════════════════════════════════════════════════════
# _classify_bets 内部函数
# ═══════════════════════════════════════════════════════════════


class TestClassifyBets:
    """_classify_bets 分类逻辑。"""

    def test_classification_keys(self):
        from src.backtest.backtest_engine import _classify_bets

        bets = [
            {"exit_type": ""},
            {"exit_type": None},
            {"exit_type": "resolution"},
            {"exit_type": "stop_loss"},
            {"exit_type": "profit_take"},
            {"exit_type": "manual_exit"},
            {"exit_type": "wu_temp_breach"},
            {"exit_type": "unknown_type"},
        ]
        result = _classify_bets(bets)
        assert len(result["open"]) == 2  # empty + None
        assert len(result["resolution"]) == 1
        assert len(result["stop_loss"]) == 1
        assert len(result["profit_take"]) == 1
        assert len(result["manual_exit"]) == 1
        assert len(result["wu_temp_breach"]) == 1
        assert len(result["other"]) == 1

    def test_manual_alias(self):
        from src.backtest.backtest_engine import _classify_bets

        result = _classify_bets([{"exit_type": "manual"}])
        assert len(result["manual_exit"]) == 1


# ═══════════════════════════════════════════════════════════════
# deep 止损分类 (mock API)
# ═══════════════════════════════════════════════════════════════


class TestDeepStopClassification:
    """深度止损分类 (mock Polymarket API)。"""

    def _make_api_response(self, yes_price, no_price):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "outcomePrices": json.dumps([str(yes_price), str(no_price)])
        }
        return mock_resp

    @patch("requests.get")
    def test_deep_correct_classification(self, mock_get, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        # 投 YES, 市场最终 NO (YES price < 0.05) → 正确止损
        _insert_bet(
            tmp_db,
            bet_id="sl_deep_1",
            exit_type="stop_loss",
            pnl=-0.3,
            is_win=0,
            outcome="YES",
            market_id="deep_mid_1",
        )
        mock_get.return_value = self._make_api_response(0.02, 0.98)
        result = run_backtest(mode="days", value=1, deep=True, db_path=tmp_db)
        da = result["stop_quality"]["deep_analysis"]
        assert da is not None
        assert da["correct"] == 1
        assert da["wrong"] == 0

    @patch("requests.get")
    def test_deep_wrong_classification(self, mock_get, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        # 投 YES, 市场最终 YES (YES price >= 0.95) → 错误止损
        _insert_bet(
            tmp_db,
            bet_id="sl_deep_2",
            exit_type="stop_loss",
            pnl=-0.3,
            is_win=0,
            outcome="YES",
            market_id="deep_mid_2",
        )
        mock_get.return_value = self._make_api_response(0.97, 0.03)
        result = run_backtest(mode="days", value=1, deep=True, db_path=tmp_db)
        da = result["stop_quality"]["deep_analysis"]
        assert da["wrong"] == 1
        assert da["correct"] == 0

    @patch("requests.get")
    def test_deep_pending_classification(self, mock_get, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        # 市场未结算 (prices not extreme)
        _insert_bet(
            tmp_db,
            bet_id="sl_deep_3",
            exit_type="stop_loss",
            pnl=-0.3,
            is_win=0,
            outcome="YES",
            market_id="deep_mid_3",
        )
        mock_get.return_value = self._make_api_response(0.55, 0.45)
        result = run_backtest(mode="days", value=1, deep=True, db_path=tmp_db)
        da = result["stop_quality"]["deep_analysis"]
        assert da["pending"] == 1

    @patch("requests.get")
    def test_deep_api_failure_pending(self, mock_get, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _insert_bet(
            tmp_db,
            bet_id="sl_deep_4",
            exit_type="stop_loss",
            pnl=-0.3,
            is_win=0,
            outcome="YES",
            market_id="deep_mid_4",
        )
        mock_get.side_effect = Exception("API timeout")
        result = run_backtest(mode="days", value=1, deep=True, db_path=tmp_db)
        da = result["stop_quality"]["deep_analysis"]
        assert da["pending"] == 1  # API failure → pending


# ═══════════════════════════════════════════════════════════════
# 空结构
# ═══════════════════════════════════════════════════════════════


class TestEmptyStructures:
    """空结构函数验证。"""

    def test_empty_global(self):
        from src.backtest.backtest_engine import _empty_global

        g = _empty_global()
        assert g["total"] == 0
        assert g["total_pnl"] == 0
        assert "by_exit_type" in g

    def test_empty_stop_quality(self):
        from src.backtest.backtest_engine import _empty_stop_quality

        s = _empty_stop_quality()
        assert s["total_stops"] == 0
        assert s["deep_analysis"] is None

    def test_empty_signal_quality(self):
        from src.backtest.backtest_engine import _empty_signal_quality

        s = _empty_signal_quality()
        assert s["by_ev"] == []
        assert s["by_model"] == []


# ═══════════════════════════════════════════════════════════════
# API 路由测试
# ═══════════════════════════════════════════════════════════════


class TestBacktestAPI:
    """API 路由测试 (mock backtest engine)。"""

    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from scripts.dashboard_routes.backtest_routes import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    @patch("src.backtest.backtest_engine.run_backtest")
    def test_run_endpoint(self, mock_bt, client):
        mock_bt.return_value = {
            "query": {"mode": "days", "total_bets": 5},
            "global": {"total": 5},
        }
        resp = client.get("/api/backtest/run?mode=days&value=7")
        assert resp.status_code == 200
        assert resp.json()["query"]["total_bets"] == 5
        mock_bt.assert_called_once()
        call_kwargs = mock_bt.call_args[1]
        assert call_kwargs["mode"] == "days"
        assert call_kwargs["deep"] is False

    @patch("src.backtest.backtest_engine.run_backtest")
    def test_deep_endpoint(self, mock_bt, client):
        mock_bt.return_value = {
            "query": {"mode": "days", "total_bets": 3},
            "global": {"total": 3},
        }
        resp = client.get("/api/backtest/deep?mode=days&value=3")
        assert resp.status_code == 200
        call_kwargs = mock_bt.call_args[1]
        assert call_kwargs["deep"] is True

    @patch("src.backtest.backtest_engine.run_backtest")
    def test_track_param_forwarded(self, mock_bt, client):
        mock_bt.return_value = {"query": {"total_bets": 0}, "global": {"total": 0}}
        resp = client.get("/api/backtest/run?mode=days&value=7&track=weather")
        assert resp.status_code == 200
        call_kwargs = mock_bt.call_args[1]
        assert call_kwargs["track"] == "weather"

    @patch("src.backtest.backtest_engine.run_backtest")
    def test_export_endpoint(self, mock_bt, client):
        mock_bt.return_value = {"query": {"total_bets": 2}, "global": {"total": 2}}
        resp = client.get("/api/backtest/export?mode=days&value=7")
        assert resp.status_code == 200
        assert "attachment" in resp.headers.get("content-disposition", "")
        body = resp.json()
        assert body["query"]["total_bets"] == 2

    @patch("src.backtest.backtest_engine.run_backtest")
    def test_error_handling(self, mock_bt, client):
        mock_bt.side_effect = ValueError("bad input")
        resp = client.get("/api/backtest/run?mode=days&value=7")
        assert resp.status_code == 500
        assert "error" in resp.json()


# ═══════════════════════════════════════════════════════════════
# 结果结构完整性
# ═══════════════════════════════════════════════════════════════


class TestResultStructure:
    """确保返回结果包含所有必要字段。"""

    def test_full_result_keys(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _seed_mixed_bets(tmp_db, count=6)
        result = run_backtest(mode="days", value=1, db_path=tmp_db)
        assert "query" in result
        assert "global" in result
        assert "by_category" in result
        assert "stop_quality" in result
        assert "signal_quality" in result
        assert "elapsed_ms" in result
        assert isinstance(result["elapsed_ms"], int)

    def test_query_has_time_info(self, tmp_db):
        from src.backtest.backtest_engine import run_backtest

        _seed_mixed_bets(tmp_db, count=2)
        result = run_backtest(mode="days", value=3, db_path=tmp_db)
        q = result["query"]
        assert q["mode"] == "days"
        assert "start" in q
        assert "end" in q
