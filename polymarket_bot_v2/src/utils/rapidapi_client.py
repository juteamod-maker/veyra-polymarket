"""
rapidapi_client.py — RapidAPI 统一请求客户端

职责：
  封装对 RapidAPI 平台上各体育 API 的 HTTP 请求。
  单一 `RAPIDAPI_KEY` 环境变量，对应 x-rapidapi-key 请求头。

  支持的 API 主机（通过 config sports_fetcher.rapidapi_hosts 配置）：
    odds     → odds-api1.p.rapidapi.com     (The Odds API)
    football → api-football-v1.p.rapidapi.com (API-Football v3)
    nba      → api-nba-v1.p.rapidapi.com    (API-NBA)

用途：
  - odds_fetcher.py: 获取多家博彩公司实时赔率
  - sports_fetcher.py: 获取伤病名单、上场阵容
"""

import logging
import os
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

import requests
from src.utils.config_loader import get

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 内存缓存：{cache_key: (timestamp, response_data)}
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, dict | list]] = {}

# ---------------------------------------------------------------------------
# 默认 API 主机（可被 config 覆盖）
# ---------------------------------------------------------------------------
_DEFAULT_HOSTS: dict[str, str] = {
    "odds": "odds-api1.p.rapidapi.com",
    "football": "api-football-v1.p.rapidapi.com",
    "nba": "api-nba-v1.p.rapidapi.com",
}

_TIMEOUT_DEFAULT = 10
_CACHE_TTL_DEFAULT = 1800  # 30 分钟
_BUDGET_LOCK = Lock()
_budget_state = {
    "day": "",
    "total_calls": 0,
    "api_calls": {},
    "next_allowed_at": 0.0,
    "cooldown_until": 0.0,
}

# T869: 持久化预算计数（避免进程重启绕过日配额）+ 备用 key 月度计数
_BUDGET_STATE_PATH = os.environ.get(
    "RAPIDAPI_BUDGET_STATE_PATH",
    "/opt/polymarket_bot_v2/data/rapidapi_budget_state.json",
)
_BACKUP_STATE_PATH = os.environ.get(
    "RAPIDAPI_BACKUP_STATE_PATH",
    "/opt/polymarket_bot_v2/data/rapidapi_backup_state.json",
)
_backup_state = {
    "month": "",
    "total_calls": 0,
}
_BUDGET_LOADED = False


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _month_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _load_budget_state_locked() -> None:
    """T869：进程启动时从磁盘恢复当日计数。"""
    global _BUDGET_LOADED
    if _BUDGET_LOADED:
        return
    try:
        import json as _json

        if os.path.exists(_BUDGET_STATE_PATH):
            with open(_BUDGET_STATE_PATH, "r", encoding="utf-8") as f:
                d = _json.load(f) or {}
            today = _today_utc()
            if d.get("day") == today:
                _budget_state["day"] = today
                _budget_state["total_calls"] = int(d.get("total_calls") or 0)
                _budget_state["api_calls"] = dict(d.get("api_calls") or {})
                _budget_state["cooldown_until"] = float(d.get("cooldown_until") or 0)
        if os.path.exists(_BACKUP_STATE_PATH):
            with open(_BACKUP_STATE_PATH, "r", encoding="utf-8") as f:
                d = _json.load(f) or {}
            month = _month_utc()
            if d.get("month") == month:
                _backup_state["month"] = month
                _backup_state["total_calls"] = int(d.get("total_calls") or 0)
    except Exception as _exc:
        logger.warning(f"[RapidAPI budget] state load failed: {_exc}")
    _BUDGET_LOADED = True


def _persist_budget_state_locked() -> None:
    try:
        import json as _json

        os.makedirs(os.path.dirname(_BUDGET_STATE_PATH), exist_ok=True)
        tmp = _BUDGET_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(
                {
                    "day": _budget_state["day"],
                    "total_calls": int(_budget_state["total_calls"]),
                    "api_calls": dict(_budget_state["api_calls"]),
                    "cooldown_until": float(_budget_state["cooldown_until"]),
                    "saved_at": time.time(),
                },
                f,
            )
        os.replace(tmp, _BUDGET_STATE_PATH)
    except Exception as _exc:
        logger.debug(f"[RapidAPI budget] state save failed: {_exc}")


