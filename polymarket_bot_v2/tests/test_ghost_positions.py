"""
test_ghost_positions.py — 幽灵仓位修复的单元测试

覆盖：
  1. DB schema v7 迁移：sell_fail_count 列
  2. 持久化卖出失败计数器 (increment / reset)
  3. 幽灵仓位重新打开 (reopen_ghost_position)
  4. 最近关闭仓位查询 (get_recently_closed_bets)
  5. monitor_positions 止盈/止损：dry_run vs 实盘流程分离
  6. _reconcile_with_chain 幽灵仓位检测
"""

import sqlite3
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from src.utils.db import (connect, get_open_paper_bets,
                          get_recently_closed_bets, increment_sell_fail_count,
                          init_db, insert_paper_bet, reopen_ghost_position,
                          reset_sell_fail_count, set_db_path)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path):
    """创建一个临时 DB 用于测试。"""
    db_path = tmp_path / "test.db"
    set_db_path(db_path)
    init_db(db_path)
    return db_path


def _make_bet(
    bet_id: str = None,
    market_id: str = "mkt_001",
    question: str = "Test question?",
    category: str = "weather",
    outcome: str = "YES",
    size_usdc: float = 2.0,
    market_price: float = 0.5,
    condition_id: str = "0xcond_abc",
):
    return {
        "bet_id": bet_id or str(uuid.uuid4()),
        "ts_entered": time.time() - 3600,
        "market_id": market_id,
        "question": question,
        "category": category,
        "outcome": outcome,
        "confidence": 0.7,
        "ai_probability": 0.65,
        "market_price": market_price,
        "ev": 0.08,
        "size_usdc": size_usdc,
        "sim_balance_before": 100.0,
        "sim_balance_after": 98.0,
        "market_end_date": "2026-03-25",
        "weather_context": "",
        "ai_models": "test",
        "cycle_id": 1,
        "condition_id": condition_id,
    }


# ---------------------------------------------------------------------------
# 1. DB schema v7：sell_fail_count 列存在
# ---------------------------------------------------------------------------


