"""Bounded parsing and validation for the supported OpenAPI subset.

The importer intentionally accepts a much smaller surface than OpenAPI.  It
does not resolve references, load remote documents, interpret server
variables, evaluate templates, or make an import-time network request.
"""
from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import yaml
from yaml.tokens import AliasToken, AnchorToken, ScalarToken, TagToken

from .models import (
    ALLOWED_METHODS,
    DEFAULT_TIMEOUT_MS,
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_DEPTH,
    MAX_MAPPING_DEPTH,
    MAX_OPERATION_COUNT,
    MAX_PAGE_COUNT,
    MAX_PAGE_LIMIT,
    AuthScheme,
    DeclarativeOperation,
    DeclarativeRevision,
    InputMapping,
    OutputMapping,
    PaginationPolicy,
    SpecValidationError,
    SyncSpec,
)


_OPENAPI_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_UNSUPPORTED_METHODS = frozenset({"head", "options", "trace", "connect"})
_PROTECTED_HEADERS = frozenset(
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
_PATH_PARAMETER_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_.-]{0,127})\}")


def _raise_expression() -> None:
    raise SpecValidationError("expressions are not supported")


def _contains_expression(value: str) -> bool:
    normalized = value.lower()
    return any(marker in normalized for marker in _EXPRESSION_MARKERS)


def _scan_yaml(text: str) -> None:
    """Reject aliases, anchors, and explicit tags before safe_load allocates data."""
    try:
        for token in yaml.scan(text, Loader=yaml.SafeLoader):
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise SpecValidationError("YAML aliases and tags are not supported")
            if isinstance(token, ScalarToken) and token.value == "<<":
                raise SpecValidationError("YAML merge keys are not supported")
    except SpecValidationError:
        raise
    except yaml.YAMLError:
        raise SpecValidationError("invalid specification document") from None


