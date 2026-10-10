"""T93: 天气赛道隔离单元测试

规则：无论 weather_only 是 True 还是 False，
      天气市场必须被剔除出老管线（通过 _exclude_weather_markets），
      绝不得流入 scanner→signal_engine 老管线。
"""

import importlib
import os
import sys
import types
import unittest
from unittest import mock

# ── Make src importable ─────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class TestExcludeWeatherMarkets(unittest.TestCase):
    """直接测试 _exclude_weather_markets 的隔离逻辑（通过 category_detector）。"""

    def _make_market(self, tags: list, question: str = "Will it rain?") -> dict:
        return {"id": "mid1", "question": question, "tags": tags, "groupItemTagId": ""}

    def test_detect_category_returns_weather_for_weather_market(self):
        """category_detector.detect_category 对天气市场返回 'weather'。"""
        from src.utils.category_detector import detect_category

        weather_market = {
            "id": "abc",
            "question": "High in NYC above 80°F on July 4?",
            "tags": [{"id": "weather"}, {"id": "temperature"}],
            "groupItemTagId": "",
        }
        result = detect_category(weather_market)
        self.assertEqual(result, "weather")

    def test_detect_category_not_weather_for_sports_market(self):
        """非天气市场不应被识别为天气赛道。"""
        from src.utils.category_detector import detect_category

        sports_market = {
            "id": "xyz",
            "question": "Will Argentina win the World Cup?",
            "tags": [{"id": "soccer"}, {"id": "sports"}],
            "groupItemTagId": "",
        }
        result = detect_category(sports_market)
        self.assertNotEqual(result, "weather")

    def test_filter_removes_weather_markets_from_pool(self):
        """verify that filtering candidate_pool by detect_category != 'weather'
        removes weather markets and keeps non-weather markets — this is the
        exact logic used by _exclude_weather_markets in main.py."""
        from src.utils.category_detector import detect_category

        weather_market = {
            "id": "wm1",
            "question": "High in NYC above 80°F on July 4?",
            "tags": [{"id": "weather"}, {"id": "temperature"}],
            "groupItemTagId": "",
        }
        sports_market = {
            "id": "sm1",
            "question": "Will Argentina win?",
            "tags": [{"id": "soccer"}],
            "groupItemTagId": "",
        }
        candidate_pool = {
            "tier1": [weather_market, sports_market],
            "tier2": [weather_market],
        }

        # Replicate _exclude_weather_markets logic
        non_weather = {
            tier: [m for m in markets if detect_category(m) != "weather"]
            for tier, markets in candidate_pool.items()
        }

        # Weather market must be excluded
        self.assertNotIn(weather_market, non_weather["tier1"])
        self.assertNotIn(weather_market, non_weather.get("tier2", []))
        # Sports market must be kept
        self.assertIn(sports_market, non_weather["tier1"])

    def test_filter_empty_pool_returns_empty(self):
        """空候选池时过滤后仍为空（无崩溃）。"""
        from src.utils.category_detector import detect_category

        candidate_pool: dict = {}
        non_weather = {
            tier: [m for m in markets if detect_category(m) != "weather"]
            for tier, markets in candidate_pool.items()
        }
        self.assertEqual(non_weather, {})

    def test_filter_all_non_weather_pool_unchanged(self):
        """全非天气池，过滤后数量不变。"""
        from src.utils.category_detector import detect_category

        markets = [
            {
                "id": f"m{i}",
                "question": "Will team win?",
                "tags": [{"id": "sports"}],
                "groupItemTagId": "",
            }
            for i in range(5)
        ]
        candidate_pool = {"tier1": markets}
        non_weather = {
            tier: [m for m in ms if detect_category(m) != "weather"]
            for tier, ms in candidate_pool.items()
        }
        self.assertEqual(len(non_weather["tier1"]), 5)

    def test_weather_pipeline_can_be_imported(self):
        """T93启动验证：WeatherPipeline 模块必须可 import（proof of pipeline loaded）。"""
        from src.signal.weather_pipeline import WeatherPipeline  # noqa

        self.assertTrue(hasattr(WeatherPipeline, "run_cycle"))

    def test_weather_pipeline_run_cycle_returns_list(self):
        """WeatherPipeline.run_cycle() 在禁用时返回空列表（不抛异常）。"""
        from src.signal.weather_pipeline import WeatherPipeline

        # ws_cfg = get("weather_strategy") returns {} → enabled defaults to True
        # so mock it to return a dict with enabled=False
        with mock.patch(
            "src.signal.weather_pipeline.get",
            return_value={"enabled": False},
        ):
            result = WeatherPipeline.run_cycle(100.0)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)


class TestWeatherScannerTimeConstraints(unittest.TestCase):
    """T93 supplement: WeatherScanner 时间约束过滤单元测试。"""

    def _make_market(self, hours_to_expiry: float, cond_id: str = "0xabc") -> dict:
        """构建一个距离结算 hours_to_expiry 小时的 mock 市场。"""
        from datetime import datetime, timedelta, timezone

        end = datetime.now(timezone.utc) + timedelta(hours=hours_to_expiry)
        return {
            "id": "123",
            "conditionId": cond_id,
            "question": "Will the highest temperature in Paris be 20°C on April 8?",
            "bestAsk": "0.5",
            "active": True,
            "closed": False,
            "clobTokenIds": '["tok_yes","tok_no"]',
            "volume24hrClob": "5000",
            "endDate": end.isoformat().replace("+00:00", "Z"),
        }

    def test_temperature_strike_has_end_date_iso(self):
        """T93: TemperatureStrike 必须有 end_date_iso 字段。"""
        from src.scanner.weather_scanner import TemperatureStrike

        ts = TemperatureStrike(
            market_id="1",
            condition_id="0x1",
            token_yes="ty",
            token_no="tn",
            question="q",
            threshold=20.0,
            threshold_low=20.0,
            threshold_high=20.0,
            direction="exact",
            best_ask_yes=0.5,
            volume_24h=1000.0,
        )
        self.assertTrue(hasattr(ts, "end_date_iso"))
        self.assertEqual(ts.end_date_iso, "")

    def test_group_markets_filters_too_close_to_expiry(self):
        """WeatherScanner: 距结算 < 16h 的市场被 time constraint 过滤。"""
        from src.scanner.weather_scanner import WeatherScanner
        from src.utils.weather_fetcher import parse_weather_question

        scanner = WeatherScanner()
        # Market expiring in 5 hours: too close (< min_hours_to_expiry=16)
        m = self._make_market(5.0)
        city_map = {
            "paris": {
                "name": "Paris",
                "slug": "paris",
                "lat": 48.8,
                "lon": 2.3,
                "icao": "LFPG",
                "timezone": "Europe/Paris",
            }
        }
        groups = scanner._group_markets(
            [m], city_map, min_hours_to_expiry=16, max_hours_to_expiry=42
        )
        # All strikes should be filtered out
        total_strikes = sum(len(g.strikes) for g in groups)
        self.assertEqual(total_strikes, 0)

    def test_group_markets_filters_too_far_from_expiry(self):
        """WeatherScanner: 距结算 > 42h 的市场被 time constraint 过滤。"""
        from src.scanner.weather_scanner import WeatherScanner

        scanner = WeatherScanner()
        # Market expiring in 50 hours: too far (> entry_window=42)
        m = self._make_market(50.0)
        city_map = {
            "paris": {
                "name": "Paris",
                "slug": "paris",
                "lat": 48.8,
                "lon": 2.3,
                "icao": "LFPG",
                "timezone": "Europe/Paris",
            }
        }
        groups = scanner._group_markets(
            [m], city_map, min_hours_to_expiry=16, max_hours_to_expiry=42
        )
        total_strikes = sum(len(g.strikes) for g in groups)
        self.assertEqual(total_strikes, 0)

    def test_group_markets_accepts_valid_time_window(self):
        """WeatherScanner: 距结算在 [16h, 42h] 内的市场通过 time constraint。"""
        from src.scanner.weather_scanner import WeatherScanner

        scanner = WeatherScanner()
        # Market expiring in 24 hours: within valid window
        m = self._make_market(24.0)
        city_map = {
            "paris": {
                "name": "Paris",
                "slug": "paris",
                "lat": 48.8,
                "lon": 2.3,
                "icao": "LFPG",
                "timezone": "Europe/Paris",
            }
        }
        groups = scanner._group_markets(
            [m], city_map, min_hours_to_expiry=16, max_hours_to_expiry=42
        )
        # Should have at least one group with a strike (if parse_weather_question works)
        # We just check it doesn't get filtered by time (might fail on question parse)
        # The time filter should NOT reject this market
        # We mock parse_weather_question to ensure it works
        with mock.patch(
            "src.scanner.weather_scanner.parse_weather_question",
            return_value={
                "city": "Paris",
                "date_str": "2026-04-08",
                "threshold_value": 20.0,
                "threshold_low": 20.0,
                "threshold_high": 20.0,
                "direction": "exact",
            },
        ):
            groups2 = scanner._group_markets(
                [m], city_map, min_hours_to_expiry=16, max_hours_to_expiry=42
            )
        # Market was not filtered by time, so group should exist
        self.assertEqual(len(groups2), 1)
        self.assertEqual(len(groups2[0].strikes), 1)


