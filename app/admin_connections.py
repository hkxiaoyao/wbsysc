"""Authenticated, tenant-scoped administration for MCP connection instances."""
from __future__ import annotations

import hashlib
import inspect
import logging
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from . import admin
from .auth import ConnectionCtx
from .connections import store
from .connections.models import ConnectionRecord, ToolPolicy
from .connectors.contracts import ConnectionContext, ConnectorSpec
from .connectors.declarative.provider import DECLARATIVE_CONNECTOR_KEY
from .connectors.declarative.models import (
    MAX_MAPPING_DEPTH,
    MAX_OPERATION_COUNT,
    MAX_OUTPUT_MAPPINGS,
    SpecValidationError,
)
from .connectors.declarative.validator import import_openapi_revision, validate_revision
from .db import ensure_schema
from .mcp_audit import write_event
from .mcp_log_models import McpLogEvent
from .mcp_services import store as service_store


logger = logging.getLogger(__name__)
WECOM_CONNECTOR_KEY = "wecom"
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}
_SENSITIVE_PARTS = frozenset(
    {"authorization", "cookie", "credential", "password", "secret", "token"}
)
_PREVIEW_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)
_PREVIEW_SCHEMA_NONNEGATIVE_INTEGER_KEYWORDS = (
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
)
_PREVIEW_SCHEMA_NUMBER_KEYWORDS = (
    "minimum",
    "maximum",
)
_PREVIEW_SCHEMA_EXCLUSIVE_BOUND_KEYWORDS = (
    "exclusiveMinimum",
    "exclusiveMaximum",
)
_PREVIEW_SCHEMA_MAX_NODES = 2_048
_PREVIEW_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b",
        r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}\b",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b",
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        r"\b(?:api[\s_-]*key|access[\s_-]*token|authorization|client[\s_-]*secret|password|secret|token)\s*[:=]\s*[^\s,;]{4,}",
    )
)
_BEARER_VALUE_RE = re.compile(r"\bbearer\s+([^\s,;]+)", re.IGNORECASE)
_CREDENTIAL_CANDIDATE_RE = re.compile(r"[A-Za-z0-9_+=/-]{24,}")
_PREVIEW_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


def _require_admin(request: Request) -> None:
    admin._require_auth(request)


router = APIRouter(
    prefix="/admin",
    tags=["admin-connections"],
    dependencies=[Depends(_require_admin)],
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionCreateRequest(_StrictModel):
    connector_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=128)
    data_mode: Literal["direct", "stored", "hybrid"]
    public_config: Any = Field(default_factory=dict)
    credentials: Any = Field(default_factory=dict)
    status: Literal["draft", "active"] = "active"


class ConnectionUpdateRequest(_StrictModel):
    display_name: str = Field(min_length=1, max_length=128)
    data_mode: Literal["direct", "stored", "hybrid"]
    public_config: Any = Field(default_factory=dict)
    status: Literal["draft", "active", "disabled", "error"] | None = None


class CredentialReplaceRequest(_StrictModel):
    credentials: Any


class TokenIssueRequest(_StrictModel):
    label: str = Field(default="", max_length=128)


def _wecom_schema_name(connection_id: str) -> str:
    digest = hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:12]
    return f"wbd_{digest}"


def _legacy_wecom_connection_id(tenant_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"wbsysc:legacy-wecom:{tenant_id}"))


def _connection_public_config(
    connector_key: str,
    connection_id: str,
    value: Mapping[str, Any],
    *,
    tenant_id: str = "",
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = dict(value)
    if connector_key != WECOM_CONNECTOR_KEY:
        return config
    current_config = current or {}
    current_schema = current_config.get("schema_name")
    is_verified_legacy = (
        bool(tenant_id)
        and connection_id == _legacy_wecom_connection_id(tenant_id)
        and current_config.get("legacy_source") == "tenant_config"
        and isinstance(current_schema, str)
        and bool(current_schema)
    )
    if is_verified_legacy:
        config["schema_name"] = current_schema
        config["legacy_source"] = "tenant_config"
    else:
        config["schema_name"] = _wecom_schema_name(connection_id)
        config.pop("legacy_source", None)
    return config


class PolicyInput(_StrictModel):
    tool_key: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    allow_write: bool = False
    timeout_ms: int | None = Field(default=None, ge=1, le=300_000)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=60_000)


class ToolPoliciesRequest(_StrictModel):
    policies: list[PolicyInput] = Field(max_length=512)


class OpenApiImportRequest(_StrictModel):
    document: Any
    spec_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    revision: int = Field(ge=1)
    allowed_hosts: list[str] | None = None
    sync_spec: dict[str, Any] | None = None


def _registry(request: Request):
    registry = getattr(request.app.state, "connector_registry", None)
    if registry is None:
        raise HTTPException(503, "connector registry unavailable")
    return registry


def _spec(request: Request, connector_key: str) -> ConnectorSpec:
    spec = _registry(request).validated_spec(connector_key)
    if not isinstance(spec, ConnectorSpec):
        raise HTTPException(422, "invalid connector configuration")
    return spec


def _spec_for_record(request: Request, record: ConnectionRecord) -> ConnectorSpec:
    if record.connector_key != DECLARATIVE_CONNECTOR_KEY:
        return _spec(request, record.connector_key)
    try:
        runtime = request.app.state.mcp_gateway._runtime
        spec = runtime._connector_resolver.spec_for(
            ConnectionContext(connection=record)
        )
    except Exception:
        raise HTTPException(409, "declarative revision is unavailable") from None
    if not isinstance(spec, ConnectorSpec):
        raise HTTPException(409, "declarative revision is unavailable")
    return spec


def _declarative_candidate_spec(
    request: Request,
    record: ConnectionRecord,
    spec_id: str,
    revision: int,
) -> ConnectorSpec:
    candidate_config = dict(record.public_config)
    candidate_config.update({"spec_id": spec_id, "revision": revision})
    candidate = replace(record, public_config=candidate_config)
    return _spec_for_record(request, candidate)


def _credential_spec_for_record(
    request: Request, record: ConnectionRecord
) -> ConnectorSpec:
    return _management_spec_for_record(request, record)