class TestSchemaV7:
    def test_sell_fail_count_column_exists(self, tmp_db):
        """迁移后 paper_bets 应有 sell_fail_count 列，默认值为 0。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        with connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT sell_fail_count FROM paper_bets WHERE bet_id = ?",
                (bet["bet_id"],),
            ).fetchone()
        assert row is not None
        assert row["sell_fail_count"] == 0

    def test_schema_version_is_7(self, tmp_db):
        """schema_version 表最大版本号应为当前最新版本（v20）。"""
        with connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT MAX(version) as v FROM schema_version"
            ).fetchone()
        assert row["v"] == 20


# ---------------------------------------------------------------------------
# 2. 持久化卖出失败计数器
# ---------------------------------------------------------------------------


class TestSellFailCounter:
    def test_increment_from_zero(self, tmp_db):
        """首次 increment 应返回 1。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        count = increment_sell_fail_count(bet["bet_id"], db_path=tmp_db)
        assert count == 1

    def test_increment_accumulates(self, tmp_db):
        """多次 increment 应累加。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        for expected in range(1, 6):
            count = increment_sell_fail_count(bet["bet_id"], db_path=tmp_db)
            assert count == expected

    def test_reset_clears_count(self, tmp_db):
        """reset 后计数归零。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        increment_sell_fail_count(bet["bet_id"], db_path=tmp_db)
        increment_sell_fail_count(bet["bet_id"], db_path=tmp_db)
        reset_sell_fail_count(bet["bet_id"], db_path=tmp_db)

        with connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT sell_fail_count FROM paper_bets WHERE bet_id = ?",
                (bet["bet_id"],),
            ).fetchone()
        assert row["sell_fail_count"] == 0

    def test_counter_survives_reconnect(self, tmp_db):
        """计数器在新连接后仍然保持（模拟重启）。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        increment_sell_fail_count(bet["bet_id"], db_path=tmp_db)
        increment_sell_fail_count(bet["bet_id"], db_path=tmp_db)

        # 新连接读取
        with connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT sell_fail_count FROM paper_bets WHERE bet_id = ?",
                (bet["bet_id"],),
            ).fetchone()
        assert row["sell_fail_count"] == 2


# ---------------------------------------------------------------------------
# 3. 幽灵仓位重新打开
# ---------------------------------------------------------------------------


class TestReopenGhostPosition:
    def test_reopen_clears_resolved_fields(self, tmp_db):
        """reopen 应清除 resolved_at, pnl, exit_price, exit_type。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        # 先关闭这个仓位
        with connect(tmp_db) as conn:
            conn.execute(
                "UPDATE paper_bets SET resolved_outcome='STOP_LOSS', "
                "resolved_at=?, pnl=-1.5, payout=0.5, exit_price=0.3, "
                "exit_type='stop_loss', sell_fail_count=5 WHERE bet_id=?",
                (time.time(), bet["bet_id"]),
            )

        # 验证已关闭
        open_bets = get_open_paper_bets(db_path=tmp_db)
        assert len(open_bets) == 0

        # 重新打开
        result = reopen_ghost_position(bet["bet_id"], db_path=tmp_db)
        assert result is True

        # 验证已打开且清除了已解决字段
        open_bets = get_open_paper_bets(db_path=tmp_db)
        assert len(open_bets) == 1
        reopened = open_bets[0]
        assert reopened["resolved_outcome"] is None
        assert reopened["resolved_at"] is None
        assert reopened["pnl"] is None
        assert reopened["exit_price"] is None
        assert reopened["exit_type"] is None
        assert reopened["sell_fail_count"] == 0

    def test_reopen_already_open_noop(self, tmp_db):
        """对已经 open 的仓位调用 reopen 返回 False（无变更）。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        result = reopen_ghost_position(bet["bet_id"], db_path=tmp_db)
        assert result is False

    def test_reopen_nonexistent_bet(self, tmp_db):
        """不存在的 bet_id 返回 False。"""
        result = reopen_ghost_position("nonexistent-id", db_path=tmp_db)
        assert result is False


# ---------------------------------------------------------------------------
# 4. 最近关闭仓位查询
# ---------------------------------------------------------------------------


class TestGetRecentlyClosedBets:
    def test_returns_recently_closed(self, tmp_db):
        """返回最近关闭的仓位。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)
        with connect(tmp_db) as conn:
            conn.execute(
                "UPDATE paper_bets SET resolved_outcome='STOP_LOSS', "
                "resolved_at=?, pnl=-1.0 WHERE bet_id=?",
                (time.time(), bet["bet_id"]),
            )

        closed = get_recently_closed_bets(days=7, db_path=tmp_db)
        assert len(closed) == 1
        assert closed[0]["bet_id"] == bet["bet_id"]

    def test_excludes_old_closures(self, tmp_db):
        """排除超过 N 天前关闭的仓位。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)
        old_ts = time.time() - 10 * 86400  # 10 天前
        with connect(tmp_db) as conn:
            conn.execute(
                "UPDATE paper_bets SET resolved_outcome='STOP_LOSS', "
                "resolved_at=?, pnl=-1.0 WHERE bet_id=?",
                (old_ts, bet["bet_id"]),
            )

        closed = get_recently_closed_bets(days=7, db_path=tmp_db)
        assert len(closed) == 0

    def test_excludes_open_bets(self, tmp_db):
        """不返回仍然开放的仓位。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        closed = get_recently_closed_bets(days=7, db_path=tmp_db)
        assert len(closed) == 0


# ---------------------------------------------------------------------------
# 5. monitor_positions: dry_run vs 实盘流程分离
# ---------------------------------------------------------------------------


