from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.copytrader_v2.official_client import (
    PolymarketOfficialClient,
    get_default_client,
)
from src.services.copytrader_v2.config import get as copytrader_cfg_get
from src.services.copytrader_v2.wallet_analyzer import WalletAnalyzer
from src.executor.clob_client_factory import get_gateway_for_funder
from src.executor.execution_gateway import (
    ExecutionOrderRequest,
    ExecutionOrderType,
    OrderSide,
)
from src.utils.config_loader import is_dry_run as _is_dry_run
from src.utils.database import connect as _pg_connect

LOG_FILE = ROOT / "logs" / "copytrader_v2.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("copytrader_v2.daemon")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)-30s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


_running = True


def _shutdown(signum, frame) -> None:
    global _running
    logger.info("[Daemon] received signal %s, stopping", signum)
    _running = False


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


class CopytraderV2Daemon:
    def __init__(self) -> None:
        self._client: PolymarketOfficialClient = get_default_client()
        self._analyzer = WalletAnalyzer(client=self._client)
        self._last_pool_score_at: float = 0.0
        self._last_task_scan_at: float = 0.0
        self._migrate_pg_schema()

    def _migrate_pg_schema(self) -> None:
        """Idempotent: add T397 smart wallet columns to PostgreSQL if not present."""
        new_cols = [
            ("pnl_realized_usd", "REAL DEFAULT 0"),
            ("win_rate_f", "REAL DEFAULT 0"),
            ("trade_count_30d", "INTEGER DEFAULT 0"),
            ("max_drawdown_usd", "REAL DEFAULT 0"),
        ]
        try:
            with _pg_connect() as conn:
                for col, col_def in new_cols:
                    conn.execute(
                        f"ALTER TABLE copytrader_v2_wallet_pool ADD COLUMN IF NOT EXISTS {col} {col_def}"
                    )
                conn.commit()
            logger.info("[CTV2] pg schema migration OK (T397 columns)")
        except Exception as exc:
            logger.warning("[CTV2] pg schema migration warning: %s", exc)

    def _settings_for(self, owner_wallet_address: str) -> dict[str, Any]:
        defaults = {
            "scan_interval_seconds": 60,
            "max_price_filter": 0.85,
            "min_ev_filter": 0.002,
            "trade_lookback_minutes": 30,
            "follow_sell_enabled": True,
            "trade_mode": "taker",
            "trade_mode_options": ["taker", "maker"],
            "trade_mode_locked": True,
            "follow_multiplier": 1,
            "min_order_size_buffer_shares": 1,
        }
        with _pg_connect() as conn:
            row = conn.execute(
                "SELECT settings_json FROM copytrader_v2_settings WHERE owner_wallet_address=?",
                (owner_wallet_address,),
            ).fetchone()
        if row and row["settings_json"]:
            try:
                stored = json.loads(row["settings_json"])
                if isinstance(stored, dict):
                    return {**defaults, **stored}
            except (TypeError, json.JSONDecodeError):
                pass
        return defaults

    def _load_running_tasks(self) -> list[dict]:
        with _pg_connect() as conn:
            rows = conn.execute(
                "SELECT * FROM copytrader_v2_tasks WHERE status='running'"
            ).fetchall()
        return [dict(r) for r in rows]

    def _resolve_funder_for_owner(self, owner_wallet_address: str) -> str:
        wallet = (owner_wallet_address or "").strip().lower()
        if not wallet.startswith("0x") or len(wallet) != 42:
            return ""
        with _pg_connect() as conn:
            row = conn.execute(
                "SELECT funder_address FROM users WHERE wallet_address=?",
                (wallet,),
            ).fetchone()
        if not row:
            return ""
        funder = str(row["funder_address"] or "").strip().lower()
        if funder.startswith("0x") and len(funder) == 42:
            return funder
        return ""

    def _get_owner_wallet_type(self, owner_wallet_address: str) -> str:
        """Return wallet_type ('safe' | 'deposit_wallet') for an owner.

        T882-FundAudit#2: copytrader auto-trades must NOT be dispatched for
        deposit_wallet users until the Privy EOA + signature_type=3 ordering
        chain is implemented in V2BuilderAdapter. Without this guard the
        copytrader silently signs orders with sig_type=2 + Safe-style funder,
        which the CLOB rejects ("maker address not allowed"), risking repeated
        retries and noisy logs.
        """
        wallet = (owner_wallet_address or "").strip().lower()
        if not wallet.startswith("0x") or len(wallet) != 42:
            return "safe"
        try:
            with _pg_connect() as conn:
                row = conn.execute(
                    "SELECT wallet_type FROM users WHERE wallet_address=?",
                    (wallet,),
                ).fetchone()
        except Exception:
            return "safe"
        if not row:
            return "safe"
        wt = str(row["wallet_type"] or "safe").strip().lower() or "safe"
        if wt not in ("safe", "deposit_wallet"):
            wt = "safe"
        return wt

    def _min_order_size_shares(self, token_id: str) -> int:
        try:
            book = self._client.get_orderbook(token_id)
            raw = float(book.get("min_order_size") or 5)
            if raw <= 0:
                return 5
            return int(math.ceil(raw))
        except Exception:
            return 5

    def _get_position_shares(self, user_wallet: str, token_id: str) -> float:
        try:
            positions = self._client.get_positions(user_wallet, limit=500)
            for p in positions:
                tid = str(
                    p.get("asset")
                    or p.get("token_id")
                    or p.get("tokenID")
                    or p.get("token")
                    or ""
                )
                if tid != token_id:
                    continue
                size = float(p.get("size") or p.get("shares") or 0)
                if size > 0:
                    return size
        except Exception:
            return 0.0
        return 0.0

    def _mirror_buy(self, owner_wallet_address: str, token_id: str, settings: dict[str, Any], task: dict) -> bool:
        funder = self._resolve_funder_for_owner(owner_wallet_address)
        if not funder:
            logger.warning("[CTV2] no funder bound for owner=%s", owner_wallet_address[:10])
            return False
        # T882-FundAudit#2: block deposit_wallet users until sig_type=3 chain ready.
        if self._get_owner_wallet_type(owner_wallet_address) == "deposit_wallet":
            logger.warning(
                "[CTV2] BUY skipped: owner=%s is deposit_wallet (sig_type=3 ordering chain not yet implemented)",
                owner_wallet_address[:10],
            )
            return False
        mode = str(settings.get("trade_mode") or "taker").lower().strip()
        if mode != "taker":
            mode = "taker"

        min_shares = self._min_order_size_shares(token_id)
        multiplier = max(1, min(1000, int(settings.get("follow_multiplier") or 1)))
        buffer = max(0, min(20, int(settings.get("min_order_size_buffer_shares") or 1)))
        desired = max(min_shares * multiplier, min_shares + buffer)
        desired_shares = int(math.ceil(desired))

        gw = get_gateway_for_funder(funder)
        req = ExecutionOrderRequest(
            token_id=token_id,
            price=0.99,
            size=float(desired_shares),
            side=OrderSide.BUY,
            order_type=ExecutionOrderType.FOK,
            signal_id=f"ctv2-buy-{task.get('id')}",
            trace_id=f"ctv2-{int(time.time())}",
        )
        result = gw.place_order(req)
        if not result.success:
            logger.warning(
                "[CTV2] BUY failed task=%s owner=%s token=%s size=%s err=%s",
                task.get("id"),
                owner_wallet_address[:10],
                token_id[:16],
                desired_shares,
                result.error,
            )
            return False
        logger.info(
            "[CTV2] BUY ok task=%s owner=%s token=%s min=%s mult=%s buffer=%s size=%s order=%s",
            task.get("id"),
            owner_wallet_address[:10],
            token_id[:16],
            min_shares,
            multiplier,
            buffer,
            desired_shares,
            result.order_id,
        )
        return True

    def _mirror_sell(self, owner_wallet_address: str, token_id: str, settings: dict[str, Any], task: dict) -> bool:
        if not bool(settings.get("follow_sell_enabled", True)):
            return False
        funder = self._resolve_funder_for_owner(owner_wallet_address)
        if not funder:
            logger.warning("[CTV2] no funder bound for owner=%s", owner_wallet_address[:10])
            return False
        # T882-FundAudit#2: block deposit_wallet users until sig_type=3 chain ready.
        if self._get_owner_wallet_type(owner_wallet_address) == "deposit_wallet":
            logger.warning(
                "[CTV2] SELL skipped: owner=%s is deposit_wallet (sig_type=3 ordering chain not yet implemented)",
                owner_wallet_address[:10],
            )
            return False

        held = self._get_position_shares(funder, token_id)
        if held <= 0:
            return False
        min_shares = self._min_order_size_shares(token_id)
        if held < min_shares:
            logger.info(
                "[CTV2] skip SELL task=%s token=%s held=%.4f below min_order_size=%d",
                task.get("id"),
                token_id[:16],
                held,
                min_shares,
            )
            return False

        # Full-close only: sell entire currently held position.
        sell_size = float(held)
        gw = get_gateway_for_funder(funder)
        req = ExecutionOrderRequest(
            token_id=token_id,
            price=0.01,
            size=sell_size,
            side=OrderSide.SELL,
            order_type=ExecutionOrderType.FOK,
            signal_id=f"ctv2-sell-{task.get('id')}",
            trace_id=f"ctv2-{int(time.time())}",
        )
        result = gw.place_order(req)
        if not result.success:
            logger.warning(
                "[CTV2] SELL failed task=%s owner=%s token=%s size=%.4f err=%s",
                task.get("id"),
                owner_wallet_address[:10],
                token_id[:16],
                sell_size,
                result.error,
            )
            return False
        logger.info(
            "[CTV2] SELL ok task=%s owner=%s token=%s size=%.4f order=%s",
            task.get("id"),
            owner_wallet_address[:10],
            token_id[:16],
            sell_size,
            result.order_id,
        )
        return True

    def _seed_leaderboard_wallets(self) -> int:
        """Deprecated — superseded by _run_leaderboard_precompute."""
        return self._run_leaderboard_precompute()

    def _discover_candidates_via_holders(self) -> list[str]:
        """Phase A: Discover active wallets by scraping holders of top hot markets.

        Uses official GET /holders?market={conditionId} API (no scraping).
        Returns a deduplicated list of up to 300 proxy wallet addresses.

        T510 P1-3: First tries Data API leaderboard direct read (~500ms).
        Falls back to per-market holders enumeration (~8s) only if direct
        read returns nothing (network error / endpoint shape change).
        """
        # ── Fast path: Data API leaderboard direct read ──────────────────
        try:
            lb_rows = self._client.get_data_leaderboard(period="30d", limit=200)
        except Exception as exc:
            logger.debug("[CTV2-LB] data leaderboard fast-path failed: %s", exc)
            lb_rows = []
        if lb_rows:
            seen_fast: set[str] = set()
            fast_addrs: list[str] = []
            for r in lb_rows:
                addr = str(r.get("proxyWallet") or "").strip().lower()
                if addr.startswith("0x") and len(addr) == 42 and addr not in seen_fast:
                    seen_fast.add(addr)
                    fast_addrs.append(addr)
            if fast_addrs:
                logger.info(
                    "[CTV2-LB] Phase A fast-path (data leaderboard) discovered %d candidates",
                    len(fast_addrs),
                )
                return fast_addrs[:300]

        # ── Fallback: legacy holders enumeration ─────────────────────────
        hot_markets = self._client.get_hot_markets(limit=20)
        seen: set[str] = set()
        addresses: list[str] = []
        for market in hot_markets:
            condition_id = (
                market.get("conditionId")
                or market.get("condition_id")
                or ""
            ).strip()
            if not condition_id:
                continue
            try:
                holders_data = self._client.get_holders(condition_id, limit=20)
                # holders_data is [{token, holders:[{proxyWallet, ...}]}, ...]
                for token_entry in holders_data:
                    holders = token_entry.get("holders") or []
                    if isinstance(token_entry, dict) and "proxyWallet" in token_entry:
                        # flat list case
                        holders = holders_data
                        addr = (token_entry.get("proxyWallet") or "").strip().lower()
                        if addr.startswith("0x") and len(addr) == 42 and addr not in seen:
                            seen.add(addr)
                            addresses.append(addr)
                        break
                    for h in holders:
                        addr = (h.get("proxyWallet") or "").strip().lower()
                        if addr.startswith("0x") and len(addr) == 42 and addr not in seen:
                            seen.add(addr)
                            addresses.append(addr)
            except Exception as exc:
                logger.debug("[CTV2-LB] holders failed market=%s: %s", condition_id[:16], exc)
            time.sleep(0.2)
        logger.info("[CTV2-LB] Phase A discovered %d candidate addresses", len(addresses))
        return addresses[:300]

    def _run_leaderboard_precompute(self) -> int:
        """Pre-compute leaderboard: Phase A discovery + Phase B scoring.

        Phase A: Fetch top 20 hot markets via Gamma API, get holders for each,
                 upsert all discovered addresses into the wallet pool.
        Phase B: Select oldest-scored 50 addresses from pool, compute full metrics
                 (4-dim smart score using closed-positions), update pool columns.

        Uses only official Polymarket APIs. Rate-limited at 0.3s between wallets.
        Returns: number of wallets scored in this cycle.
        """
        # ── Phase A: Discovery ──────────────────────────────────────────
        phase_a_started = time.perf_counter()
        candidates = self._discover_candidates_via_holders()
        phase_a_ms = (time.perf_counter() - phase_a_started) * 1000.0
        seeded_count = 0
        if candidates:
            try:
                with _pg_connect() as conn:
                    for addr in candidates:
                        conn.execute(
                            "INSERT INTO copytrader_v2_wallet_pool (address, source) VALUES (?, ?) "
                            "ON CONFLICT(address) DO UPDATE SET source=CASE "
                            "  WHEN copytrader_v2_wallet_pool.source='manual' THEN copytrader_v2_wallet_pool.source "
                            "  ELSE excluded.source END",
                            (addr, "holders"),
                        )
                    conn.commit()
                seeded_count = len(candidates)
                logger.info("[CTV2-LB] Phase A seeded %d candidates into pool in %.1fms", seeded_count, phase_a_ms)
            except Exception as exc:
                logger.error("[CTV2-LB] Phase A DB upsert failed: %s", exc)
        else:
            logger.info("[CTV2-LB] Phase A discovered no candidates in %.1fms", phase_a_ms)

        # ── Phase B: Scoring ────────────────────────────────────────────
        top_n = int(copytrader_cfg_get("leaderboard_top_n") or 50)
        phase_b_fetch_started = time.perf_counter()
        try:
            with _pg_connect() as conn:
                rows = conn.execute(
                    "SELECT address FROM copytrader_v2_wallet_pool "
                    "ORDER BY COALESCE(last_scored_at, '1970-01-01') ASC LIMIT ?",
                    (top_n,),
                ).fetchall()
            addresses_to_score = [r["address"] for r in rows]
        except Exception as exc:
            logger.error("[CTV2-LB] Phase B fetch failed: %s", exc)
            return 0
        phase_b_fetch_ms = (time.perf_counter() - phase_b_fetch_started) * 1000.0

        phase_b_started = time.perf_counter()
        n = 0
        failed = 0
        durations_ms: list[float] = []
        for addr in addresses_to_score:
            wallet_started = time.perf_counter()
            try:
                metrics = self._analyzer.compute_full_metrics(addr)
                with _pg_connect() as conn:
                    conn.execute(
                        "UPDATE copytrader_v2_wallet_pool SET "
                        "last_metrics_json=?, score=?, primary_category=?, is_likely_bot=?, "
                        "pnl_realized_usd=?, win_rate_f=?, trade_count_30d=?, max_drawdown_usd=?, "
                        "last_scored_at=CURRENT_TIMESTAMP WHERE address=?",
                        (
                            json.dumps(metrics),
                            float(metrics.get("score", 0)),
                            metrics.get("primary_category", "other"),
                            bool(metrics.get("is_likely_bot")),
                            float(metrics.get("pnl_realized_usd", 0)),
                            float(metrics.get("win_rate", 0)),
                            int(metrics.get("trade_count_30d", 0)),
                            float(metrics.get("max_drawdown_usd", 0)),
                            addr,
                        ),
                    )
                    conn.commit()
                n += 1
                durations_ms.append((time.perf_counter() - wallet_started) * 1000.0)
                logger.debug("[CTV2-LB] scored %s score=%.1f", addr[:12], metrics.get("score", 0))
            except Exception as exc:
                failed += 1
                durations_ms.append((time.perf_counter() - wallet_started) * 1000.0)
                logger.warning("[CTV2-LB] score failed %s: %s", addr[:12], exc)
            time.sleep(0.3)

        phase_b_total_ms = (time.perf_counter() - phase_b_started) * 1000.0
        avg_wallet_ms = (sum(durations_ms) / len(durations_ms)) if durations_ms else 0.0
        logger.info(
            "[CTV2-LB] cycle metrics discovery=%d seeded=%d score_candidates=%d scored=%d failed=%d phase_a_ms=%.1f fetch_ms=%.1f phase_b_ms=%.1f avg_wallet_ms=%.1f",
            len(candidates),
            seeded_count,
            len(addresses_to_score),
            n,
            failed,
            phase_a_ms,
            phase_b_fetch_ms,
            phase_b_total_ms,
            avg_wallet_ms,
        )
        return n

    def _scan_task(self, task: dict) -> int:
        wallet = (task.get("wallet_address") or "").lower()
        if not wallet.startswith("0x"):
            return 0
        owner_wallet_address = str(task.get("owner_wallet_address") or "").strip().lower()
        settings = self._settings_for(owner_wallet_address)
        last_seen = float(task.get("last_seen_trade_ts") or 0.0)
        lookback_min = int(settings.get("trade_lookback_minutes") or 30)
        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff = max(last_seen, now_ts - lookback_min * 60)

        activity = self._client.get_activity(wallet, limit=100)
        new_trades = [
            a
            for a in activity
            if (a.get("type") or "").upper() == "TRADE"
            and float(a.get("timestamp") or 0) > cutoff
        ]
        if not new_trades:
            return 0

        max_price = float(settings.get("max_price_filter") or 0.85)
        min_ev = float(settings.get("min_ev_filter") or 0.002)
        max_ts = last_seen
        signals = 0
        for tr in new_trades:
            price = float(tr.get("price") or 0.5)
            side = str(tr.get("side") or "").upper().strip()
            if side not in {"BUY", "SELL"}:
                continue
            if side == "BUY" and price > max_price:
                continue
            est_ev = abs(price - 0.5) * 0.1
            if side == "BUY" and est_ev < min_ev:
                continue
            ts = float(tr.get("timestamp") or 0)
            if ts > max_ts:
                max_ts = ts
            signals += 1
            token_id = str(
                tr.get("asset")
                or tr.get("token_id")
                or tr.get("tokenID")
                or tr.get("token")
                or ""
            ).strip()
            if not token_id.isdigit():
                logger.debug("[CTV2] skip trade without token_id task=%s trade=%s", task.get("id"), tr)
                continue

            logger.info(
                "[CTV2] task=%s user=%s wallet=%s detected %s %s price=%.3f ev~=%.4f",
                task.get("id"),
                owner_wallet_address,
                wallet[:10],
                side,
                (tr.get("title") or "")[:60],
                price,
                est_ev,
            )

            try:
                if side == "BUY":
                    self._mirror_buy(owner_wallet_address, token_id, settings, task)
                else:
                    self._mirror_sell(owner_wallet_address, token_id, settings, task)
            except Exception as exec_exc:
                logger.warning("[CTV2] mirror order failed task=%s side=%s token=%s err=%s", task.get("id"), side, token_id[:16], exec_exc)

        if signals or max_ts > last_seen:
            try:
                with _pg_connect() as conn:
                    conn.execute(
                        "UPDATE copytrader_v2_tasks SET last_seen_trade_ts=?, "
                        "total_signals=total_signals+?, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=?",
                        (max_ts, signals, task["id"]),
                    )
                    conn.commit()
            except Exception as exc:
                logger.warning("[CTV2] update task failed id=%s: %s", task.get("id"), exc)
        return signals

    def _refresh_pool_metrics(self) -> int:
        """Deprecated — superseded by _run_leaderboard_precompute (Phase B)."""
        return 0

    def cycle(self) -> dict[str, int]:
        result = {"signals": 0, "tasks": 0, "pool_refreshed": 0, "wallets_seeded": 0}
        global_enabled = bool(copytrader_cfg_get("enabled"))
        if copytrader_cfg_get("enabled") is None:
            global_enabled = True
        if not global_enabled:
            return result
        tasks = self._load_running_tasks()
        result["tasks"] = len(tasks)
        for t in tasks:
            try:
                result["signals"] += self._scan_task(t)
            except Exception as exc:
                logger.error("[CTV2] task %s failed: %s", t.get("id"), exc)
                try:
                    with _pg_connect() as conn:
                        conn.execute(
                            "UPDATE copytrader_v2_tasks SET error_count=error_count+1, "
                            "last_error=? WHERE id=?",
                            (str(exc)[:300], t["id"]),
                        )
                        conn.commit()
                except Exception:
                    pass
        now = time.time()
        pool_interval = int(copytrader_cfg_get("leaderboard_refresh_interval_seconds") or copytrader_cfg_get("pool_refresh_interval_seconds") or 600)
        if now - self._last_pool_score_at >= pool_interval:
            try:
                result["pool_refreshed"] = self._run_leaderboard_precompute()
                result["wallets_seeded"] = result["pool_refreshed"]
            except Exception as exc:
                logger.error("[CTV2] leaderboard precompute failed: %s", exc)
            self._last_pool_score_at = now
        return result

    def run_forever(self) -> None:
        interval = int(copytrader_cfg_get("scan_interval_seconds") or 60)
        logger.info(
            "[CTV2 Daemon] start dry_run=%s interval=%ds polymarket_official_api=enabled",
            _is_dry_run(),
            interval,
        )
        while _running:
            t0 = time.time()
            try:
                stats = self.cycle()
                if stats["signals"] or stats["pool_refreshed"]:
                    logger.info(
                        "[CTV2] cycle done signals=%d tasks=%d pool_refreshed=%d wallets_seeded=%d",
                        stats["signals"],
                        stats["tasks"],
                        stats["pool_refreshed"],
                        stats["wallets_seeded"],
                    )
            except Exception as exc:
                logger.exception("[CTV2] cycle exception: %s", exc)
            elapsed = time.time() - t0
            sleep_for = max(1.0, interval - elapsed)
            for _ in range(int(sleep_for)):
                if not _running:
                    break
                time.sleep(1.0)
        logger.info("[CTV2 Daemon] stopped")


def main() -> int:
    pid_file = ROOT / "data" / "copytrader_v2.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    try:
        daemon = CopytraderV2Daemon()
        daemon.run_forever()
    finally:
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
