"""
points_routes.py — 用户积分、三级分销推荐、手续费系统 API
T265 + T267 + T268
"""

import logging
import secrets
import string
import re
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Query, Request
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import (
    db_connect,
    get_request_identity,
    verify_user_session,
)
import asyncio

logger = logging.getLogger("dashboard.points")
router = APIRouter()

# ── 常量 ──
REFERRAL_CODE_LENGTH = 8
_PM_CATEGORY_ALIASES = {
    "financial": "finance",
    "economics": "economy",
    "geopolitical": "geopolitics",
    "political": "politics",
    "pop_culture": "pop-culture",
}
_RULE_ALIASES = {
    "signup": ("signup", "register", "registration"),
    "referral_l1": ("referral_l1", "referral_level_1", "ref_l1"),
    "referral_l2": ("referral_l2", "referral_level_2", "ref_l2"),
    "referral_l3": ("referral_l3", "referral_level_3", "ref_l3"),
    "referral_referred": (
        "referral_referred",
        "referral_bonus",
        "referral_first_trade_referred",
    ),
    "onramp_deposit_user": (
        "onramp_deposit_user",
        "onramp_deposit",
        "deposit_onramp_user",
    ),
    "onramp_deposit_l1": ("onramp_deposit_l1", "onramp_referral_l1"),
    "onramp_deposit_l2": ("onramp_deposit_l2", "onramp_referral_l2"),
    "onramp_deposit_l3": ("onramp_deposit_l3", "onramp_referral_l3"),
}

_FALLBACK_RULE_POINTS = {
    "signup": 200,
    "referral_l1": 300,
    "referral_l2": 100,
    "referral_l3": 50,
    "referral_referred": 300,
    "onramp_deposit_user": 120,
    "onramp_deposit_l1": 80,
    "onramp_deposit_l2": 40,
    "onramp_deposit_l3": 20,
}

_POINTS_RULES_JSON = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "points_rules.json")
)
_POINTS_RULES_CACHE: dict[str, object] = {
    "mtime": None,
    "rules": {},
    "meta": {},
}

_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_REFERRAL_CODE_RE = re.compile(r"^[A-Z0-9]{8}$")


# ── 工具函数 ──


def _get_user_wallet(user_session: str | None) -> str | None:
    if not user_session:
        return None
    sess = verify_user_session(user_session)
    if not sess:
        return None
    wallet = str(sess.get("wallet_address") or "").strip().lower()
    return wallet or None


def _get_user_wallet_from_request(
    request: Request, user_session: str | None
) -> str | None:
    """优先使用 user_session，其次回退到已认证请求身份（支持 admin key）。"""
    wallet = _get_user_wallet(user_session)
    if wallet:
        return wallet
    identity = get_request_identity(request)
    if not identity or not identity.get("authenticated"):
        return None
    wallet_address = str(identity.get("wallet_address") or "").strip().lower()
    return wallet_address or None


def _ensure_points_account(wallet: str) -> None:
    """确保积分账户存在"""
    wallet = str(wallet or "").strip().lower()
    if not wallet:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT wallet_address FROM user_points WHERE LOWER(wallet_address)=? LIMIT 1",
            (wallet,),
        ).fetchone()
        if existing:
            existing_wallet = str(existing["wallet_address"] or "").strip()
            if existing_wallet and existing_wallet != wallet:
                try:
                    conn.execute(
                        "UPDATE user_points SET wallet_address=? WHERE LOWER(wallet_address)=?",
                        (wallet, wallet),
                    )
                    conn.commit()
                except Exception:
                    pass
            return
        conn.execute(
            """INSERT OR IGNORE INTO user_points(wallet_address, balance, total_earned, total_spent, updated_at)
               VALUES(?, 0, 0, 0, ?)""",
            (wallet, now),
        )
        conn.commit()


def _add_points(
    wallet: str, amount: int, source: str, description: str = "", ref_id: str = ""
) -> int:
    """增加积分并记录流水，返回新余额"""
    wallet = str(wallet or "").strip().lower()
    now = datetime.now(timezone.utc).isoformat()
    _ensure_points_account(wallet)
    with db_connect() as conn:
        conn.execute(
            """UPDATE user_points SET balance = balance + ?, total_earned = total_earned + ?,
               updated_at = ? WHERE LOWER(wallet_address) = ?""",
            (amount, amount, now, wallet),
        )
        conn.execute(
            """INSERT INTO point_transactions(wallet_address, type, amount, source, ref_id, description, created_at)
               VALUES(?, 'earn', ?, ?, ?, ?, ?)""",
            (wallet, amount, source, ref_id, description, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT COALESCE(SUM(balance),0) AS balance FROM user_points WHERE LOWER(wallet_address)=?",
            (wallet,),
        ).fetchone()
        return row["balance"] if row else 0


def _spend_points(
    wallet: str, amount: int, source: str, description: str = "", ref_id: str = ""
) -> tuple[bool, int]:
    """扣减积分，返回(成功, 新余额)"""
    wallet = str(wallet or "").strip().lower()
    now = datetime.now(timezone.utc).isoformat()
    _ensure_points_account(wallet)
    with db_connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(balance),0) AS balance FROM user_points WHERE LOWER(wallet_address)=?",
            (wallet,),
        ).fetchone()
        if not row or row["balance"] < amount:
            return False, row["balance"] if row else 0
        conn.execute(
            """UPDATE user_points SET balance = balance - ?, total_spent = total_spent + ?,
               updated_at = ? WHERE LOWER(wallet_address) = ?""",
            (amount, amount, now, wallet),
        )
        conn.execute(
            """INSERT INTO point_transactions(wallet_address, type, amount, source, ref_id, description, created_at)
               VALUES(?, 'spend', ?, ?, ?, ?, ?)""",
            (wallet, amount, source, ref_id, description, now),
        )
        conn.commit()
        return True, row["balance"] - amount


def _active_point_rules_map() -> dict[str, int]:
    _ensure_default_point_rules()
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT action_type, points FROM point_rules WHERE active=1"
        ).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        action = str(r["action_type"] or "").strip().lower()
        if not action:
            continue
        try:
            out[action] = int(r["points"] or 0)
        except Exception:
            out[action] = 0
    return out


