from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import admin_connections
from app.connections.models import ConnectionRecord, IssuedToken
from app.connectors.contracts import ConnectorSpec, ToolSpec
from app.connectors.declarative.models import (
    DeclarativeOperation,
    InputMapping,
    OutputMapping,
)
from app.main import create_app
from app.tenant_auth import store as tenant_auth_store
from app.tenant_auth.models import TenantPrincipal


def _record(**changes):
    record = ConnectionRecord(
        connection_id="conn-a",
        tenant_id="tenant-a",
        connector_key="sample",
        display_name="Sample",
        status="active",
        data_mode="direct",
        public_config={"base_url": "https://api.example.com"},
        config_version=1,
    )
    return replace(record, **changes)


def _sample_spec() -> ConnectorSpec:
    return ConnectorSpec(
        connector_key="sample",
        tools=(),
        supports_data_modes=("direct", "stored"),
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
    )


def _client(
    monkeypatch,
    *,
    authenticated: bool = True,
    raise_server_exceptions: bool = True,
) -> TestClient:
    monkeypatch.setattr(
        tenant_auth_store,
        "resolve_session",
        lambda raw: (
            TenantPrincipal(principal_type="tenant", tenant_id="tenant-a")
            if authenticated and raw == "tenant-session-a"
            else None
        ),
    )
    monkeypatch.setattr(
        admin_connections,
        "_spec",
        lambda request, connector_key: _sample_spec(),
    )
    client = TestClient(
        create_app(), raise_server_exceptions=raise_server_exceptions
    )
    client.cookies.set("wbg_tenant_session", "tenant-session-a", path="/tenant")
    client.headers["Origin"] = "http://testserver"
    return client


def _create_payload():
    return {
        "connector_key": "sample",
        "display_name": "Sample",
        "data_mode": "direct",
        "public_config": {"base_url": "https://api.example.com"},
        "credentials": {"api_key": "credential-secret"},
    }


def test_tenant_domain_verification_cannot_cross_connection_owner(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: None,
    )

    get_response = client.get("/tenant/connections/conn-other/domain-verify")
    upload_response = client.post(
        "/tenant/connections/conn-other/domain-verify",
        files={"file": ("WW_verify.txt", b"value", "text/plain")},
    )
    delete_response = client.delete(
        "/tenant/connections/conn-other/domain-verify"
    )

    assert get_response.status_code == 404
    assert upload_response.status_code == 404
    assert delete_response.status_code == 404


_BODY_SURFACE = (
    ("post", "/tenant/connections", _create_payload()),
    (
        "put",
        "/tenant/connections/conn-a",
        {
            "display_name": "Updated",
            "data_mode": "direct",
            "public_config": {"base_url": "https://api.example.com"},
            "status": "active",
        },
    ),
    (
        "put",
        "/tenant/connections/conn-a/credentials",
        {"credentials": {"api_key": "credential-secret"}},
    ),
    (
        "post",
        "/tenant/connections/conn-a/credentials/rotate",
        {"credentials": {"api_key": "credential-secret"}},
    ),
    ("post", "/tenant/connections/conn-a/tokens", {"label": "cli"}),
    ("post", "/tenant/connections/conn-a/tokens/rotate", {"label": "cli"}),
    (
        "put",
        "/tenant/connections/conn-a/tools",
        {"policies": [{"tool_key": "items.list", "enabled": True}]},
    ),
    (
        "post",
        "/tenant/connections/conn-a/specs/import",
        {"document": {}, "spec_id": "spec-a", "revision": 1},
    ),
)

_BODYLESS_SURFACE = (
    ("get", "/tenant/connections/conn-a"),
    ("post", "/tenant/connections/conn-a/disable"),
    ("delete", "/tenant/connections/conn-a"),
    ("delete", "/tenant/connections/conn-a/tokens/token-a"),
    ("get", "/tenant/connections/conn-a/tools"),
    ("post", "/tenant/connections/conn-a/test"),
    ("post", "/tenant/connections/conn-a/sync"),
    (
        "post",
        "/tenant/connections/conn-a/specs/spec-a/revisions/1/validate",
    ),
    (
        "delete",
        "/tenant/connections/conn-a/specs/spec-a/revisions/1",
    ),
    (
        "post",
        "/tenant/connections/conn-a/specs/spec-a/revisions/1/publish",
    ),
    (
        "post",
        "/tenant/connections/conn-a/specs/spec-a/revisions/1/activate",
    ),
)

