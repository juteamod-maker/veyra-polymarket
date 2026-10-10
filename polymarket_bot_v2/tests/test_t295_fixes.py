"""
test_t295_fixes.py — T295 回归测试

测试范围:
  1. T295-B: BkFollow 信号生成 (_bookmaker_follow_signal)
     - 正常路径: bk_prob >= 82%, poly_price <= 80% → 生成信号
     - EV 不足: bk_prob 82% 但 poly_price 79% (fee后 EV < min) → 无信号
     - bk_prob 不足: bk_prob < 82% → 无信号
     - 价格超硬上限: poly_price > hard_max → 无信号
     - 功能禁用: enabled=False → 无信号
     - NO 方向: bk_prob_no = 1 - bk_prob >= 82% → NO 方向信号
     - spread market 过滤: "Spread" 标题 → 无信号
  2. T295-B: analyze_sports_two_phase_batch Phase0 逻辑
     - Phase0 命中 → bk_follow_results 包含信号 + 市场不进 Phase1
  3. T295-C: 日志类型模式覆盖 (data.py _LOG_TYPE_PATTERNS)
     - 所有关键词均有归属
     - 旧类型向后兼容
  4. T295-C: 默认日志 tab 为 'trades'（JavaScript 常量验证）
  5. T295-A: config.json 包含 sports_bookmaker_follow 键
"""

import json
import os
import re
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from src.signal.signal_engine import Signal, SignalEngine

# ── 1. T295-B: _bookmaker_follow_signal 单元测试 ─────────────────────


def _make_engine() -> SignalEngine:
    """创建最小化的 SignalEngine 实例（跳过 __init__）"""
    return SignalEngine.__new__(SignalEngine)


def _make_sports_market(
    market_id: str = "mkt_sport_001",
    question: str = "Will Manchester United vs Liverpool end in a home win?",
    best_ask: float = 0.72,
    best_bid: float = 0.70,
    condition_id: str = "cond_abc123",
    liquidity: float = 10000.0,
) -> dict:
    return {
        "id": market_id,
        "conditionId": condition_id,
        "question": question,
        "bestAsk": best_ask,
        "bestBid": best_bid,
        "liquidity": liquidity,
        "tokens": [
            {"outcome": "YES", "token_id": "tok_yes_001"},
            {"outcome": "NO", "token_id": "tok_no_001"},
        ],
    }


def _bk_follow_cfg(
    enabled: bool = True,
    min_bk_prob: float = 0.82,
    max_poly_price: float = 0.80,
    hard_max: float = 0.85,
    min_ev: float = 0.005,
    fee_deduction: float = 0.005,
) -> dict:
    return {
        "enabled": enabled,
        "min_bk_probability": min_bk_prob,
        "max_polymarket_price": max_poly_price,
        "hard_max_price": hard_max,
        "min_ev": min_ev,
        "fee_deduction": fee_deduction,
    }


def _call_bk_follow(engine, market, balance=50.0, bk_prob=0.85, config_overrides=None):
    """辅助: mock get() 后调用 _bookmaker_follow_signal"""
    cfg = _bk_follow_cfg(**(config_overrides or {}))

    def _get(path):
        if "bookmaker_follow" in path:
            return cfg
        if "taker_fee_rate" in path:
            return 0.01
        if "tail_max_price" in path:
            return 0.85
        return None

    with patch("src.signal.signal_engine.get", side_effect=_get):
        with patch.object(
            engine,
            "_get_bookmaker_implied_prob",
            return_value=bk_prob,
        ):
            with patch.object(
                engine,
                "_is_spread_market",
                return_value=False,
            ):
                with patch.object(
                    engine,
                    "_extract_token_id",
                    return_value="tok_yes_001",
                ):
                    with patch.object(
                        engine,
                        "_calc_size",
                        return_value=1.5,
                    ):
                        return engine._bookmaker_follow_signal(market, balance)


