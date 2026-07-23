# Task 5 frontend/admin research

Scope: Tenant Console rollout Task 5, limited to `admin-ui/src/pages/Tenants.jsx`, `tenantsView.js`, and `tenantsView.test.js`. No product files were changed during research.

## Files Found

- `docs/superpowers/plans/2026-07-17-tenant-console-rollout.md:378-444` — requires initial-password, reset-password, and login-status controls; explicitly says the UI never reads an existing tenant password.
- `.ccg/tasks/mcp-service-layer/requirements.md` — tenant is a single account identified by tenant ID plus password; no member/role model.
- `admin-ui/src/pages/Tenants.jsx:104-179,682-804` — one create/edit drawer; payload currently spreads every form value; no tenant-login fields or actions; reloads are unsequenced.
- `admin-ui/src/pages/tenantsView.js:1-33` — only filtering/statistics helpers; no login payload, endpoint, validation, or request-generation helpers.
- `admin-ui/src/pages/tenantsView.test.js:1-68` — pure Node tests only; no React/DOM test dependency exists in `admin-ui/package.json`.
- `admin-ui/src/api.js:3-25` — same-origin Axios client, cookies enabled, and admin bearer token added from local storage.
- `app/admin.py:103-127` — `TenantUpsert.tenant_password: Optional[SecretStr]`; reset and status request models.
- `app/admin.py:147-178` — available admin reset/status write endpoints.
- `app/admin.py:208-295` — tenant list serializer does not expose account existence or login status and does not expose a password.
- `app/admin.py:317-379` — create accepts optional `tenant_password`, validates it before the tenant-config write, then provisions the account.
- `app/admin.py:382-452` — general tenant update accepts a replacement password and always synchronizes account status from `enabled`.
- `app/tenant_auth/passwords.py:8-19` — authoritative password policy: exact string, 12-256 characters, no leading/trailing whitespace, and case-insensitive substring `password` forbidden.
- `app/tenant_auth/store.py:170-202` — password upsert hashes with Argon2, clears lock state and revokes sessions; disabling login revokes sessions.
- `app/tenant_auth/dependencies.py:22-36` — reusable fail-closed same-origin check.
- `tests/test_admin_security.py:435-509,630-791,834-884` — backend coverage for reset, disabled preservation, session revocation, initial password, pre-write validation, and general-update status synchronization.
- `admin-ui/src/pages/connectionView.js:150-176` and `Connections.jsx:80-147` — established request-generation plus `AbortController` pattern for rejecting stale asynchronous responses.

## Dependencies

```text
Tenant create drawer
  -> buildTenantLoginPatch(raw password)
  -> POST /admin/tenants with optional tenant_password
  -> TenantUpsert(SecretStr)
  -> validate_password
  -> tenant_auth_store.upsert_account (Argon2 + session revocation)

Tenant reset action
  -> PUT /admin/tenants/{encoded tenant_id}/login-password
     body: { password: <exact raw string> }
  -> TenantPasswordRequest(SecretStr)
  -> upsert_account(status derived from tenant_config.enabled)

Tenant login enable/disable
  -> PUT /admin/tenants/{encoded tenant_id}/login-status
     body: { status: "active" | "disabled" }
  -> set_account_status
  -> 409 when no tenant account/password exists
  -> disabling revokes tenant sessions

UI status rendering
  -> GET /admin/tenants
  -> BLOCKED: response has neither has_login_account nor login_status
```

Available contracts:

| Operation | Request | Success | Expected failures |
|---|---|---|---|
| Initial password | `POST /admin/tenants`, optional `tenant_password` | `{ok, schema_name, trusted_domain}` | 422 weak password; existing create errors |
| Reset password | `PUT /admin/tenants/{tenant_id}/login-password`, `{password}` | `{ok: true}` | 401, 404 missing tenant, 422 weak password |
| Change login status | `PUT /admin/tenants/{tenant_id}/login-status`, `{status: "active"|"disabled"}` | `{ok: true, status}` | 401, 404 missing tenant, 409 no login password, 422 invalid enum |
| Read login state | none | none | contract missing |

## Exact Gaps

1. Create UI has no initial-password input and never sends `tenant_password`.
2. Edit UI has no dedicated reset-password operation. Using the general tenant PUT for reset would couple a credential mutation to unrelated stale form fields and is not recommended.
3. There is no enable/disable-login action or status presentation.
4. `GET /admin/tenants` cannot distinguish `no account`, `active`, or `disabled`. `row.enabled` is tenant runtime state, not a reliable login-state field. A frontend must not infer or display login state from it.
5. Backend `update_tenant` currently calls `set_account_status(..., "active")` whenever an enabled tenant's ordinary settings are saved. Thus a separately disabled login can be silently re-enabled by a later configuration save. This must be resolved in the contract/semantics before independent login controls are trustworthy.
6. Password/status endpoints and password-bearing tenant create call `_require_auth` but not `require_same_origin`; cookie-authenticated mutations therefore lack the explicit same-origin enforcement already used by admin service mutations.
7. `submit()` spreads all form values (`Tenants.jsx:160-165`). Adding confirmation or action-only fields to the same form would leak them into the generic tenant payload. Build an allowlisted base payload, then merge only `buildTenantLoginPatch()` for create.
8. `load()` has no abort/generation guard. Mutation-triggered reloads can finish out of order and overwrite newer tenant/login state. The current `editing` object can also become stale while its drawer remains open.

## Patterns

