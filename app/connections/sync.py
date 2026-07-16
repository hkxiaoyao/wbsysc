"""Connection-scoped synchronization scheduling with redaction-only outcomes."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol

from sqlalchemy import text

from .models import ConnectionRecord, SyncState as ConnectionSyncState
from ..connectors.contracts import ConnectionContext, SyncResult
from ..connectors.registry import ConnectorRegistry
from ..mcp_log_models import McpLogEvent


logger = logging.getLogger(__name__)

_SAFE_SYNC_METRICS = frozenset(
    {
        "pulled",
        "stored",
        "partial_count",
        "skipped",
        "busy",
        "cached",
        "count",
    }
)
_WECOM_RESOURCE_ALIASES = {
    "report": "report",
    "reports": "report",
    "approval": "approval",
    "approvals": "approval",
    "checkin": "checkin",
    "checkins": "checkin",
}
_SAFE_RESOURCE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SENSITIVE_RESOURCE_PARTS = frozenset(
    {"authorization", "cookie", "credential", "password", "secret", "token"}
)


class ConnectionContextBuilder(Protocol):
    """Build the short-lived credential-bearing context after connection lookup."""

    def build(self, connection: ConnectionRecord) -> ConnectionContext: ...


class ResolverConnectionContextBuilder:
    """Adapt the Task 4 resolver without exposing its credential loader."""

    def __init__(self, resolver: Any | None = None) -> None:
        self._resolver = resolver

    def build(self, connection: ConnectionRecord) -> ConnectionContext:
        # The resolver is the published boundary that decrypts credentials only
        # for a server-side ConnectionCtx.  This module never reads credentials.
        from ..auth import ConnectionCtx
        from ..mcp_gateway import ConnectionResolver

        resolver = self._resolver or ConnectionResolver()
        return resolver.execution_context(
            ConnectionCtx(
                tenant_id=connection.tenant_id,
                connection_id=connection.connection_id,
                connector_key=connection.connector_key,
                data_mode=connection.data_mode,
                public_config=connection.public_config,
                config_version=connection.config_version,
            )
        )


class _ConnectionLocks:
    """One nonblocking process-local lock per opaque connection ID.

    The scheduler invokes this orchestrator from separate event loops in worker
    threads.  ``asyncio.Lock`` is loop-bound under contention, so this registry
    uses thread-safe locks and short cooperative polling instead.
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @asynccontextmanager
    async def acquire(self, connection_id: str):
        with self._guard:
            lock = self._locks.setdefault(connection_id, threading.Lock())
        while not lock.acquire(blocking=False):
            # This never blocks an event loop and, unlike a blocking worker
            # acquire, cannot claim a lock after its coroutine is cancelled.
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            lock.release()


def _safe_resource_key(value: str | None) -> str:
    resource_key = "default" if value is None else value
    if (
        not isinstance(resource_key, str)
        or not resource_key
        or _SAFE_RESOURCE_KEY_RE.fullmatch(resource_key) is None
        or any(
            part in _SENSITIVE_RESOURCE_PARTS
            for part in re.split(r"[^a-z0-9]+", resource_key.lower())
            if part
        )
    ):
        raise ValueError("resource_key is required")
    return resource_key


def _safe_sync_data(data: Any) -> dict[str, int | bool]:
    """Keep only bounded scalar counters; never retain upstream response bodies."""
    if not isinstance(data, Mapping):
        return {}
    safe: dict[str, int | bool] = {}
    for key in _SAFE_SYNC_METRICS:
        value = data.get(key)
        if isinstance(value, bool):
            safe[key] = value
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            safe[key] = value
    return safe


def _eligible(connection: ConnectionRecord) -> bool:
    if connection.status != "active" or connection.data_mode not in {
        "stored",
        "hybrid",
    }:
        return False
    if connection.data_mode == "hybrid":
        return connection.public_config.get("sync_enabled") is not False
    return True


def _sync_resources(
    connection: ConnectionRecord, resource_key: str | None
) -> tuple[str, ...]:
    if resource_key is not None:
        return (_safe_resource_key(resource_key),)
    configured = connection.public_config.get("sync_resources")
    if isinstance(configured, (list, tuple)):
        resources = []
        for value in configured:
            try:
                resources.append(_safe_resource_key(value))
            except ValueError:
                continue
        # An explicit list is an allowlist.  If every label is unsafe or
        # malformed, do not fall back to an implicit sync target.
        return tuple(dict.fromkeys(resources))
    if connection.connector_key == "wecom":
        raw_modules = connection.public_config.get("enabled_modules", "")
        if isinstance(raw_modules, str):
            modules = raw_modules.split(",")
        elif isinstance(raw_modules, (list, tuple, set, frozenset)):
            modules = raw_modules
        else:
            modules = ()
        resources = tuple(
            dict.fromkeys(
                _WECOM_RESOURCE_ALIASES[module.strip().lower()]
                for module in modules
                if isinstance(module, str)
                and module.strip().lower() in _WECOM_RESOURCE_ALIASES
            )
        )
        return resources or ("report", "approval", "checkin")
    return ("default",)


def _row_values(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row)


