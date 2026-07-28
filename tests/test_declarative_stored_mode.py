from __future__ import annotations

import httpx
import pytest
from urllib.parse import parse_qs

from app.connections.models import ConnectionRecord
from app.connectors.contracts import ConnectionContext
from app.connectors.declarative.connector import DeclarativeConnector
from app.connectors.declarative.http_client import SafeHttpClient
from app.connectors.declarative.models import SpecValidationError
from app.connectors.declarative.token_cache import OAuthTokenCache
from app.connectors.declarative.validator import import_openapi_revision


def _list_operation() -> dict[str, object]:
    return {
        "operationId": "people.list",
        "parameters": [],
        "responses": {
            "200": {
                "description": "ok",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "items": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "name": {"type": "string"},
                                            "token": {"type": "string"},
                                        },
                                    },
                                },
                                "next_cursor": {"type": "string"},
                            },
                        }
                    }
                },
            }
        },
    }


def _document(*, sync_spec: dict[str, object] | None = None) -> dict[str, object]:
    document: dict[str, object] = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {"/people": {"get": _list_operation()}},
    }
    document["x-sync-spec"] = sync_spec if sync_spec is not None else {
        "resource_key": "people",
        "operation_key": "people.list",
        "items_pointer": "/items",
        "primary_key_pointer": "/id",
        "field_mappings": {"id": "/id", "name": "/name"},
    }
    return document


def _context(
    *, connection_id: str = "conn-a", data_mode: str = "stored"
) -> ConnectionContext:
    return ConnectionContext(
        connection=ConnectionRecord(
            connection_id=connection_id,
            tenant_id="tenant-a",
            connector_key="http_declarative",
            display_name="Declared API",
            status="active",
            data_mode=data_mode,  # type: ignore[arg-type]
            public_config={},
            config_version=1,
        ),
        credentials={},
    )


def _resolver(_: str, __: int) -> list[str]:
    return ["93.184.216.34"]


