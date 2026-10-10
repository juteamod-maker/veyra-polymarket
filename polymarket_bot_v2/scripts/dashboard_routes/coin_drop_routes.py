"""
coin_drop_routes.py — T838 海岛/弹窗探宝金币收集激励

设计要点（强制约束，写死不可绕过）：
- 完全旁路：不引入任何新积分通道，所有积分增加调用 points_routes._add_points。
- 每日上限：默认 10 枚/账号/服务器 UTC 自然日；以 config/points_rules.json
  里 coin_drop.daily_cap 为准（admin 可调，运维通过修改 JSON 即可）。
- 单枚奖励：默认 10 积分；以 config/points_rules.json coin_drop.points_per_coin 为准。
- 双身份：
  * 注册用户：按 user_session 认证拿到 wallet_address；上限按 wallet 计算；
    每次成功 collect 立即调用 _add_points 入账，触发现有积分体系。
  * 游客：按 cookie `coin_guest_token` 跟踪；不入账，仅累加 pending_pts；
    用户登录后调用 status 接口会自动把 guest pending 合并到 wallet。
- 防刷：
  * UNIQUE(identity_key, date_utc) + UPDATE ... WHERE count<cap 原子化；
  * pending 总额封顶 guest_pending_max，避免被恶意刷出超大未结算余额。
- 复用性：组件不依赖任何具体页面，前端调用同一对接口即可。
"""

import json
import logging
import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import JSONResponse

from scripts.dashboard_routes._shared import (
    db_connect,
    get_request_identity,
    verify_user_session,
)
from scripts.dashboard_routes.points_routes import (
    _add_points,
    _ensure_points_account,
    _load_points_rules_json,
)

logger = logging.getLogger("dashboard.coin_drop")
router = APIRouter()

GUEST_COOKIE_NAME = "coin_guest_token"
GUEST_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

DEFAULT_DAILY_CAP = 10
DEFAULT_POINTS_PER_COIN = 10
DEFAULT_GUEST_PENDING_MAX = 200


# ── DDL ───────────────────────────────────────────────────────────────


