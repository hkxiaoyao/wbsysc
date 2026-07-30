from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import admin, admin_connections
from app.connections.models import ConnectionRecord, IssuedToken
from app.connectors.contracts import (
    ConnectionContext,
    ConnectorSpec,
    SyncResult,
    ToolSpec,
)
from app.connectors.registry import ConnectorRegistry
from app.connectors.runtime import PolicyGuard
from app.connectors.declarative.models import (
    DeclarativeOperation,
    DeclarativeStep,
    DeclarativeTool,
    InputMapping,
    OutputMapping,
    ValueRef,
)
from app.connectors.declarative.validator import import_openapi_revision


def test_wecom_public_config_gets_connection_owned_schema():
    connection_id = "connection-a"

    config = admin_connections._connection_public_config(
        "wecom",
        connection_id,
        {
            "corpid": "ww123",
            "enabled_modules": ["report"],
            "trusted_domain": "mcp.example.com",
        },
    )

    assert config == {
        "corpid": "ww123",
        "enabled_modules": ["report"],
        "trusted_domain": "mcp.example.com",
        "schema_name": admin_connections._wecom_schema_name(connection_id),
    }


def test_wecom_public_config_rebinds_unverified_existing_schema():
    config = admin_connections._connection_public_config(
        "wecom",
        "connection-a",
        {
            "corpid": "ww456",
            "schema_name": "attacker_selected",
            "legacy_source": "tenant_config",
        },
        tenant_id="tenant-a",
        current={"schema_name": "wbd_existing"},
    )

    assert config == {
        "corpid": "ww456",
        "schema_name": admin_connections._wecom_schema_name("connection-a"),
    }


def test_wecom_public_config_preserves_verified_legacy_schema():
    tenant_id = "tenant-a"
    connection_id = admin_connections._legacy_wecom_connection_id(tenant_id)

    config = admin_connections._connection_public_config(
        "wecom",
        connection_id,
        {
            "corpid": "ww456",
            "schema_name": "ignored",
            "legacy_source": "tenant_config",
        },
        tenant_id=tenant_id,
        current={
            "schema_name": "wbd_existing",
            "legacy_source": "tenant_config",
        },
    )

    assert config == {
        "corpid": "ww456",
        "schema_name": "wbd_existing",
        "legacy_source": "tenant_config",
    }


def test_wecom_domain_verification_is_connection_scoped(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="wecom",
        public_config={
            "corpid": "ww123",
            "trusted_domain": "mcp.example.com",
            "schema_name": "wbd_conn",
        },
    )
    saved = {}
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: (
            record
            if connection_id == "conn-a"
            and (tenant_id is None or tenant_id == "tenant-a")
            else None
        ),
    )
    monkeypatch.setattr(
        admin_connections,
        "get_verify_by_connection",
        lambda connection_id, tenant_id, adopt_legacy=False: (
            {
                "filename": "WW_verify_a.txt",
                "updated_at": "2026-07-23 15:00:00",
            }
            if not saved
            else saved
        ),
    )

    def save(**values):
        saved.update(
            filename=values["filename"],
            updated_at="2026-07-23 15:01:00",
        )
        assert values["connection_id"] == "conn-a"
        assert values["tenant_id"] == "tenant-a"
        assert values["trusted_domain"] == "mcp.example.com"
        return saved

    monkeypatch.setattr(admin_connections, "save_verify_file_for_connection", save)
    monkeypatch.setattr(
        admin_connections,
        "delete_verify_by_connection",
        lambda connection_id, tenant_id: (
            connection_id == "conn-a" and tenant_id == "tenant-a"
        ),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    current = client.get("/admin/connections/conn-a/domain-verify")
    uploaded = client.post(
        "/admin/connections/conn-a/domain-verify",
        files={"file": ("WW_verify_b.txt", b"verify-value", "text/plain")},
    )
    deleted = client.delete("/admin/connections/conn-a/domain-verify")

    assert current.status_code == 200
    assert current.json()["verify_url"] == "https://mcp.example.com/WW_verify_a.txt"
    assert uploaded.status_code == 200
    assert uploaded.json()["connection_id"] == "conn-a"
    assert uploaded.json()["verify_filename"] == "WW_verify_b.txt"
    assert deleted.status_code == 200


def test_domain_verification_rejects_non_wecom_connection(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda *args: _record(),
    )

    response = client.get("/admin/connections/conn-a/domain-verify")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "domain verification requires a WeCom connection"
    )


class _Connector:
    def spec(self):
        return ConnectorSpec(
            connector_key="sample",
            version="1.0.0",
            supports_data_modes=("direct",),
            config_schema={
                "type": "object",
                "required": ["base_url"],
                "properties": {"base_url": {"type": "string"}},
                "additionalProperties": False,
            },
            credential_schema={
                "type": "object",
                "required": ["api_key"],
                "properties": {"api_key": {"type": "string", "minLength": 8}},
                "additionalProperties": False,
            },
            tools=(
                ToolSpec(
                    tool_key="items.list",
                    mcp_name="items_list",
                    description="List items",
                    input_schema={"type": "object"},
                    output_schema=None,
                    operation_kind="read",
                    default_timeout_ms=1000,
                    cache_ttl_seconds=10,
                ),
            ),
        )


def _record(**changes):
    base = ConnectionRecord(
        connection_id="conn-a",
        tenant_id="tenant-a",
        connector_key="sample",
        display_name="Sample",
        status="active",
        data_mode="direct",
        public_config={"base_url": "https://api.example.com"},
        config_version=1,
    )
    return replace(base, **changes)


def _compiled_revision_stub(*, spec_id: str, revision: int, status: str):
    return SimpleNamespace(
        spec_id=spec_id,
        revision=revision,
        status=status,
        operations=(_compiled_operation_stub(),),
        tools=(),
    )


def _compiled_operation_stub(
    *,
    tool_key: str = "items.get",
    mcp_name: str = "items_get",
    input_schema=None,
):
    input_mappings = ()
    if input_schema is not None:
        input_mappings = (
            InputMapping(
                arg_name="item_id",
                location="query",
                target="item_id",
                required=True,
                schema=input_schema,
            ),
        )
    return DeclarativeOperation(
        tool_key=tool_key,
        mcp_name=mcp_name,
        description="Get item",
        method="GET",
        path="/items",
        input_mappings=input_mappings,
        output_mappings=(OutputMapping(name="id", pointer="/id"),),
        operation_kind="read",
        base_url="https://api.example.com",
    )


