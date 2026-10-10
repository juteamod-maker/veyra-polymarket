from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent.parent
CONFIG_PATH = ROOT / "config" / "copytrader_v2.json"
_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {}
_LAST_MTIME: float = 0.0

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "scan_interval_seconds": 60,
    "pool_refresh_interval_seconds": 600,
    "hard_max_bet_usdc": 25.0,
    "trade_mode": "taker",
    "trade_mode_options": ["taker", "maker"],
    "trade_mode_locked": True,
    "follow_multiplier": 1,
    "min_order_size_buffer_shares": 1,
    "max_concurrent_follows_per_user": 5,
    "min_score": 60.0,
    "max_price_filter": 0.85,
    "min_ev_filter": 0.002,
    "trade_lookback_minutes": 30,
    "exclude_bots": True,
    "follow_sell_enabled": True,
    "data_source": "polymarket_official_api",
}


def _ensure_file() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        return
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_config(force: bool = False) -> dict[str, Any]:
    global _CACHE, _LAST_MTIME
    with _LOCK:
        _ensure_file()
        mtime = CONFIG_PATH.stat().st_mtime
        if force or mtime != _LAST_MTIME:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cfg = deepcopy(DEFAULT_CONFIG)
            if isinstance(raw, dict):
                cfg.update(raw)
            _CACHE = cfg
            _LAST_MTIME = mtime
        return deepcopy(_CACHE)


def get(key_path: str, default=None):
    cfg = load_config()
    val: Any = cfg
    for key in str(key_path or "").split("."):
        if isinstance(val, dict) and key in val:
            val = val[key]
        else:
            return default
    return val


def update_config(updates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cfg = load_config(force=True)
    defaults = deepcopy(DEFAULT_CONFIG)
    applied: dict[str, dict[str, Any]] = {}
    for key_path, new_val in updates.items():
        keys = str(key_path or "").split(".")
        target: Any = cfg
        default_target: Any = defaults
        for key in keys[:-1]:
            if isinstance(target, dict) and key in target:
                target = target[key]
                if isinstance(default_target, dict) and key in default_target:
                    default_target = default_target[key]
                else:
                    default_target = None
            else:
                raise KeyError(key_path)
        leaf_key = keys[-1]
        if not isinstance(target, dict):
            raise KeyError(key_path)

        if leaf_key in target:
            old_val = target[leaf_key]
        elif isinstance(default_target, dict) and leaf_key in default_target:
            old_val = default_target[leaf_key]
        else:
            raise KeyError(key_path)

        if old_val is not None and not isinstance(new_val, type(old_val)):
            if isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
                new_val = type(old_val)(new_val)
            else:
                raise TypeError(key_path)
        target[leaf_key] = new_val
        applied[key_path] = {"old": old_val, "new": new_val}
    with _LOCK:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        global _CACHE, _LAST_MTIME
        _CACHE = deepcopy(cfg)
        _LAST_MTIME = CONFIG_PATH.stat().st_mtime
    return applied