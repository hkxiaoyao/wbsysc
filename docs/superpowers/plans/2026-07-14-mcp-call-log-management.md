# MCP Call Log Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a secure central MCP call-log workbench with business/protocol/auth logging, structured search, tenant dashboards, guarded cleanup, and configurable retention.

**Architecture:** New central log tables and a focused store module replace new writes to per-tenant `audit_log` tables. A unified audit module records tool, protocol, and auth metadata without request/response bodies, while a separate admin router exposes list, stats, previewed deletion, and retention APIs. The React admin app adds a URL-backed unified log workbench; tenant shortcuts reuse the same page with `tenant_id` preselected.

**Tech Stack:** Python 3.11+, FastAPI/Starlette, SQLAlchemy 2, MySQL 5.7-compatible SQL, APScheduler 3, pytest, React 18, Ant Design 5.29.3, Axios, Day.js, Node test runner, Vite 5.

## Global Constraints

- Never persist Authorization headers, MCP tokens, WeCom secrets, cookies, complete request bodies, or complete response bodies.
- New events write only to the central `mcp_call_log`; existing tenant `audit_log` tables remain readable for migration but receive no new writes.
- Migrate only the latest 90 days of legacy audit rows and deduplicate by `(legacy_schema, legacy_id)`.
- Retention defaults to 90 days, accepts `0–3650`, and `0` disables scheduled cleanup.
- Delete operations require a preview token bound to normalized criteria, current admin session, preview maximum ID, count, and five-minute expiry.
- Queries use structured filters and bound SQL parameters; no raw SQL or advanced query expression is accepted.
- All deletion loops use independent transactions of at most 5000 rows.
- Ant Design code targets 5.29.3 and uses supported `Table.rowSelection`, `Drawer.destroyOnHidden`, `DatePicker.RangePicker.presets`, `Statistic.loading`, and `Modal.useModal` APIs.

---

## File Map and Ownership

- `app/mcp_log_models.py`: shared immutable DTOs and normalized filter/delete contracts.
- `app/mcp_log_store.py`: central DDL, legacy migration, inserts, queries, statistics, settings, previews, and batched deletion.
- `app/mcp_audit.py`: sanitization, event creation, auth write throttling, and protocol ASGI middleware.
- `app/mcp_logs_admin.py`: authenticated admin HTTP request/response models and routes.
- `app/db.py`: startup hook only; existing tenant schema behavior stays intact.
- `app/mcp_server.py`: tool result instrumentation only.
- `app/auth.py`: auth event instrumentation only.
- `app/main.py`: router/middleware registration and daily cleanup scheduling only.
- `admin-ui/src/pages/mcpLogsView.js`: URL/query/delete payload helpers.
- `admin-ui/src/pages/McpLogs.jsx`: log workbench state and Ant Design UI.
- `admin-ui/src/pages/McpLogs.css`: responsive workbench styling.
- `admin-ui/src/App.jsx`: lightweight URL-backed navigation.
- `admin-ui/src/pages/Tenants.jsx`: tenant log shortcut callback.

---

### Task 1: Central Log Models, Schema, Store, and Legacy Migration

**Files:**
- Create: `app/mcp_log_models.py`
- Create: `app/mcp_log_store.py`
- Create: `sql/005_mcp_call_log.sql`
- Create: `tests/test_mcp_log_store.py`
- Modify: `app/db.py:185-189`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Produces `McpLogEvent`, `LogFilters`, and `DeleteSpec` dataclasses.
- Produces `ensure_central_log_tables()`, `migrate_legacy_logs(days=90)`, `insert_event(event)`, `list_logs(filters, page, page_size)`, `get_log_stats(filters)`, `preview_delete(spec)`, `delete_matching(spec, max_id, batch_size=5000)`, `get_retention_days()`, `set_retention_days(days)`, and `cleanup_expired_logs(now=None)`.
- Consumes only `app.db.get_engine()` through a local import to avoid module import cycles.

- [ ] **Step 1: Write failing model and store tests**

