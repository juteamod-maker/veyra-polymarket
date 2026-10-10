"""
dashboard_routes/sportslive_routes.py — VeYra 体育赔率

[内部说明 — 不输出给前端]
上游赔率原始数据来自 RapidAPI · odds-api1.p.rapidapi.com；预测概率参考来自
Polymarket gamma-api。两者在用户面前一律包装为 "VeYra 体育赔率网络"，
业务核心目标：把流量留在 /sportslive → /event/<slug>，让用户在我方下单，
通过 ColB referral 归属到我方账户赚取 PM 佣金。

OddsPapi v5 真实端点（RapidAPI 网关已剥离 /{lang}/ 前缀，直接调相对路径）:
  GET /sports
       → 全部体育 [{sportId, slug, sportName}]
  GET /fixtures?sportId=N&future=true
       → 仅 metadata，bookmakers={} 永远空（用作 fixtureId 列表）
  GET /fixtures/today?sportId=N
       → 今日赛程（轻量 metadata 列表）
  GET /fixtures/live?sportId=N
       → 进行中赛程
  GET /fixtures/odds/main?fixtureIds=a,b,c
       → 多场主赔率: items[*].odds = {bookmakerSlug:{quoteId:{outcomeId,price,marketId,...}}}
  GET /fixtures/odds?fixtureId=X
       → 单场全部博彩（含未 main line 的盘口）

实现策略（T557 W2）:
  step1: 调 /fixtures?sportId=N&future=true 拿 fixture metadata（cache 30 min，
         一次 ~2MB 但被 30 分钟内存缓存兜住，PRO 配额 10K/月足够）
  step2: 过滤掉 Finished/Cancelled/Postponed/Abandoned，按"进行中优先 + 开赛时间"排序，
         取前 limit 个 fixtureId
  step3: 调 /fixtures/odds/main?fixtureIds=<csv> 拿这些场的多博彩赔率
  step4: 合并 metadata + odds → consensus 隐含概率（IQR 过滤后中位数）

固定不变的设计原则:
  - 永远不返回伪造数据；上游失败时返回 {items:[], error:reason} 让前端显示具体原因
  - 不把任何中文 / 英文 hardcode 进 JSON（前端走 i18n）
  - 所有外部请求经 rapidapi_client.fetch_with_meta，自带 30min cache + 4xx/5xx/超时分类

端点:
  - GET /api/sportslive/sports
       → {items:[{key,label_zh,label_en,group}], rapidapi: bool}
  - GET /api/sportslive/events?sport=KEY&limit=24&window_hours=72
       → {sport, items:[normalized fixtures], count, has_odds, fetched_total}
"""

from __future__ import annotations

import statistics
import threading
import time
import json
import os as _os
import re as _re
import unicodedata as _ud
import requests as _requests
from threading import Lock as _Lock
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from scripts.dashboard_routes._shared import logger
from src.utils import rapidapi_client

router = APIRouter()


# ── 受支持的体育白名单 ─────────────────────────────────────────
# 上游 sportId 通过线上 GET /sports 探测确认。
# T558-E：电竞类（sportId 16/17/18）历史曾被排除，T582 起加入支持，
#   但拆分到独立 /esports-live 页（按 group=='esports' 过滤）。
_EXCLUDED_SPORT_IDS: set[int] = set()  # 历史字段，已不再使用
_SUPPORTED_SPORTS: list[dict] = [
    {
        "key": "soccer",
        "sportId": 10,
        "label_zh": "足球",
        "label_en": "Soccer",
        "group": "soccer",
    },
    {
        "key": "basketball",
        "sportId": 11,
        "label_zh": "篮球",
        "label_en": "Basketball",
        "group": "basketball",
    },
    {
        "key": "tennis",
        "sportId": 12,
        "label_zh": "网球",
        "label_en": "Tennis",
        "group": "tennis",
    },
    {
        "key": "baseball",
        "sportId": 13,
        "label_zh": "棒球",
        "label_en": "Baseball",
        "group": "baseball",
    },
    {
        "key": "american-football",
        "sportId": 14,
        "label_zh": "美式足球",
        "label_en": "American Football",
        "group": "football_us",
    },
    {
        "key": "ice-hockey",
        "sportId": 15,
        "label_zh": "冰球",
        "label_en": "Ice Hockey",
        "group": "hockey",
    },
    {
        "key": "mma",
        "sportId": 20,
        "label_zh": "MMA",
        "label_en": "MMA",
        "group": "combat",
    },
    {
        "key": "boxing",
        "sportId": 21,
        "label_zh": "拳击",
        "label_en": "Boxing",
        "group": "combat",
    },
    # T582 — Esports（独立 /esports-live 页面消费；group=='esports' 用于按页过滤）
    {
        "key": "esport-counter-strike",
        "sportId": 17,
        "label_zh": "反恐精英 (CS2)",
        "label_en": "Counter-Strike (CS2)",
        "group": "esports",
    },
    {
        "key": "esport-league-of-legends",
        "sportId": 18,
        "label_zh": "英雄联盟 (LoL)",
        "label_en": "League of Legends",
        "group": "esports",
    },
    {
        "key": "esport-dota",
        "sportId": 16,
        "label_zh": "刀塔 (Dota 2)",
        "label_en": "Dota 2",
        "group": "esports",
    },
    # T592 — 扩展电竞全量覆盖（OddsPapi /sports 探测确认的实时 sportId）
    {
        "key": "esoccer",
        "sportId": 54,
        "label_zh": "电子足球",
        "label_en": "eSoccer",
        "group": "esports",
    },
    {
        "key": "ebasketball",
        "sportId": 55,
        "label_zh": "电子篮球",
        "label_en": "eBasketball",
        "group": "esports",
    },
    {
        "key": "esport-call-of-duty",
        "sportId": 56,
        "label_zh": "使命召唤",
        "label_en": "Call of Duty",
        "group": "esports",
    },
    {
        "key": "esport-overwatch",
        "sportId": 57,
        "label_zh": "守望先锋",
        "label_en": "Overwatch",
        "group": "esports",
    },
    {
        "key": "esport-rainbow-six",
        "sportId": 58,
        "label_zh": "彩虹六号",
        "label_en": "Rainbow Six",
        "group": "esports",
    },
    {
        "key": "esport-rocket-league",
        "sportId": 59,
        "label_zh": "火箭联盟",
        "label_en": "Rocket League",
        "group": "esports",
    },
    {
        "key": "esport-starcraft",
        "sportId": 60,
        "label_zh": "星际争霸",
        "label_en": "StarCraft",
        "group": "esports",
    },
    {
        "key": "esport-valorant",
        "sportId": 61,
        "label_zh": "无畏契约",
        "label_en": "Valorant",
        "group": "esports",
    },
    {
        "key": "esport-arena-of-valor",
        "sportId": 62,
        "label_zh": "传说对决",
        "label_en": "Arena of Valor",
        "group": "esports",
    },
    {
        "key": "esport-king-of-glory",
        "sportId": 63,
        "label_zh": "王者荣耀",
        "label_en": "King of Glory",
        "group": "esports",
    },
    {
        "key": "esport-honor-of-kings",
        "sportId": 65,
        "label_zh": "Honor of Kings",
        "label_en": "Honor of Kings",
        "group": "esports",
    },
]


def _sport_by_key(key: str) -> Optional[dict]:
    for s in _SUPPORTED_SPORTS:
        if s["key"] == key:
            return s
    return None


# ── consensus 计算 ────────────────────────────────────────────
def _decimal_to_implied(odds: float) -> float:
    if not odds or odds <= 1.0:
        return 0.0
    return 1.0 / float(odds)


