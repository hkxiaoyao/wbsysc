# Final review research: tenant deletion lifecycle

## Recommendation

Use one central-database transaction that locks the tenant lifecycle, disables every tenant-owned MCP service and connection, revokes every service/connection bearer Token, removes tenant sessions/account, deletes the verification file, writes a durable deletion audit row, and deletes `tenant_config` last. Preserve service/connection definitions, bindings, encrypted connection data, declarative revisions, call logs, and the tenant history schema. After commit, invalidate the exact service and connection gateway caches and reload the legacy tenant cache.

Also make both non-legacy Token resolvers require a live, enabled `tenant_config` row. This is the final authorization fence if historical child rows are accidentally reactivated or a concurrent/post-delete writer creates an orphan. A request admitted before the deletion commit may finish; no request beginning authentication after commit may succeed.

Do **not** choose the 409-only alternative as the primary remediation:

- there is no service-delete lifecycle, so “remove all services first” has no usable closure;
- `connection_store.delete_connection` removes only `connection_instance` and deliberately leaves credentials, Tokens, policies, sync state, and revisions (`app/connections/store.py:585-622`), so deletion is not a historical cleanup primitive;
- merely requiring disabled children/revoked Tokens does not prevent later admin mutation of orphan rows because the stores generally do not consult `tenant_config`;
- without foreign keys, a check followed by tenant deletion is vulnerable to concurrent child creation/issuance unless all relevant parent ranges and issue rows are locked.

The atomic soft-retirement design is the smallest compatible implementation that satisfies the canonical requirement to disable services and revoke Tokens while preserving history (`docs/superpowers/specs/2026-07-17-mcp-service-layer-design.md:302-309`).

## Files Found

- `app/admin.py:478-491` — current route deletes `tenant_config`, commits, then calls account deletion in a second transaction; verification deletion is separately committed and exceptions are swallowed.
- `app/tenant_auth/store.py:77-82, 177-227, 269-303` — session revocation already accepts a caller transaction indirectly; `delete_account` does not. Session resolution already joins live/enabled `tenant_config`.
- `app/mcp_services/store.py:675-726, 729-874` — service status, issue, resolve, reveal, list, and revoke paths. Service Token resolution checks only Token + active service, not tenant existence.
- `app/mcp_services/store.py:404-440, 443-453, 573-607` — service cache callback registry and current connection-to-service invalidation logic.
- `app/connections/store.py:192-242` — exact connection-version invalidation runs after commit and cascades to service invalidation.
- `app/connections/store.py:1243-1352, 1472-1618, 1660-1679` — connection Token issue/rotate/revoke and connection disable. Resolution checks only Token + active connection, not tenant existence.
- `app/mcp_service_gateway.py:70-100` and `app/mcp_gateway.py:119-167` — every HTTP authentication is delegated to the stores; cached session managers are reached only after resolution.
- `app/main.py:422-447, 502-519` — service cache invalidator removes every cached version for one `service_id` on the gateway event loop.
- `app/domain_verify.py:197-204` — verification deletion currently starts its own transaction; it needs an optional caller connection or an inline tenant-scoped delete.
- `app/mcp_log_store.py:262-295` — durable `mcp_call_log` insert shape; existing async `write_event` is not transaction-coupled.
- `sql/006_connection_platform.sql`, `sql/007_tenant_auth.sql`, `sql/008_mcp_service.sql` — all lifecycle tables are InnoDB but have no foreign keys. Ownership of child tables is inferred through `connection_instance` or `mcp_service`.
- `tests/test_admin_security.py:23-86, 922-1015, 1103-1237` — reusable rollback fake and current weak deletion test.
- `tests/test_mcp_service_lifecycle.py:204-478` — tenant-predicate, lock-order, conflict, history-preservation, and cache-invalidation patterns.
- `tests/test_connection_store.py:663-699, 953-1003` and `tests/test_mcp_service_tokens.py:175-269` — Token resolver/issue/revoke and post-commit invalidation coverage to extend.

There is no `.ccg/spec/` directory in this worktree. The canonical source is the design spec above plus `.ccg/tasks/mcp-service-layer/requirements.md` and `.superpowers/sdd/mcp-service-branch-review.md`.

