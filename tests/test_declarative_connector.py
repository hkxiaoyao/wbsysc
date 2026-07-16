from __future__ import annotations

import json

import httpx
import pytest

from app import db
from app.connections import store
from app.connections.models import ConnectionRecord
from app.connectors.contracts import ConnectionContext
from app.connectors.declarative.connector import DeclarativeConnector
from app.connectors.declarative.http_client import SafeHttpClient
from app.connectors.declarative.models import (
    AuthScheme,
    DeclarativeOperation,
    DeclarativeRevision,
    InputMapping,
    OutputMapping,
    SpecValidationError,
    UnknownToolError,
)
from app.connectors.declarative.validator import import_openapi_revision, validate_revision


def _document() -> dict[str, object]:
    return {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/users/{user_id}": {
                "get": {
                    "operationId": "users.get",
                    "parameters": [
                        {
                            "name": "user_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "maxLength": 64},
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {"type": "integer", "maximum": 20},
                        },
                    ],
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
                                            "secret": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }


def _context(*, data_mode: str = "direct", credentials: dict[str, str] | None = None) -> ConnectionContext:
    return ConnectionContext(
        connection=ConnectionRecord(
            connection_id="conn-declarative",
            tenant_id="tenant-a",
            connector_key="http_declarative",
            display_name="Declared API",
            status="active",
            data_mode=data_mode,  # type: ignore[arg-type]
            public_config={},
            config_version=1,
        ),
        credentials=credentials or {},
    )


def _resolver(_: str, __: int) -> list[str]:
    return ["93.184.216.34"]


def test_import_compiles_only_declared_operations_and_output_fields() -> None:
    revision = import_openapi_revision(
        _document(),
        spec_id="spec-users",
        revision=2,
        tenant_id="tenant-a",
        connection_id="conn-declarative",
    )

    operation = revision.operation_for("users.get")
    assert operation.build_request({"user_id": "u / ?", "limit": 5}) == {
        "method": "GET",
        "url": "https://api.example.com/v1/users/u%20%2F%20%3F?limit=5",
        "json_body": None,
    }
    assert operation.extract_safe_output({"id": "u1", "name": "Ada", "secret": "omit"}) == {
        "id": "u1",
        "name": "Ada",
        "secret": "omit",
    }
    with pytest.raises(SpecValidationError, match="undeclared input"):
        operation.build_request({"user_id": "u1", "url": "https://metadata.example"})


def test_import_rejects_script_like_mapping() -> None:
    with pytest.raises(SpecValidationError, match="expressions are not supported"):
        validate_revision({"x-template": "${__import__('os').system('id')}"})


def test_import_rejects_stored_mode_without_a_validated_sync_spec() -> None:
    revision = import_openapi_revision(_document())

    with pytest.raises(SpecValidationError, match="stored mode requires"):
        revision.assert_data_mode_allowed("stored")


def test_connector_rejects_an_http_client_with_a_broader_host_policy() -> None:
    revision = import_openapi_revision(_document())
    client = SafeHttpClient(
        allowed_hosts={"api.example.com", "other.example.com"},
        resolver=_resolver,
    )

    with pytest.raises(ValueError, match="host policy"):
        DeclarativeConnector(revision=revision, client=client)


def test_programmatic_revision_cannot_point_outside_its_allowed_hosts() -> None:
    operation = DeclarativeOperation(
        tool_key="users.get",
        mcp_name="users.get",
        description="Read users",
        method="GET",
        path="/users",
        input_mappings=(),
        output_mappings=(OutputMapping(name="id", pointer="/id"),),
        operation_kind="read",
        base_url="https://untrusted.example.net",
    )

    with pytest.raises(SpecValidationError, match="revision base URL"):
        DeclarativeRevision(
            base_url="https://untrusted.example.net",
            allowed_hosts=("api.example.com",),
            operations=(operation,),
        )


def test_programmatic_oauth_token_url_must_remain_on_the_allowed_hosts() -> None:
    operation = DeclarativeOperation(
        tool_key="users.get",
        mcp_name="users.get",
        description="Read users",
        method="GET",
        path="/users",
        input_mappings=(),
        output_mappings=(OutputMapping(name="id", pointer="/id"),),
        operation_kind="read",
        base_url="https://api.example.com",
    )
    auth = AuthScheme(
        kind="oauth2_client_credentials",
        token_url="https://untrusted.example.net/token",
        client_id_key="client_id",
        client_secret_key="client_secret",
    )

    with pytest.raises(SpecValidationError, match="OAuth token URL"):
        DeclarativeRevision(
            base_url="https://api.example.com",
            allowed_hosts=("api.example.com",),
            operations=(operation,),
            auth_scheme=auth,
        )


def test_programmatic_operation_cannot_map_a_protected_header() -> None:
    with pytest.raises(SpecValidationError, match="protected headers"):
        InputMapping(
            arg_name="bearer",
            location="header",
            target="Authorization",
            schema={"type": "string"},
        )


def test_import_allows_stored_only_for_a_validated_read_sync_mapping() -> None:
    document = _document()
    document["x-sync-spec"] = {
        "resource_key": "users",
        "operation_key": "users.get",
        "primary_key_pointer": "/id",
        "field_mappings": {"id": "/id", "name": "/name"},
    }

    revision = import_openapi_revision(document)

    revision.assert_data_mode_allowed("stored")

    document["x-sync-spec"] = {
        "resource_key": "users",
        "operation_key": "users.get",
        "primary_key_pointer": "/id",
        "field_mappings": {"not_declared": "/not-declared"},
    }
    with pytest.raises(SpecValidationError, match="sync field mapping is not declared"):
        import_openapi_revision(document)


def test_import_rejects_write_without_explicit_enablement() -> None:
    document = _document()
    document["paths"] = {
        "/users": {
            "post": {
                "operationId": "users.create",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"name": {"type": "string"}},
                                "required": ["name"],
                            }
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "created",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"id": {"type": "string"}},
                                }
                            }
                        },
                    }
                },
            }
        }
    }

    with pytest.raises(SpecValidationError, match="write operation requires explicit enablement"):
        import_openapi_revision(document)


