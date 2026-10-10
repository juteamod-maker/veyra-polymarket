"""
EnvSecretsProvider — MVP implementation backed by environment variables.

Reads all secrets from os.environ (populated by .env via python-dotenv).
This is the single legitimate place in the V2 codebase allowed to call os.getenv().
"""

import os

from src.secrets.secrets_provider import SecretsProvider


class EnvSecretsProvider(SecretsProvider):
    """Read secrets from environment variables.

    Single-tenant MVP: tenant_id is ignored.
    Multi-tenant future: subclass and override get_secret() to namespace by tenant.
    """

    # Canonical env-var names used across the V2 system.
    KNOWN_SECRETS: tuple[str, ...] = (
        "POLY_TX_PRIVATE_KEY",  # dedicated signing key for redeem/withdraw tx path
        "POLY_WITHDRAW_PRIVATE_KEY",  # legacy alias for tx signing override
        "POLY_PRIVATE_KEY",
        "POLY_API_KEY",
        "POLY_SECRET",
        "POLY_PASSPHRASE",
        "POLY_RELAYER_ADDRESS",  # proxy/funder wallet address
        "POLY_BUILDER_CODE",  # bytes32 builder code from Builder Profile
        "DEEPSEEK_API_KEY",
        "DASHBOARD_KEY",
        "TOTP_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "WEATHER_API_KEY",
        "ODDS_API_KEY",
    )

    def get_secret(self, name: str, *, tenant_id: str | None = None) -> str | None:
        """Return env var *name*, or None if unset / empty."""
        value = os.environ.get(name, "").strip()
        return value if value else None
