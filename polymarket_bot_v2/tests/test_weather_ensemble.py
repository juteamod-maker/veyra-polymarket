"""
test_weather_ensemble.py — GFS 31-member Ensemble 概率引擎测试

测试覆盖：
  1. 问题解析 (parse_weather_question) — 三种 Polymarket 问题格式
     A) exact: "highest temperature in CITY be NUMBER°C on DATE?"
     B) range: "highest temperature in CITY be between LOW-HIGH°F on DATE?"
     C) legacy: "high temp in CITY on DATE be NUMBER°F or higher/lower"
  2. 日期解析 (_parse_target_date) — 月份名+日期
  3. ICAO 站点映射 — 城市名 → 气象站精确坐标
  4. 概率计算 (fetch_ensemble_probability) — mock GFS API
  5. 边界条件 — 超出预报范围、坐标获取失败、成员数不足
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from src.utils.weather_fetcher import (
    _STATION_MAP,
    _apply_metar_calibration,
    _fetch_metar_aviationweather,
    _fetch_metar_metarcentral,
    _fetch_metar_observation,
    _parse_target_date,
    fetch_ensemble_probability,
    parse_weather_question,
)

# ══════════════════════════════════════════════════════════════════════
# 1. 问题解析测试 — 三种格式
# ══════════════════════════════════════════════════════════════════════


class TestParseWeatherQuestion:
    """Polymarket 天气问题 → 结构化参数。"""

    # ── Format A: exact "be NUMBER°C on DATE?" ──

    def test_exact_high_celsius(self):
        """实际 Polymarket 格式: 'highest temperature in Shanghai be 12°C on March 17?'"""
        q = "Will the highest temperature in Shanghai be 12°C on March 17?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["city"] == "Shanghai"
        assert result["date_str"] == "March 17"
        assert result["threshold_value"] == 12
        assert result["unit"] == "C"
        assert result["variable"] == "temp_max"
        assert result["direction"] == "exact"
        assert result["station_icao"] == "ZSPD"

    def test_exact_low_celsius(self):
        q = "Will the lowest temperature in London be 5°C on March 18?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["city"] == "London"
        assert result["threshold_value"] == 5
        assert result["variable"] == "temp_min"
        assert result["direction"] == "exact"
        assert result["station_icao"] == "EGLC"

    def test_exact_high_fahrenheit(self):
        q = "Will the highest temperature in Miami be 76°F on March 17?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["city"] == "Miami"
        assert result["threshold_value"] == 76
        assert result["unit"] == "F"
        assert result["direction"] == "exact"
        assert result["station_icao"] == "KMIA"

    def test_exact_unknown_city(self):
        """未知城市: station_icao = None 但仍能解析."""
        q = "Will the highest temperature in Timbuktu be 40°C on March 20?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["city"] == "Timbuktu"
        assert result["station_icao"] is None

    # ── Format B: range "between LOW-HIGH°F on DATE?" ──

    def test_range_high_fahrenheit(self):
        """实际 Polymarket 格式: 'between 50-51°F on March 17?'"""
        q = "Will the highest temperature in Seattle be between 50-51°F on March 17?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["city"] == "Seattle"
        assert result["date_str"] == "March 17"
        assert result["threshold_low"] == 50
        assert result["threshold_high"] == 51
        assert result["threshold_value"] == 50.5  # T30: range格式取中点值
        assert result["unit"] == "F"
        assert result["variable"] == "temp_max"
        assert result["direction"] == "range"
        assert result["station_icao"] == "KSEA"

    def test_range_high_celsius(self):
        q = "Will the highest temperature in Paris be between 15-16°C on April 1?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["threshold_low"] == 15
        assert result["threshold_high"] == 16
        assert result["direction"] == "range"
        assert result["station_icao"] == "LFPG"

    def test_range_low_fahrenheit(self):
        q = "Will the lowest temperature in Chicago be between 24-25°F on March 17?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["variable"] == "temp_min"
        assert result["direction"] == "range"
        assert result["threshold_low"] == 24
        assert result["threshold_high"] == 25
        assert result["station_icao"] == "KORD"

    def test_range_wide(self):
        q = "Will the highest temperature in Dallas be between 64-67°F on March 17?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["threshold_low"] == 64
        assert result["threshold_high"] == 67

    # ── Format C: legacy "or higher/lower" ──

    def test_legacy_gte_fahrenheit(self):
        q = "Will the high temperature in New York City on March 20 be 62°F or higher?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["city"] == "New York City"
        assert result["date_str"] == "March 20"
        assert result["threshold_value"] == 62
        assert result["direction"] == "gte"

    def test_legacy_lte_fahrenheit(self):
        q = "Will the low temperature in Chicago on March 21 be 28°F or lower?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["direction"] == "lte"
        assert result["variable"] == "temp_min"

    def test_or_warmer(self):
        q = "Will the high temperature in Los Angeles on April 1 be 80°F or warmer?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["direction"] == "gte"

    def test_or_colder(self):
        q = "Will the low temperature in Denver on April 2 be 25°F or colder?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["direction"] == "lte"

    # ── 新格式字段完整性 ──

    def test_new_fields_present_exact(self):
        """Exact format 返回完整字段 (threshold_low/high/station_icao)."""
        q = "Will the highest temperature in Tokyo be 20°C on March 20?"
        result = parse_weather_question(q)
        assert result is not None
        assert "threshold_low" in result
        assert "threshold_high" in result
        assert "station_icao" in result
        assert result["threshold_low"] is None
        assert result["threshold_high"] is None
        assert result["station_icao"] == "RJTT"

    def test_new_fields_present_legacy(self):
        """Legacy format 也返回 threshold_low/high/station_icao."""
        q = "Will the high temperature in Seoul on March 20 be 15°C or higher?"
        result = parse_weather_question(q)
        assert result is not None
        assert result["threshold_low"] is None
        assert result["threshold_high"] is None
        assert result["station_icao"] == "RKSI"

    # ── 非匹配 ──

    def test_unrecognized_question(self):
        q = "Will Bitcoin reach $100,000 by end of year?"
        assert parse_weather_question(q) is None

    def test_precipitation_not_matched(self):
        q = 'Will there be 0.1" or more of rain in Seattle on March 20?'
        assert parse_weather_question(q) is None


# ══════════════════════════════════════════════════════════════════════
# 2. ICAO 站点映射测试
# ══════════════════════════════════════════════════════════════════════


class TestStationMap:
    """验证 Polymarket 天气市场常用城市的 ICAO 站点映射。"""

    @pytest.mark.parametrize(
        "city,expected_icao",
        [
            ("shanghai", "ZSPD"),
            ("london", "EGLC"),
            ("nyc", "KLGA"),
            ("new york", "KLGA"),
            ("chicago", "KORD"),
            ("dallas", "KDAL"),
            ("miami", "KMIA"),
            ("seattle", "KSEA"),
            ("paris", "LFPG"),
            ("sao paulo", "SBGR"),
            ("seoul", "RKSI"),
            ("tokyo", "RJTT"),
            ("toronto", "CYYZ"),
            ("singapore", "WSSS"),
            ("ankara", "LTAC"),
            ("wellington", "NZWN"),
            ("tel aviv", "LLBG"),
            ("hong kong", "VHHH"),
            ("austin", "KAUS"),
            ("chongqing", "ZUCK"),
            ("houston", "KHOU"),
            ("shenzhen", "ZGSZ"),
        ],
    )
    def test_station_mapping(self, city, expected_icao):
        station = _STATION_MAP.get(city)
        assert station is not None, f"Missing station for '{city}'"
        assert station[0] == expected_icao

    def test_station_has_valid_coords(self):
        """All stations must have valid lat/lon coordinates."""
        for city, (icao, lat, lon, name) in _STATION_MAP.items():
            assert -90 <= lat <= 90, f"{city}/{icao}: invalid lat {lat}"
            assert -180 <= lon <= 180, f"{city}/{icao}: invalid lon {lon}"
            assert len(icao) == 4, f"{city}: ICAO code must be 4 chars, got {icao}"


# ══════════════════════════════════════════════════════════════════════
# 3. 日期解析测试
# ══════════════════════════════════════════════════════════════════════


class TestParseDateStr:
    def test_month_day(self):
        result = _parse_target_date("March 20")
        assert result is not None
        assert result.endswith("-03-20")

    def test_month_day_year(self):
        result = _parse_target_date("March 20, 2026")
        assert result == "2026-03-20"

    def test_abbreviated_month(self):
        result = _parse_target_date("Mar 20")
        assert result is not None
        assert "-03-20" in result

    def test_invalid_date(self):
        assert _parse_target_date("Smarch 32") is None

    def test_february_29_leap(self):
        assert _parse_target_date("February 29, 2028") == "2028-02-29"


# ══════════════════════════════════════════════════════════════════════
# 4. Ensemble 概率计算测试 (mocked API)
# ══════════════════════════════════════════════════════════════════════


def _make_ensemble_response(date: str, values: list[float], variable: str = "temp_max"):
    """Build a mock Open-Meteo Ensemble API response."""
    daily_var = "temperature_2m_max" if variable == "temp_max" else "temperature_2m_min"
    daily = {"time": [date]}
    for i, val in enumerate(values):
        daily[f"{daily_var}_member{i + 1:02d}"] = [val]
    return {"daily": daily}


def _mock_ens_config(key, *a, **kw):
    return {
        "weather_ensemble": {
            "enabled": True,
            "model": "gfs_seamless",
            "max_forecast_days": 7,
            "min_members_for_confidence": 20,
            "api_timeout": 15,
        },
    }.get(key, None)


class TestFetchEnsembleProbability:
    """Ensemble probability with mocked API responses."""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0  # 重置熔断器
        wf._ADAPTIVE_INTERVAL = 0.01

    @patch("src.utils.weather_fetcher.get", side_effect=_mock_ens_config)
    @patch("src.utils.weather_fetcher._get_city_coords")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_exact_temp_probability(self, mock_requests, mock_coords, mock_get):
        """Exact match: P(round(temp) == 12) via ±0.5 window."""
        mock_coords.return_value = (31.14, 121.81)  # ZSPD

        today = datetime.now(timezone.utc).date()
        target = today.strftime("%Y-%m-%d")
        # 12°C: [11.5, 12.5). 20 in window, 11 outside.
        values_c = [12.1] * 15 + [11.8] * 5 + [13.5] * 6 + [10.0] * 5

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = _make_ensemble_response(target, values_c)
        mock_requests.return_value = mock_resp

        from src.utils.weather_fetcher import _CACHE

        _CACHE.clear()

        market = {
            "question": f"Will the highest temperature in Shanghai be 12°C on {today.strftime('%B %-d')}?",
        }
        result = fetch_ensemble_probability(market)
        assert result is not None
        assert result["probability"] == pytest.approx(20 / 31, abs=0.01)
        assert result["direction"] == "exact"
        assert (
            result["source"] == "ensemble"
        )  # T273: source_type, not hardcoded "gfs_ensemble"
        assert "ZSPD" in result["station_icao"]

    @patch("src.utils.weather_fetcher.get", side_effect=_mock_ens_config)
    @patch("src.utils.weather_fetcher._get_city_coords")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_range_temp_probability(self, mock_requests, mock_coords, mock_get):
        """Range match: P(50 ≤ round(temp) ≤ 51) via window."""
        mock_coords.return_value = (47.45, -122.31)  # KSEA

        today = datetime.now(timezone.utc).date()
        target = today.strftime("%Y-%m-%d")
        # 50°F=10.0°C, 51°F=10.56°C → window [9.72, 11.06)
        values_c = [10.2] * 18 + [10.8] * 3 + [8.0] * 5 + [15.0] * 5

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = _make_ensemble_response(target, values_c)
        mock_requests.return_value = mock_resp

        from src.utils.weather_fetcher import _CACHE

        _CACHE.clear()

        market = {
            "question": f"Will the highest temperature in Seattle be between 50-51°F on {today.strftime('%B %-d')}?",
        }
        result = fetch_ensemble_probability(market)
        assert result is not None
        assert result["direction"] == "range"
        assert result["members_yes"] == 21  # 18 + 3 in window
        assert result["members_total"] == 31

    @patch("src.utils.weather_fetcher.get", side_effect=_mock_ens_config)
    @patch("src.utils.weather_fetcher._get_city_coords")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_legacy_gte_majority_yes(self, mock_requests, mock_coords, mock_get):
        """Legacy format: 24/31 above threshold → P(YES) ≈ 0.774."""
        mock_coords.return_value = (40.71, -74.01)

        today = datetime.now(timezone.utc).date()
        target = today.strftime("%Y-%m-%d")
        values_c = [17.0] * 24 + [15.0] * 7  # 62°F ≈ 16.67°C

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = _make_ensemble_response(target, values_c)
        mock_requests.return_value = mock_resp

        from src.utils.weather_fetcher import _CACHE

        _CACHE.clear()

        market = {
            "question": f"Will the high temperature in New York City on {today.strftime('%B %-d')} be 62°F or higher?",
        }
        result = fetch_ensemble_probability(market)
        assert result is not None
        assert result["probability"] == pytest.approx(24 / 31, abs=0.01)
        assert result["direction"] == "gte"
        assert result["confidence"] > 0.5

    @patch("src.utils.weather_fetcher.get", side_effect=_mock_ens_config)
    @patch("src.utils.weather_fetcher._get_city_coords")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_legacy_lte_all_yes(self, mock_requests, mock_coords, mock_get):
        """All 31 below threshold → P(YES) = 1.0."""
        mock_coords.return_value = (41.88, -87.63)

        today = datetime.now(timezone.utc).date()
        target = today.strftime("%Y-%m-%d")
        values_c = [-5.0] * 31  # All below -2.2°C (28°F)

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = _make_ensemble_response(
            target, values_c, "temp_min"
        )
        mock_requests.return_value = mock_resp

        from src.utils.weather_fetcher import _CACHE

        _CACHE.clear()

        market = {
            "question": f"Will the low temperature in Chicago on {today.strftime('%B %-d')} be 28°F or lower?",
        }
        result = fetch_ensemble_probability(market)
        assert result is not None
        assert result["probability"] == 1.0

    @patch("src.utils.weather_fetcher.get")
    def test_disabled_in_config(self, mock_get):
        mock_get.side_effect = lambda key, *a, **kw: {
            "weather_ensemble": {"enabled": False},
        }.get(key, None)
        market = {
            "question": "Will the highest temperature in Shanghai be 12°C on March 20?"
        }
        assert fetch_ensemble_probability(market) is None

    @patch("src.utils.weather_fetcher.get")
    def test_unparseable_question(self, mock_get):
        mock_get.side_effect = lambda key, *a, **kw: {
            "weather_ensemble": {"enabled": True, "max_forecast_days": 7},
        }.get(key, None)
        market = {"question": "Will it rain in Seattle tomorrow?"}
        assert fetch_ensemble_probability(market) is None

    @patch("src.utils.weather_fetcher.get")
    def test_date_too_far_out(self, mock_get):
        mock_get.side_effect = lambda key, *a, **kw: {
            "weather_ensemble": {
                "enabled": True,
                "max_forecast_days": 7,
                "model": "gfs_seamless",
            },
        }.get(key, None)
        far_date = (datetime.now(timezone.utc) + timedelta(days=30)).strftime(
            "%B %-d, %Y"
        )
        market = {
            "question": f"Will the highest temperature in Shanghai be 12°C on {far_date}?"
        }
        assert fetch_ensemble_probability(market) is None

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher._get_city_coords")
    def test_coords_not_found(self, mock_coords, mock_get):
        mock_get.side_effect = lambda key, *a, **kw: {
            "weather_ensemble": {
                "enabled": True,
                "max_forecast_days": 7,
                "model": "gfs_seamless",
            },
        }.get(key, None)
        mock_coords.return_value = None
        today = datetime.now(timezone.utc).date()
        market = {
            "question": f"Will the highest temperature in Atlantis be 20°C on {today.strftime('%B %-d')}?",
        }
        assert fetch_ensemble_probability(market) is None


# ══════════════════════════════════════════════════════════════════════
# 5. 单位转换测试
# ══════════════════════════════════════════════════════════════════════


class TestUnitConversion:
    def test_fahrenheit_to_celsius_threshold(self):
        q = "Will the high temperature in NYC on March 20 be 62°F or higher?"
        parsed = parse_weather_question(q)
        assert parsed is not None
        threshold_c = (parsed["threshold_value"] - 32) * 5 / 9
        assert 16.6 < threshold_c < 16.7

    def test_celsius_no_conversion(self):
        q = "Will the highest temperature in Tokyo be 20°C on March 20?"
        parsed = parse_weather_question(q)
        assert parsed is not None
        assert parsed["threshold_value"] == 20
        assert parsed["unit"] == "C"


# ══════════════════════════════════════════════════════════════════════
# 6. Config 集成测试
# ══════════════════════════════════════════════════════════════════════


class TestWeatherEnsembleConfig:
    def test_config_has_ensemble_section(self):
        import json
        from pathlib import Path

        config_path = Path(__file__).parent.parent / "config" / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not found")
        with open(config_path) as f:
            cfg = json.load(f)
        ens = cfg.get("weather_ensemble")
        assert ens is not None, "config.json missing weather_ensemble section"
        assert "enabled" in ens
        assert "model" in ens
        assert "max_forecast_days" in ens
        assert ens["model"] == "gfs_seamless"
        assert ens["max_forecast_days"] <= 16

    def test_config_dry_run_still_true(self):
        import json
        from pathlib import Path

        config_path = Path(__file__).parent.parent / "config" / "config.json"
        if not config_path.exists():
            pytest.skip("config.json not found")
        with open(config_path) as f:
            cfg = json.load(f)
        assert cfg["general"]["dry_run"] is True, "dry_run MUST be true!"


# ══════════════════════════════════════════════════════════════════════
# 7. METAR 实时观测数据源测试（Task 106）
# ══════════════════════════════════════════════════════════════════════


class TestFetchMetarAviationweather:
    """aviationweather.gov JSON API 数据解析。"""

    def test_parse_valid_response(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "temp": 20.6,
                "dewp": 18.0,
                "reportTime": "2026-03-24T04:00:00Z",
                "rawOb": "METAR KHOU 240400Z 16009KT 10SM SCT005 21/18 A3010",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        with patch("src.utils.weather_fetcher.requests.get", return_value=mock_resp):
            result = _fetch_metar_aviationweather("KHOU", timeout=10)
        assert result is not None
        assert result["temp_c"] == 20.6
        assert result["dewpoint_c"] == 18.0
        assert result["source"] == "aviationweather.gov"
        assert "KHOU" in result["raw"]

    def test_empty_response_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        with patch("src.utils.weather_fetcher.requests.get", return_value=mock_resp):
            assert _fetch_metar_aviationweather("XXXX", timeout=5) is None

    def test_missing_temp_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"dewp": 10, "rawOb": "METAR XXXX"}]
        mock_resp.raise_for_status = MagicMock()
        with patch("src.utils.weather_fetcher.requests.get", return_value=mock_resp):
            assert _fetch_metar_aviationweather("XXXX", timeout=5) is None

    def test_network_error_returns_none(self):
        import requests as req

        with patch(
            "src.utils.weather_fetcher.requests.get",
            side_effect=req.ConnectionError("timeout"),
        ):
            assert _fetch_metar_aviationweather("KHOU", timeout=5) is None


class TestFetchMetarMetarcentral:
    """metarcentral.com HTML 降级解析。"""

    SAMPLE_HTML = """
    <h2>William P Hobby Airport</h2>
    <pre>METAR KHOU 240353Z 16009KT 10SM SCT005 21/20 A3010 RMK AO2 SLP197 T02060200</pre>
    """

    def test_parse_html_with_t_group(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = self.SAMPLE_HTML
        mock_resp.raise_for_status = MagicMock()
        with patch("src.utils.weather_fetcher.requests.get", return_value=mock_resp):
            result = _fetch_metar_metarcentral("KHOU", timeout=10)
        assert result is not None
        assert result["temp_c"] == 20.6  # T0206 → 20.6°C
        assert result["dewpoint_c"] == 20.0
        assert result["source"] == "metarcentral.com"

    def test_parse_html_negative_temp(self):
        html = (
            "<pre>METAR KLGA 240451Z 33012G20KT 10SM FEW250 M02/M07 A3032 RMK AO2</pre>"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()
        with patch("src.utils.weather_fetcher.requests.get", return_value=mock_resp):
            result = _fetch_metar_metarcentral("KLGA", timeout=10)
        assert result is not None
        assert result["temp_c"] == -2.0
        assert result["dewpoint_c"] == -7.0

    def test_no_metar_in_html_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>No data</body></html>"
        mock_resp.raise_for_status = MagicMock()
        with patch("src.utils.weather_fetcher.requests.get", return_value=mock_resp):
            assert _fetch_metar_metarcentral("XXXX", timeout=5) is None


class TestFetchMetarObservation:
    """综合 METAR 获取：主路径 + 降级 + 缓存。"""

    @patch("src.utils.weather_fetcher.get")
    def test_disabled_returns_none(self, mock_get):
        mock_get.return_value = {"metar_enabled": False}
        assert _fetch_metar_observation("KHOU") is None

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher._fetch_metar_aviationweather")
    @patch("src.utils.weather_fetcher._fetch_nws_raw_metar")
    def test_primary_source_success(self, mock_nws, mock_avwx, mock_get):
        mock_get.return_value = {"metar_enabled": True, "metar_cache_ttl_seconds": 1800}
        mock_avwx.return_value = {
            "temp_c": 25.0,
            "dewpoint_c": 20.0,
            "obs_time": "2026-03-24T12:00Z",
            "raw": "METAR KHOU ...",
            "source": "aviationweather.gov",
        }
        mock_nws.return_value = None
        result = _fetch_metar_observation("KHOU")
        assert result is not None
        assert result["temp_c"] == 25.0
        assert result["source"] == "aviationweather.gov"
        mock_avwx.assert_called_once()

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher._fetch_metar_aviationweather", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_metar_metarcentral")
    @patch("src.utils.weather_fetcher._fetch_nws_raw_metar")
    def test_fallback_to_metarcentral(self, mock_nws, mock_mc, mock_avwx, mock_get):
        mock_get.return_value = {"metar_enabled": True, "metar_cache_ttl_seconds": 1800}
        mock_mc.return_value = {
            "temp_c": 24.0,
            "dewpoint_c": 19.0,
            "obs_time": "240400Z",
            "raw": "METAR KLGA 240400Z ...",
            "source": "metarcentral.com",
        }
        mock_nws.return_value = None
        # Use different ICAO to avoid cache hit from prior test
        from src.utils.weather_fetcher import _CACHE

        _CACHE.pop("metar:KLGA", None)
        result = _fetch_metar_observation("KLGA")
        assert result is not None
        assert result["source"] == "metarcentral.com"

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher._fetch_metar_aviationweather", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_metar_metarcentral", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_nws_raw_metar", return_value=None)
    def test_both_fail_returns_none(self, mock_nws, mock_mc, mock_avwx, mock_get):
        mock_get.return_value = {"metar_enabled": True, "metar_cache_ttl_seconds": 1800}
        assert _fetch_metar_observation("XXXX") is None

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher._fetch_metar_aviationweather", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_metar_metarcentral", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_nws_raw_metar")
    def test_fallback_to_nws_raw(self, mock_nws, mock_mc, mock_avwx, mock_get):
        mock_get.return_value = {"metar_enabled": True, "metar_cache_ttl_seconds": 1800}
        mock_nws.return_value = {
            "temp_c": 18.2,
            "dewpoint_c": 10.0,
            "obs_time": "2026/04/16 09:00",
            "raw": "RJTT 160900Z 02005KT 10SM FEW030 18/10 A3002",
            "source": "tgftp.nws.noaa.gov",
        }
        result = _fetch_metar_observation("RJTT")
        assert result is not None
        assert result["source"] == "tgftp.nws.noaa.gov"


class TestApplyMetarCalibration:
    """METAR 实时观测概率校准逻辑。"""

    METAR_DATA = {"temp_c": 35.0, "dewpoint_c": 20.0, "obs_time": "240400Z"}

    PARSED_MAX = {
        "variable": "temp_max",
        "direction": "gte",
        "threshold_value": 30,
        "unit": "C",
    }
    PARSED_MIN = {
        "variable": "temp_min",
        "direction": "lte",
        "threshold_value": 5,
        "unit": "C",
    }

    @patch("src.utils.weather_fetcher.get")
    def test_same_day_already_exceeded_boosts_prob(self, mock_get):
        """当日 METAR 已超阈值 → P(YES) 上调。"""
        mock_get.return_value = {
            "metar_same_day_weight": 0.15,
            "metar_bias_correction_max": 2.0,
        }
        prob, note = _apply_metar_calibration(
            0.70, self.METAR_DATA, None, self.PARSED_MAX, "2026-03-24", days_ahead=0
        )
        assert prob > 0.70  # probability increased
        assert "已超阈值" in note

    @patch("src.utils.weather_fetcher.get")
    def test_same_day_far_below_reduces_prob(self, mock_get):
        """当日 METAR 远低于阈值 → P(YES) 下调。"""
        mock_get.return_value = {
            "metar_same_day_weight": 0.15,
            "metar_bias_correction_max": 2.0,
        }
        cold_metar = {"temp_c": 20.0, "dewpoint_c": 15.0, "obs_time": "240400Z"}
        prob, note = _apply_metar_calibration(
            0.50, cold_metar, None, self.PARSED_MAX, "2026-03-24", days_ahead=0
        )
        assert prob < 0.50  # probability decreased
        assert "低于阈值" in note

    @patch("src.utils.weather_fetcher.get")
    def test_min_temp_already_below_boosts(self, mock_get):
        """当日 min temp 已低于阈值 → P(YES) 上调。"""
        mock_get.return_value = {
            "metar_same_day_weight": 0.15,
            "metar_bias_correction_max": 2.0,
        }
        cold_metar = {"temp_c": 3.0, "dewpoint_c": 1.0, "obs_time": "240400Z"}
        prob, note = _apply_metar_calibration(
            0.60, cold_metar, None, self.PARSED_MIN, "2026-03-24", days_ahead=0
        )
        assert prob > 0.60
        assert "已低于阈值" in note

    @patch("src.utils.weather_fetcher.get")
    def test_near_term_bias_correction(self, mock_get):
        """近期事件（days_ahead=1）：GFS 偏冷 → P(YES) 上调。"""
        _cfg = {
            "metar_same_day_weight": 0.15,
            "metar_bias_correction_max": 2.0,
            "metar_bias_trigger_c": 0.5,
            "metar_bias_rate_per_c": 0.02,
            "metar_bias_max_shift": 0.05,
        }
        mock_get.side_effect = lambda key, *a, **kw: (
            _cfg.get(key.split(".")[-1], _cfg) if "." in key else _cfg
        )
        gfs_data = {datetime.now(timezone.utc).strftime("%Y-%m-%d"): [32.0, 33.0, 31.0]}
        # METAR shows 35°C but GFS predicted 32°C → model is cold-biased by +3°C (capped to 2°C)
        prob, note = _apply_metar_calibration(
            0.60, self.METAR_DATA, gfs_data, self.PARSED_MAX, "2026-03-25", days_ahead=1
        )
        assert prob > 0.60  # cold bias → higher prob for max temp
        assert "偏差校准" in note

    @patch("src.utils.weather_fetcher.get")
    def test_no_gfs_data_no_correction(self, mock_get):
        """days_ahead=1 但无 GFS 数据 → 不校准。"""
        mock_get.return_value = {
            "metar_same_day_weight": 0.15,
            "metar_bias_correction_max": 2.0,
        }
        prob, note = _apply_metar_calibration(
            0.60, self.METAR_DATA, {}, self.PARSED_MAX, "2026-03-25", days_ahead=1
        )
        assert prob == 0.60
        assert note == ""

    @patch("src.utils.weather_fetcher.get")
    def test_days_ahead_3_no_calibration(self, mock_get):
        """days_ahead=3 → 超出校准范围，不调整。"""
        mock_get.return_value = {
            "metar_same_day_weight": 0.15,
            "metar_bias_correction_max": 2.0,
        }
        prob, note = _apply_metar_calibration(
            0.60, self.METAR_DATA, None, self.PARSED_MAX, "2026-03-27", days_ahead=3
        )
        assert prob == 0.60
        assert note == ""

    @patch("src.utils.weather_fetcher.get")
    def test_prob_clamped_to_bounds(self, mock_get):
        """概率不会超出 [0.02, 0.98] 范围。"""
        mock_get.return_value = {
            "metar_same_day_weight": 0.99,
            "metar_bias_correction_max": 2.0,
        }
        prob, _ = _apply_metar_calibration(
            0.97, self.METAR_DATA, None, self.PARSED_MAX, "2026-03-24", days_ahead=0
        )
        assert prob <= 0.98


# ══════════════════════════════════════════════════════════════════════
# T265: Open-Meteo API 速率限制器测试
# ══════════════════════════════════════════════════════════════════════


class TestEnsembleApiThrottle:
    """T265+T267: 测试自适应速率限制器。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._LAST_ENSEMBLE_API_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 0.0  # 重置自适应间隔
        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0  # 重置熔断器

    @patch("src.utils.weather_fetcher.get")
    def test_throttle_sleeps_when_too_fast(self, mock_get):
        """连续请求应触发 sleep 等待。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {"api_request_interval_sec": 1.0}
        wf._ADAPTIVE_INTERVAL = 1.0

        with patch("src.utils.weather_fetcher.time") as mock_time:
            # 第一次调用: _LAST=0, now=100 → 无需等待
            mock_time.time.side_effect = [100.0, 100.0]
            mock_time.sleep = MagicMock()
            wf._LAST_ENSEMBLE_API_TS = 0.0
            wf._throttle_ensemble_api()
            mock_time.sleep.assert_not_called()

            # 第二次调用: _LAST=100, now=100.3 → 需要等待 0.7s
            wf._LAST_ENSEMBLE_API_TS = 100.0
            mock_time.time.side_effect = [100.3, 100.3]
            wf._throttle_ensemble_api()
            mock_time.sleep.assert_called_once()
            sleep_val = mock_time.sleep.call_args[0][0]
            assert 0.6 < sleep_val < 0.8  # ~0.7s

    @patch("src.utils.weather_fetcher.get")
    def test_throttle_no_sleep_after_interval(self, mock_get):
        """间隔足够长时不应 sleep。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {"api_request_interval_sec": 1.0}
        wf._ADAPTIVE_INTERVAL = 1.0

        with patch("src.utils.weather_fetcher.time") as mock_time:
            wf._LAST_ENSEMBLE_API_TS = 100.0
            mock_time.time.side_effect = [102.0, 102.0]
            mock_time.sleep = MagicMock()
            wf._throttle_ensemble_api()
            mock_time.sleep.assert_not_called()

    @patch("src.utils.weather_fetcher.get")
    def test_throttle_uses_config_interval(self, mock_get):
        """应使用配置中的间隔值。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {"api_request_interval_sec": 3.0}
        wf._ADAPTIVE_INTERVAL = 3.0

        with patch("src.utils.weather_fetcher.time") as mock_time:
            wf._LAST_ENSEMBLE_API_TS = 100.0
            mock_time.time.side_effect = [101.5, 101.5]
            mock_time.sleep = MagicMock()
            wf._throttle_ensemble_api()
            mock_time.sleep.assert_called_once()
            sleep_val = mock_time.sleep.call_args[0][0]
            assert 1.3 < sleep_val < 1.7  # ~1.5s (need to wait 3.0 - 1.5)

    @patch("src.utils.weather_fetcher.get")
    def test_throttle_default_interval_is_3s(self, mock_get):
        """无配置时使用默认 3.0s 间隔。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {}  # 无 api_request_interval_sec
        wf._ADAPTIVE_INTERVAL = 0.0  # 强制重新初始化

        interval = wf._get_adaptive_interval()
        assert interval == 3.0


