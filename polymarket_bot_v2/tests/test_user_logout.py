from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.dashboard_routes import user_routes


class _FakeCursor:
    def __init__(self, rowcount: int = 0):
        self.rowcount = rowcount

    def fetchone(self):
        return None


class _FakeConn:
    def __init__(self):
        self.calls = []
        self.committed = False

    def execute(self, sql, params=()):
        self.calls.append((" ".join(str(sql).split()).lower(), tuple(params)))
        return _FakeCursor(rowcount=1)

    def commit(self):
        self.committed = True


class _DbCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        return False


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(user_routes.router)
    return app


def test_logout_revokes_current_and_wallet_sessions_and_clears_cookies(monkeypatch):
    fake_conn = _FakeConn()
    revoked = {"token": ""}

    monkeypatch.setattr(user_routes, "db_connect", lambda: _DbCtx(fake_conn))
    monkeypatch.setattr(
        user_routes,
        "get_request_identity",
        lambda _req: {
            "role": "manual_user",
            "wallet_address": "0x1538cccf03bd2d95c210e22eab15e7a606faab35",
        },
    )
    monkeypatch.setattr(
        user_routes,
        "revoke_user_session",
        lambda token: revoked.update({"token": token}) or True,
    )

    client = TestClient(_build_app())
    resp = client.post(
        "/api/user/logout",
        cookies={
            "user_session": "session-token-1",
            "dashboard_key": "k",
            "totp_session": "t",
            "mfa_verified": "m",
        },
    )

    assert resp.status_code == 200
    assert revoked["token"] == "session-token-1"
    assert fake_conn.committed is True
    assert any(
        "delete from user_sessions where wallet_address=?" in sql
        and params == ("0x1538cccf03bd2d95c210e22eab15e7a606faab35",)
        for sql, params in fake_conn.calls
    )

    set_cookies = "\n".join(resp.headers.get_list("set-cookie"))
    assert "user_session=" in set_cookies
    assert "dashboard_key=" in set_cookies
    assert "totp_session=" in set_cookies
    assert "mfa_verified=" in set_cookies