class TestBkFollowSignal:
    def setup_method(self):
        self.engine = _make_engine()

    def test_yes_signal_generated_when_conditions_met(self):
        """bk_prob=0.85 poly=0.72 → YES 信号生成 EV>0.005"""
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow(self.engine, market, bk_prob=0.85)
        assert sig is not None
        assert sig.outcome == "YES"
        assert sig.strategy_tag == "bk_follow"
        assert sig.ev > 0.005
        assert sig.confidence > 0
        assert sig.size_usdc == 1.5  # mocked

    def test_signal_ev_correct(self):
        """T33: EV = bk_prob - poly_price - fee_deduction = 0.85 - 0.72 - 0.005 = 0.125"""
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow(self.engine, market, bk_prob=0.85)
        assert sig is not None
        assert abs(sig.ev - (0.85 - 0.72 - 0.005)) < 0.0001

    def test_no_signal_when_ev_insufficient(self):
        """bk_prob=0.82 poly=0.815 fee=0.01 → EV=−0.005 < min_ev → None"""
        market = _make_sports_market(best_ask=0.815)
        # poly_price=0.815 > max_poly_price=0.80 → 已被价格检查拦截
        sig = _call_bk_follow(self.engine, market, bk_prob=0.82)
        assert sig is None

    def test_no_signal_when_bk_prob_insufficient(self):
        """bk_prob=0.79 < min_bk_probability=0.82 → None"""
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow(self.engine, market, bk_prob=0.79)
        assert sig is None

    def test_no_signal_when_price_exceeds_max(self):
        """poly_price=0.82 > max_poly_price=0.80 → None"""
        market = _make_sports_market(best_ask=0.82)
        sig = _call_bk_follow(self.engine, market, bk_prob=0.85)
        assert sig is None

    def test_no_signal_when_price_exceeds_hard_max(self):
        """poly_price=0.86 > hard_max=0.85 → None"""
        market = _make_sports_market(best_ask=0.86)
        sig = _call_bk_follow(self.engine, market, bk_prob=0.90)
        assert sig is None

    def test_no_signal_when_disabled(self):
        """enabled=False → None"""
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow(
            self.engine, market, bk_prob=0.90, config_overrides={"enabled": False}
        )
        assert sig is None

    def test_no_direction_signal(self):
        """bk_prob=0.10 (NO side = 0.90 >= 0.82) → NO 方向信号"""
        market = _make_sports_market(best_ask=0.85, best_bid=0.28)
        # NO: poly_price = 1 - best_bid = 1 - 0.28 = 0.72
        # EV = 0.90 - 0.72 - 0.01 = 0.17
        sig = _call_bk_follow(self.engine, market, bk_prob=0.10)
        assert sig is not None
        assert sig.outcome == "NO"
        assert sig.ev > 0

    def test_spread_market_filtered(self):
        """is_spread_market returns True → None"""
        market = _make_sports_market(question="Manchester United -1.5 Spread at 2.00")

        def _get(path):
            if "bookmaker_follow" in path:
                return _bk_follow_cfg()
            if "taker_fee_rate" in path:
                return 0.01
            if "tail_max_price" in path:
                return 0.85
            return None

        with patch("src.signal.signal_engine.get", side_effect=_get):
            with patch.object(
                self.engine, "_get_bookmaker_implied_prob", return_value=0.85
            ):
                with patch.object(self.engine, "_is_spread_market", return_value=True):
                    sig = self.engine._bookmaker_follow_signal(market, 50.0)
        assert sig is None

    def test_non_vs_format_filtered(self):
        """非 'X vs Y' 格式的市场 → None（BkFollow仅适用于两队对抗）"""
        market = _make_sports_market(
            question="Will the Super Bowl MVP be an offensive player?"
        )
        sig = _call_bk_follow(self.engine, market, bk_prob=0.85)
        assert sig is None

    def test_signal_has_required_fields(self):
        """验证信号包含所有必需字段"""
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow(self.engine, market, bk_prob=0.85)
        assert sig is not None
        assert sig.market_id
        assert sig.question
        assert sig.outcome in ("YES", "NO")
        assert sig.token_id
        assert sig.condition_id
        assert sig.strategy_tag == "bk_follow"
        assert "bookmaker_consensus" in sig.model_votes

    def test_model_votes_structure(self):
        """验证 model_votes 包含 BkFollow 推理信息"""
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow(self.engine, market, bk_prob=0.85)
        assert sig is not None
        bk_vote = sig.model_votes.get("bookmaker_consensus", {})
        assert bk_vote.get("outcome") == "YES"
        assert bk_vote.get("probability") >= 0.82
        assert "BkFollow" in bk_vote.get("reasoning", "")

    # ── T33: 纯博彩公司跟随策略测试 ──────────────────────────────────
    def test_t33_small_gap_passes_with_correct_fee(self):
        """T33: bk=0.745 poly=0.740 → 旧fee(1%)拒绝，新fee(0.5%)通过
        EV = 0.745 - 0.740 - 0.005 = 0.000 ≥ min_ev(0)
        """
        market = _make_sports_market(best_ask=0.740)
        sig = _call_bk_follow(
            self.engine,
            market,
            bk_prob=0.745,
            config_overrides={
                "min_bk_prob": 0.55,
                "min_ev": 0.0,
                "fee_deduction": 0.005,
                "max_poly_price": 0.83,
            },
        )
        assert sig is not None
        assert sig.outcome == "YES"
        assert sig.ev >= 0.0
        assert abs(sig.ev - 0.0) < 0.001

    def test_t33_min_ev_zero_accepts_breakeven(self):
        """T33: min_ev=0 允许 EV 刚好为 0 的信号通过"""
        market = _make_sports_market(best_ask=0.600)
        # EV = 0.605 - 0.600 - 0.005 = 0.000
        sig = _call_bk_follow(
            self.engine,
            market,
            bk_prob=0.605,
            config_overrides={
                "min_bk_prob": 0.55,
                "min_ev": 0.0,
                "fee_deduction": 0.005,
            },
        )
        assert sig is not None
        assert sig.ev >= 0.0

    def test_t33_fee_deduction_default_0005(self):
        """T33: fee_deduction 默认 0.005（不提供时 fallback）"""
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow(
            self.engine,
            market,
            bk_prob=0.85,
            config_overrides={"min_bk_prob": 0.55, "min_ev": 0.0},
        )
        assert sig is not None
        # EV = 0.85 - 0.72 - 0.005 = 0.125 (默认 fee_deduction=0.005)
        assert abs(sig.ev - 0.125) < 0.001

    def test_t33_negative_ev_rejected(self):
        """T33: gap 小于 fee_deduction → EV<0 → 拒绝"""
        market = _make_sports_market(best_ask=0.740)
        # EV = 0.742 - 0.740 - 0.005 = -0.003 < 0
        sig = _call_bk_follow(
            self.engine,
            market,
            bk_prob=0.742,
            config_overrides={
                "min_bk_prob": 0.55,
                "min_ev": 0.0,
                "fee_deduction": 0.005,
            },
        )
        assert sig is None


