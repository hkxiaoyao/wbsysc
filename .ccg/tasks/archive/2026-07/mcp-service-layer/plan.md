# Task 5 implementation plan

Canonical scope: `docs/superpowers/plans/2026-07-17-tenant-console-rollout.md`, Task 5. Research: `research/task5-frontend.md` and `research/task5-deploy.md`.

## Layer 1 — prerequisites

### A. Tenant login admin contract

- Own `app/admin.py` and focused admin security/route tests only.
- Add non-secret `has_login_account` and `login_status` tenant-list metadata; never expose hashes, passwords, or password-derived hints.
- Preserve an explicitly disabled login during ordinary enabled-tenant edits; disabling the tenant may still disable login and revoke sessions.
- Require same-origin for password-bearing create/update/reset and login-status mutations while preserving auth-first behavior.
- Add adversarial tests for missing/cross-site/allowed origins, no-account state, session revocation, and ordinary-edit preservation.

### B. Deployment and feature rollback contract

- Own `app/main.py`, `deploy/server_deploy.sh`, `docker-compose.yml`, `tests/test_server_deploy_script.py`, and focused main route/health tests.
- Apply migrations in exact 004→005→006→007→008 order before pull/start.
- Mirror production plaintext-key validation: non-example, at least 32 UTF-8 bytes, distinct from credential and HMAC keys, never print secrets.
- Force disabled start and bounded disabled health verification before optional enable/recreate/health.
- On enabled-phase failure, restore false, recreate, verify disabled recovery, and exit nonzero without deleting 008 data.
- Report effective boolean in `/health`; gate tenant service-management/runtime while false, retain authenticated admin cleanup and old MCP routes.

## Layer 2 — user surface and operational assets

### C. Admin tenant controls

- Own `admin-ui/src/pages/Tenants.jsx`, `tenantsView.js`, and `tenantsView.test.js` only.
- Tests first for exact non-trimming password payloads/policy, encoded endpoints, fail-closed status values, metadata projection, and request generations.
- Add create-only optional initial password, edit-only reset, and active/disabled controls.
- Allowlist generic tenant payloads, never read/populate an existing password, serialize duplicate actions, and reject stale responses across close/switch/reload.

### D. Smoke and operations documentation

- Own `tests/test_smoke_client.py`, `docs/connection-platform-operations.md`, `docs/部署指南.md`, and `README.md` only.
- Require explicit opt-in plus written-authorization acknowledgement and explicit endpoints/tokens for production smoke.
- Parameterize legacy/service checks, wrong-service rejection, alias checks, and redact all raw tokens/results/errors from output.
- Document three-key rotation, exact Token visibility rules, 004→008 deployment, disabled rollback, default-service backfill, tenant password actions, and exact-ID child-first transactional cleanup.
- Do not execute production smoke without authorization and credentials.

## Layer 3 — review and release gate

- Run focused tests after each layer, then full Python, full frontend Node tests, Vite build, shell syntax, compose config when available, Ruff, and diff checks.
- Independent reviewers inspect auth/CSRF/state semantics and deployment/rollback/smoke safety.
- Critical/Important findings are fixed and re-reviewed.
- Production smoke remains explicitly blocked until the operator supplies written authorization and the required environment; local/mock smoke contracts must still pass.

## Layer 4 — final branch review remediation

- Make tenant deletion a fail-closed lifecycle operation: no service/connection bearer credential may remain usable after a successful delete; all central mutations and session/account invalidation must be atomic or deletion must return a strict precondition failure. Preserve historical schemas/data and invalidate affected gateway caches only after commit.
- Require accepted mandatory success audit before returning revealed service Token plaintext; audit rejection or exception returns a generic no-store failure with no secret.
- Implement optional strictly validated UTC service Token expiry for tenant/admin issue paths and persist `last_used_at` only for successful active, matching, unrevoked, unexpired service authentication.
- Add injected-failure, concurrency/tenant-isolation, expiry boundary, wrong-service, revoked, audit-failure, and cache invalidation tests; re-run independent security review and full verification.
