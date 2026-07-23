from __future__ import annotations

import asyncio

import httpx
import pytest

from app.connections.models import ConnectionRecord
from app.connectors.contracts import ConnectionContext
from app.connectors.declarative import connector as connector_module
from app.connectors.declarative.connector import DeclarativeConnector
from app.connectors.declarative.http_client import SafeHttpClient
from app.connectors.declarative.models import UnknownToolError
from app.connectors.declarative.validator import import_openapi_revision


def _operation(output_fields: tuple[str, ...]) -> dict[str, object]:
    return {
        "parameters": [
            {
                "name": "id",
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
                                name: {"type": "string"} for name in output_fields
                            },
                        }
                    }
                },
            }
        },
    }


def _document(*, first_timeout_ms: int | None = None) -> dict[str, object]:
    lookup = _operation(("entity_id", "upstream_secret"))
    lookup["operationId"] = "people.lookup"
    details = _operation(("display_name", "internal_note"))
    details["operationId"] = "people.details"
    first_step: dict[str, object] = {
        "step_id": "lookup",
        "operation_key": "people.lookup",
        "input_map": {"id": "$input.id"},
        "output_mappings": {"resolved_id": "entity_id"},
    }
    if first_timeout_ms is not None:
        first_step["timeout_ms"] = first_timeout_ms
    return {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/people/lookup": {"get": lookup},
            "/people/details": {"get": details},
        },
        "x-mcp-tools": [
            {
                "tool_key": "people.get",
                "mcp_name": "people.get",
                "description": "Get one person",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "steps": [
                    first_step,
                    {
                        "step_id": "details",
                        "operation_key": "people.details",
                        "input_map": {"id": "$steps.lookup.resolved_id"},
                        "output_mappings": {"public_name": "display_name"},
                    },
                ],
                "result_map": {"name": "$steps.details.public_name"},
            }
        ],
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


async def _resolver(_: str, __: int) -> list[str]:
    return ["93.184.216.34"]


def _connector(
    handler,
    *,
    document: dict[str, object] | None = None,
) -> DeclarativeConnector:
    revision = import_openapi_revision(document or _document())
    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_resolver,
        transport=httpx.MockTransport(handler),
    )
    return DeclarativeConnector._for_test(revision=revision, client=client)


@pytest.mark.asyncio
async def test_execute_runs_steps_sequentially_and_returns_only_the_result_map() -> (
    None
):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/lookup"):
            return httpx.Response(
                200,
                json={"entity_id": "entity-7", "upstream_secret": "omit-me"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"display_name": "Ada", "internal_note": "omit-me-too"},
            request=request,
        )

    result = await _connector(handler).execute(_context(), "people.get", {"id": "7"})

    assert seen == ["/v1/people/lookup", "/v1/people/details"]
    assert result.status == "ok"
    assert result.data == {"name": "Ada"}


@pytest.mark.asyncio
async def test_execute_resolves_tool_input_and_only_mapped_earlier_step_outputs() -> (
    None
):
    seen_queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(dict(request.url.params))
        if request.url.path.endswith("/lookup"):
            return httpx.Response(
                200,
                json={"entity_id": "bounded-id", "upstream_secret": "not-a-reference"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"display_name": "Lin", "internal_note": "bounded-too"},
            request=request,
        )

    result = await _connector(handler).execute(
        _context(), "people.get", {"id": "public-input"}
    )

    assert seen_queries == [{"id": "public-input"}, {"id": "bounded-id"}]
    assert result.data == {"name": "Lin"}
    assert "not-a-reference" not in repr(result)


@pytest.mark.asyncio
async def test_execute_stops_before_later_requests_after_the_first_failed_step() -> (
    None
):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            503,
            json={"detail": "sensitive upstream failure"},
            request=request,
        )

    result = await _connector(handler).execute(_context(), "people.get", {"id": "7"})

    assert seen == ["/v1/people/lookup"]
    assert result.status == "error"
    assert result.data == {"error": "declarative operation failed"}


