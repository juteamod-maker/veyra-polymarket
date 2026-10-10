#!/usr/bin/env python3
"""
VPS 工具：将指定钱包地址提升为 super_admin。

用法：
  python3 scripts/create_super_admin.py <wallet_address>
  python3 scripts/create_super_admin.py list
  python3 scripts/create_super_admin.py <wallet_address> --demote   # 降级为 manual_user

若用户记录不存在，会先创建再提升。
"""
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from datetime import datetime, timezone

from scripts.dashboard_routes._shared import _ensure_user_tables, db_connect


def list_users():
    _ensure_user_tables()
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT wallet_address, role, display_name, email, created_at, last_login_at FROM users ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    if not rows:
        print("（暂无用户记录）")
        return
    print(f"{'钱包地址':<44} {'角色':<15} {'昵称':<20} {'注册时间'}")
    print("-" * 110)
    for r in rows:
        print(
            f"{r['wallet_address']:<44} {r['role']:<15} {(r['display_name'] or '-'):<20} {r['created_at']}"
        )


def promote(wallet: str, role: str = "super_admin"):
    wallet = wallet.strip().lower()
    if not wallet.startswith("0x") or len(wallet) != 42:
        print(f"错误：无效的钱包地址格式：{wallet}", file=sys.stderr)
        sys.exit(1)

    _ensure_user_tables()
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT role FROM users WHERE wallet_address=?", (wallet,)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE users SET role=?, updated_at=? WHERE wallet_address=?",
                (role, now, wallet),
            )
        else:
            # 用户不存在则先创建
            conn.execute(
                """INSERT INTO users(wallet_address, role, created_at, last_login_at, source_ip, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(wallet_address) DO UPDATE SET role=excluded.role, updated_at=excluded.updated_at""",
                (wallet, role, now, now, "cli", now),
            )

        # 同步更新所有活跃会话
        conn.execute(
            "UPDATE user_sessions SET role=? WHERE wallet_address=?",
            (role, wallet),
        )
        conn.commit()

    action = "创建并提升" if not existing else "更新"
    print(f"✓ {action} {wallet} → {role}")
    print(f"  该用户现在登录时将拥有 is_admin=True 权限")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    if args[0] == "list":
        list_users()
        return

    wallet = args[0]
    if "--demote" in args:
        promote(wallet, role="manual_user")
    else:
        promote(wallet, role="super_admin")


if __name__ == "__main__":
    main()
    main()
    main()
