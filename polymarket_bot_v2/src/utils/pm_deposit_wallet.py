"""T877 — Polymarket Deposit Wallet derivation.

After Polymarket's 2026-05-04 cutover all new accounts use a
"Deposit Wallet" (factory ``0x00000000000Fb5C9ADea0298D729A0CB3823Cc07``)
instead of legacy Gnosis Safe. The address is fully deterministic CREATE2,
so we compute it locally — PM accepts deposits to the counterfactual
address before deployment, and auto-deploys on the user's first on-chain
interaction (same pattern as legacy Safe).

This module wraps the official derivation from PM's own
``py-builder-relayer-client`` SDK using constants from
:mod:`src.utils.pm_v2_constants` (single source of truth).
"""

from __future__ import annotations

from src.utils.pm_v2_constants import (
    DEPOSIT_WALLET_FACTORY,
    DEPOSIT_WALLET_IMPLEMENTATION,
)


def derive_deposit_wallet_address(eoa: str) -> str:
    """Return the deterministic Polymarket Deposit Wallet address for ``eoa``.

    Pure CREATE2 — no private key, no network call. Uses Polymarket's official
    ``py-builder-relayer-client`` derivation with constants from
    :mod:`src.utils.pm_v2_constants`.
    """

    from py_builder_relayer_client.builder.derive import derive_deposit_wallet

    return derive_deposit_wallet(
        eoa,
        DEPOSIT_WALLET_FACTORY,
        DEPOSIT_WALLET_IMPLEMENTATION,
    )


def verify_with_relayer(address: str, timeout: float = 8.0) -> Optional[bool]:
    """Probe PM relayer ``/deployed`` for a derived deposit wallet address.

    Returns True/False if PM responds (deployed flag), or ``None`` on
    network/HTTP error. Used as a sanity hook — a 4xx here would indicate
    that the derivation drifted from PM's contracts.
    """

    try:
        r = requests.get(
            f"{_RELAYER_API}/deployed",
            params={"address": address, "type": "DEPOSIT_WALLET"},
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return bool(r.json().get("deployed"))
    except Exception:
        return None
