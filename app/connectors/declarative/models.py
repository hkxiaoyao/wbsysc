"""Typed declarations for the deliberately small REST/OpenAPI surface.

All values in this module are data only.  In particular, mappings select
declared names and JSON pointers; they never contain code, expressions, or a
runtime-provided URL/method/header name.
"""
from __future__ import annotations

import json
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit


ALLOWED_AUTH_SCHEMES = frozenset({"api_key", "basic", "oauth2_client_credentials"})
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
ALLOWED_OPERATION_KINDS = frozenset({"read", "write"})
ALLOWED_MAPPING_LOCATIONS = frozenset({"path", "query", "body", "header"})

MAX_DOCUMENT_BYTES = 256 * 1024
MAX_DOCUMENT_DEPTH = 24
MAX_PERSISTED_COLLECTION_ITEMS = 2_000
MAX_OPERATION_COUNT = 64
MAX_MAPPING_DEPTH = 8
MAX_INPUT_MAPPINGS = 64
MAX_OUTPUT_MAPPINGS = 32
MAX_POINTER_LENGTH = 512
MAX_REQUEST_BODY_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_OUTPUT_ITEMS = 1_000
MAX_OUTPUT_DEPTH = 8
MAX_PAGE_COUNT = 10
MAX_PAGE_LIMIT = 1_000
MAX_TIMEOUT_MS = 30_000
DEFAULT_TIMEOUT_MS = 10_000

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_BODY_TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_PROTECTED_DYNAMIC_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "proxy-authorization",
        "proxy-connection",
        "content-type",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
    }
)
_EXPRESSION_MARKERS = (
    "${",
    "{{",
    "}}",
    "{%",
    "%}",
    "<script",
    "javascript:",
    "__import__",
    "eval(",
    "exec(",
)


class SpecValidationError(ValueError):
    """A published declaration is outside the supported safe subset."""


class UnsafeTargetError(ValueError):
    """An outbound target failed the fixed SSRF guard."""


class UnknownToolError(LookupError):
    """A caller attempted an operation absent from the published revision."""


class ResponseTooLargeError(ValueError):
    """An upstream response exceeded the fixed bounded-read limit."""


class RequestTooLargeError(ValueError):
    """A generated JSON request body exceeded the fixed size limit."""


class SafeRequestError(RuntimeError):
    """An upstream request failed without exposing upstream details."""


class OutputSelectionError(ValueError):
    """The published selection cannot safely represent an upstream value."""


