"""
live_filters.py — 实盘信号过滤管线
职责：将 paper_trader.py 的关键风控过滤逻辑移植到实盘 main.py
防止：重复建仓、过度杠杆、低质量信号、余额透支

过滤器列表（按执行顺序）:
  1. 余额保底检查（balance_reserve_min_usdc）
  2. 持仓上限检查（max_open_positions）
  3. 赛道屏蔽（blocked_categories）
  4. 赛道配额（category_max_positions）
  5. 同事件去重（max_per_event_positions）
  6. 同市场去重（max_per_market_positions）
  7. 底层资产去重（max_per_asset_positions）
  8. 止损冷却期（stop_loss_cooldown_hours）
  9. 市场熔断器（circuit_breaker）
  9.5 赛道结算时限（settlement_max_hours）
  10. 天气赛道城市NO策略简化入场（price + EV + score）
  11. 方向调整（YES仓位缩减 + EV门槛提升）
  12. SmartCapitalEngine 统一仓位缩放（SCE关闭时回退旧参数）
  13. 硬上限 + 余额检查
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.utils import db
from src.utils.category_detector import detect_category
from src.utils.config_loader import get

logger = logging.getLogger(__name__)


def _sig_label(sig) -> str:
    """人性化市场标签：问题名称 + 市场ID（用于日志，禁止仅显示 market_id，禁止截断）。
    格式: "Will the high in Miami be above 82°F on April 14? [1953611]"
    """
    q = (getattr(sig, "question", "") or "").replace("\n", " ").strip()
    mid = getattr(sig, "market_id", "") or ""
    if q:
        return f"{q} [{mid}]"
    return mid or "(unknown)"


def filter_signals_for_live(
    signals: list,
    balance: float,
    market_lookup: dict,
) -> list:
    """
    对 SignalEngine 产出的信号列表进行风控过滤（实盘专用）。

    移植自 paper_trader.py 的关键过滤逻辑，确保实盘模式与 DRY_RUN 模式
    具有相同的风控保护层级。

    Args:
        signals: SignalEngine.generate_signals() 的输出
        balance: 当前 USDC 可用余额
        market_lookup: {market_id: market_dict} 市场数据

    Returns:
        过滤 + 调仓后的信号列表
    """
    pt_cfg = get("paper_trading") or {}

    # ── 余额保底检查 ─────────────────────────────────────────────
    # T190: 简化为绝对底线，删除 balance_reserve_pct 百分比保留（过度设计，阻碍资金流动）
    min_reserve = float(pt_cfg.get("balance_reserve_min_usdc", 0.10))
    if balance < min_reserve:
        logger.warning(
            f"[LiveFilter] 余额 {balance:.2f} < 保底线 {min_reserve:.2f} USDC，停止新开仓"
        )
        return []

    # ── 持仓上限检查 ─────────────────────────────────────────────
    open_bets = db.get_open_paper_bets()
    current_open = len(open_bets) if open_bets else 0
    max_open = int(pt_cfg.get("max_open_positions", 999))
    if current_open >= max_open:
        logger.warning(
            f"[LiveFilter] 当前开仓 {current_open} 已达上限 {max_open}，停止新开仓"
        )
        return []

    # ── 构建已有仓位索引 ─────────────────────────────────────────
    category_open: dict[str, int] = {}
    event_slug_open: dict[str, int] = {}
    asset_open: dict[str, int] = {}
    market_id_open: dict[str, int] = {}
    # T227: 右尾做空策略级跨周期去重 (RightTail)计数器
    right_tail_event_open: dict[str, int] = {}
    # Wave-2: PureCov 加买去重计数器（独立于 right_tail_event_open）
    pure_cov_event_open: dict[str, int] = {}
    for b in open_bets or []:
        cat = b.get("category", "unknown")
        category_open[cat] = category_open.get(cat, 0) + 1
        es = b.get("event_slug", "")
        if es:
            event_slug_open[es] = event_slug_open.get(es, 0) + 1
            # T227: 统计每个 event_slug 下的右尾做空持仓数
            _stag_b = b.get("strategy_tag", "") or ""
            if _stag_b.startswith(("weather_right_tail_", "weather_double_no_")):
                right_tail_event_open[es] = right_tail_event_open.get(es, 0) + 1
            # Wave-2: 统计 PureCov 加买持仓数
            elif _stag_b == "weather_pure_cov":
                pure_cov_event_open[es] = pure_cov_event_open.get(es, 0) + 1
        mid = b.get("market_id", "")
        if mid:
            market_id_open[mid] = market_id_open.get(mid, 0) + 1
        _asset = _extract_underlying_asset(b.get("question", ""), cat)
        if _asset:
            asset_open[_asset] = asset_open.get(_asset, 0) + 1

    # ── 逐信号过滤 ──────────────────────────────────────────────
    cat_max_pos: dict = pt_cfg.get("category_max_positions") or {}
    cat_ev_cfg: dict = pt_cfg.get("category_min_ev") or {}
    cat_conf_cfg: dict = pt_cfg.get("category_min_confidence") or {}
    blocked_cats = set(pt_cfg.get("blocked_categories", []))
    cat_event_cfg = pt_cfg.get("category_max_per_event") or {}
    cat_asset_cfg = pt_cfg.get("category_max_per_asset") or {}

    remaining_balance = balance
    filtered: list = []
    slots_left = max_open - current_open

    funnel = {
        "input": len(signals),
        "blocked_cat": 0,
        "cat_quota": 0,
        "event_dedup": 0,
        "market_dedup": 0,
        "asset_dedup": 0,
        "stop_cooldown": 0,
        "circuit_break": 0,
        "ev_reject": 0,
        "conf_reject": 0,
        "balance_insuf": 0,
        "weather_bucket_reject": 0,
        "passed": 0,
    }

    # ── 辅助：从 strategy_tag 推断赛道（弥补 market_lookup 缺失）──
    def _infer_category(sig):
        """T202: 统一的赛道推断：market_lookup → strategy_tag → fallback"""
        _m = market_lookup.get(sig.market_id, {})
        if _m:
            return detect_category(_m)
        _st = getattr(sig, "strategy_tag", "") or ""
        if _st.startswith("weather_"):
            return "weather"
        if _st == "copy_trade":
            return "default"
        return detect_category(_m) if _m is not None else "unknown"

    # ── 赛道优先排序（天气优先获取执行机会）───────────────────
    _priority_cfg = pt_cfg.get("signal_category_priority") or {}
    if _priority_cfg:

        def _sig_priority(s):
            _c = _infer_category(s)
            return int(_priority_cfg.get(_c, _priority_cfg.get("default", 99)))

        signals = sorted(signals, key=_sig_priority)
        # 统计排序后各赛道分布
        _sorted_cats: dict[str, int] = {}
        for _s in signals:
            _c = _infer_category(_s)
            _sorted_cats[_c] = _sorted_cats.get(_c, 0) + 1
        logger.info(f"[LiveFilter|Priority] 信号排序: {_sorted_cats}")

    for sig in signals:
        if slots_left <= 0:
            break

        market = market_lookup.get(sig.market_id, {})
        # T93/T202: 统一赛道推断（覆盖所有 weather_* strategy_tag）
        category = _infer_category(sig)
        _weather_pipeline_sig = (
            not market
        ) and category == "weather"  # T93天气已预过滤标记

        # 1. 赛道屏蔽
        if category in blocked_cats:
            funnel["blocked_cat"] += 1
            logger.info(
                f"[LiveFilter|BlockedCat] 赛道「{category}」已屏蔽，拒绝: "
                f"{_sig_label(sig)}"
            )
            continue

        # 2. 赛道配额
        cat_limit = cat_max_pos.get(category, cat_max_pos.get("default", 15))
        if category_open.get(category, 0) >= cat_limit:
            funnel["cat_quota"] += 1
            logger.info(
                f"[LiveFilter|CatQuota] 赛道「{category}」已满 "
                f"{category_open.get(category, 0)}/{cat_limit}，拒绝: "
                f"{_sig_label(sig)}"
            )
            continue

        # 3. 同事件去重
        # T227: 天气管线信号无 market_lookup 条目，需从 sig.event_slug 获取
        event_slug = market.get("_event_slug", "") or getattr(sig, "event_slug", "")
        max_per_event = int(
            cat_event_cfg.get(
                category,
                cat_event_cfg.get("default", 1),
            )
        )
        if event_slug and event_slug_open.get(event_slug, 0) >= max_per_event:
            funnel["event_dedup"] += 1
            logger.info(
                f"[LiveFilter|EventDedup] event={event_slug} "
                f"已有 {event_slug_open[event_slug]} 仓位 ≥ {max_per_event}，跳过"
            )
            continue

        # 3.5 T227: 右尾做空策略级跨周期去重 (RightTail)
        # right_tail_max_signals 控制每个城市日组(event_slug)最多持有的右尾做空仓位数
        # 解决跨周期重复入场：cycle N 下1单, cycle N+1 再下1单 → 总共2单违反限制
        _sig_stag = getattr(sig, "strategy_tag", "") or ""
        if (
            _sig_stag.startswith(("weather_right_tail_", "weather_double_no_"))
            and event_slug
        ):
            _ws_cfg = get("weather_strategy") or {}
            _dn_max = int(_ws_cfg.get("right_tail_max_signals", 2))
            _dn_existing = right_tail_event_open.get(event_slug, 0)
            if _dn_existing >= _dn_max:
                funnel["right_tail_dedup"] = funnel.get("right_tail_dedup", 0) + 1
                logger.info(
                    f"[LiveFilter|DoubleNoDedup] event={event_slug} "
                    f"已有 {_dn_existing} 个右尾做空仓位 ≥ 上限 {_dn_max}"
                    f"(right_tail_max_signals)，拒绝: {_sig_label(sig)}"
                )
                continue

        # 3.6 Wave-2: PureCov 加买独立去重（addon_max_signals）
        # weather_pure_cov 标签独立于 right_tail_event_open，不共享同一上限
        if _sig_stag == "weather_pure_cov" and event_slug:
            _pc_cfg2 = (get("weather_strategy") or {}).get("pure_coverage") or {}
            _addon_max = int(_pc_cfg2.get("addon_max_signals", 1))
            _addon_existing = pure_cov_event_open.get(event_slug, 0)
            if _addon_existing >= _addon_max:
                funnel["pure_cov_addon_dedup"] = (
                    funnel.get("pure_cov_addon_dedup", 0) + 1
                )
                logger.info(
                    f"[LiveFilter|PureCovAddonDedup] event={event_slug} "
                    f"已有 {_addon_existing} 个 PureCov加买仓位 ≥ 上限 {_addon_max}"
                    f"(addon_max_signals)，拒绝: {_sig_label(sig)}"
                )
                continue

        # 4. 同市场去重
        max_per_market = int(pt_cfg.get("max_per_market_positions", 1))
        if market_id_open.get(sig.market_id, 0) >= max_per_market:
            funnel["market_dedup"] += 1
            logger.info(
                f"[LiveFilter|MarketDedup] 市场已有 "
                f"{market_id_open[sig.market_id]}/{max_per_market} 仓位，拒绝: "
                f"{_sig_label(sig)}"
            )
            continue

        # 5. 底层资产去重
        underlying = _extract_underlying_asset(sig.question or "", category)
        if underlying:
            max_per_asset = int(
                cat_asset_cfg.get(
                    category,
                    cat_asset_cfg.get("default", 2),
                )
            )
            # 防御: max_per_asset<=0 时跳过去重（0=无限制，与config含义对齐）
            if max_per_asset > 0 and asset_open.get(underlying, 0) >= max_per_asset:
                funnel["asset_dedup"] += 1
                logger.info(
                    f"[LiveFilter|AssetDedup] {underlying} 已有 "
                    f"{asset_open.get(underlying, 0)} 仓位 ≥ {max_per_asset}，跳过"
                )
                continue

        # 6. 止损冷却期
        sl_cooldown = float(pt_cfg.get("stop_loss_cooldown_hours", 6.0))
        if sl_cooldown > 0 and db.had_recent_stop_loss(sig.market_id, sl_cooldown):
            funnel["stop_cooldown"] += 1
            logger.info(
                f"[LiveFilter|Cooldown] {_sig_label(sig)} 在 "
                f"{sl_cooldown:.0f}h 冷却期内，跳过"
            )
            continue

        # 7. 市场熔断器
        cb_cfg = pt_cfg.get("circuit_breaker") or {}
        if cb_cfg.get("enabled", True):
            cb_max = int(cb_cfg.get("consecutive_stops", 3))
            cb_hours = float(cb_cfg.get("cooldown_hours", 24))
            recent_exits = db.get_recent_exits_for_market(
                sig.market_id, limit=cb_max, hours=cb_hours
            )
            if len(recent_exits) >= cb_max and all(
                e == "stop_loss" for e in recent_exits
            ):
                funnel["circuit_break"] += 1
                logger.info(
                    f"[LiveFilter|CircuitBreaker] {_sig_label(sig)} "
                    f"连续{cb_max}次止损，{cb_hours:.0f}h内禁止入场"
                )
                continue

        # 7.1 高位入场禁止（category-aware price ceiling）
        _ws_cfg_price = get("weather_strategy") or {}
        _cat_price_ceilings = {
            "sports": float((pt_cfg.get("sports") or {}).get("tail_max_price", 0.80)),
            # 天气 NO：与 weather_strategy.no_max_entry_price 一致
            "weather": float(_ws_cfg_price.get("no_max_entry_price", 0.80)),
        }
        _eff_max_entry = _cat_price_ceilings.get(
            category, float(pt_cfg.get("max_entry_price", 0.85))
        )
        # T187: EHCL 高赢尾桶策略使用自身的 max_price 作为入场价上限（豁免全局价格上限）
        _stag_price = getattr(sig, "strategy_tag", "") or ""
        if _stag_price == "weather_ehcl":
            _el_cfg = _ws_cfg_price.get("extreme_lock") or {}
            _eff_max_entry = float(_el_cfg.get("max_price", 0.90))
        elif _stag_price == "weather_pure_cov":
            # PureCoverage 使用其独立上限，避免与 weather 全局上限冲突。
            _pc_cfg = _ws_cfg_price.get("pure_coverage") or {}
            _eff_max_entry = float(_pc_cfg.get("no_max_entry_price", 0.80))
        _entry_price_val = getattr(sig, "expected_price", 0) or 0
        if _entry_price_val >= _eff_max_entry:
            funnel["price_ceiling"] = funnel.get("price_ceiling", 0) + 1
            logger.info(
                f"[LiveFilter|PriceCeiling] 入场价 {_entry_price_val:.3f}"
                f" >= 上限 {_eff_max_entry:.2f} ({category})，"
                f"利润空间仅 {(1-_entry_price_val)*100:.1f}¢/share"
                f"（部分成交风险+手续费侵蚀），拒绝: {_sig_label(sig)}"
            )
            continue

        # 7.2 手续费/滑点预评估（确保扣费后仍有正收益）
        # T33: BkFollow 信号的 EV 已在生成时扣除实际费率，跳过重复扣费
        _stag = getattr(sig, "strategy_tag", "") or ""
        if _stag == "bk_follow":
            # BkFollow EV 已含费用扣除，仅检查 EV >= 0
            if sig.ev < 0:
                funnel["fee_reject"] = funnel.get("fee_reject", 0) + 1
                logger.info(
                    f"[LiveFilter|FeeCheck] BkFollow EV={sig.ev:.4f}<0，"
                    f"拒绝: {_sig_label(sig)}"
                )
                continue
        elif _stag.startswith(("weather_right_tail", "weather_double_no")):
            # T208完全修复: 天气RightTail/DoubleNO完全豁免FeeCheck
            # WCS score ≥ min_score 是唯一质量门槛，EV轻微负值（如-0.015）
            # 属于市场spread不确定区间，不应作为拒绝依据
            pass  # 无条件通过，不执行任何EV检查
        elif _stag == "weather_ehcl":
            # T208完全修复: EHCL完全豁免FeeCheck
            # EHCL尾桶质量门槛（max_price + extreme桶过滤）已验证信号质量
            pass  # 无条件通过，不执行任何EV检查
        else:
            # T36: 使用 per-category 费率 + 官方公式 feeRate × p × (1-p)
            # 参考: docs.polymarket.com/trading/fees
            # 买入taker付费(份额扣), 卖出taker付费(USDC扣), maker不付费
            # 入场决策仅扣买入单边费（卖出费取决于未知的卖出价）
            # 优先使用 signal_engine 预计算的动态费率（来自市场 feeSchedule）
            _sig_params = getattr(sig, "strategy_params", None) or {}
            _precomputed_fee_rate = _sig_params.get("_fee_rate")
            if _precomputed_fee_rate is not None:
                _cat_fee_rate = float(_precomputed_fee_rate)
            else:
                _fee_rates = pt_cfg.get("taker_fee_rates") or {}
                _fee_default = float(pt_cfg.get("taker_fee_rate_default", 0.05))
                _cat_fee_rate = float(_fee_rates.get(category, _fee_default))
            _entry_p = getattr(sig, "expected_price", 0.5) or 0.5
            _actual_fee_pct = _cat_fee_rate * _entry_p * (1.0 - _entry_p)
            _min_net_edge = float(pt_cfg.get("min_net_edge_after_fees", 0.01))
            # 买入单边手续费
            _total_fee_cost = _actual_fee_pct
            _net_edge = sig.ev - _total_fee_cost
            if _net_edge < _min_net_edge:
                funnel["fee_reject"] = funnel.get("fee_reject", 0) + 1
                logger.info(
                    f"[LiveFilter|FeeCheck] EV={sig.ev:.3f} - 买入费"
                    f"{_total_fee_cost:.4f}(feeRate={_cat_fee_rate},p={_entry_p:.2f})"
                    f" = 净edge {_net_edge:.4f}"
                    f" < 最低 {_min_net_edge:.3f}，拒绝: {_sig_label(sig)}"
                )
                continue

        # 7.5 T257/258/259: 全赛道结算时限过滤（统一≤36小时）
        _settlement_hours_cfg = pt_cfg.get("settlement_max_hours") or {}
        _max_hours = float(_settlement_hours_cfg.get(category, 0))
        # T93: WeatherPipeline天气信号已在WeatherScanner中预过滤时间约束，跳过重复检查
        if _max_hours > 0 and not _weather_pipeline_sig:
            _s_end = market.get("endDate") or market.get("endDateIso", "")
            if _s_end:
                try:
                    _s_end_dt = datetime.fromisoformat(_s_end.replace("Z", "+00:00"))
                    _s_hours = (
                        _s_end_dt - datetime.now(timezone.utc)
                    ).total_seconds() / 3600
                    if _s_hours > _max_hours:
                        funnel["settlement_expiry"] = (
                            funnel.get("settlement_expiry", 0) + 1
                        )
                        logger.info(
                            f"[LiveFilter|SettlementExpiry] {category} "
                            f"距结算{_s_hours:.0f}h > "
                            f"{_max_hours:.0f}h限制，跳过: "
                            f"{_sig_label(sig)}"
                        )
                        continue
                except (ValueError, TypeError):
                    funnel["settlement_expiry"] = funnel.get("settlement_expiry", 0) + 1
                    logger.debug(
                        f"[LiveFilter|SettlementExpiry] 无法解析endDate，安全跳过"
                    )
                    continue
            else:
                funnel["settlement_expiry"] = funnel.get("settlement_expiry", 0) + 1
                logger.debug(
                    f"[LiveFilter|SettlementExpiry] 无endDate，跳过: "
                    f"{_sig_label(sig)}"
                )
                continue

        # 9. 赛道 EV 门槛
        # T206追加修正：天气 pipeline 信号已有严格的 WCS score / EHCL 质量门槛，
        # EVReject 对其为冗余二次过滤（与 FeeCheck 豁免逻辑一致）。仅检查 EV ≥ 0。
        _stag_ev = getattr(sig, "strategy_tag", "") or ""
        if _stag_ev.startswith("weather_"):
            # T208完全修复: 天气pipeline信号完全豁免EVReject
            # WCS score / EHCL 已在上游做全面质量控制，EVReject是冗余三重过滤
            # EV计算基于fusion_p（含skill_factor+pos_mult惩罚），轻微负值不代表无边际
            pass  # 无条件通过
        else:
            min_ev = float(cat_ev_cfg.get(category, cat_ev_cfg.get("default", 1.0)))
            if sig.ev < min_ev:
                funnel["ev_reject"] += 1
                logger.info(
                    f"[LiveFilter|EVReject] EV={sig.ev:.4f} < 门槛{min_ev:.3f} "
                    f"({category})，拒绝: {_sig_label(sig)}"
                )
                continue

        # 10. 赛道置信度门槛
        min_conf = float(cat_conf_cfg.get(category, cat_conf_cfg.get("default", 1.0)))
        if sig.confidence < min_conf:
            funnel["conf_reject"] += 1
            logger.info(
                f"[LiveFilter|ConfReject] 置信度={sig.confidence:.3f} < "
                f"门槛{min_conf:.3f} ({category})，拒绝: "
                f"{_sig_label(sig)}"
            )
            continue

        # 11+14+15. T134: 统一由 SmartCapitalEngine 管控仓位缩放
        #   SCE 综合了 max_bet_pct × category_multiplier × hard_max_bet_usdc
        # T138: YesAmbush 使用固定仓位 ambush_size_usdc，绕过 Kelly-based cat_max 裁剪
        #   原因：cat_max 基于余额动态计算（如余额$24 → cat_max=$1.92），会截断固定埋伏仓位
        from src.utils.smart_capital import SmartCapitalEngine

        _min_bet = float(get("executor.min_bet_usdc") or 2.0)
        if getattr(sig, "_bet_bucket", None) == "YesAmbush":
            # YesAmbush 固定仓位 — 重新读取 ambush_size_usdc（与信号生成时一致）
            _ws_cfg_lf = get("weather_strategy") or {}
            _pt_cfg_lf = get("paper_trading") or {}
            _hard_max = float(_pt_cfg_lf.get("hard_max_bet_usdc", 25.0))
            sig.size_usdc = min(
                float(_ws_cfg_lf.get("ambush_size_usdc", 2.4)), _hard_max
            )
        else:
            cat_max = SmartCapitalEngine.get_cat_max_usdc(balance, category)
            sig.size_usdc = min(sig.size_usdc, cat_max)

        # T178/T179: 天气NO投注压缩 — 仅保留“刚好可下单”的最小金额
        # 目标 shares = max(post_buy_topup_safety_floor_shares, min_order_size_shares + buffer)
        # 避免天气 NO 单在高余额下被 SCE 放大，占用过多资金。
        _wcs_cfg_lf = get("weather_city_scanner") or {}
        _no_compress = bool(_wcs_cfg_lf.get("weather_no_compress_sizing", True))
        if (
            _no_compress
            and category == "weather"
            and sig.outcome == "NO"
            and sig.expected_price > 0
        ):
            _min_shares = float(get("executor.min_order_size_shares") or 5.0)
            _safety_floor = float(
                get("executor.post_buy_topup_safety_floor_shares") or 5.1
            )
            _shares_buf = float(_wcs_cfg_lf.get("weather_min_shares_buffer", 0.35))
            _target_shares = max(_safety_floor, _min_shares + _shares_buf)
            _compressed_size = round(_target_shares * sig.expected_price, 2)
            _compressed_size = max(_compressed_size, _min_bet)
            if _compressed_size < sig.size_usdc:
                logger.info(
                    f"[LiveFilter|NOCompress] {_sig_label(sig)}: "
                    f"SCE=${sig.size_usdc:.2f} → 压缩=${_compressed_size:.2f} "
                    f"(target_shares=max({_safety_floor:.2f}, {_min_shares:.2f}+{_shares_buf:.2f})={_target_shares:.2f}, "
                    f"price={sig.expected_price:.3f})"
                )
                sig.size_usdc = _compressed_size

        # T178追加: 同结算日天气敞口上限，避免同一天气到期市场占用过多资金
        _max_exp_pct = float(_wcs_cfg_lf.get("weather_max_daily_exposure_pct", 0.30))
        if category == "weather" and _max_exp_pct > 0:

            def _expiry_day(raw: str) -> str:
                if not raw:
                    return ""
                try:
                    return (
                        datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        .date()
                        .isoformat()
                    )
                except Exception:
                    return ""

            def _extract_date_from_question(q: str) -> str:
                """T181: 从天气市场标题提取日期作为 fallback（修复 date=? bug）。
                格式: '... on April 11?' 或 '... on April 11, 2026?'"""
                import re as _re

                _month_map = {
                    "january": 1,
                    "february": 2,
                    "march": 3,
                    "april": 4,
                    "may": 5,
                    "june": 6,
                    "july": 7,
                    "august": 8,
                    "september": 9,
                    "october": 10,
                    "november": 11,
                    "december": 12,
                }
                m = _re.search(
                    r"on\s+(january|february|march|april|may|june|july|august|"
                    r"september|october|november|december)\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?",
                    q,
                    _re.IGNORECASE,
                )
                if not m:
                    return ""
                _mon = _month_map.get(m.group(1).lower(), 0)
                _day = int(m.group(2))
                _year = (
                    int(m.group(3)) if m.group(3) else datetime.now(timezone.utc).year
                )
                try:
                    return f"{_year:04d}-{_mon:02d}-{_day:02d}"
                except Exception:
                    return ""

            # T181: 日期提取链 — ISO字段 → question文本解析
            _sig_expiry_day = _expiry_day(
                market.get("endDate")
                or market.get("endDateIso", "")
                or getattr(sig, "end_date_iso", "")
            )
            if not _sig_expiry_day:
                _sig_expiry_day = _extract_date_from_question(
                    getattr(sig, "question", "") or ""
                )

            def _same_day_weather_size(item) -> float:
                if (item.get("category") or "") != "weather":
                    return 0.0
                if not _sig_expiry_day:
                    # T181: 仍无法确定日期时，保守跳过该持仓（不计入敞口）
                    return 0.0
                _bet_day = _expiry_day(item.get("market_end_date") or "")
                if not _bet_day:
                    # T181: 已有持仓日期缺失时，从 question 提取
                    _bet_day = _extract_date_from_question(item.get("question") or "")
                if _bet_day and _bet_day != _sig_expiry_day:
                    return 0.0
                return float(item.get("size_usdc") or 0)

            _weather_open_total = sum(
                _same_day_weather_size(b) for b in (open_bets or [])
            )
            _weather_this_cycle = 0.0
            for s in filtered:
                if (getattr(s, "strategy_tag", "") or "").startswith("weather_"):
                    _s_day = _expiry_day(getattr(s, "end_date_iso", "") or "")
                    if not _s_day:
                        _s_day = _extract_date_from_question(
                            getattr(s, "question", "") or ""
                        )
                    if not _sig_expiry_day or not _s_day or _s_day == _sig_expiry_day:
                        _weather_this_cycle += s.size_usdc

            _weather_total = _weather_open_total + _weather_this_cycle
            _max_exposure = balance * _max_exp_pct
            if _weather_total + sig.size_usdc > _max_exposure:
                _remaining_budget = max(0, _max_exposure - _weather_total)
                if _remaining_budget < _min_bet:
                    funnel["weather_exposure_cap"] = (
                        funnel.get("weather_exposure_cap", 0) + 1
                    )
                    logger.info(
                        f"[LiveFilter|ExposureCap] 天气同日敞口已达上限: "
                        f"date={_sig_expiry_day or '?'} 已用=${_weather_total:.2f} + 本单=${sig.size_usdc:.2f} "
                        f"> 上限=${_max_exposure:.2f} ({_max_exp_pct:.0%} of ${balance:.2f})"
                    )
                    continue
                sig.size_usdc = round(_remaining_budget, 2)
                logger.info(
                    f"[LiveFilter|ExposureCap] 天气同日敞口裁剪: "
                    f"date={_sig_expiry_day or '?'} 剩余预算=${_remaining_budget:.2f}"
                )

        # 小余额场景：裁剪后低于 CLOB 最低限额时，使用 CLOB 最低限额
        if sig.size_usdc < _min_bet and remaining_balance >= _min_bet:
            sig.size_usdc = _min_bet

        # 最终余额检查
        if sig.size_usdc > remaining_balance:
            funnel["balance_insuf"] += 1
            logger.info(
                f"[LiveFilter|BalanceInsuf] 需${sig.size_usdc:.2f} > "
                f"剩余${remaining_balance:.2f}，拒绝: "
                f"{_sig_label(sig)}"
            )
            continue
        if sig.size_usdc < _min_bet:
            funnel["balance_insuf"] += 1
            logger.info(
                f"[LiveFilter|BalanceInsuf] 仓位${sig.size_usdc:.2f} < "
                f"最低${_min_bet:.2f}，拒绝: "
                f"{_sig_label(sig)}"
            )
            continue

        # ── 通过所有过滤，加入执行列表 ────────────────────────────
        filtered.append(sig)
        remaining_balance -= sig.size_usdc
        slots_left -= 1
        funnel["passed"] += 1

        # 更新索引（防止同周期内重复）
        category_open[category] = category_open.get(category, 0) + 1
        if event_slug:
            event_slug_open[event_slug] = event_slug_open.get(event_slug, 0) + 1
            # T227: 更新右尾做空计数器
            if _sig_stag.startswith(("weather_right_tail_", "weather_double_no_")):
                right_tail_event_open[event_slug] = (
                    right_tail_event_open.get(event_slug, 0) + 1
                )
            # Wave-2: 更新 PureCov 加买计数器
            elif _sig_stag == "weather_pure_cov":
                pure_cov_event_open[event_slug] = (
                    pure_cov_event_open.get(event_slug, 0) + 1
                )
        market_id_open[sig.market_id] = market_id_open.get(sig.market_id, 0) + 1
        if underlying:
            asset_open[underlying] = asset_open.get(underlying, 0) + 1

    # 漏斗日志（人性化格式：仅显示有拦截的步骤）
    _steps = [
        ("赛道屏蔽(被封赛道)", funnel["blocked_cat"]),
        ("赛道配额(已满)", funnel["cat_quota"]),
        ("事件去重(同事件已有仓)", funnel["event_dedup"]),
        ("市场去重(同市场已有仓)", funnel["market_dedup"]),
        ("资产去重(同资产已有仓)", funnel["asset_dedup"]),
        ("止损冷却(近期止损)", funnel["stop_cooldown"]),
        ("市场熔断(连续止损)", funnel["circuit_break"]),
        ("高价拒绝(利润空间不足)", funnel.get("price_ceiling", 0)),
        ("费用拒绝(手续费>收益)", funnel.get("fee_reject", 0)),
        ("结算时限(距结算太久)", funnel.get("settlement_expiry", 0)),
        ("天气敞口(同日上限)", funnel.get("weather_exposure_cap", 0)),
        ("EV不足(期望值低)", funnel["ev_reject"]),
        ("置信不足(AI不确定)", funnel["conf_reject"]),
        ("余额不足", funnel["balance_insuf"]),
    ]
    _active = [f"{name}={cnt}" for name, cnt in _steps if cnt > 0]
    _dropped = funnel["input"] - funnel["passed"]
    logger.info(
        f"[LiveFilter] 漏斗汇总: 输入={funnel['input']} → "
        f"通过={funnel['passed']}，"
        + (f"淘汰={_dropped} [{', '.join(_active)}]" if _active else "全部通过")
    )

    return filtered


def _extract_underlying_asset(question: str, category: str) -> Optional[str]:
    """从问题文本中提取底层资产标识（用于资产级别去重）。"""
    q = question.lower()
    if category == "crypto":
        # Bitcoin Up or Down → "bitcoin"
        for coin in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp"]:
            if coin in q:
                return f"crypto_{coin}"
    elif category == "weather":
        # "Will the highest temperature in Paris be 15°C on March 22?"
        # → "weather_paris" (城市级去重，防止同城市跨日期过度集中)
        m = re.search(r"temperature in (.+?)\b(?:\s+be\b)", q)
        if m:
            city = re.sub(r"\s+", "_", m.group(1).strip())
            return f"weather_{city}"
    elif category == "sports":
        # 提取队名组合作为 key
        m = re.match(r"^(.+?)\s+vs\.?\s+(.+?)[\s\?]", q)
        if m:
            return f"sports_{m.group(1).strip()}_{m.group(2).strip()}"
    return None
