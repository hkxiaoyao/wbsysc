import asyncio
import logging
import threading
import time

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
        ("private_key=private-value-1", "private-value-1"),
        ("PRIVATE KEY : private-value-2", "private-value-2"),
        ({"Private-Key": "private-value-3"}, "private-value-3"),
        ("jwt=jwt-value-1", "jwt-value-1"),
        ({"JWT": "jwt-value-2"}, "jwt-value-2"),
        ("session=session-value-1", "session-value-1"),
        ("session_id=session-value-2", "session-value-2"),
        ({"Session-Id": "session-value-3"}, "session-value-3"),
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


def test_write_event_persists_only_on_background_worker(monkeypatch):
    caller_thread = threading.get_ident()
    inserted_threads = []
    inserted = threading.Event()

    def fake_insert(event):
        inserted_threads.append(threading.get_ident())
        inserted.set()

    writer = mcp_audit.AuditEventWriter(insert=fake_insert, max_queue_size=2)
    monkeypatch.setattr(mcp_audit, "_audit_writer", writer)
    try:
        assert mcp_audit.write_event(McpLogEvent()) is True
        assert inserted.wait(1)
        assert writer.flush(1) is True
    finally:
        writer.shutdown(1)

    assert inserted_threads == [inserted_threads[0]]
    assert inserted_threads[0] != caller_thread


def test_blocking_audit_storage_does_not_block_protocol_event_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocking_insert(event):
        started.set()
        release.wait(2)

    writer = mcp_audit.AuditEventWriter(insert=blocking_insert, max_queue_size=2)
    monkeypatch.setattr(mcp_audit, "_audit_writer", writer)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        return None

    heartbeat = []

    async def exercise():
        middleware = mcp_audit.McpProtocolAuditMiddleware(app)

        async def tick():
            await asyncio.sleep(0.01)
            heartbeat.append("advanced")

        await asyncio.wait_for(
            asyncio.gather(
                middleware(
                    {"type": "http", "method": "GET", "headers": [], "client": None},
                    receive,
                    send,
                ),
                tick(),
            ),
            timeout=0.25,
        )

    try:
        asyncio.run(exercise())
        assert started.wait(1)
        assert heartbeat == ["advanced"]
    finally:
        release.set()
        writer.shutdown(1)


def test_audit_writer_lifecycle_refcounts_and_restarts_after_final_release(monkeypatch):
    initial_writer = mcp_audit.AuditEventWriter(insert=lambda event: None)
    monkeypatch.setattr(mcp_audit, "_audit_writer", initial_writer)
    monkeypatch.setattr(mcp_audit, "_audit_writer_refcount", 0, raising=False)

    first = mcp_audit.acquire_audit_writer()
    second = mcp_audit.acquire_audit_writer()

    assert first is initial_writer
    assert second is first
    assert first._thread is not None and first._thread.is_alive()
    assert mcp_audit.release_audit_writer(1) is True
    assert first._thread.is_alive()
    assert mcp_audit.release_audit_writer(1) is True
    assert not first._thread.is_alive()

    restarted = mcp_audit.acquire_audit_writer()
    try:
        assert restarted is not first
        assert restarted._thread is not None and restarted._thread.is_alive()
    finally:
        mcp_audit.release_audit_writer(1)