_MUTATION_SURFACE = _BODY_SURFACE + tuple(
    (method, path, None)
    for method, path in _BODYLESS_SURFACE
    if method != "get"
)


@pytest.mark.parametrize(("method", "path", "payload"), _BODY_SURFACE)
def test_tenant_body_surface_authenticates_and_rejects_tenant_id_fields(
    monkeypatch, method, path, payload
):
    client = _client(monkeypatch)
    for field in ("tenant_id", "tenantId", "tenant", "Tenant_ID"):
        response = getattr(client, method)(path, json={**payload, field: "tenant-b"})
        assert response.status_code == 422


@pytest.mark.parametrize(("method", "path", "payload"), _BODY_SURFACE)
def test_invalid_tenant_body_cannot_bypass_authentication(
    monkeypatch, method, path, payload
):
    response = getattr(_client(monkeypatch, authenticated=False), method)(
        path, json={**payload, "tenant_id": "tenant-b"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    _BODYLESS_SURFACE + tuple((method, path) for method, path, _ in _BODY_SURFACE),
)
@pytest.mark.parametrize(
    "query",
    (
        "tenant_id=tenant-b",
        "tenant.id=tenant-b",
        "Tenant-ID=tenant-b",
        "unknown=value",
        "x=1&x=2",
    ),
)
def test_tenant_connection_routes_reject_unknown_or_repeated_query(
    monkeypatch, method, path, query
):
    client = _client(monkeypatch)
    response = client.request(method.upper(), f"{path}?{query}", json=None)
    assert response.status_code == 422


@pytest.mark.parametrize(("method", "path"), _BODYLESS_SURFACE)
@pytest.mark.parametrize(
    ("body", "content_type"),
    (
        (b'{"tenant_id":"tenant-b"}', "application/json"),
        (b"tenant_id=tenant-b", "application/x-www-form-urlencoded"),
        (b"tenant-b", "text/plain"),
    ),
)
def test_bodyless_tenant_connection_routes_reject_nonempty_body(
    monkeypatch, method, path, body, content_type
):
    response = _client(monkeypatch).request(
        method.upper(), path, content=body, headers={"Content-Type": content_type}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(("method", "path"), _BODYLESS_SURFACE)
def test_invalid_bodyless_input_cannot_bypass_authentication(monkeypatch, method, path):
    response = _client(monkeypatch, authenticated=False).request(
        method.upper(), path, content=b"tenant_id=tenant-b"
    )
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "payload"), _MUTATION_SURFACE)
def test_hostile_origin_rejects_every_mutation_before_domain_or_side_effect(
    monkeypatch, method, path, payload
):
    calls = []
    use_cases = (
        "create_connection_use_case",
        "update_connection_use_case",
        "disable_connection_use_case",
        "delete_connection_use_case",
        "replace_connection_credentials_use_case",
        "issue_connection_token_use_case",
        "rotate_connection_token_use_case",
        "revoke_connection_token_use_case",
        "update_connection_tools_use_case",
        "test_connection_use_case",
        "sync_connection_use_case",
        "import_connection_spec_use_case",
        "validate_connection_spec_use_case",
        "delete_connection_spec_use_case",
        "publish_connection_spec_use_case",
        "activate_connection_spec_use_case",
    )
    for name in use_cases:
        monkeypatch.setattr(
            admin_connections,
            name,
            lambda *args, _name=name, **kwargs: calls.append(
                ("domain", _name, args, kwargs)
            )
            or {"ok": True},
        )
    for name in (
        "create_connection_with_token",
        "update_connection",
        "disable_connection",
        "delete_connection",
        "replace_credentials",
        "issue_token",
        "rotate_token",
        "revoke_token",
        "replace_tool_policies",
        "save_declarative_revision",
        "delete_declarative_revision",
        "publish_declarative_revision",
        "activate_declarative_revision",
        "_notify_connection_cache_invalidator",
    ):
        monkeypatch.setattr(
            admin_connections.store,
            name,
            lambda *args, _name=name, **kwargs: calls.append(
                ("store", _name, args, kwargs)
            ),
        )
    monkeypatch.setattr(
        admin_connections,
        "write_event",
        lambda event: calls.append(("audit", event)),
    )
    client = _client(monkeypatch)
    client.app.state.connection_sync_orchestrator = SimpleNamespace(
        run_connection=lambda *args: calls.append(("orchestrator", args))
    )
    client.app.state.mcp_gateway = SimpleNamespace(
        resolver=SimpleNamespace(
            execution_context=lambda *args: calls.append(("resolver", args))
        ),
        _runtime=SimpleNamespace(
            execute=lambda *args: calls.append(("network", args))
        ),
    )

    response = client.request(
        method.upper(),
        path,
        json=payload,
        headers={"Origin": "https://attacker.invalid"},
    )

    assert response.status_code == 403
    assert calls == []
    if path in {
        "/tenant/connections",
        "/tenant/connections/conn-a/tokens",
        "/tenant/connections/conn-a/tokens/rotate",
    }:
        assert response.headers["cache-control"] == "no-store"