```python
def test_log_filters_reject_inverted_cost_range():
    with pytest.raises(ValueError, match="cost_min"):
        LogFilters(cost_min=500, cost_max=100)


def test_legacy_migration_is_bounded_and_idempotent(fake_engine):
    migrate_legacy_logs(days=90)
    migrate_legacy_logs(days=90)
    sql = "\n".join(fake_engine.statements)
    assert "INTERVAL 90 DAY" in sql
    assert "legacy_schema" in sql
    assert "legacy_id" in sql
    assert "INSERT IGNORE" in sql


def test_cleanup_zero_retention_skips_delete(monkeypatch):
    monkeypatch.setattr(store, "get_retention_days", lambda: 0)
    assert cleanup_expired_logs() == 0
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pytest tests/test_mcp_log_store.py tests/test_migrations.py -q`

Expected: collection fails because `app.mcp_log_models` and `app.mcp_log_store` do not exist.

- [ ] **Step 3: Implement immutable contracts**

```python
@dataclass(frozen=True)
class McpLogEvent:
    tenant_id: str = ""
    category: Literal["tool", "protocol", "auth"] = "protocol"
    event_name: str = "mcp_http_request"
    target: str = ""
    params_summary: str = ""
    result_status: Literal["ok", "partial", "error", "denied"] = "ok"
    error_code: str = ""
    error_summary: str = ""
    cost_ms: int = 0
    request_id: str = ""
    client_ip: str = ""
    http_method: str = ""
    http_status: int = 0
    created_at: datetime | None = None


@dataclass(frozen=True)
class DeleteSpec:
    mode: Literal["ids", "filter", "before_date", "all"]
    ids: tuple[int, ...] = ()
    filters: LogFilters = field(default_factory=LogFilters)
    before_date: datetime | None = None
```

`LogFilters.__post_init__` validates enum values, normalized UTC-naive datetimes, non-negative costs, `cost_min <= cost_max`, and strings no longer than their API limits.

- [ ] **Step 4: Implement MySQL 5.7-compatible DDL and startup migration**

Create central tables with `CREATE TABLE IF NOT EXISTS`, all indexes from the design, `DATETIME(6)`, and the `(legacy_schema, legacy_id)` unique key. Add this order to `db.run_startup_migrations()`:

```python
def run_startup_migrations() -> None:
    ensure_central_columns()
    from .mcp_log_store import ensure_central_log_tables, migrate_legacy_logs
    ensure_central_log_tables()
    for schema_name in get_tenant_schema_names():
        ensure_schema(schema_name)
    migrate_legacy_logs(days=90)
```

Legacy migration selects the real `tenant_id` from `tenant_config`, validates every schema with the existing schema validator, and migrates only the 最近 90 天 rows by executing `INSERT IGNORE ... SELECT` for records newer than `UTC_TIMESTAMP() - INTERVAL 90 DAY`.

- [ ] **Step 5: Implement parameterized insert, list, stats, settings, preview, and batched deletion**

Build every `WHERE` clause from a fixed field map and SQLAlchemy bind parameters. Keyword search escapes `%`, `_`, and `\\` before applying `LIKE ... ESCAPE '\\\\'`. P95 first counts matching rows, computes `floor((count - 1) * 0.95)`, then queries one ordered `cost_ms` using a bounded offset.

For batched deletion, first select at most 5000 matching IDs with `id <= :max_id`, then delete those exact IDs in the same transaction. Repeat until no IDs remain.

- [ ] **Step 6: Run store and migration tests**

Run: `pytest tests/test_mcp_log_store.py tests/test_migrations.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit the storage foundation**

```bash
git add app/mcp_log_models.py app/mcp_log_store.py app/db.py sql/005_mcp_call_log.sql tests/test_mcp_log_store.py tests/test_migrations.py
git commit -m "feat: add central MCP log storage"
```

---

### Task 2: Frontend Query and URL View Model

**Files:**
- Create: `admin-ui/src/pages/mcpLogsView.js`
- Create: `admin-ui/src/pages/mcpLogsView.test.js`

**Interfaces:**
- Produces `DEFAULT_LOG_FILTERS`, `parseLogLocation(search)`, `serializeLogFilters(filters)`, `buildLogQuery(filters, page, pageSize)`, `buildDeleteSpec(mode, filters, selectedIds, beforeDate)`, `formatDuration(ms)`, and `statusMeta(status)`.
- No React or browser-only dependency is allowed so Node can test the module directly.

- [ ] **Step 1: Write failing URL/filter tests**

```javascript
test('parseLogLocation restores tenant and defaults to the last 24 hours', () => {
  const filters = parseLogLocation('?tenant_id=tenant-a&status=error')
  assert.equal(filters.tenantId, 'tenant-a')
  assert.equal(filters.status, 'error')
  assert.ok(filters.from)
  assert.ok(filters.to)
})