class TestLiveFiltersWeatherCategoryFallback(unittest.TestCase):
    """T93 supplement: live_filters strategy_tag → category fallback。"""

    def _make_weather_signal(self, tag: str = "weather_right_tail_left"):
        """创建一个 WeatherPipeline 生成的 Signal mock。"""
        from unittest.mock import MagicMock

        sig = MagicMock()
        sig.market_id = "FAKE_WEATHER_MID_001"
        sig.question = "Will the highest temperature in Miami be 80°F on April 6?"
        sig.outcome = "NO"
        sig.ev = 0.05
        sig.confidence = 0.8
        sig.size_usdc = 3.0
        sig.skip = False
        sig.skip_reason = ""
        sig.token_id = "0x_fake_token"
        sig.expected_price = 0.5
        sig.condition_id = "0x_fake_cond"
        sig.strategy_tag = tag
        return sig

    def test_weather_pipeline_signal_category_fallback(self):
        """WeatherPipeline 信号（不在 market_lookup 中）应从 strategy_tag 推断 category='weather'。"""
        sig = self._make_weather_signal("weather_right_tail_left")
        market_lookup = {}  # Empty — simulates WeatherPipeline signals
        market = market_lookup.get(sig.market_id, {})
        _stag = getattr(sig, "strategy_tag", "") or ""

        if market:
            from src.utils.category_detector import detect_category

            category = detect_category(market)
        else:
            if _stag.startswith(("weather_double", "weather_yes")):
                category = "weather"
            else:
                category = "default"

        self.assertEqual(category, "weather")

    def test_yes_ambush_tag_maps_to_weather(self):
        """weather_yes_ambush strategy_tag 也应映射为 category='weather'。"""
        sig = self._make_weather_signal("weather_yes_ambush")
        market = {}
        _stag = getattr(sig, "strategy_tag", "") or ""

        if market:
            from src.utils.category_detector import detect_category

            category = detect_category(market)
        else:
            if _stag.startswith(("weather_double", "weather_yes")):
                category = "weather"
            else:
                category = "default"

        self.assertEqual(category, "weather")

    def test_non_weather_tag_stays_default(self):
        """非天气 strategy_tag 在空 market 时回落到 'default'。"""
        sig = self._make_weather_signal("bk_follow")
        market = {}
        _stag = getattr(sig, "strategy_tag", "") or ""

        if market:
            from src.utils.category_detector import detect_category

            category = detect_category(market)
        else:
            if _stag.startswith(("weather_double", "weather_yes")):
                category = "weather"
            else:
                category = "default"

        self.assertEqual(category, "default")


class TestMatchCityAliases(unittest.TestCase):
    """T104-fix: _match_city 别名匹配测试。"""

    def setUp(self):
        from src.scanner.weather_scanner import WeatherScanner

        self.scanner = WeatherScanner.__new__(WeatherScanner)

    def test_match_by_name(self):
        city_map = {"nyc": {"name": "NYC", "slug": "nyc"}}
        self.assertIsNotNone(self.scanner._match_city("NYC", city_map))

    def test_match_by_slug(self):
        city_map = {"los-angeles": {"name": "Los Angeles", "slug": "los-angeles"}}
        self.assertIsNotNone(self.scanner._match_city("los angeles", city_map))

    def test_match_by_alias(self):
        """Polymarket 用 'New York City'，config 中 name='NYC'，通过 aliases 匹配。"""
        city_map = {
            "nyc": {
                "name": "NYC",
                "slug": "nyc",
                "aliases": ["New York City", "New York"],
            }
        }
        result = self.scanner._match_city("New York City", city_map)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "NYC")

    def test_alias_case_insensitive(self):
        city_map = {"nyc": {"name": "NYC", "slug": "nyc", "aliases": ["New York City"]}}
        self.assertIsNotNone(self.scanner._match_city("new york city", city_map))

    def test_no_alias_no_match(self):
        """无 aliases 字段时，'New York City' 无法匹配 'NYC'。"""
        city_map = {"nyc": {"name": "NYC", "slug": "nyc"}}
        self.assertIsNone(self.scanner._match_city("New York City", city_map))

    def test_empty_aliases_no_match(self):
        city_map = {"nyc": {"name": "NYC", "slug": "nyc", "aliases": []}}
        self.assertIsNone(self.scanner._match_city("New York City", city_map))


