"""
test_signal_engine.py — SignalEngine 单元测试
覆盖：ensemble 归一化、EV 计算、仓位计算、generate_signals 过滤逻辑
"""

from unittest.mock import MagicMock, patch

import pytest
from src.signal.signal_engine import Signal, SignalEngine
from src.utils.config_loader import get

# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


def make_market(
    market_id: str = "mkt_001",
    question: str = "Will BTC exceed $100k?",
    best_ask: float = 0.45,
    best_bid: float = 0.43,
) -> dict:
    return {
        "id": market_id,
        "question": question,
        "bestAsk": best_ask,
        "bestBid": best_bid,
        "liquidity": 50000,
    }


def make_ai_response(
    outcome: str, probability: float, confidence: float = 0.75
) -> dict:
    return {
        "outcome": outcome,
        "probability": probability,
        "confidence": confidence,
        "reasoning": "test",
    }


def _calibration_mock(calibration_cfg):
    """Create a mock side_effect for get() that returns calibration_cfg for
    'signal.probability_calibration' and '' for 'signal.arbiter_model'.
    Bayesian update is disabled so calibration tests are isolated."""

    def _side_effect(key, *args, **kwargs):
        if key == "signal.probability_calibration":
            return calibration_cfg
        if key == "signal.arbiter_model":
            return ""
        if key == "signal.bayesian_update":
            return {"enabled": False}
        return None

    return _side_effect


# ---------------------------------------------------------------------------
# _ensemble_vote 归一化测试
# ---------------------------------------------------------------------------


class TestEnsembleVote:
    def setup_method(self):
        self.engine = SignalEngine.__new__(SignalEngine)
        self.engine._evolver = None
        # 为旧测试禁用概率校准，专用测试类 TestProbabilityCalibration 覆盖校准逻辑
        self._cal_patcher = patch(
            "src.signal.signal_engine.get",
            side_effect=lambda key: (
                {"enabled": False}
                if key == "signal.probability_calibration"
                else get(key)
            ),
        )
        self._cal_patcher.start()

    def teardown_method(self):
        self._cal_patcher.stop()

    def test_yes_outcome_high_prob(self):
        votes = {
            "gpt-4o": make_ai_response("YES", 0.70),
            "claude": make_ai_response("YES", 0.65),
        }
        outcome, ev, conf, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48
        )
        assert outcome == "YES"
        assert ev > 0
        assert 0.0 < conf <= 1.0

    def test_no_outcome_normalization(self):
        """模型声明 outcome=NO 且 probability > 0.5 时，应取反归一化为 P(YES) < 0.5。"""
        votes = {
            "gpt-4o": make_ai_response("NO", 0.70)
        }  # 说明 P(NO)=0.70 → P(YES)=0.30
        outcome, ev, conf, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48
        )
        # P(YES)=0.30 < best_ask=0.50 → 应买 NO
        assert outcome == "NO"

    def test_yes_outcome_low_prob_normalization(self):
        """模型声明 outcome=YES 但 probability < 0.5 时，应取反归一化。"""
        votes = {"gpt-4o": make_ai_response("YES", 0.30)}  # 异常：声明 YES 但给了 0.30
        outcome, ev, conf, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.60, best_bid=0.58
        )
        # 取反后 P(YES)=0.70 > best_ask=0.60 → 买 YES
        assert outcome == "YES"
        assert ev > 0

    def test_no_ev_when_neutral(self):
        """avg_prob ≈ best_ask 时 EV 接近 0。"""
        votes = {"gpt-4o": make_ai_response("YES", 0.50)}
        outcome, ev, conf, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48
        )
        assert abs(ev) < 0.05

    def test_multi_model_average(self):
        """多模型同方向时结果偏向YES，EV显著正。（Bayesian更新后非简单平均）"""
        votes = {
            "gpt-4o": make_ai_response("YES", 0.70),
            "claude": make_ai_response("YES", 0.80),
        }
        outcome, ev, conf, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48
        )
        # Bayesian更新：两模型同方向 → posterior > market → YES
        assert outcome == "YES"
        assert ev > 0.15  # 明显正EV

    def test_low_prob_yes_should_not_buy_yes(self):
        """P(YES)=12.5% 不应买入 YES，即使 AI 概率略高于市场价。
        回归测试：Taiwan invasion case — 防止低概率投机。
        """
        votes = {
            "model-a": make_ai_response("NO", 0.88),  # P(NO)=0.88 → P(YES)=0.12
            "model-b": make_ai_response("NO", 0.87),  # P(NO)=0.87 → P(YES)=0.13
        }
        outcome, ev, conf, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.106, best_bid=0.096
        )
        # avg P(YES) ≈ 0.125 < 0.5 → 方向必须是 NO
        assert outcome == "NO", "低P(YES)不应投注YES方向"

    def test_direction_follows_majority_not_ev(self):
        """方向判定应跟随 AI 多数判断（0.5分界），而非哪个方向 EV 更高。"""
        # outcome=NO, prob=0.60 → 归一化 P(YES) = 0.40 < 0.5 → 必须选 NO
        # 旧逻辑：P(YES)=0.40 > best_ask=0.30 → 会错误选 YES
        votes = {"model": make_ai_response("NO", 0.60)}
        outcome, ev, conf, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.30, best_bid=0.28
        )
        assert outcome == "NO"


# ---------------------------------------------------------------------------
# _calc_size 仓位计算测试
# ---------------------------------------------------------------------------


class TestCalcSize:
    def setup_method(self):
        self.engine = SignalEngine.__new__(SignalEngine)
        self.engine._evolver = None

    def test_size_within_max_pct(self):
        # T38: Standard Kelly — p_true=0.65, entry_price=0.50 → positive edge
        size = self.engine._calc_size(balance=1000.0, p_true=0.65, entry_price=0.50)
        max_pct = get("risk.max_single_bet_pct") or 0.10
        max_allowed = 1000.0 * max_pct
        assert size <= max_allowed

    def test_minimum_size_is_one_usdc(self):
        # 极小 edge → Kelly 返回最小注额 1 USDC
        size = self.engine._calc_size(balance=100.0, p_true=0.51, entry_price=0.50)
        assert size >= 1.0

    def test_size_scales_with_balance(self):
        _real_get = get

        def _no_fixed(key, *a, **kw):
            if key == "paper_trading.fixed_bet_size_usdc":
                return None
            return _real_get(key, *a, **kw)

        with patch("src.signal.signal_engine.get", side_effect=_no_fixed):
            size_small = self.engine._calc_size(
                balance=100.0, p_true=0.70, entry_price=0.50
            )
            size_large = self.engine._calc_size(
                balance=10000.0, p_true=0.70, entry_price=0.50
            )
        assert size_large > size_small
        assert size_large > size_small


# ---------------------------------------------------------------------------
# generate_signals 集成过滤测试（mock AI 调用）
# ---------------------------------------------------------------------------


class TestGenerateSignals:
    def setup_method(self):
        with patch("src.signal.signal_engine.OpenAI"):
            self.engine = SignalEngine()
        # T259: crypto require_smart_money pre-filter needs smart_money mock
        self.engine.smart_money.get_smart_context_for_market = lambda m: "mock_sm"

    def _mock_call_ai(self, outcome: str, probability: float, confidence: float):
        def _call(model, user_prompt, system_prompt=""):
            return make_ai_response(outcome, probability, confidence)

        return _call

    def test_signal_passes_ev_and_confidence_filter(self):
        pool = {
            "tier1": [make_market(best_ask=0.50, best_bid=0.48)],
            "tier2": [],
            "tier3": [],
        }
        # P(YES)=0.70, ev=0.20 → 通过
        with patch.object(
            self.engine, "_call_ai", side_effect=self._mock_call_ai("YES", 0.70, 0.80)
        ):
            signals = self.engine.generate_signals(pool, balance=100.0)
        assert len(signals) == 1
        assert not signals[0].skip

    def test_signal_rejected_low_ev(self):
        pool = {
            "tier1": [make_market(best_ask=0.70, best_bid=0.68)],
            "tier2": [],
            "tier3": [],
        }
        # P(YES)=0.693 < best_ask=0.70, Bayesian+Shrinkage后 EV < 0 → 过滤
        with patch.object(
            self.engine, "_call_ai", side_effect=self._mock_call_ai("YES", 0.693, 0.80)
        ):
            signals = self.engine.generate_signals(pool, balance=100.0)
        assert len(signals) == 0

    def test_signal_rejected_low_confidence(self):
        pool = {
            "tier1": [make_market(best_ask=0.50, best_bid=0.48)],
            "tier2": [],
            "tier3": [],
        }
        # confidence=0.15 < 全局0.30 且 < crypto赛道0.25 → 过滤
        with patch.object(
            self.engine, "_call_ai", side_effect=self._mock_call_ai("YES", 0.70, 0.15)
        ):
            signals = self.engine.generate_signals(pool, balance=100.0)
        assert len(signals) == 0

    def test_balance_parameter_used_for_sizing(self):
        pool = {
            "tier1": [make_market(best_ask=0.50, best_bid=0.48)],
            "tier2": [],
            "tier3": [],
        }
        _real_get = get

        def _no_fixed(key, *a, **kw):
            if key == "paper_trading.fixed_bet_size_usdc":
                return None
            return _real_get(key, *a, **kw)

        with patch("src.signal.signal_engine.get", side_effect=_no_fixed):
            with patch.object(
                self.engine,
                "_call_ai",
                side_effect=self._mock_call_ai("YES", 0.70, 0.80),
            ):
                signals_small = self.engine.generate_signals(pool, balance=100.0)
            with patch.object(
                self.engine,
                "_call_ai",
                side_effect=self._mock_call_ai("YES", 0.70, 0.80),
            ):
                signals_large = self.engine.generate_signals(pool, balance=10000.0)
        assert signals_large[0].size_usdc > signals_small[0].size_usdc

    def test_no_signals_when_ai_fails(self):
        pool = {"tier1": [make_market()], "tier2": [], "tier3": []}
        with patch.object(self.engine, "_call_ai", return_value=None):
            signals = self.engine.generate_signals(pool, balance=100.0)
        assert len(signals) == 0

    def test_fallback_model_used_when_all_free_models_fail(self):
        """所有免费模型返回 None 时，兜底模型被调用并生成信号"""
        pool = {
            "tier1": [make_market(best_ask=0.40, best_bid=0.38)],
            "tier2": [],
            "tier3": [],
        }
        fallback_response = make_ai_response("YES", 0.78, 0.80)

        call_count = {"n": 0}

        def mock_call_ai(model, *args, **kwargs):
            call_count["n"] += 1
            # 免费模型全部失败，兜底模型成功
            if model == "google/gemini-2.5-flash-lite":
                return fallback_response
            return None

        with patch("src.signal.signal_engine.get") as mock_get:

            def cfg(key):
                defaults = {
                    "signal.ensemble_models": [
                        "model-free-1:free",
                        "model-free-2:free",
                    ],
                    "signal.fallback_model": "google/gemini-2.5-flash-lite",
                    "signal.second_model_min_ev": 0.0,
                    "signal.min_ev_threshold": 0.03,
                    "signal.min_confidence": 0.60,
                    "signal.temperature": 0.2,
                    "signal.max_retries": 1,
                    "signal.max_tier1_markets": 5,
                    "signal.max_tier2_markets": 3,
                    "signal.max_tier3_markets": 0,
                    "risk.max_single_bet_pct": 0.03,
                    "executor.min_bet_usdc": 1.0,
                    "ai.default_model": "fallback",
                }
                return defaults.get(key)

            mock_get.side_effect = cfg
            with patch.object(self.engine, "_call_ai", side_effect=mock_call_ai):
                signals = self.engine.generate_signals(pool, balance=100.0)

        assert len(signals) == 1, "兜底模型应生成 1 个有效信号"
        assert "gemini" in list(signals[0].model_votes.keys())[0]

    def test_fallback_not_called_when_free_model_succeeds(self):
        """免费模型有响应时，兜底模型不应被调用"""
        pool = {
            "tier1": [make_market(best_ask=0.40, best_bid=0.38)],
            "tier2": [],
            "tier3": [],
        }
        free_response = make_ai_response("YES", 0.75, 0.80)
        fallback_called = {"flag": False}

        def mock_call_ai(model, *args, **kwargs):
            if model == "google/gemini-2.5-flash-lite":
                fallback_called["flag"] = True
                return make_ai_response("YES", 0.75, 0.80)
            return free_response  # 免费模型成功

        with patch("src.signal.signal_engine.get") as mock_get:

            def cfg(key):
                defaults = {
                    "signal.ensemble_models": ["model-free-1:free"],
                    "signal.fallback_model": "google/gemini-2.5-flash-lite",
                    "signal.second_model_min_ev": 0.0,
                    "signal.min_ev_threshold": 0.03,
                    "signal.min_confidence": 0.60,
                    "signal.temperature": 0.2,
                    "signal.max_retries": 1,
                    "signal.max_tier1_markets": 5,
                    "signal.max_tier2_markets": 3,
                    "signal.max_tier3_markets": 0,
                    "risk.max_single_bet_pct": 0.03,
                    "executor.min_bet_usdc": 1.0,
                    "ai.default_model": "fallback",
                }
                return defaults.get(key)

            mock_get.side_effect = cfg
            with patch.object(self.engine, "_call_ai", side_effect=mock_call_ai):
                signals = self.engine.generate_signals(pool, balance=100.0)

        assert not fallback_called["flag"], "免费模型成功时不应调用兜底模型"


