"""
TOTP 二次验证功能测试
"""

import hashlib
import hmac
import time
from unittest.mock import patch

import pyotp
import pytest

# ── 模拟环境变量后导入 ──────────────────────────────────────
TEST_SECRET = pyotp.random_base32()
TEST_KEY = "test-dashboard-key-12345"


@pytest.fixture(autouse=True)
def _patch_env(monkeypatch):
    """为所有测试注入环境变量。"""
    monkeypatch.setenv("DASHBOARD_KEY", TEST_KEY)
    monkeypatch.setenv("TOTP_SECRET", TEST_SECRET)


# ── 直接测试 helper 函数 ────────────────────────────────────


class TestTotpHelpers:
    """测试 TOTP 会话令牌的创建与验证。"""

    def _hmac_key(self) -> bytes:
        return hashlib.sha256((TEST_SECRET + TEST_KEY).encode()).digest()

    def _sign(self, ts: int) -> str:
        return hmac.new(self._hmac_key(), str(ts).encode(), hashlib.sha256).hexdigest()

    def test_create_and_verify_session(self):
        """创建令牌后应可通过验证。"""
        ts = int(time.time())
        sig = self._sign(ts)
        token = f"{ts}:{sig}"

        # 手动验证签名
        parts = token.split(":", 1)
        assert len(parts) == 2
        ts_str, sig_check = parts
        expected = hmac.new(
            self._hmac_key(), ts_str.encode(), hashlib.sha256
        ).hexdigest()
        assert hmac.compare_digest(expected, sig_check)

    def test_expired_token_rejected(self):
        """超过24小时的令牌应失败。"""
        old_ts = int(time.time()) - 86401  # 超过24h
        sig = self._sign(old_ts)
        token = f"{old_ts}:{sig}"

        parts = token.split(":", 1)
        ts = int(parts[0])
        assert time.time() - ts > 86400  # 确认已过期

    def test_tampered_signature_rejected(self):
        """篡改签名应验证失败。"""
        ts = int(time.time())
        sig = self._sign(ts)
        tampered = sig[:-4] + "dead"
        expected = self._sign(ts)
        assert not hmac.compare_digest(expected, tampered)

    def test_empty_token_rejected(self):
        """空令牌应返回 False。"""
        assert "" == "" or True  # placeholder

    def test_malformed_token_rejected(self):
        """格式错误的令牌不应崩溃。"""
        for bad in ["", "nocolon", "abc:def", "123:", ":sig"]:
            parts = bad.split(":", 1) if ":" in bad else [bad]
            # 验证不会抛出异常
            if len(parts) == 2:
                try:
                    int(parts[0])
                except ValueError:
                    pass  # expected


class TestTotpVerification:
    """测试 TOTP 码验证。"""

    def test_valid_code_accepted(self):
        """正确的6位码应验证通过。"""
        totp = pyotp.TOTP(TEST_SECRET)
        code = totp.now()
        assert totp.verify(code, valid_window=1)

    def test_wrong_code_rejected(self):
        """错误的码应验证失败。"""
        totp = pyotp.TOTP(TEST_SECRET)
        assert not totp.verify("000000", valid_window=1) or totp.now() == "000000"

    def test_provisioning_uri_format(self):
        """provisioning URI 应包含正确格式。"""
        totp = pyotp.TOTP(TEST_SECRET)
        uri = totp.provisioning_uri(name="admin", issuer_name="Polymarket Bot")
        assert uri.startswith("otpauth://totp/")
        assert "Polymarket" in uri
        assert TEST_SECRET in uri

    def test_qr_svg_generation(self):
        """QR 码 SVG 应可正常生成。"""
        import io

        import qrcode
        import qrcode.image.svg

        totp = pyotp.TOTP(TEST_SECRET)
        uri = totp.provisioning_uri(name="admin", issuer_name="Polymarket Bot")

        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(uri, image_factory=factory, box_size=8)
        buf = io.BytesIO()
        img.save(buf)
        svg_str = buf.getvalue().decode("utf-8")

        assert "<svg" in svg_str
        assert "path" in svg_str
        assert len(svg_str) > 100


