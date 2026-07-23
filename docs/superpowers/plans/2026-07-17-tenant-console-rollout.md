# Tenant Console and Production Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the tenant-facing console, service and composite-tool workbenches, scoped logs, admin controls, and a reversible production rollout for the new backend capabilities.

**Architecture:** Serve one Vite build at separate admin and tenant paths, with distinct API clients and shells. Reuse connection/log presentation through explicit scope adapters, add a dedicated MCP service page, extend the declarative wizard with a safe step builder, and deploy migrations 007–008 behind feature flags.

**Tech Stack:** React 18, Ant Design 5, Axios, Vite 5, Node test runner, FastAPI, pytest, Docker Compose, MySQL 5.7.

**Depends on:** `2026-07-17-tenant-authentication.md`, `2026-07-17-mcp-service-runtime.md`, and `2026-07-17-openapi-tool-orchestration.md`, in that order.
**Blocks:** Production rollout only.
**Hot-file ownership:** This plan becomes the sole writer of `app/main.py`, `app/admin_connections.py`, `admin-ui/src/App.jsx`, shared connection/log pages, deployment scripts, and operations docs after the three backend plans are merged.

## Global Constraints

- Tenant UI never accepts or persists a selectable tenant ID; its scope comes from the tenant session.
- Tenant session values stay in HttpOnly cookies and are never stored in localStorage.
- Connector creation uses cards; users never type `connector_key`.
- Token lists never include raw values; issue and reveal responses may show and copy them.
- Revealed Tokens are not written to URLs, localStorage, console logs, analytics, or error reports.
- Existing admin workflows remain available during the rollout.
- Deploy migrations in order 004, 005, 006, 007, 008 and retain a feature-flag rollback.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `admin-ui/src/tenantApi.js` | Cookie-only `/tenant/**` Axios client. |
| `admin-ui/src/TenantApp.jsx` | Tenant login, shell, navigation, and route state. |
| `admin-ui/src/pages/TenantLogin.jsx` | Tenant ID and password form. |
| `admin-ui/src/pages/Services.jsx` | MCP service list, editor, tool bindings, and Tokens. |
| `admin-ui/src/pages/ServiceTokenModal.jsx` | Issue/reveal/copy no-store Token UI. |
| `admin-ui/src/pages/DeclarativeToolBuilder.jsx` | Safe sequential multi-operation builder. |
| `app/tenant_console.py` | Tenant overview and strictly scoped log endpoints. |
| `deploy/server_deploy.sh` | Ordered 007–008 migration and feature-flag deployment. |

### Task 1: Tenant Shell and Cookie-Only Login

**Files:**
- Create: `admin-ui/src/tenantApi.js`
- Create: `admin-ui/src/TenantApp.jsx`
- Create: `admin-ui/src/pages/TenantLogin.jsx`
- Create: `admin-ui/src/pages/tenantAppView.js`
- Create: `admin-ui/src/pages/tenantAppView.test.js`
- Modify: `admin-ui/src/main.jsx`
- Modify: `admin-ui/vite.config.js`
- Modify: `app/main.py`
- Modify: `admin-ui/src/index.css`

**Interfaces:**
- `main.jsx` renders `TenantApp` when pathname starts with `/tenant/ui`, otherwise `App`.
- Tenant API sends cookies only and does not use admin localStorage tokens.

- [ ] **Step 1: Write failing tenant location and navigation tests.**

```javascript
test('tenant location allows only tenant views', () => {
  assert.deepEqual(parseTenantLocation('?view=services'), { view: 'services' })
  assert.deepEqual(parseTenantLocation('?view=tenants'), { view: 'overview' })
})

test('tenant URLs never serialize tenant_id', () => {
  assert.equal(tenantUrl('connections').includes('tenant_id'), false)
})
```

- [ ] **Step 2: Run the Node test and verify missing module failure.**

Run: `node --test admin-ui/src/pages/tenantAppView.test.js`
Expected: FAIL because `tenantAppView.js` is absent.

- [ ] **Step 3: Implement path selection, cookie-only API, and login form.**

```javascript
export const tenantApi = axios.create({ baseURL: '/tenant', withCredentials: true })

tenantApi.interceptors.response.use(
  response => response,
  error => Promise.reject(error),
)

const root = createRoot(document.getElementById('root'))
root.render(window.location.pathname.startsWith('/tenant/ui') ? <TenantApp /> : <App />)
```

`TenantLogin` posts `{tenant_id, password}` to `/tenant/login`, clears the password field in `finally`, and never stores a returned session value. `TenantApp` verifies `/tenant/session` and exposes only overview, connections, services, logs, and account settings.

