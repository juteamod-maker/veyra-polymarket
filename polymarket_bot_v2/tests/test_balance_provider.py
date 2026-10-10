"""Tests for balance_provider module."""

import sys
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset balance cache before each test."""
    from src.utils.balance_provider import _balance_cache

    _balance_cache["value"] = None
    _balance_cache["ts"] = 0
    yield
    _balance_cache["value"] = None
    _balance_cache["ts"] = 0


class TestCalcSimulatedBalance:
    @patch("src.utils.balance_provider._initial_balance", return_value=1000.0)
    @patch("src.utils.db.connect")
    def test_basic_calculation(self, mock_connect, _mock_init):
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        # realized_pnl = 50, deployed = 200
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(50.0,))),
            MagicMock(fetchone=MagicMock(return_value=(200.0,))),
        ]

        from src.utils.balance_provider import _calc_simulated_balance

        result = _calc_simulated_balance()
        assert result["source"] == "simulated"
        assert result["balance"] == 850.0  # 1000 + 50 - 200
        assert result["deployed"] == 200.0
        assert result["nlv"] == 1050.0  # 850 + 200
        assert result["realized_pnl"] == 50.0

    @patch("src.utils.balance_provider._initial_balance", return_value=100.0)
    @patch("src.utils.db.connect")
    def test_balance_floor_at_zero(self, mock_connect, _mock_init):
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        # Heavy losses: realized_pnl = -80, deployed = 50
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(-80.0,))),
            MagicMock(fetchone=MagicMock(return_value=(50.0,))),
        ]

        from src.utils.balance_provider import _calc_simulated_balance

        result = _calc_simulated_balance()
        assert result["balance"] == 0.0  # max(100 - 80 - 50, 0) = 0

    @patch("src.utils.balance_provider._initial_balance", return_value=100.0)
    @patch("src.utils.db.connect")
    def test_db_failure_fallback(self, mock_connect, _mock_init):
        mock_connect.side_effect = Exception("DB error")

        from src.utils.balance_provider import _calc_simulated_balance

        result = _calc_simulated_balance()
        assert result["source"] == "fallback"
        assert result["balance"] == 100.0


class TestFetchLiveBalance:
    @patch.dict(
        "os.environ",
        {
            "POLY_PRIVATE_KEY": "0xabc123",
            "POLY_API_KEY": "key",
            "POLY_SECRET": "secret",
            "POLY_PASSPHRASE": "pass",
            "POLY_RELAYER_ADDRESS": "0x1111111111111111111111111111111111111111",
        },
    )
    @patch("src.utils.balance_provider.get")
    def test_successful_fetch(self, mock_get_cfg):
        mock_get_cfg.side_effect = lambda k: {
            "polymarket.clob_api_url": "https://clob.polymarket.com",
            "polymarket.chain_id": 137,
        }.get(k)

        mock_client_instance = MagicMock()
        mock_client_instance.get_balance_allowance.return_value = {
            "balance": "14500000"
        }

        with patch.dict(
            "sys.modules",
            {
                "py_clob_client_v2": MagicMock(
                    ApiCreds=MagicMock(),
                    ClobClient=MagicMock(return_value=mock_client_instance),
                ),
            },
        ):
            from src.utils.balance_provider import _fetch_live_balance

            result = _fetch_live_balance()

        assert result is not None
        assert result["balance"] == 14.5
        assert result["source"] == "live"

    @patch("src.utils.balance_provider.get")
    @patch.dict("os.environ", {"POLY_PRIVATE_KEY": ""}, clear=False)
    def test_no_private_key(self, mock_get_cfg):
        mock_get_cfg.return_value = None

        from src.utils.balance_provider import _fetch_live_balance

        result = _fetch_live_balance()
        assert result is None


class TestGetPortfolio:
    @patch("src.utils.balance_provider._is_dry_run", return_value=True)
    @patch("src.utils.balance_provider._calc_simulated_balance")
    def test_dry_run_returns_simulated(self, mock_sim, _mock_dry):
        mock_sim.return_value = {
            "balance": 900.0,
            "deployed": 100.0,
            "nlv": 1000.0,
            "realized_pnl": 0.0,
            "source": "simulated",
        }

        from src.utils.balance_provider import get_portfolio

        result = get_portfolio()
        assert result["source"] == "simulated"
        assert result["balance"] == 900.0
        mock_sim.assert_called_once()

    @patch("src.utils.balance_provider._is_dry_run", return_value=True)
    @patch("src.utils.balance_provider._calc_simulated_balance")
    def test_cache_hit(self, mock_sim, _mock_dry):
        mock_sim.return_value = {
            "balance": 900.0,
            "deployed": 100.0,
            "nlv": 1000.0,
            "realized_pnl": 0.0,
            "source": "simulated",
        }

        from src.utils.balance_provider import get_portfolio

        r1 = get_portfolio()
        r2 = get_portfolio()
        assert r1 == r2
        # Should only calculate once (cached)
        mock_sim.assert_called_once()

    @patch("src.utils.balance_provider._is_dry_run", return_value=False)
    @patch("src.utils.balance_provider._fetch_live_balance")
    @patch("src.utils.balance_provider._calc_simulated_balance")
    def test_live_mode_combines_data(self, mock_sim, mock_live, _mock_dry):
        mock_live.return_value = {"balance": 14.5, "source": "live"}
        mock_sim.return_value = {
            "balance": 800.0,
            "deployed": 200.0,
            "nlv": 1000.0,
            "realized_pnl": 0.0,
            "source": "simulated",
        }

        from src.utils.balance_provider import get_portfolio

        result = get_portfolio()
        assert result["source"] == "live"
        assert result["balance"] == 14.5  # from live
        assert result["deployed"] == 200.0  # from simulated

    @patch("src.utils.balance_provider._is_dry_run", return_value=False)
    @patch("src.utils.balance_provider._fetch_live_balance", return_value=None)
    @patch("src.utils.balance_provider._calc_simulated_balance")
    def test_live_mode_fallback(self, mock_sim, _mock_live, _mock_dry):
        mock_sim.return_value = {
            "balance": 800.0,
            "deployed": 200.0,
            "nlv": 1000.0,
            "realized_pnl": 0.0,
            "source": "simulated",
        }

        from src.utils.balance_provider import get_portfolio

        result = get_portfolio()
        assert result["source"] == "simulated"


class TestGetAvailableBalance:
    @patch("src.utils.balance_provider.get_portfolio")
    def test_returns_balance_float(self, mock_portfolio):
        mock_portfolio.return_value = {"balance": 42.5}

        from src.utils.balance_provider import get_available_balance

        assert get_available_balance() == 42.5


class TestInvalidateCache:
    @patch("src.utils.balance_provider._is_dry_run", return_value=True)
    @patch("src.utils.balance_provider._calc_simulated_balance")
    def test_invalidate_forces_recalc(self, mock_sim, _mock_dry):
        mock_sim.return_value = {
            "balance": 900.0,
            "deployed": 100.0,
            "nlv": 1000.0,
            "realized_pnl": 0.0,
            "source": "simulated",
        }

        from src.utils.balance_provider import get_portfolio, invalidate_cache

        get_portfolio()
        assert mock_sim.call_count == 1

        invalidate_cache()
        get_portfolio()
        assert mock_sim.call_count == 2
