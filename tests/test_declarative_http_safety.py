from __future__ import annotations

import gzip
import json

import httpx
import httpcore
import pytest

from app.connectors.declarative.http_client import (
    _PinnedNetworkBackend,
    ResponseTooLargeError,
    SafeRequestError,
    SafeHttpClient,
    TargetGuard,
)
from app.connectors.declarative.models import UnsafeTargetError


async def _public_resolver(_: str, __: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1/items",
        "https://127.0.0.1/admin",
        "https://169.254.169.254/latest/meta-data",
        "https://user:password@api.example.com/v1/items",
    ],
)
async def test_safe_http_client_rejects_unsafe_targets(url: str) -> None:
    client = SafeHttpClient(
        allowed_hosts={"api.example.com"},
        resolver=_public_resolver,
    )

    with pytest.raises(UnsafeTargetError):
        await client.request("GET", url, headers={}, json_body=None)


@pytest.mark.asyncio
async def test_target_guard_rechecks_dns_for_each_redirect_hop() -> None:
    calls: list[str] = []

    async def rebinding_resolver(host: str, _: int) -> list[str]:
        calls.append(host)
        return ["93.184.216.34"] if len(calls) == 1 else ["127.0.0.1"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/next"}, request=request)

    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=rebinding_resolver,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UnsafeTargetError):
        await client.request(
            "GET",
            "https://api.example.com/start",
            headers={},
            json_body=None,
        )

    assert calls == ["api.example.com", "api.example.com"]


@pytest.mark.asyncio
async def test_safe_http_client_streaming_limit_rejects_oversized_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 33, request=request)

    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_public_resolver,
        max_response_bytes=32,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ResponseTooLargeError):
        await client.request(
            "GET",
            "https://api.example.com/v1/items",
            headers={},
            json_body=None,
        )


@pytest.mark.asyncio
async def test_cross_host_redirect_is_rejected_before_a_second_request() -> None:
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        if request.url.host == "api.example.com":
            return httpx.Response(
                302,
                headers={"location": "https://secondary.example.com/v1/items"},
                request=request,
            )
        return httpx.Response(200, content=json.dumps({"ok": True}), request=request)

    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com", "secondary.example.com"},
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SafeRequestError, match="unsafe redirect"):
        await client.request(
            "GET",
            "https://api.example.com/v1/items",
            headers={"Authorization": "Bearer never-forward", "X-Trace": "safe"},
            json_body=None,
        )

    assert [request.url.host for request in received] == ["api.example.com"]


@pytest.mark.asyncio
async def test_cross_origin_redirect_never_replays_a_request_body() -> None:
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        if request.url.host == "api.example.com":
            return httpx.Response(
                307,
                headers={"location": "https://other.example.com/collect"},
                request=request,
            )
        return httpx.Response(200, json={"ok": True}, request=request)

    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com", "other.example.com"},
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SafeRequestError, match="unsafe redirect"):
        await client.request(
            "POST",
            "https://api.example.com/start",
            headers={"Authorization": "Bearer sensitive"},
            json_body={"secret": "never-replay"},
        )

    assert [request.url.host for request in received] == ["api.example.com"]


def test_public_safe_client_constructor_rejects_arbitrary_async_client() -> None:
    injected = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    )

    with pytest.raises(TypeError):
        SafeHttpClient(
            allowed_hosts={"api.example.com"},
            resolver=_public_resolver,
            client=injected,
        )


@pytest.mark.asyncio
async def test_target_guard_rejects_private_dns_answer_even_with_a_public_answer() -> None:
    async def mixed_resolver(_: str, __: int) -> list[str]:
        return ["93.184.216.34", "10.0.0.8"]

    guard = TargetGuard({"api.example.com"}, resolver=mixed_resolver)

    with pytest.raises(UnsafeTargetError):
        await guard.assert_allowed("https://api.example.com/v1/items")


@pytest.mark.asyncio
async def test_target_guard_rejects_port_zero_before_transport() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, request=request)

    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UnsafeTargetError):
        await client.request(
            "GET",
            "https://api.example.com:0/v1/items",
            headers={},
            json_body=None,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_safe_http_client_redacts_transport_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("credential=never-forward", request=request)

    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SafeRequestError) as exc_info:
        await client.request(
            "GET",
            "https://api.example.com/v1/items",
            headers={},
            json_body=None,
        )

    assert "credential=never-forward" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_safe_http_client_redacts_invalid_header_unicode() -> None:
    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_public_resolver,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
    )

    with pytest.raises(SafeRequestError) as exc_info:
        await client.request(
            "GET",
            "https://api.example.com/v1/items",
            headers={"X-Value": "\ud800"},
            json_body=None,
        )

    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_safe_http_client_rejects_requests_past_its_page_limit() -> None:
    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_public_resolver,
        max_pages=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
    )

    with pytest.raises(SafeRequestError, match="pagination limit exceeded"):
        await client.request(
            "GET",
            "https://api.example.com/v1/items",
            headers={},
            json_body=None,
            page_count=2,
        )