# ── 2. T295-B: analyze_sports_two_phase_batch Phase0 逻辑 ─────────────


class TestBkFollowPhase0:
    def setup_method(self):
        self.engine = _make_engine()

    def _mock_get(self, path):
        if "bookmaker_follow" in path:
            return _bk_follow_cfg()
        if "sports_max_odds_verify_per_cycle" in path:
            return 6
        if "taker_fee_rate" in path:
            return 0.01
        if "tail_max_price" in path:
            return 0.85
        return None

    def test_bk_follow_markets_skip_phase1(self):
        """Phase0 命中的市场不进入 Phase1 循环（_sports_lightweight_prescreen 不被调用）"""
        markets = [_make_sports_market(best_ask=0.72, market_id="m001")]

        mock_sig = Signal(
            market_id="m001",
            question="test vs test2",
            outcome="YES",
            ev=0.10,
            confidence=0.85,
            size_usdc=1.5,
            model_votes={"bookmaker_consensus": {}},
            strategy_tag="bk_follow",
        )

        with patch("src.signal.signal_engine.get", side_effect=self._mock_get):
            with patch.object(
                self.engine, "_bookmaker_follow_signal", return_value=mock_sig
            ) as mock_bk:
                with patch.object(
                    self.engine, "_sports_lightweight_prescreen"
                ) as mock_pre:
                    with patch.object(
                        self.engine, "_analyze_market", return_value=None
                    ):
                        result = self.engine.analyze_sports_two_phase_batch(
                            markets, balance=50.0
                        )
        # Phase0 捕获 → prescreen 不被调用（或调用但对该市场已跳过）
        assert "m001" in result
        assert result["m001"].strategy_tag == "bk_follow"
        # prescreen should NOT have been called for the bk_follow market
        if mock_pre.called:
            for call in mock_pre.call_args_list:
                m = call.args[0] if call.args else call.kwargs.get("market")
                if m:
                    assert (m.get("id") or m.get("conditionId")) != "m001"

    def test_empty_markets_returns_empty(self):
        """空市场列表返回空字典"""
        with patch("src.signal.signal_engine.get", side_effect=self._mock_get):
            result = self.engine.analyze_sports_two_phase_batch([], balance=50.0)
        assert result == {}

    def test_phase0_and_phase2_results_merged(self):
        """Phase0 和 Phase2 的结果均在最终字典中"""
        phase0_market = _make_sports_market(best_ask=0.72, market_id="m_bk")
        phase2_market = _make_sports_market(
            best_ask=0.55,
            market_id="m_ai",
            question="Will Lionel Messi vs Ronaldo 2026 happen?",
        )

        mock_sig_bk = Signal(
            market_id="m_bk",
            question="Team A vs Team B",
            outcome="YES",
            ev=0.10,
            confidence=0.85,
            size_usdc=1.5,
            model_votes={"bookmaker_consensus": {}},
            strategy_tag="bk_follow",
        )
        mock_sig_ai = Signal(
            market_id="m_ai",
            question="Will Lionel Messi vs Ronaldo 2026 happen?",
            outcome="YES",
            ev=0.06,
            confidence=0.70,
            size_usdc=1.0,
            model_votes={},
        )

        def bk_follow_side_effect(market, balance):
            if market.get("id") == "m_bk":
                return mock_sig_bk
            return None

        with patch("src.signal.signal_engine.get", side_effect=self._mock_get):
            with patch.object(
                self.engine,
                "_bookmaker_follow_signal",
                side_effect=bk_follow_side_effect,
            ):
                with patch.object(
                    self.engine,
                    "_sports_lightweight_prescreen",
                    return_value=(True, 0.8, {}),
                ):
                    with patch.object(
                        self.engine,
                        "_analyze_market",
                        return_value=mock_sig_ai,
                    ):
                        result = self.engine.analyze_sports_two_phase_batch(
                            [phase0_market, phase2_market], balance=50.0
                        )

        assert "m_bk" in result
        assert "m_ai" in result
        assert result["m_bk"].strategy_tag == "bk_follow"