test('buildDeleteSpec never emits UI-only pagination fields', () => {
  const body = buildDeleteSpec('filter', { ...DEFAULT_LOG_FILTERS, tenantId: 't1' }, [], null)
  assert.deepEqual(body, { mode: 'filter', filter: { tenant_id: 't1' } })
})
```

- [ ] **Step 2: Run tests and verify failure**

Run: `node --test src/pages/mcpLogsView.test.js`

Expected: module-not-found failure.

- [ ] **Step 3: Implement deterministic helpers**

Use `URLSearchParams`, ISO timestamps, fixed enum maps, and no local timezone parsing. Exclude empty/default filters from serialized URLs while always preserving an explicit selected time range.

- [ ] **Step 4: Run frontend view-model tests**

Run: `node --test src/pages/mcpLogsView.test.js`

Expected: all tests pass.

- [ ] **Step 5: Commit the frontend view model**

```bash
git add admin-ui/src/pages/mcpLogsView.js admin-ui/src/pages/mcpLogsView.test.js
git commit -m "test: define MCP log workbench view model"
```

---

### Task 3: Unified Audit Writer and MCP Instrumentation

**Files:**
- Create: `app/mcp_audit.py`
- Create: `tests/test_mcp_audit.py`
- Modify: `app/mcp_server.py:121-161` and every mock return path
- Modify: `app/auth.py:51-86`
- Modify: `tests/test_mcp_tools.py`
- Modify: `tests/test_admin_security.py`

**Interfaces:**
- Consumes `McpLogEvent` and `insert_event()` from Tasks 1.
- Produces `write_event(event)`, `safe_summary(value, limit)`, `client_ip_from_scope(scope)`, `AuthWriteLimiter`, and `McpProtocolAuditMiddleware`.
- `write_event` catches and safely logs storage failures without raising.

- [ ] **Step 1: Write failing sanitization, auth, protocol replay, and tool tests**

```python
@pytest.mark.parametrize("secret", ["Bearer abc", "mcp_token=x", "secret=y", "Cookie: sid=z"])
def test_safe_summary_redacts_sensitive_values(secret):
    assert secret not in safe_summary(secret, 512)


async def test_protocol_middleware_replays_body_and_logs_only_method():
    body = b'{"jsonrpc":"2.0","method":"tools/list","id":1}'
    response, received_body, events = await exercise_middleware(body)
    assert received_body == body
    assert events[0].event_name == "tools/list"
    assert body.decode() not in repr(events[0])
```

Extend existing MCP tests to assert mock and real tool paths each emit one `tool` event and no secret is present in captured events.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `pytest tests/test_mcp_audit.py tests/test_mcp_tools.py tests/test_admin_security.py -q`

Expected: missing audit module and missing events fail.

- [ ] **Step 3: Implement safe writer and bounded auth limiter**

`safe_summary` applies case-insensitive key/value redaction before length truncation. `AuthWriteLimiter` keys by normalized IP and event, allows 60 writes per minute, prunes expired buckets, and never includes token-derived material.

- [ ] **Step 4: Implement pure ASGI protocol middleware**

Buffer POST request bodies only up to 64 KiB, replay every original ASGI message unchanged, extract only a string JSON-RPC `method`, and record response status plus elapsed monotonic time. GET and DELETE events use `mcp_http_get` and `mcp_http_delete`.

- [ ] **Step 5: Replace per-schema tool writes and add auth events**

Tool instrumentation passes the real `tenant_id`, explicit target/parameter summaries, status, request metadata, and elapsed time to `write_event`. Remove calls to `db.log_audit`. Auth middleware records `auth_missing`, `auth_invalid`, and `auth_ok` without reading or storing the token value.

- [ ] **Step 6: Run audit and existing MCP/security tests**

Run: `pytest tests/test_mcp_audit.py tests/test_mcp_tools.py tests/test_admin_security.py -q`

Expected: all tests pass and existing tool signatures remain unchanged.

- [ ] **Step 7: Commit instrumentation**

```bash
git add app/mcp_audit.py app/mcp_server.py app/auth.py tests/test_mcp_audit.py tests/test_mcp_tools.py tests/test_admin_security.py
git commit -m "feat: audit MCP protocol auth and tool calls"
```

---

### Task 4: Admin Query, Statistics, Cleanup, and Settings API

**Files:**
- Create: `app/mcp_logs_admin.py`
- Create: `tests/test_mcp_logs_admin.py`

**Interfaces:**
- Consumes all query/delete/settings functions from Task 1 and `_require_auth(request)` from `app.admin`.
- Produces `router = APIRouter(prefix="/admin", tags=["mcp-logs"])`.
- Produces endpoints exactly matching the approved design.

- [ ] **Step 1: Write failing API tests**

```python
def test_list_logs_requires_admin_session(client):
    assert client.get("/admin/mcp-logs").status_code == 401


