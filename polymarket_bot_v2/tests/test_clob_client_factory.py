"""T288 Wave 4 — ClobClientFactory tests.

Verifies the central factory:
  - returns the same instance for the same funder (caching)
  - returns distinct instances for different funders
  - get_user_gateway looks up users.funder_address
  - rejects invalid input
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test fixtures: install a fake py_clob_client_v2 + ensure factory cache cleared
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_v2_sdk(monkeypatch):
    """Install a minimal fake py_clob_client_v2 so V2BuilderAdapter._get_client
    does not try to load the real SDK / hit the network during tests."""
    mod = types.ModuleType("py_clob_client_v2")

    class _FakeClient:
        def __init__(self, **kw):
            self.kw = kw

    class _FakeApiCreds:
        def __init__(self, **kw):
            self.kw = kw

    mod.ClobClient = _FakeClient
    mod.ApiCreds = _FakeApiCreds
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", mod)

    # Ensure required env vars exist
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setenv("POLY_API_KEY", "fake-api-key")
    monkeypatch.setenv("POLY_SECRET", "fake-secret")
    monkeypatch.setenv("POLY_PASSPHRASE", "fake-pass")
    monkeypatch.setenv("POLY_RELAYER_ADDRESS", "0x" + "a" * 40)

    # Reset factory cache between tests
    from src.executor import clob_client_factory

    clob_client_factory.reset_cache()
    yield
    clob_client_factory.reset_cache()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_system_gateway_returns_singleton():
    from src.executor.clob_client_factory import get_system_gateway

    gw1 = get_system_gateway()
    gw2 = get_system_gateway()
    assert gw1 is gw2, "system gateway must be cached singleton"


def test_get_gateway_for_funder_caches_per_address():
    from src.executor.clob_client_factory import get_gateway_for_funder

    f1 = "0x" + "1" * 40
    f2 = "0x" + "2" * 40
    a = get_gateway_for_funder(f1)
    b = get_gateway_for_funder(f1)
    c = get_gateway_for_funder(f2)
    assert a is b, "same funder must yield same instance"
    assert a is not c, "different funders must yield distinct instances"


def test_get_gateway_for_funder_normalises_case():
    from src.executor.clob_client_factory import get_gateway_for_funder

    addr_lower = "0x" + "a" * 40
    addr_upper = "0x" + "A" * 40
    a = get_gateway_for_funder(addr_lower)
    b = get_gateway_for_funder(addr_upper)
    assert a is b, "funder address comparison must be case-insensitive"


def test_get_gateway_for_funder_rejects_invalid():
    from src.executor.clob_client_factory import get_gateway_for_funder

    for bad in ["", "notanaddr", "0xshort", None]:
        with pytest.raises(ValueError):
            get_gateway_for_funder(bad)  # type: ignore[arg-type]


def test_v2_builder_adapter_uses_constructor_funder_over_env(monkeypatch):
    """V2BuilderAdapter(funder=X) must override POLY_RELAYER_ADDRESS env."""
    monkeypatch.setenv("POLY_RELAYER_ADDRESS", "0x" + "f" * 40)
    from src.executor.v2_builder_adapter import V2BuilderAdapter

    custom_funder = "0x" + "b" * 40
    adapter = V2BuilderAdapter(host="https://x", funder=custom_funder)
    assert adapter.funder_address == custom_funder
    # Trigger lazy init and inspect the kwargs the SDK got
    client = adapter._get_client()  # noqa: SLF001
    assert client.kw["funder"] == custom_funder
    assert client.kw["signature_type"] == 2


def test_v2_builder_adapter_falls_back_to_env_funder(monkeypatch):
    """No constructor override → use POLY_RELAYER_ADDRESS env."""
    env_funder = "0x" + "c" * 40
    monkeypatch.setenv("POLY_RELAYER_ADDRESS", env_funder)
    from src.executor.v2_builder_adapter import V2BuilderAdapter

    adapter = V2BuilderAdapter(host="https://x")
    assert adapter.funder_address is None
    client = adapter._get_client()  # noqa: SLF001
    assert client.kw["funder"] == env_funder


def test_get_user_gateway_uses_db_funder():
    """get_user_gateway(wallet) → reads users.funder_address → returns gateway."""
    from src.executor import clob_client_factory

    funder = "0x" + "d" * 40
    wallet = "0x" + "e" * 40

    fake_row = {"funder_address": funder}
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = fake_row
    fake_conn.__enter__ = lambda self: fake_conn
    fake_conn.__exit__ = lambda *a: None

    with patch.object(clob_client_factory, "_pg_connect", return_value=fake_conn):
        gw = clob_client_factory.get_user_gateway(wallet)
        assert gw is not None
        # adapter should carry the looked-up funder
        assert gw.funder_address == funder  # type: ignore[attr-defined]


def test_get_user_gateway_raises_when_no_funder_bound():
    from src.executor import clob_client_factory

    wallet = "0x" + "1" * 40

    fake_row = {"funder_address": None}
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = fake_row
    fake_conn.__enter__ = lambda self: fake_conn
    fake_conn.__exit__ = lambda *a: None

    with patch.object(clob_client_factory, "_pg_connect", return_value=fake_conn):
        with pytest.raises(LookupError):
            clob_client_factory.get_user_gateway(wallet)


def test_get_user_gateway_rejects_invalid_wallet():
    from src.executor.clob_client_factory import get_user_gateway

    for bad in ["", "0xshort", "notwallet"]:
        with pytest.raises(ValueError):
            get_user_gateway(bad)
