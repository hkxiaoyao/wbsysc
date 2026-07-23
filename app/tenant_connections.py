"""Tenant-session adapters for connection management use cases."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from . import admin_connections as connections
from .tenant_auth.dependencies import require_same_origin, require_tenant_principal
from .tenant_auth.models import TenantPrincipal


_NO_STORE_HEADERS = {"Cache-Control": "no-store"}
logger = logging.getLogger(__name__)
_RAW_RESPONSE_SUFFIXES = (
    "/connections",
    "/tokens",
    "/tokens/rotate",
)


class _TenantConnectionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        protects_raw_response = self.path.endswith(_RAW_RESPONSE_SUFFIXES)

        async def no_store_failures(request: Request):
            try:
                return await handler(request)
            except HTTPException as exc:
                if protects_raw_response:
                    raise _no_store_exception(exc) from None
                raise
            except RequestValidationError as exc:
                if not protects_raw_response:
                    raise
                raise HTTPException(
                    422,
                    detail=exc.errors(),
                    headers=_NO_STORE_HEADERS,
                ) from None
            except Exception as exc:
                if not protects_raw_response:
                    raise
                logger.error(
                    "Tenant connection raw response failed type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    500,
                    "tenant connection operation failed",
                    headers=_NO_STORE_HEADERS,
                ) from None

        return no_store_failures


router = APIRouter(
    prefix="/tenant",
    tags=["tenant-connections"],
    route_class=_TenantConnectionRoute,
)


def _principal(request: Request) -> TenantPrincipal:
    principal = require_tenant_principal(request)
    _reject_query(request)
    return principal


def _mutation_principal(request: Request) -> TenantPrincipal:
    principal = require_tenant_principal(request)
    require_same_origin(request)
    _reject_query(request)
    return principal


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise HTTPException(422, "ambiguous tenant connection query")


async def _require_empty_body(request: Request) -> None:
    if await request.body():
        raise HTTPException(422, "tenant connection route does not accept a body")


def _no_store_exception(exc: HTTPException) -> HTTPException:
    exc.headers = {**(exc.headers or {}), **_NO_STORE_HEADERS}
    return exc


@router.post("/connections", status_code=201)
def create_connection(
    body: connections.ConnectionCreateRequest,
    request: Request,
    response: Response,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    response.headers.update(_NO_STORE_HEADERS)
    try:
        return connections.create_connection_use_case(
            principal.tenant_id, body, request
        )
    except HTTPException as exc:
        raise _no_store_exception(exc) from None


@router.get("/connections/{connection_id}")
async def get_connection(
    connection_id: str,
    request: Request,
    principal: TenantPrincipal = Depends(_principal),
):
    await _require_empty_body(request)
    return connections.get_connection_use_case(
        principal.tenant_id, connection_id, request
    )


@router.get("/connections/{connection_id}/domain-verify")
async def get_domain_verify(
    connection_id: str,
    request: Request,
    principal: TenantPrincipal = Depends(_principal),
):
    await _require_empty_body(request)
    return connections.get_domain_verify_use_case(
        principal.tenant_id,
        connection_id,
    )


@router.post("/connections/{connection_id}/domain-verify")
async def upload_domain_verify(
    connection_id: str,
    request: Request,
    file: UploadFile = File(...),
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    return await connections.upload_domain_verify_use_case(
        principal.tenant_id,
        connection_id,
        file,
    )


@router.delete("/connections/{connection_id}/domain-verify")
async def delete_domain_verify(
    connection_id: str,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    await _require_empty_body(request)
    return connections.delete_domain_verify_use_case(
        principal.tenant_id,
        connection_id,
    )


@router.put("/connections/{connection_id}")
def update_connection(
    connection_id: str,
    body: connections.ConnectionUpdateRequest,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    return connections.update_connection_use_case(
        principal.tenant_id, connection_id, body, request
    )


@router.post("/connections/{connection_id}/disable")
async def disable_connection(
    connection_id: str,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    await _require_empty_body(request)
    return connections.disable_connection_use_case(
        principal.tenant_id, connection_id, request
    )


@router.delete("/connections/{connection_id}")
async def delete_connection(
    connection_id: str,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    await _require_empty_body(request)
    return connections.delete_connection_use_case(
        principal.tenant_id, connection_id, request
    )


@router.put("/connections/{connection_id}/credentials")
@router.post("/connections/{connection_id}/credentials/rotate")
def replace_connection_credentials(
    connection_id: str,
    body: connections.CredentialReplaceRequest,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    return connections.replace_connection_credentials_use_case(
        principal.tenant_id, connection_id, body, request
    )


@router.post("/connections/{connection_id}/tokens", status_code=201)
def issue_connection_token(
    connection_id: str,
    body: connections.TokenIssueRequest,
    request: Request,
    response: Response,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    response.headers.update(_NO_STORE_HEADERS)
    try:
        return connections.issue_connection_token_use_case(
            principal.tenant_id, connection_id, body, request
        )
    except HTTPException as exc:
        raise _no_store_exception(exc) from None


@router.post("/connections/{connection_id}/tokens/rotate", status_code=201)
def rotate_connection_token(
    connection_id: str,
    body: connections.TokenIssueRequest,
    request: Request,
    response: Response,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    response.headers.update(_NO_STORE_HEADERS)
    try:
        return connections.rotate_connection_token_use_case(
            principal.tenant_id, connection_id, body, request
        )
    except HTTPException as exc:
        raise _no_store_exception(exc) from None


@router.delete("/connections/{connection_id}/tokens/{token_id}")
async def revoke_connection_token(
    connection_id: str,
    token_id: str,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    await _require_empty_body(request)
    return connections.revoke_connection_token_use_case(
        principal.tenant_id, connection_id, token_id, request
    )


@router.get("/connections/{connection_id}/tools")
async def list_connection_tools(
    connection_id: str,
    request: Request,
    principal: TenantPrincipal = Depends(_principal),
):
    await _require_empty_body(request)
    return connections.list_connection_tools_use_case(
        principal.tenant_id, connection_id, request
    )


@router.put("/connections/{connection_id}/tools")
def update_connection_tools(
    connection_id: str,
    body: connections.ToolPoliciesRequest,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    return connections.update_connection_tools_use_case(
        principal.tenant_id, connection_id, body, request
    )


@router.post("/connections/{connection_id}/test")
async def test_connection(
    connection_id: str,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    await _require_empty_body(request)
    return await connections.test_connection_use_case(
        principal.tenant_id, connection_id, request
    )


@router.post("/connections/{connection_id}/sync")
async def sync_connection(
    connection_id: str,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    await _require_empty_body(request)
    return await connections.sync_connection_use_case(
        principal.tenant_id, connection_id, request
    )


@router.post("/connections/{connection_id}/specs/import", status_code=201)
def import_connection_spec(
    connection_id: str,
    body: connections.OpenApiImportRequest,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    return connections.import_connection_spec_use_case(
        principal.tenant_id, connection_id, body, request
    )


@router.post(
    "/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/validate"
)
async def validate_connection_spec(
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    await _require_empty_body(request)
    return connections.validate_connection_spec_use_case(
        principal.tenant_id, connection_id, spec_id, revision, request
    )


@router.delete(
    "/connections/{connection_id}/specs/{spec_id}/revisions/{revision}"
)
async def delete_connection_spec(
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    await _require_empty_body(request)
    return connections.delete_connection_spec_use_case(
        principal.tenant_id, connection_id, spec_id, revision, request
    )


@router.post(
    "/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/publish"
)
async def publish_connection_spec(
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    await _require_empty_body(request)
    return connections.publish_connection_spec_use_case(
        principal.tenant_id, connection_id, spec_id, revision, request
    )


@router.post(
    "/connections/{connection_id}/specs/{spec_id}/revisions/{revision}/activate"
)
async def activate_connection_spec(
    connection_id: str,
    spec_id: str,
    revision: int,
    request: Request,
    principal: TenantPrincipal = Depends(_mutation_principal),
):
    await _require_empty_body(request)
    return connections.activate_connection_spec_use_case(
        principal.tenant_id, connection_id, spec_id, revision, request
    )
