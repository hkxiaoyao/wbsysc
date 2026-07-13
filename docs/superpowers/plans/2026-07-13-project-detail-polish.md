# Gateway reliability and direct mode implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add tenant-selectable MySQL and real-time WeCom data modes while fixing sync loss, production defaults, token exposure, and the broken test baseline.

**Architecture:** Keep the scheduler, WeCom clients, tenant schemas, and MCP tool names. Add `app/data_access.py` as the single MCP data-source boundary, store `data_mode` in `tenant_config`, and make sync cursor commits conditional on complete processing. Keep configuration, encrypted credentials, MCP tokens, and audit records in MySQL for both modes.

**Tech Stack:** Python 3.11+, FastAPI, MCP Python SDK, SQLAlchemy 2, Pydantic 2, PyMySQL, pytest, React 18, Ant Design 5, Vite 5, MySQL 5.7+

## Global constraints

- Keep MCP tool names and request parameters backward compatible
- Allow `data_mode` values `stored` and `direct`; default to `stored`
- Limit MCP list results to 1 through 100 records
- Never write approval, report, or check-in business rows in `direct` mode
- Keep tenant configuration, encrypted credentials, MCP tokens, and audit records in MySQL
- Do not silently fall back from `direct` to cached data
- Do not add Redis, a message queue, another database, or smart-table support
- Keep MySQL migrations compatible with MySQL 5.7
- Write tests before implementation and commit each task independently

---

## File map

- Create `app/data_access.py`: route MCP reads to MySQL or WeCom and normalize results
- Create `sql/004_gateway_hardening.sql`: idempotent MySQL 5.7 migration for mode and partial-record fields
- Create `tests/test_config.py`: production validation and SQLAlchemy URL tests
- Create `tests/test_tenant_modes.py`: tenant mode loading, auth context, and scheduler skip tests
- Create `tests/test_data_access.py`: stored/direct contract and direct failure tests
- Create `tests/test_sync_reliability.py`: window splitting, partial records, and cursor tests
- Create `tests/test_admin_security.py`: token redaction and explicit token access tests
- Modify `app/config.py`: production validation and URL-safe database configuration
- Modify `app/crypto.py`: retain development fallback while relying on production validation
- Modify `app/tenant.py`: load `data_mode` into tenant context
- Modify `app/auth.py`: expose direct-mode runtime fields through request context
- Modify `app/wecom/sync.py`: allow early result limits for direct list calls
- Modify `app/wecom/approval_sync.py`: allow early result limits for direct list calls
- Modify `app/data_access.py`: implement the data-source adapters
- Modify `app/mcp_server.py`: delegate reads to the data-access boundary
- Modify `app/wecom/dispatch.py`: skip direct tenants and commit cursors only after complete processing
- Modify `app/db.py`: persist and query partial-record source windows
- Modify `app/admin.py`: manage `data_mode`, redact tokens, and reject sync for direct tenants
- Modify `app/tenant_init.py`: initialize and migrate `data_mode`
- Modify `admin-ui/src/pages/Tenants.jsx`: expose mode selection and explicit MCP configuration retrieval
- Modify `.env.prod.example`, `README.md`, `docs/部署指南.md`: document safe production and data-mode behavior
- Modify `tests/test_smoke_client.py`: stop import-time execution and allow environment configuration

### Task 1: Restore the test baseline and reject unsafe production configuration

**Files:**
- Create: `tests/test_config.py`
- Modify: `tests/test_smoke_client.py`
- Modify: `app/config.py`
- Modify: `app/crypto.py`
- Modify: `.env.prod.example`

**Interfaces:**
- Produces: `Settings.validate_production()` through a Pydantic `model_validator`
- Produces: `Settings.db_url: sqlalchemy.engine.URL`
- Produces: import-safe `tests.test_smoke_client`

- [ ] **Step 1: Protect the manual smoke-test entry point**

Replace the hard-coded endpoint constants and bottom-level execution in `tests/test_smoke_client.py`:

```python
SERVER = os.getenv("MCP_SMOKE_SERVER", "http://localhost:8000/mcp")
TOKEN = os.getenv("MCP_SMOKE_TOKEN", "test-token")
BAD_TOKEN = os.getenv("MCP_SMOKE_BAD_TOKEN", "wrong-token")

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify pytest can collect without opening a network connection**

Run: `.\.venv\Scripts\python.exe -m pytest --collect-only -q`

Expected: collection completes without `httpcore.ConnectError`.

- [ ] **Step 3: Write failing production configuration tests**

Create `tests/test_config.py`:

```python
import pytest
from pydantic import ValidationError
from sqlalchemy.engine import URL

from app.config import Settings


def prod_settings(**overrides):
    values = {
        "app_env": "prod",
        "credential_key": "k" * 32,
        "admin_password": "admin-password-123",
        "db_password": "db-password-123",
        "wecom_use_mock": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("credential_key", "", "CREDENTIAL_KEY"),
        ("admin_password", "CHANGE_ME", "ADMIN_PASSWORD"),
        ("db_password", "", "DB_PASSWORD"),
        ("wecom_use_mock", True, "WECOM_USE_MOCK"),
    ],
)
def test_prod_rejects_unsafe_values(field, value, message):
    with pytest.raises(ValidationError, match=message):
        prod_settings(**{field: value})


def test_dev_keeps_mock_and_empty_key_fallback():
    settings = Settings(app_env="dev", wecom_use_mock=True, credential_key="")
    assert settings.wecom_use_mock is True


def test_db_url_preserves_special_password_characters():
    settings = Settings(db_password="p@ss:#/word")
    assert isinstance(settings.db_url, URL)
    assert settings.db_url.password == "p@ss:#/word"
```

- [ ] **Step 4: Run the tests and verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py -q`

Expected: failures because `Settings` has no production validator and `db_url` is a string.

- [ ] **Step 5: Add production validation and a SQLAlchemy URL**

Add the imports and validator to `app/config.py`:

