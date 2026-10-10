"""Tests for M5-T251: 城市历史准确率追踪模块（city_accuracy_tracker）。"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# TestCityAccuracyTracker — 单元测试（mock DB）
# ─────────────────────────────────────────────────────────────────────────────


class TestCityAccuracyTracker:
    """M5 城市历史准确率追踪模块测试。"""

    def setup_method(self):
        """每次测试前清除 TTL 缓存。"""
        from src.utils import city_accuracy_tracker

        city_accuracy_tracker.invalidate_cache()

    def _make_db_result(self, by_city: dict) -> dict:
        return {
            "total": sum(v["total"] for v in by_city.values()),
            "wins": sum(v["wins"] for v in by_city.values()),
            "win_rate": 0.5,
            "pnl": 0.0,
            "by_city": {
                city: {
                    "total": v["total"],
                    "wins": v["wins"],
                    "pnl": v.get("pnl", 0.0),
                    "win_rate": (
                        round(v["wins"] / v["total"], 4) if v["total"] > 0 else 0.0
                    ),
                }
                for city, v in by_city.items()
            },
            "by_strategy": {},
        }

    def test_returns_empty_when_db_fails(self):
        """DB查询异常时返回空字典，不抛出异常。"""
        with patch("src.utils.city_accuracy_tracker.get_city_accuracy_stats") as mock:
            mock.side_effect = Exception("DB error")
            # 直接测试 get_city_accuracy_stats 内部 fallback
            from src.utils import city_accuracy_tracker

            city_accuracy_tracker.invalidate_cache()

        # 模拟 query_city_no_performance_windowed 失败
        with patch(
            "src.utils.city_accuracy_tracker.get_city_accuracy_stats",
            side_effect=Exception("DB error"),
        ):
            # 使用直接调用，捕获内部异常路径
            pass

        # 验证正常情况下不抛出
        with patch("src.utils.db.query_city_no_performance_windowed") as mock_db:
            mock_db.side_effect = Exception("DB连接失败")
            from src.utils.city_accuracy_tracker import get_city_accuracy_stats

            result = get_city_accuracy_stats(window_days=30)
            assert result == {}, "DB失败时应返回空字典"

    def test_accuracy_computed_correctly(self):
        """高样本城市应正确计算准确率。"""
        from src.utils import city_accuracy_tracker

        city_accuracy_tracker.invalidate_cache()

        db_data = self._make_db_result(
            {
                "Miami": {"total": 10, "wins": 7, "pnl": 2.5},
                "London": {"total": 8, "wins": 3, "pnl": -1.0},
                "Tokyo": {"total": 3, "wins": 2, "pnl": 0.5},  # 样本不足 (min_sample=5)
            }
        )

        with patch(
            "src.utils.db.query_city_no_performance_windowed",
            return_value=db_data,
        ):
            result = city_accuracy_tracker.get_city_accuracy_stats(
                window_days=30, min_sample=5
            )

        assert "Miami" in result
        assert result["Miami"]["accuracy"] == 0.7  # 7/10
        assert result["Miami"]["total"] == 10

        assert "London" in result
        assert result["London"]["accuracy"] == 0.375  # 3/8

        # Tokyo 样本不足，accuracy 应为 None
        assert "Tokyo" in result
        assert result["Tokyo"]["accuracy"] is None, "样本不足时accuracy应为None"
        assert result["Tokyo"]["total"] == 3

    def test_low_accuracy_city_detected(self):
        """低准确率城市（< threshold）应正确标记。"""
        from src.utils import city_accuracy_tracker

        city_accuracy_tracker.invalidate_cache()

        db_data = self._make_db_result(
            {
                "BadCity": {"total": 10, "wins": 3},  # 30% = 低准确率
                "GoodCity": {"total": 10, "wins": 8},  # 80% = 正常
            }
        )

        with patch(
            "src.utils.db.query_city_no_performance_windowed",
            return_value=db_data,
        ):
            result = city_accuracy_tracker.get_city_accuracy_stats(
                window_days=30, min_sample=5
            )

        assert result["BadCity"]["accuracy"] == 0.3
        assert result["BadCity"]["accuracy"] < 0.45  # 低于默认阈值

        assert result["GoodCity"]["accuracy"] == 0.8
        assert result["GoodCity"]["accuracy"] >= 0.45  # 不触发惩罚

    def test_ttl_cache_works(self):
        """TTL 缓存：同 window_days 第二次调用不重查 DB。"""
        from src.utils import city_accuracy_tracker

        city_accuracy_tracker.invalidate_cache()

        db_data = self._make_db_result({"Miami": {"total": 10, "wins": 7}})

        with patch(
            "src.utils.db.query_city_no_performance_windowed",
            return_value=db_data,
        ) as mock_db:
            r1 = city_accuracy_tracker.get_city_accuracy_stats(window_days=30)
            r2 = city_accuracy_tracker.get_city_accuracy_stats(window_days=30)

        assert mock_db.call_count == 1, "TTL内第二次调用不应重查DB"
        assert r1 == r2

    def test_cache_invalidated_on_window_change(self):
        """不同 window_days 应重新查 DB。"""
        from src.utils import city_accuracy_tracker

        city_accuracy_tracker.invalidate_cache()

        db_data = self._make_db_result({"Miami": {"total": 10, "wins": 7}})

        with patch(
            "src.utils.db.query_city_no_performance_windowed",
            return_value=db_data,
        ) as mock_db:
            city_accuracy_tracker.get_city_accuracy_stats(window_days=30)
            city_accuracy_tracker.get_city_accuracy_stats(window_days=7)

        assert mock_db.call_count == 2, "不同window_days应各查一次DB"


# ─────────────────────────────────────────────────────────────────────────────
# TestQueryCityNoPerformanceWindowed — DB 函数测试
# ─────────────────────────────────────────────────────────────────────────────


class TestQueryCityNoPerformanceWindowed:
    """测试 db.query_city_no_performance_windowed 的时间窗口参数。"""

    def test_windowed_function_exists(self):
        """函数应可正常导入。"""
        from src.utils.db import query_city_no_performance_windowed

        assert callable(query_city_no_performance_windowed)

    def test_windowed_function_signature(self):
        """windowed 函数接受 window_days 参数（接口检验）。"""
        import inspect

        from src.utils.db import query_city_no_performance_windowed

        sig = inspect.signature(query_city_no_performance_windowed)
        params = list(sig.parameters.keys())
        assert "window_days" in params
        assert "db_path" in params

    def test_original_function_delegates_to_windowed(self):
        """原 query_city_no_performance 应委托给 windowed 版本（向后兼容）。"""
        import src.utils.db as db_mod

        with patch.object(
            db_mod,
            "query_city_no_performance_windowed",
            return_value={
                "total": 0,
                "wins": 0,
                "win_rate": 0.0,
                "pnl": 0.0,
                "by_city": {},
                "by_strategy": {},
            },
        ) as mock_w:
            result = db_mod.query_city_no_performance()
            assert mock_w.called, "原函数应调用 windowed 版本"
            assert "total" in result

    def test_windowed_mock_result_has_win_rate_in_by_city(self):
        """windowed 函数通过 mock 验证返回结构包含 win_rate in by_city。"""
        import src.utils.db as db_mod

        # 验证 query_city_no_performance_windowed 返回的 by_city 每个城市有 win_rate
        mock_rows = [
            {
                "question": "Will the high temperature in Miami exceed 90°F",
                "strategy_tag": "weather_right_tail_right",
                "is_win": 1,
                "pnl": 1.0,
            },
            {
                "question": "Will the high temperature in Miami exceed 90°F",
                "strategy_tag": "weather_right_tail_right",
                "is_win": 0,
                "pnl": -0.5,
            },
        ]

        class FakeConn:
            def execute(self, *args, **kwargs):
                return self

            def fetchall(self):
                return mock_rows

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def close(self):
                pass

            def cursor(self):
                return self

        with patch("src.utils.db.connect", return_value=FakeConn()):
            result = db_mod.query_city_no_performance_windowed(window_days=30)
        assert "by_city" in result
        for city_name, city_data in result["by_city"].items():
            assert "win_rate" in city_data, f"{city_name} 缺少 win_rate 字段"
            assert "win_rate" in city_data, f"{city_name} 缺少 win_rate 字段"