def _persist_backup_state_locked() -> None:
    try:
        import json as _json

        os.makedirs(os.path.dirname(_BACKUP_STATE_PATH), exist_ok=True)
        tmp = _BACKUP_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(
                {
                    "month": _backup_state["month"],
                    "total_calls": int(_backup_state["total_calls"]),
                    "saved_at": time.time(),
                },
                f,
            )
        os.replace(tmp, _BACKUP_STATE_PATH)
    except Exception as _exc:
        logger.debug(f"[RapidAPI backup] state save failed: {_exc}")


def _budget_cfg() -> dict:
    cfg = get("sports_fetcher.rapidapi_budget") or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "daily_total_limit": int(cfg.get("daily_total_limit") or 400),
        "daily_per_api_limit": int(cfg.get("daily_per_api_limit") or 400),
        "min_interval_sec": float(cfg.get("min_interval_sec") or 2.0),
        "cooldown_on_429_sec": int(cfg.get("cooldown_on_429_sec") or 600),
        "backup_monthly_limit": int(cfg.get("backup_monthly_limit") or 230),
    }


def _budget_guard_before(api_name: str) -> tuple[bool, str]:
    cfg = _budget_cfg()
    if not cfg["enabled"]:
        return True, "ok"
    now = time.time()
    with _BUDGET_LOCK:
        _load_budget_state_locked()
        day = _today_utc()
        if _budget_state["day"] != day:
            _budget_state["day"] = day
            _budget_state["total_calls"] = 0
            _budget_state["api_calls"] = {}
            _budget_state["next_allowed_at"] = 0.0
            _budget_state["cooldown_until"] = 0.0
            _persist_budget_state_locked()

        if now < float(_budget_state["cooldown_until"]):
            return False, "cooldown"
        if _budget_state["total_calls"] >= cfg["daily_total_limit"]:
            return False, "daily_total_limit"

        per_api = int((_budget_state["api_calls"] or {}).get(api_name, 0))
        if per_api >= cfg["daily_per_api_limit"]:
            return False, "daily_per_api_limit"

        if now < float(_budget_state["next_allowed_at"]):
            return False, "min_interval"

        _budget_state["total_calls"] += 1
        _budget_state["api_calls"][api_name] = per_api + 1
        _budget_state["next_allowed_at"] = now + float(cfg["min_interval_sec"])
        _persist_budget_state_locked()
    return True, "ok"


def _backup_guard_before() -> tuple[bool, str]:
    """T869: 备用 key 月度配额守门（默认 230 次/月，留 20 次余量）。"""
    cfg = _budget_cfg()
    with _BUDGET_LOCK:
        _load_budget_state_locked()
        month = _month_utc()
        if _backup_state["month"] != month:
            _backup_state["month"] = month
            _backup_state["total_calls"] = 0
            _persist_backup_state_locked()
        if _backup_state["total_calls"] >= cfg["backup_monthly_limit"]:
            return False, "backup_monthly_limit"
        _backup_state["total_calls"] += 1
        _persist_backup_state_locked()
    return True, "ok"


def _budget_mark_429() -> None:
    cfg = _budget_cfg()
    with _BUDGET_LOCK:
        _budget_state["cooldown_until"] = max(
            float(_budget_state["cooldown_until"]),
            time.time() + int(cfg["cooldown_on_429_sec"]),
        )
        _persist_budget_state_locked()


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------


def is_available() -> bool:
    """检查 RAPIDAPI_KEY 或 RAPIDAPI_KEY_BACKUP 是否已配置。"""
    return bool(_primary_key()) or bool(_backup_key())


def _primary_key() -> str:
    return os.environ.get("RAPIDAPI_KEY", "").strip()


def _backup_key() -> str:
    """T869：备用 RapidAPI key（独立账号），仅在主 key 被阻塞/429 时使用。
    免费版仅 250 次/月，须严格守门。
    """
    return os.environ.get("RAPIDAPI_KEY_BACKUP", "").strip()


