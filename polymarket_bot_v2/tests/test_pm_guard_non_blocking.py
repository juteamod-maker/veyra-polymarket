"""T287 Wave 3.7 regression: pm_guard.js must not block first paint.

The previous implementation hid <body> via `visibility: hidden` until two
serial fetches completed, causing a multi-second "正在校验账户钱包…" wait.

This test guards the new skeleton-first architecture by asserting:
1. base.html no longer injects the early `pm-guard-pending` visibility:hidden CSS.
2. pm_guard.js does not call `documentElement.classList.add('pm-guard-pending')`
   prior to overlay decisions (no early body-blocking).
3. pm_guard.js fetches `/api/user/me` and `/api/status` in parallel
   (`Promise.all([getMe(), getStatus()])`).
4. pm_guard.js implements sessionStorage caching for `/api/user/me`.
5. The loading overlay text is no longer the long blocking phrase.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_HTML = REPO_ROOT / "scripts" / "frontend" / "templates" / "base.html"
GUARD_JS = REPO_ROOT / "scripts" / "frontend" / "static" / "js" / "pm_guard.js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_base_html_no_visibility_hidden_block():
    html = _read(BASE_HTML)
    assert "visibility: hidden" not in html, (
        "base.html must not hide body via visibility:hidden — first paint "
        "must always be allowed."
    )
    assert "pm-guard-early-css" not in html, (
        "Legacy early-blocking CSS block must be removed."
    )


def test_pm_guard_js_does_not_eagerly_block_body():
    js = _read(GUARD_JS)
    # The legacy code added pm-guard-pending before any fetch resolved.
    # Now it must only manage classes inside clearGuards / showOverlay.
    assert "documentElement.classList.add('pm-guard-pending')" not in js, (
        "pm_guard.js must not eagerly mark the document as pending."
    )


def test_pm_guard_js_uses_parallel_fetch():
    js = _read(GUARD_JS)
    assert "Promise.all([getMe(), getStatus()])" in js, (
        "pm_guard.js must fetch /api/user/me and /api/status in parallel."
    )


def test_pm_guard_js_has_session_cache():
    js = _read(GUARD_JS)
    assert "ME_CACHE_KEY" in js and "sessionStorage" in js, (
        "pm_guard.js must cache /api/user/me in sessionStorage for SWR."
    )
    assert "readMeCache" in js and "writeMeCache" in js, (
        "Cache helpers must be implemented."
    )


def test_pm_guard_js_loading_text_is_minimal():
    js = _read(GUARD_JS)
    # The legacy long full-screen text is forbidden.
    assert "正在校验账户钱包…" not in js, (
        "The blocking '正在校验账户钱包…' overlay text must be removed."
    )


def test_pm_guard_js_uses_delayed_spinner():
    js = _read(GUARD_JS)
    assert "SPINNER_DELAY_MS" in js, "Spinner must be delayed via SPINNER_DELAY_MS."
    assert "setTimeout(injectLoadingOverlay" in js, (
        "Spinner must be scheduled via setTimeout, not shown immediately."
    )
