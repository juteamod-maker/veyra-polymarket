"""
test_chain_verify_before_close.py — SUPREME 1: 关闭DB前链上余额验证

验证 _verify_chain_position_cleared 在所有sell→close路径中的行为。
"""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest


@pytest.fixture
def mock_executor():
    """Mock _executor with chain balance query."""
    with patch("main._executor") as m:
        client = MagicMock()
        m._get_clob_client.return_value = client
        m._refresh_and_get_token_balance.return_value = 0.0  # 默认清零
        yield m


@pytest.fixture
def mock_env():
    """Standard mocks for non-dry-run env."""
    with (
        patch("main.is_dry_run", return_value=False),
        patch(
            "main.get",
            side_effect=lambda k, d=None: {
                "executor.sweep_min_shares": 0.01,
                "reconcile.chain_verify_retries": 3,
                "reconcile.chain_verify_retry_delay_sec": 0.01,
            }.get(k, d),
        ),
    ):
        yield


class TestVerifyChainPositionCleared:
    """SUPREME 1: _verify_chain_position_cleared 单元测试。"""

    def test_allows_close_when_balance_zero(self, mock_executor, mock_env):
        """链上余额=0 → 允许关闭DB。"""
        from main import _verify_chain_position_cleared

        mock_executor._refresh_and_get_token_balance.return_value = 0.0
        bet = {
            "bet_id": "test_001",
            "market_id": "mkt_1",
            "outcome": "YES",
            "condition_id": "0xcond1",
        }
        market_data = {"mkt_1": {"tokens": [{"outcome": "Yes", "token_id": "tok_yes"}]}}
        assert _verify_chain_position_cleared(bet, market_data) is True

    def test_blocks_close_when_balance_remaining(self, mock_executor, mock_env):
        """链上余额=5.0 → 禁止关闭DB。"""
        from main import _verify_chain_position_cleared

        mock_executor._refresh_and_get_token_balance.return_value = 5.0
        bet = {
            "bet_id": "test_002",
            "market_id": "mkt_1",
            "outcome": "NO",
            "condition_id": "0xcond1",
        }
        market_data = {"mkt_1": {"tokens": [{"outcome": "No", "token_id": "tok_no"}]}}
        assert _verify_chain_position_cleared(bet, market_data) is False

    def test_blocks_close_on_query_failure(self, mock_executor, mock_env):
        """链上查询失败(返回-1) → 保守禁止关闭DB。"""
        from main import _verify_chain_position_cleared

        mock_executor._refresh_and_get_token_balance.return_value = -1.0
        bet = {
            "bet_id": "test_003",
            "market_id": "mkt_1",
            "outcome": "YES",
            "condition_id": "0xcond1",
        }
        market_data = {"mkt_1": {"tokens": [{"outcome": "Yes", "token_id": "tok_yes"}]}}
        assert _verify_chain_position_cleared(bet, market_data) is False

    def test_blocks_close_on_exception(self, mock_executor, mock_env):
        """链上查询异常 → 保守禁止关闭DB。"""
        from main import _verify_chain_position_cleared

        mock_executor._get_clob_client.side_effect = Exception("connection error")
        bet = {
            "bet_id": "test_004",
            "market_id": "mkt_1",
            "outcome": "YES",
            "condition_id": "0xcond1",
        }
        market_data = {"mkt_1": {"tokens": [{"outcome": "Yes", "token_id": "tok_yes"}]}}
        assert _verify_chain_position_cleared(bet, market_data) is False

    def test_allows_close_with_dust_balance(self, mock_executor, mock_env):
        """链上余额=0.005 < sweep_min=0.01 → 允许关闭。"""
        from main import _verify_chain_position_cleared

        mock_executor._refresh_and_get_token_balance.return_value = 0.005
        bet = {
            "bet_id": "test_005",
            "market_id": "mkt_1",
            "outcome": "YES",
            "condition_id": "0xcond1",
        }
        market_data = {"mkt_1": {"tokens": [{"outcome": "Yes", "token_id": "tok_yes"}]}}
        assert _verify_chain_position_cleared(bet, market_data) is True

    def test_dry_run_always_allows(self):
        """DRY RUN → 始终允许关闭。"""
        with patch("main.is_dry_run", return_value=True):
            from main import _verify_chain_position_cleared

            bet = {"bet_id": "test_dry", "market_id": "m", "outcome": "YES"}
            assert _verify_chain_position_cleared(bet) is True

    def test_no_token_id_blocks_close(self, mock_executor, mock_env):
        """T67: 无法获取token_id → 禁止关闭DB（SUPREME1修复，防止绕过链上验证）。"""
        from main import _verify_chain_position_cleared

        bet = {
            "bet_id": "test_006",
            "market_id": "mkt_unknown",
            "outcome": "YES",
            "condition_id": "",
        }
        # 无 market_data 匹配 + 无 condition_id → 无法解析 token_id → False
        assert _verify_chain_position_cleared(bet, {}) is False

    def test_retry_on_query_failure_then_success(self, mock_executor, mock_env):
        """T67: 前2次查询失败(-1), 第3次成功(0.0) → 允许关闭。"""
        from main import _verify_chain_position_cleared

        mock_executor._refresh_and_get_token_balance.side_effect = [-1.0, -1.0, 0.0]
        bet = {
            "bet_id": "test_retry_ok",
            "market_id": "mkt_1",
            "outcome": "YES",
            "condition_id": "0xcond1",
        }
        market_data = {"mkt_1": {"tokens": [{"outcome": "Yes", "token_id": "tok_yes"}]}}
        assert _verify_chain_position_cleared(bet, market_data) is True
        assert mock_executor._refresh_and_get_token_balance.call_count == 3

    def test_retry_all_failures_blocks_close(self, mock_executor, mock_env):
        """T67: 3次查询全部失败(-1) → 禁止关闭。"""
        from main import _verify_chain_position_cleared

        mock_executor._refresh_and_get_token_balance.return_value = -1.0
        bet = {
            "bet_id": "test_retry_fail",
            "market_id": "mkt_1",
            "outcome": "YES",
            "condition_id": "0xcond1",
        }
        market_data = {"mkt_1": {"tokens": [{"outcome": "Yes", "token_id": "tok_yes"}]}}
        assert _verify_chain_position_cleared(bet, market_data) is False
        assert mock_executor._refresh_and_get_token_balance.call_count == 3

    def test_retry_on_exception_then_success(self, mock_executor, mock_env):
        """T67: 前2次异常, 第3次余额=0 → 允许关闭。"""
        from main import _verify_chain_position_cleared

        mock_executor._refresh_and_get_token_balance.side_effect = [
            Exception("timeout"),
            Exception("conn reset"),
            0.0,
        ]
        bet = {
            "bet_id": "test_exc_retry",
            "market_id": "mkt_1",
            "outcome": "YES",
            "condition_id": "0xcond1",
        }
        market_data = {"mkt_1": {"tokens": [{"outcome": "Yes", "token_id": "tok_yes"}]}}
        assert _verify_chain_position_cleared(bet, market_data) is True

    def test_retry_all_exceptions_blocks_close(self, mock_executor, mock_env):
        """T67: 3次全部异常 → 禁止关闭。"""
        from main import _verify_chain_position_cleared

        mock_executor._refresh_and_get_token_balance.side_effect = Exception("fail")
        bet = {
            "bet_id": "test_all_exc",
            "market_id": "mkt_1",
            "outcome": "YES",
            "condition_id": "0xcond1",
        }
        market_data = {"mkt_1": {"tokens": [{"outcome": "Yes", "token_id": "tok_yes"}]}}
        assert _verify_chain_position_cleared(bet, market_data) is False

    def test_first_attempt_has_balance_returns_false_immediately(
        self, mock_executor, mock_env
    ):
        """T67: 第1次查询就发现有余额 → 立即返回False（不重试）。"""
        from main import _verify_chain_position_cleared

        mock_executor._refresh_and_get_token_balance.return_value = 5.0
        bet = {
            "bet_id": "test_has_bal",
            "market_id": "mkt_1",
            "outcome": "YES",
            "condition_id": "0xcond1",
        }
        market_data = {"mkt_1": {"tokens": [{"outcome": "Yes", "token_id": "tok_yes"}]}}
        assert _verify_chain_position_cleared(bet, market_data) is False
        # 有余额时不需要重试，首次查询即返回
        assert mock_executor._refresh_and_get_token_balance.call_count == 1
