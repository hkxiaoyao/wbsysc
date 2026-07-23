# Review Record

## Tenant authentication phase

- Reviewer: Claude Code (`claude` backend only; Gemini was not called)
- Result: APPROVED
- Verification: `96 passed`
- Closed findings:
  - Argon2 verification now runs outside the `SELECT ... FOR UPDATE` transaction.
  - The second authentication phase re-reads and locks the account, rejects password-hash changes, and uses fresh status and failure counters.
  - The in-memory login limiter is documented as best-effort; persistent database account lockout is authoritative.
  - Admin password reset preserves the status implied by `tenant_config.enabled`.
- Remaining findings: no Critical or Warning findings.
## MCP service persistence phase

- Commits: `2c12a7a`, `1cf8d0a`
- Result: Spec PASS; Code Quality APPROVED
- Verification: `644 passed, 2 skipped`
- Closed finding: connection alias conflicts now fail before connection or credential writes; update targets are selected only by `connection_id`.
- Remaining findings: no Critical, Important, or Minor findings.
- Residual risk: MySQL 5.7 migration and locking behavior have not yet been exercised against a live server.

## Viewable service Token phase

- Commit: `0c7ff6f`
- Result: Spec PASS; Code Quality APPROVED
- Verification: `662 passed, 2 skipped`
- Remaining Minor findings:
  - Add explicit reveal/revoke isolation tests for wrong service and Token IDs.
  - Exercise Token SQL, transactions, and binary columns on live MySQL 5.7.
  - Ensure rollout generates and validates `MCP_TOKEN_PLAINTEXT_KEY` even before the feature is enabled in production.

## Tenant/admin service management API phase

- Commits: `769bcd5`, `466e7fd`
- Result: Spec PASS; Code Quality APPROVED
- Verification: `689 passed, 2 skipped`
- Closed findings:
  - Status locks are tenant-bound in SQL.
  - Token issue locks and validates the active service in the insert transaction.
  - Reveal failures are no-store, uniformly safe, and minimally audited.
- Remaining Minor: concurrency behavior is structurally tested but not exercised against a live multi-connection database.

## Service-scoped MCP gateway phase

- Commits: `a287d7b`, `231ca8a`
- Result: Spec PASS; Code Quality APPROVED
- Verification: `705 passed, 2 skipped`
- Closed findings:
  - `/mcp/service` and `/mcp/service/` are reserved before dynamic connection routing for all supported methods.
  - Connection persistence and backfill reject the reserved `service` ID case-insensitively.
- Remaining findings: none.

## Tenant console shell and cookie-only login phase

- Commits: `6b9853d`, `5f1121c`
- Result: Spec PASS; independent review APPROVED after logout lifecycle remediation.
- Verification: frontend `48 passed`; focused backend `51 passed`; post-build routing, Ruff, interceptor probe, diff checks, and production build passed.
- Closed findings:
  - Failed logout preserves the authenticated tenant shell and exposes a safe retry path while the HttpOnly session remains valid.
  - Only the current successful logout request transitions to login; stale and unmounted requests are aborted or ignored.
  - Tenant identity remains server-owned and the tenant console never stores or selects a tenant ID.
- Remaining findings: none.

## Default-service backfill and cache invalidation phase

- Commits: `f639d6d`, `b0819c9`
- Result: Spec PASS; Code Quality APPROVED
- Verification: `714 passed, 2 skipped`; focused migration/gateway suite `17 passed`; required adjacent regressions `31 passed`; Ruff and diff checks passed.
- Closed findings:
  - Default service backfill now rejects service-key ownership conflicts before binding or watermark writes and converges existing deterministic services.
  - Cache invalidator failures are isolated and still trigger complete tenant-level fail-safe invalidation.
  - Cross-thread gateway cache eviction is marshalled onto the gateway event loop and serialized under the manager lock.
- Remaining findings: none.

## Reference-aware delete and revision lifecycle phase