def _compiled_preview_with_bounds(
    *, operation_schema, tool_property_schema
):
    operation = _compiled_operation_stub(input_schema=operation_schema)
    step = DeclarativeStep(
        step_id="operation",
        operation_key=operation.tool_key,
        input_mappings={
            "item_id": ValueRef(source="input", field="item_id"),
        },
        output_mappings={"id": "id"},
    )
    tool = DeclarativeTool(
        tool_key="items.get.safe",
        mcp_name="items_get_safe",
        description="Get item safely",
        input_schema={
            "type": "object",
            "properties": {"item_id": tool_property_schema},
            "required": ["item_id"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
        steps=(step,),
        result_map={
            "id": ValueRef(source="steps", step_id="operation", field="id"),
        },
    )
    return admin_connections._declarative_preview(
        SimpleNamespace(operations=(operation,), tools=(tool,))
    )


def _quick_openapi_revision(*, status="published"):
    document = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com"}],
        "components": {
            "securitySchemes": {
                "ApiKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "x-credential-key": "api_key",
                }
            }
        },
        "security": [{"ApiKey": []}],
        "paths": {
            "/health": {
                "get": {
                    "operationId": "health.check",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "ok": {"type": "boolean"}
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/items": {
                "post": {
                    "operationId": "items.create",
                    "x-write-enabled": True,
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"}
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/items/archive": {
                "post": {
                    "operationId": "items.archive",
                    "x-write-enabled": True,
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"}
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
        },
    }
    return import_openapi_revision(
        document,
        spec_id="quick-analysis",
        revision=1,
        tenant_id="tenant-a",
        connection_id="conn-a",
        status=status,
    )


def _install_quick_activation(
    monkeypatch,
    client,
    *,
    execute_error=None,
    credential_version_increment=1,
    policy_version_increment=1,
):
    compiled = _quick_openapi_revision()
    state = {
        "record": _record(
            connector_key="http_declarative",
            status="draft",
            public_config={"spec_id": compiled.spec_id, "revision": 1},
            config_version=8,
        )
    }
    captured = {
        "credentials": None,
        "policies": [],
        "executed": [],
        "bypass_cache": [],
        "activated": [],
    }

    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: state["record"],
    )
    monkeypatch.setattr(
        admin_connections.store,
        "get_declarative_revision",
        lambda *args: compiled,
    )
    monkeypatch.setattr(
        admin_connections,
        "_declarative_candidate_spec",
        lambda *args: compiled.connector_spec(),
    )

    def replace_credentials(connection_id, tenant_id, credentials, **kwargs):
        assert kwargs["expected_config_version"] == state["record"].config_version
        captured["credentials"] = dict(credentials)
        state["record"] = replace(
            state["record"],
            config_version=(
                state["record"].config_version + credential_version_increment
            ),
        )
        return True

    def replace_policies(connection_id, tenant_id, policies, **kwargs):
        assert kwargs["expected_config_version"] == state["record"].config_version
        captured["policies"] = list(policies)
        state["record"] = replace(
            state["record"],
            config_version=(
                state["record"].config_version + policy_version_increment
            ),
        )
        return True

    def activate_revision(*args, **kwargs):
        assert kwargs["expected_config_version"] == state["record"].config_version
        captured["activated"].append((args[0], args[1]))
        state["record"] = replace(
            state["record"],
            status="active",
            config_version=state["record"].config_version + 1,
        )
        return state["record"]

    monkeypatch.setattr(
        admin_connections.store, "replace_credentials", replace_credentials
    )
    monkeypatch.setattr(
        admin_connections.store, "replace_tool_policies", replace_policies
    )
    monkeypatch.setattr(
        admin_connections.store,
        "list_tool_policies",
        lambda connection_id: list(captured["policies"]),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "activate_declarative_revision",
        activate_revision,
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    class Resolver:
        def execution_context(self, ctx):
            return ConnectionContext(
                connection=replace(
                    state["record"], public_config=dict(ctx.public_config)
                ),
                credentials=dict(captured["credentials"] or {}),
            )

    class Runtime:
        def list_enabled_tools(self, context):
            enabled = {
                policy.tool_name
                for policy in captured["policies"]
                if policy.enabled
            }
            return tuple(
                tool
                for tool in compiled.connector_spec().tools
                if tool.tool_key in enabled
            )

        async def execute(
            self,
            context,
            tool_key,
            args,
            *,
            bypass_cache=False,
        ):
            captured["executed"].append((tool_key, args))
            captured["bypass_cache"].append(bypass_cache)
            if execute_error is not None:
                raise execute_error
            return SimpleNamespace(status="ok")

    client.app.state.mcp_gateway = SimpleNamespace(
        resolver=Resolver(),
        _runtime=Runtime(),
    )
    digest = admin_connections._openapi_analysis_digest(
        state["record"], compiled
    )
    body = {
        "spec_id": compiled.spec_id,
        "revision": 1,
        "expected_config_version": 8,
        "analysis_digest": digest,
        "credentials": {"api_key": "quick-secret-value"},
        "enabled_write_tools": ["items.create"],
    }
    return state, captured, body


def _client(monkeypatch, *, authed=True):
    app = FastAPI()
    app.state.connector_registry = ConnectorRegistry([_Connector()])
    app.state.connection_sync_orchestrator = SimpleNamespace()
    app.include_router(admin_connections.router)
    monkeypatch.setattr(admin, "_is_authed", lambda request: authed)
    return TestClient(app)


def test_connection_api_requires_admin_session(monkeypatch):
    client = _client(monkeypatch, authed=False)
    assert client.get("/admin/tenants/tenant-a/connections").status_code == 401
    assert client.post("/admin/tenants/tenant-a/connections", json={}).status_code == 401


@pytest.mark.parametrize(
    ("path", "payload"),
    (
        (
            "/admin/connections/conn-a/openapi/analyze",
            {"document": {"openapi": "3.0.3", "paths": {}}},
        ),
        (
            "/admin/connections/conn-a/openapi/activate",
            {
                "spec_id": "quick-analysis",
                "revision": 1,
                "expected_config_version": 8,
                "analysis_digest": "a" * 64,
                "credentials": {},
                "enabled_write_tools": [],
            },
        ),
    ),
)
def test_quick_openapi_admin_mutations_require_same_origin_before_lookup(
    monkeypatch,
    path,
    payload,
):
    calls = []
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda *args: calls.append(args),
    )
    client = _client(monkeypatch)

    missing = client.post(path, json=payload)
    cross_site = client.post(
        path,
        json=payload,
        headers={"Origin": "https://attacker.invalid"},
    )

    assert missing.status_code == 403
    assert cross_site.status_code == 403
    assert calls == []


def test_quick_openapi_analyze_imports_validates_and_publishes(monkeypatch):
    client = _client(monkeypatch)
    spec_id = "quick-" + "a" * 32
    initial = _record(
        connector_key="http_declarative",
        status="draft",
        public_config={},
        config_version=7,
    )
    state = {"record": initial, "revision": None}
    calls = []
    template = _quick_openapi_revision(status="draft")
    derived_spec = template.connector_spec()
    credential_properties = dict(derived_spec.credential_schema["properties"])
    candidate_spec = replace(
        derived_spec,
        credential_schema={
            "type": "object",
            "properties": credential_properties,
            "required": sorted(credential_properties),
            "additionalProperties": False,
        },
    )
    monkeypatch.setattr(
        admin_connections.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="a" * 32),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: state["record"],
    )

    def importer(document, **kwargs):
        calls.append(("import", kwargs["spec_id"], kwargs["revision"]))
        return replace(
            template,
            spec_id=kwargs["spec_id"],
            revision=kwargs["revision"],
        )

    def save_revision(revision, **kwargs):
        assert kwargs["expected_config_version"] == 7
        state["revision"] = revision

    def publish_revision(
        requested_spec_id,
        revision,
        tenant_id,
        connection_id,
        **kwargs,
    ):
        calls.append(("publish", requested_spec_id, revision))
        assert kwargs["expected_config_version"] == 7
        state["revision"] = replace(state["revision"], status="published")
        state["record"] = replace(
            state["record"],
            public_config={"spec_id": requested_spec_id, "revision": revision},
            config_version=8,
        )
        return state["revision"]

    monkeypatch.setattr(admin_connections, "import_openapi_revision", importer)
    monkeypatch.setattr(
        admin_connections.store, "save_declarative_revision", save_revision
    )
    monkeypatch.setattr(
        admin_connections.store,
        "get_declarative_revision",
        lambda *args: state["revision"],
    )
    monkeypatch.setattr(
        admin_connections.store,
        "publish_declarative_revision",
        publish_revision,
    )
    monkeypatch.setattr(
        admin_connections,
        "_declarative_candidate_spec",
        lambda *args: candidate_spec,
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.post(
        "/admin/connections/conn-a/openapi/analyze",
        json={"document": {"openapi": "3.0.3", "secret": "raw-secret"}},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["spec_id"] == spec_id
    assert result["revision"] == 1
    assert result["status"] == "published"
    assert result["config_version"] == 8
    assert len(result["analysis_digest"]) == 64
    assert result["credential_schema"]["properties"] == {
        "api_key": {"type": "string", "writeOnly": True}
    }
    assert result["credential_schema"]["required"] == ["api_key"]
    assert result["credential_schema"]["additionalProperties"] is False
    assert {
        tool["tool_key"]: tool["operation_kind"]
        for tool in result["preview"]["tools"]
    } == {
        "health.check": "read",
        "items.create": "write",
        "items.archive": "write",
    }
    assert calls == [("import", spec_id, 1), ("publish", spec_id, 1)]
    assert "raw-secret" not in repr(result)


def test_quick_openapi_activate_builds_complete_safe_policies(monkeypatch):
    client = _client(monkeypatch)
    state, captured, body = _install_quick_activation(monkeypatch, client)

    response = client.post(
        "/admin/connections/conn-a/openapi/activate",
        json=body,
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200
    assert state["record"].status == "active"
    assert state["record"].config_version == 11
    assert captured["activated"] == [("quick-analysis", 1)]
    assert captured["executed"] == [("health.check", {})]
    assert captured["bypass_cache"] == [True]
    assert [
        (policy.tool_name, policy.enabled, policy.policy)
        for policy in captured["policies"]
    ] == [
        ("health.check", True, {"allow_write": False}),
        ("items.create", True, {"allow_write": True}),
        ("items.archive", False, {"allow_write": False}),
    ]
    assert "quick-secret-value" not in response.text


@pytest.mark.parametrize(
    "enabled_write_tools",
    [["unknown.tool"], ["health.check"], ["items.create", "items.create"]],
)
def test_quick_openapi_activate_rejects_invalid_write_confirmation(
    monkeypatch,
    enabled_write_tools,
):
    client = _client(monkeypatch)
    state, captured, body = _install_quick_activation(monkeypatch, client)
    body["enabled_write_tools"] = enabled_write_tools

    response = client.post(
        "/admin/connections/conn-a/openapi/activate",
        json=body,
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid write tool selection"
    assert state["record"].config_version == 8
    assert captured["credentials"] is None
    assert captured["policies"] == []


def test_quick_openapi_activate_rejects_stale_version_and_digest(monkeypatch):
    client = _client(monkeypatch)
    state, captured, body = _install_quick_activation(monkeypatch, client)

    stale_version = client.post(
        "/admin/connections/conn-a/openapi/activate",
        json={**body, "expected_config_version": 7},
        headers={"Origin": "http://testserver"},
    )
    stale_digest = client.post(
        "/admin/connections/conn-a/openapi/activate",
        json={**body, "analysis_digest": "0" * 64},
        headers={"Origin": "http://testserver"},
    )

    assert stale_version.status_code == 409
    assert stale_version.json()["detail"] == "connection configuration changed"
    assert stale_digest.status_code == 409
    assert stale_digest.json()["detail"] == "openapi analysis changed"
    assert state["record"].config_version == 8
    assert captured["credentials"] is None


@pytest.mark.parametrize(
    ("credential_version_increment", "policy_version_increment"),
    ((2, 1), (1, 2)),
)
def test_quick_openapi_activate_rejects_concurrent_mutation_between_stages(
    monkeypatch,
    credential_version_increment,
    policy_version_increment,
):
    client = _client(monkeypatch)
    state, captured, body = _install_quick_activation(
        monkeypatch,
        client,
        credential_version_increment=credential_version_increment,
        policy_version_increment=policy_version_increment,
    )

    response = client.post(
        "/admin/connections/conn-a/openapi/activate",
        json=body,
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "connection configuration changed"
    assert state["record"].status == "draft"
    assert captured["activated"] == []
    assert captured["executed"] == []


def test_quick_openapi_test_failure_never_activates_or_leaks_sensitive_detail(
    monkeypatch,
):
    client = _client(monkeypatch)
    state, captured, body = _install_quick_activation(
        monkeypatch,
        client,
        execute_error=RuntimeError(
            "upstream response exposed quick-secret-value"
        ),
    )

    response = client.post(
        "/admin/connections/conn-a/openapi/activate",
        json=body,
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "connection test failed"
    assert state["record"].status == "draft"
    assert captured["activated"] == []
    assert "quick-secret-value" not in response.text


def test_validated_update_passes_owned_version_to_atomic_store_mutation(monkeypatch):
    client = _client(monkeypatch)
    record = _record(config_version=7)
    captured = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)

    def update(*args, **kwargs):
        captured.append(kwargs["expected_config_version"])
        return replace(record, display_name="Updated", config_version=8)

    monkeypatch.setattr(admin_connections.store, "update_connection", update)
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.put(
        "/admin/connections/conn-a",
        json={
            "display_name": "Updated",
            "data_mode": "direct",
            "public_config": {"base_url": "https://api.example.com"},
            "status": "active",
        },
    )

    assert response.status_code == 200
    assert captured == [7]


def test_store_version_conflict_is_a_safe_http_409(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: _record())

    def conflict(*args, **kwargs):
        raise admin_connections.store.ConnectionVersionConflictError

    monkeypatch.setattr(admin_connections.store, "update_connection", conflict)

    response = client.put(
        "/admin/connections/conn-a",
        json={
            "display_name": "Updated",
            "data_mode": "direct",
            "public_config": {"base_url": "https://api.example.com"},
            "status": "active",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "connection configuration changed"}


def test_validated_credentials_and_policies_pass_owned_version_to_store(monkeypatch):
    client = _client(monkeypatch)
    record = _record(config_version=7)
    captured = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections.store,
        "replace_credentials",
        lambda *args, **kwargs: captured.append(
            ("credentials", kwargs["expected_config_version"])
        )
        or True,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "replace_tool_policies",
        lambda *args, **kwargs: captured.append(
            ("policies", kwargs["expected_config_version"])
        )
        or True,
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    credentials = client.put(
        "/admin/connections/conn-a/credentials",
        json={"credentials": {"api_key": "credential-secret"}},
    )
    policies = client.put(
        "/admin/connections/conn-a/tools",
        json={"policies": [{"tool_key": "items.list", "enabled": True}]},
    )

    assert credentials.status_code == 200
    assert policies.status_code == 200
    assert captured == [("credentials", 7), ("policies", 7)]


def test_tenant_scoped_lookup_returns_generic_404(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: None)
    response = client.get("/admin/tenants/tenant-a/connections/conn-b")
    assert response.status_code == 404
    assert response.json() == {"detail": "connection not found"}


def test_create_returns_raw_token_once_and_redacts_credentials(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        admin_connections.store,
        "create_connection_with_token",
        lambda record, credentials: (
            record,
            IssuedToken("token-id", "mcp_one_time_secret", "abc123"),
        ),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)
    response = client.post(
        "/admin/tenants/tenant-a/connections",
        json={
            "connector_key": "sample",
            "display_name": "Sample",
            "data_mode": "direct",
            "public_config": {"base_url": "https://api.example.com"},
            "credentials": {"api_key": "credential-secret"},
        },
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["initial_token"] == "mcp_one_time_secret"
    assert "credential-secret" not in repr(body)
    assert "credentials" not in body["connection"]


def test_list_and_detail_never_return_raw_tokens_or_secret_config(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(admin_connections.store, "list_connections", lambda tenant_id: [_record()])
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: _record())
    monkeypatch.setattr(
        admin_connections.store,
        "list_connection_tokens",
        lambda connection_id: [{"token_id": "t1", "prefix": "abc123", "label": "cli"}],
    )
    listed = client.get("/admin/tenants/tenant-a/connections").json()
    detail = client.get("/admin/tenants/tenant-a/connections/conn-a").json()
    assert "token_hmac" not in repr((listed, detail))
    assert "raw_value" not in repr((listed, detail))
    assert detail["tokens"] == [{"token_id": "t1", "prefix": "abc123", "label": "cli"}]


def test_admin_connection_token_reveal_is_no_store_and_audited(monkeypatch):
    client = _client(monkeypatch)
    events = []
    calls = []
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: _record()
        if connection_id == "conn-a"
        and (tenant_id is None or tenant_id == "tenant-a")
        else None,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "reveal_connection_token",
        lambda connection_id, tenant_id, token_id: calls.append(
            (connection_id, tenant_id, token_id)
        )
        or "mcp_revealed_once",
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: events.append(event) or True)
    admin_connections.reset_reveal_limiter()

    scoped = client.post(
        "/admin/tenants/tenant-a/connections/conn-a/tokens/token-a/reveal",
        headers={"Origin": "http://testserver"},
    )
    global_response = client.post(
        "/admin/connections/conn-a/tokens/token-a/reveal",
        headers={"Origin": "http://testserver"},
    )

    assert scoped.status_code == global_response.status_code == 200
    assert scoped.json() == global_response.json() == {"token": "mcp_revealed_once"}
    assert scoped.headers["cache-control"] == "no-store"
    assert global_response.headers["cache-control"] == "no-store"
    assert calls == [("conn-a", "tenant-a", "token-a")] * 2
    assert [event.event_name for event in events] == ["connection_token_reveal"] * 2
    assert all("mcp_revealed_once" not in repr(event) for event in events)


def test_admin_connection_token_reveal_rate_limit_is_no_store_and_audited(
    monkeypatch,
):
    client = _client(monkeypatch)
    events = []
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: _record(),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "reveal_connection_token",
        lambda *_args: "mcp_rate_limited",
    )
    monkeypatch.setattr(
        admin_connections,
        "write_event",
        lambda event: events.append(event) or True,
    )
    monkeypatch.setattr(
        admin_connections,
        "_reveal_limiter",
        admin_connections.RevealLimiter(limit=2, window_seconds=60),
    )

    responses = [
        client.post(
            "/admin/connections/conn-a/tokens/token-a/reveal",
            headers={"Origin": "http://testserver"},
        )
        for _ in range(3)
    ]

    assert [response.status_code for response in responses] == [200, 200, 429]
    assert responses[-1].headers["cache-control"] == "no-store"
    assert responses[-1].json() == {"detail": "request rate limit exceeded"}
    assert [event.result_status for event in events] == ["ok", "ok", "denied"]
    assert "mcp_rate_limited" not in repr(events)


@pytest.mark.parametrize("audit_behavior", ["false", "throw"])
def test_admin_connection_token_reveal_requires_accepted_success_audit(
    monkeypatch, audit_behavior
):
    client = _client(monkeypatch)
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: _record())
    monkeypatch.setattr(
        admin_connections.store,
        "reveal_connection_token",
        lambda *_args: "mcp_must_not_escape",
    )

    def audit(_event):
        if audit_behavior == "throw":
            raise RuntimeError("audit unavailable with mcp_must_not_escape")
        return False

    monkeypatch.setattr(admin_connections, "write_event", audit)
    admin_connections.reset_reveal_limiter()

    response = client.post(
        "/admin/connections/conn-a/tokens/token-a/reveal",
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "connection operation failed"}
    assert "mcp_must_not_escape" not in response.text


def test_admin_connection_list_and_detail_project_authoritative_alias(monkeypatch):
    client = _client(monkeypatch)
    record = _record(connection_alias="renamed_admin_alias")
    monkeypatch.setattr(admin_connections.store, "list_connections", lambda tenant_id: [record])
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: record)
    monkeypatch.setattr(admin_connections.store, "list_connection_tokens", lambda cid: [])

    listed = client.get("/admin/tenants/tenant-a/connections").json()["items"][0]
    detail = client.get("/admin/connections/conn-a").json()["connection"]

    expected_keys = {
        "connection_id", "connection_alias", "tenant_id", "connector_key",
        "display_name", "status", "data_mode", "public_config", "config_version",
    }
    assert set(listed) == expected_keys
    assert set(detail) == expected_keys
    assert listed["connection_alias"] == "renamed_admin_alias"
    assert detail["connection_alias"] == "renamed_admin_alias"
    assert "credentials" not in repr((listed, detail))
    assert "raw_value" not in repr((listed, detail))


def test_declarative_fallback_projection_keeps_alias_and_redacts_config(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connection_alias="declarative_alias",
        connector_key="http_declarative",
        status="draft",
        public_config={"unavailable_secret": "must-not-project"},
    )
    monkeypatch.setattr(admin_connections.store, "list_connections", lambda tenant_id: [record])

    response = client.get("/admin/tenants/tenant-a/connections")

    assert response.status_code == 200
    projected = response.json()["items"][0]
    assert projected == {
        "connection_id": "conn-a",
        "connection_alias": "declarative_alias",
        "tenant_id": "tenant-a",
        "connector_key": "http_declarative",
        "display_name": "Sample",
        "status": "draft",
        "data_mode": "direct",
        "public_config": {},
        "config_version": 1,
    }
    assert "must-not-project" not in repr(projected)


def test_invalid_config_credentials_and_policy_fail_before_store(monkeypatch):
    client = _client(monkeypatch)
    calls = []
    monkeypatch.setattr(
        admin_connections.store,
        "create_connection_with_token",
        lambda *a, **k: calls.append("create"),
    )

    invalid_config = client.post(
        "/admin/tenants/tenant-a/connections",
        json={
            "connector_key": "sample",
            "display_name": "Sample",
            "data_mode": "direct",
            "public_config": {},
            "credentials": {"api_key": "credential-secret"},
        },
    )
    invalid_credentials = client.post(
        "/admin/tenants/tenant-a/connections",
        json={
            "connector_key": "sample",
            "display_name": "Sample",
            "data_mode": "direct",
            "public_config": {"base_url": "https://api.example.com"},
            "credentials": {"api_key": "short"},
        },
    )
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: _record())
    invalid_policy = client.put(
        "/admin/tenants/tenant-a/connections/conn-a/tools",
        json={"policies": [{"tool_key": "unknown", "enabled": True}]},
    )

    assert invalid_config.status_code == 422
    assert invalid_credentials.status_code == 422
    assert invalid_policy.status_code == 422
    assert calls == []


def test_global_connection_detail_uses_resolved_tenant_and_redacts_config(monkeypatch):
    client = _client(monkeypatch)
    spec = client.app.state.connector_registry.validated_spec("sample")
    object.__setattr__(
        spec,
        "config_schema",
        {
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "api_token": {"type": "string", "x-sensitive": True},
            },
        },
    )
    record = _record(
        public_config={
            "base_url": "https://api.example.com",
            "api_token": "config-secret",
        }
    )
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: record)
    monkeypatch.setattr(admin_connections.store, "list_connection_tokens", lambda cid: [])

    response = client.get("/admin/connections/conn-a")

    assert response.status_code == 200
    assert "config-secret" not in repr(response.json())
    assert response.json()["connection"]["public_config"] == {
        "base_url": "https://api.example.com"
    }


def test_declarative_revision_validate_publish_activate_lifecycle(monkeypatch):
    client = _client(monkeypatch)
    record = _record(connector_key="http_declarative", status="draft")
    record = replace(
        record, public_config={"spec_id": "spec-a", "revision": 2}
    )
    revision = _compiled_revision_stub(
        spec_id="spec-a", revision=2, status="draft"
    )
    published = SimpleNamespace(spec_id="spec-a", revision=2, status="published")
    calls = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: record)
    monkeypatch.setattr(
        admin_connections.store,
        "get_declarative_revision",
        lambda *args: revision,
        raising=False,
    )
    monkeypatch.setattr(admin_connections, "validate_revision", lambda item, data_mode=None: item)
    monkeypatch.setattr(
        admin_connections,
        "_declarative_candidate_spec",
        lambda *args: ConnectorSpec(connector_key="http_declarative", tools=()),
    )
    monkeypatch.setattr(
        admin_connections, "_load_connection_credentials", lambda *args: {}
    )
    monkeypatch.setattr(
        admin_connections.store,
        "publish_declarative_revision",
        lambda *args, **kwargs: calls.append("publish") or published,
        raising=False,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "activate_declarative_revision",
        lambda *args, **kwargs: calls.append("activate")
        or replace(record, status="active", config_version=2),
        raising=False,
    )
    monkeypatch.setattr(admin_connections.store, "list_tool_policies", lambda cid: [])
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    validated = client.post(
        "/admin/tenants/tenant-a/connections/conn-a/specs/spec-a/revisions/2/validate"
    )
    published_response = client.post(
        "/admin/tenants/tenant-a/connections/conn-a/specs/spec-a/revisions/2/publish"
    )
    activated = client.post(
        "/admin/tenants/tenant-a/connections/conn-a/specs/spec-a/revisions/2/activate"
    )

    assert validated.json()["valid"] is True
    assert published_response.json()["status"] == "published"
    assert activated.json()["connection"]["config_version"] == 2
    assert calls == ["publish", "activate"]


def test_declarative_import_and_validate_return_compiled_safe_step_preview(monkeypatch):
    client = _client(monkeypatch)
    record = _record(connector_key="http_declarative", status="draft", public_config={})
    document = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/items/{item_id}": {"get": {
                "operationId": "items.get",
                "x-mcp-name": "items_get",
                "summary": "Get item",
                "parameters": [{
                    "name": "item_id",
                    "in": "path",
                    "required": True,
                    "schema": {
                        "type": "string",
                        "maxLength": 128,
                    },
                }],
                "x-output-mappings": {"public_title": "/internal_title"},
                "responses": {"200": {
                    "description": "ok",
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"internal_title": {"type": "string"}},
                    }}},
                }},
            }},
            "/items": {"post": {
            "operationId": "items.create",
            "x-mcp-name": "items_create",
            "summary": "Create item operation",
            "x-write-enabled": True,
            "parameters": [{"name": "name", "in": "query", "required": True, "schema": {"type": "string"}}],
            "x-output-mappings": {"item_id": "/id"},
            "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"type": "object", "properties": {"id": {"type": "string"}}}}}}},
        }}},
        "x-mcp-tools": [{
            "tool_key": "items.create.safe", "mcp_name": "items_create_safe", "description": "Create item",
            "input_schema": {"type": "object", "$comment": "Bearer schema-secret", "properties": {
                "name": {"type": "string", "description": "Authorization: schema-secret", "default": "secret-default", "x-sample": "secret-sample"},
                "metadata": {"type": "object", "description": "token nested-secret", "$comment": "password nested-secret", "x-auth-header": "Bearer nested-secret", "properties": {"label": {"type": "string", "description": "cookie nested-secret"}}, "additionalProperties": False},
                "authorization_header": {"type": "string"},
            }, "required": ["name"], "additionalProperties": False},
            "output_schema": {"type": "object", "properties": {"id": {"type": "string", "example": "secret-output"}}, "required": ["id"], "additionalProperties": False},
            "steps": [{"step_id": "create", "operation_key": "items.create", "input_map": {"name": "$input.name"}, "output_mappings": {"id": "item_id"}}],
            "result_map": {"id": "$steps.create.id"},
        }],
    }
    compiled = import_openapi_revision(document, spec_id="spec-a", revision=2, tenant_id="tenant-a", connection_id="conn-a")
    unsafe_document = deepcopy(document)
    unsafe_document["x-mcp-tools"][0]["description"] = "Bearer preview-secret"
    unsafe_compiled = import_openapi_revision(unsafe_document, spec_id="spec-a", revision=3, tenant_id="tenant-a", connection_id="conn-a")
    stored = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(admin_connections, "import_openapi_revision", lambda *args, **kwargs: compiled)
    monkeypatch.setattr(admin_connections.store, "save_declarative_revision", lambda revision, **kwargs: stored.append(revision))
    monkeypatch.setattr(admin_connections.store, "get_declarative_revision", lambda *args: compiled)
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    imported = client.post("/admin/connections/conn-a/specs/import", json={"document": document, "spec_id": "spec-a", "revision": 2})
    validated = client.post("/admin/connections/conn-a/specs/spec-a/revisions/2/validate")

    expected = {
        "operations": [
            {
                "operation_key": "items.get",
                "mcp_name": "items_get",
                "description": "Get item",
                "operation_kind": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string", "maxLength": 128},
                    },
                    "required": ["item_id"],
                    "additionalProperties": False,
                },
                "output_names": ["public_title"],
            },
            {
                "operation_key": "items.create",
                "mcp_name": "items_create",
                "description": "Create item operation",
                "operation_kind": "write",
                "input_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "output_names": ["item_id"],
            },
        ],
        "tools": [{
            "tool_key": "items.create.safe", "mcp_name": "items_create_safe", "description": "Create item", "operation_kind": "write",
            "input_schema": {"type": "object", "properties": {
                "name": {"type": "string"},
                "metadata": {"type": "object", "properties": {"label": {"type": "string"}}, "additionalProperties": False},
                "authorization_header": {"type": "string"},
            }, "required": ["name"], "additionalProperties": False},
            "output_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False},
            "steps": [{"step_id": "create", "operation_key": "items.create", "operation_kind": "write"}],
        }]
    }
    assert imported.status_code == 201
    assert validated.status_code == 200
    assert imported.json()["preview"] == expected
    assert validated.json()["preview"] == expected
    assert stored == [compiled]
    assert "secret-" not in repr((imported.json(), validated.json()))
    assert [
        set(operation) for operation in validated.json()["preview"]["operations"]
    ] == [{
        "operation_key",
        "mcp_name",
        "description",
        "operation_kind",
        "input_schema",
        "output_names",
    }] * 2
    serialized = repr((imported.json(), validated.json()))
    for forbidden in (
        "https://api.example.com",
        "/items/{item_id}",
        "/internal_title",
        "path",
        "query",
        "method",
        "base_url",
        "auth_scheme",
        "timeout_ms",
        "pagination",
    ):
        assert forbidden not in serialized
    assert admin_connections._declarative_preview(unsafe_compiled)["tools"][0]["description"] == ""
    assert "preview-secret" not in repr(admin_connections._declarative_preview(unsafe_compiled))


