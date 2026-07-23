from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import gc
import warnings

import httpx
import pytest

from app.connections.models import ConnectionRecord
from app.connectors.contracts import ConnectionContext
from app.connectors.declarative import connector as connector_module
from app.connectors.declarative.connector import DeclarativeConnector
from app.connectors.declarative.http_client import SafeHttpClient
from app.connectors.declarative.validator import import_openapi_revision
from app.mcp_log_models import MAX_STEP_AUDIT_COST_MS, StepAuditEvent


def _document(*, timeout_ms: int | None = None) -> dict[str, object]:
    first_step: dict[str, object] = {
        "step_id": "lookup",
        "operation_key": "people.lookup",
        "input_map": {"id": "$input.id"},
        "output_mappings": {"resolved_id": "entity_id"},
    }
    if timeout_ms is not None:
        first_step["timeout_ms"] = timeout_ms
    return {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/lookup": {
                "get": {
                    "operationId": "people.lookup",
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
                                        "properties": {"entity_id": {"type": "string"}},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/details": {
                "get": {
                    "operationId": "people.details",
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
                                            "display_name": {"type": "string"}
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
        },
        "x-mcp-tools": [
            {
                "tool_key": "people.get",
                "mcp_name": "people_get",
                "description": "Get a person",
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
                        "output_mappings": {"name": "display_name"},
                    },
                ],
                "result_map": {"name": "$steps.details.name"},
            }
        ],
    }


async def _resolver(_: str, __: int) -> list[str]:
    return ["93.184.216.34"]


def _context() -> ConnectionContext:
    return ConnectionContext(
        connection=ConnectionRecord(
            connection_id="conn-safe",
            tenant_id="tenant-a",
            connector_key="http_declarative",
            display_name="Safe",
            status="active",
            data_mode="direct",
            public_config={},
            config_version=1,
        ),
        credentials={"token": "credential=must-not-leak"},
    )


def _connector(handler, events, *, document=None, sink=None) -> DeclarativeConnector:
    revision = import_openapi_revision(
        document or _document(), connection_id="conn-safe", tenant_id="tenant-a"
    )
    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_resolver,
        transport=httpx.MockTransport(handler),
    )
    return DeclarativeConnector._for_test(
        revision=revision,
        client=client,
        audit_sink=events.append if sink is None else sink,
    )


@pytest.mark.asyncio
async def test_step_audit_is_immutable_bounded_and_payload_free() -> None:
    secret = "credential=must-not-leak"
    events = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            {"entity_id": secret}
            if request.url.path.endswith("lookup")
            else {"display_name": secret}
        )
        return httpx.Response(
            200, json=body, headers={"X-Token": secret}, request=request
        )

    result = await _connector(handler, events).execute(
        _context(), "people_get", {"id": secret}
    )

    assert result.status == "ok"
    assert [
        (
            event.tool_key,
            event.step_id,
            event.operation_key,
            event.status,
            event.error_code,
        )
        for event in events
    ] == [
        ("people.get", "lookup", "people.lookup", "ok", ""),
        ("people.get", "details", "people.details", "ok", ""),
    ]
    assert all(0 <= event.cost_ms <= 300_000 for event in events)
    assert secret not in repr(events)
    assert "api.example.com" not in repr(events)
    with pytest.raises(FrozenInstanceError):
        events[0].status = "error"


@pytest.mark.parametrize(
    "changes",
    [
        {"connection_id": "token=unsafe"},
        {"error_code": "exception-text"},
        {"cost_ms": MAX_STEP_AUDIT_COST_MS + 1},
    ],
)
def test_step_audit_contract_rejects_unbounded_or_unsafe_values(changes) -> None:
    values = {
        "connection_id": "conn-safe",
        "tool_key": "people.get",
        "step_id": "lookup",
        "operation_key": "people.lookup",
        "status": "error",
        "error_code": "operation_error",
        "cost_ms": 0,
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError)):
        StepAuditEvent(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "document", "error_code"),
    [
        (
            lambda request: (_ for _ in ()).throw(RuntimeError("secret=transport")),
            _document(),
            "operation_error",
        ),
        (
            lambda request: httpx.Response(
                200, json={"wrong": "secret=mapping"}, request=request
            ),
            _document(),
            "mapping_error",
        ),
    ],
)
async def test_failed_step_is_audited_once_and_fail_fast(
    handler, document, error_code
) -> None:
    events = []
    result = await _connector(handler, events, document=document).execute(
        _context(), "people.get", {"id": "7"}
    )
    assert result.status == "error"
    assert len(events) == 1
    assert events[0].step_id == "lookup"
    assert events[0].status == "error"
    assert events[0].error_code == error_code
    assert "details" not in repr(events)
    assert "secret=" not in repr(events)


@pytest.mark.asyncio
async def test_step_timeout_is_audited_once_and_stops_later_steps() -> None:
    events = []

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={}, request=request)

    result = await _connector(
        handler, events, document=_document(timeout_ms=5)
    ).execute(_context(), "people.get", {"id": "7"})
    assert result.status == "error"
    assert [(event.step_id, event.error_code) for event in events] == [
        ("lookup", "timeout")
    ]


