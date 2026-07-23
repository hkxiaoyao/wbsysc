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
from collections.abc import Mapping
from typing import Any

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
    MAX_OUTPUT_ITEMS,
    DeclarativeOperation,
    DeclarativeRevision,
    SpecValidationError,
    ValueRef,
    _validate_input_value,
)
from .validator import validate_revision


_GENERIC_ERROR = {"error": "declarative operation failed"}
_MAX_CREDENTIAL_BYTES = 4_096
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
    ) -> None:
        self._bind(
            revision,
            client,
            audit_sink=audit_sink,
            allow_test_transport=False,
        )

    @classmethod
    def _for_test(
        cls,
        *,
        revision: DeclarativeRevision,
        client: SafeHttpClient,
        audit_sink: StepAuditSink | None = _write_step_audit,
    ) -> "DeclarativeConnector":
        instance = cls.__new__(cls)
        instance._bind(
            revision,
            client,
            audit_sink=audit_sink,
            allow_test_transport=True,
        )
        return instance

    def _bind(
        self,
        revision: DeclarativeRevision,
        client: SafeHttpClient,
        *,
        audit_sink: StepAuditSink | None,
        allow_test_transport: bool,
    ) -> None:
        if not isinstance(revision, DeclarativeRevision):
            raise TypeError("revision must be a DeclarativeRevision")
        if not isinstance(client, SafeHttpClient):
            raise TypeError("client must be a SafeHttpClient")
        revision = validate_revision(revision)
        if not client.uses_pinned_transport and not allow_test_transport:
            raise ValueError("HTTP client must use the pinned transport")
        if not client.exactly_matches_hosts(revision.allowed_hosts):
            raise ValueError("HTTP client host policy must exactly match the revision")
        self._revision = revision
        self._client = client
        self._audit_sink = audit_sink
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
                request = operation.build_request(args)
                headers = operation.declared_headers(args)
                headers.update(
                    await self._auth_headers(
                        self._revision.auth_scheme,
                        context,
                        timeout_ms,
                    )
                )
                response = await self._request_with_timeout(
                    timeout_ms,
                    request["method"],
                    request["url"],
                    headers,
                    request["json_body"],
                )
        except TimeoutError:
            raise _StepExecutionFailure("timeout") from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _StepExecutionFailure("operation_error") from None
        if not 200 <= response.status_code < 300:
            raise _StepExecutionFailure("operation_error")
        try:
            safe_output = operation.extract_safe_output(response.json())
        except Exception:
            raise _StepExecutionFailure("mapping_error") from None
        return ExecutionResult.ok(safe_output)

    async def sync(self, context: ConnectionContext, resource_key: str) -> SyncResult:
        """Run the declared sync operation without persisting an upstream body."""
        revision = self._revision_for_context(context)
        sync_spec = revision.sync_spec
        if sync_spec is None or resource_key != sync_spec.resource_key:
            raise SpecValidationError("unknown declarative sync resource")
        result = await self.execute(context, sync_spec.operation_key, {})
        return SyncResult(
            connection_id=context.connection_id,
            resource_key=resource_key,
            data=dict(result.data),
            status=result.status,
        )

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
            form_body = {
                "grant_type": "client_credentials",
                "client_id": self._credential(context, auth_scheme.client_id_key),
                "client_secret": self._credential(
                    context, auth_scheme.client_secret_key
                ),
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
            return {"Authorization": f"Bearer {token}"}
        raise RuntimeError("unsupported authentication scheme")

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
        form_body: Mapping[str, str] | None = None,
        allow_redirects: bool = True,
    ):
        async with asyncio.timeout(timeout_ms / 1000):
            return await self._client.request(
                method,
                url,
                headers=headers,
                json_body=json_body,
                form_body=form_body,
                allow_redirects=allow_redirects,
            )

    @staticmethod
    def _error() -> ExecutionResult:
        return ExecutionResult(data=dict(_GENERIC_ERROR), status="error")
