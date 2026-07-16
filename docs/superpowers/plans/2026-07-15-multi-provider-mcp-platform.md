# Multi-Provider MCP Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the WeCom-centric gateway into a tenant-scoped, multi-provider MCP platform where each third-party connection has its own `/mcp/{connection_id}` endpoint, token, policy, credentials, data mode, and audit boundary.

**Architecture:** Introduce a connection domain and connector registry. Trusted code connectors and constrained REST/OpenAPI declarative connectors both produce `ConnectorSpec` objects consumed by a shared runtime. Replace module-global FastMCP tool registration with a connection-aware low-level MCP server so `tools/list` and `tools/call` are derived from the resolved connection instance.

**Tech Stack:** Python 3.11+, FastAPI, MCP SDK 1.28 low-level `Server`, SQLAlchemy 2, MySQL 5.7-compatible SQL, httpx, Pydantic 2, PyYAML safe loader, APScheduler, React 18, Ant Design 5, Vite 5.

## Global Constraints

- Preserve the current `/mcp` endpoint during the compatibility window; it must resolve existing WeCom Tokens to generated default connections.
- New endpoints are `/mcp/{connection_id}` and must authenticate both path instance and instance Token server-side.
- Store third-party credentials encrypted; store MCP Tokens only as keyed HMAC digests plus non-sensitive metadata.
- Never persist or log Authorization, Token, Cookie, Secret, raw request bodies, or raw response bodies.
- Keep MySQL DDL and queries compatible with MySQL 5.7; use bound parameters for values.
- Do not accept uploaded Python packages, arbitrary scripts, Shell, JavaScript, or free-form expression evaluation.
- Declarative outbound HTTP must enforce HTTPS, hostname allowlists, DNS/IP checks, redirect checks, timeout, response-size, and pagination bounds.
- A write tool is callable only when both ToolSpec and connection policy explicitly permit it.
- A declarative connection may use `stored` only when its published revision declares a `SyncSpec`, stable resource key, and field mapping.
- All migrations must be idempotent, restart-safe, and must not delete legacy WeCom data.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `app/connections/models.py` | Typed connection, token, credential, policy, and sync state contracts. |
| `app/connections/crypto.py` | HMAC Token lookup and credential encryption boundaries. |
| `app/connections/store.py` | MySQL DDL, CRUD, token lookup, policies, migration watermarking. |
| `app/connectors/contracts.py` | `Connector`, `ConnectorSpec`, `ToolSpec`, `ConnectionContext`, and result types. |
| `app/connectors/registry.py` | Allowlisted connector discovery and lookup. |
| `app/connectors/runtime.py` | Policy checks, direct/stored/hybrid planning, timeout, rate limit, audit handoff. |
| `app/connectors/wecom.py` | WeCom implementation wrapping existing `data_access` and sync behavior. |
| `app/connectors/declarative/*.py` | OpenAPI import, revision validation, safe HTTP client, mapping, and execution. |
| `app/mcp_gateway.py` | Low-level dynamic MCP protocol server and connection-scoped session manager. |
| `app/admin_connections.py` | Admin API for connection instances, credentials, tokens, policies, sync, and specs. |
| `admin-ui/src/pages/Connections*.jsx` | Connection workbench and declarative connector wizard. |
| `sql/006_connection_platform.sql` | Operational MySQL 5.7 upgrade script matching runtime DDL. |

## Task 1: Connection Persistence, Token Security, and Startup Migration

**Files:**

- Create: `app/connections/__init__.py`
- Create: `app/connections/models.py`
- Create: `app/connections/crypto.py`
- Create: `app/connections/store.py`
- Create: `sql/006_connection_platform.sql`
- Create: `tests/test_connection_store.py`
- Modify: `app/config.py`
- Modify: `app/db.py`
- Modify: `tests/test_migrations.py`

**Interfaces:**

- Produces `ConnectionRecord`, `CredentialRecord`, `ToolPolicy`, `ConnectionToken` and `SyncState` dataclasses.
- Produces `create_connection`, `get_connection`, `list_connections`, `issue_token`, `resolve_connection_token`, `set_tool_policy`, `ensure_connection_tables`, and `migrate_legacy_wecom_connections` from `app.connections.store`.
- Consumes `Settings.mcp_token_hmac_key` and existing `encrypt_secret` / `decrypt_secret` helpers.

- [ ] **Step 1: Write failing store and token-isolation tests.**

```python
def test_token_resolution_requires_matching_connection_id(monkeypatch):
    token = store.issue_token("conn-a", "token-a")
    assert store.resolve_connection_token("token-a", "conn-a").connection_id == "conn-a"
    assert store.resolve_connection_token("token-a", "conn-b") is None
    assert token.raw_value == "token-a"

def test_token_row_never_contains_raw_value(monkeypatch):
    store.issue_token("conn-a", "token-a")
    params = fake_connection.statements[-1][1]
    assert "token-a" not in repr(params)
    assert params["token_hmac"] != "token-a"
```

- [ ] **Step 2: Run the focused tests to verify they fail.**

Run: `python -m pytest tests/test_connection_store.py -q`  
Expected: import or attribute failure for `app.connections.store`.

- [ ] **Step 3: Define typed connection contracts and cryptographic primitives.**

