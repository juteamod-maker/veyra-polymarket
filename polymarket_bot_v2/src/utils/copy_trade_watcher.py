"""copy_trade_watcher.py — T298/T140 跟投监控后台服务类

监控活跃跟投任务对应钱包的新交易，
通过 Polymarket Data API 检测新信号，
DRY RUN 模式：直接插入 paper_bets 表（source='copy_trade'）。
实盘模式：构造 Signal → OrderExecutor.execute() CLOB 买入 → 写入 paper_bets。
T140: 升级为完整实盘跟投支持，受 emergency_freeze 约束。
"""

# ═══════════════════════════════════════════════════════════════════
# SECURITY RED LINE COMPLIANCE (T298-D 安全附加条款)
# ───────────────────────────────────────────────────────────────────
# ✅ 不使用任何第三方跟投脚本、polyhub_copy、外部可执行文件或外部 API 密钥
# ✅ DRY RUN 模式：信号以 paper_bets 形式插入；实盘模式：经 OrderExecutor 下 CLOB 单后写入 paper_bets
# ✅ 信号处理链：Data API → 过滤器 → Signal → OrderExecutor → paper_bets → OutcomeTracker
# ✅ 连续错误达到 max_error_count 后自动暂停任务并发送 Telegram 告警
# ✅ 下注金额受双重限制：任务 base_usdc × 跟投系数 ≤ hard_max_bet_usdc
# ✅ 私钥仅通过 .env 注入，本模块不访问任何凭据
# ═══════════════════════════════════════════════════════════════════

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from src.utils.database import OperationalError
from src.utils.database import connect as _pg_connect

logger = logging.getLogger("copy_trade_watcher")

_DATA_API = "https://data-api.polymarket.com"
_CLOB_API = "https://clob.polymarket.com"
_REQUEST_TIMEOUT = 12


