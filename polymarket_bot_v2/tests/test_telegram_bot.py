"""
test_telegram_bot.py — 双向 Telegram Bot 单元测试

覆盖：
  - TelegramBot 初始化与启用/禁用逻辑
  - Chat ID 白名单安全验证（静默拒绝）
  - 命令路由 (/help, /docs, /status, /log, /positions, /pnl, /config, /restart)
  - /positions #ID 详情查看
  - /config list + 中文名解析
  - /docs 完整文档
  - 速率限制
  - 配置修改安全检查（黑名单、section白名单）
  - 值解析 (_parse_value)
  - HTML 转义 (_escape)
  - callback 处理
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

# ── 测试前准备 ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个测试重置单例。"""
    import src.utils.telegram_bot as mod

    mod._bot_instance = None
    yield
    mod._bot_instance = None


@pytest.fixture
def mock_env(monkeypatch):
    """设置 Telegram 环境变量。"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")


@pytest.fixture
def bot(mock_env):
    """创建 TelegramBot 实例。"""
    from src.utils.telegram_bot import TelegramBot

    return TelegramBot()


@pytest.fixture
def mock_config():
    """Mock config_loader.get 返回 telegram_bot 配置。"""

    def _get(key, default=None):
        cfg = {
            "telegram_bot": {
                "enabled": True,
                "allowed_chat_ids": [12345],
                "command_rate_limit": 20,
                "log_lines_default": 20,
                "log_lines_max": 50,
            },
            "telegram_bot.enabled": True,
            "telegram_bot.allowed_chat_ids": [12345],
            "telegram_bot.command_rate_limit": 20,
            "telegram_bot.log_lines_default": 20,
            "telegram_bot.log_lines_max": 50,
        }
        return cfg.get(key, default)

    return _get


# ── 初始化测试 ────────────────────────────────────────────


class TestInit:
    def test_enabled_with_credentials(self, bot):
        assert bot.enabled is True
        assert bot._token == "test-token-123"
        assert bot._chat_id == "12345"

    def test_disabled_without_token(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        from src.utils.telegram_bot import TelegramBot

        b = TelegramBot()
        assert b.enabled is False

    def test_disabled_without_chat_id(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
        from src.utils.telegram_bot import TelegramBot

        b = TelegramBot()
        assert b.enabled is False

    def test_start_does_nothing_when_disabled(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
        from src.utils.telegram_bot import TelegramBot

        b = TelegramBot()
        b.start()
        assert b._thread is None


# ── 安全验证测试 ──────────────────────────────────────────


class TestSecurity:
    def test_unauthorized_chat_id_silent_reject(self, bot):
        """非白名单 chat_id 静默忽略（不回复）。"""
        update = {
            "message": {
                "chat": {"id": 99999},
                "from": {"username": "stranger"},
                "text": "/status",
            }
        }
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot.get"
        ) as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "allowed_chat_ids": [12345],
                "command_rate_limit": 20,
            }
            bot._handle_update(update)
            mock_send.assert_not_called()

    def test_authorized_chat_id_processed(self, bot):
        """白名单 chat_id 的消息被处理。"""
        update = {
            "message": {
                "chat": {"id": 12345},
                "text": "/help",
            }
        }
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot.get"
        ) as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "allowed_chat_ids": [12345],
                "command_rate_limit": 20,
            }
            bot._handle_update(update)
            mock_send.assert_called_once()

    def test_whitelist_fallback_to_env_chat_id(self, bot):
        """白名单为空时回退到环境变量 CHAT_ID。"""
        with patch("src.utils.telegram_bot.get") as mock_get:
            mock_get.return_value = {"allowed_chat_ids": []}
            allowed = bot._get_allowed_chat_ids()
            assert "12345" in allowed

    def test_whitelist_multiple_ids(self, bot):
        """白名单支持多个 Chat ID。"""
        with patch("src.utils.telegram_bot.get") as mock_get:
            mock_get.return_value = {"allowed_chat_ids": [12345, 67890]}
            allowed = bot._get_allowed_chat_ids()
            assert "12345" in allowed
            assert "67890" in allowed

    def test_allowed_chat_ids_blocked_from_config_set(self, bot):
        """allowed_chat_ids 不可通过 /config set 修改。"""
        with patch("src.utils.telegram_bot.get") as mock_get, patch.object(
            bot, "_send"
        ) as mock_send:
            mock_get.return_value = [12345]
            bot._config_set_confirm(
                "12345", "telegram_bot.allowed_chat_ids", "[12345, 99999]"
            )
            assert "受保护" in mock_send.call_args[0][1]

    def test_rate_limit_blocks_excess_commands(self, bot):
        """超过速率限制时拒绝响应。"""
        with patch("src.utils.telegram_bot.get") as mock_get:
            mock_get.return_value = {"command_rate_limit": 3}
            now = time.time()
            bot._rate_buckets["12345"] = [now, now, now]
            assert bot._check_rate_limit("12345") is False

    def test_rate_limit_allows_within_limit(self, bot):
        """未超限时允许。"""
        with patch("src.utils.telegram_bot.get") as mock_get:
            mock_get.return_value = {"command_rate_limit": 20}
            assert bot._check_rate_limit("12345") is True

    def test_callback_unauthorized_chat_silent(self, bot):
        """非白名单 chat_id 的 callback 静默忽略。"""
        callback = {
            "message": {"chat": {"id": 99999}},
            "id": "cb123",
            "data": "restart_confirm",
        }
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot.get"
        ) as mock_get:
            mock_get.return_value = {"allowed_chat_ids": [12345]}
            bot._handle_callback(callback)
            mock_send.assert_not_called()


# ── 命令路由测试 ──────────────────────────────────────────


class TestCommandRouting:
    def test_unknown_command(self, bot):
        """未知命令返回提示。"""
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot.get"
        ) as mock_get:
            mock_get.return_value = {"command_rate_limit": 20}
            bot._route_command("12345", "/unknown")
            mock_send.assert_called_once()
            assert "未知命令" in mock_send.call_args[0][1]

    def test_help_command(self, bot):
        """/help 返回命令列表。"""
        with patch.object(bot, "_send") as mock_send:
            bot._cmd_help("12345", [])
            mock_send.assert_called_once()
            text = mock_send.call_args[0][1]
            assert "/status" in text
            assert "/positions" in text
            assert "/docs" in text
            assert "/sort" in text

    def test_docs_command(self, bot):
        """/docs 发送多条文档消息。"""
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot.get"
        ) as mock_get:
            mock_get.return_value = 0.08
            bot._cmd_docs("12345", [])
            assert mock_send.call_count == 3  # 3 条消息
            texts = [c[0][1] for c in mock_send.call_args_list]
            assert "1/3" in texts[0]
            assert "2/3" in texts[1]
            assert "3/3" in texts[2]

    def test_command_with_bot_suffix(self, bot):
        """带 @bot_name 后缀的命令正常路由。"""
        with patch.object(bot, "_cmd_help") as mock_help, patch(
            "src.utils.telegram_bot.get"
        ) as mock_get:
            mock_get.return_value = {"command_rate_limit": 20}
            bot._route_command("12345", "/help@VagusPolyBot")
            mock_help.assert_called_once()


# ── /status 测试 ──────────────────────────────────────────


class TestStatusCommand:
    @patch("src.utils.telegram_bot.get")
    def test_status_output(self, mock_get, bot):
        mock_get.return_value = None
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.config_loader.is_dry_run", return_value=True
        ), patch(
            "src.utils.db.get_open_paper_bets", return_value=[{"id": "1"}, {"id": "2"}]
        ), patch(
            "src.utils.db.connect"
        ) as mock_connect, patch(
            "src.utils.db.get_today_settled_pnl", return_value=5.25
        ):
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.execute.return_value.fetchone.return_value = (125.50,)
            mock_connect.return_value = mock_conn
            bot._cmd_status("12345", [])
            mock_send.assert_called_once()
            text = mock_send.call_args[0][1]
            assert "DRY RUN" in text
            assert "2" in text  # 2 positions


# ── /log 测试 ─────────────────────────────────────────────


class TestLogCommand:
    @patch("src.utils.telegram_bot.get")
    def test_log_default_lines(self, mock_get, bot):
        mock_get.return_value = {"log_lines_default": 5, "log_lines_max": 50}
        fake_lines = [f"log line {i}\n" for i in range(10)]
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot._LOG_PATH"
        ) as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data="".join(fake_lines))):
                bot._cmd_log("12345", [])
            mock_send.assert_called_once()
            assert "5 行日志" in mock_send.call_args[0][1]

    @patch("src.utils.telegram_bot.get")
    def test_log_custom_lines(self, mock_get, bot):
        mock_get.return_value = {"log_lines_default": 20, "log_lines_max": 50}
        fake_lines = [f"line {i}\n" for i in range(100)]
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot._LOG_PATH"
        ) as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data="".join(fake_lines))):
                bot._cmd_log("12345", ["30"])
            mock_send.assert_called_once()
            assert "30 行日志" in mock_send.call_args[0][1]

    @patch("src.utils.telegram_bot.get")
    def test_log_max_limit(self, mock_get, bot):
        mock_get.return_value = {"log_lines_default": 20, "log_lines_max": 50}
        fake_lines = [f"line {i}\n" for i in range(200)]
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot._LOG_PATH"
        ) as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data="".join(fake_lines))):
                bot._cmd_log("12345", ["999"])
            mock_send.assert_called_once()
            assert "50 行日志" in mock_send.call_args[0][1]

    @patch("src.utils.telegram_bot.get")
    def test_log_file_not_found(self, mock_get, bot):
        mock_get.return_value = {"log_lines_default": 20, "log_lines_max": 50}
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot._LOG_PATH"
        ) as mock_path:
            mock_path.exists.return_value = False
            bot._cmd_log("12345", [])
            assert "不存在" in mock_send.call_args[0][1]


# ── /positions 测试 ───────────────────────────────────────


class TestPositionsCommand:
    def test_no_positions(self, bot):
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.db.get_open_paper_bets", return_value=[]
        ), patch.object(bot, "_fetch_chain_pnl", return_value={}):
            bot._cmd_positions("12345", [])
            assert "无持仓" in mock_send.call_args[0][1]

    def test_with_positions_shows_pnl(self, bot):
        """持仓列表显示实时价格和 P&L，不显示赛道。"""
        bets = [
            {
                "question": "Will it rain?",
                "outcome": "YES",
                "market_price": 0.65,
                "size_usdc": 5.0,
                "category": "weather",
                "condition_id": "cid-1",
                "ts_entered": 1000,
            },
            {
                "question": "BTC > 100k?",
                "outcome": "NO",
                "market_price": 0.4,
                "size_usdc": 3.0,
                "category": "crypto",
                "condition_id": "cid-2",
                "ts_entered": 2000,
            },
        ]
        chain = {
            "cid-1": {"curPrice": 0.70, "cashPnl": 0.30, "eventSlug": "rain"},
            "cid-2": {"curPrice": 0.35, "cashPnl": -0.20, "eventSlug": "btc"},
        }
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.db.get_open_paper_bets", return_value=bets
        ), patch.object(bot, "_fetch_chain_pnl", return_value=chain):
            bot._cmd_positions("12345", [])
            text = mock_send.call_args[0][1]
            assert "2 笔" in text
            assert "#1" in text
            # HTML-escaped P&L
            assert "P&amp;L" in text or "P&L" in text
            # 赛道信息不应出现在列表行中
            assert "weather" not in text.split("\n")[3]

    def test_sort_by_pnl_default(self, bot):
        """默认按 PnL 降序排序。"""
        bets = [
            {
                "question": "Loser",
                "outcome": "NO",
                "market_price": 0.5,
                "size_usdc": 1.0,
                "condition_id": "c1",
                "ts_entered": 1000,
                "ev": 0.1,
            },
            {
                "question": "Winner",
                "outcome": "YES",
                "market_price": 0.5,
                "size_usdc": 1.0,
                "condition_id": "c2",
                "ts_entered": 2000,
                "ev": 0.2,
            },
        ]
        chain = {
            "c1": {"curPrice": 0.4, "cashPnl": -0.50, "eventSlug": ""},
            "c2": {"curPrice": 0.7, "cashPnl": 1.00, "eventSlug": ""},
        }
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.db.get_open_paper_bets", return_value=bets
        ), patch.object(bot, "_fetch_chain_pnl", return_value=chain):
            bot._cmd_positions("12345", [])
            text = mock_send.call_args[0][1]
            # #1 should be the winner (higher PnL)
            winner_pos = text.index("Winner")
            loser_pos = text.index("Loser")
            assert winner_pos < loser_pos

    def test_position_detail_with_polymarket_link(self, bot):
        """详情页包含 Polymarket 链接。"""
        bets = [
            {
                "question": "Seoul temperature > 10C?",
                "outcome": "YES",
                "bet_id": "test-bet-001",
                "market_id": "mkt-001",
                "market_price": 0.65,
                "size_usdc": 5.0,
                "category": "weather",
                "confidence": 0.75,
                "ai_probability": 0.72,
                "ev": 0.07,
                "ts_entered": 1711324800.0,
                "market_end_date": "2026-03-25",
                "condition_id": "cond-123",
                "event_slug": "seoul-temp-march25",
            },
        ]
        chain = {
            "cond-123": {
                "curPrice": 0.70,
                "cashPnl": 0.30,
                "eventSlug": "seoul-temp-march25",
            }
        }
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.db.get_open_paper_bets", return_value=bets
        ), patch.object(bot, "_fetch_chain_pnl", return_value=chain):
            bot._cmd_positions("12345", ["1"])
            text = mock_send.call_args[0][1]
            assert "详情" in text
            assert "polymarket.com" in text
            assert "seoul-temp-march25" in text
            assert "$5.00" in text

    def test_position_detail_invalid_id(self, bot):
        """无效编号返回错误提示。"""
        bets = [
            {
                "question": "test",
                "outcome": "YES",
                "market_price": 0.5,
                "size_usdc": 1.0,
                "condition_id": "c1",
                "ts_entered": 1000,
            }
        ]
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.db.get_open_paper_bets", return_value=bets
        ), patch.object(bot, "_fetch_chain_pnl", return_value={}):
            bot._cmd_positions("12345", ["5"])
            assert "超出范围" in mock_send.call_args[0][1]

    def test_position_detail_hash_prefix(self, bot):
        """支持 #1 格式输入。"""
        bets = [
            {
                "question": "Test bet",
                "outcome": "NO",
                "bet_id": "b-002",
                "market_id": "m-002",
                "market_price": 0.3,
                "size_usdc": 2.0,
                "condition_id": "c1",
                "confidence": 0.6,
                "ai_probability": 0.4,
                "ev": 0.05,
                "ts_entered": 1711324800.0,
            },
        ]
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.db.get_open_paper_bets", return_value=bets
        ), patch.object(bot, "_fetch_chain_pnl", return_value={}):
            bot._cmd_positions("12345", ["#1"])
            text = mock_send.call_args[0][1]
            assert "详情" in text