```python
@dataclass(frozen=True)
class ConnectionRecord:
    connection_id: str
    tenant_id: str
    connector_key: str
    display_name: str
    status: Literal["draft", "active", "disabled", "error"]
    data_mode: Literal["direct", "stored", "hybrid"]
    public_config: dict[str, Any]
    config_version: int

@dataclass(frozen=True)
class IssuedToken:
    token_id: str
    raw_value: str
    prefix: str

def token_hmac(raw_token: str) -> str:
    key = get_settings().mcp_token_hmac_key.encode("utf-8")
    return hmac.new(key, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()
```

Add `mcp_token_hmac_key: str` to `Settings`; production validation must require at least 32 UTF-8 bytes and reject example values. Do not reuse the credential encryption key.

- [ ] **Step 4: Implement MySQL 5.7-compatible tables and idempotent access methods.**

Create runtime DDL and matching `sql/006_connection_platform.sql` for `connection_instance`, `connection_credential`, `connection_token`, `connection_tool_policy`, `connection_sync_state`, `declarative_spec_revision`, and `declarative_spec_operation`. Ensure `token_hmac` is unique, use `VARCHAR`/`TEXT`/`DATETIME`, and avoid `ADD COLUMN IF NOT EXISTS`.

```python
def resolve_connection_token(raw_token: str, connection_id: str) -> ConnectionRecord | None:
    statement = text("""
        SELECT c.connection_id, c.tenant_id, c.connector_key, c.display_name,
               c.status, c.data_mode, c.public_config_json, c.config_version
        FROM connection_token t
        JOIN connection_instance c ON c.connection_id=t.connection_id
        WHERE t.connection_id=:connection_id AND t.token_hmac=:token_hmac
          AND t.revoked_at IS NULL
          AND (t.expires_at IS NULL OR t.expires_at > UTC_TIMESTAMP())
          AND c.status='active'
        LIMIT 1
    """)
```

Call `ensure_connection_tables()` from `db.run_startup_migrations()` immediately after central log tables and before tenant-schema work. `migrate_legacy_wecom_connections()` must create exactly one deterministic default `wecom` connection per existing tenant, HMAC-backfill existing Tokens, copy encrypted secret/config metadata, and save a per-tenant completion watermark only after successful transaction commit.

- [ ] **Step 5: Add migration and restart-safety tests.**

```python
def test_startup_orders_connection_tables_before_legacy_wecom_backfill(monkeypatch):
    events = []
    monkeypatch.setattr(connection_store, "ensure_connection_tables", lambda: events.append("tables"))
    monkeypatch.setattr(connection_store, "migrate_legacy_wecom_connections", lambda: events.append("backfill"))
    db.run_startup_migrations()
    assert events.index("tables") < events.index("backfill")

def test_legacy_wecom_backfill_is_idempotent(monkeypatch):
    assert store.migrate_legacy_wecom_connections() == 1
    assert store.migrate_legacy_wecom_connections() == 0
```

- [ ] **Step 6: Run focused and migration tests.**

Run: `python -m pytest tests/test_connection_store.py tests/test_migrations.py -q`  
Expected: all tests pass.

- [ ] **Step 7: Commit the persistence foundation.**

```bash
git add app/connections app/config.py app/db.py sql/006_connection_platform.sql tests/test_connection_store.py tests/test_migrations.py
git commit -m "feat: add connection platform storage"
```

## Task 2: Connector Contracts, Registry, and Policy Runtime

**Files:**

- Create: `app/connectors/__init__.py`
- Create: `app/connectors/contracts.py`
- Create: `app/connectors/registry.py`
- Create: `app/connectors/runtime.py`
- Create: `tests/test_connector_runtime.py`

**Interfaces:**

- Consumes `ConnectionRecord`, `ToolPolicy`, and credentials from Task 1.
- Produces `ConnectorSpec`, `ToolSpec`, `Connector`, `ConnectorRegistry`, `ConnectionContext`, `ExecutionResult`, and `ConnectorRuntime.execute`.

- [ ] **Step 1: Write failing contract and policy tests.**

```python
async def test_runtime_rejects_disabled_or_write_prohibited_tools():
    runtime = ConnectorRuntime(registry)
    with pytest.raises(ToolDisabledError):
        await runtime.execute(ctx, "reports.list", {})
    with pytest.raises(WritePolicyError):
        await runtime.execute(ctx, "reports.delete", {"id": "1"})

def test_registry_rejects_duplicate_connector_keys():
    registry.register(FakeConnector("wecom"))
    with pytest.raises(ValueError, match="duplicate connector_key"):
        registry.register(FakeConnector("wecom"))
```

- [ ] **Step 2: Run the focused tests to verify they fail.**

Run: `python -m pytest tests/test_connector_runtime.py -q`  
Expected: import failure for `app.connectors`.

- [ ] **Step 3: Implement the shared contracts and registry.**