```python
from pydantic import model_validator
from sqlalchemy.engine import URL

EXAMPLE_PASSWORDS = {"CHANGE_ME", "<强密码，与开发库不同>", "<强密码，登录管理后台用>"}

@model_validator(mode="after")
def validate_production(self):
    if self.app_env.lower() != "prod":
        return self
    errors = []
    if not self.credential_key:
        errors.append("CREDENTIAL_KEY must be set in production")
    if not self.admin_password or self.admin_password in EXAMPLE_PASSWORDS:
        errors.append("ADMIN_PASSWORD must be a non-example value in production")
    if not self.db_password or self.db_password in EXAMPLE_PASSWORDS:
        errors.append("DB_PASSWORD must be a non-example value in production")
    if self.wecom_use_mock:
        errors.append("WECOM_USE_MOCK must be false in production")
    if errors:
        raise ValueError("; ".join(errors))
    return self

@property
def db_url(self) -> URL:
    return URL.create(
        "mysql+pymysql",
        username=self.db_user,
        password=self.db_password,
        host=self.db_host,
        port=self.db_port,
        database=self.db_name,
        query={"charset": "utf8mb4"},
    )
```

Place both methods inside `Settings`. Keep the fixed key fallback in `app/crypto.py` for development and change its docstring to state that production validation blocks it.

- [ ] **Step 6: Set the production example to real-data mode**

Add to `.env.prod.example`:

```dotenv
WECOM_USE_MOCK=false
```

- [ ] **Step 7: Run focused and collection tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py -q`

Expected: all tests pass.

Run: `.\.venv\Scripts\python.exe -m pytest --collect-only -q`

Expected: collection succeeds with no network request.

- [ ] **Step 8: Commit the configuration baseline**

```powershell
git add app/config.py app/crypto.py tests/test_config.py tests/test_smoke_client.py .env.prod.example
git commit -m "fix: enforce safe production configuration"
```

### Task 2: Persist tenant data mode and expose it in runtime context

**Files:**
- Create: `sql/004_gateway_hardening.sql`
- Create: `tests/test_tenant_modes.py`
- Modify: `app/tenant.py`
- Modify: `app/auth.py`
- Modify: `app/tenant_init.py`
- Modify: `app/wecom/dispatch.py`

**Interfaces:**
- Produces: `_TenantCtx.data_mode: Literal["stored", "direct"]`
- Produces: `TenantCtx.secret`, `contact_secret`, `checkin_userids`, `enabled_modules`, and `data_mode`
- Produces: direct-tenant scheduler result `{"skipped": "direct_mode"}`

- [ ] **Step 1: Write failing tenant-mode tests**

Create `tests/test_tenant_modes.py`:

```python
from app.tenant import _TenantCtx
from app.wecom import dispatch


def tenant(mode="stored"):
    return _TenantCtx(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="secret",
        schema_name="wbd_123",
        sync_interval_min=30,
        enabled_modules={"report"},
        checkin_userids=[],
        contact_secret="",
        data_mode=mode,
    )


def test_direct_tenant_is_not_synchronized(monkeypatch):
    monkeypatch.setattr(dispatch, "get_all_tenants", lambda: [tenant("direct")])
    monkeypatch.setattr(
        dispatch,
        "run_sync_tenant",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not sync")),
    )
    assert dispatch.run_sync_all() == {"tenant-a": {"skipped": "direct_mode"}}


