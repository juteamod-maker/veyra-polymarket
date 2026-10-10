"""
test_post_buy_topup.py — 买入后自动补齐至 min_order_size 的单元测试

覆盖场景：
  1. matched_shares < min → 自动补买到 min + buffer
  2. matched_shares >= min → 不补买
  3. DRY RUN → 跳过
  4. 补买失败 → 重试直到 max_retries
  5. 余额不足 → 跳过
  6. 补买成本超限 → 跳过
  7. 功能开关关闭 → 跳过
  8. DB 更新正确性
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# _post_buy_topup 内部使用 from ... import get_min_order_size（本地导入），
# conftest 已全局 mock src.utils.orderbook_history.get_min_order_size，
# 所以在测试中用同一 target override 即可。
_MOS_TARGET = "src.utils.orderbook_history.get_min_order_size"


def _make_result(matched_shares=4.5, filled_size=2.25, actual_price=0.50):
    """构造 OrderResult mock。"""
    from src.signal.signal_engine import Signal

    sig = Signal(
        market_id="mkt_test",
        question="Test?",
        outcome="NO",
        ev=0.10,
        confidence=0.65,
        size_usdc=2.50,
    )
    sig.expected_price = 0.50

    r = MagicMock()
    r.signal = sig
    r.success = True
    r.matched_shares = matched_shares
    r.filled_size = filled_size
    r.actual_price = actual_price
    r.condition_id = "0xcond_test"
    return r


def _make_bet_record(bet_id="test_topup_01", shares=4.5, size=2.25, price=0.50):
    return {
        "bet_id": bet_id,
        "market_id": "mkt_test",
        "question": "Will it rain?",
        "outcome": "NO",
        "market_price": price,
        "size_usdc": size,
        "condition_id": "0xcond_test",
        "matched_shares": shares,
    }


class TestPostBuyTopUp:
    """买入后自动补齐机制测试。"""

    # ── 辅助：统一构建 config mock ──────────────────────────
    @staticmethod
    def _cfg(overrides=None):
        defaults = {
            "executor.post_buy_topup_enabled": True,
            "executor.post_buy_topup_max_retries": 3,
            "executor.post_buy_topup_buffer_shares": 0.8,
            "executor.post_buy_topup_safety_floor_shares": 5.05,
            "executor.min_order_size_shares": 5.0,
            "executor.max_topup_usdc": 5.0,
            "executor.max_topup_amplification": 50.0,  # T215 fix: allow amplification in tests
        }
        if overrides:
            defaults.update(overrides)
        return lambda key, default=None: defaults.get(key, default)

    # ── 1. shares < min → 自动补买 ─────────────────────────
    @patch("main.connect")
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_topup_when_shares_below_min(
        self, _mos, mock_get, _dry, mock_exec, mock_conn
    ):
        """shares=4.5 < target=5.8 → 自动补买。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()
        mock_exec._check_balance.return_value = 10.0

        topup_result = MagicMock()
        topup_result.success = True
        topup_result.matched_shares = 1.5
        topup_result.filled_size = 0.75
        mock_exec.execute.return_value = topup_result

        result = _make_result(matched_shares=4.5)
        bet = _make_bet_record(shares=4.5)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)

        mock_exec.execute.assert_called()
        mock_conn.return_value.__enter__.return_value.execute.assert_called()

    # ── 2. shares >= min → 不补买 ──────────────────────────
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_no_topup_when_shares_sufficient(self, _mos, mock_get, _dry, mock_exec):
        """shares=6.0 >= min=5 → 不需要补买。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()

        result = _make_result(matched_shares=6.0)
        bet = _make_bet_record(shares=6.0)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)
        mock_exec.execute.assert_not_called()

    # ── 3. DRY RUN → 跳过 ─────────────────────────────────
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=True)
    def test_skip_in_dry_run(self, _dry, mock_exec):
        """DRY RUN → 跳过。"""
        from main import _post_buy_topup

        result = _make_result(matched_shares=4.0)
        bet = _make_bet_record(shares=4.0)
        market = {}

        _post_buy_topup(bet, result, market)
        mock_exec.execute.assert_not_called()

    # ── 4. 功能关闭 → 跳过 ────────────────────────────────
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    def test_skip_when_disabled(self, mock_get, _dry, mock_exec):
        """功能开关关闭 → 跳过。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg({"executor.post_buy_topup_enabled": False})

        result = _make_result(matched_shares=4.0)
        bet = _make_bet_record(shares=4.0)
        market = {}

        _post_buy_topup(bet, result, market)
        mock_exec.execute.assert_not_called()

    # ── 5. 余额不足 → 跳过 ────────────────────────────────
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_skip_when_balance_insufficient(self, _mos, mock_get, _dry, mock_exec):
        """余额不足 → 跳过。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()
        mock_exec._check_balance.return_value = 0.20  # Too low

        result = _make_result(matched_shares=4.5)
        bet = _make_bet_record(shares=4.5)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)
        mock_exec.execute.assert_not_called()

    # ── 6. 补买成本超限 → 跳过 ────────────────────────────
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_skip_when_cost_exceeds_limit(self, _mos, mock_get, _dry, mock_exec):
        """补买成本超限 → 跳过。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg({"executor.max_topup_usdc": 0.10})

        result = _make_result(matched_shares=1.0)  # Need 4.5 more → expensive
        bet = _make_bet_record(shares=1.0, price=0.80)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)
        mock_exec.execute.assert_not_called()

    # ── 7. 全部重试失败 → 保持原样 ───────────────────────
    @patch("main.connect")
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_retries_on_failure(self, _mos, mock_get, _dry, mock_exec, mock_conn):
        """补买失败 → 重试 max_retries 次，DB 不更新。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()
        mock_exec._check_balance.return_value = 10.0

        fail_result = MagicMock()
        fail_result.success = False
        fail_result.error = "TIMEOUT"
        fail_result.matched_shares = 0
        fail_result.filled_size = 0
        mock_exec.execute.return_value = fail_result

        result = _make_result(matched_shares=4.5)
        bet = _make_bet_record(shares=4.5)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)

        assert mock_exec.execute.call_count == 3
        mock_conn.return_value.__enter__.return_value.execute.assert_not_called()

    # ── 8. 部分补买 + 第2次成功 → DB 更新合并 ────────────
    @patch("main.connect")
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_partial_topup_then_success(
        self, _mos, mock_get, _dry, mock_exec, mock_conn
    ):
        """部分补买 + 逐步补满 → DB 更新合并值。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()
        mock_exec._check_balance.return_value = 10.0

        partial = MagicMock()
        partial.success = True
        partial.matched_shares = 0.5
        partial.filled_size = 0.25

        full = MagicMock()
        full.success = True
        full.matched_shares = 0.5
        full.filled_size = 0.25

        final = MagicMock()
        final.success = True
        final.matched_shares = 0.5
        final.filled_size = 0.25

        mock_exec.execute.side_effect = [partial, full, final]

        result = _make_result(matched_shares=3.5)  # 需 3 轮 ×0.5 补齐到 target=5.0
        bet = _make_bet_record(shares=3.5)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)

        assert mock_exec.execute.call_count == 3
        mock_conn.return_value.__enter__.return_value.execute.assert_called()

    # ── 9. matched_shares = 0 → 跳过 ─────────────────────
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    def test_skip_when_matched_shares_zero(self, mock_get, _dry, mock_exec):
        """matched_shares=0 → 跳过。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()

        result = _make_result(matched_shares=0)
        bet = _make_bet_record(shares=0)
        market = {}

        _post_buy_topup(bet, result, market)
        mock_exec.execute.assert_not_called()


class TestT68BuySizeEnforce:
    """T68: BuySizeStrictEnforce — target = min_shares (简化版，不再使用 safety_floor + buffer)。"""

    @staticmethod
    def _cfg(overrides=None):
        defaults = {
            "executor.post_buy_topup_enabled": True,
            "executor.post_buy_topup_max_retries": 3,
            "executor.post_buy_topup_buffer_shares": 0.8,
            "executor.post_buy_topup_safety_floor_shares": 5.05,
            "executor.min_order_size_shares": 5.0,
            "executor.max_topup_usdc": 10.0,
            "executor.max_topup_amplification": 50.0,  # 放开放大限制，专注测试 target 逻辑
        }
        if overrides:
            defaults.update(overrides)
        return lambda key, default=None: defaults.get(key, default)

    # ── T68-1: safety_floor 优先于 min+buffer ──────────────
    @patch("main.connect")
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_safety_floor_takes_priority(
        self, _mos, mock_get, _dry, mock_exec, mock_conn
    ):
        """safety_floor=6.0 > min(5)+buffer(0.8)=5.8 → target=6.0。
        shares=5.5 < 6.0 → 需补买。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg(
            {
                "executor.post_buy_topup_safety_floor_shares": 6.0,
            }
        )
        mock_exec._check_balance.return_value = 10.0

        topup_result = MagicMock()
        topup_result.success = True
        topup_result.matched_shares = 0.5
        topup_result.filled_size = 0.25
        mock_exec.execute.return_value = topup_result

        result = _make_result(matched_shares=4.5)
        bet = _make_bet_record(shares=4.5)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)
        mock_exec.execute.assert_called()

    # ── T68-2: min+buffer 优先于 safety_floor ──────────────
    @patch("main.connect")
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_min_plus_buffer_takes_priority(
        self, _mos, mock_get, _dry, mock_exec, mock_conn
    ):
        """safety_floor=5.05, min(5)+buffer(1.5)=6.5 → target=6.5。
        shares=5.5 < 6.5 → 需补买。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg(
            {
                "executor.post_buy_topup_buffer_shares": 1.5,
                "executor.post_buy_topup_safety_floor_shares": 5.05,
            }
        )
        mock_exec._check_balance.return_value = 10.0

        topup_result = MagicMock()
        topup_result.success = True
        topup_result.matched_shares = 1.0
        topup_result.filled_size = 0.50
        mock_exec.execute.return_value = topup_result

        result = _make_result(matched_shares=5.5)
        bet = _make_bet_record(shares=5.5)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)
        # matched=5.5 >= target=min_shares=5.0 → 不需补买（buffer 不再影响 target）
        mock_exec.execute.assert_not_called()

    # ── T68-3: shares >= target → 不补买 ──────────────────
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_no_topup_when_above_target(self, _mos, mock_get, _dry, mock_exec):
        """shares=5.9 >= target=max(5.05, 5.8)=5.8 → 不补买。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()

        result = _make_result(matched_shares=5.9)
        bet = _make_bet_record(shares=5.9)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)
        mock_exec.execute.assert_not_called()

    # ── T68-4: API不可用时使用config fallback ──────────────
    @patch("main.connect")
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=None)
    def test_config_fallback_when_api_unavailable(
        self, _mos, mock_get, _dry, mock_exec, mock_conn
    ):
        """API returns None → 使用 config min_order_size_shares=5.0 作为回退。
        shares=4.5 < target=5.8 → 需补买。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()
        mock_exec._check_balance.return_value = 10.0

        topup_result = MagicMock()
        topup_result.success = True
        topup_result.matched_shares = 1.3
        topup_result.filled_size = 0.65
        mock_exec.execute.return_value = topup_result

        result = _make_result(matched_shares=4.5)
        bet = _make_bet_record(shares=4.5)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)
        mock_exec.execute.assert_called()

    # ── T68-5: 边界值 matched_shares == 5.0 → 仍需补买 ──
    @patch("main.connect")
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_topup_at_exact_min(self, _mos, mock_get, _dry, mock_exec, mock_conn):
        """shares==5.0 (刚好等于min) < target=5.8 → 仍需补买。
        这正是7648bcbc的根因场景。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()
        mock_exec._check_balance.return_value = 10.0

        topup_result = MagicMock()
        topup_result.success = True
        topup_result.matched_shares = 0.8
        topup_result.filled_size = 0.40
        mock_exec.execute.return_value = topup_result

        result = _make_result(matched_shares=5.0)
        bet = _make_bet_record(shares=5.0, size=2.50, price=0.50)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)
        # matched=5.0 == target=min_shares=5.0 → 刚好达到可卖出水平，不需补买
        mock_exec.execute.assert_not_called()

    # ── T68-6: shares=4.996 (真实7648场景) → 必须补买 ─────
    @patch("main.connect")
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_topup_at_4996_real_scenario(
        self, _mos, mock_get, _dry, mock_exec, mock_conn
    ):
        """shares=4.996 < min=5.0 (7648bcbc真实数据) → 必须补买到target=5.8。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()
        mock_exec._check_balance.return_value = 10.0

        topup_result = MagicMock()
        topup_result.success = True
        topup_result.matched_shares = 0.804
        topup_result.filled_size = 0.40
        mock_exec.execute.return_value = topup_result

        result = _make_result(matched_shares=4.996)
        bet = _make_bet_record(shares=4.996, size=2.50, price=0.50)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)
        # need=0.004 shares, cost=$0.002 < CLOB最小$0.10 → 无法补齐，不调用 execute
        mock_exec.execute.assert_not_called()


class TestT204TopUpIsTopupFlag:
    """T204: PostBuyTopUp 补买信号必须设置 _is_topup=True，
    防止 Executor CLOB min 放大导致超买。"""

    @staticmethod
    def _cfg(overrides=None):
        defaults = {
            "executor.post_buy_topup_enabled": True,
            "executor.post_buy_topup_max_retries": 3,
            "executor.post_buy_topup_buffer_shares": 0.8,
            "executor.post_buy_topup_safety_floor_shares": 5.05,
            "executor.min_order_size_shares": 5.0,
            "executor.max_topup_usdc": 10.0,
        }
        if overrides:
            defaults.update(overrides)
        return lambda key, default=None: defaults.get(key, default)

    @patch("main.connect")
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_topup_signal_has_is_topup_flag(
        self, _mos, mock_get, _dry, mock_exec, mock_conn
    ):
        """T204: PostBuyTopUp 创建的 Signal 必须有 _is_topup=True。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()
        mock_exec._check_balance.return_value = 10.0

        topup_result = MagicMock()
        topup_result.success = True
        topup_result.matched_shares = 1.5
        topup_result.filled_size = 0.75
        mock_exec.execute.return_value = topup_result

        result = _make_result(matched_shares=3.0)
        bet = _make_bet_record(shares=3.0, price=0.80)
        market = {"tokens": [{"outcome": "No", "token_id": "tok123"}]}

        _post_buy_topup(bet, result, market)

        # 验证 execute 被调用，且信号有 _is_topup=True
        mock_exec.execute.assert_called()
        called_signal = mock_exec.execute.call_args[0][0]
        assert getattr(called_signal, "_is_topup", False) is True, (
            "TopUp 信号缺少 _is_topup=True 标记，"
            "会导致 Executor CLOB min 放大补买量造成超买"
        )