# ---------------------------------------------------------------------------
# 体育赛道 prompt 选择测试
# ---------------------------------------------------------------------------


class TestSportsPromptSelection:
    def test_sports_prompt_constant_exists(self):
        """SPORTS_SYSTEM_PROMPT 常量应存在。"""
        from src.signal.signal_engine import SPORTS_SYSTEM_PROMPT

        assert isinstance(SPORTS_SYSTEM_PROMPT, str)
        assert len(SPORTS_SYSTEM_PROMPT) > 50

    def test_sports_prompt_contains_key_terms(self):
        """体育 prompt 应包含关键体育分析术语。"""
        from src.signal.signal_engine import SPORTS_SYSTEM_PROMPT

        prompt_lower = SPORTS_SYSTEM_PROMPT.lower()
        assert "team" in prompt_lower or "sport" in prompt_lower
        assert "probability" in prompt_lower


# ---------------------------------------------------------------------------
# CostSave 天气/地缘豁免测试
# ---------------------------------------------------------------------------


class TestCostSaveExemption:
    """天气和地缘赛道应跳过 CostSave，始终执行 ensemble 双模型。"""

    def setup_method(self):
        with (
            patch("src.signal.signal_engine.get", return_value=None),
            patch("src.signal.signal_engine.OpenAI"),
        ):
            self.engine = SignalEngine()

    def test_weather_market_calls_both_models(self):
        """天气市场（GFS禁用时）即使首模型 EV < threshold，仍应调用第二模型。"""
        weather_market = {
            "id": "mkt-weather-costsave",
            "question": "Will the high temperature in London be 18°C tomorrow?",
            "bestAsk": 0.30,
            "bestBid": 0.28,
            "tags": [{"label": "Weather"}],
            "description": "Resolves YES if...",
        }
        # 首模型返回低 EV 信号
        low_ev_response = make_ai_response("YES", 0.32, 0.70)
        call_count = {"n": 0}

        def mock_call_ai(model, *args, **kwargs):
            call_count["n"] += 1
            return low_ev_response

        with patch("src.signal.signal_engine.get") as mock_get:

            def cfg(key):
                defaults = {
                    "signal.ensemble_models": ["model-a", "model-b"],
                    "signal.fallback_model": "",
                    "signal.second_model_min_ev": 0.05,
                    "signal.min_ev_threshold": 0.03,
                    "signal.min_confidence": 0.50,
                    "signal.temperature": 0.2,
                    "signal.max_retries": 1,
                    "risk.max_single_bet_pct": 0.03,
                    "executor.min_bet_usdc": 1.0,
                    "ai.default_model": "fallback",
                    "signal.pre_filter": {"enabled": False},
                    # T192: GFS禁用 → 天气市场走LLM路径，验证CostSave豁免
                    "weather_ensemble": {"enabled": False},
                }
                return defaults.get(key)

            mock_get.side_effect = cfg
            with patch.object(self.engine, "_call_ai", side_effect=mock_call_ai):
                self.engine._analyze_market(weather_market, balance=100.0)

        assert call_count["n"] == 2, (
            f"Weather market (GFS disabled) should call both models (CostSave exempt), "
            f"but called {call_count['n']}"
        )

    def test_crypto_market_skips_second_model_on_low_ev(self):
        """Phase 24: Crypto 市场现在豁免 CostSave（强制双模型 ensemble），应调用两个模型。"""
        crypto_market = {
            "id": "mkt-crypto-costsave",
            "question": "Will BTC exceed $150k by July 2026?",
            "bestAsk": 0.50,
            "bestBid": 0.48,
            "tags": [{"label": "Crypto"}],
            "description": "Resolves YES if...",
        }
        # 首模型返回低 EV 信号 (prob ≈ ask → EV ≈ 0)
        low_ev_response = make_ai_response("YES", 0.51, 0.60)
        call_count = {"n": 0}

        def mock_call_ai(model, *args, **kwargs):
            call_count["n"] += 1
            return low_ev_response

        with patch("src.signal.signal_engine.get") as mock_get:

            def cfg(key):
                defaults = {
                    "signal.ensemble_models": ["model-a", "model-b"],
                    "signal.fallback_model": "",
                    "signal.second_model_min_ev": 0.05,
                    "signal.min_ev_threshold": 0.03,
                    "signal.min_confidence": 0.50,
                    "signal.temperature": 0.2,
                    "signal.max_retries": 1,
                    "risk.max_single_bet_pct": 0.03,
                    "executor.min_bet_usdc": 1.0,
                    "ai.default_model": "fallback",
                    "signal.pre_filter": {"enabled": False},
                }
                return defaults.get(key)

            mock_get.side_effect = cfg
            with patch.object(self.engine, "_call_ai", side_effect=mock_call_ai):
                self.engine._analyze_market(crypto_market, balance=100.0)

        assert call_count["n"] == 2, (
            f"Crypto market should call both models (CostSave exempt since Phase 24), "
            f"but called {call_count['n']}"
        )


# ---------------------------------------------------------------------------
# AI 概率校准 (Probability Calibration) 测试
# ---------------------------------------------------------------------------


class TestProbabilityCalibration:
    """生产数据驱动修复：AI系统性高估概率，用shrinkage向市场价收缩。"""

    def setup_method(self):
        self.engine = SignalEngine.__new__(SignalEngine)
        self.engine._evolver = None

    @patch("src.signal.signal_engine.get")
    def test_calibration_reduces_overconfident_yes(self, mock_get):
        """AI概率0.80 + 市场0.50 + shrinkage=0.5 → 校准后0.65，EV从0.30降到0.15。"""
        mock_get.side_effect = _calibration_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.5,
                "category_shrinkage": {"weather": 0.5},
            }
        )
        votes = {"m1": make_ai_response("YES", 0.80, 0.75)}
        outcome, ev, conf, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="weather"
        )
        # calibrated = 0.49 + 0.5*(0.80-0.49) = 0.645
        assert outcome == "YES"
        assert ev < 0.20, f"EV should be reduced by calibration, got {ev}"

    @patch("src.signal.signal_engine.get")
    def test_calibration_disabled_no_change(self, mock_get):
        """校准关闭时，EV保持原值。"""
        mock_get.side_effect = _calibration_mock({"enabled": False})
        votes = {"m1": make_ai_response("YES", 0.80, 0.75)}
        outcome, ev, _, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="weather"
        )
        # Without calibration: ev = 0.80 - 0.50 = 0.30
        assert abs(ev - 0.30) < 0.01

    @patch("src.signal.signal_engine.get")
    def test_calibration_filters_marginal_bets(self, mock_get):
        """AI概率0.55 + 市场0.50 + shrinkage=0.5 → 校准后0.525，EV从0.05降到~0.025，可能被门槛过滤。"""
        mock_get.side_effect = _calibration_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.5,
                "category_shrinkage": {},
            }
        )
        votes = {"m1": make_ai_response("YES", 0.55, 0.60)}
        outcome, ev, _, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="tech"
        )
        # calibrated = 0.49 + 0.5*(0.55-0.49) = 0.52, ev = 0.52-0.50 = 0.02
        assert (
            ev < 0.05
        ), f"Marginal bet should have tiny EV after calibration, got {ev}"

    @patch("src.signal.signal_engine.get")
    def test_calibration_uses_category_specific_shrinkage(self, mock_get):
        """不同赛道使用不同shrinkage因子。"""
        mock_get.side_effect = _calibration_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.5,
                "category_shrinkage": {"sports": 0.3, "crypto": 0.7},
            }
        )
        votes = {"m1": make_ai_response("YES", 0.80, 0.75)}

        # Sports: shrinkage=0.3 → calibrated = 0.49+0.3*0.31 = 0.583
        _, ev_sports, _, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="sports"
        )
        # Crypto: shrinkage=0.7 → calibrated = 0.49+0.7*0.31 = 0.707
        _, ev_crypto, _, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="crypto"
        )
        assert ev_crypto > ev_sports, "Higher shrinkage should preserve more AI signal"

    @patch("src.signal.signal_engine.get")
    def test_calibration_no_direction_preserved(self, mock_get):
        """NO方向的校准也应正确工作。"""
        mock_get.side_effect = _calibration_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.5,
                "category_shrinkage": {},
            }
        )
        votes = {"m1": make_ai_response("NO", 0.80, 0.70)}
        # P(YES)=0.20 after normalization
        outcome, ev, _, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="geopolitics"
        )
        # Without calibration: P(YES)=0.20, outcome=NO, ev=0.80-0.52=0.28
        # With calibration: calibrated P(YES) = 0.49+0.5*(0.20-0.49) = 0.345
        # P(NO)=0.655, NO ask=1-0.48=0.52, ev=0.655-0.52=0.135
        assert outcome == "NO"
        assert ev < 0.28, f"NO direction EV should be reduced, got {ev}"

    @patch("src.signal.signal_engine.get")
    def test_backward_compat_no_category(self, mock_get):
        """不传category时使用全局默认shrinkage。"""
        mock_get.side_effect = _calibration_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.4,
                "category_shrinkage": {},
            }
        )
        votes = {"m1": make_ai_response("YES", 0.80, 0.75)}
        outcome, ev, _, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48
        )
        # Uses default 0.4: calibrated = 0.49+0.4*0.31 = 0.614, ev=0.114
        assert outcome == "YES"
        assert 0.05 < ev < 0.20

    @patch("src.signal.signal_engine.get")
    def test_probability_ceiling_clamps_high(self, mock_get):
        """概率超过 ceiling 时应被裁剪。"""
        mock_get.side_effect = _calibration_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.95,  # 几乎不收缩
                "category_shrinkage": {},
                "probability_ceiling": 0.92,
                "probability_floor": 0.08,
            }
        )
        votes = {"m1": make_ai_response("YES", 0.98, 0.90)}
        outcome, ev, _, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="weather"
        )
        # With shrinkage 0.95: calibrated ≈ 0.49 + 0.95*(0.98-0.49) = 0.9555
        # After ceiling clamp: capped to 0.92
        # EV = 0.92 - 0.50 = 0.42
        assert outcome == "YES"
        assert ev <= 0.92 - 0.50 + 0.01  # Allow small float tolerance

    @patch("src.signal.signal_engine.get")
    def test_probability_floor_clamps_low(self, mock_get):
        """概率低于 floor 时应被裁剪。"""
        mock_get.side_effect = _calibration_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.95,
                "category_shrinkage": {},
                "probability_ceiling": 0.92,
                "probability_floor": 0.08,
            }
        )
        votes = {"m1": make_ai_response("NO", 0.97, 0.85)}
        # P(YES) = 1-0.97 = 0.03 after normalization
        outcome, ev, _, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="crypto"
        )
        # calibrated P(YES) ≈ 0.49 + 0.95*(0.03-0.49) ≈ 0.053
        # After floor clamp: raised to 0.08
        # P(NO) = 0.92, NO ask = 1-0.48 = 0.52, ev = 0.92-0.52 = 0.40
        assert outcome == "NO"

    @patch("src.signal.signal_engine.get")
    def test_probability_within_range_no_clamp(self, mock_get):
        """概率在 ceiling/floor 范围内不应被裁剪。"""
        mock_get.side_effect = _calibration_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.75,
                "category_shrinkage": {},
                "probability_ceiling": 0.92,
                "probability_floor": 0.08,
            }
        )
        votes = {"m1": make_ai_response("YES", 0.80, 0.70)}
        outcome, ev_with_clamp, _, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="weather"
        )
        # calibrated = 0.49 + 0.75*(0.80-0.49) = 0.7225 → within [0.08, 0.92], no clamp
        assert outcome == "YES"
        # Should be same as without clamp
        mock_get.side_effect = _calibration_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.75,
                "category_shrinkage": {},
            }
        )
        _, ev_no_clamp, _, _avg_p = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="weather"
        )
        assert abs(ev_with_clamp - ev_no_clamp) < 0.001