def _load_points_rules_json() -> tuple[dict[str, int], dict[str, object]]:
    """Load optional config/points_rules.json as a non-breaking fallback source."""
    cache_mtime = _POINTS_RULES_CACHE.get("mtime")
    try:
        mtime = os.path.getmtime(_POINTS_RULES_JSON)
    except Exception:
        _POINTS_RULES_CACHE["mtime"] = None
        _POINTS_RULES_CACHE["rules"] = {}
        _POINTS_RULES_CACHE["meta"] = {}
        return {}, {}

    if cache_mtime == mtime:
        rules_cached = _POINTS_RULES_CACHE.get("rules")
        meta_cached = _POINTS_RULES_CACHE.get("meta")
        return (
            dict(rules_cached) if isinstance(rules_cached, dict) else {},
            dict(meta_cached) if isinstance(meta_cached, dict) else {},
        )

    try:
        with open(_POINTS_RULES_JSON, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        logger.warning("load points_rules.json failed: %s", exc)
        _POINTS_RULES_CACHE["mtime"] = mtime
        _POINTS_RULES_CACHE["rules"] = {}
        _POINTS_RULES_CACHE["meta"] = {}
        return {}, {}

    raw_rules = payload.get("rules") if isinstance(payload, dict) else {}
    norm_rules: dict[str, int] = {}
    if isinstance(raw_rules, dict):
        for key, val in raw_rules.items():
            k = str(key or "").strip().lower()
            if not k:
                continue
            try:
                norm_rules[k] = max(0, int(val))
            except Exception:
                continue

    meta: dict[str, object] = {}
    if isinstance(payload, dict):
        for mk in ("min_verified_deposit_usdc",):
            if mk in payload:
                meta[mk] = payload.get(mk)

    _POINTS_RULES_CACHE["mtime"] = mtime
    _POINTS_RULES_CACHE["rules"] = norm_rules
    _POINTS_RULES_CACHE["meta"] = meta
    return norm_rules, meta


def _rule_points(rule_key: str, default: int = 0) -> int:
    aliases = _RULE_ALIASES.get(str(rule_key or "").strip().lower(), ())
    if not aliases:
        return int(
            _FALLBACK_RULE_POINTS.get(str(rule_key or "").strip().lower(), default)
        )
    rules = _active_point_rules_map()
    for alias in aliases:
        pts = rules.get(str(alias or "").strip().lower())
        if pts is not None and int(pts) > 0:
            return int(pts)

    cfg_rules, _cfg_meta = _load_points_rules_json()
    for alias in aliases:
        pts = cfg_rules.get(str(alias or "").strip().lower())
        if pts is not None and int(pts) > 0:
            return int(pts)

    rule_key_norm = str(rule_key or "").strip().lower()
    return int(_FALLBACK_RULE_POINTS.get(rule_key_norm, default))


def get_points_config_float(key: str, default: float) -> float:
    _cfg_rules, cfg_meta = _load_points_rules_json()
    raw = cfg_meta.get(str(key or "").strip())
    try:
        return float(raw)
    except Exception:
        return float(default)


def _ensure_default_point_rules() -> None:
    """Ensure key rules exist and are active to avoid misleading zero-reward UX.

    Logic (idempotent):
    - If a rule does NOT exist → INSERT with active=1 and the default points value.
    - If a rule EXISTS but is inactive (active=0) → UPDATE to active=1 so fallback
      values are visible in the UI. Admin-set points values are NOT overwritten.
    - If a rule EXISTS and is already active → do nothing.
    """
    now = datetime.now(timezone.utc).isoformat()
    defaults = [
        ("signup", 200, "新用户注册奖励", 1),
        ("referral_l1", 300, "一级推荐：被邀请人完成首笔交易", 0),
        ("referral_l2", 100, "二级推荐：下级邀请人完成首笔交易", 0),
        ("referral_l3", 50, "三级推荐：下下级邀请人完成首笔交易", 0),
        ("referral_referred", 300, "被推荐人首笔交易奖励", 1),
        ("onramp_deposit_user", 120, "法币入金核验奖励", 24),
        ("onramp_deposit_l1", 80, "一级推荐：被邀请人法币入金奖励", 0),
        ("onramp_deposit_l2", 40, "二级推荐：下级被邀请人法币入金奖励", 0),
        ("onramp_deposit_l3", 20, "三级推荐：下下级被邀请人法币入金奖励", 0),
    ]
    with db_connect() as conn:
        rows = conn.execute("SELECT action_type, active FROM point_rules").fetchall()
        existing_active: dict[str, bool] = {
            str(r["action_type"] or "").strip().lower(): bool(int(r["active"] or 0))
            for r in rows
            if str(r["action_type"] or "").strip()
        }
        changed = False
        for action_type, points, description, max_daily in defaults:
            if action_type in existing_active:
                # Rule exists — reactivate if currently inactive
                if not existing_active[action_type]:
                    conn.execute(
                        "UPDATE point_rules SET active=1, updated_at=? WHERE LOWER(action_type)=?",
                        (now, action_type),
                    )
                    changed = True
                # else: active, leave admin-configured points value alone
                continue
            # Rule not found — insert with default values
            try:
                conn.execute(
                    """
                    INSERT INTO point_rules(
                        action_type, points, description, max_daily, active,
                        condition_type, condition_json, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, 1, 'always', '{}', ?, ?)
                    """,
                    (action_type, int(points), description, int(max_daily), now, now),
                )
            except Exception:
                conn.execute(
                    """
                    INSERT INTO point_rules(action_type, points, description, max_daily, active, created_at, updated_at)
                    VALUES(?, ?, ?, ?, 1, ?, ?)
                    """,
                    (action_type, int(points), description, int(max_daily), now, now),
                )
            changed = True
        if changed:
            conn.commit()


def _ensure_points_tiers_table() -> None:
    """Ensure points tier table exists and has sensible defaults."""
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS points_tiers (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    tier_name       TEXT NOT NULL,
                    min_points      INTEGER NOT NULL,
                    max_points      INTEGER,
                    badge           TEXT,
                    description     TEXT,
                    active          INTEGER NOT NULL DEFAULT 1,
                    sort_order      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                )
                """)
        except Exception:
            conn.rollback()  # clear failed transaction state before retry
            conn.execute("""
                CREATE TABLE IF NOT EXISTS points_tiers (
                    id              SERIAL PRIMARY KEY,
                    tier_name       TEXT NOT NULL,
                    min_points      INTEGER NOT NULL,
                    max_points      INTEGER,
                    badge           TEXT,
                    description     TEXT,
                    active          INTEGER NOT NULL DEFAULT 1,
                    sort_order      INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                )
                """)
        row = conn.execute("SELECT COUNT(*) AS c FROM points_tiers").fetchone()
        if int(row["c"] if row else 0) <= 0:
            defaults = [
                ("Bronze", 0, 999, "bronze", "Entry tier", 1, 10),
                ("Silver", 1000, 4999, "silver", "Stable trader", 1, 20),
                ("Gold", 5000, 19999, "gold", "High activity", 1, 30),
                ("Platinum", 20000, 99999, "platinum", "Advanced tier", 1, 40),
                ("Diamond", 100000, None, "diamond", "Top tier", 1, 50),
            ]
            conn.executemany(
                """
                INSERT INTO points_tiers(
                    tier_name, min_points, max_points, badge, description,
                    active, sort_order, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        name,
                        int(min_pts),
                        (None if max_pts is None else int(max_pts)),
                        badge,
                        desc,
                        int(active),
                        int(order),
                        now,
                        now,
                    )
                    for (name, min_pts, max_pts, badge, desc, active, order) in defaults
                ],
            )
        conn.commit()


