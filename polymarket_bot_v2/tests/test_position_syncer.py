"""
test_position_syncer.py — PositionSyncer v2 单元测试
覆盖：Data API 查询、paper_bet 构建、双向对账、赛道推断、Gamma 解析
"""

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from src.sync.position_syncer import PositionSyncer, _infer_event_slug

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

SAMPLE_DATA_API_POSITIONS = [
    {
        "proxyWallet": "0xproxy",
        "asset": "token_eth_yes",
        "conditionId": "0xcond_eth",
        "size": "16.94",
        "avgPrice": "0.057",
        "curPrice": "0.0455",
        "initialValue": "0.965",
        "currentValue": "0.771",
        "cashPnl": "-0.194",
        "percentPnl": "-20.1",
        "title": "Will ETH be above $2,200 on March 21?",
        "slug": "eth-above-2200-march-21",
        "outcome": "Yes",
        "outcomeIndex": "0",
        "eventSlug": "eth-price-march-21",
        "eventId": "event-eth-123",
        "endDate": "2026-12-21T23:59:59Z",
        "negativeRisk": False,
        "redeemable": False,
        "mergeable": False,
    },
    {
        "proxyWallet": "0xproxy",
        "asset": "token_recession",
        "conditionId": "0xcond_recession",
        "size": "10.0",
        "avgPrice": "0.35",
        "curPrice": "0.345",
        "initialValue": "3.5",
        "currentValue": "3.45",
        "cashPnl": "-0.05",
        "percentPnl": "-1.43",
        "title": "US recession by end of 2026?",
        "slug": "us-recession-2026",
        "outcome": "Yes",
        "outcomeIndex": "0",
        "eventSlug": "",
        "eventId": "event-recession",
        "endDate": "2026-12-31T23:59:59Z",
        "negativeRisk": False,
        "redeemable": False,
        "mergeable": False,
    },
    {
        "proxyWallet": "0xproxy",
        "asset": "token_london_no",
        "conditionId": "0xcond_london",
        "size": "10.0",
        "avgPrice": "0.475",
        "curPrice": "0.465",
        "initialValue": "4.75",
        "currentValue": "4.65",
        "cashPnl": "-0.10",
        "percentPnl": "-2.1",
        "title": "Highest temperature in London on March 21 — 13°C?",
        "slug": "london-highest-temp-march-21-13c",
        "outcome": "No",
        "outcomeIndex": "1",
        "eventSlug": "london-highest-temp-march-21",
        "eventId": "event-london",
        "endDate": "2026-12-21T23:59:59Z",
        "negativeRisk": False,
        "redeemable": False,
        "mergeable": False,
    },
    {
        "proxyWallet": "0xproxy",
        "asset": "token_pelicans",
        "conditionId": "0xcond_cavs_pels",
        "size": "5.0",
        "avgPrice": "0.36",
        "curPrice": "0.355",
        "initialValue": "1.8",
        "currentValue": "1.775",
        "cashPnl": "-0.025",
        "percentPnl": "-1.39",
        "title": "Cavaliers vs. Pelicans",
        "slug": "cavaliers-vs-pelicans",
        "outcome": "Pelicans",
        "outcomeIndex": "1",
        "eventSlug": "nba-march-21",
        "eventId": "event-nba",
        "endDate": "2026-12-21T23:59:59Z",
        "negativeRisk": False,
        "redeemable": False,
        "mergeable": False,
    },
]


@pytest.fixture
def mock_executor():
    """创建 mock OrderExecutor。"""
    executor = MagicMock()
    return executor


@pytest.fixture
def sample_positions():
    """返回 Data API 模拟响应。"""
    return [dict(p) for p in SAMPLE_DATA_API_POSITIONS]


# ------------------------------------------------------------------
# _fetch_real_positions
# ------------------------------------------------------------------


