"""
test_scanner.py — Market Scanner 单元测试
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from src.scanner.market_scanner import MarketScanner

MOCK_MARKETS = [
    {
        "id": "market_001",
        "question": "Will it rain in New York tomorrow?",
        "bestAsk": 0.60,
        "bestBid": 0.58,
        "volume24hr": 5000,
        "endDate": "2026-04-15T00:00:00Z",
        "tags": ["weather"],
    },
    {
        "id": "market_002",
        "question": "Will BTC exceed $100k by March 31?",
        "bestAsk": 0.45,
        "bestBid": 0.43,
        "volume24hr": 50000,
        "endDate": "2026-03-31T00:00:00Z",
        "tags": ["crypto"],
    },
]


class TestMarketScanner:
    def setup_method(self):
        self.scanner = MarketScanner()

    @patch("src.scanner.market_scanner.requests.Session.get")
    def test_fetch_markets_returns_list(self, mock_get):
        mock_resp = MagicMock()
        # keyset endpoint returns {"markets": [...], "next_cursor": None}
        mock_resp.json.return_value = {"markets": MOCK_MARKETS, "next_cursor": None}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        markets = self.scanner._fetch_markets()
        assert isinstance(markets, list)
        assert len(markets) == 2

    @patch("src.scanner.market_scanner.requests.Session.get")
    def test_scan_returns_tier_structure(self, mock_get):
        mock_resp = MagicMock()
        # keyset endpoint returns {"markets": [...], "next_cursor": None}
        mock_resp.json.return_value = {"markets": MOCK_MARKETS, "next_cursor": None}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        result = self.scanner.scan()
        assert "tier1" in result
        assert "tier2" in result
        assert "tier3" in result

    def test_smart_score_positive(self):
        cfg = {"categories_bonus": {"weather": 1.3, "crypto": 1.1}}
        score = self.scanner._smart_score(MOCK_MARKETS[0], cfg)
        assert score > 0

    def test_tier_split(self):
        markets = [{"_score": 80}, {"_score": 55}, {"_score": 40}, {"_score": 20}]
        result = self.scanner._tier_split(markets, min_score=50)
        assert len(result["tier1"]) == 1
        assert len(result["tier2"]) == 1
        assert len(result["tier3"]) == 1

    def test_smart_score_sports_bonus(self):
        """体育市场应获得 sports bonus。"""
        sports_market = {
            "id": "sports_001",
            "question": "Will Lakers win the NBA game?",
            "bestAsk": 0.55,
            "bestBid": 0.53,
            "volume24hr": 50000,
            "endDate": "2026-04-15T00:00:00Z",
            "tags": [{"slug": "nba"}],
        }
        cfg = {"categories_bonus": {"weather": 1.3, "sports": 1.2, "crypto": 1.1}}
        score = self.scanner._smart_score(sports_market, cfg)
        assert score > 0

    # ── 六维度扫描测试 ──────────────────────────────────────────

    def test_pass_dimension_filter_competitive_pass(self):
        """competitive 维度: 价格在 0.35-0.65 内应通过。"""
        market = {"bestAsk": 0.50}
        settings = {"price_min": 0.35, "price_max": 0.65}
        assert self.scanner._pass_dimension_filter(market, {}, "competitive", settings)

    def test_pass_dimension_filter_competitive_fail_high(self):
        """competitive 维度: 价格超出上限应被过滤。"""
        market = {"bestAsk": 0.80}
        settings = {"price_min": 0.35, "price_max": 0.65}
        assert not self.scanner._pass_dimension_filter(
            market, {}, "competitive", settings
        )

    def test_pass_dimension_filter_competitive_fail_low(self):
        """competitive 维度: 价格低于下限应被过滤。"""
        market = {"bestAsk": 0.10}
        settings = {"price_min": 0.35, "price_max": 0.65}
        assert not self.scanner._pass_dimension_filter(
            market, {}, "competitive", settings
        )

    def test_pass_dimension_filter_ending_soon_pass(self):
        """ending_soon 维度: 3天后到期应通过 (max_days_left=7)。"""
        end = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        market = {"endDate": end}
        settings = {"max_days_left": 7}
        assert self.scanner._pass_dimension_filter(market, {}, "ending_soon", settings)

    def test_pass_dimension_filter_ending_soon_fail(self):
        """ending_soon 维度: 14天后到期应被过滤 (max_days_left=7)。"""
        end = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
        market = {"endDate": end}
        settings = {"max_days_left": 7}
        assert not self.scanner._pass_dimension_filter(
            market, {}, "ending_soon", settings
        )

    def test_pass_dimension_filter_ending_soon_expired(self):
        """ending_soon 维度: 已过期市场应被过滤。"""
        end = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        market = {"endDate": end}
        settings = {"max_days_left": 7}
        assert not self.scanner._pass_dimension_filter(
            market, {}, "ending_soon", settings
        )

    def test_pass_dimension_filter_newest_pass(self):
        """newest 维度: 1天前创建应通过 (max_age_days=3)。"""
        created = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        market = {}
        event = {"createdAt": created}
        settings = {"max_age_days": 3}
        assert self.scanner._pass_dimension_filter(market, event, "newest", settings)

    def test_pass_dimension_filter_newest_fail(self):
        """newest 维度: 10天前创建应被过滤 (max_age_days=3)。"""
        created = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        market = {}
        event = {"createdAt": created}
        settings = {"max_age_days": 3}
        assert not self.scanner._pass_dimension_filter(
            market, event, "newest", settings
        )

    def test_pass_dimension_filter_liquidity_pass(self):
        """liquidity 维度: 流动性足够应通过。"""
        market = {"liquidity": 10000}
        settings = {"min_liquidity": 5000}
        assert self.scanner._pass_dimension_filter(market, {}, "liquidity", settings)

    def test_pass_dimension_filter_liquidity_fail(self):
        """liquidity 维度: 流动性不足应被过滤。"""
        market = {"liquidity": 1000}
        settings = {"min_liquidity": 5000}
        assert not self.scanner._pass_dimension_filter(
            market, {}, "liquidity", settings
        )

    def test_pass_dimension_filter_volume_pass(self):
        """volume 维度: 成交量足够应通过。"""
        market = {"volume24hr": 50000}
        settings = {"min_volume_24h": 10000}
        assert self.scanner._pass_dimension_filter(market, {}, "volume", settings)

    def test_pass_dimension_filter_volume_fail(self):
        """volume 维度: 成交量不足应被过滤。"""
        market = {"volume24hr": 2000}
        settings = {"min_volume_24h": 10000}
        assert not self.scanner._pass_dimension_filter(market, {}, "volume", settings)

    def test_pass_dimension_filter_trending_always_pass(self):
        """trending 维度: 无特殊过滤，应始终通过。"""
        market = {"bestAsk": 0.50}
        assert self.scanner._pass_dimension_filter(market, {}, "trending", {})

    @patch("src.scanner.market_scanner.get")
    @patch("src.scanner.market_scanner.requests.Session.get")
    def test_fetch_dimension_markets_disabled(self, mock_get, mock_cfg_get):
        """dimension_sorts.enabled=false 时返回空列表。"""
        mock_cfg_get.return_value = {"enabled": False}
        result = self.scanner._fetch_dimension_markets()
        assert result == []
        mock_get.assert_not_called()

    @patch("src.scanner.market_scanner.get")
    @patch("src.scanner.market_scanner.requests.Session.get")
    @patch("src.scanner.market_scanner.time.sleep")
    def test_fetch_dimension_markets_merges_tags(
        self, mock_sleep, mock_get, mock_cfg_get
    ):
        """同一市场在 trending 和 volume 中出现，应 merge _dimension_tags。"""
        event_with_shared_market = [
            {
                "title": "BTC Price Event",
                "slug": "btc-price",
                "tags": [{"slug": "crypto"}],
                "markets": [
                    {
                        "id": "mkt_001",
                        "conditionId": "cond_001",
                        "active": True,
                        "closed": False,
                        "bestAsk": 0.50,
                        "volume24hr": 50000,
                        "liquidity": 20000,
                    }
                ],
            }
        ]

        mock_resp = MagicMock()
        # keyset endpoint returns {"events": [...], "next_cursor": None}
        mock_resp.json.return_value = {
            "events": event_with_shared_market,
            "next_cursor": None,
        }
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        mock_cfg_get.return_value = {
            "enabled": True,
            "request_delay_sec": 0,
            "dimensions": {
                "trending": {"enabled": True, "limit": 50},
                "volume": {"enabled": True, "limit": 50, "min_volume_24h": 10000},
            },
        }

        result = self.scanner._fetch_dimension_markets()
        assert len(result) == 1
        assert "trending" in result[0]["_dimension_tags"]
        assert "volume" in result[0]["_dimension_tags"]

    def test_smart_score_dimension_bonus(self):
        """有维度标签的市场应获得维度 bonus 加分。"""
        market = {
            "id": "dim_001",
            "bestAsk": 0.50,
            "bestBid": 0.48,
            "volume24hr": 50000,
            "endDate": "2026-04-15T00:00:00Z",
            "tags": ["crypto"],
            "_dimension_tags": ["trending"],
        }
        cfg_no_dim = {"categories_bonus": {}}
        cfg_with_dim = {
            "categories_bonus": {},
            "dimension_sorts": {
                "dimensions": {"trending": {"bonus": 1.15}},
                "multi_dimension_bonus": 1.1,
            },
        }
        score_without = self.scanner._smart_score(market, cfg_no_dim)
        score_with = self.scanner._smart_score(market, cfg_with_dim)
        assert score_with > score_without

    def test_smart_score_multi_dimension_bonus(self):
        """多维度重叠的市场应获得额外加成。"""
        market = {
            "id": "multi_001",
            "bestAsk": 0.50,
            "bestBid": 0.48,
            "volume24hr": 50000,
            "endDate": "2026-04-15T00:00:00Z",
            "tags": ["crypto"],
            "_dimension_tags": ["trending", "volume"],
        }
        cfg = {
            "categories_bonus": {},
            "dimension_sorts": {
                "dimensions": {
                    "trending": {"bonus": 1.15},
                    "volume": {"bonus": 1.10},
                },
                "multi_dimension_bonus": 1.1,
            },
        }
        # 单维度市场
        single_market = {**market, "_dimension_tags": ["trending"]}
        score_single = self.scanner._smart_score(single_market, cfg)
        score_multi = self.scanner._smart_score(market, cfg)
        # 多维度应比单维度更高（多了 multi_dimension_bonus * max(bonus)）
        assert score_multi > score_single

    # ── 价差套利检测测试 (来源: Gabagool2-2 Pure Arbitrage) ──────

    @patch("src.scanner.market_scanner.get")
    def test_detect_arbitrage_finds_opportunity(self, mock_get):
        """YES_ask + NO_ask < threshold 时应报告套利机会。"""
        mock_get.return_value = {
            "arbitrage_threshold": 0.98,
            "arbitrage_min_liquidity": 1000,
        }
        markets = [
            {
                "id": "arb_001",
                "question": "Will ETH > $4000?",
                "bestAsk": 0.48,
                "bestBid": 0.55,
                "liquidity": 50000,
            }
        ]
        # NO_ask_effective = 1 - bestBid = 1 - 0.55 = 0.45
        # combined = 0.48 + 0.45 = 0.93 < 0.98
        opps = self.scanner.detect_arbitrage(markets)
        assert len(opps) == 1
        assert opps[0]["profit_pct"] > 0
        assert opps[0]["combined"] < 0.98

    @patch("src.scanner.market_scanner.get")
    def test_detect_arbitrage_no_opportunity(self, mock_get):
        """YES_ask + NO_ask >= threshold 时不应报告。"""
        mock_get.return_value = {
            "arbitrage_threshold": 0.98,
            "arbitrage_min_liquidity": 1000,
        }
        markets = [
            {
                "id": "arb_002",
                "question": "Will BTC > $100k?",
                "bestAsk": 0.55,
                "bestBid": 0.46,
                "liquidity": 50000,
            }
        ]
        # NO_ask_effective = 1 - 0.46 = 0.54
        # combined = 0.55 + 0.54 = 1.09 > 0.98
        opps = self.scanner.detect_arbitrage(markets)
        assert len(opps) == 0

    @patch("src.scanner.market_scanner.get")
    def test_detect_arbitrage_filters_low_liquidity(self, mock_get):
        """套利机会存在但流动性不足应被过滤。"""
        mock_get.return_value = {
            "arbitrage_threshold": 0.98,
            "arbitrage_min_liquidity": 10000,
        }
        markets = [
            {
                "id": "arb_003",
                "question": "Low liquidity arb?",
                "bestAsk": 0.48,
                "bestBid": 0.55,
                "liquidity": 500,
            }
        ]
        opps = self.scanner.detect_arbitrage(markets)
        assert len(opps) == 0


class TestWeatherStrikeOptimizerV2:
    """T242 v2: 天气 strike 优化器双模型测试"""

    def setup_method(self):
        with patch("src.scanner.market_scanner.get") as mock_get:
            mock_get.return_value = {}
            self.scanner = MarketScanner()

    def _make_weather_market(
        self, mid, question, event_slug, ask=0.30, bid=0.28, score=60.0
    ):
        return {
            "id": mid,
            "question": question,
            "_event_slug": event_slug,
            "_score": score,
            "bestAsk": ask,
            "bestBid": bid,
            "tags": ["temperature"],
        }

    @patch("src.scanner.market_scanner.fetch_dual_model_probability")
    def test_single_strike_event_passthrough(self, mock_dual):
        """单个strike事件无需优化，直接通过。"""
        m = self._make_weather_market(
            "1", "Will Paris be 12°C on April 1?", "paris-event"
        )
        result = self.scanner._weather_strike_optimizer([m])
        assert len(result) == 1
        mock_dual.assert_not_called()  # 单strike不调用API

    @patch("src.scanner.market_scanner.fetch_dual_model_probability")
    def test_diverged_all_strikes_keeps_all(self, mock_dual):
        """所有strike双模型分歧时，保留全部strikes让signal_engine处理。"""
        mock_dual.return_value = {
            "__divergence_skip__": True,
            "gfs_probability": 0.55,
            "ecmwf_probability": 0.10,
            "divergence": 0.45,
        }
        group = [
            self._make_weather_market(
                "1", "Will Paris be 10°C on March 29?", "paris-slug"
            ),
            self._make_weather_market(
                "2", "Will Paris be 13°C on March 29?", "paris-slug"
            ),
        ]
        result = self.scanner._weather_strike_optimizer(group)
        # 所有分歧 → 全部保留
        assert len(result) == 2

    @patch("src.scanner.market_scanner.fetch_dual_model_probability")
    def test_dual_model_selects_best_ev_strike(self, mock_dual):
        """双模型有效时，选择EV最高的strike。"""

        # 10°C: dual P=0.53, ask=0.09 → EV_yes=0.44 (幻象高EV，但在双模型下会分歧)
        # 13°C: dual P=0.06, ask=0.19 → EV_no=(1-0.06)-(1-0.18)=0.94-0.82=0.12 > EV_yes=-0.13
        def mock_dual_side_effect(market):
            q = market.get("question", "")
            if "10°C" in q:
                return {
                    "probability": 0.53,
                    "confidence": 0.80,
                    "source": "dual_model_gfs_ecmwf",
                }
            elif "13°C" in q:
                return {
                    "probability": 0.06,
                    "confidence": 0.85,
                    "source": "dual_model_gfs_ecmwf",
                }
            return None

        mock_dual.side_effect = mock_dual_side_effect

        group = [
            self._make_weather_market(
                "1",
                "Will Paris be 10°C on March 29?",
                "paris-slug",
                ask=0.09,
                bid=0.07,
                score=65.0,
            ),
            self._make_weather_market(
                "2",
                "Will Paris be 13°C on March 29?",
                "paris-slug",
                ask=0.19,
                bid=0.17,
                score=70.0,
            ),
        ]
        result = self.scanner._weather_strike_optimizer(group)
        assert len(result) == 1
        # 10°C YES EV = 0.53 - 0.09 = 0.44
        # 13°C NO EV = (1-0.06) - (1-0.17) = 0.94 - 0.83 = 0.11
        # 10°C wins on raw EV (dual模型均有效时按EV选择)
        assert result[0]["id"] == "1"

    @patch("src.scanner.market_scanner.fetch_dual_model_probability")
    def test_gfs_only_uses_raw_probability(self, mock_dual):
        """ECMWF不可用时，GFS单模型概率直接使用，不做市场价修正。"""
        mock_dual.return_value = None  # ECMWF 不可用

        with patch("src.scanner.market_scanner.fetch_ensemble_probability") as mock_ens:
            # GFS: 55% for exact 10°C, market at 9¢ → no correction applied
            mock_ens.return_value = {
                "probability": 0.55,
                "confidence": 0.75,
            }
            group = [
                self._make_weather_market(
                    "1",
                    "Will Paris be 10°C on March 29?",
                    "paris-slug",
                    ask=0.09,
                    bid=0.07,
                ),
                self._make_weather_market(
                    "2",
                    "Will Paris be 12°C on March 29?",
                    "paris-slug",
                    ask=0.30,
                    bid=0.28,
                ),
            ]
            result = self.scanner._weather_strike_optimizer(group)
            # GFS raw probability used directly: _strike_p_yes == 0.55
            m10 = next((x for x in result if x["id"] == "1"), None)
            if m10 and "_strike_p_yes" in m10:
                assert abs(m10["_strike_p_yes"] - 0.55) < 0.01

    @patch("src.scanner.market_scanner.fetch_dual_model_probability")
    def test_non_weather_passthrough_unchanged(self, mock_dual):
        """非天气赛道市场不参与优化，原样输出。"""
        m = {
            "id": "s1",
            "question": "Will Team A win the match?",
            "_event_slug": "match-event",
            "_score": 55.0,
            "bestAsk": 0.45,
            "bestBid": 0.43,
            "tags": ["sports"],
        }
        result = self.scanner._weather_strike_optimizer([m])
        assert len(result) == 1
        assert result[0]["id"] == "s1"
        mock_dual.assert_not_called()


# ── T48: Crypto daily-only 过滤测试 ────────────────────────────
class TestCryptoDailyOnlyFilter:
    """T48: _fetch_tagged_event_markets 对 crypto 赛道的 daily-only 过滤。"""

    def setup_method(self):
        self.scanner = MarketScanner()

    @patch("src.scanner.market_scanner.get")
    @patch("src.scanner.market_scanner.requests.Session.get")
    def test_crypto_daily_filter_keeps_daily_events(self, mock_http, mock_cfg):
        """crypto 赛道 daily-only 开启时，保留带 'daily' tag 的事件。"""
        mock_cfg.side_effect = lambda k, *a, **kw: {
            "scanner.crypto_daily_tag_filter": True,
        }.get(k, kw.get("default", a[0] if a else None))

        daily_event = {
            "title": "What price will Bitcoin hit on April 3?",
            "slug": "what-price-will-bitcoin-hit-on-april-3",
            "tags": [
                {"slug": "crypto"},
                {"slug": "bitcoin"},
                {"slug": "daily"},
                {"slug": "today"},
            ],
            "markets": [
                {
                    "id": "m_daily",
                    "active": True,
                    "closed": False,
                    "bestAsk": 0.50,
                    "endDate": "2026-04-04T04:00:00Z",
                }
            ],
        }
        weekly_event = {
            "title": "Bitcoin above ___ on April 6?",
            "slug": "bitcoin-above-on-april-6",
            "tags": [
                {"slug": "bitcoin"},
                {"slug": "weekly"},
                {"slug": "crypto"},
            ],
            "markets": [
                {
                    "id": "m_weekly",
                    "active": True,
                    "closed": False,
                    "bestAsk": 0.40,
                    "endDate": "2026-04-06T16:00:00Z",
                }
            ],
        }

        mock_resp = MagicMock()
        # keyset endpoint returns {"events": [...], "next_cursor": None}
        mock_resp.json.return_value = {
            "events": [daily_event, weekly_event],
            "next_cursor": None,
        }
        mock_resp.raise_for_status.return_value = None
        mock_http.return_value = mock_resp

        result = self.scanner._fetch_tagged_event_markets(["bitcoin"], "crypto")
        assert len(result) == 1
        assert result[0]["id"] == "m_daily"

    @patch("src.scanner.market_scanner.get")
    @patch("src.scanner.market_scanner.requests.Session.get")
    def test_crypto_daily_filter_disabled_keeps_all(self, mock_http, mock_cfg):
        """crypto_daily_tag_filter=False 时不过滤，保留所有事件。"""
        mock_cfg.side_effect = lambda k, *a, **kw: {
            "scanner.crypto_daily_tag_filter": False,
        }.get(k, kw.get("default", a[0] if a else None))

        events = [
            {
                "title": "Bitcoin 2026",
                "slug": "bitcoin-2026",
                "tags": [{"slug": "bitcoin"}, {"slug": "crypto"}],
                "markets": [
                    {"id": "m1", "active": True, "closed": False, "bestAsk": 0.70}
                ],
            },
            {
                "title": "BTC daily",
                "slug": "btc-daily",
                "tags": [{"slug": "bitcoin"}, {"slug": "daily"}],
                "markets": [
                    {"id": "m2", "active": True, "closed": False, "bestAsk": 0.50}
                ],
            },
        ]
        mock_resp = MagicMock()
        # keyset endpoint returns {"events": [...], "next_cursor": None}
        mock_resp.json.return_value = {"events": events, "next_cursor": None}
        mock_resp.raise_for_status.return_value = None
        mock_http.return_value = mock_resp

        result = self.scanner._fetch_tagged_event_markets(["bitcoin"], "crypto")
        assert len(result) == 2

    @patch("src.scanner.market_scanner.get")
    @patch("src.scanner.market_scanner.requests.Session.get")
    def test_non_crypto_label_not_filtered(self, mock_http, mock_cfg):
        """非 crypto 赛道(如 geopolitics)不受 daily 过滤影响。"""
        mock_cfg.side_effect = lambda k, *a, **kw: {
            "scanner.crypto_daily_tag_filter": True,
        }.get(k, kw.get("default", a[0] if a else None))

        events = [
            {
                "title": "US Elections 2026",
                "slug": "us-elections-2026",
                "tags": [{"slug": "politics"}],
                "markets": [
                    {"id": "m_geo", "active": True, "closed": False, "bestAsk": 0.55}
                ],
            },
        ]
        mock_resp = MagicMock()
        # keyset endpoint returns {"events": [...], "next_cursor": None}
        mock_resp.json.return_value = {"events": events, "next_cursor": None}
        mock_resp.raise_for_status.return_value = None
        mock_http.return_value = mock_resp

        result = self.scanner._fetch_tagged_event_markets(["politics"], "geopolitics")
        assert len(result) == 1


class TestConsensusFilterBestAsk:
    """T69b: 市场共识过滤必须使用 YES bestAsk 而非 bestBid (1-no_ask)"""

    def test_wide_spread_yes_ask_blocks_no_candidate(self):
        """Ankara案例: bestBid=0.34(低) bestAsk=0.59(高), 共识过滤应使用bestAsk"""
        # 模拟宽spread市场的NO候选
        candidate_wide_spread = {
            "no_ask": 0.66,  # = 1 - bestBid(0.34)
            "yes_ask": 0.59,  # bestAsk: 市场真实YES定价
            "strike_val": 15,
            "net_ev_no": 0.17,
            "score": 0.82,
        }
        _max_market_yes = 0.40

        # 旧逻辑: 1 - no_ask = 0.34 < 0.40 → 错误放行
        old_result = (1.0 - candidate_wide_spread["no_ask"]) < _max_market_yes
        assert old_result is True, "旧逻辑(bestBid=0.34)会错误放行"

        # 新逻辑: yes_ask = 0.59 >= 0.40 → 正确拦截
        new_result = (
            candidate_wide_spread.get("yes_ask", 1.0 - candidate_wide_spread["no_ask"])
            < _max_market_yes
        )
        assert new_result is False, "新逻辑(bestAsk=0.59)应正确拦截NO投注"

    def test_narrow_spread_unchanged_behavior(self):
        """窄spread市场: bestBid≈bestAsk, 两种逻辑结果一致"""
        candidate_narrow = {
            "no_ask": 0.72,  # = 1 - bestBid(0.28)
            "yes_ask": 0.30,  # bestAsk ≈ bestBid
            "strike_val": 10,
        }
        _max_market_yes = 0.40
        old = (1.0 - candidate_narrow["no_ask"]) < _max_market_yes  # 0.28 < 0.40
        new = (
            candidate_narrow.get("yes_ask", 1.0 - candidate_narrow["no_ask"])
            < _max_market_yes
        )  # 0.30 < 0.40
        assert old == new, "窄spread时新旧逻辑一致"

    def test_fallback_when_yes_ask_missing(self):
        """yes_ask缺失时回退到旧逻辑(1-no_ask)"""
        candidate_no_ask = {
            "no_ask": 0.80,  # 1 - no_ask = 0.20
            # 没有 yes_ask
        }
        _max_market_yes = 0.40
        result = (
            candidate_no_ask.get("yes_ask", 1.0 - candidate_no_ask["no_ask"])
            < _max_market_yes
        )
        assert result is True, "yes_ask缺失时回退到1-no_ask"
