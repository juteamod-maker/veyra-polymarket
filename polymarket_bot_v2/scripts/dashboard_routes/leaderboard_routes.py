"""
dashboard_routes/leaderboard_routes.py — T610B 积分排行榜 API

仅一只端点：
  - GET /api/leaderboard/points?limit=50

数据源：user_points + users（display_name / avatar / created_at）。
排行依据：user_points.balance DESC, total_earned DESC, wallet_address ASC（稳定排序）。
公开端点（无需登录）：所有可见用户都可以看到排行榜，但只显示脱敏字段。
"""

import asyncio
import hashlib

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts.dashboard_routes._shared import db_connect, logger

router = APIRouter()


_VIRTUAL_POOL_TARGET = 50
_VIRTUAL_BALANCES = [
    3680,
    3510,
    3340,
    3180,
    3040,
    2910,
    2790,
    2680,
    2580,
    2490,
    2410,
    2330,
    2260,
    2190,
    2120,
    2060,
    2000,
    1940,
    1880,
    1820,
    1760,
    1700,
    1640,
    1580,
    1520,
    1460,
    1400,
    1340,
    1280,
    1220,
    1160,
    1100,
    1040,
    980,
    920,
    860,
    800,
    740,
    680,
    620,
    560,
    500,
    440,
    380,
    320,
    260,
    210,
    170,
    135,
    105,
]


def _virtual_earned(balance: int, idx: int) -> int:
    return int(balance + 120 + (idx % 7) * 11)


def _virtual_spent(balance: int, idx: int) -> int:
    earned = _virtual_earned(balance, idx)
    return max(0, int(earned - balance + 25 + (idx % 5) * 7))


def _virtual_wallet(idx: int) -> str:
    seed = f"v2:leaderboard:virtual:{idx}".encode("utf-8")
    digest = hashlib.sha1(seed).hexdigest()[:40]
    return f"0x{digest}"


def _build_virtual_items(limit: int) -> list[dict]:
    out: list[dict] = []
    n = max(limit, _VIRTUAL_POOL_TARGET)
    for i in range(n):
        balance = (
            _VIRTUAL_BALANCES[i]
            if i < len(_VIRTUAL_BALANCES)
            else max(10, _VIRTUAL_BALANCES[-1] - (i - len(_VIRTUAL_BALANCES) + 1) * 4)
        )
        wallet = _virtual_wallet(i + 1)
        out.append(
            {
                "id": wallet,
                "wallet": wallet,
                "wallet_short": _short_wallet(wallet),
                "name": wallet,
                "initial": "0",
                "avatar_url": "",
                "balance": int(balance),
                "total_earned": _virtual_earned(int(balance), i),
                "total_spent": "*****",
                "total_spent_value": _virtual_spent(int(balance), i),
                "joined_at": "",
                "updated_at": "",
                "is_virtual": True,
            }
        )
    return out


def _merge_with_real(
    virtual_items: list[dict], real_items: list[dict], limit: int
) -> list[dict]:
    mixed = list(virtual_items)
    real_sorted = sorted(
        real_items,
        key=lambda x: (
            -(int(x.get("balance") or 0)),
            -(int(x.get("total_earned") or 0)),
            str(x.get("id") or ""),
        ),
    )
    for real in real_sorted:
        rid = str(real.get("id") or "")
        mixed = [it for it in mixed if str(it.get("id") or "") != rid]
        mixed.append(real)
        mixed.sort(
            key=lambda x: (
                -(int(x.get("balance") or 0)),
                -(int(x.get("total_earned") or 0)),
                str(x.get("id") or ""),
            )
        )
        if len(mixed) > limit:
            # 保留刚插入的真实用户：优先裁掉末尾虚拟项。
            drop_idx = None
            for j in range(len(mixed) - 1, -1, -1):
                if mixed[j].get("is_virtual"):
                    drop_idx = j
                    break
            if drop_idx is None:
                drop_idx = len(mixed) - 1
            mixed.pop(drop_idx)
    mixed.sort(
        key=lambda x: (
            -(int(x.get("balance") or 0)),
            -(int(x.get("total_earned") or 0)),
            str(x.get("id") or ""),
        )
    )
    return mixed[:limit]


