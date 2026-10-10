"""
sports_fetcher.py — 体育数据抓取工具

职责：
  - 从 Polymarket 体育类市场 question 中解析对阵双方
  - 调用 TheSportsDB 免费 API 获取：近期战绩、主客场、对战历史
  - 通过 RapidAPI api-football-v1 获取伤病名单（需 RAPIDAPI_KEY）
  - 构建 sports_context 注入 AI Prompt（类似天气的 weather_context）

用途：
  SignalEngine._analyze_market() 对 category=="sports" 调用本模块，
  使 AI 基于真实数据（而非纯推理）评估体育市场。

API 来源：
  1. TheSportsDB (https://www.thesportsdb.com/api/v1/json/3/)
     免费公开端点，无需 API key。
  2. API-Football v3 via RapidAPI (api-football-v1.p.rapidapi.com)
     伤病名单数据，需 RAPIDAPI_KEY。
"""

import logging
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_API_BASE = "https://www.thesportsdb.com/api/v1/json/3"
_TIMEOUT = 10
_CACHE_TTL = 1800  # 30 分钟缓存

# Polymarket 常用短名 → TheSportsDB 搜索用全名
# 只收录 Polymarket 高频出现且短名搜索失败的队伍
_TEAM_ALIASES: dict[str, str] = {
    "76ers": "Philadelphia 76ers",
    "sixers": "Philadelphia 76ers",
    "trail blazers": "Portland Trail Blazers",
    "blazers": "Portland Trail Blazers",
    "celtics": "Boston Celtics",
    "lakers": "Los Angeles Lakers",
    "clippers": "LA Clippers",
    "knicks": "New York Knicks",
    "nets": "Brooklyn Nets",
    "warriors": "Golden State Warriors",
    "heat": "Miami Heat",
    "bulls": "Chicago Bulls",
    "cavs": "Cleveland Cavaliers",
    "cavaliers": "Cleveland Cavaliers",
    "mavs": "Dallas Mavericks",
    "mavericks": "Dallas Mavericks",
    "nuggets": "Denver Nuggets",
    "pistons": "Detroit Pistons",
    "rockets": "Houston Rockets",
    "pacers": "Indiana Pacers",
    "grizzlies": "Memphis Grizzlies",
    "bucks": "Milwaukee Bucks",
    "timberwolves": "Minnesota Timberwolves",
    "wolves": "Minnesota Timberwolves",
    "pelicans": "New Orleans Pelicans",
    "thunder": "Oklahoma City Thunder",
    "magic": "Orlando Magic",
    "raptors": "Toronto Raptors",
    "spurs": "San Antonio Spurs",
    "kings": "Sacramento Kings",
    "suns": "Phoenix Suns",
    "jazz": "Utah Jazz",
    "wizards": "Washington Wizards",
    "hawks": "Atlanta Hawks",
    "hornets": "Charlotte Hornets",
    # NHL
    "maple leafs": "Toronto Maple Leafs",
    "canadiens": "Montreal Canadiens",
    "habs": "Montreal Canadiens",
    "bruins": "Boston Bruins",
    "rangers": "New York Rangers",
    "islanders": "New York Islanders",
    "penguins": "Pittsburgh Penguins",
    "blackhawks": "Chicago Blackhawks",
    "red wings": "Detroit Red Wings",
    "avalanche": "Colorado Avalanche",
    "lightning": "Tampa Bay Lightning",
    "panthers": "Florida Panthers",
    "oilers": "Edmonton Oilers",
    "flames": "Calgary Flames",
    "canucks": "Vancouver Canucks",
    "kraken": "Seattle Kraken",
    "golden knights": "Vegas Golden Knights",
    "wild": "Minnesota Wild",
    "predators": "Nashville Predators",
    "blues": "St. Louis Blues",
    "stars": "Dallas Stars",
    "senators": "Ottawa Senators",
    "sabres": "Buffalo Sabres",
    "hurricanes": "Carolina Hurricanes",
    "jets": "Winnipeg Jets",
    "sharks": "San Jose Sharks",
    "coyotes": "Arizona Coyotes",
    "ducks": "Anaheim Ducks",
    "devils": "New Jersey Devils",
    "capitals": "Washington Capitals",
    "blue jackets": "Columbus Blue Jackets",
    # NFL
    "chiefs": "Kansas City Chiefs",
    "eagles": "Philadelphia Eagles",
    "bills": "Buffalo Bills",
    "cowboys": "Dallas Cowboys",
    "49ers": "San Francisco 49ers",
    "niners": "San Francisco 49ers",
    "steelers": "Pittsburgh Steelers",
    "ravens": "Baltimore Ravens",
    "dolphins": "Miami Dolphins",
    "bengals": "Cincinnati Bengals",
    "chargers": "Los Angeles Chargers",
    "broncos": "Denver Broncos",
    "raiders": "Las Vegas Raiders",
    "patriots": "New England Patriots",
    "seahawks": "Seattle Seahawks",
    "packers": "Green Bay Packers",
    "vikings": "Minnesota Vikings",
    "bears": "Chicago Bears",
    "lions": "Detroit Lions",
    "commanders": "Washington Commanders",
    "giants": "New York Giants",
    "falcons": "Atlanta Falcons",
    "saints": "New Orleans Saints",
    "panthers (nfl)": "Carolina Panthers",
    "buccaneers": "Tampa Bay Buccaneers",
    "bucs": "Tampa Bay Buccaneers",
    "jaguars": "Jacksonville Jaguars",
    "titans": "Tennessee Titans",
    "colts": "Indianapolis Colts",
    "texans": "Houston Texans",
    "cardinals": "Arizona Cardinals",
    "rams": "Los Angeles Rams",
    # MLB
    "yankees": "New York Yankees",
    "red sox": "Boston Red Sox",
    "dodgers": "Los Angeles Dodgers",
    "astros": "Houston Astros",
    "braves": "Atlanta Braves",
    "mets": "New York Mets",
    "cubs": "Chicago Cubs",
    "white sox": "Chicago White Sox",
    "padres": "San Diego Padres",
    "phillies": "Philadelphia Phillies",
    # Tennis (individual players keep as-is, no alias needed)
}

