"""T314 追加修复：卖出残留 0.1 份 + 持仓余额不足友好提示

修复目标：
1. PM 微单位余额错误 'balance: 121578, order amount: 2000000' → 翻译为含人类可读份数
   的中文提示（实际 0.1216 份 vs 请求 2.0 份）
2. 0 持仓场景下 SELL 按钮禁用 — 由前端 canSubmit 保护，同时后端必须给出明确翻译
3. 残留 0.1 份场景：用户 MAX 卖出后再次点击 → 不能让原始 CLOB 错误暴露给用户

注：前端 canSubmit/refresh 轮询/自动重试残留逻辑在 event_detail.html（legacy runtime），
浏览器集成测试通过 Playwright 验证。本文件验证后端契约。
"""
from __future__ import annotations

import pytest


# ---------- 微单位余额错误翻译 ----------

def test_friendly_error_microunit_balance_with_full_pattern():
    """完整 PM 错误：'not enough balance / allowance: the balance is not enough -> balance: 121578, order amount: 2000000'

    121578 微单位 = 0.121578 share (÷ 1e6)
    2000000 微单位 = 2.0 share
    必须翻译为人类可读：实际可卖 0.1216 份, 请求 2.0 份, 提示刷新页面
    """
    from scripts.dashboard_routes.discover_routes import _friendly_clob_error

    raw = (
        "not enough balance / allowance: the balance is not enough -> "
        "balance: 121578, order amount: 2000000"
    )
    msg = _friendly_clob_error(raw)
    assert "0.1216" in msg, f"必须翻译微单位为可读份数: {msg}"
    assert "2.0000" in msg, f"必须显示请求份数: {msg}"
    assert "持仓余额不足" in msg, f"必须中文化: {msg}"
    assert "刷新" in msg, f"必须引导用户刷新页面: {msg}"
    # 不得暴露原始微单位整数
    assert "121578" not in msg
    assert "2000000" not in msg


def test_friendly_error_microunit_small_residual():
    """0.1 份残留典型场景：实际 0.10 share, 请求 2.34 share"""
    from scripts.dashboard_routes.discover_routes import _friendly_clob_error

    raw = (
        "not enough balance / allowance: the balance is not enough -> "
        "balance: 100000, order amount: 2340000"
    )
    msg = _friendly_clob_error(raw)
    assert "0.1000" in msg
    assert "2.3400" in msg
    assert "持仓余额不足" in msg


def test_friendly_error_microunit_zero_balance():
    """完全清仓后再次点击：实际 0 份, 请求任意份数"""
    from scripts.dashboard_routes.discover_routes import _friendly_clob_error

    raw = (
        "not enough balance / allowance: the balance is not enough -> "
        "balance: 0, order amount: 1000000"
    )
    msg = _friendly_clob_error(raw)
    assert "0.0000" in msg
    assert "持仓余额不足" in msg


def test_friendly_error_balance_fallback_when_no_microunit_match():
    """兜底：当原始错误含 'not enough balance' 但无微单位数字时也要友好"""
    from scripts.dashboard_routes.discover_routes import _friendly_clob_error

    raw = "not enough balance / allowance for order, please retry"
    msg = _friendly_clob_error(raw)
    # 未匹配微单位但含 balance — 走"insufficient balance"分支或新加的兜底
    assert any(k in msg for k in ("钱包余额", "持仓余额"))
    # 原始字串不暴露
    assert "balance / allowance" not in msg


def test_friendly_error_not_enough_allowance_fallback():
    """无 'order amount' 字段时的 not enough allowance 兜底分支"""
    from scripts.dashboard_routes.discover_routes import _friendly_clob_error

    raw = "not enough allowance, please grant USDC approval"
    msg = _friendly_clob_error(raw)
    assert any(k in msg for k in ("钱包余额", "持仓余额", "充值"))


# ---------- 既有错误翻译保持不退化 ----------

def test_friendly_error_t315_min_notional_still_works():
    """T315 修复不被本次改动覆盖"""
    from scripts.dashboard_routes.discover_routes import _friendly_clob_error

    raw = "invalid amount for a marketable BUY order ($0.99), min size: $1"
    msg = _friendly_clob_error(raw)
    assert "$0.99" in msg
    assert "订单价值" in msg


def test_friendly_error_not_enough_shares_still_works():
    from scripts.dashboard_routes.discover_routes import _friendly_clob_error

    raw = "not enough shares for sell order"
    msg = _friendly_clob_error(raw)
    assert "持仓份数" in msg or "持仓余额" in msg


# ---------- 微单位换算正确性 ----------

@pytest.mark.parametrize(
    "balance_micro,order_micro,expected_bal,expected_req",
    [
        (1234567, 5000000, "1.2346", "5.0000"),  # 4 位小数舍入
        (10, 1000000, "0.0000", "1.0000"),  # 微小残尘
        (999999, 1000000, "1.0000", "1.0000"),  # 接近 1 时四舍五入
    ],
)
def test_friendly_error_microunit_conversion_precision(
    balance_micro, order_micro, expected_bal, expected_req
):
    from scripts.dashboard_routes.discover_routes import _friendly_clob_error

    raw = (
        f"not enough balance / allowance: the balance is not enough -> "
        f"balance: {balance_micro}, order amount: {order_micro}"
    )
    msg = _friendly_clob_error(raw)
    assert expected_bal in msg, f"余额格式化错: {msg}"
    assert expected_req in msg, f"请求量格式化错: {msg}"
