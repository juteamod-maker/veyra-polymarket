from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any

from fastapi.responses import JSONResponse

from scripts.dashboard_routes import system


class _DummyResp:
    def __init__(self, payload: Any):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def test_query_slug_from_source_prefers_pm_route_prefix() -> None:
    assert system._query_slug_from_source("pm_route:highest-temperature", "high-temperature") == "highest-temperature"
    assert system._query_slug_from_source("", "nba") == "nba"


def test_fetch_pm_route_tags_builds_alias_and_query_slug(monkeypatch) -> None:
    def fake_get(url: str, params: dict[str, Any] | None = None, timeout: int = 0):
        if url == system._PM_EVENTS_URL:
            return _DummyResp(
                [
                    {
                        "id": "e1",
                        "slug": "event-1",
                        "tags": [
                            {"id": "1", "slug": "sports", "label": "Sports"},
                            {"id": "745", "slug": "nba", "label": "NBA"},
                            {
                                "id": "102983",
                                "slug": "highest-temperature",
                                "label": "Highest Temperature",
                            },
                        ],
                    }
                ]
            )
        if url == system._PM_SPORTS_URL:
            return _DummyResp([{"sport": "nba"}])
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(system.http_requests, "get", fake_get)

    rows = system._fetch_pm_route_tags_from_gamma()
    by_slug = {r["slug"]: r for r in rows}

    assert "nba" in by_slug
    assert by_slug["nba"]["query_slug"] == "nba"

    assert "high-temperature" in by_slug
    assert by_slug["high-temperature"]["query_slug"] == "highest-temperature"
    assert by_slug["high-temperature"]["active_events_count"] == by_slug["highest-temperature"]["active_events_count"]


class _NoRowResult:
    def fetchone(self):
        return None


class _NoRowConn:
    def execute(self, *_args, **_kwargs):
        return _NoRowResult()


def test_pm_tag_events_uses_alias_when_cache_missing(monkeypatch) -> None:
    @contextmanager
    def fake_db_connect():
        yield _NoRowConn()

    def fake_get(url: str, params: dict[str, Any] | None = None, timeout: int = 0):
        if url == system._PM_EVENTS_URL:
            assert (params or {}).get("tag_slug") == "highest-temperature"
            return _DummyResp(
                [
                    {
                        "id": "e1",
                        "slug": "event-1",
                        "title": "Highest temperature check",
                    }
                ]
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(system, "_ensure_pm_tags_table", lambda: None)
    monkeypatch.setattr(system, "db_connect", fake_db_connect)
    monkeypatch.setattr(system.http_requests, "get", fake_get)

    result = asyncio.run(system.api_pm_tag_events("high-temperature"))

    assert isinstance(result, dict)
    assert result["query_slug"] == "highest-temperature"
    assert result["count"] == 1


def test_pm_tag_events_returns_404_when_alias_has_no_events(monkeypatch) -> None:
    @contextmanager
    def fake_db_connect():
        yield _NoRowConn()

    def fake_get(url: str, params: dict[str, Any] | None = None, timeout: int = 0):
        if url == system._PM_EVENTS_URL:
            return _DummyResp([])
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(system, "_ensure_pm_tags_table", lambda: None)
    monkeypatch.setattr(system, "db_connect", fake_db_connect)
    monkeypatch.setattr(system.http_requests, "get", fake_get)

    result = asyncio.run(system.api_pm_tag_events("high-temperature"))

    assert isinstance(result, JSONResponse)
    assert result.status_code == 404