def test_delete_preview_and_execute_bind_same_normalized_spec(authed_client, store):
    preview = authed_client.post("/admin/mcp-logs/delete-preview", json={"mode": "all"}).json()
    response = authed_client.request(
        "DELETE", "/admin/mcp-logs",
        json={"mode": "all", "confirm_token": preview["confirm_token"]},
    )
    assert response.status_code == 200
    assert response.json()["deleted"] == preview["matched_count"]
```

Cover invalid enums, time inversion, page size 101, keyword length 101, cost inversion, expired/tampered token, changed filters, and `id > preview_max_id` survival.

- [ ] **Step 2: Run API tests and verify failure**

Run: `pytest tests/test_mcp_logs_admin.py -q`

Expected: module and route failures.

- [ ] **Step 3: Implement Pydantic request/response contracts and list/stats routes**

Use `Annotated` with FastAPI `Query` constraints. Convert all request fields into `LogFilters`; never pass arbitrary dictionaries to the store. Return only the field whitelist from the design.

- [ ] **Step 4: Implement signed delete preview and execution**

Sign canonical JSON containing version, admin-session digest, normalized delete spec, `max_id`, count, and expiry using HMAC-SHA256 with a key derived from `credential_key` plus `admin_password`. Reject missing production key material through existing production settings validation; development derives a process-local fallback and logs a warning without exposing it.

- [ ] **Step 5: Implement retention settings endpoints**

`PUT /admin/mcp-log-settings` accepts only `{ "retention_days": integer }`, validates `0–3650`, writes through the store, and returns the persisted value.

- [ ] **Step 6: Run API tests**

Run: `pytest tests/test_mcp_logs_admin.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit admin APIs**

```bash
git add app/mcp_logs_admin.py tests/test_mcp_logs_admin.py
git commit -m "feat: add MCP log management APIs"
```

---

### Task 5: Unified MCP Log Workbench Page

**Files:**
- Create: `admin-ui/src/pages/McpLogs.jsx`
- Create: `admin-ui/src/pages/McpLogs.css`

**Interfaces:**
- Consumes Task 2 helpers and `/admin/mcp-logs`, `/admin/mcp-logs/stats`, `/admin/mcp-logs/delete-preview`, `/admin/mcp-logs`, and `/admin/mcp-log-settings`.
- Receives `onFiltersChange(filters)` so App can synchronize the URL.
- Produces the unified global/tenant log workbench.

- [ ] **Step 1: Build the page state around one normalized filter object**

Use separate `logsLoading`, `statsLoading`, `logsError`, and `statsError` states so statistics failure never hides the table. Abort or sequence requests so older responses cannot overwrite newer filters.

- [ ] **Step 2: Build tenant dashboard and structured filters**

When `filters.tenantId` is set, render four `Statistic` cards plus CSS-rendered trend, top-tool, and status-distribution panels. Use `DatePicker.RangePicker` presets for 1 hour, 24 hours, 7, 30, and 90 days. Place request ID, IP, event name, and cost range inside a “更多筛选” popover/drawer.

- [ ] **Step 3: Build table, row details, and responsive behavior**