def _resolve_points_tier(balance: int) -> dict[str, int | str | None]:
    """Resolve current tier and next tier threshold from points_tiers."""
    bal = int(balance or 0)
    _ensure_points_tiers_table()
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT id, tier_name, min_points, max_points, badge, sort_order
            FROM points_tiers
            WHERE active=1
            ORDER BY sort_order ASC, min_points ASC, id ASC
            """).fetchall()
    if not rows:
        return {
            "tier": "Bronze",
            "tier_badge": "bronze",
            "tier_id": None,
            "next_tier_pts": 0,
        }

    current = None
    for row in rows:
        min_pts = int(row["min_points"] or 0)
        max_pts = row["max_points"]
        if bal >= min_pts and (max_pts is None or bal <= int(max_pts or 0)):
            current = row
    if current is None:
        # fallback to highest eligible min_points
        eligible = [r for r in rows if bal >= int(r["min_points"] or 0)]
        current = eligible[-1] if eligible else rows[0]

    next_tier_pts = 0
    current_min = int(current["min_points"] or 0)
    higher = [
        int(r["min_points"] or 0)
        for r in rows
        if int(r["min_points"] or 0) > current_min
    ]
    if higher:
        next_tier_pts = min(higher)

    return {
        "tier": str(current["tier_name"] or "Bronze"),
        "tier_badge": str(current["badge"] or "bronze"),
        "tier_id": int(current["id"]),
        "next_tier_pts": int(next_tier_pts),
    }


# ---------------------------------------------------------------------------
# T491A: 银行级"成交→积分/手续费"结算
# ---------------------------------------------------------------------------
# 规则：1 USD 实际成交额 = 10 积分；以 trade_settlements.order_ref UNIQUE 做幂等保护，
# 同一笔 CLOB 成交（不论被结算流程调用几次）只会落账一次。
POINTS_PER_USD = 10


def _read_active_fee_bps() -> int:
    """读取当前激活的 builder/taker 费率 bps，无配置时返回 0。"""
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT rate_bps FROM fee_config WHERE active=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                return int(row["rate_bps"] or 0)
    except Exception:
        pass
    return 0


def settle_trade_award_points(
    wallet_address: str,
    order_ref: str,
    *,
    notional_usd: float,
    market_id: str = "",
    token_id: str = "",
    side: str = "",
    raw: dict | None = None,
) -> dict:
    """对一次真实成交记账并发放积分（幂等）。

    返回结构：
        {"status": "ok"/"duplicate"/"skipped", "settlement_id": int|None,
         "points_awarded": int, "fee_usd": float, "fee_bps": int}

    bank-grade 设计：
      1. order_ref 是 trade_settlements.UNIQUE 字段；INSERT ... ON CONFLICT DO NOTHING
         保证并发结算同一 order_ref 只会写入一行；
      2. RETURNING id 拿到本次实际写入的 id；若为空说明已结算过 → duplicate；
      3. 仅在新写入时再发放积分，确保积分账务和结算账务一致；
      4. 全程使用同一 db_connect 连接，依赖 PG 连接池的事务上下文。
    """
    out = {
        "status": "skipped",
        "settlement_id": None,
        "points_awarded": 0,
        "fee_usd": 0.0,
        "fee_bps": 0,
    }
    wallet = (wallet_address or "").strip().lower()
    ref = (order_ref or "").strip()
    if not wallet or not wallet.startswith("0x") or not ref:
        return out
    try:
        notional = float(notional_usd or 0.0)
    except Exception:
        notional = 0.0
    if notional <= 0:
        return out

    fee_bps = _read_active_fee_bps()
    fee_usd = round(notional * fee_bps / 10000.0, 6)
    points = int(notional * POINTS_PER_USD)
    out["fee_bps"] = fee_bps
    out["fee_usd"] = fee_usd

    now = datetime.now(timezone.utc).isoformat()
    raw_json = json.dumps(raw or {}, ensure_ascii=False)[:8000]

    _ensure_points_account(wallet)
    with db_connect() as conn:
        # 幂等写入 trade_settlements；ON CONFLICT DO NOTHING + RETURNING id
        ins = conn.execute(
            """
            INSERT INTO trade_settlements(
                wallet_address, order_ref, market_id, token_id, side,
                notional_usd, fee_bps, fee_usd, points_awarded, status, created_at, raw_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'settled', ?, ?)
            ON CONFLICT (order_ref) DO NOTHING
            RETURNING id
            """,
            (
                wallet,
                ref,
                str(market_id or ""),
                str(token_id or ""),
                str(side or ""),
                notional,
                fee_bps,
                fee_usd,
                points,
                now,
                raw_json,
            ),
        ).fetchone()
        if not ins:
            out["status"] = "duplicate"
            return out
        settlement_id = int(ins["id"])
        out["settlement_id"] = settlement_id

        if points > 0:
            conn.execute(
                """UPDATE user_points
                   SET balance = balance + ?, total_earned = total_earned + ?, updated_at = ?
                   WHERE wallet_address=?""",
                (points, points, now, wallet),
            )
            tx = conn.execute(
                """INSERT INTO point_transactions(
                       wallet_address, type, amount, source, ref_id, description, created_at
                   ) VALUES(?, 'earn', ?, 'trade_fill', ?, ?, ?)
                   RETURNING id""",
                (
                    wallet,
                    points,
                    ref,
                    f"trade fill ${notional:.4f} → {points} pts",
                    now,
                ),
            ).fetchone()
            if tx:
                conn.execute(
                    "UPDATE trade_settlements SET points_tx_id=? WHERE id=?",
                    (int(tx["id"]), settlement_id),
                )

        if fee_usd > 0:
            try:
                conn.execute(
                    """INSERT INTO fee_transactions(
                          wallet_address, bet_id, trade_type, gross_amount,
                          fee_rate_bps, fee_amount, discount_points, discount_amount,
                          final_fee, created_at
                       ) VALUES(?, ?, 'trade', ?, ?, ?, 0, 0, ?, ?)""",
                    (wallet, ref, notional, fee_bps, fee_usd, fee_usd, now),
                )
            except Exception:
                pass

        conn.commit()

    out["status"] = "ok"
    out["points_awarded"] = points if out["settlement_id"] else 0

    try:
        with db_connect() as conn:
            c_row = conn.execute(
                "SELECT COUNT(*) AS c FROM trade_settlements WHERE wallet_address=?",
                (wallet,),
            ).fetchone()
        if int(c_row["c"] if c_row else 0) == 1:
            referral_rewards = _award_referral_on_first_trade(wallet)
            if any(int(v or 0) > 0 for v in referral_rewards.values()):
                logger.info(
                    "first-trade referral rewards wallet=%s rewards=%s",
                    wallet,
                    referral_rewards,
                )
    except Exception as exc:
        logger.warning(
            "first-trade referral settle failed wallet=%s err=%s", wallet, exc
        )

    return out


def _generate_referral_code() -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(REFERRAL_CODE_LENGTH))


def _process_referral_chain(new_wallet: str, referral_code: str) -> list[dict]:
    """仅建立三级分销关系，奖励在被推荐人首笔成交后统一按规则发放。"""
    new_wallet = str(new_wallet or "").strip().lower()
    referral_code = str(referral_code or "").strip().upper()
    if not _WALLET_RE.match(new_wallet) or not _REFERRAL_CODE_RE.match(referral_code):
        return []

    chain = []
    now = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        # 查找推荐码拥有者(L1推荐人)
        code_row = conn.execute(
            "SELECT wallet_address FROM referral_codes WHERE UPPER(code)=?",
            (referral_code,),
        ).fetchone()
        if not code_row:
            return chain

        l1_wallet = str(code_row["wallet_address"] or "").strip().lower()
        if not _WALLET_RE.match(l1_wallet):
            return chain
        if l1_wallet == new_wallet:
            return chain  # 不能推荐自己

        # 检查是否已有推荐关系
        existing = conn.execute(
            "SELECT id FROM user_referrals WHERE LOWER(referred_wallet)=?",
            (new_wallet,),
        ).fetchone()
        if existing:
            return chain

        # 写入L1推荐关系
        conn.execute(
            """INSERT INTO user_referrals(referrer_wallet, referred_wallet, referral_code, level, created_at)
               VALUES(?, ?, ?, 1, ?)""",
            (l1_wallet, new_wallet, referral_code, now),
        )

        chain.append({"wallet": l1_wallet, "level": 1})
        seen_wallets = {new_wallet, l1_wallet}

        # 查找L2推荐人(谁推荐了L1)
        l2_row = conn.execute(
            "SELECT referrer_wallet FROM user_referrals WHERE LOWER(referred_wallet)=? AND level=1",
            (l1_wallet,),
        ).fetchone()
        if l2_row:
            l2_wallet = str(l2_row["referrer_wallet"] or "").strip().lower()
            if _WALLET_RE.match(l2_wallet) and l2_wallet not in seen_wallets:
                chain.append({"wallet": l2_wallet, "level": 2})
                seen_wallets.add(l2_wallet)

                # 查找L3推荐人(谁推荐了L2)
                l3_row = conn.execute(
                    "SELECT referrer_wallet FROM user_referrals WHERE LOWER(referred_wallet)=? AND level=1",
                    (l2_wallet,),
                ).fetchone()
                if l3_row:
                    l3_wallet = str(l3_row["referrer_wallet"] or "").strip().lower()
                    if _WALLET_RE.match(l3_wallet) and l3_wallet not in seen_wallets:
                        chain.append({"wallet": l3_wallet, "level": 3})

        conn.commit()
    return chain


def _award_referral_on_first_trade(trader_wallet: str) -> dict[str, int]:
    """在被推荐人首笔成交后发放奖励，严格按 point_rules 且幂等。"""
    wallet = str(trader_wallet or "").strip().lower()
    if not wallet or not _WALLET_RE.match(wallet):
        return {"l1": 0, "l2": 0, "l3": 0, "referred": 0}

    marker_source = "referral_first_trade_marker"
    with db_connect() as conn:
        marker = conn.execute(
            "SELECT id FROM point_transactions WHERE LOWER(wallet_address)=? AND source=? AND ref_id=? LIMIT 1",
            (wallet, marker_source, wallet),
        ).fetchone()
        if marker:
            return {"l1": 0, "l2": 0, "l3": 0, "referred": 0}

    rewards = {"l1": 0, "l2": 0, "l3": 0, "referred": 0}
    now = datetime.now(timezone.utc).isoformat()

    with db_connect() as conn:
        l1_row = conn.execute(
            "SELECT referrer_wallet FROM user_referrals WHERE LOWER(referred_wallet)=? AND level=1",
            (wallet,),
        ).fetchone()
        if not l1_row:
            conn.execute(
                """INSERT INTO point_transactions(wallet_address, type, amount, source, ref_id, description, created_at)
                   VALUES(?, 'earn', 0, ?, ?, ?, ?)""",
                (wallet, marker_source, wallet, "no referral relation", now),
            )
            conn.commit()
            return rewards

    chain: list[tuple[int, str]] = []
    with db_connect() as conn:
        seen_wallets = {wallet}
        l1 = conn.execute(
            "SELECT referrer_wallet FROM user_referrals WHERE LOWER(referred_wallet)=? AND level=1",
            (wallet,),
        ).fetchone()
        if l1:
            w1 = str(l1["referrer_wallet"] or "").strip().lower()
            if _WALLET_RE.match(w1) and w1 not in seen_wallets:
                chain.append((1, w1))
                seen_wallets.add(w1)
                l2 = conn.execute(
                    "SELECT referrer_wallet FROM user_referrals WHERE LOWER(referred_wallet)=? AND level=1",
                    (w1,),
                ).fetchone()
                if l2:
                    w2 = str(l2["referrer_wallet"] or "").strip().lower()
                    if _WALLET_RE.match(w2) and w2 not in seen_wallets:
                        chain.append((2, w2))
                        seen_wallets.add(w2)
                        l3 = conn.execute(
                            "SELECT referrer_wallet FROM user_referrals WHERE LOWER(referred_wallet)=? AND level=1",
                            (w2,),
                        ).fetchone()
                        if l3:
                            w3 = str(l3["referrer_wallet"] or "").strip().lower()
                            if _WALLET_RE.match(w3) and w3 not in seen_wallets:
                                chain.append((3, w3))

    for level, ref_wallet in chain:
        pts = _rule_points(f"referral_l{level}", default=0)
        if pts <= 0:
            continue
        reason_key = f"L{level}_first_trade"
        with db_connect() as conn:
            already_rewarded = conn.execute(
                """SELECT id FROM referral_rewards
                   WHERE LOWER(wallet_address)=? AND LOWER(from_wallet)=? AND reason=?
                   LIMIT 1""",
                (ref_wallet, wallet, reason_key),
            ).fetchone()
        if already_rewarded:
            continue

        _add_points(
            ref_wallet,
            pts,
            "referral",
            f"L{level} referral reward after first trade: {wallet[:10]}...",
            wallet,
        )
        with db_connect() as conn:
            conn.execute(
                """INSERT INTO referral_rewards(wallet_address, from_wallet, level, points, reason, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (ref_wallet, wallet, level, pts, reason_key, now),
            )
            conn.commit()
        rewards[f"l{level}"] = pts

    referred_pts = _rule_points("referral_referred", default=0)
    if referred_pts > 0:
        with db_connect() as conn:
            referred_bonus_exists = conn.execute(
                """SELECT id FROM point_transactions
                   WHERE LOWER(wallet_address)=? AND source='referral_bonus' AND ref_id=?
                   LIMIT 1""",
                (wallet, wallet),
            ).fetchone()
        if not referred_bonus_exists:
            _add_points(
                wallet,
                referred_pts,
                "referral_bonus",
                "Referral first-trade bonus",
                wallet,
            )
            rewards["referred"] = referred_pts

    with db_connect() as conn:
        marker = conn.execute(
            "SELECT id FROM point_transactions WHERE LOWER(wallet_address)=? AND source=? AND ref_id=? LIMIT 1",
            (wallet, marker_source, wallet),
        ).fetchone()
        if not marker:
            conn.execute(
                """INSERT INTO point_transactions(wallet_address, type, amount, source, ref_id, description, created_at)
                   VALUES(?, 'earn', 0, ?, ?, ?, ?)""",
                (
                    wallet,
                    marker_source,
                    wallet,
                    "first-trade referral rewards settled",
                    now,
                ),
            )
            conn.commit()

    return rewards