def _ensure_tables() -> None:
    with db_connect() as conn:
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coin_drop_daily (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity_key    TEXT    NOT NULL,
                    identity_type   TEXT    NOT NULL,
                    date_utc        TEXT    NOT NULL,
                    count           INTEGER NOT NULL DEFAULT 0,
                    pending_pts     INTEGER NOT NULL DEFAULT 0,
                    updated_at      TEXT    NOT NULL,
                    UNIQUE(identity_key, date_utc)
                )
                """
            )
        except Exception:
            conn.rollback()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coin_drop_daily (
                    id              SERIAL PRIMARY KEY,
                    identity_key    TEXT    NOT NULL,
                    identity_type   TEXT    NOT NULL,
                    date_utc        TEXT    NOT NULL,
                    count           INTEGER NOT NULL DEFAULT 0,
                    pending_pts     INTEGER NOT NULL DEFAULT 0,
                    updated_at      TEXT    NOT NULL,
                    UNIQUE(identity_key, date_utc)
                )
                """
            )
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coin_drop_guest_pending (
                    guest_token         TEXT PRIMARY KEY,
                    pending_pts         INTEGER NOT NULL DEFAULT 0,
                    pending_coins       INTEGER NOT NULL DEFAULT 0,
                    last_collected_at   TEXT,
                    updated_at          TEXT    NOT NULL
                )
                """
            )
        except Exception:
            conn.rollback()
        # T839 — 每个海岛/弹窗位（slot）一天只能被同一身份采集一次。
        # 与 coin_drop_daily 联用：先写 slot 唯一约束（防同槽重复），再
        # 走 daily 计数（防全站超额）。两表对一次 collect 的全局约束。
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coin_drop_slots (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity_key    TEXT    NOT NULL,
                    date_utc        TEXT    NOT NULL,
                    slot_id         TEXT    NOT NULL,
                    collected_at    TEXT    NOT NULL,
                    UNIQUE(identity_key, date_utc, slot_id)
                )
                """
            )
        except Exception:
            conn.rollback()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS coin_drop_slots (
                        id              SERIAL PRIMARY KEY,
                        identity_key    TEXT    NOT NULL,
                        date_utc        TEXT    NOT NULL,
                        slot_id         TEXT    NOT NULL,
                        collected_at    TEXT    NOT NULL,
                        UNIQUE(identity_key, date_utc, slot_id)
                    )
                    """
                )
            except Exception:
                conn.rollback()
        conn.commit()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config() -> tuple[int, int, int]:
    """Return (daily_cap, points_per_coin, guest_pending_max)."""
    _rules, meta = _load_points_rules_json()
    daily_cap = DEFAULT_DAILY_CAP
    pts_per = DEFAULT_POINTS_PER_COIN
    pending_max = DEFAULT_GUEST_PENDING_MAX
    try:
        # _load_points_rules_json caches only known meta keys; read the whole
        # file directly so we can pick up nested coin_drop block without
        # touching the existing rule loading contract.
        path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__), "..", "..", "config", "points_rules.json"
            )
        )
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        cd = payload.get("coin_drop") if isinstance(payload, dict) else None
        if isinstance(cd, dict):
            daily_cap = max(1, int(cd.get("daily_cap") or DEFAULT_DAILY_CAP))
            pts_per = max(1, int(cd.get("points_per_coin") or DEFAULT_POINTS_PER_COIN))
            pending_max = max(
                pts_per,
                int(cd.get("guest_pending_max") or DEFAULT_GUEST_PENDING_MAX),
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("coin_drop config load failed: %s", exc)
    return daily_cap, pts_per, pending_max


def _resolve_wallet(request: Request, user_session: str | None) -> str | None:
    if user_session:
        sess = verify_user_session(user_session)
        if sess:
            wallet = str(sess.get("wallet_address") or "").strip().lower()
            if wallet:
                return wallet
    ident = get_request_identity(request)
    if ident and ident.get("authenticated"):
        wallet = str(ident.get("wallet_address") or "").strip().lower()
        return wallet or None
    return None


def _get_or_make_guest_token(request: Request) -> tuple[str, bool]:
    """Return (token, is_new). New token → caller must set cookie."""
    raw = request.cookies.get(GUEST_COOKIE_NAME) or ""
    raw = raw.strip()
    # Allow A-Z a-z 0-9 _- with length 16..64
    if 16 <= len(raw) <= 64 and all(
        c.isalnum() or c in ("_", "-") for c in raw
    ):
        return raw, False
    return secrets.token_urlsafe(24), True


def _read_daily_count(identity_key: str) -> tuple[int, int]:
    """Return (count_today, pending_pts_today)."""
    today = _today_utc()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT count, pending_pts FROM coin_drop_daily WHERE identity_key=? AND date_utc=?",
            (identity_key, today),
        ).fetchone()
    if not row:
        return 0, 0
    try:
        return int(row["count"] or 0), int(row["pending_pts"] or 0)
    except Exception:
        return 0, 0


def _try_increment_daily(
    identity_key: str, identity_type: str, cap: int, pts: int
) -> tuple[bool, int, int]:
    """Atomically increment today's counter when below cap.

    Returns (ok, new_count, new_pending_pts).
    """
    today = _today_utc()
    now = _now_iso()
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT count, pending_pts FROM coin_drop_daily WHERE identity_key=? AND date_utc=?",
            (identity_key, today),
        ).fetchone()
        if not existing:
            try:
                conn.execute(
                    """
                    INSERT INTO coin_drop_daily(
                        identity_key, identity_type, date_utc, count, pending_pts, updated_at
                    ) VALUES(?, ?, ?, 1, ?, ?)
                    """,
                    (identity_key, identity_type, today, pts, now),
                )
                conn.commit()
                return True, 1, pts
            except Exception as exc:
                conn.rollback()
                logger.warning("coin_drop insert race wallet=%s err=%s", identity_key, exc)
                existing = conn.execute(
                    "SELECT count, pending_pts FROM coin_drop_daily WHERE identity_key=? AND date_utc=?",
                    (identity_key, today),
                ).fetchone()
                if not existing:
                    return False, 0, 0

        cur_count = int(existing["count"] or 0)
        cur_pending = int(existing["pending_pts"] or 0)
        if cur_count >= cap:
            return False, cur_count, cur_pending
        new_count = cur_count + 1
        new_pending = cur_pending + pts
        cur = conn.execute(
            """
            UPDATE coin_drop_daily SET count=?, pending_pts=?, updated_at=?
            WHERE identity_key=? AND date_utc=? AND count=?
            """,
            (new_count, new_pending, now, identity_key, today, cur_count),
        )
        if cur.rowcount != 1:
            conn.rollback()
            row = conn.execute(
                "SELECT count, pending_pts FROM coin_drop_daily WHERE identity_key=? AND date_utc=?",
                (identity_key, today),
            ).fetchone()
            return (
                False,
                int(row["count"] or 0) if row else cur_count,
                int(row["pending_pts"] or 0) if row else cur_pending,
            )
        conn.commit()
        return True, new_count, new_pending


def _bump_guest_pending(
    guest_token: str, pts: int, coins: int, pending_max: int
) -> tuple[int, int]:
    """Add pts/coins to guest pending pool, capped by pending_max. Return (pending_pts, pending_coins)."""
    if not guest_token:
        return 0, 0
    now = _now_iso()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT pending_pts, pending_coins FROM coin_drop_guest_pending WHERE guest_token=?",
            (guest_token,),
        ).fetchone()
        cur_pts = int(row["pending_pts"] or 0) if row else 0
        cur_coins = int(row["pending_coins"] or 0) if row else 0
        new_pts = min(cur_pts + max(0, pts), max(pts, pending_max))
        new_coins = cur_coins + max(0, coins)
        if row:
            conn.execute(
                "UPDATE coin_drop_guest_pending SET pending_pts=?, pending_coins=?, last_collected_at=?, updated_at=? WHERE guest_token=?",
                (new_pts, new_coins, now, now, guest_token),
            )
        else:
            conn.execute(
                "INSERT INTO coin_drop_guest_pending(guest_token, pending_pts, pending_coins, last_collected_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                (guest_token, new_pts, new_coins, now, now),
            )
        conn.commit()
        return new_pts, new_coins


def _read_guest_pending(guest_token: str) -> tuple[int, int]:
    if not guest_token:
        return 0, 0
    with db_connect() as conn:
        row = conn.execute(
            "SELECT pending_pts, pending_coins FROM coin_drop_guest_pending WHERE guest_token=?",
            (guest_token,),
        ).fetchone()
    if not row:
        return 0, 0
    return int(row["pending_pts"] or 0), int(row["pending_coins"] or 0)


def _drain_guest_pending_to_wallet(guest_token: str, wallet: str) -> tuple[int, int]:
    """Move pending pts from guest pool into wallet via _add_points. Idempotent
    (zero-out after a successful add). Returns (added_pts, added_coins).
    """
    if not guest_token or not wallet:
        return 0, 0
    pending_pts, pending_coins = _read_guest_pending(guest_token)
    if pending_pts <= 0:
        return 0, 0
    now = _now_iso()
    # Zero out first to avoid double-merge under retry, then add. If add fails
    # we restore. This is the standard "lock then commit" pattern at app level.
    with db_connect() as conn:
        cur = conn.execute(
            "UPDATE coin_drop_guest_pending SET pending_pts=0, pending_coins=0, updated_at=? WHERE guest_token=? AND pending_pts=? AND pending_coins=?",
            (now, guest_token, pending_pts, pending_coins),
        )
        conn.commit()
        if cur.rowcount != 1:
            # Concurrent merge happened; re-read.
            return 0, 0
    try:
        _ensure_points_account(wallet)
        _add_points(
            wallet,
            int(pending_pts),
            "coin_drop_guest_merge",
            f"Merge guest coin_drop pending: {pending_coins} coins",
            guest_token,
        )
        return int(pending_pts), int(pending_coins)
    except Exception as exc:
        logger.error(
            "coin_drop guest merge add_points failed wallet=%s token=%s err=%s",
            wallet,
            guest_token,
            exc,
        )
        # Restore pending
        with db_connect() as conn:
            conn.execute(
                "UPDATE coin_drop_guest_pending SET pending_pts=?, pending_coins=?, updated_at=? WHERE guest_token=?",
                (pending_pts, pending_coins, _now_iso(), guest_token),
            )
            conn.commit()
        return 0, 0


def _attach_cookie(resp: JSONResponse, token: str, is_new_token: bool) -> JSONResponse:
    if is_new_token:
        resp.set_cookie(
            key=GUEST_COOKIE_NAME,
            value=token,
            max_age=GUEST_COOKIE_MAX_AGE,
            path="/",
            httponly=False,  # readable by JS for cross-tab visibility; not security-critical
            samesite="lax",
        )
    return resp


# ── T839 slot helpers ────────────────────────────────────────────────


_SLOT_ID_MAX_LEN = 64


def _normalise_slot_id(raw: object) -> str:
    """Return a sanitised slot id ('' if invalid). Allows [a-zA-Z0-9_-:.] up to 64 chars."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or len(s) > _SLOT_ID_MAX_LEN:
        return ""
    if not all(c.isalnum() or c in ("-", "_", ":", ".") for c in s):
        return ""
    return s