@pytest.mark.asyncio
async def test_audit_sink_failure_does_not_change_success_or_start_extra_steps() -> (
    None
):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = (
            {"entity_id": "7"}
            if request.url.path.endswith("lookup")
            else {"display_name": "Ada"}
        )
        return httpx.Response(200, json=body, request=request)

    def failing_sink(event):
        raise RuntimeError("secret=audit-sink")

    result = await _connector(handler, [], sink=failing_sink).execute(
        _context(), "people.get", {"id": "7"}
    )
    assert result.status == "ok"
    assert seen == ["/v1/lookup", "/v1/details"]


@pytest.mark.asyncio
async def test_audit_sink_failure_preserves_failed_step_fail_fast() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(503, json={"secret": "response"}, request=request)

    def failing_sink(event):
        raise RuntimeError("secret=audit-sink")

    result = await _connector(handler, [], sink=failing_sink).execute(
        _context(), "people.get", {"id": "7"}
    )
    assert result.status == "error"
    assert seen == ["/v1/lookup"]


@pytest.mark.asyncio
async def test_slow_async_sink_cannot_consume_timeouts_or_stop_later_steps(
    monkeypatch,
) -> None:
    monkeypatch.setattr(connector_module, "_MAX_TOOL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(connector_module, "_MAX_PENDING_STEP_AUDITS", 1, raising=False)
    seen = []
    sink_started = []
    release_sink = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = (
            {"entity_id": "7"}
            if request.url.path.endswith("lookup")
            else {"display_name": "Ada"}
        )
        return httpx.Response(200, json=body, request=request)

    async def stuck_sink(event) -> None:
        sink_started.append(event.step_id)
        await release_sink.wait()

    connector = _connector(
        handler,
        [],
        document=_document(timeout_ms=5),
        sink=stuck_sink,
    )
    result = await connector.execute(_context(), "people.get", {"id": "7"})
    await asyncio.sleep(0)

    assert result.status == "ok"
    assert result.data == {"name": "Ada"}
    assert seen == ["/v1/lookup", "/v1/details"]
    assert sink_started == ["lookup"]
    assert len(connector._audit_tasks) <= 1
    await connector.aclose()
    assert connector._audit_tasks == set()


@pytest.mark.asyncio
async def test_cancellation_resistant_sinks_are_globally_bounded_and_quarantined(
    monkeypatch,
) -> None:
    monkeypatch.setattr(connector_module, "_MAX_PENDING_STEP_AUDITS", 2)
    release_sink = asyncio.Event()
    sink_started = []

    async def cancellation_resistant_sink(event) -> None:
        sink_started.append(event.step_id)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_sink.wait()

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            {"entity_id": "7"}
            if request.url.path.endswith("lookup")
            else {"display_name": "Ada"}
        )
        return httpx.Response(200, json=body, request=request)

    first = _connector(handler, [], sink=cancellation_resistant_sink)
    second = _connector(handler, [], sink=cancellation_resistant_sink)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        assert (await first.execute(_context(), "people.get", {"id": "7"})).status == "ok"
        await asyncio.sleep(0)
        await first.aclose()
        await first.aclose()

        assert first._audit_tasks == set()
        assert len(connector_module._ACTIVE_STEP_AUDIT_TASKS) == 2
        assert len(connector_module._QUARANTINED_STEP_AUDIT_TASKS) == 2

        assert (await second.execute(_context(), "people.get", {"id": "7"})).status == "ok"
        await asyncio.sleep(0)
        await second.aclose()
        assert second._audit_tasks == set()
        assert len(connector_module._ACTIVE_STEP_AUDIT_TASKS) == 2
        assert sink_started == ["lookup", "details"]

        release_sink.set()
        for _ in range(10):
            if not connector_module._ACTIVE_STEP_AUDIT_TASKS:
                break
            await asyncio.sleep(0)
        gc.collect()

    assert connector_module._ACTIVE_STEP_AUDIT_TASKS == set()
    assert connector_module._QUARANTINED_STEP_AUDIT_TASKS == set()
    assert not any(
        "Task was destroyed" in str(item.message)
        or "was never awaited" in str(item.message)
        for item in captured
    )


def test_default_log_adapter_preserves_step_id_without_payload_fields(
    monkeypatch,
) -> None:
    emitted = []
    monkeypatch.setattr(connector_module, "write_event", emitted.append)
    event = StepAuditEvent(
        connection_id="conn-safe",
        tool_key="people.get",
        step_id="phase_one",
        operation_key="people.lookup",
        status="ok",
    )

    connector_module._write_step_audit(event)

    assert len(emitted) == 1
    assert emitted[0].event_name == "declarative_step.phase_one"
    assert emitted[0].target == "people.lookup"
    assert emitted[0].params_summary == "omitted"
    assert emitted[0].error_summary == ""
    assert "phase_one" in repr(emitted[0])


@pytest.mark.asyncio
async def test_external_cancellation_audits_started_step_and_propagates() -> None:
    events = []
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        _connector(handler, events).execute(_context(), "people.get", {"id": "7"})
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [(event.step_id, event.status, event.error_code) for event in events] == [
        ("lookup", "error", "cancelled")
    ]
