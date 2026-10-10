"""
Tests for V2 core abstraction layer (Phase 1 batch 1):
  - SecretsProvider / EnvSecretsProvider
  - ExecutionGateway data classes
  - V2BuilderAdapter (mocked, no real network)
  - _encode_metadata helper
"""

import importlib
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Make polymarket_bot_v2/src importable from tests/
# ---------------------------------------------------------------------------
V2_ROOT = os.path.join(os.path.dirname(__file__), "..")
if V2_ROOT not in sys.path:
    sys.path.insert(0, V2_ROOT)


# ===========================================================================
# SecretsProvider / EnvSecretsProvider
# ===========================================================================


class TestEnvSecretsProvider:
    def test_get_existing_var(self, monkeypatch):
        monkeypatch.setenv("POLY_PRIVATE_KEY", "0xdeadbeef")
        from src.secrets.env_secrets_provider import EnvSecretsProvider

        provider = EnvSecretsProvider()
        assert provider.get_secret("POLY_PRIVATE_KEY") == "0xdeadbeef"

    def test_get_missing_var_returns_none(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_SECRET", raising=False)
        from src.secrets.env_secrets_provider import EnvSecretsProvider

        provider = EnvSecretsProvider()
        assert provider.get_secret("NONEXISTENT_SECRET") is None

    def test_get_empty_var_returns_none(self, monkeypatch):
        monkeypatch.setenv("POLY_API_KEY", "")
        from src.secrets.env_secrets_provider import EnvSecretsProvider

        provider = EnvSecretsProvider()
        assert provider.get_secret("POLY_API_KEY") is None

    def test_require_secret_raises_on_missing(self, monkeypatch):
        monkeypatch.delenv("POLY_SECRET", raising=False)
        from src.secrets.env_secrets_provider import EnvSecretsProvider

        provider = EnvSecretsProvider()
        with pytest.raises(ValueError, match="POLY_SECRET"):
            provider.require_secret("POLY_SECRET")

    def test_tenant_id_ignored_in_mvp(self, monkeypatch):
        monkeypatch.setenv("POLY_PASSPHRASE", "pass123")
        from src.secrets.env_secrets_provider import EnvSecretsProvider

        provider = EnvSecretsProvider()
        # tenant_id is accepted but ignored in MVP
        assert provider.get_secret("POLY_PASSPHRASE", tenant_id="tenant_A") == "pass123"

    def test_get_secrets_provider_singleton(self):
        # Importing twice should return the same instance
        from src.secrets import get_secrets_provider

        p1 = get_secrets_provider()
        p2 = get_secrets_provider()
        assert p1 is p2


# ===========================================================================
# ExecutionGateway dataclasses
# ===========================================================================


class TestExecutionGatewayDataClasses:
    def test_order_request_defaults(self):
        from src.executor.execution_gateway import (
            ExecutionOrderRequest,
            ExecutionOrderType,
            OrderSide,
        )

        req = ExecutionOrderRequest(
            token_id="abc123",
            price=0.45,
            size=10.0,
            side=OrderSide.BUY,
        )
        assert req.order_type == ExecutionOrderType.GTC
        assert req.tick_size == "0.01"
        assert req.builder_code is None
        assert req.signal_id is None

    def test_order_result_defaults(self):
        from src.executor.execution_gateway import ExecutionOrderResult

        r = ExecutionOrderResult(success=True, order_id="oid-001")
        assert r.filled_size == 0.0
        assert r.error is None
        assert r.raw_response == {}

    def test_balance_info_defaults(self):
        from src.executor.execution_gateway import BalanceInfo

        b = BalanceInfo(collateral_balance=42.5)
        assert b.currency == "pUSD"
        assert b.total_shares_value == 0.0


# ===========================================================================
# _encode_metadata helper
# ===========================================================================


class TestEncodeMetadata:
    def _fn(self):
        from src.executor.v2_builder_adapter import _encode_metadata

        return _encode_metadata

    def test_none_returns_zero_bytes32(self):
        fn = self._fn()
        result = fn(None)
        assert result == "0x" + "0" * 64

    def test_empty_returns_zero_bytes32(self):
        fn = self._fn()
        assert fn("") == "0x" + "0" * 64

    def test_already_bytes32_passthrough(self):
        fn = self._fn()
        val = "0x" + "a" * 64
        assert fn(val) == val

    def test_short_string_padded_to_32_bytes(self):
        fn = self._fn()
        result = fn("hello")
        assert result.startswith("0x")
        assert len(result) == 66  # 0x + 64 hex chars

    def test_long_string_truncated(self):
        fn = self._fn()
        # 33 chars > 32 bytes → should truncate to last 32 bytes
        result = fn("a" * 33)
        assert len(result) == 66


# ===========================================================================
# V2BuilderAdapter (mocked)
# ===========================================================================


def _make_mock_v2_module():
    """Create a minimal fake py_clob_client_v2 module for testing."""
    mod = types.ModuleType("py_clob_client_v2")

    class Side:
        BUY = "BUY"
        SELL = "SELL"

    class OrderType:
        GTC = "GTC"
        FOK = "FOK"
        FAK = "FAK"

    class ApiCreds:
        def __init__(self, api_key, api_secret, api_passphrase):
            self.api_key = api_key

    class PartialCreateOrderOptions:
        def __init__(self, tick_size="0.01"):
            self.tick_size = tick_size

    class OrderArgs:
        def __init__(self, token_id, price, size, side, builder=None, metadata=None):
            self.token_id = token_id
            self.price = price
            self.size = size
            self.side = side
            self.builder = builder
            self.metadata = metadata

    class OpenOrderParams:
        pass

    class ClobClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def create_and_post_order(self, order_args, options, order_type):
            return {"orderID": "mock-order-001", "status": "resting"}

        def cancel(self, order_id):
            return True

        def cancel_all(self):
            return True

        def get_order(self, order_id):
            return {"id": order_id, "status": "open"}

        def get_orders(self, params):
            return []

        def get_balance_allowance(self, params=None):
            return {"balance": 100.0}

        def get_order_book(self, token_id):
            return {"bids": [], "asks": []}

        def get_midpoint(self, token_id):
            return 0.5

    mod.Side = Side
    mod.OrderType = OrderType
    mod.ApiCreds = ApiCreds
    mod.PartialCreateOrderOptions = PartialCreateOrderOptions
    mod.OrderArgs = OrderArgs
    mod.OpenOrderParams = OpenOrderParams
    mod.ClobClient = ClobClient
    return mod


MOCK_ENV = {
    "POLY_PRIVATE_KEY": "0x" + "a" * 64,
    "POLY_API_KEY": "apikey",
    "POLY_SECRET": "apisecret",
    "POLY_PASSPHRASE": "passphrase",
    "POLY_RELAYER_ADDRESS": "0x" + "b" * 40,
    "POLY_BUILDER_CODE": "0x" + "c" * 64,
}


class TestV2BuilderAdapter:
    def setup_method(self):
        # Inject mock module before each test
        sys.modules["py_clob_client_v2"] = _make_mock_v2_module()
        for key, val in MOCK_ENV.items():
            os.environ[key] = val

    def teardown_method(self):
        sys.modules.pop("py_clob_client_v2", None)
        for key in MOCK_ENV:
            os.environ.pop(key, None)
        # Reset singleton
        import src.secrets as secrets_mod

        secrets_mod._provider = None

    def _make_adapter(self):
        from src.executor.v2_builder_adapter import V2BuilderAdapter

        return V2BuilderAdapter(host="https://clob-v2.polymarket.com")

    def test_sdk_version_is_v2(self):
        adapter = self._make_adapter()
        assert adapter.sdk_version == "v2"

    def test_is_healthy_initially_true(self):
        adapter = self._make_adapter()
        assert adapter.is_healthy is True

    def test_place_order_success(self):
        from src.executor.execution_gateway import ExecutionOrderRequest, OrderSide

        adapter = self._make_adapter()
        req = ExecutionOrderRequest(
            token_id="tok_abc123",
            price=0.45,
            size=10.0,
            side=OrderSide.BUY,
            signal_id="sig-001",
            trace_id="trc-001",
        )
        result = adapter.place_order(req)
        assert result.success is True
        assert result.order_id == "mock-order-001"
        assert result.status == "resting"

    def test_place_order_injects_builder_code(self):
        from src.executor.execution_gateway import ExecutionOrderRequest, OrderSide
        from src.executor.v2_builder_adapter import _ZERO_BYTES32

        adapter = self._make_adapter()

        captured_args = {}
        original_create = sys.modules[
            "py_clob_client_v2"
        ].ClobClient.create_and_post_order

        def capturing_create_and_post(self_, order_args, options, order_type):
            captured_args["builder"] = order_args.builder
            captured_args["metadata"] = order_args.metadata
            return {"orderID": "captured-001", "status": "resting"}

        sys.modules["py_clob_client_v2"].ClobClient.create_and_post_order = (
            capturing_create_and_post
        )

        req = ExecutionOrderRequest(
            token_id="tok_xyz",
            price=0.5,
            size=5.0,
            side=OrderSide.SELL,
        )
        adapter.place_order(req)

        assert captured_args.get("builder") == "0x" + "c" * 64  # POLY_BUILDER_CODE
        assert captured_args.get("builder") != _ZERO_BYTES32

    def test_place_order_no_builder_code_uses_zero(self):
        """When POLY_BUILDER_CODE is missing, orders get zero bytes32."""
        os.environ.pop("POLY_BUILDER_CODE", None)
        import src.secrets as secrets_mod

        secrets_mod._provider = None  # Reset singleton

        from src.executor.execution_gateway import ExecutionOrderRequest, OrderSide
        from src.executor.v2_builder_adapter import _ZERO_BYTES32, V2BuilderAdapter

        adapter = V2BuilderAdapter(host="https://clob-v2.polymarket.com")
        captured = {}

        def capture(self_, order_args, options, order_type):
            captured["builder"] = order_args.builder
            return {"orderID": "zero-001", "status": "resting"}

        sys.modules["py_clob_client_v2"].ClobClient.create_and_post_order = capture

        req = ExecutionOrderRequest(
            token_id="tok_zero",
            price=0.5,
            size=5.0,
            side=OrderSide.BUY,
        )
        result = adapter.place_order(req)
        assert captured["builder"] == _ZERO_BYTES32

    def test_cancel_order(self):
        adapter = self._make_adapter()
        assert adapter.cancel_order("some-order-id") is True

    def test_cancel_all_orders(self):
        adapter = self._make_adapter()
        assert adapter.cancel_all_orders() is True

    def test_get_balance_returns_pUSD(self):
        adapter = self._make_adapter()
        balance = adapter.get_balance()
        assert balance.currency == "pUSD"
        assert balance.collateral_balance == 100.0

    def test_get_open_orders(self):
        adapter = self._make_adapter()
        orders = adapter.get_open_orders()
        assert isinstance(orders, list)

    def test_get_midpoint(self):
        adapter = self._make_adapter()
        mid = adapter.get_midpoint("tok_test")
        assert mid == 0.5

    def test_missing_private_key_raises(self):
        os.environ.pop("POLY_PRIVATE_KEY")
        import src.secrets as secrets_mod

        secrets_mod._provider = None

        from src.executor.v2_builder_adapter import V2BuilderAdapter

        adapter = V2BuilderAdapter(host="https://clob-v2.polymarket.com")
        with pytest.raises(EnvironmentError, match="POLY_PRIVATE_KEY"):
            adapter._get_client()

    def test_import_error_propagates(self):
        sys.modules.pop("py_clob_client_v2", None)
        from src.executor.v2_builder_adapter import V2BuilderAdapter

        adapter = V2BuilderAdapter(host="https://clob-v2.polymarket.com")
        with pytest.raises(ImportError, match="py-clob-client-v2"):
            adapter._get_client()

    def test_place_order_failure_sets_unhealthy(self):
        from src.executor.execution_gateway import ExecutionOrderRequest, OrderSide

        def raise_error(self_, order_args, options, order_type):
            raise RuntimeError("network error")

        sys.modules["py_clob_client_v2"].ClobClient.create_and_post_order = raise_error

        adapter = self._make_adapter()
        req = ExecutionOrderRequest(
            token_id="tok_fail",
            price=0.5,
            size=5.0,
            side=OrderSide.BUY,
        )
        result = adapter.place_order(req)
        assert result.success is False
        assert "network error" in result.error
        assert adapter.is_healthy is False


# ===========================================================================
# V2 Production Guard
# ===========================================================================


class TestV2ProductionGuard:
    """Verify DRY_RUN=true is always rejected in V2."""

    def test_is_dry_run_always_false(self, monkeypatch):
        """config_loader.is_dry_run() must always return False in V2."""
        # Even if DRY_RUN=true is in env, V2 is_dry_run() returns False
        monkeypatch.setenv("DRY_RUN", "true")
        from src.utils.config_loader import is_dry_run

        assert is_dry_run() is False

    def test_is_dry_run_false_without_env(self, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        from src.utils.config_loader import is_dry_run

        assert is_dry_run() is False

    def test_is_dry_run_false_even_with_false_env(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "false")
        from src.utils.config_loader import is_dry_run

        assert is_dry_run() is False

    def test_assert_production_mode_panics_on_dry_run_true(self, monkeypatch):
        """assert_production_mode() must call sys.exit(2) when DRY_RUN=true."""
        monkeypatch.setenv("DRY_RUN", "true")
        from src.utils.v2_production_guard import assert_production_mode

        with pytest.raises(SystemExit) as exc_info:
            assert_production_mode()
        assert exc_info.value.code == 2

    def test_assert_production_mode_ok_without_dry_run(self, monkeypatch):
        """assert_production_mode() must succeed when DRY_RUN is not set."""
        monkeypatch.delenv("DRY_RUN", raising=False)
        from src.utils.v2_production_guard import assert_production_mode

        # Should not raise
        assert_production_mode()

    def test_migration_gate_closed_by_default(self, monkeypatch):
        monkeypatch.delenv("V2_MIGRATION_COMPLETE", raising=False)
        from src.utils.v2_production_guard import (
            MigrationGateClosedError,
            assert_migration_gate_open,
        )

        with pytest.raises(MigrationGateClosedError):
            assert_migration_gate_open("test context")

    def test_migration_gate_open_when_set(self, monkeypatch):
        monkeypatch.setenv("V2_MIGRATION_COMPLETE", "true")
        from src.utils.v2_production_guard import assert_migration_gate_open

        # Should not raise
        assert_migration_gate_open("test context")

    def test_is_migration_complete_false_by_default(self, monkeypatch):
        monkeypatch.delenv("V2_MIGRATION_COMPLETE", raising=False)
        from src.utils.v2_production_guard import is_migration_complete

        assert is_migration_complete() is False

    def test_is_migration_complete_true_when_set(self, monkeypatch):
        monkeypatch.setenv("V2_MIGRATION_COMPLETE", "true")
        from src.utils.v2_production_guard import is_migration_complete

        assert is_migration_complete() is True
