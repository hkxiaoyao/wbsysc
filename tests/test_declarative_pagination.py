from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.connections.models import ConnectionRecord
from app.connectors.contracts import ConnectionContext
from app.connectors.declarative.connector import DeclarativeConnector
from app.connectors.declarative.http_client import SafeHttpClient
from app.connectors.declarative.models import (
    MAX_OUTPUT_BYTES,
    PaginationPolicy,
    SpecValidationError,
)
from app.connectors.declarative.token_cache import OAuthTokenCache
from app.connectors.declarative.validator import import_openapi_revision


def _list_operation(*, pagination: dict[str, object] | None = None) -> dict[str, object]:
    operation: dict[str, object] = {
        "operationId": "people.list",
        "parameters": [
            {
                "name": "team",
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
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
                                    "items": {"type": "string"},
                                },
                                "next_cursor": {"type": "string"},
                                "total": {"type": "integer"},
                            },
                        }
                    }
                },
            }
        },
    }
    if pagination is not None:
        operation["x-pagination"] = pagination
    return operation


def _pagination(**overrides: object) -> dict[str, object]:
    declaration: dict[str, object] = {
        "max_pages": 3,
        "max_items": 500,
        "items_pointer": "/items",
        "next_pointer": "/next_cursor",
        "next_query_param": "cursor",
    }
    declaration.update(overrides)
    return declaration


def _document(
    *,
    pagination: dict[str, object] | None = None,
    operation: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/people": {"get": operation or _list_operation(pagination=pagination)}
        },
    }


def _context() -> ConnectionContext:
    return ConnectionContext(
        connection=ConnectionRecord(
            connection_id="conn-declarative",
            tenant_id="tenant-a",
            connector_key="http_declarative",
            display_name="Declared API",
            status="active",
            data_mode="direct",
            public_config={},
            config_version=1,
        ),
        credentials={},
    )


def _resolver(_: str, __: int) -> list[str]:
    return ["93.184.216.34"]


class _Pages:
    """Serve a fixed page sequence and record every request URL."""

    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.urls: list[httpx.URL] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(request.url)
        index = len(self.urls) - 1
        if index >= len(self.pages):
            raise AssertionError("upstream received more requests than declared pages")
        return httpx.Response(200, json=self.pages[index], request=request)


def _connector(document: dict[str, object], pages: _Pages) -> DeclarativeConnector:
    revision = import_openapi_revision(document)
    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_resolver,
        transport=httpx.MockTransport(pages.handler),
    )
    return DeclarativeConnector._for_test(
        revision=revision,
        client=client,
        token_cache=OAuthTokenCache(),
    )


# --------------------------------------------------------------------------
# Import-time validation
# --------------------------------------------------------------------------


def test_declared_pagination_compiles_into_a_bounded_policy() -> None:
    revision = import_openapi_revision(_document(pagination=_pagination()))

    operation = revision.operation_for("people.list")
    assert operation.pagination == PaginationPolicy(
        max_pages=3,
        max_items=500,
        items_pointer="/items",
        next_pointer="/next_cursor",
        next_query_param="cursor",
    )


def test_operations_without_a_declaration_stay_unpaginated() -> None:
    revision = import_openapi_revision(_document())

    assert revision.operation_for("people.list").pagination is None


def test_cursor_parameter_may_not_collide_with_a_declared_query_input() -> None:
    with pytest.raises(SpecValidationError, match="cursor parameter"):
        import_openapi_revision(
            _document(pagination=_pagination(next_query_param="team"))
        )


def test_items_pointer_must_be_declared_by_the_response_schema() -> None:
    with pytest.raises(SpecValidationError, match="pagination items"):
        import_openapi_revision(
            _document(pagination=_pagination(items_pointer="/undeclared"))
        )


def test_items_pointer_must_address_an_array() -> None:
    with pytest.raises(SpecValidationError, match="pagination items"):
        import_openapi_revision(
            _document(pagination=_pagination(items_pointer="/total"))
        )


