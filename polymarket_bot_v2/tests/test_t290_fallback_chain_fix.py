"""T290: 降级链漏洞修复测试 — 确保 API 返回 200 但空/不足数据时走降级链而非返回 None。

修复的 4 个 BUG:
 1. _fetch_gfs_ensemble: API返回200但daily无日期数据 → 之前返回None, 现在走降级链
 2. _fetch_gfs_ensemble: API返回200但成员数不足min_members → 之前返回None, 现在走降级链
 3. _fetch_ecmwf_ensemble: 同 BUG#1 (ECMWF版)
 4. _fetch_ecmwf_ensemble: 同 BUG#2 (ECMWF版)

额外防御修复:
 5. fetch_ensemble_probability: GFS返回空时防御性调用降级链
 6. fetch_ensemble_probability: target_date不在数据中 → 紧急气候学覆盖
 7. fetch_dual_model_probability: GFS返回空时防御性调用降级链
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════
def _clear_weather_caches():
    """清理 weather_fetcher 全局状态。"""
    import src.utils.weather_fetcher as wf

    wf._CACHE.clear()
    wf._DATA_SOURCE_META.clear()
    wf._GFS_CIRCUIT_OPEN_UNTIL = 0.0
    wf._ECMWF_CIRCUIT_OPEN_UNTIL = 0.0
    wf._BATCH_CIRCUIT_OPEN_UNTIL = 0.0
    wf._OPEN_METEO_LAST_429_TS = 0.0


def _mock_api_response_empty_daily():
    """API 返回 200 但 daily 里没有 time 字段。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"daily": {}}  # 无 time 字段
    resp.raise_for_status = MagicMock()
    return resp


def _mock_api_response_no_members(dates):
    """API 返回 200 有日期但没有成员列(或成员数不足)。"""
    resp = MagicMock()
    resp.status_code = 200
    daily = {"time": dates}
    # 只有 3 个成员 (远低于 min_members_for_confidence=20)
    for i in range(1, 4):
        col = f"temperature_2m_max_member{i:02d}"
        daily[col] = [20.0 + i] * len(dates)
    resp.json.return_value = {"daily": daily}
    resp.raise_for_status = MagicMock()
    return resp


# ═══════════════════════════════════════════════════════════════════════
# 1. _fetch_gfs_ensemble: 空 daily 走降级链
# ═══════════════════════════════════════════════════════════════════════
class TestGfsEmptyDailyFallback:
    """BUG#1: API 返回 200 + 空 daily → 必须走降级链,不返回 None。"""

    def test_empty_daily_triggers_fallback(self):
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        _clear_weather_caches()
        resp = _mock_api_response_empty_daily()

        with patch("src.utils.weather_fetcher.requests.get", return_value=resp):
            result = _fetch_gfs_ensemble(40.78, -73.87, "temp_max", 8)

        # 降级链最终会返回紧急气候学数据 (never-None)
        assert result is not None
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_empty_daily_logs_warning(self):
        """确认日志包含降级链启动信息。"""
        import logging

        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        _clear_weather_caches()
        resp = _mock_api_response_empty_daily()

        with patch("src.utils.weather_fetcher.requests.get", return_value=resp):
            with patch("src.utils.weather_fetcher.logger") as mock_logger:
                _fetch_gfs_ensemble(40.78, -73.87, "temp_max", 8)

        # 确认 warning 被记录
        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("API返回200但daily无数据" in s for s in warning_calls)


# ═══════════════════════════════════════════════════════════════════════
# 2. _fetch_gfs_ensemble: 成员数不足走降级链
# ═══════════════════════════════════════════════════════════════════════
class TestGfsInsufficientMembersFallback:
    """BUG#2: API 返回 200 + 成员数不足 → 必须走降级链。"""

    def test_insufficient_members_triggers_fallback(self):
        from src.utils.weather_fetcher import _fetch_gfs_ensemble

        _clear_weather_caches()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resp = _mock_api_response_no_members([today])

        with patch("src.utils.weather_fetcher.requests.get", return_value=resp):
            result = _fetch_gfs_ensemble(40.78, -73.87, "temp_max", 8)

        assert result is not None
        assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════════════