class TestMonitorPositionsDryRunSafety:
    """验证止盈/止损在 dry_run 和实盘模式下的 DB 操作正确性。"""

    @patch("main.get")
    @patch("main.is_dry_run", return_value=True)
    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    def test_dry_run_closes_db_without_sell(
        self, mock_get_bets, mock_components, mock_is_dry, mock_get
    ):
        """DRY RUN 模式下不调用 sell_position，但仍关闭 DB。"""
        mock_get.side_effect = lambda k: {
            "general.position_monitor_interval_minutes": 2,
        }.get(k)

        tracker = MagicMock()
        executor = MagicMock()
        mock_components.return_value = (MagicMock(), MagicMock(), executor, MagicMock())

        import main

        main._outcome_tracker = tracker
        main._executor = executor

        bet = {
            "bet_id": "test-dry-001",
            "market_id": "mkt_d1",
            "question": "Test?",
            "exit_price": 0.3,
            "market_price": 0.5,
            "size_usdc": 2.0,
        }
        mock_get_bets.return_value = [bet]
        tracker._fetch_market_statuses.return_value = {}
        tracker.check_profit_take_exits.return_value = [bet]
        tracker.check_stop_loss_exits.return_value = []

        main.monitor_positions()

        # 关键：DRY RUN 不调用 sell_position
        executor.sell_position.assert_not_called()
        # 但仍关闭 DB
        tracker.close_position_profit_take.assert_called_once_with(bet)

    @patch("main.get")
    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main._post_close_chain_verify")
    def test_live_mode_sell_fail_does_not_close_db(
        self, mock_verify, mock_get_bets, mock_components, mock_is_dry, mock_get
    ):
        """实盘模式：卖出失败时不关闭 DB（防止幽灵仓位）。"""
        mock_get.side_effect = lambda k: {
            "general.position_monitor_interval_minutes": 2,
            "executor.max_sell_retries_before_writeoff": 3,
            "executor.sweep_min_shares": 0.01,
            "executor.min_order_size_shares": 5,
        }.get(k)

        tracker = MagicMock()
        executor = MagicMock()
        mock_components.return_value = (MagicMock(), MagicMock(), executor, MagicMock())

        import main

        main._outcome_tracker = tracker
        main._executor = executor

        bet = {
            "bet_id": "test-live-fail-001",
            "market_id": "mkt_l1",
            "question": "Test?",
            "exit_price": 0.3,
            "market_price": 0.5,
            "size_usdc": 2.0,
            "matched_shares": 6.0,  # >= min_order_shares, 避免被 Phase 43 主动扫描拦截
        }
        mock_get_bets.return_value = [bet]
        tracker._fetch_market_statuses.return_value = {}
        tracker.check_profit_take_exits.return_value = []
        tracker.check_stop_loss_exits.return_value = [bet]

        # 卖出失败
        sell_result = MagicMock()
        sell_result.success = False
        sell_result.error = "卖出未成交: TIMEOUT_CANCELLED"
        executor.sell_position.return_value = sell_result

        with patch("main.increment_sell_fail_count", return_value=1):
            main.monitor_positions()

        # 关键：卖出失败时 DB 不应被关闭
        tracker.close_position_stop_loss.assert_not_called()


# ---------------------------------------------------------------------------
# 6. _reconcile_with_chain 幽灵仓位检测
# ---------------------------------------------------------------------------


class TestReconcileGhostDetection:
    """验证 _reconcile_with_chain 能检测并恢复幽灵仓位。"""

    @patch("main.reopen_ghost_position", return_value=True)
    @patch("main.get_recently_closed_bets")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.requests.get")
    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xTestAddr"})
    def test_detects_ghost_position(
        self,
        mock_req_get,
        mock_is_dry,
        mock_get,
        mock_open_bets,
        mock_closed_bets,
        mock_reopen,
    ):
        """链上有 shares 但 DB 已关闭的仓位应被重新打开。"""
        mock_get.side_effect = lambda k: {
            "reconcile.min_age_seconds": 300,
            "reconcile.min_ghost_shares": 1.0,
        }.get(k)

        # 链上数据：condition_id=0xcond_ghost 有 20 shares
        chain_response = MagicMock()
        chain_response.status_code = 200
        chain_response.json.return_value = [
            {"conditionId": "0xcond_ghost", "size": "20.0", "avgPrice": "0.1"},
        ]
        chain_response.raise_for_status = MagicMock()
        mock_req_get.return_value = chain_response

        # DB：无 open bets
        mock_open_bets.return_value = []

        # DB：有一个关闭的 bet 匹配此 condition_id
        # Phase 35: 使用 resolution exit_type（可重开），
        # stop_loss/profit_take/write_off 已被标记为不可重开
        mock_closed_bets.return_value = [
            {
                "bet_id": "ghost-001",
                "condition_id": "0xcond_ghost",
                "question": "Toronto high temp 5C?",
                "exit_type": "resolution",
            }
        ]

        import main

        main._reconcile_with_chain()

        # 验证幽灵仓位被重新打开
        mock_reopen.assert_called_once_with("ghost-001")

    @patch("main.reopen_ghost_position")
    @patch("main.get_recently_closed_bets")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.requests.get")
    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xTestAddr"})
    def test_ignores_dust_ghost_position(
        self,
        mock_req_get,
        mock_is_dry,
        mock_get,
        mock_open_bets,
        mock_closed_bets,
        mock_reopen,
    ):
        """链上 shares 低于 min_ghost_shares 的不应被恢复。"""
        mock_get.side_effect = lambda k: {
            "reconcile.min_age_seconds": 300,
            "reconcile.min_ghost_shares": 1.0,
        }.get(k)

        chain_response = MagicMock()
        chain_response.status_code = 200
        chain_response.json.return_value = [
            {"conditionId": "0xcond_dust", "size": "0.5", "avgPrice": "0.1"},
        ]
        chain_response.raise_for_status = MagicMock()
        mock_req_get.return_value = chain_response

        mock_open_bets.return_value = []
        mock_closed_bets.return_value = [
            {
                "bet_id": "dust-001",
                "condition_id": "0xcond_dust",
                "question": "Dust position?",
                "exit_type": "stop_loss",
            }
        ]

        import main

        main._reconcile_with_chain()

        # 灰尘仓位不应被恢复
        mock_reopen.assert_not_called()