```python
@dataclass(frozen=True)
class ToolSpec:
    tool_key: str
    mcp_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    operation_kind: Literal["read", "write"]
    default_timeout_ms: int
    cache_ttl_seconds: int | None

class ToolDisabledError(PermissionError): pass
class WritePolicyError(PermissionError): pass

@dataclass(frozen=True)
class ExecutionResult:
    data: dict[str, Any]
    status: Literal["ok", "partial", "error"]

    @classmethod
    def ok(cls, data: dict[str, Any]) -> "ExecutionResult":
        return cls(data=data, status="ok")

class Connector(Protocol):
    def spec(self) -> ConnectorSpec: ...
    async def execute(self, context: ConnectionContext, tool_key: str, args: dict[str, Any]) -> ExecutionResult: ...
    async def sync(self, context: ConnectionContext, resource_key: str) -> SyncResult: ...
```

`ConnectorRegistry` must only register explicitly imported built-ins in this task. Keep package entry-point discovery for Task 8 so no unreviewed code is loaded incidentally.

- [ ] **Step 4: Implement execution planning, policies, timeouts, and audit handoff.**

```python
async def execute(self, context: ConnectionContext, tool_key: str, args: dict[str, Any]) -> ExecutionResult:
    tool = self._registry.get(context.connection.connector_key).spec().tool(tool_key)
    policy = self._policy_store.get(context.connection.connection_id, tool_key)
    self._policy_guard.assert_allowed(tool, policy)
    return await asyncio.wait_for(
        self._execute_with_data_mode(context, tool, args),
        timeout=policy.timeout_ms / 1000,
    )
```

Use one internal audit event builder that records `tenant_id`, `connection_id`, `connector_key`, `tool_key`, status, cost and safe summaries. It must never stringify credentials or arbitrary exception objects.

- [ ] **Step 5: Run the contract/runtime tests.**

Run: `python -m pytest tests/test_connector_runtime.py -q`  
Expected: all tests pass, including direct/stored/hybrid planner selection and timeout normalization.

- [ ] **Step 6: Commit the connector runtime.**

```bash
git add app/connectors tests/test_connector_runtime.py
git commit -m "feat: add connector runtime contracts"
```

## Task 3: Move Enterprise WeCom Behind the Connector Interface

**Files:**

- Create: `app/connectors/wecom.py`
- Modify: `app/data_access.py`
- Modify: `app/wecom/dispatch.py`
- Modify: `app/mcp_server.py`
- Create: `tests/test_wecom_connector.py`
- Modify: `tests/test_mcp_tools.py`

**Interfaces:**

- Consumes `Connector`, `ConnectionContext`, `ExecutionResult`, and `SyncResult` from Task 2.
- Produces `WeComConnector.spec()`, `WeComConnector.execute()`, and `WeComConnector.sync()`.
- Preserves existing public tool names and existing `data_access` result envelopes during the compatibility window.

- [ ] **Step 1: Capture existing WeCom result compatibility in tests.**

```python
@pytest.mark.parametrize("tool_key,args", [
    ("reports.list", {"starttime": 1, "endtime": 2, "limit": 10}),
    ("reports.get", {"journaluuid": "r-1"}),
])
async def test_wecom_connector_preserves_existing_result_envelope(monkeypatch, tool_key, args):
    result = await WeComConnector().execute(connection_context(), tool_key, args)
    assert result.data["tenant"] == "tenant-a"
    assert result.data["source"] in {"wecom", "db", "mock"}
```

- [ ] **Step 2: Run the test to verify it fails.**

Run: `python -m pytest tests/test_wecom_connector.py -q`  
Expected: import failure for `WeComConnector`.

- [ ] **Step 3: Implement `WeComConnector` as the only WeCom-specific adapter.**

```python
class WeComConnector:
    connector_key = "wecom"

    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(self.connector_key, tools=(REPORTS_LIST, REPORTS_GET, APPROVALS_LIST, ...), supports_sync=True)

    async def execute(self, context, tool_key, args):
        return await run_wecom_tool(context, tool_key, args)
```

Move module-level FastMCP decorators and `_run_real`/`_run_mock` mechanics out of `app/mcp_server.py`; retain only a legacy adapter until Task 4 routes old `/mcp` requests through the dynamic gateway. Refactor `data_access` to consume `ConnectionContext` while preserving current WeCom storage-schema behavior through a `WeComStorageAdapter`.

- [ ] **Step 4: Add sync parity tests and run focused suites.**

```python
async def test_wecom_sync_uses_connection_scoped_cursor(monkeypatch):
    result = await WeComConnector().sync(connection_context("conn-a"), "reports")
    assert result.connection_id == "conn-a"
    assert sync_store.load("conn-b", "reports") is None
```

Run: `python -m pytest tests/test_wecom_connector.py tests/test_mcp_tools.py -q`  
Expected: all tests pass.

- [ ] **Step 5: Commit the WeCom adapter.**

```bash
git add app/connectors/wecom.py app/data_access.py app/wecom/dispatch.py app/mcp_server.py tests/test_wecom_connector.py tests/test_mcp_tools.py
git commit -m "refactor: move wecom behind connector interface"
```

## Task 4: Dynamic MCP Gateway, Connection Authentication, and Legacy Compatibility

**Files:**

- Create: `app/mcp_gateway.py`
- Modify: `app/auth.py`
- Modify: `app/main.py`
- Modify: `app/mcp_audit.py`
- Create: `tests/test_mcp_gateway.py`
- Modify: `tests/test_admin_security.py`
- Modify: `tests/test_mcp_log_runtime.py`

**Interfaces:**