# ---------------------------------------------------------------------------
# Phase 45 — Bayesian Discount 测试
# ---------------------------------------------------------------------------


def _bayesian_discount_mock(calibration_cfg, bayesian_enabled=True):
    """Mock that enables BOTH Bayesian update AND calibration for discount tests."""

    def _side_effect(key, *args, **kwargs):
        if key == "signal.probability_calibration":
            return calibration_cfg
        if key == "signal.arbiter_model":
            return ""
        if key == "signal.bayesian_update":
            return {"enabled": bayesian_enabled}
        return None

    return _side_effect


class TestBayesianDiscount:
    """Phase 45: Bayesian+Calibration 复合压缩折扣，减少双重衰减。"""

    def setup_method(self):
        self.engine = SignalEngine.__new__(SignalEngine)
        self.engine._evolver = None

    @patch("src.signal.signal_engine.get")
    def test_discount_reduces_compression(self, mock_get):
        """sports discount=0.5: effective shrinkage 0.60→0.80，EV 应比无折扣时高。"""
        # 无折扣 — Bayesian ON + shrinkage 0.6
        mock_get.side_effect = _bayesian_discount_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.6,
                "category_shrinkage": {"sports": 0.6},
            },
            bayesian_enabled=True,
        )
        votes = {"m1": make_ai_response("YES", 0.80, 0.75)}
        _, ev_no_discount, _, _ = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="sports"
        )

        # 有折扣 — sports discount=0.5 → effective shrinkage=1-(1-0.6)*0.5=0.80
        mock_get.side_effect = _bayesian_discount_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.6,
                "category_shrinkage": {"sports": 0.6},
                "bayesian_discount": 1.0,
                "bayesian_discount_by_category": {"sports": 0.5},
            },
            bayesian_enabled=True,
        )
        _, ev_with_discount, _, _ = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="sports"
        )

        assert (
            ev_with_discount > ev_no_discount
        ), f"Discount should increase EV: {ev_with_discount:.4f} > {ev_no_discount:.4f}"

    @patch("src.signal.signal_engine.get")
    def test_discount_1_0_no_effect(self, mock_get):
        """discount=1.0 (默认) 应无任何影响，向后兼容。"""
        mock_get.side_effect = _bayesian_discount_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.6,
                "category_shrinkage": {"sports": 0.6},
                "bayesian_discount": 1.0,
                "bayesian_discount_by_category": {},
            },
            bayesian_enabled=True,
        )
        votes = {"m1": make_ai_response("YES", 0.80, 0.75)}
        _, ev_d1, _, _ = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="sports"
        )

        # 无 discount 字段
        mock_get.side_effect = _bayesian_discount_mock(
            {
                "enabled": True,
                "shrinkage_factor": 0.6,
                "category_shrinkage": {"sports": 0.6},
            },
            bayesian_enabled=True,
        )
        _, ev_none, _, _ = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="sports"
        )

        assert (
            abs(ev_d1 - ev_none) < 0.001
        ), f"discount=1.0 should match no-discount: {ev_d1:.4f} vs {ev_none:.4f}"

    @patch("src.signal.signal_engine.get")
    def test_per_category_discount(self, mock_get):
        """不同赛道获得不同折扣：sports 0.5 vs crypto 0.7。"""
        cfg = {
            "enabled": True,
            "shrinkage_factor": 0.6,
            "category_shrinkage": {"sports": 0.6, "crypto": 0.55},
            "bayesian_discount": 1.0,
            "bayesian_discount_by_category": {"sports": 0.5, "crypto": 0.7},
        }
        mock_get.side_effect = _bayesian_discount_mock(cfg, bayesian_enabled=True)
        votes = {"m1": make_ai_response("YES", 0.80, 0.75)}

        # Sports: discount=0.5, shrinkage=0.6 → effective=1-(1-0.6)*0.5=0.80
        _, ev_sports, _, _ = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="sports"
        )
        # Crypto: discount=0.7, shrinkage=0.55 → effective=1-(1-0.55)*0.7=0.685
        _, ev_crypto, _, _ = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="crypto"
        )

        # Sports gets MORE discount (0.5<0.7) → higher effective shrinkage → MORE signal preserved → higher EV
        assert (
            ev_sports > ev_crypto
        ), f"Sports (deeper discount) should have higher EV: {ev_sports:.4f} vs {ev_crypto:.4f}"

    @patch("src.signal.signal_engine.get")
    def test_discount_not_applied_without_bayesian(self, mock_get):
        """Bayesian 关闭时，discount 不应影响结果。"""
        cfg_with_discount = {
            "enabled": True,
            "shrinkage_factor": 0.6,
            "category_shrinkage": {"sports": 0.6},
            "bayesian_discount_by_category": {"sports": 0.3},
        }
        # Bayesian OFF + has discount → discount should NOT apply
        mock_get.side_effect = _bayesian_discount_mock(
            cfg_with_discount, bayesian_enabled=False
        )
        votes = {"m1": make_ai_response("YES", 0.80, 0.75)}
        _, ev_off, _, _ = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="sports"
        )

        # Bayesian OFF + no discount → should be identical
        cfg_no_discount = {
            "enabled": True,
            "shrinkage_factor": 0.6,
            "category_shrinkage": {"sports": 0.6},
        }
        mock_get.side_effect = _bayesian_discount_mock(
            cfg_no_discount, bayesian_enabled=False
        )
        _, ev_base, _, _ = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="sports"
        )

        assert (
            abs(ev_off - ev_base) < 0.001
        ), f"Bayesian OFF: discount should not apply: {ev_off:.4f} vs {ev_base:.4f}"

    @patch("src.signal.signal_engine.get")
    def test_global_discount_fallback(self, mock_get):
        """无赛道专属折扣时，应使用全局 bayesian_discount。"""
        cfg = {
            "enabled": True,
            "shrinkage_factor": 0.6,
            "category_shrinkage": {},
            "bayesian_discount": 0.5,
            "bayesian_discount_by_category": {},
        }
        mock_get.side_effect = _bayesian_discount_mock(cfg, bayesian_enabled=True)
        votes = {"m1": make_ai_response("YES", 0.80, 0.75)}
        _, ev_global, _, _ = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="tech"
        )

        # 无任何折扣（全局=1.0）
        cfg_no = {
            "enabled": True,
            "shrinkage_factor": 0.6,
            "category_shrinkage": {},
            "bayesian_discount": 1.0,
        }
        mock_get.side_effect = _bayesian_discount_mock(cfg_no, bayesian_enabled=True)
        _, ev_none, _, _ = self.engine._ensemble_vote(
            votes, best_ask=0.50, best_bid=0.48, category="tech"
        )

        assert (
            ev_global > ev_none
        ), f"Global discount=0.5 should increase EV: {ev_global:.4f} > {ev_none:.4f}"


# ---------------------------------------------------------------------------
# Phase 20 — 天气 hint 隔离测试（修复 #BUG-weather-hint-leak）
# ---------------------------------------------------------------------------


