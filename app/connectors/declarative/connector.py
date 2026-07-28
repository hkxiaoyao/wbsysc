"""Execution boundary for prevalidated declarative API revisions.

The connector never accepts a caller supplied URL, method, header name, or
mapping.  It turns an already compiled operation into one bounded request and
returns only the revision's selected output fields.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import time
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.connections.cache import contains_sensitive_value
from app.connectors.contracts import ConnectionContext, ExecutionResult, SyncResult
from app.mcp_audit import write_event
from app.mcp_log_models import (
    MAX_STEP_AUDIT_COST_MS,
    McpLogEvent,
    StepAuditEvent,
    StepAuditSink,
    StepAuditStatus,
)

from .http_client import SafeHttpClient
from .models import (
    AuthScheme,
    DEFAULT_TIMEOUT_MS,
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_ITEMS,
    DeclarativeOperation,
    DeclarativeRevision,
    SpecValidationError,
    ValueRef,
    _json_size,
    _read_pointer,
    _selected_copy,
    _validate_input_value,
)
from .token_cache import (
    OAuthTokenCache,
    TokenCacheKey,
    get_default_token_cache,
    parse_lifetime_seconds,
)
from .validator import validate_revision


_GENERIC_ERROR = {"error": "declarative operation failed"}
_MAX_CREDENTIAL_BYTES = 4_096
_MAX_CURSOR_LENGTH = 512
_MAX_RECORD_KEY_LENGTH = 255
_MAX_TOOL_TIMEOUT_SECONDS = 60.0
_MAX_PENDING_STEP_AUDITS = 32
_STEP_AUDIT_SINK_TIMEOUT_SECONDS = 1.0
_STEP_AUDIT_CLOSE_TIMEOUT_SECONDS = 0.1
_ACTIVE_STEP_AUDIT_TASKS: set[asyncio.Task[None]] = set()
_QUARANTINED_STEP_AUDIT_TASKS: set[asyncio.Task[None]] = set()


logger = logging.getLogger(__name__)


class _StepExecutionFailure(Exception):
    def __init__(self, error_code: str) -> None:
        super().__init__()
        self.error_code = error_code


def _cursor_value(document: Any, pointer: str) -> str | None:
    """Read the declared cursor as a bounded scalar, or ``None`` to stop.

    The cursor is upstream-controlled, so it is treated as untrusted control
    data: it is never a URL, only ever one query parameter value, and anything
    unexpected ends the traversal instead of raising.
    """
    try:
        value = _read_pointer(document, pointer)
    except Exception:
        return None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str) or not value:
        return None
    if len(value) > _MAX_CURSOR_LENGTH:
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    return value


def _default_record_store() -> Any:
    """Bind the central record table lazily to avoid an import cycle."""
    from app.connections import store

    return store


def _record_key_value(value: Any) -> str | None:
    """Normalize one projected primary-key value without retaining its source."""
    return _normalize_cursor(value)


def _normalize_cursor(value: Any) -> str | None:
    """Accept only the bounded scalar form used in a cursor query value."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not value or len(value) > _MAX_RECORD_KEY_LENGTH:
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    return value


def _record_key(item: Any, pointer: str) -> str | None:
    """Read a bounded scalar primary key, or ``None`` to skip the record."""
    try:
        value = _read_pointer(item, pointer)
    except Exception:
        return None
    return _record_key_value(value)


def _url_with_cursor(url: str, parameter: str, cursor: str) -> str:
    """Rewrite exactly one query parameter, leaving scheme/host/path fixed."""
    parts = urlsplit(url)
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name != parameter
    ]
    query.append((parameter, cursor))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query, doseq=False, safe=""),
            parts.fragment,
        )
    )


def _write_step_audit(event: StepAuditEvent) -> None:
    """Persist the safe event without copying tool data into central logs."""
    write_event(
        McpLogEvent(
            connection_id=event.connection_id,
            tool_key=event.tool_key,
            category="tool",
            event_name=f"declarative_step.{event.step_id}",
            target=event.operation_key,
            params_summary="omitted",
            result_status=event.status,
            error_code=event.error_code,
            cost_ms=event.cost_ms,
        )
    )


