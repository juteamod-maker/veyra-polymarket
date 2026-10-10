"""
outcome_tracker.py — 模拟投注结果追踪器
职责：
  - 定期轮询 Polymarket Gamma API，检查 paper_bets 中未解决的押注
  - 在市场解决后自动更新 resolved_outcome、pnl、is_win 等字段
  - 批量查询（每次最多 50 个，避免 rate limit）
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from src.utils import db
from src.utils.config_loader import get

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
OPEN_METEO_API = "https://api.open-meteo.com/v1/forecast"

# T189: CertaintyHold 日志节流 — {bet_id: last_log_timestamp}
_certainty_hold_last_log: dict[str, float] = {}
# T235: CertaintyHold 超时追踪 — {bet_id: first_hold_timestamp}
_certainty_hold_since: dict[str, float] = {}
# T189: Reconcile 日志节流 — {bet_id: last_log_timestamp}
_reconcile_last_log: dict[str, float] = {}


def _resolve_exit_order_type(category: str, entry_order_type: str = "taker") -> str:
    """T228追加: 退出费率使用 sell_mode 配置推断，而非入场 order_type。

    当入场为 maker（0费用）但 sell_mode 为 taker 时，退出费率应按 taker 计算。
    DRY RUN (simulated) 保持原样不改。
    """
    if entry_order_type == "simulated":
        return "simulated"
    sell_cfg = get("order_execution.sell_mode") or {}
    sell_per_cat = sell_cfg.get("per_category") or {}
    mode = sell_per_cat.get(category, "") or sell_cfg.get("default") or "taker"
    return "maker" if mode == "maker" else "taker"


# 城市坐标表，用于自解析天气市场结果
CITY_COORDS: dict[str, tuple[float, float]] = {
    # 北美
    "New York City": (40.7128, -74.0060),
    "New York": (40.7128, -74.0060),
    "Los Angeles": (34.0522, -118.2437),
    "Chicago": (41.8781, -87.6298),
    "Houston": (29.7604, -95.3698),
    "Phoenix": (33.4484, -112.0740),
    "Philadelphia": (39.9526, -75.1652),
    "San Antonio": (29.4241, -98.4936),
    "San Diego": (32.7157, -117.1611),
    "Dallas": (32.7767, -96.7970),
    "San Jose": (37.3382, -121.8863),
    "Austin": (30.2672, -97.7431),
    "San Francisco": (37.7749, -122.4194),
    "Seattle": (47.6062, -122.3321),
    "Denver": (39.7392, -104.9903),
    "Miami": (25.7617, -80.1918),
    "Atlanta": (33.7490, -84.3880),
    "Boston": (42.3601, -71.0589),
    "Minneapolis": (44.9778, -93.2650),
    "Nashville": (36.1627, -86.7816),
    "Portland": (45.5152, -122.6784),
    "Las Vegas": (36.1699, -115.1398),
    "Charlotte": (35.2271, -80.8431),
    "Detroit": (42.3314, -83.0458),
    "Washington DC": (38.9072, -77.0369),
    "Washington": (38.9072, -77.0369),
    "Toronto": (43.6532, -79.3832),
    "Mexico City": (19.4326, -99.1332),
    # 南美
    "Sao Paulo": (-23.5505, -46.6333),
    "Buenos Aires": (-34.6118, -58.4173),
    "Rio de Janeiro": (-22.9068, -43.1729),
    "Bogota": (4.7110, -74.0721),
    "Lima": (-12.0464, -77.0428),
    "Santiago": (-33.4489, -70.6693),
    # 欧洲
    "London": (51.5074, -0.1278),
    "Paris": (48.8566, 2.3522),
    "Berlin": (52.5200, 13.4050),
    "Madrid": (40.4168, -3.7038),
    "Rome": (41.9028, 12.4964),
    "Amsterdam": (52.3676, 4.9041),
    "Istanbul": (41.0082, 28.9784),
    "Moscow": (55.7558, 37.6173),
    "Vienna": (48.2082, 16.3738),
    "Munich": (48.1351, 11.5820),
    "Warsaw": (52.2297, 21.0122),
    "Athens": (37.9838, 23.7275),
    "Lisbon": (38.7223, -9.1393),
    # 中东 / 非洲
    "Dubai": (25.2048, 55.2708),
    "Cairo": (30.0444, 31.2357),
    "Riyadh": (24.7136, 46.6753),
    "Tel Aviv": (32.0853, 34.7818),
    # 亚洲
    "Tokyo": (35.6762, 139.6503),
    "Shanghai": (31.2304, 121.4737),
    "Beijing": (39.9042, 116.4074),
    "Seoul": (37.5665, 126.9780),
    "Hong Kong": (22.3193, 114.1694),
    "Singapore": (1.3521, 103.8198),
    "Mumbai": (19.0760, 72.8777),
    "Delhi": (28.7041, 77.1025),
    "Bangkok": (13.7563, 100.5018),
    "Taipei": (25.0330, 121.5654),
    "Osaka": (34.6937, 135.5023),
    "Jakarta": (-6.2088, 106.8456),
    "Manila": (14.5995, 120.9842),
    "Kuala Lumpur": (3.1390, 101.6869),
    # 大洋洲
    "Sydney": (-33.8688, 151.2093),
    "Melbourne": (-37.8136, 144.9631),
    "Auckland": (-36.8485, 174.7633),
}


def _get_city_coords_from_config(city_name: str) -> Optional[tuple[float, float]]:
    """T276: 从 config.json weather_cities 加载城市坐标，优先于硬编码 CITY_COORDS。"""
    key = city_name.lower().strip()
    cities_cfg = get("weather_cities.cities") or []
    for city in cities_cfg:
        if not city.get("enabled", True):
            continue
        if city.get("name", "").lower() == key or city.get("slug", "").lower() == key:
            lat, lon = city.get("lat"), city.get("lon")
            if lat is not None and lon is not None:
                return (float(lat), float(lon))
    # slug 连字符匹配: "buenos aires" → "buenos-aires"
    slug_key = key.replace(" ", "-")
    for city in cities_cfg:
        if not city.get("enabled", True):
            continue
        if city.get("slug", "").lower() == slug_key:
            lat, lon = city.get("lat"), city.get("lon")
            if lat is not None and lon is not None:
                return (float(lat), float(lon))
    return None


def get_city_coords(city_name: str) -> Optional[tuple[float, float]]:
    """获取城市坐标。查找顺序: config → CITY_COORDS 硬编码 fallback。"""
    coords = _get_city_coords_from_config(city_name)
    if coords:
        return coords
    return CITY_COORDS.get(city_name)


_BATCH_SIZE = 50
_REQUEST_TIMEOUT = 15

# PriceDeviation 日志去重：同一 (market_id, label) 每 60 秒仅告警一次
_price_deviation_seen: dict[str, float] = {}
_PRICE_DEVIATION_COOLDOWN = 60.0

# PriceDeviation 参考价缓存 {market_id: (ref_price, monotonic_timestamp)}
_ref_price_cache: dict[str, tuple[float, float]] = {}


class OutcomeTracker:
    """
    结果追踪器，检查未解决的 paper_bets 并更新市场结果。
    同时实现主动止盈（Take Profit）：接近二元市场天花板时立即卖出。
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        # 追踪每个持仓的历史最高卖出价（trailing take-profit 用）
        self._peak_prices: dict[str, float] = {}
        # Trailing Floor 保护：一旦仓位盈利超过 trailing_min_profit_pct，
        # 锁定 floor = entry_price，防止赢家变输家（死亡地带保护）
        self._floor_prices: dict[str, float] = {}
        # ── 流动性真空止损防护 ──────────────────────────────
        # armed_ticks: 连续满足止损条件的 tick 数（多周期确认）
        self._armed_ticks: dict[str, int] = {}
        # ExitPolicy 可组合退出策略链
        self._exit_policy_chain = self._build_policy_chain()
        # T70: 已进入低价卖出暂停的 bet_id 集合（避免每周期重复日志）
        self._dead_paused_bets: set[str] = set()
        # T195: 已记录止损事件的 (bet_id, trigger_type) 集合（避免重复写入 stop_loss_events 表）
        self._sl_logged_events: set[tuple[str, str]] = set()
        # T101: P1-9 连续价格为 None 计数器
        self._price_null_counts: dict[str, int] = {}
        # Phase 57: 从 DB 恢复 trailing 状态（防止重启后 floor/peak 丢失）
        self._restore_trailing_state()

    def _restore_trailing_state(self) -> None:
        """从 DB 读取所有未结算仓位的 trailing + CertaintyHold 状态，恢复内存字典。"""
        try:
            rows = db.get_open_bets_trailing_state()
            _ch_restored = 0
            for r in rows:
                bet_id = r.get("bet_id") or ""
                if not bet_id:
                    continue
                if r.get("peak_price") is not None:
                    self._peak_prices[bet_id] = float(r["peak_price"])
                if r.get("floor_price") is not None:
                    self._floor_prices[bet_id] = float(r["floor_price"])
                # T235追加: 恢复 CertaintyHold 计时器（ISO UTC → Unix timestamp）
                if r.get("certainty_hold_since"):
                    try:
                        from datetime import datetime, timezone

                        dt = datetime.fromisoformat(r["certainty_hold_since"])
                        _certainty_hold_since[bet_id] = dt.timestamp()
                        _ch_restored += 1
                    except (ValueError, TypeError):
                        pass
            if self._peak_prices or self._floor_prices or _ch_restored:
                logger.info(
                    f"[OutcomeTracker] trailing 状态已恢复: "
                    f"{len(self._peak_prices)} peak, {len(self._floor_prices)} floor"
                    + (f", {_ch_restored} certainty_hold" if _ch_restored else "")
                )
        except Exception as e:
            logger.warning(f"[OutcomeTracker] trailing 状态恢复失败（重启清零）: {e}")

    @staticmethod
    def _build_policy_chain():
        """构建 ExitPolicy 策略链。"""
        from src.exit_policy.base import PolicyChain
        from src.exit_policy.policies import (
            CityNoScoreHoldPolicy,
            WeatherConfidenceHoldPolicy,
        )

        policies = [
            WeatherConfidenceHoldPolicy(),
            CityNoScoreHoldPolicy(),
        ]
        chain = PolicyChain(policies)
        logger.info(f"[ExitPolicy] 策略链已加载: {chain.policy_names}")
        return chain

    @staticmethod
    def _calc_hours_to_expiry(bet: dict, now_ts: float) -> float | None:
        """计算距离市场结算的小时数（时区感知）。

        T211 修复：天气赛道纯日期格式（如 '2026-04-14'）使用城市本地时区
        的当日 23:59 作为结算时间，而非 VPS 系统时区 midnight。

        非天气赛道纯日期格式统一用 UTC midnight。
        ISO 格式（含 'T'）直接解析。
        """
        end_date_str = (bet.get("market_end_date") or "").strip()
        if not end_date_str:
            return None
        try:
            is_date_only = "T" not in end_date_str

            if is_date_only:
                # 纯日期格式（如 '2026-04-14'）
                end_dt = datetime.fromisoformat(end_date_str)
                bet_category = (bet.get("category") or "").lower()

                if bet_category == "weather":
                    # T211: 天气赛道 — 用城市本地 EOD 作为结算时间
                    try:
                        from src.utils.weather_fetcher import (
                            get_city_utc_offset,
                            parse_weather_question,
                        )

                        parsed = parse_weather_question(bet.get("question", ""))
                        if parsed:
                            offset = get_city_utc_offset(parsed["city"])
                            if offset is not None:
                                # 城市本地 23:59:59 → UTC
                                eod_utc = datetime(
                                    end_dt.year,
                                    end_dt.month,
                                    end_dt.day,
                                    23,
                                    59,
                                    59,
                                    tzinfo=timezone.utc,
                                ) - timedelta(hours=offset)
                                return (eod_utc.timestamp() - now_ts) / 3600.0
                    except Exception:
                        pass
                    # fallback: 用 23:59 UTC
                    eod_utc = end_dt.replace(
                        hour=23, minute=59, second=59, tzinfo=timezone.utc
                    )
                    return (eod_utc.timestamp() - now_ts) / 3600.0

                # 非天气：UTC midnight（明确时区避免 VPS 系统时区偏差）
                end_dt_utc = end_dt.replace(tzinfo=timezone.utc)
                return (end_dt_utc.timestamp() - now_ts) / 3600.0

            # ISO 格式含时间（如 '2026-04-14T12:00:00Z'）
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            return (end_dt.timestamp() - now_ts) / 3600.0
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def check_and_update(self) -> int:
        """
        检查并更新所有未解决的 paper_bets。
        返回：本次更新的已解决条目数量。
        """
        open_bets = db.get_open_paper_bets()
        if not open_bets:
            logger.debug("[OutcomeTracker] 无未解决的模拟投注")
            return 0

        logger.info(f"[OutcomeTracker] 检查 {len(open_bets)} 个未解决投注...")

        # 按 market_id 分组（节省 API 调用）
        market_bets: dict[str, list[dict]] = {}
        for bet in open_bets:
            mid = bet["market_id"]
            market_bets.setdefault(mid, []).append(bet)

        # 批量查询市场状态
        market_ids = list(market_bets.keys())
        _slug_map = {
            b["market_id"]: b.get("market_slug", "")
            for b in open_bets
            if b.get("market_slug")
        }
        market_status = self._fetch_market_statuses(market_ids, slug_map=_slug_map)

        updated = 0
        for market_id, bets in market_bets.items():
            status = market_status.get(market_id)
            if status is None:
                logger.debug(f"[OutcomeTracker] 未获取到市场状态: {market_id[:16]}...")
                continue

            resolved_outcome = ""
            resolved_at = time.time()

            if status.get("resolved", False):
                # 官方已解决：从 Polymarket API 读取结果
                resolved_outcome = self._parse_outcome(status)
                if not resolved_outcome:
                    # outcome 字段为空时，尝试通过 outcomePrices 推断
                    resolved_outcome = self._resolve_by_outcome_prices(status)
                if not resolved_outcome:
                    logger.warning(
                        f"[OutcomeTracker] 无法解析官方结果: {market_id[:16]}..."
                    )
                    continue
                resolution_time = status.get("resolutionTime") or status.get(
                    "endDate", ""
                )
                resolved_at = self._parse_timestamp(resolution_time)

            elif status.get("closed", False) or self._is_market_expired(status):
                # 市场已关闭或 endDate 已过（官方尚未解决）→ 尝试自解析

                # ── Oracle 终态价格判断（实盘+DRY RUN 均适用）──────────────
                # 当 outcomePrices 已经是 [~0, ~1] 或 [~1, ~0]（Polymarket oracle 已
                # 完成定价），即使 resolved 字段因 API 元数据滞后仍为 None，也可安全
                # 关闭 DB 记录。这不属于"自解析"（不依赖外部推断），而是直接使用
                # Polymarket 自身 oracle 的终态价格。
                # 典型案例：closed=True, resolved=None, outcomePrices=["0","1"]
                _ORACLE_TERMINAL_THRESH = float(
                    (get("executor") or {}).get(
                        "oracle_terminal_thresh",
                        (get("outcome_tracker") or {}).get(
                            "oracle_terminal_thresh", 0.001
                        ),
                    )
                )
                _op_raw = status.get("outcomePrices", "")
                _op_list: list = []
                if isinstance(_op_raw, str):
                    try:
                        import json as _j

                        _op_list = _j.loads(_op_raw)
                    except Exception:
                        pass
                elif isinstance(_op_raw, list):
                    _op_list = _op_raw
                if len(_op_list) >= 2:
                    try:
                        _op0 = float(_op_list[0])
                        _op1 = float(_op_list[1])
                        _is_oracle_terminal = (
                            _op0 <= _ORACLE_TERMINAL_THRESH
                            and _op1 >= (1.0 - _ORACLE_TERMINAL_THRESH)
                        ) or (
                            _op1 <= _ORACLE_TERMINAL_THRESH
                            and _op0 >= (1.0 - _ORACLE_TERMINAL_THRESH)
                        )
                    except (ValueError, TypeError):
                        _is_oracle_terminal = False
                else:
                    _is_oracle_terminal = False

                if _is_oracle_terminal:
                    # Oracle 已明确结算：通过价格推断胜负，绕过实盘守卫
                    resolved_outcome = self._resolve_by_outcome_prices(status)
                    if resolved_outcome:
                        resolution_time = status.get("resolutionTime") or status.get(
                            "endDate", ""
                        )
                        resolved_at = self._parse_timestamp(resolution_time)
                        logger.info(
                            f"[OutcomeTracker] Oracle终态价格确认结算"
                            f" (prices={_op_list[:2]}): "
                            f"{market_id[:16]}... → {resolved_outcome}"
                        )
                        # 直接进入下方 for bet in bets 结算流程
                    else:
                        # oracle 价格无法推断结果（罕见），继续后续自解析
                        _is_oracle_terminal = False

                if not _is_oracle_terminal:
                    # ── 实盘安全守卫 ─────────────────────────────────────
                    # 实盘模式下，自解析只做DB标记不卖token → 导致幽灵仓位！
                    # 必须等平台官方解决(resolved=True)，或由SL/TP主动卖出。
                    # DRY RUN无链上资产，可以自解析。
                    from src.utils.config_loader import is_dry_run as _is_dry_run

                    if not _is_dry_run():
                        logger.info(
                            f"[OutcomeTracker] 实盘模式跳过自解析（等待平台官方结算）: "
                            f"{market_id[:16]}..."
                        )
                        continue

                    question = bets[0].get("question", "") if bets else ""
                    end_date = (
                        str(bets[0].get("market_end_date") or status.get("endDate", ""))
                        if bets
                        else ""
                    )
                    # 1) 天气市场自解析（Open-Meteo 实际温度数据）
                    resolved_outcome = self._self_resolve_weather_market(
                        question, end_date
                    )
                    # 2) 非天气市场 → 通过 outcomePrices 推断胜负
                    if not resolved_outcome:
                        resolved_outcome = self._resolve_by_outcome_prices(status)
                    if not resolved_outcome:
                        continue  # 暂时无法自解析，下轮再试

                    # Phase43: 交叉验证 — 自解析结果须与 outcomePrices 方向一致
                    # 否则极易出现系统误判（如 London 15°C 事故：自解析 NO 但实际 YES）
                    if not self._validate_self_resolution(resolved_outcome, status):
                        logger.warning(
                            f"[OutcomeTracker] 自解析与 outcomePrices 不一致, 跳过: "
                            f"{market_id[:16]}... → 自解析={resolved_outcome}, "
                            f"prices={status.get('outcomePrices')}"
                        )
                        continue

                    resolution_time = status.get("endDate") or end_date
                    resolved_at = self._parse_timestamp(resolution_time)
                    logger.info(
                        f"[OutcomeTracker] 自解析成功: {market_id[:16]}... → {resolved_outcome}"
                    )

            else:
                continue  # 市场仍在交易中，跳过

            for bet in bets:
                pnl, payout, is_win = self._calc_pnl(bet, resolved_outcome)
                try:
                    db.update_paper_bet_outcome(
                        bet_id=bet["bet_id"],
                        resolved_outcome=resolved_outcome,
                        resolved_at=resolved_at,
                        pnl=pnl,
                        payout=payout,
                        is_win=is_win,
                    )
                    updated += 1
                    status_str = "赢" if is_win else "输"
                    logger.info(
                        f"[OutcomeTracker] {status_str} | {bet['question'][:50]}... "
                        f"→ 预测={bet['outcome']}, 实际={resolved_outcome} | "
                        f"P&L={pnl:+.2f} USDC"
                    )
                    from src.utils.telegram_notifier import get_notifier

                    get_notifier().notify_settlement(
                        question=bet.get("question", ""),
                        our_outcome=bet.get("outcome", ""),
                        actual_outcome=resolved_outcome,
                        is_win=is_win,
                        pnl=pnl,
                        size_usdc=float(bet.get("size_usdc", 0)),
                    )
                    # [PRO] 体育结算：OddsPapi 独立验证 + CLV 边际记录
                    self._log_sports_settlement_verification(bet, resolved_outcome)
                except Exception as e:
                    logger.error(f"[OutcomeTracker] 更新失败 {bet['bet_id']}: {e}")

        logger.info(f"[OutcomeTracker] 本次更新 {updated} 条结果")
        return updated

    # ------------------------------------------------------------------
    # 止盈（Take Profit）接口
    # ------------------------------------------------------------------

    def check_profit_take_exits(
        self, open_bets: list[dict], market_data: dict[str, dict] | None = None
    ) -> list[dict]:
        """
        检查开放仓位是否达到止盈条件（智能多层策略）。

        ── 核心原则 ─────────────────────────────────────────────────────
        二元市场价格范围 0~1。策略追求"让利润奔跑、在回调时才收割"——
        避免在概率单向上升时过早卖出，错失结算 $1.00 的最大利润。

        ── 智能止盈层级（优先级从高到低）───────────────────────────────
        0. 持有到结算（Hold-to-Settlement）：
           概率 ≥ 0.95 且距结算 < 48h → 跳过所有止盈，等结算拿 $1.00
        1. 天花板止盈：价格 ≥ 0.97 → 卖出（除非满足 hold-to-settle）
        2. 天气快速止盈：低价入场→价格修正到目标值立即卖出
        2.5 Quick Exit: 低价入场(≤0.25)且小幅盈利即刻离场
        3. 标准止盈：中高价位入场(>0.25)且收益率≥50%→直接止盈
        4. 追踪止盈（阶梯Trailing）：从历史最高价回撤 N% 才卖出
           - 收益<30%: 回撤8%止盈 (原值)
           - 收益30–60%: 回撤12% (给更多空间)
           - 收益>60%: 回撤15% (大赢面前不被小波动震出)
           - 天气市场: 最低10%回调才止盈
        5. 临近到期止盈：距到期 <24h 且收益 ≥50%

        返回：应该平仓的投注列表（含 exit_price 字段）。
        """
        pt_cfg = get("profit_taking") or {}
        if not pt_cfg.get("enabled", True):
            return []
        if not open_bets:
            return []

        near_ceiling: float = pt_cfg.get("near_ceiling_threshold", 0.99)
        expiry_hours: float = pt_cfg.get("expiry_hours_threshold", 12.0)
        expiry_profit_pct: float = pt_cfg.get("expiry_profit_pct", 0.80)
        weather_quick_exit: float = pt_cfg.get("weather_quick_exit_price", 0.65)
        weather_quick_entry_max: float = pt_cfg.get("weather_quick_entry_max", 0.23)

        # Trailing take-profit 参数
        trailing_enabled: bool = pt_cfg.get("trailing_enabled", True)
        trailing_drop_pct: float = pt_cfg.get("trailing_drop_pct", 0.45)
        trailing_min_profit_pct: float = pt_cfg.get("trailing_min_profit_pct", 0.50)
        trailing_floor_min_profit_pct: float = pt_cfg.get(
            "trailing_floor_min_profit_pct", 0.50
        )
        # T65: 动态 Trailing Floor 参数
        trailing_floor_dynamic: bool = pt_cfg.get(
            "trailing_floor_dynamic_enabled", True
        )
        trailing_floor_retreat_pct: float = pt_cfg.get(
            "trailing_floor_retreat_pct", 0.20
        )
        sports_floor_retreat_pct: float = pt_cfg.get(
            "sports_trailing_floor_retreat_pct", 0.15
        )
        crypto_floor_retreat_pct: float = (pt_cfg.get("crypto") or {}).get(
            "trailing_floor_retreat_pct", 0.12
        )
        # standard_profit_take 入场价下限（低于此价格的仓位不触发标准止盈，改走 trailing/ceiling 路径）
        # 注意：quick_exit 功能已废弃删除；此参数保留是因为代码用来区分低价仓位与标准止盈路径
        quick_exit_entry_max: float = pt_cfg.get("quick_exit_entry_max", 0.25)

        # 标准止盈: 中高价位入场,收益达到阈值直接止盈
        profit_take_pct: float = pt_cfg.get("profit_take_pct", 0.99)

        # 阶梯 Trailing: 收益越高允许越多回调空间
        trailing_tiers: list = pt_cfg.get("trailing_tiers", [])
        weather_trailing_min_drop: float = pt_cfg.get("weather_trailing_min_drop", 0.45)

        # Hold-to-settlement 参数
        hold_threshold: float = pt_cfg.get("hold_to_settle_threshold", 0.90)
        hold_hours: float = pt_cfg.get("hold_to_settle_hours", 18.0)

        # CertaintyHold: 高价格赛道等待结算（结算收益 $1.00 > taker约 $0.95）
        _ch_threshold: float = float(pt_cfg.get("certainty_hold_threshold", 0.95))
        _ch_raw = pt_cfg.get("certainty_hold_categories", ["weather"])
        # 注意：不用 `or ["weather"]`，允许空列表 [] 表示"全部禁用"
        _ch_categories: list = [
            c.lower() for c in (_ch_raw if _ch_raw is not None else ["weather"])
        ]

        # T185: 天气赛道事件日轮动退出参数（extract once, outside loop）
        _sl_cfg = get("stop_loss") or {}
        _rotation_cfg = _sl_cfg.get("weather_rotation_exit") or {}
        _rotation_enabled: bool = bool(_rotation_cfg.get("enabled", True))
        _rotation_trigger_hours: float = float(_rotation_cfg.get("trigger_hours", 6.0))
        _rotation_min_return: float = float(_rotation_cfg.get("min_return_pct", -0.05))
        # T183: 接近确定性缓冲 — 价格接近 CertaintyHold 时跳过轮动退出
        _rotation_cert_buffer: float = float(
            _rotation_cfg.get("certainty_buffer", 0.10)
        )

        # 体育比赛进行中专用止盈参数
        sl_cfg_live = get("sports_live") or {}
        live_window: float = sl_cfg_live.get("game_window_hours", 4.0)
        live_post_expiry: float = sl_cfg_live.get("post_expiry_window_hours", 2.0)
        live_ceiling: float = sl_cfg_live.get("near_ceiling_threshold", 0.93)
        live_profit_pct: float = sl_cfg_live.get("profit_take_pct", 0.40)

        # Crypto 赛道专用止盈参数（更激进的早止盈策略）
        _cr_cfg: dict = pt_cfg.get("crypto") or {}
        _crypto_override: bool = _cr_cfg.get("override_enabled", False)
        _crypto_certainty: float = float(_cr_cfg.get("certainty_sell_threshold", 0.95))
        _crypto_ceiling: float = float(_cr_cfg.get("near_ceiling_threshold", 0.93))
        _crypto_profit_pct: float = float(_cr_cfg.get("profit_take_pct", 0.30))
        _crypto_trail_drop: float = float(_cr_cfg.get("trailing_drop_pct", 0.15))
        _crypto_trail_min: float = float(_cr_cfg.get("trailing_min_profit_pct", 0.10))
        _crypto_hold_thresh: float = float(
            _cr_cfg.get("hold_to_settle_threshold", 0.93)
        )
        _crypto_hold_hrs: float = float(_cr_cfg.get("hold_to_settle_hours", 24.0))
        _crypto_max_hold_hrs: float = float(_cr_cfg.get("max_hold_hours", 72.0))

        # 批量获取当前市场价格（支持外部传入缓存，避免同一周期重复请求）
        if market_data is None:
            market_ids = list({b["market_id"] for b in open_bets})
            _slug_map = {
                b["market_id"]: b.get("market_slug", "")
                for b in open_bets
                if b.get("market_slug")
            }
            market_data = self._fetch_market_statuses(market_ids, slug_map=_slug_map)

        exits: list[dict] = []
        now = time.time()
        min_hold_sec = float(pt_cfg.get("min_hold_minutes", 60)) * 60

        for bet in open_bets:
            market_id = bet["market_id"]
            bet_id = bet.get("bet_id", "")

            # ── 最小持仓时间保护：刚买入的仓位跳过止盈评估 ──────────
            ts_entered = float(bet.get("ts_entered") or 0)
            if ts_entered > 0 and (now - ts_entered) < min_hold_sec:
                continue

            mdata = market_data.get(market_id)
            if not mdata:
                continue

            outcome = (bet.get("outcome") or "").strip().upper()
            current_price = self._extract_sell_price(mdata, outcome)
            if current_price is None or current_price <= 0:
                # T101: P1-9 连续价格为 None 时记录警告
                _null_key = bet_id or market_id
                _null_cnt = self._price_null_counts.get(_null_key, 0) + 1
                self._price_null_counts[_null_key] = _null_cnt
                if _null_cnt >= 3 and _null_cnt % 3 == 0:
                    logger.warning(
                        f"[ProfitTake] 价格连续 {_null_cnt} 次为 None/0: "
                        f"{market_id[:16]}... outcome={outcome}"
                    )
                continue

            entry_price = float(bet.get("market_price") or 0)
            if entry_price <= 0:
                continue
            # T101: P1-9 价格恢复后清零连续null计数
            self._price_null_counts.pop(bet_id or market_id, None)

            return_pct = (current_price - entry_price) / entry_price

            # ── 更新 peak_price（追踪历史最高价）──────────────────────
            peak_key = bet_id or market_id
            prev_peak = self._peak_prices.get(peak_key, entry_price)
            if current_price > prev_peak:
                self._peak_prices[peak_key] = current_price
                prev_peak = current_price
                # Phase 57: 持久化新峰值，防止重启后丢失
                try:
                    db.update_trailing_state(peak_key, peak_price=current_price)
                except Exception:
                    pass

            # ── 判断是否体育比赛进行中 ──────────────────────────────
            bet_category = (bet.get("category") or "").lower()
            is_live = False
            # T211: 使用时区感知的 hours_to_expiry 计算
            hours_to_expiry = self._calc_hours_to_expiry(bet, now)

            if (
                bet_category == "sports"
                and hours_to_expiry is not None
                and -live_post_expiry < hours_to_expiry < live_window
            ):
                # 比赛进行中 OR 赛后延迟结算期
                is_live = True

            # ── Crypto 赛道参数覆盖（更激进止盈策略）──────────────
            is_crypto = bet_category == "crypto" and _crypto_override

            # ── Crypto 最大持仓时间限制 ──────────────────────────
            if is_crypto and ts_entered > 0:
                _hours_held = (now - ts_entered) / 3600.0
                if _hours_held >= _crypto_max_hold_hrs and return_pct >= 0:
                    logger.info(
                        f"[CryptoMaxHold] 持仓 {_hours_held:.1f}h ≥ "
                        f"{_crypto_max_hold_hrs}h → 限时止盈 | "
                        f"{bet.get('question', '')[:50]}... | "
                        f"{outcome} {entry_price:.3f}→{current_price:.3f} "
                        f"收益={return_pct:+.1%}"
                    )
                    bet_copy = dict(bet)
                    bet_copy["exit_price"] = current_price
                    bet_copy["return_pct"] = return_pct
                    bet_copy["trigger"] = "crypto_max_hold"
                    exits.append(bet_copy)
                    continue

            # ── -2. CertaintyHold: 高价格放弃 taker 卖出，等待结算 ──────
            # 天气等赛道在价格 ≥ certainty_hold_threshold 时：
            #   等待结算: $1.00/share × 0 fee = $1.00
            #   Taker 卖出: ~threshold × (1-0.02 fee) ≈ $0.97/share
            # 节省约 3%，且彻底消除 TIMEOUT_CANCELLED 问题
            # 不适用于 Crypto（资金占用时间不确定，优先释放资金）
            # T235: 超时机制 — 等待超过 max_hours 后放行卖出，释放资金
            # T235追加: 持久化到 DB，bot 重启后计时器不丢失
            if (
                not is_crypto
                and bet_category in _ch_categories
                and current_price >= _ch_threshold
            ):
                _ch_bet_id = bet.get("bet_id", "")
                _ch_now = time.time()

                # T235追加: 首次记录时间并持久化到 DB
                _ch_max_hours = float(pt_cfg.get("certainty_hold_max_hours", 8))
                if _ch_bet_id not in _certainty_hold_since:
                    _certainty_hold_since[_ch_bet_id] = _ch_now
                    try:
                        from datetime import datetime, timezone

                        _ch_iso = datetime.fromtimestamp(
                            _ch_now, tz=timezone.utc
                        ).isoformat()
                        db.update_certainty_hold_since(_ch_bet_id, _ch_iso)
                    except Exception:
                        pass
                _ch_held_hours = (_ch_now - _certainty_hold_since[_ch_bet_id]) / 3600.0

                if _ch_max_hours > 0 and _ch_held_hours >= _ch_max_hours:
                    # 超时：放行卖出，释放资金
                    logger.info(
                        f"[CertaintyHold] 超时 {_ch_held_hours:.1f}h ≥ {_ch_max_hours}h，"
                        f"放行卖出释放资金 | 价格 {current_price:.4f} | "
                        f"{bet.get('question', '')[:50]}... | "
                        f"{outcome} {entry_price:.3f}→{current_price:.3f} "
                        f"收益={return_pct:+.1%}"
                    )
                    _certainty_hold_since.pop(_ch_bet_id, None)
                    _certainty_hold_last_log.pop(_ch_bet_id, None)
                    try:
                        db.update_certainty_hold_since(_ch_bet_id, None)
                    except Exception:
                        pass
                    bet_copy = dict(bet)
                    bet_copy["exit_price"] = current_price
                    bet_copy["return_pct"] = return_pct
                    bet_copy["trigger"] = "certainty_hold_timeout"
                    exits.append(bet_copy)
                    continue

                # T189: 日志节流 — 每个 bet_id 最多每5分钟打一次 INFO
                _ch_interval = float(pt_cfg.get("certainty_hold_log_interval_sec", 300))
                _ch_last = _certainty_hold_last_log.get(_ch_bet_id, 0)
                if _ch_now - _ch_last >= _ch_interval:
                    _certainty_hold_last_log[_ch_bet_id] = _ch_now
                    logger.info(
                        f"[CertaintyHold] 价格 {current_price:.4f} ≥ {_ch_threshold}，"
                        f"类别={bet_category} 等待结算（省taker费）"
                        f"（已等待 {_ch_held_hours:.1f}h/{_ch_max_hours}h） | "
                        f"{bet.get('question', '')[:50]}..."
                    )
                continue  # 持有到官方结算，绝不提前 taker 卖出

            # ── -1. Certainty Sell: 价格接近 $1.00 → 无条件立即卖出 ────
            # 优先级最高，覆盖 hold-to-settlement：
            # - 0.99→1.00 只有 $0.01/share 的上行空间
            # - 资金占用的机会成本远 > 等结算多赚的 $0.01
            # Crypto 使用更低阈值（0.95），更早释放资金
            certainty_threshold: float = (
                _crypto_certainty
                if is_crypto
                else pt_cfg.get("certainty_sell_threshold", 0.99)
            )
            if current_price >= certainty_threshold:
                logger.info(
                    f"[CertaintySell] 价格 {current_price:.4f} ≥ "
                    f"{certainty_threshold} → 立即卖出释放资金 | "
                    f"{bet.get('question', '')[:50]}... | "
                    f"{outcome} {entry_price:.3f}→{current_price:.3f} "
                    f"收益={return_pct:+.1%}"
                )
                bet_copy = dict(bet)
                bet_copy["exit_price"] = current_price
                bet_copy["return_pct"] = return_pct
                bet_copy["trigger"] = "certainty_sell"
                exits.append(bet_copy)
                continue

            # ── 0. Hold-to-Settlement 检查 ───────────────────────────
            # 高概率 + 临近到期 → 直接持有到结算拿 $1.00，跳过所有止赢
            # Crypto 使用更低阈值 + 更短持有窗口
            _eff_hold_thresh = _crypto_hold_thresh if is_crypto else hold_threshold
            _eff_hold_hrs = _crypto_hold_hrs if is_crypto else hold_hours
            if (
                current_price >= _eff_hold_thresh
                and hours_to_expiry is not None
                and 0 < hours_to_expiry < _eff_hold_hrs
            ):
                logger.debug(
                    f"[HoldToSettle] {bet.get('question', '')[:40]}... "
                    f"价格={current_price:.3f} ≥ {_eff_hold_thresh}，"
                    f"距到期 {hours_to_expiry:.1f}h → 持有到结算"
                )
                continue  # 跳过所有止盈，等待自然结算

            # ── 0.1 ExitPolicy 策略链（替代内联 weather_confidence_hold）──
            # 使用 PolicyChain 统一评估是否应该持有
            # 包含 WeatherConfidenceHoldPolicy + CityNoScoreHoldPolicy
            # 注意：此处拦截 "所有后续止盈"，certainty_sell 和 hold_to_settle 已在上方处理
            _policy_chain = getattr(self, "_exit_policy_chain", None)
            if _policy_chain is not None:
                from src.exit_policy.base import BetContext

                _pc_ctx = BetContext.from_bet(
                    bet,
                    current_price,
                    hours_to_expiry,
                    trigger_type="profit_take",
                    trigger_name="pre_scan",
                )
                _pc_advice = _policy_chain.should_hold(_pc_ctx, config_fn=get)
                if _pc_advice is not None:
                    continue  # PolicyChain 建议持有，跳过所有后续止盈

            # ── 选择参数（比赛中 vs Crypto vs 常规）──────────────────
            if is_live:
                eff_ceiling = live_ceiling
            elif is_crypto:
                eff_ceiling = _crypto_ceiling
            else:
                eff_ceiling = near_ceiling

            # ── 检查临近到期条件 ─────────────────────────────────────
            expiry_trigger = False
            if hours_to_expiry is not None:
                if (
                    0 < hours_to_expiry < expiry_hours
                    and return_pct >= expiry_profit_pct
                ):
                    expiry_trigger = True

            # ── 0.5 方向错误遗留仓位清理 ──────────────────────────
            # 历史 _ensemble_vote 方向 bug 造成的仓位：ai_prob <0.5 却买 YES
            # 任何正收益即刻止盈离场，避免继续暴露风险
            # 注意：排除 chain_sync 仓位（ai_probability 是虚拟值，不可用于方向判断）
            _ai_prob = float(bet.get("ai_probability") or 0)
            _bet_source = (bet.get("source") or "").lower()
            _is_chain_sync = _bet_source == "manual" or "链上持仓" in (
                bet.get("ai_reasoning") or ""
            )
            # Phase29修复：提高方向不一致检测阈值至0.60
            # 76ers NO bet ai_prob=0.57被误杀(bordeline)——ensemble决策可合法偏移
            # 新增 legacy_cleanup_enabled 配置开关
            # T188修复：排除天气赛道——天气pipeline的 ai_probability
            # 表示 P(NO-side wins)，不是 P(YES)。天气NO仓位的 ai_prob=0.99
            # 意味着"99%确信NO会赢"，LegacyCleanup误读为"AI说YES=0.99却买了NO"
            _legacy_enabled = pt_cfg.get("legacy_cleanup_enabled", True)
            _legacy_threshold = float(pt_cfg.get("legacy_mismatch_threshold", 0.60))
            _is_legacy_wrong = (
                _legacy_enabled
                and not _is_chain_sync
                and bet_category != "weather"
                and (
                    (
                        _ai_prob > 0
                        and _ai_prob < (1.0 - _legacy_threshold)
                        and outcome == "YES"
                    )
                    or (_ai_prob > _legacy_threshold and outcome == "NO")
                )
            )
            if _is_legacy_wrong and return_pct > 0:
                trigger = "legacy_cleanup"
                logger.info(
                    f"[LegacyCleanup] 方向错误遗留仓位止盈 "
                    f"{market_id[:20]}: {outcome} ai={_ai_prob:.2f} "
                    f"entry={entry_price:.3f}→{current_price:.3f} +{return_pct:.1%}"
                )
                bet_copy = dict(bet)
                bet_copy["exit_price"] = current_price
                bet_copy["return_pct"] = return_pct
                bet_copy["trigger"] = trigger
                exits.append(bet_copy)
                continue

            # ── 止盈决策（优先级从高到低）────────────────────────────
            trigger = None

            if current_price >= eff_ceiling:
                # 1. 天花板止盈：价格已接近最大值
                trigger = "ceiling" if not is_live else "live_ceiling"
            elif (
                weather_quick_exit > 0
                and entry_price <= weather_quick_entry_max
                and current_price >= weather_quick_exit
            ):
                # 2. 天气快速止盈
                trigger = "weather_quick"
            elif entry_price >= quick_exit_entry_max and return_pct >= (
                _crypto_profit_pct if is_crypto else profit_take_pct
            ):
                # 3. 标准止盈: 中高价位入场(>0.25)且收益达标
                # Crypto 使用 30% 更低阈值，常规赛道 50%
                trigger = "standard_profit_take"
            elif trailing_enabled and prev_peak > entry_price:
                # 4. 阶梯 Trailing take-profit
                # 收益越高,允许越多回调空间,避免在大赢面前被小波动震出
                drop_from_peak = (prev_peak - current_price) / prev_peak

                # 计算有效trailing回调阈值(阶梯式)
                # Crypto 使用更紧的回调阈值 (15% vs 8%)
                _base_trail_drop = (
                    _crypto_trail_drop if is_crypto else trailing_drop_pct
                )
                eff_trailing_drop = _base_trail_drop
                if trailing_tiers:
                    for tier in trailing_tiers:
                        if return_pct < tier.get("max_return", 999):
                            eff_trailing_drop = tier.get("drop_pct", _base_trail_drop)
                            break
                # 天气市场最低回调保护
                if bet_category == "weather":
                    eff_trailing_drop = max(
                        eff_trailing_drop, weather_trailing_min_drop
                    )

                # ── T65: Dynamic Trailing Floor 动态保底 ─────────────────────
                # 一旦峰值确认超过 trailing_min_profit_pct 阈值（赢家已确立），
                # 动态计算 floor = peak × (1 - retreat_pct)，且不低于 entry × (1 + floor_min)。
                # 棘轮机制：floor 只升不降。每次 peak 创新高时自动上调 floor。
                # Crypto 使用更低的最小利润阈值 (10% vs 5%)
                _eff_trail_min = (
                    _crypto_trail_min if is_crypto else trailing_min_profit_pct
                )
                if prev_peak >= entry_price * (1.0 + _eff_trail_min):
                    # 最低保底 = entry × (1 + floor_min_profit_pct)
                    min_floor = entry_price * (1.0 + trailing_floor_min_profit_pct)
                    old_floor = self._floor_prices.get(peak_key, 0.0)

                    if trailing_floor_dynamic:
                        # T65 动态模式：floor = peak × (1 - retreat_pct)
                        if is_crypto:
                            _retreat = crypto_floor_retreat_pct
                        elif bet_category == "sports":
                            _retreat = sports_floor_retreat_pct
                        else:
                            _retreat = trailing_floor_retreat_pct
                        dynamic_floor = prev_peak * (1.0 - _retreat)
                        new_floor = max(dynamic_floor, min_floor)
                    else:
                        # 旧逻辑：静态一次性 floor = entry × (1 + floor_min)
                        new_floor = min_floor

                    # 棘轮：只升不降
                    if new_floor > old_floor:
                        self._floor_prices[peak_key] = new_floor
                        try:
                            db.update_trailing_state(peak_key, floor_price=new_floor)
                        except Exception:
                            pass
                        if old_floor <= 0:
                            logger.info(
                                f"[TrailingFloor] {peak_key[:8]} 赢家保底锁定: "
                                f"peak={prev_peak:.3f} ≥ entry×{1+_eff_trail_min:.2f} "
                                f"→ floor={new_floor:.3f} "
                                f"(entry={entry_price:.3f}"
                                + (
                                    f", dynamic=peak×{1-_retreat:.0%}"
                                    if trailing_floor_dynamic
                                    else f"+{trailing_floor_min_profit_pct:.0%}"
                                )
                                + ")"
                            )
                        else:
                            logger.info(
                                f"[TrailingFloor] {peak_key[:8]} 动态上调: "
                                f"floor {old_floor:.3f} → {new_floor:.3f} "
                                f"(peak={prev_peak:.3f}, entry={entry_price:.3f})"
                            )

                # 检查 floor 保护（优先于 trailing 常规逻辑）
                # Task227修复: 增加 return_pct >= 0 检查，确保止盈不会导致亏损
                # 场景: peak曾到高点触发floor锁定，但价格随后暴跌到entry以下
                # 此时floor_protection不应触发（让止损逻辑处理），避免"止盈亏损"
                if (
                    peak_key in self._floor_prices
                    and current_price < self._floor_prices[peak_key]
                    and return_pct >= 0
                ):
                    trigger = "floor_protection"
                    logger.info(
                        f"[TrailingFloor] ⚡ 赢家变输家保护触发! "
                        f"{peak_key[:8]} floor={self._floor_prices[peak_key]:.3f} "
                        f"> now={current_price:.3f} "
                        f"(entry={entry_price:.3f}, peak={prev_peak:.3f}, return={return_pct:+.1%})"
                    )
                elif (
                    peak_key in self._floor_prices
                    and current_price < self._floor_prices[peak_key]
                    and return_pct < 0
                ):
                    # 价格已跌破entry — floor_protection不触发，让止损通道处理
                    logger.warning(
                        f"[TrailingFloor] ⚠️ 跌破floor且亏损，跳过止盈 "
                        f"{peak_key[:8]} floor={self._floor_prices[peak_key]:.3f} "
                        f"now={current_price:.3f} entry={entry_price:.3f} "
                        f"return={return_pct:+.1%} → 交给止损处理"
                    )
                elif current_price >= prev_peak:
                    # 正在创新高 → 不卖，让利润继续奔跑
                    pass
                elif (
                    drop_from_peak >= eff_trailing_drop and return_pct >= _eff_trail_min
                ):
                    # 从高点回撤超过阈值 且 仍在盈利 → 锁定利润
                    trigger = "trailing"
                    logger.info(
                        f"[Trailing] peak={prev_peak:.3f} → now={current_price:.3f} "
                        f"回撤={drop_from_peak:.1%} ≥ {eff_trailing_drop:.1%} "
                        f"收益={return_pct:+.1%} ≥ {_eff_trail_min:.1%}"
                    )
            # 注意：体育比赛进行中，如果有更宽松的止赢百分比可以作为补充
            if trigger is None and is_live and return_pct >= live_profit_pct:
                trigger = "live_standard"

            if trigger is None and expiry_trigger:
                # 4. 临近到期止盈
                trigger = "expiry"

            # T185: 天气赛道事件日轮动退出
            # 事件日当天（距结算≤trigger_hours）且浮亏不超min_return_pct → 主动平仓释放资金
            # 区别于expiry_trigger（需≥50%利润），本策略允许微亏(-5%)也退出，换取资金周转率
            # T183: 增加接近确定性保护 — 价格接近 CertaintyHold 阈值时跳过轮动退出
            if trigger is None and _rotation_enabled and bet_category == "weather":
                _near_certainty = (
                    bet_category in _ch_categories
                    and _rotation_cert_buffer > 0
                    and current_price >= _ch_threshold - _rotation_cert_buffer
                )
                if _near_certainty:
                    logger.info(
                        f"[RotationExit] 跳过 — 价格 {current_price:.3f} 接近确定性阈值 "
                        f"{_ch_threshold}(buffer={_rotation_cert_buffer}) "
                        f"| 收益={return_pct:+.1%} | {bet.get('question', '')[:40]}..."
                    )
                elif (
                    hours_to_expiry is not None
                    and 0 < hours_to_expiry <= _rotation_trigger_hours
                    and return_pct >= _rotation_min_return
                ):
                    trigger = "weather_rotation_exit"
                    logger.info(
                        f"[RotationExit] T185 天气事件日轮动退出 "
                        f"| 距到期 {hours_to_expiry:.1f}h ≤ {_rotation_trigger_hours}h "
                        f"| 收益={return_pct:+.1%} ≥ {_rotation_min_return:.1%} "
                        f"| {bet.get('question', '')[:40]}..."
                    )

            if trigger is None:
                continue  # 不满足任何止盈条件，继续持有

            # ── T231: Weather Exit Gate — 天气赛道智能退出门控 ──────
            # 天气市场的结果由物理温度决定，非市场情绪。
            # 当基本面明确支持持仓方向时（实时温度距阈值有安全间距），
            # 阻止因市场操纵/流动性异常导致的误卖出，保留确定性利润。
            # 安全默认：任何异常均不拦截（allow sell），避免与 SUPREME 1 冲突。
            if (
                bet_category == "weather"
                and trigger in ("floor_protection", "trailing")
                and get("profit_taking.weather_exit_gate_enabled", True)
            ):
                _gate_result = self._weather_exit_gate(bet, current_price, trigger)
                if _gate_result == "hold":
                    continue  # 基本面支持持仓，跳过卖出

            logger.info(
                f"[ProfitTake] [{trigger}] | {bet['question'][:50]}... "
                f"| {outcome} {entry_price:.3f}→{current_price:.3f} "
                f"| 收益={return_pct:+.1%}"
                + (f" | peak={prev_peak:.3f}" if trailing_enabled else "")
            )
            bet_copy = dict(bet)
            bet_copy["exit_price"] = current_price
            bet_copy["return_pct"] = return_pct
            bet_copy["trigger"] = trigger

            exits.append(bet_copy)

        # 清理已平仓的 peak / floor 记录
        open_ids = {b.get("bet_id", b["market_id"]) for b in open_bets}
        stale = [k for k in self._peak_prices if k not in open_ids]
        for k in stale:
            del self._peak_prices[k]
        stale_floor = [k for k in self._floor_prices if k not in open_ids]
        for k in stale_floor:
            del self._floor_prices[k]

        return exits

    # ------------------------------------------------------------------
    # T231: Weather Exit Gate — 天气赛道智能退出门控
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_weather_question(question: str) -> Optional[dict]:
        """从 Polymarket 天气事件标题解析城市名和温度阈值。

        支持格式 (°F 和 °C):
        - "Will the highest temperature in Los Angeles be between 70-71°F ..."
        - "Will the highest temperature in Munich be 16°C on ..."
        - "Will the lowest temperature in Chicago be below 30°F ..."

        Returns: {"city": str, "temp_low": float, "temp_high": float,
                  "metric": str, "unit": "F"|"C"}
                 or None if parse fails
        """
        if not question:
            return None
        # 匹配 "highest/lowest temperature in {City} be between X-Y°F/°C"
        m = re.search(
            r"(highest|lowest)\s+temperature\s+in\s+(.+?)\s+be\s+"
            r"(?:between\s+(\d+)[–\-](\d+)|(?:above|below)\s+(\d+)|\s*(\d+))\s*°?\s*([FC])",
            question,
            re.IGNORECASE,
        )
        if not m:
            return None
        metric = m.group(1).lower()  # "highest" or "lowest"
        city = m.group(2).strip()
        unit = m.group(7).upper()  # "F" or "C"
        if m.group(3) and m.group(4):
            low = float(m.group(3))
            high = float(m.group(4))
        elif m.group(5):
            low = high = float(m.group(5))
        elif m.group(6):
            low = high = float(m.group(6))
        else:
            return None
        return {
            "city": city,
            "temp_low": low,
            "temp_high": high,
            "metric": metric,
            "unit": unit,
            # 向后兼容
            "temp_low_f": low if unit == "F" else low * 9 / 5 + 32,
            "temp_high_f": high if unit == "F" else high * 9 / 5 + 32,
        }

    def _weather_exit_gate(self, bet: dict, current_price: float, trigger: str) -> str:
        """天气退出门控：基于实时温度判断基本面是否支持持仓方向。

        只使用当前实时温度（非历史最高温/预报值），因为门控目的是
        判断"此刻卖出是否合理"，过去的温度峰值与当前决策无关。

        优化：当仓位仍处于显著盈利状态时，直接 HOLD 而不调用温度
        API，避免浪费资源（价格恢复 = 市场已自我纠正 = 基本面健康）。

        Returns:
            "hold"  — 基本面支持持仓，阻止卖出
            "sell"  — 基本面不支持或无法判断，允许正常卖出
        """
        bet_id = bet.get("bet_id", "")[:8]
        question = bet.get("question", "")
        outcome = bet.get("outcome", "YES").upper()
        entry_price = float(bet.get("market_price") or 0)

        try:
            # ── 第1层：价格恢复短路 ──────────────────────────
            # 如果仓位仍在显著盈利区（如 ≥15%），说明价格已恢复/基本面健康。
            # 直接 HOLD，跳过温度 API 调用，节省资源。
            profit_hold_pct = float(
                get("profit_taking.weather_exit_gate_profit_hold_pct") or 0.15
            )
            if entry_price > 0 and current_price > 0:
                gain_pct = (current_price - entry_price) / entry_price
                if gain_pct >= profit_hold_pct:
                    logger.info(
                        f"[WeatherExitGate] 🛡️ 价格恢复短路 {bet_id} | "
                        f"price={current_price:.3f} entry={entry_price:.3f} "
                        f"gain=+{gain_pct:.1%} ≥ {profit_hold_pct:.0%} | "
                        f"基本面健康，跳过温度检查"
                    )
                    return "hold"

            # ── 第2层：实时温度门控 ──────────────────────────
            parsed = self._parse_weather_question(question)
            if not parsed:
                logger.debug(f"[WeatherExitGate] {bet_id} 无法解析标题，放行卖出")
                return "sell"

            city = parsed["city"]
            metric = parsed["metric"]  # "highest" or "lowest"
            unit = parsed.get("unit", "F")  # "F" or "C"
            threshold_low = parsed["temp_low"]  # 市场原始单位
            threshold_high = parsed["temp_high"]

            # 获取实时温度（METAR优先 → NWS → Open-Meteo fallback，返回摄氏度）
            from src.utils.weather_fetcher import fetch_realtime_temperature

            current_temp_c = fetch_realtime_temperature(city)
            if current_temp_c is None:
                logger.info(
                    f"[WeatherExitGate] {bet_id} 无法获取{city}实时温度，放行卖出"
                )
                return "sell"

            # 将实时温度转换为市场单位
            if unit == "F":
                current_temp = current_temp_c * 9 / 5 + 32  # °C → °F
            else:
                current_temp = current_temp_c  # 保持 °C

            # 安全间距：华氏度和摄氏度分别使用独立参数
            if unit == "C":
                safe_margin = float(
                    get("profit_taking.weather_exit_gate_safe_margin_c") or 1
                )
            else:
                safe_margin = float(
                    get("profit_taking.weather_exit_gate_safe_margin_f") or 2
                )

            should_hold = False
            reason = ""

            if outcome == "NO" and metric == "highest":
                # NO on "highest temp between X-Y" — 实时温度远低于阈值 → NO安全
                margin = threshold_low - current_temp
                if margin >= safe_margin:
                    should_hold = True
                    reason = (
                        f"实时{current_temp:.1f}°{unit} "
                        f"距阈值{threshold_low:.0f}°{unit} "
                        f"尚有{margin:.1f}°{unit}间距 ≥ "
                        f"{safe_margin:.1f}°{unit}安全线"
                    )
            elif outcome == "YES" and metric == "highest":
                # YES on "highest temp between X-Y" — 实时温度已接近区间 → YES安全
                if current_temp >= threshold_low - safe_margin:
                    should_hold = True
                    reason = (
                        f"实时{current_temp:.1f}°{unit} "
                        f"已接近/进入阈值区间"
                        f"[{threshold_low:.0f}-{threshold_high:.0f}°{unit}]"
                    )
            elif outcome == "NO" and metric == "lowest":
                # NO on "lowest temp between X-Y" — 实时温度远高于阈值上限 → NO安全
                margin = current_temp - threshold_high
                if margin >= safe_margin:
                    should_hold = True
                    reason = (
                        f"实时{current_temp:.1f}°{unit} "
                        f"距阈值上限{threshold_high:.0f}°{unit} "
                        f"尚有{margin:.1f}°{unit}间距 ≥ "
                        f"{safe_margin:.1f}°{unit}安全线"
                    )
            elif outcome == "YES" and metric == "lowest":
                # YES on "lowest temp between X-Y" — 实时温度已接近区间 → YES安全
                if current_temp <= threshold_high + safe_margin:
                    should_hold = True
                    reason = (
                        f"实时{current_temp:.1f}°{unit} "
                        f"已接近/进入阈值区间"
                        f"[{threshold_low:.0f}-{threshold_high:.0f}°{unit}]"
                    )

            if should_hold:
                logger.info(
                    f"[WeatherExitGate] 🛡️ 阻止卖出 {bet_id} | trigger={trigger} | "
                    f"{outcome} | {reason} | "
                    f"price {current_price:.3f} 可能是市场操纵 | "
                    f"{question[:60]}..."
                )
                return "hold"
            else:
                logger.info(
                    f"[WeatherExitGate] ✅ 放行卖出 {bet_id} | trigger={trigger} | "
                    f"{outcome} | 实时{current_temp:.1f}°{unit} vs "
                    f"阈值[{threshold_low:.0f}-{threshold_high:.0f}°{unit}] | "
                    f"间距不足，基本面不支持持有"
                )
                return "sell"

        except Exception as e:
            # 安全默认：异常时不拦截，允许正常卖出
            logger.warning(f"[WeatherExitGate] {bet_id} 门控异常，放行卖出: {e}")
            return "sell"

    # ------------------------------------------------------------------
    # DRY RUN 手续费估算（Polymarket 官方公式: fee = shares × feeRate × p × (1-p))
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_dry_run_fees(
        shares: float, entry_price: float, exit_price: float, category: str
    ) -> float:
        """估算 DRY RUN 模式下双边手续费（买入+卖出）。"""
        _taker_rates = get("paper_trading.taker_fee_rates") or {}
        _fee_rate = float(
            _taker_rates.get(category, 0)
            or get("paper_trading.taker_fee_rate_default")
            or 0.05
        )
        buy_fee = (
            _fee_rate * entry_price * (1 - entry_price) if 0 < entry_price < 1 else 0
        )
        sell_fee = (
            _fee_rate * exit_price * (1 - exit_price) if 0 < exit_price < 1 else 0
        )
        return round(shares * (buy_fee + sell_fee), 4)

    def close_position_profit_take(self, bet: dict, db_path=None) -> None:
        """
        以当前市场价模拟卖出，记录主动止盈结果。
        P&L 计算：shares = size/entry_price；proceeds = shares × exit_price
        """
        from src.utils import db as _db

        _db_path = db_path if db_path is not None else _db._DB_PATH

        size = float(bet.get("size_usdc", 0.0))
        entry_price = float(bet.get("market_price") or 0.5)
        exit_price = float(bet.get("exit_price", 0.0))

        if entry_price <= 0 or exit_price <= 0:
            logger.warning(f"[ProfitTake] 无效价格, 跳过: {bet.get('bet_id')}")
            return

        # Phase40: 优先使用实际到账金额（余额差底层数据）作为权威收益
        actual_proceeds = float(bet.get("actual_proceeds") or 0)
        if actual_proceeds > 0:
            proceeds = round(actual_proceeds, 4)
        else:
            shares = size / entry_price
            gross = round(shares * exit_price, 4)
            category = bet.get("category", "weather")
            estimated_fees = self._estimate_dry_run_fees(
                shares, entry_price, exit_price, category
            )
            proceeds = round(gross - estimated_fees, 4)
        pnl = round(proceeds - size, 4)
        # T189-G: 扣除买入+卖出手续费得到真实PnL
        _tp_entry_fee = float(bet.get("entry_fee_usdc") or 0)
        pnl = round(proceeds - size - _tp_entry_fee, 4)
        actual_return_pct = pnl / size if size > 0 else 0.0

        _db.close_paper_bet_profit_take(
            bet_id=bet["bet_id"],
            exit_price=exit_price,
            pnl=pnl,
            payout=proceeds,
            db_path=_db_path,
        )
        # T189-F: 记录卖出手续费
        # T228追加: 退出费率使用 sell_mode 配置（而非入场 order_type）
        _tp_shares = size / entry_price if entry_price > 0 else 0
        _tp_cat = bet.get("category", "weather")
        _tp_ot = _resolve_exit_order_type(_tp_cat, bet.get("order_type", "taker"))
        _tp_exit_fee, _ = _db.calc_pm_fee(_tp_shares, exit_price, _tp_cat, _tp_ot)
        if _tp_exit_fee > 0:
            _db.update_bet_fees(
                bet["bet_id"], exit_fee_usdc=_tp_exit_fee, db_path=_db_path
            )
            # T189-G: 卖出手续费也要反映到pnl中
            pnl = round(pnl - _tp_exit_fee, 4)
            with _db.connect(_db_path) as _conn:
                _conn.execute(
                    "UPDATE paper_bets SET pnl=? WHERE bet_id=?", (pnl, bet["bet_id"])
                )
        logger.info(
            f"[ProfitTake] ✅ 止盈完成 | bet={bet['bet_id'][:8]}... "
            f"| 入场={entry_price:.3f} 离场={exit_price:.3f} "
            f"| P&L={pnl:+.4f} USDC ({actual_return_pct:+.1%})"
        )
        from src.utils.telegram_notifier import get_notifier

        get_notifier().notify_position_closed(
            question=bet.get("question", ""),
            close_type="profit_take",
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            size_usdc=size,
            category=bet.get("category", ""),
        )

    # ------------------------------------------------------------------
    # 止损（Stop Loss）接口
    # ------------------------------------------------------------------

    def check_stop_loss_exits(
        self, open_bets: list[dict], market_data: dict[str, dict] | None = None
    ) -> list[dict]:
        """
        检查开放仓位是否触发止损条件。

        ── 设计原则（二元市场专用）─────────────────────────────────────
        二元市场止损 ≠ 传统资产追踪止损。
        但不同赛道策略不同：
        - 天气/crypto：禁用百分比止损（价格波动是噪音，结果非0即1）
        - 体育：启用百分比止损（价格变化反映真实比分，是**信号**不是噪音！）

        ── 止损层级（优先级从高到低）──────────────────────────────
        1. 地板止损（floor_price）：市场已判死刑
        2. 标准止损（standard_stop_pct，赛道区分）：深度亏损
        3. 方向翻转止损：入场≥40¢现跌到<reversal
        4. 临近到期止损（expiry_stop）
        5. 体育赛后强制清仓（sports_expired）

        ── 体育比赛进行中（sports_live）──────────────────────────────
        距到期 < game_window_hours(4h) 且 category=sports：
        使用 sports_live 配置的更敏感参数（floor=0.05, stop=0.45, reversal=0.15）
        比赛中价格变化快且方向性强，需更早止损保护本金。
        赛后延长保护：post_expiry_window_hours(2h) 内仍用 live 参数。

        返回：应该止损平仓的投注列表（含 exit_price 字段）。
        """
        sl_cfg = get("stop_loss") or {}
        if not sl_cfg.get("enabled", True):
            return []
        if not open_bets:
            return []

        floor_price: float = sl_cfg.get("floor_price", 0.05)
        weather_edge_floor: float = sl_cfg.get("weather_edge_floor_price", 0.03)
        standard_stop_pct: float = sl_cfg.get("standard_stop_pct", 0.75)
        weather_stop_pct: float = sl_cfg.get(
            "weather_standard_stop_pct", standard_stop_pct
        )
        expiry_stop_hours: float = sl_cfg.get("expiry_stop_hours", 4.0)
        expiry_stop_loss_pct: float = sl_cfg.get("expiry_stop_loss_pct", 0.60)
        reversal_threshold: float = sl_cfg.get("reversal_exit_price", 0.08)

        # 体育赛道专用止损（非比赛中也生效）
        sports_stop_pct: float = sl_cfg.get("sports_standard_stop_pct", 0.70)

        # 体育比赛进行中专用参数（更敏感）
        sl_cfg_live = get("sports_live") or {}
        live_window: float = sl_cfg_live.get("game_window_hours", 4.0)
        live_post_expiry: float = sl_cfg_live.get("post_expiry_window_hours", 2.0)
        live_floor: float = sl_cfg_live.get("floor_price", 0.05)
        live_stop_pct: float = sl_cfg_live.get("standard_stop_pct", 0.80)
        live_reversal: float = sl_cfg_live.get("reversal_exit_price", 0.08)

        # 批量获取当前市场价格（支持外部传入缓存，避免同一周期重复请求）
        if market_data is None:
            market_ids = list({b["market_id"] for b in open_bets})
            _slug_map = {
                b["market_id"]: b.get("market_slug", "")
                for b in open_bets
                if b.get("market_slug")
            }
            market_data = self._fetch_market_statuses(market_ids, slug_map=_slug_map)

        stops: list[dict] = []
        now = time.time()
        # 最小持仓时间保护（与止盈共享配置，防止刚买入即被止损）
        pt_cfg_ref = get("profit_taking") or {}
        sl_min_hold_sec = float(pt_cfg_ref.get("min_hold_minutes", 60)) * 60

        for bet in open_bets:
            market_id = bet["market_id"]

            # ── 最小持仓时间保护：刚买入的仓位跳过止损评估 ──────────
            ts_entered = float(bet.get("ts_entered") or 0)
            if ts_entered > 0 and (now - ts_entered) < sl_min_hold_sec:
                continue

            mdata = market_data.get(market_id)
            if not mdata:
                continue

            outcome = (bet.get("outcome") or "").strip().upper()

            # ── 止损价格模式：始终使用 market 模式（纯盘口价）──
            # market 模式跳过 PriceDeviation 校验，防止参考价替代真实暴跌的盘口价
            # T60 Fix: Crypto 止损也曾出现 anchor 模式滞后（亏损86%才触发），统一为 market
            # stoploss_mode 配置已废弃删除（原来 all="market" 与默认值完全相同）
            bet_category = (bet.get("category") or "").lower()
            stoploss_mode = "market"
            use_raw_price = True

            # 使用 best_bid 价格：主动卖出的真实成交价（Polymarket 手续费 0%，卖出 = best_bid）
            current_price = self._extract_sell_price(
                mdata, outcome, validate=not use_raw_price
            )
            if current_price is None or current_price <= 0:
                # ── 有效结算检测：卖价=0时检查 outcomePrices 是否表明仓位已死 ──
                # 场景：市场未官方结算但 bestAsk=1.0（对方概率99%+），NO卖价=1-1=0
                _prices_raw = mdata.get("outcomePrices", "") or ""
                _prices: list = []
                if isinstance(_prices_raw, str):
                    try:
                        _prices = json.loads(_prices_raw)
                    except Exception:
                        pass
                elif isinstance(_prices_raw, list):
                    _prices = _prices_raw
                if len(_prices) >= 2:
                    try:
                        _yes_p = float(_prices[0])
                        _no_p = float(_prices[1])
                        _is_dead = (outcome == "NO" and _yes_p >= 0.99) or (
                            outcome == "YES" and _no_p >= 0.99
                        )
                        if _is_dead:
                            _entry_p = float(bet.get("market_price") or 0)
                            _dead_exit = float(_no_p if outcome == "NO" else _yes_p)
                            _dead_exit = max(_dead_exit, 0.0001)
                            # T70: 低价卖出暂停 — 价格极低且多次卖出失败时停止尝试
                            _dead_max_retries = int(
                                (get("position_management") or {}).get(
                                    "dead_position_max_sell_retries", 3
                                )
                            )
                            _dead_price_thresh = float(
                                (get("position_management") or {}).get(
                                    "dead_position_price_threshold", 0.05
                                )
                            )
                            _sell_fails = int(bet.get("sell_fail_count") or 0)
                            _bet_id_guard = bet.get("bet_id", "")

                            if (
                                _dead_exit < _dead_price_thresh
                                and _sell_fails >= _dead_max_retries
                            ):
                                _paused_set = getattr(self, "_dead_paused_bets", None)
                                if _paused_set is None:
                                    self._dead_paused_bets = set()
                                    _paused_set = self._dead_paused_bets
                                if _bet_id_guard not in _paused_set:
                                    _paused_set.add(_bet_id_guard)
                                    logger.info(
                                        f"[StopLoss] 💤 低价卖出暂停 bet={bet.get('bet_id', '')[:8]}... "
                                        f"| {bet.get('question', '')[:50]} "
                                        f"| price={_dead_exit:.4f} "
                                        f"sell_fails={_sell_fails}/{_dead_max_retries} "
                                        f"→ 停止卖出尝试，等待平台正式结算"
                                    )
                            else:
                                _dead_return = (
                                    (_dead_exit - _entry_p) / _entry_p
                                    if _entry_p > 0
                                    else -1.0
                                )
                                bet_copy = dict(bet)
                                bet_copy["exit_price"] = _dead_exit
                                bet_copy["return_pct"] = _dead_return
                                bet_copy["trigger"] = "effective_resolution"
                                stops.append(bet_copy)
                                logger.warning(
                                    f"[StopLoss] ⚡ 有效结算止损 bet={bet.get('bet_id', '')[:8]}... "
                                    f"| {bet.get('question', '')[:50]} "
                                    f"| {outcome} YES={_yes_p:.4f}/NO={_no_p:.4f} → 强制止损"
                                )
                    except (ValueError, TypeError):
                        pass
                continue

            entry_price = float(bet.get("market_price") or 0)
            if entry_price <= 0:
                continue

            return_pct = (current_price - entry_price) / entry_price

            # ── 判断是否体育比赛进行中 ──────────────────────────────
            is_live = False
            # T211: 使用时区感知的 hours_to_expiry 计算
            hours_to_expiry = self._calc_hours_to_expiry(bet, now)

            if (
                bet_category == "sports"
                and hours_to_expiry is not None
                and -live_post_expiry < hours_to_expiry < live_window
            ):
                # 比赛进行中 OR 赛后延迟结算期（time-based 推算）
                is_live = True

            # [PRO] OddsPapi /fixtures/live 精准验证（增强 time-based 判断）
            if bet_category == "sports" and not is_live:
                try:
                    from src.utils.odds_fetcher import check_fixture_live

                    _q = bet.get("question", "")
                    if _q and check_fixture_live(_q):
                        is_live = True
                        logger.debug(
                            f"[StopLoss] OddsPapi live 确认赛中: bet={bet.get('bet_id', '')[:8]}... | {bet.get('question', '')[:50]}"
                        )
                except Exception:
                    pass  # API 失败时静默降级到 time-based

            # ── 检查临近到期条件 ────────────────────────────────────
            expiry_trigger = False
            if hours_to_expiry is not None:
                if (
                    0 < hours_to_expiry < expiry_stop_hours
                    and return_pct <= -expiry_stop_loss_pct
                ):
                    expiry_trigger = True

            # ── 选择参数（比赛中 vs 天气 vs 常规）──────────────────
            eff_floor = live_floor if is_live else floor_price
            if is_live:
                eff_stop_pct = live_stop_pct
            elif bet_category == "sports":
                # 体育赛道始终用独立止损（不同于天气/crypto的禁用策略）
                eff_stop_pct = sports_stop_pct
            elif bet_category == "weather":
                # ── Smart 时间加权天气止损 ──────────────────────
                # 越远离结算时间越宽松（给预测回归时间），
                # 越接近结算时间越严格（信息确定性高）
                tw_cfg = sl_cfg.get("weather_time_weighted_stop") or {}

                # T185: 强制止损（任何时间点浮亏超阈值立即止损）
                _force_stop = float(tw_cfg.get("force_stop_pct", 0.80))
                if return_pct <= -_force_stop:
                    eff_stop_pct = _force_stop
                    # T195: 限制重复日志 — 每bet仅记录一次 WeatherForce INFO
                    _wf_key = ("weatherforce", market_id)
                    _wf_fn = (
                        logger.info
                        if _wf_key not in self._sl_logged_events
                        else logger.debug
                    )
                    _wf_fn(
                        f"[StopLoss-WeatherForce] {market_id[:12]} "
                        f"浮亏{return_pct:.0%} >= 强制止损{_force_stop:.0%}"
                    )
                    if _wf_key not in self._sl_logged_events:
                        self._sl_logged_events.add(_wf_key)
                elif tw_cfg.get("enabled", False) and hours_to_expiry is not None:
                    _tw_far_h = float(tw_cfg.get("far_hours", 12.0))
                    _tw_mid_h = float(tw_cfg.get("mid_hours", 4.0))
                    if hours_to_expiry > _tw_far_h:
                        eff_stop_pct = float(tw_cfg.get("far_stop_pct", 0.75))
                    elif hours_to_expiry > _tw_mid_h:
                        eff_stop_pct = float(tw_cfg.get("mid_stop_pct", 0.70))
                    else:
                        eff_stop_pct = float(tw_cfg.get("near_stop_pct", 0.75))
                    logger.debug(
                        f"[StopLoss-WeatherTW] {market_id[:12]} "
                        f"h2exp={hours_to_expiry:.1f} → stop={eff_stop_pct:.0%}"
                    )
                else:
                    eff_stop_pct = weather_stop_pct
            elif bet_category == "crypto":
                # T60: Crypto 赛道专用止损 — 加密市场波动剧烈，需更紧止损
                # 默认 35%（远低于通用 75%），防止深度亏损
                eff_stop_pct = float(sl_cfg.get("crypto_standard_stop_pct", 0.35))
            else:
                eff_stop_pct = standard_stop_pct
            eff_reversal = live_reversal if is_live else reversal_threshold

            # ── 早期止损加速（前N分钟更紧的止损保护）─────────────────
            # 新建仓位刚入场不久，若已出现亏损说明入场判断可能有误
            # 天气赛道使用更紧阈值(15%)因有气象数据支撑，其他赛道(20%)适度宽松
            early_cfg = sl_cfg.get("early_stop") or {}
            if early_cfg.get("enabled", True) and not is_live:
                _early_window = float(early_cfg.get("window_minutes", 10))
                _weather_pct = float(early_cfg.get("weather_pct", 0.15))
                _default_pct = float(early_cfg.get("default_pct", 0.20))
                # 兼容旧配置：若还有 stop_pct 字段则作为 fallback
                _fallback_pct = float(early_cfg.get("stop_pct", _default_pct))
                _early_pct = (
                    _weather_pct
                    if bet_category == "weather"
                    else early_cfg.get("default_pct", _fallback_pct)
                )
                _ts_entered = float(bet.get("ts_entered") or 0)
                if _ts_entered > 0:
                    _hold_min = (now - _ts_entered) / 60.0
                    if _hold_min < _early_window:
                        eff_stop_pct = min(eff_stop_pct, _early_pct)

            # ── 止损决策（优先级从高到低）────────────────────────────
            # T172: 时间风险桶使用更紧止损（触顶/触底窗口入场，40%止损）
            bet_bucket = (bet.get("bet_bucket") or "").lower()
            if "time_risk" in bet_bucket:
                _time_risk_stop = float(sl_cfg.get("time_risk_stop_pct", 0.40))
                eff_stop_pct = min(eff_stop_pct, _time_risk_stop)

            # 天气 B_edge 使用更低的地板价（入场价本身就低，全局 floor 会误杀）
            if bet_category == "weather" and (
                bet_bucket == "b_edge" or entry_price < 0.25
            ):
                eff_floor = weather_edge_floor

            # ── ExitPolicy 策略链（替代内联 weather_confidence_hold-SL）──
            # 使用 PolicyChain 统一评估是否应该持有
            # 注意：地板止损在内部不会被 hold（_UNBLOCKABLE_SL_TRIGGERS）
            # 此处先判断地板止损，再由 PolicyChain 审查非地板触发

            if current_price < eff_floor:
                # 1. 地板止损：市场已判死刑，没有理由继续持有
                trigger = "floor" if not is_live else "live_floor"
            elif return_pct <= -eff_stop_pct:
                # 2. 标准止损：深度亏损，减少机会成本
                trigger = "standard_sl" if not is_live else "live_standard_sl"
            elif current_price < eff_reversal and entry_price >= 0.40:
                # 3. 方向翻转止损
                trigger = "reversal" if not is_live else "live_reversal"
            elif expiry_trigger:
                # 4. 临近到期止损
                trigger = "expiry_sl"
            elif (
                bet_category == "sports"
                and hours_to_expiry is not None
                and hours_to_expiry <= -live_post_expiry
                and return_pct
                < float(
                    (get("stop_loss") or {}).get("sports_expired_min_loss_pct", -0.10)
                )
            ):
                # 5. 体育赛后强制清仓
                trigger = "sports_expired"
            else:
                # 不满足止损条件 → 重置 armed 计数
                self._armed_ticks.pop(market_id, None)
                continue

            # ── PolicyChain HOLD 门控（地板/effective_resolution 不拦截）──
            _sl_policy_chain = getattr(self, "_exit_policy_chain", None)
            if _sl_policy_chain is not None and trigger not in (
                "floor",
                "live_floor",
                "effective_resolution",
            ):
                from src.exit_policy.base import BetContext

                _sl_ctx = BetContext.from_bet(
                    bet,
                    current_price,
                    hours_to_expiry,
                    trigger_type="stop_loss",
                    trigger_name=trigger,
                )
                _sl_advice = _sl_policy_chain.should_hold(_sl_ctx, config_fn=get)
                if _sl_advice is not None:
                    self._armed_ticks.pop(market_id, None)
                    continue  # PolicyChain 建议持有，跳过此止损

            # ── T188: 天气气象数据止损门控 ──────────────────────────
            # 在止损执行前，用实时气象观测数据验证是否应继续持有
            # 仅适用于weather赛道，排除floor/effective_resolution硬止损
            if bet_category == "weather" and trigger not in (
                "floor",
                "live_floor",
                "effective_resolution",
            ):
                _veto, _veto_reason = self._weather_stop_loss_veto(
                    bet, trigger, return_pct
                )
                if _veto:
                    self._armed_ticks.pop(market_id, None)
                    logger.info(
                        f"[WeatherVeto] 气象数据门控拦截止损 "
                        f"bet={bet.get('bet_id', '')[:8]}... "
                        f"| {bet.get('question', '')[:60]} "
                        f"| {trigger} ret={return_pct:+.1%} "
                        f"| 原因: {_veto_reason}"
                    )
                    continue

            # ── 流动性真空门控 ──────────────────────────────
            # 地板止损跳过门控（价格已极低,无需确认）
            # market 模式跳过全部门控：直接按盘口价止损（天气/体育流动性好）
            liq_cfg = sl_cfg.get("liquidity_guard") or {}
            liq_enabled = liq_cfg.get("enabled", True)
            ticks_required = int(liq_cfg.get("armed_ticks_required", 2))
            wide_spread_extra_ticks = int(liq_cfg.get("wide_spread_extra_ticks", 0))
            spread_max = float(liq_cfg.get("fair_ref_spread_max", 0.15))

            _skip_liq_guard = (
                not liq_enabled
                # market 模式下：仅对非天气类别跳过 LiqGuard（T251修复）
                # 天气/anchor 类别仍需门控：防止在假盘口价下误触发止损
                or (
                    use_raw_price
                    and bet_category
                    in (
                        "sports",
                        "crypto",
                        "geopolitics",
                        "finance",
                        "popular_culture",
                        "technology",
                    )
                )
                or trigger in ("floor", "live_floor", "sports_expired")
            )
            if _skip_liq_guard:
                if use_raw_price and trigger not in (
                    "floor",
                    "live_floor",
                    "sports_expired",
                ):
                    logger.debug(
                        f"[StopLoss-Market] {bet_category} 模式跳过 LiqGuard "
                        f"{market_id[:12]}: {trigger} "
                        f"sell={current_price:.3f} entry={entry_price:.3f} "
                        f"return={return_pct:+.1%}"
                    )
            elif trigger not in ("floor", "live_floor", "sports_expired"):
                # anchor 模式：保留 AnchorPrice + FairRef + armed 多重门控
                fair_ref = self._compute_fair_ref(mdata, outcome)
                ticks_target = ticks_required
                # 计算以 entry_price 为锚的止损触发价
                trigger_price = entry_price * (1.0 - eff_stop_pct)

                # ── Anchor Price 防闪跌 ──────────────────────
                # 用入场价+公允价加权锚点替代单一bestBid判断
                # 来源：@c_dylanber 价格真空防护策略
                # 安全阀：深度亏损时绕过锚定保护（防止 anchor 权重过高掩盖真实损失）
                anchor_enabled = bool(liq_cfg.get("anchor_price_enabled", False))
                anchor_max_loss = float(liq_cfg.get("anchor_max_loss_override", 0.30))
                _is_deep_loss = False  # 深度亏损标志：绕过 anchor + fair_ref 双重门控
                if anchor_enabled and fair_ref is not None:
                    # 深度亏损 → 强制绕过锚定保护
                    if return_pct <= -anchor_max_loss:
                        _is_deep_loss = True  # 同时跳过后续 fair_ref 门控
                        logger.info(
                            f"[AnchorOverride] 深度亏损绕过锚定 {market_id[:12]}: "
                            f"return={return_pct:+.1%} ≤ -{anchor_max_loss:.0%} "
                            f"(entry={entry_price:.3f} bid={current_price:.3f})"
                        )
                        # 不continue, 跳过anchor检查, 继续进入armed确认流程
                    else:
                        anchor_w = float(liq_cfg.get("anchor_weight", 0.6))
                        anchor = anchor_w * entry_price + (1.0 - anchor_w) * fair_ref
                        anchor_return = (anchor - entry_price) / entry_price
                        if (
                            bet_category != "sports"
                            and anchor_return > -eff_stop_pct
                            and anchor > trigger_price
                        ):
                            # 锚定价仍在安全区间，bestBid 可能是瞬时闪跌
                            # 体育赛事价格反映真实比分（非流动性真空），跳过锚定门控
                            self._armed_ticks.pop(market_id, None)
                            logger.debug(
                                f"[AnchorPrice] 锚定安全 {market_id[:12]}: "
                                f"anchor={anchor:.3f} > trigger={trigger_price:.3f} "
                                f"(entry={entry_price:.3f} fair={fair_ref:.3f} bid={current_price:.3f})"
                            )
                            continue

                # 公允价格门控：mid-price 健康 → 市场正常,可能只是盘口瞬间撤单
                # 深度亏损时跳过此门控（anchor 已绕过，fair_ref 不应再次阻拦）
                # 体育赛事跳过：体育价格反映真实比分，非流动性真空，不适用此门控
                if (
                    not _is_deep_loss
                    and fair_ref is not None
                    and fair_ref > trigger_price
                    and bet_category != "sports"
                ):
                    bid_ask_spread = self._compute_spread(mdata)
                    if bid_ask_spread is not None and bid_ask_spread > spread_max:
                        # 价差过大 = 流动性真空，fair_ref 不可信 → 继续确认
                        ticks_target += wide_spread_extra_ticks
                    else:
                        # 市场健康，重置 armed
                        self._armed_ticks.pop(market_id, None)
                        logger.debug(
                            f"[LiqGuard] 市场健康 {market_id[:12]}: "
                            f"fair={fair_ref:.3f} > trigger={trigger_price:.3f}"
                        )
                        continue

                # 多周期确认
                # 体育赛事价格反映真实比分（非流动性瞬时噪声），无需多周期确认
                # 直接触发，避免1+分钟额外延迟造成更大亏损
                armed = self._armed_ticks.get(market_id, 0) + 1
                self._armed_ticks[market_id] = armed
                if armed < ticks_target and bet_category != "sports":
                    logger.info(
                        f"[LiqGuard] armed {armed}/{ticks_target} "
                        f"{market_id[:12]}: {trigger} "
                        f"sell={current_price:.3f} fair={fair_ref}"
                    )
                    continue  # 尚未达到确认次数，不止损
                elif bet_category == "sports" and armed < ticks_required:
                    logger.info(
                        f"[LiqGuard-Sports] 体育快速路径armed跳过 {market_id[:12]}: "
                        f"{trigger} sell={current_price:.3f} fair={fair_ref}"
                    )

            # 已确认止损（或 floor 跳过门控）→ 重置 armed
            self._armed_ticks.pop(market_id, None)

            # T195: 止损日志去重 — 每个 (bet_id, trigger) 只记录一次 INFO 日志
            _sl_event_key = (bet.get("bet_id", ""), trigger)
            _sl_first_time = _sl_event_key not in self._sl_logged_events
            _sl_log_fn = logger.info if _sl_first_time else logger.debug
            _sl_log_fn(
                f"[StopLoss] [{trigger}] | {bet['question'][:50]}... "
                f"| {outcome} {entry_price:.3f}→{current_price:.3f} "
                f"| 亏损={return_pct:+.1%} | cat={bet_category} "
                f"mode={stoploss_mode} live={is_live} floor={eff_floor} "
                f"stop={eff_stop_pct:.0%} "
                f"rev={eff_reversal}"
            )

            # ── T239: 结构化止损事件日志（T195: 去重，每bet+trigger仅记录一次）──
            if _sl_first_time:
                try:
                    from src.utils.db import log_stop_loss_event

                    _sl_conditions = {
                        "floor_triggered": current_price < eff_floor,
                        "standard_triggered": return_pct <= -eff_stop_pct,
                        "reversal_triggered": (
                            current_price < eff_reversal and entry_price >= 0.40
                        ),
                        "expiry_triggered": expiry_trigger,
                        "is_live": is_live,
                    }
                    # 天气时间加权条件（仅天气赛道）
                    if bet_category == "weather":
                        _tw = sl_cfg.get("weather_time_weighted_stop") or {}
                        _sl_conditions["force_stop_triggered"] = return_pct <= -float(
                            _tw.get("force_stop_pct", 0.80)
                        )
                        if hours_to_expiry is not None:
                            _tw_far = float(_tw.get("far_hours", 12.0))
                            _tw_mid = float(_tw.get("mid_hours", 4.0))
                            _sl_conditions["tw_far_triggered"] = (
                                hours_to_expiry > _tw_far
                                and return_pct <= -float(_tw.get("far_stop_pct", 0.75))
                            )
                            _sl_conditions["tw_mid_triggered"] = (
                                _tw_mid < hours_to_expiry <= _tw_far
                                and return_pct <= -float(_tw.get("mid_stop_pct", 0.70))
                            )
                            _sl_conditions["tw_near_triggered"] = (
                                hours_to_expiry <= _tw_mid
                                and return_pct <= -float(_tw.get("near_stop_pct", 0.75))
                            )

                    _sl_context = {
                        "stoploss_mode": stoploss_mode,
                        "eff_floor": eff_floor,
                        "eff_stop_pct": eff_stop_pct,
                        "eff_reversal": eff_reversal,
                        "outcome": outcome,
                        "bet_bucket": bet_bucket,
                    }
                    _size = float(bet.get("size_usdc") or 0)
                    _shares = _size / entry_price if entry_price > 0 else 0
                    # T189-G: 日志PnL也扣费
                    _log_entry_fee = float(bet.get("entry_fee_usdc") or 0)
                    _pnl = round(_shares * current_price - _size - _log_entry_fee, 4)

                    log_stop_loss_event(
                        bet_id=bet.get("bet_id", ""),
                        question=bet.get("question", ""),
                        category=bet_category,
                        trigger_type=trigger,
                        entry_price=entry_price,
                        current_price=current_price,
                        return_pct=return_pct,
                        threshold_pct=eff_stop_pct,
                        size_usdc=_size,
                        hours_to_expiry=hours_to_expiry,
                        pnl=_pnl,
                        conditions=_sl_conditions,
                        context=_sl_context,
                    )
                    self._sl_logged_events.add(_sl_event_key)
                except Exception as _e:
                    logger.debug(f"[StopLoss] 事件日志写入失败: {_e}")
            else:
                logger.debug(
                    f"[StopLoss] 事件已记录,跳过重复写入 | bet={bet.get('bet_id', '')[:8]}... trigger={trigger}"
                )

            bet_copy = dict(bet)
            bet_copy["exit_price"] = current_price
            bet_copy["return_pct"] = return_pct
            bet_copy["trigger"] = trigger
            stops.append(bet_copy)

        return stops

    # ------------------------------------------------------------------
    # T188: 天气气象数据止损门控（Weather Stop-Loss Veto Gate）
    # ------------------------------------------------------------------
    # 当天气仓位触发止损时，用实时气象观测数据验证温度是否仍然
    # 对我方有利。如果温度远未达到赌注阈值，则VETO止损、继续持有。
    # 设计原则：
    #   - 仅延迟止损（不核销/关闭仓位）→ SUPREME 1 合规
    #   - 数据获取失败 → 不VETO（failsafe）
    #   - max_veto_count 限制 → 防止无限持有
    #   - floor 和 effective_resolution 不可被VETO
    # ------------------------------------------------------------------

    # 每个 bet_id 的已VETO次数
    _weather_veto_counts: dict[str, int] = {}
    # 每个 bet_id 的上次VETO时间戳（cooldown用）
    _weather_veto_last_ts: dict[str, float] = {}

    def _weather_stop_loss_veto(
        self, bet: dict, trigger: str, return_pct: float
    ) -> tuple[bool, str]:
        """
        天气止损气象数据门控：检查实时温度是否支持继续持有。

        Returns:
            (True, reason) 如果应该VETO止损（继续持有）
            (False, reason) 如果不应VETO（放行止损）
        """
        veto_cfg = (get("stop_loss") or {}).get("weather_veto") or {}
        if not veto_cfg.get("enabled", True):
            return False, "weather_veto已禁用"

        bet_id = bet.get("bet_id", "")
        question = bet.get("question", "")
        outcome = (bet.get("outcome") or "").strip().upper()

        # ── 1. 检查veto次数上限 ──
        max_veto = int(veto_cfg.get("max_veto_count", 3))
        current_count = self._weather_veto_counts.get(bet_id, 0)
        if current_count >= max_veto:
            return False, f"已达最大VETO次数({current_count}/{max_veto})"

        # ── 2. 冷却检查（防止同一周期重复VETO日志刷屏） ──
        cooldown_sec = float(veto_cfg.get("cooldown_seconds", 300))
        now = time.time()
        last_ts = self._weather_veto_last_ts.get(bet_id, 0)
        if last_ts > 0 and (now - last_ts) < cooldown_sec:
            # 冷却期内静默VETO（延续上次决策）
            return True, f"冷却期内(距上次{int(now - last_ts)}s)"

        # ── 3. 解析问题获取城市/阈值/单位 ──
        try:
            from src.utils.weather_fetcher import (
                get_city_local_hour,
                parse_weather_question,
            )
        except ImportError:
            return False, "weather_fetcher导入失败"

        parsed = parse_weather_question(question)
        if not parsed:
            return False, "无法解析天气问题"

        city = parsed["city"]
        unit = parsed["unit"]  # "C" or "F"
        direction = parsed["direction"]  # exact/range/gte/lte
        variable = parsed["variable"]  # temp_max/temp_min

        # ── 4. 获取实时温度（METAR优先，T233修复） ──
        from src.utils.weather_fetcher import fetch_realtime_temperature

        current_temp_c = fetch_realtime_temperature(city)
        if current_temp_c is None:
            return False, f"无法获取{city}实时温度"

        current_temp_f = current_temp_c * 9 / 5 + 32

        current_temp = current_temp_c if unit == "C" else current_temp_f

        # ── 5. 获取安全边距 ──
        safe_margin_c = float(veto_cfg.get("safe_margin_c", 3))
        safe_margin_f = float(veto_cfg.get("safe_margin_f", 5))
        safe_margin = safe_margin_c if unit == "C" else safe_margin_f

        # ── 6. 获取当地时间判断是否在升温/降温窗口 ──
        local_hour = get_city_local_hour(city)
        peak_end_hour = int(veto_cfg.get("peak_window_end_hour", 16))
        peak_start_hour = int(veto_cfg.get("peak_window_start_hour", 11))
        peak_margin_mult = float(veto_cfg.get("peak_window_margin_multiplier", 1.5))
        if (
            variable == "temp_max"
            and outcome == "NO"
            and local_hour is not None
            and peak_start_hour <= local_hour <= peak_end_hour
        ):
            safe_margin *= peak_margin_mult

        # ── 7. 温度对比决策 ──
        # 根据 direction 和 outcome 判断温度是否对我方有利
        if direction == "exact":
            # "Will temp be X°C?" — NO赢=温度不是X
            threshold = parsed["threshold_value"]
            temp_diff = abs(current_temp - threshold)
            if outcome == "NO":
                # NO赢: 温度远离阈值 → 有利
                if temp_diff > safe_margin:
                    self._weather_veto_counts[bet_id] = current_count + 1
                    self._weather_veto_last_ts[bet_id] = now
                    return True, (
                        f"当前{current_temp:.1f}°{unit}距阈值{threshold}°{unit}"
                        f"差{temp_diff:.1f}°>{safe_margin}° 有利于NO"
                    )
            else:
                # YES赢: 温度接近阈值 → 有利
                if temp_diff <= safe_margin:
                    self._weather_veto_counts[bet_id] = current_count + 1
                    self._weather_veto_last_ts[bet_id] = now
                    return True, (
                        f"当前{current_temp:.1f}°{unit}距阈值{threshold}°{unit}"
                        f"差{temp_diff:.1f}°<={safe_margin}° 有利于YES"
                    )
        elif direction == "range":
            # "Will temp be between LOW-HIGH?" — NO赢=温度不在范围内
            low = parsed["threshold_low"]
            high = parsed["threshold_high"]
            if outcome == "NO":
                # 温度远离范围 → 有利
                if (
                    current_temp < low - safe_margin
                    or current_temp > high + safe_margin
                ):
                    self._weather_veto_counts[bet_id] = current_count + 1
                    self._weather_veto_last_ts[bet_id] = now
                    return True, (
                        f"当前{current_temp:.1f}°{unit}在{low}-{high}范围外"
                        f"(margin={safe_margin}°) 有利于NO"
                    )
            else:
                # YES赢: 温度在范围内 → 有利
                if low - safe_margin <= current_temp <= high + safe_margin:
                    self._weather_veto_counts[bet_id] = current_count + 1
                    self._weather_veto_last_ts[bet_id] = now
                    return True, (
                        f"当前{current_temp:.1f}°{unit}在{low}-{high}范围附近"
                        f" 有利于YES"
                    )
        elif direction == "gte":
            # "Will temp be X°F or higher?" — NO赢=温度低于X
            threshold = parsed["threshold_value"]
            if outcome == "NO":
                # 温度远低于阈值 → 有利
                if current_temp < threshold - safe_margin:
                    self._weather_veto_counts[bet_id] = current_count + 1
                    self._weather_veto_last_ts[bet_id] = now
                    return True, (
                        f"当前{current_temp:.1f}°{unit}<{threshold}-{safe_margin}"
                        f"={threshold - safe_margin}° 有利于NO"
                    )
            else:
                # YES赢: 温度已高于阈值 → 有利
                if current_temp >= threshold - safe_margin:
                    # 如果在升温窗口且已接近阈值，有利
                    self._weather_veto_counts[bet_id] = current_count + 1
                    self._weather_veto_last_ts[bet_id] = now
                    return True, (
                        f"当前{current_temp:.1f}°{unit}>={threshold - safe_margin}°"
                        f" 有利于YES"
                    )
        elif direction == "lte":
            # "Will temp be X°F or lower?" — NO赢=温度高于X
            threshold = parsed["threshold_value"]
            if outcome == "NO":
                # 温度远高于阈值 → 有利
                if current_temp > threshold + safe_margin:
                    self._weather_veto_counts[bet_id] = current_count + 1
                    self._weather_veto_last_ts[bet_id] = now
                    return True, (
                        f"当前{current_temp:.1f}°{unit}>{threshold}+{safe_margin}"
                        f"={threshold + safe_margin}° 有利于NO"
                    )
            else:
                # YES赢: 温度低于阈值 → 有利
                if current_temp <= threshold + safe_margin:
                    self._weather_veto_counts[bet_id] = current_count + 1
                    self._weather_veto_last_ts[bet_id] = now
                    return True, (
                        f"当前{current_temp:.1f}°{unit}<={threshold + safe_margin}°"
                        f" 有利于YES"
                    )

        # ── 8. 升温窗口额外保护（仅 temp_max + NO 方向） ──
        # 如果当前在升温前(早晨)且temp_max方向，当前温度不代表日最高
        if (
            variable == "temp_max"
            and outcome == "NO"
            and local_hour is not None
            and local_hour < peak_start_hour
        ):
            # 早晨时段，当日还未达到最高温
            # 如果当前温度比阈值低很多，更有信心NO会赢
            threshold = parsed.get("threshold_value") or (
                (parsed.get("threshold_low", 0) + parsed.get("threshold_high", 0)) / 2
            )
            temp_diff = threshold - current_temp if threshold else 0
            morning_margin = safe_margin * 1.5  # 早晨用更宽的margin
            if temp_diff > morning_margin:
                self._weather_veto_counts[bet_id] = current_count + 1
                self._weather_veto_last_ts[bet_id] = now
                return True, (
                    f"早晨{local_hour}时(升温前) 当前{current_temp:.1f}°{unit}"
                    f"距阈值{threshold}°差{temp_diff:.1f}°"
                    f">{morning_margin:.0f}° 升温窗口保护"
                )

        # ── 9. T211: 已过峰值窗口保护（temp_max/gte + NO方向）──
        # 如果当日最高温已过峰值窗口(>16时)且观测温度仍低于阈值，
        # NO大概率获胜，应阻止止损。
        # 事故案例：Seoul 23°C < 24°C，已过中午峰值，但市场价格波动触发了止损。
        if (
            variable == "temp_max"
            and outcome == "NO"
            and direction in ("gte", "exact", "range")
            and local_hour is not None
            and local_hour >= peak_end_hour
        ):
            threshold = parsed.get("threshold_value") or (
                (parsed.get("threshold_low", 0) + parsed.get("threshold_high", 0)) / 2
            )
            if threshold and current_temp < threshold:
                self._weather_veto_counts[bet_id] = current_count + 1
                self._weather_veto_last_ts[bet_id] = now
                return True, (
                    f"已过峰值{local_hour}时(>{peak_end_hour}时) "
                    f"当前{current_temp:.1f}°{unit}<阈值{threshold}°{unit} "
                    f"NO大概率获胜 已过峰值保护"
                )

        return False, (
            f"温度{current_temp:.1f}°{unit}不满足VETO条件"
            f"(dir={direction} out={outcome})"
        )

    def close_position_stop_loss(self, bet: dict, db_path=None) -> None:
        """
        以当前市场价模拟止损卖出，记录主动止损结果。
        P&L 计算：shares = size/entry_price；proceeds = shares × exit_price（亏损）
        """
        from src.utils import db as _db

        _db_path = db_path if db_path is not None else _db._DB_PATH

        size = float(bet.get("size_usdc", 0.0))
        entry_price = float(bet.get("market_price") or 0.5)
        exit_price = float(bet.get("exit_price", 0.0))

        if entry_price <= 0 or exit_price <= 0:
            logger.warning(f"[StopLoss] 无效价格, 跳过: {bet.get('bet_id')}")
            return

        # Phase40: 优先使用实际到账金额（余额差底层数据）作为权威收益
        # 避免 Data API 校准价格导致 proceeds 与钱包实际收到不符
        actual_proceeds = float(bet.get("actual_proceeds") or 0)
        if actual_proceeds > 0:
            proceeds = round(actual_proceeds, 4)
        else:
            shares = size / entry_price
            gross = round(shares * exit_price, 4)
            category = bet.get("category", "weather")
            estimated_fees = self._estimate_dry_run_fees(
                shares, entry_price, exit_price, category
            )
            proceeds = round(gross - estimated_fees, 4)
        pnl = round(proceeds - size, 4)
        # T189-G: 扣除买入+卖出手续费得到真实PnL
        _sl_entry_fee = float(bet.get("entry_fee_usdc") or 0)
        pnl = round(proceeds - size - _sl_entry_fee, 4)
        actual_return_pct = pnl / size if size > 0 else 0.0

        _db.close_paper_bet_stop_loss(
            bet_id=bet["bet_id"],
            exit_price=exit_price,
            pnl=pnl,
            payout=proceeds,
            db_path=_db_path,
        )
        # T189-F: 记录卖出手续费
        # T228追加: 退出费率使用 sell_mode 配置（而非入场 order_type）
        _sl_shares = size / entry_price if entry_price > 0 else 0
        _sl_cat = bet.get("category", "weather")
        _sl_ot = _resolve_exit_order_type(_sl_cat, bet.get("order_type", "taker"))
        _sl_exit_fee, _ = _db.calc_pm_fee(_sl_shares, exit_price, _sl_cat, _sl_ot)
        if _sl_exit_fee > 0:
            _db.update_bet_fees(
                bet["bet_id"], exit_fee_usdc=_sl_exit_fee, db_path=_db_path
            )
            # T189-G: 卖出手续费也要反映到pnl中
            pnl = round(pnl - _sl_exit_fee, 4)
            with _db.connect(_db_path) as _conn:
                _conn.execute(
                    "UPDATE paper_bets SET pnl=? WHERE bet_id=?", (pnl, bet["bet_id"])
                )
        logger.info(
            f"[StopLoss] 🛑 止损完成 | bet={bet['bet_id'][:8]}... "
            f"| 入场={entry_price:.3f} 离场={exit_price:.3f} "
            f"| P&L={pnl:+.4f} USDC ({actual_return_pct:+.1%})"
        )
        # T195: 止损完成后清理去重记录，释放内存
        _bid = bet.get("bet_id", "")
        self._sl_logged_events.discard((_bid, bet.get("trigger", "")))
        from src.utils.telegram_notifier import get_notifier

        get_notifier().notify_position_closed(
            question=bet.get("question", ""),
            close_type="stop_loss",
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            size_usdc=size,
            category=bet.get("category", ""),
        )

    def close_position_wu_temp_breach(
        self,
        bet: dict,
        observed_temp_c: float,
        threshold_c: float,
        obs_source: str = "wunderground",
        db_path=None,
    ) -> None:
        """
        WU Daily Observations 实测温度已达投注阈值，强制平仓（"死刑" 策略）。

        当气象站实测温度已触及/超过投注问题中的温度阈值，市场结算结果已基本注定，
        此方法立即以当前市价平仓以止损，避免等待价格归零。

        Args:
            bet:            仓位字典（含 bet_id, market_price, exit_price, size_usdc 等）
            observed_temp_c: WU 实测最高温（°C）
            threshold_c:    投注阈值温度（°C）
            obs_source:     数据来源名称（"wunderground" / "open-meteo"）
            db_path:        SQLite 路径（None=使用默认路径）
        """
        from src.utils import db as _db

        _db_path = db_path if db_path is not None else _db._DB_PATH

        size = float(bet.get("size_usdc", 0.0))
        entry_price = float(bet.get("market_price") or 0.5)
        exit_price = float(bet.get("exit_price", 0.0))

        if entry_price <= 0 or exit_price < 0:
            logger.warning(f"[WUTempObserver] 无效价格, 跳过平仓: {bet.get('bet_id')}")
            return

        actual_proceeds = float(bet.get("actual_proceeds") or 0)
        if actual_proceeds > 0:
            proceeds = round(actual_proceeds, 4)
        else:
            shares = size / entry_price if entry_price > 0 else 0
            gross = round(shares * exit_price, 4)
            category = bet.get("category", "weather")
            estimated_fees = self._estimate_dry_run_fees(
                shares, entry_price, exit_price, category
            )
            proceeds = round(gross - estimated_fees, 4)
        pnl = round(proceeds - size, 4)
        # T189-G: 扣除买入手续费
        _wu_entry_fee = float(bet.get("entry_fee_usdc") or 0)
        pnl = round(proceeds - size - _wu_entry_fee, 4)
        actual_return_pct = pnl / size if size > 0 else 0.0

        _db.close_paper_bet_wu_temp_breach(
            bet_id=bet["bet_id"],
            exit_price=exit_price,
            pnl=pnl,
            payout=proceeds,
            db_path=_db_path,
        )
        logger.warning(
            f"[WUTempObserver] 🌡️ 温度死刑平仓 | bet={bet['bet_id'][:8]}... "
            f"| 实测={observed_temp_c:.1f}°C 阈值={threshold_c:.1f}°C 来源={obs_source} "
            f"| 入场={entry_price:.3f} 离场={exit_price:.3f} "
            f"| P&L={pnl:+.4f} USDC ({actual_return_pct:+.1%})"
        )
        from src.utils.telegram_notifier import get_notifier

        _outcome = (bet.get("outcome") or "").upper()
        _tg_question = (
            f"[🌡️{observed_temp_c:.1f}°C≥{threshold_c:.1f}°C] "
            + bet.get("question", "")[:80]
        )
        get_notifier().notify_position_closed(
            question=_tg_question,
            close_type="wu_temp_breach",
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            size_usdc=size,
            category=bet.get("category", ""),
        )

    # ------------------------------------------------------------------
    # 平台持仓对账（Phase46）
    # ------------------------------------------------------------------

    def check_platform_reconciliation(self, wallet_address: str) -> dict:
        """
        检查 Polymarket 平台实际持仓与 DB 的一致性。
        每 N 个监控周期运行一次（main.py 控制频率）。

        核心逻辑（按 conditionId 精确匹配）：
        1. 平台持仓但 DB 已关闭（write_off/stop_loss 等）→ 自动重新打开，重入监控循环
        2. 可赎回仓位（curPrice >= 0.98）→ 告警，需在平台手动 Redeem
        3. 死亡仓位（curPrice <= 0.001, DB 仍 OPEN）→ 保持OPEN等待平台结算（T70: 禁止自动核销）

        重开仓位后，止损/止盈逻辑在下一个 monitor_positions 周期自动执行。
        """
        from src.utils import db as _db

        if not wallet_address:
            return {}
        result: dict = {
            "redeemable": [],
            "dead_auto_closed": 0,
            "ghost_reopened": 0,
            "total_platform": 0,
        }
        try:
            resp = self.session.get(
                f"https://data-api.polymarket.com/positions"
                f"?user={wallet_address}&sizeThreshold=0",
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"[Reconcile] 平台API返回 {resp.status_code}")
                return result
            positions = resp.json()
        except Exception as e:
            logger.warning(f"[Reconcile] 平台API请求失败: {e}")
            return result

        if not isinstance(positions, list):
            return result

        result["total_platform"] = len(positions)

        # --- 构建平台持仓索引（conditionId → pos）---
        plat_by_cid: dict[str, dict] = {}
        for pos in positions:
            cid = str(pos.get("conditionId") or "")
            if cid:
                plat_by_cid[cid] = pos

        # --- 获取 DB 中所有仓位（OPEN + 最近关闭）---
        all_bets = _db.get_open_paper_bets()  # OPEN 仓位
        recent_closed = _db.get_recently_closed_bets(days=30)  # 最近30天关闭的

        open_cids: set[str] = {str(b.get("condition_id") or "") for b in all_bets}
        open_by_cid: dict[str, dict] = {
            str(b.get("condition_id") or ""): b
            for b in all_bets
            if b.get("condition_id")
        }

        # --- 核心检测：平台有持仓但 DB 已关闭 → 幽灵仓位处理 ---
        for closed_bet in recent_closed:
            cid = str(closed_bet.get("condition_id") or "")
            if not cid or cid not in plat_by_cid:
                continue  # 平台没有，正常结算/卖出

            if cid in open_cids:
                continue  # DB 也是 OPEN，一致，跳过

            # 平台有持仓，DB 却已关闭 → 幽灵仓位
            pos = plat_by_cid[cid]
            size = float(pos.get("size") or 0)
            cur_price = float(pos.get("curPrice") or 0)
            exit_type = closed_bet.get("exit_type", "unknown")
            bet_id = closed_bet["bet_id"]
            title = str(pos.get("title") or "")[:60]

            if size < 0.01:
                continue  # 余额太小，忽略

            # Phase 48: DB 已有 resolved_outcome 但平台仍持有
            # 关键判断：链上仓位是否 redeemable？
            #   - NOT redeemable = 平台尚未正式结算 → 系统自解析有误，必须重开
            #   - redeemable + curPrice>=0.98 → 重开让 certainty_sell 卖出
            if closed_bet.get("resolved_outcome"):
                is_redeemable = pos.get("redeemable", False)
                # T70: 死亡价格禁止重开 — 价格≤阈值时链上无买方，重开只会引发无限卖出循环
                _no_reopen_price = float(
                    (get("position_management") or {}).get(
                        "dead_position_no_reopen_price", 0.01
                    )
                )
                if cur_price <= _no_reopen_price and not is_redeemable:
                    logger.debug(
                        f"[Reconcile] 死亡价格禁止重开 {bet_id[:8]} "
                        f"price={cur_price:.5f} ≤ {_no_reopen_price} "
                        f"→ 维持关闭状态，等待平台结算"
                    )
                    continue
                if not is_redeemable and size >= 0.01:
                    # 链上仍活跃（未被平台结算）→ 自解析有误，重开
                    reopened = _db.reopen_ghost_position(bet_id)
                    if reopened:
                        open_cids.add(cid)
                        result["ghost_reopened"] += 1
                        logger.warning(
                            f"[Reconcile] 🔄 自解析仓位链上仍活跃 | "
                            f"bet={bet_id[:8]} size={size:.2f} "
                            f"curPrice={cur_price:.4f} redeemable=False "
                            f"→ 重开（平台尚未正式结算）"
                        )
                    continue
                if is_redeemable and cur_price >= 0.98 and size >= 0.5:
                    reopened = _db.reopen_ghost_position(bet_id)
                    if reopened:
                        open_cids.add(cid)
                        result["ghost_reopened"] += 1
                        logger.warning(
                            f"[Reconcile] 🔄 已结算可赎回仓位 | "
                            f"bet={bet_id[:8]} size={size:.2f} "
                            f"curPrice={cur_price:.4f} → 重开以通过 "
                            f"certainty_sell 卖出释放资金"
                        )
                    continue
                logger.debug(
                    f"[Reconcile] 跳过已结算仓位 {bet_id[:8]} "
                    f"resolved={closed_bet['resolved_outcome']} size={size:.2f}"
                )
                continue

            # 市场已结算（curPrice≥0.90）→ 正常profit_take/结算后的粉尘代币
            # DB已关闭是正确状态，后续redeemable检查会提醒赎回
            if cur_price >= 0.90:
                continue

            # 死亡市场（curPrice≤0.001）→ 代币几乎无价值，CLOB无法卖出（没有买方）
            # 等待平台正式结算归零。DB write_off是正确的最终状态，不应重开引发循环
            if cur_price <= 0.001:
                logger.debug(
                    f"[Reconcile] 死亡市场 {bet_id[:8]} price={cur_price:.5f}"
                    f" size={size:.2f}份 ≈ ${size*cur_price:.4f}，维持write_off状态"
                )
                continue

            # Phase 48: 对主动关闭的仓位(profit_take/stop_loss/manual_exit),
            # 仅在链上仍活跃且有重要价值时重开
            # write_off 特殊处理：链上仍活跃且价值 > $0.10 → 重开
            _INTENTIONAL_EXIT_TYPES = (
                "profit_take",
                "stop_loss",
                "manual_exit",
            )
            is_redeemable = pos.get("redeemable", False)
            _reopen_wo_val = float(
                (get("position_management") or {}).get(
                    "reconcile_writeoff_reopen_value", 0.10
                )
            )
            _reopen_exit_val = float(
                (get("position_management") or {}).get(
                    "reconcile_exit_reopen_value", 0.50
                )
            )
            if exit_type == "write_off":
                chain_value = size * cur_price
                if not is_redeemable and chain_value > _reopen_wo_val:
                    reopened = _db.reopen_ghost_position(bet_id)
                    if reopened:
                        open_cids.add(cid)
                        result["ghost_reopened"] += 1
                        logger.warning(
                            f"[Reconcile] 🔄 write_off仓位链上仍活跃 | "
                            f"bet={bet_id[:8]} size={size:.2f} "
                            f"curPrice={cur_price:.4f} val=${chain_value:.2f} "
                            f"→ 重开（平台未结算）"
                        )
                    continue
                logger.debug(
                    f"[Reconcile] 维持write_off {bet_id[:8]} "
                    f"price={cur_price:.5f} redeemable={is_redeemable}"
                )
                continue
            if exit_type in _INTENTIONAL_EXIT_TYPES:
                # Phase 56 safety net (Bug 225): 即使是 stop_loss/profit_take，
                # 如果链上仍有大量持仓且未结算，说明卖出可能失败了 → 重开
                chain_value = size * cur_price
                if not is_redeemable and chain_value > _reopen_exit_val:
                    reopened = _db.reopen_ghost_position(bet_id)
                    if reopened:
                        open_cids.add(cid)
                        result["ghost_reopened"] += 1
                        logger.warning(
                            f"[Reconcile] 🔄 {exit_type}仓位链上仍有大额持仓 | "
                            f"bet={bet_id[:8]} size={size:.2f} "
                            f"curPrice={cur_price:.4f} val=${chain_value:.2f} "
                            f"→ 重开（卖出可能失败）"
                        )
                    continue
                logger.debug(
                    f"[Reconcile] 跳过已关闭仓位 {bet_id[:8]} "
                    f"exit_type={exit_type} size={size:.2f} curPrice={cur_price:.4f}"
                    f"（等待市场结算）"
                )
                continue

            reopened = _db.reopen_ghost_position(bet_id)
            if reopened:
                open_cids.add(cid)  # 避免重复处理
                open_by_cid[cid] = (
                    closed_bet  # 同步更新查找表（供下方死亡仓位检测使用）
                )
                result["ghost_reopened"] += 1
                logger.warning(
                    f"[Reconcile] 🔄 幽灵仓位重开 | bet={bet_id[:8]} "
                    f"exit_type_was={exit_type} size={size:.2f} curPrice={cur_price:.4f}"
                    f" | {title}"
                )
            else:
                logger.error(f"[Reconcile] 重开失败 bet={bet_id[:8]} | {title}")

        if result["ghost_reopened"] > 0:
            logger.warning(
                f"[Reconcile] ✅ 共重开 {result['ghost_reopened']} 个幽灵仓位，"
                f"将在下一监控周期执行止损/止盈"
            )

        # --- 检测用户手动卖出：DB OPEN 但平台已无持仓 ---
        # 场景：用户在 Polymarket 平台手动卖出，平台持仓消失，DB 仍显示 OPEN
        import time as _time_mod

        _MANUAL_SELL_MIN_AGE = 15 * 60  # 至少15分钟前的仓位才检测（避免竞态条件）
        result["manual_sell_closed"] = 0
        now_ts = _time_mod.time()

        # T176 Fix: 防护 Data API 返回空/异常少量数据导致批量误判手动卖出
        # 事故: 2026-04-10 Data API临时返回0个仓位 → 8个OPEN仓位被全部错误关闭
        _open_with_cid = [b for b in all_bets if b.get("condition_id")]
        if len(_open_with_cid) > 0 and len(plat_by_cid) == 0:
            logger.warning(
                f"[Reconcile] ⚠️ Data API 返回 0 个平台仓位，"
                f"但 DB 有 {len(_open_with_cid)} 个 OPEN 仓位 — "
                f"疑似 API 临时故障，跳过手动卖出检测（SUPREME 1 防护）"
            )
            return result

        # 可疑场景：平台仓位骤降超过 50%（相对于DB OPEN数量）
        if len(_open_with_cid) >= 4 and len(plat_by_cid) < len(_open_with_cid) * 0.5:
            logger.warning(
                f"[Reconcile] ⚠️ 平台仓位({len(plat_by_cid)})远少于 DB OPEN({len(_open_with_cid)}) — "
                f"疑似 Data API 数据不完整，跳过手动卖出检测（SUPREME 1 防护）"
            )
            return result

        for bet in all_bets:
            cid = str(bet.get("condition_id") or "")
            if not cid:
                continue  # 无 conditionId 无法精确判断
            if cid in plat_by_cid:
                continue  # 平台仍有持仓，正常

            # 仓位年龄守卫：防止刚创建的仓位误判
            ts_entered = float(bet.get("ts_entered") or 0)
            if now_ts - ts_entered < _MANUAL_SELL_MIN_AGE:
                continue

            # 平台无持仓 → 进一步判断：市场是否已关闭（由 outcome_tracker 的结算逻辑处理）
            # 若市场已关闭则跳过，等待结算日志自动处理
            market_id = str(bet.get("market_id") or "")
            _bet_slug = str(bet.get("market_slug") or "")
            market_still_active = True  # 默认假设活跃
            if market_id:
                try:
                    if market_id.startswith("0x"):
                        _q_params = (
                            {"slug": _bet_slug}
                            if _bet_slug
                            else {"condition_id": market_id}
                        )
                        mresp = self.session.get(
                            f"https://gamma-api.polymarket.com/markets",
                            params=_q_params,
                            timeout=8,
                        )
                    else:
                        mresp = self.session.get(
                            f"https://gamma-api.polymarket.com/markets/{market_id}",
                            timeout=8,
                        )
                    if mresp.status_code == 200:
                        mdata = mresp.json()
                        if isinstance(mdata, list):
                            mdata = mdata[0] if mdata else {}
                        # gamma API: active=false 或 closed=true 表示市场已结算
                        if not mdata.get("active", True) or mdata.get("closed", False):
                            market_still_active = False
                except Exception:
                    pass  # 网络异常：保守处理，跳过本次

            if not market_still_active:
                # 市场已结算，不是手动卖出，让结算检测逻辑处理
                logger.debug(
                    f"[Reconcile] 市场已关闭，非手动卖出，等待结算 | bet={bet['bet_id'][:8]}"
                )
                continue

            # 确认是用户手动卖出 → 获取当前市场价作为卖出价
            bet_id = bet["bet_id"]
            size_usdc = float(bet.get("size_usdc") or 0)
            matched_shares = float(bet.get("matched_shares") or 0)
            entry_price = float(bet.get("market_price") or 0.5)
            bet_outcome = str(bet.get("outcome") or "YES").upper()
            question = str(bet.get("question") or market_id)[:50]
            shares = (
                matched_shares
                if matched_shares > 0
                else (size_usdc / entry_price if entry_price > 0 else 0)
            )

            # 尝试从 gamma API 精确获取卖出时的市场价
            exit_price = 0.0
            if market_id and market_still_active:
                try:
                    if market_id.startswith("0x"):
                        _q_params2 = (
                            {"slug": _bet_slug}
                            if _bet_slug
                            else {"condition_id": market_id}
                        )
                        mresp2 = self.session.get(
                            f"https://gamma-api.polymarket.com/markets",
                            params=_q_params2,
                            timeout=8,
                        )
                    else:
                        mresp2 = self.session.get(
                            f"https://gamma-api.polymarket.com/markets/{market_id}",
                            timeout=8,
                        )
                    if mresp2.status_code == 200:
                        mdata2 = mresp2.json()
                        if isinstance(mdata2, list):
                            mdata2 = mdata2[0] if mdata2 else {}
                        tokens = mdata2.get("tokens") or []
                        for tok in tokens:
                            if (tok.get("outcome") or "").upper() == bet_outcome:
                                exit_price = float(tok.get("price") or 0)
                                break
                        if exit_price <= 0:
                            # 回退到 bestBid/bestAsk
                            if bet_outcome == "YES":
                                bid = mdata2.get("bestBid")
                                exit_price = float(bid) if bid else 0
                            else:
                                ask = mdata2.get("bestAsk")
                                exit_price = round(1.0 - float(ask), 4) if ask else 0
                except Exception as e:
                    logger.debug(f"[Reconcile] 获取卖出价失败 {bet_id[:8]}: {e}")

            if exit_price <= 0:
                # 无法获取价格，使用入场价作为保守估算（避免错误记录极端亏损）
                exit_price = entry_price
                logger.debug(f"[Reconcile] 使用入场价作为卖出价估算 {bet_id[:8]}")

            gross_payout = round(shares * exit_price, 4)
            category = bet.get("category", "weather")
            _est_fees = self._estimate_dry_run_fees(
                shares, entry_price, exit_price, category
            )
            payout = round(gross_payout - _est_fees, 4)
            # T189-G: 扣除买入手续费
            _me_entry_fee = float(bet.get("entry_fee_usdc") or 0)
            pnl = round(payout - size_usdc - _me_entry_fee, 4)

            try:
                _db.close_paper_bet_manual_exit(
                    bet_id=bet_id,
                    exit_price=exit_price,
                    pnl=pnl,
                    payout=payout,
                )
                result["manual_sell_closed"] += 1
                logger.warning(
                    f"[Reconcile] 👋 手动卖出 | bet={bet_id[:8]} "
                    f"exit={exit_price:.4f} pnl={pnl:+.4f} sz={size_usdc:.2f} | {question}"
                )
            except Exception as e:
                logger.error(f"[Reconcile] 手动卖出关闭失败 {bet_id[:8]}: {e}")

        if result["manual_sell_closed"] > 0:
            logger.warning(
                f"[Reconcile] 👋 共检测到 {result['manual_sell_closed']} 笔用户手动卖出，"
                f"已更新 DB 状态"
            )

        redeemable_total = 0.0

        for pos in positions:
            cur_price = 0.0
            try:
                cur_price = float(pos.get("curPrice") or 0)
            except (ValueError, TypeError):
                pass

            size = 0.0
            try:
                size = float(pos.get("size") or 0)
            except (ValueError, TypeError):
                pass

            cid = str(pos.get("conditionId") or "")
            title = str(pos.get("title") or "")[:60]
            outcome = str(pos.get("outcome") or "")
            val = size * cur_price

            # 1) 可赎回：curPrice >= 0.98（市场已结算为该方向赢家）
            # T267: 尘土仓位（val < $0.10）静默跳过，无需告警
            if cur_price >= 0.98 and size >= 0.01 and val >= 0.10:
                redeemable_total += val
                result["redeemable"].append(
                    {"title": title, "outcome": outcome, "size": size, "value": val}
                )
                # T189: 日志节流 — 相同 cid 最多每30分钟打一次 WARNING
                _rc_key = f"redeem_{cid}"
                _rc_now = time.time()
                _rc_interval = 1800  # 30 minutes
                _rc_last = _reconcile_last_log.get(_rc_key, 0)
                if _rc_now - _rc_last >= _rc_interval:
                    _reconcile_last_log[_rc_key] = _rc_now
                    logger.warning(
                        f"[Reconcile] ⚡ 可赎回代币 | {outcome} {size:.2f}份 "
                        f"= ${val:.2f} | {title}"
                    )

            # 2) 死亡仓位：curPrice <= 0.001 且 DB 仍 OPEN
            # T70: 禁止自动核销（违反 SUPREME 1：未经 CLOB 实际卖出不得关闭 DB）
            # 正确行为：保持 OPEN，effective_resolution 机制会尝试卖出，
            # 多次失败后自动暂停卖出尝试（dead_position_max_sell_retries），
            # 最终由平台正式结算后关闭
            elif cur_price <= 0.001 and size >= 0.5 and cid in open_cids:
                bet = open_by_cid.get(cid)
                bid = bet["bet_id"] if bet else "unknown"
                logger.info(
                    f"[Reconcile] ☠ 死亡仓位（链上无买方）| bet={bid[:8]} "
                    f"price={cur_price:.5f} size={size:.2f}份 "
                    f"≈ ${size*cur_price:.4f} → 保持OPEN，等待平台正式结算"
                )

        if result["redeemable"]:
            # T189: CRITICAL 也节流 — 每30分钟打一次
            _action_key = "action_required"
            _action_now = time.time()
            _action_last = _reconcile_last_log.get(_action_key, 0)
            if _action_now - _action_last >= 1800:
                _reconcile_last_log[_action_key] = _action_now
                logger.critical(
                    f"[Reconcile] 🔴 ACTION REQUIRED: {len(result['redeemable'])} 个可赎回仓位"
                    f"，共 ${redeemable_total:.2f} 待兑换！"
                    f" → 请登录 https://polymarket.com → Portfolio → Redeem All"
                )

        return result

    # _try_force_close_dead_position 已删除 (T181)
    # 原因：使用标题关键词匹配核销仓位，无condition_id校验无链上验证，
    # 严重违反 SUPREME_RULE_1（仓位一致性铁律）。该方法为死代码已彻底移除。

    # ------------------------------------------------------------------
    # 持仓管理 — 加仓（Scale-In）/ 部分止盈（Partial Take-Profit）
    # ------------------------------------------------------------------

    def check_position_scaling(
        self,
        open_bets: list[dict],
        sim_balance: float,
        db_path=None,
        market_data: dict[str, dict] | None = None,
    ) -> dict:
        """
        在监控轮次中检查现有仓位：加仓（创建新独立订单）或卖出整笔（仅多笔时）。

        遵循 Polymarket 原子订单规则：
          - 加仓 = 创建一个全新的 paper_bet 记录（独立订单）
          - 减仓 = 卖出（关闭）某个完整的订单，不可拆分

        返回 {"scale_ins": int, "sold_bets": int, "balance_delta": float}
        """
        import json as _json

        from src.utils import db as _db

        _db_path = db_path if db_path is not None else _db._DB_PATH

        pm_cfg = get("position_management") or {}
        if not pm_cfg.get("enabled", False):
            return {"scale_ins": 0, "sold_bets": 0, "balance_delta": 0.0}

        si_cfg = pm_cfg.get("scale_in") or {}
        sob_cfg = pm_cfg.get("sell_one_bet") or {}

        if not open_bets:
            return {"scale_ins": 0, "sold_bets": 0, "balance_delta": 0.0}

        # 批量获取实时价格（支持外部传入缓存，避免同一周期重复请求）
        if market_data is None:
            market_ids = list({b["market_id"] for b in open_bets})
            _slug_map = {
                b["market_id"]: b.get("market_slug", "")
                for b in open_bets
                if b.get("market_slug")
            }
            market_data = self._fetch_market_statuses(market_ids, slug_map=_slug_map)

        scale_ins = 0
        sold_bets = 0
        balance_delta = 0.0

        # ── 阶段 1: 卖出整笔（sell-one-bet）──────────────────────────
        # 按 market_id 分组，仅处理 ≥2 笔订单的市场
        if sob_cfg.get("enabled", False):
            by_market: dict[str, list[dict]] = {}
            for bet in open_bets:
                by_market.setdefault(bet["market_id"], []).append(bet)

            for market_id, bets in by_market.items():
                if len(bets) < 2:
                    continue  # 只有 1 笔 → 不能减仓

                mdata = market_data.get(market_id)
                if not mdata:
                    continue

                sold = self._try_sell_one_bet(bets, mdata, sob_cfg, _db_path)
                if sold:
                    sold_bets += 1
                    balance_delta += sold  # sold = 回收金额

        # ── 阶段 2: 加仓（scale-in → 创建新订单）──────────────────────
        # 仅对 entry 类型的父订单检查加仓条件
        if si_cfg.get("enabled", False):
            _excluded_cats = set(si_cfg.get("excluded_categories", []))
            entry_bets = [
                b
                for b in open_bets
                if (b.get("bet_type") or "entry") == "entry"
                and b.get("category", "") not in _excluded_cats
            ]
            for bet in entry_bets:
                market_id = bet["market_id"]
                mdata = market_data.get(market_id)
                if not mdata:
                    continue

                outcome = (bet.get("outcome") or "").strip().upper()
                sell_price = self._extract_sell_price(mdata, outcome)
                buy_price = self._extract_buy_price(mdata, outcome)
                if not sell_price or sell_price <= 0 or not buy_price or buy_price <= 0:
                    continue

                entry_price = float(bet.get("market_price") or 0)
                if entry_price <= 0:
                    continue

                size = float(bet.get("size_usdc") or 0)
                if size <= 0:
                    continue

                shares = size / entry_price
                current_value = shares * sell_price
                return_pct = (current_value - size) / size

                add_amount = self._try_scale_in(
                    bet,
                    return_pct,
                    buy_price,
                    sell_price,
                    si_cfg,
                    sim_balance + balance_delta,
                    market_data,
                    _db_path,
                )
                if add_amount > 0:
                    scale_ins += 1
                    balance_delta -= add_amount

        if scale_ins > 0 or sold_bets > 0:
            logger.info(
                f"[PositionMgmt] 加仓={scale_ins}笔 卖出整笔={sold_bets}笔 "
                f"余额变动={balance_delta:+.2f} USDC"
            )
        else:
            # 无操作时输出摘要，让用户知道监控在工作
            entry_bets = [
                b for b in open_bets if (b.get("bet_type") or "entry") == "entry"
            ]
            if entry_bets and market_data:
                returns = []
                for b in entry_bets:
                    mid = b["market_id"]
                    md = market_data.get(mid)
                    if not md:
                        continue
                    ep = float(b.get("market_price") or 0)
                    if ep <= 0:
                        continue
                    outcome = (b.get("outcome") or "").strip().upper()
                    sp = self._extract_sell_price(md, outcome)
                    if sp and sp > 0:
                        ret = (sp - ep) / ep
                        returns.append(ret)
                if returns:
                    best = max(returns)
                    worst = min(returns)
                    threshold = float(si_cfg.get("return_threshold_pct", 0.10))
                    min_return_next = float(si_cfg.get("min_return_for_next", 0.05))
                    max_count = int(si_cfg.get("max_count", 5))
                    # 显示实际有效阈值范围（考虑已加仓次数的递增）
                    max_threshold = threshold + max_count * min_return_next
                    near_count = sum(1 for r in returns if r >= threshold * 0.7)
                    logger.info(
                        f"[PositionMgmt] 扫描 {len(entry_bets)} 个持仓 | "
                        f"最高收益={best:+.1%} 最低={worst:+.1%} | "
                        f"加仓阈值={threshold:.0%}~{max_threshold:.0%}(递增) "
                        f"接近阈值={near_count}个 → 未触发"
                    )

        return {
            "scale_ins": scale_ins,
            "sold_bets": sold_bets,
            "balance_delta": balance_delta,
        }

    def _try_scale_in(
        self,
        bet: dict,
        return_pct: float,
        buy_price: float,
        sell_price: float,
        si_cfg: dict,
        available_balance: float,
        market_data: dict,
        db_path,
    ) -> float:
        """
        检查并执行加仓 — 创建一个全新的 paper_bet 记录（独立订单）。

        条件（全部满足才加仓）：
        1. 盈利 ≥ return_threshold_pct
        2. 加仓次数 < max_count
        3. 上次加仓距今 > cooldown_minutes
        4. 同市场总仓位 < max_total_position_pct × balance
        5. 余额足够

        返回加仓金额（0 = 未加仓）。
        """
        import json as _json
        import uuid as _uuid

        from src.utils import db as _db

        threshold = float(si_cfg.get("return_threshold_pct", 0.10))
        min_return_next = float(si_cfg.get("min_return_for_next", 0.05))
        max_count = int(si_cfg.get("max_count", 5))
        cooldown_min = float(si_cfg.get("cooldown_minutes", 15))
        base_ratio = float(si_cfg.get("base_ratio", 0.60))
        growth_factor = float(si_cfg.get("growth_factor", 0.80))
        max_total_pct = float(si_cfg.get("max_total_position_pct", 0.15))

        bet_id = bet.get("bet_id", "")
        size = float(bet.get("size_usdc") or 0)
        scale_count = int(bet.get("scale_in_count") or 0)
        last_scale = float(bet.get("last_scale_at") or 0)
        entry_price = float(bet.get("market_price") or 0)

        # 条件检查
        effective_threshold = threshold + scale_count * min_return_next

        # ── 趋势加速：价格持续上涨时降低加仓门槛 ────────────────────
        # 如果当前卖出价 > 入场价（盈利中），而且已在盈利区间，给予折扣
        trend_discount = float(si_cfg.get("trend_discount", 0.30))
        if return_pct > 0 and entry_price > 0 and sell_price > entry_price:
            # 正在盈利且趋势向好 → 降低阈值30%让加仓更快触发
            effective_threshold = effective_threshold * (1.0 - trend_discount)

        if return_pct < effective_threshold:
            return 0.0
        if scale_count >= max_count:
            return 0.0
        if last_scale > 0 and (time.time() - last_scale) < cooldown_min * 60:
            return 0.0

        # 加仓价格上限：不在过高价位加仓（避免高位追涨被套）
        max_premium_pct = float(si_cfg.get("max_entry_premium_pct", 0.15))
        if entry_price > 0 and buy_price > entry_price * (1 + max_premium_pct):
            logger.info(
                f"[ScaleIn] 跳过 | 加仓价={buy_price:.3f} > "
                f"初始价{entry_price:.3f}×{1+max_premium_pct:.2f}="
                f"{entry_price*(1+max_premium_pct):.3f} | {bet.get('bet_id','')[:8]}..."
            )
            return 0.0

        # 边际EV检查：加仓价相对AI概率的EV不得低于父订单EV的阈值
        parent_ev = float(bet.get("ev") or 0)
        if parent_ev > 0 and buy_price > 0:
            _ai_prob = float(bet.get("ai_probability") or 0.5)
            _marginal_ev = _ai_prob - buy_price
            _min_ratio = float(si_cfg.get("min_marginal_ev_ratio", 0.50))
            if _marginal_ev < parent_ev * _min_ratio:
                logger.info(
                    f"[ScaleIn] 边际EV不足 | 父EV={parent_ev:.3f} "
                    f"边际EV={_marginal_ev:.3f} < "
                    f"{parent_ev * _min_ratio:.3f} | {bet_id[:8]}..."
                )
                return 0.0

        # 计算同市场总仓位
        market_id = bet["market_id"]
        market_bets = _db.get_open_bets_for_market(market_id, db_path)
        total_market_size = sum(float(b.get("size_usdc") or 0) for b in market_bets)

        # 动态总仓位上限 = balance × max_total_position_pct
        pt_cfg = get("paper_trading") or {}
        initial_bal = float(pt_cfg.get("initial_balance", 1000.0))
        effective_balance = max(available_balance, initial_bal)
        max_total = effective_balance * max_total_pct

        if total_market_size >= max_total:
            return 0.0

        # 递减比例: base_ratio × growth_factor^scale_count
        ratio = base_ratio * (growth_factor**scale_count)
        add_amount = round(size * ratio, 2)

        # 不超过总仓位上限
        if total_market_size + add_amount > max_total:
            add_amount = round(max_total - total_market_size, 2)

        # 最小加仓额度
        _scale_in_min = float(
            ((get("position_management") or {}).get("scale_in") or {}).get(
                "min_amount_usdc", 0.50
            )
        )
        if add_amount < _scale_in_min:
            return 0.0

        # 余额检查（T190: 简化为绝对底线）
        min_reserve = float(pt_cfg.get("balance_reserve_min_usdc", 0.10))
        if available_balance - add_amount < min_reserve:
            return 0.0

        # ── 执行加仓：创建新的独立订单 ────────────────────────────────
        now = time.time()
        new_bet_id = str(_uuid.uuid4())

        new_bet = {
            "bet_id": new_bet_id,
            "ts_entered": now,
            "market_id": market_id,
            "question": bet.get("question", ""),
            "category": bet.get("category", "weather"),
            "outcome": bet.get("outcome", "YES"),
            "confidence": bet.get("confidence", 0.0),
            "ai_probability": bet.get("ai_probability", 0.0),
            "market_price": buy_price,
            "ev": bet.get("ev", 0.0),
            "size_usdc": add_amount,
            "sim_balance_before": available_balance,
            "sim_balance_after": round(available_balance - add_amount, 4),
            "market_end_date": bet.get("market_end_date", ""),
            "weather_context": bet.get("weather_context", ""),
            "ai_models": bet.get("ai_models", ""),
            "cycle_id": bet.get("cycle_id", 0),
            "bet_bucket": bet.get("bet_bucket", "standard"),
            "market_slug": bet.get("market_slug", ""),
            "event_slug": bet.get("event_slug", ""),
            "bet_type": "scale_in",
            "parent_bet_id": bet_id,
        }
        _db.insert_paper_bet(new_bet, db_path)

        # 更新父订单追踪字段
        history_raw = bet.get("position_history") or "[]"
        try:
            history = _json.loads(history_raw)
        except (ValueError, TypeError):
            history = []
        history.append(
            {
                "action": "scale_in",
                "ts": now,
                "child_bet_id": new_bet_id,
                "size": add_amount,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "return_pct": round(return_pct, 4),
            }
        )

        _db.update_paper_bet_scale_in(
            bet_id=bet_id,
            scale_in_count=scale_count + 1,
            last_scale_at=now,
            position_history=_json.dumps(history),
            db_path=db_path,
        )

        logger.info(
            f"[ScaleIn] ✅ 加仓 #{scale_count + 1} | parent={bet_id[:8]}... "
            f"| 新订单={new_bet_id[:8]}... +${add_amount:.2f} "
            f"| 买入价={buy_price:.3f} 卖出价={sell_price:.3f} "
            f"| 父仓收益={return_pct:+.1%}"
        )
        return add_amount

    def _try_sell_one_bet(
        self,
        bets: list[dict],
        mdata: dict,
        sob_cfg: dict,
        db_path,
    ) -> float:
        """
        检查并卖出一个完整订单（Polymarket 原子订单规则）。

        策略：
        1. 优先锁定赢家：收益 ≥ sell_winner_return_pct → 卖出盈利最高的订单
        2. 其次止损输家：亏损 ≥ sell_loser_loss_pct → 卖出亏损最重的订单
        3. 每市场每轮最多卖 1 笔

        返回卖出回收金额（0 = 未卖出）。
        """
        from src.utils import db as _db

        winner_threshold = float(sob_cfg.get("sell_winner_return_pct", 0.50))
        loser_threshold = float(sob_cfg.get("sell_loser_loss_pct", 0.20))
        # 加仓冷却保护：刚加仓的订单在冷却期内不被 sell_one_bet 卖出
        scale_in_cooldown = float(sob_cfg.get("scale_in_cooldown_minutes", 30)) * 60
        now = time.time()

        # 计算每笔订单的当前收益
        scored: list[tuple[dict, float, float]] = []  # (bet, return_pct, sell_price)
        for bet in bets:
            outcome = (bet.get("outcome") or "").strip().upper()
            sell_price = self._extract_sell_price(mdata, outcome)
            if sell_price is None or sell_price <= 0:
                continue

            entry_price = float(bet.get("market_price") or 0)
            if entry_price <= 0:
                continue

            size = float(bet.get("size_usdc") or 0)
            if size <= 0:
                continue

            shares = size / entry_price
            current_value = shares * sell_price
            return_pct = (current_value - size) / size

            # 冷却保护：scale_in 订单在冷却期内不参与卖出（防止刚加仓就被割肉）
            ts_entered = float(bet.get("ts_entered") or 0)
            if (
                bet.get("bet_type") == "scale_in"
                and ts_entered > 0
                and (now - ts_entered) < scale_in_cooldown
            ):
                continue

            scored.append((bet, return_pct, sell_price))

        if not scored:
            return 0.0

        # 优先卖出盈利最高的（锁定利润）
        best = max(scored, key=lambda x: x[1])
        if best[1] >= winner_threshold:
            return self._execute_sell_bet(
                best[0], best[1], best[2], "sell_winner", db_path
            )

        # 其次卖出亏损最重的（止损）
        worst = min(scored, key=lambda x: x[1])
        if worst[1] <= -loser_threshold:
            return self._execute_sell_bet(
                worst[0], worst[1], worst[2], "sell_loser", db_path
            )

        return 0.0

    def _execute_sell_bet(
        self, bet: dict, return_pct: float, sell_price: float, trigger: str, db_path
    ) -> float:
        """卖出（关闭）一个完整的订单，返回回收金额。"""
        from src.utils import db as _db

        size = float(bet.get("size_usdc") or 0)
        entry_price = float(bet.get("market_price") or 0.5)
        shares = size / entry_price
        proceeds = round(shares * sell_price, 4)
        # T189-G: 扣除买入手续费
        _esb_entry_fee = float(bet.get("entry_fee_usdc") or 0)
        pnl = round(proceeds - size - _esb_entry_fee, 4)
        bet_id = bet.get("bet_id", "")

        if pnl >= 0:
            _db.close_paper_bet_profit_take(
                bet_id=bet_id,
                exit_price=sell_price,
                pnl=pnl,
                payout=proceeds,
                db_path=db_path,
            )
        else:
            _db.close_paper_bet_stop_loss(
                bet_id=bet_id,
                exit_price=sell_price,
                pnl=pnl,
                payout=proceeds,
                db_path=db_path,
            )

        # T189-F: 记录卖出手续费
        # T228追加: 退出费率使用 sell_mode 配置（而非入场 order_type）
        _category = bet.get("category", "weather")
        _order_type = _resolve_exit_order_type(
            _category, bet.get("order_type", "taker")
        )
        _exit_fee, _ = _db.calc_pm_fee(shares, sell_price, _category, _order_type)
        if _exit_fee > 0:
            _db.update_bet_fees(bet_id, exit_fee_usdc=_exit_fee, db_path=db_path)
            # T189-G: 卖出手续费也要反映到pnl中
            pnl = round(pnl - _exit_fee, 4)
            with _db.connect(db_path) as _conn:
                _conn.execute(
                    "UPDATE paper_bets SET pnl=? WHERE bet_id=?", (pnl, bet_id)
                )

        bet_type = bet.get("bet_type", "entry")
        logger.info(
            f"[SellOneBet] ✅ {trigger} | bet={bet_id[:8]}... ({bet_type}) "
            f"| 入场={entry_price:.3f} 卖出={sell_price:.3f} "
            f"| P&L={pnl:+.4f} USDC ({return_pct:+.1%})"
        )
        return proceeds

    @staticmethod
    def _extract_buy_price(market_data: dict, outcome: str) -> float | None:
        """
        提取买入时的实际成交价（CLOB Best Ask）。

        买入 YES → 按 bestAsk 成交
        买入 NO  → 按 1 - bestBid 成交

        用于加仓时记录真实买入成本。
        """
        outcome_upper = outcome.upper()
        try:
            if outcome_upper == "YES":
                ask = market_data.get("bestAsk")
                if ask is not None:
                    return float(ask)
            elif outcome_upper == "NO":
                bid = market_data.get("bestBid")
                if bid is not None:
                    return round(1.0 - float(bid), 6)
        except (ValueError, TypeError):
            pass
        return OutcomeTracker._extract_current_price(market_data, outcome)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _is_market_expired(status: dict) -> bool:
        """市场 endDate 是否已过当前 UTC 时间。"""
        end_date_str = status.get("endDate", "")
        if not end_date_str:
            return False
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            return end_dt < datetime.now(timezone.utc)
        except Exception:
            return False

    @staticmethod
    def _fetch_actual_max_temp(lat: float, lon: float, date_str: str) -> float | None:
        """
        使用 Open-Meteo API 获取指定城市指定日期的实际最高气温（°C）。

        注意: 这是旧版兜底方法。T278 新增了 _fetch_actual_max_temp_by_source
        按 settlement_source 路由。_self_resolve_weather_market 已切换到新方法。
        """
        try:
            r = requests.get(
                OPEN_METEO_API,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max",
                    "timezone": "UTC",
                    "start_date": date_str,
                    "end_date": date_str,
                },
                timeout=_REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            temps = r.json().get("daily", {}).get("temperature_2m_max", [])
            return float(temps[0]) if temps else None
        except Exception as e:
            logger.warning(
                f"[OutcomeTracker] Open-Meteo 获取失败 ({lat},{lon},{date_str}): {e}"
            )
            return None

    @staticmethod
    def _fetch_actual_max_temp_by_source(
        city: str, lat: float, lon: float, date_str: str
    ) -> tuple[float | None, str]:
        """
        T278: 按城市 settlement_source 获取实际最高温度。
        返回 (max_temp_c, source_name) 或 (None, "")。
        """
        try:
            from src.utils.weather_fetcher import (
                fetch_daily_observed_max_temp,
                get_city_settlement_source,
                get_station_info,
            )

            ss_info = get_city_settlement_source(city)
            settlement_source = ss_info.get("settlement_source", "wunderground")
            station = get_station_info(city)
            icao = station[0] if station else ""
            native_unit = "C"  # 结算时总是°C比较，°F在格式匹配时转换

            result = fetch_daily_observed_max_temp(
                icao=icao,
                lat=lat,
                lon=lon,
                date_str=date_str,
                city_name=city,
                native_unit=native_unit,
                settlement_source=settlement_source,
            )
            return result
        except Exception as e:
            logger.warning(
                f"[OutcomeTracker] T278 source-routed fetch 失败 {city}: {e}"
            )
            # 降级到 Open-Meteo
            temp = OutcomeTracker._fetch_actual_max_temp(lat, lon, date_str)
            return (temp, "open-meteo-fallback") if temp is not None else (None, "")

    @staticmethod
    def _self_resolve_weather_market(question: str, market_end_date: str) -> str:
        """
        对天气最高温度市场进行自解析（Polymarket 官方未解决时的兜底）。

        支持多种问题格式：
          - "be {N}°C on"                → 精确匹配（四舍五入）
          - "be {N}°C or higher on"      → 实际温度 ≥ N°C
          - "be {N}°C or lower on"       → 实际温度 ≤ N°C
          - "be between {N1}-{N2}°C on"  → N1 ≤ 实际 ≤ N2（°C）
          - "be between {N1}-{N2}°F on"  → N1 ≤ (实际转°F, 四舍五入) ≤ N2
        返回："YES" / "NO"，或 ""（无法解析）。
        """
        if not question or not market_end_date:
            return ""

        # 提取城市名
        city_m = re.match(
            r"Will the highest temperature in (.+?) be ", question, re.IGNORECASE
        )
        if not city_m:
            return ""
        city = city_m.group(1).strip()

        # 解析市场结束日期
        try:
            end_dt = datetime.fromisoformat(market_end_date.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            date_str = end_dt.strftime("%Y-%m-%d")
        except Exception:
            return ""

        # 仅处理已过去的日期
        if end_dt > datetime.now(timezone.utc):
            return ""

        coords = get_city_coords(city)
        if not coords:
            logger.debug(f"[OutcomeTracker] 城市坐标未知，无法自解析: {city}")
            return ""

        lat, lon = coords
        # T278: 使用 settlement_source 路由（NOAA/HKO/WU），不再统一用 Open-Meteo
        actual_max_c, _source = OutcomeTracker._fetch_actual_max_temp_by_source(
            city, lat, lon, date_str
        )
        if actual_max_c is None:
            return ""
        logger.info(
            f"[OutcomeTracker] 自解析数据源={_source}, {city} {date_str} "
            f"max={actual_max_c:.1f}°C"
        )

        # ── 格式 1: between X-Y°F ──────────────────────────────────────
        m = re.search(r"be between (\d+)-(\d+)°F", question, re.IGNORECASE)
        if m:
            lo_f, hi_f = int(m.group(1)), int(m.group(2))
            actual_f = round(actual_max_c * 9 / 5 + 32)
            logger.info(
                f"[OutcomeTracker] 自解析天气(°F范围) | {city} {date_str} "
                f"实际={actual_max_c:.1f}°C={actual_f}°F, 范围={lo_f}-{hi_f}°F"
            )
            return "YES" if lo_f <= actual_f <= hi_f else "NO"

        # ── 格式 2: between X-Y°C ──────────────────────────────────────
        m = re.search(r"be between (\d+)-(\d+)°C", question, re.IGNORECASE)
        if m:
            lo_c, hi_c = int(m.group(1)), int(m.group(2))
            logger.info(
                f"[OutcomeTracker] 自解析天气(°C范围) | {city} {date_str} "
                f"实际={actual_max_c:.1f}°C, 范围={lo_c}-{hi_c}°C"
            )
            return "YES" if lo_c <= round(actual_max_c) <= hi_c else "NO"

        # ── 格式 3: X°C or higher ──────────────────────────────────────
        m = re.search(r"be (\d+)°C or higher", question, re.IGNORECASE)
        if m:
            threshold = int(m.group(1))
            logger.info(
                f"[OutcomeTracker] 自解析天气(≥°C) | {city} {date_str} "
                f"实际={actual_max_c:.1f}°C, 阈值≥{threshold}°C"
            )
            return "YES" if round(actual_max_c) >= threshold else "NO"

        # ── 格式 4: X°C or lower ──────────────────────────────────────
        m = re.search(r"be (\d+)°C or lower", question, re.IGNORECASE)
        if m:
            threshold = int(m.group(1))
            logger.info(
                f"[OutcomeTracker] 自解析天气(≤°C) | {city} {date_str} "
                f"实际={actual_max_c:.1f}°C, 阈值≤{threshold}°C"
            )
            return "YES" if round(actual_max_c) <= threshold else "NO"

        # ── 格式 5: 精确 X°C ──────────────────────────────────────────
        m = re.search(r"be (\d+)°C", question, re.IGNORECASE)
        if m:
            target_temp = int(m.group(1))
            resolved_temp = round(actual_max_c)
            logger.info(
                f"[OutcomeTracker] 自解析天气 | {city} {date_str} "
                f"实际最高={actual_max_c:.1f}°C → 四舍五入={resolved_temp}°C, 目标={target_temp}°C"
            )
            return "YES" if resolved_temp == target_temp else "NO"

        logger.debug(f"[OutcomeTracker] 无法匹配天气问题格式: {question[:80]}")
        return ""

    def _fetch_market_statuses(
        self,
        market_ids: list[str],
        slug_map: dict[str, str] | None = None,
    ) -> dict[str, dict]:
        """
        并发请求 Gamma API，获取市场状态。

        使用 ThreadPoolExecutor 并发拉取，显著缩短监控延迟。
        对 0x 开头的 condition_id 优先使用 ?slug= 查询（精确唯一匹配），
        回退到 ?condition_id=。数字 ID 仍使用路径参数。

        Args:
            market_ids: 市场 ID 列表（可能是 0x condition_id 或数字 ID）
            slug_map: 可选的 {market_id: slug} 映射，用于 0x ID 的精确查询
        """
        result: dict[str, dict] = {}
        if not market_ids:
            return result
        _slug_map = slug_map or {}

        def _fetch_one(market_id: str) -> tuple[str, dict | None]:
            try:
                if market_id.startswith("0x"):
                    slug = _slug_map.get(market_id, "")
                    if slug:
                        resp = self.session.get(
                            f"{GAMMA_API}/markets",
                            params={"slug": slug},
                            timeout=_REQUEST_TIMEOUT,
                        )
                    else:
                        resp = self.session.get(
                            f"{GAMMA_API}/markets",
                            params={"condition_id": market_id},
                            timeout=_REQUEST_TIMEOUT,
                        )
                else:
                    resp = self.session.get(
                        f"{GAMMA_API}/markets/{market_id}",
                        timeout=_REQUEST_TIMEOUT,
                    )
                resp.raise_for_status()
                m = resp.json()
                if isinstance(m, list):
                    m = m[0] if m else None
                if isinstance(m, dict):
                    return (market_id, m)
            except Exception as e:
                logger.warning(f"[OutcomeTracker] 查询失败 market_id={market_id}: {e}")
            return (market_id, None)

        _max_workers = int(get("general.api_max_workers") or 8)
        with ThreadPoolExecutor(max_workers=_max_workers) as executor:
            futures = {executor.submit(_fetch_one, mid): mid for mid in market_ids}
            for future in as_completed(futures):
                mid, data = future.result()
                if data is not None:
                    result[mid] = data
        return result

    def _log_sports_settlement_verification(
        self, bet: dict, resolved_outcome: str
    ) -> None:
        """
        体育结算后调用 OddsPapi 独立验证 + CLV 边际记录。

        仅记录日志用于长期系统评估，不影响结算结果。
        """
        bet_category = (bet.get("category") or "").lower()
        if bet_category != "sports":
            return

        try:
            from src.utils.odds_fetcher import fetch_settlement_verification

            question = bet.get("question", "")
            if not question:
                return

            verification = fetch_settlement_verification(question)
            if not verification:
                return

            fixture_id = verification.get("fixture_id", "")

            # 1. 独立结算验证
            settlement = verification.get("settlement")
            if settlement:
                settlements = settlement.get("settlements") or []
                if settlements:
                    statuses = [s.get("status", "") for s in settlements[:3]]
                    logger.info(
                        f"[OutcomeTracker] OddsPapi 结算验证 "
                        f"{fixture_id[:20]}: {statuses} "
                        f"(Poly结果={resolved_outcome})"
                    )

            # 2. CLV 边际记录
            clv_edge = verification.get("clv_edge")
            if clv_edge:
                avg_move = clv_edge.get("avg_line_movement_pct", 0)
                bm = clv_edge.get("bookmaker", "?")
                entry_price = float(bet.get("market_price") or 0)
                logger.info(
                    f"[OutcomeTracker] CLV 边际 {fixture_id[:20]}: "
                    f"{bm} 平均线移={avg_move:+.1f}% "
                    f"(我方入场={entry_price:.3f})"
                )
        except Exception as e:
            logger.debug(f"[OutcomeTracker] 体育结算验证失败(非致命): {e}")

    @staticmethod
    def _parse_outcome(market_status: dict) -> str:
        """
        从 Gamma API 的市场数据中解析解决结果。
        - 二元市场：outcome = "Yes" / "No" → 归一化为 "YES" / "NO"
        - 命名结果市场：outcome = "Lightning" → 归一化为 "LIGHTNING"
        """
        outcome = market_status.get("outcome", "")
        if isinstance(outcome, str):
            normalized = outcome.strip().upper()
            if normalized in ("YES", "TRUE", "1"):
                return "YES"
            if normalized in ("NO", "FALSE", "0"):
                return "NO"
            if normalized:
                return normalized
        return ""

    @staticmethod
    def _validate_self_resolution(resolved_outcome: str, status: dict) -> bool:
        """
        交叉验证自解析结果与 outcomePrices 是否方向一致。

        Phase43 防御手段：自解析（天气/价格推断）可能基于不准确的数据
        得出错误结论（如 London 15°C 事故）。此方法要求 outcomePrices 中
        对应胜出 outcome 的价格至少 ≥ 0.80，否则拒绝自解析。

        返回 True 表示验证通过，False 表示方向不一致（应跳过解析）。
        """
        prices_raw = status.get("outcomePrices", "")
        outcomes_raw = status.get("outcomes", "")

        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, TypeError):
                return True  # 无法验证，放行
        elif isinstance(prices_raw, list):
            prices = prices_raw
        else:
            return True

        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except (json.JSONDecodeError, TypeError):
                outcomes = None
        elif isinstance(outcomes_raw, list):
            outcomes = outcomes_raw
        else:
            outcomes = None

        if not prices or len(prices) < 2:
            return True  # 无法验证

        # 找到自解析结果对应的价格
        resolved_price = None
        if outcomes:
            for i, name in enumerate(outcomes):
                if str(name).strip().upper() == resolved_outcome and i < len(prices):
                    try:
                        resolved_price = float(prices[i])
                    except (ValueError, TypeError):
                        pass
                    break

        if resolved_price is None:
            # 二元市场 fallback: YES=[0], NO=[1]
            if resolved_outcome == "YES" and len(prices) >= 1:
                try:
                    resolved_price = float(prices[0])
                except (ValueError, TypeError):
                    return True
            elif resolved_outcome == "NO" and len(prices) >= 2:
                try:
                    resolved_price = float(prices[1])
                except (ValueError, TypeError):
                    return True
            else:
                return True

        # 阈值：自解析结果的 outcomePrices 至少 ≥ 0.95
        # Phase48: 从 0.80 提高到 0.95，防止市场 closed 但未正式 resolved 时
        # 过早自解析（Buenos Aires 28°C / Seattle 52-53°F 误关事故）
        _CROSS_VALIDATION_THRESHOLD = 0.95
        return resolved_price >= _CROSS_VALIDATION_THRESHOLD

    @staticmethod
    def _resolve_by_outcome_prices(status: dict) -> str:
        """
        通过 outcomePrices 推断市场结果（当官方 resolved=False 但市场已关闭/过期时）。

        Polymarket 规则：outcomePrices 为 JSON 字符串列表 ["1","0"] 表示第一个 outcome 胜出。
        当某个价格 >= 0.95 时，认为该 outcome 已实质性胜出。

        返回：胜出的 outcome 名称（大写），或 ""（无法判断）。
        """
        prices_raw = status.get("outcomePrices", "")
        outcomes_raw = status.get("outcomes", "")

        # 解析 prices（可能是 JSON 字符串或已解析列表）
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, TypeError):
                return ""
        elif isinstance(prices_raw, list):
            prices = prices_raw
        else:
            return ""

        # 解析 outcomes（可能是 JSON 字符串或已解析列表）
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except (json.JSONDecodeError, TypeError):
                outcomes = None
        elif isinstance(outcomes_raw, list):
            outcomes = outcomes_raw
        else:
            outcomes = None

        if not prices:
            return ""

        # 查找价格 >= 0.95 的胜出 outcome
        winner_idx = -1
        for i, p in enumerate(prices):
            try:
                if float(p) >= 0.95:
                    winner_idx = i
                    break
            except (ValueError, TypeError):
                continue

        if winner_idx < 0:
            return ""

        # 有 outcomes 名称列表 → 返回具体名称
        if outcomes and winner_idx < len(outcomes):
            name = str(outcomes[winner_idx]).strip().upper()
            if name:
                return name

        # 无 outcomes 名称 → 二元市场默认 YES/NO
        if len(prices) == 2:
            return "YES" if winner_idx == 0 else "NO"

        return ""

    @staticmethod
    def _parse_timestamp(ts_str: Any) -> float:
        """将 ISO 时间字符串或 UNIX 时间戳转为 float（秒）。"""
        if not ts_str:
            return time.time()
        if isinstance(ts_str, (int, float)):
            return float(ts_str)
        try:
            from datetime import datetime, timezone

            dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return time.time()

    @staticmethod
    def _calc_pnl(bet: dict, resolved_outcome: str) -> tuple[float, float, int]:
        """
        计算模拟投注盈亏（市场自然结算）。

        Polymarket 结算费率（2024 年起）：0%
          → 每张 YES 代币在事件发生时结算 $1.00（无手续费扣除）
          → 每张 NO  代币在事件未发生时结算 $1.00

        规则（二元预测市场）：
          - 预测正确：payout = (size / entry_price) × $1.00 = size / entry_price
            「entry_price = best_ask，shares = size / best_ask，每股值 $1.00」
          - 预测错误：payout = $0.00，pnl = -size（全额损失）

        T189-G: pnl = payout - size_usdc - entry_fee - exit_fee（扣除PM手续费）
        返回：(pnl, payout, is_win)
        """
        size = float(bet.get("size_usdc", 0.0))
        price = float(bet.get("market_price") or 0.5)
        predicted = (bet.get("outcome") or "").strip().upper()
        _entry_fee = float(bet.get("entry_fee_usdc") or 0)
        _exit_fee = float(bet.get("exit_fee_usdc") or 0)

        # Polymarket O/U 及命名结果市场的 outcome 等价映射
        _YES_EQUIVALENTS = frozenset({"YES", "OVER", "TRUE", "1"})
        _NO_EQUIVALENTS = frozenset({"NO", "UNDER", "FALSE", "0"})

        is_match = (
            (predicted == resolved_outcome)
            or (predicted in _YES_EQUIVALENTS and resolved_outcome in _YES_EQUIVALENTS)
            or (predicted in _NO_EQUIVALENTS and resolved_outcome in _NO_EQUIVALENTS)
        )

        if is_match:
            # 赢：赔率 = 1 / price（如买 0.6 的 YES，赢得 1/0.6 = 1.667 倍）
            payout = round(size / max(price, 0.01), 4)
            # T189-G: 结算无卖出手续费（PM结算免费），但需扣除买入手续费
            pnl = round(payout - size - _entry_fee, 4)
            return pnl, payout, 1
        else:
            # T189-G: 输了也要扣买入手续费
            pnl = round(-size - _entry_fee, 4)
            return pnl, 0.0, 0

    # ------------------------------------------------------------------
    # 流动性真空辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_fair_ref(market_data: dict, outcome: str) -> float | None:
        """
        计算公允参考价（mid-price）：(bestBid + bestAsk) / 2。
        用于判断市场是否健康（流动性真空检测）。

        对 YES 仓位：fair = (bestBid + bestAsk) / 2
        对 NO  仓位：fair = 1 - (bestBid + bestAsk) / 2
        """
        try:
            bid = market_data.get("bestBid")
            ask = market_data.get("bestAsk")
            if bid is None or ask is None:
                return None
            bid_f = float(bid)
            ask_f = float(ask)
            if bid_f <= 0 or ask_f <= 0:
                return None
            mid = (bid_f + ask_f) / 2.0
            if outcome.upper() == "NO":
                return round(1.0 - mid, 6)
            return round(mid, 6)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _compute_spread(market_data: dict) -> float | None:
        """
        计算 bid-ask 价差比例：(ask - bid) / mid。
        价差过大（>15%）表示流动性真空，bestBid 不可信。
        """
        try:
            bid = float(market_data.get("bestBid", 0))
            ask = float(market_data.get("bestAsk", 0))
            if bid <= 0 or ask <= 0:
                return None
            mid = (bid + ask) / 2.0
            if mid <= 0:
                return None
            return (ask - bid) / mid
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _get_reference_price(market_data: dict) -> float | None:
        """
        提取公允参考价（YES 方向），用于校验 bestBid/bestAsk 偏差。
        优先 lastTradePrice → tokens[].price → outcomePrices[0]。
        """
        # lastTradePrice — 最近成交价，最可靠
        ltp = market_data.get("lastTradePrice")
        if ltp:
            try:
                v = float(ltp)
                if 0 < v < 1:
                    return v
            except (ValueError, TypeError):
                pass
        # tokens[].price — YES token 最近成交
        for tok in market_data.get("tokens") or []:
            if (tok.get("outcome") or "").upper() == "YES":
                p = tok.get("price")
                if p is not None:
                    try:
                        v = float(p)
                        if 0 < v < 1:
                            return v
                    except (ValueError, TypeError):
                        pass
        # outcomePrices[0]
        try:
            op = market_data.get("outcomePrices", "")
            prices = json.loads(op) if isinstance(op, str) and op else op
            if isinstance(prices, list) and len(prices) >= 2:
                v = float(prices[0])
                if 0 < v < 1:
                    return v
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return None

    @staticmethod
    def _validate_orderbook_price(
        raw_price: float, ref_price: float | None, label: str, market_id: str = ""
    ) -> float:
        """
        校验 bestBid/bestAsk 是否偏离参考价过大，若偏离则用参考价替代。
        防止 stale orderbook 导致虚假止盈/止损。

        智能参考价刷新（Phase 53）：
        - 阈值从 config 读取（signal.price_deviation_soft_threshold）
        - 若偏离 > auto_update_threshold（40%）且参考价过旧，自动刷新参考价
        - 参考价缓存有 max_age（默认60秒）过期保护
        """
        if ref_price is None or ref_price <= 0 or raw_price <= 0:
            return raw_price

        _sig_cfg = get("signal") or {}
        _threshold = float(_sig_cfg.get("price_deviation_soft_threshold", 0.8))
        _auto_update_pct = float(
            _sig_cfg.get("price_deviation_ref_auto_update_threshold", 0.4)
        )
        _max_age = float(_sig_cfg.get("price_deviation_ref_max_age_seconds", 60))

        # 检查缓存中的参考价是否过旧，若过旧则信任 orderbook
        _now = time.monotonic()
        if market_id:
            cached = _ref_price_cache.get(market_id)
            if cached is not None:
                _cached_ref, _cached_ts = cached
                if _now - _cached_ts > _max_age:
                    # 参考价已过期，更新为当前 orderbook 价格
                    _ref_price_cache[market_id] = (raw_price, _now)
                    return raw_price

        deviation = abs(raw_price - ref_price) / max(ref_price, 0.01)

        if deviation > _auto_update_pct and market_id:
            # 偏离超过 auto_update 阈值 → 参考价可能已过时，自动刷新
            cached = _ref_price_cache.get(market_id)
            if cached is not None:
                _cached_ref, _cached_ts = cached
                age = _now - _cached_ts
                if age > _max_age:
                    # 参考价过旧，信任 orderbook 并刷新缓存
                    _ref_price_cache[market_id] = (raw_price, _now)
                    _key = f"{market_id}:{label}"
                    _last = _price_deviation_seen.get(_key, 0.0)
                    if _now - _last >= _PRICE_DEVIATION_COOLDOWN:
                        _price_deviation_seen[_key] = _now
                        logger.info(
                            f"[PriceDeviation] {label} {market_id}: "
                            f"ref过期({age:.0f}s>{_max_age:.0f}s), "
                            f"自动刷新 ref={raw_price:.4f}"
                        )
                    return raw_price
            else:
                # 首次见到该市场，记录参考价
                _ref_price_cache[market_id] = (ref_price, _now)

        if deviation > _threshold:
            _key = f"{market_id}:{label}"
            _last = _price_deviation_seen.get(_key, 0.0)
            if _now - _last >= _PRICE_DEVIATION_COOLDOWN:
                _price_deviation_seen[_key] = _now
                logger.warning(
                    f"[PriceDeviation] {label} {market_id}: "
                    f"orderbook={raw_price:.4f} vs ref={ref_price:.4f} "
                    f"(deviation={deviation:+.0%}), 使用参考价替代"
                )
            return ref_price
        return raw_price

    @staticmethod
    def _extract_sell_price(
        market_data: dict, outcome: str, *, validate: bool = True
    ) -> float | None:
        """
        提取主动卖出时的实际成交价（CLOB Best Bid）。

        Polymarket 费率结构（2024 年起）：
          - Maker 费：0%   Taker 费：0%   结算费：0%
          - 唯一隐性成本 = Bid-Ask 价差（买 ask，卖 bid）

        本方法用于主动止盈/止损卖出 P&L 计算：
          YES token 卖出 → 按 market.bestBid 成交（买方最高出价）
          NO  token 卖出 → 按 1 - market.bestAsk 成交（对应 NO 的买方价）

        包含价格偏差保护：校验 bestBid/bestAsk 与 lastTradePrice 偏差，
        防止 stale orderbook 导致虚假止盈/止损。

        Args:
            validate: True=校验偏差并替换异常价格（止盈/展示用），
                      False=使用原始盘口价（market模式止损用）。
        """
        outcome_upper = outcome.upper()
        ref_yes = OutcomeTracker._get_reference_price(market_data) if validate else None
        market_id = str(market_data.get("id") or market_data.get("conditionId", ""))[
            :20
        ]
        try:
            if outcome_upper == "YES":
                bid = market_data.get("bestBid")
                if bid is not None:
                    raw_bid = float(bid)
                    if validate:
                        return OutcomeTracker._validate_orderbook_price(
                            raw_bid, ref_yes, "bestBid", market_id
                        )
                    return raw_bid
            elif outcome_upper == "NO":
                ask = market_data.get("bestAsk")
                if ask is not None:
                    raw_ask = float(ask)
                    if validate:
                        validated_ask = OutcomeTracker._validate_orderbook_price(
                            raw_ask, ref_yes, "bestAsk", market_id
                        )
                        return round(1.0 - validated_ask, 6)
                    return round(1.0 - raw_ask, 6)
        except (ValueError, TypeError):
            pass
        # 回退：bestBid/bestAsk 不可用时使用 mid-price
        return OutcomeTracker._extract_current_price(market_data, outcome)

    @staticmethod
    def _extract_current_price(market_data: dict, outcome: str) -> float | None:
        """
        从 Gamma API market 对象中提取指定方向（YES/NO）的 mid-price。
        优先从 tokens 数组读取（last-trade price），回退到 outcomePrices。
        用于一般价格展示；主动卖出 P&L 请使用 _extract_sell_price()。
        """
        outcome_lower = outcome.lower()  # "yes" 或 "no"

        # 方式1: tokens 列表
        tokens = market_data.get("tokens") or []
        for token in tokens:
            token_outcome = (token.get("outcome") or "").lower()
            if token_outcome == outcome_lower:
                price = token.get("price")
                if price is not None:
                    try:
                        return float(price)
                    except (ValueError, TypeError):
                        pass

        # 方式2: outcomePrices + outcomes 数组
        outcomes_list = market_data.get("outcomes") or []
        outcome_prices = market_data.get("outcomePrices") or []
        for i, o in enumerate(outcomes_list):
            if str(o).lower() == outcome_lower and i < len(outcome_prices):
                try:
                    return float(outcome_prices[i])
                except (ValueError, TypeError):
                    pass

        # 方式3: 直接字段（bestBid/bestAsk 近似）
        # 对于 YES 仓位，卖出价接近 bestBid
        if outcome_lower == "yes":
            best_bid = market_data.get("bestBid")
            if best_bid:
                try:
                    return float(best_bid)
                except (ValueError, TypeError):
                    pass
        else:
            # NO 仓位的价格 ≈ 1 - YES bestAsk
            best_ask = market_data.get("bestAsk")
            if best_ask:
                try:
                    return round(1.0 - float(best_ask), 4)
                except (ValueError, TypeError):
                    pass

        return None
