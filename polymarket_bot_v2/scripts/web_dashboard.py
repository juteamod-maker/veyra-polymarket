"""
web_dashboard.py — Veyra.trade Dashboard 入口
轻量 FastAPI 服务，路由拆分到 dashboard_routes/ 子模块。

用法：
    cd /opt/polymarket_bot_v2
    .venv/bin/uvicorn scripts.web_dashboard:app --host 0.0.0.0 --port 80

认证：T474 起统一使用 Privy 单一通道。管理员通过 user_session cookie + role
in {admin, super_admin} 自动识别，不再需要 DASHBOARD_KEY/TOTP/?key= 旁路。
"""

import hashlib
import logging
import os
import re
import secrets
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from scripts.dashboard_routes._shared import (
    STATIC_DIR,
    ip_blacklist,
    ip_rate_limiter,
    verify_user_session,
)
from scripts.dashboard_routes.auth import router as auth_router
from scripts.dashboard_routes.backtest_routes import router as backtest_router
from scripts.dashboard_routes.config_routes import router as config_router
from scripts.dashboard_routes.copytrade_routes import router as copytrade_router
from scripts.dashboard_routes.copytrader_v2_routes import router as copytrader_v2_router
from scripts.dashboard_routes.data import router as data_router
from scripts.dashboard_routes.discover_routes import (
    router as discover_router,
)  # T289: PM 事件发现/详情/下单
from scripts.dashboard_routes.frontend_routes import router as frontend_router
from scripts.dashboard_routes.frontend_routes import (
    alias_router as frontend_alias_router,
)
from scripts.dashboard_routes.help_pages import (
    router as help_pages_router,
)  # T287: /api/help-pages
from scripts.dashboard_routes.homepage_routes import router as homepage_router
from scripts.dashboard_routes.ecosystem_routes import router as ecosystem_router  # T509
from scripts.dashboard_routes.map_routes import router as map_router
from scripts.dashboard_routes.menu_routes import router as menu_router  # T302: 菜单管理
from scripts.dashboard_routes.frontend_banner_routes import (
    router as frontend_banner_router,
)  # T561 W5-D: Frontend Banner 管理
from scripts.dashboard_routes.i18n_dynamic import (
    router as i18n_dynamic_router,
)  # T599: 动态 i18n 字典聚合
from scripts.dashboard_routes.i18n_admin_routes import (
    router as i18n_admin_router,
)  # T604: 后台 i18n JSON 直编
from scripts.dashboard_routes.leaderboard_routes import (
    router as leaderboard_router,
)  # T610B: 积分排行榜 API
from scripts.dashboard_routes.valuelab_routes import (
    router as valuelab_router,
)  # T735: Value Lab API
from scripts.dashboard_routes.sportslive_routes import (
    router as sportslive_router,
)  # T557 W2: 赛事赔率 Hub
from scripts.dashboard_routes.search_routes import (
    router as search_router,
    alias_router as search_alias_router,
)  # T872: 全站搜索（PM public-search 代理 + 结果页）
from scripts.dashboard_routes.points_routes import router as points_router
from scripts.dashboard_routes.coin_drop_routes import router as coin_drop_router
from scripts.dashboard_routes.polymarket_manual_routes import (
    router as polymarket_manual_router,
)
from scripts.dashboard_routes.price_monitor_routes import router as price_monitor_router
from scripts.dashboard_routes.security import router as security_router
from scripts.dashboard_routes.system import router as system_router
from scripts.dashboard_routes.tracking_routes import router as tracking_router
from scripts.dashboard_routes.user_routes import router as user_router
from scripts.dashboard_routes.redeem_withdraw_routes import (
    router as redeem_withdraw_router,
)  # T460
from scripts.dashboard_routes.wallet_routes import router as wallet_router
from scripts.dashboard_routes.weather_city_routes import router as weather_city_router
from scripts.dashboard_routes.weather_map_routes import router as weather_map_router

# ── FastAPI ───────────────────────────────────────────────────────────
app = FastAPI(title="Veyra.trade Dashboard", docs_url=None, redoc_url=None)

_SESSION_SECRET = os.environ.get("USER_SESSION_SECRET") or os.environ.get(
    "SESSION_SECRET"
)
if not _SESSION_SECRET:
    # T901 安全修复：删除硬编码兜底密钥（"veyra-v2-session-fallback-change-me"）。
    # 该常量一旦随开源仓库公开，任何人都能伪造 Starlette session cookie。
    # 未配置时改用进程内随机密钥：不再存在可预测秘密，代价是重启后
    # needs_onboarding 之类的 session 标记失效。
    _SESSION_SECRET = secrets.token_hex(32)
    logging.getLogger("web_dashboard").critical(
        "USER_SESSION_SECRET/SESSION_SECRET is not configured; using an "
        "ephemeral random session key. Sessions will not survive a restart. "
        "Set USER_SESSION_SECRET in production."
    )
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    session_cookie="veyra_session",
    max_age=86400 * 7,
    same_site="lax",
    https_only=True,
)


def _friendly_404_html(path: str) -> str:
    # T786: pixel-perfect replica of https://demo.tailadmin.com/404 (light theme)
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>404 - Page Not Found</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh;
      background: #f9fafb;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      position: relative;
      overflow: hidden;
    }
    .grid-deco {
      position: absolute;
      z-index: 0;
      width: 250px;
      max-width: 250px;
      pointer-events: none;
    }
    .grid-top-right { right: 0; top: 0; }
    .grid-bottom-left { left: 0; bottom: 0; transform: rotate(180deg); }
    @media (min-width: 1280px) {
      .grid-deco { width: 450px; max-width: 450px; }
    }
    .content {
      position: relative;
      z-index: 1;
      width: 100%;
      max-width: 242px;
      text-align: center;
    }
    @media (min-width: 640px) {
      .content { max-width: 472px; }
    }
    h1 {
      margin-bottom: 32px;
      font-size: 48px;
      font-weight: 700;
      line-height: 1.2;
      color: #1d2939;
    }
    @media (min-width: 1280px) {
      h1 { font-size: 72px; }
    }
    .svg-404 { width: 100%; height: auto; }
    p {
      margin: 40px 0 24px;
      font-size: 16px;
      color: #374151;
      line-height: 1.5;
    }
    @media (min-width: 640px) {
      p { font-size: 18px; }
    }
    .btn-home {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 8px;
      border: 1px solid #d1d5db;
      background: #ffffff;
      padding: 14px 20px;
      font-size: 14px;
      font-weight: 500;
    font-family: inherit;
      color: #374151;
      text-decoration: none;
      box-shadow: 0 1px 2px 0 rgba(16,24,40,.05);
      transition: background .15s, color .15s;
    }
    .btn-home:hover {
      background: #f9fafb;
      color: #111827;
    }
    .page-footer {
      position: absolute;
      bottom: 24px;
      left: 50%;
      transform: translateX(-50%);
      font-size: 14px;
      color: #667085;
      white-space: nowrap;
    }
  </style>