# ── /sort 测试 ────────────────────────────────────────────


class TestSortCommand:
    def test_sort_no_args_shows_current(self, bot):
        """无参数时展示当前排序和用法。"""
        with patch.object(bot, "_send") as mock_send:
            bot._cmd_sort("12345", [])
            text = mock_send.call_args[0][1]
            assert "pnl" in text.lower()
            assert "desc" in text.lower() or "降序" in text

    def test_sort_valid_field_and_direction(self, bot):
        with patch.object(bot, "_send") as mock_send:
            bot._cmd_sort("12345", ["ev", "asc"])
            assert bot._sort_field == "ev"
            assert bot._sort_desc is False

    def test_sort_field_only_defaults_desc(self, bot):
        with patch.object(bot, "_send") as mock_send:
            bot._cmd_sort("12345", ["time"])
            assert bot._sort_field == "time"
            assert bot._sort_desc is True

    def test_sort_invalid_field(self, bot):
        with patch.object(bot, "_send") as mock_send:
            bot._cmd_sort("12345", ["foo"])
            text = mock_send.call_args[0][1]
            assert "pnl" in text and "ev" in text and "time" in text


# ── /pnl 测试 ────────────────────────────────────────────


class TestPnlCommand:
    def test_pnl_output(self, bot):
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.db.get_today_settled_pnl", return_value=3.50
        ), patch("src.utils.db.get_total_realized_loss", return_value=-10.0), patch(
            "src.utils.db.get_open_paper_bets", return_value=[{"size_usdc": 5.0}]
        ), patch(
            "src.utils.db.connect"
        ) as mock_connect:
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.execute.return_value.fetchone.side_effect = [
                (15.0,),  # total_pnl
                (8,),  # won count
                (12,),  # total closed count
            ]
            mock_connect.return_value = mock_conn
            bot._cmd_pnl("12345", [])
            text = mock_send.call_args[0][1]
            assert "PnL" in text
            assert "$3.50" in text


