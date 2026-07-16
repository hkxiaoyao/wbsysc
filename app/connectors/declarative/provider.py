"""Connection-scoped resolver for constrained declarative connectors."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from app.connections import store
from app.connectors.contracts import ConnectionContext, Connector, ConnectorSpec

from .connector import DeclarativeConnector
from .http_client import SafeHttpClient
from .models import DeclarativeRevision
from .validator import validate_revision


DECLARATIVE_CONNECTOR_KEY = "http_declarative"


class DeclarativeProviderUnavailableError(LookupError):
    """The exact published revision cannot safely serve this connection."""


RevisionLoader = Callable[[str, int, str, str], Any]
ClientFactory = Callable[[DeclarativeRevision], SafeHttpClient]


def _load_revision(
    spec_id: str, revision: int, tenant_id: str, connection_id: str
) -> Any:
    return store.get_declarative_revision(
        spec_id, revision, tenant_id, connection_id
    )


def _production_client(revision: DeclarativeRevision) -> SafeHttpClient:
    return SafeHttpClient(allowed_hosts=revision.allowed_hosts)


class DeclarativeConnectorProvider:
    connector_key = DECLARATIVE_CONNECTOR_KEY

    def __init__(
        self,
        *,
        revision_loader: RevisionLoader | None = None,
        client_factory: ClientFactory | None = None,
        _allow_test_transport: bool = False,
    ) -> None:
        self._revision_loader = revision_loader or _load_revision
        self._client_factory = client_factory or _production_client
        self._allow_test_transport = _allow_test_transport

    @classmethod
    def _for_test(
        cls,
        *,
        revision_loader: RevisionLoader,
        client_factory: ClientFactory,
    ) -> "DeclarativeConnectorProvider":
        return cls(
            revision_loader=revision_loader,
            client_factory=client_factory,
            _allow_test_transport=True,
        )

    def spec_for(self, context: ConnectionContext) -> ConnectorSpec:
        revision = self._revision_for(context)
        derived = revision.connector_spec()
        credential_schema = dict(derived.credential_schema)
        properties = credential_schema.get("properties", {})
        if not isinstance(properties, dict):
            properties = dict(properties)
        credential_schema.update(
            {
                "properties": properties,
                "required": sorted(properties),
                "additionalProperties": False,
            }
        )
        return ConnectorSpec(
            connector_key=derived.connector_key,
            tools=derived.tools,
            supports_sync=derived.supports_sync,
            version=derived.version,
            config_schema={
                "type": "object",
                "required": ["spec_id", "revision"],
                "properties": {
                    "spec_id": {"type": "string"},
                    "revision": {"type": "integer"},
                    "pending_spec_id": {"type": "string"},
                    "pending_revision": {"type": "integer"},
                    "sync_enabled": {"type": "boolean"},
                    "sync_resources": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "additionalProperties": False,
            },
            credential_schema=credential_schema,
            supports_data_modes=derived.supports_data_modes,
        )

    @asynccontextmanager
    async def connect(self, context: ConnectionContext) -> AsyncIterator[Connector]:
        revision = self._revision_for(context)
        client: SafeHttpClient | None = None
        try:
            client = self._client_factory(revision)
            if not isinstance(client, SafeHttpClient):
                raise TypeError("invalid declarative HTTP client")
            connector = (
                DeclarativeConnector._for_test(revision=revision, client=client)
                if self._allow_test_transport
                else DeclarativeConnector(revision=revision, client=client)
            )
            yield connector
        except DeclarativeProviderUnavailableError:
            raise
        except Exception:
            raise DeclarativeProviderUnavailableError(
                "declarative connector is unavailable"
            ) from None
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass

    def _revision_for(self, context: ConnectionContext) -> DeclarativeRevision:
        try:
            if (
                not isinstance(context, ConnectionContext)
                or context.connector_key != self.connector_key
            ):
                raise ValueError
            spec_id = context.public_config.get("spec_id")
            revision_number = context.public_config.get("revision")
            if (
                not isinstance(spec_id, str)
                or not spec_id
                or not isinstance(revision_number, int)
                or isinstance(revision_number, bool)
                or revision_number < 1
            ):
                raise ValueError
            loaded = self._revision_loader(
                spec_id,
                revision_number,
                context.tenant_id,
                context.connection_id,
            )
            if not isinstance(loaded, DeclarativeRevision):
                raise ValueError
            revision = validate_revision(loaded, data_mode=context.data_mode)
            if (
                revision.status != "published"
                or revision.spec_id != spec_id
                or revision.revision != revision_number
                or revision.tenant_id != context.tenant_id
                or revision.connection_id != context.connection_id
            ):
                raise ValueError
            return revision
        except Exception:
            raise DeclarativeProviderUnavailableError(
                "declarative connector is unavailable"
            ) from None
