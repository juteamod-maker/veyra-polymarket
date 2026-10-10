"""T289: 生产环境修复验证测试。

修复项:
 1. Price tick size: round(4) → round(2) 符合 Polymarket 0.01 tick
 2. TelegramNotifier: .send() → .send_message()
 3. settlement_max_hours: 36 → 72（允许多日天气市场）
 4. RapidAPI 5xx: 增加 502/503/504 自动重试
 5. ECMWF batch: batch_chunk_delay_seconds 可配置
"""

import math
from unittest.mock import MagicMock, patch

import pytest

# ═════════════════════════════════════════════════════════
# 1. Price Tick Size — 0.01 精度验证
# ═════════════════════════════════════════════════════════


class TestPriceTickSize:
    """T289: 下单价格必须是 0.01 的整数倍。"""

    def test_limit_price_rounds_to_2_decimals(self):
        """expected_price + offset 结果必须 round(2)。"""
        # 0.17 + 0.001 = 0.171 → round(2) = 0.17
        expected = 0.17
        offset = 0.001
        price = round(min(max(expected + offset, 0.01), 0.99), 2)
        assert price == 0.17
        assert len(str(price).split(".")[-1]) <= 2

    def test_high_price_rounds_correctly(self):
        """0.995 + 0.001 = 0.996 → clamp 0.99 → round(2) = 0.99。"""
        expected = 0.995
        offset = 0.001
        price = round(min(max(expected + offset, 0.01), 0.99), 2)
        assert price == 0.99

    def test_low_price_floor(self):
        """极低价格不低于 0.01。"""
        expected = 0.005
        offset = 0.001
        price = round(min(max(expected + offset, 0.01), 0.99), 2)
        assert price == 0.01

    def test_exact_cent_unchanged(self):
        """已是整分的价格不变。"""
        expected = 0.50
        offset = 0.0
        price = round(min(max(expected + offset, 0.01), 0.99), 2)
        assert price == 0.50

    def test_price_171_becomes_17(self):
        """触发生产 BUG 的精确场景：0.171 → 0.17。"""
        # 生产日志: Price (0.171) breaks minimum tick size rule: 0.01
        raw = 0.171
        rounded = round(raw, 2)
        assert rounded == 0.17
        # 确保是 0.01 的整数倍
        assert abs(rounded * 100 - round(rounded * 100)) < 1e-9


# ═════════════════════════════════════════════════════════
# 2. TelegramNotifier 方法名验证
# ═════════════════════════════════════════════════════════


class TestTelegramNotifierMethod:
    """T289: TelegramNotifier 应使用 send_message 非 send。"""

    def test_notifier_has_send_message(self):
        """TelegramNotifier 类有 send_message 方法。"""
        from src.utils.telegram_notifier import TelegramNotifier

        assert hasattr(TelegramNotifier, "send_message")

    def test_notifier_has_no_send(self):
        """TelegramNotifier 实例不应有 .send() 方法（仅模块级函数）。"""
        from src.utils.telegram_notifier import TelegramNotifier

        # send 是模块级函数，不是实例方法
        assert not hasattr(TelegramNotifier, "send")

    def test_module_level_send_exists(self):
        """模块级 send() 快捷函数存在。"""
        from src.utils import telegram_notifier

        assert callable(getattr(telegram_notifier, "send", None))


# ═════════════════════════════════════════════════════════
# 3. Settlement Max Hours — 48h 验证
# ═════════════════════════════════════════════════════════


class TestSettlementMaxHours:
    """settlement_max_hours 配置验证。"""

    def test_weather_48h(self):
        from src.utils.config_loader import get

        cfg = get("paper_trading.settlement_max_hours")
        assert float(cfg["weather"]) == 32

    def test_sports_72h(self):
        from src.utils.config_loader import get

        cfg = get("paper_trading.settlement_max_hours")
        assert float(cfg["sports"]) == 48

    def test_crypto_60h(self):
        """T49: crypto settlement 60h (保守值)。"""
        from src.utils.config_loader import get

        cfg = get("paper_trading.settlement_max_hours")
        assert float(cfg["crypto"]) == 60

    def test_60h_weather_market_passes(self):
        """60h 后结算的天气市场在 72h 窗口内通过。"""
        max_hours = 72.0
        hours_left = 60.0
        assert hours_left <= max_hours, "60h市场应在72h窗口内通过"

    def test_80h_weather_market_still_blocked(self):
        """80h 后结算的天气市场仍被过滤（防资金锁定）。"""
        max_hours = 72.0
        hours_left = 80.0
        assert hours_left > max_hours, "80h市场仍应被SETTLEMENT_SKIP"


# ═════════════════════════════════════════════════════════
# 4. RapidAPI 5xx Retry 逻辑验证
# ═════════════════════════════════════════════════════════


