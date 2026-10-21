"""Token → Jira account cache keyed by a salted hash of the credentials (ADDENDUM-01 §1A.3).

The account ID comes from Jira's current-user endpoint called with the caller's own token; a
header claiming an identity is never trusted. The salt is random per process start and the token
itself is never kept, so the cache reveals nothing about tokens.
"""

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable

from cachetools import TTLCache

from pulseboard.credentials import Credentials
from pulseboard.domain.types import AccountId

IDENTITY_TTL_S = 600.0
IDENTITY_MAX_ENTRIES = 4096
_SALT_BYTES = 32


class IdentityCache:
    """Remembers which Jira account a set of credentials belongs to, for ten minutes."""

    def __init__(
        self,
        *,
        ttl_s: float = IDENTITY_TTL_S,
        max_entries: int = IDENTITY_MAX_ENTRIES,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._salt = secrets.token_bytes(_SALT_BYTES)
        self._accounts: TTLCache[str, AccountId] = TTLCache(max_entries, ttl_s, timer=timer)
        self._lock = threading.Lock()

    def key(self, credentials: Credentials) -> str:
        """The salted hash the credentials are cached under."""
        token = credentials.token.get_secret_value() if credentials.token else ""
        material = f"{credentials.email or ''}\0{token}".encode()
        return hmac.new(self._salt, material, hashlib.sha256).hexdigest()

    def resolve(self, credentials: Credentials, lookup: Callable[[], AccountId]) -> AccountId:
        """The cached account, or ``lookup()`` (a Jira call) on a miss. Runs in worker threads."""
        key = self.key(credentials)
        with self._lock:
            found = self._accounts.get(key)
        if found is not None:
            return found
        account = lookup()
        with self._lock:
            self._accounts[key] = account
        return account