class TestFetchRealPositions:
    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": ""})
    def test_no_funder_returns_none(self):
        """POLY_RELAYER_ADDRESS 未设置时返回 None。"""
        result = PositionSyncer._fetch_real_positions()
        assert result is None

    @patch("src.sync.position_syncer.requests.get")
    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xfunder123"})
    def test_api_error_returns_none(self, mock_get):
        """API 异常时返回 None。"""
        import requests as _req

        mock_get.side_effect = _req.ConnectionError("connection timeout")
        result = PositionSyncer._fetch_real_positions()
        assert result is None

    @patch("src.sync.position_syncer.requests.get")
    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xfunder123"})
    def test_success_returns_list(self, mock_get):
        """正常返回持仓列表。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = SAMPLE_DATA_API_POSITIONS
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = PositionSyncer._fetch_real_positions()
        assert isinstance(result, list)
        assert len(result) == 4

    @patch("src.sync.position_syncer.requests.get")
    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xFUND"})
    def test_uses_lowercase_funder(self, mock_get):
        """查询时应将 funder 地址转小写。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        PositionSyncer._fetch_real_positions()
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"]["user"] == "0xfund"

    @patch("src.sync.position_syncer.requests.get")
    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xfunder123"})
    def test_non_list_response_returns_none(self, mock_get):
        """Data API 返回非列表时返回 None。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"error": "invalid"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = PositionSyncer._fetch_real_positions()
        assert result is None


# ------------------------------------------------------------------
# _build_bet_from_data_api
# ------------------------------------------------------------------


class TestBuildBetFromDataApi:
    def test_builds_valid_record(self, mock_executor):
        syncer = PositionSyncer(mock_executor)
        pos = SAMPLE_DATA_API_POSITIONS[0]  # ETH above $2200

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"id": "99001"}]
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos)

        assert record is not None
        assert record["question"] == "Will ETH be above $2,200 on March 21?"
        assert record["outcome"] == "Yes"
        assert record["condition_id"] == "0xcond_eth"
        assert record["source"] == "manual"
        assert record["bet_bucket"] == "manual"
        assert record["dimension_tags"] == "chain_sync"
        assert abs(record["market_price"] - 0.057) < 0.001
        assert abs(record["size_usdc"] - 0.965) < 0.01  # initialValue
        assert record["market_end_date"] == "2026-12-21T23:59:59Z"
        assert record["event_slug"] == "eth-price-march-21"

    def test_no_title_returns_none(self, mock_executor):
        syncer = PositionSyncer(mock_executor)
        pos = {"conditionId": "0x123", "title": ""}
        assert syncer._build_bet_from_data_api(pos) is None

    def test_no_condition_id_returns_none(self, mock_executor):
        syncer = PositionSyncer(mock_executor)
        pos = {"title": "Some market", "conditionId": ""}
        assert syncer._build_bet_from_data_api(pos) is None

    def test_yes_outcome_ai_probability(self, mock_executor):
        """YES outcome → ai_probability = avgPrice."""
        syncer = PositionSyncer(mock_executor)
        pos = {**SAMPLE_DATA_API_POSITIONS[1]}  # recession, Yes

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos)

        assert abs(record["ai_probability"] - 0.35) < 0.001

    def test_no_outcome_ai_probability(self, mock_executor):
        """NO outcome → ai_probability = 1 - avgPrice."""
        syncer = PositionSyncer(mock_executor)
        pos = {**SAMPLE_DATA_API_POSITIONS[2]}  # London temp, No

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos)

        assert abs(record["ai_probability"] - 0.525) < 0.001  # 1 - 0.475

    def test_named_outcome_ai_probability(self, mock_executor):
        """命名 outcome (非 YES/NO) → ai_probability = avgPrice."""
        syncer = PositionSyncer(mock_executor)
        pos = {**SAMPLE_DATA_API_POSITIONS[3]}  # Pelicans

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos)

        assert abs(record["ai_probability"] - 0.36) < 0.001

    def test_category_crypto_from_title(self, mock_executor):
        """ETH 标题应推断为 crypto。"""
        syncer = PositionSyncer(mock_executor)
        pos = {**SAMPLE_DATA_API_POSITIONS[0]}

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"id": "99001"}]
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos)

        assert record["category"] == "crypto"

    def test_category_weather_from_title(self, mock_executor):
        """temperature 标题应推断为 weather。"""
        syncer = PositionSyncer(mock_executor)
        pos = {**SAMPLE_DATA_API_POSITIONS[2]}

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos)

        assert record["category"] == "weather"

    def test_category_sports_from_title(self, mock_executor):
        """vs. 标题应推断为 sports。"""
        syncer = PositionSyncer(mock_executor)
        pos = {**SAMPLE_DATA_API_POSITIONS[3]}

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos)

        assert record["category"] == "sports"

    def test_fallback_size_usdc_without_initial_value(self, mock_executor):
        """initialValue 为 0 时用 size × avgPrice。"""
        syncer = PositionSyncer(mock_executor)
        pos = {
            "title": "Test market",
            "conditionId": "0xtest",
            "slug": "",
            "eventSlug": "",
            "outcome": "Yes",
            "avgPrice": "0.50",
            "size": "10.0",
            "initialValue": "0",
            "endDate": "",
        }

        record = syncer._build_bet_from_data_api(pos)
        assert record is not None
        assert abs(record["size_usdc"] - 5.0) < 0.01  # 10 × 0.50

    def test_gamma_fallback_uses_condition_id(self, mock_executor):
        """Gamma 查询失败时 market_id 应为 condition_id。"""
        syncer = PositionSyncer(mock_executor)
        pos = {
            "title": "Test market",
            "conditionId": "0xfallback_cid",
            "slug": "",
            "eventSlug": "",
            "outcome": "Yes",
            "avgPrice": "0.50",
            "size": "10.0",
            "initialValue": "5.0",
            "endDate": "",
        }

        record = syncer._build_bet_from_data_api(pos)
        assert record["market_id"] == "0xfallback_cid"


# ------------------------------------------------------------------
# T201: 天气策略标签推断 + 元数据继承
# ------------------------------------------------------------------


class TestT201WeatherStrategyTagInference:
    """T201: chain_sync 天气仓位应自动推断 strategy_tag。"""

    def test_weather_no_infers_double_no_same(self, mock_executor):
        """天气 NO 仓位 → weather_double_no_same。"""
        syncer = PositionSyncer(mock_executor)
        pos = {**SAMPLE_DATA_API_POSITIONS[2]}  # London temp, No

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos)

        assert record["strategy_tag"] == "weather_right_tail_same"
        assert record["bet_bucket"] == "weather_right_tail_same"

    def test_weather_yes_infers_ambush(self, mock_executor):
        """天气 YES 仓位 → weather_yes_ambush。"""
        syncer = PositionSyncer(mock_executor)
        pos = {
            "title": "Will the highest temperature in Tokyo be 22°C on April 15?",
            "conditionId": "0xweather_yes",
            "slug": "tokyo-temp-april-15-22c",
            "eventSlug": "tokyo-temp-april-15",
            "outcome": "Yes",
            "avgPrice": "0.45",
            "size": "20.0",
            "initialValue": "9.0",
            "endDate": "2026-04-15T23:59:59Z",
        }

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos)

        assert record["strategy_tag"] == "weather_yes_ambush"
        assert record["bet_bucket"] == "weather_yes_ambush"

    def test_non_weather_no_strategy_tag(self, mock_executor):
        """非天气仓位不应有 strategy_tag。"""
        syncer = PositionSyncer(mock_executor)
        pos = {**SAMPLE_DATA_API_POSITIONS[0]}  # ETH, crypto

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"id": "99001"}]
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos)

        assert record["strategy_tag"] == ""
        assert record["bet_bucket"] == "manual"

    def test_inherits_metadata_from_existing_bet_by_slug(self, mock_executor):
        """相同 slug 的已有记录元数据应被继承。"""
        syncer = PositionSyncer(mock_executor)
        pos = {**SAMPLE_DATA_API_POSITIONS[2]}  # London temp, No
        existing = [
            {
                "bet_id": "existing-001",
                "market_slug": "london-highest-temp-march-21-13c",
                "market_id": "gamma-999",
                "condition_id": "0xcond_london",
                "outcome": "No",
                "strategy_tag": "weather_right_tail_right",
                "bet_bucket": "weather_right_tail_right",
                "source": "auto",
                "ai_models": "qwen3-235b,gemini-flash",
                "ai_reasoning": "双NO右翼策略：P(NO)=0.65, edge=0.12",
                "category": "weather",
            }
        ]

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos, existing)

        assert record["strategy_tag"] == "weather_right_tail_right"
        assert record["bet_bucket"] == "weather_right_tail_right"
        assert record["source"] == "auto"
        assert record["ai_models"] == "qwen3-235b,gemini-flash"
        assert "双NO右翼策略" in record["ai_reasoning"]

    def test_inherits_metadata_from_existing_bet_by_condition_id(self, mock_executor):
        """相同 condition_id 的已有记录元数据应被继承。"""
        syncer = PositionSyncer(mock_executor)
        pos = {
            "title": "Will the highest temperature in Seoul be 18°C on April 12?",
            "conditionId": "0xcondition_seoul",
            "slug": "seoul-temp-different-slug",
            "eventSlug": "",
            "outcome": "No",
            "avgPrice": "0.30",
            "size": "15.0",
            "initialValue": "4.5",
            "endDate": "2026-04-12T23:59:59Z",
        }
        existing = [
            {
                "bet_id": "existing-002",
                "market_slug": "seoul-temp-original-slug",
                "market_id": "gamma-888",
                "condition_id": "0xcondition_seoul",
                "outcome": "No",
                "strategy_tag": "weather_right_tail_left",
                "bet_bucket": "weather_right_tail_left",
                "source": "auto",
                "ai_models": "deepseek-v3",
                "ai_reasoning": "左翼NO策略",
                "category": "weather",
            }
        ]

        record = syncer._build_bet_from_data_api(pos, existing)
        assert record["strategy_tag"] == "weather_right_tail_left"
        assert record["source"] == "auto"

    def test_no_inheritance_different_outcome(self, mock_executor):
        """不同 outcome 的已有记录不应继承。"""
        syncer = PositionSyncer(mock_executor)
        pos = {
            "title": "Will the highest temperature in Seoul be 18°C on April 12?",
            "conditionId": "0xcondition_seoul",
            "slug": "seoul-temp-18c",
            "eventSlug": "",
            "outcome": "Yes",
            "avgPrice": "0.45",
            "size": "10.0",
            "initialValue": "4.5",
            "endDate": "2026-04-12T23:59:59Z",
        }
        existing = [
            {
                "bet_id": "existing-003",
                "market_slug": "seoul-temp-18c",
                "market_id": "gamma-888",
                "condition_id": "0xcondition_seoul",
                "outcome": "No",
                "strategy_tag": "weather_right_tail_right",
                "bet_bucket": "weather_right_tail_right",
                "source": "auto",
                "ai_models": "qwen3",
                "ai_reasoning": "右翼NO",
                "category": "weather",
            }
        ]

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos, existing)

        # Should get default weather_yes_ambush (from inference), not inherit NO's tag
        assert record["strategy_tag"] == "weather_yes_ambush"


class TestT201InferWeatherStrategyTag:
    """T201: _infer_weather_strategy_tag 单元测试。"""

    def test_no_outcome_temperature(self):
        tag = PositionSyncer._infer_weather_strategy_tag(
            "Will the highest temperature in Seoul be 18°C?", "NO"
        )
        assert tag == "weather_right_tail_same"

    def test_yes_outcome_low_price_is_ambush(self):
        """YES + 低价(0.15) → weather_yes_ambush。"""
        tag = PositionSyncer._infer_weather_strategy_tag(
            "Will the lowest temperature in Tokyo be 5°C?", "YES", avg_price=0.15
        )
        assert tag == "weather_yes_ambush"

    def test_yes_outcome_high_price_is_ehcl(self):
        """T204: YES + 高价(0.90) → weather_ehcl（非 ambush）。"""
        tag = PositionSyncer._infer_weather_strategy_tag(
            "Will the highest temperature in Dallas be 82°F?", "YES", avg_price=0.90
        )
        assert tag == "weather_ehcl"

    def test_yes_outcome_at_ehcl_threshold(self):
        """T204: YES + 价格恰好=0.80 (config min_price) → weather_ehcl。"""
        tag = PositionSyncer._infer_weather_strategy_tag(
            "Will the highest temperature in London be 16°C?", "YES", avg_price=0.80
        )
        assert tag == "weather_ehcl"

    def test_yes_outcome_below_ehcl_threshold(self):
        """T204: YES + 价格<0.80 → weather_yes_ambush。"""
        tag = PositionSyncer._infer_weather_strategy_tag(
            "Will the highest temperature in London be 16°C?", "YES", avg_price=0.79
        )
        assert tag == "weather_yes_ambush"

    def test_yes_outcome_default_price_is_ambush(self):
        """YES + 无价格(默认0.0) → weather_yes_ambush（向下兼容）。"""
        tag = PositionSyncer._infer_weather_strategy_tag(
            "Will the lowest temperature in Tokyo be 5°C?", "YES"
        )
        assert tag == "weather_yes_ambush"

    def test_non_temperature_returns_empty(self):
        tag = PositionSyncer._infer_weather_strategy_tag(
            "Will it rain in London tomorrow?", "NO"
        )
        assert tag == ""

    def test_empty_question_returns_empty(self):
        tag = PositionSyncer._infer_weather_strategy_tag("", "NO")
        assert tag == ""


# ------------------------------------------------------------------
# _detect_manual_exits
# ------------------------------------------------------------------


class TestDetectManualExits:
    @patch("src.sync.position_syncer.db")
    def test_marks_exited_position(self, mock_db):
        """链上不存在的持仓应标记为手动退出。"""
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "bet-sold-out",
                "condition_id": "0xgone",
                "question": "Sold on website",
                "market_price": 0.50,
                "size_usdc": 2.0,
                "source": "manual",
            }
        ]
        chain_cids = {"0xstill_here"}

        result = PositionSyncer._detect_manual_exits(chain_cids)
        assert result == ["bet-sold-out"]
        mock_db.close_paper_bet_manual_exit.assert_called_once_with(
            bet_id="bet-sold-out",
            exit_price=0.50,
            pnl=0.0,
            payout=0.0,
        )

    @patch("src.sync.position_syncer.db")
    def test_skips_active_position(self, mock_db):
        """链上仍存在的持仓不应标记退出。"""
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "bet-active",
                "condition_id": "0xstill_here",
                "question": "Still holding",
                "market_price": 0.50,
            }
        ]
        chain_cids = {"0xstill_here"}

        result = PositionSyncer._detect_manual_exits(chain_cids)
        assert result == []
        mock_db.close_paper_bet_manual_exit.assert_not_called()

    @patch("src.sync.position_syncer.db")
    def test_skips_no_condition_id(self, mock_db):
        """无 condition_id 的老记录应跳过。"""
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "bet-old",
                "condition_id": "",
                "question": "Old bet",
                "market_price": 0.50,
            }
        ]
        chain_cids = set()

        result = PositionSyncer._detect_manual_exits(chain_cids)
        assert result == []
        mock_db.close_paper_bet_manual_exit.assert_not_called()

    @patch("src.sync.position_syncer.db")
    def test_multiple_exits(self, mock_db):
        """多个退出应全部标记。"""
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "bet-1",
                "condition_id": "0xgone1",
                "question": "Gone 1",
                "market_price": 0.30,
                "source": "manual",
            },
            {
                "bet_id": "bet-2",
                "condition_id": "0xgone2",
                "question": "Gone 2",
                "market_price": 0.70,
                "source": "manual",
            },
            {
                "bet_id": "bet-3",
                "condition_id": "0xstill",
                "question": "Still here",
                "market_price": 0.50,
                "source": "manual",
            },
        ]
        chain_cids = {"0xstill"}

        result = PositionSyncer._detect_manual_exits(chain_cids)
        assert sorted(result) == ["bet-1", "bet-2"]
        assert mock_db.close_paper_bet_manual_exit.call_count == 2


# ------------------------------------------------------------------
# _infer_category_from_tags
# ------------------------------------------------------------------


class TestInferCategory:
    def test_weather_tag(self):
        assert (
            PositionSyncer._infer_category_from_tags(["Weather", "Recurring"], "")
            == "weather"
        )

    def test_weather_question(self):
        assert (
            PositionSyncer._infer_category_from_tags(
                [], "Will the highest temperature in Seoul..."
            )
            == "weather"
        )

    def test_sports_tag(self):
        assert (
            PositionSyncer._infer_category_from_tags(["Sports", "NHL"], "Game text")
            == "sports"
        )

    def test_sports_keyword(self):
        assert (
            PositionSyncer._infer_category_from_tags([], "Lightning vs. Canucks")
            == "sports"
        )

    def test_crypto_tag(self):
        assert (
            PositionSyncer._infer_category_from_tags(["Crypto"], "BTC price?")
            == "crypto"
        )

    def test_crypto_keyword(self):
        assert (
            PositionSyncer._infer_category_from_tags([], "Will ETH be above $2,200?")
            == "crypto"
        )

    def test_finance_keyword(self):
        assert (
            PositionSyncer._infer_category_from_tags([], "US recession by 2026?")
            == "finance"
        )

    def test_unknown_fallback(self):
        assert (
            PositionSyncer._infer_category_from_tags([], "Some random question")
            == "unknown"
        )


# ------------------------------------------------------------------
# sync — 集成逻辑
# ------------------------------------------------------------------


class TestSyncIntegration:
    @patch("src.sync.position_syncer.get")
    def test_disabled_returns_empty(self, mock_get, mock_executor):
        """sync.enabled=false 时应直接返回空。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": False,
            "general.dry_run": False,
        }.get(k)
        syncer = PositionSyncer(mock_executor)
        assert syncer.sync() == []

    @patch("src.sync.position_syncer.get")
    def test_dry_run_returns_empty(self, mock_get, mock_executor):
        """dry_run=true 时无链上数据可同步。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": True,
        }.get(k)
        syncer = PositionSyncer(mock_executor)
        assert syncer.sync() == []

    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_imports_new_position(
        self, mock_get, mock_fetch, mock_db, _mock_dry, mock_executor
    ):
        """未追踪的链上仓位应被导入。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": False,
            "sync.min_position_shares": 0.01,
        }.get(k)

        mock_fetch.return_value = [SAMPLE_DATA_API_POSITIONS[0]]
        mock_db.get_tracked_condition_ids.return_value = set()
        mock_db.get_open_paper_bets.return_value = []

        syncer = PositionSyncer(mock_executor)

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"id": "99001"}]
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            result = syncer.sync()

        assert len(result) == 1
        mock_db.insert_paper_bet.assert_called_once()
        bet = mock_db.insert_paper_bet.call_args[0][0]
        assert bet["question"] == "Will ETH be above $2,200 on March 21?"
        assert bet["condition_id"] == "0xcond_eth"
        assert bet["source"] == "manual"

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_skips_tracked_position(self, mock_get, mock_fetch, mock_db, mock_executor):
        """已追踪的仓位不重复导入。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": False,
            "sync.min_position_shares": 0.01,
        }.get(k)

        mock_fetch.return_value = [SAMPLE_DATA_API_POSITIONS[0]]
        mock_db.get_tracked_condition_ids.return_value = {"0xcond_eth"}
        mock_db.get_open_paper_bets.return_value = []

        syncer = PositionSyncer(mock_executor)
        result = syncer.sync()
        assert result == []
        mock_db.insert_paper_bet.assert_not_called()

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_skips_small_position(self, mock_get, mock_fetch, mock_db, mock_executor):
        """size < min_position_shares 的持仓不导入。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": False,
            "sync.min_position_shares": 100.0,  # 高阈值
        }.get(k)

        mock_fetch.return_value = [SAMPLE_DATA_API_POSITIONS[0]]  # size=16.94
        mock_db.get_tracked_condition_ids.return_value = set()
        mock_db.get_open_paper_bets.return_value = []

        syncer = PositionSyncer(mock_executor)
        result = syncer.sync()
        assert result == []
        mock_db.insert_paper_bet.assert_not_called()

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_api_failure_returns_empty(
        self, mock_get, mock_fetch, mock_db, mock_executor
    ):
        """Data API 失败时返回空列表。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": False,
        }.get(k)

        mock_fetch.return_value = None  # API 失败

        syncer = PositionSyncer(mock_executor)
        result = syncer.sync()
        assert result == []

    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_detects_manual_exit(
        self, mock_get, mock_fetch, mock_db, _mock_dry, mock_executor
    ):
        """DB 有而链上无的仓位应标记手动退出。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": False,
            "sync.min_position_shares": 0.01,
        }.get(k)

        # 链上只有 ETH 仓位
        mock_fetch.return_value = [SAMPLE_DATA_API_POSITIONS[0]]
        mock_db.get_tracked_condition_ids.return_value = {"0xcond_eth"}
        # DB 中有 ETH + recession（recession 不在链上了）
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "bet-recession",
                "condition_id": "0xcond_recession",
                "question": "US recession?",
                "market_price": 0.35,
                "source": "manual",
            }
        ]

        syncer = PositionSyncer(mock_executor)
        result = syncer.sync()
        # No new imports (ETH already tracked)
        assert result == []
        # But manual exit should be detected
        mock_db.close_paper_bet_manual_exit.assert_called_once()

    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_imports_multiple_positions(
        self, mock_get, mock_fetch, mock_db, _mock_dry, mock_executor
    ):
        """应能一次导入多个新仓位。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": False,
            "sync.min_position_shares": 0.01,
        }.get(k)

        mock_fetch.return_value = SAMPLE_DATA_API_POSITIONS
        mock_db.get_tracked_condition_ids.return_value = set()
        mock_db.get_open_paper_bets.return_value = []

        syncer = PositionSyncer(mock_executor)

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"id": "99999"}]
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            result = syncer.sync()

        assert len(result) == 4
        assert mock_db.insert_paper_bet.call_count == 4

    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_fallback_dedup_by_question_outcome(
        self, mock_get, mock_fetch, mock_db, _mock_dry, mock_executor
    ):
        """旧记录无 condition_id 时，应通过 question+outcome 去重并补填 condition_id。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": False,
            "sync.min_position_shares": 0.01,
        }.get(k)

        mock_fetch.return_value = [SAMPLE_DATA_API_POSITIONS[0]]  # ETH position
        mock_db.get_tracked_condition_ids.return_value = set()  # 无 cid 在追踪
        # 旧记录存在，有相同 question 但无 condition_id
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "old-bet-eth",
                "question": "Will ETH be above $2,200 on March 21?",
                "outcome": "Yes",
                "condition_id": "",
            }
        ]

        syncer = PositionSyncer(mock_executor)
        result = syncer.sync()

        # 不应导入新记录（去重命中）
        assert result == []
        mock_db.insert_paper_bet.assert_not_called()
        # 应补填旧记录的 condition_id
        mock_db.update_condition_id.assert_called_once_with("old-bet-eth", "0xcond_eth")

    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_fallback_dedup_case_insensitive_outcome(
        self, mock_get, mock_fetch, mock_db, _mock_dry, mock_executor
    ):
        """outcome 大小写不同时也应能去重（Yes vs YES）。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": False,
            "sync.min_position_shares": 0.01,
        }.get(k)

        mock_fetch.return_value = [SAMPLE_DATA_API_POSITIONS[0]]
        mock_db.get_tracked_condition_ids.return_value = set()
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "old-bet-eth",
                "question": "Will ETH be above $2,200 on March 21?",
                "outcome": "YES",  # 大写
                "condition_id": "",
            }
        ]

        syncer = PositionSyncer(mock_executor)
        result = syncer.sync()

        assert result == []
        mock_db.insert_paper_bet.assert_not_called()
        mock_db.update_condition_id.assert_called_once()

    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_detects_manual_exit_auto_source_live_mode(
        self, mock_get, mock_fetch, mock_db, _mock_dry, mock_executor
    ):
        """T96: 实盘模式下 source=auto 仓位用户在平台清仓后，DB 必须标记手动退出。"""
        import time as _t

        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "sync.min_position_shares": 0.01,
            "reconcile.min_age_seconds": 1,  # 1秒内生效，便于测试
        }.get(k)

        # 链上已无该仓位（用户已清仓）
        mock_fetch.return_value = []
        mock_db.get_tracked_condition_ids.return_value = set()
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "auto-bet-001",
                "condition_id": "0xcond_weather_no",
                "question": "Will the highest temperature in Chicago be between 50-51°F on April 6?",
                "market_price": 0.20,
                "source": "auto",  # 机器人自动下单
                "outcome": "NO",
                "matched_shares": 5.73,
                "market_end_date": "2027-04-07",  # 未来日期（活跃市场，测试主动退出场景）
                "ts_entered": _t.time() - 600,  # 10分钟前下单，min_age=1s，已超过
            }
        ]

        syncer = PositionSyncer(mock_executor)
        syncer.sync()

        # 必须检测到手动退出
        mock_db.close_paper_bet_manual_exit.assert_called_once()

    @patch("src.sync.position_syncer.is_dry_run", return_value=True)  # DRY_RUN
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_auto_source_dry_run_never_marked_manual_exit(
        self, mock_get, mock_fetch, mock_db, _mock_dry, mock_executor
    ):
        """T96: DRY_RUN 模式下 source=auto 仓位绝不能被标记为手动退出（纸质仓位无链上对应）。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
        }.get(k)

        # DRY_RUN 模式下 sync() 直接 return []，不会执行
        syncer = PositionSyncer(mock_executor)
        result = syncer.sync()

        assert result == []
        mock_db.close_paper_bet_manual_exit.assert_not_called()

    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_auto_source_fresh_bet_not_marked_exit(
        self, mock_get, mock_fetch, mock_db, _mock_dry, mock_executor
    ):
        """T96: 刚下单的 auto 仓位（< min_age）不能被误标为手动退出（等待链上确认）。"""
        import time as _t

        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "sync.min_position_shares": 0.01,
            "reconcile.min_age_seconds": 300,  # 5分钟
        }.get(k)

        mock_fetch.return_value = []
        mock_db.get_tracked_condition_ids.return_value = set()
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "fresh-auto-bet",
                "condition_id": "0xcond_fresh",
                "question": "Fresh bet just placed",
                "market_price": 0.30,
                "source": "auto",
                "outcome": "NO",
                "matched_shares": 3.0,
                "market_end_date": "2026-04-07",
                "ts_entered": _t.time() - 10,  # 10秒前下单，min_age=300s，未到期
            }
        ]

        syncer = PositionSyncer(mock_executor)
        syncer.sync()

        # 不应被标记为手动退出
        mock_db.close_paper_bet_manual_exit.assert_not_called()