- Commits: `0881f20`, `0c17c61`
- Result: Spec PASS; Code Quality and SQL Transaction Safety APPROVED after remediation
- Verification: `736 passed, 2 skipped`; focused re-review `150 passed`; Ruff and diff checks passed.
- Closed findings:
  - Malformed selected-revision values now fail closed instead of allowing deletion.
  - Revision publication and deletion use the same connection-to-revision lock order.
  - Connection deletion and binding replacement use the same connection-to-service lock order.
- Remaining findings: none.

## OpenAPI closed references and immutable tool models phase

- Commits: `8cdf952`, `91e8c36`
- Result: Spec PASS; Code Quality APPROVED after remediation
- Verification: full suite `762 passed, 2 skipped`; independent focused regression `140 passed`; Ruff and diff checks passed.
- Closed finding: explicit null, empty, or invalid `tools` containers now fail closed instead of being mistaken for legacy documents and widening the public tool surface.
- Remaining findings: none.

## OpenAPI import and strict storage round-trip phase

- Commits: `06dd286`, `e2fcb6d`
- Result: Spec PASS; Code Quality and Security APPROVED after remediation
- Verification: full suite `795 passed, 2 skipped`; independent focused regression `232 passed`; Ruff and diff checks passed.
- Closed findings:
  - Duplicate JSON/YAML keys are rejected before normalization at root, tool, and nested levels.
  - Malformed property schemas and empty output/result declarations fail closed while valid empty inputs and legacy restoration remain supported.
- Remaining findings: none.

## OpenAPI sequential fail-fast executor phase

- Commits: `625719e`, `eb9f6ea`, `477a5eb`, `c442f8b`
- Result: Spec PASS; Async/Security Code Quality APPROVED after remediation
- Verification: full suite `825 passed, 2 skipped`; final re-review `192 passed`; Ruff and diff checks passed.
- Closed findings:
  - Legacy and composite undeclared arguments now fail before any network request.
  - Recursive `additionalProperties` semantics match the accepted JSON Schema subset for objects and array items.
  - `type: null` now rejects every non-null JSON value; primitive type conformance is covered.
- Remaining findings: none.

## OpenAPI safe step audit and admin preview phase

- Commits: `fa9fe64`, `67b1082`, `22b7f30`
- Result: Spec PASS; Async/Security Code Quality APPROVED after remediation
- Verification: full suite `857 passed, 2 skipped`; independent focused regression `283 passed`; asyncio debug and warnings-as-errors passed; Ruff and diff checks passed.
- Closed findings:
  - Preview now uses a strict structural allowlist and focused secret-shape redaction while preserving normal prose.
  - Async audit delivery no longer consumes execution deadlines; cancellation-resistant sinks are quarantined under a hard global cap and connector-owned tasks clear on bounded close.
  - Persisted safe audit event names retain validated step IDs without payload leakage.
- Remaining findings: none.

## Tenant-scoped connections, overview, and logs phase

- Commits: `bf33913`, `a95c9e6`
- Result: Spec PASS; independent security/contract review APPROVED after fail-closed input remediation.
- Verification: focused backend `120 passed`; relevant frontend `55 passed`; independent adversarial request matrix, Ruff, diff checks, and production build passed.
- Closed findings:
  - All tenant read adapters authenticate first, reject every non-empty body, reject unknown or repeated query keys, and never call stores for ambiguous scope input.
  - Tenant log alias and source-key filters remain distinct; admin endpoint/query behavior remains unchanged.
  - Tenant log scope copy now reflects the current session tenant instead of a global view.
- Remaining finding: tenant detail/mutation routes are the immediately following Task 2B dependency and are not an independently deployable checkpoint.

## Complete tenant connection management API phase

