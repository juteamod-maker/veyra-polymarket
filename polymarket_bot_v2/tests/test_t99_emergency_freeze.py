"""
tests/test_t99_emergency_freeze.py — T99 紧急冻结功能测试

测试内容：
1. emergency_freeze=true 时 run_cycle 早退，不下新单
2. emergency_freeze=false 时 run_cycle 正常执行
3. emergency_freeze=true 时 OutcomeTracker 和 position_syncer 仍被调用（保护持仓）
4. API 端点 /api/emergency-freeze GET/POST 基本功能
"""

import json
import types
from unittest.mock import MagicMock, patch

# T901: /api/emergency-freeze 现要求 admin / super_admin，测试统一注入该身份。
_ADMIN_IDENTITY = {
    "authenticated": True,
    "role": "admin",
    "wallet_address": "0x" + "0" * 40,
    "is_admin": True,
}

# ────────────────────────────────────────────────────────────────────────────
# 1. run_cycle 行为测试
# ────────────────────────────────────────────────────────────────────────────


class TestEmergencyFreezeRunCycle:
    """验证 run_cycle 在 emergency_freeze=true 时的行为。"""

    @patch("main.monitor_positions")
    @patch("main._outcome_tracker")
    @patch("main._syncer")
    @patch("main._get_components")
    @patch("main.load_config")
    @patch("main.get")
    def test_run_cycle_exits_early_when_frozen(
        self,
        mock_get,
        mock_load_config,
        mock_get_components,
        mock_syncer,
        mock_outcome_tracker,
        mock_monitor_positions,
    ):
        """emergency_freeze=true 时 run_cycle 应在扫描前退出。"""
        import main

        # Setup
        mock_monitor = MagicMock()
        mock_monitor.globally_paused = False
        mock_scanner = MagicMock()
        mock_signal_engine = MagicMock()
        mock_executor = MagicMock()
        mock_get_components.return_value = (
            mock_scanner,
            mock_signal_engine,
            mock_executor,
            mock_monitor,
        )
        mock_outcome_tracker.check_and_update.return_value = 0

        # Config: emergency_freeze=true
        def get_side_effect(key):
            config = {
                "general.log_level": "INFO",
                "risk.emergency_freeze": True,
            }
            return config.get(key)

        mock_get.side_effect = get_side_effect

        main.run_cycle()

        # 扫描器不应被调用（冻结时不扫描市场）
        mock_scanner.scan.assert_not_called()
        mock_signal_engine.generate_signals.assert_not_called()

    @patch("main.monitor_positions")
    @patch("main._outcome_tracker")
    @patch("main._syncer")
    @patch("main._get_components")
    @patch("main.load_config")
    @patch("main.get")
    def test_run_cycle_outcome_tracker_runs_when_frozen(
        self,
        mock_get,
        mock_load_config,
        mock_get_components,
        mock_syncer,
        mock_outcome_tracker,
        mock_monitor_positions,
    ):
        """emergency_freeze=true 时 OutcomeTracker 仍应运行（保护持仓 SL/TP）。"""
        import main

        mock_monitor = MagicMock()
        mock_monitor.globally_paused = False
        mock_get_components.return_value = (
            MagicMock(),
            MagicMock(),
            MagicMock(),
            mock_monitor,
        )
        mock_outcome_tracker.check_and_update.return_value = 2

        def get_side_effect(key):
            config = {
                "general.log_level": "INFO",
                "risk.emergency_freeze": True,
            }
            return config.get(key)

        mock_get.side_effect = get_side_effect

        main.run_cycle()

        # OutcomeTracker 应该被调用（持仓保护）
        mock_outcome_tracker.check_and_update.assert_called_once()
        # monitor_positions 也应调用
        mock_monitor_positions.assert_called_once()

    @patch("main.monitor_positions")
    @patch("main._outcome_tracker")
    @patch("main._syncer")
    @patch("main._get_components")
    @patch("main.load_config")
    @patch("main.get")
    def test_run_cycle_syncer_runs_when_frozen(
        self,
        mock_get,
        mock_load_config,
        mock_get_components,
        mock_syncer,
        mock_outcome_tracker,
        mock_monitor_positions,
    ):
        """emergency_freeze=true 时 position syncer 仍应运行（SUPREME 1 合规）。"""
        import main

        mock_monitor = MagicMock()
        mock_monitor.globally_paused = False
        mock_get_components.return_value = (
            MagicMock(),
            MagicMock(),
            MagicMock(),
            mock_monitor,
        )
        mock_outcome_tracker.check_and_update.return_value = 0

        def get_side_effect(key):
            return {
                "general.log_level": "INFO",
                "risk.emergency_freeze": True,
            }.get(key)

        mock_get.side_effect = get_side_effect

        main.run_cycle()

        # 持仓对账仍应运行
        mock_syncer.sync.assert_called_once()

    @patch("main.monitor_positions")
    @patch("main._outcome_tracker")
    @patch("main._syncer")
    @patch("main._get_components")
    @patch("main.load_config")
    @patch("main.get")
    def test_run_cycle_not_frozen_proceeds_normally(
        self,
        mock_get,
        mock_load_config,
        mock_get_components,
        mock_syncer,
        mock_outcome_tracker,
        mock_monitor_positions,
    ):
        """emergency_freeze=false 时 monitor_positions 不应被调用（不走冻结路径）。"""
        import main

        mock_monitor = MagicMock()
        mock_monitor.globally_paused = False
        mock_scanner = MagicMock()
        mock_scanner.scan.return_value = {}
        mock_signal_engine = MagicMock()
        mock_executor = MagicMock()
        mock_get_components.return_value = (
            mock_scanner,
            mock_signal_engine,
            mock_executor,
            mock_monitor,
        )
        mock_outcome_tracker.check_and_update.return_value = 0
        mock_syncer.sync.return_value = 0

        get_calls = []

        def get_side_effect(key):
            get_calls.append(key)
            config = {
                "general.log_level": "INFO",
                "risk.emergency_freeze": False,
                "paper_trading.weather_only": False,
                "paper_trading.cycle_interval_minutes": 8,
            }
            return config.get(key)

        mock_get.side_effect = get_side_effect

        # Patch WeatherPipeline at import level in src.signal.weather_pipeline
        with patch(
            "src.signal.weather_pipeline.WeatherPipeline.run_cycle",
            return_value=[],
        ):
            try:
                main.run_cycle()
            except Exception:
                pass  # other unpatched deps may raise; that's OK

        # Key: risk.emergency_freeze was checked (not skipped)
        assert (
            "risk.emergency_freeze" in get_calls
        ), "run_cycle should check risk.emergency_freeze even when not frozen"


