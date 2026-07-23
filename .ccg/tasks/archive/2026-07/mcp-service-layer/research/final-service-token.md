# Final service-token lifecycle research

## Executive decision

The final-review findings are real and localized. The reveal path currently discards the boolean returned by `write_event`, so a full audit queue or closed writer can return `False` while plaintext is still sent (`app/mcp_services/router.py:200-230,307-374`; `app/mcp_audit.py:155-177,269-274`). Token expiry is already represented in SQL and read-side predicates, but it is not accepted or persisted by either issue endpoint (`app/mcp_services/router.py:66-67,470-481,593-605`; `app/mcp_services/store.py:729-780`). `last_used_at` is selected for metadata but never written (`app/mcp_services/store.py:783-802,833-851`; `sql/008_mcp_service.sql:85-99`).

Recommended contract:

1. A reveal is successful only if authorization, ownership/availability, decryption, and mandatory safe success-audit **queue acceptance** all succeed. Only the literal boolean `True` from `write_event` counts as acceptance.
2. A rejected or throwing success-audit sink returns the existing generic `500 {"detail":"service operation failed"}` with `Cache-Control: no-store`; the response, exception, application log, and audit payload contain no plaintext.
3. Denial/error audits remain best-effort and must never replace the original safe 401/403/404/429/500 response. Do not recursively submit another audit when mandatory success-audit acceptance fails.
4. Optional expiry is an explicitly timezone-qualified RFC 3339 string, normalized to UTC and stored as UTC-naive, whole-second `DATETIME`. Omitted or JSON `null` remains non-expiring. A token is expired at `expires_at <= UTC_TIMESTAMP()`; active predicates continue to use strict `expires_at > UTC_TIMESTAMP()`.
5. Service-token authentication becomes a write transaction: select and lock one exact eligible token/service row, update that token's `last_used_at=UTC_TIMESTAMP()`, and commit before returning the service. Any update or commit failure fails authentication closed. No update occurs for wrong-service, wrong-HMAC, revoked, expired, or inactive-service attempts.

## Files Found

- `docs/superpowers/specs/2026-07-17-mcp-service-layer-design.md:105-132,148-157,270-300,311-329` — authoritative service-token fields, reveal secrecy/audit rule, AND authorization semantics, compatibility contract, and acceptance criteria.
- `.ccg/tasks/mcp-service-layer/requirements.md:1-12` — owning tenant/admin may reveal service tokens; legacy connection-token behavior remains compatible.
- `.ccg/tasks/mcp-service-layer/plan.md:49-53` — final remediation explicitly requires mandatory accepted reveal audit, optional strict UTC expiry, and successful-use-only `last_used_at`.
- `.ccg/tasks/mcp-service-layer/review.md:23-43` — prior token/API reviews and their no-store/isolation assumptions.
- `app/mcp_services/router.py:35-68,139-174,200-374,457-513,582-637` — strict request DTOs, timestamp projection, reveal auditing/guards, tenant/admin issue/reveal/revoke adapters.
- `app/mcp_services/manager.py:72-86` — thin issue/list/reveal/revoke use-case delegation and compatibility-sensitive signatures.
- `app/mcp_services/store.py:729-874` — issue transaction, authentication query, reveal eligibility, metadata list, and revoke transaction.
- `app/mcp_services/models.py:54-80` — raw-token and metadata models; expiry/last-use fields already exist only on metadata.
- `app/mcp_service_gateway.py:70-100,304-340` — resolver converts every token-store exception into an indistinguishable invalid-token result before gateway 401.
- `app/mcp_audit.py:76-205,238-274` — asynchronous bounded audit queue; `True` means accepted into the queue, `False` means closed/start failure/full, while later persistence failure is only logged by the worker.
- `app/mcp_services/crypto.py:1-31` — HMAC and plaintext encryption boundary; authentication must not select/decrypt ciphertext.
- `sql/008_mcp_service.sql:85-99` — `expires_at` and `last_used_at` already nullable `DATETIME`; no schema migration is needed.
- `tests/test_mcp_service_api.py:16-140,237-404` — shared tenant API fake plus current no-store, rate-limit, boundary, audit, disabled-service recovery, and issue contracts.
- `tests/test_admin_security.py:100-190` — admin authentication/CSRF and reveal tenant/service/token isolation coverage.
- `tests/test_mcp_service_tokens.py:25-171,174-298` — fake SQL engine and token secrecy/locking/ownership/revoke/list tests; it must learn expiry and usage-update SQL.
- `tests/test_mcp_service_gateway.py:439-489` — requested-service binding and generic invalid-token behavior.
- `admin-ui/src/pages/ServiceTokenModal.jsx:19-181,215-265` — shared tenant/admin issue/reveal UI; issue currently sends label only and token rows do not display expiry/last-use.
- `admin-ui/src/pages/servicesView.js:94-103,165-170` — reveal eligibility currently checks revocation only; safe error allowlist already collapses backend 500s.
- `admin-ui/src/pages/servicesView.test.js:144-167,232-259` — token secrecy, reveal eligibility, stale-response, and sensitive-state tests.