def _read_collected_slots(identity_key: str) -> list[str]:
    """Return today's collected slot_ids for the identity."""
    today = _today_utc()
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT slot_id FROM coin_drop_slots WHERE identity_key=? AND date_utc=?",
            (identity_key, today),
        ).fetchall() or []
    out = []
    for r in rows:
        try:
            v = r["slot_id"]
            if v:
                out.append(str(v))
        except Exception:
            continue
    return out


def _try_claim_slot(identity_key: str, slot_id: str) -> bool:
    """Atomically claim a slot for today. Returns True on first claim, False if duplicate."""
    if not slot_id:
        return True  # no slot id supplied → no slot constraint
    today = _today_utc()
    now = _now_iso()
    with db_connect() as conn:
        try:
            conn.execute(
                "INSERT INTO coin_drop_slots(identity_key, date_utc, slot_id, collected_at) VALUES(?, ?, ?, ?)",
                (identity_key, today, slot_id, now),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False


def _release_slot(identity_key: str, slot_id: str) -> None:
    """Roll back a slot claim (used when downstream points award fails)."""
    if not slot_id:
        return
    today = _today_utc()
    try:
        with db_connect() as conn:
            conn.execute(
                "DELETE FROM coin_drop_slots WHERE identity_key=? AND date_utc=? AND slot_id=?",
                (identity_key, today, slot_id),
            )
            conn.commit()
    except Exception:
        pass


def _legacy_attach_cookie(resp: JSONResponse, token: str, is_new_token: bool) -> JSONResponse:
    """Deprecated alias kept for callers; identical to _attach_cookie."""
    return _attach_cookie(resp, token, is_new_token)


# ── API ───────────────────────────────────────────────────────────────


@router.get("/api/coin_drop/status")
async def coin_drop_status(
    request: Request, user_session: str | None = Cookie(None)
):
    _ensure_tables()
    cap, pts_per, _pending_max = _config()
    wallet = _resolve_wallet(request, user_session)
    guest_token, is_new = _get_or_make_guest_token(request)

    merged_pts = 0
    merged_coins = 0
    if wallet and not is_new:
        merged_pts, merged_coins = _drain_guest_pending_to_wallet(guest_token, wallet)

    if wallet:
        identity_key = f"wallet:{wallet}"
        identity_type = "wallet"
    else:
        identity_key = f"guest:{guest_token}"
        identity_type = "guest"

    count, _today_pending = _read_daily_count(identity_key)
    pending_pts, pending_coins = (0, 0)
    if not wallet:
        pending_pts, pending_coins = _read_guest_pending(guest_token)

    # T839 — slot 级跟踪：今天已采集的 slot_id 列表（用于前端阻止同一槽位重复出币）
    collected_slots = _read_collected_slots(identity_key)

    # T846 — 登录用户返回当前积分余额，状态弹窗直接显示真实数字
    points_balance = None
    if wallet:
        try:
            with db_connect() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(balance),0) AS balance FROM user_points WHERE LOWER(wallet_address)=?",
                    (str(wallet).strip().lower(),),
                ).fetchone()
                points_balance = int(row["balance"] if row else 0)
        except Exception as exc:
            logger.warning("coin_drop status read balance failed wallet=%s err=%s", wallet, exc)
            points_balance = None

    payload = {
        "ok": True,
        "is_guest": wallet is None,
        "wallet": wallet or "",
        "today_count": count,
        "daily_cap": cap,
        "remaining": max(0, cap - count),
        "points_per_coin": pts_per,
        "pending_points": pending_pts,
        "pending_coins": pending_coins,
        "points_balance": points_balance,
        "guest_token": guest_token,
        "merged": {
            "points": merged_pts,
            "coins": merged_coins,
        },
        "identity_type": identity_type,
        "collected_slots": collected_slots,
    }
    resp = JSONResponse(payload)
    return _attach_cookie(resp, guest_token, is_new)