# 内存缓存：{team_name_lower: (timestamp, team_data)}
_team_cache: dict[str, tuple[float, dict]] = {}
# 内存缓存：{team_id: (timestamp, [events])}
_events_cache: dict[str, tuple[float, list]] = {}

# ---------------------------------------------------------------------------
# API-Football 每日调用计数器（与 odds_fetcher 保持一致的设计）
# ---------------------------------------------------------------------------
_api_football_daily: dict[str, object] = {"date": "", "total": 0}


def _check_api_football_daily_limit() -> bool:
    """检查 api-football-v1 是否超出每日调用限制。"""
    from datetime import datetime, timezone

    from src.utils.config_loader import get

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _api_football_daily["date"] != today:
        _api_football_daily["date"] = today
        _api_football_daily["total"] = 0
    max_calls = int(
        (get("sports_fetcher") or {}).get("api_football_max_daily_calls", 100)
    )
    return int(_api_football_daily["total"]) < max_calls


def _increment_api_football_daily() -> None:
    """递增 api-football-v1 每日调用计数。"""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _api_football_daily["date"] != today:
        _api_football_daily["date"] = today
        _api_football_daily["total"] = 0
    _api_football_daily["total"] = int(_api_football_daily["total"]) + 1


def get_api_football_daily_calls() -> int:
    """返回当日 api-football 已用调用次数（供外部监控）。"""
    return int(_api_football_daily.get("total", 0))


# API-Football ligaID 映射（ question 关键词 → league_id）
# 第一个匹配的 league_id 用于查询伤病
_FOOTBALL_LEAGUE_MAP: dict[str, int] = {
    "premier league": 39,
    "epl": 39,
    "la liga": 140,
    "laliga": 140,
    "bundesliga": 78,
    "serie a": 135,
    "ligue 1": 61,
    "ligue1": 61,
    "champions league": 2,
    "ucl": 2,
    "europa league": 3,
    "mls": 253,
}


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------


def fetch_sports_context(question: str) -> str:
    """
    从市场 question 中解析对阵双方，获取体育数据，构建 AI 上下文。

    包括 TheSportsDB 战绩数据 + API-Football 伤病名单（如有 RAPIDAPI_KEY）。

    Args:
        question: Polymarket 市场问题, e.g. "Trail Blazers vs. 76ers"

    Returns:
        格式化的体育数据上下文字符串（可能为空字符串如果获取失败）
    """
    teams = extract_teams(question)
    if not teams:
        logger.debug(f"[SportsFetcher] 无法从 question 中解析对阵: {question[:60]}")
        return ""

    team1_name, team2_name = teams
    team1_data = _get_team_data(team1_name)
    team2_data = _get_team_data(team2_name)

    if not team1_data and not team2_data:
        logger.info(f"[SportsFetcher] 两队数据均获取失败: {team1_name} vs {team2_name}")
        return ""

    ctx = _build_context(team1_name, team1_data, team2_name, team2_data)

    # 附加 API-Football 伤病名单（需 RAPIDAPI_KEY）
    injury_ctx = fetch_api_football_injury_context(question)
    if injury_ctx:
        ctx = ctx + "\n" + injury_ctx

    return ctx