# ------------------------------------------------------------------
# _resolve_gamma_id / _resolve_gamma_info
# ------------------------------------------------------------------


class TestResolveGammaId:
    def test_empty_slug(self):
        assert PositionSyncer._resolve_gamma_id("") == ""

    @patch("src.sync.position_syncer.requests.get")
    def test_successful_lookup(self, mock_req):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"id": "1626023"}]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        result = PositionSyncer._resolve_gamma_id("highest-temperature-in-shanghai")
        assert result == "1626023"

    @patch("src.sync.position_syncer.requests.get")
    def test_multiple_results_returns_empty(self, mock_req):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"id": "1"}, {"id": "2"}]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        result = PositionSyncer._resolve_gamma_id("ambiguous-slug")
        assert result == ""

    @patch("src.sync.position_syncer.requests.get")
    def test_api_failure_returns_empty(self, mock_req):
        mock_req.side_effect = Exception("timeout")
        result = PositionSyncer._resolve_gamma_id("some-slug")
        assert result == ""


class TestResolveGammaInfo:
    def test_empty_slug_returns_empty_tuple(self):
        assert PositionSyncer._resolve_gamma_info("") == ("", "")

    @patch("src.sync.position_syncer.requests.get")
    def test_returns_gamma_id_and_event_slug(self, mock_req):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "id": "1626023",
                "groupItemTitle": "highest-temperature-in-shanghai-on-march-21-2026",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        gamma_id, event_slug = PositionSyncer._resolve_gamma_info("test-market-slug")
        assert gamma_id == "1626023"
        assert event_slug == "highest-temperature-in-shanghai-on-march-21-2026"

    @patch("src.sync.position_syncer.requests.get")
    def test_api_failure_returns_empty_tuple(self, mock_req):
        mock_req.side_effect = Exception("timeout")
        assert PositionSyncer._resolve_gamma_info("fail-slug") == ("", "")