Keep the existing Vite `base: '/admin/ui/'` so one shared build emits stable absolute asset URLs. Mount the same built `dist` directory at both `/admin/ui` and `/tenant/ui` in `app/main.py`; HTML loaded from `/tenant/ui/` therefore loads its hashed assets from the still-mounted `/admin/ui/assets/**` path. Add `/tenant` to the Vite development proxy beside `/admin`. Add `/tenant`, `/tenant/`, and `/tenant/index.html` redirects to `/tenant/ui/`. A production test must request both `/admin/ui/` and `/tenant/ui/` and assert the tenant HTML's `/admin/ui/assets/**` JS asset returns 200.

- [ ] **Step 4: Run logic tests and production build.**

Run: `node --test admin-ui/src/pages/tenantAppView.test.js`
Expected: PASS.
Run: `npm --prefix admin-ui run build`
Expected: Vite build succeeds.

- [ ] **Step 5: Commit the tenant shell.**

```bash
git add admin-ui/src/main.jsx admin-ui/src/tenantApi.js admin-ui/src/TenantApp.jsx admin-ui/src/pages/TenantLogin.jsx admin-ui/src/pages/tenantAppView.js admin-ui/src/pages/tenantAppView.test.js admin-ui/src/index.css admin-ui/vite.config.js app/main.py
git commit -m "feat: add tenant management console shell"
```

### Task 2: Scoped Connections, Overview, and Logs

**Files:**
- Create: `app/tenant_console.py`
- Modify: `app/main.py`
- Modify: `app/mcp_logs_admin.py`
- Modify: `admin-ui/src/pages/Connections.jsx`
- Modify: `admin-ui/src/pages/connectionView.js`
- Modify: `admin-ui/src/pages/connectionView.test.js`
- Modify: `admin-ui/src/pages/McpLogs.jsx`
- Modify: `admin-ui/src/pages/mcpLogsView.js`
- Modify: `admin-ui/src/pages/mcpLogsView.test.js`
- Modify: `admin-ui/src/TenantApp.jsx`
- Test: `tests/test_tenant_console.py`

**Interfaces:**
- Adds `/tenant/overview`, `/tenant/connections`, and `/tenant/mcp-logs` adapters.
- Adds `scope="admin" | "tenant"` and `apiClient` props to shared pages.

- [ ] **Step 1: Write failing server isolation and frontend endpoint tests.**

```python
def test_tenant_logs_force_session_tenant_even_when_query_contains_other_tenant(client, tenant_a_cookie):
    response = client.get("/tenant/mcp-logs?tenant_id=tenant-b", cookies=tenant_a_cookie)
    assert response.status_code == 422
    assert log_store.last_filters.tenant_id != "tenant-b"
```

```javascript
test('tenant connection collection never uses admin tenant route', () => {
  assert.equal(connectionCollectionEndpoint('tenant', ''), '/tenant/connections')
})
```

- [ ] **Step 2: Run focused Python and Node tests.**

Run: `python -m pytest tests/test_tenant_console.py -q`
Expected: FAIL with missing router.
Run: `node --test admin-ui/src/pages/connectionView.test.js admin-ui/src/pages/mcpLogsView.test.js`
Expected: FAIL on missing scope helpers.

- [ ] **Step 3: Implement server-owned scope adapters.**

```python
@router.get("/mcp-logs")
def tenant_logs(request: Request, query: TenantLogQuery = Depends()):
    principal = require_tenant_principal(request)
    filters = LogFilters(tenant_id=principal.tenant_id,
                         service_id=query.service_id,
                         tool_alias=query.tool_alias,
                         connection_id=query.connection_id,
                         tool_key=query.source_tool_key,
                         status=query.status)
    return safe_log_list(log_store.list_logs(filters, query.page, query.page_size))
```

Reject a tenant ID query parameter rather than silently honoring it. Tenant log filters expose `service_id`, `tool_alias`, `connection_id`, and `source_tool_key`, matching the service log schema; they never reinterpret an alias as a source key. Overview aggregates only the session tenant's connection, service, tool, and log statistics.

- [ ] **Step 4: Refactor shared pages through explicit endpoint helpers.**

```javascript
export function connectionCollectionEndpoint(scope, tenantId) {
  if (scope === 'tenant') return '/tenant/connections'
  return `/admin/tenants/${encodeURIComponent(tenantId)}/connections`
}
```

Tenant scope skips `/admin/tenants`, tenant selectors, and cross-tenant filters. Admin behavior and existing URL serialization remain unchanged.

