#!/usr/bin/env python3
"""Mark existing users as CLOB API-key authorized (one-time migration).

Usage:
  python3 scripts/mark_clob_api_key_authorized.py

This script sets:
  users.clob_api_key_authorized = TRUE
  users.clob_api_key_authorized_at = <now>
  users.clob_api_key_address = <RELAYER_API_KEY_ADDRESS or POLY_PRIVATE_KEY address>

It is intended for existing test users/admins to avoid manual one-by-one authorization.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from src.utils.database import connect as db_connect


def _resolve_api_key_address() -> str:
    env_addr = str(os.environ.get("RELAYER_API_KEY_ADDRESS") or "").strip().lower()
    if env_addr.startswith("0x") and len(env_addr) == 42:
        return env_addr
    pk = str(os.environ.get("POLY_PRIVATE_KEY") or "").strip()
    if not pk:
        return ""
    from eth_account import Account  # noqa: PLC0415

    return str(Account.from_key(pk).address or "").strip().lower()


def main() -> int:
    api_key_address = _resolve_api_key_address()
    if not api_key_address:
        print("ERROR: unable to resolve API key address")
        return 1

    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        for ddl in (
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS clob_api_key_authorized BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS clob_api_key_authorized_at TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS clob_api_key_address TEXT",
        ):
            try:
                conn.execute(ddl)
            except Exception:
                pass

        updated = conn.execute(
            """
            UPDATE users
            SET clob_api_key_authorized=TRUE,
                clob_api_key_authorized_at=?,
                clob_api_key_address=?
            """,
            (now, api_key_address),
        )
        conn.commit()

    count = int(getattr(updated, "rowcount", 0) or 0)
    print(f"OK: marked {count} users as authorized for api_key_address={api_key_address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
