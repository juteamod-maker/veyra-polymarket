"""
test_smart_money.py — SmartMoneyTracker 单元测试
覆盖：缓存、buildId 提取、leaderboard 解析、holders 匹配、信号格式化、巨鲸检测
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import requests
from src.utils.smart_money import SmartMoneyTracker

# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture
def mock_config():
    """提供 smart_money 配置的 mock（新格式：纯地址数组）。"""
    config = {
        "smart_money.enabled": True,
        "smart_money.refresh_interval_hours": 6,
        "smart_money.categories": ["weather", "sports"],
        "smart_money.top_n_wallets": 5,
        "smart_money.min_pnl_usd": 5000,
        "smart_money.signal_min_wallets": 2,
        "smart_money.manual_wallets": [
            "0xaaa111aaa111aaa111aaa111aaa111aaa111aaa1",
            "0xbbb222bbb222bbb222bbb222bbb222bbb222bbb2",
            "0xccc333ccc333ccc333ccc333ccc333ccc333ccc3",
        ],
    }
    return config


@pytest.fixture
def tracker(mock_config, tmp_path):
    """创建带 mock 配置的 SmartMoneyTracker 实例。"""
    cache_file = tmp_path / "smart_money_cache.json"
    with (
        patch(
            "src.utils.smart_money.get",
            side_effect=lambda k, d=None: mock_config.get(k, d),
        ),
        patch("src.utils.smart_money._CACHE_FILE", cache_file),
        patch("src.utils.smart_money._CACHE_DIR", tmp_path),
    ):
        t = SmartMoneyTracker()
    # Re-patch get for subsequent calls
    t._get_patcher = patch(
        "src.utils.smart_money.get", side_effect=lambda k, d=None: mock_config.get(k, d)
    )
    t._get_patcher.start()
    yield t
    t._get_patcher.stop()


# ── 测试：钱包初始化 ─────────────────────────────────────


def test_init_wallets_from_manual_config(tracker):
    """手动配置的钱包应正确加载（纯地址数组格式）。"""
    assert "_global" in tracker._wallets
    assert len(tracker._wallets["_global"]) == 3
    assert (
        tracker._wallets["_global"][0]["address"]
        == "0xaaa111aaa111aaa111aaa111aaa111aaa111aaa1"
    )
    # 自动生成显示名
    assert tracker._wallets["_global"][0]["name"] == "0xaaa1...aaa1"
    assert tracker._wallets["_global"][0]["source"] == "manual"


def test_get_all_known_wallets(tracker):
    """扁平化钱包字典应包含所有手动钱包。"""
    all_wallets = tracker._get_all_known_wallets()
    assert "0xaaa111aaa111aaa111aaa111aaa111aaa111aaa1" in all_wallets
    assert "0xbbb222bbb222bbb222bbb222bbb222bbb222bbb2" in all_wallets
    assert "0xccc333ccc333ccc333ccc333ccc333ccc333ccc3" in all_wallets
    assert (
        all_wallets["0xaaa111aaa111aaa111aaa111aaa111aaa111aaa1"]["name"]
        == "0xaaa1...aaa1"
    )
    assert (
        all_wallets["0xaaa111aaa111aaa111aaa111aaa111aaa111aaa1"]["category"]
        == "_global"
    )


# ── 测试：holders 匹配 ──────────────────────────────────


def test_get_smart_context_with_match(tracker):
    """当 holders 中有已知聪明钱包时，应返回格式化的 context。"""
    holders_response = [
        {
            "token": "12345",
            "holders": [
                {
                    "proxyWallet": "0xaaa111aaa111aaa111aaa111aaa111aaa111aaa1",
                    "amount": 500,
                    "outcomeIndex": 0,
                    "name": "ColdMath",
                },
                {
                    "proxyWallet": "0xZZZ999",
                    "amount": 1000,
                    "outcomeIndex": 0,
                    "name": "RandomUser",
                },
            ],
        },
        {
            "token": "67890",
            "holders": [
                {
                    "proxyWallet": "0xbbb222bbb222bbb222bbb222bbb222bbb222bbb2",
                    "amount": 300,
                    "outcomeIndex": 1,
                    "name": "yourmymuse45",
                },
            ],
        },
    ]

    market = {
        "conditionId": "0xtest123",
        "outcomes": '["Yes", "No"]',
    }

    with patch.object(tracker, "_fetch_holders", return_value=holders_response):
        ctx = tracker.get_smart_context_for_market(market)

    assert "聪明钱包信号" in ctx
    assert "0xaaa1...aaa1" in ctx  # auto-generated display name for wallet 1
    assert "0xbbb2...bbb2" in ctx  # auto-generated display name for wallet 2
    assert "Yes" in ctx  # wallet 1 holds YES (outcomeIndex=0)
    assert "No" in ctx  # wallet 2 holds NO (outcomeIndex=1)


def test_get_smart_context_no_match(tracker):
    """当 holders 中无已知聪明钱包时，应返回空字符串。"""
    holders_response = [
        {
            "token": "12345",
            "holders": [
                {
                    "proxyWallet": "0xunknown1",
                    "amount": 500,
                    "outcomeIndex": 0,
                    "name": "Nobody",
                },
            ],
        },
    ]

    market = {"conditionId": "0xtest123", "outcomes": '["Yes", "No"]'}

    with patch.object(tracker, "_fetch_holders", return_value=holders_response):
        ctx = tracker.get_smart_context_for_market(market)

    assert ctx == ""


def test_get_smart_context_no_condition_id(tracker):
    """没有 conditionId 的市场应返回空字符串。"""
    market = {"question": "Will it rain?"}
    ctx = tracker.get_smart_context_for_market(market)
    assert ctx == ""


def test_get_smart_context_disabled(tracker):
    """smart_money.enabled=False 时应返回空字符串。"""
    with patch(
        "src.utils.smart_money.get",
        side_effect=lambda k, d=None: False if k == "smart_money.enabled" else d,
    ):
        ctx = tracker.get_smart_context_for_market({"conditionId": "0xtest"})
    assert ctx == ""


# ── 测试：holders API 调用 ────────────────────────────────


@patch("src.utils.smart_money.requests.get")
def test_fetch_holders_success(mock_get, tracker):
    """正常 /holders 响应应正确解析。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"token": "abc", "holders": [{"proxyWallet": "0x1", "amount": 100}]}
    ]
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    result = tracker._fetch_holders("0xcond123")
    assert len(result) == 1
    assert result[0]["holders"][0]["amount"] == 100