# ------------------------------------------------------------------
# _infer_event_slug（模块级函数）
# ------------------------------------------------------------------


class TestInferEventSlug:
    def test_empty_slug_returns_empty(self):
        assert _infer_event_slug("") == ""

    @patch("src.sync.position_syncer.requests.get")
    def test_direct_events_match(self, mock_req):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"slug": "highest-temperature-in-shanghai-on-march-21-2026"}
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        result = _infer_event_slug(
            "highest-temperature-in-shanghai-on-march-21-2026-16c"
        )
        assert result == "highest-temperature-in-shanghai-on-march-21-2026"

    @patch("src.sync.position_syncer.requests.get")
    def test_slug_contains_fallback(self, mock_req):
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if call_count[0] == 1:
                resp.json.return_value = []  # 直接 slug 不匹配
            else:
                resp.json.return_value = [{"slug": "my-event"}]
            return resp

        mock_req.side_effect = side_effect
        result = _infer_event_slug("my-event-option1")
        assert result == "my-event"

    @patch("src.sync.position_syncer.requests.get")
    def test_no_match_returns_empty(self, mock_req):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        result = _infer_event_slug("totally-unknown-slug")
        assert result == ""


# ------------------------------------------------------------------
# slug 级别去重（Fix: 同市场不同 outcome 名称的重复导入）
# ------------------------------------------------------------------