</head>
<body>
  <!-- Grid corner decorations -->
  <div class="grid-deco grid-top-right" aria-hidden="true">
    <svg width="450" height="254" viewBox="0 0 450 254" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path fill-rule="evenodd" clip-rule="evenodd" d="M0.50555 45.1131L450 45.1132L450 44.6073L0.50555 44.6072L0.50555 45.1131Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M205.546 253.529L205.546 -2.13709e-05L205.04 -2.1392e-05L205.04 253.529L205.546 253.529Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M0.505546 97.2164L450 97.2165L450 96.7106L0.505546 96.7106L0.505546 97.2164Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M256.806 253.529L256.806 -1.68895e-05L256.3 -1.69106e-05L256.3 253.529L256.806 253.529Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M0.505837 253.529L0.505859 -3.9296e-05L0 -3.93171e-05L-2.21642e-05 253.529L0.505837 253.529Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M0.505541 149.321L450 149.321L450 148.815L0.505541 148.815L0.505541 149.321Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M308.066 253.529L308.066 -1.24083e-05L307.56 -1.24294e-05L307.56 253.529L308.066 253.529Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M51.7662 253.529L51.7662 -3.48147e-05L51.2603 -3.48358e-05L51.2603 253.529L51.7662 253.529Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M0.505537 201.424L450 201.424L450 200.918L0.505537 200.918L0.505537 201.424Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M359.326 253.529L359.326 -7.92695e-06L358.82 -7.94806e-06L358.82 253.529L359.326 253.529Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M103.026 253.529L103.026 -3.03334e-05L102.52 -3.03545e-05L102.52 253.529L103.026 253.529Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M410.586 253.529L410.586 -3.44569e-06L410.08 -3.4668e-06L410.08 253.529L410.586 253.529Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M154.286 253.529L154.286 -2.58521e-05L153.78 -2.58732e-05L153.78 253.529L154.286 253.529Z" fill="url(#g0a)" fill-opacity="0.3"/>
      <rect width="50.7536" height="51.5982" transform="matrix(-1 -8.74228e-08 -8.74228e-08 1 358.821 45.1138)" fill="#B2B2B2" fill-opacity="0.08"/>
      <rect width="50.756" height="51.5985" transform="matrix(-1 -8.74228e-08 -8.74228e-08 1 307.559 97.2163)" fill="#B2B2B2" fill-opacity="0.08"/>
      <defs>
        <linearGradient id="g0a" x1="277.872" y1="-9.94587e-06" x2="194.87" y2="235.867" gradientUnits="userSpaceOnUse">
          <stop stop-color="#B2B2B2"/>
          <stop offset="1" stop-color="#B2B2B2" stop-opacity="0"/>
        </linearGradient>
      </defs>
    </svg>
  </div>
  <div class="grid-deco grid-bottom-left" aria-hidden="true">
    <svg width="450" height="254" viewBox="0 0 450 254" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path fill-rule="evenodd" clip-rule="evenodd" d="M0.50555 45.1131L450 45.1132L450 44.6073L0.50555 44.6072L0.50555 45.1131Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M205.546 253.529L205.546 -2.13709e-05L205.04 -2.1392e-05L205.04 253.529L205.546 253.529Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M0.505546 97.2164L450 97.2165L450 96.7106L0.505546 96.7106L0.505546 97.2164Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M256.806 253.529L256.806 -1.68895e-05L256.3 -1.69106e-05L256.3 253.529L256.806 253.529Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M0.505837 253.529L0.505859 -3.9296e-05L0 -3.93171e-05L-2.21642e-05 253.529L0.505837 253.529Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M0.505541 149.321L450 149.321L450 148.815L0.505541 148.815L0.505541 149.321Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M308.066 253.529L308.066 -1.24083e-05L307.56 -1.24294e-05L307.56 253.529L308.066 253.529Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M51.7662 253.529L51.7662 -3.48147e-05L51.2603 -3.48358e-05L51.2603 253.529L51.7662 253.529Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M0.505537 201.424L450 201.424L450 200.918L0.505537 200.918L0.505537 201.424Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M359.326 253.529L359.326 -7.92695e-06L358.82 -7.94806e-06L358.82 253.529L359.326 253.529Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M103.026 253.529L103.026 -3.03334e-05L102.52 -3.03545e-05L102.52 253.529L103.026 253.529Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M410.586 253.529L410.586 -3.44569e-06L410.08 -3.4668e-06L410.08 253.529L410.586 253.529Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M154.286 253.529L154.286 -2.58521e-05L153.78 -2.58732e-05L153.78 253.529L154.286 253.529Z" fill="url(#g0b)" fill-opacity="0.3"/>
      <rect width="50.7536" height="51.5982" transform="matrix(-1 -8.74228e-08 -8.74228e-08 1 358.821 45.1138)" fill="#B2B2B2" fill-opacity="0.08"/>
      <rect width="50.756" height="51.5985" transform="matrix(-1 -8.74228e-08 -8.74228e-08 1 307.559 97.2163)" fill="#B2B2B2" fill-opacity="0.08"/>
      <defs>
        <linearGradient id="g0b" x1="277.872" y1="-9.94587e-06" x2="194.87" y2="235.867" gradientUnits="userSpaceOnUse">
          <stop stop-color="#B2B2B2"/>
          <stop offset="1" stop-color="#B2B2B2" stop-opacity="0"/>
        </linearGradient>
      </defs>
    </svg>
  </div>

  <!-- Main content -->
  <div class="content">
    <h1>ERROR</h1>

        <img class="svg-404" src="/static/Veyra404.png" alt="404" />

    <p>We can't seem to find the page you are looking for!</p>

        <a href="https://veyra.trade/discover" class="btn-home">Back to Home Page</a>
  </div>

  <!-- Footer -->
  <p class="page-footer">&copy; <span id="yr">2026</span> - Veyra.Trade</p>
  <script>document.getElementById('yr').textContent=new Date().getFullYear();</script>