def _validate_public_input(value: Any, schema: Mapping[str, Any]) -> None:
    """Validate the compiled subset while honoring open nested objects."""
    schema_type = schema.get("type")
    if schema_type == "null":
        if value is not None:
            raise SpecValidationError("input type does not match declaration")
        _validate_input_value(value, schema)
        return
    if schema_type not in {"object", "array"}:
        _validate_input_value(value, schema)
        return

    if schema_type == "array":
        if not isinstance(value, list):
            raise SpecValidationError("input type does not match declaration")
        if len(value) > MAX_OUTPUT_ITEMS:
            raise SpecValidationError("input array exceeds limits")
        if "enum" in schema and value not in schema["enum"]:
            raise SpecValidationError("input is outside the declared enum")
        item_schema = schema.get("items")
        if not isinstance(item_schema, Mapping):
            raise SpecValidationError("invalid declared input schema")
        for item in value:
            _validate_public_input(item, item_schema)
        return

    if not isinstance(value, Mapping):
        raise SpecValidationError("input type does not match declaration")
    properties = schema.get("properties", {})
    required = schema.get("required", ())
    if not isinstance(properties, Mapping) or not isinstance(required, (list, tuple)):
        raise SpecValidationError("invalid declared input schema")
    if any(name not in value for name in required):
        raise SpecValidationError("required object input is missing")
    if "enum" in schema and value not in schema["enum"]:
        raise SpecValidationError("input is outside the declared enum")

    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, (bool, Mapping)):
        raise SpecValidationError("invalid declared input schema")
    for key, item in value.items():
        if not isinstance(key, str):
            raise SpecValidationError("undeclared object input")
        child_schema = properties.get(key)
        if child_schema is not None:
            if not isinstance(child_schema, Mapping):
                raise SpecValidationError("invalid declared input schema")
            _validate_public_input(item, child_schema)
        elif additional is False:
            raise SpecValidationError("undeclared object input")
        elif isinstance(additional, Mapping):
            _validate_public_input(item, additional)