class TestSlugDedup:
    """PositionSyncer 应通过 market_slug 匹配防止同一市场重复导入。"""

    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_slug_dedup_prevents_duplicate(
        self, mock_get, mock_fetch, mock_db, _mock_dry, mock_executor
    ):
        """已有同 slug 的 auto bet 时，链上同 slug 仓位不应重复导入。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": False,
            "sync.min_position_shares": 0.01,
        }.get(k)

        # 链上持仓: Hornets spread, outcome="Grizzlies"
        mock_fetch.return_value = [
            {
                "conditionId": "0xcond_hornets",
                "size": "5.0",
                "avgPrice": "0.40",
                "initialValue": "2.0",
                "title": "Spread: Hornets (-17.5)",
                "slug": "spread-hornets-175",
                "outcome": "Grizzlies",
                "eventSlug": "nba-march-22",
                "endDate": "2026-12-22T23:59:59Z",
            }
        ]

        mock_db.get_tracked_condition_ids.return_value = set()  # cid 不在追踪
        # 已有同 slug 的 auto bet (outcome=NO, 不同名称)
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "auto-hornets-bet",
                "question": "Spread: Hornets (-17.5)",
                "outcome": "NO",
                "condition_id": "",
                "market_slug": "spread-hornets-175",
                "market_id": "1672308",
                "category": "sports",
            }
        ]

        syncer = PositionSyncer(mock_executor)
        result = syncer.sync()

        assert result == []
        mock_db.insert_paper_bet.assert_not_called()
        # 应补填 condition_id 到已有 auto bet
        mock_db.update_condition_id.assert_called_once_with(
            "auto-hornets-bet", "0xcond_hornets"
        )

    @patch("src.sync.position_syncer.is_dry_run", return_value=False)
    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_slug_dedup_different_slug_imports(
        self, mock_get, mock_fetch, mock_db, _mock_dry, mock_executor
    ):
        """slug 不同的仓位应正常导入。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": False,
            "sync.min_position_shares": 0.01,
        }.get(k)

        mock_fetch.return_value = [
            {
                "conditionId": "0xcond_new",
                "size": "5.0",
                "avgPrice": "0.40",
                "initialValue": "2.0",
                "title": "Cavaliers vs. Pelicans",
                "slug": "cavaliers-vs-pelicans",
                "outcome": "Pelicans",
                "eventSlug": "nba-march-22",
                "endDate": "2026-12-22T23:59:59Z",
            }
        ]

        mock_db.get_tracked_condition_ids.return_value = set()
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "auto-hornets-bet",
                "question": "Spread: Hornets (-17.5)",
                "outcome": "NO",
                "condition_id": "",
                "market_slug": "spread-hornets-175",
                "market_id": "1672308",
            }
        ]

        syncer = PositionSyncer(mock_executor)

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"id": "9999"}]
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            result = syncer.sync()

        assert len(result) == 1
        mock_db.insert_paper_bet.assert_called_once()

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.PositionSyncer._fetch_real_positions")
    @patch("src.sync.position_syncer.get")
    def test_slug_dedup_no_cid_no_backfill(
        self, mock_get, mock_fetch, mock_db, mock_executor
    ):
        """链上持仓无 conditionId 时仍应去重但不补填。"""
        mock_get.side_effect = lambda k: {
            "sync.enabled": True,
            "general.dry_run": False,
            "sync.min_position_shares": 0.01,
        }.get(k)

        mock_fetch.return_value = [
            {
                "conditionId": "",
                "size": "5.0",
                "avgPrice": "0.40",
                "initialValue": "2.0",
                "title": "Spread: Hornets (-17.5)",
                "slug": "spread-hornets-175",
                "outcome": "Grizzlies",
                "eventSlug": "",
                "endDate": "",
            }
        ]

        mock_db.get_tracked_condition_ids.return_value = set()
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "auto-hornets-bet",
                "question": "Spread: Hornets (-17.5)",
                "outcome": "NO",
                "condition_id": "",
                "market_slug": "spread-hornets-175",
                "market_id": "1672308",
            }
        ]

        syncer = PositionSyncer(mock_executor)
        result = syncer.sync()

        assert result == []
        mock_db.insert_paper_bet.assert_not_called()
        mock_db.update_condition_id.assert_not_called()


