"""
paper_trader.py — 模拟投注引擎
职责：
  - 用独立的模拟账户（100 USDC）运行完整的 scan → signal → record 流程
  - 多赛道支持：天气（ColdMath双桶）、体育（EV驱动）、加密、地缘
  - 与实盘完全物理隔离：使用 paper_bets 表，不调用任何真实下单接口
  - 内置 4 小时 AI 信号缓存，控制 DeepSeek API 费用
"""

import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.scanner.market_scanner import MarketScanner
from src.signal.signal_engine import Signal, SignalEngine
from src.simulation.outcome_tracker import OutcomeTracker
from src.utils import db
from src.utils.category_detector import detect_category
from src.utils.config_loader import get
from src.utils.weather_fetcher import is_threshold_weather_question, is_weather_market

logger = logging.getLogger(__name__)


class PaperTrader:
    """
    模拟投注引擎。
    线程不安全（单线程运行），按 run_cycle() 触发。
    """

    def __init__(self, db_path=None) -> None:
        self.scanner = MarketScanner()
        # 自我进化系统
        from src.evolution.strategy_evolver import StrategyEvolver

        self.evolver = StrategyEvolver(db_path=db_path)
        self.signal_engine = SignalEngine(evolver=self.evolver)
        self.outcome_tracker = OutcomeTracker()
        # AI 信号缓存: {market_id: (timestamp, Signal)}
        self._signal_cache: dict[str, tuple[float, Signal]] = {}
        # 每市场最近已记录的价格，用于重入判断
        self._last_price: dict[str, float] = {}
        # 多轮价格历史缓存: {market_id: [(timestamp, price), ...]}
        # 用于多tick动量检测，替代单次快照
        self._price_history: dict[str, list[tuple[float, float]]] = {}
        self._cycle_count = 0
        # db_path: None = 使用 db._DB_PATH（生产默认值）
        self._db_path = db_path

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run_cycle(self, scan_new: bool = True) -> int:
        """
        执行一轮模拟投注：扫描天气市场 → 生成信号 → 记录投注。

        Args:
            scan_new: True = 完整轮次（扫描+AI信号+入场判断）；
                      False = 仅检查现有仓位止盈/止损，不扫描新市场（快速监控模式）。

        返回：本轮新增的模拟投注数量。
        """
        self._cycle_count += 1
        logger.info(
            f"[PaperTrader] 第 {self._cycle_count} 轮开始"
            + (" [仅持仓监控]" if not scan_new else "")
        )

        # 从 config 读取参数
        pt_cfg = get("paper_trading") or {}
        weather_only: bool = pt_cfg.get("weather_only", True)
        # 各赛道独立EV门槛（统一从category_min_ev字典取值，default=1.0阻塞未配置赛道）
        cat_ev_cfg: dict = pt_cfg.get("category_min_ev") or {}
        _default_min_ev: float = float(cat_ev_cfg.get("default", 1.0))
        max_per_market: int = pt_cfg.get("max_per_market_positions", 2)
        cache_hours: float = pt_cfg.get("signal_cache_hours", 4.0)
        reentry_pct: float = pt_cfg.get("reentry_min_price_change", 0.05)

        # ── ColdMath双桶参数 ─────────────────────────────────────────
        # Bucket B (Edge/Asymmetric): price ≤ edge_max → 小仓，等大幅价格修正
        edge_max: float = float(pt_cfg.get("weather_entry_max_price", 1.0))
        # Bucket A (Conviction): price ≥ conviction_min → 大仓，确定性小收益
        conviction_min: float = float(pt_cfg.get("weather_conviction_min_price", 0.82))
        conviction_min_ev: float = float(pt_cfg.get("weather_conviction_min_ev", 0.02))
        conviction_max_size: float = float(
            pt_cfg.get("weather_conviction_max_size", 10.0)
        )
        conviction_min_ai_prob: float = float(
            pt_cfg.get("weather_conviction_min_ai_prob", 0.85)
        )

        # ── 全赛道高确定性策略参数（如 NO@0.89 的"几乎确定"市场）──────
        hc_cfg: dict = pt_cfg.get("high_conviction", {})
        hc_enabled: bool = hc_cfg.get("enabled", False)
        hc_min_prob: float = float(hc_cfg.get("min_ai_probability", 0.85))
        hc_min_ev: float = float(hc_cfg.get("min_ev_threshold", 0.01))
        hc_min_volume: float = float(hc_cfg.get("min_volume_usd", 100000))

        # 当前模拟余额
        sim_balance = self._get_sim_balance()
        logger.info(f"[PaperTrader] 模拟余额: {sim_balance:.2f} USDC")

        # 检查模拟账户是否还有余额
        if sim_balance < 1.0:
            logger.warning("[PaperTrader] 模拟余额不足 1 USDC，本轮跳过")
            return 0

        # ── 余额保底检查 ─────────────────────────────────────
        # T190: 简化为绝对底线，删除 balance_reserve_pct 百分比保留
        _min_reserve = float(pt_cfg.get("balance_reserve_min_usdc", 0.10))
        _reserve_triggered = sim_balance < _min_reserve

        # 当前未解决的开仓数
        open_bets = db.get_open_paper_bets(self._db_kw())

        # ━━━ 在寻找新机会之前，先检查现有仓位是否应止盈/止损平仓 ━━━
        if open_bets:
            # 集中获取一次市场数据，传递给止盈/止损/仓位管理，避免 3× 重复 API 请求
            _all_market_ids = list({b["market_id"] for b in open_bets})
            _pt_slug_map = {
                b["market_id"]: b.get("market_slug", "")
                for b in open_bets
                if b.get("market_slug")
            }
            _cached_market_data = self.outcome_tracker._fetch_market_statuses(
                _all_market_ids, slug_map=_pt_slug_map
            )

            try:
                exits = self.outcome_tracker.check_profit_take_exits(
                    open_bets, market_data=_cached_market_data
                )
                for exit_bet in exits:
                    self.outcome_tracker.close_position_profit_take(
                        exit_bet, self._db_kw()
                    )
                if exits:
                    # 平仓后重新读取开仓列表
                    open_bets = db.get_open_paper_bets(self._db_kw())
                    logger.info(
                        f"[PaperTrader] 止盈平仓: {len(exits)} 笔，当前开仓: {len(open_bets)} 笔"
                    )
            except Exception as e:
                logger.warning(f"[PaperTrader] 止盈检查异常(不影响投注流程): {e}")

            try:
                stops = self.outcome_tracker.check_stop_loss_exits(
                    open_bets, market_data=_cached_market_data
                )
                for stop_bet in stops:
                    self.outcome_tracker.close_position_stop_loss(
                        stop_bet, self._db_kw()
                    )
                if stops:
                    open_bets = db.get_open_paper_bets(self._db_kw())
                    logger.info(
                        f"[PaperTrader] 止损平仓: {len(stops)} 笔，当前开仓: {len(open_bets)} 笔"
                    )
            except Exception as e:
                logger.warning(f"[PaperTrader] 止损检查异常(不影响投注流程): {e}")

        # 仅持仓监控模式：止盈/止损检查后，再检查加仓/部分止盈，不扫描新市场
        if not scan_new:
            # ── 持仓管理：加仓 / 部分止盈 ───────────────────────────
            if open_bets:
                try:
                    self.outcome_tracker.check_position_scaling(
                        open_bets,
                        sim_balance,
                        self._db_kw(),
                        market_data=_cached_market_data,
                    )
                except Exception as e:
                    logger.warning(f"[PaperTrader] 持仓管理异常(不影响主流程): {e}")
            return 0

        # ── 余额保底：触底后不再开新仓 ────────────────────────────────
        if _reserve_triggered:
            logger.warning(
                f"[Reserve] 余额 {sim_balance:.2f} < 保底线 "
                f"{_min_reserve:.2f} USDC，停止新开仓"
            )
            return 0

        # ── 持仓上限：已达最大仓位数时不再开新仓 ───────────────────────
        _max_open = int(pt_cfg.get("max_open_positions", 40))
        _current_open = len(open_bets) if open_bets else 0
        if _current_open >= _max_open:
            logger.warning(
                f"[MaxPositions] 当前开仓 {_current_open} 已达上限 {_max_open}，"
                f"停止新开仓（等待止盈/止损/结算释放仓位）"
            )
            return 0

        # 刷新聪明钱包排行榜（内部按 refresh_interval_hours 自动节流）
        try:
            self.signal_engine.smart_money.refresh_leaderboard()
        except Exception as e:
            logger.warning(f"[PaperTrader] 聪明钱包刷新失败(不影响主流程): {e}")

        # 市场扫描
        try:
            candidate_pool = self.scanner.scan()
        except Exception as e:
            logger.error(f"[PaperTrader] 扫描失败: {e}")
            return 0

        # 提取候选市场
        all_markets = (
            candidate_pool.get("tier1", [])
            + candidate_pool.get("tier2", [])
            + candidate_pool.get("tier3", [])
        )

        # ── 套利优先执行（无风险利润，在常规投注前处理）──────────────
        arb_opportunities = candidate_pool.get("arbitrage", [])
        arb_new = 0
        if arb_opportunities:
            arb_new, sim_balance = self._process_arbitrage(
                arb_opportunities, sim_balance, pt_cfg
            )

        # 天气过滤
        if weather_only:
            markets = [m for m in all_markets if is_weather_market(m)]
            logger.info(f"天气市场过滤: {len(all_markets)} → {len(markets)} 个")
        else:
            markets = all_markets

        # 黑名单过滤（禁止娱乐/电竞赛道，检查 question + slug 字段）
        blacklist = [kw.lower() for kw in (get("scanner.blacklist_keywords") or [])]
        if blacklist:
            before = len(markets)

            def _is_blacklisted(m: dict) -> bool:
                text = " ".join(
                    [
                        (m.get("question") or ""),
                        (m.get("market_slug") or ""),
                        (m.get("event_slug") or ""),
                    ]
                ).lower()
                return any(kw in text for kw in blacklist)

            markets = [m for m in markets if not _is_blacklisted(m)]
            filtered = before - len(markets)
            if filtered > 0:
                logger.info(
                    f"黑名单过滤: {before} → {len(markets)} 个 (移除 {filtered} 个)"
                )

        if not markets:
            logger.info("[PaperTrader] 无候选市场，跳过本轮")
            return 0

        # ── Round-Robin 赛道交叉排序（防止单赛道霸占所有 AI 分析名额）────
        by_cat: dict[str, list] = {}
        for m in markets:
            cat = detect_category(m)
            by_cat.setdefault(cat, []).append(m)
        # 各赛道内按 _score 降序
        for cat in by_cat:
            by_cat[cat].sort(key=lambda x: x.get("_score", 0), reverse=True)
        # 赛道按名称排序（资金分配由 SmartCapitalEngine 管理）
        cat_order = sorted(by_cat.keys())
        interleaved: list[dict] = []
        max_len = max((len(v) for v in by_cat.values()), default=0)
        for i in range(max_len):
            for cat in cat_order:
                if i < len(by_cat[cat]):
                    interleaved.append(by_cat[cat][i])
        markets = interleaved
        logger.info(
            f"[PaperTrader] 候选分布: "
            + " ".join(f"{c}={len(v)}" for c, v in sorted(by_cat.items()))
        )

        # ── 赛道配额：统计已有仓位 + 读取上限 ──────────────────────────
        cat_max_pos: dict = pt_cfg.get("category_max_positions") or {}
        category_open: dict[str, int] = {}
        asset_open: dict[str, int] = {}
        _weather_q_set: set[str] = set()
        _event_slug_open: dict[str, int] = {}  # event级别已有仓位计数
        for b in open_bets:
            cat = b.get("category", "default")
            category_open[cat] = category_open.get(cat, 0) + 1
            _asset = self._extract_underlying_asset(b.get("question", ""), cat)
            if _asset:
                asset_open[_asset] = asset_open.get(_asset, 0) + 1
            # 构建event_slug去重集合（防同赛事多子市场下注）
            _es = b.get("event_slug", "")
            if _es:
                _event_slug_open[_es] = _event_slug_open.get(_es, 0) + 1
            # 构建天气问题去重集合（标准化后的问题文本）
            if cat == "weather":
                _nq = PaperTrader._normalize_weather_question(b.get("question", ""))
                if _nq:
                    _weather_q_set.add(_nq)

        new_bets = 0
        ai_called = 0
        _cache_hits = 0
        # ── 漏斗计数器（追踪每个过滤阶段淘汰数）─────────────────────
        funnel: dict[str, int] = {
            "candidates": len(markets),
            "cat_blocked": 0,
            "cat_quota": 0,
            "market_quota": 0,
            "event_quota": 0,
            "weather_dedup": 0,
            "asset_dedup": 0,
            "stop_cooldown": 0,
            "circuit_breaker": 0,
            "exit_cooldown": 0,
            "reentry_skip": 0,
            "threshold_weather": 0,
            "weather_bucket_skip": 0,
            "low_volume": 0,
            "high_spread": 0,
            "ai_analyzed": 0,
            "signal_none": 0,
            "win_prob_low": 0,
            "exact_cap": 0,
            "divergence": 0,
            "low_price": 0,
            "entry_guard": 0,
            "momentum": 0,
            "entry_eval_fail": 0,
            "balance_insuf": 0,
            "ev_safety_gate": 0,
            "model_insuf": 0,
            "ai_uncertain": 0,
            "sports_phase1_filtered": 0,
            "placed": 0,
            "city_no_placed": 0,
            "city_no_price_reject": 0,
            "city_no_ev_reject": 0,
            "city_no_score_reject": 0,
        }

        # ── T284: 体育两阶段信号预计算（精准调用 OddsPapi）────────────────
        # Phase1: 免费数据 + AI初步 → EV过滤；Phase2: OddsPapi赔率注入AI prompt → AI重推理
        _sports_pre = [m for m in markets if detect_category(m) == "sports"]
        _sports_phase2_cache: dict[str, "Signal"] = {}
        if _sports_pre:
            try:
                _sports_phase2_cache = (
                    self.signal_engine.analyze_sports_two_phase_batch(
                        _sports_pre, sim_balance
                    )
                )
                logger.info(
                    f"[T284] 体育两阶段完成: "
                    f"{len(_sports_pre)} 市场 → {len(_sports_phase2_cache)} 通过最终门控"
                )
            except Exception as _e:
                logger.warning(f"[T284] 两阶段批处理异常，本轮体育市场跳过: {_e}")

        for market in markets:

            market_id = market.get("id") or market.get("conditionId", "")
            if not market_id:
                continue

            # ── 赛道检测（提前，用于配额检查）──────────────────────────
            market_category = detect_category(market)

            # ── 赛道屏蔽检查 ──────────────────────────────────────────
            _blocked_cats = set(pt_cfg.get("blocked_categories", []))
            if market_category in _blocked_cats:
                funnel["cat_blocked"] += 1
                logger.debug(
                    f"[CategoryBlock] {market_category} 赛道已屏蔽，跳过: "
                    f"{market_id[:20]}"
                )
                continue

            # ── 赛道配额检查 ──────────────────────────────────────────
            if cat_max_pos:
                cat_limit = cat_max_pos.get(market_category, 9999)
                if category_open.get(market_category, 0) >= cat_limit:
                    funnel["cat_quota"] += 1
                    continue

            # 检查重入：未解决的入场仓位超限（加仓订单不计入，避免阻塞新市场扫描）
            open_count = db.count_open_entry_bets_for_market(market_id, self._db_kw())
            if open_count >= max_per_market:
                funnel["market_quota"] += 1
                continue

            # ── T173: 已退出市场防重入（绝对禁止重入已止损/手动退出的市场）──
            if db.had_any_exit_for_market(market_id, self._db_kw()):
                funnel["exited_dedup"] = funnel.get("exited_dedup", 0) + 1
                logger.info(
                    f"[ExitedDedup] market_id={market_id[:16]} 已有退出记录，"
                    f"禁止重入 | {market.get('question', '')[:50]}"
                )
                continue

            # ── event 级别仓位限制（防同事件多子市场下注）──────────────
            event_slug = market.get("_event_slug", "")
            _cat_event_cfg = pt_cfg.get("category_max_per_event") or {}
            max_per_event: int = int(
                _cat_event_cfg.get(
                    market_category,
                    _cat_event_cfg.get("default", 1),
                )
            )
            if event_slug and max_per_event > 0:
                # 优先用内存集合（含本轮已下单），回退到DB查询
                event_count = _event_slug_open.get(event_slug, 0)
                if event_count >= max_per_event:
                    funnel["event_quota"] += 1
                    logger.info(
                        f"[EventDedup] event={event_slug[:40]} 已有 {event_count} 仓位，"
                        f"≥ 上限 {max_per_event}（{market_category}），跳过 {market_id}"
                    )
                    continue

            # ── 天气问题文本去重（防同城市同温度跨日重复投注）──────────
            if market_category == "weather":
                _q_normalized = PaperTrader._normalize_weather_question(
                    market.get("question", "")
                )
                if _q_normalized and _q_normalized in _weather_q_set:
                    funnel["weather_dedup"] += 1
                    logger.info(
                        f"[WeatherQuestionDedup] 已有相同天气问题开仓: "
                        f"{_q_normalized[:60]}"
                    )
                    continue

            # ── 底层资产级别仓位限制（防同资产多BIN散投，如BTC多个价格区间）──
            _underlying = self._extract_underlying_asset(
                market.get("question", ""), market_category
            )
            if _underlying:
                _cat_asset_max = pt_cfg.get("category_max_per_asset") or {}
                _max_per_asset = int(
                    _cat_asset_max.get(
                        market_category,
                        _cat_asset_max.get("default", 2),
                    )
                )
                if asset_open.get(_underlying, 0) >= _max_per_asset:
                    funnel["asset_dedup"] += 1
                    logger.info(
                        f"[AssetDedup] {_underlying} 已有 "
                        f"{asset_open[_underlying]} 仓位 ≥ {_max_per_asset}（{market_category}），跳过"
                    )
                    continue

            # ── 止损冷却期保护（防止止损后立即重入同一市场）──────────────
            # 与守护进程重启无关：基于 DB resolved_at 字段，跨重启持久生效
            # 天气市场使用更长冷却期（24h），因为天气基本面短期不变
            if is_weather_market(market):
                sl_cooldown_hours = float(
                    pt_cfg.get("weather_stop_loss_cooldown_hours", 24.0)
                )
            else:
                sl_cooldown_hours = float(pt_cfg.get("stop_loss_cooldown_hours", 6.0))
            if sl_cooldown_hours > 0 and db.had_recent_stop_loss(
                market_id, sl_cooldown_hours, self._db_kw()
            ):
                funnel["stop_cooldown"] += 1
                logger.debug(
                    f"[StopLossCooldown] {market_id[:16]} 在 {sl_cooldown_hours:.0f}h "
                    f"冷却期内，禁止重入"
                )
                continue

            # ── 止损后同方向重入拦截 (Phase 18) ────────────────────────
            # 上一笔在同市场止损后，同方向重入几乎必败（基本面未变）
            # 仅允许方向反转（YES→NO 或 NO→YES）的重入
            if pt_cfg.get("stop_loss_same_dir_block", True):
                _last_sl_outcome = db.get_last_stop_loss_outcome(
                    market_id, self._db_kw()
                )
                if _last_sl_outcome and _last_sl_outcome == signal.outcome:
                    funnel["stop_same_dir"] = funnel.get("stop_same_dir", 0) + 1
                    logger.info(
                        f"[StopLossSameDirBlock] {market_id[:16]} "
                        f"上次止损方向={_last_sl_outcome}，当前信号同方向={signal.outcome}，拒绝重入"
                    )
                    continue

            # ── 市场熔断器（连续止损后自动冷却，防反复亏损）──────────────
            _cb_cfg = pt_cfg.get("circuit_breaker") or {}
            if _cb_cfg.get("enabled", True):
                _cb_max = int(_cb_cfg.get("consecutive_stops", 3))
                _cb_hours = float(_cb_cfg.get("cooldown_hours", 24))
                _recent_exits = db.get_recent_exits_for_market(
                    market_id, limit=_cb_max, hours=_cb_hours, db_path=self._db_kw()
                )
                if len(_recent_exits) >= _cb_max and all(
                    e == "stop_loss" for e in _recent_exits
                ):
                    funnel["circuit_breaker"] += 1
                    logger.info(
                        f"[CircuitBreaker] {market_id[:20]} 连续"
                        f"{_cb_max}次止损，{_cb_hours:.0f}h内禁止入场"
                    )
                    continue

            # ── 交易冷却期保护（condition_id 级别，Phase 36）──────────────
            # 防止同一 condition_id 刚卖出又被信号引擎重新买入
            # 覆盖 profit_take/stop_loss/write_off/manual_exit 全部卖出类型
            _exit_cd_hours = float(pt_cfg.get("exit_cooldown_hours", 1.0))
            _cond_id = market.get("conditionId", "")
            if _exit_cd_hours > 0 and _cond_id:
                if db.had_recent_exit_by_condition(
                    _cond_id, _exit_cd_hours, self._db_kw()
                ):
                    funnel["exit_cooldown"] = funnel.get("exit_cooldown", 0) + 1
                    logger.debug(
                        f"[ExitCooldown] {_cond_id[:16]} 在 {_exit_cd_hours:.1f}h "
                        f"冷却期内有过卖出，禁止重入"
                    )
                    continue

            # 检查重入：价格变化是否足够（有开仓或有历史止损时）
            current_ask = float(market.get("bestAsk") or 0.5)
            last_price = self._last_price.get(market_id)
            _reentry_sl_hours = float(pt_cfg.get("reentry_stop_loss_history_hours", 48))
            _has_history = open_count > 0 or db.had_recent_stop_loss(
                market_id, _reentry_sl_hours, self._db_kw()
            )
            if last_price is not None and _has_history:
                price_change = abs(current_ask - last_price) / max(last_price, 0.01)
                if price_change < reentry_pct:
                    funnel["reentry_skip"] += 1
                    continue  # 价格变化不够，不重入

            # ── 阈值型天气问题过滤（AI无法准确估价累积概率，直接跳过）──────
            if is_weather_market(market) and is_threshold_weather_question(
                market.get("question", "")
            ):
                funnel["threshold_weather"] += 1
                logger.debug(
                    f"[PaperTrader] 跳过阈值型天气问题: {market.get('question', '')[:50]}"
                )
                continue

            # ── 按赛道查找独立 EV / 置信度门槛 ──────────────────────────
            base_min_ev = float(
                cat_ev_cfg.get(market_category, cat_ev_cfg.get("default", 1.0))
            )
            cat_conf_cfg: dict = pt_cfg.get("category_min_confidence") or {}
            effective_min_conf = float(
                cat_conf_cfg.get(market_category, cat_conf_cfg.get("default", 1.0))
            )
            effective_min_ev = base_min_ev

            # ── 赛道专用入场前过滤 ──────────────────────────────────────
            # ── 体育赛道结算时限过滤（防止长期锁定资金）────
            if market_category == "sports":
                _settle_cfg = pt_cfg.get("settlement_max_hours") or {}
                _sports_max_hours = float(_settle_cfg.get("sports", 48))
                _s_end = market.get("endDate") or market.get("endDateIso", "")
                if _s_end:
                    try:
                        _s_end_dt = datetime.fromisoformat(
                            _s_end.replace("Z", "+00:00")
                        )
                        _s_hours = (
                            _s_end_dt - datetime.now(timezone.utc)
                        ).total_seconds() / 3600
                        if _s_hours > _sports_max_hours:
                            funnel["sports_expiry"] = funnel.get("sports_expiry", 0) + 1
                            logger.info(
                                f"[SportsExpiry] 距结算{_s_hours:.0f}h > "
                                f"{_sports_max_hours:.0f}h限制，跳过: "
                                f"{market_id[:20]} | {market.get('question', '')[:40]}"
                            )
                            continue
                    except (ValueError, TypeError):
                        funnel["sports_expiry"] = funnel.get("sports_expiry", 0) + 1
                        logger.debug(
                            f"[SportsExpiry] 无法解析endDate="
                            f"{str(_s_end)[:30]}，安全跳过"
                        )
                        continue
                else:
                    # 无结算日期 → 保守跳过（防止长期锁定资金）
                    funnel["sports_expiry"] = funnel.get("sports_expiry", 0) + 1
                    logger.debug(f"[SportsExpiry] 无endDate，跳过: {market_id[:20]}")
                    continue

            if market_category == "weather":
                current_bid = float(market.get("bestBid") or 0.5)
                est_yes_ask = current_ask
                est_no_ask = 1.0 - current_bid
                likely_conviction = (
                    est_yes_ask >= conviction_min or est_no_ask >= conviction_min
                )
                likely_edge = est_yes_ask <= edge_max or est_no_ask <= edge_max
                if likely_conviction and not likely_edge:
                    effective_min_ev = conviction_min_ev
                elif not likely_conviction and not likely_edge:
                    funnel["weather_bucket_skip"] += 1
                    continue

            # ── 最低成交量硬性过滤（天气赛道豁免：天气成交量天然较低）────
            _min_vol_reject = float(pt_cfg.get("min_volume_24h_reject", 1000))
            _mkt_vol_24h = float(market.get("volume24hr", 0) or 0)
            if market_category != "weather" and _mkt_vol_24h < _min_vol_reject:
                funnel["low_volume"] += 1
                logger.debug(
                    f"[LowVolume] 24h成交量不足 {market_id[:20]}: "
                    f"vol=${_mkt_vol_24h:,.0f} < ${_min_vol_reject:,.0f}"
                )
                continue

            # ── 最大价差过滤（bestAsk-bestBid 过大说明流动性极差）─────
            _max_spread = float(pt_cfg.get("max_spread_reject", 0.15))
            _mkt_bid = float(market.get("bestBid", 0) or 0)
            _mkt_ask = float(market.get("bestAsk", 0) or 0)
            _mkt_spread = _mkt_ask - _mkt_bid if _mkt_ask > _mkt_bid else 0
            if _mkt_spread > _max_spread > 0:
                funnel["high_spread"] += 1
                logger.debug(
                    f"[HighSpread] 价差过大 {market_id[:20]}: "
                    f"spread={_mkt_spread:.3f} > {_max_spread:.3f}"
                )
                continue

            # 获取信号（体育: T284两阶段预计算缓存；其他赛道: 正常路径带缓存）
            # 天气市场变化快 → 1h 缓存；其他赛道 → 使用全局 signal_cache_hours
            _cache_h = (
                min(cache_hours, 1.0) if market_category == "weather" else cache_hours
            )

            if market_category == "sports":
                # T284: 体育市场使用预计算的 Phase2 信号（AI已看过赔率数据推理）
                if market_id not in _sports_phase2_cache:
                    funnel["sports_phase1_filtered"] += 1
                    logger.debug(f"[T284] {market_id[:16]} 未通过Phase1 EV筛选，跳过")
                    continue
                signal = _sports_phase2_cache[market_id]
                ai_called += 1
                funnel["ai_analyzed"] += 1
                if signal is None or signal.skip:
                    funnel["signal_none"] += 1
                    continue
            else:
                _cache_ttl = _cache_h * 3600
                _is_cached = (
                    market_id in self._signal_cache
                    and time.time() - self._signal_cache[market_id][0] < _cache_ttl
                )
                ai_called += 1
                funnel["ai_analyzed"] += 1
                if _is_cached:
                    _cache_hits += 1
                signal = self._get_signal(
                    market, sim_balance, _cache_h, effective_min_ev, effective_min_conf
                )

                # ── 高确定性低EV策略：常规EV不足时降级重试 ─────────────────
                # 适用场景：Iran ceasefire NO@0.89（EV仅1-2%但胜率>90%）
                if signal is None and hc_enabled:
                    _mkt_vol = float(market.get("volume", 0) or 0)
                    if _mkt_vol >= hc_min_volume:
                        signal = self._get_signal(
                            market, sim_balance, _cache_h, hc_min_ev, effective_min_conf
                        )
                        if signal is not None and not signal.skip:
                            logger.info(
                                f"[HighConviction] 高确定性策略命中 {market_id[:20]}: "
                                f"EV={signal.ev:.3f} volume=${_mkt_vol:,.0f} "
                                f"{signal.outcome}"
                            )

                if signal is None or signal.skip:
                    funnel["signal_none"] += 1
                    continue

            # ── Task225: 全局高位入场禁止（DRY RUN路径）──────────────
            # 高价位(≥0.85)：利润空间小、部分成交风险高、手续费侵蚀利润
            # 天气/体育有独立价格上限，此处用赛道级别 override
            _cat_price_ceilings = {
                "weather": float(pt_cfg.get("weather_max_price_ceiling", 0.90)),
                "sports": float(
                    (pt_cfg.get("sports") or {}).get("tail_max_price", 0.80)
                ),
            }
            _eff_max_entry = _cat_price_ceilings.get(
                market_category, float(pt_cfg.get("max_entry_price", 0.85))
            )
            _entry_price_check = signal.expected_price or current_ask
            if _entry_price_check >= _eff_max_entry:
                funnel["price_ceiling"] = funnel.get("price_ceiling", 0) + 1
                logger.info(
                    f"[PriceCeiling] 入场价 {_entry_price_check:.3f}"
                    f" >= 上限 {_eff_max_entry:.2f}，跳过: "
                    f"{market_id[:20]} ({market_category})"
                )
                continue

            # ── T28: 手续费预评估（官方公式 feeRate × p × (1-p)）──
            # 参考: docs.polymarket.com/trading/fees
            # 买入taker付费(份额扣), 卖出taker付费(USDC扣), maker不付费
            # 入场决策仅扣买入单边费（卖出费取决于未知的卖出价）
            # T228: 使用新配置结构 order_execution.buy_mode
            _buy_mode_cfg = get("order_execution.buy_mode") or {}
            _buy_per_cat = _buy_mode_cfg.get("per_category") or {}
            _oe_mode = (
                _buy_per_cat.get(market_category, "")
                or _buy_mode_cfg.get("default")
                or "taker"
            )
            _entry_p = signal.expected_price or current_ask
            if _oe_mode == "maker":
                _fee_pct = 0.0  # Maker 零手续费(官方确认)
            else:
                # Taker feeRate: 优先使用 signal_engine 预计算的动态费率（来自市场 feeSchedule）
                _sig_params = getattr(signal, "strategy_params", None) or {}
                _precomputed_fee_rate = _sig_params.get("_fee_rate")
                if _precomputed_fee_rate is not None:
                    _fee_rate_raw = float(_precomputed_fee_rate)
                else:
                    _taker_rates = pt_cfg.get("taker_fee_rates", {})
                    _fee_rate_raw = float(
                        _taker_rates.get(market_category, 0)
                        or pt_cfg.get("taker_fee_rate_default", 0.05)
                    )
                # 官方公式: fee_per_share = feeRate × p × (1-p)
                _fee_pct = _fee_rate_raw * _entry_p * (1.0 - _entry_p)
            _min_net = float(pt_cfg.get("min_net_edge_after_fees", 0.005))
            # 买入单边手续费
            _fee_total = _fee_pct
            _net_ev = signal.ev - _fee_total
            # T208: 天气pipeline信号（weather_right_tail/weather_double_no/weather_ehcl）完全豁免FeeCheck
            # WCS score已是质量门槛，与live_filters.py一致
            _stag_fee = getattr(signal, "strategy_tag", "") or ""
            _is_weather_pipeline = (
                _stag_fee.startswith(("weather_right_tail", "weather_double_no")) or _stag_fee == "weather_ehcl"
            )
            if _net_ev < _min_net and not _is_weather_pipeline:
                funnel["fee_reject"] = funnel.get("fee_reject", 0) + 1
                logger.info(
                    f"[FeeCheck] EV={signal.ev:.3f} - 费用{_fee_total:.4f}"
                    f"({_oe_mode},{market_category},p={_entry_p:.2f})"
                    f" = {_net_ev:.4f} < {_min_net:.3f}，跳过: "
                    f"{signal.question or market_id}"
                )
                continue

            # ── 高胜率筛选（投注方向的AI预测概率必须达标）──────────────
            # 使用归一化后的 P(YES) 推算 win_prob，与 _ensemble_vote 一致
            _avg_p_yes = self._extract_avg_probability(signal)
            _win_prob = _avg_p_yes if signal.outcome == "YES" else (1.0 - _avg_p_yes)

            # 分赛道 min_win_probability 门槛
            _is_weather_edge = (
                market_category == "weather" and signal.expected_price < 0.25
            )
            _win_prob_tiers = pt_cfg.get("min_win_probability_tiers", {})
            if _is_weather_edge:
                _min_win_prob = float(_win_prob_tiers.get("weather_edge", 0.30))
            elif (
                isinstance(_win_prob_tiers, dict) and market_category in _win_prob_tiers
            ):
                _min_win_prob = float(_win_prob_tiers[market_category])
            else:
                _min_win_prob = float(pt_cfg.get("min_win_probability", 0.60))
            if _win_prob < _min_win_prob:
                funnel["win_prob_low"] += 1
                logger.info(
                    f"[WinProb] 胜率不足 {market_id[:20]}: "
                    f"{signal.outcome} win_prob={_win_prob:.2f} < {_min_win_prob:.2f} ({market_category})"
                )
                continue
            # ── 精确型天气市场概率上限保护（"be X°C" 精确度数问题）──────
            if market_category == "weather":
                _q_low = (market.get("question") or "").lower()
                # 正向检测精确温度问题：含 "be N°" 但不含阈值/范围关键词
                _is_exact_hit = bool(
                    re.search(r"\bbe\s+\d+\s*°", _q_low)
                    and not is_threshold_weather_question(_q_low)
                    and "between" not in _q_low
                )
                if _is_exact_hit:
                    _exact_cap = float(pt_cfg.get("exact_weather_max_ai_prob", 0.30))
                    # GFS ensemble 信号有气象数据支持，非AI幻觉，放宽上限
                    _has_gfs = "gfs_ensemble" in (signal.model_votes or {})
                    if _has_gfs:
                        _exact_cap = max(_exact_cap, 0.99)
                    if _win_prob > _exact_cap:
                        funnel["exact_cap"] += 1
                        logger.info(
                            f"[ExactWeatherCap] 精确温度市场 AI概率过高 "
                            f"{market_id[:20]}: win_prob={_win_prob:.2f} > "
                            f"cap={_exact_cap:.2f}{'(GFS)' if _has_gfs else ''}，跳过"
                        )
                        continue

            # ── 市价-AI背离保护（赛道分级：数据驱动赛道允许更大背离）──
            _divergence = abs(_win_prob - signal.expected_price)
            _div_cfg = pt_cfg.get("divergence_limits") or {}
            _max_divergence = float(
                _div_cfg.get(
                    market_category,
                    _div_cfg.get("default", 0.35),
                )
            )
            if _divergence > _max_divergence:
                funnel["divergence"] += 1
                logger.info(
                    f"[Divergence] 市价-AI背离过大 {market_id[:20]}: "
                    f"price={signal.expected_price:.2f} AI={_win_prob:.2f} "
                    f"diff={_divergence:.2f} > {_max_divergence:.2f} ({market_category})"
                )
                continue

            # ── 全赛道低价市场跳过（防止AI幻觉导致垃圾投注）─────────
            # 注：天气edge也不再豁免，等接入ECMWF/GFS真实数据后再放开
            _low_price_thresh = float(pt_cfg.get("low_price_skip_threshold", 0.25))
            if signal.expected_price < _low_price_thresh:
                funnel["low_price"] += 1
                logger.info(
                    f"[LowPrice] 低价市场跳过 {market_id[:20]}: "
                    f"price={signal.expected_price:.3f} < {_low_price_thresh} "
                    f"({market_category})"
                )
                continue

            # ── 入场价格地板保护（双重检查：信号价 + 当前实时市价）────────
            _entry_price = signal.expected_price
            current_bid = float(market.get("bestBid") or 0.0)
            _current_outcome_price = (
                current_ask if signal.outcome == "YES" else (1.0 - current_bid)
            )
            _floor = float((get("stop_loss") or {}).get("floor_price", 0.07))
            # 天气 B_edge 使用更低的地板（入场价本身就低）
            if market_category == "weather" and _entry_price < 0.25:
                _floor = float(
                    (get("stop_loss") or {}).get("weather_edge_floor_price", 0.03)
                )
            if _entry_price < _floor or _current_outcome_price < _floor:
                funnel["entry_guard"] += 1
                logger.info(
                    f"[EntryGuard] 入场价({_entry_price:.3f})/实时价({_current_outcome_price:.3f}) "
                    f"< 地板价 {_floor:.3f}，禁止入场: {market_id[:20]}"
                )
                continue

            # ── 入场动量守卫（多轮趋势检测 + 实时快照双重验证）──────────
            # 1) 记录本轮价格到历史缓存（无论是否入场）
            # 2) 检查近N轮价格是否呈连续下跌趋势
            # 3) 回退：历史不足时使用信号价vs当前价单次比对
            _mg_cfg = pt_cfg.get("momentum_guard") or {}
            if _mg_cfg.get("enabled", True):
                _decline_thresh = float(_mg_cfg.get("decline_threshold_pct", 0.02))
                _mg_ticks = int(_mg_cfg.get("ticks", 3))
                _mg_max_drop = float(_mg_cfg.get("max_drop_pct", 0.03))
                try:
                    _fresh_slug = getattr(signal, "market_slug", "") or ""
                    _fresh_slug_map = {market_id: _fresh_slug} if _fresh_slug else {}
                    _fresh_data = self.outcome_tracker._fetch_market_statuses(
                        [market_id], slug_map=_fresh_slug_map
                    )
                    _fresh_mdata = _fresh_data.get(market_id)
                    if _fresh_mdata:
                        _fresh_price = self.outcome_tracker._extract_buy_price(
                            _fresh_mdata, signal.outcome
                        )
                        if _fresh_price and _fresh_price > 0:
                            # 记录到价格历史
                            self._record_price(market_id, _fresh_price)

                            # 多轮趋势检测
                            _rejected = self._check_momentum_trend(
                                market_id, _mg_ticks, _mg_max_drop
                            )
                            if _rejected:
                                funnel["momentum"] += 1
                                logger.info(
                                    f"[MomentumGuard] 多轮下行趋势 "
                                    f"{market_id[:20]}: {_rejected}"
                                )
                                continue

                            # 单次快照检测（信号价 vs 当前价）
                            _price_decline = (
                                signal.expected_price - _fresh_price
                            ) / signal.expected_price
                            if _price_decline > _decline_thresh:
                                funnel["momentum"] += 1
                                logger.info(
                                    f"[MomentumGuard] 价格下跌过快 "
                                    f"{market_id[:20]}: "
                                    f"signal={signal.expected_price:.3f} → "
                                    f"fresh={_fresh_price:.3f} "
                                    f"跌幅={_price_decline:.1%}"
                                )
                                continue
                except Exception as e:
                    logger.warning(f"[MomentumGuard] 检查失败: {e}")

            # ── 全赛道聪明钱包方向检查（注入 market 供入场评估使用）───
            try:
                _sm = self.signal_engine.smart_money
                market["_smart_alignment"] = _sm.get_smart_money_alignment(
                    market, signal.outcome
                )
            except Exception as e:
                logger.warning(f"[SmartMoney] 聪明钱包检查失败: {e}")
                market["_smart_alignment"] = None

            # Geopolitics 额外获取全体持仓者分析（无聪明钱包时的 fallback）
            if market_category == "geopolitics":
                market["_geo_smart"] = market.get("_smart_alignment")
                try:
                    market["_geo_holders"] = _sm.get_market_holder_analysis(
                        market.get("conditionId", "")
                    )
                except Exception:
                    market["_geo_holders"] = None

            # ── 赛道专用入场评估（分轨策略）────────────────────────────
            entry = self._evaluate_entry(
                market, signal, market_category, pt_cfg, sim_balance, funnel
            )
            if not entry:
                funnel["entry_eval_fail"] += 1
                continue
            size, bet_bucket = entry

            # ── 全赛道聪明钱包仓位调节（geopolitics 在内部已处理）─────
            _sm_cfg: dict = pt_cfg.get("smart_money") or {}
            _sm_excluded = set(_sm_cfg.get("excluded_categories") or [])
            if market_category != "geopolitics" and market_category not in _sm_excluded:
                _sm_align = market.get("_smart_alignment")
                if _sm_align and _sm_align.get("has_smart_positions"):
                    # ── Crypto 赛道聪明钱包否决权 (Phase 18 + Phase 44 升级) ──
                    # 加密货币市场信息不对称明显，聪明钱包反向 = 强烈反信号
                    # Phase 44 升级：从"对手盘否决"升级为"要求有同向聪明钱"
                    # 第一性原理：crypto 市场链上大户有真实信息优势，
                    # 无聪明钱同向 = 信号不可靠（不仅仅是对手盘反向才危险）
                    if market_category == "crypto":
                        _veto_enabled = _sm_cfg.get("crypto_veto_enabled", True)
                        _require_aligned = _sm_cfg.get("crypto_require_aligned", True)
                        _veto_min = int(_sm_cfg.get("crypto_veto_min_opposed", 1))
                        _opposed = _sm_align.get("opposed_count", 0)
                        _aligned_cnt = _sm_align.get("aligned_count", 0)
                        # 硬否决：对手盘 > 同向
                        if (
                            _veto_enabled
                            and _opposed >= _veto_min
                            and _opposed > _aligned_cnt
                        ):
                            logger.info(
                                f"[SmartMoneyCryptoVeto] {market_id}: "
                                f"聪明钱包反向 ({_opposed} opposed vs {_aligned_cnt} aligned), "
                                f"拒绝 crypto 入场"
                            )
                            funnel["smart_money_veto"] = (
                                funnel.get("smart_money_veto", 0) + 1
                            )
                            continue
                        # Phase 44: 要求至少有1个聪明钱包同向（无同向 = 不入场）
                        if _require_aligned and _aligned_cnt < 1:
                            logger.info(
                                f"[SmartMoneyCryptoGate] {market_id}: "
                                f"无聪明钱包同向 (aligned={_aligned_cnt}), "
                                f"crypto 赛道要求至少1个同向确认"
                            )
                            funnel["smart_money_no_aligned"] = (
                                funnel.get("smart_money_no_aligned", 0) + 1
                            )
                            continue

                    _cat_cap = PaperTrader._get_cat_max(
                        market_category, pt_cfg, sim_balance
                    )
                    if _sm_align.get("aligned"):
                        _bonus = float(_sm_cfg.get("bonus_multiplier", 1.3))
                        size = min(size * _bonus, _cat_cap, sim_balance)
                        bet_bucket += "_sm_boost"
                        logger.info(
                            f"[SmartMoney] 聪明钱包同向加仓 "
                            f"({market_category}): ×{_bonus} → "
                            f"{size:.1f} USDC"
                        )
                    elif _sm_align.get("opposed_count", 0) > _sm_align.get(
                        "aligned_count", 0
                    ):
                        _penalty = float(_sm_cfg.get("penalty_multiplier", 0.7))
                        _min_bet = float(get("executor.min_bet_usdc") or 2.0)
                        size = max(size * _penalty, _min_bet)
                        bet_bucket += "_sm_caution"
                        logger.info(
                            f"[SmartMoney] 聪明钱包反向减仓 "
                            f"({market_category}): ×{_penalty} → "
                            f"{size:.1f} USDC"
                        )

            _min_bet = float(get("executor.min_bet_usdc") or 2.0)
            if size < _min_bet:
                size = _min_bet

            # T38: direction_adjustment / confidence_sizing / category_size_multiplier
            # 已全部移除。sizing 由 Kelly (_calc_size) 内化，赛道乘数在
            # live_filters 中一次性应用，此处不再重复。

            if size > sim_balance:
                funnel["balance_insuf"] += 1
                logger.info(
                    f"[BalanceInsuf] {market_id[:20]}: "
                    f"需 {size:.1f} > 余额 {sim_balance:.1f}"
                )
                continue

            # ── Monte Carlo铁律：最终EV安全闸 (EV>0) ──────────────────
            # 5篇学习文章一致结论：EV≤0的任何一笔投注=定时炸弹
            # 此检查在所有调整之后、下注之前，是最后一道防线
            if signal.ev <= 0:
                funnel["ev_safety_gate"] += 1
                logger.warning(
                    f"[EVSafetyGate] 最终EV≤0拦截 {market_id[:20]}: "
                    f"ev={signal.ev:.4f} {signal.outcome} ({market_category})"
                )
                continue

            # ── 空模型检查：无AI模型共识时不入场 ──────────────────────
            _min_models = int(pt_cfg.get("min_model_count", 2))
            _model_count = len(signal.model_votes) if signal.model_votes else 0
            if _model_count < _min_models:
                funnel["model_insuf"] += 1
                logger.info(
                    f"[ModelInsuf] 模型数不足 {market_id[:20]}: "
                    f"{_model_count}<{_min_models} ({market_category})"
                )
                continue

            # ── AI概率不确定区保护（0.5±width/2 = 掷硬币区间）───────────
            _raw_ai_prob = self._extract_avg_probability(signal)
            _uncertainty_width = float(pt_cfg.get("ai_uncertainty_zone_width", 0.02))
            _uncertainty_lo = 0.5 - _uncertainty_width / 2
            _uncertainty_hi = 0.5 + _uncertainty_width / 2
            if _uncertainty_lo <= _raw_ai_prob <= _uncertainty_hi:
                # 天气赛道有GFS独立数据源，允许通过（GFS不依赖LLM）
                _has_gfs_model = "gfs_ensemble" in (signal.model_votes or {})
                if not _has_gfs_model:
                    funnel["ai_uncertain"] += 1
                    logger.info(
                        f"[AIUncertain] 概率不确定区拦截 {market_id[:20]}: "
                        f"P(YES)={_raw_ai_prob:.3f} in [{_uncertainty_lo},{_uncertainty_hi}] "
                        f"({market_category})"
                    )
                    continue

            # 检查 AI 概率字段
            ai_prob = self._extract_avg_probability(signal)
            market_price = signal.expected_price

            # ── 最小下单金额校验：确保买入 ≥ min_bet_usdc 安全底线
            # 实际 min_order_size 在实盘下单时由 order_executor 从 Orderbook API 动态获取
            _min_bet_usdc = float(get("executor.min_bet_usdc") or 2.0)
            if size < _min_bet_usdc:
                if _min_bet_usdc > sim_balance:
                    funnel["balance_insuf"] += 1
                    logger.info(
                        f"[MinSize] {market_id[:20]}: "
                        f"需 {_min_bet_usdc:.2f} USDC > 余额 {sim_balance:.1f}"
                    )
                    continue
                logger.info(
                    f"[MinSize] {market_id[:20]}: "
                    f"${size:.2f} < 最小 ${_min_bet_usdc:.2f}，"
                    f"向上调整 → ${_min_bet_usdc:.2f} USDC"
                )
                size = _min_bet_usdc

            # 记录下注
            bet_id = str(uuid.uuid4())
            sim_balance_after = sim_balance - size

            bet_record = {
                "bet_id": bet_id,
                "ts_entered": time.time(),
                "market_id": market_id,
                "question": signal.question[:500] if signal.question else "",
                "category": market_category,
                "outcome": signal.outcome,
                "confidence": signal.confidence,
                "ai_probability": ai_prob,
                "market_price": market_price,
                "ev": signal.ev,
                "size_usdc": size,
                "sim_balance_before": sim_balance,
                "sim_balance_after": sim_balance_after,
                "market_end_date": market.get("endDate")
                or market.get("endDateIso", "")
                or getattr(signal, "end_date_iso", ""),  # T179-C: 天气管线 fallback
                "weather_context": self._extract_weather_context(signal),
                "ai_models": ",".join(signal.model_votes.keys()),
                "ai_reasoning": self._extract_reasoning(signal),
                "cycle_id": self._cycle_count,
                "bet_bucket": bet_bucket,
                "market_slug": market.get("slug", "")
                or getattr(signal, "market_slug", ""),  # T181: 天气管线 fallback
                "event_slug": market.get("_event_slug", "")
                or getattr(signal, "event_slug", ""),  # T181: 天气管线 fallback
                "dimension_tags": ",".join(market.get("_dimension_tags", [])),
                "matched_shares": (
                    round(size / market_price, 6) if market_price > 0 else 0
                ),
                # 策略标签传递（优先signal.strategy_tag，fallback market._strategy_tag）
                "strategy_tag": getattr(signal, "strategy_tag", "")
                or market.get("_strategy_tag"),
                "strategy_params": getattr(signal, "strategy_params", None)
                or market.get("_strategy_params"),
                # T179-B: DRY RUN 模式下标记为 simulated
                "order_type": "simulated",
            }
            # T189-F: DRY RUN 模拟手续费估算
            _sim_shares = bet_record["matched_shares"]
            _sim_fee, _sim_fr = db.calc_pm_fee(
                _sim_shares, market_price, market_category, "simulated"
            )
            bet_record["entry_fee_usdc"] = _sim_fee
            bet_record["fee_rate"] = _sim_fr

            try:
                db.insert_paper_bet(bet_record, self._db_kw())
                self._last_price[market_id] = current_ask
                sim_balance = sim_balance_after
                new_bets += 1
                funnel["placed"] += 1
                # T174: 城市NO策略漏斗计数
                _stag = getattr(signal, "strategy_tag", "") or ""
                if _stag.startswith(("weather_right_tail", "weather_double_no")) or _stag in (
                    "city_no",
                    "t268_city_no",
                ):
                    funnel["city_no_placed"] = funnel.get("city_no_placed", 0) + 1
                # 更新赛道配额计数
                category_open[market_category] = (
                    category_open.get(market_category, 0) + 1
                )
                # 更新底层资产计数
                _bet_asset = self._extract_underlying_asset(
                    signal.question or "", market_category
                )
                if _bet_asset:
                    asset_open[_bet_asset] = asset_open.get(_bet_asset, 0) + 1
                # 更新event_slug去重计数（防同事件本轮重复下注）
                _placed_event = market.get("_event_slug", "")
                if _placed_event:
                    _event_slug_open[_placed_event] = (
                        _event_slug_open.get(_placed_event, 0) + 1
                    )
                # 更新天气问题去重集合
                if market_category == "weather":
                    _nq = PaperTrader._normalize_weather_question(signal.question or "")
                    if _nq:
                        _weather_q_set.add(_nq)
                bucket_tag = f"[{bet_bucket}]" if bet_bucket != "standard" else ""
                # ── 详细审计日志（结构化，便于事后分析）────────────────
                logger.info(
                    f"[PaperBet #{new_bets}][{market_category}]{bucket_tag} "
                    f"{signal.question[:50]}... "
                    f"→ {signal.outcome} @ {market_price:.3f} | "
                    f"EV={signal.ev:.3f} conf={signal.confidence:.2f} "
                    f"win_prob={_win_prob:.2f} | size={size:.1f} USDC "
                    f"| balance={sim_balance:.1f} | "
                    f"models={','.join(signal.model_votes.keys())}"
                )
                # Telegram 通知
                from src.utils.telegram_notifier import get_notifier

                get_notifier().notify_new_bet(
                    question=signal.question or "",
                    outcome=signal.outcome,
                    market_price=market_price,
                    ev=signal.ev,
                    confidence=signal.confidence,
                    size_usdc=size,
                    category=market_category,
                    models=",".join(signal.model_votes.keys()),
                )
            except Exception as e:
                logger.error(f"[PaperTrader] 记录失败 {market_id}: {e}")

        new_bets += arb_new
        self._log_cycle_summary(
            markets,
            ai_called,
            _cache_hits,
            new_bets,
            arb_new,
            arb_opportunities,
            funnel,
        )
        return new_bets

    def get_balance(self) -> float:
        """返回当前模拟余额。"""
        return self._get_sim_balance()

    # ------------------------------------------------------------------
    # 轮次摘要日志（从 run_cycle 拆出，Tech Debt #3）
    # ------------------------------------------------------------------

    def _log_cycle_summary(
        self,
        markets: list,
        ai_called: int,
        cache_hits: int,
        new_bets: int,
        arb_new: int,
        arb_opportunities: list,
        funnel: dict[str, int],
    ) -> None:
        """输出轮次结束的过滤漏斗、资金利用率、关键指标等汇总日志。"""
        _arb_count = len(arb_opportunities)
        logger.info(
            f"[PaperTrader] 第 {self._cycle_count} 轮完成，"
            f"候选 {len(markets)} 个，AI分析 {ai_called} 个"
            f"（缓存命中 {cache_hits}），新增 {new_bets} 笔"
            + (f"（含套利 {arb_new} 笔）" if arb_new > 0 else "")
        )
        # AI 调用成本统计
        self.signal_engine.log_cycle_stats()
        # ── 过滤漏斗汇总 ──────────────────────────────────────────────
        _pre_ai = funnel["candidates"] - sum(
            [
                funnel["cat_blocked"],
                funnel["cat_quota"],
                funnel["market_quota"],
                funnel["event_quota"],
                funnel["weather_dedup"],
                funnel["asset_dedup"],
                funnel["stop_cooldown"],
                funnel["circuit_breaker"],
                funnel.get("exit_cooldown", 0),
                funnel["reentry_skip"],
                funnel["threshold_weather"],
                funnel["weather_bucket_skip"],
                funnel["low_volume"],
                funnel["high_spread"],
            ]
        )
        logger.info(
            f"[漏斗] 候选={funnel['candidates']} → 前置过滤后={_pre_ai} → "
            f"AI分析={funnel['ai_analyzed']} → 下注={funnel['placed']} | "
            f"CatQ={funnel['cat_quota']} MktQ={funnel['market_quota']} "
            f"EvtQ={funnel['event_quota']} 信号无={funnel['signal_none']} "
            f"WinP={funnel['win_prob_low']} Div={funnel['divergence']} "
            f"LowP={funnel['low_price']} Entry={funnel['entry_eval_fail']} "
            f"Bal={funnel['balance_insuf']} "
            f"Arb检测={_arb_count} 执行={arb_new}"
        )
        # ── 城市NO策略监控统计 ─────────────────────────────────────────
        _cn_placed = funnel.get("city_no_placed", 0)
        _cn_price_reject = funnel.get("city_no_price_reject", 0)
        _cn_ev_reject = funnel.get("city_no_ev_reject", 0)
        _cn_score_reject = funnel.get("city_no_score_reject", 0)
        _cn_total = _cn_placed + _cn_price_reject + _cn_ev_reject + _cn_score_reject
        if _cn_total > 0:
            logger.info(
                f"[城市NO监控] 候选={_cn_total} → "
                f"通过={_cn_placed} | "
                f"价格拒={_cn_price_reject} EV拒={_cn_ev_reject} "
                f"评分拒={_cn_score_reject}"
            )
        # ── 资金利用率统计 ──────────────────────────────────────────
        _all_open = db.get_open_paper_bets(self._db_kw())
        _locked = sum(b.get("size_usdc", 0.0) for b in _all_open)
        _bal = self._get_sim_balance()
        _total = _locked + _bal
        _util_pct = (_locked / _total * 100) if _total > 0 else 0
        _cat_counts = {}
        for _b in _all_open:
            _c = _b.get("category", "other")
            _cat_counts[_c] = _cat_counts.get(_c, 0) + 1
        _cat_str = " ".join(f"{k}={v}" for k, v in sorted(_cat_counts.items()))
        logger.info(
            f"[资金] 利用率={_util_pct:.0f}% | "
            f"锁定=${_locked:.1f} 可用=${_bal:.1f} 总=${_total:.1f} | "
            f"持仓{len(_all_open)}个: {_cat_str}"
        )
        # ── 关键指标概览（每10轮输出一次，不影响正常日志量）──────────
        if self._cycle_count % 10 == 0:
            try:
                _metrics = db.get_metrics_summary(self._db_kw())
                _sl_ratio = _metrics.get("stop_loss_ratio", 0)
                _resolved = _metrics.get("resolved", 0)
                _sl_cnt = _metrics.get("stop_loss_count", 0)
                _pt_cnt = _metrics.get("profit_take_count", 0)
                logger.info(
                    f"[指标] 已结算={_resolved} | "
                    f"止损={_sl_cnt}({_sl_ratio:.0%}) "
                    f"止盈={_pt_cnt} | "
                    f"资金利用率={_util_pct:.0f}%"
                )
                # ── 近期止损率快速上升告警 ──────────────────────────
                _recent = db.get_recent_resolved_exits(limit=10, db_path=self._db_kw())
                if len(_recent) >= 5:
                    _recent_sl = sum(1 for e in _recent if e == "stop_loss")
                    _recent_sl_ratio = _recent_sl / len(_recent)
                    _sl_alarm = float(
                        (get("paper_trading") or {}).get("recent_sl_alarm_ratio", 0.50)
                    )
                    if _recent_sl_ratio > _sl_alarm:
                        logger.warning(
                            f"[止损告警] 近{len(_recent)}笔止损率="
                            f"{_recent_sl_ratio:.0%}（{_recent_sl}/{len(_recent)}），"
                            f"超过{_sl_alarm:.0%}警戒线！请检查策略参数"
                        )
            except Exception as e:
                logger.debug(f"[指标] 统计失败: {e}")
            # ── 预测准确率追踪（来源: solship PredictionScore 系统）──
            try:
                _acc = db.get_prediction_accuracy(recent_n=50, db_path=self._db_kw())
                _overall = _acc.get("_overall", {})
                if _overall.get("total", 0) >= 5:
                    _acc_parts = []
                    for _cat, _stats in sorted(_acc.items()):
                        if _cat.startswith("_") or _stats.get("total", 0) < 3:
                            continue
                        _acc_parts.append(
                            f"{_cat}={_stats['win_rate']:.0%}({_stats['total']}笔"
                            f",校准误差{_stats['calibration_error']:.2f})"
                        )
                    logger.info(
                        f"[准确率] 总胜率={_overall.get('win_rate', 0):.0%}"
                        f"({_overall.get('total', 0)}笔) | " + " ".join(_acc_parts)
                    )
            except Exception as e:
                logger.debug(f"[准确率] 统计失败: {e}")
            # ── 城市NO策略累计表现 ──────────────────────────────
            try:
                _cn_stats = db.query_city_no_performance(db_path=self._db_kw())
                if _cn_stats["total"] > 0:
                    _city_parts = []
                    for _city, _cs in sorted(_cn_stats["by_city"].items()):
                        _city_parts.append(
                            f"{_city}={_cs['wins']}/{_cs['total']}"
                            f"(${_cs['pnl']:+.2f})"
                        )
                    logger.info(
                        f"[城市NO表现] 总投注={_cn_stats['total']} "
                        f"Win={_cn_stats['wins']}"
                        f"({_cn_stats['win_rate']:.0%}) "
                        f"PNL=${_cn_stats['pnl']:+.2f} | "
                        f"按城市: {' '.join(_city_parts)}"
                    )
            except Exception as e:
                logger.debug(f"[城市NO表现] 统计失败: {e}")

    # ------------------------------------------------------------------
    # 套利执行
    # ------------------------------------------------------------------

    def _process_arbitrage(
        self, opportunities: list[dict], sim_balance: float, pt_cfg: dict
    ) -> tuple[int, float]:
        """
        处理套利机会：同时买入 YES + NO，锁定无风险利润。

        返回: (new_bets_count, updated_sim_balance)
        """
        arb_cfg = pt_cfg.get("arbitrage") or {}
        if not arb_cfg.get("enabled", True):
            return 0, sim_balance

        min_profit = float(arb_cfg.get("min_profit_pct", 5.0))
        min_liq = float(arb_cfg.get("min_liquidity", 5000))
        max_per_cycle = int(arb_cfg.get("max_per_cycle", 5))
        max_single = float(arb_cfg.get("max_single_size", 50.0))
        max_exposure_pct = float(arb_cfg.get("max_total_exposure_pct", 0.30))

        initial_balance = sim_balance
        max_total_exposure = initial_balance * max_exposure_pct
        total_deployed = 0.0
        new_bets = 0

        # 按利润率降序排列
        opportunities.sort(key=lambda x: x.get("profit_pct", 0), reverse=True)

        for opp in opportunities:
            if new_bets >= max_per_cycle * 2:  # 每个套利产生2笔bet
                break

            profit_pct = opp.get("profit_pct", 0)
            liquidity = opp.get("liquidity", 0)
            market_id = opp.get("market_id", "")
            question = opp.get("question", "")

            # 利润率门槛
            if profit_pct < min_profit:
                logger.info(
                    f"[ARB_SKIP] 利润不足: {question[:40]} "
                    f"profit={profit_pct:.1f}% < {min_profit}%"
                )
                continue

            # 流动性门槛
            if liquidity < min_liq:
                logger.info(
                    f"[ARB_SKIP] 流动性不足: {question[:40]} "
                    f"liq=${liquidity:,.0f} < ${min_liq:,.0f}"
                )
                continue

            # 检查是否已有这个市场的仓位
            existing = db.count_open_entry_bets_for_market(market_id, self._db_kw())
            if existing > 0:
                logger.info(
                    f"[ARB_SKIP] 已有仓位: {question[:40]} " f"existing={existing}"
                )
                continue

            # 计算仓位
            yes_ask = opp.get("yes_ask", 0)
            no_ask_eff = opp.get("no_ask_effective", 0)
            combined = opp.get("combined", 0)
            if combined <= 0 or combined >= 1.0:
                continue

            # 每单位(1 share YES + 1 share NO)成本 = combined
            # 每单位保证回报 = 1.00
            # 投入金额 = min(max_single, 余额限制, 总敞口限制)
            total_size = min(max_single, sim_balance * 0.5)
            remaining_exposure = max_total_exposure - total_deployed
            if remaining_exposure <= 0:
                logger.info("[ARB] 总套利敞口已达上限，停止")
                break
            total_size = min(total_size, remaining_exposure)

            if total_size < 2.0:  # 最小投入 $2（YES+NO各$1）
                logger.info(f"[ARB_SKIP] 余额不足套利: balance={sim_balance:.1f}")
                break

            # 按比例分配 YES 和 NO 的投入
            yes_size = round(total_size * (yes_ask / combined), 2)
            no_size = round(total_size * (no_ask_eff / combined), 2)
            actual_total = yes_size + no_size

            # ── 最小下单金额校验（套利两端都需满足）──────
            # 实际 min_order_size 在实盘由 order_executor 动态获取
            _min_bet = float(get("executor.min_bet_usdc") or 2.0)
            if yes_size < _min_bet or no_size < _min_bet:
                _min_yes = max(yes_size, _min_bet)
                _min_no = max(no_size, _min_bet)
                _min_total = _min_yes + _min_no
                if _min_total > sim_balance * 0.5 or _min_total > remaining_exposure:
                    logger.info(
                        f"[ARB_MIN_SIZE] 套利跳过: 需 ${_min_total:.2f} "
                        f"满足最低 ${_min_bet:.2f}/侧"
                    )
                    continue
                yes_size = _min_yes
                no_size = _min_no
                actual_total = yes_size + no_size
                logger.info(
                    f"[ARB_MIN_SIZE] 向上调整至 ${actual_total:.2f} "
                    f"以满足最低 ${_min_bet:.2f}/侧"
                )

            # 预期利润
            units = actual_total / combined
            expected_profit = units * (1.0 - combined)

            arb_pair_id = str(uuid.uuid4())[:8]
            ts_now = time.time()
            market_category = opp.get("category", "unknown")
            arb_event_slug = opp.get("event_slug", "")

            # 记录 YES 方向
            bet_yes = {
                "bet_id": str(uuid.uuid4()),
                "ts_entered": ts_now,
                "market_id": market_id,
                "question": question[:500],
                "category": market_category,
                "outcome": "YES",
                "confidence": 1.0,
                "ai_probability": 0.5,
                "market_price": yes_ask,
                "ev": expected_profit / actual_total if actual_total > 0 else 0,
                "size_usdc": yes_size,
                "sim_balance_before": sim_balance,
                "sim_balance_after": sim_balance - yes_size,
                "market_end_date": "",
                "weather_context": "",
                "ai_models": "arbitrage",
                "ai_reasoning": (
                    f"ARBITRAGE: YES@{yes_ask:.4f}+NO@{no_ask_eff:.4f}"
                    f"={combined:.4f} profit={profit_pct:.1f}% "
                    f"pair={arb_pair_id}"
                ),
                "cycle_id": self._cycle_count,
                "bet_bucket": "arbitrage",
                "bet_type": "arbitrage",
                "market_slug": "",
                "event_slug": arb_event_slug,
                "dimension_tags": f"arb_pair:{arb_pair_id}",
                "matched_shares": round(yes_size / yes_ask, 6) if yes_ask > 0 else 0,
            }

            # 记录 NO 方向
            bet_no = {
                "bet_id": str(uuid.uuid4()),
                "ts_entered": ts_now,
                "market_id": market_id,
                "question": question[:500],
                "category": market_category,
                "outcome": "NO",
                "confidence": 1.0,
                "ai_probability": 0.5,
                "market_price": no_ask_eff,
                "ev": expected_profit / actual_total if actual_total > 0 else 0,
                "size_usdc": no_size,
                "sim_balance_before": sim_balance - yes_size,
                "sim_balance_after": sim_balance - yes_size - no_size,
                "market_end_date": "",
                "weather_context": "",
                "ai_models": "arbitrage",
                "ai_reasoning": (
                    f"ARBITRAGE: YES@{yes_ask:.4f}+NO@{no_ask_eff:.4f}"
                    f"={combined:.4f} profit={profit_pct:.1f}% "
                    f"pair={arb_pair_id}"
                ),
                "cycle_id": self._cycle_count,
                "bet_bucket": "arbitrage",
                "bet_type": "arbitrage",
                "market_slug": "",
                "event_slug": arb_event_slug,
                "dimension_tags": f"arb_pair:{arb_pair_id}",
                "matched_shares": (
                    round(no_size / no_ask_eff, 6) if no_ask_eff > 0 else 0
                ),
            }

            try:
                db.insert_paper_bet(bet_yes, self._db_kw())
                db.insert_paper_bet(bet_no, self._db_kw())
                sim_balance -= actual_total
                total_deployed += actual_total
                new_bets += 2
                logger.info(
                    f"[ARB_EXEC] 套利执行! {question[:50]} | "
                    f"YES@{yes_ask:.4f} ${yes_size:.1f} + "
                    f"NO@{no_ask_eff:.4f} ${no_size:.1f} = "
                    f"${actual_total:.1f} | 预期利润=${expected_profit:.2f} "
                    f"({profit_pct:.1f}%) pair={arb_pair_id}"
                )
            except Exception as e:
                logger.error(f"[ARB] 记录失败 {market_id}: {e}")

        if new_bets > 0:
            logger.info(
                f"[ARB] 本轮套利: {new_bets}笔 | "
                f"投入=${total_deployed:.1f} | 余额=${sim_balance:.1f}"
            )

        return new_bets, sim_balance

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _db_kw(self):
        """返回 db_path 关键字参数（若没有指定则使用 db 模块默认值）。"""
        if self._db_path is not None:
            return self._db_path
        return db._DB_PATH

    def _get_sim_balance(self) -> float:
        """
        计算当前模拟余额：
          = 初始余额 + 所有已结算 pnl 之和
          - 所有未结算仓位占用金额
        """
        pt_cfg = get("paper_trading") or {}
        initial = float(pt_cfg.get("initial_balance", 100.0))
        try:
            summary = db.get_paper_summary(self._db_kw())
            realized_pnl = summary.get("total_pnl", 0.0)
            open_bets = db.get_open_paper_bets(self._db_kw())
            locked = sum(b.get("size_usdc", 0.0) for b in open_bets)
            return round(initial + realized_pnl - locked, 4)
        except Exception as e:
            logger.warning(f"[PaperTrader] 余额计算失败，使用初始值: {e}")
            return initial

    def _record_price(self, market_id: str, price: float) -> None:
        """记录市场价格到历史缓存（每轮调用，用于多tick动量检测）。"""
        _MAX_HISTORY = 20
        history = self._price_history.setdefault(market_id, [])
        history.append((time.time(), price))
        if len(history) > _MAX_HISTORY:
            self._price_history[market_id] = history[-_MAX_HISTORY:]

    def _check_momentum_trend(
        self, market_id: str, ticks: int, max_drop_pct: float
    ) -> str:
        """
        多轮价格趋势检测。

        检查最近 N 轮价格记录是否呈连续下行趋势，
        且累计跌幅超过阈值。比单次快照更能识别持续下行。

        Returns:
            空字符串 = 通过（无下行趋势），非空 = 拒绝原因描述
        """
        history = self._price_history.get(market_id, [])
        if len(history) < ticks:
            return ""  # 数据不足，安全放行

        recent = [p for _, p in history[-ticks:]]

        # 检查是否连续下跌（每一轮都比前一轮低）
        is_downtrend = all(recent[i] > recent[i + 1] for i in range(len(recent) - 1))
        if not is_downtrend:
            return ""

        # 检查累计跌幅
        first_price = recent[0]
        last_price = recent[-1]
        if first_price <= 0:
            return ""
        total_drop = (first_price - last_price) / first_price
        if total_drop >= max_drop_pct:
            return (
                f"连续{ticks}轮下跌: "
                f"{first_price:.3f}→{last_price:.3f} "
                f"累计跌幅={total_drop:.1%} ≥ {max_drop_pct:.1%}"
            )
        return ""

    def _get_signal(
        self,
        market: dict,
        balance: float,
        cache_hours: float,
        min_ev: float,
        min_conf: float,
    ) -> Optional[Signal]:
        """获取信号，优先使用缓存（减少 AI API 费用）。

        直接调用 signal_engine._analyze_market()，避免 generate_signals() 中
        config_loader.get() 触发热加载覆盖临时阈值的竞态问题。
        """
        market_id = market.get("id") or market.get("conditionId", "")
        cache_ttl = cache_hours * 3600

        # 检查缓存
        if market_id in self._signal_cache:
            cached_ts, cached_signal = self._signal_cache[market_id]
            if time.time() - cached_ts < cache_ttl:
                logger.debug(f"[PaperTrader] 使用缓存信号: {market_id[:16]}...")
                return cached_signal

        # 直接调用 _analyze_market，在此层做 EV/置信度过滤
        try:
            # 先检查是否有缓存的原始信号（即使之前因EV不足被过滤）
            _raw_cache_key = f"_raw_{market_id}"
            if _raw_cache_key in self._signal_cache:
                _rts, sig = self._signal_cache[_raw_cache_key]
                if time.time() - _rts < cache_ttl:
                    logger.debug(f"[PaperTrader] 使用原始缓存信号: {market_id[:16]}...")
                else:
                    sig = self.signal_engine._analyze_market(market, balance)
                    self._signal_cache[_raw_cache_key] = (time.time(), sig)
            else:
                sig = self.signal_engine._analyze_market(market, balance)
                self._signal_cache[_raw_cache_key] = (time.time(), sig)

            if sig.skip:
                logger.info(f"[PaperTrader] AI跳过 {market_id[:20]}: {sig.skip_reason}")
                return None
            if sig.ev < min_ev:
                logger.info(
                    f"[PaperTrader] EV不足 {market_id[:20]}: "
                    f"EV={sig.ev:.3f} < {min_ev:.3f}"
                )
                return None
            if sig.confidence < min_conf:
                logger.info(
                    f"[PaperTrader] 置信不足 {market_id[:20]}: "
                    f"conf={sig.confidence:.2f} < {min_conf:.2f}"
                )
                return None

            self._signal_cache[market_id] = (time.time(), sig)
            return sig
        except Exception as e:
            logger.warning(f"[PaperTrader] 信号生成失败 {market_id}: {e}")
            return None

    def _extract_avg_probability(self, signal: Signal) -> float:
        """从 model_votes 中提取平均 P(YES)。"""
        return self._extract_avg_probability_static(signal)

    @staticmethod
    def _get_cat_max(category: str, pt_cfg: dict, sim_balance: float) -> float:
        """按赛道获取单笔最大下注额，统一由 SmartCapitalEngine 管控。"""
        from src.utils.smart_capital import SmartCapitalEngine

        return SmartCapitalEngine.get_cat_max_usdc(sim_balance, category)

    @staticmethod
    def _apply_slippage_factor(size: float, market_price: float) -> float:
        """
        根据市场价格估算流动性深度，对仓位施加滑点折扣。

        极端价格区间（<10¢ 或 >90¢）流动性薄，大单会造成严重滑点。
        中间价格区间流动性相对充裕但仍需折扣。
        """
        _slip_cfg = (get("paper_trading") or {}).get("slippage_factors") or {}
        if market_price < 0.10:
            factor = float(_slip_cfg.get("extreme_low", 0.5))
        elif market_price < 0.20:
            factor = float(_slip_cfg.get("low", 0.7))
        elif market_price > 0.90:
            factor = float(_slip_cfg.get("extreme_high", 0.7))
        else:
            factor = float(_slip_cfg.get("normal", 0.9))
        adjusted = round(size * factor, 2)
        _min_bet = float(get("executor.min_bet_usdc") or 2.0)
        return max(adjusted, _min_bet)

    @staticmethod
    def _calc_dynamic_size(
        signal: Signal,
        market: dict,
        category: str,
        pt_cfg: dict,
        sim_balance: float,
        base_multiplier: float = 1.0,
        balance_cap_pct: float = 1.0,
    ) -> float:
        """
        动态仓位调整（T38 精简：Kelly 已处理 EV/置信度，此处仅做市场约束调整）。

        保留因子：
        1. Kelly 基础仓位  (signal.size_usdc × base_multiplier)
        2. 市场流动性折扣  (volume24hr → 阶梯折扣)
        + slippage factor  (price-based)

        Args:
            base_multiplier: 对 base 的乘数（conviction=3.0, spread=0.5）
            balance_cap_pct: 余额占比上限（conviction=0.15）
        """
        ds_cfg = pt_cfg.get("dynamic_sizing") or {}
        cat_max = PaperTrader._get_cat_max(category, pt_cfg, sim_balance)
        bal_cap = sim_balance * balance_cap_pct
        base = min(signal.size_usdc * base_multiplier, cat_max, bal_cap)

        # 未启用动态sizing时回退到旧逻辑
        if not ds_cfg.get("enabled", False):
            return PaperTrader._apply_slippage_factor(base, signal.expected_price)

        # Factor 1: 市场流动性
        volume = float(market.get("volume24hr", 0) or 0)
        liq_tiers = ds_cfg.get("liquidity_tiers", [5000, 20000, 100000])
        liq_factors = ds_cfg.get("liquidity_factors", [0.3, 0.5, 0.7, 1.0])
        if volume < liq_tiers[0]:
            liq_f = liq_factors[0]
        elif volume < liq_tiers[1]:
            liq_f = liq_factors[1]
        elif volume < liq_tiers[2]:
            liq_f = liq_factors[2]
        else:
            liq_f = liq_factors[3]

        adjusted = base * liq_f

        # 滑点因子（price-based）
        adjusted = PaperTrader._apply_slippage_factor(adjusted, signal.expected_price)

        # 硬限制（不含 sim_balance，由调用方检查 size > sim_balance）
        adjusted = min(adjusted, cat_max, bal_cap)
        _min_bet = float(get("executor.min_bet_usdc") or 2.0)
        adjusted = max(adjusted, _min_bet)
        adjusted = round(adjusted, 2)

        logger.info(
            f"[DynSize] {category}: base=${base:.1f} "
            f"liq={liq_f}(vol={volume:.0f}) → ${adjusted:.1f}"
        )
        return adjusted

    @staticmethod
    def _extract_underlying_asset(question: str, category: str) -> str:
        """从问题文本中提取底层资产标识符。

        crypto: 提取币种名（Bitcoin/Ethereum 等）
        weather: 提取城市名（New York City/Seattle 等）
        用于防止同一底层资产在不同价格区间/日期被重复投注。
        """
        q = question.lower()
        if category == "crypto":
            _ASSET_KEYWORDS = {
                "bitcoin": ["bitcoin", " btc ", "btc ", " btc"],
                "ethereum": ["ethereum", " eth ", "eth ", " eth"],
                "solana": ["solana", " sol "],
                "dogecoin": ["dogecoin", " doge "],
                "xrp": [" xrp "],
            }
            for asset, keywords in _ASSET_KEYWORDS.items():
                if any(kw in q for kw in keywords):
                    return asset
            return ""
        elif category == "weather":
            # 提取城市名作为底层资产
            _CITY_PATTERNS = [
                "new york city",
                "nyc",
                "los angeles",
                "chicago",
                "seattle",
                "miami",
                "houston",
                "dallas",
                "phoenix",
                "san francisco",
                "denver",
                "boston",
                "atlanta",
                "minneapolis",
                "detroit",
                "philadelphia",
                "washington",
                "tokyo",
                "london",
                "paris",
                "berlin",
                "seoul",
                "sydney",
                "toronto",
                "mumbai",
                "shanghai",
                "beijing",
            ]
            for city in _CITY_PATTERNS:
                if city in q:
                    return f"weather_{city.replace(' ', '_')}"
            return ""
        return ""

    @staticmethod
    def _normalize_weather_question(question: str) -> str:
        """标准化天气问题文本用于跨日去重。

        去除日期引用，使同城市同温度不同日期的问题映射到同一键。
        例：
          "Will the high in Shanghai be 11°C on March 18?" → "will the high in shanghai be 11°c"
          "Will the high in Shanghai be 11°C on March 19?" → "will the high in shanghai be 11°c"
        """
        if not question:
            return ""
        q = question.lower().strip()
        # 去除日期模式: "on March 18", "on March 18, 2026", "on 2026-03-18"
        q = re.sub(
            r"\bon\s+"
            r"(january|february|march|april|may|june|july|august|"
            r"september|october|november|december)\s+\d{1,2}"
            r"(,?\s*\d{4})?",
            "",
            q,
        )
        q = re.sub(r"\bon\s+\d{4}-\d{2}-\d{2}", "", q)
        # 去除尾部问号和多余空格
        q = re.sub(r"\s*\?\s*$", "", q)
        q = re.sub(r"\s+", " ", q).strip()
        return q

    @staticmethod
    def _evaluate_entry(
        market: dict,
        signal: Signal,
        category: str,
        pt_cfg: dict,
        sim_balance: float,
        funnel: dict | None = None,
    ) -> Optional[tuple[float, str]]:
        """
        赛道分轨入场评估。

        Args:
            market: 原始市场数据
            signal: AI 生成的信号
            category: 赛道名称 (weather/sports/crypto/geopolitics/default)
            pt_cfg: paper_trading 配置节
            sim_balance: 当前模拟余额

        Returns:
            (size_usdc, bet_bucket) 或 None（不入场）
        """
        if category == "weather":
            return None  # T91: 天气赛道旧管线已删除，新管线待重构
        elif category == "sports":
            return PaperTrader._evaluate_sports_entry(
                market, signal, pt_cfg, sim_balance
            )
        elif category == "crypto":
            return PaperTrader._evaluate_crypto_entry(
                market, signal, pt_cfg, sim_balance
            )
        elif category == "geopolitics":
            base_size = PaperTrader._calc_dynamic_size(
                signal, market, "geopolitics", pt_cfg, sim_balance
            )

            # 聪明钱包增强策略
            geo_smart = market.get("_geo_smart")
            geo_holders = market.get("_geo_holders")
            bonus_mult = float(
                (pt_cfg.get("smart_money") or {}).get(
                    "geopolitics_bonus_multiplier", 1.5
                )
            )

            if geo_smart and geo_smart.get("has_smart_positions"):
                if geo_smart.get("aligned"):
                    # 聪明钱包同方向 → 可加仓
                    cat_max = PaperTrader._get_cat_max(
                        "geopolitics", pt_cfg, sim_balance
                    )
                    base_size = min(base_size * bonus_mult, cat_max, sim_balance)
                    bucket = "geo_smart_confirmed"
                    logger.info(
                        f"[GeoSmart] 聪明钱包同向确认 "
                        f"(aligned={geo_smart['aligned_count']}), "
                        f"加仓至 {base_size:.1f}"
                    )
                elif geo_smart.get("opposed_count", 0) > geo_smart.get(
                    "aligned_count", 0
                ):
                    # 聪明钱包反向 → 拒绝入场
                    logger.info(
                        f"[GeoSmart] 聪明钱包反向 "
                        f"(opposed={geo_smart['opposed_count']}), 拒绝入场"
                    )
                    return None
                else:
                    bucket = "geo_smart_mixed"
            elif geo_holders:
                # 聪明钱包不在此市场 → 分析市场持仓者
                total = geo_holders.get("total_holders", 0)
                consensus = geo_holders.get("consensus", "neutral")
                if total < 10:
                    logger.info(
                        f"[GeoHolders] 持仓者({total})<10 + 无聪明钱包，"
                        f"流动性不足，拒绝入场"
                    )
                    return None
                if consensus == signal.outcome:
                    bucket = "geo_holders_confirmed"
                    logger.info(
                        f"[GeoHolders] 市场共识({consensus})与AI({signal.outcome})"
                        f"一致，允许入场"
                    )
                elif consensus == "neutral":
                    bucket = "geo_holders_neutral"
                else:
                    # 共识与AI矛盾 → 降低仓位但允许入场（逎势信号可能有alpha）
                    base_size = base_size * 0.5
                    bucket = "geo_holders_opposed"
                    logger.info(
                        f"[GeoHolders] 市场共识({consensus})与AI({signal.outcome})"
                        f"矛盾，降仓50%入场"
                    )
            else:
                # 聪明钱包/持仓者数据获取失败 → 保守基础仓位
                bucket = "geopolitics"

            # 滑点因子（geo路径在bonus/调仓后重新应用）
            base_size = PaperTrader._apply_slippage_factor(
                base_size, signal.expected_price
            )
            if base_size > sim_balance:
                return None
            return (base_size, bucket)
        else:
            # default fallback
            size = PaperTrader._calc_dynamic_size(
                signal, market, category, pt_cfg, sim_balance
            )
            if size > sim_balance:
                return None
            return (size, "standard")

    @staticmethod
    def _evaluate_crypto_entry(
        market: dict, signal: Signal, pt_cfg: dict, sim_balance: float
    ) -> Optional[tuple[float, str]]:
        """
        Crypto 赛道专用入场评估 — 数据驱动概率门控。

        第一性原理：与天气赛道 GFS ensemble 同理，crypto 用 Black-Scholes
        概率引擎独立计算 P(YES)，作为 AI 信号的数据验证层。
        学习 polyrec/mlmodelpoly 特征工程思路（VWAP 偏差 + 动量 + F&G）。
        """
        from src.utils.crypto_probability_engine import get_crypto_data_signal

        question = market.get("question", "")
        _cr_cfg: dict = pt_cfg.get("crypto") or {}
        crypto_max = float(_cr_cfg.get("max_size_usdc", 5.0))
        min_data_edge = float(_cr_cfg.get("min_data_edge", 0.06))
        insurance_min_price = float(_cr_cfg.get("insurance_min_price", 0.88))

        # 尝试获取数据驱动信号
        data_signal = None
        try:
            data_signal = get_crypto_data_signal(question)
        except Exception as e:
            logger.warning(f"[CryptoEntry] 概率引擎异常，降级到基础逻辑: {e}")

        if data_signal is None:
            # 无法解析目标价/币种 → fallback 到基础仓位计算（不拦截）
            logger.info(
                "[CryptoEntry] 无法计算数据概率（无目标价或数据不足），使用基础逻辑"
            )
            size = PaperTrader._calc_dynamic_size(
                signal, market, "crypto", pt_cfg, sim_balance
            )
            size = min(size, crypto_max)
            if size > sim_balance:
                return None
            return (size, "crypto_basic")

        data_prob = data_signal["probability"]
        data_edge = data_signal["edge"]
        data_direction = data_signal["direction"]
        ai_direction = signal.outcome or "YES"

        # Insurance 模式检测（best_ask >= 0.88 → 高概率事件）
        outcome_price = signal.expected_price
        if outcome_price and outcome_price >= insurance_min_price:
            logger.info(
                f"[CryptoInsurance] 高价候选: price={outcome_price:.2f}≥"
                f"{insurance_min_price} — 建议考虑 Sell NO 保险策略"
            )

        # 数据方向 vs AI 方向一致性检查
        if data_direction != ai_direction:
            logger.info(
                f"[CryptoEntry] 方向矛盾: 数据={data_direction}(P={data_prob:.3f}) "
                f"vs AI={ai_direction} → 跳过"
            )
            return None

        # Edge 门控
        if data_edge < min_data_edge:
            logger.info(
                f"[CryptoEntry] 数据edge={data_edge:.3f} < 门槛{min_data_edge} → 跳过"
            )
            return None

        # 通过门控 → 仓位计算
        size = PaperTrader._calc_dynamic_size(
            signal, market, "crypto", pt_cfg, sim_balance
        )
        size = min(size, crypto_max)
        if size > sim_balance:
            return None

        # 数据强信号时适度加仓（edge > 2× 门槛）
        if data_edge >= min_data_edge * 2:
            bonus = 1.2
            size = min(size * bonus, crypto_max, sim_balance)
            logger.info(
                f"[CryptoEntry] 强数据信号(edge={data_edge:.3f}≥{min_data_edge*2:.3f})"
                f" 加仓×{bonus} → ${size:.2f}"
            )

        logger.info(
            f"[CryptoEntry] ✅ 通过: data_P={data_prob:.3f} "
            f"edge={data_edge:.3f} dir={data_direction} size=${size:.2f}"
        )
        return (size, "crypto_data")

    @staticmethod
    def _evaluate_sports_entry(
        market: dict, signal: Signal, pt_cfg: dict, sim_balance: float
    ) -> Optional[tuple[float, str]]:
        """
        体育赛道入场评估。

        策略（T183 Moneyline-only 铁律 + Phase 49 搏冷优化）：
        - Moneyline-only：非胜负盘市场（spread/O-U/props）→ 硬性拒绝
        - 尾盘过滤：买入价 ≥ sports_tail_max_price(0.85) → 跳过
        - 搏冷加仓：买入价 ≤ sports_underdog_max_price(0.35) → 仓位 × boost
        - Moneyline（整场胜负）：标准 EV 驱动
        """
        # ── T183 铁律：Moneyline-only 硬性拒绝 ──
        _sp_cfg: dict = pt_cfg.get("sports") or {}
        moneyline_only = _sp_cfg.get("moneyline_only", True)
        if moneyline_only:
            q = (signal.question or "").lower()
            non_ml_patterns = (
                "spread:",
                "spread ",
                "(+",
                "(-",
                "+0.5",
                "+1.5",
                "+2.5",
                "+3.5",
                "+4.5",
                "+5.5",
                "-0.5",
                "-1.5",
                "-2.5",
                "-3.5",
                "-4.5",
                "-5.5",
                "+6.5",
                "+7.5",
                "+8.5",
                "+9.5",
                "+10.5",
                "-6.5",
                "-7.5",
                "-8.5",
                "-9.5",
                "-10.5",
                "o/u",
                "over/under",
                "total points",
                "total goals",
                "total runs",
                "total rounds",
                "handicap",
                "first to score",
                "both teams to score",
                "clean sheet",
                "exact score",
                "correct score",
                "player prop",
                "strikeouts",
                "assists over",
                "assists under",
                "rebounds over",
                "rebounds under",
                "three-pointers",
                "3-pointers",
                "corners",
                "rounds",
            )
            if any(pat in q for pat in non_ml_patterns):
                logger.info(
                    f"[Sports] ⛔ Moneyline-only铁律: 非胜负盘市场被拒绝 | "
                    f"{signal.question[:60]}"
                )
                return None

        # ── 尾盘过滤：避免买高概率蚊子肉 ──
        buying_price = signal.expected_price or 0.5
        tail_max = float(_sp_cfg.get("tail_max_price", 0.80))
        if buying_price >= tail_max:
            logger.info(
                f"[Sports] ⛔ 尾盘过滤: 买入价 {buying_price:.3f} ≥ {tail_max} "
                f"→ 蚊子肉跳过 | {signal.question[:50]}"
            )
            return None
        # Moneyline（标准体育投注）

        # ── T184: 博彩效率 Gap 检测（Moneyline专属）──
        from src.signal.signal_engine import SignalEngine

        gap_result = SignalEngine.evaluate_bookmaker_gap(
            signal.question or "",
            signal.outcome or "YES",
            buying_price,
        )
        effective_ev = signal.ev
        if gap_result and gap_result["gap_level"] in ("medium", "strong"):
            gap_cfg = _sp_cfg.get("bookmaker_gap") or {}
            if gap_result["aligned"]:
                bonus = float(gap_cfg.get("ev_bonus_aligned", -0.005))
                effective_ev = signal.ev + abs(bonus)
                logger.info(
                    f"[Sports] 📊 博彩Gap同向确认: gap={gap_result['gap']:+.3f} "
                    f"({gap_result['gap_level']}), EV {signal.ev:.3f}→{effective_ev:.3f}"
                )
            else:
                penalty = float(gap_cfg.get("ev_penalty_opposed", 0.02))
                cat_min_ev = float(get("paper_trading.category_min_ev.sports") or 0.02)
                required_ev = signal.ev + penalty
                if signal.ev < required_ev:
                    logger.info(
                        f"[Sports] ⚠️ 博彩Gap反向警告: gap={gap_result['gap']:+.3f} "
                        f"({gap_result['gap_level']}), EV {signal.ev:.3f} 需 {required_ev:.3f}"
                    )

        size = PaperTrader._calc_dynamic_size(
            signal, market, "sports", pt_cfg, sim_balance
        )

        # ── 搏冷加仓：低赔率冷门仓位加成 ──
        underdog_max = float(_sp_cfg.get("underdog_max_price", 0.35))
        if buying_price <= underdog_max:
            boost = float(_sp_cfg.get("underdog_size_boost", 1.3))
            old_size = size
            size = round(size * boost, 2)
            logger.info(
                f"[Sports] 🏆 搏冷加仓: 买入价 {buying_price:.3f} ≤ {underdog_max} "
                f"→ 仓位 ${old_size:.2f} × {boost} = ${size:.2f}"
            )
        if size > sim_balance:
            return None
        return (size, "sports")

    @staticmethod
    def _extract_avg_probability_static(signal: Signal) -> float:
        """从 model_votes 中提取平均 P(YES)（静态版本，供入场评估使用）。"""
        if not signal.model_votes:
            return 0.5
        probs = []
        for v in signal.model_votes.values():
            p = float(v.get("probability", 0.5))
            outcome = v.get("outcome", "YES")
            if outcome == "NO" and p > 0.5:
                p = 1.0 - p
            elif outcome == "YES" and p < 0.5:
                p = 1.0 - p
            probs.append(p)
        return round(sum(probs) / len(probs), 4) if probs else 0.5

    @staticmethod
    def _extract_reasoning(signal: Signal) -> str:
        """从 model_votes 中提取各模型的 reasoning。

        T179-D修复: 兼容两种 model_votes 格式：
        - AI ensemble: {"model_name": {"r": "reasoning text", ...}}
        - Weather: {"GFS": {"p": 0.27, "r": "P(YES)=0.270 D+3"}, ...}
        - 旧版 weather: {"GFS": 0.27}（float降级兼容）
        """
        parts = []
        for model, v in (signal.model_votes or {}).items():
            if model.startswith("_"):
                if isinstance(v, dict):
                    r = v.get("r", "")
                    if r:
                        parts.append(f"[融合] {r}")
                continue
            if isinstance(v, dict):
                r = v.get("r") or v.get("reasoning", "")
            elif isinstance(v, (int, float)):
                r = f"P={v:.3f}"
            else:
                r = str(v) if v else ""
            if r:
                short_model = model.split("/")[-1][:20]
                parts.append(f"[{short_model}] {r}")
        return "\n".join(parts)[:600] if parts else ""

    def _extract_weather_context(self, signal: Signal) -> str:
        """从 model_votes 的第一个 vote 里提取 weather_context（如果有）。"""
        if signal.model_votes:
            for v in signal.model_votes.values():
                if isinstance(v, dict):
                    ctx = v.get("weather_context", "")
                    if ctx:
                        return str(ctx)[:300]
        return ""
        return ""
        return ""
        return ""
        return ""
        return ""
        return ""
        return ""
        return ""
        return ""