def _pending_revision_identity(
    record: ConnectionRecord,
) -> tuple[str, int] | None:
    if record.connector_key != DECLARATIVE_CONNECTOR_KEY or record.status == "active":
        return None
    spec_id = record.public_config.get("pending_spec_id")
    revision = record.public_config.get("pending_revision")
    if not isinstance(spec_id, str) or not spec_id:
        spec_id = record.public_config.get("spec_id")
        revision = record.public_config.get("revision")
    if (
        isinstance(spec_id, str)
        and spec_id
        and isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision > 0
    ):
        return spec_id, revision
    return None


def _management_record(record: ConnectionRecord) -> ConnectionRecord:
    pending = _pending_revision_identity(record)
    if pending is None:
        return record
    public_config = dict(record.public_config)
    public_config.update({"spec_id": pending[0], "revision": pending[1]})
    return replace(record, public_config=public_config)


def _management_spec_for_record(
    request: Request, record: ConnectionRecord
) -> ConnectorSpec:
    pending = _pending_revision_identity(record)
    if pending is None:
        return _spec_for_record(request, record)
    return _declarative_candidate_spec(request, record, *pending)


def _load_connection_credentials(
    request: Request, record: ConnectionRecord
) -> dict[str, Any]:
    try:
        context = request.app.state.mcp_gateway.resolver.execution_context(
            ConnectionCtx(
                tenant_id=record.tenant_id,
                connection_id=record.connection_id,
                connector_key=record.connector_key,
                data_mode=record.data_mode,
                public_config=record.public_config,
                config_version=record.config_version,
            )
        )
        return dict(context.credentials)
    except Exception:
        raise HTTPException(409, "connection credentials unavailable") from None


def _schema_error() -> None:
    raise HTTPException(422, "invalid connector configuration")


def _validate_schema(value: Any, schema: Mapping[str, Any]) -> None:
    """Validate the bounded JSON-Schema subset accepted by connector manifests."""
    if not schema:
        return
    if "enum" in schema and value not in schema["enum"]:
        _schema_error()
    expected = schema.get("type")
    valid = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if isinstance(expected, list):
        if not any(valid.get(item, False) for item in expected):
            _schema_error()
    elif expected and not valid.get(expected, False):
        _schema_error()
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, Mapping) or not isinstance(required, (list, tuple)):
            _schema_error()
        if any(key not in value for key in required):
            _schema_error()
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            _schema_error()
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, Mapping):
                _validate_schema(item, child)
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)) or len(value) > int(
            schema.get("maxItems", 10_000)
        ):
            _schema_error()
        child = schema.get("items")
        if isinstance(child, Mapping):
            for item in value:
                _validate_schema(item, child)
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)) or len(value) > int(
            schema.get("maxLength", 1_000_000)
        ):
            _schema_error()
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            _schema_error()


def _credentials(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, list):
        normalized = {}
        for item in value:
            if not isinstance(item, Mapping) or set(item) != {"key", "value"}:
                _schema_error()
            normalized[item["key"]] = item["value"]
        items = normalized.items()
    else:
        _schema_error()
        raise AssertionError
    result: dict[str, str] = {}
    for key, item in items:
        if not isinstance(key, str) or not key or not isinstance(item, str):
            _schema_error()
        result[key] = item
    return result


def _is_sensitive(name: str, schema: Mapping[str, Any] | None = None) -> bool:
    parts = {part for part in re.split(r"[^a-z0-9]+", name.lower()) if part}
    declaration = schema or {}
    return bool(parts & _SENSITIVE_PARTS) or declaration.get("writeOnly") is True or declaration.get("x-sensitive") is True or declaration.get("format") == "password"


def _safe_config(value: Any, schema: Mapping[str, Any]) -> Any:
    """Project only fields explicitly covered by a non-sensitive schema."""
    if not isinstance(value, Mapping) or not isinstance(schema, Mapping):
        return None

    def project(item: Any, declaration: Mapping[str, Any], name: str = "") -> Any:
        if name and _is_sensitive(name, declaration):
            return _OMIT
        expected = declaration.get("type")
        expected_types = expected if isinstance(expected, list) else [expected]
        matches = {
            "object": isinstance(item, Mapping),
            "array": isinstance(item, list),
            "string": isinstance(item, str),
            "integer": isinstance(item, int) and not isinstance(item, bool),
            "number": isinstance(item, (int, float)) and not isinstance(item, bool),
            "boolean": isinstance(item, bool),
            "null": item is None,
        }
        if not any(matches.get(schema_type, False) for schema_type in expected_types):
            return _OMIT
        if expected == "object" or isinstance(item, Mapping):
            if not isinstance(item, Mapping):
                return _OMIT
            properties = declaration.get("properties", {})
            if not isinstance(properties, Mapping):
                return _OMIT
            additional = declaration.get("additionalProperties", False)
            result: dict[str, Any] = {}
            for key, child_value in item.items():
                child_schema = properties.get(key)
                if not isinstance(child_schema, Mapping):
                    child_schema = additional if isinstance(additional, Mapping) else None
                if not isinstance(child_schema, Mapping):
                    continue
                child = project(child_value, child_schema, str(key))
                if child is not _OMIT:
                    result[str(key)] = child
            return result
        if expected == "array" or isinstance(item, list):
            if not isinstance(item, list):
                return _OMIT
            child_schema = declaration.get("items")
            if not isinstance(child_schema, Mapping):
                return []
            return [
                child
                for value in item
                if (child := project(value, child_schema)) is not _OMIT
            ]
        return item

    projected = project(value, schema)
    return {} if projected is _OMIT else projected