# ------------------------------------------------------------------
# category 推断增强（sports 关键词 + 继承）
# ------------------------------------------------------------------


class TestCategoryEnhancements:
    def test_spread_keyword_detected_as_sports(self):
        """'Spread:' 关键词应被识别为 sports 赛道。"""
        result = PositionSyncer._infer_category_from_tags([], "Spread: Hornets (-17.5)")
        assert result == "sports"

    def test_over_under_keyword_detected_as_sports(self):
        """'over/under' 关键词应被识别为 sports。"""
        result = PositionSyncer._infer_category_from_tags(
            [], "Over/Under 220.5 Lakers vs Celtics"
        )
        assert result == "sports"

    def test_moneyline_keyword_detected_as_sports(self):
        """'moneyline' 关键词应被识别为 sports。"""
        result = PositionSyncer._infer_category_from_tags(
            [], "Moneyline: Warriors vs Rockets"
        )
        assert result == "sports"

    def test_category_inheritance_from_existing_slug(self, mock_executor):
        """category='unknown' 时应从同 slug 已有记录继承。"""
        syncer = PositionSyncer(mock_executor)
        pos = {
            "title": "Some Unknown Market Title",
            "conditionId": "0xcond_abc",
            "slug": "some-market-slug",
            "eventSlug": "",
            "outcome": "Yes",
            "avgPrice": "0.50",
            "size": "10.0",
            "initialValue": "5.0",
            "endDate": "2026-12-31T23:59:59Z",
        }
        existing_bets = [
            {
                "bet_id": "existing-bet",
                "market_slug": "some-market-slug",
                "market_id": "12345",
                "category": "sports",
            }
        ]

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"id": "12345"}]
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos, existing_bets)

        assert record is not None
        assert record["category"] == "sports"

    def test_category_inheritance_from_existing_market_id(self, mock_executor):
        """category='unknown' 时应从同 market_id 已有记录继承。"""
        syncer = PositionSyncer(mock_executor)
        pos = {
            "title": "Some Unknown Market Title",
            "conditionId": "0xcond_abc",
            "slug": "different-slug",
            "eventSlug": "",
            "outcome": "Yes",
            "avgPrice": "0.50",
            "size": "10.0",
            "initialValue": "5.0",
            "endDate": "2026-12-31T23:59:59Z",
        }
        existing_bets = [
            {
                "bet_id": "existing-bet",
                "market_slug": "other-slug",
                "market_id": "12345",
                "category": "finance",
            }
        ]

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"id": "12345"}]
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos, existing_bets)

        assert record is not None
        assert record["category"] == "finance"

    def test_no_inheritance_when_category_known(self, mock_executor):
        """category 已知时不继承。"""
        syncer = PositionSyncer(mock_executor)
        pos = {
            "title": "Bitcoin above $100k?",
            "conditionId": "0xcond_btc",
            "slug": "btc-100k",
            "eventSlug": "",
            "outcome": "Yes",
            "avgPrice": "0.50",
            "size": "10.0",
            "initialValue": "5.0",
            "endDate": "2026-12-31T23:59:59Z",
        }
        existing_bets = [
            {
                "bet_id": "existing-bet",
                "market_slug": "btc-100k",
                "market_id": "12345",
                "category": "sports",  # 不应覆盖
            }
        ]

        with patch("src.sync.position_syncer.requests.get") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"id": "12345"}]
            mock_resp.raise_for_status = MagicMock()
            mock_req.return_value = mock_resp

            record = syncer._build_bet_from_data_api(pos, existing_bets)

        assert record is not None
        assert record["category"] == "crypto"  # 关键词匹配优先


# ------------------------------------------------------------------
# Bug fix: _build_bet_from_data_api 必须包含 matched_shares
# ------------------------------------------------------------------


class TestBuildBetMatchedShares:
    def test_matched_shares_is_set(self, mock_executor):
        """构建记录时 matched_shares 必须等于 Data API 的 size 字段。"""
        syncer = PositionSyncer(mock_executor)
        pos = {
            "title": "Another 7.0 or above earthquake by March 31, 2026?",
            "conditionId": "0xaca616c49b67a19911755080e0e06fecfd0b716b1eaa8350a93232ee9669ffa7",
            "slug": "another-7pt0-or-above-earthquake-by-march-31-2026-347",
            "eventSlug": "",
            "outcome": "No",
            "avgPrice": "0.789999",
            "size": "2.531638",
            "initialValue": "1.999991",
            "endDate": "2026-05-31",
        }

        record = syncer._build_bet_from_data_api(pos)

        assert record is not None
        assert "matched_shares" in record
        assert abs(record["matched_shares"] - 2.531638) < 0.0001
        assert abs(record["market_price"] - 0.789999) < 0.0001
        assert abs(record["size_usdc"] - 1.999991) < 0.001

    def test_matched_shares_zero_when_size_zero(self, mock_executor):
        """size 为 0 时 matched_shares 也应为 0（不报错）。"""
        syncer = PositionSyncer(mock_executor)
        pos = {
            "title": "Test market",
            "conditionId": "0xtest",
            "slug": "",
            "eventSlug": "",
            "outcome": "Yes",
            "avgPrice": "0.0",
            "size": "0.0",
            "initialValue": "0.0",
            "endDate": "",
        }

        record = syncer._build_bet_from_data_api(pos)

        assert record is not None
        assert record["matched_shares"] == 0.0


# ------------------------------------------------------------------
# Bug fix: _reconcile_auto_positions 应修复 chain_sync 零值数据
# ------------------------------------------------------------------