@pytest.mark.asyncio
async def test_execute_applies_the_declared_step_timeout_and_stops_later_requests() -> (
    None
):
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"entity_id": "late"}, request=request)

    result = await _connector(handler, document=_document(first_timeout_ms=5)).execute(
        _context(), "people.get", {"id": "7"}
    )

    assert seen == ["/v1/people/lookup"]
    assert result.status == "error"
    assert result.data == {"error": "declarative operation failed"}


@pytest.mark.asyncio
async def test_execute_applies_a_whole_tool_timeout_and_cancels_remaining_steps(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        connector_module, "_MAX_TOOL_TIMEOUT_SECONDS", 0.01, raising=False
    )
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"entity_id": "late"}, request=request)

    result = await _connector(handler).execute(_context(), "people.get", {"id": "7"})

    assert seen == ["/v1/people/lookup"]
    assert result.status == "error"
    assert result.data == {"error": "declarative operation failed"}


@pytest.mark.asyncio
async def test_execute_redacts_transport_and_mapping_failures() -> None:
    secret = "credential=never-retain"

    def transport_failure(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"{secret} url={request.url}")

    transport_result = await _connector(transport_failure).execute(
        _context(), "people.get", {"id": secret}
    )

    def mapping_failure(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"body": secret}, request=request)

    mapping_result = await _connector(mapping_failure).execute(
        _context(), "people.get", {"id": secret}
    )

    for result in (transport_result, mapping_result):
        assert result.status == "error"
        assert result.data == {"error": "declarative operation failed"}
        assert secret not in repr(result)
        assert "api.example.com" not in repr(result)


@pytest.mark.asyncio
async def test_execute_preserves_unknown_public_tool_errors() -> None:
    connector = _connector(
        lambda request: httpx.Response(200, json={}, request=request)
    )

    with pytest.raises(UnknownToolError):
        await connector.execute(_context(), "people.missing", {})


@pytest.mark.asyncio
async def test_execute_preserves_synthesized_legacy_single_step_behavior() -> None:
    document = _document()
    document.pop("x-mcp-tools")
    document["paths"].pop("/people/details")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"entity_id": "entity-7", "upstream_secret": "selected-value"},
            request=request,
        )

    result = await _connector(handler, document=document).execute(
        _context(), "people.lookup", {"id": "7"}
    )

    assert result.status == "ok"
    assert result.data == {
        "entity_id": "entity-7",
        "upstream_secret": "selected-value",
    }


@pytest.mark.asyncio
async def test_legacy_single_step_rejects_undeclared_args_before_any_request() -> None:
    document = _document()
    document.pop("x-mcp-tools")
    document["paths"].pop("/people/details")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={}, request=request)

    result = await _connector(handler, document=document).execute(
        _context(),
        "people.lookup",
        {"id": "7", "undeclared": "must-reject"},
    )

    assert seen == []
    assert result.status == "error"
    assert result.data == {"error": "declarative operation failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"id": "7", "undeclared": "must-reject"},
        {"id": 7},
    ],
    ids=["required", "additional-properties", "declared-type"],
)
async def test_composite_tool_validates_its_public_input_schema_before_requests(
    args: dict[str, object],
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={}, request=request)

    result = await _connector(handler).execute(_context(), "people.get", args)

    assert seen == []
    assert result.status == "error"
    assert result.data == {"error": "declarative operation failed"}


def _document_with_metadata_schema(schema: dict[str, object]) -> dict[str, object]:
    document = _document()
    tool_schema = document["x-mcp-tools"][0]["input_schema"]
    tool_schema["properties"]["metadata"] = schema
    return document