def test_declarative_preview_rejects_duplicate_operation_keys_and_output_names():
    document = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/items": {"get": {
                "operationId": "items.get",
                "responses": {"200": {
                    "description": "ok",
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                    }}},
                }},
            }},
        },
    }
    compiled = import_openapi_revision(document)
    corrupt_revisions = []

    duplicate_operation = copy(compiled)
    object.__setattr__(
        duplicate_operation,
        "operations",
        (duplicate_operation.operations[0], duplicate_operation.operations[0]),
    )
    corrupt_revisions.append(duplicate_operation)

    duplicate_output = copy(compiled)
    duplicate_output_operation = copy(compiled.operations[0])
    object.__setattr__(
        duplicate_output_operation,
        "output_mappings",
        (
            OutputMapping(name="id", pointer="/id"),
            OutputMapping(name="id", pointer="/id"),
        ),
    )
    object.__setattr__(duplicate_output, "operations", (duplicate_output_operation,))
    corrupt_revisions.append(duplicate_output)

    for corrupt in corrupt_revisions:
        try:
            admin_connections._declarative_preview(corrupt)
        except HTTPException as exc:
            assert exc.status_code == 409
            assert exc.detail == "declarative revision is unavailable"
        else:
            raise AssertionError("corrupt revision preview must fail closed")


