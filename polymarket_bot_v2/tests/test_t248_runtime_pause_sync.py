"""T248: 运行时风控暂停状态与 DB 对账回归测试。"""

import main


class _DummyMonitor:
    def __init__(self, paused: bool, reason: str | None = None):
        self._paused = paused
        self._reason = reason
        self.restore_calls = 0

    @property
    def globally_paused(self) -> bool:
        return self._paused

    @property
    def pause_reason(self) -> str | None:
        return self._reason

    def restore_pause_state(self, state: dict[str, str]) -> None:
        self.restore_calls += 1
        self._paused = (
            str(state.get("globally_paused", "false")).strip().lower() == "true"
        )
        self._reason = state.get("pause_reason") or None


def test_t248_sync_runtime_pause_state_from_db_unpauses(monkeypatch):
    monitor = _DummyMonitor(paused=True, reason="daily")
    monkeypatch.setattr(
        main,
        "get_all_pause_state",
        lambda: {
            "globally_paused": "false",
            "pause_reason": "",
            "paused_cats": "",
        },
    )

    changed = main._sync_runtime_pause_state_from_db(monitor)

    assert changed is True
    assert monitor.restore_calls == 1
    assert monitor.globally_paused is False
    assert monitor.pause_reason is None


def test_t248_sync_runtime_pause_state_from_db_keeps_paused(monkeypatch):
    monitor = _DummyMonitor(paused=True, reason="daily")
    monkeypatch.setattr(
        main,
        "get_all_pause_state",
        lambda: {
            "globally_paused": "true",
            "pause_reason": "daily",
            "paused_cats": "weather",
        },
    )

    changed = main._sync_runtime_pause_state_from_db(monitor)

    assert changed is False
    assert monitor.restore_calls == 0
    assert monitor.globally_paused is True
    assert monitor.pause_reason == "daily"


def test_t248_runtime_pause_reconcile_tick_triggers_scan_when_unpaused(monkeypatch):
    monitor = _DummyMonitor(paused=False, reason=None)
    monkeypatch.setattr(main, "_sync_runtime_pause_state_from_db", lambda m: True)

    should_trigger = main._runtime_pause_reconcile_tick(monitor)

    assert should_trigger is True


def test_t248_runtime_pause_reconcile_tick_no_trigger_when_still_paused(monkeypatch):
    monitor = _DummyMonitor(paused=True, reason="daily")
    monkeypatch.setattr(main, "_sync_runtime_pause_state_from_db", lambda m: True)

    should_trigger = main._runtime_pause_reconcile_tick(monitor)

    assert should_trigger is False