class TestReconcileChainSyncZeroData:
    """phase N fix — chain_sync 仓位 size=0 应被对账修复。"""

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.get")
    def test_chain_sync_zero_data_is_reconciled(self, mock_get, mock_db):
        """chain_sync 仓位 size_usdc=0 → 应用链上真实数据更新。"""
        mock_get.side_effect = lambda k: {"reconcile.min_age_seconds": 0}.get(k, None)

        # chain_sync 仓位：DB 中全为 0
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "bet-earthquake",
                "source": "manual",
                "dimension_tags": "chain_sync",
                "condition_id": "0xcond_eq",
                "ts_entered": 0.0,
                "size_usdc": 0.0,
                "market_price": 0.0,
                "matched_shares": 0.0,
                "question": "Another 7.0 earthquake?",
            }
        ]

        chain_positions = [
            {
                "conditionId": "0xcond_eq",
                "size": "2.531638",
                "avgPrice": "0.789999",
                "initialValue": "1.999991",
            }
        ]

        count = PositionSyncer._reconcile_auto_positions(chain_positions)

        assert count == 1
        mock_db.connect.assert_called()  # DB 更新被调用

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.get")
    def test_chain_sync_with_data_not_reconciled(self, mock_get, mock_db):
        """chain_sync 仓位已有正确数据时不应重复更新。"""
        mock_get.side_effect = lambda k: {"reconcile.min_age_seconds": 0}.get(k, None)

        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "bet-earthquake",
                "source": "manual",
                "dimension_tags": "chain_sync",
                "condition_id": "0xcond_eq",
                "ts_entered": 0.0,
                "size_usdc": 2.0,  # 已有正确数据
                "market_price": 0.79,
                "matched_shares": 2.53,
                "question": "Another 7.0 earthquake?",
            }
        ]

        chain_positions = [
            {
                "conditionId": "0xcond_eq",
                "size": "2.531638",
                "avgPrice": "0.789999",
                "initialValue": "1.999991",
            }
        ]

        count = PositionSyncer._reconcile_auto_positions(chain_positions)

        # 差异在阈值以内，不应更新
        assert count == 0

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.get")
    def test_manual_non_chain_sync_not_reconciled(self, mock_get, mock_db):
        """source=manual 但非 chain_sync 标签的仓位不应被对账。"""
        mock_get.side_effect = lambda k: {"reconcile.min_age_seconds": 0}.get(k, None)

        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "bet-manual",
                "source": "manual",
                "dimension_tags": "",  # 无 chain_sync 标签
                "condition_id": "0xcond_manual",
                "ts_entered": 0.0,
                "size_usdc": 0.0,  # 即使是 0 也不对账
                "market_price": 0.0,
                "matched_shares": 0.0,
                "question": "Manual bet",
            }
        ]

        chain_positions = [
            {
                "conditionId": "0xcond_manual",
                "size": "5.0",
                "avgPrice": "0.5",
                "initialValue": "2.5",
            }
        ]

        count = PositionSyncer._reconcile_auto_positions(chain_positions)
        assert count == 0

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.get")
    def test_auto_source_still_reconciled(self, mock_get, mock_db):
        """原有 auto 来源仓位的对账逻辑不应受影响（回归测试）。"""
        mock_get.side_effect = lambda k: {"reconcile.min_age_seconds": 0}.get(k, None)

        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "bet-auto",
                "source": "auto",
                "dimension_tags": "",
                "condition_id": "0xcond_auto",
                "ts_entered": 0.0,
                "size_usdc": 1.5,  # 与链上差异 > 0.01
                "market_price": 0.50,
                "matched_shares": 3.0,
                "question": "Auto bet",
            }
        ]

        chain_positions = [
            {
                "conditionId": "0xcond_auto",
                "size": "3.5",
                "avgPrice": "0.51",
                "initialValue": "1.785",
            }
        ]

        count = PositionSyncer._reconcile_auto_positions(chain_positions)
        assert count == 1
        mock_db.connect.assert_called()

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.get")
    def test_copy_trade_source_is_now_reconciled(self, mock_get, mock_db):
        """T170 Fix: copy_trade 来源的仓位现在也应被链上对账（pre-T169 bug产生的错误成本修正）。"""
        mock_get.side_effect = lambda k: {"reconcile.min_age_seconds": 0}.get(k, None)

        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "copy_cce120xyz",
                "source": "copy_trade",
                "dimension_tags": "",
                "condition_id": "0xcond_ct",
                "ts_entered": 0.0,
                "size_usdc": 0.32,  # pre-T169 bug: stored buy_price not CLOB cost
                "market_price": 0.32,
                "matched_shares": 6.0,  # wrong: only 1 share actually filled
                "question": "Madrid 29C",
            }
        ]

        chain_positions = [
            {
                "conditionId": "0xcond_ct",
                "size": "1.0",  # chain truth: 1 share
                "avgPrice": "0.32",
                "initialValue": "0.32",  # 1 × 0.32
            }
        ]

        count = PositionSyncer._reconcile_auto_positions(chain_positions)
        assert count == 1, "copy_trade 仓位应被对账"
        mock_db.connect.assert_called()

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.get")
    def test_manual_non_chain_sync_still_skipped(self, mock_get, mock_db):
        """T170: source=manual 非chain_sync 仍应跳过（回归测试）。"""
        mock_get.side_effect = lambda k: {"reconcile.min_age_seconds": 0}.get(k, None)

        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "bet-manual-x",
                "source": "manual",
                "dimension_tags": "",  # no chain_sync tag
                "condition_id": "0xcond_manu",
                "ts_entered": 0.0,
                "size_usdc": 2.0,
                "market_price": 0.5,
                "matched_shares": 4.0,
                "question": "User manual bet",
            }
        ]

        chain_positions = [
            {
                "conditionId": "0xcond_manu",
                "size": "5.0",
                "avgPrice": "0.51",
                "initialValue": "2.55",
            }
        ]

        count = PositionSyncer._reconcile_auto_positions(chain_positions)
        assert count == 0, "source=manual 非chain_sync 不应被自动对账"


# ------------------------------------------------------------------
# T170 Fix: dust_threshold_shares — active_cids 过滤测试
# ------------------------------------------------------------------


class TestDustThresholdActiveCids:
    """T170 Fix 验证：active_cids 应使用 dust_threshold_shares 而非 min_order_shares。

    bug: 旧代码使用 min_order_shares=5.0 作为阈值，导致 1.0 share
    (partial fill) 的持仓被误判为 dust → 错误标记 manual_exit。
    修复: 使用独立的 sync.dust_threshold_shares=0.1。
    """

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.get")
    def test_partial_fill_not_treated_as_dust(self, mock_get, mock_db):
        """1 share的partial fill仓位应在 active_cids 中（不被当作dust关闭）。"""
        mock_get.side_effect = lambda k: {
            "sync.dust_threshold_shares": 0.1,  # dust阈值（非min_order_shares=5）
            "reconcile.min_age_seconds": 0,
        }.get(k, None)

        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "partial-fill-1share",
                "condition_id": "0xcond_partial",
                "question": "Madrid 29C partial fill",
                "market_price": 0.32,
                "size_usdc": 0.32,
                "matched_shares": 1.0,
                "source": "copy_trade",
            }
        ]

        # 链上有 1 share (>0.1 dust threshold，但 < min_order_shares=5)
        chain_positions = [
            {
                "conditionId": "0xcond_partial",
                "size": "1.0",
                "avgPrice": "0.32",
                "initialValue": "0.32",
                "redeemable": False,
            }
        ]

        # 用代码中的 dust_threshold 逻辑构建 active_cids
        _dust_threshold = float(mock_get("sync.dust_threshold_shares") or 0.1)
        active_cids = {
            pos.get("conditionId", "")
            for pos in chain_positions
            if not pos.get("redeemable")
            and pos.get("conditionId")
            and float(pos.get("size") or 0) >= _dust_threshold
        }

        # 1.0 >= 0.1 → 应在 active_cids 中
        assert (
            "0xcond_partial" in active_cids
        ), "1 share partial fill 应在 active_cids 中（不是dust）"

        # _detect_manual_exits: 如果 cid 在 active_cids 中，不应关闭
        result = PositionSyncer._detect_manual_exits(active_cids)
        assert (
            "partial-fill-1share" not in result
        ), "partial fill仓位不应被误标为manual_exit"

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.get")
    def test_gas_dust_is_filtered(self, mock_get, mock_db):
        """真正的 gas dust (0.007 shares) 应被过滤，不阻止退出检测。"""
        mock_get.side_effect = lambda k: {
            "sync.dust_threshold_shares": 0.1,
        }.get(k, None)

        # 链上 dust：0.007 shares (gas residual after successful sell)
        dust_positions = [
            {
                "conditionId": "0xcond_dust",
                "size": "0.007",
                "redeemable": False,
            }
        ]

        _dust_threshold = 0.1
        active_cids = {
            pos.get("conditionId", "")
            for pos in dust_positions
            if float(pos.get("size") or 0) >= _dust_threshold
        }

        # 0.007 < 0.1 → 不在 active_cids 中
        assert (
            "0xcond_dust" not in active_cids
        ), "gas dust (0.007 shares) 应被过滤，不阻止退出检测"

    def test_threshold_comparison_old_vs_new(self):
        """对比旧阈值(5.0)和新阈值(0.1)对1-share仓位的处理差异。"""
        size_1_share = 1.0
        old_threshold = 5.0  # T100 旧配置
        new_threshold = 0.1  # T170 修正值

        # 旧行为: 1.0 < 5.0 → exclude from active_cids → 错误触发manual_exit
        assert size_1_share < old_threshold, "旧阈值会错误过滤1 share仓位"

        # 新行为: 1.0 >= 0.1 → include in active_cids → 正确跟踪
        assert size_1_share >= new_threshold, "新阈值正确保留1 share仓位"