def _successful_orchestration_handler(
    seen: list[httpx.Request],
):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/lookup"):
            return httpx.Response(
                200,
                json={"entity_id": "entity-7", "upstream_secret": "omit"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"display_name": "Ada", "internal_note": "omit"},
            request=request,
        )

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nested_schema",
    [
        {
            "type": "object",
            "properties": {"known": {"type": "string"}},
            "additionalProperties": True,
        },
        {
            "type": "object",
            "properties": {"known": {"type": "string"}},
        },
    ],
    ids=["explicit-open", "default-open"],
)
async def test_nested_open_objects_accept_additional_properties(
    nested_schema: dict[str, object],
) -> None:
    seen: list[httpx.Request] = []
    connector = _connector(
        _successful_orchestration_handler(seen),
        document=_document_with_metadata_schema(nested_schema),
    )

    result = await connector.execute(
        _context(),
        "people.get",
        {"id": "7", "metadata": {"extension": {"nested": "allowed"}}},
    )

    assert len(seen) == 2
    assert result.status == "ok"
    assert result.data == {"name": "Ada"}


@pytest.mark.asyncio
async def test_nested_closed_objects_reject_additional_properties_before_requests() -> None:
    schema = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "additionalProperties": False,
    }
    seen: list[httpx.Request] = []
    connector = _connector(
        _successful_orchestration_handler(seen),
        document=_document_with_metadata_schema(schema),
    )

    result = await connector.execute(
        _context(),
        "people.get",
        {"id": "7", "metadata": {"extension": "rejected"}},
    )

    assert seen == []
    assert result.status == "error"
    assert result.data == {"error": "declarative operation failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extension", "expected_status", "expected_requests"),
    [("allowed", "ok", 2), (7, "error", 0)],
    ids=["valid-extra-value", "invalid-extra-value"],
)
async def test_nested_additional_property_schemas_validate_extra_values(
    extension: object,
    expected_status: str,
    expected_requests: int,
) -> None:
    schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": {"type": "string"},
    }
    seen: list[httpx.Request] = []
    connector = _connector(
        _successful_orchestration_handler(seen),
        document=_document_with_metadata_schema(schema),
    )

    result = await connector.execute(
        _context(),
        "people.get",
        {"id": "7", "metadata": {"extension": extension}},
    )

    assert len(seen) == expected_requests
    assert result.status == expected_status
    assert result.data == (
        {"name": "Ada"}
        if expected_status == "ok"
        else {"error": "declarative operation failed"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_value",
    ["not-null", 7, False, {}, []],
    ids=["string", "number", "boolean", "object", "array"],
)
async def test_null_public_input_rejects_every_non_null_json_type_before_requests(
    invalid_value: object,
) -> None:
    seen: list[httpx.Request] = []
    connector = _connector(
        _successful_orchestration_handler(seen),
        document=_document_with_metadata_schema({"type": "null"}),
    )

    result = await connector.execute(
        _context(),
        "people.get",
        {"id": "7", "metadata": invalid_value},
    )

    assert seen == []
    assert result.status == "error"
    assert result.data == {"error": "declarative operation failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "valid_value", "invalid_value"),
    [
        ({"type": "string"}, "value", 1),
        ({"type": "integer"}, 1, True),
        ({"type": "number"}, 1.5, False),
        ({"type": "boolean"}, True, 1),
        ({"type": "null"}, None, "not-null"),
        (
            {"type": "object", "properties": {}, "additionalProperties": True},
            {},
            [],
        ),
        ({"type": "array", "items": {"type": "string"}}, ["value"], {}),
    ],
    ids=["string", "integer", "number", "boolean", "null", "object", "array"],
)
async def test_public_input_type_conformance(
    schema: dict[str, object],
    valid_value: object,
    invalid_value: object,
) -> None:
    seen: list[httpx.Request] = []
    connector = _connector(
        _successful_orchestration_handler(seen),
        document=_document_with_metadata_schema(schema),
    )

    valid_result = await connector.execute(
        _context(),
        "people.get",
        {"id": "7", "metadata": valid_value},
    )

    assert len(seen) == 2
    assert valid_result.status == "ok"
    seen.clear()

    invalid_result = await connector.execute(
        _context(),
        "people.get",
        {"id": "7", "metadata": invalid_value},
    )

    assert seen == []
    assert invalid_result.status == "error"
    assert invalid_result.data == {"error": "declarative operation failed"}
