"""T288 Wave 4-after — verify email-verify endpoints return 410 Gone."""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    # Bypass V2 production guard for unit tests
    monkeypatch.setenv("V2_GUARD_ALLOW_DRY_RUN", "1")
    user_routes = importlib.import_module("scripts.dashboard_routes.user_routes")
    app = FastAPI()
    app.include_router(user_routes.router)
    return TestClient(app)


def test_email_verify_send_returns_410(client):
    r = client.post("/api/user/email/verify", json={"email": "x@y.z"})
    assert r.status_code == 410, r.text
    body = r.json()
    assert body.get("error") == "deprecated"
    assert "Privy" in body.get("message", "")


def test_email_verify_confirm_returns_410(client):
    r = client.post(
        "/api/user/email/verify/confirm",
        json={"email": "x@y.z", "code": "123456"},
    )
    assert r.status_code == 410, r.text
    body = r.json()
    assert body.get("error") == "deprecated"