</body>
</html>"""


@app.exception_handler(404)
async def _friendly_not_found_handler(request: Request, exc: StarletteHTTPException):
    path = request.url.path or ""
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return HTMLResponse(_friendly_404_html(path), status_code=404)


@app.exception_handler(FastAPIHTTPException)
async def _http_exception_handler(request: Request, exc: FastAPIHTTPException):
    if int(getattr(exc, "status_code", 500)) != 404:
        detail = getattr(exc, "detail", "http_error")
        return JSONResponse({"detail": detail}, status_code=int(exc.status_code))
    path = request.url.path or ""
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return HTMLResponse(_friendly_404_html(path), status_code=404)


# ── T456: CORS 严格限制 — 只允许本站源，禁止第三方跨域调用 ───────────
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://veyra.trade", "https://www.veyra.trade"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    max_age=600,
)

# T214I + T475: HTML 压缩 → GZip。顺序要求 HTMLMinify 必须比 GZip 先注册，
# 这样在响应路上 GZip 作为外层压缩已压缩后的 HTML，避免 HTMLMinify 处理 gzip 字节。
import re as _re_t475  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.responses import Response as _StarletteResponse  # noqa: E402

_T475_HTML_COMMENT_RE = _re_t475.compile(rb"<!--(?!\[if).*?-->", _re_t475.DOTALL)
# 严格保留 script/textarea/pre 内容（含字符串/正则字面量，不可触碰）
_T475_PRESERVE_RE = _re_t475.compile(
    rb"(<(?:pre|textarea|script)\b[^>]*>.*?</(?:pre|textarea|script)>)",
    _re_t475.DOTALL | _re_t475.IGNORECASE,
)
# style 区块单独提取，做 CSS 级最小压缩
_T475_STYLE_RE = _re_t475.compile(
    rb"(<style\b[^>]*>)(.*?)(</style>)", _re_t475.DOTALL | _re_t475.IGNORECASE
)
_T475_BETWEEN_TAGS_WS = _re_t475.compile(rb">\s+<")
_T475_MULTI_WS = _re_t475.compile(rb"[ \t]{2,}")
_T475_BLANK_LINES = _re_t475.compile(rb"\n\s*\n+")
# CSS minify regex
_T475_CSS_COMMENT = _re_t475.compile(rb"/\*.*?\*/", _re_t475.DOTALL)
_T475_CSS_WS = _re_t475.compile(rb"\s+")
_T475_CSS_AROUND = _re_t475.compile(rb"\s*([{};:,>+~])\s*")
_T475_CSS_SEMI = _re_t475.compile(rb";}")


def _t475_minify_css(css: bytes) -> bytes:
    try:
        css = _T475_CSS_COMMENT.sub(b"", css)
        css = _T475_CSS_WS.sub(b" ", css)
        css = _T475_CSS_AROUND.sub(rb"\1", css)
        css = _T475_CSS_SEMI.sub(b"}", css)
        return css.strip()
    except Exception:
        return css


def _t475_minify_html(body: bytes) -> bytes:
    try:
        # 1. CSS minify inside <style>
        def _style_repl(m):
            return m.group(1) + _t475_minify_css(m.group(2)) + m.group(3)

        body = _T475_STYLE_RE.sub(_style_repl, body)

        # 2. 占位保留 script/pre/textarea
        parts: list[bytes] = []
        last = 0
        preserved: list[bytes] = []
        for m in _T475_PRESERVE_RE.finditer(body):
            parts.append(body[last : m.start()])
            preserved.append(m.group(0))
            parts.append(("\x00T475\x00" + str(len(preserved) - 1) + "\x00").encode())
            last = m.end()
        parts.append(body[last:])
        joined = b"".join(parts)

        # 3. HTML 级压缩
        joined = _T475_HTML_COMMENT_RE.sub(b"", joined)
        joined = _T475_BETWEEN_TAGS_WS.sub(b"><", joined)
        joined = _T475_MULTI_WS.sub(b" ", joined)
        joined = _T475_BLANK_LINES.sub(b"\n", joined)

        # 4. 还原保留区段
        for idx, raw in enumerate(preserved):
            ph = ("\x00T475\x00" + str(idx) + "\x00").encode()
            joined = joined.replace(ph, raw, 1)
        return joined.strip()
    except Exception:
        return body


class HTMLMinifyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        try:
            if response.status_code != 200:
                return response
            ct = response.headers.get("content-type", "")
            if "text/html" not in ct:
                return response
            path = (request.url.path or "").strip().lower()
            if path.startswith("/admin"):
                return response
            body_iter = getattr(response, "body_iterator", None)
            if body_iter is None:
                raw = getattr(response, "body", None) or b""
                if not raw:
                    return response
                new_body = _t475_minify_html(raw)
                response.body = new_body
                response.headers["content-length"] = str(len(new_body))
                return response
            chunks: list[bytes] = []
            async for chunk in body_iter:
                chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
            original = b"".join(chunks)
            minified = _t475_minify_html(original)
            return _StarletteResponse(
                content=minified,
                status_code=response.status_code,
                headers={
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() not in {"content-length"}
                },
                media_type=ct,
            )
        except Exception:
            return response


class CSSMinifyMiddleware(BaseHTTPMiddleware):
    """T475 W2: 对 text/css 响应做正则压缩 + 强缓存 头，跳过 *.min.css。"""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        try:
            if response.status_code != 200:
                return response
            ct = response.headers.get("content-type", "")
            if "text/css" not in ct.lower():
                return response
            path = (request.url.path or "").lower()
            if path.endswith(".min.css"):
                # already minified; only add cache header
                response.headers.setdefault(
                    "cache-control", "public, max-age=604800, immutable"
                )
                return response
            body_iter = getattr(response, "body_iterator", None)
            chunks: list[bytes] = []
            if body_iter is None:
                raw = getattr(response, "body", None) or b""
                minified = _t475_minify_css(raw)
                response.body = minified
                response.headers["content-length"] = str(len(minified))
                response.headers.setdefault(
                    "cache-control", "public, max-age=604800, immutable"
                )
                return response
            async for chunk in body_iter:
                chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
            original = b"".join(chunks)
            minified = _t475_minify_css(original)
            new_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() not in {"content-length"}
            }
            new_headers.setdefault("cache-control", "public, max-age=604800, immutable")
            return _StarletteResponse(
                content=minified,
                status_code=response.status_code,
                headers=new_headers,
                media_type=ct,
            )
        except Exception:
            return response


app.add_middleware(HTMLMinifyMiddleware)
app.add_middleware(CSSMinifyMiddleware)


class StaticAssetCacheMiddleware(BaseHTTPMiddleware):
    """T475 W2: 给 /static/, /v2/static/ 下的 js/css/字体/图片 添加强缓存。
    模板引用使用 ?v={asset_version} cache-bust，可放心使用 immutable。"""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        try:
            if response.status_code != 200:
                return response
            path = (request.url.path or "").lower()
            if not (path.startswith("/static/") or path.startswith("/v2/static/")):
                return response
            if path.endswith(
                (
                    ".js",
                    ".mjs",
                    ".css",
                    ".woff",
                    ".woff2",
                    ".ttf",
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".webp",
                    ".svg",
                    ".ico",
                    ".map",
                )
            ):
                response.headers.setdefault(
                    "cache-control", "public, max-age=604800, immutable"
                )
        except Exception:
            pass
        return response


app.add_middleware(StaticAssetCacheMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=500)
logger = logging.getLogger("web_dashboard")


# ── Phase-2: 全局并发限流 + 慢请求日志 ─────────────────────────────────
# 为什么用 Starlette pure middleware 而非装饰器：
#   - 信号量必须在路由执行前后管理；
#   - 慢请求日志只关心总耗时，与具体路由无关；
#   - 需要排除 /static/ 与 /healthz 等高频低成本路径。
import asyncio as _t566_asyncio  # noqa: E402
import collections as _t566_collections  # noqa: E402
import time as _t566_time  # noqa: E402

# 单 worker 内：
#   - 全局并发上限 = max(8, GUNICORN_WORKERS=2 时 60/worker = 60)
#   - 每 IP 每秒上限 = 15（滑动 1s 窗口）
_T566_GLOBAL_CONCURRENCY = int(os.environ.get("DASH_MAX_CONCURRENCY", "60"))
_T566_PER_IP_PER_SEC = int(os.environ.get("DASH_PER_IP_QPS", "15"))
_T566_SLOW_LOG_MS = int(os.environ.get("DASH_SLOW_LOG_MS", "800"))

_T566_SEM = _t566_asyncio.Semaphore(_T566_GLOBAL_CONCURRENCY)
_T566_IP_HITS: dict[str, _t566_collections.deque] = {}
_T566_IP_LOCK = _t566_asyncio.Lock()

# 这些路径不计入限流/慢日志（静态文件 + 健康检查）
_T566_BYPASS_PREFIXES = (
    "/static/",
    "/v2/static/",
    "/favicon.ico",
    "/robots.txt",
)


def _t566_client_ip(request) -> str:
    try:
        # 信任 X-Forwarded-For 第一段（前置 nginx 已通过 _shared 校验）
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "0.0.0.0"
    except Exception:
        return "0.0.0.0"


@app.middleware("http")
async def t566_throttle_and_slowlog(request, call_next):
    path = request.url.path or ""
    if any(path.startswith(p) for p in _T566_BYPASS_PREFIXES):
        return await call_next(request)

    # 1) 每 IP 滑动 1s 窗口限流（in-memory，单 worker 范围）
    ip = _t566_client_ip(request)
    now = _t566_time.monotonic()
    async with _T566_IP_LOCK:
        dq = _T566_IP_HITS.get(ip)
        if dq is None:
            dq = _t566_collections.deque()
            _T566_IP_HITS[ip] = dq
        # 修剪 1s 之外的旧时间戳
        cutoff = now - 1.0
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _T566_PER_IP_PER_SEC:
            logger.warning("[Throttle] per-ip 429 ip=%s qps=%d", ip, len(dq))
            return JSONResponse(
                {"error": "rate_limited", "scope": "per_ip"},
                status_code=429,
                headers={"Retry-After": "1"},
            )
        dq.append(now)
        # 简单 GC：表过大时清理空 deque
        if len(_T566_IP_HITS) > 2048:
            for k in list(_T566_IP_HITS.keys())[:512]:
                if not _T566_IP_HITS[k]:
                    del _T566_IP_HITS[k]

    # 2) 全局并发信号量 — 满载时立即 503，避免请求排队耗尽 anyio 线程
    if not _T566_SEM.locked() or _T566_SEM._value > 0:  # type: ignore[attr-defined]
        pass  # quick path
    try:
        await _t566_asyncio.wait_for(_T566_SEM.acquire(), timeout=2.0)
    except _t566_asyncio.TimeoutError:
        logger.warning("[Throttle] global 503 path=%s", path)
        return JSONResponse(
            {"error": "service_busy"},
            status_code=503,
            headers={"Retry-After": "1"},
        )
    t0 = _t566_time.monotonic()
    try:
        response = await call_next(request)
    finally:
        _T566_SEM.release()
        elapsed_ms = int((_t566_time.monotonic() - t0) * 1000)
        if elapsed_ms >= _T566_SLOW_LOG_MS:
            logger.warning(
                "[Slow] path=%s ms=%d ip=%s",
                path,
                elapsed_ms,
                ip,
            )
    return response


# ── 静态文件 ──────────────────────────────────────────────────────────
JS_DIR = STATIC_DIR / "js"
if JS_DIR.is_dir():
    app.mount("/static/js", StaticFiles(directory=str(JS_DIR)), name="static-js")

CSS_DIR = STATIC_DIR / "css"
if CSS_DIR.is_dir():
    app.mount("/static/css", StaticFiles(directory=str(CSS_DIR)), name="static-css")

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static-root")

# ── V2 前端静态文件 ──────────────────────────────────────────────────
from pathlib import Path as _Path

_V2_STATIC = _Path(__file__).resolve().parent / "frontend" / "static"
if _V2_STATIC.is_dir():
    app.mount("/v2/static", StaticFiles(directory=str(_V2_STATIC)), name="v2-static")

FAVICON_PATH = STATIC_DIR / "favicon.ico"


def _js_build_version() -> str:
    """Compute a short hash from all JS files' mtimes for cache-busting."""
    js_dir = STATIC_DIR / "js"
    try:
        mtimes = sorted(
            str(int(os.path.getmtime(p))) for p in js_dir.iterdir() if p.suffix == ".js"
        )
        return hashlib.md5("".join(mtimes).encode()).hexdigest()[:8]
    except Exception:
        return "0"


