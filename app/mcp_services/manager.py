from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from . import store
from .models import (
    IssuedServiceToken,
    McpService,
    ServiceTokenMetadata,
    ServiceToolBinding,
)


class ServiceManager:
    def list_services(self, tenant_id: str) -> list[McpService]:
        return store.list_services(tenant_id)

    def get_service(self, tenant_id: str, service_id: str) -> McpService:
        service = store.get_service(service_id, tenant_id)
        if service is None:
            raise store.ServiceOwnershipError("service is not owned by tenant")
        return service

    def create_service(
        self, tenant_id: str, display_name: str, service_key: str
    ) -> McpService:
        return store.create_service(
            McpService(
                service_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                display_name=display_name,
                service_key=service_key,
                status="draft",
                config_version=1,
            )
        )

    def update_status(
        self,
        tenant_id: str,
        service_id: str,
        status: str,
        expected_config_version: int,
    ) -> McpService:
        return store.update_service_status(
            service_id,
            tenant_id,
            status,
            expected_config_version=expected_config_version,
        )

    def list_bindings(
        self, tenant_id: str, service_id: str
    ) -> list[ServiceToolBinding]:
        return store.list_bindings(service_id, tenant_id)

    def replace_bindings(
        self,
        tenant_id: str,
        service_id: str,
        items: Sequence[ServiceToolBinding],
        expected_config_version: int,
    ) -> McpService:
        return store.replace_bindings(
            service_id,
            tenant_id,
            items,
            expected_config_version,
        )

    def list_tokens(
        self, tenant_id: str, service_id: str
    ) -> list[ServiceTokenMetadata]:
        return store.list_tokens(service_id, tenant_id)

    def issue_token(
        self,
        tenant_id: str,
        service_id: str,
        label: str,
        expires_at: datetime | None = None,
    ) -> IssuedServiceToken:
        return store.issue_token(
            service_id, tenant_id, label=label, expires_at=expires_at
        )

    def reveal_token(self, tenant_id: str, service_id: str, token_id: str) -> str:
        return store.reveal_token(service_id, tenant_id, token_id)

    def revoke_token(self, tenant_id: str, service_id: str, token_id: str) -> bool:
        return store.revoke_token(service_id, tenant_id, token_id)
