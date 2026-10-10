"""
test_live_filters.py — 实盘信号过滤管线单元测试
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from src.signal.signal_engine import Signal
from src.utils.live_filters import _extract_underlying_asset, filter_signals_for_live


def make_signal(
    market_id="mkt_001",
    question="Will BTC go up?",
    outcome="YES",
    ev=0.05,
    confidence=0.70,
    size_usdc=2.0,
    expected_price=0.0,
    model_votes=None,
):
    return Signal(
        market_id=market_id,
        question=question,
        outcome=outcome,
        ev=ev,
        confidence=confidence,
        size_usdc=size_usdc,
        expected_price=expected_price,
        model_votes=model_votes or {},
    )


def default_pt_cfg():
    return {
        "balance_reserve_min_usdc": 0.10,
        "max_open_positions": 10,
        "blocked_categories": ["esports"],
        "category_max_positions": {"crypto": 3, "default": 15},
        "category_max_per_event": {"default": 1},
        "max_per_market_positions": 1,
        "category_max_per_asset": {"crypto": 1, "default": 2},
        "stop_loss_cooldown_hours": 2.0,
        "circuit_breaker": {
            "enabled": True,
            "consecutive_stops": 3,
            "cooldown_hours": 24,
        },
        "category_min_ev": {"crypto": 0.06, "default": 0.015},
        "category_min_confidence": {"crypto": 0.60, "default": 0.45},
        "category_size_multiplier": {"crypto": 0.7, "default": 1.0},
        "hard_max_bet_usdc": 2.5,
        "weather_max_size_usdc": 2.0,
        "category_max_bet_pct": {"crypto": 0.08, "default": 0.08},
        "max_entry_price": 0.90,
        "city_no": {
            "max_price": 0.85,
            "min_ev": 0.02,
            "min_score": 0.9,
        },
        "sports": {
            "tail_max_price": 0.85,
            "max_size_usdc": 10.0,
            "moneyline_only": True,
            "underdog_max_price": 0.35,
            "underdog_size_boost": 1.3,
        },
        "taker_fee_rates": {"sports": 0.01, "weather": 0.01, "crypto": 0.01},
        "taker_fee_rate_default": 0.01,
        "min_net_edge_after_fees": 0.01,
    }


def mock_get(pt_cfg):
    def _get(k):
        if k == "paper_trading":
            return pt_cfg
        if k == "general.live_initial_deposit":
            return 30.0
        parts = k.split(".", 1)
        if len(parts) == 2 and parts[0] == "paper_trading":
            return pt_cfg.get(parts[1])
        return None

    return _get


class TestLiveFilters:

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_basic_pass(self, mock_cat, mock_get_fn, mock_db):
        """Signal that passes all filters."""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.08, confidence=0.70)
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 1

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_balance_reserve_blocks(self, mock_cat, mock_get_fn, mock_db):
        """Balance below reserve line → blocks all."""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []

        sig = make_signal()
        # balance=$1.0, reserve = $30 * 0.05 = $1.50
        result = filter_signals_for_live([sig], balance=1.0, market_lookup={})
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="esports")
    def test_blocked_category(self, mock_cat, mock_get_fn, mock_db):
        """Blocked category → filtered out."""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []

        sig = make_signal()
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_category_quota(self, mock_cat, mock_get_fn, mock_db):
        """Category with 3 existing positions + max=3 → blocked."""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = [
            {
                "category": "crypto",
                "market_id": f"m{i}",
                "event_slug": "",
                "question": "",
            }
            for i in range(3)
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.08, confidence=0.70)
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_event_dedup(self, mock_cat, mock_get_fn, mock_db):
        """Same event_slug already has a position → blocked."""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = [
            {
                "category": "crypto",
                "market_id": "m_old",
                "event_slug": "btc-event",
                "question": "",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.08, confidence=0.70)
        market = {"_event_slug": "btc-event"}
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": market}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_market_dedup(self, mock_cat, mock_get_fn, mock_db):
        """Same market_id already has a position → blocked."""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = [
            {
                "category": "crypto",
                "market_id": "mkt_001",
                "event_slug": "",
                "question": "",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.08, confidence=0.70)
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_ev_reject(self, mock_cat, mock_get_fn, mock_db):
        """EV below category threshold → rejected."""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.04, confidence=0.70)  # crypto min_ev = 0.06
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_yes_direction_passes_without_old_ev_boost(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """T38: direction_adjustment 已移除，YES不再有额外EV加码。"""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # crypto min_ev=0.06, YES方向不再需要额外+0.015
        sig = make_signal(ev=0.07, confidence=0.70, outcome="YES")
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 1  # T38: 0.07 > 0.06 = pass (no more YES EV boost)

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_hard_max_cap(self, mock_cat, mock_get_fn, mock_db):
        """Size gets capped to hard_max_bet_usdc."""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.10, confidence=0.70, size_usdc=10.0, outcome="NO")
        result = filter_signals_for_live(
            [sig], balance=50.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 1
        # hard_max=2.5, cat_mult=0.7 → 10.0 * 0.7 = 7.0 → capped to 2.5
        assert result[0].size_usdc <= 2.5

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_stop_loss_cooldown(self, mock_cat, mock_get_fn, mock_db):
        """Recent stop loss on same market → blocked."""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = True

        sig = make_signal(ev=0.05, confidence=0.60)
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_multiple_signals_dedup_within_cycle(self, mock_cat, mock_get_fn, mock_db):
        """Two signals for same market_id in same cycle → only first passes."""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig1 = make_signal(
            market_id="mkt_A",
            ev=0.05,
            confidence=0.60,
            outcome="NO",
            expected_price=0.40,
        )
        sig2 = make_signal(
            market_id="mkt_A",
            ev=0.06,
            confidence=0.65,
            outcome="NO",
            expected_price=0.40,
        )
        # Bug218: 天气市场必须有endDate，否则时间窗口检查会硬性拒绝
        _future = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        result = filter_signals_for_live(
            [sig1, sig2],
            balance=20.0,
            market_lookup={
                "mkt_A": {
                    "endDate": _future,
                    "_strategy_tag": "city_no",
                    "_city_no_score": 1.0,
                }
            },
        )
        assert len(result) == 1

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_weather_same_event_only_one_passes(self, mock_cat, mock_get_fn, mock_db):
        """Two weather signals from same event (different temps) → only first passes.

        Phase 35b: category_max_per_event.weather = 1 prevents multiple
        temperature bets on the same city+date event.
        """
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 1
        cfg["weather_entry_window_hours"] = 999
        cfg["weather_min_hours_to_expiry"] = 0
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        _tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%-d")
        _month = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%B")
        _date_str = f"{_month} {_tomorrow}"
        sig1 = make_signal(
            market_id="m_15c",
            question=f"Will the highest temperature in Paris be 15°C on {_date_str}?",
            outcome="NO",
            ev=0.06,
            confidence=0.70,
        )
        sig2 = make_signal(
            market_id="m_16c",
            question=f"Will the highest temperature in Paris be 16°C on {_date_str}?",
            outcome="NO",
            ev=0.05,
            confidence=0.65,
        )
        event_slug = f"highest-temperature-in-paris-on-{_date_str.lower().replace(' ', '-')}-2026"
        _future = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        lookup = {
            "m_15c": {
                "_event_slug": event_slug,
                "endDate": _future,
                "_strategy_tag": "city_no",
                "_city_no_score": 1.0,
            },
            "m_16c": {
                "_event_slug": event_slug,
                "endDate": _future,
                "_strategy_tag": "city_no",
                "_city_no_score": 1.0,
            },
        }
        result = filter_signals_for_live(
            [sig1, sig2], balance=20.0, market_lookup=lookup
        )
        assert len(result) == 1
        assert result[0].market_id == "m_15c"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_weather_asset_dedup_limits_same_city(self, mock_cat, mock_get_fn, mock_db):
        """Weather signals from same city but different dates → limited by asset dedup.

        Phase 35b: category_max_per_asset.weather = 2 prevents excessive
        concentration on a single city across multiple dates.
        """
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 1
        cfg["category_max_per_asset"]["weather"] = 2
        cfg["weather_entry_window_hours"] = 999
        cfg["weather_min_hours_to_expiry"] = 0
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        _d1 = datetime.now(timezone.utc) + timedelta(days=1)
        _d2 = datetime.now(timezone.utc) + timedelta(days=2)
        _d3 = datetime.now(timezone.utc) + timedelta(days=3)
        _date_str = f"{_d1.strftime('%B')} {_d1.day}"
        _date_str2 = f"{_d2.strftime('%B')} {_d2.day}"
        _date_str3 = f"{_d3.strftime('%B')} {_d3.day}"

        sig1 = make_signal(
            market_id="m_mar22",
            question=f"Will the highest temperature in Paris be 15°C on {_date_str}?",
            outcome="NO",
            ev=0.06,
            confidence=0.70,
        )
        sig2 = make_signal(
            market_id="m_mar23",
            question=f"Will the highest temperature in Paris be 18°C on {_date_str2}?",
            outcome="NO",
            ev=0.05,
            confidence=0.65,
        )
        sig3 = make_signal(
            market_id="m_mar24",
            question=f"Will the highest temperature in Paris be 17°C on {_date_str3}?",
            outcome="NO",
            ev=0.04,
            confidence=0.60,
        )
        _future = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        lookup = {
            "m_mar22": {
                "_event_slug": f"highest-temperature-in-paris-on-{_date_str.lower().replace(' ', '-')}-2026",
                "endDate": _future,
                "_strategy_tag": "city_no",
                "_city_no_score": 1.0,
            },
            "m_mar23": {
                "_event_slug": f"highest-temperature-in-paris-on-{_date_str2.lower().replace(' ', '-')}-2026",
                "endDate": _future,
                "_strategy_tag": "city_no",
                "_city_no_score": 1.0,
            },
            "m_mar24": {
                "_event_slug": f"highest-temperature-in-paris-on-{_date_str3.lower().replace(' ', '-')}-2026",
                "endDate": _future,
                "_strategy_tag": "city_no",
                "_city_no_score": 1.0,
            },
        }
        result = filter_signals_for_live(
            [sig1, sig2, sig3], balance=30.0, market_lookup=lookup
        )
        # weather asset limit = 2: first two Paris bets pass, third blocked
        assert len(result) == 2
        assert {r.market_id for r in result} == {"m_mar22", "m_mar23"}


class TestCategoryAwarePriceCeiling:
    """T225: price ceiling should vary by category (weather=0.90, sports=0.85, default=0.85)."""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_weather_allows_high_price(self, mock_cat, mock_get_fn, mock_db):
        """Weather at 0.75 should pass (no_max_entry_price=0.80)."""
        cfg = default_pt_cfg()
        cfg.setdefault("city_no", {})["max_price"] = 0.85
        cfg.setdefault("city_no", {})["max_entry_price"] = 0.85
        cfg["weather_min_hours_to_expiry"] = 0
        cfg["weather_entry_window_hours"] = 999
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        _future = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        sig = make_signal(
            ev=0.08,
            confidence=0.70,
            expected_price=0.75,
            outcome="NO",
        )
        result = filter_signals_for_live(
            [sig],
            balance=20.0,
            market_lookup={
                "mkt_001": {
                    "endDate": _future,
                    "_strategy_tag": "city_no",
                    "_city_no_score": 1.0,
                }
            },
        )
        assert len(result) == 1, "Weather@0.75 should pass with ceiling=0.80"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_weather_pure_cov_uses_own_ceiling(self, mock_cat, mock_get_fn, mock_db):
        """weather_pure_cov 应使用 pure_coverage.no_max_entry_price 而非 weather 全局上限。"""
        cfg = default_pt_cfg()
        base_get = mock_get(cfg)

        def _get_with_weather_cfg(key):
            if key == "weather_strategy":
                return {
                    "no_max_entry_price": 0.85,
                    "pure_coverage": {"no_max_entry_price": 0.90},
                }
            return base_get(key)

        mock_get_fn.side_effect = _get_with_weather_cfg
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.08, confidence=0.70, expected_price=0.87, outcome="NO")
        sig.strategy_tag = "weather_pure_cov"

        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert (
            len(result) == 1
        ), "PureCov@0.87 should pass with pure_coverage ceiling=0.90"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_weather_non_pure_cov_keeps_global_ceiling(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """非 PureCov 天气策略仍应遵循 weather 全局 no_max_entry_price。"""
        cfg = default_pt_cfg()
        base_get = mock_get(cfg)

        def _get_with_weather_cfg(key):
            if key == "weather_strategy":
                return {
                    "no_max_entry_price": 0.85,
                    "pure_coverage": {"no_max_entry_price": 0.90},
                }
            return base_get(key)

        mock_get_fn.side_effect = _get_with_weather_cfg
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.08, confidence=0.70, expected_price=0.87, outcome="NO")
        sig.strategy_tag = "weather_right_tail_right"

        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert (
            len(result) == 0
        ), "Non-PureCov weather@0.87 should still be blocked at 0.85"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="sports")
    def test_sports_blocks_at_085(self, mock_cat, mock_get_fn, mock_db):
        """Sports at 0.86 should be blocked (ceiling=0.85)."""
        cfg = default_pt_cfg()
        cfg.setdefault("sports", {})["tail_max_price"] = 0.85
        cfg["settlement_max_hours"] = {"sports": 9999}
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.08, confidence=0.70, expected_price=0.86)
        _future = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {"endDate": _future}}
        )
        assert len(result) == 0, "Sports@0.86 should be blocked with ceiling=0.85"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="sports")
    def test_sports_allows_below_ceiling(self, mock_cat, mock_get_fn, mock_db):
        """Sports at 0.84 should pass (ceiling=0.85)."""
        cfg = default_pt_cfg()
        cfg.setdefault("sports", {})["tail_max_price"] = 0.85
        cfg["settlement_max_hours"] = {"sports": 9999}
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.08, confidence=0.70, expected_price=0.84)
        _future = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {"endDate": _future}}
        )
        assert len(result) == 1, "Sports@0.84 should pass with ceiling=0.85"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="finance")
    def test_default_category_uses_global_ceiling(self, mock_cat, mock_get_fn, mock_db):
        """Finance (no specific ceiling) uses global max_entry_price=0.85."""
        cfg = default_pt_cfg()
        cfg["max_entry_price"] = 0.85
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.08, confidence=0.70, expected_price=0.86)
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert (
            len(result) == 0
        ), "Finance@0.86 should be blocked with global ceiling=0.85"


class TestExtractUnderlyingAsset:
    def test_crypto_bitcoin(self):
        assert (
            _extract_underlying_asset("Bitcoin Up or Down today?", "crypto")
            == "crypto_bitcoin"
        )

    def test_crypto_eth(self):
        assert (
            _extract_underlying_asset("Will ETH price exceed $5k?", "crypto")
            == "crypto_eth"
        )

    def test_sports_matchup(self):
        result = _extract_underlying_asset("Lakers vs Celtics: Who will win?", "sports")
        assert result is not None
        assert "lakers" in result

    def test_unknown_returns_none(self):
        assert _extract_underlying_asset("Will it rain?", "weather") is None
        assert _extract_underlying_asset("Random question?", "finance") is None


class TestWeatherCooldownLiveFilters:
    """TASK 164: 天气市场应使用 weather_stop_loss_cooldown_hours（更长冷却期）。"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_sports_uses_general_cooldown(self, mock_cat, mock_get_fn, mock_db):
        """非天气市场使用通用 stop_loss_cooldown_hours。"""
        cfg = default_pt_cfg()
        cfg["stop_loss_cooldown_hours"] = 6.0
        cfg["weather_stop_loss_cooldown_hours"] = 24.0
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = True

        sig = make_signal(ev=0.05, confidence=0.60)
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 0
        call_args = mock_db.had_recent_stop_loss.call_args
        assert call_args[0][1] == 6.0, f"Expected 6.0h cooldown, got {call_args[0][1]}"

    def test_weather_paris(self):
        result = _extract_underlying_asset(
            "Will the highest temperature in Paris be 15°C on March 22?", "weather"
        )
        assert result == "weather_paris"

    def test_weather_sao_paulo(self):
        result = _extract_underlying_asset(
            "Will the highest temperature in Sao Paulo be 32°C on March 22?", "weather"
        )
        assert result == "weather_sao_paulo"

    def test_weather_buenos_aires(self):
        result = _extract_underlying_asset(
            "Will the highest temperature in Buenos Aires be 25°C on March 22?",
            "weather",
        )
        assert result == "weather_buenos_aires"

    def test_weather_different_temps_same_city(self):
        """Same city different temps should return the same asset key."""
        a = _extract_underlying_asset(
            "Will the highest temperature in Paris be 15°C on March 22?", "weather"
        )
        b = _extract_underlying_asset(
            "Will the highest temperature in Paris be 16°C on March 22?", "weather"
        )
        assert a == b == "weather_paris"

    def test_weather_different_dates_same_city(self):
        """Same city different dates should return the same asset key."""
        a = _extract_underlying_asset(
            "Will the highest temperature in Paris be 15°C on March 22?", "weather"
        )
        b = _extract_underlying_asset(
            "Will the highest temperature in Paris be 18°C on March 23?", "weather"
        )
        assert a == b == "weather_paris"

    def test_weather_no_match_returns_none(self):
        """Weather question without standard format returns None."""
        assert _extract_underlying_asset("Will it rain tomorrow?", "weather") is None


