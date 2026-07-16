"""Explicit registry for trusted, already-imported connector instances."""

from __future__ import annotations

from collections.abc import Iterable
import re
from types import MappingProxyType

from .contracts import Connector, ConnectorSpec


_CONNECTOR_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MANIFEST_VERSION_PATTERN = re.compile(
    r"^[0-9]+(?:\.[0-9]+){0,2}(?:[-+][A-Za-z0-9][A-Za-z0-9.-]*)?$"
)


def validate_connector_manifest(
    spec: object,
    *,
    expected_connector_key: str | None = None,
) -> ConnectorSpec:
    """Validate the data-only contract exposed by a package connector."""
    if not isinstance(spec, ConnectorSpec):
        raise ValueError("invalid connector manifest")
    if not _CONNECTOR_KEY_PATTERN.fullmatch(spec.connector_key):
        raise ValueError("invalid connector manifest")
    if (
        expected_connector_key is not None
        and spec.connector_key != expected_connector_key
    ):
        raise ValueError("connector entry-point identity mismatch")
    if not isinstance(spec.version, str) or not _MANIFEST_VERSION_PATTERN.fullmatch(
        spec.version.strip()
    ):
        raise ValueError("connector manifest version is invalid")

    identifiers: set[str] = set()
    for tool in spec.tools:
        if not isinstance(tool.tool_key, str) or not tool.tool_key:
            raise ValueError("invalid connector manifest")
        if not isinstance(tool.mcp_name, str) or not tool.mcp_name:
            raise ValueError("invalid connector manifest")
        for identifier in {tool.tool_key, tool.mcp_name}:
            if identifier in identifiers:
                raise ValueError("invalid connector manifest")
            identifiers.add(identifier)
    return spec


class ConnectorRegistry:
    """Stores only connector instances explicitly supplied by application code.

    Package entry-point discovery remains a separate, explicitly invoked
    responsibility.
    """

    def __init__(self, connectors: Iterable[Connector] = ()) -> None:
        self._connectors: dict[str, Connector] = {}
        for connector in connectors:
            self.register(connector)

    def register(self, connector: Connector) -> None:
        spec = connector.spec()
        connector_key = spec.connector_key
        if not isinstance(connector_key, str) or not connector_key:
            raise ValueError("connector_key is required")
        if connector_key in self._connectors:
            raise ValueError(f"duplicate connector_key: {connector_key}")
        self._connectors[connector_key] = connector

    def get(self, connector_key: str) -> Connector:
        try:
            return self._connectors[connector_key]
        except KeyError as exc:
            raise KeyError(f"unknown connector_key: {connector_key}") from exc

    def connectors(self) -> tuple[Connector, ...]:
        return tuple(self._connectors.values())

    @property
    def registered(self):
        """Read-only keyed view for administration and diagnostics."""
        return MappingProxyType(dict(self._connectors))
