"""
order_executor.py — Order Executor 模块
职责：余额检查 → VWAP 滑点预估 → 试探下单 → 滑点确认 → 正式下单 → 记录 tx
"""

import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from src.signal.signal_engine import Signal
from src.utils.category_detector import detect_category
from src.utils.config_loader import get, is_dry_run

logger = logging.getLogger(__name__)

# T80: 卖出失败日志降频 — 同一 (market_id, outcome) below_min_order_size 每 N秒只打印一次
_sell_min_warn_seen: dict[str, float] = {}


def _get_sell_min_warn_interval() -> float:
    """T101: 降频间隔从 config 读取（支持热加载）。"""
    return float(get("executor.sell_min_warn_interval_sec") or 300)


@dataclass
class OrderResult:
    signal: Signal
    success: bool
    tx_hash: str = ""
    actual_price: float = 0.0
    filled_size: float = 0.0
    error: str = ""
    condition_id: str = ""  # CLOB condition_id，下单时解析
    remaining_shares: float = 0.0  # 卖出后链上残余 shares（>0 表示部分卖出）
    matched_shares: float = 0.0  # CLOB 实际成交股数（平台数据源）
    order_type: str = ""  # 实际下单方式: maker / taker / simulated


class OrderExecutor:
    """
    安全下单执行器。
    - dry_run=True 时只记录日志，不真实下单（默认启用，受 config.json 控制）
    - probe_enabled=True 时先下 1 USDC 试探单，确认价格后再下剩余仓位
    """

    def __init__(self) -> None:
        self._client = None  # 延迟初始化 py-clob-client

    # ------------------------------------------------------------------
    # VWAP 滑点预估（轻量级，下单前一次性计算）
    # ------------------------------------------------------------------

    @staticmethod
    def estimate_vwap(book: dict, size_usdc: float, side: str = "buy") -> dict:
        """
        根据当前订单簿深度预估实际成交均价。

        Args:
            book: fetch_orderbook() 返回的订单簿 dict（含 bids/asks）
            size_usdc: 期望下单金额（USDC）
            side: "buy"（吃 asks）或 "sell"（吃 bids）

        Returns:
            {
                "vwap": float | None,       # 预估成交均价
                "best_price": float | None,  # 最优价（bestAsk 或 bestBid）
                "deviation": float | None,   # vwap 与 best_price 的偏离比例
                "fillable_usdc": float,      # 订单簿可承接的最大金额
                "levels_consumed": int,       # 需要吃掉的档位数
            }
        """
        if not book or not isinstance(book, dict):
            return {
                "vwap": None,
                "best_price": None,
                "deviation": None,
                "fillable_usdc": 0.0,
                "levels_consumed": 0,
            }

        levels = book.get("asks", []) if side == "buy" else book.get("bids", [])
        if not levels:
            return {
                "vwap": None,
                "best_price": None,
                "deviation": None,
                "fillable_usdc": 0.0,
                "levels_consumed": 0,
            }

        # 按价格排序：buy → asks 升序；sell → bids 降序
        try:
            parsed = [
                (float(l["price"]), float(l["size"]))
                for l in levels
                if l.get("price") and l.get("size")
            ]
        except (ValueError, TypeError, KeyError):
            return {
                "vwap": None,
                "best_price": None,
                "deviation": None,
                "fillable_usdc": 0.0,
                "levels_consumed": 0,
            }

        parsed.sort(key=lambda x: x[0], reverse=(side != "buy"))
        best_price = parsed[0][0] if parsed else None

        total_cost = 0.0  # 总花费 USDC
        total_qty = 0.0  # 总数量（shares）
        levels_consumed = 0
        remaining = size_usdc

        for price, qty in parsed:
            if remaining <= 0:
                break
            level_cost = price * qty  # 该档位总价值
            if level_cost <= remaining:
                total_cost += level_cost
                total_qty += qty
                remaining -= level_cost
            else:
                # 部分成交该档位
                partial_qty = remaining / price
                total_cost += remaining
                total_qty += partial_qty
                remaining = 0.0
            levels_consumed += 1

        fillable = total_cost
        vwap = (total_cost / total_qty) if total_qty > 0 else None
        deviation = None
        if vwap is not None and best_price and best_price > 0:
            deviation = abs(vwap - best_price) / best_price

        return {
            "vwap": round(vwap, 6) if vwap else None,
            "best_price": round(best_price, 6) if best_price else None,
            "deviation": round(deviation, 6) if deviation is not None else None,
            "fillable_usdc": round(fillable, 2),
            "levels_consumed": levels_consumed,
        }

    def _check_vwap(self, signal: Signal) -> Optional[str]:
        """
        下单前 VWAP 滑点预估。返回 None 表示通过，否则返回跳过/缩仓原因。
        同时可能缩减 signal.size_usdc。
        """
        if not get("executor.vwap_enabled"):
            return None

        token_id = getattr(signal, "token_id", "")
        if not token_id:
            return None  # 无 token_id 时跳过 VWAP（DRY RUN 常态）

        try:
            from src.utils.orderbook_history import fetch_orderbook

            book = fetch_orderbook(token_id)
        except Exception as e:
            logger.debug(f"[VWAP] 获取订单簿失败: {e}")
            return None  # 获取失败不阻塞下单

        if not book:
            return None

        max_dev = get("executor.vwap_max_deviation") or 0.05

        # ── 小额限价单豁免：bet_size 很小时深度检查无意义 ──────────
        small_threshold = get("executor.vwap_small_bet_threshold") or 4.0
        if signal.size_usdc <= small_threshold:
            logger.debug(
                f"[VWAP] {signal.market_id} 小额下单"
                f"({signal.size_usdc:.1f} USDC)豁免深度检查"
            )
            return None

        # ── 大额下单：基于下单金额的动态深度要求 ──────────────────
        static_floor = get("executor.vwap_min_depth_usdc") or 3.0
        depth_mult = get("executor.vwap_bet_depth_multiplier") or 0.8
        min_depth = max(static_floor, signal.size_usdc * depth_mult)

        result = self.estimate_vwap(book, signal.size_usdc, side="buy")

        if result["fillable_usdc"] < min_depth:
            return (
                f"订单簿深度不足: {result['fillable_usdc']:.1f} USDC "
                f"< 最小要求 {min_depth:.0f} USDC"
            )

        if result["deviation"] is not None and result["deviation"] > max_dev:
            # 尝试缩仓到可承受深度的 80%
            safe_size = result["fillable_usdc"] * 0.8
            min_bet = get("executor.min_bet_usdc") or 2.0
            if safe_size >= min_bet:
                old_size = signal.size_usdc
                signal.size_usdc = round(safe_size, 2)
                logger.info(
                    f"[VWAP] 缩仓 {old_size:.1f} → {signal.size_usdc:.1f} USDC "
                    f"(偏离 {result['deviation']:.1%} > {max_dev:.0%})"
                )
                return None  # 缩仓后继续
            return (
                f"VWAP 偏离过大 ({result['deviation']:.1%} > {max_dev:.0%}), "
                f"深度不足缩仓"
            )

        if result["vwap"] and result["best_price"]:
            logger.debug(
                f"[VWAP] {signal.market_id} 预估均价={result['vwap']:.4f} "
                f"bestAsk={result['best_price']:.4f} "
                f"偏离={result['deviation']:.2%} 档位={result['levels_consumed']}"
            )
        return None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def execute(self, signal: Signal) -> OrderResult:
        """执行单个信号对应的下单流程。"""
        dry_run: bool = is_dry_run()

        # VWAP 滑点预估（DRY RUN 和实盘都执行，有 token_id 时生效）
        vwap_skip = self._check_vwap(signal)
        if vwap_skip:
            logger.warning(f"[VWAP] 跳过下单 {signal.market_id}: {vwap_skip}")
            if dry_run:
                return OrderResult(signal=signal, success=False, error=vwap_skip)
            return OrderResult(signal=signal, success=False, error=vwap_skip)

        # T303: 灰度 dry-run 模式（SUPREME 1 合规：仅阻断【新开】订单，
        # 不影响任何已存在的 OPEN 仓位的卖出/止损/止盈/重试循环）
        if not dry_run:
            try:
                gray_enabled = bool(get("dashboard.gray_dry_run_enabled") or False)
            except Exception:
                gray_enabled = False
            if gray_enabled:
                logger.info(
                    f"[GRAY_RUN] 灰度模式拦截新开订单 | {signal.market_id} "
                    f"{signal.outcome} {signal.size_usdc} USDC EV={signal.ev} "
                    f"(仅阻断新单，现有仓位的卖出/SL/TP 不受影响)"
                )
                return OrderResult(signal=signal, success=True, tx_hash="GRAY_RUN")

        if dry_run:
            logger.info(
                f"[DRY RUN] 模拟下单 | {signal.market_id} "
                f"{signal.outcome} {signal.size_usdc} USDC EV={signal.ev}"
            )
            return OrderResult(signal=signal, success=True, tx_hash="DRY_RUN")

        # 实盘流程
        balance = self._check_balance()
        max_pct = get("risk.max_single_market_pct") or 0.15
        _clob_min = float(get("executor.min_bet_usdc") or 2.0)
        if signal.size_usdc > balance * max_pct:
            clipped = round(balance * max_pct, 2)
            # 小余额场景：裁剪后低于最低限额时，允许使用最低限额
            if clipped < _clob_min and balance >= _clob_min:
                signal.size_usdc = _clob_min
                logger.info(
                    f"仓位裁剪 {clipped:.2f} < CLOB最小限额，"
                    f"使用 {_clob_min} USDC（余额 {balance:.2f} 充足）"
                )
            else:
                signal.size_usdc = clipped
                logger.warning(
                    f"仓位已裁剪至 {signal.size_usdc} USDC（单市场限额保护）"
                )

        min_bet = get("executor.min_bet_usdc") or 2.0
        if signal.size_usdc < min_bet:
            # T206追加 Fix: TopUp 订单豁免 min_bet 检查 ——
            # TopUp 的成本可能低于 min_bet（如差额 1.92 shares × $0.69 = $1.32），
            # 但 _submit_order 会自动放大至 CLOB 最小份数(5 shares)，
            # 确保订单可被 CLOB 接受。不豁免会导致结构性死锁。
            if getattr(signal, "_is_topup", False):
                logger.info(
                    f"[OrderExecutor] TopUp豁免min_bet: "
                    f"${signal.size_usdc:.2f} < ${min_bet} "
                    f"但允许执行（_submit_order将放大至CLOB最小份数）"
                )
            else:
                msg = (
                    f"余额不足: 可用 {balance:.2f} USDC, "
                    f"裁剪后仓位 {signal.size_usdc:.2f} < 最小下单额 {min_bet} USDC"
                )
                logger.warning(msg)
                return OrderResult(signal=signal, success=False, error=msg)

        if get("executor.probe_enabled"):
            # Phase34: 小额订单跳过 probe，避免产生两笔平台交易
            probe_min = get("executor.probe_min_order_usdc") or 0
            if probe_min > 0 and signal.size_usdc < probe_min:
                logger.debug(
                    f"订单 {signal.size_usdc:.2f} USDC < probe_min {probe_min}，"
                    f"跳过试探单直接下单"
                )
                return self._submit_order(signal)
            probe_result = self._probe_order(signal)
            if not probe_result.success:
                return probe_result
            # 滑点检查
            if not self._slippage_ok(signal, probe_result.actual_price):
                return OrderResult(
                    signal=signal,
                    success=False,
                    error=f"滑点过大，预期价: {signal.ev + self._market_price(signal):.4f} "
                    f"实际价: {probe_result.actual_price:.4f}",
                )
            # [Critical Fix] 主单扣除 probe 已成交量，防止超配
            probe_filled = probe_result.filled_size or 0.0
            signal.size_usdc = round(signal.size_usdc - probe_filled, 2)
            if signal.size_usdc < (get("executor.min_bet_usdc") or 2.0):
                logger.info(
                    f"Probe 已填充 {probe_filled} USDC，剩余 {signal.size_usdc} USDC "
                    f"低于最小下单额，跳过主单"
                )
                return probe_result  # probe 已达目标仓位，无需主单

            main_result = self._submit_order(signal)
            # [Phase21 Fix] 合并 probe 成本到主单结果，确保 DB 记录真实总投入
            # [Phase33 Fix] 合并后重新计算加权平均入场价，修复部分成交导致的虚假均价
            if main_result.success and probe_filled > 0:
                main_filled = main_result.filled_size or 0
                total_filled = round(main_filled + probe_filled, 2)
                # 加权平均入场价 = 总成本 / 总股数
                # probe 和 main 的 actual_price 在各自 _submit_order 中已校准
                expected = getattr(signal, "expected_price", 0) or 0.5
                probe_price = probe_result.actual_price or expected
                main_price = main_result.actual_price or expected
                if total_filled > 0 and probe_price > 0 and main_price > 0:
                    probe_shares = probe_filled / probe_price
                    main_shares = main_filled / main_price
                    total_shares = probe_shares + main_shares
                    if total_shares > 0:
                        weighted_avg = total_filled / total_shares
                        main_result.actual_price = round(weighted_avg, 6)
                main_result.filled_size = total_filled
                signal.size_usdc = round(signal.size_usdc + probe_filled, 2)
                logger.info(
                    f"[Probe+Main] 合并成本: probe={probe_filled:.2f} + "
                    f"main={main_filled:.2f} = "
                    f"total={total_filled:.2f} USDC | "
                    f"加权均价={main_result.actual_price:.4f}"
                )
            return main_result

        return self._submit_order(signal)

    def execute_all(self, signals: list[Signal]) -> list[OrderResult]:
        results = []
        for sig in signals:
            result = self.execute(sig)
            results.append(result)
            if result.success:
                logger.info(f"下单成功 {sig.market_id} tx={result.tx_hash}")
            else:
                logger.error(f"下单失败 {sig.market_id} err={result.error}")
        return results

    # ------------------------------------------------------------------
    # 内部方法（实盘时实现 py-clob-client 调用）
    # ------------------------------------------------------------------

    def _get_clob_client(self):
        """
        延迟初始化 CLOB 客户端。
        - dry_run=True 时不调用此方法，无需私钥。
        - dry_run=False 时需要 POLY_PRIVATE_KEY 环境变量（用于链上签名）。
        """
        if self._client is None:
            try:
                from py_clob_client_v2.client import ClobClient
                from py_clob_client_v2.clob_types import ApiCreds

                host = get("polymarket.clob_api_url") or "https://clob.polymarket.com"
                chain_id = get("polymarket.chain_id") or 137
                private_key = os.environ.get("POLY_PRIVATE_KEY", "")
                if not private_key or private_key == "your_private_key_here":
                    raise EnvironmentError(
                        "POLY_PRIVATE_KEY 未设置。实盘下单需要 Polygon 钱包私钥用于签名。"
                        "若仍在测试阶段，请保持 config.json 中 dry_run=true。"
                    )
                creds = ApiCreds(
                    api_key=os.environ["POLY_API_KEY"],
                    api_secret=os.environ["POLY_SECRET"],
                    api_passphrase=os.environ["POLY_PASSPHRASE"],
                )
                funder = os.environ.get("POLY_RELAYER_ADDRESS", "")
                if not funder:
                    raise EnvironmentError(
                        "POLY_RELAYER_ADDRESS 未设置。Gnosis Safe 签名需要指定 funder 地址。"
                    )
                self._client = ClobClient(
                    host,
                    key=private_key,
                    chain_id=chain_id,
                    creds=creds,
                    signature_type=2,
                    funder=funder,
                )
                logger.info(
                    "CLOB 客户端已初始化 (signature_type=2, funder=%s...)", funder[:10]
                )
            except ImportError:
                logger.error(
                    "py-clob-client 未安装，请运行: pip install py-clob-client"
                )
                raise
            except KeyError as e:
                logger.error(f"缺少环境变量: {e}")
                raise
        return self._client

    def _check_balance(self) -> float:
        """
        获取 Polymarket 账户 USDC 余额。
        - dry_run 模式：从模拟账本（paper_bets DB）计算当前模拟余额，
          初始余额 - 未结算持仓成本 + 已结算盈亏 = 当前可用余额。
        - 实盘模式：通过 get_balance_allowance(signature_type=2) 查询平台内余额。
        """
        live_fallback_balance = float(get("general.live_initial_deposit") or 0.0)
        dry_run: bool = is_dry_run()
        if dry_run:
            initial = float((get("paper_trading") or {}).get("initial_balance", 100.0))
            try:
                from src.utils.db import connect

                with connect() as conn:
                    # 已结算仓位的总盈亏
                    row = conn.execute(
                        "SELECT COALESCE(SUM(pnl), 0) FROM paper_bets WHERE resolved_outcome IS NOT NULL"
                    ).fetchone()
                    realized_pnl = row[0] if row else 0.0
                    # 未结算仓位的总占用资金
                    row2 = conn.execute(
                        "SELECT COALESCE(SUM(size_usdc), 0) FROM paper_bets WHERE resolved_outcome IS NULL"
                    ).fetchone()
                    open_cost = row2[0] if row2 else 0.0
                balance = initial + realized_pnl - open_cost
                balance = max(balance, 0.0)
                logger.info(
                    f"[DRY RUN] 模拟余额: {balance:.2f} USDC (初始={initial}, 已实现PnL={realized_pnl:.2f}, 占用={open_cost:.2f})"
                )
                return round(balance, 2)
            except Exception as e:
                logger.debug(f"模拟余额计算失败，回退到初始余额: {e}")
                return initial
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

            client = self._get_clob_client()
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=2,  # Polymarket proxy wallet 余额
            )
            result = client.get_balance_allowance(params)
            raw = int(result.get("balance", "0") or "0")
            balance = raw / 1_000_000  # USDC 6 位小数
            logger.info(f"Polymarket 账户 USDC 余额: {balance:.2f} USDC")
            return balance
        except Exception as e:
            logger.error(
                f"余额查询失败: {e}，回退到配置初始余额 {live_fallback_balance}"
            )
            return live_fallback_balance

    def _probe_order(self, signal: Signal) -> "OrderResult":
        """下 probe_size_usdc 的试探单，返回成交价格。"""
        _clob_min = float(get("executor.min_bet_usdc") or 2.0)
        probe_size = float(get("executor.probe_size_usdc") or _clob_min)
        probe_sig = Signal(
            market_id=signal.market_id,
            question=signal.question,
            outcome=signal.outcome,
            ev=signal.ev,
            confidence=signal.confidence,
            size_usdc=probe_size,
        )
        probe_sig._is_probe = True  # T86: 豁免 CLOB amplification 安全阀
        # 继承关键字段：expected_price 和 token_id
        if hasattr(signal, "expected_price"):
            probe_sig.expected_price = signal.expected_price
        if hasattr(signal, "token_id"):
            probe_sig.token_id = signal.token_id
        logger.info(f"试探下单 {signal.market_id} {probe_size} USDC")
        return self._submit_order(probe_sig)

    def _get_order_mode(self, category: str, direction: str = "buy") -> str:
        """Return 'maker' or 'taker' for the given signal category and direction.

        Args:
            category: Market category (e.g. 'weather', 'crypto', 'sports')
            direction: 'buy' or 'sell'
        """
        # T63: 智能模式 — 根据 spread/depth 自动选择（仅用于买入）
        # T215追加修正: sell 方向禁用 smart_mode，始终按 per_category 配置执行。
        # 原因：smart_mode 可能将 weather=taker 的卖出覆盖为 maker，是 b0d6f4e9 潜在风险。
        if direction == "buy":
            smart_cfg = get("order_execution.smart_mode") or {}
            if smart_cfg.get("enabled"):
                return "smart"  # 由调用方判断具体选择

        # T63: 按买/卖方向独立配置
        dir_key = "buy_mode" if direction == "buy" else "sell_mode"
        dir_cfg = get(f"order_execution.{dir_key}") or {}
        per_cat = dir_cfg.get("per_category") or {}
        mode = per_cat.get(category or "") or dir_cfg.get("default")

        return "maker" if mode == "maker" else "taker"

    def _resolve_smart_mode(self, token_id: str, direction: str) -> str:
        """智能模式：根据当前 orderbook spread 和深度决定 Maker/Taker。"""
        smart_cfg = get("order_execution.smart_mode") or {}
        min_spread = float(smart_cfg.get("min_spread_for_maker") or 0.02)
        min_depth = float(smart_cfg.get("min_depth_usdc_for_maker") or 50.0)
        try:
            from src.utils.orderbook_history import analyze_liquidity, fetch_orderbook

            book = fetch_orderbook(token_id)
            liq = analyze_liquidity(book) if book else {}
            spread = float(liq.get("spread") or 0)
            bid_depth = float(liq.get("bid_depth") or 0)
            if direction == "sell":
                # 卖出时：spread 宽且 bid 深度足够 → Maker，否则 Taker
                if spread >= min_spread and bid_depth >= min_depth:
                    logger.info(
                        f"[SmartMode] SELL → Maker (spread={spread:.4f} bid_depth=${bid_depth:.1f})"
                    )
                    return "maker"
                logger.info(
                    f"[SmartMode] SELL → Taker (spread={spread:.4f} bid_depth=${bid_depth:.1f})"
                )
                return "taker"
            else:
                # 买入时：spread 宽 → Maker 有利
                if spread >= min_spread:
                    logger.info(f"[SmartMode] BUY → Maker (spread={spread:.4f})")
                    return "maker"
                logger.info(f"[SmartMode] BUY → Taker (spread={spread:.4f})")
                return "taker"
        except Exception as exc:
            logger.debug(f"[SmartMode] orderbook 查询失败，回退 Taker: {exc}")
            return "taker"

    def _submit_order(
        self, signal: Signal, _force_taker: bool = False
    ) -> "OrderResult":
        """
        向 Polymarket CLOB 提交限价单。
        需要环境变量：POLY_PRIVATE_KEY / POLY_API_KEY / POLY_SECRET / POLY_PASSPHRASE
        """
        try:
            from py_clob_client_v2.clob_types import OrderArgs  # noqa: F401
            from py_clob_client_v2.clob_types import OrderType  # noqa: F401
            from py_clob_client_v2.order_builder.constants import BUY
        except ImportError as exc:
            logger.error("py-clob-client 未安装： pip install py-clob-client")
            return OrderResult(signal=signal, success=False, error=str(exc))

        try:
            client = self._get_clob_client()

            # 刷新 CLOB 服务器的 USDC 余额缓存
            self._refresh_collateral_balance(client)

            # 1. 解析 CTF token ID
            token_id = getattr(signal, "token_id", "") or self._resolve_token_id(
                client, signal
            )

            # 1.5 token_id 验证（安全关键：空 token 会导致购买错误代币）
            if not token_id:
                err = f"token_id 为空，中止下单 [{signal.market_id}/{signal.outcome}]"
                logger.error(f"[OrderExecutor] {err}")
                return OrderResult(signal=signal, success=False, error=err)

            # 1.6 M2-T251：入场前 orderbook 深度门槛（防止在流动性枯竭市场开仓）
            _min_depth_usdc = float(get("executor.min_orderbook_depth_usdc") or 0)
            if _min_depth_usdc > 0:
                try:
                    from src.utils.orderbook_history import (
                        analyze_liquidity,
                        fetch_orderbook,
                    )

                    _depth_book = fetch_orderbook(token_id)
                    _depth_liq = analyze_liquidity(_depth_book) if _depth_book else {}
                    _ask_depth = float(_depth_liq.get("ask_depth") or 0)
                    if _ask_depth < _min_depth_usdc:
                        err = (
                            f"orderbook_depth_insufficient: "
                            f"ask_depth=${_ask_depth:.2f} < min=${_min_depth_usdc:.0f} USDC "
                            f"[{signal.market_id}]，流动性不足，拒绝入场"
                        )
                        logger.warning(f"[OrderExecutor] ⛔ {err}")
                        return OrderResult(signal=signal, success=False, error=err)
                    logger.debug(
                        f"[DepthGate] 通过 ask_depth=${_ask_depth:.2f} "
                        f">= min=${_min_depth_usdc:.0f} [{signal.market_id[:16]}]"
                    )
                except Exception as _de:
                    logger.debug(f"[DepthGate] orderbook 深度检查失败，放行: {_de}")

            # 2. 计算限价：Maker模式在 best_ask 下方抜单（加流动性，产生费用优惠）；
            # Taker模式在 best_ask 上方下单（立即成交）
            expected = getattr(signal, "expected_price", 0.0) or 0.5
            # T228: Signal dataclass 没有 category 字段，必须用 detect_category 推断
            _buy_cat = getattr(signal, "category", "") or detect_category(
                {"question": signal.question or ""}
            )
            order_mode = (
                "taker"
                if _force_taker
                else self._get_order_mode(_buy_cat, direction="buy")
            )
            # T63: 智能模式 → 根据 orderbook 动态决定
            if order_mode == "smart":
                order_mode = self._resolve_smart_mode(token_id, direction="buy")
            if order_mode == "maker":
                maker_offset = float(get("order_execution.maker.offset_pct") or 0.002)
                limit_price = round(min(max(expected - maker_offset, 0.01), 0.99), 2)
                maker_timeout = float(get("order_execution.maker.timeout_sec") or 300)
            else:
                # ── T170: Taker 模式 — orderbook 感知定价 ──────────────
                # 优先从 orderbook 获取 best_ask 作为 Taker 吃单价格
                # 确保 Taker 价格 > Maker 价格，真正实现跨越 spread 吃单
                taker_offset = float(get("order_execution.taker.offset_pct") or 0.01)
                taker_price = expected + taker_offset  # 基础价格

                # Fallback 场景: 从 orderbook 获取 best_ask 确保能吃到单
                if _force_taker:
                    try:
                        from src.utils.orderbook_history import fetch_orderbook

                        book = fetch_orderbook(token_id)
                        if book:
                            asks = book.get("asks") or []
                            if asks:
                                best_ask = float(asks[0].get("price", 0))
                                if 0.01 <= best_ask <= 0.99:
                                    # Taker 价格 = max(best_ask, expected+offset)
                                    # 确保能吃到 orderbook 最优卖单
                                    old_taker = taker_price
                                    taker_price = max(taker_price, best_ask)
                                    logger.info(
                                        f"[TakerFallback] orderbook定价: "
                                        f"best_ask={best_ask:.4f} "
                                        f"base={old_taker:.4f} → "
                                        f"taker={taker_price:.4f}"
                                    )
                    except Exception as exc:
                        logger.debug(f"[TakerFallback] orderbook查询失败: {exc}")

                limit_price = round(min(max(taker_price, 0.01), 0.99), 2)
                # 安全兜底: 确保 Taker 价格至少比 expected 高 1 tick (0.01)
                _expected_rounded = round(expected, 2)
                if limit_price <= _expected_rounded and _expected_rounded < 0.99:
                    limit_price = round(_expected_rounded + 0.01, 2)
                    logger.info(
                        f"[Taker] 1-tick保底: {_expected_rounded:.2f} → "
                        f"{limit_price:.2f}"
                    )
                maker_timeout = None  # use default fill_timeout_sec

            # 3. USDC → shares 转换（CLOB size = 股数，非 USDC）
            # T101: P0-5 显式防护降为0的价格
            if limit_price <= 0:
                logger.error(
                    f"[OrderExecutor] ⛔ limit_price={limit_price} <= 0，"
                    f"拒绝下单 | {signal.market_id}"
                )
                return OrderResult(
                    signal=signal,
                    success=False,
                    error="limit_price_zero_or_negative",
                )
            size_shares = signal.size_usdc / limit_price

            # 3.1 确保订单金额满足 CLOB 最小限额（动态从 Orderbook API 获取）
            from src.utils.orderbook_history import get_min_order_size

            _api_min_shares = get_min_order_size(token_id)
            _min_bet = float(get("executor.min_bet_usdc") or 2.0)
            _cfg_min_shares = float(get("executor.min_order_size_shares") or 5.0)
            # T79 Fix: 加入 buffer 防止 CLOB 部分成交导致 matched_shares < min_order_size
            # 目标 = min_shares + buffer（而非精确 min_shares），吸收部分成交损耗
            _buf_shares = float(get("executor.post_buy_topup_buffer_shares") or 1.0)
            # T181-3 Fix: 同步 safety_floor 作为最低目标
            # 确保首次买入目标 shares >= safety_floor，防止 partial fill 后 < CLOB 最小限额
            _safety_floor = float(
                get("executor.post_buy_topup_safety_floor_shares") or 5.1
            )
            # T204: TopUp 订单使用裸 CLOB 最小限额（无 buffer/floor），防止超买
            _is_topup = getattr(signal, "_is_topup", False)
            if _api_min_shares is not None:
                _raw_min = _api_min_shares
                if _is_topup:
                    _target_shares = _raw_min  # TopUp: 裸最小限额，不加 buffer/floor
                else:
                    _target_shares = max(_raw_min + _buf_shares, _safety_floor)
                _min_usdc_from_api = _target_shares * limit_price
                _effective_min_usdc = max(_min_usdc_from_api, _min_bet)
            else:
                # API 失败：使用 config min_order_size_shares 回退
                _raw_min = _cfg_min_shares
                if _is_topup:
                    _target_shares = _raw_min  # TopUp: 裸最小限额
                else:
                    _target_shares = max(_raw_min + _buf_shares, _safety_floor)
                _effective_min_usdc = max(_target_shares * limit_price, _min_bet)
                logger.warning(
                    f"[OrderExecutor] Orderbook API 获取 min_order_size 失败, "
                    f"使用 config fallback={_cfg_min_shares} shares "
                    f"(target={_target_shares:.1f} with buffer/floor, "
                    f"${_effective_min_usdc:.2f} USDC)"
                )
            _effective_cost = round(size_shares, 2) * limit_price
            if _effective_cost < _effective_min_usdc:
                _new_shares = math.ceil(_effective_min_usdc / limit_price * 100) / 100
                _new_cost = round(_new_shares * limit_price, 2)
                _original_cost = signal.size_usdc
                _amplification = (
                    _new_cost / _original_cost if _original_cost > 0 else 999
                )

                # T86-fix: 安全阀 — 放大超过上限时拒绝下单
                _max_amp = float(get("executor.max_clob_amplification") or 4.0)
                _is_probe = getattr(signal, "_is_probe", False)
                _is_topup = getattr(signal, "_is_topup", False)
                # TopUp 使用独立的放大上限（默认 5x），不再无限豁免
                _max_topup_amp = float(get("executor.max_topup_amplification") or 5.0)
                if _is_topup and _amplification > _max_topup_amp:
                    logger.warning(
                        f"[OrderExecutor] ⛔ TopUp放大过高: "
                        f"{_amplification:.1f}x > {_max_topup_amp:.1f}x上限 | "
                        f"策略=${_original_cost:.2f} → CLOB要求=${_new_cost:.2f} | "
                        f"{signal.market_id}"
                    )
                    return OrderResult(
                        signal=signal,
                        success=False,
                        error=(
                            f"TopUp amplification {_amplification:.1f}x > "
                            f"{_max_topup_amp:.1f}x limit"
                        ),
                    )
                elif _amplification > _max_amp and not _is_probe and not _is_topup:
                    logger.warning(
                        f"[OrderExecutor] ⛔ 拒绝下单: CLOB最小限额导致"
                        f"放大{_amplification:.1f}x (>${_max_amp:.1f}x上限) | "
                        f"策略=${_original_cost:.2f} → CLOB要求=${_new_cost:.2f} | "
                        f"{signal.market_id}"
                    )
                    return OrderResult(
                        signal=signal,
                        success=False,
                        error=(
                            f"CLOB amplification {_amplification:.1f}x > "
                            f"{_max_amp:.1f}x limit"
                        ),
                    )

                size_shares = _new_shares
                signal.size_usdc = _new_cost
                _mode_label = "TopUp裸最小" if _is_topup else "含buffer/floor"
                logger.info(
                    f"[OrderExecutor] 调整份数以满足最小订单({_mode_label}): "
                    f"${_effective_cost:.4f} → ${_new_cost:.4f} USDC "
                    f"(amplification={_amplification:.1f}x, "
                    f"target={_target_shares:.2f} shares "
                    f"[min={_raw_min}+buf={'0(topup)' if _is_topup else _buf_shares}, floor={'skip' if _is_topup else _safety_floor}], "
                    f"min_usdc={_effective_min_usdc:.2f})"
                )

            # T174: 放大后余额预检查 — 避免CLOB返回"not enough balance"
            # 利用已刷新的余额缓存，不额外发API请求
            _final_cost = round(size_shares, 2) * limit_price
            try:
                _pre_balance = self._check_balance()
                if _final_cost > _pre_balance:
                    msg = (
                        f"放大后成本超余额: cost=${_final_cost:.2f} > "
                        f"balance=${_pre_balance:.2f} "
                        f"(shares={size_shares:.2f} × price={limit_price:.4f})"
                    )
                    logger.warning(f"[OrderExecutor] ⛔ {msg} | {signal.market_id}")
                    return OrderResult(signal=signal, success=False, error=msg)
            except Exception as exc:
                logger.debug(f"[OrderExecutor] 放大后余额预检查失败（继续下单）: {exc}")

            # 4. 构建并提交限价单
            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=round(size_shares, 2),
                side=BUY,
            )
            # ── 下单前记录余额（用于计算实际成交金额）──────────────
            balance_before: float = -1.0
            try:
                balance_before = self._get_usdc_balance_raw(client)
            except Exception as exc:
                logger.debug(f"[BUY] 下单前余额查询失败（将回退到估算）: {exc}")

            resp = client.create_and_post_order(order_args)
            order_id = resp.get("orderID", "") if isinstance(resp, dict) else str(resp)

            _mode_tag = "[MakerSubmit]" if order_mode == "maker" else "[TakerSubmit]"
            logger.info(
                f"{_mode_tag} {signal.market_id} {signal.outcome} "
                f"@ {limit_price:.4f} shares={size_shares:.1f} "
                f"cost={signal.size_usdc:.2f}USDC orderID={order_id}"
            )

            # ── 等待成交确认 ──────────────────────────────────────
            fill = self._wait_for_fill(
                order_id, signal.size_usdc, timeout_override=maker_timeout
            )
            # 成交结果日志
            _fill_status = fill.get("status", "")
            if order_mode == "maker":
                if _fill_status == "MATCHED":
                    logger.info(
                        f"[MakerFullFill] orderID={order_id} "
                        f"filled={fill['filled_usdc']:.2f}USDC "
                        f"shares={fill.get('matched_shares', 0):.1f}"
                    )
                elif _fill_status in (
                    "TIMEOUT_PARTIAL",
                    "TIMEOUT_CANCELLED",
                    "TIMEOUT_CANCEL_FAILED",
                ):
                    _matched_pct = (
                        fill.get("matched_shares", 0) / size_shares
                        if size_shares > 0
                        else 0
                    )
                    if _fill_status == "TIMEOUT_PARTIAL" and _matched_pct >= float(
                        get("order_execution.maker.partial_fill_threshold") or 0.5
                    ):
                        logger.info(
                            f"[MakerPartialFill] orderID={order_id} "
                            f"filled={fill['filled_usdc']:.2f}USDC ({_matched_pct:.0%})"
                        )
                    else:
                        _maker_partial_usdc = fill.get("filled_usdc", 0)
                        logger.warning(
                            f"[MakerTimeout] orderID={order_id} "
                            f"filled={_maker_partial_usdc:.2f}USDC ({_matched_pct:.0%}) 成交不足"
                        )
                        if _maker_partial_usdc > 0:
                            # T204追加: Maker已有链上Token(部分成交)，
                            # 跳过TakerFallback防止全额重发导致超买。
                            # 部分成交结果由下方 fill["filled_usdc"]>0 分支
                            # 返回 success=True，PostBuyTopUp 自动补齐剩余份额。
                            logger.info(
                                f"[MakerTimeout] 保留Maker部分成交"
                                f"${_maker_partial_usdc:.2f}, "
                                f"跳过TakerFallback，由PostBuyTopUp补齐"
                            )
                        elif get("order_execution.maker.fallback_to_taker"):
                            logger.info(
                                f"[TakerFallback] {signal.market_id} {signal.outcome} "
                                f"Maker超时(零成交)，退回 Taker 模式重新下单"
                            )
                            return self._submit_order(signal, _force_taker=True)
            # CLOB 返回的实际成交股数（平台真实数据）
            _clob_matched = fill.get("matched_shares", 0.0)

            # ── 用余额差计算实际花费（辅助参考）──────────────────
            actual_cost = fill["filled_usdc"]  # fallback: CLOB matched×limit
            actual_avg_price = limit_price  # fallback: 限价
            if fill["filled_usdc"] > 0 and balance_before >= 0:
                try:
                    # 等待链上结算（CLOB match 与链上扣款之间有延迟）
                    _sd = get("executor.settlement_delay_sec")
                    settle_delay = float(_sd if _sd is not None else 8)
                    if settle_delay > 0:
                        time.sleep(settle_delay)

                    balance_after = self._get_usdc_balance_raw(client)
                    delta = round(balance_before - balance_after, 6)
                    if delta > 0:
                        est = fill["filled_usdc"]
                        # Phase35c: 用 CLOB 实际成交股数计算 fill_ratio 和均价
                        # 防止部分成交时分母为原始请求股数导致均价偏低
                        matched_shares = fill.get("matched_shares", 0)
                        cal_shares = (
                            matched_shares if matched_shares > 0 else size_shares
                        )
                        expected_cost = cal_shares * limit_price
                        fill_ratio = delta / expected_cost if expected_cost > 0 else 0

                        # ── 余额疑似未结算 — 重试 ──────────────────
                        if fill_ratio < 0.85 and fill["status"] == "MATCHED":
                            _rc = get("executor.balance_retry_count")
                            retry_n = int(_rc if _rc is not None else 3)
                            _ri = get("executor.balance_retry_interval_sec")
                            retry_iv = float(_ri if _ri is not None else 3)
                            for _r in range(retry_n):
                                time.sleep(retry_iv)
                                balance_after = self._get_usdc_balance_raw(client)
                                delta = round(balance_before - balance_after, 6)
                                fill_ratio = (
                                    delta / expected_cost if expected_cost > 0 else 0
                                )
                                logger.debug(
                                    f"[BUY] 余额结算重试 "
                                    f"{_r + 1}/{retry_n}: "
                                    f"delta=${delta:.4f} "
                                    f"fill_ratio={fill_ratio:.0%}"
                                )
                                if fill_ratio >= 0.85:
                                    break

                        # ── 部分成交检测 + 余额校准 ──────────────────
                        if fill_ratio < 0.85:
                            if fill["status"] == "MATCHED":
                                # T189-FIX: 高限价低成交场景检测
                                # 当 limit_price >> 实际成交价时（如跟投taker @ 0.99，
                                # 实际成交 0.35），CLOB filled_usdc = shares × limit（上界）
                                # 远大于真实链上扣款（余额差）。此时余额差才是准确的。
                                _bal_avg = (
                                    round(delta / cal_shares, 6)
                                    if cal_shares > 0
                                    else 0
                                )
                                if (
                                    delta > 0
                                    and 0.01 <= _bal_avg <= 0.99
                                    and limit_price >= 0.80
                                    and _bal_avg < limit_price * 0.85
                                ):
                                    # 余额已结算，成交价远低于限价 → 信任余额差
                                    actual_cost = delta
                                    actual_avg_price = _bal_avg
                                    logger.info(
                                        f"[BUY] 高限价低成交校准: "
                                        f"limit={limit_price:.4f} "
                                        f"余额均价={_bal_avg:.4f} "
                                        f"delta=${delta:.4f} "
                                        f"CLOB=${fill['filled_usdc']:.4f}"
                                    )
                                else:
                                    # 真正的余额结算超时 → 信任 CLOB
                                    actual_cost = fill["filled_usdc"]
                                    logger.warning(
                                        f"[BUY] 余额结算超时: "
                                        f"delta=${delta:.4f} vs "
                                        f"CLOB=${fill['filled_usdc']:.4f}，"
                                        f"使用 CLOB 成交数据"
                                    )
                            else:
                                # 真正的部分成交
                                actual_cost = delta
                                logger.info(
                                    f"[BUY] 部分成交检测: "
                                    f"delta=${delta:.4f} vs "
                                    f"预期=${expected_cost:.4f} "
                                    f"(fill_ratio={fill_ratio:.0%})，"
                                    f"使用限价 {limit_price:.4f} 作为入场价"
                                )
                        else:
                            # 全额（或近全额）成交：用余额差校准均价
                            calibrated_avg = (
                                round(delta / cal_shares, 6) if cal_shares > 0 else 0
                            )
                            # 双重保护：校准后均价仍不得低于合理基准的 50%
                            # T172-FIX: limit_price 在 Taker 模式下可能高达 0.99（taker上限）
                            # 当 limit_price >= 0.90 时，余额校准结果（如 0.27）会被错误地
                            # 判为「异常」并回退到 limit_price=0.99，导致 market_price 写错
                            # 修复：当 limit_price 接近 taker 上限时，仅拒绝明显无效的校准值（<0.01）
                            _price_guard_threshold = (
                                0.01 if limit_price >= 0.90 else limit_price * 0.5
                            )
                            if calibrated_avg < _price_guard_threshold:
                                logger.warning(
                                    f"[BUY] 余额校准异常: "
                                    f"校准均价={calibrated_avg:.4f} "
                                    f"< 下限={_price_guard_threshold:.4f}，"
                                    f"使用限价作为入场价"
                                )
                            else:
                                actual_cost = delta
                                if cal_shares > 0:
                                    actual_avg_price = calibrated_avg
                                if abs(est - delta) > 0.001:
                                    logger.info(
                                        f"[BUY] 金额校准: 估算=${est:.4f}"
                                        f" → 实际=${delta:.4f}"
                                        f" (差${est - delta:.4f})"
                                    )
                except Exception as exc:
                    logger.debug(f"[BUY] 成交后余额查询失败: {exc}")

            # ── 以 Data API 官方成交数据为准（最终校准）──────────
            cid = getattr(signal, "condition_id", "")
            # condition_id 为空时尝试从 market_id 解析（兼容旧 bets）
            if not cid:
                cid = self._to_condition_id(signal.market_id)
                if cid and cid.startswith("0x"):
                    signal.condition_id = cid
            if fill["filled_usdc"] > 0 and cid:
                chain = self._verify_trade_on_chain(cid, side="BUY")
                if chain.get("verified"):
                    chain_cost = chain["size"] * chain["price"]
                    if chain_cost > 0:
                        # Phase34b: 安全防护 — 不允许校准大幅缩小成本（陈旧数据特征）
                        # T223-FIX: 当 limit_price >= 0.90 时，CLOB 返回的
                        # actual_cost 基于 limit_price（taker 上限），不代表真实成本。
                        # 若 actual_avg_price 仍接近 limit_price（≥85%），说明前序校准
                        # 均未生效，此时必须信任 Data API — 这是最后的价格修正机会。
                        _taker_override = (
                            limit_price >= 0.90
                            and actual_avg_price >= limit_price * 0.85
                            and chain["price"] > 0.01
                        )
                        if (
                            actual_cost > 0
                            and chain_cost < actual_cost * 0.7
                            and not _taker_override
                        ):
                            logger.warning(
                                f"[BUY] Data API 校准被拒: "
                                f"chain_cost=${chain_cost:.4f} < "
                                f"actual×0.7=${actual_cost * 0.7:.4f}，"
                                f"疑似陈旧数据"
                            )
                        else:
                            if _taker_override:
                                logger.info(
                                    f"[BUY] Data API 校准覆盖 "
                                    f"(taker高限价 {limit_price:.2f}): "
                                    f"chain_cost=${chain_cost:.4f} < "
                                    f"actual×0.7=${actual_cost * 0.7:.4f}，"
                                    f"但 avg_price={actual_avg_price:.4f} "
                                    f"仍接近限价 → 信任 Data API"
                                )
                            # T172-FIX: 即使成本一致，若 chain_price 与 actual_avg_price
                            # 差距 > 0.01，也必须修正价格。防止 limit_price=0.99（taker上限）
                            # 作为 fallback 填入 market_price 导致数据严重失真
                            price_changed = (
                                abs(chain["price"] - actual_avg_price) > 0.01
                            )
                            cost_changed = abs(chain_cost - actual_cost) > 0.005
                            if price_changed or cost_changed:
                                logger.info(
                                    f"[BUY] Data API 校准: "
                                    f"price {actual_avg_price:.4f}→{chain['price']:.4f} | "
                                    f"cost ${actual_cost:.4f}→${chain_cost:.4f}"
                                )
                                actual_avg_price = chain["price"]
                                actual_cost = chain_cost
                                # Data API 返回的 size 是精确股数
                                if chain["size"] > 0:
                                    _clob_matched = chain["size"]

            if fill["status"] == "MATCHED":
                logger.info(
                    f"订单已成交 | {order_id} "
                    f"filled={actual_cost:.2f} USDC "
                    f"shares={_clob_matched:.4f} "
                    f"avg={actual_avg_price:.6f}"
                )
                return OrderResult(
                    signal=signal,
                    success=True,
                    tx_hash=order_id,
                    actual_price=actual_avg_price,
                    filled_size=actual_cost,
                    condition_id=getattr(signal, "condition_id", ""),
                    matched_shares=_clob_matched,
                    order_type=order_mode,
                )
            elif fill["filled_usdc"] > 0:
                # 部分成交
                logger.warning(
                    f"订单部分成交 | {order_id} "
                    f"filled={actual_cost:.2f}/{signal.size_usdc:.2f} USDC "
                    f"shares={_clob_matched:.4f} "
                    f"status={fill['status']}"
                )
                return OrderResult(
                    signal=signal,
                    success=True,
                    tx_hash=order_id,
                    actual_price=actual_avg_price,
                    filled_size=actual_cost,
                    condition_id=getattr(signal, "condition_id", ""),
                    matched_shares=_clob_matched,
                    order_type=order_mode,
                )
            else:
                logger.warning(
                    f"订单未成交 | {order_id} status={fill['status']}，"
                    f"不计入投注记录"
                )
                return OrderResult(
                    signal=signal,
                    success=False,
                    tx_hash=order_id,
                    error=f"订单未成交: {fill['status']}",
                    condition_id=getattr(signal, "condition_id", ""),
                    order_type=order_mode,
                )
        except Exception as exc:
            logger.error(f"下单失败 [{signal.market_id} {signal.outcome}]: {exc}")
            return OrderResult(signal=signal, success=False, error=str(exc))

    def _resolve_token_id(self, client, signal: Signal) -> str:
        """通过 CLOB API 查询市场 token ID（signal 中无此字段时的备选方案）。

        适配两种 market_id 格式：
        - condition_id (0x...)：直接查 CLOB
        - Gamma numeric ID：先通过 Gamma API 获取 conditionId，再查 CLOB

        部分市场使用自定义 outcome 名称（如 Up/Down、Knicks/Nets），而非标准 Yes/No。
        先尝试精确匹配，失败则按位置回退：YES→第一个token，NO→第二个token。

        副作用: 设置 signal.condition_id（供后续 reconcile 使用）。
        """
        market_id = signal.market_id
        condition_id = self._to_condition_id(market_id)

        # 保存 condition_id 到 signal（reconcile 依赖此字段匹配链上数据）
        if condition_id and condition_id.startswith("0x"):
            signal.condition_id = condition_id

        try:
            market_data = client.get_market(condition_id)
            tokens = market_data.get("tokens", [])
            # 1. 精确匹配 outcome 名称（标准 Yes/No 市场）
            for t in tokens:
                if (
                    isinstance(t, dict)
                    and t.get("outcome", "").upper() == signal.outcome.upper()
                ):
                    return t["token_id"]
            # 2. 位置回退：自定义 outcome 市场（Up/Down, Knicks/Nets 等）
            #    Polymarket 惯例：tokens[0]=肯定方(YES等价), tokens[1]=否定方(NO等价)
            if len(tokens) >= 2:
                idx = 0 if signal.outcome.upper() in ("YES", "Y") else 1
                fallback_token = tokens[idx]
                if isinstance(fallback_token, dict) and fallback_token.get("token_id"):
                    actual_outcome = fallback_token.get("outcome", "?")
                    logger.info(
                        f"[token_id] 位置回退: {market_id} outcome={signal.outcome}"
                        f" → tokens[{idx}] (actual={actual_outcome})"
                    )
                    return fallback_token["token_id"]
        except Exception as exc:
            raise ValueError(
                f"无法解析 token_id [{market_id}/{signal.outcome}]: {exc}"
            ) from exc
        raise ValueError(f"市场 {market_id} 无匹配 outcome={signal.outcome} 的 token")

    @staticmethod
    def _to_condition_id(market_id: str) -> str:
        """将 Gamma numeric ID 转换为 CLOB condition_id（0x...）。

        如果已是 0x 格式，直接返回。否则请求 Gamma API 获取 conditionId。
        """
        if market_id.startswith("0x"):
            return market_id
        # Gamma numeric ID → 查 Gamma API 获取 conditionId
        try:
            resp = requests.get(
                f"https://gamma-api.polymarket.com/markets/{market_id}",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                data = data[0] if data else {}
            cid = data.get("conditionId") or data.get("condition_id", "")
            if cid:
                logger.debug(
                    f"[token_id] Gamma {market_id} → conditionId {cid[:16]}..."
                )
                return cid
        except Exception as e:
            logger.warning(f"[token_id] Gamma API 查询失败 {market_id}: {e}")
        return market_id  # 回退（可能仍然失败）

    # ------------------------------------------------------------------
    # Post-trade 链上数据验证（以平台数据为准）
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_trade_on_chain(
        condition_id: str, side: str = "BUY", retries: int = 3
    ) -> dict:
        """下单成功后，查 Data API /trades 获取真实成交数据。

        Returns:
            {"verified": True, "price": float, "size": float}
            或 {"verified": False}（查询失败/无数据时）
        """
        funder = os.environ.get("POLY_RELAYER_ADDRESS", "")
        if not funder or not condition_id:
            return {"verified": False}

        # Data API 索引延迟可达 10-30 秒，先等待再查询
        _cd = get("executor.chain_verify_initial_delay_sec")
        initial_delay = float(_cd if _cd is not None else 5)
        if initial_delay > 0:
            time.sleep(initial_delay)

        for attempt in range(retries):
            if attempt > 0:
                time.sleep(5)  # Data API 索引延迟
            try:
                resp = requests.get(
                    "https://data-api.polymarket.com/trades",
                    params={
                        "user": funder.lower(),
                        "market": condition_id,
                        "side": side,
                        "limit": 5,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                trades = resp.json()
                if not isinstance(trades, list) or not trades:
                    continue

                # 取最新成交
                latest = max(trades, key=lambda t: t.get("timestamp", 0))
                chain_price = float(latest.get("price", 0))
                chain_size = float(latest.get("size", 0))
                # Phase34b: 检查成交时间新鲜度，防止读到旧订单的成交数据
                trade_ts = float(latest.get("timestamp", 0))
                max_age = get("reconcile.min_age_seconds") or 300
                age = time.time() - trade_ts
                if age > max_age:
                    logger.debug(
                        f"[TradeVerify] 跳过过期成交记录: "
                        f"age={age:.0f}s > {max_age}s"
                    )
                    continue
                if chain_price > 0 and chain_size > 0:
                    logger.info(
                        f"[TradeVerify] Data API 成交确认: "
                        f"price={chain_price:.4f} size={chain_size:.2f} "
                        f"age={age:.0f}s "
                        f"tx={str(latest.get('transactionHash', '?'))[:16]}"
                    )
                    return {
                        "verified": True,
                        "price": chain_price,
                        "size": chain_size,
                    }
            except Exception as exc:
                logger.debug(f"[TradeVerify] attempt {attempt + 1} 失败: {exc}")

        return {"verified": False}

    # ------------------------------------------------------------------
    # 卖出（止盈/止损时使用）
    # ------------------------------------------------------------------

    def sell_position(self, bet: dict) -> OrderResult:
        """
        卖出持仓（止盈/止损时调用）。

        DRY_RUN 模式下仅记录日志，不真实下单。
        实盘模式下通过 CLOB API 提交 SELL 限价单。

        Args:
            bet: 包含 market_id, outcome, exit_price, size_usdc, market_price 的字典
        Returns:
            OrderResult
        """
        dry_run: bool = is_dry_run()
        market_id = bet.get("market_id", "")
        outcome = (bet.get("outcome") or "YES").upper()
        exit_price = float(bet.get("exit_price", 0))
        entry_price = float(bet.get("market_price") or 0.5)
        size_usdc = float(bet.get("size_usdc", 0))
        trigger = bet.get("trigger", "unknown")

        # 构造一个 Signal 以复用 token_id 解析逻辑
        sell_signal = Signal(
            market_id=market_id,
            question=bet.get("question", "")[:200],
            outcome=outcome,
            ev=0.0,
            confidence=0.0,
            size_usdc=size_usdc,
            condition_id=bet.get("condition_id", ""),
        )

        if dry_run:
            _dry_sell_shares = (
                round(size_usdc / entry_price, 6) if entry_price > 0 else 0.0
            )
            logger.info(
                f"[DRY RUN SELL] {trigger} | {market_id[:20]} {outcome} "
                f"entry={entry_price:.3f} exit={exit_price:.3f} "
                f"size={size_usdc:.2f} USDC shares={_dry_sell_shares:.4f}"
            )
            return OrderResult(
                signal=sell_signal,
                success=True,
                tx_hash="DRY_RUN_SELL",
                matched_shares=_dry_sell_shares,
            )

        # 实盘卖出
        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import SELL

            client = self._get_clob_client()

            # 解析 token_id
            token_id = self._resolve_token_id(client, sell_signal)
            if not token_id:
                err = f"token_id 为空，中止卖出 [{market_id}/{outcome}]"
                logger.error(f"[OrderExecutor] {err}")
                return OrderResult(signal=sell_signal, success=False, error=err)

            # ── 刷新 CLOB 缓存 + 查询实际 token 余额 ──────────────
            actual_shares = self._refresh_and_get_token_balance(client, token_id)
            if actual_shares == 0:
                err = (
                    f"链上 token 余额为 0，无法卖出 [{market_id}/{outcome}] "
                    f"token_id={token_id[:16]}..."
                )
                logger.error(f"[OrderExecutor] {err}")
                return OrderResult(signal=sell_signal, success=False, error=err)

            # 计算股数：shares = size_usdc / entry_price
            shares = size_usdc / entry_price if entry_price > 0 else 0
            if shares <= 0:
                err = f"无效股数 {shares:.2f}，中止卖出"
                logger.warning(f"[OrderExecutor] {err}")
                return OrderResult(signal=sell_signal, success=False, error=err)

            # 卖出全部链上实际余额（避免残余 shares 成为孤儿仓位）
            # actual_shares < 0 表示查询失败，此时退回使用计算股数
            if actual_shares > 0:
                if abs(shares - actual_shares) / max(shares, 1e-9) > 0.01:
                    logger.info(
                        f"[SELL] 计算股数 {shares:.4f} vs 实际余额 {actual_shares:.4f}，"
                        f"使用实际余额以清仓全部持仓"
                    )
                shares = actual_shares

            # 分批止盈：仅卖出指定股数而非全仓
            _sell_shares_override = float(bet.get("sell_shares_override", 0))
            if _sell_shares_override > 0 and _sell_shares_override < shares:
                logger.info(
                    f"[SELL] 分批止盈: 卖出 {_sell_shares_override:.4f} / "
                    f"{shares:.4f} 股 ({_sell_shares_override/shares:.0%})"
                )
                shares = _sell_shares_override

            # T63: 卖出模式决策 — Maker/Taker/智能 按赛道独立配置
            _sell_category = bet.get("category", "")
            sell_order_mode = self._get_order_mode(_sell_category, direction="sell")
            if sell_order_mode == "smart":
                sell_order_mode = self._resolve_smart_mode(token_id, direction="sell")

            # T169: 小仓位安全保护 — 防止 partial fill 导致残余 < min_order_size 无法卖出
            # 当总份数 < 2×min_order_size 时，partial fill 后剩余必然无法CLOB卖出
            # 强制 taker 模式以最大化全量成交概率
            _sell_min_order = float(get("executor.min_order_size_shares") or 5.0)
            if shares < _sell_min_order * 2 and sell_order_mode != "taker":
                logger.info(
                    f"[SELL|T169] 仓位安全保护: shares={shares:.2f} < "
                    f"2×min_order_size={_sell_min_order * 2:.1f}，"
                    f"强制 taker 模式防止 partial fill 导致残余无法卖出"
                )
                sell_order_mode = "taker"

            # 卖出限价计算
            fail_count = int(bet.get("sell_fail_count", 0) or 0)
            # M2-T251：高失败次数强制 Taker 激进出场（防止 Maker 挂单无法成交无限滞留）
            _aggressive_threshold = int(
                get("executor.sell_fail_count_for_aggressive") or 5
            )
            if fail_count >= _aggressive_threshold and sell_order_mode != "taker":
                logger.warning(
                    f"[SELL|Aggressive] fail_count={fail_count} >= "
                    f"threshold={_aggressive_threshold}，"
                    f"强制切换 Taker(FOK) 激进出场 [{market_id[:16]}]"
                )
                sell_order_mode = "taker"
            # T245: sell_order_type 控制 GTC vs FOK
            # Taker 使用 FOK（Fill or Kill）防止部分成交导致残余 < min_order_size
            # Maker 使用 GTC（Good Till Cancel）挂单等待
            sell_order_type = OrderType.GTC  # 默认 GTC（Maker）
            if sell_order_mode == "taker":
                # T245核心修复: Taker 卖出必须使用 FOK 订单类型。
                # 事故 eb2f98d9: GTC 限价单在薄流动性下 45s 超时仅成交 1.6/6.0 shares,
                # 残余 4.35 < min_order_size 5.0，导致 CLOB 无法卖出，用户被迫手动操作。
                # FOK 保证: 全量成交或全部拒绝，绝不产生部分成交残余。
                sell_order_type = OrderType.FOK
                # T215: Taker卖出使用实时 orderbook bestBid 定价
                taker_offset = float(get("executor.offset_pct") or 0.001)
                offset_pct = taker_offset + fail_count * float(
                    get("executor.sell_progressive_step") or 0.005
                )
                # fallback: 使用 exit_price 减去渐进 offset
                limit_price = round(max(exit_price - offset_pct, 0.01), 4)
                if get("order_execution.taker.use_orderbook_best_bid", True):
                    try:
                        from src.utils.orderbook_history import fetch_orderbook as _fob

                        _book = _fob(token_id)
                        _bids = (_book.get("bids") or []) if _book else []
                        if _bids:
                            _best_bid = float(_bids[0].get("price", 0))
                            if 0.01 <= _best_bid <= 0.99:
                                # T231: bestBid极端偏离保护 — 防止做市操纵导致贱卖
                                _bid_sanity_ratio = float(
                                    get("executor.bid_sanity_min_ratio") or 0.1
                                )
                                if (
                                    exit_price > 0.05
                                    and _best_bid < exit_price * _bid_sanity_ratio
                                ):
                                    logger.warning(
                                        f"[SELL|Taker] ⛔ bestBid极端偏离保护: "
                                        f"bestBid={_best_bid:.4f} < "
                                        f"exit_cache={exit_price:.4f}×{_bid_sanity_ratio} "
                                        f"= {exit_price * _bid_sanity_ratio:.4f}，"
                                        f"疑似做市操纵，拒绝贱卖"
                                    )
                                    return OrderResult(
                                        signal=sell_signal,
                                        success=False,
                                        error=(
                                            f"bid_sanity_block: bestBid={_best_bid:.4f} "
                                            f"远低于exit_cache={exit_price:.4f}，"
                                            f"疑似做市操纵"
                                        ),
                                    )
                                # 以 bestBid 为 limit：卖单立即与最高买盘成交（真正 taker）
                                _entry = float(bet.get("market_price") or 0.01)
                                _floor = round(max(_entry * 0.02, 0.01), 4)
                                limit_price = round(max(_best_bid, _floor), 4)
                                logger.info(
                                    f"[SELL|Taker] 实时bestBid定价: "
                                    f"exit_cache={exit_price:.4f} "
                                    f"bestBid={_best_bid:.4f} → limit={limit_price:.4f}"
                                )
                    except Exception as _ob_err:
                        logger.debug(
                            f"[SELL|Taker] orderbook查询失败，回退exit_price定价: {_ob_err}"
                        )
                maker_sell_timeout = None
                logger.info(
                    f"[SELL] Taker(FOK)模式 | cat={_sell_category} "
                    f"exit={exit_price:.4f} limit={limit_price:.4f}"
                )
            else:
                # Maker卖出：更保守的偏移，挂单等待更好价格
                base_offset = float(get("executor.sell_offset_pct") or 0.002)
                progressive_step = float(get("executor.sell_progressive_step") or 0.005)
                offset_pct = base_offset + fail_count * progressive_step
                limit_price = round(max(exit_price - offset_pct, 0.01), 4)
                maker_sell_timeout = float(
                    get("order_execution.maker.timeout_sec") or 300
                )
                logger.info(
                    f"[SELL] Maker模式 | cat={_sell_category} "
                    f"exit={exit_price:.4f} offset={offset_pct:.4f} limit={limit_price:.4f}"
                )
            if fail_count > 0:
                logger.info(
                    f"[SELL] 渐进式定价: fail_count={fail_count} "
                    f"offset={offset_pct:.4f} limit={limit_price:.4f}"
                )

            # 向下取整：防止 round() 上取超过实际余额
            sell_size = math.floor(shares * 100) / 100
            if sell_size <= 0:
                # T34: 判断是否是外部卖出导致的灰尘残余
                # actual_shares > 0 但 < 0.01 = 用户手动卖出后的灰尘
                if 0 < actual_shares < 0.01:
                    err = (
                        f"zero_shares_dust_residual: 链上仅剩灰尘 "
                        f"{actual_shares:.6f} shares（用户可能已手动卖出），"
                        f"无法CLOB卖出 [{market_id[:20]}/{outcome}]"
                    )
                    logger.warning(f"[OrderExecutor] {err}")
                else:
                    err = f"向下取整后股数为 0 (原始={shares:.6f})，中止卖出"
                    logger.warning(f"[OrderExecutor] {err}")
                return OrderResult(signal=sell_signal, success=False, error=err)

            # ── 最小下单量预检查（从 Orderbook API 动态获取，API 不可用时回退 config）──
            from src.utils.orderbook_history import get_min_order_size

            _min_order_shares = get_min_order_size(token_id)
            if _min_order_shares is None:
                _cfg_min = float(get("executor.min_order_size_shares") or 5.0)
                logger.warning(
                    f"[OrderExecutor] Orderbook API 获取 min_order_size 失败, "
                    f"使用 config fallback={_cfg_min} [{market_id[:20]}/{outcome}]"
                )
                _min_order_shares = _cfg_min
            if sell_size < _min_order_shares:
                import time as _tmod

                _throttle_key = f"{market_id}_{outcome}"
                _now = _tmod.monotonic()
                _last_warn = _sell_min_warn_seen.get(_throttle_key, 0.0)
                err = (
                    f"below_min_order_size: sell_size={sell_size:.2f} "
                    f"< min={_min_order_shares} shares, "
                    f"仓位过小无法在 CLOB 卖出 [{market_id[:20]}/{outcome}]"
                )
                if _now - _last_warn >= _get_sell_min_warn_interval():
                    _sell_min_warn_seen[_throttle_key] = _now
                    logger.warning(f"[OrderExecutor] {err}")
                else:
                    logger.debug(f"[OrderExecutor] {err} (降频静默)")
                return OrderResult(signal=sell_signal, success=False, error=err)
            logger.info(
                f"[SELL] min_order_size={_min_order_shares} shares, "
                f"sell_size={sell_size:.2f} shares (source=orderbook_api)"
            )

            # ── 卖出前记录余额（用于计算实际收回金额）──────────────
            sell_balance_before: float = -1.0
            try:
                sell_balance_before = self._get_usdc_balance_raw(client)
            except Exception as exc:
                logger.debug(f"[SELL] 卖出前余额查询失败: {exc}")

            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=sell_size,
                side=SELL,
            )
            # T245: Taker 使用 FOK（Fill or Kill），Maker 使用 GTC
            _signed_order = client.create_order(order_args)
            resp = client.post_order(_signed_order, orderType=sell_order_type)
            order_id = resp.get("orderID", "") if isinstance(resp, dict) else str(resp)

            _otype_label = "FOK" if sell_order_type == OrderType.FOK else "GTC"
            logger.info(
                f"[SELL] {trigger} 卖出单已提交 | {market_id[:20]} {outcome} "
                f"@ {limit_price:.4f} shares={shares:.1f} mode={sell_order_mode} "
                f"orderType={_otype_label} orderID={order_id}"
            )

            # ── 等待卖出成交确认 ──────────────────────────────────
            # T245: FOK 订单是即时的（成交或拒绝），使用短超时(10s)
            # GTC (Maker) 使用配置的 maker_sell_timeout
            if sell_order_mode == "taker":
                _sell_timeout_override = float(get("executor.fok_timeout_sec") or 10)
            else:
                _sell_timeout_override = maker_sell_timeout
            fill = self._wait_for_fill(
                order_id, size_usdc, timeout_override=_sell_timeout_override
            )
            _sell_matched = fill.get("matched_shares", 0.0)
            if _sell_matched <= 0:
                _sell_matched = sell_size
            if fill["filled_usdc"] > 0:
                # 用实际成交额 / 卖出股数算出实际均价
                actual_avg_price = (
                    fill["filled_usdc"] / sell_size if sell_size > 0 else limit_price
                )
                logger.info(
                    f"[SELL] 卖出已成交 | {order_id} "
                    f"filled={fill['filled_usdc']:.2f} USDC "
                    f"shares={_sell_matched:.4f} status={fill['status']}"
                )

                # ── sweep 残余 shares（防止孤儿仓位）──────────────
                sweep_usdc, final_residual = self._sweep_residual_shares(
                    client, token_id, limit_price
                )
                total_filled = fill["filled_usdc"] + sweep_usdc

                # ── 用余额差校准实际收回金额 ──────────────────────
                _balance_delta_applied = False
                if sell_balance_before >= 0:
                    try:
                        sell_balance_after = self._get_usdc_balance_raw(client)
                        sell_delta = round(sell_balance_after - sell_balance_before, 6)
                        if sell_delta > 0:
                            if abs(total_filled - sell_delta) > 0.001:
                                logger.info(
                                    f"[SELL] 金额校准: 估算=${total_filled:.4f} → "
                                    f"实际=${sell_delta:.4f} "
                                    f"(差${total_filled - sell_delta:.4f})"
                                )
                            total_filled = sell_delta
                            _balance_delta_applied = True
                    except Exception as exc:
                        logger.debug(f"[SELL] 卖出后余额查询失败: {exc}")

                actual_avg_price = (
                    total_filled / sell_size if sell_size > 0 else limit_price
                )

                # ── 以 Data API 官方成交数据校准卖出价 ────────────
                sell_cid = getattr(sell_signal, "condition_id", "")
                # condition_id 为空时尝试从 market_id 解析（兼容旧 bets）
                if not sell_cid:
                    sell_cid = self._to_condition_id(market_id)
                    if sell_cid and sell_cid.startswith("0x"):
                        sell_signal.condition_id = sell_cid
                if sell_cid:
                    chain = self._verify_trade_on_chain(sell_cid, side="SELL")
                    if chain.get("verified"):
                        chain_proceeds = chain["size"] * chain["price"]
                        if (
                            chain_proceeds > 0
                            and abs(chain_proceeds - total_filled) > 0.005
                        ):
                            # Phase34b: 安全防护 — 不允许校准大幅缩小收益
                            if total_filled > 0 and chain_proceeds < total_filled * 0.7:
                                logger.warning(
                                    f"[SELL] Data API 校准被拒: "
                                    f"chain_proceeds=${chain_proceeds:.4f} < "
                                    f"actual×0.7=${total_filled * 0.7:.4f}，"
                                    f"疑似陈旧数据"
                                )
                            # Phase40: 余额差可信时禁止 Data API 虚增收益
                            # (Data API size×price 可能是含手续费前的总额，高于实际到账)
                            elif (
                                _balance_delta_applied
                                and total_filled > 0
                                and chain_proceeds > total_filled * 1.05
                            ):
                                logger.info(
                                    f"[SELL] Data API 仅参考价格(余额差为准): "
                                    f"chain_proceeds=${chain_proceeds:.4f} > 余额差${total_filled:.4f}×1.05，"
                                    f"price 更新={chain['price']:.4f}，proceeds 保持实际到账=${total_filled:.4f}"
                                )
                                actual_avg_price = chain["price"]
                                _sell_matched = chain["size"]
                                # total_filled 保持余额差（钱包实际收到），不覆盖
                            else:
                                logger.info(
                                    f"[SELL] Data API 校准: "
                                    f"price {actual_avg_price:.4f}→{chain['price']:.4f} | "
                                    f"proceeds ${total_filled:.4f}→${chain_proceeds:.4f}"
                                )
                                actual_avg_price = chain["price"]
                                total_filled = chain_proceeds
                                _sell_matched = chain["size"]

                # ── 记录残余 shares（供调用方决定DB处理逻辑）────
                _sweep_min = float(get("executor.sweep_min_shares") or 0.01)
                _residual = max(final_residual, 0.0) if final_residual >= 0 else 0.0
                if _residual > _sweep_min:
                    logger.warning(
                        f"[SELL] ⚠️ 卖出后仍有 {_residual:.4f} shares 残余 | "
                        f"{market_id[:20]} {outcome}"
                    )

                return OrderResult(
                    signal=sell_signal,
                    success=True,
                    tx_hash=order_id,
                    actual_price=actual_avg_price,
                    filled_size=total_filled,
                    condition_id=sell_cid,
                    remaining_shares=_residual,
                    matched_shares=_sell_matched,
                )
            else:
                logger.warning(
                    f"[SELL] 卖出未成交 | {order_id} status={fill['status']}"
                )
                return OrderResult(
                    signal=sell_signal,
                    success=False,
                    tx_hash=order_id,
                    error=f"卖出未成交: {fill['status']}",
                )
        except ImportError as exc:
            logger.error("py-clob-client 未安装: %s", exc)
            return OrderResult(signal=sell_signal, success=False, error=str(exc))
        except Exception as exc:
            logger.error(f"卖出失败 [{market_id} {outcome}]: {exc}")
            return OrderResult(signal=sell_signal, success=False, error=str(exc))

    # ------------------------------------------------------------------
    # Sweep 残余 shares（防止孤儿仓位）
    # ------------------------------------------------------------------

    def _sweep_residual_shares(
        self, client, token_id: str, last_price: float
    ) -> tuple[float, float]:
        """卖出成功后检查并清扫链上残余 shares。

        等待链上结算后再检查余额，避免读到 stale 数据导致孤儿仓位。

        Args:
            client: CLOB client 实例
            token_id: 已卖出的 token ID
            last_price: 上一笔卖出的限价（作为 sweep 定价参考）

        Returns:
            (sweep_usdc, final_residual):
              sweep_usdc: sweep 成交金额 (USDC)
              final_residual: sweep 后仍残留的 shares 数量
        """
        min_sweep = float(get("executor.sweep_min_shares") or 0.01)

        # ── 等待链上结算，确保余额反映真实状态 ──────────────
        _sd = get("executor.settlement_delay_sec")
        settle_delay = float(_sd if _sd is not None else 8)
        if settle_delay > 0:
            logger.debug(f"[SWEEP] 等待链上结算 {settle_delay}s ...")
            time.sleep(settle_delay)

        try:
            residual = self._refresh_and_get_token_balance(client, token_id)
            if residual <= min_sweep:
                return 0.0, max(residual, 0.0)

            logger.warning(
                f"[SWEEP] 检测到残余 {residual:.4f} shares，启动清扫 | "
                f"token={token_id[:16]}..."
            )

            from py_clob_client_v2.clob_types import OrderArgs
            from py_clob_client_v2.order_builder.constants import SELL

            sweep_size = math.floor(residual * 100) / 100
            if sweep_size <= 0:
                logger.info(f"[SWEEP] 残余 {residual:.6f} shares 截断后为 0，跳过")
                return 0.0, residual

            # T56/T68: min_order_size 预检查——避免向 CLOB 提交必定被拒的小额清扫单
            try:
                from src.utils.orderbook_history import get_min_order_size as _get_mos

                _sweep_min_order = _get_mos(token_id)
                if _sweep_min_order is None:
                    _sweep_min_order = float(
                        get("executor.min_order_size_shares") or 5.0
                    )
                if sweep_size < _sweep_min_order:
                    logger.warning(
                        f"[SWEEP] 残余 {sweep_size:.2f} < min_order_size "
                        f"{_sweep_min_order}，CLOB 无法直接卖出 | "
                        f"将在下次周期由 SellRecovery 处理（补买+卖出）"
                    )
                    return 0.0, residual
            except Exception:
                pass  # 查询失败不阻塞，仍尝试清扫

            # 以 last_price 的 80% 定价确保成交
            sweep_price = round(max(last_price * 0.8, 0.01), 4)
            order_args = OrderArgs(
                token_id=token_id,
                price=sweep_price,
                size=sweep_size,
                side=SELL,
            )
            resp = client.create_and_post_order(order_args)
            sweep_order_id = (
                resp.get("orderID", "") if isinstance(resp, dict) else str(resp)
            )
            logger.info(
                f"[SWEEP] 清扫单已提交 | {sweep_size:.2f} shares "
                f"@ {sweep_price:.4f} orderID={sweep_order_id}"
            )

            sweep_fill = self._wait_for_fill(sweep_order_id, sweep_size * sweep_price)
            if sweep_fill["filled_usdc"] > 0:
                logger.info(f"[SWEEP] 清扫成交 {sweep_fill['filled_usdc']:.4f} USDC")
                # 最终检查残余
                final = self._refresh_and_get_token_balance(client, token_id)
                return sweep_fill["filled_usdc"], max(final, 0.0)
            else:
                logger.warning(f"[SWEEP] 清扫未成交 status={sweep_fill['status']}")
                return 0.0, residual
        except Exception as exc:
            logger.warning(f"[SWEEP] 清扫异常（不影响原始卖出）: {exc}")
            # 查一次真实残余量
            try:
                final = self._refresh_and_get_token_balance(client, token_id)
                return 0.0, max(final, 0.0)
            except Exception:
                return 0.0, -1.0  # -1 表示查询失败，调用方应保守处理

    # ------------------------------------------------------------------
    # CLOB 余额缓存刷新
    # ------------------------------------------------------------------

    def _refresh_collateral_balance(self, client) -> None:
        """刷新 CLOB 服务器的 USDC (collateral) 余额缓存。"""
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=2,
            )
            client.update_balance_allowance(params)
            logger.debug("[BUY] 已刷新 USDC 余额缓存")
        except Exception as exc:
            logger.debug(f"[BUY] 刷新 USDC 余额缓存失败（非致命）: {exc}")

    def _get_usdc_balance_raw(self, client) -> float:
        """查询平台 USDC 余额（6位小数精度），用于余额差计算实际成交金额。"""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=2
        )
        result = client.get_balance_allowance(params)
        raw = int(result.get("balance", "0") or "0")
        return raw / 1_000_000

    # ------------------------------------------------------------------
    # Conditional Token 余额刷新与查询
    # ------------------------------------------------------------------

    def _refresh_and_get_token_balance(self, client, token_id: str) -> float:
        """刷新 CLOB 服务器的 conditional token 余额缓存，并返回实际持有的 shares 数量。

        CLOB 服务器维护余额缓存，不会自动感知链上变更。
        在提交 SELL 订单前必须调用 update_balance_allowance 刷新缓存。

        Returns:
            实际持有的 shares 数量（浮点数），失败时返回 -1.0
        """
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
            signature_type=2,
        )

        # 1. 通知 CLOB 刷新缓存
        try:
            client.update_balance_allowance(params)
            logger.debug(f"[SELL] 已刷新 token 余额缓存: {token_id[:16]}...")
        except Exception as exc:
            logger.warning(f"[SELL] 刷新 token 余额缓存失败: {exc}")

        # 2. 查询实际余额
        try:
            result = client.get_balance_allowance(params)
            raw_balance = int(result.get("balance", "0") or "0")
            # CLOB API 返回 "allowances": {"0xAddr": "value", ...} 格式
            allowances_dict = result.get("allowances") or {}
            has_allowance = (
                any(int(v or "0") > 0 for v in allowances_dict.values())
                if isinstance(allowances_dict, dict)
                else False
            )
            # Conditional tokens 使用 10^6 精度（与 USDC 相同）
            actual_shares = raw_balance / 1_000_000
            logger.info(
                f"[SELL] token 余额: {actual_shares:.4f} shares, "
                f"allowance: {'✓ approved' if has_allowance else '✗ none'} "
                f"({len(allowances_dict)} operators) | {token_id[:16]}..."
            )
            if not has_allowance and actual_shares > 0:
                logger.warning(
                    f"[SELL] allowance 为 0 但有余额 {actual_shares:.4f}，"
                    f"可能需要链上 setApprovalForAll"
                )
            return actual_shares
        except Exception as exc:
            logger.warning(f"[SELL] 查询 token 余额失败: {exc}")
            return -1.0

    # ------------------------------------------------------------------
    # 成交确认（GTC 限价单提交后轮询）
    # ------------------------------------------------------------------

    def _wait_for_fill(
        self, order_id: str, expected_usdc: float, timeout_override: float | None = None
    ) -> dict:
        """
        轮询 CLOB 订单状态，等待成交确认。

        Args:
            timeout_override: 若非 None，覆盖 config 中的 fill_timeout_sec（用于 Maker 长等待）

        Returns:
            {"status": str, "filled_usdc": float, "matched_shares": float}
            status: MATCHED | PARTLY_MATCHED | CANCELLED | EXPIRED | TIMEOUT_CANCELLED | ERROR
        """
        timeout_sec = (
            timeout_override
            if timeout_override is not None
            else (get("executor.fill_timeout_sec") or 45)
        )
        poll_interval = get("executor.fill_poll_interval_sec") or 5

        if not order_id:
            return {"status": "ERROR", "filled_usdc": 0.0, "matched_shares": 0.0}

        client = self._get_clob_client()
        deadline = time.monotonic() + timeout_sec

        while time.monotonic() < deadline:
            try:
                order = client.get_order(order_id)
                status = (
                    (order.get("status") or "").upper()
                    if isinstance(order, dict)
                    else ""
                )
                size_matched = (
                    float(order.get("size_matched", 0))
                    if isinstance(order, dict)
                    else 0.0
                )
                price = float(order.get("price", 0)) if isinstance(order, dict) else 0.0
                filled_usdc = size_matched * price if price > 0 else size_matched

                if status == "MATCHED":
                    return {
                        "status": "MATCHED",
                        "filled_usdc": filled_usdc or expected_usdc,
                        "matched_shares": size_matched,
                    }

                if status in ("CANCELLED", "EXPIRED"):
                    return {
                        "status": status,
                        "filled_usdc": filled_usdc,
                        "matched_shares": size_matched,
                    }

                if status == "PARTLY_MATCHED" and filled_usdc > 0:
                    # 部分成交 — 继续等待直至超时
                    pass

                logger.debug(
                    f"[fill] 等待成交 {order_id} status={status} "
                    f"matched={size_matched} elapsed={timeout_sec - (deadline - time.monotonic()):.0f}s"
                )
            except Exception as exc:
                logger.warning(f"[fill] 查询订单 {order_id} 失败: {exc}")

            time.sleep(poll_interval)

        # 超时 — 尝试取消剩余未成交部分
        logger.warning(f"[fill] 订单 {order_id} 超时 {timeout_sec}s，尝试取消")
        try:
            # 最后查一次状态（可能刚成交）
            order = client.get_order(order_id)
            status = (
                (order.get("status") or "").upper() if isinstance(order, dict) else ""
            )
            size_matched = (
                float(order.get("size_matched", 0)) if isinstance(order, dict) else 0.0
            )
            price = float(order.get("price", 0)) if isinstance(order, dict) else 0.0
            filled_usdc = size_matched * price if price > 0 else size_matched

            if status == "MATCHED":
                return {
                    "status": "MATCHED",
                    "filled_usdc": filled_usdc or expected_usdc,
                    "matched_shares": size_matched,
                }

            # 仍未完全成交 → 取消
            if status in ("LIVE", "PARTLY_MATCHED"):
                client.cancel(order_id)
                logger.info(f"[fill] 已取消超时订单 {order_id}")

            if filled_usdc > 0:
                return {
                    "status": "TIMEOUT_PARTIAL",
                    "filled_usdc": filled_usdc,
                    "matched_shares": size_matched,
                }
            return {
                "status": "TIMEOUT_CANCELLED",
                "filled_usdc": 0.0,
                "matched_shares": 0.0,
            }
        except Exception as exc:
            logger.error(f"[fill] 取消订单 {order_id} 失败: {exc}")
            return {
                "status": "TIMEOUT_CANCEL_FAILED",
                "filled_usdc": 0.0,
                "matched_shares": 0.0,
            }

    def cancel_stale_orders(self) -> int:
        """
        取消所有 LIVE 挂单（系统启动或定期清理时调用）。
        Returns: 取消的订单数
        """
        dry_run = is_dry_run()
        if dry_run:
            return 0
        try:
            client = self._get_clob_client()
            resp = client.cancel_all()
            cancelled = resp.get("canceled", []) if isinstance(resp, dict) else []
            count = len(cancelled) if isinstance(cancelled, list) else 0
            if count > 0:
                logger.info(f"[cleanup] 已取消 {count} 笔挂单")
            return count
        except Exception as exc:
            logger.error(f"[cleanup] 取消挂单失败: {exc}")
            return 0

    def _slippage_ok(self, signal: Signal, actual_price: float) -> bool:
        threshold = get("executor.slippage_threshold") or 0.02
        expected_price = self._market_price(signal)
        slippage = abs(actual_price - expected_price)
        return slippage <= threshold

    def _market_price(self, signal: Signal) -> float:
        """返回信号方向的基准价格（用于滑点计算）。"""
        return getattr(signal, "expected_price", 0.0) or 0.5
        return slippage <= threshold

    def _market_price(self, signal: Signal) -> float:
        """返回信号方向的基准价格（用于滑点计算）。"""
        return getattr(signal, "expected_price", 0.0) or 0.5
        return slippage <= threshold

    def _market_price(self, signal: Signal) -> float:
        """返回信号方向的基准价格（用于滑点计算）。"""
        return getattr(signal, "expected_price", 0.0) or 0.5
        return slippage <= threshold

    def _market_price(self, signal: Signal) -> float:
        """返回信号方向的基准价格（用于滑点计算）。"""
        return getattr(signal, "expected_price", 0.0) or 0.5
        return slippage <= threshold

    def _market_price(self, signal: Signal) -> float:
        """返回信号方向的基准价格（用于滑点计算）。"""
        return getattr(signal, "expected_price", 0.0) or 0.5
        return slippage <= threshold

    def _market_price(self, signal: Signal) -> float:
        """返回信号方向的基准价格（用于滑点计算）。"""
        return getattr(signal, "expected_price", 0.0) or 0.5

    def _market_price(self, signal: Signal) -> float:
        """返回信号方向的基准价格（用于滑点计算）。"""
        return getattr(signal, "expected_price", 0.0) or 0.5

    def _market_price(self, signal: Signal) -> float:
        """返回信号方向的基准价格（用于滑点计算）。"""
        return getattr(signal, "expected_price", 0.0) or 0.5

    def _market_price(self, signal: Signal) -> float:
        """返回信号方向的基准价格（用于滑点计算）。"""
        return getattr(signal, "expected_price", 0.0) or 0.5
