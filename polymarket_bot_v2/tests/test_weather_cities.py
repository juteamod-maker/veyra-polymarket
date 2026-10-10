"""
tests/test_weather_cities.py — T276 天气城市配置参数化测试
覆盖: config城市加载、_get_city_coords config优先、get_city_local_hour config优先、
      outcome_tracker get_city_coords config优先、dashboard API 增删改查
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. weather_fetcher: _load_config_city + _get_city_coords config优先
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLoadConfigCity:
    """_load_config_city 从 config.json weather_cities 查找城市。"""

    MOCK_CITIES = [
        {
            "name": "Test City",
            "slug": "test-city",
            "lat": 40.0,
            "lon": -74.0,
            "icao": "KTST",
            "timezone": "America/New_York",
            "utc_offset": -4,
            "enabled": True,
        },
        {
            "name": "Disabled Town",
            "slug": "disabled-town",
            "lat": 35.0,
            "lon": -80.0,
            "icao": "KDIS",
            "timezone": "America/New_York",
            "utc_offset": -4,
            "enabled": False,
        },
    ]

    @patch("src.utils.weather_fetcher.get")
    def test_find_by_slug(self, mock_get):
        from src.utils.weather_fetcher import _load_config_city

        mock_get.return_value = self.MOCK_CITIES
        result = _load_config_city("test-city")
        assert result is not None
        assert result["slug"] == "test-city"
        assert result["lat"] == 40.0

    @patch("src.utils.weather_fetcher.get")
    def test_find_by_name(self, mock_get):
        from src.utils.weather_fetcher import _load_config_city

        mock_get.return_value = self.MOCK_CITIES
        result = _load_config_city("test city")
        assert result is not None
        assert result["name"] == "Test City"

    @patch("src.utils.weather_fetcher.get")
    def test_space_to_hyphen_slug(self, mock_get):
        """空格自动转连字符匹配 slug。"""
        from src.utils.weather_fetcher import _load_config_city

        mock_get.return_value = self.MOCK_CITIES
        # "test city" → "test-city"
        result = _load_config_city("test city")
        assert result is not None

    @patch("src.utils.weather_fetcher.get")
    def test_disabled_city_skipped(self, mock_get):
        from src.utils.weather_fetcher import _load_config_city

        mock_get.return_value = self.MOCK_CITIES
        result = _load_config_city("disabled-town")
        assert result is None

    @patch("src.utils.weather_fetcher.get")
    def test_not_found(self, mock_get):
        from src.utils.weather_fetcher import _load_config_city

        mock_get.return_value = self.MOCK_CITIES
        result = _load_config_city("nonexistent")
        assert result is None

    @patch("src.utils.weather_fetcher.get")
    def test_empty_config(self, mock_get):
        from src.utils.weather_fetcher import _load_config_city

        mock_get.return_value = None
        result = _load_config_city("test-city")
        assert result is None


class TestGetCityCoordsConfigPriority:
    """_get_city_coords 优先从 config 加载坐标。"""

    @patch("src.utils.weather_fetcher._load_config_city")
    def test_config_city_used_first(self, mock_load):
        from src.utils.weather_fetcher import _COORD_CACHE, _get_city_coords

        # 清除缓存
        _COORD_CACHE.pop("config-test", None)

        mock_load.return_value = {
            "name": "Config Test",
            "slug": "config-test",
            "lat": 99.0,
            "lon": -99.0,
            "icao": "KCFG",
        }
        result = _get_city_coords("config-test")
        assert result == (99.0, -99.0)
        # 清理
        _COORD_CACHE.pop("config-test", None)

    @patch("src.utils.weather_fetcher._load_config_city")
    def test_fallback_to_station_map(self, mock_load):
        from src.utils.weather_fetcher import _COORD_CACHE, _get_city_coords

        _COORD_CACHE.pop("chicago", None)
        mock_load.return_value = None
        result = _get_city_coords("chicago")
        # chicago 在 _STATION_MAP 中
        assert result is not None
        assert abs(result[0] - 41.9742) < 0.1
        _COORD_CACHE.pop("chicago", None)

    @patch("src.utils.weather_fetcher._load_config_city")
    def test_cache_hit(self, mock_load):
        from src.utils.weather_fetcher import _COORD_CACHE, _get_city_coords

        _COORD_CACHE["cached-city"] = (12.0, 34.0)
        result = _get_city_coords("cached-city")
        assert result == (12.0, 34.0)
        mock_load.assert_not_called()
        _COORD_CACHE.pop("cached-city", None)


class TestGetCityLocalHourConfigPriority:
    """get_city_local_hour 优先从 config 加载 utc_offset。"""

    @patch("src.utils.weather_fetcher._load_config_city")
    def test_config_offset_used(self, mock_load):
        from src.utils.weather_fetcher import get_city_local_hour

        mock_load.return_value = {"utc_offset": 8}
        hour = get_city_local_hour("fake-city")
        assert isinstance(hour, int)
        assert 0 <= hour <= 23

    @patch("src.utils.weather_fetcher._load_config_city")
    def test_fallback_to_offset_map(self, mock_load):
        from src.utils.weather_fetcher import get_city_local_hour

        mock_load.return_value = None
        hour = get_city_local_hour("tokyo")  # tokyo 在 _CITY_UTC_OFFSET 中
        assert isinstance(hour, int)
        assert 0 <= hour <= 23


class TestGetStationInfoConfigPriority:
    """get_station_info 优先从 config 加载。"""

    @patch("src.utils.weather_fetcher._load_config_city")
    def test_config_station_info(self, mock_load):
        from src.utils.weather_fetcher import get_station_info

        mock_load.return_value = {
            "name": "Test Cfg",
            "icao": "KCFG",
            "lat": 40.5,
            "lon": -74.5,
        }
        result = get_station_info("test-cfg")
        assert result is not None
        assert result[0] == "KCFG"
        assert result[1] == 40.5
        assert result[2] == -74.5
        assert result[3] == "Test Cfg"

    @patch("src.utils.weather_fetcher._load_config_city")
    def test_fallback_station_map(self, mock_load):
        from src.utils.weather_fetcher import get_station_info

        mock_load.return_value = None
        result = get_station_info("seattle")
        assert result is not None
        assert result[0] == "KSEA"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. outcome_tracker: get_city_coords config优先
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestOutcomeTrackerGetCityCoords:
    """outcome_tracker.get_city_coords 优先从 config 查找。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_config_priority(self, mock_get):
        from src.simulation.outcome_tracker import get_city_coords

        mock_get.return_value = [
            {
                "name": "Custom City",
                "slug": "custom-city",
                "lat": 50.0,
                "lon": 10.0,
                "enabled": True,
            }
        ]
        result = get_city_coords("Custom City")
        assert result == (50.0, 10.0)

    @patch("src.simulation.outcome_tracker.get")
    def test_fallback_hardcoded(self, mock_get):
        from src.simulation.outcome_tracker import get_city_coords

        mock_get.return_value = []
        result = get_city_coords("Tokyo")
        assert result is not None
        assert abs(result[0] - 35.6762) < 0.01

    @patch("src.simulation.outcome_tracker.get")
    def test_slug_hyphen_match(self, mock_get):
        from src.simulation.outcome_tracker import get_city_coords

        mock_get.return_value = [
            {
                "name": "Buenos Aires",
                "slug": "buenos-aires",
                "lat": -34.82,
                "lon": -58.54,
                "enabled": True,
            }
        ]
        result = get_city_coords("Buenos Aires")
        assert result is not None
        assert abs(result[0] - (-34.82)) < 0.01


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Dashboard API: weather_city_routes CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestWeatherCityRoutes:
    """Dashboard weather city API 路由测试。"""

    @pytest.fixture
    def tmp_config(self, tmp_path):
        """创建临时 config.json."""
        cfg = {
            "weather_cities": {
                "_doc_": "test",
                "cities": [
                    {
                        "name": "Test City",
                        "slug": "test-city",
                        "lat": 40.0,
                        "lon": -74.0,
                        "icao": "KTST",
                        "timezone": "America/New_York",
                        "utc_offset": -4,
                        "enabled": True,
                    }
                ],
            }
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return config_path

    def test_validate_city_errors(self):
        from scripts.dashboard_routes.weather_city_routes import _validate_city

        errors = _validate_city({})
        assert len(errors) >= 4  # name, slug, lat, lon

    def test_validate_city_invalid_lat(self):
        from scripts.dashboard_routes.weather_city_routes import _validate_city

        errors = _validate_city({"name": "X", "slug": "x", "lat": 999, "lon": 0})
        assert any("lat" in e for e in errors)

    def test_validate_city_invalid_slug(self):
        from scripts.dashboard_routes.weather_city_routes import _validate_city

        errors = _validate_city(
            {"name": "X", "slug": "Invalid Slug!", "lat": 0, "lon": 0}
        )
        assert any("slug" in e for e in errors)

    def test_validate_city_ok(self):
        from scripts.dashboard_routes.weather_city_routes import _validate_city

        errors = _validate_city(
            {"name": "NYC", "slug": "nyc", "lat": 40.77, "lon": -73.87}
        )
        assert errors == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Config.json 结构验证
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConfigWeatherCities:
    """验证 config.json weather_cities 段结构。"""

    @pytest.fixture
    def config_data(self):
        config_path = Path(__file__).resolve().parent.parent / "config" / "config.json"
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_weather_cities_exists(self, config_data):
        assert "weather_cities" in config_data

    def test_cities_is_list(self, config_data):
        wc = config_data["weather_cities"]
        assert "cities" in wc
        assert isinstance(wc["cities"], list)

    def test_42_cities(self, config_data):
        cities = config_data["weather_cities"]["cities"]
        # T95 added 2 cities (Busan, Wellington → 44 total); update count expectation here
        assert len(cities) >= 42, f"Expected at least 42 cities, got {len(cities)}"

    def test_city_fields(self, config_data):
        required_fields = {
            "name",
            "slug",
            "lat",
            "lon",
            "icao",
            "timezone",
            "utc_offset",
            "enabled",
        }
        for city in config_data["weather_cities"]["cities"]:
            missing = required_fields - set(city.keys())
            assert not missing, f"City {city.get('name', '?')} missing: {missing}"

    def test_all_polymarket_slugs(self, config_data):
        expected_slugs = {
            "ankara",
            "atlanta",
            "austin",
            "beijing",
            "buenos-aires",
            "chengdu",
            "chicago",
            "chongqing",
            "dallas",
            "denver",
            "hong-kong",
            "houston",
            "istanbul",
            "london",
            "los-angeles",
            "lucknow",
            "madrid",
            "mexico-city",
            "miami",
            "milan",
            "moscow",
            "munich",
            "nyc",
            "paris",
            "san-francisco",
            "sao-paulo",
            "seattle",
            "seoul",
            "shanghai",
            "shenzhen",
            "singapore",
            "taipei",
            "tel-aviv",
            "tokyo",
            "toronto",
            "warsaw",
            "wellington",
            "wuhan",
        }
        actual_slugs = {c["slug"] for c in config_data["weather_cities"]["cities"]}
        missing = expected_slugs - actual_slugs
        assert not missing, f"Missing slugs: {missing}"

    def test_lat_lon_range(self, config_data):
        for city in config_data["weather_cities"]["cities"]:
            assert -90 <= city["lat"] <= 90, f"{city['name']} lat out of range"
            assert -180 <= city["lon"] <= 180, f"{city['name']} lon out of range"

    def test_all_enabled(self, config_data):
        for city in config_data["weather_cities"]["cities"]:
            assert city["enabled"] is True, f"{city['name']} should be enabled"

    def test_icao_all_present(self, config_data):
        for city in config_data["weather_cities"]["cities"]:
            assert city["icao"], f"{city['name']} missing ICAO code"
            assert len(city["icao"]) == 4, f"{city['name']} ICAO should be 4 chars"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# T98: WeatherScanner._build_city_map 优先城市过滤测试
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_MOCK_WC_CITIES = [
    {
        "name": "Chicago",
        "slug": "chicago",
        "lat": 41.85,
        "lon": -87.65,
        "icao": "KORD",
        "timezone": "America/Chicago",
        "utc_offset": -5,
        "enabled": True,
    },
    {
        "name": "Tokyo",
        "slug": "tokyo",
        "lat": 35.69,
        "lon": 139.69,
        "icao": "RJTT",
        "timezone": "Asia/Tokyo",
        "utc_offset": 9,
        "enabled": True,
    },
    {
        "name": "London",
        "slug": "london",
        "lat": 51.51,
        "lon": -0.13,
        "icao": "EGLL",
        "timezone": "Europe/London",
        "utc_offset": 1,
        "enabled": True,
    },
    {
        "name": "Seoul",
        "slug": "seoul",
        "lat": 37.57,
        "lon": 126.98,
        "icao": "RKSS",
        "timezone": "Asia/Seoul",
        "utc_offset": 9,
        "enabled": True,
    },
    {
        "name": "Disabled City",
        "slug": "disabled-city",
        "lat": 0.0,
        "lon": 0.0,
        "icao": "XXXX",
        "timezone": "UTC",
        "utc_offset": 0,
        "enabled": False,
    },
]


def _mock_get(key):
    if key == "weather_cities":
        return {"cities": _MOCK_WC_CITIES}
    return None


class TestWeatherScannerPriorityCitiesFilter:
    """T98: _build_city_map 使用 priority_cities 作为硬性约束白名单。"""

    def _scanner(self):
        from src.scanner.weather_scanner import WeatherScanner

        return WeatherScanner()

    @patch("src.scanner.weather_scanner.get")
    def test_priority_filter_restricts_cities(self, mock_get):
        """只有在 priority_cities 中的城市才出现在 city_map 里。"""

        def side(key):
            if key == "weather_cities":
                return {"cities": _MOCK_WC_CITIES}
            if key == "paper_trading":
                return {"weather_priority_cities": ["Chicago", "Tokyo"]}
            return None

        mock_get.side_effect = side

        scanner = self._scanner()
        city_map = scanner._build_city_map()

        assert set(city_map.keys()) == {
            "chicago",
            "tokyo",
        }, f"Expected only chicago/tokyo, got: {set(city_map.keys())}"

    @patch("src.scanner.weather_scanner.get")
    def test_priority_filter_case_insensitive(self, mock_get):
        """大小写不敏感匹配——'CHICAGO' 匹配 city name 'Chicago'。"""

        def side(key):
            if key == "weather_cities":
                return {"cities": _MOCK_WC_CITIES}
            if key == "paper_trading":
                return {"weather_priority_cities": ["CHICAGO", "london"]}
            return None

        mock_get.side_effect = side

        scanner = self._scanner()
        city_map = scanner._build_city_map()

        assert "chicago" in city_map
        assert "london" in city_map
        assert "tokyo" not in city_map

    @patch("src.scanner.weather_scanner.get")
    def test_empty_priority_cities_fallback_all_enabled(self, mock_get):
        """priority_cities 为空列表时，fallback 到全部 enabled 城市。"""

        def side(key):
            if key == "weather_cities":
                return {"cities": _MOCK_WC_CITIES}
            if key == "paper_trading":
                return {"weather_priority_cities": []}
            return None

        mock_get.side_effect = side

        scanner = self._scanner()
        city_map = scanner._build_city_map()

        enabled_slugs = {c["slug"] for c in _MOCK_WC_CITIES if c["enabled"]}
        assert set(city_map.keys()) == enabled_slugs

    @patch("src.scanner.weather_scanner.get")
    def test_none_priority_cities_fallback_all_enabled(self, mock_get):
        """paper_trading 里无 weather_priority_cities 键时，fallback 到全部 enabled。"""

        def side(key):
            if key == "weather_cities":
                return {"cities": _MOCK_WC_CITIES}
            if key == "paper_trading":
                return {}  # 无 priority_cities 键
            return None

        mock_get.side_effect = side

        scanner = self._scanner()
        city_map = scanner._build_city_map()

        enabled_slugs = {c["slug"] for c in _MOCK_WC_CITIES if c["enabled"]}
        assert set(city_map.keys()) == enabled_slugs

    @patch("src.scanner.weather_scanner.get")
    def test_disabled_city_excluded_even_if_in_priority(self, mock_get):
        """disabled 城市不进入 city_map，即使在 priority_cities 里。"""

        def side(key):
            if key == "weather_cities":
                return {"cities": _MOCK_WC_CITIES}
            if key == "paper_trading":
                return {"weather_priority_cities": ["Chicago", "Disabled City"]}
            return None

        mock_get.side_effect = side

        scanner = self._scanner()
        city_map = scanner._build_city_map()

        assert "chicago" in city_map
        assert (
            "disabled-city" not in city_map
        ), "Disabled city must be excluded even if in priority"

    @patch("src.scanner.weather_scanner.get")
    def test_no_match_fallback(self, mock_get):
        """priority_cities 有值但无一城市名匹配时，fallback 到全部 enabled 城市。"""

        def side(key):
            if key == "weather_cities":
                return {"cities": _MOCK_WC_CITIES}
            if key == "paper_trading":
                return {
                    "weather_priority_cities": ["NonExistentCity", "AnotherFakeCity"]
                }
            return None

        mock_get.side_effect = side

        scanner = self._scanner()
        city_map = scanner._build_city_map()

        # fallback: 全部 enabled 城市
        enabled_slugs = {c["slug"] for c in _MOCK_WC_CITIES if c["enabled"]}
        assert set(city_map.keys()) == enabled_slugs


class TestWeatherCityRoutesT98:
    """T98: weather_city_routes 精准同步 priority_cities 逻辑测试。"""

    def _make_cfg(self, names=None):
        return {
            "weather_cities": {
                "cities": [
                    {"name": n, "slug": n.lower(), "enabled": True}
                    for n in (names or ["Chicago", "Tokyo", "London"])
                ]
            },
            "paper_trading": {
                "weather_priority_cities": list(names or ["Chicago", "Tokyo", "London"])
            },
        }

    def test_add_to_priority_cities(self):
        from scripts.dashboard_routes.weather_city_routes import _add_to_priority_cities

        cfg = self._make_cfg()
        _add_to_priority_cities(cfg, "Seoul")
        result = cfg["paper_trading"]["weather_priority_cities"]
        assert "Seoul" in result
        assert result == sorted(result), "priority_cities should remain sorted"

    def test_add_to_priority_cities_no_duplicate(self):
        from scripts.dashboard_routes.weather_city_routes import _add_to_priority_cities

        cfg = self._make_cfg()
        _add_to_priority_cities(cfg, "Chicago")  # already exists
        result = cfg["paper_trading"]["weather_priority_cities"]
        assert result.count("Chicago") == 1, "No duplicates on re-add"

    def test_remove_from_priority_cities(self):
        from scripts.dashboard_routes.weather_city_routes import (
            _remove_from_priority_cities,
        )

        cfg = self._make_cfg()
        _remove_from_priority_cities(cfg, "Tokyo")
        result = cfg["paper_trading"]["weather_priority_cities"]
        assert "Tokyo" not in result

    def test_rename_in_priority_cities(self):
        from scripts.dashboard_routes.weather_city_routes import (
            _rename_in_priority_cities,
        )

        cfg = self._make_cfg()
        _rename_in_priority_cities(cfg, "Tokyo", "Osaka")
        result = cfg["paper_trading"]["weather_priority_cities"]
        assert "Tokyo" not in result
        assert "Osaka" in result
        assert result == sorted(result)

    def test_rename_nonexistent_is_safe(self):
        from scripts.dashboard_routes.weather_city_routes import (
            _rename_in_priority_cities,
        )

        cfg = self._make_cfg()
        original = sorted(cfg["paper_trading"]["weather_priority_cities"])
        _rename_in_priority_cities(cfg, "NonExistent", "NewName")
        result = cfg["paper_trading"]["weather_priority_cities"]
        # NonExistent 不在列表中，结果应与原列表相同（不新增条目）
        assert (
            result == original
        ), "Renaming nonexistent entry should not modify the list"