Run: `python -m pytest tests/test_tenant_console.py tests/test_mcp_logs_admin.py -q`
Expected: PASS.
Run: `node --test admin-ui/src/pages/connectionView.test.js admin-ui/src/pages/mcpLogsView.test.js && npm --prefix admin-ui run build`
Expected: tests and build pass.

- [ ] **Step 5: Commit scoped console resources.**

```bash
git add app/tenant_console.py app/main.py app/mcp_logs_admin.py admin-ui/src tests/test_tenant_console.py
git commit -m "feat: scope tenant connections and logs"
```

### Task 2B: Complete Tenant Connection Management API

**Files:**
- Create: `app/tenant_connections.py`
- Modify: `app/admin_connections.py`
- Modify: `app/main.py`
- Test: `tests/test_tenant_connections.py`
- Test: `tests/test_admin_connections.py`

**Interfaces:**
- Produces the tenant-scoped equivalents of connection CRUD, credentials, connection Tokens, tool policy, test/sync, and declarative revision operations.
- Reuses extracted domain functions from `admin_connections.py`; it does not call admin HTTP handlers internally.

- [ ] **Step 1: Write failing full-surface isolation tests.**

```python
@pytest.mark.parametrize("method,path,payload", [
    ("post", "/tenant/connections", connection_payload()),
    ("put", "/tenant/connections/conn-a/credentials", {"credentials": {"api_key": "secret-value"}}),
    ("post", "/tenant/connections/conn-a/tokens", {"label": "legacy-connection-client"}),
    ("put", "/tenant/connections/conn-a/tools", {"items": []}),
    ("post", "/tenant/connections/conn-a/test", None),
    ("post", "/tenant/connections/conn-a/sync", None),
    ("post", "/tenant/connections/conn-a/specs/import", {"document": safe_openapi()}),
])
def test_tenant_connection_surface_uses_session_tenant(client, tenant_a_cookie, method, path, payload):
    response = getattr(client, method)(path, cookies=tenant_a_cookie, json=payload)
    assert response.status_code not in {401, 404}
    assert connection_store.last_tenant_id == "tenant-a"

def test_tenant_cannot_access_foreign_connection(client, tenant_a_cookie):
    response = client.get("/tenant/connections/tenant-b-connection", cookies=tenant_a_cookie)
    assert response.status_code == 404
```

- [ ] **Step 2: Run focused tests and verify routes are missing.**

Run: `python -m pytest tests/test_tenant_connections.py tests/test_admin_connections.py -q`
Expected: FAIL because tenant connection routes are absent.

- [ ] **Step 3: Extract shared connection use cases and add the tenant router.**

```python
def owned_connection(tenant_id: str, connection_id: str) -> ConnectionRecord:
    record = connection_store.get_connection(connection_id, tenant_id)
    if record is None:
        raise HTTPException(404, "connection not found")
    return record

@tenant_router.post("/connections")
def create_tenant_connection(body: ConnectionCreateRequest,
                             principal: TenantPrincipal = Depends(require_tenant_principal)):
    return create_connection_use_case(principal.tenant_id, body)
```

Cover list/create/get/update/disable; credential replace; connection Token issue/rotate/revoke; tool list/policy replace; connection test/sync; and OpenAPI import/validate/publish/activate. Every use case receives an explicit server-resolved `tenant_id`. Tenant routes never accept tenant ID in path, query, or body.

- [ ] **Step 4: Run tenant/admin connection regressions.**

Run: `python -m pytest tests/test_tenant_connections.py tests/test_admin_connections.py tests/test_admin_security.py -q`
Expected: PASS and existing admin response contracts remain unchanged.

- [ ] **Step 5: Commit the complete tenant connection API.**

```bash
git add app/tenant_connections.py app/admin_connections.py app/main.py tests/test_tenant_connections.py tests/test_admin_connections.py
git commit -m "feat: expose tenant-scoped connection management"
```

### Task 3: Connector Cards and MCP Service Workbench

**Files:**
- Create: `admin-ui/src/pages/Services.jsx`
- Create: `admin-ui/src/pages/servicesView.js`
- Create: `admin-ui/src/pages/servicesView.test.js`
- Create: `admin-ui/src/pages/ServiceTokenModal.jsx`
- Modify: `admin-ui/src/pages/Connections.jsx`
- Modify: `admin-ui/src/tenantApi.js`
- Modify: `admin-ui/src/TenantApp.jsx`
- Modify: `admin-ui/src/App.jsx`

**Interfaces:**
- `Services` accepts `{scope, tenantId, apiClient}`.
- Service editor submits explicit `{connection_id, source_tool_key, tool_alias, binding_status, policy}` bindings; user writes only `active` or `disabled`.

