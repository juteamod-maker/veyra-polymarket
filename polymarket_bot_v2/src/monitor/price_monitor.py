"""
price_monitor.py — 持仓价格时序记录 + 分级涨跌阈值穿越检测 + Telegram 告警

设计决策:
- 10秒级采样保存在内存 deque（每 bet_id 独立缓冲区）
- 每60秒将内存数据聚合（1分钟 OHLC）后批量 INSERT 到 SQLite bet_price_series
- 阈值穿越事件写入 price_move_log（UNIQUE 约束防重复）
- 下跌阈值触发 Telegram 告警，上涨仅记录

T169: 分级涨跌记录、可视化与阈值告警
"""

import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from src.utils.config_loader import get
from src.utils.db import connect

logger = logging.getLogger("price_monitor")


class PriceMonitor:
    """持仓价格监控器：采样 → 阈值检测 → DB持久化 → Telegram告警。"""

    def __init__(self) -> None:
        cfg = get("price_monitor") or {}
        self._enabled: bool = cfg.get("enabled", False)
        self._buffer_size: int = cfg.get("memory_buffer_size", 360)
        self._flush_interval: int = cfg.get("db_flush_interval_seconds", 60)
        self._retention_days: int = cfg.get("retention_days", 7)
        self._thresholds_down: list[float] = cfg.get(
            "thresholds_down_pct", [5, 10, 15, 20, 30, 40, 50]
        )
        self._thresholds_up: list[float] = cfg.get(
            "thresholds_up_pct", [10, 20, 30, 50]
        )
        self._alert_on_down: bool = cfg.get("telegram_alert_on_down", True)
        self._alert_on_up: bool = cfg.get("telegram_alert_on_up", False)
        self._alert_cooldown: int = cfg.get("alert_cooldown_seconds", 300)
        self._auto_sell_enabled: bool = cfg.get("auto_sell_enabled", False)
        self._auto_sell_threshold: float = cfg.get("auto_sell_threshold_pct", 50)

        # T71: Flash Crash 闪崩检测
        fc_cfg = cfg.get("flash_crash", {})
        self._fc_enabled: bool = fc_cfg.get("enabled", False)
        self._fc_3min_pct: float = fc_cfg.get("window_3min_pct", 18.0)
        self._fc_5min_pct: float = fc_cfg.get("window_5min_pct", 28.0)
        self._fc_confirm_delay: int = fc_cfg.get("confirm_delay_seconds", 90)
        self._fc_rebound_threshold: float = fc_cfg.get(
            "confirm_rebound_threshold", 0.70
        )
        self._fc_min_price: float = fc_cfg.get("min_price_for_trigger", 0.10)
        # flash crash 确认队列: {bet_id: (trigger_ts, trigger_price, entry_price)}
        self._fc_pending: dict[str, tuple[float, float, float]] = {}
        # 已触发的 flash crash (防重复): {bet_id: trigger_ts}
        self._fc_triggered: dict[str, float] = {}
        # T136: 反弹取消冷却期 (防重检测循环): {bet_id: cancel_ts}
        self._fc_rebound_cooldown: dict[str, float] = {}
        self._fc_rebound_cooldown_sec: int = int(
            fc_cfg.get("rebound_cooldown_seconds", 300)
        )

        # 内存缓冲区: {bet_id: deque of (ts_float, mid, bid, ask)}
        self._buffers: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self._buffer_size)
        )
        # 已穿越阈值集合: {bet_id: set((threshold_pct, direction))}
        self._crossed: dict[str, set[tuple[float, str]]] = defaultdict(set)
        # 告警冷却: {bet_id: last_alert_ts_float}
        self._alert_ts: dict[str, float] = {}
        # 各 bet 的方向: {bet_id: "YES"/"NO"}
        self._outcomes: dict[str, str] = {}
        # 上次 flush 时间
        self._last_flush_ts: float = 0.0
        self._lock = threading.Lock()

        if self._enabled:
            self._load_crossed_from_db()
            logger.info(
                f"[PriceMonitor] 初始化完成 | "
                f"down={self._thresholds_down} up={self._thresholds_up}"
            )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def record_prices(
        self,
        open_bets: list[dict],
        market_data: dict[str, dict],
    ) -> list[dict]:
        """
        记录当前价格到内存缓冲区，检测阈值穿越。

        参数:
            open_bets: get_open_paper_bets() 返回的仓位列表
            market_data: {market_id: {outcomePrices: ...}} 市场数据

        返回:
            本次新穿越的事件列表 [{bet_id, threshold_pct, direction, ...}]
        """
        if not self._enabled or not open_bets:
            return []

        now = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        new_events: list[dict] = []

        with self._lock:
            for bet in open_bets:
                bet_id = bet.get("bet_id", "")
                market_id = bet.get("market_id", "")
                outcome = (bet.get("outcome") or "YES").strip().upper()
                entry_price = float(bet.get("market_price") or 0)
                if entry_price <= 0 or not bet_id:
                    continue

                mdata = market_data.get(market_id, {})
                self._outcomes[bet_id] = outcome
                mid_price = self._extract_mid_price(mdata, outcome)
                if mid_price is None or mid_price <= 0:
                    continue

                bid = self._extract_price(mdata, outcome, "bid")
                ask = self._extract_price(mdata, outcome, "ask")

                # 写入内存缓冲区
                self._buffers[bet_id].append((now, mid_price, bid, ask))

                # 阈值穿越检测
                events = self._check_thresholds(
                    bet_id=bet_id,
                    entry_price=entry_price,
                    current_price=mid_price,
                    now_iso=now_iso,
                    category=bet.get("category", ""),
                    question=bet.get("question", ""),
                )
                new_events.extend(events)

            # 定期 flush 到 DB
            if now - self._last_flush_ts >= self._flush_interval:
                self._flush_to_db(now_iso)
                self._last_flush_ts = now

        # 发送 Telegram 告警（在锁外执行，避免阻塞）
        for evt in new_events:
            self._send_alert(evt)

        return new_events

    def cleanup_old_data(self) -> int:
        """清理过期的价格时序数据。返回删除行数。"""
        if not self._enabled:
            return 0
        cutoff = datetime.now(timezone.utc).isoformat()
        # 简单方式：用字符串比较 ISO 格式日期
        from datetime import timedelta

        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        cutoff_str = cutoff_dt.isoformat()
        try:
            with connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM bet_price_series WHERE ts < ?", (cutoff_str,)
                )
                deleted = cursor.rowcount
                if deleted:
                    logger.info(
                        f"[PriceMonitor] 清理 {deleted} 条过期价格记录"
                        f"（>{self._retention_days}天）"
                    )
                return deleted
        except Exception as e:
            logger.warning(f"[PriceMonitor] 清理过期数据失败: {e}")
            return 0

    def cleanup_closed_bets(self, open_bet_ids: set[str]) -> None:
        """清理已关仓 bet 的内存缓冲区。"""
        with self._lock:
            stale = [bid for bid in self._buffers if bid not in open_bet_ids]
            for bid in stale:
                del self._buffers[bid]
                self._crossed.pop(bid, None)
                self._alert_ts.pop(bid, None)
                self._outcomes.pop(bid, None)

    def get_price_series(self, bet_id: str, hours: int = 24) -> list[dict]:
        """查询指定 bet 的价格时序（供 Dashboard API 使用）。"""
        from datetime import timedelta

        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        try:
            with connect() as conn:
                rows = conn.execute(
                    "SELECT ts, price_mid, price_bid, price_ask "
                    "FROM bet_price_series "
                    "WHERE bet_id = ? AND ts >= ? ORDER BY ts",
                    (bet_id, since),
                ).fetchall()
                return [
                    {
                        "ts": r[0],
                        "mid": r[1],
                        "bid": r[2],
                        "ask": r[3],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.warning(f"[PriceMonitor] 查询价格时序失败: {e}")
            return []

    def get_events(self, bet_id: str | None = None) -> list[dict]:
        """查询阈值穿越事件（供 Dashboard API 使用）。"""
        try:
            with connect() as conn:
                if bet_id:
                    rows = conn.execute(
                        "SELECT bet_id, threshold_pct, direction, price_at_cross, "
                        "entry_price, return_pct, ts, action, category, question "
                        "FROM price_move_log WHERE bet_id = ? ORDER BY ts DESC",
                        (bet_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT bet_id, threshold_pct, direction, price_at_cross, "
                        "entry_price, return_pct, ts, action, category, question "
                        "FROM price_move_log ORDER BY ts DESC LIMIT 200",
                    ).fetchall()
                return [
                    {
                        "bet_id": r[0],
                        "threshold_pct": r[1],
                        "direction": r[2],
                        "price_at_cross": r[3],
                        "entry_price": r[4],
                        "return_pct": r[5],
                        "ts": r[6],
                        "action": r[7],
                        "category": r[8],
                        "question": r[9],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.warning(f"[PriceMonitor] 查询穿越事件失败: {e}")
            return []

    def get_realtime_buffer(self, bet_id: str) -> list[dict]:
        """返回内存中的实时10秒级数据（供 Dashboard API 使用）。

        与 _flush_to_db 保持一致：返回 YES token 价格，前端负责 NO 转换。
        """
        with self._lock:
            buf = self._buffers.get(bet_id, deque())
            outcome = self._outcomes.get(bet_id, "YES")
            result = []
            for p in buf:
                mid, bid, ask = p[1], p[2], p[3]
                if outcome == "NO":
                    mid = round(1.0 - mid, 6) if mid else mid
                    yes_bid = round(1.0 - ask, 6) if ask else ask
                    yes_ask = round(1.0 - bid, 6) if bid else bid
                    bid, ask = yes_bid, yes_ask
                result.append({"ts": p[0], "mid": mid, "bid": bid, "ask": ask})
            return result

    def get_summary(self) -> dict:
        """返回监控摘要统计。"""
        with self._lock:
            active_bets = len(self._buffers)
            total_samples = sum(len(b) for b in self._buffers.values())
            total_crossed = sum(len(s) for s in self._crossed.values())

        # DB 统计
        db_rows = 0
        event_count = 0
        try:
            with connect() as conn:
                row = conn.execute("SELECT COUNT(*) FROM bet_price_series").fetchone()
                db_rows = row[0] if row else 0
                row2 = conn.execute("SELECT COUNT(*) FROM price_move_log").fetchone()
                event_count = row2[0] if row2 else 0
        except Exception:
            pass

        return {
            "enabled": self._enabled,
            "active_bets_monitored": active_bets,
            "memory_samples": total_samples,
            "thresholds_crossed": total_crossed,
            "db_price_rows": db_rows,
            "db_event_rows": event_count,
            "retention_days": self._retention_days,
        }

    # ------------------------------------------------------------------
    # T71: Flash Crash 闪崩检测
    # ------------------------------------------------------------------

    def check_flash_crashes(
        self, open_bets: list[dict], market_data: dict[str, dict]
    ) -> list[dict]:
        """
        检测体育赛道闪崩，返回需要紧急止损的投注列表。

        二次确认机制：
        1. 首次检测到闪崩 → 加入待确认队列
        2. confirm_delay_seconds 后检查是否反弹
        3. 未反弹 → 确认闪崩，返回需止损的bet
        4. 已反弹 → 取消

        返回: [{bet_id, exit_price, trigger: "flash_crash", ...}]
        """
        if not self._fc_enabled:
            return []

        now = time.time()
        results: list[dict] = []

        with self._lock:
            for bet in open_bets:
                bet_id = bet.get("bet_id", "")
                category = bet.get("category", "")
                if not bet_id or category != "sports":
                    continue
                if bet_id in self._fc_triggered:
                    continue  # 已触发过，不重复

                entry_price = float(bet.get("market_price") or 0)
                if entry_price <= 0:
                    continue

                market_id = bet.get("market_id", "")
                outcome = (bet.get("outcome") or "YES").strip().upper()
                mdata = market_data.get(market_id, {})
                current_price = self._extract_mid_price(mdata, outcome)
                if current_price is None or current_price <= 0:
                    continue

                # 低于最低价格直接触发（无需确认）
                if current_price < self._fc_min_price:
                    if bet_id not in self._fc_triggered:
                        self._fc_triggered[bet_id] = now
                        self._fc_pending.pop(bet_id, None)
                        results.append(
                            self._make_fc_exit(
                                bet, current_price, "flash_crash_min_price"
                            )
                        )
                        logger.warning(
                            f"[FlashCrash] ⚡ 低价直接触发 {bet_id[:8]} "
                            f"price={current_price:.4f} < min={self._fc_min_price}"
                        )
                    continue

                # 检查是否正在待确认
                if bet_id in self._fc_pending:
                    trigger_ts, trigger_price, _ = self._fc_pending[bet_id]
                    elapsed = now - trigger_ts
                    if elapsed >= self._fc_confirm_delay:
                        # 确认期到：检查是否反弹
                        rebound_target = trigger_price * self._fc_rebound_threshold
                        if current_price >= rebound_target:
                            # 已反弹 → 取消
                            logger.info(
                                f"[FlashCrash] ✅ 反弹取消 {bet_id[:8]} "
                                f"price={current_price:.4f} ≥ "
                                f"rebound_target={rebound_target:.4f}"
                            )
                            del self._fc_pending[bet_id]
                            # T136: 记录反弹冷却，防止下个周期立即重检测
                            self._fc_rebound_cooldown[bet_id] = now
                        else:
                            # 未反弹 → 确认闪崩
                            self._fc_triggered[bet_id] = now
                            del self._fc_pending[bet_id]
                            results.append(
                                self._make_fc_exit(
                                    bet, current_price, "flash_crash_confirmed"
                                )
                            )
                            logger.warning(
                                f"[FlashCrash] 🔴 闪崩确认 {bet_id[:8]} "
                                f"price={current_price:.4f} 未反弹 "
                                f"(target≥{rebound_target:.4f})"
                            )
                    continue  # 等待期内不做新检测

                # 新检测：计算滑窗跌幅
                # T136: 反弹冷却期内跳过新检测（防止反弹取消→立即重检测循环）
                cooldown_ts = self._fc_rebound_cooldown.get(bet_id, 0)
                if now - cooldown_ts < self._fc_rebound_cooldown_sec:
                    continue

                buf = self._buffers.get(bet_id, deque())
                if len(buf) < 2:
                    continue

                # 找3分钟前和5分钟前的价格
                _3min_ago = now - 180
                _5min_ago = now - 300
                price_3min = self._find_price_at(buf, _3min_ago)
                price_5min = self._find_price_at(buf, _5min_ago)

                triggered = False
                drop_pct = 0.0

                if price_3min and price_3min > 0:
                    drop_3 = ((price_3min - current_price) / price_3min) * 100
                    if drop_3 >= self._fc_3min_pct:
                        triggered = True
                        drop_pct = drop_3

                if not triggered and price_5min and price_5min > 0:
                    drop_5 = ((price_5min - current_price) / price_5min) * 100
                    if drop_5 >= self._fc_5min_pct:
                        triggered = True
                        drop_pct = drop_5

                if triggered:
                    # 加入待确认队列
                    self._fc_pending[bet_id] = (now, current_price, entry_price)
                    logger.warning(
                        f"[FlashCrash] ⚠️ 闪崩检测 {bet_id[:8]} "
                        f"drop={drop_pct:.1f}% price={current_price:.4f} "
                        f"→ 等待{self._fc_confirm_delay}s确认"
                    )

        return results

    def _find_price_at(self, buf: deque, target_ts: float) -> float | None:
        """在缓冲区中找到最接近target_ts的价格"""
        best = None
        best_diff = float("inf")
        for ts, mid, _bid, _ask in buf:
            diff = abs(ts - target_ts)
            if diff < best_diff:
                best_diff = diff
                best = mid
        # 只返回30秒内的匹配
        return best if best_diff < 30 else None

    @staticmethod
    def _make_fc_exit(bet: dict, exit_price: float, trigger: str) -> dict:
        """构造闪崩止损退出字典"""
        entry_p = float(bet.get("market_price") or 0)
        return_pct = (exit_price - entry_p) / entry_p if entry_p > 0 else -1.0
        result = dict(bet)
        result["exit_price"] = exit_price
        result["return_pct"] = return_pct
        result["trigger"] = trigger
        return result

    def cleanup_fc_state(self, open_bet_ids: set[str]) -> None:
        """清理已关仓bet的闪崩状态"""
        with self._lock:
            stale_pending = [k for k in self._fc_pending if k not in open_bet_ids]
            for k in stale_pending:
                del self._fc_pending[k]
            stale_triggered = [k for k in self._fc_triggered if k not in open_bet_ids]
            for k in stale_triggered:
                del self._fc_triggered[k]
            # T136: 清理反弹冷却（已关仓或已过期）
            now = time.time()
            stale_cooldown = [
                k
                for k, ts in self._fc_rebound_cooldown.items()
                if k not in open_bet_ids or now - ts >= self._fc_rebound_cooldown_sec
            ]
            for k in stale_cooldown:
                del self._fc_rebound_cooldown[k]

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _load_crossed_from_db(self) -> None:
        """启动时从 DB 恢复已穿越阈值（幂等保护）。"""
        try:
            with connect() as conn:
                rows = conn.execute(
                    "SELECT bet_id, threshold_pct, direction FROM price_move_log"
                ).fetchall()
                for r in rows:
                    self._crossed[r[0]].add((r[1], r[2]))
            if rows:
                logger.info(f"[PriceMonitor] 从 DB 恢复 {len(rows)} 条已穿越阈值记录")
        except Exception as e:
            logger.warning(f"[PriceMonitor] 恢复已穿越阈值失败: {e}")

    def _check_thresholds(
        self,
        bet_id: str,
        entry_price: float,
        current_price: float,
        now_iso: str,
        category: str,
        question: str,
    ) -> list[dict]:
        """检测阈值穿越，返回新事件列表（不含已穿越的）。"""
        if entry_price <= 0:
            return []

        return_pct = ((current_price - entry_price) / entry_price) * 100
        events: list[dict] = []

        # 下跌阈值
        if return_pct < 0:
            abs_drop = abs(return_pct)
            for th in self._thresholds_down:
                key = (th, "down")
                if key not in self._crossed[bet_id] and abs_drop >= th:
                    evt = self._record_event(
                        bet_id=bet_id,
                        threshold_pct=th,
                        direction="down",
                        price_at_cross=current_price,
                        entry_price=entry_price,
                        return_pct=return_pct,
                        now_iso=now_iso,
                        category=category,
                        question=question,
                    )
                    if evt:
                        events.append(evt)

        # 上涨阈值
        if return_pct > 0:
            for th in self._thresholds_up:
                key = (th, "up")
                if key not in self._crossed[bet_id] and return_pct >= th:
                    evt = self._record_event(
                        bet_id=bet_id,
                        threshold_pct=th,
                        direction="up",
                        price_at_cross=current_price,
                        entry_price=entry_price,
                        return_pct=return_pct,
                        now_iso=now_iso,
                        category=category,
                        question=question,
                    )
                    if evt:
                        events.append(evt)

        return events

    def _record_event(
        self,
        bet_id: str,
        threshold_pct: float,
        direction: str,
        price_at_cross: float,
        entry_price: float,
        return_pct: float,
        now_iso: str,
        category: str,
        question: str,
    ) -> dict | None:
        """写入 price_move_log 并标记已穿越。UNIQUE 约束保证幂等。"""
        # price_at_cross 是方向调整后的价格；DB 存 YES token 价格以配合前端转换
        outcome = self._outcomes.get(bet_id, "YES")
        db_price = (
            round(1.0 - price_at_cross, 4)
            if outcome == "NO"
            else round(price_at_cross, 4)
        )
        try:
            with connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO price_move_log "
                    "(bet_id, threshold_pct, direction, price_at_cross, "
                    "entry_price, return_pct, ts, action, category, question) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'record', ?, ?)",
                    (
                        bet_id,
                        threshold_pct,
                        direction,
                        db_price,
                        round(entry_price, 4),
                        round(return_pct, 2),
                        now_iso,
                        category,
                        question[:200] if question else "",
                    ),
                )
            self._crossed[bet_id].add((threshold_pct, direction))
            arrow = "📉" if direction == "down" else "📈"
            logger.info(
                f"[PriceMonitor] {arrow} {bet_id[:8]} "
                f"crossed {direction} {threshold_pct}% "
                f"(entry={entry_price:.3f} now={price_at_cross:.3f} "
                f"return={return_pct:+.1f}%)"
            )
            return {
                "bet_id": bet_id,
                "threshold_pct": threshold_pct,
                "direction": direction,
                "price_at_cross": price_at_cross,
                "entry_price": entry_price,
                "return_pct": return_pct,
                "ts": now_iso,
                "category": category,
                "question": question,
            }
        except Exception as e:
            logger.warning(f"[PriceMonitor] 记录穿越事件失败: {e}")
            return None

    def _flush_to_db(self, now_iso: str) -> None:
        """将内存缓冲区写入 SQLite（每分钟一条快照）。

        注意：DB 始终存储 YES token 价格（与 Gamma API bestBid/bestAsk 一致），
        前端负责 NO 方向显示转换。缓冲区内为方向调整后的价格，
        NO 方向需要反转回 YES token 价格后再写入。
        """
        rows_to_insert: list[tuple] = []
        for bet_id, buf in self._buffers.items():
            if not buf:
                continue
            # 取最新一个价格点作为快照
            latest = buf[-1]  # (ts_float, mid, bid, ask)
            mid, bid, ask = latest[1], latest[2], latest[3]

            # 缓冲区存的是方向调整后的价格，DB 需要 YES token 价格
            outcome = self._outcomes.get(bet_id, "YES")
            if outcome == "NO":
                mid = round(1.0 - mid, 6) if mid else mid
                # YES_bid = 1 - NO_ask, YES_ask = 1 - NO_bid
                yes_bid = round(1.0 - ask, 6) if ask else ask
                yes_ask = round(1.0 - bid, 6) if bid else bid
                bid, ask = yes_bid, yes_ask

            rows_to_insert.append((bet_id, now_iso, mid, bid, ask))

        if not rows_to_insert:
            return

        try:
            with connect() as conn:
                conn.executemany(
                    "INSERT INTO bet_price_series "
                    "(bet_id, ts, price_mid, price_bid, price_ask) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows_to_insert,
                )
            logger.debug(f"[PriceMonitor] flush {len(rows_to_insert)} 条价格记录到 DB")
        except Exception as e:
            logger.warning(f"[PriceMonitor] flush 到 DB 失败: {e}")

    def _send_alert(self, evt: dict) -> None:
        """发送 Telegram 告警（含冷却控制）。"""
        direction = evt["direction"]
        should_alert = (direction == "down" and self._alert_on_down) or (
            direction == "up" and self._alert_on_up
        )
        if not should_alert:
            return

        bet_id = evt["bet_id"]
        now = time.time()

        # 冷却检查
        last_alert = self._alert_ts.get(bet_id, 0)
        if now - last_alert < self._alert_cooldown:
            return

        self._alert_ts[bet_id] = now

        arrow = "📉" if direction == "down" else "📈"
        cat_emoji = {
            "weather": "⛅",
            "sports": "⚽",
            "crypto": "₿",
            "finance": "💰",
            "geopolitics": "🌍",
            "tech": "📊",
        }.get(evt.get("category", ""), "📊")

        question = evt.get("question", "")
        if len(question) > 80:
            question = question[:77] + "..."

        text = (
            f"{arrow} <b>价格告警 {direction.upper()} {evt['threshold_pct']}%</b>\n\n"
            f"{cat_emoji} {question}\n"
            f"入场价: <code>{evt['entry_price']:.3f}</code>\n"
            f"当前价: <code>{evt['price_at_cross']:.3f}</code>\n"
            f"回报率: <code>{evt['return_pct']:+.1f}%</code>\n"
            f"Bet: <code>{bet_id[:12]}</code>"
        )

        try:
            from src.utils.telegram_notifier import get_notifier

            notifier = get_notifier()
            notifier.send_async(text)
        except Exception as e:
            logger.debug(f"[PriceMonitor] Telegram 告警发送失败: {e}")

    @staticmethod
    def _extract_mid_price(mdata: dict, outcome: str) -> float | None:
        """从 market_data 提取中间价（bid+ask 均值或 outcomePrices）。"""
        # 方式1：有 orderbook 数据时取 bid/ask 中值
        bid = PriceMonitor._extract_price(mdata, outcome, "bid")
        ask = PriceMonitor._extract_price(mdata, outcome, "ask")
        if bid and ask and bid > 0 and ask > 0:
            return (bid + ask) / 2

        # 方式2：outcomePrices 字段（Gamma API 返回）
        prices_str = mdata.get("outcomePrices", "")
        if prices_str:
            try:
                import json

                prices = json.loads(prices_str)
                if isinstance(prices, list) and len(prices) >= 2:
                    idx = 0 if outcome == "YES" else 1
                    return float(prices[idx])
            except (json.JSONDecodeError, ValueError, IndexError):
                pass

        # 方式3：bestAsk / bestBid 字段（始终是 YES token 价格）
        best_bid = float(mdata.get("bestBid", 0) or 0)
        best_ask = float(mdata.get("bestAsk", 0) or 0)
        outcome_upper = (outcome or "YES").strip().upper()
        if best_bid > 0 and best_ask > 0:
            yes_mid = (best_bid + best_ask) / 2
            return round(1.0 - yes_mid, 6) if outcome_upper == "NO" else yes_mid
        if best_ask > 0:
            return round(1.0 - best_ask, 6) if outcome_upper == "NO" else best_ask
        if best_bid > 0:
            return round(1.0 - best_bid, 6) if outcome_upper == "NO" else best_bid

        return None

    @staticmethod
    def _extract_price(mdata: dict, outcome: str, side: str) -> float | None:
        """提取指定方向(YES/NO)的 bid 或 ask 价格。

        Gamma API 的 bestBid/bestAsk 始终是 YES token 的价格。
        对于 NO 方向需要转换：NO_bid = 1 - YES_ask, NO_ask = 1 - YES_bid。
        """
        outcome_upper = (outcome or "YES").strip().upper()

        if outcome_upper == "YES":
            key = "bestBid" if side == "bid" else "bestAsk"
            val = mdata.get(key)
            if val:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        else:
            # NO_bid(卖NO) = 1 - YES_ask;  NO_ask(买NO) = 1 - YES_bid
            key = "bestAsk" if side == "bid" else "bestBid"
            val = mdata.get(key)
            if val:
                try:
                    return round(1.0 - float(val), 6)
                except (ValueError, TypeError):
                    pass

        # 回退：tokens[] 价格已按方向区分
        tokens = mdata.get("tokens", [])
        if isinstance(tokens, list):
            for t in tokens:
                t_outcome = (t.get("outcome") or "").strip().upper()
                if t_outcome == outcome_upper:
                    price = t.get("price")
                    if price:
                        try:
                            return float(price)
                        except (ValueError, TypeError):
                            pass
        return None
