from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.mcp_services.models import IssuedServiceToken, McpService, ServiceToolBinding
from app.tenant_auth.models import TenantPrincipal
from app.tenant_auth import store as tenant_auth_store


RAW_TOKEN = "mcp_svc_raw-secret-value"


class FakeManager:
    def __init__(self):
        self.services = {
            "service-a": McpService(
                "service-a", "tenant-a", "Operations", "operations", "active", 1
            ),
            "service-b": McpService(
                "service-b", "tenant-b", "Foreign", "foreign", "active", 1
            ),
        }
        self.bindings = {
            "service-a": [
                ServiceToolBinding(
                    "binding-a",
                    "service-a",
                    "conn-a",
                    "users.get",
                    "wecom.users.get",
                    "active",
                    {},
                )
            ]
        }
        self.issued = IssuedServiceToken("token-a", RAW_TOKEN, "abc123")
        self.revoked = set()
        self.issue_calls = []

    def _owned(self, tenant_id, service_id):
        from app.mcp_services.store import ServiceOwnershipError

        item = self.services.get(service_id)
        if item is None or item.tenant_id != tenant_id:
            raise ServiceOwnershipError("not owned")
        return item

    def list_services(self, tenant_id):
        return [item for item in self.services.values() if item.tenant_id == tenant_id]

    def get_service(self, tenant_id, service_id):
        return self._owned(tenant_id, service_id)

    def create_service(self, tenant_id, display_name, service_key):
        item = McpService("created-service", tenant_id, display_name, service_key, "draft", 1)
        self.services[item.service_id] = item
        return item

    def update_status(self, tenant_id, service_id, status, expected_config_version):
        current = self._owned(tenant_id, service_id)
        updated = replace(
            current, status=status, config_version=expected_config_version + 1
        )
        self.services[service_id] = updated
        return updated

    def list_bindings(self, tenant_id, service_id):
        self._owned(tenant_id, service_id)
        return self.bindings.get(service_id, [])

    def replace_bindings(self, tenant_id, service_id, items, expected_config_version):
        self._owned(tenant_id, service_id)
        self.bindings[service_id] = list(items)
        return replace(
            self.services[service_id], config_version=expected_config_version + 1
        )

    def list_tokens(self, tenant_id, service_id):
        self._owned(tenant_id, service_id)
        return [
            SimpleNamespace(
                token_id=self.issued.token_id,
                prefix=self.issued.prefix,
                label="client-a",
                expires_at=None,
                revoked_at=None,
                last_used_at=None,
                created_at=datetime(2026, 7, 17),
            )
        ]

    def issue_token(self, tenant_id, service_id, label, expires_at=None):
        self._owned(tenant_id, service_id)
        self.issue_calls.append((tenant_id, service_id, label, expires_at))
        return self.issued

    def reveal_token(self, tenant_id, service_id, token_id):
        from app.mcp_services.store import TokenUnavailableError

        self._owned(tenant_id, service_id)
        if token_id != self.issued.token_id:
            raise TokenUnavailableError("unavailable")
        return self.issued.raw_value

    def revoke_token(self, tenant_id, service_id, token_id):
        self._owned(tenant_id, service_id)
        self.revoked.add(token_id)
        return True


def _client(monkeypatch, manager=None) -> tuple[TestClient, FakeManager]:
    from app.mcp_services import router as service_router

    fake = manager or FakeManager()
    monkeypatch.setattr(service_router, "manager", fake)
    service_router.reset_reveal_limiter()
    monkeypatch.setattr(
        tenant_auth_store,
        "resolve_session",
        lambda raw: (
            TenantPrincipal(principal_type="tenant", tenant_id="tenant-a")
            if raw == "tenant-session-a"
            else None
        ),
    )
    app = FastAPI()
    app.include_router(service_router.tenant_router)
    client = TestClient(app)
    client.cookies.set("wbg_tenant_session", "tenant-session-a", path="/tenant")
    return client, fake


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def test_service_manager_constructs_tenant_bound_draft_and_delegates(monkeypatch):
    from app.mcp_services import manager as manager_module

    captured = []
    monkeypatch.setattr(
        manager_module.store,
        "create_service",
        lambda service: captured.append(service) or service,
    )

    created = manager_module.ServiceManager().create_service(
        "tenant-a", "Operations", "operations"
    )

    assert created.tenant_id == "tenant-a"
    assert created.status == "draft"
    assert created.config_version == 1
    assert captured == [created]


