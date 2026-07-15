"""Typed, connection-scoped contracts for trusted connectors.

This module deliberately contains declarations and lookup helpers only.  It
does not discover packages, execute user-provided code, or touch credentials.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

from app.connections.models import ConnectionRecord


OperationKind = Literal["read", "write"]
ExecutionStatus = Literal["ok", "partial", "error"]
DataMode = Literal["direct", "stored", "hybrid"]


class ToolDisabledError(PermissionError):
    """Raised when a connection has disabled a declared tool."""


class ConnectionUnavailableError(PermissionError):
    """Raised when a connection is not active for tool enumeration or execution."""


class WritePolicyError(PermissionError):
    """Raised when a write lacks an explicit per-connection allowance."""


@dataclass(frozen=True)
class ToolSpec:
    tool_key: str
    mcp_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    operation_kind: OperationKind
    default_timeout_ms: int
    cache_ttl_seconds: int | None


@dataclass(frozen=True)
class ConnectorSpec:
    """The explicit manifest for one trusted connector implementation."""

    connector_key: str
    tools: tuple[ToolSpec, ...]
    supports_sync: bool = False
    version: str = ""
    config_schema: dict[str, Any] = field(default_factory=dict)
    credential_schema: dict[str, Any] = field(default_factory=dict)
    supports_data_modes: tuple[DataMode, ...] = ("direct", "stored", "hybrid")

    def __post_init__(self) -> None:
        tools = tuple(self.tools)
        tool_keys = [tool.tool_key for tool in tools]
        mcp_names = [tool.mcp_name for tool in tools]
        if len(tool_keys) != len(set(tool_keys)) or len(mcp_names) != len(set(mcp_names)):
            raise ValueError("duplicate tool_key or mcp_name in ConnectorSpec")
        object.__setattr__(self, "tools", tools)

    def tool(self, tool_key: str) -> ToolSpec:
        """Resolve either the stable internal key or its MCP-facing name."""
        for tool in self.tools:
            if tool.tool_key == tool_key or tool.mcp_name == tool_key:
                return tool
        raise KeyError(f"unknown tool_key: {tool_key}")


@dataclass(frozen=True)
class ConnectionContext:
    """Safe execution context; credential values are never repr'd by default."""

    connection: ConnectionRecord
    credentials: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    request_metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "credentials", MappingProxyType(dict(self.credentials)))
        object.__setattr__(
            self,
            "request_metadata",
            MappingProxyType(dict(self.request_metadata)),
        )

    @property
    def tenant_id(self) -> str:
        return self.connection.tenant_id

    @property
    def connection_id(self) -> str:
        return self.connection.connection_id

    @property
    def connector_key(self) -> str:
        return self.connection.connector_key

    @property
    def data_mode(self) -> DataMode:
        return self.connection.data_mode

    @property
    def public_config(self) -> Mapping[str, Any]:
        return MappingProxyType(dict(self.connection.public_config))


@dataclass(frozen=True)
class ExecutionResult:
    data: dict[str, Any]
    status: ExecutionStatus

    @classmethod
    def ok(cls, data: dict[str, Any]) -> "ExecutionResult":
        return cls(data=data, status="ok")


@dataclass(frozen=True)
class SyncResult:
    connection_id: str
    resource_key: str
    data: dict[str, Any] = field(default_factory=dict)
    status: ExecutionStatus = "ok"

    @classmethod
    def ok(
        cls,
        connection_id: str,
        resource_key: str,
        data: dict[str, Any] | None = None,
    ) -> "SyncResult":
        return cls(
            connection_id=connection_id,
            resource_key=resource_key,
            data={} if data is None else data,
            status="ok",
        )


@runtime_checkable
class Connector(Protocol):
    def spec(self) -> ConnectorSpec: ...

    async def execute(
        self,
        context: ConnectionContext,
        tool_key: str,
        args: dict[str, Any],
    ) -> ExecutionResult: ...

    async def sync(
        self,
        context: ConnectionContext,
        resource_key: str,
    ) -> SyncResult: ...