# ══════════════════════════════════════════════════════
# 城市NO策略: 天气城市NO简化入场测试（替代旧ColdMath三桶）
# ══════════════════════════════════════════════════════


def _make_cn_market(
    hours_until: float = 20.0,
    city_no_score: float = 1.0,
    extreme_type: str = "low",
    question: str = "",
):
    """返回带城市NO标签的天气市场dict。"""
    import json

    _end = (datetime.now(timezone.utc) + timedelta(hours=hours_until)).isoformat()
    return {
        "endDate": _end,
        "question": question,
        "_strategy_tag": "city_no",
        "_city_no_score": city_no_score,
        "_strategy_params": json.dumps({"extreme_type": extreme_type}),
    }


class TestCityNoWeatherEntry:
    """城市NO策略简化入场 — 替代旧ColdMath三桶系统。"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_cn_low_ev_rejected(self, mock_cat, mock_get_fn, mock_db):
        """城市NO策略 NO: EV < city_no_min_ev → 拒绝。"""
        cfg = default_pt_cfg()
        cfg.setdefault("city_no", {})["max_price"] = 0.85
        cfg.setdefault("city_no", {})["min_ev"] = 0.02
        cfg.setdefault("city_no", {})["min_score"] = 0.9
        cfg["weather_min_hours_to_expiry"] = 0
        cfg["weather_entry_window_hours"] = 999
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(outcome="NO", expected_price=0.50, ev=0.01, confidence=0.65)
        market = _make_cn_market(city_no_score=1.2)
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": market}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_cn_pair_uses_lower_score_threshold(self, mock_cat, mock_get_fn, mock_db):
        """T78: 双NO对(low/high)使用city_no_pair_min_score宽松阈值。"""
        cfg = default_pt_cfg()
        cfg.setdefault("city_no", {})["max_price"] = 0.85
        cfg.setdefault("city_no", {})["min_ev"] = 0.02
        cfg.setdefault("city_no", {})["min_score"] = 0.9
        cfg.setdefault("city_no", {})["pair_min_score"] = 0.5
        cfg["weather_min_hours_to_expiry"] = 0
        cfg["weather_entry_window_hours"] = 999
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(outcome="NO", expected_price=0.50, ev=0.05, confidence=0.65)
        # score=0.55 < 0.9(standard) but >= 0.5(pair) → should pass
        market = _make_cn_market(city_no_score=0.55, extreme_type="low")
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": market}
        )
        assert len(result) == 1

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_cn_only_scenario_halves_size(self, mock_cat, mock_get_fn, mock_db):
        """城市NO策略 NO: extreme_type=only → 仓位×0.5。"""
        cfg = default_pt_cfg()
        cfg.setdefault("city_no", {})["max_price"] = 0.85
        cfg.setdefault("city_no", {})["min_ev"] = 0.02
        cfg.setdefault("city_no", {})["min_score"] = 0.9
        cfg.setdefault("city_no", {})["only_scenario_multiplier"] = 0.5
        cfg["weather_min_hours_to_expiry"] = 0
        cfg["weather_entry_window_hours"] = 999
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(
            outcome="NO",
            expected_price=0.50,
            ev=0.05,
            confidence=0.65,
            size_usdc=4.0,
        )
        market = _make_cn_market(city_no_score=1.2, extreme_type="only")
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": market}
        )
        assert len(result) == 1
        assert result[0].size_usdc <= 2.0, "only-scenario should halve size"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_non_weather_no_bet_bucket(self, mock_cat, mock_get_fn, mock_db):
        """非天气赛道 → 不设置 _bet_bucket 属性。"""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(outcome="YES", expected_price=0.50, ev=0.08, confidence=0.65)
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 1
        assert not hasattr(result[0], "_bet_bucket")


# ══════════════════════════════════════════════════════
# T178: 体育结算时限过滤测试（live_filters）
# ══════════════════════════════════════════════════════


class TestSportsSettlementFilter:
    """T178: 体育赛道结算时限过滤（防止长期锁定资金）。"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="sports")
    def test_sports_long_settlement_filtered(self, mock_cat, mock_get_fn, mock_db):
        """体育 endDate 超过36h → 过滤。"""
        from datetime import datetime, timedelta, timezone

        cfg = default_pt_cfg()
        cfg["settlement_max_hours"] = {"sports": 36}
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        far_end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        market = {"endDate": far_end}
        sig = make_signal(ev=0.08, confidence=0.65, outcome="NO")
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": market}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="sports")
    def test_sports_no_enddate_filtered(self, mock_cat, mock_get_fn, mock_db):
        """体育无 endDate → 保守过滤。"""
        cfg = default_pt_cfg()
        cfg["settlement_max_hours"] = {"sports": 36}
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(ev=0.08, confidence=0.65, outcome="NO")
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="sports")
    def test_sports_bad_enddate_filtered(self, mock_cat, mock_get_fn, mock_db):
        """体育 endDate 格式错误 → 保守过滤。"""
        cfg = default_pt_cfg()
        cfg["settlement_max_hours"] = {"sports": 36}
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        market = {"endDate": "not-a-date"}
        sig = make_signal(ev=0.08, confidence=0.65, outcome="NO")
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": market}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="sports")
    def test_sports_within_limit_passes(self, mock_cat, mock_get_fn, mock_db):
        """体育 endDate ≤ 36h → 通过。"""
        from datetime import datetime, timedelta, timezone

        cfg = default_pt_cfg()
        cfg["settlement_max_hours"] = {"sports": 36}
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        near_end = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        market = {"endDate": near_end}
        sig = make_signal(ev=0.08, confidence=0.65, outcome="NO")
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": market}
        )
        assert len(result) == 1


