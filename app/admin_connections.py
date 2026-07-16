"""Authenticated, tenant-scoped administration for MCP connection instances."""
from __future__ import annotations

import inspect
import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import admin
from .connections import store
from .connections.models import ConnectionRecord, ToolPolicy
from .connectors.contracts import ConnectorSpec
from .connectors.declarative.models import SpecValidationError
from .connectors.declarative.validator import import_openapi_revision, validate_revision
from .mcp_audit import write_event
from .mcp_log_models import McpLogEvent


logger = logging.getLogger(__name__)
_SENSITIVE_PARTS = frozenset(
    {"authorization", "cookie", "credential", "password", "secret", "token"}
)


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
    if not isinstance(value, Mapping):
        return None
    properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    projected: dict[str, Any] = {}
    for key, item in value.items():
        child = properties.get(key, {}) if isinstance(properties, Mapping) else {}
        if _is_sensitive(str(key), child if isinstance(child, Mapping) else None):
            continue
        if isinstance(item, Mapping):
            projected[key] = _safe_config(item, child if isinstance(child, Mapping) else {})
        elif isinstance(item, list):
            projected[key] = [
                _safe_config(part, {}) if isinstance(part, Mapping) else part
                for part in item
            ]
        else:
            projected[key] = item
    return projected


def _safe_connection(record: ConnectionRecord, spec: ConnectorSpec) -> dict[str, Any]:
    return {
        "connection_id": record.connection_id,
        "tenant_id": record.tenant_id,
        "connector_key": record.connector_key,
        "display_name": record.display_name,
        "status": record.status,
        "data_mode": record.data_mode,
        "public_config": _safe_config(record.public_config, spec.config_schema),
        "config_version": record.config_version,
    }


def _owned(tenant_id: str, connection_id: str) -> ConnectionRecord:
    record = store.get_connection(connection_id, tenant_id)
    if record is None:
        raise HTTPException(404, "connection not found")
    return record


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
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Connection mutation failed type=%s", type(exc).__name__)
        raise HTTPException(400, "connection mutation failed") from None


@router.get("/tenants/{tenant_id}/connections")
def list_connections(tenant_id: str, request: Request):
    items = []
    for record in store.list_connections(tenant_id):
        items.append(_safe_connection(record, _spec(request, record.connector_key)))
    return {"items": items}


@router.post("/tenants/{tenant_id}/connections", status_code=201)
def create_connection(tenant_id: str, body: ConnectionCreateRequest, request: Request):
    spec = _spec(request, body.connector_key)
    if body.data_mode not in spec.supports_data_modes:
        _schema_error()
    if not isinstance(body.public_config, Mapping):
        _schema_error()
    credentials = _credentials(body.credentials)
    _validate_schema(body.public_config, spec.config_schema)
    _validate_schema(credentials, spec.credential_schema)
    record = ConnectionRecord(
        connection_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        connector_key=body.connector_key,
        display_name=body.display_name,
        status=body.status,
        data_mode=body.data_mode,
        public_config=dict(body.public_config),
        config_version=1,
    )
    try:
        created, issued = store.create_connection_with_token(record, credentials)
    except Exception as exc:
        logger.warning("Connection create failed type=%s", type(exc).__name__)
        raise HTTPException(400, "connection mutation failed") from None
    _audit(created, "connection_created")
    return {
        "connection": _safe_connection(created, spec),
        "initial_token": issued.raw_value,
        "token_prefix": issued.prefix,
    }


@router.get("/tenants/{tenant_id}/connections/{connection_id}")
def get_connection(tenant_id: str, connection_id: str, request: Request):
    record = _owned(tenant_id, connection_id)
    return {
        "connection": _safe_connection(record, _spec(request, record.connector_key)),
        "tokens": store.list_connection_tokens(connection_id),
    }


@router.get("/connections/{connection_id}")
def get_connection_global(connection_id: str, request: Request):
    record = store.get_connection(connection_id)
    if record is None:
        raise HTTPException(404, "connection not found")
    return get_connection(record.tenant_id, connection_id, request)


def _global_owner(connection_id: str) -> ConnectionRecord:
    record = store.get_connection(connection_id)
    if record is None:
        raise HTTPException(404, "connection not found")
    return record


@router.put("/tenants/{tenant_id}/connections/{connection_id}")
def update_connection(tenant_id: str, connection_id: str, body: ConnectionUpdateRequest, request: Request):
    current = _owned(tenant_id, connection_id)
    spec = _spec(request, current.connector_key)
    if body.data_mode not in spec.supports_data_modes or not isinstance(body.public_config, Mapping):
        _schema_error()
    _validate_schema(body.public_config, spec.config_schema)
    updated = _mutate(store.update_connection,
        connection_id,
        tenant_id,
        display_name=body.display_name,
        data_mode=body.data_mode,
        public_config=body.public_config,
        status=body.status,
    )
    if updated is None:
        raise HTTPException(404, "connection not found")
    _audit(updated, "connection_updated")
    return {"connection": _safe_connection(updated, spec)}


@router.put("/connections/{connection_id}")
def update_connection_global(connection_id: str, body: ConnectionUpdateRequest, request: Request):
    return update_connection(_global_owner(connection_id).tenant_id, connection_id, body, request)