def _consensus_prob(prices: list[float]) -> float:
    """多家博彩同方向价格 → IQR 过滤后取中位数 → 隐含概率（含 vig）。"""
    cleaned = [p for p in prices if p and p > 1.0]
    if not cleaned:
        return 0.0
    if len(cleaned) >= 4:
        try:
            q = statistics.quantiles(cleaned, n=4)
            iqr = q[2] - q[0]
            lo = q[0] - 1.5 * iqr
            hi = q[2] + 1.5 * iqr
            cleaned = [p for p in cleaned if lo <= p <= hi] or cleaned
        except statistics.StatisticsError:
            pass
    return _decimal_to_implied(statistics.median(cleaned))


# ── OddsPapi 响应规整 ─────────────────────────────────────────
# /fixtures/odds/main 返回单条结构（实测）:
#   {
#     fixtureId, status:{live, statusName},
#     sport:{sportId, sportName},
#     tournament:{tournamentId, tournamentName, categoryName},
#     startTime (epoch sec),
#     participants:{participant1Id/Name/Abbr, participant2Id/Name/Abbr, ...},
#     scores:{...}, externalProviders:{polymarketId, kalshiId, ...},
#     odds:{
#       <bookmakerSlug>: {
#         "<fixtureId>:<bk>:<outcomeId>:<playerId>": {
#           outcomeId, playerId, price, marketId, mainLine, marketActive, active, ...
#         }, ...
#       }, ...
#     },
#     bookmakers:{ <slug>:{hasOdds, staleOdds, suspended, ...} }
#   }
# 我们目前只关心 winner/moneyline 主市场（marketId 通常 == 111 / 1 / 1x2 类）。
# 取每家 bookmaker 中 mainLine=true 且 active=true 的报价，按 outcomeId 聚合。


def _empty_consensus() -> dict:
    return {
        "books_count": 0,
        "p1_implied": 0.0,
        "p2_implied": 0.0,
        "draw_implied": 0.0,
        "p1_max": 0.0,
        "p2_max": 0.0,
        "draw_max": 0.0,
        "market_id": None,
        "arb_margin": 0.0,
        "top_bookmakers": [],
    }