def test_existing_tenant_connection_list_uses_session_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(
        admin_connections.store,
        "list_connections",
        lambda tenant_id: calls.append(tenant_id) or [],
    )
    response = _client(monkeypatch).get("/tenant/connections")
    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert calls == ["tenant-a"]


def test_tenant_validation_returns_shared_safe_operation_catalog(monkeypatch):
    record = _record(
        connector_key="http_declarative",
        status="draft",
        public_config={"spec_id": "spec-a", "revision": 2},
    )
    operation = DeclarativeOperation(
        tool_key="items.get",
        mcp_name="items_get",
        description="Get item",
        method="GET",
        path="/private/items/{item_id}",
        input_mappings=(
            InputMapping(
                arg_name="item_id",
                location="path",
                target="item_id",
                required=True,
                schema={
                    "type": "string",
                    "description": "Authorization: tenant-schema-secret",
                },
            ),
        ),
        output_mappings=(OutputMapping(name="public_title", pointer="/internal_title"),),
        operation_kind="read",
        base_url="https://private.example.invalid",
        timeout_ms=9999,
    )
    compiled = SimpleNamespace(
        status="draft",
        operations=(operation,),
        tools=(),
        raw_source={"credential": "tenant-secret"},
    )
    calls = []
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: record
        if tenant_id == "tenant-a"
        else None,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "get_declarative_revision",
        lambda spec_id, revision, tenant_id, connection_id: calls.append(
            (spec_id, revision, tenant_id, connection_id)
        )
        or compiled,
    )
    monkeypatch.setattr(
        admin_connections, "validate_revision", lambda revision, data_mode=None: revision
    )

    response = _client(monkeypatch).post(
        "/tenant/connections/conn-a/specs/spec-a/revisions/2/validate"
    )

    assert response.status_code == 200
    assert calls == [("spec-a", 2, "tenant-a", "conn-a")]
    assert response.json()["preview"] == {
        "tools": [],
        "operations": [{
            "operation_key": "items.get",
            "mcp_name": "items_get",
            "description": "Get item",
            "operation_kind": "read",
            "input_schema": {
                "type": "object",
                "properties": {"item_id": {"type": "string"}},
                "required": ["item_id"],
                "additionalProperties": False,
            },
            "output_names": ["public_title"],
        }],
    }
    serialized = repr(response.json())
    for forbidden in (
        "tenant-schema-secret",
        "/internal_title",
        "/private/items",
        "private.example.invalid",
        "tenant-secret",
        "private_cursor",
        "method",
        "path",
        "base_url",
        "auth_scheme",
        "timeout_ms",
        "pagination",
        "raw_source",
    ):
        assert forbidden not in serialized