class TestT112FixAmbushMinHoursToExpiry(unittest.TestCase):
    """T112-fix: ambush_min_hours_to_expiry 防护测试。

    验证：
      - _calc_hours_to_expiry 正确计算距结算时间
      - 单日组城市近期市场被 ambush_min_hours 拦截
      - 双日组城市远期市场正确放行
    """

    def test_calc_hours_to_expiry_normal(self):
        """正常 CityDayGroup 返回正确的小时数。"""
        from datetime import datetime, timedelta, timezone

        from src.scanner.weather_scanner import CityDayGroup, TemperatureStrike
        from src.signal.weather_pipeline import _calc_hours_to_expiry

        end_time = datetime.now(timezone.utc) + timedelta(hours=36)
        strike = TemperatureStrike(
            market_id="m1",
            condition_id="0x1",
            token_yes="ty",
            token_no="tn",
            question="q",
            threshold=20.0,
            threshold_low=20.0,
            threshold_high=20.0,
            direction="exact",
            best_ask_yes=0.5,
            volume_24h=1000,
        )
        strike.end_date_iso = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        group = CityDayGroup(
            city_name="TestCity",
            city_slug="testcity",
            date_str="2026-04-08",
            lat=0.0,
            lon=0.0,
            icao="XXXX",
            timezone="UTC",
            strikes=[strike],
        )
        hours = _calc_hours_to_expiry(group)
        self.assertIsNotNone(hours)
        self.assertAlmostEqual(hours, 36.0, delta=0.1)

    def test_calc_hours_to_expiry_empty_strikes(self):
        """空 strikes 列表返回 None。"""
        from src.scanner.weather_scanner import CityDayGroup
        from src.signal.weather_pipeline import _calc_hours_to_expiry

        group = CityDayGroup(
            city_name="TestCity",
            city_slug="testcity",
            date_str="2026-04-08",
            lat=0.0,
            lon=0.0,
            icao="XXXX",
            timezone="UTC",
            strikes=[],
        )
        self.assertIsNone(_calc_hours_to_expiry(group))

    def test_calc_hours_to_expiry_no_end_date(self):
        """end_date_iso 为空字符串时返回 None。"""
        from src.scanner.weather_scanner import CityDayGroup, TemperatureStrike
        from src.signal.weather_pipeline import _calc_hours_to_expiry

        strike = TemperatureStrike(
            market_id="m1",
            condition_id="0x1",
            token_yes="ty",
            token_no="tn",
            question="q",
            threshold=20.0,
            threshold_low=20.0,
            threshold_high=20.0,
            direction="exact",
            best_ask_yes=0.5,
            volume_24h=1000,
        )
        # end_date_iso defaults to ""
        group = CityDayGroup(
            city_name="TestCity",
            city_slug="testcity",
            date_str="2026-04-08",
            lat=0.0,
            lon=0.0,
            icao="XXXX",
            timezone="UTC",
            strikes=[strike],
        )
        self.assertIsNone(_calc_hours_to_expiry(group))

    def test_single_day_group_blocked_by_min_hours(self):
        """T112-fix 核心: 单日组城市（12h < 24h）ambush 被拦截。"""
        from datetime import datetime, timedelta, timezone

        from src.scanner.weather_scanner import CityDayGroup, TemperatureStrike

        # 创建 12h 后到期的 group（模拟 Shanghai April 7 场景）
        end_time = datetime.now(timezone.utc) + timedelta(hours=12)
        strike = TemperatureStrike(
            market_id="m1",
            condition_id="0x1",
            token_yes="ty",
            token_no="tn",
            question="Shanghai 15°C April 7?",
            threshold=15.0,
            threshold_low=15.0,
            threshold_high=15.0,
            direction="exact",
            best_ask_yes=0.05,
            volume_24h=1000,
        )
        strike.end_date_iso = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        group_near = CityDayGroup(
            city_name="Shanghai",
            city_slug="shanghai",
            date_str="2026-04-07",
            lat=31.14,
            lon=121.81,
            icao="ZSPD",
            timezone="Asia/Shanghai",
            strikes=[strike],
        )

        # 模拟 pipeline 的 farthest-group + min_hours 逻辑
        city_groups = [group_near]
        farthest_date = {}
        for g in city_groups:
            if (
                g.city_name not in farthest_date
                or g.date_str > farthest_date[g.city_name]
            ):
                farthest_date[g.city_name] = g.date_str

        ambush_min_hours = 24.0
        is_farthest = group_near.date_str == farthest_date.get(group_near.city_name)
        skip_ambush = not is_farthest

        self.assertTrue(is_farthest, "单日组应是最远组")
        self.assertFalse(skip_ambush, "最远组初始不应跳过")

        # T112-fix 保护
        from src.signal.weather_pipeline import _calc_hours_to_expiry

        _hrs = _calc_hours_to_expiry(group_near)
        if _hrs is not None and _hrs < ambush_min_hours:
            skip_ambush = True

        self.assertTrue(skip_ambush, "12h < 24h 应导致 skip_ambush=True")

    def test_far_group_passes_min_hours(self):
        """双日组城市远期（36h > 24h）ambush 正确放行。"""
        from datetime import datetime, timedelta, timezone

        from src.scanner.weather_scanner import CityDayGroup, TemperatureStrike

        end_time = datetime.now(timezone.utc) + timedelta(hours=36)
        strike = TemperatureStrike(
            market_id="m2",
            condition_id="0x2",
            token_yes="ty",
            token_no="tn",
            question="London 19°C April 8?",
            threshold=19.0,
            threshold_low=19.0,
            threshold_high=19.0,
            direction="exact",
            best_ask_yes=0.005,
            volume_24h=2000,
        )
        strike.end_date_iso = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        group_far = CityDayGroup(
            city_name="London",
            city_slug="london",
            date_str="2026-04-08",
            lat=51.51,
            lon=-0.06,
            icao="EGLC",
            timezone="Europe/London",
            strikes=[strike],
        )

        from src.signal.weather_pipeline import _calc_hours_to_expiry

        ambush_min_hours = 24.0
        is_farthest = True
        skip_ambush = not is_farthest  # False

        _hrs = _calc_hours_to_expiry(group_far)
        if _hrs is not None and _hrs < ambush_min_hours:
            skip_ambush = True

        self.assertFalse(skip_ambush, "36h > 24h 应允许 ambush")

    def test_dual_group_near_date_always_skipped(self):
        """双日组中近期日组（非最远）无论时间都跳过 ambush。"""
        from datetime import datetime, timedelta, timezone

        from src.scanner.weather_scanner import CityDayGroup, TemperatureStrike

        end_near = datetime.now(timezone.utc) + timedelta(hours=12)
        end_far = datetime.now(timezone.utc) + timedelta(hours=36)

        def make_strike(end_dt):
            s = TemperatureStrike(
                market_id="m",
                condition_id="0x",
                token_yes="ty",
                token_no="tn",
                question="q",
                threshold=20.0,
                threshold_low=20.0,
                threshold_high=20.0,
                direction="exact",
                best_ask_yes=0.5,
                volume_24h=1000,
            )
            s.end_date_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            return s

        near_group = CityDayGroup(
            city_name="London",
            city_slug="london",
            date_str="2026-04-07",
            lat=51.51,
            lon=-0.06,
            icao="EGLC",
            timezone="Europe/London",
            strikes=[make_strike(end_near)],
        )
        far_group = CityDayGroup(
            city_name="London",
            city_slug="london",
            date_str="2026-04-08",
            lat=51.51,
            lon=-0.06,
            icao="EGLC",
            timezone="Europe/London",
            strikes=[make_strike(end_far)],
        )

        city_groups = [near_group, far_group]
        farthest_date = {}
        for g in city_groups:
            if (
                g.city_name not in farthest_date
                or g.date_str > farthest_date[g.city_name]
            ):
                farthest_date[g.city_name] = g.date_str

        # Near group
        is_farthest_near = near_group.date_str == farthest_date["London"]
        self.assertFalse(is_farthest_near, "April 7 不是最远组")
        skip_near = not is_farthest_near
        self.assertTrue(skip_near, "近期组 ambush 应被跳过")

        # Far group
        is_farthest_far = far_group.date_str == farthest_date["London"]
        self.assertTrue(is_farthest_far, "April 8 是最远组")


if __name__ == "__main__":
    unittest.main()


class TestT114ScannerEventsLimit(unittest.TestCase):
    """T114-A: Scanner gamma_events_limit 从配置读取。"""

    def test_scanner_uses_config_limit(self):
        """_fetch_events 使用 config.json 的 gamma_events_limit。"""
        from unittest.mock import MagicMock, patch

        from src.scanner.weather_scanner import WeatherScanner

        scanner = WeatherScanner.__new__(WeatherScanner)
        scanner._session = MagicMock()

        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        scanner._session.get.return_value = mock_resp

        with patch("src.scanner.weather_scanner.get", return_value=500):
            scanner._fetch_events()

        call_kwargs = scanner._session.get.call_args
        params = call_kwargs[1].get("params") or call_kwargs.kwargs.get("params")
        self.assertEqual(params["limit"], 500)

    def test_scanner_default_limit_500(self):
        """无 config 时 fallback 为 500。"""
        from unittest.mock import MagicMock, patch

        from src.scanner.weather_scanner import WeatherScanner

        scanner = WeatherScanner.__new__(WeatherScanner)
        scanner._session = MagicMock()

        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        scanner._session.get.return_value = mock_resp

        with patch("src.scanner.weather_scanner.get", return_value=None):
            scanner._fetch_events()

        call_kwargs = scanner._session.get.call_args
        params = call_kwargs[1].get("params") or call_kwargs.kwargs.get("params")
        self.assertEqual(params["limit"], 500)


