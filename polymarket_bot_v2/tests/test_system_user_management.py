import secrets
from datetime import datetime, timedelta, timezone

import scripts.web_dashboard as wd
from fastapi.testclient import TestClient
from scripts.dashboard_routes import _shared


def _admin_client(monkeypatch):
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(wd, "TOTP_SECRET", "")
    monkeypatch.setattr(_shared, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(_shared, "TOTP_SECRET", "")
    return TestClient(wd.app)


def _seed_user(wallet: str, role: str = "manual_user", pm_wallet: str | None = None):
    _shared._ensure_user_tables()
    now = datetime.now(timezone.utc).isoformat()
    with _shared.db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users(wallet_address, role, display_name, privy_user_id, auth_provider,
                              polymarket_wallet, created_at, last_login_at, source_ip, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                role=excluded.role,
                display_name=excluded.display_name,
                privy_user_id=excluded.privy_user_id,
                auth_provider=excluded.auth_provider,
                polymarket_wallet=excluded.polymarket_wallet,
                last_login_at=excluded.last_login_at,
                source_ip=excluded.source_ip,
                updated_at=excluded.updated_at
            """,
            (
                wallet,
                role,
                "tester",
                f"privy_{wallet[-8:]}",
                "privy",
                pm_wallet,
                now,
                now,
                "127.0.0.1",
                now,
            ),
        )
        conn.commit()


def _seed_session(wallet: str):
    _shared._ensure_user_tables()
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(24)
    with _shared.db_connect() as conn:
        conn.execute(
            """
            INSERT INTO user_sessions(token, wallet_address, role, created_at, expires_at, last_used_at, source_ip)
            VALUES(?, ?, 'manual_user', ?, ?, ?, '127.0.0.1')
            """,
            (
                token,
                wallet,
                now.isoformat(),
                (now + timedelta(days=2)).isoformat(),
                now.isoformat(),
            ),
        )
        conn.commit()


def _cleanup_user(wallet: str):
    with _shared.db_connect() as conn:
        for table in (
            "user_sessions",
            "user_runtime_controls",
            "user_subscriptions",
            "point_transactions",
            "fee_transactions",
            "user_points",
            "user_pm_profiles",
            "user_pm_equity_snapshots",
            "user_pm_positions_snapshots",
            "user_pm_activity_snapshots",
            "users",
        ):
            try:
                conn.execute(f"DELETE FROM {table} WHERE wallet_address=?", (wallet,))
            except Exception:
                pass
        conn.commit()


def test_system_users_list_and_runtime_update(monkeypatch):
    c = _admin_client(monkeypatch)
    wallet = f"0x{secrets.token_hex(20)}"
    try:
        _seed_user(wallet, role="manual_user", pm_wallet="0xpmtest")

        r1 = c.get(f"/api/system/users/list?key=k&q={wallet}")
        assert r1.status_code == 200
        data = r1.json()
        assert data["status"] == "ok"
        assert any(u["wallet_address"] == wallet for u in data["users"])

        r2 = c.post(
            "/api/system/users/runtime?key=k",
            json={
                "wallet_address": wallet,
                "freeze_auto": True,
                "auto_trading_enabled": False,
            },
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["freeze_auto"] is True
        assert d2["auto_trading_enabled"] is False

        r3 = c.get(f"/api/system/users/list?key=k&q={wallet}")
        users = r3.json().get("users") or []
        matched = [u for u in users if u["wallet_address"] == wallet][0]
        assert matched["freeze_auto"] is True
        assert matched["auto_trading_enabled"] is False
    finally:
        _cleanup_user(wallet)


def test_system_users_revoke_sessions_and_unbind(monkeypatch):
    c = _admin_client(monkeypatch)
    wallet = f"0x{secrets.token_hex(20)}"
    try:
        _seed_user(wallet, role="manual_user", pm_wallet="0xpmtest")
        _seed_session(wallet)

        r1 = c.post(
            "/api/system/users/revoke-sessions?key=k",
            json={"wallet_address": wallet},
        )
        assert r1.status_code == 200
        assert int(r1.json().get("revoked_sessions") or 0) >= 1

        r2 = c.post(
            "/api/system/users/unbind-pm-wallet?key=k",
            json={"wallet_address": wallet},
        )
        assert r2.status_code == 200

        with _shared.db_connect() as conn:
            row = conn.execute(
                "SELECT polymarket_wallet FROM users WHERE wallet_address=?", (wallet,)
            ).fetchone()
            assert row is not None
            assert row["polymarket_wallet"] is None
    finally:
        _cleanup_user(wallet)


def test_system_users_delete_forbids_admin_and_deletes_manual_user(monkeypatch):
    c = _admin_client(monkeypatch)

    admin_wallet = f"0x{secrets.token_hex(20)}"
    user_wallet = f"0x{secrets.token_hex(20)}"
    try:
        _seed_user(admin_wallet, role="admin")
        _seed_user(user_wallet, role="manual_user")

        r1 = c.post(
            "/api/system/users/delete?key=k",
            json={"wallet_address": admin_wallet, "confirm": "DELETE"},
        )
        assert r1.status_code == 403

        r2 = c.post(
            "/api/system/users/delete?key=k",
            json={"wallet_address": user_wallet, "confirm": "DELETE"},
        )
        assert r2.status_code == 200
        assert int(r2.json().get("deleted") or 0) == 1

        with _shared.db_connect() as conn:
            row = conn.execute(
                "SELECT wallet_address FROM users WHERE wallet_address=?",
                (user_wallet,),
            ).fetchone()
            assert row is None
    finally:
        _cleanup_user(admin_wallet)
        _cleanup_user(user_wallet)


def test_system_users_save_profile_atomic_create_and_update(monkeypatch):
    c = _admin_client(monkeypatch)
    wallet = f"0x{secrets.token_hex(20)}"
    try:
        create_resp = c.post(
            "/api/system/users/save-profile?key=k",
            json={
                "mode": "create",
                "wallet_address": wallet,
                "role": "manual_user",
                "display_name": "alpha",
                "polymarket_wallet": "0xpmalpha",
                "subscription": {
                    "plan": "pro",
                    "status": "active",
                    "seats": 2,
                    "notes": "created-by-test",
                },
                "runtime": {
                    "freeze_auto": False,
                    "auto_trading_enabled": True,
                },
            },
        )
        assert create_resp.status_code == 200
        assert create_resp.json().get("status") == "ok"

        with _shared.db_connect() as conn:
            urow = conn.execute(
                "SELECT role, display_name, polymarket_wallet FROM users WHERE wallet_address=?",
                (wallet,),
            ).fetchone()
            srow = conn.execute(
                "SELECT plan, status, seats, notes FROM user_subscriptions WHERE wallet_address=?",
                (wallet,),
            ).fetchone()
            rrow = conn.execute(
                "SELECT freeze_auto, auto_trading_enabled FROM user_runtime_controls WHERE wallet_address=?",
                (wallet,),
            ).fetchone()

        assert urow is not None
        assert urow["role"] == "manual_user"
        assert urow["display_name"] == "alpha"
        assert urow["polymarket_wallet"] == "0xpmalpha"

        assert srow is not None
        assert srow["plan"] == "pro"
        assert srow["status"] == "active"
        assert int(srow["seats"]) == 2
        assert srow["notes"] == "created-by-test"

        assert rrow is not None
        assert int(rrow["freeze_auto"]) == 0
        assert int(rrow["auto_trading_enabled"]) == 1

        update_resp = c.post(
            "/api/system/users/save-profile?key=k",
            json={
                "mode": "update",
                "wallet_address": wallet,
                "role": "admin",
                "display_name": "beta",
                "polymarket_wallet": "",
                "subscription": {
                    "plan": "enterprise",
                    "status": "paused",
                    "seats": 5,
                    "notes": "updated-by-test",
                },
                "runtime": {
                    "freeze_auto": True,
                    "auto_trading_enabled": False,
                },
            },
        )
        assert update_resp.status_code == 200
        assert update_resp.json().get("status") == "ok"

        with _shared.db_connect() as conn:
            urow2 = conn.execute(
                "SELECT role, display_name, polymarket_wallet FROM users WHERE wallet_address=?",
                (wallet,),
            ).fetchone()
            srow2 = conn.execute(
                "SELECT plan, status, seats, notes FROM user_subscriptions WHERE wallet_address=?",
                (wallet,),
            ).fetchone()
            rrow2 = conn.execute(
                "SELECT freeze_auto, auto_trading_enabled FROM user_runtime_controls WHERE wallet_address=?",
                (wallet,),
            ).fetchone()

        assert urow2 is not None
        assert urow2["role"] == "admin"
        assert urow2["display_name"] == "beta"
        assert urow2["polymarket_wallet"] is None

        assert srow2 is not None
        assert srow2["plan"] == "enterprise"
        assert srow2["status"] == "paused"
        assert int(srow2["seats"]) == 5
        assert srow2["notes"] == "updated-by-test"

        assert rrow2 is not None
        assert int(rrow2["freeze_auto"]) == 1
        assert int(rrow2["auto_trading_enabled"]) == 0
    finally:
        _cleanup_user(wallet)


def test_user_points_endpoint_allows_admin_identity(monkeypatch):
    c = _admin_client(monkeypatch)
    try:
        resp = c.get("/api/user/points?key=k")
        assert resp.status_code == 200
        payload = resp.json()
        assert "balance" in payload
        assert "total_earned" in payload
        assert "recent_transactions" in payload
    finally:
        _cleanup_user("__admin__")


def test_user_points_endpoint_normalizes_signed_admin_transactions(monkeypatch):
    c = _admin_client(monkeypatch)
    wallet = "__admin__"
    now = datetime.now(timezone.utc)
    try:
        _seed_user(wallet, role="admin")
        with _shared.db_connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_points(wallet_address,balance,total_earned,total_spent,updated_at)
                VALUES(?,?,?,?,?)
                """,
                (wallet, 130, 200, 70, now.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO point_transactions(wallet_address,type,amount,source,description,created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    wallet,
                    "spend",
                    5,
                    "trade_fee",
                    "spent points",
                    (now - timedelta(minutes=3)).isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO point_transactions(wallet_address,type,amount,source,description,created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    wallet,
                    "admin_deduct",
                    -10,
                    "admin_v3",
                    "manual deduct",
                    (now - timedelta(minutes=2)).isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO point_transactions(wallet_address,type,amount,source,description,created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    wallet,
                    "admin_adjust",
                    25,
                    "admin_v3",
                    "manual add",
                    (now - timedelta(minutes=1)).isoformat(),
                ),
            )
            conn.commit()

        resp = c.get("/api/user/points?key=k")
        assert resp.status_code == 200
        payload = resp.json()
        txs = payload.get("recent_transactions") or []
        assert len(txs) >= 3
        assert txs[0]["delta"] == 25
        assert txs[1]["delta"] == -10
        assert txs[2]["delta"] == -5
    finally:
        _cleanup_user(wallet)
