# Task 5 backend / deployment / docs / smoke research

Scope: Tenant Console rollout Task 5, lines 378-444, excluding the admin UI implementation. No `.ccg/spec/` directory or repository `AGENTS.md` exists in this worktree; the task requirements and the supplied root instructions therefore govern this research.

## Files Found

- `docs/superpowers/plans/2026-07-17-tenant-console-rollout.md:378-444` — Task 5 contract: migrations 007-008, plaintext-key gate, feature-flag rollout, docs, and authorized reversible smoke.
- `.ccg/tasks/mcp-service-layer/requirements.md:1-12` — confirmed product rules, especially revealable service Tokens and compatibility of old connection Tokens/routes.
- `docs/superpowers/specs/2026-07-17-mcp-service-layer-design.md:122-132,300-309` — three independent production keys; rollback must close the service route and tenant self-service entry while retaining tables and old MCP routes.
- `app/main.py:188-389` — app construction, health response, unconditional service management routers, service-route flag, disabled-route 404 guard, and mount order.
- `app/main.py:448-568` — startup migration, connector discovery, flag-gated default-service backfill/session manager, and scheduler lifecycle.
- `app/config.py:16-42,114-155` — authoritative example-value, UTF-8 byte-length, and pairwise-distinct production key validation.
- `deploy/server_deploy.sh:14-246` — `.env` parser, current key validation, and migration array, currently ending at 006.
- `deploy/server_deploy.sh:248-293` — current pull/start/single generic health check; no staged flag activation or automatic flag rollback.
- `docker-compose.yml:15-30` — `.env` injection and generic `/health` container probe.
- `tests/test_server_deploy_script.py:11-58` — current static deploy contract covers only migrations 004-006 and migration-account safety.
- `tests/test_smoke_client.py:1-110` — opt-in legacy MCP smoke only; uses one good/bad Token, hard-coded WeCom tool expectations/calls, and prints response excerpts.
- `sql/006_connection_platform.sql:4-87` — creates `connection_instance` and connection/declarative tables.
- `sql/007_tenant_auth.sql:3-26` — creates tenant account/session tables.
- `sql/008_mcp_service.sql:3-53,55-163` — alters `connection_instance`, creates service/binding/token tables, and alters/indexes `mcp_call_log`.
- `docs/connection-platform-operations.md:10-185` — current production config, 004-006 migration/rollback, old Token rule, and a one-connection non-production cleanup recipe.
- `docs/部署指南.md:28-95,151-179` — current deployment guide mentions only migration 004 in several places and lacks service flag/key rollout.
- `README.md:77-138` — current upgrade/config instructions stop at 006 and omit the plaintext key and service flag from the configuration table.
- `.env.prod.example:31-42` — contains all three distinct-key placeholders and defaults `MCP_SERVICE_ENABLED=false`.

## Dependencies

```text
004 tenant hardening
  -> 005 mcp_call_log + gateway_setting
     -> 006 connection_instance + connection/declarative tables
        -> 007 tenant_account + tenant_session (independent DDL, required for console login)
        -> 008 requires both 005 and 006:
             alters connection_instance.connection_alias
             alters/indexes mcp_call_log.service_id/tool_alias
             creates mcp_service, mcp_service_tool_binding, mcp_service_token

MCP_SERVICE_ENABLED=false
  -> service MCP mount absent and /mcp/service/{id} returns 404
  -> service gateway session manager absent
  -> default-service backfill skipped
  -> currently tenant/admin service-management routers are still mounted (gap)

MCP_SERVICE_ENABLED=true on process creation/restart
  -> service mount present before connection/legacy mounts
  -> lifespan runs DB startup migration and connector discovery
  -> default-service backfill runs before the app accepts traffic
  -> service session manager and cache invalidator start
```

The setting is captured in `create_app()` (`app/main.py:193-201`); editing `.env` does not alter a running process. Every flag transition therefore requires container recreation/restart, not merely an environment-file edit.

## Patterns

