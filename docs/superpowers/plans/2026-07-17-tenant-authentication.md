# Tenant Single-Account Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a database-backed tenant login using tenant ID and password while keeping platform-admin authentication isolated and backward compatible.

**Architecture:** Introduce a focused `tenant_auth` package with Argon2id password hashing, digest-only database sessions, tenant-scoped dependencies, and a `/tenant/**` router. Platform-admin tenant CRUD provisions or resets the single tenant account through domain functions rather than sharing admin sessions.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic 2, SQLAlchemy 2, MySQL 5.7, argon2-cffi, pytest.

**Depends on:** None.
**Blocks:** `2026-07-17-mcp-service-runtime.md` and the tenant console.
**Hot-file ownership:** This plan is the sole writer of `app/db.py`, `app/main.py`, and `tests/test_admin_security.py` until its commits are merged.

## Global Constraints

- A tenant has exactly one shared management account; there are no members or roles.
- Tenant login uses `tenant_id + password`; the password is never an MCP Bearer Token.
- Platform-admin and tenant sessions use different cookies, API prefixes, and principal types.
- Tenant identity comes only from the server-side session, never from a client-selected tenant ID.
- Passwords are stored only as Argon2id hashes; session values are stored only as SHA-256 digests.
- MySQL DDL and queries remain MySQL 5.7 compatible and idempotent.
- Do not log passwords, raw session values, cookies, or request bodies.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `app/tenant_auth/models.py` | Immutable tenant account, principal, and session contracts. |
| `app/tenant_auth/passwords.py` | Argon2id hash and verify boundary. |
| `app/tenant_auth/store.py` | Account lifecycle, login failure tracking, and digest-only sessions. |
| `app/tenant_auth/dependencies.py` | Tenant-session resolution and tenant-scoped FastAPI dependency. |
| `app/tenant_auth/router.py` | `/tenant/login`, session, logout, and password-change endpoints. |
| `sql/007_tenant_auth.sql` | Operational MySQL 5.7 schema. |
| `tests/test_tenant_auth_*.py` | Persistence, API, isolation, and lifecycle coverage. |

### Task 1: Password and Account Persistence

**Files:**
- Create: `app/tenant_auth/__init__.py`
- Create: `app/tenant_auth/models.py`
- Create: `app/tenant_auth/passwords.py`
- Create: `app/tenant_auth/store.py`
- Create: `sql/007_tenant_auth.sql`
- Modify: `requirements.txt`
- Modify: `app/db.py`
- Test: `tests/test_tenant_auth_store.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces `TenantAccount`, `TenantPrincipal`, `hash_password(raw: str) -> str`, `verify_password(hash_value: str, raw: str) -> bool`.
- Produces `upsert_account`, `authenticate`, `change_password`, `set_account_status`, and `ensure_tenant_auth_tables`.

- [ ] **Step 1: Write failing password and account tests.**

```python
def test_password_hash_is_argon2_and_never_contains_raw_value():
    encoded = hash_password("tenant-password-123")
    assert encoded.startswith("$argon2id$")
    assert "tenant-password-123" not in encoded
    assert verify_password(encoded, "tenant-password-123") is True

def test_authenticate_returns_only_active_matching_tenant(monkeypatch):
    account = store.authenticate("tenant-a", "correct-password")
    assert account == TenantAccount(tenant_id="tenant-a", status="active", failed_attempts=0)
    assert store.authenticate("tenant-b", "correct-password") is None
```

- [ ] **Step 2: Run the focused tests and verify the expected failure.**

Run: `python -m pytest tests/test_tenant_auth_store.py -q`
Expected: FAIL because `app.tenant_auth` does not exist.

- [ ] **Step 3: Add the dependency, contracts, hashing boundary, and store API.**

```python
@dataclass(frozen=True)
class TenantAccount:
    tenant_id: str
    status: Literal["active", "disabled", "locked"]
    failed_attempts: int = 0
    locked_until: datetime | None = None

@dataclass(frozen=True)
class TenantPrincipal:
    principal_type: Literal["tenant"]
    tenant_id: str

def hash_password(raw: str) -> str:
    validate_password(raw)
    return PasswordHasher().hash(raw)

