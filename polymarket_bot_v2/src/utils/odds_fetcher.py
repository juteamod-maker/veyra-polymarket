"""
odds_fetcher.py — 体育赔率实时数据抓取工具（OddsPapi PRO 极限压榨版）

职责：
  - 从 Polymarket 体育类市场 question 中解析对阵双方
  - 调用 OddsPapi API (odds-api1.p.rapidapi.com) 获取多家博彩公司的赔率
  - 从同一 API 获取近期比分数据（包含在 fixtures 中）
  - 构建 odds_context + scores_context 注入 AI Prompt
  - [NEW] 今日赛程预加载（/fixtures/today）
  - [NEW] 进行中赛事监控（/fixtures/live）
  - [NEW] CLV 开收赔对比验证（/fixtures/odds/clv）
  - [NEW] 独立结算核对（/fixtures/settlement）
  - [NEW] 元数据初始化（/sports + /tournaments）
  - [NEW] API 配额统计

API 来源：
  OddsPapi via RapidAPI (RAPIDAPI_KEY) — PRO $19/月
  10,000 requests/month HARD LIMIT, 3,600/hour
  配额分配：见 config odds_fetcher 节
"""

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from src.utils.config_loader import get as config_get

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OddsPapi 锦标赛 ID 映射
# ---------------------------------------------------------------------------

# (tournamentId, sportName)
_TOURNAMENT_INFO: dict[str, tuple[int, str]] = {
    # 联赛关键词
    "nba": (132, "Basketball"),
    "wnba": (486, "Basketball"),
    "nhl": (234, "Ice Hockey"),
    "nfl": (31, "American Football"),
    "mlb": (109, "Baseball"),
    "epl": (17, "Soccer"),
    "premier league": (17, "Soccer"),
    "bundesliga": (35, "Soccer"),
    "serie a": (23, "Soccer"),
    "ligue 1": (34, "Soccer"),
    "mls": (242, "Soccer"),
}

# 队名 → tournamentId 列表映射（同名队伍可能跨联赛）
_TEAM_TO_TOURNAMENT: dict[str, list[int]] = {}

_NBA_TEAMS = [
    "lakers",
    "celtics",
    "warriors",
    "nets",
    "knicks",
    "bucks",
    "76ers",
    "suns",
    "nuggets",
    "heat",
    "mavericks",
    "clippers",
    "thunder",
    "cavaliers",
    "grizzlies",
    "timberwolves",
    "pacers",
    "hawks",
    "bulls",
    "rockets",
    "pistons",
    "magic",
    "pelicans",
    "kings",
    "raptors",
    "spurs",
    "jazz",
    "wizards",
    "hornets",
    "blazers",
    "trail blazers",
]
_NHL_TEAMS = [
    "bruins",
    "maple leafs",
    "canadiens",
    "rangers",
    "penguins",
    "blackhawks",
    "red wings",
    "avalanche",
    "lightning",
    "panthers",
    "oilers",
    "flames",
    "canucks",
    "kraken",
    "golden knights",
    "wild",
    "predators",
    "blues",
    "stars",
    "senators",
    "hurricanes",
    "islanders",
    "flyers",
    "capitals",
    "devils",
    "sabres",
    "coyotes",
    "sharks",
    "blue jackets",
    "jets",
    "ducks",
]
_MLB_TEAMS = [
    "yankees",
    "dodgers",
    "red sox",
    "cubs",
    "mets",
    "astros",
    "braves",
    "phillies",
    "padres",
    "cardinals",
    "giants",
    "brewers",
    "orioles",
    "guardians",
    "twins",
    "royals",
    "tigers",
    "white sox",
    "mariners",
    "angels",
    "athletics",
    "rays",
    "blue jays",
    "reds",
    "pirates",
    "nationals",
    "marlins",
    "rockies",
    "diamondbacks",
    "rangers",
]
_NFL_TEAMS = [
    "chiefs",
    "eagles",
    "bills",
    "cowboys",
    "49ers",
    "dolphins",
    "ravens",
    "lions",
    "packers",
    "bengals",
    "vikings",
    "chargers",
    "jaguars",
    "texans",
    "seahawks",
    "bears",
    "steelers",
    "saints",
    "rams",
    "broncos",
    "raiders",
    "colts",
    "cardinals",
    "falcons",
    "commanders",
    "browns",
    "titans",
    "panthers",
    "buccaneers",
    "patriots",
    "jets",
    "giants",
]

for _t in _NBA_TEAMS:
    _TEAM_TO_TOURNAMENT.setdefault(_t, []).append(132)
for _t in _NHL_TEAMS:
    _TEAM_TO_TOURNAMENT.setdefault(_t, []).append(234)
for _t in _MLB_TEAMS:
    _TEAM_TO_TOURNAMENT.setdefault(_t, []).append(109)
for _t in _NFL_TEAMS:
    _TEAM_TO_TOURNAMENT.setdefault(_t, []).append(31)

# 元数据增强标志位（懒加载 one-shot）
_metadata_enriched: bool = False

# sportId → sportName 映射（API 填充）
_SPORT_ID_TO_NAME: dict[int, str] = {
    10: "Soccer",
    11: "Basketball",
    12: "Tennis",
    13: "Cricket",
    14: "American Football",
    15: "Ice Hockey",
    16: "Baseball",
    22: "MMA",
}

# ---------------------------------------------------------------------------
# 缓存与配额（config 驱动，PRO 10K/月极限压榨）
# ---------------------------------------------------------------------------

# 主赔率缓存 {tournamentId: (timestamp, fixtures_list)}
_cache: dict[int, tuple[float, list]] = {}

# 今日赛程缓存 {sport_id: (timestamp, fixtures_list)}
_today_cache: dict[int, tuple[float, list]] = {}

# 进行中赛事缓存 {sport_id: (timestamp, fixtures_list)}
_live_cache: dict[int, tuple[float, list]] = {}

# CLV 缓存 {fixture_id: (timestamp, data)}
_clv_cache: dict[str, tuple[float, dict]] = {}