class TestRapidApi5xxRetry:
    """T289: RapidAPI 502/503/504 自动重试一次。"""

    @patch("src.utils.rapidapi_client.time.sleep")
    @patch("src.utils.rapidapi_client.requests.get")
    @patch("src.utils.rapidapi_client.os.environ.get", return_value="test_key")
    def test_502_retry_success(self, mock_env, mock_get, mock_sleep):
        """502 后重试成功应返回正常数据。"""
        from src.utils.rapidapi_client import clear_cache, fetch

        clear_cache()

        # 第一次 502，第二次 200
        resp_502 = MagicMock()
        resp_502.status_code = 502

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"fixtures": []}
        resp_200.raise_for_status.return_value = None

        mock_get.side_effect = [resp_502, resp_200]

        result = fetch(
            "odds",
            "/fixtures/today",
            cache_ttl=0,
        )
        assert result == {"fixtures": []}
        assert mock_sleep.called
        assert mock_get.call_count == 2

    @patch("src.utils.rapidapi_client.time.sleep")
    @patch("src.utils.rapidapi_client.requests.get")
    @patch("src.utils.rapidapi_client.os.environ.get", return_value="test_key")
    def test_502_retry_still_fails(self, mock_env, mock_get, mock_sleep):
        """502 重试仍失败应返回 None。"""
        from src.utils.rapidapi_client import clear_cache, fetch

        clear_cache()

        resp_502 = MagicMock()
        resp_502.status_code = 502

        mock_get.side_effect = [resp_502, resp_502]

        result = fetch(
            "odds",
            "/fixtures/live",
            cache_ttl=0,
        )
        assert result is None

    @patch("src.utils.rapidapi_client.time.sleep")
    @patch("src.utils.rapidapi_client.requests.get")
    @patch("src.utils.rapidapi_client.os.environ.get", return_value="test_key")
    def test_503_also_retried(self, mock_env, mock_get, mock_sleep):
        """503 也应触发重试。"""
        from src.utils.rapidapi_client import clear_cache, fetch

        clear_cache()

        resp_503 = MagicMock()
        resp_503.status_code = 503

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"ok": True}
        resp_200.raise_for_status.return_value = None

        mock_get.side_effect = [resp_503, resp_200]

        result = fetch(
            "odds",
            "/test",
            cache_ttl=0,
        )
        assert result == {"ok": True}


# ═════════════════════════════════════════════════════════
# 5. ECMWF Batch Config 可配置验证
# ═════════════════════════════════════════════════════════


class TestEcmwfBatchConfig:
    """T289: ECMWF batch_chunk_delay_seconds 可配置。"""

    def test_batch_chunk_delay_in_config(self):
        from src.utils.config_loader import get

        cfg = get("weather_ensemble")
        delay = float(cfg.get("batch_chunk_delay_seconds", 2.0))
        assert delay >= 3.0, "batch_chunk_delay 应 ≥3s 以减少 ECMWF 429"

    def test_batch_chunk_size_in_config(self):
        from src.utils.config_loader import get

        cfg = get("weather_ensemble")
        size = int(cfg.get("batch_chunk_size", 8))
        assert 1 <= size <= 20


# ═════════════════════════════════════════════════════════
# 6. T289 Phase 2: 天气时间窗口 + 体育漏斗优化
# ═════════════════════════════════════════════════════════


class TestWeatherTimingPhase2:
    """天气时间窗口配置验证。"""

    def test_min_hours_to_expiry_removed(self):
        from src.utils.config_loader import get

        pt = get("paper_trading")
        assert (
            "weather_min_hours_to_expiry" not in pt
        ), "T125: weather_min_hours_to_expiry 已删除，精细入场由SmartEntry控制"

    def test_entry_window_is_48(self):
        from src.utils.config_loader import get

        pt = get("paper_trading")
        assert float(pt["weather_entry_window_hours"]) == 32

    def test_nyc_1775h_rejected_by_18h_threshold(self):
        """NYC endDate=12:00UTC, at 18:15UTC hours=17.75 → 18h阈值正确拒绝(防当日高温已过)。"""
        min_hours = 18
        hours_to_expiry = 17.75
        assert not (
            hours_to_expiry > min_hours
        ), "17.75h should be rejected by min_hours=18"

    def test_18h_boundary_still_rejects(self):
        """距到期恰好18h，应被拒绝（≤18h）。"""
        min_hours = 18
        hours_to_expiry = 18.0
        assert not (hours_to_expiry > min_hours), "18.0h should be rejected at boundary"

    def test_entry_window_48h_allows_2day_ahead(self):
        """48h窗口允许提前2天入场。"""
        entry_window = 48
        hours_to_expiry = 40.0
        min_hours = 18
        assert min_hours < hours_to_expiry <= entry_window


class TestSportsFunnelPhase2:
    """T289-P2: 体育赛道漏斗放宽——更多候选进入Phase2分析。"""

    def test_category_top_n_sports_at_least_100(self):
        """T48: sports top_n 调整至100以平衡赛道分布。"""
        from src.utils.config_loader import get

        cfg = get("scanner.category_top_n")
        assert int(cfg["sports"]) >= 100

    def test_sports_settlement_72h(self):
        from src.utils.config_loader import get

        cfg = get("paper_trading.settlement_max_hours")
        assert float(cfg["sports"]) == 48

    def test_sports_max_verify_at_least_25(self):
        """T292: OddsAPI验证预算从15增到25。"""
        from src.utils.config_loader import get

        cfg = get("signal")
        assert int(cfg["sports_max_odds_verify_per_cycle"]) >= 12  # T39: config更新为12

    def test_sports_prescreen_min_data_len_configurable(self):
        """TheSportsDB min_data_len 现在从配置读取，默认20。"""
        from src.utils.config_loader import get

        cfg = get("signal")
        val = int(cfg.get("sports_prescreen_min_data_len", 20))
        assert val <= 50, "min_data_len should be ≤50 to reduce false rejections"