# ── /config 测试 ──────────────────────────────────────────


class TestConfigCommand:
    def test_config_no_args(self, bot):
        with patch.object(bot, "_send") as mock_send:
            bot._cmd_config("12345", [])
            text = mock_send.call_args[0][1]
            assert "config list" in text

    def test_config_list(self, bot):
        """测试 /config list 显示配置速查表。"""
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot.get"
        ) as mock_get:
            mock_get.return_value = 0.08
            bot._cmd_config("12345", ["list"])
            text = mock_send.call_args[0][1]
            assert "速查表" in text
            assert "最低EV" in text

    @patch("src.utils.telegram_bot.get")
    def test_config_get_existing(self, mock_get, bot):
        mock_get.return_value = 0.08
        with patch.object(bot, "_send") as mock_send:
            bot._config_get("12345", "paper_trading.min_ev_threshold")
            text = mock_send.call_args[0][1]
            assert "0.08" in text

    @patch("src.utils.telegram_bot.get")
    def test_config_get_by_friendly_name(self, mock_get, bot):
        """支持中文名查询配置。"""
        mock_get.return_value = 0.08
        with patch.object(bot, "_send") as mock_send:
            resolved = bot._resolve_config_key("最低EV")
            assert resolved == "paper_trading.min_ev_threshold"

    @patch("src.utils.telegram_bot.get")
    def test_config_get_not_found(self, mock_get, bot):
        mock_get.return_value = None
        with patch.object(bot, "_send") as mock_send:
            bot._config_get("12345", "nonexistent.key")
            assert "不存在" in mock_send.call_args[0][1]

    @patch("src.utils.telegram_bot.get")
    def test_config_set_blocked_key(self, mock_get, bot):
        mock_get.return_value = True
        with patch.object(bot, "_send") as mock_send:
            bot._config_set_confirm("12345", "general.dry_run", "false")
            assert "受保护" in mock_send.call_args[0][1]

    @patch("src.utils.telegram_bot.get")
    def test_config_set_blocked_section(self, mock_get, bot):
        mock_get.return_value = "val"
        with patch.object(bot, "_send") as mock_send:
            bot._config_set_confirm("12345", "forbidden_section.key", "val")
            assert "不允许" in mock_send.call_args[0][1]

    @patch("src.utils.telegram_bot.get")
    def test_config_set_sends_confirmation(self, mock_get, bot):
        mock_get.return_value = 0.08
        with patch.object(bot, "_send") as mock_send:
            bot._config_set_confirm("12345", "paper_trading.min_ev_threshold", "0.10")
            call_args = mock_send.call_args
            text = call_args[0][1]
            assert "确认配置修改" in text
            assert call_args[1].get("reply_markup") is not None