- Consumes Tasks 1-3.
- Produces `ConnectionResolver.resolve(connection_id, bearer_token)`, `ConnectionMcpGateway`, and `ConnectionCtx` with `tenant_id`, `connection_id`, `connector_key`, `data_mode`, and safe public config.
- Replaces static `mcp` mounting with a low-level MCP `Server` that serves connection-specific `tools/list` and `tools/call` schemas.

- [ ] **Step 1: Write protocol and isolation tests before changing routes.**

```python
def test_connection_endpoint_lists_only_enabled_tools(client, active_connection):
    response = client.post(f"/mcp/{active_connection.connection_id}", headers=bearer("token-a"), json=TOOLS_LIST)
    assert tool_names(response.json()) == {"wecom_list_reports"}

def test_wrong_path_and_valid_token_is_not_authorized(client):
    response = client.post("/mcp/conn-b", headers=bearer("token-a"), json=TOOLS_LIST)
    assert response.status_code == 401

def test_legacy_mcp_path_resolves_default_wecom_connection(client):
    assert client.post("/mcp", headers=bearer("legacy-token"), json=TOOLS_LIST).status_code == 200
```

- [ ] **Step 2: Run the gateway test to verify it fails.**

Run: `python -m pytest tests/test_mcp_gateway.py -q`  
Expected: `/mcp/{connection_id}` returns 404 before the dynamic gateway exists.

- [ ] **Step 3: Implement a connection-scoped low-level MCP server.**

Use `mcp.server.lowlevel.Server` instead of `FastMCP` for dynamic schemas. Its handlers support connection-specific `types.Tool` definitions without generating Python signatures from arbitrary OpenAPI JSON schemas.

```python
@server.list_tools()
async def list_tools() -> list[types.Tool]:
    context = current_connection_ctx()
    return [to_mcp_tool(spec) for spec in runtime.list_enabled_tools(context)]

@server.call_tool(validate_input=True)
async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return (await runtime.execute(current_connection_ctx(), name, arguments)).data
```

Create a per-connection `StreamableHTTPSessionManager` cache keyed by `(connection_id, config_version)`. Invalidate the exact cache key after credential, Token, policy, connection status, or declarative revision changes. Keep the existing transport-security policy and apply auth before protocol handling.

- [ ] **Step 4: Replace tenant-only context with connection-aware context safely.**

```python
@dataclass(frozen=True)
class ConnectionCtx:
    tenant_id: str
    connection_id: str
    connector_key: str
    data_mode: Literal["direct", "stored", "hybrid"]
    public_config: Mapping[str, Any]

def current_ctx() -> ConnectionCtx: ...
```

Keep `require_tenant()` as a compatibility wrapper returning `current_ctx().tenant_id`. Auth failures must emit the existing safe auth audit plus `connection_id` only when it is already resolved safely.

- [ ] **Step 5: Run protocol, auth, runtime, and regression tests.**

Run: `python -m pytest tests/test_mcp_gateway.py tests/test_admin_security.py tests/test_mcp_log_runtime.py tests/test_mcp_tools.py -q`  
Expected: all tests pass; wrong instance/token combinations never call connector code.

- [ ] **Step 6: Commit the gateway migration.**

```bash
git add app/mcp_gateway.py app/auth.py app/main.py app/mcp_audit.py tests/test_mcp_gateway.py tests/test_admin_security.py tests/test_mcp_log_runtime.py
git commit -m "feat: add connection scoped MCP gateway"
```

## Task 5: Connection-Scoped Sync, Cache, and Observability

**Files:**

- Create: `app/connections/sync.py`
- Create: `app/connections/cache.py`
- Modify: `app/main.py`
- Modify: `app/mcp_log_models.py`
- Modify: `app/mcp_log_store.py`
- Modify: `app/mcp_logs_admin.py`
- Modify: `tests/test_mcp_log_store.py`
- Create: `tests/test_connection_sync.py`
- Modify: `tests/test_mcp_logs_admin.py`

**Interfaces:**

- Consumes `ConnectorRegistry`, `ConnectorRuntime`, `ConnectionRecord`, and `ConnectionSyncState`.
- Produces `SyncOrchestrator.run_connection`, `ConnectionCache.get_or_load`, and connection-aware log filter fields.

- [ ] **Step 1: Write failing cache, sync, and log-dimension tests.**

```python
async def test_sync_orchestrator_never_syncs_direct_connection(monkeypatch):
    await orchestrator.run_connection(connection(data_mode="direct"))
    connector.sync.assert_not_called()

async def test_hybrid_cache_is_partitioned_by_connection_and_tool(monkeypatch):
    await cache.put("conn-a", "reports.list", {"x": 1}, ttl_seconds=60)
    assert await cache.get("conn-b", "reports.list") is None

def test_log_filter_isolates_connection_dimension(monkeypatch):
    assert store.list_logs(LogFilters(connection_id="conn-a"), 1, 20)["items"] == [conn_a_log]
```

- [ ] **Step 2: Run the focused tests to verify they fail.**

Run: `python -m pytest tests/test_connection_sync.py tests/test_mcp_log_store.py tests/test_mcp_logs_admin.py -q`  
Expected: missing connection sync/cache APIs and `LogFilters.connection_id`.

- [ ] **Step 3: Implement the scoped sync and cache services.**

