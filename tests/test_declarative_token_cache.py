from __future__ import annotations

import asyncio

import httpx
import pytest

from app.connections.models import ConnectionRecord
from app.connectors.contracts import ConnectionContext
from app.connectors.declarative.connector import DeclarativeConnector
from app.connectors.declarative.http_client import SafeHttpClient
from app.connectors.declarative.token_cache import OAuthTokenCache
from app.connectors.declarative.validator import import_openapi_revision


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _operation(operation_id: str, output_fields: tuple[str, ...]) -> dict[str, object]:
    return {
        "operationId": operation_id,
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


def _oauth_components() -> dict[str, object]:
    return {
        "securitySchemes": {
            "serviceOauth": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "https://auth.example.com/oauth/token",
                        "scopes": {"people.read": "Read people"},
                    }
                },
                "x-client-id-credential-key": "oauth_client_id",
                "x-client-secret-credential-key": "oauth_client_secret",
            }
        }
    }


def _single_step_document() -> dict[str, object]:
    return {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "x-allowed-hosts": ["api.example.com", "auth.example.com"],
        "components": _oauth_components(),
        "security": [{"serviceOauth": []}],
        "paths": {"/people/lookup": {"get": _operation("people.lookup", ("entity_id",))}},
    }


def _three_step_document() -> dict[str, object]:
    return {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "x-allowed-hosts": ["api.example.com", "auth.example.com"],
        "components": _oauth_components(),
        "security": [{"serviceOauth": []}],
        "paths": {
            "/people/first": {"get": _operation("people.first", ("first_id",))},
            "/people/second": {"get": _operation("people.second", ("second_id",))},
            "/people/third": {"get": _operation("people.third", ("display_name",))},
        },
        "x-mcp-tools": [
            {
                "tool_key": "people.get",
                "mcp_name": "people.get",
                "description": "Resolve one person through three hops",
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
                        "step_id": "first",
                        "operation_key": "people.first",
                        "input_map": {"id": "$input.id"},
                        "output_mappings": {"resolved": "first_id"},
                    },
                    {
                        "step_id": "second",
                        "operation_key": "people.second",
                        "input_map": {"id": "$steps.first.resolved"},
                        "output_mappings": {"resolved": "second_id"},
                    },
                    {
                        "step_id": "third",
                        "operation_key": "people.third",
                        "input_map": {"id": "$steps.second.resolved"},
                        "output_mappings": {"public_name": "display_name"},
                    },
                ],
                "result_map": {"name": "$steps.third.public_name"},
            }
        ],
    }


def _context(
    *,
    connection_id: str = "conn-declarative",
    client_secret: str = "oauth-client-secret",
) -> ConnectionContext:
    return ConnectionContext(
        connection=ConnectionRecord(
            connection_id=connection_id,
            tenant_id="tenant-a",
            connector_key="http_declarative",
            display_name="Declared API",
            status="active",
            data_mode="direct",
            public_config={},
            config_version=1,
        ),
        credentials={
            "oauth_client_id": "oauth-client-id",
            "oauth_client_secret": client_secret,
        },
    )


def _resolver(_: str, __: int) -> list[str]:
    return ["93.184.216.34"]


class _Upstream:
    """Count token exchanges and serve a fixed business payload."""

    def __init__(
        self,
        *,
        expires_in: object | None = None,
        unauthorized_tokens: frozenset[str] = frozenset(),
    ) -> None:
        self.token_requests = 0
        self.business_requests = 0
        self.expires_in = expires_in
        self.unauthorized_tokens = unauthorized_tokens
        self.seen_authorizations: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example.com":
            self.token_requests += 1
            payload: dict[str, object] = {"access_token": f"token-{self.token_requests}"}
            if self.expires_in is not None:
                payload["expires_in"] = self.expires_in
            return httpx.Response(200, json=payload, request=request)
        self.business_requests += 1
        authorization = request.headers.get("authorization", "")
        self.seen_authorizations.append(authorization)
        token = authorization.removeprefix("Bearer ")
        if token in self.unauthorized_tokens:
            return httpx.Response(401, json={"error": "expired"}, request=request)
        return httpx.Response(
            200,
            json={
                "entity_id": "e1",
                "first_id": "f1",
                "second_id": "s1",
                "display_name": "Ada",
            },
            request=request,
        )


def _connector(
    document: dict[str, object],
    upstream: _Upstream,
    cache: OAuthTokenCache,
) -> DeclarativeConnector:
    revision = import_openapi_revision(document)
    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com", "auth.example.com"},
        resolver=_resolver,
        transport=httpx.MockTransport(upstream.handler),
    )
    return DeclarativeConnector._for_test(
        revision=revision,
        client=client,
        token_cache=cache,
    )


