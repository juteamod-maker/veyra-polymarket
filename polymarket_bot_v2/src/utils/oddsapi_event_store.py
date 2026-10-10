"""T869+ — Per-event OddsPapi 数据存储（事件驱动缓存）

业务铁律（用户原文）：
  "假设这个月我们一共从 PM 拉取了 3 千个各项体育赛事和电子竞技事件，
   那么也就才需要调用查询三千次 oddspapi 的 API…如果从 PM 新增了体育赛
   事和电子竞技事件，那么我们也就在已经存储的对应 json 里新增赔率等数
   据即可，无需每次都去查询。"

实现策略
--------
- 每个 fixture 对应一个独立 JSON 文件：
    data/oddsapi_event_store/{sport_key}/{fixture_id}.json
- 文件结构：
    {
      "sport_key": "soccer",
      "fixture_id": "abc123",
      "fixture":   {... 来自 /fixtures 的原始 metadata ...},
      "odds":      {... 来自 /fixtures/odds/main 的 odds dict ...},
      "bookmakers":{... 同上 bookmakers dict ...},
      "fetched_at":      1778034901.5,   # fixture metadata 写入时间
      "odds_fetched_at": 1778035012.7,   # 最近一次 odds 写入时间（无则缺省）
      "scheduled_iso":   "2026-05-07T18:00:00Z",  # 比赛开始 ISO（可空）
    }

刷新策略
--------
- **新事件**（无 odds_fetched_at）：必须取一次 odds
- **未来 48h 内开赛 + 距离上次 odds 抓取 > 6h**：刷新
- **未来 48h 之外**：跳过（赔率波动小）
- **已经开始/已结束**：归档（移到 archive/，不再刷新）
- **无 scheduled_iso**：保守 6h 刷新一次

API 调用预算估算
----------------
- 月度新事件 ~3000 → 3000 odds calls（首次）
- 临场刷新 ~100 events × 4 = 400 calls
- 总：~3400 calls/月，远低于 10K 配额
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 路径定位 ─────────────────────────────────────────────
_THIS = Path(__file__).resolve()
_REPO_ROOT_CANDIDATES = [
    _THIS.parents[2],  # polymarket_bot_v2/
    Path("/opt/polymarket_bot_v2"),  # VPS 生产路径
]
_DATA_ROOT: Path | None = None
for _c in _REPO_ROOT_CANDIDATES:
    if (_c / "data").exists():
        _DATA_ROOT = _c / "data"
        break
if _DATA_ROOT is None:
    _DATA_ROOT = _THIS.parents[2] / "data"
_STORE_DIR = _DATA_ROOT / "oddsapi_event_store"
_ARCHIVE_DIR = _STORE_DIR / "_archive"

# ── 同步锁（每个 sport 一把锁，避免热点） ─────────────────
_locks: dict[str, threading.Lock] = {}
_locks_master = threading.Lock()


def _lock_for(sport_key: str) -> threading.Lock:
    with _locks_master:
        lk = _locks.get(sport_key)
        if lk is None:
            lk = threading.Lock()
            _locks[sport_key] = lk
        return lk


def _sport_dir(sport_key: str) -> Path:
    return _STORE_DIR / sport_key


def _event_path(sport_key: str, fixture_id: str | int) -> Path:
    safe = str(fixture_id).replace("/", "_").replace("..", "_")
    return _sport_dir(sport_key) / f"{safe}.json"


def _archive_path(sport_key: str, fixture_id: str | int) -> Path:
    safe = str(fixture_id).replace("/", "_").replace("..", "_")
    return _ARCHIVE_DIR / sport_key / f"{safe}.json"


# ── 时间帮助函数 ───────────────────────────────────────────
def _parse_iso(iso: str | None) -> float | None:
    if not iso or not isinstance(iso, str):
        return None
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        try:
            return datetime.fromisoformat(iso).timestamp()
        except Exception:
            return None


# ── 公共 API ───────────────────────────────────────────────
def get_event(sport_key: str, fixture_id: str | int) -> dict | None:
    """读取单个 fixture 的缓存条目。不存在返回 None。"""
    p = _event_path(sport_key, fixture_id)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(
            f"[oddsapi_event_store] read failed sport={sport_key} fid={fixture_id}: {exc}"
        )
        return None


def list_events(sport_key: str) -> list[dict]:
    """列出某 sport 当前所有有效（未归档）事件。"""
    d = _sport_dir(sport_key)
    if not d.exists():
        return []
    out: list[dict] = []
    for p in d.iterdir():
        if not p.is_file() or not p.name.endswith(".json"):
            continue
        try:
            with p.open("r", encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            continue
    return out


def save_fixture_meta(
    sport_key: str,
    fixture_id: str | int,
    fixture: dict,
    scheduled_iso: str | None = None,
) -> None:
    """把 /fixtures 里某条 fixture 的 metadata 写入缓存。
    若已存在 odds，保留 odds 字段；只更新 fixture 与 scheduled_iso。"""
    p = _event_path(sport_key, fixture_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with _lock_for(sport_key):
        existing = get_event(sport_key, fixture_id) or {}
        existing.update(
            {
                "sport_key": sport_key,
                "fixture_id": str(fixture_id),
                "fixture": fixture,
                "fetched_at": now,
            }
        )
        if scheduled_iso:
            existing["scheduled_iso"] = scheduled_iso
        try:
            tmp = p.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception as exc:
            logger.warning(
                f"[oddsapi_event_store] write fixture failed sport={sport_key} fid={fixture_id}: {exc}"
            )


def save_odds(
    sport_key: str,
    fixture_id: str | int,
    odds: dict | None,
    bookmakers: dict | None,
) -> None:
    """把 /fixtures/odds/main 抓回的某条 fixture 的 odds + bookmakers 写入缓存。"""
    p = _event_path(sport_key, fixture_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with _lock_for(sport_key):
        existing = get_event(sport_key, fixture_id) or {
            "sport_key": sport_key,
            "fixture_id": str(fixture_id),
            "fixture": {},
            "fetched_at": now,
        }
        existing["odds"] = odds or {}
        existing["bookmakers"] = bookmakers or {}
        existing["odds_fetched_at"] = now
        try:
            tmp = p.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception as exc:
            logger.warning(
                f"[oddsapi_event_store] write odds failed sport={sport_key} fid={fixture_id}: {exc}"
            )


def should_fetch_odds(
    sport_key: str,
    fixture_id: str | int,
    scheduled_iso: str | None = None,
    *,
    near_window_hours: float = 48.0,
    refresh_interval_hours: float = 6.0,
) -> bool:
    """决定是否需要为这个 fixture 调用 /fixtures/odds/main：

    - 未缓存 odds → True（首次必须拉）
    - 未来 48h 内开赛 + 距离上次刷新 > 6h → True
    - 已开始 / 已结束 → False（应被归档）
    - 远期赛事（>48h）+ 已有 odds → False（赔率波动小，节省配额）
    """
    now = time.time()
    ev = get_event(sport_key, fixture_id)
    if ev is None or not ev.get("odds"):
        return True
    last = float(ev.get("odds_fetched_at") or 0)
    sched_ts = _parse_iso(scheduled_iso or ev.get("scheduled_iso"))
    if sched_ts is None:
        # 时间未知 → 走保守 6h 间隔
        return (now - last) > refresh_interval_hours * 3600
    if sched_ts < now - 6 * 3600:
        # 已经开始 6h 前 → 不再刷新
        return False
    if sched_ts <= now + near_window_hours * 3600:
        # 临场区间，按 refresh_interval 节奏刷
        return (now - last) > refresh_interval_hours * 3600
    # 远期赛事，已经有 odds → 跳过
    return False


def archive_past_events(now_ts: float | None = None, grace_hours: float = 24.0) -> int:
    """把已经结束（开赛 + grace_hours 之前）的事件归档到 _archive/。
    返回归档的文件数。"""
    if now_ts is None:
        now_ts = time.time()
    cutoff = now_ts - grace_hours * 3600
    n = 0
    if not _STORE_DIR.exists():
        return 0
    for sport_dir in _STORE_DIR.iterdir():
        if not sport_dir.is_dir() or sport_dir.name.startswith("_"):
            continue
        for p in list(sport_dir.iterdir()):
            if not p.is_file() or not p.name.endswith(".json"):
                continue
            try:
                with p.open("r", encoding="utf-8") as f:
                    ev = json.load(f)
                sched_ts = _parse_iso(ev.get("scheduled_iso"))
                if sched_ts is None:
                    # 如果文件 14 天没动过且无时间戳，归档
                    if (now_ts - p.stat().st_mtime) > 14 * 86400:
                        sched_ts = -1
                    else:
                        continue
                if sched_ts < cutoff:
                    arc = _archive_path(sport_dir.name, p.stem)
                    arc.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(p, arc)
                    n += 1
            except Exception as exc:
                logger.warning(f"[oddsapi_event_store] archive scan {p}: {exc}")
                continue
    if n:
        logger.info(f"[oddsapi_event_store] archived {n} past events")
    return n


def attach_clv(
    sport_key: str,
    fixture_id: str | int,
    clv: dict | None,
) -> None:
    """T873 — 把 /fixtures/odds/clv 返回的 CLV 数据 attach 到 event 缓存。

    与 odds 共用同一个事件 JSON，新增 `clv` + `clv_fetched_at` 字段；
    若 fixture 未存在，先建一个最小骨架。
    """
    if clv is None:
        return
    p = _event_path(sport_key, fixture_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with _lock_for(sport_key):
        existing = get_event(sport_key, fixture_id) or {
            "sport_key": sport_key,
            "fixture_id": str(fixture_id),
            "fixture": {},
            "fetched_at": now,
        }
        existing["clv"] = clv
        existing["clv_fetched_at"] = now
        try:
            tmp = p.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception as exc:
            logger.warning(
                f"[oddsapi_event_store] write clv failed sport={sport_key} fid={fixture_id}: {exc}"
            )


def append_history(
    sport_key: str,
    fixture_id: str | int,
    snapshot: dict | None,
) -> None:
    """T873 — 把 /fixtures/odds/historical 拉到的赔率快照追加到 history 数组。

    history 是 list[dict]，每项形如 {"ts": <unix>, "snapshot": {...}}；
    上限默认保留 50 条（覆盖 ~24h 半小时粒度即可）。
    """
    if snapshot is None:
        return
    p = _event_path(sport_key, fixture_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with _lock_for(sport_key):
        existing = get_event(sport_key, fixture_id) or {
            "sport_key": sport_key,
            "fixture_id": str(fixture_id),
            "fixture": {},
            "fetched_at": now,
        }
        history: list = existing.get("history") or []
        if not isinstance(history, list):
            history = []
        history.append({"ts": now, "snapshot": snapshot})
        if len(history) > 50:
            history = history[-50:]
        existing["history"] = history
        existing["history_fetched_at"] = now
        try:
            tmp = p.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception as exc:
            logger.warning(
                f"[oddsapi_event_store] write history failed sport={sport_key} fid={fixture_id}: {exc}"
            )


def attach_settlement(
    sport_key: str,
    fixture_id: str | int,
    settlement: dict | None,
) -> None:
    """T873 — 把 /fixtures/settlement 拉到的真实结算结果（含 winner）写入。

    需要支持「已归档事件」补结算 —— 若 active 路径找不到，尝试 archive 路径。
    """
    if settlement is None:
        return
    now = time.time()
    p_active = _event_path(sport_key, fixture_id)
    p_archive = _archive_path(sport_key, fixture_id)
    target_path = p_active if p_active.exists() else (p_archive if p_archive.exists() else p_active)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(sport_key):
        existing: dict = {}
        for cand in (p_active, p_archive):
            if cand.exists():
                try:
                    with cand.open("r", encoding="utf-8") as f:
                        existing = json.load(f) or {}
                    target_path = cand
                    break
                except Exception:
                    continue
        if not existing:
            existing = {
                "sport_key": sport_key,
                "fixture_id": str(fixture_id),
                "fixture": {},
                "fetched_at": now,
            }
        existing["settlement"] = settlement
        existing["settlement_fetched_at"] = now
        try:
            tmp = target_path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)
            os.replace(tmp, target_path)
        except Exception as exc:
            logger.warning(
                f"[oddsapi_event_store] write settlement failed sport={sport_key} fid={fixture_id}: {exc}"
            )


def attach_media(
    sport_key: str,
    fixture_id: str | int,
    media: dict | None,
) -> None:
    """T873 — 把 /fixtures/media 拉到的媒体（视频/图片链接）attach 到事件。"""
    if media is None:
        return
    p = _event_path(sport_key, fixture_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with _lock_for(sport_key):
        existing = get_event(sport_key, fixture_id) or {
            "sport_key": sport_key,
            "fixture_id": str(fixture_id),
            "fixture": {},
            "fetched_at": now,
        }
        existing["media"] = media
        existing["media_fetched_at"] = now
        try:
            tmp = p.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception as exc:
            logger.warning(
                f"[oddsapi_event_store] write media failed sport={sport_key} fid={fixture_id}: {exc}"
            )


def list_archive(sport_key: str | None = None) -> list[dict]:
    """T873 — 列出归档事件（用于结算回填扫描）。

    若 sport_key=None，返回所有 sport 的归档；否则只返回该 sport。
    每项含 sport_key/fixture_id/scheduled_iso/has_settlement 关键字段。
    """
    out: list[dict] = []
    if not _ARCHIVE_DIR.exists():
        return out
    sport_dirs = (
        [_ARCHIVE_DIR / sport_key]
        if sport_key
        else [d for d in _ARCHIVE_DIR.iterdir() if d.is_dir()]
    )
    for sd in sport_dirs:
        if not sd.exists() or not sd.is_dir():
            continue
        for p in sd.iterdir():
            if not p.is_file() or not p.name.endswith(".json"):
                continue
            try:
                with p.open("r", encoding="utf-8") as f:
                    ev = json.load(f)
                out.append(
                    {
                        "sport_key": sd.name,
                        "fixture_id": ev.get("fixture_id"),
                        "scheduled_iso": ev.get("scheduled_iso"),
                        "has_settlement": bool(ev.get("settlement")),
                        "_path": str(p),
                    }
                )
            except Exception:
                continue
    return out


def stats() -> dict[str, Any]:
    """返回缓存统计：每 sport 的 active count / archive count / total bytes。"""
    out: dict[str, Any] = {"sports": {}, "archive": {}, "total_active": 0, "total_archived": 0}
    if _STORE_DIR.exists():
        for sport_dir in _STORE_DIR.iterdir():
            if not sport_dir.is_dir() or sport_dir.name.startswith("_"):
                continue
            files = [p for p in sport_dir.iterdir() if p.suffix == ".json"]
            out["sports"][sport_dir.name] = len(files)
            out["total_active"] += len(files)
    if _ARCHIVE_DIR.exists():
        for sport_dir in _ARCHIVE_DIR.iterdir():
            if not sport_dir.is_dir():
                continue
            files = [p for p in sport_dir.iterdir() if p.suffix == ".json"]
            out["archive"][sport_dir.name] = len(files)
            out["total_archived"] += len(files)
    return out
