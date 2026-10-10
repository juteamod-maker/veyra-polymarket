from scripts.dashboard_routes import weather_map_routes as wr


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql, _params=None):
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


def test_get_city_bets_matches_tokyo_by_event_slug(monkeypatch):
    rows = [
        (
            "b_tokyo_1",
            "",
            "YES",
            0.55,
            None,
            1713800000,
            "weather_auto",
            0.55,
            "cond_x",
            "",
            25.0,
            10.0,
            "highest-temperature-in-tokyo-on-april-24-2026",
        )
    ]

    monkeypatch.setattr(wr, "db_connect", lambda: _FakeConn(rows))
    monkeypatch.setattr(wr, "_extract_city_from_question", lambda _q, _wc: None)

    weather_cities = [{"name": "Tokyo"}]
    bets = wr._get_city_bets("Tokyo", weather_cities)

    assert len(bets) == 1
    assert bets[0]["bet_id"] == "b_tokyo_1"
    assert bets[0]["event_slug"] == "highest-temperature-in-tokyo-on-april-24-2026"


def test_weather_map_cities_counts_by_event_slug_when_question_missing(monkeypatch):
    rows = [
        (
            "",
            "",
            "highest-temperature-in-tokyo-on-april-24-2026",
        )
    ]

    monkeypatch.setattr(wr, "db_connect", lambda: _FakeConn(rows))
    monkeypatch.setattr(
        wr,
        "_get_weather_cities",
        lambda: [{"name": "Tokyo", "lat": 35.67, "lon": 139.65, "timezone": "Asia/Tokyo", "icao": "RJTT", "enabled": True}],
    )

    def _extract_city(text, _weather_cities):
        txt = str(text or "").lower().replace("-", " ")
        if "tokyo" in txt:
            return "Tokyo"
        return None

    monkeypatch.setattr(wr, "_extract_city_from_question", _extract_city)
    wr._CITIES_CACHE["data"] = None
    wr._CITIES_CACHE["ts"] = 0

    import asyncio

    result = asyncio.run(wr.weather_map_cities())
    assert result["status"] == "ok"
    assert result["cities"][0]["active_bets"] == 1


def test_weather_map_cities_merges_snapshot_positions(monkeypatch):
    rows = []
    monkeypatch.setattr(wr, "db_connect", lambda: _FakeConn(rows))
    monkeypatch.setattr(
        wr,
        "_get_weather_cities",
        lambda: [{"name": "Tokyo", "lat": 35.67, "lon": 139.65, "timezone": "Asia/Tokyo", "icao": "RJTT", "enabled": True}],
    )
    monkeypatch.setattr(
        wr,
        "_fetch_latest_user_weather_positions",
        lambda: [
            {
                "title": "Will the highest temperature in Tokyo be 24C on April 24?",
                "slug": "highest-temperature-tokyo-24c",
                "event_slug": "highest-temperature-in-tokyo-on-april-24-2026",
            }
        ],
    )
    monkeypatch.setattr(wr, "_extract_city_from_question", lambda q, _wc: "Tokyo" if "tokyo" in str(q).lower() else None)
    wr._CITIES_CACHE["data"] = None
    wr._CITIES_CACHE["ts"] = 0

    import asyncio

    result = asyncio.run(wr.weather_map_cities())
    assert result["cities"][0]["active_bets"] == 1


def test_get_city_bets_merges_snapshot_positions(monkeypatch):
    monkeypatch.setattr(wr, "db_connect", lambda: _FakeConn([]))
    monkeypatch.setattr(
        wr,
        "_fetch_latest_user_weather_positions",
        lambda: [
            {
                "wallet_address": "0xabc",
                "snapshot_ts": "2026-04-24T00:01:00Z",
                "condition_id": "cond_tokyo",
                "title": "Will the highest temperature in Tokyo be 24C on April 24?",
                "outcome": "YES",
                "size": 12.0,
                "avg_price": 0.53,
                "cur_price": 0.58,
                "cash_pnl": 1.2,
                "percent_pnl": 0.07,
                "slug": "highest-temperature-tokyo-24c",
                "event_slug": "highest-temperature-in-tokyo-on-april-24-2026",
            }
        ],
    )
    monkeypatch.setattr(wr, "_extract_city_from_question", lambda q, _wc: "Tokyo" if "tokyo" in str(q).lower() else None)
    bets = wr._get_city_bets("Tokyo", [{"name": "Tokyo"}])
    assert len(bets) == 1
    assert bets[0]["source"] == "user_pm_positions_snapshots"
    assert bets[0]["condition_id"] == "cond_tokyo"