def test_stored_tenant_runs_sync(monkeypatch):
    monkeypatch.setattr(dispatch, "get_all_tenants", lambda: [tenant("stored")])
    monkeypatch.setattr(dispatch, "run_sync_tenant", lambda *args, **kwargs: {"report": {"stored": 1}})
    assert dispatch.run_sync_all()["tenant-a"]["report"]["stored"] == 1
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_tenant_modes.py -q`

Expected: `_TenantCtx` rejects `data_mode` or the direct tenant reaches `run_sync_tenant`.

- [ ] **Step 3: Add an idempotent MySQL 5.7 mode migration**

Start `sql/004_gateway_hardening.sql` with an `information_schema` check:

```sql
SET @db := DATABASE();
SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=@db AND TABLE_NAME='tenant_config' AND COLUMN_NAME='data_mode'
);
SET @sql := IF(
  @exists=0,
  "ALTER TABLE tenant_config ADD COLUMN data_mode VARCHAR(16) NOT NULL DEFAULT 'stored' COMMENT 'stored=MySQL缓存,direct=企微实时'",
  "SELECT 1"
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
```

- [ ] **Step 4: Load and propagate `data_mode`**

Add this field to `app/tenant.py`:

```python
from typing import Literal

data_mode: Literal["stored", "direct"] = "stored"
```

Select `IFNULL(data_mode, 'stored')`, normalize unknown values to `stored`, and pass the result to `_TenantCtx`.

Extend `app/auth.py`:

```python
@dataclass
class TenantCtx:
    tenant_id: str
    corpid: str
    secret: str
    schema_name: str
    contact_secret: str
    checkin_userids: list[str]
    enabled_modules: set[str]
    data_mode: str
```

Build this context from the cached `_TenantCtx` inside `BearerTokenMiddleware`.

- [ ] **Step 5: Initialize the column for new and existing databases**

Add to `_TENANT_CONFIG_DDL` and `wanted_cols` in `app/tenant_init.py`:

```python
"data_mode": "ADD COLUMN data_mode VARCHAR(16) NOT NULL DEFAULT 'stored'",
```

Add `data_mode: str = "stored"` to `init_tenant`, validate it against `{"stored", "direct"}`, and include it in the insert and update SQL.

- [ ] **Step 6: Skip direct tenants in scheduled sync**

Add this branch before `run_sync_tenant` in `run_sync_all`:

```python
if t.data_mode == "direct":
    logger.info("跳过直连租户 tenant=%s", t.tenant_id)
    result[t.tenant_id] = {"skipped": "direct_mode"}
    continue
```

- [ ] **Step 7: Run the tenant-mode tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_tenant_modes.py -q`

Expected: both tests pass.

- [ ] **Step 8: Commit tenant mode persistence**

```powershell
git add sql/004_gateway_hardening.sql app/tenant.py app/auth.py app/tenant_init.py app/wecom/dispatch.py tests/test_tenant_modes.py
git commit -m "feat: add tenant data modes"
```

### Task 3: Add the MySQL and WeCom data-access boundary

**Files:**
- Create: `app/data_access.py`
- Create: `tests/test_data_access.py`
- Modify: `app/wecom/sync.py`
- Modify: `app/wecom/approval_sync.py`

**Interfaces:**
- Consumes: `auth.TenantCtx`
- Produces: `list_reports(ctx, starttime, endtime, limit) -> dict`
- Produces: `get_report(ctx, journaluuid) -> dict`
- Produces: `list_approvals(ctx, starttime, endtime, limit) -> dict`
- Produces: `get_approval(ctx, sp_no) -> dict`
- Produces: `list_checkins(ctx, starttime, endtime, limit) -> dict`

- [ ] **Step 1: Write failing contract tests**

Create `tests/test_data_access.py` with a reusable context and direct partial-detail case:

```python
from app.auth import TenantCtx
from app import data_access


def ctx(mode):
    return TenantCtx(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="secret",
        schema_name="wbd_123",
        contact_secret="contact",
        checkin_userids=["user-a"],
        enabled_modules={"report", "approval", "checkin"},
        data_mode=mode,
    )


def test_stored_reports_use_database(monkeypatch):
    monkeypatch.setattr(
        data_access.db,
        "query_reports_by_window",
        lambda schema, start, end, limit: [{
            "journaluuid": "r1", "template_id": "t1", "template_name": "日报",
            "report_time": 100, "submitter_userid": "u1", "is_partial": 0,
        }],
    )
    result = data_access.list_reports(ctx("stored"), 1, 200, 20)
    assert result["source"] == "db"
    assert result["records"][0]["journaluuid"] == "r1"


def test_direct_reports_keep_failed_detail_as_partial(monkeypatch):
    monkeypatch.setattr(data_access, "sync_reports_window", lambda *args, **kwargs: ["r1", "r2"])
    monkeypatch.setattr(
        data_access,
        "fetch_report_detail",
        lambda corpid, secret, value: {"journaluuid": "r1", "report_time": 100}
        if value == "r1" else {"errcode": 40001, "errmsg": "invalid credential"},
    )
    result = data_access.list_reports(ctx("direct"), 1, 200, 20)
    assert result["source"] == "wecom"
    assert result["partial_count"] == 1
    assert result["records"][1]["_partial"] is True


def test_direct_limit_is_bounded_to_100(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        data_access,
        "sync_reports_window",
        lambda *args, **kwargs: captured.setdefault("limit", kwargs["max_records"]) and [],
    )
    data_access.list_reports(ctx("direct"), 1, 200, 1000)
    assert captured["limit"] == 100


def test_direct_approval_detail_failure_is_partial(monkeypatch):
    monkeypatch.setattr(data_access, "sync_approvals_window", lambda *args, **kwargs: ["sp1"])
    monkeypatch.setattr(
        data_access,
        "fetch_approval_detail",
        lambda *args: {"errcode": 301055, "errmsg": "not authorized"},
    )
    result = data_access.list_approvals(ctx("direct"), 1, 200, 20)
    assert result["partial_count"] == 1
    assert result["records"][0]["sp_no"] == "sp1"


def test_direct_checkins_require_userids(monkeypatch):
    context = ctx("direct")
    context.contact_secret = ""
    context.checkin_userids = []
    with pytest.raises(ValueError, match="userid"):
        data_access.list_checkins(context, 1, 200, 20)


def test_direct_reports_do_not_query_database(monkeypatch):
    monkeypatch.setattr(
        data_access.db,
        "query_reports_by_window",
        lambda *args: (_ for _ in ()).throw(AssertionError("database read is forbidden")),
    )
    monkeypatch.setattr(data_access, "sync_reports_window", lambda *args, **kwargs: [])
    result = data_access.list_reports(ctx("direct"), 1, 200, 20)
    assert result["source"] == "wecom"
```

Import `pytest` at the top of the file for the configuration-error assertion.

- [ ] **Step 2: Run the tests and verify import failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_data_access.py -q`

Expected: collection fails because `app.data_access` does not exist.

- [ ] **Step 3: Add bounded list collection to the WeCom helpers**

Add `max_records: int | None = None` to `sync_reports_window` and `sync_approvals_window`. After appending a unique identifier, stop and return when the result reaches `max_records`:

```python
if max_records is not None and len(result) >= max_records:
    return result
```

Keep `max_records=None` as the stored-sync behavior so scheduled sync still retrieves the full window.

- [ ] **Step 4: Implement the data-access module**

Create `app/data_access.py` with these public functions and shared validation:

```python
from __future__ import annotations

from . import db
from .auth import TenantCtx
from .wecom.approval_sync import fetch_approval_detail, sync_approvals_window
from .wecom.checkin_sync import fetch_checkin_records
from .wecom.contact import fetch_all_userids
from .wecom.sync import fetch_report_detail, sync_reports_window


def _limit(value: int) -> int:
    return max(1, min(int(value or 100), 100))


def _wecom_partial(identifier: str, response: dict) -> dict:
    return {
        "id": identifier,
        "_partial": True,
        "errcode": response.get("errcode"),
        "errmsg": response.get("errmsg", "detail unavailable"),
    }


def list_reports(ctx: TenantCtx, starttime: int, endtime: int, limit: int) -> dict:
    size = _limit(limit)
    if ctx.data_mode == "stored":
        rows = db.query_reports_by_window(ctx.schema_name, starttime, endtime, size)
        records = [{
            "journaluuid": row["journaluuid"],
            "template_id": row["template_id"],
            "template_name": row["template_name"],
            "report_time": row["report_time"],
            "submitter": row["submitter_userid"],
            "_partial": bool(row.get("is_partial", 0)),
        } for row in rows]
        return {"tenant": ctx.tenant_id, "source": "db", "count": len(records), "records": records, "partial_count": sum(bool(r["_partial"]) for r in records)}

    identifiers = sync_reports_window(
        ctx.corpid, ctx.secret, starttime, endtime, max_records=size
    )
    records = []
    for identifier in identifiers:
        detail = fetch_report_detail(ctx.corpid, ctx.secret, identifier)
        if detail.get("errcode") not in (None, 0):
            partial = _wecom_partial(identifier, detail)
            partial["journaluuid"] = identifier
            records.append(partial)
            continue
        records.append(detail)
    partial_count = sum(bool(record.get("_partial")) for record in records)
    return {"tenant": ctx.tenant_id, "source": "wecom", "count": len(records), "records": records, "partial_count": partial_count}
```

Add the remaining public functions with explicit database and WeCom branches:

```python
def get_report(ctx: TenantCtx, journaluuid: str) -> dict:
    if ctx.data_mode == "stored":
        detail = db.get_report_detail(ctx.schema_name, journaluuid)
        if not detail:
            return {"source": "db", "errcode": 404, "errmsg": "汇报单号不存在"}
        return {"source": "db", "detail": detail}
    detail = fetch_report_detail(ctx.corpid, ctx.secret, journaluuid)
    if detail.get("errcode") not in (None, 0):
        return {"source": "wecom", **detail}
    return {"source": "wecom", "detail": detail}


def list_approvals(ctx: TenantCtx, starttime: int, endtime: int, limit: int) -> dict:
    size = _limit(limit)
    if ctx.data_mode == "stored":
        rows = db.query_approvals_by_window(ctx.schema_name, starttime, endtime, size)
        records = [{
            "sp_no": row["sp_no"],
            "sp_name": row["sp_name"],
            "sp_status": row["sp_status"],
            "template_id": row["template_id"],
            "apply_time": row["apply_time"],
            "applyer": row["applyer_userid"],
            "_partial": bool(row.get("is_partial", 0)),
        } for row in rows]
        return {"tenant": ctx.tenant_id, "source": "db", "count": len(records), "records": records, "partial_count": sum(bool(r["_partial"]) for r in records)}
    identifiers = sync_approvals_window(
        ctx.corpid, ctx.secret, starttime, endtime, max_records=size
    )
    records = []
    for identifier in identifiers:
        detail = fetch_approval_detail(ctx.corpid, ctx.secret, identifier)
        if detail.get("errcode") not in (None, 0):
            partial = _wecom_partial(identifier, detail)
            partial["sp_no"] = identifier
            records.append(partial)
            continue
        applyer = detail.get("applyer") or {}
        records.append({
            "sp_no": detail.get("sp_no", identifier),
            "sp_name": detail.get("sp_name", ""),
            "sp_status": detail.get("sp_status", 0),
            "template_id": detail.get("template_id", ""),
            "apply_time": detail.get("apply_time", 0),
            "applyer": applyer.get("userid", "") if isinstance(applyer, dict) else applyer,
            "_partial": False,
        })
    partial_count = sum(bool(record.get("_partial")) for record in records)
    return {"tenant": ctx.tenant_id, "source": "wecom", "count": len(records), "records": records, "partial_count": partial_count}


def get_approval(ctx: TenantCtx, sp_no: str) -> dict:
    if ctx.data_mode == "stored":
        detail = db.get_approval_detail(ctx.schema_name, sp_no)
        if not detail:
            return {"source": "db", "errcode": 404, "errmsg": "审批单号不存在"}
        return {"source": "db", "detail": detail}
    detail = fetch_approval_detail(ctx.corpid, ctx.secret, sp_no)
    if detail.get("errcode") not in (None, 0):
        return {"source": "wecom", **detail}
    return {"source": "wecom", "detail": detail}


def list_checkins(ctx: TenantCtx, starttime: int, endtime: int, limit: int) -> dict:
    size = _limit(limit)
    if ctx.data_mode == "stored":
        rows = db.query_checkins_by_window(ctx.schema_name, starttime, endtime, size)
        records = [{
            "userid": row["userid"],
            "checkin_type": row["checkin_type"],
            "checkin_time": row["checkin_time"],
            "exception_type": row["exception_type"],
            "location_title": row["location_title"],
            "group_name": row["group_name"],
        } for row in rows]
        return {"tenant": ctx.tenant_id, "source": "db", "count": len(records), "records": records, "partial_count": 0}
    userids = fetch_all_userids(ctx.corpid, ctx.contact_secret) if ctx.contact_secret else list(ctx.checkin_userids)
    if not userids:
        raise ValueError("直连打卡需要通讯录 Secret 或手工 userid")
    records = fetch_checkin_records(ctx.corpid, ctx.secret, starttime, endtime, userids)
    records.sort(key=lambda record: int(record.get("checkin_time", 0) or 0), reverse=True)
    selected = records[:size]
    return {"tenant": ctx.tenant_id, "source": "wecom", "count": len(selected), "records": selected, "partial_count": 0}
```

- [ ] **Step 5: Run the data-access tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_data_access.py -q`

Expected: all stored, direct, partial, and limit tests pass.

- [ ] **Step 6: Commit the data-access boundary**

```powershell
git add app/data_access.py app/wecom/sync.py app/wecom/approval_sync.py tests/test_data_access.py
git commit -m "feat: route MCP reads by tenant mode"
```

### Task 4: Delegate MCP tools to the data-access boundary

**Files:**
- Create: `tests/test_mcp_tools.py`
- Modify: `app/mcp_server.py`

**Interfaces:**
- Consumes: all five public functions from `app.data_access`
- Produces: unchanged MCP tool names and JSON string return values

- [ ] **Step 1: Write failing MCP delegation tests**

Create `tests/test_mcp_tools.py`:

```python
import json

from app.auth import TenantCtx, _ctx
from app import mcp_server


def test_list_reports_delegates_to_data_access(monkeypatch):
    context = TenantCtx(
        tenant_id="tenant-a", corpid="ww123", secret="secret", schema_name="wbd_123",
        contact_secret="", checkin_userids=[], enabled_modules={"report"}, data_mode="direct",
    )
    token = _ctx.set(context)
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(
        mcp_server.data_access,
        "list_reports",
        lambda ctx, start, end, limit: {"tenant": ctx.tenant_id, "source": "wecom", "count": 0, "records": [], "partial_count": 0},
    )
    monkeypatch.setattr(mcp_server, "_audit", lambda *args: None)
    try:
        result = json.loads(mcp_server.wecom_list_reports(1, 2, 10))
    finally:
        _ctx.reset(token)
    assert result["source"] == "wecom"


@pytest.mark.parametrize(
    ("tool_name", "accessor_name", "args"),
    [
        ("wecom_get_report", "get_report", ("r1",)),
        ("wecom_list_approvals", "list_approvals", (1, 2, 10)),
        ("wecom_get_approval_detail", "get_approval", ("sp1",)),
        ("wecom_list_checkins", "list_checkins", (1, 2, 10)),
    ],
)
def test_real_tools_delegate(monkeypatch, tool_name, accessor_name, args):
    context = TenantCtx(
        tenant_id="tenant-a", corpid="ww123", secret="secret", schema_name="wbd_123",
        contact_secret="", checkin_userids=[], enabled_modules={"report"}, data_mode="direct",
    )
    token = _ctx.set(context)
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(
        getattr(mcp_server, "data_access"),
        accessor_name,
        lambda *values: {"tenant": "tenant-a", "source": "wecom", "count": 0, "records": []},
    )
    monkeypatch.setattr(mcp_server, "_audit", lambda *values: None)
    try:
        result = json.loads(getattr(mcp_server, tool_name)(*args))
    finally:
        _ctx.reset(token)
    assert result["source"] == "wecom"


def test_mock_list_bypasses_data_access(monkeypatch):
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: True)
    monkeypatch.setattr(
        mcp_server.data_access,
        "list_reports",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not call data access")),
    )
    token = _ctx.set(TenantCtx(
        tenant_id="tenant-a", corpid="ww123", secret="", schema_name="wbd_123",
        contact_secret="", checkin_userids=[], enabled_modules={"report"}, data_mode="stored",
    ))
    try:
        result = json.loads(mcp_server.wecom_list_reports(1, 2, 10))
    finally:
        _ctx.reset(token)
    assert result["source"] == "mock"
```

Import `pytest` beside `json` for the parameterized delegation test.

- [ ] **Step 2: Run the tests and verify delegation is absent**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_mcp_tools.py -q`

Expected: failure because `mcp_server.data_access` is not imported or tools still query `db`.

- [ ] **Step 3: Refactor each tool to one data-access call**

Import the boundary once:

```python
from . import data_access
```

Use this structure in every real-data branch:

```python
ctx = current_ctx()
try:
    result = data_access.list_reports(ctx, starttime, endtime, limit)
    status = "partial" if result.get("partial_count") else "ok"
except Exception as exc:
    result = {"tenant": ctx.tenant_id, "source": "wecom" if ctx.data_mode == "direct" else "db", "errcode": 502, "errmsg": str(exc)}
    status = "error"
_audit("wecom_list_reports", "", f"{starttime}-{endtime}#{limit}", status, int((time.time() - t0) * 1000))
return _ok(result)
```

Add one helper and use it in all five real-data branches:

```python
def _run_real(tool, target, params, started_at, call):
    ctx = current_ctx()
    try:
        result = call(ctx)
        status = "partial" if result.get("partial_count") else ("error" if result.get("errcode") else "ok")
    except Exception as exc:
        result = {
            "tenant": ctx.tenant_id,
            "source": "wecom" if ctx.data_mode == "direct" else "db",
            "errcode": 502,
            "errmsg": str(exc),
        }
        status = "error"
    _audit(tool, target, params, status, int((time.time() - started_at) * 1000))
    return _ok(result)


# Real-data return statements inside the existing tool functions:
return _run_real(
    "wecom_list_reports", "", f"{starttime}-{endtime}#{limit}", t0,
    lambda ctx: data_access.list_reports(ctx, starttime, endtime, limit),
)
return _run_real(
    "wecom_get_report", journaluuid, journaluuid, t0,
    lambda ctx: data_access.get_report(ctx, journaluuid),
)
return _run_real(
    "wecom_list_approvals", "", f"{starttime}-{endtime}#{limit}", t0,
    lambda ctx: data_access.list_approvals(ctx, starttime, endtime, limit),
)
return _run_real(
    "wecom_get_approval_detail", sp_no, sp_no, t0,
    lambda ctx: data_access.get_approval(ctx, sp_no),
)
return _run_real(
    "wecom_list_checkins", "", f"{starttime}-{endtime}#{limit}", t0,
    lambda ctx: data_access.list_checkins(ctx, starttime, endtime, limit),
)
```

Place each return statement in its matching tool after the unchanged mock branch; do not place the five returns consecutively in one function.

- [ ] **Step 4: Log audit failures without changing tool results**

Add a module logger and replace the empty audit exception branch:

```python
except Exception as exc:
    logger.warning("MCP audit write failed tool=%s: %s", tool, type(exc).__name__)
```

- [ ] **Step 5: Run MCP and data-access tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_mcp_tools.py tests/test_data_access.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit MCP delegation**

```powershell
git add app/mcp_server.py tests/test_mcp_tools.py
git commit -m "refactor: isolate MCP data sources"
```

### Task 5: Make stored synchronization complete and queryable

**Files:**
- Create: `tests/test_sync_reliability.py`
- Modify: `sql/004_gateway_hardening.sql`
- Modify: `sql/001_init.sql`
- Modify: `app/db.py`
- Modify: `app/wecom/dispatch.py`

**Interfaces:**
- Produces: `_bounded_windows(fetch, starttime, endtime, limit=500) -> Iterator[tuple[int, int, list]]`
- Produces: `upsert_report(..., source_window: tuple[int, int] | None = None)`
- Produces: `upsert_approval(..., source_window: tuple[int, int] | None = None)`
- Produces: query rows with `is_partial`

- [ ] **Step 1: Write failing window and cursor tests**

Create `tests/test_sync_reliability.py`:

```python
from app.tenant import _TenantCtx
from app.wecom import dispatch


def test_bounded_windows_processes_every_identifier():
    calls = []
    def fetch(start, end):
        calls.append((start, end))
        count = 600 if end - start > 60 else 300
        return [f"{start}-{index}" for index in range(count)]
    batches = list(dispatch._bounded_windows(fetch, 0, 120, limit=500))
    assert sum(len(items) for _, _, items in batches) == 600
    assert all(len(items) <= 500 for _, _, items in batches)


def test_force_does_not_reset_cursor(monkeypatch):
    tenant = _TenantCtx("t", "ww", "s", "wbd_x", 30, set(), [], "", "stored")
    reset_calls = []
    monkeypatch.setattr(dispatch, "reset_cursors", lambda *args: reset_calls.append(args))
    dispatch.run_sync_tenant(tenant, force=True, reset_cursor=False)
    assert reset_calls == []


def test_reset_cursor_persists_reset(monkeypatch):
    tenant = _TenantCtx("t", "ww", "s", "wbd_x", 30, set(), [], "", "stored")
    reset_calls = []
    monkeypatch.setattr(dispatch, "reset_cursors", lambda *args: reset_calls.append(args))
    dispatch.run_sync_tenant(tenant, force=False, reset_cursor=True)
    assert len(reset_calls) == 1


def report_tenant():
    return _TenantCtx("t", "ww", "s", "wbd_x", 30, {"report"}, [], "", "stored")


def test_report_sync_processes_all_identifiers(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (0, 120))
    monkeypatch.setattr(
        dispatch,
        "sync_reports_window",
        lambda corpid, secret, start, end: [f"{start}-{index}" for index in range(700 if end - start > 60 else 350)],
    )
    monkeypatch.setattr(dispatch, "fetch_report_detail", lambda *args: {"journaluuid": args[-1], "report_time": 10})
    stored = []
    cursors = []
    monkeypatch.setattr(dispatch.db, "upsert_report", lambda schema, identifier, info, source_window=None: stored.append(identifier))
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: cursors.append(args))
    result = dispatch._sync_one_report(report_tenant(), 30)
    assert len(set(stored)) == 700
    assert result["stored"] == 700
    assert len(cursors) == 1


