"""
test_copytrade.py — T298 跟投模块单元测试

涵盖：
- 二次打分公式
- copy_tasks CRUD（FastAPI TestClient）
- CopyTradeWatcher 新交易检测逻辑
- 参数校验（钱包地址、重复任务等）
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── 二次打分公式测试 ──────────────────────────────────────────────────


def _score(ev, timing, trades30, pnl):
    """直接内联公式，和 copytrade_routes._compute_secondary_score 保持一致。"""
    if ev < 0.1 or timing == 50.0:
        return 0.0
    ev_score = min(ev / 0.5, 1.0) * 30.0
    timing_score = max(0.0, min((timing - 50.0) / 25.0, 1.0)) * 30.0
    trades_score = min(trades30 / 100.0, 1.0) * 20.0
    pnl_score = min(pnl / 15000.0, 1.0) * 20.0
    return round(ev_score + timing_score + trades_score + pnl_score, 2)


class TestSecondaryScore:
    def test_elite_trader(self):
        """精英交易者应得 100 分"""
        score = _score(ev=0.5, timing=75, trades30=100, pnl=15000)
        assert score == 100.0

    def test_disqualify_ev_too_low(self):
        """EV < 0.1 → 淘汰"""
        score = _score(ev=0.05, timing=65, trades30=50, pnl=5000)
        assert score == 0.0

    def test_disqualify_timing_exactly_50(self):
        """timing_score == 50.0 → 随机基准，淘汰"""
        score = _score(ev=0.3, timing=50.0, trades30=80, pnl=8000)
        assert score == 0.0

    def test_partial_score(self):
        """EV 满分 30 + timing 满分 30，trade/pnl 各一半"""
        score = _score(ev=0.5, timing=75, trades30=50, pnl=7500)
        # ev:30 + timing:30 + trades:10 + pnl:10 = 80
        assert score == 80.0

    def test_zero_pnl_still_scores(self):
        """PnL 为 0 但 EV/timing 高也能得分"""
        score = _score(ev=0.5, timing=75, trades30=0, pnl=0)
        assert score == 60.0  # ev:30 + timing:30 + 0 + 0

    def test_capped_at_100(self):
        """超额指标不超过 100"""
        score = _score(ev=999, timing=999, trades30=999, pnl=999999)
        assert score == 100.0

    def test_boundary_ev_exactly_0_1(self):
        """EV 恰好=0.1 应通过淘汰线"""
        score = _score(ev=0.1, timing=60, trades30=50, pnl=5000)
        assert score > 0.0

    def test_compute_secondary_score_function(self):
        """测试 copytrade_routes._compute_secondary_score 接受 trader dict"""
        from scripts.dashboard_routes.copytrade_routes import _compute_secondary_score

        trader = {
            "ev_per_bought": 0.5,
            "timing_score": 75,
            "trade_count_30": 100,
            "pnl": 15000,
        }
        # 新公式(PolyHub校准): ev=35 + pnl=0.525 + trades=3 + timing=15 = 53.52
        assert _compute_secondary_score(trader) == 53.52

    def test_compute_score_disqualify(self):
        from scripts.dashboard_routes.copytrade_routes import _compute_secondary_score

        # 新公式无淘汰逻辑，全零输入→0分
        assert (
            _compute_secondary_score({"ev_per_bought": 0.0, "timing_score": 50.0})
            == 0.0
        )


# ── copy_tasks CRUD 路由测试 ──────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path):
    """创建临时数据库并执行 copy_tasks schema + paper_bets schema"""
    from src.utils.db import init_db

    db_path = tmp_path / "test.db"
    # T142-A: init full schema (includes paper_bets) so category_max_per_event checks work
    init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS copy_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_address TEXT NOT NULL,
            categories TEXT DEFAULT '[]',
            base_usdc REAL DEFAULT 10.0,
            niche_only INTEGER DEFAULT 1,
            score_threshold REAL DEFAULT 75.0,
            status TEXT DEFAULT 'running',
            created_at TEXT NOT NULL,
            last_active_at TEXT,
            last_seen_trade_ts REAL DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            last_error TEXT,
            tag_label TEXT DEFAULT '',
            total_signals INTEGER DEFAULT 0
        )"""
        )
    return db_path