# ── _resolve_config_key 测试 ─────────────────────────────


class TestResolveConfigKey:
    def test_exact_friendly_name(self, bot):
        assert bot._resolve_config_key("最低EV") == "paper_trading.min_ev_threshold"

    def test_dot_path_passthrough(self, bot):
        assert (
            bot._resolve_config_key("risk.daily_loss_pause_pct")
            == "risk.daily_loss_pause_pct"
        )

    def test_fuzzy_match(self, bot):
        result = bot._resolve_config_key("日亏")
        assert result == "risk.daily_loss_pause_pct"

    def test_unknown_key_passthrough(self, bot):
        assert bot._resolve_config_key("unknown_thing") == "unknown_thing"


# ── /restart 测试 ─────────────────────────────────────────


class TestRestartCommand:
    def test_restart_sends_confirmation(self, bot):
        with patch.object(bot, "_send") as mock_send:
            bot._cmd_restart("12345", [])
            call_args = mock_send.call_args
            assert "确认重启" in call_args[0][1]
            assert call_args[1].get("reply_markup") is not None

    def test_restart_cancel_callback(self, bot):
        callback = {
            "message": {"chat": {"id": 12345}},
            "id": "cb456",
            "data": "restart_cancel",
        }
        with patch.object(bot, "_send") as mock_send, patch.object(
            bot, "_answer_callback"
        ), patch.object(bot, "_get_allowed_chat_ids", return_value={"12345"}):
            bot._handle_callback(callback)
            assert "取消" in mock_send.call_args[0][1]


