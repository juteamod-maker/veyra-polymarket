from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts.dashboard_routes import copytrader_v2_routes as routes
from fastapi import HTTPException


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        if not self._rows:
            return None
        return self._rows[0]


class _FakeConn:
    def __init__(self) -> None:
        self.pool: dict[str, dict] = {}
        self.settings_rows: dict[str, dict] = {}

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO copytrader_v2_wallet_pool"):
            address, source = params
            row = self.pool.get(
                address,
                {
                    "address": address,
                    "source": source,
                    "last_metrics_json": "{}",
                    "score": 0.0,
                    "primary_category": "other",
                    "is_likely_bot": False,
                    "last_scored_at": None,
                },
            )
            row["source"] = source
            self.pool[address] = row
            return _FakeResult([])
        if normalized.startswith("SELECT address, source, last_metrics_json"):
            addresses = list(params)
            return _FakeResult([self.pool[address] for address in addresses if address in self.pool])
        if normalized.startswith("UPDATE copytrader_v2_wallet_pool SET last_metrics_json"):
            payload, score, category, is_likely_bot, address = params
            row = self.pool[address]
            row["last_metrics_json"] = payload
            row["score"] = score
            row["primary_category"] = category
            row["is_likely_bot"] = is_likely_bot
            row["last_scored_at"] = "2026-04-24T00:00:00+00:00"
            return _FakeResult([])
        if normalized.startswith("INSERT INTO copytrader_v2_settings"):
            _user_id, owner_wallet_address, settings_json, updated_at = params
            self.settings_rows[owner_wallet_address] = {
                "settings_json": settings_json,
                "updated_at": updated_at,
            }
            return _FakeResult([])
        raise AssertionError(f"Unexpected SQL: {normalized}")

    def commit(self):
        return None


class _FakeDbContext:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        return False


def test_wallets_search_paginates_and_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    dt = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    pool = [
        {
            "address": "0x1111111111111111111111111111111111111111",
            "source": "holders",
            "last_metrics_json": json.dumps({"max_drawdown_usd": 10, "trade_count_total": 7}),
            "score": 91.0,
            "primary_category": "sports",
            "is_likely_bot": False,
            "last_scored_at": dt,
            "pnl_realized_usd": 321.5,
            "win_rate_f": 0.62,
            "trade_count_30d": 12,
        },
        {
            "address": "0x2222222222222222222222222222222222222222",
            "source": "holders",
            "last_metrics_json": json.dumps({"max_drawdown_usd": 25, "trade_count_total": 9}),
            "score": 87.0,
            "primary_category": "sports",
            "is_likely_bot": True,
            "last_scored_at": dt,
            "pnl_realized_usd": 555.0,
            "win_rate_f": 0.71,
            "trade_count_30d": 18,
        },
        {
            "address": "0x3333333333333333333333333333333333333333",
            "source": "holders",
            "last_metrics_json": json.dumps({"max_drawdown_usd": 5, "trade_count_total": 4}),
            "score": 44.0,
            "primary_category": "crypto",
            "is_likely_bot": False,
            "last_scored_at": dt,
            "pnl_realized_usd": 40.0,
            "win_rate_f": 0.55,
            "trade_count_30d": 3,
        },
    ]

    class _SearchConn:
        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            rows = list(pool)
            idx = 0
            if "primary_category = ?" in normalized:
                cat = params[idx]
                idx += 1
                rows = [r for r in rows if r["primary_category"] == cat]
            if "score >= ?" in normalized:
                min_score = float(params[idx])
                idx += 1
                rows = [r for r in rows if float(r["score"] or 0) >= min_score]
            if "pnl_realized_usd >= ?" in normalized:
                min_pnl = float(params[idx])
                idx += 1
                rows = [r for r in rows if float(r["pnl_realized_usd"] or 0) >= min_pnl]
            if "trade_count_30d >= ?" in normalized:
                min_trades = int(params[idx])
                idx += 1
                rows = [r for r in rows if int(r["trade_count_30d"] or 0) >= min_trades]
            if "win_rate_f >= ?" in normalized:
                min_wr = float(params[idx])
                idx += 1
                rows = [r for r in rows if float(r["win_rate_f"] or 0) >= min_wr]
            if "(is_likely_bot IS NOT TRUE)" in normalized:
                rows = [r for r in rows if not r["is_likely_bot"]]

            if normalized.startswith("SELECT COUNT(*) as cnt FROM copytrader_v2_wallet_pool"):
                return _FakeResult([{"cnt": len(rows)}])
            if normalized.startswith("SELECT address, source, last_metrics_json"):
                limit = int(params[-2])
                offset = int(params[-1])
                rows = sorted(rows, key=lambda r: float(r["score"]), reverse=True)
                return _FakeResult(rows[offset:offset + limit])
            if normalized.startswith("SELECT MAX(last_scored_at) as latest FROM copytrader_v2_wallet_pool"):
                latest = max((r["last_scored_at"] for r in pool), default=None)
                return _FakeResult([{"latest": latest}])
            raise AssertionError(f"Unexpected SQL: {normalized}")

    def fake_db_connect():
        return _FakeDbContext(_SearchConn())

    monkeypatch.setattr(routes, "db_connect", fake_db_connect)
    monkeypatch.setattr(routes, "_require_identity", lambda request: {"authenticated": True})
    monkeypatch.setattr(routes, "_resolve_owner_wallet", lambda request: "0xabc")
    monkeypatch.setattr(routes, "_load_user_exclude_bots", lambda owner_wallet_address: True)

    response = asyncio.run(
        routes.wallets_search(
            SimpleNamespace(),
            category="sports",
            sort_by="score",
            min_score=80.0,
            min_pnl_usd=100.0,
            min_trades_30d=10,
            min_win_rate=0.6,
            exclude_bots=True,
            page=1,
            limit=20,
        )
    )
    body = json.loads(response.body)
    assert body["count"] == 1
    assert body["total"] == 1
    assert body["exclude_bots"] is True
    assert body["cache_updated_at"] == dt.isoformat()
    assert body["wallets"][0]["address"] == "0x1111111111111111111111111111111111111111"
    assert body["wallets"][0]["last_scored_at"] == dt.isoformat()
    assert body["wallets"][0]["is_likely_bot"] is False


