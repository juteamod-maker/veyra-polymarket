"""T873 — OddsPapi 参考元数据缓存（meta store）

业务铁律（用户原文 / Section 8 backlog）：
  /participants /bookmakers /markets /sports /tournaments /seasons /currencies /venues
  这八类参考数据「一次性批量」拉取，落地到 data/oddsapi_meta/<type>.json。
  常态读用本地 JSON，远低于 10K/月 RapidAPI 配额。

特性
----
- 每种 type 一个 JSON 文件：{"fetched_at": ts, "data": [...]}
- TTL 默认 7 天（/bookmakers /markets /sports /currencies / venues 等）
- /tournaments 与 /seasons 按 sportId 分文件：tournaments_<sid>.json
- /participants 按 tournamentId 分文件：participants_<tid>.json
- 可选 force=True 立即刷新（运维 admin 接口使用）

不进 RAM 缓存——文件极小（<5MB total），直接读盘即可。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_REPO_ROOT_CANDIDATES = [
    _THIS.parents[2],
    Path("/opt/polymarket_bot_v2"),
]
_DATA_ROOT: Path | None = None
for _c in _REPO_ROOT_CANDIDATES:
    if (_c / "data").exists():
        _DATA_ROOT = _c / "data"
        break
if _DATA_ROOT is None:
    _DATA_ROOT = _THIS.parents[2] / "data"
_META_DIR = _DATA_ROOT / "oddsapi_meta"

# 各 type 的默认 TTL（秒）
_DEFAULT_TTL: dict[str, int] = {
    "sports": 7 * 86400,
    "bookmakers": 7 * 86400,
    "markets": 7 * 86400,
    "currencies": 30 * 86400,
    "venues": 7 * 86400,
    "tournaments": 7 * 86400,
    "seasons": 7 * 86400,
    "participants": 3 * 86400,
}


def _path(name: str) -> Path:
    safe = name.replace("/", "_").replace("..", "_")
    return _META_DIR / f"{safe}.json"


def _read(name: str) -> dict | None:
    p = _path(name)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning(f"[oddsapi_meta_store] read failed name={name}: {exc}")
    return None


def _write(name: str, payload: list | dict) -> None:
    _META_DIR.mkdir(parents=True, exist_ok=True)
    out = {"fetched_at": time.time(), "data": payload}
    p = _path(name)
    tmp = p.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception as exc:
        logger.warning(f"[oddsapi_meta_store] write failed name={name}: {exc}")


def _is_fresh(name: str, ttl: int) -> bool:
    p = _path(name)
    if not p.exists():
        return False
    age = time.time() - p.stat().st_mtime
    return age < ttl


def get_cached(name: str) -> Any:
    """读取缓存（如果存在），返回 data 字段（list/dict）；不存在返回 None。"""
    rec = _read(name)
    if rec is None:
        return None
    return rec.get("data")


def get_or_fetch(
    name: str,
    fetch_fn,
    *,
    ttl: int | None = None,
    force: bool = False,
) -> Any:
    """通用入口：缓存未到期就读盘；否则调 fetch_fn() 并落盘。

    fetch_fn() 应返回 list/dict（None 视作失败，保持旧缓存不动）。
    """
    if ttl is None:
        ttl = _DEFAULT_TTL.get(name.split("_")[0], 7 * 86400)
    if not force and _is_fresh(name, ttl):
        return get_cached(name)
    try:
        data = fetch_fn()
    except Exception as exc:
        logger.warning(f"[oddsapi_meta_store] fetch_fn raised name={name}: {exc}")
        data = None
    if data is None:
        # 拉取失败 → 退回已有缓存（即使过期）
        return get_cached(name)
    _write(name, data)
    return data


def stats() -> dict[str, Any]:
    """列出所有缓存文件，包含大小 + 最后更新时间。"""
    out: dict[str, Any] = {"items": {}}
    if not _META_DIR.exists():
        return out
    for p in _META_DIR.iterdir():
        if not p.is_file() or not p.name.endswith(".json"):
            continue
        try:
            st = p.stat()
            data = None
            try:
                with p.open("r", encoding="utf-8") as f:
                    blob = json.load(f)
                if isinstance(blob, dict):
                    raw = blob.get("data")
                    if isinstance(raw, list):
                        data = len(raw)
                    elif isinstance(raw, dict):
                        data = len(raw)
            except Exception:
                data = None
            out["items"][p.stem] = {
                "size_bytes": st.st_size,
                "updated_at": int(st.st_mtime),
                "count": data,
            }
        except Exception:
            continue
    return out
