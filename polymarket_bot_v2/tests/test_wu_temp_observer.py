"""
Tests for WU Daily Observations Temperature Observer (Phase 56).
Tests cover:
  - fetch_daily_observed_max_temp() with WU and Open-Meteo sources
  - _fetch_wu_daily_max_temp() regex pattern extraction
  - _fetch_open_meteo_observed_max() with past-hour filtering
  - close_paper_bet_wu_temp_breach() DB function
  - close_position_wu_temp_breach() OutcomeTracker method
  - get_station_info() helper
  - _WU_HISTORY_PATH_MAP completeness
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bet(
    bet_id: str = "abc12345-0000-0000-0000-000000000000",
    market_id: str = "mkt-001",
    question: str = "Will the highest temperature in Wellington be 21°C or higher on March 28?",
    outcome: str = "NO",
    market_price: float = 0.70,
    size_usdc: float = 5.0,
    category: str = "weather",
) -> dict:
    return {
        "bet_id": bet_id,
        "market_id": market_id,
        "question": question,
        "outcome": outcome,
        "market_price": market_price,
        "size_usdc": size_usdc,
        "category": category,
        "exit_price": 0.0,
    }


# ---------------------------------------------------------------------------
# get_station_info()
# ---------------------------------------------------------------------------


class TestGetStationInfo:
    def test_known_city_returns_tuple(self):
        from src.utils.weather_fetcher import get_station_info

        result = get_station_info("wellington")
        assert result is not None
        icao, lat, lon, name = result
        assert icao == "NZWN"
        assert isinstance(lat, float)
        assert isinstance(lon, float)

    def test_case_insensitive(self):
        from src.utils.weather_fetcher import get_station_info

        # get_station_info does .lower().strip() internally, so both work
        assert get_station_info("Wellington") is not None  # lowercased internally
        assert get_station_info("wellington") is not None  # lowercase works

    def test_unknown_city_returns_none(self):
        from src.utils.weather_fetcher import get_station_info

        assert get_station_info("atlantisCity") is None

    def test_multiple_cities_covered(self):
        from src.utils.weather_fetcher import get_station_info

        for city in ["tokyo", "seoul", "sydney", "paris", "chicago", "beijing"]:
            assert get_station_info(city) is not None, f"Missing: {city}"


# ---------------------------------------------------------------------------
# _WU_HISTORY_PATH_MAP completeness
# ---------------------------------------------------------------------------


class TestWUHistoryPathMap:
    def test_key_cities_present(self):
        from src.utils.weather_fetcher import _STATION_MAP, _WU_HISTORY_PATH_MAP

        # All ICAO codes in _STATION_MAP should ideally be in _WU_HISTORY_PATH_MAP
        key_icaos = {
            "NZWN",
            "NZAA",
            "YSSY",
            "YMML",
            "RJTT",
            "RKSI",
            "KLGA",
            "KORD",
            "KLAX",
            "LFPG",
            "EGLC",
        }
        for icao in key_icaos:
            assert icao in _WU_HISTORY_PATH_MAP, f"Missing ICAO in path map: {icao}"

    def test_path_format(self):
        from src.utils.weather_fetcher import _WU_HISTORY_PATH_MAP

        for icao, path in _WU_HISTORY_PATH_MAP.items():
            parts = path.split("/")
            assert len(parts) == 3, f"Expected 3 path parts for {icao}: {path}"
            assert parts[2] == icao, f"Last segment should match ICAO: {path}"


# ---------------------------------------------------------------------------
# _fetch_wu_daily_max_temp()
# ---------------------------------------------------------------------------


class TestFetchWuDailyMaxTemp:
    def _mock_response(self, html: str, status: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.text = html
        resp.status_code = status
        if status != 200:
            import requests

            resp.raise_for_status.side_effect = requests.HTTPError(f"{status} Error")
        else:
            resp.raise_for_status.return_value = None
        return resp

    def test_pattern1_metric_tempHigh(self):
        from src.utils.weather_fetcher import _fetch_wu_daily_max_temp

        html = 'some json data "metric":{"tempHigh":21,"tempLow":15} more data'
        with patch("requests.Session") as mock_session:
            mock_sess = MagicMock()
            mock_session.return_value = mock_sess
            mock_sess.get.return_value = self._mock_response(html)

            result = _fetch_wu_daily_max_temp("nz/wellington/NZWN", "2026-03-28")
            assert result == 21.0

    def test_pattern2_tempHigh_value(self):
        from src.utils.weather_fetcher import _fetch_wu_daily_max_temp

        html = 'data "tempHigh":{"value":23.5} more'
        with patch("requests.Session") as mock_session:
            mock_sess = MagicMock()
            mock_session.return_value = mock_sess
            mock_sess.get.return_value = self._mock_response(html)

            result = _fetch_wu_daily_max_temp("nz/wellington/NZWN", "2026-03-28")
            assert result == 23.5

    def test_pattern5_imperial_converts_to_celsius(self):
        from src.utils.weather_fetcher import _fetch_wu_daily_max_temp

        # 70°F should convert to ~21.1°C
        html = '"imperial":{"tempHigh":70,"tempLow":50}'
        with patch("requests.Session") as mock_session:
            mock_sess = MagicMock()
            mock_session.return_value = mock_sess
            mock_sess.get.return_value = self._mock_response(html)

            result = _fetch_wu_daily_max_temp("us-ny/new-york-city/KLGA", "2026-03-28")
            assert result is not None
            assert abs(result - 21.1) < 0.2  # 70°F ≈ 21.1°C

    def test_pattern6_fallback_to_individual_temps(self):
        from src.utils.weather_fetcher import _fetch_wu_daily_max_temp

        # Multiple metric temp values - should return max
        html = '"metric":{"temp":18} ' '"metric":{"temp":21} ' '"metric":{"temp":19}'
        with patch("requests.Session") as mock_session:
            mock_sess = MagicMock()
            mock_session.return_value = mock_sess
            mock_sess.get.return_value = self._mock_response(html)

            result = _fetch_wu_daily_max_temp("nz/wellington/NZWN", "2026-03-28")
            assert result == 21.0

    def test_returns_none_on_no_match(self):
        from src.utils.weather_fetcher import _fetch_wu_daily_max_temp

        html = "no temperature data here"
        with patch("requests.Session") as mock_session:
            mock_sess = MagicMock()
            mock_session.return_value = mock_sess
            mock_sess.get.return_value = self._mock_response(html)

            result = _fetch_wu_daily_max_temp("nz/wellington/NZWN", "2026-03-28")
            assert result is None

    def test_returns_none_on_network_error(self):
        import requests as _req
        from src.utils.weather_fetcher import _fetch_wu_daily_max_temp

        with patch("requests.Session") as mock_session:
            mock_sess = MagicMock()
            mock_session.return_value = mock_sess
            mock_sess.get.side_effect = _req.RequestException("network error")

            result = _fetch_wu_daily_max_temp("nz/wellington/NZWN", "2026-03-28")
            assert result is None

    def test_date_format_conversion(self):
        """WU 使用不带前导零的日期格式: 2026-3-5 而非 2026-03-05"""
        from src.utils.weather_fetcher import _fetch_wu_daily_max_temp

        captured_url = {}

        def fake_get(url, **kwargs):
            captured_url["url"] = url
            mock_r = MagicMock()
            mock_r.raise_for_status.return_value = None
            mock_r.text = ""
            return mock_r

        with patch("requests.Session") as mock_session:
            mock_sess = MagicMock()
            mock_session.return_value = mock_sess
            mock_sess.get.side_effect = fake_get

            _fetch_wu_daily_max_temp("nz/wellington/NZWN", "2026-03-05")
            assert "2026-3-5" in captured_url.get(
                "url", ""
            ), f"Expected non-zero-padded date but got: {captured_url.get('url')}"


# ---------------------------------------------------------------------------
# _fetch_open_meteo_observed_max()
# ---------------------------------------------------------------------------


class TestFetchOpenMeteoObservedMax:
    def _mock_om_response(
        self, temps: list, times: list, status: int = 200
    ) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"hourly": {"temperature_2m": temps, "time": times}}
        if status != 200:
            import requests

            resp.raise_for_status.side_effect = requests.HTTPError(f"{status}")
        else:
            resp.raise_for_status.return_value = None
        return resp

    def test_returns_max_of_past_hours_only(self):
        from src.utils.weather_fetcher import _fetch_open_meteo_observed_max

        # Simulate hourly temps where some are future (beyond now_utc)
        # Provide explicit times: past hours have real values, future hours too
        # We mock current UTC time to be 2026-03-28 10:00:00
        fake_now = MagicMock()
        fake_now.replace.return_value = MagicMock()

        import datetime as dt

        fixed_utc = dt.datetime(2026, 3, 28, 10, 0, 0)

        temps = [15.0, 17.0, 20.0, 25.0, 30.0]  # hours 0,1,2,3,4 → but 3,4 are future
        times = [
            "2026-03-28T00:00",
            "2026-03-28T01:00",
            "2026-03-28T02:00",
            "2026-03-28T11:00",  # future (after 10:00)
            "2026-03-28T12:00",  # future
        ]

        with patch("src.utils.weather_fetcher.datetime") as mock_dt:
            # datetime.now(timezone.utc).replace(tzinfo=None) should return fixed_utc
            mock_dt.now.return_value.replace.return_value = fixed_utc
            mock_dt.strptime.side_effect = dt.datetime.strptime

            with patch("requests.get") as mock_get:
                mock_get.return_value = self._mock_om_response(temps, times)
                result = _fetch_open_meteo_observed_max(
                    -41.3272, 174.8053, "2026-03-28"
                )

        # Should only use hours 00, 01, 02 → max is 20.0 (not 25 or 30)
        assert result == 20.0

    def test_returns_none_on_empty_data(self):
        from src.utils.weather_fetcher import _fetch_open_meteo_observed_max

        with patch("requests.get") as mock_get:
            mock_get.return_value = self._mock_om_response([], [])
            result = _fetch_open_meteo_observed_max(-41.3272, 174.8053, "2026-03-28")
        assert result is None

    def test_returns_none_on_network_error(self):
        import requests as _req
        from src.utils.weather_fetcher import _fetch_open_meteo_observed_max

        with patch("requests.get", side_effect=_req.RequestException("fail")):
            result = _fetch_open_meteo_observed_max(-41.3272, 174.8053, "2026-03-28")
        assert result is None

    def test_skips_none_temps(self):
        """None values in temp array should be skipped"""
        import datetime as dt

        from src.utils.weather_fetcher import _fetch_open_meteo_observed_max

        fixed_utc = dt.datetime(2026, 3, 28, 12, 0, 0)
        temps = [None, 18.0, None, 20.0, None]
        times = [
            "2026-03-28T00:00",
            "2026-03-28T01:00",
            "2026-03-28T02:00",
            "2026-03-28T03:00",
            "2026-03-28T04:00",
        ]

        with patch("src.utils.weather_fetcher.datetime") as mock_dt:
            mock_dt.now.return_value.replace.return_value = fixed_utc
            mock_dt.strptime.side_effect = dt.datetime.strptime

            with patch("requests.get") as mock_get:
                mock_get.return_value = self._mock_om_response(temps, times)
                result = _fetch_open_meteo_observed_max(
                    -41.3272, 174.8053, "2026-03-28"
                )
        assert result == 20.0


# ---------------------------------------------------------------------------
# _fetch_wu_v1_historical_max_temp() — T148
# ---------------------------------------------------------------------------


class TestFetchWuV1HistoricalMaxTemp:
    """T148: WU v1 Historical JSON API 新增降级源"""

    def test_returns_max_from_observations(self):
        from src.utils.weather_fetcher import _fetch_wu_v1_historical_max_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "observations": [
                {"temp": 18},
                {"temp": 22},
                {"temp": 20},
                {"temp": 19},
            ]
        }

        with patch("src.utils.weather_fetcher.requests.get", return_value=mock_resp):
            result = _fetch_wu_v1_historical_max_temp(
                -41.3272, 174.8053, "2026-03-28", "wellington"
            )

        assert result == 22.0

    def test_returns_none_on_empty_observations(self):
        from src.utils.weather_fetcher import _fetch_wu_v1_historical_max_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"observations": []}

        with patch("src.utils.weather_fetcher.requests.get", return_value=mock_resp):
            result = _fetch_wu_v1_historical_max_temp(
                -41.3272, 174.8053, "2026-03-28", "wellington"
            )

        assert result is None

    def test_returns_none_on_request_failure(self):
        import requests as _req
        from src.utils.weather_fetcher import _fetch_wu_v1_historical_max_temp

        with patch(
            "src.utils.weather_fetcher.requests.get",
            side_effect=_req.RequestException("timeout"),
        ):
            result = _fetch_wu_v1_historical_max_temp(
                -41.3272, 174.8053, "2026-03-28", "wellington"
            )

        assert result is None

    def test_returns_none_on_zero_coords(self):
        from src.utils.weather_fetcher import _fetch_wu_v1_historical_max_temp

        result = _fetch_wu_v1_historical_max_temp(0, 0, "2026-03-28", "test")
        # lat=0, lon=0 → falsy check → None
        assert result is None

    def test_date_format_conversion(self):
        from src.utils.weather_fetcher import _fetch_wu_v1_historical_max_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"observations": [{"temp": 25}]}

        with patch(
            "src.utils.weather_fetcher.requests.get", return_value=mock_resp
        ) as mock_get:
            result = _fetch_wu_v1_historical_max_temp(
                35.6762, 139.6503, "2026-04-07", "tokyo"
            )

        assert result == 25.0
        # 确认API请求中日期格式为 YYYYMMDD
        call_url = mock_get.call_args[0][0]
        assert "20260407" in call_url

    def test_skips_invalid_temp_entries(self):
        from src.utils.weather_fetcher import _fetch_wu_v1_historical_max_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "observations": [
                {"temp": None},
                {"temp": "invalid"},
                {"temp": 18},
                {},
            ]
        }

        with patch("src.utils.weather_fetcher.requests.get", return_value=mock_resp):
            result = _fetch_wu_v1_historical_max_temp(
                -41.3272, 174.8053, "2026-03-28", "wellington"
            )

        assert result == 18.0

    def test_tries_next_key_on_401(self):
        from src.utils.weather_fetcher import _fetch_wu_v1_historical_max_temp

        mock_resp_401 = MagicMock()
        mock_resp_401.status_code = 401

        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.raise_for_status = MagicMock()
        mock_resp_ok.json.return_value = {"observations": [{"temp": 30}]}

        with patch(
            "src.utils.weather_fetcher.requests.get",
            side_effect=[mock_resp_401, mock_resp_ok],
        ):
            result = _fetch_wu_v1_historical_max_temp(
                35.0, 139.0, "2026-04-07", "tokyo"
            )

        assert result == 30.0


# ---------------------------------------------------------------------------
# fetch_daily_observed_max_temp() integration
# ---------------------------------------------------------------------------


class TestFetchDailyObservedMaxTemp:
    def setup_method(self):
        """清空所有WU缓存在每个测试前"""
        from src.utils import weather_fetcher

        weather_fetcher._WU_OBS_CACHE.clear()
        weather_fetcher._WU_REALTIME_CACHE.clear()  # Task214: 新增实时缓存
        weather_fetcher._WU_API_CACHE.clear()  # Task224: 新增API缓存
        weather_fetcher._WU_RUNNING_MAX.clear()  # T229: running max缓存

    def test_uses_wu_realtime_as_primary_source(self):
        """Task224: WU v3 API (wu-api) 现为最高优先级数据源"""
        from src.utils.weather_fetcher import fetch_daily_observed_max_temp

        with patch(
            "src.utils.weather_fetcher._fetch_wu_api_temp", return_value=21.5
        ) as mock_api:
            with patch(
                "src.utils.weather_fetcher._fetch_wu_realtime_temp", return_value=20.0
            ) as mock_rt:
                with patch(
                    "src.utils.weather_fetcher._fetch_wu_daily_max_temp",
                    return_value=19.0,
                ) as mock_hist:
                    result_temp, result_src = fetch_daily_observed_max_temp(
                        "NZWN", -41.3272, 174.8053, "2026-03-28", "wellington"
                    )

        assert result_temp == 21.5
        assert result_src == "wu-api"  # Task224: P0 数据源标识
        mock_api.assert_called_once()
        mock_rt.assert_not_called()  # P0成功则不调用P1
        mock_hist.assert_not_called()  # P0成功则不调用P2

    def test_falls_back_to_history_when_realtime_fails(self):
        """Task224+T148: P0(wu-api)+P1(wu-weather)均失败 → P1.5(wu-v1-historical)"""
        from src.utils.weather_fetcher import fetch_daily_observed_max_temp

        with patch("src.utils.weather_fetcher._fetch_wu_api_temp", return_value=None):
            with patch(
                "src.utils.weather_fetcher._fetch_wu_realtime_temp", return_value=None
            ):
                with patch(
                    "src.utils.weather_fetcher._fetch_wu_v1_historical_max_temp",
                    return_value=21.5,
                ) as mock_v1:
                    with patch(
                        "src.utils.weather_fetcher._fetch_wu_daily_max_temp",
                        return_value=21.5,
                    ) as mock_hist:
                        with patch(
                            "src.utils.weather_fetcher._fetch_open_meteo_observed_max",
                            return_value=20.1,
                        ) as mock_om:
                            result_temp, result_src = fetch_daily_observed_max_temp(
                                "NZWN", -41.3272, 174.8053, "2026-03-28", "wellington"
                            )

        # T148: P1.5 (wu-v1-historical) 成功, 不调用P2/P3
        assert result_temp == 21.5
        assert result_src == "wu-v1-historical"
        mock_v1.assert_called_once()
        mock_hist.assert_not_called()
        mock_om.assert_not_called()

    def test_falls_back_to_open_meteo_when_wu_fails(self):
        """P0(wu-api)+P1(wu-weather)+P1.5(wu-v1)+P2(history)均失败 → Open-Meteo"""
        from src.utils.weather_fetcher import fetch_daily_observed_max_temp

        with patch("src.utils.weather_fetcher._fetch_wu_api_temp", return_value=None):
            with patch(
                "src.utils.weather_fetcher._fetch_wu_realtime_temp", return_value=None
            ):
                with patch(
                    "src.utils.weather_fetcher._fetch_wu_v1_historical_max_temp",
                    return_value=None,
                ):
                    with patch(
                        "src.utils.weather_fetcher._fetch_wu_daily_max_temp",
                        return_value=None,
                    ):
                        with patch(
                            "src.utils.weather_fetcher._fetch_open_meteo_observed_max",
                            return_value=20.8,
                        ) as mock_om:
                            result_temp, result_src = fetch_daily_observed_max_temp(
                                "NZWN", -41.3272, 174.8053, "2026-03-28", "wellington"
                            )

        assert result_temp == 20.8
        assert result_src == "open-meteo"

    def test_returns_none_when_all_sources_fail(self):
        from src.utils.weather_fetcher import fetch_daily_observed_max_temp

        with patch("src.utils.weather_fetcher._fetch_wu_api_temp", return_value=None):
            with patch(
                "src.utils.weather_fetcher._fetch_wu_realtime_temp", return_value=None
            ):
                with patch(
                    "src.utils.weather_fetcher._fetch_wu_v1_historical_max_temp",
                    return_value=None,
                ):
                    with patch(
                        "src.utils.weather_fetcher._fetch_wu_daily_max_temp",
                        return_value=None,
                    ):
                        with patch(
                            "src.utils.weather_fetcher._fetch_open_meteo_observed_max",
                            return_value=None,
                        ):
                            result_temp, result_src = fetch_daily_observed_max_temp(
                                "NZWN", -41.3272, 174.8053, "2026-03-28", "wellington"
                            )

        assert result_temp is None
        assert result_src == ""

    def test_realtime_cache_prevents_repeat_requests(self):
        """Task224: wu-api 数据缓存120s，避免同一坐标重复请求"""
        from src.utils.weather_fetcher import fetch_daily_observed_max_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "temperature": 20,
            "temperatureMax24Hour": 22,
            "temperatureMaxSince7am": 21,
        }

        with patch(
            "src.utils.weather_fetcher.requests.get",
            return_value=mock_resp,
        ) as mock_get:
            # 第一次调用: 命中WU-API（实际HTTP请求）
            r1, src1 = fetch_daily_observed_max_temp(
                "NZWN", -41.3272, 174.8053, "2026-03-28"
            )
            # 第二次调用: 应命中_WU_API_CACHE，不再发HTTP请求
            r2, src2 = fetch_daily_observed_max_temp(
                "NZWN", -41.3272, 174.8053, "2026-03-28"
            )

        assert r1 == r2 == 21.0
        assert src1 == src2 == "wu-api"
        assert mock_get.call_count == 1  # HTTP请求只发一次(缓存命中)

    def test_icao_not_in_path_map_skips_wu(self):
        """ICAO不在_WU_HISTORY_PATH_MAP时，wu-api失败后P1.5→Open-Meteo"""
        from src.utils.weather_fetcher import fetch_daily_observed_max_temp

        with patch(
            "src.utils.weather_fetcher._fetch_wu_api_temp", return_value=None
        ) as mock_api:
            with patch("src.utils.weather_fetcher._fetch_wu_realtime_temp") as mock_rt:
                with patch(
                    "src.utils.weather_fetcher._fetch_wu_v1_historical_max_temp",
                    return_value=None,
                ) as mock_v1:
                    with patch(
                        "src.utils.weather_fetcher._fetch_wu_daily_max_temp"
                    ) as mock_wu:
                        with patch(
                            "src.utils.weather_fetcher._fetch_open_meteo_observed_max",
                            return_value=25.0,
                        ):
                            result_temp, result_src = fetch_daily_observed_max_temp(
                                "XXXX",  # 不在 _WU_HISTORY_PATH_MAP
                                10.0,
                                20.0,
                                "2026-03-28",
                            )

        # P0(wu-api)被调用但返回None，P1跳过（无path），P1.5被调用但返回None，降级到P3
        mock_api.assert_called_once()
        mock_rt.assert_not_called()
        mock_v1.assert_called_once()
        mock_wu.assert_not_called()
        assert result_temp == 25.0
        assert result_src == "open-meteo"


# ---------------------------------------------------------------------------
# close_paper_bet_wu_temp_breach() DB function
# ---------------------------------------------------------------------------


class TestClosepaperBetWuTempBreach:
    def test_writes_correct_fields(self, tmp_path):
        from src.utils.db import (
            close_paper_bet_wu_temp_breach,
            connect,
            init_db,
            insert_paper_bet,
        )

        db_path = tmp_path / "test.db"
        init_db(db_path)

        import time as _t

        bet_id = "test-wu-breach-001-aa00"
        test_bet = {
            "bet_id": bet_id,
            "market_id": "mkt-wu-001",
            "question": "Will highest temp be 21°C?",
            "outcome": "NO",
            "market_price": 0.65,
            "size_usdc": 5.0,
            "confidence": 0.75,
            "condition_id": "cond-001",
            "source": "test",
            "matched_shares": 7.69,
            "ts_entered": _t.time(),
            "category": "weather",
        }
        insert_paper_bet(test_bet, db_path=db_path)

        close_paper_bet_wu_temp_breach(
            bet_id=bet_id,
            exit_price=0.10,
            pnl=-4.23,
            payout=0.77,
            db_path=db_path,
        )

        with connect(db_path) as conn:
            row = conn.execute(
                "SELECT exit_type, resolved_outcome, is_win, exit_price, pnl "
                "FROM paper_bets WHERE bet_id=?",
                (bet_id,),
            ).fetchone()

        assert row is not None
        assert row[0] == "wu_temp_breach"
        assert row[1] == "WU_TEMP_BREACH"
        assert row[2] == 0  # is_win = 0
        assert abs(row[3] - 0.10) < 0.001
        assert abs(row[4] - (-4.23)) < 0.01


# ---------------------------------------------------------------------------
# close_position_wu_temp_breach() OutcomeTracker method
# ---------------------------------------------------------------------------


class TestClosePositionWuTempBreach:
    def _make_tracker(self) -> MagicMock:
        """Build a minimal OutcomeTracker-like object with close_position_wu_temp_breach."""
        from src.simulation.outcome_tracker import OutcomeTracker

        ot = OutcomeTracker.__new__(OutcomeTracker)
        return ot

    def test_calls_db_function(self, tmp_path):
        from src.simulation.outcome_tracker import OutcomeTracker

        ot = OutcomeTracker.__new__(OutcomeTracker)
        bet = _make_bet(
            outcome="NO",
            market_price=0.65,
            size_usdc=5.0,
        )
        bet["exit_price"] = 0.10

        with patch("src.utils.db.close_paper_bet_wu_temp_breach") as mock_db:
            with patch("src.utils.telegram_notifier.get_notifier") as mock_notif:
                mock_notif.return_value.notify_position_closed = MagicMock()
                ot.close_position_wu_temp_breach(
                    bet=bet,
                    observed_temp_c=22.5,
                    threshold_c=21.0,
                    obs_source="wunderground",
                )

        mock_db.assert_called_once()
        call_kwargs = mock_db.call_args
        assert call_kwargs.kwargs["bet_id"] == bet["bet_id"]
        assert abs(call_kwargs.kwargs["exit_price"] - 0.10) < 0.001

    def test_sends_telegram_notification(self):
        from src.simulation.outcome_tracker import OutcomeTracker

        ot = OutcomeTracker.__new__(OutcomeTracker)
        bet = _make_bet(outcome="NO", market_price=0.65, size_usdc=5.0)
        bet["exit_price"] = 0.15

        with patch("src.utils.db.close_paper_bet_wu_temp_breach"):
            with patch("src.utils.telegram_notifier.get_notifier") as mock_notif:
                mock_notify = MagicMock()
                mock_notif.return_value.notify_position_closed = mock_notify
                ot.close_position_wu_temp_breach(
                    bet=bet,
                    observed_temp_c=21.5,
                    threshold_c=21.0,
                    obs_source="wunderground",
                )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        assert call_kwargs["close_type"] == "wu_temp_breach"
        # Question should have temperature prefix prepended
        q = call_kwargs["question"]
        assert "21.5" in q or "21" in q  # observed or threshold temp in question
        assert call_kwargs["entry_price"] == pytest.approx(0.65)
        assert call_kwargs["exit_price"] == pytest.approx(0.15)

    def test_handles_negative_exit_price(self):
        """exit_price < 0 should skip the DB call (invalid)"""
        from src.simulation.outcome_tracker import OutcomeTracker

        ot = OutcomeTracker.__new__(OutcomeTracker)
        bet = _make_bet(outcome="NO", market_price=0.65, size_usdc=5.0)
        bet["exit_price"] = -0.10  # negative = invalid

        with patch("src.utils.db.close_paper_bet_wu_temp_breach") as mock_db:
            with patch("src.utils.telegram_notifier.get_notifier"):
                ot.close_position_wu_temp_breach(
                    bet=bet,
                    observed_temp_c=21.5,
                    threshold_c=21.0,
                )

        # Should not call DB when exit_price < 0 (invalid)
        mock_db.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: breach decision logic
# ---------------------------------------------------------------------------


class TestFetchWuRealtimeTemp:
    """
    Task214: 测试新的 WU /weather/ 实时页面抓取函数。
    URL: https://www.wunderground.com/weather/{path}
    """

    def test_extracts_temp_from_json_tempHigh(self):
        """从嵌入JSON的tempHigh字段提取温度"""
        from src.utils.weather_fetcher import _fetch_wu_realtime_temp

        fake_html = '{"tempHigh": {"value": 21.5}, "metric": {}}'
        mock_resp = MagicMock()
        mock_resp.text = fake_html
        mock_resp.raise_for_status.return_value = None

        with patch("requests.Session") as mock_session:
            mock_session.return_value.get.return_value = mock_resp
            result = _fetch_wu_realtime_temp("nz/wellington/NZWN")

        assert result == 21.5

    def test_extracts_temp_from_current_temp_div(self):
        """从 <div class='current-temp'> 中的 wu-value-to span 提取温度"""
        from src.utils.weather_fetcher import _fetch_wu_realtime_temp

        fake_html = (
            '<div class="current-temp" style="color: blue;">'
            '<lib-display-unit type="temperature">'
            '<span class="wu-unit wu-unit-temperature">'
            '<span class="wu-value wu-value-to">21</span>'
            '&nbsp;<span class="wu-label"><span class="wu-degree">°</span><span>C</span></span>'
            "</span></lib-display-unit>"
        )
        mock_resp = MagicMock()
        mock_resp.text = fake_html
        mock_resp.raise_for_status.return_value = None

        with patch("requests.Session") as mock_session:
            mock_session.return_value.get.return_value = mock_resp
            result = _fetch_wu_realtime_temp("nz/wellington/NZWN")

        assert result == 21.0

    def test_returns_none_on_no_match(self):
        """HTML无温度数据时返回None（不抛出异常）"""
        from src.utils.weather_fetcher import _fetch_wu_realtime_temp

        mock_resp = MagicMock()
        mock_resp.text = "<html><body>Loading...</body></html>"
        mock_resp.raise_for_status.return_value = None

        with patch("requests.Session") as mock_session:
            mock_session.return_value.get.return_value = mock_resp
            result = _fetch_wu_realtime_temp("nz/wellington/NZWN")

        assert result is None

    def test_returns_none_on_network_error(self):
        """网络请求失败时返回None（不抛出异常）"""
        import requests as _req
        from src.utils.weather_fetcher import _fetch_wu_realtime_temp

        with patch("requests.Session") as mock_session:
            mock_session.return_value.get.side_effect = _req.RequestException("timeout")
            result = _fetch_wu_realtime_temp("nz/wellington/NZWN")

        assert result is None

    def test_rejects_implausible_temperature(self):
        """超出-60°C~60°C范围的温度值应被过滤（防止误解析其他数字）"""
        from src.utils.weather_fetcher import _fetch_wu_realtime_temp

        # wu-value-to 中出现999之类的异常数字（如JS时间戳片段）
        fake_html = (
            '<div class="current-temp">'
            '<span class="wu-value wu-value-to">999</span>'
            "</div>"
        )
        mock_resp = MagicMock()
        mock_resp.text = fake_html
        mock_resp.raise_for_status.return_value = None

        with patch("requests.Session") as mock_session:
            mock_session.return_value.get.return_value = mock_resp
            result = _fetch_wu_realtime_temp("nz/wellington/NZWN")

        assert result is None


class TestWuBreachDecisionLogic:
    """
    Tests the breach decision logic that would run inside _weather_peak_watchdog.

    Task215 修正:
      exact 市场逻辑:
        - YES死刑: 实测 > 阈值 (超过精确值，YES不可能)
        - NO仓位: 不在此处触发死刑 (实测=阈值时YES未确认，不能误杀NO)
    """

    def _should_force_exit(
        self,
        direction: str,
        outcome: str,
        obs_max_c: float,
        threshold_c: float,
        threshold_low_c: Optional[float] = None,
        threshold_high_c: Optional[float] = None,
        unit: str = "C",
        threshold_native: Optional[float] = None,
        threshold_low_native: Optional[float] = None,
        threshold_high_native: Optional[float] = None,
    ) -> tuple[bool, str]:
        """
        Mirrors the breach decision logic from _weather_peak_watchdog (T228 corrected).
        Returns (should_force, reason).

        T228 修正: 四舍五入必须在市场原始单位下进行！
          °F市场 → 观测值转°F后四舍五入 → 和°F阈值比较
          °C市场 → 观测值在°C下四舍五入 → 和°C阈值比较

        当 unit="F" 时, threshold_native/threshold_high_native 保存原始°F值。
        当 unit="C" 时, 使用 threshold_c / threshold_high_c (等同于 native)。
        """
        force = False
        reason = ""

        # T228: 在市场原始单位下四舍五入
        if unit == "F":
            obs_in_native = obs_max_c * 9 / 5 + 32  # °C→°F
            obs_rounded = int(obs_in_native + 0.5)
            thresh_cmp = (
                threshold_native if threshold_native is not None else threshold_c
            )
        else:
            obs_rounded = int(obs_max_c + 0.5)
            thresh_cmp = threshold_c

        if direction == "gte":
            if obs_rounded >= thresh_cmp and outcome == "NO":
                force = True
                reason = f"gte: obs={obs_max_c:.1f}(→{obs_rounded}) >= thresh={thresh_cmp:.1f} → NO死刑"
        elif direction == "lte":
            if obs_rounded > thresh_cmp and outcome == "YES":
                force = True
                reason = f"lte: obs={obs_max_c:.1f}(→{obs_rounded}) > thresh={thresh_cmp:.1f} → YES死刑"
        elif direction == "exact":
            if obs_rounded > thresh_cmp and outcome == "YES":
                force = True
                reason = f"exact: obs={obs_max_c:.1f}(→{obs_rounded}) > thresh={thresh_cmp:.1f} → YES死刑"
        elif direction == "range":
            high_cmp = (
                threshold_high_native
                if threshold_high_native is not None
                else threshold_high_c
            )
            if high_cmp and obs_rounded > high_cmp and outcome == "YES":
                force = True
                reason = f"range: obs={obs_max_c:.1f}(→{obs_rounded}) > high={high_cmp} → YES死刑"

        return force, reason

    # ── gte direction ────────────────────────────────────────────────

    def test_gte_no_position_triggered_when_threshold_reached(self):
        """NO position in gte market → triggered when observed >= threshold"""
        force, reason = self._should_force_exit(
            direction="gte", outcome="NO", obs_max_c=21.0, threshold_c=21.0
        )
        assert force is True
        assert "NO死刑" in reason

    def test_gte_no_position_triggered_when_threshold_exceeded(self):
        force, reason = self._should_force_exit(
            direction="gte", outcome="NO", obs_max_c=23.0, threshold_c=21.0
        )
        assert force is True

    def test_gte_no_position_not_triggered_below_threshold(self):
        # round(20.4)=20, 20 < 21 → False ✓
        force, _ = self._should_force_exit(
            direction="gte", outcome="NO", obs_max_c=20.4, threshold_c=21.0
        )
        assert force is False

    def test_gte_no_position_triggered_when_rounds_to_threshold(self):
        """Task216: 20.6°C → round→21°C >= 21 → YES已确定 → kill NO"""
        force, _ = self._should_force_exit(
            direction="gte", outcome="NO", obs_max_c=20.6, threshold_c=21.0
        )
        assert force is True  # round(20.6)=21, 21 >= 21 → True

    def test_gte_yes_position_not_triggered_even_when_threshold_reached(self):
        """YES position in gte market → winning, should NOT force exit"""
        force, _ = self._should_force_exit(
            direction="gte", outcome="YES", obs_max_c=22.0, threshold_c=21.0
        )
        assert force is False

    # ── lte direction ────────────────────────────────────────────────

    def test_lte_yes_position_triggered_when_threshold_exceeded(self):
        """YES position in lte market → triggered when observed > threshold (YES impossible)"""
        force, reason = self._should_force_exit(
            direction="lte", outcome="YES", obs_max_c=22.0, threshold_c=21.0
        )
        assert force is True
        assert "YES死刑" in reason

    def test_lte_yes_position_not_triggered_at_threshold(self):
        """obs == threshold doesn't kill lte YES (it's still possible to settle as YES)"""
        force, _ = self._should_force_exit(
            direction="lte", outcome="YES", obs_max_c=21.0, threshold_c=21.0
        )
        assert force is False

    def test_lte_yes_not_triggered_when_rounds_to_threshold(self):
        """Task216: 21.4°C → round→21°C, 21 > 21 = False → lte YES不触发"""
        force, _ = self._should_force_exit(
            direction="lte", outcome="YES", obs_max_c=21.4, threshold_c=21.0
        )
        assert force is False  # round(21.4)=21, 21 > 21 → False

    def test_lte_no_position_not_triggered(self):
        """NO position in lte market that's going against YES → don't exit NO"""
        force, _ = self._should_force_exit(
            direction="lte", outcome="NO", obs_max_c=25.0, threshold_c=21.0
        )
        assert force is False

    # ── exact direction (Task215 corrected) ──────────────────────────

    def test_exact_no_not_killed_when_threshold_reached(self):
        """Task215修正: exact市场obs=21°C时NO仓位不触发死刑(YES未确认，可能继续上升)"""
        force, _ = self._should_force_exit(
            direction="exact", outcome="NO", obs_max_c=21.0, threshold_c=21.0
        )
        assert force is False  # 修正: 旧代码错误地在此处杀NO

    def test_exact_no_not_killed_even_above_threshold(self):
        """Task215: exact市场NO仓位始终不触发WU死刑(由价格机制处理)"""
        force, _ = self._should_force_exit(
            direction="exact", outcome="NO", obs_max_c=22.0, threshold_c=21.0
        )
        assert force is False  # NO不被WU死刑机制触发

    def test_exact_yes_triggered_when_above_threshold(self):
        """Task215: exact市场obs超过阈值 → YES死刑(最高温已超精确值，YES不可能)"""
        # WU整数22°C > 阈值21°C → 触发YES死刑
        force, reason = self._should_force_exit(
            direction="exact", outcome="YES", obs_max_c=22.0, threshold_c=21.0
        )
        assert force is True
        assert "YES死刑" in reason

    def test_exact_yes_triggered_when_far_above_exact(self):
        """明显超过阈值(23.5°C)时YES必须死"""
        force, _ = self._should_force_exit(
            direction="exact", outcome="YES", obs_max_c=23.5, threshold_c=21.0
        )
        assert force is True

    def test_exact_yes_not_triggered_at_exact_threshold(self):
        """Task215: obs恰好=阈值时YES不触发死刑(WU整数21°C，必须21>21才触发→False)"""
        # WU实时页面显示21°C，阈值21°C → 21.0 > 21.0 = False → 不触发
        force, _ = self._should_force_exit(
            direction="exact", outcome="YES", obs_max_c=21.0, threshold_c=21.0
        )
        assert force is False  # 精确地在阈值：还不能判断(可能继续上升or就是21°C)

    def test_exact_yes_triggered_when_marginally_above(self):
        """Task216: Open-Meteo 21.5°C → round→22°C > 阈值21°C → YES死刑"""
        # 21.5°C四舍五入为22°C，22 > 21 = True → 最高温已超精确值，YES不可能
        force, _ = self._should_force_exit(
            direction="exact", outcome="YES", obs_max_c=21.5, threshold_c=21.0
        )
        assert force is True

    def test_exact_yes_not_triggered_when_rounds_to_threshold(self):
        """Task216: Open-Meteo 21.4°C → round→21°C = 阈值21°C → 不触发YES死刑

        Polymarket无0.5°C概念，21.4°C四舍五入为21°C，与阈值相等，
        不能判断最高温是否精确=21°C，故不触发(21 > 21 = False)。
        """
        force, _ = self._should_force_exit(
            direction="exact", outcome="YES", obs_max_c=21.4, threshold_c=21.0
        )
        assert force is False  # round(21.4)=21, 21 > 21 → False → 不触发

    # ── range direction ─────────────────────────────────────────────

    def test_range_yes_position_triggered_when_above_high(self):
        force, _ = self._should_force_exit(
            direction="range",
            outcome="YES",
            obs_max_c=23.0,
            threshold_c=21.0,
            threshold_high_c=22.0,
        )
        assert force is True  # round(23)=23 > 22 → True

    def test_range_yes_position_not_triggered_within_range(self):
        force, _ = self._should_force_exit(
            direction="range",
            outcome="YES",
            obs_max_c=20.0,
            threshold_c=18.0,
            threshold_high_c=22.0,
        )
        assert force is False

    # ── °F market rounding (T228) ──────────────────────────────────

    def test_fahrenheit_range_boundary_no_false_kill(self):
        """T228 核心BUG修复测试:
        市场 "between 84-85°F"，实测 29.67°C = 85.4°F。
        °F四舍五入 = 85°F → 在[84,85]范围内 → 不应死刑。
        旧BUG: °C四舍五入 int(29.67+0.5)=30 > int(29.44+0.5)=29 → 误杀！
        """
        obs_c = 29.67  # = 85.4°F
        force, _ = self._should_force_exit(
            direction="range",
            outcome="YES",
            obs_max_c=obs_c,
            threshold_c=(84 - 32) * 5 / 9,
            threshold_high_c=(85 - 32) * 5 / 9,
            unit="F",
            threshold_high_native=85.0,
        )
        assert force is False, "85.4°F rounds to 85°F, within 84-85°F range → no kill"

    def test_fahrenheit_range_boundary_correct_kill(self):
        """T228: 实测 29.72°C = 85.5°F → °F四舍五入 = 86°F → 超出85°F → 应该死刑。"""
        obs_c = 29.72  # = 85.496°F → rounds to 85 in °F actually
        # Let's be precise: 29.72 * 9/5 + 32 = 53.496 + 32 = 85.496 → int(85.496+0.5) = int(85.996) = 85
        # Need 85.5°F exactly: (85.5 - 32) * 5/9 = 29.722°C
        obs_c = 29.723  # = 85.5°F → int(85.5+0.5) = 86
        force, _ = self._should_force_exit(
            direction="range",
            outcome="YES",
            obs_max_c=obs_c,
            threshold_c=(84 - 32) * 5 / 9,
            threshold_high_c=(85 - 32) * 5 / 9,
            unit="F",
            threshold_high_native=85.0,
        )
        assert force is True, "85.5°F rounds to 86°F, exceeds 85°F range → kill"

    def test_fahrenheit_gte_boundary(self):
        """T228: gte市场 ">=85°F", 实测84.5°F(29.17°C) → 四舍五入85°F → YES确定 → kill NO。"""
        obs_c = 29.17  # = 84.506°F → int(84.506+0.5)=int(85.006)=85
        force, _ = self._should_force_exit(
            direction="gte",
            outcome="NO",
            obs_max_c=obs_c,
            threshold_c=(85 - 32) * 5 / 9,
            unit="F",
            threshold_native=85.0,
        )
        assert force is True, "84.5°F rounds to 85°F >= 85°F → YES confirmed → NO死刑"

    def test_fahrenheit_gte_below_boundary(self):
        """T228: gte市场 ">=85°F", 实测84.4°F(29.11°C) → 四舍五入84°F → 未到阈值 → 不触发。"""
        obs_c = 29.11  # = 84.398°F → int(84.398+0.5)=int(84.898)=84
        force, _ = self._should_force_exit(
            direction="gte",
            outcome="NO",
            obs_max_c=obs_c,
            threshold_c=(85 - 32) * 5 / 9,
            unit="F",
            threshold_native=85.0,
        )
        assert force is False, "84.4°F rounds to 84°F < 85°F → not triggered"

    def test_fahrenheit_exact_boundary(self):
        """T228: exact市场 "=85°F", 实测85.4°F(29.67°C) → 四舍五入85°F → 等于阈值 → 不触发。"""
        obs_c = 29.67  # ≈ 85.4°F → int(85.4+0.5)=int(85.9)=85
        force, _ = self._should_force_exit(
            direction="exact",
            outcome="YES",
            obs_max_c=obs_c,
            threshold_c=(85 - 32) * 5 / 9,
            unit="F",
            threshold_native=85.0,
        )
        assert (
            force is False
        ), "85.4°F rounds to 85°F, equals threshold → no kill (could settle as YES)"

    def test_fahrenheit_exact_kills_when_above(self):
        """T228: exact市场 "=85°F", 实测85.5°F(29.72°C) → 四舍五入86°F → 超过阈值 → YES死刑。"""
        obs_c = 29.723  # ≈ 85.5°F → int(86.0)=86
        force, _ = self._should_force_exit(
            direction="exact",
            outcome="YES",
            obs_max_c=obs_c,
            threshold_c=(85 - 32) * 5 / 9,
            unit="F",
            threshold_native=85.0,
        )
        assert force is True, "85.5°F rounds to 86°F > 85°F → YES impossible → kill"

    def test_celsius_market_unchanged(self):
        """T228: °C市场行为不变——21.5°C → round=22 > 21 → YES死刑。"""
        force, _ = self._should_force_exit(
            direction="exact",
            outcome="YES",
            obs_max_c=21.5,
            threshold_c=21.0,
            unit="C",
        )
        assert force is True, "°C market: 21.5→22 > 21 → YES死刑 (unchanged behavior)"


# ---------------------------------------------------------------------------
# Edge cases: unit conversion (°F → °C)
# ---------------------------------------------------------------------------


class TestUnitConversion:
    def test_fahrenheit_threshold_converts_correctly(self):
        """Threshold of 70°F = 21.11°C — observed 22°C should trigger gte NO"""
        threshold_f = 70.0
        threshold_c = (threshold_f - 32) * 5 / 9  # ≈ 21.11°C
        obs_max_c = 22.0

        # gte NO should be triggered
        assert obs_max_c >= threshold_c

    def test_negative_celsius_threshold(self):
        """Negative temperature thresholds should work (e.g., -5°C or below)"""
        threshold_c = -5.0
        obs_max_c = 0.0

        # gte direction: 0 >= -5 → YES confirmed → NO死刑
        assert obs_max_c >= threshold_c


class TestWuApiTempFieldPriority:
    """Task226: 验证 _fetch_wu_api_temp 字段优先级。
    temperatureMaxSince7am > temperature > (temperatureMax24Hour仅日志参考)
    """

    def setup_method(self):
        from src.utils import weather_fetcher

        weather_fetcher._WU_API_CACHE.clear()
        weather_fetcher._WU_RUNNING_MAX.clear()

    @patch("src.utils.weather_fetcher.requests.get")
    def test_prefers_max_since_7am_over_current(self, mock_get):
        """优先使用 temperatureMaxSince7am（今日最高）而非 temperature。"""
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "temperature": 26.0,
            "temperatureMax24Hour": 28.0,
            "temperatureMaxSince7am": 27.0,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_wu_api_temp(25.76, -80.19, "Miami")
        assert result == 27.0, "Should use temperatureMaxSince7am (27) not max24h (28)"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_falls_back_to_current_when_since7am_missing(self, mock_get):
        """temperatureMaxSince7am 缺失时回退到 temperature。"""
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "temperature": 26.0,
            "temperatureMax24Hour": 28.0,
            # temperatureMaxSince7am missing
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_wu_api_temp(25.76, -80.19, "Miami")
        assert result == 26.0, "Should use temperature (26) not max24h (28)"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_does_not_use_max24h_alone(self, mock_get):
        """temperatureMax24Hour 不应单独作为返回值候选。"""
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "temperatureMax24Hour": 28.0,
            # both temperature and temperatureMaxSince7am missing
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_wu_api_temp(25.76, -80.19, "Miami")
        assert result is None, "Should return None — max24h alone is unreliable"


# ---------------------------------------------------------------------------
# T229: WU API units=e/m + running_max 测试
# ---------------------------------------------------------------------------


class TestWuApiUnitsAndRunningMax:
    """T229: 验证 _fetch_wu_api_temp 的 native_unit 参数和 running_max 机制。"""

    def setup_method(self):
        from src.utils import weather_fetcher

        weather_fetcher._WU_API_CACHE.clear()
        weather_fetcher._WU_RUNNING_MAX.clear()

    @patch("src.utils.weather_fetcher.requests.get")
    def test_fahrenheit_market_uses_units_e(self, mock_get):
        """°F市场应使用 units=e 请求API，URL中包含 units=e"""
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "temperature": 81,
            "temperatureMax24Hour": 82,
            "temperatureMaxSince7am": 81,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="F")
        call_url = mock_get.call_args[0][0]
        assert (
            "units=e" in call_url
        ), f"Should use units=e for °F market, got: {call_url}"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_celsius_market_uses_units_m(self, mock_get):
        """°C市场应使用 units=m 请求API"""
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "temperature": 27,
            "temperatureMax24Hour": 28,
            "temperatureMaxSince7am": 27,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        _fetch_wu_api_temp(-41.33, 174.81, "Wellington", native_unit="C")
        call_url = mock_get.call_args[0][0]
        assert (
            "units=m" in call_url
        ), f"Should use units=m for °C market, got: {call_url}"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_fahrenheit_precision_preserved(self, mock_get):
        """°F市场: 81°F → (81-32)*5/9 = 27.222°C，不是整数27或28"""
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "temperature": 81,
            "temperatureMaxSince7am": 81,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="F")
        expected_c = (81 - 32) * 5 / 9  # 27.2222...
        assert (
            abs(result - expected_c) < 0.01
        ), f"81°F should convert to {expected_c:.4f}°C, got {result}"
        # T228 反转: 27.222 × 9/5 + 32 = 81.0°F → int(81.5) = 81 ✓
        back_to_f = result * 9 / 5 + 32
        assert (
            int(back_to_f + 0.5) == 81
        ), f"Round-trip should recover 81°F, got {int(back_to_f + 0.5)}"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_fahrenheit_82_preserves_correctly(self, mock_get):
        """°F市场: 82°F → 27.778°C → T228反转 = 82°F（非28°C→82.4→82的旧错误路径）"""
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"temperature": 82, "temperatureMaxSince7am": 82}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="F")
        back_to_f = result * 9 / 5 + 32
        assert int(back_to_f + 0.5) == 82

    @patch("src.utils.weather_fetcher.requests.get")
    def test_fahrenheit_boundary_85(self, mock_get):
        """°F市场: 85°F → 29.444°C → T228反转 = 85°F（range 84-85 不触发死刑）"""
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"temperature": 85, "temperatureMaxSince7am": 85}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="F")
        back_to_f = result * 9 / 5 + 32
        assert int(back_to_f + 0.5) == 85

    @patch("src.utils.weather_fetcher.requests.get")
    def test_running_max_tracks_daily_high(self, mock_get):
        """since7am=None时，running_max应追踪当日最高"""
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        def make_resp(temp):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"temperature": temp}  # since7am missing
            r.raise_for_status = MagicMock()
            return r

        from src.utils import weather_fetcher

        # 第一次: current=80°F
        weather_fetcher._WU_API_CACHE.clear()
        mock_get.return_value = make_resp(80)
        r1 = _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="F")

        # 第二次: current=82°F (温度上升)
        weather_fetcher._WU_API_CACHE.clear()
        mock_get.return_value = make_resp(82)
        r2 = _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="F")

        # 第三次: current=79°F (下午降温) — running_max 应保持82°F
        weather_fetcher._WU_API_CACHE.clear()
        mock_get.return_value = make_resp(79)
        r3 = _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="F")

        expected_82_c = (82 - 32) * 5 / 9
        assert (
            abs(r3 - expected_82_c) < 0.01
        ), f"Running max should keep 82°F({expected_82_c:.2f}°C) after cooling, got {r3}"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_running_max_resets_on_date_change(self, mock_get):
        """running_max应在日期变更时重置"""
        from src.utils import weather_fetcher
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"temperature": 30}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        # 设置旧日期的 running_max 为 35°C
        _rmax_key = "25.76,-80.19:m"
        weather_fetcher._WU_RUNNING_MAX[_rmax_key] = ("2026-01-01", 35.0)

        weather_fetcher._WU_API_CACHE.clear()
        result = _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="C")
        # 日期不同(今天 vs 2026-01-01), running_max 应重置为当前值 30
        assert result == 30.0, f"Running max should reset on new day, got {result}"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_running_max_not_applied_to_since7am(self, mock_get):
        """since7am有值时不应使用running_max"""
        from src.utils import weather_fetcher
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        # 先设置一个高的 running_max
        _rmax_key = "25.76,-80.19:m"
        _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        weather_fetcher._WU_RUNNING_MAX[_rmax_key] = (_today, 40.0)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "temperature": 26,
            "temperatureMaxSince7am": 28,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        weather_fetcher._WU_API_CACHE.clear()
        result = _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="C")
        assert (
            result == 28.0
        ), f"Since7am available: should use 28, not running_max 40, got {result}"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_cache_key_separates_units(self, mock_get):
        """不同 native_unit 请求应使用不同缓存 key"""
        from src.utils import weather_fetcher
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        mock_resp_f = MagicMock()
        mock_resp_f.status_code = 200
        mock_resp_f.json.return_value = {
            "temperature": 81,
            "temperatureMaxSince7am": 81,
        }
        mock_resp_f.raise_for_status = MagicMock()

        mock_resp_c = MagicMock()
        mock_resp_c.status_code = 200
        mock_resp_c.json.return_value = {
            "temperature": 27,
            "temperatureMaxSince7am": 27,
        }
        mock_resp_c.raise_for_status = MagicMock()

        # First call: °F
        mock_get.return_value = mock_resp_f
        r_f = _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="F")

        # Second call: °C — should NOT use °F cache
        mock_get.return_value = mock_resp_c
        r_c = _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="C")

        assert abs(r_f - (81 - 32) * 5 / 9) < 0.01, f"°F result wrong: {r_f}"
        assert r_c == 27.0, f"°C result should be 27.0, got {r_c} (cache leak?)"
        assert mock_get.call_count == 2, "Should make 2 separate API calls"

    @patch("src.utils.weather_fetcher.requests.get")
    def test_fahrenheit_sanity_check_rejects_impossible(self, mock_get):
        """°F模式: 超过合理范围(-76~140°F)应返回None"""
        from src.utils.weather_fetcher import _fetch_wu_api_temp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "temperature": 200,
            "temperatureMaxSince7am": 200,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_wu_api_temp(25.76, -80.19, "Miami", native_unit="F")
        assert result is None, "200°F should be rejected as impossible"
