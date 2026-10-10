"""T314 追加 C: size_too_small_after_precision_snap 友好化翻译"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scripts.dashboard_routes.discover_routes import _friendly_clob_error


def test_size_too_small_basic():
    raw = "size_too_small_after_precision_snap: price=0.01 要求 size 为 1.00 的整数倍"
    msg = _friendly_clob_error(raw)
    assert "零碎份数无法卖出" in msg
    assert "$0.01" in msg
    assert "1.00" in msg
    # 必须给出 3 个解决方案中的关键词
    assert "等待" in msg and "结算" in msg
    assert "polymarket.com" in msg.lower()
    assert "切勿" in msg or "加买" in msg  # 防止用户为凑整加买


def test_size_too_small_other_price():
    raw = "size_too_small_after_precision_snap: price=0.03 要求 size 为 5.00 的整数倍"
    msg = _friendly_clob_error(raw)
    assert "$0.03" in msg
    assert "5.00" in msg


def test_size_too_small_english_variant():
    raw = "Order rejected: size_too_small_after_precision_snap (multiple of 10)"
    msg = _friendly_clob_error(raw)
    assert "零碎份数" in msg


def test_t314_microunit_still_works():
    """回归: 微单位翻译不被新规则影响"""
    raw = "not enough balance / allowance: the balance is not enough -> balance: 121578, order amount: 2000000"
    msg = _friendly_clob_error(raw)
    assert "持仓余额不足" in msg
    assert "0.1216" in msg or "0.1215" in msg or "0.1" in msg


def test_t315_min_notional_still_works():
    raw = "invalid amount for a marketable BUY order ($0.99), min size: $1"
    msg = _friendly_clob_error(raw)
    assert "订单价值不足" in msg