# 结算缓存 {fixture_id: (timestamp, data)}
_settlement_cache: dict[str, tuple[float, dict]] = {}

# 元数据缓存
_metadata_cache: dict[str, tuple[float, list]] = {}


def _get_cache_ttl(key: str, default: int) -> int:
    """从 config 读取缓存 TTL，带默认值。"""
    return int(config_get(f"odds_fetcher.{key}") or default)


# 每日 API 调用计数（per-endpoint 追踪，共享总上限）
_daily_calls: dict[str, object] = {"date": None, "total": 0, "by_endpoint": {}}
# T101: P2-4 线程安全锁
_daily_calls_lock = threading.Lock()


def _get_max_daily_calls() -> int:
    """从 config 读取每日上限。"""
    return int(config_get("odds_fetcher.max_daily_calls") or 300)


# 首选博彩公司（按优先级，可被 config 覆盖）
_DEFAULT_PREFERRED_BOOKMAKERS = [
    "pinnacle",
    "bet365",
    "draftkings",
    "betmgm",
    "fanduel",
    "unibet",
    "888sport",
    "bwin",
]


def _get_preferred_bookmakers() -> list[str]:
    """从 config 读取首选博彩公司列表。"""
    cfg = config_get("odds_fetcher.preferred_bookmakers")
    if isinstance(cfg, list) and cfg:
        return cfg
    return _DEFAULT_PREFERRED_BOOKMAKERS


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------


def fetch_odds_context(question: str) -> str:
    """
    从市场 question 中解析对阵双方，获取 OddsPapi 实时赔率，构建 AI 上下文。

    Returns:
        格式化的赔率上下文字符串（空字符串表示无数据或获取失败）
    """
    if not question:
        return ""

    if not os.environ.get("RAPIDAPI_KEY", "").strip():
        logger.debug("[OddsFetcher] 无 RAPIDAPI_KEY，跳过赔率查询")
        return ""

    tournament_id, sport_name = _detect_tournament(question)
    if not tournament_id:
        logger.debug(f"[OddsFetcher] 无法识别赛事: {question[:60]}")
        return ""

    fixtures = _get_fixtures_odds(tournament_id)
    if not fixtures:
        return ""

    team_keywords = _extract_team_keywords(question)
    if not team_keywords:
        return ""

    matched = _match_fixture(fixtures, team_keywords)
    if not matched:
        logger.info(f"[OddsFetcher] 未匹配到赛事: {team_keywords}")
        return ""

    moneyline = _extract_moneyline(matched)
    return _build_context(matched, moneyline)


def fetch_odds_implied_prob(question: str) -> Optional[dict]:
    """
    获取 OddsPapi 博彩公司归一化隐含概率（结构化数据），用于交叉验证。

    复用 _get_fixtures_odds 缓存，无额外 API 调用。

    Returns:
        {"participant1": str, "participant2": str,
         "participant1_prob": float, "participant2_prob": float} or None
    """
    if not question or not os.environ.get("RAPIDAPI_KEY", "").strip():
        return None

    tournament_id, _ = _detect_tournament(question)
    if not tournament_id:
        return None

    fixtures = _get_fixtures_odds(tournament_id)
    if not fixtures:
        return None

    team_keywords = _extract_team_keywords(question)
    if not team_keywords:
        return None

    matched = _match_fixture(fixtures, team_keywords)
    if not matched:
        return None

    moneyline = _extract_moneyline(matched)
    p1_odds = moneyline.get("participant1", [])
    p2_odds = moneyline.get("participant2", [])
    if not p1_odds or not p2_odds:
        return None

    participants = matched.get("participants") or {}
    p1_name = participants.get("participant1Name", "")
    p2_name = participants.get("participant2Name", "")

    avg_p1_ip = sum(ip for _, _, ip in p1_odds[:5]) / min(len(p1_odds), 5)
    avg_p2_ip = sum(ip for _, _, ip in p2_odds[:5]) / min(len(p2_odds), 5)
    total_ip = avg_p1_ip + avg_p2_ip
    if total_ip <= 0:
        return None

    return {
        "participant1": p1_name,
        "participant2": p2_name,
        "participant1_prob": round(avg_p1_ip / total_ip, 4),
        "participant2_prob": round(avg_p2_ip / total_ip, 4),
    }


def fetch_scores_context(question: str) -> str:
    """
    获取近期比赛比分，构建 AI 上下文。

    复用 /fixtures/odds/main 的缓存数据（包含已结束比赛的比分），
    无需额外 API 调用。

    Returns:
        格式化的近期比分上下文字符串（空字符串表示无数据）
    """
    if not question:
        return ""

    if not os.environ.get("RAPIDAPI_KEY", "").strip():
        return ""

    tournament_id, _ = _detect_tournament(question)
    if not tournament_id:
        return ""

    team_keywords = _extract_team_keywords(question)
    if not team_keywords:
        return ""

    fixtures = _get_fixtures_odds(tournament_id)
    if not fixtures:
        return ""

    relevant = _filter_team_fixtures(fixtures, team_keywords)
    if not relevant:
        return ""

    return _build_scores_context(relevant, team_keywords)


def fetch_today_context(question: str) -> str:
    """
    获取今日赛程概览上下文，帮助 AI 了解当天比赛安排。

    用于 SignalEngine._fetch_category_context() 的体育分支，
    在 fetch_odds_context() 之前调用，提供整体赛程背景。

    Returns:
        格式化的今日赛程摘要（空字符串 = 无数据/API不可用）
    """
    if not question or not os.environ.get("RAPIDAPI_KEY", "").strip():
        return ""

    tournament_id, sport_name = _detect_tournament(question)
    if not tournament_id:
        return ""

    # 从 tournament_id 反推 sport_id
    sport_id = _tournament_to_sport_id(tournament_id)
    if not sport_id:
        return ""

    fixtures = fetch_today_fixtures(sport_id=sport_id)
    if not fixtures:
        return ""

    # 构建简要赛程概览
    lines = [f"📅 Today's {sport_name} Schedule ({len(fixtures)} games):"]
    shown = 0
    for f in fixtures[:12]:  # 最多展示12场
        p = f.get("participants") or {}
        p1 = p.get("participant1Name", "?")
        p2 = p.get("participant2Name", "?")
        status = (f.get("status") or {}).get("statusName", "")
        start_ts = f.get("startTime", 0)
        time_str = ""
        if start_ts:
            try:
                dt = datetime.fromtimestamp(int(start_ts), tz=timezone.utc)
                time_str = dt.strftime("%H:%M UTC")
            except Exception:
                pass
        line = f"  • {p1} vs {p2}"
        if time_str:
            line += f" ({time_str})"
        if status and status.lower() not in ("not started", "pre-game", ""):
            line += f" [{status}]"
        lines.append(line)
        shown += 1

    if len(fixtures) > shown:
        lines.append(f"  ... and {len(fixtures) - shown} more games")

    return "\n".join(lines)