def test_declarative_preview_revalidates_compiled_operation_invariants():
    invalid_pointer = _compiled_operation_stub()
    object.__setattr__(
        invalid_pointer.output_mappings[0],
        "pointer",
        "/",
    )
    invalid_method = _compiled_operation_stub()
    object.__setattr__(invalid_method, "method", "TRACE")
    invalid_schema = _compiled_operation_stub(
        input_schema={"type": "object", "properties": {}}
    )
    object.__setattr__(
        invalid_schema.input_mappings[0],
        "schema",
        {"type": "object", "properties": []},
    )

    for operation in (invalid_pointer, invalid_method, invalid_schema):
        with pytest.raises(HTTPException) as caught:
            admin_connections._declarative_preview(
                SimpleNamespace(operations=(operation,), tools=())
            )
        assert caught.value.status_code == 409
        assert caught.value.detail == "declarative revision is unavailable"


def test_declarative_preview_rejects_duplicate_and_cross_colliding_identities():
    duplicate_mcp_name = (
        _compiled_operation_stub(tool_key="items.first", mcp_name="items_shared"),
        _compiled_operation_stub(tool_key="items.second", mcp_name="items_shared"),
    )
    cross_collision = (
        _compiled_operation_stub(tool_key="items.first", mcp_name="items_alias"),
        _compiled_operation_stub(tool_key="items_alias", mcp_name="items_second"),
    )

    for operations in (duplicate_mcp_name, cross_collision):
        with pytest.raises(HTTPException) as caught:
            admin_connections._declarative_preview(
                SimpleNamespace(operations=operations, tools=())
            )
        assert caught.value.status_code == 409
        assert caught.value.detail == "declarative revision is unavailable"