def test_tenant_can_manage_only_own_services(monkeypatch):
    client, _ = _client(monkeypatch)

    own = client.get("/tenant/services")
    foreign = client.get("/tenant/services/service-b")

    assert own.status_code == 200
    assert [item["service_id"] for item in own.json()["items"]] == ["service-a"]
    assert foreign.status_code == 404
    assert "tenant-b" not in foreign.text


def test_tenant_mutations_use_principal_tenant_and_require_same_origin(monkeypatch):
    client, fake = _client(monkeypatch)
    body = {
        "display_name": "New service",
        "service_key": "new-service",
        "tenant_id": "tenant-b",
    }

    cross_site = client.post(
        "/tenant/services",
        headers={"Origin": "https://attacker.invalid"},
        json={"display_name": "New service", "service_key": "new-service"},
    )
    injected = client.post("/tenant/services", headers=_origin(), json=body)
    created = client.post(
        "/tenant/services",
        headers=_origin(),
        json={"display_name": "New service", "service_key": "new-service"},
    )

    assert cross_site.status_code == 403
    assert injected.status_code == 422
    assert created.status_code == 201
    assert fake.services["created-service"].tenant_id == "tenant-a"


def test_service_status_and_binding_management(monkeypatch):
    client, _ = _client(monkeypatch)

    bindings = client.put(
        "/tenant/services/service-a/tools",
        headers=_origin(),
        json={
            "expected_config_version": 1,
            "items": [
                {
                    "binding_id": "binding-b",
                    "connection_id": "conn-a",
                    "source_tool_key": "users.list",
                    "tool_alias": "wecom.users.list",
                    "binding_status": "active",
                    "policy": {},
                }
            ],
        },
    )
    status = client.patch(
        "/tenant/services/service-a",
        headers=_origin(),
        json={"status": "disabled", "expected_config_version": 2},
    )

    assert status.status_code == 200
    assert status.json()["service"]["status"] == "disabled"
    assert bindings.status_code == 200
    assert bindings.json()["service"]["config_version"] == 2


def test_list_never_returns_raw_token_but_reveal_does_with_no_store(monkeypatch):
    client, fake = _client(monkeypatch)

    listed = client.get("/tenant/services/service-a/tokens")
    revealed = client.post(
        f"/tenant/services/service-a/tokens/{fake.issued.token_id}/reveal",
        headers=_origin(),
    )

    assert listed.status_code == 200
    assert RAW_TOKEN not in repr(listed.json())
    assert "encrypted_token" not in repr(listed.json())
    assert revealed.json()["token"] == RAW_TOKEN
    assert revealed.headers["cache-control"] == "no-store"


def test_reveal_is_rate_limited_per_principal_and_token(monkeypatch):
    from app.mcp_services import router as service_router

    events = []
    monkeypatch.setattr(
        service_router, "write_event", lambda event: events.append(event) or True
    )
    client, fake = _client(monkeypatch)
    path = f"/tenant/services/service-a/tokens/{fake.issued.token_id}/reveal"

    for _ in range(10):
        assert client.post(path, headers=_origin()).status_code == 200
    response = client.post(path, headers=_origin())

    assert response.status_code == 429
    assert response.headers["cache-control"] == "no-store"
    assert RAW_TOKEN not in response.text
    assert events[-1].result_status == "denied"
    assert RAW_TOKEN not in repr(events)


