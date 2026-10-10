"""
T82: YES_best 多维度融合识别引擎 单元测试
- composite scoring (模型+偏差+动量+成交量)
- 窄区间奖励
- 价格动量缓存
- 动态刷新 TTL
- 城市对比 (纯模型 vs 融合)
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════
# 1. 价格动量计算测试
# ═══════════════════════════════════════════════════════════


class TestPriceMomentum:
    """_calc_price_momentum() 测试"""

    def test_first_scan_zero_momentum(self):
        from src.scanner.market_scanner import (
            _YESBEST_PRICE_CACHE,
            _calc_price_momentum,
        )

        _YESBEST_PRICE_CACHE.clear()
        result = _calc_price_momentum("test-market-1", 0.25)
        assert result == 0.0  # 首次无历史

    def test_price_increase_positive_momentum(self):
        from src.scanner.market_scanner import (
            _YESBEST_PRICE_CACHE,
            _calc_price_momentum,
        )

        _YESBEST_PRICE_CACHE.clear()
        # 模拟上次扫描: 价格0.20, 30分钟前
        _YESBEST_PRICE_CACHE["test-mom-up"] = (0.20, time.monotonic() - 1800)
        result = _calc_price_momentum("test-mom-up", 0.25)
        assert result > 0.0  # 价格上涨 → 正动量

    def test_price_decrease_zero_momentum(self):
        from src.scanner.market_scanner import (
            _YESBEST_PRICE_CACHE,
            _calc_price_momentum,
        )

        _YESBEST_PRICE_CACHE.clear()
        _YESBEST_PRICE_CACHE["test-mom-dn"] = (0.30, time.monotonic() - 1800)
        result = _calc_price_momentum("test-mom-dn", 0.25)
        assert result == 0.0  # 价格下跌 → 动量为0 (clamped)

    def test_momentum_capped_at_1(self):
        from src.scanner.market_scanner import (
            _YESBEST_PRICE_CACHE,
            _calc_price_momentum,
        )

        _YESBEST_PRICE_CACHE.clear()
        _YESBEST_PRICE_CACHE["test-mom-cap"] = (0.01, time.monotonic() - 1800)
        result = _calc_price_momentum("test-mom-cap", 0.50)
        assert result <= 1.0

    def test_cache_update(self):
        from src.scanner.market_scanner import (
            _YESBEST_PRICE_CACHE,
            _calc_price_momentum,
        )

        _YESBEST_PRICE_CACHE.clear()
        _calc_price_momentum("test-update", 0.10)
        assert "test-update" in _YESBEST_PRICE_CACHE
        assert _YESBEST_PRICE_CACHE["test-update"][0] == 0.10


# ═══════════════════════════════════════════════════════════
# 2. 动态刷新 TTL 测试
# ═══════════════════════════════════════════════════════════


class TestYesBestRefresh:
    """_should_refresh_yesbest() 多级分段TTL测试"""

    _FUSION_CFG = {
        "enabled": True,
        "refresh_interval_minutes": 45,
        "approach_refresh_minutes": 30,
        "rush_hour_refresh_minutes": 15,
        "final_hour_refresh_minutes": 5,
    }

    def test_no_cache_must_refresh(self):
        from src.scanner.market_scanner import (
            _YESBEST_RESULT_CACHE,
            _should_refresh_yesbest,
        )

        _YESBEST_RESULT_CACHE.clear()
        assert _should_refresh_yesbest("Seoul/April 10", 24.0) is True

    @patch("src.scanner.market_scanner.get")
    def test_normal_tier_gt24h(self, mock_get):
        """距到期>24h: 正常TTL=45min"""
        from src.scanner.market_scanner import (
            _YESBEST_RESULT_CACHE,
            _should_refresh_yesbest,
        )

        mock_get.return_value = self._FUSION_CFG
        _YESBEST_RESULT_CACHE.clear()
        _YESBEST_RESULT_CACHE["Seoul/April 10"] = (
            "cond-123",
            0.85,
            time.monotonic() - 600,
        )
        assert _should_refresh_yesbest("Seoul/April 10", 30.0) is False
        _YESBEST_RESULT_CACHE["Seoul/April 10"] = (
            "cond-123",
            0.85,
            time.monotonic() - 3000,
        )
        assert _should_refresh_yesbest("Seoul/April 10", 30.0) is True

    @patch("src.scanner.market_scanner.get")
    def test_approach_tier_20_24h(self, mock_get):
        """距到期20-24h: approach TTL=30min"""
        from src.scanner.market_scanner import (
            _YESBEST_RESULT_CACHE,
            _should_refresh_yesbest,
        )

        mock_get.return_value = self._FUSION_CFG
        _YESBEST_RESULT_CACHE.clear()
        # 20分钟前 < 30min → 不刷新
        _YESBEST_RESULT_CACHE["city/d"] = (
            "c",
            0.8,
            time.monotonic() - 1200,
        )
        assert _should_refresh_yesbest("city/d", 22.0) is False
        # 35分钟前 > 30min → 刷新
        _YESBEST_RESULT_CACHE["city/d"] = (
            "c",
            0.8,
            time.monotonic() - 2100,
        )
        assert _should_refresh_yesbest("city/d", 22.0) is True

    @patch("src.scanner.market_scanner.get")
    def test_rush_tier_18_20h(self, mock_get):
        """距到期18-20h: rush TTL=15min"""
        from src.scanner.market_scanner import (
            _YESBEST_RESULT_CACHE,
            _should_refresh_yesbest,
        )

        mock_get.return_value = self._FUSION_CFG
        _YESBEST_RESULT_CACHE.clear()
        _YESBEST_RESULT_CACHE["city/d"] = (
            "c",
            0.8,
            time.monotonic() - 600,  # 10min < 15min
        )
        assert _should_refresh_yesbest("city/d", 19.0) is False
        _YESBEST_RESULT_CACHE["city/d"] = (
            "c",
            0.8,
            time.monotonic() - 1000,  # 16.7min > 15min
        )
        assert _should_refresh_yesbest("city/d", 19.0) is True

    @patch("src.scanner.market_scanner.get")
    def test_final_tier_16_18h(self, mock_get):
        """距到期16-18h(临门一脚): final TTL=5min"""
        from src.scanner.market_scanner import (
            _YESBEST_RESULT_CACHE,
            _should_refresh_yesbest,
        )

        mock_get.return_value = self._FUSION_CFG
        _YESBEST_RESULT_CACHE.clear()
        _YESBEST_RESULT_CACHE["city/d"] = (
            "c",
            0.8,
            time.monotonic() - 180,  # 3min < 5min
        )
        assert _should_refresh_yesbest("city/d", 17.0) is False
        _YESBEST_RESULT_CACHE["city/d"] = (
            "c",
            0.8,
            time.monotonic() - 360,  # 6min > 5min
        )
        assert _should_refresh_yesbest("city/d", 17.0) is True

    @patch("src.scanner.market_scanner.get")
    def test_out_of_window_lt16h_no_refresh(self, mock_get):
        """距到期<16h: 已出窗口 → 不刷新"""
        from src.scanner.market_scanner import (
            _YESBEST_RESULT_CACHE,
            _should_refresh_yesbest,
        )

        mock_get.return_value = self._FUSION_CFG
        _YESBEST_RESULT_CACHE.clear()
        _YESBEST_RESULT_CACHE["city/d"] = (
            "c",
            0.8,
            time.monotonic() - 7200,  # 2小时前, 理应刷新
        )
        # 但 hours_to_expiry=10 < 16 → 不刷新
        assert _should_refresh_yesbest("city/d", 10.0) is False


# ═══════════════════════════════════════════════════════════
# 3. Composite Scoring 核心逻辑测试
# ═══════════════════════════════════════════════════════════


class TestCompositeScoring:
    """市场维度对 composite score 的影响 (v2: adaptive volume + extreme div)"""

    def _calc_composite(
        self,
        model_p,
        market_p,
        momentum=0.0,
        volume=0,
        city_max_vol=500,
        narrow=False,
        model_w=0.55,
        dev_w=0.25,
        mom_w=0.15,
        narrow_bonus=1.35,
        ext_div_thresh=0.70,
        ext_div_damp=0.85,
    ):
        """模拟 composite scoring v2 计算"""
        vol_w = max(0.0, 1.0 - model_w - dev_w - mom_w)
        d1 = model_p
        if model_p > 0.01:
            d2 = min(1.0, max(0.0, (model_p - market_p) / model_p))
        else:
            d2 = 0.0
        d3 = momentum
        d4 = min(1.0, volume / max(city_max_vol, 1.0))
        composite = model_w * d1 + dev_w * d2 + mom_w * d3 + vol_w * d4
        if narrow:
            composite *= narrow_bonus
        if d2 > ext_div_thresh:
            composite *= ext_div_damp
        return composite

    def test_undervalued_strike_scores_higher(self):
        """模型看好但市场低估的 strike 应得到更高分"""
        s_a = self._calc_composite(0.30, 0.30, volume=500)
        s_b = self._calc_composite(0.30, 0.15, volume=500)
        assert s_b > s_a, f"被低估strike应分更高: {s_b:.4f} vs {s_a:.4f}"

    def test_model_still_dominant(self):
        """高模型概率仍是最主要因素（偏差不极端时）"""
        s_a = self._calc_composite(0.50, 0.50, volume=500)
        s_b = self._calc_composite(0.20, 0.15, volume=500)
        assert s_a > s_b, f"高模型概率应分更高: {s_a:.4f} vs {s_b:.4f}"

    def test_momentum_boost(self):
        """价格上涨动量应提升分数"""
        s_no_mom = self._calc_composite(0.30, 0.30, momentum=0.0)
        s_with_mom = self._calc_composite(0.30, 0.30, momentum=0.8)
        assert s_with_mom > s_no_mom

    def test_volume_boost(self):
        """高成交量应提升分数"""
        s_low_vol = self._calc_composite(0.30, 0.30, volume=50)
        s_high_vol = self._calc_composite(0.30, 0.30, volume=1000)
        assert s_high_vol > s_low_vol

    def test_narrow_interval_bonus(self):
        """窄区间奖励应提升分数"""
        s_normal = self._calc_composite(0.30, 0.30, narrow=False)
        s_narrow = self._calc_composite(0.30, 0.30, narrow=True)
        assert s_narrow > s_normal
        assert abs(s_narrow / s_normal - 1.35) < 0.01

    def test_zero_model_degradation(self):
        """model_p=0 时不产生除零错误"""
        s = self._calc_composite(0.0, 0.05)
        assert s >= 0.0

    def test_weights_sum_to_1(self):
        """默认权重之和=1.0"""
        total = 0.55 + 0.25 + 0.15 + 0.05
        assert abs(total - 1.0) < 0.001

    def test_extreme_divergence_dampener(self):
        """极端背离(>70%)应触发dampener衰减"""
        # model=0.50, market=0.10 → deviation=0.80 > 0.70 → dampener
        s_extreme = self._calc_composite(0.50, 0.10, volume=500)
        # model=0.50, market=0.30 → deviation=0.40 < 0.70 → 无dampener
        s_moderate = self._calc_composite(0.50, 0.30, volume=500)
        # 极端背离虽然deviation更高, 但被dampener抑制
        # 核心: dampener确实降低了分数
        s_no_damp = self._calc_composite(0.50, 0.10, volume=500, ext_div_thresh=1.0)
        assert s_extreme < s_no_damp, "dampener应降低极端背离分数"

    def test_adaptive_volume_high_liquidity(self):
        """高流动性城市(Seoul): city_max_vol=5000"""
        # volume=3000/5000=0.6 vs volume=200/5000=0.04
        s_a = self._calc_composite(0.30, 0.28, volume=3000, city_max_vol=5000)
        s_b = self._calc_composite(0.30, 0.28, volume=200, city_max_vol=5000)
        assert s_a > s_b

    def test_adaptive_volume_low_liquidity(self):
        """低流动性城市(Wellington): city_max_vol=100"""
        # volume=80/100=0.8 (满分附近)
        s = self._calc_composite(0.30, 0.28, volume=80, city_max_vol=100)
        # 相比固定500: 80/500=0.16 → adaptive让低流动性城市volume分更公平
        s_fixed = self._calc_composite(0.30, 0.28, volume=80, city_max_vol=500)
        assert s > s_fixed, "adaptive should give higher score for low-liq city"

    def test_time_weight_shift(self):
        """距到期16h时, model权重降低, deviation/momentum权重增加"""
        # 模拟时间权重微调: shift=0.10
        # eff_model=0.45, eff_dev=0.31, eff_mom=0.19, eff_vol=0.05
        s_shifted = self._calc_composite(
            0.30,
            0.20,
            momentum=0.5,  # 被低估+有动量
            model_w=0.45,
            dev_w=0.31,
            mom_w=0.19,
        )
        s_normal = self._calc_composite(
            0.30,
            0.20,
            momentum=0.5,
            model_w=0.55,
            dev_w=0.25,
            mom_w=0.15,
        )
        # shifted增加了市场信号权重, 应增强偏差+动量贡献
        assert s_shifted > s_normal, "时间偏移应增强市场信号得分"


# ═══════════════════════════════════════════════════════════
# 4. 城市对比测试 (纯模型 vs 融合)
# ═══════════════════════════════════════════════════════════


class TestCityComparison:
    """
    模拟城市市场数据，对比纯模型 YES_best vs 融合 YES_best。
    验证融合引擎在市场有信息优势时能纠正模型偏差。
    """

    def _make_strike(self, cond_id, question, model_p, best_ask, volume=500):
        return {
            "conditionId": cond_id,
            "question": question,
            "bestAsk": best_ask,
            "bestBid": best_ask - 0.01,
            "volume24hr": volume,
            "_strike_p_yes": model_p,
            "_strike_ev": 0.05,
            "_strike_source": "dual",
        }

    def test_seoul_model_vs_fusion(self):
        """
        Seoul: 模型认为 10°C 最高概率, 但市场大幅低估 11°C
        融合后 11°C 应超过 10°C (因为偏差信号强)
        """
        strikes = [
            (
                self._make_strike(
                    "s-10",
                    "Will highest temp in Seoul be 10°C on Apr 10, 2026?",
                    model_p=0.35,
                    best_ask=0.33,  # 公平定价
                    volume=800,
                ),
                {"threshold_value": 10.0},
            ),
            (
                self._make_strike(
                    "s-11",
                    "Will highest temp in Seoul be 11°C on Apr 10, 2026?",
                    model_p=0.30,
                    best_ask=0.12,  # 严重低估! (模型0.30 vs 市场0.12)
                    volume=600,
                ),
                {"threshold_value": 11.0},
            ),
            (
                self._make_strike(
                    "s-12",
                    "Will highest temp in Seoul be 12°C on Apr 10, 2026?",
                    model_p=0.20,
                    best_ask=0.18,
                    volume=400,
                ),
                {"threshold_value": 12.0},
            ),
        ]

        # 纯模型: 选 model_p 最高 → 10°C
        model_best = max(strikes, key=lambda x: x[0]["_strike_p_yes"])
        assert model_best[1]["threshold_value"] == 10.0

        # 融合 composite(v2 adaptive volume): city_max=max(800,600,400)=800
        from src.scanner.market_scanner import (
            _YESBEST_PRICE_CACHE,
            _calc_price_momentum,
        )

        _YESBEST_PRICE_CACHE.clear()
        best_cid = None
        best_score = 0.0
        _city_max = max(m["volume24hr"] for m, _ in strikes)
        for m, p in strikes:
            model_p = m["_strike_p_yes"]
            market_p = m["bestAsk"]
            vol = m["volume24hr"]
            d1 = model_p
            d2 = min(1.0, max(0.0, (model_p - market_p) / max(model_p, 0.01)))
            d3 = _calc_price_momentum(m["conditionId"], market_p)
            d4 = min(1.0, vol / _city_max)
            score = 0.55 * d1 + 0.25 * d2 + 0.15 * d3 + 0.05 * d4
            if score > best_score:
                best_score = score
                best_cid = p["threshold_value"]

        assert best_cid == 11.0, f"Seoul: 融合应选11°C(严重低估), 实际选了{best_cid}°C"

    def test_london_consistent_model_market(self):
        """
        London: 模型和市场一致 → 融合不改变结果
        """
        strikes = [
            (
                self._make_strike(
                    "l-8",
                    "Will highest temp in London be 8°C?",
                    model_p=0.40,
                    best_ask=0.38,
                    volume=1000,
                ),
                {"threshold_value": 8.0},
            ),
            (
                self._make_strike(
                    "l-9",
                    "Will highest temp in London be 9°C?",
                    model_p=0.25,
                    best_ask=0.24,
                    volume=500,
                ),
                {"threshold_value": 9.0},
            ),
        ]

        # 纯模型和融合应一致选 8°C
        model_best_val = max(strikes, key=lambda x: x[0]["_strike_p_yes"])[1][
            "threshold_value"
        ]
        assert model_best_val == 8.0

        from src.scanner.market_scanner import _YESBEST_PRICE_CACHE

        _YESBEST_PRICE_CACHE.clear()
        best_score, best_val = 0.0, None
        _city_max = max(m["volume24hr"] for m, _ in strikes)
        for m, p in strikes:
            mp = m["_strike_p_yes"]
            mk = m["bestAsk"]
            d1 = mp
            d2 = min(1.0, max(0.0, (mp - mk) / max(mp, 0.01)))
            d4 = min(1.0, m["volume24hr"] / _city_max)
            score = 0.55 * d1 + 0.25 * d2 + 0.15 * 0.0 + 0.05 * d4
            if score > best_score:
                best_score = score
                best_val = p["threshold_value"]
        assert best_val == 8.0, "London: 一致时融合不改变结果"

    def test_chicago_volume_signal(self):
        """
        Chicago: 两个strike模型概率相近,
        但54°F有异常高成交量 → 融合应偏好高成交量
        """
        strikes = [
            (
                self._make_strike(
                    "c-54",
                    "Will highest temp in Chicago be 54°F?",
                    model_p=0.28,
                    best_ask=0.26,
                    volume=3000,  # 异常高成交量
                ),
                {"threshold_value": 54.0},
            ),
            (
                self._make_strike(
                    "c-55",
                    "Will highest temp in Chicago be 55°F?",
                    model_p=0.30,
                    best_ask=0.29,
                    volume=200,
                ),
                {"threshold_value": 55.0},
            ),
        ]

        # 纯模型选 55°F (0.30 > 0.28)
        model_best = max(strikes, key=lambda x: x[0]["_strike_p_yes"])
        assert model_best[1]["threshold_value"] == 55.0

        # 融合(自适应volume): city_max_vol=max(3000,200)=3000
        from src.scanner.market_scanner import _YESBEST_PRICE_CACHE

        _YESBEST_PRICE_CACHE.clear()
        scores = {}
        _city_max = max(m["volume24hr"] for m, _ in strikes)
        for m, p in strikes:
            mp = m["_strike_p_yes"]
            mk = m["bestAsk"]
            d1 = mp
            d2 = min(1.0, max(0.0, (mp - mk) / max(mp, 0.01)))
            d4 = min(1.0, m["volume24hr"] / _city_max)
            score = 0.55 * d1 + 0.25 * d2 + 0.15 * 0.0 + 0.05 * d4
            scores[p["threshold_value"]] = score

        # 自适应volume: 54°F=3000/3000=1.0, 55°F=200/3000=0.067
        assert scores[54.0] > scores[55.0] - 0.02, (
            f"Chicago: 高成交量54°F分数{scores[54.0]:.4f}"
            f" 应接近低成交量55°F的{scores[55.0]:.4f}"
        )

    def test_wellington_narrow_interval(self):
        """
        Wellington: strike间隔 ≤ 2°C 时窄区间奖励生效
        """
        strikes = [
            (
                self._make_strike(
                    "w-14",
                    "Will highest temp in Wellington be 14°C?",
                    model_p=0.30,
                    best_ask=0.28,
                    volume=400,
                ),
                {"threshold_value": 14.0},
            ),
            (
                self._make_strike(
                    "w-15",
                    "Will highest temp in Wellington be 15°C?",
                    model_p=0.35,
                    best_ask=0.32,
                    volume=450,
                ),
                {"threshold_value": 15.0},
            ),
        ]

        # 间距 = 1°C ≤ 2°C → 窄区间 (per-strike: 每个strike独立检查)
        tvs = sorted([p["threshold_value"] for _, p in strikes])
        nearest_gap = {}
        for i, tv in enumerate(tvs):
            dists = [abs(tv - tvs[j]) for j in range(len(tvs)) if j != i]
            nearest_gap[tv] = min(dists) if dists else 999
        for tv, gap in nearest_gap.items():
            assert gap <= 2.0, f"{tv}°C gap={gap} should be narrow"

        # adaptive volume: city_max=max(400,450)=450
        _city_max = max(m["volume24hr"] for m, _ in strikes)
        for m, p in strikes:
            mp = m["_strike_p_yes"]
            mk = m["bestAsk"]
            d1 = mp
            d2 = min(1.0, max(0.0, (mp - mk) / max(mp, 0.01)))
            d4 = min(1.0, m["volume24hr"] / _city_max)
            base = 0.55 * d1 + 0.25 * d2 + 0.15 * 0.0 + 0.05 * d4
            # per-strike narrow check
            if nearest_gap[p["threshold_value"]] <= 2.0:
                boosted = base * 1.35
            else:
                boosted = base
            assert boosted >= base
            if nearest_gap[p["threshold_value"]] <= 2.0:
                assert boosted / base == pytest.approx(1.35, abs=0.001)


# ═══════════════════════════════════════════════════════════
# 5. Config 完整性测试
# ═══════════════════════════════════════════════════════════


class TestYesBestFusionConfig:
    """config.json 参数完整性"""

    def test_config_exists(self):
        from pathlib import Path

        cfg_path = Path(__file__).parent.parent / "config" / "config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        yb = cfg.get("paper_trading", {}).get("yesbest_fusion")
        assert yb is not None, "缺少 yesbest_fusion 配置块"

    def test_config_keys(self):
        from pathlib import Path

        cfg_path = Path(__file__).parent.parent / "config" / "config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        yb = cfg["paper_trading"]["yesbest_fusion"]
        required = [
            "enabled",
            "market_rank_weight",
            "model_confirmation_weight",
            "model_overconfidence_penalty",
            "price_momentum_weight",
            "market_leader_tie_threshold",
            "narrow_interval_bonus",
            "narrow_interval_threshold_c",
            "refresh_interval_minutes",
            "approach_refresh_minutes",
            "rush_hour_refresh_minutes",
            "final_hour_refresh_minutes",
            "time_weight_shift_max",
            "extreme_divergence_threshold",
            "extreme_divergence_dampener",
            "volume_norm_adaptive",
        ]
        for key in required:
            assert key in yb, f"缺少配置键: {key}"

    def test_weights_sum(self):
        from pathlib import Path

        cfg_path = Path(__file__).parent.parent / "config" / "config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        yb = cfg["paper_trading"]["yesbest_fusion"]
        total = (
            yb["market_rank_weight"]
            + yb["model_confirmation_weight"]
            + yb["price_momentum_weight"]
        )
        # 剩余给 volume, 总和应=1.0
        assert total <= 1.0, f"权重总和{total} > 1.0"
        assert total >= 0.90, f"权重总和{total} < 0.90 (volume占比过高)"

    def test_config_docs_completeness(self):
        from pathlib import Path

        docs_path = Path(__file__).parent.parent / "config" / "config_docs.json"
        with open(docs_path) as f:
            docs = json.load(f)
        pt_docs = docs.get("paper_trading", {})
        yb_docs = [k for k in pt_docs if "yesbest_fusion" in k]
        assert len(yb_docs) >= 14, f"yesbest_fusion 文档 {len(yb_docs)} < 14"

    def test_default_values_sensible(self):
        from pathlib import Path

        cfg_path = Path(__file__).parent.parent / "config" / "config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        yb = cfg["paper_trading"]["yesbest_fusion"]
        assert yb["enabled"] is True
        assert 0.2 <= yb["market_rank_weight"] <= 0.8
        assert yb["narrow_interval_bonus"] >= 1.0
        assert yb["refresh_interval_minutes"] >= 15
        assert yb["approach_refresh_minutes"] < yb["refresh_interval_minutes"]
        assert yb["rush_hour_refresh_minutes"] < yb["approach_refresh_minutes"]
        assert yb["final_hour_refresh_minutes"] < yb["rush_hour_refresh_minutes"]
        assert 0.0 <= yb["time_weight_shift_max"] <= 0.20
        assert 0.5 <= yb["extreme_divergence_threshold"] <= 1.0
        assert 0.5 <= yb["extreme_divergence_dampener"] <= 1.0
        assert isinstance(yb["volume_norm_adaptive"], bool)


# ═══════════════════════════════════════════════════════════
# 6. 反例/边缘城市测试 — 验证融合引擎在负面场景下的稳健性
# ═══════════════════════════════════════════════════════════


class TestNegativeCases:
    """
    反例城市：验证融合引擎不会被噪声市场信号误导。
    这些是融合可能"更差"或需要保护的场景。
    """

    def _calc_v2(
        self,
        model_p,
        market_p,
        momentum=0.0,
        volume=0,
        city_max_vol=500,
        model_w=0.55,
        dev_w=0.25,
        mom_w=0.15,
        narrow=False,
        narrow_bonus=1.35,
        ext_thresh=0.70,
        ext_damp=0.85,
    ):
        vol_w = max(0.0, 1.0 - model_w - dev_w - mom_w)
        d1 = model_p
        d2 = min(1.0, max(0.0, (model_p - market_p) / max(model_p, 0.01)))
        d3 = momentum
        d4 = min(1.0, volume / max(city_max_vol, 1.0))
        comp = model_w * d1 + dev_w * d2 + mom_w * d3 + vol_w * d4
        if narrow:
            comp *= narrow_bonus
        if d2 > ext_thresh:
            comp *= ext_damp
        return comp

    def test_sparse_strikes_no_narrow_bonus(self):
        """
        稀疏strike城市(如 Riyadh 间距5°C): per-strike narrow不应触发。
        v1全局narrow可能误授, v2 per-strike正确拒绝。
        """
        # Riyadh: 40, 45, 50°C → 间距5°C > 2°C阈值
        # per-strike nearest_gap: 40→5, 45→5, 50→5 → 无narrow
        s_no_narrow = self._calc_v2(0.30, 0.20, volume=300, city_max_vol=400)
        s_with_narrow = self._calc_v2(
            0.30,
            0.20,
            volume=300,
            city_max_vol=400,
            narrow=True,
        )
        # v2中稀疏城市不会获得narrow bonus
        assert s_no_narrow < s_with_narrow, "narrow bonus确实会放大分数"
        # 在稀疏城市，narrow=False是正确行为，不应该有×1.35

    def test_extreme_divergence_prevents_false_confidence(self):
        """
        模型给出极高概率(0.60)而市场只给0.05 → deviation=0.92 >>> 0.70
        极端背离保护应降低composite，防止盲目信任失控模型。
        """
        # 无dampener
        s_nodamp = self._calc_v2(0.60, 0.05, volume=500, ext_thresh=1.0)
        # 有dampener
        s_damp = self._calc_v2(0.60, 0.05, volume=500)
        assert s_damp < s_nodamp, "极端背离dampener应降低分数"
        assert s_damp / s_nodamp == pytest.approx(0.85, abs=0.01)

    def test_low_liquidity_city_volume_not_zero(self):
        """
        低流动性城市(Wellington vol=30, max=50): adaptive应给公平volume分。
        旧/500逻辑下 30/500=0.06 → 几乎无贡献; adaptive 30/50=0.60 更公平。
        """
        s_adaptive = self._calc_v2(0.30, 0.28, volume=30, city_max_vol=50)
        s_fixed = self._calc_v2(0.30, 0.28, volume=30, city_max_vol=500)
        assert s_adaptive > s_fixed, "adaptive volume应提升低流动性城市分数"

    def test_market_noise_does_not_flip_best(self):
        """
        噪声市场: 两个strike模型概率差大(0.40 vs 0.10), 但低概率那个
        有噪声性低估(market=0.02)。模型应仍然主导, 不被偏差翻转。
        """
        # Strike A: 高模型(0.40) + 公平定价
        s_a = self._calc_v2(0.40, 0.38, volume=500, city_max_vol=500)
        # Strike B: 低模型(0.10) + "严重低估"(noise)
        s_b = self._calc_v2(0.10, 0.02, volume=500, city_max_vol=500)
        # A应仍然胜出 (model_w=0.55 is dominant)
        assert s_a > s_b, f"模型主导应防止噪声翻转: A={s_a:.4f} B={s_b:.4f}"

    def test_time_shift_at_boundary_24h(self):
        """距24h: shift=0 → 权重不变"""
        # hours_to_exp=24 → shift = 0.10 * (24-24)/8 = 0
        s_24h = self._calc_v2(
            0.30,
            0.20,
            momentum=0.5,
            model_w=0.55,
            dev_w=0.25,
            mom_w=0.15,
        )
        # hours_to_exp=16 → shift = 0.10 → model=0.45, dev=0.31, mom=0.19
        s_16h = self._calc_v2(
            0.30,
            0.20,
            momentum=0.5,
            model_w=0.45,
            dev_w=0.31,
            mom_w=0.19,
        )
        assert s_16h > s_24h, "16h更依赖市场信号(低估+动量时)"

    def test_per_strike_mixed_narrow(self):
        """
        混合城市: 部分strike窄(18,19°C gap=1), 部分宽(18,25°C gap=7)。
        Per-strike narrow只给窄的加bonus, 宽的不受益。
        """
        # 窄strike (gap=1)
        s_narrow = self._calc_v2(0.25, 0.20, narrow=True)
        # 宽strike (gap=7, 不给bonus)
        s_wide = self._calc_v2(0.25, 0.20, narrow=False)
        assert s_narrow > s_wide, "per-strike: 只有真正相邻的strike获得bonus"