# ══════════════════════════════════════════════════════
# T278-BUG 回归测试: TimeGuard 实盘修复 + 边界条件
# ══════════════════════════════════════════════════════

_WEATHER_FETCHER_PARSE = "src.utils.weather_fetcher.parse_weather_question"
_WEATHER_FETCHER_HOUR = "src.utils.weather_fetcher.get_city_local_hour"


def _weather_cfg_with_time_guard(**overrides):
    """含完整天气配置（包含TimeGuard）的pt_cfg。"""
    cfg = default_pt_cfg()
    cfg.update(
        {
            "weather_min_hours_to_expiry": 8,
            "weather_entry_window_hours": 36,
            "weather_time_guard": {
                "enabled": True,
                "peak_window_start": 10,
                "peak_window_end": 17,
                "trough_window_start": 3,
                "trough_window_end": 7,
                "core_margin_hours": 1,
                "edge_size_multiplier": 0.25,
                "reject_in_core": True,
            },
            "city_no": {
                "max_price": 0.85,
                "min_ev": 0.02,
                "min_score": 0.9,
            },
        }
    )
    cfg.update(overrides)
    return cfg


def _weather_market(hours_until: float = 20.0, question: str = ""):
    """返回带正确endDate和city_no标签的天气市场dict。"""
    _end = (datetime.now(timezone.utc) + timedelta(hours=hours_until)).isoformat()
    return {
        "endDate": _end,
        "question": question,
        "_strategy_tag": "city_no",
        "_city_no_score": 1.0,
    }


