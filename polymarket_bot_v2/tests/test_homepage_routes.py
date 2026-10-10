import scripts.web_dashboard as wd
from fastapi.testclient import TestClient
import pytest
from scripts.dashboard_routes import _shared


def _admin_client(monkeypatch):
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(wd, "TOTP_SECRET", "")
    monkeypatch.setattr(_shared, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(_shared, "TOTP_SECRET", "")
    c = TestClient(wd.app)
    c.cookies.set("dashboard_key", "k")
    return c


def _anon_client(monkeypatch):
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "")
    monkeypatch.setattr(_shared, "DASHBOARD_KEY", "")
    return TestClient(wd.app)


def _require_db():
    try:
        with _shared.db_connect() as conn:
            conn.execute("SELECT 1")
    except Exception as e:
        pytest.skip(f"db unavailable: {e}")


def test_public_homepage_cards(monkeypatch):
    _require_db()
    c = _anon_client(monkeypatch)
    r = c.get("/api/public/homepage/cards")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert isinstance(d["cards"], list)
    assert len(d["cards"]) >= 1


def test_homepage_admin_crud(monkeypatch):
    _require_db()
    c = _admin_client(monkeypatch)
    slot_key = "event_test_homepage_route"

    save = c.post(
        "/api/system/homepage/cards/upsert",
        json={
            "slot_key": slot_key,
            "enabled": True,
            "sort_order": 77,
            "title_i18n_key": "homeevent_test_admin_crud",
        },
    )
    assert save.status_code == 200
    assert save.json()["ok"] is True

    listing = c.get("/api/system/homepage/cards")
    assert listing.status_code == 200
    cards = listing.json().get("cards") or []
    target = next((x for x in cards if x.get("slot_key") == slot_key), None)
    assert target is not None
    assert target.get("title_i18n_key") == "homeevent_test_admin_crud"

    delete = c.post("/api/system/homepage/cards/delete", json={"slot_key": slot_key})
    assert delete.status_code == 200
    assert delete.json()["ok"] is True


def test_homepage_admin_requires_admin(monkeypatch):
    _require_db()
    c = _anon_client(monkeypatch)
    r = c.get("/api/system/homepage/cards")
    assert r.status_code == 403