def _connection_from_row(row: Any) -> ConnectionRecord | None:
    try:
        values = _row_values(row)
        public_config = json.loads(values.get("public_config_json") or "{}")
        if not isinstance(public_config, dict):
            return None
        record = ConnectionRecord(
            connection_id=values["connection_id"],
            tenant_id=values["tenant_id"],
            connector_key=values["connector_key"],
            display_name=values.get("display_name") or "",
            status=values["status"],
            data_mode=values["data_mode"],
            public_config=public_config,
            config_version=int(values["config_version"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not _eligible(record):
        return None
    return record


def list_syncable_connections() -> tuple[ConnectionRecord, ...]:
    """Read only active stored/hybrid public connection rows, never credentials."""
    from ..db import get_engine

    statement = text("""
        SELECT connection_id, tenant_id, connector_key, display_name, status,
               data_mode, public_config_json, config_version
        FROM connection_instance
        WHERE status=:status AND data_mode IN ('stored', 'hybrid')
        ORDER BY tenant_id, connection_id
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(statement, {"status": "active"}).mappings().all()
    return tuple(
        record for row in rows if (record := _connection_from_row(row)) is not None
    )


class SyncOrchestrator:
    """Serializes sync per connection and emits only safe status observations."""

    def __init__(
        self,
        registry: ConnectorRegistry,
        *,
        contexts: ConnectionContextBuilder,
        connection_lister: Callable[
            [], Iterable[ConnectionRecord]
        ] = list_syncable_connections,
        event_writer: Callable[[McpLogEvent], Any] | None = None,
    ) -> None:
        if not isinstance(registry, ConnectorRegistry):
            raise TypeError("registry must be a ConnectorRegistry")
        if not hasattr(contexts, "build") or not callable(contexts.build):
            raise TypeError("contexts must provide build(connection)")
        self._registry = registry
        self._contexts = contexts
        self._connection_lister = connection_lister
        self._event_writer = event_writer or _default_event_writer
        self._locks = _ConnectionLocks()

    async def run_connection(
        self,
        connection: ConnectionRecord,
        resource_key: str | None = None,
    ) -> SyncResult | None:
        """Synchronize one active stored/hybrid connection under its own lock."""
        if not isinstance(connection, ConnectionRecord):
            raise TypeError("connection must be a ConnectionRecord")
        # The direct-mode guard is intentionally before context construction or
        # connector lookup so no direct connection can reach sync code.
        if not _eligible(connection):
            return None
        resource = _safe_resource_key(resource_key)
        async with self._locks.acquire(connection.connection_id):
            try:
                connector = self._registry.get(connection.connector_key)
                spec = self._registry.validated_spec(connection.connector_key)
                if spec is None or spec.supports_sync is not True:
                    return None
                context = self._contexts.build(connection)
                if inspect.isawaitable(context):
                    context = await context
                if not isinstance(context, ConnectionContext):
                    raise TypeError(
                        "connection context builder returned an invalid context"
                    )
                if context.connection.connection_id != connection.connection_id:
                    raise ValueError("connection context scope does not match")
                result = connector.sync(context, resource)
                if not inspect.isawaitable(result):
                    raise TypeError(
                        "connector sync must return an awaitable SyncResult"
                    )
                result = await result
                if not isinstance(result, SyncResult):
                    raise TypeError("connector sync returned an invalid result")
                if (
                    result.connection_id != connection.connection_id
                    or result.resource_key != resource
                    or result.status not in {"ok", "partial", "error"}
                ):
                    raise ValueError("connector sync result scope is invalid")
                safe_result = SyncResult(
                    connection_id=connection.connection_id,
                    resource_key=resource,
                    data=_safe_sync_data(result.data),
                    status=result.status,
                )
            except Exception:
                # Exception strings and arbitrary types can contain upstream
                # payloads or credentials, so the public/logged failure is fixed.
                logger.warning("Connection sync failed code=sync_failed")
                safe_result = SyncResult(
                    connection_id=connection.connection_id,
                    resource_key=resource,
                    data={"error": "sync_failed"},
                    status="error",
                )
            self._write_event(connection, safe_result)
            return safe_result

    async def run_scheduled(
        self,
        *,
        connections: Iterable[ConnectionRecord] | None = None,
        resource_key: str | None = None,
    ) -> tuple[SyncResult, ...]:
        """Enumerate safe rows and run eligible resource syncs one at a time."""
        try:
            scheduled = (
                tuple(connections)
                if connections is not None
                else tuple(await asyncio.to_thread(self._connection_lister))
            )
        except Exception:
            logger.warning(
                "Connection sync enumeration failed code=sync_enumeration_failed"
            )
            return ()
        results: list[SyncResult] = []
        for connection in scheduled:
            if not isinstance(connection, ConnectionRecord) or not _eligible(
                connection
            ):
                continue
            for resource in _sync_resources(connection, resource_key):
                result = await self.run_connection(connection, resource)
                if result is not None:
                    results.append(result)
        return tuple(results)

    def _write_event(self, connection: ConnectionRecord, result: SyncResult) -> None:
        try:
            self._event_writer(
                McpLogEvent(
                    tenant_id=connection.tenant_id,
                    connection_id=connection.connection_id,
                    connector_key=connection.connector_key,
                    tool_key=result.resource_key,
                    category="protocol",
                    event_name="connection_sync",
                    target="",
                    params_summary="omitted",
                    result_status=result.status,
                    error_code="sync_failed" if result.status == "error" else "",
                    error_summary="sync failed" if result.status == "error" else "",
                )
            )
        except Exception:
            # Observability must not change sync behavior or reveal sink errors.
            logger.warning("Connection sync audit failed code=sync_audit_failed")


def _default_event_writer(event: McpLogEvent) -> Any:
    from ..mcp_audit import write_event

    return write_event(event)


__all__ = [
    "ConnectionContextBuilder",
    "ConnectionSyncState",
    "ResolverConnectionContextBuilder",
    "SyncOrchestrator",
    "list_syncable_connections",
]
