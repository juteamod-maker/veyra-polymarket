"""Risk resume API regression tests.

Focus:
1) /api/risk/status should normalize pause_reason.
2) /api/risk/resume should clear pause_state, sync emergency_freeze, and report success.
3) /api/risk/resume should still succeed with warning when bot restart fails.
"""

import asyncio
import json
from pathlib import Path

import pytest

from scripts.dashboard_routes import system


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _admin_identity(monkeypatch):
    """T901: `/api/risk/resume` 现在要求 admin / super_admin。

    注入管理员身份，使本模块继续走到与改动前相同的断言路径（这 3 个用例本身
    在 pristine HEAD 上即为过期断言 `assert isinstance(result, dict)`，与本次
    改动无关）。
    """
    monkeypatch.setattr(
        system,
        "get_request_identity",
        lambda _request: {
            "authenticated": True,
            "role": "admin",
            "wallet_address": "0x" + "0" * 40,
            "is_admin": True,
        },
    )


class _Rows:
    def __init__(self, rows=None, one=None):
        self._rows = rows or []
        self._one = one

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class _ConnStatus:
    def execute(self, query, *args, **kwargs):
        if "SELECT key, value FROM pause_state" in query:
            return _Rows(
                rows=[
                    ("globally_paused", "true"),
                    ("pause_reason", " daily "),
                    ("paused_cats", "weather"),
                ]
            )
        raise AssertionError(f"unexpected query: {query}")


class _ConnResume:
    def execute(self, query, *args, **kwargs):
        if "SELECT key FROM pause_state" in query:
            return _Rows(
                rows=[
                    ("globally_paused",),
                    ("pause_reason",),
                    ("paused_cats",),
                    ("streak_weather",),
                ]
            )
        if "SELECT usdc_balance FROM balance_log" in query:
            return _Rows(one=(123.4567,))
        raise AssertionError(f"unexpected query: {query}")


class _DbCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        return False


def test_api_risk_status_normalizes_reason(monkeypatch):
    monkeypatch.setattr(system, "db_connect", lambda: _DbCtx(_ConnStatus()))

    result = asyncio.run(system.api_risk_status())
    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["globally_paused"] is True
    assert result["pause_reason"] == "daily"
    assert result["paused_categories"] == ["weather"]


def test_api_risk_resume_success_syncs_stop_params(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"risk": {"emergency_freeze": True}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setattr(system, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(system, "db_connect", lambda: _DbCtx(_ConnResume()))

    calls = []

    def _fake_save_pause_state(key, value):
        calls.append((key, value))

    # api_risk_resume imports inside function, so patch source symbol
    import src.utils.db as db_mod

    monkeypatch.setattr(db_mod, "save_pause_state", _fake_save_pause_state)

    events = []

    def _fake_systemctl(action, service):
        events.append((action, service))
        return True, "ok"

    monkeypatch.setattr(system, "_run_systemctl", _fake_systemctl)

    result = asyncio.run(system.api_risk_resume(_FakeRequest({"confirm": "RESUME"})))

    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["restarted"] is True
    assert result["synced_stop_params"]["emergency_freeze_old"] is True
    assert result["synced_stop_params"]["emergency_freeze_new"] is False

    updated_cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    assert updated_cfg["risk"]["emergency_freeze"] is False

    assert ("stop", "polymarket-bot") in events
    assert ("start", "polymarket-bot") in events

    # Must clear core pause keys and streak key
    assert ("globally_paused", "false") in calls
    assert ("pause_reason", "") in calls
    assert ("paused_cats", "") in calls
    assert ("streak_weather", "0") in calls
    assert any(k == "peak_nlv_override" for k, _ in calls)


def test_api_risk_resume_returns_warning_on_restart_failure(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"risk": {"emergency_freeze": True}}), encoding="utf-8"
    )
    monkeypatch.setattr(system, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(system, "db_connect", lambda: _DbCtx(_ConnResume()))

    import src.utils.db as db_mod

    monkeypatch.setattr(db_mod, "save_pause_state", lambda *args, **kwargs: None)

    def _fake_systemctl(action, service):
        if action == "start":
            return False, "permission denied"
        return True, "ok"

    monkeypatch.setattr(system, "_run_systemctl", _fake_systemctl)

    result = asyncio.run(system.api_risk_resume(_FakeRequest({"confirm": "RESUME"})))

    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["restarted"] is False
    assert "warning" in result
    assert result["start_service_ok"] is False