## Dependencies

```text
DELETE /admin/tenants/{tenant_id}
  -> app.admin.delete_tenant
     -> central InnoDB transaction (recommended tenant lifecycle use case)
        -> tenant_config lifecycle row lock
        -> connection_instance ordered row/range locks
        -> mcp_service ordered row/range locks
        -> service + connection disable/revoke
        -> tenant_session + tenant_account removal
        -> domain_verify_file removal
        -> durable audit insert
        -> tenant_config delete last
     -> transaction commit
     -> connection store invalidators (id, old config_version)
        -> ConnectionMcpGateway cache retirement
        -> referenced ServiceMcpGateway cache retirement
     -> direct service-id invalidators
     -> reload_tenants (legacy `/mcp` cache)

/mcp/service/{service_id}
  -> ServiceResolver
  -> mcp_services.store.resolve_token
  -> Token + active service + live enabled tenant (required)

/mcp/{connection_id}
  -> ConnectionResolver
  -> connections.store.resolve_connection_token
  -> Token + active connection + live enabled tenant (required)

/mcp legacy
  -> cached tenant lookup -> default connection resolution
  -> reload_tenants after commit + same live-tenant connection resolver fence
```

## Exact transaction plan and SQL

Prefer a narrow new module, `app/tenant_lifecycle.py`, called by `app/admin.py`. It avoids making the route own cross-domain SQL and returns a frozen result containing the affected service IDs and `(connection_id, old_config_version)` keys for post-commit invalidation. It must use the same `get_engine()` central database as all tables below.

Within `with get_engine().begin() as conn`, use this order:

1. Lock and prove the exact tenant exists. Fetch `schema_name` for the response/audit but never drop it.

   ```sql
   SELECT tenant_id, schema_name
   FROM tenant_config
   WHERE tenant_id=:tenant_id
   LIMIT 1 FOR UPDATE
   ```

   No row means 404 with no mutation. The row lock serializes concurrent tenant update/delete.

2. Lock all tenant connections in deterministic key order and capture old versions. Under MySQL/InnoDB `REPEATABLE READ`, the indexed tenant range prevents phantoms for this tenant while the transaction is open.

   ```sql
   SELECT connection_id, config_version
   FROM connection_instance
   WHERE tenant_id=:tenant_id
   ORDER BY connection_id
   FOR UPDATE
   ```

3. Lock all tenant services in deterministic key order and capture IDs/versions.

   ```sql
   SELECT service_id, config_version
   FROM mcp_service
   WHERE tenant_id=:tenant_id
   ORDER BY service_id
   FOR UPDATE
   ```

   Connection-before-service matches existing binding replacement and connection deletion order (`app/mcp_services/store.py:909-985`, `app/connections/store.py:585-611`), avoiding a new inversion.

4. Disable services and bump versions. Keep already-disabled rows idempotent; bump only non-disabled rows so retry/caches are predictable.

   ```sql
   UPDATE mcp_service
   SET status='disabled', config_version=config_version+1
   WHERE tenant_id=:tenant_id AND status<>'disabled'
   ```

5. Revoke every tenant service Token and destroy reveal ciphertext. The join is the required ownership predicate; do not update by a caller-supplied list alone.

   ```sql
   UPDATE mcp_service_token AS token_row
   JOIN mcp_service AS service_row
     ON service_row.service_id=token_row.service_id
   SET token_row.revoked_at=COALESCE(token_row.revoked_at, UTC_TIMESTAMP()),
       token_row.encrypted_token=NULL
   WHERE service_row.tenant_id=:tenant_id
     AND (token_row.revoked_at IS NULL OR token_row.encrypted_token IS NOT NULL)
   ```

6. Disable connections and bump versions, then revoke every connection Token through its owned parent.

   ```sql
   UPDATE connection_instance
   SET status='disabled', config_version=config_version+1
   WHERE tenant_id=:tenant_id AND status<>'disabled';

   UPDATE connection_token AS token_row
   JOIN connection_instance AS connection_row
     ON connection_row.connection_id=token_row.connection_id
   SET token_row.revoked_at=COALESCE(token_row.revoked_at, UTC_TIMESTAMP())
   WHERE connection_row.tenant_id=:tenant_id
     AND token_row.revoked_at IS NULL;
   ```

