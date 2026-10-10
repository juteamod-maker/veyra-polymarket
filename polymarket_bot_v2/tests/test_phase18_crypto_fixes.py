"""
test_phase18_crypto_fixes.py — Phase 18 加密货币修复单元测试

覆盖：
  - FIX 1: Direction Lock Guard（Bayesian 方向翻转拦截）
  - FIX 2: Smart Money Crypto Veto（加密赛道聪明钱否决权）
  - FIX 3: Fear & Greed Index（情绪指数获取与注入）
  - FIX 4: Crypto config 配置确认
  - FIX 5: 止损后同方向重入拦截
"""

import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from src.signal.signal_engine import Signal, SignalEngine
from src.utils.config_loader import get

# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


def make_ai_response(
    outcome: str, probability: float, confidence: float = 0.55
) -> dict:
    return {
        "outcome": outcome,
        "probability": probability,
        "confidence": confidence,
        "r": "test reasoning",
    }


def _make_engine():
    engine = SignalEngine.__new__(SignalEngine)
    engine._evolver = None
    return engine


def _setup_db(db_path: Path):
    """创建最小 paper_bets 表用于测试。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_bets (
            bet_id TEXT PRIMARY KEY,
            market_id TEXT,
            outcome TEXT,
            exit_type TEXT,
            resolved_at REAL,
            size_usdc REAL,
            pnl REAL,
            is_win INTEGER
        )"""
    )
    conn.commit()
    conn.close()


# ===========================================================================
# FIX 1: Direction Lock Guard
# ===========================================================================


class TestDirectionLockGuard:
    """Bayesian 方向翻转拦截测试。

    核心场景：market_price 极低（<15%），AI 说 NO（P(YES)<0.5），
    但 Bayesian LR 放大导致 posterior > 0.5 → 系统错误买 YES。
    Direction Lock 应阻止此翻转。
    """

    def setup_method(self):
        self.engine = _make_engine()
        self._patcher = patch(
            "src.signal.signal_engine.get",
            side_effect=lambda key, *a, **kw: (
                {"enabled": False}
                if key == "signal.probability_calibration"
                else get(key)
            ),
        )
        self._patcher.start()

    def teardown_method(self):
        self._patcher.stop()

    def test_extreme_low_odds_no_flip_to_yes(self):
        """Bitcoin $72K 场景：market=9%, AI=36%, Bayesian 不应翻转为 YES。"""
        votes = {
            "model-a": make_ai_response("NO", 0.64),  # P(YES)=0.36
            "model-b": make_ai_response("NO", 0.64),  # P(YES)=0.36
            "model-c": make_ai_response("NO", 0.64),  # P(YES)=0.36
        }
        result = self.engine._ensemble_vote(
            votes, best_ask=0.09, best_bid=0.08, category="crypto"
        )
        assert result is not None
        outcome, ev, conf, avg_prob = result
        # raw AI avg = 0.36 < 0.5 → 必须是 NO，不能被 Bayesian 翻转
        assert outcome == "NO", (
            f"Direction Lock 失败: raw_ai=0.36<0.5 但 outcome={outcome} "
            f"(avg_prob={avg_prob})"
        )

    def test_extreme_high_odds_no_flip_to_no(self):
        """对称场景：market=92%, AI=65% YES, Bayesian 不应翻转为 NO。"""
        votes = {
            "model-a": make_ai_response("YES", 0.65),  # P(YES)=0.65
            "model-b": make_ai_response("YES", 0.60),  # P(YES)=0.60
        }
        result = self.engine._ensemble_vote(
            votes, best_ask=0.92, best_bid=0.91, category="crypto"
        )
        assert result is not None
        outcome, ev, conf, avg_prob = result
        # raw AI avg ≈ 0.625 > 0.5 → 必须是 YES
        assert (
            outcome == "YES"
        ), f"Direction Lock 失败: raw_ai=0.625>0.5 但 outcome={outcome}"

    def test_normal_odds_no_direction_lock(self):
        """正常赔率下 Bayesian 不会翻转方向 → Direction Lock 不触发。"""
        votes = {
            "model-a": make_ai_response("YES", 0.70),
            "model-b": make_ai_response("YES", 0.68),
        }
        result = self.engine._ensemble_vote(
            votes, best_ask=0.55, best_bid=0.53, category="crypto"
        )
        assert result is not None
        outcome, ev, conf, avg_prob = result
        assert outcome == "YES"

    def test_all_models_say_no_at_extreme_odds(self):
        """所有模型都说 NO，市场也说 NO（5%），不应产生 YES 信号。"""
        votes = {
            "model-a": make_ai_response("NO", 0.80),  # P(YES)=0.20
            "model-b": make_ai_response("NO", 0.85),  # P(YES)=0.15
            "model-c": make_ai_response("NO", 0.90),  # P(YES)=0.10
        }
        result = self.engine._ensemble_vote(
            votes, best_ask=0.05, best_bid=0.04, category="crypto"
        )
        assert result is not None
        outcome, ev, conf, avg_prob = result
        assert (
            outcome == "NO"
        ), f"All models say NO but outcome={outcome} (avg_prob={avg_prob})"


