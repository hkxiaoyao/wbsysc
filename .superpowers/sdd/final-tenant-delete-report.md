# Final tenant deletion remediation report

## Outcome

Tenant deletion is now one fail-closed central-database transaction. It locks the
tenant, then sorted connection and service ranges; disables live children; revokes
service and connection bearer credentials; clears service Token ciphertext; removes
sessions, the login account, and domain verification; writes the durable safe audit;
and deletes `tenant_config` last with an exact row-count assertion. Historical
services, bindings, connections, credentials, policies, revisions, operations,
call logs, and tenant schemas are retained.

Both service and connection Token resolvers now require a live enabled
`tenant_config` row. Token issuance and the externally used connection/service
creation paths acquire a live tenant lock before their child lock/write. Exact
captured connection versions and service IDs are invalidated only after commit;
individual invalidator failures do not stop remaining cleanup, and legacy tenant
reload is attempted last.

Reusing a deleted tenant ID is rejected while retained connection or service
history exists.

## Adversarial coverage

- Stateful rollback snapshots after every mutation phase, including audit insert
  and final tenant deletion.
- Tenant-B isolation and ownership-joined Token revocation.
- Retry after rollback revokes a Token issued between attempts.
- Disabled or missing tenant roots deny service and connection Tokens even when
  historical parents are reactivated.
- Caller-owned account/session deletion transaction with session-first ordering.
- Tenant -> sorted connections -> sorted services lock order and tenant-scoped SQL.
- No `DROP` and no destructive deletion of retained service, connection, Token,
  credential, revision, operation, log, or schema history.
- Safe count/request-only audit content and audit-failure rollback.
- Cache invalidation only after a returned commit result, exact captured keys,
  continue-on-error behavior, and legacy reload last.

## Verification

- Focused lifecycle/auth/token/connection/gateway suite: `202 passed`.
- Full Python suite: `1307 passed, 1 skipped` (the existing authorization-gated
  live smoke test).
- Ruff on every owned changed Python file: passed.
- `python -m compileall -q app tests`: passed.
- `git diff --check`: passed.

## Environment limitation

No live MySQL 5.7 server was available. InnoDB next-key locking, binary columns,
rollback, and two-connection issue-vs-delete/create-vs-delete races are therefore
covered by SQL-shape and stateful transactional probes, not a live MySQL run.

## Tenant-first lock re-review

The follow-up review added exact tenant-first locking to service activation and
declarative revision activation. Tenant creation now locks the exact absent
`tenant_config` key/gap before ordered connection history and ordered service
history ranges, then inserts the tenant root. Stateful probes verify that a
missing or disabled tenant stops before any service, connection, or revision
lock/write.

The service-store fake now models `tenant_config` explicitly and includes
missing/disabled tenant counterexamples that stop before the service lock. The
related store/lifecycle/admin suite passes with `222 passed`; the complete Python
suite passes with `1312 passed, 1 skipped`. Ruff, compileall, and diff checks pass.