## Dependencies

```text
tenant/admin POST .../tokens
  -> router.TokenIssue validation and UTC normalization
  -> ServiceManager.issue_token(..., expires_at=None)
  -> store.issue_token(..., expires_at=None)
  -> lock owned active mcp_service
  -> validate expiry against database UTC clock
  -> INSERT mcp_service_token(..., expires_at)
  -> no-store plaintext issue response

POST .../tokens/{token_id}/reveal
  -> tenant/admin auth + same-origin guard
  -> rate limiter
  -> manager/store ownership + active-token reveal check
  -> decrypt plaintext in memory
  -> mandatory safe success event -> write_event(event) is True
  -> no-store plaintext response

/mcp/service/{service_id}
  -> ServiceResolver.resolve(raw_token, path service_id)
  -> store.resolve_token HMAC-only transaction
  -> exact eligible token/service row lock
  -> UPDATE last_used_at for that token
  -> commit
  -> ServiceContext and normal gateway execution
```

The same `ServiceTokenModal` and backend router serve both tenant and admin scopes, so there must be one expiry payload/normalization contract, not separate behavior by principal. Legacy `connection_token`, `/mcp`, and `/mcp/{connection_id}` do not enter these call chains and must remain untouched.

## Exact reveal semantics

### Mandatory success audit

Refactor `_audit_reveal` to return an acceptance boolean. It should build exactly the current safe event. For `result="ok"`, the event may contain the authenticated tenant plus validated service/token identifiers and request metadata; it must never contain raw token, HMAC, ciphertext, Cookie, Authorization, body, or exception text (`router.py:209-227`). Treat `write_event(event) is True` as accepted. `None`, truthy non-boolean mocks, `False`, and exceptions are not accepted.

The reveal order must be:

1. Set the response no-store header before any operation.
2. Run auth/origin/rate/ownership/revoked/expiry checks.
3. Decrypt into a local variable.
4. Submit the safe success event.
5. If and only if submission returns literal `True`, construct and return `{"token": raw_value}`.

If step 4 returns anything else or raises, clear/drop the local plaintext reference as soon as practical and raise `HTTPException(500, "service operation failed", headers={"Cache-Control":"no-store"})`. Log only a fixed message plus the exception type, matching existing secrecy patterns; never interpolate the exception or raw value. Do not return a token field, partial DTO, token prefix, service ID, or token ID in that failure response.

This contract deliberately means “accepted by the bounded audit queue,” not “durably inserted.” `write_event=True` can still be followed by an asynchronous storage failure (`app/mcp_audit.py:190-197`). Making durable persistence mandatory would require a new synchronous/acknowledged sink and is outside the current review finding. The implementation and tests should name this distinction explicitly.

### Denial/error audit behavior

- Authentication and same-origin denials retain their original 401/403, no-store header, and safe best-effort audit. Before authentication, audit only `principal_type`; do not include user-controlled path identifiers (`router.py:238-304`).
- Rate-limit denial retains 429; unavailable/wrong-tenant/wrong-service/wrong-token/revoked/expired reveal retains indistinguishable 404; unexpected domain/decrypt failure retains generic 500. All remain no-store.
- A denial/error audit returning `False`, `None`, or throwing must be logged safely and ignored; it must not transform a 401 into 500, reveal resource existence, retry recursively, or permit plaintext.
- A mandatory success-audit failure must not trigger a second `service_token_reveal` denial/error submission. The sink is already unavailable, and recursion/duplicate semantics would be misleading. The safe application warning is the only fallback.
- Current tests use `events.append`, which returns `None`. Success-path tests must instead inject a sink such as `lambda event: events.append(event) or True`. Denial tests may continue to use a false/throwing sink specifically to prove original responses are preserved.