- Keep secret-presence metadata only: existing tenant serialization uses `has_secret`, `has_contact_secret`, and token hint rather than returning secrets (`app/admin.py:208-225`). Login metadata should similarly be `has_login_account` plus `login_status`, never a password/hash/hint.
- Blank edit secrets mean preserve (`Tenants.jsx:122-126,734-755`). Initial password should follow the explicit-only helper requested by the rollout plan, but only on create.
- Endpoint identifiers should be encoded, following `connectionView.js:8-18` and `servicesView.js:20-27`; do not interpolate raw tenant IDs.
- Use request tickets/abort guards following `connectionView.js:150-176` and `Connections.jsx:108-147` so late list/mutation responses cannot update current UI.
- Backend validation is authoritative. Client validation should mirror it for feedback but must send the exact password unchanged; trimming changes a secret and hides invalid surrounding whitespace.

Recommended pure helpers in `tenantsView.js`:

- `buildTenantLoginPatch(value)`: return `{}` only for `undefined`, `null`, or exact `''`; for a non-empty string return `{tenant_password: value}` unchanged; reject non-string input rather than coercing it.
- `tenantLoginPasswordEndpoint(tenantId)` and `tenantLoginStatusEndpoint(tenantId)`: require nonblank ID and use `encodeURIComponent`.
- `buildTenantPasswordReset(value)`: require a non-empty string and return `{password: value}` unchanged.
- `buildTenantLoginStatusPatch(status)`: accept only `active`/`disabled`; throw otherwise.
- `tenantPasswordValidationError(value, {optional})`: mirror 12-256, exact trim, and forbidden-substring rules; blank is accepted only when explicitly optional.
- A small request-generation helper (same interface as the connection sequence) for list/login mutations, or reuse an established shared helper if ownership permits.

## Tests First / Proposed Test Matrix

All frontend tests fit the existing pure `node:test` style in `tenantsView.test.js`; no new DOM test framework is needed.

| Area | Exact cases/assertions |
|---|---|
| Create patch | `buildTenantLoginPatch('')`, `null`, `undefined` => `{}`; strong exact string => `{tenant_password: exact}` |
| Fail closed | non-string input throws; whitespace-only and surrounding-space strings produce validation errors, not omission/trimming |
| Password policy | 11 chars rejected; 12 accepted; 256 accepted; 257 rejected; mixed-case `PassWord` substring rejected |
| Reset body | strong input => `{password: exact}`; empty/non-string throws |
| Status body | `active`/`disabled` accepted; booleans, `enabled`, empty, unknown values throw |
| Endpoints | tenant `tenant /a` becomes `tenant%20%2Fa`; blank/whitespace ID throws |
| Never read password | login-state projection/helper uses only `has_login_account` and `login_status`; unknown/missing metadata remains `unknown`, never inferred from `enabled` |
| Race guard | first ticket becomes stale after second begins; invalidation makes the current ticket stale; optionally bind ticket to tenant/action identity |

Manual/build verification after implementation:

1. Create with password omitted sends no password key; create with valid input sends exact value once.
2. Opening edit never populates any password field from a row or response.
3. Reset requires explicit confirmation, disables duplicate submission, clears the input on success, and surfaces 422 safely.
4. Disable requires confirmation, remains busy until completion, and cannot be overwritten by a stale list response.
5. 409 (`no password`) keeps login fail-closed and directs admin to set/reset a password first; it must not optimistically show active.
6. Close/switch tenant while a request is pending cannot update the next tenant's drawer or show success for the wrong tenant.
7. `node --test admin-ui/src/pages/tenantsView.test.js` and `npm --prefix admin-ui run build` pass.

## Ownership-safe Implementation Plan

Frontend owner only:

1. `tenantsView.test.js`: add the pure failing cases above.
2. `tenantsView.js`: implement payload/endpoint/validation/status-projection and request-generation helpers.
3. `Tenants.jsx`: add optional initial password to create only; add a separate edit-only login-security section/modal for reset and status; build an allowlisted generic tenant payload; encode endpoints; use per-tenant busy state plus sequence/abort guards; clear secret fields on completion/close; never store/read a returned password.

Backend owner prerequisite (do not fold into frontend-owned files):

1. Extend tenant-list metadata with `has_login_account` and `login_status` (or add an authenticated no-store read endpoint) without exposing `password_hash` or password-derived material.
2. Define stable independence semantics between `tenant_config.enabled` and login status. Safest rule: disabling the tenant may force login disabled, but ordinary saves of an enabled tenant must not reactivate an explicitly disabled login.
3. Apply `require_same_origin` to password-bearing create/update/reset and status mutations; add missing/cross-site/accepted-origin tests.

Until those backend prerequisites land, frontend status should render `未知/不可操作` rather than infer a value; reset can still be implemented against the existing endpoint.

## Risks

### Critical

- **No readable login-state contract:** accurate enable/disable UI is impossible; inference from tenant `enabled` can misreport security state.
- **Silent re-enable:** an ordinary save of an enabled tenant currently resets an existing login account to active (`app/admin.py:441-449`).
- **CSRF defense gap:** password/status mutations use cookie-capable admin auth without the explicit same-origin check used elsewhere.
- **Secret leakage by form spread:** adding password/confirmation controls to the current form without payload allowlisting can transmit unintended fields.

### Important

- Out-of-order reloads or late mutation responses can overwrite newer UI state or update the wrong open tenant.
- Empty/whitespace coercion or trimming can accidentally omit a requested reset or silently change a password; validate exact input and fail closed.
- A 409 status change means no account exists. Optimistic toggles must roll back/avoid committing, and the UI should lead to password setup.
- Separate login actions need their own per-tenant busy lock so double-clicks and simultaneous reset/status changes cannot race.

### Minor

- Use `autoComplete="new-password"`, explanatory password-policy copy, and destructive confirmation for disable/reset.
- Avoid success/error text containing the password or raw Axios/config objects; use only sanitized server `detail`/generic messages.
- Add `no-store` to any future login-state read response; although it contains no password, it is security-sensitive account metadata.
