"""Tenant-safe MCP gateway projecting tools from multiple connections."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from .auth import ConnectionCtx
from .connections import store as connection_store
from .connections.models import ConnectionRecord, ToolPolicy
from .connectors.contracts import ConnectionContext, ExecutionResult, ToolDisabledError, ToolSpec
from .connectors.runtime import (
    ConnectorRuntime,
    InvalidToolPolicyError,
    PolicyGuard,
    RateLimitError,
    SlidingWindowRateLimiter,
)
from .mcp_audit import (
    client_ip_from_scope,
    current_request_metadata,
    request_id_from_scope,
    safe_summary,
    write_event,
)
from .mcp_gateway import (
    ConnectionMcpGateway,
    _build_transport_security,
    _is_mount_root,
)
from .mcp_log_models import McpLogEvent
from .mcp_services import store as service_store
from .mcp_services.models import McpService, ServiceToolBinding


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceContext:
    service_id: str
    tenant_id: str
    config_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.service_id, str) or not 0 < len(self.service_id) <= 64:
            raise ValueError("service_id is required")
        if not isinstance(self.tenant_id, str) or not 0 < len(self.tenant_id) <= 64:
            raise ValueError("tenant_id is required")
        if (
            isinstance(self.config_version, bool)
            or not isinstance(self.config_version, int)
            or self.config_version < 1
        ):
            raise ValueError("config_version must be positive")


class ServiceResolver:
    """Resolve a bearer token only for the service selected by the route."""

    def __init__(
        self,
        *,
        token_resolver: Callable[[str, str], McpService | None] | None = None,
    ) -> None:
        self._token_resolver = token_resolver or service_store.resolve_token

    def resolve(self, service_id: str, raw_token: str) -> ServiceContext | None:
        if not isinstance(service_id, str) or not service_id:
            return None
        if not isinstance(raw_token, str) or not raw_token:
            return None
        try:
            service = self._token_resolver(raw_token, service_id)
        except Exception as exc:
            logger.warning("MCP service token resolution failed type=%s", type(exc).__name__)
            return None
        if (
            not isinstance(service, McpService)
            or service.service_id != service_id
            or service.status != "active"
        ):
            return None
        return ServiceContext(
            service_id=service.service_id,
            tenant_id=service.tenant_id,
            config_version=service.config_version,
        )


@dataclass(frozen=True)
class ProjectedTool:
    alias: str
    connection_id: str
    source_tool_key: str
    spec: ToolSpec


@dataclass(frozen=True)
class _ResolvedProjection:
    projected: ProjectedTool
    binding: ServiceToolBinding
    context: ConnectionContext
    policy: Any


@dataclass
class _SessionEntry:
    server: Server


class ServiceMcpGateway:
    """Serve materialized service aliases without widening source policies."""

    def __init__(
        self,
        *,
        resolver: ServiceResolver | None = None,
        runtime: ConnectorRuntime | None = None,
        binding_loader: Callable[[str, str], Sequence[ServiceToolBinding]] | None = None,
        connection_loader: Callable[[str, str], ConnectionRecord | None] | None = None,
        connection_context_builder: Callable[[ConnectionCtx], ConnectionContext]
        | None = None,
        transport_security: TransportSecuritySettings | None = None,
        json_response: bool = True,
        audit_writer: Callable[[McpLogEvent], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        default_connection_gateway: ConnectionMcpGateway | None = None
        if runtime is None or connection_context_builder is None:
            default_connection_gateway = ConnectionMcpGateway()
        self.resolver = resolver or ServiceResolver()
        self._runtime = runtime or default_connection_gateway._runtime  # noqa: SLF001
        self._binding_loader = binding_loader or service_store.list_bindings
        self._connection_loader = connection_loader or connection_store.get_connection
        self._connection_context_builder = connection_context_builder or (
            default_connection_gateway.resolver.execution_context
        )
        self._binding_policy_guard = PolicyGuard()
        self._service_rate_limiter = SlidingWindowRateLimiter(clock)
        self._transport_security = transport_security or _build_transport_security()
        self._json_response = json_response
        self._audit_writer = audit_writer or write_event
        self._clock = clock
        self._entries: dict[tuple[str, int], _SessionEntry] = {}
        self._manager_lock: asyncio.Lock | None = None
        self._run_count = 0

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        if self._run_count == 0:
            self._manager_lock = asyncio.Lock()
        self._run_count += 1
        try:
            yield
        finally:
            self._run_count -= 1
            if self._run_count == 0:
                lock = self._manager_lock
                if lock is not None:
                    async with lock:
                        self._entries.clear()
                self._manager_lock = None

    @property
    def cached_session_keys(self) -> tuple[tuple[str, int], ...]:
        return tuple(self._entries)

    async def list_tools(self, service: ServiceContext) -> list[types.Tool]:
        tools: list[types.Tool] = []
        try:
            bindings = self._bindings_for(service)
        except Exception as exc:
            logger.warning("MCP service binding load failed type=%s", type(exc).__name__)
            return tools
        for binding in bindings:
            if binding.binding_status != "active":
                continue
            try:
                resolved = self._resolve_projection(service, binding)
            except Exception:
                continue
            spec = resolved.projected.spec
            tools.append(
                types.Tool(
                    name=resolved.projected.alias,
                    description=spec.description,
                    inputSchema=dict(spec.input_schema),
                    outputSchema=(
                        None if spec.output_schema is None else dict(spec.output_schema)
                    ),
                )
            )
        return tools

    async def call_tool(
        self,
        service: ServiceContext,
        alias: str,
        args: dict[str, Any],
    ) -> ExecutionResult:
        if not isinstance(args, dict):
            raise TypeError("args must be a dict")
        binding = next(
            (
                item
                for item in self._bindings_for(service)
                if item.binding_status == "active" and item.tool_alias == alias
            ),
            None,
        )
        if binding is None:
            raise ToolDisabledError("tool is unavailable for this service")
        try:
            resolved = self._resolve_projection(service, binding)
        except (ToolDisabledError, InvalidToolPolicyError, RateLimitError):
            raise
        except Exception:
            raise ToolDisabledError("tool is unavailable for this service") from None

        rate_limit = resolved.policy.rate_limit
        if rate_limit is not None and not self._service_rate_limiter.allow(
            service.service_id,
            alias,
            rate_limit,
        ):
            await self._audit_tool(
                service,
                resolved,
                status="denied",
                error_code="rate_limited",
                cost_ms=0,
            )
            raise RateLimitError("service tool rate limit exceeded")

        started_at = self._clock()
        status = "error"
        error_code = "connector_error"
        try:
            execution = self._runtime.execute(
                resolved.context,
                resolved.projected.source_tool_key,
                args,
            )
            if "timeout_ms" in binding.policy:
                result = await asyncio.wait_for(
                    execution,
                    timeout=resolved.policy.timeout_ms / 1000,
                )
            else:
                result = await execution
            status = result.status
            error_code = ""
            return result
        except Exception as exc:
            if isinstance(exc, (ToolDisabledError, RateLimitError)):
                status = "denied"
                error_code = (
                    "tool_disabled" if isinstance(exc, ToolDisabledError) else "rate_limited"
                )
            elif isinstance(exc, asyncio.TimeoutError):
                error_code = "timeout"
            raise
        finally:
            await self._audit_tool(
                service,
                resolved,
                status=status,
                error_code=error_code,
                cost_ms=max(0, int((self._clock() - started_at) * 1000)),
            )

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            return
        if not _is_mount_root(scope):
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return

        started_at = self._clock()
        http_status = 500

        async def tracked_send(message):
            nonlocal http_status
            if message.get("type") == "http.response.start":
                try:
                    http_status = int(message.get("status", 500))
                except (TypeError, ValueError):
                    http_status = 500
            await send(message)

        service: ServiceContext | None = None
        auth = _header(scope, b"authorization")
        service_id = scope.get("path_params", {}).get("service_id")
        if not auth.startswith("Bearer "):
            await self._audit_auth(scope, "auth_missing", None)
            await JSONResponse(
                {"errcode": 401, "errmsg": "缺少 Bearer Token"}, status_code=401
            )(scope, receive, tracked_send)
        else:
            raw_token = auth[len("Bearer ") :].strip()
            if isinstance(service_id, str) and raw_token:
                service = self.resolver.resolve(service_id, raw_token)
            if service is None:
                await self._audit_auth(scope, "auth_invalid", None)
                await JSONResponse(
                    {"errcode": 401, "errmsg": "Token 无效或未绑定服务"},
                    status_code=401,
                )(scope, receive, tracked_send)
            else:
                await self._audit_auth(scope, "auth_ok", service)
                try:
                    manager = await self._manager_for(service)
                    async with manager.run():
                        await manager.handle_request(scope, receive, tracked_send)
                except Exception as exc:
                    logger.warning("MCP service session failed type=%s", type(exc).__name__)
                    await JSONResponse(
                        {"errcode": 503, "errmsg": "MCP 服务暂不可用"},
                        status_code=503,
                    )(scope, receive, tracked_send)

        await self._audit_protocol(
            scope,
            service,
            http_status=http_status,
            cost_ms=max(0, int((self._clock() - started_at) * 1000)),
        )

    async def _manager_for(
        self, service: ServiceContext
    ) -> StreamableHTTPSessionManager:
        key = (service.service_id, service.config_version)
        lock = self._manager_lock
        if lock is None:
            raise RuntimeError("MCP service gateway is not running")
        async with lock:
            entry = self._entries.get(key)
            if entry is None:
                stale = [
                    cached
                    for cached in self._entries
                    if cached[0] == service.service_id and cached != key
                ]
                for cached in stale:
                    self._entries.pop(cached)
                entry = _SessionEntry(server=self._build_server(service))
                self._entries[key] = entry
        return StreamableHTTPSessionManager(
            app=entry.server,
            json_response=self._json_response,
            stateless=True,
            security_settings=self._transport_security,
        )

    def _build_server(self, service: ServiceContext) -> Server:
        server = Server("service-mcp-gateway")

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return await self.list_tools(service)

        @server.call_tool(validate_input=True)
        async def call_tool(name: str, arguments: dict[str, Any]):
            try:
                result = await self.call_tool(service, name, arguments)
                return result.data
            except Exception as exc:
                logger.warning("MCP service tool failed type=%s", type(exc).__name__)
                return types.CallToolResult(
                    content=[
                        types.TextContent(type="text", text="Tool execution failed")
                    ],
                    isError=True,
                )

        return server

    def _bindings_for(self, service: ServiceContext) -> tuple[ServiceToolBinding, ...]:
        if not isinstance(service, ServiceContext):
            raise TypeError("service context is required")
        values = self._binding_loader(service.service_id, service.tenant_id)
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise TypeError("binding loader must return a sequence")
        bindings = tuple(values)
        if any(
            not isinstance(item, ServiceToolBinding)
            or item.service_id != service.service_id
            for item in bindings
        ):
            raise ValueError("service binding snapshot is invalid")
        return bindings

    def _resolve_projection(
        self,
        service: ServiceContext,
        binding: ServiceToolBinding,
    ) -> _ResolvedProjection:
        record = self._connection_loader(binding.connection_id, service.tenant_id)
        if (
            not isinstance(record, ConnectionRecord)
            or record.connection_id != binding.connection_id
            or record.tenant_id != service.tenant_id
            or record.status != "active"
        ):
            raise ToolDisabledError("tool connection is unavailable")
        connection_ctx = ConnectionCtx(
            tenant_id=record.tenant_id,
            connection_id=record.connection_id,
            connector_key=record.connector_key,
            data_mode=record.data_mode,
            public_config=record.public_config,
            config_version=record.config_version,
        )
        context = self._connection_context_builder(connection_ctx)
        if (
            not isinstance(context, ConnectionContext)
            or context.connection_id != record.connection_id
            or context.tenant_id != service.tenant_id
            or context.connection.status != "active"
        ):
            raise ToolDisabledError("tool connection context is unavailable")
        spec = self._runtime.require_enabled_tool(context, binding.source_tool_key)
        binding_policy = ToolPolicy(
            connection_id=binding.connection_id,
            tool_name=binding.source_tool_key,
            enabled=True,
            policy=dict(binding.policy),
        )
        resolved_policy = self._binding_policy_guard.assert_allowed(spec, binding_policy)
        return _ResolvedProjection(
            projected=ProjectedTool(
                alias=binding.tool_alias,
                connection_id=binding.connection_id,
                source_tool_key=binding.source_tool_key,
                spec=spec,
            ),
            binding=binding,
            context=context,
            policy=resolved_policy,
        )

    async def _audit(self, event: McpLogEvent) -> None:
        try:
            result = self._audit_writer(event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("MCP service audit failed type=%s", type(exc).__name__)

    async def _audit_tool(
        self,
        service: ServiceContext,
        resolved: _ResolvedProjection,
        *,
        status: str,
        error_code: str,
        cost_ms: int,
    ) -> None:
        metadata = current_request_metadata()
        await self._audit(
            McpLogEvent(
                tenant_id=service.tenant_id,
                service_id=service.service_id,
                tool_alias=resolved.projected.alias,
                connection_id=resolved.context.connection_id,
                connector_key=resolved.context.connector_key,
                tool_key=resolved.projected.source_tool_key,
                category="tool",
                event_name=resolved.projected.alias,
                target=resolved.context.connection_id,
                params_summary="omitted",
                result_status=status,
                error_code=error_code,
                cost_ms=cost_ms,
                request_id=metadata.get("request_id", ""),
                client_ip=metadata.get("client_ip", ""),
                http_method=metadata.get("http_method", ""),
            )
        )

    async def _audit_auth(
        self,
        scope: Mapping[str, Any],
        event_name: str,
        service: ServiceContext | None,
    ) -> None:
        await self._audit(
            McpLogEvent(
                tenant_id="" if service is None else service.tenant_id,
                service_id=None if service is None else service.service_id,
                category="auth",
                event_name=event_name,
                target="" if service is None else service.service_id,
                result_status="ok" if event_name == "auth_ok" else "denied",
                error_code="" if event_name == "auth_ok" else "401",
                request_id=request_id_from_scope(dict(scope)),
                client_ip=client_ip_from_scope(dict(scope)),
                http_method=safe_summary(scope.get("method", ""), 16),
                http_status=0 if event_name == "auth_ok" else 401,
            )
        )

    async def _audit_protocol(
        self,
        scope: Mapping[str, Any],
        service: ServiceContext | None,
        *,
        http_status: int,
        cost_ms: int,
    ) -> None:
        method = str(scope.get("method", "")).upper()
        event_name = {
            "GET": "mcp_http_get",
            "DELETE": "mcp_http_delete",
        }.get(method, "mcp_http_request")
        await self._audit(
            McpLogEvent(
                tenant_id="" if service is None else service.tenant_id,
                service_id=None if service is None else service.service_id,
                category="protocol",
                event_name=event_name,
                target="" if service is None else service.service_id,
                result_status=(
                    "ok"
                    if http_status < 400
                    else "denied"
                    if http_status in (401, 403)
                    else "error"
                ),
                error_code=str(http_status) if http_status >= 400 else "",
                cost_ms=cost_ms,
                request_id=request_id_from_scope(dict(scope)),
                client_ip=client_ip_from_scope(dict(scope)),
                http_method=safe_summary(method, 16),
                http_status=http_status,
            )
        )


def _header(scope: Mapping[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1", "replace")
    return ""
