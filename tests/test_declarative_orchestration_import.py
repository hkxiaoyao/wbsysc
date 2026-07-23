from __future__ import annotations

from copy import deepcopy
import json

import pytest
import yaml

from app.connectors.declarative.models import (
    MAX_OPERATION_COUNT,
    MAX_TOOL_STEPS,
    DeclarativeRevision,
    SpecValidationError,
    ValueRef,
)
from app.connectors.declarative.validator import import_openapi_revision


def _operation(output_name: str) -> dict:
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
                            "properties": {output_name: {"type": "string"}},
                        }
                    }
                },
            }
        },
    }


def _document() -> dict:
    lookup = _operation("entity_id")
    lookup["operationId"] = "people.lookup"
    details = _operation("display_name")
    details["operationId"] = "people.details"
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
                    {
                        "step_id": "lookup",
                        "operation_key": "people.lookup",
                        "input_map": {"id": "$input.id"},
                        "output_mappings": {"entity_id": "entity_id"},
                    },
                    {
                        "step_id": "details",
                        "operation_key": "people.details",
                        "input_map": {"id": "$steps.lookup.entity_id"},
                        "output_mappings": {"display_name": "display_name"},
                        "timeout_ms": 9000,
                    },
                ],
                "result_map": {"name": "$steps.details.display_name"},
            }
        ],
    }


def _import(document: dict) -> DeclarativeRevision:
    return import_openapi_revision(
        document,
        spec_id="people",
        revision=2,
        tenant_id="tenant-a",
        connection_id="conn-a",
    )


def test_import_compiles_composite_tools_and_round_trips_typed_references():
    revision = _import(_document())

    assert [tool.tool_key for tool in revision.tools] == ["people.get"]
    tool = revision.tools[0]
    assert tool.steps[0].input_mappings["id"] == ValueRef(
        source="input", field="id"
    )
    assert tool.steps[1].input_mappings["id"] == ValueRef(
        source="steps", step_id="lookup", field="entity_id"
    )
    assert tool.result_map["name"] == ValueRef(
        source="steps", step_id="details", field="display_name"
    )

    stored = revision.storage_document()
    assert set(stored) == {
        "base_url",
        "allowed_hosts",
        "auth_scheme",
        "sync_spec",
        "operations",
        "tools",
    }
    assert "credentials" not in repr(stored).lower()
    restored = DeclarativeRevision.from_storage_document(
        spec_id=revision.spec_id,
        revision=revision.revision,
        tenant_id=revision.tenant_id,
        connection_id=revision.connection_id,
        status=revision.status,
        document=stored,
    )
    assert restored == revision
    assert isinstance(restored.tools[0].steps[0].input_mappings["id"], ValueRef)


def test_yaml_import_accepts_root_composite_tools():
    yaml_document = """
openapi: 3.0.3
servers:
  - url: https://api.example.com
paths:
  /lookup:
    get:
      operationId: people.lookup
      parameters:
        - {name: id, in: query, required: true, schema: {type: string}}
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: {type: object, properties: {entity_id: {type: string}}}
x-mcp-tools:
  - tool_key: people.get
    mcp_name: people.get
    description: Get person
    input_schema:
      type: object
      properties: {id: {type: string}}
      required: [id]
      additionalProperties: false
    output_schema:
      type: object
      properties: {entity_id: {type: string}}
      required: [entity_id]
      additionalProperties: false
    steps:
      - step_id: lookup
        operation_key: people.lookup
        input_map: {id: $input.id}
        output_mappings: {entity_id: entity_id}
    result_map: {entity_id: $steps.lookup.entity_id}
"""

    revision = import_openapi_revision(yaml_document)

    assert [tool.tool_key for tool in revision.tools] == ["people.get"]


@pytest.mark.parametrize(
    "inject_duplicate",
    [
        lambda text: text.replace(
            '"openapi":"3.0.3"',
            '"openapi":"3.0.3","openapi":"3.0.3"',
            1,
        ),
        lambda text: text.replace(
            '"description":"Get one person"',
            '"description":"first","description":"Get one person"',
            1,
        ),
        lambda text: text.replace(
            '"input_map":{"id":"$input.id"}',
            '"input_map":{"id":"$input.id","id":"$input.id"}',
            1,
        ),
    ],
)
def test_json_import_rejects_duplicate_keys_before_normalization(inject_duplicate):
    document = json.dumps(_document(), separators=(",", ":"))

    with pytest.raises(SpecValidationError):
        import_openapi_revision(inject_duplicate(document))


