"""Outbound HTTP with a fail-closed SSRF and bounded-response boundary."""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import re
import socket
import zlib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
import httpcore
from httpcore._backends.auto import AutoBackend

from .models import (
    ALLOWED_METHODS,
    MAX_PAGE_COUNT,
    MAX_REQUEST_BODY_BYTES,
    RequestTooLargeError,
    ResponseTooLargeError,
    SafeRequestError,
    UnsafeTargetError,
)


DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 1_024 * 1_024
DEFAULT_MAX_REDIRECTS = 3
_MAX_HEADERS = 32
_MAX_HEADER_BYTES = 8_192
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,64}$")
_PROHIBITED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "proxy-authorization",
        "proxy-connection",
        "cookie",
    }
)
_VERIFIED_HTTPX_VERSION = "0.28.1"
_VERIFIED_HTTPCORE_VERSION = "1.0.9"
_TEST_TRANSPORT_TOKEN = object()


Resolver = Callable[[str, int], Iterable[str] | Awaitable[Iterable[str]]]


@dataclass(frozen=True)
class _Target:
    host: str
    port: int
    addresses: tuple[str, ...]


def _normalize_host(host: str) -> str:
    if not isinstance(host, str) or not host or len(host) > 253:
        raise UnsafeTargetError("unsafe outbound target")
    try:
        normalized = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise UnsafeTargetError("unsafe outbound target") from None
    if not normalized or any(character.isspace() for character in normalized):
        raise UnsafeTargetError("unsafe outbound target")
    return normalized


def _is_safe_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    # ``is_global`` excludes RFC1918, loopback, link-local, multicast,
    # documentation, unspecified, and carrier-grade NAT ranges.  Spell out
    # the properties as a defense against implementation differences between
    # Python versions.
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


class TargetGuard:
    """Validate the URL and freshly resolved target for every outbound hop."""

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *,
        resolver: Resolver | Callable[[str], Iterable[str] | Awaitable[Iterable[str]]] | None = None,
        allow_ip_literals: bool = False,
        allowed_ip_literals: Iterable[str] = (),
        allowed_ports: Iterable[int] = (443,),
    ) -> None:
        if isinstance(allowed_hosts, (str, bytes)):
            raise ValueError("allowed_hosts must be an iterable of hostnames")
        hosts = tuple(_normalize_host(host) for host in allowed_hosts)
        if not hosts or any(
            "*" in host
            for host in hosts
        ):
            raise ValueError("wildcard hosts are not supported")
        ports = tuple(allowed_ports)
        if not ports or any(
            not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535
            for port in ports
        ):
            raise ValueError("allowed_ports must contain valid ports")
        ip_literals = tuple(allowed_ip_literals)
        if any(not _is_safe_address(value) for value in ip_literals):
            raise ValueError("allowed IP literals must be globally routable")
        self._allowed_hosts = frozenset(hosts)
        self._resolver = resolver
        self._allow_ip_literals = allow_ip_literals is True
        self._allowed_ip_literals = frozenset(ip_literals)
        self._allowed_ports = frozenset(ports)
        self._approved_addresses: dict[tuple[str, int], tuple[str, ...]] = {}

    async def assert_allowed(self, url: str) -> _Target:
        if not isinstance(url, str) or not url or len(url) > 2_048:
            raise UnsafeTargetError("unsafe outbound target")
        # urlsplit accepts and normalizes several raw control characters.  Do
        # not let a redirect use parser-specific backslash or whitespace
        # behavior to change the eventual HTTPX request after validation.
        if any(
            character == "\\"
            or character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            for character in url
        ):
            raise UnsafeTargetError("unsafe outbound target")
        try:
            parts = urlsplit(url)
            port = parts.port if parts.port is not None else 443
        except ValueError:
            raise UnsafeTargetError("unsafe outbound target") from None
        if (
            parts.scheme.lower() != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or port not in self._allowed_ports
        ):
            raise UnsafeTargetError("unsafe outbound target")
        host = _normalize_host(parts.hostname)
        if host == "localhost" or host.endswith(".localhost"):
            raise UnsafeTargetError("unsafe outbound target")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not self._host_is_allowed(host):
                raise UnsafeTargetError("unsafe outbound target")
            addresses = await self._resolve(host, port)
            if not addresses or any(not _is_safe_address(address) for address in addresses):
                raise UnsafeTargetError("unsafe outbound target")
        else:
            if (
                not self._allow_ip_literals
                or host not in self._allowed_ip_literals
                or not _is_safe_address(host)
            ):
                raise UnsafeTargetError("unsafe outbound target")
            addresses = (host,)
        approved = tuple(addresses)
        self._approved_addresses[(host, port)] = approved
        return _Target(host=host, port=port, addresses=approved)

    def approved_addresses(self, host: str, port: int) -> tuple[str, ...]:
        """Return only a DNS answer validated immediately before this request."""
        return self._approved_addresses.get((host.lower().rstrip("."), port), ())

    def exactly_matches_hosts(self, allowed_hosts: Iterable[str]) -> bool:
        """Whether this guard is no broader than a revision's host boundary."""
        if isinstance(allowed_hosts, (str, bytes)):
            return False
        try:
            expected = frozenset(_normalize_host(host) for host in allowed_hosts)
        except (TypeError, UnsafeTargetError):
            return False
        return bool(expected) and (
            expected == self._allowed_hosts
            and not self._allow_ip_literals
            and not self._allowed_ip_literals
            and self._allowed_ports == frozenset({443})
        )

    def _host_is_allowed(self, host: str) -> bool:
        return host in self._allowed_hosts

    async def _resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            if self._resolver is None:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    host,
                    port,
                    type=socket.SOCK_STREAM,
                )
                values: Iterable[Any] = (info[4][0] for info in infos)
            else:
                try:
                    outcome = self._resolver(host, port)  # type: ignore[misc]
                except TypeError:
                    outcome = self._resolver(host)  # type: ignore[call-arg,misc]
                values = await outcome if inspect.isawaitable(outcome) else outcome
            addresses: list[str] = []
            for value in values:
                if isinstance(value, (tuple, list)):
                    value = value[0] if value else ""
                if not isinstance(value, str):
                    raise UnsafeTargetError("unsafe outbound target")
                # Canonicalize through ipaddress before returning so a resolver
                # cannot smuggle an IPv6 zone, hostname, or malformed literal.
                addresses.append(str(ipaddress.ip_address(value)))
            return tuple(dict.fromkeys(addresses))
        except UnsafeTargetError:
            raise
        except Exception:
            # DNS errors and resolver internals are deliberately indistinct to
            # callers; error text may reveal private names or topology.
            raise UnsafeTargetError("unsafe outbound target") from None