class _FakeRecordStore:
    """In-memory stand-in for the central declarative_record table."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict[str, object]] = {}
        self.reads: list[tuple[str, str]] = []
        self.cursors: dict[tuple[str, str], str] = {}

    def upsert_declarative_records(self, connection_id, resource_key, records):
        for record in records:
            self.rows[(connection_id, resource_key, record["record_key"])] = dict(
                record["payload"]
            )
        return len(records)

    def list_declarative_records(self, connection_id, resource_key, limit=1000):
        self.reads.append((connection_id, resource_key))
        return tuple(
            dict(payload)
            for (conn, res, _), payload in self.rows.items()
            if conn == connection_id and res == resource_key
        )[:limit]

    def get_declarative_sync_cursor(self, connection_id, resource_key):
        return self.cursors.get((connection_id, resource_key))

    def save_declarative_sync_cursor(self, connection_id, resource_key, cursor):
        key = (connection_id, resource_key)
        if cursor is None:
            self.cursors.pop(key, None)
        else:
            self.cursors[key] = cursor


class _Upstream:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        index = self.requests
        self.requests += 1
        return httpx.Response(200, json=self.pages[index], request=request)


class _CursorUpstream:
    def __init__(self, pages: dict[str, dict[str, object]]) -> None:
        self.pages = pages
        self.cursors: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        cursor = parse_qs(request.url.query.decode()).get("cursor", [""])[0]
        self.cursors.append(cursor)
        return httpx.Response(200, json=self.pages[cursor], request=request)


def _connector(
    document: dict[str, object],
    upstream: _Upstream,
    store: _FakeRecordStore,
) -> DeclarativeConnector:
    revision = import_openapi_revision(document)
    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_resolver,
        transport=httpx.MockTransport(upstream.handler),
    )
    return DeclarativeConnector._for_test(
        revision=revision,
        client=client,
        token_cache=OAuthTokenCache(),
        record_store=store,
    )


# --------------------------------------------------------------------------
# Declaration
# --------------------------------------------------------------------------


def test_items_pointer_enables_a_list_sync_declaration() -> None:
    revision = import_openapi_revision(_document())

    assert revision.sync_spec is not None
    assert revision.sync_spec.items_pointer == "/items"
    revision.assert_data_mode_allowed("stored")


def test_items_pointer_must_be_a_declared_output_mapping() -> None:
    with pytest.raises(SpecValidationError, match="sync items"):
        import_openapi_revision(
            _document(
                sync_spec={
                    "resource_key": "people",
                    "operation_key": "people.list",
                    "items_pointer": "/undeclared",
                    "primary_key_pointer": "/id",
                    "field_mappings": {"id": "/id"},
                }
            )
        )


def test_single_record_sync_declarations_remain_supported() -> None:
    document: dict[str, object] = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/person": {
                "get": {
                    "operationId": "person.get",
                    "parameters": [],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "name": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
        "x-sync-spec": {
            "resource_key": "person",
            "operation_key": "person.get",
            "primary_key_pointer": "/id",
            "field_mappings": {"id": "/id", "name": "/name"},
        },
    }

    revision = import_openapi_revision(document)

    assert revision.sync_spec is not None
    assert revision.sync_spec.items_pointer == ""
    revision.assert_data_mode_allowed("stored")


# --------------------------------------------------------------------------
# Sync persistence
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_persists_only_the_mapped_projection() -> None:
    upstream = _Upstream(
        [
            {
                "items": [
                    {"id": "p1", "name": "Ada", "token": "secret-value"},
                    {"id": "p2", "name": "Bob", "token": "secret-value"},
                ],
                "next_cursor": "",
            }
        ]
    )
    store = _FakeRecordStore()
    connector = _connector(_document(), upstream, store)

    result = await connector.sync(_context(), "people")

    assert result.status == "ok"
    assert result.data == {"pulled": 2, "stored": 2, "skipped": 0}
    assert store.rows[("conn-a", "people", "p1")] == {"id": "p1", "name": "Ada"}
    assert store.rows[("conn-a", "people", "p2")] == {"id": "p2", "name": "Bob"}
    # The undeclared upstream field never reaches storage.
    assert all("token" not in payload for payload in store.rows.values())


@pytest.mark.asyncio
async def test_repeated_sync_is_idempotent() -> None:
    page = {
        "items": [{"id": "p1", "name": "Ada", "token": "t"}],
        "next_cursor": "",
    }
    upstream = _Upstream([dict(page), dict(page)])
    store = _FakeRecordStore()
    connector = _connector(_document(), upstream, store)

    await connector.sync(_context(), "people")
    await connector.sync(_context(), "people")

    assert len(store.rows) == 1


@pytest.mark.asyncio
async def test_paginated_sync_resumes_from_its_durable_page_checkpoint() -> None:
    document = _document()
    operation = document["paths"]["/people"]["get"]
    operation["x-pagination"] = {
        "max_pages": 2,
        "max_items": 100,
        "items_pointer": "/items",
        "next_pointer": "/next_cursor",
        "next_query_param": "cursor",
    }
    upstream = _CursorUpstream(
        {
            "": {
                "items": [{"id": "p1", "name": "Ada"}],
                "next_cursor": "c1",
            },
            "c1": {
                "items": [{"id": "p2", "name": "Bob"}],
                "next_cursor": "c2",
            },
            "c2": {
                "items": [{"id": "p3", "name": "Cy"}],
                "next_cursor": "",
            },
        }
    )
    store = _FakeRecordStore()
    connector = _connector(document, upstream, store)  # type: ignore[arg-type]

    first = await connector.sync(_context(), "people")
    assert first.data == {"pulled": 2, "stored": 2, "skipped": 0}
    assert store.cursors[("conn-a", "people")] == "c2"

    second = await connector.sync(_context(), "people")
    assert second.data == {"pulled": 1, "stored": 1, "skipped": 0}
    assert upstream.cursors == ["", "c1", "c2"]
    assert store.cursors == {}
    assert len(store.rows) == 3


@pytest.mark.asyncio
async def test_records_without_a_usable_primary_key_are_skipped() -> None:
    upstream = _Upstream(
        [
            {
                "items": [
                    {"id": "p1", "name": "Ada", "token": "t"},
                    {"name": "Missing key", "token": "t"},
                ],
                "next_cursor": "",
            }
        ]
    )
    store = _FakeRecordStore()
    connector = _connector(_document(), upstream, store)

    result = await connector.sync(_context(), "people")

    assert result.status == "partial"
    assert result.data == {"pulled": 2, "stored": 1, "skipped": 1}
    assert set(store.rows) == {("conn-a", "people", "p1")}


@pytest.mark.asyncio
async def test_sync_rejects_an_unknown_resource_key() -> None:
    upstream = _Upstream([{"items": [], "next_cursor": ""}])
    store = _FakeRecordStore()
    connector = _connector(_document(), upstream, store)

    with pytest.raises(SpecValidationError, match="unknown declarative sync resource"):
        await connector.sync(_context(), "not-declared")


@pytest.mark.asyncio
async def test_sync_never_returns_upstream_payloads_to_the_orchestrator() -> None:
    upstream = _Upstream(
        [
            {
                "items": [{"id": "p1", "name": "Ada", "token": "secret-value"}],
                "next_cursor": "",
            }
        ]
    )
    store = _FakeRecordStore()
    connector = _connector(_document(), upstream, store)

    result = await connector.sync(_context(), "people")

    assert "secret-value" not in repr(result)
    assert "Ada" not in repr(result)
    assert set(result.data) == {"pulled", "stored", "skipped"}


@pytest.mark.asyncio
async def test_a_mapped_sensitive_field_skips_the_record() -> None:
    document = _document(
        sync_spec={
            "resource_key": "people",
            "operation_key": "people.list",
            "items_pointer": "/items",
            "primary_key_pointer": "/id",
            "field_mappings": {"id": "/id", "access_token": "/token"},
        }
    )
    upstream = _Upstream(
        [
            {
                "items": [{"id": "p1", "name": "Ada", "token": "secret-value"}],
                "next_cursor": "",
            }
        ]
    )
    store = _FakeRecordStore()
    connector = _connector(document, upstream, store)

    result = await connector.sync(_context(), "people")

    assert result.status == "partial"
    assert result.data == {"pulled": 1, "stored": 0, "skipped": 1}
    assert store.rows == {}


@pytest.mark.asyncio
async def test_single_record_sync_resolves_renamed_output_fields_by_pointer() -> None:
    operation = {
        "operationId": "person.get",
        "parameters": [],
        "x-output-mappings": {
            "person_id": "/id",
            "display_name": "/name",
        },
        "responses": {
            "200": {
                "description": "ok",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "name": {"type": "string"},
                            },
                        }
                    }
                },
            }
        },
    }
    document = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {"/person": {"get": operation}},
        "x-sync-spec": {
            "resource_key": "person",
            "operation_key": "person.get",
            "primary_key_pointer": "/id",
            "field_mappings": {"external_id": "/id", "label": "/name"},
        },
    }
    upstream = _Upstream([{"id": "p1", "name": "Ada"}])
    store = _FakeRecordStore()
    connector = _connector(document, upstream, store)

    result = await connector.sync(_context(), "person")

    assert result.status == "ok"
    assert store.rows == {
        ("conn-a", "person", "p1"): {"external_id": "p1", "label": "Ada"}
    }


# --------------------------------------------------------------------------
# Stored read path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stored_mode_reads_the_local_table_without_calling_upstream() -> None:
    upstream = _Upstream(
        [
            {
                "items": [{"id": "p1", "name": "Ada", "token": "t"}],
                "next_cursor": "",
            }
        ]
    )
    store = _FakeRecordStore()
    connector = _connector(_document(), upstream, store)

    await connector.sync(_context(), "people")
    assert upstream.requests == 1

    result = await connector.execute(_context(), "people.list", {})

    assert result.status == "ok"
    assert result.data == {"items": [{"id": "p1", "name": "Ada"}]}
    # No second upstream request: the read was served from storage.
    assert upstream.requests == 1
    assert store.reads == [("conn-a", "people")]


@pytest.mark.asyncio
async def test_direct_mode_still_reaches_upstream() -> None:
    upstream = _Upstream(
        [{"items": [{"id": "p1", "name": "Ada", "token": "t"}], "next_cursor": ""}]
    )
    store = _FakeRecordStore()
    connector = _connector(_document(), upstream, store)

    result = await connector.execute(_context(data_mode="direct"), "people.list", {})

    assert upstream.requests == 1
    assert store.reads == []
    assert result.data["items"][0]["id"] == "p1"


@pytest.mark.asyncio
async def test_stored_reads_are_scoped_to_the_requesting_connection() -> None:
    upstream = _Upstream(
        [
            {"items": [{"id": "a1", "name": "A", "token": "t"}], "next_cursor": ""},
            {"items": [{"id": "b1", "name": "B", "token": "t"}], "next_cursor": ""},
        ]
    )
    store = _FakeRecordStore()
    connector = _connector(_document(), upstream, store)

    await connector.sync(_context(connection_id="conn-a"), "people")
    await connector.sync(_context(connection_id="conn-b"), "people")

    result = await connector.execute(
        _context(connection_id="conn-b"), "people.list", {}
    )

    assert result.data == {"items": [{"id": "b1", "name": "B"}]}
