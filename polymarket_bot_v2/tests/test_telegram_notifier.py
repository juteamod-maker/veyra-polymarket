"""
test_telegram_notifier.py — TelegramNotifier 单元测试
覆盖：消息发送、通知类型开关、HTML转义、失败容错
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from src.utils.telegram_notifier import TelegramNotifier, _escape, get_notifier

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_notifier(token="fake_token", chat_id="12345"):
    """创建测试用 TelegramNotifier，注入 token/chat_id。"""
    with patch.dict(
        os.environ, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id}
    ):
        return TelegramNotifier()


# ---------------------------------------------------------------------------
# 基础发送测试
# ---------------------------------------------------------------------------


class TestSendMessage:
    def test_send_success(self):
        """发送成功返回 True。"""
        notifier = _make_notifier()
        mock_resp = MagicMock(status_code=200)
        with patch("src.utils.telegram_notifier.requests.post", return_value=mock_resp):
            with patch(
                "src.utils.telegram_notifier.get", return_value={"enabled": True}
            ):
                result = notifier.send_message("test message")
        assert result is True

    def test_send_http_error(self):
        """HTTP 错误返回 False，不抛异常。"""
        notifier = _make_notifier()
        mock_resp = MagicMock(status_code=400, text="Bad Request")
        with patch("src.utils.telegram_notifier.requests.post", return_value=mock_resp):
            with patch(
                "src.utils.telegram_notifier.get", return_value={"enabled": True}
            ):
                result = notifier.send_message("test")
        assert result is False

    def test_send_network_exception(self):
        """网络异常返回 False，不抛异常。"""
        notifier = _make_notifier()
        with patch(
            "src.utils.telegram_notifier.requests.post",
            side_effect=Exception("timeout"),
        ):
            with patch(
                "src.utils.telegram_notifier.get", return_value={"enabled": True}
            ):
                result = notifier.send_message("test")
        assert result is False

    def test_send_disabled_by_config(self):
        """配置 enabled=false 时不发送。"""
        notifier = _make_notifier()
        with patch("src.utils.telegram_notifier.requests.post") as mock_post:
            with patch(
                "src.utils.telegram_notifier.get", return_value={"enabled": False}
            ):
                result = notifier.send_message("test")
        assert result is False
        mock_post.assert_not_called()

    def test_send_no_credentials(self):
        """无 token/chat_id 时 available=False，不发送。"""
        notifier = _make_notifier(token="", chat_id="")
        assert notifier.available is False
        with patch("src.utils.telegram_notifier.requests.post") as mock_post:
            result = notifier.send_message("test")
        assert result is False
        mock_post.assert_not_called()

    def test_message_truncated(self):
        """超长消息自动截断到 4000 字符。"""
        notifier = _make_notifier()
        long_msg = "x" * 5000
        mock_resp = MagicMock(status_code=200)
        with patch(
            "src.utils.telegram_notifier.requests.post", return_value=mock_resp
        ) as mock_post:
            with patch(
                "src.utils.telegram_notifier.get", return_value={"enabled": True}
            ):
                notifier.send_message(long_msg)
        sent_text = mock_post.call_args[1]["json"]["text"]
        assert len(sent_text) < 4100
        assert "截断" in sent_text

    def test_correct_api_url(self):
        """API URL 正确包含 token。"""
        notifier = _make_notifier(token="my_secret_token")
        mock_resp = MagicMock(status_code=200)
        with patch(
            "src.utils.telegram_notifier.requests.post", return_value=mock_resp
        ) as mock_post:
            with patch(
                "src.utils.telegram_notifier.get", return_value={"enabled": True}
            ):
                notifier.send_message("hello")
        called_url = mock_post.call_args[0][0]
        assert "my_secret_token" in called_url
        assert "sendMessage" in called_url


# ---------------------------------------------------------------------------
# 通知类型开关测试
# ---------------------------------------------------------------------------


class TestNotificationToggle:
    def test_new_bet_disabled(self):
        """notify_new_bet=false 时不发送下单通知。"""
        notifier = _make_notifier()
        with patch.object(notifier, "send_async") as mock_send:
            with patch(
                "src.utils.telegram_notifier.get",
                return_value={"enabled": True, "notify_new_bet": False},
            ):
                notifier.notify_new_bet(
                    question="test?",
                    outcome="YES",
                    market_price=0.5,
                    ev=0.1,
                    confidence=0.8,
                    size_usdc=2.0,
                    category="crypto",
                )
        mock_send.assert_not_called()

    def test_new_bet_enabled(self):
        """notify_new_bet=true 时发送下单通知。"""
        notifier = _make_notifier()
        with patch.object(notifier, "send_async") as mock_send:
            with patch(
                "src.utils.telegram_notifier.get",
                return_value={"enabled": True, "notify_new_bet": True},
            ):
                notifier.notify_new_bet(
                    question="Will it rain?",
                    outcome="YES",
                    market_price=0.45,
                    ev=0.15,
                    confidence=0.8,
                    size_usdc=3.0,
                    category="weather",
                )
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "新下单" in msg
        assert "Will it rain?" in msg
        assert "YES" in msg

    def test_position_closed_disabled(self):
        """notify_position_closed=false 时不发送平仓通知。"""
        notifier = _make_notifier()
        with patch.object(notifier, "send_async") as mock_send:
            with patch(
                "src.utils.telegram_notifier.get",
                return_value={"enabled": True, "notify_position_closed": False},
            ):
                notifier.notify_position_closed(
                    question="test?",
                    close_type="profit_take",
                    entry_price=0.3,
                    exit_price=0.5,
                    pnl=1.5,
                    size_usdc=3.0,
                )
        mock_send.assert_not_called()

    def test_risk_alert_enabled(self):
        """风控告警通知包含正确内容。"""
        notifier = _make_notifier()
        with patch.object(notifier, "send_async") as mock_send:
            with patch(
                "src.utils.telegram_notifier.get",
                return_value={"enabled": True, "notify_risk_alert": True},
            ):
                notifier.notify_risk_alert("[风控] 今日亏损 5% 已触发暂停")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "风控警报" in msg

    def test_cycle_summary_content(self):
        """周期汇总包含所有关键信息。"""
        notifier = _make_notifier()
        with patch.object(notifier, "send_async") as mock_send:
            with patch(
                "src.utils.telegram_notifier.get",
                return_value={"enabled": True, "notify_cycle_summary": True},
            ):
                notifier.notify_cycle_summary(
                    cycle_count=10,
                    new_bets=2,
                    open_positions=5,
                    balance=50.0,
                    nlv=76.0,
                    day_pnl=1.5,
                )
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "周期汇总" in msg
        assert "#10" in msg
        assert "$76.00" in msg


# ---------------------------------------------------------------------------
# 平仓通知内容测试
# ---------------------------------------------------------------------------


class TestPositionClosedContent:
    def test_profit_take_icon(self):
        """止盈使用 ✅ 图标。"""
        notifier = _make_notifier()
        with patch.object(notifier, "send_async") as mock_send:
            with patch(
                "src.utils.telegram_notifier.get",
                return_value={"enabled": True, "notify_position_closed": True},
            ):
                notifier.notify_position_closed(
                    question="BTC > 100k?",
                    close_type="profit_take",
                    entry_price=0.3,
                    exit_price=0.6,
                    pnl=5.0,
                    size_usdc=10.0,
                )
                # 强制刷新批量队列（测试中不等待定时器）
                notifier._flush_close_batch()
        msg = mock_send.call_args[0][0]
        assert "✅" in msg
        assert "止盈" in msg
        assert "+5.00" in msg

    def test_stop_loss_icon(self):
        """止损使用 🛑 图标。"""
        notifier = _make_notifier()
        with patch.object(notifier, "send_async") as mock_send:
            with patch(
                "src.utils.telegram_notifier.get",
                return_value={"enabled": True, "notify_position_closed": True},
            ):
                notifier.notify_position_closed(
                    question="ETH drop?",
                    close_type="stop_loss",
                    entry_price=0.5,
                    exit_price=0.3,
                    pnl=-2.0,
                    size_usdc=10.0,
                )
                # 强制刷新批量队列（测试中不等待定时器）
                notifier._flush_close_batch()
        msg = mock_send.call_args[0][0]
        assert "🛑" in msg
        assert "止损" in msg

    def test_settlement_win(self):
        """市场结算 WIN 通知。"""
        notifier = _make_notifier()
        with patch.object(notifier, "send_async") as mock_send:
            with patch(
                "src.utils.telegram_notifier.get",
                return_value={"enabled": True, "notify_position_closed": True},
            ):
                notifier.notify_settlement(
                    question="Rain in NYC?",
                    our_outcome="YES",
                    actual_outcome="YES",
                    is_win=True,
                    pnl=5.0,
                    size_usdc=3.0,
                )
        msg = mock_send.call_args[0][0]
        assert "WIN" in msg
        assert "🎉" in msg

    def test_settlement_loss(self):
        """市场结算 LOSS 通知。"""
        notifier = _make_notifier()
        with patch.object(notifier, "send_async") as mock_send:
            with patch(
                "src.utils.telegram_notifier.get",
                return_value={"enabled": True, "notify_position_closed": True},
            ):
                notifier.notify_settlement(
                    question="Rain in NYC?",
                    our_outcome="YES",
                    actual_outcome="NO",
                    is_win=False,
                    pnl=-3.0,
                    size_usdc=3.0,
                )
        msg = mock_send.call_args[0][0]
        assert "LOSS" in msg
        assert "💔" in msg


# ---------------------------------------------------------------------------
# HTML 转义测试
# ---------------------------------------------------------------------------


class TestHtmlEscape:
    def test_escapes_ampersand(self):
        assert _escape("A & B") == "A &amp; B"

    def test_escapes_angle_brackets(self):
        assert _escape("<script>") == "&lt;script&gt;"

    def test_normal_text_unchanged(self):
        assert _escape("Hello World") == "Hello World"


# ---------------------------------------------------------------------------
# 单例测试
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_notifier_returns_same_instance(self):
        """get_notifier() 返回同一实例。"""
        import src.utils.telegram_notifier as mod

        mod._notifier = None  # 重置
        with patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
        ):
            n1 = get_notifier()
            n2 = get_notifier()
        assert n1 is n2
        mod._notifier = None  # 清理