- Follow `app/config.py:114-155` as the single semantic model for deploy-time validation: trim first, reject known examples, require at least 32 **UTF-8 bytes**, and reject equality with each of the other two keys.
- Follow the existing migration array/loop at `deploy/server_deploy.sh:223-244`; append 007 then 008 rather than adding separate ad-hoc invocations. This preserves missing-file checks, `MYSQL_PWD`, fail-before-pull behavior, and exact ordering.
- Preserve route precedence in `app/main.py:377-383`: service, then connection, then legacy. The disabled catch-all at `app/main.py:272-287` correctly prevents `/mcp/service/{id}` from falling into `ConnectionMcpGateway`.
- Preserve feature rollback semantics from the service design: disable the new service route/self-service only; keep 008 schema/data and old `/mcp` plus `/mcp/{connection_id}` intact.
- Preserve explicit smoke opt-in and import safety in `tests/test_smoke_client.py:18-26,95-110`; environment mutation belongs only inside the executable entry point.
- Preserve destructive-cleanup discipline in `docs/connection-platform-operations.md:168-185`: revoke/disable through the API first, exact IDs in one transaction, exact schema verification, and no wildcard schema deletion.

## Exact gaps

### Migration and production key gates

1. `MIGRATIONS` lacks `sql/007_tenant_auth.sql` and `sql/008_mcp_service.sql`; all user-facing migration text and completion text still says 004-006.
2. The deployer never reads or validates `MCP_TOKEN_PLAINTEXT_KEY`, although application production startup already rejects it. With the template placeholder, deployment currently reaches the image/start phase and only then fails application startup.
3. Deployer validation must reject at least the same examples as `EXAMPLE_MCP_TOKEN_PLAINTEXT_KEYS`, including `replace_with_plaintext_key`, enforce `byte_length >= 32`, compare it with both `CREDENTIAL_KEY` and `MCP_TOKEN_HMAC_KEY`, and unset it after validation. Do not print any key or feed it to tracing.
4. First-run instructions list only the credential/HMAC keys and incorrectly suggest two keys; they must list the plaintext key and state all three are independent. Automatic silent rotation of an existing plaintext key is unsafe because it makes stored `encrypted_token` values unrevealable; validation should fail closed. If generation is offered, restrict it to a fresh empty/example template and document it as initial provisioning, never rotation.

### Feature flag, health, and rollback

1. A `.env` containing `MCP_SERVICE_ENABLED=true` starts the new route immediately on the first new container. The only health check happens afterward, violating “keep false until migrations and health checks pass.”
2. `/health` does not return the effective flag, so a passing response cannot prove whether the old/disabled phase or enabled phase is running. Add a non-secret boolean such as `mcp_service_enabled` to the response.
3. Stage deployment as follows:
   - validate the requested value is exactly `true` or `false` and remember it;
   - force the effective `.env` value to `false` before the first `docker compose up -d`;
   - execute 004 -> 005 -> 006 -> 007 -> 008 before pull/start;
   - recreate/start with the flag false and poll (bounded retries, not one fixed sleep) until `/health` returns HTTP 200 and `mcp_service_enabled:false`;
   - only if the remembered requested value was `true`, atomically write `true`, recreate, and poll until `/health` reports `mcp_service_enabled:true`;
   - if the enabled-phase probe fails, atomically restore `false`, recreate, require a healthy disabled response, and exit nonzero. Retain migrations/service rows; do not delete 008 objects. If disabled recovery also fails, report a critical rollback failure and leave the flag false.
4. Container health currently proves only HTTP availability. Update it to require the new health response shape (at minimum `status=ok` and the boolean field), while the deploy script additionally verifies the expected phase value.
5. `app/main.py:244-245` mounts tenant and admin service management APIs even when false. The design explicitly says rollback closes the tenant self-service entry. Gate `mcp_services_tenant_router` on the flag (404 when disabled). Keep the admin service router available while disabled so operators can list, revoke Tokens, and disable/clean services during rollback. Existing connection and legacy routes must remain unaffected.
6. `migrate_default_services(... enabled=service_enabled)` and service session startup are already correctly gated (`app/main.py:475-478,509-517`). No change should make the backfill run while false.

### Documentation

1. Add `MCP_TOKEN_PLAINTEXT_KEY` and `MCP_SERVICE_ENABLED` to all production config tables/checklists and state the exact display rule: connection Tokens remain display-once and cannot be revealed; unrevoked service Tokens may be revealed only by current platform-admin or owning-tenant sessions through rate-limited, audited, `no-store` endpoints.
2. Document key rotation separately:
   - credential key: re-encrypt credentials;
   - HMAC key: pre-issue replacements because all existing connection/service digest authentication breaks;
   - plaintext key: re-encrypt every unrevoked `mcp_service_token.encrypted_token` before switching; revoked rows already have ciphertext cleared and are not recoverable.
