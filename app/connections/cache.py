"""Safe, connection-scoped in-memory data cache primitives."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar


_SENSITIVE_KEY_PARTS = frozenset(
    {
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "password",
        "secret",
        "session",
        "token",
        "apikey",
        "body",
        "payload",
        "request",
        "response",
    }
)
_SENSITIVE_VALUE_MARKERS = frozenset(
    {
        "apikey",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "password",
        "rawresponse",
        "secret",
        "session",
        "token",
    }
)
_OMITTED = "[omitted]"
_SAFE_SCOPE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_JWT_LIKE_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
    re.IGNORECASE,
)
_INFLIGHT_CACHED = "cached"
_INFLIGHT_BYPASS = "bypass"
_INFLIGHT_FAILED = "failed"
T = TypeVar("T")


class CacheLoadError(RuntimeError):
    """Fixed public cache-load failure that never includes an upstream error."""

    def __init__(self) -> None:
        super().__init__("cache load failed")


@dataclass(frozen=True)
class _Entry:
    expires_at: float
    value: Any


def _identifier(name: str, value: str) -> str:
    if (
        not isinstance(value, str)
        or _SAFE_SCOPE_KEY_RE.fullmatch(value) is None
        or any(
            part in _SENSITIVE_KEY_PARTS
            for part in re.split(r"[^a-z0-9]+", value.lower())
            if part
        )
    ):
        raise ValueError(f"{name} is required")
    return value


def _normalized_key_name(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _is_sensitive_key(value: object) -> bool:
    normalized = _normalized_key_name(value)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _contains_sensitive_value(value: Any, *, depth: int = 0) -> bool:
    """Reject values that could carry a credential or raw upstream body."""
    if depth > 8:
        return True
    if value is None or isinstance(value, (bool, int)):
        return False
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, str):
        normalized = _normalized_key_name(value)
        return (
            _JWT_LIKE_RE.search(value) is not None
            or any(marker in normalized for marker in _SENSITIVE_VALUE_MARKERS)
        )
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, Mapping):
        if len(value) > 200:
            return True
        for key, child in value.items():
            if (
                not isinstance(key, str)
                or _is_sensitive_key(key)
                or _contains_sensitive_value(child, depth=depth + 1)
            ):
                return True
        return False
    if isinstance(value, (list, tuple)):
        if len(value) > 200:
            return True
        return any(_contains_sensitive_value(child, depth=depth + 1) for child in value)
    return True


def _canonical_arguments(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe args projection, or raise for sensitive/unbounded input."""
    if depth > 8:
        raise ValueError("arguments are too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("arguments contain a non-finite number")
        return value
    if isinstance(value, Mapping):
        if len(value) > 200:
            raise ValueError("arguments contain too many values")
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or _is_sensitive_key(key):
                raise ValueError("arguments contain a sensitive key")
            normalized[key] = _canonical_arguments(child, depth=depth + 1)
        return normalized
    if isinstance(value, (list, tuple)):
        if len(value) > 200:
            raise ValueError("arguments contain too many values")
        return [_canonical_arguments(child, depth=depth + 1) for child in value]
    raise ValueError("arguments are not cacheable")


def normalized_args_hash(args: Mapping[str, Any] | None = None) -> str | None:
    """Hash safe, canonical arguments without retaining their plaintext values.

    Sensitive argument names deliberately disable caching rather than allowing a
    credential-bearing value (or even a credential-derived hash) to become part
    of a long-lived cache key.
    """
    try:
        canonical = _canonical_arguments({} if args is None else args)
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    """Copy a bounded result while omitting credentials and raw body fields."""
    if depth > 8:
        return _OMITTED
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _OMITTED
    if isinstance(value, str):
        return value[:2048]
    if isinstance(value, Mapping):
        if len(value) > 200:
            return _OMITTED
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or _is_sensitive_key(key):
                continue
            result[key] = _safe_value(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 200:
            return _OMITTED
        return [_safe_value(child, depth=depth + 1) for child in value]
    return _OMITTED


class ConnectionCache:
    """TTL cache keyed only by opaque connection/tool/argument dimensions."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[tuple[str, str, str], _Entry] = {}
        self._inflight: dict[tuple[str, str, str], asyncio.Future[tuple[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def _key(
        self,
        connection_id: str,
        tool_key: str,
        args: Mapping[str, Any] | None,
    ) -> tuple[str, str, str] | None:
        args_hash = normalized_args_hash(args)
        if args_hash is None:
            return None
        return (_identifier("connection_id", connection_id), _identifier("tool_key", tool_key), args_hash)

    async def get(
        self,
        connection_id: str,
        tool_key: str,
        args: Mapping[str, Any] | None = None,
    ) -> Any | None:
        key = self._key(connection_id, tool_key, args)
        if key is None:
            return None
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                self._entries.pop(key, None)
                return None
            return copy.deepcopy(entry.value)

    async def put(
        self,
        connection_id: str,
        tool_key: str,
        value: Any,
        *,
        ttl_seconds: float,
        args: Mapping[str, Any] | None = None,
    ) -> bool:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be a positive finite number")
        key = self._key(connection_id, tool_key, args)
        if key is None:
            return False
        # A bare textual/binary result is indistinguishable from a raw upstream
        # response body.  Retain only structured, redacted result projections.
        if value is None or isinstance(value, (str, bytes, bytearray, memoryview)):
            return False
        if _contains_sensitive_value(value):
            return False
        entry = _Entry(
            expires_at=self._clock() + float(ttl_seconds),
            value=_safe_value(value),
        )
        async with self._lock:
            self._entries[key] = entry
        return True

    async def get_or_load(
        self,
        connection_id: str,
        tool_key: str,
        args: Mapping[str, Any] | None = None,
        loader: Callable[[], Awaitable[T] | T] | None = None,
        *,
        ttl_seconds: float = 60,
        data_mode: str = "hybrid",
    ) -> T | Any:
        """Return a scoped hit or execute one loader without retaining its error.

        ``direct`` data mode is deliberately a cache bypass.  Sensitive or
        uncacheable arguments also bypass storage and execute the loader.
        """
        if loader is None and callable(args):
            loader = args
            args = None
        if not callable(loader):
            raise TypeError("loader must be callable")
        if data_mode == "direct":
            return await _resolve_loader(loader)
        if data_mode not in {"stored", "hybrid"}:
            raise ValueError("invalid data_mode")
        key = self._key(connection_id, tool_key, args)
        if key is None:
            return await _resolve_loader(loader)
        cached = await self.get(connection_id, tool_key, args)
        if cached is not None:
            return cached

        async with self._lock:
            existing = self._inflight.get(key)
            if existing is None:
                existing = asyncio.get_running_loop().create_future()
                self._inflight[key] = existing
                owner = True
            else:
                owner = False

        if not owner:
            # A cancelling waiter must not cancel the owner's shared future.
            # The owner separately propagates its own cancellation to peers.
            outcome, result = await asyncio.shield(existing)
            if outcome == _INFLIGHT_CACHED:
                return copy.deepcopy(result)
            if outcome == _INFLIGHT_BYPASS:
                # Never put an uncacheable owner value in the shared future.
                # Each waiter instead performs its own loader call.
                return await _resolve_loader(loader)
            raise CacheLoadError()

        try:
            result = await _resolve_loader(loader)
            retained = await self.put(
                connection_id,
                tool_key,
                result,
                ttl_seconds=ttl_seconds,
                args=args,
            )
            if retained:
                safe_result = await self.get(connection_id, tool_key, args)
                existing.set_result((_INFLIGHT_CACHED, safe_result))
            else:
                existing.set_result((_INFLIGHT_BYPASS, None))
            return result
        except asyncio.CancelledError:
            # Do not convert the owner's cancellation to a cache error.  Wake
            # all same-key waiters promptly instead of leaving them pending.
            if not existing.done():
                existing.cancel()
            raise
        except Exception:
            if not existing.done():
                existing.set_result((_INFLIGHT_FAILED, None))
            raise CacheLoadError() from None
        finally:
            async with self._lock:
                if self._inflight.get(key) is existing:
                    self._inflight.pop(key, None)

    async def invalidate_connection(self, connection_id: str) -> int:
        """Remove all cached data for one connection without inspecting values."""
        _identifier("connection_id", connection_id)
        async with self._lock:
            keys = [key for key in self._entries if key[0] == connection_id]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)


async def _resolve_loader(loader: Callable[[], Awaitable[T] | T]) -> T:
    value = loader()
    if hasattr(value, "__await__"):
        return await value
    return value