def test_reveal_auth_and_same_origin_failures_are_no_store_and_audited(monkeypatch):
    from app.mcp_services import router as service_router

    events = []
    monkeypatch.setattr(service_router, "write_event", events.append)
    client, fake = _client(monkeypatch)
    path = f"/tenant/services/service-a/tokens/{fake.issued.token_id}/reveal"

    client.cookies.clear()
    unauthenticated = client.post(path, headers=_origin())
    client.cookies.set("wbg_tenant_session", "tenant-session-a", path="/tenant")
    cross_site = client.post(path, headers={"Origin": "https://attacker.invalid"})

    assert unauthenticated.status_code == 401
    assert cross_site.status_code == 403
    assert unauthenticated.headers["cache-control"] == "no-store"
    assert cross_site.headers["cache-control"] == "no-store"
    assert [event.result_status for event in events] == ["denied", "denied"]
    assert events[0].tenant_id == ""
    assert events[1].tenant_id == "tenant-a"
    for secret in (RAW_TOKEN, "encrypted_token", "token_hmac"):
        assert secret not in repr(events)


def test_reveal_rejection_does_not_audit_raw_looking_path_value(monkeypatch):
    from app.mcp_services import router as service_router

    events = []
    monkeypatch.setattr(service_router, "write_event", events.append)
    client, _ = _client(monkeypatch)
    client.cookies.clear()

    response = client.post(
        f"/tenant/services/service-a/tokens/{RAW_TOKEN}/reveal",
        headers=_origin(),
    )

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert RAW_TOKEN not in response.text
    assert RAW_TOKEN not in repr(events)
    assert json.loads(events[0].params_summary) == {"principal_type": "tenant"}


def test_reveal_wrong_service_and_token_share_safe_not_found_response(monkeypatch):
    from app.mcp_services import router as service_router

    events = []
    monkeypatch.setattr(service_router, "write_event", events.append)
    client, fake = _client(monkeypatch)

    wrong_service = client.post(
        f"/tenant/services/service-b/tokens/{fake.issued.token_id}/reveal",
        headers=_origin(),
    )
    wrong_token = client.post(
        "/tenant/services/service-a/tokens/token-b/reveal",
        headers=_origin(),
    )

    assert wrong_service.status_code == 404
    assert wrong_token.status_code == 404
    assert wrong_service.json() == wrong_token.json() == {"detail": "resource not found"}
    assert wrong_service.headers["cache-control"] == "no-store"
    assert wrong_token.headers["cache-control"] == "no-store"
    assert [event.result_status for event in events] == ["denied", "denied"]
    assert all(
        json.loads(event.params_summary) == {"principal_type": "tenant"}
        for event in events
    )


def test_reveal_audit_contains_only_safe_identifiers(monkeypatch):
    from app.mcp_services import router as service_router

    events = []
    monkeypatch.setattr(
        service_router, "write_event", lambda event: events.append(event) or True
    )
    client, fake = _client(monkeypatch)

    response = client.post(
        f"/tenant/services/service-a/tokens/{fake.issued.token_id}/reveal",
        headers={**_origin(), "X-Request-Id": "request-a"},
    )

    assert response.status_code == 200
    assert len(events) == 1
    assert events[0].tenant_id == "tenant-a"
    assert events[0].request_id == "request-a"
    assert "service-a" in events[0].params_summary
    assert "token-a" in events[0].params_summary
    for secret in (RAW_TOKEN, "encrypted_token", "token_hmac"):
        assert secret not in repr(events[0])


def test_disabled_service_token_recovery_routes_remain_available(monkeypatch):
    client, fake = _client(monkeypatch)
    fake.services["service-a"] = replace(fake.services["service-a"], status="disabled")

    listed = client.get("/tenant/services/service-a/tokens")
    revealed = client.post(
        f"/tenant/services/service-a/tokens/{fake.issued.token_id}/reveal",
        headers=_origin(),
    )
    revoked = client.delete(
        f"/tenant/services/service-a/tokens/{fake.issued.token_id}",
        headers=_origin(),
    )

    assert listed.status_code == 200
    assert revealed.status_code == 200
    assert revoked.status_code == 200


def test_token_issue_returns_raw_value_once_and_requires_same_origin(monkeypatch):
    client, _ = _client(monkeypatch)

    rejected = client.post(
        "/tenant/services/service-a/tokens", json={"label": "client-a"}
    )
    issued = client.post(
        "/tenant/services/service-a/tokens",
        headers=_origin(),
        json={"label": "client-a"},
    )

    assert rejected.status_code == 403
    assert issued.status_code == 201
    assert issued.json() == {
        "token_id": "token-a",
        "token": RAW_TOKEN,
        "prefix": "abc123",
    }
    assert issued.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("audit_result", [False, None, 1, "accepted", object()])
