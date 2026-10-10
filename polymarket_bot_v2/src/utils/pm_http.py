"""T510 Wave 7E — Unified Polymarket HTTP client.

Centralizes:
- Single requests.Session with HTTPAdapter (pool + light retry)
- Standard User-Agent string per PM convention
- One place to swap/mirror official base URLs in the future

Usage::

    from src.utils.pm_http import pm_get, GAMMA, DATA, CLOB
    payload = pm_get(GAMMA, "/events", params={"limit": 20}, timeout=4)

The helper is intentionally narrow: returns the parsed JSON or `None` on
any error. Callers are responsible for shape validation.
"""

from __future__ import annotations

from typing import Any, Mapping
import threading

import requests
from requests.adapters import HTTPAdapter

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

USER_AGENT = "VeYra-Bot/2.0 (+https://veyra.trade)"

_SESSION: requests.Session | None = None
_SESSION_LOCK = threading.Lock()


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            return _SESSION
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=32, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        })
        _SESSION = s
        return s


def pm_get(
    host: str,
    path: str,
    params: Mapping[str, Any] | None = None,
    timeout: float = 5.0,
) -> Any | None:
    """GET helper that never raises. Returns parsed JSON or None on error."""
    if not host or not path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    url = host.rstrip("/") + path
    try:
        resp = _session().get(url, params=params or None, timeout=timeout)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def pm_session() -> requests.Session:
    """Return the shared PM session (UA, pool, default headers).

    Use for direct calls that need streaming or non-JSON responses,
    e.g. ``pm_session().get(url, params=..., timeout=...)``. For typed
    JSON GETs prefer :func:`pm_get`.
    """
    return _session()


__all__ = ["pm_get", "pm_session", "GAMMA", "DATA", "CLOB", "USER_AGENT"]
