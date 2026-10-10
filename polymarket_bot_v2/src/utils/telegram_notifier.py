"""
telegram_notifier.py — Telegram Bot 通知模块

通过 Telegram Bot API 发送交易通知、PnL 报告、风控告警。
Token 和 Chat ID 从环境变量读取，绝不硬编码。
所有发送操作带超时且捕获异常，确保不影响主交易流程。

防洪机制（Phase 50）：
  1. 全局速率限制 — 滑动窗口内最多 N 条消息（默认 20条/分钟）
  2. 风控告警去重 — 同类告警 cooldown 期内不重复发送
  3. 仓位平仓合并 — 短时间内多笔平仓合并为1条汇总消息
"""

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests
from src.utils.config_loader import get

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_SEND_TIMEOUT = 8  # seconds

# 防洪默认值（可通过 config.json telegram 节覆盖）
_DEFAULT_RATE_LIMIT_PER_MIN = 20
_DEFAULT_RISK_ALERT_COOLDOWN_SEC = 1800  # 30 min
_DEFAULT_CLOSE_BATCH_WINDOW_SEC = 30  # 30s 内多笔平仓合并


class TelegramNotifier:
    """Telegram 消息推送器（单例模式，所有模块共享）。

    防洪机制：
      - 全局速率限制：滑动窗口 60s 内最多 rate_limit_per_minute 条消息
      - 风控告警 cooldown：同类告警 risk_alert_cooldown_sec 秒内不重复
      - 平仓通知合并：close_batch_window_sec 内多笔平仓聚合为一条消息
    """

    def __init__(self) -> None:
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._available = bool(self._token and self._chat_id)

        # ── 防洪状态 ──
        self._send_timestamps: deque[float] = deque()  # 滑动窗口时间戳
        self._rate_lock = threading.Lock()
        # 风控告警 cooldown: {alert_key: last_sent_ts}
        self._risk_alert_last: dict[str, float] = {}
        # 平仓批量合并
        self._close_batch: list[dict] = []
        self._close_batch_lock = threading.Lock()
        self._close_batch_timer: threading.Timer | None = None
        # 抑制计数（用于日志统计）
        self._suppressed_count = 0

        if self._available:
            logger.info("[Telegram] 通知已启用（含防洪机制）")
        else:
            logger.info("[Telegram] 未配置 TOKEN/CHAT_ID，通知已禁用")

    @property
    def available(self) -> bool:
        return self._available

    # ------------------------------------------------------------------
    # 防洪：速率限制
    # ------------------------------------------------------------------

    def _is_rate_limited(self) -> bool:
        """检查是否超过全局速率限制（滑动窗口）。"""
        cfg = get("telegram") or {}
        limit = int(cfg.get("rate_limit_per_minute", _DEFAULT_RATE_LIMIT_PER_MIN))
        now = time.monotonic()
        with self._rate_lock:
            # 清理 60s 前的时间戳
            while self._send_timestamps and self._send_timestamps[0] < now - 60:
                self._send_timestamps.popleft()
            if len(self._send_timestamps) >= limit:
                self._suppressed_count += 1
                if self._suppressed_count % 10 == 1:
                    logger.warning(
                        f"[Telegram] 速率限制触发（{limit}条/分钟），"
                        f"已抑制 {self._suppressed_count} 条消息"
                    )
                return True
            self._send_timestamps.append(now)
            return False

    # ------------------------------------------------------------------
    # 底层发送
    # ------------------------------------------------------------------

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """发送消息到 Telegram。失败时仅记录日志，不抛异常。"""
        cfg = get("telegram") or {}
        if not cfg.get("enabled", True) or not self._available:
            return False
        # 速率限制检查
        if self._is_rate_limited():
            return False
        # Telegram 消息长度限制 4096
        if len(text) > 4000:
            text = text[:4000] + "\n...(截断)"
        try:
            url = _TELEGRAM_API.format(token=self._token)
            resp = requests.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                },
                timeout=_SEND_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"[Telegram] 发送失败: HTTP {resp.status_code} — {resp.text[:200]}"
                )
                return False
            return True
        except Exception as e:
            logger.warning(f"[Telegram] 发送异常: {e}")
            return False

    def send_async(self, text: str, parse_mode: str = "HTML") -> None:
        """非阻塞发送（后台线程），不影响主交易循环性能。"""
        thread = threading.Thread(
            target=self.send_message,
            args=(text, parse_mode),
            daemon=True,
        )
        thread.start()

    # ------------------------------------------------------------------
    # 高级通知方法
    # ------------------------------------------------------------------

    def notify_new_bet(
        self,
        question: str,
        outcome: str,
        market_price: float,
        ev: float,
        confidence: float,
        size_usdc: float,
        category: str,
        models: str = "",
        is_live: bool = False,
    ) -> None:
        """新下单通知。"""
        cfg = get("telegram") or {}
        if not cfg.get("notify_new_bet", True):
            return
        mode = "🔴 实盘" if is_live else "🟡 模拟"
        text = (
            f"🆕 <b>新下单</b> {mode}\n"
            f"📊 {_escape(question[:80])}\n"
            f"🎯 {outcome} @ ${market_price:.3f} | EV: +{ev:.3f}\n"
            f"📈 置信度: {confidence:.0%} | 赛道: {category}\n"
            f"💰 投注: ${size_usdc:.2f}"
        )
        if models:
            text += f"\n🤖 {_escape(models)}"
        self.send_async(text)

    def notify_position_closed(
        self,
        question: str,
        close_type: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        size_usdc: float,
        category: str = "",
    ) -> None:
        """仓位平仓通知（止盈/止损/结算）— 带批量合并。

        短时间内多笔平仓会合并为一条汇总消息，避免消息洪水。
        """
        cfg = get("telegram") or {}
        if not cfg.get("notify_position_closed", True):
            return

        batch_window = float(
            cfg.get("close_batch_window_sec", _DEFAULT_CLOSE_BATCH_WINDOW_SEC)
        )
        item = {
            "question": question[:60],
            "close_type": close_type,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "size_usdc": size_usdc,
            "category": category,
        }

        with self._close_batch_lock:
            self._close_batch.append(item)
            # 如果已有定时器在等待，不重复创建
            if (
                self._close_batch_timer is None
                or not self._close_batch_timer.is_alive()
            ):
                self._close_batch_timer = threading.Timer(
                    batch_window, self._flush_close_batch
                )
                self._close_batch_timer.daemon = True
                self._close_batch_timer.start()

    def _flush_close_batch(self) -> None:
        """定时器到期后，发送合并的平仓通知。"""
        with self._close_batch_lock:
            batch = list(self._close_batch)
            self._close_batch.clear()
            self._close_batch_timer = None

        if not batch:
            return

        if len(batch) == 1:
            # 只有 1 笔，发送详细格式
            item = batch[0]
            return_pct = (
                item["pnl"] / item["size_usdc"] if item["size_usdc"] > 0 else 0.0
            )
            icon, label = _close_type_display(item["close_type"])
            text = (
                f"{icon} <b>{label}</b>\n"
                f"📊 {_escape(item['question'])}\n"
                f"💲 入场={item['entry_price']:.3f} → 离场={item['exit_price']:.3f}\n"
                f"💰 P&amp;L: {item['pnl']:+.2f} USDC ({return_pct:+.1%})\n"
                f"📁 赛道: {item['category']}"
            )
        else:
            # 多笔合并为汇总
            total_pnl = sum(i["pnl"] for i in batch)
            stops = sum(1 for i in batch if i["close_type"] == "stop_loss")
            profits = sum(1 for i in batch if i["close_type"] == "profit_take")
            settlements = sum(1 for i in batch if i["close_type"] == "settlement")
            others = len(batch) - stops - profits - settlements

            parts = []
            if stops:
                parts.append(f"🛑止损 {stops}")
            if profits:
                parts.append(f"✅止盈 {profits}")
            if settlements:
                parts.append(f"🏁结算 {settlements}")
            if others:
                parts.append(f"📤其他 {others}")

            text = (
                f"📤 <b>批量平仓 ({len(batch)} 笔)</b>\n"
                f"{' | '.join(parts)}\n"
                f"💰 合计 P&amp;L: {total_pnl:+.2f} USDC\n"
            )
            # 附加每笔摘要（最多 8 笔）
            for item in batch[:8]:
                icon, _ = _close_type_display(item["close_type"])
                text += (
                    f"\n{icon} {_escape(item['question'][:40])} "
                    f"| {item['pnl']:+.2f}"
                )
            if len(batch) > 8:
                text += f"\n...及另外 {len(batch) - 8} 笔"

        self.send_async(text)

    def notify_settlement(
        self,
        question: str,
        our_outcome: str,
        actual_outcome: str,
        is_win: bool,
        pnl: float,
        size_usdc: float,
    ) -> None:
        """市场结算通知。"""
        cfg = get("telegram") or {}
        if not cfg.get("notify_position_closed", True):
            return
        result = "🎉 WIN" if is_win else "💔 LOSS"
        return_pct = pnl / size_usdc if size_usdc > 0 else 0.0
        text = (
            f"🏁 <b>市场结算</b> {result}\n"
            f"📊 {_escape(question[:80])}\n"
            f"🎯 预测={our_outcome} | 实际={actual_outcome}\n"
            f"💰 P&amp;L: {pnl:+.2f} USDC ({return_pct:+.1%})"
        )
        self.send_async(text)

    def notify_risk_alert(self, alert_msg: str) -> None:
        """风控告警通知 — 带 cooldown 去重。

        同类告警在 cooldown 期内不重复发送（默认 30 分钟）。
        告警类型通过消息前缀自动识别（如 [风控]、[提现]）。
        """
        cfg = get("telegram") or {}
        if not cfg.get("notify_risk_alert", True):
            return

        cooldown = float(
            cfg.get("risk_alert_cooldown_sec", _DEFAULT_RISK_ALERT_COOLDOWN_SEC)
        )
        # 提取告警类型 key（取消息前30字符作为去重标识）
        alert_key = alert_msg[:30].strip()
        now = time.monotonic()

        last_sent = self._risk_alert_last.get(alert_key)
        if last_sent is not None and now - last_sent < cooldown:
            logger.debug(
                f"[Telegram] 风控告警已抑制（cooldown {cooldown}s）: {alert_key}..."
            )
            return

        self._risk_alert_last[alert_key] = now
        text = f"⚠️ <b>风控警报</b>\n{_escape(alert_msg)}"
        self.send_async(text)

    def notify_cycle_summary(
        self,
        cycle_count: int,
        new_bets: int,
        open_positions: int,
        balance: float,
        nlv: float,
        day_pnl: float,
    ) -> None:
        """每 N 轮发送一次周期汇总。"""
        cfg = get("telegram") or {}
        if not cfg.get("notify_cycle_summary", True):
            return
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        text = (
            f"📊 <b>周期汇总</b> #{cycle_count} ({now})\n"
            f"💰 余额: ${balance:.2f} | NLV: ${nlv:.2f}\n"
            f"📈 今日P&amp;L: {day_pnl:+.2f} USDC\n"
            f"🆕 本轮新增: {new_bets} 笔 | 持仓: {open_positions} 个"
        )
        self.send_async(text)

    def notify_startup(self, dry_run: bool, balance: float) -> None:
        """Bot 启动通知。"""
        mode = "🟡 DRY RUN" if dry_run else "🔴 LIVE"
        text = (
            f"🚀 <b>Bot 启动</b> {mode}\n"
            f"💰 余额: ${balance:.2f}\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self.send_async(text)


# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------


def _close_type_display(close_type: str) -> tuple[str, str]:
    """返回平仓类型的 (icon, label)。"""
    if close_type == "profit_take":
        return "✅", "止盈平仓"
    elif close_type == "stop_loss":
        return "🛑", "止损平仓"
    elif close_type == "settlement":
        return "🏁", "市场结算"
    elif close_type == "wu_temp_breach":
        return "🌡️", "WU温度死刑平仓"
    return "📤", "平仓"


# ------------------------------------------------------------------
# HTML 转义
# ------------------------------------------------------------------


def _escape(text: str) -> str:
    """转义 HTML 特殊字符。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------
# 模块级单例
# ------------------------------------------------------------------

_notifier: TelegramNotifier | None = None
_lock = threading.Lock()


def get_notifier() -> TelegramNotifier:
    """获取全局 TelegramNotifier 单例。"""
    global _notifier
    if _notifier is None:
        with _lock:
            if _notifier is None:
                _notifier = TelegramNotifier()
    return _notifier


def send(text: str, parse_mode: str = "HTML") -> bool:
    """快捷发送接口。"""
    return get_notifier().send_message(text, parse_mode)