@pytest.mark.parametrize(
    "document",
    [
        "openapi: &version 3.0.3\ncopy: *version\n",
        "!unsafe {openapi: 3.0.3}\n",
        {"x-template": "{{ dangerous }}"},
    ],
)
def test_import_rejects_yaml_controls_and_templates(document: object) -> None:
    with pytest.raises(SpecValidationError):
        validate_revision(document)  # type: ignore[arg-type]


def test_import_bounds_programmatic_documents_before_validation() -> None:
    document = {f"field-{index}": "x" * 15_000 for index in range(20)}

    with pytest.raises(SpecValidationError, match="specification document exceeds size limit"):
        validate_revision(document)


def test_import_redacts_recursive_programmatic_document_failures() -> None:
    recursive: dict[str, object] = {}
    current = recursive
    for _ in range(1_500):
        child: dict[str, object] = {}
        current["child"] = child
        current = child

    with pytest.raises(SpecValidationError) as exc_info:
        validate_revision(recursive)

    assert exc_info.value.__cause__ is None


def test_import_redacts_invalid_unicode_in_programmatic_documents() -> None:
    with pytest.raises(SpecValidationError) as exc_info:
        validate_revision({"invalid": "\ud800"})

    assert exc_info.value.__cause__ is None


def test_import_rejects_yaml_merge_controls() -> None:
    document = """
openapi: 3.0.3
<<: {x-template: harmless-looking}
"""

    with pytest.raises(SpecValidationError, match="YAML merge"):
        validate_revision(document)


def test_nested_json_body_rejects_undeclared_fields() -> None:
    document = _document()
    document["paths"] = {
        "/profiles": {
            "post": {
                "operationId": "profiles.update",
                "x-write-enabled": True,
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "profile": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {"display_name": {"type": "string"}},
                                        "required": ["display_name"],
                                    }
                                },
                                "required": ["profile"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"id": {"type": "string"}},
                                }
                            }
                        },
                    }
                },
            }
        }
    }
    operation = import_openapi_revision(document).operation_for("profiles.update")

    with pytest.raises(SpecValidationError, match="undeclared object input"):
        operation.build_request(
            {"profile": {"display_name": "Ada", "role": "administrator"}}
        )