class TestWeatherHintIsolation:
    """_build_user_prompt() 的天气问题类型提示必须仅在 weather_context 存在时注入。
    Phase 20 修复: 此 hint 曾泄露到所有赛道，导致 AI 为体育/Crypto 市场锚定
    10-25% 概率 → 系统持续买入 5¢ penny tokens 并止损。
    """

    def setup_method(self):
        with (
            patch("src.signal.signal_engine.get", return_value=None),
            patch("src.signal.signal_engine.OpenAI"),
        ):
            self.engine = SignalEngine()

    def test_sports_no_weather_hint(self):
        """体育市场 prompt 中不得出现天气专用提示。"""
        prompt = self.engine._build_user_prompt(
            question="Thunder vs. Wizards",
            description="If the Thunder win, market resolves to Thunder.",
            sports_context="★ Oklahoma City Thunder: 1W-0L",
        )
        assert "[EXACT question]" not in prompt
        assert "P(YES) typically 10-25%" not in prompt
        assert "[THRESHOLD question]" not in prompt
        assert "[RANGE question]" not in prompt

    def test_crypto_no_weather_hint(self):
        """Crypto 市场 prompt 中不得出现天气专用提示。"""
        prompt = self.engine._build_user_prompt(
            question="Will BTC be above $100,000 on March 25?",
            description="Resolves Yes if BTC > $100k.",
            crypto_context="BTC: $98,500 | 7d vol: 3.2%",
        )
        assert "[EXACT question]" not in prompt
        assert "P(YES) typically 10-25%" not in prompt

    def test_politics_no_weather_hint(self):
        """政治市场 prompt 中不得出现天气专用提示。"""
        prompt = self.engine._build_user_prompt(
            question="Will Israel and Hamas reach ceasefire?",
            description="Resolves Yes if ceasefire by March 30.",
            politics_context="GDELT: 150 articles, avg tone -3.5",
        )
        assert "[EXACT question]" not in prompt
        assert "P(YES) typically 10-25%" not in prompt

    def test_no_context_no_weather_hint(self):
        """无任何赛道数据时也不得注入天气提示。"""
        prompt = self.engine._build_user_prompt(
            question="Will event X happen?",
            description="Resolves Yes if X happens.",
        )
        assert "[EXACT question]" not in prompt
        assert "P(YES) typically 10-25%" not in prompt

    def test_weather_exact_hint_present(self):
        """天气精确型问题应注入 [EXACT question] 提示。"""
        prompt = self.engine._build_user_prompt(
            question="High temperature in NYC on March 22?",
            description="Resolves to nearest degree.",
            weather_context="GFS ensemble: mean=15°C, spread=2°C",
        )
        assert "[EXACT question]" in prompt
        assert "P(YES) typically 10-25%" in prompt

    def test_weather_threshold_hint_present(self):
        """天气阈值型问题应注入 [THRESHOLD question] 提示。"""
        prompt = self.engine._build_user_prompt(
            question="Will NYC high be 60°F or higher on March 22?",
            description="Resolves Yes if high >= 60°F.",
            weather_context="GFS ensemble: mean=62°F, spread=3°F",
        )
        assert "[THRESHOLD question]" in prompt
        assert "[EXACT question]" not in prompt

    def test_weather_range_hint_present(self):
        """天气区间型问题应注入 [RANGE question] 提示。"""
        prompt = self.engine._build_user_prompt(
            question="Will NYC high be between 55°F and 60°F?",
            description="Resolves Yes if 55 <= high <= 60.",
            weather_context="GFS ensemble: mean=57°F",
        )
        assert "[RANGE question]" in prompt
        assert "[EXACT question]" not in prompt

    def test_sports_prompt_still_has_analysis(self):
        """体育市场 prompt 应包含分析提示但不含天气 hint。"""
        prompt = self.engine._build_user_prompt(
            question="Thunder vs. Wizards",
            description="Thunder win = Thunder.",
            sports_context="★ Oklahoma City Thunder: 1W-0L",
        )
        assert "compare recent form" in prompt
        assert "Thunder vs. Wizards" in prompt
        # 确认 question 后面没有附带 hint（只有换行或直接跟 settlement rules）
        lines = prompt.split("\n")
        first_line = lines[0]
        assert first_line == "Market question: Thunder vs. Wizards"


# ---------------------------------------------------------------------------
# Phase 35: 候选轮换 + 价格预筛 测试
# ---------------------------------------------------------------------------


class TestQuickPriceScreen:
    """测试 _quick_price_screen: 极端价格市场应在 slot 选择前被移除。"""

    def setup_method(self):
        with patch("src.signal.signal_engine.OpenAI"):
            self.engine = SignalEngine()

    def test_extreme_low_price_rejected(self):
        """bestAsk=0.05 → 应被排除"""
        m = {"id": "1", "bestAsk": 0.05}
        assert self.engine._quick_price_screen(m) is True

    def test_extreme_high_price_rejected(self):
        """bestAsk=0.95 → 应被排除"""
        m = {"id": "2", "bestAsk": 0.95}
        assert self.engine._quick_price_screen(m) is True

    def test_normal_price_passes(self):
        """bestAsk=0.50 → 应保留"""
        m = {"id": "3", "bestAsk": 0.50}
        assert self.engine._quick_price_screen(m) is False

    def test_boundary_low_passes(self):
        """bestAsk=0.09 → 刚好超过0.08，应保留"""
        m = {"id": "4", "bestAsk": 0.09}
        assert self.engine._quick_price_screen(m) is False

    def test_boundary_high_passes(self):
        """bestAsk=0.91 → 刚好低于0.92，应保留"""
        m = {"id": "5", "bestAsk": 0.91}
        assert self.engine._quick_price_screen(m) is False

    def test_uses_outcome_prices_as_fallback(self):
        """无 bestAsk 时，使用 outcomePrices 判断"""
        m = {"id": "6", "outcomePrices": "[0.04, 0.96]"}
        assert self.engine._quick_price_screen(m) is True

    def test_no_price_info_passes(self):
        """既无 bestAsk 也无 outcomePrices → 保留（让后续 _pre_filter 处理）"""
        m = {"id": "7", "question": "test"}
        assert self.engine._quick_price_screen(m) is False


class TestCandidateRotation:
    """测试候选轮换机制：确保不同周期分析不同市场。"""

    def setup_method(self):
        with patch("src.signal.signal_engine.OpenAI"):
            self.engine = SignalEngine()

    def _make_tier1_pool(self, n: int, category: str = "weather") -> list[dict]:
        """生成 n 个模拟 Tier1 市场，带递减分数。"""
        markets = []
        for i in range(n):
            markets.append(
                {
                    "id": str(1000 + i),
                    "question": f"Weather Q{i}?",
                    "bestAsk": 0.40 + (i % 5) * 0.05,
                    "bestBid": 0.38 + (i % 5) * 0.05,
                    "_score": 100 - i,
                    "tags": [{"slug": "temperature"}],
                }
            )
        return markets

    @patch("src.signal.signal_engine.get")
    def test_rotation_produces_different_selections(self, mock_get):
        """连续两轮应产生不同的候选市场（因为 recently_analyzed 去重 + shuffle）。"""
        config_vals = {
            "signal.max_tier1_markets": 5,
            "signal.max_tier2_markets": 0,
            "signal.max_tier3_markets": 0,
            "signal.min_ev_threshold": 0.01,
            "signal.min_confidence": 0.3,
            "signal.category_min_markets": {"weather": 2, "default": 1},
            "signal.rotation_pool_multiplier": 3,
            "signal.rotation_memory_seconds": 1800,
            "signal.pre_filter": {"enabled": False},
            "paper_trading": {"category_max_per_event": {"default": 1}},
        }
        mock_get.side_effect = lambda key, *a, **kw: config_vals.get(key)

        pool = self._make_tier1_pool(30)
        candidate = {"tier1": pool, "tier2": [], "tier3": []}

        # Mock _analyze_market to skip AI but return a signal
        self.engine._analyze_market = MagicMock(
            side_effect=lambda m, b: Signal(
                market_id=m["id"],
                question=m["question"],
                outcome="YES",
                ev=0.10,
                confidence=0.80,
                size_usdc=1.0,
                token_id="tok1",
            )
        )
        self.engine._skip_saturated_events = lambda x: x

        # Run two cycles
        sigs1 = self.engine.generate_signals(candidate, balance=100)
        ids1 = {s.market_id for s in sigs1}

        sigs2 = self.engine.generate_signals(candidate, balance=100)
        ids2 = {s.market_id for s in sigs2}

        # With 30 candidates and 5 slots, rotation should produce some different markets
        # (Not necessarily ALL different due to randomness, but not identical)
        # We just verify that over 2 cycles, total unique markets > 5
        all_ids = ids1 | ids2
        assert len(all_ids) >= 6, (
            f"Rotation failed: 2 cycles only covered {len(all_ids)} unique markets "
            f"(expected >=6 from 30 candidates). ids1={ids1}, ids2={ids2}"
        )

    @patch("src.signal.signal_engine.get")
    def test_prescreen_removes_extreme_prices(self, mock_get):
        """极端价格市场应在选择前被移除，不浪费 AI 分析名额。"""
        config_vals = {
            "signal.max_tier1_markets": 5,
            "signal.max_tier2_markets": 0,
            "signal.max_tier3_markets": 0,
            "signal.min_ev_threshold": 0.01,
            "signal.min_confidence": 0.3,
            "signal.category_min_markets": {},
            "signal.rotation_pool_multiplier": 3,
            "signal.rotation_memory_seconds": 1800,
            "signal.pre_filter": {"enabled": False},
            "paper_trading": {"category_max_per_event": {"default": 1}},
        }
        mock_get.side_effect = lambda key, *a, **kw: config_vals.get(key)

        # Mix of normal and extreme-price markets
        pool = [
            {
                "id": "1",
                "question": "Q1?",
                "bestAsk": 0.50,
                "bestBid": 0.48,
                "_score": 90,
            },
            {
                "id": "2",
                "question": "Q2?",
                "bestAsk": 0.03,
                "bestBid": 0.02,
                "_score": 95,
            },  # extreme low
            {
                "id": "3",
                "question": "Q3?",
                "bestAsk": 0.96,
                "bestBid": 0.94,
                "_score": 92,
            },  # extreme high
            {
                "id": "4",
                "question": "Q4?",
                "bestAsk": 0.40,
                "bestBid": 0.38,
                "_score": 85,
            },
            {
                "id": "5",
                "question": "Q5?",
                "bestAsk": 0.60,
                "bestBid": 0.58,
                "_score": 80,
            },
        ]
        candidate = {"tier1": pool, "tier2": [], "tier3": []}

        analyzed_ids = []

        def mock_analyze(m, b):
            analyzed_ids.append(m["id"])
            return Signal(
                market_id=m["id"],
                question=m["question"],
                outcome="YES",
                ev=0.10,
                confidence=0.80,
                size_usdc=1.0,
                token_id="tok1",
            )

        self.engine._analyze_market = mock_analyze
        self.engine._skip_saturated_events = lambda x: x

        self.engine.generate_signals(candidate, balance=100)

        # Markets "2" (0.03) and "3" (0.96) should be pre-screened out
        assert (
            "2" not in analyzed_ids
        ), "Extreme low price market should be pre-screened"
        assert (
            "3" not in analyzed_ids
        ), "Extreme high price market should be pre-screened"
        # Normal markets should be analyzed
        assert "1" in analyzed_ids
        assert "4" in analyzed_ids

    @patch("src.signal.signal_engine.get")
    def test_recently_analyzed_tracking(self, mock_get):
        """分析后的市场应被记录到轮换记忆中。"""
        config_vals = {
            "signal.max_tier1_markets": 3,
            "signal.max_tier2_markets": 0,
            "signal.max_tier3_markets": 0,
            "signal.min_ev_threshold": 0.01,
            "signal.min_confidence": 0.3,
            "signal.category_min_markets": {},
            "signal.rotation_pool_multiplier": 3,
            "signal.rotation_memory_seconds": 1800,
            "signal.pre_filter": {"enabled": False},
            "paper_trading": {"category_max_per_event": {"default": 1}},
        }
        mock_get.side_effect = lambda key, *a, **kw: config_vals.get(key)

        pool = [
            {
                "id": "A",
                "question": "QA?",
                "bestAsk": 0.50,
                "bestBid": 0.48,
                "_score": 90,
            },
            {
                "id": "B",
                "question": "QB?",
                "bestAsk": 0.40,
                "bestBid": 0.38,
                "_score": 85,
            },
            {
                "id": "C",
                "question": "QC?",
                "bestAsk": 0.60,
                "bestBid": 0.58,
                "_score": 80,
            },
        ]
        candidate = {"tier1": pool, "tier2": [], "tier3": []}

        self.engine._analyze_market = MagicMock(
            side_effect=lambda m, b: Signal(
                market_id=m["id"],
                question=m["question"],
                outcome="YES",
                ev=0.05,
                confidence=0.60,
                size_usdc=1.0,
                token_id="tok1",
            )
        )
        self.engine._skip_saturated_events = lambda x: x

        assert len(self.engine._recently_analyzed) == 0
        self.engine.generate_signals(candidate, balance=100)
        # All 3 markets should be tracked
        assert len(self.engine._recently_analyzed) >= 3

    @patch("src.signal.signal_engine.get")
    def test_rotation_memory_expires(self, mock_get):
        """过期的轮换记忆应被清理。"""
        config_vals = {
            "signal.max_tier1_markets": 2,
            "signal.max_tier2_markets": 0,
            "signal.max_tier3_markets": 0,
            "signal.min_ev_threshold": 0.01,
            "signal.min_confidence": 0.3,
            "signal.category_min_markets": {},
            "signal.rotation_pool_multiplier": 3,
            "signal.rotation_memory_seconds": 60,  # 1 minute for test
            "signal.pre_filter": {"enabled": False},
            "paper_trading": {"category_max_per_event": {"default": 1}},
        }
        mock_get.side_effect = lambda key, *a, **kw: config_vals.get(key)

        import time as _time

        # Pre-populate with expired entries
        self.engine._recently_analyzed = {
            "old1": _time.time() - 120,  # 2 min ago, expired
            "old2": _time.time() - 120,
            "fresh": _time.time() - 10,  # 10 sec ago, still active
        }

        pool = [
            {
                "id": "X",
                "question": "QX?",
                "bestAsk": 0.50,
                "bestBid": 0.48,
                "_score": 90,
            }
        ]
        candidate = {"tier1": pool, "tier2": [], "tier3": []}

        self.engine._analyze_market = MagicMock(
            side_effect=lambda m, b: Signal(
                market_id=m["id"],
                question=m["question"],
                outcome="YES",
                ev=0.05,
                confidence=0.60,
                size_usdc=1.0,
                token_id="tok1",
            )
        )
        self.engine._skip_saturated_events = lambda x: x

        self.engine.generate_signals(candidate, balance=100)

        # old1 and old2 should be expired (cleaned), fresh should remain
        assert "old1" not in self.engine._recently_analyzed
        assert "old2" not in self.engine._recently_analyzed
        assert "fresh" in self.engine._recently_analyzed


