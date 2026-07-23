from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.connectors.declarative.models import (
    DeclarativeOperation,
    DeclarativeRevision,
    DeclarativeStep,
    DeclarativeTool,
    InputMapping,
    OutputMapping,
    SpecValidationError,
    ValueRef,
)


def _operation(
    tool_key: str,
    *,
    operation_kind: str = "read",
    timeout_ms: int = 10_000,
) -> DeclarativeOperation:
    return DeclarativeOperation(
        tool_key=tool_key,
        mcp_name=tool_key,
        description=tool_key,
        method="GET" if operation_kind == "read" else "POST",
        path=f"/{tool_key}",
        input_mappings=(
            InputMapping(
                arg_name="id",
                location="query",
                target="id",
                required=True,
                schema={"type": "string"},
            ),
        ),
        output_mappings=(
            OutputMapping(name="id", pointer="/id"),
            OutputMapping(name="name", pointer="/name"),
        ),
        operation_kind=operation_kind,  # type: ignore[arg-type]
        explicit_write_enabled=operation_kind == "write",
        base_url="https://api.example.com",
        timeout_ms=timeout_ms,
    )


def _tool(*, second_ref: str = "$steps.lookup.entity_id") -> DeclarativeTool:
    return DeclarativeTool(
        tool_key="people.resolve",
        mcp_name="people.resolve",
        description="Resolve a person",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        },
        steps=(
            DeclarativeStep(
                step_id="lookup",
                operation_key="people.get",
                input_mappings={"id": ValueRef.parse("$input.id")},
                output_mappings={"entity_id": "id"},
                timeout_ms=4_000,
            ),
            DeclarativeStep(
                step_id="details",
                operation_key="people.details",
                input_mappings={"id": ValueRef.parse(second_ref)},
                output_mappings={"display_name": "name"},
                timeout_ms=5_000,
            ),
        ),
        result_map={"name": ValueRef.parse("$steps.details.display_name")},
    )


def _revision(*, tools: tuple[DeclarativeTool, ...]) -> DeclarativeRevision:
    return DeclarativeRevision(
        spec_id="people",
        tenant_id="tenant-a",
        connection_id="connection-a",
        status="published",
        base_url="https://api.example.com",
        allowed_hosts=("api.example.com",),
        operations=(_operation("people.get"), _operation("people.details")),
        tools=tools,
    )


@pytest.mark.parametrize(
    ("raw", "source", "step_id", "field"),
    [
        ("$input.customer_id", "input", None, "customer_id"),
        ("$steps.lookup.customer_id", "steps", "lookup", "customer_id"),
    ],
)
def test_value_ref_accepts_only_closed_input_and_step_forms(raw, source, step_id, field):
    ref = ValueRef.parse(raw)
    assert (ref.source, ref.step_id, ref.field) == (source, step_id, field)
    assert str(ref) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "$input",
        "$input.",
        "$steps.lookup",
        "$steps.lookup.",
        "$steps..id",
        "$steps[0].id",
        "$.input.id",
        "${input.id}",
        "{{ input.id }}",
        "$input.id + 1",
        "lambda: 1",
        "plain",
        "$input.字段",
        f"$input.{'a' * 129}",
        f"$steps.{'a' * 65}.id",
    ],
)
def test_value_ref_rejects_templates_expressions_jsonpath_and_incomplete_refs(raw):
    with pytest.raises(SpecValidationError):
        ValueRef.parse(raw)