def test_reveal_requires_literal_true_success_audit(monkeypatch, audit_result):
    from app.mcp_services import router as service_router

    events = []
    monkeypatch.setattr(
        service_router,
        "write_event",
        lambda event: events.append(event) or audit_result,
    )
    client, fake = _client(monkeypatch)

    response = client.post(
        f"/tenant/services/service-a/tokens/{fake.issued.token_id}/reveal",
        headers=_origin(),
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "service operation failed"}
    assert response.headers["cache-control"] == "no-store"
    assert len(events) == 1
    assert events[0].result_status == "ok"
    for secret in (RAW_TOKEN, "token-a", "service-a", "abc123"):
        assert secret not in response.text


def test_reveal_success_audit_exception_is_generic_and_not_retried(monkeypatch):
    from app.mcp_services import router as service_router

    calls = []

    def reject(event):
        calls.append(event)
        raise RuntimeError(f"audit failed with {RAW_TOKEN}")

    monkeypatch.setattr(service_router, "write_event", reject)
    client, fake = _client(monkeypatch)

    response = client.post(
        f"/tenant/services/service-a/tokens/{fake.issued.token_id}/reveal",
        headers=_origin(),
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "service operation failed"}
    assert response.headers["cache-control"] == "no-store"
    assert len(calls) == 1
    assert RAW_TOKEN not in response.text


def test_denial_audit_failure_preserves_original_response(monkeypatch):
    from app.mcp_services import router as service_router

    def reject(_event):
        raise RuntimeError("sink unavailable")

    monkeypatch.setattr(service_router, "write_event", reject)
    client, fake = _client(monkeypatch)
    client.cookies.clear()

    response = client.post(
        f"/tenant/services/service-a/tokens/{fake.issued.token_id}/reveal",
        headers=_origin(),
    )

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "expires_at",
    [
        1,
        True,
        [],
        {},
        "2026-07-23",
        "2026-07-23 10:20:30Z",
        "2026-07-23T10:20:30",
        "2026-07-23T10:20:30.1Z",
        "2026-07-23T10:20:60Z",
        "2026-07-23T10:20:30+24:00",
    ],
)
def test_token_issue_rejects_non_strict_expiry(monkeypatch, expires_at):
    client, fake = _client(monkeypatch)

    response = client.post(
        "/tenant/services/service-a/tokens",
        headers=_origin(),
        json={"label": "client-a", "expires_at": expires_at},
    )

    assert response.status_code == 422
    assert fake.issue_calls == []


def test_token_issue_normalizes_offset_expiry_and_accepts_null(monkeypatch):
    client, fake = _client(monkeypatch)

    offset = client.post(
        "/tenant/services/service-a/tokens",
        headers=_origin(),
        json={"expires_at": "2026-07-23T10:20:30+08:00"},
    )
    no_expiry = client.post(
        "/tenant/services/service-a/tokens",
        headers=_origin(),
        json={"expires_at": None},
    )

    assert offset.status_code == no_expiry.status_code == 201
    assert fake.issue_calls[0][3] == datetime(2026, 7, 23, 2, 20, 30)
    assert fake.issue_calls[1][3] is None


def test_token_metadata_timestamps_are_canonical_utc(monkeypatch):
    client, fake = _client(monkeypatch)
    fake.list_tokens = lambda _tenant_id, _service_id: [
        SimpleNamespace(
            token_id="token-a",
            prefix="abc123",
            label="client-a",
            expires_at=datetime(2026, 7, 23, 2, 20, 30),
            revoked_at=None,
            last_used_at=datetime(2026, 7, 22, 1, 2, 3),
            created_at=datetime(2026, 7, 17),
        )
    ]

    item = client.get("/tenant/services/service-a/tokens").json()["items"][0]

    assert item["expires_at"] == "2026-07-23T02:20:30Z"
    assert item["last_used_at"] == "2026-07-22T01:02:03Z"
    assert item["created_at"] == "2026-07-17T00:00:00Z"