```python
async def run_connection(self, connection: ConnectionRecord, resource_key: str | None = None) -> SyncResult | None:
    if connection.status != "active" or connection.data_mode == "direct":
        return None
    async with self._locks.acquire(connection.connection_id):
        return await self._registry.get(connection.connector_key).sync(
            self._contexts.build(connection), resource_key or "default"
        )
```

Store cache entries under `(connection_id, tool_key, normalized_args_hash)`. Cache keys must never include plaintext credentials. Add a scheduler job that enumerates active `stored` and eligible `hybrid` connections; preserve the old WeCom job only as a compatibility wrapper calling this orchestrator.

- [ ] **Step 4: Extend central log storage without leaking historical data.**

Add nullable `connection_id`, `connector_key`, and `tool_key` to `McpLogEvent`, central-table DDL, safe output columns, filters, stats, indexes, and deletion-token normalization. Backfill historical WeCom logs to the generated default connection only when a tenant-to-default mapping is known; keep anonymous auth entries null.

```python
McpLogEvent(
    tenant_id=context.tenant_id,
    connection_id=context.connection_id,
    connector_key=context.connector_key,
    tool_key=tool.tool_key,
    category="tool",
    event_name=tool.mcp_name,
)
```

- [ ] **Step 5: Run focused cache, sync, log, and admin API tests.**

Run: `python -m pytest tests/test_connection_sync.py tests/test_mcp_log_store.py tests/test_mcp_logs_admin.py -q`  
Expected: all tests pass; connection filters affect table data and dashboard statistics.

- [ ] **Step 6: Commit runtime operations support.**

```bash
git add app/connections/sync.py app/connections/cache.py app/main.py app/mcp_log_models.py app/mcp_log_store.py app/mcp_logs_admin.py tests/test_connection_sync.py tests/test_mcp_log_store.py tests/test_mcp_logs_admin.py
git commit -m "feat: add connection scoped sync and observability"
```

## Task 6: Constrained Declarative REST/OpenAPI Connector

**Files:**

- Modify: `requirements.txt`
- Create: `app/connectors/declarative/__init__.py`
- Create: `app/connectors/declarative/models.py`
- Create: `app/connectors/declarative/validator.py`
- Create: `app/connectors/declarative/http_client.py`
- Create: `app/connectors/declarative/connector.py`
- Modify: `app/connections/store.py`
- Create: `tests/test_declarative_connector.py`
- Create: `tests/test_declarative_http_safety.py`

**Interfaces:**

- Consumes `ConnectorSpec`, `ConnectionContext`, revision rows, and credential handles.
- Produces `import_openapi_revision`, `validate_revision`, `SafeHttpClient.request`, and `DeclarativeConnector`.

- [ ] **Step 1: Add tests for schema rejection, SSRF protection, and operation gating.**

```python
@pytest.mark.parametrize("url", [
    "http://api.example.com/v1/items",
    "https://127.0.0.1/admin",
    "https://169.254.169.254/latest/meta-data",
])
async def test_safe_http_client_rejects_unsafe_targets(url):
    with pytest.raises(UnsafeTargetError):
        await client.request("GET", url, headers={}, json_body=None)

async def test_declarative_connector_rejects_undeclared_operation():
    with pytest.raises(UnknownToolError):
        await connector.execute(context, "users.delete", {})

def test_import_rejects_script_like_mapping():
    with pytest.raises(SpecValidationError, match="expressions are not supported"):
        validate_revision({"x-template": "${__import__('os').system('id')}"})
```

- [ ] **Step 2: Run the tests to verify they fail.**

Run: `python -m pytest tests/test_declarative_connector.py tests/test_declarative_http_safety.py -q`  
Expected: missing declarative modules.

- [ ] **Step 3: Add safe OpenAPI parsing and revision models.**

Add `PyYAML>=6.0` to `requirements.txt`. Parse JSON or YAML only through `yaml.safe_load`; cap document size before parsing. Convert supported OpenAPI operations to typed `DeclarativeOperation` records with explicit `tool_key`, `mcp_name`, HTTP method, path, input mappings, output JSON Pointer allowlist, pagination policy, and operation kind.

```python
ALLOWED_AUTH_SCHEMES = {"api_key", "basic", "oauth2_client_credentials"}
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

class SpecValidationError(ValueError): pass
class UnsafeTargetError(ValueError): pass
class UnknownToolError(LookupError): pass

def validate_operation(operation: DeclarativeOperation) -> None:
    if operation.method not in ALLOWED_METHODS:
        raise SpecValidationError("unsupported HTTP method")
    if operation.operation_kind == "write" and not operation.explicit_write_enabled:
        raise SpecValidationError("write operation requires explicit enablement")
```

- [ ] **Step 4: Implement an outbound client with per-hop safety checks.**

```python
async def request(self, method: str, url: str, headers: dict[str, str], json_body: object | None) -> httpx.Response:
    await self._target_guard.assert_allowed(url)
    response = await self._client.request(method, url, headers=headers, json=json_body, follow_redirects=False)
    if response.is_redirect:
        next_url = urljoin(url, response.headers["location"])
        await self._target_guard.assert_allowed(next_url)
        return await self.request(method, next_url, headers, json_body)
    return self._bounded_response(response)
```

