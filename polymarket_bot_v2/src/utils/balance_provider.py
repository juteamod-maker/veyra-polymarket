"""
balance_provider.py — 双轨余额提供器

职责：
  - dry_run 模式：从 SQLite paper_bets 表计算模拟余额
  - 实盘模式：通过 py-clob-client 查询 Polymarket 真实 USDC 余额
  - 提供统一接口供 OrderExecutor 和 Dashboard 使用
  - 带 TTL 缓存避免频繁 API 调用

原则：
  - 仅在 dry_run=false 且环境变量齐全时才连真实 API
  - 任何真实 API 失败时回退到本地 SQLite 计算
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests
from src.utils.config_loader import get, is_dry_run
from src.utils.database import connect as _pg_connect

logger = logging.getLogger(__name__)

# 缓存：避免高频 API 调用
_balance_cache: dict = {"value": None, "ts": 0}
CACHE_TTL_SEC = 30

GAMMA_API = "https://gamma-api.polymarket.com"


def _is_dry_run() -> bool:
    return is_dry_run()


def _initial_balance() -> float:
    """统一初始资金口径：dry_run 走 paper_trading，实盘回退走 live_initial_deposit。"""
    pt_cfg = get("paper_trading") or {}
    if isinstance(pt_cfg, dict):
        if _is_dry_run():
            return float(pt_cfg.get("initial_balance", 100.0))
    return float(get("general.live_initial_deposit") or 0.0)


def _calc_simulated_balance() -> dict:
    """
    从 SQLite 计算模拟余额。

    Returns:
        {
            "balance": float,     # 可用余额
            "deployed": float,    # 持仓占用（入场成本）
            "market_value": float, # 持仓实时市值
            "nlv": float,         # 净清算价值
            "realized_pnl": float,
            "source": "simulated",
        }
    """
    initial = _initial_balance()
    try:
        from src.utils.db import connect

        with connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM paper_bets WHERE resolved_outcome IS NOT NULL"
            ).fetchone()
            realized_pnl = row[0] if row else 0.0

            row2 = conn.execute(
                "SELECT COALESCE(SUM(size_usdc), 0) FROM paper_bets WHERE resolved_outcome IS NULL"
            ).fetchone()
            deployed = row2[0] if row2 else 0.0

        balance = max(initial + realized_pnl - deployed, 0.0)
        nlv = balance + deployed
        return {
            "balance": round(balance, 2),
            "deployed": round(deployed, 2),
            "market_value": round(deployed, 2),
            "nlv": round(nlv, 2),
            "realized_pnl": round(realized_pnl, 2),
            "source": "simulated",
        }
    except Exception as e:
        logger.debug("模拟余额计算失败: %s", e)
        return {
            "balance": initial,
            "deployed": 0.0,
            "market_value": 0.0,
            "nlv": initial,
            "realized_pnl": 0.0,
            "source": "fallback",
        }


def _fetch_live_balance() -> Optional[dict]:
    """
    通过 py-clob-client-v2 查询真实余额。

    Returns:
        {"balance": float, "source": "live"} or None on failure
    """
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient

        host = get("polymarket.clob_api_url") or "https://clob.polymarket.com"
        chain_id = get("polymarket.chain_id") or 137
        private_key = os.environ.get("POLY_PRIVATE_KEY", "")
        # V2 多用户：优先用 DB 真源（users.funder_address），env 仅做兜底，
        # 避免历史 POLY_RELAYER_ADDRESS（V1 单用户的 Safe 地址）被误用为
        # 当前登录用户的 funder。
        funder = _latest_bound_wallet() or os.environ.get("POLY_RELAYER_ADDRESS", "")

        if not private_key or private_key == "your_private_key_here":
            logger.warning("POLY_PRIVATE_KEY 未设置，无法查询实盘余额")
            return None
        if not funder:
            logger.warning(
                "未找到 funder 地址（POLY_RELAYER_ADDRESS/已绑定钱包），无法查询实盘余额"
            )
            return None

        creds = ApiCreds(
            api_key=os.environ["POLY_API_KEY"],
            api_secret=os.environ["POLY_SECRET"],
            api_passphrase=os.environ["POLY_PASSPHRASE"],
        )
        client = ClobClient(
            host=host,
            key=private_key,
            chain_id=chain_id,
            creds=creds,
            signature_type=2,
            funder=funder,
        )

        result = client.get_balance_allowance(params={"signature_type": 2})
        raw = float(result.get("balance", "0") or "0")
        balance = (raw / 1_000_000) if raw > 100_000 else raw

        logger.info("实盘余额查询成功: %.2f USDC", balance)
        return {"balance": round(balance, 2), "source": "live"}
    except ImportError:
        logger.error("py-clob-client-v2 未安装")
        return None
    except KeyError as e:
        logger.error("缺少环境变量: %s", e)
        return None
    except Exception as e:
        logger.error("实盘余额查询失败: %s", e)
        return None


def _latest_bound_wallet() -> str:
    """读取最近一次绑定的钱包地址，作为 funder 回退值。

    V2 真源是 users.funder_address（即 deposit_wallet）。优先从该表读取，
    若无则回退到旧的 wallet_bindings 表。
    """
    try:
        conn = _pg_connect()
        try:
            # V2 优先：users.funder_address（最新一条非空）
            try:
                row = conn.execute(
                    """
                    SELECT funder_address
                    FROM users
                    WHERE funder_address IS NOT NULL AND funder_address != ''
                    ORDER BY COALESCE(updated_at, created_at) DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row and row["funder_address"]:
                    return str(row["funder_address"]).strip()
            except Exception:
                pass
            # 旧 V1 兼容：wallet_bindings
            try:
                row = conn.execute(
                    """
                    SELECT wallet_address
                    FROM wallet_bindings
                    WHERE wallet_address IS NOT NULL AND wallet_address != ''
                    ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row and row["wallet_address"]:
                    return str(row["wallet_address"]).strip()
            except Exception:
                pass
        finally:
            conn.close()
    except Exception:
        return ""
    return ""


def _fetch_realtime_market_value() -> Optional[float]:
    """
    获取所有开仓投注的实时市场总价值。

    通过 Gamma API 获取每个市场的当前价格，计算 shares × current_price。
    返回 None 表示获取失败（调用方应回退到入场成本）。
    """
    try:
        from src.utils.db import connect

        with connect() as conn:
            rows = conn.execute(
                "SELECT market_id, outcome, market_price, size_usdc "
                "FROM paper_bets WHERE resolved_outcome IS NULL"
            ).fetchall()

        if not rows:
            return 0.0

        # 并发获取所有市场价格（性能优化：43个市场从~30秒降至~3秒）
        def _fetch_one(row):
            market_id = row[0]
            outcome = (row[1] or "YES").upper()
            entry_price = float(row[2] or 0)
            size_usdc = float(row[3] or 0)
            if entry_price <= 0 or size_usdc <= 0:
                return size_usdc
            current_price = _fetch_single_market_price(market_id, outcome)
            if current_price is not None:
                shares = size_usdc / entry_price
                return shares * current_price
            return size_usdc

        total_value = 0.0
        fetched = 0
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_fetch_one, row): row for row in rows}
            for future in as_completed(futures):
                try:
                    val = future.result(timeout=15)
                    total_value += val
                    row = futures[future]
                    entry_price = float(row[2] or 0)
                    if entry_price > 0:
                        fetched += 1
                except Exception:
                    total_value += float(futures[future][3] or 0)

        logger.debug(
            f"[RealtimeNLV] {fetched}/{len(rows)} 市场获取实时价格，"
            f"总市值={total_value:.2f}"
        )
        return total_value
    except Exception as e:
        logger.warning(f"[RealtimeNLV] 获取失败: {e}")
        return None


def _fetch_single_market_price(market_id: str, outcome: str) -> Optional[float]:
    """获取单个市场指定方向的当前卖出价（Mark-to-Market视角）。

    T191: 优先使用 bestBid/bestAsk（实际卖出价），
    回退到 tokens[].price（mid-price）。
    YES → bestBid, NO → 1 - bestAsk。
    """
    try:
        resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=10)
        resp.raise_for_status()
        mdata = resp.json()
        if not isinstance(mdata, dict):
            return None

        outcome_upper = outcome.upper()

        # 方式1 (优先): bestBid/bestAsk — 实际卖出价
        if outcome_upper == "YES":
            bid = mdata.get("bestBid")
            if bid is not None:
                return float(bid)
        elif outcome_upper == "NO":
            ask = mdata.get("bestAsk")
            if ask is not None:
                return round(1.0 - float(ask), 4)

        # 方式2 (回退): tokens 列表 mid-price
        tokens = mdata.get("tokens") or []
        for token in tokens:
            tok_outcome = (token.get("outcome") or "").upper()
            if tok_outcome == outcome_upper:
                price = token.get("price")
                if price is not None:
                    return float(price)

        return None
    except Exception:
        return None


def get_portfolio() -> dict:
    """
    统一获取投资组合数据。

    dry_run=true → 模拟数据
    dry_run=false → 真实余额 + 本地持仓数据混合

    Returns:
        {
            "balance": float,
            "deployed": float,
            "nlv": float,
            "realized_pnl": float,
            "source": "simulated" | "live" | "fallback",
        }
    """
    now = time.time()
    if _balance_cache["value"] and (now - _balance_cache["ts"]) < CACHE_TTL_SEC:
        return _balance_cache["value"]

    if _is_dry_run():
        result = _calc_simulated_balance()
    else:
        live = _fetch_live_balance()
        if live:
            # 真实余额 + 实时持仓市值
            sim = _calc_simulated_balance()
            market_value = _fetch_realtime_market_value()
            if market_value is None:
                market_value = sim["deployed"]  # 回退到入场成本
            result = {
                "balance": live["balance"],
                "deployed": sim["deployed"],
                "market_value": round(market_value, 2),
                "nlv": round(live["balance"] + market_value, 2),
                "realized_pnl": sim["realized_pnl"],
                "source": "live",
            }
        else:
            logger.warning("实盘余额不可用，回退到模拟数据")
            result = _calc_simulated_balance()

    _balance_cache["value"] = result
    _balance_cache["ts"] = now
    return result


def get_available_balance() -> float:
    """获取可用余额（单一数值，便于 OrderExecutor 使用）"""
    return get_portfolio()["balance"]


def invalidate_cache() -> None:
    """强制刷新缓存（下单后调用）"""
    _balance_cache["value"] = None
    _balance_cache["ts"] = 0