- [ ] **Step 1: Write failing alias and Token state tests.**

```javascript
test('default alias is stable and identifier-safe', () => {
  assert.equal(defaultToolAlias('wecom_abcd1234', 'users.get'), 'wecom_abcd1234__users.get')
})

test('token list never treats prefix as raw token', () => {
  assert.equal(tokenCanCopy({ token_prefix: 'mcp_abcd', raw_value: undefined }), false)
  assert.equal(tokenCanCopy({ raw_value: 'mcp_full_value' }), true)
})
```

- [ ] **Step 2: Run service view tests and verify missing helpers.**

Run: `node --test admin-ui/src/pages/servicesView.test.js`
Expected: FAIL.

- [ ] **Step 3: Replace connector-key input with connector cards.**

```javascript
const CONNECTOR_CARDS = [
  { key: 'wecom', title: '企业微信', description: '平台内置代码连接器' },
  { key: 'http_declarative', title: 'OpenAPI', description: '通过受控接口配置生成 MCP 工具' },
]
```

The selected card supplies `connector_key` internally. Do not render an editable connector-key field. Future platform-enabled connector definitions are appended from a read-only connector catalog endpoint.

- [ ] **Step 4: Implement service, binding, and reveal flows.**

`Services` provides list/create/edit/disable, connection and tool selection, alias conflict preview, service publish, Token issue/list/reveal/revoke, and copy. The frontend and backend use the same `connection_alias + "__" + source_mcp_name` algorithm; display names are never slugged at runtime. `ServiceTokenModal` requests reveal only after the user clicks “查看” or “复制”, renders the full value, sets no persistent browser storage, and clears component state on close.

Run: `node --test admin-ui/src/pages/servicesView.test.js admin-ui/src/pages/connectionView.test.js`
Expected: PASS.
Run: `npm --prefix admin-ui run build`
Expected: Vite build succeeds.

- [ ] **Step 5: Commit service management UI.**

```bash
git add admin-ui/src/App.jsx admin-ui/src/TenantApp.jsx admin-ui/src/tenantApi.js admin-ui/src/pages/Connections.jsx admin-ui/src/pages/Services.jsx admin-ui/src/pages/servicesView.js admin-ui/src/pages/servicesView.test.js admin-ui/src/pages/ServiceTokenModal.jsx
git commit -m "feat: add MCP service workbench"
```

### Task 4: Declarative Multi-Operation Tool Builder

**Files:**
- Create: `admin-ui/src/pages/DeclarativeToolBuilder.jsx`
- Create: `admin-ui/src/pages/declarativeToolView.js`
- Create: `admin-ui/src/pages/declarativeToolView.test.js`
- Modify: `admin-ui/src/pages/DeclarativeSpecWizard.jsx`

**Interfaces:**
- Produces `buildMcpToolsExtension(tools) -> x-mcp-tools array`.
- Consumes validated operation metadata returned by the backend.

- [ ] **Step 1: Write failing serialization and dependency-order tests.**

```javascript
test('builder serializes only closed references', () => {
  const extension = buildMcpToolsExtension([employeeProfileDraft])
  assert.equal(extension[0].steps[1].input_map.user_id, '$steps.find.user_id')
  assert.equal(JSON.stringify(extension).includes('${'), false)
})

test('builder rejects a forward reference before submit', () => {
  assert.deepEqual(validateToolDraft(forwardReferenceDraft), ['步骤 profile 只能引用前序步骤'])
})
```

- [ ] **Step 2: Run builder tests and verify missing module failure.**

Run: `node --test admin-ui/src/pages/declarativeToolView.test.js`
Expected: FAIL.

- [ ] **Step 3: Implement the sequential builder.**

The builder edits tool name, description, input fields, ordered steps, operation choice, input mapping, declared result mapping, and read/write summary. Available references are generated from the tool input schema and already-added step output names only; users never type a free-form expression.

```javascript
export function availableReferences(tool, steps, currentIndex) {
  const inputRefs = Object.keys(tool.input_schema.properties || {}).map(name => `$input.${name}`)
  const stepRefs = steps.slice(0, currentIndex).flatMap(step =>
    step.outputs.map(name => `$steps.${step.step_id}.${name}`))
  return [...inputRefs, ...stepRefs]
}
```

- [ ] **Step 4: Integrate with import/validate/publish and build.**

The wizard preserves the original OpenAPI JSON/YAML, adds or replaces root `x-mcp-tools`, validates on the server, previews tools and steps, then publishes a new immutable revision. Each serialized step includes explicit `output_mappings`; reference dropdowns expose only those mapped names. If the user defines no composite tools, retain the existing one-operation-per-tool flow.