7. Delete all sessions before the account, in the same caller transaction. Change `tenant_auth_store.delete_account(tenant_id, *, conn=None)` to use the existing `nullcontext(conn)` convention from `upsert_account`/`set_account_status`.

   ```sql
   DELETE FROM tenant_session WHERE tenant_id=:tenant_id;
   DELETE FROM tenant_account WHERE tenant_id=:tenant_id;
   ```

   Physical session/account deletion is compatible with current behavior and avoids retaining password hashes/session digests. Atomic rollback restores both if any later statement fails.

8. Delete the public domain-verification artifact inside this transaction, not via the current separate helper transaction.

   ```sql
   DELETE FROM domain_verify_file WHERE tenant_id=:tenant_id
   ```

9. Insert a durable management audit row on the same `conn`. Extend `mcp_log_store.insert_event(event, *, conn=None)` with the same optional-connection convention, or add a small transaction-aware insert helper. Suggested fields: `tenant_id`, `category='protocol'` (or a new validated management category only if the model permits), `event_name='tenant_deleted'`, `result_status='ok'`, `params_summary` containing only counts (`services=N,connections=N,service_tokens=N,connection_tokens=N`), admin request ID/IP/method, and no Token/schema/credential content. An insert failure should roll the deletion back; a queued post-commit event is not a durable proof of the destructive action.

10. Delete the tenant authorization root last and assert exactly one row.

   ```sql
   DELETE FROM tenant_config WHERE tenant_id=:tenant_id
   ```

   `rowcount != 1` is a fail-closed runtime error. Any exception rolls back every step, including audit and account/session changes.

After the context manager returns successfully only:

- call a public connection-store post-commit invalidator for every captured `(connection_id, old_config_version)`;
- call a public service-store invalidator for every captured `service_id` (it removes all cached versions);
- call `reload_tenants()` last to evict legacy tenant/Token state;
- log invalidator failures without changing the already-committed HTTP success. Authentication remains safe because resolvers read database state on every request; cache invalidation is defense-in-depth/resource cleanup, not the authorization decision.

Do not call any invalidator, `reload_tenants`, or external cleanup from inside the transaction. Do not call `delete_verify_by_tenant` or the current `tenant_auth_store.delete_account` unless they accept the active connection.

## Resolver defense in depth

Change both resolver queries, preserving exact route ID and digest matching:

```sql
-- service
FROM mcp_service_token AS token_row
JOIN mcp_service AS service_row
  ON service_row.service_id=token_row.service_id
JOIN tenant_config AS tenant_row
  ON tenant_row.tenant_id=service_row.tenant_id
WHERE token_row.service_id=:service_id
  AND token_row.token_hmac=:token_hmac
  AND token_row.revoked_at IS NULL
  AND (token_row.expires_at IS NULL OR token_row.expires_at>UTC_TIMESTAMP())
  AND service_row.status='active'
  AND tenant_row.enabled=1
LIMIT 1

-- connection
FROM connection_token AS token_row
JOIN connection_instance AS connection_row
  ON connection_row.connection_id=token_row.connection_id
JOIN tenant_config AS tenant_row
  ON tenant_row.tenant_id=connection_row.tenant_id
WHERE token_row.connection_id=:connection_id
  AND token_row.token_hmac=:token_hmac
  AND token_row.revoked_at IS NULL
  AND (token_row.expires_at IS NULL OR token_row.expires_at>UTC_TIMESTAMP())
  AND connection_row.status='active'
  AND tenant_row.enabled=1
LIMIT 1
```

This also fixes the pre-existing inconsistency where disabling `tenant_config.enabled` revokes tenant sessions but connection/service Tokens can still authenticate.

## Patterns