def award_referral_on_onramp_deposit(
    trader_wallet: str, verification_key: str
) -> dict[str, int]:
    """Settle L1/L2/L3 referral points after a verified onramp deposit."""
    wallet = str(trader_wallet or "").strip().lower()
    verify_key = str(verification_key or "").strip().lower()
    if not wallet or not verify_key or not _WALLET_RE.match(wallet):
        return {"l1": 0, "l2": 0, "l3": 0}

    rewards = {"l1": 0, "l2": 0, "l3": 0}
    now = datetime.now(timezone.utc).isoformat()

    chain: list[tuple[int, str]] = []
    with db_connect() as conn:
        seen_wallets = {wallet}
        l1 = conn.execute(
            "SELECT referrer_wallet FROM user_referrals WHERE LOWER(referred_wallet)=? AND level=1",
            (wallet,),
        ).fetchone()
        if l1:
            w1 = str(l1["referrer_wallet"] or "").strip().lower()
            if _WALLET_RE.match(w1) and w1 not in seen_wallets:
                chain.append((1, w1))
                seen_wallets.add(w1)
                l2 = conn.execute(
                    "SELECT referrer_wallet FROM user_referrals WHERE LOWER(referred_wallet)=? AND level=1",
                    (w1,),
                ).fetchone()
                if l2:
                    w2 = str(l2["referrer_wallet"] or "").strip().lower()
                    if _WALLET_RE.match(w2) and w2 not in seen_wallets:
                        chain.append((2, w2))
                        seen_wallets.add(w2)
                        l3 = conn.execute(
                            "SELECT referrer_wallet FROM user_referrals WHERE LOWER(referred_wallet)=? AND level=1",
                            (w2,),
                        ).fetchone()
                        if l3:
                            w3 = str(l3["referrer_wallet"] or "").strip().lower()
                            if _WALLET_RE.match(w3) and w3 not in seen_wallets:
                                chain.append((3, w3))

    for level, ref_wallet in chain:
        pts = _rule_points(f"onramp_deposit_l{level}", default=0)
        if pts <= 0:
            continue

        tx_source = f"referral_onramp_l{level}"
        with db_connect() as conn:
            already = conn.execute(
                """SELECT id FROM point_transactions
                   WHERE LOWER(wallet_address)=? AND source=? AND ref_id=?
                   LIMIT 1""",
                (ref_wallet, tx_source, verify_key),
            ).fetchone()
        if already:
            continue

        _add_points(
            ref_wallet,
            pts,
            tx_source,
            f"L{level} referral reward after verified onramp deposit",
            verify_key,
        )

        reason_key = f"L{level}_onramp_deposit:{verify_key}"
        with db_connect() as conn:
            conn.execute(
                """INSERT INTO referral_rewards(wallet_address, from_wallet, level, points, reason, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (ref_wallet, wallet, level, pts, reason_key, now),
            )
            conn.commit()
        rewards[f"l{level}"] = pts

    return rewards


def _normalize_category_slug(value: str | None) -> str:
    slug = str(value or "").strip().lower().replace("_", "-")
    slug = re.sub(r"[^a-z0-9\-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        return ""
    return _PM_CATEGORY_ALIASES.get(slug, slug)


# ── 积分 API ──


@router.get("/api/user/points")
async def get_user_points(request: Request, user_session: str | None = Cookie(None)):
    wallet = _get_user_wallet_from_request(request, user_session)
    if not wallet:
        return JSONResponse({"error": "unauthorized"}, 401)

    _ensure_points_account(wallet)
    try:
        _ensure_points_tiers_table()
    except Exception as _tiers_exc:
        logger.warning("_ensure_points_tiers_table failed (non-fatal): %s", _tiers_exc)

    def _db():
        def _parse_txn_dt(raw_value):
            text = str(raw_value or "").strip()
            if not text:
                return None
            try:
                normalized = text.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(normalized)
            except Exception:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

        # Lazy signup bonus: use direct fallback to avoid triggering _ensure_default_point_rules
        try:
            with db_connect() as conn:
                _existing_reg = conn.execute(
                    "SELECT id FROM point_transactions WHERE LOWER(wallet_address)=? AND source='registration' LIMIT 1",
                    (wallet,),
                ).fetchone()
            if not _existing_reg:
                _signup_bonus = int(_FALLBACK_RULE_POINTS.get("signup", 0))
                if _signup_bonus > 0:
                    _add_points(
                        wallet, _signup_bonus, "registration", "注册奖励（补发）", ""
                    )
        except Exception as _signup_exc:
            logger.warning(
                "lazy signup bonus failed wallet=%s err=%s", wallet, _signup_exc
            )

        with db_connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(balance), 0) AS balance,
                    COALESCE(SUM(total_earned), 0) AS total_earned,
                    COALESCE(SUM(total_spent), 0) AS total_spent
                FROM user_points
                WHERE LOWER(wallet_address)=?
                """,
                (wallet,),
            ).fetchone()
            txns = conn.execute(
                """SELECT id, type, amount, source, description, created_at
               FROM point_transactions WHERE LOWER(wallet_address)=?
               ORDER BY created_at DESC LIMIT 50""",
                (wallet,),
            ).fetchall()
            month_txns = conn.execute(
                """SELECT type, amount, created_at
               FROM point_transactions WHERE LOWER(wallet_address)=?""",
                (wallet,),
            ).fetchall()

        current_balance = int(row["balance"] if row else 0)
        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        this_month = 0
        for txn in month_txns:
            item = dict(txn)
            amount = int(item.get("amount") or 0)
            txn_type = str(item.get("type") or "").strip().lower()
            if amount < 0:
                delta = amount
            elif txn_type in ("spend", "admin_deduct") or (
                "spend" in txn_type or "deduct" in txn_type
            ):
                delta = -amount
            else:
                delta = amount
            created_at = _parse_txn_dt(item.get("created_at"))
            if created_at and created_at >= month_start and delta > 0:
                this_month += delta

        running_balance = current_balance
        normalized_txns = []
        for t in txns:
            item = dict(t)
            amount = int(item.get("amount") or 0)
            txn_type = str(item.get("type") or "").strip().lower()
            if amount < 0:
                # admin_v3 keeps signed amounts; trust explicit sign first.
                delta = amount
            elif txn_type in ("spend", "admin_deduct") or (
                "spend" in txn_type or "deduct" in txn_type
            ):
                delta = -amount
            else:
                delta = amount
            normalized_txns.append(
                {
                    "id": item.get("id"),
                    "type": txn_type,
                    "source": item.get("source"),
                    "description": item.get("description") or item.get("source") or "-",
                    "event_type": item.get("source") or txn_type,
                    "amount": amount,
                    "delta": delta,
                    "balance_after": running_balance,
                    "created_at": item.get("created_at"),
                }
            )
            running_balance -= delta

        tier_info = _resolve_points_tier(int(row["balance"] if row else 0))

        return {
            "balance": row["balance"] if row else 0,
            "total_earned": row["total_earned"] if row else 0,
            "total_spent": row["total_spent"] if row else 0,
            "this_month": int(this_month),
            "tier": tier_info.get("tier") or "Bronze",
            "tier_badge": tier_info.get("tier_badge") or "bronze",
            "tier_id": tier_info.get("tier_id"),
            "next_tier_pts": int(tier_info.get("next_tier_pts") or 0),
            "recent_transactions": normalized_txns,
        }

    return await asyncio.to_thread(_db)


