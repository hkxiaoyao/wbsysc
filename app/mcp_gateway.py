"""Connection-scoped MCP Streamable HTTP gateway.

The gateway deliberately uses the low-level MCP server API.  A connection
receives an independent server instance, so its tool cache and handlers cannot
be shared with another connection.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text
from starlette.responses import JSONResponse

from .auth import ConnectionCtx, current_ctx
from .config import get_settings
from .connections import store as connection_store
from .connections.crypto import decrypt_credential
from .connections.models import ConnectionRecord, ToolPolicy
from .connectors import (
    ConnectionContext,
    ConnectorRegistry,
    ConnectorRuntime,
)
from .connectors.declarative.provider import DeclarativeConnectorProvider
from .connectors.runtime import (
    ConnectionConnectorResolver,
    ConnectorAuditEvent,
    ToolPolicyStore,
)
from .connectors.wecom import WeComConnector
from .mcp_audit import current_request_metadata, safe_summary, write_event
from .mcp_log_models import McpLogEvent
from .tenant import get_tenant_by_token, reload_tenants


logger = logging.getLogger(__name__)


def default_wecom_connection_id(tenant_id: str) -> str:
    """Return the stable Task 1 connection ID for a legacy WeCom tenant."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"wbsysc:legacy-wecom:{tenant_id}"))


def _build_transport_security() -> TransportSecuritySettings:
    """Preserve the existing MCP Host/Origin protection policy."""
    settings = get_settings()
    hosts: list[str] = []
    origins: list[str] = []

    def host_base(value: str) -> str:
        value = (value or "").strip().lower().rstrip(".")
        if value.endswith(":*"):
            value = value[:-2]
        if value.startswith("[") and "]" in value:
            return value[1:value.index("]")]
        return value.split(":")[0]

    def is_local_host(value: str) -> bool:
        return host_base(value) in ("127.0.0.1", "localhost", "::1")

    def add_host(value: str) -> None:
        value = (value or "").strip().lower().rstrip(".")
        if not value:
            return
        if value not in hosts:
            hosts.append(value)
        if not value.endswith(":*") and ":" not in value.strip("[]"):
            wildcard = f"{value}:*"
            if wildcard not in hosts:
                hosts.append(wildcard)
        base = host_base(value)
        for scheme in ("https", "http"):
            origin = f"{scheme}://{base}"
            if origin not in origins:
                origins.append(origin)
            wildcard_origin = f"{origin}:*"
            if wildcard_origin not in origins:
                origins.append(wildcard_origin)

    for item in (settings.mcp_allowed_hosts or "").split(","):
        add_host(item)
    if settings.mcp_base_url:
        try:
            parsed = urlparse(settings.mcp_base_url)
            if parsed.hostname:
                add_host(
                    parsed.hostname
                    if not parsed.port
                    else f"{parsed.hostname}:{parsed.port}"
                )
        except Exception:
            # An invalid optional URL must not disable configured host controls.
            pass

    if not [host for host in hosts if not is_local_host(host)]:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    for host in ("127.0.0.1", "localhost", "[::1]"):
        add_host(host)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


