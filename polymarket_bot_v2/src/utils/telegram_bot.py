"""
telegram_bot.py — 双向 Telegram Bot 管理模块

通过 Long Polling 监听 Telegram 命令，实现远程系统管理：
  - /help: 命令简要帮助
  - /docs: 完整管理文档（配置说明、命令用法）
  - /status: 系统状态概览
  - /log [n]: 查看最近日志
  - /positions [#ID]: 持仓列表（含实时价格/P&L）/ 详情（含Polymarket链接）
  - /sort [field] [dir]: 持仓排序设置（pnl/ev/time, asc/desc）
  - /pnl: PnL 汇总
  - /config list: 可修改配置一览
  - /config get <key>: 读取配置值
  - /config set <key> <value>: 修改配置值（有黑名单保护）
  - /restart: 重启系统（需确认）

安全机制：
  1. Chat ID 白名单 — 仅响应白名单内用户，非白名单静默忽略
  2. 命令级别分类 — 敏感操作需确认
  3. 配置修改黑名单 — 保护关键参数
  4. 速率限制 — 防滥用
"""

import json
import logging
import os
import shutil
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from src.utils.config_loader import backup_config, get, load_config

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}"
_POLL_TIMEOUT = 30  # long polling timeout (seconds)
_REQUEST_TIMEOUT = 35  # slightly longer than poll timeout

# 配置修改黑名单 — 与 dashboard 保持一致
_BLOCKED_CONFIG_KEYS = {
    "dry_run",
    "executor",
    "api_key",
    "private_key",
    "secret",
    "allowed_chat_ids",  # 白名单只允许从 PC 端仪表盘修改
}
_ALLOWED_CONFIG_SECTIONS = {
    "paper_trading",
    "signal",
    "risk",
    "evolution",
    "scanner",
    "profit_taking",
    "stop_loss",
    "general",
    "smart_money",
    "position_management",
    "sports_live",
    "weather_ensemble",
    "telegram",
    "telegram_bot",
}

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config" / "config.json"
_LOG_PATH = _PROJECT_ROOT / "logs" / "bot.log"

# 用户友好的配置名称映射 {中文名: 点分路径}
_CONFIG_FRIENDLY: list[tuple[str, str, str]] = [
    # (中文名, 点分路径, 简要说明)
    (
        "最低EV(默认)",
        "paper_trading.category_min_ev.default",
        "未知赛道EV阻塞(1.0=禁止)",
    ),
    (
        "最低置信度(默认)",
        "paper_trading.category_min_confidence.default",
        "未知赛道置信度阻塞(1.0=禁止)",
    ),
    (
        "天气最大入场窗口",
        "paper_trading.weather_entry_window_hours",
        "天气市场最远入场时间(小时)",
    ),
    ("最大单笔比例", "risk.max_single_bet_pct", "单笔占余额最大%"),
    ("日亏暂停", "risk.daily_loss_pause_pct", "日亏损暂停阈值%"),
    ("累亏暂停", "risk.cumulative_loss_pause_pct", "累计亏损暂停%"),
    ("提现告警", "risk.profit_alert_pct", "盈利提现提醒%"),
    ("标准止损", "stop_loss.standard_stop_pct", "标准止损比例"),
    ("天气止损", "stop_loss.weather_stop_pct", "天气赛道止损"),
    ("止盈比例", "profit_taking.default_take_profit_pct", "默认止盈比例"),
    ("止盈最低利润", "profit_taking.min_profit_usdc", "止盈最低利润$"),
    ("扫描间隔", "general.scan_interval_minutes", "市场扫描间隔(分钟)"),
    ("监控间隔", "general.position_monitor_interval_minutes", "持仓监控间隔(分钟)"),
    ("信号模型", "signal.models", "AI信号模型列表"),
    ("天气EV门槛", "weather_ensemble.min_edge", "天气最低edge%"),
    ("速率限制", "telegram_bot.command_rate_limit", "Bot每分钟最大命令数"),
]
# 反向映射 {中文名小写: 点分路径}
_FRIENDLY_TO_PATH = {name: path for name, path, _ in _CONFIG_FRIENDLY}
# 路径到中文名 {点分路径: 中文名}
_PATH_TO_FRIENDLY = {path: name for name, path, _ in _CONFIG_FRIENDLY}