Reject non-HTTPS URLs, userinfo URLs, unapproved hostnames, resolved private/loopback/link-local/multicast/reserved addresses, excessive redirects, oversized bodies, and pages past a published maximum.

- [ ] **Step 5: Compile revisions into ConnectorSpecs and execute only declared mappings.**

```python
class DeclarativeConnector:
    async def execute(self, context, tool_key, args):
        operation = self._revision(context).operation_for(tool_key)
        request = operation.build_request(args)
        response = await self._client.request(**request, headers=self._credentials.headers(context))
        return ExecutionResult.ok(operation.extract_safe_output(response.json()))
```

Do not persist raw responses. Reject `stored` unless the revision has a validated `SyncSpec` with stable primary key and mapped fields.

- [ ] **Step 6: Run declarative tests and commit.**

Run: `python -m pytest tests/test_declarative_connector.py tests/test_declarative_http_safety.py -q`  
Expected: all tests pass, including safe mappings, OAuth client credential redaction, redirects, and size limits.

```bash
git add requirements.txt app/connectors/declarative app/connections/store.py tests/test_declarative_connector.py tests/test_declarative_http_safety.py
git commit -m "feat: add constrained declarative connector"
```

## Task 7: Trusted Connector Package Discovery and Version Governance

**Files:**

- Modify: `app/connectors/registry.py`
- Create: `app/connectors/discovery.py`
- Create: `tests/test_connector_discovery.py`
- Modify: `app/config.py`
- Modify: `.env.example`

**Interfaces:**

- Consumes package metadata entry points in group `wbsysc.connectors` and `Settings.connector_allowlist`.
- Produces `discover_trusted_connectors()` returning only valid `Connector` instances.

- [ ] **Step 1: Write failing allowlist and version tests.**

```python
def test_discovery_loads_only_allowlisted_entry_points(monkeypatch):
    monkeypatch.setattr(discovery, "entry_points", lambda group: [trusted_ep, untrusted_ep])
    assert [c.connector_key for c in discover_trusted_connectors()] == ["feishu"]

def test_discovery_rejects_missing_manifest_version(monkeypatch):
    with pytest.raises(ConnectorDiscoveryError, match="version"):
        discover_trusted_connectors()
```

- [ ] **Step 2: Run the tests to verify they fail.**

Run: `python -m pytest tests/test_connector_discovery.py -q`  
Expected: missing discovery module.

- [ ] **Step 3: Implement explicit package discovery.**

```python
def discover_trusted_connectors() -> list[Connector]:
    allowed = {value.strip() for value in get_settings().connector_allowlist.split(",") if value.strip()}
    connectors = []
    for entry_point in entry_points(group="wbsysc.connectors"):
        if entry_point.name not in allowed:
            continue
        connector = entry_point.load()()
        validate_connector_manifest(connector.spec())
        connectors.append(connector)
    return connectors
```

Do not scan directories, execute administrator input, or auto-install packages. Record connector key/version at startup; fail startup only when an active connection references an allowlisted connector whose manifest is unavailable.

Define `ConnectorDiscoveryError(RuntimeError)` and raise it when an allowlisted entry point cannot load or lacks a valid manifest version.

- [ ] **Step 4: Run discovery tests and commit.**

Run: `python -m pytest tests/test_connector_discovery.py -q`  
Expected: all tests pass.

```bash
git add app/connectors/registry.py app/connectors/discovery.py app/config.py .env.example tests/test_connector_discovery.py
git commit -m "feat: add trusted connector discovery"
```

## Task 8: Connection Administration API and Safe Lifecycle Operations

**Files:**

- Create: `app/admin_connections.py`
- Modify: `app/admin.py`
- Modify: `app/main.py`
- Modify: `app/connections/store.py`
- Create: `tests/test_admin_connections.py`
- Modify: `tests/test_admin_security.py`

**Interfaces:**

- Consumes Task 1 store functions, Task 2 registry/spec metadata, Task 4 gateway cache invalidator, and Task 6 declarative revision services.
- Produces `/admin/tenants/{tenant_id}/connections`, `/admin/connections/{connection_id}`, credential, Token, tool-policy, test, sync, and declarative-spec endpoints.

- [ ] **Step 1: Write authorization, validation, and secret-redaction tests.**

```python
def test_connection_api_requires_admin_session():
    assert client.post("/admin/tenants/tenant-a/connections", json={}).status_code == 401

def test_create_connection_returns_token_once_and_never_returns_credential(monkeypatch, authed_client):
    response = authed_client.post("/admin/tenants/tenant-a/connections", json=wecom_payload())
    assert response.status_code == 201
    assert response.json()["initial_token"].startswith("mcp_")
    assert "secret" not in repr(response.json())

def test_tenant_cannot_manage_another_tenants_connection(monkeypatch, authed_client):
    assert authed_client.get("/admin/tenants/tenant-a/connections/conn-b").status_code == 404
```

- [ ] **Step 2: Run the test to verify it fails.**

Run: `python -m pytest tests/test_admin_connections.py -q`  
Expected: missing router/API routes.

- [ ] **Step 3: Implement strict Pydantic request models and routes.**

