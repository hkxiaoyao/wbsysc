import asyncio
import logging

import pytest

from app import mcp_audit
from app.auth import TenantCtx, _ctx
from app.mcp_log_models import McpLogEvent


@pytest.mark.parametrize(
    ("value", "leaked"),
    [
        ("Authorization: Bearer abc", "abc"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        (
            "authorization = Digest username=admin response=digest-secret",
            "digest-secret",
        ),
        ("BEARER abc", "abc"),
        ("mcp_token = token-value", "token-value"),
        ("MCP TOKEN: token-value", "token-value"),
        ("secret=y", "y"),
        ("Cookie: sid=z", "sid=z"),
        ("Cookie: sid=first-secret; session=second-secret", "second-secret"),
        ("SET-COOKIE = session=case-secret; HttpOnly", "case-secret"),
        ("mysql+pymysql://root:db-password@db/gateway", "db-password"),
        ("postgresql+psycopg://root:driver-secret@db/gateway", "driver-secret"),
        ("db_password=db-secret", "db-secret"),
        ({"Database-Password": "quoted-secret"}, "quoted-secret"),
        ("corp_secret=corp-value-1", "corp-value-1"),
        ("CorpSecret : corp-value-2", "corp-value-2"),
        ("corp secret = corp-value-3", "corp-value-3"),
        ({"CORP-SECRET": "corp-value-4"}, "corp-value-4"),
        ("api_key=api-value-1", "api-value-1"),
        ("API KEY : api-value-2", "api-value-2"),
        ("api-key = api-value-3", "api-value-3"),
        ({"ApiKey": "api-value-4"}, "api-value-4"),
        ("dsn=opaque-value-1", "opaque-value-1"),
        ({"DSN": "opaque-value-2"}, "opaque-value-2"),
        ({"Authorization": "Bearer abc", "mcp_token": "token-value"}, "token-value"),
    ],
)
def test_safe_summary_redacts_sensitive_values_before_truncation(value, leaked):
    summary = mcp_audit.safe_summary("prefix " + value if isinstance(value, str) else value, 512)

    assert leaked not in summary
    assert len(summary) <= 512


def test_safe_summary_redacts_new_sensitive_keys_before_length_truncation():
    summary = mcp_audit.safe_summary(
        "x" * 32 + " api_key=api-value-before-truncation",
        48,
    )

    assert "api-value" not in summary
    assert len(summary) <= 48


def test_write_event_swallows_storage_failure_and_logs_only_exception_type(
    monkeypatch, caplog
):
    monkeypatch.setattr(
        mcp_audit,
        "insert_event",
        lambda event: (_ for _ in ()).throw(RuntimeError("secret=db-secret")),
    )

    with caplog.at_level(logging.WARNING, logger="app.mcp_audit"):
        mcp_audit.write_event(McpLogEvent())

    assert "RuntimeError" in caplog.text
    assert "db-secret" not in caplog.text


def test_auth_limiter_allows_sixty_per_minute_and_prunes_expired_buckets():
    limiter = mcp_audit.AuthWriteLimiter(limit=60, window_seconds=60)

    assert all(limiter.allow("2001:0db8::1", "auth_invalid", now=0) for _ in range(60))
    assert not limiter.allow("2001:db8::1", "auth_invalid", now=59)
    assert limiter.allow("2001:db8::1", "auth_invalid", now=61)
    assert limiter.allow("2001:db8::1", "auth_ok", now=61)
    assert all("token" not in repr(key).lower() for key in limiter._buckets)


def test_auth_limiter_enforces_bucket_cap_and_coalesces_rejection_notices():
    limiter = mcp_audit.AuthWriteLimiter(
        limit=1,
        window_seconds=60,
        max_buckets=3,
    )

    assert limiter.allow_with_notice("192.0.2.1", "auth_invalid", now=0) == (
        True,
        False,
    )
    assert limiter.allow_with_notice("192.0.2.1", "auth_invalid", now=1) == (
        False,
        True,
    )
    assert limiter.allow_with_notice("192.0.2.1", "auth_invalid", now=2) == (
        False,
        False,
    )
    for suffix in range(2, 10):
        limiter.allow(f"192.0.2.{suffix}", "auth_invalid", now=2)

    assert len(limiter._buckets) == 3


def test_client_ip_from_scope_normalizes_and_rejects_non_ip_values():
    assert mcp_audit.client_ip_from_scope({"client": ("2001:0db8::1", 123)}) == "2001:db8::1"
    assert mcp_audit.client_ip_from_scope({"client": ("not-an-ip", 123)}) == ""


def test_protocol_middleware_replays_messages_and_logs_only_method():
    body = b'{"jsonrpc":"2.0","method":"tools/list","id":1}'
    original_messages = [
        {"type": "http.request", "body": body[:20], "more_body": True},
        {"type": "http.request", "body": body[20:], "more_body": False},
    ]
    received = []
    sent = []
    events = []

    async def app(scope, receive, send):
        while True:
            message = await receive()
            received.append(message)
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    queue = list(original_messages)

    async def receive():
        return queue.pop(0)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"x-request-id", b"request-1")],
        "client": ("127.0.0.1", 1234),
    }
    middleware = mcp_audit.McpProtocolAuditMiddleware(app, writer=events.append)
    tenant = TenantCtx("tenant-a", "ww", "secret", "schema", "", [], set(), "stored")
    token = _ctx.set(tenant)
    try:
        asyncio.run(middleware(scope, receive, send))
    finally:
        _ctx.reset(token)

    assert received == original_messages
    assert sent[0]["status"] == 202
    assert len(events) == 1
    event = events[0]
    assert event.event_name == "tools/list"
    assert event.tenant_id == "tenant-a"
    assert event.request_id == "request-1"
    assert event.client_ip == "127.0.0.1"
    assert event.http_status == 202
    assert body.decode() not in repr(event)