def _serve_static_html(filename: str) -> HTMLResponse:
    """Serve static HTML with cache-busting for local JS/CSS assets."""
    page_path = STATIC_DIR / filename
    if not page_path.exists():
        return HTMLResponse(f"<h1>{filename} not found</h1>", status_code=500)
    content = page_path.read_text(encoding="utf-8")
    bv = _js_build_version()
    content = re.sub(r'(src="/static/js/[^"]+\.js)"', rf'\1?v={bv}"', content)
    content = re.sub(r'(href="/static/css/[^"]+\.css)"', rf'\1?v={bv}"', content)
    return HTMLResponse(content)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    if FAVICON_PATH.exists():
        return FileResponse(str(FAVICON_PATH), media_type="image/x-icon")
    return Response(status_code=204)


# ── T547 性能埋点中间件 ─────────────────────────────────────────────────
# Lightweight per-request timing into an in-memory ring buffer. Surfaced via
# `/api/admin/perf/metrics` (admin-only). No DB writes; bounded memory.
from collections import deque  # noqa: E402
import time as _t547_time  # noqa: E402

_T547_BUFFER: deque = deque(maxlen=512)
_T547_BUCKETS: dict[str, dict[str, float | int]] = {}
_T547_BUCKET_LIMIT = 200


def _t547_bucket_key(path: str) -> str:
    p = (path or "").lower()
    if not p.startswith("/api/"):
        return "_html"
    # Collapse path params: /api/event/<slug>/x → /api/event/{slug}/x
    parts = p.split("?", 1)[0].split("/")
    out = []
    for seg in parts:
        if not seg:
            out.append(seg)
            continue
        if any(ch.isdigit() for ch in seg) and any(ch.isalpha() for ch in seg):
            out.append("{p}")
        elif seg.isdigit() or (
            len(seg) > 8 and all(c.isalnum() or c in "-_" for c in seg)
        ):
            out.append("{p}")
        else:
            out.append(seg)
    return "/".join(out) or p


@app.middleware("http")
async def t547_perf_middleware(request: Request, call_next):
    started = _t547_time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (_t547_time.perf_counter() - started) * 1000.0
    try:
        path = request.url.path or ""
        key = _t547_bucket_key(path)
        bucket = _T547_BUCKETS.get(key)
        if bucket is None:
            if len(_T547_BUCKETS) >= _T547_BUCKET_LIMIT:
                # Drop the oldest bucket to bound memory
                try:
                    _T547_BUCKETS.pop(next(iter(_T547_BUCKETS)))
                except StopIteration:
                    pass
            bucket = {"n": 0, "sum_ms": 0.0, "max_ms": 0.0, "p50_acc": 0.0, "err": 0}
            _T547_BUCKETS[key] = bucket
        bucket["n"] = int(bucket["n"]) + 1
        bucket["sum_ms"] = float(bucket["sum_ms"]) + elapsed_ms
        if elapsed_ms > float(bucket["max_ms"]):
            bucket["max_ms"] = elapsed_ms
        if response.status_code >= 500:
            bucket["err"] = int(bucket["err"]) + 1
        if elapsed_ms >= 200.0 or response.status_code >= 500:
            _T547_BUFFER.append(
                {
                    "ts": _t547_time.time(),
                    "method": request.method,
                    "path": path,
                    "status": response.status_code,
                    "ms": round(elapsed_ms, 2),
                }
            )
        # Add response header for browser-side perf overlay (non-PII).
        response.headers["x-veyra-rt-ms"] = f"{elapsed_ms:.1f}"
    except Exception:
        pass
    return response