def test_resolve_owner_wallet_user_fallback_to_session_token(monkeypatch: pytest.MonkeyPatch) -> None:
    ident = {
        "authenticated": True,
        "is_admin": False,
        "wallet_address": "",
    }
    monkeypatch.setattr(routes, "_require_identity", lambda request: ident)

    req = SimpleNamespace(cookies={"user_session": "tok_abc_123"})
    owner = routes._resolve_owner_wallet(req)
    expected = "session_" + hashlib.sha256("tok_abc_123".encode("utf-8")).hexdigest()[:40]
    assert owner == expected


def test_resolve_owner_wallet_user_without_wallet_and_session_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    ident = {
        "authenticated": True,
        "is_admin": False,
        "wallet_address": "",
    }
    monkeypatch.setattr(routes, "_require_identity", lambda request: ident)

    req = SimpleNamespace(cookies={})
    with pytest.raises(HTTPException) as exc:
        routes._resolve_owner_wallet(req)
    assert exc.value.status_code == 401
    assert exc.value.detail == "no_owner_identity"


def test_resolve_owner_wallet_admin_not_mixed_with_user(monkeypatch: pytest.MonkeyPatch) -> None:
    ident = {
        "authenticated": True,
        "is_admin": True,
        "wallet_address": "__admin__",
    }
    monkeypatch.setattr(routes, "_require_identity", lambda request: ident)

    req = SimpleNamespace(cookies={"user_session": "tok_abc_123"})
    owner = routes._resolve_owner_wallet(req)
    assert owner == "__admin__"


def test_settings_put_succeeds_for_user_session_without_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn()

    def fake_db_connect():
        return _FakeDbContext(conn)

    ident = {
        "authenticated": True,
        "is_admin": False,
        "wallet_address": "",
    }
    monkeypatch.setattr(routes, "db_connect", fake_db_connect)
    monkeypatch.setattr(routes, "_require_identity", lambda request: ident)
    monkeypatch.setattr(routes, "cfg_get", lambda key: 25.0)

    req = SimpleNamespace(cookies={"user_session": "tok_normal_user"})
    payload = routes.SettingsPut(settings={"enabled": True, "default_base_usdc": 6.5})

    resp = asyncio.run(routes.settings_put(req, payload))
    body = json.loads(resp.body)
    assert body.get("ok") is True

    owner = "session_" + hashlib.sha256("tok_normal_user".encode("utf-8")).hexdigest()[:40]
    assert owner in conn.settings_rows
    stored = json.loads(conn.settings_rows[owner]["settings_json"])
    assert stored.get("enabled") is True
    assert float(stored.get("default_base_usdc")) == 6.5
