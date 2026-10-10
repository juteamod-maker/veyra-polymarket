"""
strategy_evolver.py — 自我进化策略优化器

核心架构（灵感来源）：
  - skharchikov: Bayesian 概率更新 + 校准曲线（10桶Laplace平滑）
  - resided: 信号源权重自学习（MAE驱动EMA更新）
  - ImMike: 机会时序分析 + 冷却机制

三大组件：
  1. CalibrationEngine — 10桶概率校准，修正AI系统性偏差
  2. ModelPerformanceTracker — 跟踪各模型MAE/Brier，自动调权重
  3. StrategyEvolver — 综合分析，自动调参（shrinkage/min_ev/bonus等）

安全设计：
  - 所有参数调整有硬上下界（EVOLUTION_BOUNDS）
  - 单次调整幅度限制（MAX_CHANGE_PCT = 20%）
  - runtime_overrides 持久化到 DB，重启自动恢复，不修改 config.json
  - 可通过 config evolution.enabled=false 完全禁用
  - 最少样本量门槛（默认30笔）才启动进化
"""

import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.utils import db
from src.utils.config_loader import get

logger = logging.getLogger(__name__)

NUM_BINS = 10
BIN_WIDTH = 1.0 / NUM_BINS

# 参数调整的硬上下界
EVOLUTION_BOUNDS: dict[str, tuple[float, float]] = {
    "signal.probability_calibration.shrinkage_factor": (0.40, 0.95),
    "signal.min_ev_threshold": (0.005, 0.10),
    "signal.probability_calibration.probability_ceiling": (0.85, 0.98),
    "signal.probability_calibration.probability_floor": (0.02, 0.15),
}

# 赛道bonus调整范围
CATEGORY_BONUS_BOUNDS = (0.5, 2.0)

# 单次调整最大幅度（相对当前值）
MAX_CHANGE_PCT = 0.20


@dataclass
class CalibrationBin:
    """一个概率桶的校准数据。"""

    bin_index: int
    total: int = 0
    wins: int = 0
    midpoint: float = 0.0

    @property
    def calibrated_rate(self) -> float:
        """Laplace平滑后的实际胜率。"""
        if self.total == 0:
            return self.midpoint
        return (self.wins + 1) / (self.total + 2)


@dataclass
class CalibrationCurve:
    """
    10桶概率校准曲线（灵感: skharchikov/calibration.rs）。
    将AI原始概率映射到历史实际胜率，修正系统性偏差。
    """

    bins: list[CalibrationBin] = field(default_factory=list)
    active: bool = False
    total_samples: int = 0

    def correct(self, raw_prob: float) -> float:
        """校准原始概率。不活跃时返回原值。"""
        if not self.active or not self.bins:
            return raw_prob

        idx = min(int(raw_prob / BIN_WIDTH), NUM_BINS - 1)
        calibrated = self.bins[idx].calibrated_rate

        # 相邻桶插值（skharchikov模式：更平滑）
        bin_center = (idx + 0.5) * BIN_WIDTH
        offset = raw_prob - bin_center
        if offset > 0 and idx + 1 < NUM_BINS:
            next_cal = self.bins[idx + 1].calibrated_rate
            weight = offset / BIN_WIDTH
            calibrated = calibrated + (next_cal - calibrated) * weight
        elif offset < 0 and idx > 0:
            prev_cal = self.bins[idx - 1].calibrated_rate
            weight = -offset / BIN_WIDTH
            calibrated = calibrated + (prev_cal - calibrated) * weight

        return max(0.01, min(0.99, calibrated))

    def bias_direction(self) -> str:
        """判断整体偏差方向：OVERCONFIDENT / UNDERCONFIDENT / BALANCED。"""
        if not self.active:
            return "UNKNOWN"
        diffs = []
        for b in self.bins:
            if b.total >= 3:
                diffs.append(b.calibrated_rate - b.midpoint)
        if not diffs:
            return "UNKNOWN"
        avg_diff = sum(diffs) / len(diffs)
        if avg_diff < -0.03:
            return "OVERCONFIDENT"
        elif avg_diff > 0.03:
            return "UNDERCONFIDENT"
        return "BALANCED"

    def summary(self) -> str:
        """生成人类可读的校准摘要。"""
        if not self.active:
            return f"校准曲线: 未激活 ({self.total_samples} 样本，需要更多数据)"
        lines = [f"校准曲线 ({self.total_samples} 样本) — {self.bias_direction()}:"]
        for b in self.bins:
            lo = b.bin_index * 10
            hi = (b.bin_index + 1) * 10
            expected = b.midpoint * 100
            actual = b.calibrated_rate * 100
            diff = actual - expected
            tag = "✓" if abs(diff) < 3 else ("↓过度自信" if diff < 0 else "↑信心不足")
            lines.append(
                f"  {lo:2d}-{hi:2d}%: 实际={actual:4.0f}% ({diff:+.0f}%) [{b.total}笔] {tag}"
            )
        return "\n".join(lines)


