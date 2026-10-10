"""
test_dead_position_guard.py — T70 死亡仓位保护机制测试
覆盖：
  1. effective_resolution 低价卖出暂停（sell_fail_count >= threshold）
  2. Reconcile 死亡价格禁止重开
  3. Reconcile 死亡仓位不再自动核销（保持OPEN）
  4. SportsFastMonitor 卖出失败计数递增
"""

import time
from unittest.mock import MagicMock, patch

import pytest


class TestEffectiveResolutionSellSuspend:
    """effective_resolution 在低价+多次失败时暂停卖出"""

    def _make_tracker(self):
        from src.simulation.outcome_tracker import OutcomeTracker

        tracker = OutcomeTracker.__new__(OutcomeTracker)
        tracker.session = MagicMock()
        return tracker

    @patch("src.simulation.outcome_tracker.get")
    def test_sell_suspended_when_price_low_and_retries_exhausted(self, mock_get):
        """价格<0.05 且 sell_fail_count>=3 → 不触发 effective_resolution"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "position_management": {
                "dead_position_max_sell_retries": 3,
                "dead_position_price_threshold": 0.05,
            },
            "stop_loss": {},
        }.get(key, None)

        tracker = self._make_tracker()
        # Bet with 3 sell failures and near-zero price
        bet = {
            "bet_id": "test-dead-001",
            "market_id": "mkt123",
            "outcome": "YES",
            "market_price": 0.50,
            "sell_fail_count": 3,
            "category": "sports",
            "market_end_date": "2099-01-01T00:00:00Z",
            "bet_bucket": "sports",
            "strategy_tag": None,
        }
        # Market data: YES=0.001 (dead), NO=0.999
        market_data = {
            "mkt123": {
                "active": True,
                "resolved": False,
                "outcomePrices": "[0.001, 0.999]",
            }
        }
        stops = tracker.check_stop_loss_exits([bet], market_data=market_data)
        # Should NOT trigger effective_resolution because sell suspended
        assert len(stops) == 0

    @patch("src.simulation.outcome_tracker.get")
    def test_sell_proceeds_when_retries_not_exhausted(self, mock_get):
        """sell_fail_count < threshold → 正常触发 effective_resolution"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "position_management": {
                "dead_position_max_sell_retries": 3,
                "dead_position_price_threshold": 0.05,
            },
            "stop_loss": {},
        }.get(key, None)

        tracker = self._make_tracker()
        bet = {
            "bet_id": "test-dead-002",
            "market_id": "mkt456",
            "outcome": "YES",
            "market_price": 0.50,
            "sell_fail_count": 1,  # < 3
            "category": "sports",
            "market_end_date": "2099-01-01T00:00:00Z",
            "bet_bucket": "sports",
            "strategy_tag": None,
        }
        market_data = {
            "mkt456": {
                "active": True,
                "resolved": False,
                "outcomePrices": "[0.001, 0.999]",
            }
        }
        stops = tracker.check_stop_loss_exits([bet], market_data=market_data)
        # Should trigger because fail count is below threshold
        assert len(stops) == 1
        assert stops[0]["trigger"] == "effective_resolution"

    @patch("src.simulation.outcome_tracker.get")
    def test_sell_proceeds_when_threshold_very_low(self, mock_get):
        """price_threshold 设极低时，即使多次失败也正常触发"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "position_management": {
                "dead_position_max_sell_retries": 3,
                "dead_position_price_threshold": 0.001,  # 极低阈值
            },
            "stop_loss": {},
        }.get(key, None)

        tracker = self._make_tracker()
        bet = {
            "bet_id": "test-dead-003",
            "market_id": "mkt789",
            "outcome": "YES",
            "market_price": 0.50,
            "sell_fail_count": 10,
            "category": "sports",
            "market_end_date": "2099-01-01T00:00:00Z",
            "bet_bucket": "sports",
            "strategy_tag": None,
        }
        # YES dead: NO=0.99, YES=0.01 → dead_exit=0.01 ≥ threshold 0.001
        market_data = {
            "mkt789": {
                "active": True,
                "resolved": False,
                "outcomePrices": "[0.01, 0.99]",
            }
        }
        stops = tracker.check_stop_loss_exits([bet], market_data=market_data)
        # dead_exit=0.01 ≥ threshold=0.001 → should trigger
        assert len(stops) == 1


class TestReconcileDeadPositionNoClose:
    """Reconcile 死亡仓位保持OPEN而非自动核销"""

    def _make_tracker(self):
        from src.simulation.outcome_tracker import OutcomeTracker

        tracker = OutcomeTracker.__new__(OutcomeTracker)
        tracker.session = MagicMock()
        return tracker

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_dead_position_not_closed(self, mock_get, mock_db):
        """curPrice <= 0.001 时不自动核销，保持OPEN"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "position_management": {
                "dead_position_no_reopen_price": 0.01,
            },
        }.get(key, None)

        tracker = self._make_tracker()
        # Mock platform positions: one dead position
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "conditionId": "cid_dead",
                "size": 5.0,
                "curPrice": 0.0005,
                "title": "Test Dead Position",
                "outcome": "YES",
                "redeemable": False,
            }
        ]
        tracker.session.get.return_value = mock_resp

        # Mock DB: open bet with same conditionId
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "dead-bet-001",
                "condition_id": "cid_dead",
                "market_id": "mkt_dead",
                "outcome": "YES",
                "matched_shares": 5.0,
                "size_usdc": 2.50,
                "market_price": 0.50,
                "ts_entered": time.time() - 3600,
            }
        ]
        mock_db.get_recently_closed_bets.return_value = []

        result = tracker.check_platform_reconciliation("0xwallet")

        # close_paper_bet_stop_loss should NOT be called (no more auto-close)
        mock_db.close_paper_bet_stop_loss.assert_not_called()