def _plain_schema_metadata(value: Any) -> Any:
    """Detach validated schema metadata for JSON responses without values/secrets."""
    if isinstance(value, Mapping):
        return {
            str(key): _plain_schema_metadata(item)
            for key, item in value.items()
            if key not in {"const", "default", "enum", "example", "examples"}
            and not str(key).lower().startswith("x-")
        }
    if isinstance(value, (list, tuple)):
        return [_plain_schema_metadata(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise HTTPException(409, "connector schema is unavailable")


def _credential_shape(value: str, *, minimum: int = 12) -> bool:
    if len(value) < minimum:
        return False
    classes = sum(
        (
            any(character.islower() for character in value),
            any(character.isupper() for character in value),
            any(character.isdigit() for character in value),
            any(not character.isalnum() for character in value),
        )
    )
    if classes >= 3:
        return True
    return (
        any(character.isalpha() for character in value)
        and any(character.isdigit() for character in value)
    ) or (classes >= 2 and any(not character.isalnum() for character in value))


def _high_entropy_credential(value: str) -> bool:
    if not _credential_shape(value, minimum=24) or len(set(value)) < 10:
        return False
    frequencies = (value.count(character) / len(value) for character in set(value))
    entropy = -sum(frequency * math.log2(frequency) for frequency in frequencies)
    return entropy >= 3.5


def _unsafe_preview_description(value: str) -> bool:
    if len(value) > 512 or "://" in value or any(
        character in value for character in "\r\n\0"
    ):
        return True
    if any(pattern.search(value) is not None for pattern in _PREVIEW_SECRET_PATTERNS):
        return True
    bearer = _BEARER_VALUE_RE.search(value)
    if bearer is not None and _credential_shape(bearer.group(1), minimum=8):
        return True
    return any(
        _high_entropy_credential(match.group(0))
        for match in _CREDENTIAL_CANDIDATE_RE.finditer(value)
    )


def _safe_preview_description(value: Any) -> str:
    if not isinstance(value, str) or _unsafe_preview_description(value):
        return ""
    return value


def _safe_preview_schema(value: Any) -> dict[str, Any]:
    """Return a bounded structural subset of a compiled JSON schema."""

    def unavailable() -> None:
        raise HTTPException(409, "declarative revision is unavailable")

    active: set[int] = set()
    nodes = [0]

    def project(schema: Any, *, depth: int) -> dict[str, Any]:
        if not isinstance(schema, Mapping) or depth > MAX_MAPPING_DEPTH:
            unavailable()
        identity = id(schema)
        if identity in active:
            unavailable()
        nodes[0] += 1
        if nodes[0] > _PREVIEW_SCHEMA_MAX_NODES:
            unavailable()
        active.add(identity)
        try:
            result: dict[str, Any] = {}
            schema_type = schema.get("type", _OMIT)
            if schema_type is not _OMIT:
                if (
                    not isinstance(schema_type, str)
                    or schema_type not in _PREVIEW_SCHEMA_TYPES
                ):
                    unavailable()
                result["type"] = schema_type

            for keyword in _PREVIEW_SCHEMA_NONNEGATIVE_INTEGER_KEYWORDS:
                item = schema.get(keyword, _OMIT)
                if item is _OMIT:
                    continue
                if (
                    not isinstance(item, int)
                    or isinstance(item, bool)
                    or item < 0
                ):
                    unavailable()
                result[keyword] = item

            for keyword in _PREVIEW_SCHEMA_NUMBER_KEYWORDS:
                item = schema.get(keyword, _OMIT)
                if item is _OMIT:
                    continue
                if (
                    not isinstance(item, (int, float))
                    or isinstance(item, bool)
                    or not math.isfinite(item)
                ):
                    unavailable()
                result[keyword] = item

            for keyword in _PREVIEW_SCHEMA_EXCLUSIVE_BOUND_KEYWORDS:
                item = schema.get(keyword, _OMIT)
                if item is _OMIT:
                    continue
                if isinstance(item, bool):
                    result[keyword] = item
                elif isinstance(item, (int, float)) and math.isfinite(item):
                    result[keyword] = item
                else:
                    unavailable()

            unique_items = schema.get("uniqueItems", _OMIT)
            if unique_items is not _OMIT:
                if not isinstance(unique_items, bool):
                    unavailable()
                result["uniqueItems"] = unique_items

            properties = schema.get("properties", _OMIT)
            safe_property_names: set[str] = set()
            if properties is not _OMIT:
                if not isinstance(properties, Mapping):
                    unavailable()
                projected_properties = {}
                for name, child_schema in properties.items():
                    if (
                        not isinstance(name, str)
                        or _PREVIEW_IDENTIFIER_RE.fullmatch(name) is None
                        or not isinstance(child_schema, Mapping)
                    ):
                        unavailable()
                    projected_properties[name] = project(
                        child_schema, depth=depth + 1
                    )
                    safe_property_names.add(name)
                result["properties"] = projected_properties

            required = schema.get("required", _OMIT)
            if required is not _OMIT:
                if not isinstance(required, (list, tuple)):
                    unavailable()
                required_names = []
                seen_required = set()
                for name in required:
                    if (
                        not isinstance(name, str)
                        or name not in safe_property_names
                        or name in seen_required
                    ):
                        unavailable()
                    seen_required.add(name)
                    required_names.append(name)
                result["required"] = required_names

            items = schema.get("items", _OMIT)
            if items is not _OMIT:
                if not isinstance(items, Mapping):
                    unavailable()
                result["items"] = project(items, depth=depth + 1)

            additional = schema.get("additionalProperties", _OMIT)
            if additional is not _OMIT:
                if isinstance(additional, bool):
                    result["additionalProperties"] = additional
                elif isinstance(additional, Mapping):
                    result["additionalProperties"] = project(
                        additional, depth=depth + 1
                    )
                else:
                    unavailable()

            for minimum_key, maximum_key in (
                ("minLength", "maxLength"),
                ("minItems", "maxItems"),
                ("minProperties", "maxProperties"),
                ("minimum", "maximum"),
            ):
                if (
                    minimum_key in result
                    and maximum_key in result
                    and result[minimum_key] > result[maximum_key]
                ):
                    unavailable()
            return result
        finally:
            active.discard(identity)

    try:
        return project(value, depth=0)
    except HTTPException:
        raise
    except (SpecValidationError, TypeError, ValueError, RecursionError, OverflowError):
        unavailable()
        raise AssertionError  # pragma: no cover


def _declarative_preview(revision: Any) -> dict[str, Any]:
    """Project only compiled tool metadata; transport and auth data stay absent."""
    compiled_operations = getattr(revision, "operations", _OMIT)
    if (
        not isinstance(compiled_operations, (list, tuple))
        or not compiled_operations
        or len(compiled_operations) > MAX_OPERATION_COUNT
    ):
        raise HTTPException(409, "declarative revision is unavailable")
    operations = {}
    operation_identities = set()
    operation_catalog = []
    for compiled_operation in compiled_operations:
        try:
            input_mappings = tuple(
                replace(mapping)
                for mapping in compiled_operation.input_mappings
            )
            output_mappings = tuple(
                replace(mapping)
                for mapping in compiled_operation.output_mappings
            )
            operation = replace(
                compiled_operation,
                input_mappings=input_mappings,
                output_mappings=output_mappings,
            )
        except (
            AttributeError,
            SpecValidationError,
            TypeError,
            ValueError,
            RecursionError,
        ):
            raise HTTPException(
                409, "declarative revision is unavailable"
            ) from None
        operation_key = getattr(operation, "tool_key", None)
        mcp_name = getattr(operation, "mcp_name", None)
        operation_kind = getattr(operation, "operation_kind", None)
        identities = {operation_key, mcp_name}
        if (
            not isinstance(operation_key, str)
            or _PREVIEW_IDENTIFIER_RE.fullmatch(operation_key) is None
            or not isinstance(mcp_name, str)
            or _PREVIEW_IDENTIFIER_RE.fullmatch(mcp_name) is None
            or bool(operation_identities & identities)
            or operation_kind not in {"read", "write"}
            or not isinstance(output_mappings, (list, tuple))
            or not output_mappings
            or len(output_mappings) > MAX_OUTPUT_MAPPINGS
            or not isinstance(getattr(operation, "description", None), str)
        ):
            raise HTTPException(409, "declarative revision is unavailable")
        operation_identities.update(identities)
        output_names = []
        seen_output_names = set()
        for mapping in output_mappings:
            name = getattr(mapping, "name", None)
            pointer = getattr(mapping, "pointer", None)
            if (
                not isinstance(name, str)
                or _PREVIEW_IDENTIFIER_RE.fullmatch(name) is None
                or name in seen_output_names
                or not isinstance(pointer, str)
                or not pointer.startswith("/")
            ):
                raise HTTPException(409, "declarative revision is unavailable")
            seen_output_names.add(name)
            output_names.append(name)
        operations[operation_key] = operation
        try:
            input_schema = _safe_preview_schema(operation.input_schema)
        except (AttributeError, TypeError, HTTPException):
            raise HTTPException(409, "declarative revision is unavailable") from None
        operation_catalog.append(
            {
                "operation_key": operation_key,
                "mcp_name": mcp_name,
                "description": _safe_preview_description(
                    getattr(operation, "description", None)
                ),
                "operation_kind": operation_kind,
                "input_schema": input_schema,
                "output_names": output_names,
            }
        )
    tools = []
    for tool in getattr(revision, "tools", ()):
        steps = []
        operation_kinds = []
        for step in tool.steps:
            operation = operations.get(step.operation_key)
            if operation is None:
                raise HTTPException(409, "declarative revision is unavailable")
            operation_kinds.append(operation.operation_kind)
            steps.append(
                {
                    "step_id": step.step_id,
                    "operation_key": operation.tool_key,
                    "operation_kind": operation.operation_kind,
                }
            )
        tools.append(
            {
                "tool_key": tool.tool_key,
                "mcp_name": tool.mcp_name,
                "description": _safe_preview_description(tool.description),
                "input_schema": _safe_preview_schema(tool.input_schema),
                "output_schema": _safe_preview_schema(tool.output_schema),
                "operation_kind": (
                    "write" if "write" in operation_kinds else "read"
                ),
                "steps": steps,
            }
        )
    return {"tools": tools, "operations": operation_catalog}


_OMIT = object()


def _safe_connection(record: ConnectionRecord, spec: ConnectorSpec) -> dict[str, Any]:
    return {
        "connection_id": record.connection_id,
        "connection_alias": record.connection_alias,
        "tenant_id": record.tenant_id,
        "connector_key": record.connector_key,
        "display_name": record.display_name,
        "status": record.status,
        "data_mode": record.data_mode,
        "public_config": _safe_config(record.public_config, spec.config_schema),
        "config_version": record.config_version,
    }


def _safe_connection_for_request(request: Request, record: ConnectionRecord) -> dict[str, Any]:
    if record.connector_key == DECLARATIVE_CONNECTOR_KEY:
        try:
            spec = _spec_for_record(request, record)
        except HTTPException:
            return {
                "connection_id": record.connection_id,
                "connection_alias": record.connection_alias,
                "tenant_id": record.tenant_id,
                "connector_key": record.connector_key,
                "display_name": record.display_name,
                "status": record.status,
                "data_mode": record.data_mode,
                "public_config": {},
                "config_version": record.config_version,
            }
    else:
        spec = _spec(request, record.connector_key)
    return _safe_connection(record, spec)


def _owned(tenant_id: str, connection_id: str) -> ConnectionRecord:
    record = store.get_connection(connection_id, tenant_id)
    if record is None:
        raise HTTPException(404, "connection not found")
    return record


def _require_declarative(record: ConnectionRecord) -> None:
    if record.connector_key != DECLARATIVE_CONNECTOR_KEY:
        raise HTTPException(422, "invalid declarative connection")


def _audit(record: ConnectionRecord, event_name: str, *, status: str = "ok") -> None:
    try:
        write_event(
            McpLogEvent(
                tenant_id=record.tenant_id,
                connection_id=record.connection_id,
                connector_key=record.connector_key,
                category="protocol",
                event_name=event_name,
                target="",
                params_summary="omitted",
                result_status=status,  # type: ignore[arg-type]
            )
        )
    except Exception as exc:
        logger.warning("Connection management audit failed type=%s", type(exc).__name__)


def _mutate(operation, *args, **kwargs):
    """Execute a store mutation without exposing its inputs or exception text."""
    try:
        return operation(*args, **kwargs)
    except store.ConnectionVersionConflictError:
        raise HTTPException(409, "connection configuration changed") from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Connection mutation failed type=%s", type(exc).__name__)
        raise HTTPException(400, "connection mutation failed") from None


def list_connections_use_case(tenant_id: str, request: Request):
    items = []
    for record in store.list_connections(tenant_id):
        items.append(_safe_connection_for_request(request, record))
    return {"items": items}


@router.get("/tenants/{tenant_id}/connections")
def list_connections(tenant_id: str, request: Request):
    return list_connections_use_case(tenant_id, request)


def create_connection_use_case(
    tenant_id: str, body: ConnectionCreateRequest, request: Request
):
    if not isinstance(body.public_config, Mapping):
        _schema_error()
    credentials = _credentials(body.credentials)
    connection_id = str(uuid.uuid4())
    public_config = _connection_public_config(
        body.connector_key,
        connection_id,
        body.public_config,
        tenant_id=tenant_id,
    )
    if body.connector_key == DECLARATIVE_CONNECTOR_KEY:
        if body.status != "draft" or public_config or credentials:
            _schema_error()
        spec = None
    else:
        spec = _spec(request, body.connector_key)
        if body.data_mode not in spec.supports_data_modes:
            _schema_error()
        _validate_schema(public_config, spec.config_schema)
        _validate_schema(credentials, spec.credential_schema)
    if body.connector_key == WECOM_CONNECTOR_KEY:
        ensure_schema(public_config["schema_name"])
    record = ConnectionRecord(
        connection_id=connection_id,
        tenant_id=tenant_id,
        connector_key=body.connector_key,
        display_name=body.display_name,
        status=body.status,
        data_mode=body.data_mode,
        public_config=public_config,
        config_version=1,
    )
    try:
        created, issued = store.create_connection_with_token(record, credentials)
    except Exception as exc:
        logger.warning("Connection create failed type=%s", type(exc).__name__)
        raise HTTPException(400, "connection mutation failed") from None
    _audit(created, "connection_created")
    return {
        "connection": (
            _safe_connection(created, spec)
            if spec is not None
            else _safe_connection_for_request(request, created)
        ),
        "initial_token": issued.raw_value,
        "token_prefix": issued.prefix,
    }


@router.post("/tenants/{tenant_id}/connections", status_code=201)
def create_connection(
    tenant_id: str,
    body: ConnectionCreateRequest,
    request: Request,
    response: Response,
):
    response.headers.update(_NO_STORE_HEADERS)
    return create_connection_use_case(tenant_id, body, request)


def get_connection_use_case(tenant_id: str, connection_id: str, request: Request):
    record = _owned(tenant_id, connection_id)
    return {
        "connection": _safe_connection_for_request(request, record),
        "tokens": store.list_connection_tokens(connection_id),
    }


@router.get("/tenants/{tenant_id}/connections/{connection_id}")
def get_connection(tenant_id: str, connection_id: str, request: Request):
    return get_connection_use_case(tenant_id, connection_id, request)


@router.get("/connections/{connection_id}")
def get_connection_global(connection_id: str, request: Request):
    record = store.get_connection(connection_id)
    if record is None:
        raise HTTPException(404, "connection not found")
    return get_connection_use_case(record.tenant_id, connection_id, request)


def _global_owner(connection_id: str) -> ConnectionRecord:
    record = store.get_connection(connection_id)
    if record is None:
        raise HTTPException(404, "connection not found")
    return record


def update_connection_use_case(
    tenant_id: str,
    connection_id: str,
    body: ConnectionUpdateRequest,
    request: Request,
):
    current = _owned(tenant_id, connection_id)
    if current.connector_key == DECLARATIVE_CONNECTOR_KEY:
        if body.status == "active":
            raise HTTPException(409, "use declarative revision activation")
        if dict(body.public_config) != current.public_config:
            _schema_error()
        candidate = replace(current, data_mode=body.data_mode)
        spec = _spec_for_record(request, candidate)
    else:
        spec = _spec(request, current.connector_key)
    if body.data_mode not in spec.supports_data_modes or not isinstance(body.public_config, Mapping):
        _schema_error()
    public_config = _connection_public_config(
        current.connector_key,
        connection_id,
        body.public_config,
        tenant_id=tenant_id,
        current=current.public_config,
    )
    _validate_schema(public_config, spec.config_schema)
    if current.connector_key == WECOM_CONNECTOR_KEY:
        ensure_schema(public_config["schema_name"])
    updated = _mutate(store.update_connection,
        connection_id,
        tenant_id,
        display_name=body.display_name,
        data_mode=body.data_mode,
        public_config=public_config,
        status=body.status,
        expected_config_version=current.config_version,
    )
    if updated is None:
        raise HTTPException(404, "connection not found")
    _audit(updated, "connection_updated")
    return {"connection": _safe_connection_for_request(request, updated)}


@router.put("/tenants/{tenant_id}/connections/{connection_id}")
def update_connection(
    tenant_id: str,
    connection_id: str,
    body: ConnectionUpdateRequest,
    request: Request,
):
    return update_connection_use_case(tenant_id, connection_id, body, request)


@router.put("/connections/{connection_id}")
def update_connection_global(connection_id: str, body: ConnectionUpdateRequest, request: Request):
    return update_connection_use_case(
        _global_owner(connection_id).tenant_id, connection_id, body, request
    )


def disable_connection_use_case(tenant_id: str, connection_id: str, request: Request):
    _owned(tenant_id, connection_id)
    record = _mutate(store.disable_connection, connection_id, tenant_id)
    if record is None:
        raise HTTPException(404, "connection not found")
    _audit(record, "connection_disabled")
    return {"connection": _safe_connection_for_request(request, record)}


@router.post("/tenants/{tenant_id}/connections/{connection_id}/disable")
def disable_connection(tenant_id: str, connection_id: str, request: Request):
    return disable_connection_use_case(tenant_id, connection_id, request)


@router.post("/connections/{connection_id}/disable")
def disable_connection_global(connection_id: str, request: Request):
    record = store.get_connection(connection_id)
    if record is None:
        raise HTTPException(404, "connection not found")
    return disable_connection_use_case(record.tenant_id, connection_id, request)


def delete_connection_use_case(tenant_id: str, connection_id: str, request: Request):
    record = _owned(tenant_id, connection_id)
    try:
        deleted = store.delete_connection(connection_id, tenant_id)
    except service_store.ServiceReferenceConflictError as exc:
        raise HTTPException(
            409,
            {
                "message": "connection is referenced by MCP services",
                "services": [
                    {
                        "service_id": service.service_id,
                        "display_name": service.display_name,
                    }
                    for service in exc.services
                ],
            },
        ) from None
    if not deleted:
        raise HTTPException(404, "connection not found")
    _audit(record, "connection_deleted")
    return {"ok": True}


@router.delete("/tenants/{tenant_id}/connections/{connection_id}")
def delete_connection(tenant_id: str, connection_id: str, request: Request):
    return delete_connection_use_case(tenant_id, connection_id, request)


@router.delete("/connections/{connection_id}")
def delete_connection_global(connection_id: str, request: Request):
    record = _global_owner(connection_id)
    return delete_connection_use_case(record.tenant_id, connection_id, request)


def replace_connection_credentials_use_case(
    tenant_id: str,
    connection_id: str,
    body: CredentialReplaceRequest,
    request: Request,
):
    record = _owned(tenant_id, connection_id)
    values = _credentials(body.credentials)
    _validate_schema(
        values, _credential_spec_for_record(request, record).credential_schema
    )
    if not _mutate(
        store.replace_credentials,
        connection_id,
        tenant_id,
        values,
        expected_config_version=record.config_version,
    ):
        raise HTTPException(404, "connection not found")
    _audit(record, "connection_credentials_replaced")
    return {"ok": True, "credential_keys": sorted(values)}


@router.put("/tenants/{tenant_id}/connections/{connection_id}/credentials")
@router.post("/tenants/{tenant_id}/connections/{connection_id}/credentials/rotate")
def replace_connection_credentials(
    tenant_id: str,
    connection_id: str,
    body: CredentialReplaceRequest,
    request: Request,
):
    return replace_connection_credentials_use_case(
        tenant_id, connection_id, body, request
    )


@router.put("/connections/{connection_id}/credentials")
@router.post("/connections/{connection_id}/credentials/rotate")
def replace_connection_credentials_global(connection_id: str, body: CredentialReplaceRequest, request: Request):
    return replace_connection_credentials_use_case(
        _global_owner(connection_id).tenant_id, connection_id, body, request
    )


def issue_connection_token_use_case(
    tenant_id: str, connection_id: str, body: TokenIssueRequest, request: Request
):
    record = _owned(tenant_id, connection_id)
    issued = _mutate(store.issue_token, connection_id, label=body.label)
    _audit(record, "connection_token_issued")
    return {"token_id": issued.token_id, "token": issued.raw_value, "prefix": issued.prefix, "label": body.label}


@router.post("/tenants/{tenant_id}/connections/{connection_id}/tokens", status_code=201)
def issue_connection_token(
    tenant_id: str,
    connection_id: str,
    body: TokenIssueRequest,
    request: Request,
    response: Response,
):
    response.headers.update(_NO_STORE_HEADERS)
    return issue_connection_token_use_case(tenant_id, connection_id, body, request)


@router.post("/connections/{connection_id}/tokens", status_code=201)
def issue_connection_token_global(
    connection_id: str,
    body: TokenIssueRequest,
    request: Request,
    response: Response,
):
    response.headers.update(_NO_STORE_HEADERS)
    return issue_connection_token_use_case(
        _global_owner(connection_id).tenant_id, connection_id, body, request
    )


def rotate_connection_token_use_case(
    tenant_id: str, connection_id: str, body: TokenIssueRequest, request: Request
):
    record = _owned(tenant_id, connection_id)
    issued = _mutate(store.rotate_token, connection_id, tenant_id, label=body.label)
    if issued is None:
        raise HTTPException(404, "connection not found")
    _audit(record, "connection_token_rotated")
    return {"token_id": issued.token_id, "token": issued.raw_value, "prefix": issued.prefix, "label": body.label}


@router.post("/tenants/{tenant_id}/connections/{connection_id}/tokens/rotate", status_code=201)
def rotate_connection_token(
    tenant_id: str,
    connection_id: str,
    body: TokenIssueRequest,
    request: Request,
    response: Response,
):
    response.headers.update(_NO_STORE_HEADERS)
    return rotate_connection_token_use_case(tenant_id, connection_id, body, request)


@router.post("/connections/{connection_id}/tokens/rotate", status_code=201)
def rotate_connection_token_global(
    connection_id: str,
    body: TokenIssueRequest,
    request: Request,
    response: Response,
):
    response.headers.update(_NO_STORE_HEADERS)
    return rotate_connection_token_use_case(
        _global_owner(connection_id).tenant_id, connection_id, body, request
    )


def revoke_connection_token_use_case(
    tenant_id: str, connection_id: str, token_id: str, request: Request
):
    record = _owned(tenant_id, connection_id)
    if not _mutate(store.revoke_token, connection_id, tenant_id, token_id):
        raise HTTPException(404, "token not found")
    _audit(record, "connection_token_revoked")
    return {"ok": True}


@router.delete("/tenants/{tenant_id}/connections/{connection_id}/tokens/{token_id}")
def revoke_connection_token(
    tenant_id: str, connection_id: str, token_id: str, request: Request
):
    return revoke_connection_token_use_case(
        tenant_id, connection_id, token_id, request
    )


@router.delete("/connections/{connection_id}/tokens/{token_id}")
def revoke_connection_token_global(connection_id: str, token_id: str, request: Request):
    return revoke_connection_token_use_case(
        _global_owner(connection_id).tenant_id, connection_id, token_id, request
    )


def list_connection_tools_use_case(
    tenant_id: str, connection_id: str, request: Request
):
    record = _owned(tenant_id, connection_id)
    spec = _management_spec_for_record(request, record)
    configured = {item.tool_name: item for item in store.list_tool_policies(connection_id)}
    return {
        "connector_key": spec.connector_key,
        "version": spec.version,
        "credential_schema": _plain_schema_metadata(spec.credential_schema),
        "items": [{
        "tool_key": tool.tool_key,
        "mcp_name": tool.mcp_name,
        "description": tool.description,
        "input_schema": _plain_schema_metadata(tool.input_schema),
        "output_schema": (
            None
            if tool.output_schema is None
            else _plain_schema_metadata(tool.output_schema)
        ),
        "operation_kind": tool.operation_kind,
        "enabled": configured.get(tool.tool_key, ToolPolicy(connection_id, tool.tool_key, True, {})).enabled,
        "policy": configured.get(tool.tool_key, ToolPolicy(connection_id, tool.tool_key, True, {})).policy,
        } for tool in spec.tools],
    }


@router.get("/tenants/{tenant_id}/connections/{connection_id}/tools")
def list_connection_tools(tenant_id: str, connection_id: str, request: Request):
    return list_connection_tools_use_case(tenant_id, connection_id, request)


@router.get("/connections/{connection_id}/tools")
def list_connection_tools_global(connection_id: str, request: Request):
    return list_connection_tools_use_case(
        _global_owner(connection_id).tenant_id, connection_id, request
    )


def update_connection_tools_use_case(
    tenant_id: str,
    connection_id: str,
    body: ToolPoliciesRequest,
    request: Request,
):
    record = _owned(tenant_id, connection_id)
    spec = _management_spec_for_record(request, record)
    declared = {tool.tool_key: tool for tool in spec.tools}
    policies: list[ToolPolicy] = []
    seen = set()
    for item in body.policies:
        tool = declared.get(item.tool_key)
        if tool is None or item.tool_key in seen:
            raise HTTPException(422, "invalid tool policy")
        if tool.operation_kind == "write" and item.enabled and not item.allow_write:
            raise HTTPException(422, "invalid tool policy")
        seen.add(item.tool_key)
        policy: dict[str, Any] = {"allow_write": item.allow_write}
        if item.timeout_ms is not None:
            policy["timeout_ms"] = item.timeout_ms
        if item.rate_limit_per_minute is not None:
            policy["rate_limit"] = {"limit": item.rate_limit_per_minute, "window_seconds": 60}
        policies.append(ToolPolicy(connection_id, item.tool_key, item.enabled, policy))
    if not _mutate(
        store.replace_tool_policies,
        connection_id,
        tenant_id,
        policies,
        expected_config_version=record.config_version,
    ):
        raise HTTPException(404, "connection not found")
    _audit(record, "connection_tools_updated")
    return {"ok": True}


@router.put("/tenants/{tenant_id}/connections/{connection_id}/tools")
def update_connection_tools(
    tenant_id: str,
    connection_id: str,
    body: ToolPoliciesRequest,
    request: Request,
):
    return update_connection_tools_use_case(tenant_id, connection_id, body, request)


@router.put("/connections/{connection_id}/tools")
def update_connection_tools_global(connection_id: str, body: ToolPoliciesRequest, request: Request):
    return update_connection_tools_use_case(
        _global_owner(connection_id).tenant_id, connection_id, body, request
    )


async def test_connection_use_case(
    tenant_id: str, connection_id: str, request: Request
):
    record = _owned(tenant_id, connection_id)
    execution_record = _management_record(record)
    if record.status != "active" and execution_record is record:
        raise HTTPException(409, "connection test is unsupported")
    gateway = getattr(request.app.state, "mcp_gateway", None)
    try:
        execution_context = gateway.resolver.execution_context(
            ConnectionCtx(
                tenant_id=execution_record.tenant_id,
                connection_id=execution_record.connection_id,
                connector_key=execution_record.connector_key,
                data_mode=execution_record.data_mode,
                public_config=execution_record.public_config,
                config_version=execution_record.config_version,
            )
        )
        tools = gateway._runtime.list_enabled_tools(execution_context)
        tool = next(
            (
                candidate
                for candidate in tools
                if candidate.operation_kind == "read"
                and not candidate.input_schema.get("required")
            ),
            None,
        )
        if tool is None:
            raise HTTPException(409, "connection test is unsupported")
        result = await gateway._runtime.execute(execution_context, tool.tool_key, {})
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Connection test failed type=%s", type(exc).__name__)
        _audit(record, "connection_tested", status="error")
        raise HTTPException(502, "connection test failed") from None
    if getattr(result, "status", "error") != "ok":
        _audit(record, "connection_tested", status="error")
        raise HTTPException(502, "connection test failed")
    _audit(record, "connection_tested")
    return {"ok": True, "status": "ok"}


@router.post("/tenants/{tenant_id}/connections/{connection_id}/test")
async def test_connection(tenant_id: str, connection_id: str, request: Request):
    return await test_connection_use_case(tenant_id, connection_id, request)


@router.post("/connections/{connection_id}/test")
async def test_connection_global(connection_id: str, request: Request):
    return await test_connection_use_case(
        _global_owner(connection_id).tenant_id, connection_id, request
    )


async def sync_connection_use_case(
    tenant_id: str, connection_id: str, request: Request
):
    record = _owned(tenant_id, connection_id)
    if record.status != "active" or record.data_mode not in {"stored", "hybrid"}:
        raise HTTPException(409, "connection is not eligible for sync")
    result = request.app.state.connection_sync_orchestrator.run_connection(record)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        raise HTTPException(409, "connection is not eligible for sync")
    _audit(
        record,
        "connection_sync_triggered",
        status="error" if result.status == "error" else "ok",
    )
    return {"ok": result.status != "error", "status": result.status}


@router.post("/tenants/{tenant_id}/connections/{connection_id}/sync")
async def sync_connection(tenant_id: str, connection_id: str, request: Request):
    return await sync_connection_use_case(tenant_id, connection_id, request)


@router.post("/connections/{connection_id}/sync")
async def sync_connection_global(connection_id: str, request: Request):
    return await sync_connection_use_case(
        _global_owner(connection_id).tenant_id, connection_id, request
    )


def import_connection_spec_use_case(
    tenant_id: str,
    connection_id: str,
    body: OpenApiImportRequest,
    request: Request,
):
    record = _owned(tenant_id, connection_id)
    _require_declarative(record)
    if record.status not in {"draft", "disabled"}:
        raise HTTPException(409, "disable connection before changing revision")
    try:
        revision = import_openapi_revision(
            body.document,
            spec_id=body.spec_id,
            revision=body.revision,
            tenant_id=tenant_id,
            connection_id=connection_id,
            status="draft",
            allowed_hosts=body.allowed_hosts,
            sync_spec=body.sync_spec,
        )
        compiled = validate_revision(revision, data_mode=record.data_mode)
        store.save_declarative_revision(
            compiled, expected_config_version=record.config_version
        )
    except store.ConnectionVersionConflictError:
        raise HTTPException(409, "connection configuration changed") from None
    except (SpecValidationError, ValueError, TypeError) as exc:
        logger.warning("Declarative import rejected type=%s", type(exc).__name__)
        raise HTTPException(422, "invalid declarative specification") from None
    _audit(record, "declarative_spec_imported")
    return {
        "spec_id": compiled.spec_id,
        "revision": compiled.revision,
        "status": compiled.status,
        "preview": _declarative_preview(compiled),
    }


@router.post("/tenants/{tenant_id}/connections/{connection_id}/specs/import", status_code=201)
def import_connection_spec(
    tenant_id: str,
    connection_id: str,
    body: OpenApiImportRequest,
    request: Request,
):
    return import_connection_spec_use_case(tenant_id, connection_id, body, request)


@router.post("/connections/{connection_id}/specs/import", status_code=201)
def import_connection_spec_global(connection_id: str, body: OpenApiImportRequest, request: Request):
    return import_connection_spec_use_case(
        _global_owner(connection_id).tenant_id, connection_id, body, request
    )


def validate_connection_spec_use_case(
    tenant_id: str,
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    record = _owned(tenant_id, connection_id)
    _require_declarative(record)
    stored = store.get_declarative_revision(spec_id, revision, tenant_id, connection_id)
    if stored is None:
        raise HTTPException(404, "revision not found")
    try:
        compiled = validate_revision(stored, data_mode=record.data_mode)
    except (SpecValidationError, ValueError, TypeError):
        raise HTTPException(422, "invalid declarative specification") from None
    return {
        "valid": True,
        "spec_id": spec_id,
        "revision": revision,
        "status": compiled.status,
        "preview": _declarative_preview(compiled),
    }


@router.post("/tenants/{tenant_id}/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/validate")
def validate_connection_spec(
    tenant_id: str,
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    return validate_connection_spec_use_case(
        tenant_id, connection_id, spec_id, revision, request
    )


@router.post("/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/validate")
def validate_connection_spec_global(connection_id: str, spec_id: str, revision: int, request: Request):
    return validate_connection_spec_use_case(
        _global_owner(connection_id).tenant_id,
        connection_id,
        spec_id,
        revision,
        request,
    )


def delete_connection_spec_use_case(
    tenant_id: str,
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    record = _owned(tenant_id, connection_id)
    _require_declarative(record)
    try:
        deleted = store.delete_declarative_revision(
            spec_id,
            revision,
            tenant_id,
            connection_id,
        )
    except store.DeclarativeRevisionInUseError:
        raise HTTPException(409, "declarative revision is referenced") from None
    if not deleted:
        raise HTTPException(404, "revision not found")
    _audit(record, "declarative_spec_deleted")
    return {"ok": True}


@router.delete("/tenants/{tenant_id}/connections/{connection_id}/specs/{spec_id}/revisions/{revision}")
def delete_connection_spec(
    tenant_id: str,
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    return delete_connection_spec_use_case(
        tenant_id, connection_id, spec_id, revision, request
    )


@router.delete("/connections/{connection_id}/specs/{spec_id}/revisions/{revision}")
def delete_connection_spec_global(
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    record = _global_owner(connection_id)
    return delete_connection_spec_use_case(
        record.tenant_id,
        connection_id,
        spec_id,
        revision,
        request,
    )


def publish_connection_spec_use_case(
    tenant_id: str,
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    record = _owned(tenant_id, connection_id)
    _require_declarative(record)
    if record.status not in {"draft", "disabled"}:
        raise HTTPException(409, "disable connection before changing revision")
    try:
        published = store.publish_declarative_revision(
            spec_id,
            revision,
            tenant_id,
            connection_id,
            expected_config_version=record.config_version,
        )
    except store.ConnectionVersionConflictError:
        raise HTTPException(409, "connection configuration changed") from None
    except (SpecValidationError, ValueError, TypeError):
        raise HTTPException(422, "invalid declarative specification") from None
    if published is None:
        raise HTTPException(404, "revision not found")
    _audit(record, "declarative_spec_published")
    return {"spec_id": spec_id, "revision": revision, "status": "published"}


@router.post("/tenants/{tenant_id}/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/publish")
def publish_connection_spec(
    tenant_id: str,
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    return publish_connection_spec_use_case(
        tenant_id, connection_id, spec_id, revision, request
    )


@router.post("/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/publish")
def publish_connection_spec_global(connection_id: str, spec_id: str, revision: int, request: Request):
    return publish_connection_spec_use_case(
        _global_owner(connection_id).tenant_id,
        connection_id,
        spec_id,
        revision,
        request,
    )


def activate_connection_spec_use_case(
    tenant_id: str,
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    current = _owned(tenant_id, connection_id)
    _require_declarative(current)
    if _pending_revision_identity(current) != (spec_id, revision):
        raise HTTPException(409, "pending declarative revision is unavailable")
    spec = _declarative_candidate_spec(
        request, current, spec_id, revision
    )
    _validate_schema(
        _load_connection_credentials(request, current),
        spec.credential_schema,
    )
    declared_tools = {tool.tool_key for tool in spec.tools}
    configured_tools = {
        policy.tool_name for policy in store.list_tool_policies(connection_id)
    }
    if configured_tools != declared_tools:
        raise HTTPException(409, "pending tool policies are incomplete")
    try:
        activated = store.activate_declarative_revision(
            spec_id,
            revision,
            tenant_id,
            connection_id,
            expected_config_version=current.config_version,
        )
    except store.ConnectionVersionConflictError:
        raise HTTPException(409, "connection configuration changed") from None
    except (SpecValidationError, ValueError, TypeError):
        raise HTTPException(422, "invalid declarative specification") from None
    if activated is None:
        raise HTTPException(404, "revision not found")
    _audit(activated, "declarative_spec_activated")
    return {"connection": _safe_connection_for_request(request, activated)}


@router.post("/tenants/{tenant_id}/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/activate")
def activate_connection_spec(
    tenant_id: str,
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    return activate_connection_spec_use_case(
        tenant_id, connection_id, spec_id, revision, request
    )


@router.post("/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/activate")
def activate_connection_spec_global(connection_id: str, spec_id: str, revision: int, request: Request):
    return activate_connection_spec_use_case(
        _global_owner(connection_id).tenant_id,
        connection_id,
        spec_id,
        revision,
        request,
    )