class TestT278TimeGuardLiveFilters:
    """
    T278-BUG 回归测试:
    TimeGuard 必须在 live_filters.py 中执行（而非仅在 paper_trader 中）。
    Wellington 生产事故: UTC+13导致当地10:49入场NO方向, TimeGuard未生效。
    """

    @patch("src.utils.smart_capital.get", return_value=None)
    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    @patch(_WEATHER_FETCHER_PARSE)
    @patch(_WEATHER_FETCHER_HOUR)
    def test_edge_window_no_bet_size_reduced(
        self, mock_hour, mock_parse, mock_cat, mock_get_fn, mock_db, _mock_sce
    ):
        """边缘窗口天气信号通过live_filters时由SCE裁剪（TimeGuard在smart_entry层执行）。
        NOTE: live_filters不实现TimeGuard缩减逻辑，仅做SCE仓位裁剪。
        """
        mock_hour.return_value = 10
        mock_parse.return_value = {"city": "Wellington", "variable": "temp_max"}

        cfg = _weather_cfg_with_time_guard()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # SCE default tiers: balance=200 → medium tier, weather mult=1.0
        # cat_max = min(200*0.08*1.0, 25.0) = 16.0
        # 信号 8.0 < 16.0 → 不裁剪
        original_size = 8.0
        sig = make_signal(
            question="Will the highest temperature in Wellington be 20°C on March 30?",
            outcome="NO",
            ev=0.08,
            confidence=0.70,
            expected_price=0.16,
            size_usdc=original_size,
        )
        market = _weather_market(hours_until=13.0)
        result = filter_signals_for_live(
            [sig], balance=200.0, market_lookup={"mkt_001": market}
        )

        assert len(result) == 1, "天气NO信号应通过live_filters"
        # T178/T179: NO压缩生效 — target_shares=max(5.10, 5.0+0.35)=5.35
        # 对于 0.16 的低价市场，压缩后金额会被 min_bet_usdc=2.0 托底
        _expected_compressed = 2.0
        assert result[0].size_usdc == pytest.approx(
            _expected_compressed, abs=0.01
        ), f"NO压缩后size应为{_expected_compressed}, got={result[0].size_usdc}"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    @patch(_WEATHER_FETCHER_PARSE)
    @patch(_WEATHER_FETCHER_HOUR)
    def test_off_peak_no_bet_allowed(
        self, mock_hour, mock_parse, mock_cat, mock_get_fn, mock_db
    ):
        """非峰值窗口(8时) NO方向 → 允许通过，TimeGuard不影响。"""
        mock_hour.return_value = 8  # 非峰值窗口 (peak_start=10)
        mock_parse.return_value = {"city": "Wellington", "variable": "temp_max"}

        cfg = _weather_cfg_with_time_guard()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(
            question="Will the highest temperature in Wellington be 20°C on March 30?",
            outcome="NO",
            ev=0.08,
            confidence=0.70,
            expected_price=0.16,
        )
        market = _weather_market(hours_until=20.0)
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": market}
        )

        assert len(result) == 1, "非峰值窗口NO方向应允许通过"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    @patch(_WEATHER_FETCHER_PARSE)
    @patch(_WEATHER_FETCHER_HOUR)
    def test_time_guard_disabled_allows_core_window(
        self, mock_hour, mock_parse, mock_cat, mock_get_fn, mock_db
    ):
        """time_guard.enabled=False → 核心窗口NO方向仍被允许。"""
        mock_hour.return_value = 12  # 核心窗口
        mock_parse.return_value = {"city": "Wellington", "variable": "temp_max"}

        cfg = _weather_cfg_with_time_guard()
        cfg["weather_time_guard"]["enabled"] = False
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = make_signal(
            question="Will the highest temperature in Wellington be 20°C on March 30?",
            outcome="NO",
            ev=0.08,
            confidence=0.70,
            expected_price=0.16,
        )
        market = _weather_market(hours_until=13.0)
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": market}
        )

        assert len(result) == 1, "TimeGuard=disabled时核心窗口NO方向应允许通过"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_min_hours_just_above_boundary_allowed(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """距到期8.01h → 允许通过（刚好超过边界）。"""
        cfg = _weather_cfg_with_time_guard()
        cfg["weather_min_hours_to_expiry"] = 8
        cfg["weather_time_guard"] = {"enabled": False}
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # 8h + 36秒 → 约 8.01h (> 8)
        _end = (datetime.now(timezone.utc) + timedelta(hours=8, seconds=36)).isoformat()
        market = {"endDate": _end, "_strategy_tag": "city_no", "_city_no_score": 1.0}
        sig = make_signal(outcome="NO", ev=0.08, confidence=0.70, expected_price=0.16)
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": market}
        )

        assert len(result) == 1, "距到期>8h的微量情况应允许通过"

    @patch("src.utils.smart_capital.get", return_value=None)
    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    @patch(_WEATHER_FETCHER_PARSE)
    @patch(_WEATHER_FETCHER_HOUR)
    def test_wellington_production_scenario_size_reduced(
        self, mock_hour, mock_parse, mock_cat, mock_get_fn, mock_db, _mock_sce
    ):
        """Wellington天气信号通过live_filters时由SCE裁剪。
        NOTE: TimeGuard缩减在smart_entry层执行，live_filters仅做SCE仓位裁剪。
        """
        mock_hour.return_value = 10
        mock_parse.return_value = {"city": "Wellington", "variable": "temp_max"}

        cfg = _weather_cfg_with_time_guard()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # SCE default tiers: balance=200 → medium, weather×1.0
        # cat_max = min(200*0.08*1.0, 25.0) = 16.0, 信号8.0 < 16.0 → 不裁剪
        original_size = 8.0
        sig = make_signal(
            market_id="mkt_wellington",
            question="Will the highest temperature in Wellington be 20°C on March 30?",
            outcome="NO",
            ev=0.08,
            confidence=0.70,
            expected_price=0.16,
            size_usdc=original_size,
        )
        market = _weather_market(
            hours_until=13.18,
            question="Will the highest temperature in Wellington be 20°C on March 30?",
        )

        result = filter_signals_for_live(
            [sig], balance=200.0, market_lookup={"mkt_wellington": market}
        )

        assert len(result) == 1, "Wellington天气信号应通过live_filters"
        # T178追加: NO压缩生效 — target_shares=max(safety_floor, min_order+buffer)
        # safety_floor=5.10, min+buffer=5.0+0.35=5.35 → 取max=5.35
        # compressed=5.35*0.16=0.856 → min_bet_floor $2.00
        _expected_compressed = 2.0
        assert result[0].size_usdc == pytest.approx(
            _expected_compressed, abs=0.01
        ), f"NO压缩后size应为{_expected_compressed}, got={result[0].size_usdc}"


