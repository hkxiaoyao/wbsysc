# Cloud MySQL gateway migration execution

## Target

- Server version: MySQL `5.7.44-log`
- Center database: `wbsysc`
- Tenant: `tenant1`
- Tenant schema: `wbd_8c0bd0b7d127`

Credentials are intentionally not recorded.

## Preflight

- Confirmed the current database contains `tenant_config`.
- Confirmed the account has ALTER, CREATE, CREATE ROUTINE, and DROP privileges required by migration 004.
- Confirmed all central report/approval rows already exist in the tenant schema; no historical business rows required relocation.
- Confirmed the tenant schema already contained the five business/support tables.
- Missing before migration: center `data_mode` and report/approval `source_window_start`, `source_window_end`, `is_partial`.

## Execution

- Parsed and executed `sql/004_gateway_hardening.sql` using MySQL delimiter semantics.
- First sequential execution: successful, 8 statements.
- Second sequential execution: successful, 8 statements (idempotency exercise).
- Ran `app.db.run_startup_migrations()` successfully as the application-side verification path.

## Post-migration verification

- All five required center columns are present.
- `tenant1` retained `data_mode=stored`.
- All five tenant tables are present.
- All six report/approval source-window and partial columns are present.
- No `migrate_gateway%` stored procedures remain.
- Representative report and approval queries using the new columns succeeded.
- Tenant row counts after migration: reports 3, approvals 1, check-ins 4, cursors 3, audit rows 7; these match preflight counts.

## Review availability

- Gemini review was attempted but unavailable because `GEMINI_API_KEY` is not configured.
- Claude classified the migration as additive and non-destructive, with a conditional go after the target database, table layout, privileges, and historical-row placement were verified.

## Operational warning

No cloud-provider snapshot was created by this task. The migration contains additive DDL and does not delete business tables or rows. The supplied database credential is weak and the MySQL endpoint is internet-addressable; rotate the password and restrict port 3306 to trusted source IPs immediately.

## Verdict

Migration completed and verified successfully.
