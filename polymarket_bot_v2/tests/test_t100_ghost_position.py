"""
test_t100_ghost_position.py — T100 幽灵仓位修复验证

根因：卖出成功后链上残留 dust (e.g. 0.007 shares)，
position_syncer active_cids 不过滤 dust，导致 DB 永远不关闭。
修复：active_cids 构建时排除 size < min_order_shares 的 dust 仓位。
"""

import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Test 1: position_syncer active_cids 过滤 dust
# ---------------------------------------------------------------------------


class TestDustPositionDetection(unittest.TestCase):
    """验证 position_syncer._do_sync 将 dust 仓位排除出 active_cids，
    使 _detect_manual_exits 能正确检测到手动退出。"""

    def _make_chain_position(
        self, cid: str, size: float, redeemable: bool = False
    ) -> dict:
        return {
            "conditionId": cid,
            "size": str(size),
            "redeemable": redeemable,
            "title": "Test Market",
            "slug": "test-market",
            "outcome": "YES",
            "avgPrice": "0.5",
            "initialValue": str(size * 0.5),
        }

    @patch("src.sync.position_syncer.get")
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    def test_dust_excluded_from_active_cids(self, mock_dry, mock_db, mock_get):
        """链上 dust 仓位 (size < min_order_shares) 不应出现在 active_cids 中。"""
        from src.sync.position_syncer import PositionSyncer

        syncer = PositionSyncer(executor=MagicMock())

        # 模拟链上持仓：一个正常仓位 + 一个 dust 仓位
        positions = [
            self._make_chain_position("cid_normal", 10.0),
            self._make_chain_position("cid_dust", 0.007),  # dust
        ]

        mock_get.side_effect = lambda key, *args: {
            "sync.enabled": True,
            "sync.min_position_shares": 0.01,
            "sync.min_position_usdc": 0.10,
            "executor.min_order_shares": 5.0,
            "reconcile.min_age_seconds": 300,
        }.get(key, None)

        mock_db.get_tracked_condition_ids.return_value = set()
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "bet_dust_123",
                "question": "Test dust bet",
                "outcome": "YES",
                "condition_id": "cid_dust",
                "source": "auto",
                "market_end_date": "2099-12-31T23:59:59Z",  # 远未到期
                "market_price": 0.5,
                "size_usdc": 4.0,
                "matched_shares": 0.007,
                "market_slug": "test-slug",
                "market_id": "12345",
            }
        ]
        mock_db.connect.return_value.__enter__ = MagicMock()
        mock_db.connect.return_value.__exit__ = MagicMock()

        with patch.object(syncer, "_fetch_real_positions", return_value=positions):
            with patch.object(syncer, "_detect_manual_exits") as mock_detect:
                with patch.object(syncer, "_reconcile_auto_positions"):
                    syncer._do_sync()

                    # 验证 _detect_manual_exits 被调用时，active_cids 不包含 dust CID
                    mock_detect.assert_called_once()
                    active_cids = mock_detect.call_args[0][0]
                    self.assertIn("cid_normal", active_cids)
                    self.assertNotIn("cid_dust", active_cids)

    @patch("src.sync.position_syncer.get")
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    def test_normal_position_stays_in_active_cids(self, mock_dry, mock_db, mock_get):
        """正常仓位 (size >= min_order_shares) 仍在 active_cids 中。"""
        from src.sync.position_syncer import PositionSyncer

        syncer = PositionSyncer(executor=MagicMock())

        positions = [
            self._make_chain_position("cid_normal_1", 10.0),
            self._make_chain_position("cid_normal_2", 5.0),  # 恰好等于阈值
        ]

        mock_get.side_effect = lambda key, *args: {
            "sync.enabled": True,
            "sync.min_position_shares": 0.01,
            "sync.min_position_usdc": 0.10,
            "executor.min_order_shares": 5.0,
            "reconcile.min_age_seconds": 300,
        }.get(key, None)

        mock_db.get_tracked_condition_ids.return_value = set()
        mock_db.get_open_paper_bets.return_value = []

        with patch.object(syncer, "_fetch_real_positions", return_value=positions):
            with patch.object(syncer, "_detect_manual_exits") as mock_detect:
                with patch.object(syncer, "_reconcile_auto_positions"):
                    syncer._do_sync()

                    active_cids = mock_detect.call_args[0][0]
                    self.assertIn("cid_normal_1", active_cids)
                    self.assertIn("cid_normal_2", active_cids)

    @patch("src.sync.position_syncer.get")
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    def test_redeemable_still_excluded(self, mock_dry, mock_db, mock_get):
        """redeemable 仓位无论 size 大小都应被排除。"""
        from src.sync.position_syncer import PositionSyncer

        syncer = PositionSyncer(executor=MagicMock())

        positions = [
            self._make_chain_position("cid_redeem", 100.0, redeemable=True),
            self._make_chain_position("cid_active", 10.0, redeemable=False),
        ]

        mock_get.side_effect = lambda key, *args: {
            "sync.enabled": True,
            "sync.min_position_shares": 0.01,
            "sync.min_position_usdc": 0.10,
            "executor.min_order_shares": 5.0,
            "reconcile.min_age_seconds": 300,
        }.get(key, None)

        mock_db.get_tracked_condition_ids.return_value = set()
        mock_db.get_open_paper_bets.return_value = []

        with patch.object(syncer, "_fetch_real_positions", return_value=positions):
            with patch.object(syncer, "_detect_manual_exits") as mock_detect:
                with patch.object(syncer, "_reconcile_auto_positions"):
                    syncer._do_sync()

                    active_cids = mock_detect.call_args[0][0]
                    self.assertNotIn("cid_redeem", active_cids)
                    self.assertIn("cid_active", active_cids)


