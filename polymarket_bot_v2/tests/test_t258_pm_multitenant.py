import scripts.web_dashboard as wd
from fastapi.testclient import TestClient
from scripts.dashboard_routes import _shared, user_routes


def _mock_discovery(login_wallet: str, linked_wallets: list[str]):
    assert login_wallet == "0x1538cccf03bd2d95c210e22eab15e7a606faab35"
    assert linked_wallets
    return {
        "pm_wallet": "0x822e0247c3fe0ccb38c624c94ed96bca5bfed8dd",
        "proxy_wallet": "0x822e0247c3fe0ccb38c624c94ed96bca5bfed8dd",
        "method": "public_profile+data_api",
        "confidence": 88.0,
        "profile": {
            "proxyWallet": "0x822e0247c3fe0ccb38c624c94ed96bca5bfed8dd",
            "name": "TestUser",
        },
        "value": {
            "user": "0x822e0247c3fe0ccb38c624c94ed96bca5bfed8dd",
            "value": 14.8421,
        },
        "positions": [
            {
                "conditionId": "0xcond1",
                "asset": "asset1",
                "title": "Will temp exceed X?",
                "outcome": "No",
                "size": 5.9,
                "avgPrice": 0.75,
                "initialValue": 4.45,
                "currentValue": 5.91,
                "cashPnl": 1.46,
                "percentPnl": 32.9,
                "curPrice": 0.997,
                "eventId": "387440",
                "slug": "market-slug",
                "eventSlug": "event-slug",
                "endDate": "2026-04-19",
            }
        ],
        "activity": [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "0xcond1",
                "asset": "asset1",
                "title": "Will temp exceed X?",
                "outcome": "No",
                "size": 1.0,
                "usdcSize": 0.9,
                "price": 0.9,
                "eventId": "387440",
                "eventSlug": "event-slug",
                "timestamp": 1776584878,
                "transactionHash": "0xhash",
            }
        ],
    }


def test_t258_auto_discovery_and_metrics(monkeypatch, tmp_path):
    test_db = tmp_path / "pm_t258.sqlite"
    monkeypatch.setattr(_shared, "DB_PATH", test_db)
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "k")

    monkeypatch.setattr(
        user_routes,
        "_verify_privy_tokens",
        lambda _i, _a: (
            "did:privy:test-user",
            {
                "linkedAccounts": [
                    {"address": "0x1538cccf03bd2d95c210e22eab15e7a606faab35"},
                ]
            },
        ),
    )
    monkeypatch.setattr(user_routes, "_discover_pm_wallet", _mock_discovery)
    monkeypatch.setattr(user_routes, "_fetch_usdc_balance", lambda _w: None)
    monkeypatch.setattr(
        user_routes,
        "_fetch_accounting_snapshot",
        lambda _w: {
            "cash_balance": 10.000688,
            "positions_value": 14.861883,
            "equity": 24.862571,
            "valuation_time": "2026-04-19T19:44:19Z",
            "positions_count": 1,
            "positions_rows": [],
        },
    )

    c = TestClient(wd.app)

    r_verify = c.post(
        "/api/user/privy/verify",
        json={"id_token": "dummy", "access_token": "dummy", "user": {}},
    )
    assert r_verify.status_code == 200

    r_me = c.get("/api/user/me")
    assert r_me.status_code == 200
    me = r_me.json()
    assert me["authenticated"] is True
    assert me["role"] == "manual_user"
    assert me["polymarket_wallet"] == "0x822e0247c3fe0ccb38c624c94ed96bca5bfed8dd"

    r_status = c.get("/api/user/pm-wallet/status")
    assert r_status.status_code == 200
    status = r_status.json()
    assert status["bound"] is True
    assert status["polymarket_wallet"] == "0x822e0247c3fe0ccb38c624c94ed96bca5bfed8dd"
    assert abs(float(status["portfolio_value"]["value"]) - 14.861883) < 1e-6
    assert abs(float(status["portfolio_value"]["balance"]) - 10.000688) < 1e-6
    assert status["open_positions_count"] == 1

    r_positions = c.get("/api/user/pm/positions")
    assert r_positions.status_code == 200
    positions = r_positions.json()["positions"]
    assert len(positions) == 1
    assert positions[0]["title"] == "Will temp exceed X?"

    r_metrics = c.get("/api/user/pm/metrics")
    assert r_metrics.status_code == 200
    metrics = r_metrics.json()
    assert abs(float(metrics["nlv"]) - 24.862571) < 1e-6
    assert abs(float(metrics["balance"]) - 10.000688) < 1e-6
    assert abs(float(metrics["positions_value"]) - 14.861883) < 1e-6
    assert metrics["open_count"] == 1

    r_tracks = c.get("/api/tracks")
    assert r_tracks.status_code == 200
    tracks = r_tracks.json()["tracks"]
    assert tracks
    assert tracks[0]["category"] == "weather"

    r_runtime = c.get("/api/user/runtime-control")
    assert r_runtime.status_code == 200
    runtime = r_runtime.json()
    assert runtime["automation_capability"] is False

    r_pm_bets_open = c.get("/api/user/pm/bets?status=open")
    assert r_pm_bets_open.status_code == 200
    open_bets = r_pm_bets_open.json()
    assert open_bets["total"] >= 1
    assert all(not b.get("resolved_outcome") for b in open_bets["bets"])

    r_pm_bets_resolved = c.get("/api/user/pm/bets?status=resolved")
    assert r_pm_bets_resolved.status_code == 200
    resolved_bets = r_pm_bets_resolved.json()
    assert resolved_bets["total"] >= 1
    assert all(b.get("resolved_outcome") for b in resolved_bets["bets"])