def _extract_consensus(odds_dict: dict) -> dict:
    """
    从 OddsPapi odds 块抽取 winner consensus + per-bookmaker 价格 + 套利缝隙。
    返回 {
        books_count, market_id,
        p1_implied, p2_implied, draw_implied,
        p1_max, p2_max, draw_max,                  # 每方向最高 decimal（用于 EV 计算）
        arb_margin,                                # max(0, 1 - sum(implied))；>0 即套利
        top_bookmakers: [{slug, p1, p2, draw}]     # 最多 10 家（按覆盖 outcome 数 desc）
    }
    """
    if not isinstance(odds_dict, dict) or not odds_dict:
        return _empty_consensus()

    # 第一步：找出每家公司的"主 winner 市场" → 用出现次数最多的 marketId 作为该 fixture 的主市场
    market_id_counts: dict[int, int] = {}
    for bk_slug, quotes in odds_dict.items():
        if not isinstance(quotes, dict):
            continue
        # 收集本家公司里 mainLine=true 的 marketId
        seen_for_book: set[int] = set()
        for q in quotes.values():
            if not isinstance(q, dict):
                continue
            if q.get("mainLine") is False:
                continue
            if q.get("active") is False:
                continue
            mid = q.get("marketId")
            if isinstance(mid, int):
                seen_for_book.add(mid)
        for mid in seen_for_book:
            market_id_counts[mid] = market_id_counts.get(mid, 0) + 1

    if not market_id_counts:
        return _empty_consensus()

    # 取出现次数最多、且 outcome 数量在 2~3 的 marketId
    sorted_mids = sorted(market_id_counts.items(), key=lambda x: -x[1])
    chosen_mid = sorted_mids[0][0]

    # 第二步：按 outcomeId 收集价格 + 同步收集每家 bookmaker 的 outcome→price 表
    by_outcome: dict[int, list[float]] = {}
    books_used: set[str] = set()
    book_prices: dict[str, dict[int, float]] = {}  # {slug: {outcomeId: best_price}}
    for bk_slug, quotes in odds_dict.items():
        if not isinstance(quotes, dict):
            continue
        for q in quotes.values():
            if not isinstance(q, dict):
                continue
            if q.get("marketId") != chosen_mid:
                continue
            if q.get("mainLine") is False or q.get("active") is False:
                continue
            try:
                price = float(q.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price <= 1.0:
                continue
            outcome_id = q.get("outcomeId")
            if not isinstance(outcome_id, int):
                continue
            by_outcome.setdefault(outcome_id, []).append(price)
            books_used.add(bk_slug)
            slot = book_prices.setdefault(bk_slug, {})
            # 同一 bookmaker 多次报价取最高（对用户最有利）
            if price > slot.get(outcome_id, 0.0):
                slot[outcome_id] = price

    if not by_outcome:
        out = _empty_consensus()
        out["market_id"] = chosen_mid
        return out

    # 第三步：把 outcomeId 排序后映射到 p1 / draw / p2
    sorted_oids = sorted(by_outcome.keys())
    consensus = _empty_consensus()
    consensus["books_count"] = len(books_used)
    consensus["market_id"] = chosen_mid

    def _max(prices: list[float]) -> float:
        return round(max(prices), 4) if prices else 0.0

    if len(sorted_oids) == 2:
        oid_p1, oid_p2 = sorted_oids
        consensus["p1_implied"] = round(_consensus_prob(by_outcome[oid_p1]), 4)
        consensus["p2_implied"] = round(_consensus_prob(by_outcome[oid_p2]), 4)
        consensus["p1_max"] = _max(by_outcome[oid_p1])
        consensus["p2_max"] = _max(by_outcome[oid_p2])
    elif len(sorted_oids) == 3:
        oid_p1, oid_draw, oid_p2 = sorted_oids
        consensus["p1_implied"] = round(_consensus_prob(by_outcome[oid_p1]), 4)
        consensus["draw_implied"] = round(_consensus_prob(by_outcome[oid_draw]), 4)
        consensus["p2_implied"] = round(_consensus_prob(by_outcome[oid_p2]), 4)
        consensus["p1_max"] = _max(by_outcome[oid_p1])
        consensus["draw_max"] = _max(by_outcome[oid_draw])
        consensus["p2_max"] = _max(by_outcome[oid_p2])
    else:
        oid_p1 = sorted_oids[0]
        oid_p2 = sorted_oids[-1]

    # 套利缝隙：sum(隐含概率) < 1 → 100% 包仓可锁正收益
    s_imp = (
        consensus["p1_implied"] + consensus["p2_implied"] + consensus["draw_implied"]
    )
    consensus["arb_margin"] = round(max(0.0, 1.0 - s_imp), 4) if s_imp > 0 else 0.0

    # 构建 top_bookmakers（按覆盖的 outcome 数 desc，再按 #q 总数 desc）
    if len(sorted_oids) >= 2:
        oid_first = sorted_oids[0]
        oid_last = sorted_oids[-1]
        oid_mid = sorted_oids[1] if len(sorted_oids) == 3 else None
        ranked = sorted(
            book_prices.items(),
            key=lambda kv: (-len(kv[1]), -sum(1 for _ in kv[1].values())),
        )
        top: list[dict] = []
        for slug, prices in ranked[:10]:
            top.append(
                {
                    "slug": slug,
                    "p1": round(prices.get(oid_first, 0.0), 4),
                    "p2": round(prices.get(oid_last, 0.0), 4),
                    "draw": (
                        round(prices.get(oid_mid, 0.0), 4)
                        if oid_mid is not None
                        else 0.0
                    ),
                }
            )
        consensus["top_bookmakers"] = top
    return consensus


def _normalize_meta(raw: dict) -> Optional[dict]:
    """OddsPapi /fixtures 单条 → 前端友好 metadata（不含赔率）。"""
    try:
        fid = raw.get("fixtureId") or ""
        if not fid:
            return None
        parts = raw.get("participants") or {}
        p1_name = (
            parts.get("participant1Name") or parts.get("participant1ShortName") or ""
        )
        p2_name = (
            parts.get("participant2Name") or parts.get("participant2ShortName") or ""
        )
        if not (p1_name and p2_name):
            return None
        st = raw.get("status") or {}
        scores = (raw.get("scores") or {}).get("result") or {}
        external = raw.get("externalProviders") or {}
        return {
            "id": fid,
            "status": st.get("statusName") or "",
            "is_live": bool(st.get("live")),
            "sport_id": (raw.get("sport") or {}).get("sportId"),
            "sport_name": (raw.get("sport") or {}).get("sportName") or "",
            "tournament": (raw.get("tournament") or {}).get("tournamentName") or "",
            "category": (raw.get("tournament") or {}).get("categoryName") or "",
            "start_time": int(raw.get("startTime") or 0),  # epoch seconds
            "p1": {"name": p1_name, "abbr": parts.get("participant1Abbr") or ""},
            "p2": {"name": p2_name, "abbr": parts.get("participant2Abbr") or ""},
            "score": {
                "p1": scores.get("participant1Score"),
                "p2": scores.get("participant2Score"),
            },
            "external_ids": {
                "polymarket": external.get("polymarketId"),
                "kalshi": external.get("kalshiId"),
            },
        }
    except Exception as e:
        logger.debug(f"[sportslive] normalize_meta failed: {e}")
        return None


def _filter_and_sort(items: list[dict], window_hours: int) -> list[dict]:
    """跳过已结束/取消，保留 live + 未来 window_hours 内；live 优先 + 按开赛时间升序。
    T573 — 同时丢弃 start_time 早于现在 6 小时以前的"live"幽灵 fixture（OddsPapi 偶尔
    会把已结束的赛事仍标记为 live）。"""
    now = int(time.time())
    horizon = now + window_hours * 3600
    stale_floor = now - 6 * 3600
    out = []
    for f in items:
        st_name = f.get("status") or ""
        if st_name in ("Finished", "Cancelled", "Postponed", "Abandoned"):
            continue
        start = int(f.get("start_time") or 0)
        if start and start < stale_floor:
            # likely stale "live" entry from upstream
            continue
        if f.get("is_live") or (start and now <= start <= horizon):
            out.append(f)
    out.sort(
        key=lambda f: (0 if f.get("is_live") else 1, int(f.get("start_time") or 0))
    )
    return out


# ── 路由 ──────────────────────────────────────────────────────
@router.get("/api/sportslive/sports")
def list_sports() -> JSONResponse:
    """返回受支持体育白名单 + RapidAPI 是否可用。"""
    return JSONResponse(
        {
            "items": [
                {
                    "key": s["key"],
                    "label_zh": s["label_zh"],
                    "label_en": s["label_en"],
                    "group": s["group"],
                }
                for s in _SUPPORTED_SPORTS
            ],
            "rapidapi": rapidapi_client.is_available(),
        }
    )


@router.get("/api/sportslive/event_store/stats")
def event_store_stats() -> JSONResponse:
    """T869+ — 返回 per-event 缓存统计：active/archive 计数 + 每 sport 数量。
    供运维确认 "事件驱动缓存" 是否生效。"""
    from src.utils import oddsapi_event_store as _ev_store
    from src.utils import rapidapi_client as _rc

    return JSONResponse(
        {
            "store": _ev_store.stats(),
            "budget": _rc.budget_snapshot(),
            "primary_blocked": _rc.is_primary_blocked(),
            "rapidapi_available": _rc.is_available(),
        }
    )


# ── enriched payload (内部 + 缓存) ─────────────────────────────
# T558-F: sport 级别 60 秒"完整结果"缓存——切换 sport / 重新进入页面
# 都是毫秒级返回，且单 sport 60s 内只触发一次上游拉取。
# T561 W5-A: 全 sport 后台预热缓存 — daemon 线程每 ~60s 周期刷新，
# 所有用户请求与 SSR 都直接读热缓存，**永不**触发同步上游调用。
_events_cache: dict[str, tuple[float, dict]] = {}
_events_cache_lock = _Lock()
_EVENTS_CACHE_TTL = 1800.0  # 缓存视为新鲜的窗口（s）— 30 min 防止 10K/月配额 2 天耗尽
# T586 — OddsPapi 上游不稳，warmer 偶尔拿到 502/timeout 会让 sport=0 卡。
# stale_ok 提高到 72h，配合"不覆盖好缓存"策略，让用户在长时间限流窗口内
# 仍可命中最近一次成功结果。
_EVENTS_CACHE_STALE_OK = 72 * 3600.0


def _warmer_cfg() -> tuple[float, int, int]:
    """T869: warmer 周期与候选量改为可配置；默认 6h × limit=48 × window=168h。
    旧默认（30min × limit=200）会在 2 天耗尽 10K/月配额。
    """
    from src.utils.config_loader import get as _cfg_get

    cfg = _cfg_get("sports_fetcher.warmer") or {}
    interval = float(cfg.get("interval_sec") or 21600.0)  # 6h
    limit = int(cfg.get("candidate_limit") or 48)
    window = int(cfg.get("fixtures_window_hours") or 168)
    return interval, limit, window


_inflight: set[str] = set()
_inflight_lock = _Lock()
_warmer_started = False
_warmer_lock = _Lock()
# T561 H: 至少 N 家博彩有报价才视为有"投注价值"，过滤掉空盘卡片。
_MIN_BOOKS_FOR_VALUE = 2

# T577/581-FU — 跨 worker 共享缓存目录。多 gunicorn worker 下只有 leader
# 跑 warmer，其它 worker 必须能读到 leader 写入的 payload，否则用户随机
# 命中"空 worker"就看到 0 卡。文件原子写入 + mtime 当作 fresh 判定。
_SHARED_CACHE_DIR = _os.environ.get(
    "SPORTSLIVE_SHARED_CACHE_DIR",
    "/tmp/veyra_sportslive_cache",
)
try:
    _os.makedirs(_SHARED_CACHE_DIR, exist_ok=True)
except Exception:
    pass


def _shared_cache_path(cache_key: str) -> str:
    safe = cache_key.replace("/", "_").replace(":", "__")
    return _os.path.join(_SHARED_CACHE_DIR, f"{safe}.json")


def _shared_cache_write(cache_key: str, payload: dict) -> None:
    """warmer 调用：把 payload 原子写到共享目录，供其它 worker mmap 读取。"""
    try:
        path = _shared_cache_path(cache_key)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        _os.replace(tmp, path)
    except Exception as exc:
        logger.warning(f"[sportslive] shared cache write failed key={cache_key}: {exc}")


def _shared_cache_read(cache_key: str, max_age_sec: float) -> Optional[dict]:
    """非 leader worker 调用：当本 worker 内存缓存为空时回退到磁盘。"""
    try:
        path = _shared_cache_path(cache_key)
        st = _os.stat(path)
        if (time.time() - st.st_mtime) > max_age_sec:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning(f"[sportslive] shared cache read failed key={cache_key}: {exc}")
        return None


# ── T585: OddsPapi 原始数据资产持久化 ────────────────────────
# 用户铁律：所有从 OddsPapi 拉回来的数据都是付费数据资产，必须保存，
# 不允许"用完即抛"。warmer 每次拉到成功响应就落盘一份；上游 502/timeout
# 时复用最近一次成功快照，杜绝"偶发上游故障 → sport=0 卡"。
_ODDSAPI_STORE_DIR = _os.environ.get(
    "ODDSAPI_SNAPSHOT_DIR",
    "/opt/polymarket_bot_v2/data/oddsapi_snapshots",
)
try:
    _os.makedirs(_ODDSAPI_STORE_DIR, exist_ok=True)
except Exception:
    # 容器/本地开发回退到 /tmp
    _ODDSAPI_STORE_DIR = "/tmp/veyra_oddsapi_snapshots"
    try:
        _os.makedirs(_ODDSAPI_STORE_DIR, exist_ok=True)
    except Exception:
        pass


def _oddsapi_snapshot_path(sport_key: str, kind: str) -> str:
    safe_sport = sport_key.replace("/", "_")
    safe_kind = kind.replace("/", "_")
    return _os.path.join(_ODDSAPI_STORE_DIR, f"{safe_sport}__{safe_kind}.json")


def _oddsapi_save(sport_key: str, kind: str, data) -> None:
    """成功响应才入库；写入原子化以避免读侧拿到半截 JSON。"""
    try:
        path = _oddsapi_snapshot_path(sport_key, kind)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"saved_at": time.time(), "data": data}, fh, ensure_ascii=False)
        _os.replace(tmp, path)
    except Exception as exc:
        logger.warning(
            f"[sportslive] oddsapi snapshot save failed sport={sport_key} kind={kind}: {exc}"
        )