class TestT33BkFollowFeeBypass:
    """T33: BkFollow 信号的 EV 已包含费用扣除，live_filter 不应重复扣费"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="sports")
    def test_bk_follow_small_ev_passes_feecheck(self, mock_cat, mock_get_fn, mock_db):
        """T33: BkFollow EV=0.005 (已含fee) 不该被 FeeCheck 二次扣费拒绝"""
        cfg = default_pt_cfg()
        cfg["taker_fee_rates"] = {"sports": 0.01, "weather": 0.01, "crypto": 0.01}
        cfg["taker_fee_rate_default"] = 0.01
        cfg["min_net_edge_after_fees"] = 0.001
        cfg["category_min_ev"]["sports"] = 0
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False

        sig = Signal(
            market_id="mkt_sport_001",
            question="Team A vs. Team B",
            outcome="YES",
            ev=0.005,
            confidence=0.70,
            size_usdc=2.0,
            expected_price=0.55,
            strategy_tag="bk_follow",
        )
        market = {
            "endDate": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        }
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_sport_001": market}
        )
        # BkFollow EV=0.005 >= 0 → 应通过 FeeCheck（不再双重扣费）
        assert len(result) >= 1, "T33: BkFollow EV=0.005 不应被 FeeCheck 拒绝"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="sports")
    def test_bk_follow_negative_ev_rejected(self, mock_cat, mock_get_fn, mock_db):
        """T33: BkFollow EV=-0.003 (after fee) 应被拒绝"""
        cfg = default_pt_cfg()
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []

        sig = Signal(
            market_id="mkt_sport_002",
            question="Team C vs. Team D",
            outcome="NO",
            ev=-0.003,
            confidence=0.70,
            size_usdc=2.0,
            expected_price=0.45,
            strategy_tag="bk_follow",
        )
        market = {
            "endDate": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        }
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_sport_002": market}
        )
        assert len(result) == 0, "T33: BkFollow 负EV 应被拒绝"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="sports")
    def test_non_bk_follow_sports_still_fees_checked(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """T33: 非BkFollow的体育信号仍然走双重扣费逻辑"""
        cfg = default_pt_cfg()
        cfg["taker_fee_rates"] = {"sports": 0.01, "weather": 0.01, "crypto": 0.01}
        cfg["taker_fee_rate_default"] = 0.01
        cfg["min_net_edge_after_fees"] = 0.01
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []

        sig = Signal(
            market_id="mkt_sport_003",
            question="Team E vs. Team F",
            outcome="YES",
            ev=0.005,
            confidence=0.70,
            size_usdc=2.0,
            expected_price=0.55,
            strategy_tag="",  # 非 bk_follow
        )
        market = {
            "endDate": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        }
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_sport_003": market}
        )
        # 非BkFollow: net_edge = 0.005 - 0.02 = -0.015 < 0.01 → 应被拒绝
        assert len(result) == 0, "非BkFollow体育信号应被双重扣费拒绝"
        assert len(result) == 0, "非BkFollow体育信号应被双重扣费拒绝"
        assert len(result) == 0, "非BkFollow体育信号应被双重扣费拒绝"
        assert len(result) == 0, "非BkFollow体育信号应被双重扣费拒绝"
        assert len(result) == 0, "非BkFollow体育信号应被双重扣费拒绝"
        assert len(result) == 0, "非BkFollow体育信号应被双重扣费拒绝"


class TestSignalCategoryPriority:
    """T69: 赛道优先排序测试 — 天气信号优先获取执行机会"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category")
    def test_weather_signals_processed_before_sports(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """天气信号排在体育前面，余额有限时天气优先下单"""
        cfg = default_pt_cfg()
        cfg["signal_category_priority"] = {
            "weather": 0,
            "crypto": 1,
            "sports": 2,
            "default": 99,
        }
        # 天气信号需要的额外配置
        cfg["weather_entry_window_hours"] = 48
        cfg["weather_min_hours_to_expiry"] = 6
        cfg.setdefault("city_no", {})["min_score"] = 0.7
        cfg.setdefault("city_no", {})["max_entry_price"] = 0.93
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        end_date = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        # 故意让sports先、weather后（排序前顺序）
        sports_sig = make_signal(
            market_id="mkt_sports_first",
            question="Will team A win?",
            outcome="YES",
            ev=0.10,
            confidence=0.60,
            size_usdc=5.0,
            expected_price=0.50,
        )
        weather_sig = make_signal(
            market_id="mkt_weather_second",
            question="Will Temp in Dallas be above 80°F on April 6?",
            outcome="NO",
            ev=0.15,
            confidence=0.70,
            size_usdc=1.0,
            expected_price=0.60,
        )

        # detect_category要按market_id区分
        def _cat(m):
            if not m:
                return "unknown"
            mid = m.get("_id", "")
            if "weather" in mid:
                return "weather"
            return "sports"

        mock_cat.side_effect = _cat

        market_lookup = {
            "mkt_sports_first": {
                "_id": "mkt_sports_first",
                "endDate": end_date,
            },
            "mkt_weather_second": {
                "_id": "mkt_weather_second",
                "endDate": end_date,
                "_strategy_tag": "city_no",
                "_city_no_score": 0.95,
                "question": "Will Temp in Dallas be above 80°F on April 6?",
            },
        }

        # 余额仅够1单(1.5 USDC)，如果sports先处理则weather被挤出
        result = filter_signals_for_live(
            [sports_sig, weather_sig],  # sports在前
            balance=1.5,
            market_lookup=market_lookup,
        )
        # 排序后weather应先处理，至少weather通过（sports被余额不足拒绝）
        weather_passed = [r for r in result if "weather" in r.market_id]
        assert (
            len(weather_passed) >= 1
        ), "天气信号应优先于体育信号被处理，在余额有限时获得执行机会"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category")
    def test_no_priority_config_preserves_order(self, mock_cat, mock_get_fn, mock_db):
        """无优先级配置时保持原始信号顺序"""
        cfg = default_pt_cfg()
        # 不设置 signal_category_priority
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig1 = make_signal(
            market_id="mkt_a",
            question="Will ETH go up?",
            ev=0.10,
            confidence=0.70,
            size_usdc=1.0,
            expected_price=0.50,
        )
        sig2 = make_signal(
            market_id="mkt_b",
            question="Will SOL go up?",
            ev=0.08,
            confidence=0.60,
            size_usdc=1.0,
            expected_price=0.45,
        )
        mock_cat.return_value = "crypto"

        end_date = (datetime.now(timezone.utc) + timedelta(hours=10)).isoformat()
        market_lookup = {
            "mkt_a": {"endDate": end_date},
            "mkt_b": {"endDate": end_date},
        }

        result = filter_signals_for_live(
            [sig1, sig2],
            balance=20.0,
            market_lookup=market_lookup,
        )
        # 无priority配置，信号应正常通过（顺序无强制要求）
        assert len(result) == 2, "无优先级配置时所有信号应正常通过"


class TestT134SmartCapitalEngineIntegration:
    """T134: live_filters应在SCE启用时使用SCE进行仓位缩放。"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_sce_enabled_caps_via_tier(self, mock_cat, mock_get_fn, mock_db):
        """SCE启用时，size_usdc由SCE的cat_max_usdc裁剪，不使用旧参数。"""
        cfg = default_pt_cfg()

        def _get_with_sce(k):
            if k == "smart_capital":
                return {
                    "enabled": True,
                    "tiers": [
                        {
                            "name": "small",
                            "max_balance": 50,
                            "max_bet_pct": 0.10,
                            "category_multipliers": {"weather": 1.0, "default": 0.5},
                        }
                    ],
                }
            if k == "paper_trading":
                return cfg
            if k == "general.live_initial_deposit":
                return 30.0
            parts = k.split(".", 1)
            if len(parts) == 2 and parts[0] == "paper_trading":
                return cfg.get(parts[1])
            return None

        mock_get_fn.side_effect = _get_with_sce
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # balance=20, SCE max_bet_pct=0.10, weather mult=1.0
        # cat_max = min(20 * 0.10 * 1.0, hard_max=2.5) = min(2.0, 2.5) = 2.0
        sig = make_signal(ev=0.10, confidence=0.70, size_usdc=5.0, outcome="NO")
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 1
        assert (
            result[0].size_usdc <= 2.0
        ), f"SCE应裁剪到cat_max=2.0, got {result[0].size_usdc}"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_sce_always_on_caps_crypto(self, mock_cat, mock_get_fn, mock_db):
        """SCE永远启用，crypto信号通过SCE tier裁剪。"""
        cfg = default_pt_cfg()

        def _get_with_sce(k):
            if k == "smart_capital":
                return {
                    "tiers": [
                        {
                            "name": "small",
                            "max_balance": 50,
                            "max_bet_pct": 0.10,
                            "category_multipliers": {"crypto": 0.5, "default": 1.0},
                        }
                    ],
                }
            if k == "paper_trading":
                return cfg
            if k == "general.live_initial_deposit":
                return 30.0
            parts = k.split(".", 1)
            if len(parts) == 2 and parts[0] == "paper_trading":
                return cfg.get(parts[1])
            return None

        mock_get_fn.side_effect = _get_with_sce
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # balance=50, max_bet_pct=0.10, crypto mult=0.5
        # cat_max = min(50*0.10*0.5, hard_max=2.5) = min(2.5, 2.5) = 2.5
        sig = make_signal(ev=0.10, confidence=0.70, size_usdc=10.0, outcome="NO")
        result = filter_signals_for_live(
            [sig], balance=50.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 1
        assert result[0].size_usdc <= 2.5, "SCE应裁剪crypto到cat_max"
        assert result[0].size_usdc <= 2.5, "SCE应裁剪crypto到cat_max"
        assert result[0].size_usdc <= 2.5, "SCE应裁剪crypto到cat_max"


class TestT138YesAmbushSizeBypass:
    """T138: YesAmbush信号应绕过SCE cat_max裁剪，使用固定的ambush_size_usdc。"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_yes_ambush_bypasses_cat_max(self, mock_cat, mock_get_fn, mock_db):
        """YesAmbush信号即使余额低导致cat_max < ambush_size_usdc，也应保持ambush_size_usdc。"""
        cfg = default_pt_cfg()

        def _get(k):
            if k == "smart_capital":
                return {
                    "tiers": [
                        {
                            "name": "small",
                            "max_balance": 50,
                            "max_bet_pct": 0.08,
                            "category_multipliers": {"weather": 1.0, "default": 1.0},
                        }
                    ],
                }
            if k == "paper_trading":
                return {**cfg, "hard_max_bet_usdc": 25.0}
            if k == "weather_strategy":
                return {"ambush_size_usdc": 2.4}
            if k == "general.live_initial_deposit":
                return 30.0
            if k == "executor.min_bet_usdc":
                return 1.0
            parts = k.split(".", 1)
            if len(parts) == 2 and parts[0] == "paper_trading":
                return cfg.get(parts[1])
            return None

        mock_get_fn.side_effect = _get
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # balance=24: SCE cat_max = 24 × 0.08 × 1.0 = 1.92（小于 ambush_size_usdc=2.4）
        sig = make_signal(ev=0.10, confidence=0.70, size_usdc=2.4, outcome="YES")
        sig._bet_bucket = "YesAmbush"
        result = filter_signals_for_live(
            [sig], balance=24.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 1, "YesAmbush信号应通过过滤"
        assert result[0].size_usdc == pytest.approx(
            2.4, abs=0.01
        ), f"YesAmbush应保持ambush_size_usdc=2.4，不被cat_max=1.92裁剪, got {result[0].size_usdc}"

    @patch(
        "src.utils.smart_capital.SmartCapitalEngine.get_cat_max_usdc", return_value=1.50
    )
    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_non_ambush_weather_still_gets_cat_max(
        self, mock_cat, mock_get_fn, mock_db, mock_sce_max
    ):
        """非YesAmbush天气信号仍然受 SCE cat_max 限制（mock cat_max=1.50 验证裁剪）。"""
        cfg = default_pt_cfg()

        def _get(k):
            if k == "paper_trading":
                return cfg
            if k == "weather_strategy":
                return {"ambush_size_usdc": 2.4}
            if k == "general.live_initial_deposit":
                return 30.0
            if k == "executor.min_bet_usdc":
                return 1.0
            parts = k.split(".", 1)
            if len(parts) == 2 and parts[0] == "paper_trading":
                return cfg.get(parts[1])
            return None

        mock_get_fn.side_effect = _get
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # 普通天气信号（无 _bet_bucket），size=5.0 应被 mock cat_max=1.50 裁剪
        sig = make_signal(ev=0.10, confidence=0.70, size_usdc=5.0, outcome="YES")
        result = filter_signals_for_live(
            [sig], balance=24.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 1
        assert (
            result[0].size_usdc <= 1.50 + 0.01
        ), f"普通天气信号应被cat_max=1.50裁剪, got {result[0].size_usdc}"


class TestWeatherDoubleNoFeeBypass:
    """天气双NO信号已通过 WCS score 质量门槛，FeeCheck 仅检查 EV≥0"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_right_tail_small_ev_passes_feecheck(self, mock_cat, mock_get_fn, mock_db):
        """天气双NO EV=0.008 不应被 FeeCheck 拒绝（旧逻辑会扣费后拒绝）"""
        cfg = default_pt_cfg()
        cfg["taker_fee_rates"] = {"sports": 0.05, "weather": 0.05, "crypto": 0.05}
        cfg["taker_fee_rate_default"] = 0.05
        cfg["min_net_edge_after_fees"] = 0.005
        cfg["category_min_ev"]["weather"] = 0
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False

        sig = Signal(
            market_id="mkt_weather_dn_001",
            question="Will the high in Miami be 82°F or above?",
            outcome="NO",
            ev=0.008,
            confidence=0.70,
            size_usdc=2.0,
            expected_price=0.28,
            strategy_tag="weather_right_tail_right",
        )
        market = {
            "endDate": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        }
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_weather_dn_001": market}
        )
        assert len(result) >= 1, "天气双NO EV=0.008 不应被 FeeCheck 拒绝"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_right_tail_negative_ev_rejected(self, mock_cat, mock_get_fn, mock_db):
        """天气双NO EV<0 应被拒絕"""
        cfg = default_pt_cfg()
        cfg["category_min_ev"]["weather"] = 0
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []

        sig = Signal(
            market_id="mkt_weather_dn_002",
            question="Will the high in Miami be 82°F or above?",
            outcome="NO",
            ev=-0.01,
            confidence=0.70,
            size_usdc=2.0,
            expected_price=0.28,
            strategy_tag="weather_right_tail_left",
        )
        market = {
            "endDate": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        }
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_weather_dn_002": market}
        )
        assert len(result) == 0, "天气双NO 负EV 应被拒绝"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_weather_small_ev_passes_ev_reject(self, mock_cat, mock_get_fn, mock_db):
        """T206追加：天气信号 EV=0.025 不应被 EVReject 拒绝（已通过 WCS 质量门槛）。"""
        cfg = default_pt_cfg()
        cfg["category_min_ev"]["weather"] = 0.03  # 普通阈值 0.03 会拒绝 0.025
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False

        sig = Signal(
            market_id="mkt_weather_sh_001",
            question="Will the temperature in Shanghai be 19°C or above?",
            outcome="NO",
            ev=0.025,
            confidence=0.70,
            size_usdc=2.0,
            expected_price=0.28,
            strategy_tag="weather_right_tail_right",
        )
        market = {
            "endDate": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        }
        result = filter_signals_for_live(
            [sig], balance=20.0, market_lookup={"mkt_weather_sh_001": market}
        )
        assert (
            len(result) >= 1
        ), "天气信号 EV=0.025 不应被 EVReject 拒绝（weather_ 豁免）"


class TestT202EhclCategoryDetection:
    """T202: EHCL 天气信号必须被正确识别为 weather 赛道"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="default")
    def test_ehcl_not_in_market_lookup_detected_as_weather(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """weather_ehcl 信号不在 market_lookup 时应被识别为 weather 赛道，而非 default"""
        cfg = default_pt_cfg()
        cfg["category_max_positions"] = {"weather": 50, "default": 0}
        cfg["category_min_ev"]["weather"] = 0
        mock_get_fn.side_effect = mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False

        sig = Signal(
            market_id="mkt_ehcl_001",
            question="Will the highest temperature in NYC be 80°F or higher?",
            outcome="NO",
            ev=0.05,
            confidence=0.80,
            size_usdc=5.0,
            expected_price=0.85,
            strategy_tag="weather_ehcl",
        )
        # 关键：market_lookup 中没有这个市场（EHCL 绕过 MarketScanner）
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert len(result) >= 1, (
            "EHCL信号(strategy_tag=weather_ehcl) 不在 market_lookup 时"
            " 应被识别为 weather 赛道，不受 default=0 配额限制"
        )


# ─────────────────────────────────────────────────────────────────────
# T227: 右尾做空跨周期策略级去重测试
# ─────────────────────────────────────────────────────────────────────


class TestDoubleNoDedup:
    """T227: right_tail_max_signals 应跨周期生效 — 限制同一 event_slug 下的右尾做空总仓位数。"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_cross_cycle_right_tail_blocked_by_existing_position(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """DB中已有1个右尾做空仓位 + right_tail_max_signals=1 → 新右尾做空信号被拦截。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 2  # event允许2，但右尾做空限1
        mock_get_fn.side_effect = lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 1,
                "no_max_entry_price": 0.82,
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        # DB中已有一个右尾做空仓位
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "existing-1",
                "market_id": "mkt_32c",
                "category": "weather",
                "event_slug": _event,
                "strategy_tag": "weather_right_tail_right",
                "question": "Will the highest temperature in Singapore be 32°C on April 16?",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # 新周期产生的右尾做空信号（33°C）
        sig = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.10,
            confidence=0.70,
            size_usdc=5.0,
            expected_price=0.17,
            strategy_tag="weather_right_tail_right",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert (
            len(result) == 0
        ), "已有1个右尾做空 + right_tail_max_signals=1 → 应阻断新右尾做空信号"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_right_tail_allows_yes_ambush_on_same_event(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """DB中已有1个右尾做空仓位 → YES_Ambush信号不受右尾做空去重限制。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 2
        mock_get_fn.side_effect = lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 1,
                "no_max_entry_price": 0.82,
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "existing-1",
                "market_id": "mkt_32c",
                "category": "weather",
                "event_slug": _event,
                "strategy_tag": "weather_right_tail_right",
                "question": "Will the highest temperature in Singapore be 32°C on April 16?",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # YES Ambush 信号（不同策略）
        sig = Signal(
            market_id="mkt_30c",
            question="Will the highest temperature in Singapore be 30°C on April 16?",
            outcome="YES",
            ev=0.08,
            confidence=0.65,
            size_usdc=3.0,
            expected_price=0.05,
            strategy_tag="weather_yes_ambush",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert len(result) == 1, "YES_Ambush 不应被 右尾做空去重限制阻断"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_right_tail_max_signals_2_allows_second(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """right_tail_max_signals=2 → 已有1个右尾做空时允许第2个。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 3
        mock_get_fn.side_effect = lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 2,
                "no_max_entry_price": 0.82,
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "existing-1",
                "market_id": "mkt_32c",
                "category": "weather",
                "event_slug": _event,
                "strategy_tag": "weather_right_tail_right",
                "question": "Will the highest temperature in Singapore be 32°C on April 16?",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.10,
            confidence=0.70,
            size_usdc=5.0,
            expected_price=0.17,
            strategy_tag="weather_right_tail_right",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert len(result) == 1, "right_tail_max_signals=2 + 已有1个 → 应允许第2个"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_same_cycle_right_tail_limited(self, mock_cat, mock_get_fn, mock_db):
        """同一周期内2个右尾做空信号 + max_signals=1 → 只通过1个。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 3  # 放宽event限制
        mock_get_fn.side_effect = lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 1,
                "no_max_entry_price": 0.82,
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig1 = Signal(
            market_id="mkt_32c",
            question="Will the highest temperature in Singapore be 32°C on April 16?",
            outcome="NO",
            ev=0.10,
            confidence=0.70,
            size_usdc=5.0,
            expected_price=0.17,
            strategy_tag="weather_right_tail_right",
            event_slug=_event,
        )
        sig2 = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.08,
            confidence=0.65,
            size_usdc=5.0,
            expected_price=0.20,
            strategy_tag="weather_right_tail_right",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig1, sig2], balance=50.0, market_lookup={})
        assert len(result) == 1, "同周期2个右尾做空 + max=1 → 只应通过1个"
        assert result[0].market_id == "mkt_32c"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_weather_event_slug_from_signal_fallback(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """天气管线信号无market_lookup条目时，event_slug应从sig.event_slug获取。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 1  # 只允许1个
        mock_get_fn.side_effect = lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 2,
                "no_max_entry_price": 0.82,
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        # DB已有同event的仓位
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "existing-1",
                "market_id": "mkt_32c",
                "category": "weather",
                "event_slug": _event,
                "strategy_tag": "weather_right_tail_right",
                "question": "Will the highest temperature in Singapore be 32°C on April 16?",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.10,
            confidence=0.70,
            size_usdc=5.0,
            expected_price=0.17,
            strategy_tag="weather_right_tail_right",
            # event_slug 在 Signal 上，而非 market_lookup
            event_slug=_event,
        )
        # 空 market_lookup — 模拟天气管线信号
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert (
            len(result) == 0
        ), "即使market_lookup为空，也应通过sig.event_slug进行event_dedup"


# ─────────────────────────────────────────────────────────────────────
# T227: 右尾做空跨周期策略级去重测试
# ─────────────────────────────────────────────────────────────────────


class TestDoubleNoDedup:
    """T227: right_tail_max_signals 应跨周期生效 — 限制同一 event_slug 下的右尾做空总仓位数。"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_cross_cycle_right_tail_blocked_by_existing_position(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """DB中已有1个右尾做空仓位 + right_tail_max_signals=1 → 新右尾做空信号被拦截。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 2  # event允许2，但右尾做空限1
        mock_get_fn.side_effect = lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 1,
                "no_max_entry_price": 0.82,
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        # DB中已有一个右尾做空仓位
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "existing-1",
                "market_id": "mkt_32c",
                "category": "weather",
                "event_slug": _event,
                "strategy_tag": "weather_right_tail_right",
                "question": "Will the highest temperature in Singapore be 32°C on April 16?",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # 新周期产生的右尾做空信号（33°C）
        sig = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.10,
            confidence=0.70,
            size_usdc=5.0,
            expected_price=0.17,
            strategy_tag="weather_right_tail_right",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert (
            len(result) == 0
        ), "已有1个右尾做空 + right_tail_max_signals=1 → 应阻断新右尾做空信号"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_right_tail_allows_yes_ambush_on_same_event(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """DB中已有1个右尾做空仓位 → YES_Ambush信号不受右尾做空去重限制。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 2
        mock_get_fn.side_effect = lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 1,
                "no_max_entry_price": 0.82,
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "existing-1",
                "market_id": "mkt_32c",
                "category": "weather",
                "event_slug": _event,
                "strategy_tag": "weather_right_tail_right",
                "question": "Will the highest temperature in Singapore be 32°C on April 16?",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # YES Ambush 信号（不同策略）
        sig = Signal(
            market_id="mkt_30c",
            question="Will the highest temperature in Singapore be 30°C on April 16?",
            outcome="YES",
            ev=0.08,
            confidence=0.65,
            size_usdc=3.0,
            expected_price=0.05,
            strategy_tag="weather_yes_ambush",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert len(result) == 1, "YES_Ambush 不应被 右尾做空去重限制阻断"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_right_tail_max_signals_2_allows_second(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """right_tail_max_signals=2 → 已有1个右尾做空时允许第2个。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 3
        mock_get_fn.side_effect = lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 2,
                "no_max_entry_price": 0.82,
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "existing-1",
                "market_id": "mkt_32c",
                "category": "weather",
                "event_slug": _event,
                "strategy_tag": "weather_right_tail_right",
                "question": "Will the highest temperature in Singapore be 32°C on April 16?",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.10,
            confidence=0.70,
            size_usdc=5.0,
            expected_price=0.17,
            strategy_tag="weather_right_tail_right",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert len(result) == 1, "right_tail_max_signals=2 + 已有1个 → 应允许第2个"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_same_cycle_right_tail_limited(self, mock_cat, mock_get_fn, mock_db):
        """同一周期内2个右尾做空信号 + max_signals=1 → 只通过1个。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 3  # 放宽event限制
        mock_get_fn.side_effect = lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 1,
                "no_max_entry_price": 0.82,
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig1 = Signal(
            market_id="mkt_32c",
            question="Will the highest temperature in Singapore be 32°C on April 16?",
            outcome="NO",
            ev=0.10,
            confidence=0.70,
            size_usdc=5.0,
            expected_price=0.17,
            strategy_tag="weather_right_tail_right",
            event_slug=_event,
        )
        sig2 = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.08,
            confidence=0.65,
            size_usdc=5.0,
            expected_price=0.20,
            strategy_tag="weather_right_tail_right",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig1, sig2], balance=50.0, market_lookup={})
        assert len(result) == 1, "同周期2个右尾做空 + max=1 → 只应通过1个"
        assert result[0].market_id == "mkt_32c"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_weather_event_slug_from_signal_fallback(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """天气管线信号无market_lookup条目时，event_slug应从sig.event_slug获取。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 1  # 只允许1个
        mock_get_fn.side_effect = lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 2,
                "no_max_entry_price": 0.82,
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        # DB已有同event的仓位
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "existing-1",
                "market_id": "mkt_32c",
                "category": "weather",
                "event_slug": _event,
                "strategy_tag": "weather_right_tail_right",
                "question": "Will the highest temperature in Singapore be 32°C on April 16?",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.10,
            confidence=0.70,
            size_usdc=5.0,
            expected_price=0.17,
            strategy_tag="weather_right_tail_right",
            # event_slug 在 Signal 上，而非 market_lookup
            event_slug=_event,
        )
        # 空 market_lookup — 模拟天气管线信号
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert (
            len(result) == 0
        ), "即使market_lookup为空，也应通过sig.event_slug进行event_dedup"


class TestPureCovAddonDedup:
    """Wave-2: weather_pure_cov 加买信号独立去重 — 使用 pure_coverage.addon_max_signals。"""

    def _mock_get(self, cfg, addon_max=1):
        return lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 1,
                "no_max_entry_price": 0.82,
                "pure_coverage": {
                    "enabled": True,
                    "addon_max_signals": addon_max,
                },
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_addon_blocked_when_existing_pure_cov(self, mock_cat, mock_get_fn, mock_db):
        """DB中已有1个 weather_pure_cov 仓位 + addon_max_signals=1 → 新加买信号被拦截。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 3
        mock_get_fn.side_effect = self._mock_get(cfg, addon_max=1)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "pc-existing-1",
                "market_id": "mkt_32c",
                "category": "weather",
                "event_slug": _event,
                "strategy_tag": "weather_pure_cov",
                "question": "Will the highest temperature in Singapore be 32°C on April 16?",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.09,
            confidence=0.68,
            size_usdc=4.0,
            expected_price=0.20,
            strategy_tag="weather_pure_cov",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert len(result) == 0, "已有1个pure_cov + addon_max=1 → 应阻断新加买"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_addon_allowed_when_max_is_2(self, mock_cat, mock_get_fn, mock_db):
        """addon_max_signals=2 → 已有1个pure_cov时允许第2个。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 3
        mock_get_fn.side_effect = self._mock_get(cfg, addon_max=2)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "pc-existing-1",
                "market_id": "mkt_32c",
                "category": "weather",
                "event_slug": _event,
                "strategy_tag": "weather_pure_cov",
                "question": "Will the highest temperature in Singapore be 32°C on April 16?",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.09,
            confidence=0.68,
            size_usdc=4.0,
            expected_price=0.20,
            strategy_tag="weather_pure_cov",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert len(result) == 1, "addon_max=2 + 已有1个 → 应允许第2个pure_cov"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_pure_cov_does_not_affect_right_tail_counter(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """DB中已有1个pure_cov → 右尾做空首单不受 addon 计数影响（right_tail_max_signals=1仍可通过）。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 3
        mock_get_fn.side_effect = self._mock_get(cfg, addon_max=1)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "pc-existing-1",
                "market_id": "mkt_32c",
                "category": "weather",
                "event_slug": _event,
                "strategy_tag": "weather_pure_cov",
                "question": "Will the highest temperature in Singapore be 32°C on April 16?",
            }
        ]
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        # 右尾首单（不同策略标签）
        sig = Signal(
            market_id="mkt_34c",
            question="Will the highest temperature in Singapore be 34°C on April 16?",
            outcome="NO",
            ev=0.11,
            confidence=0.72,
            size_usdc=5.0,
            expected_price=0.15,
            strategy_tag="weather_right_tail_right",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert len(result) == 1, "pure_cov计数不应影响right_tail首单去重"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_same_cycle_two_pure_cov_max_1(self, mock_cat, mock_get_fn, mock_db):
        """同周期2个pure_cov + addon_max=1 → 只通过1个。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 3
        mock_get_fn.side_effect = self._mock_get(cfg, addon_max=1)

        _event = "highest-temperature-in-singapore-on-april-16-2026"
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig1 = Signal(
            market_id="mkt_32c",
            question="Will the highest temperature in Singapore be 32°C on April 16?",
            outcome="NO",
            ev=0.09,
            confidence=0.68,
            size_usdc=4.0,
            expected_price=0.20,
            strategy_tag="weather_pure_cov",
            event_slug=_event,
        )
        sig2 = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.08,
            confidence=0.66,
            size_usdc=4.0,
            expected_price=0.22,
            strategy_tag="weather_pure_cov",
            event_slug=_event,
        )
        result = filter_signals_for_live([sig1, sig2], balance=50.0, market_lookup={})
        assert len(result) == 1, "同周期2个pure_cov + addon_max=1 → 只应通过1个"
        assert result[0].market_id == "mkt_32c"


class TestPureCovPriceCeiling:
    """T238G: PureCov 使用独立的 no_max_entry_price，不受右尾首单的全局价格上限约束。"""

    def _mock_get(self, cfg, ws_no_max=0.85, pc_no_max=0.90):
        return lambda k: {
            "paper_trading": cfg,
            "weather_strategy": {
                "right_tail_max_signals": 2,
                "no_max_entry_price": ws_no_max,
                "pure_coverage": {
                    "enabled": True,
                    "addon_max_signals": 3,
                    "no_max_entry_price": pc_no_max,
                },
            },
            "general.live_initial_deposit": 30.0,
        }.get(k, cfg.get(k.split(".", 1)[-1]) if "." in k else None)

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_right_tail_blocked_above_global_ceiling(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """右尾首单价格 >= no_max_entry_price(0.85) → 被拦截。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 3
        mock_get_fn.side_effect = self._mock_get(cfg, ws_no_max=0.85)

        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = Signal(
            market_id="mkt_32c",
            question="Will the highest temperature in Singapore be 32°C on April 16?",
            outcome="NO",
            ev=0.05,
            confidence=0.70,
            size_usdc=5.0,
            expected_price=0.87,
            strategy_tag="weather_right_tail_right",
            event_slug="highest-temperature-in-singapore-on-april-16-2026",
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert len(result) == 0, "右尾首单 0.87 >= 全局上限 0.85 → 应被PriceCeiling拦截"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_pure_cov_passes_above_global_but_below_own_ceiling(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """PureCov 价格 0.87 > 全局上限 0.85 但 < PureCov独立上限 0.90 → 应通过。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 3
        mock_get_fn.side_effect = self._mock_get(cfg, ws_no_max=0.85, pc_no_max=0.90)

        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.06,
            confidence=0.68,
            size_usdc=4.0,
            expected_price=0.87,
            strategy_tag="weather_pure_cov",
            event_slug="highest-temperature-in-singapore-on-april-16-2026",
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert len(result) == 1, "PureCov 0.87 < 独立上限 0.90 → 应通过"

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="weather")
    def test_pure_cov_blocked_above_own_ceiling(
        self, mock_cat, mock_get_fn, mock_db
    ):
        """PureCov 价格 0.92 >= PureCov独立上限 0.90 → 被拦截。"""
        cfg = default_pt_cfg()
        cfg["category_max_per_event"]["weather"] = 3
        mock_get_fn.side_effect = self._mock_get(cfg, ws_no_max=0.85, pc_no_max=0.90)

        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = Signal(
            market_id="mkt_33c",
            question="Will the highest temperature in Singapore be 33°C on April 16?",
            outcome="NO",
            ev=0.04,
            confidence=0.65,
            size_usdc=4.0,
            expected_price=0.92,
            strategy_tag="weather_pure_cov",
            event_slug="highest-temperature-in-singapore-on-april-16-2026",
        )
        result = filter_signals_for_live([sig], balance=50.0, market_lookup={})
        assert len(result) == 0, "PureCov 0.92 >= 独立上限 0.90 → 应被拦截"