def test_safe_preview_schema_rejects_cycles_depth_and_url_valued_type():
    cyclic = {"type": "object"}
    cyclic["properties"] = {"self": cyclic}
    deep = {"type": "string"}
    for index in range(10):
        deep = {
            "type": "object",
            "properties": {f"level_{index}": deep},
        }

    for schema in (
        cyclic,
        deep,
        {"type": "https://private.example.invalid"},
    ):
        operation = _compiled_operation_stub(input_schema={"type": "string"})
        object.__setattr__(operation.input_mappings[0], "schema", schema)
        with pytest.raises(HTTPException) as caught:
            admin_connections._declarative_preview(
                SimpleNamespace(operations=(operation,), tools=())
            )
        assert caught.value.status_code == 409
        assert caught.value.detail == "declarative revision is unavailable"


@pytest.mark.parametrize(
    "schema",
    (
        {"properties": []},
        {"properties": {"item_id": "not-a-schema"}},
        {"items": []},
        {"required": "item_id"},
        {"properties": {"item_id": {}}, "required": ["item_id", "item_id"]},
        {"properties": {}, "required": ["missing"]},
        {"additionalProperties": "yes"},
        {"minLength": "1"},
        {"uniqueItems": 1},
        {"minimum": True},
    ),
)
def test_safe_preview_schema_rejects_malformed_keyword_values(schema):
    with pytest.raises(HTTPException) as caught:
        admin_connections._safe_preview_schema(schema)
    assert caught.value.status_code == 409
    assert caught.value.detail == "declarative revision is unavailable"