def is_primary_blocked() -> bool:
    """T869：主 key 当前是否处于阻塞态（cooldown 或日配额耗尽）。"""
    cfg = _budget_cfg()
    if not cfg["enabled"]:
        return False
    with _BUDGET_LOCK:
        _load_budget_state_locked()
        now = time.time()
        if now < float(_budget_state["cooldown_until"]):
            return True
        if _budget_state["day"] == _today_utc() and (
            int(_budget_state["total_calls"]) >= cfg["daily_total_limit"]
        ):
            return True
    return False


def budget_snapshot() -> dict:
    """T869：暴露当前预算状态，便于诊断。"""
    cfg = _budget_cfg()
    with _BUDGET_LOCK:
        _load_budget_state_locked()
        return {
            "enabled": cfg["enabled"],
            "daily_total_limit": cfg["daily_total_limit"],
            "daily_per_api_limit": cfg["daily_per_api_limit"],
            "backup_monthly_limit": cfg["backup_monthly_limit"],
            "primary_today_calls": int(_budget_state["total_calls"]),
            "primary_per_api": dict(_budget_state["api_calls"]),
            "primary_cooldown_until": float(_budget_state["cooldown_until"]),
            "backup_month_calls": int(_backup_state["total_calls"]),
            "backup_key_set": bool(_backup_key()),
            "primary_key_set": bool(_primary_key()),
        }


def get_host(api_name: str) -> str:
    """
    获取指定 API 的 RapidAPI 主机名。
    优先读 config sports_fetcher.rapidapi_hosts，否则使用默认值。

    Args:
        api_name: "odds" | "football" | "nba"
    """
    cfg = get("sports_fetcher.rapidapi_hosts") or {}
    return cfg.get(api_name) or _DEFAULT_HOSTS.get(api_name, "")


