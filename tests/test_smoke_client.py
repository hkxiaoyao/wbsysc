"""Default-off, authorization-gated MCP rollout smoke client.

Only fixed status codes, safe target labels, and counts are printed. Credentials,
headers, response bodies, and exception messages are never emitted.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from builtins import BaseExceptionGroup, ExceptionGroup
import hashlib
import ipaddress
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError


_TRUE = {"1", "true", "yes", "on"}
_PRODUCTION_OPT_IN = "I_ACCEPT_PRODUCTION_SMOKE"
_AUTHORIZATION_ACK = "I_HAVE_WRITTEN_AUTHORIZATION"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_PLACEHOLDER_HOSTS = {
    "example.com",
    "www.example.com",
    "mcp.example.com",
    "mcp.example.test",
    "test.example.com",
}
_PLACEHOLDER_VALUES = {
    "example",
    "example-id",
    "test",
    "test-id",
    "test-token",
    "wrong-token",
    "replace-me",
    "replace_me",
    "changeme",
    "connection-a",
    "connection-b",
    "service-a",
    "service-b",
}
_UNAUTHORIZED_STATUSES = {401, 403}
_CONNECTION_TOKEN_RE = re.compile(r"mcp_[A-Za-z0-9_-]{43}\Z")
_SERVICE_TOKEN_RE = re.compile(r"mcp_svc_[A-Za-z0-9_-]{43}\Z")
_RESERVED_PRODUCTION_DOMAINS = ("example.com", "example.net", "example.org")


class SmokeConfigurationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__("smoke configuration rejected")


@dataclass(frozen=True)
class SmokeTarget:
    safe_id: str
    endpoint: str
    token: str
    expected_aliases: frozenset[str]
    call_alias: str
    call_arguments: Mapping[str, object]

    def __repr__(self) -> str:
        return f"SmokeTarget(safe_id={self.safe_id!r}, secrets=<redacted>)"


@dataclass(frozen=True)
class SmokeConfig:
    connection: SmokeTarget
    second_connection: SmokeTarget
    service: SmokeTarget
    wrong_service_endpoint: str
    bad_connection_token: str
    bad_service_token: str
    production: bool

    def __repr__(self) -> str:
        return f"SmokeConfig(production={self.production}, credentials=<redacted>)"


def _enabled(env: Mapping[str, str]) -> bool:
    return env.get("MCP_SMOKE_RUN", "").strip().lower() in _TRUE


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SmokeConfigurationError(f"missing_{name.lower()}")
    return value


def _credential(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise SmokeConfigurationError(f"missing_{name.lower()}")
    if value != value.strip():
        raise SmokeConfigurationError(f"whitespace_{name.lower()}")
    return value


def _normalized_url(value: str) -> str:
    value = value.strip().rstrip("/")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise SmokeConfigurationError("invalid_endpoint") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise SmokeConfigurationError("invalid_endpoint")
    host = (parts.hostname or "").lower()
    port_text = f":{port}" if port is not None else ""
    display_host = f"[{host}]" if ":" in host else host
    return urlunsplit(
        (parts.scheme.lower(), f"{display_host}{port_text}", parts.path, "", "")
    )


def build_endpoint(base_url: str, route: str, resource_id: str) -> str:
    base = _normalized_url(base_url)
    if urlsplit(base).path not in {"", "/"}:
        raise SmokeConfigurationError("invalid_base_endpoint")
    if route not in {"connection", "service"}:
        raise SmokeConfigurationError("invalid_endpoint_route")
    resource_id = resource_id.strip()
    if not resource_id or resource_id in {".", ".."}:
        raise SmokeConfigurationError("invalid_resource_id")
    prefix = "/mcp" if route == "connection" else "/mcp/service"
    return f"{base.rstrip('/')}{prefix}/{quote(resource_id, safe='')}"


def _explicit_endpoint(
    env: Mapping[str, str], name: str, base_url: str, route: str, resource_id: str
) -> str:
    supplied = _normalized_url(_required(env, name))
    expected = build_endpoint(base_url, route, resource_id)
    if supplied != expected:
        raise SmokeConfigurationError(f"mismatch_{name.lower()}")
    return supplied


def _aliases(value: str, name: str) -> frozenset[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    aliases = frozenset(items)
    if not aliases:
        raise SmokeConfigurationError(f"missing_{name.lower()}")
    if len(items) != len(aliases):
        raise SmokeConfigurationError(f"duplicate_{name.lower()}")
    return aliases


def _call_arguments(env: Mapping[str, str], name: str) -> Mapping[str, object]:
    try:
        value = json.loads(_required(env, name))
    except json.JSONDecodeError as exc:
        raise SmokeConfigurationError(f"invalid_{name.lower()}") from exc
    if not isinstance(value, dict):
        raise SmokeConfigurationError(f"invalid_{name.lower()}")
    return value


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        normalized in _PLACEHOLDER_VALUES
        or normalized.startswith(
            ("raw-", "example-", "test-", "wrong-", "replace-", "replace_", "changeme")
        )
        or normalized.endswith("_here")
        or "placeholder" in normalized
        or "replace_with_" in normalized
    )


def _validate_production_host(base_url: str) -> None:
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https":
        raise SmokeConfigurationError("production_https_required")
    if (
        host in _LOOPBACK_HOSTS
        or host in _PLACEHOLDER_HOSTS
        or host.endswith((".example", ".invalid", ".localhost", ".test"))
    ):
        raise SmokeConfigurationError("production_host_rejected")
    if any(
        host == reserved or host.endswith(f".{reserved}")
        for reserved in _RESERVED_PRODUCTION_DOMAINS
    ):
        raise SmokeConfigurationError("production_host_rejected")
    labels = host.split(".")
    if any(
        label in {"test", "testing", "staging", "dev", "development", "qa"}
        or label.startswith(("test-", "staging-", "dev-", "qa-"))
        for label in labels
    ):
        raise SmokeConfigurationError("production_host_rejected")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise SmokeConfigurationError("production_host_rejected")
    else:
        if not address.is_global:
            raise SmokeConfigurationError("production_host_rejected")


def _has_proper_period(
    value: str | bytes, *, ignored: frozenset[int] = frozenset()
) -> bool:
    for period in range(1, (len(value) // 2) + 1):
        if all(index in ignored or value[index] == value[index % period] for index in range(len(value))):
            return True
    return False


def _is_arithmetic_sequence(
    value: bytes, *, ignored: frozenset[int] = frozenset()
) -> bool:
    deltas = {
        (value[index + 1] - value[index]) % 256
        for index in range(len(value) - 1)
        if index not in ignored and index + 1 not in ignored
    }
    return len(deltas) == 1


def _target(
    env: Mapping[str, str], prefix: str, safe_id: str, base_url: str, route: str
) -> tuple[SmokeTarget, str]:
    id_name = f"MCP_SMOKE_{prefix}_ID"
    resource_id = _persisted_id(_required(env, id_name), id_name)
    endpoint = _explicit_endpoint(
        env, f"MCP_SMOKE_{prefix}_ENDPOINT", base_url, route, resource_id
    )
    target = SmokeTarget(
        safe_id,
        endpoint,
        _token(
            env,
            f"MCP_SMOKE_{prefix}_TOKEN",
            service=route == "service",
        ),
        _aliases(
            _required(env, f"MCP_SMOKE_{prefix}_ALIASES"),
            f"MCP_SMOKE_{prefix}_ALIASES",
        ),
        _required(env, f"MCP_SMOKE_{prefix}_CALL_ALIAS"),
        _call_arguments(env, f"MCP_SMOKE_{prefix}_CALL_ARGUMENTS"),
    )
    return target, resource_id


def _persisted_id(value: str, name: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SmokeConfigurationError(f"invalid_{name.lower()}") from exc
    if str(parsed) != value or parsed.version not in {4, 5}:
        raise SmokeConfigurationError(f"invalid_{name.lower()}")
    payload = parsed.bytes
    hex_payload = parsed.hex
    if len(set(hex_payload)) < 6:
        raise SmokeConfigurationError(f"placeholder_{name.lower()}")
    if _has_proper_period(hex_payload, ignored=frozenset({12, 16})):
        raise SmokeConfigurationError(f"placeholder_{name.lower()}")
    if _is_arithmetic_sequence(payload, ignored=frozenset({6, 8})):
        raise SmokeConfigurationError(f"placeholder_{name.lower()}")
    return value.lower()


def _token(env: Mapping[str, str], name: str, *, service: bool) -> str:
    value = _credential(env, name)
    pattern = _SERVICE_TOKEN_RE if service else _CONNECTION_TOKEN_RE
    if pattern.fullmatch(value) is None:
        raise SmokeConfigurationError(f"invalid_{name.lower()}")
    body = value.removeprefix("mcp_svc_" if service else "mcp_")
    try:
        payload = base64.urlsafe_b64decode(f"{body}=")
    except (ValueError, binascii.Error) as exc:
        raise SmokeConfigurationError(f"invalid_{name.lower()}") from exc
    canonical = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    if len(payload) != 32 or canonical != body:
        raise SmokeConfigurationError(f"invalid_{name.lower()}")
    if (
        len(set(body)) < 10
        or len(set(payload)) < 10
        or _has_proper_period(body)
        or _has_proper_period(payload)
        or _is_arithmetic_sequence(payload)
    ):
        raise SmokeConfigurationError(f"placeholder_{name.lower()}")
    return value


def load_config(env: Mapping[str, str]) -> SmokeConfig:
    if not _enabled(env):
        raise SmokeConfigurationError("smoke_not_enabled")
    mode = _required(env, "MCP_SMOKE_MODE").lower()
    if mode not in {"local", "production"}:
        raise SmokeConfigurationError("invalid_smoke_mode")
    base_url = _normalized_url(_required(env, "MCP_SMOKE_BASE_URL"))
    host = (urlsplit(base_url).hostname or "").lower()
    production = mode == "production"
    if production:
        if env.get("MCP_SMOKE_PRODUCTION_OPT_IN") != _PRODUCTION_OPT_IN:
            raise SmokeConfigurationError("production_opt_in_required")
        if env.get("MCP_SMOKE_WRITTEN_AUTHORIZATION") != _AUTHORIZATION_ACK:
            raise SmokeConfigurationError("written_authorization_required")
        _validate_production_host(base_url)
        declared_host = _required(env, "MCP_SMOKE_PRODUCTION_HOST").lower().rstrip(".")
        if declared_host != host:
            raise SmokeConfigurationError("production_host_mismatch")
    elif host not in _LOOPBACK_HOSTS:
        raise SmokeConfigurationError("local_loopback_required")

    connection, connection_id = _target(
        env, "CONNECTION", "connection-1", base_url, "connection"
    )
    second_connection, second_connection_id = _target(
        env, "SECOND_CONNECTION", "connection-2", base_url, "connection"
    )
    service, service_id = _target(env, "SERVICE", "service", base_url, "service")
    wrong_service_id = _persisted_id(
        _required(env, "MCP_SMOKE_WRONG_SERVICE_ID"),
        "MCP_SMOKE_WRONG_SERVICE_ID",
    )
    wrong_service_endpoint = _explicit_endpoint(
        env,
        "MCP_SMOKE_WRONG_SERVICE_ENDPOINT",
        base_url,
        "service",
        wrong_service_id,
    )
    bad_connection_token = _token(
        env, "MCP_SMOKE_BAD_CONNECTION_TOKEN", service=False
    )
    bad_service_token = _token(env, "MCP_SMOKE_BAD_SERVICE_TOKEN", service=True)

    if connection_id == second_connection_id:
        raise SmokeConfigurationError("connection_ids_not_distinct")
    if service_id == wrong_service_id:
        raise SmokeConfigurationError("service_ids_not_distinct")
    tokens = {
        connection.token,
        second_connection.token,
        service.token,
        bad_connection_token,
        bad_service_token,
    }
    if len(tokens) != 5:
        raise SmokeConfigurationError("tokens_not_distinct")
    if production:
        values = (
            connection_id,
            second_connection_id,
            service_id,
            wrong_service_id,
            connection.token,
            second_connection.token,
            service.token,
            bad_connection_token,
            bad_service_token,
        )
        if any(_is_placeholder(value) for value in values):
            raise SmokeConfigurationError("production_placeholder_rejected")

    return SmokeConfig(
        connection,
        second_connection,
        service,
        wrong_service_endpoint,
        bad_connection_token,
        bad_service_token,
        production,
    )


async def _list_aliases(target: SmokeTarget) -> tuple[str, ...]:
    async with streamablehttp_client(
        target.endpoint, headers={"Authorization": f"Bearer {target.token}"}
    ) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = tuple(tool.name for tool in tools.tools)
            if target.call_alias not in names:
                raise AssertionError("configured call alias is absent")
            await session.call_tool(target.call_alias, dict(target.call_arguments))
            return names


def _safe_failure_code(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, AssertionError):
        return "contract_failed"
    return "probe_failed"


def _recognized_unauthorized(exc: Exception) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(
            isinstance(child, Exception) and _recognized_unauthorized(child)
            for child in exc.exceptions
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _UNAUTHORIZED_STATUSES
    if isinstance(exc, McpError):
        data = exc.error.data
        data_status = data.get("http_status") if isinstance(data, dict) else None
        return exc.error.code in _UNAUTHORIZED_STATUSES or data_status in _UNAUTHORIZED_STATUSES
    return False


async def verify_target(
    target: SmokeTarget,
    probe: Callable[[SmokeTarget], Awaitable[Sequence[str]]] = _list_aliases,
) -> bool:
    try:
        names = tuple(await probe(target))
        if len(names) != len(set(names)):
            print(f"FAIL target={target.safe_id} code=duplicate_alias count={len(names)}")
            return False
        actual = set(names)
        if actual != target.expected_aliases:
            print(
                f"FAIL target={target.safe_id} code=alias_set_mismatch "
                f"count={len(actual.symmetric_difference(target.expected_aliases))}"
            )
            return False
        print(f"PASS target={target.safe_id} code=aliases_verified count={len(names)}")
        return True
    except Exception as exc:
        print(f"FAIL target={target.safe_id} code={_safe_failure_code(exc)} count=0")
        return False


async def _rejection_probe(endpoint: str, token: str) -> None:
    async with streamablehttp_client(
        endpoint, headers={"Authorization": f"Bearer {token}"}
    ) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            await session.list_tools()


async def verify_rejected(
    safe_id: str,
    endpoint: str,
    token: str,
    probe: Callable[[str, str], Awaitable[None]] = _rejection_probe,
) -> bool:
    try:
        await probe(endpoint, token)
    except Exception as exc:
        if _recognized_unauthorized(exc):
            print(f"PASS target={safe_id} code=unauthorized count=1")
            return True
        print(f"FAIL target={safe_id} code=rejection_unrecognized count=0")
        return False
    print(f"FAIL target={safe_id} code=unexpected_accept count=1")
    return False


async def run_smoke(config: SmokeConfig) -> bool:
    checks = [
        await verify_rejected(
            "bad-connection-token",
            config.connection.endpoint,
            config.bad_connection_token,
        ),
        await verify_rejected(
            "bad-connection-token-at-connection-2",
            config.second_connection.endpoint,
            config.bad_connection_token,
        ),
        await verify_rejected(
            "bad-service-token", config.service.endpoint, config.bad_service_token
        ),
        await verify_rejected(
            "bad-service-token-at-wrong-service",
            config.wrong_service_endpoint,
            config.bad_service_token,
        ),
        await verify_target(config.connection),
        await verify_target(config.second_connection),
        await verify_target(config.service),
        await verify_rejected(
            "service-at-connection", config.connection.endpoint, config.service.token
        ),
        await verify_rejected(
            "service-at-connection-2",
            config.second_connection.endpoint,
            config.service.token,
        ),
        await verify_rejected(
            "connection-at-service", config.service.endpoint, config.connection.token
        ),
        await verify_rejected(
            "connection-2-at-service",
            config.service.endpoint,
            config.second_connection.token,
        ),
        await verify_rejected(
            "connection-1-at-connection-2",
            config.second_connection.endpoint,
            config.connection.token,
        ),
        await verify_rejected(
            "connection-2-at-connection-1",
            config.connection.endpoint,
            config.second_connection.token,
        ),
        await verify_rejected(
            "wrong-service", config.wrong_service_endpoint, config.service.token
        ),
    ]
    passed = sum(checks)
    result = "pass" if passed == len(checks) else "fail"
    print(f"SUMMARY target=rollout code={result} count={passed}/{len(checks)}")
    return passed == len(checks)


@pytest.mark.parametrize(
    ("route", "resource_id", "expected"),
    [
        ("connection", "a/b", "http://localhost:8000/mcp/a%2Fb"),
        ("service", "svc 1", "http://localhost:8000/mcp/service/svc%201"),
    ],
)
def test_build_endpoint_encodes_ids(route, resource_id, expected):
    assert build_endpoint("http://localhost:8000/", route, resource_id) == expected


@pytest.mark.parametrize(
    "endpoint", ["http://localhost:not-a-port", "http://[broken-ipv6:8000"]
)
def test_malformed_url_is_mapped_to_fixed_configuration_code(endpoint):
    with pytest.raises(SmokeConfigurationError) as rejected:
        _normalized_url(endpoint)
    assert rejected.value.code == "invalid_endpoint"


def test_production_requires_explicit_opt_in_ack_and_public_https_host():
    env = _complete_env("https://mcp.authorized-rollout.cn", mode="production")
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == "production_opt_in_required"
    env["MCP_SMOKE_PRODUCTION_OPT_IN"] = _PRODUCTION_OPT_IN
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == "written_authorization_required"
    env["MCP_SMOKE_WRITTEN_AUTHORIZATION"] = _AUTHORIZATION_ACK
    assert load_config(env).production is True


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.com",
        "https://sub.example.com",
        "https://example.net",
        "https://sub.example.net",
        "https://example.org",
        "https://sub.example.org",
        "https://mcp.example.test",
        "https://localhost",
        "http://mcp.authorized-rollout.cn",
        "https://127.0.0.1",
    ],
)
def test_production_rejects_template_or_non_public_hosts(base_url):
    env = _complete_env(base_url, mode="production", authorize=True)
    with pytest.raises(SmokeConfigurationError):
        load_config(env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MCP_SMOKE_CONNECTION_ID", "replace-me"),
        ("MCP_SMOKE_SECOND_CONNECTION_ID", "your-connection-id-here"),
        ("MCP_SMOKE_SERVICE_ID", "dummy"),
        ("MCP_SMOKE_WRONG_SERVICE_ID", "todo"),
        ("MCP_SMOKE_CONNECTION_TOKEN", "test-token"),
        ("MCP_SMOKE_SECOND_CONNECTION_TOKEN", "replace_with_token"),
        ("MCP_SMOKE_SERVICE_TOKEN", "your-token-here"),
        ("MCP_SMOKE_BAD_CONNECTION_TOKEN", "your_token"),
        ("MCP_SMOKE_BAD_SERVICE_TOKEN", "wrong-token"),
    ],
)
def test_production_rejects_placeholder_ids_and_tokens(name, value):
    env = _complete_env(
        "https://mcp.authorized-rollout.cn", mode="production", authorize=True
    )
    env[name] = value
    if name.endswith("_ID"):
        endpoint_name = name.removesuffix("_ID") + "_ENDPOINT"
        route = "service" if "SERVICE" in name else "connection"
        env[endpoint_name] = build_endpoint(env["MCP_SMOKE_BASE_URL"], route, value)
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code.startswith("invalid_mcp_smoke_")


@pytest.mark.parametrize(
    "name",
    [
        "MCP_SMOKE_CONNECTION_TOKEN",
        "MCP_SMOKE_SECOND_CONNECTION_TOKEN",
        "MCP_SMOKE_SERVICE_TOKEN",
        "MCP_SMOKE_BAD_CONNECTION_TOKEN",
        "MCP_SMOKE_BAD_SERVICE_TOKEN",
    ],
)
def test_tokens_reject_edge_whitespace_without_normalizing(name):
    env = _complete_env("http://127.0.0.1:8000")
    env[name] = f" {env[name]} "
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == f"whitespace_{name.lower()}"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MCP_SMOKE_CONNECTION_TOKEN", f"mcp_{'A' * 42}"),
        ("MCP_SMOKE_SECOND_CONNECTION_TOKEN", f"mcp_{'A' * 42}!"),
        ("MCP_SMOKE_SERVICE_TOKEN", f"mcp_{'A' * 43}"),
        ("MCP_SMOKE_BAD_CONNECTION_TOKEN", f"mcp_svc_{'A' * 43}"),
        ("MCP_SMOKE_BAD_SERVICE_TOKEN", f"mcp_svc_{'A' * 42}"),
    ],
)
def test_each_token_category_enforces_generator_prefix_length_and_charset(name, value):
    env = _complete_env("http://127.0.0.1:8000")
    env[name] = value
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == f"invalid_{name.lower()}"


@pytest.mark.parametrize(
    ("name", "payload", "service"),
    [
        ("MCP_SMOKE_CONNECTION_TOKEN", b"A" * 32, False),
        ("MCP_SMOKE_SECOND_CONNECTION_TOKEN", b"AB" * 16, False),
        ("MCP_SMOKE_SERVICE_TOKEN", b"ABCD" * 8, True),
        ("MCP_SMOKE_BAD_CONNECTION_TOKEN", bytes(range(32)), False),
        ("MCP_SMOKE_BAD_SERVICE_TOKEN", bytes(range(31, -1, -1)), True),
    ],
)
def test_all_token_roles_reject_obvious_low_diversity_periodic_or_sequential_payloads(
    name, payload, service
):
    body = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    env = _complete_env("http://127.0.0.1:8000")
    env[name] = f"{'mcp_svc_' if service else 'mcp_'}{body}"
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == f"placeholder_{name.lower()}"


@pytest.mark.parametrize(
    ("name", "motif", "service"),
    [
        ("MCP_SMOKE_CONNECTION_TOKEN", bytes(range(10, 20)), False),
        ("MCP_SMOKE_BAD_SERVICE_TOKEN", bytes(range(30, 46)), True),
    ],
)
def test_token_payload_rejects_long_motif_repetition_and_truncation(
    name, motif, service
):
    payload = (motif * 4)[:32]
    body = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    env = _complete_env("http://127.0.0.1:8000")
    env[name] = f"{'mcp_svc_' if service else 'mcp_'}{body}"
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == f"placeholder_{name.lower()}"


def test_token_suffix_must_be_canonical_base64url_for_exactly_32_bytes():
    env = _complete_env("http://127.0.0.1:8000")
    valid = env["MCP_SMOKE_CONNECTION_TOKEN"]
    replacement = "B" if valid[-1] != "B" else "C"
    env["MCP_SMOKE_CONNECTION_TOKEN"] = f"{valid[:-1]}{replacement}"
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == "invalid_mcp_smoke_connection_token"


def test_all_explicit_endpoints_and_second_connection_are_required():
    env = _complete_env("http://127.0.0.1:8000")
    del env["MCP_SMOKE_SECOND_CONNECTION_ENDPOINT"]
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == "missing_mcp_smoke_second_connection_endpoint"


@pytest.mark.parametrize(
    "name",
    [
        "MCP_SMOKE_BASE_URL",
        "MCP_SMOKE_CONNECTION_ID",
        "MCP_SMOKE_CONNECTION_ENDPOINT",
        "MCP_SMOKE_CONNECTION_TOKEN",
        "MCP_SMOKE_SECOND_CONNECTION_ID",
        "MCP_SMOKE_SECOND_CONNECTION_ENDPOINT",
        "MCP_SMOKE_SECOND_CONNECTION_TOKEN",
        "MCP_SMOKE_SERVICE_ID",
        "MCP_SMOKE_SERVICE_ENDPOINT",
        "MCP_SMOKE_SERVICE_TOKEN",
        "MCP_SMOKE_WRONG_SERVICE_ID",
        "MCP_SMOKE_WRONG_SERVICE_ENDPOINT",
        "MCP_SMOKE_BAD_CONNECTION_TOKEN",
        "MCP_SMOKE_BAD_SERVICE_TOKEN",
    ],
)
def test_each_live_identity_endpoint_and_token_is_explicit(name):
    env = _complete_env("http://127.0.0.1:8000")
    del env[name]
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == f"missing_{name.lower()}"


def test_production_endpoint_must_match_constructed_authorized_host():
    env = _complete_env(
        "https://mcp.authorized-rollout.cn", mode="production", authorize=True
    )
    env["MCP_SMOKE_SERVICE_ENDPOINT"] = "https://example.com/mcp/service/template"
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == "mismatch_mcp_smoke_service_endpoint"


def test_declared_production_host_must_exactly_match_endpoint_host():
    env = _complete_env(
        "https://mcp.authorized-rollout.cn", mode="production", authorize=True
    )
    env["MCP_SMOKE_PRODUCTION_HOST"] = "other.authorized-rollout.cn"
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == "production_host_mismatch"


@pytest.mark.parametrize(
    ("left", "right", "code"),
    [
        ("MCP_SMOKE_CONNECTION_ID", "MCP_SMOKE_SECOND_CONNECTION_ID", "connection_ids_not_distinct"),
        ("MCP_SMOKE_SERVICE_ID", "MCP_SMOKE_WRONG_SERVICE_ID", "service_ids_not_distinct"),
        (
            "MCP_SMOKE_CONNECTION_TOKEN",
            "MCP_SMOKE_BAD_CONNECTION_TOKEN",
            "tokens_not_distinct",
        ),
        (
            "MCP_SMOKE_CONNECTION_TOKEN",
            "MCP_SMOKE_SECOND_CONNECTION_TOKEN",
            "tokens_not_distinct",
        ),
        (
            "MCP_SMOKE_SERVICE_TOKEN",
            "MCP_SMOKE_BAD_SERVICE_TOKEN",
            "tokens_not_distinct",
        ),
    ],
)
def test_ids_and_tokens_must_be_distinct(left, right, code):
    env = _complete_env("http://127.0.0.1:8000")
    env[right] = env[left]
    if right.endswith("_ID"):
        endpoint_name = right.removesuffix("_ID") + "_ENDPOINT"
        route = "service" if "SERVICE" in right else "connection"
        env[endpoint_name] = build_endpoint(env["MCP_SMOKE_BASE_URL"], route, env[right])
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == code


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MCP_SMOKE_CONNECTION_ID", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("MCP_SMOKE_SECOND_CONNECTION_ID", "12341234-1234-4234-9234-123412341234"),
        ("MCP_SMOKE_SERVICE_ID", "01234567-89ab-4def-8123-456789abcdef"),
    ],
)
def test_persisted_ids_reject_obvious_low_diversity_periodic_or_sequential_templates(
    name, value
):
    env = _complete_env("http://127.0.0.1:8000")
    env[name] = value
    endpoint_name = name.removesuffix("_ID") + "_ENDPOINT"
    route = "service" if "SERVICE" in name else "connection"
    env[endpoint_name] = build_endpoint(env["MCP_SMOKE_BASE_URL"], route, value)
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == f"placeholder_{name.lower()}"


@pytest.mark.parametrize(
    "value",
    [
        "550E8400-E29B-41D4-A716-446655440000",
        "550e8400-E29b-41d4-a716-446655440000",
    ],
)
def test_persisted_ids_require_original_canonical_lowercase(value):
    env = _complete_env("http://127.0.0.1:8000")
    env["MCP_SMOKE_CONNECTION_ID"] = value
    env["MCP_SMOKE_CONNECTION_ENDPOINT"] = build_endpoint(
        env["MCP_SMOKE_BASE_URL"], "connection", value
    )
    with pytest.raises(SmokeConfigurationError) as rejected:
        load_config(env)
    assert rejected.value.code == "invalid_mcp_smoke_connection_id"


def test_aliases_are_an_exact_set_and_output_is_redacted(capsys):
    async def extra_alias(_target: SmokeTarget) -> Sequence[str]:
        return ("expected.alias", "unexpected.alias")

    target = SmokeTarget(
        "service", "https://redacted.invalid", "raw-token", frozenset({"expected.alias"}),
        "expected.alias", {},
    )
    assert asyncio.run(verify_target(target, extra_alias)) is False
    output = capsys.readouterr().out
    assert output == "FAIL target=service code=alias_set_mismatch count=1\n"
    assert "raw-token" not in output


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://redacted.invalid")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("redacted", request=request, response=response)


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("protocol"),
        httpx.ConnectError("dns", request=httpx.Request("POST", "https://redacted.invalid")),
        TimeoutError("timeout"),
        AssertionError("programming"),
        _http_error(500),
    ],
)
def test_rejection_fails_closed_for_unrecognized_failures(failure, capsys):
    async def rejected(_endpoint: str, _token: str) -> None:
        raise failure

    assert asyncio.run(verify_rejected("wrong-service", "https://redacted.invalid", "secret", rejected)) is False
    assert capsys.readouterr().out == "FAIL target=wrong-service code=rejection_unrecognized count=0\n"


@pytest.mark.parametrize("status", [401, 403])
def test_rejection_accepts_only_bounded_unauthorized_http(status, capsys):
    async def rejected(_endpoint: str, _token: str) -> None:
        raise ExceptionGroup("transport", [_http_error(status)])

    assert asyncio.run(verify_rejected("wrong-service", "https://redacted.invalid", "secret", rejected)) is True
    assert capsys.readouterr().out == "PASS target=wrong-service code=unauthorized count=1\n"


def test_cancellation_is_not_converted_to_a_rejection_pass():
    async def cancelled(_endpoint: str, _token: str) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            verify_rejected(
                "wrong-service", "https://redacted.invalid", "secret", cancelled
            )
        )


def test_run_smoke_has_complete_stable_cross_scope_trace(monkeypatch):
    trace = []

    async def accepted(target):
        trace.append(("accept", target.safe_id))
        return True

    async def rejected(safe_id, _endpoint, _token):
        trace.append(("reject", safe_id))
        return True

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "verify_target", accepted)
    monkeypatch.setattr(module, "verify_rejected", rejected)
    config = load_config(_complete_env("http://127.0.0.1:8000"))

    assert asyncio.run(run_smoke(config)) is True
    assert trace == [
        ("reject", "bad-connection-token"),
        ("reject", "bad-connection-token-at-connection-2"),
        ("reject", "bad-service-token"),
        ("reject", "bad-service-token-at-wrong-service"),
        ("accept", "connection-1"),
        ("accept", "connection-2"),
        ("accept", "service"),
        ("reject", "service-at-connection"),
        ("reject", "service-at-connection-2"),
        ("reject", "connection-at-service"),
        ("reject", "connection-2-at-service"),
        ("reject", "connection-1-at-connection-2"),
        ("reject", "connection-2-at-connection-1"),
        ("reject", "wrong-service"),
    ]


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_status"),
    [
        (asyncio.CancelledError(), "cancelled", 130),
        (ExceptionGroup("secret-group", [RuntimeError("secret-value")]), "fatal", 1),
        (BaseException("secret-value"), "fatal", 1),
        (KeyboardInterrupt("secret-value"), "interrupted", 130),
        (SystemExit(7), "terminated", 7),
        (SystemExit(0), "terminated", 1),
        (SystemExit(-4), "terminated", 1),
        (SystemExit(126), "terminated", 1),
        (SystemExit(True), "terminated", 1),
        (SystemExit("secret-value"), "terminated", 1),
    ],
)
def test_cli_contains_base_failures_without_message_or_traceback(
    failure, expected_code, expected_status, capsys
):
    async def failed(_config):
        raise failure

    status = asyncio.run(
        run_cli(_complete_env("http://127.0.0.1:8000"), runner=failed)
    )
    captured = capsys.readouterr()
    assert status == expected_status
    assert captured.out == f"FAIL target=runner code={expected_code} count=0\n"
    assert captured.err == ""
    assert "secret" not in captured.out


def _test_uuid(seed: str, *, version: int = 4) -> str:
    payload = bytearray(hashlib.sha256(seed.encode("ascii")).digest()[:16])
    payload[6] = (payload[6] & 0x0F) | (version << 4)
    payload[8] = (payload[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(payload)))


def _test_token(seed: str, *, service: bool = False) -> str:
    body = base64.urlsafe_b64encode(
        hashlib.sha256(seed.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return f"{'mcp_svc_' if service else 'mcp_'}{body}"


def _complete_env(
    base_url: str, *, mode: str = "local", authorize: bool = False
) -> dict[str, str]:
    ids = {
        "CONNECTION": _test_uuid("connection-one-fixture"),
        "SECOND_CONNECTION": _test_uuid("connection-two-fixture"),
        "SERVICE": _test_uuid("service-fixture"),
        "WRONG_SERVICE": _test_uuid("wrong-service-fixture", version=5),
    }
    env = {
        "MCP_SMOKE_RUN": "1",
        "MCP_SMOKE_MODE": mode,
        "MCP_SMOKE_BASE_URL": base_url,
        "MCP_SMOKE_BAD_CONNECTION_TOKEN": _test_token("bad-connection-fixture"),
        "MCP_SMOKE_BAD_SERVICE_TOKEN": _test_token(
            "bad-service-fixture", service=True
        ),
    }
    for prefix, resource_id in ids.items():
        env[f"MCP_SMOKE_{prefix}_ID"] = resource_id
        route = "service" if "SERVICE" in prefix else "connection"
        env[f"MCP_SMOKE_{prefix}_ENDPOINT"] = build_endpoint(base_url, route, resource_id)
        if prefix != "WRONG_SERVICE":
            env[f"MCP_SMOKE_{prefix}_TOKEN"] = _test_token(
                f"{prefix.lower()}-fixture", service=prefix == "SERVICE"
            )
            env[f"MCP_SMOKE_{prefix}_ALIASES"] = f"{prefix.lower()}.list"
            env[f"MCP_SMOKE_{prefix}_CALL_ALIAS"] = f"{prefix.lower()}.list"
            env[f"MCP_SMOKE_{prefix}_CALL_ARGUMENTS"] = "{}"
    if authorize:
        env["MCP_SMOKE_PRODUCTION_OPT_IN"] = _PRODUCTION_OPT_IN
        env["MCP_SMOKE_WRITTEN_AUTHORIZATION"] = _AUTHORIZATION_ACK
    if mode == "production":
        env["MCP_SMOKE_PRODUCTION_HOST"] = (urlsplit(base_url).hostname or "")
    return env


@pytest.mark.skipif(
    not _enabled(os.environ), reason="set explicit MCP_SMOKE_* inputs to run live smoke"
)
def test_live_rollout_smoke():
    assert asyncio.run(run_smoke(load_config(os.environ)))


async def run_cli(
    env: Mapping[str, str],
    *,
    runner: Callable[[SmokeConfig], Awaitable[bool]] = run_smoke,
) -> int:
    try:
        config = load_config(env)
        return 0 if await runner(config) else 1
    except SmokeConfigurationError as exc:
        print(f"FAIL target=config code={exc.code} count=0")
        return 2
    except asyncio.CancelledError:
        print("FAIL target=runner code=cancelled count=0")
        return 130
    except KeyboardInterrupt:
        print("FAIL target=runner code=interrupted count=0")
        return 130
    except SystemExit as exc:
        print("FAIL target=runner code=terminated count=0")
        return exc.code if type(exc.code) is int and 1 <= exc.code <= 125 else 1
    except BaseExceptionGroup:
        print("FAIL target=runner code=fatal count=0")
        return 1
    except BaseException:
        print("FAIL target=runner code=fatal count=0")
        return 1


async def main() -> int:
    return await run_cli(os.environ)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
