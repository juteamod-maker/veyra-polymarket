"""T121F: 验证 query_city_no_performance SQL 扩展覆盖所有天气投注类型。"""

import time
import unittest

from src.utils import db as _db


def _insert_resolved_bet(
    db_path, bet_id, question, category, strategy_tag, is_win, pnl
):
    _db.insert_paper_bet(
        {
            "bet_id": bet_id,
            "ts_entered": time.time(),
            "market_id": f"mkt-{bet_id}",
            "question": question,
            "category": category,
            "outcome": "NO",
            "confidence": 0.7,
            "ai_probability": 0.3,
            "market_price": 0.30,
            "ev": 0.05,
            "size_usdc": 2.0,
            "sim_balance_before": 100.0,
            "sim_balance_after": 98.0,
            "market_end_date": "",
            "weather_context": "",
            "ai_models": "test",
            "cycle_id": "test-cycle",
            "strategy_tag": strategy_tag,
        },
        db_path=db_path,
    )
    from src.utils.db import connect

    with connect(db_path) as conn:
        conn.execute(
            """UPDATE paper_bets SET resolved_outcome=?, is_win=?, pnl=?
               WHERE bet_id=?""",
            ("YES" if is_win else "NO", is_win, pnl, bet_id),
        )


class TestQueryCityNoPerformanceExpanded(unittest.TestCase):
    """扩展查询覆盖全部天气策略标签。"""

    def _setup_db(self, tmp_path):
        db_path = tmp_path / "test.db"
        _db.init_db(db_path)
        # 各类天气策略标签
        _insert_resolved_bet(
            db_path,
            "b1",
            "Will the high temperature in Tokyo be 25°C?",
            "weather",
            "city_no",
            True,
            1.5,
        )
        _insert_resolved_bet(
            db_path,
            "b2",
            "Will the high temperature in Seoul be 20°C?",
            "weather",
            "t268_city_no",
            False,
            -2.0,
        )
        _insert_resolved_bet(
            db_path,
            "b3",
            "Will the high temperature in Shanghai be 28°C?",
            "weather",
            "weather_right_tail_left",
            True,
            0.8,
        )
        _insert_resolved_bet(
            db_path,
            "b4",
            "Will the high temperature in Shanghai be 30°C?",
            "weather",
            "weather_right_tail_right",
            True,
            1.2,
        )
        _insert_resolved_bet(
            db_path,
            "b5",
            "Will the high temperature in Tokyo be 26°C?",
            "weather",
            "weather_yes_ambush",
            False,
            -1.0,
        )
        # 早期天气投注 (strategy_tag=None, category=weather)
        _insert_resolved_bet(
            db_path,
            "b6",
            "Will the high temperature in Seoul be 22°C?",
            "weather",
            None,
            True,
            0.5,
        )
        # 非天气投注 (should NOT be included)
        _insert_resolved_bet(
            db_path, "b7", "Will Bitcoin reach 100K?", "crypto", None, True, 5.0
        )
        _insert_resolved_bet(
            db_path, "b8", "Will team A win?", "sports", "some_sports_tag", True, 3.0
        )
        return db_path

    def test_covers_all_weather_tags(self, tmp_path=None):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            db_path = self._setup_db(Path(td))
            result = _db.query_city_no_performance(db_path=db_path)
            # Should include b1-b6 (6 weather bets), exclude b7/b8
            self.assertEqual(result["total"], 6)
            self.assertEqual(result["wins"], 4)  # b1, b3, b4, b6
            self.assertAlmostEqual(result["pnl"], 1.0, places=2)  # 1.5-2+0.8+1.2-1+0.5

    def test_by_strategy_breakdown(self, tmp_path=None):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            db_path = self._setup_db(Path(td))
            result = _db.query_city_no_performance(db_path=db_path)
            by_strat = result.get("by_strategy", {})
            self.assertIn("city_no", by_strat)
            self.assertIn("weather_yes_ambush", by_strat)
            self.assertIn("legacy", by_strat)  # strategy_tag=None → "legacy"
            self.assertEqual(by_strat["city_no"]["total"], 1)
            self.assertEqual(by_strat["weather_yes_ambush"]["total"], 1)
            self.assertEqual(by_strat["legacy"]["total"], 1)

    def test_by_city_breakdown(self, tmp_path=None):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            db_path = self._setup_db(Path(td))
            result = _db.query_city_no_performance(db_path=db_path)
            by_city = result.get("by_city", {})
            self.assertIn("Tokyo", by_city)
            self.assertIn("Shanghai", by_city)
            self.assertIn("Seoul", by_city)
            self.assertEqual(by_city["Tokyo"]["total"], 2)  # b1 + b5
            self.assertEqual(by_city["Shanghai"]["total"], 2)  # b3 + b4

    def test_excludes_non_weather(self, tmp_path=None):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            db_path = self._setup_db(Path(td))
            result = _db.query_city_no_performance(db_path=db_path)
            by_strat = result.get("by_strategy", {})
            self.assertNotIn("some_sports_tag", by_strat)
            # crypto bet with None tag but category=crypto should not appear
            self.assertEqual(result["total"], 6)


if __name__ == "__main__":
    unittest.main()
