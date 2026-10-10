"""
test_security.py — IP 黑名单 / 白名单 / 自动升级 / 安全事件日志 测试
"""

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def tmp_blacklist(tmp_path):
    """临时黑名单文件。"""
    return tmp_path / "ip_blacklist.json"


@pytest.fixture
def tmp_security_log(tmp_path):
    """临时安全事件日志文件。"""
    return tmp_path / "security_events.json"


@pytest.fixture
def blacklist(tmp_blacklist, tmp_security_log):
    """创建一个隔离的 IPBlacklist 实例。"""
    with patch("scripts.dashboard_routes._shared.SECURITY_LOG_PATH", tmp_security_log):
        from scripts.dashboard_routes._shared import IPBlacklist

        bl = IPBlacklist(path=tmp_blacklist)
        return bl


@pytest.fixture
def rate_limiter():
    """小参数的 rate limiter，方便测试。"""
    from scripts.dashboard_routes._shared import IPBlacklist, IPRateLimiter

    rl = IPRateLimiter(max_fails=3, window_sec=60, block_sec=10)
    return rl


# ── IPBlacklist 基本功能 ──────────────────────────────────────────────


class TestIPBlacklist:
    def test_add_and_check(self, blacklist):
        assert not blacklist.is_blocked("1.2.3.4")
        ok = blacklist.add("1.2.3.4", "测试封禁")
        assert ok is True
        assert blacklist.is_blocked("1.2.3.4")

    def test_add_duplicate(self, blacklist):
        blacklist.add("1.2.3.4", "第一次")
        ok = blacklist.add("1.2.3.4", "第二次")
        assert ok is False  # 重复添加返回 False

    def test_remove(self, blacklist):
        blacklist.add("1.2.3.4", "测试")
        assert blacklist.is_blocked("1.2.3.4")
        ok = blacklist.remove("1.2.3.4")
        assert ok is True
        assert not blacklist.is_blocked("1.2.3.4")

    def test_remove_nonexistent(self, blacklist):
        ok = blacklist.remove("9.9.9.9")
        assert ok is False

    def test_list_all(self, blacklist):
        blacklist.add("1.1.1.1", "A")
        blacklist.add("2.2.2.2", "B")
        bl = blacklist.list_all()
        assert "1.1.1.1" in bl
        assert "2.2.2.2" in bl
        assert bl["1.1.1.1"]["reason"] == "A"

    def test_persistence(self, tmp_blacklist, tmp_security_log):
        """黑名单数据应持久化到 JSON 文件。"""
        with patch(
            "scripts.dashboard_routes._shared.SECURITY_LOG_PATH", tmp_security_log
        ):
            from scripts.dashboard_routes._shared import IPBlacklist

            bl1 = IPBlacklist(path=tmp_blacklist)
            bl1.add("5.5.5.5", "持久化测试")

            # 重新创建实例，应从文件加载
            bl2 = IPBlacklist(path=tmp_blacklist)
            assert bl2.is_blocked("5.5.5.5")

    def test_corrupted_file(self, tmp_blacklist, tmp_security_log):
        """损坏文件应安全重建。"""
        tmp_blacklist.write_text("not json!!")
        with patch(
            "scripts.dashboard_routes._shared.SECURITY_LOG_PATH", tmp_security_log
        ):
            from scripts.dashboard_routes._shared import IPBlacklist

            bl = IPBlacklist(path=tmp_blacklist)
            assert not bl.is_blocked("1.1.1.1")  # 不崩溃


# ── 白名单 ────────────────────────────────────────────────────────────