## Optional UTC expiry contract

### Input and normalization

Add `expires_at` to `TokenIssue` and thread it through both tenant/admin adapters, `ServiceManager.issue_token`, and `store.issue_token`. Exact accepted wire contract:

- omitted or JSON `null`: `None`, preserving every existing caller and existing non-expiring row;
- otherwise: a JSON string in RFC 3339 date-time form with seconds and an explicit `Z` or `+/-HH:MM` offset;
- reject numbers/epoch values, booleans, arrays/objects, date-only strings, a space in place of `T`, missing offset, invalid offsets/dates/leap seconds, and fractional seconds (the current MySQL column is `DATETIME` without fractional precision);
- normalize any valid offset to UTC, then remove `tzinfo` only at the SQL boundary because MySQL `DATETIME` is timezone-naive;
- reject an instant not strictly greater than the database's current UTC second. Equality is already expired and must be rejected.

Use one shared parser/normalizer rather than relying on Pydantic's permissive datetime coercion. The store must still defensively validate direct Python callers: `None` or `datetime`, no booleans/strings, whole-second precision, normalize aware datetimes to UTC or require the manager's normalized UTC-naive value, and reject non-future values using the database clock. A single documented helper in `models.py` can keep router/store behavior identical.

For response metadata, treat naive DB datetimes as UTC and serialize canonical UTC (prefer `YYYY-MM-DDTHH:MM:SSZ`) rather than the current timezone-less `isoformat()` (`router.py:139-150`). This applies consistently to `expires_at`, `last_used_at`, and `created_at`; it avoids browsers interpreting server UTC as local time. If minimizing response churn is mandatory, at least expiry/last-use must be emitted with `Z`, but one canonical timestamp helper is safer.

### Persistence and boundary

`sql/008_mcp_service.sql` and the runtime fallback DDL already contain both columns, so do not add migration 009 or alter existing data. Extend the insert column/value list and bind `expires_at`; never interpolate it. Existing rows with NULL remain non-expiring. Reveal and authentication already use the correct strict-active condition:

```sql
t.expires_at IS NULL OR t.expires_at > UTC_TIMESTAMP()
```

At exactly the stored second the token is expired. Apply the same rule to issue-time validation and UI eligibility. Reveal remains allowed for an unexpired token on a disabled service as the existing recovery contract requires (`tests/test_mcp_service_api.py:366-382`); runtime authentication still requires `s.status='active'`.

Recommended issue transaction: keep the owned-active service `FOR UPDATE` read, include `UTC_TIMESTAMP() AS db_now`, compare normalized expiry to that database value, then insert the row with `expires_at`. The service lock preserves current status/issue serialization. An expiry only one second in the future may naturally expire before the client uses it; no undocumented minimum TTL should be invented.

### UI behavior

Add an optional expiry control to `ServiceTokenModal` shared by tenant/admin. Convert the selected local instant to canonical UTC before sending; never send a timezone-less browser value. Reset it on close, service switch, and successful issue alongside the label and raw-token state. Display canonical expiry and last-used metadata. Client-side reveal eligibility is advisory but must fail closed for invalid dates and return false when `expires_at <= now`; inject/pass `now` into the pure helper for deterministic tests. Keep the backend authoritative.

Do not collapse expired into “已撤销.” The row needs distinct active/expired/revoked tags. An expired token cannot reveal or authenticate, but it may still be explicitly revoked/cleaned up; therefore reveal/copy and revoke eligibility should be separate helpers/actions.

## `last_used_at` transaction/query strategy

Use `_engine().begin()`, not `.connect()`, so the timestamp is committed before authentication success is returned. Compute only the HMAC digest; raw token must never appear in SQL parameters, logs, reprs, or the selected columns.

Recommended query sequence:

```sql
SELECT t.token_id,
       s.service_id, s.tenant_id, s.display_name, s.service_key,
       s.status, s.config_version
FROM mcp_service_token t
JOIN mcp_service s ON s.service_id=t.service_id
WHERE t.service_id=:service_id
  AND t.token_hmac=:token_hmac
  AND t.revoked_at IS NULL
  AND (t.expires_at IS NULL OR t.expires_at > UTC_TIMESTAMP())
  AND s.status='active'
LIMIT 1
FOR UPDATE;
```

If no row exists, return `None` with no update. If a row exists, update by the selected immutable identity inside the same transaction:

```sql
UPDATE mcp_service_token
SET last_used_at=UTC_TIMESTAMP()
WHERE token_id=:token_id
  AND service_id=:service_id
  AND token_hmac=:token_hmac
  AND revoked_at IS NULL
  AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP());
```

The joined `FOR UPDATE` read serializes a same-token authentication with revoke and service-status changes; the second predicates are defense in depth. Do not decide validity from update `rowcount`: MySQL affected-row behavior and second-resolution `DATETIME` can report zero when concurrent/same-second requests write the same timestamp. The locked eligible row is the validity decision; an update/commit exception is failure. Return `_service_from_row` only after the transaction context exits successfully.

This makes usage persistence security-relevant and fail-closed. A write/update/deadlock/commit failure propagates from the store; `ServiceResolver` already catches it, logs only its type, and returns no context, producing the same generic 401 as an invalid bearer (`app/mcp_service_gateway.py:80-100,304-323`). That preserves token secrecy and prevents a usable request with missing usage evidence. The availability cost is an extra short write transaction and serialization of simultaneous uses of the same token. It is preferable to a detached best-effort update, which can authenticate after an update failure, race revocation/expiry/status changes, or update a token that was not the successful authorization subject.

Concurrent uses may share the same whole-second timestamp; that is acceptable because `last_used_at` is a high-water mark, not a request counter. It must never move backwards. Database `UTC_TIMESTAMP()` avoids application-clock skew. Deadlocks should fail closed through the existing generic invalid-token path; do not retry with raw-token logging. If bounded retry is later introduced, retry the complete transaction and retain only the HMAC digest in memory/parameters.

## Test matrix

| File | Required cases |
| --- | --- |
| `tests/test_mcp_service_api.py` | Tenant success sink returns `True` -> 200/no-store/plaintext and one safe `ok` event; success sink returns `False`, `None`, or throws -> generic 500/no-store/no token/no secret; ensure no second audit; denial sink false/throws preserves auth 401, CSRF 403, rate 429, unavailable 404; issue omitted/null expiry is accepted; valid `Z` and non-zero offset normalize identically; malformed/naive/fractional/numeric/bool/past/equal-now rejected with no issue call; response/list timestamps canonical UTC. Update `FakeManager.issue_token(..., expires_at=None)` and audit stubs to return `True` on successful reveal. |
| `tests/test_admin_security.py` | Mirror mandatory-success sink False/throw for admin; admin issue passes normalized expiry and keeps auth-before-CSRF/body side effects; wrong tenant/service/token denials remain indistinguishable/no-store even if denial audit fails. |
| `tests/test_mcp_service_tokens.py` | Insert binds `expires_at` and contains no raw token; omitted remains NULL; direct store rejects invalid type/precision/past/equality using DB time; before-boundary resolves and equality/after-boundary does not; successful exact match updates only selected token in same transaction and commits before return; wrong service/HMAC, revoked, expired, inactive service perform no update; update and commit failures yield no service; same-second/concurrent success does not rely on rowcount; reveal respects expiry but disabled-service reveal remains supported; parameters/repr never contain raw token. Extend fake rows with expiry/last-use/database clock and transaction commit-failure injection. |
| `tests/test_mcp_service_gateway.py` | Successful resolver calls usage-updating store once; update/store exception yields generic invalid-token 401 and does not enter manager/tool execution; wrong-service/revoked/expired remain the same 401; no raw bearer in logs/audit. |
| `tests/test_mcp_service_migration.py` | Assert 008 already has nullable `expires_at`/`last_used_at`, UTC-compatible predicates/index shape, and remains idempotent; no destructive schema change. |
| `admin-ui/src/pages/servicesView.test.js` | Expiry payload conversion requires a valid instant and emits UTC `Z`; omitted stays null/omitted per chosen payload helper; `tokenCanReveal` false at equality and after expiry, false on malformed metadata, true one millisecond before; revoked wins; separate revoke eligibility for expired tokens; deterministic injected clock; safe generic audit-failure message cannot display response secret/exception. |
| `admin-ui/src/pages/ServiceTokenModal.jsx` focused/source tests | Both tenant/admin scopes send the same expiry field; reset expiry on close/switch/success; render active/expired/revoked distinctly; expired has no reveal/copy but retains revoke; list/prefix never becomes copyable; raw state still clears on failure/unmount/stale response. |
| Full regression | `tests/test_mcp_service_api.py tests/test_admin_security.py tests/test_mcp_service_tokens.py tests/test_mcp_service_gateway.py tests/test_mcp_service_migration.py`, then full Python suite; frontend `servicesView.test.js`, full Node suite, and production build; Ruff/diff checks. |