# ── _parse_value 测试 ─────────────────────────────────────


class TestParseValue:
    def test_parse_bool_true(self):
        from src.utils.telegram_bot import _parse_value

        assert _parse_value("true", False) is True
        assert _parse_value("yes", False) is True
        assert _parse_value("1", False) is True

    def test_parse_bool_false(self):
        from src.utils.telegram_bot import _parse_value

        assert _parse_value("false", True) is False
        assert _parse_value("no", True) is False
        assert _parse_value("0", True) is False

    def test_parse_int(self):
        from src.utils.telegram_bot import _parse_value

        assert _parse_value("42", 0) == 42

    def test_parse_float(self):
        from src.utils.telegram_bot import _parse_value

        assert _parse_value("0.08", 0.0) == 0.08

    def test_parse_string(self):
        from src.utils.telegram_bot import _parse_value

        assert _parse_value("hello", "default") == "hello"

    def test_parse_list(self):
        from src.utils.telegram_bot import _parse_value

        result = _parse_value('["a", "b"]', [])
        assert result == ["a", "b"]

    def test_parse_invalid_bool(self):
        from src.utils.telegram_bot import _parse_value

        with pytest.raises(ValueError, match="布尔值"):
            _parse_value("maybe", False)

    def test_parse_invalid_int(self):
        from src.utils.telegram_bot import _parse_value

        with pytest.raises(ValueError):
            _parse_value("not_a_number", 0)


