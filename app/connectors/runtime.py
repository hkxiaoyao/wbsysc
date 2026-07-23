"""Connection-scoped policy enforcement and connector execution runtime."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.connections.models import ToolPolicy

from .contracts import (
    ConnectionConnectorProvider,
    ConnectionContext,
    ConnectionUnavailableError,
    Connector,
    ExecutionResult,
    ToolDisabledError,
    ToolSpec,
    WritePolicyError,
)
from .registry import ConnectorRegistry


MAX_TIMEOUT_MS = 300_000
_CONNECTOR_EXECUTION_ERROR_CODE = "connector_error"


class RateLimitError(PermissionError):
    """Raised when a connection has exhausted a tool's local rate budget."""


class InvalidToolPolicyError(PermissionError):
    """Raised when an explicitly configured policy cannot be safely applied."""


class UnsupportedDataModeError(ValueError):
    """Raised when a connection mode is absent from the connector manifest."""


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _public_tool_copy(tool: ToolSpec) -> ToolSpec:
    """Return JSON-serializable schemas without exposing the registry snapshot."""
    return ToolSpec(
        tool_key=tool.tool_key,
        mcp_name=tool.mcp_name,
        description=tool.description,
        input_schema=_thaw_json(tool.input_schema),
        output_schema=(
            None if tool.output_schema is None else _thaw_json(tool.output_schema)
        ),
        operation_kind=tool.operation_kind,
        default_timeout_ms=tool.default_timeout_ms,
        cache_ttl_seconds=tool.cache_ttl_seconds,
    )


@dataclass(frozen=True)
class RateLimit:
    limit: int
    window_seconds: float


@dataclass(frozen=True)
class ResolvedToolPolicy:
    enabled: bool
    allow_write: bool
    timeout_ms: int
    rate_limit: RateLimit | None


@dataclass(frozen=True)
class ConnectorAuditEvent:
    """A redaction-only audit handoff; it intentionally has no payload fields."""

    tenant_id: str
    connection_id: str
    connector_key: str
    tool_key: str
    status: Literal["ok", "partial", "error", "denied"]
    cost_ms: int
    args_summary: str = "omitted"
    result_summary: str = "omitted"
    error_code: str = ""
    error_summary: str = ""


class ToolPolicyStore(Protocol):
    def get(self, connection_id: str, tool_key: str) -> ToolPolicy | None: ...


def _optional_policy_bool(values: Mapping[str, Any], key: str) -> bool | None:
    if key not in values:
        return None
    value = values[key]
    if not isinstance(value, bool):
        raise InvalidToolPolicyError("invalid boolean policy control")
    return value


class PolicyGuard:
    """Converts persisted policy JSON into a narrow, fail-safe execution policy."""

    def resolve(self, tool: ToolSpec, policy: ToolPolicy | None) -> ResolvedToolPolicy:
        values: Mapping[str, Any]
        if policy is None:
            enabled = True
            values = {}
        else:
            enabled = policy.enabled is True
            if not isinstance(policy.policy, Mapping):
                raise InvalidToolPolicyError("policy must be a mapping")
            values = policy.policy

        allow_write = _optional_policy_bool(values, "allow_write")
        read_only = _optional_policy_bool(values, "read_only")
        readonly = _optional_policy_bool(values, "readonly")

        return ResolvedToolPolicy(
            enabled=enabled,
            allow_write=(
                allow_write is True and read_only is not True and readonly is not True
            ),
            timeout_ms=normalize_timeout_ms(
                values.get("timeout_ms"),
                tool.default_timeout_ms,
            ),
            rate_limit=normalize_rate_limit(values),
        )

    def assert_allowed(
        self,
        tool: ToolSpec,
        policy: ToolPolicy | None,
    ) -> ResolvedToolPolicy:
        resolved = self.resolve(tool, policy)
        if not resolved.enabled:
            raise ToolDisabledError("tool is disabled for this connection")
        if tool.operation_kind == "write" and not resolved.allow_write:
            raise WritePolicyError("write tools require explicit connection policy")
        return resolved