def test_import_applies_global_auth_without_an_operation_id() -> None:
    document = _document()
    operation = document["paths"]["/users/{user_id}"]["get"]
    operation.pop("operationId")
    operation["x-tool-key"] = "users.get"
    document["components"] = {
        "securitySchemes": {
            "serviceKey": {
                "type": "apiKey",
                "name": "X-Service-Key",
                "in": "header",
                "x-credential-key": "service_api_key",
            }
        }
    }
    document["security"] = [{"serviceKey": []}]

    revision = import_openapi_revision(document)

    assert revision.auth_scheme is not None
    assert revision.auth_scheme.kind == "api_key"


def test_import_rejects_forwarded_header_mapping() -> None:
    document = _document()
    operation = document["paths"]["/users/{user_id}"]["get"]
    operation["parameters"].append(
        {
            "name": "X-Forwarded-For",
            "in": "header",
            "schema": {"type": "string"},
        }
    )

    with pytest.raises(SpecValidationError, match="protected headers"):
        import_openapi_revision(document)


def test_import_rejects_percent_encoded_static_path_escapes() -> None:
    document = _document()
    document["paths"] = {"/%2e%2e/admin": document["paths"]["/users/{user_id}"]}

    with pytest.raises(SpecValidationError, match="invalid OpenAPI path"):
        import_openapi_revision(document)


def test_import_redacts_invalid_server_port_details() -> None:
    document = _document()
    document["servers"] = [{"url": "https://api.example.com:not-a-port/v1"}]

    with pytest.raises(SpecValidationError) as exc_info:
        import_openapi_revision(document)

    assert exc_info.value.__cause__ is None


def test_import_rejects_an_oauth_token_host_outside_the_allowlist() -> None:
    document = _document()
    document["components"] = {
        "securitySchemes": {
            "serviceOauth": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "https://auth.example.com/oauth/token",
                        "scopes": {},
                    }
                },
            }
        }
    }
    document["security"] = [{"serviceOauth": []}]

    with pytest.raises(SpecValidationError, match="OAuth token host is absent"):
        import_openapi_revision(document)


@pytest.mark.asyncio
async def test_declarative_connector_rejects_undeclared_operation() -> None:
    revision = import_openapi_revision(_document())
    connector = DeclarativeConnector(
        revision=revision,
        client=SafeHttpClient(allowed_hosts={"api.example.com"}, resolver=_resolver),
    )

    with pytest.raises(UnknownToolError):
        await connector.execute(_context(), "users.delete", {})


@pytest.mark.asyncio
async def test_declarative_connector_only_sends_declared_mapping_and_returns_selected_output() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"id": "u1", "name": "Ada", "secret": "upstream-secret", "extra": "omit"},
            request=request,
        )

    revision = import_openapi_revision(_document())
    client = SafeHttpClient(
        allowed_hosts={"api.example.com"},
        resolver=_resolver,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    connector = DeclarativeConnector(revision=revision, client=client)

    result = await connector.execute(_context(), "users.get", {"user_id": "u1", "limit": 2})

    assert result.status == "ok"
    assert result.data == {"id": "u1", "name": "Ada", "secret": "upstream-secret"}
    assert seen[0].url.path == "/v1/users/u1"
    assert seen[0].url.params == httpx.QueryParams({"limit": "2"})


@pytest.mark.asyncio
async def test_api_key_credentials_are_declared_and_never_in_result_or_errors() -> None:
    document = _document()
    document["components"] = {
        "securitySchemes": {
            "serviceKey": {
                "type": "apiKey",
                "name": "X-Service-Key",
                "in": "header",
                "x-credential-key": "service_api_key",
            }
        }
    }
    document["security"] = [{"serviceKey": []}]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, json={"detail": "api-key-should-not-leak"}, request=request)

    revision = import_openapi_revision(document)
    client = SafeHttpClient(
        allowed_hosts={"api.example.com"},
        resolver=_resolver,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    connector = DeclarativeConnector(revision=revision, client=client)

    result = await connector.execute(
        _context(credentials={"service_api_key": "api-key-should-not-leak"}),
        "users.get",
        {"user_id": "u1"},
    )

    assert seen[0].headers["x-service-key"] == "api-key-should-not-leak"
    assert result.status == "error"
    assert "api-key-should-not-leak" not in repr(result)
    assert "api-key-should-not-leak" not in str(result.data)


@pytest.mark.asyncio
async def test_oauth_client_credentials_are_typed_and_redacted() -> None:
    document = _document()
    document["x-allowed-hosts"] = ["api.example.com", "auth.example.com"]
    document["components"] = {
        "securitySchemes": {
            "serviceOauth": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "https://auth.example.com/oauth/token",
                        "scopes": {"users.read": "Read users"},
                    }
                },
                "x-client-id-credential-key": "oauth_client_id",
                "x-client-secret-credential-key": "oauth_client_secret",
            }
        }
    }
    document["security"] = [{"serviceOauth": []}]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "auth.example.com":
            assert request.headers["content-type"].startswith(
                "application/x-www-form-urlencoded"
            )
            assert b"client_secret=oauth-client-secret" in request.content
            return httpx.Response(
                200,
                json={"access_token": "oauth-access-token"},
                request=request,
            )
        assert request.headers["authorization"] == "Bearer oauth-access-token"
        return httpx.Response(200, json={"id": "u1", "name": "Ada", "secret": "omit"}, request=request)

    revision = import_openapi_revision(document)
    client = SafeHttpClient(
        allowed_hosts={"api.example.com", "auth.example.com"},
        resolver=_resolver,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    connector = DeclarativeConnector(revision=revision, client=client)

    result = await connector.execute(
        _context(
            credentials={
                "oauth_client_id": "oauth-client-id",
                "oauth_client_secret": "oauth-client-secret",
            }
        ),
        "users.get",
        {"user_id": "u1"},
    )

    assert result.status == "ok"
    assert result.data == {"id": "u1", "name": "Ada", "secret": "omit"}
    assert [request.url.host for request in seen] == ["auth.example.com", "api.example.com"]
    assert "oauth-client-secret" not in repr(result)
    assert "oauth-access-token" not in repr(result)


class _RevisionStoreConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, object]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def execute(self, statement, params=None):
        self.statements.append((str(statement), dict(params or {})))


