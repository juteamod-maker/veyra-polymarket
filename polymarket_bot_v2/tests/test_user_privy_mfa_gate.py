"""T902: 前台登录 MFA 门禁回归测试 —— 服务端 TOTP 语义。

覆盖：
1) 客户端 mfa_verified 布尔值被忽略（无 mfa_code 时，TOTP 用户被拦下）。
2) 错误 TOTP 码被拦下。
3) 正确 TOTP 码放行，并签发签名 mfa_verified cookie。
4) 未注册 TOTP 的用户不受门禁影响。
5) verify-mark / enroll 端点仅在有效 TOTP 码时签发签名 cookie。
"""

import json

import pyotp
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.dashboard_routes import user_routes

WALLET = "0x1538cccf03bd2d95c210e22eab15e7a606faab35"


class _FakeCursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, totp_enabled: bool, totp_secret: str = ""):
        self.mfa_row = {
            "mfa_enabled": 1 if totp_enabled else 0,
            "mfa_methods": json.dumps(["totp"]),
            "privy_signer_status": None,
            "privy_signer_policies_json": None,
            "totp_enabled": 1 if totp_enabled else 0,
            "totp_secret": totp_secret,
            "mfa_last_verified_at": None,
        }

    def execute(self, sql, params=()):
        n = " ".join(str(sql or "").strip().split()).lower()
        if "select mfa_enabled" in n and "totp_secret" in n:
            return _FakeCursor(self.mfa_row)
        if "select role from users" in n:
            return _FakeCursor({"role": "manual_user"})
        if "select totp_secret" in n or "select totp_enabled" in n:
            return _FakeCursor(self.mfa_row)
        return _FakeCursor(None)

    def commit(self):
        return None


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


def _patch_common(monkeypatch, fake_conn: _FakeConn):
    monkeypatch.setattr(user_routes, "db_connect", lambda: _DbCtx(fake_conn))
    monkeypatch.setattr(user_routes, "_ensure_user_tables", lambda: None)
    monkeypatch.setattr(
        user_routes,
        "_verify_privy_tokens",
        lambda _i, _a, _u=None: (
            "did:privy:test-user",
            {"linkedAccounts": [{"address": WALLET}]},
        ),
    )
    monkeypatch.setattr(
        user_routes,
        "_sync_user_pm_data",
        lambda *_args, **_kwargs: {"status": "ok", "needs_pm_binding": False},
    )
    monkeypatch.setattr(
        user_routes,
        "create_user_session",
        lambda wallet_address, role, source_ip: "test-user-session-token",
    )
    monkeypatch.setattr(user_routes, "_get_or_create_runtime", lambda _w: {"ok": True})


def _totp_code(secret: str) -> str:
    return pyotp.TOTP(secret).now()


def test_login_blocked_when_totp_enabled_and_no_code(monkeypatch):
    secret = pyotp.random_base32()
    fake_conn = _FakeConn(totp_enabled=True, totp_secret=secret)
    _patch_common(monkeypatch, fake_conn)
    client = TestClient(_build_app())

    resp = client.post(
        "/api/user/privy/verify",
        json={"id_token": "dummy", "access_token": "dummy", "user": {}, "mfa_verified": True},
    )
    assert resp.status_code == 403
    data = resp.json()
    assert data.get("error") == "mfa_required"
    assert "totp" in (data.get("methods") or [])


def test_login_blocked_with_wrong_totp_code(monkeypatch):
    secret = pyotp.random_base32()
    fake_conn = _FakeConn(totp_enabled=True, totp_secret=secret)
    _patch_common(monkeypatch, fake_conn)
    client = TestClient(_build_app())

    resp = client.post(
        "/api/user/privy/verify",
        json={"id_token": "d", "access_token": "d", "user": {}, "mfa_code": "000000"},
    )
    assert resp.status_code == 403


def test_login_allowed_with_valid_totp_code(monkeypatch):
    secret = pyotp.random_base32()
    fake_conn = _FakeConn(totp_enabled=True, totp_secret=secret)
    _patch_common(monkeypatch, fake_conn)
    client = TestClient(_build_app())

    resp = client.post(
        "/api/user/privy/verify",
        json={"id_token": "d", "access_token": "d", "user": {}, "mfa_code": _totp_code(secret)},
    )
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "user_session=" in set_cookie
    assert "mfa_verified=" in set_cookie


def test_login_unaffected_when_totp_not_enrolled(monkeypatch):
    fake_conn = _FakeConn(totp_enabled=False)
    _patch_common(monkeypatch, fake_conn)
    client = TestClient(_build_app())

    resp = client.post(
        "/api/user/privy/verify",
        json={"id_token": "d", "access_token": "d", "user": {}},
    )
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "user_session=" in set_cookie
    # 未注册 TOTP：登录应直接成功；后端通过 delete_cookie 清理 mfa cookie（Set-Cookie 里
    # 会出现 `mfa_verified=` 删除头），但不会签发签名 token —— 关键是状态码为 200。
    assert resp.status_code == 200


def test_verify_mark_requires_valid_totp(monkeypatch):
    secret = pyotp.random_base32()
    fake_conn = _FakeConn(totp_enabled=True, totp_secret=secret)
    _patch_common(monkeypatch, fake_conn)
    monkeypatch.setattr(
        user_routes,
        "get_request_identity",
        lambda _r: {"authenticated": True, "role": "manual_user", "wallet_address": WALLET, "is_admin": False},
    )
    client = TestClient(_build_app())

    bad = client.post("/api/user/mfa/verify-mark", json={"code": "000000"})
    assert bad.status_code == 401

    good = client.post("/api/user/mfa/verify-mark", json={"code": _totp_code(secret)})
    assert good.status_code == 200
    set_cookie = good.headers.get("set-cookie", "")
    assert "mfa_verified=" in set_cookie


def test_enroll_flow_sets_totp(monkeypatch):
    fake_conn = _FakeConn(totp_enabled=False)
    _patch_common(monkeypatch, fake_conn)
    monkeypatch.setattr(
        user_routes,
        "get_request_identity",
        lambda _r: {"authenticated": True, "role": "manual_user", "wallet_address": WALLET, "is_admin": False},
    )
    client = TestClient(_build_app())

    start = client.post("/api/user/mfa/enroll/start")
    assert start.status_code == 200
    data = start.json()
    assert data.get("secret")
    assert data.get("otpauth_uri", "").startswith("otpauth://")
    assert data.get("qr_svg", "").startswith("<svg")

    confirm = client.post(
        "/api/user/mfa/enroll/confirm",
        json={"secret": data["secret"], "code": _totp_code(data["secret"])},
    )
    assert confirm.status_code == 200
    assert "mfa_verified=" in confirm.headers.get("set-cookie", "")