def test_preview_preserves_boolean_and_numeric_exclusive_bounds_on_both_paths():
    boolean_bounds = {
        "type": "integer",
        "minimum": 0,
        "maximum": 10,
        "exclusiveMinimum": True,
        "exclusiveMaximum": False,
    }
    numeric_bounds = {
        "type": "number",
        "minimum": 0,
        "maximum": 10,
        "exclusiveMinimum": 0.5,
        "exclusiveMaximum": 9.5,
    }
    preview = _compiled_preview_with_bounds(
        operation_schema=numeric_bounds,
        tool_property_schema=boolean_bounds,
    )
    reversed_preview = _compiled_preview_with_bounds(
        operation_schema=boolean_bounds,
        tool_property_schema=numeric_bounds,
    )

    assert preview["operations"][0]["input_schema"]["properties"]["item_id"] == numeric_bounds
    assert preview["tools"][0]["input_schema"]["properties"]["item_id"] == boolean_bounds
    assert reversed_preview["operations"][0]["input_schema"]["properties"]["item_id"] == boolean_bounds
    assert reversed_preview["tools"][0]["input_schema"]["properties"]["item_id"] == numeric_bounds


@pytest.mark.parametrize(
    ("keyword", "value"),
    (
        ("minimum", True),
        ("maximum", False),
        ("minimum", float("nan")),
        ("maximum", float("inf")),
        ("minimum", float("-inf")),
        ("minimum", "0"),
        ("exclusiveMinimum", float("nan")),
        ("exclusiveMaximum", float("inf")),
        ("exclusiveMinimum", float("-inf")),
        ("exclusiveMaximum", "10"),
    ),
)
@pytest.mark.parametrize("path", ("operation", "tool"))
def test_preview_rejects_invalid_bound_values_on_both_paths(keyword, value, path):
    valid_schema = {"type": "number", "minimum": 0, "maximum": 10}
    operation = _compiled_operation_stub(input_schema=valid_schema)

    invalid_schema = {"type": "number", keyword: value}
    if path == "operation":
        object.__setattr__(operation.input_mappings[0], "schema", invalid_schema)
        revision = SimpleNamespace(operations=(operation,), tools=())
    else:
        valid_preview_operation = _compiled_operation_stub(input_schema=valid_schema)
        operation_step = DeclarativeStep(
            step_id="operation",
            operation_key=valid_preview_operation.tool_key,
            input_mappings={
                "item_id": ValueRef(source="input", field="item_id"),
            },
            output_mappings={"id": "id"},
        )
        tool = DeclarativeTool(
            tool_key="items.get.safe",
            mcp_name="items_get_safe",
            description="Get item safely",
            input_schema={
                "type": "object",
                "properties": {"item_id": valid_schema},
                "required": ["item_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
            steps=(operation_step,),
            result_map={
                "id": ValueRef(
                    source="steps", step_id="operation", field="id"
                ),
            },
        )
        object.__setattr__(
            tool,
            "input_schema",
            {
                "type": "object",
                "properties": {"item_id": invalid_schema},
                "required": ["item_id"],
                "additionalProperties": False,
            },
        )
        revision = SimpleNamespace(
            operations=(valid_preview_operation,), tools=(tool,)
        )

    with pytest.raises(HTTPException) as caught:
        admin_connections._declarative_preview(revision)
    assert caught.value.status_code == 409
    assert caught.value.detail == "declarative revision is unavailable"


def test_preview_description_keeps_bounded_ordinary_prose():
    for description in (
        "Get basic account details",
        "List active sessions for the account",
        "Explain bearer authentication requirements",
    ):
        assert admin_connections._safe_preview_description(description) == description


def test_preview_description_redacts_common_secret_shapes():
    secrets = (
        "Use Bearer abc12345-secret-value",
        "JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
        "Stripe sk_live_51ABCDEF1234567890",
        "GitHub ghp_1234567890abcdefghijklmnopqrstuv",
        "Slack " + "xox" + "b-123456789012-123456789012-abcdefghijklmnop",
        "AWS AKIAIOSFODNN7EXAMPLE",
        "client_secret=correct-horse-battery-staple",
        "key 8fK2mP9qR4sT7vW1xY6zA3bC5dE0gH",
    )
    for description in secrets:
        assert admin_connections._safe_preview_description(description) == ""


def test_disabled_connection_cannot_run_manual_sync(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda cid, tid=None: _record(status="disabled", data_mode="stored"),
    )
    response = client.post("/admin/tenants/tenant-a/connections/conn-a/sync")
    assert response.status_code == 409


def test_global_mutation_alias_resolves_owner_before_disable(monkeypatch):
    client = _client(monkeypatch)
    record = _record()
    calls = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: record)
    monkeypatch.setattr(
        admin_connections.store,
        "disable_connection",
        lambda cid, tid: calls.append((cid, tid)) or replace(record, status="disabled"),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.post("/admin/connections/conn-a/disable")

    assert response.status_code == 200
    assert calls == [("conn-a", "tenant-a")]


def test_tool_policy_payload_is_accepted_by_runtime_guard(monkeypatch):
    client = _client(monkeypatch)
    record = _record()
    captured = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections.store,
        "replace_tool_policies",
        lambda cid, tid, policies, **kwargs: captured.extend(policies) or True,
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.put(
        "/admin/connections/conn-a/tools",
        json={
            "policies": [
                {
                    "tool_key": "items.list",
                    "enabled": True,
                    "rate_limit_per_minute": 12,
                }
            ]
        },
    )

    tool = client.app.state.connector_registry.validated_spec("sample").tools[0]
    resolved = PolicyGuard().resolve(tool, captured[0])
    assert response.status_code == 200
    assert resolved.rate_limit.limit == 12
    assert resolved.rate_limit.window_seconds == 60


def test_explicit_token_rotation_returns_new_raw_token_once(monkeypatch):
    client = _client(monkeypatch)
    record = _record()
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections.store,
        "rotate_token",
        lambda cid, tid, label="": IssuedToken(
            "new-token", "mcp_rotated_once", "rotated-prefix"
        ),
        raising=False,
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.post(
        "/admin/connections/conn-a/tokens/rotate", json={"label": "cli"}
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["token"] == "mcp_rotated_once"


def test_safe_config_projects_only_declared_nested_and_array_fields():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "nested": {
                "type": "object",
                "properties": {"safe": {"type": "string"}},
                "additionalProperties": False,
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"safe": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
            "metadata": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        },
        "additionalProperties": False,
    }
    value = {
        "name": "visible",
        "unknown": "must-not-leak",
        "nested": {"safe": "visible", "extra": "must-not-leak"},
        "items": [{"safe": "visible", "secret": "must-not-leak"}],
        "metadata": {"region": "cn", "token": "must-not-leak"},
    }

    projected = admin_connections._safe_config(value, schema)

    assert projected == {
        "name": "visible",
        "nested": {"safe": "visible"},
        "items": [{"safe": "visible"}],
        "metadata": {"region": "cn"},
    }
    assert "must-not-leak" not in repr(projected)


def test_safe_config_omits_declared_scalars_with_wrong_runtime_types():
    projected = admin_connections._safe_config(
        {
            "name": 7,
            "count": True,
            "ratio": 2,
            "enabled": 1,
            "nullable": None,
            "items": "not-an-array",
        },
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "enabled": {"type": "boolean"},
                "nullable": {"type": "null"},
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    )

    assert projected == {"ratio": 2, "nullable": None}


def test_declarative_create_is_draft_and_does_not_require_static_registry(monkeypatch):
    client = _client(monkeypatch)
    captured = []
    monkeypatch.setattr(
        admin_connections.store,
        "create_connection_with_token",
        lambda record, credentials: (
            captured.append((record, credentials)) or record,
            IssuedToken("token-id", "mcp_once", "prefix"),
        ),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.post(
        "/admin/tenants/tenant-a/connections",
        json={
            "connector_key": "http_declarative",
            "display_name": "Declared API",
            "data_mode": "direct",
            "status": "draft",
            "public_config": {},
            "credentials": {},
        },
    )

    assert response.status_code == 201
    assert captured[0][0].status == "draft"
    assert captured[0][0].public_config == {}


def test_declarative_create_rejects_prebound_config_or_credentials(monkeypatch):
    client = _client(monkeypatch)
    for payload in (
        {"public_config": {"spec_id": "forged"}, "credentials": {}},
        {"public_config": {}, "credentials": {"api_key": "forged-secret"}},
    ):
        response = client.post(
            "/admin/tenants/tenant-a/connections",
            json={
                "connector_key": "http_declarative",
                "display_name": "Declared API",
                "data_mode": "direct",
                "status": "draft",
                **payload,
            },
        )
        assert response.status_code == 422


def test_spec_lifecycle_rejects_non_declarative_connection(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: _record())
    response = client.post(
        "/admin/connections/conn-a/specs/spec-a/revisions/1/publish"
    )
    assert response.status_code == 422


def test_active_declarative_connection_rejects_import_and_publish(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative",
        public_config={"spec_id": "spec-a", "revision": 1},
    )
    calls = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections.store,
        "save_declarative_revision",
        lambda *args, **kwargs: calls.append("import"),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "publish_declarative_revision",
        lambda *args, **kwargs: calls.append("publish"),
    )

    imported = client.post(
        "/admin/connections/conn-a/specs/import",
        json={"document": {}, "spec_id": "spec-b", "revision": 2},
    )
    published = client.post(
        "/admin/connections/conn-a/specs/spec-b/revisions/2/publish"
    )

    assert imported.status_code == 409
    assert published.status_code == 409
    assert calls == []


def test_generic_update_cannot_activate_a_disabled_declarative_connection(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative",
        status="disabled",
        public_config={
            "spec_id": "spec-a",
            "revision": 1,
            "pending_spec_id": "spec-b",
            "pending_revision": 2,
        },
        config_version=7,
    )
    calls = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections,
        "_spec_for_record",
        lambda *args: ConnectorSpec(
            connector_key="http_declarative",
            tools=(),
            config_schema={
                "type": "object",
                "properties": {
                    "spec_id": {"type": "string"},
                    "revision": {"type": "integer"},
                    "pending_spec_id": {"type": "string"},
                    "pending_revision": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            supports_data_modes=("direct",),
        ),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "update_connection",
        lambda *args, **kwargs: calls.append("update"),
    )

    response = client.put(
        "/admin/connections/conn-a",
        json={
            "display_name": "API",
            "data_mode": "direct",
            "public_config": record.public_config,
            "status": "active",
        },
    )

    assert response.status_code == 409
    assert calls == []


def test_declarative_import_passes_owned_version_to_atomic_save(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative", status="draft", config_version=7
    )
    revision = _compiled_revision_stub(
        spec_id="spec-b", revision=2, status="draft"
    )
    captured = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections,
        "import_openapi_revision",
        lambda *args, **kwargs: revision,
    )
    monkeypatch.setattr(admin_connections, "validate_revision", lambda *args, **kwargs: revision)
    monkeypatch.setattr(
        admin_connections.store,
        "save_declarative_revision",
        lambda *args, **kwargs: captured.append(kwargs.get("expected_config_version")),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.post(
        "/admin/connections/conn-a/specs/import",
        json={"document": {}, "spec_id": "spec-b", "revision": 2},
    )

    assert response.status_code == 201
    assert captured == [7]


def test_disabled_pending_revision_drives_credentials_tools_and_real_test(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative",
        status="disabled",
        public_config={
            "spec_id": "spec-a",
            "revision": 1,
            "pending_spec_id": "spec-b",
            "pending_revision": 2,
        },
        config_version=7,
    )
    pending_tool = ToolSpec(
        tool_key="new.health",
        mcp_name="new.health",
        description="pending health",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean", "example": True},
            },
            "additionalProperties": False,
        },
        operation_kind="read",
        default_timeout_ms=1000,
        cache_ttl_seconds=None,
    )
    pending_spec = ConnectorSpec(
        connector_key="http_declarative",
        tools=(pending_tool,),
        credential_schema={
            "type": "object",
            "required": ["new_key"],
            "properties": {
                "new_key": {
                    "type": "string",
                    "default": "must-not-leak",
                    "x-internal-example": "must-not-leak",
                },
            },
            "additionalProperties": False,
        },
    )
    selected = []
    mutations = []
    executed = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)

    def candidate(request, candidate_record, spec_id, revision):
        selected.append((spec_id, revision))
        assert (spec_id, revision) == ("spec-b", 2)
        return pending_spec

    monkeypatch.setattr(admin_connections, "_declarative_candidate_spec", candidate)
    monkeypatch.setattr(
        admin_connections.store,
        "replace_credentials",
        lambda *args, **kwargs: mutations.append(
            ("credentials", kwargs["expected_config_version"])
        )
        or True,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "list_tool_policies",
        lambda connection_id: [
            admin_connections.ToolPolicy(connection_id, "new.health", True, {})
        ],
    )
    monkeypatch.setattr(
        admin_connections.store,
        "replace_tool_policies",
        lambda *args, **kwargs: mutations.append(
            ("policies", kwargs["expected_config_version"])
        )
        or True,
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    class Resolver:
        def execution_context(self, ctx):
            selected.append(
                (ctx.public_config.get("spec_id"), ctx.public_config.get("revision"))
            )
            return ConnectionContext(connection=replace(record, public_config=dict(ctx.public_config)), credentials={"new_key": "secret"})

    class Runtime:
        def list_enabled_tools(self, context):
            return (pending_tool,)

        async def execute(self, context, tool_key, args):
            executed.append((context.public_config, tool_key))
            return SimpleNamespace(status="ok")

    client.app.state.mcp_gateway = SimpleNamespace(
        resolver=Resolver(), _runtime=Runtime()
    )

    credentials = client.put(
        "/admin/connections/conn-a/credentials",
        json={"credentials": {"new_key": "new-secret"}},
    )
    listed = client.get("/admin/connections/conn-a/tools")
    policies = client.put(
        "/admin/connections/conn-a/tools",
        json={"policies": [{"tool_key": "new.health", "enabled": True}]},
    )
    tested = client.post("/admin/connections/conn-a/test")

    assert [credentials.status_code, listed.status_code, policies.status_code, tested.status_code] == [200, 200, 200, 200]
    assert listed.json()["items"][0]["tool_key"] == "new.health"
    assert listed.json()["connector_key"] == "http_declarative"
    assert listed.json()["credential_schema"] == {
        "type": "object",
        "properties": {"new_key": {"type": "string"}},
        "required": ["new_key"],
        "additionalProperties": False,
    }
    assert listed.json()["items"][0]["description"] == "pending health"
    assert listed.json()["items"][0]["input_schema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert listed.json()["items"][0]["output_schema"] == {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "additionalProperties": False,
    }
    assert "must-not-leak" not in repr(listed.json())
    assert mutations == [("credentials", 7), ("policies", 7)]
    assert selected and all(item == ("spec-b", 2) for item in selected)
    assert executed == [
        (
            {
                "spec_id": "spec-b",
                "revision": 2,
                "pending_spec_id": "spec-b",
                "pending_revision": 2,
            },
            "new.health",
        )
    ]


def test_activation_requires_complete_exact_pending_tool_policies(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative",
        status="disabled",
        public_config={
            "spec_id": "spec-a",
            "revision": 1,
            "pending_spec_id": "spec-b",
            "pending_revision": 2,
        },
        config_version=7,
    )
    tools = tuple(
        ToolSpec(
            tool_key=key,
            mcp_name=key,
            description=key,
            input_schema={"type": "object"},
            output_schema=None,
            operation_kind="read",
            default_timeout_ms=1000,
            cache_ttl_seconds=None,
        )
        for key in ("new.one", "new.two")
    )
    configured = [admin_connections.ToolPolicy("conn-a", "new.one", True, {})]
    activated = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections,
        "_declarative_candidate_spec",
        lambda *args: ConnectorSpec(connector_key="http_declarative", tools=tools),
    )
    monkeypatch.setattr(admin_connections, "_load_connection_credentials", lambda *args: {})
    monkeypatch.setattr(
        admin_connections.store,
        "list_tool_policies",
        lambda connection_id: list(configured),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "activate_declarative_revision",
        lambda *args, **kwargs: activated.append(kwargs["expected_config_version"])
        or replace(record, status="active", config_version=8),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    missing = client.post(
        "/admin/connections/conn-a/specs/spec-b/revisions/2/activate"
    )
    configured[:] = [
        admin_connections.ToolPolicy("conn-a", "new.one", True, {}),
        admin_connections.ToolPolicy("conn-a", "stale.old", False, {}),
    ]
    stale = client.post(
        "/admin/connections/conn-a/specs/spec-b/revisions/2/activate"
    )
    configured[:] = [
        admin_connections.ToolPolicy("conn-a", tool.tool_key, True, {})
        for tool in tools
    ]
    complete = client.post(
        "/admin/connections/conn-a/specs/spec-b/revisions/2/activate"
    )

    assert missing.status_code == 409
    assert stale.status_code == 409
    assert complete.status_code == 200
    assert activated == [7]


def test_connection_test_executes_real_safe_read_operation(monkeypatch):
    client = _client(monkeypatch)
    record = _record()
    calls = []

    class Resolver:
        def execution_context(self, ctx):
            return ConnectionContext(connection=record, credentials={"api_key": "secret"})

    class Runtime:
        def list_enabled_tools(self, context):
            return client.app.state.connector_registry.validated_spec("sample").tools

        async def execute(self, context, tool_key, args):
            calls.append((context.connection_id, tool_key, args))
            return SimpleNamespace(status="ok", data={"ok": True})

    client.app.state.mcp_gateway = SimpleNamespace(
        resolver=Resolver(), _runtime=Runtime()
    )
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.post("/admin/connections/conn-a/test")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "ok"}
    assert calls == [("conn-a", "items.list", {})]