def fetch(
    api_name: str,
    path: str,
    params: Optional[dict] = None,
    timeout: Optional[int] = None,
    cache_ttl: Optional[int] = None,
) -> Optional[dict | list]:
    """
    向 RapidAPI 指定 API 发起 GET 请求，带内存缓存。

    Args:
        api_name: "odds" | "football" | "nba"（对应不同 host）
        path:     URL 路径，例如 "/v4/sports/basketball_nba/odds"
        params:   查询参数字典
        timeout:  超时（秒），默认读 config sports_fetcher.request_timeout_sec
        cache_ttl: 缓存 TTL（秒），默认读 config sports_fetcher.cache_ttl_sec

    Returns:
        解析后的 JSON（dict 或 list），失败时返回 None
    """
    if not _primary_key() and not _backup_key():
        logger.debug("[RapidAPI] 无 RAPIDAPI_KEY，跳过请求")
        return None

    host = get_host(api_name)
    if not host:
        logger.warning(f"[RapidAPI] 未知 API 名称: {api_name}")
        return None

    _timeout = timeout or int(
        get("sports_fetcher.request_timeout_sec") or _TIMEOUT_DEFAULT
    )
    _ttl = cache_ttl or int(get("sports_fetcher.cache_ttl_sec") or _CACHE_TTL_DEFAULT)

    # 缓存 key = host + path + sorted params
    _params = params or {}
    cache_key = f"{host}{path}?{sorted(_params.items())}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < _ttl:
        logger.debug(f"[RapidAPI] 缓存命中: {api_name}{path}")
        return cached[1]

    url = f"https://{host}{path}"
    headers = {
        "x-rapidapi-key": _primary_key() or _backup_key(),
        "x-rapidapi-host": host,
    }

    allowed, reason = _budget_guard_before(api_name)
    using_backup = False
    if not allowed:
        # T869: 主 key 阻塞 → 尝试备用 key（受月度配额限制）
        if _backup_key():
            ok_b, reason_b = _backup_guard_before()
            if ok_b:
                using_backup = True
                headers["x-rapidapi-key"] = _backup_key()
                logger.info(
                    f"[RapidAPI] primary blocked ({reason}) → using BACKUP key for api={api_name} path={path}"
                )
            else:
                stale = _cache.get(cache_key)
                if stale:
                    logger.info(
                        f"[RapidAPI budget] primary={reason} backup={reason_b} api={api_name} path={path} — serving stale cache (age={int(time.time()-stale[0])}s)"
                    )
                    return stale[1]
                logger.warning(
                    f"[RapidAPI budget] blocked api={api_name} path={path} primary={reason} backup={reason_b} — no stale cache available"
                )
                return None
        else:
            stale = _cache.get(cache_key)
            if stale:
                logger.info(
                    f"[RapidAPI budget] blocked api={api_name} path={path} reason={reason} — serving stale cache (age={int(time.time()-stale[0])}s)"
                )
                return stale[1]
            logger.warning(
                f"[RapidAPI budget] blocked api={api_name} path={path} reason={reason} — no stale cache available"
            )
            return None

    try:
        resp = requests.get(url, headers=headers, params=_params, timeout=_timeout)

        if resp.status_code == 429:
            # 429 限流：等待后重试一次
            _budget_mark_429()
            retry_after = int(resp.headers.get("Retry-After", 3))
            retry_after = min(retry_after, 10)  # 上限 10 秒
            logger.info(
                f"[RapidAPI] {api_name} 限流 429（{path}），"
                f"等待 {retry_after}s 后重试"
            )
            time.sleep(retry_after)
            resp = requests.get(url, headers=headers, params=_params, timeout=_timeout)
            if resp.status_code == 429:
                _budget_mark_429()
                # T685+: Return stale cache on 429 if available
                stale = _cache.get(cache_key)
                if stale:
                    logger.warning(
                        f"[RapidAPI] {api_name} 限流 429（{path}）——重试仍失败，返回缓存数据"
                    )
                    return stale[1]
                logger.warning(
                    f"[RapidAPI] {api_name} 限流 429（{path}）" "——重试仍失败，本次跳过"
                )
                return None

        if resp.status_code == 403:
            logger.warning(
                f"[RapidAPI] {api_name} 无权限 403（{path}）——请检查订阅计划"
            )
            return None

        # T289: 5xx 服务器错误自动重试一次（502/503/504）
        if resp.status_code in (502, 503, 504):
            logger.info(
                f"[RapidAPI] {api_name}{path} 服务端错误 {resp.status_code}，"
                f"等待 3s 后重试"
            )
            time.sleep(3)
            resp = requests.get(url, headers=headers, params=_params, timeout=_timeout)
            if resp.status_code in (502, 503, 504):
                logger.warning(
                    f"[RapidAPI] {api_name}{path} 重试仍返回 {resp.status_code}，跳过"
                )
                return None

        resp.raise_for_status()
        data = resp.json()
        _cache[cache_key] = (time.time(), data)
        logger.debug(f"[RapidAPI] 请求成功: {api_name}{path} → {type(data).__name__}")
        return data

    except requests.exceptions.Timeout:
        logger.warning(f"[RapidAPI] {api_name}{path} 请求超时（>{_timeout}s）")
    except requests.exceptions.RequestException as e:
        logger.warning(f"[RapidAPI] {api_name}{path} 请求失败: {e}")
    except Exception as e:
        logger.warning(f"[RapidAPI] {api_name}{path} 解析失败: {e}")

    return None