class _PinnedNetworkBackend:
    """Connect HTTPX only to the address TargetGuard just approved.

    HTTPX/httpcore otherwise resolves the hostname a second time between a
    preflight DNS check and TCP connect.  The backend keeps the original
    hostname for HTTP Host and TLS SNI while pinning the socket peer to a
    validated address.
    """

    def __init__(self, target_guard: TargetGuard) -> None:
        self._target_guard = target_guard
        self._backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object | None = None,
    ):
        addresses = self._target_guard.approved_addresses(host, port)
        if not addresses:
            raise httpcore.ConnectError("outbound target was not prevalidated")
        return await self._backend.connect_tcp(
            host=addresses[0],
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args: object, **kwargs: object):
        raise httpcore.ConnectError("Unix sockets are not supported")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport that uses the guard-backed, DNS-pinning backend."""

    def __init__(self, target_guard: TargetGuard) -> None:
        if (
            httpx.__version__ != _VERIFIED_HTTPX_VERSION
            or httpcore.__version__ != _VERIFIED_HTTPCORE_VERSION
        ):
            raise RuntimeError("unsupported HTTP transport runtime")
        super().__init__(trust_env=False, retries=0)
        # AsyncHTTPTransport creates an httpcore connection pool.  It has no
        # public resolver hook, so replace the backend before any connection
        # is created.  The origin remains the hostname, preserving TLS SNI and
        # certificate verification while the backend pins TCP to a safe IP.
        pool = getattr(self, "_pool", None)
        if pool is None or not hasattr(pool, "_network_backend"):
            raise RuntimeError("unsupported HTTP transport private network backend hook")
        backend = _PinnedNetworkBackend(target_guard)
        pool._network_backend = backend
        if pool._network_backend is not backend:
            raise RuntimeError("unsupported HTTP transport runtime")


class SafeHttpClient:
    """A small HTTP client that never follows redirects or buffers unbounded data."""

    def __init__(
        self,
        allowed_hosts: Iterable[str] | None = None,
        *,
        target_guard: TargetGuard | None = None,
        resolver: Resolver | Callable[[str], Iterable[str] | Awaitable[Iterable[str]]] | None = None,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_pages: int = MAX_PAGE_COUNT,
        _test_transport: httpx.AsyncBaseTransport | None = None,
        _test_token: object | None = None,
    ) -> None:
        if _test_transport is not None and _test_token is not _TEST_TRANSPORT_TOKEN:
            raise TypeError("test transport is private")
        if target_guard is None:
            if allowed_hosts is None:
                raise ValueError("allowed_hosts or target_guard is required")
            target_guard = TargetGuard(allowed_hosts, resolver=resolver)
        elif allowed_hosts is not None or resolver is not None:
            raise ValueError("target_guard cannot be combined with host or resolver options")
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or not 1 <= max_response_bytes <= 16 * 1024 * 1024
        ):
            raise ValueError("max_response_bytes must be bounded")
        if (
            not isinstance(max_redirects, int)
            or isinstance(max_redirects, bool)
            or not 0 <= max_redirects <= 10
        ):
            raise ValueError("max_redirects must be bounded")
        if (
            not isinstance(max_pages, int)
            or isinstance(max_pages, bool)
            or not 1 <= max_pages <= MAX_PAGE_COUNT
        ):
            raise ValueError("max_pages must be bounded")
        if (
            not isinstance(connect_timeout_seconds, (int, float))
            or isinstance(connect_timeout_seconds, bool)
            or not isinstance(read_timeout_seconds, (int, float))
            or isinstance(read_timeout_seconds, bool)
            or not 0 < connect_timeout_seconds <= MAX_TIMEOUT_SECONDS
            or not 0 < read_timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("timeouts must be positive and bounded")
        self._target_guard = target_guard
        self._max_response_bytes = max_response_bytes
        self._max_redirects = max_redirects
        self._max_pages = max_pages
        self._uses_pinned_transport = _test_transport is None
        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=read_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        transport = (
            _PinnedAsyncHTTPTransport(target_guard)
            if _test_transport is None
            else _test_transport
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=self._timeout,
            trust_env=False,
        )

    @classmethod
    def _for_test(
        cls,
        allowed_hosts: Iterable[str],
        *,
        resolver: Resolver
        | Callable[[str], Iterable[str] | Awaitable[Iterable[str]]]
        | None = None,
        transport: httpx.AsyncBaseTransport,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_pages: int = MAX_PAGE_COUNT,
    ) -> "SafeHttpClient":
        """Build an explicit in-process test seam, unavailable to declarations."""
        return cls(
            allowed_hosts,
            resolver=resolver,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            max_response_bytes=max_response_bytes,
            max_redirects=max_redirects,
            max_pages=max_pages,
            _test_transport=transport,
            _test_token=_TEST_TRANSPORT_TOKEN,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def uses_pinned_transport(self) -> bool:
        return self._uses_pinned_transport

    def exactly_matches_hosts(self, allowed_hosts: Iterable[str]) -> bool:
        """Expose only the safe policy comparison needed by a connector."""
        return self._target_guard.exactly_matches_hosts(allowed_hosts)

    async def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: object | None,
        *,
        page_count: int = 1,
        form_body: Mapping[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> httpx.Response:
        """Execute a declared request with fresh target checks at each hop.

        ``form_body`` exists solely for the fixed OAuth client-credentials
        token exchange.  Connector operation calls use JSON only.
        """
        method = method.upper() if isinstance(method, str) else ""
        if method not in ALLOWED_METHODS:
            raise SafeRequestError("unsupported outbound method")
        if (
            not isinstance(page_count, int)
            or isinstance(page_count, bool)
            or not 1 <= page_count <= self._max_pages
        ):
            raise SafeRequestError("pagination limit exceeded")
        if json_body is not None and form_body is not None:
            raise SafeRequestError("invalid outbound request")
        if not isinstance(allow_redirects, bool):
            raise SafeRequestError("invalid redirect policy")
        request_headers = self._validated_headers(headers)
        if json_body is not None and self._json_size(json_body) > MAX_REQUEST_BODY_BYTES:
            raise RequestTooLargeError("request body exceeds limit")
        if form_body is not None:
            if not isinstance(form_body, Mapping) or any(
                not isinstance(key, str) or not isinstance(value, str) for key, value in form_body.items()
            ):
                raise SafeRequestError("invalid OAuth request")
            try:
                encoded = urlencode(dict(form_body), doseq=False).encode("utf-8")
            except (TypeError, ValueError, OverflowError):
                raise SafeRequestError("invalid OAuth request") from None
            if len(encoded) > MAX_REQUEST_BODY_BYTES:
                raise RequestTooLargeError("request body exceeds limit")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            encoded = None

        redirects = 0
        current_url = url
        current_headers = request_headers
        current_target: _Target | None = None
        while True:
            next_target = await self._target_guard.assert_allowed(current_url)
            if current_target is not None and (
                current_target.host != next_target.host or current_target.port != next_target.port
            ):
                raise SafeRequestError("unsafe redirect")
            current_target = next_target
            response = await self._send_bounded(
                method,
                current_url,
                current_headers,
                json_body=json_body,
                content=encoded,
            )
            if not response.is_redirect:
                return response
            if not allow_redirects:
                raise SafeRequestError("unsafe redirect")
            location = response.headers.get("location")
            if not location or redirects >= self._max_redirects:
                raise SafeRequestError("unsafe redirect")
            redirects += 1
            # urljoin handles relative locations, but the next loop validates
            # the resulting scheme, host, port, and fresh DNS answer before IO.
            try:
                current_url = urljoin(current_url, location)
            except (TypeError, ValueError, UnicodeError):
                raise SafeRequestError("unsafe redirect") from None

    def _validated_headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(headers, Mapping) or len(headers) > _MAX_HEADERS:
            raise SafeRequestError("invalid outbound headers")
        normalized: dict[str, str] = {}
        total_bytes = 0
        for name, value in headers.items():
            if (
                not isinstance(name, str)
                or not isinstance(value, str)
                or not _HEADER_NAME_RE.fullmatch(name)
                or name.lower() in _PROHIBITED_HEADERS
                or "\r" in value
                or "\n" in value
            ):
                raise SafeRequestError("invalid outbound headers")
            try:
                total_bytes += len(name.encode("ascii")) + len(value.encode("utf-8"))
            except UnicodeError:
                raise SafeRequestError("invalid outbound headers") from None
            if total_bytes > _MAX_HEADER_BYTES:
                raise SafeRequestError("outbound headers exceed limit")
            normalized[name] = value
        return normalized

    @staticmethod
    def _json_size(value: object) -> int:
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise SafeRequestError("request body must be JSON") from None
        return len(encoded.encode("utf-8"))

    async def _send_bounded(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        *,
        json_body: object | None,
        content: bytes | None,
    ) -> httpx.Response:
        try:
            request = self._client.build_request(
                method,
                url,
                headers=dict(headers),
                json=None if content is not None else json_body,
                content=content,
                timeout=self._timeout,
            )
            response = await self._client.send(request, stream=True, follow_redirects=False)
        except Exception:
            raise SafeRequestError("outbound request failed") from None
        try:
            if response.is_redirect:
                # Redirect bodies are not useful and can be attacker-sized.
                # Keep only the routing field needed by ``request``.  In
                # particular, do not copy Content-Encoding onto a synthetic
                # empty response or HTTPX will try to decode it again later.
                location = response.headers.get("location")
                return httpx.Response(
                    response.status_code,
                    headers={} if location is None else {"location": location},
                )
            content_bytes = await self._read_bounded(response)
            return httpx.Response(
                response.status_code,
                content=content_bytes,
            )
        except (ResponseTooLargeError, SafeRequestError):
            raise
        except Exception:
            raise SafeRequestError("outbound request failed") from None
        finally:
            try:
                await response.aclose()
            except Exception:
                pass

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        # MockTransport handlers commonly return an already-materialized
        # response. HTTPX has decoded such content before the client receives
        # it, so this compatibility branch is restricted to the private test
        # seam. Production responses must always be consumed from raw chunks.
        if response.is_stream_consumed:
            if self._uses_pinned_transport:
                raise SafeRequestError("invalid upstream response stream")
            content = response.content
            if len(content) > self._max_response_bytes:
                raise ResponseTooLargeError("response exceeds limit")
            return content
        content_encoding = response.headers.get("content-encoding", "").strip().lower()
        if content_encoding in {"", "identity"}:
            decoder = None
        elif content_encoding == "gzip":
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif content_encoding == "deflate":
            decoder = zlib.decompressobj()
        else:
            raise SafeRequestError("unsupported upstream content encoding")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                raise SafeRequestError("invalid upstream response") from None
            if length < 0:
                raise SafeRequestError("invalid upstream response")
            if length > self._max_response_bytes:
                raise ResponseTooLargeError("response exceeds limit")
        chunks: list[bytes] = []
        compressed_total = 0
        decoded_total = 0
        async for raw_chunk in response.aiter_raw():
            compressed_total += len(raw_chunk)
            if compressed_total > self._max_response_bytes:
                raise ResponseTooLargeError("response exceeds limit")
            if decoder is None:
                decoded = raw_chunk
            else:
                remaining = self._max_response_bytes - decoded_total
                decoded = decoder.decompress(raw_chunk, remaining + 1)
                if decoder.unconsumed_tail:
                    raise ResponseTooLargeError("response exceeds limit")
            decoded_total += len(decoded)
            if decoded_total > self._max_response_bytes:
                raise ResponseTooLargeError("response exceeds limit")
            if decoded:
                chunks.append(decoded)
        if decoder is not None:
            remaining = self._max_response_bytes - decoded_total
            tail = decoder.flush(remaining + 1)
            decoded_total += len(tail)
            if decoded_total > self._max_response_bytes:
                raise ResponseTooLargeError("response exceeds limit")
            if not decoder.eof or decoder.unused_data:
                raise SafeRequestError("invalid upstream response")
            if tail:
                chunks.append(tail)
        return b"".join(chunks)