def extract_teams(question: str) -> Optional[tuple[str, str]]:
    """
    从 Polymarket 市场 question 中解析对阵双方队名。

    支持格式：
      - "Trail Blazers vs. 76ers"
      - "Trail Blazers vs 76ers"
      - "Senators vs. Rangers: O/U 5.5"  → ("Senators", "Rangers")
      - "Will the Trail Blazers beat the 76ers?"
      - "Hawks vs. Hawks" (returns None if same team)
    """
    q = question.strip()
    # 先移除 O/U / spread 后缀，避免污染队名解析
    q = re.sub(r":\s*(?:O/U|Over/Under|Under/Over)\s+[\d.]+\s*$", "", q).strip()

    # 标准 "A vs. B" 或 "A vs B" 格式
    m = re.match(r"^(.+?)\s+vs\.?\s+(.+?)$", q, re.IGNORECASE)
    if m:
        t1, t2 = m.group(1).strip(), m.group(2).strip()
        if t1.lower() != t2.lower():
            return (t1, t2)

    # "Will X beat Y" / "Will X win against Y"
    m = re.search(
        r"will\s+(?:the\s+)?(.+?)\s+(?:beat|win against|defeat)\s+(?:the\s+)?(.+?)[\?\.]",
        q,
        re.IGNORECASE,
    )
    if m:
        t1, t2 = m.group(1).strip(), m.group(2).strip()
        if t1.lower() != t2.lower():
            return (t1, t2)

    return None


# ---------------------------------------------------------------------------
# 内部方法
# ---------------------------------------------------------------------------


def _resolve_team_name(short_name: str) -> str:
    """将 Polymarket 短名映射为 TheSportsDB 搜索用全名。"""
    key = short_name.strip().lower()
    return _TEAM_ALIASES.get(key, short_name)