def test_protocol_middleware_replays_oversized_post_without_parsing_body():
    body = b"x" * (64 * 1024) + b'{"method":"must-not-be-retained"}'
    received = []
    events = []

    async def app(scope, receive, send):
        message = await receive()
        received.append(message["body"])
        await send({"type": "http.response.start", "status": 400, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        return None

    middleware = mcp_audit.McpProtocolAuditMiddleware(app, writer=events.append)
    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "headers": [], "client": None},
            receive,
            send,
        )
    )

    assert received == [body]
    assert events[0].event_name == "mcp_http_request"
    assert "must-not-be-retained" not in repr(events[0])


def test_protocol_middleware_streams_one_hundred_chunks_without_preconsuming():
    original_messages = [
        {
            "type": "http.request",
            "body": b"x" * 1024,
            "more_body": index < 99,
        }
        for index in range(100)
    ]
    queue = list(original_messages)
    received = []
    receive_calls = 0
    calls_at_app_start = None
    events = []

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        return queue.pop(0)

    async def app(scope, app_receive, send):
        nonlocal calls_at_app_start
        calls_at_app_start = receive_calls
        while True:
            message = await app_receive()
            received.append(message)
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        return None

    middleware = mcp_audit.McpProtocolAuditMiddleware(app, writer=events.append)
    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "headers": [], "client": None},
            receive,
            send,
        )
    )

    assert calls_at_app_start == 0
    assert received == original_messages
    assert receive_calls == 100
    assert events[0].event_name == "mcp_http_request"


def test_protocol_middleware_does_not_consume_an_unread_stream():
    events = []

    async def receive():
        raise AssertionError("middleware must not pre-consume an unread request body")

    async def app(scope, app_receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})

    async def send(message):
        return None

    middleware = mcp_audit.McpProtocolAuditMiddleware(app, writer=events.append)
    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "headers": [], "client": None},
            receive,
            send,
        )
    )

    assert events[0].event_name == "mcp_http_request"
    assert events[0].http_status == 204


def test_protocol_writer_failure_isolated_and_request_metadata_reset(caplog):
    async def app(scope, receive, send):
        assert mcp_audit.current_request_metadata()["request_id"] == "request-1"
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        return None

    def failing_writer(event):
        raise RuntimeError("secret=writer-secret")

    middleware = mcp_audit.McpProtocolAuditMiddleware(app, writer=failing_writer)
    with caplog.at_level(logging.WARNING, logger="app.mcp_audit"):
        asyncio.run(
            middleware(
                {
                    "type": "http",
                    "method": "GET",
                    "headers": [(b"x-request-id", b"request-1")],
                    "client": None,
                },
                receive,
                send,
            )
        )

    assert mcp_audit.current_request_metadata() == {}
    assert "RuntimeError" in caplog.text
    assert "writer-secret" not in caplog.text


@pytest.mark.parametrize(
    ("method", "event_name"),
    [("GET", "mcp_http_get"), ("DELETE", "mcp_http_delete")],
)
def test_protocol_middleware_names_non_post_requests(method, event_name):
    events = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        return None

    middleware = mcp_audit.McpProtocolAuditMiddleware(app, writer=events.append)
    asyncio.run(
        middleware(
            {"type": "http", "method": method, "headers": [], "client": None},
            receive,
            send,
        )
    )

    assert events[0].event_name == event_name
