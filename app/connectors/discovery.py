"""Allowlist-first discovery for reviewed, preinstalled connector packages."""

from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import entry_points
import re

from app.config import get_settings
from app.connections.models import ConnectionRecord

from .contracts import Connector
from .registry import validate_connector_manifest


ENTRY_POINT_GROUP = "wbsysc.connectors"
_NORMALIZED_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class ConnectorDiscoveryError(RuntimeError):
    """A trusted package could not provide a safe, compatible connector."""


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


def discover_trusted_connectors() -> list[Connector]:
    """Load only reviewed entry points named in the explicit allowlist."""
    allowed = parse_connector_allowlist(get_settings().connector_allowlist)
    candidates = [
        entry_point
        for entry_point in entry_points(group=ENTRY_POINT_GROUP)
        if normalize_connector_name(entry_point.name) in allowed
    ]

    normalized_names = [normalize_connector_name(ep.name) for ep in candidates]
    if len(normalized_names) != len(set(normalized_names)):
        raise ConnectorDiscoveryError("duplicate trusted connector entry point")

    connectors: list[Connector] = []
    connector_keys: set[str] = set()
    tool_identities: set[str] = set()
    for entry_point in candidates:
        safe_name = normalize_connector_name(entry_point.name)
        try:
            factory = entry_point.load()
            connector = factory()
        except Exception:
            raise ConnectorDiscoveryError(
                f"trusted connector '{safe_name}' could not be loaded"
            ) from None

        if not isinstance(connector, Connector):
            raise ConnectorDiscoveryError(
                f"trusted connector '{safe_name}' contract is incompatible"
            )

        try:
            manifest = connector.spec()
        except Exception:
            raise ConnectorDiscoveryError(
                f"trusted connector '{safe_name}' manifest is invalid"
            ) from None

        try:
            spec = validate_connector_manifest(
                manifest,
                expected_connector_key=_manifest_key_for_entry_point(entry_point.name),
            )
        except Exception as exc:
            if exc.args == ("connector manifest version is invalid",):
                reason = "manifest version is invalid"
            elif exc.args == ("connector entry-point identity mismatch",):
                reason = "entry-point identity is invalid"
            else:
                reason = "manifest is invalid"
            raise ConnectorDiscoveryError(
                f"trusted connector '{safe_name}' {reason}"
            ) from None

        if spec.connector_key in connector_keys:
            raise ConnectorDiscoveryError("duplicate trusted connector identity")
        package_tool_ids = {
            identifier
            for tool in spec.tools
            for identifier in (tool.tool_key, tool.mcp_name)
        }
        if package_tool_ids & tool_identities:
            raise ConnectorDiscoveryError("duplicate trusted connector tool identity")
        connector_keys.add(spec.connector_key)
        tool_identities.update(package_tool_ids)
        connectors.append(connector)
    return connectors


def validate_active_connector_dependencies(
    connections: Iterable[ConnectionRecord],
    connectors: Iterable[Connector],
    *,
    allowlist: str | None = None,
) -> None:
    """Fail only when an active row needs a missing allowlisted package."""
    allowed = parse_connector_allowlist(
        get_settings().connector_allowlist if allowlist is None else allowlist
    )
    available = {
        normalize_connector_name(connector.spec().connector_key)
        for connector in connectors
    }
    for connection in connections:
        connector_name = normalize_connector_name(connection.connector_key)
        if (
            connection.status == "active"
            and connector_name in allowed
            and connector_name not in available
        ):
            raise ConnectorDiscoveryError(
                f"active connection requires unavailable connector '{connector_name}'"
            )