def _get_team_data(team_name: str) -> Optional[dict]:
    """
    获取一个队伍的完整数据（团队信息 + 最近比赛）。
    带缓存避免重复请求。

    Returns:
        {
            "team_info": {...},    # TheSportsDB team object
            "last_events": [...],  # 最近5场比赛
        }
        或 None（获取失败）
    """
    resolved_name = _resolve_team_name(team_name)
    cache_key = resolved_name.lower()

    # 检查缓存
    if cache_key in _team_cache:
        ts, data = _team_cache[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return data

    # 搜索队伍
    team_info = _search_team(resolved_name)
    if not team_info:
        # 短名直接搜索失败，尝试拼接城市名（Polymarket 有时只用城市名如 "Philadelphia"）
        logger.debug(f"[SportsFetcher] 搜索失败: '{resolved_name}'，跳过")
        _team_cache[cache_key] = (time.time(), None)
        return None

    team_id = team_info.get("idTeam", "")
    last_events = _get_last_events(team_id) if team_id else []

    data = {
        "team_info": team_info,
        "last_events": last_events,
    }
    _team_cache[cache_key] = (time.time(), data)
    return data


def _search_team(name: str) -> Optional[dict]:
    """TheSportsDB 搜索球队, 返回第一个匹配结果。"""
    try:
        resp = requests.get(
            f"{_API_BASE}/searchteams.php",
            params={"t": name},
            timeout=_TIMEOUT,
        )
        if resp.ok:
            teams = resp.json().get("teams") or []
            if teams:
                return teams[0]
    except Exception as e:
        logger.warning(f"[SportsFetcher] API 搜索异常 '{name}': {e}")
    return None


def _get_last_events(team_id: str) -> list[dict]:
    """获取球队最近5场比赛结果。"""
    # 检查缓存
    if team_id in _events_cache:
        ts, events = _events_cache[team_id]
        if time.time() - ts < _CACHE_TTL:
            return events

    try:
        resp = requests.get(
            f"{_API_BASE}/eventslast.php",
            params={"id": team_id},
            timeout=_TIMEOUT,
        )
        if resp.ok:
            events = resp.json().get("results") or []
            _events_cache[team_id] = (time.time(), events)
            return events
    except Exception as e:
        logger.warning(f"[SportsFetcher] 获取比赛历史异常 team={team_id}: {e}")

    return []


def _format_record(events: list[dict], team_name: str) -> str:
    """
    将最近比赛格式化为 W/L 序列 + 胜率。

    例: "WWLWL (3W-2L, 60%)"
    """
    if not events:
        return "无数据"

    results = []
    for ev in events[:5]:
        home = ev.get("strHomeTeam", "")
        away = ev.get("strAwayTeam", "")
        home_score = _safe_int(ev.get("intHomeScore"))
        away_score = _safe_int(ev.get("intAwayScore"))

        if home_score is None or away_score is None:
            continue

        is_home = team_name.lower() in home.lower()
        if is_home:
            won = home_score > away_score
        else:
            won = away_score > home_score

        results.append("W" if won else "L")

    if not results:
        return "无数据"

    wins = results.count("W")
    total = len(results)
    seq = "".join(results)
    pct = round(wins / total * 100)
    return f"{seq} ({wins}W-{total - wins}L, {pct}%)"


def _format_recent_games(events: list[dict], max_games: int = 5) -> str:
    """将最近比赛格式化为逐场描述。"""
    lines = []
    for ev in events[:max_games]:
        date = ev.get("dateEvent", "?")
        home = ev.get("strHomeTeam", "?")
        away = ev.get("strAwayTeam", "?")
        hs = ev.get("intHomeScore", "?")
        aws = ev.get("intAwayScore", "?")
        venue = "🏠" if True else ""  # 简化
        lines.append(f"  {date}: {home} {hs}-{aws} {away}")
    return "\n".join(lines) if lines else "  无数据"


def _build_context(
    team1_name: str,
    team1_data: Optional[dict],
    team2_name: str,
    team2_data: Optional[dict],
) -> str:
    """构建完整的 sports_context 字符串，注入 AI prompt。"""
    lines = ["【体育数据分析（来源: TheSportsDB）】"]

    for name, data in [(team1_name, team1_data), (team2_name, team2_data)]:
        if not data:
            lines.append(f"\n★ {name}: 数据获取失败")
            continue

        info = data["team_info"]
        events = data["last_events"]

        team_full = info.get("strTeam", name)
        league = info.get("strLeague", "?")
        sport = info.get("strSport", "?")
        stadium = info.get("strStadium", "")

        record_str = _format_record(events, team_full)
        n_games = min(len(events), 5)

        lines.append(f"\n★ {team_full} ({league})")
        if n_games < 3:
            lines.append(
                f"  近期战绩({n_games}场可查): {record_str}"
                f"  ⚠️ 样本极少，数据参考价值有限"
            )
        else:
            lines.append(f"  近{n_games}场: {record_str}")

        # 最近比赛详情
        if events:
            lines.append("  最近比赛:")
            lines.append(_format_recent_games(events, 3))

    # 查找对战历史（从两队的最近比赛中查找对方）
    h2h = _find_h2h(team1_data, team2_data)
    if h2h:
        lines.append(f"\n★ 对战历史(本赛季): {h2h}")

    lines.append(
        "\n分析提示: 基于以上数据评估双方实力对比和胜率，结合近期状态做出判断。"
    )
    return "\n".join(lines)


def _find_h2h(team1_data: Optional[dict], team2_data: Optional[dict]) -> Optional[str]:
    """从两队的最近比赛中查找对战记录。"""
    if not team1_data or not team2_data:
        return None

    info1 = team1_data["team_info"]
    info2 = team2_data["team_info"]
    name1 = info1.get("strTeam", "").lower()
    name2 = info2.get("strTeam", "").lower()

    if not name1 or not name2:
        return None

    h2h_results = []
    for ev in team1_data.get("last_events", []):
        home = (ev.get("strHomeTeam") or "").lower()
        away = (ev.get("strAwayTeam") or "").lower()
        if name2 in home or name2 in away:
            hs = ev.get("intHomeScore", "?")
            aws = ev.get("intAwayScore", "?")
            date = ev.get("dateEvent", "?")
            h2h_results.append(
                f"{ev.get('strHomeTeam', '?')} {hs}-{aws} {ev.get('strAwayTeam', '?')} ({date})"
            )

    if h2h_results:
        return "; ".join(h2h_results[:3])
    return None


def _safe_int(val) -> Optional[int]:
    """安全转换为 int，无效值返回 None。"""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# API-Football 伤病名单（通过 RapidAPI）
# ---------------------------------------------------------------------------


def fetch_api_football_injury_context(question: str) -> str:
    """
    通过 RapidAPI api-football-v1 获取即将比赛的伤病名单。

    流程:
      1. 解析 question 中的赛事联赛
      2. /v3/fixtures 获取未来 7 天内赛程
      3. 匹配队名，找到对应 fixture_id
      4. /v3/injuries 获取伤病情况
      5. 格式化为 伤病上下文字符串

    需要 RAPIDAPI_KEY；无 key 或请求失败时返回空字符串。
    """
    import os

    from src.utils.config_loader import get
    from src.utils.rapidapi_client import fetch as rapidapi_fetch

    # T235: api-football 开关（403 时无需反复请求浪费日志）
    if not (get("sports_fetcher") or {}).get("api_football_enabled", True):
        return ""

    if not os.environ.get("RAPIDAPI_KEY", "").strip():
        return ""

    # T186: api-football 每日调用限额检查
    if not _check_api_football_daily_limit():
        logger.warning(
            "[InjuryFetcher] api-football 每日调用限额已达上限，跳过伤病查询"
        )
        return ""

    teams = extract_teams(question)
    if not teams:
        return ""

    team1_name, team2_name = teams

    # 识别联赛
    league_id = _detect_football_league(question)
    if not league_id:
        logger.debug(f"[InjuryFetcher] 无法识别足球联赛: {question[:60]}")
        return ""

    season = int(get("sports_fetcher.api_football_season") or 2025)

    # Step 1: 获取即将赛程
    from datetime import datetime, timedelta, timezone

    now_utc = datetime.now(timezone.utc)
    date_from = now_utc.strftime("%Y-%m-%d")
    date_to = (now_utc + timedelta(days=7)).strftime("%Y-%m-%d")

    fixtures_data = rapidapi_fetch(
        "football",
        "/v3/fixtures",
        params={
            "league": league_id,
            "season": season,
            "from": date_from,
            "to": date_to,
        },
    )
    if not fixtures_data:
        return ""
    _increment_api_football_daily()

    fixtures = (
        (fixtures_data.get("response") or []) if isinstance(fixtures_data, dict) else []
    )
    if not fixtures:
        return ""

    # Step 2: 匹配队名找 fixture_id
    fixture_id = _match_fixture(fixtures, team1_name, team2_name)
    if not fixture_id:
        logger.debug(f"[InjuryFetcher] 未匹配到 fixture: {team1_name} vs {team2_name}")
        return ""

    # Step 3: 获取伤病
    injury_data = rapidapi_fetch(
        "football",
        "/v3/injuries",
        params={"fixture": fixture_id},
    )
    if not injury_data:
        return ""
    _increment_api_football_daily()

    injuries = (
        (injury_data.get("response") or []) if isinstance(injury_data, dict) else []
    )
    return _format_injuries(injuries, team1_name, team2_name)


def _detect_football_league(question: str) -> Optional[int]:
    """从 question 中识别足球联赛 ID。"""
    q_lower = question.lower()
    for keyword, league_id in _FOOTBALL_LEAGUE_MAP.items():
        if keyword in q_lower:
            return league_id
    return None


def _match_fixture(fixtures: list[dict], team1: str, team2: str) -> Optional[int]:
    """从 fixtures 列表中匹配包含两队的比赛，返回 fixture_id。"""
    t1 = team1.lower()
    t2 = team2.lower()
    for fix in fixtures:
        teams_info = fix.get("teams") or {}
        home = (teams_info.get("home") or {}).get("name", "").lower()
        away = (teams_info.get("away") or {}).get("name", "").lower()
        combined = f"{home} {away}"
        # 允许部分匹配（队名可能有做差）
        t1_match = any(word in combined for word in t1.split() if len(word) > 3)
        t2_match = any(word in combined for word in t2.split() if len(word) > 3)
        if t1_match and t2_match:
            return fix.get("fixture", {}).get("id")
    return None


def _format_injuries(injuries: list[dict], team1: str, team2: str) -> str:
    """将伤病数据格式化为 AI 上下文字符串。"""
    if not injuries:
        return ""

    lines = ["\n《伤病名单 (API-Football via RapidAPI)》"]
    team_injuries: dict[str, list[str]] = {}
    for item in injuries:
        player = (item.get("player") or {}).get("name", "?")
        inj_type = (item.get("player") or {}).get("type", "?")
        inj_reason = (item.get("player") or {}).get("reason", "")
        team_name = (item.get("team") or {}).get("name", "?")
        entry = f"  {player} ({inj_type}{': ' + inj_reason if inj_reason else ''})"
        team_injuries.setdefault(team_name, []).append(entry)

    for team_name, entries in team_injuries.items():
        lines.append(f"\u25cf {team_name} ({len(entries)}人伤病/恋奴):")
        lines.extend(entries[:8])  # 最多列 8 人

    if len(lines) <= 1:
        return ""
    return "\n".join(lines)