def check_fixture_live(question: str) -> bool:
    """
    检查 question 对应的比赛是否正在进行中。

    用于 OutcomeTracker.check_stop_loss_exits()，
    提供比时间推算更精准的比赛状态判断。

    Returns:
        True = 找到匹配的进行中比赛
    """
    if not question or not os.environ.get("RAPIDAPI_KEY", "").strip():
        return False

    tournament_id, _ = _detect_tournament(question)
    if not tournament_id:
        return False

    sport_id = _tournament_to_sport_id(tournament_id)
    if not sport_id:
        return False

    live = fetch_live_fixtures(sport_id=sport_id)
    if not live:
        return False

    team_keywords = _extract_team_keywords(question)
    if not team_keywords:
        return False

    # 在 live fixtures 中搜索匹配的比赛
    matched = _match_fixture(live, team_keywords)
    return matched is not None


def fetch_settlement_verification(
    question: str,
) -> Optional[dict]:
    """
    获取体育比赛的独立结算验证 + CLV 边际分析。

    用于 OutcomeTracker.check_and_update() 结算后调用。

    Returns:
        {"settlement": dict|None, "clv_edge": dict|None, "fixture_id": str}
        None = 无法匹配/API不可用
    """
    if not question or not os.environ.get("RAPIDAPI_KEY", "").strip():
        return None

    tournament_id, _ = _detect_tournament(question)
    if not tournament_id:
        return None

    fixtures = _get_fixtures_odds(tournament_id)
    if not fixtures:
        return None

    team_keywords = _extract_team_keywords(question)
    if not team_keywords:
        return None

    matched = _match_fixture(fixtures, team_keywords)
    if not matched:
        return None

    fixture_id = matched.get("fixtureId", "")
    if not fixture_id:
        return None

    result: dict = {"fixture_id": fixture_id, "settlement": None, "clv_edge": None}

    # 1. 独立结算验证
    settlement = fetch_fixture_settlement(fixture_id)
    if settlement:
        result["settlement"] = settlement

    # 2. CLV 边际分析
    clv_data = fetch_fixture_clv(fixture_id)
    if clv_data:
        result["clv_edge"] = analyze_clv_edge(clv_data)

    return result


def _tournament_to_sport_id(tournament_id: int) -> int:
    """从 tournamentId 反推 sportId（用于 /fixtures/today 等需要 sportId 的接口）。"""
    _TID_TO_SPORT: dict[int, int] = {
        # Basketball
        132: 11,
        486: 11,
        # Ice Hockey
        234: 15,
        # American Football
        31: 14,
        # Baseball
        109: 16,
        # Soccer
        17: 10,
        35: 10,
        23: 10,
        34: 10,
        242: 10,
    }
    sport_id = _TID_TO_SPORT.get(tournament_id)
    if sport_id:
        return sport_id
    # 尝试从 API 增强的映射中查找
    for _, (tid, sport_name) in _TOURNAMENT_INFO.items():
        if tid == tournament_id:
            for sid, sname in _SPORT_ID_TO_NAME.items():
                if sname.lower() == sport_name.lower():
                    return sid
    return 0


# ---------------------------------------------------------------------------
# 内部方法
# ---------------------------------------------------------------------------


def _enrich_tournament_info() -> None:
    """
    用 OddsPapi /sports + /tournaments 动态扩展 _TOURNAMENT_INFO。

    懒加载（one-shot）：首次调用 _detect_tournament() 时触发。
    只补充不覆盖（手动映射更精确）。
    失败不阻塞（API 不可用时静默降级）。
    """
    global _metadata_enriched
    if _metadata_enriched:
        return
    _metadata_enriched = True

    if not os.environ.get("RAPIDAPI_KEY", "").strip():
        return

    try:
        target_sports = config_get("odds_fetcher.target_sport_ids") or [
            10,
            11,
            12,
            13,
            14,
            15,
            16,
            22,
        ]
        added = 0
        for sport_id in target_sports:
            sport_name = _SPORT_ID_TO_NAME.get(sport_id, "")
            tournaments = fetch_tournaments(sport_id)
            for t in tournaments:
                t_name = (t.get("tournamentName") or "").strip().lower()
                if not t_name:
                    continue
                t_id = t.get("tournamentId")
                if not t_id:
                    continue
                s_name = sport_name or t.get("sportName", "")
                # 只补充不覆盖
                if t_name not in _TOURNAMENT_INFO:
                    _TOURNAMENT_INFO[t_name] = (int(t_id), s_name)
                    added += 1
        if added > 0:
            logger.info(
                f"[OddsFetcher] 元数据增强: 新增 {added} 个锦标赛映射 "
                f"(总计 {len(_TOURNAMENT_INFO)})"
            )
    except Exception as e:
        logger.debug(f"[OddsFetcher] 元数据增强失败(非致命): {e}")


