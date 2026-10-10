"""
test_weather_t278.py — T278 结算源对齐验证

测试覆盖：
  1. get_city_settlement_source — 从 config 正确读取 settlement_source
  2. fetch_noaa_observed_max_temp — NOAA METAR API 正确解析最高温
  3. fetch_hko_observed_max_temp — HKO Open Data API 正确解析温度
  4. fetch_daily_observed_max_temp — NOAA/HKO/WU 正确路由
  5. outcome_tracker._fetch_actual_max_temp_by_source — 结算用 source routing
  6. fetch_noaa_observed_max_temp — 空数据/异常 graceful fallback
  7. fetch_hko_observed_max_temp — HKO P1 降级到 METAR
"""

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# ══════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════

SAMPLE_METAR_RESPONSE = [
    {
        "icaoId": "LTFM",
        "reportTime": "2026-03-28T06:00:00Z",
        "temp": 12,
        "dewp": 5,
        "rawOb": "METAR LTFM 280600Z ...",
    },
    {
        "icaoId": "LTFM",
        "reportTime": "2026-03-28T09:00:00Z",
        "temp": 16,
        "dewp": 7,
    },
    {
        "icaoId": "LTFM",
        "reportTime": "2026-03-28T12:00:00Z",
        "temp": 19,
        "dewp": 8,
    },
    {
        "icaoId": "LTFM",
        "reportTime": "2026-03-28T15:00:00Z",
        "temp": 17,
        "dewp": 9,
    },
    {
        "icaoId": "LTFM",
        "reportTime": "2026-03-28T18:00:00Z",
        "temp": 14,
        "dewp": 6,
    },
]

SAMPLE_HKO_RESPONSE = {
    "temperature": {
        "data": [
            {"place": "Hong Kong Observatory", "value": 28.3, "unit": "C"},
            {"place": "King's Park", "value": 27.9, "unit": "C"},
            {"place": "Tai Po", "value": 29.1, "unit": "C"},
        ],
        "recordTime": "2026-03-28T14:00:00+08:00",
    }
}


def _mock_config_city(city_name):
    """模拟 _load_config_city 返回不同城市配置。"""
    configs = {
        "istanbul": {
            "name": "Istanbul",
            "slug": "istanbul",
            "icao": "LTFM",
            "lat": 41.2753,
            "lon": 28.7519,
            "settlement_source": "noaa",
            "settlement_url": "https://weather.gov/wrh/timeseries?site=LTFM",
            "noaa_site": "LTFM",
        },
        "hong kong": {
            "name": "Hong Kong",
            "slug": "hong-kong",
            "icao": "VHHH",
            "lat": 22.3080,
            "lon": 113.9185,
            "settlement_source": "hko",
            "settlement_url": "https://www.weather.gov.hk/en/cis/climat.htm",
            "settlement_precision": 1,
        },
        "new york city": {
            "name": "New York City",
            "slug": "new-york-city",
            "icao": "KLGA",
            "lat": 40.7128,
            "lon": -74.0060,
            "settlement_source": "wunderground",
            "settlement_url": "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
        },
        "moscow": {
            "name": "Moscow",
            "slug": "moscow",
            "icao": "UUWW",
            "lat": 55.5911,
            "lon": 37.2615,
            "settlement_source": "noaa",
            "noaa_site": "UUWW",
        },
    }
    key = city_name.lower().strip()
    return configs.get(key)


# ══════════════════════════════════════════════════════════════════════
# 1. get_city_settlement_source
# ══════════════════════════════════════════════════════════════════════


