"""
test_evolution_persistence.py — T152: 进化系统持久化测试

验证:
  - DB schema v9 迁移（evolution_overrides / evolution_overrides_history 表）
  - load / save / rollback / history CRUD 操作
  - StrategyEvolver 从 DB 恢复 overrides
  - evolve() 自动持久化所有调整
  - persist_overrides=false 时不做持久化
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from src.evolution.strategy_evolver import StrategyEvolver
from src.utils import db


@pytest.fixture
def tmp_db(tmp_path):
    """创建临时数据库并初始化 schema。"""
    db_file = tmp_path / "test_evo.db"
    db.init_db(db_file)
    return db_file


# ══════════════════════════════════════════════════════════════════════
# DB CRUD 测试
# ══════════════════════════════════════════════════════════════════════


class TestEvolutionOverridesCRUD:
    """测试 db.py 中 evolution_overrides 的 CRUD 操作。"""

    def test_load_empty(self, tmp_db):
        """空表应返回空 dict。"""
        result = db.load_evolution_overrides(db_path=tmp_db)
        assert result == {}

    def test_save_and_load(self, tmp_db):
        """保存后应能加载。"""
        db.save_evolution_override(
            param_name="shrinkage_factor",
            new_value=0.80,
            old_value=0.75,
            reason="校准偏差=OVERCONFIDENT",
            db_path=tmp_db,
        )
        result = db.load_evolution_overrides(db_path=tmp_db)
        assert "shrinkage_factor" in result
        assert abs(result["shrinkage_factor"] - 0.80) < 1e-6

    def test_save_upsert(self, tmp_db):
        """重复保存应覆盖旧值。"""
        db.save_evolution_override(param_name="min_ev", new_value=0.02, db_path=tmp_db)
        db.save_evolution_override(
            param_name="min_ev", new_value=0.03, old_value=0.02, db_path=tmp_db
        )
        result = db.load_evolution_overrides(db_path=tmp_db)
        assert abs(result["min_ev"] - 0.03) < 1e-6

    def test_save_records_history(self, tmp_db):
        """保存应同时记录历史。"""
        db.save_evolution_override(
            param_name="test_param",
            new_value=0.50,
            old_value=0.40,
            reason="test reason",
            db_path=tmp_db,
        )
        history = db.list_evolution_override_history(
            param_name="test_param", db_path=tmp_db
        )
        assert len(history) == 1
        assert history[0]["param_name"] == "test_param"
        assert abs(history[0]["new_value"] - 0.50) < 1e-6
        assert abs(history[0]["old_value"] - 0.40) < 1e-6
        assert history[0]["reason"] == "test reason"

    def test_multiple_params(self, tmp_db):
        """多个参数独立存储。"""
        db.save_evolution_override(param_name="param_a", new_value=1.0, db_path=tmp_db)
        db.save_evolution_override(param_name="param_b", new_value=2.0, db_path=tmp_db)
        result = db.load_evolution_overrides(db_path=tmp_db)
        assert len(result) == 2
        assert abs(result["param_a"] - 1.0) < 1e-6
        assert abs(result["param_b"] - 2.0) < 1e-6

    def test_history_all_params(self, tmp_db):
        """不带 param_name 查询所有历史。"""
        db.save_evolution_override(param_name="p1", new_value=0.1, db_path=tmp_db)
        db.save_evolution_override(param_name="p2", new_value=0.2, db_path=tmp_db)
        history = db.list_evolution_override_history(db_path=tmp_db)
        assert len(history) == 2

    def test_history_limit(self, tmp_db):
        """历史查询支持 limit。"""
        for i in range(10):
            db.save_evolution_override(
                param_name="x",
                new_value=float(i),
                old_value=float(i - 1),
                db_path=tmp_db,
            )
        history = db.list_evolution_override_history(
            param_name="x", limit=3, db_path=tmp_db
        )
        assert len(history) == 3
        # 最新的在前
        assert abs(history[0]["new_value"] - 9.0) < 1e-6


class TestRollback:
    """测试 rollback_evolution_override。"""

    def test_rollback_basic(self, tmp_db):
        """回滚应恢复到上一个值。"""
        db.save_evolution_override(
            param_name="shrink",
            new_value=0.80,
            old_value=0.75,
            reason="evolve",
            db_path=tmp_db,
        )
        rolled = db.rollback_evolution_override("shrink", db_path=tmp_db)
        assert rolled is not None
        assert abs(rolled - 0.75) < 1e-6
        # 当前值应更新
        current = db.load_evolution_overrides(db_path=tmp_db)
        assert abs(current["shrink"] - 0.75) < 1e-6

    def test_rollback_no_history(self, tmp_db):
        """无历史应返回 None。"""
        result = db.rollback_evolution_override("nonexistent", db_path=tmp_db)
        assert result is None

    def test_rollback_records_history(self, tmp_db):
        """回滚本身应记录到历史。"""
        db.save_evolution_override(
            param_name="test", new_value=0.50, old_value=0.40, db_path=tmp_db
        )
        db.rollback_evolution_override("test", db_path=tmp_db)
        history = db.list_evolution_override_history(param_name="test", db_path=tmp_db)
        assert len(history) == 2
        assert history[0]["reason"] == "ROLLBACK"

    def test_rollback_first_entry_none_old(self, tmp_db):
        """第一次保存 old_value=None 时回滚应返回 None。"""
        db.save_evolution_override(
            param_name="new_param",
            new_value=0.60,
            old_value=None,
            db_path=tmp_db,
        )
        result = db.rollback_evolution_override("new_param", db_path=tmp_db)
        assert result is None


# ══════════════════════════════════════════════════════════════════════
# StrategyEvolver 集成测试
# ══════════════════════════════════════════════════════════════════════


class TestEvolverPersistence:
    """测试 StrategyEvolver 与 DB 持久化的集成。"""

    def test_init_loads_from_db(self, tmp_db):
        """__init__ 应从 DB 恢复 overrides。"""
        # 先写入一些数据
        db.save_evolution_override(
            param_name="signal.min_ev_threshold",
            new_value=0.035,
            old_value=0.02,
            db_path=tmp_db,
        )
        evolver = StrategyEvolver(db_path=tmp_db)
        assert abs(evolver.get_override("signal.min_ev_threshold") - 0.035) < 1e-6

    def test_init_empty_db(self, tmp_db):
        """空 DB 时 overrides 应为空。"""
        evolver = StrategyEvolver(db_path=tmp_db)
        assert evolver.runtime_overrides == {}

    @patch("src.evolution.strategy_evolver.get")
    def test_persist_disabled(self, mock_get, tmp_db):
        """persist_overrides=false 时不应从 DB 加载。"""
        # 先写入数据
        db.save_evolution_override(
            param_name="test_param", new_value=0.99, db_path=tmp_db
        )

        def cfg_side_effect(key, *args, **kwargs):
            if key == "evolution":
                return {"enabled": True, "persist_overrides": False, "min_samples": 30}
            return None

        mock_get.side_effect = cfg_side_effect
        evolver = StrategyEvolver(db_path=tmp_db)
        # 不应加载
        assert "test_param" not in evolver.runtime_overrides

    def test_rollback_method(self, tmp_db):
        """StrategyEvolver.rollback_override 应更新内存和DB。"""
        db.save_evolution_override(
            param_name="shrinkage_factor",
            new_value=0.85,
            old_value=0.75,
            db_path=tmp_db,
        )
        evolver = StrategyEvolver(db_path=tmp_db)
        rolled = evolver.rollback_override("shrinkage_factor")
        assert rolled is not None
        assert abs(rolled - 0.75) < 1e-6
        assert abs(evolver.runtime_overrides["shrinkage_factor"] - 0.75) < 1e-6

    def test_get_override_history(self, tmp_db):
        """StrategyEvolver.get_override_history 应返回历史。"""
        db.save_evolution_override(
            param_name="ev_thresh", new_value=0.03, old_value=0.02, db_path=tmp_db
        )
        db.save_evolution_override(
            param_name="ev_thresh", new_value=0.04, old_value=0.03, db_path=tmp_db
        )
        evolver = StrategyEvolver(db_path=tmp_db)
        history = evolver.get_override_history("ev_thresh")
        assert len(history) == 2
        assert abs(history[0]["new_value"] - 0.04) < 1e-6

    @patch("src.evolution.strategy_evolver.get")
    def test_evolve_persists_adjustments(self, mock_get, tmp_db):
        """evolve() 中的调整应持久化到 DB。"""

        def cfg_side_effect(key, *args, **kwargs):
            defaults = {
                "evolution": {
                    "enabled": True,
                    "min_samples": 5,
                    "interval_bets": 10,
                    "interval_hours": 1,
                    "persist_overrides": True,
                },
                "signal.probability_calibration.shrinkage_factor": 0.75,
                "signal.min_ev_threshold": 0.02,
                "paper_trading.min_ev_threshold": 0.015,
                "signal.probability_calibration.probability_ceiling": 0.92,
                "signal.probability_calibration.probability_floor": 0.08,
                "scanner.categories_bonus": {},
            }
            return defaults.get(key)

        mock_get.side_effect = cfg_side_effect

        evolver = StrategyEvolver(db_path=tmp_db)

        # 手动设置校准曲线使其触发调整
        from src.evolution.strategy_evolver import CalibrationBin

        evolver.global_calibration.active = True
        evolver.global_calibration.total_samples = 100
        bins = []
        for i in range(10):
            b = CalibrationBin(bin_index=i, midpoint=(i + 0.5) / 10)
            # 模拟过度自信：实际胜率低于预期
            b.total = 20
            b.wins = int(b.midpoint * 20 * 0.8)  # 实际比预期少
            bins.append(b)
        evolver.global_calibration.bins = bins

        # Mock 计数函数
        with patch.object(evolver, "_count_resolved", return_value=100):
            summary = evolver.evolve()

        # 检查是否有调整被持久化
        saved = db.load_evolution_overrides(db_path=tmp_db)
        if summary["adjustments"]:
            assert len(saved) > 0
            history = db.list_evolution_override_history(db_path=tmp_db)
            assert len(history) > 0


# ══════════════════════════════════════════════════════════════════════
# Schema 迁移测试
# ══════════════════════════════════════════════════════════════════════


class TestSchemaV9Migration:
    """测试 schema v9 迁移创建了正确的表。"""

    def test_tables_exist(self, tmp_db):
        """v9 迁移后两个表应存在。"""
        import sqlite3

        conn = sqlite3.connect(str(tmp_db))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        table_names = {t[0] for t in tables}
        assert "evolution_overrides" in table_names
        assert "evolution_overrides_history" in table_names

    def test_schema_version_is_9(self, tmp_db):
        """迁移后 schema_version 应包含 9。"""
        import sqlite3

        conn = sqlite3.connect(str(tmp_db))
        versions = conn.execute("SELECT version FROM schema_version").fetchall()
        conn.close()
        version_set = {v[0] for v in versions}
        assert 9 in version_set

    def test_overrides_table_columns(self, tmp_db):
        """evolution_overrides 表应有正确的列。"""
        import sqlite3

        conn = sqlite3.connect(str(tmp_db))
        info = conn.execute("PRAGMA table_info(evolution_overrides)").fetchall()
        conn.close()
        col_names = {row[1] for row in info}
        assert col_names == {"param_name", "value", "updated_at", "reason"}

    def test_history_table_columns(self, tmp_db):
        """evolution_overrides_history 表应有正确的列。"""
        import sqlite3

        conn = sqlite3.connect(str(tmp_db))
        info = conn.execute("PRAGMA table_info(evolution_overrides_history)").fetchall()
        conn.close()
        col_names = {row[1] for row in info}
        assert col_names == {
            "id",
            "param_name",
            "old_value",
            "new_value",
            "reason",
            "created_at",
        }

    def test_idempotent_init(self, tmp_db):
        """重复调用 init_db 不应报错。"""
        db.init_db(tmp_db)
        db.init_db(tmp_db)
        result = db.load_evolution_overrides(db_path=tmp_db)
        assert isinstance(result, dict)