- Caller-owned transactions already use `nullcontext(conn)` in `tenant_auth_store.upsert_account` and `set_account_status` (`app/tenant_auth/store.py:177-215`). Extend that pattern rather than opening nested transactions.
- Connection mutations capture the retired `config_version` under `FOR UPDATE`, commit, then notify exact cache keys (`app/connections/store.py:1355-1420, 1472-1500, 1565-1584`). Tenant deletion should aggregate and replay the same pattern after commit.
- Cross-domain mutations lock connections before services (`app/mcp_services/store.py:909-985`; `tests/test_mcp_service_lifecycle.py:299-343`). Preserve it in bulk deletion.
- Ownership-sensitive SQL joins the child to its tenant-owned parent, as connection Token revoke already does (`app/connections/store.py:1565-1580`). Bulk service/connection Token revocation should follow this pattern.
- Destructive lifecycle conflicts return safe 409 payloads without foreign metadata (`app/admin_connections.py:962-983`; `tests/test_mcp_service_lifecycle.py:204-263`), although tenant deletion should use atomic retirement rather than a permanent 409 precondition.

For strict prevention of post-delete orphan writes, the long-term rule should be “lock live tenant first, then child” in every child create/activate/Token-issue transaction. The Critical can be closed minimally without rewriting every store because (a) deletion locks existing child rows/ranges, (b) it disables and revokes them, and (c) the live-tenant resolver join makes any post-delete orphan credential unusable. At minimum, add a live-tenant `FOR UPDATE` check to `create_service`, `create_connection_with_token`, standalone connection/service Token issue/rotate, and transitions to `active`; always acquire it **before** connection/service locks. Do not add that check after an existing child lock, or it creates tenant↔child deadlock potential with deletion.

## Historical retention and schema provisioning

Retain unchanged:

- `mcp_service`, `mcp_service_tool_binding`, and revoked `mcp_service_token` metadata (ciphertext cleared);
- `connection_instance`, encrypted `connection_credential`, revoked `connection_token`, tool policies, sync state, declarative specs/operations, and migration watermarks;
- `mcp_call_log`, including the deletion audit;
- the `wbd_*` tenant schema and its business/audit records.

Delete only the live authorization/config roots and public verification content: `tenant_config`, `tenant_account`, `tenant_session`, `domain_verify_file`. Do not issue `DROP SCHEMA`, cross-schema `DELETE`, or compensating DDL. This follows the existing `admin.py` promise to retain historical schema and the create/update convention that schema DDL is outside data transactions (`app/admin.py:393-396, 471-473, 478-479`).

Recreating the same `tenant_id` is currently not specified. Because retained rows use that string as ownership without foreign keys, recreation could inherit disabled historical resources. The minimal safe behavior is to reject reuse while any retained `mcp_service` or `connection_instance` row exists, or require an explicit separately audited restore workflow. `ensure_schema` must never silently make a deleted tenant live or reactivate retained rows. This should be covered even if UI currently generates tenant IDs manually.

## Token-after-delete matrix

| Credential / timing | Required result |
| --- | --- |
| Legacy `tenant_config.mcp_token`, request starts after commit | 401; tenant row/cache entry is absent and default connection resolver also requires live tenant |
| Existing active service Token, request starts after commit | 401; revoked + service disabled + live-tenant join absent |
| Existing active connection Token, request starts after commit | 401; revoked + connection disabled + live-tenant join absent |
| Revoked/expired/wrong-path Token | Remains 401; deletion must not weaken exact route-ID predicates |
| Token belonging to tenant B | Continues to work if B is enabled; every mutation contains tenant A ownership predicates |
| Token issuance committed just before deletion obtains child lock | Deletion waits, then revokes it in the same transaction; 401 after deletion commit |
| Token issuance waiting behind deletion child/tenant lock | It must revalidate live tenant/status after wake and fail; even if a legacy path inserts an orphan, resolver join makes it 401 |
| Authentication SELECT starts before deletion commit | May observe pre-delete state and admit that one in-flight request; this must be documented and bounded |
| Authentication begins after deletion commit | Must never authenticate, even if a stale gateway session-manager entry exists |
| Deletion transaction fails at any statement | Tenant config/account/sessions/statuses/Tokens remain exactly pre-delete; old Tokens behave exactly as before, and no cache invalidation fires |
| Deletion retry after rollback | Succeeds idempotently and revokes all Tokens, including Tokens issued between attempts |
| Same tenant ID recreated without explicit restore | Must be rejected while retained child history exists; old revoked Tokens never become valid |