```python
class ConnectionCreateRequest(BaseModel):
    connector_key: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    display_name: str = Field(min_length=1, max_length=128)
    data_mode: Literal["direct", "stored", "hybrid"]
    public_config: dict[str, Any]
    credentials: list[CredentialInput]

@router.post("/tenants/{tenant_id}/connections", status_code=201)
def create_connection(tenant_id: str, body: ConnectionCreateRequest, request: Request):
    _require_auth(request)
    connection, raw_token = service.create(tenant_id, body)
    return {"connection": safe_connection(connection), "initial_token": raw_token}
```

Validate `connector_key`, config schema, credential schema, and requested tool policies against the selected registered ConnectorSpec before persisting. Return a raw Token only on initial issuance or explicit rotation; return prefix/hint thereafter. Every mutation invalidates the exact connection MCP cache key and writes a safe management audit event.

- [ ] **Step 4: Add lifecycle endpoints and compatibility behavior.**

Implement: list/create connection, get/update/disable, connection test, credentials replace/rotate, Token issue/revoke, tools list/update, manual sync, OpenAPI import, revision validation, revision publish, and revision activation. Disabled connections must return a generic unavailable error to MCP callers and never schedule sync.

- [ ] **Step 5: Run API/security tests.**

Run: `python -m pytest tests/test_admin_connections.py tests/test_admin_security.py -q`  
Expected: all tests pass; raw credentials and raw Tokens are absent from list/detail/error responses and logs.

- [ ] **Step 6: Commit the administration API.**

```bash
git add app/admin_connections.py app/admin.py app/main.py app/connections/store.py tests/test_admin_connections.py tests/test_admin_security.py
git commit -m "feat: add connection administration API"
```

## Task 9: Connection Management Workbench and Declarative Spec Wizard

**Files:**

- Create: `admin-ui/src/pages/Connections.jsx`
- Create: `admin-ui/src/pages/Connections.css`
- Create: `admin-ui/src/pages/connectionView.js`
- Create: `admin-ui/src/pages/connectionView.test.js`
- Create: `admin-ui/src/pages/DeclarativeSpecWizard.jsx`
- Modify: `admin-ui/src/App.jsx`
- Modify: `admin-ui/src/pages/Tenants.jsx`
- Modify: `admin-ui/src/pages/Tenants.css`
- Modify: `admin-ui/src/pages/McpLogs.jsx`

**Interfaces:**

- Consumes Task 8 JSON payloads only; never reconstructs secrets from client state.
- Produces tenant connection list, connection editor, token display-once modal, tool-policy editor, sync/log shortcuts, and declarative import/revision workflow.

- [ ] **Step 1: Write pure view-model tests before React changes.**

```javascript
test('buildConnectionMcpConfig uses the instance-specific endpoint', () => {
  expect(buildConnectionMcpConfig({ connection_id: 'conn-a', token: 'mcp_x' }, 'https://gw.example.com'))
    .toContain('https://gw.example.com/mcp/conn-a')
})

test('token display state is cleared after the one-time modal closes', () => {
  expect(closeTokenModal({ rawToken: 'mcp_secret' })).toEqual({ open: false, rawToken: '' })
})

test('write tools require two explicit UI flags', () => {
  expect(canEnableWriteTool({ operation_kind: 'write' }, { explicitWrite: false })).toBe(false)
})
```

- [ ] **Step 2: Run the test to verify it fails.**

Run: `node --test src/pages/connectionView.test.js`  
Expected: module-not-found error for `connectionView.js`.

- [ ] **Step 3: Implement a tenant-scoped connections workbench.**

Use a tenant row action named `连接实例` to open the workbench. The list must show connector type, active/disabled state, data mode, MCP endpoint, Token hint, tool count, sync health, and a log shortcut filtered by `connection_id`. Render raw Token only in the post-create/post-rotate modal, include copy support, and clear it from React state when closed.

```javascript
export function buildConnectionMcpConfig(connection, origin) {
  return JSON.stringify({
    mcpServers: {
      [connection.connection_id]: {
        url: `${origin}/mcp/${encodeURIComponent(connection.connection_id)}`,
        headers: { Authorization: `Bearer ${connection.initial_token}` },
      },
    },
  }, null, 2)
}
```

- [ ] **Step 4: Implement the declarative connection wizard.**

The wizard steps are: select `http_declarative` → paste/import specification → backend validation → select operations → configure approved input/output mappings → connection test → publish revision → activate connection. Do not render a code editor for JavaScript/Python/template logic; show server-returned validation errors without secrets or raw upstream responses.

- [ ] **Step 5: Extend logs navigation and responsive behavior.**

Add `connection_id` and connector type to URL filter serialization, log filters, detail drawer, dashboard title, and narrow-screen priority rules. Existing tenant logs remain functional with no connection filter.

- [ ] **Step 6: Run frontend checks and commit.**

Run: `node --test src/pages/tenantsView.test.js src/pages/mcpLogsView.test.js src/pages/connectionView.test.js`  
Expected: all tests pass.

Run: `pnpm run build`  
Expected: production build exits 0.

Run: `antd lint ./src --format json`  
Expected: `"total": 0`.