# ---------------------------------------------------------------------------
# YES/NO 队伍映射 Prompt 注入测试
# ---------------------------------------------------------------------------


class TestSportsYesNoMapping:
    """验证 "Team A vs Team B" 型体育市场 Prompt 中注入 YES/NO 映射。"""

    def setup_method(self):
        self.engine = SignalEngine.__new__(SignalEngine)
        self.engine._evolver = None

    def test_vs_market_injects_mapping(self):
        """'Grizzlies vs. Hawks' 应注入 YES=Grizzlies, NO=Hawks。"""
        prompt = self.engine._build_user_prompt(
            question="Grizzlies vs. Hawks",
            sports_context="★ Memphis Grizzlies (NBA)\n★ Atlanta Hawks (NBA)",
        )
        assert "YES = Grizzlies wins" in prompt
        assert "NO  = Hawks wins" in prompt
        assert "P(Grizzlies wins)" in prompt

    def test_vs_dot_market_injects_mapping(self):
        """'Lakers vs. Celtics' (with dot) 应也注入映射。"""
        prompt = self.engine._build_user_prompt(
            question="Lakers vs. Celtics",
            sports_context="★ Lakers (NBA)\n★ Celtics (NBA)",
        )
        assert "YES = Lakers wins" in prompt
        assert "NO  = Celtics wins" in prompt

    def test_non_vs_market_no_mapping(self):
        """非 vs 格式的问题不应注入映射。"""
        prompt = self.engine._build_user_prompt(
            question="Will the Lakers win the NBA Finals?",
            sports_context="★ Lakers (NBA)",
        )
        assert "CRITICAL — YES/NO Definition" not in prompt

    def test_no_sports_context_no_mapping(self):
        """非体育市场（无 sports_context）不应注入映射。"""
        prompt = self.engine._build_user_prompt(
            question="BTC vs ETH price race",
            crypto_context="Bitcoin: $100000",
        )
        assert "CRITICAL — YES/NO Definition" not in prompt

    def test_mapping_includes_example(self):
        """映射应包含反例说明（防止 AI 误解）。"""
        prompt = self.engine._build_user_prompt(
            question="Grizzlies vs Hawks",
            sports_context="★ data",
        )
        assert "probability should be 0.20" in prompt


# ---------------------------------------------------------------------------
# OddsPapi 交叉验证测试
# ---------------------------------------------------------------------------


class TestOddsCrossValidation:
    """验证 _cross_validate_odds 能检测 AI 与博彩公司方向冲突。"""

    def setup_method(self):
        self.engine = SignalEngine.__new__(SignalEngine)
        self.engine._evolver = None

    @patch("src.signal.signal_engine.fetch_odds_implied_prob")
    def test_directional_conflict_rejected(self, mock_odds):
        """AI 说 YES(Grizzlies 胜) 但博彩公司说 NO(Hawks 胜) → 拒绝。"""
        mock_odds.return_value = {
            "participant1": "Atlanta Hawks",
            "participant2": "Memphis Grizzlies",
            "participant1_prob": 0.857,
            "participant2_prob": 0.143,
        }
        skip_reason = self.engine._cross_validate_odds(
            market_id="1627600",
            question="Grizzlies vs. Hawks",
            outcome="YES",
            avg_prob=0.815,  # AI says Grizzlies 81.5%
        )
        assert skip_reason != "", "方向冲突应被拒绝"
        assert "方向与博彩公司赔率冲突" in skip_reason

    @patch("src.signal.signal_engine.fetch_odds_implied_prob")
    def test_directional_agreement_passes(self, mock_odds):
        """AI 和博彩公司方向一致 → 通过。"""
        mock_odds.return_value = {
            "participant1": "Atlanta Hawks",
            "participant2": "Memphis Grizzlies",
            "participant1_prob": 0.857,
            "participant2_prob": 0.143,
        }
        skip_reason = self.engine._cross_validate_odds(
            market_id="1627600",
            question="Grizzlies vs. Hawks",
            outcome="NO",
            avg_prob=0.20,  # AI says Grizzlies 20% (Hawks favored)
        )
        assert skip_reason == "", "方向一致应通过"

    @patch("src.signal.signal_engine.fetch_odds_implied_prob")
    def test_no_odds_data_passes(self, mock_odds):
        """OddsPapi 无数据时直接放行。"""
        mock_odds.return_value = None
        skip_reason = self.engine._cross_validate_odds(
            market_id="1627600",
            question="Grizzlies vs. Hawks",
            outcome="YES",
            avg_prob=0.815,
        )
        assert skip_reason == ""

    def test_non_vs_question_passes(self):
        """非 'vs' 问题格式直接放行。"""
        skip_reason = self.engine._cross_validate_odds(
            market_id="1627600",
            question="Will Lakers win the championship?",
            outcome="YES",
            avg_prob=0.70,
        )
        assert skip_reason == ""


# ---------------------------------------------------------------------------
# Bayesian LR Cap 测试
# ---------------------------------------------------------------------------


class TestBayesianLRCap:
    """验证 Bayesian 更新的 LR 上限生效。"""

    def test_extreme_divergence_capped(self):
        """AI 0.85 vs market 0.115 — combined LR 应被 cap 住。"""
        from src.evolution.strategy_evolver import StrategyEvolver

        post, lr, ag = StrategyEvolver.bayesian_update(
            0.115, [(0.85, 0.72), (0.85, 0.72), (0.85, 1.08)]
        )
        # 无 cap 时 combined_lr ≈ 23772，有 cap 后应 ≤ 400 (max_lr²)
        assert lr <= 400, f"Combined LR={lr:.0f} 应被 cap 在 400 以内"
        # posterior 仍偏高但已被显著抑制（从 0.997 降至 ~0.98）
        assert post < 0.99, f"posterior={post:.3f} 过高"

    def test_moderate_divergence_unaffected(self):
        """AI 0.70 vs market 0.50 — LR 适中，不应被 cap 影响。"""
        from src.evolution.strategy_evolver import StrategyEvolver

        post, lr, ag = StrategyEvolver.bayesian_update(0.5, [(0.7, 1.0)])
        # LR = (0.7/0.5)*(0.5/0.3) = 1.4*1.667 = 2.33 < 20 → 不受影响
        assert post > 0.5
        assert lr < 20


# ---------------------------------------------------------------------------
# T284: 体育两阶段流水线测试
# ---------------------------------------------------------------------------


def make_sports_market(
    market_id: str = "sport_001", question: str = "Will Lakers win?"
) -> dict:
    return {
        "id": market_id,
        "question": question,
        "bestAsk": 0.55,
        "bestBid": 0.53,
        "liquidity": 80000,
        "volume24hr": 15000,
        "endDate": "2026-04-01T20:00:00Z",
        "tags": [{"label": "Sports"}],
    }


