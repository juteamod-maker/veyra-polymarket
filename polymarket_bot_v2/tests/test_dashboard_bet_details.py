from unittest.mock import patch

from scripts.dashboard_routes.data import (
    _build_display_reasoning,
    _refresh_market_end_date_for_display,
)


def _make_weather_bet(**overrides):
    bet = {
        "category": "weather",
        "outcome": "NO",
        "strategy_tag": "weather_double_no_right",
        "bet_bucket": "weather",
        "question": "Will the highest temperature in Helsinki be 11°C on April 11?",
        "ai_probability": 0.5,
        "market_price": 0.7,
        "ev": 0.20024,
        "confidence": 0.8332352,
        "weather_context": "",
        "strategy_params": None,
        "ai_reasoning": "",
        "market_id": "1902121",
        "market_end_date": "2026-04-11T23:59:59Z",
    }
    bet.update(overrides)
    return bet


@patch(
    "scripts.dashboard_routes.data._fetch_live_meteo_parts",
    return_value=["GFS峰值=18°C", "ECMWF峰值=19°C", "阈值=11°C"],
)
def test_weather_no_reasoning_uses_no_entry_price_and_live_meteo(_mock_live_parts):
    text = _build_display_reasoning(_make_weather_bet())

    assert "📐 策略: 右翼NO（高于预测峰值）" in text
    assert "NO入场价: 0.700" in text
    assert "买入NO花费 70.0¢/份" in text
    assert "原始气象概率未持久化" in text
    assert "🌤️ 气象数据: GFS峰值=18°C, ECMWF峰值=19°C, 阈值=11°C" in text


@patch(
    "scripts.dashboard_routes.data._fetch_live_meteo_parts",
    return_value=["GFS峰值=18°C", "ECMWF峰值=19°C"],
)
def test_existing_legacy_weather_reasoning_is_enriched(_mock_live_parts):
    legacy = (
        "📐 策略: 右翼NO（高于预测峰值）\n"
        "📋 市场: Will the highest temperature in Helsinki be 11°C on April 11?"
    )
    text = _build_display_reasoning(_make_weather_bet(ai_reasoning=legacy))

    assert text.startswith("📐 策略: 右翼NO（高于预测峰值）")
    assert "🌤️ 气象数据: GFS峰值=18°C, ECMWF峰值=19°C" in text


@patch("scripts.dashboard_routes.data.requests.get")
def test_refresh_market_end_date_for_display_uses_authoritative_gamma(mock_get):
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {"endDate": "2026-04-11T12:00:00Z"}

    bet = _make_weather_bet()
    _refresh_market_end_date_for_display(bet)

    assert bet["market_end_date"] == "2026-04-11T12:00:00Z"