def test_final_release_does_not_hold_lifecycle_lock_while_storage_drains(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocking_insert(event):
        started.set()
        release.wait(2)

    initial_writer = mcp_audit.AuditEventWriter(insert=blocking_insert)
    monkeypatch.setattr(mcp_audit, "_audit_writer", initial_writer)
    monkeypatch.setattr(mcp_audit, "_audit_writer_refcount", 0)
    assert mcp_audit.acquire_audit_writer() is initial_writer
    assert mcp_audit.write_event(McpLogEvent(event_name="blocking")) is True
    assert started.wait(1)

    release_result = []
    release_thread = threading.Thread(
        target=lambda: release_result.append(mcp_audit.release_audit_writer(1))
    )
    release_thread.start()
    try:
        deadline = time.monotonic() + 1
        while initial_writer._accepting and time.monotonic() < deadline:
            time.sleep(0.01)

        started_at = time.monotonic()
        replacement = mcp_audit.acquire_audit_writer()
        assert time.monotonic() - started_at < 0.25
        assert replacement is not initial_writer
    finally:
        release.set()
        release_thread.join(2)
        mcp_audit.release_audit_writer(1)

    assert release_result == [True]


def test_write_event_swallows_queue_infrastructure_failure(monkeypatch, caplog):
    class BrokenWriter:
        def submit(self, event):
            raise RuntimeError("secret=queue-infrastructure")

    monkeypatch.setattr(mcp_audit, "_audit_writer", BrokenWriter())

    with caplog.at_level(logging.WARNING, logger="app.mcp_audit"):
        assert mcp_audit.write_event(McpLogEvent()) is False

    assert caplog.text == ""
    assert "queue-infrastructure" not in caplog.text


def test_audit_writer_queue_is_bounded_and_drop_warning_runs_off_submit_thread(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    caller_thread = threading.get_ident()
    warnings = []

    def blocking_insert(event):
        started.set()
        release.wait(2)

    writer = mcp_audit.AuditEventWriter(
        insert=blocking_insert,
        max_queue_size=2,
        warning_interval=60,
    )
    monkeypatch.setattr(
        mcp_audit.logger,
        "warning",
        lambda message, *args: warnings.append((threading.get_ident(), message, args)),
    )
    try:
        assert writer.submit(McpLogEvent(event_name="first")) is True
        assert started.wait(1)
        assert writer.submit(McpLogEvent(event_name="second")) is True
        assert writer.submit(McpLogEvent(event_name="third")) is True
        assert writer.submit(McpLogEvent(event_name="secret=drop-one")) is False
        assert writer.submit(McpLogEvent(event_name="secret=drop-two")) is False

        assert writer._queue.qsize() == 2
        assert writer.dropped_count == 2
        assert warnings == []
    finally:
        release.set()
        writer.shutdown(1)

    queue_warnings = [entry for entry in warnings if "audit queue full" in entry[1]]
    assert len(queue_warnings) == 1
    assert queue_warnings[0][0] != caller_thread
    assert queue_warnings[0][2] == ("Full", 2)
    assert "drop-one" not in repr(warnings)
    assert "drop-two" not in repr(warnings)


def test_audit_worker_isolates_storage_failure_and_flush_is_bounded(caplog):
    started = threading.Event()
    release = threading.Event()

    def failing_insert(event):
        started.set()
        release.wait(1)
        raise RuntimeError("secret=db-secret")

    writer = mcp_audit.AuditEventWriter(insert=failing_insert, max_queue_size=1)
    try:
        with caplog.at_level(logging.WARNING, logger="app.mcp_audit"):
            assert writer.submit(McpLogEvent()) is True
            assert started.wait(1)
            assert writer.flush(0.01) is False
            release.set()
            assert writer.flush(1) is True
    finally:
        release.set()
        writer.shutdown(1)

    assert "RuntimeError" in caplog.text
    assert "db-secret" not in caplog.text


def test_request_id_from_scope_preserves_request_id_and_hashes_session_fallback():
    request_scope = {"headers": [(b"x-request-id", b"request-value")]}
    session_scope = {"headers": [(b"mcp-session-id", b"opaque-session-value")]}
    metadata_summary = mcp_audit.safe_summary(
        {
            "request_id": "request-value",
            "mcp_session_id": "mcp-session-value",
        },
        512,
    )

    assert mcp_audit.request_id_from_scope(request_scope) == "request-value"
    session_request_id = mcp_audit.request_id_from_scope(session_scope)
    assert session_request_id.startswith("sha256:")
    assert len(session_request_id) == 39
    assert "opaque-session-value" not in session_request_id
    assert mcp_audit.request_id_from_scope(
        {"headers": [(b"mcp-session-id", b"")]}
    ) == ""
    assert "request-value" in metadata_summary
    assert "mcp-session-value" in metadata_summary


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