class StrategyEvolver:
    """
    自我进化策略优化器。
    基于累积投注数据自动调整策略参数（shrinkage / min_ev / category_bonus 等）。
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path
        # 运行时参数覆盖（持久化到 evolution_overrides 表，重启自动恢复）
        self.runtime_overrides: dict[str, float] = {}
        # 校准曲线（全局 + 各赛道）
        self.global_calibration = CalibrationCurve()
        self.category_calibrations: dict[str, CalibrationCurve] = {}
        # 模型权重
        self.model_weights: dict[str, float] = {}
        # 上次进化时间
        self._last_evolve_ts: float = 0.0
        self._bets_since_last_evolve: int = 0
        # 从 DB 恢复持久化的 overrides
        self._load_persisted_overrides()
        # 加载历史数据
        self._load_calibration()

    def _db_kw(self) -> dict:
        return {"db_path": self._db_path} if self._db_path else {}

    def _load_persisted_overrides(self) -> None:
        """从 DB 恢复持久化的 runtime_overrides。"""
        persist = (get("evolution") or {}).get("persist_overrides", True)
        if not persist:
            return
        try:
            saved = db.load_evolution_overrides(**self._db_kw())
            if saved:
                self.runtime_overrides.update(saved)
                logger.info(
                    f"[Evolution] 从DB恢复 {len(saved)} 项进化参数: "
                    + ", ".join(f"{k}={v:.4f}" for k, v in saved.items())
                )
        except Exception as e:
            logger.warning(f"[Evolution] 加载持久化overrides失败(非致命): {e}")

    def _persist_override(
        self, param_name: str, new_value: float, old_value: float, reason: str
    ) -> None:
        """持久化单个 override 到 DB。"""
        persist = (get("evolution") or {}).get("persist_overrides", True)
        if not persist:
            return
        try:
            db.save_evolution_override(
                param_name=param_name,
                new_value=new_value,
                old_value=old_value,
                reason=reason,
                **self._db_kw(),
            )
        except Exception as e:
            logger.warning(f"[Evolution] 持久化override失败(非致命): {e}")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        category: str,
        ai_probability: float,
        actual_outcome: int,
        model_votes: dict,
    ) -> None:
        """
        记录一次投注结果（用于校准和模型追踪）。
        由 OutcomeTracker 在结算时调用。

        Args:
            category: 赛道名称
            ai_probability: ensemble后的AI概率 P(YES)
            actual_outcome: 1=YES赢, 0=NO赢
            model_votes: {model_name: {probability, confidence, ...}}
        """
        # 概率桶索引
        bin_idx = min(int(ai_probability / BIN_WIDTH), NUM_BINS - 1)

        # 写入校准表
        db.insert_calibration_point(
            category=category,
            bin_index=bin_idx,
            ai_probability=ai_probability,
            actual_outcome=actual_outcome,
            **self._db_kw(),
        )

        # 写入模型表现表
        for model_name, vote in model_votes.items():
            predicted = float(vote.get("probability", 0.5))
            stated_outcome = vote.get("outcome", "YES")
            # 归一化为 P(YES)
            if stated_outcome == "NO" and predicted > 0.5:
                predicted = 1.0 - predicted
            elif stated_outcome == "YES" and predicted < 0.5:
                predicted = 1.0 - predicted
            db.insert_model_performance(
                model_name=model_name,
                category=category,
                predicted_prob=predicted,
                actual_outcome=actual_outcome,
                **self._db_kw(),
            )

        self._bets_since_last_evolve += 1
        logger.debug(
            f"[Evolution] 记录结果: {category} bin={bin_idx} "
            f"ai_prob={ai_probability:.3f} actual={actual_outcome}"
        )

    def should_evolve(self) -> bool:
        """检查是否应触发一次进化周期。"""
        evo_cfg = get("evolution") or {}
        if not evo_cfg.get("enabled", True):
            return False
        interval_bets = int(evo_cfg.get("interval_bets", 50))
        interval_hours = float(evo_cfg.get("interval_hours", 24))
        min_samples = int(evo_cfg.get("min_samples", 30))

        # 样本量不足
        total = self._count_resolved()
        if total < min_samples:
            return False

        # 投注次数触发
        if self._bets_since_last_evolve >= interval_bets:
            return True

        # 时间触发
        if time.time() - self._last_evolve_ts > interval_hours * 3600:
            return True

        return False

    def evolve(self) -> dict:
        """
        执行一次进化周期：分析数据→计算调整→应用→记录。
        返回调整摘要 dict。
        """
        logger.info("[Evolution] ═══════ 开始进化周期 ═══════")
        self._last_evolve_ts = time.time()
        self._bets_since_last_evolve = 0
        adjustments: list[dict] = []

        # 1. 刷新校准曲线
        self._load_calibration()

        # 2. 刷新模型权重
        self._load_model_weights()

        # 3. 分析并调整 shrinkage
        adj = self._evolve_shrinkage()
        if adj:
            adjustments.append(adj)

        # 4. 分析并调整 min_ev
        adj = self._evolve_min_ev()
        if adj:
            adjustments.append(adj)

        # 5. 分析并调整赛道 bonus
        cat_adjs = self._evolve_category_bonuses()
        adjustments.extend(cat_adjs)

        # 6. 分析并调整概率 ceiling/floor
        adjs = self._evolve_probability_bounds()
        adjustments.extend(adjs)

        # 记录所有调整到DB
        for adj in adjustments:
            db.insert_evolution_log(
                param_name=adj["param"],
                old_value=adj["old"],
                new_value=adj["new"],
                reason=adj["reason"],
                data_points=adj.get("data_points", 0),
                confidence=adj.get("confidence", 0.0),
                **self._db_kw(),
            )
            # T152: 持久化 override 到 evolution_overrides 表
            self._persist_override(
                param_name=adj["param"],
                new_value=adj["new"],
                old_value=adj["old"],
                reason=adj["reason"],
            )

        summary = {
            "ts": time.time(),
            "adjustments": adjustments,
            "calibration_bias": self.global_calibration.bias_direction(),
            "calibration_samples": self.global_calibration.total_samples,
            "model_weights": dict(self.model_weights),
        }
        if adjustments:
            logger.info(
                f"[Evolution] 完成 {len(adjustments)} 项调整: "
                + ", ".join(
                    f"{a['param']}={a['old']:.4f}→{a['new']:.4f}" for a in adjustments
                )
            )
        else:
            logger.info("[Evolution] 分析完成，当前参数无需调整")
        logger.info(
            f"[Evolution] 校准偏差: {summary['calibration_bias']}, 样本: {summary['calibration_samples']}"
        )
        return summary

    def get_calibrated_probability(self, raw_prob: float, category: str = "") -> float:
        """
        获取校准后的概率（供 signal_engine 调用）。
        优先用赛道专用曲线，无则用全局曲线。
        """
        curve = self.category_calibrations.get(category, self.global_calibration)
        if curve.active:
            return curve.correct(raw_prob)
        if self.global_calibration.active:
            return self.global_calibration.correct(raw_prob)
        return raw_prob

    def get_model_weight(self, model_name: str) -> float:
        """获取模型权重（供 signal_engine ensemble 加权用）。"""
        return self.model_weights.get(model_name, 1.0)

    def get_override(self, key: str, default: float = None) -> Optional[float]:
        """获取运行时覆盖值（进化系统调整后的参数）。"""
        return self.runtime_overrides.get(key, default)

    def rollback_override(self, param_name: str) -> Optional[float]:
        """回滚一个进化参数到上一个值。返回回滚后的值，无历史则返回 None。"""
        rolled = db.rollback_evolution_override(param_name, **self._db_kw())
        if rolled is not None:
            self.runtime_overrides[param_name] = rolled
            logger.info(f"[Evolution] 回滚 {param_name} → {rolled:.4f}")
        return rolled

    def get_override_history(
        self, param_name: str = None, limit: int = 20
    ) -> list[dict]:
        """查询进化参数变更历史（审计用）。"""
        return db.list_evolution_override_history(
            param_name=param_name, limit=limit, **self._db_kw()
        )

    def get_status(self) -> dict:
        """获取进化系统状态摘要（供仪表盘使用）。"""
        return {
            "enabled": bool((get("evolution") or {}).get("enabled", True)),
            "global_calibration_active": self.global_calibration.active,
            "global_calibration_samples": self.global_calibration.total_samples,
            "global_calibration_bias": self.global_calibration.bias_direction(),
            "category_calibrations": {
                cat: {
                    "active": curve.active,
                    "samples": curve.total_samples,
                    "bias": curve.bias_direction(),
                }
                for cat, curve in self.category_calibrations.items()
            },
            "model_weights": dict(self.model_weights),
            "runtime_overrides": dict(self.runtime_overrides),
            "bets_since_last_evolve": self._bets_since_last_evolve,
            "last_evolve_ts": self._last_evolve_ts,
        }

    # ------------------------------------------------------------------
    # Bayesian 概率更新（灵感: skharchikov/bayesian.rs）
    # ------------------------------------------------------------------

    @staticmethod
    def bayesian_update(
        market_prob: float,
        assessments: list[tuple[float, float]],
    ) -> tuple[float, float, float]:
        """
        贝叶斯更新：以市场价为先验，各模型提供似然比。

        Args:
            market_prob: 市场中间价 P(YES)
            assessments: [(ai_prob, confidence), ...] — 各模型 P(YES) + 置信度

        Returns:
            (posterior, combined_lr, agreement)
        """
        market_prob = max(0.01, min(0.99, market_prob))
        prior_odds = market_prob / (1.0 - market_prob)
        combined_lr = 1.0
        log_lrs = []
        # 单模型 LR 上限：防止 AI 极端概率摧毁市场先验
        max_lr_per_model = 20.0

        for ai_prob, conf in assessments:
            ai_prob = max(0.01, min(0.99, ai_prob))
            conf = max(0.0, min(1.0, conf))
            # 似然比：AI概率相对于市场概率的 Bayes factor
            lr = (ai_prob / market_prob) * ((1.0 - market_prob) / (1.0 - ai_prob))
            # LR 上限/下限钳位（防止极端爆炸）
            lr = max(1.0 / max_lr_per_model, min(max_lr_per_model, lr))
            # 置信度衰减：LR^confidence（低置信度→LR趋近1.0，即无更新）
            dampened_lr = lr**conf if lr > 0 else 1.0
            combined_lr *= dampened_lr
            if lr > 0:
                log_lrs.append(math.log(lr))

        posterior_odds = prior_odds * combined_lr
        # 合并 LR 上限：即使多模型一致也不让后验偏离过度
        max_combined_lr = max_lr_per_model**2  # 400
        if combined_lr > max_combined_lr:
            combined_lr = max_combined_lr
            posterior_odds = prior_odds * combined_lr
        elif combined_lr < 1.0 / max_combined_lr:
            combined_lr = 1.0 / max_combined_lr
            posterior_odds = prior_odds * combined_lr
        posterior = posterior_odds / (1.0 + posterior_odds)
        posterior = max(0.01, min(0.99, posterior))

        # 一致性度量（skharchikov模式）
        agreement = 1.0
        if len(log_lrs) > 1:
            mean_log = sum(log_lrs) / len(log_lrs)
            variance = sum((l - mean_log) ** 2 for l in log_lrs) / len(log_lrs)
            agreement = math.exp(-math.sqrt(variance))

        return posterior, combined_lr, agreement

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _count_resolved(self) -> int:
        """统计已结算投注总数。"""
        try:
            bets = db.get_resolved_for_calibration(recent_n=10000, **self._db_kw())
            return len(bets)
        except Exception:
            return 0

    def _load_calibration(self) -> None:
        """从DB加载校准曲线。"""
        evo_cfg = get("evolution") or {}
        min_samples = int(evo_cfg.get("min_samples", 30))

        try:
            # 全局校准
            global_bins_data = db.get_calibration_bins(category="", **self._db_kw())
            total = sum(b["total"] for b in global_bins_data)
            self.global_calibration = CalibrationCurve(
                bins=[
                    CalibrationBin(
                        bin_index=b["bin_index"],
                        total=b["total"],
                        wins=b["wins"],
                        midpoint=b["midpoint"],
                    )
                    for b in global_bins_data
                ],
                active=(total >= min_samples),
                total_samples=total,
            )

            # 赛道校准
            categories = [
                "weather",
                "crypto",
                "sports",
                "geopolitics",
                "finance",
                "tech",
                "economy",
            ]
            for cat in categories:
                cat_bins = db.get_calibration_bins(category=cat, **self._db_kw())
                cat_total = sum(b["total"] for b in cat_bins)
                self.category_calibrations[cat] = CalibrationCurve(
                    bins=[
                        CalibrationBin(
                            bin_index=b["bin_index"],
                            total=b["total"],
                            wins=b["wins"],
                            midpoint=b["midpoint"],
                        )
                        for b in cat_bins
                    ],
                    active=(cat_total >= min_samples),
                    total_samples=cat_total,
                )
        except Exception as e:
            logger.warning(f"[Evolution] 加载校准数据失败: {e}")

    def _load_model_weights(self) -> None:
        """从DB加载模型权重（resided风格: weight = 1/(1+MAE*10)）。"""
        try:
            stats = db.get_model_stats(**self._db_kw())
            self.model_weights = {name: s["weight"] for name, s in stats.items()}
            if self.model_weights:
                logger.info(
                    f"[Evolution] 模型权重: "
                    + ", ".join(f"{n}={w:.3f}" for n, w in self.model_weights.items())
                )
        except Exception as e:
            logger.warning(f"[Evolution] 加载模型权重失败: {e}")

    def _evolve_shrinkage(self) -> Optional[dict]:
        """基于校准偏差调整 shrinkage。"""
        if not self.global_calibration.active:
            return None

        bias = self.global_calibration.bias_direction()
        current = float(
            self.runtime_overrides.get(
                "signal.probability_calibration.shrinkage_factor",
                get("signal.probability_calibration.shrinkage_factor") or 0.72,
            )
        )
        bounds = EVOLUTION_BOUNDS["signal.probability_calibration.shrinkage_factor"]

        # 计算偏差幅度
        diffs = []
        for b in self.global_calibration.bins:
            if b.total >= 3:
                diffs.append(b.calibrated_rate - b.midpoint)
        if not diffs:
            return None
        avg_diff = sum(diffs) / len(diffs)

        # 调整方向：过度自信→增大shrinkage，信心不足→减小shrinkage
        adjustment = -avg_diff * 0.3  # 缓慢调整
        new_val = self._bounded_adjust(current, adjustment, bounds)

        if abs(new_val - current) < 0.005:
            return None

        self.runtime_overrides["signal.probability_calibration.shrinkage_factor"] = (
            new_val
        )
        return {
            "param": "shrinkage_factor",
            "old": current,
            "new": new_val,
            "reason": f"校准偏差={bias}, avg_diff={avg_diff:.3f}",
            "data_points": self.global_calibration.total_samples,
            "confidence": min(self.global_calibration.total_samples / 100, 1.0),
        }

    def _evolve_min_ev(self) -> Optional[dict]:
        """基于胜率调整 min_ev。"""
        try:
            bets = db.get_resolved_for_calibration(recent_n=200, **self._db_kw())
        except Exception:
            return None

        if len(bets) < 20:
            return None

        wins = sum(1 for b in bets if b.get("is_win"))
        win_rate = wins / len(bets)
        target_win_rate = 0.50  # 目标胜率

        current = float(
            self.runtime_overrides.get(
                "signal.min_ev_threshold",
                get("signal.min_ev_threshold") or 0.04,
            )
        )
        bounds = EVOLUTION_BOUNDS["signal.min_ev_threshold"]

        # 胜率低→提高EV门槛（更严格），胜率高→降低EV门槛（更宽松）
        adjustment = (target_win_rate - win_rate) * 0.02
        new_val = self._bounded_adjust(current, adjustment, bounds)

        if abs(new_val - current) < 0.002:
            return None

        self.runtime_overrides["signal.min_ev_threshold"] = new_val
        # T83: category_min_ev.default=1.0保持阻塞未知赛道，不再自动调参

        return {
            "param": "min_ev_threshold",
            "old": current,
            "new": new_val,
            "reason": f"胜率={win_rate:.1%} (目标{target_win_rate:.0%})",
            "data_points": len(bets),
            "confidence": min(len(bets) / 100, 1.0),
        }

    def _evolve_category_bonuses(self) -> list[dict]:
        """基于赛道表现调整 categories_bonus。"""
        adjustments = []
        try:
            accuracy = db.get_prediction_accuracy(recent_n=100, **self._db_kw())
        except Exception:
            return []

        current_bonuses = get("scanner.categories_bonus") or {}

        for cat, stats in accuracy.items():
            if cat.startswith("_"):
                continue
            total = stats.get("total", 0)
            if total < 10:
                continue

            win_rate = stats.get("win_rate", 0.5)
            current_bonus = float(current_bonuses.get(cat, 1.0))
            effective = float(
                self.runtime_overrides.get(
                    f"scanner.categories_bonus.{cat}", current_bonus
                )
            )

            # 胜率高→提高bonus，胜率低→降低bonus
            if win_rate > 0.55:
                adj = 0.05
            elif win_rate < 0.40:
                adj = -0.10
            elif win_rate < 0.45:
                adj = -0.05
            else:
                continue

            new_val = max(
                CATEGORY_BONUS_BOUNDS[0], min(CATEGORY_BONUS_BOUNDS[1], effective + adj)
            )
            if abs(new_val - effective) < 0.01:
                continue

            self.runtime_overrides[f"scanner.categories_bonus.{cat}"] = new_val
            adjustments.append(
                {
                    "param": f"category_bonus.{cat}",
                    "old": effective,
                    "new": new_val,
                    "reason": f"胜率={win_rate:.1%}, 样本={total}",
                    "data_points": total,
                    "confidence": min(total / 50, 1.0),
                }
            )

        return adjustments

    def _evolve_probability_bounds(self) -> list[dict]:
        """基于极端概率区间表现调整 ceiling/floor。"""
        adjustments = []
        if not self.global_calibration.active:
            return []

        bins = self.global_calibration.bins

        # 检查高概率区间（bin 8-9, 即 80-100%）
        high_bins = [b for b in bins if b.bin_index >= 8 and b.total >= 5]
        if high_bins:
            avg_cal = sum(b.calibrated_rate for b in high_bins) / len(high_bins)
            avg_mid = sum(b.midpoint for b in high_bins) / len(high_bins)
            # 如果实际胜率显著低于预期 → 降低 ceiling
            if avg_cal < avg_mid - 0.05:
                current = float(
                    self.runtime_overrides.get(
                        "signal.probability_calibration.probability_ceiling",
                        get("signal.probability_calibration.probability_ceiling")
                        or 0.95,
                    )
                )
                bounds = EVOLUTION_BOUNDS[
                    "signal.probability_calibration.probability_ceiling"
                ]
                new_val = self._bounded_adjust(current, -0.02, bounds)
                if abs(new_val - current) >= 0.01:
                    self.runtime_overrides[
                        "signal.probability_calibration.probability_ceiling"
                    ] = new_val
                    adjustments.append(
                        {
                            "param": "probability_ceiling",
                            "old": current,
                            "new": new_val,
                            "reason": f"高概率区实际胜率={avg_cal:.2f} < 预期{avg_mid:.2f}",
                            "data_points": sum(b.total for b in high_bins),
                        }
                    )

        # 检查低概率区间（bin 0-1, 即 0-20%）
        low_bins = [b for b in bins if b.bin_index <= 1 and b.total >= 5]
        if low_bins:
            avg_cal = sum(b.calibrated_rate for b in low_bins) / len(low_bins)
            avg_mid = sum(b.midpoint for b in low_bins) / len(low_bins)
            if avg_cal > avg_mid + 0.05:
                current = float(
                    self.runtime_overrides.get(
                        "signal.probability_calibration.probability_floor",
                        get("signal.probability_calibration.probability_floor") or 0.08,
                    )
                )
                bounds = EVOLUTION_BOUNDS[
                    "signal.probability_calibration.probability_floor"
                ]
                new_val = self._bounded_adjust(current, 0.02, bounds)
                if abs(new_val - current) >= 0.01:
                    self.runtime_overrides[
                        "signal.probability_calibration.probability_floor"
                    ] = new_val
                    adjustments.append(
                        {
                            "param": "probability_floor",
                            "old": current,
                            "new": new_val,
                            "reason": f"低概率区实际胜率={avg_cal:.2f} > 预期{avg_mid:.2f}",
                            "data_points": sum(b.total for b in low_bins),
                        }
                    )

        return adjustments

    @staticmethod
    def _bounded_adjust(
        current: float, adjustment: float, bounds: tuple[float, float]
    ) -> float:
        """应用有界调整：限制单次幅度 + 硬上下限。"""
        max_delta = abs(current) * MAX_CHANGE_PCT
        clamped_adj = max(-max_delta, min(max_delta, adjustment))
        new_val = current + clamped_adj
        return round(max(bounds[0], min(bounds[1], new_val)), 4)


# ══════════════════════════════════════════════════════════════════════
# 标准化残差分析器（灵感: AlchainHust Attention Residual）
# ══════════════════════════════════════════════════════════════════════


@dataclass
class ResidualGroup:
    """一组投注的残差统计。"""

    key: str
    count: int = 0
    sum_residual: float = 0.0
    sum_sq_residual: float = 0.0

    @property
    def mean(self) -> float:
        return self.sum_residual / self.count if self.count else 0

    @property
    def std(self) -> float:
        if self.count < 2:
            return 0
        var = self.sum_sq_residual / self.count - self.mean**2
        return math.sqrt(max(0, var))

    @property
    def z_score(self) -> float:
        """Z-score: how many standard deviations from break-even."""
        if self.count < 5 or self.std == 0:
            return 0
        return self.mean / (self.std / math.sqrt(self.count))


class StandardizedResidualAnalyzer:
    """
    分析投注残差（return per dollar）以识别系统性亏损模式。

    残差 = pnl / size_usdc（每笔投注的标准化收益率）
    按多个维度分组：
      - 赛道 (category)
      - 入场价区间 (entry_price_range)
      - EV 区间 (ev_range)
      - 退出类型 (exit_type)

    Z-score < -1.5 的组被标记为 "系统性亏损"，输出建议。
    """

    Z_THRESHOLD = -1.5  # 标记亏损模式的临界值
    MIN_SAMPLES = 8  # 每组最少样本数

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path

    def analyze(self) -> dict:
        """
        执行残差分析，返回结果字典：
        {
          "groups": {dimension: [group_summary, ...]},
          "alerts": [{"dimension", "key", "z_score", "mean_return", "suggestion"}, ...],
          "total_bets": int,
        }
        """
        kw = {"db_path": self._db_path} if self._db_path else {}
        try:
            bets = db.get_resolved_for_calibration(recent_n=2000, **kw)
        except Exception as e:
            logger.warning(f"[ResidualAnalyzer] 获取投注数据失败: {e}")
            return {"groups": {}, "alerts": [], "total_bets": 0}

        resolved = [
            b
            for b in bets
            if b.get("pnl") is not None and (b.get("size_usdc") or 0) > 0
        ]
        if len(resolved) < 20:
            return {"groups": {}, "alerts": [], "total_bets": len(resolved)}

        # 按多维度分组计算残差
        dimensions = {
            "category": lambda b: b.get("category", "unknown"),
            "entry_price_range": lambda b: self._price_bucket(b.get("market_price", 0)),
            "ev_range": lambda b: self._ev_bucket(b.get("ev", 0)),
            "exit_type": lambda b: b.get("exit_type", "resolution") or "resolution",
        }

        all_groups: dict[str, dict[str, ResidualGroup]] = {}
        for dim_name, key_fn in dimensions.items():
            groups: dict[str, ResidualGroup] = {}
            for b in resolved:
                key = key_fn(b)
                if key not in groups:
                    groups[key] = ResidualGroup(key=key)
                g = groups[key]
                residual = (b["pnl"] or 0) / b["size_usdc"]
                g.count += 1
                g.sum_residual += residual
                g.sum_sq_residual += residual**2
            all_groups[dim_name] = groups

        # 识别系统性亏损模式
        alerts = []
        for dim_name, groups in all_groups.items():
            for key, g in groups.items():
                if g.count < self.MIN_SAMPLES:
                    continue
                z = g.z_score
                if z < self.Z_THRESHOLD:
                    suggestion = self._generate_suggestion(dim_name, key, g)
                    alerts.append(
                        {
                            "dimension": dim_name,
                            "key": key,
                            "z_score": round(z, 2),
                            "mean_return": round(g.mean, 4),
                            "count": g.count,
                            "suggestion": suggestion,
                        }
                    )

        # 按 z_score 排序（最严重的在前）
        alerts.sort(key=lambda a: a["z_score"])

        # 构建摘要
        group_summaries = {}
        for dim_name, groups in all_groups.items():
            group_summaries[dim_name] = [
                {
                    "key": g.key,
                    "count": g.count,
                    "mean_return": round(g.mean, 4),
                    "std": round(g.std, 4),
                    "z_score": round(g.z_score, 2),
                }
                for g in sorted(groups.values(), key=lambda x: x.z_score)
                if g.count >= self.MIN_SAMPLES
            ]

        if alerts:
            logger.warning(
                f"[ResidualAnalyzer] 发现 {len(alerts)} 个系统性亏损模式: "
                + "; ".join(
                    f"{a['dimension']}/{a['key']}(z={a['z_score']})" for a in alerts[:5]
                )
            )

        return {
            "groups": group_summaries,
            "alerts": alerts,
            "total_bets": len(resolved),
        }

    @staticmethod
    def _price_bucket(price: float) -> str:
        if price < 0.2:
            return "0.00-0.20"
        elif price < 0.4:
            return "0.20-0.40"
        elif price < 0.6:
            return "0.40-0.60"
        elif price < 0.8:
            return "0.60-0.80"
        return "0.80-1.00"

    @staticmethod
    def _ev_bucket(ev: float) -> str:
        if ev < 0.05:
            return "0.00-0.05"
        elif ev < 0.10:
            return "0.05-0.10"
        elif ev < 0.20:
            return "0.10-0.20"
        return "0.20+"

    @staticmethod
    def _generate_suggestion(dimension: str, key: str, g: ResidualGroup) -> str:
        mean_pct = g.mean * 100
        suggestions = {
            "category": f"赛道 '{key}' 系统性亏损(avg {mean_pct:.1f}%/笔)，建议降低该赛道 bonus 或提高 min_ev",
            "entry_price_range": f"入场价区间 {key} 系统性亏损(avg {mean_pct:.1f}%/笔)，建议收紧该价格区间入场条件",
            "ev_range": f"EV 区间 {key} 系统性亏损(avg {mean_pct:.1f}%/笔)，建议提高该区间最低 EV 门槛",
            "exit_type": f"退出类型 '{key}' 系统性亏损(avg {mean_pct:.1f}%/笔)，建议优化该退出策略参数",
        }
        return suggestions.get(dimension, f"{dimension}/{key} 亏损模式")