class TestWhitelist:
    def test_add_whitelist(self, blacklist):
        ok = blacklist.add_whitelist("10.0.0.1")
        assert ok is True
        assert blacklist.is_whitelisted("10.0.0.1")

    def test_whitelist_prevents_blacklist(self, blacklist):
        blacklist.add_whitelist("10.0.0.1")
        ok = blacklist.add("10.0.0.1", "尝试封禁白名单IP")
        assert ok is False  # 白名单IP无法加入黑名单
        assert not blacklist.is_blocked("10.0.0.1")

    def test_whitelist_removes_from_blacklist(self, blacklist):
        blacklist.add("10.0.0.1", "先封禁")
        assert blacklist.is_blocked("10.0.0.1")
        blacklist.add_whitelist("10.0.0.1")
        assert not blacklist.is_blocked("10.0.0.1")  # 加入白名单时自动从黑名单移除

    def test_whitelist_subnet_removes_block_members(self, blacklist):
        """白名单/24或/16添加时，应自动清除同网段的黑名单IP（根本Bug修复验证）。"""
        # 模拟场景：某个 /16 网段内的 IP 先被封禁，之后添加该 /16 白名单
        blacklist.add("10.1.2.50", "先封禁同网段IP")
        blacklist.add("10.1.3.99", "先封禁同网段IP2")
        blacklist.add("10.2.0.1", "不同/16网段，不应被移除")
        assert blacklist.is_blocked("10.1.2.50")
        assert blacklist.is_blocked("10.1.3.99")
        assert blacklist.is_blocked("10.2.0.1")
        # 添加 10.1.0.1/16 白名单 → 自动清除 10.1.x.x
        blacklist.add_whitelist("10.1.0.1", mode="/16")
        assert not blacklist.is_blocked("10.1.2.50")  # 10.1.x.x 已从黑名单移除
        assert not blacklist.is_blocked("10.1.3.99")  # 同上
        assert blacklist.is_blocked("10.2.0.1")  # 不同网段，不受影响

    def test_whitelist_24_subnet_removes_block_members(self, blacklist):
        """/24白名单添加时只清除同/24网段的黑名单IP。"""
        blacklist.add("192.168.1.5", "封禁/24内")
        blacklist.add("192.168.2.5", "封禁/24外")
        blacklist.add_whitelist("192.168.1.1", mode="/24")
        assert not blacklist.is_blocked("192.168.1.5")  # 同/24，已清除
        assert blacklist.is_blocked("192.168.2.5")  # 不同/24，保留

    def test_remove_whitelist(self, blacklist):
        blacklist.add_whitelist("10.0.0.1")
        ok = blacklist.remove_whitelist("10.0.0.1")
        assert ok is True
        assert not blacklist.is_whitelisted("10.0.0.1")

    def test_get_whitelist(self, blacklist):
        blacklist.add_whitelist("10.0.0.1")
        blacklist.add_whitelist("10.0.0.2")
        wl = blacklist.get_whitelist()
        wl_ips = [w["ip"] if isinstance(w, dict) else w for w in wl]
        assert "10.0.0.1" in wl_ips
        assert "10.0.0.2" in wl_ips


# ── 自动升级阈值 ──────────────────────────────────────────────────────


class TestAutoBanThreshold:
    def test_default_threshold(self, blacklist):
        assert blacklist.get_auto_ban_threshold() == 3

    def test_set_threshold(self, blacklist):
        blacklist.set_auto_ban_threshold(5)
        assert blacklist.get_auto_ban_threshold() == 5

    def test_threshold_min_1(self, blacklist):
        blacklist.set_auto_ban_threshold(0)
        assert blacklist.get_auto_ban_threshold() == 1  # 最小为1


# ── Rate Limiter 增强功能 ─────────────────────────────────────────────