# ── _escape 测试 ──────────────────────────────────────────


class TestEscape:
    def test_escape_html(self):
        from src.utils.telegram_bot import _escape

        assert _escape("<b>test & run</b>") == "&lt;b&gt;test &amp; run&lt;/b&gt;"

    def test_escape_plain(self):
        from src.utils.telegram_bot import _escape

        assert _escape("hello world") == "hello world"


# ── 单例测试 ─────────────────────────────────────────────


class TestSingleton:
    def test_get_bot_returns_same_instance(self, mock_env):
        from src.utils.telegram_bot import get_bot

        b1 = get_bot()
        b2 = get_bot()
        assert b1 is b2

    def test_get_bot_disabled(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
        from src.utils.telegram_bot import get_bot

        b = get_bot()
        assert b.enabled is False


# ── 消息处理边界测试 ──────────────────────────────────────


class TestEdgeCases:
    def test_empty_text_ignored(self, bot):
        """空文本消息被忽略。"""
        update = {"message": {"chat": {"id": 12345}, "text": ""}}
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot.get"
        ) as mock_get:
            mock_get.return_value = {"command_rate_limit": 20}
            bot._handle_update(update)
            mock_send.assert_not_called()

    def test_no_text_message_ignored(self, bot):
        """无文本（如图片）消息被忽略。"""
        update = {"message": {"chat": {"id": 12345}}}
        with patch.object(bot, "_send") as mock_send:
            bot._handle_update(update)
            mock_send.assert_not_called()

    def test_non_command_text_ignored(self, bot):
        """非命令文本被忽略。"""
        update = {"message": {"chat": {"id": 12345}, "text": "hello"}}
        with patch.object(bot, "_send") as mock_send, patch(
            "src.utils.telegram_bot.get"
        ) as mock_get:
            mock_get.return_value = {"command_rate_limit": 20}
            bot._handle_update(update)
            mock_send.assert_not_called()

    def test_send_truncates_long_message(self, bot):
        """超长消息被截断。"""
        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp
            long_text = "x" * 5000
            bot._send("12345", long_text)
            sent_text = mock_post.call_args[1]["json"]["text"]
            assert len(sent_text) <= 4010  # 4000 + "...(截断)"