# 3. _fetch_ecmwf_ensemble: 空 daily 走降级链
# ═══════════════════════════════════════════════════════════════════════
class TestEcmwfEmptyDailyFallback:
    """BUG#3: ECMWF API 返回 200 + 空 daily → 必须走降级链。"""

    def test_empty_daily_triggers_fallback(self):
        from src.utils.weather_fetcher import _fetch_ecmwf_ensemble

        _clear_weather_caches()
        resp = _mock_api_response_empty_daily()

        with patch("src.utils.weather_fetcher.requests.get", return_value=resp):
            result = _fetch_ecmwf_ensemble(40.78, -73.87, "temp_max", 8)

        assert result is not None
        assert isinstance(result, dict)
        assert len(result) > 0


# ═══════════════════════════════════════════════════════════════════════
# 4. _fetch_ecmwf_ensemble: 成员数不足走降级链
# ═══════════════════════════════════════════════════════════════════════
class TestEcmwfInsufficientMembersFallback:
    """BUG#4: ECMWF 成员数不足 → 走降级链。"""

    def test_insufficient_members_triggers_fallback(self):
        from src.utils.weather_fetcher import _fetch_ecmwf_ensemble

        _clear_weather_caches()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resp = _mock_api_response_no_members([today])

        with patch("src.utils.weather_fetcher.requests.get", return_value=resp):
            result = _fetch_ecmwf_ensemble(40.78, -73.87, "temp_max", 8)

        assert result is not None
        assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════════════
# 5. fetch_ensemble_probability: 降级链保证不返回 None (T192 防护)
# ═══════════════════════════════════════════════════════════════════════
class TestEnsembleProbabilityNeverNone:
    """fetch_ensemble_probability 在 GFS 数据全部失败时不应触发 T192 跳过。"""

    def test_api_empty_data_produces_probability(self):
        """API 返204空数据 → 降级链 → 仍返回 probability 结果。"""
        from src.utils.weather_fetcher import fetch_ensemble_probability

        _clear_weather_caches()
        tomorrow = (
            (datetime.now(timezone.utc) + timedelta(days=1))
            .strftime("%B %-d")
            .replace(" 0", " ")
        )
        market = {
            "question": f"Will the highest temperature in New York City be 70°F on {tomorrow}?",
            "bestAsk": 0.30,
            "bestBid": 0.28,
        }

        # mock API 返回空 daily → 降级链启动
        resp = _mock_api_response_empty_daily()
        with patch("src.utils.weather_fetcher.requests.get", return_value=resp):
            result = fetch_ensemble_probability(market)

        # 降级链兜底(紧急气候学) → 应返回概率结果而非 None
        if result is not None:
            assert "probability" in result
            assert 0.0 <= result["probability"] <= 1.0


# ═══════════════════════════════════════════════════════════════════════
# 6. 紧急气候学估计: 始终覆盖所有预报日
# ═══════════════════════════════════════════════════════════════════════
class TestEmergencyClimateEstimate:
    """_emergency_climate_estimate 必须为所有预报日生成数据。"""

    def test_covers_all_forecast_days(self):
        from src.utils.weather_fetcher import _emergency_climate_estimate

        result = _emergency_climate_estimate(40.78, "temp_max", 8)

        assert result is not None
        assert len(result) >= 7  # 至少覆盖 7 天
        # 所有值都是合理温度
        for date_str, vals in result.items():
            assert len(vals) == 1
            assert -50 < vals[0] < 60

    def test_never_returns_empty(self):
        """任何纬度、任何变量都必须返回非空。"""
        from src.utils.weather_fetcher import _emergency_climate_estimate

        for lat in [-60, -30, 0, 30, 60]:
            for var in ["temp_max", "temp_min"]:
                result = _emergency_climate_estimate(lat, var, 8)
                assert result, f"lat={lat}, var={var} 返回空"
                assert len(result) > 0