@app.get("/api/admin/perf/metrics")
async def t547_perf_metrics(request: Request):
    from scripts.dashboard_routes._shared import (
        get_request_identity as _t547_identity,
    )  # noqa: PLC0415

    info = _t547_identity(request)
    if not info or not info.get("is_admin"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = []
    for key, b in _T547_BUCKETS.items():
        n = int(b.get("n") or 0)
        rows.append(
            {
                "endpoint": key,
                "count": n,
                "avg_ms": round(float(b.get("sum_ms") or 0) / n, 2) if n else 0.0,
                "max_ms": round(float(b.get("max_ms") or 0), 2),
                "errors": int(b.get("err") or 0),
            }
        )
    rows.sort(key=lambda r: r["avg_ms"], reverse=True)
    return {
        "ok": True,
        "buckets": rows[:100],
        "recent_slow_or_err": list(_T547_BUFFER)[-100:],
        "buffer_size": len(_T547_BUFFER),
        "endpoint_count": len(_T547_BUCKETS),
    }


# ── T420 CSP 安全响应头中间件 ─────────────────────────────────────────
# ── T602 admin no-store middleware ────────────────────────────────────
# 全局兜底：所有 admin 类后端写路由的响应一律 Cache-Control: no-store，
# 避免任何中间层（浏览器 / Cloudflare / nginx）缓存 admin 列表 / 写后状态，
# 杜绝"改了看不到 / 删了还在列表"等 BUG（详见 T600 菜单根因 #1+#3）。
_ADMIN_NOSTORE_PREFIXES = (
    "/api/admin/",
    "/api/menu/admin/",
    "/api/system/",
    "/api/security/",
    "/api/help-pages",
    "/api/weather-cities",
    "/api/map/markers",
    "/api/copy-tasks",
    "/api/v2/copytrader/",
    "/api/env/",
    "/api/risk/",
    "/api/pm/i18n/",
    "/api/pm-tags/",
    "/api/pm-city-sync",
    "/api/toggle-dry-run",
    "/api/emergency-freeze",
)
_ADMIN_NOSTORE_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def admin_no_store_middleware(request: Request, call_next):
    response = await call_next(request)
    try:
        path = request.url.path or ""
        method = (request.method or "").upper()
        # 任意 admin 写操作 → 一律 no-store；
        # admin GET 列表也一律 no-store（admin 后台界面对实时性要求高，且流量极小）。
        if method in _ADMIN_NOSTORE_WRITE_METHODS or any(
            path.startswith(p) for p in _ADMIN_NOSTORE_PREFIXES
        ):
            if any(path.startswith(p) for p in _ADMIN_NOSTORE_PREFIXES):
                # 如果路由已显式设置 Cache-Control，尊重它（避免覆盖 stale-while-revalidate 等高级策略）
                if "cache-control" not in {k.lower() for k in response.headers.keys()}:
                    response.headers["Cache-Control"] = "no-store"
                    response.headers["Pragma"] = "no-cache"
    except Exception:
        pass
    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """为所有 HTML 响应注入 Content-Security-Policy 头，
    维持前端依赖白名单并禁止 unsafe-eval，
    防止浏览器因缺少 CSP 声明而产生控制台阻止警告。
    """
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        script_src = "'self' 'unsafe-inline' "
        csp = (
            "default-src 'self'; "
            f"script-src {script_src}"
            "static.cloudflareinsights.com; "
            "style-src 'self' 'unsafe-inline' "
            "; "
            "img-src 'self' data: blob: "
            "*.polymarket.com polymarket.com "
            "polymarket-upload.s3.us-east-2.amazonaws.com "
            "*.s3.amazonaws.com "
            "www.gstatic.com *.gstatic.com "
            "explorer-api.walletconnect.com "
            "*.basemaps.cartocdn.com "
            "cartodb-basemaps-a.global.ssl.fastly.net "
            "cartodb-basemaps-b.global.ssl.fastly.net "
            "cartodb-basemaps-c.global.ssl.fastly.net "
            "cartodb-basemaps-d.global.ssl.fastly.net; "
            "font-src 'self'; "
            "connect-src 'self' *.polymarket.com clob.polymarket.com "
            "gamma-api.polymarket.com data-api.polymarket.com "
            "cloudflareinsights.com "
            "auth.privy.io api.privy.io api.web3modal.org *.web3modal.org "
            "explorer-api.walletconnect.com pulse.walletconnect.org "
            "relay.walletconnect.com relay.walletconnect.org "
            "apple.com *.apple.com google.com *.google.com "
            "https://*.rpc.privy.systems "
            "wss://relay.walletconnect.com wss://relay.walletconnect.org wss://www.walletlink.org; "
            "child-src 'self' auth.privy.io verify.walletconnect.com verify.walletconnect.org challenges.cloudflare.com; "
            "frame-src 'self' auth.privy.io verify.walletconnect.com verify.walletconnect.org challenges.cloudflare.com; "
            "object-src 'none'"
        )
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # T929 fix: payment=(self) is REQUIRED for Privy fundWallet (Coinbase/MoonPay
        # onramp uses Payment Request API). Removing it -> "payment is not allowed in
        # this document" violations + Buy crypto modal silently broken.
        # publickey-credentials-get=(self) reserved for future Privy passkey login.
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), usb=(), "
            "magnetometer=(), gyroscope=(), accelerometer=(), "
            "payment=(self), publickey-credentials-get=(self), fullscreen=(self)"
        )
    try:
        forwarded_proto = (request.headers.get("x-forwarded-proto") or "").strip().lower()
        scheme = forwarded_proto or request.url.scheme
        if scheme == "https" and "strict-transport-security" not in {
            k.lower() for k in response.headers.keys()
        }:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if "x-content-type-options" not in {k.lower() for k in response.headers.keys()}:
            response.headers["X-Content-Type-Options"] = "nosniff"
    except Exception:
        pass
    return response