For true database concurrency confidence, add a MySQL 5.7 integration case with two connections: authenticate versus revoke, authenticate versus service disable, two simultaneous same-token authentications, and an injected deadlock/commit failure. Assert a linearizable outcome (authentication either commits `last_used_at` while eligible or fails) and no plaintext/ciphertext selection on the auth path.

## Patterns

- Fail-closed token predicates already use strict `>` against `UTC_TIMESTAMP()` (`app/mcp_services/store.py:789-799,810-820`); preserve this exact boundary.
- Token issue already locks the owned active service and inserts within one transaction (`app/mcp_services/store.py:747-780`); add DB-time expiry validation without weakening that lock.
- Reveal failures already share generic/no-store responses and omit identifiers from non-success audits (`app/mcp_services/router.py:200-235,307-365`). Make only success acceptance mandatory.
- `write_event` intentionally exposes queue acceptance as a boolean and absorbs its own exception (`app/mcp_audit.py:155-177,269-274`); callers that release plaintext must consume that boolean.
- Resolver failures are intentionally indistinguishable from invalid credentials (`app/mcp_service_gateway.py:85-95,313-321`), which is the correct fail-closed behavior for usage-write failures.
- Raw-token authentication uses HMAC and never selects ciphertext; only reveal selects encrypted plaintext (`app/mcp_services/store.py:783-830`; design spec lines 122-132).
- Metadata list already includes expiry and last-use without HMAC/ciphertext (`app/mcp_services/store.py:833-851`), so lifecycle output is additive rather than a new endpoint.
- Existing domain convention stores UTC in naive MySQL datetimes (`app/mcp_logs_admin.py:95-108`; `app/mcp_log_models.py:55-61`); normalize explicitly at boundaries.

## Risks

- **Audit durability:** `True` only confirms enqueue, not database persistence. Do not claim a durable mandatory audit unless the audit subsystem gains acknowledgements.
- **Mock compatibility:** `list.append` returns `None`; enforcing literal `True` will intentionally break current success-test stubs until updated.
- **Timestamp ambiguity:** current `.isoformat()` emits naive values from MySQL. Without a `Z`, browser parsing can shift expiry/last-use by the client timezone.
- **Pydantic coercion:** a plain `datetime | None` field accepts more wire shapes than the strict contract. A before-validator/string grammar is required.
- **MySQL precision:** the existing `DATETIME` columns are whole-second. Accepting fractional input without a schema change creates truncation and equality-boundary bugs.
- **Rowcount:** same-second usage updates may be “unchanged” and report zero. Never equate update rowcount with invalid authentication after a locked eligible select.
- **Concurrency/throughput:** a write and row lock on every successful auth serializes same-token bursts and increases DB writes. This is the cost of fail-closed usage evidence; monitor contention and issue multiple tokens per high-volume client if needed.
- **Deadlocks:** joined token/service locking can interact with revoke, status, tenant deletion, and service lifecycle transactions. Keep transactions short, use consistent token/service lock ordering where those paths overlap, and test on MySQL 5.7.
- **UI clock skew:** client expiry state is advisory. Server DB time remains authoritative; a reveal can still safely return 404 if the browser thought the token active.
- **Expired cleanup:** hiding revoke together with reveal would strand expired ciphertext. Separate revealability from revocability and preserve explicit cleanup.
- **Scope creep:** do not alter connection tokens, legacy MCP routes, plaintext-key semantics, or SQL history. Columns already exist in 008.
