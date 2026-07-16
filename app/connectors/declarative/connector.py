"""Execution boundary for prevalidated declarative API revisions.

The connector never accepts a caller supplied URL, method, header name, or
mapping.  It turns an already compiled operation into one bounded request and
returns only the revision's selected output fields.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from typing import Any

from app.connectors.contracts import ConnectionContext, ExecutionResult, SyncResult

from .http_client import SafeHttpClient
from .models import (
    AuthScheme,
    DeclarativeRevision,
    SpecValidationError,
    UnknownToolError,
)


_GENERIC_ERROR = {"error": "declarative operation failed"}
_MAX_CREDENTIAL_BYTES = 4_096


class DeclarativeConnector:
    """Execute only the operations carried by one immutable revision."""

    def __init__(self, *, revision: DeclarativeRevision, client: SafeHttpClient) -> None:
        if not isinstance(revision, DeclarativeRevision):
            raise TypeError("revision must be a DeclarativeRevision")
        if not isinstance(client, SafeHttpClient):
            raise TypeError("client must be a SafeHttpClient")
        if not client.exactly_matches_hosts(revision.allowed_hosts):
            raise ValueError("HTTP client host policy must exactly match the revision")
        self._revision = revision
        self._client = client

    def spec(self):
        """Return the common, data-only connector manifest for the revision."""
        return self._revision.connector_spec()

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if callable(close):
            await close()

    async def execute(
        self,
        context: ConnectionContext,
        tool_key: str,
        args: dict[str, Any],
    ) -> ExecutionResult:
        """Run one declared request and redact all upstream failure details."""
        revision = self._revision_for_context(context)
        # Keep an undeclared tool distinguishable for the shared runtime.  It
        # is an authorization boundary, not an upstream failure.
        operation = revision.operation_for(tool_key)
        try:
            if not isinstance(args, dict):
                raise SpecValidationError("tool arguments must be an object")
            request = operation.build_request(args)
            headers = operation.declared_headers(args)
            headers.update(await self._auth_headers(revision.auth_scheme, context, operation.timeout_ms))
            response = await self._request_with_timeout(
                operation.timeout_ms,
                request["method"],
                request["url"],
                headers,
                request["json_body"],
            )
            if not 200 <= response.status_code < 300:
                return self._error()
            payload = response.json()
            return ExecutionResult.ok(operation.extract_safe_output(payload))
        except UnknownToolError:
            raise
        except Exception:
            # Never include response bodies, URLs, request values, credentials,
            # or exception strings in a connector result.
            return self._error()

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
        if self._revision.connection_id and self._revision.connection_id != context.connection_id:
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
            return {auth_scheme.header_name: self._credential(context, auth_scheme.credential_key)}
        if auth_scheme.kind == "basic":
            username = self._credential(context, auth_scheme.username_key)
            password = self._credential(context, auth_scheme.password_key)
            raw = f"{username}:{password}".encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        if auth_scheme.kind == "oauth2_client_credentials":
            form_body = {
                "grant_type": "client_credentials",
                "client_id": self._credential(context, auth_scheme.client_id_key),
                "client_secret": self._credential(context, auth_scheme.client_secret_key),
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
            )
            if not 200 <= response.status_code < 300:
                raise RuntimeError("OAuth token request failed")
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise RuntimeError("OAuth token response is invalid")
            token = payload.get(auth_scheme.access_token_key)
            if not isinstance(token, str) or not token or len(token.encode("utf-8")) > _MAX_CREDENTIAL_BYTES:
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
        if len(value.encode("utf-8")) > _MAX_CREDENTIAL_BYTES or "\r" in value or "\n" in value:
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
    ):
        async with asyncio.timeout(timeout_ms / 1000):
            return await self._client.request(
                method,
                url,
                headers=headers,
                json_body=json_body,
                form_body=form_body,
            )

    @staticmethod
    def _error() -> ExecutionResult:
        return ExecutionResult(data=dict(_GENERIC_ERROR), status="error")