class DeclarativeConnector:
    """Execute only the operations carried by one immutable revision."""

    def __init__(
        self,
        *,
        revision: DeclarativeRevision,
        client: SafeHttpClient,
        audit_sink: StepAuditSink | None = _write_step_audit,
        token_cache: OAuthTokenCache | None = None,
        record_store: Any | None = None,
    ) -> None:
        self._bind(
            revision,
            client,
            audit_sink=audit_sink,
            token_cache=token_cache,
            record_store=record_store,
            allow_test_transport=False,
        )

    @classmethod
    def _for_test(
        cls,
        *,
        revision: DeclarativeRevision,
        client: SafeHttpClient,
        audit_sink: StepAuditSink | None = _write_step_audit,
        token_cache: OAuthTokenCache | None = None,
        record_store: Any | None = None,
    ) -> "DeclarativeConnector":
        instance = cls.__new__(cls)
        instance._bind(
            revision,
            client,
            audit_sink=audit_sink,
            token_cache=token_cache,
            record_store=record_store,
            allow_test_transport=True,
        )
        return instance

    def _bind(
        self,
        revision: DeclarativeRevision,
        client: SafeHttpClient,
        *,
        audit_sink: StepAuditSink | None,
        token_cache: OAuthTokenCache | None,
        record_store: Any | None,
        allow_test_transport: bool,
    ) -> None:
        if not isinstance(revision, DeclarativeRevision):
            raise TypeError("revision must be a DeclarativeRevision")
        if not isinstance(client, SafeHttpClient):
            raise TypeError("client must be a SafeHttpClient")
        if token_cache is not None and not isinstance(token_cache, OAuthTokenCache):
            raise TypeError("token_cache must be an OAuthTokenCache")
        revision = validate_revision(revision)
        if not client.uses_pinned_transport and not allow_test_transport:
            raise ValueError("HTTP client must use the pinned transport")
        if not client.exactly_matches_hosts(revision.allowed_hosts):
            raise ValueError("HTTP client host policy must exactly match the revision")
        self._revision = revision
        self._client = client
        self._audit_sink = audit_sink
        self._token_cache = token_cache or get_default_token_cache()
        self._record_store = record_store or _default_record_store()
        self._audit_tasks: set[asyncio.Task[None]] = set()

    def spec(self):
        """Return the common, data-only connector manifest for the revision."""
        return self._revision.connector_spec()

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if callable(close):
            await close()
        tasks = tuple(self._audit_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=_STEP_AUDIT_CLOSE_TIMEOUT_SECONDS)
        for task in tasks:
            self._audit_tasks.discard(task)
            if not task.done():
                _QUARANTINED_STEP_AUDIT_TASKS.add(task)
                # Python cannot force-kill a coroutine that swallows cancellation.
                # The global registry keeps these tasks capped and observed.
                task._log_destroy_pending = False  # type: ignore[attr-defined]

    async def execute(
        self,
        context: ConnectionContext,
        tool_key: str,
        args: dict[str, Any],
    ) -> ExecutionResult:
        """Run a public tool's declared steps and retain only mapped outputs."""
        revision = self._revision_for_context(context)
        # Keep an undeclared tool distinguishable for the shared runtime.  It
        # is an authorization boundary, not an upstream failure.
        tool = revision.tool_for(tool_key)
        try:
            if not isinstance(args, dict):
                raise SpecValidationError("tool arguments must be an object")
            _validate_public_input(args, tool.input_schema)
        except Exception:
            return self._error()

        # ``stored`` means the synced resource is served from the local
        # projection; every other tool still goes upstream, matching the
        # WeCom connector's stored semantics.
        try:
            stored = self._stored_read(context, revision, tool_key)
        except Exception:
            logger.warning("Declarative stored read failed")
            return self._error()
        if stored is not None:
            return stored

        step_outputs: dict[str, dict[str, Any]] = {}
        active_step = None
        active_operation = None
        active_started_at = 0.0
        active_audited = False
        try:
            async with asyncio.timeout(_MAX_TOOL_TIMEOUT_SECONDS):
                for step in tool.steps:
                    operation = revision.operation_for(step.operation_key)
                    active_step = step
                    active_operation = operation
                    active_started_at = time.monotonic()
                    active_audited = False
                    try:
                        step_args = {
                            name: self._resolve_ref(reference, args, step_outputs)
                            for name, reference in step.input_mappings.items()
                            if not (
                                reference.source == "input"
                                and reference.field not in args
                            )
                        }
                    except Exception:
                        raise _StepExecutionFailure("mapping_error") from None
                    timeout_ms = (
                        operation.timeout_ms
                        if step.timeout_ms is None
                        else step.timeout_ms
                    )
                    result = await self._execute_operation(
                        context,
                        operation,
                        step_args,
                        timeout_ms=timeout_ms,
                    )
                    if result.status != "ok":
                        raise _StepExecutionFailure("operation_error")
                    try:
                        step_outputs[step.step_id] = {
                            name: result.data[operation_output]
                            for name, operation_output in step.output_mappings.items()
                        }
                    except Exception:
                        raise _StepExecutionFailure("mapping_error") from None
                    active_audited = True
                    self._emit_step_audit(
                        context,
                        tool.tool_key,
                        step.step_id,
                        operation.tool_key,
                        "ok",
                        "",
                        active_started_at,
                    )
                return ExecutionResult.ok(
                    {
                        name: self._resolve_ref(reference, args, step_outputs)
                        for name, reference in tool.result_map.items()
                    }
                )
        except _StepExecutionFailure as exc:
            if active_step is not None and active_operation is not None:
                self._emit_step_audit(
                    context,
                    tool.tool_key,
                    active_step.step_id,
                    active_operation.tool_key,
                    "error",
                    exc.error_code,
                    active_started_at,
                )
            return self._error()
        except TimeoutError:
            if (
                active_step is not None
                and active_operation is not None
                and not active_audited
            ):
                self._emit_step_audit(
                    context,
                    tool.tool_key,
                    active_step.step_id,
                    active_operation.tool_key,
                    "error",
                    "timeout",
                    active_started_at,
                )
            return self._error()
        except asyncio.CancelledError:
            if (
                active_step is not None
                and active_operation is not None
                and not active_audited
            ):
                self._emit_step_audit(
                    context,
                    tool.tool_key,
                    active_step.step_id,
                    active_operation.tool_key,
                    "error",
                    "cancelled",
                    active_started_at,
                )
            raise
        except Exception:
            if (
                active_step is not None
                and active_operation is not None
                and not active_audited
            ):
                self._emit_step_audit(
                    context,
                    tool.tool_key,
                    active_step.step_id,
                    active_operation.tool_key,
                    "error",
                    "mapping_error",
                    active_started_at,
                )
            # Never include response bodies, URLs, request values, credentials,
            # or exception strings in a connector result.
            return self._error()

    def _emit_step_audit(
        self,
        context: ConnectionContext,
        tool_key: str,
        step_id: str,
        operation_key: str,
        status: StepAuditStatus,
        error_code: str,
        started_at: float,
    ) -> None:
        if self._audit_sink is None:
            return
        event = StepAuditEvent(
            connection_id=context.connection_id,
            tool_key=tool_key,
            step_id=step_id,
            operation_key=operation_key,
            status=status,
            error_code=error_code,
            cost_ms=min(
                MAX_STEP_AUDIT_COST_MS,
                max(0, int((time.monotonic() - started_at) * 1000)),
            ),
        )
        try:
            outcome = self._audit_sink(event)
            if inspect.isawaitable(outcome):
                if len(_ACTIVE_STEP_AUDIT_TASKS) >= _MAX_PENDING_STEP_AUDITS:
                    self._discard_step_audit_awaitable(outcome)
                    logger.warning("Declarative step audit queue is full")
                    return
                task = asyncio.create_task(self._deliver_step_audit(outcome))
                self._audit_tasks.add(task)
                _ACTIVE_STEP_AUDIT_TASKS.add(task)
                task.add_done_callback(self._step_audit_task_done)
        except BaseException:
            logger.warning("Declarative step audit sink failed")

    def _step_audit_task_done(self, task: asyncio.Task[None]) -> None:
        self._audit_tasks.discard(task)
        _ACTIVE_STEP_AUDIT_TASKS.discard(task)
        _QUARANTINED_STEP_AUDIT_TASKS.discard(task)
        try:
            task.exception()
        except asyncio.CancelledError:
            return
        except BaseException:
            logger.warning("Declarative step audit sink failed")

    async def _deliver_step_audit(self, outcome: Any) -> None:
        try:
            async with asyncio.timeout(_STEP_AUDIT_SINK_TIMEOUT_SECONDS):
                await outcome
        except asyncio.CancelledError:
            return
        except BaseException:
            logger.warning("Declarative step audit sink failed")

    @staticmethod
    def _discard_step_audit_awaitable(outcome: Any) -> None:
        try:
            if isinstance(outcome, asyncio.Future):
                outcome.cancel()
                return
            close = getattr(outcome, "close", None)
            if callable(close):
                close()
        except BaseException:
            logger.warning("Declarative step audit sink failed")

    @staticmethod
    def _resolve_ref(
        ref: ValueRef,
        tool_args: Mapping[str, Any],
        step_outputs: Mapping[str, Mapping[str, Any]],
    ) -> Any:
        if ref.source == "input":
            return tool_args[ref.field]
        return step_outputs[ref.step_id or ""][ref.field]

    async def _execute_operation(
        self,
        context: ConnectionContext,
        operation: DeclarativeOperation,
        args: Mapping[str, Any],
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> ExecutionResult:
        """Run one operation through the safe client and discard its raw response."""
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                return await self._execute_pages(context, operation, args, timeout_ms)
        except _StepExecutionFailure:
            raise
        except TimeoutError:
            raise _StepExecutionFailure("timeout") from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _StepExecutionFailure("operation_error") from None

    async def _execute_pages(
        self,
        context: ConnectionContext,
        operation: DeclarativeOperation,
        args: Mapping[str, Any],
        timeout_ms: int,
    ) -> ExecutionResult:
        """Fetch the first page and, when declared, follow its cursor.

        Every page reuses the same authorization headers, so a multi-page read
        performs at most one OAuth exchange.  Later pages differ from the first
        only by the declared cursor parameter.
        """
        request = operation.build_request(args)
        declared_headers = operation.declared_headers(args)
        document = await self._fetch_page(
            context, request, declared_headers, timeout_ms, page_count=1
        )
        first_page = self._safe_output(operation, document)

        pagination = operation.pagination
        if pagination is None or pagination.max_pages <= 1:
            return ExecutionResult.ok(first_page)

        items_field = operation.pagination_items_field
        collected = first_page.get(items_field)
        if not isinstance(collected, list):
            return ExecutionResult.ok(first_page)
        first_items = list(collected)
        if not self._fits_output_budget(first_page, items_field, []):
            raise _StepExecutionFailure("mapping_error")
        collected = self._bounded_items(
            first_page,
            items_field,
            first_items,
            pagination.max_items,
        )
        # The first page itself exhausted an item or byte budget.  Returning a
        # bounded prefix is safer than issuing another request that cannot be
        # represented in the result.
        if len(collected) < min(
            len(first_items), pagination.max_items, MAX_OUTPUT_ITEMS
        ):
            result = dict(first_page)
            result[items_field] = collected
            return ExecutionResult.ok(result)

        for page_count in range(2, pagination.max_pages + 1):
            if len(collected) >= pagination.max_items:
                break
            cursor = _cursor_value(document, pagination.next_pointer)
            if cursor is None:
                break
            document = await self._fetch_page(
                context,
                {
                    **request,
                    "url": _url_with_cursor(
                        request["url"], pagination.next_query_param, cursor
                    ),
                },
                declared_headers,
                timeout_ms,
                page_count=page_count,
            )
            page_items = self._safe_output(operation, document).get(items_field)
            if not isinstance(page_items, list) or not page_items:
                break
            combined = collected + page_items
            candidate = self._bounded_items(
                first_page,
                items_field,
                combined,
                pagination.max_items,
            )
            if len(candidate) == len(collected):
                break
            collected = candidate
            if len(candidate) < min(
                len(combined), pagination.max_items, MAX_OUTPUT_ITEMS
            ):
                break

        result = dict(first_page)
        result[items_field] = collected[: pagination.max_items]
        return ExecutionResult.ok(result)

    async def _fetch_page(
        self,
        context: ConnectionContext,
        request: Mapping[str, Any],
        declared_headers: dict[str, str],
        timeout_ms: int,
        *,
        page_count: int,
    ) -> Any:
        response = await self._send_authorized(
            context,
            request,
            declared_headers,
            timeout_ms,
            page_count=page_count,
        )
        if not 200 <= response.status_code < 300:
            raise _StepExecutionFailure("operation_error")
        try:
            return response.json()
        except Exception:
            raise _StepExecutionFailure("mapping_error") from None

    @staticmethod
    def _safe_output(
        operation: DeclarativeOperation, document: Any
    ) -> dict[str, Any]:
        try:
            # A paginated items array may be larger than the final output
            # budget even though a safe prefix can be returned.  Copy only the
            # declared selections here; _bounded_items applies the aggregate
            # byte limit before anything leaves the connector.
            if operation.pagination is not None and operation.pagination.max_pages > 1:
                selected: dict[str, Any] = {}
                for mapping in operation.output_mappings:
                    value = _read_pointer(document, mapping.pointer)
                    if (
                        mapping.pointer == operation.pagination.items_pointer
                        and isinstance(value, list)
                    ):
                        selected[mapping.name] = [
                            _selected_copy(item)
                            for item in value[:MAX_OUTPUT_ITEMS]
                        ]
                    else:
                        selected[mapping.name] = _selected_copy(value)
                return selected
            return operation.extract_safe_output(document)
        except Exception:
            raise _StepExecutionFailure("mapping_error") from None

    @staticmethod
    def _fits_output_budget(
        first_page: Mapping[str, Any],
        items_field: str,
        candidate: list[Any],
    ) -> bool:
        """Stop before the accumulated projection outgrows the output ceiling."""
        if len(candidate) > MAX_OUTPUT_ITEMS:
            return False
        probe = dict(first_page)
        probe[items_field] = candidate
        try:
            return _json_size(probe) <= MAX_OUTPUT_BYTES
        except Exception:
            return False

    @classmethod
    def _bounded_items(
        cls,
        first_page: Mapping[str, Any],
        items_field: str,
        items: list[Any],
        max_items: int,
    ) -> list[Any]:
        """Return the largest prefix that satisfies all output budgets."""
        upper = min(len(items), max_items, MAX_OUTPUT_ITEMS)
        low = 0
        high = upper
        while low < high:
            midpoint = (low + high + 1) // 2
            if cls._fits_output_budget(first_page, items_field, items[:midpoint]):
                low = midpoint
            else:
                high = midpoint - 1
        return list(items[:low])

    async def sync(self, context: ConnectionContext, resource_key: str) -> SyncResult:
        """Persist the declared projection and report only bounded counters.

        The orchestrator must never see upstream content, so the result carries
        counts alone.  Storage receives the ``field_mappings`` projection only.
        """
        revision = self._revision_for_context(context)
        sync_spec = revision.sync_spec
        if sync_spec is None or resource_key != sync_spec.resource_key:
            raise SpecValidationError("unknown declarative sync resource")
        operation = revision.operation_for(sync_spec.operation_key)
        if operation.pagination is not None and operation.pagination.max_pages > 1:
            try:
                async with asyncio.timeout(
                    min(operation.timeout_ms / 1000, _MAX_TOOL_TIMEOUT_SECONDS)
                ):
                    return await self._sync_paginated(
                        context, resource_key, sync_spec, operation
                    )
            except TimeoutError:
                return SyncResult(
                    connection_id=context.connection_id,
                    resource_key=resource_key,
                    data={"pulled": 0, "stored": 0, "skipped": 0},
                    status="error",
                )
        document = await self._fetch_sync_document(context, operation)

        pulled, records = self._project_records(sync_spec, operation, document)
        stored = self._record_store.upsert_declarative_records(
            context.connection_id, resource_key, records
        )
        skipped = pulled - stored
        return SyncResult(
            connection_id=context.connection_id,
            resource_key=resource_key,
            data={"pulled": pulled, "stored": stored, "skipped": skipped},
            status="partial" if skipped else "ok",
        )

    async def _sync_paginated(
        self,
        context: ConnectionContext,
        resource_key: str,
        sync_spec: Any,
        operation: DeclarativeOperation,
    ) -> SyncResult:
        """Persist one bounded page at a time and durably advance its cursor.

        A failed fetch or write leaves the last committed cursor untouched, so
        the next scheduled run resumes at the first page not known to be stored.
        Replayed pages remain safe because record persistence is an UPSERT.
        """
        pagination = operation.pagination
        if pagination is None:
            raise RuntimeError("paginated sync requires pagination")
        request = operation.build_request({})
        declared_headers = operation.declared_headers({})
        load_cursor = getattr(
            self._record_store, "get_declarative_sync_cursor", None
        )
        save_cursor = getattr(
            self._record_store, "save_declarative_sync_cursor", None
        )
        persisted_cursor = load_cursor(context.connection_id, resource_key)
        cursor = _normalize_cursor(persisted_cursor)
        if cursor is not None:
            request = {
                **request,
                "url": _url_with_cursor(
                    request["url"], pagination.next_query_param, cursor
                ),
            }

        pulled_total = 0
        stored_total = 0
        skipped_total = 0
        for page_count in range(1, pagination.max_pages + 1):
            document = await self._fetch_page(
                context,
                request,
                declared_headers,
                operation.timeout_ms,
                page_count=page_count,
            )
            safe_page = self._safe_output(operation, document)
            items_field = operation.pagination_items_field
            page_items = safe_page.get(items_field)
            if not isinstance(page_items, list):
                raise _StepExecutionFailure("mapping_error")
            remaining = max(0, pagination.max_items - pulled_total)
            if len(page_items) > remaining:
                safe_page = dict(safe_page)
                safe_page[items_field] = page_items[:remaining]
            pulled, records = self._project_records(sync_spec, operation, safe_page)
            stored = self._record_store.upsert_declarative_records(
                context.connection_id, resource_key, records
            )
            pulled_total += pulled
            stored_total += stored
            skipped_total += pulled - stored

            next_cursor = _cursor_value(document, pagination.next_pointer)
            if next_cursor is None:
                save_cursor(context.connection_id, resource_key, None)
                break
            # The checkpoint advances only after this page's projection has
            # committed.  A crash in either operation replays at most one page.
            save_cursor(context.connection_id, resource_key, next_cursor)
            if pulled_total >= pagination.max_items or not page_items:
                break
            cursor = next_cursor
            request = {
                **request,
                "url": _url_with_cursor(
                    request["url"], pagination.next_query_param, cursor
                ),
            }

        return SyncResult(
            connection_id=context.connection_id,
            resource_key=resource_key,
            data={
                "pulled": pulled_total,
                "stored": stored_total,
                "skipped": skipped_total,
            },
            status="partial" if skipped_total else "ok",
        )

    async def _fetch_sync_document(
        self,
        context: ConnectionContext,
        operation: DeclarativeOperation,
    ) -> dict[str, Any]:
        """Run the sync operation upstream, following any declared pagination."""
        result = await self._execute_operation(
            context, operation, {}, timeout_ms=operation.timeout_ms
        )
        return dict(result.data)

    @staticmethod
    def _project_records(
        sync_spec: Any,
        operation: DeclarativeOperation,
        document: Mapping[str, Any],
    ) -> tuple[int, list[dict[str, Any]]]:
        """Turn one projected response into keyed, field-mapped storage rows.

        ``document`` is already the operation's safe output, so this only
        reshapes declared fields; it can never widen the projection.
        """
        if sync_spec.items_pointer:
            items_field = next(
                (
                    mapping.name
                    for mapping in operation.output_mappings
                    if mapping.pointer == sync_spec.items_pointer
                ),
                "",
            )
            raw_items = document.get(items_field)
            items = raw_items if isinstance(raw_items, list) else []
        else:
            items = [document]

        pointer_fields = {
            mapping.pointer: mapping.name for mapping in operation.output_mappings
        }
        records: list[dict[str, Any]] = []
        for item in items:
            if sync_spec.items_pointer:
                def read_value(pointer: str) -> Any:
                    return _read_pointer(item, pointer)
            else:
                def read_value(pointer: str) -> Any:
                    field_name = pointer_fields.get(pointer)
                    if not field_name:
                        raise KeyError(pointer)
                    return document[field_name]

            try:
                record_key = _record_key_value(
                    read_value(sync_spec.primary_key_pointer)
                )
            except Exception:
                record_key = None
            if record_key is None:
                continue
            payload: dict[str, Any] = {}
            usable = True
            for field_name, pointer in sync_spec.field_mappings.items():
                try:
                    payload[field_name] = read_value(pointer)
                except Exception:
                    usable = False
                    break
            if not usable or contains_sensitive_value(payload):
                continue
            records.append({"record_key": record_key, "payload": payload})
        return len(items), records

    def _stored_read(
        self,
        context: ConnectionContext,
        revision: DeclarativeRevision,
        tool_key: str,
    ) -> ExecutionResult | None:
        """Serve a synced resource from storage, or ``None`` to go upstream."""
        sync_spec = revision.sync_spec
        if sync_spec is None or context.data_mode != "stored":
            return None
        try:
            operation = revision.operation_for(sync_spec.operation_key)
        except Exception:
            return None
        if tool_key not in {operation.tool_key, operation.mcp_name}:
            return None
        payloads = self._record_store.list_declarative_records(
            context.connection_id, sync_spec.resource_key
        )
        if not sync_spec.items_pointer:
            return ExecutionResult.ok(dict(payloads[0]) if payloads else {})
        items_field = next(
            (
                mapping.name
                for mapping in operation.output_mappings
                if mapping.pointer == sync_spec.items_pointer
            ),
            "",
        )
        if not items_field:
            return None
        return ExecutionResult.ok({items_field: [dict(row) for row in payloads]})

    def _revision_for_context(self, context: ConnectionContext) -> DeclarativeRevision:
        if not isinstance(context, ConnectionContext):
            raise TypeError("context must be a ConnectionContext")
        if (
            self._revision.connection_id
            and self._revision.connection_id != context.connection_id
        ):
            raise PermissionError("declarative revision is unavailable")
        if self._revision.tenant_id and self._revision.tenant_id != context.tenant_id:
            raise PermissionError("declarative revision is unavailable")
        self._revision.assert_data_mode_allowed(context.data_mode)
        return self._revision

    async def _send_authorized(
        self,
        context: ConnectionContext,
        request: Mapping[str, Any],
        declared_headers: dict[str, str],
        timeout_ms: int,
        *,
        page_count: int = 1,
    ) -> Any:
        """Send one declared request, refreshing a rejected OAuth token once.

        A cached token can be revoked upstream before its announced expiry.
        Retrying exactly once keeps that recoverable without weakening the
        fail-fast contract for genuine authorization failures.
        """
        auth_scheme = self._revision.auth_scheme
        headers = dict(declared_headers)
        headers.update(await self._auth_headers(auth_scheme, context, timeout_ms))
        response = await self._request_with_timeout(
            timeout_ms,
            request["method"],
            request["url"],
            headers,
            request["json_body"],
            page_count=page_count,
        )
        if response.status_code != 401:
            return response
        key = self._oauth_cache_key(auth_scheme, context)
        if key is None:
            return response
        self._token_cache.discard(key)
        headers = dict(declared_headers)
        headers.update(await self._auth_headers(auth_scheme, context, timeout_ms))
        return await self._request_with_timeout(
            timeout_ms,
            request["method"],
            request["url"],
            headers,
            request["json_body"],
            page_count=page_count,
        )

    def _oauth_cache_key(
        self,
        auth_scheme: AuthScheme | None,
        context: ConnectionContext,
    ) -> TokenCacheKey | None:
        """Derive the token identity, or ``None`` when the scheme has no token."""
        if auth_scheme is None or auth_scheme.kind != "oauth2_client_credentials":
            return None
        try:
            return self._token_cache.cache_key(
                context.connection_id,
                token_url=auth_scheme.token_url,
                client_id_key=auth_scheme.client_id_key,
                client_secret_key=auth_scheme.client_secret_key,
                scopes=auth_scheme.scopes,
                client_id=self._credential(context, auth_scheme.client_id_key),
                client_secret=self._credential(context, auth_scheme.client_secret_key),
            )
        except Exception:
            return None

    async def _auth_headers(
        self,
        auth_scheme: AuthScheme | None,
        context: ConnectionContext,
        timeout_ms: int,
    ) -> dict[str, str]:
        if auth_scheme is None:
            return {}
        if auth_scheme.kind == "api_key":
            return {
                auth_scheme.header_name: self._credential(
                    context, auth_scheme.credential_key
                )
            }
        if auth_scheme.kind == "basic":
            username = self._credential(context, auth_scheme.username_key)
            password = self._credential(context, auth_scheme.password_key)
            raw = f"{username}:{password}".encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        if auth_scheme.kind == "oauth2_client_credentials":
            client_id = self._credential(context, auth_scheme.client_id_key)
            client_secret = self._credential(context, auth_scheme.client_secret_key)
            key = self._token_cache.cache_key(
                context.connection_id,
                token_url=auth_scheme.token_url,
                client_id_key=auth_scheme.client_id_key,
                client_secret_key=auth_scheme.client_secret_key,
                scopes=auth_scheme.scopes,
                client_id=client_id,
                client_secret=client_secret,
            )

            async def exchange() -> tuple[str, float]:
                return await self._exchange_oauth_token(
                    auth_scheme, client_id, client_secret, timeout_ms
                )

            token = await self._token_cache.get_or_exchange(key, exchange)
            return {"Authorization": f"Bearer {token}"}
        raise RuntimeError("unsupported authentication scheme")

    async def _exchange_oauth_token(
        self,
        auth_scheme: AuthScheme,
        client_id: str,
        client_secret: str,
        timeout_ms: int,
    ) -> tuple[str, float]:
        """Perform one client-credentials exchange and read its lifetime."""
        form_body = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if auth_scheme.scopes:
            form_body["scope"] = " ".join(auth_scheme.scopes)
        response = await self._request_with_timeout(
            timeout_ms,
            "POST",
            auth_scheme.token_url,
            {"Accept": "application/json"},
            None,
            form_body=form_body,
            allow_redirects=False,
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError("OAuth token request failed")
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("OAuth token response is invalid")
        token = payload.get(auth_scheme.access_token_key)
        if (
            not isinstance(token, str)
            or not token
            or len(token.encode("utf-8")) > _MAX_CREDENTIAL_BYTES
        ):
            raise RuntimeError("OAuth token response is invalid")
        if "\r" in token or "\n" in token:
            raise RuntimeError("OAuth token response is invalid")
        return token, parse_lifetime_seconds(payload.get("expires_in"))

    @staticmethod
    def _credential(context: ConnectionContext, credential_key: str) -> str:
        value = context.credentials.get(credential_key)
        if not isinstance(value, str) or not value:
            raise RuntimeError("credential is unavailable")
        if (
            len(value.encode("utf-8")) > _MAX_CREDENTIAL_BYTES
            or "\r" in value
            or "\n" in value
        ):
            raise RuntimeError("credential is unavailable")
        return value

    async def _request_with_timeout(
        self,
        timeout_ms: int,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: object | None,
        *,
        page_count: int = 1,
        form_body: Mapping[str, str] | None = None,
        allow_redirects: bool = True,
    ):
        async with asyncio.timeout(timeout_ms / 1000):
            return await self._client.request(
                method,
                url,
                headers=headers,
                json_body=json_body,
                page_count=page_count,
                form_body=form_body,
                allow_redirects=allow_redirects,
            )

    @staticmethod
    def _error() -> ExecutionResult:
        return ExecutionResult(data=dict(_GENERIC_ERROR), status="error")
