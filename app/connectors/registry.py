"""Explicit registry for trusted, already-imported connector instances."""
from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType

from .contracts import Connector


class ConnectorRegistry:
    """Stores only connector instances explicitly supplied by application code.

    Package entry-point discovery is intentionally absent.  Loading third-party
    packages is a later, separately reviewed responsibility.
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
