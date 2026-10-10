"""T289 Wave 5+6 — discover_routes tests.

Validates Gamma proxy, orderbook proxy, deposit-info, balance-check
and trade/submit guard rails (balance-below-min, invalid payloads).
External HTTP and DB are fully mocked.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("V2_GUARD_ALLOW_DRY_RUN", "1")
    dr = importlib.import_module("scripts.dashboard_routes.discover_routes")
    # Reset cache between tests
    dr._events_cache.clear()
    app = FastAPI()
    app.include_router(dr.router)
    return TestClient(app), dr


# ---------- Public Gamma proxy ----------


def test_events_gamma_proxy_returns_normalised_payload(client):
    tc, dr = client
    fake_events = [
        {
            "id": "ev1",
            "slug": "tesla-q4",
            "title": "Tesla Q4 earnings beat?",
            "image": "x.png",
            "category": "Business",
            "endDate": "2026-04-30",
            "volume24hr": 12345.6,
            "volume": 567890.1,
            "liquidity": 5000.0,
            "active": True,
            "closed": False,
            "markets": [
                {
                    "id": "m1",
                    "conditionId": "0xabc",
                    "question": "Beat?",
                    "slug": "tesla-q4-beat",
                    "clobTokenIds": '["111","222"]',
                    "bestBid": 0.58,
                    "bestAsk": 0.62,
                    "volume24hr": 100.0,
                }
            ],
        }
    ]
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = fake_events
    mock_resp.raise_for_status.return_value = None
    mock_resp.status_code = 200
    with patch.object(dr.requests, "get", return_value=mock_resp) as mock_get:
        r = tc.get("/api/pm/events?limit=5&offset=0&sort=volume24hr")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["count"] == 1
        ev = body["events"][0]
        assert ev["slug"] == "tesla-q4"
        assert ev["markets_count"] == 1
        assert ev["markets"][0]["yes_token_id"] == "111"
        assert ev["markets"][0]["no_token_id"] == "222"
        # Gamma called with correct params
        args, kwargs = mock_get.call_args
        assert kwargs["params"]["order"] == "volume24hr"


def test_event_detail_404(client):
    tc, dr = client
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.ok = False
    with patch.object(dr.requests, "get", return_value=mock_resp):
        r = tc.get("/api/pm/event/does-not-exist")
        assert r.status_code == 404


def test_orderbook_rejects_non_numeric_token(client):
    tc, _dr = client
    r = tc.get("/api/pm/orderbook?token_id=abc")
    assert r.status_code == 400


def test_orderbook_proxy_returns_levels(client):
    tc, dr = client
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "bids": [{"price": "0.58", "size": "100"}],
        "asks": [{"price": "0.62", "size": "80"}],
        "timestamp": "1",
    }
    with patch.object(dr.requests, "get", return_value=mock_resp):
        r = tc.get("/api/pm/orderbook?token_id=12345")
        assert r.status_code == 200
        body = r.json()
        assert body["best_bid"]["price"] == 0.58
        assert body["best_ask"]["size"] == 80.0


# ---------- Auth-guarded endpoints ----------


def test_deposit_info_requires_auth(client):
    tc, dr = client
    with patch.object(dr, "get_request_identity", return_value=None):
        r = tc.get("/api/pm/deposit-info")
        assert r.status_code == 403


def test_deposit_info_returns_funder_and_thresholds(client):
    tc, dr = client
    with patch.object(
        dr,
        "get_request_identity",
        return_value={
            "authenticated": True,
            "wallet_address": "0xuser",
            "is_admin": False,
        },
    ), patch.object(dr, "_get_user_funder", return_value="0xfunder"), patch.object(
        dr, "_fetch_usdc_balance", return_value=12.34
    ):
        r = tc.get("/api/pm/deposit-info")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["funder_address"] == "0xfunder"
        assert body["chain_id"] == 137
        assert body["usdc_balance"] == 12.34
        assert body["balance_status"] == "ok"
        assert body["thresholds"]["min_usdc_to_trade"] >= 1.0


def test_balance_check_blocks_below_min(client):
    tc, dr = client
    with patch.object(
        dr,
        "get_request_identity",
        return_value={
            "authenticated": True,
            "wallet_address": "0xuser",
            "is_admin": False,
        },
    ), patch.object(dr, "_get_user_funder", return_value="0xfunder"), patch.object(
        dr, "_fetch_usdc_balance", return_value=0.50
    ):
        r = tc.get("/api/pm/balance-check")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["can_trade"] is False
        assert body["should_deposit"] is True
        assert any("充值" in a or "低于" in a for a in body["alerts"])


# ---------- Trade submit guard rails ----------


def test_trade_submit_requires_auth(client):
    tc, dr = client
    with patch.object(dr, "get_request_identity", return_value=None):
        r = tc.post(
            "/api/pm/trade/submit",
            json={"token_id": "123", "side": "BUY", "price": 0.5, "size": 10},
        )
        assert r.status_code == 403


def test_trade_submit_rejects_low_balance(client):
    tc, dr = client

    def fake_runtime(_w):
        return {"freeze_auto": False, "auto_trading_enabled": True}

    with patch.object(
        dr,
        "get_request_identity",
        return_value={
            "authenticated": True,
            "wallet_address": "0xuser",
            "is_admin": False,
        },
    ), patch.object(dr, "_runtime_global_block", return_value=(False, "")), patch.object(
        dr, "_get_user_runtime", side_effect=fake_runtime
    ), patch.object(dr, "_get_user_funder", return_value="0xfunder"), patch.object(
        dr, "_fetch_usdc_balance", return_value=0.20
    ):
        r = tc.post(
            "/api/pm/trade/submit",
            json={
                "token_id": "12345",
                "side": "BUY",
                "price": 0.5,
                "size": 10,
                "order_type": "FOK",
            },
        )
        assert r.status_code == 402, r.text
        body = r.json()
        assert body["error"] == "usdc_balance_below_minimum"


def test_trade_submit_rejects_invalid_price(client):
    tc, dr = client
    with patch.object(
        dr,
        "get_request_identity",
        return_value={
            "authenticated": True,
            "wallet_address": "0xuser",
            "is_admin": False,
        },
    ), patch.object(dr, "_runtime_global_block", return_value=(False, "")), patch.object(
        dr, "_get_user_runtime", return_value={"freeze_auto": False, "auto_trading_enabled": True}
    ), patch.object(dr, "_get_user_funder", return_value="0xfunder"):
        r = tc.post(
            "/api/pm/trade/submit",
            json={"token_id": "123", "side": "BUY", "price": 1.5, "size": 10},
        )
        assert r.status_code == 400


def test_trade_submit_blocks_during_maintenance(client):
    tc, dr = client
    with patch.object(
        dr,
        "get_request_identity",
        return_value={
            "authenticated": True,
            "wallet_address": "0xuser",
            "is_admin": False,
        },
    ), patch.object(dr, "_runtime_global_block", return_value=(True, "maintenance")):
        r = tc.post(
            "/api/pm/trade/submit",
            json={"token_id": "12345", "side": "BUY", "price": 0.5, "size": 10},
        )
        assert r.status_code == 403