## Tests to add or extend

### `tests/test_admin_security.py`

- Replace `test_delete_tenant_disables_login_account` with a transactional lifecycle fake that models tenant config, services, service Tokens/ciphertext, connections, connection Tokens, account, sessions, verification file, and audit rows.
- Assert the exact lock order is tenant → connections ordered by ID → services ordered by ID.
- Assert all state changes share the one `engine.begin()` connection and `tenant_config` is the final destructive statement.
- Parameterize failure injection after each lifecycle phase (service disable, service revoke, connection disable, connection revoke, session delete, account delete, verification delete, audit insert, tenant delete). Every failure restores the full original state, emits no invalidation/reload, and does not return `{ok:true}`.
- Assert unknown tenant is 404 and performs no child mutation or metadata disclosure.
- Seed tenant A and B plus deliberately colliding-looking IDs; assert only A changes and every child mutation SQL includes `tenant_id=:tenant_id` directly or via an owned-parent join.
- Assert post-commit invalidators receive exactly A’s captured service IDs and `(connection_id, old_version)` pairs, once, in sorted order. Assert invalidator exceptions are safely logged and do not expose data.
- Assert audit failure rolls back; audit row contains counts/request metadata but no raw Token, token HMAC, ciphertext, credential, password hash, or schema name.

### `tests/test_mcp_service_tokens.py`

- Update the fake resolver SQL handling for `JOIN tenant_config`.
- Add enabled tenant → success; disabled/missing tenant → `None`; foreign tenant cannot satisfy the join.
- Add old Token after successful lifecycle deletion → `None`, including a deliberately reactivated historical service row.
- Add issuance/delete barrier test or SQL lock-order test: issue-before-lock is caught and revoked; issue-after-delete revalidation fails.

### `tests/test_connection_store.py`

- Mirror the enabled/disabled/missing-tenant resolver cases for connection Tokens.
- Prove a reactivated historical connection plus unrevoked-or-new orphan Token still fails without `tenant_config`.
- If live-tenant checks are added to issue/rotate, cover absence/disabled tenant and verify no INSERT occurs.

### `tests/test_mcp_service_lifecycle.py`

- Add the cross-domain tenant deletion SQL-shape test: connection locks precede service locks; each parent list is ordered; all updates use tenant predicates.
- Add history-retention assertions: no `DROP`, and no DELETE from service, binding, Token history, connection, credential, policy, sync, revision, operation, call-log, or tenant schema tables.
- Add retry test with a Token issued after the first rolled-back attempt; the successful retry must revoke it.
- Add tenant-B isolation and corrupt/orphan-child cases. An orphan whose parent ownership cannot be proven must not be touched by an ID-only update; resolver fence still denies it if the tenant root is gone.

### `tests/test_mcp_service_gateway.py` / `tests/test_mcp_gateway.py`

- Populate a cached manager, commit deletion, run the registered invalidator, and assert exact service/connection cache keys are retired.
- Prove no invalidation is scheduled before commit or on rollback.
- Prove a stale cached manager cannot bypass a resolver denial on the next request.

### `tests/test_tenant_auth_store.py`

- Extend `delete_account` caller-transaction coverage: no nested transaction, session DELETE precedes account DELETE, and injected later failure in the outer transaction restores both.

### `tests/test_migrations.py` / integration test when MySQL 5.7 is available

- Assert all involved tables use InnoDB and document the intentional lack of FKs.
- Run two real connections with barriers for issue-vs-delete and create-vs-delete. Confirm ordered next-key locks prevent pre-commit phantoms, no deadlock occurs, and post-commit resolvers deny.

## Failure injection matrix