@router.get("/api/user/points/leaderboard")
async def points_leaderboard(
    limit: int = Query(20, ge=1, le=100),
):
    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                """SELECT up.wallet_address, up.balance, up.total_earned,
                      u.display_name
               FROM user_points up
               LEFT JOIN users u ON u.wallet_address = up.wallet_address
               ORDER BY up.total_earned DESC LIMIT ?""",
                (limit,),
            ).fetchall()

        return {
            "leaderboard": [
                {
                    "rank": i + 1,
                    "wallet": r["wallet_address"][:6]
                    + "..."
                    + r["wallet_address"][-4:],
                    "display_name": r["display_name"] or "匿名",
                    "balance": r["balance"],
                    "total_earned": r["total_earned"],
                }
                for i, r in enumerate(rows)
            ]
        }

    return await asyncio.to_thread(_db)


# ── 推荐 API ──


@router.get("/api/user/referral")
async def get_referral_info(user_session: str | None = Cookie(None)):
    wallet = _get_user_wallet(user_session)
    if not wallet:
        return JSONResponse({"error": "unauthorized"}, 401)
    wallet = str(wallet or "").strip().lower()

    def _db():

        def _mask_wallet(addr: str) -> str:
            raw = str(addr or "").strip()
            if len(raw) <= 12:
                return raw or "-"
            return f"{raw[:6]}...{raw[-4:]}"

        with db_connect() as conn:
            # 获取或创建推荐码
            code_row = conn.execute(
                "SELECT code FROM referral_codes WHERE LOWER(wallet_address)=?",
                (wallet,),
            ).fetchone()
            if not code_row:
                code = _generate_referral_code()
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO referral_codes(code, wallet_address, created_at) VALUES(?, ?, ?)",
                    (code, wallet, now),
                )
                conn.commit()
            else:
                code = code_row["code"]

            # 统计推荐数据（直接推荐 L1）
            l1_row = conn.execute(
                "SELECT COUNT(*) AS c FROM user_referrals WHERE LOWER(referrer_wallet)=?",
                (wallet,),
            ).fetchone()
            l1_count = int(l1_row["c"] if l1_row else 0)

            total_reward_points = conn.execute(
                "SELECT COALESCE(SUM(points), 0) AS s FROM referral_rewards WHERE LOWER(wallet_address)=?",
                (wallet,),
            ).fetchone()["s"]

            # 推荐详情（含三级网络）
            referrals = conn.execute(
                """
                WITH RECURSIVE chain(level, parent_wallet, referred_wallet, bound_at) AS (
                    SELECT
                        1 AS level,
                        LOWER(ur.referrer_wallet) AS parent_wallet,
                        LOWER(ur.referred_wallet) AS referred_wallet,
                        ur.created_at AS bound_at
                    FROM user_referrals ur
                    WHERE LOWER(ur.referrer_wallet)=?

                    UNION ALL

                    SELECT
                        c.level + 1 AS level,
                        LOWER(ur.referrer_wallet) AS parent_wallet,
                        LOWER(ur.referred_wallet) AS referred_wallet,
                        ur.created_at AS bound_at
                    FROM chain c
                    JOIN user_referrals ur
                      ON LOWER(ur.referrer_wallet)=c.referred_wallet
                    WHERE c.level < 3
                )
                SELECT
                    c.referred_wallet,
                    c.parent_wallet,
                    c.level,
                    c.bound_at,
                    u.created_at AS user_created_at
                FROM chain c
                LEFT JOIN users u
                  ON LOWER(u.wallet_address)=c.referred_wallet
                ORDER BY c.level ASC, COALESCE(u.created_at, c.bound_at) DESC
                LIMIT 300
                """,
                (wallet,),
            ).fetchall()

            # 我的推荐人
            my_referrer = conn.execute(
                "SELECT referrer_wallet FROM user_referrals WHERE LOWER(referred_wallet)=? AND level=1",
                (wallet,),
            ).fetchone()

        level_counts = {"l1": 0, "l2": 0, "l3": 0}
        for r in referrals:
            lv = int(r["level"] or 0)
            if lv in (1, 2, 3):
                level_counts[f"l{lv}"] += 1

        return {
            "code": code,
            "referral_code": code,
            "referral_link": f"https://veyra.trade/?ref={code}",
            "direct_referrals": l1_count,
            "total_referrals_all_levels": len(referrals),
            "level_counts": level_counts,
            "total_reward_points": total_reward_points,
            "my_referrer": (
                my_referrer["referrer_wallet"][:10] + "..." if my_referrer else None
            ),
            "referrals": [
                {
                    "wallet": _mask_wallet(r["referred_wallet"]),
                    "wallet_masked": _mask_wallet(r["referred_wallet"]),
                    "referred_wallet": r["referred_wallet"],
                    "parent_wallet": r["parent_wallet"],
                    "parent_wallet_masked": _mask_wallet(r["parent_wallet"]),
                    "level": r["level"],
                    "created_at": r["bound_at"],
                    "registered_at": r["user_created_at"] or r["bound_at"],
                }
                for r in referrals
            ],
        }

    return await asyncio.to_thread(_db)


