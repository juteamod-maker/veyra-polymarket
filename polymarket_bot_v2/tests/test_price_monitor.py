"""
test_price_monitor.py — PriceMonitor 单元测试
T169: 分级涨跌记录、可视化与阈值告警
"""

import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def _init_db(tmp_path):
    """创建临时 DB 并初始化 price_monitor 所需表。"""
    db_path = tmp_path / "test.db"

    def _connect():
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # 创建表
    conn = _connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bet_price_series (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bet_id      TEXT    NOT NULL,
            ts          TEXT    NOT NULL,
            price_mid   REAL    NOT NULL,
            price_bid   REAL,
            price_ask   REAL
        );
        CREATE INDEX IF NOT EXISTS idx_bps_bet_ts ON bet_price_series(bet_id, ts);
        CREATE TABLE IF NOT EXISTS price_move_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bet_id          TEXT    NOT NULL,
            threshold_pct   REAL    NOT NULL,
            direction       TEXT    NOT NULL,
            price_at_cross  REAL    NOT NULL,
            entry_price     REAL    NOT NULL,
            return_pct      REAL    NOT NULL,
            ts              TEXT    NOT NULL,
            action          TEXT    NOT NULL DEFAULT 'record',
            category        TEXT    DEFAULT '',
            question        TEXT    DEFAULT '',
            UNIQUE(bet_id, threshold_pct, direction)
        );
        CREATE INDEX IF NOT EXISTS idx_pml_ts ON price_move_log(ts);
        CREATE INDEX IF NOT EXISTS idx_pml_bet ON price_move_log(bet_id);
        CREATE TABLE IF NOT EXISTS paper_bets (
            bet_id TEXT PRIMARY KEY,
            market_id TEXT,
            market_price REAL,
            question TEXT,
            category TEXT,
            outcome TEXT DEFAULT 'YES',
            status TEXT DEFAULT 'open'
        );
    """
    )
    conn.close()

    # Patch connect to use our tmp db
    import contextlib

    @contextlib.contextmanager
    def mock_connect():
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    with patch("src.monitor.price_monitor.connect", mock_connect):
        yield mock_connect


@pytest.fixture
def pm_config():
    """返回 PriceMonitor 用到的配置。"""
    return {
        "enabled": True,
        "sample_interval_seconds": 10,
        "db_flush_interval_seconds": 60,
        "memory_buffer_size": 100,
        "retention_days": 7,
        "cleanup_interval_hours": 6,
        "thresholds_down_pct": [5, 10, 20, 50],
        "thresholds_up_pct": [10, 20],
        "telegram_alert_on_down": True,
        "telegram_alert_on_up": False,
        "alert_cooldown_seconds": 300,
        "auto_sell_enabled": False,
        "auto_sell_threshold_pct": 50,
    }


@pytest.fixture
def pm(_init_db, pm_config):
    """创建一个 PriceMonitor 实例（DB 已 mock）。"""
    with patch("src.monitor.price_monitor.get", return_value=pm_config):
        from src.monitor.price_monitor import PriceMonitor

        monitor = PriceMonitor()
    return monitor


# ── 基础功能测试 ──────────────────────────────────────────────────────


class TestPriceMonitorInit:
    def test_enabled(self, pm):
        assert pm._enabled is True

    def test_thresholds_loaded(self, pm):
        assert pm._thresholds_down == [5, 10, 20, 50]
        assert pm._thresholds_up == [10, 20]

    def test_disabled_when_config_false(self, _init_db):
        with patch(
            "src.monitor.price_monitor.get",
            return_value={"enabled": False},
        ):
            from src.monitor.price_monitor import PriceMonitor

            pm = PriceMonitor()
            assert pm._enabled is False


# ── record_prices 测试 ────────────────────────────────────────────────


class TestRecordPrices:
    def _make_bets(self, entry_price=0.50):
        return [
            {
                "bet_id": "bet_001",
                "market_id": "mkt_001",
                "outcome": "YES",
                "market_price": entry_price,
                "category": "weather",
                "question": "Will it rain?",
            }
        ]

    def _make_market_data(self, best_bid=0.45, best_ask=0.55):
        return {
            "mkt_001": {
                "bestBid": str(best_bid),
                "bestAsk": str(best_ask),
            }
        }

    def test_records_to_memory_buffer(self, pm):
        """价格应记录到内存缓冲区。"""
        bets = self._make_bets(0.50)
        mdata = self._make_market_data(0.48, 0.52)
        pm.record_prices(bets, mdata)

        assert "bet_001" in pm._buffers
        assert len(pm._buffers["bet_001"]) == 1
        _, mid, bid, ask = pm._buffers["bet_001"][0]
        assert abs(mid - 0.50) < 0.001
        assert abs(bid - 0.48) < 0.001
        assert abs(ask - 0.52) < 0.001

    def test_no_event_when_price_stable(self, pm):
        """价格在阈值范围内不应触发事件。"""
        bets = self._make_bets(0.50)
        mdata = self._make_market_data(0.48, 0.52)  # mid = 0.50, 0% change
        events = pm.record_prices(bets, mdata)
        assert events == []

    def test_down_threshold_5pct(self, pm):
        """价格下跌 >= 5% 应触发下跌事件。"""
        bets = self._make_bets(0.50)
        # mid = (0.45 + 0.50) / 2 = 0.475, drop = 5%
        mdata = self._make_market_data(0.45, 0.50)
        events = pm.record_prices(bets, mdata)
        assert len(events) == 1
        assert events[0]["direction"] == "down"
        assert events[0]["threshold_pct"] == 5

    def test_down_threshold_multiple_levels(self, pm):
        """大幅下跌应同时触发多个阈值。"""
        bets = self._make_bets(0.50)
        # mid = (0.30 + 0.35) / 2 = 0.325, drop = 35%
        mdata = self._make_market_data(0.30, 0.35)
        events = pm.record_prices(bets, mdata)
        # 应触发 5%, 10%, 20% (不含 50%)
        thresholds = {e["threshold_pct"] for e in events}
        assert 5 in thresholds
        assert 10 in thresholds
        assert 20 in thresholds
        assert 50 not in thresholds  # 35% < 50%

    def test_up_threshold_10pct(self, pm):
        """价格上涨 >= 10% 应触发上涨事件。"""
        bets = self._make_bets(0.50)
        # mid = (0.54 + 0.56) / 2 = 0.55, gain = 10%
        mdata = self._make_market_data(0.54, 0.56)
        events = pm.record_prices(bets, mdata)
        assert len(events) == 1
        assert events[0]["direction"] == "up"
        assert events[0]["threshold_pct"] == 10

    def test_idempotent_no_duplicate_events(self, pm):
        """同一阈值只应触发一次。"""
        bets = self._make_bets(0.50)
        mdata = self._make_market_data(0.45, 0.50)  # -5%
        events1 = pm.record_prices(bets, mdata)
        events2 = pm.record_prices(bets, mdata)
        assert len(events1) == 1
        assert len(events2) == 0  # 第二次不触发

    def test_different_thresholds_triggered_separately(self, pm):
        """先触发 5%，价格继续跌再触发 10%。"""
        bets = self._make_bets(0.50)
        # First: -5%
        mdata1 = self._make_market_data(0.45, 0.50)
        events1 = pm.record_prices(bets, mdata1)
        assert len(events1) == 1
        assert events1[0]["threshold_pct"] == 5

        # Then: -12%
        mdata2 = self._make_market_data(0.42, 0.46)  # mid = 0.44
        events2 = pm.record_prices(bets, mdata2)
        assert len(events2) == 1
        assert events2[0]["threshold_pct"] == 10

    def test_disabled_returns_empty(self, pm):
        """禁用状态不记录任何数据。"""
        pm._enabled = False
        bets = self._make_bets(0.50)
        mdata = self._make_market_data(0.30, 0.35)
        events = pm.record_prices(bets, mdata)
        assert events == []

    def test_skips_invalid_entry_price(self, pm):
        """入场价为 0 的仓位应跳过。"""
        bets = self._make_bets(0)
        mdata = self._make_market_data(0.45, 0.55)
        events = pm.record_prices(bets, mdata)
        assert events == []

    def test_skips_missing_market_data(self, pm):
        """没有市场数据的仓位应跳过。"""
        bets = self._make_bets(0.50)
        mdata = {}  # empty
        events = pm.record_prices(bets, mdata)
        assert events == []

    def test_no_direction_correct_price(self, pm):
        """NO 方向仓位应使用 NO 价格进行回报率计算。"""
        bets = [
            {
                "bet_id": "bet_no",
                "market_id": "mkt_no",
                "outcome": "NO",
                "market_price": 0.82,  # NO entry = 1 - 0.18
                "category": "weather",
                "question": "Munich 7°C",
            }
        ]
        # YES bestBid=0.17, bestAsk=0.19 → NO mid ≈ 0.82
        mdata = {"mkt_no": {"bestBid": "0.17", "bestAsk": "0.19"}}
        events = pm.record_prices(bets, mdata)
        # NO 仓位 entry=0.82, current NO mid≈0.82 → ~0% → 无告警
        assert events == []

        # 验证缓冲区中的价格是 NO 方向的
        buf = pm._buffers.get("bet_no")
        assert buf is not None
        _, mid, bid, ask = buf[0]
        assert mid > 0.75  # NO mid ≈ 0.82, 不是 YES mid 0.18

    def test_no_direction_drop_alert(self, pm):
        """NO 方向仓位价格真正下跌时应正确触发告警。"""
        bets = [
            {
                "bet_id": "bet_nodrop",
                "market_id": "mkt_nodrop",
                "outcome": "NO",
                "market_price": 0.80,  # NO entry
                "category": "weather",
                "question": "Test NO drop",
            }
        ]
        # YES 涨到 0.30-0.32 → NO 跌到 0.68-0.70
        # NO mid ≈ 0.69, return = (0.69-0.80)/0.80 = -13.75%
        mdata = {"mkt_nodrop": {"bestBid": "0.30", "bestAsk": "0.32"}}
        events = pm.record_prices(bets, mdata)
        thresholds = {e["threshold_pct"] for e in events}
        assert 5 in thresholds
        assert 10 in thresholds


# ── flush 到 DB 测试 ──────────────────────────────────────────────────


class TestFlushToDb:
    def test_flushes_after_interval(self, pm, _init_db):
        """超过 flush 间隔后应写入 DB。"""
        pm._last_flush_ts = 0  # 强制触发 flush
        bets = [
            {
                "bet_id": "bet_flush",
                "market_id": "mkt_flush",
                "outcome": "YES",
                "market_price": 0.50,
                "category": "weather",
                "question": "Test",
            }
        ]
        mdata = {"mkt_flush": {"bestBid": "0.48", "bestAsk": "0.52"}}
        pm.record_prices(bets, mdata)

        # 检查 DB 内容
        with _init_db() as conn:
            rows = conn.execute(
                "SELECT * FROM bet_price_series WHERE bet_id = 'bet_flush'"
            ).fetchall()
        assert len(rows) >= 1

    def test_no_flush_before_interval(self, pm, _init_db):
        """未到 flush 间隔不应写入 DB。"""
        pm._last_flush_ts = time.time()  # 刚 flush 过
        bets = [
            {
                "bet_id": "bet_noflush",
                "market_id": "mkt_noflush",
                "outcome": "YES",
                "market_price": 0.50,
                "category": "weather",
                "question": "Test",
            }
        ]
        mdata = {"mkt_noflush": {"bestBid": "0.48", "bestAsk": "0.52"}}
        pm.record_prices(bets, mdata)

        with _init_db() as conn:
            rows = conn.execute(
                "SELECT * FROM bet_price_series WHERE bet_id = 'bet_noflush'"
            ).fetchall()
        assert len(rows) == 0

    def test_flush_no_direction_stores_yes_prices(self, pm, _init_db):
        """NO 方向仓位 flush 到 DB 时应存储 YES token 价格（前端负责转换）。"""
        pm._last_flush_ts = 0  # 强制触发 flush
        bets = [
            {
                "bet_id": "bet_no_flush",
                "market_id": "mkt_no_flush",
                "outcome": "NO",
                "market_price": 0.64,  # NO entry price
                "category": "weather",
                "question": "Test NO flush",
            }
        ]
        # YES bestBid=0.36, bestAsk=0.38 → NO mid ≈ 0.63
        mdata = {"mkt_no_flush": {"bestBid": "0.36", "bestAsk": "0.38"}}
        pm.record_prices(bets, mdata)

        # 缓冲区应有 NO 方向调整后的价格
        buf = pm._buffers.get("bet_no_flush")
        assert buf is not None
        _, buf_mid, buf_bid, buf_ask = buf[0]
        assert buf_mid > 0.6  # NO mid ≈ 0.63

        # DB 应存储 YES token 价格（<0.4）
        with _init_db() as conn:
            rows = conn.execute(
                "SELECT price_mid, price_bid, price_ask "
                "FROM bet_price_series WHERE bet_id = 'bet_no_flush'"
            ).fetchall()
        assert len(rows) >= 1
        db_mid, db_bid, db_ask = rows[0]
        # DB 中 YES mid ≈ 0.37（= 1 - 0.63），不是 NO mid 0.63
        assert db_mid < 0.4, f"DB should store YES price (~0.37), got {db_mid}"
        assert db_mid == pytest.approx(1.0 - buf_mid, abs=0.001)

    def test_flush_yes_direction_stores_as_is(self, pm, _init_db):
        """YES 方向仓位 flush 到 DB 时应直接存储（不转换）。"""
        pm._last_flush_ts = 0
        bets = [
            {
                "bet_id": "bet_yes_flush",
                "market_id": "mkt_yes_flush",
                "outcome": "YES",
                "market_price": 0.60,
                "category": "sports",
                "question": "Test YES flush",
            }
        ]
        mdata = {"mkt_yes_flush": {"bestBid": "0.58", "bestAsk": "0.62"}}
        pm.record_prices(bets, mdata)

        buf = pm._buffers.get("bet_yes_flush")
        assert buf is not None
        _, buf_mid, _, _ = buf[0]

        with _init_db() as conn:
            rows = conn.execute(
                "SELECT price_mid FROM bet_price_series "
                "WHERE bet_id = 'bet_yes_flush'"
            ).fetchall()
        assert len(rows) >= 1
        assert rows[0][0] == pytest.approx(buf_mid, abs=0.001)


# ── DB 事件持久化测试 — NO 方向 price_at_cross ──────────────────────


class TestEventNoDirection:
    def test_event_stores_yes_price_at_cross(self, pm, _init_db):
        """NO 方向穿越事件的 price_at_cross 应存储 YES token 价格。"""
        bets = [
            {
                "bet_id": "bet_no_evt",
                "market_id": "mkt_no_evt",
                "outcome": "NO",
                "market_price": 0.80,  # NO entry
                "category": "weather",
                "question": "Test NO event",
            }
        ]
        # NO entry=0.80, YES 涨到 0.30-0.32 → NO mid≈0.69
        # return = (0.69-0.80)/0.80 = -13.75% → 触发 5% 和 10%
        mdata = {"mkt_no_evt": {"bestBid": "0.30", "bestAsk": "0.32"}}
        pm.record_prices(bets, mdata)

        with _init_db() as conn:
            rows = conn.execute(
                "SELECT price_at_cross FROM price_move_log "
                "WHERE bet_id = 'bet_no_evt' ORDER BY threshold_pct"
            ).fetchall()
        assert len(rows) >= 1
        # DB 中 price_at_cross 应为 YES 价格 ≈ 0.31（= 1 - 0.69）
        for row in rows:
            assert (
                row[0] < 0.4
            ), f"price_at_cross should be YES token price (~0.31), got {row[0]}"


# ── DB 事件持久化测试 ─────────────────────────────────────────────────


class TestEventPersistence:
    def test_event_persisted_to_db(self, pm, _init_db):
        """穿越事件应写入 price_move_log。"""
        bets = [
            {
                "bet_id": "bet_persist",
                "market_id": "mkt_persist",
                "outcome": "YES",
                "market_price": 0.50,
                "category": "sports",
                "question": "Will team win?",
            }
        ]
        mdata = {"mkt_persist": {"bestBid": "0.30", "bestAsk": "0.35"}}
        pm.record_prices(bets, mdata)

        with _init_db() as conn:
            rows = conn.execute(
                "SELECT bet_id, threshold_pct, direction FROM price_move_log "
                "WHERE bet_id = 'bet_persist' ORDER BY threshold_pct"
            ).fetchall()
        # mid = 0.325, drop = 35%, should trigger 5, 10, 20
        thresholds = [r[1] for r in rows]
        assert 5.0 in thresholds
        assert 10.0 in thresholds
        assert 20.0 in thresholds

    def test_unique_constraint_prevents_duplicates(self, pm, _init_db):
        """UNIQUE 约束防止重复写入。"""
        bets = [
            {
                "bet_id": "bet_dup",
                "market_id": "mkt_dup",
                "outcome": "YES",
                "market_price": 0.50,
                "category": "weather",
                "question": "Test",
            }
        ]
        mdata = {"mkt_dup": {"bestBid": "0.45", "bestAsk": "0.50"}}

        # Reset crossed to simulate restart
        pm.record_prices(bets, mdata)
        pm._crossed["bet_dup"].discard((5.0, "down"))

        # 再次尝试（DB 中已存在）
        pm.record_prices(bets, mdata)

        with _init_db() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM price_move_log "
                "WHERE bet_id = 'bet_dup' AND threshold_pct = 5"
            ).fetchone()
        assert rows[0] == 1


# ── cleanup 测试 ──────────────────────────────────────────────────────


class TestCleanup:
    def test_cleanup_closed_bets(self, pm):
        """关仓后的 bet 应从内存中清理。"""
        pm._buffers["bet_open"].append((time.time(), 0.5, 0.48, 0.52))
        pm._buffers["bet_closed"].append((time.time(), 0.3, 0.28, 0.32))
        pm._crossed["bet_closed"].add((5.0, "down"))

        pm.cleanup_closed_bets({"bet_open"})

        assert "bet_open" in pm._buffers
        assert "bet_closed" not in pm._buffers
        assert "bet_closed" not in pm._crossed

    def test_cleanup_old_data(self, pm, _init_db):
        """过期数据应被删除。"""
        old_ts = "2020-01-01T00:00:00+00:00"
        with _init_db() as conn:
            conn.execute(
                "INSERT INTO bet_price_series (bet_id, ts, price_mid) "
                "VALUES ('old_bet', ?, 0.5)",
                (old_ts,),
            )
        deleted = pm.cleanup_old_data()
        assert deleted >= 1


# ── 查询接口测试 ──────────────────────────────────────────────────────


class TestQueryInterfaces:
    def test_get_price_series(self, pm, _init_db):
        """查询价格时序应返回正确格式。"""
        now_iso = datetime.now(timezone.utc).isoformat()
        with _init_db() as conn:
            conn.execute(
                "INSERT INTO bet_price_series "
                "(bet_id, ts, price_mid, price_bid, price_ask) "
                "VALUES ('q_bet', ?, 0.55, 0.54, 0.56)",
                (now_iso,),
            )
        result = pm.get_price_series("q_bet", hours=1)
        assert len(result) == 1
        assert result[0]["mid"] == 0.55

    def test_get_events(self, pm, _init_db):
        """查询事件应返回正确格式。"""
        now_iso = datetime.now(timezone.utc).isoformat()
        with _init_db() as conn:
            conn.execute(
                "INSERT INTO price_move_log "
                "(bet_id, threshold_pct, direction, price_at_cross, "
                "entry_price, return_pct, ts, action, category, question) "
                "VALUES ('e_bet', 10, 'down', 0.45, 0.50, -10.0, ?, "
                "'record', 'sports', 'Test?')",
                (now_iso,),
            )
        result = pm.get_events("e_bet")
        assert len(result) == 1
        assert result[0]["threshold_pct"] == 10
        assert result[0]["direction"] == "down"

    def test_get_realtime_buffer(self, pm):
        """查询内存缓冲区应返回正确格式。"""
        pm._buffers["buf_bet"].append((time.time(), 0.55, 0.54, 0.56))
        result = pm.get_realtime_buffer("buf_bet")
        assert len(result) == 1
        assert result[0]["mid"] == 0.55

    def test_get_summary(self, pm):
        """摘要统计应返回正确字段。"""
        pm._buffers["s_bet"].append((time.time(), 0.5, 0.48, 0.52))
        summary = pm.get_summary()
        assert summary["enabled"] is True
        assert summary["active_bets_monitored"] == 1
        assert summary["memory_samples"] == 1


# ── extract_mid_price 测试 ────────────────────────────────────────────


class TestExtractMidPrice:
    def test_from_best_bid_ask(self):
        from src.monitor.price_monitor import PriceMonitor

        mdata = {"bestBid": "0.45", "bestAsk": "0.55"}
        result = PriceMonitor._extract_mid_price(mdata, "YES")
        assert abs(result - 0.50) < 0.001

    def test_from_best_bid_ask_no_direction(self):
        """NO 方向应返回 1 - YES_mid。"""
        from src.monitor.price_monitor import PriceMonitor

        mdata = {"bestBid": "0.17", "bestAsk": "0.19"}
        # YES mid = 0.18, NO mid = 1 - 0.18 = 0.82
        result = PriceMonitor._extract_mid_price(mdata, "NO")
        assert abs(result - 0.82) < 0.001

    def test_from_outcome_prices(self):
        import json

        from src.monitor.price_monitor import PriceMonitor

        mdata = {"outcomePrices": json.dumps([0.60, 0.40])}
        result = PriceMonitor._extract_mid_price(mdata, "YES")
        assert abs(result - 0.60) < 0.001

    def test_no_outcome(self):
        import json

        from src.monitor.price_monitor import PriceMonitor

        mdata = {"outcomePrices": json.dumps([0.60, 0.40])}
        result = PriceMonitor._extract_mid_price(mdata, "NO")
        assert abs(result - 0.40) < 0.001

    def test_empty_data_returns_none(self):
        from src.monitor.price_monitor import PriceMonitor

        result = PriceMonitor._extract_mid_price({}, "YES")
        assert result is None

    def test_fallback_bestbid_only_no_direction(self):
        """方式3回退：只有 bestBid 时，NO 方向应返回 1 - bestBid。"""
        from src.monitor.price_monitor import PriceMonitor

        mdata = {"bestBid": "0.20"}
        result = PriceMonitor._extract_mid_price(mdata, "NO")
        assert abs(result - 0.80) < 0.001

    def test_fallback_bestask_only_no_direction(self):
        """方式3回退：只有 bestAsk 时，NO 方向应返回 1 - bestAsk。"""
        from src.monitor.price_monitor import PriceMonitor

        mdata = {"bestAsk": "0.19"}
        result = PriceMonitor._extract_mid_price(mdata, "NO")
        assert abs(result - 0.81) < 0.001


# ── _extract_price 测试（NO 方向转换）────────────────────────────────


class TestExtractPrice:
    def test_yes_bid(self):
        from src.monitor.price_monitor import PriceMonitor

        mdata = {"bestBid": "0.45", "bestAsk": "0.55"}
        assert abs(PriceMonitor._extract_price(mdata, "YES", "bid") - 0.45) < 0.001

    def test_yes_ask(self):
        from src.monitor.price_monitor import PriceMonitor

        mdata = {"bestBid": "0.45", "bestAsk": "0.55"}
        assert abs(PriceMonitor._extract_price(mdata, "YES", "ask") - 0.55) < 0.001

    def test_no_bid_is_1_minus_yes_ask(self):
        """NO bid（卖NO价）= 1 - YES ask。"""
        from src.monitor.price_monitor import PriceMonitor

        mdata = {"bestBid": "0.17", "bestAsk": "0.19"}
        result = PriceMonitor._extract_price(mdata, "NO", "bid")
        assert abs(result - 0.81) < 0.001  # 1 - 0.19

    def test_no_ask_is_1_minus_yes_bid(self):
        """NO ask（买NO价）= 1 - YES bid。"""
        from src.monitor.price_monitor import PriceMonitor

        mdata = {"bestBid": "0.17", "bestAsk": "0.19"}
        result = PriceMonitor._extract_price(mdata, "NO", "ask")
        assert abs(result - 0.83) < 0.001  # 1 - 0.17

    def test_tokens_fallback_yes(self):
        from src.monitor.price_monitor import PriceMonitor

        mdata = {
            "tokens": [
                {"outcome": "Yes", "price": "0.60"},
                {"outcome": "No", "price": "0.40"},
            ]
        }
        assert abs(PriceMonitor._extract_price(mdata, "YES", "bid") - 0.60) < 0.001

    def test_tokens_fallback_no(self):
        from src.monitor.price_monitor import PriceMonitor

        mdata = {
            "tokens": [
                {"outcome": "Yes", "price": "0.60"},
                {"outcome": "No", "price": "0.40"},
            ]
        }
        assert abs(PriceMonitor._extract_price(mdata, "NO", "bid") - 0.40) < 0.001

    def test_munich_7c_scenario(self):
        """重现 Munich 7°C 告警 BUG：NO入场0.82, YES当前≈0.19。"""
        from src.monitor.price_monitor import PriceMonitor

        mdata = {"bestBid": "0.17", "bestAsk": "0.19"}
        # 修复前：mid = (0.17+0.19)/2 = 0.18 → return -78% 错误
        # 修复后：NO mid = 1 - 0.18 = 0.82 → return ~0% 正确
        mid = PriceMonitor._extract_mid_price(mdata, "NO")
        assert mid is not None
        assert abs(mid - 0.82) < 0.01  # NO 价格应 ~0.82, 不是 0.18

        # 回报率校验
        entry_price = 0.82
        return_pct = ((mid - entry_price) / entry_price) * 100
        assert abs(return_pct) < 5  # 应接近 0%，而非 -76.8%


# ── Telegram 告警测试 ─────────────────────────────────────────────────


class TestAlerts:
    @patch("src.utils.telegram_notifier.get_notifier")
    def test_down_event_sends_telegram(self, mock_get_notifier, pm):
        """下跌事件应触发 Telegram 告警。"""
        mock_notifier = MagicMock()
        mock_get_notifier.return_value = mock_notifier

        evt = {
            "bet_id": "alert_bet",
            "threshold_pct": 10,
            "direction": "down",
            "price_at_cross": 0.45,
            "entry_price": 0.50,
            "return_pct": -10.0,
            "ts": "2026-01-01T00:00:00+00:00",
            "category": "weather",
            "question": "Will it rain?",
        }
        pm._send_alert(evt)
        mock_notifier.send_async.assert_called_once()

    @patch("src.utils.telegram_notifier.get_notifier")
    def test_up_event_no_telegram_by_default(self, mock_get_notifier, pm):
        """上涨事件默认不发送 Telegram。"""
        mock_notifier = MagicMock()
        mock_get_notifier.return_value = mock_notifier

        evt = {
            "bet_id": "up_bet",
            "threshold_pct": 10,
            "direction": "up",
            "price_at_cross": 0.55,
            "entry_price": 0.50,
            "return_pct": 10.0,
            "ts": "2026-01-01T00:00:00+00:00",
            "category": "sports",
            "question": "Test?",
        }
        pm._send_alert(evt)
        mock_notifier.send_async.assert_not_called()

    @patch("src.utils.telegram_notifier.get_notifier")
    def test_cooldown_prevents_rapid_alerts(self, mock_get_notifier, pm):
        """冷却时间内不应重复告警。"""
        mock_notifier = MagicMock()
        mock_get_notifier.return_value = mock_notifier

        evt = {
            "bet_id": "cool_bet",
            "threshold_pct": 5,
            "direction": "down",
            "price_at_cross": 0.475,
            "entry_price": 0.50,
            "return_pct": -5.0,
            "ts": "2026-01-01T00:00:00+00:00",
            "category": "weather",
            "question": "Test?",
        }
        pm._send_alert(evt)
        pm._send_alert(evt)  # 冷却期内
        assert mock_notifier.send_async.call_count == 1


# ── T177 get_realtime_buffer YES token 价格测试 ──────────────────────


class TestRealtimeBufferYesPrices:
    """get_realtime_buffer 应返回 YES token 价格（与 DB 一致）。"""

    def test_realtime_no_direction_returns_yes_prices(self, pm):
        """NO 方向 bet: buffer=NO 调整价 → get_realtime_buffer 返回 YES token 价格。"""
        pm._outcomes["no_rt_bet"] = "NO"
        # 模拟 NO 调整后的 buffer: NO mid=0.62 → YES mid=0.38
        # NO bid=0.60 → YES ask=1-0.60=0.40; NO ask=0.64 → YES bid=1-0.64=0.36
        pm._buffers["no_rt_bet"] = deque([(time.time(), 0.62, 0.60, 0.64)])
        result = pm.get_realtime_buffer("no_rt_bet")
        assert len(result) == 1
        assert abs(result[0]["mid"] - 0.38) < 0.001
        assert abs(result[0]["bid"] - 0.36) < 0.001
        assert abs(result[0]["ask"] - 0.40) < 0.001

    def test_realtime_yes_direction_returns_as_is(self, pm):
        """YES 方向 bet: buffer=YES 价格 → 原样返回。"""
        pm._outcomes["yes_rt_bet"] = "YES"
        pm._buffers["yes_rt_bet"] = deque([(time.time(), 0.55, 0.54, 0.56)])
        result = pm.get_realtime_buffer("yes_rt_bet")
        assert len(result) == 1
        assert abs(result[0]["mid"] - 0.55) < 0.001
        assert abs(result[0]["bid"] - 0.54) < 0.001
        assert abs(result[0]["ask"] - 0.56) < 0.001


# ── T136: FlashCrash 反弹冷却测试 ────────────────────────────────────


@pytest.fixture
def fc_config():
    """返回包含 flash_crash 的 PriceMonitor 配置。"""
    return {
        "enabled": True,
        "sample_interval_seconds": 10,
        "db_flush_interval_seconds": 60,
        "memory_buffer_size": 100,
        "retention_days": 7,
        "cleanup_interval_hours": 6,
        "thresholds_down_pct": [5, 10, 20, 50],
        "thresholds_up_pct": [10, 20],
        "telegram_alert_on_down": True,
        "telegram_alert_on_up": False,
        "alert_cooldown_seconds": 300,
        "auto_sell_enabled": False,
        "auto_sell_threshold_pct": 50,
        "flash_crash": {
            "enabled": True,
            "window_3min_pct": 18.0,
            "window_5min_pct": 28.0,
            "confirm_delay_seconds": 90,
            "confirm_rebound_threshold": 0.70,
            "min_price_for_trigger": 0.10,
            "rebound_cooldown_seconds": 300,
        },
    }


@pytest.fixture
def fc_pm(_init_db, fc_config):
    """创建启用了 flash_crash 的 PriceMonitor 实例。"""
    with patch("src.monitor.price_monitor.get", return_value=fc_config):
        from src.monitor.price_monitor import PriceMonitor

        monitor = PriceMonitor()
    return monitor


class TestFlashCrashReboundCooldown:
    """T136: 验证反弹取消后的冷却期防止重检测循环。"""

    def _make_bet(
        self, bet_id="bet-fc1", entry_price=0.50, category="sports", market_id="mkt-fc1"
    ):
        return {
            "bet_id": bet_id,
            "market_id": market_id,
            "market_price": entry_price,
            "outcome": "YES",
            "category": category,
        }

    def _make_market_data(self, market_id="mkt-fc1", bid=0.34, ask=0.36):
        return {market_id: {"bestBid": str(bid), "bestAsk": str(ask)}}

    def test_rebound_cancel_adds_cooldown(self, fc_pm):
        """反弹取消后 bet_id 应进入冷却 dict。"""
        now = time.time()
        bet_id = "bet-rebound1"
        market_id = "mkt-rebound1"
        # 模拟已在 pending 中，且已过确认期
        fc_pm._fc_pending[bet_id] = (now - 100, 0.34, 0.50)
        # 缓冲区有数据
        fc_pm._buffers[bet_id] = deque([(now, 0.45, 0.44, 0.46)])

        bets = [self._make_bet(bet_id, 0.50, market_id=market_id)]
        mdata = self._make_market_data(market_id, 0.44, 0.46)

        results = fc_pm.check_flash_crashes(bets, mdata)
        assert len(results) == 0  # 反弹取消，无退出
        assert bet_id not in fc_pm._fc_pending  # 已从 pending 移除
        assert bet_id in fc_pm._fc_rebound_cooldown  # 进入冷却

    def test_cooldown_blocks_redetection(self, fc_pm):
        """冷却期内，即使价格满足检测条件也不应触发新检测。"""
        now = time.time()
        bet_id = "bet-cooldown1"
        market_id = "mkt-cooldown1"

        # 设置冷却状态（刚反弹取消）
        fc_pm._fc_rebound_cooldown[bet_id] = now - 10  # 10秒前反弹

        # 制造足够的缓冲数据使得 drop ≥ 18%
        fc_pm._buffers[bet_id] = deque(
            [
                (now - 200, 0.50, 0.49, 0.51),
                (now, 0.35, 0.34, 0.36),
            ]
        )

        bets = [self._make_bet(bet_id, 0.50, market_id=market_id)]
        mdata = self._make_market_data(market_id, 0.34, 0.36)

        results = fc_pm.check_flash_crashes(bets, mdata)
        assert len(results) == 0
        assert bet_id not in fc_pm._fc_pending  # 未重新进入 pending

    def test_cooldown_expires_allows_redetection(self, fc_pm):
        """冷却期过后应允许重新检测。"""
        now = time.time()
        bet_id = "bet-expire1"
        market_id = "mkt-expire1"

        # 冷却已过期（400秒前反弹，冷却期300秒）
        fc_pm._fc_rebound_cooldown[bet_id] = now - 400

        # 制造缓冲数据: 3分钟前价格 0.50，现在 0.35 → drop=30%
        fc_pm._buffers[bet_id] = deque(
            [
                (now - 200, 0.50, 0.49, 0.51),
                (now, 0.35, 0.34, 0.36),
            ]
        )

        bets = [self._make_bet(bet_id, 0.50, market_id=market_id)]
        mdata = self._make_market_data(market_id, 0.34, 0.36)

        results = fc_pm.check_flash_crashes(bets, mdata)
        # 冷却已过期，应重新检测并加入 pending
        assert bet_id in fc_pm._fc_pending

    def test_cleanup_removes_expired_cooldown(self, fc_pm):
        """cleanup_fc_state 应移除已关仓和已过期的冷却条目。"""
        now = time.time()
        fc_pm._fc_rebound_cooldown["closed-bet"] = now - 10
        fc_pm._fc_rebound_cooldown["expired-bet"] = now - 400
        fc_pm._fc_rebound_cooldown["active-bet"] = now - 10

        open_ids = {"active-bet"}
        fc_pm.cleanup_fc_state(open_ids)

        # closed-bet: 不在 open_ids → 移除
        assert "closed-bet" not in fc_pm._fc_rebound_cooldown
        # expired-bet: 不在 open_ids → 移除
        assert "expired-bet" not in fc_pm._fc_rebound_cooldown
        # active-bet: 在 open_ids 且未过期 → 保留
        assert "active-bet" in fc_pm._fc_rebound_cooldown
