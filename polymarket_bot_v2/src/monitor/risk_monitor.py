"""
risk_monitor.py — Risk & Monitor 模块
职责：监控余额、日亏损、累计亏损、类别连亏；触发暂停、提现告警
智能暂停：小余额自适应阈值 + 绝对最小损失地板 + 每日自动恢复
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timezone

from src.executor.order_executor import OrderResult
from src.utils.category_detector import detect_category
from src.utils.config_loader import get

logger = logging.getLogger(__name__)


class RiskMonitor:
    """
    维护运行时风控状态，并在每次执行后调用 check() 更新。

    状态：
        _day_pnl       今日盈亏（USDC）
        _cum_loss      累计亏损（USDC）
        _balance       最新余额快照
        _peak_balance  历史最高余额（用于回撤计算）
        _category_loss 各赛道连亏次数
        _paused_cats   已暂停赛道集合
        _globally_paused 全局暂停标志
        _pause_reason  暂停原因（'daily'/'cumulative'/None），用于自动恢复判断
    """

    def __init__(
        self,
        initial_balance: float = 0.0,
        peak_balance_from_db: float = 0.0,
    ) -> None:
        self._balance: float = initial_balance
        self._initial_balance: float = initial_balance
        # peak_balance: 优先使用 DB 历史最高值，防止重启后丢失
        self._peak_balance: float = max(initial_balance, peak_balance_from_db)
        # NLV (Net Liquidation Value) = 可用余额 + 持仓市值
        # 用于风控百分比计算，避免将投注部署资金误计为亏损
        self._nlv: float = initial_balance
        self._peak_nlv: float = max(initial_balance, peak_balance_from_db)
        self._nlv_available: bool = False  # True when NLV explicitly provided
        self._day_pnl: float = 0.0
        self._day_date: date = datetime.now(timezone.utc).date()
        self._cum_loss: float = 0.0
        self._category_loss: dict[str, int] = defaultdict(int)
        self._paused_cats: set[str] = set()
        self._globally_paused: bool = False
        self._pause_reason: str | None = None  # 'daily' / 'cumulative' / None
        # DB 来源的实际已结算盈亏 — None 时回退到余额变动追踪（兼容测试）
        self._day_realized_pnl: float | None = None
        self._cum_realized_loss: float | None = None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def update_balance(self, new_balance: float, nlv: float | None = None) -> None:
        """更新余额快照，计算盈亏。

        Args:
            new_balance: CLOB 可用余额（free cash）
            nlv: 净清算价值（free cash + 持仓市值）。若为 None，回退到 new_balance。
        """
        effective_nlv = nlv if nlv is not None else new_balance
        if nlv is not None:
            self._nlv_available = True

        if self._balance == 0.0:
            self._balance = new_balance
            self._nlv = effective_nlv
            self._peak_balance = max(self._peak_balance, new_balance)
            self._peak_nlv = max(self._peak_nlv, effective_nlv)
            return

        change = new_balance - self._balance
        nlv_change = effective_nlv - self._nlv
        self._balance = new_balance
        self._nlv = effective_nlv
        self._peak_balance = max(self._peak_balance, new_balance)
        self._peak_nlv = max(self._peak_nlv, effective_nlv)

        # 跨天重置当日盈亏 + 每日自动恢复（仅限 daily 触发的暂停）
        today = datetime.now(timezone.utc).date()
        if today != self._day_date:
            self._day_pnl = 0.0
            self._day_date = today
            if self._globally_paused and self._pause_reason == "daily":
                self._globally_paused = False
                self._pause_reason = None
                logger.info("[风控] 跨日自动恢复（昨日日亏暂停已清除）")
                self._persist_pause_state()

        # 使用 NLV 变化追踪盈亏（避免将投注部署资金误计为亏损）
        # 当 nlv=None 时，nlv_change == change（完全向后兼容）
        # T179-A: 检测可能的充值/提现（大额余额变动且NLV同步等量变动）
        _deposit_threshold = float(get("risk.deposit_detection_threshold_usdc") or 15.0)
        if change > _deposit_threshold and abs(change - nlv_change) < 1.0:
            # free cash 和 NLV 同步大幅增加 → 极可能是充值（非交易盈利）
            logger.info(
                f"[风控] 检测到可能的充值/转入: +${change:.2f} USDC "
                f"(NLV变化: {nlv_change:+.2f})，不影响盈亏统计"
            )
        elif change < -_deposit_threshold and abs(change - nlv_change) < 1.0:
            # free cash 和 NLV 同步大幅减少 → 极可能是提现
            logger.info(
                f"[风控] 检测到可能的提现/转出: ${change:.2f} USDC "
                f"(NLV变化: {nlv_change:+.2f})，不影响盈亏统计"
            )
        self._day_pnl += nlv_change
        if nlv_change < 0:
            self._cum_loss += abs(nlv_change)

        # 优先显示 DB 来源的已结算盈亏（与仪表盘"今日 P&L"一致）
        # _day_pnl（NLV累计变化）作为次要参考，包含未实现浮动盈亏
        if self._day_realized_pnl is not None:
            pnl_info = (
                f"今日盈亏: {self._day_realized_pnl:+.2f}(已结算) "
                f"NLV变化: {self._day_pnl:+.2f}"
            )
        else:
            pnl_info = f"今日NLV变化: {self._day_pnl:+.2f}"
        logger.info(
            f"余额更新: {new_balance:.2f} USDC (NLV: {effective_nlv:.2f}) | "
            f"{pnl_info} | 峰值NLV: {self._peak_nlv:.2f}"
        )

    def _get_smart_thresholds(self) -> tuple[float, float, float, float]:
        """返回 (daily_pct, cum_pct, min_daily_abs, min_cum_abs)。

        支持 `risk.smart_pause` 小余额自适应阈值：
        - enabled=true 时，优先读取 smart_pause 的绝对金额地板
        - 当当前 NLV/余额 ≤ small_balance_threshold_usdc 时，改用更宽松的小余额百分比阈值
        """
        daily_pct = float(get("risk.daily_loss_pause_pct") or 0.50)
        cum_pct = float(get("risk.cumulative_loss_pause_pct") or 0.40)
        min_daily_abs = float(get("risk.min_absolute_daily_loss_usdc") or 5.0)
        min_cum_abs = float(get("risk.min_absolute_cumulative_loss_usdc") or 10.0)

        smart_cfg = get("risk.smart_pause") or {}
        if smart_cfg.get("enabled"):
            min_daily_abs = float(
                smart_cfg.get("min_absolute_daily_loss_usdc", min_daily_abs)
            )
            min_cum_abs = float(
                smart_cfg.get("min_absolute_cumulative_loss_usdc", min_cum_abs)
            )
            small_balance_threshold = float(
                smart_cfg.get("small_balance_threshold_usdc") or 0.0
            )
            balance_ref = self._nlv if self._nlv > 0 else self._balance
            if small_balance_threshold > 0 and balance_ref <= small_balance_threshold:
                daily_pct = float(
                    smart_cfg.get("small_balance_daily_pause_pct", daily_pct)
                )
                cum_pct = float(
                    smart_cfg.get("small_balance_cumulative_pause_pct", cum_pct)
                )

        return daily_pct, cum_pct, min_daily_abs, min_cum_abs

    def set_day_realized_pnl(self, realized_pnl: float) -> None:
        """设置今日已结算的实际盈亏（从 DB 读取，正值=盈利，负值=亏损）。

        调用后 check() 将使用此值而非余额变动累积的 _day_pnl，
        彻底消除「开仓部署资金」被误计为当日亏损的问题。
        """
        self._day_realized_pnl = float(realized_pnl)

    def set_cum_realized_loss(self, total_loss: float) -> None:
        """设置历史全部已结算的累计亏损（从 DB 读取，正数）。"""
        self._cum_realized_loss = float(total_loss)

    def check(self, results: list["OrderResult"]) -> list[str]:
        """
        检查执行结果，返回告警消息列表；触发暂停时设置对应标志。
        智能暂停：同时满足 百分比阈值 AND 绝对最小损失金额 才触发暂停。

        日亏损 & 累计亏损优先使用 DB 来源的已结算数据（set_day_realized_pnl /
        set_cum_realized_loss 注入），避免将开仓部署资金误算为亏损。
        若未注入（None），则回退到余额变动追踪（兼容 DRY RUN / 单元测试）。
        """
        alerts: list[str] = []
        daily_pct, cum_pct, min_daily_abs, min_cum_abs = self._get_smart_thresholds()

        # 余额绝对底线检查（独立于损失%：余额太少无法正常运营）
        _floor = float(get("risk.balance_floor_usdc") or 0.5)
        if self._balance < _floor and not self._globally_paused:
            self._globally_paused = True
            self._pause_reason = "balance_floor"
            msg = f"[风控] 余额 ${self._balance:.2f} < 地板 ${_floor:.2f}，暂停所有投注"
            logger.warning(msg)
            alerts.append(msg)

        # 1. 日亏损暂停（百分比 + 绝对金额双重门槛）
        # 优先使用 DB 已结算数据；回退到余额变动累积（DRY RUN / 测试）
        # 分母使用 NLV（净清算价值），避免投注部署资金导致余额偏低、百分比虚高
        if self._day_realized_pnl is not None:
            daily_loss = max(0.0, -self._day_realized_pnl)  # 只计亏损，部署资金不算
        else:
            daily_loss = max(0.0, -self._day_pnl)
        denom = self._nlv if self._nlv > 0 else self._balance
        if (
            denom > 0
            and daily_loss >= min_daily_abs
            and (daily_loss / denom) >= daily_pct
        ):
            # 仅在首次触发暂停时生成告警，避免每个 check() 周期重复告警
            first_trigger = not self._globally_paused
            if first_trigger:
                self._globally_paused = True
                self._pause_reason = "daily"
                source_tag = (
                    "(DB结算)" if self._day_realized_pnl is not None else "(余额追踪)"
                )
                msg = (
                    f"[风控] 今日亏损 {daily_loss:.2f} USDC "
                    f"({daily_loss/denom:.1%}) 已触发暂停阈值 "
                    f"(threshold: {daily_pct:.0%}, min: ${min_daily_abs:.1f}) {source_tag}"
                )
                logger.warning(msg)
                alerts.append(msg)

        # 2. 累计亏损暂停（百分比 + 绝对金额双重门槛）
        # NLV 可用时使用 NLV 回撤（peak_nlv - nlv）：标准最大回撤指标，
        # 自动包含未实现持仓价值，避免将投注部署资金误计为亏损。
        # NLV 不可用时（DRY RUN / 测试）：回退到 DB 已结算或余额变动追踪。
        if self._nlv_available:
            cum_loss = max(0.0, self._peak_nlv - self._nlv)
            peak_ref = self._peak_nlv
        else:
            cum_loss = (
                self._cum_realized_loss
                if self._cum_realized_loss is not None
                else self._cum_loss
            )
            peak_ref = self._peak_balance
        if (
            peak_ref > 0
            and cum_loss >= min_cum_abs
            and (cum_loss / peak_ref) >= cum_pct
        ):
            # 仅在非 cumulative 暂停状态下生成告警（避免重复）
            was_cum_paused = (
                self._globally_paused and self._pause_reason == "cumulative"
            )
            self._globally_paused = True
            self._pause_reason = "cumulative"  # 覆盖 daily，更严重
            if not was_cum_paused:
                if self._nlv_available:
                    source_tag = "(NLV回撤)"
                elif self._cum_realized_loss is not None:
                    source_tag = "(DB结算)"
                else:
                    source_tag = "(余额追踪)"
                msg = (
                    f"[风控] 累计回撤 {cum_loss:.2f} USDC "
                    f"({cum_loss/peak_ref:.1%}) 已触发全局暂停 "
                    f"(threshold: {cum_pct:.0%}, min: ${min_cum_abs:.1f}, "
                    f"peak: ${peak_ref:.2f}) {source_tag}"
                )
                logger.warning(msg)
                alerts.append(msg)

        # 3. 提现告警（基于 DB 已结算盈亏，而非余额差）
        #    T179-A修复: 余额差 = balance - initial 会将用户充值误判为交易利润
        #    使用 DB 已结算 P&L 则不受充值/提现影响
        withdraw_trigger: float = get("risk.withdraw_trigger_pct") or 0.30
        try:
            from src.utils.db import get_total_realized_pnl

            db_realized_pnl = get_total_realized_pnl()
            profit = max(db_realized_pnl, 0.0)
            profit_source = "(DB结算)"
        except Exception:
            # DB 不可用时回退到余额差（保留旧逻辑作为降级）
            profit = max(self._balance - self._initial_balance, 0.0)
            profit_source = "(余额追踪-降级)"
        if (
            self._initial_balance > 0
            and (profit / self._initial_balance) >= withdraw_trigger
        ):
            msg = (
                f"[提现] 利润 {profit:.2f} USDC ({profit/self._initial_balance:.1%}) "
                f"已达提现阈值，请人工提现！ {profit_source}"
            )
            logger.info(msg)
            alerts.append(msg)

        # 4. 类别连亏暂停
        # [Critical Fix] 基于交易亏损(pnl/is_win)统计连亏，而非执行状态(success)
        # DRY RUN 模拟单不计连亏（无真实盈亏数据）
        loss_streak_limit: int = get("risk.category_pause_on_loss_streak") or 3
        for result in results:
            category = self._infer_category(result.signal.question)
            if result.tx_hash == "DRY_RUN":
                # DRY RUN 模拟单：不影响连亏计数
                continue
            elif not result.success:
                # 执行失败（滑点/CLOB拒绝）：记录但不计入交易连亏
                logger.debug(
                    f"赛道 '{category}' 下单执行失败（非交易亏损），不计入连亏"
                )
            elif hasattr(result, "pnl") and result.pnl is not None:
                # 有明确盈亏数据：基于实际交易结果判断连亏
                if result.pnl < 0:
                    self._category_loss[category] += 1
                    logger.debug(
                        f"赛道 '{category}' 交易亏损 {result.pnl:.2f}，连亏计数: {self._category_loss[category]}"
                    )
                else:
                    self._category_loss[category] = 0
            else:
                # 实盘成功但无 pnl 数据（新下单尚未结算）：重置连亏计数
                self._category_loss[category] = 0

        for cat, streak in list(self._category_loss.items()):
            if streak >= loss_streak_limit and cat not in self._paused_cats:
                self._paused_cats.add(cat)
                msg = f"[风控] 赛道 '{cat}' 连亏 {streak} 次，已暂停该赛道！"
                logger.warning(msg)
                alerts.append(msg)

        # Telegram 通知所有风控告警
        if alerts:
            from src.utils.telegram_notifier import get_notifier

            for alert in alerts:
                get_notifier().notify_risk_alert(alert)

        # 持久化暂停状态（确保重启后风控不失效）
        self._persist_pause_state()

        return alerts

    @property
    def globally_paused(self) -> bool:
        return self._globally_paused

    @property
    def paused_categories(self) -> set[str]:
        return self._paused_cats

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def nlv(self) -> float:
        return self._nlv

    @property
    def pause_reason(self) -> str | None:
        return self._pause_reason

    def resume(self) -> None:
        """手动恢复（需人工调用）。

        重置 peak_nlv / peak_balance 到当前值，防止 check() 立即
        因历史回撤再次触发暂停。同时将新 peak 持久化到 pause_state，
        使 bot 重启后也不会从 balance_log 恢复旧 peak 再次暂停。
        """
        old_peak = self._peak_nlv
        self._globally_paused = False
        self._pause_reason = None
        self._paused_cats.clear()
        # 重置 peak 到当前 NLV/余额，防止即时重触发
        current = self._nlv if self._nlv > 0 else self._balance
        self._peak_nlv = current
        self._peak_balance = self._balance
        # 重置日亏损计数
        self._day_pnl = 0.0
        self._day_realized_pnl = None
        self._cum_loss = 0.0
        self._cum_realized_loss = None
        logger.info(f"风控已手动恢复 | peak_nlv {old_peak:.2f} → {current:.2f}")
        self._persist_pause_state()

    def check_auto_recovery(self, current_balance: float | None = None) -> bool:
        """智能自动恢复：检查暂停条件是否已消失，条件满足则自动恢复。

        适用场景：
          - balance_floor：余额恢复到地板值以上（含10%缓冲）→ 自动恢复
          - daily：UTC跨日（已在update_balance处理）→ 此处兜底检查
          - cumulative：累计损失恢复到阈值以下 → 自动恢复（重置peak）

        绝不自动恢复的场景：
          - emergency_freeze（紧急冻结）— 必须用户人工解除
          - 用户手动 pause()（pause_reason 为 None 或 "manual"）

        Args:
            current_balance: 当前余额（若为None则使用内部缓存的_balance）

        Returns:
            True 如果成功自动恢复，False 否则
        """
        if not self._globally_paused:
            return False

        # 自动恢复总开关
        if not bool(
            get("risk.auto_recovery_enabled")
            if get("risk.auto_recovery_enabled") is not None
            else True
        ):
            return False

        reason = self._pause_reason or ""
        bal = current_balance if current_balance is not None else self._balance

        # 紧急冻结：绝不自动恢复
        if bool(get("risk.emergency_freeze")):
            return False

        # 手动暂停（无reason或reason=manual）：不自动恢复
        if reason in ("", "manual"):
            return False

        recovered = False
        recovery_buffer = float(get("risk.auto_recovery_buffer_pct") or 0.10)

        if reason == "balance_floor":
            floor_val = float(get("risk.balance_floor_usdc") or 0.5)
            threshold = floor_val * (1.0 + recovery_buffer)
            if bal >= threshold:
                recovered = True
                logger.info(
                    f"[风控自动恢复] balance_floor 条件已解除 | "
                    f"余额 ${bal:.2f} >= 地板+缓冲 ${threshold:.2f} "
                    f"(地板=${floor_val:.2f}, 缓冲={recovery_buffer:.0%})"
                )

        elif reason == "daily":
            # 跨日检查（update_balance已有处理，此处兜底）
            today = datetime.now(timezone.utc).date()
            if today != self._day_date:
                recovered = True
                self._day_pnl = 0.0
                self._day_date = today
                logger.info("[风控自动恢复] daily_loss 条件已解除 | UTC跨日重置")

        elif reason == "cumulative":
            # 累计损失恢复：当前NLV与峰值的回撤低于阈值
            _, cum_pct, _, min_cum_abs = self._get_smart_thresholds()
            if self._nlv_available:
                cum_loss = max(0.0, self._peak_nlv - self._nlv)
                peak_ref = self._peak_nlv
            else:
                cum_loss = (
                    self._cum_realized_loss
                    if self._cum_realized_loss is not None
                    else self._cum_loss
                )
                peak_ref = self._peak_balance

            # 恢复条件：回撤低于阈值的80%（留安全余量）
            recovery_cum_pct = cum_pct * 0.8
            if peak_ref > 0 and (
                cum_loss < min_cum_abs or (cum_loss / peak_ref) < recovery_cum_pct
            ):
                recovered = True
                logger.info(
                    f"[风控自动恢复] cumulative_loss 条件已解除 | "
                    f"回撤 ${cum_loss:.2f} ({cum_loss/peak_ref:.1%}) "
                    f"< 恢复阈值 {recovery_cum_pct:.0%}"
                )

        if recovered:
            old_reason = self._pause_reason
            old_peak = self._peak_nlv
            self._globally_paused = False
            self._pause_reason = None
            # balance_floor 和 daily 恢复后重置 peak 到当前值（防止即时重触发）
            if old_reason in ("balance_floor", "daily"):
                current = self._nlv if self._nlv > 0 else bal
                self._peak_nlv = current
                self._peak_balance = bal
            self._persist_pause_state()

            # Telegram 通知
            try:
                from src.utils.telegram_notifier import get_notifier

                get_notifier().notify_risk_alert(
                    f"✅ [风控自动恢复] 暂停原因 '{old_reason}' 条件已解除，"
                    f"系统恢复正常投注。余额: ${bal:.2f}"
                )
            except Exception:
                pass

            logger.info(
                f"[风控自动恢复] 成功恢复 | 原因: {old_reason} | "
                f"余额: ${bal:.2f} | peak: {old_peak:.2f} → {self._peak_nlv:.2f}"
            )
            return True

        return False

    def restore_pause_state(self, state: dict[str, str]) -> None:
        """从 DB pause_state 表恢复暂停标志，防止重启后风控失效。

        在 _get_components() 初始化 RiskMonitor 后立即调用。
        """
        if not state:
            return
        try:
            self._globally_paused = (
                state.get("globally_paused", "false").lower() == "true"
            )
            self._pause_reason = state.get("pause_reason") or None
            cats_str = state.get("paused_cats", "")
            self._paused_cats = set(c for c in cats_str.split(",") if c.strip())
            # 恢复 peak_nlv_override（resume() 重置后的 peak）
            peak_override = state.get("peak_nlv_override")
            if peak_override:
                try:
                    override_val = float(peak_override)
                    if 0 < override_val < self._peak_nlv:
                        self._peak_nlv = override_val
                        self._peak_balance = min(self._peak_balance, override_val)
                        logger.info(
                            f"[风控恢复] peak_nlv 从 DB override 恢复: {override_val:.2f}"
                        )
                except ValueError:
                    pass
            # 恢复各赛道连亏计数
            for key, val in state.items():
                if key.startswith("streak_"):
                    cat = key[len("streak_") :]
                    try:
                        self._category_loss[cat] = int(val)
                    except ValueError:
                        pass
            logger.info(
                f"[风控恢复] globally_paused={self._globally_paused} "
                f"reason={self._pause_reason} "
                f"paused_cats={self._paused_cats} "
                f"streaks={ {k: v for k, v in self._category_loss.items()} }"
            )
        except Exception as e:
            logger.warning(f"[风控恢复] 状态恢复失败（将从零开始）: {e}")

    def _persist_pause_state(self) -> None:
        """将当前暂停状态持久化到 DB，供重启后恢复。"""
        try:
            from src.utils.db import save_pause_state

            save_pause_state("globally_paused", str(self._globally_paused).lower())
            save_pause_state("pause_reason", self._pause_reason or "")
            save_pause_state("paused_cats", ",".join(sorted(self._paused_cats)))
            # 持久化 peak_nlv：resume() 重置后需要保存，
            # 否则重启会从 balance_log MAX 恢复旧 peak 再次触发暂停
            save_pause_state("peak_nlv_override", f"{self._peak_nlv:.4f}")
            for cat, streak in self._category_loss.items():
                save_pause_state(f"streak_{cat}", str(streak))
        except Exception as e:
            logger.debug(f"[风控持久化] 写入 DB 失败（不影响运行时）: {e}")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _infer_category(self, question: str) -> str:
        """使用共享 detect_category 推断赛道，确保与其他模块一致。"""
        return detect_category({"question": question})