def _detect_tournament(question: str) -> tuple[int, str]:
    """
    从 question 中检测锦标赛，返回 (tournamentId, sportName)。
    未识别返回 (0, "")。
    对于跨联赛同名队伍（如 Rangers），使用投票计数选择最可能的锦标赛。
    """
    # 懒加载元数据扩展（one-shot）
    _enrich_tournament_info()

    q_lower = question.lower()

    # 1. 联赛关键词匹配（优先级最高）
    for keyword, (tid, sport) in _TOURNAMENT_INFO.items():
        if keyword in q_lower:
            return tid, sport

    # 2. 队名推断 — 投票制
    tid_to_sport = {
        132: "Basketball",
        234: "Ice Hockey",
        109: "Baseball",
        31: "American Football",
    }
    votes: dict[int, int] = {}
    for team_name, tid_list in _TEAM_TO_TOURNAMENT.items():
        if team_name in q_lower:
            for tid in tid_list:
                votes[tid] = votes.get(tid, 0) + 1

    if votes:
        best_tid = max(votes, key=votes.get)  # type: ignore[arg-type]
        return best_tid, tid_to_sport.get(best_tid, "")

    return 0, ""


def _check_daily_limit() -> bool:
    """检查是否超出每日 API 调用限制。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _daily_calls_lock:
        if _daily_calls["date"] != today:
            _daily_calls["date"] = today
            _daily_calls["total"] = 0
            _daily_calls["by_endpoint"] = {}
        return int(_daily_calls["total"]) < _get_max_daily_calls()


def _increment_daily_calls(endpoint: str = "/fixtures/odds/main") -> None:
    """递增每日调用计数（per-endpoint 追踪）。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _daily_calls_lock:
        if _daily_calls["date"] != today:
            _daily_calls["date"] = today
            _daily_calls["total"] = 0
            _daily_calls["by_endpoint"] = {}
        _daily_calls["total"] = int(_daily_calls["total"]) + 1
        by_ep = _daily_calls["by_endpoint"]
        if not isinstance(by_ep, dict):
            by_ep = {}
            _daily_calls["by_endpoint"] = by_ep
        by_ep[endpoint] = by_ep.get(endpoint, 0) + 1


def _get_fixtures_odds(tournament_id: int) -> list:
    """
    获取指定锦标赛的赛程 + 赔率（带 config 驱动缓存 + 每日调用限制）。

    通过 OddsPapi /fixtures/odds/main?tournamentId=X 端点获取，
    单次请求返回 ~100 场比赛的完整赔率。
    """
    cache_ttl = _get_cache_ttl("main_cache_ttl_seconds", 7200)
    now = time.time()
    if tournament_id in _cache:
        ts, data = _cache[tournament_id]
        if now - ts < cache_ttl:
            return data

    if not _check_daily_limit():
        logger.warning(
            f"[OddsFetcher] 每日 API 限额已达 {_get_max_daily_calls()} 次，"
            "跳过赔率查询（防止超出配额）"
        )
        return []

    try:
        from src.utils.rapidapi_client import fetch as rapidapi_fetch

        data = rapidapi_fetch(
            "odds",
            "/fixtures/odds/main",
            params={"tournamentId": str(tournament_id)},
            cache_ttl=cache_ttl,
        )
        if data is not None:
            fixtures = data if isinstance(data, list) else []
            _cache[tournament_id] = (now, fixtures)
            _increment_daily_calls("/fixtures/odds/main")
            logger.info(
                f"[OddsFetcher] OddsPapi tournamentId={tournament_id}: "
                f"{len(fixtures)} fixtures loaded"
            )
            return fixtures
    except Exception as e:
        # T101: P2-3 日志包含请求参数 + P1-6 失败时也计入配额（API已消耗）
        logger.warning(
            f"[OddsFetcher] OddsPapi 请求异常: {e} | "
            f"endpoint=/fixtures/odds/main, tournamentId={tournament_id}"
        )
        _increment_daily_calls("/fixtures/odds/main")

    return []


def _extract_team_keywords(question: str) -> list[str]:
    """从 question 中提取队名关键词。"""
    # 匹配 "Team1 vs Team2" 或 "Team1 to beat Team2"
    vs_match = re.search(
        r"(?:will\s+(?:the\s+)?)?(.+?)\s+(?:vs?\.?|beat|defeat|win against)\s+(?:the\s+)?(.+?)(?:\s+(?:in|on|at|by)\b|\?|$)",
        question,
        re.IGNORECASE,
    )
    if vs_match:
        return [vs_match.group(1).strip(), vs_match.group(2).strip()]

    # 匹配 "Will X win"
    win_match = re.search(
        r"(?:will\s+(?:the\s+)?)?(.+?)\s+win\b",
        question,
        re.IGNORECASE,
    )
    if win_match:
        return [win_match.group(1).strip()]

    return []


def _match_fixture(fixtures: list, team_keywords: list[str]) -> Optional[dict]:
    """
    从 OddsPapi fixtures 中匹配包含指定队名的赛事。
    优先匹配未开始/进行中的比赛。
    """
    best_match: Optional[dict] = None
    best_score = 0
    best_is_upcoming = False

    for fixture in fixtures:
        participants = fixture.get("participants") or {}
        p1 = (participants.get("participant1Name") or "").lower()
        p2 = (participants.get("participant2Name") or "").lower()
        fixture_str = f"{p1} {p2}"

        matched = sum(1 for kw in team_keywords if kw.lower() in fixture_str)
        if matched == 0:
            continue

        status_name = (fixture.get("status") or {}).get("statusName", "")
        is_upcoming = status_name in (
            "Not Started",
            "Pre-Game",
            "Live",
            "1st Half",
            "2nd Half",
        )

        # 优先选未开始/进行中的比赛，其次选匹配数最多的
        if (is_upcoming and not best_is_upcoming) or (
            is_upcoming == best_is_upcoming and matched > best_score
        ):
            best_match = fixture
            best_score = matched
            best_is_upcoming = is_upcoming

    return best_match