# ── 认证中间件 ────────────────────────────────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    admin_page_paths = {"/admin", "/admin/", "/v2/admin", "/v2/admin/"}

    # Hard isolation: user-facing site must not serve or redirect admin entries.
    if path in admin_page_paths:
        return Response(status_code=404)

    # Canonical access policy:
    # - Prefer https://veyra.trade externally
    # - Keep legacy host and proxy host headers compatible to avoid CDN/origin redirect loops
    # - Keep localhost/127.0.0.1 reachable for internal health checks/deploy hooks
    host_header = (request.headers.get("host") or "").strip().lower()
    host_name = host_header.split(":", 1)[0] if host_header else ""
    fwd_host_header = (request.headers.get("x-forwarded-host") or "").strip().lower()
    forwarded_host = fwd_host_header.split(",", 1)[0].strip().split(":", 1)[0]
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").strip().lower()
    scheme = forwarded_proto or request.url.scheme
    internal_hosts = {"127.0.0.1", "localhost", "testserver"}
    canonical_host = "veyra.trade"
    # 兼容历史域名和代理回源 Host，避免在 CDN 场景下触发 301 循环导致 502。
    compatible_hosts = {"veyra.trade", "www.veyra.trade"}
    effective_host = forwarded_host or host_name
    if effective_host and effective_host not in internal_hosts:
        needs_redirect = effective_host not in compatible_hosts or scheme != "https"
        if needs_redirect:
            target = f"https://{canonical_host}{request.url.path}"
            if request.url.query:
                target += f"?{request.url.query}"
            return RedirectResponse(url=target, status_code=301)

    # favicon 无需认证
    if path == "/favicon.ico":
        return await call_next(request)

    # 静态资源公开，避免登录页资源被401/403拦截。
    if path.startswith("/static/") or path.startswith("/v2/static/"):
        return await call_next(request)

    # ── T485: 同源守卫 ────────────────────────────────────────────────
    # 仅 /api/* 路径生效；保证只有从 veyra.trade 站点发起的请求（含游客匿名）
    # 才能访问后端 API。AI/搜索引擎爬虫只允许抓取 HTML 页面（同源守卫只盯 /api），
    # 不允许它们直接消耗后端业务 API；任何持有 user_session/dashboard_key
    # 的合法登录会话不受同源守卫影响（API SDK 客户端仍可使用）。
    if path.startswith("/api/"):
        origin_raw = (request.headers.get("origin") or "").strip().lower()
        referer_raw = (request.headers.get("referer") or "").strip().lower()
        host_for_check = (forwarded_host or host_name or "").lower()
        # 内部健康检查/部署回调直接放行
        if host_for_check in internal_hosts:
            pass
        # 已登录会话视为可信调用方
        elif request.cookies.get("user_session") or request.cookies.get(
            "dashboard_key"
        ):
            pass
        else:
            allowed_origins = (
                "https://veyra.trade",
                "https://www.veyra.trade",
                "http://localhost",
                "http://127.0.0.1",
            )
            ok = False
            if origin_raw:
                ok = any(
                    origin_raw == o or origin_raw.startswith(o + ":")
                    for o in allowed_origins
                )
            if not ok and referer_raw:
                ok = any(
                    referer_raw.startswith(o + "/") or referer_raw == o
                    for o in allowed_origins
                )
            if not ok:
                return JSONResponse(
                    {
                        "error": "cross-origin api access denied",
                        "code": "T485_SAME_ORIGIN_ONLY",
                    },
                    status_code=403,
                )

    # ── T257/T456: 公开路由（无需任何认证）──
    # 原则：最小化白名单，只放行真正的登录/认证流程端点。
    # 所有市场数据、行情、跟投、i18n 管理接口均须持有有效 session 才可访问。
    # /  (无 key 时) → 公开登录页
    # /Index → 公开登录页（别名）
    _PUBLIC_EXACT = frozenset(
        [
            # ── Privy 诊断页（临时，任何人可访问）──
            "/privy-diag",
            "/v2/debug/privy-bind",
            # ── V2 帮助页公开路由（T663-T674 验证链路）──
            "/v2/go",
            "/v2/faq",
            "/v2/about",
            "/v2/privacy",
            "/v2/terms",
            "/faq",
            "/about",
            "/privacy",
            "/terms",
            # ── T476: 游客预览页（无需登录）──
            "/preview",
            "/Builders",
            "/builders",
            # ── Privy SSO 登录流 ──
            "/api/user/privy/config",
            "/api/user/privy/verify",
            # ── 钱包连接登录流 ──
            "/api/user/challenge",
            "/api/user/verify",
            # ── 登录后基础操作（须持有 user_session cookie）──
            "/api/user/me",
            "/api/user/logout",
            # ── 首页公开卡片（landing page 渲染） ──
            "/api/public/homepage/cards",
            "/api/public/homepage/synth-titles",
            # ── T584：banner 页面槽位元数据（公开，供前端枚举可用页面） ──
            "/api/public/frontend-banner/_pages",
            # ── T599：动态 i18n 字典（公开，菜单/横幅文案运行时合并到 i18n dict） ──
            "/api/i18n/dynamic",
            # ── T509 Wave 7D：/preview 公开 ecosystem 列表（无需登录） ──
            "/api/ecosystem/list",
            # ── T509追加 Wave 8A：/preview Builders 目录公开接口 ──
            "/api/builders/list",
            "/api/builders/timeseries",
            # ── Valuelab 公开读接口（页面本身公开，避免游客 401 控制台报错） ──
            "/api/valuelab/events",
            "/api/valuelab/sources/health",
            "/api/valuelab/events/fragment",
            # ── T511 Wave 10A：/preview 顶部交易流（marquee）公开（游客可见） ──
            "/api/pm/marquee",
            # ── T610B：积分排行榜公开端点（任何登录/未登录用户均可查看） ──
            "/api/leaderboard/points",
            # ── 帮助/壳层状态（游客页面需要） ──
            "/api/status",
            # ── 登录页预取（只读） ──
            "/api/pm/events",
            "/api/pm/categories",
            "/api/v2/copytrader/wallets/search",
            # ── T859: 侧栏推广图公开端点（前台页面无需登录即可读取）──
            "/api/public/sidebar-promo",
        ]
    )
    if path in _PUBLIC_EXACT:
        return await call_next(request)

    # T584：按页面分发的 banner 公开端点（任何页面均可读，包含 SSR/匿名访问）
    if path.startswith("/api/public/frontend-banner/page/"):
        return await call_next(request)

    # T838：弹窗探宝金币收集（游客 + 注册用户均可访问，cookie 自动签发 guest_token）
    if path.startswith("/api/coin_drop/"):
        return await call_next(request)

    # 帮助文档 API：仅放行公开帮助 slug 的读取。
    if path.startswith("/api/help-pages/") and request.method.upper() == "GET":
        slug = path[len("/api/help-pages/") :].split("/", 1)[0].strip().lower()
        if slug in {"go", "faq", "about", "privacy", "terms"}:
            return await call_next(request)

    # T474: 禁止 ?key= / ?totp_session= 旁路认证 — 任何路径(含 / 与 /Index)
    # 若携带 key 都返回 400 / 重定向去掉，避免 URL 泄露式认证旁路残留。
    sensitive_qs_keys = {"key", "totp_session"}
    has_sensitive_qs = any(request.query_params.get(k, "") for k in sensitive_qs_keys)
    if has_sensitive_qs:
        if path.startswith("/api/"):
            return JSONResponse(
                {
                    "error": "query credential is not allowed",
                    "hint": "use cookie auth only",
                },
                status_code=400,
            )
        clean_items = [
            (k, v)
            for k, v in request.query_params.multi_items()
            if k not in sensitive_qs_keys
        ]
        target = request.url.path
        if clean_items:
            target += "?" + urlencode(clean_items, doseq=True)
        return RedirectResponse(url=target, status_code=302)

    # / 和 /Index：公开登录页（已登录用户也可见，由前端 JS 决定是否跳转）
    if path in ("/", "/Index"):
        return await call_next(request)

    # ── T474: 单一认证通道 — user_session cookie + Privy verify ──
    user_session_token = request.cookies.get("user_session", "")
    user_info = verify_user_session(user_session_token) if user_session_token else None
    user_role = str((user_info or {}).get("role") or "").strip().lower()
    is_admin = bool(user_info and user_role in {"admin", "super_admin"})
    is_manual_user = bool(user_info and user_role == "manual_user")
    is_authenticated = bool(user_info)

    # IP 级别安全检查：仅对未认证请求启用。
    client_ip = request.client.host if request.client else "unknown"
    trusted_ip = ip_blacklist.is_whitelisted(client_ip)
    if not is_authenticated and not trusted_ip:
        if ip_blacklist.is_blocked(client_ip):
            return Response(status_code=403)
        if ip_rate_limiter.is_blocked(client_ip):
            return Response(status_code=403)

    if not is_authenticated:
        if path in admin_page_paths:
            return RedirectResponse(url="/Index", status_code=302)
        if path.startswith("/api/"):
            # T849 + T922: public read-only market data APIs — accessible to guests (event detail browsing)
            _PUBLIC_API_PREFIXES = (
                "/api/pm/event/",
                "/api/pm/events",
                "/api/pm/search",
                "/api/pm/market/trades",
                "/api/pm/market/holders",
                "/api/pm/market/price_history",
                "/api/pm/orderbook",
                "/api/pm/categories",
                "/api/pm/tags",
                "/api/pm/prices/live",  # T922: realtime price ticker — public read-only by design (T316)
                "/api/oddsapi/meta",
                "/api/oddsapi/futures",
                "/api/oddsapi/event/",
            )
            if request.method == "GET" and any(path.startswith(p) for p in _PUBLIC_API_PREFIXES):
                return await call_next(request)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # T849: event detail pages allow guest browsing (read-only view)
        import re as _re
        if _re.match(r'^/(v2/)?event/[^/]+$', path):
            return await call_next(request)
            return await call_next(request)
        protected_prefixes = (
            "/v2",
            "/discover",
            "/event",
            "/deposit",
            "/withdraw",
            "/copytrader",
            "/copytrade",
            "/strategy",
            "/system",
            "/security",
            "/weathermap",
            "/weathereventmap",
            "/betslist",
            "/betlist",
            "/overview",
            "/analysis",
            "/ordermonitor",
        )
        path_l = (path or "").lower()
        if any(path_l.startswith(p) for p in protected_prefixes):
            return RedirectResponse(url="/Index", status_code=302)
        # Unknown non-API path: allow downstream routing to produce a friendly 404 page.
        return await call_next(request)

    # ── T474: manual_user 权限控制（admin/super_admin 不受限） ──
    if is_manual_user and not is_admin:
        _ADMIN_ONLY_PAGES = frozenset(
            [
                "/Strategy",
                "/System",
                "/Security",
            ]
        )
        _ADMIN_ONLY_API_PREFIXES = (
            "/api/config",
            "/api/system",
            "/api/security",
            "/api/bot",
            "/api/wallet/",
            "/api/pm/i18n",
            "/api/env",
            "/api/admin/",
        )
        if path in _ADMIN_ONLY_PAGES:
            return RedirectResponse(url="/v2", status_code=302)
        if any(path.startswith(p) for p in _ADMIN_ONLY_API_PREFIXES):
            return JSONResponse(
                {"error": "permission denied", "role": "manual_user"},
                status_code=403,
            )

    return await call_next(request)