- Commits: `9117baf`, `e64fe1f`
- Result: Spec PASS; independent security/contract review APPROVED after CSRF and raw-response remediation.
- Verification: focused API/security/console `300 passed`; associated auth/store/gateway/token/declarative `275 passed`; final reviewer probes `43 passed`; Ruff, route-duplication, scope, and diff checks passed.
- Closed findings:
  - All 17 mutation families authenticate first, enforce same-origin, reject ambiguous input, and produce zero domain/audit/orchestrator/network/cache/token side effects for hostile origins.
  - Raw create/issue/rotate successes and every tested failure class are no-store and do not leak exception or secret text; non-raw exception behavior is unchanged.
  - Tenant adapters call explicit tenant-scoped use cases, foreign ownership fails before secondary work, and admin/global contracts remain intact.
- Remaining findings: none.

## Connector cards and MCP service workbench phase

- Commits: `f0d6f46`, `a43e2c6`
- Result: Spec PASS; independent frontend/security review APPROVED after alias and clipboard lifecycle remediation.
- Verification: frontend `76 passed`; admin connection `32 passed`; backend security/tenant connection regressions `266 passed`; deferred clipboard matrix, Ruff, diff checks, and production build passed.
- Closed findings:
  - Admin projections now expose authoritative `connection_alias`; service binding aliases never fall back to connection IDs and match backend canonical identity.
  - Alias conflict blocking uses the same ASCII case-insensitive semantics as backend uniqueness checks.
  - Clipboard copies are service/token-ticketed; close, switch, replacement, revoke, and unmount prevent stale completion from writing state.
  - Connector creation uses fixed accessible cards, and service token prefixes/metadata are never treated as raw credentials.
- Remaining findings: none.

## Safe declarative operation catalog prerequisite

- Commits: `7e308ef`, `c871afd`, `7329468`
- Result: additive preview contract APPROVED after malformed-object and OpenAPI-bound compatibility remediation.
- Verification: admin/tenant `253 passed`; declarative/security `247 passed`; final compatibility/adversarial/contracts `26 passed`; Ruff and diff checks passed.
- Closed findings:
  - Validation preview exposes only operation identity/kind, bounded safe input schema, and public output names; transport, auth, pointer, secret, and raw source data remain absent.
  - Mutated compiled mappings, identity collisions, cycles/depth overflow, and malformed schema keywords fail closed with uniform 409.
  - OpenAPI 3.0 boolean and 3.1 finite numeric exclusive bounds remain compatible without weakening ordinary-bound validation.
- Remaining findings: none.

## Declarative multi-operation tool builder phase

- Commits: `954cab9`, `c8407d3`, `98c2967`, `8fde364`
- Result: Spec PASS; independent frontend/contract review APPROVED after three remediation rounds.
- Verification: targeted frontend `47 passed`; full frontend `101 passed`; PyYAML differential matrix `17 passed`; production build and cumulative diff checks passed.
- Closed findings:
  - Tool keys and MCP names share one collision-safe namespace across tools and approved operation metadata.
  - Composite drafts enforce the backend's single-write-step rule and reserve immutable revisions before import requests, so ambiguous successful responses cannot trap retries on one revision.
  - JSON and YAML extension replacement preserves unrelated source and root comments, handles bounded multiline flow collections, and fails closed on malformed delimiters.
  - YAML flow token scanning matches the backend parser for tested nested collections, plain and quoted scalars, escapes, comments, URL/colon/hash cases, and trailing-content rejection.
- Remaining findings: none.

## Task 5 Layer 1 — login administration and rollout gates

- Commits: `c0c761f`, `6022f39`, `e777e15`, `5cc2b7a`
- Result: both independent high-risk reviews APPROVED after transaction and fail-closed rollout remediation.
- Verification: admin/auth/store/console `269 passed`; adversarial transaction rollback/retry `6 passed`; deploy regression `140 passed`; service regression `29 passed`; final deployment matrix `31 passed`; Ruff, YAML, and diff checks passed.
- Closed findings:
  - Tenant list exposes only account existence and nullable status; same-origin protections cover password/status mutations and auth remains first.
  - Tenant configuration and account/status/session writes share one transaction, so injected failures cannot partially commit or revive old sessions.
  - Deployment applies 004→008, validates the independent plaintext key, stages false health before optional true, and recovers every post-mutation error/signal to verified false.
  - Health retry inputs are bounded decimal values; key normalization matches application semantics; disabled mode retains admin cleanup and old MCP routes while closing tenant service management/runtime.