class TestRateLimiterEnhanced:
    def test_temp_ban_triggers(self, rate_limiter):
        ip = "172.233.28.142"
        for _ in range(2):
            assert rate_limiter.record_fail(ip) is False
        assert rate_limiter.record_fail(ip) is True  # 第3次触发封禁

    def test_is_blocked_after_ban(self, rate_limiter):
        ip = "172.233.28.142"
        for _ in range(3):
            rate_limiter.record_fail(ip)
        assert rate_limiter.is_blocked(ip) is True

    def test_get_temp_bans(self, rate_limiter):
        ip = "172.233.28.142"
        for _ in range(3):
            rate_limiter.record_fail(ip)
        bans = rate_limiter.get_temp_bans()
        assert len(bans) == 1
        assert bans[0]["ip"] == ip
        assert bans[0]["ban_count"] == 1

    def test_auto_promote_to_blacklist(self, tmp_blacklist, tmp_security_log):
        """临时封禁达阈值应自动升级为永久黑名单。"""
        with patch(
            "scripts.dashboard_routes._shared.SECURITY_LOG_PATH", tmp_security_log
        ):
            from scripts.dashboard_routes._shared import IPBlacklist, IPRateLimiter

            bl = IPBlacklist(path=tmp_blacklist)
            bl.set_auto_ban_threshold(2)  # 2次临时封禁→永久

            with patch("scripts.dashboard_routes._shared.ip_blacklist", bl):
                rl = IPRateLimiter(max_fails=3, window_sec=60, block_sec=1)

                ip = "172.233.28.142"
                # 第1次临时封禁
                for _ in range(3):
                    rl.record_fail(ip)
                assert not bl.is_blocked(ip)  # 1次临时 < 阈值2

                # 等待临时封禁过期
                time.sleep(1.1)

                # 第2次临时封禁 → 自动升级
                for _ in range(3):
                    rl.record_fail(ip)
                assert bl.is_blocked(ip)  # 自动加入永久黑名单

    def test_whitelist_prevents_auto_promote(self, tmp_blacklist, tmp_security_log):
        """白名单IP不应被自动升级。"""
        with patch(
            "scripts.dashboard_routes._shared.SECURITY_LOG_PATH", tmp_security_log
        ):
            from scripts.dashboard_routes._shared import IPBlacklist, IPRateLimiter

            bl = IPBlacklist(path=tmp_blacklist)
            bl.set_auto_ban_threshold(1)  # 1次就升级
            bl.add_whitelist("10.0.0.1")

            with patch("scripts.dashboard_routes._shared.ip_blacklist", bl):
                rl = IPRateLimiter(max_fails=3, window_sec=60, block_sec=10)

                for _ in range(3):
                    rl.record_fail("10.0.0.1")
                assert not bl.is_blocked("10.0.0.1")  # 白名单不升级


# ── 安全事件日志 ──────────────────────────────────────────────────────


class TestSecurityEvents:
    def test_events_logged_on_blacklist_add(self, blacklist, tmp_security_log):
        with patch(
            "scripts.dashboard_routes._shared.SECURITY_LOG_PATH", tmp_security_log
        ):
            blacklist.add("1.2.3.4", "测试事件")
            from scripts.dashboard_routes._shared import get_security_events

            events = get_security_events(10)
            assert len(events) >= 1
            assert events[0]["type"] == "blacklist_add"
            assert events[0]["ip"] == "1.2.3.4"

    def test_events_logged_on_remove(self, blacklist, tmp_security_log):
        with patch(
            "scripts.dashboard_routes._shared.SECURITY_LOG_PATH", tmp_security_log
        ):
            blacklist.add("1.2.3.4", "先添加")
            blacklist.remove("1.2.3.4")
            from scripts.dashboard_routes._shared import get_security_events

            events = get_security_events(10)
            types = [e["type"] for e in events]
            assert "blacklist_remove" in types

    def test_event_limit(self, tmp_security_log):
        """事件日志应限制最大条数。"""
        with patch(
            "scripts.dashboard_routes._shared.SECURITY_LOG_PATH", tmp_security_log
        ), patch("scripts.dashboard_routes._shared._MAX_SECURITY_EVENTS", 5):
            from scripts.dashboard_routes._shared import (
                _log_security_event,
                get_security_events,
            )

            for i in range(10):
                _log_security_event("test", f"1.0.0.{i}", f"event {i}")
            events = json.loads(tmp_security_log.read_text())
            assert len(events) == 5  # 保留最新5条


# ── 线程安全 ──────────────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_blacklist_add(self, blacklist):
        """并发添加不应崩溃。"""
        errors = []

        def add_ip(n):
            try:
                blacklist.add(f"10.0.{n}.1", f"并发测试{n}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_ip, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0
        assert len(blacklist.list_all()) == 10


# ── Security API 路由 ─────────────────────────────────────────────────


class TestSecurityAPI:
    """测试 IP 校验逻辑（直接使用 ipaddress 模块）。"""

    def test_validate_ip_valid(self):
        import ipaddress

        for ip in ("192.168.1.1", "::1", "2001:db8::1", "10.0.0.1"):
            try:
                ipaddress.ip_address(ip)
                valid = True
            except ValueError:
                valid = False
            assert valid is True, f"{ip} should be valid"

    def test_validate_ip_invalid(self):
        import ipaddress

        for ip in ("not-an-ip", "999.999.999.999", ""):
            try:
                ipaddress.ip_address(ip)
                valid = True
            except ValueError:
                valid = False
            assert valid is False, f"{ip} should be invalid"