def test_tenant_connection_surface_is_registered_without_duplicate_collection_get():
    app = create_app()
    included_routes = []
    for route in app.routes:
        included_routes.extend(
            getattr(getattr(route, "original_router", None), "routes", ())
        )
    routes = [
        (route.path, method)
        for route in [*app.routes, *included_routes]
        for method in getattr(route, "methods", ())
        if getattr(route, "path", "").startswith("/tenant/connections")
    ]

    def template(path: str) -> str:
        return (
            path.replace("/conn-a", "/{connection_id}")
            .replace("/token-a", "/{token_id}")
            .replace("/spec-a", "/{spec_id}")
            .replace("/revisions/1", "/revisions/{revision}")
        )

    for method, path, _payload in _BODY_SURFACE:
        assert (template(path), method.upper()) in routes
    for method, path in _BODYLESS_SURFACE:
        assert (template(path), method.upper()) in routes
    assert routes.count(("/tenant/connections", "GET")) == 1


def test_foreign_connection_is_404_before_any_secondary_read_or_side_effect(monkeypatch):
    calls = []
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: calls.append(
            ("owned", connection_id, tenant_id)
        )
        or None,
    )
    for name in (
        "list_connection_tokens",
        "disable_connection",
        "delete_connection",
        "replace_credentials",
        "issue_token",
        "rotate_token",
        "revoke_token",
        "list_tool_policies",
        "replace_tool_policies",
        "get_declarative_revision",
        "delete_declarative_revision",
        "publish_declarative_revision",
        "activate_declarative_revision",
    ):
        monkeypatch.setattr(
            admin_connections.store,
            name,
            lambda *args, _name=name, **kwargs: calls.append((_name, args, kwargs)),
        )
    monkeypatch.setattr(
        admin_connections,
        "write_event",
        lambda event: calls.append(("audit", event)),
    )
    client = _client(monkeypatch)

    for method, path in _BODYLESS_SURFACE:
        calls.clear()
        response = client.request(method.upper(), path)
        assert response.status_code == 404
        assert calls == [("owned", "conn-a", "tenant-a")]

    for method, path, payload in _BODY_SURFACE[1:]:
        calls.clear()
        response = getattr(client, method)(path, json=payload)
        assert response.status_code == 404
        assert calls == [("owned", "conn-a", "tenant-a")]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    (
        ("post", "/tenant/connections", {}),
        ("post", "/tenant/connections/conn-a/tokens", {}),
        ("post", "/tenant/connections/conn-a/tokens/rotate", {}),
    ),
)
def test_one_time_token_routes_set_no_store_on_auth_validation_and_safe_failures(
    monkeypatch, method, path, payload
):
    unauthenticated = getattr(_client(monkeypatch, authenticated=False), method)(
        path, json=payload
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["cache-control"] == "no-store"

    authenticated = _client(monkeypatch)
    invalid = getattr(authenticated, method)(
        path, json={**payload, "tenant_id": "tenant-b"}
    )
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("path", "payload", "use_case"),
    (
        (
            "/tenant/connections",
            _create_payload(),
            "create_connection_use_case",
        ),
        (
            "/tenant/connections/conn-a/tokens",
            {"label": "cli"},
            "issue_connection_token_use_case",
        ),
        (
            "/tenant/connections/conn-a/tokens/rotate",
            {"label": "cli"},
            "rotate_connection_token_use_case",
        ),
    ),
)
def test_raw_token_routes_convert_unexpected_errors_to_safe_no_store_500(
    monkeypatch, caplog, path, payload, use_case
):
    def fail(*args, **kwargs):
        raise RuntimeError("private-boundary-secret")

    monkeypatch.setattr(admin_connections, use_case, fail)
    client = _client(monkeypatch, raise_server_exceptions=False)

    response = client.post(path, json=payload)

    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "tenant connection operation failed"}
    assert "private-boundary-secret" not in response.text
    assert "RuntimeError" in caplog.text
    assert "private-boundary-secret" not in caplog.text