@pytest.mark.asyncio
async def test_repeated_calls_reuse_one_cached_token() -> None:
    upstream = _Upstream()
    cache = OAuthTokenCache(clock=_Clock())
    connector = _connector(_single_step_document(), upstream, cache)
    context = _context()

    for _ in range(3):
        result = await connector.execute(context, "people.lookup", {"id": "p1"})
        assert result.status == "ok"

    assert upstream.token_requests == 1
    assert upstream.business_requests == 3


@pytest.mark.asyncio
async def test_three_step_tool_exchanges_exactly_one_token() -> None:
    upstream = _Upstream()
    cache = OAuthTokenCache(clock=_Clock())
    connector = _connector(_three_step_document(), upstream, cache)

    result = await connector.execute(_context(), "people.get", {"id": "p1"})

    assert result.status == "ok"
    assert upstream.business_requests == 3
    assert upstream.token_requests == 1


@pytest.mark.asyncio
async def test_expired_token_is_exchanged_again_with_a_safety_margin() -> None:
    clock = _Clock()
    upstream = _Upstream(expires_in=300)
    cache = OAuthTokenCache(clock=clock)
    connector = _connector(_single_step_document(), upstream, cache)
    context = _context()

    await connector.execute(context, "people.lookup", {"id": "p1"})
    assert upstream.token_requests == 1

    # Still inside the usable window (300s lifetime minus the 60s margin).
    clock.advance(239)
    await connector.execute(context, "people.lookup", {"id": "p1"})
    assert upstream.token_requests == 1

    # The safety margin retires the token before the upstream expiry.
    clock.advance(2)
    await connector.execute(context, "people.lookup", {"id": "p1"})
    assert upstream.token_requests == 2


@pytest.mark.asyncio
async def test_token_without_expires_in_uses_a_bounded_default_lifetime() -> None:
    clock = _Clock()
    upstream = _Upstream()
    cache = OAuthTokenCache(clock=clock)
    connector = _connector(_single_step_document(), upstream, cache)
    context = _context()

    await connector.execute(context, "people.lookup", {"id": "p1"})
    clock.advance(299)
    await connector.execute(context, "people.lookup", {"id": "p1"})
    assert upstream.token_requests == 1

    clock.advance(2)
    await connector.execute(context, "people.lookup", {"id": "p1"})
    assert upstream.token_requests == 2


@pytest.mark.asyncio
async def test_unusably_short_lifetimes_are_never_cached() -> None:
    upstream = _Upstream(expires_in=30)
    cache = OAuthTokenCache(clock=_Clock())
    connector = _connector(_single_step_document(), upstream, cache)
    context = _context()

    await connector.execute(context, "people.lookup", {"id": "p1"})
    await connector.execute(context, "people.lookup", {"id": "p1"})

    assert upstream.token_requests == 2


@pytest.mark.asyncio
async def test_tokens_are_never_shared_across_connections() -> None:
    upstream = _Upstream()
    cache = OAuthTokenCache(clock=_Clock())
    connector = _connector(_single_step_document(), upstream, cache)

    await connector.execute(_context(connection_id="conn-a"), "people.lookup", {"id": "p1"})
    await connector.execute(_context(connection_id="conn-b"), "people.lookup", {"id": "p1"})
    await connector.execute(_context(connection_id="conn-a"), "people.lookup", {"id": "p1"})

    assert upstream.token_requests == 2
    assert upstream.seen_authorizations == [
        "Bearer token-1",
        "Bearer token-2",
        "Bearer token-1",
    ]


@pytest.mark.asyncio
async def test_rotated_credentials_are_never_served_a_stale_token() -> None:
    upstream = _Upstream()
    cache = OAuthTokenCache(clock=_Clock())
    connector = _connector(_single_step_document(), upstream, cache)

    await connector.execute(_context(client_secret="secret-one"), "people.lookup", {"id": "p1"})
    await connector.execute(_context(client_secret="secret-two"), "people.lookup", {"id": "p1"})

    assert upstream.token_requests == 2