def test_t258_pm_bets_open_matches_positions_and_closed_source(monkeypatch, tmp_path):
    test_db = tmp_path / "pm_t258_consistency.sqlite"
    monkeypatch.setattr(_shared, "DB_PATH", test_db)
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "k")

    monkeypatch.setattr(
        user_routes,
        "_verify_privy_tokens",
        lambda _i, _a: (
            "did:privy:test-user-2",
            {
                "linkedAccounts": [
                    {"address": "0x1538cccf03bd2d95c210e22eab15e7a606faab35"},
                ]
            },
        ),
    )
    monkeypatch.setattr(user_routes, "_discover_pm_wallet", _mock_discovery)
    monkeypatch.setattr(user_routes, "_fetch_usdc_balance", lambda _w: None)
    monkeypatch.setattr(
        user_routes,
        "_fetch_accounting_snapshot",
        lambda _w: {
            "cash_balance": 10.0,
            "positions_value": 14.8,
            "equity": 24.8,
            "valuation_time": "2026-04-19T19:44:19Z",
            "positions_count": 1,
            "positions_rows": [],
        },
    )

    monkeypatch.setattr(
        user_routes,
        "_fetch_closed_positions_full",
        lambda _w, limit=3000: [
            {
                "conditionId": "0xclosed1",
                "avgPrice": 0.42,
                "totalBought": 12.6,
                "realizedPnl": 2.1,
                "timestamp": 1776500001,
                "title": "Will rain exceed threshold?",
                "outcome": "No",
                "slug": "will-rain-exceed-threshold",
                "eventSlug": "rain-event",
                "endDate": "2026-04-18T00:00:00Z",
            },
            {
                "conditionId": "0xclosed2",
                "avgPrice": 0.61,
                "totalBought": 9.15,
                "realizedPnl": -1.4,
                "timestamp": 1776500002,
                "title": "Will BTC close above 90k?",
                "outcome": "Yes",
                "slug": "will-btc-close-above-90k",
                "eventSlug": "btc-event",
                "endDate": "2026-04-18T00:00:00Z",
            },
        ],
    )

    c = TestClient(wd.app)

    r_verify = c.post(
        "/api/user/privy/verify",
        json={"id_token": "dummy", "access_token": "dummy", "user": {}},
    )
    assert r_verify.status_code == 200

    r_positions = c.get("/api/user/pm/positions")
    assert r_positions.status_code == 200
    pos_count = r_positions.json().get("count", 0)
    assert pos_count == 1

    r_open = c.get("/api/user/pm/bets?status=open&refresh=1")
    assert r_open.status_code == 200
    open_payload = r_open.json()
    assert open_payload["total"] == pos_count
    assert all(not b.get("resolved_outcome") for b in open_payload["bets"])

    r_resolved = c.get("/api/user/pm/bets?status=resolved&refresh=1")
    assert r_resolved.status_code == 200
    resolved_payload = r_resolved.json()
    assert resolved_payload["total"] == 2
    assert all(b.get("resolved_outcome") for b in resolved_payload["bets"])