def _extract_moneyline(fixture: dict) -> dict[str, list[tuple[str, float, float]]]:
    """
    从赛事赔率中提取 moneyline（胜负盘）。

    Returns:
        {
            "participant1": [(bookmaker, price, implied_prob), ...],
            "participant2": [(bookmaker, price, implied_prob), ...],
        }
    """
    odds_data = fixture.get("odds") or {}
    result: dict[str, list[tuple[str, float, float]]] = {
        "participant1": [],
        "participant2": [],
    }

    # 按首选博彩公司优先顺序提取
    checked_bookmakers: list[str] = []
    preferred = _get_preferred_bookmakers()
    for bm_slug in preferred:
        if bm_slug in odds_data:
            checked_bookmakers.append(bm_slug)
    # 补充其他非首选但有数据的博彩公司
    for bm_slug in odds_data:
        if bm_slug not in checked_bookmakers:
            checked_bookmakers.append(bm_slug)

    for bm_slug in checked_bookmakers:
        bm_odds = odds_data[bm_slug]
        if not isinstance(bm_odds, dict):
            continue

        # 按 marketId 分组，找只有 2 个 outcome 的市场（= moneyline）
        market_groups: dict[int, list[dict]] = {}
        for entry in bm_odds.values():
            if not isinstance(entry, dict) or not entry.get("active"):
                continue
            mid = entry.get("marketId")
            if mid is not None:
                market_groups.setdefault(mid, []).append(entry)

        for mid, outcomes in market_groups.items():
            if len(outcomes) != 2:
                continue

            # 按 outcomeId 排序：低ID = participant1，高ID = participant2
            outcomes.sort(key=lambda x: x.get("outcomeId", 0))
            p1_price = outcomes[0].get("price", 0)
            p2_price = outcomes[1].get("price", 0)

            if p1_price > 0 and p2_price > 0:
                result["participant1"].append((bm_slug, p1_price, 1.0 / p1_price))
                result["participant2"].append((bm_slug, p2_price, 1.0 / p2_price))
                break  # 每个博彩公司只取第一个 moneyline 市场

    return result


def _build_context(fixture: dict, moneyline: dict) -> str:
    """从匹配的 OddsPapi fixture 构建赔率上下文。"""
    participants = fixture.get("participants") or {}
    p1_name = participants.get("participant1Name", "?")
    p2_name = participants.get("participant2Name", "?")

    status_info = fixture.get("status") or {}
    status_name = status_info.get("statusName", "Unknown")
    start_time = fixture.get("startTime")

    lines = ["[Real-time Betting Odds — OddsPapi]"]
    lines.append(f"  Match: {p1_name} vs {p2_name}")
    lines.append(f"  Status: {status_name}")

    if start_time:
        try:
            dt = datetime.fromtimestamp(start_time, tz=timezone.utc)
            lines.append(f"  Start: {dt.strftime('%Y-%m-%d %H:%M UTC')}")
        except (OSError, ValueError):
            pass

    # 如果有比分
    scores = fixture.get("scores") or {}
    fulltime = scores.get("fulltime")
    if fulltime:
        s1 = fulltime.get("participant1Score", "?")
        s2 = fulltime.get("participant2Score", "?")
        lines.append(f"  Score: {p1_name} {s1} - {s2} {p2_name}")

    # 赔率数据
    p1_odds_list = moneyline.get("participant1", [])
    p2_odds_list = moneyline.get("participant2", [])

    if not p1_odds_list:
        lines.append("  No moneyline odds available")
        return "\n".join(lines)

    lines.append("  Moneyline Odds (decimal):")
    for i, (bm, p1_price, _) in enumerate(p1_odds_list[:5]):
        _, p2_price, _ = p2_odds_list[i] if i < len(p2_odds_list) else ("", 0, 0)
        p2_str = f"{p2_price:.2f}" if p2_price else "N/A"
        lines.append(f"    {bm}: {p1_name} {p1_price:.2f} / {p2_name} {p2_str}")

    # 计算平均隐含概率（归一化去 overround）
    if len(p1_odds_list) >= 1 and len(p2_odds_list) >= 1:
        avg_p1_ip = sum(ip for _, _, ip in p1_odds_list[:5]) / min(len(p1_odds_list), 5)
        avg_p2_ip = sum(ip for _, _, ip in p2_odds_list[:5]) / min(len(p2_odds_list), 5)
        total_ip = avg_p1_ip + avg_p2_ip
        if total_ip > 0:
            norm_p1 = avg_p1_ip / total_ip * 100
            norm_p2 = avg_p2_ip / total_ip * 100
            lines.append(f"\n  Implied probability (normalized):")
            lines.append(f"    {p1_name}: {norm_p1:.1f}%")
            lines.append(f"    {p2_name}: {norm_p2:.1f}%")
            lines.append(
                "  Analysis: Compare Polymarket price vs bookmaker implied probability. "
                "Large discrepancies suggest potential mispricing."
            )

    context = "\n".join(lines)
    bm_count = len(p1_odds_list)
    logger.info(f"[OddsFetcher] {p1_name} vs {p2_name}: {bm_count} bookmakers")
    return context


# ---------------------------------------------------------------------------
# 近期比分上下文（复用 /fixtures/odds/main 缓存）
# ---------------------------------------------------------------------------


def _filter_team_fixtures(fixtures: list[dict], team_keywords: list[str]) -> list[dict]:
    """筛选包含目标队伍的已结束/进行中比赛。"""
    relevant = []
    for fixture in fixtures:
        participants = fixture.get("participants") or {}
        p1 = (participants.get("participant1Name") or "").lower()
        p2 = (participants.get("participant2Name") or "").lower()
        combined = f"{p1} {p2}"
        for kw in team_keywords:
            if kw.lower() in combined:
                relevant.append(fixture)
                break
    return relevant


