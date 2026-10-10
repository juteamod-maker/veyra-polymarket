import scripts.web_dashboard as wd
from fastapi.testclient import TestClient
from scripts.dashboard_routes import _shared


def test_whitelisted_ip_bypasses_temp_ban(monkeypatch):
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(wd, "TOTP_SECRET", "")

    monkeypatch.setattr(wd.ip_blacklist, "is_whitelisted", lambda _ip: True)
    monkeypatch.setattr(wd.ip_blacklist, "is_blocked", lambda _ip: True)
    monkeypatch.setattr(wd.ip_rate_limiter, "is_blocked", lambda _ip: True)

    c = TestClient(wd.app)
    r = c.get("/?key=k")

    assert r.status_code == 200


def test_whitelisted_ip_wrong_key_does_not_increment_fail(monkeypatch):
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(wd, "TOTP_SECRET", "")

    monkeypatch.setattr(wd.ip_blacklist, "is_whitelisted", lambda _ip: True)
    monkeypatch.setattr(wd.ip_blacklist, "is_blocked", lambda _ip: False)
    monkeypatch.setattr(wd.ip_rate_limiter, "is_blocked", lambda _ip: False)

    called = {"n": 0}

    def _record_fail(_ip):
        called["n"] += 1
        return False

    monkeypatch.setattr(wd.ip_rate_limiter, "record_fail", _record_fail)

    c = TestClient(wd.app)
    r = c.get("/?key=wrong")

    assert r.status_code == 200
    assert len(r.history) >= 1
    assert r.history[0].status_code in (301, 302, 307, 308)
    assert called["n"] == 0


def test_index_does_not_auto_redirect_with_user_session(monkeypatch):
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(wd, "TOTP_SECRET", "")

    c = TestClient(wd.app)
    token = _shared.create_user_session(
        wallet_address="0x1111111111111111111111111111111111111111",
        role="manual_user",
        source_ip="127.0.0.1",
    )
    c.cookies.set("user_session", token)

    try:
        r = c.get("/Index")
        assert r.status_code == 200
        assert len(r.history) == 0
        assert "Privy" in r.text
    finally:
        _shared.revoke_user_session(token)