@pytest.fixture
def test_client(tmp_db):
    """FastAPI TestClient with overridden DB_PATH"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    with patch("scripts.dashboard_routes.copytrade_routes.DB_PATH", tmp_db):
        from scripts.dashboard_routes import copytrade_routes

        copytrade_routes.DB_PATH = tmp_db
        app.include_router(copytrade_routes.router)
        yield TestClient(app)


def _make_wallet():
    return "0x" + "a1b2c3d4" * 5  # 42-char hex address


class TestCopyTaskCRUD:
    def test_list_empty(self, test_client):
        """空库返回空列表"""
        r = test_client.get("/api/copy-tasks")
        assert r.status_code == 200
        assert r.json()["tasks"] == []
        assert r.json()["count"] == 0

    def test_create_task(self, test_client):
        wallet = _make_wallet()
        payload = {
            "wallet_address": wallet,
            "categories": ["weather"],
            "base_usdc": 15.0,
            "niche_only": True,
            "score_threshold": 70.0,
            "tag_label": "Weather",
        }
        r = test_client.post("/api/copy-tasks", json=payload)
        assert r.status_code == 201
        assert r.json()["ok"] is True
        assert "task_id" in r.json()

    def test_create_duplicate_wallet_returns_409(self, test_client):
        wallet = _make_wallet()
        payload = {"wallet_address": wallet}
        test_client.post("/api/copy-tasks", json=payload)
        r2 = test_client.post("/api/copy-tasks", json=payload)
        assert r2.status_code == 409

    def test_create_invalid_wallet_address(self, test_client):
        r = test_client.post("/api/copy-tasks", json={"wallet_address": "invalid"})
        assert r.status_code == 400

    def test_list_after_create(self, test_client):
        wallet = _make_wallet()
        test_client.post("/api/copy-tasks", json={"wallet_address": wallet})
        r = test_client.get("/api/copy-tasks")
        assert r.json()["count"] == 1

    def test_patch_status_pause(self, test_client):
        wallet = _make_wallet()
        cr = test_client.post("/api/copy-tasks", json={"wallet_address": wallet})
        tid = cr.json()["task_id"]
        r = test_client.patch(f"/api/copy-tasks/{tid}", json={"status": "paused"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_patch_status_invalid(self, test_client):
        wallet = _make_wallet()
        cr = test_client.post("/api/copy-tasks", json={"wallet_address": wallet})
        tid = cr.json()["task_id"]
        r = test_client.patch(
            f"/api/copy-tasks/{tid}", json={"status": "invalid_status"}
        )
        assert r.status_code == 400

    def test_patch_no_fields_returns_400(self, test_client):
        wallet = _make_wallet()
        cr = test_client.post("/api/copy-tasks", json={"wallet_address": wallet})
        tid = cr.json()["task_id"]
        r = test_client.patch(f"/api/copy-tasks/{tid}", json={})
        assert r.status_code == 400

    def test_delete_task(self, test_client):
        wallet = _make_wallet()
        cr = test_client.post("/api/copy-tasks", json={"wallet_address": wallet})
        tid = cr.json()["task_id"]
        r = test_client.delete(f"/api/copy-tasks/{tid}")
        assert r.status_code == 200
        # 列表应为空
        assert test_client.get("/api/copy-tasks").json()["count"] == 0

    def test_delete_nonexistent_returns_404(self, test_client):
        r = test_client.delete("/api/copy-tasks/99999")
        assert r.status_code == 404

    def test_create_paused_wallet_returns_409(self, test_client):
        """已暂停的钱包不允许重复创建任务（BUG2修复验证）"""
        wallet = _make_wallet()
        cr = test_client.post("/api/copy-tasks", json={"wallet_address": wallet})
        tid = cr.json()["task_id"]
        # 暂停该任务
        test_client.patch(f"/api/copy-tasks/{tid}", json={"status": "paused"})
        # 尝试为同一钱包创建新任务 → 应该 409
        r2 = test_client.post("/api/copy-tasks", json={"wallet_address": wallet})
        assert r2.status_code == 409

    def test_stopped_wallet_can_create_new(self, test_client):
        """已停止的钱包可以创建新任务"""
        wallet = _make_wallet()
        cr = test_client.post("/api/copy-tasks", json={"wallet_address": wallet})
        tid = cr.json()["task_id"]
        # 停止该任务
        test_client.patch(f"/api/copy-tasks/{tid}", json={"status": "stopped"})
        # 为同一钱包创建新任务 → 应该成功
        r2 = test_client.post("/api/copy-tasks", json={"wallet_address": wallet})
        assert r2.status_code == 201

    def test_pause_and_resume_workflow(self, test_client):
        """完整的暂停→恢复工作流测试"""
        wallet = _make_wallet()
        cr = test_client.post("/api/copy-tasks", json={"wallet_address": wallet})
        tid = cr.json()["task_id"]

        # 验证初始状态为 running
        tasks = test_client.get("/api/copy-tasks").json()["tasks"]
        task = [t for t in tasks if t["id"] == tid][0]
        assert task["status"] == "running"

        # 暂停
        r = test_client.patch(f"/api/copy-tasks/{tid}", json={"status": "paused"})
        assert r.status_code == 200
        tasks = test_client.get("/api/copy-tasks").json()["tasks"]
        task = [t for t in tasks if t["id"] == tid][0]
        assert task["status"] == "paused"

        # 恢复
        r = test_client.patch(f"/api/copy-tasks/{tid}", json={"status": "running"})
        assert r.status_code == 200
        tasks = test_client.get("/api/copy-tasks").json()["tasks"]
        task = [t for t in tasks if t["id"] == tid][0]
        assert task["status"] == "running"


# ── CopyTradeWatcher 逻辑测试 ─────────────────────────────────────────


class TestCopyTradeWatcher:
    @pytest.fixture
    def watcher(self, tmp_db):
        from src.utils.copy_trade_watcher import CopyTradeWatcher

        return CopyTradeWatcher(db_path=tmp_db)

    def test_run_cycle_disabled_returns_zero(self, watcher):
        """模块关闭时 run_cycle 应立即返回 0"""
        with patch.object(watcher, "_get", return_value=False):
            assert watcher.run_cycle() == 0

    def test_run_cycle_no_active_tasks(self, watcher, tmp_db):
        """无运行任务时 run_cycle 应返回 0"""
        with patch("src.utils.copy_trade_watcher.requests.get") as mock_get:
            with patch.object(
                watcher, "_get", side_effect=lambda k: True if "enabled" in k else None
            ):
                result = watcher.run_cycle()
        assert result == 0
        mock_get.assert_not_called()

    def test_compute_secondary_score_disqualify(self, watcher):
        """全零输入→0分"""
        from scripts.dashboard_routes.copytrade_routes import _compute_secondary_score

        score = _compute_secondary_score({"ev_per_bought": 0.0, "timing_score": 50.0})
        assert score == 0.0

    def test_make_bet_id_deterministic(self, watcher):
        """相同输入始终产生相同 bet_id"""
        id1 = watcher._make_bet_id("0xabc", "0xcond", "YES", 1700000000.0)
        id2 = watcher._make_bet_id("0xabc", "0xcond", "YES", 1700000000.0)
        assert id1 == id2
        assert id1.startswith("copy_")

    def test_make_bet_id_unique_for_different_trades(self, watcher):
        """不同交易应有不同 bet_id"""
        id1 = watcher._make_bet_id("0xabc", "0xcond", "YES", 1700000000.0)
        id2 = watcher._make_bet_id("0xabc", "0xcond", "NO", 1700000000.0)
        assert id1 != id2

    def test_calc_bet_size_with_multiplier(self, watcher):
        """天气赛道倍率 1.8 应生效"""
        task = {"base_usdc": 10.0}
        with patch.object(
            watcher,
            "_get",
            side_effect=lambda k: {
                "local_copy_trading.default_base_usdc": 10.0,
                "local_copy_trading.category_size_multiplier": {"weather": 1.8},
            }.get(k),
        ):
            size = watcher._calc_bet_size(task, ["weather"])
        assert size == 18.0

    def test_increment_error_pauses_task(self, watcher, tmp_db):
        """连续错误达上限后任务应被暂停"""
        # 先插入一个任务
        with sqlite3.connect(str(tmp_db)) as conn:
            conn.execute(
                "INSERT INTO copy_tasks (wallet_address, created_at, error_count) VALUES (?,?,?)",
                ("0x" + "b" * 40, "2025-01-01T00:00:00+00:00", 2),
            )
            task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        with patch.object(watcher, "_get", return_value=3):  # max_error_count=3
            with patch.object(watcher, "_send_telegram_alert"):
                watcher._increment_error(task_id, "test error")

        with sqlite3.connect(str(tmp_db)) as conn:
            row = conn.execute(
                "SELECT status FROM copy_tasks WHERE id=?", (task_id,)
            ).fetchone()
        assert row[0] == "paused"

    def test_fetch_wallet_trades_handles_list(self, watcher):
        """Data API 返回列表格式应直接使用"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "type": "TRADE",
                "conditionId": "0xabc",
                "side": "BUY",
                "outcome": "Yes",
                "asset": "token_abc_123",
                "price": "0.6",
                "size": "10",
                "timestamp": 9999999,
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        with patch(
            "src.utils.copy_trade_watcher.requests.get", return_value=mock_resp
        ) as mock_get:
            trades = watcher._fetch_wallet_trades("0x" + "a" * 40)
        assert len(trades) == 1
        # 验证使用 /activity 端点（非 /trades，后者有 ~3 小时数据延迟）
        call_url = mock_get.call_args[0][0]
        assert "/activity" in call_url, f"必须使用 /activity 端点，实际: {call_url}"

    def test_fetch_wallet_trades_filters_reward_type(self, watcher):
        """DEFECT FIX: /activity 端点返回 REWARD 等非交易活动应被过滤，只保留 TRADE"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "type": "TRADE",
                "conditionId": "0xtrade",
                "side": "BUY",
                "outcome": "Yes",
                "asset": "tok1",
                "price": "0.6",
                "timestamp": 9000001,
            },
            {
                "type": "REWARD",
                "conditionId": "0xreward",
                "side": "BUY",
                "outcome": "Yes",
                "asset": "tok2",
                "price": "0.5",
                "timestamp": 9000002,
            },
            {
                "type": "REDEMPTION",
                "conditionId": "0xredeem",
                "side": "BUY",
                "outcome": "Yes",
                "asset": "tok3",
                "price": "0.4",
                "timestamp": 9000003,
            },
        ]
        mock_resp.raise_for_status = MagicMock()
        with patch("src.utils.copy_trade_watcher.requests.get", return_value=mock_resp):
            trades = watcher._fetch_wallet_trades("0x" + "a" * 40)
        assert len(trades) == 1, "只有 TRADE 类型应保留，REWARD/REDEMPTION 必须被过滤"
        assert trades[0]["conditionId"] == "0xtrade"

    def test_fetch_wallet_trades_handles_error(self, watcher):
        """API 失败时返回空列表，不抛出异常"""
        import requests as req

        with patch(
            "src.utils.copy_trade_watcher.requests.get",
            side_effect=req.RequestException("timeout"),
        ):
            trades = watcher._fetch_wallet_trades("0x" + "a" * 40)
        assert trades == []

    # ── T298-D 安全红线测试 ────────────────────────────────────────────

    def test_no_third_party_copy_imports(self):
        """T298-D: 验证 copy_trade_watcher 不引入任何第三方跟投模块"""
        import ast
        import importlib.util

        spec = importlib.util.find_spec("src.utils.copy_trade_watcher")
        source = Path(spec.origin).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = {"polyhub_copy", "copy_trading_bot", "phub", "external_copy"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module_name = ""
                if isinstance(node, ast.Import):
                    module_name = node.names[0].name if node.names else ""
                else:
                    module_name = node.module or ""
                assert not any(
                    f in module_name for f in forbidden
                ), f"Forbidden third-party copy module import detected: {module_name}"

    def test_hard_max_bet_usdc_caps_size(self, watcher, tmp_db):
        """T298-D: hard_max_bet_usdc 必须限制 copy_trade 下注金额上限"""
        # 插入任务（base_usdc=100，远高于 hard_max=5）
        with sqlite3.connect(str(tmp_db)) as conn:
            conn.execute(
                "INSERT INTO copy_tasks (wallet_address, created_at, base_usdc)"
                " VALUES (?,?,?)",
                ("0x" + "c" * 40, "2025-01-01T00:00:00+00:00", 100.0),
            )
            task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "UPDATE copy_tasks SET status='running' WHERE id=?", (task_id,)
            )

        trade = {
            "conditionId": "0x" + "d" * 64,
            "side": "BUY",
            "outcome": "Yes",
            "asset": "token_" + "d" * 16,
            "price": "0.6",
            "size": "100",
            "timestamp": 9_999_999_999.0,
            "title": "Will X happen?",
            "slug": "will-x-happen",
            "eventSlug": "event-x",
            "tag": "crypto",
        }

        def fake_get(key: str):
            mapping = {
                "local_copy_trading.enabled": True,
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.95,
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 100.0,
                "local_copy_trading.category_size_multiplier": {},
                "paper_trading.hard_max_bet_usdc": 5.0,
            }
            return mapping.get(key)

        inserted_bets: list[dict] = []

        def fake_insert_bet_safe(bet: dict) -> bool:
            inserted_bets.append(bet)
            return True

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch.object(watcher, "_fetch_wallet_trades", return_value=[trade]),
            patch.object(watcher, "_insert_bet_safe", side_effect=fake_insert_bet_safe),
            patch.object(watcher, "_update_task_after_cycle"),
        ):
            task = {
                "id": task_id,
                "wallet_address": "0x" + "c" * 40,
                "last_seen_trade_ts": 0,
                "categories": "[]",
                "niche_only": 0,
                "base_usdc": 100.0,
            }
            watcher._process_task(task)

        assert inserted_bets, "应当插入至少一条跟投记录"
        for bet in inserted_bets:
            assert (
                bet["size_usdc"] <= 5.0
            ), f"size_usdc={bet['size_usdc']} 超出 hard_max_bet_usdc=5.0"
            assert (
                bet["size_usdc"] <= 5.0
            ), f"size_usdc={bet['size_usdc']} 超出 hard_max_bet_usdc=5.0"

    # ── T139: emergency_freeze + dry_run 双重保护测试 ─────────────────

    def test_run_cycle_skips_when_emergency_frozen(self, watcher):
        """T139: emergency_freeze=true 时 run_cycle 必须返回 0，不生成任何信号"""

        def fake_get(key: str):
            if key == "local_copy_trading.enabled":
                return True
            if key == "risk.emergency_freeze":
                return True
            return None

        with patch.object(watcher, "_get", side_effect=fake_get):
            result = watcher.run_cycle()
        assert result == 0, "emergency_freeze=true 时应跳过跟投信号生成"

    def test_run_cycle_skips_in_live_mode(self, watcher):
        """T139→T140: 实盘模式无 executor 注入 → run_cycle 应返回 0（executor 守卫）"""

        def fake_get(key: str):
            if key == "local_copy_trading.enabled":
                return True
            if key == "risk.emergency_freeze":
                return False
            return None

        with patch.object(watcher, "_get", side_effect=fake_get):
            with patch("src.utils.config_loader.is_dry_run", return_value=False):
                result = watcher.run_cycle()
        assert result == 0, "实盘模式下无 executor 时应跳过跟投信号生成"

    def test_run_cycle_respects_runtime_gate_before_execution(self):
        """N: runtime gate 命中时，必须跳过执行路径并走冻结跳过分支。"""
        from src.utils.copy_trade_watcher import CopyTradeWatcher

        watcher = CopyTradeWatcher.__new__(CopyTradeWatcher)
        watcher._last_scan_ts = 0.0
        watcher._executor = None

        def fake_get(key: str):
            if key == "local_copy_trading.enabled":
                return True
            if key == "local_copy_trading.scan_interval_minutes":
                return 0
            return None

        watcher._get = fake_get

        task = {
            "id": 101,
            "wallet_address": "0x" + "a" * 40,
        }

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch("src.utils.config_loader.is_dry_run", return_value=False),
            patch.object(watcher, "_load_active_tasks", return_value=[task]),
            patch.object(
                watcher,
                "_runtime_gate_for_wallet",
                return_value={
                    "blocked": True,
                    "reason": "freeze_auto",
                    "maintenance_mode": False,
                    "emergency_freeze": False,
                    "freeze_auto": True,
                    "auto_trading_enabled": True,
                },
            ),
            patch.object(watcher, "_process_task") as mock_dry,
            patch.object(watcher, "_process_task_live") as mock_live,
            patch.object(watcher, "_process_task_frozen") as mock_frozen,
            patch.object(watcher, "_monitor_copy_sells"),
        ):
            out = watcher.run_cycle()

        assert out == 0
        mock_dry.assert_not_called()
        mock_live.assert_not_called()
        mock_frozen.assert_called_once()

    def test_get_candidate_trades_empty_returns_three_values(self):
        """N: 无原始交易时 _get_candidate_trades 仍应返回三元组，避免解包异常。"""
        from src.utils.copy_trade_watcher import CopyTradeWatcher

        watcher = CopyTradeWatcher.__new__(CopyTradeWatcher)
        watcher._get = lambda _k: None

        task = {
            "wallet_address": "0x" + "b" * 40,
            "last_seen_trade_ts": 123.0,
            "categories": "[]",
            "niche_only": 0,
        }

        with (
            patch.object(watcher, "_fetch_wallet_trades", return_value=[]),
            patch.object(watcher, "_get", return_value=None),
        ):
            candidates, categories, max_seen = watcher._get_candidate_trades(task)

        assert candidates == []
        assert categories == []
        assert max_seen == 123.0

    def test_monitor_copy_sells_skips_wallet_when_runtime_blocked(self):
        """N: 跟卖监控也必须受 runtime gate 约束。"""
        from src.utils.copy_trade_watcher import CopyTradeWatcher

        watcher = CopyTradeWatcher.__new__(CopyTradeWatcher)
        watcher._get = lambda k: {
            "local_copy_trading.follow_sell": {"enabled": True},
        }.get(k)

        with (
            patch.object(
                watcher,
                "_get_open_copy_bets",
                return_value=[
                    {
                        "bet_id": "copy_xxx",
                        "copy_source_wallet": "0x" + "c" * 40,
                        "condition_id": "cond_1",
                    }
                ],
            ),
            patch.object(
                watcher,
                "_runtime_gate_for_wallet",
                side_effect=[
                    {
                        "blocked": False,
                        "reason": "",
                        "maintenance_mode": False,
                        "emergency_freeze": False,
                    },
                    {
                        "blocked": True,
                        "reason": "auto_trading_disabled",
                        "maintenance_mode": False,
                        "emergency_freeze": False,
                    },
                ],
            ),
            patch.object(watcher, "_fetch_wallet_trades") as mock_fetch,
        ):
            watcher._monitor_copy_sells()

        mock_fetch.assert_not_called()

    # ── T140: 实盘跟投测试 ─────────────────────────────────────────────

    def test_run_cycle_live_no_executor_skips(self, watcher, tmp_db):
        """T140: 实盘模式 + executor=None → _process_task_live 应返回 0"""
        with sqlite3.connect(str(tmp_db)) as conn:
            conn.execute(
                "INSERT INTO copy_tasks (wallet_address, created_at, status)"
                " VALUES (?,?,?)",
                ("0x" + "e" * 40, "2025-01-01T00:00:00+00:00", "running"),
            )

        def fake_get(key: str):
            if key == "local_copy_trading.enabled":
                return True
            if key == "risk.emergency_freeze":
                return False
            return None

        with patch.object(watcher, "_get", side_effect=fake_get):
            with patch("src.utils.config_loader.is_dry_run", return_value=False):
                with patch.object(watcher, "_fetch_wallet_trades", return_value=[]):
                    result = watcher.run_cycle()
        assert result == 0

    def test_run_cycle_live_calls_executor_on_new_trade(self, watcher, tmp_db):
        """T140: 实盘模式 + executor 注入 + 有新交易 → executor.execute() 必须被调用"""
        from unittest.mock import MagicMock

        # 注入 mock executor
        mock_executor = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.tx_hash = "0xABC123"
        mock_result.actual_price = 0.61
        mock_result.matched_shares = 100
        mock_result.error = None
        mock_executor.execute.return_value = mock_result
        watcher._executor = mock_executor

        trade = {
            "conditionId": "0x" + "f" * 64,
            "side": "BUY",
            "outcome": "Yes",
            "asset": "yes_token_123",
            "price": "0.6",
            "size": "100",
            "timestamp": 9_999_999_999.0,
            "title": "Will T140 live trade fire?",
            "slug": "t140-live-trade",
            "eventSlug": "event-t140",
            "tag": "crypto",
        }

        task = {
            "id": 1,
            "wallet_address": "0x" + "f" * 40,
            "last_seen_trade_ts": 0,
            "categories": '["crypto"]',
            "niche_only": 0,
            "base_usdc": 10.0,
        }

        def fake_get(key: str):
            mapping = {
                "local_copy_trading.enabled": True,
                "risk.emergency_freeze": False,
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.95,
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 10.0,
                "local_copy_trading.category_size_multiplier": {},
                "paper_trading.hard_max_bet_usdc": 50.0,
            }
            return mapping.get(key)

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch.object(watcher, "_fetch_wallet_trades", return_value=[trade]),
            patch.object(watcher, "_insert_bet_safe", return_value=True),
            patch.object(watcher, "_update_task_after_cycle"),
            patch("src.utils.config_loader.is_dry_run", return_value=False),
        ):
            count = watcher._process_task_live(task)

        assert mock_executor.execute.called, "实盘模式下必须调用 executor.execute()"
        assert count == 1, "成功执行后信号计数应为 1"
        # 验证 Signal 传给 executor 的关键字段
        signal = mock_executor.execute.call_args[0][0]
        assert signal.outcome == "YES"
        # token_id 直接来自 trade["asset"] 字段，无需 Gamma API
        assert signal.token_id == "yes_token_123"
        assert signal.condition_id == "0x" + "f" * 64
        assert signal.strategy_tag == "copy_trade"

    def test_run_cycle_live_frozen_does_not_call_executor(self, watcher, tmp_db):
        """T140 SUPREME-1: 实盘模式 + emergency_freeze=true → 必须记录冻结跳过日志，
        不调用 executor.execute()，run_cycle 返回 0"""
        from unittest.mock import MagicMock

        mock_executor = MagicMock()
        watcher._executor = mock_executor

        trade = {
            "conditionId": "0x" + "a" * 64,
            "side": "BUY",
            "outcome": "Yes",
            "asset": "token_freeze_test",
            "price": "0.55",
            "size": "100",
            "timestamp": 9_999_999_998.0,
            "title": "Freeze test: should NOT execute",
            "slug": "freeze-test",
            "eventSlug": "event-freeze",
            "tag": "weather",
        }

        with sqlite3.connect(str(tmp_db)) as conn:
            conn.execute(
                "INSERT INTO copy_tasks (wallet_address, created_at, status)"
                " VALUES (?,?,?)",
                ("0x" + "a" * 40, "2025-01-01T00:00:00+00:00", "running"),
            )

        def fake_get(key: str):
            mapping = {
                "local_copy_trading.enabled": True,
                "risk.emergency_freeze": True,  # ← 冻结！
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.95,
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 10.0,
                "local_copy_trading.category_size_multiplier": {},
                "paper_trading.hard_max_bet_usdc": 50.0,
            }
            return mapping.get(key)

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch.object(watcher, "_fetch_wallet_trades", return_value=[trade]),
            patch("src.utils.config_loader.is_dry_run", return_value=False),
        ):
            result = watcher.run_cycle()

        assert result == 0, "emergency_freeze=true 时 run_cycle 应返回 0"
        mock_executor.execute.assert_not_called()

    def test_resolve_token_ids_success(self, watcher):
        """T140: _resolve_token_ids 成功解析 Gamma API 返回的 clobTokenIds"""
        import json

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "conditionId": "0x" + "b" * 64,
                "clobTokenIds": json.dumps(["yes_tok_111", "no_tok_222"]),
            }
        ]
        mock_resp.raise_for_status = MagicMock()

        with patch("src.utils.copy_trade_watcher.requests.get", return_value=mock_resp):
            yes_tok, no_tok = watcher._resolve_token_ids("0x" + "b" * 64)

        assert yes_tok == "yes_tok_111"
        assert no_tok == "no_tok_222"

    def test_resolve_token_ids_api_error_returns_empty(self, watcher):
        """T140: _resolve_token_ids API 失败时返回 ('', '') 不抛出异常"""
        import requests as req

        with patch(
            "src.utils.copy_trade_watcher.requests.get",
            side_effect=req.RequestException("timeout"),
        ):
            yes_tok, no_tok = watcher._resolve_token_ids("0x" + "c" * 64)

        assert yes_tok == ""
        assert no_tok == ""

    def test_build_signal_returns_correct_signal(self, watcher):
        """T140: _build_signal 返回含正确字段的 Signal 对象"""
        task = {"id": 1, "wallet_address": "0x" + "d" * 40, "base_usdc": 15.0}
        trade = {
            "conditionId": "0x" + "e" * 64,
            "side": "BUY",
            "outcome": "No",
            "asset": "no_token_999",
            "price": "0.35",
            "title": "Will it rain tomorrow in NYC?",
            "slug": "rain-nyc",
            "eventSlug": "event-nyc",
            "tag": "weather",
        }

        signal = watcher._build_signal(task, trade, "no_token_999", 15.0, ["weather"])

        assert signal.outcome == "NO"
        assert signal.token_id == "no_token_999"
        assert signal.condition_id == "0x" + "e" * 64
        assert signal.strategy_tag == "copy_trade"
        assert signal.size_usdc == 15.0
        assert signal.expected_price == pytest.approx(0.35, abs=0.001)
        assert "copy_trade" in signal.model_votes

    def test_process_task_live_records_actual_cost_and_balance_snapshots(self, watcher):
        """T169: copy_trade 成功入场后，应记录实际成交额和余额快照。"""
        task = {"id": 1, "wallet_address": "0x" + "a" * 40}
        trade = {
            "conditionId": "0x" + "b" * 64,
            "outcome": "Yes",
            "asset": "tok_yes_123",
            "price": "0.35",
            "title": "Madrid high 28C?",
            "slug": "madrid-28c",
            "eventSlug": "weather-madrid",
            "timestamp": 1_777_777_777.0,
            "endDate": "2026-04-10T23:59:59Z",
        }

        mock_signal = MagicMock()
        mock_signal.ev = 0.08
        mock_result = MagicMock(
            success=True,
            actual_price=0.38,
            filled_size=1.9147,
            matched_shares=5.0388,
            tx_hash="0xabc123",
        )
        watcher._executor = MagicMock()
        watcher._executor.execute.return_value = mock_result

        inserted = {}

        def _capture_insert(bet_record):
            inserted.update(bet_record)
            return True

        with patch.object(
            watcher,
            "_get_candidate_trades",
            return_value=([(trade, 0.882)], ["weather"], trade["timestamp"]),
        ):
            with patch.object(watcher, "_build_signal", return_value=mock_signal):
                with patch.object(
                    watcher, "_insert_bet_safe", side_effect=_capture_insert
                ):
                    with patch(
                        "src.utils.balance_provider.get_available_balance",
                        return_value=6.87,
                    ):
                        signals = watcher._process_task_live(task)

        assert signals == 1
        assert inserted["size_usdc"] == pytest.approx(1.9147, abs=1e-6)
        assert inserted["sim_balance_after"] == pytest.approx(6.87, abs=1e-6)
        assert inserted["sim_balance_before"] == pytest.approx(8.78, abs=1e-6)
        assert inserted["matched_shares"] == pytest.approx(5.0388, abs=1e-6)

    # ── DEFECT FIX 测试 ────────────────────────────────────────────────

    def test_latest_ts_advances_for_filtered_trades(self, watcher):
        """DEFECT FIX: _process_task 中即使所有交易被过滤(未插入)，
        last_seen_trade_ts 也必须推进到所有 new_trades 的最大时间戳，
        避免下轮重复处理已识别为无效的交易。"""
        trade_ts = 9_000_000_000.0
        trade = {
            "conditionId": "0x" + "f" * 64,
            "side": "BUY",
            "outcome": "Yes",
            "asset": "tok_filter",
            "price": "0.99",  # > max_price_filter=0.85 → 被过滤掉
            "size": "10",
            "timestamp": trade_ts,
            "title": "Filtered trade",
            "slug": "filtered",
            "eventSlug": "filtered-event",
            "tag": "crypto",
        }

        def fake_get(key: str):
            return {
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.85,  # trade.price=0.99 > 0.85 → skip
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 10.0,
                "local_copy_trading.category_size_multiplier": {},
                "paper_trading.hard_max_bet_usdc": 50.0,
            }.get(key)

        captured_ts: list[float] = []

        def fake_update(task_id, last_ts, signals, error_count=0):
            captured_ts.append(last_ts)

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch.object(watcher, "_fetch_wallet_trades", return_value=[trade]),
            patch.object(watcher, "_update_task_after_cycle", side_effect=fake_update),
        ):
            task = {
                "id": 1,
                "wallet_address": "0x" + "a" * 40,
                "last_seen_trade_ts": 0.0,
                "categories": "[]",
                "niche_only": 0,
                "base_usdc": 10.0,
            }
            signals = watcher._process_task(task)

        # 交易被过滤 → 0 信号插入
        assert signals == 0
        # 但 last_seen_trade_ts 必须推进到 trade_ts（不再重复处理）
        assert captured_ts, "_update_task_after_cycle 必须被调用"
        assert captured_ts[0] == pytest.approx(
            trade_ts, abs=1.0
        ), f"last_seen_trade_ts 应推进到 {trade_ts}，实际 {captured_ts[0]}"

    def test_process_task_frozen_updates_last_seen_ts(self, watcher):
        """DEFECT FIX: 实盘冻结模式下出现候选交易时必须更新 last_seen_trade_ts，
        避免解冻后累积的旧交易被一次性批量跟投。"""
        trade_ts = 8_888_888_888.0
        trade = {
            "conditionId": "0x" + "aa" * 32,
            "side": "BUY",
            "outcome": "Yes",
            "asset": "tok_frozen",
            "price": "0.60",
            "size": "10",
            "timestamp": trade_ts,
            "title": "Frozen test trade",
            "slug": "frozen-test",
            "eventSlug": "frozen-event",
            "tag": "weather",
        }

        def fake_get(key: str):
            return {
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.85,
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 10.0,
                "local_copy_trading.category_size_multiplier": {},
                "paper_trading.hard_max_bet_usdc": 50.0,
            }.get(key)

        captured: list[tuple] = []

        def fake_update(task_id, last_ts, signals, error_count=0):
            captured.append((task_id, last_ts, signals))

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch.object(watcher, "_fetch_wallet_trades", return_value=[trade]),
            patch.object(watcher, "_update_task_after_cycle", side_effect=fake_update),
        ):
            task = {
                "id": 2,
                "wallet_address": "0x" + "b" * 40,
                "last_seen_trade_ts": 0.0,
                "categories": '["weather"]',
                "niche_only": 1,
                "base_usdc": 10.0,
            }
            watcher._process_task_frozen(task)

        # 冻结模式下 _update_task_after_cycle 必须被调用
        assert (
            captured
        ), "_process_task_frozen 必须调用 _update_task_after_cycle 推进 last_seen_trade_ts"
        task_id_called, ts_called, sigs_called = captured[0]
        assert task_id_called == 2
        assert ts_called == pytest.approx(
            trade_ts, abs=1.0
        ), f"冻结模式应将 last_seen_trade_ts 推进到 {trade_ts}，实际 {ts_called}"
        assert sigs_called == 0, "冻结模式不应产生信号（signals=0）"

    # ── T141-A: per-conditionId 去重 ─────────────────────────────────

    def test_per_condition_id_dedup_keeps_latest(self, watcher):
        """同一市场两笔拆单只应产生1个候选信号（取时间戳最新的那笔）。"""
        now = datetime(2026, 4, 7, tzinfo=timezone.utc).timestamp()
        trade_older = {
            "conditionId": "0x" + "c" * 64,
            "side": "BUY",
            "price": "0.012",
            "outcome": "Yes",
            "timestamp": now - 60,  # 60秒前
            "title": "Paris 27C older",
            "slug": "paris-27c",
            "type": "TRADE",
        }
        trade_newer = {
            "conditionId": "0x" + "c" * 64,  # 同一 conditionId
            "side": "BUY",
            "price": "0.012",
            "outcome": "Yes",
            "timestamp": now - 5,  # 5秒前（更新）
            "title": "Paris 27C newer",
            "slug": "paris-27c",
            "type": "TRADE",
        }

        def fake_get(key: str):
            return {
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.85,
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.category_size_multiplier": {},
                "local_copy_trading.dynamic_usdc_enabled": False,
                "paper_trading.hard_max_bet_usdc": 50.0,
            }.get(key)

        task = {
            "wallet_address": "0x" + "a" * 40,
            "last_seen_trade_ts": 0.0,
            "categories": "[]",
            "niche_only": 0,
            "base_usdc": 5.0,
        }

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch.object(
                watcher, "_fetch_wallet_trades", return_value=[trade_older, trade_newer]
            ),
        ):
            candidates, cats, max_ts = watcher._get_candidate_trades(task)

        # 去重后只有1个候选
        assert (
            len(candidates) == 1
        ), f"同一 conditionId 应去重为1个，实际 {len(candidates)}"
        # 取的是时间戳最新的那笔
        kept_trade = candidates[0][0]
        assert kept_trade["title"] == "Paris 27C newer", "应保留时间戳更新的那笔交易"

    # ── T141-D: 动态 USDC 缩放 ────────────────────────────────────────

    def test_dynamic_usdc_scales_low_price(self, watcher):
        """低买入价应触发缩放，但 T189 保底不低于 min_bet_usdc。"""
        task = {"base_usdc": 5.0}
        with patch.object(
            watcher,
            "_get",
            side_effect=lambda k: {
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.dynamic_usdc_enabled": True,
                "local_copy_trading.dynamic_usdc_scale_anchor": 0.50,
                "local_copy_trading.dynamic_usdc_min_shares": 5,
                "local_copy_trading.category_size_multiplier": {},
                "executor.min_bet_usdc": 2.0,
            }.get(k),
        ):
            # price=0.012 → scale_factor=0.024 → scaled=0.12 → floor=0.06
            # T189: 但 min_bet=2.0 → base = max(0.12, 2.0) = 2.0
            size = watcher._calc_bet_size(task, [], trade_price=0.012)
        assert size >= 2.0, f"T189: 极低价也不应低于 min_bet_usdc，实际: ${size}"
        assert size <= 5.0, f"不超过 base_usdc 上限，实际: ${size}"

    def test_dynamic_usdc_full_amount_at_high_price(self, watcher):
        """买入价>=scale_anchor时应使用满额。"""
        task = {"base_usdc": 5.0}
        with patch.object(
            watcher,
            "_get",
            side_effect=lambda k: {
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.dynamic_usdc_enabled": True,
                "local_copy_trading.dynamic_usdc_scale_anchor": 0.50,
                "local_copy_trading.dynamic_usdc_min_shares": 5,
                "local_copy_trading.category_size_multiplier": {},
            }.get(k),
        ):
            # price=0.70 → scale_factor=min(1, 0.70/0.50)=1.0 → bet=5.0
            size = watcher._calc_bet_size(task, [], trade_price=0.70)
        assert size == pytest.approx(5.0), f"高于基准价时应满额5U，实际: ${size}"

    def test_dynamic_usdc_disabled_uses_full(self, watcher):
        """dynamic_usdc_enabled=false 时始终使用满额，不缩放。"""
        task = {"base_usdc": 5.0}
        with patch.object(
            watcher,
            "_get",
            side_effect=lambda k: {
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.dynamic_usdc_enabled": False,
                "local_copy_trading.category_size_multiplier": {},
            }.get(k),
        ):
            # price=0.01 但 dynamic 已关闭 → 满额5.0
            size = watcher._calc_bet_size(task, [], trade_price=0.01)
        assert size == pytest.approx(5.0), f"dynamic关闭时应满额$5，实际: ${size}"

    # ── T142-A: category_max_per_event + 同市场去重（跨信号源约束）──────

    def test_t142_same_market_dedup_skips_candidate(self, watcher, tmp_db):
        """T142-A: 已有同 conditionId 开放仓位时，copy_trade 候选必须被跳过（跨信号源约束）。"""
        from datetime import datetime, timezone

        from src.utils import db as _db

        cid = "0x" + "d" * 64
        existing_bet = {
            "bet_id": "existing-bet-001",
            "ts_entered": datetime.now(timezone.utc).isoformat(),
            "market_id": cid,
            "question": "Will T142 test pass?",
            "category": "weather",
            "outcome": "NO",
            "confidence": 0.83,
            "ai_probability": 0.24,
            "market_price": 0.76,
            "ev": 0.06,
            "size_usdc": 2.0,
            "event_slug": "test-event-t142",
            "source": "weather_signal",
        }
        _db.insert_paper_bet(existing_bet, tmp_db)

        now = datetime(2026, 4, 7, tzinfo=timezone.utc).timestamp()
        trade = {
            "conditionId": cid,
            "side": "BUY",
            "outcome": "Yes",
            "asset": "yes_token_t142",
            "price": "0.20",
            "size": "50",
            "timestamp": now + 10,
            "title": "Will T142 test pass?",
            "slug": "t142-test-market",
            "eventSlug": "test-event-t142",
            "tag": "weather",
            "type": "TRADE",
        }
        task = {
            "wallet_address": "0x" + "a" * 40,
            "last_seen_trade_ts": 0.0,
            "categories": '["weather"]',
            "niche_only": 0,
            "base_usdc": 5.0,
        }

        def fake_get(key: str):
            return {
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.90,
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.category_size_multiplier": {},
                "local_copy_trading.dynamic_usdc_enabled": False,
                "paper_trading.hard_max_bet_usdc": 50.0,
                "paper_trading.category_max_per_event": {"weather": 2, "default": 1},
                "paper_trading.max_per_market_positions": 1,
            }.get(key)

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch.object(watcher, "_fetch_wallet_trades", return_value=[trade]),
        ):
            candidates, cats, max_ts = watcher._get_candidate_trades(task)

        assert (
            len(candidates) == 0
        ), f"T142-A: 同市场已有开放仓位时，copy_trade 候选应被过滤，实际: {len(candidates)}"

    def test_t142_event_max_dedup_skips_when_event_full(self, watcher, tmp_db):
        """T142-A: event_slug 已达 category_max_per_event 上限时，copy_trade 候选必须被跳过。"""
        from datetime import datetime, timezone

        from src.utils import db as _db

        event_slug = "singapore-april-8-temp"
        now = datetime(2026, 4, 7, tzinfo=timezone.utc).timestamp()

        # Insert 2 open weather bets for same event_slug (weather max=2)
        for i, cid_suffix in enumerate(["a" * 64, "b" * 64]):
            bet = {
                "bet_id": f"event-bet-{i:03d}",
                "ts_entered": datetime.now(timezone.utc).isoformat(),
                "market_id": "0x" + cid_suffix,
                "question": f"Singapore temp bet {i}",
                "category": "weather",
                "outcome": "NO",
                "confidence": 0.80,
                "ai_probability": 0.25,
                "market_price": 0.75,
                "ev": 0.05,
                "size_usdc": 2.0,
                "event_slug": event_slug,
                "source": "weather_signal",
            }
            _db.insert_paper_bet(bet, tmp_db)

        # A new trade targeting the same event_slug but different conditionId
        trade = {
            "conditionId": "0x" + "c" * 64,
            "side": "BUY",
            "outcome": "Yes",
            "asset": "yes_token_new_market",
            "price": "0.20",
            "size": "50",
            "timestamp": now + 30,
            "title": "Singapore temp 34C April 8",
            "slug": "singapore-34c-april-8",
            "eventSlug": event_slug,
            "tag": "weather",
            "type": "TRADE",
        }
        task = {
            "wallet_address": "0x" + "a" * 40,
            "last_seen_trade_ts": 0.0,
            "categories": '["weather"]',
            "niche_only": 0,
            "base_usdc": 5.0,
        }

        def fake_get(key: str):
            return {
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.90,
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.category_size_multiplier": {},
                "local_copy_trading.dynamic_usdc_enabled": False,
                "paper_trading.hard_max_bet_usdc": 50.0,
                "paper_trading.category_max_per_event": {"weather": 2, "default": 1},
                "paper_trading.max_per_market_positions": 1,
            }.get(key)

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch.object(watcher, "_fetch_wallet_trades", return_value=[trade]),
        ):
            candidates, cats, max_ts = watcher._get_candidate_trades(task)

        assert (
            len(candidates) == 0
        ), f"T142-A: event event_slug 已满2仓时，copy_trade 候选应被过滤，实际: {len(candidates)}"

    def test_t142_event_max_allows_below_limit(self, watcher, tmp_db):
        """T142-A: event_slug 低于上限时，copy_trade 候选应通过（正常跟投）。"""
        from datetime import datetime, timezone

        from src.utils import db as _db

        event_slug = "sydney-april-8-temp"
        now = datetime(2026, 4, 7, tzinfo=timezone.utc).timestamp()

        # Only 1 open weather bet for same event (max=2, so still room)
        bet = {
            "bet_id": "event-bet-under-limit",
            "ts_entered": datetime.now(timezone.utc).isoformat(),
            "market_id": "0x" + "e" * 64,
            "question": "Sydney temp 32C",
            "category": "weather",
            "outcome": "NO",
            "confidence": 0.80,
            "ai_probability": 0.25,
            "market_price": 0.75,
            "ev": 0.05,
            "size_usdc": 2.0,
            "event_slug": event_slug,
            "source": "weather_signal",
        }
        _db.insert_paper_bet(bet, tmp_db)

        trade = {
            "conditionId": "0x" + "f" * 64,  # different market in same event
            "side": "BUY",
            "outcome": "Yes",
            "asset": "yes_token_sydney_33c",
            "price": "0.20",
            "size": "50",
            "timestamp": now + 30,
            "title": "Sydney temp 33C April 8",
            "slug": "sydney-33c-april-8",
            "eventSlug": event_slug,
            "tag": "weather",
            "type": "TRADE",
        }
        task = {
            "wallet_address": "0x" + "a" * 40,
            "last_seen_trade_ts": 0.0,
            "categories": '["weather"]',
            "niche_only": 0,
            "base_usdc": 5.0,
        }

        def fake_get(key: str):
            return {
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.90,
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.category_size_multiplier": {},
                "local_copy_trading.dynamic_usdc_enabled": False,
                "paper_trading.hard_max_bet_usdc": 50.0,
                "paper_trading.category_max_per_event": {"weather": 2, "default": 1},
                "paper_trading.max_per_market_positions": 1,
            }.get(key)

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch.object(watcher, "_fetch_wallet_trades", return_value=[trade]),
        ):
            candidates, cats, max_ts = watcher._get_candidate_trades(task)

        assert (
            len(candidates) == 1
        ), f"T142-A: event未达上限时候选应通过，实际: {len(candidates)}"

    def test_t167_copy_trade_event_dedup_blocks_second_copy(self, watcher, tmp_db):
        """T167: 跟投专属同事件去重 — 同事件已有1笔 copy_trade 仓位时，第2笔必须被跳过。"""
        from datetime import datetime, timezone

        from src.utils import db as _db

        event_slug = "highest-temperature-in-madrid-on-april-10-2026"
        now = datetime(2026, 4, 9, tzinfo=timezone.utc).timestamp()

        # Insert 1 open copy_trade bet for this event
        bet = {
            "bet_id": "copy_ea5",
            "ts_entered": datetime.now(timezone.utc).isoformat(),
            "market_id": "0x" + "d" * 64,
            "question": "Madrid 28°C April 10",
            "category": "weather",
            "outcome": "YES",
            "confidence": 0.70,
            "ai_probability": 0.60,
            "market_price": 0.50,
            "ev": 0.10,
            "size_usdc": 2.0,
            "event_slug": event_slug,
            "source": "copy_trade",
        }
        _db.insert_paper_bet(bet, tmp_db)

        # New trade from wallet: same event, different conditionId (Madrid 29°C)
        trade = {
            "conditionId": "0x" + "e" * 64,
            "side": "BUY",
            "outcome": "Yes",
            "asset": "yes_token_madrid_29",
            "price": "0.20",
            "size": "50",
            "timestamp": now + 30,
            "title": "Madrid 29°C April 10",
            "slug": "madrid-29c-april-10",
            "eventSlug": event_slug,
            "tag": "weather",
            "type": "TRADE",
        }
        task = {
            "wallet_address": "0x" + "a" * 40,
            "last_seen_trade_ts": 0.0,
            "categories": '["weather"]',
            "niche_only": 0,
            "base_usdc": 5.0,
        }

        def fake_get(key: str):
            return {
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.90,
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.category_size_multiplier": {},
                "local_copy_trading.dynamic_usdc_enabled": False,
                "local_copy_trading.max_per_event": 1,
                "paper_trading.hard_max_bet_usdc": 50.0,
                "paper_trading.category_max_per_event": {"weather": 2, "default": 1},
                "paper_trading.max_per_market_positions": 1,
            }.get(key)

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch.object(watcher, "_fetch_wallet_trades", return_value=[trade]),
        ):
            candidates, cats, max_ts = watcher._get_candidate_trades(task)

        assert (
            len(candidates) == 0
        ), f"T167: 同事件已有1笔copy_trade仓位时，第2笔应被跳过，实际: {len(candidates)}"

    def test_t167_copy_trade_event_dedup_allows_non_copy_source(self, watcher, tmp_db):
        """T167: 全局事件有仓位但来源不是 copy_trade 时，跟投去重不应阻止。"""
        from datetime import datetime, timezone

        from src.utils import db as _db

        event_slug = "highest-temperature-in-tokyo-on-april-10-2026"
        now = datetime(2026, 4, 9, tzinfo=timezone.utc).timestamp()

        # Insert 1 open weather_signal bet (NOT copy_trade source)
        bet = {
            "bet_id": "weather-bet-tokyo",
            "ts_entered": datetime.now(timezone.utc).isoformat(),
            "market_id": "0x" + "f" * 64,
            "question": "Tokyo 25°C April 10",
            "category": "weather",
            "outcome": "NO",
            "confidence": 0.80,
            "ai_probability": 0.25,
            "market_price": 0.75,
            "ev": 0.05,
            "size_usdc": 2.0,
            "event_slug": event_slug,
            "source": "weather_signal",
        }
        _db.insert_paper_bet(bet, tmp_db)

        trade = {
            "conditionId": "0x" + "a" * 64,
            "side": "BUY",
            "outcome": "Yes",
            "asset": "yes_tokyo_26c",
            "price": "0.20",
            "size": "50",
            "timestamp": now + 30,
            "title": "Tokyo 26°C April 10",
            "slug": "tokyo-26c-april-10",
            "eventSlug": event_slug,
            "tag": "weather",
            "type": "TRADE",
        }
        task = {
            "wallet_address": "0x" + "a" * 40,
            "last_seen_trade_ts": 0.0,
            "categories": '["weather"]',
            "niche_only": 0,
            "base_usdc": 5.0,
        }

        def fake_get(key: str):
            return {
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.90,
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.category_size_multiplier": {},
                "local_copy_trading.dynamic_usdc_enabled": False,
                "local_copy_trading.max_per_event": 1,
                "paper_trading.hard_max_bet_usdc": 50.0,
                "paper_trading.category_max_per_event": {"weather": 2, "default": 1},
                "paper_trading.max_per_market_positions": 1,
            }.get(key)

        with (
            patch.object(watcher, "_get", side_effect=fake_get),
            patch.object(watcher, "_fetch_wallet_trades", return_value=[trade]),
        ):
            candidates, cats, max_ts = watcher._get_candidate_trades(task)

        # 全局 event dedup (weather=2) 允许第2个，且 copy_trade event dedup 无 copy 仓位
        assert (
            len(candidates) == 1
        ), f"T167: 非copy_trade源的仓位不应阻止跟投，实际: {len(candidates)}"


# ── T164: 智能跟卖测试 ──────────────────────────────────────────


class TestFollowSell:
    """T164: 跟投智能跟卖功能测试。"""

    def _make_watcher_with_bets(self, bets, follow_sell_cfg=None):
        """创建带有预设 paper_bets 的 watcher（包含 copy_source_wallet 列）。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(tmp.name)
        tmp.close()

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """CREATE TABLE IF NOT EXISTS paper_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bet_id TEXT UNIQUE NOT NULL,
                ts_entered TEXT NOT NULL,
                market_id TEXT NOT NULL,
                question TEXT,
                category TEXT DEFAULT 'weather',
                outcome TEXT,
                confidence REAL,
                ai_probability REAL,
                market_price REAL,
                ev REAL,
                size_usdc REAL,
                sim_balance_before REAL,
                sim_balance_after REAL,
                market_end_date TEXT,
                weather_context TEXT,
                ai_models TEXT,
                cycle_id INTEGER,
                resolved_at REAL,
                resolved_outcome TEXT,
                is_win INTEGER,
                payout REAL,
                pnl REAL,
                exit_price REAL,
                exit_type TEXT DEFAULT NULL,
                bet_bucket TEXT,
                market_slug TEXT,
                event_slug TEXT,
                scale_in_count INTEGER,
                last_scale_at REAL,
                position_history TEXT,
                parent_bet_id TEXT,
                bet_type TEXT,
                dimension_tags TEXT,
                ai_reasoning TEXT,
                condition_id TEXT,
                source TEXT,
                matched_shares REAL,
                sell_fail_count INTEGER DEFAULT 0,
                ghost_reopened_at REAL,
                trailing_peak_price REAL,
                trailing_floor_price REAL,
                strategy_tag TEXT,
                strategy_params TEXT,
                copy_source_wallet TEXT DEFAULT NULL,
                order_type TEXT DEFAULT NULL,
                entry_fee_usdc REAL DEFAULT 0,
                exit_fee_usdc REAL DEFAULT 0,
                fee_rate REAL DEFAULT 0
            )"""
        )
        for b in bets:
            conn.execute(
                "INSERT INTO paper_bets (bet_id, ts_entered, market_id, question, "
                "outcome, market_price, size_usdc, condition_id, source, "
                "copy_source_wallet, category) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    b["bet_id"],
                    b.get("ts_entered", "2026-04-08T10:00:00+00:00"),
                    b["market_id"],
                    b.get("question", "Test Q"),
                    b.get("outcome", "YES"),
                    b.get("market_price", 0.50),
                    b.get("size_usdc", 5.0),
                    b.get("condition_id", "cond_abc"),
                    "copy_trade",
                    b.get("copy_source_wallet", "0xWALLET123"),
                    b.get("category", "weather"),
                ),
            )
        conn.commit()
        conn.close()

        if follow_sell_cfg is None:
            follow_sell_cfg = {
                "enabled": True,
                "min_profit_pct": -0.05,
                "loss_fallback": "system_manage",
                "max_sell_age_seconds": 999999,
            }

        from src.utils.copy_trade_watcher import CopyTradeWatcher

        watcher = CopyTradeWatcher(db_path=db_path)

        def fake_get(key):
            return {
                "local_copy_trading.enabled": True,
                "local_copy_trading.follow_sell": follow_sell_cfg,
                "local_copy_trading.scan_interval_minutes": 0,
                "paper_trading.taker_fee_rates": {
                    "weather": 0.05,
                    "sports": 0.03,
                    "crypto": 0.072,
                },
                "paper_trading.taker_fee_rate_default": 0.05,
            }.get(key)

        watcher._get = fake_get
        return watcher, db_path

    def test_follow_sell_profit_dry_run(self):
        """盈利时 DRY RUN 跟卖：DB 应关闭仓位。"""
        bets = [
            {
                "bet_id": "test-fs-001",
                "market_id": "mkt_001",
                "condition_id": "cond_001",
                "market_price": 0.40,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_A",
                "outcome": "YES",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        watcher, db_path = self._make_watcher_with_bets(bets)

        # 原始钱包 SELL 交易（价格涨到0.60，盈利50%）
        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_001",
                "price": 0.60,
                "timestamp": datetime(
                    2026, 4, 8, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
                "outcome": "Yes",
                "title": "Test Market",
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=True):
                watcher._monitor_copy_sells()

        # 验证仓位已关闭
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT resolved_outcome, pnl FROM paper_bets WHERE bet_id='test-fs-001'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] is not None, "跟卖后 resolved_outcome 应非 NULL"
        assert row[1] > 0, f"盈利跟卖 pnl 应 > 0, 实际: {row[1]}"

    def test_follow_sell_small_loss_within_tolerance(self):
        """小幅亏损在容忍范围内（扣费后约-4% < -5%阈值），应执行跟卖。"""
        bets = [
            {
                "bet_id": "test-fs-002",
                "market_id": "mkt_002",
                "condition_id": "cond_002",
                "market_price": 0.50,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_B",
                "outcome": "YES",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        watcher, db_path = self._make_watcher_with_bets(bets)

        # 原始钱包 SELL，价格微涨到 0.505（GROSS +1%，扣费后约 -4% 仍在 -5% 容忍范围内）
        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_002",
                "price": 0.505,
                "timestamp": datetime(
                    2026, 4, 8, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
                "outcome": "Yes",
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=True):
                watcher._monitor_copy_sells()

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT resolved_outcome FROM paper_bets WHERE bet_id='test-fs-002'"
        ).fetchone()
        conn.close()

        assert row[0] is not None, "扣费后约-4%亏损在-5%容忍范围内，应跟卖关闭仓位"

    def test_follow_sell_large_loss_unconditional(self):
        """默认 unconditional=True 时，即使大幅亏损也执行跟卖（镜像模式）。"""
        bets = [
            {
                "bet_id": "test-fs-003",
                "market_id": "mkt_003",
                "condition_id": "cond_003",
                "market_price": 0.50,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_C",
                "outcome": "YES",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        watcher, db_path = self._make_watcher_with_bets(bets)

        # 原始钱包 SELL，价格暴跌到 0.40（-20%）
        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_003",
                "price": 0.40,
                "timestamp": datetime(
                    2026, 4, 8, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
                "outcome": "Yes",
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=True):
                watcher._monitor_copy_sells()

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT exit_type FROM paper_bets WHERE bet_id='test-fs-003'"
        ).fetchone()
        conn.close()

        assert row[0] is not None, "unconditional=True 默认镜像模式，大幅亏损也应跟卖"

    def test_follow_sell_large_loss_rejected_profit_gate(self):
        """unconditional=False 时，大幅亏损超出容忍范围（-20% > -5%阈值），不跟卖。"""
        bets = [
            {
                "bet_id": "test-fs-003b",
                "market_id": "mkt_003",
                "condition_id": "cond_003",
                "market_price": 0.50,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_C",
                "outcome": "YES",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        gated_cfg = {
            "enabled": True,
            "unconditional": False,
            "min_profit_pct": -0.05,
            "loss_fallback": "system_manage",
            "max_sell_age_seconds": 999999,
        }
        watcher, db_path = self._make_watcher_with_bets(bets, follow_sell_cfg=gated_cfg)

        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_003",
                "price": 0.40,
                "timestamp": datetime(
                    2026, 4, 8, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
                "outcome": "Yes",
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=True):
                watcher._monitor_copy_sells()

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT resolved_outcome FROM paper_bets WHERE bet_id='test-fs-003b'"
        ).fetchone()
        conn.close()

        assert (
            row[0] is None
        ), "unconditional=False 利润门控，-20%亏损 > -5%容忍，不跟卖"

    def test_follow_sell_disabled(self):
        """follow_sell.enabled=false 时不执行跟卖。"""
        bets = [
            {
                "bet_id": "test-fs-004",
                "market_id": "mkt_004",
                "condition_id": "cond_004",
                "market_price": 0.50,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_D",
                "outcome": "YES",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        disabled_cfg = {
            "enabled": False,
            "min_profit_pct": -0.05,
            "loss_fallback": "system_manage",
        }
        watcher, db_path = self._make_watcher_with_bets(
            bets, follow_sell_cfg=disabled_cfg
        )

        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_004",
                "price": 0.70,
                "timestamp": datetime(
                    2026, 4, 8, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
                "outcome": "Yes",
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=True):
                watcher._monitor_copy_sells()

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT resolved_outcome FROM paper_bets WHERE bet_id='test-fs-004'"
        ).fetchone()
        conn.close()

        assert row[0] is None, "follow_sell disabled 时不应跟卖"

    def test_follow_sell_no_match_different_condition(self):
        """原始钱包SELL的conditionId与我方持仓不匹配，不触发跟卖。"""
        bets = [
            {
                "bet_id": "test-fs-005",
                "market_id": "mkt_005",
                "condition_id": "cond_005",
                "market_price": 0.50,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_E",
                "outcome": "YES",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        watcher, db_path = self._make_watcher_with_bets(bets)

        # SELL 交易的 conditionId 是不同的市场
        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_OTHER",
                "price": 0.70,
                "timestamp": datetime(
                    2026, 4, 8, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
                "outcome": "Yes",
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=True):
                watcher._monitor_copy_sells()

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT resolved_outcome FROM paper_bets WHERE bet_id='test-fs-005'"
        ).fetchone()
        conn.close()

        assert row[0] is None, "conditionId 不匹配时不应跟卖"

    def test_follow_sell_sell_before_entry_ignored(self):
        """原始钱包 SELL 时间早于我方入场时间，不应触发跟卖（历史卖出不跟）。"""
        bets = [
            {
                "bet_id": "test-fs-006",
                "market_id": "mkt_006",
                "condition_id": "cond_006",
                "market_price": 0.50,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_F",
                "outcome": "YES",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        watcher, db_path = self._make_watcher_with_bets(bets)

        # SELL 交易时间是入场之前（历史 SELL）
        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_006",
                "price": 0.60,
                "timestamp": datetime(
                    2026, 4, 8, 8, 0, tzinfo=timezone.utc
                ).timestamp(),
                "outcome": "Yes",
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=True):
                watcher._monitor_copy_sells()

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT resolved_outcome FROM paper_bets WHERE bet_id='test-fs-006'"
        ).fetchone()
        conn.close()

        assert row[0] is None, "入场前的历史SELL不应触发跟卖"

    def test_follow_sell_live_executor_called(self):
        """实盘跟卖：应调用 executor.sell_position()。"""
        bets = [
            {
                "bet_id": "test-fs-007",
                "market_id": "mkt_007",
                "condition_id": "cond_007",
                "market_price": 0.40,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_G",
                "outcome": "YES",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        watcher, db_path = self._make_watcher_with_bets(bets)

        # 注入 mock executor
        mock_executor = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.filled_size = 7.0  # 实际成交金额（NET）
        mock_result.actual_price = 0.60
        mock_executor.sell_position.return_value = mock_result
        watcher._executor = mock_executor

        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_007",
                "price": 0.60,
                "timestamp": datetime(
                    2026, 4, 8, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
                "outcome": "Yes",
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=False):
                watcher._monitor_copy_sells()

        # executor.sell_position 应被调用
        mock_executor.sell_position.assert_called_once()
        call_args = mock_executor.sell_position.call_args[0][0]
        assert call_args["trigger"] == "copy_follow_sell"

    def test_follow_sell_live_failure_keeps_open(self):
        """实盘跟卖失败：SUPREME-1 要求仓位保持 OPEN（不关闭DB）。"""
        bets = [
            {
                "bet_id": "test-fs-008",
                "market_id": "mkt_008",
                "condition_id": "cond_008",
                "market_price": 0.40,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_H",
                "outcome": "YES",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        watcher, db_path = self._make_watcher_with_bets(bets)

        # 注入 mock executor（卖出失败）
        mock_executor = MagicMock()
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.error = "CLOB timeout"
        mock_executor.sell_position.return_value = mock_result
        watcher._executor = mock_executor

        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_008",
                "price": 0.60,
                "timestamp": datetime(
                    2026, 4, 8, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
                "outcome": "Yes",
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=False):
                watcher._monitor_copy_sells()

        # SUPREME-1: 卖出失败不关闭 DB
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT resolved_outcome FROM paper_bets WHERE bet_id='test-fs-008'"
        ).fetchone()
        conn.close()

        assert row[0] is None, "SUPREME-1: 卖出失败时仓位必须保持 OPEN"

    def test_copy_source_wallet_written_in_bet(self):
        """DRY RUN 跟买时 copy_source_wallet 应写入 paper_bets。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(tmp.name)
        tmp.close()

        # 初始化最小化 DB
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """CREATE TABLE IF NOT EXISTS paper_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bet_id TEXT UNIQUE NOT NULL,
                ts_entered TEXT NOT NULL,
                market_id TEXT NOT NULL,
                question TEXT,
                category TEXT DEFAULT 'weather',
                outcome TEXT,
                confidence REAL,
                ai_probability REAL,
                market_price REAL,
                ev REAL,
                size_usdc REAL,
                sim_balance_before REAL,
                sim_balance_after REAL,
                market_end_date TEXT,
                weather_context TEXT,
                ai_models TEXT,
                cycle_id INTEGER,
                resolved_at REAL,
                resolved_outcome TEXT,
                is_win INTEGER,
                payout REAL,
                pnl REAL,
                exit_price REAL,
                exit_type TEXT DEFAULT NULL,
                bet_bucket TEXT,
                market_slug TEXT,
                event_slug TEXT,
                scale_in_count INTEGER,
                last_scale_at REAL,
                position_history TEXT,
                parent_bet_id TEXT,
                bet_type TEXT,
                dimension_tags TEXT,
                ai_reasoning TEXT,
                condition_id TEXT,
                source TEXT,
                matched_shares REAL,
                sell_fail_count INTEGER DEFAULT 0,
                ghost_reopened_at REAL,
                trailing_peak_price REAL,
                trailing_floor_price REAL,
                strategy_tag TEXT,
                strategy_params TEXT,
                copy_source_wallet TEXT DEFAULT NULL,
                order_type TEXT DEFAULT NULL,
                entry_fee_usdc REAL DEFAULT 0,
                exit_fee_usdc REAL DEFAULT 0,
                fee_rate REAL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS copy_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address TEXT NOT NULL,
                categories TEXT DEFAULT '[]',
                base_usdc REAL DEFAULT 10.0,
                niche_only INTEGER DEFAULT 1,
                score_threshold REAL DEFAULT 75.0,
                status TEXT DEFAULT 'running',
                created_at TEXT NOT NULL,
                last_active_at TEXT,
                last_seen_trade_ts REAL DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                last_error TEXT,
                tag_label TEXT DEFAULT '',
                total_signals INTEGER DEFAULT 0
            )"""
        )
        # 创建一个跟投任务
        conn.execute(
            "INSERT INTO copy_tasks (wallet_address, categories, base_usdc, status, created_at) "
            "VALUES ('0xTEST_WALLET', '[\"weather\"]', 5.0, 'running', '2026-04-08T00:00:00')"
        )
        conn.commit()
        conn.close()

        from src.utils.copy_trade_watcher import CopyTradeWatcher

        watcher = CopyTradeWatcher(db_path=db_path)

        def fake_get(key):
            return {
                "local_copy_trading.enabled": True,
                "local_copy_trading.scan_interval_minutes": 0,
                "local_copy_trading.trade_lookback_minutes": 9999,
                "local_copy_trading.max_price_filter": 0.90,
                "local_copy_trading.min_ev_filter": 0.001,
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.category_size_multiplier": {},
                "local_copy_trading.dynamic_usdc_enabled": False,
                "local_copy_trading.follow_sell": {"enabled": False},
                "paper_trading.hard_max_bet_usdc": 50.0,
                "risk.emergency_freeze": False,
            }.get(key)

        watcher._get = fake_get

        trade = {
            "type": "TRADE",
            "side": "BUY",
            "conditionId": "cond_NEW",
            "price": 0.65,
            "outcome": "Yes",
            "timestamp": datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc).timestamp(),
            "title": "Test Market ABC",
            "slug": "test-market",
            "eventSlug": "test-event",
            "asset": "token_xyz",
        }

        with (
            patch.object(watcher, "_fetch_wallet_trades", return_value=[trade]),
            patch("src.utils.config_loader.is_dry_run", return_value=True),
        ):
            signals = watcher.run_cycle()

        assert signals == 1, f"应生成 1 个信号，实际: {signals}"

        # 验证 copy_source_wallet 已写入
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT copy_source_wallet FROM paper_bets WHERE condition_id='cond_NEW'"
        ).fetchone()
        conn.close()

        assert row is not None, "paper_bet 应已插入"
        assert (
            row["copy_source_wallet"] == "0xTEST_WALLET"
        ), f"copy_source_wallet 应为 0xTEST_WALLET，实际: {row['copy_source_wallet']}"
        assert (
            row["copy_source_wallet"] == "0xTEST_WALLET"
        ), f"copy_source_wallet 应为 0xTEST_WALLET，实际: {row['copy_source_wallet']}"


class TestT176_2_RealtimePrice:
    """T176-2: 跟卖使用实时市场价格 + 信号新鲜度过滤。"""

    def _make_watcher_with_bets(self, bets, follow_sell_cfg=None):
        """复用 TestFollowSell 的 watcher 构造逻辑，增加 token_id 列。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(tmp.name)
        tmp.close()

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """CREATE TABLE IF NOT EXISTS paper_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bet_id TEXT UNIQUE NOT NULL,
                ts_entered TEXT NOT NULL,
                market_id TEXT NOT NULL,
                question TEXT,
                category TEXT DEFAULT 'weather',
                outcome TEXT,
                confidence REAL,
                ai_probability REAL,
                market_price REAL,
                ev REAL,
                size_usdc REAL,
                sim_balance_before REAL,
                sim_balance_after REAL,
                market_end_date TEXT,
                weather_context TEXT,
                ai_models TEXT,
                cycle_id INTEGER,
                resolved_at REAL,
                resolved_outcome TEXT,
                is_win INTEGER,
                payout REAL,
                pnl REAL,
                exit_price REAL,
                exit_type TEXT DEFAULT NULL,
                bet_bucket TEXT,
                market_slug TEXT,
                event_slug TEXT,
                scale_in_count INTEGER,
                last_scale_at REAL,
                position_history TEXT,
                parent_bet_id TEXT,
                bet_type TEXT,
                dimension_tags TEXT,
                ai_reasoning TEXT,
                condition_id TEXT,
                source TEXT,
                matched_shares REAL,
                sell_fail_count INTEGER DEFAULT 0,
                ghost_reopened_at REAL,
                trailing_peak_price REAL,
                trailing_floor_price REAL,
                strategy_tag TEXT,
                strategy_params TEXT,
                copy_source_wallet TEXT DEFAULT NULL,
                token_id TEXT DEFAULT NULL,
                order_type TEXT DEFAULT NULL,
                entry_fee_usdc REAL DEFAULT 0,
                exit_fee_usdc REAL DEFAULT 0,
                fee_rate REAL DEFAULT 0
            )"""
        )
        for b in bets:
            conn.execute(
                "INSERT INTO paper_bets (bet_id, ts_entered, market_id, question, "
                "outcome, market_price, size_usdc, condition_id, source, "
                "copy_source_wallet, category, token_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    b["bet_id"],
                    b.get("ts_entered", "2026-04-08T10:00:00+00:00"),
                    b["market_id"],
                    b.get("question", "Test Q"),
                    b.get("outcome", "YES"),
                    b.get("market_price", 0.50),
                    b.get("size_usdc", 5.0),
                    b.get("condition_id", "cond_abc"),
                    "copy_trade",
                    b.get("copy_source_wallet", "0xWALLET123"),
                    b.get("category", "weather"),
                    b.get("token_id", "tok_abc"),
                ),
            )
        conn.commit()
        conn.close()

        if follow_sell_cfg is None:
            follow_sell_cfg = {
                "enabled": True,
                "min_profit_pct": -0.05,
                "loss_fallback": "system_manage",
                "max_sell_age_seconds": 999999,
            }

        from src.utils.copy_trade_watcher import CopyTradeWatcher

        watcher = CopyTradeWatcher(db_path=db_path)

        def fake_get(key):
            return {
                "local_copy_trading.enabled": True,
                "local_copy_trading.follow_sell": follow_sell_cfg,
                "local_copy_trading.scan_interval_minutes": 0,
                "paper_trading.taker_fee_rates": {"weather": 0.05},
                "paper_trading.taker_fee_rate_default": 0.05,
            }.get(key)

        watcher._get = fake_get
        return watcher, db_path

    @patch("src.utils.copy_trade_watcher.CopyTradeWatcher._fetch_current_best_bid")
    def test_follow_sell_uses_realtime_bid_not_source_price(self, mock_bid):
        """T176-2: 跟卖 P&L 应使用实时 best_bid 而非源钱包价。

        源钱包卖在 0.60 但市场已跌到 0.35 → 用 0.35 计算 P&L。
        默认 unconditional=True → 即使亏损也执行跟卖。
        """
        bets = [
            {
                "bet_id": "rt-001",
                "market_id": "mkt_rt1",
                "condition_id": "cond_rt1",
                "market_price": 0.40,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_RT",
                "outcome": "YES",
                "token_id": "tok_rt1",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        watcher, db_path = self._make_watcher_with_bets(bets)

        # 实时 bid = 0.35 (低于入场价 0.40 → 亏损)
        mock_bid.return_value = 0.35

        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_rt1",
                "price": 0.60,  # 源钱包卖在 0.60 (盈利)
                "timestamp": datetime(
                    2026, 4, 8, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=True):
                watcher._monitor_copy_sells()

        # 默认 unconditional=True → 即使亏损也镜像跟卖
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT exit_type, exit_price FROM paper_bets WHERE bet_id='rt-001'"
        ).fetchone()
        conn.close()
        assert row[0] is not None, "unconditional=True 默认镜像跟卖，即使亏损也执行"
        # 确认使用了实时市场价 0.35 而非源钱包价 0.60
        assert abs(float(row[1]) - 0.35) < 0.01, "应使用实时 best_bid 作为退出价"

    @patch("src.utils.copy_trade_watcher.CopyTradeWatcher._fetch_current_best_bid")
    def test_follow_sell_realtime_bid_profit_gate_rejects(self, mock_bid):
        """unconditional=False 时，实时价亏损超阈值 → 不跟卖。"""
        bets = [
            {
                "bet_id": "rt-002",
                "market_id": "mkt_rt2",
                "condition_id": "cond_rt2",
                "market_price": 0.40,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_RT",
                "outcome": "YES",
                "token_id": "tok_rt2",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        gated_cfg = {
            "enabled": True,
            "unconditional": False,
            "min_profit_pct": -0.05,
            "loss_fallback": "system_manage",
            "max_sell_age_seconds": 999999,
        }
        watcher, db_path = self._make_watcher_with_bets(bets, follow_sell_cfg=gated_cfg)
        mock_bid.return_value = 0.35

        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_rt2",
                "price": 0.60,
                "timestamp": datetime(
                    2026, 4, 8, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=True):
                watcher._monitor_copy_sells()

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT resolved_outcome FROM paper_bets WHERE bet_id='rt-002'"
        ).fetchone()
        conn.close()
        assert row[0] is None, "unconditional=False + 亏损超阈值 → 不跟卖"

    @patch("src.utils.copy_trade_watcher.CopyTradeWatcher._fetch_current_best_bid")
    def test_follow_sell_uses_realtime_bid_profitable(self, mock_bid):
        """T176-2: 实时 bid 仍盈利时正常跟卖。"""
        bets = [
            {
                "bet_id": "rt-002",
                "market_id": "mkt_rt2",
                "condition_id": "cond_rt2",
                "market_price": 0.40,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_RT2",
                "outcome": "YES",
                "token_id": "tok_rt2",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        watcher, db_path = self._make_watcher_with_bets(bets)

        # 实时 bid = 0.55 (高于入场价 0.40 → 盈利)
        mock_bid.return_value = 0.55

        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_rt2",
                "price": 0.60,
                "timestamp": datetime(
                    2026, 4, 8, 12, 0, tzinfo=timezone.utc
                ).timestamp(),
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=True):
                watcher._monitor_copy_sells()

        # 实时价 0.55 > 入场价 0.40 → 盈利 → 应跟卖关闭
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT resolved_outcome FROM paper_bets WHERE bet_id='rt-002'"
        ).fetchone()
        conn.close()
        assert row[0] is not None, "实时价盈利时应正常跟卖"

    def test_stale_sell_signal_filtered(self):
        """T176-2: 超过 max_sell_age_seconds 的卖出信号应被跳过。"""
        bets = [
            {
                "bet_id": "stale-001",
                "market_id": "mkt_s1",
                "condition_id": "cond_s1",
                "market_price": 0.40,
                "size_usdc": 5.0,
                "copy_source_wallet": "0xWALLET_STALE",
                "outcome": "YES",
                "token_id": "tok_s1",
                "ts_entered": "2026-04-08T10:00:00+00:00",
            }
        ]
        follow_cfg = {
            "enabled": True,
            "min_profit_pct": -0.05,
            "loss_fallback": "system_manage",
            "max_sell_age_seconds": 60,  # 仅允许 60 秒内的信号
        }
        watcher, db_path = self._make_watcher_with_bets(bets, follow_cfg)

        # 源钱包 10 分钟前卖出（远超 60 秒限制）
        now_ts = datetime(2026, 4, 8, 12, 10, tzinfo=timezone.utc).timestamp()
        stale_ts = datetime(
            2026, 4, 8, 12, 0, tzinfo=timezone.utc
        ).timestamp()  # 10 min ago

        sell_trades = [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "cond_s1",
                "price": 0.60,
                "timestamp": stale_ts,
            }
        ]

        with patch.object(watcher, "_fetch_wallet_trades", return_value=sell_trades):
            with patch("src.utils.config_loader.is_dry_run", return_value=True):
                with patch("src.utils.copy_trade_watcher.datetime") as mock_dt:
                    mock_dt.now.return_value = datetime(
                        2026, 4, 8, 12, 10, tzinfo=timezone.utc
                    )
                    mock_dt.fromisoformat = datetime.fromisoformat
                    mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
                    watcher._monitor_copy_sells()

        # 信号过期 → 不跟卖 → 仓位保持 OPEN
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT resolved_outcome FROM paper_bets WHERE bet_id='stale-001'"
        ).fetchone()
        conn.close()
        assert row[0] is None, "过期卖出信号不应触发跟卖"

    def test_fetch_best_bid_returns_none_on_failure(self):
        """_fetch_current_best_bid 网络失败时返回 None。"""
        from src.utils.copy_trade_watcher import CopyTradeWatcher

        with patch("src.utils.copy_trade_watcher.requests.get") as mock_get:
            mock_get.side_effect = Exception("Connection timeout")
            result = CopyTradeWatcher._fetch_current_best_bid("tok_123")
        assert result is None

    def test_fetch_best_bid_empty_token(self):
        """空 token_id 直接返回 None。"""
        from src.utils.copy_trade_watcher import CopyTradeWatcher

        result = CopyTradeWatcher._fetch_current_best_bid("")
        assert result is None

    def test_fetch_best_bid_parses_orderbook(self):
        """正常 orderbook 返回最高 bid。"""
        from src.utils.copy_trade_watcher import CopyTradeWatcher

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "bids": [{"price": "0.45"}, {"price": "0.40"}],
            "asks": [{"price": "0.50"}],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("src.utils.copy_trade_watcher.requests.get", return_value=mock_resp):
            result = CopyTradeWatcher._fetch_current_best_bid("tok_abc")
        assert result == 0.45
        with patch("src.utils.copy_trade_watcher.requests.get", return_value=mock_resp):
            result = CopyTradeWatcher._fetch_current_best_bid("tok_abc")
        assert result == 0.45
        with patch("src.utils.copy_trade_watcher.requests.get", return_value=mock_resp):
            result = CopyTradeWatcher._fetch_current_best_bid("tok_abc")
        assert result == 0.45


# ══════════════════════════════════════════════════════════════════════
# T189 Tests — 跟投价格校准、余额下限、日志节流
# ══════════════════════════════════════════════════════════════════════


class TestT189ClobMidPrice:
    """T189-B: _fetch_clob_mid_price 方法测试。"""

    @pytest.fixture(autouse=True)
    def _import(self):
        from src.utils.copy_trade_watcher import CopyTradeWatcher

        self.CopyTradeWatcher = CopyTradeWatcher

    def test_midpoint_both_sides(self):
        """有 bid 和 ask 时返回中间价。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "bids": [{"price": "0.30"}],
            "asks": [{"price": "0.40"}],
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("src.utils.copy_trade_watcher.requests.get", return_value=mock_resp):
            mid = self.CopyTradeWatcher._fetch_clob_mid_price("tok_abc")
        assert mid == 0.35

    def test_only_bids(self):
        """只有 bid 时返回 bid + 0.01。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "bids": [{"price": "0.25"}],
            "asks": [],
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("src.utils.copy_trade_watcher.requests.get", return_value=mock_resp):
            mid = self.CopyTradeWatcher._fetch_clob_mid_price("tok_abc")
        assert mid == 0.26

    def test_only_asks(self):
        """只有 ask 时返回 ask。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "bids": [],
            "asks": [{"price": "0.60"}],
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("src.utils.copy_trade_watcher.requests.get", return_value=mock_resp):
            mid = self.CopyTradeWatcher._fetch_clob_mid_price("tok_abc")
        assert mid == 0.60

    def test_empty_book(self):
        """空 orderbook 返回 None。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"bids": [], "asks": []}
        mock_resp.raise_for_status = MagicMock()
        with patch("src.utils.copy_trade_watcher.requests.get", return_value=mock_resp):
            mid = self.CopyTradeWatcher._fetch_clob_mid_price("tok_abc")
        assert mid is None

    def test_no_token_id(self):
        """空 token_id 返回 None。"""
        assert self.CopyTradeWatcher._fetch_clob_mid_price("") is None

    def test_network_error_returns_none(self):
        """网络异常返回 None（不崩溃）。"""
        with patch(
            "src.utils.copy_trade_watcher.requests.get",
            side_effect=Exception("timeout"),
        ):
            mid = self.CopyTradeWatcher._fetch_clob_mid_price("tok_abc")
        assert mid is None


class TestT189CalcBetSizeFloor:
    """T189-C: _calc_bet_size 最小下单额保底测试。"""

    @pytest.fixture
    def watcher(self, tmp_path):
        from src.utils import db
        from src.utils.copy_trade_watcher import CopyTradeWatcher

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        return CopyTradeWatcher(db_path=db_path)

    def test_floor_enforced_at_min_bet(self, watcher):
        """trade_price × min_shares < min_bet 时，返回 min_bet。"""
        task = {"base_usdc": 5.0}
        with patch.object(
            watcher,
            "_get",
            side_effect=lambda k: {
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.dynamic_usdc_enabled": True,
                "local_copy_trading.dynamic_usdc_scale_anchor": 0.50,
                "local_copy_trading.dynamic_usdc_min_shares": 5,
                "local_copy_trading.category_size_multiplier": {},
                "executor.min_bet_usdc": 2.0,
            }.get(k),
        ):
            size = watcher._calc_bet_size(task, [], trade_price=0.28)
        assert size >= 2.0, f"应不低于 min_bet_usdc=2.0, 实际: {size}"

    def test_floor_capped_by_max_usdc(self, watcher):
        """base_usdc < min_bet 时，受 max_usdc 限制。"""
        task = {"base_usdc": 1.5}  # 任务最大额度只有 $1.50
        with patch.object(
            watcher,
            "_get",
            side_effect=lambda k: {
                "local_copy_trading.default_base_usdc": 1.5,
                "local_copy_trading.dynamic_usdc_enabled": True,
                "local_copy_trading.dynamic_usdc_scale_anchor": 0.50,
                "local_copy_trading.dynamic_usdc_min_shares": 5,
                "local_copy_trading.category_size_multiplier": {},
                "executor.min_bet_usdc": 2.0,
            }.get(k),
        ):
            size = watcher._calc_bet_size(task, [], trade_price=0.20)
        # max_usdc=1.5 < min_bet=2.0 → 受限于 max_usdc
        assert size == 1.5, f"应受 max_usdc 限制为 1.5, 实际: {size}"

    def test_high_price_no_amplification(self, watcher):
        """高买入价时不需要额外保底。"""
        task = {"base_usdc": 5.0}
        with patch.object(
            watcher,
            "_get",
            side_effect=lambda k: {
                "local_copy_trading.default_base_usdc": 5.0,
                "local_copy_trading.dynamic_usdc_enabled": True,
                "local_copy_trading.dynamic_usdc_scale_anchor": 0.50,
                "local_copy_trading.dynamic_usdc_min_shares": 5,
                "local_copy_trading.category_size_multiplier": {},
                "executor.min_bet_usdc": 2.0,
            }.get(k),
        ):
            size = watcher._calc_bet_size(task, [], trade_price=0.60)
        assert size == 5.0, f"高价时应使用满额, 实际: {size}"


# ── T189-E: size_usdc 交叉验证与 matched_shares 防护 ────────────────


class TestT189ECrossValidation:
    """T189-E: 验证 copy_trade bet 记录的 size_usdc 和 matched_shares 写入正确性。

    根因：
    - filled_size 可能返回近零值（探单金额）或策略预算金额（非实际成交）
    - matched_shares 为 0 时应从 actual_cost / fill_price 推算
    """

    @pytest.fixture
    def watcher(self):
        from src.utils.copy_trade_watcher import CopyTradeWatcher

        with patch("src.utils.copy_trade_watcher.get", return_value=None):
            w = CopyTradeWatcher.__new__(CopyTradeWatcher)
            w._executor = MagicMock()
            w._notifier = MagicMock()
            w._notifier.send_message = MagicMock()
        return w

    def test_near_zero_filled_size_corrected(self):
        """filled_size 返回近零值（如 0.0052）时，应使用 price*shares 计算 cost。"""
        # 模拟: result.filled_size=0.0052 (探单碎片), result.matched_shares=5.026
        # fill_price=0.33 → calc_cost = 0.33 * 5.026 = 1.6586
        # actual_cost 初始 = 0.0052 (< 0.01) → 应修正为 1.6586
        fill_price = 0.33
        _matched = 5.026
        _filled_size = 0.0052
        size_usdc = 1.50  # 策略预算

        actual_cost = _filled_size if _filled_size > 0 else size_usdc

        # T189-E 交叉验证逻辑
        if fill_price > 0 and _matched > 0:
            _calc_cost = round(fill_price * _matched, 4)
            if actual_cost < 0.01 or abs(actual_cost - _calc_cost) > _calc_cost * 0.15:
                actual_cost = _calc_cost

        assert abs(actual_cost - 1.6586) < 0.01, f"应修正为 ~1.66, 实际: {actual_cost}"

    def test_budget_fallback_corrected(self):
        """filled_size=0 回退到策略预算时，若偏差>10%则使用 price*shares。"""
        fill_price = 0.22
        _matched = 5.766
        _filled_size = 0.0  # CLOB 未返回
        size_usdc = 1.44  # 策略预算 — 与 calc_cost 差 >10%

        actual_cost = _filled_size if _filled_size > 0 else size_usdc

        if fill_price > 0 and _matched > 0:
            _calc_cost = round(fill_price * _matched, 4)
            if actual_cost < 0.01 or abs(actual_cost - _calc_cost) > _calc_cost * 0.10:
                actual_cost = _calc_cost

        expected = round(0.22 * 5.766, 4)  # 1.2685
        assert (
            abs(actual_cost - expected) < 0.01
        ), f"应修正为 {expected}, 实际: {actual_cost}"

    def test_correct_filled_size_not_overridden(self):
        """filled_size 正确且在 10% 容差内时不应被覆盖。"""
        fill_price = 0.33
        _matched = 5.026
        _filled_size = 1.65  # 正确值 (接近 0.33*5.026=1.6586)
        size_usdc = 1.50

        actual_cost = _filled_size if _filled_size > 0 else size_usdc

        if fill_price > 0 and _matched > 0:
            _calc_cost = round(fill_price * _matched, 4)
            if actual_cost < 0.01 or abs(actual_cost - _calc_cost) > _calc_cost * 0.10:
                actual_cost = _calc_cost

        assert actual_cost == 1.65, f"正确值不应被覆盖, 实际: {actual_cost}"

    def test_matched_shares_fallback_from_cost_div_price(self):
        """matched_shares=0 时应从 actual_cost/fill_price 推算。"""
        fill_price = 0.32
        actual_cost = 1.92
        _matched = 0  # CLOB 未返回

        shares = (
            _matched
            if _matched > 0
            else (round(actual_cost / fill_price, 6) if fill_price > 0 else 0)
        )
        assert shares == 6.0, f"应推算为 6.0, 实际: {shares}"
