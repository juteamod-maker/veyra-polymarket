"""
test_weather_fetcher.py — 天气数据获取模块测试
重点测试 Open-Meteo 预报补充逻辑。
"""

from unittest.mock import MagicMock, patch

import pytest
from src.utils.weather_fetcher import _enrich_with_open_meteo


class TestEnrichWithOpenMeteo:
    """测试 Open-Meteo 预报补充函数。"""

    def _mock_open_meteo_response(self):
        """模拟 Open-Meteo API 返回数据。"""
        return {
            "daily": {
                "time": ["2026-03-15", "2026-03-16", "2026-03-17"],
                "temperature_2m_max": [10.5, 12.1, 8.3],
                "temperature_2m_min": [3.2, 5.0, 1.8],
            }
        }

    @patch("src.utils.weather_fetcher.requests.get")
    def test_enriches_forecast_3day(self, mock_get):
        """补充后 forecast_3day 应有3天数据。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._mock_open_meteo_response()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        data = {"latitude": "40.71", "longitude": "-74.01", "forecast_3day": []}
        _enrich_with_open_meteo(data)

        assert len(data["forecast_3day"]) == 3
        assert "2026-03-15" in data["forecast_3day"][0]
        assert "10.5°C" in data["forecast_3day"][0]

    @patch("src.utils.weather_fetcher.requests.get")
    def test_sets_tomorrow_fields(self, mock_get):
        """补充后应设置 tomorrow_max_c/f 和 tomorrow_min_c/f。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._mock_open_meteo_response()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        data = {"latitude": "40.71", "longitude": "-74.01", "forecast_3day": []}
        _enrich_with_open_meteo(data)

        assert data["tomorrow_max_c"] == 12.1
        assert data["tomorrow_min_c"] == 5.0
        assert data["tomorrow_max_f"] == round(12.1 * 9 / 5 + 32, 1)
        assert data["tomorrow_min_f"] == round(5.0 * 9 / 5 + 32, 1)

    @patch("src.utils.weather_fetcher.requests.get")
    def test_updates_forecast_max_min(self, mock_get):
        """补充后应更新 forecast_max_c/forecast_min_c（今日数据）。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._mock_open_meteo_response()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        data = {
            "latitude": "40.71",
            "longitude": "-74.01",
            "forecast_3day": [],
            "forecast_max_c": 0,
            "forecast_min_c": 0,
        }
        _enrich_with_open_meteo(data)

        assert data["forecast_max_c"] == 10.5
        assert data["forecast_min_c"] == 3.2

    def test_no_latlon_skips(self):
        """没有经纬度时不调用API。"""
        data = {"forecast_3day": []}
        _enrich_with_open_meteo(data)
        assert data["forecast_3day"] == []

    @patch("src.utils.weather_fetcher.requests.get")
    def test_api_failure_graceful(self, mock_get):
        """API 失败时不崩溃，保留原有数据。"""
        import requests

        mock_get.side_effect = requests.RequestException("timeout")

        data = {
            "latitude": "40.71",
            "longitude": "-74.01",
            "forecast_3day": ["original"],
            "raw_summary": "original summary",
        }
        _enrich_with_open_meteo(data)

        # 原数据应保持不变
        assert data["forecast_3day"] == ["original"]
        assert "tomorrow_max_c" not in data

    @patch("src.utils.weather_fetcher.requests.get")
    def test_updates_raw_summary(self, mock_get):
        """补充后 raw_summary 应包含 Open-Meteo 预报信息。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._mock_open_meteo_response()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        data = {
            "latitude": "40.71",
            "longitude": "-74.01",
            "forecast_3day": [],
            "raw_summary": "当前: 15°C/59°F, Cloudy",
        }
        _enrich_with_open_meteo(data)

        assert "Open-Meteo" in data["raw_summary"]
        assert "2026-03-16" in data["raw_summary"]

    @patch("src.utils.weather_fetcher.requests.get")
    def test_replaces_existing_forecast_in_summary(self, mock_get):
        """如果 raw_summary 已有旧预报，应替换而非追加。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._mock_open_meteo_response()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        data = {
            "latitude": "40.71",
            "longitude": "-74.01",
            "forecast_3day": [],
            "raw_summary": "当前: 15°C | 预报: old_data",
        }
        _enrich_with_open_meteo(data)

        assert "old_data" not in data["raw_summary"]
        assert "Open-Meteo" in data["raw_summary"]

    @patch("src.utils.weather_fetcher.requests.get")
    def test_fahrenheit_conversion_accuracy(self, mock_get):
        """°F 转换精度验证。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "daily": {
                "time": ["2026-03-15", "2026-03-16"],
                "temperature_2m_max": [0.0, 100.0],
                "temperature_2m_min": [-10.0, 20.0],
            }
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        data = {"latitude": "40.71", "longitude": "-74.01", "forecast_3day": []}
        _enrich_with_open_meteo(data)

        assert data["tomorrow_max_f"] == 212.0  # 100°C = 212°F
        assert data["tomorrow_min_f"] == 68.0  # 20°C = 68°F
        assert "32.0°F" in data["forecast_3day"][0]  # 0°C = 32°F


class TestEcmwfEnsemble:
    """ECMWF ensemble member 解析测试。"""

    def setup_method(self):
        import src.utils.weather_fetcher as wf

        wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
        wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
        wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0  # 重置熔断器
        wf._OPEN_METEO_LAST_429_TS = 0.0  # 重置 IP 级 429 跳过

    @patch("src.utils.weather_fetcher.get")
    @patch("src.utils.weather_fetcher.requests.get")
    def test_ecmwf_ifs025_members_start_at_1(self, mock_get, mock_cfg):
        """ecmwf_ifs025 成员从 member01 开始（无 member00）。"""
        from src.utils.weather_fetcher import _fetch_ecmwf_ensemble

        mock_cfg.return_value = {
            "ecmwf_model": "ecmwf_ifs025",
            "ecmwf_members": 50,
            "ecmwf_cache_ttl_seconds": 0,
            "api_timeout": 10,
            "min_members_for_confidence": 5,
        }

        # 模拟 ecmwf_ifs025 的响应（member01-member05, 无 member00）
        daily = {
            "time": ["2026-03-23"],
        }
        for i in range(1, 6):
            daily[f"temperature_2m_max_member{i:02d}"] = [10.0 + i]

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"daily": daily}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_ecmwf_ensemble(43.68, -79.63, "temp_max", 3)
        assert result is not None
        assert "2026-03-23" in result
        assert len(result["2026-03-23"]) == 5  # 5 members, not 0