def test_report_write_failure_blocks_cursor(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (0, 60))
    monkeypatch.setattr(dispatch, "sync_reports_window", lambda *args: ["r1"])
    monkeypatch.setattr(dispatch, "fetch_report_detail", lambda *args: {"journaluuid": "r1", "report_time": 10})
    monkeypatch.setattr(dispatch.db, "upsert_report", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db down")))
    cursors = []
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: cursors.append(args))
    result = dispatch._sync_one_report(report_tenant(), 30)
    assert result["write_err"] == 1
    assert cursors == []


def test_partial_report_keeps_source_window(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (10, 70))
    monkeypatch.setattr(dispatch, "sync_reports_window", lambda *args: ["r1"])
    monkeypatch.setattr(dispatch, "fetch_report_detail", lambda *args: {"errcode": 40001, "errmsg": "bad secret"})
    captured = {}
    monkeypatch.setattr(
        dispatch.db,
        "upsert_report",
        lambda schema, identifier, info, source_window=None: captured.update(info=info, source_window=source_window),
    )
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: None)
    dispatch._sync_one_report(report_tenant(), 30)
    assert captured["info"]["_partial"] is True
    assert captured["source_window"] == (10, 70)
```

Add the approval cases explicitly:

```python
def approval_tenant():
    return _TenantCtx("t", "ww", "s", "wbd_x", 30, {"approval"}, [], "", "stored")


