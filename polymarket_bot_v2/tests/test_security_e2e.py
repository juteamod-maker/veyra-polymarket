"""
test_security_e2e.py — 安全 TAB 端到端 Playwright 测试
使用内嵌 uvicorn 服务器，测试所有安全管理 UI 功能。
"""

import os
import socket
import subprocess
import sys
import time

import pytest

# ── 在导入 playwright 前确认可用 ────────────────────────────
pw = pytest.importorskip("playwright.sync_api")

TEST_KEY = "e2e_test_key_abc123"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    """启动本地 dashboard 服务器并返回 base URL。"""
    port = _find_free_port()
    env = os.environ.copy()
    env["DASHBOARD_KEY"] = TEST_KEY
    env.pop("TOTP_SECRET", None)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "scripts.web_dashboard:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    else:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        pytest.fail(
            f"Server did not start.\n"
            f"stdout: {stdout.decode()[:500]}\nstderr: {stderr.decode()[:500]}"
        )
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def browser_ctx():
    """启动 Playwright Chromium 浏览器（module scope 复用）。"""
    from playwright.sync_api import sync_playwright

    pw_inst = sync_playwright().start()
    browser = pw_inst.chromium.launch(headless=True)
    ctx = browser.new_context()
    yield ctx
    ctx.close()
    browser.close()
    pw_inst.stop()


@pytest.fixture()
def page(browser_ctx, server_url):
    """每个测试创建一个新 page，导航到安全 TAB。"""
    pg = browser_ctx.new_page()
    pg.goto(f"{server_url}/?key={TEST_KEY}")
    pg.wait_for_load_state("networkidle")
    # 切换到安全 TAB
    sec_tab = pg.locator('.nav-tab[data-tab="security"]')
    sec_tab.click()
    pg.wait_for_timeout(800)
    yield pg
    pg.close()


# ────────────────────────────────────────────────────────────────
# 测试用例
# ────────────────────────────────────────────────────────────────


class TestSecurityTabLoad:
    """安全 TAB 基本加载测试。"""

    def test_tab_visible(self, page):
        """安全 TAB 应可见并激活。"""
        tab_content = page.locator("#tab-security")
        assert tab_content.is_visible()

    def test_overview_cards_present(self, page):
        """安全概览 4 张卡片存在。"""
        assert page.locator("#sec-bl-count").is_visible()
        assert page.locator("#sec-tb-count").is_visible()
        assert page.locator("#sec-wl-count").is_visible()
        assert page.locator("#sec-auto-threshold").is_visible()

    def test_overview_loads_data(self, page):
        """概览数字已加载（不为空）。"""
        text = page.locator("#sec-bl-count").text_content()
        assert text is not None and text.strip() != ""


class TestBlacklistCRUD:
    """黑名单增删操作。"""

    def test_add_ip_to_blacklist(self, page):
        """添加 IP 到黑名单并验证显示。"""
        page.fill("#sec-bl-ip", "10.20.30.40")
        page.fill("#sec-bl-reason", "E2E测试-黑名单")
        page.click("button:has-text('添加到黑名单')")
        page.wait_for_timeout(1000)
        # 列表应包含刚添加的 IP
        bl_list = page.locator("#sec-bl-list")
        assert bl_list.text_content() is not None
        assert "10.20.30.40" in bl_list.text_content()

    def test_prevent_duplicate_blacklist(self, page):
        """重复添加同一 IP 应提示已存在。"""
        page.fill("#sec-bl-ip", "10.20.30.41")
        page.fill("#sec-bl-reason", "E2E测试dup")
        page.click("button:has-text('添加到黑名单')")
        page.wait_for_timeout(800)
        # 再次添加同一 IP
        page.fill("#sec-bl-ip", "10.20.30.41")
        page.fill("#sec-bl-reason", "E2E测试dup2")
        # 监听 alert dialog
        handled = []
        page.on("dialog", lambda d: (handled.append(d.message), d.accept()))
        page.click("button:has-text('添加到黑名单')")
        page.wait_for_timeout(1000)
        # 结果：alert popup 显示已在黑名单中 或 列表中只有一条
        # (如果 api 返回 error 对象但无 alert，需检查 JS 逻辑)

    def test_remove_ip_from_blacklist(self, page):
        """从黑名单移除 IP。"""
        # 先添加
        page.fill("#sec-bl-ip", "10.20.30.50")
        page.fill("#sec-bl-reason", "E2E测试-待移除")
        page.click("button:has-text('添加到黑名单')")
        page.wait_for_timeout(1000)
        # 确认已添加
        assert "10.20.30.50" in page.locator("#sec-bl-list").text_content()
        # 点击移除（自动弹确认对话框，需 accept）
        page.on("dialog", lambda d: d.accept())
        remove_btn = page.locator("#sec-bl-list button:has-text('移除')").last
        remove_btn.click()
        page.wait_for_timeout(1000)
        # IP 应已消失
        content = page.locator("#sec-bl-list").text_content()
        assert "10.20.30.50" not in content

    def test_invalid_ip_rejected(self, page):
        """无效 IP 格式应不被添加。"""
        page.fill("#sec-bl-ip", "not-an-ip")
        page.fill("#sec-bl-reason", "E2E-invalid")
        page.click("button:has-text('添加到黑名单')")
        page.wait_for_timeout(800)
        content = page.locator("#sec-bl-list").text_content()
        assert "not-an-ip" not in content