@patch("src.utils.smart_money.requests.get")
def test_fetch_holders_failure(mock_get, tracker):
    """/holders 请求失败应返回空列表。"""
    mock_get.side_effect = requests.exceptions.ConnectionError("Network error")
    result = tracker._fetch_holders("0xcond123")
    assert result == []


# ── 测试：buildId ──────────────────────────────────────────


@patch("src.utils.smart_money.requests.get")
def test_get_build_id_from_html(mock_get, tracker):
    """从 HTML 中提取 buildId。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '...{"buildId":"ABC123xyz_test",...}...'
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    tracker._cache.pop("build_id", None)
    tracker._cache.pop("build_id_ts", None)

    build_id = tracker._get_build_id(force=True)
    assert build_id == "ABC123xyz_test"
    assert tracker._cache["build_id"] == "ABC123xyz_test"


def test_get_build_id_from_cache(tracker):
    """缓存未过期时应直接返回缓存的 buildId。"""
    tracker._cache["build_id"] = "cached_id_123"
    tracker._cache["build_id_ts"] = time.time()

    build_id = tracker._get_build_id()
    assert build_id == "cached_id_123"


# ── 测试：leaderboard 解析 ────────────────────────────────


@patch("src.utils.smart_money.requests.get")
def test_discover_top_wallets(mock_get, tracker):
    """Leaderboard 数据应正确解析为钱包列表。"""
    # 第一次调用是 _get_build_id
    build_id_resp = MagicMock()
    build_id_resp.status_code = 200
    build_id_resp.text = '{"buildId":"test_id"}'
    build_id_resp.raise_for_status = MagicMock()

    # 第二次调用是 leaderboard JSON
    lb_resp = MagicMock()
    lb_resp.status_code = 200
    # Q0 = volume 排名（按 volume 降序，pnl 较低的排前面）
    # Q1 = PnL 排名（按 pnl 降序，TopTrader 真正排第一）
    lb_resp.json.return_value = {
        "pageProps": {
            "dehydratedState": {
                "queries": [
                    {
                        "state": {
                            "data": [
                                {
                                    "rank": 1,
                                    "proxyWallet": "0xVolKing",
                                    "name": "VolumeKing",
                                    "pnl": 8000,
                                    "volume": 500000,
                                },
                                {
                                    "rank": 2,
                                    "proxyWallet": "0xWallet1",
                                    "name": "TopTrader",
                                    "pnl": 50000,
                                    "volume": 200000,
                                },
                            ]
                        }
                    },
                    {
                        "state": {
                            "data": [
                                {
                                    "rank": 1,
                                    "proxyWallet": "0xWallet1",
                                    "name": "TopTrader",
                                    "pnl": 50000,
                                    "volume": 200000,
                                },
                                {
                                    "rank": 2,
                                    "proxyWallet": "0xWallet2",
                                    "name": "SecondBest",
                                    "pnl": 20000,
                                    "volume": 100000,
                                },
                                {
                                    "rank": 3,
                                    "proxyWallet": "0xWallet3",
                                    "name": "SmallFish",
                                    "pnl": 1000,
                                    "volume": 5000,
                                },
                            ]
                        }
                    },
                ]
            }
        }
    }
    lb_resp.raise_for_status = MagicMock()

    mock_get.side_effect = [lb_resp]

    tracker._cache["build_id"] = "test_id"
    tracker._cache["build_id_ts"] = time.time()

    wallets = tracker._discover_top_wallets("weather", top_n=5, min_pnl=5000)
    # Should pick Q1 (PnL ranking), NOT Q0 (volume ranking)
    # Q0 first entry VolumeKing pnl=8000 < Q1 first entry TopTrader pnl=50000
    # SmallFish (pnl=1000) should be filtered out by min_pnl=5000
    assert len(wallets) == 2
    assert wallets[0]["name"] == "TopTrader"
    assert wallets[0]["pnl"] == 50000
    assert wallets[1]["name"] == "SecondBest"
    # VolumeKing (from Q0) should NOT appear — Q1 was selected
    assert all(w["name"] != "VolumeKing" for w in wallets)


# ── 测试：巨鲸拆单检测 ────────────────────────────────────


def test_detect_whale_accumulation(tracker):
    """5 分钟内同一市场累计超过阈值应触发检测。"""
    now = int(time.time())
    fake_trades = [
        {
            "conditionId": "0xMarketA",
            "side": "BUY",
            "size": 1000,
            "price": 0.30,
            "timestamp": now - 60,
            "title": "Test Market A",
            "slug": "test-a",
            "eventSlug": "event-a",
        },
        {
            "conditionId": "0xMarketA",
            "side": "BUY",
            "size": 800,
            "price": 0.32,
            "timestamp": now - 120,
            "title": "Test Market A",
            "slug": "test-a",
            "eventSlug": "event-a",
        },
        {
            "conditionId": "0xMarketB",
            "side": "SELL",
            "size": 100,
            "price": 0.50,
            "timestamp": now - 30,
            "title": "Test Market B",
            "slug": "test-b",
            "eventSlug": "event-b",
        },
    ]

    with patch.object(tracker, "get_wallet_recent_trades", return_value=fake_trades):
        triggered = tracker.detect_whale_accumulation(
            "0xwallet1", window_minutes=5, threshold_usdc=500
        )

    # MarketA: 1000*0.30 + 800*0.32 = 300 + 256 = 556 > 500 → triggered
    # MarketB: 100*0.50 = 50 < 500 → not triggered
    assert len(triggered) == 1
    assert triggered[0]["conditionId"] == "0xMarketA"
    assert triggered[0]["total_usdc"] == pytest.approx(556, abs=1)
    assert triggered[0]["trade_count"] == 2


def test_detect_whale_no_trigger(tracker):
    """累计未达阈值时不应触发。"""
    now = int(time.time())
    fake_trades = [
        {
            "conditionId": "0xMarketC",
            "side": "BUY",
            "size": 50,
            "price": 0.50,
            "timestamp": now - 30,
            "title": "Small Trade",
            "slug": "small",
            "eventSlug": "event-c",
        },
    ]

    with patch.object(tracker, "get_wallet_recent_trades", return_value=fake_trades):
        triggered = tracker.detect_whale_accumulation(
            "0xwallet1", window_minutes=5, threshold_usdc=500
        )

    assert len(triggered) == 0


def test_detect_whale_expired_trades(tracker):
    """超出时间窗口的交易不应被聚合。"""
    now = int(time.time())
    fake_trades = [
        {
            "conditionId": "0xMarketD",
            "side": "BUY",
            "size": 2000,
            "price": 0.50,
            "timestamp": now - 600,
            "title": "Old Trade",
            "slug": "old",
            "eventSlug": "event-d",
        },
    ]

    with patch.object(tracker, "get_wallet_recent_trades", return_value=fake_trades):
        triggered = tracker.detect_whale_accumulation(
            "0xwallet1", window_minutes=5, threshold_usdc=500
        )

    # Trade is 10 minutes old, window is 5 minutes → should not trigger
    assert len(triggered) == 0


# ── 测试：信号格式化 ──────────────────────────────────────


def test_format_smart_context(tracker):
    """格式化输出应包含钱包名称、方向、持仓量。"""
    matches = [
        {"name": "ColdMath", "pnl": 19580, "amount": 500, "outcome_index": 0},
        {"name": "yourmymuse45", "pnl": 32499, "amount": 300, "outcome_index": 0},
    ]
    market = {"outcomes": '["Yes", "No"]'}

    ctx = tracker._format_smart_context(matches, market)

    assert "ColdMath" in ctx
    assert "yourmymuse45" in ctx
    assert "$19,580" in ctx
    assert "$32,499" in ctx
    assert "Yes" in ctx
    assert "共识: Yes" in ctx
    assert "2 个" in ctx


def test_format_smart_context_split_consensus(tracker):
    """钱包方向分歧时应显示'分歧'。"""
    matches = [
        {"name": "WalletA", "pnl": 10000, "amount": 500, "outcome_index": 0},
        {"name": "WalletB", "pnl": 15000, "amount": 400, "outcome_index": 1},
    ]
    market = {"outcomes": '["Yes", "No"]'}

    ctx = tracker._format_smart_context(matches, market)
    assert "分歧" in ctx


# ── 测试：缓存 ──────────────────────────────────────────


def test_cache_save_load(tracker, tmp_path):
    """缓存应正确保存和加载。"""
    cache_file = tmp_path / "test_cache.json"
    with (
        patch("src.utils.smart_money._CACHE_FILE", cache_file),
        patch("src.utils.smart_money._CACHE_DIR", tmp_path),
    ):
        tracker._cache["test_key"] = "test_value"
        tracker._save_cache()

        assert cache_file.exists()
        with open(cache_file) as f:
            loaded = json.load(f)
        assert loaded["test_key"] == "test_value"


# ── 测试：rate limiting ───────────────────────────────────


def test_rate_limit(tracker):
    """连续调用应有间隔。"""
    tracker._last_request_ts = time.time()
    start = time.time()
    tracker._rate_limit()
    elapsed = time.time() - start
    # Should have waited at least some time (not exact due to overhead)
    assert elapsed >= 0.3  # _RATE_LIMIT_SLEEP=0.5, but allow some tolerance


# ── 测试：聪明钱包方向校验 (get_smart_money_alignment) ────


def test_alignment_smart_wallets_aligned(tracker):
    """聪明钱包同方向 → aligned=True。"""
    holders_response = [
        {
            "token": "12345",
            "holders": [
                {
                    "proxyWallet": "0xaaa111aaa111aaa111aaa111aaa111aaa111aaa1",
                    "amount": 500,
                    "outcomeIndex": 0,
                },
                {
                    "proxyWallet": "0xbbb222bbb222bbb222bbb222bbb222bbb222bbb2",
                    "amount": 300,
                    "outcomeIndex": 0,
                },
            ],
        },
    ]
    market = {"conditionId": "0xtest_align"}

    with patch.object(tracker, "_fetch_holders", return_value=holders_response):
        result = tracker.get_smart_money_alignment(market, "YES")

    assert result["has_smart_positions"] is True
    assert result["aligned"] is True
    assert result["aligned_count"] == 2
    assert result["opposed_count"] == 0
    assert result["total_aligned_amount"] == 800.0


def test_alignment_smart_wallets_opposed(tracker):
    """聪明钱包反方向 → aligned=False。"""
    holders_response = [
        {
            "token": "12345",
            "holders": [
                {
                    "proxyWallet": "0xaaa111aaa111aaa111aaa111aaa111aaa111aaa1",
                    "amount": 500,
                    "outcomeIndex": 1,
                },
                {
                    "proxyWallet": "0xbbb222bbb222bbb222bbb222bbb222bbb222bbb2",
                    "amount": 300,
                    "outcomeIndex": 1,
                },
            ],
        },
    ]
    market = {"conditionId": "0xtest_opposed"}

    with patch.object(tracker, "_fetch_holders", return_value=holders_response):
        result = tracker.get_smart_money_alignment(market, "YES")

    assert result["has_smart_positions"] is True
    assert result["aligned"] is False
    assert result["aligned_count"] == 0
    assert result["opposed_count"] == 2


def test_alignment_no_smart_wallets(tracker):
    """聪明钱包不在此市场 → has_smart_positions=False。"""
    holders_response = [
        {
            "token": "12345",
            "holders": [
                {"proxyWallet": "0xunknown1", "amount": 500, "outcomeIndex": 0},
            ],
        },
    ]
    market = {"conditionId": "0xtest_none"}

    with patch.object(tracker, "_fetch_holders", return_value=holders_response):
        result = tracker.get_smart_money_alignment(market, "YES")

    assert result["has_smart_positions"] is False
    assert result["aligned"] is False


def test_alignment_disabled(tracker):
    """smart_money.enabled=False → 默认空结果。"""
    with patch(
        "src.utils.smart_money.get",
        side_effect=lambda k, d=None: False if k == "smart_money.enabled" else d,
    ):
        result = tracker.get_smart_money_alignment({"conditionId": "0x1"}, "YES")

    assert result["has_smart_positions"] is False
    assert result["aligned"] is False


# ── 测试：市场全体持仓者分析 (get_market_holder_analysis) ──


def test_holder_analysis_yes_consensus(tracker):
    """YES 持仓显著大于 NO → consensus='YES'。"""
    holders_response = [
        {
            "token": "yes_token",
            "holders": [
                {"proxyWallet": "0x1", "amount": 1000, "outcomeIndex": 0},
                {"proxyWallet": "0x2", "amount": 800, "outcomeIndex": 0},
                {"proxyWallet": "0x3", "amount": 500, "outcomeIndex": 0},
            ],
        },
        {
            "token": "no_token",
            "holders": [
                {"proxyWallet": "0x4", "amount": 200, "outcomeIndex": 1},
            ],
        },
    ]

    with patch.object(tracker, "_fetch_holders", return_value=holders_response):
        result = tracker.get_market_holder_analysis("0xcondition1")

    assert result["total_holders"] == 4
    assert result["yes_holders"] == 3
    assert result["no_holders"] == 1
    assert result["yes_total_amount"] == 2300.0
    assert result["no_total_amount"] == 200.0
    assert result["consensus"] == "YES"


def test_holder_analysis_neutral_consensus(tracker):
    """YES / NO 接近 → consensus='neutral'。"""
    holders_response = [
        {
            "token": "mixed",
            "holders": [
                {"proxyWallet": "0x1", "amount": 500, "outcomeIndex": 0},
                {"proxyWallet": "0x2", "amount": 450, "outcomeIndex": 1},
            ],
        },
    ]

    with patch.object(tracker, "_fetch_holders", return_value=holders_response):
        result = tracker.get_market_holder_analysis("0xcondition2")

    assert result["consensus"] == "neutral"


def test_holder_analysis_no_holders(tracker):
    """/holders 返回空 → 默认空结果。"""
    with patch.object(tracker, "_fetch_holders", return_value=[]):
        result = tracker.get_market_holder_analysis("0xcondition_empty")

    assert result["total_holders"] == 0
    assert result["consensus"] == "neutral"


def test_holder_analysis_top5_concentration(tracker):
    """Top5集中度计算。"""
    holders = []
    for i in range(10):
        holders.append(
            {
                "proxyWallet": f"0x{i:04x}",
                "amount": 1000 - i * 100,
                "outcomeIndex": 0,
            }
        )
    holders_response = [{"token": "tok", "holders": holders}]

    with patch.object(tracker, "_fetch_holders", return_value=holders_response):
        result = tracker.get_market_holder_analysis("0xcondition_conc")

    # top5: 1000+900+800+700+600=4000, total: 5500
    assert result["top5_concentration_pct"] == pytest.approx(4000 / 5500, abs=0.01)