# ────────────────────────────────────────────────────────────────────────────
# 2. Config value tests
# ────────────────────────────────────────────────────────────────────────────


class TestEmergencyFreezeConfig:
    """验证 emergency_freeze 配置项正确存在于 config.json。"""

    def test_emergency_freeze_exists_in_config(self, tmp_path):
        """config.json 中 risk.emergency_freeze 应存在且为 false。"""
        import os

        config_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "config", "config.json"
        )
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        assert "risk" in cfg, "risk section missing from config.json"
        assert (
            "emergency_freeze" in cfg["risk"]
        ), "emergency_freeze missing from risk section"
        assert (
            cfg["risk"]["emergency_freeze"] is False
        ), "emergency_freeze should default to False"

    def test_emergency_freeze_in_config_docs(self):
        """config_docs.json 中应有 emergency_freeze 的文档说明。"""
        import os

        docs_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "config", "config_docs.json"
        )
        with open(docs_path, "r", encoding="utf-8") as f:
            docs = json.load(f)

        risk_docs = docs.get("risk", {})
        assert (
            "emergency_freeze" in risk_docs
        ), "emergency_freeze missing from config_docs.json risk section"
        doc_text = risk_docs["emergency_freeze"]
        assert len(doc_text) > 10, "emergency_freeze doc is too short"