class TestSportsTwoPhaseArch:
    """T284: 体育两阶段信号生成架构验证"""

    def _make_engine(self):
        engine = SignalEngine.__new__(SignalEngine)
        engine._cycle_stats = {"ai_calls": 0, "pre_filtered": 0, "second_skipped": 0}
        engine._sports_enriched_this_cycle = 0
        engine._sports_odds_calls_this_cycle = 0
        engine._signal_cache = {}
        engine._recently_analyzed = {}
        return engine

    @patch("src.signal.signal_engine.get")
    def test_phase1_uses_free_data_only(self, mock_get):
        """Phase1 (skip_sports_paid=True) 不应调用任何 OddsPapi 函数。"""
        mock_get.return_value = None

        engine = self._make_engine()
        market = make_sports_market()

        # 确认 fetch_odds_context / fetch_today_context / fetch_scores_context 不被调用
        with (
            patch(
                "src.signal.signal_engine.fetch_sports_context",
                return_value="TheSportsDB data",
            ) as mock_free,
            patch("src.signal.signal_engine.fetch_odds_context") as mock_odds,
            patch("src.signal.signal_engine.fetch_today_context") as mock_today,
            patch("src.signal.signal_engine.fetch_scores_context") as mock_scores,
            patch.object(engine, "_call_ai", return_value=None),
        ):
            try:
                engine._fetch_category_context(
                    market,
                    "sport_001",
                    "Will Lakers win?",
                    "sports",
                    skip_sports_paid=True,
                )
            except Exception:
                pass  # 其他初始化问题不影响本测试

            mock_free.assert_called_once()
            mock_odds.assert_not_called()
            mock_today.assert_not_called()
            mock_scores.assert_not_called()

    @patch("src.signal.signal_engine.get")
    def test_phase2_calls_odds_api(self, mock_get):
        """Phase2 (skip_sports_paid=False) 应调用 OddsPapi (fetch_odds_context)。"""
        mock_get.return_value = None

        engine = self._make_engine()
        market = make_sports_market()

        with (
            patch(
                "src.signal.signal_engine.fetch_sports_context",
                return_value="free data",
            ),
            patch(
                "src.signal.signal_engine.fetch_odds_context",
                return_value="ODDS: Lakers -150, Hawks +120",
            ) as mock_odds,
            patch(
                "src.signal.signal_engine.fetch_today_context",
                return_value="today data",
            ),
            patch(
                "src.signal.signal_engine.fetch_scores_context",
                return_value="scores data",
            ),
            patch.object(engine, "_call_ai", return_value=None),
        ):
            try:
                engine._fetch_category_context(
                    market,
                    "sport_001",
                    "Will Lakers win?",
                    "sports",
                    skip_sports_paid=False,
                )
            except Exception:
                pass

            mock_odds.assert_called_once()

    @patch("src.signal.signal_engine.get")
    def test_phase2_odds_data_injected_into_context(self, mock_get):
        """Phase2 的 odds 数据应被合并进 sports context 供 AI prompt 使用。"""

        def _get(key, *a, **kw):
            if key == "signal":
                return {"sports_max_enriched_per_cycle": 15}
            return None

        mock_get.side_effect = _get

        engine = self._make_engine()
        engine.smart_money = MagicMock()
        engine.smart_money.get_smart_context_for_market = MagicMock(return_value="")
        market = make_sports_market()
        ODDS_TEXT = "ODDS: Lakers -150, Hawks +120"

        with (
            patch(
                "src.signal.signal_engine.fetch_sports_context",
                return_value="TheSportsDB",
            ),
            patch(
                "src.signal.signal_engine.fetch_odds_context", return_value=ODDS_TEXT
            ),
            patch("src.signal.signal_engine.fetch_today_context", return_value=""),
            patch("src.signal.signal_engine.fetch_scores_context", return_value=""),
        ):
            ctx = engine._fetch_category_context(
                market,
                "sport_001",
                "Will Lakers win?",
                "sports",
                skip_sports_paid=False,
            )

        assert "sports" in ctx
        assert ODDS_TEXT in ctx["sports"], "赔率数据必须注入 AI context"

    @patch("src.signal.signal_engine.get")
    def test_prescreen_filters_no_edge_markets(self, mock_get):
        """PreScreen 拒绝的市场不进入 Phase2（零 AI 调用）。"""

        def _get(key, *a, **kw):
            if key == "signal":
                return {
                    "sports_max_odds_verify_per_cycle": 6,
                    "sports_odds_verify_min_ev": 0.04,
                    "sports_max_enriched_per_cycle": 15,
                }
            return None

        mock_get.side_effect = _get

        engine = self._make_engine()

        # PreScreen 全部拒绝 → Phase2 不应被调用
        with (
            patch.object(
                engine,
                "_sports_lightweight_prescreen",
                return_value=(False, 0.0, ""),
            ) as mock_prescreen,
            patch.object(engine, "_analyze_market") as mock_analyze,
        ):
            markets = [make_sports_market("sport_001"), make_sports_market("sport_002")]
            result = engine.analyze_sports_two_phase_batch(markets, 100.0)

        assert result == {}, "预筛拒绝的市场不应有任何结果"
        assert mock_prescreen.call_count == 2, "每个市场应被预筛一次"
        mock_analyze.assert_not_called(), "Phase1 预筛拒绝 → 零 AI 调用"

    @patch("src.signal.signal_engine.get")
    def test_prescreen_pass_triggers_phase2_full_analysis(self, mock_get):
        """通过 PreScreen 的市场在 Phase2 中以 skip_sports_paid=False 做完整 AI+OddsAPI 分析。"""

        def _get(key, *a, **kw):
            if key == "signal":
                return {
                    "sports_max_odds_verify_per_cycle": 6,
                    "sports_odds_verify_min_ev": 0.04,
                    "sports_max_enriched_per_cycle": 15,
                }
            return None

        mock_get.side_effect = _get

        engine = self._make_engine()

        phase2_sig = Signal(
            market_id="sport_001",
            question="Will Lakers win?",
            outcome="YES",
            ev=0.065,
            confidence=0.75,
            size_usdc=5.0,
            skip=False,
        )

        with (
            patch.object(
                engine,
                "_sports_lightweight_prescreen",
                return_value=(True, 0.85, "TheSportsDB data..."),
            ),
            patch.object(
                engine, "_analyze_market", return_value=phase2_sig
            ) as mock_analyze,
        ):
            result = engine.analyze_sports_two_phase_batch(
                [make_sports_market("sport_001")], 100.0
            )

        assert "sport_001" in result, "预筛通过的市场应在 Phase2 结果中"
        assert result["sport_001"].confidence == 0.75, "结果应为 Phase2 信号"
        mock_analyze.assert_called_once()
        _, kwargs = mock_analyze.call_args
        assert (
            kwargs.get("skip_sports_paid") is False
        ), "Phase2 必须 skip_sports_paid=False"
        assert engine._sports_odds_calls_this_cycle == 1

    @patch("src.signal.signal_engine.get")
    def test_phase2_top_n_limit_from_prescreen(self, mock_get):
        """Phase2 最多处理 sports_max_odds_verify_per_cycle 个市场。"""

        def _get(key, *a, **kw):
            if key == "signal":
                return {
                    "sports_max_odds_verify_per_cycle": 3,  # 限制3个
                    "sports_odds_verify_min_ev": 0.04,
                    "sports_max_enriched_per_cycle": 15,
                }
            return None

        mock_get.side_effect = _get

        engine = self._make_engine()
        phase2_calls = []

        # PreScreen 全部通过，但评分不同
        scores = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2]

        def _prescreen(market):
            idx = int(market.get("id", "sport_0").split("_")[-1])
            return (True, scores[idx], f"data_{idx}")

        def _mock_analyze(market, balance, skip_sports_paid=False):
            phase2_calls.append(market.get("id"))
            return Signal(
                market_id=market.get("id"),
                question="q",
                outcome="YES",
                ev=0.05,
                confidence=0.7,
                size_usdc=5.0,
                skip=False,
            )

        markets = [make_sports_market(f"sport_{i}") for i in range(8)]
        with (
            patch.object(
                engine, "_sports_lightweight_prescreen", side_effect=_prescreen
            ),
            patch.object(engine, "_analyze_market", side_effect=_mock_analyze),
        ):
            engine.analyze_sports_two_phase_batch(markets, 100.0)

        assert len(phase2_calls) <= 3, "Phase2 调用次数不得超过 max_verify=3"

    @patch("src.signal.signal_engine.get")
    def test_reset_clears_odds_calls_counter(self, mock_get):
        """_reset_sports_enriched_count 应同时清零 _sports_odds_calls_this_cycle。"""
        mock_get.return_value = None
        engine = self._make_engine()
        engine._sports_odds_calls_this_cycle = 5
        engine._reset_sports_enriched_count()
        assert engine._sports_odds_calls_this_cycle == 0

    @patch("src.signal.signal_engine.get")
    def test_log_cycle_stats_includes_odds_calls(self, mock_get):
        """log_cycle_stats 应在返回值中包含 sports_odds_calls 字段。"""
        mock_get.return_value = None
        engine = self._make_engine()
        engine._sports_odds_calls_this_cycle = 4
        stats = engine.log_cycle_stats()
        assert "sports_odds_calls" in stats
        assert stats["sports_odds_calls"] == 4

    def test_non_sports_markets_unaffected(self):
        """非体育赛道市场不应受到 skip_sports_paid 参数影响（不传参）。"""
        engine = self._make_engine()
        engine.smart_money = MagicMock()
        engine.smart_money.get_smart_context_for_market = MagicMock(return_value="")
        weather_market = {
            "id": "weather_001",
            "question": "Will it rain?",
            "bestAsk": 0.4,
            "bestBid": 0.38,
            "tags": [{"label": "Weather"}],
        }
        # 确认 _fetch_category_context 对 weather 赛道正常运行（不应报错）
        with (
            patch(
                "src.signal.signal_engine.fetch_weather",
                return_value={"raw_summary": ""},
            ),
            patch("src.signal.signal_engine.extract_oracle_url", return_value=""),
        ):
            ctx = engine._fetch_category_context(
                weather_market, "weather_001", "Will it rain?", "weather"
            )
        # weather 不受 skip_sports_paid 影响，ctx 可以为空但不报错
        assert isinstance(ctx, dict)


# ---------------------------------------------------------------------------
# T284v2: _sports_lightweight_prescreen() 单元测试
# ---------------------------------------------------------------------------