class TestGetCitySettlementSource:
    """测试 settlement source 配置读取。"""

    @patch("src.utils.weather_fetcher._load_config_city", side_effect=_mock_config_city)
    def test_noaa_city(self, mock_cfg):
        from src.utils.weather_fetcher import get_city_settlement_source

        result = get_city_settlement_source("Istanbul")
        assert result["settlement_source"] == "noaa"
        assert result["noaa_site"] == "LTFM"

    @patch("src.utils.weather_fetcher._load_config_city", side_effect=_mock_config_city)
    def test_hko_city(self, mock_cfg):
        from src.utils.weather_fetcher import get_city_settlement_source

        result = get_city_settlement_source("Hong Kong")
        assert result["settlement_source"] == "hko"
        assert result["settlement_precision"] == 1

    @patch("src.utils.weather_fetcher._load_config_city", side_effect=_mock_config_city)
    def test_wu_city(self, mock_cfg):
        from src.utils.weather_fetcher import get_city_settlement_source

        result = get_city_settlement_source("New York City")
        assert result["settlement_source"] == "wunderground"

    @patch("src.utils.weather_fetcher._load_config_city", return_value=None)
    def test_unknown_city_defaults_wu(self, mock_cfg):
        from src.utils.weather_fetcher import get_city_settlement_source

        result = get_city_settlement_source("UnknownCity123")
        assert result["settlement_source"] == "wunderground"


# ══════════════════════════════════════════════════════════════════════
# 2. fetch_noaa_observed_max_temp
# ══════════════════════════════════════════════════════════════════════


class TestFetchNoaaObservedMaxTemp:
    """测试 NOAA METAR 最高温度获取。"""

    def setup_method(self):
        """清除 NOAA 缓存。"""
        import src.utils.weather_fetcher as wf

        wf._NOAA_OBS_CACHE.clear()

    @patch("src.utils.weather_fetcher.requests.get")
    def test_normal_metar_response(self, mock_get):
        from src.utils.weather_fetcher import fetch_noaa_observed_max_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_METAR_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        temp, src = fetch_noaa_observed_max_temp("LTFM", "2026-03-28", "Istanbul")
        assert temp == 19.0  # max of [12, 16, 19, 17, 14]
        assert src == "noaa-metar"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_empty_metar_response(self, mock_get):
        from src.utils.weather_fetcher import fetch_noaa_observed_max_temp

        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        temp, src = fetch_noaa_observed_max_temp("LTFM", "2026-03-28", "Istanbul")
        assert temp is None
        assert src == ""

    @patch("src.utils.weather_fetcher.requests.get")
    def test_network_error_returns_none(self, mock_get):
        import requests as req
        from src.utils.weather_fetcher import fetch_noaa_observed_max_temp

        mock_get.side_effect = req.RequestException("Connection timeout")

        temp, src = fetch_noaa_observed_max_temp("RCTP", "2026-03-28", "Taipei")
        assert temp is None
        assert src == ""

    @patch("src.utils.weather_fetcher.requests.get")
    def test_cache_hit(self, mock_get):
        from src.utils.weather_fetcher import fetch_noaa_observed_max_temp

        mock_resp = MagicMock()
        mock_resp.json.return_value = SAMPLE_METAR_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        # First call — API hit
        t1, s1 = fetch_noaa_observed_max_temp("LTFM", "2026-03-28", "Istanbul")
        assert t1 == 19.0
        assert mock_get.call_count == 1

        # Second call — cache hit, no API call
        t2, s2 = fetch_noaa_observed_max_temp("LTFM", "2026-03-28", "Istanbul")
        assert t2 == 19.0
        assert mock_get.call_count == 1  # still 1

    @patch("src.utils.weather_fetcher.requests.get")
    def test_filters_by_target_date(self, mock_get):
        from src.utils.weather_fetcher import fetch_noaa_observed_max_temp

        mixed_dates = [
            {"reportTime": "2026-03-27T23:00:00Z", "temp": 25},  # wrong date
            {"reportTime": "2026-03-28T06:00:00Z", "temp": 15},  # correct
            {"reportTime": "2026-03-28T12:00:00Z", "temp": 20},  # correct
            {"reportTime": "2026-03-29T01:00:00Z", "temp": 30},  # wrong date
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = mixed_dates
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        temp, src = fetch_noaa_observed_max_temp("LLBG", "2026-03-28", "Tel Aviv")
        assert temp == 20.0  # only 15,20 from March 28
        assert src == "noaa-metar"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_moscow_uuww(self, mock_get):
        """T278: Moscow 使用 UUWW (Vnukovo) 而非 UUEE (Sheremetyevo)。"""
        from src.utils.weather_fetcher import fetch_noaa_observed_max_temp

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"reportTime": "2026-03-28T12:00:00Z", "temp": 5, "icaoId": "UUWW"},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        temp, _ = fetch_noaa_observed_max_temp("UUWW", "2026-03-28", "Moscow")
        assert temp == 5.0

        # 验证请求 URL 使用 UUWW
        call_args = mock_get.call_args
        assert call_args[1]["params"]["ids"] == "UUWW"


