"""
dashboard.py — Polymarket 多赛道自动投注系统 · 终端实时仪表盘
使用 rich 库渲染，每 60 秒自动刷新，直接读 SQLite + /proc。

面板列表：
  1. Header — 标题 + 模式 + 状态 + 资金 + 进度
  2. System — CPU / RAM / Disk / 系统运行时间 / Bot进程 / API统计
  3. Track  — 按赛道分轨统计 (weather/sports/crypto/geopolitics)
  4. Summary — 总体统计
  5. Open    — 开放仓位（含赛道图标）
  6. Weather — 天气情报（仅有天气仓位时显示）
  7. Recent  — 最近投注（含赛道图标）
  8. EV Cal  — EV 校准表
  9. Daily   — 每日 P&L 横柱图

用法：
    cd polymarket_bot
    python3 scripts/dashboard.py [--interval 60]

快捷键：Ctrl+C 退出
"""

import argparse
import os
import re as _re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from rich.console import Console, Group
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from src.utils.config_loader import get
from src.utils.db import _DB_PATH, connect, get_open_paper_bets, get_paper_summary

console = Console()

BAR_MAX_WIDTH = 20
PID_FILE = ROOT / "logs" / "paper_trading.pid"
SYSTEMD_SERVICE = "polymarket-bot"

# 赛道图标映射
TRACK_ICONS = {
    "weather": "🌤",
    "sports": "⚽",
    "crypto": "₿",
    "geopolitics": "🌍",
    "default": "📦",
    "other": "📦",
}


# =============================================================================
# 系统监控 — 纯 Linux /proc，无外部依赖
# =============================================================================


def get_cpu_percent() -> float | None:
    """读取 /proc/stat 计算瞬时 CPU（双采样 150ms）。"""
    try:

        def _read():
            with open("/proc/stat") as f:
                parts = f.readline().split()
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return idle, sum(vals)

        idle1, total1 = _read()
        time.sleep(0.15)
        idle2, total2 = _read()
        d_total = total2 - total1
        if d_total == 0:
            return 0.0
        return round((1.0 - (idle2 - idle1) / d_total) * 100, 1)
    except Exception:
        return None


def get_memory_info() -> dict:
    """从 /proc/meminfo 读取 RAM 使用。"""
    try:
        with open("/proc/meminfo") as f:
            info = {}
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    info[k.strip()] = int(v.strip().split()[0])
        total_mb = info.get("MemTotal", 0) / 1024
        avail_mb = info.get("MemAvailable", 0) / 1024
        used_mb = total_mb - avail_mb
        pct = used_mb / total_mb * 100 if total_mb > 0 else 0
        return {
            "total_mb": total_mb,
            "used_mb": used_mb,
            "avail_mb": avail_mb,
            "pct": pct,
        }
    except Exception:
        return {}


def get_disk_info() -> dict:
    """os.statvfs 获取磁盘使用。"""
    try:
        s = os.statvfs("/")
        total_gb = s.f_blocks * s.f_frsize / (1024**3)
        free_gb = s.f_bavail * s.f_frsize / (1024**3)
        used_gb = total_gb - free_gb
        pct = used_gb / total_gb * 100 if total_gb > 0 else 0
        return {
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": free_gb,
            "pct": pct,
        }
    except Exception:
        return {}


