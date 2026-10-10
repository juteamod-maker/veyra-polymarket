"""
category_detector.py — 统一赛道检测模块

职责：
  - 将 Polymarket 市场分类为 8 个赛道:
    weather / sports / crypto / geopolitics / finance / tech / pop_culture / economy
  - 被 paper_trader, risk_monitor, signal_engine 共用，确保分类一致性
  - 优先级: weather > sports > crypto > geopolitics > finance > tech > economy > pop_culture
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── 体育赛道关键词 ──────────────────────────────────────────────────────────
# 涵盖 Polymarket 上的所有体育+电竞类市场
_SPORTS_LEAGUE_KW = (
    # 北美职业联赛
    "nba",
    "nhl",
    "nfl",
    "mlb",
    "mls",
    "ncaab",
    "ncaaf",
    "cbb",
    # 足球联赛
    "epl",
    "premier league",
    "la liga",
    "bundesliga",
    "serie a",
    "ligue 1",
    "ucl",
    "champions league",
    "europa league",
    "copa america",
    "world cup",
    "euro 2026",
    "euro 2024",
    "afc",
    "nfc",
    # 其他体育
    "nascar",
    "formula 1",
    " f1 ",
    "wimbledon",
    "us open tennis",
    "french open",
    "australian open",
    "pga",
    "masters golf",
    "the masters",
    "ufc",
    "bellator",
    "boxing",
    "cricket",
    "ipl",
    # 电竞（Polymarket 归为 sports 分类）
    "dota 2",
    "dota2",
    "counter-strike",
    "cs2",
    "csgo",
    "cs:go",
    "valorant",
    "league of legends",
    "lol worlds",
    "starcraft",
    "overwatch",
    "esl pro league",
    "pgl ",
    "vct ",
)

_SPORTS_GENERIC_KW = (
    "playoff",
    "playoffs",
    "finals",
    "semifinal",
    "quarterfinal",
    "championship",
    "tournament",
    "best of 3",
    "best of 5",
    "bo3",
    "bo5",
    "game 1",
    "game 2",
    "game 3",
    "game 4",
    "game 5",
    "series",
    "round of",
)

_SPORTS_TAG_SLUGS = frozenset(
    {
        "nba",
        "nhl",
        "nfl",
        "mlb",
        "mls",
        "epl",
        "soccer",
        "ucl",
        "bundesliga",
        "ligue-1",
        "la-liga",
        "serie-a",
        "tennis",
        "golf",
        "ufc",
        "cricket",
        "cbb",
        "dota-2",
        "counter-strike",
        "valorant",
        "league-of-legends",
        "esports",
        "sports",
    }
)

# ── 加密赛道关键词 ──────────────────────────────────────────────────────────
_CRYPTO_KW = (
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "crypto",
    "solana",
    "sol",
    "xrp",
    "ripple",
    "cardano",
    "ada",
    "doge",
    "dogecoin",
    "bnb",
    "binance",
    "defi",
    "nft",
    "altcoin",
    "stablecoin",
    "usdc",
    "usdt",
    "coinbase",
    "kraken",
    "bitfinex",
    "blockchain",
)

# ── 地缘政治赛道关键词（含 politics 合并）───────────────────────────────────
_GEO_KW = (
    "election",
    "president",
    "trump",
    "biden",
    "harris",
    "vote",
    "war",
    "ukraine",
    "russia",
    "china",
    "taiwan",
    "nuclear",
    "ceasefire",
    "nato",
    "sanction",
    "treaty",
    "parliament",
    "geopolit",
    "minister",
    "congress",
    "senate",
    "military",
    "invasion",
    "conflict",
    "peace",
    "coup",
    "referendum",
    # politics 合并到 geopolitics —— polymarket.com/politics
    "governor",
    "mayor",
    "speaker",
    "impeach",
    "legislation",
    "executive order",
    "supreme court",
    "democrat",
    "republican",
    "gop",
    "dnc",
    "rnc",
    "primary",
    "midterm",
    "ballot",
    "cabinet",
    "veto",
    "filibuster",
)

_GEO_TAG_SLUGS = frozenset(
    {
        "politics",
        "us-politics",
        "elections",
        "geopolitics",
        "world",
        "us-elections",
        "regulation",
        "congress",
        "senate",
        "house",
        "government",
    }
)

# ── 金融赛道关键词 ──────────────────────────────────────────────────────────
_FINANCE_KW = (
    "stock",
    "s&p 500",
    "s&p500",
    "sp500",
    "nasdaq",
    "dow jones",
    "djia",
    "russell",
    "ipo",
    "earnings",
    "dividend",
    "market cap",
    "share price",
    "stock price",
    "ticker",
    "nyse",
    "wall street",
    "hedge fund",
    "short sell",
    "bond",
    "treasury",
    "yield curve",
    "fed rate",
    "interest rate",
    "rate cut",
    "rate hike",
    "fomc",
    "federal reserve",
    "mortgage rate",
    "forex",
    "gold price",
    "oil price",
    "commodity",
    "futures",
    "options",
)

_FINANCE_TAG_SLUGS = frozenset(
    {
        "finance",
        "stocks",
        "markets",
        "fed",
        "interest-rates",
        "bonds",
        "commodities",
        "forex",
    }
)

# ── 科技赛道关键词 ──────────────────────────────────────────────────────────
_TECH_KW = (
    "artificial intelligence",
    " ai ",
    "openai",
    "chatgpt",
    "gpt-",
    "llm",
    "machine learning",
    "apple",
    "google",
    "meta",
    "microsoft",
    "nvidia",
    "tesla",
    "spacex",
    "tiktok",
    "twitter",
    "social media",
    "semiconductor",
    "chip",
    "quantum comput",
    "self-driving",
    "autonomous vehicle",
    "robot",
    "iphone",
    "android",
    "startup",
    "silicon valley",
    "cybersecur",
    "data breach",
    "hack",
)

_TECH_TAG_SLUGS = frozenset(
    {
        "tech",
        "ai",
        "apple",
        "google",
        "meta",
        "microsoft",
        "nvidia",
        "tesla",
        "spacex",
        "social-media",
        "technology",
    }
)

# ── 经济赛道关键词 ──────────────────────────────────────────────────────────
_ECONOMY_KW = (
    "gdp",
    "inflation",
    "cpi",
    "unemployment",
    "jobs report",
    "nonfarm",
    "non-farm",
    "payroll",
    "recession",
    "economic growth",
    "trade deficit",
    "trade surplus",
    "tariff",
    "trade war",
    "consumer confidence",
    "retail sales",
    "housing start",
    "pmi",
    "manufacturing",
    "supply chain",
    "labor market",
    "minimum wage",
    "poverty",
    "debt ceiling",
    "national debt",
    "fiscal",
    "stimulus",
    "subsidy",
)

_ECONOMY_TAG_SLUGS = frozenset(
    {
        "economy",
        "gdp",
        "inflation",
        "jobs",
        "trade",
        "economics",
        "recession",
        "tariffs",
    }
)

# ── 文化赛道关键词 ──────────────────────────────────────────────────────────
_POP_CULTURE_KW = (
    "movie",
    "film",
    "box office",
    "oscar",
    "grammy",
    "emmy",
    "golden globe",
    "academy award",
    "celebrity",
    "album",
    "billboard",
    "chart",
    "music",
    "concert",
    "tour",
    "netflix",
    "disney",
    "streaming",
    "tv show",
    "reality tv",
    "influencer",
    "youtube",
    "subscriber",
    "viral",
    "meme",
    "tiktok trend",
    "fashion",
    "red carpet",
    "scandal",
    "dating",
    "married",
    "pregnant",
    "baby",
    "divorce",
)

_POP_CULTURE_TAG_SLUGS = frozenset(
    {
        "pop-culture",
        "entertainment",
        "movies",
        "music",
        "celebrities",
        "tv",
        "culture",
        "media",
    }
)


def _match_any_keyword(text: str, keywords: tuple) -> bool:
    """关键词匹配，短单词（≤4字符纯字母）使用 word boundary 避免子串误匹配。"""
    for kw in keywords:
        if len(kw) <= 4 and kw.isalpha():
            if re.search(r"\b" + re.escape(kw) + r"\b", text):
                return True
        else:
            if kw in text:
                return True
    return False


def _is_crypto_price_market(question: str) -> bool:
    """
    验证含 crypto 关键词的市场问题是否为真正的价格/交易类市场。

    Phase 42: 防止 "Will bitcoin hit $1m before GTA VI" 之类的非价格市场被误分类。
    真正的 crypto 市场通常含有价格目标、日期约束、或交易所/链上操作的表达。
    """
    # 价格模式：$X, $X,XXX, $Xk, above/below X
    _CRYPTO_PRICE_PATTERNS = (
        r"\$[\d,]{2,}\b",  # $100, $1,000, $100000 (≥2 digits, excludes $1m)
        r"above\s+\$?\d",  # above $2100, above 2100
        r"below\s+\$?\d",  # below $2100
        r"reach\s+\$?\d",  # reach $100k
        r"hit\s+\$[\d,]{2,}\b",  # hit $100,000 (≥2 digits)
        r"drop\s+(?:to|below)\s+\$?\d",  # drop to $80k
        r"rise\s+(?:to|above)\s+\$?\d",  # rise to $100k
        r"close\s+(?:above|below)\s+\$?\d",  # close above $90k
        r"\d+k\b",  # 100k, 50k
        r"price\s+(?:of|at|above|below)",  # price of, price above
        r"by\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",  # by March 2026
        r"(?:end|start)\s+of\s+(?:20\d\d|q[1-4])",  # end of 2026, start of Q2
        r"(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d",
        r"etf\b",  # ETF approval
        r"halving",  # Bitcoin halving
        r"all[- ]time[- ]high",  # all-time high
        r"ath\b",  # ATH
        r"market\s+cap",  # market cap
        r"flippening",  # ETH flippening
    )
    q = question.lower()
    for pat in _CRYPTO_PRICE_PATTERNS:
        if re.search(pat, q):
            return True
    return False


def detect_category(market: dict) -> str:
    """
    检测市场所属赛道（8 类）。

    优先级: weather > sports > crypto > geopolitics > finance > tech > economy > pop_culture

    使用 question + tags 综合判断，tag_slug 匹配优先级最高。
    """
    # 延迟导入避免循环引用
    from src.utils.weather_fetcher import is_weather_market

    if is_weather_market(market):
        return "weather"

    question = (market.get("question") or "").lower()
    tags = market.get("tags") or []
    tag_slugs = set()
    for t in tags:
        if isinstance(t, dict):
            s = (t.get("slug") or t.get("label") or "").lower()
        else:
            s = str(t).lower()
        if s:
            tag_slugs.add(s)
    tag_str = " ".join(tag_slugs)
    combined = question + " " + tag_str

    # ── T101: P1-11 电竞/娱乐检测（MUST be before Sports）─────────────
    _ESPORTS_TAG_SLUGS = frozenset(
        {
            "dota-2",
            "counter-strike",
            "valorant",
            "league-of-legends",
            "esports",
        }
    )
    _ESPORTS_KW = (
        "dota 2",
        "dota2",
        "counter-strike",
        "cs2",
        "csgo",
        "cs:go",
        "valorant",
        "league of legends",
        "lol worlds",
        "starcraft",
        "overwatch",
        "esl pro league",
        "pgl ",
        "vct ",
        "blast premier",
        "iem ",
        "major qualifier",
    )
    if tag_slugs & _ESPORTS_TAG_SLUGS:
        return "esports"
    if _match_any_keyword(combined, _ESPORTS_KW):
        return "esports"

    # ── Sports 检测（tag_slug 优先）──────────────────────────────────
    if tag_slugs & _SPORTS_TAG_SLUGS:
        return "sports"

    # "Team A vs Team B" 格式的市场极大概率是体育
    if " vs " in question or " vs. " in question:
        # 排除政治辩论等（含 debate/election 关键词的不计）
        if not any(kw in combined for kw in ("debate", "election", "poll")):
            return "sports"

    if _match_any_keyword(combined, _SPORTS_LEAGUE_KW):
        return "sports"
    if _match_any_keyword(combined, _SPORTS_GENERIC_KW):
        if " vs " in question or _match_any_keyword(combined, _SPORTS_LEAGUE_KW):
            return "sports"

    # ── Crypto（tag_slug 优先，关键词需验证价格模式）──────────────────
    # Phase 42 Fix: 纯关键词匹配可能误分类（如"Bitcoin before GTA VI"不是价格市场）
    # tag_slug 匹配 → 直接确认（平台已标记）
    _CRYPTO_TAG_SLUGS = frozenset(
        {"crypto", "cryptocurrency", "bitcoin", "ethereum", "defi", "nft"}
    )
    if tag_slugs & _CRYPTO_TAG_SLUGS:
        return "crypto"
    # 关键词匹配 → 需要额外验证是否为真正的价格/交易类市场
    if _match_any_keyword(combined, _CRYPTO_KW):
        if _is_crypto_price_market(question):
            return "crypto"
        else:
            logger.debug(
                f"[CategoryDetector] 含crypto关键词但非价格市场，降级分类: "
                f"{question[:60]}"
            )

    # ── Geopolitics + Politics（合并）─────────────────────────────────
    if tag_slugs & _GEO_TAG_SLUGS:
        return "geopolitics"
    if _match_any_keyword(combined, _GEO_KW):
        return "geopolitics"

    # ── Finance（tag_slug 优先）────────────────────────────────────────
    if tag_slugs & _FINANCE_TAG_SLUGS:
        return "finance"
    if _match_any_keyword(combined, _FINANCE_KW):
        return "finance"

    # ── Tech（tag_slug 优先）──────────────────────────────────────────
    if tag_slugs & _TECH_TAG_SLUGS:
        return "tech"
    if _match_any_keyword(combined, _TECH_KW):
        return "tech"

    # ── Economy（tag_slug 优先）────────────────────────────────────────
    if tag_slugs & _ECONOMY_TAG_SLUGS:
        return "economy"
    if _match_any_keyword(combined, _ECONOMY_KW):
        return "economy"

    # ── Pop Culture（tag_slug 优先）────────────────────────────────────
    if tag_slugs & _POP_CULTURE_TAG_SLUGS:
        return "pop_culture"
    if _match_any_keyword(combined, _POP_CULTURE_KW):
        return "pop_culture"

    return "default"


def _has_sports_tag(tags: list) -> bool:
    """检查 market tags 是否包含已知体育 tag_slug。"""
    for t in tags:
        if isinstance(t, dict):
            slug = (t.get("slug") or t.get("label") or "").lower()
        else:
            slug = str(t).lower()
        if slug in _SPORTS_TAG_SLUGS:
            return True
    return False


def _has_tag(tags: list, known_slugs: frozenset) -> bool:
    """通用 tag_slug 匹配。"""
    for t in tags:
        if isinstance(t, dict):
            slug = (t.get("slug") or t.get("label") or "").lower()
        else:
            slug = str(t).lower()
        if slug in known_slugs:
            return True
    return False


def is_sports_market(market: dict) -> bool:
    """快捷判断：该市场是否属于体育赛道。"""
    return detect_category(market) == "sports"
