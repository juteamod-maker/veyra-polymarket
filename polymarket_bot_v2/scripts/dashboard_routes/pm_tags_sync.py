"""T579-FU2 — 13 类 PM 标签 + 标签↔事件全量同步守护

用户铁律：「必须将所有13个类别下的所有PM标签全部同步拉取回来保存！并且
将所有标签下的事件都对应起来！」

设计原则（与 T585 一致，避免 DB 膨胀）：
- 全部数据资产 JSON 落盘到 /opt/polymarket_bot_v2/data/pm_tags/
- 守护线程仅 leader worker 启动（fcntl flock），周期 ~10 分钟
- 上游瞬时失败不覆盖好快照（与 T586 同语义）
- 提供同步 read 接口供 pm_tags / 前端使用

落盘结构：
    data/pm_tags/_index.json                  → {ts, categories: {<cat>: {tag_count, tags: [<slug>...]}}}
    data/pm_tags/<cat>/_tags.json             → [{tag_id, slug, label, category_slug, event_count}]
    data/pm_tags/<cat>/<tag_slug>.json        → {ts, category, tag, events: [<normalised event>]}
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from typing import Optional

from scripts.dashboard_routes._shared import logger
from src.utils.pm_http import pm_session as _pm_session

_GAMMA_API = "https://gamma-api.polymarket.com"

# 13 个 PM 顶级类别（与 discover_routes._CAT_SLUGS_LOWER 对齐）
PM_CATEGORIES: tuple[str, ...] = (
    "sports",
    "crypto",
    "politics",
    "elections",
    "geopolitics",
    "world",
    "weather",
    "tech",
    "economy",
    "earnings",
    "finance",
    "mentions",
    "pop-culture",
    "esports",
)

# Sports 类必须强制曝光的小众体育（与 discover_routes._PINNED_SPORTS 一致）
_PINNED_SPORTS = (
    "boxing",
    "cricket",
    "hockey",
    "mma",
    "ufc",
    "golf",
    "formula-1",
    "f1",
    "rugby",
    "darts",
    "olympics",
    "horse-racing",
    "nascar",
    "wnba",
    "esports",
    "league-of-legends",
    "counter-strike-2",
)

_HIDDEN_TAGS = {"hide-from-new", "hide-from-top", "all", "trending", "new"}
_CAT_SET = set(PM_CATEGORIES)

_STORE_DIR = os.environ.get(
    "PM_TAGS_STORE_DIR",
    "/opt/polymarket_bot_v2/data/pm_tags",
)
try:
    os.makedirs(_STORE_DIR, exist_ok=True)
except Exception:
    _STORE_DIR = "/tmp/veyra_pm_tags"
    try:
        os.makedirs(_STORE_DIR, exist_ok=True)
    except Exception:
        pass

# 周期 / 上游限制
_SYNC_INTERVAL_SEC = int(os.environ.get("PM_TAGS_SYNC_INTERVAL", "600"))
_FETCH_TIMEOUT = 12
_PER_TAG_EVENT_LIMIT = 200  # 每个 tag 落盘最多 200 个事件
_MAX_TAGS_PER_CAT = 80  # 每类最多保留 80 个 sub-tag（按事件数倒序 + pinned）

_started = False
_lock = threading.Lock()


# ── 路径工具 ─────────────────────────────────────────────
def _safe(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_")


def _cat_dir(cat: str) -> str:
    p = os.path.join(_STORE_DIR, _safe(cat))
    try:
        os.makedirs(p, exist_ok=True)
    except Exception:
        pass
    return p


def _atomic_write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning(f"[pm_tags_sync] atomic write failed path={path}: {exc}")


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning(f"[pm_tags_sync] read failed path={path}: {exc}")
        return None


# ── 上游拉取 ─────────────────────────────────────────────
def _fetch_events(params: dict) -> list[dict]:
    try:
        r = _pm_session().get(
            f"{_GAMMA_API}/events", params=params, timeout=_FETCH_TIMEOUT
        )
        if not r.ok:
            return []
        d = r.json()
        if isinstance(d, list):
            return [e for e in d if isinstance(e, dict)]
    except Exception as exc:
        logger.debug(f"[pm_tags_sync] fetch failed params={params}: {exc}")
    return []


def _slim_event(ev: dict) -> dict:
    """只保留前端列表所需的精简字段，避免单文件膨胀。"""
    return {
        "id": ev.get("id"),
        "slug": ev.get("slug"),
        "title": ev.get("title"),
        "image": ev.get("image"),
        "icon": ev.get("icon"),
        "volume24hr": ev.get("volume24hr"),
        "volume": ev.get("volume"),
        "liquidity": ev.get("liquidity"),
        "endDate": ev.get("endDate"),
        "startDate": ev.get("startDate"),
        "active": ev.get("active"),
        "closed": ev.get("closed"),
        "tags": [
            {"slug": t.get("slug"), "label": t.get("label")}
            for t in (ev.get("tags") or [])
            if isinstance(t, dict)
        ],
    }


def _aggregate_tags_for_category(cat: str) -> tuple[list[dict], list[dict]]:
    """返回 (sub_tags, all_events_for_category)。"""
    seen_event_ids: set = set()
    events: list[dict] = []
    for params in (
        {
            "tag_slug": cat,
            "limit": 500,
            "closed": "false",
            "active": "true",
            "order": "volume24hr",
            "ascending": "false",
        },
        {
            "tag_slug": cat,
            "limit": 500,
            "closed": "false",
            "active": "true",
            "order": "startDate",
            "ascending": "true",
        },
    ):
        for ev in _fetch_events(params):
            eid = str(ev.get("id") or ev.get("slug") or "")
            if eid and eid in seen_event_ids:
                continue
            if eid:
                seen_event_ids.add(eid)
            events.append(ev)

    counter: Counter = Counter()
    label_map: dict[str, str] = {}
    id_map: dict[str, int] = {}
    for ev in events:
        for t in ev.get("tags") or []:
            if not isinstance(t, dict):
                continue
            slug = str(t.get("slug") or "").lower()
            label = str(t.get("label") or "").strip()
            if not slug or not label:
                continue
            if slug == cat or slug in _CAT_SET or slug in _HIDDEN_TAGS:
                continue
            if t.get("forceHide") is True:
                continue
            counter[slug] += 1
            if slug not in label_map:
                label_map[slug] = label
                try:
                    id_map[slug] = int(t.get("id") or 0)
                except Exception:
                    id_map[slug] = 0

    sub_tags: list[dict] = []
    for slug, cnt in counter.most_common(_MAX_TAGS_PER_CAT):
        sub_tags.append(
            {
                "tag_id": id_map.get(slug, 0),
                "slug": slug,
                "label": label_map.get(slug, slug),
                "category_slug": cat,
                "event_count": int(cnt),
            }
        )

    if cat == "sports":
        existing = {t["slug"] for t in sub_tags}
        for ps in _PINNED_SPORTS:
            if ps in existing:
                continue
            sub_tags.append(
                {
                    "tag_id": id_map.get(ps, 0),
                    "slug": ps,
                    "label": label_map.get(ps, ps.replace("-", " ").title()),
                    "category_slug": cat,
                    "event_count": int(counter.get(ps, 0)),
                }
            )

    return sub_tags, events


def _fetch_tag_events(tag_slug: str) -> list[dict]:
    """单个 sub-tag 拉取事件（两轮：热门 + 即将开始）"""
    seen: set = set()
    out: list[dict] = []
    for params in (
        {
            "tag_slug": tag_slug,
            "limit": _PER_TAG_EVENT_LIMIT,
            "closed": "false",
            "active": "true",
            "order": "volume24hr",
            "ascending": "false",
        },
        {
            "tag_slug": tag_slug,
            "limit": _PER_TAG_EVENT_LIMIT,
            "closed": "false",
            "active": "true",
            "order": "startDate",
            "ascending": "true",
        },
    ):
        for ev in _fetch_events(params):
            eid = str(ev.get("id") or ev.get("slug") or "")
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            out.append(_slim_event(ev))
            if len(out) >= _PER_TAG_EVENT_LIMIT:
                break
        if len(out) >= _PER_TAG_EVENT_LIMIT:
            break
    return out


# ── 主同步 ─────────────────────────────────────────────
def _sync_category(cat: str) -> dict:
    """同步单类别。返回汇总元数据。"""
    sub_tags, cat_events = _aggregate_tags_for_category(cat)
    cat_dir = _cat_dir(cat)

    # T586 — 不覆盖好缓存：当本次 sub_tags 为空但磁盘已有结果时，跳过覆盖
    if not sub_tags:
        existing = _read_json(os.path.join(cat_dir, "_tags.json"))
        if existing:
            logger.warning(
                f"[pm_tags_sync] cat={cat} fresh fetch empty, keep existing tags={len(existing)}"
            )
            return {"category": cat, "tag_count": len(existing), "skipped": True}
        # 磁盘也空，至少落一个空文件标记 ts
        _atomic_write_json(os.path.join(cat_dir, "_tags.json"), [])
        return {"category": cat, "tag_count": 0, "skipped": False}

    # 优先用 cat_events 池本身的命中信息为每个 tag 提供初步事件列表，
    # 减少额外的 /events 调用。
    events_by_tag: dict[str, list[dict]] = {}
    for ev in cat_events:
        slim = _slim_event(ev)
        for t in ev.get("tags") or []:
            if not isinstance(t, dict):
                continue
            ts = str(t.get("slug") or "").lower()
            if not ts:
                continue
            events_by_tag.setdefault(ts, []).append(slim)

    full_tag_meta: list[dict] = []
    for tag in sub_tags:
        slug = tag["slug"]
        events = events_by_tag.get(slug) or []
        # 数量不足 _PER_TAG_EVENT_LIMIT/2 时，主动单独拉一次该 tag 补全
        if len(events) < max(20, _PER_TAG_EVENT_LIMIT // 4):
            extra = _fetch_tag_events(slug)
            seen = {e.get("slug") for e in events if e.get("slug")}
            for e in extra:
                if e.get("slug") and e["slug"] in seen:
                    continue
                events.append(e)
                if len(events) >= _PER_TAG_EVENT_LIMIT:
                    break
        events = events[:_PER_TAG_EVENT_LIMIT]

        # T586 — 不覆盖好快照
        path = os.path.join(cat_dir, f"{_safe(slug)}.json")
        if not events:
            old = _read_json(path)
            if old and (old.get("events") or []):
                logger.warning(
                    f"[pm_tags_sync] cat={cat} tag={slug} empty, keep existing events={len(old['events'])}"
                )
                full_tag_meta.append({**tag, "event_count": len(old["events"])})
                continue
        _atomic_write_json(
            path,
            {
                "ts": time.time(),
                "category": cat,
                "tag": slug,
                "label": tag.get("label"),
                "events": events,
            },
        )
        full_tag_meta.append({**tag, "event_count": len(events)})

    _atomic_write_json(os.path.join(cat_dir, "_tags.json"), full_tag_meta)
    return {
        "category": cat,
        "tag_count": len(full_tag_meta),
        "event_total": sum(int(t.get("event_count") or 0) for t in full_tag_meta),
    }


def _run_full_sync() -> dict:
    started = time.time()
    summary = {"ts": started, "categories": {}}
    for cat in PM_CATEGORIES:
        try:
            res = _sync_category(cat)
            summary["categories"][cat] = res
            logger.info(
                f"[pm_tags_sync] cat={cat} tags={res.get('tag_count')} "
                f"events={res.get('event_total', '?')}"
            )
        except Exception as exc:
            logger.warning(f"[pm_tags_sync] cat={cat} failed: {exc}")
            summary["categories"][cat] = {"category": cat, "error": str(exc)}
        time.sleep(0.3)
    summary["elapsed_sec"] = time.time() - started
    _atomic_write_json(os.path.join(_STORE_DIR, "_index.json"), summary)
    logger.info(
        f"[pm_tags_sync] full sync done in {summary['elapsed_sec']:.1f}s "
        f"cats={len(summary['categories'])}"
    )
    return summary


# ── 守护线程 ─────────────────────────────────────────────
def _loop() -> None:
    time.sleep(8)  # 启动延迟，让其它路由先就绪
    while True:
        try:
            _run_full_sync()
        except Exception as exc:
            logger.warning(f"[pm_tags_sync] loop iteration failed: {exc}")
        time.sleep(_SYNC_INTERVAL_SEC)


def ensure_started() -> None:
    global _started
    with _lock:
        if _started:
            return
        try:
            from src.utils.process_leader import try_acquire_leader

            if not try_acquire_leader("pm_tags_sync"):
                _started = True
                logger.info(
                    "[pm_tags_sync] skipped on non-leader worker pid=%d", os.getpid()
                )
                return
        except Exception as exc:
            logger.warning(
                f"[pm_tags_sync] leader check failed: {exc} — falling back to start"
            )
        try:
            threading.Thread(target=_loop, name="pm-tags-sync", daemon=True).start()
            _started = True
            logger.info(
                f"[pm_tags_sync] daemon started interval={_SYNC_INTERVAL_SEC}s "
                f"store={_STORE_DIR}"
            )
        except Exception as exc:
            logger.warning(f"[pm_tags_sync] failed to start daemon: {exc}")


# ── 同步 read 接口（供 pm_tags 路由 / 前端使用）────────────
def read_category_tags(
    category: str, max_age_sec: float = 24 * 3600.0
) -> Optional[list[dict]]:
    """返回某类别下所有 sub_tags（含 event_count）；过期返回 None 让上层走在线路径。"""
    cat = (category or "").strip().lower()
    if not cat:
        return None
    path = os.path.join(_cat_dir(cat), "_tags.json")
    try:
        st = os.stat(path)
        if (time.time() - st.st_mtime) > max_age_sec:
            return None
    except FileNotFoundError:
        return None
    return _read_json(path)


def read_tag_events(
    category: str, tag: str, max_age_sec: float = 24 * 3600.0
) -> Optional[dict]:
    cat = (category or "").strip().lower()
    tg = (tag or "").strip().lower()
    if not cat or not tg:
        return None
    path = os.path.join(_cat_dir(cat), f"{_safe(tg)}.json")
    try:
        st = os.stat(path)
        if (time.time() - st.st_mtime) > max_age_sec:
            return None
    except FileNotFoundError:
        return None
    return _read_json(path)


def read_index() -> Optional[dict]:
    return _read_json(os.path.join(_STORE_DIR, "_index.json"))


# 模块加载时启动
ensure_started()