def get_system_uptime() -> str:
    """从 /proc/uptime 读取系统运行时间。"""
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        d = int(secs // 86400)
        h = int((secs % 86400) // 3600)
        m = int((secs % 3600) // 60)
        if d > 0:
            return f"{d}天{h}时{m}分"
        elif h > 0:
            return f"{h}时{m}分"
        return f"{m}分"
    except Exception:
        return "N/A"


def _find_bot_pid() -> int | None:
    """找到 bot 主进程 PID：优先 systemd → 回退 PID 文件 → 回退 ps grep。"""
    # 1. systemd: systemctl show --property=MainPID
    try:
        out = subprocess.check_output(
            ["systemctl", "show", SYSTEMD_SERVICE, "--property=MainPID"],
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out.startswith("MainPID="):
            pid = int(out.split("=", 1)[1])
            if pid > 0:
                os.kill(pid, 0)  # 确认存活
                return pid
    except (
        subprocess.SubprocessError,
        ValueError,
        ProcessLookupError,
        FileNotFoundError,
    ):
        pass
    # 2. PID 文件
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError):
            pass
    # 3. ps grep fallback
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "run_paper_trading"],
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            pid = int(out.splitlines()[0])
            return pid
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        pass
    return None


def get_bot_process_info() -> dict:
    """获取 bot 进程：PID / 内存 / 运行时间。"""
    info = {"running": False, "pid": "N/A", "mem_mb": 0, "uptime": "N/A"}
    pid = _find_bot_pid()
    if pid is None:
        return info
    info["running"] = True
    info["pid"] = str(pid)
    try:
        status_path = f"/proc/{pid}/status"
        if os.path.exists(status_path):
            with open(status_path) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        info["mem_mb"] = round(int(line.split()[1]) / 1024, 1)
                        break
        stat_path = f"/proc/{pid}/stat"
        if os.path.exists(stat_path):
            with open(stat_path) as f:
                fields = f.read().split()
            starttime_ticks = int(fields[21])
            clk_tck = os.sysconf("SC_CLK_TCK")
            with open("/proc/uptime") as f:
                sys_up = float(f.read().split()[0])
            proc_secs = sys_up - (starttime_ticks / clk_tck)
            if proc_secs > 0:
                h = int(proc_secs // 3600)
                m = int((proc_secs % 3600) // 60)
                info["uptime"] = f"{h}h{m:02d}m" if h > 0 else f"{m}m"
    except (ValueError, FileNotFoundError, IndexError):
        pass
    return info


def _find_log_path() -> Path | None:
    """找到活跃日志文件：bot.log（systemd写入） > paper_trading.log。"""
    for name in ("bot.log", "paper_trading.log"):
        p = ROOT / "logs" / name
        if p.exists():
            return p
    return None


def get_log_stats() -> dict:
    """从日志解析近1h API 调用 + 最近错误。"""
    stats = {"ok": 0, "rate_limit": 0, "errors": 0, "last_ok": None, "last_error": None}
    log_path = _find_log_path()
    if not log_path:
        return stats
    try:
        lines = log_path.read_text(errors="replace").splitlines()[-500:]
    except Exception:
        return stats
    cutoff = time.time() - 3600
    ts_pat = _re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
    for line in lines:
        ts_m = ts_pat.search(line)
        if not ts_m:
            continue
        try:
            dt = datetime.strptime(ts_m.group(1), "%Y-%m-%d %H:%M:%S")
            if dt.timestamp() < cutoff:
                continue
        except ValueError:
            continue
        lower = line.lower()
        if "200" in line and ("ok" in lower or "success" in lower or "fetch" in lower):
            stats["ok"] += 1
            stats["last_ok"] = dt.strftime("%H:%M:%S")
        elif "429" in line or "rate" in lower:
            stats["rate_limit"] += 1
        elif "error" in lower or "exception" in lower:
            stats["errors"] += 1
            stats["last_error"] = dt.strftime("%H:%M:%S")
    return stats


def get_last_cycle_duration() -> float | None:
    """从日志推算上轮循环耗时（秒）。"""
    log_path = _find_log_path()
    if not log_path:
        return None
    try:
        lines = log_path.read_text(errors="replace").splitlines()[-300:]
    except Exception:
        return None
    ts_pat = _re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
    starts, ends = [], []
    for line in lines:
        ts_m = ts_pat.search(line)
        if not ts_m:
            continue
        if "轮开始" in line:
            starts.append(ts_m.group(1))
        elif "轮完成" in line:
            ends.append(ts_m.group(1))
    if starts and ends:
        try:
            s = datetime.strptime(starts[-1], "%Y-%m-%d %H:%M:%S")
            e = datetime.strptime(ends[-1], "%Y-%m-%d %H:%M:%S")
            dur = (e - s).total_seconds()
            return dur if dur >= 0 else None
        except ValueError:
            pass
    return None


# =============================================================================
# 数据获取
# =============================================================================


def is_process_running() -> tuple[bool, str]:
    pid = _find_bot_pid()
    if pid is not None:
        return True, str(pid)
    return False, "N/A"


def load_recent_bets(limit: int = 12) -> list[dict]:
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_bets ORDER BY ts_entered DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_next_cycle_info() -> tuple[int | None, int]:
    log_path = _find_log_path()
    if not log_path:
        return None, 0
    try:
        lines = log_path.read_text(errors="replace").splitlines()[-300:]
    except Exception:
        return None, 0
    wait_pat = _re.compile(r"等待 (\d+) 秒")
    ts_pat = _re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
    cycle_pat = _re.compile(r"第 (\d+) 轮")
    last_wait = None
    last_wait_dt = None
    last_cycle = 0
    for line in reversed(lines):
        if last_wait is None:
            m = wait_pat.search(line)
            if m:
                last_wait = int(m.group(1))
                ts_m = ts_pat.search(line)
                if ts_m:
                    last_wait_dt = datetime.strptime(ts_m.group(1), "%Y-%m-%d %H:%M:%S")
        m2 = cycle_pat.search(line)
        if m2 and last_cycle == 0:
            last_cycle = int(m2.group(1))
        if last_wait is not None and last_cycle > 0:
            break
    remaining = None
    if last_wait is not None and last_wait_dt is not None:
        elapsed = (datetime.now() - last_wait_dt).total_seconds()
        remaining = max(0, int(last_wait - elapsed))
    return remaining, last_cycle


def load_all_bets_7d() -> list[dict]:
    cutoff = time.time() - 7 * 86400
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_bets WHERE ts_entered >= ? ORDER BY ts_entered",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def calc_daily_pnl(bets: list[dict]) -> list[dict]:
    by_day: dict[str, dict] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "staked": 0.0}
    )
    for bet in bets:
        if bet.get("resolved_at") is None:
            continue
        date_str = datetime.fromtimestamp(
            float(bet["resolved_at"]), tz=timezone.utc
        ).strftime("%m-%d")
        d = by_day[date_str]
        d["wins"] += int(bet.get("is_win") or 0)
        d["losses"] += 1 - int(bet.get("is_win") or 0)
        d["pnl"] += float(bet.get("pnl") or 0)
        d["staked"] += float(bet.get("size_usdc") or 0)
    return [{"date": k, **v} for k, v in sorted(by_day.items())]


def calc_ev_calibration(bets: list[dict]) -> list[dict]:
    buckets: dict[str, list] = defaultdict(list)
    for bet in bets:
        if bet.get("is_win") is None:
            continue
        ev = float(bet.get("ev") or 0)
        if ev < 0.02:
            key = "< 2%"
        elif ev < 0.04:
            key = "2~4%"
        elif ev < 0.06:
            key = "4~6%"
        elif ev < 0.10:
            key = "6~10%"
        else:
            key = "> 10%"
        buckets[key].append(bet)
    result = []
    for key in ["< 2%", "2~4%", "4~6%", "6~10%", "> 10%"]:
        group = buckets.get(key, [])
        if not group:
            continue
        wins = sum(1 for b in group if b.get("is_win") == 1)
        staked = sum(float(b.get("size_usdc") or 0) for b in group)
        pnl = sum(float(b.get("pnl") or 0) for b in group)
        result.append(
            {
                "ev_range": key,
                "count": len(group),
                "win_rate": wins / len(group) if group else 0.0,
                "roi": pnl / staked if staked > 0 else 0.0,
            }
        )
    return result


def calc_track_stats() -> list[dict]:
    """按赛道(category)汇总投注统计。"""
    try:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(category, 'other') as track,
                    COUNT(*) as total,
                    SUM(CASE WHEN is_win IS NOT NULL THEN 1 ELSE 0 END) as resolved,
                    SUM(CASE WHEN is_win = 1 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN is_win = 0 THEN 1 ELSE 0 END) as losses,
                    ROUND(SUM(COALESCE(pnl, 0.0)), 2) as pnl,
                    ROUND(SUM(COALESCE(size_usdc, 0.0)), 2) as staked,
                    ROUND(AVG(CASE WHEN ev > 0 THEN ev ELSE NULL END), 4) as avg_ev
                FROM paper_bets
                GROUP BY COALESCE(category, 'other')
                ORDER BY total DESC
            """
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["win_rate"] = d["wins"] / d["resolved"] if d["resolved"] > 0 else 0
                d["roi"] = d["pnl"] / d["staked"] if d["staked"] > 0 else 0
                result.append(d)
            return result
    except Exception:
        return []


# =============================================================================
# Rich 渲染组件
# =============================================================================


def _bar(pct: float, width: int = 12, fill_style: str = "green") -> Text:
    """百分比进度条。"""
    filled = min(int(pct / 100 * width), width)
    t = Text()
    t.append("█" * filled, style=fill_style)
    t.append("░" * (width - filled), style="dim")
    return t


def make_header(
    summary: dict,
    open_bets: list,
    running: bool,
    pid: str,
    next_secs: int | None = None,
    cycle_num: int = 0,
) -> Panel:
    """顶部标题栏：模式 + 运行状态 + 资金 + 进度。"""
    pt_cfg = get("paper_trading") or {}
    dry_run = get("general.dry_run")
    if dry_run is None:
        dry_run = True

    target = pt_cfg.get("target_bets", 1000)
    max_open = pt_cfg.get("max_open_positions", 20)
    initial = float(pt_cfg.get("initial_balance", 100.0))
    total = summary.get("total", 0)
    pnl = summary.get("total_pnl", 0.0)
    locked = sum(float(b.get("size_usdc", 0)) for b in open_bets)
    balance = round(initial + pnl - locked, 2)
    total_value = round(initial + pnl, 2)
    util_pct = locked / initial * 100 if initial > 0 else 0

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status_icon = "🟢 运行中" if running else "🔴 未运行"
    status_style = "green bold" if running else "red bold"

    if dry_run:
        mode_tag = "🧪 模拟测试"
        mode_style = "yellow bold"
    else:
        mode_tag = "💰 实盘运行"
        mode_style = "red bold"

    progress_pct = total / target * 100 if target > 0 else 0
    bar_done = int(progress_pct / 5)
    bar_todo = 20 - bar_done

    pnl_style = "green bold" if pnl >= 0 else "red bold"
    tvstyle = "green bold" if total_value >= initial else "red bold"
    util_bars = min(int(util_pct / 5), 20)

    line1 = Text()
    line1.append("  ⚡ Polymarket 多赛道自动投注系统", style="bold cyan")
    line1.append("  ")
    line1.append(mode_tag, style=mode_style)
    line1.append(f"{'':>8}{now_str}", style="dim")

    line2 = Text()
    line2.append("  状态: ")
    line2.append(status_icon, style=status_style)
    line2.append(
        f"  (PID {pid})  轮次: {cycle_num}  进度: {total}/{target}笔  ", style="dim"
    )
    line2.append("█" * bar_done, style="green")
    line2.append("░" * bar_todo, style="dim")
    line2.append(f"  {progress_pct:.0f}%")
    if next_secs is not None:
        m, s = divmod(next_secs, 60)
        line2.append(f"   下一轮: {m}分{s:02d}秒后", style="cyan dim")
    else:
        line2.append("   下一轮: 计算中...", style="cyan dim")

    line3 = Text()
    line3.append("  资金总值: ")
    line3.append(f"{total_value:.2f}U", style=tvstyle)
    line3.append("  │  可用: ", style="dim")
    line3.append(f"{balance:.2f}U", style="white bold")
    line3.append("  已部署: ", style="dim")
    line3.append(f"{locked:.1f}U ", style="yellow bold")
    line3.append("█" * util_bars, style="yellow")
    line3.append("░" * (20 - util_bars), style="dim")
    line3.append(f" {util_pct:.0f}%  ", style="yellow")
    line3.append(f"({len(open_bets)}/{max_open}仓)", style="dim")
    line3.append("  P&L: ", style="dim")
    line3.append(f"{pnl:+.2f}U", style=pnl_style)

    return Panel(Group(line1, line2, line3), border_style="cyan", padding=(0, 1))


def make_system_panel() -> Panel:
    """🖥 系统 / VPS 运行状态面板。"""
    cpu = get_cpu_percent()
    mem = get_memory_info()
    disk = get_disk_info()
    uptime_str = get_system_uptime()
    bot = get_bot_process_info()
    log_st = get_log_stats()
    cycle_dur = get_last_cycle_duration()

    t = Table.grid(padding=(0, 2))
    t.add_column(width=30)
    t.add_column(width=30)
    t.add_column(width=30)

    # Row 1: CPU / RAM / Disk
    cpu_text = Text()
    cpu_text.append("CPU  ", style="dim")
    if cpu is not None:
        cpu_style = "green" if cpu < 50 else ("yellow" if cpu < 80 else "red bold")
        cpu_text.append(f"{cpu:5.1f}%  ", style=cpu_style)
        cpu_text.append_text(_bar(cpu, 10, cpu_style))
    else:
        cpu_text.append("N/A", style="dim")

    mem_text = Text()
    mem_text.append("RAM  ", style="dim")
    if mem:
        mp = mem["pct"]
        ms = "green" if mp < 60 else ("yellow" if mp < 85 else "red bold")
        mem_text.append(f"{mp:5.1f}%  ", style=ms)
        mem_text.append_text(_bar(mp, 10, ms))
        mem_text.append(f"  {mem['used_mb']:.0f}/{mem['total_mb']:.0f}MB", style="dim")
    else:
        mem_text.append("N/A", style="dim")

    disk_text = Text()
    disk_text.append("Disk ", style="dim")
    if disk:
        dp = disk["pct"]
        ds = "green" if dp < 70 else ("yellow" if dp < 90 else "red bold")
        disk_text.append(f"{dp:5.1f}%  ", style=ds)
        disk_text.append_text(_bar(dp, 10, ds))
        disk_text.append(f"  {disk['free_gb']:.0f}GB可用", style="dim")
    else:
        disk_text.append("N/A", style="dim")

    t.add_row(cpu_text, mem_text, disk_text)

    # Row 2: Uptime / Bot Process / API
    up_text = Text()
    up_text.append("系统运行  ", style="dim")
    up_text.append(uptime_str, style="cyan")

    bot_text = Text()
    bot_text.append("Bot进程  ", style="dim")
    if bot["running"]:
        bot_text.append(f"PID:{bot['pid']}", style="green")
        bot_text.append(f"  {bot['mem_mb']}MB", style="cyan")
        bot_text.append(f"  ⏱{bot['uptime']}", style="dim")
    else:
        bot_text.append("未运行", style="red")

    api_text = Text()
    api_text.append("API(1h)  ", style="dim")
    api_text.append(f"✓{log_st['ok']}", style="green")
    if log_st["rate_limit"] > 0:
        api_text.append(f"  ⚠{log_st['rate_limit']}", style="yellow")
    if log_st["errors"] > 0:
        api_text.append(f"  ✗{log_st['errors']}", style="red")
    if log_st["last_ok"]:
        api_text.append(f"  @{log_st['last_ok']}", style="dim")

    t.add_row(up_text, bot_text, api_text)

    # Row 3: Cycle duration
    cyc_text = Text()
    cyc_text.append("上轮耗时  ", style="dim")
    if cycle_dur is not None:
        cs = "green" if cycle_dur < 60 else ("yellow" if cycle_dur < 180 else "red")
        cyc_text.append(f"{cycle_dur:.0f}秒", style=cs)
    else:
        cyc_text.append("N/A", style="dim")

    t.add_row(cyc_text, Text(""), Text(""))

    return Panel(
        t, title="[bold]🖥 系统状态[/bold]", border_style="bright_black", padding=(0, 1)
    )


def make_track_panel(track_stats: list[dict]) -> Panel:
    """📊 赛道分轨统计面板。"""
    t = Table(
        show_header=True,
        header_style="bold magenta",
        show_lines=False,
        padding=(0, 1),
        expand=True,
    )
    t.add_column("赛道", width=14)
    t.add_column("投注", width=5, justify="right")
    t.add_column("已结", width=5, justify="right")
    t.add_column("赢/输", width=9, justify="right")
    t.add_column("胜率", width=6, justify="right")
    t.add_column("P&L", width=8, justify="right")
    t.add_column("ROI", width=7, justify="right")
    t.add_column("平均EV", width=7, justify="right")
    t.add_column("总投入", width=8, justify="right")

    if not track_stats:
        t.add_row("[dim]暂无数据...[/dim]", "", "", "", "", "", "", "", "")
    else:
        totals = {
            "bets": 0,
            "resolved": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "staked": 0.0,
        }

        for row in track_stats:
            icon = TRACK_ICONS.get(row["track"], "📦")
            name = Text(f"{icon} {row['track']}", style="bold")
            wins = row.get("wins", 0)
            losses = row.get("losses", 0)
            wr = row.get("win_rate", 0)
            pnl = row.get("pnl", 0)
            roi = row.get("roi", 0)
            avg_ev = row.get("avg_ev") or 0
            staked = row.get("staked", 0)
            resolved = row.get("resolved", 0)
            total_bets = row.get("total", 0)

            wr_s = "green" if wr >= 0.55 else ("yellow" if wr >= 0.45 else "red")
            pnl_s = "green" if pnl >= 0 else "red"
            roi_s = "green" if roi >= 0 else "red"

            wl = Text()
            wl.append(f"{wins}✅", style="green")
            wl.append(f"{losses}❌", style="red")

            t.add_row(
                name,
                str(total_bets),
                str(resolved),
                wl,
                (
                    Text(f"{wr:.0%}", style=wr_s)
                    if resolved > 0
                    else Text("—", style="dim")
                ),
                Text(f"{pnl:+.2f}", style=pnl_s),
                (
                    Text(f"{roi:.1%}", style=roi_s)
                    if staked > 0
                    else Text("—", style="dim")
                ),
                (
                    Text(f"{avg_ev:.1%}", style="green" if avg_ev >= 0.05 else "yellow")
                    if avg_ev > 0
                    else Text("—", style="dim")
                ),
                f"{staked:.1f}U",
            )

            totals["bets"] += total_bets
            totals["resolved"] += resolved
            totals["wins"] += wins
            totals["losses"] += losses
            totals["pnl"] += pnl
            totals["staked"] += staked

        # 合计行（多赛道时）
        if len(track_stats) > 1:
            t.add_row(
                Text("─" * 12, style="dim"),
                Text("─" * 5, style="dim"),
                Text("─" * 5, style="dim"),
                Text("─" * 9, style="dim"),
                Text("─" * 6, style="dim"),
                Text("─" * 8, style="dim"),
                Text("─" * 7, style="dim"),
                Text("─" * 7, style="dim"),
                Text("─" * 8, style="dim"),
            )
            total_wr = (
                totals["wins"] / totals["resolved"] if totals["resolved"] > 0 else 0
            )
            total_roi = totals["pnl"] / totals["staked"] if totals["staked"] > 0 else 0
            twl = Text()
            twl.append(f"{totals['wins']}✅", style="green")
            twl.append(f"{totals['losses']}❌", style="red")
            t.add_row(
                Text("📊 合计", style="bold white"),
                Text(str(totals["bets"]), style="bold"),
                Text(str(totals["resolved"]), style="bold"),
                twl,
                Text(f"{total_wr:.0%}", style="bold"),
                Text(
                    f"{totals['pnl']:+.2f}",
                    style="green bold" if totals["pnl"] >= 0 else "red bold",
                ),
                (
                    Text(f"{total_roi:.1%}", style="bold")
                    if totals["staked"] > 0
                    else Text("—")
                ),
                Text(""),
                Text(f"{totals['staked']:.1f}U", style="bold"),
            )

    return Panel(
        t, title="[bold]📊 赛道分轨统计[/bold]", border_style="magenta", padding=(0, 0)
    )


def make_summary_panel(summary: dict) -> Panel:
    """总体统计。"""
    total = summary.get("total", 0)
    resolved = summary.get("resolved", 0)
    wins = summary.get("wins", 0)
    losses = summary.get("losses", 0)
    win_rate = summary.get("win_rate", 0.0)
    pnl = summary.get("total_pnl", 0.0)
    roi = summary.get("roi", 0.0)
    staked = summary.get("total_staked", 0.0)

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", width=12)
    t.add_column(style="bold", width=14)
    t.add_row("总投注", f"{total} 笔")
    t.add_row("已解决", f"{resolved} 笔")
    ws = Text()
    ws.append(f"✅ {wins} 赢", style="green")
    ws.append("  ")
    ws.append(f"❌ {losses} 输", style="red")
    t.add_row("赢/输", ws)
    t.add_row(
        "胜率",
        Text(
            f"{win_rate:.1%}", style="green bold" if win_rate >= 0.5 else "yellow bold"
        ),
    )
    t.add_row(
        "已实现P&L",
        Text(f"{pnl:+.2f} USDC", style="green bold" if pnl >= 0 else "red bold"),
    )
    t.add_row("ROI", Text(f"{roi:.1%}", style="green bold" if roi >= 0 else "red bold"))
    t.add_row("总投入", f"{staked:.1f} USDC")

    return Panel(
        t, title="[bold]📊 总体统计[/bold]", border_style="blue", padding=(0, 1)
    )


def make_open_positions_panel(open_bets: list[dict]) -> Panel:
    """开放仓位面板（含赛道图标）。"""
    pt_cfg = get("profit_taking") or {}
    sl_cfg = get("stop_loss") or {}
    near_ceiling = pt_cfg.get("near_ceiling_threshold", 0.97)
    profit_take_pct = pt_cfg.get("profit_take_pct", 0.80)
    floor_price = sl_cfg.get("floor_price", 0.07)
    standard_stop_pct = sl_cfg.get("standard_stop_pct", 0.70)

    t = Table(
        show_header=True,
        header_style="bold yellow",
        show_lines=False,
        padding=(0, 1),
        expand=True,
    )
    t.add_column("赛道", width=4)
    t.add_column("桶", width=3)
    t.add_column("持仓", width=5, style="dim")
    t.add_column("市场问题", min_width=36, ratio=1, no_wrap=False)
    t.add_column("方向", width=4)
    t.add_column("入场¢", width=6, justify="right")
    t.add_column("AI概率", width=6, justify="right")
    t.add_column("EV", width=6, justify="right")
    t.add_column("规模", width=5, justify="right")
    t.add_column("到期", width=6, justify="right")

    if not open_bets:
        t.add_row("", "", "[dim]暂无开放仓位[/dim]", "", "", "", "", "", "", "")
    else:
        now = time.time()
        for bet in open_bets:
            ts = float(bet.get("ts_entered") or now)
            held_min = int((now - ts) / 60)
            held_str = (
                f"{held_min}m"
                if held_min < 120
                else f"{held_min // 60}h{held_min % 60}m"
            )

            question = bet.get("question") or ""
            outcome = bet.get("outcome") or "?"
            entry_price = float(bet.get("market_price") or 0.5)
            ai_prob = float(bet.get("ai_probability") or 0.5)
            ev_val = float(bet.get("ev") or 0)
            size = float(bet.get("size_usdc") or 0)

            cat = bet.get("category") or "other"
            track_icon = TRACK_ICONS.get(cat, "📦")

            end_date_str = bet.get("market_end_date") or ""
            expiry_str, expiry_style = "?", "dim"
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    hrs = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hrs < 0:
                        expiry_str, expiry_style = "等待结算", "yellow"
                    elif hrs < 2:
                        expiry_str, expiry_style = f"{int(hrs * 60)}m", "yellow bold"
                    else:
                        expiry_str = f"{hrs:.1f}h"
                        expiry_style = "cyan" if hrs < 12 else "dim"
                except ValueError:
                    pass

            ev_style = (
                "green bold" if ev_val >= 0.05 else ("green" if ev_val > 0 else "red")
            )

            bucket = bet.get("bet_bucket") or "standard"
            if bucket == "A_conviction":
                bc = Text("[A]", style="cyan bold")
            elif bucket == "B_edge":
                bc = Text("[B]", style="green bold")
            elif bucket == "sports":
                bc = Text("[S]", style="yellow bold")
            else:
                bc = Text("", style="dim")

            t.add_row(
                track_icon,
                bc,
                held_str,
                question,
                Text(outcome, style="green" if outcome == "YES" else "red"),
                f"{entry_price:.2f}",
                Text(f"{ai_prob:.0%}", style="cyan"),
                Text(f"{ev_val:.0%}", style=ev_style),
                Text(f"{size:.1f}U", style="yellow"),
                Text(expiry_str, style=expiry_style),
            )

    return Panel(
        t,
        title=f"[bold]📈 开放仓位 ({len(open_bets)})[/bold]",
        border_style="yellow",
        subtitle=(
            f"[dim]止盈: ≥{near_ceiling} / +{int(profit_take_pct * 100)}%  │  "
            f"止损: <{floor_price} / -{int(standard_stop_pct * 100)}%[/dim]"
        ),
        padding=(0, 0),
    )


def make_recent_bets_panel(recent: list[dict]) -> Panel:
    """最近投注记录（含赛道图标）。"""
    t = Table(
        show_header=True,
        header_style="bold cyan",
        show_lines=False,
        padding=(0, 1),
        expand=True,
    )
    t.add_column("赛道", width=4)
    t.add_column("时间", width=5, style="dim")
    t.add_column("市场问题", min_width=36, ratio=1, no_wrap=False)
    t.add_column("预测", width=4)
    t.add_column("入场¢", width=6, justify="right")
    t.add_column("AI%", width=5, justify="right")
    t.add_column("规模", width=5, justify="right")
    t.add_column("结果", width=4)
    t.add_column("EV", width=5, justify="right")
    t.add_column("P&L", width=7, justify="right")

    if not recent:
        t.add_row("", "[dim]暂无记录...[/dim]", "", "", "", "", "", "", "", "")
    else:
        for bet in recent:
            ts = float(bet.get("ts_entered") or time.time())
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M")
            question = bet.get("question") or ""
            outcome = bet.get("outcome") or "?"
            cat = bet.get("category") or "other"
            track_icon = TRACK_ICONS.get(cat, "📦")

            resolved_outcome = bet.get("resolved_outcome")
            is_win = bet.get("is_win")
            if resolved_outcome is None:
                result_cell = Text("⏳", style="dim")
            elif is_win == 1:
                result_cell = Text("✅", style="green")
            else:
                result_cell = Text("❌", style="red")

            ev_val = float(bet.get("ev") or 0)
            pnl = bet.get("pnl")
            pnl_str = (
                Text("—", style="dim")
                if pnl is None
                else Text(
                    f"{float(pnl):+.2f}", style="green" if float(pnl) > 0 else "red"
                )
            )

            entry_price = float(bet.get("market_price") or 0.5)
            ai_prob = float(bet.get("ai_probability") or 0.5)
            size = float(bet.get("size_usdc") or 0)

            t.add_row(
                track_icon,
                time_str,
                question,
                Text(outcome, style="green" if outcome == "YES" else "red"),
                f"{entry_price:.2f}",
                Text(f"{ai_prob:.0%}", style="cyan dim"),
                Text(f"{size:.1f}U", style="yellow"),
                result_cell,
                Text(f"{ev_val:.2%}", style="green" if ev_val > 0.03 else "yellow"),
                pnl_str,
            )

    return Panel(
        t, title="[bold]📋 最近投注[/bold]", border_style="blue", padding=(0, 0)
    )


def _extract_city_from_question(question: str) -> str:
    m = _re.search(
        r"\bin\s+([A-Z][a-zA-Z\s]+?)(?:\s+be\s|\s+on\s|\s+exceed|\s+reach|\?|$)",
        question,
    )
    if m:
        return m.group(1).strip()
    m = _re.search(
        r"(上海|北京|东京|伦敦|纽约|巴黎|首尔|洛杉矶|芝加哥|迈阿密|奥斯汀|达拉斯)",
        question,
    )
    if m:
        return m.group(1)
    return ""


def make_weather_intel_panel(open_bets: list[dict]) -> Panel | None:
    """天气情报面板（仅当有天气仓位时显示，否则返回 None）。"""
    try:
        from src.utils.weather_fetcher import fetch_city_weather
    except ImportError:
        return None

    weather_bets = [b for b in open_bets if (b.get("category") or "") == "weather"]
    if not weather_bets:
        return None

    city_bets: dict[str, list] = defaultdict(list)
    for bet in weather_bets:
        city = _extract_city_from_question(bet.get("question") or "")
        if not city:
            city = "Unknown"
        city_bets[city].append(bet)

    t = Table(show_header=True, header_style="bold blue", padding=(0, 1), expand=True)
    t.add_column("城市", width=10, no_wrap=True)
    t.add_column("当前°C", width=7, justify="right")
    t.add_column("今日预测高", width=9, justify="right")
    t.add_column("活跃仓", width=5, justify="right")
    t.add_column("最近阈值", width=8, justify="right")
    t.add_column("方向", width=4)
    t.add_column("最近到期", width=7, justify="right")

    for city, bets in sorted(city_bets.items()):
        wx = {}
        try:
            wx = fetch_city_weather(city)
        except Exception:
            pass
        current_c = wx.get("temperature_c")
        forecast_max = wx.get("forecast_max_c")
        curr_str = f"{current_c:.1f}°" if current_c is not None else "N/A"
        fmax_str = f"{forecast_max:.1f}°" if forecast_max is not None else "N/A"

        bets_sorted = sorted(bets, key=lambda b: b.get("market_end_date") or "9999")
        nearest = bets_sorted[0]
        thresh_m = _re.search(r"(\d+(?:\.\d+)?)\s*°[Cc]", nearest.get("question") or "")
        thresh_str = f"{thresh_m.group(1)}°C" if thresh_m else "?"
        outcome = nearest.get("outcome") or "?"

        end_date_str = nearest.get("market_end_date") or ""
        expiry_display, expiry_style = "?", "dim"
        if end_date_str:
            try:
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                hrs = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                if hrs < 0:
                    expiry_display, expiry_style = "等待结算", "yellow"
                elif hrs < 2:
                    expiry_display, expiry_style = f"{int(hrs * 60)}m", "yellow bold"
                else:
                    expiry_display = f"{hrs:.1f}h"
                    expiry_style = "cyan" if hrs < 12 else "dim"
            except ValueError:
                pass

        t.add_row(
            city[:10],
            curr_str,
            Text(
                fmax_str, style="yellow bold" if forecast_max is not None else "white"
            ),
            str(len(bets)),
            thresh_str,
            Text(outcome, style="green" if outcome == "YES" else "red"),
            Text(expiry_display, style=expiry_style),
        )

    return Panel(
        t,
        title="[bold]🌡 天气情报[/bold]",
        border_style="blue",
        subtitle="[dim]预测高温 vs 市场阈值 | 缓存10分钟[/dim]",
        padding=(0, 0),
    )


def make_ev_calibration_panel(bets_7d: list[dict]) -> Panel:
    """EV 校准表。"""
    ev_cal = calc_ev_calibration(bets_7d)
    t = Table(show_header=True, header_style="bold magenta", padding=(0, 1))
    t.add_column("EV 区间", width=8)
    t.add_column("数量", width=5, justify="right")
    t.add_column("胜率", width=7, justify="right")
    t.add_column("ROI", width=8, justify="right")
    t.add_column("信号质量", width=10)

    if not ev_cal:
        t.add_row("[dim]待积累数据...[/dim]", "", "", "", "")
    else:
        for row in ev_cal:
            wr = row["win_rate"]
            roi = row["roi"]
            if roi > 0.10:
                q = Text("🟢 优秀", style="green")
            elif roi > 0:
                q = Text("🟡 良好", style="yellow")
            elif roi > -0.05:
                q = Text("🟠 待观察", style="yellow")
            else:
                q = Text("🔴 改进", style="red")
            t.add_row(
                row["ev_range"],
                str(row["count"]),
                Text(
                    f"{wr:.1%}",
                    style=(
                        "green" if wr >= 0.55 else ("yellow" if wr >= 0.45 else "red")
                    ),
                ),
                Text(f"{roi:+.1%}", style="green" if roi > 0 else "red"),
                q,
            )

    return Panel(
        t,
        title="[bold]🎯 EV 校准[/bold]",
        border_style="magenta",
        subtitle="[dim]EV校准: ROI 接近 EV → 信号可信[/dim]",
        padding=(0, 0),
    )


def make_daily_pnl_panel(bets_7d: list[dict]) -> Panel:
    """每日 P&L 横柱图。"""
    daily = calc_daily_pnl(bets_7d)
    if not daily:
        return Panel(
            Text("  暂无已解决记录，等待市场结算...", style="dim"),
            title="[bold]📅 每日 P&L[/bold]",
            border_style="green",
            padding=(0, 1),
        )

    max_abs = max(abs(d["pnl"]) for d in daily) or 1.0
    lines = []
    cum_pnl = 0.0
    for d in daily:
        pnl = d["pnl"]
        cum_pnl += pnl
        bar_len = int(abs(pnl) / max_abs * BAR_MAX_WIDTH)
        bar_len = max(bar_len, 1) if pnl != 0 else 0

        bt = Text(f"  {d['date']}  ", style="dim")
        if pnl >= 0:
            bt.append(f"{'█' * bar_len:<{BAR_MAX_WIDTH}}", style="green")
            bt.append(f"  {pnl:+.2f}  ", style="green bold")
        else:
            bt.append(f"{'':>{BAR_MAX_WIDTH}}  ", style="")
            bt.append(f"{pnl:+.2f}  ", style="red bold")
            bt.append("█" * bar_len, style="red")
            bt.append("  ", style="")
        bt.append(f"(累计: {cum_pnl:+.2f})", style="dim")
        bt.append(f" {d['wins']}✅{d['losses']}❌", style="dim")
        lines.append(bt)

    pnl_total = sum(d["pnl"] for d in daily)
    ts = "green bold" if pnl_total >= 0 else "red bold"

    return Panel(
        Group(*lines),
        title="[bold]📅 每日 P&L (最近7天)[/bold]",
        border_style="green",
        subtitle=f"[{ts}]7日合计: {pnl_total:+.2f} USDC[/{ts}]",
        padding=(0, 1),
    )


def make_footer(interval: int = 60) -> Text:
    dry_run = get("general.dry_run")
    mode = "🧪 模拟模式" if dry_run else "💰 实盘模式"
    ms = "yellow dim" if dry_run else "red dim"

    t = Text(justify="center")
    t.append(f"  {mode}", style=ms)
    t.append("  |  ", style="dim")
    t.append("Ctrl+C 退出", style="dim")
    t.append("  |  ", style="dim")
    t.append(f"刷新: {interval}s", style="dim")
    t.append("  |  ", style="dim")
    t.append("./start_paper_trading.sh status", style="dim cyan")
    t.append("  ", style="dim")
    t.append("./start_paper_trading.sh log", style="dim cyan")
    return t


# =============================================================================
# 主渲染循环
# =============================================================================


def build_dashboard(interval: int = 60) -> Group:
    """构建完整仪表盘布局。"""
    summary = get_paper_summary()
    open_bets = get_open_paper_bets()
    recent = load_recent_bets(12)
    bets_7d = load_all_bets_7d()
    running, pid = is_process_running()
    next_secs, cycle_num = get_next_cycle_info()
    track_stats = calc_track_stats()

    header = make_header(summary, open_bets, running, pid, next_secs, cycle_num)
    system = make_system_panel()
    track = make_track_panel(track_stats)
    summ = make_summary_panel(summary)
    open_p = make_open_positions_panel(open_bets)
    weather = make_weather_intel_panel(open_bets)
    recent_p = make_recent_bets_panel(recent)
    ev = make_ev_calibration_panel(bets_7d)
    daily = make_daily_pnl_panel(bets_7d)
    footer = make_footer(interval)

    panels = [header, system, track, summ, open_p]
    if weather is not None:
        panels.append(weather)
    panels.extend([recent_p, ev, daily, Padding(footer, (0, 1))])

    return Group(*panels)


def run(interval: int = 60) -> None:
    try:
        from src.utils.db import init_db

        init_db()
    except Exception:
        pass

    with Live(
        build_dashboard(interval),
        console=console,
        refresh_per_second=0.3,
        screen=False,
        vertical_overflow="visible",
    ) as live:
        last_refresh = time.time()
        while True:
            try:
                now = time.time()
                if now - last_refresh >= interval:
                    live.update(build_dashboard(interval))
                    last_refresh = now
                time.sleep(1)
            except KeyboardInterrupt:
                break

    console.print("[green]仪表盘已关闭[/green]")
    console.print("[dim]后台进程仍在运行（若已启动）[/dim]")
    console.print("[dim]查看状态: ./start_paper_trading.sh status[/dim]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket 多赛道自动投注仪表盘")
    parser.add_argument("--interval", type=int, default=60, help="刷新间隔（秒）")
    args = parser.parse_args()
    run(args.interval)
    run(args.interval)
    run(args.interval)
