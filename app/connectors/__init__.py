"""Trusted connector contracts and the shared connection runtime."""

from .contracts import (
    ConnectionContext,
    ConnectionUnavailableError,
    Connector,
    ConnectorSpec,
    ExecutionResult,
    SyncResult,
    ToolDisabledError,
    ToolSpec,
    WritePolicyError,
)
from .registry import ConnectorRegistry
from .runtime import (
    ConnectorRuntime,
    InvalidToolPolicyError,
    RateLimitError,
    UnsupportedDataModeError,
)

__all__ = (
    "ConnectionContext",
    "ConnectionUnavailableError",
    "Connector",
    "ConnectorRegistry",
    "ConnectorRuntime",
    "ConnectorSpec",
    "ExecutionResult",
    "InvalidToolPolicyError",
    "RateLimitError",
    "SyncResult",
    "ToolDisabledError",
    "ToolSpec",
    "UnsupportedDataModeError",
    "WritePolicyError",
)