def test_approval_write_failure_blocks_cursor(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (0, 60))
    monkeypatch.setattr(dispatch, "sync_approvals_window", lambda *args: ["sp1"])
    monkeypatch.setattr(dispatch, "fetch_approval_detail", lambda *args: {"sp_no": "sp1", "apply_time": 10})
    monkeypatch.setattr(dispatch.db, "upsert_approval", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db down")))
    cursors = []
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: cursors.append(args))
    result = dispatch._sync_one_approval(approval_tenant(), 30)
    assert result["write_err"] == 1
    assert cursors == []


def test_partial_approval_keeps_source_window(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (10, 70))
    monkeypatch.setattr(dispatch, "sync_approvals_window", lambda *args: ["sp1"])
    monkeypatch.setattr(dispatch, "fetch_approval_detail", lambda *args: {"errcode": 301055, "errmsg": "forbidden"})
    captured = {}
    monkeypatch.setattr(
        dispatch.db,
        "upsert_approval",
        lambda schema, identifier, info, source_window=None: captured.update(info=info, source_window=source_window),
    )
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: None)
    dispatch._sync_one_approval(approval_tenant(), 30)
    assert captured["info"]["_partial"] is True
    assert captured["source_window"] == (10, 70)
```

- [ ] **Step 2: Run reliability tests and verify failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sync_reliability.py -q`