class TestT114AmbushMaxUndervaluation(unittest.TestCase):
    """T114-B: ambush_max_undervaluation 市场怀疑护盾测试。"""

    def _make_group_and_probs(self, ask: float, model_p: float):
        """构造测试用 CityDayGroup 和概率字典。"""
        from src.scanner.weather_scanner import CityDayGroup, TemperatureStrike

        strike = TemperatureStrike(
            market_id="m_test",
            condition_id="0xtest",
            token_yes="ty_test",
            token_no="tn_test",
            question="Seoul 9°C April 8?",
            threshold=9.0,
            threshold_low=9.0,
            threshold_high=9.0,
            direction="exact",
            best_ask_yes=ask,
            volume_24h=50000,
        )
        group = CityDayGroup(
            city_name="Seoul",
            city_slug="seoul",
            date_str="2026-04-08",
            lat=37.57,
            lon=126.97,
            icao="RKSI",
            timezone="Asia/Seoul",
            strikes=[strike],
        )
        probs = {"m_test": model_p}
        return group, probs

    def test_extreme_undervaluation_rejected(self):
        """Seoul 9°C: model_p=0.867/ask=0.033 = 26.3x > 15 → 被拦截。"""
        from src.signal.weather_strategy import build_yes_ambush_signals

        group, probs = self._make_group_and_probs(ask=0.033, model_p=0.867)
        ws_cfg = {
            "ambush_max_ask": 0.04,
            "ambush_confidence_multiplier": 1.6,
            "ambush_confidence_min": 0.15,
            "ambush_confidence_cap": 0.95,
            "ambush_max_signals": 1,
            "ambush_size_usdc": 1.5,
            "ambush_max_undervaluation": 15.0,
        }
        signals = build_yes_ambush_signals(group, probs, ws_cfg)
        self.assertEqual(len(signals), 0, "26.3x 超过 15.0 上限应被拦截")

    def test_normal_undervaluation_passes(self):
        """model_p=0.40/ask=0.04 = 10x < 15 → 正常通过。"""
        from src.signal.weather_strategy import build_yes_ambush_signals

        group, probs = self._make_group_and_probs(ask=0.04, model_p=0.40)
        ws_cfg = {
            "ambush_max_ask": 0.04,
            "ambush_confidence_multiplier": 1.6,
            "ambush_confidence_min": 0.15,
            "ambush_confidence_cap": 0.95,
            "ambush_max_signals": 1,
            "ambush_size_usdc": 1.5,
            "ambush_max_undervaluation": 15.0,
        }
        signals = build_yes_ambush_signals(group, probs, ws_cfg)
        self.assertEqual(len(signals), 1, "10x < 15.0 应正常通过")

    def test_boundary_undervaluation_at_limit(self):
        """model_p=0.60/ask=0.04 = 15.0x 恰好等于上限 → 应通过。"""
        from src.signal.weather_strategy import build_yes_ambush_signals

        group, probs = self._make_group_and_probs(ask=0.04, model_p=0.60)
        ws_cfg = {
            "ambush_max_ask": 0.04,
            "ambush_confidence_multiplier": 1.6,
            "ambush_confidence_min": 0.15,
            "ambush_confidence_cap": 0.95,
            "ambush_max_signals": 1,
            "ambush_size_usdc": 1.5,
            "ambush_max_undervaluation": 15.0,
        }
        signals = build_yes_ambush_signals(group, probs, ws_cfg)
        self.assertEqual(len(signals), 1, "恰好 15.0x 不应被拦截（>才拦截）")

    def test_no_max_uv_config_uses_default(self):
        """未配置 ambush_max_undervaluation 时使用默认值 15.0。"""
        from src.signal.weather_strategy import build_yes_ambush_signals

        group, probs = self._make_group_and_probs(ask=0.033, model_p=0.867)
        ws_cfg = {
            "ambush_max_ask": 0.04,
            "ambush_confidence_multiplier": 1.6,
            "ambush_confidence_min": 0.15,
            "ambush_confidence_cap": 0.95,
            "ambush_max_signals": 1,
            "ambush_size_usdc": 1.5,
            # ambush_max_undervaluation NOT set → default 15.0
        }
        signals = build_yes_ambush_signals(group, probs, ws_cfg)
        self.assertEqual(len(signals), 0, "默认值 15.0 应拦截 26.3x")


# ═══════════════════════════════════════════════════════════════════════
# T115: 全天气引擎最大化利用 — 双模型优先 + GFS降级 + 增强日志
# ═══════════════════════════════════════════════════════════════════════


class TestT115DualModelIntegration(unittest.TestCase):
    """T115: _fetch_all_model_probs 优先调用 dual-model，降级到 GFS-only。"""

    def _make_strike(self):
        from src.scanner.weather_scanner import TemperatureStrike

        return TemperatureStrike(
            market_id="m_t115",
            condition_id="0xt115",
            token_yes="ty_t115",
            token_no="tn_t115",
            question="Boston high above 60°F on April 10?",
            threshold=60.0,
            threshold_low=60.0,
            threshold_high=60.0,
            direction="gte",
            best_ask_yes=0.03,
            volume_24h=30000,
        )

    @mock.patch("src.signal.weather_strategy.fetch_ensemble_probability")
    @mock.patch("src.signal.weather_strategy.fetch_dual_model_probability")
    def test_dual_model_priority(self, mock_dual, mock_gfs):
        """双模型成功时优先使用，不调用 GFS-only。"""
        from src.signal.weather_strategy import _fetch_all_model_probs

        mock_dual.return_value = {
            "probability": 0.72,
            "source": "dual_model_gfs_ecmwf",
            "gfs_probability": 0.68,
            "ecmwf_probability": 0.75,
            "divergence": 0.07,
            "confidence_level": "high",
            "metar_calibration": "METAR当日偏暖+0.5°C, P+0.02",
        }

        strike = self._make_strike()
        probs = _fetch_all_model_probs([strike])

        self.assertIn("m_t115", probs)
        self.assertAlmostEqual(probs["m_t115"], 0.72)
        mock_dual.assert_called_once()
        mock_gfs.assert_not_called()

    @mock.patch("src.signal.weather_strategy.fetch_ensemble_probability")
    @mock.patch("src.signal.weather_strategy.fetch_dual_model_probability")
    def test_fallback_to_gfs_when_ecmwf_unavailable(self, mock_dual, mock_gfs):
        """ECMWF不可用时降级到 GFS-only。"""
        from src.signal.weather_strategy import _fetch_all_model_probs

        mock_dual.return_value = None
        mock_gfs.return_value = {"probability": 0.55, "source": "ensemble"}

        strike = self._make_strike()
        probs = _fetch_all_model_probs([strike])

        self.assertIn("m_t115", probs)
        self.assertAlmostEqual(probs["m_t115"], 0.55)
        mock_dual.assert_called_once()
        mock_gfs.assert_called_once()

    @mock.patch("src.signal.weather_strategy.fetch_ensemble_probability")
    @mock.patch("src.signal.weather_strategy.fetch_dual_model_probability")
    def test_divergence_skip_fallback_to_gfs(self, mock_dual, mock_gfs):
        """双模型分歧过大时降级到 GFS-only。"""
        from src.signal.weather_strategy import _fetch_all_model_probs

        mock_dual.return_value = {
            "__divergence_skip__": True,
            "gfs_probability": 0.80,
            "ecmwf_probability": 0.50,
            "divergence": 0.30,
        }
        mock_gfs.return_value = {"probability": 0.80, "source": "ensemble"}

        strike = self._make_strike()
        probs = _fetch_all_model_probs([strike])

        self.assertIn("m_t115", probs)
        self.assertAlmostEqual(probs["m_t115"], 0.80)
        mock_dual.assert_called_once()
        mock_gfs.assert_called_once()

    @mock.patch("src.signal.weather_strategy.fetch_ensemble_probability")
    @mock.patch("src.signal.weather_strategy.fetch_dual_model_probability")
    def test_details_cache_populated(self, mock_dual, mock_gfs):
        """双模型成功时 _DUAL_MODEL_DETAILS 缓存被正确填充。"""
        import src.signal.weather_strategy as ws

        detail = {
            "probability": 0.65,
            "source": "dual_model_gfs_ecmwf",
            "gfs_probability": 0.60,
            "ecmwf_probability": 0.68,
            "divergence": 0.08,
            "confidence_level": "medium",
            "metar_calibration": None,
        }
        mock_dual.return_value = detail

        strike = self._make_strike()
        ws._fetch_all_model_probs([strike])

        self.assertIn("m_t115", ws._DUAL_MODEL_DETAILS)
        self.assertEqual(ws._DUAL_MODEL_DETAILS["m_t115"]["gfs_probability"], 0.60)
        self.assertEqual(ws._DUAL_MODEL_DETAILS["m_t115"]["ecmwf_probability"], 0.68)

    @mock.patch("src.signal.weather_strategy.fetch_ensemble_probability")
    @mock.patch("src.signal.weather_strategy.fetch_dual_model_probability")
    def test_divergence_skip_details_recorded(self, mock_dual, mock_gfs):
        """分歧跳过时详情缓存记录 fallback_reason。"""
        import src.signal.weather_strategy as ws

        mock_dual.return_value = {
            "__divergence_skip__": True,
            "gfs_probability": 0.90,
            "ecmwf_probability": 0.55,
            "divergence": 0.35,
        }
        mock_gfs.return_value = {"probability": 0.90}

        strike = self._make_strike()
        ws._fetch_all_model_probs([strike])

        self.assertIn("m_t115", ws._DUAL_MODEL_DETAILS)
        self.assertEqual(
            ws._DUAL_MODEL_DETAILS["m_t115"]["_fallback_reason"], "divergence_skip"
        )