def test_next_pointer_must_be_declared_by_the_response_schema() -> None:
    with pytest.raises(SpecValidationError, match="pagination cursor"):
        import_openapi_revision(
            _document(pagination=_pagination(next_pointer="/undeclared"))
        )


def test_pagination_is_rejected_for_write_operations() -> None:
    operation = _list_operation(pagination=_pagination())
    operation["x-operation-kind"] = "write"
    operation["x-write-enabled"] = True
    document = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {"/people": {"post": operation}},
    }

    with pytest.raises(SpecValidationError, match="pagination"):
        import_openapi_revision(document)


def test_page_budget_may_not_exceed_the_platform_ceiling() -> None:
    with pytest.raises(SpecValidationError, match="pagination page limit"):
        import_openapi_revision(_document(pagination=_pagination(max_pages=11)))


def test_item_budget_may_not_exceed_the_platform_ceiling() -> None:
    with pytest.raises(SpecValidationError, match="pagination item limit"):
        import_openapi_revision(_document(pagination=_pagination(max_items=1001)))


def test_unknown_pagination_keys_are_rejected() -> None:
    with pytest.raises(SpecValidationError, match="pagination"):
        import_openapi_revision(
            _document(pagination=_pagination(follow_next_link=True))
        )


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_declared_pages_are_concatenated() -> None:
    pages = _Pages(
        [
            {"items": ["a", "b"], "next_cursor": "c1", "total": 5},
            {"items": ["c", "d"], "next_cursor": "c2", "total": 5},
            {"items": ["e"], "next_cursor": "", "total": 5},
        ]
    )
    connector = _connector(_document(pagination=_pagination()), pages)

    result = await connector.execute(_context(), "people.list", {"team": "core"})

    assert result.status == "ok"
    assert result.data["items"] == ["a", "b", "c", "d", "e"]
    assert len(pages.urls) == 3


@pytest.mark.asyncio
async def test_pagination_stops_when_the_cursor_is_absent() -> None:
    # The cursor is control data, not output: an operation may declare it in the
    # response schema without projecting it, and omitting it ends the traversal.
    operation = _list_operation(pagination=_pagination())
    operation["x-output-mappings"] = {"items": "/items", "total": "/total"}
    pages = _Pages(
        [
            {"items": ["a"], "next_cursor": "c1", "total": 2},
            {"items": ["b"], "total": 2},
        ]
    )
    connector = _connector(_document(operation=operation), pages)

    result = await connector.execute(_context(), "people.list", {"team": "core"})

    assert result.data["items"] == ["a", "b"]
    assert len(pages.urls) == 2


@pytest.mark.asyncio
async def test_page_budget_truncates_the_traversal() -> None:
    pages = _Pages(
        [
            {"items": ["a"], "next_cursor": "c1", "total": 9},
            {"items": ["b"], "next_cursor": "c2", "total": 9},
        ]
    )
    connector = _connector(
        _document(pagination=_pagination(max_pages=2)), pages
    )

    result = await connector.execute(_context(), "people.list", {"team": "core"})

    assert result.data["items"] == ["a", "b"]
    assert len(pages.urls) == 2


@pytest.mark.asyncio
async def test_item_budget_truncates_the_traversal() -> None:
    pages = _Pages(
        [
            {"items": ["a", "b", "c"], "next_cursor": "c1", "total": 9},
        ]
    )
    connector = _connector(
        _document(pagination=_pagination(max_items=2)), pages
    )

    result = await connector.execute(_context(), "people.list", {"team": "core"})

    assert result.data["items"] == ["a", "b"]
    assert len(pages.urls) == 1


@pytest.mark.asyncio
async def test_oversized_accumulation_stops_before_exceeding_the_output_ceiling() -> None:
    chunk = "x" * 4_096
    page = {
        "items": [chunk] * 8,
        "next_cursor": "more",
        "total": 999,
    }
    pages = _Pages([dict(page) for _ in range(3)])
    connector = _connector(
        _document(pagination=_pagination(max_pages=3, max_items=1000)), pages
    )

    result = await connector.execute(_context(), "people.list", {"team": "core"})

    assert result.status == "ok"
    assert len(json.dumps(result.data).encode("utf-8")) <= MAX_OUTPUT_BYTES
    assert len(result.data["items"]) < 24


