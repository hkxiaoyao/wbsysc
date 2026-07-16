"""Explicit registry for trusted, already-imported connector instances."""

from __future__ import annotations

from collections.abc import Iterable
import re
from types import MappingProxyType

from .contracts import Connector, ConnectorSpec, ToolSpec


_CONNECTOR_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MANIFEST_VERSION_PATTERN = re.compile(
    r"^[0-9]+(?:\.[0-9]+){0,2}(?:[-+][A-Za-z0-9][A-Za-z0-9.-]*)?$"
)
_TOOL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


def validate_connector_manifest(
    spec: object,
    *,
    expected_connector_key: str | None = None,
    require_version: bool = True,
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
    if not isinstance(spec.version, str):
        raise ValueError("connector manifest version is invalid")
    if require_version and (
        spec.version != spec.version.strip()
        or not _MANIFEST_VERSION_PATTERN.fullmatch(spec.version)
    ):
        raise ValueError("connector manifest version is invalid")
    if not isinstance(spec.config_schema, dict) or not isinstance(
        spec.credential_schema, dict
    ):
        raise ValueError("invalid connector manifest")
    if not isinstance(spec.supports_sync, bool):
        raise ValueError("invalid connector manifest")
    if (
        not isinstance(spec.supports_data_modes, tuple)
        or not spec.supports_data_modes
        or len(spec.supports_data_modes) != len(set(spec.supports_data_modes))
        or any(
            mode not in {"direct", "stored", "hybrid"}
            for mode in spec.supports_data_modes
        )
    ):
        raise ValueError("invalid connector manifest")

    identifiers: set[str] = set()
    for tool in spec.tools:
        if not isinstance(tool, ToolSpec):
            raise ValueError("invalid connector tool")
        if not isinstance(tool.tool_key, str) or not _TOOL_IDENTIFIER_PATTERN.fullmatch(
            tool.tool_key
        ):
            raise ValueError("invalid connector tool identifier")
        if not isinstance(tool.mcp_name, str) or not _TOOL_IDENTIFIER_PATTERN.fullmatch(
            tool.mcp_name
        ):
            raise ValueError("invalid connector tool identifier")
        if tool.operation_kind not in {"read", "write"}:
            raise ValueError("invalid connector tool operation")
        if (
            isinstance(tool.default_timeout_ms, bool)
            or not isinstance(tool.default_timeout_ms, int)
            or tool.default_timeout_ms <= 0
        ):
            raise ValueError("invalid connector tool timeout")
        if tool.cache_ttl_seconds is not None and (
            isinstance(tool.cache_ttl_seconds, bool)
            or not isinstance(tool.cache_ttl_seconds, int)
            or tool.cache_ttl_seconds < 0
        ):
            raise ValueError("invalid connector tool cache ttl")
        if not isinstance(tool.input_schema, dict) or (
            tool.output_schema is not None and not isinstance(tool.output_schema, dict)
        ):
            raise ValueError("invalid connector tool schema")
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
        self._specs: dict[str, ConnectorSpec] = {}
        self._tool_identities: dict[str, str] = {}
        self._discovered_keys: set[str] = set()
        for connector in connectors:
            self.register(connector)

    def register(self, connector: Connector) -> None:
        spec = validate_connector_manifest(connector.spec(), require_version=False)
        self._register(connector, spec)

    def register_validated(
        self,
        connector: Connector,
        spec: ConnectorSpec,
        *,
        discovered: bool = False,
    ) -> None:
        """Register a package connector using its immutable validated metadata."""
        validated = validate_connector_manifest(spec)
        self._register(connector, validated)
        if discovered:
            self._discovered_keys.add(validated.connector_key)

    def clear_discovered_connectors(self) -> None:
        """Remove only prior package discoveries before a new startup pass."""
        if not self._discovered_keys:
            return
        stale = set(self._discovered_keys)
        for connector_key in stale:
            self._connectors.pop(connector_key, None)
            self._specs.pop(connector_key, None)
        self._tool_identities = {
            identity: owner
            for identity, owner in self._tool_identities.items()
            if owner not in stale
        }
        self._discovered_keys.clear()

    def validate_registration(self, spec: ConnectorSpec) -> None:
        """Preflight global identities without mutating registry state."""
        validated = validate_connector_manifest(spec)
        self._validate_registration(validated)

    def _validate_registration(self, spec: ConnectorSpec) -> set[str]:
        connector_key = spec.connector_key
        if connector_key in self._connectors:
            raise ValueError(f"duplicate connector_key: {connector_key}")
        candidate_identities = {
            identifier
            for tool in spec.tools
            for identifier in (tool.tool_key, tool.mcp_name)
        }
        if (
            connector_key in candidate_identities
            or connector_key in self._tool_identities
            or (candidate_identities & self._connectors.keys())
        ):
            raise ValueError("cross-namespace connector identity collision")
        if candidate_identities & self._tool_identities.keys():
            raise ValueError("duplicate tool identifier across connectors")
        return candidate_identities

    def _register(self, connector: Connector, spec: ConnectorSpec) -> None:
        connector_key = spec.connector_key
        candidate_identities = self._validate_registration(spec)

        self._connectors[connector_key] = connector
        self._specs[connector_key] = spec
        self._tool_identities.update(
            {identifier: connector_key for identifier in candidate_identities}
        )

    def get(self, connector_key: str) -> Connector:
        try:
            return self._connectors[connector_key]
        except KeyError as exc:
            raise KeyError(f"unknown connector_key: {connector_key}") from exc

    def connectors(self) -> tuple[Connector, ...]:
        return tuple(self._connectors.values())

    def validated_spec(self, connector_key: str) -> ConnectorSpec | None:
        """Return registration-time metadata without executing connector code."""
        return self._specs.get(connector_key)

    @property
    def registered(self):
        """Read-only keyed view for administration and diagnostics."""
        return MappingProxyType(dict(self._connectors))
