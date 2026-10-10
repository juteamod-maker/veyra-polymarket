from __future__ import annotations

from scripts.dashboard_routes import valuelab_routes as routes
from scripts.dashboard_routes import discover_routes


def test_build_events_payload_returns_items(monkeypatch):
    sample = {
        "events": [
            {
                "slug": "btc-above-120k-2026",
                "title": "Will BTC close above 120k this year?",
                "category": "crypto",
                "volume_24hr": 1200000,
                "liquidity": 440000,
                "markets": [{"yes_prob": 0.42}],
            },
            {
                "slug": "us-cpi-above-4",
                "title": "Will US CPI be above 4% in Q4?",
                "category": "macro",
                "volume_24hr": 640000,
                "liquidity": 230000,
                "markets": [{"yes_prob": 0.37}],
            },
        ]
    }

    monkeypatch.setattr(discover_routes, "get_ssr_payload", lambda **kwargs: sample)
    monkeypatch.setattr(
        routes,
        "_fetch_sources_health",
        lambda force_refresh=False: {
            "freshness_grade": "A",
            "max_lag_sec": 12,
            "signals": {
                "btc_24h_change": -2.2,
                "fear_greed": 41,
                "funding_rate": 0.0001,
                "macro_rate": 4.3,
            },
            "sources": [],
        },
    )

    payload = routes._build_events_payload(
        tab="all",
        q="",
        sort="score",
        order="desc",
        page=1,
        page_size=18,
        fast_sources=True,
    )

    assert payload["ok"] is True
    assert payload["total"] == 2
    assert len(payload["items"]) == 2
    assert "value_score" in payload["items"][0]
    assert "score_ci" in payload["items"][0]
    assert "ev_pct" in payload["items"][0]


def test_analysis_payload_not_found(monkeypatch):
    monkeypatch.setattr(
        discover_routes, "get_ssr_payload", lambda **kwargs: {"events": []}
    )
    out = routes.get_valuelab_analysis_payload("missing-slug")
    assert out is None


def test_build_events_payload_strict_discover_categories(monkeypatch):
    samples = {
        "crypto": {
            "events": [
                {
                    "slug": "btc-up",
                    "title": "BTC up this week?",
                    "volume_24hr": 120000,
                    "liquidity": 45000,
                    "markets": [{"yes_prob": 0.44}],
                }
            ]
        },
        "earnings": {
            "events": [
                {
                    "slug": "nvda-earnings",
                    "title": "NVDA beat earnings?",
                    "volume_24hr": 98000,
                    "liquidity": 38000,
                    "markets": [{"yes_prob": 0.49}],
                }
            ]
        },
        "economy": {
            "events": [
                {
                    "slug": "us-cpi-q4",
                    "title": "US CPI above 4%?",
                    "volume_24hr": 86000,
                    "liquidity": 30000,
                    "markets": [{"yes_prob": 0.39}],
                }
            ]
        },
        "finance": {
            "events": [
                {
                    "slug": "spx-5600",
                    "title": "SPX above 5600?",
                    "volume_24hr": 76000,
                    "liquidity": 29000,
                    "markets": [{"yes_prob": 0.41}],
                }
            ]
        },
    }

    monkeypatch.setattr(
        discover_routes,
        "get_ssr_payload",
        lambda **kwargs: samples.get((kwargs.get("p") or "").lower(), {"events": []}),
    )
    monkeypatch.setattr(
        routes,
        "_fetch_sources_health",
        lambda force_refresh=False: {
            "freshness_grade": "A",
            "max_lag_sec": 20,
            "signals": {
                "btc_24h_change": -1.8,
                "fear_greed": 43,
                "funding_rate": 0.0001,
                "macro_rate": 4.2,
            },
            "sources": [],
        },
    )

    payload = routes._build_events_payload(tab="finance", page=1, page_size=18)
    assert payload["ok"] is True
    assert payload["tab"] == "finance"
    assert payload["total"] == 1
    assert payload["items"][0]["slug"] == "spx-5600"
    assert payload["items"][0]["category"] == "finance"


def test_sources_health_missing_optional_source_not_force_grade_d(monkeypatch):
    monkeypatch.setattr(routes, "get", lambda key: "")

    def _fake_http(url, params=None, timeout=2.0):
        if "coingecko" in url:
            return {
                "bitcoin": {"usd_24h_change": -1.2},
                "ethereum": {"usd_24h_change": -0.8},
            }
        if "alternative.me" in url:
            return {"data": [{"value": "47"}]}
        if "binance" in url:
            return {"lastFundingRate": "0.0001"}
        if "llama.fi" in url:
            return [{"name": "Ethereum"}]
        raise RuntimeError("unexpected url")

    monkeypatch.setattr(routes, "_http_get_json", _fake_http)
    out = routes._fetch_sources_health(force_refresh=True)

    assert out["freshness_grade"] in ("A", "B", "C")
    assert int(out["max_lag_sec"]) < 86400
    assert int(out["max_lag_sec_all_sources"]) >= int(out["max_lag_sec"])
