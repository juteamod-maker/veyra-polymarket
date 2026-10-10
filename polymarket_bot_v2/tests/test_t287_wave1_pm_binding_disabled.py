"""T287 Wave1: Verify manual PM-wallet binding endpoints return 410 Gone
without writing to DB. These tests deliberately avoid touching DB so they
run on developer laptops without a local Postgres instance.

Admin-side polymarket_wallet ignore behaviour is covered by
``test_system_user_management.py`` integration suite (CI/VPS only).
"""
import pytest
from fastapi.testclient import TestClient

import scripts.web_dashboard as wd
from scripts.dashboard_routes import _shared


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(wd, "TOTP_SECRET", "")
    monkeypatch.setattr(_shared, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(_shared, "TOTP_SECRET", "")
    c = TestClient(wd.app)
    # Admin Bearer satisfies auth middleware so we exercise the route handler
    # directly. Even with the most privileged caller, the endpoint MUST 410.
    c.headers.update({"Authorization": "Bearer k"})
    return c


def test_pm_wallet_bind_returns_410(client):
    resp = client.post(
        "/api/user/pm-wallet/bind",
        json={"polymarket_wallet": "0x" + "a" * 40},
    )
    assert resp.status_code == 410, resp.text
    body = resp.json()
    assert body["error"] == "manual_pm_wallet_binding_disabled"
    assert body["code"] == "T287_WAVE1"
    assert body["next_action"] == "privy_authorize"


def test_user_wallet_bind_returns_410(client):
    resp = client.post(
        "/api/user/wallet/bind",
        json={"pm_wallet_address": "0x" + "b" * 40},
    )
    assert resp.status_code == 410, resp.text
    body = resp.json()
    assert body["error"] == "manual_pm_wallet_binding_disabled"


def test_user_wallet_unbind_returns_410(client):
    resp = client.post("/api/user/wallet/unbind")
    assert resp.status_code == 410, resp.text
    body = resp.json()
    assert body["error"] == "manual_pm_wallet_binding_disabled"
    assert "联系管理员" in body["message"]


def test_pm_wallet_bind_rejects_invalid_payload_with_410(client):
    """Garbage JSON payloads must still produce 410 (no info leak)."""
    resp = client.post(
        "/api/user/pm-wallet/bind",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 410, resp.text