def _identifier(label: str, value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise SpecValidationError(f"invalid {label}")
    return value


def _pointer_tokens(pointer: str) -> tuple[str, ...]:
    if not isinstance(pointer, str) or not pointer or len(pointer) > MAX_POINTER_LENGTH:
        raise SpecValidationError("invalid output pointer")
    if not pointer.startswith("/"):
        raise SpecValidationError("invalid output pointer")
    tokens = tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/"))
    if len(tokens) > MAX_MAPPING_DEPTH or any(not token for token in tokens):
        raise SpecValidationError("invalid output pointer")
    return tokens


def _frozen_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise SpecValidationError("mapping must be an object")
    return MappingProxyType(dict(value))


def _json_size(value: Any) -> int:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise SpecValidationError("request body must be JSON") from None
    return len(encoded.encode("utf-8"))


def _assert_bounded_json_value(value: Any, *, depth: int = 0) -> None:
    """Reject malformed or unexpectedly deep persisted JSON before rebuilding.

    ``storage_document`` only writes ordinary JSON primitives, lists, and
    objects.  Rechecking that boundary when data comes back from the database
    keeps a corrupt row from turning into a recursive object graph or a value
    that later trips a serializer in an error path.
    """
    if depth > MAX_DOCUMENT_DEPTH:
        raise SpecValidationError("persisted declaration exceeds nesting limit")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise SpecValidationError("persisted declaration is not JSON")
        return
    if isinstance(value, list):
        if len(value) > MAX_PERSISTED_COLLECTION_ITEMS:
            raise SpecValidationError("persisted declaration exceeds limits")
        for item in value:
            _assert_bounded_json_value(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_PERSISTED_COLLECTION_ITEMS:
            raise SpecValidationError("persisted declaration exceeds limits")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise SpecValidationError("persisted declaration is not JSON")
            _assert_bounded_json_value(item, depth=depth + 1)
        return
    raise SpecValidationError("persisted declaration is not JSON")


def assert_safe_declaration_value(
    value: Any,
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    """Apply one bounded, expression-free policy to every declaration path."""
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > 20_000 or depth > MAX_DOCUMENT_DEPTH:
        raise SpecValidationError("specification mapping exceeds limit")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise SpecValidationError("specification contains unsupported values")
        return
    if isinstance(value, str):
        try:
            if len(value.encode("utf-8")) > 16_384:
                raise SpecValidationError("specification string exceeds limit")
        except UnicodeError:
            raise SpecValidationError("specification contains unsupported values") from None
        normalized = value.lower()
        if any(marker in normalized for marker in _EXPRESSION_MARKERS):
            raise SpecValidationError("expressions are not supported")
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_PERSISTED_COLLECTION_ITEMS:
            raise SpecValidationError("specification mapping exceeds limit")
        for key, child in value.items():
            if not isinstance(key, str):
                raise SpecValidationError("specification keys must be strings")
            if key == "$ref":
                raise SpecValidationError("references are not supported")
            assert_safe_declaration_value(key, depth=depth + 1, counter=counter)
            assert_safe_declaration_value(child, depth=depth + 1, counter=counter)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_PERSISTED_COLLECTION_ITEMS:
            raise SpecValidationError("specification list exceeds limit")
        for child in value:
            assert_safe_declaration_value(child, depth=depth + 1, counter=counter)
        return
    raise SpecValidationError("specification contains unsupported values")


def _stored_object(
    value: Any,
    *,
    required: frozenset[str],
) -> Mapping[str, Any]:
    """Return a persisted object only if it has the exact expected shape."""
    if not isinstance(value, Mapping) or set(value) != required:
        raise SpecValidationError("invalid persisted declarative revision")
    if any(not isinstance(key, str) for key in value):
        raise SpecValidationError("invalid persisted declarative revision")
    return value


def _normalize_declaration_host(value: Any, *, allow_wildcard: bool) -> str:
    """Normalize a hostname stored in a declaration, never an IP literal."""
    if not isinstance(value, str) or not value or len(value) > 253:
        raise SpecValidationError("invalid declarative hostname")
    if "*" in value:
        raise SpecValidationError("wildcard declarative hosts are not supported")
    host = value
    if (
        not host
        or "*" in host
        or any(character.isspace() or character in "/:@?#\\" for character in host)
    ):
        raise SpecValidationError("invalid declarative hostname")
    try:
        normalized = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise SpecValidationError("invalid declarative hostname") from None
    if not normalized or len(normalized) > 253:
        raise SpecValidationError("invalid declarative hostname")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise SpecValidationError("declarative IP literals are not supported")
    if (
        normalized == "localhost"
        or normalized.endswith(".localhost")
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(character.isalnum() or character == "-" for character in label)
            for label in normalized.split(".")
        )
    ):
        raise SpecValidationError("invalid declarative hostname")
    return normalized


def _declaration_base_host(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise SpecValidationError("invalid revision base URL")
    if any(character.isspace() or character == "\\" for character in value):
        raise SpecValidationError("invalid revision base URL")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise SpecValidationError("invalid revision base URL") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or port not in (None, 443)
    ):
        raise SpecValidationError("invalid revision base URL")
    path = parts.path.rstrip("/")
    if (
        "//" in path
        or "/../" in path
        or path.endswith("/..")
        or "/./" in path
        or path.endswith("/.")
        or "%" in path
    ):
        raise SpecValidationError("invalid revision base URL")
    return _normalize_declaration_host(parts.hostname, allow_wildcard=False)


def _host_is_declared(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    return host in allowed_hosts


def _assert_scalar(value: Any) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (str, int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise SpecValidationError("input must be finite JSON")
        return
    raise SpecValidationError("mapped path, query, and header inputs must be scalar")


@dataclass(frozen=True)
class InputMapping:
    """One caller input mapped to a fixed declared request location."""

    arg_name: str
    location: Literal["path", "query", "body", "header"]
    target: str
    required: bool = False
    schema: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier("input name", self.arg_name)
        if self.location not in ALLOWED_MAPPING_LOCATIONS:
            raise SpecValidationError("unsupported input mapping location")
        if not isinstance(self.target, str) or not self.target or len(self.target) > 128:
            raise SpecValidationError("invalid input mapping target")
        if self.location == "header" and not _HEADER_RE.fullmatch(self.target):
            raise SpecValidationError("invalid mapped header")
        if self.location == "header" and self.target.lower() in _PROTECTED_DYNAMIC_HEADERS:
            raise SpecValidationError("protected headers must use typed authentication")
        if self.location == "body" and not _BODY_TARGET_RE.fullmatch(self.target):
            raise SpecValidationError("invalid body mapping target")
        if self.location in {"path", "query"} and not _IDENTIFIER_RE.fullmatch(self.target):
            raise SpecValidationError("invalid input mapping target")
        if not isinstance(self.required, bool):
            raise SpecValidationError("input required must be a bool")
        schema = _frozen_mapping(self.schema)
        assert_safe_declaration_value(schema)
        object.__setattr__(self, "schema", schema)


@dataclass(frozen=True)
class OutputMapping:
    """A named result field selected by a pre-published JSON Pointer."""

    name: str
    pointer: str

    def __post_init__(self) -> None:
        _identifier("output name", self.name)
        _pointer_tokens(self.pointer)


@dataclass(frozen=True)
class PaginationPolicy:
    """Bounds for an explicitly declared pagination protocol.

    The first release does not follow arbitrary next links.  A later page is
    only possible when its cursor name and source pointer are predeclared.
    """

    max_pages: int = 1
    max_items: int = MAX_PAGE_LIMIT
    items_pointer: str = ""
    next_pointer: str = ""
    next_query_param: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_pages, bool)
            or not isinstance(self.max_pages, int)
            or not 1 <= self.max_pages <= MAX_PAGE_COUNT
        ):
            raise SpecValidationError("invalid pagination page limit")
        if (
            isinstance(self.max_items, bool)
            or not isinstance(self.max_items, int)
            or not 1 <= self.max_items <= MAX_PAGE_LIMIT
        ):
            raise SpecValidationError("invalid pagination item limit")
        if self.max_pages > 1:
            _pointer_tokens(self.items_pointer)
            _pointer_tokens(self.next_pointer)
            if not _IDENTIFIER_RE.fullmatch(self.next_query_param):
                raise SpecValidationError("invalid pagination cursor parameter")
        elif self.items_pointer or self.next_pointer or self.next_query_param:
            raise SpecValidationError("pagination cursor requires more than one page")


@dataclass(frozen=True)
class AuthScheme:
    """A typed, declared authentication scheme; never a free-form header."""

    kind: Literal["api_key", "basic", "oauth2_client_credentials"]
    credential_key: str = ""
    header_name: str = ""
    username_key: str = ""
    password_key: str = ""
    token_url: str = ""
    client_id_key: str = ""
    client_secret_key: str = ""
    access_token_key: str = "access_token"
    scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in ALLOWED_AUTH_SCHEMES:
            raise SpecValidationError("unsupported authentication scheme")
        if self.kind == "api_key":
            _identifier("credential key", self.credential_key)
            if not _HEADER_RE.fullmatch(self.header_name):
                raise SpecValidationError("invalid API key header")
        elif self.kind == "basic":
            _identifier("username credential key", self.username_key)
            _identifier("password credential key", self.password_key)
        else:
            if not isinstance(self.token_url, str) or not self.token_url:
                raise SpecValidationError("OAuth client credentials requires token URL")
            _identifier("client ID credential key", self.client_id_key)
            _identifier("client secret credential key", self.client_secret_key)
            _identifier("access token credential key", self.access_token_key)
            if not isinstance(self.scopes, tuple) or any(
                not isinstance(scope, str) or not scope or len(scope) > 128
                for scope in self.scopes
            ):
                raise SpecValidationError("invalid OAuth scope")


@dataclass(frozen=True)
class SyncSpec:
    """The minimum declaration necessary to permit persistent stored mode."""

    resource_key: str
    primary_key_pointer: str
    field_mappings: Mapping[str, str]
    operation_key: str = ""

    def __post_init__(self) -> None:
        _identifier("resource key", self.resource_key)
        _pointer_tokens(self.primary_key_pointer)
        mappings = _frozen_mapping(self.field_mappings)
        if not mappings or len(mappings) > MAX_OUTPUT_MAPPINGS:
            raise SpecValidationError("stored mode requires mapped fields")
        for field_name, pointer in mappings.items():
            _identifier("sync field", field_name)
            _pointer_tokens(pointer)
        _identifier("sync operation key", self.operation_key)
        object.__setattr__(self, "field_mappings", mappings)


def _validate_input_value(value: Any, schema: Mapping[str, Any]) -> None:
    """Enforce only a small JSON-schema subset needed by declared mappings."""
    if not schema:
        return
    schema_type = schema.get("type")
    if schema_type == "string":
        if not isinstance(value, str):
            raise SpecValidationError("input type does not match declaration")
        limit = schema.get("maxLength")
        if isinstance(limit, int) and not isinstance(limit, bool) and len(value) > limit:
            raise SpecValidationError("input exceeds declared length")
    elif schema_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise SpecValidationError("input type does not match declaration")
    elif schema_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise SpecValidationError("input type does not match declaration")
    elif schema_type == "boolean":
        if not isinstance(value, bool):
            raise SpecValidationError("input type does not match declaration")
    elif schema_type == "object":
        if not isinstance(value, Mapping):
            raise SpecValidationError("input type does not match declaration")
    elif schema_type == "array":
        if not isinstance(value, list):
            raise SpecValidationError("input type does not match declaration")
    elif schema_type not in (None, "null"):
        raise SpecValidationError("unsupported input schema type")

    if schema_type == "string":
        limit = schema.get("maxLength")
        if isinstance(limit, int) and not isinstance(limit, bool) and len(value) > limit:
            raise SpecValidationError("input exceeds declared length")
    if schema_type in {"integer", "number"}:
        for key in ("minimum", "maximum"):
            bound = schema.get(key)
            if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                if key == "minimum" and value < bound:
                    raise SpecValidationError("input is below declared minimum")
                if key == "maximum" and value > bound:
                    raise SpecValidationError("input exceeds declared maximum")
    if "enum" in schema and value not in schema["enum"]:
        raise SpecValidationError("input is outside the declared enum")
    if schema_type == "array":
        if len(value) > MAX_OUTPUT_ITEMS:
            raise SpecValidationError("input array exceeds limits")
        item_schema = schema.get("items")
        if not isinstance(item_schema, Mapping):
            raise SpecValidationError("invalid declared input schema")
        for item in value:
            _validate_input_value(item, item_schema)
    if schema_type == "object":
        properties = schema.get("properties")
        required = schema.get("required", ())
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            raise SpecValidationError("invalid declared input schema")
        if any(not isinstance(key, str) or key not in properties for key in value):
            raise SpecValidationError("undeclared object input")
        if any(name not in value for name in required):
            raise SpecValidationError("required object input is missing")
        for key, item in value.items():
            child_schema = properties[key]
            if not isinstance(child_schema, Mapping):
                raise SpecValidationError("invalid declared input schema")
            _validate_input_value(item, child_schema)


def _selected_copy(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_OUTPUT_DEPTH:
        raise OutputSelectionError("selected output exceeds limits")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise OutputSelectionError("selected output is not JSON")
        return value
    if isinstance(value, list):
        if len(value) > MAX_OUTPUT_ITEMS:
            raise OutputSelectionError("selected output exceeds limits")
        return [_selected_copy(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > MAX_OUTPUT_ITEMS:
            raise OutputSelectionError("selected output exceeds limits")
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise OutputSelectionError("selected output is not JSON")
            safe[key] = _selected_copy(item, depth=depth + 1)
        return safe
    raise OutputSelectionError("selected output is not JSON")


def _read_pointer(document: Any, pointer: str) -> Any:
    value = document
    for token in _pointer_tokens(pointer):
        if isinstance(value, Mapping):
            if token not in value:
                raise OutputSelectionError("selected output is absent")
            value = value[token]
        elif isinstance(value, list):
            if not token.isdigit():
                raise OutputSelectionError("selected output is absent")
            index = int(token)
            if index >= len(value):
                raise OutputSelectionError("selected output is absent")
            value = value[index]
        else:
            raise OutputSelectionError("selected output is absent")
    return value


@dataclass(frozen=True)
class DeclarativeOperation:
    """A prevalidated operation with no caller-controlled transport fields."""

    tool_key: str
    mcp_name: str
    description: str
    method: str
    path: str
    input_mappings: tuple[InputMapping, ...]
    output_mappings: tuple[OutputMapping, ...]
    operation_kind: Literal["read", "write"]
    explicit_write_enabled: bool = False
    base_url: str = ""
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    cache_ttl_seconds: int | None = None
    pagination: PaginationPolicy | None = None

    def __post_init__(self) -> None:
        _identifier("tool key", self.tool_key)
        _identifier("MCP name", self.mcp_name)
        if not isinstance(self.description, str) or len(self.description) > 512:
            raise SpecValidationError("invalid operation description")
        if self.method not in ALLOWED_METHODS:
            raise SpecValidationError("unsupported HTTP method")
        if self.operation_kind not in ALLOWED_OPERATION_KINDS:
            raise SpecValidationError("invalid operation kind")
        if not isinstance(self.explicit_write_enabled, bool):
            raise SpecValidationError("write enablement must be a bool")
        if self.operation_kind == "write" and not self.explicit_write_enabled:
            raise SpecValidationError("write operation requires explicit enablement")
        if self.operation_kind == "read" and self.method != "GET":
            raise SpecValidationError("non-GET operations must be explicit writes")
        if not isinstance(self.path, str) or not self.path.startswith("/") or len(self.path) > 512:
            raise SpecValidationError("invalid operation path")
        if (
            "//" in self.path
            or "/../" in self.path
            or self.path.endswith("/..")
            or "/./" in self.path
            or self.path.endswith("/.")
            or "?" in self.path
            or "#" in self.path
            or "\\" in self.path
            or "%" in self.path
        ):
            raise SpecValidationError("invalid operation path")
        if not isinstance(self.base_url, str) or not self.base_url.startswith("https://"):
            raise SpecValidationError("invalid operation base URL")
        inputs = tuple(self.input_mappings)
        outputs = tuple(self.output_mappings)
        if len(inputs) > MAX_INPUT_MAPPINGS or len(outputs) > MAX_OUTPUT_MAPPINGS:
            raise SpecValidationError("too many declared mappings")
        if len({mapping.arg_name for mapping in inputs}) != len(inputs):
            raise SpecValidationError("duplicate input mapping")
        if len({(mapping.location, mapping.target) for mapping in inputs}) != len(inputs):
            raise SpecValidationError("duplicate request target mapping")
        if len({mapping.name for mapping in outputs}) != len(outputs):
            raise SpecValidationError("duplicate output mapping")
        if not outputs:
            raise SpecValidationError("operation requires an output selection")
        if not isinstance(self.timeout_ms, int) or isinstance(self.timeout_ms, bool) or not 1 <= self.timeout_ms <= MAX_TIMEOUT_MS:
            raise SpecValidationError("invalid operation timeout")
        if self.cache_ttl_seconds is not None and (
            not isinstance(self.cache_ttl_seconds, int)
            or isinstance(self.cache_ttl_seconds, bool)
            or not 0 <= self.cache_ttl_seconds <= 86_400
        ):
            raise SpecValidationError("invalid cache TTL")
        if self.pagination is not None:
            raise SpecValidationError("pagination is not supported")
        if any(not isinstance(mapping, InputMapping) for mapping in inputs) or any(
            not isinstance(mapping, OutputMapping) for mapping in outputs
        ):
            raise SpecValidationError("invalid operation mapping")
        object.__setattr__(self, "input_mappings", inputs)
        object.__setattr__(self, "output_mappings", outputs)

    @property
    def input_schema(self) -> dict[str, Any]:
        properties = {mapping.arg_name: dict(mapping.schema) for mapping in self.input_mappings}
        required = [mapping.arg_name for mapping in self.input_mappings if mapping.required]
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        return schema

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {mapping.name: {} for mapping in self.output_mappings},
            "additionalProperties": False,
        }

    def build_request(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(args, Mapping):
            raise SpecValidationError("tool arguments must be an object")
        declared = {mapping.arg_name for mapping in self.input_mappings}
        if any(not isinstance(key, str) or key not in declared for key in args):
            raise SpecValidationError("undeclared input")

        path = self.path
        query: list[tuple[str, str]] = []
        body: dict[str, Any] = {}
        for mapping in self.input_mappings:
            if mapping.arg_name not in args:
                if mapping.required:
                    raise SpecValidationError("required input is missing")
                continue
            value = args[mapping.arg_name]
            _validate_input_value(value, mapping.schema)
            if mapping.location == "path":
                _assert_scalar(value)
                placeholder = "{" + mapping.target + "}"
                if placeholder not in path:
                    raise SpecValidationError("path mapping is not declared in path")
                path = path.replace(placeholder, quote(str(value), safe=""))
            elif mapping.location == "query":
                _assert_scalar(value)
                query.append((mapping.target, str(value).lower() if isinstance(value, bool) else str(value)))
            elif mapping.location == "body":
                body[mapping.target] = value
            else:
                # Headers are deliberately not passed in this return value.
                # The connector creates them from fixed declaration mappings.
                _assert_scalar(value)

        if "{" in path or "}" in path:
            raise SpecValidationError("unmapped path parameter")
        url = self.base_url.rstrip("/") + path
        if query:
            url += "?" + urlencode(query, doseq=False, safe="")
        json_body: object | None = body or None
        if json_body is not None and _json_size(json_body) > MAX_REQUEST_BODY_BYTES:
            raise RequestTooLargeError("request body exceeds limit")
        return {"method": self.method, "url": url, "json_body": json_body}

    def declared_headers(self, args: Mapping[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        for mapping in self.input_mappings:
            if mapping.location != "header" or mapping.arg_name not in args:
                continue
            value = args[mapping.arg_name]
            _validate_input_value(value, mapping.schema)
            _assert_scalar(value)
            headers[mapping.target] = str(value)
        return headers

    def extract_safe_output(self, document: Any) -> dict[str, Any]:
        selected: dict[str, Any] = {}
        for mapping in self.output_mappings:
            selected[mapping.name] = _selected_copy(_read_pointer(document, mapping.pointer))
        if _json_size(selected) > MAX_OUTPUT_BYTES:
            raise OutputSelectionError("selected output exceeds limits")
        return selected


@dataclass(frozen=True)
class DeclarativeRevision:
    """One immutable, published set of operations for one connection."""

    spec_id: str = ""
    revision: int = 1
    tenant_id: str = ""
    connection_id: str = ""
    status: Literal["draft", "published"] = "draft"
    base_url: str = ""
    allowed_hosts: tuple[str, ...] = ()
    operations: tuple[DeclarativeOperation, ...] = ()
    auth_scheme: AuthScheme | None = None
    sync_spec: SyncSpec | None = None

    def __post_init__(self) -> None:
        if self.spec_id:
            _identifier("spec ID", self.spec_id)
        if self.tenant_id:
            _identifier("tenant ID", self.tenant_id)
        if self.connection_id:
            _identifier("connection ID", self.connection_id)
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise SpecValidationError("invalid revision number")
        if self.status not in {"draft", "published"}:
            raise SpecValidationError("invalid revision status")
        base_host = _declaration_base_host(self.base_url)
        hosts = tuple(
            _normalize_declaration_host(host, allow_wildcard=True)
            for host in self.allowed_hosts
        )
        if not hosts or len(set(hosts)) != len(hosts):
            raise SpecValidationError("revision requires allowed hosts")
        if not _host_is_declared(base_host, hosts):
            raise SpecValidationError("revision base URL is absent from allowed hosts")
        operations = tuple(self.operations)
        if not operations or len(operations) > MAX_OPERATION_COUNT:
            raise SpecValidationError("revision requires declared operations")
        identifiers: set[str] = set()
        for operation in operations:
            if not isinstance(operation, DeclarativeOperation):
                raise SpecValidationError("invalid declarative operation")
            if operation.base_url != self.base_url:
                raise SpecValidationError("operation base URL does not match revision")
            for identifier in {operation.tool_key, operation.mcp_name}:
                if identifier in identifiers:
                    raise SpecValidationError("duplicate operation identifier")
                identifiers.add(identifier)
        if self.auth_scheme is not None and not isinstance(self.auth_scheme, AuthScheme):
            raise SpecValidationError("invalid declarative authentication")
        if self.auth_scheme is not None and self.auth_scheme.kind == "oauth2_client_credentials":
            token_host = _declaration_base_host(self.auth_scheme.token_url)
            if not _host_is_declared(token_host, hosts):
                raise SpecValidationError("OAuth token URL is absent from allowed hosts")
        if self.sync_spec is not None:
            if not isinstance(self.sync_spec, SyncSpec):
                raise SpecValidationError("invalid sync specification")
            operation_key = self.sync_spec.operation_key
            sync_operation = next(
                (operation for operation in operations if operation.tool_key == operation_key),
                None,
            )
            if sync_operation is None:
                raise SpecValidationError("sync operation is not declared")
            if sync_operation.operation_kind != "read":
                raise SpecValidationError("sync operation must be a declared read")
            declared_pointers = {
                mapping.pointer for mapping in sync_operation.output_mappings
            }
            if self.sync_spec.primary_key_pointer not in declared_pointers:
                raise SpecValidationError("sync primary key is not declared")
            if any(
                pointer not in declared_pointers
                for pointer in self.sync_spec.field_mappings.values()
            ):
                raise SpecValidationError("sync field mapping is not declared")
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "operations", operations)

    def operation_for(self, tool_key: str) -> DeclarativeOperation:
        for operation in self.operations:
            if tool_key in {operation.tool_key, operation.mcp_name}:
                return operation
        raise UnknownToolError("unknown declarative tool")

    def assert_data_mode_allowed(self, data_mode: str) -> None:
        if data_mode not in {"direct", "hybrid", "stored"}:
            raise SpecValidationError("unsupported data mode")
        if data_mode == "stored" and self.sync_spec is None:
            raise SpecValidationError("stored mode requires a validated sync spec")

    def connector_spec(self):
        """Build the common runtime manifest without importing it at module load."""
        from app.connectors.contracts import ConnectorSpec, ToolSpec

        tools = tuple(
            ToolSpec(
                tool_key=operation.tool_key,
                mcp_name=operation.mcp_name,
                description=operation.description,
                input_schema=operation.input_schema,
                output_schema=operation.output_schema,
                operation_kind=operation.operation_kind,
                default_timeout_ms=operation.timeout_ms,
                cache_ttl_seconds=operation.cache_ttl_seconds,
            )
            for operation in self.operations
        )
        credential_schema: dict[str, Any] = {"type": "object", "properties": {}}
        if self.auth_scheme is not None:
            properties = credential_schema["properties"]
            if self.auth_scheme.kind == "api_key":
                properties[self.auth_scheme.credential_key] = {"type": "string", "writeOnly": True}
            elif self.auth_scheme.kind == "basic":
                properties[self.auth_scheme.username_key] = {"type": "string", "writeOnly": True}
                properties[self.auth_scheme.password_key] = {"type": "string", "writeOnly": True}
            else:
                properties[self.auth_scheme.client_id_key] = {"type": "string", "writeOnly": True}
                properties[self.auth_scheme.client_secret_key] = {"type": "string", "writeOnly": True}
        return ConnectorSpec(
            connector_key="http_declarative",
            tools=tools,
            supports_sync=self.sync_spec is not None,
            version=str(self.revision),
            config_schema={"type": "object", "additionalProperties": False},
            credential_schema=credential_schema,
            supports_data_modes=("direct", "hybrid", "stored") if self.sync_spec else ("direct", "hybrid"),
        )

    def storage_document(self) -> dict[str, Any]:
        """Return the JSON-only, credential-free document persisted for a revision."""
        def input_mapping(mapping: InputMapping) -> dict[str, Any]:
            return {
                "arg_name": mapping.arg_name,
                "location": mapping.location,
                "target": mapping.target,
                "required": mapping.required,
                "schema": dict(mapping.schema),
            }

        def operation_document(operation: DeclarativeOperation) -> dict[str, Any]:
            pagination = operation.pagination
            return {
                "tool_key": operation.tool_key,
                "mcp_name": operation.mcp_name,
                "description": operation.description,
                "method": operation.method,
                "path": operation.path,
                "input_mappings": [input_mapping(mapping) for mapping in operation.input_mappings],
                "output_mappings": [
                    {"name": mapping.name, "pointer": mapping.pointer}
                    for mapping in operation.output_mappings
                ],
                "operation_kind": operation.operation_kind,
                "explicit_write_enabled": operation.explicit_write_enabled,
                "timeout_ms": operation.timeout_ms,
                "cache_ttl_seconds": operation.cache_ttl_seconds,
                "pagination": (
                    None
                    if pagination is None
                    else {
                        "max_pages": pagination.max_pages,
                        "max_items": pagination.max_items,
                        "items_pointer": pagination.items_pointer,
                        "next_pointer": pagination.next_pointer,
                        "next_query_param": pagination.next_query_param,
                    }
                ),
            }

        auth_scheme: dict[str, Any] | None
        if self.auth_scheme is None:
            auth_scheme = None
        else:
            auth_scheme = {
                "kind": self.auth_scheme.kind,
                "credential_key": self.auth_scheme.credential_key,
                "header_name": self.auth_scheme.header_name,
                "username_key": self.auth_scheme.username_key,
                "password_key": self.auth_scheme.password_key,
                "token_url": self.auth_scheme.token_url,
                "client_id_key": self.auth_scheme.client_id_key,
                "client_secret_key": self.auth_scheme.client_secret_key,
                "access_token_key": self.auth_scheme.access_token_key,
                "scopes": list(self.auth_scheme.scopes),
            }
        sync_spec = (
            None
            if self.sync_spec is None
            else {
                "resource_key": self.sync_spec.resource_key,
                "primary_key_pointer": self.sync_spec.primary_key_pointer,
                "field_mappings": dict(self.sync_spec.field_mappings),
                "operation_key": self.sync_spec.operation_key,
            }
        )
        return {
            "base_url": self.base_url,
            "allowed_hosts": list(self.allowed_hosts),
            "auth_scheme": auth_scheme,
            "sync_spec": sync_spec,
            "operations": [operation_document(operation) for operation in self.operations],
        }

    @classmethod
    def from_storage_document(
        cls,
        *,
        spec_id: str,
        revision: int,
        tenant_id: str,
        connection_id: str,
        status: str,
        document: Any,
    ) -> "DeclarativeRevision":
        """Rebuild a compiled revision from the credential-free DB document.

        This is intentionally not an OpenAPI importer: only the exact,
        already-compiled storage format is accepted.  Every nested object is
        reconstructed through the immutable model constructors so persisted
        declarations receive the same transport, mapping, write-gate, and
        stored-sync validation as newly imported ones.
        """
        _assert_bounded_json_value(document)
        assert_safe_declaration_value(document)
        if _json_size(document) > MAX_DOCUMENT_BYTES:
            raise SpecValidationError("persisted declaration exceeds size limit")
        stored = _stored_object(
            document,
            required=frozenset(
                {"base_url", "allowed_hosts", "auth_scheme", "sync_spec", "operations"}
            ),
        )
        base_url = stored["base_url"]
        allowed_hosts = stored["allowed_hosts"]
        raw_operations = stored["operations"]
        if (
            not isinstance(base_url, str)
            or not isinstance(allowed_hosts, list)
            or not all(isinstance(host, str) for host in allowed_hosts)
            or not isinstance(raw_operations, list)
            or not raw_operations
            or len(raw_operations) > MAX_OPERATION_COUNT
        ):
            raise SpecValidationError("invalid persisted declarative revision")

        auth_scheme: AuthScheme | None
        raw_auth = stored["auth_scheme"]
        if raw_auth is None:
            auth_scheme = None
        else:
            auth = _stored_object(
                raw_auth,
                required=frozenset(
                    {
                        "kind",
                        "credential_key",
                        "header_name",
                        "username_key",
                        "password_key",
                        "token_url",
                        "client_id_key",
                        "client_secret_key",
                        "access_token_key",
                        "scopes",
                    }
                ),
            )
            raw_scopes = auth["scopes"]
            if not isinstance(raw_scopes, list):
                raise SpecValidationError("invalid persisted declarative revision")
            auth_scheme = AuthScheme(
                kind=auth["kind"],
                credential_key=auth["credential_key"],
                header_name=auth["header_name"],
                username_key=auth["username_key"],
                password_key=auth["password_key"],
                token_url=auth["token_url"],
                client_id_key=auth["client_id_key"],
                client_secret_key=auth["client_secret_key"],
                access_token_key=auth["access_token_key"],
                scopes=tuple(raw_scopes),
            )

        sync_spec: SyncSpec | None
        raw_sync = stored["sync_spec"]
        if raw_sync is None:
            sync_spec = None
        else:
            sync = _stored_object(
                raw_sync,
                required=frozenset(
                    {
                        "resource_key",
                        "primary_key_pointer",
                        "field_mappings",
                        "operation_key",
                    }
                ),
            )
            if not isinstance(sync["field_mappings"], Mapping):
                raise SpecValidationError("invalid persisted declarative revision")
            sync_spec = SyncSpec(
                resource_key=sync["resource_key"],
                primary_key_pointer=sync["primary_key_pointer"],
                field_mappings=sync["field_mappings"],
                operation_key=sync["operation_key"],
            )

        operations: list[DeclarativeOperation] = []
        for raw_operation in raw_operations:
            operation = _stored_object(
                raw_operation,
                required=frozenset(
                    {
                        "tool_key",
                        "mcp_name",
                        "description",
                        "method",
                        "path",
                        "input_mappings",
                        "output_mappings",
                        "operation_kind",
                        "explicit_write_enabled",
                        "timeout_ms",
                        "cache_ttl_seconds",
                        "pagination",
                    }
                ),
            )
            raw_inputs = operation["input_mappings"]
            raw_outputs = operation["output_mappings"]
            if (
                not isinstance(raw_inputs, list)
                or len(raw_inputs) > MAX_INPUT_MAPPINGS
                or not isinstance(raw_outputs, list)
                or len(raw_outputs) > MAX_OUTPUT_MAPPINGS
            ):
                raise SpecValidationError("invalid persisted declarative revision")
            input_mappings: list[InputMapping] = []
            for raw_input in raw_inputs:
                mapping = _stored_object(
                    raw_input,
                    required=frozenset({"arg_name", "location", "target", "required", "schema"}),
                )
                if not isinstance(mapping["schema"], Mapping):
                    raise SpecValidationError("invalid persisted declarative revision")
                input_mappings.append(
                    InputMapping(
                        arg_name=mapping["arg_name"],
                        location=mapping["location"],
                        target=mapping["target"],
                        required=mapping["required"],
                        schema=mapping["schema"],
                    )
                )
            output_mappings: list[OutputMapping] = []
            for raw_output in raw_outputs:
                mapping = _stored_object(
                    raw_output,
                    required=frozenset({"name", "pointer"}),
                )
                output_mappings.append(
                    OutputMapping(name=mapping["name"], pointer=mapping["pointer"])
                )
            pagination: PaginationPolicy | None
            raw_pagination = operation["pagination"]
            if raw_pagination is None:
                pagination = None
            else:
                pagination_values = _stored_object(
                    raw_pagination,
                    required=frozenset(
                        {
                            "max_pages",
                            "max_items",
                            "items_pointer",
                            "next_pointer",
                            "next_query_param",
                        }
                    ),
                )
                pagination = PaginationPolicy(
                    max_pages=pagination_values["max_pages"],
                    max_items=pagination_values["max_items"],
                    items_pointer=pagination_values["items_pointer"],
                    next_pointer=pagination_values["next_pointer"],
                    next_query_param=pagination_values["next_query_param"],
                )
            operations.append(
                DeclarativeOperation(
                    tool_key=operation["tool_key"],
                    mcp_name=operation["mcp_name"],
                    description=operation["description"],
                    method=operation["method"],
                    path=operation["path"],
                    input_mappings=tuple(input_mappings),
                    output_mappings=tuple(output_mappings),
                    operation_kind=operation["operation_kind"],
                    explicit_write_enabled=operation["explicit_write_enabled"],
                    base_url=base_url,
                    timeout_ms=operation["timeout_ms"],
                    cache_ttl_seconds=operation["cache_ttl_seconds"],
                    pagination=pagination,
                )
            )
        return cls(
            spec_id=spec_id,
            revision=revision,
            tenant_id=tenant_id,
            connection_id=connection_id,
            status=status,  # type: ignore[arg-type]
            base_url=base_url,
            allowed_hosts=tuple(allowed_hosts),
            operations=tuple(operations),
            auth_scheme=auth_scheme,
            sync_spec=sync_spec,
        )