class CopyTradeWatcher:
    """
    跟投钱包监控器。

    在 main.py / run_paper_trading.py 的主循环中周期性调用 run_cycle()：
        watcher = CopyTradeWatcher(db_path=...)
        signals_generated = watcher.run_cycle()
    """

    def __init__(self, db_path: Path | None = None, executor=None) -> None:
        from pathlib import Path as _Path

        from src.utils.config_loader import get as _get

        if db_path is None:
            _root = _Path(__file__).resolve().parent.parent.parent
            db_path = _root / "data" / "polymarket.db"
        self._db_path = db_path
        self._get = _get
        # T140: 实盘模式下注入 OrderExecutor 用于 CLOB 下单
        self._executor = executor
        # 独立扫描间隔：防止与主循环频率耦合
        self._last_scan_ts: float = 0.0

    # ── Public API ────────────────────────────────────────────────────

    def run_cycle(self) -> int:
        """
        处理全部 running 状态的跟投任务。
        返回本轮生成/执行的信号数（插入 paper_bets 的条数）。
        T140: 支持 DRY RUN / 实盘 / 实盘冻结三路径。
        """
        if not self._get("local_copy_trading.enabled"):
            return 0

        # 独立扫描间隔节流：避免与主循环频率耦合（SUPREME-2: 从 config 读取）
        scan_interval_min = int(
            self._get("local_copy_trading.scan_interval_minutes") or 5
        )
        now_ts = datetime.now(timezone.utc).timestamp()
        if (now_ts - self._last_scan_ts) < scan_interval_min * 60:
            return 0  # 距上次扫描时间不足，跳过本轮
        self._last_scan_ts = now_ts

        from src.utils.config_loader import is_dry_run as _is_dry_run

        maintenance_mode = bool(self._get("system.maintenance_mode"))
        emergency_freeze = bool(self._get("risk.emergency_freeze"))
        _dry = _is_dry_run()

        tasks = self._load_active_tasks()
        if not tasks:
            return 0

        total_signals = 0
        for task in tasks:
            try:
                wallet = str(task.get("wallet_address") or "")
                gate = self._runtime_gate_for_wallet(
                    wallet,
                    maintenance_mode=maintenance_mode,
                    emergency_freeze=emergency_freeze,
                )
                if gate["blocked"]:
                    # Runtime gate applies to both DRY RUN and live paths.
                    self._process_task_frozen(task, reason=gate["reason"], gate=gate)
                    continue

                if _dry:
                    # DRY RUN: 原有路径 — 直接写 paper_bets（无链上买单）
                    total_signals += self._process_task(task)
                else:
                    # 实盘 + 未冻结: Signal → executor.execute() → insert paper_bet
                    total_signals += self._process_task_live(task)
            except Exception as exc:
                logger.error("[CopyTrade] task_id=%s 处理异常: %s", task["id"], exc)
                self._increment_error(task["id"], str(exc))

        if total_signals:
            logger.info("[CopyTrade] 本轮共生成 %d 个跟投信号", total_signals)

        # ── T164: 跟卖监控 ───────────────────────────────────────
        self._monitor_copy_sells()

        return total_signals

    # ── Internal helpers ──────────────────────────────────────────────

    def _load_active_tasks(self) -> list[dict]:
        with _pg_connect() as conn:
            rows = conn.execute(
                "SELECT * FROM copy_tasks WHERE status='running'"
            ).fetchall()
            return [dict(r) for r in rows]

    def _runtime_gate_for_wallet(
        self,
        wallet: str,
        maintenance_mode: bool | None = None,
        emergency_freeze: bool | None = None,
    ) -> dict[str, Any]:
        """Build runtime gate state for a wallet.

        Gate order:
        1. system.maintenance_mode
        2. risk.emergency_freeze
        3. user_runtime_controls.freeze_auto
        4. user_runtime_controls.auto_trading_enabled

        If a wallet has no runtime row, user-level flags default to allow.
        """
        if maintenance_mode is None:
            maintenance_mode = bool(self._get("system.maintenance_mode"))
        if emergency_freeze is None:
            emergency_freeze = bool(self._get("risk.emergency_freeze"))

        freeze_auto = False
        auto_trading_enabled = True
        if wallet:
            try:
                with _pg_connect() as conn:
                    row = conn.execute(
                        "SELECT freeze_auto, auto_trading_enabled "
                        "FROM user_runtime_controls WHERE wallet_address=?",
                        (wallet,),
                    ).fetchone()
                if row:
                    freeze_auto = bool(row["freeze_auto"])
                    auto_trading_enabled = bool(row["auto_trading_enabled"])
            except Exception as exc:
                logger.debug(
                    "[CopyTrade] runtime gate read failed wallet=%s: %s",
                    wallet[:14],
                    exc,
                )

        blocked = (
            maintenance_mode
            or emergency_freeze
            or freeze_auto
            or (not auto_trading_enabled)
        )
        if maintenance_mode:
            reason = "maintenance_mode"
        elif emergency_freeze:
            reason = "emergency_freeze"
        elif freeze_auto:
            reason = "freeze_auto"
        elif not auto_trading_enabled:
            reason = "auto_trading_disabled"
        else:
            reason = ""

        return {
            "wallet": wallet,
            "maintenance_mode": maintenance_mode,
            "emergency_freeze": emergency_freeze,
            "freeze_auto": freeze_auto,
            "auto_trading_enabled": auto_trading_enabled,
            "blocked": blocked,
            "reason": reason,
        }

    def _process_task(self, task: dict) -> int:
        """检查单个任务钱包的新交易，返回信号数。"""
        wallet = task["wallet_address"]
        last_ts = float(task.get("last_seen_trade_ts") or 0.0)

        lookback_min = int(self._get("local_copy_trading.trade_lookback_minutes") or 30)
        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff_ts = max(last_ts, now_ts - lookback_min * 60)

        raw_trades = self._fetch_wallet_trades(wallet)
        if not raw_trades:
            return 0

        # 只看比截止时间更新的 BUY 交易（Data API side字段为BUY/SELL，非YES/NO）
        new_trades = [
            t
            for t in raw_trades
            if float(t.get("timestamp", 0)) > cutoff_ts
            and t.get("side", "").upper() == "BUY"
        ]
        if not new_trades:
            return 0

        # 解析任务配置
        try:
            task_categories: list[str] = json.loads(task.get("categories") or "[]")
        except (json.JSONDecodeError, TypeError):
            task_categories = []

        max_price = float(self._get("local_copy_trading.max_price_filter") or 0.85)
        min_ev = float(self._get("local_copy_trading.min_ev_filter") or 0.002)
        niche_only = bool(task.get("niche_only", 1))

        signals = 0
        latest_ts = last_ts

        for trade in new_trades:
            trade_ts = float(trade.get("timestamp", 0))
            price = float(trade.get("price", 0.5))
            # Data API 的 outcome 字段才是 YES/NO 方向（"Yes" 或 "No"）
            side = (trade.get("outcome") or "Yes").upper()
            condition_id = trade.get("conditionId", "")
            title = trade.get("title", "")
            slug = trade.get("slug", "")
            event_slug = trade.get("eventSlug", "")

            # 安全检查：排除超高价（风险收益比差）
            if price > max_price:
                continue
            # 安全检查：极低价 NO 另一面 YES> max_price
            if side == "NO" and (1.0 - price) > max_price:
                continue

            # 简单EV估算（copy trade 没有真实AI EV，用均价偏差估算）
            est_ev = abs(price - 0.5) * 0.1  # 保守估算
            if est_ev < min_ev:
                continue

            # 分类过滤（tag=null 时从 slug/title 关键词推断，再匹配 task_categories）
            if niche_only and task_categories:
                trade_tag = (trade.get("tag") or "").lower()
                if not trade_tag:
                    trade_tag = self._detect_category_from_content(slug, title)
                if trade_tag and not any(
                    c.lower() in trade_tag or trade_tag in c.lower()
                    for c in task_categories
                ):
                    continue

            # 计算下注金额（基础 × 赛道倍率）
            size_usdc = self._calc_bet_size(task, task_categories)

            # T298-D 安全硬上限：超出 hard_max_bet_usdc 的强制截断
            hard_max = float(self._get("paper_trading.hard_max_bet_usdc") or 25.0)
            size_usdc = min(size_usdc, hard_max)
            if size_usdc < 1.0:  # 极小金额无意义，跳过
                continue

            # 生成唯一 bet_id
            bet_id = self._make_bet_id(wallet, condition_id, side, trade_ts)

            # 推断分类
            category = self._infer_category(task_categories, trade)

            bet_record: dict[str, Any] = {
                "bet_id": bet_id,
                "ts_entered": datetime.now(timezone.utc).timestamp(),
                "market_id": condition_id,
                "question": title or f"Copy trade: {condition_id[:16]}",
                "category": category,
                "outcome": side,
                "confidence": 0.60,  # 跟单保守置信度
                "ai_probability": price if side == "YES" else 1.0 - price,
                "market_price": price,
                "ev": est_ev,
                "size_usdc": size_usdc,
                "market_slug": slug,
                "event_slug": event_slug,
                "condition_id": condition_id,
                "source": "copy_trade",
                "strategy_tag": "copy_trade",
                "ai_reasoning": f"跟投钱包 {wallet[:10]}… 方向 {side} @ ${price:.3f}",
                "copy_source_wallet": wallet,
                "ai_models": "copy_trade",
                "bet_bucket": "CopyTrade",
                "market_end_date": trade.get("endDate") or trade.get("endDateIso", ""),
            }

            # T189-F: 计算买入手续费（跟投默认 taker 模式）
            _ct_shares = size_usdc / price if price > 0 else 0
            from src.utils.db import calc_pm_fee

            _ct_fee, _ct_fr = calc_pm_fee(_ct_shares, price, category, "taker")
            bet_record["entry_fee_usdc"] = _ct_fee
            bet_record["fee_rate"] = _ct_fr

            if self._insert_bet_safe(bet_record):
                signals += 1

        # 无论是否有信号，推进 last_seen_trade_ts 到所有 new_trades 的最大时间戳
        # 避免下一轮重复处理被过滤的已知交易（DEFECT FIX: latest_ts 独立于插入成功）
        max_seen_ts = max(
            (float(t.get("timestamp", 0)) for t in new_trades), default=last_ts
        )
        self._update_task_after_cycle(task["id"], max_seen_ts, signals, error_count=0)

        return signals

    # ── T140: 跟投实盘路径 ────────────────────────────────────────────

    def _get_candidate_trades(
        self, task: dict
    ) -> tuple[list[tuple[dict, float]], list[str], float]:
        """
        公共候选交易过滤逻辑（DRY RUN / 冻结 / 实盘均复用）。
        返回 (candidates, task_categories, max_seen_ts)。
        - candidates = [(trade_dict, size_usdc), ...]
        - max_seen_ts = 所有新BUY交易的最大时间戳（无论是否通过过滤），用于更新 last_seen_trade_ts
        """
        wallet = task["wallet_address"]
        last_ts = float(task.get("last_seen_trade_ts") or 0.0)
        lookback_min = int(self._get("local_copy_trading.trade_lookback_minutes") or 30)
        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff_ts = max(last_ts, now_ts - lookback_min * 60)

        raw_trades = self._fetch_wallet_trades(wallet)
        if not raw_trades:
            return [], [], last_ts

        # 只跟 BUY 单（Data API side 字段为 BUY/SELL）
        new_trades = [
            t
            for t in raw_trades
            if float(t.get("timestamp", 0)) > cutoff_ts
            and t.get("side", "").upper() == "BUY"
        ]
        if not new_trades:
            return [], [], last_ts

        # 计算所有 new_trades 的最大时间戳（无论是否通过后续过滤），作为 last_seen_trade_ts 的新值
        max_seen_ts = max(float(t.get("timestamp", 0)) for t in new_trades)

        try:
            task_categories: list[str] = json.loads(task.get("categories") or "[]")
        except (json.JSONDecodeError, TypeError):
            task_categories = []

        max_price = float(self._get("local_copy_trading.max_price_filter") or 0.85)
        min_ev = float(self._get("local_copy_trading.min_ev_filter") or 0.002)
        niche_only = bool(task.get("niche_only", 1))
        hard_max = float(self._get("paper_trading.hard_max_bet_usdc") or 25.0)

        candidates: list[tuple[dict, float]] = []
        for trade in new_trades:
            price = float(trade.get("price", 0.5))
            # outcome 字段才是 YES/NO 方向
            outcome = (trade.get("outcome") or "Yes").upper()
            if price > max_price:
                continue
            if outcome == "NO" and (1.0 - price) > max_price:
                continue
            est_ev = abs(price - 0.5) * 0.1
            if est_ev < min_ev:
                continue
            if niche_only and task_categories:
                trade_tag = (trade.get("tag") or "").lower()
                if not trade_tag:
                    trade_tag = self._detect_category_from_content(
                        trade.get("slug", ""), trade.get("title", "")
                    )
                if trade_tag and not any(
                    c.lower() in trade_tag or trade_tag in c.lower()
                    for c in task_categories
                ):
                    continue
            size_usdc = self._calc_bet_size(task, task_categories, trade_price=price)
            size_usdc = min(size_usdc, hard_max)
            if size_usdc < 1.0:
                continue
            candidates.append((trade, size_usdc))

        # ── T141-A: per-conditionId 去重 ─────────────────────────────────
        # 同一 conditionId 只保留时间戳最新的那笔，防止目标钱包拆单重复触发信号
        dedup: dict[str, tuple[dict, float]] = {}
        for trade, size_usdc in candidates:
            cid = trade.get("conditionId", "")
            existing = dedup.get(cid)
            if existing is None or float(trade.get("timestamp", 0)) > float(
                existing[0].get("timestamp", 0)
            ):
                dedup[cid] = (trade, size_usdc)
        candidates = list(dedup.values())

        # ── T142-A: category_max_per_event + 同市场去重（跨信号源约束）────────
        # copy_trade 信号必须与 weather/AI 信号共享同一套赛道上限，防止系统对同事件重复建仓
        from src.utils import db as _db

        _cat_event_cfg: dict = self._get("paper_trading.category_max_per_event") or {}
        _max_per_market = int(
            (self._get("paper_trading.max_per_market_positions") or 1)
        )
        filtered_candidates: list[tuple[dict, float]] = []
        for trade, size_usdc in candidates:
            cid = trade.get("conditionId", "")
            ev_slug = trade.get("eventSlug", "")
            trade_cat = self._infer_category(task_categories, trade)

            # 1. 同市场去重：已有该 conditionId 的未平仓仓位则跳过（跨信号源）
            if (
                cid
                and _db.count_open_entry_bets_for_market(cid, self._db_path)
                >= _max_per_market
            ):
                logger.info(
                    "[CopyTrade|MarketDedup] market=%s 已有开放仓位 ≥ %d，跳过",
                    cid[:14],
                    _max_per_market,
                )
                continue

            # 2. 同事件去重：category_max_per_event 约束（跨信号源，含 weather/AI 仓位）
            if ev_slug:
                _max_per_event = int(
                    _cat_event_cfg.get(trade_cat, _cat_event_cfg.get("default", 1))
                )
                _event_count = _db.count_open_bets_for_event(ev_slug, self._db_path)
                if _event_count >= _max_per_event:
                    logger.info(
                        "[CopyTrade|EventDedup] event=%s cat=%s 已有 %d 仓位 ≥ %d，跳过",
                        ev_slug[:40],
                        trade_cat,
                        _event_count,
                        _max_per_event,
                    )
                    continue

            filtered_candidates.append((trade, size_usdc))
        candidates = filtered_candidates

        # ── T167: 跟投专属同事件去重 ────────────────────────────────
        # 用户明确要求：无论跟投钱包买入多少次，同城市同市场同温度只跟一次
        # 此检查独立于全局 category_max_per_event，专门约束 copy_trade 源
        _ct_max_per_event = int(self._get("local_copy_trading.max_per_event") or 1)
        ct_filtered: list[tuple[dict, float]] = []
        for trade, size_usdc in candidates:
            ev_slug = trade.get("eventSlug", "")
            if ev_slug:
                _ct_event_count = _db.count_open_copy_bets_for_event(
                    ev_slug, self._db_path
                )
                if _ct_event_count >= _ct_max_per_event:
                    logger.info(
                        "[CopyTrade|CTEventDedup] event=%s 跟投已有 %d 仓位 ≥ %d，跳过",
                        ev_slug[:40],
                        _ct_event_count,
                        _ct_max_per_event,
                    )
                    continue
            ct_filtered.append((trade, size_usdc))
        candidates = ct_filtered

        return candidates, task_categories, max_seen_ts

    def _process_task_frozen(
        self, task: dict, reason: str = "runtime_blocked", gate: dict | None = None
    ) -> None:
        """
        实盘冻结模式：检测候选交易并记录 [冻结跳过-CopyTrade] 日志，
        不执行任何下单，不写 paper_bets。
        """
        candidates, task_categories, max_seen_ts = self._get_candidate_trades(task)
        reason_label = reason or "runtime_blocked"
        for trade, size_usdc in candidates:
            # outcome 字段是 YES/NO 方向（"Yes"/"No"），side 字段是 BUY/SELL 操作
            side = (trade.get("outcome") or "Yes").upper()
            price = float(trade.get("price", 0.5))
            question = (
                trade.get("title", "")
                or f"Copy trade: {trade.get('conditionId', '')[:16]}"
            )
            est_ev = abs(price - 0.5) * 0.1
            tag = self._infer_category(task_categories, trade)
            logger.info(
                "[冻结跳过-CopyTrade] reason=%s %s | outcome=%s buy_price=%.4f bet=$%.2f"
                " ev=%.4f conf=0.60 tag=%s",
                reason_label,
                question[:80],
                side,
                price,
                size_usdc,
                est_ev,
                tag,
            )
        if not candidates:
            wallet = str(task.get("wallet_address") or "")
            gate_msg = ""
            if gate:
                gate_msg = (
                    f" freeze={int(bool(gate.get('freeze_auto')))}"
                    f" auto={int(bool(gate.get('auto_trading_enabled')))}"
                )
            logger.info(
                "[冻结跳过-CopyTrade] reason=%s wallet=%s%s 无新候选交易",
                reason_label,
                wallet[:14],
                gate_msg,
            )
        # DEFECT FIX: 冻结模式也推进 last_seen_trade_ts，避免解冻后积压重投
        if candidates:
            self._update_task_after_cycle(task["id"], max_seen_ts, 0, error_count=0)

    def _process_task_live(self, task: dict) -> int:
        """
        实盘模式（非冻结）：
        候选交易 → resolve token_id → build Signal → executor.execute() → insert paper_bet。
        返回真正成功入场的信号数。
        SUPREME-1: 只有 CLOB 执行成功后才写 paper_bet，保证 DB ↔ 链上仓位一致。
        """
        if not self._executor:
            logger.warning("[CopyTrade] 实盘模式下 executor 未注入，跳过跟投执行")
            return 0

        candidates, task_categories, max_seen_ts = self._get_candidate_trades(task)
        if not candidates:
            return 0

        signals = 0

        for trade, size_usdc in candidates:
            trade_ts = float(trade.get("timestamp", 0))
            condition_id = trade.get("conditionId", "")
            # outcome 字段是 YES/NO 方向；asset 字段是 CLOB token_id（无需 Gamma API）
            side = (trade.get("outcome") or "Yes").upper()
            price = float(trade.get("price", 0.5))
            question = trade.get("title", "") or f"Copy trade: {condition_id[:16]}"
            tag = self._infer_category(task_categories, trade)
            wallet = task.get("wallet_address", "")

            # 直接从 Data API 的 asset 字段获取 token_id（无需额外 Gamma API 调用）
            token_id = trade.get("asset", "")
            if not token_id:
                logger.warning(
                    "[CopyTrade] asset(token_id) 缺失，跳过 %s %s",
                    condition_id[:14],
                    side,
                )
                continue

            # T189-FIX: 使用 CLOB orderbook 中间价作为 expected_price
            # 源钱包 trade["price"] 可能是高限价（如 0.99），不代表实际市场价
            # 使用 midpoint 确保 OrderExecutor 的 limit_price 接近真实成交价
            clob_mid = self._fetch_clob_mid_price(token_id)
            signal_expected_price = price  # fallback: 源钱包价格
            if clob_mid and 0.01 <= clob_mid <= 0.99:
                signal_expected_price = clob_mid
                if abs(clob_mid - price) > 0.05:
                    logger.info(
                        "[CopyTrade 🔄] 价格纠偏 %s: 源价=%.3f → CLOB中间价=%.3f",
                        condition_id[:14],
                        price,
                        clob_mid,
                    )

            # 构建 Signal 对象（使用纠偏后的 expected_price）
            signal = self._build_signal(
                task,
                trade,
                token_id,
                size_usdc,
                task_categories,
                override_expected_price=signal_expected_price,
            )

            # 调用 OrderExecutor 实盘下单
            try:
                result = self._executor.execute(signal)
            except Exception as exc:
                logger.warning(
                    "[CopyTrade ❌] %s | reason=%s outcome=%s buy_price=%.4f tag=%s",
                    question[:60],
                    exc,
                    side,
                    price,
                    tag,
                )
                continue

            if result.success:
                # T189-FIX: fill_price 优先使用 OrderExecutor 校准后的实际成交价
                # 源钱包 price 可能是高限价（0.99），绝不应作为最终 fallback
                fill_price = result.actual_price if result.actual_price > 0 else price
                # 二次防护：如果 result.actual_price 仍是限价级别(>=0.90)
                # 但 CLOB 中间价远低于此，使用 CLOB 中间价
                if fill_price >= 0.90 and clob_mid and 0.01 <= clob_mid < 0.80:
                    logger.warning(
                        "[CopyTrade] fill_price=%.4f 疑似限价，纠偏为 CLOB mid=%.4f",
                        fill_price,
                        clob_mid,
                    )
                    fill_price = clob_mid
                bet_id = self._make_bet_id(wallet, condition_id, side, trade_ts)

                # T169: 使用实际CLOB成交金额而非策略预算金额
                # 避免 amplification 导致 size_usdc 失真（如 $0.88→$1.91），影响 P&L 计算
                try:
                    _filled_size = float(getattr(result, "filled_size", 0) or 0)
                except (TypeError, ValueError):
                    _filled_size = 0.0
                actual_cost = _filled_size if _filled_size > 0 else size_usdc

                # T189-E: 交叉验证 — 确保 size_usdc 与 price*shares 一致
                # 根因：filled_size 可能返回探单金额(近零)或策略预算(偏高)
                _matched = float(getattr(result, "matched_shares", 0) or 0)
                if fill_price > 0 and _matched > 0:
                    _calc_cost = round(fill_price * _matched, 4)
                    if (
                        actual_cost < 0.01
                        or abs(actual_cost - _calc_cost) > _calc_cost * 0.10
                    ):
                        logger.warning(
                            "[CopyTrade] size_usdc交叉验证修正: "
                            "filled=%.4f → calc=%.4f (price=%.4f × shares=%.4f)",
                            actual_cost,
                            _calc_cost,
                            fill_price,
                            _matched,
                        )
                        actual_cost = _calc_cost

                # T169: 余额快照 — 使用实盘 USDC 余额（下单后）
                _bal_after: float | None = None
                _bal_before: float | None = None
                try:
                    from src.utils.balance_provider import get_available_balance

                    _bal_after = get_available_balance()
                    if _bal_after is not None and actual_cost > 0:
                        _bal_before = round(_bal_after + actual_cost, 2)
                except Exception:
                    pass  # 余额查询失败不阻塞入场

                bet_record: dict[str, Any] = {
                    "bet_id": bet_id,
                    "ts_entered": datetime.now(timezone.utc).timestamp(),
                    "market_id": condition_id,
                    "question": question[:500],
                    "category": tag,
                    "outcome": side,
                    "confidence": 0.60,
                    "ai_probability": price if side == "YES" else 1.0 - price,
                    "market_price": fill_price,
                    "ev": signal.ev,
                    "size_usdc": actual_cost,
                    "sim_balance_before": _bal_before,
                    "sim_balance_after": _bal_after,
                    "market_slug": trade.get("slug", ""),
                    "event_slug": trade.get("eventSlug", ""),
                    "condition_id": condition_id,
                    "matched_shares": (
                        _matched
                        if _matched > 0
                        else (
                            round(actual_cost / fill_price, 6) if fill_price > 0 else 0
                        )
                    ),
                    "source": "copy_trade",
                    "strategy_tag": "copy_trade",
                    "ai_reasoning": (
                        f"跟投钱包 {wallet[:10]}… 方向 {side} @ ${price:.3f}"
                        f" | tx={str(result.tx_hash)[:20]}"
                    ),
                    "copy_source_wallet": wallet,
                    "ai_models": "copy_trade",
                    "bet_bucket": "CopyTrade",
                    "market_end_date": trade.get("endDate")
                    or trade.get("endDateIso", ""),
                }
                if self._insert_bet_safe(bet_record):
                    signals += 1
                tx_short = str(result.tx_hash)[:10] if result.tx_hash else "N/A"
                logger.info(
                    "[CopyTrade ✅] %s | outcome=%s buy_price=%.4f bet=$%.2f"
                    " ev=%.4f conf=0.60 tag=%s tx=%s…",
                    question[:60],
                    side,
                    fill_price,
                    size_usdc,
                    signal.ev,
                    tag,
                    tx_short,
                )
            else:
                logger.warning(
                    "[CopyTrade ❌] %s | reason=%s outcome=%s buy_price=%.4f tag=%s",
                    question[:60],
                    result.error or "未知",
                    side,
                    price,
                    tag,
                )

        # 无论 CLOB 成功与否，推进 last_seen_trade_ts 到所有 candidates 的 max_seen_ts
        # 避免网络抖动时 CLOB 失败使同一交易下一轮仍被重试（DEFECT FIX）
        if candidates:
            self._update_task_after_cycle(
                task["id"], max_seen_ts, signals, error_count=0
            )

        return signals

    def _resolve_token_ids(self, condition_id: str) -> tuple[str, str]:
        """
        从 Polymarket Gamma API 获取 condition_id 对应的 YES/NO token IDs。
        返回 (yes_token_id, no_token_id)；失败返回 ("", "")。
        clobTokenIds 是 JSON 字符串，index 0=YES, index 1=NO。
        """
        if not condition_id:
            return ("", "")
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"condition_ids": condition_id},
                timeout=8,
            )
            resp.raise_for_status()
            markets = resp.json()
            if not markets:
                return ("", "")
            raw = markets[0].get("clobTokenIds") or "[]"
            tokens = (
                json.loads(raw)
                if isinstance(raw, str)
                else (raw if isinstance(raw, list) else [])
            )
            if len(tokens) >= 2:
                return (str(tokens[0]), str(tokens[1]))
            logger.warning(
                "[CopyTrade] clobTokenIds 不完整 (%d): %s",
                len(tokens),
                condition_id[:14],
            )
            return ("", "")
        except Exception as exc:
            logger.warning(
                "[CopyTrade] _resolve_token_ids 失败 %s: %s", condition_id[:14], exc
            )
            return ("", "")

    def _build_signal(
        self,
        task: dict,
        trade: dict,
        token_id: str,
        size_usdc: float,
        task_categories: list[str],
        override_expected_price: float | None = None,
    ):
        """从跟投交易数据构建 Signal 对象，供 OrderExecutor 使用。"""
        from src.signal.signal_engine import Signal

        condition_id = trade.get("conditionId", "")
        # outcome 字段= "Yes"/"No"，side 字段= "BUY"/"SELL"
        side = (trade.get("outcome") or "Yes").upper()
        price = float(trade.get("price", 0.5))
        question = trade.get("title", "") or f"Copy trade: {condition_id[:16]}"
        est_ev = abs(price - 0.5) * 0.1
        wallet = task.get("wallet_address", "")

        # T189-FIX: 优先使用 CLOB 纠偏后的 expected_price
        expected_price = override_expected_price if override_expected_price else price

        return Signal(
            market_id=condition_id,
            question=question[:500],
            outcome=side,
            ev=est_ev,
            confidence=0.60,
            size_usdc=size_usdc,
            token_id=token_id,
            expected_price=expected_price,
            condition_id=condition_id,
            strategy_tag="copy_trade",
            model_votes={
                "copy_trade": {
                    "probability": price if side == "YES" else 1.0 - price,
                    "outcome": side,
                    "confidence": 0.60,
                    "reasoning": f"跟投钱包 {wallet[:10]}… 方向 {side} @ ${price:.3f}",
                }
            },
        )

    # ── T164: 智能跟卖 ──────────────────────────────────────────────

    def _monitor_copy_sells(self) -> None:
        """
        监控原始跟投钱包的 SELL 交易，匹配我方持仓后跟随卖出。

        逻辑：
        1. 查询所有 copy_source_wallet IS NOT NULL 且未平仓的 paper_bets
        2. 按钱包分组，对每个钱包拉取交易
        3. 过滤出 side="SELL" 的交易
        4. 用 conditionId 与我方持仓匹配
        5. 盈亏判断后决定跟卖还是交给系统管理
        """
        follow_sell_cfg = self._get("local_copy_trading.follow_sell") or {}
        if not follow_sell_cfg.get("enabled", True):
            return

        global_gate = self._runtime_gate_for_wallet("")
        if global_gate["maintenance_mode"] or global_gate["emergency_freeze"]:
            logger.info(
                "[CopyTrade] 跳过跟卖监控: reason=%s",
                global_gate["reason"] or "runtime_blocked",
            )
            return

        from src.utils import db as _db
        from src.utils.config_loader import is_dry_run as _is_dry_run

        # 查询所有来源于跟投且仍在持仓中的 paper_bets
        open_copy_bets = self._get_open_copy_bets()
        if not open_copy_bets:
            return

        # 按钱包分组
        wallet_bets: dict[str, list[dict]] = {}
        for bet in open_copy_bets:
            w = bet.get("copy_source_wallet", "")
            if w:
                wallet_bets.setdefault(w, []).append(bet)

        min_profit_pct = float(follow_sell_cfg.get("min_profit_pct", -0.05))

        for wallet, bets in wallet_bets.items():
            wallet_gate = self._runtime_gate_for_wallet(
                wallet,
                maintenance_mode=global_gate["maintenance_mode"],
                emergency_freeze=global_gate["emergency_freeze"],
            )
            if wallet_gate["blocked"]:
                logger.info(
                    "[CopyTrade] 跳过跟卖钱包 %s: reason=%s",
                    wallet[:14],
                    wallet_gate["reason"],
                )
                continue

            raw_trades = self._fetch_wallet_trades(wallet)
            if not raw_trades:
                continue

            # 筛选 SELL 交易
            sell_trades = [t for t in raw_trades if t.get("side", "").upper() == "SELL"]
            if not sell_trades:
                continue

            # 按 conditionId 索引
            sell_by_cid: dict[str, dict] = {}
            for t in sell_trades:
                cid = t.get("conditionId", "")
                if cid:
                    # 取最新的 SELL 交易
                    existing = sell_by_cid.get(cid)
                    if existing is None or float(t.get("timestamp", 0)) > float(
                        existing.get("timestamp", 0)
                    ):
                        sell_by_cid[cid] = t

            # 匹配我方持仓
            for bet in bets:
                cid = bet.get("condition_id", "")
                if cid not in sell_by_cid:
                    continue

                sell_trade = sell_by_cid[cid]
                sell_ts = float(sell_trade.get("timestamp", 0))
                enter_ts_raw = bet.get("ts_entered", "")

                # 确保 SELL 交易时间晚于我方入场时间（避免误匹配历史卖出）
                try:
                    if isinstance(enter_ts_raw, (int, float)):
                        enter_ts = float(enter_ts_raw)
                    else:
                        enter_dt = datetime.fromisoformat(str(enter_ts_raw))
                        if enter_dt.tzinfo is None:
                            enter_dt = enter_dt.replace(tzinfo=timezone.utc)
                        enter_ts = enter_dt.timestamp()
                except (ValueError, TypeError):
                    enter_ts = 0.0
                if sell_ts <= enter_ts:
                    continue

                # T176-2: 交易新鲜度过滤 — 跳过太旧的卖出信号
                # 源钱包卖出后如果过了太长时间，价格已剧变，跟卖毫无意义
                _max_sell_age_sec = float(
                    (follow_sell_cfg.get("max_sell_age_seconds") or 300)
                )
                _now_ts = datetime.now(timezone.utc).timestamp()
                if _now_ts - sell_ts > _max_sell_age_sec:
                    logger.debug(
                        "[CopyTrade] 跳过过期卖出信号 %s (age=%.0fs > %.0fs)",
                        cid[:14],
                        _now_ts - sell_ts,
                        _max_sell_age_sec,
                    )
                    continue

                self._handle_copy_sell_signal(
                    bet, sell_trade, min_profit_pct, _is_dry_run()
                )

    def _handle_copy_sell_signal(
        self,
        bet: dict,
        sell_trade: dict,
        min_profit_pct: float,
        dry_run: bool,
    ) -> None:
        """
        处理单个跟卖信号：计算盈亏 → 决定跟卖或系统管理。

        - 盈利 / 亏损在容忍范围内 → 执行跟卖
        - 亏损超出容忍 → 记录日志，交给 OutcomeTracker 系统止损
        """
        from src.utils import db as _db

        bet_id = bet.get("bet_id", "")
        entry_price = float(bet.get("market_price") or 0.5)
        size_usdc = float(bet.get("size_usdc") or 0)
        outcome = (bet.get("outcome") or "YES").upper()
        question = bet.get("question", "")[:60]
        wallet = bet.get("copy_source_wallet", "")[:10]
        source_sell_price = float(sell_trade.get("price", 0))

        # ── T176-2: 使用实时市场价而非源钱包历史成交价 ──
        # 源钱包卖出后价格可能已剧变（特别是低流动性天气市场）
        # 优先用 CLOB 当前 best_bid 做 P&L 决策
        token_id = bet.get("token_id", "")
        current_bid = self._fetch_current_best_bid(token_id)
        if current_bid is not None and current_bid > 0:
            sell_price = current_bid
            if abs(sell_price - source_sell_price) > 0.02:
                logger.info(
                    "[CopyTrade 🔄] 实时价格纠偏 %s: 源价=%.3f → 市场bid=%.3f",
                    bet_id[:8],
                    source_sell_price,
                    sell_price,
                )
        else:
            sell_price = source_sell_price

        # ── 双边手续费估算（Polymarket 官方公式: fee = shares × feeRate × p × (1-p)）
        category = bet.get("category", "weather")
        _taker_rates = self._get("paper_trading.taker_fee_rates") or {}
        _fee_rate = float(
            _taker_rates.get(category, 0)
            or self._get("paper_trading.taker_fee_rate_default")
            or 0.05
        )
        _buy_fee_per_share = (
            _fee_rate * entry_price * (1 - entry_price) if 0 < entry_price < 1 else 0
        )
        _sell_fee_per_share = (
            _fee_rate * sell_price * (1 - sell_price) if 0 < sell_price < 1 else 0
        )
        _total_fee_per_share = _buy_fee_per_share + _sell_fee_per_share

        # 费用调整后的盈亏计算
        shares = size_usdc / entry_price if entry_price > 0 else 0
        gross_proceeds = round(shares * sell_price, 4)
        estimated_fees = round(shares * _total_fee_per_share, 4)
        proceeds = round(gross_proceeds - estimated_fees, 4)
        pnl = round(proceeds - size_usdc, 4)

        if size_usdc > 0:
            return_pct = pnl / size_usdc
        else:
            return_pct = 0.0

        # 决策：是否跟卖
        # 镜像模式 (unconditional=true)：无条件跟随源钱包卖出
        # 利润门控模式 (unconditional=false)：仅盈利或亏损在容忍范围内时跟卖
        _fs_cfg = self._get("local_copy_trading.follow_sell") or {}
        unconditional = _fs_cfg.get("unconditional", True)
        if unconditional or return_pct >= min_profit_pct:
            # 盈利或亏损在容忍范围内 → 跟卖
            if dry_run:
                # DRY RUN: 直接关闭 DB 记录
                if pnl >= 0:
                    _db.close_paper_bet_profit_take(
                        bet_id=bet_id,
                        exit_price=sell_price,
                        pnl=pnl,
                        payout=proceeds,
                        db_path=self._db_path,
                    )
                else:
                    _db.close_paper_bet_stop_loss(
                        bet_id=bet_id,
                        exit_price=sell_price,
                        pnl=pnl,
                        payout=proceeds,
                        db_path=self._db_path,
                    )
                logger.info(
                    "[CopyTrade 🔄] DRY RUN 跟卖 %s | wallet=%s… "
                    "entry=%.3f exit=%.3f fee=%.4f P&L=%+.4f (%+.1f%%)",
                    question,
                    wallet,
                    entry_price,
                    sell_price,
                    estimated_fees,
                    pnl,
                    return_pct * 100,
                )
            else:
                # 实盘: 调用 executor.sell_position()
                if not self._executor:
                    logger.warning(
                        "[CopyTrade 🔄] 实盘跟卖但 executor 未注入，跳过 %s",
                        bet_id[:8],
                    )
                    return
                sell_bet = {
                    "market_id": bet.get("market_id", ""),
                    "outcome": outcome,
                    "exit_price": sell_price,
                    "size_usdc": size_usdc,
                    "market_price": entry_price,
                    "condition_id": bet.get("condition_id", ""),
                    "question": question,
                    "trigger": "copy_follow_sell",
                    "bet_id": bet_id,
                    "category": category,  # 确保 sell_mode per_category 规则生效
                }
                try:
                    result = self._executor.sell_position(sell_bet)
                except Exception as exc:
                    logger.warning(
                        "[CopyTrade 🔄❌] 跟卖执行异常 %s: %s", bet_id[:8], exc
                    )
                    return

                if result.success:
                    # 使用 executor 实际到账金额（余额差，已扣平台费用）
                    actual_proceeds = getattr(result, "filled_size", 0.0) or proceeds
                    actual_exit = (
                        getattr(result, "actual_price", sell_price) or sell_price
                    )
                    real_pnl = round(actual_proceeds - size_usdc, 4)
                    if real_pnl >= 0:
                        _db.close_paper_bet_profit_take(
                            bet_id=bet_id,
                            exit_price=actual_exit,
                            pnl=real_pnl,
                            payout=actual_proceeds,
                            db_path=self._db_path,
                        )
                    else:
                        _db.close_paper_bet_stop_loss(
                            bet_id=bet_id,
                            exit_price=actual_exit,
                            pnl=real_pnl,
                            payout=actual_proceeds,
                            db_path=self._db_path,
                        )
                    logger.info(
                        "[CopyTrade 🔄✅] 跟卖成功 %s | wallet=%s… "
                        "entry=%.3f exit=%.3f 到账=$%.4f P&L=%+.4f",
                        question,
                        wallet,
                        entry_price,
                        actual_exit,
                        actual_proceeds,
                        real_pnl,
                    )
                else:
                    # SUPREME-1: 卖出失败不关闭 DB，等待下次重试
                    logger.warning(
                        "[CopyTrade 🔄❌] 跟卖失败 %s | %s → 下轮重试",
                        bet_id[:8],
                        result.error or "未知",
                    )
        else:
            # 亏损超出容忍范围 → 不跟卖，交给系统止损管理
            logger.info(
                "[CopyTrade ⏸️] 跟卖信号忽略(亏损) %s | wallet=%s… "
                "entry=%.3f exit=%.3f loss=%+.1f%% > 容忍%.0f%% → 系统管理",
                question,
                wallet,
                entry_price,
                sell_price,
                return_pct * 100,
                min_profit_pct * 100,
            )

    def _get_open_copy_bets(self) -> list[dict]:
        """查询所有来源于跟投且仍在持仓中的 paper_bets。"""
        try:
            with _pg_connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM paper_bets "
                    "WHERE copy_source_wallet IS NOT NULL "
                    "AND resolved_outcome IS NULL "
                    "ORDER BY ts_entered"
                ).fetchall()
                return [dict(r) for r in rows]
        except OperationalError:
            # column 尚未迁移时安全降级
            return []

    # ── 原 DRY RUN 辅助方法 ───────────────────────────────────────────

    def _fetch_wallet_trades(self, wallet: str) -> list[dict]:
        """
        从 Polymarket Data API 获取钱包近期活动中的交易记录。

        ⚠️ T140追加续-BUG修复: /trades 端点有约 3 小时数据延迟！
        必须使用 /activity 端点获取实时交易数据，
        并过滤 type=="TRADE" 以排除 REWARD 等非交易记录。
        """
        try:
            resp = requests.get(
                f"{_DATA_API}/activity",
                params={"user": wallet, "limit": 50},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data if isinstance(data, list) else data.get("data", [])
            # 仅保留买卖交易（排除 REWARD / REDEMPTION 等活动类型）
            return [t for t in raw if t.get("type", "TRADE") == "TRADE"]
        except requests.RequestException as exc:
            logger.warning("[CopyTrade] 获取钱包 %s 活动失败: %s", wallet[:14], exc)
            return []

    @staticmethod
    def _fetch_current_best_bid(token_id: str) -> float | None:
        """从 CLOB orderbook 获取指定 token 的当前最佳 bid 价格。

        T176-2: 跟卖决策必须使用实时市场价而非源钱包历史成交价。
        源钱包卖出后价格可能发生剧烈变化，用历史价计算 P&L 会导致误判。

        Returns:
            最佳 bid 价格（float），失败返回 None
        """
        if not token_id:
            return None
        try:
            resp = requests.get(
                f"{_CLOB_API}/book",
                params={"token_id": token_id},
                timeout=8,
            )
            resp.raise_for_status()
            book = resp.json()
            bids = book.get("bids", [])
            if bids:
                return float(bids[0].get("price", 0))
            return None
        except Exception as exc:
            logger.debug("[CopyTrade] 获取 orderbook 失败 %s: %s", token_id[:16], exc)
            return None

    @staticmethod
    def _fetch_clob_mid_price(token_id: str) -> float | None:
        """从 CLOB orderbook 获取中间价（用于跟投信号的 expected_price）。

        T189-FIX: 源钱包的 trade["price"] 可能是其 taker 限价单价格（最高 0.99），
        不代表当前市场公允价格。使用 (best_bid + best_ask)/2 作为 expected_price，
        确保 OrderExecutor 的 limit_price 接近实际成交价而非被限价膨胀。

        Fallback 策略:
          1. 有 bid+ask → midpoint
          2. 只有 bid → bid + 0.01
          3. 只有 ask → ask
          4. 都没有 → None（由调用方 fallback）

        Returns:
            估算市场价（float），失败返回 None
        """
        if not token_id:
            return None
        try:
            resp = requests.get(
                f"{_CLOB_API}/book",
                params={"token_id": token_id},
                timeout=8,
            )
            resp.raise_for_status()
            book = resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0].get("price", 0)) if bids else 0
            best_ask = float(asks[0].get("price", 0)) if asks else 0

            if best_bid > 0 and best_ask > 0:
                return round((best_bid + best_ask) / 2, 4)
            if best_bid > 0:
                return round(best_bid + 0.01, 4)
            if best_ask > 0:
                return best_ask
            return None
        except Exception as exc:
            logger.debug(
                "[CopyTrade] 获取 orderbook mid 失败 %s: %s", token_id[:16], exc
            )
            return None

    def _calc_bet_size(
        self, task: dict, categories: list[str], trade_price: float = 0.5
    ) -> float:
        """计算跟投下注金额（基础额 × 动态价格缩放 × 赛道倍率）。

        T141-D: 当 local_copy_trading.dynamic_usdc_enabled=true 时，
        按买入价阶梯缩放 base_usdc，避免低价市场固定投入过多资金：
          scaled = base * min(1.0, trade_price / scale_anchor)
          保底：max(scaled, trade_price * min_shares)  — 确保买到最小份数
          上限：不超过任务配置的 base_usdc 上限
        """
        max_usdc = float(
            task.get("base_usdc")
            or self._get("local_copy_trading.default_base_usdc")
            or 10.0
        )

        # ── T141-D: 动态 USDC 缩放 ──────────────────────────────────────
        dynamic_setting = self._get("local_copy_trading.dynamic_usdc_enabled")
        dynamic_enabled = bool(dynamic_setting) if dynamic_setting is not None else True
        if dynamic_enabled and 0.0 < trade_price < 1.0:
            scale_anchor = float(
                self._get("local_copy_trading.dynamic_usdc_scale_anchor") or 0.50
            )
            min_shares = int(
                self._get("local_copy_trading.dynamic_usdc_min_shares") or 5
            )
            # 线性缩放：price >= scale_anchor → 满额；price < scale_anchor → 按比例缩减
            scale_factor = min(1.0, trade_price / scale_anchor)
            scaled = max_usdc * scale_factor
            # 保底：至少能买到 min_shares 份（忽略微小手续费影响）
            floor_usdc = trade_price * min_shares
            base = max(scaled, floor_usdc)
            base = min(base, max_usdc)  # 不超过任务配置上限
        else:
            base = max_usdc

        # ── T189-FIX: 确保不低于 CLOB 最小下单额 ──────────────────────
        # 当 trade_price 低时，price × min_shares 可能 < min_bet_usdc，
        # 导致 OrderExecutor.execute() 因 "余额不足" 直接拒绝（在 amplification 之前）
        _min_bet = float(self._get("executor.min_bet_usdc") or 2.0)
        base = max(base, _min_bet)
        base = min(base, max_usdc)  # 仍不超过任务配置上限

        # ── 赛道倍率 ────────────────────────────────────────────────────
        multipliers: dict = (
            self._get("local_copy_trading.category_size_multiplier") or {}
        )
        if not categories:
            return round(base, 2)
        for cat in categories:
            multiplier = float(multipliers.get(cat.lower(), 1.0))
            if multiplier != 1.0:
                return round(base * multiplier, 2)
        return round(base, 2)

    def _infer_category(self, task_categories: list[str], trade: dict) -> str:
        """从任务分类列表推断 category 字符串。"""
        if task_categories:
            return task_categories[0].lower()
        tag = (trade.get("tag") or "").lower()
        if tag:
            return tag
        # tag=null 时从 slug/title 关键词推断
        detected = self._detect_category_from_content(
            trade.get("slug", ""), trade.get("title", "")
        )
        return detected or "copy_trade"

    @staticmethod
    def _detect_category_from_content(slug: str, title: str) -> str:
        """
        从 slug 和 title 中提取赛道关键词，返回标准化赛道名。
        Data API 中 tag=null 时作为分类过滤的备用逻辑。
        """
        text = f"{slug} {title}".lower()
        if any(
            kw in text
            for kw in (
                "temperature",
                "rain",
                "snow",
                "celsius",
                "fahrenheit",
                "humidity",
                "wind",
                "weather",
                "storm",
                "flood",
                "cloud",
                "sunny",
                "drought",
            )
        ):
            return "weather"
        if any(
            kw in text
            for kw in (
                "bitcoin",
                "btc",
                "ethereum",
                "eth",
                "crypto",
                "solana",
                "sol",
                "doge",
                "xrp",
                "token",
            )
        ):
            return "crypto"
        if any(
            kw in text
            for kw in (
                "nba",
                "nfl",
                "mlb",
                "nhl",
                "soccer",
                "football",
                "basketball",
                "baseball",
                "tennis",
                "match",
                "serie",
                "premier",
                "champions",
                "league",
                "team",
            )
        ):
            return "sports"
        if any(
            kw in text
            for kw in (
                "election",
                "vote",
                "president",
                "congress",
                "senator",
                "trump",
                "biden",
                "political",
            )
        ):
            return "politics"
        return ""

    def _make_bet_id(self, wallet: str, condition_id: str, side: str, ts: float) -> str:
        """生成幂等 bet_id（相同 wallet+condition+side+ts 始终得相同ID）。"""
        raw = f"copy_{wallet}_{condition_id}_{side}_{ts}"
        return "copy_" + hashlib.md5(raw.encode()).hexdigest()[:16]  # noqa: S324

    def _insert_bet_safe(self, bet: dict) -> bool:
        """
        插入 paper_bets（INSERT OR IGNORE 天然幂等）。
        返回 True = 真正插入了新记录；False = 已存在跳过。
        """
        from src.utils import db

        try:
            before = self._count_bets(bet["bet_id"])
            db.insert_paper_bet(bet, self._db_path)
            after = self._count_bets(bet["bet_id"])
            if after > before:
                logger.info(
                    "[CopyTrade] ✅ 新跟投信号: %s %s @ %.3f",
                    bet["outcome"],
                    bet["question"][:40],
                    bet["market_price"],
                )
                return True
            return False
        except Exception as exc:
            logger.error("[CopyTrade] 插入 paper_bet 失败: %s", exc)
            return False

    def _count_bets(self, bet_id: str) -> int:
        with _pg_connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_bets WHERE bet_id=?", (bet_id,)
            ).fetchone()
            return row["c"] if row else 0

    def _update_task_after_cycle(
        self, task_id: int, last_ts: float, new_signals: int, error_count: int
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with _pg_connect() as conn:
            conn.execute(
                """UPDATE copy_tasks
                   SET last_active_at=?, last_seen_trade_ts=?,
                       total_signals=total_signals+?, error_count=?
                   WHERE id=?""",
                (now_iso, last_ts, new_signals, error_count, task_id),
            )

    def _increment_error(self, task_id: int, error_msg: str) -> None:
        max_errors = int(self._get("local_copy_trading.max_error_count") or 3)
        with _pg_connect() as conn:
            row = conn.execute(
                "SELECT error_count FROM copy_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                return
            new_count = (row["error_count"] or 0) + 1
            new_status = "paused" if new_count >= max_errors else "running"
            conn.execute(
                "UPDATE copy_tasks SET error_count=?, last_error=?, status=? WHERE id=?",
                (new_count, error_msg[:500], new_status, task_id),
            )
        if new_count >= max_errors:
            logger.warning(
                "[CopyTrade] ⚠️ 任务 id=%s 连续错误 %d 次，已自动暂停",
                task_id,
                new_count,
            )
            self._send_telegram_alert(task_id, error_msg)

    def _send_telegram_alert(self, task_id: int, error_msg: str) -> None:
        """尝试发送 Telegram 告警（失败不影响主流程）。"""
        try:
            from src.utils.telegram_notifier import send_message  # optional dep

            send_message(
                f"⚠️ 跟投任务 id={task_id} 已被自动暂停\n错误：{error_msg[:200]}"
            )
        except Exception:
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
            pass  # Telegram 模块不存在或发送失败，不影响主流程