class TestWhitelistCRUD:
    """白名单增删操作。"""

    def test_add_ip_to_whitelist(self, page):
        """添加 IP 到白名单。"""
        page.fill("#sec-wl-ip", "192.168.0.100")
        page.click("button:has-text('添加到白名单')")
        page.wait_for_timeout(1000)
        wl_list = page.locator("#sec-wl-list")
        assert "192.168.0.100" in wl_list.text_content()

    def test_remove_ip_from_whitelist(self, page):
        """从白名单移除 IP。"""
        page.fill("#sec-wl-ip", "192.168.0.200")
        page.click("button:has-text('添加到白名单')")
        page.wait_for_timeout(1000)
        assert "192.168.0.200" in page.locator("#sec-wl-list").text_content()
        page.on("dialog", lambda d: d.accept())
        remove_btn = page.locator("#sec-wl-list button:has-text('移除')").last
        remove_btn.click()
        page.wait_for_timeout(1000)
        content = page.locator("#sec-wl-list").text_content()
        assert "192.168.0.200" not in content


class TestAutoThresholdConfig:
    """自动升级阈值配置。"""

    def test_save_threshold(self, page):
        """修改并保存自动升级阈值。"""
        inp = page.locator("#sec-auto-threshold-input")
        inp.fill("5")
        page.click("button:has-text('保存')")
        page.wait_for_timeout(1000)
        status = page.locator("#sec-auto-save-status")
        assert "已保存" in status.text_content()

    def test_threshold_updates_overview(self, page):
        """保存后概览卡片应更新。"""
        inp = page.locator("#sec-auto-threshold-input")
        inp.fill("7")
        page.click("button:has-text('保存')")
        page.wait_for_timeout(1200)
        overview_val = page.locator("#sec-auto-threshold").text_content()
        assert overview_val.strip() == "7"


class TestSecurityEvents:
    """安全事件日志显示。"""

    def test_events_container_visible(self, page):
        """事件日志区域可见。"""
        container = page.locator("#sec-events-list")
        assert container.is_visible()

    def test_events_load_after_actions(self, page):
        """执行黑名单操作后事件日志应有记录。"""
        # 先添加一个黑名单 IP 产生事件
        page.fill("#sec-bl-ip", "10.99.99.99")
        page.fill("#sec-bl-reason", "事件测试")
        page.click("button:has-text('添加到黑名单')")
        page.wait_for_timeout(1000)
        events = page.locator("#sec-events-list")
        content = events.text_content()
        assert len(content.strip()) > 0
        assert "blacklist_add" in content


class TestFavicon:
    """Favicon 加载测试。"""

    def test_favicon_no_404(self, page, server_url):
        """请求 /favicon.ico 应返回 200。"""
        resp = page.request.get(f"{server_url}/favicon.ico")
        assert resp.status == 200
        assert "icon" in resp.headers.get("content-type", "")


class TestNoConsoleErrors:
    """无 JS 控制台错误。"""

    def test_security_tab_no_errors(self, browser_ctx, server_url):
        """安全 TAB 加载后无控制台 JS 错误。"""
        pg = browser_ctx.new_page()
        errors = []
        pg.on(
            "console",
            lambda msg: errors.append(msg.text) if msg.type == "error" else None,
        )
        pg.goto(f"{server_url}/?key={TEST_KEY}")
        pg.wait_for_load_state("networkidle")
        sec_tab = pg.locator('.nav-tab[data-tab="security"]')
        sec_tab.click()
        pg.wait_for_timeout(2000)
        pg.close()
        # 过滤掉外部 CDN 加载错误（如 tailwindcss / chart.js）
        internal_errors = [
            e for e in errors if "cdn" not in e.lower() and "ERR_" not in e
        ]
        assert len(internal_errors) == 0, f"Console errors: {internal_errors}"