def _oddsapi_load(
    sport_key: str,
    kind: str,
    max_age_sec: float = 72 * 3600.0,
    allow_expired: bool = False,
):
    """上游失败时调用：返回最近一次成功 payload + 它的年龄。
    返回 (data, age_seconds) 或 (None, None)。"""
    try:
        path = _oddsapi_snapshot_path(sport_key, kind)
        st = _os.stat(path)
        age = time.time() - st.st_mtime
        if age > max_age_sec and not allow_expired:
            return None, None
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj.get("data"), age
    except FileNotFoundError:
        return None, None
    except Exception as exc:
        logger.warning(
            f"[sportslive] oddsapi snapshot load failed sport={sport_key} kind={kind}: {exc}"
        )
        return None, None


# ── T575追加（已撤销） ─────────────────────────────────────────────
# 原计划用 PM /teams 接口给每张赔率卡注入两队 logo 做半透明背景，
# 因占用大量资源 + 视觉收益低，产品已回退到 "无背景" 方案。模块代码
# 与磁盘缓存均已移除。如需复原，请在 git 历史中搜索 _PM_TEAMS_API。


def _build_enriched_payload(sport_key: str, limit: int, window_hours: int) -> dict:
    """核心数据流水线（无 HTTP 装饰）：fixture metadata → odds → 我方内部
    事件匹配 → 过滤掉无内部详情页的赛事（T558-G 业务铁律）。"""
    s_meta = _sport_by_key(sport_key)
    if not s_meta:
        return {
            "sport": sport_key,
            "items": [],
            "count": 0,
            "error": "unsupported_sport",
        }

    if not rapidapi_client.is_available():
        return {
            "sport": sport_key,
            "items": [],
            "count": 0,
            "error": "rapidapi_key_missing",
        }

    list_meta = rapidapi_client.fetch_with_meta(
        api_name="odds",
        path="/fixtures",
        params={"sportId": s_meta["sportId"], "future": "true"},
    )
    raw_list = None
    upstream_source = "live"
    snapshot_age_sec = None
    if list_meta.get("ok"):
        raw_list = list_meta.get("data")
        if isinstance(raw_list, list):
            # T585 — 成功响应入库（数据资产持久化）
            _oddsapi_save(sport_key, "fixtures", raw_list)
    if not isinstance(raw_list, list):
        # T585/T586 — 上游失败，回退到最近一次成功快照
        snap, age = _oddsapi_load(sport_key, "fixtures")
        if not isinstance(snap, list) or not snap:
            snap, age = _oddsapi_load(sport_key, "fixtures", allow_expired=True)
        if isinstance(snap, list) and snap:
            raw_list = snap
            upstream_source = "snapshot"
            snapshot_age_sec = age
            logger.info(
                f"[sportslive] sport={sport_key} fixtures fallback to snapshot "
                f"age={int(age or 0)}s reason={list_meta.get('reason')}"
            )
        else:
            return {
                "sport": sport_key,
                "items": [],
                "count": 0,
                "error": list_meta.get("reason") or "upstream_unavailable",
                "http_status": list_meta.get("http_status") or 0,
            }

    metas: list[dict] = []
    for r in raw_list:
        n = _normalize_meta(r)
        if n:
            metas.append(n)
    # T730 — 快照降级时跳过时间窗口限制，避免"老数据全被过滤 → 0 卡"问题。
    # 配额耗尽时宁可展示几天前的赔率数据，也不显示空页面。
    effective_window = window_hours if upstream_source == "live" else 24 * 180
    filtered = _filter_and_sort(metas, window_hours=effective_window)
    # T577/581-FU — 用户明确命令：所有有赔率的 fixture 都必须显示，禁止
    # 替用户筛选"高价值"。因此候选池 = 全部 filtered，PM 匹配变为
    # 软关联（matched 才生成 internal_event_url，未 matched 仍出卡）。
    candidates = filtered
    # T586 — 关键稳定性修复：odds/main URL 长度受上游限制，拼几千个
    # fixtureIds 会被 RapidAPI 网关直接 502/timeout（soccer 14k+ fixtures
    # 时实测 reason=network_error）。按 limit 截断 + 50 一批分页拉取，
    # 既保证 quota 利用率又彻底消除"切到 soccer/basketball 永远 0 卡"。
    if limit and limit > 0:
        candidates = candidates[: int(limit)]
    fixture_ids = [c["id"] for c in candidates]
    if not fixture_ids:
        return {
            "sport": sport_key,
            "items": [],
            "count": 0,
            "fetched_total": len(metas),
        }

    odds_by_fid: dict[str, dict] = {}
    bookmakers_by_fid: dict[str, dict] = {}
    odds_source = "live"
    odds_any_ok = False
    last_odds_err = None
    _BATCH = 50

    # ── T869+ 事件驱动缓存（per-event store）─────────────────
    # 业务铁律：fixture 一旦拉过 odds 就保存独立 JSON，下次同样的 fixture
    # 不再重复消耗 API 配额；仅当 (a) 没拉过 (b) 临场 48h 内且距离上次刷
    # 新 > 6h，才会发起新调用。这彻底杜绝"21 sport 全量批量"的浪费。
    from src.utils import oddsapi_event_store as _ev_store

    # 1) fixture metadata 入 store（覆盖性写入，因为 fixture 不会变）
    candidates_by_id = {c["id"]: c for c in candidates}
    for c in candidates:
        try:
            _ev_store.save_fixture_meta(
                sport_key,
                c["id"],
                fixture=c,
                scheduled_iso=c.get("starts_at"),
            )
        except Exception:
            pass

    # 2) 先把 store 里已有的 odds 灌进 odds_by_fid
    cached_hits = 0
    need_fetch_ids: list[str] = []
    for fid in fixture_ids:
        ev = _ev_store.get_event(sport_key, fid)
        if ev and ev.get("odds"):
            odds_by_fid[fid] = ev.get("odds") or {}
            bookmakers_by_fid[fid] = ev.get("bookmakers") or {}
            cached_hits += 1
        sched_iso = (candidates_by_id.get(fid) or {}).get("starts_at")
        if _ev_store.should_fetch_odds(sport_key, fid, sched_iso):
            need_fetch_ids.append(fid)

    if cached_hits:
        logger.info(
            f"[sportslive] event_store hit sport={sport_key} cached={cached_hits}/{len(fixture_ids)} need_fetch={len(need_fetch_ids)}"
        )

    # 3) 仅对 need_fetch 的 fixture 调 /fixtures/odds/main
    for _i in range(0, len(need_fetch_ids), _BATCH):
        chunk = need_fetch_ids[_i : _i + _BATCH]
        odds_meta = rapidapi_client.fetch_with_meta(
            api_name="odds",
            path="/fixtures/odds/main",
            params={"fixtureIds": ",".join(chunk)},
        )
        if odds_meta.get("ok"):
            odds_any_ok = True
            odds_payload = odds_meta.get("data") or []
            if isinstance(odds_payload, list):
                for item in odds_payload:
                    if not isinstance(item, dict):
                        continue
                    fid = item.get("fixtureId")
                    if not fid:
                        continue
                    odds_dict_n = item.get("odds") or {}
                    bk_dict_n = item.get("bookmakers") or {}
                    odds_by_fid[fid] = odds_dict_n
                    bookmakers_by_fid[fid] = bk_dict_n
                    # T869+ — 立即写入 per-event store
                    try:
                        _ev_store.save_odds(sport_key, fid, odds_dict_n, bk_dict_n)
                    except Exception:
                        pass
        else:
            last_odds_err = odds_meta
            logger.warning(
                f"[sportslive] odds/main batch failed sport={sport_key} "
                f"chunk={_i}-{_i+len(chunk)} reason={odds_meta.get('reason')} "
                f"http={odds_meta.get('http_status')}"
            )

    if odds_any_ok and odds_by_fid:
        # T585 — 成功响应入库（聚合后的 by_fid 视图，回放时直接重建 odds_payload）
        _oddsapi_save(
            sport_key,
            "odds_main",
            [
                {
                    "fixtureId": fid,
                    "odds": od,
                    "bookmakers": bookmakers_by_fid.get(fid, {}),
                }
                for fid, od in odds_by_fid.items()
            ],
        )
    elif odds_by_fid and not need_fetch_ids:
        # T869+ — 全部命中 per-event 缓存，无需上游调用
        odds_source = "event_store_cache"
    elif not odds_by_fid:
        # T585/T586 — 全部 batch 失败，回退到最近一次成功快照
        snap, age = _oddsapi_load(sport_key, "odds_main")
        if not isinstance(snap, list) or not snap:
            snap, age = _oddsapi_load(sport_key, "odds_main", allow_expired=True)
        if isinstance(snap, list) and snap:
            odds_source = "snapshot"
            for item in snap:
                if not isinstance(item, dict):
                    continue
                fid = item.get("fixtureId")
                if not fid:
                    continue
                odds_by_fid[fid] = item.get("odds") or {}
                bookmakers_by_fid[fid] = item.get("bookmakers") or {}
            logger.info(
                f"[sportslive] sport={sport_key} odds fallback to snapshot age={int(age or 0)}s"
            )

    # T558-G: 一次性拉我方内部事件池（PM gamma，5 min cache），在
    # 服务端做 home/away 模糊匹配。只有匹配到我方 /event/<slug> 的
    # fixture 才会进入返回列表 —— 这是本页存在的核心商业前提。
    internal_pool = _fetch_pm_sports_events()

    enriched: list[dict] = []
    for c in candidates:
        if len(enriched) >= limit:
            break
        fid = c["id"]
        p1_name = (c.get("p1") or {}).get("name") or ""
        p2_name = (c.get("p2") or {}).get("name") or ""
        if not (p1_name and p2_name):
            continue
        odds_dict = odds_by_fid.get(fid) or {}
        bk_meta = bookmakers_by_fid.get(fid) or {}
        consensus = _extract_consensus(odds_dict) if odds_dict else _empty_consensus()
        # T577/581-FU — 用户铁律：有一家以上博彩报价就入选，全部展示。
        if int(consensus.get("books_count") or 0) < 1:
            continue
        has_odds_count = sum(
            1 for v in bk_meta.values() if isinstance(v, dict) and v.get("hasOdds")
        )
        c2 = dict(c)
        c2["consensus"] = consensus
        c2["has_odds"] = bool(consensus.get("books_count"))
        c2["bookmaker_total"] = has_odds_count
        # T577/581-FU — PM 内部事件为软关联：匹配到则提供跳转链接，
        # 未匹配仍出卡（纯赔率对比卡片，不带 internal_event_url）。
        # T871 — 双层匹配：池里没找到再 fallback 到 PM public-search 全库检索。
        match = _match_with_search_fallback(p1_name, p2_name, internal_pool)
        if match and match.get("event_slug"):
            c2["internal_event_slug"] = match["event_slug"]
            c2["internal_event_url"] = "/event/" + str(match["event_slug"])
            c2["internal_event_title"] = match.get("event_title") or ""
            c2["internal_market_yes_prob"] = match.get("yes_price")
        else:
            c2["internal_event_slug"] = ""
            c2["internal_event_url"] = ""
            c2["internal_event_title"] = ""
            c2["internal_market_yes_prob"] = None
        enriched.append(c2)

    has_any_odds = any(f.get("has_odds") for f in enriched)
    return {
        "sport": sport_key,
        "items": enriched,
        "count": len(enriched),
        "has_odds": has_any_odds,
        "fetched_total": len(metas),
        "internal_pool": len(internal_pool),
        # T585 — 数据源溯源，便于线上排障
        "fixtures_source": upstream_source,
        "odds_source": odds_source,
        "fixtures_snapshot_age_sec": snapshot_age_sec,
    }


