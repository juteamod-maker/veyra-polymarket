"""
tests/test_map_routes.py — T146 全球事件看板 API 单元测试
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ─────────────────────────────────────────────────────────────────

MOCK_WEATHER_CITIES = [
    {
        "name": "Tokyo",
        "slug": "tokyo",
        "lat": 35.5494,
        "lon": 139.7798,
        "tz": "Asia/Tokyo",
        "enabled": True,
    },
    {
        "name": "Hong Kong",
        "slug": "hong-kong",
        "lat": 22.308,
        "lon": 113.9185,
        "tz": "Asia/Hong_Kong",
        "enabled": True,
    },
    {
        "name": "Amsterdam",
        "slug": "amsterdam",
        "lat": 52.3676,
        "lon": 4.9041,
        "tz": "Europe/Amsterdam",
        "enabled": True,
    },
]

MOCK_CONFIG = {"weather_cities": {"cities": MOCK_WEATHER_CITIES}}


@pytest.fixture
def mock_config(tmp_path):
    """Mock config.json in a temporary directory."""
    cfg_path = tmp_path / "config" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(MOCK_CONFIG), encoding="utf-8")
    return cfg_path


@pytest.fixture
def mock_markers_file(tmp_path):
    """Provide a temporary map_markers.json path."""
    return tmp_path / "data" / "map_markers.json"


# ── Import helpers with patched paths ─────────────────────────────────────────


def get_map_routes_module(monkeypatch, mock_config, mock_markers_file):
    """Import and patch map_routes module for testing."""
    # We need to patch ROOT so files are found at tmp locations
    with patch.dict("sys.modules", {}):
        import importlib

        import scripts.dashboard_routes.map_routes as mr

        importlib.reload(mr)  # reload with fresh state

        monkeypatch.setattr(mr, "MARKERS_FILE", mock_markers_file)

        # Patch _get_weather_cities to return mock cities
        monkeypatch.setattr(mr, "_get_weather_cities", lambda: MOCK_WEATHER_CITIES)

        return mr


# ── Tests: _extract_city_from_question ───────────────────────────────────────


class TestExtractCityFromQuestion:
    def test_basic_extraction(self):
        from scripts.dashboard_routes.map_routes import \
            _extract_city_from_question

        q = "Will the highest temperature in Tokyo be 30°C on April 10?"
        result = _extract_city_from_question(q, MOCK_WEATHER_CITIES)
        assert result == "Tokyo"

    def test_hong_kong(self):
        from scripts.dashboard_routes.map_routes import \
            _extract_city_from_question

        q = "Will the highest temperature in Hong Kong be 27°C on April 15?"
        result = _extract_city_from_question(q, MOCK_WEATHER_CITIES)
        assert result == "Hong Kong"

    def test_no_match(self):
        from scripts.dashboard_routes.map_routes import \
            _extract_city_from_question

        q = "Will Bitcoin reach $100k?"
        result = _extract_city_from_question(q, MOCK_WEATHER_CITIES)
        assert result is None

    def test_empty_question(self):
        from scripts.dashboard_routes.map_routes import \
            _extract_city_from_question

        result = _extract_city_from_question("", MOCK_WEATHER_CITIES)
        assert result is None

    def test_none_question(self):
        from scripts.dashboard_routes.map_routes import \
            _extract_city_from_question

        result = _extract_city_from_question(None, MOCK_WEATHER_CITIES)
        assert result is None

    def test_exceed_keyword(self):
        from scripts.dashboard_routes.map_routes import \
            _extract_city_from_question

        q = "Will the highest temperature in Amsterdam exceed 25°C on April 20?"
        result = _extract_city_from_question(q, MOCK_WEATHER_CITIES)
        assert result == "Amsterdam"


# ── Tests: _load_markers / _save_markers ─────────────────────────────────────


class TestMarkersIO:
    def test_load_empty_when_file_not_exist(self, tmp_path):
        from scripts.dashboard_routes import map_routes as mr

        original = mr.MARKERS_FILE
        try:
            mr.MARKERS_FILE = tmp_path / "nonexistent.json"
            assert mr._load_markers() == []
        finally:
            mr.MARKERS_FILE = original

    def test_save_and_load_roundtrip(self, tmp_path):
        from scripts.dashboard_routes import map_routes as mr

        original = mr.MARKERS_FILE
        try:
            mr.MARKERS_FILE = tmp_path / "data" / "test_markers.json"
            markers = [
                {"id": "m1", "city": "Tokyo", "lat": 35.5, "lon": 139.7, "events": []}
            ]
            mr._save_markers(markers)
            loaded = mr._load_markers()
            assert len(loaded) == 1
            assert loaded[0]["city"] == "Tokyo"
        finally:
            mr.MARKERS_FILE = original

    def test_save_corrupted_file_returns_empty(self, tmp_path):
        from scripts.dashboard_routes import map_routes as mr

        original = mr.MARKERS_FILE
        try:
            bad_file = tmp_path / "bad.json"
            bad_file.write_text("NOT JSON", encoding="utf-8")
            mr.MARKERS_FILE = bad_file
            assert mr._load_markers() == []
        finally:
            mr.MARKERS_FILE = original


# ── Tests: _build_auto_markers ────────────────────────────────────────────────


class TestBuildAutoMarkers:
    def test_returns_list(self):
        from scripts.dashboard_routes import map_routes as mr

        # patch db_connect to return no rows
        with patch.object(mr, "db_connect") as mock_db:
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value.fetchall.return_value = []
            mock_db.return_value = mock_conn

            result = mr._build_auto_markers(MOCK_WEATHER_CITIES)
            assert isinstance(result, list)

    def test_builds_marker_for_weather_bet(self):
        from scripts.dashboard_routes import map_routes as mr

        fake_rows = [
            (
                1,
                "Will the highest temperature in Tokyo be 30°C on April 10?",
                "YES",
                0.65,
                0.90,
                "2026-04-05T12:00",
                "cond_001",
                "tokyo-april-10",
                "highest-temperature-in-tokyo-on-april-10-2026",
            ),
        ]
        with patch.object(mr, "db_connect") as mock_db:
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value.fetchall.return_value = fake_rows
            mock_db.return_value = mock_conn

            result = mr._build_auto_markers(MOCK_WEATHER_CITIES)
            assert len(result) == 1
            assert result[0]["city"] == "Tokyo"
            assert result[0]["lat"] == 35.5494
            assert result[0]["is_auto"] is True
            assert len(result[0]["events"]) == 1
            assert result[0]["events"][0]["is_bet"] is True
            assert (
                result[0]["events"][0]["event_slug"]
                == "highest-temperature-in-tokyo-on-april-10-2026"
            )

    def test_multiple_bets_same_city_one_marker(self):
        from scripts.dashboard_routes import map_routes as mr

        fake_rows = [
            (
                1,
                "Will the highest temperature in Tokyo be 30°C on April 10?",
                "YES",
                0.65,
                0.90,
                "2026-04-05",
                "c1",
                "slug1",
                "event1",
            ),
            (
                2,
                "Will the highest temperature in Tokyo be 32°C on April 11?",
                "NO",
                0.40,
                0.10,
                "2026-04-06",
                "c2",
                "slug2",
                "event2",
            ),
        ]
        with patch.object(mr, "db_connect") as mock_db:
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value.fetchall.return_value = fake_rows
            mock_db.return_value = mock_conn

            result = mr._build_auto_markers(MOCK_WEATHER_CITIES)
            assert len(result) == 1  # one marker for Tokyo
            assert len(result[0]["events"]) == 2  # two events

    def test_db_failure_returns_empty(self):
        from scripts.dashboard_routes import map_routes as mr

        with patch.object(mr, "db_connect", side_effect=Exception("DB error")):
            result = mr._build_auto_markers(MOCK_WEATHER_CITIES)
            assert result == []

    def test_unmatched_question_skipped(self):
        from scripts.dashboard_routes import map_routes as mr

        fake_rows = [
            (
                1,
                "Will Bitcoin reach $100k?",
                "YES",
                0.5,
                0.7,
                "2026-04-05",
                "c1",
                "slug1",
                "event1",
            ),
        ]
        with patch.object(mr, "db_connect") as mock_db:
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value.fetchall.return_value = fake_rows
            mock_db.return_value = mock_conn

            result = mr._build_auto_markers(MOCK_WEATHER_CITIES)
            assert result == []


# ── Tests: FastAPI endpoints (via TestClient) ─────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient with patched file paths.

    T901: the map-marker write endpoints now require admin / super_admin, so this
    fixture injects an admin identity. `test_post_marker_requires_admin` covers the
    non-admin rejection path.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from scripts.dashboard_routes import map_routes as mr

    # Override markers file
    monkeypatch.setattr(mr, "MARKERS_FILE", tmp_path / "data" / "markers.json")

    # Override _get_weather_cities
    monkeypatch.setattr(mr, "_get_weather_cities", lambda: MOCK_WEATHER_CITIES)

    # Override _build_auto_markers to return empty (no DB needed)
    monkeypatch.setattr(mr, "_build_auto_markers", lambda wc: [])

    # T901: inject an admin identity for the write endpoints
    monkeypatch.setattr(
        mr,
        "get_request_identity",
        lambda _request: {
            "authenticated": True,
            "role": "admin",
            "wallet_address": "0x" + "0" * 40,
            "is_admin": True,
        },
    )

    app = FastAPI()
    app.include_router(mr.router)
    return TestClient(app)


class TestMapEndpoints:
    def test_post_marker_requires_admin(self, tmp_path, monkeypatch):
        """T901 回归：非 admin 调用写接口必须 403，且不得写入 marker 文件。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from scripts.dashboard_routes import map_routes as mr

        markers_file = tmp_path / "data" / "markers.json"
        monkeypatch.setattr(mr, "MARKERS_FILE", markers_file)
        monkeypatch.setattr(mr, "_get_weather_cities", lambda: MOCK_WEATHER_CITIES)
        monkeypatch.setattr(mr, "_build_auto_markers", lambda wc: [])
        monkeypatch.setattr(
            mr,
            "get_request_identity",
            lambda _request: {
                "authenticated": True,
                "role": "manual_user",
                "wallet_address": "0x" + "1" * 40,
                "is_admin": False,
            },
        )

        app = FastAPI()
        app.include_router(mr.router)
        c = TestClient(app)

        resp = c.post(
            "/api/map/markers",
            json={"city": "Tokyo", "lat": 35.5, "lon": 139.7, "category": "weather"},
        )
        assert resp.status_code == 403
        assert not markers_file.exists()

    def test_get_markers_empty(self, client):
        resp = client.get("/api/map/markers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["count"] == 0
        assert data["markers"] == []

    def test_post_marker_creates_new(self, client):
        payload = {
            "city": "Tokyo",
            "lat": 35.5494,
            "lon": 139.7798,
            "category": "weather",
            "events": [
                {
                    "title": "Tokyo April 10",
                    "description": "Will temp exceed 30°C?",
                    "is_bet": False,
                }
            ],
        }
        resp = client.post("/api/map/markers", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["action"] == "created"
        assert data["marker"]["city"] == "Tokyo"
        assert len(data["marker"]["events"]) == 1

    def test_post_marker_requires_city(self, client):
        resp = client.post("/api/map/markers", json={"lat": 35.5, "lon": 139.7})
        assert resp.status_code == 400

    def test_post_marker_requires_lat_lon(self, client):
        resp = client.post("/api/map/markers", json={"city": "Tokyo"})
        assert resp.status_code == 400

    def test_post_and_get_roundtrip(self, client):
        payload = {
            "city": "Amsterdam",
            "lat": 52.3676,
            "lon": 4.9041,
            "category": "custom",
            "events": [{"title": "Test Event", "description": "Test", "is_bet": False}],
        }
        client.post("/api/map/markers", json=payload)

        resp = client.get("/api/map/markers")
        assert resp.status_code == 200
        data = resp.json()
        # Should have the new manual marker
        cities = [m["city"] for m in data["markers"]]
        assert "Amsterdam" in cities

    def test_post_marker_merges_same_city(self, client):
        payload = {
            "city": "Berlin",
            "lat": 52.52,
            "lon": 13.405,
            "category": "custom",
            "events": [{"title": "Event 1", "description": "", "is_bet": False}],
        }
        r1 = client.post("/api/map/markers", json=payload)
        assert r1.json()["action"] == "created"

        payload["events"] = [{"title": "Event 2", "description": "", "is_bet": False}]
        r2 = client.post("/api/map/markers", json=payload)
        assert r2.json()["action"] == "merged"

    def test_delete_marker(self, client):
        # Create
        payload = {
            "city": "Paris",
            "lat": 48.85,
            "lon": 2.35,
            "category": "custom",
            "events": [],
        }
        r = client.post("/api/map/markers", json=payload)
        marker_id = r.json()["marker"]["id"]

        # Delete
        resp = client.delete(f"/api/map/markers/{marker_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify gone
        get_resp = client.get("/api/map/markers")
        cities = [m["city"] for m in get_resp.json()["markers"]]
        assert "Paris" not in cities

    def test_delete_nonexistent_marker(self, client):
        resp = client.delete("/api/map/markers/nonexistent_id")
        assert resp.status_code == 404

    def test_get_weather_cities(self, client):
        resp = client.get("/api/map/weather-cities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["count"] == len(MOCK_WEATHER_CITIES)
        names = [c["name"] for c in data["cities"]]
        assert "Tokyo" in names
        assert "Hong Kong" in names

    def test_get_city_bets_no_db_rows(self, client):
        from scripts.dashboard_routes import map_routes as mr

        with patch.object(mr, "db_connect") as mock_db:
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value.fetchall.return_value = []
            mock_db.return_value = mock_conn

            resp = client.get("/api/map/city-bets/Tokyo")
            assert resp.status_code == 200
            data = resp.json()
            assert data["city"] == "Tokyo"
            assert data["bets"] == []

    def test_get_temperature_unavailable(self, client):
        from scripts.dashboard_routes import map_routes as mr

        with patch.object(mr, "_get_city_info_sync", return_value=None):
            resp = client.get("/api/map/cityinfo/Tokyo")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "unavailable"
            assert data["today_max_c"] is None

    def test_get_temperature_success(self, client):
        from scripts.dashboard_routes import map_routes as mr

        mock_data = {
            "today_max_c": 25.3,
            "today_max_f": 77.5,
            "timezone": "Asia/Tokyo",
            "utc_offset_s": 0,
            "source": "wu-v1-historical",
        }
        with patch.object(mr, "_get_city_info_sync", return_value=mock_data):
            resp = client.get("/api/map/cityinfo/Tokyo")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["today_max_c"] == 25.3
            assert data["today_max_f"] == 77.5
            assert data["source"] == "wu-v1-historical"
            resp = client.get("/api/map/cityinfo/Tokyo")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["today_max_c"] == 25.3
            assert data["today_max_f"] == 77.5
            assert data["source"] == "wu-v1-historical"

    def test_cityinfo_peak_window_in_range(self, client):
        """T182: local_time 在 peak_window 范围内时 is_in_peak_window=True，且返回 settlement_url。"""
        from scripts.dashboard_routes import map_routes as mr

        mr._CITYINFO_CACHE.pop("tokyo", None)
        mock_data = {
            "today_max_c": 22.8,
            "today_max_f": 73.0,
            "current_temp_c": 20.4,
            "timezone": "Asia/Tokyo",
            "local_time": "2026-04-11T13:30",  # 13h，在 10~17 窗口内
            "utc_offset_s": 0,
            "source": "wu-v1-historical",
        }
        with patch.object(mr, "_get_city_info_sync", return_value=mock_data):
            resp = client.get("/api/map/cityinfo/Tokyo")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["is_in_peak_window"] is True
        assert "settlement_url" in data  # settlement_url key is present
        assert data["current_temp_c"] == 20.4

    def test_cityinfo_peak_window_outside_range(self, client):
        """T182: local_time 不在 peak_window 范围时 is_in_peak_window=False。"""
        from scripts.dashboard_routes import map_routes as mr

        mock_data = {
            "today_max_c": 22.8,
            "today_max_f": 73.0,
            "current_temp_c": 18.0,
            "timezone": "Asia/Tokyo",
            "local_time": "2026-04-11T03:00",  # 3h，在 10~17 窗口外
            "utc_offset_s": 0,
            "source": "wu-v1-historical",
        }
        # Clear cache so fresh request is made
        mr._CITYINFO_CACHE.pop("tokyo", None)
        with patch.object(mr, "_get_city_info_sync", return_value=mock_data):
            resp = client.get("/api/map/cityinfo/Tokyo")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_in_peak_window"] is False

    def test_cityinfo_peak_window_missing_local_time(self, client):
        """T182: local_time 缺失时 is_in_peak_window=False（安全默认）。"""
        from scripts.dashboard_routes import map_routes as mr

        mock_data = {
            "today_max_c": 22.8,
            "today_max_f": 73.0,
            "current_temp_c": None,
            "timezone": "Asia/Tokyo",
            # local_time intentionally missing
            "utc_offset_s": 0,
            "source": "open-meteo-hourly",
        }
        mr._CITYINFO_CACHE.pop("tokyo", None)
        with patch.object(mr, "_get_city_info_sync", return_value=mock_data):
            resp = client.get("/api/map/cityinfo/Tokyo")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_in_peak_window"] is False


class TestCityInfoSync:
    """T165: 验证 _get_city_info_sync 使用 units=e 获取原生华氏度。"""

    def test_wu_v1_returns_native_fahrenheit(self):
        """P1 WU API 用 units=e 返回原生 °F，today_max_f 无转换误差。"""
        from scripts.dashboard_routes import map_routes as mr

        # WU v1 API units=e 返回整数华氏度
        mock_wu_resp = MagicMock()
        mock_wu_resp.status_code = 200
        mock_wu_resp.raise_for_status = MagicMock()
        mock_wu_resp.json.return_value = {
            "observations": [
                {"temp": 39},
                {"temp": 44},
                {"temp": 49},  # max = 49°F (native)
                {"temp": 46},
            ]
        }

        with patch("src.utils.weather_fetcher._WU_API_KEYS", ["test_key"]):
            with patch("requests.get", return_value=mock_wu_resp) as mock_get:
                result = mr._get_city_info_sync(
                    lat=40.7772,
                    lon=-73.8726,
                    icao="KLGA",
                    city_name="NYC",
                    timezone_iana="America/New_York",
                    settlement_source="wunderground",
                )

        assert result is not None
        assert result["today_max_f"] == 49.0  # 原生 WU °F，无转换误差
        assert result["today_max_c"] == round((49 - 32) * 5 / 9, 1)  # 9.4°C
        assert result["source"] == "wu-v1-historical"
        # 验证 API 调用使用 units=e
        call_url = mock_get.call_args[0][0]
        assert "units=e" in call_url

    def test_wu_v1_celsius_fahrenheit_consistency(self):
        """验证 C→F 反算一致性：F=77 → C=25.0, 验证后往返转换无误。"""
        from scripts.dashboard_routes import map_routes as mr

        mock_wu_resp = MagicMock()
        mock_wu_resp.status_code = 200
        mock_wu_resp.raise_for_status = MagicMock()
        mock_wu_resp.json.return_value = {
            "observations": [{"temp": 68}, {"temp": 72}, {"temp": 77}]
        }

        with patch("src.utils.weather_fetcher._WU_API_KEYS", ["test_key"]):
            with patch("requests.get", return_value=mock_wu_resp):
                result = mr._get_city_info_sync(
                    lat=41.9742,
                    lon=-87.9073,
                    city_name="Chicago",
                    timezone_iana="America/Chicago",
                )

        assert result["today_max_f"] == 77.0
        assert result["today_max_c"] == 25.0  # (77-32)*5/9 = 25.0

    def test_open_meteo_fallback_returns_both_units(self):
        """P2 Open-Meteo 降级时仍返回 today_max_f（从 C 转换）。"""
        from scripts.dashboard_routes import map_routes as mr

        # WU 失败，降级到 Open-Meteo
        wu_resp = MagicMock()
        wu_resp.status_code = 401  # API key expired

        om_resp = MagicMock()
        om_resp.status_code = 200
        om_resp.raise_for_status = MagicMock()
        om_resp.json.return_value = {
            "hourly": {
                "time": [
                    "2026-04-09T00:00",
                    "2026-04-09T06:00",
                    "2026-04-09T12:00",
                ],
                "temperature_2m": [5.2, 8.1, 12.3],
            }
        }

        def side_effect(url, **kwargs):
            if "weather.com" in url:
                return wu_resp
            return om_resp

        with patch("src.utils.weather_fetcher._WU_API_KEYS", ["bad_key"]):
            with patch("requests.get", side_effect=side_effect):
                result = mr._get_city_info_sync(
                    lat=40.7772,
                    lon=-73.8726,
                    city_name="NYC",
                    timezone_iana="America/New_York",
                )

        assert result is not None
        assert result["today_max_c"] == 12.3
        assert result["today_max_f"] == round(12.3 * 9 / 5 + 32, 1)
        assert result["source"] == "open-meteo-hourly"


class TestMetarTafFallbacks:
    def test_metar_fallback_uses_weather_fetcher_chain(self):
        from scripts.dashboard_routes import map_routes as mr

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = ""

        fallback_obs = {
            "temp_c": 19.6,
            "dewpoint_c": 12.1,
            "obs_time": "2026-04-16T09:00Z",
            "raw": "RJTT 160900Z 02005KT 10SM FEW030 20/12 A2998",
            "source": "tgftp.nws.noaa.gov",
        }

        with patch("requests.get", return_value=mock_resp):
            with patch(
                "src.utils.weather_fetcher._fetch_metar_observation",
                return_value=fallback_obs,
            ):
                text = mr._fetch_and_decode_metar("RJTT")

        assert "来源 tgftp.nws.noaa.gov" in text
        assert "气温 19.6" in text

    def test_taf_fallback_uses_nws_raw(self):
        from scripts.dashboard_routes import map_routes as mr

        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = ""

        with patch("requests.get", return_value=mock_resp):
            with patch.object(
                mr,
                "_fetch_nws_raw_taf",
                return_value=(
                    "TAF RJTT 160500Z 1606/1712 02008KT 9999 FEW020 "
                    "BECMG 1610/1612 16012KT"
                ),
            ):
                text = mr._fetch_and_decode_taf("RJTT")

        assert text.startswith("TAF原文(降级源NWS)：")
        assert "TAF RJTT" in text