Expected: `_bounded_windows` is missing, force resets the cursor, and current sync truncates at 500.

- [ ] **Step 3: Add partial-record fields to migration and runtime DDL**

For both `wecom_report` and `wecom_approval`, add these MySQL 5.7 fields through repeated `information_schema` checks in `sql/004_gateway_hardening.sql`:

```sql
source_window_start BIGINT NOT NULL DEFAULT 0,
source_window_end BIGINT NOT NULL DEFAULT 0,
is_partial TINYINT NOT NULL DEFAULT 0
```

Use one procedure to update every tenant schema listed in `tenant_config`:

```sql
DROP PROCEDURE IF EXISTS migrate_gateway_business_columns;
DELIMITER //
CREATE PROCEDURE migrate_gateway_business_columns()
BEGIN
  DECLARE done INT DEFAULT 0;
  DECLARE schema_value VARCHAR(64);
  DECLARE table_value VARCHAR(64);
  DECLARE column_value VARCHAR(64);
  DECLARE definition_value VARCHAR(255);
  DECLARE column_exists INT DEFAULT 0;
  DECLARE columns_cursor CURSOR FOR
    SELECT DISTINCT tc.schema_name, defs.table_name, defs.column_name, defs.definition
    FROM tenant_config tc
    JOIN (
      SELECT 'wecom_report' table_name, 'source_window_start' column_name, 'BIGINT NOT NULL DEFAULT 0' definition
      UNION ALL SELECT 'wecom_report', 'source_window_end', 'BIGINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_report', 'is_partial', 'TINYINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_approval', 'source_window_start', 'BIGINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_approval', 'source_window_end', 'BIGINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_approval', 'is_partial', 'TINYINT NOT NULL DEFAULT 0'
    ) defs ON 1=1
    WHERE tc.schema_name REGEXP '^wbd_[0-9A-Za-z_]+$';
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;

  OPEN columns_cursor;
  migration_loop: LOOP
    FETCH columns_cursor INTO schema_value, table_value, column_value, definition_value;
    IF done = 1 THEN
      LEAVE migration_loop;
    END IF;
    SELECT COUNT(*) INTO column_exists
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=schema_value AND TABLE_NAME=table_value AND COLUMN_NAME=column_value;
    IF column_exists = 0 THEN
      SET @ddl = CONCAT(
        'ALTER TABLE `', schema_value, '`.`', table_value,
        '` ADD COLUMN `', column_value, '` ', definition_value
      );
      PREPARE stmt FROM @ddl;
      EXECUTE stmt;
      DEALLOCATE PREPARE stmt;
    END IF;
  END LOOP;
  CLOSE columns_cursor;
END//
DELIMITER ;
CALL migrate_gateway_business_columns();
DROP PROCEDURE migrate_gateway_business_columns;
```

Add these exact columns after `detail_json` in both `sql/001_init.sql` table definitions and both `_BIZ_DDLS` definitions in `app/db.py`:

```sql
source_window_start BIGINT NOT NULL DEFAULT 0,
source_window_end BIGINT NOT NULL DEFAULT 0,
is_partial TINYINT NOT NULL DEFAULT 0,
```

- [ ] **Step 4: Persist and query source windows**

Change the upsert signatures in `app/db.py`:

```python
def upsert_report(schema, journaluuid, info, source_window=None):
    window_start, window_end = source_window or (0, 0)
    partial = 1 if info.get("_partial") else 0