def _build_scores_context(relevant: list[dict], team_keywords: list[str]) -> str:
    """将近期比分格式化为 AI 上下文。"""
    # 只包含已结束的比赛
    finished = [
        f for f in relevant if (f.get("status") or {}).get("statusName") == "Finished"
    ]
    if not finished:
        return ""

    lines = ["\n[Recent Scores — OddsPapi]"]

    for fixture in finished[:6]:
        participants = fixture.get("participants") or {}
        p1 = participants.get("participant1Name", "?")
        p2 = participants.get("participant2Name", "?")

        scores = fixture.get("scores") or {}
        fulltime = scores.get("fulltime")
        if fulltime:
            s1 = fulltime.get("participant1Score", "?")
            s2 = fulltime.get("participant2Score", "?")
            score_str = f"{s1}-{s2}"
        else:
            score_str = "N/A"

        start_time = fixture.get("startTime")
        date_str = ""
        if start_time:
            try:
                dt = datetime.fromtimestamp(start_time, tz=timezone.utc)
                date_str = dt.strftime("%m-%d")
            except (OSError, ValueError):
                pass

        lines.append(f"  {date_str} {p1} vs {p2}: {score_str} (FINAL)")

    # 计算简单胜率统计
    wins = 0
    total = 0
    main_team = team_keywords[0].lower() if team_keywords else ""
    for fixture in finished:
        scores = fixture.get("scores") or {}
        fulltime = scores.get("fulltime")
        if not fulltime:
            continue
        s1 = fulltime.get("participant1Score", 0)
        s2 = fulltime.get("participant2Score", 0)
        if not isinstance(s1, (int, float)) or not isinstance(s2, (int, float)):
            continue

        participants = fixture.get("participants") or {}
        p1 = (participants.get("participant1Name") or "").lower()
        p2 = (participants.get("participant2Name") or "").lower()

        if main_team in p1:
            total += 1
            if s1 > s2:
                wins += 1
        elif main_team in p2:
            total += 1
            if s2 > s1:
                wins += 1

    if total > 0:
        pct = round(wins / total * 100)
        lines.append(
            f"\n  Recent form ({team_keywords[0]}): "
            f"{wins}W-{total - wins}L ({pct}%)"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# [NEW] OddsPapi PRO 新端点函数（极限压榨 10K/月配额）
# ---------------------------------------------------------------------------


def _rapidapi_call(
    path: str, params: Optional[dict] = None, cache_ttl: int = 7200
) -> Optional[dict | list]:
    """统一 OddsPapi API 调用入口（含配额检查）。"""
    if not os.environ.get("RAPIDAPI_KEY", "").strip():
        logger.debug("[OddsFetcher] 无 RAPIDAPI_KEY，跳过请求")
        return None

    if not _check_daily_limit():
        logger.warning(
            f"[OddsFetcher] 每日 API 限额已达 {_get_max_daily_calls()} 次，"
            f"跳过 {path}"
        )
        return None

    try:
        from src.utils.rapidapi_client import fetch as rapidapi_fetch

        data = rapidapi_fetch("odds", path, params=params, cache_ttl=cache_ttl)
        if data is not None:
            _increment_daily_calls(path)
            return data
    except Exception as e:
        logger.warning(f"[OddsFetcher] OddsPapi {path} 请求异常: {e}")

    return None


def fetch_today_fixtures(sport_id: int = 0) -> list[dict]:
    """
    获取今日所有赛事（/fixtures/today）。

    Args:
        sport_id: 运动ID（10=Soccer,11=Basketball,14=AmFootball,12=Tennis）。
                  0 = 不过滤，拉全部。

    Returns:
        fixture 列表（包含 participants, status, startTime, tournament 等）
    """
    cache_ttl = _get_cache_ttl("today_cache_ttl_seconds", 14400)
    now = time.time()

    cached = _today_cache.get(sport_id)
    if cached and now - cached[0] < cache_ttl:
        return cached[1]

    params: dict[str, str] = {}
    if sport_id > 0:
        params["sportId"] = str(sport_id)

    data = _rapidapi_call("/fixtures/today", params=params, cache_ttl=cache_ttl)
    if data is not None:
        fixtures = data if isinstance(data, list) else []
        _today_cache[sport_id] = (now, fixtures)
        logger.info(
            f"[OddsFetcher] /fixtures/today sportId={sport_id}: "
            f"{len(fixtures)} fixtures"
        )
        return fixtures

    return []


def fetch_live_fixtures(sport_id: int = 0) -> list[dict]:
    """
    获取当前进行中赛事（/fixtures/live）。

    Args:
        sport_id: 运动ID。0 = 不过滤。

    Returns:
        live fixture 列表
    """
    cache_ttl = _get_cache_ttl("live_cache_ttl_seconds", 1800)
    now = time.time()

    cached = _live_cache.get(sport_id)
    if cached and now - cached[0] < cache_ttl:
        return cached[1]

    params: dict[str, str] = {}
    if sport_id > 0:
        params["sportId"] = str(sport_id)

    data = _rapidapi_call("/fixtures/live", params=params, cache_ttl=cache_ttl)
    if data is not None:
        fixtures = data if isinstance(data, list) else []
        _live_cache[sport_id] = (now, fixtures)
        logger.info(
            f"[OddsFetcher] /fixtures/live sportId={sport_id}: "
            f"{len(fixtures)} live fixtures"
        )
        return fixtures

    return []


def fetch_fixture_clv(fixture_id: str, bookmakers: str = "") -> Optional[dict]:
    """
    获取赛事的 CLV（Closing Line Value）数据。

    CLV 是验证投注边际的金标准：如果买入价 beat 收盘线 → 正期望。

    Args:
        fixture_id: OddsPapi fixture ID（如 "id1100013262924543"）
        bookmakers: 博彩公司过滤（逗号分隔，如 "pinnacle,bet365"），
                    空字符串使用 config clv_bookmakers 默认值

    Returns:
        CLV 响应 dict: {fixtureId, status, sport, odds: {bookmaker: {oddsId: {olv, clv}}}}
        None = 无数据/请求失败
    """
    if not fixture_id:
        return None

    cache_ttl = _get_cache_ttl("clv_cache_ttl_seconds", 86400)
    now = time.time()

    cached = _clv_cache.get(fixture_id)
    if cached and now - cached[0] < cache_ttl:
        return cached[1]

    if not bookmakers:
        bookmakers = str(config_get("odds_fetcher.clv_bookmakers") or "pinnacle,bet365")

    params = {"fixtureId": fixture_id, "bookmakers": bookmakers}
    data = _rapidapi_call("/fixtures/odds/clv", params=params, cache_ttl=cache_ttl)
    if data is not None and isinstance(data, dict):
        _clv_cache[fixture_id] = (now, data)
        odds = data.get("odds") or {}
        bm_count = len(odds)
        logger.info(f"[OddsFetcher] CLV {fixture_id}: {bm_count} bookmakers")
        return data

    return None


def fetch_fixture_settlement(fixture_id: str) -> Optional[dict]:
    """
    获取赛事结算数据（/fixtures/settlement）。

    用于独立验证比赛结果，不依赖 Polymarket 结算。

    Args:
        fixture_id: OddsPapi fixture ID

    Returns:
        Settlement 响应 dict: {fixtureId, settlements: [{marketId, status, margin, ...}]}
        None = 无数据/请求失败
    """
    if not fixture_id:
        return None

    cache_ttl = _get_cache_ttl("settlement_cache_ttl_seconds", 86400)
    now = time.time()

    cached = _settlement_cache.get(fixture_id)
    if cached and now - cached[0] < cache_ttl:
        return cached[1]

    params = {"fixtureId": fixture_id}
    data = _rapidapi_call("/fixtures/settlement", params=params, cache_ttl=cache_ttl)
    if data is not None and isinstance(data, dict):
        _settlement_cache[fixture_id] = (now, data)
        settlements = data.get("settlements") or []
        logger.info(
            f"[OddsFetcher] Settlement {fixture_id}: " f"{len(settlements)} outcomes"
        )
        return data

    return None


def fetch_sports_metadata() -> list[dict]:
    """
    获取 API 支持的所有运动列表（/sports）。

    24h 缓存，每天只调一次。

    Returns:
        Sports 列表: [{"sportId": 10, "slug": "soccer", "sportName": "Soccer"}, ...]
    """
    cache_ttl = _get_cache_ttl("metadata_cache_ttl_seconds", 86400)
    now = time.time()

    cached = _metadata_cache.get("sports")
    if cached and now - cached[0] < cache_ttl:
        return cached[1]

    data = _rapidapi_call("/sports", cache_ttl=cache_ttl)
    if data is not None:
        sports = data if isinstance(data, list) else []
        _metadata_cache["sports"] = (now, sports)
        logger.info(f"[OddsFetcher] /sports: {len(sports)} sports loaded")
        return sports

    return []


def fetch_tournaments(sport_id: int) -> list[dict]:
    """
    获取指定运动的所有锦标赛列表（/tournaments）。

    24h 缓存，每天每运动只调一次。

    Args:
        sport_id: 运动ID（10=Soccer, 11=Basketball, 14=AmFootball, 12=Tennis）

    Returns:
        Tournaments 列表: [{"tournamentId": 132, "sportId": 11,
                            "tournamentName": "NBA", "categoryName": "USA"}, ...]
    """
    cache_ttl = _get_cache_ttl("metadata_cache_ttl_seconds", 86400)
    now = time.time()

    cache_key = f"tournaments_{sport_id}"
    cached = _metadata_cache.get(cache_key)
    if cached and now - cached[0] < cache_ttl:
        return cached[1]

    params = {"sportId": str(sport_id)}
    data = _rapidapi_call("/tournaments", params=params, cache_ttl=cache_ttl)
    if data is not None:
        tournaments = data if isinstance(data, list) else []
        _metadata_cache[cache_key] = (now, tournaments)
        logger.info(
            f"[OddsFetcher] /tournaments sportId={sport_id}: "
            f"{len(tournaments)} loaded"
        )
        return tournaments

    return []


def get_api_usage_stats() -> dict:
    """
    获取当日 OddsPapi API 使用统计（纯内存读取，不消耗配额）。

    Returns:
        {"date": "2026-03-25", "total_calls": 42,
         "max_daily": 300, "remaining": 258,
         "by_endpoint": {"/fixtures/odds/main": 10, "/fixtures/today": 4, ...}}
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _daily_calls.get("date") != today:
        return {
            "date": today,
            "total_calls": 0,
            "max_daily": _get_max_daily_calls(),
            "remaining": _get_max_daily_calls(),
            "by_endpoint": {},
        }

    total = int(_daily_calls.get("total", 0))
    max_daily = _get_max_daily_calls()
    by_ep = _daily_calls.get("by_endpoint", {})
    return {
        "date": str(_daily_calls["date"]),
        "total_calls": total,
        "max_daily": max_daily,
        "remaining": max(0, max_daily - total),
        "by_endpoint": dict(by_ep) if isinstance(by_ep, dict) else {},
    }


def analyze_clv_edge(clv_data: dict) -> Optional[dict]:
    """
    从 CLV 数据中分析开收赔价差，计算系统边际。

    Args:
        clv_data: fetch_fixture_clv() 返回的原始数据

    Returns:
        {"fixture_id": str, "bookmaker": str,
         "outcomes": [{"outcome_id": int, "olv_price": float, "clv_price": float,
                       "line_moved_pct": float}],
         "avg_line_movement_pct": float}
        None = 无有效数据
    """
    if not clv_data or not isinstance(clv_data, dict):
        return None

    fixture_id = clv_data.get("fixtureId", "")
    odds = clv_data.get("odds") or {}
    if not odds:
        return None

    # 取第一个有数据的 bookmaker
    for bm_name, bm_odds in odds.items():
        if not isinstance(bm_odds, dict):
            continue

        outcomes = []
        for odds_id, clv_pair in bm_odds.items():
            if not isinstance(clv_pair, dict):
                continue
            olv = clv_pair.get("olv") or {}
            clv = clv_pair.get("clv") or {}
            olv_price = olv.get("price", 0)
            clv_price = clv.get("price", 0)
            outcome_id = olv.get("outcomeId") or clv.get("outcomeId", 0)

            if olv_price > 0 and clv_price > 0:
                movement = (clv_price - olv_price) / olv_price * 100
                outcomes.append(
                    {
                        "outcome_id": outcome_id,
                        "olv_price": olv_price,
                        "clv_price": clv_price,
                        "line_moved_pct": round(movement, 2),
                    }
                )

        if outcomes:
            avg_movement = sum(o["line_moved_pct"] for o in outcomes) / len(outcomes)
            return {
                "fixture_id": fixture_id,
                "bookmaker": bm_name,
                "outcomes": outcomes,
                "avg_line_movement_pct": round(avg_movement, 2),
            }

    return None


def clear_all_caches() -> None:
    """清空所有 OddsFetcher 缓存（供测试使用）。"""
    _cache.clear()
    _today_cache.clear()
    _live_cache.clear()
    _clv_cache.clear()
    _settlement_cache.clear()
    _metadata_cache.clear()


# ---------------------------------------------------------------------------
# T873 — 补齐 OddsPapi REST 余下端点（不含 WS / lineups / injuries / stats）
# ---------------------------------------------------------------------------


def fetch_bookmakers() -> list[dict]:
    """获取 OddsPapi 支持的全部博彩公司清单（含 logo / slug）。

    7 天落盘缓存 → 每周仅 1 次调用。
    """
    cache_ttl = _get_cache_ttl("metadata_cache_ttl_seconds", 86400)
    data = _rapidapi_call("/bookmakers", cache_ttl=cache_ttl)
    return data if isinstance(data, list) else []


def fetch_participants(tournament_id: int) -> list[dict]:
    """按 tournamentId 获取队伍/参赛者列表（含 logo / 国家）。"""
    if not tournament_id:
        return []
    cache_ttl = _get_cache_ttl("metadata_cache_ttl_seconds", 86400)
    params = {"tournamentId": str(tournament_id)}
    data = _rapidapi_call("/participants", params=params, cache_ttl=cache_ttl)
    return data if isinstance(data, list) else []


def fetch_markets(sport_id: int = 0) -> list[dict]:
    """获取 OddsPapi 支持的市场类型（moneyline / spread / total / ...）。"""
    cache_ttl = _get_cache_ttl("metadata_cache_ttl_seconds", 86400)
    params: dict[str, str] = {}
    if sport_id > 0:
        params["sportId"] = str(sport_id)
    data = _rapidapi_call("/markets", params=params or None, cache_ttl=cache_ttl)
    return data if isinstance(data, list) else []


def fetch_seasons(tournament_id: int) -> list[dict]:
    """获取某锦标赛的赛季列表。"""
    if not tournament_id:
        return []
    cache_ttl = _get_cache_ttl("metadata_cache_ttl_seconds", 86400)
    params = {"tournamentId": str(tournament_id)}
    data = _rapidapi_call("/seasons", params=params, cache_ttl=cache_ttl)
    return data if isinstance(data, list) else []


def fetch_currencies() -> list[dict]:
    """获取 OddsPapi 支持的货币代码（投注金额本币换算用）。"""
    cache_ttl = _get_cache_ttl("metadata_cache_ttl_seconds", 30 * 86400)
    data = _rapidapi_call("/currencies", cache_ttl=cache_ttl)
    return data if isinstance(data, list) else []


def fetch_venues(sport_id: int = 0) -> list[dict]:
    """获取场馆清单（NBA arena / 足球球场等）。"""
    cache_ttl = _get_cache_ttl("metadata_cache_ttl_seconds", 7 * 86400)
    params: dict[str, str] = {}
    if sport_id > 0:
        params["sportId"] = str(sport_id)
    data = _rapidapi_call("/venues", params=params or None, cache_ttl=cache_ttl)
    return data if isinstance(data, list) else []


def fetch_fixture_historical(
    fixture_id: str,
    bookmakers: str = "",
) -> Optional[dict]:
    """获取单场赛事的历史赔率轨迹（/fixtures/odds/historical）。

    返回结构含每个 bookmaker 的 line movement 时间序列；
    比 CLV 多了完整轨迹，主要用于深度复盘 / line-watching。
    """
    if not fixture_id:
        return None
    cache_ttl = _get_cache_ttl("clv_cache_ttl_seconds", 86400)
    if not bookmakers:
        bookmakers = str(config_get("odds_fetcher.clv_bookmakers") or "pinnacle,bet365")
    params = {"fixtureId": fixture_id, "bookmakers": bookmakers}
    data = _rapidapi_call(
        "/fixtures/odds/historical", params=params, cache_ttl=cache_ttl
    )
    return data if isinstance(data, dict) else None


def fetch_fixture_media(fixture_id: str) -> Optional[dict]:
    """获取单场赛事的媒体资源（/fixtures/media）：录像 / 直播链接 / 海报图。"""
    if not fixture_id:
        return None
    cache_ttl = _get_cache_ttl("metadata_cache_ttl_seconds", 86400)
    params = {"fixtureId": fixture_id}
    data = _rapidapi_call("/fixtures/media", params=params, cache_ttl=cache_ttl)
    return data if isinstance(data, dict) else None


def fetch_futures(sport_id: int = 0) -> list[dict]:
    """期货市场（赛季冠军 / 季后赛晋级等长周期赔率）的赛事清单。"""
    cache_ttl = _get_cache_ttl("metadata_cache_ttl_seconds", 6 * 3600)
    params: dict[str, str] = {}
    if sport_id > 0:
        params["sportId"] = str(sport_id)
    data = _rapidapi_call("/futures", params=params or None, cache_ttl=cache_ttl)
    return data if isinstance(data, list) else []


def fetch_futures_odds(future_id: str, bookmakers: str = "") -> Optional[dict]:
    """单个期货市场的赔率明细（/futures/odds）。"""
    if not future_id:
        return None
    cache_ttl = _get_cache_ttl("metadata_cache_ttl_seconds", 6 * 3600)
    if not bookmakers:
        bookmakers = str(config_get("odds_fetcher.clv_bookmakers") or "pinnacle,bet365")
    params = {"futureId": future_id, "bookmakers": bookmakers}
    data = _rapidapi_call("/futures/odds", params=params, cache_ttl=cache_ttl)
    return data if isinstance(data, dict) else None