# ── 3. T295-C: 日志类型模式覆盖完整性 ────────────────────────────────


class TestLogTypePatternCoverage:
    """验证新 _LOG_TYPE_PATTERNS 中的关键词及向后兼容性"""

    @pytest.fixture(autouse=True)
    def load_patterns(self):
        from scripts.dashboard_routes.data import _LOG_TYPE_PATTERNS

        self.patterns = _LOG_TYPE_PATTERNS

    def test_new_tab_types_exist(self):
        """7个新分类均存在"""
        for t in ("trades", "signal", "sports", "weather", "risk", "system", "errors"):
            assert t in self.patterns, f"缺少新日志类型: {t}"

    def test_backward_compat_old_types(self):
        """旧类型名称向后兼容"""
        for t in (
            "trading",
            "scanner",
            "evolution",
            "error",
            "warning",
            "executor",
            "position",
            "reconcile",
        ):
            assert t in self.patterns, f"旧日志类型向后兼容失败: {t}"

    def test_trades_covers_key_patterns(self):
        """trades 覆盖下单+仓位+对账关键词"""
        keywords = [
            "paper_trader",
            "PaperBet",
            "止盈",
            "对账",
            "reconcile",
            "fill",
            "CLOB",
            "WriteOff",
        ]
        for kw in keywords:
            assert any(
                kw in p for p in self.patterns["trades"]
            ), f"trades 缺少关键词: {kw}"

    def test_sports_covers_bk_patterns(self):
        """sports 覆盖 BkFollow / T284 / OddsPapi 关键词"""
        keywords = ["BkFollow", "T284", "OddsPapi", "bookmaker"]
        for kw in keywords:
            assert any(
                kw in p for p in self.patterns["sports"]
            ), f"sports 缺少关键词: {kw}"

    def test_errors_covers_both_error_and_warning(self):
        """errors 同时包含 ERROR 和 WARNING 内容"""
        has_error = any("ERROR" in p for p in self.patterns["errors"])
        has_warning = any("WARNING" in p for p in self.patterns["errors"])
        assert has_error and has_warning, "errors 未同时覆盖 ERROR 和 WARNING"

    def test_signal_covers_scanner(self):
        """signal 包含 scanner 关键词（原独立 scanner tab 已合并）"""
        scanner_covered = any("scanner" in p.lower() for p in self.patterns["signal"])
        assert scanner_covered, "signal 未包含 scanner 关键词"


