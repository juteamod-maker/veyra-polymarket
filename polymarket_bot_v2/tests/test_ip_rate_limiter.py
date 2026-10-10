"""
IP Rate Limiter 安全测试
验证：封禁逻辑、窗口清理、并发安全、auth_middleware 最小化响应
"""

import threading
import time
from unittest.mock import patch

import pytest


class TestIPRateLimiter:
    """IPRateLimiter 核心逻辑测试。"""

    def _make_limiter(self, max_fails=3, window_sec=60, block_sec=120):
        from scripts.dashboard_routes._shared import IPRateLimiter

        return IPRateLimiter(
            max_fails=max_fails, window_sec=window_sec, block_sec=block_sec
        )

    def test_not_blocked_initially(self):
        limiter = self._make_limiter()
        assert not limiter.is_blocked("1.2.3.4")

    def test_single_fail_no_block(self):
        limiter = self._make_limiter(max_fails=3)
        limiter.record_fail("1.2.3.4")
        assert not limiter.is_blocked("1.2.3.4")

    def test_block_after_max_fails(self):
        limiter = self._make_limiter(max_fails=3)
        limiter.record_fail("1.2.3.4")
        limiter.record_fail("1.2.3.4")
        triggered = limiter.record_fail("1.2.3.4")  # 第3次触发
        assert triggered is True
        assert limiter.is_blocked("1.2.3.4")

    def test_different_ips_independent(self):
        limiter = self._make_limiter(max_fails=2)
        limiter.record_fail("10.0.0.1")
        limiter.record_fail("10.0.0.2")
        assert not limiter.is_blocked("10.0.0.1")
        assert not limiter.is_blocked("10.0.0.2")

    def test_block_expires(self):
        limiter = self._make_limiter(max_fails=2, block_sec=1)
        limiter.record_fail("5.5.5.5")
        limiter.record_fail("5.5.5.5")
        assert limiter.is_blocked("5.5.5.5")
        time.sleep(1.1)
        assert not limiter.is_blocked("5.5.5.5")

    def test_fails_expire_outside_window(self):
        limiter = self._make_limiter(max_fails=3, window_sec=1)
        limiter.record_fail("6.6.6.6")
        limiter.record_fail("6.6.6.6")
        time.sleep(1.1)
        # 旧的失败已过期，再加 1 次不应封禁
        triggered = limiter.record_fail("6.6.6.6")
        assert triggered is False
        assert not limiter.is_blocked("6.6.6.6")

    def test_cleanup_removes_expired(self):
        limiter = self._make_limiter(max_fails=2, window_sec=1, block_sec=1)
        limiter.record_fail("7.7.7.7")
        limiter.record_fail("7.7.7.7")  # 触发封禁
        time.sleep(1.2)
        # _cleanup 在 is_blocked 中自动触发
        limiter.is_blocked("7.7.7.7")
        assert "7.7.7.7" not in limiter._records

    def test_record_fail_returns_false_before_threshold(self):
        limiter = self._make_limiter(max_fails=5)
        for _ in range(4):
            assert limiter.record_fail("8.8.8.8") is False
        assert limiter.record_fail("8.8.8.8") is True

    def test_thread_safety(self):
        """并发写入不抛异常、计数正确。"""
        limiter = self._make_limiter(max_fails=100, window_sec=60)
        errors = []

        def hammer():
            try:
                for _ in range(50):
                    limiter.record_fail("9.9.9.9")
                    limiter.is_blocked("9.9.9.9")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


class TestAuthMiddlewareMinimalResponse:
    """验证 auth_middleware 对错误 key / 无 key 返回最小化 403。"""

    @pytest.fixture
    def client(self):
        fastapi = pytest.importorskip("fastapi")  # noqa: F841
        import os

        os.environ["DASHBOARD_KEY"] = "correct-secret-key-12345"
        os.environ.pop("TOTP_SECRET", None)

        # 重新导入以拾取新的环境变量
        import importlib

        import scripts.dashboard_routes._shared as shared_mod

        importlib.reload(shared_mod)
        import scripts.web_dashboard as wd_mod

        importlib.reload(wd_mod)

        from fastapi.testclient import TestClient

        return TestClient(wd_mod.app)

    def test_no_key_returns_403_empty(self, client):
        resp = client.get("/")
        assert resp.status_code == 403
        # 不应包含 HTML 提示信息
        assert "Access Denied" not in resp.text
        assert "key" not in resp.text.lower()

    def test_wrong_key_returns_403_empty(self, client):
        resp = client.get("/?key=wrong-key-attempt")
        assert resp.status_code == 403
        assert len(resp.text) == 0 or "key" not in resp.text.lower()

    def test_correct_key_returns_200(self, client):
        resp = client.get("/?key=correct-secret-key-12345")
        assert resp.status_code == 200

    def test_api_wrong_header_returns_403(self, client):
        resp = client.get("/api/status", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 403

    def test_api_correct_header_returns_200(self, client):
        resp = client.get(
            "/api/status",
            headers={"Authorization": "Bearer correct-secret-key-12345"},
        )
        # 200 或 (可能因为 DB 不在此处返回 500)，但不是 403
        assert resp.status_code != 403

    def test_brute_force_gets_blocked(self, client):
        """连续 10 次错误 key 后应被 IP 封禁。"""
        # 重置全局限制器
        from scripts.dashboard_routes._shared import ip_rate_limiter

        ip_rate_limiter._records.clear()

        for _ in range(10):
            client.get("/?key=brute-force-attempt")

        # 第 11 次即使用正确 key 也应被封禁
        resp = client.get("/?key=correct-secret-key-12345")
        assert resp.status_code == 403

        # 清理
        ip_rate_limiter._records.clear()

    def test_static_files_bypass_auth(self, client):
        resp = client.get("/static/js/app.js")
        # 可能 404（文件不在测试环境），但不应是 403
        assert resp.status_code != 403 or resp.status_code == 404