```bash
git add admin-ui/src/App.jsx admin-ui/src/pages/Connections.jsx admin-ui/src/pages/Connections.css admin-ui/src/pages/connectionView.js admin-ui/src/pages/connectionView.test.js admin-ui/src/pages/DeclarativeSpecWizard.jsx admin-ui/src/pages/Tenants.jsx admin-ui/src/pages/Tenants.css admin-ui/src/pages/McpLogs.jsx
git commit -m "feat: add connection management workbench"
```

## Task 10: Compatibility Migration, End-to-End Verification, and Operations Handoff

**Files:**

- Create: `tests/test_connection_migration_e2e.py`
- Create: `tests/test_mcp_connection_isolation.py`
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `.env.prod.example`
- Create: `docs/connection-platform-operations.md`
- Modify: `docs/superpowers/specs/2026-07-15-multi-provider-mcp-platform-design.md`

**Interfaces:**

- Consumes all prior tasks.
- Produces an executable legacy migration validation, production configuration guide, rollback procedure, connector-package release procedure, and operations checklist.

- [ ] **Step 1: Write end-to-end migration and cross-instance isolation tests.**

```python
def test_legacy_wecom_client_and_new_connection_endpoint_return_equivalent_tools(client, seeded_legacy_tenant):
    legacy = client.post("/mcp", headers=bearer("legacy-token"), json=TOOLS_LIST)
    modern = client.post(f"/mcp/{seeded_legacy_tenant.connection_id}", headers=bearer("legacy-token"), json=TOOLS_LIST)
    assert tool_names(legacy.json()) == tool_names(modern.json())

def test_instance_cannot_read_another_instance_cache_credentials_or_logs(client, conn_a, conn_b):
    assert client.post(f"/mcp/{conn_b.connection_id}", headers=bearer(conn_a.token), json=TOOLS_LIST).status_code == 401
    assert list_logs(LogFilters(connection_id=conn_a.connection_id))["items"] != list_logs(LogFilters(connection_id=conn_b.connection_id))["items"]
```

- [ ] **Step 2: Run end-to-end tests to verify the final integration gaps.**

Run: `python -m pytest tests/test_connection_migration_e2e.py tests/test_mcp_connection_isolation.py -q`  
Expected: all tests pass after prior tasks; any failure is fixed in the owning task rather than patched around here.

- [ ] **Step 3: Document deployment, rollback, and operational controls.**

Document exact environment variables (`MCP_TOKEN_HMAC_KEY`, `CONNECTOR_ALLOWLIST`), schema migration order, backup/restore point, legacy endpoint compatibility period, Token rotation, connector install/release process, declarative specification review, SSRF allowlist maintenance, audit retention, and rollback from new route to legacy route. Include a concrete rollback condition: if parity checks fail, disable the affected connection, invalidate its MCP cache, retain data, and route legacy WeCom Tokens through the legacy adapter until corrected.

- [ ] **Step 4: Run the complete verification matrix.**

Run: `$env:APP_ENV='dev'; $env:WECOM_USE_MOCK='false'; .\.venv\Scripts\python.exe -m pytest -q`  
Expected: all backend tests pass.

Run: `python -m pytest -q`  
Expected: all backend tests pass in the alternate configured environment.

Run: `node --test src/pages/tenantsView.test.js src/pages/mcpLogsView.test.js src/pages/connectionView.test.js` in `admin-ui`  
Expected: all frontend unit tests pass.

Run: `pnpm run build` and `antd lint ./src --format json` in `admin-ui`  
Expected: build exits 0 and lint reports 0 issues.

- [ ] **Step 5: Perform a browser and MySQL smoke test.**

Use a non-production test tenant and separate test connection instances. Verify: create WeCom connection, retrieve one-time Token, use `/mcp/{connection_id}`, rotate Token, disable a tool, inspect connection-scoped logs, import a safe OpenAPI read operation, reject an unsafe OpenAPI URL, and verify that no test credentials/retention rows remain afterward.

- [ ] **Step 6: Commit handoff artifacts.**

```bash
git add tests/test_connection_migration_e2e.py tests/test_mcp_connection_isolation.py README.md .env.example .env.prod.example docs/connection-platform-operations.md docs/superpowers/specs/2026-07-15-multi-provider-mcp-platform-design.md
git commit -m "docs: add connection platform operations guide"
```

## Coverage Self-Review

| Design requirement | Plan task |
| --- | --- |
| Tenant-to-many connection instances | Task 1, Task 8, Task 9 |
| `/mcp/{connection_id}` and independent Token | Task 1, Task 4, Task 10 |
| Code and declarative connector convergence | Task 2, Task 3, Task 6, Task 7 |
| Direct/stored/hybrid behavior | Task 2, Task 3, Task 5, Task 6 |
| Tool enablement, read-only, timeout, rate limit | Task 2, Task 4, Task 8, Task 9 |
| Encrypted credentials and HMAC Tokens | Task 1, Task 4, Task 8 |
| WeCom no-downtime migration | Task 1, Task 3, Task 4, Task 10 |
| Connection-scoped logs and dashboards | Task 5, Task 8, Task 9 |
| SSRF-safe declarative HTTP | Task 6, Task 8, Task 10 |
| Trusted package governance | Task 7, Task 10 |
| Production rollback and operations | Task 10 |

Self-review completed: the plan contains no unfinished markers, all referenced types are introduced by an earlier task, and each implementation task has a focused test cycle and commit boundary.