@pytest.mark.parametrize(
    "inject_duplicate",
    [
        lambda text: text.replace(
            "openapi: 3.0.3\n",
            "openapi: 3.0.3\nopenapi: 3.0.3\n",
            1,
        ),
        lambda text: text.replace(
            "  description: Get one person\n",
            "  description: first\n  description: Get one person\n",
            1,
        ),
        lambda text: text.replace(
            "    input_map:\n      id: $input.id\n",
            "    input_map:\n      id: $input.id\n      id: $input.id\n",
            1,
        ),
    ],
)
def test_yaml_import_rejects_duplicate_keys_before_normalization(inject_duplicate):
    document = yaml.safe_dump(_document(), sort_keys=False)

    with pytest.raises(SpecValidationError):
        import_openapi_revision(inject_duplicate(document))


@pytest.mark.parametrize("invalid_schema", [None, "string", []])
@pytest.mark.parametrize("schema_key", ["input_schema", "output_schema"])
def test_import_rejects_non_mapping_tool_property_schemas(
    schema_key, invalid_schema
):
    document = _document()
    property_name = "id" if schema_key == "input_schema" else "name"
    document["x-mcp-tools"][0][schema_key]["properties"][property_name] = (
        invalid_schema
    )

    with pytest.raises(SpecValidationError):
        _import(document)


def test_import_rejects_empty_public_output_schema_and_result_map():
    document = _document()
    document["x-mcp-tools"][0]["output_schema"]["properties"] = {}
    document["x-mcp-tools"][0]["output_schema"]["required"] = []
    document["x-mcp-tools"][0]["result_map"] = {}

    with pytest.raises(SpecValidationError):
        _import(document)


@pytest.mark.parametrize("invalid_result_map", [None, [], "invalid"])
def test_import_rejects_invalid_result_map_containers(invalid_result_map):
    document = _document()
    document["x-mcp-tools"][0]["result_map"] = invalid_result_map

    with pytest.raises(SpecValidationError):
        _import(document)


def test_import_preserves_valid_empty_tool_input_properties():
    document = _document()
    document["paths"]["/people/lookup"]["get"]["parameters"] = []
    tool = document["x-mcp-tools"][0]
    tool["input_schema"]["properties"] = {}
    tool["input_schema"]["required"] = []
    tool["steps"][0]["input_map"] = {}

    revision = _import(document)

    assert dict(revision.tools[0].input_schema["properties"]) == {}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.__setitem__("x-mcp-tools", None),
        lambda document: document.__setitem__("x-mcp-tools", {}),
        lambda document: document.__setitem__("x-mcp-tools", []),
        lambda document: document.__setitem__(
            "x-mcp-tools", document["x-mcp-tools"] * (MAX_OPERATION_COUNT + 1)
        ),
        lambda document: document["x-mcp-tools"][0].__setitem__("unknown", True),
        lambda document: document["x-mcp-tools"][0].pop("result_map"),
        lambda document: document["x-mcp-tools"][0].__setitem__("steps", []),
        lambda document: document["x-mcp-tools"][0].__setitem__(
            "steps",
            document["x-mcp-tools"][0]["steps"][:1] * (MAX_TOOL_STEPS + 1),
        ),
        lambda document: document["x-mcp-tools"][0]["steps"][0].__setitem__(
            "unknown", True
        ),
        lambda document: document["x-mcp-tools"][0]["steps"][0].pop("input_map"),
        lambda document: document["x-mcp-tools"][0]["steps"][0].__setitem__(
            "operation_key", "unknown.operation"
        ),
        lambda document: document["x-mcp-tools"][0]["steps"][0][
            "input_map"
        ].__setitem__("unknown", "$input.id"),
        lambda document: document["x-mcp-tools"][0]["steps"][1][
            "input_map"
        ].__setitem__("id", "$steps.details.display_name"),
        lambda document: document["x-mcp-tools"][0]["steps"][1][
            "output_mappings"
        ].__setitem__("unknown", "missing"),
        lambda document: document["x-mcp-tools"][0]["result_map"].__setitem__(
            "name", "$steps.missing.value"
        ),
    ],
)
def test_import_rejects_malformed_or_undeclared_composite_tools(mutate):
    document = _document()
    mutate(document)

    with pytest.raises(SpecValidationError):
        _import(document)