def test_activation_rejects_missing_revision_credentials_before_store(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative",
        status="draft",
        public_config={"spec_id": "spec-a", "revision": 1},
    )
    calls = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections,
        "_declarative_candidate_spec",
        lambda request, record, spec_id, revision: ConnectorSpec(
            connector_key="http_declarative",
            tools=(),
            credential_schema={
                "type": "object",
                "required": ["api_key"],
                "properties": {"api_key": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        raising=False,
    )
    monkeypatch.setattr(
        admin_connections,
        "_load_connection_credentials",
        lambda request, record: {},
        raising=False,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "activate_declarative_revision",
        lambda *args: calls.append("activate"),
    )

    response = client.post(
        "/admin/connections/conn-a/specs/spec-a/revisions/1/activate"
    )

    assert response.status_code == 422
    assert calls == []


def test_manual_sync_writes_safe_management_audit(monkeypatch):
    client = _client(monkeypatch)
    record = _record(data_mode="stored")
    events = []

    class Orchestrator:
        async def run_connection(self, connection):
            return SyncResult.ok(connection.connection_id, "health", {"stored": 1})

    client.app.state.connection_sync_orchestrator = Orchestrator()
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(admin_connections, "write_event", events.append)

    response = client.post("/admin/connections/conn-a/sync")

    assert response.status_code == 200
    assert any(event.event_name == "connection_sync_triggered" for event in events)
    assert "secret" not in repr(events)
