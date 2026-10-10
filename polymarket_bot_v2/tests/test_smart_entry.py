"""
test_smart_entry.py — T107 SmartEntry 智能入场门控 单元测试

覆盖：
  - CitySignalCache 暂存、清理、稳定性、价格历史
  - SmartEntryGate 时区判断、窗口检查、最小轮次、信号增强
  - Ambush 模型-市场分歧检查
  - 不同时区城市的完整流程
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from src.signal.signal_engine import Signal
from src.signal.smart_entry import (
    CityRoundRecord,
    CitySignalCache,
    SignalSnapshot,
    SmartEntryGate,
)

# ─── 辅助工厂 ─────────────────────────────────────────────────────


def _make_signal(
    market_id="m1",
    outcome="NO",
    ev=0.1,
    price=0.3,
    confidence=0.8,
    tag="weather_right_tail_left",
    token_id="tok1",
    condition_id="0xabc",
    question="Will it be 15°C?",
) -> Signal:
    return Signal(
        market_id=market_id,
        question=question,
        outcome=outcome,
        ev=ev,
        confidence=confidence,
        size_usdc=3.0,
        token_id=token_id,
        expected_price=price,
        condition_id=condition_id,
        strategy_tag=tag,
    )


def _default_config():
    """返回测试用默认 config（模拟 config_loader.get 返回值）。"""
    return {
        "weather_strategy": {
            "smart_entry_enabled": True,
            "peak_window_buy_offset_hours": 4,
            "signal_cache_hours": 12.0,
            "min_history_rounds_for_buy": 3,
            "enable_price_trend_scoring": True,
            "trend_improving_bonus": 0.02,
            "trend_worsening_penalty": 0.02,
            "signal_stability_min_pct": 0.3,
            "max_days_until_event": 0,
        },
        "paper_trading": {
            "weather_time_guard": {
                "peak_window_start": 10,
                "peak_window_end": 17,
            }
        },
    }


def _mock_get(cfg):
    """创建 config_loader.get 的 mock。"""

    def getter(key, default=None):
        parts = key.split(".")
        val = cfg
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return default
            if val is None:
                return default
        return val

    return getter


# ─── CitySignalCache 测试 ─────────────────────────────────────────


class TestCitySignalCache:
    """测试信号暂存缓存。"""

    def test_add_round_increments_counter(self):
        cache = CitySignalCache("Seoul", "April 7", "Asia/Seoul")
        s1 = _make_signal(market_id="m1")
        s2 = _make_signal(market_id="m2")
        r1 = cache.add_round([s1, s2], yesbest_threshold=12.0, yesbest_ask=0.35)
        assert r1 == 1
        assert cache.rounds_count == 1
        r2 = cache.add_round([s1], yesbest_threshold=12.0, yesbest_ask=0.36)
        assert r2 == 2
        assert cache.rounds_count == 2

    def test_cleanup_expired(self):
        cache = CitySignalCache("Tokyo", "April 7", "Asia/Tokyo")
        s = _make_signal()
        cache.add_round([s])
        # 手动篡改时间戳为13小时前
        cache.records[0].timestamp_utc = datetime.now(timezone.utc) - timedelta(
            hours=13
        )
        cache.add_round([s])  # 最新的
        removed = cache.cleanup_expired(12.0)
        assert removed == 1
        assert cache.rounds_count == 1

    def test_get_price_history(self):
        cache = CitySignalCache("London", "April 8", "Europe/London")
        cache.add_round([_make_signal(market_id="m1", price=0.30)])
        cache.add_round([_make_signal(market_id="m1", price=0.28)])
        cache.add_round([_make_signal(market_id="m1", price=0.25)])
        history = cache.get_price_history("m1")
        assert len(history) == 3
        prices = [p for _, p in history]
        assert prices == [0.30, 0.28, 0.25]

    def test_get_signal_stability_all_present(self):
        cache = CitySignalCache("Paris", "April 7", "Europe/Paris")
        for _ in range(5):
            cache.add_round([_make_signal(market_id="m1")])
        assert cache.get_signal_stability("m1") == 1.0

    def test_get_signal_stability_partial(self):
        cache = CitySignalCache("Seoul", "April 7", "Asia/Seoul")
        cache.add_round([_make_signal(market_id="m1")])
        cache.add_round([_make_signal(market_id="m2")])  # m1 不出现
        cache.add_round([_make_signal(market_id="m1")])
        # m1 出现在 2/3 轮
        assert abs(cache.get_signal_stability("m1") - 2 / 3) < 0.01

    def test_get_signal_stability_never(self):
        cache = CitySignalCache("NYC", "April 7", "America/New_York")
        cache.add_round([_make_signal(market_id="m2")])
        assert cache.get_signal_stability("m99") == 0.0

    def test_yesbest_stability_consistent(self):
        cache = CitySignalCache("Chicago", "April 7", "America/Chicago")
        for _ in range(5):
            cache.add_round([], yesbest_threshold=15.0)
        stable, threshold = cache.get_yesbest_stability()
        assert stable is True
        assert threshold == 15.0

    def test_yesbest_stability_inconsistent(self):
        cache = CitySignalCache("Miami", "April 7", "America/New_York")
        cache.add_round([], yesbest_threshold=15.0)
        cache.add_round([], yesbest_threshold=16.0)
        cache.add_round([], yesbest_threshold=17.0)
        cache.add_round([], yesbest_threshold=18.0)
        stable, _ = cache.get_yesbest_stability()
        assert stable is False

    def test_yesbest_stability_empty(self):
        cache = CitySignalCache("Wellington", "April 7", "Pacific/Auckland")
        stable, threshold = cache.get_yesbest_stability()
        assert stable is False
        assert threshold is None


# ─── SmartEntryGate 测试 ──────────────────────────────────────────


class TestSmartEntryGate:
    """测试智能入场门控器。"""

    def _gate_with_config(self, cfg=None):
        return SmartEntryGate(), cfg or _default_config()

    @patch("src.signal.smart_entry.get")
    def test_disabled_passes_through(self, mock_get):
        """smart_entry_enabled=false 时直接透传信号。"""
        cfg = _default_config()
        cfg["weather_strategy"]["smart_entry_enabled"] = False
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        signals = [_make_signal()]
        result = gate.process("Seoul", "April 7", "Asia/Seoul", signals)
        assert len(result) == 1
        assert result[0].market_id == "m1"

    @patch("src.signal.smart_entry.get")
    def test_empty_signals_returns_empty(self, mock_get):
        mock_get.side_effect = _mock_get(_default_config())
        gate = SmartEntryGate()
        result = gate.process("Seoul", "April 7", "Asia/Seoul", [])
        assert result == []

    @patch("src.signal.smart_entry.get")
    @patch("src.signal.smart_entry.datetime")
    def test_before_window_caches_only(self, mock_dt, mock_get):
        """城市本地时间未到窗口 → 仅暂存，返回空。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        # Shanghai 本地时间 02:00（窗口=[06:00, 17:00]）→ 未到
        now_utc = datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc)  # 上海 02:00
        mock_dt.now.return_value = now_utc
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

        gate = SmartEntryGate()
        result = gate.process(
            "Shanghai",
            "April 7",
            "Asia/Shanghai",
            [_make_signal()],
            yesbest_threshold=15.0,
            yesbest_ask=0.35,
        )
        assert result == []

    @patch("src.signal.smart_entry.get")
    @patch("src.signal.smart_entry.datetime")
    def test_in_window_insufficient_rounds(self, mock_dt, mock_get):
        """在窗口内但轮次不足 → 仍返回空。"""
        cfg = _default_config()
        cfg["weather_strategy"]["min_history_rounds_for_buy"] = 3
        mock_get.side_effect = _mock_get(cfg)

        # Seoul 本地时间 08:00 (窗口=[06:00, 17:00]) → 在窗口内
        now_utc = datetime(2026, 4, 6, 23, 0, tzinfo=timezone.utc)  # Seoul 08:00
        mock_dt.now.return_value = now_utc
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

        gate = SmartEntryGate()
        # 只有第1轮
        result = gate.process(
            "Seoul",
            "April 7",
            "Asia/Seoul",
            [_make_signal()],
            yesbest_threshold=12.0,
            yesbest_ask=0.35,
        )
        assert result == []

    @patch("src.signal.smart_entry.get")
    @patch("src.signal.smart_entry.datetime")
    def test_in_window_sufficient_rounds_releases(self, mock_dt, mock_get):
        """在窗口内且轮次足够 → 释放增强信号。"""
        cfg = _default_config()
        cfg["weather_strategy"]["min_history_rounds_for_buy"] = 3
        cfg["weather_strategy"]["enable_price_trend_scoring"] = False
        mock_get.side_effect = _mock_get(cfg)

        # Seoul 本地时间 08:00
        now_utc = datetime(2026, 4, 6, 23, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = now_utc
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

        gate = SmartEntryGate()
        sig = _make_signal(market_id="m1", ev=0.15)

        # 前2轮：暂存
        for _ in range(2):
            result = gate.process(
                "Seoul",
                "April 7",
                "Asia/Seoul",
                [sig],
                yesbest_threshold=12.0,
                yesbest_ask=0.35,
            )
            assert result == []

        # 第3轮：释放
        result = gate.process(
            "Seoul",
            "April 7",
            "Asia/Seoul",
            [sig],
            yesbest_threshold=12.0,
            yesbest_ask=0.35,
        )
        assert len(result) == 1
        assert result[0].market_id == "m1"

    @patch("src.signal.smart_entry.get")
    @patch("src.signal.smart_entry.datetime")
    def test_unstable_signal_dropped(self, mock_dt, mock_get):
        """稳定性低于阈值的偶发信号被跳过。"""
        cfg = _default_config()
        cfg["weather_strategy"]["min_history_rounds_for_buy"] = 3
        cfg["weather_strategy"]["signal_stability_min_pct"] = 0.5
        cfg["weather_strategy"]["enable_price_trend_scoring"] = False
        mock_get.side_effect = _mock_get(cfg)

        now_utc = datetime(2026, 4, 6, 23, 0, tzinfo=timezone.utc)  # Seoul 08:00
        mock_dt.now.return_value = now_utc
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

        gate = SmartEntryGate()
        stable_sig = _make_signal(market_id="stable")
        unstable_sig = _make_signal(market_id="unstable")

        # 前2轮：只有 stable 出现
        for _ in range(2):
            gate.process(
                "Seoul",
                "April 7",
                "Asia/Seoul",
                [stable_sig],
                yesbest_threshold=12.0,
            )

        # 第3轮：stable + unstable 都出现（unstable 稳定性=1/3=33%<50%）
        result = gate.process(
            "Seoul",
            "April 7",
            "Asia/Seoul",
            [stable_sig, unstable_sig],
            yesbest_threshold=12.0,
        )
        assert len(result) == 1
        assert result[0].market_id == "stable"

    @patch("src.signal.smart_entry.get")
    @patch("src.signal.smart_entry.datetime")
    def test_price_trend_bonus(self, mock_dt, mock_get):
        """价格下降趋势给 EV 加成。"""
        cfg = _default_config()
        cfg["weather_strategy"]["min_history_rounds_for_buy"] = 3
        cfg["weather_strategy"]["enable_price_trend_scoring"] = True
        cfg["weather_strategy"]["trend_improving_bonus"] = 0.05
        cfg["weather_strategy"]["signal_stability_min_pct"] = 0.0
        mock_get.side_effect = _mock_get(cfg)

        now_utc = datetime(2026, 4, 7, 0, 0, tzinfo=timezone.utc)  # Seoul 09:00
        mock_dt.now.return_value = now_utc
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

        gate = SmartEntryGate()
        # 价格持续下降（对 NO 有利：ask_yes 下降 → NO方向利好）
        for p in [0.40, 0.35, 0.30]:
            sig = _make_signal(market_id="trend_test", outcome="NO", price=p, ev=0.10)
            result = gate.process(
                "Seoul",
                "April 7",
                "Asia/Seoul",
                [sig],
                yesbest_threshold=12.0,
            )

        # 第3轮释放，EV 应该有加成
        assert len(result) == 1
        assert result[0].ev > 0.10  # 原始 0.10 + trend bonus

    @patch("src.signal.smart_entry.get")
    def test_get_cache_status(self, mock_get):
        """缓存状态API返回正确结构。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        cache = CitySignalCache("Tokyo", "April 7", "Asia/Tokyo")
        cache.add_round([_make_signal()], yesbest_threshold=18.0)
        gate._caches[("tokyo", "april 7")] = cache

        status = gate.get_cache_status()
        assert len(status) == 1
        assert status[0]["city"] == "Tokyo"
        assert status[0]["rounds"] == 1

    def test_cleanup_all_expired(self):
        """清理全部过期缓存。"""
        with patch("src.signal.smart_entry.get") as mock_get:
            cfg = _default_config()
            cfg["weather_strategy"]["signal_cache_hours"] = 1.0
            mock_get.side_effect = _mock_get(cfg)

            gate = SmartEntryGate()
            cache = CitySignalCache("Old", "April 5", "UTC")
            cache.add_round([_make_signal()])
            # 强制设为2小时前
            cache.records[0].timestamp_utc = datetime.now(timezone.utc) - timedelta(
                hours=2
            )
            gate._caches[("old", "april 5")] = cache

            removed = gate.cleanup_all_expired()
            assert removed == 1
            assert len(gate._caches) == 0  # 空缓存被清理


# ─── SmartEntryGate 买入窗口计算测试 ──────────────────────────────


class TestBuyWindowCalculation:
    """测试不同时区城市的买入窗口判断。"""

    @patch("src.signal.smart_entry.get")
    def test_shanghai_06am_local(self, mock_get):
        """上海当地06:00 = peak_start(10) - offset(4) = 恰好进入窗口。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        # 上海 06:00 = UTC 22:00 (前一天)
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 6, 22, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, local_str, info = gate._check_buy_window("Asia/Shanghai", 4)
            assert in_w is True
            assert "06:00" in local_str

    @patch("src.signal.smart_entry.get")
    def test_shanghai_03am_local(self, mock_get):
        """上海当地03:00 → 未到窗口。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        # 上海 03:00 = UTC 19:00
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 6, 19, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, _, _ = gate._check_buy_window("Asia/Shanghai", 4)
            assert in_w is False

    @patch("src.signal.smart_entry.get")
    def test_buenos_aires_08am_local(self, mock_get):
        """Buenos Aires 当地08:00 → 在窗口内 [06:00-10:00)。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        # BA 08:00 = UTC 11:00 (UTC-3)
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 7, 11, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, _, _ = gate._check_buy_window("America/Argentina/Buenos_Aires", 4)
            assert in_w is True

    @patch("src.signal.smart_entry.get")
    def test_chicago_midnight_local(self, mock_get):
        """Chicago 当地00:00 → 未到窗口。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        # Chicago 00:00 = UTC 05:00 (CDT, UTC-5)
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 7, 5, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, _, _ = gate._check_buy_window("America/Chicago", 4)
            assert in_w is False

    @patch("src.signal.smart_entry.get")
    def test_london_09am_local(self, mock_get):
        """London 当地09:00 → 在窗口内 [06:00-10:00)。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        # London 09:00 BST = UTC 08:00
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 7, 8, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, _, _ = gate._check_buy_window("Europe/London", 4)
            assert in_w is True

    @patch("src.signal.smart_entry.get")
    def test_wellington_18pm_local(self, mock_get):
        """Wellington 当地18:00 → 超出窗口。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        # Wellington 18:00 NZST = UTC 06:00 (UTC+12)
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 7, 6, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, _, _ = gate._check_buy_window("Pacific/Auckland", 4)
            assert in_w is False

    @patch("src.signal.smart_entry.get")
    def test_seoul_1135_local_outside_window(self, mock_get):
        """Seoul 当地11:35 → 超出窗口 [06:00-10:00)（修复 April 7 泄漏BUG）。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        # Seoul 11:35 KST = UTC 02:35 (UTC+9)
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 7, 2, 35, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, _, _ = gate._check_buy_window("Asia/Seoul", 4)
            assert in_w is False

    @patch("src.signal.smart_entry.get")
    def test_london_10am_at_peak_start_excluded(self, mock_get):
        """London 当地10:00 = peak_start → 窗口已结束 [06:00-10:00)。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        # London 10:00 BST = UTC 09:00
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, _, _ = gate._check_buy_window("Europe/London", 4)
            assert in_w is False

    @patch("src.signal.smart_entry.get")
    def test_seoul_1135_local_outside_window(self, mock_get):
        """Seoul 当地11:35 → 超出窗口 [06:00-10:00)（修复 April 7 泄漏BUG）。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        # Seoul 11:35 KST = UTC 02:35 (UTC+9)
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 7, 2, 35, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, _, _ = gate._check_buy_window("Asia/Seoul", 4)
            assert in_w is False

    @patch("src.signal.smart_entry.get")
    def test_london_10am_at_peak_start_excluded(self, mock_get):
        """London 当地10:00 = peak_start → 窗口已结束 [06:00-10:00)。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        # London 10:00 BST = UTC 09:00
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, _, _ = gate._check_buy_window("Europe/London", 4)
            assert in_w is False

    @patch("src.signal.smart_entry.get")
    def test_shanghai_april11_is_today_group_in_window(self, mock_get):
        """BRT 4/10 15:18 ≈ Shanghai 4/11 02:18，April 11 应识别为当日组并在窗口内。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 10, 18, 18, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, local_str, info = gate._check_buy_window(
                "Asia/Shanghai", 8, "April 11"
            )
            assert in_w is True
            assert "02:18" in local_str
            assert "当日组" in info

    @patch("src.signal.smart_entry.get")
    def test_los_angeles_april11_is_tomorrow_group_outside_window(self, mock_get):
        """BRT 4/10 15:18 ≈ LA 4/10 11:18，April 11 应识别为明日组且不在窗口内。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        with patch("src.signal.smart_entry.datetime") as mock_dt:
            now_utc = datetime(2026, 4, 10, 18, 18, tzinfo=timezone.utc)
            mock_dt.now.return_value = now_utc
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

            in_w, local_str, info = gate._check_buy_window(
                "America/Los_Angeles", 8, "April 11"
            )
            assert in_w is False
            assert "11:18" in local_str
            assert "明日组" in info


# ─── Ambush 正常通过测试 ──────────────────────────────────────────


class TestAmbushSignalGeneration:
    """测试 ambush 信号生成的正常路径。"""

    @patch("src.signal.weather_strategy.fetch_ensemble_probability")
    @patch("src.signal.weather_strategy.get")
    def test_normal_ambush_passes(self, mock_get, mock_fetch):
        """正常 ambush: model_p=0.15, ask=0.05 → ratio=3.0 < 10 → 通过。"""
        from src.scanner.weather_scanner import CityDayGroup, TemperatureStrike
        from src.signal.weather_strategy import YesBestResult, build_yes_ambush_signals

        ws_cfg = {
            "ambush_enabled": True,
            "ambush_max_ask": 0.1,
            "ambush_min_distance_c": 1,
            "ambush_max_distance_c": 4,
            "ambush_size_usdc": 1.5,
            "ambush_max_signals": 1,
            "ambush_confidence_multiplier": 1.5,
            "ambush_confidence_cap": 0.95,
            "ambush_confidence_min": 0.15,
            "ambush_max_model_market_ratio": 10.0,
        }

        yesbest_strike = TemperatureStrike(
            market_id="yb14",
            condition_id="0x1",
            token_yes="t1",
            token_no="t1n",
            question="Will it be 14°C?",
            threshold=14.0,
            threshold_low=14.0,
            threshold_high=14.0,
            direction="exact",
            best_ask_yes=0.40,
            volume_24h=50000,
        )
        amb_strike = TemperatureStrike(
            market_id="amb12",
            condition_id="0x2",
            token_yes="t2",
            token_no="t2n",
            question="Will it be 12°C?",
            threshold=12.0,
            threshold_low=12.0,
            threshold_high=12.0,
            direction="exact",
            best_ask_yes=0.05,
            volume_24h=30000,
        )
        group = CityDayGroup(
            city_name="Seoul",
            city_slug="seoul",
            date_str="April 7",
            lat=37.46,
            lon=126.5,
            icao="RKSI",
            timezone="Asia/Seoul",
            strikes=[amb_strike, yesbest_strike],
        )

        result = YesBestResult(
            city_group=group,
            yes_best_strike=yesbest_strike,
            yes_best_idx=1,
            combined_score=0.40,
            model_prob=0.45,
            all_model_probs={
                "yb14": 0.45,
                "amb12": 0.15,  # ratio=0.15/0.05=3.0 < 10 → OK
            },
        )

        signals = build_yes_ambush_signals(group, result.all_model_probs, ws_cfg)
        assert len(signals) == 1, (
            f"Seoul 12°C (model=0.15, ask=0.05, ratio=3.0) 应通过检查，"
            f"but got {len(signals)} signals"
        )
        assert signals[0].outcome == "YES"
        assert signals[0].market_id == "amb12"


# ─── WeatherPipeline 集成测试 ─────────────────────────────────────


class TestWeatherPipelineSmartEntry:
    """测试 WeatherPipeline 与 SmartEntry 的集成。"""

    @patch("src.signal.weather_pipeline._is_tomorrow_or_today", return_value=False)
    @patch("src.signal.weather_pipeline._calc_hours_to_expiry", return_value=50.0)
    @patch("src.signal.weather_pipeline.process_city_group_ex")
    @patch("src.signal.weather_pipeline.WeatherScanner")
    @patch("src.signal.weather_pipeline.get")
    def test_smart_entry_disabled_passthrough(
        self, mock_get, mock_scanner_cls, mock_process, _mock_hrs, _mock_tmrw
    ):
        """smart_entry_enabled=false 时信号直接透传。"""
        from src.scanner.weather_scanner import CityDayGroup
        from src.signal.weather_pipeline import WeatherPipeline

        cfg = _default_config()
        cfg["weather_strategy"]["smart_entry_enabled"] = False
        mock_get.side_effect = _mock_get(cfg)

        group = MagicMock(spec=CityDayGroup)
        group.city_name = "Seoul"
        group.date_str = "April 7"
        group.timezone = "Asia/Seoul"

        mock_scanner = MagicMock()
        mock_scanner.scan_city_groups.return_value = [group]
        mock_scanner_cls.return_value = mock_scanner

        sig = _make_signal()
        mock_process.return_value = ([sig], 12.0, 0.35)

        result = WeatherPipeline.run_cycle(20.0)
        assert len(result) == 1
        assert result[0].market_id == "m1"

    @patch("src.signal.weather_pipeline._is_tomorrow_or_today", return_value=False)
    @patch("src.signal.weather_pipeline._calc_hours_to_expiry", return_value=50.0)
    @patch("src.signal.weather_pipeline.process_city_group_ex")
    @patch("src.signal.weather_pipeline.WeatherScanner")
    @patch("src.signal.weather_pipeline.get")
    def test_smart_entry_caches_when_not_in_window(
        self, mock_get, mock_scanner_cls, mock_process, _mock_hrs, _mock_tmrw
    ):
        """SmartEntry 启用但未到窗口 → 返回空。"""
        from src.scanner.weather_scanner import CityDayGroup
        from src.signal.weather_pipeline import WeatherPipeline, _smart_entry_gate

        cfg = _default_config()
        cfg["weather_strategy"]["smart_entry_enabled"] = True
        mock_get.side_effect = _mock_get(cfg)

        group = MagicMock(spec=CityDayGroup)
        group.city_name = "Chicago"
        group.date_str = "April 7"
        group.timezone = "America/Chicago"

        mock_scanner = MagicMock()
        mock_scanner.scan_city_groups.return_value = [group]
        mock_scanner_cls.return_value = mock_scanner

        sig = _make_signal()
        mock_process.return_value = ([sig], 15.0, 0.30)

        # Mock SmartEntryGate to return empty (not in window)
        with patch.object(_smart_entry_gate, "process", return_value=[]):
            with patch.object(_smart_entry_gate, "cleanup_all_expired", return_value=0):
                result = WeatherPipeline.run_cycle(20.0)
                assert len(result) == 0


class TestRemoteGroupSkipWhenAmbushDisabled:
    """ambush_enabled=False时，远期日组(非今天/明天)应被跳过。"""

    @patch(
        "src.signal.weather_pipeline._get_day_group_status",
        return_value=("2日后组", "2026-04-10 09:00", 2),
    )
    @patch("src.signal.weather_pipeline._is_tomorrow_or_today", return_value=False)
    @patch("src.signal.weather_pipeline._calc_hours_to_expiry", return_value=50.0)
    @patch("src.signal.weather_pipeline.process_city_group_ex")
    @patch("src.signal.weather_pipeline.WeatherScanner")
    @patch("src.signal.weather_pipeline.get")
    def test_remote_group_skipped_when_ambush_disabled(
        self,
        mock_get,
        mock_scanner_cls,
        mock_process,
        _mock_hrs,
        _mock_tmrw,
        _mock_day,
    ):
        """ambush关闭 + 远期日组 → process_city_group_ex 不应被调用"""
        from src.scanner.weather_scanner import CityDayGroup
        from src.signal.weather_pipeline import WeatherPipeline

        cfg = _default_config()
        cfg["weather_strategy"]["ambush_enabled"] = False
        cfg["weather_strategy"]["smart_entry_enabled"] = False
        mock_get.side_effect = _mock_get(cfg)

        group = MagicMock(spec=CityDayGroup)
        group.city_name = "Helsinki"
        group.date_str = "April 13"
        group.timezone = "Europe/Helsinki"

        mock_scanner = MagicMock()
        mock_scanner.scan_city_groups.return_value = [group]
        mock_scanner_cls.return_value = mock_scanner

        result = WeatherPipeline.run_cycle(20.0)
        assert len(result) == 0
        mock_process.assert_not_called()

    @patch(
        "src.signal.weather_pipeline._get_day_group_status",
        return_value=("2日后组", "2026-04-10 09:00", 2),
    )
    @patch("src.signal.weather_pipeline._is_tomorrow_or_today", return_value=False)
    @patch("src.signal.weather_pipeline._calc_hours_to_expiry", return_value=50.0)
    @patch("src.signal.weather_pipeline.process_city_group_ex")
    @patch("src.signal.weather_pipeline.WeatherScanner")
    @patch("src.signal.weather_pipeline.get")
    def test_remote_group_processed_when_ambush_enabled(
        self,
        mock_get,
        mock_scanner_cls,
        mock_process,
        _mock_hrs,
        _mock_tmrw,
        _mock_day,
    ):
        """ambush开启 + 远期日组 → process_city_group_ex 应被调用"""
        from src.scanner.weather_scanner import CityDayGroup
        from src.signal.weather_pipeline import WeatherPipeline

        cfg = _default_config()
        cfg["weather_strategy"]["ambush_enabled"] = True
        cfg["weather_strategy"]["smart_entry_enabled"] = False
        mock_get.side_effect = _mock_get(cfg)

        group = MagicMock(spec=CityDayGroup)
        group.city_name = "Helsinki"
        group.date_str = "April 13"
        group.timezone = "Europe/Helsinki"

        mock_scanner = MagicMock()
        mock_scanner.scan_city_groups.return_value = [group]
        mock_scanner_cls.return_value = mock_scanner

        sig = _make_signal()
        mock_process.return_value = ([sig], 12.0, 0.35)

        result = WeatherPipeline.run_cycle(20.0)
        assert len(result) == 1
        mock_process.assert_called_once()

    @patch(
        "src.signal.weather_pipeline._get_day_group_status",
        return_value=("明日组", "2026-04-10 09:00", 1),
    )
    @patch("src.signal.weather_pipeline._is_tomorrow_or_today", return_value=True)
    @patch("src.signal.weather_pipeline._calc_hours_to_expiry", return_value=20.0)
    @patch("src.signal.weather_pipeline.process_city_group_ex")
    @patch("src.signal.weather_pipeline.WeatherScanner")
    @patch("src.signal.weather_pipeline.get")
    def test_tomorrow_group_skipped_when_ambush_disabled_max0(
        self, mock_get, mock_scanner_cls, mock_process, _mock_hrs, _mock_tmrw, _mock_day
    ):
        """T178: ambush关闭 + max_days=0 + 明日组 → SmartEntry无法释放 → 应被跳过（不再处理）"""
        from src.scanner.weather_scanner import CityDayGroup
        from src.signal.weather_pipeline import WeatherPipeline

        cfg = _default_config()
        cfg["weather_strategy"]["ambush_enabled"] = False
        cfg["weather_strategy"]["smart_entry_enabled"] = False
        cfg["weather_strategy"]["max_days_until_event"] = 0
        mock_get.side_effect = _mock_get(cfg)

        group = MagicMock(spec=CityDayGroup)
        group.city_name = "Miami"
        group.date_str = "April 11"  # 当前运行时间是 April 10 → 明日组 days_diff=1
        group.timezone = "America/New_York"

        mock_scanner = MagicMock()
        mock_scanner.scan_city_groups.return_value = [group]
        mock_scanner_cls.return_value = mock_scanner

        result = WeatherPipeline.run_cycle(20.0)
        # max_days=0 时明日组被跳过，process_city_group_ex 不应被调用
        assert len(result) == 0
        mock_process.assert_not_called()

    @patch(
        "src.signal.weather_pipeline._get_day_group_status",
        return_value=("当日组", "2026-04-10 09:00", 0),
    )
    @patch("src.signal.weather_pipeline._is_tomorrow_or_today", return_value=True)
    @patch("src.signal.weather_pipeline._calc_hours_to_expiry", return_value=6.0)
    @patch("src.signal.weather_pipeline.process_city_group_ex")
    @patch("src.signal.weather_pipeline.WeatherScanner")
    @patch("src.signal.weather_pipeline.get")
    def test_today_group_always_processed(
        self, mock_get, mock_scanner_cls, mock_process, _mock_hrs, _mock_tmrw, _mock_day
    ):
        """T178: 当日组(days_diff=0)无论ambush是否开启、max_days如何都应被处理"""
        from src.scanner.weather_scanner import CityDayGroup
        from src.signal.weather_pipeline import WeatherPipeline

        cfg = _default_config()
        cfg["weather_strategy"]["ambush_enabled"] = False
        cfg["weather_strategy"]["smart_entry_enabled"] = False
        cfg["weather_strategy"]["max_days_until_event"] = 0
        mock_get.side_effect = _mock_get(cfg)

        group = MagicMock(spec=CityDayGroup)
        group.city_name = "Miami"
        group.date_str = "April 10"  # 今天 = days_diff=0（由 mock 保证）
        group.timezone = "America/New_York"

        mock_scanner = MagicMock()
        mock_scanner.scan_city_groups.return_value = [group]
        mock_scanner_cls.return_value = mock_scanner

        sig = _make_signal()
        mock_process.return_value = ([sig], 12.0, 0.35)

        result = WeatherPipeline.run_cycle(20.0)
        # days_diff=0 <= max_days=0 → 不跳过 → 处理并输出信号
        assert len(result) == 1
        mock_process.assert_called_once()

    @patch(
        "src.signal.weather_pipeline._get_day_group_status",
        return_value=("明日组", "2026-04-10 09:00", 1),
    )
    @patch("src.signal.weather_pipeline._is_tomorrow_or_today", return_value=True)
    @patch("src.signal.weather_pipeline._calc_hours_to_expiry", return_value=20.0)
    @patch("src.signal.weather_pipeline.process_city_group_ex")
    @patch("src.signal.weather_pipeline.WeatherScanner")
    @patch("src.signal.weather_pipeline.get")
    def test_tomorrow_group_processed_when_max_days_1(
        self, mock_get, mock_scanner_cls, mock_process, _mock_hrs, _mock_tmrw, _mock_day
    ):
        """T178: ambush关闭 + max_days=1 + 明日组 → SmartEntry可能释放次日信号 → 应被处理"""
        from src.scanner.weather_scanner import CityDayGroup
        from src.signal.weather_pipeline import WeatherPipeline

        cfg = _default_config()
        cfg["weather_strategy"]["ambush_enabled"] = False
        cfg["weather_strategy"]["smart_entry_enabled"] = False
        cfg["weather_strategy"]["max_days_until_event"] = 1
        mock_get.side_effect = _mock_get(cfg)

        group = MagicMock(spec=CityDayGroup)
        group.city_name = "Los Angeles"
        group.date_str = "April 11"  # 明日组 days_diff=1（由 mock 保证）
        group.timezone = "America/Los_Angeles"

        mock_scanner = MagicMock()
        mock_scanner.scan_city_groups.return_value = [group]
        mock_scanner_cls.return_value = mock_scanner

        sig = _make_signal()
        mock_process.return_value = ([sig], 60.5, 0.35)

        result = WeatherPipeline.run_cycle(20.0)
        # max_days=1 时明日组不再被跳过
        assert len(result) == 1
        mock_process.assert_called_once()


class TestProcessCityGroupEx:
    """测试 process_city_group_ex 返回正确的元信息。"""

    @patch("src.signal.weather_strategy._fetch_all_model_probs")
    @patch("src.signal.weather_strategy.get")
    def test_returns_yesbest_info(self, mock_get, mock_fetch):
        """验证返回 (signals, yb_threshold, yb_ask) 三元组。"""
        from src.scanner.weather_scanner import CityDayGroup, TemperatureStrike
        from src.signal.weather_strategy import process_city_group_ex

        ws_cfg = {
            "enabled": True,
            "double_no_size_usdc": 3.0,
            "right_tail_max_signals": 2,
            "no_max_entry_price": 0.80,
            "right_tail_confidence_cap": 0.95,
            "ambush_enabled": False,
        }
        mock_get.return_value = ws_cfg

        strikes = [
            TemperatureStrike(
                market_id="s1",
                condition_id="0x1",
                token_yes="t1",
                token_no="t1n",
                question="Will it be 10°C?",
                threshold=10.0,
                threshold_low=10.0,
                threshold_high=10.0,
                direction="exact",
                best_ask_yes=0.10,
                volume_24h=10000,
            ),
            TemperatureStrike(
                market_id="s2",
                condition_id="0x2",
                token_yes="t2",
                token_no="t2n",
                question="Will it be 12°C?",
                threshold=12.0,
                threshold_low=12.0,
                threshold_high=12.0,
                direction="exact",
                best_ask_yes=0.40,
                volume_24h=10000,
            ),
        ]
        group = CityDayGroup(
            city_name="Seoul",
            city_slug="seoul",
            date_str="April 7",
            lat=37.46,
            lon=126.5,
            icao="RKSI",
            timezone="Asia/Seoul",
            strikes=strikes,
        )
        mock_fetch.return_value = {"s1": 0.1, "s2": 0.3}

        signals, yb_thresh, yb_ask = process_city_group_ex(group)
        assert yb_thresh == 12.0  # highest ask=0.40
        assert yb_ask == 0.40
        assert isinstance(signals, list)

    @patch("src.signal.weather_strategy._fetch_all_model_probs")
    @patch("src.signal.weather_strategy.get")
    def test_returns_none_when_no_yesbest(self, mock_get, mock_fetch):
        """所有挡位无效时返回 ([], None, 0.0)。"""
        from src.scanner.weather_scanner import CityDayGroup
        from src.signal.weather_strategy import process_city_group_ex

        ws_cfg = {"enabled": True}
        mock_get.return_value = ws_cfg
        mock_fetch.return_value = {}

        group = CityDayGroup(
            city_name="TestCity",
            city_slug="test",
            date_str="April 7",
            lat=0,
            lon=0,
            icao="XXXX",
            timezone="UTC",
            strikes=[],  # 空挡位列表
        )

        signals, yb_thresh, yb_ask = process_city_group_ex(group)
        assert signals == []
        assert yb_thresh is None
        assert yb_ask == 0.0


# ─── T108/T109 增强测试 ───────────────────────────────────────────


class TestT108WindowInfoEnhanced:
    """T108: _check_buy_window 日志增强测试。"""

    @patch("src.signal.smart_entry.get")
    @patch("src.signal.smart_entry.datetime")
    def test_window_info_includes_offset_details(self, mock_dt, mock_get):
        """窗口信息应包含 peak/offset 计算详情。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        # Mock 时间为窗口内
        from datetime import datetime as real_dt
        from datetime import timezone

        fixed = real_dt(2026, 4, 7, 12, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = fixed
        mock_dt.side_effect = lambda *a, **k: real_dt(*a, **k)

        in_win, local_str, info = gate._check_buy_window("UTC", 4)

        # 应包含计算详情
        assert "peak_start=" in info
        assert "offset=" in info


class TestT108BetBucketPropagation:
    """T109: _enhance_signals 应传播 _bet_bucket 属性。"""

    @patch("src.signal.smart_entry.get")
    def test_bet_bucket_propagated_through_enhance(self, mock_get):
        """增强后的信号应保留原始 _bet_bucket 属性。"""
        cfg = _default_config()
        mock_get.side_effect = _mock_get(cfg)

        gate = SmartEntryGate()
        cache = CitySignalCache("Seoul", "April 7", "Asia/Seoul")

        # 添加3轮以满足 min_history_rounds
        sig = _make_signal(tag="weather_yes_ambush")
        sig._bet_bucket = "YesAmbush"
        snap = SignalSnapshot("m1", "YES", 0.1, 0.05, 0.8, "weather_yes_ambush")
        for _ in range(3):
            cache.add_round([snap], 15.0, 0.4)

        ws_cfg = cfg["weather_strategy"]
        enhanced = gate._enhance_signals([sig], cache, ws_cfg)

        assert len(enhanced) >= 1
        for es in enhanced:
            assert hasattr(es, "_bet_bucket")
            assert es._bet_bucket == "YesAmbush"


# ─── T111: YES 埋伏独立入场测试 ───────────────────────────────────


class TestT111AmbushIndependentEntry:
    """T111: YES 埋伏信号绕过 SmartEntry 直接输出。"""

    @patch("src.signal.weather_pipeline._is_tomorrow_or_today", return_value=False)
    @patch("src.signal.weather_pipeline._calc_hours_to_expiry", return_value=50.0)
    @patch("src.signal.weather_pipeline.process_city_group_ex")
    @patch("src.signal.weather_pipeline.WeatherScanner")
    @patch("src.signal.weather_pipeline.get")
    def test_ambush_bypasses_smart_entry(
        self, mock_get, mock_scanner_cls, mock_process, _mock_hrs, _mock_tmrw
    ):
        """ambush_independent_entry_enabled=true 时，YesAmbush 信号直接输出。"""
        from src.scanner.weather_scanner import CityDayGroup
        from src.signal.weather_pipeline import WeatherPipeline, _smart_entry_gate

        cfg = _default_config()
        cfg["weather_strategy"]["smart_entry_enabled"] = True
        cfg["weather_strategy"]["ambush_independent_entry_enabled"] = True
        mock_get.side_effect = _mock_get(cfg)

        group = MagicMock(spec=CityDayGroup)
        group.city_name = "Seoul"
        group.date_str = "April 8"
        group.timezone = "Asia/Seoul"

        mock_scanner = MagicMock()
        mock_scanner.scan_city_groups.return_value = [group]
        mock_scanner_cls.return_value = mock_scanner

        # 1 ambush + 1 NO signal
        ambush_sig = _make_signal(tag="weather_yes_ambush")
        ambush_sig._bet_bucket = "YesAmbush"
        no_sig = _make_signal(tag="weather_no")
        mock_process.return_value = ([ambush_sig, no_sig], 15.0, 0.03)

        # SmartEntry returns empty (not in window) — only NO should be blocked
        with patch.object(_smart_entry_gate, "process", return_value=[]) as mock_se:
            with patch.object(_smart_entry_gate, "cleanup_all_expired", return_value=0):
                result = WeatherPipeline.run_cycle(20.0)
                # Ambush should bypass, NO should be cached (empty)
                assert len(result) == 1
                assert result[0]._bet_bucket == "YesAmbush"
                # SmartEntry should only receive the NO signal
                assert mock_se.called
                call_args = mock_se.call_args
                assert (
                    len(
                        call_args.kwargs.get(
                            "raw_signals", call_args[1].get("raw_signals", [])
                        )
                    )
                    == 1
                )

    @patch("src.signal.weather_pipeline._is_tomorrow_or_today", return_value=False)
    @patch("src.signal.weather_pipeline._calc_hours_to_expiry", return_value=50.0)
    @patch("src.signal.weather_pipeline.process_city_group_ex")
    @patch("src.signal.weather_pipeline.WeatherScanner")
    @patch("src.signal.weather_pipeline.get")
    def test_ambush_independent_disabled_all_go_to_smart_entry(
        self, mock_get, mock_scanner_cls, mock_process, _mock_hrs, _mock_tmrw
    ):
        """ambush_independent_entry_enabled=false 时，全部信号走 SmartEntry。"""
        from src.scanner.weather_scanner import CityDayGroup
        from src.signal.weather_pipeline import WeatherPipeline, _smart_entry_gate

        cfg = _default_config()
        cfg["weather_strategy"]["smart_entry_enabled"] = True
        cfg["weather_strategy"]["ambush_independent_entry_enabled"] = False
        mock_get.side_effect = _mock_get(cfg)

        group = MagicMock(spec=CityDayGroup)
        group.city_name = "Miami"
        group.date_str = "April 8"
        group.timezone = "America/New_York"

        mock_scanner = MagicMock()
        mock_scanner.scan_city_groups.return_value = [group]
        mock_scanner_cls.return_value = mock_scanner

        ambush_sig = _make_signal(tag="weather_yes_ambush")
        ambush_sig._bet_bucket = "YesAmbush"
        no_sig = _make_signal(tag="weather_no")
        mock_process.return_value = ([ambush_sig, no_sig], 15.0, 0.03)

        # SmartEntry gets ALL signals when independent=false
        with patch.object(_smart_entry_gate, "process", return_value=[]) as mock_se:
            with patch.object(_smart_entry_gate, "cleanup_all_expired", return_value=0):
                result = WeatherPipeline.run_cycle(20.0)
                assert len(result) == 0  # all cached by SmartEntry
                assert mock_se.called
                call_args = mock_se.call_args
                raw = call_args.kwargs.get("raw_signals") or call_args[1].get(
                    "raw_signals", []
                )
                assert len(raw) == 2  # both signals sent to SmartEntry
                assert len(raw) == 2  # both signals sent to SmartEntry
                assert len(raw) == 2  # both signals sent to SmartEntry
                assert len(raw) == 2  # both signals sent to SmartEntry
                assert len(raw) == 2  # both signals sent to SmartEntry
                assert len(raw) == 2  # both signals sent to SmartEntry


class TestT124DateConstraint:
    """T124→T176-3: SmartEntry 日期约束 — 仅允许当日组(max_days_until_event=0)。"""

    @patch("src.signal.smart_entry.get")
    @patch("src.signal.smart_entry.datetime")
    def test_tomorrow_event_in_window_blocked(self, mock_dt, mock_get):
        """T176-3: 事件日=明天，当前在窗口内，max_days=0(默认) → 阻塞。
        双NO策略核心：买入窗口只投注当日温度市场，次日市场不投注。
        """
        cfg = _default_config()
        cfg["weather_strategy"]["min_history_rounds_for_buy"] = 1
        cfg["weather_strategy"]["enable_price_trend_scoring"] = False
        mock_get.side_effect = _mock_get(cfg)

        # Buenos Aires 本地时间 April 7 06:00 (窗口内), 事件日 April 8 (明天)
        now_utc = datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc)  # BA=UTC-3 → 06:00
        mock_dt.now.return_value = now_utc
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

        gate = SmartEntryGate()
        sig = _make_signal(market_id="ba-m1", ev=0.15)
        result = gate.process(
            "Buenos Aires",
            "April 8",
            "America/Argentina/Buenos_Aires",
            [sig],
        )
        assert result == []  # T176-3: 默认仅当日，次日不释放

    @patch("src.signal.smart_entry.get")
    @patch("src.signal.smart_entry.datetime")
    def test_tomorrow_event_released_when_max_days_one(self, mock_dt, mock_get):
        """max_days_until_event=1 时，次日事件允许释放（用户手动设置场景）。"""
        cfg = _default_config()
        cfg["weather_strategy"]["min_history_rounds_for_buy"] = 1
        cfg["weather_strategy"]["enable_price_trend_scoring"] = False
        cfg["weather_strategy"]["max_days_until_event"] = 1
        mock_get.side_effect = _mock_get(cfg)

        now_utc = datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = now_utc
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

        gate = SmartEntryGate()
        sig = _make_signal(market_id="ba-m1", ev=0.15)
        result = gate.process(
            "Buenos Aires",
            "April 8",
            "America/Argentina/Buenos_Aires",
            [sig],
        )
        assert len(result) == 1  # max_days=1 → 当日+次日，明天允许

    @patch("src.signal.smart_entry.get")
    @patch("src.signal.smart_entry.datetime")
    def test_day_after_tomorrow_event_blocked(self, mock_dt, mock_get):
        """事件日=后天(days_until=2)，max_days=0(默认) → 暂存，不释放。"""
        cfg = _default_config()
        cfg["weather_strategy"]["min_history_rounds_for_buy"] = 1
        cfg["weather_strategy"]["enable_price_trend_scoring"] = False
        mock_get.side_effect = _mock_get(cfg)

        # Buenos Aires 本地时间 April 7 06:00 (窗口内), 事件日 April 9 (后天)
        now_utc = datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = now_utc
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

        gate = SmartEntryGate()
        sig = _make_signal(market_id="ba-m2", ev=0.15)
        result = gate.process(
            "Buenos Aires",
            "April 9",
            "America/Argentina/Buenos_Aires",
            [sig],
        )
        assert result == []  # 后天的市场不应释放

    @patch("src.signal.smart_entry.get")
    @patch("src.signal.smart_entry.datetime")
    def test_today_event_in_window_releases(self, mock_dt, mock_get):
        """事件日=今天，当前在窗口内 → 正常释放。"""
        cfg = _default_config()
        cfg["weather_strategy"]["min_history_rounds_for_buy"] = 1
        cfg["weather_strategy"]["enable_price_trend_scoring"] = False
        cfg["weather_strategy"]["peak_window_buy_offset_hours"] = 6
        mock_get.side_effect = _mock_get(cfg)

        # NYC 本地时间 April 7 05:00 (窗口=[04:00,10:00)), 事件日 April 7 (今天)
        now_utc = datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc)  # NYC=UTC-4 → 05:00
        mock_dt.now.return_value = now_utc
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

        gate = SmartEntryGate()
        sig = _make_signal(market_id="nyc-m1", ev=0.15)
        result = gate.process(
            "NYC",
            "April 7",
            "America/New_York",
            [sig],
        )
        assert len(result) == 1  # 今天的市场应该释放

    def test_days_until_event_parsing(self):
        """_days_until_event 日期解析正确性。"""
        local_now = datetime(2026, 4, 7, 6, 0, tzinfo=timezone.utc)
        gate = SmartEntryGate()

        # 今天
        assert gate._days_until_event("April 7", local_now) == 0
        # 明天
        assert gate._days_until_event("April 8", local_now) == 1
        # 后天
        assert gate._days_until_event("April 9", local_now) == 2
        # 无效格式
        assert gate._days_until_event("InvalidDate", local_now) is None
        assert gate._days_until_event("InvalidDate", local_now) is None
        assert gate._days_until_event("InvalidDate", local_now) is None
        assert gate._days_until_event("InvalidDate", local_now) is None
