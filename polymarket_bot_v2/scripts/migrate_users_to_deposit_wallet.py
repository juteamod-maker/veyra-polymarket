#!/usr/bin/env python3
"""T881 (2026-05-06) — One-shot migration: users → deposit_wallet.

Forces every non-super_admin / non-admin user whose
``wallet_type != 'deposit_wallet'`` (or whose ``signature_type != 3``) to
the deterministic CREATE2 deposit-wallet derivation produced by
``src.utils.pm_deposit_wallet.derive_deposit_wallet_address``.

Side effects per user:
  * users.polymarket_wallet     ← derived deposit wallet
  * users.funder_address        ← derived deposit wallet
  * users.signature_type        ← 3
  * users.wallet_type           ← 'deposit_wallet'
  * user_pm_profiles row aligned (discovery_method='create2_derived')

super_admin / admin rows are skipped to preserve the operator's
pre-cutover Safe binding.

Usage:
  cd /opt/polymarket_bot_v2
  .venv/bin/python scripts/migrate_users_to_deposit_wallet.py            # dry run
  .venv/bin/python scripts/migrate_users_to_deposit_wallet.py --apply    # write changes
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.utils.database import db_connect  # noqa: E402
from src.utils.pm_deposit_wallet import derive_deposit_wallet_address  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("migrate_deposit_wallet")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Persist changes (omit for a dry run that only prints the plan).",
    )
    ap.add_argument(
        "--include-admins",
        action="store_true",
        help=(
            "T883: also migrate super_admin / admin rows. WARNING: existing "
            "Safe-held funds are NOT moved on-chain; operators must manually "
            "withdraw + redeposit through the new deposit_wallet."
        ),
    )
    args = ap.parse_args()

    now = datetime.now(timezone.utc).isoformat()
    plan: list[dict] = []

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT wallet_address, role,
                   COALESCE(wallet_type, '') AS wallet_type,
                   COALESCE(signature_type, 0) AS signature_type,
                   COALESCE(polymarket_wallet, '') AS polymarket_wallet,
                   COALESCE(funder_address, '') AS funder_address
            FROM users
            ORDER BY created_at ASC
            """
        ).fetchall()

    for r in rows:
        d = dict(r)
        role = str(d.get("role") or "").strip().lower()
        if role in ("super_admin", "admin") and not args.include_admins:
            continue
        if d["wallet_type"] == "deposit_wallet" and int(d["signature_type"]) == 3:
            continue
        plan.append(d)

    print(
        f"[migrate] candidates: {len(plan)} of {len(rows)} total "
        f"(super_admin/admin skipped, already-deposit_wallet skipped)"
    )
    for d in plan:
        print(
            f"  - {d['wallet_address']} role={d.get('role')!r} "
            f"wt={d['wallet_type']!r} st={d['signature_type']} "
            f"pmw={d['polymarket_wallet'][:14]}…"
        )

    if not args.apply:
        print("[migrate] dry-run only; pass --apply to commit changes")
        return 0

    if not plan:
        print("[migrate] nothing to do")
        return 0

    with db_connect() as conn:
        for d in plan:
            wallet = d["wallet_address"]
            derived = derive_deposit_wallet_address(wallet)
            logger.info(
                "[DepositWallet][migrate] wallet=%s old_pmw=%s old_st=%s "
                "→ deposit=%s st=3 wt=deposit_wallet",
                wallet,
                d["polymarket_wallet"][:14],
                d["signature_type"],
                derived,
            )
            conn.execute(
                """
                UPDATE users SET
                    polymarket_wallet=?,
                    main_wallet=COALESCE(NULLIF(main_wallet, ''), ?),
                    funder_address=?,
                    signature_type=3,
                    wallet_type='deposit_wallet',
                    updated_at=?
                WHERE wallet_address=?
                """,
                (derived, wallet, derived, now, wallet),
            )
            conn.execute(
                """
                INSERT INTO user_pm_profiles(
                    wallet_address, login_wallet, polymarket_wallet, proxy_wallet,
                    linked_wallets_json, discovery_method, discovery_confidence,
                    profile_json, last_synced_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '[]', 'create2_derived', 100.0, '{}', ?, '', ?, ?)
                ON CONFLICT(wallet_address) DO UPDATE SET
                    polymarket_wallet=excluded.polymarket_wallet,
                    proxy_wallet=excluded.proxy_wallet,
                    discovery_method=excluded.discovery_method,
                    discovery_confidence=excluded.discovery_confidence,
                    last_synced_at=excluded.last_synced_at,
                    updated_at=excluded.updated_at
                """,
                (wallet, wallet, derived, derived, now, now, now),
            )
        conn.commit()
    print(f"[migrate] applied {len(plan)} updates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
