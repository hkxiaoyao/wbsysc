# Multi-Entry MCP Service Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each tenant create multiple MCP service endpoints that explicitly project tools from multiple connection instances and own multiple viewable service Tokens.

**Architecture:** Add an `mcp_services` domain for service, binding, and Token persistence. A service-aware MCP gateway resolves a service Token plus path, maps an external alias to one source connection tool, then delegates execution to the existing `ConnectorRuntime`; connection and legacy gateways stay unchanged.

**Tech Stack:** Python 3.11+, FastAPI/Starlette, MCP SDK low-level Server, SQLAlchemy 2, MySQL 5.7, cryptography, pytest.

**Depends on:** `2026-07-17-tenant-authentication.md`.
**Blocks:** OpenAPI orchestration integration and the tenant console.
**Hot-file ownership:** This plan is the sole writer of `app/main.py`, `app/db.py`, `app/mcp_log_models.py`, `app/mcp_log_store.py`, and `tests/test_admin_security.py` until its commits are merged.

## Global Constraints

- A service binding is authorized only when service, binding, connection policy, and write gates all allow it.
- A service may tighten but never loosen connection-level policy.
- A binding must reference a connection owned by the same tenant, checked transactionally.
- `tool_alias` is materialized and unique per service; changing `connection_alias` never rewrites it.
- Service Token authentication uses HMAC; reveal uses a separate `MCP_TOKEN_PLAINTEXT_KEY`.
- Platform admins and the owning tenant may reveal active service Tokens without password re-entry.
- Existing `/mcp` and `/mcp/{connection_id}` behavior and HMAC-only Tokens remain unchanged.
- New service routing is protected by a production feature flag.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `app/mcp_services/models.py` | Service, binding, service context, and Token contracts. |
| `app/mcp_services/crypto.py` | Independent Token plaintext encryption boundary. |
| `app/mcp_services/store.py` | Service/binding/Token CRUD, tenant ownership, and backfill. |
| `app/mcp_services/manager.py` | Policy composition and management use cases. |
| `app/mcp_services/router.py` | Tenant and admin management endpoints. |
| `app/mcp_service_gateway.py` | Service-scoped MCP protocol gateway. |
| `sql/008_mcp_service.sql` | Service, binding, Token, connection alias, and log dimensions. |
| `tests/test_mcp_service_*.py` | Persistence, API, runtime, isolation, and migration tests. |

### Task 1: Service Persistence and Alias Invariants

**Files:**
- Create: `app/mcp_services/__init__.py`
- Create: `app/mcp_services/models.py`
- Create: `app/mcp_services/store.py`
- Create: `sql/008_mcp_service.sql`
- Modify: `app/connections/models.py`
- Modify: `app/connections/store.py`
- Modify: `app/db.py`
- Test: `tests/test_mcp_service_store.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces `McpService`, `ServiceToolBinding`, `create_service`, `get_service`, `list_services`, `replace_bindings`, and `list_bindings`.
- Adds `connection_alias: str` to `ConnectionRecord` and tenant-unique persistence.

- [ ] **Step 1: Write failing ownership, uniqueness, and snapshot tests.**

```python
def test_replace_bindings_rejects_cross_tenant_connection():
    with pytest.raises(ServiceOwnershipError):
        store.replace_bindings("service-a", "tenant-a", [binding(connection_id="tenant-b-conn")])

def test_connection_alias_change_does_not_rewrite_materialized_tool_alias():
    store.replace_bindings("service-a", "tenant-a", [binding(tool_alias="hq_wecom__get_users")])
    connection_store.update_connection_alias("conn-a", "tenant-a", "renamed")
    assert store.list_bindings("service-a", "tenant-a")[0].tool_alias == "hq_wecom__get_users"
```

- [ ] **Step 2: Run the focused store tests and verify missing imports.**

Run: `python -m pytest tests/test_mcp_service_store.py -q`
Expected: FAIL because `app.mcp_services` is absent.

- [ ] **Step 3: Add immutable contracts and transactional store signatures.**

```python
@dataclass(frozen=True)
class McpService:
    service_id: str
    tenant_id: str
    display_name: str
    service_key: str
    status: Literal["draft", "active", "disabled"]
    config_version: int