class TelegramBot:
    """双向 Telegram Bot — 长轮询监听命令。"""

    def __init__(self) -> None:
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._enabled = bool(self._token and self._chat_id)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._offset = 0  # Telegram getUpdates offset
        self._start_time = datetime.now(timezone.utc)
        self._monitor = None  # T39: RiskMonitor 引用，由 main.py 注入

        # 持仓排序偏好（默认按 PnL 从大到小）
        self._sort_field = "pnl"  # pnl | ev | time
        self._sort_desc = True  # True = desc, False = asc

        # 速率限制: {chat_id: [timestamp, ...]}
        self._rate_buckets: dict[str, list[float]] = defaultdict(list)

        # 待确认的回调 {callback_data: handler_func}
        self._pending_callbacks: dict[str, dict] = {}

        if not self._enabled:
            logger.info("[TelegramBot] 未配置 TOKEN/CHAT_ID，双向 Bot 已禁用")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _get_allowed_chat_ids(self) -> set[str]:
        """从配置获取 Chat ID 白名单。"""
        cfg = get("telegram_bot") or {}
        ids = cfg.get("allowed_chat_ids", [])
        result = {str(i) for i in ids if i}
        # 兼容：如果白名单为空但有环境变量 CHAT_ID，使用它
        if not result and self._chat_id:
            result.add(self._chat_id)
        return result

    # ── 生命周期 ──────────────────────────────────────────

    def start(self) -> None:
        """启动长轮询后台线程。"""
        if not self._enabled:
            return
        cfg = get("telegram_bot") or {}
        if not cfg.get("enabled", True):
            logger.info("[TelegramBot] 配置 telegram_bot.enabled=false，跳过启动")
            return
        # 白名单检查
        allowed = self._get_allowed_chat_ids()
        if not allowed:
            logger.warning(
                "[TelegramBot] allowed_chat_ids 为空且无 CHAT_ID，Bot 已禁用"
            )
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._polling_loop, daemon=True, name="TelegramBotPoller"
        )
        self._thread.start()
        logger.info("[TelegramBot] 长轮询已启动")

    def stop(self) -> None:
        """停止长轮询线程。"""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=_POLL_TIMEOUT + 5)
        logger.info("[TelegramBot] 已停止")

    # ── 长轮询 ────────────────────────────────────────────

    def _polling_loop(self) -> None:
        """主轮询循环 — 持续调用 getUpdates。"""
        logger.info("[TelegramBot] 轮询线程启动")
        while not self._stop_event.is_set():
            try:
                updates = self._get_updates()
                for update in updates:
                    self._handle_update(update)
            except requests.exceptions.Timeout:
                continue  # long polling 正常超时
            except requests.exceptions.ConnectionError:
                logger.warning("[TelegramBot] 网络连接失败，30秒后重试")
                self._stop_event.wait(30)
            except Exception as e:
                logger.error(f"[TelegramBot] 轮询异常: {e}")
                self._stop_event.wait(10)

    def _get_updates(self) -> list:
        """调用 Telegram getUpdates API。"""
        url = f"{_TELEGRAM_API.format(token=self._token)}/getUpdates"
        resp = requests.get(
            url,
            params={
                "offset": self._offset,
                "timeout": _POLL_TIMEOUT,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=_REQUEST_TIMEOUT,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.warning(f"[TelegramBot] getUpdates 失败: {data}")
            return []
        results = data.get("result", [])
        if results:
            self._offset = results[-1]["update_id"] + 1
        return results

    # ── 消息处理 ──────────────────────────────────────────

    def _handle_update(self, update: dict) -> None:
        """路由分发消息和回调。"""
        # 处理 callback_query（内联按钮确认）
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return

        msg = update.get("message")
        if not msg or not msg.get("text"):
            return

        chat_id = str(msg["chat"]["id"])
        text = msg["text"].strip()

        # 安全层 1：Chat ID 白名单 — 非白名单静默拒绝（不回复、不提示）
        allowed = self._get_allowed_chat_ids()
        if chat_id not in allowed:
            logger.warning(
                f"[TelegramBot] 🚫 未授权访问 chat_id={chat_id} "
                f"user={msg.get('from', {}).get('username', '?')} 尝试: {text[:50]}"
            )
            return

        # 安全层 2：速率限制
        if not self._check_rate_limit(chat_id):
            self._send(chat_id, "⚠️ 操作过于频繁，请稍后再试。")
            return

        # 路由命令
        if text.startswith("/"):
            self._route_command(chat_id, text)

    def _handle_callback(self, callback: dict) -> None:
        """处理内联键盘回调（确认操作）。"""
        chat_id = str(callback["message"]["chat"]["id"])
        callback_id = callback["id"]
        data = callback.get("data", "")

        # Chat ID 白名单验证
        allowed = self._get_allowed_chat_ids()
        if chat_id not in allowed:
            return

        # 应答回调（移除按钮旋转加载）
        self._answer_callback(callback_id)

        if data == "restart_confirm":
            self._send(chat_id, "🔄 系统正在重启...")
            logger.info("[TelegramBot] 收到远程重启确认，执行 sys.exit(0)")
            time.sleep(1)
            os._exit(0)  # systemd 自动重启
        elif data == "restart_cancel":
            self._send(chat_id, "❌ 已取消重启。")
        elif data.startswith("cfgset_confirm:"):
            self._execute_config_set(chat_id, data)
        elif data == "cfgset_cancel":
            self._send(chat_id, "❌ 已取消配置修改。")

    def _route_command(self, chat_id: str, text: str) -> None:
        """将文本命令路由到对应处理函数。"""
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]  # 去掉 @bot_name 后缀

        handlers = {
            "/help": self._cmd_help,
            "/docs": self._cmd_docs,
            "/status": self._cmd_status,
            "/log": self._cmd_log,
            "/positions": self._cmd_positions,
            "/sort": self._cmd_sort,
            "/pnl": self._cmd_pnl,
            "/config": self._cmd_config,
            "/restart": self._cmd_restart,
            "/resume": self._cmd_resume,
        }

        handler = handlers.get(cmd)
        if handler:
            try:
                handler(chat_id, parts[1:])
            except Exception as e:
                logger.error(f"[TelegramBot] 命令 {cmd} 执行异常: {e}")
                self._send(chat_id, f"❌ 命令执行出错: {e}")
        else:
            self._send(chat_id, f"❓ 未知命令: {cmd}\n输入 /help 查看可用命令")

    # ── 命令实现 ──────────────────────────────────────────

    def _cmd_help(self, chat_id: str, args: list) -> None:
        """显示可用命令列表。"""
        text = (
            "📖 <b>VagusPoly Bot 命令</b>\n\n"
            "📊 /status — 系统状态概览\n"
            "📋 /positions — 持仓列表（含实时P&amp;L）\n"
            "📋 /positions 3 — 查看第3笔持仓详情\n"
            "🔀 /sort pnl desc — 按盈亏排序\n"
            "🔀 /sort ev asc — 按EV排序\n"
            "🔀 /sort time — 按时间排序\n"
            "📈 /pnl — 盈亏汇总\n"
            "📜 /log — 最近日志\n"
            "📜 /log 30 — 最近30行日志\n"
            "⚙️ /config list — 查看可修改配置\n"
            "⚙️ /config get 最低EV — 读取配置\n"
            "⚙️ /config set 最低EV 0.05 — 修改配置\n"
            "🔄 /restart — 重启系统\n"
            "▶️ /resume — 解除风控暂停\n\n"
            "📚 /docs — 完整管理文档\n"
        )
        self._send(chat_id, text)

    def _cmd_docs(self, chat_id: str, args: list) -> None:
        """发送完整管理文档（分多条消息）。"""
        # 第1条：命令详解
        msg1 = (
            "📚 <b>VagusPoly 管理文档 (1/3)</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "📊 <b>/status</b>\n"
            "查看系统运行模式、在线时长、余额、今日盈亏、持仓数。\n\n"
            "📋 <b>/positions</b>\n"
            "列出所有持仓，显示实时价格和P&amp;L。\n"
            "• <code>/positions</code> — 持仓概览列表\n"
            "• <code>/positions 3</code> — 查看第3笔详情（含Polymarket链接）\n\n"
            "🔀 <b>/sort [字段] [方向]</b>\n"
            "设置持仓列表排序方式。\n"
            "• <code>/sort pnl desc</code> — 按盈亏从大到小（默认）\n"
            "• <code>/sort ev asc</code> — 按EV从小到大\n"
            "• <code>/sort time</code> — 按时间排序\n\n"
            "📈 <b>/pnl</b>\n"
            "显示今日已结算盈亏、累计盈亏、胜率、持仓价值。\n\n"
            "📜 <b>/log [行数]</b>\n"
            "查看最近的系统日志。\n"
            "• <code>/log</code> — 默认20行\n"
            "• <code>/log 50</code> — 最多50行\n\n"
            "🔄 <b>/restart</b>\n"
            "重启交易系统。会弹出确认按钮，点击后系统自动重启。\n"
        )
        self._send(chat_id, msg1)

        # 第2条：配置管理
        msg2 = (
            "📚 <b>VagusPoly 管理文档 (2/3)</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "⚙️ <b>配置管理</b>\n\n"
            "<b>查看所有可修改配置：</b>\n"
            "• <code>/config list</code>\n\n"
            "<b>读取某项配置：</b>\n"
            "• <code>/config get 最低EV</code> — 用中文名\n"
            "• <code>/config get paper_trading.category_min_ev.default</code> — 用路径\n\n"
            "<b>修改配置：</b>\n"
            "• <code>/config set 最低EV 0.05</code>\n"
            "• <code>/config set 日亏暂停 0.08</code>\n"
            "• <code>/config set risk.daily_loss_pause_pct 0.08</code>\n\n"
            "修改后会显示确认按钮，点击确认才生效。\n"
            "配置热加载，无需重启即可生效。\n\n"
            "🔒 <b>受保护配置（不可远程修改）：</b>\n"
            "dry_run、executor、api_key、private_key、secret、"
            "allowed_chat_ids\n"
        )
        self._send(chat_id, msg2)

        # 第3条：配置速查表
        msg3 = self._build_config_cheatsheet()
        self._send(chat_id, msg3)

    def _build_config_cheatsheet(self) -> str:
        """构建配置速查表消息。"""
        lines = [
            "📚 <b>VagusPoly 管理文档 (3/3)</b>",
            "━━━━━━━━━━━━━━━━━━━",
            "",
            "⚙️ <b>配置速查表</b>",
            "",
        ]
        for name, path, desc in _CONFIG_FRIENDLY:
            val = get(path)
            val_str = _escape(str(val)) if val is not None else "N/A"
            if len(val_str) > 40:
                val_str = val_str[:37] + "..."
            lines.append(f"<b>{name}</b>  →  <code>{val_str}</code>")
            lines.append(f"  <i>{desc}</i>  |  <code>{path}</code>")
        lines.append("")
        lines.append("💡 用中文名或路径都可以操作：")
        lines.append("<code>/config get 最低EV</code>")
        lines.append("<code>/config set 日亏暂停 0.08</code>")
        return "\n".join(lines)

    @staticmethod
    def _resolve_config_key(key_input: str) -> str:
        """将用户输入（可能是中文名或点分路径）解析为实际点分路径。"""
        # 先检查是否是已知中文名
        if key_input in _FRIENDLY_TO_PATH:
            return _FRIENDLY_TO_PATH[key_input]
        # 再检查是否是点分路径（原样返回）
        if "." in key_input:
            return key_input
        # 模糊匹配中文名
        for name, path, _ in _CONFIG_FRIENDLY:
            if key_input in name or name in key_input:
                return path
        # 最后尝试作为 leaf key 在所有 section 中搜索
        return key_input

    def _cmd_status(self, chat_id: str, args: list) -> None:
        """返回系统状态概览。"""
        from src.utils.config_loader import is_dry_run

        dry_run = is_dry_run()
        mode = "🟢 DRY RUN（模拟）" if dry_run else "🔴 LIVE（实盘）"
        uptime = datetime.now(timezone.utc) - self._start_time
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)

        # 持仓数
        open_count = 0
        try:
            from src.utils.db import get_open_paper_bets

            open_bets = get_open_paper_bets()
            open_count = len(open_bets)
        except Exception:
            pass

        # 余额
        balance_str = "N/A"
        try:
            from src.utils.db import connect

            with connect() as conn:
                row = conn.execute(
                    "SELECT usdc_balance FROM balance_log ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if row:
                    balance_str = f"${row[0]:.2f}"
        except Exception:
            pass

        # 今日 PnL
        today_pnl_str = "N/A"
        try:
            from src.utils.db import get_today_settled_pnl

            today_pnl = get_today_settled_pnl()
            sign = "+" if today_pnl >= 0 else ""
            today_pnl_str = f"{sign}${today_pnl:.2f}"
        except Exception:
            pass

        text = (
            f"📊 <b>系统状态</b>\n\n"
            f"🔒 模式: {mode}\n"
            f"⏱ 运行时间: {hours}h {minutes}m\n"
            f"💰 余额: {balance_str}\n"
            f"📈 今日 PnL: {today_pnl_str}\n"
            f"📋 持仓数: {open_count}"
        )
        self._send(chat_id, text)

    def _cmd_log(self, chat_id: str, args: list) -> None:
        """返回最近 n 行日志。"""
        cfg = get("telegram_bot") or {}
        default_lines = cfg.get("log_lines_default", 20)
        max_lines = cfg.get("log_lines_max", 50)

        n = default_lines
        if args:
            try:
                n = int(args[0])
            except ValueError:
                self._send(chat_id, "⚠️ 用法: /log [行数]")
                return
        n = max(1, min(n, max_lines))

        if not _LOG_PATH.exists():
            self._send(chat_id, "⚠️ 日志文件不存在")
            return

        try:
            with open(_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            tail = lines[-n:]
            text = "".join(tail)
            if len(text) > 3800:
                text = text[-3800:]
            self._send(
                chat_id,
                f"📋 <b>最近 {len(tail)} 行日志</b>\n\n<pre>{_escape(text)}</pre>",
            )
        except Exception as e:
            self._send(chat_id, f"❌ 读取日志失败: {e}")

    def _cmd_positions(self, chat_id: str, args: list) -> None:
        """列出当前持仓（含实时价格和P&L），或查看某笔详情。"""
        try:
            from src.utils.db import get_open_paper_bets

            bets = get_open_paper_bets()
        except Exception as e:
            self._send(chat_id, f"❌ 查询持仓失败: {e}")
            return

        if not bets:
            self._send(chat_id, "📋 当前无持仓")
            return

        # 获取链上实时数据（curPrice, cashPnl）
        chain_data = self._fetch_chain_pnl()

        # 为每笔持仓附加实时数据
        for b in bets:
            cid = b.get("condition_id", "")
            chain = chain_data.get(cid, {})
            b["_cur_price"] = chain.get("curPrice")
            b["_cash_pnl"] = chain.get("cashPnl")
            b["_event_slug"] = chain.get("eventSlug") or b.get("event_slug", "")

        # Gamma API 回退：对无链上数据的持仓（DRY RUN 或实盘新建单）补充实时价格
        missing = [b for b in bets if b.get("_cur_price") is None]
        if missing:
            gamma_prices = self._fetch_gamma_sell_prices(missing)
            for b in missing:
                gp = gamma_prices.get(b.get("bet_id", ""))
                if gp is not None:
                    b["_cur_price"] = gp
                    # 估算 PnL（仅 DRY RUN 无链上数据时使用）
                    entry = float(b.get("market_price") or 0)
                    shares = float(b.get("matched_shares") or 0)
                    if entry > 0 and shares > 0 and b.get("_cash_pnl") is None:
                        b["_cash_pnl"] = round((gp - entry) * shares, 4)

        # 排序
        bets = self._sort_bets(bets)

        # 如果有参数，查看详情
        if args:
            idx_str = args[0].lstrip("#")
            try:
                idx = int(idx_str)
            except ValueError:
                self._send(chat_id, "⚠️ 用法: /positions [编号]\n例: /positions 3")
                return
            if idx < 1 or idx > len(bets):
                self._send(chat_id, f"⚠️ 编号 {idx} 超出范围（1-{len(bets)}）")
                return
            b = bets[idx - 1]
            self._send_position_detail(chat_id, b, idx)
            return

        # 持仓概览列表
        total_pnl = sum(b.get("_cash_pnl") or 0 for b in bets)
        pnl_sign = "+" if total_pnl >= 0 else ""
        sort_label = {"pnl": "盈亏", "ev": "EV", "time": "时间"}.get(
            self._sort_field, "盈亏"
        )
        arrow = "↓" if self._sort_desc else "↑"
        lines = [
            f"📋 <b>当前持仓</b>（{len(bets)} 笔）"
            f"| 总P&amp;L: {pnl_sign}${total_pnl:.2f}\n"
            f"<i>排序: {sort_label}{arrow}  用 /sort 更改</i>\n"
        ]
        for i, b in enumerate(bets[:20], 1):
            question = b.get("question", "?")[:42]
            outcome = b.get("outcome", "?")
            entry_price = b.get("market_price", 0)
            size = b.get("size_usdc", 0)
            cur_price = b.get("_cur_price")
            cash_pnl = b.get("_cash_pnl")

            # 格式化实时价格和 PnL
            price_str = f"${cur_price:.3f}" if cur_price is not None else "—"
            if cash_pnl is not None:
                pnl_s = "+" if cash_pnl >= 0 else ""
                pnl_str = f"{pnl_s}${cash_pnl:.2f}"
            else:
                pnl_str = "—"

            lines.append(
                f"<b>#{i}</b> {_escape(question)}\n"
                f"  {outcome} @ ${entry_price:.3f} → {price_str}"
                f" | ${size:.2f} | P&amp;L: {pnl_str}"
            )

        if len(bets) > 20:
            lines.append(f"\n...还有 {len(bets) - 20} 笔未显示")
        lines.append("\n💡 查看详情: <code>/positions 编号</code>")
        self._send(chat_id, "\n".join(lines))

    def _fetch_chain_pnl(self) -> dict[str, dict]:
        """从 Data API 获取链上实时价格和 P&L 数据。

        Returns:
            {condition_id: {curPrice, cashPnl, eventSlug, ...}}
        """
        funder = os.getenv("POLY_RELAYER_ADDRESS", "")
        if not funder:
            return {}
        try:
            resp = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": funder.lower(), "sizeThreshold": "0"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return {}
            result = {}
            for p in data:
                cid = p.get("conditionId", "")
                if cid:
                    result[cid] = {
                        "curPrice": float(p.get("curPrice") or 0),
                        "cashPnl": float(p.get("cashPnl") or 0),
                        "currentValue": float(p.get("currentValue") or 0),
                        "eventSlug": p.get("eventSlug", ""),
                    }
            return result
        except Exception as e:
            logger.debug(f"[TelegramBot] 获取链上实时数据失败: {e}")
            return {}

    def _fetch_gamma_sell_prices(self, bets: list[dict]) -> dict[str, float]:
        """从 Gamma API 获取持仓的卖出价格（与仪表盘逻辑对齐）。

        YES 仓位 → bestBid（买方最高出价）
        NO  仓位 → 1 - bestAsk（NO token 有效卖出价）

        Returns:
            {bet_id: sell_price}，价格范围 0-1
        """
        result: dict[str, float] = {}
        gamma_api = "https://gamma-api.polymarket.com"
        # 按 market_id 分组，同市场多笔单只请求一次
        market_map: dict[str, list[dict]] = {}
        for b in bets:
            mid = b.get("market_id", "")
            if mid:
                market_map.setdefault(mid, []).append(b)

        for mid, bet_list in market_map.items():
            try:
                resp = requests.get(f"{gamma_api}/markets/{mid}", timeout=8)
                resp.raise_for_status()
                mdata = resp.json()
                if not isinstance(mdata, dict):
                    continue
                best_bid = mdata.get("bestBid")
                best_ask = mdata.get("bestAsk")
                tokens = mdata.get("tokens") or []
                outcome_prices: dict[str, float] = {}
                for tok in tokens:
                    oc = (tok.get("outcome") or "").upper()
                    tp = tok.get("price")
                    if oc and tp is not None:
                        outcome_prices[oc] = float(tp)

                for b in bet_list:
                    outcome = (b.get("outcome") or "YES").upper()
                    sell_price: float | None = None
                    if outcome == "YES":
                        if best_bid is not None:
                            sell_price = float(best_bid)
                        else:
                            sell_price = outcome_prices.get("YES")
                    elif outcome == "NO":
                        if best_ask is not None:
                            sell_price = round(1.0 - float(best_ask), 4)
                        else:
                            no_p = outcome_prices.get("NO")
                            sell_price = no_p
                    else:
                        sell_price = outcome_prices.get(outcome)
                    if sell_price is not None:
                        result[b["bet_id"]] = sell_price
            except Exception as e:
                logger.debug(f"[TelegramBot] Gamma API 获取失败 {mid[:16]}: {e}")

        return result

    def _sort_bets(self, bets: list[dict]) -> list[dict]:
        """按当前排序偏好排序持仓列表。"""
        field = self._sort_field
        desc = self._sort_desc
        if field == "pnl":
            key_fn = lambda b: b.get("_cash_pnl") or 0
        elif field == "ev":
            key_fn = lambda b: b.get("ev") or 0
        else:  # time
            key_fn = lambda b: b.get("ts_entered") or 0
        return sorted(bets, key=key_fn, reverse=desc)

    def _send_position_detail(self, chat_id: str, b: dict, idx: int) -> None:
        """发送单笔持仓的详细信息（含 Polymarket 链接和实时 P&L）。"""
        from datetime import datetime as dt

        question = b.get("question", "N/A")
        bet_id = b.get("bet_id", "N/A")
        market_id = b.get("market_id", "N/A")
        category = b.get("category", "N/A")
        outcome = b.get("outcome", "N/A")
        confidence = b.get("confidence", 0)
        ai_prob = b.get("ai_probability", 0)
        market_price = b.get("market_price", 0)
        entry_price = b.get("entry_price", market_price)
        ev = b.get("ev", 0)
        size = b.get("size_usdc", 0)
        end_date = b.get("market_end_date", "N/A")
        ai_models = b.get("ai_models", "N/A")
        ai_reasoning = b.get("ai_reasoning", "")
        bet_bucket = b.get("bet_bucket", "standard")
        condition_id = b.get("condition_id", "")
        dimension_tags = b.get("dimension_tags", "")

        # 实时数据（可能从 _cmd_positions 预填充）
        cur_price = b.get("_cur_price")
        cash_pnl = b.get("_cash_pnl")
        event_slug = b.get("_event_slug") or b.get("event_slug", "")

        # 入场时间
        ts = b.get("ts_entered", 0)
        entered_str = (
            dt.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")
            if ts
            else "N/A"
        )

        # Polymarket 链接
        poly_link = ""
        if event_slug:
            poly_link = f"https://polymarket.com/event/{event_slug}"

        # 实时价格和 P&L
        price_line = ""
        if cur_price is not None:
            price_line = f"📊 实时价: ${cur_price:.4f}\n"
        if cash_pnl is not None:
            pnl_s = "+" if cash_pnl >= 0 else ""
            emoji = "🟢" if cash_pnl >= 0 else "🔴"
            price_line += f"{emoji} P&amp;L: {pnl_s}${cash_pnl:.2f}\n"

        text = (
            f"📋 <b>持仓 #{idx} 详情</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"❓ <b>{_escape(question)}</b>\n\n"
            f"🎯 方向: {_escape(outcome)}\n"
            f"💰 投入: ${size:.2f}\n"
            f"📊 入场价: ${entry_price:.4f}\n"
            f"{price_line}"
            f"📈 AI概率: {ai_prob:.3f}\n"
            f"🔮 置信度: {confidence:.2f}\n"
            f"📐 EV: {ev:.4f}\n"
            f"📁 赛道: {_escape(category)}\n"
            f"🏷 分组: {_escape(bet_bucket)}\n"
            f"🕐 入场时间: {entered_str} UTC\n"
            f"📅 到期: {_escape(str(end_date))}\n"
        )
        if ai_models and ai_models != "N/A":
            text += f"🤖 模型: {_escape(str(ai_models)[:80])}\n"
        if dimension_tags:
            text += f"🏷 标签: {_escape(str(dimension_tags)[:60])}\n"
        if ai_reasoning:
            text += f"\n💬 <b>AI 分析:</b>\n<i>{_escape(str(ai_reasoning)[:500])}</i>\n"
        if poly_link:
            text += f'\n🔗 <a href="{poly_link}">在 Polymarket 查看</a>\n'
        text += f"\n🆔 <code>{_escape(bet_id[:40])}</code>\n"
        if condition_id:
            text += f"<code>cond: {_escape(condition_id[:40])}</code>\n"

        self._send(chat_id, text)

    def _cmd_sort(self, chat_id: str, args: list) -> None:
        """设置持仓排序方式。"""
        valid_fields = {"pnl", "ev", "time"}
        valid_dirs = {"asc", "desc"}

        if not args:
            current = {"pnl": "盈亏", "ev": "EV", "time": "时间"}.get(
                self._sort_field, self._sort_field
            )
            arrow = "↓ (大→小)" if self._sort_desc else "↑ (小→大)"
            self._send(
                chat_id,
                f"🔀 当前排序: <b>{current} {arrow}</b>\n\n"
                f"用法:\n"
                f"• <code>/sort pnl desc</code> — 按盈亏从大到小\n"
                f"• <code>/sort ev asc</code> — 按EV从小到大\n"
                f"• <code>/sort time</code> — 按时间排序",
            )
            return

        field = args[0].lower()
        if field not in valid_fields:
            self._send(chat_id, f"⚠️ 无效字段: {field}\n可用: pnl, ev, time")
            return

        direction = args[1].lower() if len(args) > 1 else "desc"
        if direction not in valid_dirs:
            direction = "desc"

        self._sort_field = field
        self._sort_desc = direction == "desc"

        label = {"pnl": "盈亏", "ev": "EV", "time": "时间"}[field]
        arrow = "↓ 从大到小" if self._sort_desc else "↑ 从小到大"
        self._send(chat_id, f"✅ 排序已设置: <b>{label} {arrow}</b>")

    def _cmd_pnl(self, chat_id: str, args: list) -> None:
        """返回 PnL 汇总。"""
        try:
            from src.utils.db import (
                connect,
                get_open_paper_bets,
                get_today_settled_pnl,
                get_total_realized_loss,
            )

            today_pnl = get_today_settled_pnl()
            total_loss = get_total_realized_loss()
            open_bets = get_open_paper_bets()
            open_value = sum(b.get("size_usdc", 0) for b in open_bets)

            # 已结算总盈亏
            with connect() as conn:
                row = conn.execute(
                    "SELECT SUM(pnl) FROM paper_bets WHERE resolved_outcome IS NOT NULL"
                ).fetchone()
                total_pnl = float(row[0]) if row and row[0] else 0.0

                # 胜率
                won = conn.execute(
                    "SELECT COUNT(*) FROM paper_bets WHERE resolved_outcome IS NOT NULL AND pnl > 0"
                ).fetchone()[0]
                total_closed = conn.execute(
                    "SELECT COUNT(*) FROM paper_bets WHERE resolved_outcome IS NOT NULL"
                ).fetchone()[0]

            win_rate = (won / total_closed * 100) if total_closed > 0 else 0
            sign_today = "+" if today_pnl >= 0 else ""
            sign_total = "+" if total_pnl >= 0 else ""

            text = (
                f"📈 <b>PnL 汇总</b>\n\n"
                f"📊 今日已结算: {sign_today}${today_pnl:.2f}\n"
                f"📊 累计已结算: {sign_total}${total_pnl:.2f}\n"
                f"🏆 胜率: {win_rate:.1f}% ({won}/{total_closed})\n"
                f"📋 持仓中: {len(open_bets)} 笔 (${open_value:.2f})\n"
                f"📉 累计实亏: ${total_loss:.2f}"
            )
            self._send(chat_id, text)
        except Exception as e:
            self._send(chat_id, f"❌ 查询 PnL 失败: {e}")

    def _cmd_config(self, chat_id: str, args: list) -> None:
        """读取或修改配置值。"""
        if not args:
            self._send(
                chat_id,
                "⚙️ <b>配置管理</b>\n\n"
                "/config list — 查看可修改配置\n"
                "/config get 最低EV — 读取配置\n"
                "/config set 最低EV 0.05 — 修改配置\n\n"
                "💡 支持中文名和点分路径两种方式",
            )
            return

        subcmd = args[0].lower()
        if subcmd == "list":
            self._send(chat_id, self._build_config_cheatsheet())
        elif subcmd == "get" and len(args) >= 2:
            key_input = " ".join(args[1:])
            resolved = self._resolve_config_key(key_input)
            self._config_get(chat_id, resolved)
        elif subcmd == "set" and len(args) >= 3:
            key_input = args[1]
            resolved = self._resolve_config_key(key_input)
            value_str = " ".join(args[2:])
            self._config_set_confirm(chat_id, resolved, value_str)
        else:
            self._send(
                chat_id,
                "⚠️ 用法:\n"
                "/config list\n"
                "/config get <名称>\n"
                "/config set <名称> <值>",
            )

    def _config_get(self, chat_id: str, key_path: str) -> None:
        """读取配置值。"""
        val = get(key_path)
        if val is None:
            self._send(chat_id, f"⚠️ 配置项 <code>{_escape(key_path)}</code> 不存在")
            return

        friendly = _PATH_TO_FRIENDLY.get(key_path, "")
        label = f"{friendly} ({key_path})" if friendly else key_path

        if isinstance(val, dict):
            formatted = json.dumps(val, indent=2, ensure_ascii=False)
            if len(formatted) > 3500:
                formatted = formatted[:3500] + "\n..."
            self._send(
                chat_id,
                f"⚙️ <b>{_escape(label)}</b>\n<pre>{_escape(formatted)}</pre>",
            )
        else:
            self._send(
                chat_id,
                f"⚙️ <b>{_escape(label)}</b>\n值: <code>{_escape(str(val))}</code>",
            )

    def _config_set_confirm(self, chat_id: str, key_path: str, value_str: str) -> None:
        """发送配置修改确认按钮。"""
        # 验证 section
        top = key_path.split(".")[0]
        if top not in _ALLOWED_CONFIG_SECTIONS:
            self._send(chat_id, f"❌ 不允许修改 section: {_escape(top)}")
            return

        # 验证黑名单
        leaf = key_path.split(".")[-1].lower()
        for bk in _BLOCKED_CONFIG_KEYS:
            if bk in leaf:
                self._send(
                    chat_id,
                    f"🔒 <code>{_escape(key_path)}</code> 是受保护参数，不可修改",
                )
                return

        # 获取当前值
        current = get(key_path)
        if current is None:
            self._send(chat_id, f"⚠️ 配置项 <code>{_escape(key_path)}</code> 不存在")
            return

        # 存储待确认信息
        cb_data = f"cfgset_confirm:{key_path}:{value_str}"
        if len(cb_data) > 64:
            # Telegram callback_data 限制 64 字节
            cb_key = f"cfgset_{int(time.time())}"
            self._pending_callbacks[cb_key] = {
                "key_path": key_path,
                "value_str": value_str,
            }
            cb_data = f"cfgset_confirm:{cb_key}"

        text = (
            f"⚙️ <b>确认配置修改</b>\n\n"
            f"📝 Key: <code>{_escape(key_path)}</code>\n"
            f"📌 当前值: <code>{_escape(str(current))}</code>\n"
            f"➡️ 新值: <code>{_escape(value_str)}</code>"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ 确认修改", "callback_data": cb_data},
                    {"text": "❌ 取消", "callback_data": "cfgset_cancel"},
                ]
            ]
        }
        self._send(chat_id, text, reply_markup=keyboard)

    def _execute_config_set(self, chat_id: str, callback_data: str) -> None:
        """执行配置修改。"""
        # 解析 callback_data
        parts = callback_data.split(":", 2)
        if len(parts) < 2:
            self._send(chat_id, "❌ 无效的回调数据")
            return

        cb_key = parts[1]

        # 尝试从 pending_callbacks 获取
        if cb_key in self._pending_callbacks:
            info = self._pending_callbacks.pop(cb_key)
            key_path = info["key_path"]
            value_str = info["value_str"]
        elif len(parts) >= 3:
            key_path = parts[1]
            value_str = parts[2]
        else:
            self._send(chat_id, "❌ 确认已过期，请重新执行 /config set")
            return

        # 解析值类型
        current = get(key_path)
        try:
            new_val = _parse_value(value_str, current)
        except ValueError as e:
            self._send(chat_id, f"❌ 值解析失败: {e}")
            return

        # 备份 + 写入
        try:
            backup_config()
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            keys = key_path.split(".")
            target = cfg
            for k in keys[:-1]:
                if isinstance(target, dict) and k in target:
                    target = target[k]
                else:
                    self._send(
                        chat_id, f"❌ 路径 <code>{_escape(key_path)}</code> 不存在"
                    )
                    return

            leaf_key = keys[-1]
            if not isinstance(target, dict) or leaf_key not in target:
                self._send(chat_id, f"❌ Key <code>{_escape(leaf_key)}</code> 不存在")
                return

            old_val = target[leaf_key]
            target[leaf_key] = new_val
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
                f.write("\n")

            logger.info(
                f"[TelegramBot] 配置已修改: {key_path} = {old_val!r} → {new_val!r}"
            )
            self._send(
                chat_id,
                f"✅ 配置已修改\n"
                f"<code>{_escape(key_path)}</code>\n"
                f"{_escape(str(old_val))} → {_escape(str(new_val))}\n\n"
                f"ℹ️ 热加载将在下一轮自动生效",
            )
        except Exception as e:
            logger.error(f"[TelegramBot] 配置修改失败: {e}")
            self._send(chat_id, f"❌ 配置修改失败: {e}")

    def _cmd_restart(self, chat_id: str, args: list) -> None:
        """发送重启确认按钮。"""
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ 确认重启", "callback_data": "restart_confirm"},
                    {"text": "❌ 取消", "callback_data": "restart_cancel"},
                ]
            ]
        }
        self._send(
            chat_id,
            "🔄 <b>确认重启系统？</b>\n\n"
            "系统将退出并由 systemd 自动重新启动。\n"
            "预计停机时间: ~10 秒",
            reply_markup=keyboard,
        )

    def _cmd_resume(self, chat_id: str, args: list) -> None:
        """T39: 解除风控暂停（人工 resume，符合 P1-6 规则）。"""
        try:
            from src.utils.db import get_all_pause_state

            state = get_all_pause_state()
            paused = state.get("globally_paused", "false").lower() == "true"
            reason = state.get("pause_reason", "") or "unknown"

            if not paused:
                self._send(chat_id, "✅ 系统当前未处于暂停状态，无需恢复。")
                return

            # 直接调用 RiskMonitor.resume()（in-process）
            if self._monitor is not None:
                self._monitor.resume()
                self._send(
                    chat_id,
                    f"▶️ <b>风控暂停已解除</b>\n\n"
                    f"原因: {reason}\n"
                    f"状态: 已恢复正常投注\n\n"
                    f"⚠️ 风控参数不变，若触发条件再次满足会重新暂停。",
                )
                logger.warning(
                    f"[TelegramBot] 人工 /resume 解除风控暂停 (原因: {reason})"
                )
            else:
                self._send(
                    chat_id, "❌ RiskMonitor 实例不可用，请通过 Dashboard 恢复。"
                )
        except Exception as e:
            logger.error(f"[TelegramBot] /resume 失败: {e}")
            self._send(chat_id, f"❌ 恢复失败: {e}")

    # ── 速率限制 ──────────────────────────────────────────

    def _check_rate_limit(self, chat_id: str) -> bool:
        """检查命令速率是否超限。"""
        cfg = get("telegram_bot") or {}
        limit = cfg.get("command_rate_limit", 20)
        now = time.time()
        window = 60  # 1 minute window

        bucket = self._rate_buckets[chat_id]
        # 清除过期记录
        self._rate_buckets[chat_id] = [t for t in bucket if now - t < window]
        bucket = self._rate_buckets[chat_id]

        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    # ── Telegram API 调用 ─────────────────────────────────

    def _send(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
    ) -> bool:
        """发送消息到 Telegram。"""
        if len(text) > 4000:
            text = text[:4000] + "\n...(截断)"
        try:
            url = f"{_TELEGRAM_API.format(token=self._token)}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            if reply_markup:
                payload["reply_markup"] = json.dumps(reply_markup)
            resp = requests.post(url, json=payload, timeout=8)
            if resp.status_code != 200:
                logger.warning(
                    f"[TelegramBot] 发送失败: HTTP {resp.status_code} — {resp.text[:200]}"
                )
                return False
            return True
        except Exception as e:
            logger.warning(f"[TelegramBot] 发送异常: {e}")
            return False

    def _answer_callback(self, callback_id: str) -> None:
        """应答 callback_query（移除按钮加载状态）。"""
        try:
            url = f"{_TELEGRAM_API.format(token=self._token)}/answerCallbackQuery"
            requests.post(url, json={"callback_query_id": callback_id}, timeout=5)
        except Exception:
            pass