# ── 4. T295-C: 默认日志 Tab 验证 ──────────────────────────────────────


class TestDefaultLogTabInJs:
    """验证 data.js 中默认日志 tab 已改为 'trades'"""

    def test_default_log_filter_is_trades(self):
        js_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "static", "js", "data.js"
        )
        js_path = os.path.normpath(js_path)
        assert os.path.exists(js_path), f"data.js 不存在: {js_path}"
        with open(js_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert (
            "_currentLogFilter = 'trades'" in content
        ), "data.js 中默认日志 tab 应为 'trades'，请检查 T295-C 更改"

    def test_all_tab_removed_from_html(self):
        """index.html 中不再有 data-type='all' 的日志按钮"""
        html_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "static", "index.html"
        )
        html_path = os.path.normpath(html_path)
        assert os.path.exists(html_path), f"index.html 不存在: {html_path}"
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
        # 旧"全部"按钮不应存在于日志过滤区
        assert (
            'data-type="all"' not in content
        ), "index.html 仍有 data-type='all' 日志按钮，请检查 T295-C 更改"

    def test_new_tabs_in_html(self):
        """index.html 包含所有新分类按钮"""
        html_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "static", "index.html"
        )
        html_path = os.path.normpath(html_path)
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
        for tab_type in (
            "trades",
            "signal",
            "sports",
            "weather",
            "risk",
            "system",
            "errors",
        ):
            assert (
                f'data-type="{tab_type}"' in content
            ), f"index.html 缺少新日志 tab: {tab_type}"


# ── 5. T295-A: config.json 聪明钱配置完整性 ──────────────────────────