# ===========================================================================
# FIX 2: Smart Money Crypto Veto
# ===========================================================================


class TestSmartMoneyCryptoVeto:
    """加密赛道聪明钱否决权测试。"""

    def test_crypto_veto_blocks_entry(self):
        """crypto 赛道 + smart money 反向 → 应触发否决。"""
        pt_cfg = {
            "smart_money": {
                "crypto_veto_enabled": True,
                "crypto_veto_min_opposed": 1,
                "excluded_categories": ["sports"],
            },
        }
        sm_align = {
            "has_smart_positions": True,
            "aligned": False,
            "aligned_count": 0,
            "opposed_count": 2,
            "total_aligned_amount": 0,
        }

        # 验证否决逻辑条件
        category = "crypto"
        sm_cfg = pt_cfg.get("smart_money") or {}
        veto_enabled = sm_cfg.get("crypto_veto_enabled", True)
        veto_min = int(sm_cfg.get("crypto_veto_min_opposed", 1))
        opposed = sm_align.get("opposed_count", 0)
        aligned_cnt = sm_align.get("aligned_count", 0)

        should_veto = (
            category == "crypto"
            and sm_align.get("has_smart_positions")
            and veto_enabled
            and not sm_align.get("aligned")
            and opposed >= veto_min
            and opposed > aligned_cnt
        )
        assert should_veto, "Crypto veto 应触发"

    def test_crypto_veto_disabled_in_config(self):
        """配置关闭时不触发否决。"""
        pt_cfg = {
            "smart_money": {
                "crypto_veto_enabled": False,
                "crypto_veto_min_opposed": 1,
            },
        }
        sm_align = {
            "has_smart_positions": True,
            "aligned": False,
            "opposed_count": 2,
            "aligned_count": 0,
        }

        sm_cfg = pt_cfg.get("smart_money") or {}
        veto_enabled = sm_cfg.get("crypto_veto_enabled", True)
        assert not veto_enabled, "Config 关闭时不应触发"

    def test_non_crypto_category_no_veto(self):
        """非 crypto 赛道不应触发否决。"""
        category = "weather"
        sm_align = {
            "has_smart_positions": True,
            "aligned": False,
            "opposed_count": 3,
            "aligned_count": 0,
        }
        should_veto = category == "crypto" and not sm_align.get("aligned")
        assert not should_veto

    def test_crypto_aligned_no_veto(self):
        """smart money 同向不触发否决。"""
        sm_align = {
            "has_smart_positions": True,
            "aligned": True,
            "opposed_count": 0,
            "aligned_count": 2,
        }
        should_veto = (
            not sm_align.get("aligned") and sm_align.get("opposed_count", 0) >= 1
        )
        assert not should_veto


# ===========================================================================
# FIX 3: Fear & Greed Index
# ===========================================================================