@pytest.mark.asyncio
async def test_an_oversized_first_page_is_bounded_before_returning() -> None:
    chunk = "x" * 4_096
    pages = _Pages(
        [{"items": [chunk] * 64, "next_cursor": "more", "total": 999}]
    )
    connector = _connector(
        _document(pagination=_pagination(max_pages=3, max_items=1000)), pages
    )

    result = await connector.execute(_context(), "people.list", {"team": "core"})

    assert result.status == "ok"
    assert len(json.dumps(result.data).encode("utf-8")) <= MAX_OUTPUT_BYTES
    assert len(result.data["items"]) < 64
    assert len(pages.urls) == 1


@pytest.mark.asyncio
async def test_a_single_page_over_the_item_ceiling_returns_a_bounded_prefix() -> None:
    pages = _Pages(
        [{"items": [str(index) for index in range(1_001)], "next_cursor": "", "total": 1_001}]
    )
    connector = _connector(
        _document(pagination=_pagination(max_pages=2, max_items=1_000)), pages
    )

    result = await connector.execute(_context(), "people.list", {"team": "core"})

    assert result.status == "ok"
    assert len(result.data["items"]) <= 1_000
    assert len(json.dumps(result.data).encode("utf-8")) <= MAX_OUTPUT_BYTES


@pytest.mark.asyncio
async def test_only_the_cursor_parameter_changes_between_pages() -> None:
    pages = _Pages(
        [
            {"items": ["a"], "next_cursor": "c1", "total": 2},
            {"items": ["b"], "next_cursor": "", "total": 2},
        ]
    )
    connector = _connector(_document(pagination=_pagination()), pages)

    await connector.execute(_context(), "people.list", {"team": "core"})

    first, second = pages.urls
    assert first.host == second.host == "api.example.com"
    assert first.path == second.path == "/v1/people"
    assert parse_qs(urlsplit(str(first)).query) == {"team": ["core"]}
    assert parse_qs(urlsplit(str(second)).query) == {"team": ["core"], "cursor": ["c1"]}


@pytest.mark.asyncio
async def test_a_hostile_cursor_never_redirects_the_next_page() -> None:
    pages = _Pages(
        [
            {
                "items": ["a"],
                "next_cursor": "https://metadata.internal/steal?x=",
                "total": 2,
            },
            {"items": ["b"], "next_cursor": "", "total": 2},
        ]
    )
    connector = _connector(_document(pagination=_pagination()), pages)

    await connector.execute(_context(), "people.list", {"team": "core"})

    assert [url.host for url in pages.urls] == ["api.example.com", "api.example.com"]
    assert pages.urls[1].path == "/v1/people"


@pytest.mark.asyncio
async def test_a_non_scalar_cursor_stops_the_traversal() -> None:
    pages = _Pages([{"items": ["a"], "next_cursor": ["not", "scalar"], "total": 2}])
    connector = _connector(_document(pagination=_pagination()), pages)

    result = await connector.execute(_context(), "people.list", {"team": "core"})

    assert result.data["items"] == ["a"]
    assert len(pages.urls) == 1


@pytest.mark.asyncio
async def test_non_items_fields_come_from_the_first_page() -> None:
    pages = _Pages(
        [
            {"items": ["a"], "next_cursor": "c1", "total": 5},
            {"items": ["b"], "next_cursor": "", "total": 999},
        ]
    )
    connector = _connector(_document(pagination=_pagination()), pages)

    result = await connector.execute(_context(), "people.list", {"team": "core"})

    assert result.data["total"] == 5


@pytest.mark.asyncio
async def test_unpaginated_operations_still_issue_exactly_one_request() -> None:
    pages = _Pages([{"items": ["a"], "next_cursor": "c1", "total": 5}])
    connector = _connector(_document(), pages)

    result = await connector.execute(_context(), "people.list", {"team": "core"})

    assert result.data["items"] == ["a"]
    assert len(pages.urls) == 1