class TestSportsLightweightPrescreen:
    """T284v2: 验证轻量预筛的各种过滤/通过条件。"""

    def _make_engine(self):
        engine = SignalEngine.__new__(SignalEngine)
        engine._cycle_stats = {"ai_calls": 0, "pre_filtered": 0, "second_skipped": 0}
        engine._sports_enriched_this_cycle = 0
        engine._sports_odds_calls_this_cycle = 0
        engine._signal_cache = {}
        engine._recently_analyzed = {}
        return engine

    @patch("src.signal.signal_engine.get")
    def test_spread_market_rejected(self, mock_get):
        """非胜负盘(spread/O-U)应被预筛直接拒绝。"""
        mock_get.return_value = None
        engine = self._make_engine()
        market = make_sports_market(question="Lakers vs Hawks: O/U 215.5")
        passed, score, ctx = engine._sports_lightweight_prescreen(market)
        assert not passed, "Spread/O-U 市场不应通过预筛"

    @patch("src.signal.signal_engine.get")
    def test_extreme_low_price_rejected(self, mock_get):
        """bestAsk 过低（<0.10）应被拒绝。"""
        mock_get.return_value = {
            "data_driven_extreme_low": 0.10,
            "data_driven_extreme_high": 0.90,
        }
        engine = self._make_engine()
        market = make_sports_market()
        market["bestAsk"] = 0.05
        passed, score, ctx = engine._sports_lightweight_prescreen(market)
        assert not passed, "极低价格不应通过预筛"

    @patch("src.signal.signal_engine.get")
    def test_extreme_high_price_rejected(self, mock_get):
        """bestAsk 过高（>0.90）应被拒绝。"""
        mock_get.return_value = {
            "data_driven_extreme_low": 0.10,
            "data_driven_extreme_high": 0.90,
        }
        engine = self._make_engine()
        market = make_sports_market()
        market["bestAsk"] = 0.95
        passed, score, ctx = engine._sports_lightweight_prescreen(market)
        assert not passed, "极高价格不应通过预筛"

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.fetch_sports_context")
    def test_no_thesportsdb_data_high_liquidity_passes(self, mock_fetch, mock_get):
        """T29: TheSportsDB 无数据但流动性高 → 仍通过预筛。"""
        mock_get.return_value = {
            "data_driven_extreme_low": 0.10,
            "data_driven_extreme_high": 0.90,
        }
        mock_fetch.return_value = "short"
        engine = self._make_engine()
        market = make_sports_market()
        market["liquidity"] = 80000  # > 5000 min
        passed, score, ctx = engine._sports_lightweight_prescreen(market)
        assert passed, "T29: 高流动性市场即使无数据也应通过预筛"
        assert ctx == "", "无数据时 context 应为空"

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.fetch_sports_context")
    def test_no_data_low_liquidity_rejected(self, mock_fetch, mock_get):
        """T29: TheSportsDB 无数据且流动性低 → 拒绝。"""
        mock_get.return_value = {
            "data_driven_extreme_low": 0.10,
            "data_driven_extreme_high": 0.90,
        }
        mock_fetch.return_value = "short"
        engine = self._make_engine()
        market = make_sports_market()
        market["liquidity"] = 2000  # < 5000 min
        market["volume24hr"] = 1000
        passed, score, ctx = engine._sports_lightweight_prescreen(market)
        assert not passed, "低流动性+无数据应被拒绝"

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.fetch_sports_context")
    def test_thesportsdb_fetch_error_low_liq_rejected(self, mock_fetch, mock_get):
        """T29: TheSportsDB 获取异常 + 低流动性 → 安全拒绝。"""
        mock_get.return_value = {
            "data_driven_extreme_low": 0.10,
            "data_driven_extreme_high": 0.90,
        }
        mock_fetch.side_effect = Exception("Connection timeout")
        engine = self._make_engine()
        market = make_sports_market()
        market["liquidity"] = 1000  # < 5000 min
        market["volume24hr"] = 500
        passed, score, ctx = engine._sports_lightweight_prescreen(market)
        assert not passed, "API异常+低流动性应安全拒绝"

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.fetch_sports_context")
    def test_valid_market_passes(self, mock_fetch, mock_get):
        """价格合理 + 有 TheSportsDB 数据的市场应通过预筛。"""
        mock_get.return_value = {
            "data_driven_extreme_low": 0.10,
            "data_driven_extreme_high": 0.90,
        }
        mock_fetch.return_value = "Lakers recent form: W5, Hawks recent form: L3. " * 5
        engine = self._make_engine()
        market = make_sports_market()
        market["bestAsk"] = 0.55
        passed, score, ctx = engine._sports_lightweight_prescreen(market)
        assert passed, "合理价格+有效数据应通过预筛"
        assert score > 0, "评分应大于0"
        assert "Lakers" in ctx, "应返回 TheSportsDB 数据"

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.fetch_sports_context")
    def test_price_near_05_scores_higher(self, mock_fetch, mock_get):
        """T26: bestAsk 偏离 0.5 越多（clear favorite）评分越高。"""
        mock_get.return_value = {
            "data_driven_extreme_low": 0.10,
            "data_driven_extreme_high": 0.90,
        }
        data = "Lakers vs Hawks detailed stats and records " * 8
        mock_fetch.return_value = data
        engine = self._make_engine()

        m1 = make_sports_market("m1")
        m1["bestAsk"] = 0.50
        _, score_50, _ = engine._sports_lightweight_prescreen(m1)

        m2 = make_sports_market("m2")
        m2["bestAsk"] = 0.80
        _, score_80, _ = engine._sports_lightweight_prescreen(m2)

        assert (
            score_80 > score_50
        ), "T26: clear favorite (0.80) 应比 50/50 (0.50) 评分更高"

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.fetch_sports_context")
    def test_zero_ai_calls_during_prescreen(self, mock_fetch, mock_get):
        """预筛过程中不应有任何 AI 调用。"""
        mock_get.return_value = {
            "data_driven_extreme_low": 0.10,
            "data_driven_extreme_high": 0.90,
        }
        mock_fetch.return_value = "Test data " * 20
        engine = self._make_engine()
        market = make_sports_market()
        with patch.object(engine, "_call_ai") as mock_ai:
            engine._sports_lightweight_prescreen(market)
            mock_ai.assert_not_called(), "预筛不应调用 AI"


# ---------------------------------------------------------------------------
# T284-fix: generate_signals() 实盘路径体育两阶段路由验证
# ---------------------------------------------------------------------------


class TestGenerateSignalsSportsRouting:
    """验证 generate_signals() 将体育市场路由到 analyze_sports_two_phase_batch()。"""

    def _make_engine(self):
        engine = SignalEngine.__new__(SignalEngine)
        engine._cycle_stats = {"ai_calls": 0, "pre_filtered": 0, "second_skipped": 0}
        engine._sports_enriched_this_cycle = 0
        engine._sports_odds_calls_this_cycle = 0
        engine._signal_cache = {}
        engine._recently_analyzed = {}
        engine.smart_money = MagicMock()
        return engine

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.detect_category")
    def test_sports_markets_use_two_phase_batch(self, mock_detect, mock_get):
        """体育市场必须走 analyze_sports_two_phase_batch()，不走 _analyze_market()。"""
        config_vals = {
            "signal.min_ev_threshold": 0.03,
            "signal.min_confidence": 0.65,
            "paper_trading.category_min_confidence": {},
            "signal.max_tier1_markets": 10,
            "signal.max_tier2_markets": 0,
            "signal.max_tier3_markets": 0,
            "signal.rotation_memory_seconds": 1800,
            "signal.category_min_markets": {},
            "paper_trading.sports_blocked_keywords": [],
            "paper_trading.settlement_max_hours": {},
            "signal": {
                "sports_max_odds_verify_per_cycle": 6,
                "sports_odds_verify_min_ev": 0.04,
            },
        }
        mock_get.side_effect = lambda k, *a, **kw: config_vals.get(k)

        sport_sig = Signal(
            market_id="sport_001",
            question="Will Lakers win?",
            outcome="YES",
            ev=0.08,
            confidence=0.72,
            size_usdc=5.0,
            skip=False,
        )

        def _detect(m):
            return "sports" if "sport" in str(m.get("id", "")) else "weather"

        mock_detect.side_effect = _detect

        engine = self._make_engine()

        candidate_pool = {
            "tier1": [make_sports_market("sport_001"), make_sports_market("sport_002")],
        }

        with (
            patch.object(
                engine,
                "analyze_sports_two_phase_batch",
                return_value={"sport_001": sport_sig},
            ) as mock_batch,
            patch.object(engine, "_analyze_market") as mock_single,
            patch.object(engine, "_get_balance", return_value=100.0),
            patch.object(engine, "_quick_price_screen", return_value=False),
            patch.object(engine, "_skip_saturated_events", side_effect=lambda x: x),
            patch(
                "src.signal.signal_engine.prefetch_ensemble_data",
            ),
        ):
            signals = engine.generate_signals(candidate_pool, balance=100.0)

        mock_batch.assert_called_once()
        mock_single.assert_not_called()
        assert len(signals) == 1
        assert signals[0].market_id == "sport_001"

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.detect_category")
    def test_non_sports_still_use_single_phase(self, mock_detect, mock_get):
        """非体育市场（如天气）仍走原有 _analyze_market() 单阶段路径。"""
        config_vals = {
            "signal.min_ev_threshold": 0.03,
            "signal.min_confidence": 0.65,
            "paper_trading.category_min_confidence": {},
            "signal.max_tier1_markets": 10,
            "signal.max_tier2_markets": 0,
            "signal.max_tier3_markets": 0,
            "signal.rotation_memory_seconds": 1800,
            "signal.category_min_markets": {},
            "paper_trading.sports_blocked_keywords": [],
            "paper_trading.settlement_max_hours": {},
            "signal": {},
        }
        mock_get.side_effect = lambda k, *a, **kw: config_vals.get(k)

        weather_sig = Signal(
            market_id="weather_001",
            question="Will it rain?",
            outcome="YES",
            ev=0.06,
            confidence=0.70,
            size_usdc=5.0,
            skip=False,
        )

        mock_detect.return_value = "weather"
        engine = self._make_engine()

        candidate_pool = {
            "tier1": [
                {
                    "id": "weather_001",
                    "question": "Will it rain?",
                    "bestAsk": 0.4,
                    "bestBid": 0.38,
                    "tags": [{"label": "Weather"}],
                }
            ],
        }

        with (
            patch.object(engine, "analyze_sports_two_phase_batch") as mock_batch,
            patch.object(engine, "_analyze_market", return_value=weather_sig),
            patch.object(engine, "_get_balance", return_value=100.0),
            patch.object(engine, "_quick_price_screen", return_value=False),
            patch.object(engine, "_skip_saturated_events", side_effect=lambda x: x),
            patch("src.signal.signal_engine.prefetch_ensemble_data"),
        ):
            signals = engine.generate_signals(candidate_pool, balance=100.0)

        # 天气走 _analyze_market，不走 batch
        mock_batch.assert_not_called()
        assert len(signals) == 1
        assert signals[0].market_id == "weather_001"

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.detect_category")
    def test_mixed_markets_split_correctly(self, mock_detect, mock_get):
        """混合市场：体育走两阶段，天气走单阶段，两组信号正确合并。"""
        config_vals = {
            "signal.min_ev_threshold": 0.03,
            "signal.min_confidence": 0.65,
            "paper_trading.category_min_confidence": {},
            "signal.max_tier1_markets": 10,
            "signal.max_tier2_markets": 0,
            "signal.max_tier3_markets": 0,
            "signal.rotation_memory_seconds": 1800,
            "signal.category_min_markets": {},
            "paper_trading.sports_blocked_keywords": [],
            "paper_trading.settlement_max_hours": {},
            "signal": {
                "sports_max_odds_verify_per_cycle": 6,
                "sports_odds_verify_min_ev": 0.04,
            },
        }
        mock_get.side_effect = lambda k, *a, **kw: config_vals.get(k)

        sport_sig = Signal(
            market_id="sport_001",
            question="Will Lakers win?",
            outcome="YES",
            ev=0.08,
            confidence=0.72,
            size_usdc=5.0,
            skip=False,
        )
        weather_sig = Signal(
            market_id="weather_001",
            question="Will it rain?",
            outcome="YES",
            ev=0.06,
            confidence=0.70,
            size_usdc=5.0,
            skip=False,
        )

        def _detect(m):
            return "sports" if "sport" in str(m.get("id", "")) else "weather"

        mock_detect.side_effect = _detect

        engine = self._make_engine()

        candidate_pool = {
            "tier1": [
                make_sports_market("sport_001"),
                {
                    "id": "weather_001",
                    "question": "Will it rain?",
                    "bestAsk": 0.4,
                    "bestBid": 0.38,
                    "tags": [{"label": "Weather"}],
                },
            ],
        }

        with (
            patch.object(
                engine,
                "analyze_sports_two_phase_batch",
                return_value={"sport_001": sport_sig},
            ) as mock_batch,
            patch.object(
                engine, "_analyze_market", return_value=weather_sig
            ) as mock_single,
            patch.object(engine, "_get_balance", return_value=100.0),
            patch.object(engine, "_quick_price_screen", return_value=False),
            patch.object(engine, "_skip_saturated_events", side_effect=lambda x: x),
            patch("src.signal.signal_engine.prefetch_ensemble_data"),
        ):
            signals = engine.generate_signals(candidate_pool, balance=100.0)

        mock_batch.assert_called_once()
        assert len(mock_batch.call_args[0][0]) == 1  # 1 sports market
        mock_single.assert_called_once()  # 1 weather market → _analyze_market
        assert len(signals) == 2
        ids = {s.market_id for s in signals}
        assert ids == {"sport_001", "weather_001"}