def normalize_timeout_ms(value: Any, default_timeout_ms: int) -> int:
    """Use a declared positive default for malformed policy values, with a cap."""
    fallback = (
        default_timeout_ms
        if isinstance(default_timeout_ms, int)
        and not isinstance(default_timeout_ms, bool)
        and default_timeout_ms > 0
        else MAX_TIMEOUT_MS
    )
    timeout_ms = (
        value if isinstance(value, int) and not isinstance(value, bool) else fallback
    )
    if timeout_ms <= 0:
        timeout_ms = fallback
    return min(timeout_ms, MAX_TIMEOUT_MS)


def normalize_rate_limit(values: Mapping[str, Any]) -> RateLimit | None:
    """Accept only bounded, declarative rate-limit settings from policy JSON."""
    has_nested_limit = "rate_limit" in values
    has_per_minute_limit = "rate_limit_per_minute" in values
    if not has_nested_limit and not has_per_minute_limit:
        return None

    raw_limit = values.get("rate_limit")
    if has_nested_limit and isinstance(raw_limit, Mapping):
        limit = raw_limit.get("limit", raw_limit.get("max_calls"))
        window_seconds = raw_limit.get("window_seconds")
        if window_seconds is None and isinstance(
            raw_limit.get("window_ms"), (int, float)
        ):
            window_seconds = raw_limit["window_ms"] / 1000
    elif not has_nested_limit and has_per_minute_limit:
        limit = values.get("rate_limit_per_minute")
        window_seconds = 60
    else:
        raise InvalidToolPolicyError("invalid rate limit configuration")

    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= 0
        or not isinstance(window_seconds, (int, float))
        or isinstance(window_seconds, bool)
        or window_seconds <= 0
    ):
        raise InvalidToolPolicyError("invalid rate limit configuration")
    return RateLimit(limit=limit, window_seconds=float(window_seconds))


class SlidingWindowRateLimiter:
    """In-process fixed-key limiter used until the shared operations service exists."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, connection_id: str, tool_key: str, rate_limit: RateLimit) -> bool:
        now = self._clock()
        key = (connection_id, tool_key)
        cutoff = now - rate_limit.window_seconds
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= rate_limit.limit:
                return False
            bucket.append(now)
            return True


ModeExecutor = Callable[
    [ConnectionContext, Connector, ToolSpec, dict[str, Any]],
    Awaitable[ExecutionResult],
]


class ExecutionPlanner:
    """Routes the connection's declared data mode to an explicit executor hook."""

    def __init__(
        self,
        *,
        direct_executor: ModeExecutor | None = None,
        stored_executor: ModeExecutor | None = None,
        hybrid_executor: ModeExecutor | None = None,
    ) -> None:
        self._executors = {
            "direct": direct_executor,
            "stored": stored_executor,
            "hybrid": hybrid_executor,
        }

    async def execute(
        self,
        context: ConnectionContext,
        connector: Connector,
        tool: ToolSpec,
        args: dict[str, Any],
    ) -> ExecutionResult:
        if context.data_mode not in self._executors:
            raise UnsupportedDataModeError("unsupported connection data mode")
        executor = self._executors[context.data_mode]
        if executor is None:
            return await connector.execute(context, tool.tool_key, args)
        result = executor(context, connector, tool, args)
        if not inspect.isawaitable(result):
            raise TypeError(
                "data-mode executor must return an awaitable ExecutionResult"
            )
        return await result


AuditSink = Callable[[ConnectorAuditEvent], Any]