@pytest.mark.asyncio
async def test_invalidate_connection_drops_only_that_connection() -> None:
    upstream = _Upstream()
    cache = OAuthTokenCache(clock=_Clock())
    connector = _connector(_single_step_document(), upstream, cache)

    await connector.execute(_context(connection_id="conn-a"), "people.lookup", {"id": "p1"})
    await connector.execute(_context(connection_id="conn-b"), "people.lookup", {"id": "p1"})
    assert upstream.token_requests == 2

    assert cache.invalidate_connection("conn-a") == 1

    await connector.execute(_context(connection_id="conn-b"), "people.lookup", {"id": "p1"})
    assert upstream.token_requests == 2

    await connector.execute(_context(connection_id="conn-a"), "people.lookup", {"id": "p1"})
    assert upstream.token_requests == 3


@pytest.mark.asyncio
async def test_upstream_401_refreshes_the_token_and_retries_once() -> None:
    upstream = _Upstream(unauthorized_tokens=frozenset({"token-1"}))
    cache = OAuthTokenCache(clock=_Clock())
    connector = _connector(_single_step_document(), upstream, cache)

    result = await connector.execute(_context(), "people.lookup", {"id": "p1"})

    assert result.status == "ok"
    assert upstream.token_requests == 2
    assert upstream.seen_authorizations == ["Bearer token-1", "Bearer token-2"]


@pytest.mark.asyncio
async def test_persistent_401_fails_without_a_second_retry() -> None:
    upstream = _Upstream(
        unauthorized_tokens=frozenset({"token-1", "token-2", "token-3"})
    )
    cache = OAuthTokenCache(clock=_Clock())
    connector = _connector(_single_step_document(), upstream, cache)

    result = await connector.execute(_context(), "people.lookup", {"id": "p1"})

    assert result.status == "error"
    assert upstream.business_requests == 2
    assert upstream.token_requests == 2


@pytest.mark.asyncio
async def test_concurrent_calls_collapse_into_one_token_exchange() -> None:
    upstream = _Upstream()
    cache = OAuthTokenCache(clock=_Clock())
    connector = _connector(_single_step_document(), upstream, cache)
    context = _context()

    results = await asyncio.gather(
        *(connector.execute(context, "people.lookup", {"id": "p1"}) for _ in range(5))
    )

    assert all(result.status == "ok" for result in results)
    assert upstream.business_requests == 5
    assert upstream.token_requests == 1


@pytest.mark.asyncio
async def test_cached_tokens_are_absent_from_the_cache_repr() -> None:
    upstream = _Upstream()
    cache = OAuthTokenCache(clock=_Clock())
    connector = _connector(_single_step_document(), upstream, cache)

    await connector.execute(_context(), "people.lookup", {"id": "p1"})

    assert "token-1" not in repr(cache)
    assert "oauth-client-secret" not in repr(cache)


@pytest.mark.asyncio
async def test_committed_connection_changes_retire_cached_tokens() -> None:
    from app.connections import store as connection_store
    from app.connectors.declarative import token_cache as token_cache_module
    from app.mcp_gateway import ConnectionMcpGateway

    cache = token_cache_module.get_default_token_cache()
    upstream = _Upstream()
    connector = _connector(_single_step_document(), upstream, cache)
    context = _context(connection_id="conn-lifecycle")

    gateway = ConnectionMcpGateway()
    async with gateway.run():
        await connector.execute(context, "people.lookup", {"id": "p1"})
        await connector.execute(context, "people.lookup", {"id": "p1"})
        assert upstream.token_requests == 1

        # A committed credential/policy/status change emits exactly this hook.
        connection_store.invalidate_connection_cache("conn-lifecycle", 2)

        await connector.execute(context, "people.lookup", {"id": "p1"})
        assert upstream.token_requests == 2

    # After the lifespan ends the hook must be unregistered again.
    assert cache.invalidate_connection("conn-lifecycle") == 1


def test_cache_keys_never_embed_credential_plaintext() -> None:
    cache = OAuthTokenCache(clock=_Clock())

    first = cache.cache_key(
        "conn-a",
        token_url="https://auth.example.com/oauth/token",
        client_id_key="oauth_client_id",
        client_secret_key="oauth_client_secret",
        scopes=("people.read",),
        client_id="oauth-client-id",
        client_secret="oauth-client-secret",
    )
    second = cache.cache_key(
        "conn-a",
        token_url="https://auth.example.com/oauth/token",
        client_id_key="oauth_client_id",
        client_secret_key="oauth_client_secret",
        scopes=("people.read",),
        client_id="oauth-client-id",
        client_secret="rotated-secret",
    )

    assert first != second
    rendered = repr(first) + repr(second)
    assert "oauth-client-secret" not in rendered
    assert "rotated-secret" not in rendered
    assert "oauth-client-id" not in rendered