Use `Table` with `rowKey="id"`, controlled server pagination, controlled `rowSelection.selectedRowKeys`, and responsive columns. Clicking a row opens a `Drawer` with `destroyOnHidden={false}` and only safe fields.

- [ ] **Step 4: Build guarded cleanup interactions**

Every destructive action calls preview first. Show matched count and preview expiry in a `Modal.useModal()` confirmation. For `all`, require the exact text `清空全部日志` before enabling the danger button. On success clear selection and reload both list and stats.

- [ ] **Step 5: Build retention settings drawer and all visual states**

Use an `InputNumber` constrained to `0–3650`; explain that 0 disables automation. Add loading skeletons, empty state, retry alerts, compact narrow layout, and a persistent filter summary.

- [ ] **Step 6: Run view-model tests, production build, and Ant Design lint**

Run:

```bash
node --test src/pages/mcpLogsView.test.js
pnpm run build
antd lint src/pages/McpLogs.jsx --format json
```

Expected: tests and build pass; lint reports zero issues.

- [ ] **Step 7: Commit the workbench page**

```bash
git add admin-ui/src/pages/McpLogs.jsx admin-ui/src/pages/McpLogs.css
git commit -m "feat: add MCP log workbench"
```

---

### Task 6: Runtime Registration and Daily Retention Job

**Files:**
- Modify: `app/main.py:25-29`, `app/main.py:101-125`, `app/main.py:143-180`
- Create: `tests/test_mcp_log_runtime.py`

**Interfaces:**
- Consumes `mcp_logs_admin.router`, `McpProtocolAuditMiddleware`, and `cleanup_expired_logs`.
- Keeps startup ordering: DB migrations before MCP session manager and scheduler.

- [ ] **Step 1: Write failing runtime-order and scheduler tests**

```python
def test_runtime_registers_admin_logs_before_static_and_mcp_mounts():
    paths = [route.path for route in create_app().routes]
    assert "/admin/mcp-logs" in paths
    assert paths.index("/admin/mcp-logs") < paths.index("/mcp")


def test_daily_cleanup_runs_in_executor(monkeypatch):
    events = []
    monkeypatch.setattr(store, "cleanup_expired_logs", lambda: events.append("cleanup"))
    asyncio.run(main._cleanup_logs_job_async())
    assert events == ["cleanup"]
```

- [ ] **Step 2: Run runtime tests and verify failure**

Run: `pytest tests/test_mcp_log_runtime.py tests/test_migrations.py -q`

Expected: missing route/job failures.

- [ ] **Step 3: Register router and protocol middleware**

Include the logs router next to the existing admin router. Wrap the MCP child app so the bearer middleware establishes tenant context before protocol logging finalizes the event, while auth failures remain recorded by the bearer middleware itself.

- [ ] **Step 4: Schedule one daily cleanup job**

Add `_cleanup_logs_job_async()` using `run_in_executor`, schedule it with a cron trigger at `03:17` Asia/Shanghai, `max_instances=1`, and `coalesce=True`. Do not run cleanup immediately at startup.

- [ ] **Step 5: Run runtime and migration tests**