class ConnectionResolver:
    """Resolve bearer tokens only in the connection scope selected by the route."""

    def __init__(
        self,
        *,
        token_resolver: Callable[[str, str], ConnectionRecord | None] | None = None,
        legacy_tenant_lookup: Callable[[str], Any] | None = None,
        legacy_tenant_reload: Callable[[], None] | None = None,
        credential_loader: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> None:
        self._token_resolver = token_resolver or connection_store.resolve_connection_token
        self._legacy_tenant_lookup = legacy_tenant_lookup or get_tenant_by_token
        self._legacy_tenant_reload = legacy_tenant_reload or reload_tenants
        self._credential_loader = credential_loader or _load_connection_credentials

    def resolve(self, connection_id: str, bearer_token: str) -> ConnectionCtx | None:
        """Resolve only the supplied connection/token pair; never fall back by token."""
        if not _nonempty_string(connection_id) or not _nonempty_string(bearer_token):
            return None
        try:
            record = self._token_resolver(bearer_token, connection_id)
        except Exception as exc:
            logger.warning("MCP connection token resolution failed type=%s", type(exc).__name__)
            return None
        if record is None:
            return None
        try:
            return _connection_ctx(record, expected_connection_id=connection_id)
        except Exception as exc:
            logger.warning("MCP connection record rejected type=%s", type(exc).__name__)
            return None

    def resolve_legacy(self, bearer_token: str) -> ConnectionCtx | None:
        """Map an old `/mcp` token to its Task 1 default WeCom connection."""
        if not _nonempty_string(bearer_token):
            return None
        try:
            tenant = self._legacy_tenant_lookup(bearer_token)
            if tenant is None:
                self._legacy_tenant_reload()
                tenant = self._legacy_tenant_lookup(bearer_token)
            tenant_id = getattr(tenant, "tenant_id", "")
            if not _nonempty_string(tenant_id):
                return None
            return self.resolve(default_wecom_connection_id(tenant_id), bearer_token)
        except Exception as exc:
            logger.warning("MCP legacy token resolution failed type=%s", type(exc).__name__)
            return None

    def execution_context(self, ctx: ConnectionCtx) -> ConnectionContext:
        """Load credentials only after a connection token has been resolved."""
        if not isinstance(ctx, ConnectionCtx):
            raise TypeError("connection context is required")
        credentials = self._credential_loader(ctx.connection_id)
        return ConnectionContext(
            connection=ConnectionRecord(
                connection_id=ctx.connection_id,
                tenant_id=ctx.tenant_id,
                connector_key=ctx.connector_key,
                display_name="",
                status="active",
                data_mode=ctx.data_mode,
                public_config=dict(ctx.public_config),
                config_version=ctx.config_version,
            ),
            credentials=credentials,
            request_metadata=current_request_metadata(),
        )


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _connection_ctx(
    record: ConnectionRecord,
    *,
    expected_connection_id: str,
) -> ConnectionCtx:
    if not isinstance(record, ConnectionRecord):
        raise TypeError("resolved record must be a ConnectionRecord")
    if record.connection_id != expected_connection_id:
        raise ValueError("resolved record does not match requested connection")
    if record.status != "active":
        raise ValueError("resolved record is not active")
    return ConnectionCtx(
        tenant_id=record.tenant_id,
        connection_id=record.connection_id,
        connector_key=record.connector_key,
        data_mode=record.data_mode,
        public_config=record.public_config,
        config_version=record.config_version,
    )


def _load_connection_credentials(connection_id: str) -> Mapping[str, Any]:
    """Decrypt a connection's credentials for in-memory connector use only."""
    statement = text("""
        SELECT credential_key, encrypted_value
        FROM connection_credential
        WHERE connection_id=:connection_id
    """)
    try:
        with connection_store._engine().connect() as conn:  # noqa: SLF001
            rows = conn.execute(statement, {"connection_id": connection_id}).mappings().all()
        credentials: dict[str, Any] = {}
        for row in rows:
            credential_key = row.get("credential_key")
            encrypted_value = row.get("encrypted_value")
            if not _nonempty_string(credential_key) or encrypted_value is None:
                raise ValueError("invalid credential row")
            credentials[credential_key] = decrypt_credential(bytes(encrypted_value))
        return credentials
    except Exception as exc:
        logger.warning("MCP connection credential load failed type=%s", type(exc).__name__)
        raise RuntimeError("connection credentials unavailable") from None


class _DatabaseToolPolicyStore(ToolPolicyStore):
    """A bounded, fail-closed adapter over the Task 1 policy table."""

    def get(self, connection_id: str, tool_key: str) -> ToolPolicy | None:
        statement = text("""
            SELECT connection_id, tool_name, enabled, policy_json
            FROM connection_tool_policy
            WHERE connection_id=:connection_id AND tool_name=:tool_name
            LIMIT 1
        """)
        try:
            with connection_store._engine().connect() as conn:  # noqa: SLF001
                row = conn.execute(
                    statement,
                    {"connection_id": connection_id, "tool_name": tool_key},
                ).mappings().fetchone()
            if row is None:
                return None
            policy = json.loads(row["policy_json"] or "{}")
            if not isinstance(policy, Mapping):
                raise ValueError("policy must be an object")
            raw_enabled = row["enabled"]
            if isinstance(raw_enabled, bool):
                enabled = raw_enabled
            elif isinstance(raw_enabled, int) and raw_enabled in (0, 1):
                enabled = bool(raw_enabled)
            else:
                raise ValueError("policy enabled flag is invalid")
            return ToolPolicy(
                connection_id=str(row["connection_id"]),
                tool_name=str(row["tool_name"]),
                enabled=enabled,
                policy=dict(policy),
            )
        except Exception as exc:
            # A policy-read failure must not silently fall back to default-enabled.
            logger.warning("MCP tool policy load failed type=%s", type(exc).__name__)
            return ToolPolicy(
                connection_id=connection_id,
                tool_name=tool_key,
                enabled=False,
                policy={},
            )


@dataclass
class _SessionEntry:
    server: Server


class ConnectionMcpGateway:
    """Serve independent low-level MCP servers keyed by connection revision."""

    def __init__(
        self,
        *,
        resolver: ConnectionResolver | Any | None = None,
        runtime: ConnectorRuntime | None = None,
        transport_security: TransportSecuritySettings | None = None,
        json_response: bool = True,
    ) -> None:
        self.resolver = resolver or ConnectionResolver()
        self._runtime = runtime or self._default_runtime()
        self._transport_security = transport_security or _build_transport_security()
        self._json_response = json_response
        self._entries: dict[tuple[str, int], _SessionEntry] = {}
        self._manager_lock: asyncio.Lock | None = None
        self._run_count = 0
        self._invalidation_loop: asyncio.AbstractEventLoop | None = None
        self._store_invalidator: Callable[[str, int], None] | None = None

    def _default_runtime(self) -> ConnectorRuntime:
        registry = ConnectorRegistry(
            [WeComConnector(mock_enabled=lambda: get_settings().wecom_use_mock)]
        )
        return ConnectorRuntime(
            registry,
            policy_store=_DatabaseToolPolicyStore(),
            audit_sink=self._write_runtime_audit,
            connector_resolver=ConnectionConnectorResolver(
                registry,
                declarative_provider=DeclarativeConnectorProvider(),
            ),
        )

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Keep the safe connection/version server cache for the app lifespan."""
        if self._run_count == 0:
            self._manager_lock = asyncio.Lock()
            self._invalidation_loop = asyncio.get_running_loop()
            self._store_invalidator = self._invalidate_after_connection_mutation
            connection_store.register_connection_cache_invalidator(
                self._store_invalidator
            )
        self._run_count += 1
        try:
            yield
        finally:
            self._run_count -= 1
            if self._run_count == 0:
                invalidator = self._store_invalidator
                if invalidator is not None:
                    connection_store.unregister_connection_cache_invalidator(invalidator)
                lock = self._manager_lock
                if lock is not None:
                    async with lock:
                        self._entries.clear()
                self._manager_lock = None
                self._invalidation_loop = None
                self._store_invalidator = None

    def _invalidate_after_connection_mutation(
        self,
        connection_id: str,
        config_version: int,
    ) -> None:
        """Schedule raw-secret-free exact cache retirement from a store commit."""
        loop = self._invalidation_loop
        if loop is None or loop.is_closed():
            return
        coroutine = self.invalidate_connection(connection_id, config_version)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            task = loop.create_task(coroutine)
            task.add_done_callback(self._report_invalidation_failure)
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception as exc:
            coroutine.close()
            logger.warning(
                "MCP cache invalidation scheduling failed type=%s",
                type(exc).__name__,
            )
            return
        future.add_done_callback(self._report_invalidation_failure)

    @staticmethod
    def _report_invalidation_failure(future) -> None:
        try:
            future.result()
        except Exception as exc:
            logger.warning(
                "MCP cache invalidation failed type=%s",
                type(exc).__name__,
            )

    async def invalidate_connection(
        self,
        connection_id: str,
        config_version: int,
    ) -> bool:
        """Retire precisely one cached `(connection_id, config_version)` server.

        Call this after credential, Token, policy, status, or declarative
        revision changes.  The key is deliberately exact so another connection
        and another version of the same connection cannot be disrupted.
        """
        key = (connection_id, config_version)
        lock = self._manager_lock
        if lock is None:
            return False
        async with lock:
            entry = self._entries.pop(key, None)
            return entry is not None

    @property
    def cached_session_keys(self) -> tuple[tuple[str, int], ...]:
        """Read-only cache diagnostics; keys carry no bearer or credential data."""
        return tuple(self._entries)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            return
        if not _is_mount_root(scope):
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return

        try:
            ctx = current_ctx()
        except Exception:
            await _generic_auth_response(scope, receive, send)
            return
        if not isinstance(ctx, ConnectionCtx):
            await _generic_auth_response(scope, receive, send)
            return

        route_connection_id = scope.get("path_params", {}).get("connection_id")
        if route_connection_id is not None and route_connection_id != ctx.connection_id:
            # Defense in depth: authentication should already have rejected it.
            await _generic_auth_response(scope, receive, send)
            return

        try:
            manager = await self._manager_for(ctx)
            # Streamable HTTP is stateless here, so its manager is request-local.
            # Entering and closing it in this task avoids retaining request
            # context in a long-lived cache or crossing anyio task boundaries.
            async with manager.run():
                await manager.handle_request(scope, receive, send)
        except Exception as exc:
            logger.warning("MCP session setup failed type=%s", type(exc).__name__)
            await JSONResponse(
                {"errcode": 503, "errmsg": "MCP 服务暂不可用"},
                status_code=503,
            )(scope, receive, send)
            return

    async def _manager_for(self, ctx: ConnectionCtx) -> StreamableHTTPSessionManager:
        """Build a request-local manager around a safe cached server definition."""
        key = (ctx.connection_id, ctx.config_version)
        lock = self._manager_lock
        if lock is None:
            raise RuntimeError("MCP gateway is not running")
        async with lock:
            entry = self._entries.get(key)
            if entry is not None:
                server = entry.server
            else:
                # A newer connection revision is a precise boundary: sessions tied
                # to older versions of this connection must not remain routable.
                stale_keys = [
                    cached_key
                    for cached_key in self._entries
                    if cached_key[0] == ctx.connection_id and cached_key != key
                ]
                for stale_key in stale_keys:
                    self._entries.pop(stale_key)

                # Only ConnectionCtx is retained by the cached server.  It carries
                # public connection/version state, never request metadata or
                # credentials.  Handlers create those sensitive values per call.
                server = self._build_server(ctx)
                self._entries[key] = _SessionEntry(server=server)

        return StreamableHTTPSessionManager(
            app=server,
            json_response=self._json_response,
            # Stateless Streamable HTTP permits a direct protocol request at
            # `/mcp/{connection_id}` without caching request-bound managers.
            stateless=True,
            security_settings=self._transport_security,
        )

    def _build_server(self, connection_ctx: ConnectionCtx) -> Server:
        server = Server("connection-mcp-gateway")

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            try:
                execution_context = self.resolver.execution_context(connection_ctx)
                tools = self._runtime.list_enabled_tools(execution_context)
                return [_to_mcp_tool(tool) for tool in tools]
            except Exception as exc:
                logger.warning("MCP tool enumeration failed type=%s", type(exc).__name__)
                return []

        @server.call_tool(validate_input=True)
        async def call_tool(name: str, arguments: dict[str, Any]):
            try:
                execution_context = self.resolver.execution_context(connection_ctx)
                result = await self._runtime.execute(execution_context, name, arguments)
                return result.data
            except Exception as exc:
                # Never stringify a connector exception; it can contain a third-
                # party response body or a credential-bearing URL.
                logger.warning("MCP tool execution failed type=%s", type(exc).__name__)
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text="Tool execution failed")],
                    isError=True,
                )

        return server

    def _write_runtime_audit(self, event: ConnectorAuditEvent) -> None:
        try:
            metadata = current_request_metadata()
            write_event(
                McpLogEvent(
                    tenant_id=safe_summary(event.tenant_id, 64),
                    connection_id=safe_summary(event.connection_id, 64),
                    connector_key=safe_summary(event.connector_key, 64),
                    tool_key=safe_summary(event.tool_key, 128),
                    category="tool",
                    event_name=safe_summary(event.tool_key, 96),
                    target=safe_summary(event.connection_id, 256),
                    params_summary="omitted",
                    result_status=event.status,
                    error_code=safe_summary(event.error_code, 64),
                    error_summary=safe_summary(event.error_summary, 256),
                    cost_ms=max(0, int(event.cost_ms)),
                    request_id=metadata.get("request_id", ""),
                    client_ip=metadata.get("client_ip", ""),
                    http_method=metadata.get("http_method", ""),
                )
            )
        except Exception as exc:
            logger.warning("MCP tool audit failed type=%s", type(exc).__name__)


def _to_mcp_tool(tool) -> types.Tool:
    return types.Tool(
        name=tool.mcp_name,
        description=tool.description,
        inputSchema=dict(tool.input_schema),
        outputSchema=(None if tool.output_schema is None else dict(tool.output_schema)),
    )


def _is_mount_root(scope: Mapping[str, Any]) -> bool:
    path = str(scope.get("path", ""))
    root_path = str(scope.get("root_path", ""))
    if not root_path or not path.startswith(root_path):
        return False
    child_path = path[len(root_path):]
    return child_path in ("", "/")


async def _generic_auth_response(scope, receive, send) -> None:
    await JSONResponse(
        {"errcode": 401, "errmsg": "Token 无效或未绑定连接"},
        status_code=401,
    )(scope, receive, send)