# ---------------------------------------------------------------------------
# Test 2: TopUp 日志降噪
# ---------------------------------------------------------------------------


class TestTopUpLogDedup(unittest.TestCase):
    """验证 TopUp expiry-skip 日志不会每分钟重复打印。"""

    def test_topup_expiry_logged_set_exists(self):
        """main.py 中应有 _topup_expiry_logged set 定义。"""
        import pathlib

        main_src = (
            pathlib.Path(__file__).resolve().parent.parent / "main.py"
        ).read_text()
        self.assertIn(
            "_topup_expiry_logged",
            main_src,
            "_topup_expiry_logged 应在 main.py 中定义",
        )
        self.assertIn(
            "_topup_expiry_logged: set = set()",
            main_src,
            "_topup_expiry_logged 应初始化为空 set",
        )

    def test_topup_expiry_log_uses_dedup(self):
        """TopUp expiry-skip 路径使用 _topup_expiry_logged 防止重复日志。"""
        import pathlib

        main_src = (
            pathlib.Path(__file__).resolve().parent.parent / "main.py"
        ).read_text()
        self.assertIn(
            "_bid not in _topup_expiry_logged",
            main_src,
            "TopUp expiry-skip 应检查 dedup set",
        )


# ---------------------------------------------------------------------------
# Test 3: 端到端验证 — dust 仓位被正确关闭
# ---------------------------------------------------------------------------


class TestDustEndToEnd(unittest.TestCase):
    """端到端验证：链上 dust → detect_manual_exits → close DB。"""

    @patch("src.sync.position_syncer.get")
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    @patch("src.sync.position_syncer.requests")
    def test_dust_bet_closed_by_detect_manual_exits(
        self, mock_requests, mock_dry, mock_db, mock_get
    ):
        """dust 仓位应被 _detect_manual_exits 标记为手动退出并关闭。"""
        from src.sync.position_syncer import PositionSyncer

        # 设置 mock_get
        mock_get.side_effect = lambda key, *args: {
            "executor.min_order_shares": 5.0,
            "reconcile.min_age_seconds": 300,
        }.get(key, None)

        # DB 中有一个 OPEN auto 仓位（dust），老于 min_age
        dust_bet = {
            "bet_id": "27dda0d5-test-dust",
            "question": "Will the highest temperature in Chicago...",
            "outcome": "NO",
            "condition_id": "0xdust_cid_test",
            "source": "auto",
            "market_end_date": "2099-12-31T23:59:59Z",  # 远未到期
            "market_price": 0.71,
            "size_usdc": 0.005,
            "matched_shares": 0.007,
            "market_slug": "",
            "market_id": "999",
            "ts_entered": time.time() - 86400,  # 1天前
        }
        mock_db.get_open_paper_bets.return_value = [dust_bet]

        # active_cids 为空（因为 dust 被过滤了）
        chain_cids: set[str] = set()

        # Mock Gamma API 返回已结算
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "resolved": False,
            "outcomePrices": '["0.7", "0.3"]',
        }
        mock_requests.get.return_value = mock_resp

        exited = PositionSyncer._detect_manual_exits(chain_cids)

        # 应该关闭这个 dust bet
        self.assertIn("27dda0d5-test-dust", exited)
        mock_db.close_paper_bet_manual_exit.assert_called_once()
        call_kwargs = mock_db.close_paper_bet_manual_exit.call_args
        self.assertEqual(
            (
                call_kwargs[1]["bet_id"]
                if "bet_id" in (call_kwargs[1] or {})
                else (
                    call_kwargs[0][0]
                    if call_kwargs[0]
                    else call_kwargs[1].get("bet_id")
                )
            ),
            "27dda0d5-test-dust",
        )

    @patch("src.sync.position_syncer.get")
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    @patch("src.sync.position_syncer.requests")
    def test_dust_bet_not_closed_if_market_expired(
        self, mock_requests, mock_dry, mock_db, mock_get
    ):
        """市场已过期的 dust → 跳过（由结算系统处理），不标记手动退出。"""
        from src.sync.position_syncer import PositionSyncer

        mock_get.side_effect = lambda key, *args: {
            "executor.min_order_shares": 5.0,
            "reconcile.min_age_seconds": 300,
        }.get(key, None)

        dust_bet = {
            "bet_id": "expired-dust-bet",
            "question": "Expired market dust",
            "outcome": "NO",
            "condition_id": "0xexpired_cid",
            "source": "auto",
            "market_end_date": "2020-01-01T00:00:00Z",  # 已过期
            "market_price": 0.71,
            "size_usdc": 0.005,
            "matched_shares": 0.007,
            "ts_entered": time.time() - 86400,
        }
        mock_db.get_open_paper_bets.return_value = [dust_bet]

        chain_cids: set[str] = set()
        exited = PositionSyncer._detect_manual_exits(chain_cids)

        # 已过期市场不应被标记为手动退出
        self.assertEqual(len(exited), 0)
        mock_db.close_paper_bet_manual_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