# ══════════════════════════════════════════════════════════════════════
# 3. fetch_hko_observed_max_temp
# ══════════════════════════════════════════════════════════════════════


class TestFetchHkoObservedMaxTemp:
    """测试 HKO 香港天文台温度获取。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._HKO_OBS_CACHE.clear()

    @patch("src.utils.weather_fetcher.requests.get")
    def test_hko_api_normal(self, mock_get):
        from src.utils.weather_fetcher import fetch_hko_observed_max_temp

        mock_resp = MagicMock()
        mock_resp.json.return_value = SAMPLE_HKO_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        temp, src = fetch_hko_observed_max_temp("2026-03-28")
        assert temp == 28.3  # Hong Kong Observatory station
        assert src == "hko"

    @patch("src.utils.weather_fetcher.fetch_noaa_observed_max_temp")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_hko_fallback_to_metar(self, mock_get, mock_noaa):
        """HKO API 失败时降级到 aviationweather VHHH METAR。"""
        import requests as req
        from src.utils.weather_fetcher import fetch_hko_observed_max_temp

        mock_get.side_effect = req.RequestException("HKO down")
        mock_noaa.return_value = (30.0, "noaa-metar")

        temp, src = fetch_hko_observed_max_temp("2026-03-28")
        assert temp == 30.0
        assert src == "hko-metar-fallback"
        mock_noaa.assert_called_once_with("VHHH", "2026-03-28", "Hong Kong")


# ══════════════════════════════════════════════════════════════════════
# 4. fetch_daily_observed_max_temp — source routing
# ══════════════════════════════════════════════════════════════════════


class TestSourceRouting:
    """测试 settlement_source 路由分发逻辑。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._NOAA_OBS_CACHE.clear()
        wf._HKO_OBS_CACHE.clear()

    @patch("src.utils.weather_fetcher.fetch_noaa_observed_max_temp")
    def test_noaa_route(self, mock_noaa):
        from src.utils.weather_fetcher import fetch_daily_observed_max_temp

        mock_noaa.return_value = (18.0, "noaa-metar")

        temp, src = fetch_daily_observed_max_temp(
            icao="LTFM",
            lat=41.27,
            lon=28.75,
            date_str="2026-03-28",
            city_name="Istanbul",
            settlement_source="noaa",
        )
        assert temp == 18.0
        assert src == "noaa-metar"
        mock_noaa.assert_called_once()

    @patch("src.utils.weather_fetcher.fetch_hko_observed_max_temp")
    def test_hko_route(self, mock_hko):
        from src.utils.weather_fetcher import fetch_daily_observed_max_temp

        mock_hko.return_value = (27.5, "hko")

        temp, src = fetch_daily_observed_max_temp(
            icao="VHHH",
            lat=22.31,
            lon=113.92,
            date_str="2026-03-28",
            city_name="Hong Kong",
            settlement_source="hko",
        )
        assert temp == 27.5
        assert src == "hko"
        mock_hko.assert_called_once()

    @patch("src.utils.weather_fetcher._fetch_wu_api_temp")
    def test_wu_route_default(self, mock_wu_api):
        """wunderground 路由走现有 WU P0 链。"""
        from src.utils.weather_fetcher import fetch_daily_observed_max_temp

        mock_wu_api.return_value = 25.0

        temp, src = fetch_daily_observed_max_temp(
            icao="KLGA",
            lat=40.71,
            lon=-74.01,
            date_str="2026-03-28",
            city_name="New York City",
            settlement_source="wunderground",
        )
        assert temp == 25.0
        assert src == "wu-api"

    @patch("src.utils.weather_fetcher._fallback_history_or_om")
    @patch("src.utils.weather_fetcher._fetch_wu_api_temp")
    @patch("src.utils.weather_fetcher.fetch_noaa_observed_max_temp")
    def test_noaa_failure_falls_to_wu(self, mock_noaa, mock_wu, mock_fallback):
        """NOAA 失败时降级到 WU 链。"""
        from src.utils.weather_fetcher import fetch_daily_observed_max_temp

        mock_noaa.return_value = (None, "")
        mock_wu.return_value = 15.0

        temp, src = fetch_daily_observed_max_temp(
            icao="LTFM",
            lat=41.27,
            lon=28.75,
            date_str="2026-03-28",
            city_name="Istanbul",
            settlement_source="noaa",
        )
        assert temp == 15.0
        assert src == "wu-api"

    @patch("src.utils.weather_fetcher.get_city_settlement_source")
    @patch("src.utils.weather_fetcher.fetch_noaa_observed_max_temp")
    def test_auto_detect_source_from_city_name(self, mock_noaa, mock_get_ss):
        """settlement_source 为空时自动从 config 检测。"""
        from src.utils.weather_fetcher import fetch_daily_observed_max_temp

        mock_get_ss.return_value = {
            "settlement_source": "noaa",
            "noaa_site": "UUWW",
            "settlement_url": "",
            "settlement_precision": 0,
        }
        mock_noaa.return_value = (3.0, "noaa-metar")

        temp, src = fetch_daily_observed_max_temp(
            icao="UUWW",
            lat=55.59,
            lon=37.26,
            date_str="2026-03-28",
            city_name="Moscow",
            settlement_source="",  # auto-detect
        )
        assert temp == 3.0
        assert src == "noaa-metar"
        # Called twice: once in auto-detect, once in NOAA branch for noaa_site
        assert mock_get_ss.call_count == 2