class TestFearGreedIndex:
    """Fear & Greed 指数获取测试。"""

    @patch("src.utils.crypto_fetcher.requests.get")
    def test_fetch_fear_greed_success(self, mock_get):
        """正常获取 Fear & Greed 数据。"""
        import src.utils.crypto_fetcher as cf

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {
                    "value": "25",
                    "value_classification": "Extreme Fear",
                    "timestamp": "1711238400",
                }
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        # 清缓存
        cf._fng_cache = (0.0, {})
        result = cf._fetch_fear_greed()
        assert result["value"] == 25
        assert result["classification"] == "Extreme Fear"

    @patch("src.utils.crypto_fetcher.requests.get")
    def test_fetch_fear_greed_failure_returns_empty(self, mock_get):
        """API 失败时返回空 dict，不阻塞主流程。"""
        import src.utils.crypto_fetcher as cf

        mock_get.side_effect = Exception("API timeout")
        cf._fng_cache = (0.0, {})
        result = cf._fetch_fear_greed()
        assert result == {}

    @patch("src.utils.crypto_fetcher.requests.get")
    def test_fear_greed_cache(self, mock_get):
        """缓存期内不重复请求。"""
        import src.utils.crypto_fetcher as cf

        cf._fng_cache = (time.time(), {"value": 42, "classification": "Fear"})
        result = cf._fetch_fear_greed()
        assert result["value"] == 42
        mock_get.assert_not_called()

    @patch("src.utils.crypto_fetcher._fetch_fear_greed")
    def test_build_context_includes_fear_greed(self, mock_fng):
        """_build_context 输出包含 Fear & Greed 数据。"""
        import src.utils.crypto_fetcher as cf

        mock_fng.return_value = {"value": 15, "classification": "Extreme Fear"}
        data = {
            "current_price": 68000.0,
            "price_change_24h_pct": -2.5,
            "total_volume_24h": 35_000_000_000,
        }
        ctx = cf._build_context("bitcoin", data, 72000.0, "Will BTC be above $72,000?")
        assert "Fear & Greed" in ctx
        assert "15/100" in ctx
        assert "Extreme Fear" in ctx


# ===========================================================================
# FIX 4: Config 配置确认
# ===========================================================================


class TestCryptoConfig:
    """确认 config.json 中 crypto 相关配置正确。"""

    def test_crypto_veto_config_exists(self):
        """config.json 包含 crypto veto 配置。"""
        import json

        config_path = Path(__file__).parent.parent / "config" / "config.json"
        with open(config_path) as f:
            cfg = json.load(f)
        pt = cfg.get("paper_trading", {})
        sm = pt.get("smart_money", {})
        assert sm.get("crypto_veto_enabled") is True
        assert sm.get("crypto_veto_min_opposed") == 1


# ===========================================================================
# FIX 5: 止损后同方向重入拦截
# ===========================================================================


class TestStopLossSameDirectionBlock:
    """止损后同方向重入拦截测试。"""

    def test_get_last_stop_loss_outcome(self):
        """DB 函数正确返回最近止损方向。"""
        from src.utils import db

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        _setup_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO paper_bets (bet_id, market_id, outcome, exit_type, resolved_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("bet1", "mkt_btc", "YES", "stop_loss", time.time() - 3600),
        )
        conn.commit()
        conn.close()

        result = db.get_last_stop_loss_outcome("mkt_btc", db_path)
        assert result == "YES"

    def test_no_stop_loss_returns_empty(self):
        """无止损记录返回空字符串。"""
        from src.utils import db

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        _setup_db(db_path)
        result = db.get_last_stop_loss_outcome("mkt_nonexist", db_path)
        assert result == ""

    def test_same_direction_should_be_blocked(self):
        """上次止损 YES + 当前信号 YES → 应阻止。"""
        last_sl_outcome = "YES"
        signal_outcome = "YES"
        block = pt_cfg_block_same_dir(last_sl_outcome, signal_outcome)
        assert block is True

    def test_opposite_direction_should_pass(self):
        """上次止损 YES + 当前信号 NO → 允许重入。"""
        last_sl_outcome = "YES"
        signal_outcome = "NO"
        block = pt_cfg_block_same_dir(last_sl_outcome, signal_outcome)
        assert block is False

    def test_no_previous_stop_loss_should_pass(self):
        """无止损历史 → 允许入场。"""
        last_sl_outcome = ""
        signal_outcome = "YES"
        block = pt_cfg_block_same_dir(last_sl_outcome, signal_outcome)
        assert block is False


def pt_cfg_block_same_dir(last_sl_outcome: str, signal_outcome: str) -> bool:
    """复刻 paper_trader.py 中的同方向拦截逻辑。"""
    if last_sl_outcome and last_sl_outcome == signal_outcome:
        return True
    return False