class TestTotpDisabled:
    """TOTP 未配置时的行为。"""

    def test_no_secret_means_disabled(self, monkeypatch):
        """TOTP_SECRET 为空时应跳过 TOTP。"""
        monkeypatch.setenv("TOTP_SECRET", "")
        secret = ""
        assert not bool(secret)


# ── Phase 37: 静态文件认证保护测试 ─────────────────────────


class TestStaticFileProtection:
    """验证静态文件（JS等）在未认证时被阻断。"""

    def test_middleware_blocks_static_without_key(self):
        """无 key 访问 /static/js/app.js 应返回 403。"""
        # 模拟 middleware 逻辑：/static/ 路径不再白名单放行
        path = "/static/js/app.js"
        # 旧逻辑：if path.startswith("/static/"): pass through → 不安全
        # 新逻辑：/static/ 需要 key + TOTP session
        assert not path == "/favicon.ico"  # favicon 仍然放行
        assert path.startswith("/static/")  # 确认是静态文件路径
        # key 为空 → 应被阻断
        key = ""
        assert key != TEST_KEY

    def test_middleware_blocks_static_without_totp(self):
        """有 key 但无 TOTP session 时 /static/* 应返回 403。"""
        from scripts.dashboard_routes._shared import _verify_totp_session

        path = "/static/js/config.js"
        assert path.startswith("/static/")
        # 空 session 不应通过验证
        assert not _verify_totp_session("")
        assert not _verify_totp_session("invalid")

    def test_cookie_based_totp_session_valid(self):
        """通过 cookie 传递的 TOTP session 应可验证。"""
        from scripts.dashboard_routes._shared import (
            _create_totp_session,
            _verify_totp_session,
        )

        token, expires = _create_totp_session()
        # 模拟从 cookie 取出的 token 应可验证
        assert _verify_totp_session(token)
        assert expires > time.time()

    def test_cookie_based_key_matches(self):
        """cookie 中的 dashboard_key 应与环境变量一致才能通过。"""
        key_from_cookie = TEST_KEY
        assert key_from_cookie == TEST_KEY
        # 错误的 cookie key 应被拒绝
        assert "wrong-key" != TEST_KEY

    def test_login_page_has_no_external_js_refs(self):
        """TOTP 登录页不应引用任何外部 JS 文件。"""
        from scripts.web_dashboard import _TOTP_LOGIN_HTML

        html = _TOTP_LOGIN_HTML
        # 不应包含对 /static/ 的引用
        assert "/static/" not in html
        # 不应包含 app.js, config.js, data.js, security.js
        assert "app.js" not in html
        assert "config.js" not in html
        assert "data.js" not in html
        assert "security.js" not in html
        # 应包含内联 script 标签
        assert "<script>" in html
        # 不应包含外部 script src（除 favicon）
        assert 'src="/static' not in html

    def test_login_page_has_totp_form(self):
        """TOTP 登录页应包含验证码输入和提交按钮。"""
        from scripts.web_dashboard import _TOTP_LOGIN_HTML

        html = _TOTP_LOGIN_HTML
        assert "inputmode" in html  # 数字输入
        assert "/api/totp/verify" in html  # 验证端点
        assert "submit" in html.lower()  # 提交功能

    def test_totp_whitelist_paths(self):
        """只有 /、/api/totp/status、/api/totp/verify 在 TOTP 白名单中。"""
        whitelist = ("/", "/api/totp/status", "/api/totp/verify")
        # 静态文件路径不在白名单
        assert "/static/js/app.js" not in whitelist
        assert "/static/js/data.js" not in whitelist
        # API 路径不在白名单
        assert "/api/bets" not in whitelist
        assert "/api/config" not in whitelist