class TestT115AmbushEnhancedLogging(unittest.TestCase):
    """T115: ambush 信号日志增强 — 显示 GFS/ECMWF/METAR 引擎数据。"""

    def _make_group_and_probs(self, ask=0.03, model_p=0.40):
        from src.scanner.weather_scanner import CityDayGroup, TemperatureStrike

        strike = TemperatureStrike(
            market_id="m_log",
            condition_id="0xlog",
            token_yes="ty_log",
            token_no="tn_log",
            question="Boston high above 60°F on April 10?",
            threshold=60.0,
            threshold_low=60.0,
            threshold_high=60.0,
            direction="gte",
            best_ask_yes=ask,
            volume_24h=30000,
        )
        group = CityDayGroup(
            city_name="Boston",
            city_slug="boston",
            date_str="2026-04-10",
            lat=42.36,
            lon=-71.06,
            icao="KBOS",
            timezone="America/New_York",
            strikes=[strike],
        )
        return group, {"m_log": model_p}

    def test_ambush_signal_with_dual_model_details_logs(self):
        """信号生成时日志中包含 GFS/ECMWF/METAR 字段。"""
        import src.signal.weather_strategy as ws

        # 注入双模型详情缓存
        ws._DUAL_MODEL_DETAILS = {
            "m_log": {
                "probability": 0.40,
                "source": "dual_model_gfs_ecmwf",
                "gfs_probability": 0.38,
                "ecmwf_probability": 0.42,
                "divergence": 0.04,
                "confidence_level": "high",
                "metar_calibration": "METAR当日偏暖+0.3°C, P+0.01",
            }
        }

        group, probs = self._make_group_and_probs(ask=0.03, model_p=0.40)
        ws_cfg = {
            "ambush_max_ask": 0.04,
            "ambush_confidence_multiplier": 1.6,
            "ambush_confidence_min": 0.15,
            "ambush_confidence_cap": 0.95,
            "ambush_max_signals": 1,
            "ambush_size_usdc": 1.5,
            "ambush_max_undervaluation": 15.0,
        }

        with self.assertLogs("src.signal.weather_strategy", level="INFO") as cm:
            signals = ws.build_yes_ambush_signals(group, probs, ws_cfg)

        self.assertEqual(len(signals), 1)
        # 检查日志中包含 GFS/ECMWF/METAR 字段
        all_logs = "\n".join(cm.output)
        self.assertIn("GFS=0.380", all_logs)
        self.assertIn("ECMWF=0.420", all_logs)
        self.assertIn("METAR:", all_logs)
        self.assertIn("YesAmbush-Fusion", all_logs)

    def test_ambush_gfs_only_fallback_logs(self):
        """GFS-only 降级时日志显示 fallback 原因。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {
            "m_log": {
                "_fallback_reason": "ecmwf_unavailable",
                "source": "ensemble",
            }
        }

        group, probs = self._make_group_and_probs(ask=0.03, model_p=0.40)
        ws_cfg = {
            "ambush_max_ask": 0.04,
            "ambush_confidence_multiplier": 1.6,
            "ambush_confidence_min": 0.15,
            "ambush_confidence_cap": 0.95,
            "ambush_max_signals": 1,
            "ambush_size_usdc": 1.5,
            "ambush_max_undervaluation": 15.0,
        }

        with self.assertLogs("src.signal.weather_strategy", level="INFO") as cm:
            signals = ws.build_yes_ambush_signals(group, probs, ws_cfg)

        self.assertEqual(len(signals), 1)
        all_logs = "\n".join(cm.output)
        self.assertIn("fallback=ecmwf_unavailable", all_logs)


class TestT116FusionLogging(unittest.TestCase):
    """T116: 全气象数据源深度融合 — YES_best/DoubleNO/Ambush 独立融合日志。"""

    def _make_strike(self, mid="m_t116", ask=0.65, threshold=80.0):
        from src.scanner.weather_scanner import TemperatureStrike

        return TemperatureStrike(
            market_id=mid,
            condition_id="0xt116",
            token_yes=f"ty_{mid}",
            token_no=f"tn_{mid}",
            question=f"NYC high above {threshold}°F on April 12?",
            threshold=threshold,
            threshold_low=threshold,
            threshold_high=threshold,
            direction="gte",
            best_ask_yes=ask,
            volume_24h=50000,
        )

    def _make_city_group(self, strikes):
        from src.scanner.weather_scanner import CityDayGroup

        return CityDayGroup(
            city_name="NYC",
            city_slug="nyc",
            date_str="2026-04-12",
            lat=40.71,
            lon=-74.01,
            icao="KJFK",
            timezone="America/New_York",
            strikes=strikes,
        )

    def _dual_detail(self, mid="m_t116"):
        return {
            mid: {
                "probability": 0.70,
                "source": "dual_model_gfs_ecmwf",
                "gfs_probability": 0.67,
                "ecmwf_probability": 0.73,
                "divergence": 0.06,
                "confidence_level": "high",
                "metar_calibration": "METAR当日偏暖+0.2°C, P+0.01",
                "days_ahead": 2,
                "position_multiplier": 1.0,
            }
        }

    def test_yesbest_fusion_log(self):
        """find_yes_best 输出 [YesBest-Fusion] 日志含 GFS/ECMWF/skill。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = self._dual_detail()
        strike = self._make_strike()
        group = self._make_city_group([strike])
        probs = {"m_t116": 0.70}
        ws_cfg = {"enabled": True}

        with self.assertLogs("src.signal.weather_strategy", level="INFO") as cm:
            result = ws.find_yes_best(group, probs, ws_cfg)

        self.assertIsNotNone(result)
        all_logs = "\n".join(cm.output)
        self.assertIn("YesBest-Fusion", all_logs)
        self.assertIn("GFS=0.670", all_logs)
        self.assertIn("ECMWF=0.730", all_logs)
        self.assertIn("horizon=2d(skill=0.90)", all_logs)

    def test_double_no_fusion_log(self):
        """build_right_tail_signals 每条信号含融合日志。"""
        import src.signal.weather_strategy as ws

        best_strike = self._make_strike("m_best", ask=0.65, threshold=80.0)
        no_strike = self._make_strike("m_no", ask=0.15, threshold=75.0)
        group = self._make_city_group([best_strike, no_strike])

        ws._DUAL_MODEL_DETAILS = {
            "m_no": {
                "probability": 0.12,
                "source": "dual_model_gfs_ecmwf",
                "gfs_probability": 0.10,
                "ecmwf_probability": 0.14,
                "divergence": 0.04,
                "confidence_level": "high",
                "days_ahead": 3,
            }
        }

        result = ws.YesBestResult(
            city_group=group,
            yes_best_strike=best_strike,
            yes_best_idx=0,
            combined_score=0.65,
            model_prob=0.70,
            all_model_probs={"m_best": 0.70, "m_no": 0.12},
        )
        ws_cfg = {
            "double_no_size_usdc": 3.0,
            "no_max_entry_price": 0.90,
            "right_tail_max_signals": 2,
            "right_tail_confidence_cap": 0.95,
        }

        with self.assertLogs("src.signal.weather_strategy", level="INFO") as cm:
            signals = ws.build_right_tail_signals(result, ws_cfg)

        self.assertEqual(len(signals), 1)
        all_logs = "\n".join(cm.output)
        self.assertIn("GFS=0.100", all_logs)
        self.assertIn("ECMWF=0.140", all_logs)
        self.assertIn("horizon=3d(skill=0.82)", all_logs)

    def test_ambush_fusion_log_replaces_t115(self):
        """ambush 日志使用 [YesAmbush-Fusion] 标签（非 T115）。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {
            "m_amb": {
                "probability": 0.35,
                "source": "dual_model_gfs_ecmwf",
                "gfs_probability": 0.33,
                "ecmwf_probability": 0.37,
                "divergence": 0.04,
                "confidence_level": "high",
                "metar_calibration": None,
                "days_ahead": 1,
            }
        }

        strike = self._make_strike("m_amb", ask=0.03, threshold=90.0)
        group = self._make_city_group([strike])
        probs = {"m_amb": 0.35}
        ws_cfg = {
            "ambush_max_ask": 0.04,
            "ambush_confidence_multiplier": 1.6,
            "ambush_confidence_min": 0.15,
            "ambush_confidence_cap": 0.95,
            "ambush_max_signals": 1,
            "ambush_size_usdc": 1.5,
            "ambush_max_undervaluation": 15.0,
        }

        with self.assertLogs("src.signal.weather_strategy", level="INFO") as cm:
            signals = ws.build_yes_ambush_signals(group, probs, ws_cfg)

        self.assertEqual(len(signals), 1)
        all_logs = "\n".join(cm.output)
        self.assertIn("YesAmbush-Fusion", all_logs)
        self.assertNotIn("YesAmbush-T115", all_logs)
        self.assertIn("GFS=0.330", all_logs)
        self.assertIn("horizon=1d(skill=0.95)", all_logs)


class TestT116SkillCurve(unittest.TestCase):
    """T116: _get_skill_factor 在策略层正确利用。"""

    def test_skill_factor_from_defaults(self):
        """无 config 时从内置默认值读取。"""
        from src.signal.weather_strategy import _get_skill_factor

        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            self.assertAlmostEqual(_get_skill_factor(0), 0.98)
            self.assertAlmostEqual(_get_skill_factor(3), 0.82)
            self.assertAlmostEqual(_get_skill_factor(7), 0.42)
            self.assertAlmostEqual(_get_skill_factor(None), 1.0)

    def test_skill_factor_from_config(self):
        """config 指定 curve 时优先使用。"""
        from src.signal.weather_strategy import _get_skill_factor

        custom_cfg = {"skill_score_curve": {"0": 0.99, "3": 0.85, "7": 0.50}}
        with mock.patch("src.signal.weather_strategy.get", return_value=custom_cfg):
            self.assertAlmostEqual(_get_skill_factor(0), 0.99)
            self.assertAlmostEqual(_get_skill_factor(3), 0.85)
            self.assertAlmostEqual(_get_skill_factor(7), 0.50)

    def test_no_signal_confidence_scaled_by_skill(self):
        """DoubleNO 信号 confidence 被 skill factor 缩放。"""
        import src.signal.weather_strategy as ws

        best_strike = ws.TemperatureStrike(
            market_id="m_best",
            condition_id="0x1",
            token_yes="ty1",
            token_no="tn1",
            question="NYC high above 80°F?",
            threshold=80.0,
            threshold_low=80.0,
            threshold_high=80.0,
            direction="gte",
            best_ask_yes=0.65,
            volume_24h=10000,
        )
        no_strike = ws.TemperatureStrike(
            market_id="m_no_sk",
            condition_id="0x2",
            token_yes="ty2",
            token_no="tn2",
            question="NYC high above 75°F?",
            threshold=75.0,
            threshold_low=75.0,
            threshold_high=75.0,
            direction="gte",
            best_ask_yes=0.20,
            volume_24h=10000,
        )
        group = ws.CityDayGroup(
            city_name="NYC",
            city_slug="nyc",
            date_str="2026-04-15",
            lat=40.71,
            lon=-74.01,
            icao="KJFK",
            timezone="America/New_York",
            strikes=[best_strike, no_strike],
        )

        ws._DUAL_MODEL_DETAILS = {"m_no_sk": {"days_ahead": 5, "gfs_probability": 0.15}}

        result = ws.YesBestResult(
            city_group=group,
            yes_best_strike=best_strike,
            yes_best_idx=0,
            combined_score=0.65,
            model_prob=0.70,
            all_model_probs={"m_best": 0.70, "m_no_sk": 0.15},
        )
        ws_cfg = {
            "double_no_size_usdc": 3.0,
            "no_max_entry_price": 0.90,
            "right_tail_max_signals": 2,
            "right_tail_confidence_cap": 0.95,
        }

        with self.assertLogs("src.signal.weather_strategy", level="INFO"):
            signals = ws.build_right_tail_signals(result, ws_cfg)

        self.assertEqual(len(signals), 1)
        sig = signals[0]
        # T117: raw_p=0.15 → fusion_p=0.15*0.60+0.40*0.5=0.29
        # confidence = min(0.95, 1 - 0.29) = 0.71
        # With skill(5)=0.60: 0.71 * 0.60 ≈ 0.426
        self.assertAlmostEqual(sig.confidence, 0.426, places=2)

    def test_ambush_confidence_scaled_by_skill(self):
        """Ambush 信号 confidence 被 skill factor 缩放。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {
            "m_amb_sk": {"days_ahead": 4, "gfs_probability": 0.30}
        }

        strike = ws.TemperatureStrike(
            market_id="m_amb_sk",
            condition_id="0xa",
            token_yes="ty_a",
            token_no="tn_a",
            question="NYC high above 95°F?",
            threshold=95.0,
            threshold_low=95.0,
            threshold_high=95.0,
            direction="gte",
            best_ask_yes=0.03,
            volume_24h=10000,
        )
        group = ws.CityDayGroup(
            city_name="NYC",
            city_slug="nyc",
            date_str="2026-04-15",
            lat=40.71,
            lon=-74.01,
            icao="KJFK",
            timezone="America/New_York",
            strikes=[strike],
        )
        probs = {"m_amb_sk": 0.35}
        ws_cfg = {
            "ambush_max_ask": 0.04,
            "ambush_confidence_multiplier": 1.6,
            "ambush_confidence_min": 0.15,
            "ambush_confidence_cap": 0.95,
            "ambush_max_signals": 1,
            "ambush_size_usdc": 1.5,
            "ambush_max_undervaluation": 15.0,
        }

        with self.assertLogs("src.signal.weather_strategy", level="INFO"):
            signals = ws.build_yes_ambush_signals(group, probs, ws_cfg)

        self.assertEqual(len(signals), 1)
        sig = signals[0]
        # T117: raw_p=0.35 → fusion_p=0.35*0.72+0.28*0.5=0.252+0.14=0.392
        # confidence = max(0.15, min(0.95, 0.392)) = 0.392
        self.assertAlmostEqual(sig.confidence, 0.392, places=2)