# ---------------------------------------------------------------------------
# T206追加: PostBuyTopUp 结构性死锁修复测试
# ---------------------------------------------------------------------------


class TestPostBuyTopUpDeadlockFix:
    """T206追加: 验证 PostBuyTopUp 不再因 deficit_cost < min_bet 死锁。

    事故场景: Singapore 33°C NO，Maker 部分成交 3.18/5.1 shares，
    补买 1.92 × $0.69 = $1.32，executor 因 $1.32 < min_bet $2.0 拒绝。
    修复后: executor 对 _is_topup=True 豁免 min_bet 检查。
    """

    @staticmethod
    def _cfg(overrides=None):
        defaults = {
            "executor.post_buy_topup_enabled": True,
            "executor.post_buy_topup_max_retries": 3,
            "executor.post_buy_topup_buffer_shares": 0.1,
            "executor.post_buy_topup_safety_floor_shares": 5.1,
            "executor.min_order_size_shares": 5.0,
            "executor.max_topup_usdc": 5.0,
            "executor.min_bet_usdc": 2.0,
        }
        if overrides:
            defaults.update(overrides)
        return lambda key, default=None: defaults.get(key, default)

    @patch("main.connect")
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_topup_below_min_bet_still_executes(
        self, _mos, mock_get, _dry, mock_exec, mock_conn
    ):
        """deficit_cost=$1.32 < min_bet=$2.0 时，PostBuyTopUp 仍应调用 executor。

        Executor 对 _is_topup=True 豁免 min_bet，并通过 CLOB 份数放大确保下单。
        """
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()
        mock_exec._check_balance.return_value = 15.0

        topup_result = MagicMock()
        topup_result.success = True
        topup_result.matched_shares = 5.0  # CLOB 放大到 5 shares
        topup_result.filled_size = 3.45  # 5 × 0.69
        mock_exec.execute.return_value = topup_result

        # 模拟 Singapore 事故: 3.18 shares, target 5.1
        result = _make_result(matched_shares=3.18, filled_size=2.19, actual_price=0.69)
        bet = _make_bet_record(
            bet_id="a322fcab-test", shares=3.18, size=2.19, price=0.69
        )
        market = {"tokens": [{"outcome": "No", "token_id": "tok_sg"}]}

        _post_buy_topup(bet, result, market)

        # 核心断言: executor.execute 被调用（不被 min_bet 拦截）
        mock_exec.execute.assert_called()
        called_signal = mock_exec.execute.call_args[0][0]
        # 补买成本 = (5.1 - 3.18) × 0.69 = $1.32
        assert (
            called_signal.size_usdc < 2.0
        ), f"补买成本应为 ~$1.32 (< min_bet $2.0)，实际: ${called_signal.size_usdc}"
        assert getattr(called_signal, "_is_topup", False) is True

    @patch("main.connect")
    @patch("main._executor")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get")
    @patch(_MOS_TARGET, return_value=5.0)
    def test_topup_below_min_bet_updates_db_correctly(
        self, _mos, mock_get, _dry, mock_exec, mock_conn
    ):
        """TopUp 成功后 DB 应正确更新 shares/usdc/price。"""
        from main import _post_buy_topup

        mock_get.side_effect = self._cfg()
        mock_exec._check_balance.return_value = 15.0

        topup_result = MagicMock()
        topup_result.success = True
        topup_result.matched_shares = 5.0
        topup_result.filled_size = 3.45
        mock_exec.execute.return_value = topup_result

        result = _make_result(matched_shares=3.18, filled_size=2.19, actual_price=0.69)
        bet = _make_bet_record(
            bet_id="a322fcab-db-test", shares=3.18, size=2.19, price=0.69
        )
        market = {"tokens": [{"outcome": "No", "token_id": "tok_sg2"}]}

        _post_buy_topup(bet, result, market)

        # DB 更新: new_shares = 3.18 + 5.0 = 8.18
        mock_conn_ctx = mock_conn.return_value.__enter__.return_value
        if mock_conn_ctx.execute.called:
            args = mock_conn_ctx.execute.call_args[0]
            sql = args[0]
            params = args[1]
            assert "UPDATE paper_bets" in sql
            new_usdc = params[0]  # size_usdc
            new_shares = params[2]  # matched_shares
            assert new_shares == pytest.approx(
                8.18, abs=0.1
            ), f"DB shares 应为 ~8.18 (3.18+5.0)，实际: {new_shares}"