@router.post("/tenants/{tenant_id}/connections/{connection_id}/disable")
def disable_connection(tenant_id: str, connection_id: str, request: Request):
    _owned(tenant_id, connection_id)
    record = _mutate(store.disable_connection, connection_id, tenant_id)
    if record is None:
        raise HTTPException(404, "connection not found")
    _audit(record, "connection_disabled")
    return {"connection": _safe_connection(record, _spec(request, record.connector_key))}


@router.post("/connections/{connection_id}/disable")
def disable_connection_global(connection_id: str, request: Request):
    record = store.get_connection(connection_id)
    if record is None:
        raise HTTPException(404, "connection not found")
    return disable_connection(record.tenant_id, connection_id, request)


@router.put("/tenants/{tenant_id}/connections/{connection_id}/credentials")
@router.post("/tenants/{tenant_id}/connections/{connection_id}/credentials/rotate")
def replace_connection_credentials(tenant_id: str, connection_id: str, body: CredentialReplaceRequest, request: Request):
    record = _owned(tenant_id, connection_id)
    values = _credentials(body.credentials)
    _validate_schema(values, _spec(request, record.connector_key).credential_schema)
    if not _mutate(store.replace_credentials, connection_id, tenant_id, values):
        raise HTTPException(404, "connection not found")
    _audit(record, "connection_credentials_replaced")
    return {"ok": True, "credential_keys": sorted(values)}


@router.put("/connections/{connection_id}/credentials")
@router.post("/connections/{connection_id}/credentials/rotate")
def replace_connection_credentials_global(connection_id: str, body: CredentialReplaceRequest, request: Request):
    return replace_connection_credentials(_global_owner(connection_id).tenant_id, connection_id, body, request)


@router.post("/tenants/{tenant_id}/connections/{connection_id}/tokens", status_code=201)
def issue_connection_token(tenant_id: str, connection_id: str, body: TokenIssueRequest, request: Request):
    record = _owned(tenant_id, connection_id)
    issued = _mutate(store.issue_token, connection_id, label=body.label)
    _audit(record, "connection_token_issued")
    return {"token_id": issued.token_id, "token": issued.raw_value, "prefix": issued.prefix, "label": body.label}


@router.post("/connections/{connection_id}/tokens", status_code=201)
def issue_connection_token_global(connection_id: str, body: TokenIssueRequest, request: Request):
    return issue_connection_token(_global_owner(connection_id).tenant_id, connection_id, body, request)


@router.post("/tenants/{tenant_id}/connections/{connection_id}/tokens/rotate", status_code=201)
def rotate_connection_token(tenant_id: str, connection_id: str, body: TokenIssueRequest, request: Request):
    record = _owned(tenant_id, connection_id)
    issued = _mutate(store.rotate_token, connection_id, tenant_id, label=body.label)
    if issued is None:
        raise HTTPException(404, "connection not found")
    _audit(record, "connection_token_rotated")
    return {"token_id": issued.token_id, "token": issued.raw_value, "prefix": issued.prefix, "label": body.label}


@router.post("/connections/{connection_id}/tokens/rotate", status_code=201)
def rotate_connection_token_global(connection_id: str, body: TokenIssueRequest, request: Request):
    return rotate_connection_token(_global_owner(connection_id).tenant_id, connection_id, body, request)


@router.delete("/tenants/{tenant_id}/connections/{connection_id}/tokens/{token_id}")
def revoke_connection_token(tenant_id: str, connection_id: str, token_id: str, request: Request):
    record = _owned(tenant_id, connection_id)
    if not _mutate(store.revoke_token, connection_id, tenant_id, token_id):
        raise HTTPException(404, "token not found")
    _audit(record, "connection_token_revoked")
    return {"ok": True}


@router.delete("/connections/{connection_id}/tokens/{token_id}")
def revoke_connection_token_global(connection_id: str, token_id: str, request: Request):
    return revoke_connection_token(_global_owner(connection_id).tenant_id, connection_id, token_id, request)


@router.get("/tenants/{tenant_id}/connections/{connection_id}/tools")
def list_connection_tools(tenant_id: str, connection_id: str, request: Request):
    record = _owned(tenant_id, connection_id)
    spec = _spec(request, record.connector_key)
    configured = {item.tool_name: item for item in store.list_tool_policies(connection_id)}
    return {"items": [{
        "tool_key": tool.tool_key,
        "mcp_name": tool.mcp_name,
        "operation_kind": tool.operation_kind,
        "enabled": configured.get(tool.tool_key, ToolPolicy(connection_id, tool.tool_key, True, {})).enabled,
        "policy": configured.get(tool.tool_key, ToolPolicy(connection_id, tool.tool_key, True, {})).policy,
    } for tool in spec.tools]}


@router.get("/connections/{connection_id}/tools")
def list_connection_tools_global(connection_id: str, request: Request):
    return list_connection_tools(_global_owner(connection_id).tenant_id, connection_id, request)


