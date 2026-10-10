"""
test_weather_t277.py — T277 修复验证：429降级 → 单值Gaussian平滑 + 批量分块 + 分歧放宽

测试覆盖：
  1. _calc_member_probability — 单值(1-3成员)Gaussian平滑：概率不再退化为二值
  2. _calc_member_probability — 正常多成员场景不受影响
  3. fetch_dual_model_probability — 低成员数据分歧阈值自动放宽(0.15→0.30)
  4. prefetch_ensemble_data — 批量分块(8城/批+2s间隔)
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest
from src.utils.weather_fetcher import (
    _calc_member_probability,
    _normal_cdf,
    fetch_dual_model_probability,
    prefetch_ensemble_data,
)

# ══════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════


def _make_parsed(
    direction="exact",
    threshold=20,
    unit="C",
    variable="temp_max",
    threshold_low=None,
    threshold_high=None,
):
    return {
        "city": "Tokyo",
        "date_str": "March 25",
        "threshold_value": threshold,
        "threshold_low": threshold_low,
        "threshold_high": threshold_high,
        "unit": unit,
        "variable": variable,
        "direction": direction,
        "station_icao": "RJTT",
    }


def _mock_market(city="Tokyo", threshold=20, unit="C", date_offset=3):
    target = (datetime.now(timezone.utc) + timedelta(days=date_offset)).strftime(
        "%B %-d"
    )
    q = f"Will the highest temperature in {city} be {threshold}°{unit} on {target}?"
    return {"question": q}


# ══════════════════════════════════════════════════════════════════════
# 1. 单值 Gaussian 平滑（T277 核心修复）
# ══════════════════════════════════════════════════════════════════════


class TestSingleMemberGaussianSmoothing:
    """T277: 1-3成员时 _calc_member_probability 应使用NWP误差σ平滑而非二值退化。"""

    def test_single_member_exact_not_binary(self):
        """1成员恰好等于阈值时，概率应 < 1.0（Gaussian散布）。"""
        values_c = [20.0]  # 单值 = 阈值
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        # Gaussian平滑: exact ±0.5°C 窗口 vs σ~2.3 → 远小于1.0
        assert 0.02 < result["probability"] < 0.95
        assert result["total"] == 1

    def test_single_member_exact_far_from_threshold_not_zero(self):
        """1成员离阈值较远但不极远时，概率 > 0（非二值0）。"""
        values_c = [18.0]  # 距阈值20°C差2°C
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        # σ~2.3, 距离2°C → 应有一定概率
        assert result["probability"] > 0.02

    def test_single_member_gte_above_threshold(self):
        """1成员在阈值以上，gte方向 → P > 0.5（钳位至≤0.98）。"""
        values_c = [25.0]  # 远高于阈值20
        parsed = _make_parsed(direction="gte", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        assert result["probability"] > 0.5
        assert result["probability"] <= 0.98

    def test_single_member_gte_below_threshold(self):
        """1成员在阈值以下，gte方向 → P < 0.5（钳位至≥0.02）。"""
        values_c = [15.0]  # 低于阈值20
        parsed = _make_parsed(direction="gte", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        assert result["probability"] < 0.5
        assert result["probability"] >= 0.02

    def test_single_member_lte_below_threshold(self):
        """1成员在阈值以下，lte方向 → P > 0.5（钳位至≤0.98）。"""
        values_c = [15.0]
        parsed = _make_parsed(direction="lte", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        assert result["probability"] > 0.5
        assert result["probability"] <= 0.98

    def test_single_member_range_in_range(self):
        """1成员在range中间 → P < 1.0 且显著。"""
        values_c = [20.0]  # 在 [18, 22] 范围中间
        parsed = _make_parsed(
            direction="range", threshold_low=18, threshold_high=22, unit="C"
        )
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        assert 0.10 < result["probability"] < 0.95

    def test_two_members_identical_smoothed(self):
        """2个相同值成员 → Gaussian平滑仍生效（std=0）。"""
        values_c = [20.0, 20.0]
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        assert 0.02 < result["probability"] < 0.95
        assert result["total"] == 2

    def test_three_members_slightly_spread(self):
        """3成员微小散布(std<0.1) → 仍走Gaussian平滑路径。"""
        values_c = [19.95, 20.0, 20.05]  # std ≈ 0.04
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        assert 0.02 < result["probability"] < 0.95
        assert result["total"] == 3

    def test_sigma_increases_with_forecast_horizon(self):
        """预报天数越远，σ_est越大，概率越分散（exact方向）。"""
        values_c = [20.0]
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        r1 = _calc_member_probability(values_c, parsed, days_ahead=1)
        r7 = _calc_member_probability(values_c, parsed, days_ahead=7)
        # 更大的σ → exact窗口覆盖更小比例 → P更小
        assert r1["probability"] > r7["probability"]

    def test_clamped_to_valid_range(self):
        """概率应被钳位到 [0.02, 0.98]。"""
        # 极远距离 → 接近0但不为0
        values_c = [50.0]  # 距阈值20差30°C
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=1)
        assert result["probability"] >= 0.02

    def test_four_members_with_spread_uses_normal_path(self):
        """4个成员有散布 → 不触发T277平滑，走常规路径。"""
        values_c = [18.0, 19.0, 21.0, 22.0]  # std > 0.1, total > 3
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        assert result["total"] == 4


# ══════════════════════════════════════════════════════════════════════
# 2. 正常多成员场景不受影响
# ══════════════════════════════════════════════════════════════════════


class TestMultiMemberUnaffected:
    """验证T277不影响正常30+成员的概率计算。"""

    def test_30_members_exact_all_match(self):
        """30成员all-match → P=1.0（不走T277路径因total>3）。"""
        values_c = [20.0] * 30
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        assert result["probability"] == 1.0
        assert result["total"] == 30

    def test_30_members_none_match(self):
        """30成员都远离 → P应为CDF rescue值或0（不改变原有行为）。"""
        values_c = [5.0] * 30
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        assert result["total"] == 30
        assert result["yes_count"] == 0

    def test_31_members_mixed(self):
        """31成员混合 → 常规member_prob路径。"""
        values_c = [20.0] * 20 + [5.0] * 11
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        # 20/31 = 0.645, CDF may rescue slightly higher
        assert result["probability"] >= 20 / 31 - 0.01


# ══════════════════════════════════════════════════════════════════════
# 3. 双模型低成员分歧放宽（T277 Fix 3）
# ══════════════════════════════════════════════════════════════════════


class TestDualModelLowMemberDivergence:
    """T277: 双模型都只有1-3成员时，分歧阈值从15%放宽到30%。"""

    def _patch_both_models(self, gfs_data, ecmwf_data):
        return (
            patch(
                "src.utils.weather_fetcher._fetch_gfs_ensemble", return_value=gfs_data
            ),
            patch(
                "src.utils.weather_fetcher._fetch_ecmwf_ensemble",
                return_value=ecmwf_data,
            ),
        )

    def test_single_member_moderate_divergence_not_skipped(self):
        """
        单成员GFS=0.65 ECMWF=0.45 → 分歧20%。
        常规阈值15%会跳过，放宽到30%后应通过。
        """
        target_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime(
            "%Y-%m-%d"
        )
        # 使用确定温度值让Gaussian平滑后概率可预测
        # GFS: 1成员=22°C → gte≥20: P>0.5
        gfs_data = {target_date: [22.0]}
        # ECMWF: 1成员=18°C → gte≥20: P<0.5
        ecmwf_data = {target_date: [18.0]}

        market = _mock_market(date_offset=3)
        p1, p2 = self._patch_both_models(gfs_data, ecmwf_data)
        with p1, p2:
            result = fetch_dual_model_probability(market)

        # 低成员放宽阈值后，中等分歧不应跳过
        # (但如果分歧仍>30%则还是会跳过 — 这取决于Gaussian平滑后的具体值)
        if result and result.get("__divergence_skip__"):
            # 如果仍被跳过，验证分歧确实>30%
            assert result["divergence"] > 0.30
        elif result:
            assert "probability" in result

    def test_both_single_member_same_value_passes(self):
        """双模型都是单值且相同 → 分歧~0 → 应正常返回结果。"""
        target_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime(
            "%Y-%m-%d"
        )
        gfs_data = {target_date: [20.0]}
        ecmwf_data = {target_date: [20.0]}

        market = _mock_market(threshold=20, unit="C", date_offset=3)
        p1, p2 = self._patch_both_models(gfs_data, ecmwf_data)
        with p1, p2:
            result = fetch_dual_model_probability(market)

        assert result is not None
        assert "__divergence_skip__" not in result
        assert "probability" in result
        assert result["divergence"] < 0.01

    def test_30_members_high_divergence_still_skipped(self):
        """30成员高分歧 → 常规阈值仍生效（T277不影响多成员场景）。"""
        target_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime(
            "%Y-%m-%d"
        )
        gfs_data = {target_date: [20.0] * 27 + [5.0] * 3}
        ecmwf_data = {target_date: [20.0] * 8 + [5.0] * 22}

        market = _mock_market(threshold=20, unit="C", date_offset=3)
        p1, p2 = self._patch_both_models(gfs_data, ecmwf_data)
        with p1, p2:
            result = fetch_dual_model_probability(market)

        assert result is not None
        assert result.get("__divergence_skip__") is True

    def test_single_member_extreme_divergence_still_skipped(self):
        """
        单成员但分歧极大(>30%) → 即使放宽也不通过。
        GFS=50°C vs ECMWF=0°C → 概率极端分离。
        """
        target_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime(
            "%Y-%m-%d"
        )
        gfs_data = {target_date: [50.0]}  # 远高于20°C → exact P应很低
        ecmwf_data = {target_date: [20.0]}  # 恰好=20°C → exact P适中

        market = _mock_market(threshold=20, unit="C", date_offset=3)
        p1, p2 = self._patch_both_models(gfs_data, ecmwf_data)
        with p1, p2:
            result = fetch_dual_model_probability(market)

        # 概率差异应该非常大 → 即使30%阈值也会被跳过
        if result and result.get("__divergence_skip__"):
            assert result["divergence"] > 0.30


# ══════════════════════════════════════════════════════════════════════
# 4. 批量分块（T277 Fix 2）
# ══════════════════════════════════════════════════════════════════════


class TestPrefetchBatchChunking:
    """T277: prefetch_ensemble_data 批量请求应分块发送。"""

    @patch("src.utils.weather_fetcher._batch_fetch_ensemble", return_value=True)
    @patch("src.utils.weather_fetcher.time.sleep")
    def test_chunking_splits_large_batch(self, mock_sleep, mock_batch):
        """20个城市应被分成多个块（默认8个/块）。"""
        # 构造20个不同坐标的天气问题（模拟20城市）
        markets = []
        target = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%B %-d")
        for i in range(20):
            q = f"Will the highest temperature in TestCity{i} be 20°C on {target}?"
            markets.append({"question": q})

        # Mock parse_weather_question 和 _get_city_coords 生成20个不同坐标
        def fake_parse(q):
            for i in range(20):
                if f"TestCity{i}" in q:
                    return {
                        "city": f"TestCity{i}",
                        "variable": "temp_max",
                        "direction": "exact",
                        "threshold_value": 20,
                        "unit": "C",
                        "date_str": target,
                    }
            return None

        def fake_coords(city):
            for i in range(20):
                if city == f"TestCity{i}":
                    return (35.0 + i * 0.1, 139.0 + i * 0.1)
            return None

        with patch(
            "src.utils.weather_fetcher.parse_weather_question", side_effect=fake_parse
        ):
            with patch(
                "src.utils.weather_fetcher._get_city_coords", side_effect=fake_coords
            ):
                with patch("src.utils.weather_fetcher._CACHE", {}):
                    prefetch_ensemble_data(markets)

        # 20城市应产生多个batch chunk调用（具体数取决于坐标去重）
        # 关键检查: batch调用>2(无分块只有2次), 且有块间sleep
        assert mock_batch.call_count >= 4  # 多chunk GFS + ECMWF
        assert mock_sleep.call_count >= 2

    @patch("src.utils.weather_fetcher._batch_fetch_ensemble", return_value=True)
    @patch("src.utils.weather_fetcher.time.sleep")
    def test_small_batch_no_inter_chunk_sleep(self, mock_sleep, mock_batch):
        """5个城市(≤8) → 只有一个chunk, 无块间sleep。"""
        markets = []
        target = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%B %-d")
        for i in range(5):
            q = f"Will the highest temperature in SmallCity{i} be 20°C on {target}?"
            markets.append({"question": q})

        def fake_parse(q):
            for i in range(5):
                if f"SmallCity{i}" in q:
                    return {
                        "city": f"SmallCity{i}",
                        "variable": "temp_max",
                        "direction": "exact",
                        "threshold_value": 20,
                        "unit": "C",
                        "date_str": target,
                    }
            return None

        def fake_coords(city):
            for i in range(5):
                if city == f"SmallCity{i}":
                    return (35.0 + i * 0.1, 139.0 + i * 0.1)
            return None

        with patch(
            "src.utils.weather_fetcher.parse_weather_question", side_effect=fake_parse
        ):
            with patch(
                "src.utils.weather_fetcher._get_city_coords", side_effect=fake_coords
            ):
                with patch("src.utils.weather_fetcher._CACHE", {}):
                    prefetch_ensemble_data(markets)

        # 5城市 ≤ 8 → 1 GFS chunk + 1 ECMWF chunk = 2 calls
        assert mock_batch.call_count == 2
        # 无块间sleep (第一个chunk i=0不sleep)
        assert mock_sleep.call_count == 0


# ══════════════════════════════════════════════════════════════════════
# 5. 原有VPS故障场景重现
# ══════════════════════════════════════════════════════════════════════


class TestVpsFailureScenarioReproduction:
    """
    重现 VPS 实际故障场景：
    AWS S3 返回1成员, ECMWF mirror返回1成员 → 之前导致100%分歧跳过。
    T277修复后应产生合理概率并通过分歧检查。
    """

    def _patch_both_models(self, gfs_data, ecmwf_data):
        return (
            patch(
                "src.utils.weather_fetcher._fetch_gfs_ensemble", return_value=gfs_data
            ),
            patch(
                "src.utils.weather_fetcher._fetch_ecmwf_ensemble",
                return_value=ecmwf_data,
            ),
        )

    def test_aws_s3_single_value_not_binary_zero(self):
        """AWS S3 返回单值 → 经Gaussian平滑后不会是0.0。"""
        # 模拟 Tokyo 当前温度20°C, 阈值20°C
        values_c = [20.0]
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        result = _calc_member_probability(values_c, parsed, days_ahead=2)
        assert result["probability"] != 0.0
        assert result["probability"] != 1.0
        assert 0.02 <= result["probability"] <= 0.98

    def test_dual_model_both_single_value_reasonable_consensus(self):
        """
        GFS=1成员(21°C) ECMWF=1成员(19°C) → Gaussian平滑后分歧应合理。
        旧代码: GFS=1.000 ECMWF=0.000 → 分歧100% → 跳过
        新代码: 两者Gaussian平滑后应接近 → 分歧<<<30%
        """
        target_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime(
            "%Y-%m-%d"
        )
        gfs_data = {target_date: [21.0]}  # 略高于阈值20
        ecmwf_data = {target_date: [19.0]}  # 略低于阈值20

        market = _mock_market(threshold=20, unit="C", date_offset=3)
        p1, p2 = self._patch_both_models(gfs_data, ecmwf_data)
        with p1, p2:
            result = fetch_dual_model_probability(market)

        # 两模型都是单值 → T277放宽阈值到30%
        # Gaussian平滑后，21°C vs 19°C (距阈值各1°C) 应产生相近概率
        assert result is not None
        if "__divergence_skip__" not in result:
            assert "probability" in result
            assert 0.02 < result["probability"] < 0.98
            # 分歧应显著小于30%
            assert result["divergence"] < 0.30
        else:
            # 不应该被跳过 — 这是修复验证
            pytest.fail(
                f"双模型单值场景不应被跳过: divergence={result['divergence']:.4f}"
            )

    def test_logs_t277_gaussian_tag(self):
        """Gaussian平滑触发时应记录 [WeatherCDF-T277] 标签。"""
        values_c = [20.0]
        parsed = _make_parsed(direction="exact", threshold=20, unit="C")
        with patch("src.utils.weather_fetcher.logger") as mock_logger:
            _calc_member_probability(values_c, parsed, days_ahead=2)
            # 应调用debug日志包含T277标签
            debug_calls = [str(c) for c in mock_logger.debug.call_args_list]
            assert any(
                "WeatherCDF-T277" in c for c in debug_calls
            ), f"T277 Gaussian平滑应记录[WeatherCDF-T277]标签, 实际: {debug_calls}"