Run: `node --test admin-ui/src/pages/declarativeToolView.test.js`
Expected: PASS.
Run: `npm --prefix admin-ui run build`
Expected: Vite build succeeds.

- [ ] **Step 5: Commit the builder.**

```bash
git add admin-ui/src/pages/DeclarativeSpecWizard.jsx admin-ui/src/pages/DeclarativeToolBuilder.jsx admin-ui/src/pages/declarativeToolView.js admin-ui/src/pages/declarativeToolView.test.js
git commit -m "feat: build multi-operation OpenAPI tools"
```

### Task 5: Admin Controls, Deployment, and Reversible Smoke Test

**Files:**
- Modify: `admin-ui/src/pages/Tenants.jsx`
- Modify: `admin-ui/src/pages/tenantsView.js`
- Modify: `admin-ui/src/pages/tenantsView.test.js`
- Modify: `app/main.py`
- Modify: `deploy/server_deploy.sh`
- Modify: `docker-compose.yml`
- Modify: `docs/connection-platform-operations.md`
- Modify: `docs/部署指南.md`
- Modify: `README.md`
- Test: `tests/test_server_deploy_script.py`
- Test: `tests/test_smoke_client.py`

**Interfaces:**
- Admin tenant drawer adds initial-password, reset-password, and login-status controls.
- Production deployment validates `MCP_TOKEN_PLAINTEXT_KEY` and applies 007–008 before enabling the feature.

- [ ] **Step 1: Write failing deploy-order and admin form tests.**

```python
def test_deploy_runs_new_migrations_in_order():
    source = Path("deploy/server_deploy.sh").read_text(encoding="utf-8")
    assert source.index("006_connection_platform.sql") < source.index("007_tenant_auth.sql")
    assert source.index("007_tenant_auth.sql") < source.index("008_mcp_service.sql")
```

```javascript
test('tenant create payload includes password only when explicitly entered', () => {
  assert.deepEqual(buildTenantLoginPatch(''), {})
  assert.deepEqual(buildTenantLoginPatch('strong-password-123'), { tenant_password: 'strong-password-123' })
})
```

- [ ] **Step 2: Run deploy and tenant view tests.**

Run: `python -m pytest tests/test_server_deploy_script.py -q`
Expected: FAIL because 007–008 are absent.
Run: `node --test admin-ui/src/pages/tenantsView.test.js`
Expected: FAIL on missing login helpers.

- [ ] **Step 3: Add admin controls and ordered deployment gates.**

The admin UI never reads an existing tenant password. It can set an initial password, reset it, and enable/disable login. Deployment checks that the plaintext key is present, strong, and distinct before starting the new route; it applies SQL in exact numeric order and leaves `MCP_SERVICE_ENABLED=false` until migrations and health checks pass.

- [ ] **Step 4: Update operations docs and run full verification.**

Document the new Token rule precisely: connection Tokens remain display-once; service Tokens can be revealed by platform admin and owning tenant through audited endpoints. Document key rotation, tenant-password reset, feature-flag rollback, default-service backfill, and cleanup SQL.

Run: `python -m pytest -q`
Expected: PASS.
Run: `node --test admin-ui/src/pages/connectionView.test.js admin-ui/src/pages/mcpLogsView.test.js admin-ui/src/pages/tenantsView.test.js admin-ui/src/pages/tenantAppView.test.js admin-ui/src/pages/servicesView.test.js admin-ui/src/pages/declarativeToolView.test.js`
Expected: PASS.
Run: `npm --prefix admin-ui run build`
Expected: Vite build succeeds.

- [ ] **Step 5: Execute authorized production smoke and clean up.**

With a disposable tenant prefix, verify tenant login, two connections, one service exposing tools from both, alias uniqueness, issue/reveal/copy/revoke, wrong-service Token rejection, one two-step OpenAPI tool, scoped logs, and old connection endpoint compatibility. Revoke all test Tokens, disable test services/connections, delete only exact prefixed rows in a transaction, restore settings, and verify zero matching rows.

- [ ] **Step 6: Commit rollout assets.**

```bash
git add admin-ui/src/pages/Tenants.jsx admin-ui/src/pages/tenantsView.js admin-ui/src/pages/tenantsView.test.js app/main.py deploy/server_deploy.sh docker-compose.yml docs README.md tests/test_server_deploy_script.py tests/test_smoke_client.py
git commit -m "feat: prepare tenant MCP services for rollout"
```