class TestGfs429Handling:
    """T265+T267: 测试 GFS fetch 的 429 限速处理 + 自适应 + 过期缓存兜底。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._LAST_ENSEMBLE_API_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 0.01  # 测试用极小间隔
        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0  # 重置熔断器
        wf._OPEN_METEO_LAST_429_TS = 0.0  # T275: 重置IP级429时间戳
        wf._CACHE.clear()

    def _mock_gfs_config(self):
        return {
            "model": "gfs_seamless",
            "api_timeout": 5,
            "api_request_interval_sec": 0.01,
            "min_members_for_confidence": 20,
            "cache_stale_ttl_seconds": 7200,
            "gfs_cache_ttl_seconds": 3600,
            "circuit_breaker_cooldown_sec": 600,
        }

    @patch("src.utils.weather_fetcher._fetch_ecmwf_cloud_mirror", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_gfs_429_breaks_immediately(
        self, mock_req_get, mock_cfg_get, mock_det_fb, mock_aws, mock_ecmwf_m
    ):
        """GFS API 429 应立即开启熔断器并停止重试; T275: 降级链最终emergency兜底。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        mock_cfg_get.return_value = self._mock_gfs_config()

        # 模拟 429 响应
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.raise_for_status.side_effect = __import__(
            "requests"
        ).exceptions.HTTPError(response=resp_429)

        mock_req_get.return_value = resp_429

        result = _fetch_gfs_ensemble(40.0, -74.0, "temp_max", 7)
        # T275: 降级链最终 emergency climate 兜底, never None
        assert result is not None
        # 确定性降级被调用(T274 _gfs_multi_fallback → T6 det_fallback)
        mock_det_fb.assert_called_once()
        # 熔断器应已开启
        assert wf._GFS_CIRCUIT_OPEN_UNTIL > time.time()
        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_gfs_succeeds_after_circuit_expires(self, mock_req_get, mock_cfg_get):
        """熔断器过期后, GFS API 可正常获取数据。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        mock_cfg_get.return_value = self._mock_gfs_config()

        # 设置已过期的熔断器
        wf._GFS_CIRCUIT_OPEN_UNTIL = time.time() - 1

        daily_data = {
            "time": ["2026-03-25"],
        }
        for i in range(1, 32):
            daily_data[f"temperature_2m_max_member{i:02d}"] = [20.0 + i * 0.1]

        resp_ok = MagicMock()
        resp_ok.raise_for_status = MagicMock()
        resp_ok.json.return_value = {"daily": daily_data}

        mock_req_get.return_value = resp_ok

        result = _fetch_gfs_ensemble(40.0, -74.0, "temp_max", 7)
        assert result is not None
        assert "2026-03-25" in result
        assert len(result["2026-03-25"]) == 31
        # 熔断器应已关闭 (成功时 _on_api_success)
        assert wf._GFS_CIRCUIT_OPEN_UNTIL == 0.0

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_gfs_429_uses_stale_cache_fallback(self, mock_req_get, mock_cfg_get):
        """GFS API 全部 429 失败时，应使用过期缓存兜底。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        mock_cfg_get.return_value = self._mock_gfs_config()

        # 预置一条 20 分钟前的过期缓存 (TTL=10min, 但 stale_ttl=2h)
        cache_key = "ens:40.00:-74.00:temp_max:7"
        old_data = {"2026-03-25": [20.0 + i * 0.1 for i in range(31)]}
        wf._CACHE[cache_key] = (time.time() - 1200, old_data)  # 20分钟前

        # 模拟全部 429
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.raise_for_status.side_effect = __import__(
            "requests"
        ).exceptions.HTTPError(response=resp_429)
        mock_req_get.return_value = resp_429

        result = _fetch_gfs_ensemble(40.0, -74.0, "temp_max", 7)
        # 应返回过期缓存而非 None
        assert result is not None
        assert result == old_data

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_gfs_429_no_stale_cache_returns_emergency(self, mock_req_get, mock_cfg_get):
        """GFS API 全部 429 且无任何缓存时, T275: emergency climate 兜底(never None)。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        mock_cfg_get.return_value = self._mock_gfs_config()

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.raise_for_status.side_effect = __import__(
            "requests"
        ).exceptions.HTTPError(response=resp_429)
        mock_req_get.return_value = resp_429

        result = _fetch_gfs_ensemble(40.0, -74.0, "temp_max", 7)
        # T275: emergency climate 最终兜底, never None
        assert result is not None


class TestAdaptiveThrottle:
    """T267: 测试自适应间隔 — 429 时加倍, 成功时恢复。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._ADAPTIVE_INTERVAL = 0.0
        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0  # 重置熔断器

    @patch("src.utils.weather_fetcher.get")
    def test_429_doubles_interval(self, mock_get):
        """429 响应应将自适应间隔加倍。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {"api_request_interval_sec": 3.0}
        wf._ADAPTIVE_INTERVAL = 3.0

        wf._on_api_429()
        assert wf._ADAPTIVE_INTERVAL == 6.0

        wf._on_api_429()
        assert wf._ADAPTIVE_INTERVAL == 12.0

    @patch("src.utils.weather_fetcher.get")
    def test_429_interval_capped_at_30s(self, mock_get):
        """自适应间隔上限 30s。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {"api_request_interval_sec": 3.0}
        wf._ADAPTIVE_INTERVAL = 20.0

        wf._on_api_429()
        assert wf._ADAPTIVE_INTERVAL == 30.0  # 20*2=40, capped to 30

        wf._on_api_429()
        assert wf._ADAPTIVE_INTERVAL == 30.0  # still capped

    @patch("src.utils.weather_fetcher.get")
    def test_success_shrinks_interval(self, mock_get):
        """成功响应应缓慢恢复间隔 (×0.85)。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {"api_request_interval_sec": 3.0}
        wf._ADAPTIVE_INTERVAL = 12.0

        wf._on_api_success()
        assert abs(wf._ADAPTIVE_INTERVAL - 10.2) < 0.01  # 12 * 0.85

        wf._on_api_success()
        assert abs(wf._ADAPTIVE_INTERVAL - 8.67) < 0.01  # 10.2 * 0.85

    @patch("src.utils.weather_fetcher.get")
    def test_success_interval_floor_is_config_base(self, mock_get):
        """成功恢复不低于配置基准值。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {"api_request_interval_sec": 3.0}
        wf._ADAPTIVE_INTERVAL = 3.5

        wf._on_api_success()
        assert wf._ADAPTIVE_INTERVAL == 3.0  # 3.5 * 0.85 = 2.975, floored to 3.0

    @patch("src.utils.weather_fetcher.get")
    def test_429_then_gradual_recovery(self, mock_get):
        """完整场景: 429 加倍 → 连续成功逐步恢复到基准。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {"api_request_interval_sec": 3.0}
        wf._ADAPTIVE_INTERVAL = 3.0

        # 2 次 429: 3→6→12
        wf._on_api_429()
        wf._on_api_429()
        assert wf._ADAPTIVE_INTERVAL == 12.0

        # 多次成功逐步恢复
        for _ in range(20):
            wf._on_api_success()
        assert wf._ADAPTIVE_INTERVAL == 3.0  # 应已恢复到基准


class TestStaleCacheFallback:
    """T267: 测试过期缓存兜底机制。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._CACHE.clear()
        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0  # 重置熔断器

    @patch("src.utils.weather_fetcher.get")
    def test_stale_cache_within_ttl_returns_data(self, mock_get):
        """过期但在 stale_ttl 内的缓存应被返回。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {"cache_stale_ttl_seconds": 7200}

        cache_key = "ens:40.00:-74.00:temp_max:7"
        data = {"2026-03-25": [20.0, 21.0, 22.0]}
        wf._CACHE[cache_key] = (time.time() - 3600, data)  # 1小时前

        result = wf._get_stale_cache(cache_key, "GFS")
        assert result == data

    @patch("src.utils.weather_fetcher.get")
    def test_stale_cache_beyond_ttl_returns_none(self, mock_get):
        """超过 stale_ttl 的缓存应返回 None。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {"cache_stale_ttl_seconds": 7200}

        cache_key = "ens:40.00:-74.00:temp_max:7"
        data = {"2026-03-25": [20.0, 21.0, 22.0]}
        wf._CACHE[cache_key] = (time.time() - 8000, data)  # >2小时前

        result = wf._get_stale_cache(cache_key, "GFS")
        assert result is None

    @patch("src.utils.weather_fetcher.get")
    def test_no_cache_entry_returns_none(self, mock_get):
        """无缓存条目时返回 None。"""
        import src.utils.weather_fetcher as wf

        mock_get.return_value = {"cache_stale_ttl_seconds": 7200}

        result = wf._get_stale_cache("nonexistent:key", "GFS")
        assert result is None

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_cache_hit_skips_api_call(self, mock_req_get, mock_cfg_get):
        """新鲜缓存命中时不应发起 API 请求。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        wf._LAST_ENSEMBLE_API_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 0.01

        mock_cfg_get.return_value = {
            "model": "gfs_seamless",
            "api_timeout": 5,
            "api_request_interval_sec": 0.01,
            "min_members_for_confidence": 20,
        }

        # 预置新鲜缓存
        cache_key = "ens:40.00:-74.00:temp_max:7"
        cached_data = {"2026-03-25": [20.0 + i * 0.1 for i in range(31)]}
        wf._CACHE[cache_key] = (time.time(), cached_data)  # 刚刚缓存

        result = _fetch_gfs_ensemble(40.0, -74.0, "temp_max", 7)
        assert result == cached_data
        # 不应发起任何 HTTP 请求
        mock_req_get.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# 城市NO策略: 熔断器测试 — _circuit_open / _is_circuit_open / _circuit_close
# ══════════════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    """城市NO策略: API 熔断器防止 429 级联。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0
        wf._GFS_CONSEC_FAILURES = 0
        wf._ECMWF_CONSEC_FAILURES = 0
        wf._LAST_ENSEMBLE_API_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 0.01
        wf._OPEN_METEO_LAST_429_TS = 0.0  # T275: 重置IP级429时间戳
        # 清除可能残留的测试缓存
        keys_to_clean = [k for k in wf._CACHE if k.startswith("ens:40.00:-74.00")]
        for k in keys_to_clean:
            del wf._CACHE[k]

    def test_circuit_open_blocks_gfs(self):
        """熔断器开启时 _fetch_gfs_ensemble 跳过主站API, 走降级链(最终emergency兜底)。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        wf._GFS_CIRCUIT_OPEN_UNTIL = time.time() + 600  # 开启 10 分钟
        wf._LAST_ENSEMBLE_API_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 0.01

        with patch("src.utils.weather_fetcher.requests.get") as mock_req:
            with patch("src.utils.weather_fetcher.get") as mock_cfg:
                mock_cfg.return_value = {
                    "model": "gfs_seamless",
                    "api_timeout": 5,
                    "api_request_interval_sec": 0.01,
                    "min_members_for_confidence": 20,
                    "gfs_cache_ttl_seconds": 3600,
                }
                result = _fetch_gfs_ensemble(40.0, -74.0, "temp_max", 7)
                # T275: 熔断时走降级链, 最终emergency兜底(never None)
                assert result is not None
                # ensemble主站不应被调用(熔断生效)
                # 但降级链中的NOMADS可能尝试HTTP请求

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0  # 清理

    def test_circuit_open_returns_stale_cache(self):
        """熔断器开启时有过期缓存, 返回过期数据而非 None。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        wf._GFS_CIRCUIT_OPEN_UNTIL = time.time() + 600
        wf._LAST_ENSEMBLE_API_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 0.01

        # 放入过期缓存 (30分钟前)
        stale_data = {"2026-03-30": [22.0 + i * 0.1 for i in range(31)]}
        wf._CACHE["ens:40.00:-74.00:temp_max:7"] = (
            time.time() - 1800,
            stale_data,
        )

        with patch("src.utils.weather_fetcher.requests.get") as mock_req:
            with patch("src.utils.weather_fetcher.get") as mock_cfg:
                mock_cfg.return_value = {
                    "model": "gfs_seamless",
                    "api_timeout": 5,
                    "api_request_interval_sec": 0.01,
                    "min_members_for_confidence": 20,
                    "gfs_cache_ttl_seconds": 3600,
                    "cache_stale_ttl_seconds": 7200,
                }
                result = _fetch_gfs_ensemble(40.0, -74.0, "temp_max", 7)
                assert result == stale_data
                mock_req.assert_not_called()

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0
        wf._CACHE.pop("ens:40.00:-74.00:temp_max:7", None)

    def test_circuit_close_on_success(self):
        """API 成功后熔断器自动关闭。"""
        import src.utils.weather_fetcher as wf

        wf._GFS_CIRCUIT_OPEN_UNTIL = time.time() + 600

        with patch("src.utils.weather_fetcher.get") as mock_cfg:
            mock_cfg.return_value = {"api_request_interval_sec": 0.01}
            wf._on_api_success()

        assert wf._GFS_CIRCUIT_OPEN_UNTIL == 0.0

    def test_429_opens_circuit(self):
        """429 时自动开启熔断器。"""
        import src.utils.weather_fetcher as wf

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ADAPTIVE_INTERVAL = 3.0

        with patch("src.utils.weather_fetcher.get") as mock_cfg:
            mock_cfg.return_value = {
                "api_request_interval_sec": 3.0,
                "circuit_breaker_cooldown_sec": 300,
            }
            wf._on_api_429()

        assert wf._GFS_CIRCUIT_OPEN_UNTIL > time.time()
        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0  # 清理

    def test_circuit_expires_naturally(self):
        """冷却期满后 _is_circuit_open 返回 False。"""
        import src.utils.weather_fetcher as wf

        wf._GFS_CIRCUIT_OPEN_UNTIL = time.time() - 1  # 已过期
        assert not wf._is_circuit_open()

    def test_gfs_429_breaks_immediately(self):
        """GFS 429 后立即 break, 只发 1 次ensemble请求(不重试3次); 降级链最终兜底。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0
        wf._LAST_ENSEMBLE_API_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 0.01
        wf._CACHE.pop("ens:40.00:-74.00:temp_max:7", None)

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.raise_for_status.side_effect = __import__(
            "requests"
        ).exceptions.HTTPError(response=resp_429)

        with patch(
            "src.utils.weather_fetcher.requests.get", return_value=resp_429
        ) as mock_req:
            with patch("src.utils.weather_fetcher.get") as mock_cfg:
                mock_cfg.return_value = {
                    "model": "gfs_seamless",
                    "api_timeout": 5,
                    "api_request_interval_sec": 0.01,
                    "min_members_for_confidence": 20,
                    "gfs_cache_ttl_seconds": 3600,
                    "circuit_breaker_cooldown_sec": 600,
                    "cache_stale_ttl_seconds": 7200,
                }
                result = _fetch_gfs_ensemble(40.0, -74.0, "temp_max", 7)

        # T275: 降级链最终 emergency climate 兜底, never None
        assert result is not None
        # 熔断器应已开启
        assert wf._GFS_CIRCUIT_OPEN_UNTIL > time.time()

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0

    @patch("src.utils.weather_fetcher._fetch_ecmwf_cloud_mirror", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    def test_ecmwf_429_breaks_immediately(self, mock_det_fb, mock_ecmwf_m):
        """ECMWF 429 后立即 break, 只发 1 次ensemble请求; 降级链最终emergency兜底。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_ecmwf_ensemble

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0
        wf._LAST_ENSEMBLE_API_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 0.01
        wf._OPEN_METEO_LAST_429_TS = 0.0
        wf._CACHE.pop("ecmwf:40.00:-74.00:temp_max:7", None)

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.raise_for_status.side_effect = __import__(
            "requests"
        ).exceptions.HTTPError(response=resp_429)

        with patch(
            "src.utils.weather_fetcher.requests.get", return_value=resp_429
        ) as mock_req:
            with patch("src.utils.weather_fetcher.get") as mock_cfg:
                mock_cfg.return_value = {
                    "ecmwf_model": "ecmwf_ifs04",
                    "ecmwf_cache_ttl_seconds": 50400,
                    "ecmwf_members": 51,
                    "api_timeout": 5,
                    "api_request_interval_sec": 0.01,
                    "min_members_for_confidence": 20,
                    "circuit_breaker_cooldown_sec": 600,
                    "cache_stale_ttl_seconds": 7200,
                }
                result = _fetch_ecmwf_ensemble(40.0, -74.0, "temp_max", 7)

        # T275: 降级链最终 emergency climate 兜底, never None
        assert result is not None
        # T271: ensemble端点只发 1 次请求
        assert mock_req.call_count == 1
        # T272: 确定性降级被调用
        assert mock_det_fb.call_count >= 1
        # T271: ECMWF 429 应开启 ECMWF 熔断器，不影响 GFS
        assert wf._ECMWF_CIRCUIT_OPEN_UNTIL > time.time()
        assert wf._GFS_CIRCUIT_OPEN_UNTIL == 0.0

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0


