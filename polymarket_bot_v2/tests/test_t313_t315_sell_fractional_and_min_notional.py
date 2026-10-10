"""T313 + T315 单元测试覆盖

T313: 卖出小数份数支持 — `_friendly_clob_error` 提示纯净 + balance-check 输出新阈值
T315: 最小订单价值（notional ≥ $1）友好提示，区分订单价值与用户输入金额

注：sell modal 前端 canSubmit 的 epsilon 修复在 event_detail.html（legacy runtime），
浏览器集成测试通过 Playwright 验证。本文件验证后端契约。
"""
from __future__ import annotations

import math

import pytest


# --------------------------------------------------------------------------
# T315: _friendly_clob_error 区分订单价值不足 vs 用户输入金额
# --------------------------------------------------------------------------
def test_friendly_clob_error_min_notional_with_actual_value():
    """PM 返回 'invalid amount for a marketable BUY order ($0.99), min size: $1'
    时，提示必须明确告知用户：是订单价值($0.99)不足，而非用户输入金额。
    """
    from scripts.dashboard_routes.discover_routes import _friendly_clob_error

    raw = "invalid amount for a marketable BUY order ($0.99), min size: $1"
    msg = _friendly_clob_error(raw)
    assert "$0.99" in msg, f"必须显示实际订单价值给用户参考: {msg}"
    assert "$1" in msg, f"必须显示 PM 最小要求: {msg}"
    assert "订单价值" in msg, f"必须使用'订单价值'区分用户金额: {msg}"
    # 不能再出现误导的 "下单金额不足"
    assert "下单金额不足" not in msg


def test_friendly_clob_error_min_notional_no_actual_value_fallback():
    from scripts.dashboard_routes.discover_routes import _friendly_clob_error

    raw = "invalid amount for a marketable order, min size: $1"
    msg = _friendly_clob_error(raw)
    assert "$1" in msg
    assert "订单价值" in msg


def test_friendly_clob_error_min_shares_unchanged():
    """min size shares 错误仍然走份数路径，不被混淆为 notional 错误。"""
    from scripts.dashboard_routes.discover_routes import _friendly_clob_error

    raw = "Size (2) lower than the minimum: 5"
    msg = _friendly_clob_error(raw)
    assert "5" in msg
    assert "shares" in msg


# --------------------------------------------------------------------------
# T315: BUY 市价单 size 计算契约 — 模拟前端逻辑确保 notional ≥ $1
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "amount,effective_px,cap,min_trade",
    [
        (1.0, 0.5, 0.99, 1.0),    # 中等价格
        (1.0, 0.99, 0.99, 1.0),   # 高价临界 — 历史 bug 触发点
        (1.0, 0.95, 0.99, 1.0),   # 高价
        (1.0, 0.05, 0.99, 1.0),   # 低价
        (5.0, 0.50, 0.99, 1.0),   # 正常金额
        (1.0, 0.999, 0.99, 1.0),  # 极端高价
    ],
)
def test_buy_market_shares_calc_satisfies_min_notional(amount, effective_px, cap, min_trade):
    """复现前端 submitOrder BUY market 分支的 size 计算逻辑：
    size = ceil(max(amount/effective_px, min_trade/cap) * 100) / 100
    必须保证：cap × size ≥ min_trade 且 effective_px × size ≥ amount
    """
    effective_px = max(0.01, min(cap, effective_px))
    size_from_amount = amount / effective_px
    size_from_min_notional = min_trade / cap
    shares = math.ceil(max(size_from_amount, size_from_min_notional) * 100) / 100

    # 不变量1: PM notional 校验 (price_cap × size ≥ min_trade)
    assert cap * shares >= min_trade - 1e-9, (
        f"FAIL notional: cap({cap}) × shares({shares}) = {cap*shares} < min_trade({min_trade})"
    )
    # 不变量2: 用户实际花费 ≥ 输入金额
    assert effective_px * shares >= amount - 1e-9, (
        f"FAIL spend: effective_px({effective_px}) × shares({shares}) = {effective_px*shares} < amount({amount})"
    )


# --------------------------------------------------------------------------
# T313: balance-check 必须输出 min_sell_share_unit 等新阈值
# --------------------------------------------------------------------------
def test_balance_check_returns_new_thresholds():
    """前端依赖 thresholds.min_sell_share_unit / buy_price_cap / max_order_size_usdc
    在加载时同步配置。读源码确认契约。
    """
    import inspect
    from scripts.dashboard_routes import discover_routes

    src = inspect.getsource(discover_routes.pm_balance_check)
    assert "min_sell_share_unit" in src
    assert "buy_price_cap" in src
    assert "max_order_size_usdc" in src


# --------------------------------------------------------------------------
# T313: config 默认值
# --------------------------------------------------------------------------
def test_config_has_new_trading_keys():
    import json
    import os

    cfg_path = os.path.join(
        os.path.dirname(__file__), "..", "config", "config.json"
    )
    with open(cfg_path) as f:
        cfg = json.load(f)
    trading = cfg.get("trading", {})
    assert trading.get("min_sell_share_unit") == 0.01
    assert trading.get("buy_price_cap") == 0.99


def test_config_docs_has_new_trading_keys():
    import json
    import os

    docs_path = os.path.join(
        os.path.dirname(__file__), "..", "config", "config_docs.json"
    )
    with open(docs_path) as f:
        docs = json.load(f)
    assert "trading.min_sell_share_unit" in docs
    assert "trading.buy_price_cap" in docs
    # 文档不得出现任务编号（SUPREME 3 规则）
    assert "T313" not in docs["trading.min_sell_share_unit"]
    assert "T315" not in docs["trading.buy_price_cap"]