class TestConfigKeys:
    """验证 config.json 包含 T295 新增配置键"""

    @pytest.fixture(autouse=True)
    def load_config(self):
        cfg_path = os.path.join(
            os.path.dirname(__file__), "..", "config", "config.json"
        )
        cfg_path = os.path.normpath(cfg_path)
        with open(cfg_path, "r", encoding="utf-8") as f:
            self.cfg = json.load(f)

    def test_sports_bookmaker_follow_exists(self):
        """paper_trading.sports_bookmaker_follow 键存在"""
        pt = self.cfg.get("paper_trading", {})
        assert "sports" in pt and "bookmaker_follow" in pt.get(
            "sports", {}
        ), "config.json 缺少 paper_trading.sports_bookmaker_follow"

    def test_bk_follow_required_keys(self):
        """sports_bookmaker_follow 包含所有必要键"""
        bk = self.cfg["paper_trading"]["sports"]["bookmaker_follow"]
        for key in (
            "enabled",
            "min_bk_probability",
            "max_polymarket_price",
            "hard_max_price",
            "min_ev",
            "fee_deduction",
        ):
            assert key in bk, f"sports_bookmaker_follow 缺少键: {key}"

    def test_bk_follow_values_sensible(self):
        """参数值在合理范围"""
        bk = self.cfg["paper_trading"]["sports"]["bookmaker_follow"]
        assert 0.5 < bk["min_bk_probability"] < 1.0
        assert 0.5 < bk["max_polymarket_price"] < 1.0
        assert bk["hard_max_price"] >= bk["max_polymarket_price"]
        assert 0 <= bk["min_ev"] < 0.1

    def test_smart_money_manual_wallets_is_list(self):
        """smart_money.manual_wallets 是列表类型"""
        sm = self.cfg.get("smart_money", {})
        assert "manual_wallets" in sm, "config.json 缺少 smart_money.manual_wallets"
        assert isinstance(sm["manual_wallets"], list)

    def test_sports_bookmaker_gap_exists(self):
        """T295-B: paper_trading.sports.bookmaker_gap 键存在（gap 整合）"""
        pt = self.cfg.get("paper_trading", {})
        sp = pt.get("sports", {})
        assert (
            "bookmaker_gap" in sp
        ), "config.json 缺少 paper_trading.sports.bookmaker_gap"
        gap = sp["bookmaker_gap"]
        for key in (
            "enabled",
            "strong_gap_threshold",
            "medium_gap_threshold",
            "ev_bonus_aligned",
            "ev_penalty_opposed",
        ):
            assert key in gap, f"sports_bookmaker_gap 缺少键: {key}"

    def test_sports_bookmaker_blend_exists(self):
        """T295-B: paper_trading.sports.bookmaker_blend 键存在（blend 整合）"""
        pt = self.cfg.get("paper_trading", {})
        sp = pt.get("sports", {})
        assert (
            "bookmaker_blend" in sp
        ), "config.json 缺少 paper_trading.sports.bookmaker_blend"
        blend = sp["bookmaker_blend"]
        assert "bookmaker_weight" in blend
        assert "confidence_boost" in blend
        assert 0 < blend["bookmaker_weight"] <= 1.0
        assert blend["confidence_boost"] >= 0


# ── 6. T295-B v2.1: Gap + Blend 整合测试 ─────────────────────────────


def _call_bk_follow_v21(engine, market, balance=50.0, bk_prob=0.85):
    """辅助: mock 包含 gap/blend/category_min 配置后调用 _bookmaker_follow_signal"""
    bk_cfg = _bk_follow_cfg()
    gap_cfg = {
        "enabled": True,
        "strong_gap_threshold": 0.08,
        "medium_gap_threshold": 0.05,
        "weak_gap_threshold": 0.03,
        "ev_bonus_aligned": -0.005,
        "ev_penalty_opposed": 0.02,
    }
    blend_cfg = {
        "bookmaker_weight": 0.95,
        "confidence_boost": 0.15,
        "bookmaker_edge_floor": 0.001,
    }
    cat_min_ev = {"sports": 0, "default": 1.0}
    cat_min_conf = {"sports": 0.25, "default": 1.0}

    def _get(path):
        if "bookmaker_follow" in path:
            return bk_cfg
        if "bookmaker_gap" in path:
            return gap_cfg
        if "bookmaker_blend" in path:
            return blend_cfg
        if "category_min_ev" in path:
            return cat_min_ev
        if "category_min_confidence" in path:
            return cat_min_conf
        if "taker_fee_rate" in path:
            return 0.01
        if "tail_max_price" in path:
            return 0.85
        return None

    with patch("src.signal.signal_engine.get", side_effect=_get):
        with patch.object(engine, "_get_bookmaker_implied_prob", return_value=bk_prob):
            with patch.object(engine, "_is_spread_market", return_value=False):
                with patch.object(
                    engine, "_extract_token_id", return_value="tok_yes_001"
                ):
                    with patch.object(engine, "_calc_size", return_value=1.5):
                        return engine._bookmaker_follow_signal(market, balance)