def test_non_raw_unexpected_error_keeps_default_exception_behavior(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private-boundary-secret")

    monkeypatch.setattr(admin_connections, "get_connection_use_case", fail)
    response = _client(monkeypatch, raise_server_exceptions=False).get(
        "/tenant/connections/conn-a"
    )

    assert response.status_code == 500
    assert "cache-control" not in response.headers
    assert response.text == "Internal Server Error"
    assert "private-boundary-secret" not in response.text


def test_tenant_create_and_token_issue_rotate_are_no_store_one_time_responses(
    monkeypatch,
):
    record = _record()
    monkeypatch.setattr(
        admin_connections.store,
        "create_connection_with_token",
        lambda item, credentials: (
            item,
            IssuedToken("initial", "mcp_initial_once", "initial-prefix"),
        ),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: record,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "issue_token",
        lambda connection_id, label="": IssuedToken(
            "issued", "mcp_issued_once", "issued-prefix"
        ),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "rotate_token",
        lambda connection_id, tenant_id, label="": IssuedToken(
            "rotated", "mcp_rotated_once", "rotated-prefix"
        ),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)
    client = _client(monkeypatch)

    responses = (
        client.post("/tenant/connections", json=_create_payload()),
        client.post(
            "/tenant/connections/conn-a/tokens", json={"label": "issued"}
        ),
        client.post(
            "/tenant/connections/conn-a/tokens/rotate", json={"label": "rotated"}
        ),
    )

    assert [response.status_code for response in responses] == [201, 201, 201]
    assert all(response.headers["cache-control"] == "no-store" for response in responses)
    assert responses[0].json()["initial_token"] == "mcp_initial_once"
    assert responses[1].json()["token"] == "mcp_issued_once"
    assert responses[2].json()["token"] == "mcp_rotated_once"


def test_tenant_detail_lists_only_safe_token_summaries(monkeypatch):
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: _record(),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "list_connection_tokens",
        lambda connection_id: [
            {"token_id": "token-a", "prefix": "prefix", "label": "cli"}
        ],
    )
    response = _client(monkeypatch).get("/tenant/connections/conn-a")
    assert response.status_code == 200
    assert response.json()["tokens"] == [
        {"token_id": "token-a", "prefix": "prefix", "label": "cli"}
    ]
    assert "raw" not in repr(response.json()).lower()


def test_tenant_tool_and_runtime_routes_use_session_tenant(monkeypatch):
    record = _record(data_mode="stored")
    tool = ToolSpec(
        tool_key="items.list",
        mcp_name="items_list",
        description="List items",
        input_schema={"type": "object"},
        output_schema=None,
        operation_kind="read",
        default_timeout_ms=1000,
        cache_ttl_seconds=None,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: record
        if tenant_id == "tenant-a"
        else None,
    )
    monkeypatch.setattr(admin_connections.store, "list_tool_policies", lambda _: [])
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)
    client = _client(monkeypatch)
    monkeypatch.setattr(
        admin_connections,
        "_spec",
        lambda request, connector_key: ConnectorSpec(
            connector_key="sample",
            tools=(tool,),
            supports_data_modes=("direct", "stored"),
        ),
    )
    executed = []

    class Resolver:
        def execution_context(self, context):
            assert context.tenant_id == "tenant-a"
            return SimpleNamespace(connection=record)

    class Runtime:
        def list_enabled_tools(self, context):
            return (tool,)

        async def execute(self, context, tool_key, args):
            executed.append((tool_key, args))
            return SimpleNamespace(status="ok")

    class Orchestrator:
        async def run_connection(self, connection):
            assert connection.tenant_id == "tenant-a"
            return SimpleNamespace(status="ok")

    client.app.state.mcp_gateway = SimpleNamespace(
        resolver=Resolver(), _runtime=Runtime()
    )
    client.app.state.connection_sync_orchestrator = Orchestrator()

    assert client.get("/tenant/connections/conn-a/tools").status_code == 200
    assert client.post("/tenant/connections/conn-a/test").status_code == 200
    assert client.post("/tenant/connections/conn-a/sync").status_code == 200
    assert executed == [("items.list", {})]