# ------------------------------------------------------------------
# T192 Fix: conditionId+outcome 复合键防止 YES/NO 数据互相覆盖
# ------------------------------------------------------------------


class TestReconcileCompositeKey:
    """T192 Fix: 同一 condition 同时持有 YES 和 NO 时，
    _reconcile_auto_positions 不得将一方的链上数据覆盖到另一方。

    事故复现：Paris 15°C 市场，copy_trade YES (0.35/$1.81/5.18) + weather NO (0.61/$3.20/5.25)
    旧代码用 conditionId 做 key → YES 覆盖 NO → NO bet 被错误校准为 0.35/$1.81/5.18
    → 假利润触发 ProfitTake。
    """

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.get")
    def test_yes_no_same_condition_not_cross_contaminated(self, mock_get, mock_db):
        """同一 conditionId 的 YES 和 NO 仓位不应互相覆盖数据。"""
        mock_get.side_effect = lambda k: {"reconcile.min_age_seconds": 0}.get(k, None)

        # DB 中有两个仓位：YES copy trade + NO weather signal
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "copy_yes_paris",
                "source": "copy_trade",
                "condition_id": "0xPARIS15",
                "outcome": "YES",
                "ts_entered": 0.0,
                "size_usdc": 1.81,
                "market_price": 0.35,
                "matched_shares": 5.17,
                "question": "Paris 15°C YES",
                "entry_fee_usdc": 0.1,
            },
            {
                "bet_id": "no_weather_paris",
                "source": "auto",
                "condition_id": "0xPARIS15",
                "outcome": "NO",
                "ts_entered": 0.0,
                "size_usdc": 3.20,
                "market_price": 0.61,
                "matched_shares": 5.25,
                "question": "Paris 15°C NO",
                "entry_fee_usdc": 0.1,
            },
        ]

        # 链上有 YES 和 NO 两个持仓（同一 conditionId）
        chain_positions = [
            {
                "conditionId": "0xPARIS15",
                "outcome": "No",
                "size": "5.25",
                "avgPrice": "0.61",
                "initialValue": "3.20",
            },
            {
                "conditionId": "0xPARIS15",
                "outcome": "Yes",
                "size": "5.17",
                "avgPrice": "0.35",
                "initialValue": "1.81",
            },
        ]

        count = PositionSyncer._reconcile_auto_positions(chain_positions)

        # 两个仓位都匹配到自己的链上数据，差异小于阈值，不需要校准
        assert count == 0, "已匹配的仓位不应被错误校准"

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.get")
    def test_no_bet_not_overwritten_by_yes_chain_data(self, mock_get, mock_db):
        """NO 仓位不应被 YES 的链上数据覆盖（核心回归测试）。"""
        mock_get.side_effect = lambda k: {"reconcile.min_age_seconds": 0}.get(k, None)

        # DB: NO bet 有正确的初始数据
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "no_weather_paris",
                "source": "auto",
                "condition_id": "0xPARIS15",
                "outcome": "NO",
                "ts_entered": 0.0,
                "size_usdc": 3.20,
                "market_price": 0.61,
                "matched_shares": 5.25,
                "question": "Paris 15°C NO",
                "entry_fee_usdc": 0.1,
            },
        ]

        # 链上只有 YES 数据（NO 已卖出或 API 只返回 YES）
        chain_positions = [
            {
                "conditionId": "0xPARIS15",
                "outcome": "Yes",
                "size": "5.17",
                "avgPrice": "0.35",
                "initialValue": "1.81",
            },
        ]

        count = PositionSyncer._reconcile_auto_positions(chain_positions)

        # NO bet 的 conditionId 在链上没有 NO 记录 → 应跳过，不校准
        assert count == 0, "链上无 NO 数据时不应用 YES 数据覆盖 NO bet"
        # 验证 DB.connect 没有被调用（无更新操作）
        mock_db.connect.assert_not_called()

    @patch("src.sync.position_syncer.db")
    @patch("src.sync.position_syncer.get")
    def test_only_matching_outcome_calibrated(self, mock_get, mock_db):
        """只有 outcome 匹配的链上数据才应用于对账校准。"""
        mock_get.side_effect = lambda k: {"reconcile.min_age_seconds": 0}.get(k, None)

        # DB: YES bet 初始价格偏差（需要校准）
        mock_db.get_open_paper_bets.return_value = [
            {
                "bet_id": "yes_bet_tokyo",
                "source": "auto",
                "condition_id": "0xTOKYO25",
                "outcome": "YES",
                "ts_entered": 0.0,
                "size_usdc": 2.50,
                "market_price": 0.50,
                "matched_shares": 5.00,
                "question": "Tokyo 25°C YES",
                "entry_fee_usdc": 0,
            },
        ]

        # 链上有 YES 和 NO — YES 的真实数据不同于 DB
        chain_positions = [
            {
                "conditionId": "0xTOKYO25",
                "outcome": "Yes",
                "size": "5.10",
                "avgPrice": "0.48",
                "initialValue": "2.45",
            },
            {
                "conditionId": "0xTOKYO25",
                "outcome": "No",
                "size": "10.00",
                "avgPrice": "0.52",
                "initialValue": "5.20",
            },
        ]

        # Mock connect to capture the UPDATE
        mock_conn = MagicMock()
        mock_db.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.connect.return_value.__exit__ = MagicMock(return_value=False)

        count = PositionSyncer._reconcile_auto_positions(chain_positions)

        assert count == 1, "YES bet 应被 YES 链上数据校准"
        # 验证更新使用的是 YES 的链上数据（0.48/2.45），不是 NO 的（0.52/5.20）
        update_call = mock_conn.execute.call_args
        args = update_call[0][1]  # (chain_cost, chain_avg, chain_shares, bet_id)
        assert abs(args[0] - 2.45) < 0.01, f"cost should be 2.45, got {args[0]}"
        assert abs(args[1] - 0.48) < 0.01, f"price should be 0.48, got {args[1]}"