3. Extend every upgrade/manual command sequence to 004-008. State 008 depends on 005 and 006 and is retained during feature rollback.
4. Document tenant-password initial set/reset/status changes without ever reading an existing password, and state reset/revoke effects on tenant sessions.
5. Document default-service backfill: it runs only on an enabled restart, after trusted connector registration; it is idempotent/watermarked, creates bindings but never copies connection Token rows.
6. Document feature rollback as flag false + recreate + health verification; old connection endpoints/Tokens stay live, service route and tenant service self-management become unavailable, admin cleanup remains available, and new tables/data remain.

### Smoke client and reversible production smoke

1. Current smoke cannot verify Task 5: it only targets one legacy endpoint and cannot cover two connections, an aggregated service, alias uniqueness, reveal/copy/revoke, wrong-service rejection, two-step OpenAPI, scoped service logs, tenant login, or cleanup.
2. Current live defaults (`localhost`, `test-token`) are acceptable for local mock but unsafe as a production contract. Production execution must require explicit endpoint/Token variables and a separate explicit authorization acknowledgement. Do not infer authorization from a reachable URL or available credentials.
3. The client prints tool-result excerpts (`tests/test_smoke_client.py:70,76,83`), which may disclose production customer data. For an authorized production smoke, print only fixed pass/fail text, safe IDs/prefixes/counts, exception class, and sanitized error codes; never print Token values, cookies, Authorization headers, raw tool results, or arbitrary exception text.
4. Split MCP protocol checks into parameterized helpers: legacy connection endpoint + connection Token; service endpoint + service Token; same Token against a second service ID must reject; expected aliases supplied explicitly; optional controlled two-step tool call against only the disposable OpenAPI connection. Keep mutation/setup/cleanup in documented admin/tenant API steps rather than hiding broad database mutation inside pytest.
5. Exact smoke order:
   1. Obtain written change authorization, maintenance window, production target, disposable schema permission, named operator/reviewer, backup/restore point, and cleanup approval.
   2. Generate one high-entropy run ID locally; record exact tenant ID, schema name, two connection IDs/aliases, service ID/key, token IDs (not raw Tokens), declarative spec/revision IDs, original retention value, and pre-smoke row counts.
   3. Create the disposable tenant and initial password; verify tenant login/logout/reset/status without storing cookies/passwords in artifacts.
   4. Create exactly two disposable connections, including one controlled declarative/OpenAPI connection; publish a two-step tool whose upstream is explicitly authorized and disposable.
   5. Create one service with tools from both connections; verify unique aliases, list/call, wrong-service Token rejection, issue/reveal/copy, revoke then reveal/auth rejection, tenant-scoped logs, and old connection endpoint compatibility.
   6. Revoke every service and connection Token first. Disable the service(s), then both connections. Restore retention/settings through their API and verify the original value.
   7. In one DB transaction, lock and verify exact ownership/counts; delete exact-run rows in child-first order: `mcp_service_token`; `mcp_service_tool_binding`; service/connection rows in `mcp_call_log`; `mcp_service`; declarative operations/revisions; connection sync state/policies/tokens/credentials; both exact `connection_instance` rows; `tenant_session`; `tenant_account`; `domain_verify_file`; finally exact `tenant_config`. Never use an unbounded `LIKE`, wildcard schema name, or a prefix-only delete. Include any auto-backfilled service selected by exact `tenant_id` in the recorded service-ID set.
   8. Commit only after all ownership/count assertions match. Otherwise rollback and retain evidence. Drop the disposable tenant schema only after separately proving its exact configured name contains the full run ID and no other `tenant_config` row references it.
   9. Verify zero exact-run rows in every listed table, zero live Tokens, settings restored, the disposable schema absent (or explicitly recorded as retained), no unexpected row-count delta, and old endpoints still healthy.
6. There is no separate admin-audit table: management/reveal audit events are in `mcp_call_log` (for reveal see `app/mcp_services/router.py:200-230`). Cleanup must therefore include exact tenant/service/connection log dimensions, after evidence capture and before deleting parents.

## Exact TDD plan