@router.post("/api/user/referral/apply")
async def apply_referral_code(
    request: Request,
    user_session: str | None = Cookie(None),
):
    wallet = _get_user_wallet(user_session)
    if not wallet:
        return JSONResponse({"error": "unauthorized"}, 401)

    body = await request.json()
    code = (body.get("code") or "").strip().upper()
    if not code:
        return JSONResponse({"error": "missing referral code"}, 400)

    def _db():

        # 检查是否已被推荐
        with db_connect() as conn:
            existing = conn.execute(
                "SELECT id FROM user_referrals WHERE LOWER(referred_wallet)=?",
                (wallet,),
            ).fetchone()
            if existing:
                return JSONResponse({"error": "已有推荐关系，不可重复绑定"}, 400)

        chain = _process_referral_chain(wallet, code)
        if not chain:
            return JSONResponse({"error": "无效的推荐码或不可推荐自己"}, 400)

        return {
            "ok": True,
            "bonus_points": 0,
            "rewards_distributed": 0,
            "referral_bound": True,
            "referral_chain_levels": len(chain),
        }

    return await asyncio.to_thread(_db)


# ── 手续费 API ──


@router.get("/api/user/fees")
async def get_fee_info(request: Request, user_session: str | None = Cookie(None)):
    wallet = _get_user_wallet_from_request(request, user_session)
    if not wallet:
        return JSONResponse({"error": "unauthorized"}, 401)

    def _db():

        with db_connect() as conn:
            # 当前费率配置
            fee_cfg = conn.execute(
                "SELECT * FROM fee_config WHERE active=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()

            # 用户积分余额（算折扣）
            pts = conn.execute(
                "SELECT balance FROM user_points WHERE wallet_address=?", (wallet,)
            ).fetchone()
            pts_balance = pts["balance"] if pts else 0

            # 匹配折扣层级
            tier = conn.execute(
                """SELECT discount_pct, description FROM fee_discount_tiers
               WHERE active=1 AND min_points <= ?
               ORDER BY min_points DESC LIMIT 1""",
                (pts_balance,),
            ).fetchone()

            # 费用统计
            fee_stats = conn.execute(
                """SELECT COUNT(*) AS trades, COALESCE(SUM(final_fee), 0) AS total_fees,
                      COALESCE(SUM(discount_amount), 0) AS total_discounts
               FROM fee_transactions WHERE wallet_address=?""",
                (wallet,),
            ).fetchall()
            stats = (
                dict(fee_stats[0])
                if fee_stats
                else {"trades": 0, "total_fees": 0, "total_discounts": 0}
            )

        return {
            "current_rate_bps": fee_cfg["rate_bps"] if fee_cfg else 0,
            "fee_type": fee_cfg["fee_type"] if fee_cfg else "builder_taker",
            "points_balance": pts_balance,
            "discount_pct": tier["discount_pct"] if tier else 0,
            "discount_tier": tier["description"] if tier else "无折扣",
            "total_trades": stats["trades"],
            "total_fees_paid": round(stats["total_fees"], 4),
            "total_discounts_saved": round(stats["total_discounts"], 4),
        }

    return await asyncio.to_thread(_db)


@router.get("/api/user/fees/history")
async def get_fee_history(
    request: Request,
    user_session: str | None = Cookie(None),
    limit: int = Query(50, ge=1, le=200),
):
    wallet = _get_user_wallet_from_request(request, user_session)
    if not wallet:
        return JSONResponse({"error": "unauthorized"}, 401)

    def _db():

        with db_connect() as conn:
            rows = conn.execute(
                """SELECT bet_id, trade_type, gross_amount, fee_rate_bps, fee_amount,
                      discount_points, discount_amount, final_fee, created_at
               FROM fee_transactions WHERE wallet_address=?
               ORDER BY created_at DESC LIMIT ?""",
                (wallet, limit),
            ).fetchall()

        return {"history": [dict(r) for r in rows]}

    return await asyncio.to_thread(_db)


# ── 标签 API (T263) ──


@router.get("/api/tags")
async def get_all_tags(
    scope: str = Query("bet", regex="^(bet|market)$"),
    include_pm: bool = Query(True),
    category: str = Query(""),
):
    """获取所有已使用的标签及计数"""
    table = "bet_tags" if scope == "bet" else "market_tags"
    category_slug = _normalize_category_slug(category)

    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                f"SELECT tag, COUNT(*) AS count FROM {table} GROUP BY tag ORDER BY count DESC"
            ).fetchall()

            merged: dict[str, dict] = {}
            allowed_pm_slugs: set[str] = set()
            for row in rows:
                tag = str(row["tag"] or "").strip().lower()
                if not tag:
                    continue
                merged[tag] = {
                    "tag": tag,
                    "count": int(row["count"] or 0),
                    "source": "usage",
                }

            if include_pm:
                try:
                    if category_slug:
                        pm_rows = conn.execute(
                            """
                        SELECT slug, label, category_slug
                        FROM pm_tags
                        WHERE active=1 AND category_slug=?
                        ORDER BY label ASC
                        """,
                            (category_slug,),
                        ).fetchall()
                    else:
                        pm_rows = conn.execute("""
                        SELECT slug, label, category_slug
                        FROM pm_tags
                        WHERE active=1
                        ORDER BY label ASC
                        """).fetchall()
                    for pr in pm_rows:
                        tag = str(pr["slug"] or "").strip().lower()
                        if not tag:
                            continue
                        allowed_pm_slugs.add(tag)
                        if tag not in merged:
                            merged[tag] = {
                                "tag": tag,
                                "count": 0,
                                "source": "pm",
                            }
                        merged[tag]["label"] = str(pr["label"] or tag)
                        cat_value = None
                        try:
                            cat_value = pr["category_slug"]
                        except Exception:
                            cat_value = None
                        merged[tag]["category_slug"] = _normalize_category_slug(
                            cat_value
                        )
                except Exception:
                    # pm_tags 表未创建或查询失败时，回退到原有标签行为
                    pass

            if category_slug:
                merged = {k: v for k, v in merged.items() if k in allowed_pm_slugs}

        out = list(merged.values())
        out.sort(key=lambda x: (-int(x.get("count") or 0), str(x.get("tag") or "")))
        return {"tags": out}

    return await asyncio.to_thread(_db)