def _trigger_async_refresh(sport_key: str, limit: int, window_hours: int) -> None:
    """T561 W5-A: SWR 异步刷新触发。同 cache_key 同时只允许一个 in-flight，
    避免并发用户对同一 sport 的请求把上游打爆。"""
    cache_key = f"{sport_key}:{limit}:{window_hours}"
    with _inflight_lock:
        if cache_key in _inflight:
            return
        _inflight.add(cache_key)

    def _run() -> None:
        try:
            payload = _build_enriched_payload(sport_key, limit, window_hours)
            if (
                not payload.get("error")
                or payload.get("error") == "rapidapi_key_missing"
            ):
                with _events_cache_lock:
                    _events_cache[cache_key] = (time.time(), payload)
        except Exception as exc:
            logger.debug(f"[sportslive] async refresh sport={sport_key} failed: {exc}")
        finally:
            with _inflight_lock:
                _inflight.discard(cache_key)

    try:
        threading.Thread(
            target=_run, name=f"sl-refresh-{sport_key}", daemon=True
        ).start()
    except Exception:
        with _inflight_lock:
            _inflight.discard(cache_key)


# ── T564: sport=all 聚合 ──────────────────────────────────────
def _aggregate_all_sports(
    limit: int,
    window_hours: int,
    *,
    only_groups: set[str] | None = None,
    exclude_groups: set[str] | None = None,
) -> dict:
    """聚合 warmer 缓存，零额外上游 IO。

    T582 — 通过 only_groups / exclude_groups 过滤，使 /sportslive 与 /esports-live
    各自只聚合自己关心的 sports。
    """
    _ensure_warmer_started()
    now = time.time()
    merged: list[dict] = []
    pool_total = 0
    pool_oddsapi = 0
    sources_warming = 0
    sources_ok = 0
    seen_ids: set[str] = set()
    for s in _SUPPORTED_SPORTS:
        grp = s.get("group") or ""
        if only_groups is not None and grp not in only_groups:
            continue
        if exclude_groups is not None and grp in exclude_groups:
            continue
        sk = s["key"]
        # T572/T869 — align cache key with warmer (configurable candidate_limit/window)
        _, _wlimit, _wwindow = _warmer_cfg()
        ck = f"{sk}:{_wlimit}:{_wwindow}"
        with _events_cache_lock:
            cached = _events_cache.get(ck)
        payload = None
        if cached:
            payload = cached[1]
        else:
            # T577/581-FU — fall back to shared disk cache for non-leader workers
            shared = _shared_cache_read(ck, _EVENTS_CACHE_STALE_OK)
            if shared:
                with _events_cache_lock:
                    _events_cache[ck] = (now, shared)
                payload = shared
        if payload is None:
            # 触发该 sport 的预热但不等待
            _trigger_async_refresh(sk, 48, 168)
            sources_warming += 1
            continue
        items = payload.get("items") or []
        if not items:
            sources_warming += 1
            continue
        sources_ok += 1
        pool_total += int(payload.get("internal_pool", 0) or 0)
        pool_oddsapi += int(payload.get("oddsapi_pool", 0) or 0)
        for it in items:
            try:
                eid = str(it.get("id") or it.get("event_id") or "")
                if eid and eid in seen_ids:
                    continue
                if eid:
                    seen_ids.add(eid)
                # 注入 sport 字段供前端按体育归类显示
                it = dict(it)
                it.setdefault("sport", sk)
                merged.append(it)
            except Exception:
                continue

    # 按 commence_time 升序排序（早开赛在前），缺省值放最后
    def _sort_key(it: dict):
        ct = it.get("commence_time") or it.get("start_time") or ""
        return (ct == "", ct)

    merged.sort(key=_sort_key)
    if limit and limit > 0:
        merged = merged[: int(limit)]
    return {
        "sport": "all",
        "items": merged,
        "count": len(merged),
        "internal_pool": pool_total,
        "oddsapi_pool": pool_oddsapi,
        "sources_ok": sources_ok,
        "sources_warming": sources_warming,
        "warming": sources_ok == 0,
        "ts": now,
    }


