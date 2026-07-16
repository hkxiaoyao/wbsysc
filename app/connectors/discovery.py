"""Allowlist-first discovery for reviewed, preinstalled connector packages."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field as dataclass_field
from importlib.metadata import entry_points
import re

from app.config import get_settings
from app.connections.models import ConnectionRecord

from .contracts import Connector, ConnectorSpec
from .registry import ConnectorRegistry, validate_connector_manifest


ENTRY_POINT_GROUP = "wbsysc.connectors"
_NORMALIZED_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class ConnectorDiscoveryError(RuntimeError):
    """A trusted package could not provide a safe, compatible connector."""


@dataclass(frozen=True)
class ValidatedConnector:
    """A connector paired with registration-time validated immutable metadata."""

    connector: Connector = dataclass_field(repr=False)
    spec: ConnectorSpec = dataclass_field(repr=False)


@dataclass(frozen=True)
class ConnectorDiscoveryFailure:
    """Safe package failure metadata; no exception or configuration payload."""

    connector_key: str
    reason: str


@dataclass(frozen=True)
class ConnectorDiscoveryResult:
    connectors: tuple[ValidatedConnector, ...]
    failures: tuple[ConnectorDiscoveryFailure, ...]


def normalize_connector_name(value: str) -> str:
    """Apply Python distribution-name normalization for exact comparisons."""
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"[-_.]+", "-", value.strip()).lower()
    return normalized if _NORMALIZED_NAME_PATTERN.fullmatch(normalized) else ""


def parse_connector_allowlist(value: str) -> frozenset[str]:
    """Return normalized, non-empty exact entry-point names."""
    if not isinstance(value, str):
        return frozenset()
    return frozenset(
        normalized
        for item in value.split(",")
        if (normalized := normalize_connector_name(item))
    )


def _manifest_key_for_entry_point(entry_point_name: str) -> str:
    return normalize_connector_name(entry_point_name).replace("-", "_")


def discover_connector_packages() -> ConnectorDiscoveryResult:
    """Discover packages while retaining only safe, deterministic failures."""
    allowed = parse_connector_allowlist(get_settings().connector_allowlist)
    candidates = sorted(
        (
            entry_point
            for entry_point in entry_points(group=ENTRY_POINT_GROUP)
            if normalize_connector_name(entry_point.name) in allowed
        ),
        key=lambda entry_point: normalize_connector_name(entry_point.name),
    )

    normalized_names = [normalize_connector_name(ep.name) for ep in candidates]
    if len(normalized_names) != len(set(normalized_names)):
        return ConnectorDiscoveryResult(
            (), (ConnectorDiscoveryFailure("", "duplicate"),)
        )

    connectors: list[ValidatedConnector] = []
    failures: list[ConnectorDiscoveryFailure] = []
    connector_keys: set[str] = set()
    tool_identities: set[str] = set()
    for entry_point in candidates:
        expected_key = _manifest_key_for_entry_point(entry_point.name)
        try:
            factory = entry_point.load()
            connector = factory()
        except Exception:
            failures.append(ConnectorDiscoveryFailure(expected_key, "load"))
            continue

        if not isinstance(connector, Connector):
            failures.append(ConnectorDiscoveryFailure(expected_key, "contract"))
            continue

        try:
            manifest = connector.spec()
        except Exception:
            failures.append(ConnectorDiscoveryFailure(expected_key, "manifest"))
            continue

        try:
            spec = validate_connector_manifest(
                manifest,
                expected_connector_key=expected_key,
            )
        except Exception as exc:
            if exc.args == ("connector manifest version is invalid",):
                reason = "version"
            elif exc.args == ("connector entry-point identity mismatch",):
                reason = "identity"
            else:
                reason = "manifest"
            failures.append(ConnectorDiscoveryFailure(expected_key, reason))
            continue

        has_identity_collision = (
            spec.connector_key in connector_keys
            or spec.connector_key in tool_identities
        )
        package_tool_ids = {
            identifier
            for tool in spec.tools
            for identifier in (tool.tool_key, tool.mcp_name)
        }
        has_identity_collision = has_identity_collision or (
            spec.connector_key in package_tool_ids
            or package_tool_ids & tool_identities
            or package_tool_ids & connector_keys
        )
        if has_identity_collision:
            failures.append(ConnectorDiscoveryFailure(expected_key, "duplicate"))
        connector_keys.add(spec.connector_key)
        tool_identities.update(package_tool_ids)
        connectors.append(ValidatedConnector(connector, spec))
    return ConnectorDiscoveryResult(tuple(connectors), tuple(failures))


def _strict_failure_message(failure: ConnectorDiscoveryFailure) -> str:
    safe_key = failure.connector_key
    if failure.reason == "version":
        return f"trusted connector '{safe_key}' manifest version is invalid"
    if failure.reason == "identity":
        return f"trusted connector '{safe_key}' entry-point identity is invalid"
    if failure.reason == "contract":
        return f"trusted connector '{safe_key}' contract is incompatible"
    if failure.reason == "duplicate":
        return "duplicate trusted connector identity"
    if failure.reason == "load":
        return f"trusted connector '{safe_key}' could not be loaded"
    return f"trusted connector '{safe_key}' manifest is invalid"


def discover_trusted_connectors() -> list[Connector]:
    """Strict public discovery API retained for explicit callers and tests."""
    result = discover_connector_packages()
    if result.failures:
        raise ConnectorDiscoveryError(_strict_failure_message(result.failures[0]))
    return [item.connector for item in result.connectors]


def validate_active_connector_dependencies(
    connections: Iterable[ConnectionRecord],
    connectors: Iterable[Connector] | ConnectorDiscoveryResult,
    *,
    allowlist: str | None = None,
) -> None:
    """Fail only when an active row needs a missing allowlisted package."""
    allowed = parse_connector_allowlist(
        get_settings().connector_allowlist if allowlist is None else allowlist
    )
    required = {
        normalize_connector_name(connection.connector_key)
        for connection in connections
        if connection.status == "active"
        and normalize_connector_name(connection.connector_key) in allowed
    }
    if not required:
        return

    if isinstance(connectors, ConnectorDiscoveryResult):
        available = {
            normalize_connector_name(item.spec.connector_key)
            for item in connectors.connectors
        }
    else:
        available = set()
        try:
            for connector in connectors:
                available.add(normalize_connector_name(connector.spec().connector_key))
        except Exception:
            raise ConnectorDiscoveryError(
                "active connection requires unavailable trusted connector"
            ) from None

    for connector_name in sorted(required):
        if connector_name not in available:
            raise ConnectorDiscoveryError(
                f"active connection requires unavailable connector '{connector_name}'"
            )


def register_discovered_connectors(
    registry: ConnectorRegistry,
    result: ConnectorDiscoveryResult,
) -> None:
    """Idempotently register validated package metadata into one explicit registry."""
    registry.replace_discovered_connectors(
        (item.connector, item.spec) for item in result.connectors
    )