# ------------------------------------------------------------------
# T217追加: get_market_fee_rate + 动态 feeSchedule 测试
# ------------------------------------------------------------------
class TestGetMarketFeeRate:
    """验证 db.get_market_fee_rate() 的优先级逻辑。"""

    def test_fee_disabled_returns_zero(self):
        """feesEnabled=False → 费率=0（免费市场）"""
        from src.utils.db import get_market_fee_rate

        market = {"feesEnabled": False}
        assert get_market_fee_rate(market, "crypto") == 0.0

    def test_fee_schedule_rate_used(self):
        """有 feeSchedule.rate 时使用动态费率"""
        from src.utils.db import get_market_fee_rate

        market = {
            "feesEnabled": True,
            "feeSchedule": {"exponent": 1, "rate": 0.072, "takerOnly": True},
        }
        assert get_market_fee_rate(market, "crypto") == 0.072

    def test_fee_schedule_sports(self):
        """sports feeSchedule.rate=0.03"""
        from src.utils.db import get_market_fee_rate

        market = {
            "feesEnabled": True,
            "feeSchedule": {"exponent": 1, "rate": 0.03, "takerOnly": True},
        }
        assert get_market_fee_rate(market, "sports") == 0.03

    def test_fee_schedule_none_falls_back_to_config(self):
        """feeSchedule=None 时 fallback 到 config"""
        from src.utils.db import get_market_fee_rate

        market = {"feesEnabled": True, "feeSchedule": None}
        with patch("src.utils.config_loader.get") as mock_get:
            mock_get.side_effect = lambda k: {
                "paper_trading.taker_fee_rates": {"weather": 0.05},
                "paper_trading.taker_fee_rate_default": 0.05,
            }.get(k)
            result = get_market_fee_rate(market, "weather")
            assert result == 0.05

    def test_no_market_data_falls_back_to_config(self):
        """market_data=None 时 fallback 到 config"""
        from src.utils.db import get_market_fee_rate

        with patch("src.utils.config_loader.get") as mock_get:
            mock_get.side_effect = lambda k: {
                "paper_trading.taker_fee_rates": {"sports": 0.03},
                "paper_trading.taker_fee_rate_default": 0.05,
            }.get(k)
            assert get_market_fee_rate(None, "sports") == 0.03

    def test_crypto_15min_high_rate(self):
        """crypto_15_min 类型的高费率正确传递"""
        from src.utils.db import get_market_fee_rate

        market = {
            "feesEnabled": True,
            "feeSchedule": {"exponent": 2, "rate": 0.25, "takerOnly": True},
        }
        assert get_market_fee_rate(market, "crypto") == 0.25

    def test_geopolitics_fee_free_via_api(self):
        """feesEnabled=False 的地缘政治市场费率为0"""
        from src.utils.db import get_market_fee_rate

        market = {"feesEnabled": False, "feeSchedule": None}
        assert get_market_fee_rate(market, "geopolitics") == 0.0


class TestCalcSizeDynamicFeeRate:
    """验证 _calc_size 使用动态 fee_rate 参数。"""

    def _make_engine(self):
        return SignalEngine.__new__(SignalEngine)

    @patch("src.signal.signal_engine.get")
    @patch(
        "src.utils.smart_capital.SmartCapitalEngine.get_max_bet_pct", return_value=0.03
    )
    def test_fee_rate_param_used(self, mock_sce, mock_get):
        """传入 fee_rate=0.072 时使用该值计算 Kelly"""
        mock_get.side_effect = lambda k: {
            "executor.min_bet_usdc": 2.0,
            "paper_trading.fixed_bet_size_usdc": 0,
            "signal": {"kelly_fraction": 0.25},
        }.get(k)
        result = self._make_engine()._calc_size(
            100.0, 0.70, 0.50, "crypto", fee_rate=0.072
        )
        assert result >= 2.0

    @patch("src.signal.signal_engine.get")
    @patch(
        "src.utils.smart_capital.SmartCapitalEngine.get_max_bet_pct", return_value=0.03
    )
    def test_fee_rate_none_falls_back(self, mock_sce, mock_get):
        """fee_rate=None 时从 config 读取"""
        mock_get.side_effect = lambda k: {
            "executor.min_bet_usdc": 2.0,
            "paper_trading.fixed_bet_size_usdc": 0,
            "paper_trading.taker_fee_rates": {"crypto": 0.072},
            "paper_trading.taker_fee_rate_default": 0.05,
            "signal": {"kelly_fraction": 0.25},
        }.get(k)
        result = self._make_engine()._calc_size(100.0, 0.70, 0.50, "crypto")
        assert result >= 2.0

    @patch("src.signal.signal_engine.get")
    @patch(
        "src.utils.smart_capital.SmartCapitalEngine.get_max_bet_pct", return_value=0.03
    )
    def test_zero_fee_rate_increases_size(self, mock_sce, mock_get):
        """fee_rate=0（免费市场）时 Kelly size 应大于 fee_rate=0.072"""
        mock_get.side_effect = lambda k: {
            "executor.min_bet_usdc": 2.0,
            "paper_trading.fixed_bet_size_usdc": 0,
            "signal": {"kelly_fraction": 0.25},
        }.get(k)
        engine = self._make_engine()
        size_free = engine._calc_size(100.0, 0.70, 0.50, "crypto", fee_rate=0.0)
        size_paid = engine._calc_size(100.0, 0.70, 0.50, "crypto", fee_rate=0.072)
        assert size_free >= size_paid


# ------------------------------------------------------------------
# T217追加: get_market_fee_rate + 动态 feeSchedule 测试
# ------------------------------------------------------------------
class TestGetMarketFeeRate:
    """验证 db.get_market_fee_rate() 的优先级逻辑。"""

    def test_fee_disabled_returns_zero(self):
        """feesEnabled=False → 费率=0（免费市场）"""
        from src.utils.db import get_market_fee_rate

        market = {"feesEnabled": False}
        assert get_market_fee_rate(market, "crypto") == 0.0

    def test_fee_schedule_rate_used(self):
        """有 feeSchedule.rate 时使用动态费率"""
        from src.utils.db import get_market_fee_rate

        market = {
            "feesEnabled": True,
            "feeSchedule": {"exponent": 1, "rate": 0.072, "takerOnly": True},
        }
        assert get_market_fee_rate(market, "crypto") == 0.072

    def test_fee_schedule_sports(self):
        """sports feeSchedule.rate=0.03"""
        from src.utils.db import get_market_fee_rate

        market = {
            "feesEnabled": True,
            "feeSchedule": {"exponent": 1, "rate": 0.03, "takerOnly": True},
        }
        assert get_market_fee_rate(market, "sports") == 0.03

    def test_fee_schedule_none_falls_back_to_config(self):
        """feeSchedule=None 时 fallback 到 config"""
        from src.utils.db import get_market_fee_rate

        market = {"feesEnabled": True, "feeSchedule": None}
        with patch("src.utils.config_loader.get") as mock_get:
            mock_get.side_effect = lambda k: {
                "paper_trading.taker_fee_rates": {"weather": 0.05},
                "paper_trading.taker_fee_rate_default": 0.05,
            }.get(k)
            result = get_market_fee_rate(market, "weather")
            assert result == 0.05

    def test_no_market_data_falls_back_to_config(self):
        """market_data=None 时 fallback 到 config"""
        from src.utils.db import get_market_fee_rate

        with patch("src.utils.config_loader.get") as mock_get:
            mock_get.side_effect = lambda k: {
                "paper_trading.taker_fee_rates": {"sports": 0.03},
                "paper_trading.taker_fee_rate_default": 0.05,
            }.get(k)
            assert get_market_fee_rate(None, "sports") == 0.03

    def test_crypto_15min_high_rate(self):
        """crypto_15_min 类型的高费率正确传递"""
        from src.utils.db import get_market_fee_rate

        market = {
            "feesEnabled": True,
            "feeSchedule": {"exponent": 2, "rate": 0.25, "takerOnly": True},
        }
        assert get_market_fee_rate(market, "crypto") == 0.25

    def test_geopolitics_fee_free_via_api(self):
        """feesEnabled=False 的地缘政治市场费率为0"""
        from src.utils.db import get_market_fee_rate

        market = {"feesEnabled": False, "feeSchedule": None}
        assert get_market_fee_rate(market, "geopolitics") == 0.0


class TestCalcSizeDynamicFeeRate:
    """验证 _calc_size 使用动态 fee_rate 参数。"""

    def _make_engine(self):
        return SignalEngine.__new__(SignalEngine)

    @patch("src.signal.signal_engine.get")
    @patch(
        "src.utils.smart_capital.SmartCapitalEngine.get_max_bet_pct", return_value=0.03
    )
    def test_fee_rate_param_used(self, mock_sce, mock_get):
        """传入 fee_rate=0.072 时使用该值计算 Kelly"""
        mock_get.side_effect = lambda k: {
            "executor.min_bet_usdc": 2.0,
            "paper_trading.fixed_bet_size_usdc": 0,
            "signal": {"kelly_fraction": 0.25},
        }.get(k)
        result = self._make_engine()._calc_size(
            100.0, 0.70, 0.50, "crypto", fee_rate=0.072
        )
        assert result >= 2.0

    @patch("src.signal.signal_engine.get")
    @patch(
        "src.utils.smart_capital.SmartCapitalEngine.get_max_bet_pct", return_value=0.03
    )
    def test_fee_rate_none_falls_back(self, mock_sce, mock_get):
        """fee_rate=None 时从 config 读取"""
        mock_get.side_effect = lambda k: {
            "executor.min_bet_usdc": 2.0,
            "paper_trading.fixed_bet_size_usdc": 0,
            "paper_trading.taker_fee_rates": {"crypto": 0.072},
            "paper_trading.taker_fee_rate_default": 0.05,
            "signal": {"kelly_fraction": 0.25},
        }.get(k)
        result = self._make_engine()._calc_size(100.0, 0.70, 0.50, "crypto")
        assert result >= 2.0

    @patch("src.signal.signal_engine.get")
    @patch(
        "src.utils.smart_capital.SmartCapitalEngine.get_max_bet_pct", return_value=0.03
    )
    def test_zero_fee_rate_increases_size(self, mock_sce, mock_get):
        """fee_rate=0（免费市场）时 Kelly size 应大于等于 fee_rate=0.072"""
        mock_get.side_effect = lambda k: {
            "executor.min_bet_usdc": 2.0,
            "paper_trading.fixed_bet_size_usdc": 0,
            "signal": {"kelly_fraction": 0.25},
        }.get(k)
        engine = self._make_engine()
        size_free = engine._calc_size(100.0, 0.70, 0.50, "crypto", fee_rate=0.0)
        size_paid = engine._calc_size(100.0, 0.70, 0.50, "crypto", fee_rate=0.072)
        assert size_free >= size_paid