# ══════════════════════════════════════════════════════════════════════
# 城市NO策略: 批量预取测试 — prefetch_ensemble_data + _batch_fetch_ensemble
# ══════════════════════════════════════════════════════════════════════


def _make_gfs_response(dates: list[str], member_count: int = 31) -> dict:
    """构造 GFS ensemble API 响应数据。"""
    daily: dict = {"time": dates}
    for idx in range(1, member_count + 1):
        col = f"temperature_2m_max_member{idx:02d}"
        daily[col] = [20.0 + idx * 0.1] * len(dates)
    return {"daily": daily}


def _make_ecmwf_response(dates: list[str], member_count: int = 51) -> dict:
    """构造 ECMWF ensemble API 响应数据。"""
    daily: dict = {"time": dates}
    for idx in range(1, member_count + 1):
        col = f"temperature_2m_max_member{idx:02d}"
        daily[col] = [21.0 + idx * 0.1] * len(dates)
    return {"daily": daily}


class TestBatchFetchEnsemble:
    """_batch_fetch_ensemble 批量多坐标请求测试。"""

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_multi_location_request(self, mock_req_get, mock_cfg_get):
        """多坐标批量请求: 一次 API 调用获取多个城市数据。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _batch_fetch_ensemble

        wf._LAST_ENSEMBLE_API_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 0.01

        mock_cfg_get.return_value = {
            "model": "gfs_seamless",
            "api_timeout": 5,
            "api_request_interval_sec": 0.01,
            "min_members_for_confidence": 20,
        }

        dates = ["2026-03-30", "2026-03-31"]
        # 多坐标返回列表
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = [
            _make_gfs_response(dates, 31),
            _make_gfs_response(dates, 31),
        ]
        mock_req_get.return_value = mock_resp

        locations = [(40.78, -73.87), (51.47, -0.46)]
        result = _batch_fetch_ensemble(
            locations,
            "temp_max",
            "gfs_seamless",
            "GFS",
            member_count=31,
            forecast_days=8,
            cache_prefix="ens",
        )

        # 验证只发了 1 次 HTTP 请求
        assert mock_req_get.call_count == 1
        # URL 包含逗号分隔的坐标
        url = mock_req_get.call_args[0][0]
        assert "40.7800" in url
        assert "51.4700" in url
        assert "-73.8700" in url
        assert "-0.4600" in url

        # 验证结果和缓存
        assert len(result) == 2
        assert (40.78, -73.87) in result
        assert (51.47, -0.46) in result

        # 验证缓存已填充
        ck1 = "ens:40.78:-73.87:temp_max:8"
        ck2 = "ens:51.47:-0.46:temp_max:8"
        assert ck1 in wf._CACHE
        assert ck2 in wf._CACHE

        # 清理
        wf._CACHE.pop(ck1, None)
        wf._CACHE.pop(ck2, None)

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_single_location_dict_response(self, mock_req_get, mock_cfg_get):
        """单坐标时 API 返回 dict(非 list), 应正常处理。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _batch_fetch_ensemble

        wf._LAST_ENSEMBLE_API_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 0.01

        mock_cfg_get.return_value = {
            "model": "gfs_seamless",
            "api_timeout": 5,
            "api_request_interval_sec": 0.01,
            "min_members_for_confidence": 20,
        }

        dates = ["2026-03-30"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = _make_gfs_response(dates, 31)  # dict, not list
        mock_req_get.return_value = mock_resp

        locations = [(40.78, -73.87)]
        result = _batch_fetch_ensemble(
            locations,
            "temp_max",
            "gfs_seamless",
            "GFS",
            member_count=31,
            forecast_days=8,
            cache_prefix="ens",
        )

        assert len(result) == 1
        assert (40.78, -73.87) in result

        # 清理
        wf._CACHE.pop("ens:40.78:-73.87:temp_max:8", None)

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_429_returns_empty(self, mock_req_get, mock_cfg_get):
        """批量请求 429 时立即熔断, 返回空 dict, 只发1次请求。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _batch_fetch_ensemble

        wf._LAST_ENSEMBLE_API_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 0.01
        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0

        mock_cfg_get.return_value = {
            "api_timeout": 5,
            "api_request_interval_sec": 0.01,
            "min_members_for_confidence": 20,
            "circuit_breaker_cooldown_sec": 600,
        }

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.raise_for_status.side_effect = __import__(
            "requests"
        ).exceptions.HTTPError(response=resp_429)
        mock_req_get.return_value = resp_429

        result = _batch_fetch_ensemble(
            [(40.78, -73.87)],
            "temp_max",
            "gfs_seamless",
            "GFS",
            member_count=31,
            forecast_days=8,
            cache_prefix="ens",
        )
        assert result == {}
        # 城市NO策略: 429 后立即 break, 只发 1 次请求(不再重试)
        assert mock_req_get.call_count == 1
        # T271: 批量429 应开启 BATCH 熔断器，不影响 GFS/ECMWF 单城市请求
        assert wf._BATCH_CIRCUIT_OPEN_UNTIL > time.time()
        assert wf._GFS_CIRCUIT_OPEN_UNTIL == 0.0

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0


class TestDeterministicFallback:
    """T272: 三层降级链测试 — 确保 GFS/ECMWF 主站不可用时永不返回 None（只要有任何降级途径）。"""

    def setup_method(self):
        """每个测试前重置熔断器和降级计数。"""
        import src.utils.weather_fetcher as wf

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._GFS_DET_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_DET_CIRCUIT_OPEN_UNTIL = 0.0
        # T273: 重置NOMADS熔断器 (防止跨测试污染)
        wf._NOMADS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._GFS_FALLBACK_COUNT = 0
        wf._ECMWF_FALLBACK_COUNT = 0
        wf._CACHE.clear()

    @patch("src.utils.weather_fetcher.requests.get")
    @patch("src.utils.weather_fetcher.get")
    def test_gfs_circuit_open_uses_deterministic_fallback(
        self, mock_cfg_get, mock_req_get
    ):
        """GFS 熔断且无 stale cache → 确定性API提供合成集合数据（不返回 None）。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        mock_cfg_get.return_value = {
            "max_forecast_days": 7,
            "gfs_cache_ttl_seconds": 3600,
            "min_members_for_confidence": 20,
            "circuit_breaker_cooldown_sec": 60,
            "deterministic_fallback_enabled": True,
            "fallback_spread_celsius": 3.5,
            "fallback_synthetic_members": 31,
            "fallback_consecutive_warn_threshold": 3,
        }

        # 开启 GFS 熔断
        wf._GFS_CIRCUIT_OPEN_UNTIL = time.time() + 300.0

        # 确定性 API 返回有效数据
        det_resp = MagicMock()
        det_resp.status_code = 200
        det_resp.raise_for_status.return_value = None
        det_resp.json.return_value = {
            "daily": {
                "time": ["2026-03-30", "2026-03-31"],
                "temperature_2m_max": [33.0, 34.5],
            }
        }
        mock_req_get.return_value = det_resp

        result = _fetch_gfs_ensemble(1.35, 103.99, "temp_max", forecast_days=3)

        assert result is not None, "GFS 熔断时确定性API降级后不应返回 None"
        assert "2026-03-30" in result
        # T273: 确定性API返回单值列表 (NOT Gaussian合成集合)
        assert (
            len(result["2026-03-30"]) == 1
        ), "T273: 降级数据应为单值列表, 不再是31成员Gaussian合成"
        # 验证单值与真实确定性预报一致
        assert (
            abs(result["2026-03-30"][0] - 33.0) < 0.5
        ), f"T273: 单值 {result['2026-03-30'][0]:.2f} 应等于确定性预报 33.0°C"

    @patch("time.sleep")
    @patch("src.utils.weather_fetcher.requests.get")
    @patch("src.utils.weather_fetcher.get")
    def test_gfs_api_failure_uses_deterministic_fallback(
        self, mock_cfg_get, mock_req_get, mock_sleep
    ):
        """GFS ensemble 主站失败 → 自动切换到确定性API（不返回 None）。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        mock_cfg_get.return_value = {
            "max_forecast_days": 7,
            "gfs_cache_ttl_seconds": 3600,
            "min_members_for_confidence": 20,
            "circuit_breaker_cooldown_sec": 60,
            "deterministic_fallback_enabled": True,
            "fallback_spread_celsius": 3.5,
            "fallback_synthetic_members": 31,
            "fallback_consecutive_warn_threshold": 3,
        }

        # 所有非确定性API请求 → 503 失败
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        fail_resp.raise_for_status.side_effect = __import__(
            "requests"
        ).exceptions.HTTPError(response=fail_resp)
        fail_resp.response = fail_resp

        # 使用动态未来日期避免日期过期导致测试失败
        from datetime import datetime, timedelta, timezone

        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

        det_resp = MagicMock()
        det_resp.status_code = 200
        det_resp.raise_for_status.return_value = None
        det_resp.json.return_value = {
            "daily": {
                "time": [tomorrow],
                "temperature_2m_max": [28.5],
            }
        }

        # T296: 使用side_effect函数代替列表, 降级链各层请求数量动态变化
        # 确定性API URL包含 forecast? 且不含 ensemble, 其余全部503
        def _mock_get(*args, **kwargs):
            url = args[0] if args else ""
            if "forecast?" in str(url) and "ensemble" not in str(url):
                return det_resp
            return fail_resp

        mock_req_get.side_effect = _mock_get

        result = _fetch_gfs_ensemble(35.68, 139.69, "temp_max", forecast_days=3)

        assert result is not None, "ensemble 主站失败时确定性API降级后不应返回 None"
        assert tomorrow in result, f"结果中应包含 {tomorrow}: {list(result.keys())}"
        # T273: 确定性API降级返回单值列表, 不再是31成员Gaussian合成
        assert len(result[tomorrow]) == 1

    @patch("src.utils.weather_fetcher.requests.get")
    @patch("src.utils.weather_fetcher.get")
    def test_deterministic_fallback_disabled_by_config(
        self, mock_cfg_get, mock_req_get
    ):
        """deterministic_fallback_enabled=false 时不调用确定性API。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        mock_cfg_get.return_value = {
            "max_forecast_days": 7,
            "gfs_cache_ttl_seconds": 3600,
            "min_members_for_confidence": 20,
            "circuit_breaker_cooldown_sec": 60,
            "deterministic_fallback_enabled": False,
        }

        # 开启 GFS 熔断
        wf._GFS_CIRCUIT_OPEN_UNTIL = time.time() + 300.0

        result = _fetch_gfs_ensemble(1.35, 103.99, "temp_max", forecast_days=3)

        # T275: 全部降级链失败 → 紧急气候学估计 (never-None)
        assert result is not None

    @patch("src.utils.weather_fetcher.requests.get")
    @patch("src.utils.weather_fetcher.get")
    def test_stale_cache_any_age_last_resort(self, mock_cfg_get, mock_req_get):
        """所有 API 均失败 → 使用任意年龄的 stale cache 作为最终兜底。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        mock_cfg_get.return_value = {
            "max_forecast_days": 7,
            "gfs_cache_ttl_seconds": 3600,
            "min_members_for_confidence": 20,
            "circuit_breaker_cooldown_sec": 60,
            "deterministic_fallback_enabled": True,
            "fallback_spread_celsius": 3.5,
            "fallback_synthetic_members": 31,
            "fallback_consecutive_warn_threshold": 3,
            "stale_cache_ttl_sec": 7200,
        }

        # 预置非常旧的 stale cache (超过 TTL 但是唯一数据)
        OLD_DATA = {"2026-03-01": [20.0] * 31}
        very_old_ts = time.time() - 86400  # 24小时前
        wf._CACHE["ens:1.35:103.99:temp_max:3"] = (very_old_ts, OLD_DATA)

        # 开启 GFS 熔断
        wf._GFS_CIRCUIT_OPEN_UNTIL = time.time() + 300.0

        # 确定性 API 也失败
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        fail_resp.raise_for_status.side_effect = __import__(
            "requests"
        ).exceptions.HTTPError(response=fail_resp)
        mock_req_get.return_value = fail_resp

        result = _fetch_gfs_ensemble(1.35, 103.99, "temp_max", forecast_days=3)

        assert result == OLD_DATA, "所有API失败时应返回任意年龄的stale cache"

    @patch("src.utils.weather_fetcher.requests.get")
    @patch("src.utils.weather_fetcher.get")
    def test_consecutive_fallback_warning(self, mock_cfg_get, mock_req_get):
        """连续降级超过阈值时发出 WARNING 日志。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_deterministic_fallback

        mock_cfg_get.return_value = {
            "max_forecast_days": 7,
            "deterministic_fallback_enabled": True,
            "fallback_spread_celsius": 3.5,
            "fallback_synthetic_members": 31,
            "fallback_consecutive_warn_threshold": 2,
        }

        det_resp = MagicMock()
        det_resp.status_code = 200
        det_resp.raise_for_status.return_value = None
        det_resp.json.return_value = {
            "daily": {
                "time": ["2026-03-30"],
                "temperature_2m_max": [33.0],
            }
        }
        mock_req_get.return_value = det_resp

        # 调用3次（超过阈值2）
        with patch("src.utils.weather_fetcher.logger") as mock_logger:
            _fetch_deterministic_fallback(
                1.35, 103.99, "gfs_seamless", "temp_max", 3, "GFS"
            )
            _fetch_deterministic_fallback(
                1.35, 103.99, "gfs_seamless", "temp_max", 3, "GFS"
            )
            _fetch_deterministic_fallback(
                1.35, 103.99, "gfs_seamless", "temp_max", 3, "GFS"
            )

        # 第3次调用时 count>=2 → 应有 warning 调用
        warn_calls = [str(c) for c in mock_logger.warning.call_args_list]
        fallback_warns = [c for c in warn_calls if "连续" in c and "降级" in c]
        assert (
            len(fallback_warns) > 0
        ), f"连续降级3次后应有WARNING日志，实际调用: {warn_calls}"

    @patch("src.utils.weather_fetcher.requests.get")
    @patch("src.utils.weather_fetcher.get")
    def test_build_synthetic_ensemble_reproducible(self, mock_cfg_get, mock_req_get):
        """_build_synthetic_ensemble 对相同输入应产生相同输出（可复现性）。"""
        from src.utils.weather_fetcher import _build_synthetic_ensemble

        result1 = _build_synthetic_ensemble(33.0, "2026-03-30", 3.5, 31)
        result2 = _build_synthetic_ensemble(33.0, "2026-03-30", 3.5, 31)
        assert result1 == result2, "合成集合对相同输入必须产生相同输出"
        assert len(result1) == 31
        assert result1 == sorted(result1), "合成集合应为升序排列"

        # 不同输入应产生不同结果
        result3 = _build_synthetic_ensemble(28.0, "2026-03-30", 3.5, 31)
        assert result1 != result3, "不同均值应产生不同集合"

    @patch("src.utils.weather_fetcher.requests.get")
    @patch("src.utils.weather_fetcher.get")
    def test_deterministic_fallback_det_circuit_open(self, mock_cfg_get, mock_req_get):
        """确定性API自身429后，det电路熔断120s，跳过确定性API。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_deterministic_fallback

        mock_cfg_get.return_value = {
            "deterministic_fallback_enabled": True,
            "fallback_spread_celsius": 3.5,
            "fallback_synthetic_members": 31,
            "fallback_consecutive_warn_threshold": 3,
        }

        # 确定性API也429
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.raise_for_status.return_value = None
        mock_req_get.return_value = resp_429

        result = _fetch_deterministic_fallback(
            1.35, 103.99, "gfs_seamless", "temp_max", 3, "GFS"
        )
        assert result is None  # 429 → None
        assert (
            wf._GFS_DET_CIRCUIT_OPEN_UNTIL > time.time()
        ), "确定性API 429应开启det熔断器"

        # 再次调用应跳过（熔断中）
        mock_req_get.reset_mock()
        result2 = _fetch_deterministic_fallback(
            1.35, 103.99, "gfs_seamless", "temp_max", 3, "GFS"
        )
        assert result2 is None
        mock_req_get.assert_not_called()  # 熔断时不发请求


class TestNOMADSFallback:
    """T273: NOAA NOMADS GDS OPeNDAP ASCII 降级测试。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._NOMADS_CIRCUIT_OPEN_UNTIL = 0.0

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_nomads_circuit_open_skips_request(self, mock_req_get, mock_cfg_get):
        """NOMADS熔断开启时不发出requests.get请求。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_nomads_gfs

        mock_cfg_get.return_value = {"api_timeout": 10}
        wf._NOMADS_CIRCUIT_OPEN_UNTIL = time.time() + 120.0

        result = _fetch_nomads_gfs(35.68, 139.69, "temp_max", 3)
        assert result is None
        mock_req_get.assert_not_called()

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_nomads_429_opens_circuit(self, mock_req_get, mock_cfg_get):
        """NOMADS 429响应 → 熔断器开启120s。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_nomads_gfs

        mock_cfg_get.return_value = {"api_timeout": 10}

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.ok = False
        mock_req_get.return_value = resp_429

        result = _fetch_nomads_gfs(35.68, 139.69, "temp_max", 3)
        assert result is None
        assert wf._NOMADS_CIRCUIT_OPEN_UNTIL > time.time()

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_nomads_parses_opendap_ascii(self, mock_req_get, mock_cfg_get):
        """NOMADS返回正确的OPeNDAP ASCII → 解析为单值摄氏度结果。"""
        from src.utils.weather_fetcher import _fetch_nomads_gfs

        mock_cfg_get.return_value = {"api_timeout": 10}

        now_utc = datetime.now(timezone.utc)
        target = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")

        # OPeNDAP ASCII格式: -----分隔符后是数值(Kelvin)
        ascii_body = (
            "Dataset {...}\n"
            "---------------------------------------------\n"
            "tmp2m, [8][1][1]\n"
            "303.15, 304.25, 305.65, 306.10, 305.80, 304.50, 303.20, 302.00\n"
        )
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.ok = True
        resp_ok.text = ascii_body
        mock_req_get.return_value = resp_ok

        result = _fetch_nomads_gfs(35.68, 139.69, "temp_max", 2)
        # 应解析Kelvin值，取最大值，减273.15，返回单值列表
        assert result is not None
        # max Kelvin = 306.10 → 306.10 - 273.15 = 32.95°C
        for date_str, vals in result.items():
            assert len(vals) == 1, "NOMADS应返回单值列表"
            assert 30.0 < vals[0] < 35.0, f"解析值{vals[0]}应在合理范围内(30-35°C)"

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_nomads_404_tries_previous_run(self, mock_req_get, mock_cfg_get):
        """NOMADS 404 → 自动尝试前一个GFS运行时次。"""
        from src.utils.weather_fetcher import _fetch_nomads_gfs

        mock_cfg_get.return_value = {"api_timeout": 10}

        now_utc = datetime.now(timezone.utc)
        ascii_body = (
            "---------------------------------------------\n"
            "303.15, 304.25, 305.65, 306.10, 305.80, 304.50, 303.20, 302.00\n"
        )
        resp_404 = MagicMock()
        resp_404.status_code = 404
        resp_404.ok = False

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.ok = True
        resp_ok.text = ascii_body

        # 第1次(当前运行时次) → 404; 第2次(前一运行时次) → 200
        mock_req_get.side_effect = [resp_404, resp_ok] * 10  # 多天

        result = _fetch_nomads_gfs(35.68, 139.69, "temp_max", 1)
        # 前一运行时次有数据 → 成功
        assert result is not None


class TestFetchEnsembleProbabilityDegraded:
    """T273: fetch_ensemble_probability 单值降级数据 — normcdf + 源置信度惩罚测试。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._GFS_DET_CIRCUIT_OPEN_UNTIL = 0.0
        wf._NOMADS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._DATA_SOURCE_META.clear()
        wf._CACHE.clear()

    def _make_mock_config(self):
        return {
            "gfs_members": 31,
            "ecmwf_members": 51,
            "max_forecast_days": 7,
            "gfs_cache_ttl_seconds": 3600,
            "min_members_for_confidence": 20,
            "circuit_breaker_cooldown_sec": 60,
            "deterministic_fallback_enabled": True,
            "fallback_spread_celsius": 3.5,
            "fallback_synthetic_members": 31,
            "fallback_consecutive_warn_threshold": 3,
        }

    @patch(
        "src.utils.weather_fetcher.get",
        side_effect=lambda k: {
            "weather_ensemble": {
                "gfs_members": 31,
                "max_forecast_days": 7,
                "gfs_cache_ttl_seconds": 3600,
                "min_members_for_confidence": 20,
                "circuit_breaker_cooldown_sec": 60,
                "deterministic_fallback_enabled": True,
            },
            "weather_cities": ["Tokyo"],
        }.get(k, None),
    )
    @patch("src.utils.weather_fetcher._get_city_coords")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_single_value_gte_uses_normcdf(self, mock_req, mock_coords, mock_get):
        """单值det_fallback数据 → normcdf概率 + 置信度≤0.35×0.85=0.297。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import fetch_ensemble_probability

        mock_coords.return_value = (35.68, 139.69)

        now_utc = datetime.now(timezone.utc)
        cache_key = "ens:35.68:139.69:temp_max:8"  # max_days+1=8
        target = now_utc.strftime("%Y-%m-%d")  # today

        # 注入单值数据 (模拟det_fallback)
        wf._CACHE[cache_key] = (time.time(), {target: [33.0]})
        wf._DATA_SOURCE_META[cache_key] = {"source": "det_fallback", "members": 1}

        market = {
            "question": f"Will the high temperature in Tokyo on {now_utc.strftime('%B %-d')} be 30\u00b0C or warmer?"
        }
        result = fetch_ensemble_probability(market)

        assert result is not None
        # T_det=33°C, threshold=30°C, direction=gte
        # days_ahead=0 → sigma=1.5
        # P(T≥30|T_det=33, σ=1.5) = 1 - normcdf((30-33)/1.5) = 1 - normcdf(-2)
        # normcdf(-2) ≈ 0.023 → P ≈ 0.977 → capped 0.92
        assert result["probability"] >= 0.88, "33°C远高于30°C阈值, 概率应很高"
        # 置信度 ≤ 0.297 (=0.35 × horizon×0.72)
        assert result["confidence"] <= 0.50, "单值数据置信度应显著低于正常集合"
        assert (
            result["data_quality"] == "degraded"
        ), "source!=ensemble时应标记为degraded"
        assert result["source"] == "det_fallback"

    @patch(
        "src.utils.weather_fetcher.get",
        side_effect=lambda k: {
            "weather_ensemble": {
                "gfs_members": 31,
                "max_forecast_days": 7,
                "gfs_cache_ttl_seconds": 3600,
                "min_members_for_confidence": 20,
                "circuit_breaker_cooldown_sec": 60,
                "deterministic_fallback_enabled": True,
            },
            "weather_cities": ["Seoul"],
        }.get(k, None),
    )
    @patch("src.utils.weather_fetcher._get_city_coords")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_synthetic_data_confidence_capped_at_015(
        self, mock_req, mock_coords, mock_get
    ):
        """synthetic数据 → confidence硬上限≤0.15 + SYNTHETIC DATA日志标记。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import fetch_ensemble_probability

        mock_coords.return_value = (37.56, 126.98)

        now_utc = datetime.now(timezone.utc)
        cache_key = "ens:37.56:126.98:temp_max:8"
        target = now_utc.strftime("%Y-%m-%d")

        wf._CACHE[cache_key] = (time.time(), {target: [28.5]})
        wf._DATA_SOURCE_META[cache_key] = {"source": "synthetic", "members": 5}

        market = {
            "question": f"Will the high temperature in Seoul on {now_utc.strftime('%B %-d')} be 25\u00b0C or warmer?"
        }
        result = fetch_ensemble_probability(market)

        assert result is not None
        # synthetic → confidence应被硬封顶至≤0.15
        assert (
            result["confidence"] <= 0.15
        ), f"synthetic数据置信度不得超过0.15, 实际:{result['confidence']}"
        assert result["source"] == "synthetic"
        assert result["data_quality"] == "degraded"

    @patch(
        "src.utils.weather_fetcher.get",
        side_effect=lambda k: {
            "weather_ensemble": {
                "gfs_members": 31,
                "max_forecast_days": 7,
                "gfs_cache_ttl_seconds": 3600,
                "min_members_for_confidence": 20,
                "circuit_breaker_cooldown_sec": 60,
                "deterministic_fallback_enabled": True,
            },
            "weather_cities": ["Tokyo"],
        }.get(k, None),
    )
    @patch("src.utils.weather_fetcher._get_city_coords")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_real_ensemble_confidence_not_penalized(
        self, mock_req, mock_coords, mock_get
    ):
        """真实集合数据(source=ensemble) → source_mult=1.0, 置信度不被惩罚。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import fetch_ensemble_probability

        mock_coords.return_value = (35.68, 139.69)

        now_utc = datetime.now(timezone.utc)
        cache_key = "ens:35.68:139.69:temp_max:8"
        target = now_utc.strftime("%Y-%m-%d")

        # 31成员真实集合: 25个成员≥30°C → P=25/31
        members = [30.5] * 25 + [28.0] * 6
        wf._CACHE[cache_key] = (time.time(), {target: members})
        # 真实集合 → source_meta默认ensemble
        wf._DATA_SOURCE_META.pop(cache_key, None)

        market = {
            "question": f"Will the high temperature in Tokyo on {now_utc.strftime('%B %-d')} be 30\u00b0C or warmer?"
        }
        result = fetch_ensemble_probability(market)

        assert result is not None
        assert result["probability"] == pytest.approx(25 / 31, abs=0.01)
        assert result["source"] == "ensemble"
        assert result["data_quality"] == "full"
        # 真实集合置信度不受惩罚 (source_mult=1.0)
        # 25/31=0.806 共识 × horizon(day0=0.96) × 0.95 ≈ 0.735
        assert result["confidence"] >= 0.50, "真实集合置信度不应因source被惩罚"

    @patch(
        "src.utils.weather_fetcher.get",
        side_effect=lambda k: {
            "weather_ensemble": {
                "gfs_members": 31,
                "max_forecast_days": 7,
                "gfs_cache_ttl_seconds": 3600,
                "min_members_for_confidence": 20,
                "circuit_breaker_cooldown_sec": 60,
                "deterministic_fallback_enabled": True,
            },
            "weather_cities": ["Tokyo"],
        }.get(k, None),
    )
    @patch("src.utils.weather_fetcher._get_city_coords")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_noaa_nomads_confidence_multiplier(self, mock_req, mock_coords, mock_get):
        """NOMADS单值数据 → confidence×0.62惩罚系数。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import fetch_ensemble_probability

        mock_coords.return_value = (35.68, 139.69)

        now_utc = datetime.now(timezone.utc)
        cache_key = "ens:35.68:139.69:temp_max:8"
        target = now_utc.strftime("%Y-%m-%d")

        wf._CACHE[cache_key] = (time.time(), {target: [32.0]})
        wf._DATA_SOURCE_META[cache_key] = {"source": "noaa_nomads", "members": 1}

        market = {
            "question": f"Will the high temperature in Tokyo on {now_utc.strftime('%B %-d')} be 30\u00b0C or warmer?"
        }
        result = fetch_ensemble_probability(market)

        assert result is not None
        assert result["source"] == "noaa_nomads"
        # noaa_nomads mult=0.62 → conf = base_conf×0.62
        assert (
            result["confidence"] <= 0.85 * 0.62 + 0.01
        ), f"NOMADS置信度 {result['confidence']:.3f} 应不超过max_base×0.62"
        assert result["data_quality"] == "degraded"

    def test_source_confidence_multiplier_complete(self):
        """_SOURCE_CONFIDENCE_MULTIPLIER 包含所有必要键且值在合理范围。"""
        from src.utils.weather_fetcher import _SOURCE_CONFIDENCE_MULTIPLIER

        required_keys = {
            "ensemble",
            "noaa_nomads",
            "stale_fresh",
            "stale_recent",
            "stale_old",
            "det_fallback",
            "synthetic",
        }
        for key in required_keys:
            assert key in _SOURCE_CONFIDENCE_MULTIPLIER, f"缺少key: {key}"
            mult = _SOURCE_CONFIDENCE_MULTIPLIER[key]
            assert 0.0 < mult <= 1.0, f"{key}: mult={mult}超出(0,1]范围"

        # 严格递减约束
        assert _SOURCE_CONFIDENCE_MULTIPLIER["ensemble"] == 1.0
        assert _SOURCE_CONFIDENCE_MULTIPLIER["synthetic"] <= 0.2

    """prefetch_ensemble_data 批量预取入口函数测试。"""

    @patch("src.utils.weather_fetcher._batch_fetch_ensemble")
    @patch("src.utils.weather_fetcher._get_city_coords")
    @patch("src.utils.weather_fetcher.parse_weather_question")
    @patch("src.utils.weather_fetcher.get")
    def test_prefetch_calls_batch(
        self, mock_cfg_get, mock_parse, mock_coords, mock_batch
    ):
        """预取正确收集城市并调用批量 API。"""
        from src.utils.weather_fetcher import prefetch_ensemble_data

        mock_cfg_get.return_value = {
            "max_forecast_days": 7,
            "ecmwf_enabled": True,
            "ecmwf_model": "ecmwf_ifs04",
            "ecmwf_members": 51,
            "ecmwf_cache_ttl_seconds": 50400,
            "model": "gfs_seamless",
        }

        mock_parse.side_effect = [
            {"city": "New York", "variable": "temp_max"},
            {"city": "London", "variable": "temp_max"},
        ]
        mock_coords.side_effect = [(40.78, -73.87), (51.47, -0.46)]
        mock_batch.return_value = {}

        markets = [
            {"question": "Will the highest temperature in New York be 25°C?"},
            {"question": "Will the highest temperature in London be 18°C?"},
        ]
        api_calls = prefetch_ensemble_data(markets)

        # 应调用2次batch: 1 GFS + 1 ECMWF
        assert mock_batch.call_count == 2
        assert api_calls == 2

    @patch("src.utils.weather_fetcher.parse_weather_question")
    @patch("src.utils.weather_fetcher.get")
    def test_prefetch_empty_markets(self, mock_cfg_get, mock_parse):
        """空市场列表不应发起任何请求。"""
        from src.utils.weather_fetcher import prefetch_ensemble_data

        mock_cfg_get.return_value = {"max_forecast_days": 7}
        result = prefetch_ensemble_data([])
        assert result == 0
        mock_parse.assert_not_called()

    @patch("src.utils.weather_fetcher._batch_fetch_ensemble")
    @patch("src.utils.weather_fetcher._get_city_coords")
    @patch("src.utils.weather_fetcher.parse_weather_question")
    @patch("src.utils.weather_fetcher.get")
    def test_prefetch_skips_cached(
        self, mock_cfg_get, mock_parse, mock_coords, mock_batch
    ):
        """已缓存的坐标不应重新请求。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import prefetch_ensemble_data

        mock_cfg_get.return_value = {
            "max_forecast_days": 7,
            "ecmwf_enabled": True,
            "ecmwf_model": "ecmwf_ifs04",
            "ecmwf_members": 51,
            "ecmwf_cache_ttl_seconds": 50400,
            "model": "gfs_seamless",
        }

        mock_parse.return_value = {"city": "New York", "variable": "temp_max"}
        mock_coords.return_value = (40.78, -73.87)

        # 预置新鲜缓存 (GFS + ECMWF)
        now = time.time()
        wf._CACHE["ens:40.78:-73.87:temp_max:8"] = (now, {"2026-03-30": [20.0] * 31})
        wf._CACHE["ecmwf:40.78:-73.87:temp_max:8"] = (now, {"2026-03-30": [21.0] * 51})

        markets = [{"question": "NYC temp question"}]
        api_calls = prefetch_ensemble_data(markets)

        # 全部缓存命中, 0 次 API 调用
        assert api_calls == 0
        mock_batch.assert_not_called()

        # 清理
        wf._CACHE.pop("ens:40.78:-73.87:temp_max:8", None)
        wf._CACHE.pop("ecmwf:40.78:-73.87:temp_max:8", None)

    @patch("src.utils.weather_fetcher.parse_weather_question")
    @patch("src.utils.weather_fetcher.get")
    def test_prefetch_unparseable_markets(self, mock_cfg_get, mock_parse):
        """无法解析的市场问题应被跳过。"""
        from src.utils.weather_fetcher import prefetch_ensemble_data

        mock_cfg_get.return_value = {"max_forecast_days": 7}
        mock_parse.return_value = None  # 解析失败

        markets = [{"question": "Will BTC reach $100k?"}]
        result = prefetch_ensemble_data(markets)
        assert result == 0

    @patch("src.utils.weather_fetcher._batch_fetch_ensemble")
    @patch("src.utils.weather_fetcher._get_city_coords")
    @patch("src.utils.weather_fetcher.parse_weather_question")
    @patch("src.utils.weather_fetcher.get")
    def test_prefetch_deduplicates_same_city(
        self, mock_cfg_get, mock_parse, mock_coords, mock_batch
    ):
        """同一城市的多个 strike 市场应只请求一次。"""
        from src.utils.weather_fetcher import prefetch_ensemble_data

        mock_cfg_get.return_value = {
            "max_forecast_days": 7,
            "ecmwf_enabled": False,
            "model": "gfs_seamless",
        }

        # 两个市场同一城市 → 同一坐标
        mock_parse.side_effect = [
            {"city": "New York", "variable": "temp_max"},
            {"city": "New York", "variable": "temp_max"},
        ]
        mock_coords.side_effect = [(40.78, -73.87), (40.78, -73.87)]
        mock_batch.return_value = {}

        markets = [
            {"question": "NYC high be 25°C?"},
            {"question": "NYC high be 28°C?"},
        ]
        api_calls = prefetch_ensemble_data(markets)

        # ECMWF disabled → 只调1次 GFS batch
        assert api_calls == 1
        # batch 应只含 1 个坐标（去重）
        batch_call_args = mock_batch.call_args_list[0]
        locations = batch_call_args[0][0]
        assert len(locations) == 1


# ===========================================================================
# T274: AWS S3 GFS + ECMWF 云端镜像测试
# ===========================================================================


class TestParseGfsIdx:
    """T274: GFS .idx 索引文件解析测试。"""

    def test_parse_idx_finds_tmp_2m(self):
        """能正确解析 .idx 文件找到 TMP:2 m above ground 的字节范围。"""
        from src.utils.weather_fetcher import _parse_gfs_idx

        idx_text = (
            "1:0:d=2026032900:PRMSL:mean sea level:anl:\n"
            "2:45678:d=2026032900:TMP:2 m above ground:anl:\n"
            "3:89012:d=2026032900:TMP:surface:anl:\n"
            "4:120000:d=2026032900:RH:2 m above ground:anl:\n"
        )
        ranges = _parse_gfs_idx(idx_text, "TMP", "2 m above ground")
        assert len(ranges) == 1
        assert ranges[0] == (45678, 89012 - 1)

    def test_parse_idx_empty_returns_empty(self):
        """空 .idx 内容返回空列表。"""
        from src.utils.weather_fetcher import _parse_gfs_idx

        assert _parse_gfs_idx("", "TMP", "2 m above ground") == []

    def test_parse_idx_no_match_returns_empty(self):
        """不匹配的变量名返回空列表。"""
        from src.utils.weather_fetcher import _parse_gfs_idx

        idx_text = "1:0:d=2026032900:PRMSL:mean sea level:anl:\n"
        assert _parse_gfs_idx(idx_text, "TMP", "2 m above ground") == []

    def test_parse_idx_last_entry_uses_fallback_end(self):
        """最后一条消息使用 offset + 2MB 作为结束。"""
        from src.utils.weather_fetcher import _parse_gfs_idx

        idx_text = (
            "1:0:d=2026032900:PRMSL:mean sea level:anl:\n"
            "2:45678:d=2026032900:TMP:2 m above ground:anl:\n"
        )
        ranges = _parse_gfs_idx(idx_text, "TMP", "2 m above ground")
        assert len(ranges) == 1
        assert ranges[0][0] == 45678
        assert ranges[0][1] == 45678 + 2_000_000  # fallback end

    def test_parse_idx_multiple_matches(self):
        """同一变量多个时效都能匹配。"""
        from src.utils.weather_fetcher import _parse_gfs_idx

        idx_text = (
            "1:0:d=2026032900:TMP:2 m above ground:anl:\n"
            "2:50000:d=2026032900:RH:2 m above ground:anl:\n"
            "3:100000:d=2026032900:TMP:2 m above ground:3 hour fcst:\n"
            "4:200000:d=2026032900:UGRD:10 m above ground:anl:\n"
        )
        ranges = _parse_gfs_idx(idx_text, "TMP", "2 m above ground")
        assert len(ranges) == 2
        assert ranges[0] == (0, 50000 - 1)
        assert ranges[1] == (100000, 200000 - 1)


class TestDecodeGrib2Temperature:
    """T274: 纯 Python GRIB2 Simple Packing 解码器测试。"""

    def _build_grib2_message(
        self,
        ref_val: float = 270.0,
        bin_scale: int = 0,
        dec_scale: int = 0,
        bits_per_val: int = 16,
        ni: int = 1440,
        nj: int = 721,
        values: Optional[list] = None,
    ) -> bytes:
        """构造最小可解析的 GRIB2 消息 (Template 3.0 + 5.0)。"""
        import struct

        # Section 0 (Indicator) — 16 bytes
        section0 = b"GRIB" + b"\x00\x00" + b"\x00" + b"\x02"  # edition 2
        # total_length placeholder (will fill after)
        section0 += b"\x00" * 8

        # Section 1 (Identification) — minimal 21 bytes
        sect1_len = 21
        section1 = struct.pack(">I", sect1_len) + b"\x01"
        section1 += b"\x00" * (sect1_len - 5)

        # Section 3 (Grid Definition) — Template 3.0 (lat/lon), 72 bytes
        sect3_len = 72
        section3 = struct.pack(">I", sect3_len) + b"\x03"
        section3 += b"\x00" * 5  # source, num_data_points (dummy)
        section3 += b"\x00" * 2  # template 3.0
        section3 += struct.pack(">H", 0)  # template number = 0
        # pad to offset 30 (already at 14, need 16 more)
        section3 += b"\x00" * (30 - len(section3) + 4)  # +4 for length field
        # actually let me rebuild more carefully
        section3 = bytearray(sect3_len)
        struct.pack_into(">I", section3, 0, sect3_len)
        section3[4] = 3  # section number
        struct.pack_into(">H", section3, 12, 0)  # template 3.0
        struct.pack_into(">I", section3, 30, ni)
        struct.pack_into(">I", section3, 34, nj)
        struct.pack_into(">i", section3, 46, 90_000_000)  # lat_first = 90°
        struct.pack_into(">i", section3, 50, 0)  # lon_first = 0°
        struct.pack_into(">i", section3, 55, -90_000_000)  # lat_last = -90°
        struct.pack_into(">i", section3, 59, 359_750_000)  # lon_last = 359.75°
        struct.pack_into(">I", section3, 63, 250_000)  # di = 0.25°
        struct.pack_into(">I", section3, 67, 250_000)  # dj = 0.25°
        section3 = bytes(section3)

        # Section 5 (Data Representation) — Template 5.0, 21 bytes
        num_points = ni * nj
        sect5_len = 21
        section5 = bytearray(sect5_len)
        struct.pack_into(">I", section5, 0, sect5_len)
        section5[4] = 5  # section number
        struct.pack_into(">I", section5, 5, num_points)
        struct.pack_into(">H", section5, 9, 0)  # template 5.0
        struct.pack_into(">f", section5, 11, ref_val)
        struct.pack_into(">h", section5, 15, bin_scale)
        struct.pack_into(">h", section5, 17, dec_scale)
        section5[19] = bits_per_val
        section5 = bytes(section5)

        # Section 7 (Data Section) — packed binary values
        if values is None:
            # fill num_points with incrementing values
            total_bits = num_points * bits_per_val
            total_bytes = (total_bits + 7) // 8
            # Just zero-fill for simplicity (decoded = ref_val)
            data_bytes = b"\x00" * total_bytes
        else:
            # Pack provided values
            bit_buffer = 0
            bit_count = 0
            data_bytes = bytearray()
            for v in values:
                bit_buffer = (bit_buffer << bits_per_val) | (
                    v & ((1 << bits_per_val) - 1)
                )
                bit_count += bits_per_val
                while bit_count >= 8:
                    bit_count -= 8
                    data_bytes.append((bit_buffer >> bit_count) & 0xFF)
            if bit_count > 0:
                data_bytes.append((bit_buffer << (8 - bit_count)) & 0xFF)
            # Pad to cover all points
            remaining_points = num_points - len(values)
            if remaining_points > 0:
                remaining_bits = remaining_points * bits_per_val
                remaining_bytes = (remaining_bits + 7) // 8
                data_bytes += b"\x00" * remaining_bytes
            data_bytes = bytes(data_bytes)

        sect7_len = 5 + len(data_bytes)
        section7 = struct.pack(">I", sect7_len) + b"\x07" + data_bytes

        # Section 8 (End)
        section8 = b"7777"

        # Assemble
        body = section1 + section3 + section5 + section7 + section8
        total_len = 16 + len(body)
        section0_final = (
            b"GRIB" + b"\x00\x00" + b"\x00" + b"\x02" + struct.pack(">Q", total_len)
        )

        return section0_final + body

    def test_decode_returns_celsius(self):
        """零值 packed → 返回 ref_val(Kelvin) - 273.15 = Celsius。"""
        from src.utils.weather_fetcher import _decode_grib2_temperature

        # ref_val = 300K, all packed zeros → decoded = 300K → 26.85°C
        msg = self._build_grib2_message(ref_val=300.0, bits_per_val=16)
        temp = _decode_grib2_temperature(msg, 0.0, 0.0)
        assert temp is not None
        assert abs(temp - 26.85) < 0.1

    def test_decode_rejects_non_grib(self):
        """非 GRIB2 数据返回 None。"""
        from src.utils.weather_fetcher import _decode_grib2_temperature

        assert _decode_grib2_temperature(b"NOT_GRIB_DATA", 0.0, 0.0) is None
        assert _decode_grib2_temperature(b"", 0.0, 0.0) is None

    def test_decode_rejects_grib1(self):
        """GRIB1 (edition != 2) 返回 None。"""
        from src.utils.weather_fetcher import _decode_grib2_temperature

        data = b"GRIB" + b"\x00\x00" + b"\x00" + b"\x01" + b"\x00" * 100
        assert _decode_grib2_temperature(data, 0.0, 0.0) is None

    def test_decode_with_nonzero_packed_values(self):
        """非零 packed values 通过 Simple Packing 公式正确解码。"""
        from src.utils.weather_fetcher import _decode_grib2_temperature

        # ref_val=273.15K, bin_scale=0, dec_scale=0, bits=16
        # packed value X=100 → decoded = 273.15 + 100 × 2^0 / 10^0 = 373.15K → 100°C
        # This exceeds reasonable range → should return None
        # Use X=20 → 273.15 + 20 = 293.15K → 20.0°C
        ni, nj = 4, 4
        # Point (0,0) = index 0 → first value
        values = [20]  # first grid point packed value = 20
        msg = self._build_grib2_message(
            ref_val=273.15,
            bin_scale=0,
            dec_scale=0,
            bits_per_val=16,
            ni=ni,
            nj=nj,
            values=values,
        )
        # target (90°, 0°) = grid point (0,0)
        temp = _decode_grib2_temperature(msg, 90.0, 0.0)
        assert temp is not None
        assert abs(temp - 20.0) < 0.1


class TestAwsGfsS3Fallback:
    """T274: AWS S3 GFS GRIB2 降级测试。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._AWS_GFS_CIRCUIT_OPEN_UNTIL = 0.0

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_aws_gfs_circuit_open_skips(self, mock_req_get, mock_cfg_get):
        """AWS GFS 熔断开启时不发请求。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_aws_gfs_s3

        mock_cfg_get.return_value = {"api_timeout": 10}
        wf._AWS_GFS_CIRCUIT_OPEN_UNTIL = time.time() + 120.0

        result = _fetch_aws_gfs_s3(35.68, 139.69, "temp_max", 3)
        assert result is None
        mock_req_get.assert_not_called()

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_aws_gfs_429_opens_circuit(self, mock_req_get, mock_cfg_get):
        """AWS S3 429响应 → 熔断器开启120s。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_aws_gfs_s3

        mock_cfg_get.return_value = {"api_timeout": 10}

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.ok = False
        mock_req_get.return_value = resp_429

        result = _fetch_aws_gfs_s3(35.68, 139.69, "temp_max", 1)
        assert result is None
        assert wf._AWS_GFS_CIRCUIT_OPEN_UNTIL > time.time()

    @patch("src.utils.weather_fetcher._decode_grib2_temperature")
    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_aws_gfs_idx_range_request_flow(
        self, mock_req_get, mock_cfg_get, mock_decode
    ):
        """完整流程: .idx下载 → 解析 → Range Request → GRIB2解码 → 返回温度。"""
        from src.utils.weather_fetcher import _fetch_aws_gfs_s3

        mock_cfg_get.return_value = {"api_timeout": 10}

        # .idx response
        idx_body = (
            "1:0:d=2026032900:PRMSL:mean sea level:anl:\n"
            "2:45678:d=2026032900:TMP:2 m above ground:anl:\n"
            "3:89012:d=2026032900:RH:2 m above ground:anl:\n"
        )
        resp_idx = MagicMock()
        resp_idx.status_code = 200
        resp_idx.ok = True
        resp_idx.text = idx_body

        # GRIB2 range response
        resp_grib = MagicMock()
        resp_grib.status_code = 206
        resp_grib.content = b"FAKE_GRIB2_DATA"

        mock_req_get.side_effect = [resp_idx, resp_grib] * 20  # 多个时效
        mock_decode.return_value = 32.5  # 模拟解码结果

        result = _fetch_aws_gfs_s3(35.68, 139.69, "temp_max", 1)
        assert result is not None
        # 至少有1天的数据
        assert len(result) >= 1
        for date_str, vals in result.items():
            assert len(vals) == 1
            assert 30.0 < vals[0] < 35.0

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_aws_gfs_404_tries_previous_run(self, mock_req_get, mock_cfg_get):
        """AWS S3 404 → 尝试前一个GFS运行时次。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_aws_gfs_s3

        mock_cfg_get.return_value = {"api_timeout": 10}

        resp_404 = MagicMock()
        resp_404.status_code = 404
        resp_404.ok = False

        # 后续也 404 → 返回 None (不崩溃)
        mock_req_get.return_value = resp_404

        result = _fetch_aws_gfs_s3(35.68, 139.69, "temp_max", 1)
        assert result is None
        # 应该发了至少2个请求 (当前时次 + 前一时次)
        assert mock_req_get.call_count >= 2


class TestParseEcmwfIndex:
    """T274: ECMWF .index JSON 索引解析测试。"""

    def test_parse_ecmwf_index_finds_2t(self):
        """能解析 ECMWF .index 找到 2t (2m temp) 的偏移和长度。"""
        from src.utils.weather_fetcher import _parse_ecmwf_index

        index_text = (
            '{"param":"10u","levtype":"sfc","step":"24","_offset":0,"_length":5000}\n'
            '{"param":"2t","levtype":"sfc","step":"24","_offset":5000,"_length":8000}\n'
            '{"param":"2t","levtype":"sfc","step":"48","_offset":13000,"_length":8000}\n'
        )
        result = _parse_ecmwf_index(index_text, "2t", 24)
        assert result is not None
        assert result == (5000, 8000)

    def test_parse_ecmwf_index_no_match(self):
        """不匹配的参数返回 None。"""
        from src.utils.weather_fetcher import _parse_ecmwf_index

        index_text = (
            '{"param":"10u","levtype":"sfc","step":"24","_offset":0,"_length":5000}\n'
        )
        result = _parse_ecmwf_index(index_text, "2t", 24)
        assert result is None

    def test_parse_ecmwf_index_step_mismatch(self):
        """时效不匹配返回 None。"""
        from src.utils.weather_fetcher import _parse_ecmwf_index

        index_text = (
            '{"param":"2t","levtype":"sfc","step":"48","_offset":5000,"_length":8000}\n'
        )
        result = _parse_ecmwf_index(index_text, "2t", 24)
        assert result is None

    def test_parse_ecmwf_index_empty(self):
        """空内容返回 None。"""
        from src.utils.weather_fetcher import _parse_ecmwf_index

        assert _parse_ecmwf_index("", "2t", 24) is None

    def test_parse_ecmwf_index_invalid_json(self):
        """无效 JSON 行不崩溃。"""
        from src.utils.weather_fetcher import _parse_ecmwf_index

        index_text = "NOT_JSON\n{broken\n"
        assert _parse_ecmwf_index(index_text, "2t", 24) is None


class TestEcmwfCloudMirror:
    """T274: ECMWF 云端镜像 (AWS/Azure/Google) 降级测试。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL = 0.0

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_ecmwf_mirror_circuit_open_skips(self, mock_req_get, mock_cfg_get):
        """ECMWF Mirror 熔断开启时不发请求。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_ecmwf_cloud_mirror

        mock_cfg_get.return_value = {"api_timeout": 10}
        wf._ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL = time.time() + 120.0

        result = _fetch_ecmwf_cloud_mirror(35.68, 139.69, "temp_max", 3)
        assert result is None
        mock_req_get.assert_not_called()

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_ecmwf_mirror_429_opens_circuit(self, mock_req_get, mock_cfg_get):
        """所有云平台 429 → 熔断器开启。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _fetch_ecmwf_cloud_mirror

        mock_cfg_get.return_value = {"api_timeout": 10}

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.ok = False
        mock_req_get.return_value = resp_429

        result = _fetch_ecmwf_cloud_mirror(35.68, 139.69, "temp_max", 1)
        assert result is None
        assert wf._ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL > time.time()

    @patch("src.utils.weather_fetcher._decode_grib2_temperature")
    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_ecmwf_mirror_aws_success(self, mock_req_get, mock_cfg_get, mock_decode):
        """AWS 第一个镜像成功 → 返回温度数据。"""
        from src.utils.weather_fetcher import _fetch_ecmwf_cloud_mirror

        mock_cfg_get.return_value = {"api_timeout": 10}

        # .index response
        index_body = (
            '{"param":"2t","levtype":"sfc","step":"0","_offset":100,"_length":5000}\n'
            '{"param":"2t","levtype":"sfc","step":"6","_offset":5100,"_length":5000}\n'
            '{"param":"2t","levtype":"sfc","step":"12","_offset":10100,"_length":5000}\n'
            '{"param":"2t","levtype":"sfc","step":"18","_offset":15100,"_length":5000}\n'
        )
        resp_idx = MagicMock()
        resp_idx.status_code = 200
        resp_idx.ok = True
        resp_idx.text = index_body

        resp_grib = MagicMock()
        resp_grib.status_code = 206
        resp_grib.content = b"FAKE_ECMWF_GRIB2"

        mock_req_get.side_effect = [resp_idx, resp_grib] * 20
        mock_decode.return_value = 28.3

        result = _fetch_ecmwf_cloud_mirror(35.68, 139.69, "temp_max", 1)
        assert result is not None
        assert len(result) >= 1
        for vals in result.values():
            assert len(vals) == 1
            assert 25.0 < vals[0] < 32.0

    @patch("src.utils.weather_fetcher._decode_grib2_temperature")
    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_ecmwf_mirror_failover_aws_to_azure(
        self, mock_req_get, mock_cfg_get, mock_decode
    ):
        """AWS 失败 → 自动切换到 Azure 镜像。"""
        from src.utils.weather_fetcher import _fetch_ecmwf_cloud_mirror

        mock_cfg_get.return_value = {"api_timeout": 10}

        resp_fail = MagicMock()
        resp_fail.status_code = 503
        resp_fail.ok = False

        index_body = (
            '{"param":"2t","levtype":"sfc","step":"6","_offset":100,"_length":5000}\n'
        )
        resp_idx = MagicMock()
        resp_idx.status_code = 200
        resp_idx.ok = True
        resp_idx.text = index_body

        resp_grib = MagicMock()
        resp_grib.status_code = 206
        resp_grib.content = b"AZURE_GRIB2"

        # AWS fail, Azure .index OK, Azure GRIB2 OK
        mock_req_get.side_effect = [
            resp_fail,
            resp_idx,
            resp_grib,  # step 6
            resp_fail,
            resp_idx,
            resp_grib,  # step 12
            resp_fail,
            resp_idx,
            resp_grib,  # step 18
        ]
        mock_decode.return_value = 25.0

        result = _fetch_ecmwf_cloud_mirror(35.68, 139.69, "temp_max", 1)
        assert result is not None


class TestGfsMultiFallbackT274:
    """T274: GFS 降级链顺序测试 — AWS S3 在 NOMADS 之前。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._AWS_GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._NOMADS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._DATA_SOURCE_META.clear()
        wf._CACHE.clear()

    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback")
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs")
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3")
    def test_aws_s3_is_first_tier(self, mock_aws, mock_nomads, mock_det):
        """AWS S3 是降级链第一层 (T2), 成功后不调 NOMADS。"""
        from src.utils.weather_fetcher import _gfs_multi_fallback

        mock_aws.return_value = {"2026-03-29": [33.0]}
        mock_nomads.return_value = {"2026-03-29": [32.0]}

        result = _gfs_multi_fallback(35.68, 139.69, "temp_max", 3, "test_key")
        assert result is not None
        assert result == {"2026-03-29": [33.0]}
        mock_aws.assert_called_once()
        mock_nomads.assert_not_called()
        mock_det.assert_not_called()

    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback")
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs")
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3")
    def test_aws_fail_falls_to_nomads(self, mock_aws, mock_nomads, mock_det):
        """AWS S3 失败 → 降级到 NOMADS (T3)。"""
        from src.utils.weather_fetcher import _gfs_multi_fallback

        mock_aws.return_value = None  # AWS fails
        mock_nomads.return_value = {"2026-03-29": [31.5]}

        result = _gfs_multi_fallback(35.68, 139.69, "temp_max", 3, "test_key")
        assert result is not None
        assert result == {"2026-03-29": [31.5]}
        mock_aws.assert_called_once()
        mock_nomads.assert_called_once()

    @patch("src.utils.weather_fetcher._get_stale_cache")
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback")
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs")
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3")
    def test_all_cloud_fail_uses_stale_cache(
        self, mock_aws, mock_nomads, mock_det, mock_stale
    ):
        """所有云端源失败 → 使用 stale cache (T5)。"""
        from src.utils.weather_fetcher import _gfs_multi_fallback

        mock_aws.return_value = None
        mock_nomads.return_value = None
        mock_stale.return_value = {"2026-03-29": [29.0, 30.0, 31.0]}
        mock_det.return_value = None  # not reached

        result = _gfs_multi_fallback(35.68, 139.69, "temp_max", 3, "test_key")
        assert result is not None


class TestEcmwfMultiFallbackT274:
    """T274: ECMWF 降级链顺序测试 — 云端镜像在 stale cache 之前。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL = 0.0
        wf._DATA_SOURCE_META.clear()
        wf._CACHE.clear()

    @patch("src.utils.weather_fetcher._get_stale_cache")
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback")
    @patch("src.utils.weather_fetcher._fetch_ecmwf_cloud_mirror")
    def test_ecmwf_mirror_is_first_tier(self, mock_mirror, mock_det, mock_stale):
        """ECMWF 云端镜像是降级链第一层 (T2)。"""
        from src.utils.weather_fetcher import _ecmwf_multi_fallback

        mock_mirror.return_value = {"2026-03-29": [28.0]}

        result = _ecmwf_multi_fallback(35.68, 139.69, "temp_max", 3, "ecmwf_key")
        assert result is not None
        assert result == {"2026-03-29": [28.0]}
        mock_mirror.assert_called_once()
        mock_stale.assert_not_called()
        mock_det.assert_not_called()

    @patch("src.utils.weather_fetcher._get_stale_cache")
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback")
    @patch("src.utils.weather_fetcher._fetch_ecmwf_cloud_mirror")
    def test_ecmwf_mirror_fail_falls_to_stale(self, mock_mirror, mock_det, mock_stale):
        """ECMWF 镜像失败 → 降级到 stale cache (T3)。"""
        import src.utils.weather_fetcher as wf
        from src.utils.weather_fetcher import _ecmwf_multi_fallback

        mock_mirror.return_value = None
        wf._CACHE["ecmwf_key"] = (time.time() - 3600, {"2026-03-29": [27.0, 28.0]})
        mock_stale.return_value = {"2026-03-29": [27.0, 28.0]}

        result = _ecmwf_multi_fallback(35.68, 139.69, "temp_max", 3, "ecmwf_key")
        assert result is not None
        mock_mirror.assert_called_once()


class TestSourceConfidenceMultiplierT274:
    """T274: 新数据源的置信度乘数测试。"""

    def test_aws_gfs_s3_multiplier_exists(self):
        """aws_gfs_s3 源的置信度乘数已注册。"""
        from src.utils.weather_fetcher import _SOURCE_CONFIDENCE_MULTIPLIER

        assert "aws_gfs_s3" in _SOURCE_CONFIDENCE_MULTIPLIER
        assert 0.5 < _SOURCE_CONFIDENCE_MULTIPLIER["aws_gfs_s3"] < 1.0

    def test_ecmwf_mirror_multiplier_exists(self):
        """ecmwf_mirror 源的置信度乘数已注册。"""
        from src.utils.weather_fetcher import _SOURCE_CONFIDENCE_MULTIPLIER

        assert "ecmwf_mirror" in _SOURCE_CONFIDENCE_MULTIPLIER
        assert 0.5 < _SOURCE_CONFIDENCE_MULTIPLIER["ecmwf_mirror"] < 1.0

    def test_aws_gfs_higher_than_nomads(self):
        """AWS S3 直接GRIB2的置信度应高于 NOMADS OPeNDAP。"""
        from src.utils.weather_fetcher import _SOURCE_CONFIDENCE_MULTIPLIER

        assert (
            _SOURCE_CONFIDENCE_MULTIPLIER["aws_gfs_s3"]
            > _SOURCE_CONFIDENCE_MULTIPLIER["noaa_nomads"]
        )

    def test_cloud_mirrors_higher_than_det_fallback(self):
        """云端镜像(真实NWP数据)的置信度应高于 Open-Meteo 确定性降级。"""
        from src.utils.weather_fetcher import _SOURCE_CONFIDENCE_MULTIPLIER

        assert (
            _SOURCE_CONFIDENCE_MULTIPLIER["aws_gfs_s3"]
            > _SOURCE_CONFIDENCE_MULTIPLIER["det_fallback"]
        )
        assert (
            _SOURCE_CONFIDENCE_MULTIPLIER["ecmwf_mirror"]
            > _SOURCE_CONFIDENCE_MULTIPLIER["det_fallback"]
        )


class TestMainStation429FallbackToCloudMirror:
    """T274 用户必验: 主站429后，系统自动降级到 AWS S3 / ECMWF 云端镜像。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._AWS_GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL = 0.0
        wf._NOMADS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ADAPTIVE_INTERVAL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0
        wf._DATA_SOURCE_META.clear()
        wf._CACHE.clear()

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3")
    def test_gfs_main_429_aws_s3_saves(
        self, mock_aws, mock_nomads, mock_det, mock_stale
    ):
        """GFS 主站429后, AWS S3 GRIB2 自动接管, 数据不丢失。"""
        from src.utils.weather_fetcher import _DATA_SOURCE_META, _gfs_multi_fallback

        mock_aws.return_value = {"2026-03-29": [25.3]}

        result = _gfs_multi_fallback(40.0, -74.0, "temp_max", 3, "gfs_429_key")
        assert result is not None
        assert "2026-03-29" in result
        assert result["2026-03-29"] == [25.3]
        mock_aws.assert_called_once()
        assert _DATA_SOURCE_META.get("gfs_429_key", {}).get("source") == "aws_gfs_s3"

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_ecmwf_cloud_mirror")
    def test_ecmwf_main_429_cloud_mirror_saves(self, mock_mirror, mock_det, mock_stale):
        """ECMWF 主站429后, AWS/Azure/Google 云端镜像自动接管。"""
        from src.utils.weather_fetcher import _DATA_SOURCE_META, _ecmwf_multi_fallback

        mock_mirror.return_value = {"2026-03-29": [24.8]}

        result = _ecmwf_multi_fallback(40.0, -74.0, "temp_max", 3, "ecmwf_429_key")
        assert result is not None
        assert result["2026-03-29"] == [24.8]
        mock_mirror.assert_called_once()
        assert (
            _DATA_SOURCE_META.get("ecmwf_429_key", {}).get("source") == "ecmwf_mirror"
        )


class TestBatchFailureAllSourcesDown:
    """T274 用户必验: 所有数据源全部失败的极端场景。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._AWS_GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._NOMADS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL = 0.0
        wf._DATA_SOURCE_META.clear()
        wf._CACHE.clear()

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3", return_value=None)
    def test_gfs_all_7_tiers_fail_returns_emergency_climate(
        self, mock_aws, mock_nomads, mock_det, mock_stale
    ):
        """GFS 全部6层降级链失败 → T7 紧急气候学估计 (never-None)。"""
        from src.utils.weather_fetcher import _gfs_multi_fallback

        result = _gfs_multi_fallback(40.0, -74.0, "temp_max", 3, "total_fail")
        # T275: never-None 策略 — 紧急气候学估计
        assert result is not None
        mock_aws.assert_called_once()
        mock_nomads.assert_called_once()

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_ecmwf_cloud_mirror", return_value=None)
    def test_ecmwf_all_tiers_fail_returns_emergency_climate(
        self, mock_mirror, mock_det, mock_stale
    ):
        """ECMWF 全部降级链失败 → T6 紧急气候学估计 (never-None)。"""
        from src.utils.weather_fetcher import _ecmwf_multi_fallback

        result = _ecmwf_multi_fallback(40.0, -74.0, "temp_max", 3, "ecmwf_fail")
        # T275: never-None 策略 — 紧急气候学估计
        assert result is not None
        mock_mirror.assert_called_once()

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3", return_value=None)
    def test_gfs_batch_fail_all_called_in_order(
        self, mock_aws, mock_nomads, mock_det, mock_stale
    ):
        """批量失败时, 各层按 T2→T3→T4→T5→T6 顺序逐层尝试。"""
        from src.utils.weather_fetcher import _gfs_multi_fallback

        _gfs_multi_fallback(40.0, -74.0, "temp_max", 3, "order_check")
        # 所有层都被调用
        mock_aws.assert_called_once()
        mock_nomads.assert_called_once()
        mock_det.assert_called_once()
        assert mock_stale.call_count >= 2  # T5 + T7 两次 stale 调用


class TestColdStartNoCache:
    """T274 用户必验: 进程重启后无缓存, 云端镜像必须能独立工作。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        # 模拟冷启动: 清空所有缓存和熔断器
        wf._CACHE.clear()
        wf._DATA_SOURCE_META.clear()
        wf._AWS_GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._NOMADS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL = 0.0

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3")
    def test_cold_start_aws_s3_provides_data(
        self, mock_aws, mock_nomads, mock_det, mock_stale
    ):
        """冷启动无缓存: AWS S3 GFS 独立提供数据, 不依赖 stale cache。"""
        from src.utils.weather_fetcher import _gfs_multi_fallback

        mock_aws.return_value = {"2026-03-29": [22.0]}
        result = _gfs_multi_fallback(35.0, 139.0, "temp_max", 3, "cold_gfs")
        assert result is not None
        assert result["2026-03-29"] == [22.0]
        # stale cache 不应被调用 (因为 AWS 已成功)
        mock_stale.assert_not_called()

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_ecmwf_cloud_mirror")
    def test_cold_start_ecmwf_mirror_provides_data(
        self, mock_mirror, mock_det, mock_stale
    ):
        """冷启动无缓存: ECMWF 云端镜像独立工作。"""
        from src.utils.weather_fetcher import _ecmwf_multi_fallback

        mock_mirror.return_value = {"2026-03-29": [21.5]}
        result = _ecmwf_multi_fallback(35.0, 139.0, "temp_max", 3, "cold_ecmwf")
        assert result is not None
        assert result["2026-03-29"] == [21.5]
        mock_stale.assert_not_called()

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback")
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs")
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3", return_value=None)
    def test_cold_start_aws_fail_nomads_saves(
        self, mock_aws, mock_nomads, mock_det, mock_stale
    ):
        """冷启动: AWS失败 → NOMADS 仍然接管 (不跳过)。"""
        from src.utils.weather_fetcher import _gfs_multi_fallback

        mock_nomads.return_value = {"2026-03-29": [23.0]}
        result = _gfs_multi_fallback(35.0, 139.0, "temp_max", 3, "cold_nomads")
        assert result is not None
        assert result["2026-03-29"] == [23.0]


class TestT192SkipPrevention:
    """T274 用户必验: 云端镜像接入后, T192 大量跳过问题不再出现。

    T192 跳过 = 所有 GFS 数据源失败 → _gfs_multi_fallback 返回 None
    → PaperTrader 跳过该天气市场。
    云端镜像 (AWS/Azure/Google) 让链比以前多2层降级, 极大降低 T192 概率。
    """

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._CACHE.clear()
        wf._DATA_SOURCE_META.clear()
        wf._AWS_GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._NOMADS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL = 0.0

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3")
    def test_main_429_aws_prevents_t192(
        self, mock_aws, mock_nomads, mock_det, mock_stale
    ):
        """主站429 + NOMADS/stale/det全挂 → AWS S3 仍然防止 T192 跳过。"""
        from src.utils.weather_fetcher import _gfs_multi_fallback

        mock_aws.return_value = {"2026-03-29": [26.0]}
        result = _gfs_multi_fallback(40.0, -74.0, "temp_max", 3, "t192_prevent")
        assert result is not None, "T192 跳过不应发生: AWS S3 应提供数据"

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback")
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3", return_value=None)
    def test_main_and_aws_nomads_fail_det_prevents_t192(
        self, mock_aws, mock_nomads, mock_det, mock_stale
    ):
        """主站+AWS+NOMADS全挂 → det_fallback 仍然防止 T192 跳过。"""
        from src.utils.weather_fetcher import _gfs_multi_fallback

        mock_det.return_value = {"2026-03-29": [25.0]}
        result = _gfs_multi_fallback(40.0, -74.0, "temp_max", 3, "t192_det2")
        assert result is not None, "T192 跳过不应发生: det_fallback 应兜底"

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback")
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3", return_value=None)
    def test_all_cloud_fail_det_prevents_t192(
        self, mock_aws, mock_nomads, mock_det, mock_stale
    ):
        """所有云端源失败 → 确定性降级 (T6) 作为最后防线阻止 T192。"""
        from src.utils.weather_fetcher import _gfs_multi_fallback

        mock_det.return_value = {"2026-03-29": [25.0]}
        result = _gfs_multi_fallback(40.0, -74.0, "temp_max", 3, "t192_det")
        assert result is not None, "T192 跳过不应发生: det_fallback 应兜底"

    def test_t274_chain_has_6_tiers(self):
        """T274 降级链有6层, T192概率极低。"""
        from src.utils.weather_fetcher import _SOURCE_CONFIDENCE_MULTIPLIER

        required_sources = [
            "ensemble",
            "aws_gfs_s3",
            "noaa_nomads",
            "stale_fresh",
            "det_fallback",
            "stale_old",
        ]
        for src in required_sources:
            assert src in _SOURCE_CONFIDENCE_MULTIPLIER, f"{src} 缺失, T192风险增加"


# ══════════════════════════════════════════════════════════════════════
# T275: 熔断器完全分离 + IP级429短路 + emergency_climate 测试
# ══════════════════════════════════════════════════════════════════════


class TestCircuitBreakerIsolation:
    """T275: batch 429 绝不污染 individual 请求路径。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ADAPTIVE_INTERVAL = 3.0
        wf._OPEN_METEO_LAST_429_TS = 0.0
        wf._GFS_CONSEC_FAILURES = 0
        wf._ECMWF_CONSEC_FAILURES = 0

    def test_batch_429_does_not_inflate_adaptive_interval(self):
        """batch 429 不膨胀共享 _ADAPTIVE_INTERVAL。"""
        import src.utils.weather_fetcher as wf

        wf._ADAPTIVE_INTERVAL = 3.0
        original = wf._ADAPTIVE_INTERVAL
        wf._on_api_429("batch")
        # batch 429 不应改变 interval
        assert (
            wf._ADAPTIVE_INTERVAL == original
        ), f"batch 429 不应膨胀 interval: {wf._ADAPTIVE_INTERVAL} != {original}"

    def test_gfs_429_inflates_adaptive_interval(self):
        """individual gfs 429 正常膨胀 _ADAPTIVE_INTERVAL。"""
        import src.utils.weather_fetcher as wf

        wf._ADAPTIVE_INTERVAL = 3.0
        wf._on_api_429("gfs")
        assert wf._ADAPTIVE_INTERVAL == 6.0, "gfs 429 应加倍 interval"

    def test_ecmwf_429_inflates_adaptive_interval(self):
        """individual ecmwf 429 正常膨胀 _ADAPTIVE_INTERVAL。"""
        import src.utils.weather_fetcher as wf

        wf._ADAPTIVE_INTERVAL = 3.0
        wf._on_api_429("ecmwf")
        assert wf._ADAPTIVE_INTERVAL == 6.0, "ecmwf 429 应加倍 interval"

    def test_batch_429_sets_ip_level_timestamp(self):
        """batch 429 记录 _OPEN_METEO_LAST_429_TS。"""
        import src.utils.weather_fetcher as wf

        wf._OPEN_METEO_LAST_429_TS = 0.0
        wf._on_api_429("batch")
        assert wf._OPEN_METEO_LAST_429_TS > 0, "batch 429 应记录 IP 级时间戳"

    def test_batch_429_opens_only_batch_circuit(self):
        """batch 429 只开 batch 熔断器，不影响 gfs/ecmwf。"""
        import src.utils.weather_fetcher as wf

        wf._on_api_429("batch")
        assert wf._is_circuit_open("batch"), "batch 熔断器应开启"
        assert not wf._is_circuit_open("gfs"), "gfs 熔断器不应被 batch 污染"
        assert not wf._is_circuit_open("ecmwf"), "ecmwf 熔断器不应被 batch 污染"

    def test_gfs_429_opens_only_gfs_circuit(self):
        """gfs 429 只开 gfs 熔断器。"""
        import src.utils.weather_fetcher as wf

        wf._on_api_429("gfs")
        assert wf._is_circuit_open("gfs"), "gfs 熔断器应开启"
        assert not wf._is_circuit_open("ecmwf"), "ecmwf 不应被污染"
        assert not wf._is_circuit_open("batch"), "batch 不应被污染"


class TestIPLevel429ShortCircuit:
    """T275: Open-Meteo IP级429短路 — 跳过主站直接走云端镜像。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._CACHE.clear()
        wf._DATA_SOURCE_META.clear()
        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0
        wf._OPEN_METEO_LAST_429_TS = 0.0
        wf._ADAPTIVE_INTERVAL = 3.0
        wf._AWS_GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._NOMADS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL = 0.0

    @patch("src.utils.weather_fetcher._gfs_multi_fallback")
    def test_gfs_skips_main_after_recent_429(self, mock_fallback):
        """Open-Meteo 刚429 → GFS跳过主站直接走降级链。"""
        import src.utils.weather_fetcher as wf

        wf._OPEN_METEO_LAST_429_TS = time.time()  # 刚刚429
        mock_fallback.return_value = {"2026-03-29": [25.0]}

        result = wf._fetch_gfs_ensemble(40.0, -74.0, "temp_max", 3)
        mock_fallback.assert_called_once()
        assert result is not None

    @patch("src.utils.weather_fetcher._gfs_multi_fallback")
    def test_gfs_uses_main_after_429_window_expires(self, mock_fallback):
        """429窗口过期后 → GFS正常走主站。"""
        import src.utils.weather_fetcher as wf

        wf._OPEN_METEO_LAST_429_TS = time.time() - 200  # 200秒前(超过90s窗口)

        # 主站成功 → 不应走降级链
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "daily": {
                "time": ["2026-03-29"],
                **{
                    f"temperature_2m_max_member{i:02d}": [25.0 + i * 0.1]
                    for i in range(1, 32)
                },
            }
        }
        with patch("requests.get", return_value=mock_resp):
            result = wf._fetch_gfs_ensemble(40.0, -74.0, "temp_max", 3)
        mock_fallback.assert_not_called()

    @patch("src.utils.weather_fetcher._ecmwf_multi_fallback")
    def test_ecmwf_skips_main_after_recent_429(self, mock_fallback):
        """Open-Meteo 刚429 → ECMWF也跳过主站。"""
        import src.utils.weather_fetcher as wf

        wf._OPEN_METEO_LAST_429_TS = time.time()
        mock_fallback.return_value = {"2026-03-29": [24.0]}

        result = wf._fetch_ecmwf_ensemble(40.0, -74.0, "temp_max", 3)
        mock_fallback.assert_called_once()
        assert result is not None

    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3")
    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs", return_value=None)
    def test_batch_429_then_individual_goes_to_aws(
        self, mock_nomads, mock_det, mock_stale, mock_aws
    ):
        """batch 429 → individual GFS 跳过主站 → 直接走 AWS S3 (T2)。"""
        import src.utils.weather_fetcher as wf

        # 模拟 batch 429
        wf._on_api_429("batch")

        # AWS S3 返回数据
        mock_aws.return_value = {"2026-03-29": [26.0]}

        result = wf._fetch_gfs_ensemble(40.0, -74.0, "temp_max", 3)
        assert result is not None, "batch 429后, 个体GFS应通过AWS S3获取数据"
        mock_aws.assert_called_once()
        # 验证 adaptive interval 未被 batch 429 膨胀
        assert wf._ADAPTIVE_INTERVAL == 3.0, "batch 429 不应膨胀 individual interval"


