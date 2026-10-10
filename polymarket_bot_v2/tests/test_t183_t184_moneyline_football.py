"""
T183: Moneyline-only 铁律测试 — 非胜负盘市场必须被三层防线拦截
T184: 足球 Moneyline 增强测试 — 博彩效率 Gap + 联赛乘数 + SPORTS_SYSTEM_PROMPT

根因：O/U 1.5 Rounds 泄漏事件 — scanner/signal/paper_trader 三层均未完全拦截
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# ══════════════════════════════════════════════════════════════
# T183 — Moneyline-only 铁律
# ══════════════════════════════════════════════════════════════


class TestNonMoneylineBlockSignalEngine(unittest.TestCase):
    """Layer 2: signal_engine._is_spread_market() 必须检测所有非 Moneyline 模式"""

    def _check(self, question: str) -> bool:
        from src.signal.signal_engine import SignalEngine

        return SignalEngine._is_spread_market(question)

    # --- T183 根因：这些模式之前漏掉了 ---
    def test_ou_short_form(self):
        """O/U 缩写 — T183 根因"""
        self.assertTrue(self._check("O/U 1.5 Rounds"))

    def test_ou_with_number(self):
        self.assertTrue(self._check("O/U 5.5"))

    def test_rounds_keyword(self):
        """Rounds 关键词 — T183 根因"""
        self.assertTrue(self._check("Will there be 2+ rounds?"))

    def test_total_rounds(self):
        self.assertTrue(self._check("Total Rounds Over 2.5"))

    def test_total_goals(self):
        self.assertTrue(self._check("Total Goals Over 2.5"))

    def test_total_runs(self):
        self.assertTrue(self._check("Total Runs Under 8.5"))

    def test_first_to_score(self):
        self.assertTrue(self._check("Will Man City be first to score?"))

    def test_both_teams_to_score(self):
        self.assertTrue(self._check("Both teams to score in Man City vs Arsenal"))

    def test_clean_sheet(self):
        self.assertTrue(self._check("Will Real Madrid keep a clean sheet?"))

    def test_exact_score(self):
        self.assertTrue(self._check("Exact score 2-1"))

    def test_correct_score(self):
        self.assertTrue(self._check("Correct score: 1-0"))

    def test_player_props(self):
        self.assertTrue(self._check("LeBron James player props: points over 25.5"))

    def test_corners(self):
        self.assertTrue(self._check("Total corners over 10.5"))

    def test_three_pointers(self):
        self.assertTrue(self._check("Curry three-pointers over 4.5"))

    # --- 之前已经能检测到的，确保没破坏 ---
    def test_plus_half_spread(self):
        self.assertTrue(self._check("Will Lakers cover +3.5 against Celtics?"))

    def test_over_under_full(self):
        self.assertTrue(self._check("Over/under 210.5 total points"))

    def test_handicap(self):
        self.assertTrue(self._check("Asian handicap: Man City vs Liverpool"))

    # --- Moneyline 不应被误拦 ---
    def test_moneyline_simple_vs(self):
        self.assertFalse(self._check("Manchester City vs Arsenal"))

    def test_moneyline_will_beat(self):
        self.assertFalse(self._check("Will Lakers beat Celtics?"))

    def test_moneyline_who_wins(self):
        self.assertFalse(self._check("Who will win the World Cup?"))

    def test_moneyline_will_win(self):
        self.assertFalse(self._check("Will Brazil win against Argentina?"))


class TestNonMoneylineBlockPaperTrader(unittest.TestCase):
    """Layer 3: paper_trader._evaluate_sports_entry() 硬性拒绝非 Moneyline"""

    def _make_signal(self, question: str, ev: float = 0.25, conf: float = 0.80):
        from src.signal.signal_engine import Signal

        return Signal(
            market_id="mkt-test-001",
            question=question,
            outcome="YES",
            ev=ev,
            confidence=conf,
            size_usdc=2.0,
            expected_price=0.45,
            model_votes={"m1": {"outcome": "YES", "probability": 0.70}},
        )

    def _make_market(self):
        end_date = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        return {
            "id": "mkt-test-001",
            "question": "test",
            "bestAsk": 0.45,
            "bestBid": 0.43,
            "tags": [{"slug": "nba"}],
            "volume24hr": 50000,
            "endDate": end_date,
        }

    @patch("src.signal.signal_engine.fetch_odds_implied_prob", return_value=None)
    def test_ou_15_rounds_blocked(self, _mock):
        """T183 根因复现：O/U 1.5 Rounds 必须被拒绝"""
        from src.simulation.paper_trader import PaperTrader

        signal = self._make_signal("O/U 1.5 Rounds", ev=0.25, conf=0.80)
        market = self._make_market()
        pt_cfg = {"sports": {"moneyline_only": True}}
        result = PaperTrader._evaluate_sports_entry(market, signal, pt_cfg, 50.0)
        self.assertIsNone(result, "O/U 1.5 Rounds should be BLOCKED")

    @patch("src.signal.signal_engine.fetch_odds_implied_prob", return_value=None)
    def test_total_goals_blocked(self, _mock):
        from src.simulation.paper_trader import PaperTrader

        signal = self._make_signal("Total Goals Over 2.5", ev=0.30, conf=0.85)
        market = self._make_market()
        pt_cfg = {"sports": {"moneyline_only": True}}
        result = PaperTrader._evaluate_sports_entry(market, signal, pt_cfg, 50.0)
        self.assertIsNone(result)

    @patch("src.signal.signal_engine.fetch_odds_implied_prob", return_value=None)
    def test_spread_blocked(self, _mock):
        from src.simulation.paper_trader import PaperTrader

        signal = self._make_signal("Lakers +3.5 vs Celtics", ev=0.30, conf=0.85)
        market = self._make_market()
        pt_cfg = {"sports": {"moneyline_only": True}}
        result = PaperTrader._evaluate_sports_entry(market, signal, pt_cfg, 50.0)
        self.assertIsNone(result)

    @patch("src.signal.signal_engine.fetch_odds_implied_prob", return_value=None)
    def test_moneyline_allowed(self, _mock):
        """Moneyline 市场应正常通过"""
        from src.simulation.paper_trader import PaperTrader

        signal = self._make_signal("Will Lakers beat Celtics?", ev=0.08, conf=0.72)
        signal.expected_price = 0.55
        market = self._make_market()
        pt_cfg = {
            "sports": {"moneyline_only": True},
            "category_max_bet_pct": {"sports": 0.06, "default": 0.05},
        }
        result = PaperTrader._evaluate_sports_entry(market, signal, pt_cfg, 50.0)
        self.assertIsNotNone(result, "Moneyline should be ALLOWED")
        _, bucket = result
        self.assertEqual(bucket, "sports")

    @patch("src.signal.signal_engine.fetch_odds_implied_prob", return_value=None)
    def test_moneyline_only_false_allows_spread(self, _mock):
        """moneyline_only=false 时 spread 不被拦截（用于未来灵活性）"""
        from src.simulation.paper_trader import PaperTrader

        signal = self._make_signal("O/U 5.5 Goals", ev=0.08, conf=0.72)
        signal.expected_price = 0.45
        market = self._make_market()
        pt_cfg = {
            "sports": {"moneyline_only": False},
            "category_max_bet_pct": {"sports": 0.06, "default": 0.05},
        }
        result = PaperTrader._evaluate_sports_entry(market, signal, pt_cfg, 50.0)
        # When moneyline_only is False, should pass through to normal evaluation
        self.assertIsNotNone(result)


# ══════════════════════════════════════════════════════════════
# T184 — 足球 Moneyline 增强
# ══════════════════════════════════════════════════════════════


class TestSportsSystemPromptSoccerFramework(unittest.TestCase):
    """验证 SPORTS_SYSTEM_PROMPT 包含足球 Moneyline 分析框架"""

    def test_soccer_framework_present(self):
        from src.signal.signal_engine import SPORTS_SYSTEM_PROMPT

        self.assertIn("SOCCER MONEYLINE ANALYSIS FRAMEWORK", SPORTS_SYSTEM_PROMPT)

    def test_home_base_rate(self):
        from src.signal.signal_engine import SPORTS_SYSTEM_PROMPT

        self.assertIn("45-48%", SPORTS_SYSTEM_PROMPT)

    def test_mispricing_section(self):
        from src.signal.signal_engine import SPORTS_SYSTEM_PROMPT

        self.assertIn("MISPRICES MONEYLINE", SPORTS_SYSTEM_PROMPT)

    def test_moneyline_only_instruction(self):
        from src.signal.signal_engine import SPORTS_SYSTEM_PROMPT

        self.assertIn("ONLY bet on win/loss outcome", SPORTS_SYSTEM_PROMPT)


class TestBookmakerGapEvaluation(unittest.TestCase):
    """验证博彩效率 Gap 检测功能"""

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.fetch_odds_implied_prob")
    def test_gap_aligned_strong(self, mock_odds, mock_get):
        """博彩公司同向 + 强 gap → aligned=True, strong"""
        from src.signal.signal_engine import SignalEngine

        mock_get.return_value = {
            "enabled": True,
            "strong_gap_threshold": 0.08,
            "medium_gap_threshold": 0.05,
            "weak_gap_threshold": 0.03,
            "ev_bonus_aligned": -0.005,
            "ev_penalty_opposed": 0.02,
        }
        mock_odds.return_value = {
            "participant1": "Manchester City",
            "participant2": "Arsenal",
            "participant1_prob": 0.65,
            "participant2_prob": 0.35,
        }

        result = SignalEngine.evaluate_bookmaker_gap(
            "Manchester City vs Arsenal", "YES", 0.55
        )
        self.assertIsNotNone(result)
        self.assertTrue(result["aligned"])
        self.assertAlmostEqual(result["gap"], 0.10, places=2)
        self.assertEqual(result["gap_level"], "strong")

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.fetch_odds_implied_prob")
    def test_gap_opposed_medium(self, mock_odds, mock_get):
        """博彩公司反向 + 中 gap → aligned=False"""
        from src.signal.signal_engine import SignalEngine

        mock_get.return_value = {
            "enabled": True,
            "strong_gap_threshold": 0.08,
            "medium_gap_threshold": 0.05,
        }
        mock_odds.return_value = {
            "participant1": "Real Madrid",
            "participant2": "Barcelona",
            "participant1_prob": 0.40,
            "participant2_prob": 0.60,
        }

        result = SignalEngine.evaluate_bookmaker_gap(
            "Real Madrid vs Barcelona", "YES", 0.47
        )
        self.assertIsNotNone(result)
        self.assertFalse(result["aligned"])
        self.assertEqual(result["gap_level"], "medium")

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.fetch_odds_implied_prob")
    def test_gap_disabled(self, mock_odds, mock_get):
        """Gap 检测关闭时返回 None"""
        from src.signal.signal_engine import SignalEngine

        mock_get.return_value = {"enabled": False}
        result = SignalEngine.evaluate_bookmaker_gap("Man City vs Arsenal", "YES", 0.55)
        self.assertIsNone(result)

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.fetch_odds_implied_prob")
    def test_no_odds_data(self, mock_odds, mock_get):
        """无赔率数据时返回 None"""
        from src.signal.signal_engine import SignalEngine

        mock_get.return_value = {"enabled": True}
        mock_odds.return_value = None
        result = SignalEngine.evaluate_bookmaker_gap("Man City vs Arsenal", "YES", 0.55)
        self.assertIsNone(result)


class TestSoccerLeagueMultipliers(unittest.TestCase):
    """验证足球联赛质量乘数配置存在"""

    def test_config_has_league_multipliers(self):
        from src.utils.config_loader import get

        mults = get("paper_trading.sports.soccer_league_multipliers")
        self.assertIsNotNone(mults)
        self.assertIn("champions league", mults)
        self.assertIn("world cup", mults)
        self.assertIn("premier league", mults)

    def test_default_multiplier_exists(self):
        from src.utils.config_loader import get

        mults = get("paper_trading.sports.soccer_league_multipliers")
        self.assertIn("default", mults)
        self.assertLessEqual(mults["default"], 1.0)

    def test_moneyline_only_config(self):
        from src.utils.config_loader import get

        val = get("paper_trading.sports.moneyline_only")
        self.assertTrue(val)

    def test_bookmaker_gap_config(self):
        from src.utils.config_loader import get

        cfg = get("paper_trading.sports.bookmaker_gap")
        self.assertIsNotNone(cfg)
        self.assertTrue(cfg.get("enabled"))
        self.assertIn("strong_gap_threshold", cfg)
        self.assertIn("medium_gap_threshold", cfg)


class TestBlacklistKeywordsExpanded(unittest.TestCase):
    """验证 scanner blacklist_keywords 已扩展"""

    def test_ou_in_blacklist(self):
        from src.utils.config_loader import get

        keywords = get("scanner.blacklist_keywords")
        self.assertTrue(any("o/u" in kw.lower() for kw in keywords))

    def test_handicap_in_blacklist(self):
        from src.utils.config_loader import get

        keywords = get("scanner.blacklist_keywords")
        self.assertTrue(any("handicap" in kw.lower() for kw in keywords))

    def test_over_under_in_blacklist(self):
        from src.utils.config_loader import get

        keywords = get("scanner.blacklist_keywords")
        self.assertTrue(any("over/under" in kw.lower() for kw in keywords))

    def test_corners_in_blacklist(self):
        from src.utils.config_loader import get

        keywords = get("scanner.blacklist_keywords")
        self.assertTrue(any("corners" in kw.lower() for kw in keywords))


if __name__ == "__main__":
    unittest.main()
