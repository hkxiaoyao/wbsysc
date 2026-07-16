"""Explicit registry for trusted, already-imported connector instances."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
import re
from types import MappingProxyType
import unicodedata

from .contracts import Connector, ConnectorSpec, ToolSpec


_CONNECTOR_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MANIFEST_VERSION_PATTERN = re.compile(
    r"^[0-9]+(?:\.[0-9]+){0,2}(?:[-+][A-Za-z0-9][A-Za-z0-9.-]*)?$"
)
_TOOL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_MAX_DESCRIPTION_LENGTH = 2_048
_MAX_JSON_DEPTH = 24
_MAX_JSON_NODES = 4_096
_MAX_JSON_KEY_LENGTH = 256
_MAX_JSON_STRING_LENGTH = 16_384
_MIN_JSON_INTEGER = -(2**63)
_MAX_JSON_INTEGER = 2**63 - 1
_MAX_JSON_FLOAT_MAGNITUDE = 1e308


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _freeze_json_tree(value: object, *, field_name: str) -> object:
    """Validate and detach a bounded, acyclic JSON-compatible tree."""
    active: set[int] = set()
    nodes = 0

    def freeze(node: object, depth: int) -> object:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError(f"invalid connector {field_name}")
        if node is None or isinstance(node, bool):
            return node
        if isinstance(node, int) and not isinstance(node, bool):
            if not _MIN_JSON_INTEGER <= node <= _MAX_JSON_INTEGER:
                raise ValueError(f"invalid connector {field_name}")
            return node
        if isinstance(node, float):
            if not math.isfinite(node) or abs(node) > _MAX_JSON_FLOAT_MAGNITUDE:
                raise ValueError(f"invalid connector {field_name}")
            return node
        if isinstance(node, str):
            if len(node) > _MAX_JSON_STRING_LENGTH or _has_control_characters(node):
                raise ValueError(f"invalid connector {field_name}")
            return node

        identity = id(node)
        if identity in active:
            raise ValueError(f"invalid connector {field_name}")
        if isinstance(node, Mapping):
            active.add(identity)
            try:
                frozen: dict[str, object] = {}
                for key, item in node.items():
                    if (
                        not isinstance(key, str)
                        or not key
                        or len(key) > _MAX_JSON_KEY_LENGTH
                        or _has_control_characters(key)
                    ):
                        raise ValueError(f"invalid connector {field_name}")
                    frozen[key] = freeze(item, depth + 1)
                return MappingProxyType(frozen)
            finally:
                active.remove(identity)
        if isinstance(node, (list, tuple)):
            active.add(identity)
            try:
                return tuple(freeze(item, depth + 1) for item in node)
            finally:
                active.remove(identity)
        raise ValueError(f"invalid connector {field_name}")

    return freeze(value, 0)


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
    if not isinstance(spec.config_schema, Mapping) or not isinstance(
        spec.credential_schema, Mapping
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
    frozen_tools: list[ToolSpec] = []
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
            not isinstance(tool.description, str)
            or not tool.description
            or len(tool.description) > _MAX_DESCRIPTION_LENGTH
            or _has_control_characters(tool.description)
        ):
            raise ValueError("invalid connector tool description")
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
        if not isinstance(tool.input_schema, Mapping) or (
            tool.output_schema is not None
            and not isinstance(tool.output_schema, Mapping)
        ):
            raise ValueError("invalid connector tool schema")
        for identifier in {tool.tool_key, tool.mcp_name}:
            if identifier in identifiers:
                raise ValueError("invalid connector manifest")
            identifiers.add(identifier)
        frozen_tools.append(
            ToolSpec(
                tool_key=tool.tool_key,
                mcp_name=tool.mcp_name,
                description=tool.description,
                input_schema=_freeze_json_tree(
                    tool.input_schema, field_name="tool schema"
                ),
                output_schema=(
                    None
                    if tool.output_schema is None
                    else _freeze_json_tree(tool.output_schema, field_name="tool schema")
                ),
                operation_kind=tool.operation_kind,
                default_timeout_ms=tool.default_timeout_ms,
                cache_ttl_seconds=tool.cache_ttl_seconds,
            )
        )
    return ConnectorSpec(
        connector_key=spec.connector_key,
        tools=tuple(frozen_tools),
        supports_sync=spec.supports_sync,
        version=spec.version,
        config_schema=_freeze_json_tree(spec.config_schema, field_name="config schema"),
        credential_schema=_freeze_json_tree(
            spec.credential_schema, field_name="credential schema"
        ),
        supports_data_modes=tuple(spec.supports_data_modes),
    )


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

    @staticmethod
    def _validate_against(
        spec: ConnectorSpec,
        connectors: Mapping[str, Connector],
        tool_identities: Mapping[str, str],
    ) -> set[str]:
        connector_key = spec.connector_key
        if connector_key in connectors:
            raise ValueError(f"duplicate connector_key: {connector_key}")
        candidate_identities = {
            identifier
            for tool in spec.tools
            for identifier in (tool.tool_key, tool.mcp_name)
        }
        if (
            connector_key in candidate_identities
            or connector_key in tool_identities
            or (candidate_identities & connectors.keys())
        ):
            raise ValueError("cross-namespace connector identity collision")
        if candidate_identities & tool_identities.keys():
            raise ValueError("duplicate tool identifier across connectors")
        return candidate_identities

    def _validate_registration(self, spec: ConnectorSpec) -> set[str]:
        return self._validate_against(spec, self._connectors, self._tool_identities)

    def _base_without_discovered(self):
        stale = set(self._discovered_keys)
        connectors = {
            key: connector
            for key, connector in self._connectors.items()
            if key not in stale
        }
        specs = {key: spec for key, spec in self._specs.items() if key not in stale}
        identities = {
            identity: owner
            for identity, owner in self._tool_identities.items()
            if owner not in stale
        }
        return connectors, specs, identities

    def validate_discovered_batch(
        self, specs: Iterable[ConnectorSpec]
    ) -> tuple[ConnectorSpec, ...]:
        """Validate a complete replacement batch without mutating live state."""
        connectors, _registered_specs, identities = self._base_without_discovered()
        snapshots: list[ConnectorSpec] = []
        for spec in specs:
            snapshot = validate_connector_manifest(spec)
            candidate_identities = self._validate_against(
                snapshot, connectors, identities
            )
            connectors[snapshot.connector_key] = None  # type: ignore[assignment]
            identities.update(
                {identity: snapshot.connector_key for identity in candidate_identities}
            )
            snapshots.append(snapshot)
        return tuple(snapshots)

    def partition_discovered_batch(
        self, specs: Iterable[ConnectorSpec]
    ) -> tuple[tuple[tuple[int, ConnectorSpec], ...], tuple[int, ...]]:
        """Select a deterministic non-conflicting subset without live mutation."""
        connectors, _registered_specs, identities = self._base_without_discovered()
        accepted: list[tuple[int, ConnectorSpec]] = []
        rejected: list[int] = []
        for index, spec in enumerate(specs):
            try:
                snapshot = validate_connector_manifest(spec)
                candidate_identities = self._validate_against(
                    snapshot, connectors, identities
                )
            except ValueError:
                rejected.append(index)
                continue
            connectors[snapshot.connector_key] = None  # type: ignore[assignment]
            identities.update(
                {identity: snapshot.connector_key for identity in candidate_identities}
            )
            accepted.append((index, snapshot))
        return tuple(accepted), tuple(rejected)

    def replace_discovered_connectors(
        self,
        connectors: Iterable[tuple[Connector, ConnectorSpec]],
    ) -> None:
        """Atomically replace the whole package batch after global preflight."""
        items = tuple(connectors)
        snapshots = self.validate_discovered_batch(spec for _, spec in items)
        next_connectors, next_specs, next_identities = self._base_without_discovered()
        next_discovered: set[str] = set()
        for (connector, _spec), snapshot in zip(items, snapshots, strict=True):
            identities = {
                identifier
                for tool in snapshot.tools
                for identifier in (tool.tool_key, tool.mcp_name)
            }
            next_connectors[snapshot.connector_key] = connector
            next_specs[snapshot.connector_key] = snapshot
            next_identities.update(
                {identity: snapshot.connector_key for identity in identities}
            )
            next_discovered.add(snapshot.connector_key)

        self._connectors = next_connectors
        self._specs = next_specs
        self._tool_identities = next_identities
        self._discovered_keys = next_discovered

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