class TestBkFollowGapBlendIntegration:
    """T295-B v2.1: 验证 gap 和 blend 已整合到 _bookmaker_follow_signal"""

    def setup_method(self):
        self.engine = _make_engine()

    def test_confidence_boosted_by_blend(self):
        """confidence = p_novig * bk_weight + confidence_boost (not just min(p_novig, 0.95))"""
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow_v21(self.engine, market, bk_prob=0.85)
        assert sig is not None
        # Expected: 0.85 * 0.95 + 0.15 = 0.8075 + 0.15 = 0.9575 (capped at 0.98)
        expected_conf = min(0.85 * 0.95 + 0.15, 0.98)
        assert abs(sig.confidence - round(expected_conf, 4)) < 0.001, (
            f"confidence {sig.confidence} != expected {expected_conf:.4f}. "
            "blend 整合是否已实施？"
        )

    def test_confidence_higher_than_old_formula(self):
        """blend 后 confidence 应高于旧公式 min(p_novig, 0.95)"""
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow_v21(self.engine, market, bk_prob=0.85)
        assert sig is not None
        old_confidence = min(0.85, 0.95)  # old formula
        assert (
            sig.confidence > old_confidence
        ), f"blend 后 confidence={sig.confidence} 不高于旧公式 {old_confidence}"

    def test_gap_level_strong_in_reasoning(self):
        """gap=0.13 (>= 0.08 strong threshold) → reasoning 中含 gap 信息"""
        # bk_prob=0.85, poly=0.72, gap=0.13 → aligned_strong
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow_v21(self.engine, market, bk_prob=0.85)
        assert sig is not None
        reasoning = sig.model_votes.get("bookmaker_consensus", {}).get("reasoning", "")
        assert "gap=" in reasoning, f"reasoning 中未包含 gap 信息: {reasoning}"
        assert (
            "aligned_strong" in reasoning
        ), f"gap_level 应为 aligned_strong: {reasoning}"

    def test_gap_level_medium_when_gap_moderate(self):
        """bk_prob=0.82 poly=0.77 → gap=0.05 → aligned_medium"""
        # poly_price=0.77 < max_poly_price=0.80 ✓, bk_prob=0.82 >= min_bk_prob=0.82 ✓
        market = _make_sports_market(best_ask=0.77)
        sig = _call_bk_follow_v21(self.engine, market, bk_prob=0.82)
        # EV = 0.82 - 0.77 - 0.01 = 0.04 > effective_min_ev=0.0 → 生成信号
        assert (
            sig is not None
        ), "bk_prob=0.82, poly=0.77, gap=0.05 应生成信号 (effective_min_ev=0)"
        reasoning = sig.model_votes.get("bookmaker_consensus", {}).get("reasoning", "")
        assert (
            "aligned_medium" in reasoning or "aligned_weak" in reasoning
        ), f"gap_level 不正确: {reasoning}"

    def test_effective_min_ev_relaxed_when_aligned(self):
        """gap aligned → effective_min_ev = 0 → ev>0 即可通过"""
        # bk_prob=0.82, poly=0.71, fee=0.01 → ev=0.82-0.71-0.01=0.10 > 0
        market = _make_sports_market(best_ask=0.71)
        sig = _call_bk_follow_v21(self.engine, market, bk_prob=0.82)
        assert sig is not None, "aligned gap 下 ev>0 应生成信号（effective_min_ev=0）"

    def test_strategy_tag_still_bk_follow(self):
        """v2.1 版本 strategy_tag 仍为 'bk_follow'"""
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow_v21(self.engine, market, bk_prob=0.85)
        assert sig is not None
        assert sig.strategy_tag == "bk_follow"

    def test_reasoning_contains_version_tag(self):
        """reasoning 标注为 BkFollow v2.0"""
        market = _make_sports_market(best_ask=0.72)
        sig = _call_bk_follow_v21(self.engine, market, bk_prob=0.85)
        assert sig is not None
        reasoning = sig.model_votes.get("bookmaker_consensus", {}).get("reasoning", "")
        assert "v2.0" in reasoning, f"reasoning 未标注 v2.0 版本: {reasoning}"


