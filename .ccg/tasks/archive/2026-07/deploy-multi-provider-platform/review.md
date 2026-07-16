# Production deployment review

## Verdict

Deployment completed successfully. Production health, connection creation, Token issuance, credential decryption, MySQL migrations, legacy log migration, public UI access, and cleanup checks passed.

## Deployment evidence

- PR #2 merged the multi-provider platform into `main`; PR #3 merged the MySQL 5.7 legacy-log hotfix.
- GitHub Actions published the final `latest` image from merge commit `3b706b9`.
- Server repository and running container use commit `3b706b9` and image digest `sha256:dbda3b35952cb9b4b9d0e542ae6b5b3997fa878c5d7ff293d181446d45eee80f`.
- Database backup: `/root/backups/wbsysc/20260717-000527/databases.sql.gz`; SHA-256 `75a32ad3827d3d77fc3bff3c6e0ebd0364893c3cf7066cdd42375b125ff1ab4f`.
- Migrations `004`, `005`, and `006` completed before container restart.
- Production health reports `env=prod`, `mock=false`, and a healthy scheduler.
- The connection creation and one-time MCP Token path succeeded twice; both diagnostic connections were removed and zero rows remained.
- Existing encrypted tenant fields were rotated transactionally to a new 64-character credential key and verified using the running container. No connection credentials existed during rotation.
- Legacy log migration completed with 7 migrated rows after fixing the MySQL 5.7 ambiguous source-column name.
- Temporary migration accounts were removed after each successful deployment; final count is zero.
- Public `/health` and `/admin/ui/` return success, and post-deploy logs contain no migration failure, traceback, ERROR, or CRITICAL entry.

## Verification

- Backend: `603 passed, 2 skipped`.
- MCP log store: `36 passed`.
- Ruff and `git diff --check`: passed.
- Frontend pre-deploy verification: `42 passed`; Vite production build and Ant Design lint passed.
- Required Gemini review could not start without `GEMINI_API_KEY`; Claude review timed out. The limitation was recorded and supplemented with regression tests, direct MySQL 5.7 execution, two independent deploy health checks, and production smoke cleanup.

## Required security follow-up

- Rotate the root SSH password because it was shared in chat.
- Invalidate the administrator session Token shared in chat.
- Replace the existing `wbsysc@%` runtime database account: it currently has global privileges and `WITH GRANT OPTION`. Create a separate least-privilege runtime user with grants limited to the center schema and known tenant schemas, verify the application, then revoke the broad account.