def _short_wallet(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr or ""
    return f"{addr[:6]}…{addr[-4:]}"


def _initial(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return "?"
    ch = s[0].upper()
    return ch if ch.isalnum() else "?"


def _safe_avatar(av: str) -> str:
    """白名单：只允许 https/http URL 或 data:image/(png|jpeg|svg+xml);base64,...
    其它（含原始 <svg> 富文本）一律丢弃，避免 XSS 通过 innerHTML 注入。
    服务端生成的 blockies SVG 这里也会被丢弃 — 由前端按 wallet 重新生成 fallback 字母头像即可。
    """
    if not av:
        return ""
    s = str(av).strip()
    low = s.lower()
    if low.startswith("https://") or low.startswith("http://"):
        return s[:500]
    if low.startswith("data:image/"):
        # data:image/<type>;base64,<...> 仅允许 png/jpeg/webp/gif/svg+xml
        head = low.split(",", 1)[0]
        if any(t in head for t in ("png", "jpeg", "jpg", "webp", "gif", "svg+xml")):
            return s[:8000]
    return ""


@router.get("/api/leaderboard/points")
async def leaderboard_points(request: Request):
    """积分排行榜：虚拟基线 + 真实用户插入混排。"""
    try:
        limit = int(request.query_params.get("limit") or 50)
    except Exception:
        limit = 50
    limit = max(1, min(limit, 200))

    def _fetch():
        with db_connect() as conn:
            cursor = conn.execute(
                """
                SELECT
                    up.wallet_address,
                    up.balance,
                    up.total_earned,
                    up.updated_at,
                    u.display_name,
                    u.avatar_url,
                    u.created_at
                FROM user_points up
                INNER JOIN users u ON LOWER(u.wallet_address) = LOWER(up.wallet_address)
                WHERE up.balance > 0 OR up.total_earned > 0
                ORDER BY up.balance DESC, up.total_earned DESC, up.wallet_address ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall() or []

            # 总览
            stats_cur = conn.execute("""
                SELECT
                    COUNT(*) AS users,
                    COALESCE(SUM(up.balance),0) AS total_balance,
                    COALESCE(SUM(up.total_earned),0) AS total_earned
                FROM user_points up
                INNER JOIN users u ON LOWER(u.wallet_address) = LOWER(up.wallet_address)
                WHERE up.balance > 0 OR up.total_earned > 0
                """)
            stats_row = stats_cur.fetchone()
            return rows, stats_row

    try:
        rows, stats_row = await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.warning(f"[leaderboard/points] {e}")
        return JSONResponse(
            {"ok": False, "error": str(e), "items": [], "totals": {}},
            status_code=500,
        )

    items: list[dict] = []
    for idx, r in enumerate(rows):
        if isinstance(r, dict):
            row = r
        else:
            row = {
                "wallet_address": r[0],
                "balance": r[1],
                "total_earned": r[2],
                "updated_at": r[3],
                "display_name": r[4],
                "avatar_url": r[5],
                "created_at": r[6],
            }
        addr = row.get("wallet_address") or ""
        balance = int(row.get("balance") or 0)
        total_earned = int(row.get("total_earned") or 0)
        name = addr
        items.append(
            {
                "id": addr,
                "wallet_short": _short_wallet(addr),
                "name": name,
                "initial": _initial(name),
                "avatar_url": _safe_avatar(row.get("avatar_url") or ""),
                "balance": balance,
                "total_earned": total_earned,
                "total_spent": "*****",
                "total_spent_value": int(row.get("total_spent") or 0),
                "joined_at": row.get("created_at") or "",
                "updated_at": row.get("updated_at") or "",
                "is_virtual": False,
            }
        )

    virtual_items = _build_virtual_items(limit)
    merged = _merge_with_real(virtual_items, items, limit)
    for idx, row in enumerate(merged):
        row["rank"] = idx + 1

    # Totals must match the list shown to users (virtual baseline + real users merged).
    s_users = len(merged)
    s_total_balance = sum(int(it.get("balance") or 0) for it in merged)
    s_total_earned = sum(int(it.get("total_earned") or 0) for it in merged)
    s_total_spent = sum(int(it.get("total_spent_value") or 0) for it in merged)
    public_items = []
    for row in merged:
        item = dict(row)
        item.pop("total_spent_value", None)
        public_items.append(item)

    return JSONResponse(
        {
            "ok": True,
            "items": public_items,
            "totals": {
                "users": s_users,
                "total_balance": s_total_balance,
                "total_earned": s_total_earned,
                "total_spent": s_total_spent,
            },
            "limit": limit,
        },
        headers={
            # 排行榜 30s 缓存：写入由 point_transactions 触发，30s 延迟可接受。
            "Cache-Control": "public, max-age=30, stale-while-revalidate=60",
        },
    )