def test_tool_and_nested_contracts_are_immutable():
    tool = _tool()
    with pytest.raises(FrozenInstanceError):
        tool.description = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        tool.input_schema["properties"]["other"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        tool.steps[0].input_mappings["other"] = ValueRef.parse("$input.id")  # type: ignore[index]


@pytest.mark.parametrize(
    "tool",
    [
        lambda: _tool(second_ref="$steps.details.display_name"),
        lambda: _tool(second_ref="$steps.missing.id"),
        lambda: _tool(second_ref="$steps.lookup.missing"),
    ],
)
def test_tool_rejects_self_forward_unknown_step_outputs(tool):
    with pytest.raises(SpecValidationError):
        tool()


def test_revision_validates_operations_inputs_outputs_and_timeout_budget():
    valid = _revision(tools=(_tool(),))
    assert valid.tool_for("people.resolve") is valid.tools[0]
    assert valid.connector_spec().tool("people.resolve").default_timeout_ms == 9_000

    missing_operation = _tool()
    object.__setattr__(missing_operation.steps[1], "operation_key", "people.unknown")
    with pytest.raises(SpecValidationError, match="operation"):
        _revision(tools=(missing_operation,))

    bad_input = _tool()
    object.__setattr__(bad_input.steps[0], "input_mappings", {"unknown": ValueRef.parse("$input.id")})
    with pytest.raises(SpecValidationError, match="input"):
        _revision(tools=(bad_input,))

    bad_output = _tool()
    object.__setattr__(bad_output.steps[0], "output_mappings", {"entity_id": "unknown"})
    with pytest.raises(SpecValidationError, match="output"):
        _revision(tools=(bad_output,))

    over_budget = _tool()
    object.__setattr__(over_budget.steps[0], "timeout_ms", 30_001)
    object.__setattr__(over_budget.steps[1], "timeout_ms", 30_000)
    with pytest.raises(SpecValidationError, match="timeout"):
        _revision(tools=(over_budget,))


def test_revision_rejects_more_than_one_write_step():
    with pytest.raises(SpecValidationError, match="write"):
        DeclarativeRevision(
            spec_id="people",
            tenant_id="tenant-a",
            connection_id="connection-a",
            status="published",
            base_url="https://api.example.com",
            allowed_hosts=("api.example.com",),
            operations=(
                _operation("people.get", operation_kind="write"),
                _operation("people.details", operation_kind="write"),
            ),
            tools=(_tool(),),
        )


def test_legacy_revision_synthesizes_single_operation_tools_and_round_trips_storage():
    operation = _operation("people.get")
    legacy = DeclarativeRevision(
        spec_id="people",
        tenant_id="tenant-a",
        connection_id="connection-a",
        status="published",
        base_url="https://api.example.com",
        allowed_hosts=("api.example.com",),
        operations=(operation,),
    )
    assert len(legacy.tools) == 1
    assert legacy.tool_for("people.get").steps[0].operation_key == "people.get"
    assert legacy.connector_spec().tool("people.get").input_schema == operation.input_schema

    restored = DeclarativeRevision.from_storage_document(
        spec_id=legacy.spec_id,
        revision=legacy.revision,
        tenant_id=legacy.tenant_id,
        connection_id=legacy.connection_id,
        status=legacy.status,
        document=legacy.storage_document(),
    )
    assert restored == legacy


def test_storage_rejects_present_invalid_tools_but_accepts_absent_legacy_key():
    operation = _operation("people.get")
    revision = DeclarativeRevision(
        spec_id="people",
        tenant_id="tenant-a",
        connection_id="connection-a",
        status="published",
        base_url="https://api.example.com",
        allowed_hosts=("api.example.com",),
        operations=(operation,),
    )
    legacy_document = revision.storage_document()
    legacy_document.pop("tools")

    restored = DeclarativeRevision.from_storage_document(
        spec_id=revision.spec_id,
        revision=revision.revision,
        tenant_id=revision.tenant_id,
        connection_id=revision.connection_id,
        status=revision.status,
        document=legacy_document,
    )

    assert restored.tools == revision.tools
    assert restored.tools[0].steps[0].step_id == "operation"

    for invalid_tools in (None, {}, [], "", 1, False):
        with pytest.raises(SpecValidationError):
            DeclarativeRevision.from_storage_document(
                spec_id=revision.spec_id,
                revision=revision.revision,
                tenant_id=revision.tenant_id,
                connection_id=revision.connection_id,
                status=revision.status,
                document={**legacy_document, "tools": invalid_tools},
            )


def test_explicit_multi_step_tool_round_trips_credential_free_storage():
    revision = _revision(tools=(_tool(),))
    document = revision.storage_document()
    assert "credential" not in repr(document).lower()

    restored = DeclarativeRevision.from_storage_document(
        spec_id=revision.spec_id,
        revision=revision.revision,
        tenant_id=revision.tenant_id,
        connection_id=revision.connection_id,
        status=revision.status,
        document=document,
    )
    assert restored == revision
