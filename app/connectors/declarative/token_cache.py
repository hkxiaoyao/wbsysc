"""Connection-scoped OAuth token cache for declarative connectors.

A ``DeclarativeConnector`` is rebuilt for every request, so an instance-level
cache would never hit.  This module owns the only longer-lived state in the
declarative path.

Two invariants shape the design:

* A cache key never embeds credential plaintext.  Credential values are folded
  into a keyed BLAKE2b fingerprint under a process-local salt, so rotating a
  secret changes the key by construction and no explicit invalidation hook is
  required to avoid serving a token minted from a retired secret.
* Nothing here is ever handed to the audit sink, the response cache, or a log
  record.  ``__repr__`` deliberately reports counts instead of contents.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal


# Retire a token this long before the upstream expiry so an in-flight request
# cannot outlive it.
EXPIRY_SAFETY_MARGIN_SECONDS = 60.0
# Used when the token response omits a usable ``expires_in``.  Short enough to
# recover from an unannounced upstream revocation without re-exchanging often.
DEFAULT_LIFETIME_SECONDS = 300.0
MAX_LIFETIME_SECONDS = 86_400.0

_INFLIGHT_OK: Literal["ok"] = "ok"
_INFLIGHT_FAILED: Literal["failed"] = "failed"

# Never persisted and never shared between processes.  A fingerprint is only
# meaningful to the process that produced it.
_FINGERPRINT_SALT = secrets.token_bytes(32)


def _fingerprint(*parts: str) -> str:
    """Return a salted, length-prefixed digest that cannot be reversed."""
    digest = hashlib.blake2b(key=_FINGERPRINT_SALT, digest_size=16)
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class TokenCacheKey:
    """An opaque cache identity; it carries no credential or token material."""

    connection_id: str
    scheme_fingerprint: str
    credential_fingerprint: str


@dataclass(frozen=True)
class _Entry:
    token: str
    expires_at: float
    generation: int


class OAuthTokenCache:
    """A small TTL cache for client-credentials tokens, scoped per connection."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[TokenCacheKey, _Entry] = {}
        self._generations: dict[str, int] = {}
        self._entries_lock = threading.Lock()
        self._inflight: dict[TokenCacheKey, asyncio.Future[tuple[str, str]]] = {}
        self._inflight_lock = asyncio.Lock()

    def __repr__(self) -> str:
        with self._entries_lock:
            entries = len(self._entries)
        return f"<OAuthTokenCache entries={entries}>"

    def cache_key(
        self,
        connection_id: str,
        *,
        token_url: str,
        client_id_key: str,
        client_secret_key: str,
        scopes: tuple[str, ...],
        client_id: str,
        client_secret: str,
    ) -> TokenCacheKey:
        """Derive the opaque identity for one connection's token exchange."""
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_id is required")
        return TokenCacheKey(
            connection_id=connection_id,
            scheme_fingerprint=_fingerprint(
                token_url, client_id_key, client_secret_key, *scopes
            ),
            credential_fingerprint=_fingerprint(client_id, client_secret),
        )

    def get(self, key: TokenCacheKey) -> str | None:
        now = self._clock()
        with self._entries_lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                return None
            return entry.token

    def discard(self, key: TokenCacheKey) -> bool:
        """Drop one exact entry, e.g. after the upstream rejected its token."""
        with self._entries_lock:
            return self._entries.pop(key, None) is not None

    def invalidate_connection(self, connection_id: str) -> int:
        """Drop every token for one connection and retire in-flight exchanges.

        Safe to call from a synchronous post-commit hook; it never awaits.
        """
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_id is required")
        with self._entries_lock:
            self._generations[connection_id] = (
                self._generations.get(connection_id, 0) + 1
            )
            keys = [key for key in self._entries if key.connection_id == connection_id]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    async def get_or_exchange(
        self,
        key: TokenCacheKey,
        exchange: Callable[[], Awaitable[tuple[str, float]]],
    ) -> str:
        """Return a live token, collapsing concurrent misses into one exchange.

        ``exchange`` must return ``(token, lifetime_seconds)`` where the
        lifetime is the upstream validity, not the cache TTL.
        """
        cached = self.get(key)
        if cached is not None:
            return cached

        async with self._inflight_lock:
            pending = self._inflight.get(key)
            if pending is None:
                pending = asyncio.get_running_loop().create_future()
                self._inflight[key] = pending
                owner = True
            else:
                owner = False

        if not owner:
            # A cancelled waiter must not cancel the shared future the owner
            # and its peers are waiting on.
            outcome, token = await asyncio.shield(pending)
            if outcome == _INFLIGHT_OK:
                return token
            raise RuntimeError("OAuth token request failed")

        try:
            with self._entries_lock:
                generation = self._generations.get(key.connection_id, 0)
            token, lifetime = await exchange()
            self._store(key, token, lifetime, generation)
            if not pending.done():
                pending.set_result((_INFLIGHT_OK, token))
            return token
        except asyncio.CancelledError:
            if not pending.done():
                pending.cancel()
            raise
        except BaseException:
            # Resolve rather than reject: an unretrieved future exception would
            # surface as a spurious asyncio warning for peers that went away.
            if not pending.done():
                pending.set_result((_INFLIGHT_FAILED, ""))
            raise
        finally:
            async with self._inflight_lock:
                if self._inflight.get(key) is pending:
                    self._inflight.pop(key, None)

    def _store(
        self,
        key: TokenCacheKey,
        token: str,
        lifetime: float,
        generation: int,
    ) -> None:
        ttl = self._cache_ttl(lifetime)
        if ttl <= 0:
            return
        expires_at = self._clock() + ttl
        with self._entries_lock:
            # An invalidation that landed during the exchange must win, or a
            # disabled connection could keep a token minted moments earlier.
            if self._generations.get(key.connection_id, 0) != generation:
                return
            self._entries[key] = _Entry(
                token=token,
                expires_at=expires_at,
                generation=generation,
            )

    @staticmethod
    def _cache_ttl(lifetime: float) -> float:
        """Convert an upstream lifetime into a usable, bounded cache TTL."""
        if (
            isinstance(lifetime, bool)
            or not isinstance(lifetime, (int, float))
            or lifetime != lifetime  # NaN
            or lifetime <= 0
        ):
            return DEFAULT_LIFETIME_SECONDS
        return min(float(lifetime) - EXPIRY_SAFETY_MARGIN_SECONDS, MAX_LIFETIME_SECONDS)


def parse_lifetime_seconds(value: object) -> float:
    """Read a token response ``expires_in`` without trusting its type.

    Returns ``0`` for anything unusable so the caller falls back to the bounded
    default instead of caching on an attacker-influenced lifetime.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if value != value or value <= 0:  # NaN or non-positive
        return 0.0
    return min(float(value), MAX_LIFETIME_SECONDS)


_default_cache = OAuthTokenCache()


def get_default_token_cache() -> OAuthTokenCache:
    return _default_cache


__all__ = [
    "DEFAULT_LIFETIME_SECONDS",
    "EXPIRY_SAFETY_MARGIN_SECONDS",
    "MAX_LIFETIME_SECONDS",
    "OAuthTokenCache",
    "TokenCacheKey",
    "get_default_token_cache",
    "parse_lifetime_seconds",
]