1. `tests/test_server_deploy_script.py` — first extend the migration tuple to 004-008 and assert 006 < 007 < 008 < pull/up. Add assertions for plaintext-key read/example/byte-length/pairwise comparisons/unset.
2. In the same file add flag state-machine contract tests: remembered requested flag; forced false before first `compose up`; false health probe before any write of true; true phase requires recreate and true health probe; failure path restores false, recreates, and exits nonzero; bounded retry loop replaces fixed `sleep 10`. Prefer factoring shell helpers (`set_env_value`, `wait_for_health_state`, `rollback_service_flag`) so the assertions target named behavior rather than incidental strings.
3. Add/extend app tests (best home: `tests/test_mcp_connection_isolation.py` or a focused main-health test): false reports false, returns 404 for service MCP and tenant service-management routes, never enters connection gateway; true reports true and service routes retain precedence; admin cleanup routes remain mounted in false mode; old dynamic/legacy MCP routes work in both states.
4. Add a compose contract assertion that the container health probe requires the new health response shape.
5. `tests/test_smoke_client.py` — unit-test environment parsing and parameterized endpoint construction without network; keep live cases skipped unless explicitly opted in and authorized. Test redaction by capturing output and asserting raw Tokens/raw result bodies never appear. Add wrong-service rejection and expected-alias helpers using mocked MCP transports where feasible.
6. Implement deploy/app/compose changes only after the above failures are observed.
7. Update operations/deployment/README docs and add static doc assertions only for safety-critical contracts if this repository accepts doc-contract tests.

## File ownership recommendation

- Deployment owner: `deploy/server_deploy.sh`, `docker-compose.yml`, `tests/test_server_deploy_script.py`.
- Backend route owner (hot file; one writer only): `app/main.py` plus the focused route/health tests. Coordinate because Task 5 declares this plan the sole writer after backend merges.
- Smoke owner: `tests/test_smoke_client.py`.
- Documentation owner: `docs/connection-platform-operations.md`, `docs/部署指南.md`, `README.md`.
- Do not change `app/config.py` or migrations 006-008 for this task; they already encode the intended application validation/schema. Deployment should mirror them.

## Test commands

Focused baseline executed during research:

```text
python -m pytest tests/test_server_deploy_script.py tests/test_smoke_client.py tests/test_config.py tests/test_mcp_connection_isolation.py -q
46 passed, 2 skipped
```

Implementation verification:

```bash
python -m pytest tests/test_server_deploy_script.py tests/test_config.py tests/test_mcp_connection_isolation.py -q
python -m pytest tests/test_smoke_client.py -q
python -m pytest tests/test_migrations.py tests/test_mcp_service_migration.py tests/test_mcp_service_gateway.py tests/test_mcp_service_api.py tests/test_tenant_auth_api.py -q
python -m pytest -q
bash -n deploy/server_deploy.sh
docker compose config
```

Live smoke must not run until authorization and credentials are supplied. When authorized, invoke only with explicit opt-in/authorization and explicit endpoints/Tokens; never enable shell tracing and never paste command history containing raw Tokens.

## Risks

- **Critical:** enabling directly from `.env=true` exposes the new route before the required disabled-phase health gate.
- **Critical:** automatic plaintext-key replacement/rotation makes existing service Token ciphertext unrevealable.
- **Critical:** current cleanup recipe knows nothing about service bindings/tokens or tenant auth and can leave production artifacts; broad prefix deletion can delete unrelated data.
- **High:** disabling all service APIs would remove the operator cleanup path. Gate tenant self-service and MCP runtime, but retain authenticated admin cleanup endpoints.
- **High:** 008 fails if 005/006 are absent because it alters their tables; exact order is operationally required, not cosmetic.
- **Medium:** a source-string-only deploy test can pass with dead/commented code. Named shell helpers plus a stub-command harness would be stronger if time permits.
- **Medium:** a generic health 200 can come from the wrong phase/image; require the effective flag in health and verify both phases.
- **Medium:** production smoke output can leak customer data even without leaking Tokens if raw tool results are printed.

## Blockers

Authorized production smoke is blocked and must not be attempted from this environment:

- No `.env`, `.env.prod`, or deploy-local environment file is present in the worktree.
- `DB_MIGRATION_USER`, `DB_MIGRATION_PASSWORD`, all `MCP_SMOKE_*` values, an authorization acknowledgement, `GHCR_TOKEN`, and `SSH_AUTH_SOCK` are absent.
- The SSH config has no host blocks or identity entries, no default RSA/Ed25519 private key is present, and no agent key is loaded.
- Host `mysql` CLI is absent.
- Repository docs explicitly say customer authorization is currently incomplete (`docs/PLAN-wecom-mcp-gateway.md:24`) and require written authorization before production (`README.md:236`, `docs/部署指南.md:162`).

These checks inspected presence only and did not print secret values. A public production hostname in templates/docs and an HTTPS Git remote are not credentials or authorization.
