from datetime import datetime

from scripts.dashboard_routes import weather_map_routes as wr


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql, _params):
        return None

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_slugify_city_for_event_removes_accents():
    assert wr._slugify_city_for_event("Sao Paulo") == "sao-paulo"
    assert wr._slugify_city_for_event("São Paulo") == "sao-paulo"


def test_resolve_market_links_fallback(monkeypatch):
    wr._EVENT_LINK_CACHE.clear()

    def _mock_db_connect():
        return _FakeConn([])

    monkeypatch.setattr(wr, "db_connect", _mock_db_connect)

    links = wr._resolve_market_links("Hong Kong", datetime(2026, 4, 24, 9, 30, 0))
    assert links["highest_exists"] is False
    assert links["lowest_exists"] is False
    assert links["highest_url"] == "https://veyra.trade/discover?p=weather&t=highest-temperature"
    assert links["lowest_url"] == "https://veyra.trade/discover?p=weather&t=lowest-temperature"


def test_resolve_market_links_event_url_when_exists(monkeypatch):
    wr._EVENT_LINK_CACHE.clear()
    high_slug = "highest-temperature-in-hong-kong-on-april-24-2026"

    def _mock_db_connect():
        return _FakeConn([(high_slug,)])

    monkeypatch.setattr(wr, "db_connect", _mock_db_connect)

    links = wr._resolve_market_links("Hong Kong", datetime(2026, 4, 24, 9, 30, 0))
    assert links["highest_exists"] is True
    assert links["highest_url"].endswith("/event/highest-temperature-in-hong-kong-on-april-24-2026")
    assert links["lowest_exists"] is False
    assert links["lowest_url"] == "https://veyra.trade/discover?p=weather&t=lowest-temperature"