Run: `pytest tests/test_mcp_log_runtime.py tests/test_migrations.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit runtime integration**

```bash
git add app/main.py tests/test_mcp_log_runtime.py
git commit -m "feat: schedule MCP log retention"
```

---

### Task 7: Admin Navigation and Tenant Shortcut

**Files:**
- Modify: `admin-ui/src/App.jsx`
- Modify: `admin-ui/src/index.css`
- Modify: `admin-ui/src/pages/Tenants.jsx:63`, `admin-ui/src/pages/Tenants.jsx:430-460`
- Modify: `admin-ui/src/pages/Tenants.css`

**Interfaces:**
- App owns `view` and log filter URL state without adding React Router.
- `Tenants` accepts `onViewLogs(tenantId)`.
- `McpLogs` receives parsed filters and `onFiltersChange`.

- [ ] **Step 1: Add lightweight URL navigation**

Map `/admin/ui/` and `?view=tenants` to tenants, and `?view=logs` to logs. Handle `popstate`, update URL through `history.pushState`, and keep the existing authentication behavior.

- [ ] **Step 2: Replace the header title-only layout with accessible navigation**

Add two real buttons/links with active state and keyboard focus styling. Preserve logout. On narrow screens keep both navigation items visible and shorten labels only through CSS.

- [ ] **Step 3: Add tenant-row “查看调用日志” action**

Insert a non-danger menu item with a log icon before synchronization actions. Call `onViewLogs(row.tenant_id)` and do not mutate tenant state.

- [ ] **Step 4: Run frontend checks**

Run:

```bash
node --test src/pages/tenantsView.test.js src/pages/mcpLogsView.test.js
pnpm run build
antd lint src/App.jsx src/pages/Tenants.jsx src/pages/McpLogs.jsx --format json
```

Expected: all tests and build pass; lint reports zero issues.

- [ ] **Step 5: Commit navigation integration**

```bash
git add admin-ui/src/App.jsx admin-ui/src/index.css admin-ui/src/pages/Tenants.jsx admin-ui/src/pages/Tenants.css
git commit -m "feat: connect tenant and MCP log navigation"
```

---

### Task 8: Full Validation, Browser Smoke, Review, and Documentation Closure

**Files:**
- Modify: `.ccg/tasks/mcp-call-log-query/review.md`
- Modify only if a validated issue requires a fix: files owned by Tasks 1–7.

**Interfaces:**
- Consumes the complete implementation.
- Produces evidence for migration safety, sensitive-data protection, UI behavior, and release readiness.

- [ ] **Step 1: Run the full backend suite**

Run: `pytest -q`

Expected: all non-opt-in smoke tests pass; live smoke remains skipped unless `MCP_SMOKE_RUN=1`.

- [ ] **Step 2: Run all frontend checks**

Run:

```bash
cd admin-ui
node --test src/pages/tenantsView.test.js src/pages/mcpLogsView.test.js
pnpm run build
antd lint src/App.jsx src/pages/Tenants.jsx src/pages/McpLogs.jsx --format json
```

Expected: tests and build pass; lint reports zero issues. The existing Vite chunk-size warning is informational unless the feature introduces a materially larger chunk.

- [ ] **Step 3: Verify migrations against empty and existing schemas**

Run the migration tests plus a controlled database migration using non-secret environment variables already configured in the deployment environment. Verify central tables, 90-day bound, tenant ID mapping, and idempotent row counts. Never print the database password.

- [ ] **Step 4: Run browser smoke at desktop and narrow widths**

Verify login, top navigation, global list, tenant shortcut, URL restoration, dashboard, all filters, pagination, selection, details drawer, four cleanup previews, all-clear phrase gate, retention settings, error retry, empty state, and 390-pixel layout. Capture console and page errors; both must be empty.

- [ ] **Step 5: Run dual-model review**

Review `git diff` in parallel with Gemini and Claude. Classify findings as Critical/Warning/Info in `.ccg/tasks/mcp-call-log-query/review.md`. If Gemini remains unavailable because `GEMINI_API_KEY` is absent, record the attempted command and limitation; Claude review and local security checks must still complete.

- [ ] **Step 6: Fix Critical and applicable Warning findings, then rerun validation**

Repeat focused tests after each fix, followed by the full backend/frontend validation. No Critical finding may remain.

- [ ] **Step 7: Commit final review fixes**

```bash
git add app tests admin-ui/src sql .ccg/tasks/mcp-call-log-query/review.md
git commit -m "fix: harden MCP log management"
```

---

## Dependency Layers for Parallel Execution

- **Layer 1:** Task 1 and Task 2 in parallel; their file ownership does not overlap.
- **Layer 2:** After Task 1, run Tasks 3 and 4 in parallel. After Task 2, Task 5 may also run in parallel because it owns only new page files.
- **Layer 3:** After Tasks 3–5, run Task 6 and Task 7 in parallel; backend and frontend integration files do not overlap.
- **Layer 4:** Task 8 runs after all implementation tasks and is single-owner review/verification work.

## Completion Criteria

- All approved design requirements are implemented without advanced query syntax or request/response body retention.
- Existing MCP tool names and signatures remain compatible.
- Central migration is idempotent and old tenant audit tables are untouched.
- All cleanup operations use preview-bound upper IDs and batch limits.
- Admin UI provides one URL-backed workbench with tenant dashboard and guarded destructive actions.
- Full tests, build, Ant Design lint, browser smoke, migration verification, and code review pass.