# ── 注册路由 ──────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(backtest_router)
app.include_router(data_router)
app.include_router(config_router)
app.include_router(system_router)
app.include_router(security_router)
app.include_router(wallet_router)
app.include_router(user_router)  # T257: 公开用户认证路由
app.include_router(menu_router)  # T302: 应用菜单管理
app.include_router(frontend_banner_router)  # T561 W5-D
app.include_router(i18n_dynamic_router)  # T599: /api/i18n/dynamic
app.include_router(i18n_admin_router)  # T604: /api/admin/i18n/*
app.include_router(leaderboard_router)  # T610B: /api/leaderboard/points
app.include_router(valuelab_router)  # T735: /api/valuelab/*
app.include_router(sportslive_router)  # T557 W2: /api/sportslive/*
app.include_router(search_router)  # T872: /api/pm/search + /v2/search
app.include_router(search_alias_router)  # T872: /search alias
from scripts.dashboard_routes.oddsapi_routes import (
    router as oddsapi_router,
    alias_router as oddsapi_alias_router,
)
app.include_router(oddsapi_router)  # T873: /api/oddsapi/*
app.include_router(oddsapi_alias_router)  # T873: /futures + /v2/futures
app.include_router(polymarket_manual_router)
app.include_router(discover_router)  # T289: PM 事件发现/详情/下单
app.include_router(price_monitor_router)
app.include_router(weather_city_router)
app.include_router(tracking_router)
app.include_router(copytrade_router)
app.include_router(copytrader_v2_router)
app.include_router(map_router)
app.include_router(weather_map_router)
app.include_router(points_router)  # T265+T267+T268: 积分/推荐/手续费
app.include_router(coin_drop_router)  # T838: 弹窗探宝金币收集激励
app.include_router(frontend_router)  # T278: V2 前端（TailAdmin 暗色主题）
app.include_router(
    frontend_alias_router
)  # T290: /discover, /event/{slug}, /deposit 别名
app.include_router(help_pages_router)  # T287: 帮助手册管理（/go 等）
app.include_router(homepage_router)
app.include_router(ecosystem_router)  # T509: Ecosystem nav + PM Builders admin
app.include_router(redeem_withdraw_router)  # T460: 赎回/提现/Vault API


# ── T510 P1-1 / P1-2: 启动时拉起 PM Market & Sports WS 后台桥 ──────────
@app.on_event("startup")
async def _t510_start_pm_ws_bridges() -> None:
    try:
        from src.services import pm_market_ws as _mws

        _mws.start()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[T510] pm_market_ws start failed: %s", exc)
    try:
        from src.services import pm_sports_ws as _sws

        _sws.start()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[T510] pm_sports_ws start failed: %s", exc)
    try:
        from src.services import pm_user_ws as _uws

        _uws.start()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[T510] pm_user_ws start failed: %s", exc)


# ── T874: 每日 03:00 UTC OddsPapi settlement backfill 后台任务 ──────────
# 目的：自动回填 archive 中无 settlement 的事件，避免每次需要管理员手动触
# 发。配额安全：limit=100，仅扫描缺 settlement 的事件，遵循 odds_fetcher
# 自带的速率限制；不影响常规 ambient API 调用频率。
import asyncio as _t874_asyncio
import datetime as _t874_dt