- Environment limits: no Linux Bash/Docker daemon or protected `.env`; no production deployment was executed.
- Remaining findings: none.

## Task 5 Layer 2 — admin controls and authorized rollout smoke

- Commits: `ccfcdf1`, `721201f`, `43c856e`, `462cc6b`, `2d0c5d6`, `d1c7fc6`, `96ed3c2`
- Result: frontend and operations reviews APPROVED after response-confirmation, exact-lock, authorization-gate, isolation-matrix, redaction, and placeholder-validation remediation.
- Verification: frontend `110 passed` plus production build; smoke/adversarial `113 passed, 1 skipped`; Python full suite at that checkpoint `1245 passed, 1 skipped`; all production preflight probes performed zero network calls.
- Closed findings:
  - Tenant passwords are create/reset-only secrets with allowlisted payloads, strict response confirmation, exact tenant locks, and stale-response guards.
  - Production smoke requires explicit authorization, non-placeholder endpoints/resources and correctly shaped high-diversity credentials; output is fixed and secret-free.
  - Service/connection/cross-connection/wrong-service isolation is covered by an exact `3 accepted + 11 rejected` matrix.
  - HMAC rotation documents the required maintenance-window switch/restart-before-issuance sequence; cleanup uses exact IDs and child-first transactional verification.
- Production smoke: not executed; written authorization and production credentials are absent.
- Remaining findings: none.

## Final branch review remediation

- Commits: `71d2b4d`, `d01850a`, `ef1fec2`, `d26bcf9`, `c252ad1`
- Service Token result: backend and frontend reviews APPROVED; focused backend `135 passed`, frontend `25 passed`, frontend full `114 passed`, Python checkpoint `1282 passed, 1 skipped`.
- Tenant retirement result: independent review APPROVED after tenant-first activation/create lock remediation; SQL/stateful `15 passed`, service-store `17 passed`, focused `222 passed`, Python checkpoint `1312 passed, 1 skipped`.
- Closed findings:
  - Plaintext reveal requires literal-`True` audit queue acceptance; audit rejection/exception returns generic no-store failure with no secret.
  - Service Tokens support strict timezone-qualified UTC expiry and transactional `last_used_at` updates before authentication succeeds.
  - Tenant deletion atomically disables resources, revokes Tokens, clears service ciphertext, removes live auth roots, inserts safe audit, and deletes tenant config last; all failures roll back and caches invalidate only after commit.
  - Service/connection resolvers and activation/creation writers enforce live tenant fences with tenant-first deterministic lock order.
- Environment limitation: live MySQL 5.7 concurrency/next-key locking remains unexecuted; covered through SQL-shape and stateful transaction probes.
- Remaining findings: none.

## Final cross-task release gate

- Head: `1f18bef`
- Result: final branch review APPROVED after closing tenant retirement, mandatory reveal audit, service Token expiry/usage, tenant-first activation/create locks, and tenant-first service Token authentication.
- Fresh lead verification:
  - Python: `1315 passed, 1 skipped` (`python -m pytest -q`); the skip is the authorization-gated live production smoke.
  - Frontend: `114 passed` (`node --test admin-ui/src/pages/*.test.js`).
  - Production build: Vite succeeded with `4105 modules transformed`; existing large-chunk advisory only.
  - Quality: all branch-changed Python files passed Ruff; `python -m compileall -q app tests` passed.
  - Deployment contracts: Compose health requires `mcp_service_enabled`; migrations `004` through `008` precede image pull.
- Environment-only limitations:
  - No live MySQL 5.7 instance for true InnoDB concurrency/next-key-lock execution.
  - No Linux Bash or protected production `.env`/Docker deployment environment.
  - No written production authorization or credentials; production smoke was not executed.
- Remaining findings: none.