@router.get("/api/user/bets/tags")
async def get_bet_tags(
    bet_id: str = Query(...),
    user_session: str | None = Cookie(None),
):
    wallet = _get_user_wallet(user_session)
    if not wallet:
        return JSONResponse({"error": "unauthorized"}, 401)

    def _db():

        with db_connect() as conn:
            rows = conn.execute(
                "SELECT tag, source, created_at FROM bet_tags WHERE bet_id=?",
                (bet_id,),
            ).fetchall()
        return {"tags": [dict(r) for r in rows]}

    return await asyncio.to_thread(_db)


@router.post("/api/user/bets/tags")
async def add_bet_tag(
    request: Request,
    user_session: str | None = Cookie(None),
):
    wallet = _get_user_wallet(user_session)
    if not wallet:
        return JSONResponse({"error": "unauthorized"}, 401)

    body = await request.json()
    bet_id = body.get("bet_id", "").strip()
    tag = body.get("tag", "").strip().lower()

    if not bet_id or not tag:
        return JSONResponse({"error": "missing bet_id or tag"}, 400)
    if len(tag) > 50:
        return JSONResponse({"error": "tag too long"}, 400)

    now = datetime.now(timezone.utc).isoformat()

    def _db():
        with db_connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO bet_tags(bet_id, tag, source, created_at)
               VALUES(?, ?, 'user', ?)""",
                (bet_id, tag, now),
            )
            conn.commit()
        return {"ok": True}

    return await asyncio.to_thread(_db)


@router.delete("/api/user/bets/tags")
async def remove_bet_tag(
    bet_id: str = Query(...),
    tag: str = Query(...),
    user_session: str | None = Cookie(None),
):
    wallet = _get_user_wallet(user_session)
    if not wallet:
        return JSONResponse({"error": "unauthorized"}, 401)

    def _db():

        with db_connect() as conn:
            conn.execute(
                "DELETE FROM bet_tags WHERE bet_id=? AND tag=? AND source='user'",
                (bet_id, tag.strip().lower()),
            )
            conn.commit()
        return {"ok": True}

    return await asyncio.to_thread(_db)


# ── 积分规则查询 ──


@router.get("/api/points/rules")
async def get_point_rules():
    """获取当前生效的积分规则"""

    def _db():
        _ensure_default_point_rules()
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT action_type, points, description, max_daily FROM point_rules WHERE active=1"
            ).fetchall()
        l1 = _rule_points("referral_l1", default=0)
        l2 = _rule_points("referral_l2", default=0)
        l3 = _rule_points("referral_l3", default=0)
        referred = _rule_points("referral_referred", default=0)
        signup = _rule_points("signup", default=0)
        return {
            "rules": [dict(r) for r in rows],
            "referral_rewards": {"1": l1, "2": l2, "3": l3, "referred": referred},
            "registration_bonus": signup,
        }

    return await asyncio.to_thread(_db)


# ── 手续费折扣层级查询 ──


@router.get("/api/fees/discount-tiers")
async def get_discount_tiers():
    def _db():
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT min_points, discount_pct, description FROM fee_discount_tiers WHERE active=1 ORDER BY min_points"
            ).fetchall()
        return {"tiers": [dict(r) for r in rows]}

    return await asyncio.to_thread(_db)