@pytest.mark.asyncio
async def test_target_guard_rejects_localhost_even_if_a_resolver_claims_public_ip() -> None:
    guard = TargetGuard({"localhost"}, resolver=_public_resolver)

    with pytest.raises(UnsafeTargetError):
        await guard.assert_allowed("https://localhost/v1/items")


@pytest.mark.asyncio
async def test_safe_http_client_applies_its_timeout_to_a_test_transport() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)

    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_public_resolver,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=2.0,
        transport=httpx.MockTransport(handler),
    )

    await client.request(
        "GET",
        "https://api.example.com/v1/items",
        headers={},
        json_body=None,
    )

    assert seen[0].extensions["timeout"]["connect"] == 1.0
    assert seen[0].extensions["timeout"]["read"] == 2.0


class _FailingBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise RuntimeError("response credential=never-forward")
        yield b""  # pragma: no cover - makes this an async generator

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_safe_http_client_redacts_stream_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_FailingBody(), request=request)

    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SafeRequestError) as exc_info:
        await client.request(
            "GET",
            "https://api.example.com/v1/items",
            headers={},
            json_body=None,
        )

    assert "credential=never-forward" not in str(exc_info.value)


class _RecordingNetworkBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def connect_tcp(self, **kwargs):
        self.calls.append(kwargs)
        return object()

    async def sleep(self, _: float) -> None:
        return None


@pytest.mark.asyncio
async def test_dns_pinning_backend_connects_only_to_the_guarded_address() -> None:
    guard = TargetGuard({"api.example.com"}, resolver=_public_resolver)
    await guard.assert_allowed("https://api.example.com/v1/items")
    backend = _PinnedNetworkBackend(guard)
    recording = _RecordingNetworkBackend()
    backend._backend = recording  # type: ignore[assignment]

    await backend.connect_tcp("api.example.com", 443)

    assert recording.calls[0]["host"] == "93.184.216.34"
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("other.example.com", 443)


class _ClosingBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        yield b'{"ok":true}'

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_safe_http_client_closes_the_stream_before_returning_a_buffered_response() -> None:
    body = _ClosingBody()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=body, request=request)

    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )

    response = await client.request(
        "GET",
        "https://api.example.com/v1/items",
        headers={},
        json_body=None,
    )

    assert response.json() == {"ok": True}
    assert body.closed is True


@pytest.mark.asyncio
async def test_safe_http_client_does_not_reapply_upstream_content_encoding() -> None:
    compressed = gzip.compress(b'{"ok":true}')

    class RawGzipBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield compressed

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=RawGzipBody(),
            request=request,
        )

    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )

    response = await client.request(
        "GET",
        "https://api.example.com/v1/items",
        headers={},
        json_body=None,
    )

    assert response.json() == {"ok": True}
    assert "content-encoding" not in response.headers


@pytest.mark.asyncio
async def test_gzip_bomb_is_bounded_from_raw_chunks_before_decoding() -> None:
    compressed = gzip.compress(b"x" * (2 * 1024 * 1024))

    class RawCompressedBody(httpx.AsyncByteStream):
        def __init__(self, body: bytes) -> None:
            self.body = body
            self.closed = False

        async def __aiter__(self):
            yield self.body

        async def aclose(self) -> None:
            self.closed = True

    body = RawCompressedBody(compressed)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=body,
            request=request,
        )
    client = SafeHttpClient._for_test(
        allowed_hosts={"api.example.com"},
        resolver=_public_resolver,
        max_response_bytes=4_096,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ResponseTooLargeError):
        await client.request(
            "GET",
            "https://api.example.com/bomb",
            headers={},
            json_body=None,
        )

    assert body.closed is True


@pytest.mark.parametrize("module_name", ["httpx", "httpcore"])
def test_pinned_transport_refuses_unverified_dependency_versions(
    monkeypatch, module_name: str
) -> None:
    module = httpx if module_name == "httpx" else httpcore
    monkeypatch.setattr(module, "__version__", "0.0.0")

    with pytest.raises(RuntimeError, match="unsupported HTTP transport runtime"):
        SafeHttpClient(
            allowed_hosts={"api.example.com"},
            resolver=_public_resolver,
        )


def test_pinned_transport_refuses_missing_private_backend_hook(monkeypatch) -> None:
    def broken_transport_init(transport, *_args, **_kwargs) -> None:
        transport._pool = object()

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "__init__", broken_transport_init)

    with pytest.raises(RuntimeError, match="private network backend hook"):
        SafeHttpClient(
            allowed_hosts={"api.example.com"},
            resolver=_public_resolver,
        )


@pytest.mark.asyncio
async def test_target_guard_does_not_chain_url_parse_details() -> None:
    guard = TargetGuard({"api.example.com"}, resolver=_public_resolver)

    with pytest.raises(UnsafeTargetError) as exc_info:
        await guard.assert_allowed("https://api.example.com:not-a-port/items")

    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_target_guard_rejects_raw_backslashes_and_control_characters() -> None:
    guard = TargetGuard({"api.example.com"}, resolver=_public_resolver)

    for url in (
        "https://api.example.com/v1\\internal",
        "https://api.example.com/v1/items\nnext",
    ):
        with pytest.raises(UnsafeTargetError):
            await guard.assert_allowed(url)