class TestDeterministicFallback429Recording:
    """T275: api.open-meteo.com 429 也记录 IP 级时间戳。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._GFS_DET_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_DET_CIRCUIT_OPEN_UNTIL = 0.0
        wf._OPEN_METEO_LAST_429_TS = 0.0

    def test_det_fallback_429_sets_ip_timestamp(self):
        """api.open-meteo.com 429 → 记录 _OPEN_METEO_LAST_429_TS。"""
        import src.utils.weather_fetcher as wf

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        with patch("requests.get", return_value=mock_resp):
            result = wf._fetch_deterministic_fallback(
                40.0, -74.0, "gfs_seamless", "temp_max", 3, "GFS"
            )
        assert result is None
        assert wf._OPEN_METEO_LAST_429_TS > 0, "det 429 应记录 IP 级时间戳"


class TestEmergencyClimateEstimate:
    """T275: 紧急气候学估计 — 绝对最后手段, confidence ≤ 0.12。"""

    def test_never_returns_none(self):
        """emergency_climate_estimate 在任何输入下都不返回 None。"""
        from src.utils.weather_fetcher import _emergency_climate_estimate

        for lat in [-60, -30, 0, 15, 40, 65]:
            for var in ["temp_max", "temp_min"]:
                result = _emergency_climate_estimate(lat, var, 5)
                assert result is not None, f"lat={lat} var={var} 不应返回 None"
                assert len(result) > 0, f"lat={lat} var={var} 不应返回空字典"

    def test_returns_valid_temps(self):
        """返回的温度应在合理物理范围内 (-40 ~ +55°C)。"""
        from src.utils.weather_fetcher import _emergency_climate_estimate

        for lat in [-60, 0, 45, 70]:
            result = _emergency_climate_estimate(lat, "temp_max", 7)
            for date_str, vals in result.items():
                for v in vals:
                    assert -40 <= v <= 55, f"温度{v}°C超出物理范围 (lat={lat})"

    def test_confidence_multiplier_is_012(self):
        """emergency_climate 置信度乘数 = 0.12 (最低)。"""
        from src.utils.weather_fetcher import _SOURCE_CONFIDENCE_MULTIPLIER

        assert _SOURCE_CONFIDENCE_MULTIPLIER["emergency_climate"] == 0.12

    def test_source_label_has_emergency_tag(self):
        """source_label 包含 [EMERGENCY] 标记。"""
        from src.utils.weather_fetcher import _SOURCE_CONFIDENCE_MULTIPLIER

        mult = _SOURCE_CONFIDENCE_MULTIPLIER["emergency_climate"]
        # 验证 source_label dict 中 emergency_climate 格式包含 [EMERGENCY]
        label = f"⚠️[EMERGENCY] Climatological Estimate [×{mult}] - ALL SOURCES DOWN"
        assert "[EMERGENCY]" in label


class TestGfsNeverReturnsNone:
    """T275: _gfs_multi_fallback 在所有真实源失败时仍不返回 None。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._CACHE.clear()
        wf._DATA_SOURCE_META.clear()
        wf._AWS_GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._NOMADS_CIRCUIT_OPEN_UNTIL = 0.0

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3", return_value=None)
    def test_all_real_sources_fail_still_returns_data(
        self, mock_aws, mock_nomads, mock_det, mock_stale
    ):
        """所有7层真实数据源全部失败 → T8 emergency climate 兜底, 不返回 None。"""
        import src.utils.weather_fetcher as wf

        result = wf._gfs_multi_fallback(40.0, -74.0, "temp_max", 3, "all_fail_test")
        assert result is not None, "全部失败时 emergency climate 应兜底"
        meta = wf._DATA_SOURCE_META.get("all_fail_test", {})
        assert meta.get("source") == "emergency_climate"

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_nomads_gfs", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_aws_gfs_s3")
    def test_aws_success_takes_priority_over_emergency(
        self, mock_aws, mock_nomads, mock_det, mock_stale
    ):
        """AWS S3 成功时, 用真实数据而不是 emergency。"""
        import src.utils.weather_fetcher as wf

        mock_aws.return_value = {"2026-03-29": [27.5]}
        result = wf._gfs_multi_fallback(40.0, -74.0, "temp_max", 3, "aws_priority")
        assert result == {"2026-03-29": [27.5]}
        meta = wf._DATA_SOURCE_META.get("aws_priority", {})
        assert meta.get("source") == "aws_gfs_s3", "应优先使用 AWS 真实数据"


class TestEcmwfNeverReturnsNone:
    """T275: _ecmwf_multi_fallback 在所有真实源失败时仍不返回 None。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._CACHE.clear()
        wf._DATA_SOURCE_META.clear()
        wf._ECMWF_MIRROR_CIRCUIT_OPEN_UNTIL = 0.0

    @patch("src.utils.weather_fetcher._get_stale_cache", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_deterministic_fallback", return_value=None)
    @patch("src.utils.weather_fetcher._fetch_ecmwf_cloud_mirror", return_value=None)
    def test_all_real_sources_fail_still_returns_data(
        self, mock_mirror, mock_det, mock_stale
    ):
        """所有ECMWF真实数据源全部失败 → emergency climate 兜底。"""
        import src.utils.weather_fetcher as wf

        result = wf._ecmwf_multi_fallback(40.0, -74.0, "temp_max", 3, "ecmwf_all_fail")
        assert result is not None, "全部失败时 emergency climate 应兜底"
        meta = wf._DATA_SOURCE_META.get("ecmwf_all_fail", {})
        assert meta.get("source") == "emergency_climate"
