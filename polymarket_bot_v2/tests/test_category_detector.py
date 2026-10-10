"""
test_category_detector.py — 统一赛道检测模块测试
覆盖：sports/weather/crypto/geopolitics/finance/tech/economy/pop_culture/default 分类准确性
"""

import pytest
from src.utils.category_detector import detect_category, is_sports_market


class TestSportsDetection:
    """体育赛道检测测试。"""

    def test_nba_by_keyword(self):
        m = {"question": "Will the Lakers win the NBA Finals?"}
        assert detect_category(m) == "sports"

    def test_epl_by_keyword(self):
        m = {"question": "Will Arsenal win the EPL title?"}
        assert detect_category(m) == "sports"

    def test_dota2_by_keyword(self):
        m = {"question": "Will Team Spirit win the Dota 2 Major?"}
        assert detect_category(m) == "esports"

    def test_cs2_by_keyword(self):
        m = {"question": "Will FaZe win the CS2 tournament?"}
        assert detect_category(m) == "esports"

    def test_ufc_by_keyword(self):
        m = {"question": "Will Jon Jones win the UFC heavyweight bout?"}
        assert detect_category(m) == "sports"

    def test_vs_pattern_detects_sports(self):
        m = {"question": "Lakers vs Celtics: Who will win?"}
        assert detect_category(m) == "sports"

    def test_vs_pattern_excludes_debate(self):
        m = {"question": "Trump vs Biden debate: who will win?"}
        assert detect_category(m) != "sports"

    def test_tag_slug_nba(self):
        m = {"question": "Some question", "tags": [{"slug": "nba"}]}
        assert detect_category(m) == "sports"

    def test_tag_slug_soccer(self):
        m = {"question": "Some question", "tags": [{"slug": "soccer"}]}
        assert detect_category(m) == "sports"

    def test_tag_slug_esports(self):
        m = {"question": "Some match", "tags": [{"slug": "dota-2"}]}
        assert detect_category(m) == "esports"

    def test_is_sports_market_convenience(self):
        m = {"question": "Will the Warriors beat the Suns?", "tags": [{"slug": "nba"}]}
        assert is_sports_market(m)


class TestWeatherPriority:
    """天气赛道优先级高于其他赛道。"""

    def test_weather_over_sports(self):
        """含天气关键词时，即使有 vs 模式也应分为天气。"""
        m = {"question": "Will it rain tomorrow?", "tags": [{"label": "Weather"}]}
        assert detect_category(m) == "weather"


class TestCryptoDetection:
    def test_bitcoin_keyword(self):
        m = {"question": "Will Bitcoin exceed $100k?"}
        assert detect_category(m) == "crypto"

    def test_eth_keyword(self):
        m = {"question": "Will ETH reach $5000?"}
        assert detect_category(m) == "crypto"


class TestGeopoliticsDetection:
    def test_election_keyword(self):
        m = {"question": "Will Trump win the 2028 election?"}
        assert detect_category(m) == "geopolitics"

    def test_war_keyword(self):
        m = {"question": "Will the Ukraine war end by 2026?"}
        assert detect_category(m) == "geopolitics"

    def test_politics_merged_governor(self):
        """politics 关键词应归入 geopolitics。"""
        m = {"question": "Will the governor sign the bill?"}
        assert detect_category(m) == "geopolitics"

    def test_politics_merged_impeach(self):
        m = {"question": "Will Congress impeach the president?"}
        assert detect_category(m) == "geopolitics"

    def test_politics_merged_supreme_court(self):
        m = {"question": "Will the supreme court overturn the ruling?"}
        assert detect_category(m) == "geopolitics"

    def test_politics_tag_slug(self):
        m = {"question": "Some political question", "tags": [{"slug": "us-politics"}]}
        assert detect_category(m) == "geopolitics"

    def test_politics_tag_slug_senate(self):
        m = {"question": "Some question about the Senate", "tags": [{"slug": "senate"}]}
        assert detect_category(m) == "geopolitics"


class TestFinanceDetection:
    def test_sp500_keyword(self):
        m = {"question": "Will the S&P 500 close above 6000?"}
        assert detect_category(m) == "finance"

    def test_fed_rate_keyword(self):
        m = {"question": "Will the Fed cut the interest rate in July?"}
        assert detect_category(m) == "finance"

    def test_ipo_keyword(self):
        m = {"question": "Will the company IPO before Q3?"}
        assert detect_category(m) == "finance"

    def test_nasdaq_keyword(self):
        m = {"question": "Will Nasdaq reach 20000?"}
        assert detect_category(m) == "finance"

    def test_finance_tag_slug(self):
        m = {"question": "Some market question", "tags": [{"slug": "stocks"}]}
        assert detect_category(m) == "finance"


class TestTechDetection:
    def test_ai_keyword(self):
        m = {"question": "Will OpenAI release GPT-5 by June?"}
        assert detect_category(m) == "tech"

    def test_apple_keyword(self):
        m = {"question": "Will Apple announce a new product at WWDC?"}
        assert detect_category(m) == "tech"

    def test_semiconductor_keyword(self):
        m = {"question": "Will semiconductor shortages end by 2026?"}
        assert detect_category(m) == "tech"

    def test_tech_tag_slug(self):
        m = {"question": "Some tech question", "tags": [{"slug": "ai"}]}
        assert detect_category(m) == "tech"


class TestEconomyDetection:
    def test_gdp_keyword(self):
        m = {"question": "Will US GDP growth exceed 3% in Q2?"}
        assert detect_category(m) == "economy"

    def test_inflation_keyword(self):
        m = {"question": "Will inflation drop below 2%?"}
        assert detect_category(m) == "economy"

    def test_tariff_keyword(self):
        m = {"question": "Will new tariffs be imposed on imports?"}
        assert detect_category(m) == "economy"

    def test_recession_keyword(self):
        m = {"question": "Will a recession begin in 2026?"}
        assert detect_category(m) == "economy"

    def test_economy_tag_slug(self):
        m = {"question": "Some macro question", "tags": [{"slug": "inflation"}]}
        assert detect_category(m) == "economy"


class TestPopCultureDetection:
    def test_oscar_keyword(self):
        m = {"question": "Will the movie win the Oscar?"}
        assert detect_category(m) == "pop_culture"

    def test_billboard_keyword(self):
        m = {"question": "Will the artist reach #1 on Billboard?"}
        assert detect_category(m) == "pop_culture"

    def test_netflix_keyword(self):
        m = {"question": "Will Netflix gain 10M subscribers this quarter?"}
        assert detect_category(m) == "pop_culture"

    def test_celebrity_keyword(self):
        m = {"question": "Will the celebrity couple get married?"}
        assert detect_category(m) == "pop_culture"

    def test_pop_culture_tag_slug(self):
        m = {
            "question": "Some entertainment question",
            "tags": [{"slug": "entertainment"}],
        }
        assert detect_category(m) == "pop_culture"


class TestDefaultCategory:
    def test_unknown_topic(self):
        m = {"question": "Will the bridge construction finish on time?"}
        assert detect_category(m) == "default"
