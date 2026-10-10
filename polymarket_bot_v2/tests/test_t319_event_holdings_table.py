"""T319: event_detail.html 持仓表 helper 函数集成性测试。

由于 helpers 是 JS 写在 template 中，这里只能验证 holdings API 的契约稳定。
真正的 helper 验证靠 Playwright E2E。
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def fake_request():
    """模拟一个携带认证身份的 Request 对象。"""
    req = MagicMock()
    return req


def _call_holdings(req, token_id=""):
    import asyncio
    from scripts.dashboard_routes import discover_routes as dr
    return asyncio.get_event_loop().run_until_complete(
        dr.pm_user_holdings(request=req, token_id=token_id)
    )


def _identity_ok():
    return {"wallet_address": "0xUserWallet", "is_admin": False}


def _make_resp(json_payload):
    m = MagicMock()
    m.status_code = 200
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value=json_payload)
    return m


def test_holdings_returns_positions_list(fake_request):
    """T319 契约: 无 token_id 时返回 positions[] 数组，每项含 condition_id, outcome_index, title。"""
    fake_positions = [
        {
            "asset": "111", "conditionId": "0xabc",
            "size": 2.5, "avgPrice": 0.45, "currentValue": 1.30,
            "outcome": "YES", "outcomeIndex": 0,
            "title": "Tokyo 19°C", "slug": "tokyo-19c",
        },
        {
            "asset": "222", "conditionId": "0xdef",
            "size": 0.12, "avgPrice": 0.08, "currentValue": 0.01,
            "outcome": "NO", "outcomeIndex": 1,
            "title": "Tokyo 20°C", "slug": "tokyo-20c",
        },
    ]
    with patch("scripts.dashboard_routes.discover_routes.get_request_identity", return_value=_identity_ok()), \
         patch("scripts.dashboard_routes.discover_routes._get_user_funder", return_value="0xfunder"), \
         patch("scripts.dashboard_routes.discover_routes.requests.get") as m_get:
        m_get.return_value = _make_resp(fake_positions)
        result = _call_holdings(fake_request)
        assert result["status"] == "ok"
        assert result["count"] == 2
        # T319 关键字段
        positions = result["positions"]
        assert positions[0]["condition_id"] == "0xabc"
        assert positions[0]["outcome_index"] == 0
        assert positions[0]["shares"] == 2.5
        assert positions[0]["avg_price"] == 0.45
        assert positions[0]["title"] == "Tokyo 19°C"
        # 第二个不同 condition_id (验证两个市场会被分开识别)
        assert positions[1]["condition_id"] == "0xdef"
        assert positions[0]["condition_id"] != positions[1]["condition_id"]


def test_holdings_403_for_admin(fake_request):
    """T319: 管理员 / 未登录用户拿不到持仓 (前端应静默不显示)。"""
    with patch("scripts.dashboard_routes.discover_routes.get_request_identity", return_value={"is_admin": True}):
        result = _call_holdings(fake_request)
        assert hasattr(result, "status_code")
        assert result.status_code == 403


def test_holdings_412_when_no_funder(fake_request):
    """T319: 用户未绑定 funder_address 时 412, 前端应显示提示。"""
    with patch("scripts.dashboard_routes.discover_routes.get_request_identity", return_value=_identity_ok()), \
         patch("scripts.dashboard_routes.discover_routes._get_user_funder", return_value=""):
        result = _call_holdings(fake_request)
        assert hasattr(result, "status_code")
        assert result.status_code == 412


def test_holdings_distinct_markets_not_merged(fake_request):
    """T319 SUPREME 1 关键: Tokyo 19°C YES 和 Tokyo 20°C NO 是两个独立持仓,
       绝不能被合并。response 中必须保留两条独立 condition_id。"""
    fake_positions = [
        {"asset": "T1", "conditionId": "0xtokyoA", "size": 2.24, "avgPrice": 0.50,
         "outcome": "YES", "outcomeIndex": 0, "title": "Tokyo 19°C", "slug": "x"},
        {"asset": "T2", "conditionId": "0xtokyoB", "size": 0.12, "avgPrice": 0.08,
         "outcome": "NO", "outcomeIndex": 1, "title": "Tokyo 20°C", "slug": "y"},
    ]
    with patch("scripts.dashboard_routes.discover_routes.get_request_identity", return_value=_identity_ok()), \
         patch("scripts.dashboard_routes.discover_routes._get_user_funder", return_value="0xfunder"), \
         patch("scripts.dashboard_routes.discover_routes.requests.get") as m_get:
        m_get.return_value = _make_resp(fake_positions)
        result = _call_holdings(fake_request)
        assert result["count"] == 2
        cids = {p["condition_id"] for p in result["positions"]}
        assert "0xtokyoA" in cids and "0xtokyoB" in cids
        # 验证不会因 outcome=YES/NO 而被合并：两个 condition_id 不同 → 必须独立
        assert len(cids) == 2