def test_import_rejects_duplicate_tool_identifiers():
    document = _document()
    document["x-mcp-tools"].append(deepcopy(document["x-mcp-tools"][0]))

    with pytest.raises(SpecValidationError):
        _import(document)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("x-mcp-tools", 0, "description"), "describe $input.id"),
        (("x-mcp-tools", 0, "input_schema", "description"), "$steps.lookup.value"),
        (("paths", "/people/lookup", "get", "summary"), "$input.id"),
        (("paths", "/people/lookup", "get", "parameters", 0, "name"), "$steps.x.y"),
        (("servers", 0, "url"), "https://$input.id.example.com"),
    ],
)
def test_import_rejects_references_in_non_reference_string_positions(path, value):
    document = _document()
    selected = document
    for part in path[:-1]:
        selected = selected[part]
    selected[path[-1]] = value

    with pytest.raises(SpecValidationError):
        _import(document)


def test_absent_extension_generates_deterministic_single_step_tools():
    document = _document()
    document.pop("x-mcp-tools")

    first = _import(document)
    second = _import(document)

    assert [tool.tool_key for tool in first.tools] == [
        "people.lookup",
        "people.details",
    ]
    assert first.tools == second.tools
    assert [step.step_id for tool in first.tools for step in tool.steps] == [
        "operation",
        "operation",
    ]


@pytest.mark.parametrize("tools", [None, [], {}, "invalid"])
def test_storage_accepts_only_true_legacy_absence_not_invalid_tools(tools):
    revision = _import(_document())
    legacy = revision.storage_document()
    legacy.pop("tools")
    restored = DeclarativeRevision.from_storage_document(
        spec_id="people",
        revision=2,
        tenant_id="tenant-a",
        connection_id="conn-a",
        status="draft",
        document=legacy,
    )
    assert [tool.tool_key for tool in restored.tools] == [
        "people.lookup",
        "people.details",
    ]
    assert "tools" in restored.storage_document()

    with pytest.raises(SpecValidationError):
        DeclarativeRevision.from_storage_document(
            spec_id="people",
            revision=2,
            tenant_id="tenant-a",
            connection_id="conn-a",
            status="draft",
            document={**legacy, "tools": tools},
        )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("operations", 0, "description"), "stored $input.id"),
        (("operations", 0, "path"), "/stored/$steps.lookup.entity_id"),
        (("tools", 0, "description"), "stored $steps.lookup.entity_id"),
        (("tools", 0, "input_schema", "description"), "$input.id"),
    ],
)
def test_storage_rejects_references_in_non_reference_string_positions(path, value):
    revision = _import(_document())
    stored = revision.storage_document()
    selected = stored
    for part in path[:-1]:
        selected = selected[part]
    selected[path[-1]] = value

    with pytest.raises(SpecValidationError):
        DeclarativeRevision.from_storage_document(
            spec_id="people",
            revision=2,
            tenant_id="tenant-a",
            connection_id="conn-a",
            status="draft",
            document=stored,
        )


@pytest.mark.parametrize(
    ("schema_key", "property_name", "invalid_schema"),
    [
        ("input_schema", "id", None),
        ("input_schema", "id", "invalid"),
        ("output_schema", "name", None),
        ("output_schema", "name", []),
    ],
)
def test_storage_rejects_non_mapping_tool_property_schemas(
    schema_key, property_name, invalid_schema
):
    revision = _import(_document())
    stored = revision.storage_document()
    stored["tools"][0][schema_key]["properties"][property_name] = invalid_schema

    with pytest.raises(SpecValidationError):
        DeclarativeRevision.from_storage_document(
            spec_id="people",
            revision=2,
            tenant_id="tenant-a",
            connection_id="conn-a",
            status="draft",
            document=stored,
        )


def test_storage_rejects_empty_public_output_schema_and_result_map():
    revision = _import(_document())
    stored = revision.storage_document()
    stored["tools"][0]["output_schema"]["properties"] = {}
    stored["tools"][0]["output_schema"]["required"] = []
    stored["tools"][0]["result_map"] = {}

    with pytest.raises(SpecValidationError):
        DeclarativeRevision.from_storage_document(
            spec_id="people",
            revision=2,
            tenant_id="tenant-a",
            connection_id="conn-a",
            status="draft",
            document=stored,
        )