def _get_events_cached(sport_key: str, limit: int, window_hours: int) -> dict:
    """T561 W5-A: 三段式 SWR 读取 — 任何调用路径都毫秒级返回。
      1. 命中新鲜缓存（< TTL）：直接返回
      2. 命中陈旧缓存（< STALE_OK）：返回旧值 + 后台异步刷新
      3. 完全冷：触发后台刷新，立即返回 {warming:true, items:[]}（不阻塞）
    这样 SSR / API / 切体育类别永远不阻塞主线程，VPS 也不会被并发请求打爆。

    T564 — sport_key == "all" 时聚合所有受支持体育的 warmer 缓存，零额外上游 IO。
    """
    _ensure_warmer_started()
    if sport_key == "all":
        return _aggregate_all_sports(limit, window_hours)
    cache_key = f"{sport_key}:{limit}:{window_hours}"
    now = time.time()
    with _events_cache_lock:
        cached = _events_cache.get(cache_key)
    # T586 — 共享磁盘缓存（warmer 写入，candidate_limit/window 与 warmer 一致）
    # 是绝对的"事实源"。任何本 worker 内存里的旧/窄/0-count 结果都不应该
    # 遮蔽磁盘上的优质快照。读取顺序：磁盘 fresh > 内存 fresh > 磁盘 stale > 内存 stale > 触发刷新。
    _, _wlimit, _wwindow = _warmer_cfg()
    shared_key = f"{sport_key}:{_wlimit}:{_wwindow}"
    shared = _shared_cache_read(shared_key, _EVENTS_CACHE_STALE_OK)
    shared_count = int((shared or {}).get("count", 0) or 0) if shared else 0
    mem_count = int(((cached[1] if cached else None) or {}).get("count", 0) or 0)
    if shared and (shared_count > mem_count or not cached):
        # 磁盘版本更优（含数据 / 比内存丰富），写回内存供下次毫秒返回
        with _events_cache_lock:
            _events_cache[cache_key] = (now, shared)
        # 同时按需异步刷新，但不阻塞返回
        if cached:
            age = now - cached[0]
            if age >= _EVENTS_CACHE_TTL:
                _trigger_async_refresh(sport_key, limit, window_hours)
        return shared
    if cached:
        age = now - cached[0]
        if age < _EVENTS_CACHE_TTL:
            return cached[1]
        if age < _EVENTS_CACHE_STALE_OK:
            _trigger_async_refresh(sport_key, limit, window_hours)
            return cached[1]
    # 完全冷：触发首次预热，立即返回空骨架（warming）让前端继续吃骨架屏
    _trigger_async_refresh(sport_key, limit, window_hours)
    return {
        "sport": sport_key,
        "items": [],
        "count": 0,
        "warming": True,
    }


# ── T561 W5-A: 后台预热守护 ───────────────────────────────────
def _warmer_loop() -> None:
    """启动后立刻 warm 一次全 sport，然后按配置周期刷新。
    T869+：事件驱动模式 — 每个 fixture 一旦拉过 odds 就缓存到 per-event store，
    后续仅当 (a) 新增 fixture 或 (b) 临场 48h 内距上次刷新 > 6h 才会调用 API。
    周期默认 6h × candidate_limit=48 × window=168h。
    主 key 阻塞时仅跳过本轮（保留磁盘缓存供前端读取）。
    任何单 sport 失败不影响其他 sport 的刷新。"""
    from src.utils import oddsapi_event_store as _ev_store
    from src.utils import rapidapi_client as _rc

    time.sleep(2)  # 给进程一个启动缓冲
    while True:
        interval_sec, candidate_limit, window_h = _warmer_cfg()
        # T869+：定期清理已结束事件（释放磁盘 + 减少扫描）
        try:
            _ev_store.archive_past_events()
        except Exception as exc:
            logger.warning(f"[sportslive] archive_past_events failed: {exc}")
        # T869+：主 key 阻塞 → 跳过本轮 warmer，让 backup 配额留给真实用户请求
        if _rc.is_primary_blocked():
            logger.info(
                "[sportslive] warmer skipped: primary RapidAPI key is blocked "
                "(quota/cooldown). Per-event cache + stale snapshots will serve users; "
                "backup key is reserved for on-demand requests."
            )
            time.sleep(max(1800.0, interval_sec))
            continue
        for s in _SUPPORTED_SPORTS:
            sk = s["key"]
            try:
                payload = _build_enriched_payload(sk, candidate_limit, window_h)
                if (
                    not payload.get("error")
                    or payload.get("error") == "rapidapi_key_missing"
                ):
                    cache_key_w = f"{sk}:{candidate_limit}:{window_h}"
                    new_count = int(payload.get("count", 0) or 0)
                    # T586 — 稳定性铁律：上游瞬时故障导致 new_count 远低于
                    # 上次成功结果时，保留旧 payload，绝不让 0 卡覆盖好数据。
                    keep_old = False
                    with _events_cache_lock:
                        prev = _events_cache.get(cache_key_w)
                    if prev:
                        prev_payload = prev[1] or {}
                        prev_count = int(prev_payload.get("count", 0) or 0)
                        # 新结果为 0 而旧结果非 0 → 跳过覆盖
                        # 新结果不到旧结果 30% 且旧结果 ≥ 5 → 跳过覆盖
                        if (new_count == 0 and prev_count > 0) or (
                            prev_count >= 5 and new_count * 10 < prev_count * 3
                        ):
                            keep_old = True
                            logger.info(
                                f"[sportslive] warmer sport={sk} keep prev "
                                f"prev_count={prev_count} new_count={new_count} "
                                f"fixtures_source={payload.get('fixtures_source')}"
                            )
                    if not keep_old:
                        with _events_cache_lock:
                            _events_cache[cache_key_w] = (time.time(), payload)
                        # T577/581-FU — 同步广播到磁盘，供其它 worker 读取
                        _shared_cache_write(cache_key_w, payload)
                        logger.info(
                            f"[sportslive] warmer sport={sk} items={new_count} "
                            f"books_pool={payload.get('internal_pool',0)} "
                            f"src={payload.get('fixtures_source','live')}"
                        )
            except Exception as exc:
                logger.warning(f"[sportslive] warmer sport={sk} failed: {exc}")
            # 每个 sport 之间稍微让一让，避免对 RapidAPI 形成 burst
            time.sleep(0.5)
        sleep_sec = max(1800.0, interval_sec)
        time.sleep(sleep_sec)


def _ensure_warmer_started() -> None:
    global _warmer_started
    with _warmer_lock:
        if _warmer_started:
            return
        # Phase-2: Gunicorn 多 worker 下，仅 leader worker 启动 warmer。
        try:
            from src.utils.process_leader import try_acquire_leader

            if not try_acquire_leader("sportslive_warmer"):
                _warmer_started = True
                logger.info(
                    "[sportslive] warmer skipped on non-leader worker pid=%d",
                    __import__("os").getpid(),
                )
                return
        except Exception as exc:
            logger.warning(
                f"[sportslive] warmer leader check failed: {exc} — falling back to start"
            )
        try:
            threading.Thread(
                target=_warmer_loop,
                name="sportslive-warmer",
                daemon=True,
            ).start()
            _warmer_started = True
            _wint, _wlim, _wwin = _warmer_cfg()
            logger.info(
                f"[sportslive] T561/T869 warmer thread started "
                f"(interval={_wint:.0f}s candidate_limit={_wlim} window={_wwin}h)"
            )
        except Exception as exc:
            logger.warning(f"[sportslive] failed to start warmer: {exc}")