@dataclass(frozen=True)
class ServiceToolBinding:
    binding_id: str
    service_id: str
    connection_id: str
    source_tool_key: str
    tool_alias: str
    binding_status: Literal["active", "disabled", "broken"]
    policy: Mapping[str, Any]

def replace_bindings(service_id: str, tenant_id: str,
                     bindings: Sequence[ServiceToolBinding],
                     expected_config_version: int) -> McpService:
    # SELECT service and every connection FOR UPDATE, verify one tenant,
    # replace rows, then increment config_version in the same transaction.
```

Use the existing connector identifier regex for `connection_alias`, `source_tool_key`, and `tool_alias`. Persist the alias rather than recomputing it in list or call paths. `binding.source_tool_key`, `connection_tool_policy.tool_name`, and `ToolSpec.tool_key` are the same stable identity; never query policy by `mcp_name`.

- [ ] **Step 4: Add matching MySQL 5.7 DDL and migration order.**

Create the core tables with exact constraints:

```sql
CREATE TABLE IF NOT EXISTS mcp_service (
  service_id VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(64) NOT NULL,
  display_name VARCHAR(128) NOT NULL,
  service_key VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'draft',
  config_version INT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (service_id),
  UNIQUE KEY uk_mcp_service_tenant_key (tenant_id, service_key),
  KEY idx_mcp_service_tenant_status (tenant_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS mcp_service_tool_binding (
  binding_id VARCHAR(64) NOT NULL,
  service_id VARCHAR(64) NOT NULL,
  connection_id VARCHAR(64) NOT NULL,
  source_tool_key VARCHAR(128) NOT NULL,
  tool_alias VARCHAR(128) NOT NULL,
  binding_status VARCHAR(16) NOT NULL DEFAULT 'active',
  policy_json TEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (binding_id),
  UNIQUE KEY uk_service_tool_alias (service_id, tool_alias),
  UNIQUE KEY uk_service_source_tool (service_id, connection_id, source_tool_key),
  KEY idx_service_binding_connection (connection_id, service_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Add `connection_instance.connection_alias VARCHAR(64)`, backfill it deterministically, then create `UNIQUE(tenant_id, connection_alias)`. Repair missing columns/indexes through `information_schema` checks. Only the domain validator can transition a binding to `broken`, and a user must explicitly restore it to `active`. Run `ensure_mcp_service_tables()` after connection tables.

Run: `python -m pytest tests/test_mcp_service_store.py tests/test_migrations.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the service persistence unit.**

```bash
git add app/mcp_services app/connections app/db.py sql/008_mcp_service.sql tests/test_mcp_service_store.py tests/test_migrations.py
git commit -m "feat: add MCP service persistence"
```

### Task 2: Viewable Service Tokens with Split Cryptographic Duties

**Files:**
- Create: `app/mcp_services/crypto.py`
- Modify: `app/mcp_services/models.py`
- Modify: `app/mcp_services/store.py`
- Modify: `app/config.py`
- Modify: `.env.example`
- Modify: `.env.prod.example`
- Modify: `sql/008_mcp_service.sql`
- Test: `tests/test_mcp_service_tokens.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces `IssuedServiceToken`, `issue_token`, `resolve_token`, `reveal_token`, and `revoke_token`.
- Adds `Settings.mcp_token_plaintext_key` and `Settings.mcp_service_enabled`.

- [ ] **Step 1: Write failing encryption, path-binding, and revoke tests.**

```python
def test_service_token_uses_hmac_for_auth_and_ciphertext_for_reveal(fake_db):
    issued = store.issue_token("service-a", "tenant-a", label="client-a")
    assert store.resolve_token(issued.raw_value, "service-a").service_id == "service-a"
    assert store.resolve_token(issued.raw_value, "service-b") is None
    assert store.reveal_token("service-a", "tenant-a", issued.token_id) == issued.raw_value
    assert issued.raw_value not in repr(fake_db.bound_parameters)

def test_revoked_token_cannot_authenticate_or_reveal():
    issued = store.issue_token("service-a", "tenant-a", label="client-a")
    store.revoke_token("service-a", "tenant-a", issued.token_id)
    assert store.resolve_token(issued.raw_value, "service-a") is None
    with pytest.raises(TokenUnavailableError):
        store.reveal_token("service-a", "tenant-a", issued.token_id)
```

- [ ] **Step 2: Run tests and verify the missing Token API.**

Run: `python -m pytest tests/test_mcp_service_tokens.py tests/test_config.py -q`
Expected: FAIL on missing settings and service Token functions.

- [ ] **Step 3: Implement independent plaintext encryption.**

```python
def _fernet() -> Fernet:
    raw = get_settings().mcp_token_plaintext_key.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)

def encrypt_token(raw_token: str) -> bytes:
    return _fernet().encrypt(raw_token.encode("utf-8"))

def decrypt_token(ciphertext: bytes) -> str:
    return _fernet().decrypt(ciphertext).decode("utf-8")
```

Production validation requires all three keys to be non-example values of at least 32 UTF-8 bytes and pairwise distinct. Authentication queries compare `token_hmac`; only reveal queries select and decrypt `encrypted_token`.

- [ ] **Step 4: Add the Token table and verify no raw value reaches persistence.**

```sql
CREATE TABLE IF NOT EXISTS mcp_service_token (
  token_id VARCHAR(64) NOT NULL,
  service_id VARCHAR(64) NOT NULL,
  token_hmac CHAR(64) NOT NULL,
  encrypted_token VARBINARY(4096) NULL,
  token_prefix VARCHAR(32) NOT NULL,
  token_label VARCHAR(128) NOT NULL DEFAULT '',
  expires_at DATETIME NULL,
  revoked_at DATETIME NULL,
  last_used_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (token_id),
  UNIQUE KEY uk_mcp_service_token_hmac (token_hmac),
  KEY idx_mcp_service_token_service (service_id, revoked_at, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

On revoke, set `revoked_at=UTC_TIMESTAMP(), encrypted_token=NULL` atomically.

- [ ] **Step 5: Run and commit.**

Run: `python -m pytest tests/test_mcp_service_tokens.py tests/test_config.py -q`
Expected: PASS.

```bash
git add app/mcp_services app/config.py .env.example .env.prod.example sql/008_mcp_service.sql tests/test_mcp_service_tokens.py tests/test_config.py
git commit -m "feat: add viewable MCP service tokens"
```

### Task 3: Tenant/Admin Service Management APIs

**Files:**
- Create: `app/mcp_services/manager.py`
- Create: `app/mcp_services/router.py`
- Modify: `app/main.py`
- Test: `tests/test_mcp_service_api.py`
- Test: `tests/test_admin_security.py`

**Interfaces:**
- Produces tenant routes under `/tenant/services` and admin routes under `/admin/tenants/{tenant_id}/services`.
- Consumes `TenantPrincipal` from the authentication plan and existing admin `_require_auth`.

- [ ] **Step 1: Write failing tenant-boundary and reveal API tests.**

```python
def test_tenant_can_manage_only_own_services(client, tenant_a_cookie):
    own = client.get("/tenant/services", cookies=tenant_a_cookie)
    foreign = client.get("/tenant/services/service-b", cookies=tenant_a_cookie)
    assert own.status_code == 200
    assert foreign.status_code == 404

def test_list_never_returns_raw_token_but_reveal_does(client, tenant_a_cookie, issued):
    listed = client.get(f"/tenant/services/service-a/tokens", cookies=tenant_a_cookie).json()
    assert issued.raw_value not in repr(listed)
    revealed = client.post(f"/tenant/services/service-a/tokens/{issued.token_id}/reveal",
                           cookies=tenant_a_cookie).json()
    assert revealed["token"] == issued.raw_value

def test_reveal_is_rate_limited_per_principal_and_token(client, tenant_a_cookie, issued):
    for _ in range(10):
        client.post(f"/tenant/services/service-a/tokens/{issued.token_id}/reveal",
                    cookies=tenant_a_cookie)
    response = client.post(f"/tenant/services/service-a/tokens/{issued.token_id}/reveal",
                           cookies=tenant_a_cookie)
    assert response.status_code == 429
```

- [ ] **Step 2: Run API tests and verify routes are absent.**

Run: `python -m pytest tests/test_mcp_service_api.py tests/test_admin_security.py -q`
Expected: FAIL with missing routes.

- [ ] **Step 3: Implement one manager with two authorization adapters.**

```python
class ServiceManager:
    def list_services(self, tenant_id: str) -> list[McpService]:
        return store.list_services(tenant_id)

    def create_service(self, tenant_id: str, body: ServiceCreate) -> McpService:
        return store.create_service(tenant_id, body.display_name, body.service_key)

    def replace_tools(self, tenant_id: str, service_id: str,
                      body: BindingReplace) -> McpService:
        return store.replace_bindings(service_id, tenant_id, body.items,
                                      body.expected_config_version)

    def reveal_token(self, tenant_id: str, service_id: str, token_id: str) -> str:
        return store.reveal_token(service_id, tenant_id, token_id)

@tenant_router.post("/services/{service_id}/tokens/{token_id}/reveal")
def reveal_tenant_token(service_id: str, token_id: str,
                        principal: TenantPrincipal = Depends(require_tenant_principal)):
    audit_reveal(principal.tenant_id, service_id, token_id, "tenant")
    return {"token": manager.reveal_token(principal.tenant_id, service_id, token_id)}
```

Reveal uses a bounded limiter keyed by `(principal_type, principal_tenant_or_admin, token_id)` with ten requests per minute, returns `Cache-Control: no-store`, and audits only principal type, tenant ID, service ID, Token ID, result, request ID, and client IP. Admin routes pass the path tenant after admin authentication; tenant routes never accept a tenant ID.

Service status transitions are `draft -> active|disabled`, `active -> disabled`, and `disabled -> draft|active`. Binding edits are allowed in draft and active states and always increment `config_version`; disabled services reject calls. Token issue requires an active service, while list/revoke/reveal remain available for disabled services to support recovery.

- [ ] **Step 4: Run service API and security regressions.**

Run: `python -m pytest tests/test_mcp_service_api.py tests/test_admin_security.py tests/test_tenant_auth_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit management APIs.**

```bash
git add app/mcp_services app/main.py tests/test_mcp_service_api.py tests/test_admin_security.py
git commit -m "feat: manage tenant MCP services"
```

### Task 4: Service-Scoped MCP Gateway

**Files:**
- Create: `app/mcp_service_gateway.py`
- Modify: `app/main.py`
- Modify: `app/connectors/runtime.py`
- Modify: `app/mcp_log_models.py`
- Modify: `app/mcp_log_store.py`
- Modify: `app/mcp_logs_admin.py`
- Modify: `sql/008_mcp_service.sql`
- Test: `tests/test_mcp_service_gateway.py`
- Test: `tests/test_mcp_connection_isolation.py`
- Test: `tests/test_mcp_log_store.py`

**Interfaces:**
- Produces `ServiceResolver.resolve(service_id, raw_token) -> ServiceContext | None`.
- Produces `ServiceMcpGateway` ASGI app with cache key `(service_id, config_version)`.
- Consumes `ConnectorRuntime.list_enabled_tools(context)` and `ConnectorRuntime.execute(context, source_tool_key, args)`.

- [ ] **Step 1: Write failing projection and policy-AND tests.**

```python
async def test_service_projects_aliases_from_multiple_connections():
    listed = await gateway.list_tools(service_ctx("service-a"))
    assert [tool.name for tool in listed] == ["hq_wecom__get_users", "erp__get_orders"]

async def test_service_binding_cannot_reenable_connection_disabled_tool():
    connection_policy.disable("conn-a", "users.get")
    assert "hq_wecom__get_users" not in names(await gateway.list_tools(service_ctx("service-a")))
    with pytest.raises(ToolDisabledError):
        await gateway.call_tool(service_ctx("service-a"), "hq_wecom__get_users", {})

def test_service_route_never_enters_connection_gateway(client, monkeypatch):
    entered = []
    monkeypatch.setattr(connection_gateway, "__call__", lambda *args: entered.append(True))
    client.post("/mcp/service/service-a", headers={"Authorization": "Bearer bad"})
    assert entered == []
```

- [ ] **Step 2: Run gateway tests and verify missing service gateway.**

Run: `python -m pytest tests/test_mcp_service_gateway.py tests/test_mcp_connection_isolation.py -q`
Expected: FAIL because `ServiceMcpGateway` is absent.

- [ ] **Step 3: Implement binding projection and exact connection context loading.**

```python
@dataclass(frozen=True)
class ProjectedTool:
    alias: str
    connection_id: str
    source_tool_key: str
    spec: ToolSpec

async def call_projected_tool(self, service: ServiceContext, alias: str,
                              args: dict[str, Any]) -> ExecutionResult:
    binding = self._bindings.resolve_enabled(service.service_id, alias)
    connection = self._connections.require_active(binding.connection_id, service.tenant_id)
    context = self._contexts.build(connection)
    source = self._runtime.require_enabled_tool(context, binding.source_tool_key)
    self._service_policy.assert_not_looser(binding.policy, source)
    return await self._runtime.execute(context, binding.source_tool_key, args)
```

Use alias only for MCP protocol exposure; execute only the stable source key. Apply rate limits at both service alias and source connection/tool scopes.

- [ ] **Step 4: Mount the service route and add service log dimensions.**

Mount `/mcp/service/{service_id}` before `/mcp/{connection_id}` and only when `MCP_SERVICE_ENABLED=true`; then mount the connection route and finally legacy `/mcp`. Replace the generic slash condition with explicit recognition of exactly `/mcp`, `/mcp/{connection_id}`, and `/mcp/service/{service_id}`. `/mcp/service` is not a valid connection route, and connection creation/backfill rejects the reserved connection ID `service`. Add a regression test proving service requests never enter `ConnectionMcpGateway`.

Add nullable `service_id` and `tool_alias` columns/indexes to logs; authentication failures may record a service ID only after successful server-side resolution. Tenant/admin log filters accept `service_id`, `tool_alias`, `connection_id`, and `source_tool_key` (stored in the existing `tool_key` column).

Run: `python -m pytest tests/test_mcp_service_gateway.py tests/test_mcp_connection_isolation.py tests/test_mcp_log_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the service runtime.**

```bash
git add app/mcp_service_gateway.py app/main.py app/connectors/runtime.py app/mcp_log_models.py app/mcp_log_store.py app/mcp_logs_admin.py sql/008_mcp_service.sql tests/test_mcp_service_gateway.py tests/test_mcp_connection_isolation.py tests/test_mcp_log_store.py
git commit -m "feat: add multi-connection MCP service gateway"
```

### Task 5: Default-Service Backfill and Cache Invalidation

**Files:**
- Modify: `app/mcp_services/store.py`
- Modify: `app/connections/store.py`
- Modify: `app/main.py`
- Test: `tests/test_mcp_service_migration.py`
- Test: `tests/test_mcp_service_gateway.py`

**Interfaces:**
- Produces `migrate_default_services(registry: ConnectorRegistry, enabled: bool) -> int` and `invalidate_services_for_connection(connection_id)`.

- [ ] **Step 1: Write failing idempotency and invalidation tests.**

```python
def test_default_service_backfill_is_idempotent_and_does_not_copy_connection_tokens():
    assert store.migrate_default_services(enabled=True) == 1
    assert store.migrate_default_services(enabled=True) == 0
    assert store.list_service_tokens(default_service_id("conn-a"), "tenant-a") == []

def test_connection_policy_change_invalidates_only_referencing_services():
    connection_store.set_tool_policy("conn-a", "users.get", enabled=False)
    assert cache.invalidated == {"service-a", "service-b"}
```

- [ ] **Step 2: Run tests and verify missing migration behavior.**

Run: `python -m pytest tests/test_mcp_service_migration.py tests/test_mcp_service_gateway.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement deterministic backfill and reference-based invalidation.**

```python
def default_service_id(connection_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"wbsysc:default-service:{connection_id}"))

def invalidate_services_for_connection(connection_id: str) -> None:
    service_ids = store.list_service_ids_for_connection(connection_id)
    for service_id in service_ids:
        service_cache.invalidate(service_id)
```

Write the migration watermark only after all selected connection transactions commit. Do not copy `connection_token` rows. If exact invalidation raises, invalidate all services for the owning tenant.

Run default-service backfill from application lifespan only after connection migration and trusted connector registration complete, passing the validated registry so each default service binds the connection's currently enabled `ToolSpec.tool_key` values. Do not run this binding backfill inside `db.run_startup_migrations()`, where the connector registry is not a database dependency.

- [ ] **Step 4: Run migration, gateway, and legacy regression suites.**

Run: `python -m pytest tests/test_mcp_service_migration.py tests/test_mcp_service_gateway.py tests/test_mcp_gateway.py tests/test_connection_migration_e2e.py -q`
Expected: PASS.

- [ ] **Step 5: Commit compatibility migration behavior.**

```bash
git add app/mcp_services/store.py app/connections/store.py app/main.py tests/test_mcp_service_migration.py tests/test_mcp_service_gateway.py
git commit -m "feat: backfill compatible MCP services"
```

### Task 6: Reference-Aware Delete and Revision Lifecycle Guards

**Files:**
- Modify: `app/mcp_services/store.py`
- Modify: `app/connections/store.py`
- Modify: `app/admin_connections.py`
- Modify: `app/mcp_services/router.py`
- Test: `tests/test_mcp_service_lifecycle.py`

**Interfaces:**
- Produces `list_service_references(connection_id, tenant_id) -> list[McpService]`.
- Produces `assert_connection_deletable` and `assert_revision_deletable` domain guards.

- [ ] **Step 1: Write failing connection and revision reference tests.**

```python
def test_connection_with_service_bindings_cannot_be_deleted(client, admin_headers):
    response = client.delete("/admin/tenants/tenant-a/connections/conn-a", headers=admin_headers)
    assert response.status_code == 409
    assert response.json()["affected_services"] == ["service-a"]

def test_published_revision_used_by_connection_or_binding_cannot_be_deleted():
    with pytest.raises(ResourceReferencedError):
        store.delete_declarative_revision("spec-a", 2, "tenant-a", "conn-a")
```

- [ ] **Step 2: Run lifecycle tests and verify missing guards.**

Run: `python -m pytest tests/test_mcp_service_lifecycle.py -q`
Expected: FAIL because delete guards are absent.

- [ ] **Step 3: Implement reference queries and fail-closed guards.**

```python
def assert_connection_deletable(connection_id: str, tenant_id: str) -> None:
    references = list_service_references(connection_id, tenant_id)
    if references:
        raise ResourceReferencedError([item.service_id for item in references])

def assert_revision_deletable(spec_id: str, revision: int,
                               tenant_id: str, connection_id: str) -> None:
    if revision_is_active_or_service_bound(spec_id, revision, tenant_id, connection_id):
        raise ResourceReferencedError([connection_id])
```

Return 409 with safe service IDs and display names. Do not cascade-delete bindings, services, Tokens, or logs. Connection disable remains allowed and immediately hides tools from referencing services.

- [ ] **Step 4: Run lifecycle and connection API regressions.**

Run: `python -m pytest tests/test_mcp_service_lifecycle.py tests/test_admin_connections.py tests/test_mcp_service_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit lifecycle guards.**

```bash
git add app/mcp_services/store.py app/connections/store.py app/admin_connections.py app/mcp_services/router.py tests/test_mcp_service_lifecycle.py
git commit -m "feat: guard referenced MCP resources"
```