def load_revision_document(document: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    """Parse a bounded JSON/YAML document only with ``yaml.safe_load``.

    JSON is a YAML subset, so a single safe loader avoids split parser
    behavior.  Mappings are accepted for programmatic callers but are copied
    only after the same structural guard used for parsed documents.
    """
    if isinstance(document, bytes):
        if len(document) > MAX_DOCUMENT_BYTES:
            raise SpecValidationError("specification document exceeds size limit")
        try:
            text = document.decode("utf-8")
        except UnicodeDecodeError:
            raise SpecValidationError("specification document must be UTF-8") from None
        _scan_yaml(text)
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError:
            raise SpecValidationError("invalid specification document") from None
    elif isinstance(document, str):
        if len(document.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise SpecValidationError("specification document exceeds size limit")
        _scan_yaml(document)
        try:
            value = yaml.safe_load(document)
        except yaml.YAMLError:
            raise SpecValidationError("invalid specification document") from None
    elif isinstance(document, Mapping):
        try:
            encoded = json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise SpecValidationError("specification document must be JSON-compatible") from None
        if len(encoded) > MAX_DOCUMENT_BYTES:
            raise SpecValidationError("specification document exceeds size limit")
        value = dict(document)
    else:
        raise SpecValidationError("specification document must be an object or text")

    _assert_safe_value(value)
    if not isinstance(value, dict):
        raise SpecValidationError("specification document must be an object")
    return value


def _assert_safe_value(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
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
        if len(value.encode("utf-8")) > 16_384:
            raise SpecValidationError("specification string exceeds limit")
        if _contains_expression(value):
            _raise_expression()
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise SpecValidationError("specification keys must be strings")
            if key == "$ref":
                raise SpecValidationError("references are not supported")
            if _contains_expression(key):
                _raise_expression()
            _assert_safe_value(child, depth=depth + 1, counter=counter)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 2_000:
            raise SpecValidationError("specification list exceeds limit")
        for child in value:
            _assert_safe_value(child, depth=depth + 1, counter=counter)
        return
    raise SpecValidationError("specification contains unsupported values")


def _string(value: Any, message: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SpecValidationError(message)
    return value


def _safe_base_url(value: Any) -> tuple[str, str]:
    url = _string(value, "OpenAPI server URL is required", maximum=512)
    if "{" in url or "}" in url:
        raise SpecValidationError("OpenAPI server variables are not supported")
    if any(character.isspace() or character == "\\" for character in url):
        raise SpecValidationError("OpenAPI server URL must be HTTPS")
    try:
        parts = urlsplit(url)
        host_value = parts.hostname
        port = parts.port
    except ValueError:
        raise SpecValidationError("OpenAPI server URL must be HTTPS") from None
    if (
        parts.scheme != "https"
        or not host_value
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise SpecValidationError("OpenAPI server URL must be HTTPS")
    try:
        host = host_value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise SpecValidationError("invalid OpenAPI server host") from None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SpecValidationError("OpenAPI server IP literals are not supported")
    if port not in (None, 443):
        raise SpecValidationError("OpenAPI server port is not supported")
    base_path = parts.path.rstrip("/")
    if (
        "/../" in base_path
        or base_path.endswith("/..")
        or "/./" in base_path
        or base_path.endswith("/.")
        or "//" in base_path
        or "\\" in base_path
        or "%" in base_path
    ):
        raise SpecValidationError("invalid OpenAPI server path")
    return f"https://{host}{base_path}", host


def _normalize_allowed_hosts(values: Iterable[Any], base_host: str) -> tuple[str, ...]:
    hosts: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 253:
            raise SpecValidationError("invalid allowed hostname")
        host = value.lower().rstrip(".")
        if host.startswith("*."):
            bare = host[2:]
            if not bare or "*" in bare or "/" in bare or ":" in bare:
                raise SpecValidationError("invalid allowed hostname")
        elif "*" in host or "/" in host or ":" in host:
            raise SpecValidationError("invalid allowed hostname")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise SpecValidationError("allowed IP literals are not supported")
        if host not in hosts:
            hosts.append(host)
    if not hosts:
        hosts = [base_host]
    if not any(
        base_host == host or (host.startswith("*.") and base_host.endswith("." + host[2:]))
        for host in hosts
    ):
        raise SpecValidationError("server host is absent from allowed hosts")
    return tuple(hosts)


def _simple_schema(
    value: Any,
    *,
    allow_object: bool = False,
    require_closed_object: bool = True,
    depth: int = 0,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecValidationError("parameter schema is required")
    if depth > MAX_MAPPING_DEPTH:
        raise SpecValidationError("schema exceeds mapping depth")
    allowed_keys = {
        "type",
        "maxLength",
        "minimum",
        "maximum",
        "enum",
        "items",
        "properties",
        "required",
        "additionalProperties",
    }
    if any(key not in allowed_keys for key in value):
        raise SpecValidationError("unsupported schema control")
    schema_type = value.get("type")
    if schema_type not in {"string", "integer", "number", "boolean", "array", "object"}:
        raise SpecValidationError("unsupported schema type")
    if schema_type in {"array", "object"} and not allow_object:
        raise SpecValidationError("complex parameter schemas are not supported")
    result: dict[str, Any] = {"type": schema_type}
    for key in ("maxLength", "minimum", "maximum", "enum"):
        if key in value:
            result[key] = value[key]
    if schema_type == "array":
        result["items"] = _simple_schema(
            value.get("items"),
            allow_object=False,
            require_closed_object=require_closed_object,
            depth=depth + 1,
        )
    if schema_type == "object":
        if require_closed_object and value.get("additionalProperties") is not False:
            raise SpecValidationError("object request schemas require closed properties")
        properties = value.get("properties")
        if not isinstance(properties, Mapping) or not properties:
            raise SpecValidationError("object request schemas require properties")
        if require_closed_object:
            result["additionalProperties"] = False
        result["properties"] = {
            _string(name, "invalid schema property", maximum=128): _simple_schema(
                child,
                allow_object=True,
                require_closed_object=require_closed_object,
                depth=depth + 1,
            )
            for name, child in properties.items()
        }
        required = value.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(name, str) or name not in result["properties"] for name in required
        ):
            raise SpecValidationError("invalid required schema property")
        result["required"] = list(required)
    return result


def _parameter_declarations(
    path_item: Mapping[str, Any], operation: Mapping[str, Any]
) -> dict[tuple[str, str], tuple[bool, dict[str, Any]]]:
    declarations: dict[tuple[str, str], tuple[bool, dict[str, Any]]] = {}
    for source in (path_item.get("parameters", []), operation.get("parameters", [])):
        if source is None:
            continue
        if not isinstance(source, list):
            raise SpecValidationError("operation parameters must be a list")
        for parameter in source:
            if not isinstance(parameter, Mapping):
                raise SpecValidationError("invalid operation parameter")
            location = parameter.get("in")
            name = parameter.get("name")
            if location not in {"path", "query", "header"}:
                raise SpecValidationError("unsupported parameter location")
            name = _string(name, "invalid parameter name", maximum=128)
            if location == "header" and name.lower() in _PROTECTED_HEADERS:
                raise SpecValidationError("protected headers must use typed authentication")
            required = parameter.get("required", False)
            if not isinstance(required, bool):
                raise SpecValidationError("parameter required must be a bool")
            if location == "path" and not required:
                raise SpecValidationError("path parameters must be required")
            declarations[(location, name)] = (required, _simple_schema(parameter.get("schema")))
    return declarations


def _body_declarations(operation: Mapping[str, Any]) -> dict[tuple[str, str], tuple[bool, dict[str, Any]]]:
    request_body = operation.get("requestBody")
    if request_body is None:
        return {}
    if not isinstance(request_body, Mapping):
        raise SpecValidationError("invalid request body")
    required_body = request_body.get("required", False)
    if not isinstance(required_body, bool):
        raise SpecValidationError("request body required must be a bool")
    content = request_body.get("content")
    if not isinstance(content, Mapping) or set(content) != {"application/json"}:
        raise SpecValidationError("only JSON request bodies are supported")
    media = content["application/json"]
    if not isinstance(media, Mapping):
        raise SpecValidationError("invalid JSON request body")
    schema = _simple_schema(media.get("schema"), allow_object=True)
    if schema.get("type") != "object":
        raise SpecValidationError("JSON request body must be an object")
    required = set(schema.get("required", ()))
    return {
        ("body", name): (name in required or required_body, property_schema)
        for name, property_schema in schema["properties"].items()
    }


def _mapping_config(value: Any) -> list[tuple[str, Mapping[str, Any]]]:
    if isinstance(value, list):
        items: list[tuple[str, Mapping[str, Any]]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise SpecValidationError("invalid input mapping")
            arg_name = item.get("arg_name", item.get("arg"))
            if not isinstance(arg_name, str):
                raise SpecValidationError("invalid input mapping name")
            items.append((arg_name, item))
        return items
    if isinstance(value, Mapping):
        return [
            (_string(arg_name, "invalid input mapping name", maximum=128), item)
            for arg_name, item in value.items()
            if isinstance(item, Mapping)
        ]
    raise SpecValidationError("input mappings must be an object or list")


def _input_mappings(
    operation: Mapping[str, Any],
    path_item: Mapping[str, Any],
    path: str,
) -> tuple[InputMapping, ...]:
    declarations = _parameter_declarations(path_item, operation)
    declarations.update(_body_declarations(operation))
    raw_mappings = operation.get("x-input-mappings")
    if raw_mappings is None:
        mappings = [
            InputMapping(
                arg_name=name,
                location=location,  # type: ignore[arg-type]
                target=name,
                required=required,
                schema=schema,
            )
            for (location, name), (required, schema) in declarations.items()
        ]
    else:
        mappings = []
        for arg_name, config in _mapping_config(raw_mappings):
            location = config.get("location", config.get("in"))
            target = config.get("target", config.get("name"))
            if not isinstance(location, str) or not isinstance(target, str):
                raise SpecValidationError("invalid input mapping")
            declared = declarations.get((location, target))
            if declared is None:
                raise SpecValidationError("input mapping target is not declared")
            required, schema = declared
            if "required" in config:
                if config["required"] is not required:
                    raise SpecValidationError("input mapping cannot weaken declaration")
            mappings.append(
                InputMapping(
                    arg_name=arg_name,
                    location=location,  # type: ignore[arg-type]
                    target=target,
                    required=required,
                    schema=schema,
                )
            )
        declared_targets = {(mapping.location, mapping.target) for mapping in mappings}
        if declared_targets != set(declarations):
            raise SpecValidationError("all declared inputs require a fixed mapping")

    placeholders = set(_PATH_PARAMETER_RE.findall(path))
    path_targets = {mapping.target for mapping in mappings if mapping.location == "path"}
    if placeholders != path_targets or "{" in _PATH_PARAMETER_RE.sub("", path):
        raise SpecValidationError("path parameter mappings do not match path")
    return tuple(mappings)


def _response_schema(operation: Mapping[str, Any]) -> dict[str, Any]:
    responses = operation.get("responses")
    if not isinstance(responses, Mapping):
        raise SpecValidationError("operation requires JSON response declaration")
    response = next(
        (responses[status] for status in ("200", "201", "202", "default") if status in responses),
        None,
    )
    if not isinstance(response, Mapping):
        raise SpecValidationError("operation requires successful JSON response")
    content = response.get("content")
    if not isinstance(content, Mapping) or "application/json" not in content:
        raise SpecValidationError("operation requires JSON response declaration")
    media = content["application/json"]
    if not isinstance(media, Mapping):
        raise SpecValidationError("invalid JSON response declaration")
    return _simple_schema(
        media.get("schema"),
        allow_object=True,
        require_closed_object=False,
    )


def _schema_has_pointer(schema: Mapping[str, Any], pointer: str) -> bool:
    if not pointer.startswith("/"):
        return False
    current: Mapping[str, Any] = schema
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if current.get("type") == "object":
            properties = current.get("properties")
            if not isinstance(properties, Mapping) or token not in properties:
                return False
            child = properties[token]
            if not isinstance(child, Mapping):
                return False
            current = child
        elif current.get("type") == "array" and token.isdigit():
            child = current.get("items")
            if not isinstance(child, Mapping):
                return False
            current = child
        else:
            return False
    return True


def _output_mappings(operation: Mapping[str, Any]) -> tuple[OutputMapping, ...]:
    schema = _response_schema(operation)
    raw = operation.get("x-output-mappings", operation.get("x-output-mapping"))
    if raw is None:
        raw = operation.get("x-output-pointers")
    mappings: list[OutputMapping] = []
    if raw is None:
        if schema.get("type") != "object" or not isinstance(schema.get("properties"), Mapping):
            raise SpecValidationError("operation requires explicit output mappings")
        mappings = [OutputMapping(name=name, pointer="/" + name.replace("~", "~0").replace("/", "~1")) for name in schema["properties"]]
    elif isinstance(raw, Mapping):
        for name, pointer in raw.items():
            mappings.append(OutputMapping(name=_string(name, "invalid output mapping name", maximum=128), pointer=pointer))
    elif isinstance(raw, list):
        for pointer in raw:
            if not isinstance(pointer, str) or not pointer.startswith("/"):
                raise SpecValidationError("invalid output pointer")
            name = pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
            mappings.append(OutputMapping(name=name, pointer=pointer))
    else:
        raise SpecValidationError("invalid output mappings")
    if not mappings or any(not _schema_has_pointer(schema, mapping.pointer) for mapping in mappings):
        raise SpecValidationError("output mapping is not declared by response schema")
    return tuple(mappings)


def _pagination(operation: Mapping[str, Any], input_mappings: tuple[InputMapping, ...]) -> PaginationPolicy | None:
    raw = operation.get("x-pagination")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SpecValidationError("invalid pagination policy")
    max_pages = raw.get("max_pages", 1)
    max_items = raw.get("max_items", MAX_PAGE_LIMIT)
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= MAX_PAGE_COUNT:
        raise SpecValidationError("invalid pagination page limit")
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= MAX_PAGE_LIMIT:
        raise SpecValidationError("invalid pagination item limit")
    if max_pages == 1:
        return PaginationPolicy(max_pages=1, max_items=max_items)
    items_pointer = raw.get("items_pointer")
    next_pointer = raw.get("next_pointer")
    next_query_param = raw.get("next_query_param")
    if not all(isinstance(value, str) for value in (items_pointer, next_pointer, next_query_param)):
        raise SpecValidationError("pagination cursor is required")
    query_targets = {mapping.target for mapping in input_mappings if mapping.location == "query"}
    if next_query_param not in query_targets:
        raise SpecValidationError("pagination cursor must use a declared query parameter")
    return PaginationPolicy(
        max_pages=max_pages,
        max_items=max_items,
        items_pointer=items_pointer,
        next_pointer=next_pointer,
        next_query_param=next_query_param,
    )


def _operation_auth(document: Mapping[str, Any], operation: Mapping[str, Any]) -> AuthScheme | None:
    security = operation.get("security", document.get("security"))
    if security is None or security == []:
        return None
    if not isinstance(security, list) or len(security) != 1 or not isinstance(security[0], Mapping) or len(security[0]) != 1:
        raise SpecValidationError("operation requires one declared authentication scheme")
    scheme_name = next(iter(security[0]))
    if not isinstance(scheme_name, str):
        raise SpecValidationError("invalid authentication scheme")
    components = document.get("components", {})
    schemes = components.get("securitySchemes", {}) if isinstance(components, Mapping) else {}
    if not isinstance(schemes, Mapping) or not isinstance(schemes.get(scheme_name), Mapping):
        raise SpecValidationError("declared authentication scheme is absent")
    scheme = schemes[scheme_name]
    scheme_type = scheme.get("type")
    if scheme_type == "apiKey":
        if scheme.get("in") != "header":
            raise SpecValidationError("API keys must use a declared header")
        return AuthScheme(
            kind="api_key",
            credential_key=scheme.get("x-credential-key", scheme_name),
            header_name=scheme.get("name"),
        )
    if scheme_type == "http" and scheme.get("scheme") == "basic":
        return AuthScheme(
            kind="basic",
            username_key=scheme.get("x-username-credential-key", f"{scheme_name}_username"),
            password_key=scheme.get("x-password-credential-key", f"{scheme_name}_password"),
        )
    if scheme_type == "oauth2":
        flows = scheme.get("flows")
        flow = flows.get("clientCredentials") if isinstance(flows, Mapping) else None
        if not isinstance(flow, Mapping):
            raise SpecValidationError("only OAuth client credentials is supported")
        token_url, _ = _safe_base_url(flow.get("tokenUrl"))
        scopes = flow.get("scopes", {})
        if not isinstance(scopes, Mapping):
            raise SpecValidationError("invalid OAuth scopes")
        return AuthScheme(
            kind="oauth2_client_credentials",
            token_url=token_url,
            client_id_key=scheme.get("x-client-id-credential-key", f"{scheme_name}_client_id"),
            client_secret_key=scheme.get("x-client-secret-credential-key", f"{scheme_name}_client_secret"),
            access_token_key=scheme.get("x-access-token-field", "access_token"),
            scopes=tuple(scopes),
        )
    raise SpecValidationError("unsupported authentication scheme")


def _sync_spec(value: Any) -> SyncSpec | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SpecValidationError("invalid sync specification")
    return SyncSpec(
        resource_key=value.get("resource_key"),
        primary_key_pointer=value.get("primary_key_pointer", value.get("primary_key")),
        field_mappings=value.get("field_mappings", value.get("fields")),
        operation_key=value.get("operation_key", ""),
    )


def validate_operation(operation: DeclarativeOperation) -> None:
    """Public validation entry point for programmatically created operations."""
    if not isinstance(operation, DeclarativeOperation):
        raise SpecValidationError("invalid declarative operation")
    # The immutable model validates at construction.  Re-check the two policy
    # controls explicitly so the public contract remains obvious to callers.
    if operation.method not in ALLOWED_METHODS:
        raise SpecValidationError("unsupported HTTP method")
    if operation.operation_kind == "write" and not operation.explicit_write_enabled:
        raise SpecValidationError("write operation requires explicit enablement")


def _compile_operation(
    document: Mapping[str, Any],
    path: str,
    method: str,
    operation: Mapping[str, Any],
    path_item: Mapping[str, Any],
    base_url: str,
) -> DeclarativeOperation:
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 512:
        raise SpecValidationError("invalid OpenAPI path")
    if (
        "//" in path
        or "/../" in path
        or path.endswith("/..")
        or "/./" in path
        or path.endswith("/.")
        or "?" in path
        or "#" in path
        or "\\" in path
        or "%" in path
    ):
        raise SpecValidationError("invalid OpenAPI path")
    tool_key = operation.get("x-tool-key", operation.get("operationId"))
    mcp_name = operation.get("x-mcp-name", tool_key)
    input_mappings = _input_mappings(operation, path_item, path)
    operation_kind = operation.get("x-operation-kind")
    if operation_kind is None:
        operation_kind = "read" if method == "GET" else "write"
    explicit_write_enabled = operation.get("x-write-enabled", False)
    if not isinstance(explicit_write_enabled, bool):
        raise SpecValidationError("write enablement must be a bool")
    timeout_ms = operation.get("x-timeout-ms", DEFAULT_TIMEOUT_MS)
    cache_ttl = operation.get("x-cache-ttl-seconds")
    pagination = _pagination(operation, input_mappings)
    compiled = DeclarativeOperation(
        tool_key=tool_key,
        mcp_name=mcp_name,
        description=operation.get("summary", operation.get("description", tool_key or "")),
        method=method,
        path=path,
        input_mappings=input_mappings,
        output_mappings=_output_mappings(operation),
        operation_kind=operation_kind,
        explicit_write_enabled=explicit_write_enabled,
        base_url=base_url,
        timeout_ms=timeout_ms,
        cache_ttl_seconds=cache_ttl,
        pagination=pagination,
    )
    validate_operation(compiled)
    return compiled


def import_openapi_revision(
    document: str | bytes | Mapping[str, Any],
    *,
    spec_id: str = "",
    revision: int = 1,
    tenant_id: str = "",
    connection_id: str = "",
    status: str = "draft",
    allowed_hosts: Iterable[str] | None = None,
    sync_spec: Mapping[str, Any] | None = None,
) -> DeclarativeRevision:
    """Compile a bounded OpenAPI document into one immutable revision.

    This function performs no network access.  The caller must publish the
    returned revision and provide it to a connection explicitly.
    """
    data = load_revision_document(document)
    if not isinstance(data.get("openapi"), str) or not data["openapi"].startswith("3."):
        raise SpecValidationError("OpenAPI 3 document is required")
    servers = data.get("servers")
    if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], Mapping):
        raise SpecValidationError("exactly one OpenAPI server is required")
    base_url, base_host = _safe_base_url(servers[0].get("url"))
    configured_hosts: Iterable[Any]
    if allowed_hosts is not None:
        configured_hosts = allowed_hosts
    else:
        declared_hosts = data.get("x-allowed-hosts", [base_host])
        if not isinstance(declared_hosts, list):
            raise SpecValidationError("allowed hosts must be a list")
        configured_hosts = declared_hosts
    normalized_hosts = _normalize_allowed_hosts(configured_hosts, base_host)

    paths = data.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        raise SpecValidationError("OpenAPI paths are required")
    operations: list[DeclarativeOperation] = []
    for path, path_item in paths.items():
        if not isinstance(path_item, Mapping):
            raise SpecValidationError("invalid OpenAPI path item")
        for raw_method, operation in path_item.items():
            if raw_method == "parameters" or str(raw_method).startswith("x-"):
                continue
            method = raw_method.upper() if isinstance(raw_method, str) else ""
            if raw_method in _UNSUPPORTED_METHODS or method not in ALLOWED_METHODS:
                raise SpecValidationError("unsupported HTTP method")
            if raw_method not in _OPENAPI_METHODS or not isinstance(operation, Mapping):
                raise SpecValidationError("invalid OpenAPI operation")
            operations.append(
                _compile_operation(data, path, method, operation, path_item, base_url)
            )
            if len(operations) > MAX_OPERATION_COUNT:
                raise SpecValidationError("too many OpenAPI operations")
    if not operations:
        raise SpecValidationError("OpenAPI document has no supported operations")

    auth_schemes: set[AuthScheme | None] = set()
    for path_item in paths.values():
        if not isinstance(path_item, Mapping):
            continue
        for raw_method, operation in path_item.items():
            if raw_method in _OPENAPI_METHODS and isinstance(operation, Mapping):
                auth_schemes.add(_operation_auth(data, operation))
    # A revision has one credential policy.  Multiple opaque schemes would
    # make it possible for a tool call to select authentication dynamically.
    if len(auth_schemes) > 1:
        raise SpecValidationError("revision supports one authentication scheme")
    auth_scheme = next(iter(auth_schemes), None)
    if auth_scheme is not None and auth_scheme.kind == "oauth2_client_credentials":
        _, token_host = _safe_base_url(auth_scheme.token_url)
        if not any(
            token_host == allowed
            or (
                allowed.startswith("*.")
                and token_host.endswith("." + allowed[2:])
                and token_host != allowed[2:]
            )
            for allowed in normalized_hosts
        ):
            raise SpecValidationError("OAuth token host is absent from allowed hosts")
    compiled_sync = _sync_spec(sync_spec if sync_spec is not None else data.get("x-sync-spec"))
    return DeclarativeRevision(
        spec_id=spec_id,
        revision=revision,
        tenant_id=tenant_id,
        connection_id=connection_id,
        status=status,  # type: ignore[arg-type]
        base_url=base_url,
        allowed_hosts=normalized_hosts,
        operations=tuple(operations),
        auth_scheme=auth_scheme,
        sync_spec=compiled_sync,
    )


def validate_revision(
    revision: DeclarativeRevision | Mapping[str, Any] | str | bytes,
    *,
    data_mode: str | None = None,
) -> DeclarativeRevision | None:
    """Validate a revision or a raw document without evaluating any content."""
    if isinstance(revision, DeclarativeRevision):
        compiled = revision
    else:
        data = load_revision_document(revision)
        if "openapi" not in data:
            # The structural traversal above is still useful for callers that
            # validate a draft fragment; do not expose untrusted text in errors.
            raise SpecValidationError("OpenAPI 3 document is required")
        compiled = import_openapi_revision(data)
    if data_mode is not None:
        compiled.assert_data_mode_allowed(data_mode)
    return compiled