class TestReconcileNoReopenDeadPrice:
    """Reconcile 死亡价格禁止重开已关闭仓位"""

    def _make_tracker(self):
        from src.simulation.outcome_tracker import OutcomeTracker

        tracker = OutcomeTracker.__new__(OutcomeTracker)
        tracker.session = MagicMock()
        return tracker

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_no_reopen_when_price_below_threshold(self, mock_get, mock_db):
        """curPrice <= 0.01 + resolved_outcome → 禁止重开"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "position_management": {
                "dead_position_no_reopen_price": 0.01,
                "reconcile_writeoff_reopen_value": 0.10,
                "reconcile_exit_reopen_value": 0.50,
            },
        }.get(key, None)

        tracker = self._make_tracker()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "conditionId": "cid_ghost",
                "size": 5.0,
                "curPrice": 0.005,  # <= 0.01
                "title": "Dead Ghost",
                "outcome": "YES",
                "redeemable": False,
            }
        ]
        tracker.session.get.return_value = mock_resp

        mock_db.get_open_paper_bets.return_value = []
        mock_db.get_recently_closed_bets.return_value = [
            {
                "bet_id": "ghost-bet-001",
                "condition_id": "cid_ghost",
                "market_id": "mkt_ghost",
                "resolved_outcome": "STOP_LOSS",
                "exit_type": "stop_loss",
                "outcome": "YES",
                "ts_entered": time.time() - 7200,
            }
        ]

        result = tracker.check_platform_reconciliation("0xwallet")

        # reopen_ghost_position should NOT be called
        mock_db.reopen_ghost_position.assert_not_called()
        assert result["ghost_reopened"] == 0


class TestT176EmptyApiGuard:
    """T176: Data API 返回空列表时禁止批量误判手动卖出。

    事故: 2026-04-10 Data API 临时返回 0 个仓位，
    reconcile 将 8 个 OPEN 仓位全部错误关闭为 manual_exit。
    """

    def _make_tracker(self):
        from src.simulation.outcome_tracker import OutcomeTracker

        tracker = OutcomeTracker.__new__(OutcomeTracker)
        tracker.session = MagicMock()
        return tracker

    @patch("src.utils.db.close_paper_bet_manual_exit")
    @patch("src.utils.db.get_recently_closed_bets", return_value=[])
    @patch("src.utils.db.get_open_paper_bets")
    @patch("src.simulation.outcome_tracker.get")
    def test_empty_api_response_skips_manual_sell_detection(
        self, mock_get, mock_open_bets, _mock_closed, mock_close_exit
    ):
        """Data API 返回 [] 但 DB 有 OPEN 仓位 → 跳过手动卖出检测"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "position_management": {
                "dead_position_no_reopen_price": 0.01,
                "reconcile_writeoff_reopen_value": 0.10,
                "reconcile_exit_reopen_value": 0.50,
            },
        }.get(key, None)

        tracker = self._make_tracker()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []  # 空响应 — API 临时故障
        tracker.session.get.return_value = mock_resp

        # DB 有 5 个 OPEN 仓位（来自 copy_trade）
        mock_open_bets.return_value = [
            {
                "bet_id": f"copy_bet_{i:03d}",
                "condition_id": f"0xcond_{i:03d}",
                "market_id": f"mkt_{i:03d}",
                "outcome": "YES",
                "matched_shares": 5.0,
                "size_usdc": 2.50,
                "market_price": 0.50,
                "ts_entered": time.time() - 3600,
                "source": "copy_trade",
            }
            for i in range(5)
        ]

        result = tracker.check_platform_reconciliation("0xwallet")

        # 手动卖出检测不应被调用
        mock_close_exit.assert_not_called()
        assert result.get("manual_sell_closed", 0) == 0

    @patch("src.utils.db.close_paper_bet_manual_exit")
    @patch("src.utils.db.get_recently_closed_bets", return_value=[])
    @patch("src.utils.db.get_open_paper_bets")
    @patch("src.simulation.outcome_tracker.get")
    def test_suspicious_drop_skips_manual_sell_detection(
        self, mock_get, mock_open_bets, _mock_closed, mock_close_exit
    ):
        """平台仓位骤降 >50% → 跳过手动卖出检测"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "position_management": {
                "dead_position_no_reopen_price": 0.01,
                "reconcile_writeoff_reopen_value": 0.10,
                "reconcile_exit_reopen_value": 0.50,
            },
        }.get(key, None)

        tracker = self._make_tracker()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # 平台只返回 1 个仓位（严重不完整）
        mock_resp.json.return_value = [
            {
                "conditionId": "0xcond_000",
                "size": 5.0,
                "curPrice": 0.50,
                "title": "Survivor",
                "outcome": "YES",
                "redeemable": False,
            }
        ]
        tracker.session.get.return_value = mock_resp

        # DB 有 6 个 OPEN 仓位 — 平台 1 个 远 < 50%
        mock_open_bets.return_value = [
            {
                "bet_id": f"copy_bet_{i:03d}",
                "condition_id": f"0xcond_{i:03d}",
                "market_id": f"mkt_{i:03d}",
                "outcome": "YES",
                "matched_shares": 5.0,
                "size_usdc": 2.50,
                "market_price": 0.50,
                "ts_entered": time.time() - 3600,
                "source": "copy_trade",
            }
            for i in range(6)
        ]

        result = tracker.check_platform_reconciliation("0xwallet")

        # 手动卖出检测不应被调用
        mock_close_exit.assert_not_called()
        assert result.get("manual_sell_closed", 0) == 0

    @patch("src.utils.db.close_paper_bet_manual_exit")
    @patch("src.utils.db.get_recently_closed_bets", return_value=[])
    @patch("src.utils.db.get_open_paper_bets")
    @patch("src.simulation.outcome_tracker.get")
    def test_normal_manual_sell_still_works(
        self, mock_get, mock_open_bets, _mock_closed, mock_close_exit
    ):
        """正常场景：平台确实少了 1 个仓位 → 正确检测手动卖出"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "position_management": {
                "dead_position_no_reopen_price": 0.01,
                "reconcile_writeoff_reopen_value": 0.10,
                "reconcile_exit_reopen_value": 0.50,
            },
        }.get(key, None)

        tracker = self._make_tracker()

        # session.get 需要根据 URL 返回不同结果
        def _side_effect_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "data-api.polymarket.com/positions" in url:
                # 平台有 2 个仓位
                resp.json.return_value = [
                    {
                        "conditionId": "0xcond_001",
                        "size": 5.0,
                        "curPrice": 0.50,
                        "title": "Still Open 1",
                        "outcome": "YES",
                        "redeemable": False,
                    },
                    {
                        "conditionId": "0xcond_002",
                        "size": 5.0,
                        "curPrice": 0.50,
                        "title": "Still Open 2",
                        "outcome": "YES",
                        "redeemable": False,
                    },
                ]
            elif "gamma-api.polymarket.com/markets" in url:
                # 市场仍活跃
                resp.json.return_value = {"active": True, "closed": False, "tokens": []}
            else:
                resp.json.return_value = {}
            return resp

        tracker.session.get.side_effect = _side_effect_get

        # DB 有 3 个 OPEN 仓位 — 其中 0xcond_003 不在平台上（手动卖出）
        mock_open_bets.return_value = [
            {
                "bet_id": "copy_bet_001",
                "condition_id": "0xcond_001",
                "outcome": "YES",
                "matched_shares": 5.0,
                "size_usdc": 2.50,
                "market_price": 0.50,
                "ts_entered": time.time() - 3600,
                "source": "copy_trade",
            },
            {
                "bet_id": "copy_bet_002",
                "condition_id": "0xcond_002",
                "outcome": "YES",
                "matched_shares": 5.0,
                "size_usdc": 2.50,
                "market_price": 0.50,
                "ts_entered": time.time() - 3600,
                "source": "copy_trade",
            },
            {
                "bet_id": "copy_bet_003",
                "condition_id": "0xcond_003",
                "outcome": "YES",
                "matched_shares": 5.0,
                "size_usdc": 2.50,
                "market_price": 0.50,
                "ts_entered": time.time() - 3600,
                "source": "copy_trade",
                "market_id": "mkt_003",
            },
        ]

        result = tracker.check_platform_reconciliation("0xwallet")

        # 应该检测到 copy_bet_003 被手动卖出
        mock_close_exit.assert_called_once()
        assert result.get("manual_sell_closed", 0) == 1
