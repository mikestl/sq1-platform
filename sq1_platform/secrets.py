"""SQ1 Platform — Key Vault secrets helper.

Wraps `azure.keyvault.secrets.SecretClient` with:
- Lazy connection via DefaultAzureCredential (works for Managed Identity in Azure
  AND for `az login` sessions on a developer's machine)
- Short-TTL caching to avoid hitting KV on every call
- Atomic update (set replaces value, cache invalidated)

Usage:
    from sq1_platform import Secrets
    s = Secrets()
    api_key = s.get("wojie-api-key")

    # Always-fresh read (skip cache):
    token = s.get("qbo-refresh-token", fresh=True)

    # Write back (e.g., after rotation):
    s.set("qbo-refresh-token", new_value)
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Tuple

import azure.identity
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.keyvault.secrets import SecretClient

DEFAULT_VAULT_URL = "https://sq1-platform-kv.vault.azure.net/"

log = logging.getLogger("sq1_platform.secrets")


class Secrets:
    """Cached Key Vault secret accessor.

    Default cache TTL is 5 minutes — appropriate for slow-changing secrets like
    client credentials. Use `fresh=True` for tokens that rotate (refresh tokens,
    access tokens, etc).
    """

    def __init__(
        self,
        vault_url: str = DEFAULT_VAULT_URL,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self.vault_url = vault_url
        self._client = SecretClient(
            vault_url=vault_url,
            credential=azure.identity.DefaultAzureCredential(),
        )
        self._cache: Dict[str, Tuple[str, float]] = {}
        self._cache_ttl = cache_ttl_seconds

    def get(self, name: str, fresh: bool = False) -> str:
        """Read a secret value. Cached unless ``fresh=True``."""
        now = time.time()
        if not fresh:
            entry = self._cache.get(name)
            if entry and entry[1] > now:
                return entry[0]
        try:
            secret = self._client.get_secret(name)
        except ResourceNotFoundError:
            raise KeyError(f"Secret {name!r} not found in {self.vault_url}") from None
        except HttpResponseError as e:
            log.error("KV get %s failed: %s", name, e)
            raise
        self._cache[name] = (secret.value, now + self._cache_ttl)
        return secret.value

    def set(self, name: str, value: str) -> None:
        """Write a secret. Invalidates the local cache for ``name``."""
        try:
            self._client.set_secret(name, value)
        except HttpResponseError as e:
            log.error("KV set %s failed: %s", name, e)
            raise
        self._cache.pop(name, None)
        log.info("KV secret %s updated", name)

    def invalidate(self, name: str | None = None) -> None:
        """Clear cache (one entry or all)."""
        if name is None:
            self._cache.clear()
        else:
            self._cache.pop(name, None)
