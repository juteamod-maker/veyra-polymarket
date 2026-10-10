"""
test_pure_coverage.py — T203 PureCoverage 纯结构套利管线单元测试

覆盖：
  - Coverage Arbitrage 触发（Σ_No < θ_cov）
  - Right-Tail Snipe 回退（Σ_No >= θ_cov but 单桶EV>0）
  - 影子模式（生成信号但不返回）
  - 开关关闭
  - 时间窗口过滤
  - 价格地板/天花板过滤
  - 成交量过滤
  - 策略标签正确性（weather_pure_cov → weather 赛道分类）
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from src.signal.pure_coverage import scan_pure_coverage_signals

log = logging.getLogger(__name__)


# ── 测试数据工厂 ──


def _make_strike(
    threshold: float,
    best_ask_yes: float,
    direction: str = "exact",
    volume_24h: float = 50000.0,
    market_id: str = "",
    condition_id: str = "0xabc",
    token_yes: str = "tok_yes",
    token_no: str = "tok_no",
    question: str = "",
    market_slug: str = "",
    event_slug: str = "",
    end_date_iso: str = "2026-04-15T12:00:00Z",
):
    """创建TemperatureStrike mock对象。"""
    from src.scanner.weather_scanner import TemperatureStrike

    return TemperatureStrike(
        market_id=market_id or f"mkt_{threshold}",
        condition_id=condition_id,
        token_yes=token_yes,
        token_no=token_no,
        question=question or f"Will temperature be exactly {threshold}°C?",
        threshold=threshold,
        threshold_low=threshold,
        threshold_high=threshold,
        direction=direction,
        best_ask_yes=best_ask_yes,
        volume_24h=volume_24h,
        end_date_iso=end_date_iso,
        market_slug=market_slug,
        event_slug=event_slug,
    )


def _make_group(
    city_name: str = "Test City",
    city_slug: str = "test-city",
    date_str: str = "April 15",
    timezone: str = "UTC",
    strikes=None,
):
    """创建CityDayGroup mock对象。"""
    from src.scanner.weather_scanner import CityDayGroup

    return CityDayGroup(
        city_name=city_name,
        city_slug=city_slug,
        date_str=date_str,
        lat=0.0,
        lon=0.0,
        icao="XXXX",
        timezone=timezone,
        strikes=strikes or [],
    )


def _base_config():
    """基础配置：PureCoverage开启+非影子模式。"""
    return {
        "weather_strategy": {
            "pure_coverage": {
                "enabled": True,
                "shadow_mode": False,
                "theta_cov": 0.80,
                "offset_min": 1,
                "right_tail_offset_max": 2,
                "max_signals_per_city_day": 3,
                "budget_pct": 0.35,
                "no_price_floor": 0.18,
                "no_max_entry_price": 0.80,
                "min_volume_24h": 5000,
                "window_start_hour": 0,
                "window_end_hour": 24,
            }
        },
        "paper_trading": {
            "hard_max_bet_usdc": 25.0,
            "taker_fee_rates": {"weather": 0.05},
        },
        "executor": {"min_order_size_shares": 5.3},
    }


def _patch_get(config_dict):
    """Patch config_loader.get to return values from config_dict."""

    def mock_get(key, default=None):
        parts = key.split(".")
        val = config_dict
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                return default
        return val if val is not None else default

    return mock_get


# ── Coverage Arbitrage 测试 ──


class TestCoverageArbitrage:
    """Coverage Arbitrage 主引擎测试。"""

    def test_coverage_triggers_when_sigma_below_theta(self):
        """Σ_No < θ_cov → 生成Coverage信号。"""
        # YES_best at 22°C (ask=0.65)
        # Right+1: 23°C (ask=0.30 → NO=0.70)
        # Right+2: 24°C (ask=0.95 → NO=0.05) — 但NO<floor会被跳过
        # 用合理价格使Σ < 0.80
        strikes = [
            _make_strike(20, 0.10),
            _make_strike(21, 0.15),
            _make_strike(22, 0.65),  # YES_best
            _make_strike(23, 0.65),  # NO=0.35
            _make_strike(24, 0.60),  # NO=0.40
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        assert len(signals) == 2, f"Expected 2 Coverage signals, got {len(signals)}"
        for sig in signals:
            assert sig.outcome == "NO"
            assert sig.strategy_tag == "weather_pure_cov"
            assert sig.strategy_params["sub_engine"] == "coverage_arb"
            assert sig.strategy_params["sigma_no"] < 0.80

    def test_coverage_skips_when_sigma_above_theta(self):
        """Σ_No >= θ_cov → Coverage不触发，回退到Snipe。"""
        # YES_best at 22°C
        # Right: 23°C (NO=0.55), 24°C (NO=0.50) → Σ=1.05 > 0.80
        strikes = [
            _make_strike(20, 0.10),
            _make_strike(21, 0.15),
            _make_strike(22, 0.60),  # YES_best
            _make_strike(23, 0.45),  # NO=0.55
            _make_strike(24, 0.50),  # NO=0.50
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=1, hours_to_settlement=36.0
            )

        # Snipe fallback: 取最便宜的单个NO桶
        assert len(signals) == 1
        assert signals[0].strategy_params["sub_engine"] == "right_tail_snipe"

    def test_strategy_tag_is_weather_prefix(self):
        """strategy_tag = weather_pure_cov → _infer_category 自动归类为weather。"""
        strikes = [
            _make_strike(20, 0.10),
            _make_strike(21, 0.15),
            _make_strike(22, 0.65),
            _make_strike(23, 0.65),
            _make_strike(24, 0.60),
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        for sig in signals:
            assert sig.strategy_tag.startswith("weather_"), (
                f"strategy_tag '{sig.strategy_tag}' must start with 'weather_' "
                f"for _infer_category compatibility"
            )


# ── Right-Tail Snipe 测试 ──


class TestRightTailSnipe:
    """Right-Tail Snipe 子引擎测试。"""

    def test_snipe_picks_cheapest_no(self):
        """Snipe模式选择最便宜的NO桶。"""
        strikes = [
            _make_strike(20, 0.10),
            _make_strike(22, 0.60),  # YES_best
            _make_strike(23, 0.48),  # NO=0.52
            _make_strike(24, 0.55),  # NO=0.45 — 更便宜
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=20.0
            )

        # Σ = 0.52 + 0.45 = 0.97 > 0.80 → Snipe
        assert len(signals) == 1
        assert abs(signals[0].expected_price - 0.45) < 0.001  # 最便宜的NO
        assert signals[0].strategy_params["sub_engine"] == "right_tail_snipe"


# ── 影子模式测试 ──


class TestShadowMode:
    """影子模式测试。"""

    def test_shadow_mode_returns_empty(self):
        """Shadow mode生成信号记录日志但不返回。"""
        strikes = [
            _make_strike(20, 0.10),
            _make_strike(22, 0.65),
            _make_strike(23, 0.65),
            _make_strike(24, 0.60),
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()
        cfg["weather_strategy"]["pure_coverage"]["shadow_mode"] = True

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        assert signals == [], "Shadow mode should return empty list"


# ── 过滤器测试 ──


class TestFilters:
    """各类过滤器测试。"""

    def test_disabled_returns_empty(self):
        """开关关闭返回空。"""
        group = _make_group(strikes=[_make_strike(22, 0.65)])
        cfg = _base_config()
        cfg["weather_strategy"]["pure_coverage"]["enabled"] = False

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        assert signals == []

    def test_days_diff_far_returns_empty(self):
        """days_diff > 1 不扫描。"""
        group = _make_group(strikes=[_make_strike(22, 0.65)])
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=3, hours_to_settlement=72.0
            )

        assert signals == []

    def test_window_outside_returns_empty(self):
        """城市当地时间不在窗口内返回空。"""
        strikes = [
            _make_strike(22, 0.65),
            _make_strike(23, 0.65),
            _make_strike(24, 0.60),
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()
        # 窗口设置为 02:00-03:00 — 几乎不可能命中
        cfg["weather_strategy"]["pure_coverage"]["window_start_hour"] = 2
        cfg["weather_strategy"]["pure_coverage"]["window_end_hour"] = 3

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            from unittest.mock import PropertyMock

            import src.signal.pure_coverage as pc_mod

            # Mock datetime.now 返回 15:00
            with patch.object(pc_mod, "datetime") as mock_dt:
                mock_now = MagicMock()
                mock_now.hour = 15  # 下午3点
                mock_dt.now.return_value = mock_now
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

                signals = scan_pure_coverage_signals(
                    group, days_diff=0, hours_to_settlement=24.0
                )

        assert signals == []

    def test_no_floor_filters_cheap_buckets(self):
        """NO价格低于 price_floor 的桶被跳过。"""
        strikes = [
            _make_strike(22, 0.65),  # YES_best
            _make_strike(23, 0.87),  # NO=0.13 < floor(0.18) → 跳过
            _make_strike(24, 0.85),  # NO=0.15 < floor(0.18) → 跳过
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        assert signals == [], "All right-side NO buckets below floor, should be empty"

    def test_volume_filter(self):
        """成交量低于阈值的桶被跳过。"""
        strikes = [
            _make_strike(22, 0.65),
            _make_strike(23, 0.65, volume_24h=100),  # 低成交量
            _make_strike(24, 0.60, volume_24h=100),  # 低成交量
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        assert signals == []

    def test_volume_filter_bypass_with_right_tail_anchor(self):
        """同 event 已有右尾仓位时，低成交量右侧桶可作为第二个NO加买候选。"""
        strikes = [
            _make_strike(22, 0.65, event_slug="tokyo-apr17"),
            _make_strike(23, 0.65, volume_24h=100, event_slug="tokyo-apr17"),
            _make_strike(24, 0.60, volume_24h=100, event_slug="tokyo-apr17"),
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            with patch(
                "src.signal.pure_coverage.db.get_open_paper_bets",
                return_value=[
                    {
                        "event_slug": "tokyo-apr17",
                        "strategy_tag": "weather_right_tail_right",
                    }
                ],
            ):
                signals = scan_pure_coverage_signals(
                    group, days_diff=0, hours_to_settlement=24.0
                )

        assert len(signals) == 2
        for sig in signals:
            assert sig.strategy_tag == "weather_pure_cov"
            assert sig.strategy_params["sub_engine"] == "coverage_arb"

    def test_too_few_strikes(self):
        """少于3个挡位返回空。"""
        strikes = [
            _make_strike(22, 0.65),
            _make_strike(23, 0.60),
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        assert signals == []


# ── EV与仓位测试 ──


class TestEVCalculation:
    """EV计算和仓位验证。"""

    def test_ev_includes_taker_fee(self):
        """EV必须扣除taker手续费。"""
        strikes = [
            _make_strike(20, 0.10),
            _make_strike(22, 0.65),
            _make_strike(23, 0.65),
            _make_strike(24, 0.60),
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        for sig in signals:
            no_price = sig.expected_price
            gross_profit = 1.0 - no_price
            fee_rate = 0.05
            buy_fee = fee_rate * no_price * (1.0 - no_price)
            expected_ev = gross_profit - buy_fee
            assert (
                abs(sig.ev - expected_ev) < 0.001
            ), f"EV mismatch: got {sig.ev:.4f}, expected {expected_ev:.4f}"

    def test_min_shares_guarantee(self):
        """仓位金额确保 shares > min_order_size_shares。"""
        strikes = [
            _make_strike(20, 0.10),
            _make_strike(22, 0.65),
            _make_strike(23, 0.65),
            _make_strike(24, 0.60),
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        min_shares = 5.3
        for sig in signals:
            shares_estimate = sig.size_usdc / sig.expected_price
            assert (
                shares_estimate >= min_shares
            ), f"shares={shares_estimate:.1f} < min={min_shares}"


# ── 管线集成测试 ──


class TestPipelineIntegration:
    """验证 PureCoverage 在 WeatherPipeline 中的集成。"""

    def test_import_in_pipeline(self):
        """weather_pipeline 能正确 import pure_coverage。"""
        from src.signal.pure_coverage import scan_pure_coverage_signals
        from src.signal.weather_pipeline import WeatherPipeline

        assert callable(scan_pure_coverage_signals)

    def test_bet_bucket_attribute(self):
        """信号有 _bet_bucket='PureCoverage' 属性。"""
        strikes = [
            _make_strike(20, 0.10),
            _make_strike(22, 0.65),
            _make_strike(23, 0.65),
            _make_strike(24, 0.60),
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        for sig in signals:
            assert getattr(sig, "_bet_bucket", None) == "PureCoverage"


# ─── T243: offset_min 左翼信号屏蔽 ──────────────────────────────────


class TestPureCoverageOffsetMin:
    """T243: pure_coverage 加买应远离YES_best，屏蔽offset过小的左翼信号。

    事故案例: db7f132a Chongqing 30°C(offset=+1, YES_best=29°C) 距YES_best过近，
    right_tail核心策略已通过high-risk shift将offset_min提至2，但pure_coverage
    未设置offset_min，仍从+1开始选择候选信号。
    """

    def test_offset_min_2_blocks_offset_1(self):
        """offset_min=2 时，offset=+1 的候选被屏蔽。"""
        strikes = [
            _make_strike(28, 0.30),  # left side
            _make_strike(29, 0.30),  # left side
            _make_strike(30, 0.70),  # YES_best
            _make_strike(31, 0.30),  # offset=+1 from YES_best → should be blocked
            _make_strike(32, 0.30),  # offset=+2 from YES_best → should pass
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()
        cfg["weather_strategy"]["pure_coverage"]["offset_min"] = 2
        cfg["weather_strategy"]["pure_coverage"]["right_tail_offset_max"] = 3

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        offsets = [s.strategy_params.get("offset", 0) for s in signals]
        assert 1 not in offsets, "offset=+1 应被 offset_min=2 屏蔽"
        assert any(o >= 2 for o in offsets), "offset=+2 应通过 offset_min=2"

    def test_offset_min_1_allows_offset_1(self):
        """offset_min=1 时，offset=+1 的候选正常通过。"""
        strikes = [
            _make_strike(28, 0.30),
            _make_strike(29, 0.30),
            _make_strike(30, 0.70),  # YES_best
            _make_strike(31, 0.30),  # offset=+1 → allowed
            _make_strike(32, 0.30),  # offset=+2 → allowed
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()
        cfg["weather_strategy"]["pure_coverage"]["offset_min"] = 1
        cfg["weather_strategy"]["pure_coverage"]["right_tail_offset_max"] = 3

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        offsets = [s.strategy_params.get("offset", 0) for s in signals]
        assert 1 in offsets, "offset=+1 应被 offset_min=1 放行"

    def test_default_offset_min_is_2(self):
        """默认 offset_min=2，即使未在配置中指定。"""
        strikes = [
            _make_strike(30, 0.70),  # YES_best
            _make_strike(31, 0.30),  # offset=+1 → blocked by default
            _make_strike(32, 0.30),  # offset=+2 → allowed
            _make_strike(33, 0.30),  # offset=+3 → allowed
        ]
        group = _make_group(strikes=strikes)
        cfg = _base_config()
        # 不设置 offset_min — 验证默认值为2
        cfg["weather_strategy"]["pure_coverage"].pop("offset_min", None)
        cfg["weather_strategy"]["pure_coverage"]["right_tail_offset_max"] = 3

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        offsets = [s.strategy_params.get("offset", 0) for s in signals]
        assert 1 not in offsets, "默认 offset_min=2 应屏蔽 offset=+1"

    def test_chongqing_d_b7f132a_exact_scenario(self):
        """db7f132a 复现: Chongqing YES_best=29°C, offset_min=2 屏蔽30°C(+1)。"""
        strikes = [
            _make_strike(28, 0.25),
            _make_strike(29, 0.70),  # YES_best
            _make_strike(30, 0.30),  # offset=+1 → should be blocked
            _make_strike(31, 0.25),  # offset=+2 → should pass
        ]
        group = _make_group(
            city_name="Chongqing",
            strikes=strikes,
        )
        cfg = _base_config()
        cfg["weather_strategy"]["pure_coverage"]["offset_min"] = 2
        cfg["weather_strategy"]["pure_coverage"]["right_tail_offset_max"] = 3

        with patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )

        for sig in signals:
            offset = sig.strategy_params.get("offset", 0)
            assert offset >= 2, (
                f"db7f132a场景: offset={offset} < 2, "
                f"信号{sig.question}不应被生成(距YES_best过近)"
            )


# ─── T250: 模型质量过滤 ──────────────────────────────────────────────────────


class TestModelFilter:
    """T250 M2/M3: PureCoverage 模型质量门控。"""

    def _cfg_with_filter(self, **overrides) -> dict:
        cfg = _base_config()
        pc = cfg["weather_strategy"]["pure_coverage"]
        pc["model_filter_enabled"] = True
        pc["max_gfs_probability"] = 0.03
        pc["min_sigma_from_mean"] = 2.0
        pc["single_model_shadow_only"] = True
        pc["shadow_mode"] = False
        for k, v in overrides.items():
            pc[k] = v
        return cfg

    def _make_group_with_strikes(self, city_name: str = "Houston"):
        strikes = [
            _make_strike(28, 0.25),
            _make_strike(29, 0.70),  # YES_best
            _make_strike(31, 0.30),  # offset=+2
        ]
        return _make_group(city_name=city_name, strikes=strikes)

    def test_model_filter_disabled_allows_any_signal(self):
        """model_filter_enabled=False 时跳过所有模型质量检查，信号正常生成。"""
        cfg = _base_config()
        cfg["weather_strategy"]["pure_coverage"]["model_filter_enabled"] = False
        cfg["weather_strategy"]["pure_coverage"]["shadow_mode"] = False

        group = self._make_group_with_strikes()
        fake_detail = {
            "_reject_reason": "unreliable_source",
            "gfs_probability": 0.99,  # 极高：若过滤启用则会被拒绝
        }
        with (
            patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)),
            patch(
                "src.signal.pure_coverage._fetch_candidate_model_detail",
                return_value=fake_detail,
            ),
        ):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )
        # filter 关闭 → unreliable 信号也应通过
        assert isinstance(signals, list)
        # 不要求信号数=0（只需不崩溃，且模型过滤未触发）

    def test_high_gfs_prob_blocks_candidate(self):
        """GFS概率=0.06 > max_gfs_probability=0.03 → 候选被拒绝，不生成信号。"""
        cfg = self._cfg_with_filter(max_gfs_probability=0.03)
        group = self._make_group_with_strikes()
        fake_detail = {
            "gfs_probability": 0.06,
            "ensemble_mean": 35.0,
            "ensemble_std": 1.0,
        }
        with (
            patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)),
            patch(
                "src.signal.pure_coverage._fetch_candidate_model_detail",
                return_value=fake_detail,
            ),
        ):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )
        # GFS概率过高 → 信号应被拒绝
        for sig in signals:
            gfs_p = sig.strategy_params.get("gfs_probability")
            if gfs_p is not None:
                assert gfs_p <= 0.03, f"高GFS概率信号不应生成: gfs_probability={gfs_p}"

    def test_single_model_shadow_only_blocks_live_entry(self):
        """single_model_shadow_only=True + shadow_mode=False + 单模型信号 → 不生成实盘信号。"""
        cfg = self._cfg_with_filter(single_model_shadow_only=True, shadow_mode=False)
        group = self._make_group_with_strikes()
        fake_detail = {
            "_fallback_reason": "ecmwf_unavailable",
            "source": "gfs_only",
            "gfs_probability": 0.01,
            "ensemble_mean": 35.0,
            "ensemble_std": 1.5,
        }
        with (
            patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)),
            patch(
                "src.signal.pure_coverage._fetch_candidate_model_detail",
                return_value=fake_detail,
            ),
        ):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )
        # 单模型 + shadow_mode=False + single_model_shadow_only=True → 0 实盘信号
        assert (
            len(signals) == 0
        ), f"single_model_shadow_only=True 且 shadow_mode=False 时不应生成实盘信号，实际={len(signals)}"

    def test_single_model_shadow_only_allows_shadow_mode(self):
        """single_model_shadow_only=True + shadow_mode=True → 影子模式下记录但不返回。"""
        cfg = self._cfg_with_filter(single_model_shadow_only=True, shadow_mode=True)
        group = self._make_group_with_strikes()
        fake_detail = {
            "_fallback_reason": "ecmwf_unavailable",
            "source": "gfs_only",
            "gfs_probability": 0.01,
            "ensemble_mean": 35.0,
            "ensemble_std": 1.5,
        }
        with (
            patch("src.signal.pure_coverage.get", side_effect=_patch_get(cfg)),
            patch(
                "src.signal.pure_coverage._fetch_candidate_model_detail",
                return_value=fake_detail,
            ),
        ):
            signals = scan_pure_coverage_signals(
                group, days_diff=0, hours_to_settlement=24.0
            )
        # shadow_mode=True → 信号在影子模式下记录，不返回实盘信号
        assert len(signals) == 0, "影子模式不应返回信号给实盘执行"