# ── 工具函数 ──────────────────────────────────────────────


def _escape(text: str) -> str:
    """HTML 实体转义。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parse_value(value_str: str, reference):
    """根据参考值类型解析字符串为对应 Python 类型。"""
    if isinstance(reference, bool):
        lower = value_str.strip().lower()
        if lower in ("true", "1", "yes", "on"):
            return True
        elif lower in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"布尔值应为 true/false，收到: {value_str}")
    elif isinstance(reference, int):
        return int(value_str)
    elif isinstance(reference, float):
        return float(value_str)
    elif isinstance(reference, list):
        try:
            parsed = json.loads(value_str)
            if not isinstance(parsed, list):
                raise ValueError("需要 JSON 数组格式")
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析失败: {e}")
    elif isinstance(reference, dict):
        try:
            parsed = json.loads(value_str)
            if not isinstance(parsed, dict):
                raise ValueError("需要 JSON 对象格式")
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析失败: {e}")
    else:
        return value_str


# ── 单例 ─────────────────────────────────────────────────

_bot_instance: TelegramBot | None = None
_bot_lock = threading.Lock()


def get_bot() -> TelegramBot:
    """获取全局 TelegramBot 单例。"""
    global _bot_instance
    if _bot_instance is None:
        with _bot_lock:
            if _bot_instance is None:
                _bot_instance = TelegramBot()
    return _bot_instance