class _RevisionStoreEngine:
    def __init__(self, connection: _RevisionStoreConnection) -> None:
        self.connection = connection

    def begin(self) -> _RevisionStoreConnection:
        return self.connection


def test_declarative_revision_persistence_uses_bound_parameters(monkeypatch) -> None:
    document = _document()
    document["paths"]["/users/{user_id}"]["get"]["summary"] = "x'); DROP TABLE connection_instance; --"
    revision = import_openapi_revision(
        document,
        spec_id="spec-users",
        revision=2,
        tenant_id="tenant-a",
        connection_id="conn-declarative",
    )
    connection = _RevisionStoreConnection()
    monkeypatch.setattr(db, "get_engine", lambda: _RevisionStoreEngine(connection))

    store.save_declarative_revision(revision)

    revision_sql, revision_params = next(
        (sql, params)
        for sql, params in connection.statements
        if "INSERT INTO declarative_spec_revision" in sql
    )
    assert "DROP TABLE" not in revision_sql
    assert revision_params["spec_id"] == "spec-users"
    assert "DROP TABLE" in str(revision_params["spec_json"])


class _RevisionReadResult:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class _RevisionReadConnection(_RevisionStoreConnection):
    def __init__(self, row: dict[str, object]) -> None:
        super().__init__()
        self.row = row

    def execute(self, statement, params=None):
        self.statements.append((str(statement), dict(params or {})))
        return _RevisionReadResult(self.row)


class _RevisionReadEngine(_RevisionStoreEngine):
    def connect(self) -> _RevisionReadConnection:
        return self.connection  # type: ignore[return-value]


def test_published_revision_rows_rehydrate_a_validated_connector_revision(monkeypatch) -> None:
    revision = import_openapi_revision(
        _document(),
        spec_id="spec-users",
        revision=2,
        tenant_id="tenant-a",
        connection_id="conn-declarative",
        status="published",
    )
    connection = _RevisionReadConnection(
        {
            "spec_id": "spec-users",
            "revision": 2,
            "tenant_id": "tenant-a",
            "connection_id": "conn-declarative",
            "status": "published",
            "spec_json": json.dumps(revision.storage_document()),
        }
    )
    monkeypatch.setattr(db, "get_engine", lambda: _RevisionReadEngine(connection))

    loaded = store.get_published_declarative_revision("spec-users", 2, "tenant-a")

    assert loaded is not None
    assert loaded.operation_for("users.get").mcp_name == "users.get"
    sql, params = connection.statements[0]
    assert "tenant-a" not in sql
    assert params == {"spec_id": "spec-users", "revision": 2, "tenant_id": "tenant-a"}