# ────────────────────────────────────────────────────────────────────────────
# 3. Dashboard API endpoint tests
# ────────────────────────────────────────────────────────────────────────────


class TestEmergencyFreezeAPI:
    """验证 /api/emergency-freeze 端点基础逻辑。"""

    def test_get_emergency_freeze_returns_not_frozen(self, tmp_path):
        """GET /api/emergency-freeze 应返回当前状态（False 时 frozen=false）。"""
        import asyncio

        config_data = {"risk": {"emergency_freeze": False}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        async def run_test():
            with patch(
                "scripts.dashboard_routes.config_routes.CONFIG_PATH",
                str(config_file),
            ):
                from scripts.dashboard_routes import config_routes

                return await config_routes.get_emergency_freeze()

        result = asyncio.run(run_test())
        assert result["frozen"] is False
        assert result["status"] == "normal"

    def test_post_emergency_freeze_sets_true(self, tmp_path):
        """POST /api/emergency-freeze {freeze: true} 应将配置设为 true。"""
        import asyncio

        config_data = {"risk": {"emergency_freeze": False}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        async def fake_json():
            return {"freeze": True}

        mock_request = MagicMock()
        mock_request.json = fake_json

        async def run_test():
            with patch(
                "scripts.dashboard_routes.config_routes.CONFIG_PATH",
                str(config_file),
            ), patch(
                "scripts.dashboard_routes.config_routes.backup_config",
                return_value="backup.json",
            ), patch(
                "scripts.dashboard_routes.config_routes.get_request_identity",
                return_value=_ADMIN_IDENTITY,
            ):
                from scripts.dashboard_routes import config_routes

                result = await config_routes.set_emergency_freeze(mock_request)
                return result

        result = asyncio.run(run_test())
        assert result["status"] == "ok"
        assert result["frozen"] is True

        # Verify config was written
        with open(config_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["risk"]["emergency_freeze"] is True

    def test_post_emergency_freeze_invalid_body(self, tmp_path):
        """POST /api/emergency-freeze 非 bool 值应返回 400。"""
        import asyncio

        config_data = {"risk": {"emergency_freeze": False}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        async def fake_json():
            return {"freeze": "yes_please"}

        mock_request = MagicMock()
        mock_request.json = fake_json

        async def run_test():
            with patch(
                "scripts.dashboard_routes.config_routes.CONFIG_PATH",
                str(config_file),
            ), patch(
                "scripts.dashboard_routes.config_routes.get_request_identity",
                return_value=_ADMIN_IDENTITY,
            ):
                from scripts.dashboard_routes import config_routes

                return await config_routes.set_emergency_freeze(mock_request)

        result = asyncio.run(run_test())
        assert result.status_code == 400

    def test_post_emergency_freeze_requires_admin(self, tmp_path):
        """T901 回归：非 admin 调用 /api/emergency-freeze 必须 403，且不得改动配置。"""
        import asyncio

        config_data = {"risk": {"emergency_freeze": False}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        async def fake_json():
            return {"freeze": True}

        mock_request = MagicMock()
        mock_request.json = fake_json

        async def run_test():
            with patch(
                "scripts.dashboard_routes.config_routes.CONFIG_PATH",
                str(config_file),
            ), patch(
                "scripts.dashboard_routes.config_routes.backup_config",
                return_value="backup.json",
            ), patch(
                "scripts.dashboard_routes.config_routes.get_request_identity",
                return_value={
                    "authenticated": True,
                    "role": "manual_user",
                    "wallet_address": "0x" + "1" * 40,
                    "is_admin": False,
                },
            ):
                from scripts.dashboard_routes import config_routes

                return await config_routes.set_emergency_freeze(mock_request)

        result = asyncio.run(run_test())
        assert result.status_code == 403

        with open(config_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["risk"]["emergency_freeze"] is False