# ══════════════════════════════════════════════════════════════════════
# 5. OutcomeTracker._fetch_actual_max_temp_by_source
# ══════════════════════════════════════════════════════════════════════


class TestOutcomeTrackerSourceRouting:
    """测试 outcome_tracker 通过 settlement_source 获取结算温度。"""

    @patch("src.utils.weather_fetcher.get_city_settlement_source")
    @patch("src.utils.weather_fetcher.get_station_info")
    @patch("src.utils.weather_fetcher.fetch_daily_observed_max_temp")
    def test_noaa_city_routes_correctly(self, mock_fetch, mock_station, mock_ss):
        from src.simulation.outcome_tracker import OutcomeTracker

        mock_ss.return_value = {
            "settlement_source": "noaa",
            "noaa_site": "LTFM",
            "settlement_url": "",
            "settlement_precision": 0,
        }
        mock_station.return_value = ("LTFM", 41.27, 28.75, "Istanbul")
        mock_fetch.return_value = (19.0, "noaa-metar")

        temp, src = OutcomeTracker._fetch_actual_max_temp_by_source(
            "Istanbul", 41.27, 28.75, "2026-03-28"
        )
        assert temp == 19.0
        assert src == "noaa-metar"
        mock_fetch.assert_called_once()
        # 验证传入了正确的 settlement_source
        call_kwargs = mock_fetch.call_args[1]
        assert call_kwargs["settlement_source"] == "noaa"
        assert call_kwargs["city_name"] == "Istanbul"

    @patch("src.utils.weather_fetcher.get_city_settlement_source")
    @patch("src.utils.weather_fetcher.get_station_info")
    @patch("src.utils.weather_fetcher.fetch_daily_observed_max_temp")
    def test_hko_city_routes_correctly(self, mock_fetch, mock_station, mock_ss):
        from src.simulation.outcome_tracker import OutcomeTracker

        mock_ss.return_value = {
            "settlement_source": "hko",
            "noaa_site": "",
            "settlement_url": "",
            "settlement_precision": 1,
        }
        mock_station.return_value = ("VHHH", 22.31, 113.92, "Hong Kong")
        mock_fetch.return_value = (28.3, "hko")

        temp, src = OutcomeTracker._fetch_actual_max_temp_by_source(
            "Hong Kong", 22.31, 113.92, "2026-03-28"
        )
        assert temp == 28.3
        assert src == "hko"