# ---------------------------------------------------------------------------
# T179: 幽灵仓位重开后 check_and_update 不应自解析回关
# ---------------------------------------------------------------------------


class TestGhostReopenProtection:
    """T179: reopen_ghost_position 设置 ghost_reopened_at，
    check_and_update 的自解析分支应跳过这些仓位。"""

    def test_reopen_sets_ghost_reopened_at(self, tmp_db):
        """reopen 后 ghost_reopened_at 应为非空时间戳。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        # 先关闭
        with connect(tmp_db) as conn:
            conn.execute(
                "UPDATE paper_bets SET resolved_outcome='YES', "
                "resolved_at=?, pnl=1.0, payout=2.0, exit_price=0.9, "
                "exit_type='resolution' WHERE bet_id=?",
                (time.time(), bet["bet_id"]),
            )

        before = time.time()
        result = reopen_ghost_position(bet["bet_id"], db_path=tmp_db)
        assert result is True

        with connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT ghost_reopened_at FROM paper_bets WHERE bet_id=?",
                (bet["bet_id"],),
            ).fetchone()
        assert row["ghost_reopened_at"] is not None
        assert row["ghost_reopened_at"] >= before

    def test_ghost_reopened_at_cleared_on_profit_take(self, tmp_db):
        """profit_take 关闭后 ghost_reopened_at 应被清除。"""
        from src.utils.db import close_paper_bet_profit_take

        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        # 关闭 → 重开（设置 ghost_reopened_at）
        with connect(tmp_db) as conn:
            conn.execute(
                "UPDATE paper_bets SET resolved_outcome='YES', "
                "resolved_at=?, pnl=1.0, payout=2.0 WHERE bet_id=?",
                (time.time(), bet["bet_id"]),
            )
        reopen_ghost_position(bet["bet_id"], db_path=tmp_db)

        # profit_take 关闭
        close_paper_bet_profit_take(
            bet["bet_id"], exit_price=0.95, pnl=0.5, payout=1.5, db_path=tmp_db
        )

        with connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT ghost_reopened_at FROM paper_bets WHERE bet_id=?",
                (bet["bet_id"],),
            ).fetchone()
        assert row["ghost_reopened_at"] is None

    def test_ghost_reopened_at_cleared_on_stop_loss(self, tmp_db):
        """stop_loss 关闭后 ghost_reopened_at 应被清除。"""
        from src.utils.db import close_paper_bet_stop_loss

        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        # 关闭 → 重开
        with connect(tmp_db) as conn:
            conn.execute(
                "UPDATE paper_bets SET resolved_outcome='YES', "
                "resolved_at=?, pnl=1.0, payout=2.0 WHERE bet_id=?",
                (time.time(), bet["bet_id"]),
            )
        reopen_ghost_position(bet["bet_id"], db_path=tmp_db)

        # stop_loss 关闭
        close_paper_bet_stop_loss(
            bet["bet_id"], exit_price=0.3, pnl=-0.5, payout=0.5, db_path=tmp_db
        )

        with connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT ghost_reopened_at FROM paper_bets WHERE bet_id=?",
                (bet["bet_id"],),
            ).fetchone()
        assert row["ghost_reopened_at"] is None

    def test_ghost_reopened_at_column_exists(self, tmp_db):
        """migration v12 应添加 ghost_reopened_at 列，默认 NULL。"""
        bet = _make_bet()
        insert_paper_bet(bet, db_path=tmp_db)

        with connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT ghost_reopened_at FROM paper_bets WHERE bet_id=?",
                (bet["bet_id"],),
            ).fetchone()
        assert row["ghost_reopened_at"] is None