class ConnectionConnectorResolver:
    """Resolve static registry snapshots or the reserved dynamic provider."""

    def __init__(
        self,
        registry: ConnectorRegistry,
        *,
        declarative_provider: ConnectionConnectorProvider | None = None,
    ) -> None:
        self._registry = registry
        self._declarative_provider = declarative_provider

    def spec_for(self, context: ConnectionContext):
        provider = self._provider_for(context)
        if provider is not None:
            return provider.spec_for(context)
        spec = self._registry.validated_spec(context.connector_key)
        if spec is None:
            raise KeyError(f"unknown connector_key: {context.connector_key}")
        return spec

    @asynccontextmanager
    async def connect(self, context: ConnectionContext):
        provider = self._provider_for(context)
        if provider is not None:
            async with provider.connect(context) as connector:
                yield connector
            return
        yield self._registry.get(context.connector_key)

    def _provider_for(
        self, context: ConnectionContext
    ) -> ConnectionConnectorProvider | None:
        if context.connector_key != "http_declarative":
            return None
        provider = self._declarative_provider
        if provider is None or provider.connector_key != context.connector_key:
            raise KeyError("declarative connector provider unavailable")
        return provider


class ConnectorRuntime:
    def __init__(
        self,
        registry: ConnectorRegistry,
        *,
        policy_store: ToolPolicyStore
        | Mapping[tuple[str, str], ToolPolicy]
        | None = None,
        policy_guard: PolicyGuard | None = None,
        planner: ExecutionPlanner | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        audit_sink: AuditSink | None = None,
        clock: Callable[[], float] = time.monotonic,
        connector_resolver: ConnectionConnectorResolver | None = None,
    ) -> None:
        self._registry = registry
        self._policy_store = policy_store
        self._policy_guard = policy_guard or PolicyGuard()
        self._planner = planner or ExecutionPlanner()
        self._rate_limiter = rate_limiter or SlidingWindowRateLimiter(clock)
        self._audit_sink = audit_sink
        self._clock = clock
        self._connector_resolver = connector_resolver or ConnectionConnectorResolver(
            registry
        )

    def list_enabled_tools(self, context: ConnectionContext) -> tuple[ToolSpec, ...]:
        if context.connection.status != "active":
            return ()
        spec = self._connector_resolver.spec_for(context)
        enabled_tools = []
        for tool in spec.tools:
            policy = self._policy_for(context, tool)
            try:
                self._policy_guard.assert_allowed(tool, policy)
            except (ToolDisabledError, WritePolicyError, InvalidToolPolicyError):
                continue
            enabled_tools.append(_public_tool_copy(tool))
        return tuple(enabled_tools)

    def require_enabled_tool(
        self,
        context: ConnectionContext,
        source_tool_key: str,
    ) -> ToolSpec:
        """Resolve one stable source key after connection/tool policy checks."""
        if context.connection.status != "active":
            raise ConnectionUnavailableError("connection is unavailable")
        spec = self._connector_resolver.spec_for(context)
        tool = next(
            (item for item in spec.tools if item.tool_key == source_tool_key),
            None,
        )
        if tool is None:
            raise ToolDisabledError("tool is unavailable for this connection")
        policy = self._policy_for(context, tool)
        self._policy_guard.assert_allowed(tool, policy)
        return _public_tool_copy(tool)

    async def execute(
        self,
        context: ConnectionContext,
        tool_key: str,
        args: dict[str, Any],
    ) -> ExecutionResult:
        if not isinstance(args, dict):
            raise TypeError("args must be a dict")
        if context.connection.status != "active":
            raise ConnectionUnavailableError("connection is unavailable")

        spec = self._connector_resolver.spec_for(context)
        tool = spec.tool(tool_key)
        policy = self._policy_for(context, tool)
        try:
            resolved_policy = self._policy_guard.assert_allowed(tool, policy)
        except ToolDisabledError:
            await self._audit(
                context, tool, "denied", 0, "tool_disabled", "tool disabled"
            )
            raise
        except WritePolicyError:
            await self._audit(
                context,
                tool,
                "denied",
                0,
                "write_policy",
                "write policy denied",
            )
            raise
        except InvalidToolPolicyError:
            await self._audit(
                context,
                tool,
                "denied",
                0,
                "invalid_policy",
                "tool policy is invalid",
            )
            raise

        if resolved_policy.rate_limit is not None and not self._rate_limiter.allow(
            context.connection.connection_id,
            tool.tool_key,
            resolved_policy.rate_limit,
        ):
            await self._audit(
                context,
                tool,
                "denied",
                0,
                "rate_limited",
                "rate limit exceeded",
            )
            raise RateLimitError("rate limit exceeded")

        started_at = self._clock()
        try:
            result = await asyncio.wait_for(
                self._execute_with_data_mode(context, tool, args),
                timeout=resolved_policy.timeout_ms / 1000,
            )
            if not isinstance(result, ExecutionResult):
                raise TypeError("connector execution must return an ExecutionResult")
        except asyncio.TimeoutError:
            await self._audit(
                context,
                tool,
                "error",
                self._cost_ms(started_at),
                "timeout",
                "execution timed out",
            )
            raise
        except Exception:
            await self._audit(
                context,
                tool,
                "error",
                self._cost_ms(started_at),
                _CONNECTOR_EXECUTION_ERROR_CODE,
                "connector execution failed",
            )
            raise

        status = (
            result.status if result.status in {"ok", "partial", "error"} else "error"
        )
        await self._audit(context, tool, status, self._cost_ms(started_at))
        return result

    async def _execute_with_data_mode(
        self,
        context: ConnectionContext,
        tool: ToolSpec,
        args: dict[str, Any],
    ) -> ExecutionResult:
        spec = self._connector_resolver.spec_for(context)
        if context.data_mode not in spec.supports_data_modes:
            raise UnsupportedDataModeError("connection data mode is not supported")
        async with self._connector_resolver.connect(context) as connector:
            return await self._planner.execute(context, connector, tool, args)

    def _policy_for(
        self,
        context: ConnectionContext,
        tool: ToolSpec,
    ) -> ToolPolicy | None:
        store = self._policy_store
        if store is None:
            return None
        if isinstance(store, Mapping):
            policy = store.get((context.connection.connection_id, tool.tool_key))
            if policy is None and tool.mcp_name != tool.tool_key:
                policy = store.get((context.connection.connection_id, tool.mcp_name))
        else:
            policy = store.get(context.connection.connection_id, tool.tool_key)
            if policy is None and tool.mcp_name != tool.tool_key:
                policy = store.get(context.connection.connection_id, tool.mcp_name)
        accepted_tool_names = {tool.tool_key, tool.mcp_name}
        if policy is not None and (
            policy.connection_id != context.connection.connection_id
            or policy.tool_name not in accepted_tool_names
        ):
            return ToolPolicy(
                connection_id=context.connection.connection_id,
                tool_name=tool.tool_key,
                enabled=False,
                policy={},
            )
        return policy

    async def _audit(
        self,
        context: ConnectionContext,
        tool: ToolSpec,
        status: Literal["ok", "partial", "error", "denied"],
        cost_ms: int,
        error_code: str = "",
        error_summary: str = "",
    ) -> None:
        if self._audit_sink is None:
            return
        event = ConnectorAuditEvent(
            tenant_id=context.connection.tenant_id,
            connection_id=context.connection.connection_id,
            connector_key=context.connection.connector_key,
            tool_key=tool.tool_key,
            status=status,
            cost_ms=max(0, int(cost_ms)),
            error_code=error_code,
            error_summary=error_summary,
        )
        try:
            outcome = self._audit_sink(event)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            # Audit handoff must never alter connector behavior, and logging an
            # arbitrary sink exception could disclose a credential or payload.
            return

    def _cost_ms(self, started_at: float) -> int:
        return max(0, int((self._clock() - started_at) * 1000))
