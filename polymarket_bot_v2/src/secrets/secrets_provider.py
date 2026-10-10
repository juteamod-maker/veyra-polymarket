"""
SecretsProvider — abstract base for all credential sources.

MVP supports env-var backed provider.
Future: KmsSecretsProvider, VaultSecretsProvider, etc.
"""

from abc import ABC, abstractmethod


class SecretsProvider(ABC):
    """Abstract interface for retrieving secrets.

    All business-logic code must depend on this interface.
    Direct os.getenv() calls outside of concrete implementations are forbidden.
    """

    @abstractmethod
    def get_secret(self, name: str, *, tenant_id: str | None = None) -> str | None:
        """Return the secret value for *name*, or None if not found.

        Args:
            name: Secret identifier, e.g. "POLY_PRIVATE_KEY".
            tenant_id: Optional tenant scope (used by multi-tenant providers).
        """
        ...

    def require_secret(self, name: str, *, tenant_id: str | None = None) -> str:
        """Like get_secret(), but raises ValueError if not found."""
        value = self.get_secret(name, tenant_id=tenant_id)
        if not value:
            scope = f" (tenant={tenant_id})" if tenant_id else ""
            raise ValueError(
                f"Required secret '{name}'{scope} is not configured. "
                "Check your .env file or secrets backend."
            )
        return value