def verify_password(hash_value: str, raw: str) -> bool:
    try:
        return PasswordHasher().verify(hash_value, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
```

Add `argon2-cffi>=23.1.0` to `requirements.txt`. `authenticate()` must use the same generic failure result for unknown tenant, wrong password, disabled account, and active lockout; increment failures transactionally and set `locked_until` after the configured threshold.

- [ ] **Step 4: Add matching idempotent DDL and startup order.**

```sql
CREATE TABLE IF NOT EXISTS tenant_account (
  tenant_id VARCHAR(64) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  failed_attempts INT NOT NULL DEFAULT 0,
  locked_until DATETIME NULL,
  password_changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_login_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Call `ensure_tenant_auth_tables()` in `run_startup_migrations()` after central tenant configuration exists and before routers accept requests.

- [ ] **Step 5: Run tests and commit the persistence unit.**

Run: `python -m pytest tests/test_tenant_auth_store.py tests/test_migrations.py -q`
Expected: PASS.

```bash
git add requirements.txt app/tenant_auth app/db.py sql/007_tenant_auth.sql tests/test_tenant_auth_store.py tests/test_migrations.py
git commit -m "feat: add tenant account persistence"
```

### Task 2: Digest-Only Tenant Sessions

**Files:**
- Modify: `app/tenant_auth/models.py`
- Modify: `app/tenant_auth/store.py`
- Modify: `sql/007_tenant_auth.sql`
- Test: `tests/test_tenant_auth_store.py`

**Interfaces:**
- Produces `IssuedTenantSession`, `issue_session(tenant_id, ttl_seconds)`, `resolve_session(raw)`, `revoke_session(raw)`, and `revoke_tenant_sessions(tenant_id)`.
- `resolve_session(raw: str) -> TenantPrincipal | None`; session row details never escape the store.

- [ ] **Step 1: Add failing session-isolation tests.**

```python
def test_session_store_persists_digest_not_raw(monkeypatch):
    issued = store.issue_session("tenant-a", ttl_seconds=3600)
    assert store.resolve_session(issued.raw_value).tenant_id == "tenant-a"
    assert issued.raw_value not in repr(fake_db.bound_parameters)

def test_password_reset_revokes_all_existing_sessions():
    issued = store.issue_session("tenant-a", ttl_seconds=3600)
    store.change_password("tenant-a", "new-password-456")
    assert store.resolve_session(issued.raw_value) is None
```

- [ ] **Step 2: Run the session tests and verify they fail.**

Run: `python -m pytest tests/test_tenant_auth_store.py -k session -q`
Expected: FAIL because session functions and table are absent.

- [ ] **Step 3: Implement session contracts and digest storage.**

```python
@dataclass(frozen=True)
class IssuedTenantSession:
    session_id: str
    tenant_id: str
    raw_value: str = field(repr=False)
    expires_at: datetime

def session_digest(raw_value: str) -> str:
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()

def issue_session(tenant_id: str, ttl_seconds: int) -> IssuedTenantSession:
    raw_value = secrets.token_urlsafe(32)
    # Persist only session_digest(raw_value), never raw_value.
```

Session resolution must join `tenant_account`, require both session and account active, compare expiry with `UTC_TIMESTAMP()`, and update no sensitive value.

- [ ] **Step 4: Add the session table and revocation indexes.**

```sql
CREATE TABLE IF NOT EXISTS tenant_session (
  session_id VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(64) NOT NULL,
  session_digest CHAR(64) NOT NULL,
  expires_at DATETIME NOT NULL,
  revoked_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (session_id),
  UNIQUE KEY uk_tenant_session_digest (session_digest),
  KEY idx_tenant_session_tenant (tenant_id, revoked_at, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- [ ] **Step 5: Run and commit.**

Run: `python -m pytest tests/test_tenant_auth_store.py -q`
Expected: PASS.

```bash
git add app/tenant_auth sql/007_tenant_auth.sql tests/test_tenant_auth_store.py
git commit -m "feat: add tenant management sessions"
```

### Task 3: Tenant Login API and Principal Isolation

**Files:**
- Create: `app/tenant_auth/dependencies.py`
- Create: `app/tenant_auth/router.py`
- Modify: `app/main.py`
- Test: `tests/test_tenant_auth_api.py`
- Test: `tests/test_admin_security.py`

**Interfaces:**
- Produces `require_tenant_principal(request: Request) -> TenantPrincipal`.
- Produces `/tenant/login`, `/tenant/logout`, `/tenant/session`, and `/tenant/password/change`.

- [ ] **Step 1: Write failing login, cookie, and cross-principal tests.**

```python
def test_tenant_login_sets_distinct_http_only_cookie(client):
    response = client.post("/tenant/login", json={"tenant_id": "tenant-a", "password": "correct-password"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "wbg_tenant_session=" in cookie and "HttpOnly" in cookie
    assert "wbg_admin_session" not in cookie

def test_admin_cookie_cannot_authenticate_tenant_route(client, admin_cookie):
    response = client.get("/tenant/session", cookies={"wbg_admin_session": admin_cookie})
    assert response.status_code == 401

def test_tenant_login_is_rate_limited_by_tenant_and_client_ip(client):
    for _ in range(5):
        client.post("/tenant/login", json={"tenant_id": "tenant-a", "password": "wrong"},
                    headers={"X-Forwarded-For": "203.0.113.10"})
    limited = client.post("/tenant/login", json={"tenant_id": "tenant-a", "password": "wrong"},
                          headers={"X-Forwarded-For": "203.0.113.10"})
    assert limited.status_code == 429

def test_cross_site_origin_cannot_change_tenant_password(client, tenant_cookie):
    response = client.post("/tenant/password/change", cookies=tenant_cookie,
                           headers={"Origin": "https://attacker.invalid"},
                           json={"current_password": "old", "new_password": "new-password-123"})
    assert response.status_code == 403
```

- [ ] **Step 2: Run API tests and verify the routes are missing.**

Run: `python -m pytest tests/test_tenant_auth_api.py tests/test_admin_security.py -q`
Expected: tenant routes return 404 or imports fail.

- [ ] **Step 3: Implement the isolated router and dependency.**

```python
TENANT_SESSION_COOKIE = "wbg_tenant_session"

class TenantLoginRequest(BaseModel):
    tenant_id: str
    password: SecretStr

@router.post("/login")
def login(body: TenantLoginRequest, response: Response):
    account = store.authenticate(body.tenant_id, body.password.get_secret_value())
    if account is None:
        raise HTTPException(401, "认证失败")
    issued = store.issue_session(account.tenant_id, ttl_seconds=SESSION_TTL_SECONDS)
    response.set_cookie(TENANT_SESSION_COOKIE, issued.raw_value, httponly=True,
                        secure=is_production(), samesite="lax", path="/tenant")
    return {"ok": True, "tenant_id": account.tenant_id}

def require_tenant_principal(request: Request) -> TenantPrincipal:
    raw = request.cookies.get(TENANT_SESSION_COOKIE, "")
    principal = store.resolve_session(raw) if raw else None
    if principal is None:
        raise HTTPException(401, "未登录或会话过期")
    return principal
```

All mutating tenant routes must validate same-origin `Origin`/`Referer` before accepting the cookie. Do not return the raw tenant session to JavaScript or accept it from the admin Bearer header.

Before password verification, apply an in-memory bounded limiter keyed by `(normalized_tenant_id, trusted_client_ip)` and a second limiter keyed by `trusted_client_ip`. Use five failures per 15 minutes for the pair and 30 per 15 minutes for the IP. Only trust forwarded addresses through the existing proxy-aware client-IP helper. A 429 response is generic and does not reveal whether a tenant exists. Account failure counting and temporary account lockout still apply after the rate-limit gate.

- [ ] **Step 4: Register the router and test password-change invalidation.**

Include `tenant_auth.router` in `create_app()`. Password change requires the current password, writes the new hash, revokes all tenant sessions, then clears the cookie.

Logout and password change must call `delete_cookie(TENANT_SESSION_COOKIE, path="/tenant")`, matching the cookie creation path.

Run: `python -m pytest tests/test_tenant_auth_api.py tests/test_admin_security.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the tenant authentication API.**

```bash
git add app/tenant_auth app/main.py tests/test_tenant_auth_api.py tests/test_admin_security.py
git commit -m "feat: add isolated tenant login API"
```

### Task 4: Platform-Admin Account Provisioning

**Files:**
- Modify: `app/admin.py`
- Test: `tests/test_admin_security.py`
- Test: `tests/test_tenant_auth_api.py`

**Interfaces:**
- Adds optional `tenant_password: SecretStr | None` to tenant creation.
- Adds `PUT /admin/tenants/{tenant_id}/login-password` and `PUT /admin/tenants/{tenant_id}/login-status`.

- [ ] **Step 1: Write failing provisioning and reset tests.**

```python
def test_admin_can_provision_and_reset_tenant_password(client, admin_headers):
    created = client.post("/admin/tenants", headers=admin_headers, json=tenant_payload(tenant_password="initial-password-123"))
    assert created.status_code == 200
    assert login_tenant(client, "tenant-a", "initial-password-123").status_code == 200
    reset = client.put("/admin/tenants/tenant-a/login-password", headers=admin_headers,
                       json={"password": "replacement-password-456"})
    assert reset.status_code == 200

def test_tenant_cannot_call_admin_password_reset(client, tenant_cookie):
    response = client.put("/admin/tenants/tenant-a/login-password", cookies=tenant_cookie,
                          json={"password": "attacker-password-789"})
    assert response.status_code == 401
```

- [ ] **Step 2: Run the focused tests and verify endpoint absence.**

Run: `python -m pytest tests/test_admin_security.py tests/test_tenant_auth_api.py -k "provision or reset" -q`
Expected: FAIL with 404 or validation errors.

- [ ] **Step 3: Implement admin-only provisioning through the domain store.**

```python
class TenantPasswordRequest(BaseModel):
    password: SecretStr

@router.put("/tenants/{tenant_id}/login-password")
def reset_tenant_login_password(tenant_id: str, body: TenantPasswordRequest, request: Request):
    _require_auth(request)
    require_existing_tenant(tenant_id)
    tenant_auth_store.upsert_account(tenant_id, body.password.get_secret_value(), status="active")
    tenant_auth_store.revoke_tenant_sessions(tenant_id)
    return {"ok": True}
```

Tenant deletion or disable must disable the tenant account and revoke sessions in the same management operation. Error handling must not include `SecretStr` contents.

- [ ] **Step 4: Run the tenant-auth and admin regression suites.**

Run: `python -m pytest tests/test_tenant_auth_store.py tests/test_tenant_auth_api.py tests/test_admin_security.py tests/test_migrations.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the provisioning boundary.**

```bash
git add app/admin.py tests/test_admin_security.py tests/test_tenant_auth_api.py
git commit -m "feat: manage tenant login accounts"
```