| Injection point | Required database state | Required side effects |
| --- | --- | --- |
| Tenant lock/read | unchanged | none; 404 only for genuine absence |
| After service disable | rollback restores service statuses/versions | no invalidation/reload/audit |
| After service Token revoke | rollback restores `revoked_at` and ciphertext | no invalidation/reload/audit |
| After connection disable | rollback restores statuses/versions | no invalidation/reload/audit |
| After connection Token revoke | rollback restores Tokens | no invalidation/reload/audit |
| After session/account delete | rollback restores both | no invalidation/reload/audit |
| Verification deletion | rollback restores verification row | no invalidation/reload/audit |
| Audit insert | full rollback | no success response |
| Tenant-config delete or rowcount mismatch | full rollback | no invalidation/reload |
| First post-commit invalidator throws | committed deletion remains authoritative | remaining invalidators and tenant reload still attempted; safe log only |
| Tenant cache reload throws | committed deletion remains authoritative | resolver DB joins still deny; safe operational error, no secret |

## Risks

- The established cross-domain order is connection before service. Tenant deletion must keep `tenant_config → sorted connections → sorted services`; never service then connection.
- Multi-row locks need `ORDER BY` stable primary key order. Unordered `FOR UPDATE` across concurrent bulk operations can deadlock.
- If live-tenant checks are added to child writers, they must be acquired first everywhere. Adding `tenant_config FOR UPDATE` after `connection_instance` or `mcp_service` creates a direct inversion against deletion.
- Do not call cache hooks, async audit writers, `reload_tenants`, schema DDL, or separate store transactions while holding database row locks. They lengthen the critical section and may acquire unrelated connections/locks.
- MySQL next-key behavior depends on an indexed `tenant_id` predicate and isolation level. Both `connection_instance` and `mcp_service` have tenant-leading indexes. Validate this on MySQL 5.7; fake tests cannot prove gap locking.
- Token tables lack foreign keys. Locking parents is the serialization point used by existing issue paths; bulk revoke must still join the tenant-owned parent rather than trust captured IDs.
- Cache invalidation is best effort and after commit. Do not roll back or return a false deletion failure because an in-process cache callback failed after the authoritative transaction committed.

## Exact implementation file plan

1. **Add `app/tenant_lifecycle.py`** — transaction result type, ordered locks, exact tenant-scoped disable/revoke/delete/audit SQL, and post-commit invalidation orchestration.
2. **Modify `app/admin.py`** — route delegates to lifecycle use case, maps genuine absence to 404, reloads cache only through post-commit flow; remove split account/verification transactions.
3. **Modify `app/tenant_auth/store.py`** — `delete_account(..., conn=None)` caller-transaction support.
4. **Modify `app/mcp_services/store.py`** — live-enabled tenant join in resolver; expose a small public exact-ID cache invalidation function. Optionally add tenant-first live checks to create/activate/issue.
5. **Modify `app/connections/store.py`** — live-enabled tenant join in resolver; expose a public wrapper for exact post-commit invalidation. Optionally add tenant-first live checks to create/activate/issue/rotate.
6. **Modify `app/mcp_log_store.py`** — optional caller transaction for durable audit insert (or an equivalently narrow transaction-aware helper).
7. **Modify `app/domain_verify.py` only if reusing its helper** — optional caller transaction; otherwise keep lifecycle SQL in the new module.
8. **Tests** — `tests/test_admin_security.py`, `tests/test_mcp_service_lifecycle.py`, `tests/test_mcp_service_tokens.py`, `tests/test_connection_store.py`, `tests/test_tenant_auth_store.py`, and focused gateway cache tests as detailed above.

No migration is required for the minimal fix. Adding foreign keys or a deleted-tenant tombstone would be a larger compatibility project because existing installs may contain orphan/history rows and because retained history intentionally outlives `tenant_config`.

## Acceptance criteria

- A successful response means legacy, connection, and service Tokens owned by the deleted tenant all return 401 on subsequent authentication.
- Any database failure leaves tenant configuration, account/sessions, statuses, Tokens, verification, and audit exactly unchanged.
- Tenant B is untouched and remains authorized.
- Historical central rows, call logs, and tenant schema are retained; Token reveal ciphertext is cleared on revoke.
- No cache invalidation occurs before commit; exact affected keys are invalidated after commit.
- Concurrent issue/create either completes before deletion and is included in revocation, or revalidates after deletion and fails; it can never produce a usable post-delete Token.
- Lock order is deterministic and compatible with connection-before-service lifecycle operations.