async def _t874_settlement_backfill_loop() -> None:
    """每日 03:00 UTC 触发一次 settlement 回填，limit=100。"""
    from src.utils import odds_fetcher as of
    from src.utils import oddsapi_event_store as es

    while True:
        try:
            now = _t874_dt.datetime.now(_t874_dt.timezone.utc)
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if target <= now:
                target = target + _t874_dt.timedelta(days=1)
            wait_s = (target - now).total_seconds()
            await _t874_asyncio.sleep(max(60.0, wait_s))
        except Exception as exc:
            logger.warning("[T874 backfill] sleep failed: %s", exc)
            await _t874_asyncio.sleep(3600)
            continue

        try:
            items = es.list_archive(sport_key=None)
            pending = [it for it in items if not it.get("has_settlement")][:100]
            n_ok = 0
            n_fail = 0
            for it in pending:
                sk = it.get("sport_key") or ""
                fid = it.get("fixture_id") or ""
                if not sk or not fid:
                    continue
                try:
                    st = of.fetch_fixture_settlement(str(fid))
                except Exception:
                    n_fail += 1
                    continue
                if st is None:
                    n_fail += 1
                    continue
                try:
                    es.attach_settlement(sk, str(fid), st)
                    n_ok += 1
                except Exception:
                    n_fail += 1
            logger.info(
                "[T874 backfill] daily run scanned=%d attached=%d failed=%d",
                len(pending),
                n_ok,
                n_fail,
            )
        except Exception as exc:
            logger.warning("[T874 backfill] run failed: %s", exc)


@app.on_event("startup")
async def _t874_start_settlement_backfill() -> None:
    try:
        _t874_asyncio.create_task(_t874_settlement_backfill_loop())
        logger.info("[T874] OddsPapi settlement backfill loop scheduled (daily 03:00 UTC, limit=100)")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[T874] settlement backfill scheduling failed: %s", exc)


# ── T510: WS 桥可观测性端点（仅本进程内存视图）────────────────────────
from fastapi.responses import JSONResponse as _T510JSONResponse  # noqa: E402


@app.get("/api/internal/ws-bridge/stats", include_in_schema=False)
async def _t510_ws_bridge_stats(request: Request):
    # 鉴权：只允许已登录用户/内部健康检查；普通公网请求经同源守卫已被拒
    info: dict = {}
    try:
        from src.services import pm_market_ws as _mws

        info["market"] = _mws.stats()
    except Exception as exc:
        info["market"] = {"error": str(exc)}
    try:
        from src.services import pm_sports_ws as _sws

        info["sports"] = _sws.stats()
    except Exception as exc:
        info["sports"] = {"error": str(exc)}
    try:
        from src.services import pm_user_ws as _uws

        info["user"] = _uws.stats()
    except Exception as exc:
        info["user"] = {"error": str(exc)}
    return _T510JSONResponse(info)


@app.get("/api/internal/ws-bridge/price/{token_id}", include_in_schema=False)
async def _t510_ws_bridge_price(token_id: str):
    try:
        from src.services import pm_market_ws as _mws

        rec = _mws.get_live_price(token_id)
    except Exception as exc:
        return _T510JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    if rec is None:
        return _T510JSONResponse(
            {"ok": False, "reason": "not_tracked"}, status_code=404
        )
    return _T510JSONResponse({"ok": True, "token_id": token_id, **rec})


# ── T257: / 和 /Index 路由（T855：根路径切换为 helmetz 风格 Landing）─────
# `/`     → 全新 Landing 页（5 TAB 旋转卡片，宣传 Veyra 与 4 大业务）
# `/Index` → 既有公开登录页（Privy 登录入口）
@app.get("/", response_class=HTMLResponse)
@app.head("/")
async def landing(request: Request):
    """T855: helmetz-cloned Landing — 5 旋转卡片承载 Veyra 与 4 大业务故事。"""
    if request.method == "HEAD":
        return Response(status_code=200, media_type="text/html")
    # Lazy import to avoid circular deps; reuse V2 templates + i18n context.
    from scripts.dashboard_routes.frontend_routes import (
        templates as _v2_templates,
        _tpl_ctx as _v2_tpl_ctx,
    )
    resp = _v2_templates.TemplateResponse(
        request, "landing.html", _v2_tpl_ctx(request)
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/Index", response_class=HTMLResponse)
@app.head("/Index")
async def index_login(request: Request):
    """T474: 公开登录页 — 所有用户（含管理员）通过 Privy 登录。"""
    if request.method == "HEAD":
        return Response(status_code=200, media_type="text/html")
    return _serve_static_html("index.html")


@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
async def admin_alias_page(request: Request):
    """Legacy admin alias is intentionally unavailable on the user domain."""
    return Response(status_code=404)


# ── T244: 专业天气世界地图看板（旁路挂载） ────────────────────────────
@app.get("/WeatherMap", response_class=HTMLResponse)
async def weather_map_page(request: Request):
    """Legacy alias: redirect to V2 weather map page."""
    return RedirectResponse(url="/WeatherEventMap", status_code=302)


@app.get("/Betslist", response_class=HTMLResponse)
async def bets_list_page(request: Request):
    """Legacy alias: redirect to V2 trades page."""
    return RedirectResponse(url="/v2/trades", status_code=302)


@app.get("/Betlist", response_class=HTMLResponse)
async def bet_list_page_alias(request: Request):
    """Legacy alias: redirect to V2 trades page."""
    return RedirectResponse(url="/v2/trades", status_code=302)


@app.get("/Overview", response_class=HTMLResponse)
async def overview_page(request: Request):
    return RedirectResponse(url="/v2", status_code=302)


@app.get("/Copytraders", response_class=HTMLResponse)
async def copytraders_page(request: Request):
    return RedirectResponse(url="/Copytrader", status_code=302)


@app.get("/Analysis", response_class=HTMLResponse)
async def analysis_page(request: Request):
    return RedirectResponse(url="/v2", status_code=302)


@app.get("/Ordermonitor", response_class=HTMLResponse)
async def ordermonitor_page(request: Request):
    return RedirectResponse(url="/v2", status_code=302)


@app.get("/Polymarket", response_class=HTMLResponse)
async def polymarket_page(request: Request):
    return RedirectResponse(url="/discover", status_code=302)


@app.get("/Users", response_class=HTMLResponse)
async def users_page(request: Request):
    return RedirectResponse(url="/v2/user", status_code=302)


@app.get("/Strategy", response_class=HTMLResponse)
async def strategy_page(request: Request):
    return RedirectResponse(url="/v2/config", status_code=302)


@app.get("/System", response_class=HTMLResponse)
async def system_page(request: Request):
    return RedirectResponse(url="/v2", status_code=302)


@app.get("/Security", response_class=HTMLResponse)
async def security_page(request: Request):
    return RedirectResponse(url="/v2", status_code=302)


@app.get("/WalletBind", response_class=HTMLResponse)
async def wallet_bind_page(request: Request):
    return RedirectResponse(url="/v2/go", status_code=302)