# ── 7. T295-C: 扩展日志模式覆盖完整性 ───────────────────────────────


class TestExtendedLogPatternCoverage:
    """T295-C: 验证新增 _LOG_TYPE_PATTERNS 覆盖此前遗漏的模块"""

    @pytest.fixture(autouse=True)
    def load_patterns(self):
        from scripts.dashboard_routes.data import _LOG_TYPE_PATTERNS

        self.patterns = _LOG_TYPE_PATTERNS

    def _line_matches_tab(self, log_line: str, tab: str) -> bool:
        """模拟 data.py 的 any(p in log_line for p in patterns)"""
        return any(p in log_line for p in self.patterns[tab])

    def test_smart_money_covered_in_signal(self):
        """[SmartMoney] 日志归属 signal tab"""
        line = "[2025-01-01 12:00:00] INFO     src.utils.smart_money  [SmartMoney] 大户持仓检测"
        assert self._line_matches_tab(
            line, "signal"
        ), "SmartMoney 日志未被 signal tab 覆盖"

    def test_price_monitor_covered_in_trades(self):
        """[PriceMonitor] 日志归属 trades tab"""
        line = "[2025-01-01 12:00:00] INFO     src.monitor.price_monitor  [PriceMonitor] 止损触发"
        assert self._line_matches_tab(
            line, "trades"
        ), "PriceMonitor 日志未被 trades tab 覆盖"

    def test_rapidapi_covered_in_sports(self):
        """[RapidAPI] 日志归属 sports tab"""
        line = "[2025-01-01 12:00:00] INFO     src.utils.rapidapi_client  [RapidAPI] 赛事数据拉取"
        assert self._line_matches_tab(
            line, "sports"
        ), "RapidAPI 日志未被 sports tab 覆盖"

    def test_telegram_bot_covered_in_system(self):
        """[TelegramBot] 日志归属 system tab"""
        line = "[2025-01-01 12:00:00] INFO     src.utils.telegram_bot  [TelegramBot] 发送通知"
        assert self._line_matches_tab(
            line, "system"
        ), "TelegramBot 日志未被 system tab 覆盖"

    def test_odds_fetcher_covered_in_signal(self):
        """[OddsFetcher] 日志归属 signal tab"""
        line = "[2025-01-01 12:00:00] INFO     src.utils.odds_fetcher  [OddsFetcher] 获取赔率"
        assert self._line_matches_tab(
            line, "signal"
        ), "OddsFetcher 日志未被 signal tab 覆盖"

    def test_dual_model_covered_in_weather(self):
        """[DualModel] 日志归属 weather tab"""
        line = "[2025-01-01 12:00:00] INFO     src.utils.weather_fetcher  [DualModel] GFS+ECMWF"
        assert self._line_matches_tab(
            line, "weather"
        ), "DualModel 日志未被 weather tab 覆盖"

    def test_exit_policy_covered_in_trades(self):
        """[ExitPolicy] 日志归属 trades tab"""
        line = (
            "[2025-01-01 12:00:00] INFO     src.exit_policy.base  [ExitPolicy] 持仓退出"
        )
        assert self._line_matches_tab(
            line, "trades"
        ), "ExitPolicy 日志未被 trades tab 覆盖"

    def test_bookmaker_gap_covered_in_signal(self):
        """[BookmakerGap] 日志归属 signal tab (T295-B 新增日志标签)"""
        line = "[2025-01-01 12:00:00] INFO     src.signal.signal_engine  [BookmakerGap] gap=+0.13"
        assert self._line_matches_tab(
            line, "signal"
        ), "BookmakerGap 日志未被 signal tab 覆盖"
