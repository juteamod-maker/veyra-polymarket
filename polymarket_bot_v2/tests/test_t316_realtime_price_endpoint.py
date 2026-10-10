"""T316: /api/pm/prices/live 实时价格批量端点单元测试。

覆盖：
- 参数校验（缺失、非法、过滤非数字、限制 50 个）
- 调 CLOB /prices 成功路径 + 价格规整（buy/sell/mid 计算）
- CLOB 失败 → 优雅降级（有缓存返回 stale=true，无缓存返回 502）
- TTL 缓存命中（cached=true）
- 进程内缓存隔离不同 token 集
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import patch, MagicMock

import pytest


# 复用 sys.path 加入 scripts
import os
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _reset_cache():
    # 每个测试前清缓存，避免相互污染
    from scripts.dashboard_routes import discover_routes as dr
    dr._LIVE_PRICE_CACHE["ts"] = 0.0
    dr._LIVE_PRICE_CACHE["key"] = ""
    dr._LIVE_PRICE_CACHE["data"] = {}
    yield


def _call_endpoint(token_ids: str):
    """同步调用异步路由函数。"""
    import asyncio
    from scripts.dashboard_routes import discover_routes as dr
    return asyncio.get_event_loop().run_until_complete(dr.pm_prices_live(token_ids=token_ids))


def _make_resp(json_payload, status=200):
    m = MagicMock()
    m.status_code = status
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value=json_payload)
    return m


# ---------- 参数校验 ----------

def test_empty_token_ids_returns_400():
    resp = _call_endpoint("")
    # JSONResponse 类型
    assert hasattr(resp, "status_code")
    assert resp.status_code == 400


def test_only_non_numeric_returns_400():
    resp = _call_endpoint("abc,xyz,!!!")
    assert hasattr(resp, "status_code")
    assert resp.status_code == 400


def test_filters_non_numeric_keeps_valid():
    with patch("scripts.dashboard_routes.discover_routes.requests.post") as m_post:
        m_post.return_value = _make_resp({"123": {"BUY": "0.5", "SELL": "0.49"}})
        result = _call_endpoint("123,abc,456xyz,789")
        assert isinstance(result, dict)
        # 至少调用了一次 CLOB
        assert m_post.called
        payload = m_post.call_args.kwargs.get("json") or m_post.call_args.args[1] if m_post.call_args else None
        # 验证只送了纯数字 id（123, 789）
        sent_tokens = {p["token_id"] for p in (payload or [])}
        assert "123" in sent_tokens
        assert "789" in sent_tokens
        assert "abc" not in sent_tokens
        assert "456xyz" not in sent_tokens


def test_caps_at_50_tokens():
    many = ",".join(str(i) for i in range(1, 100))  # 99 个
    with patch("scripts.dashboard_routes.discover_routes.requests.post") as m_post:
        m_post.return_value = _make_resp({})
        _call_endpoint(many)
        payload = m_post.call_args.kwargs.get("json")
        # 50 个 token × 2 个 side = 100 条 payload
        assert len(payload) == 100


# ---------- 价格计算 ----------

def test_price_normalization_full():
    with patch("scripts.dashboard_routes.discover_routes.requests.post") as m_post:
        m_post.return_value = _make_resp({
            "111": {"BUY": "0.55", "SELL": "0.53"},
        })
        result = _call_endpoint("111")
        assert result["status"] == "ok"
        p = result["prices"]["111"]
        # CLOB BUY = best_bid (someone wants to buy at this price)
        # CLOB SELL = best_ask (someone wants to sell at this price)
        assert p["best_bid"] == 0.55
        assert p["best_ask"] == 0.53
        assert p["mid"] == 0.54


def test_price_normalization_only_buy():
    with patch("scripts.dashboard_routes.discover_routes.requests.post") as m_post:
        m_post.return_value = _make_resp({
            "222": {"BUY": "0.7"},
        })
        result = _call_endpoint("222")
        p = result["prices"]["222"]
        assert p["best_bid"] == 0.7
        assert p["best_ask"] is None
        assert p["mid"] == 0.7  # 退化为单边


def test_missing_token_returns_all_none():
    with patch("scripts.dashboard_routes.discover_routes.requests.post") as m_post:
        m_post.return_value = _make_resp({})
        result = _call_endpoint("333")
        p = result["prices"]["333"]
        assert p["best_bid"] is None and p["best_ask"] is None and p["mid"] is None


# ---------- 缓存 ----------

def test_cache_hit_within_ttl():
    with patch("scripts.dashboard_routes.discover_routes.requests.post") as m_post:
        m_post.return_value = _make_resp({"111": {"BUY": "0.5", "SELL": "0.49"}})
        r1 = _call_endpoint("111")
        r2 = _call_endpoint("111")
        # 第二次应命中缓存
        assert m_post.call_count == 1
        assert r2.get("cached") is True
        assert r1["prices"]["111"]["best_bid"] == 0.5


def test_cache_miss_different_keys():
    with patch("scripts.dashboard_routes.discover_routes.requests.post") as m_post:
        m_post.return_value = _make_resp({})
        _call_endpoint("111")
        _call_endpoint("222")
        assert m_post.call_count == 2


# ---------- 失败路径 ----------

def test_upstream_failure_no_cache_returns_502():
    with patch("scripts.dashboard_routes.discover_routes.requests.post") as m_post:
        m_post.side_effect = RuntimeError("network down")
        resp = _call_endpoint("111")
        assert hasattr(resp, "status_code")
        assert resp.status_code == 502


def test_upstream_failure_with_cache_returns_stale():
    from scripts.dashboard_routes import discover_routes as dr
    # 预先写入缓存
    dr._LIVE_PRICE_CACHE["ts"] = 1.0  # 远古时间确保 TTL 过期
    dr._LIVE_PRICE_CACHE["key"] = "111"
    dr._LIVE_PRICE_CACHE["data"] = {"111": {"best_bid": 0.5, "best_ask": 0.51, "mid": 0.505}}
    with patch("scripts.dashboard_routes.discover_routes.requests.post") as m_post:
        m_post.side_effect = RuntimeError("upstream timeout")
        result = _call_endpoint("111")
        assert isinstance(result, dict)
        assert result["status"] == "ok"
        assert result["stale"] is True
        assert result["prices"]["111"]["best_bid"] == 0.5