```

Write `source_window_start`, `source_window_end`, and `is_partial` in report insert and update clauses. Use this approval setup before its SQL execution:

```python
def upsert_approval(schema, sp_no, info, source_window=None):
    window_start, window_end = source_window or (0, 0)
    partial = 1 if info.get("_partial") else 0
```

Bind `:ws`, `:we`, and `:partial` in both approval insert and update clauses with values `window_start`, `window_end`, and `partial`.

Change report window filtering to:

```sql
WHERE (
  is_partial=0 AND report_time>=:s AND report_time<:e
) OR (
  is_partial=1 AND source_window_start<:e AND source_window_end>:s
)
```

Use `apply_time` for the approval equivalent and include `is_partial` in both result sets.

- [ ] **Step 5: Implement complete bounded window processing**

Add to `app/wecom/dispatch.py`:

```python
MIN_WINDOW_SECONDS = 60


def _bounded_windows(fetch, starttime, endtime, limit=MAX_DETAIL_PER_RUN):
    items = fetch(starttime, endtime)
    if len(items) <= limit:
        yield starttime, endtime, items
        return
    if endtime - starttime <= MIN_WINDOW_SECONDS:
        for offset in range(0, len(items), limit):
            yield starttime, endtime, items[offset:offset + limit]
        return
    midpoint = starttime + (endtime - starttime) // 2
    yield from _bounded_windows(fetch, starttime, midpoint, limit)
    yield from _bounded_windows(fetch, midpoint, endtime, limit)
```

Use this generator in report and approval sync. Pass `(batch_start, batch_end)` to every upsert. Track `write_err` separately from detail failures and call `save_cursor` only when `write_err == 0`.

For check-ins, remove `records[:MAX_DETAIL_PER_RUN]`. Iterate over every 500-row slice and prevent cursor commit after any database write failure.

- [ ] **Step 6: Separate force and reset behavior**

Replace the beginning of `run_sync_tenant`:

```python
if reset_cursor:
    reset_cursors(t.schema_name, lookback_days, t.enabled_modules)
    force = True
```

- [ ] **Step 7: Run reliability and data-access tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sync_reliability.py tests/test_data_access.py -q`

Expected: all tests pass, including 700-record and write-failure cases.

- [ ] **Step 8: Commit synchronization hardening**

```powershell
git add sql/004_gateway_hardening.sql sql/001_init.sql app/db.py app/wecom/dispatch.py tests/test_sync_reliability.py
git commit -m "fix: prevent sync cursor data loss"
```

### Task 6: Protect tokens and expose mode controls in the admin UI

**Files:**
- Create: `tests/test_admin_security.py`
- Modify: `app/admin.py`
- Modify: `admin-ui/src/pages/Tenants.jsx`

**Interfaces:**
- Produces: tenant list fields `data_mode`, `has_mcp_token`, and `mcp_token_hint`
- Produces: authenticated `GET /admin/tenants/{tenant_id}/mcp-config`
- Produces: create-required and update-optional `mcp_token`

- [ ] **Step 1: Write failing serializer and direct-sync tests**

Create `tests/test_admin_security.py`:

```python
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import admin
from app import tenant as tenant_module


def test_tenant_item_redacts_token(monkeypatch):
    monkeypatch.setattr(admin, "get_verify_by_tenant", lambda tenant_id: None)
    row = (
        "tenant-a", "客户A", "ww123", "secret-token-1234", "wbd_123", 30,
        "report", "", 0, 1, 1, "created", "updated", "", "direct",
    )
    item = admin._tenant_item(row)
    assert "mcp_token" not in item
    assert item["has_mcp_token"] is True
    assert item["mcp_token_hint"] == "1234"
    assert item["data_mode"] == "direct"


def test_direct_tenant_cannot_trigger_sync(monkeypatch):
    request = SimpleNamespace(cookies={}, headers={"Authorization": "Bearer admin"})
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(tenant_module, "reload_tenants", lambda: None)
    monkeypatch.setattr(tenant_module, "get_all_tenants", lambda: [SimpleNamespace(tenant_id="tenant-a", data_mode="direct")])
    with pytest.raises(HTTPException) as exc:
        admin.trigger_sync("tenant-a", request)
    assert exc.value.status_code == 409


def test_mcp_config_requires_admin_session():
    request = SimpleNamespace(cookies={}, headers={})
    with pytest.raises(HTTPException) as exc:
        admin.get_mcp_config("tenant-a", request)
    assert exc.value.status_code == 401
```

Add the authenticated response test:

```python
def test_mcp_config_returns_token_after_auth(monkeypatch):
    class Result:
        def fetchone(self):
            return ("tenant-a", "token-1234", "mcp.example.com")

    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, traceback):
            return False
        def execute(self, statement, values):
            return Result()

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    request = SimpleNamespace(cookies={}, headers={})
    result = admin.get_mcp_config("tenant-a", request)
    assert result == {
        "tenant_id": "tenant-a",
        "mcp_token": "token-1234",
        "trusted_domain": "mcp.example.com",
    }
```

- [ ] **Step 2: Run tests and verify token exposure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_admin_security.py -q`

Expected: failure because `_tenant_item` returns `mcp_token` and has no `data_mode`.

- [ ] **Step 3: Extend tenant writes without rotating tokens accidentally**

Change `TenantUpsert`:

```python
from typing import Literal

mcp_token: str = ""
data_mode: Literal["stored", "direct"] = "stored"
```

In `create_tenant`, reject an empty Token with HTTP 400. In `update_tenant`, read the existing Token beside encrypted secrets and keep it when `body.mcp_token` is empty. Insert and update `data_mode` in SQL.

- [ ] **Step 4: Redact lists and add explicit configuration retrieval**

Change `_tenant_item` to return:

```python
"has_mcp_token": bool(r[3]),
"mcp_token_hint": r[3][-4:] if r[3] else "",
"data_mode": (r[14] if len(r) > 14 else "stored") or "stored",
```

Append the mode to the full list query and keep the legacy query without it:

```sql
SELECT tenant_id, display_name, corpid, mcp_token, schema_name,
       sync_interval_min, enabled_modules, checkin_userids,
       IFNULL(contact_secret_encrypted IS NOT NULL, 0) AS has_contact_secret,
       IFNULL(secret_encrypted IS NOT NULL, 0) AS has_secret,
       enabled, created_at, updated_at,
       IFNULL(trusted_domain, '') AS trusted_domain,
       IFNULL(data_mode, 'stored') AS data_mode