def fetch_with_meta(
    api_name: str,
    path: str,
    params: Optional[dict] = None,
    timeout: Optional[int] = None,
    cache_ttl: Optional[int] = None,
) -> dict:
    """
    与 fetch() 相同行为，但返回 {ok, data, status, reason, http_status, host} 元数据，
    便于上层把上游具体失败原因显式回传给前端（例如 "not_subscribed" / "rate_limited"）。

    reason 枚举:
      ok                – 成功
      key_missing       – 未配置 RAPIDAPI_KEY
      unknown_api       – 未知 api_name
      not_subscribed    – 403：当前 RapidAPI 订阅未包含该 API
      rate_limited      – 429：触发限流（含一次重试后仍失败）
      upstream_5xx      – 5xx：上游服务端错误（含一次重试后仍失败）
      timeout           – 请求超时
      network_error     – 其它网络错误
      bad_response      – JSON 解析失败 / 上游返回非预期结构
    """
    rapidapi_key = _primary_key()
    if not rapidapi_key and not _backup_key():
        return {
            "ok": False,
            "data": None,
            "reason": "key_missing",
            "http_status": 0,
            "host": "",
        }

    host = get_host(api_name)
    if not host:
        return {
            "ok": False,
            "data": None,
            "reason": "unknown_api",
            "http_status": 0,
            "host": "",
        }

    _timeout = timeout or int(
        get("sports_fetcher.request_timeout_sec") or _TIMEOUT_DEFAULT
    )
    _ttl = cache_ttl or int(get("sports_fetcher.cache_ttl_sec") or _CACHE_TTL_DEFAULT)
    _params = params or {}
    cache_key = f"{host}{path}?{sorted(_params.items())}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < _ttl:
        return {
            "ok": True,
            "data": cached[1],
            "reason": "ok",
            "http_status": 200,
            "host": host,
            "cached": True,
        }

    url = f"https://{host}{path}"
    headers = {"x-rapidapi-key": rapidapi_key or _backup_key(), "x-rapidapi-host": host}

    def _stale_meta(reason_tag: str, http_status: int = 0) -> dict:
        stale = _cache.get(cache_key)
        if stale:
            age = int(time.time() - stale[0])
            logger.info(
                f"[RapidAPI meta] api={api_name} path={path} reason={reason_tag} "
                f"— serving stale cache (age={age}s)"
            )
            return {
                "ok": True,
                "data": stale[1],
                "reason": reason_tag,
                "http_status": http_status,
                "host": host,
                "cached": True,
                "stale": True,
            }
        return {
            "ok": False,
            "data": None,
            "reason": reason_tag,
            "http_status": http_status,
            "host": host,
        }

    allowed, reason = _budget_guard_before(api_name)
    using_backup = False
    if not allowed:
        # T869: 主 key 阻塞 → 尝试备用 key
        if _backup_key():
            ok_b, reason_b = _backup_guard_before()
            if ok_b:
                using_backup = True
                headers["x-rapidapi-key"] = _backup_key()
                logger.info(
                    f"[RapidAPI meta] primary blocked ({reason}) → using BACKUP key for api={api_name} path={path}"
                )
            else:
                logger.warning(
                    f"[RapidAPI budget] blocked api={api_name} path={path} primary={reason} backup={reason_b}"
                )
                return _stale_meta(f"{reason}/backup_{reason_b}", 0)
        else:
            logger.warning(
                f"[RapidAPI budget] blocked api={api_name} path={path} reason={reason}"
            )
            return _stale_meta(reason, 0)

    def _do_get():
        return requests.get(url, headers=headers, params=_params, timeout=_timeout)

    try:
        resp = _do_get()

        if resp.status_code == 429:
            _budget_mark_429()
            retry_after = min(int(resp.headers.get("Retry-After", 3) or 3), 10)
            time.sleep(retry_after)
            resp = _do_get()
            if resp.status_code == 429:
                _budget_mark_429()
                logger.warning(
                    f"[RapidAPI] {api_name}{path} 429 rate-limited (retried)"
                )
                return _stale_meta("rate_limited", 429)

        if resp.status_code == 403:
            logger.warning(f"[RapidAPI] {api_name}{path} 403 not subscribed")
            return {
                "ok": False,
                "data": None,
                "reason": "not_subscribed",
                "http_status": 403,
                "host": host,
            }

        if resp.status_code in (502, 503, 504):
            time.sleep(3)
            resp = _do_get()
            if resp.status_code in (502, 503, 504):
                return _stale_meta("upstream_5xx", int(resp.status_code))

        resp.raise_for_status()
        data = resp.json()
        _cache[cache_key] = (time.time(), data)
        return {
            "ok": True,
            "data": data,
            "reason": "ok",
            "http_status": 200,
            "host": host,
        }

    except requests.exceptions.Timeout:
        return _stale_meta("timeout", 0)
    except requests.exceptions.RequestException as e:
        logger.warning(f"[RapidAPI] {api_name}{path} request failed: {e}")
        return _stale_meta("network_error", 0)
    except Exception as e:
        logger.warning(f"[RapidAPI] {api_name}{path} parse failed: {e}")
        return {
            "ok": False,
            "data": None,
            "reason": "bad_response",
            "http_status": 0,
            "host": host,
        }


def clear_cache() -> None:
    """清空所有缓存（供测试使用）。"""
    _cache.clear()