# 模块加载时立刻启动一次（与 discover_routes 一致）
_ensure_warmer_started()


def get_ssr_events(sport_key: str = "basketball", limit: int = 24) -> dict:
    """供 frontend_routes 在 SSR 阶段直接 inline 注入到 HTML 的同步入口。

    T576 — 必须复用 warmer 写入的缓存键 `sport:48:168`，否则任何
    `_get_events_cached(..., 24, 72)` 的调用都会冷未命中并返回 warming，
    用户首次访问 /sportslive 就只能看到 "No upcoming matches" 直到手动刷新。
    具体策略：直接读 warmer 缓存（48/168），如果命中就把 items 截断到 limit；
    如果完全冷启动则触发 warmer 并返回空骨架（与既有行为一致）。
    """
    try:
        _, _wlimit, _wwindow = _warmer_cfg()
        payload = _get_events_cached(sport_key, _wlimit, _wwindow)
        items = payload.get("items") or []
        if limit and limit > 0 and len(items) > limit:
            payload = dict(payload)
            payload["items"] = items[: int(limit)]
            payload["count"] = len(payload["items"])
        return payload
    except Exception as e:
        logger.warning(f"[sportslive] get_ssr_events failed: {e}")
        return {"sport": sport_key, "items": [], "count": 0, "error": "ssr_failed"}


@router.get("/api/sportslive/events")
def list_events(
    sport: str = Query(
        "all", description="sport key, e.g. basketball; 'all' aggregates"
    ),
    limit: int = Query(2000, ge=1, le=5000),
    window_hours: int = Query(168, ge=1, le=336),
    page_kind: str = Query("sports", description="sports | esports"),
) -> JSONResponse:
    """指定体育的近期/进行中赛事 + 多博彩 consensus + 我方内部事件入口。

    T564 — sport='all' 聚合所有受支持体育的 warmer 缓存（零额外 IO）。
    """
    sport_norm = (sport or "all").strip().lower()
    page_kind_norm = (page_kind or "sports").strip().lower()
    only_groups = {"esports"} if page_kind_norm == "esports" else None
    exclude_groups = None if page_kind_norm == "esports" else {"esports"}

    if sport_norm != "all" and not _sport_by_key(sport_norm):
        return JSONResponse(
            {
                "sport": sport_norm,
                "items": [],
                "count": 0,
                "error": "unsupported_sport",
            },
            status_code=400,
        )

    if sport_norm != "all":
        s_meta = _sport_by_key(sport_norm) or {}
        grp = s_meta.get("group") or ""
        if only_groups is not None and grp not in only_groups:
            return JSONResponse(
                {
                    "sport": sport_norm,
                    "items": [],
                    "count": 0,
                    "error": "unsupported_sport",
                },
                status_code=400,
            )
        if exclude_groups is not None and grp in exclude_groups:
            return JSONResponse(
                {
                    "sport": sport_norm,
                    "items": [],
                    "count": 0,
                    "error": "unsupported_sport",
                },
                status_code=400,
            )

    if sport_norm == "all":
        payload = _aggregate_all_sports(
            limit,
            window_hours,
            only_groups=only_groups,
            exclude_groups=exclude_groups,
        )
    else:
        payload = _get_events_cached(sport_norm, limit, window_hours)
    return JSONResponse(payload)


# ── PM 模糊匹配 ────────────────────────────────────────────────
# 将 OddsPapi fixture (home/away 名) 与 Polymarket sports 类活跃 event 标题做 token 匹配。
# 命中后把 event_slug + market_question + 当前价(Yes 概率) 一起返回，前端用于：
#   1) 标题旁加 "→ PM 详情" 跳转按钮
#   2) 计算 +EV 徽章: ev = (pm_yes_prob - p1_implied) * p1_max_decimal
#
# Gamma /events?tag_slug=sports 公开端点；TTL 5 min 缓存避免反复打。

import re as _re
import unicodedata as _ud

import requests as _requests  # noqa: E402

_PM_GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
# T577/T580/T581 — 全体育子标签兜底匹配池
# 历史教训（T561 W5-A）：7 tag * 8s timeout 会把 worker 挂死。本次保留
# 3s 单 tag 超时上限并并发降级（每 tag limit=200，去重合并），即便所有
# tag 全部超时也仅 3s × tag 数；warmer 在后台完成，不会阻塞用户线程。
# 必须列出每一个 OddsPapi 支持的体育子标签，否则像 boxing / ice-hockey /
# tennis / mma / american-football / baseball 的 PM 事件不会进入匹配池，
# /sportslive?p=boxing 永远空。
_PM_SPORT_TAGS = (
    "sports",
    "soccer",
    "basketball",
    "football",  # 美式 / 英式 同名 fallback
    "american-football",
    "baseball",
    "tennis",
    "hockey",
    "ice-hockey",
    "mma",
    "ufc",
    "boxing",
    "cricket",
)
_PM_FETCH_TIMEOUT = 5  # 单 tag 超时（s）— 收紧到 5s 防止 worker 长时间挂起

_pm_cache: dict[str, tuple[float, list[dict]]] = {}
_pm_cache_lock = _Lock()
_PM_CACHE_TTL = 300  # 5 min

# T871 — public-search 结果缓存（高保真匹配兜底）
# 同一 (home, away) 5 分钟内只查一次 PM gamma；命中后下游 _match_one 复用同样的
# token-overlap 评分，仅 events 池来源不同（pool 是按 tag 拉的 200 条 + 24h 缓存；
# search 池是 PM 全库 full-text 命中的 ≤10 条，命中率显著更高）。
_PM_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
_pm_search_cache: dict[str, tuple[float, list[dict]]] = {}
_pm_search_cache_lock = _Lock()
_PM_SEARCH_CACHE_TTL = 300  # 5 min
_PM_SEARCH_TIMEOUT = 4  # s