FROM tenant_config ORDER BY created_at
```

Add the authenticated endpoint:

```python
@router.get("/tenants/{tenant_id}/mcp-config")
def get_mcp_config(tenant_id: str, request: Request):
    _require_auth(request)
    with get_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT tenant_id, mcp_token, IFNULL(trusted_domain, '') FROM tenant_config WHERE tenant_id=:t"
        ), {"t": tenant_id}).fetchone()
    if not row:
        raise HTTPException(404, "租户不存在")
    return {"tenant_id": row[0], "mcp_token": row[1], "trusted_domain": row[2] or ""}
```

Reject `trigger_sync` and `sync_diagnose` with HTTP 409 when `t.data_mode == "direct"`.

- [ ] **Step 5: Add mode selection and explicit Token retrieval to the UI**

In `Tenants.jsx`, set create defaults:

```jsx
form.setFieldsValue({
  enabled_modules: MODULES,
  sync_interval_min: 30,
  enabled: true,
  data_mode: 'stored',
})
```

Add the form field:

```jsx
<Form.Item name="data_mode" label="数据模式" rules={[{ required: true }]}
  extra="缓存模式定时写入 MySQL；企微直连每次实时请求且不保存业务数据">
  <Select options={[
    { value: 'stored', label: '缓存模式（MySQL）' },
    { value: 'direct', label: '企微直连（不保存业务数据）' },
  ]} />
</Form.Item>
```

Fetch the full Token only inside `openMcpConfig`:

```jsx
const r = await api.get(`/admin/tenants/${row.tenant_id}/mcp-config`)
const text = JSON.stringify(buildMcpConfig({ ...row, ...r.data }), null, 2)
```

Disable sync-related buttons when `row.data_mode === 'direct'` and add a mode `Tag` column. Add `saving` and `mcpLoadingTenant` state so save and MCP configuration requests cannot be submitted twice.

- [ ] **Step 6: Run backend tests and build the admin UI**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_admin_security.py tests/test_tenant_modes.py -q`

Expected: all tests pass.

Run: `pnpm --dir admin-ui build`

Expected: Vite build succeeds. The existing chunk-size warning may remain.

- [ ] **Step 7: Commit admin security and mode controls**

```powershell
git add app/admin.py admin-ui/src/pages/Tenants.jsx tests/test_admin_security.py
git commit -m "feat: manage direct mode securely"
```

### Task 7: Align SQL, documentation, and final verification

**Files:**
- Modify: `sql/002_tenant_config.sql`
- Modify: `README.md`
- Modify: `docs/部署指南.md`
- Modify: `.ccg/tasks/project-detail-polish/review.md`

**Interfaces:**
- Consumes: completed behavior from Tasks 1 through 6
- Produces: deployable documentation and a review record

- [ ] **Step 1: Align the baseline SQL with runtime fields**

Update `sql/002_tenant_config.sql` so a new database includes these fields without relying on runtime repair:

```sql
`enabled_modules` VARCHAR(64) NOT NULL DEFAULT 'report,approval,checkin',
`checkin_userids` TEXT NULL,
`contact_secret_encrypted` VARBINARY(512) NULL,
`trusted_domain` VARCHAR(255) NOT NULL DEFAULT '',
`data_mode` VARCHAR(16) NOT NULL DEFAULT 'stored',
```

Keep `sql/004_gateway_hardening.sql` as the idempotent upgrade path for existing databases.

- [ ] **Step 2: Document both data modes and safe production startup**

Add to `README.md` and `docs/部署指南.md`:

- `stored` reads MySQL and runs scheduled synchronization
- `direct` calls WeCom for each MCP request and does not write business tables
- both modes retain tenant configuration, encrypted credentials, Token, and audit data in MySQL
- direct failures do not fall back to cached records
- production requires `WECOM_USE_MOCK=false`, `CREDENTIAL_KEY`, `ADMIN_PASSWORD`, and `DB_PASSWORD`
- existing tenants remain in `stored` after migration

Use this exact behavior summary in both documents:

```markdown
### 数据读取模式

每个租户可选择 `stored` 或 `direct`。`stored` 定时把业务数据同步到租户 MySQL schema，MCP 查询本地表。`direct` 在每次 MCP 调用时请求企微 API，不写入审批、汇报或打卡业务表，也不参加后台同步。

两种模式都在 MySQL 保存租户配置、加密凭证、MCP Token 和审计日志。直连请求失败时会返回企微错误，不读取历史缓存。现有租户升级后保持 `stored`。
```

- [ ] **Step 3: Run the complete verification suite**

Run: `.\.venv\Scripts\python.exe -m compileall -q app`

Expected: exit code 0.

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: all tests pass with no collection error or live network dependency.

Run: `pnpm --dir admin-ui build`

Expected: build succeeds.

Run: `docker compose config -q`

Expected: exit code 0.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 4: Run dual-model review and record availability**

Run Gemini and Claude reviewer roles in parallel against `git diff HEAD~6..HEAD`. Record Critical, Warning, and Info findings in `.ccg/tasks/project-detail-polish/review.md`. If Gemini still lacks `GEMINI_API_KEY`, record the failed command and continue with Claude plus local verification; do not claim a completed Gemini review.

- [ ] **Step 5: Fix every Critical finding and rerun verification**

For each Critical finding, add a focused regression test, make the smallest fix, and rerun the command from Step 3 that exercises the changed area. Repeat the review when a Critical issue changes behavior.

- [ ] **Step 6: Commit documentation and review results**

```powershell
git add sql/002_tenant_config.sql README.md docs/部署指南.md .ccg/tasks/project-detail-polish/review.md
git commit -m "docs: complete gateway hardening rollout"
```

- [ ] **Step 7: Confirm the final diff stays within scope**

Run: `git diff --stat 6b7ad9a..HEAD`

Expected: only files named in this plan and generated frontend assets already covered by the existing build workflow.

Run: `git status --short`

Expected: clean worktree before CCG task archival.
