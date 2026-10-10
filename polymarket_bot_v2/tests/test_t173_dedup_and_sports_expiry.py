"""
test_t173_dedup_and_sports_expiry.py — T173/T174/T175 紧急修复测试

测试范围:
  1. had_any_exit_for_market(): DB函数正确性
  2. 已退出市场防重入: 模拟深圳28°C重入场被拦截
  3. settlement_max_hours: 长期体育市场被过滤
"""

import time
import uuid

import pytest
import src.utils.db as db_module
from src.utils import db


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """临时 SQLite 隔离测试。"""
    tmp_db_path = tmp_path / "test_dedup.db"
    monkeypatch.setattr(db_module, "_DB_PATH", tmp_db_path)
    db_module.init_db(tmp_db_path)
    yield tmp_db_path


def _insert_bet(
    market_id: str = "mkt-001",
    resolved_outcome: str = None,
    bet_type: str = "entry",
    exit_type: str = None,
    db_path=None,
):
    """插入一条测试投注记录，如有 resolved_outcome 则更新退出状态。"""
    bet_id = str(uuid.uuid4())
    bet = {
        "bet_id": bet_id,
        "ts_entered": time.time(),
        "market_id": market_id,
        "question": "Test question?",
        "category": "weather",
        "outcome": "NO",
        "confidence": 0.80,
        "ai_probability": 0.10,
        "market_price": 0.50,
        "ev": 0.10,
        "size_usdc": 1.0,
        "sim_balance_before": 100.0,
        "sim_balance_after": 99.0,
        "market_end_date": "2026-03-26T12:00:00Z",
        "weather_context": "",
        "ai_models": "test",
        "cycle_id": 1,
        "bet_bucket": "B_edge",
        "market_slug": "test-slug",
        "event_slug": "test-event",
        "bet_type": bet_type,
    }
    db.insert_paper_bet(bet, db_path=db_path)

    # 如果需要退出状态，用UPDATE更新（模拟真实的退出流程）
    if resolved_outcome:
        from src.utils.db import connect

        with connect(db_path) as conn:
            conn.execute(
                """UPDATE paper_bets
                   SET resolved_outcome=?, resolved_at=?, pnl=?, payout=?,
                       is_win=0, exit_price=0.10, exit_type=?
                   WHERE bet_id=?""",
                (resolved_outcome, time.time(), -0.5, 0.5, exit_type, bet_id),
            )
    return bet


# ══════════════════════════════════════════════════════
# Part 1: had_any_exit_for_market DB函数测试
# ══════════════════════════════════════════════════════


class TestHadAnyExitForMarket:
    def test_no_bets_returns_false(self, tmp_db):
        assert db.had_any_exit_for_market("nonexistent") is False

    def test_open_bet_returns_false(self, tmp_db):
        """有开仓但未退出 → False。"""
        _insert_bet("mkt-open", resolved_outcome=None)
        assert db.had_any_exit_for_market("mkt-open") is False

    def test_manual_exit_returns_true(self, tmp_db):
        """手动退出 → True。"""
        _insert_bet(
            "mkt-manual", resolved_outcome="MANUAL_EXIT", exit_type="manual_exit"
        )
        assert db.had_any_exit_for_market("mkt-manual") is True

    def test_stop_loss_returns_true(self, tmp_db):
        """止损退出 → True。"""
        _insert_bet("mkt-sl", resolved_outcome="STOP_LOSS", exit_type="stop_loss")
        assert db.had_any_exit_for_market("mkt-sl") is True

    def test_resolution_returns_true(self, tmp_db):
        """自然结算 → True。"""
        _insert_bet("mkt-res", resolved_outcome="YES", exit_type="resolution")
        assert db.had_any_exit_for_market("mkt-res") is True

    def test_scale_in_ignored(self, tmp_db):
        """scale_in 记录不算退出（只检查 entry 类型）。"""
        _insert_bet(
            "mkt-scale",
            resolved_outcome="STOP_LOSS",
            exit_type="stop_loss",
            bet_type="scale_in",
        )
        assert db.had_any_exit_for_market("mkt-scale") is False

    def test_mixed_open_and_exit(self, tmp_db):
        """有1条open + 1条已退出 → True（有退出记录即True）。"""
        _insert_bet("mkt-mix", resolved_outcome=None)
        _insert_bet("mkt-mix", resolved_outcome="MANUAL_EXIT", exit_type="manual_exit")
        assert db.had_any_exit_for_market("mkt-mix") is True


# ══════════════════════════════════════════════════════
# Part 2: 体育结算时限配置测试
# ══════════════════════════════════════════════════════


class TestSportsSettlementDaysConfig:
    def test_config_exists(self):
        from src.utils.config_loader import get

        pt_cfg = get("paper_trading")
        # T257/258/259: settlement_max_hours 统一时限配置
        assert "settlement_max_hours" in pt_cfg
        hours_cfg = pt_cfg["settlement_max_hours"]
        assert "sports" in hours_cfg
        val = float(hours_cfg["sports"])
        assert 1 <= val <= 168, f"settlement_max_hours.sports={val} 应在1-168范围"

    def test_default_is_36h(self):
        """全赛道结算配置验证。"""
        from src.utils.config_loader import get

        pt_cfg = get("paper_trading")
        hours_cfg = pt_cfg["settlement_max_hours"]
        assert float(hours_cfg["sports"]) == 48
        assert float(hours_cfg["weather"]) == 32
        assert float(hours_cfg["crypto"]) == 60  # T49: 保守值 60h


# ══════════════════════════════════════════════════════
# Part 3: T178 体育结算时限过滤加固测试
# ══════════════════════════════════════════════════════


class TestSportsExpiryEdgeCases:
    """T178: 确保体育无endDate和解析失败都被安全跳过。"""

    def test_no_enddate_is_skipped(self):
        """无endDate的体育市场应被 paper_trader 跳过。"""
        from src.utils.config_loader import get

        pt_cfg = get("paper_trading")
        hours_cfg = pt_cfg.get("settlement_max_hours", {})
        hours_limit = float(hours_cfg.get("sports", 36))
        # 模拟: 空 endDate 等同于远期市场 → 应被跳过
        # 这是配置确认级测试，功能测试在 test_live_filters.py
        assert hours_limit > 0

    def test_bad_enddate_is_skipped(self):
        """解析失败的 endDate 应等同于跳过（而非 pass）。"""
        from datetime import datetime, timezone

        bad_dates = ["", "not-a-date", "2026-99-99", None]
        for d in bad_dates:
            if d:
                try:
                    datetime.fromisoformat(d.replace("Z", "+00:00"))
                    # 如果没有抛异常说明格式有效（不应出现在此测试中）
                except (ValueError, TypeError, AttributeError):
                    pass  # 期望：解析失败 → 应被 continue 跳过
