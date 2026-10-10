"""
VagusPoly V2 — Secrets abstraction layer.

Business code must import from here, never call os.getenv() directly.
Switch implementations via SECRETS_PROVIDER_IMPL env var (default: env).
"""

from src.secrets.env_secrets_provider import EnvSecretsProvider
from src.secrets.secrets_provider import SecretsProvider

_provider: SecretsProvider | None = None


def get_secrets_provider() -> SecretsProvider:
    """Return the singleton SecretsProvider instance."""
    global _provider
    if _provider is None:
        _provider = EnvSecretsProvider()
    return _provider


def get_secret(name: str, tenant_id: str | None = None) -> str | None:
    """Convenience shortcut: get_secret("POLY_PRIVATE_KEY")."""
    return get_secrets_provider().get_secret(name, tenant_id=tenant_id)


__all__ = [
    "SecretsProvider",
    "EnvSecretsProvider",
    "get_secrets_provider",
    "get_secret",
]