def _pm_public_search(
    query: str, limit_per_type: int = 10, events_status: str = "active"
) -> list[dict]:
    """T871 — 直连 PM gamma `/public-search`，返回 events 数组。
    与 `/events?search=` 不同，public-search 是真正的 full-text 检索，
    可命中低热度赛事（J-League、各国小联赛、电竞）。"""
    q = (query or "").strip()
    if not q:
        return []
    cache_key = f"{q}|{limit_per_type}|{events_status}".lower()
    now = time.time()
    with _pm_search_cache_lock:
        cached = _pm_search_cache.get(cache_key)
        if cached and now - cached[0] < _PM_SEARCH_CACHE_TTL:
            return cached[1]
    try:
        r = _requests.get(
            _PM_SEARCH_URL,
            params={
                "q": q,
                "limit_per_type": limit_per_type,
                "events_status": events_status,
            },
            timeout=_PM_SEARCH_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        data = r.json() or {}
        events = data.get("events") or []
        if not isinstance(events, list):
            events = []
    except Exception as e:
        logger.debug(f"[sportslive] PM public-search q={q!r} failed: {e}")
        events = []
    with _pm_search_cache_lock:
        _pm_search_cache[cache_key] = (now, events)
    return events


def _norm_token(s: str) -> str:
    if not s:
        return ""
    s = _ud.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = _re.sub(r"\b(fc|cf|sc|cd|ca|ac|cp|de|del|el|la|los|club|the)\b", " ", s)
    s = _re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _tokenize(s: str) -> set[str]:
    return {t for t in _norm_token(s).split() if len(t) >= 3}


def _fetch_pm_sports_events() -> list[dict]:
    """拉 PM sports 类活跃 events（5 min cache，多 tag 合并去重）。"""
    now = time.time()
    with _pm_cache_lock:
        cached = _pm_cache.get("sports")
        if cached and now - cached[0] < _PM_CACHE_TTL:
            return cached[1]
    out: list[dict] = []
    seen: set[str] = set()
    for tag in _PM_SPORT_TAGS:
        try:
            r = _requests.get(
                _PM_GAMMA_EVENTS,
                params={
                    "tag_slug": tag,
                    "active": "true",
                    "closed": "false",
                    "limit": 200,
                },
                timeout=_PM_FETCH_TIMEOUT,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            if not isinstance(data, list):
                continue
            for ev in data:
                if not isinstance(ev, dict):
                    continue
                slug = ev.get("slug") or ""
                if not slug or slug in seen:
                    continue
                # T573 — drop already-finished PM events (gamma sometimes
                # returns events whose endDate is in the past despite
                # active=true). Without this guard old fixtures get matched
                # and surface as "请选择上方一个事件选项" on detail page.
                end_date = ev.get("endDate") or ev.get("end_date") or ""
                if end_date:
                    try:
                        from datetime import datetime as _dt, timezone as _tz

                        _ed = _dt.fromisoformat(str(end_date).replace("Z", "+00:00"))
                        if _ed.timestamp() < now - 3600:  # 1h grace
                            continue
                    except Exception:
                        pass
                if ev.get("closed") is True or ev.get("archived") is True:
                    continue
                seen.add(slug)
                out.append(ev)
        except Exception as e:
            logger.debug(f"[sportslive] PM gamma fetch tag={tag} failed: {e}")
            continue
    with _pm_cache_lock:
        _pm_cache["sports"] = (now, out)
    logger.info(f"[sportslive] PM sports events cached: {len(out)} events")
    return out


def _yes_price_from_market(mkt: dict) -> Optional[float]:
    """PM market.outcomePrices 是 JSON 字符串如 '[\"0.51\", \"0.49\"]'；取 Yes(第一)。"""
    raw = mkt.get("outcomePrices")
    if isinstance(raw, str):
        try:
            import json as _json

            arr = _json.loads(raw)
            if isinstance(arr, list) and arr:
                return float(arr[0])
        except Exception:
            return None
    if isinstance(raw, list) and raw:
        try:
            return float(raw[0])
        except Exception:
            return None
    return None


def _match_one(home: str, away: str, events: list[dict]) -> Optional[dict]:
    h_toks = _tokenize(home)
    a_toks = _tokenize(away)
    if not (h_toks and a_toks):
        return None
    best: Optional[tuple[int, dict, dict]] = None  # (score, event, market)
    for ev in events:
        title_toks = _tokenize(ev.get("title") or "")
        slug_toks = _tokenize(ev.get("slug") or "")
        ev_toks = title_toks | slug_toks
        h_hit = len(h_toks & ev_toks)
        a_hit = len(a_toks & ev_toks)
        if h_hit < 1 or a_hit < 1:
            continue
        # 必须双方各至少命中 1 token
        score = h_hit + a_hit + (2 if (h_hit >= 2 and a_hit >= 2) else 0)
        # 优先 winner/moneyline/will-win 类的 binary market（避免 Over/Under、props）
        markets = ev.get("markets") or []
        chosen_mkt: Optional[dict] = None
        scored_mkts: list[tuple[int, dict]] = []
        for mk in markets:
            if not isinstance(mk, dict):
                continue
            if mk.get("closed"):
                continue
            if mk.get("active") is False:
                continue
            q = str(mk.get("question") or "").lower()
            sc = 0
            if any(
                kw in q for kw in (" win ", "will win", "winner", "wins ", "winning")
            ):
                sc += 5
            if any(kw in q for kw in (" beat ", "beats ", "defeats", "vs.", " vs ")):
                sc += 3
            if any(
                kw in q
                for kw in (
                    "over",
                    "under",
                    "o/u",
                    "total",
                    "spread",
                    "handicap",
                    "props",
                )
            ):
                sc -= 4
            scored_mkts.append((sc, mk))
        if scored_mkts:
            scored_mkts.sort(key=lambda x: -x[0])
            chosen_mkt = scored_mkts[0][1]
        if not chosen_mkt:
            continue
        if best is None or score > best[0]:
            best = (score, ev, chosen_mkt)
    if not best:
        return None
    _score, ev, mkt = best
    yes_prob = _yes_price_from_market(mkt)
    slug = ev.get("slug") or ""
    return {
        "matched": True,
        "score": _score,
        "event_slug": slug,
        "event_title": ev.get("title") or "",
        # T558-B: 跳转我方 /event/<slug> 详情页，不再输出 polymarket.com 外链
        "event_url": f"/event/{slug}",
        "market_question": mkt.get("question") or "",
        "condition_id": mkt.get("conditionId") or "",
        "yes_price": yes_prob,
    }


def _short_team_name(name: str) -> str:
    """T871 — 取队名前两个有效 token（≥3 字符），用于 public-search 查询；
    避免 'FC' / 'Sanga' 这种公共后缀污染搜索关键词。"""
    if not name:
        return ""
    toks = [t for t in _norm_token(name).split() if len(t) >= 3]
    return " ".join(toks[:2]) if toks else name


def _match_with_search_fallback(
    home: str, away: str, pool: list[dict]
) -> Optional[dict]:
    """T871 — 双层匹配：先在已缓存的 PM 池里找；找不到再调 public-search 兜底。

    关键修复：旧实现仅用 `?tag_slug=soccer&limit=200` 拉一个粗大池子做模糊匹配，
    PM 的 200 条按 volume 降序，J-League / 小联赛 / 部分电竞事件根本不在池里，
    导致 sportslive 卡片普遍匹配不上 PM 详情页。public-search 直接对 PM 全库
    做 full-text 检索，命中率远高于 token 池法。"""
    m = _match_one(home, away, pool)
    if m and m.get("event_slug"):
        return m
    # 兜底：用关键词组合召回 PM 全库相关事件
    h_kw = _short_team_name(home)
    a_kw = _short_team_name(away)
    if not (h_kw or a_kw):
        return None
    queries: list[str] = []
    if h_kw and a_kw:
        queries.append(f"{h_kw} {a_kw}")
    if h_kw:
        queries.append(h_kw)
    if a_kw and a_kw not in queries:
        queries.append(a_kw)
    seen_slugs: set[str] = set()
    candidates: list[dict] = []
    for q in queries:
        for ev in _pm_public_search(q, limit_per_type=10):
            slug = ev.get("slug") or ""
            if not slug or slug in seen_slugs:
                continue
            if ev.get("closed") is True or ev.get("archived") is True:
                continue
            end_date = ev.get("endDate") or ev.get("end_date") or ""
            if end_date:
                try:
                    from datetime import datetime as _dt

                    _ed = _dt.fromisoformat(str(end_date).replace("Z", "+00:00"))
                    if _ed.timestamp() < time.time() - 3600:
                        continue
                except Exception:
                    pass
            seen_slugs.add(slug)
            candidates.append(ev)
        if candidates:
            m2 = _match_one(home, away, candidates)
            if m2 and m2.get("event_slug"):
                return m2
    return None


@router.post("/api/sportslive/pm-match-batch")
async def pm_match_batch(payload: dict) -> JSONResponse:
    """兑现兼容端点（仅供调试/老前端占位使用；T558 后前端已不再调）。
    返回字段中的 event_url 已改为我方 /event/<slug>。"""
    pairs = (payload or {}).get("pairs") or []
    if not isinstance(pairs, list) or not pairs:
        return JSONResponse({"results": {}})
    events = _fetch_pm_sports_events()
    results: dict[str, dict] = {}
    for p in pairs[:60]:
        if not isinstance(p, dict):
            continue
        fid = str(p.get("id") or "").strip()
        home = str(p.get("home") or "").strip()
        away = str(p.get("away") or "").strip()
        if not (fid and home and away):
            continue
        m = _match_with_search_fallback(home, away, events)
        results[fid] = m or {"matched": False}
    return JSONResponse({"results": results, "internal_pool": len(events)})