# ══════════════════════════════════════════════════════════════════════
# 6. Edge cases
# ══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """边界场景和错误恢复测试。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._NOAA_OBS_CACHE.clear()
        wf._HKO_OBS_CACHE.clear()

    @patch("src.utils.weather_fetcher.requests.get")
    def test_noaa_null_temp_skipped(self, mock_get):
        """METAR 记录中 temp=null 的观测应被跳过。"""
        from src.utils.weather_fetcher import fetch_noaa_observed_max_temp

        data_with_nulls = [
            {"reportTime": "2026-03-28T06:00:00Z", "temp": None},
            {"reportTime": "2026-03-28T09:00:00Z", "temp": 15},
            {"reportTime": "2026-03-28T12:00:00Z", "temp": None},
            {"reportTime": "2026-03-28T15:00:00Z", "temp": 12},
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = data_with_nulls
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        temp, src = fetch_noaa_observed_max_temp("RCTP", "2026-03-28", "Taipei")
        assert temp == 15.0
        assert src == "noaa-metar"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_noaa_all_null_temps(self, mock_get):
        """所有温度为 null 时返回 None。"""
        from src.utils.weather_fetcher import fetch_noaa_observed_max_temp

        data = [
            {"reportTime": "2026-03-28T06:00:00Z", "temp": None},
            {"reportTime": "2026-03-28T09:00:00Z", "temp": None},
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = data
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        temp, _ = fetch_noaa_observed_max_temp("RCTP", "2026-03-28", "Taipei")
        assert temp is None

    @patch("src.utils.weather_fetcher.requests.get")
    def test_noaa_negative_temps(self, mock_get):
        """NOAA 应正确处理零下温度（如 Moscow 冬天）。"""
        from src.utils.weather_fetcher import fetch_noaa_observed_max_temp

        data = [
            {"reportTime": "2026-01-15T06:00:00Z", "temp": -12},
            {"reportTime": "2026-01-15T12:00:00Z", "temp": -8},
            {"reportTime": "2026-01-15T18:00:00Z", "temp": -15},
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = data
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        temp, src = fetch_noaa_observed_max_temp("UUWW", "2026-01-15", "Moscow")
        assert temp == -8.0  # max of [-12, -8, -15]
        assert src == "noaa-metar"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_hko_no_observatory_station_uses_max(self, mock_get):
        """HKO 响应中缺少 Observatory 站点时取所有站最高温。"""
        from src.utils.weather_fetcher import fetch_hko_observed_max_temp

        resp_no_obs = {
            "temperature": {
                "data": [
                    {"place": "King's Park", "value": 27.9, "unit": "C"},
                    {"place": "Tai Po", "value": 29.1, "unit": "C"},
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = resp_no_obs
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        temp, src = fetch_hko_observed_max_temp("2026-03-28")
        assert temp == 29.1  # max of all stations
        assert src == "hko"