@router.put("/tenants/{tenant_id}/connections/{connection_id}/tools")
def update_connection_tools(tenant_id: str, connection_id: str, body: ToolPoliciesRequest, request: Request):
    record = _owned(tenant_id, connection_id)
    spec = _spec(request, record.connector_key)
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
    if not _mutate(store.replace_tool_policies, connection_id, tenant_id, policies):
        raise HTTPException(404, "connection not found")
    _audit(record, "connection_tools_updated")
    return {"ok": True}


@router.put("/connections/{connection_id}/tools")
def update_connection_tools_global(connection_id: str, body: ToolPoliciesRequest, request: Request):
    return update_connection_tools(_global_owner(connection_id).tenant_id, connection_id, body, request)


@router.post("/tenants/{tenant_id}/connections/{connection_id}/test")
def test_connection(tenant_id: str, connection_id: str, request: Request):
    record = _owned(tenant_id, connection_id)
    _spec(request, record.connector_key)
    _audit(record, "connection_tested")
    return {"ok": True, "status": "validated"}


@router.post("/connections/{connection_id}/test")
def test_connection_global(connection_id: str, request: Request):
    return test_connection(_global_owner(connection_id).tenant_id, connection_id, request)


@router.post("/tenants/{tenant_id}/connections/{connection_id}/sync")
async def sync_connection(tenant_id: str, connection_id: str, request: Request):
    record = _owned(tenant_id, connection_id)
    if record.status != "active" or record.data_mode not in {"stored", "hybrid"}:
        raise HTTPException(409, "connection is not eligible for sync")
    result = request.app.state.connection_sync_orchestrator.run_connection(record)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        raise HTTPException(409, "connection is not eligible for sync")
    return {"ok": result.status != "error", "status": result.status}


@router.post("/connections/{connection_id}/sync")
async def sync_connection_global(connection_id: str, request: Request):
    return await sync_connection(_global_owner(connection_id).tenant_id, connection_id, request)


@router.post("/tenants/{tenant_id}/connections/{connection_id}/specs/import", status_code=201)
def import_connection_spec(tenant_id: str, connection_id: str, body: OpenApiImportRequest, request: Request):
    record = _owned(tenant_id, connection_id)
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
        validate_revision(revision, data_mode=record.data_mode)
        store.save_declarative_revision(revision)
    except (SpecValidationError, ValueError, TypeError):
        raise HTTPException(422, "invalid declarative specification") from None
    _audit(record, "declarative_spec_imported")
    return {"spec_id": revision.spec_id, "revision": revision.revision, "status": revision.status}


@router.post("/connections/{connection_id}/specs/import", status_code=201)
def import_connection_spec_global(connection_id: str, body: OpenApiImportRequest, request: Request):
    return import_connection_spec(_global_owner(connection_id).tenant_id, connection_id, body, request)


@router.post("/tenants/{tenant_id}/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/validate")
def validate_connection_spec(
    tenant_id: str,
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    record = _owned(tenant_id, connection_id)
    stored = store.get_declarative_revision(spec_id, revision, tenant_id, connection_id)
    if stored is None:
        raise HTTPException(404, "revision not found")
    try:
        validate_revision(stored, data_mode=record.data_mode)
    except (SpecValidationError, ValueError, TypeError):
        raise HTTPException(422, "invalid declarative specification") from None
    return {"valid": True, "spec_id": spec_id, "revision": revision, "status": stored.status}


@router.post("/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/validate")
def validate_connection_spec_global(connection_id: str, spec_id: str, revision: int, request: Request):
    return validate_connection_spec(_global_owner(connection_id).tenant_id, connection_id, spec_id, revision, request)


@router.post("/tenants/{tenant_id}/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/publish")
def publish_connection_spec(
    tenant_id: str,
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    record = _owned(tenant_id, connection_id)
    try:
        published = store.publish_declarative_revision(spec_id, revision, tenant_id, connection_id)
    except (SpecValidationError, ValueError, TypeError):
        raise HTTPException(422, "invalid declarative specification") from None
    if published is None:
        raise HTTPException(404, "revision not found")
    _audit(record, "declarative_spec_published")
    return {"spec_id": spec_id, "revision": revision, "status": "published"}


@router.post("/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/publish")
def publish_connection_spec_global(connection_id: str, spec_id: str, revision: int, request: Request):
    return publish_connection_spec(_global_owner(connection_id).tenant_id, connection_id, spec_id, revision, request)


@router.post("/tenants/{tenant_id}/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/activate")
def activate_connection_spec(
    tenant_id: str,
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
):
    current = _owned(tenant_id, connection_id)
    try:
        activated = store.activate_declarative_revision(spec_id, revision, tenant_id, connection_id)
    except (SpecValidationError, ValueError, TypeError):
        raise HTTPException(422, "invalid declarative specification") from None
    if activated is None:
        raise HTTPException(404, "revision not found")
    _audit(activated, "declarative_spec_activated")
    return {"connection": _safe_connection(activated, _spec(request, current.connector_key))}


@router.post("/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/activate")
def activate_connection_spec_global(connection_id: str, spec_id: str, revision: int, request: Request):
    return activate_connection_spec(_global_owner(connection_id).tenant_id, connection_id, spec_id, revision, request)