class TestT116FormatFusionDetail(unittest.TestCase):
    """T116: _format_fusion_detail 统一格式化测试。"""

    def test_dual_model_full_detail(self):
        """完整双模型数据格式化。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {
            "m_fmt": {
                "gfs_probability": 0.55,
                "ecmwf_probability": 0.60,
                "divergence": 0.05,
                "confidence_level": "high",
                "metar_calibration": "METAR偏暖+0.3°C",
                "days_ahead": 2,
                "position_multiplier": 0.8,
                "source": "dual_model_gfs_ecmwf",
            }
        }

        result = ws._format_fusion_detail("m_fmt")
        self.assertIn("GFS=0.550", result)
        self.assertIn("ECMWF=0.600", result)
        self.assertIn("div=5.0%", result)
        self.assertIn("conf_level=high", result)
        self.assertIn("METAR:", result)
        self.assertIn("horizon=2d(skill=0.90)", result)
        self.assertIn("pos_mult=0.8", result)

    def test_gfs_only_fallback(self):
        """GFS-only 降级格式化。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {
            "m_gfs": {
                "_fallback_reason": "ecmwf_unavailable",
                "source": "ensemble",
            }
        }

        result = ws._format_fusion_detail("m_gfs")
        self.assertIn("fallback=ecmwf_unavailable", result)

    def test_empty_details_shows_source(self):
        """无详情时显示 source=unknown。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {}
        result = ws._format_fusion_detail("m_missing")
        self.assertEqual(result, "source=unknown")


class TestT116PipelineIndependence(unittest.TestCase):
    """T116: YES_best管线与Ambush管线完全独立验证。"""

    def test_ambush_does_not_read_yesbest_result(self):
        """build_yes_ambush_signals 不接收 YesBestResult 参数。"""
        import inspect

        import src.signal.weather_strategy as ws

        sig = inspect.signature(ws.build_yes_ambush_signals)
        param_names = list(sig.parameters.keys())
        self.assertNotIn("result", param_names)
        self.assertEqual(
            param_names, ["city_group", "all_probs", "ws_cfg", "hours_to_event"]
        )

    def test_double_no_does_not_share_ambush_data(self):
        """build_right_tail_signals 不接收 ambush 相关参数。"""
        import inspect

        import src.signal.weather_strategy as ws

        sig = inspect.signature(ws.build_right_tail_signals)
        param_names = list(sig.parameters.keys())
        self.assertEqual(param_names, ["result", "ws_cfg"])


# ════════════════════════════════════════════════════════════════════
# T117: 决策层深度融合测试
# ════════════════════════════════════════════════════════════════════


class TestT117DecisionFusion(unittest.TestCase):
    """T117: _compute_fusion_prob + EV/undervaluation 使用 fusion_p。"""

    def test_compute_fusion_prob_basic(self):
        """近期(0天): skill=0.98 → fusion_p 接近 raw_p。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {"m1": {"days_ahead": 0, "source": "dual_model"}}
        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            fp, src = ws._compute_fusion_prob("m1", 0.80)
        # 0.80 * 0.98 + 0.02 * 0.5 = 0.784 + 0.01 = 0.794
        self.assertAlmostEqual(fp, 0.794, places=3)

    def test_compute_fusion_prob_far_future(self):
        """远期(7天): skill=0.42 → fusion_p 向 0.5 大幅回归。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {"m2": {"days_ahead": 7, "source": "dual_model"}}
        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            fp, _ = ws._compute_fusion_prob("m2", 0.80)
        # 0.80 * 0.42 + 0.58 * 0.5 = 0.336 + 0.29 = 0.626
        self.assertAlmostEqual(fp, 0.626, places=3)

    def test_compute_fusion_prob_gfs_only_penalty(self):
        """GFS-only + pos_mult=0.8 → 额外向 0.5 衰减。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {
            "m3": {
                "days_ahead": 3,  # skill=0.82
                "position_multiplier": 0.8,
                "_fallback_reason": "divergence_skip",
            }
        }
        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            fp, src = ws._compute_fusion_prob("m3", 0.80)
        # Step 1: 0.80*0.82 + 0.18*0.5 = 0.656 + 0.09 = 0.746
        # Step 2: 0.746*0.8 + 0.2*0.5 = 0.5968 + 0.10 = 0.6968
        self.assertAlmostEqual(fp, 0.6968, places=3)
        self.assertIn("GFS-only", src)

    def test_compute_fusion_prob_source_tag_dual(self):
        """双模型成功返回 GFS+ECMWF+METAR source tag。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {
            "m4": {"days_ahead": 1, "ecmwf_probability": 0.75, "source": "dual_model"}
        }
        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            _, src = ws._compute_fusion_prob("m4", 0.80)
        self.assertEqual(src, "GFS+ECMWF+METAR")

    def test_compute_fusion_prob_no_detail(self):
        """detail 不存在时: skill=1.0, 无惩罚 → fusion_p=raw_p。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {}
        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            fp, src = ws._compute_fusion_prob("unknown", 0.75)
        self.assertAlmostEqual(fp, 0.75, places=3)
        self.assertEqual(src, "raw")

    def test_compute_fusion_prob_clamp(self):
        """极端值被 clamp 到 [0.01, 0.99]。"""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {"m5": {"days_ahead": 7, "position_multiplier": 0.5}}
        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            fp_low, _ = ws._compute_fusion_prob("m5", 0.01)
            fp_high, _ = ws._compute_fusion_prob("m5", 0.99)
        self.assertGreaterEqual(fp_low, 0.01)
        self.assertLessEqual(fp_high, 0.99)

    def test_yesbest_model_prob_is_fusion(self):
        """find_yes_best 返回的 model_prob 是 fusion_p 而非 raw_p。"""
        import src.signal.weather_strategy as ws

        strike = ws.TemperatureStrike(
            market_id="mfusion",
            condition_id="0x1",
            token_yes="ty1",
            token_no="tn1",
            question="NYC >=80°F?",
            threshold=80.0,
            threshold_low=80.0,
            threshold_high=80.0,
            direction="gte",
            best_ask_yes=0.60,
            volume_24h=10000,
        )
        group = ws.CityDayGroup(
            city_name="NYC",
            city_slug="nyc",
            date_str="2026-04-15",
            lat=40.71,
            lon=-74.01,
            icao="KJFK",
            timezone="America/New_York",
            strikes=[strike],
        )
        ws._DUAL_MODEL_DETAILS = {"mfusion": {"days_ahead": 5}}  # skill=0.60
        all_probs = {"mfusion": 0.80}
        ws_cfg = {}

        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            result = ws.find_yes_best(group, all_probs, ws_cfg)

        self.assertIsNotNone(result)
        # fusion_p = 0.80*0.60 + 0.40*0.5 = 0.48 + 0.20 = 0.68
        self.assertAlmostEqual(result.model_prob, 0.68, places=2)
        # model_prob should NOT be raw 0.80
        self.assertNotAlmostEqual(result.model_prob, 0.80, places=1)

    def test_double_no_ev_uses_fusion_p(self):
        """DoubleNO EV 使用 fusion_p 而非 raw model_p。"""
        import src.signal.weather_strategy as ws

        best_strike = ws.TemperatureStrike(
            market_id="m_yb",
            condition_id="0x1",
            token_yes="ty1",
            token_no="tn1",
            question="NYC >=80°F?",
            threshold=80.0,
            threshold_low=80.0,
            threshold_high=80.0,
            direction="gte",
            best_ask_yes=0.65,
            volume_24h=10000,
        )
        no_strike = ws.TemperatureStrike(
            market_id="m_no117",
            condition_id="0x2",
            token_yes="ty2",
            token_no="tn2",
            question="NYC >=75°F?",
            threshold=75.0,
            threshold_low=75.0,
            threshold_high=75.0,
            direction="gte",
            best_ask_yes=0.20,
            volume_24h=10000,
        )
        group = ws.CityDayGroup(
            city_name="NYC",
            city_slug="nyc",
            date_str="2026-04-15",
            lat=40.71,
            lon=-74.01,
            icao="KJFK",
            timezone="America/New_York",
            strikes=[best_strike, no_strike],
        )

        # 5天远期: skill=0.60, raw_p=0.15
        ws._DUAL_MODEL_DETAILS = {"m_no117": {"days_ahead": 5}}

        result = ws.YesBestResult(
            city_group=group,
            yes_best_strike=best_strike,
            yes_best_idx=0,
            combined_score=0.65,
            model_prob=0.70,
            all_model_probs={"m_yb": 0.70, "m_no117": 0.15},
        )
        ws_cfg = {
            "double_no_size_usdc": 3.0,
            "no_max_entry_price": 0.90,
            "right_tail_max_signals": 2,
            "right_tail_confidence_cap": 0.95,
        }

        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            with self.assertLogs("src.signal.weather_strategy", level="INFO"):
                signals = ws.build_right_tail_signals(result, ws_cfg)

        self.assertEqual(len(signals), 1)
        sig = signals[0]
        # fusion_p = 0.15*0.60 + 0.40*0.5 = 0.09 + 0.20 = 0.29
        # ev_no = ask_yes(0.20) - fusion_p(0.29) = -0.09
        self.assertAlmostEqual(sig.ev, 0.20 - 0.29, places=2)
        # With raw_p: ev_no would be 0.20 - 0.15 = 0.05 (different)
        self.assertNotAlmostEqual(sig.ev, 0.05, places=1)

    def test_ambush_undervaluation_uses_fusion_p(self):
        """Ambush undervaluation 使用 fusion_p 而非 raw model_p。"""
        import src.signal.weather_strategy as ws

        strike = ws.TemperatureStrike(
            market_id="m_amb117",
            condition_id="0xa",
            token_yes="ty_a",
            token_no="tn_a",
            question="NYC >=95°F?",
            threshold=95.0,
            threshold_low=95.0,
            threshold_high=95.0,
            direction="gte",
            best_ask_yes=0.03,
            volume_24h=10000,
        )
        group = ws.CityDayGroup(
            city_name="NYC",
            city_slug="nyc",
            date_str="2026-04-20",
            lat=40.71,
            lon=-74.01,
            icao="KJFK",
            timezone="America/New_York",
            strikes=[strike],
        )

        # 2天: skill=0.90
        ws._DUAL_MODEL_DETAILS = {"m_amb117": {"days_ahead": 2, "source": "dual_model"}}
        all_probs = {"m_amb117": 0.30}  # raw_p=0.30
        ws_cfg = {
            "ambush_max_ask": 0.05,
            "ambush_confidence_multiplier": 1.6,
            "ambush_confidence_min": 0.10,
            "ambush_confidence_cap": 0.95,
            "ambush_max_signals": 1,
            "ambush_size_usdc": 1.5,
            "ambush_max_undervaluation": 15.0,
        }

        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            with self.assertLogs("src.signal.weather_strategy", level="INFO"):
                signals = ws.build_yes_ambush_signals(group, all_probs, ws_cfg)

        self.assertEqual(len(signals), 1)
        sig = signals[0]
        # fusion_p = 0.30*0.90 + 0.10*0.5 = 0.27 + 0.05 = 0.32
        # ev_yes = 0.32 - 0.03 = 0.29
        self.assertAlmostEqual(sig.ev, 0.32 - 0.03, places=2)
        # undervaluation = 0.32 / 0.03 ≈ 10.67 (not 0.30/0.03=10.0)

    def test_ambush_far_future_reduces_undervaluation(self):
        """远期(7天) skill=0.42 → fusion_p 更小 → undervaluation 降低可能不过滤器。"""
        import src.signal.weather_strategy as ws

        strike = ws.TemperatureStrike(
            market_id="m_far",
            condition_id="0xb",
            token_yes="ty_b",
            token_no="tn_b",
            question="NYC >=95°F?",
            threshold=95.0,
            threshold_low=95.0,
            threshold_high=95.0,
            direction="gte",
            best_ask_yes=0.03,
            volume_24h=10000,
        )
        group = ws.CityDayGroup(
            city_name="NYC",
            city_slug="nyc",
            date_str="2026-04-22",
            lat=40.71,
            lon=-74.01,
            icao="KJFK",
            timezone="America/New_York",
            strikes=[strike],
        )

        # 7天: skill=0.42, raw_p=0.20
        ws._DUAL_MODEL_DETAILS = {"m_far": {"days_ahead": 7}}
        all_probs = {"m_far": 0.20}
        ws_cfg = {
            "ambush_max_ask": 0.05,
            "ambush_confidence_multiplier": 1.6,
            "ambush_confidence_min": 0.10,
            "ambush_confidence_cap": 0.95,
            "ambush_max_signals": 1,
            "ambush_size_usdc": 1.5,
            "ambush_max_undervaluation": 15.0,
        }

        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            signals = ws.build_yes_ambush_signals(group, all_probs, ws_cfg)

        # fusion_p = 0.20*0.42 + 0.58*0.5 = 0.084 + 0.29 = 0.374
        # 0.374 >= 0.03*1.6=0.048 ✓ BUT conf_min gate: fusion_p=0.374 >= 0.10 ✓
        # undervaluation = 0.374/0.03 ≈ 12.5 (vs raw 0.20/0.03=6.67)
        # 12.5 < 15.0 → should pass
        self.assertEqual(len(signals), 1)

    def test_gfs_only_penalty_reduces_ev(self):
        """GFS-only + pos_mult<1 → 额外衰减降低 EV."""
        import src.signal.weather_strategy as ws

        ws._DUAL_MODEL_DETAILS = {
            "m_gfs": {
                "days_ahead": 3,  # skill=0.82
                "position_multiplier": 0.8,
                "_fallback_reason": "ecmwf_unavailable",
            }
        }
        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            fp_penalty, src = ws._compute_fusion_prob("m_gfs", 0.80)

        ws._DUAL_MODEL_DETAILS = {
            "m_dual": {"days_ahead": 3, "ecmwf_probability": 0.78}
        }
        with mock.patch("src.signal.weather_strategy.get", return_value=None):
            fp_normal, _ = ws._compute_fusion_prob("m_dual", 0.80)

        # Penalized should be lower than normal
        self.assertLess(fp_penalty, fp_normal)
        self.assertIn("GFS-only", src)


if __name__ == "__main__":
    unittest.main()