@router.post("/api/coin_drop/collect")
async def coin_drop_collect(
    request: Request, user_session: str | None = Cookie(None)
):
    _ensure_tables()
    cap, pts_per, pending_max = _config()
    wallet = _resolve_wallet(request, user_session)
    guest_token, is_new = _get_or_make_guest_token(request)

    # T839 — 读取 body 的可选 slot_id；slot_id 表示一次"位"（如海岛 id、
    # 弹窗 id）。同一身份在同一 UTC 自然日内同一 slot_id 仅可成功采集一次，
    # 防止用户通过反复打开同一位的弹窗刷取金币。
    slot_id = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            slot_id = _normalise_slot_id(body.get("slot_id"))
    except Exception:
        slot_id = ""

    if wallet:
        identity_key = f"wallet:{wallet}"
        identity_type = "wallet"
    else:
        identity_key = f"guest:{guest_token}"
        identity_type = "guest"

    # Step 1: claim the slot (if provided). If already claimed → reject.
    if slot_id and not _try_claim_slot(identity_key, slot_id):
        cur_count, _ = _read_daily_count(identity_key)
        payload = {
            "ok": False,
            "code": "slot_already_collected",
            "is_guest": wallet is None,
            "today_count": cur_count,
            "daily_cap": cap,
            "remaining": max(0, cap - cur_count),
            "points_per_coin": pts_per,
            "slot_id": slot_id,
        }
        resp = JSONResponse(payload, status_code=200)
        return _attach_cookie(resp, guest_token, is_new)

    ok, new_count, _ = _try_increment_daily(identity_key, identity_type, cap, pts_per)
    if not ok:
        # Daily cap hit AFTER slot was claimed → release the slot.
        _release_slot(identity_key, slot_id)
        payload = {
            "ok": False,
            "code": "daily_cap_reached",
            "is_guest": wallet is None,
            "today_count": new_count,
            "daily_cap": cap,
            "remaining": max(0, cap - new_count),
            "points_per_coin": pts_per,
        }
        resp = JSONResponse(payload, status_code=200)
        return _attach_cookie(resp, guest_token, is_new)

    awarded_now = 0
    pending_pts = 0
    pending_coins = 0
    points_balance = None

    if wallet:
        try:
            _ensure_points_account(wallet)
            new_balance = _add_points(
                wallet,
                int(pts_per),
                "coin_drop_island",
                f"Coin drop reward (#{new_count} today)",
                f"{_today_utc()}#{new_count}",
            )
            awarded_now = int(pts_per)
            points_balance = int(new_balance or 0)
        except Exception as exc:
            logger.error(
                "coin_drop add_points failed wallet=%s err=%s — rolling back daily counter",
                wallet,
                exc,
            )
            # Roll back the daily counter and slot claim so the user is not
            # charged a coin without receiving the points.
            _release_slot(identity_key, slot_id)
            try:
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE coin_drop_daily SET count=CASE WHEN count>0 THEN count-1 ELSE 0 END, pending_pts=CASE WHEN pending_pts>=? THEN pending_pts-? ELSE 0 END, updated_at=? WHERE identity_key=? AND date_utc=?",
                        (int(pts_per), int(pts_per), _now_iso(), identity_key, _today_utc()),
                    )
                    conn.commit()
            except Exception:
                pass
            payload = {
                "ok": False,
                "code": "points_award_failed",
                "is_guest": False,
                "today_count": max(0, new_count - 1),
                "daily_cap": cap,
                "remaining": max(0, cap - (new_count - 1)),
                "points_per_coin": pts_per,
            }
            resp = JSONResponse(payload, status_code=200)
            return _attach_cookie(resp, guest_token, is_new)
    else:
        pending_pts, pending_coins = _bump_guest_pending(
            guest_token, int(pts_per), 1, pending_max
        )

    payload = {
        "ok": True,
        "is_guest": wallet is None,
        "today_count": new_count,
        "daily_cap": cap,
        "remaining": max(0, cap - new_count),
        "points_per_coin": pts_per,
        "awarded_now": awarded_now,
        "pending_points": pending_pts,
        "pending_coins": pending_coins,
        "points_balance": points_balance,
        "guest_token": guest_token,
        "identity_type": identity_type,
        "slot_id": slot_id,
    }
    resp = JSONResponse(payload)
    return _attach_cookie(resp, guest_token, is_new)
