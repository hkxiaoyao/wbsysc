# Project detail polish review

## Scope

Reviewed the implementation from base `5055757` through `3deffbf` against:

- `docs/superpowers/specs/2026-07-13-project-detail-polish-design.md`
- `docs/superpowers/plans/2026-07-13-project-detail-polish.md`
- the repository AGENTS.md requirements

## Review outcome

All task-level independent reviews finished with no remaining Critical or Important findings. The final focused review approved:

- per-tenant `stored` / `direct` routing without direct-to-cache fallback
- direct mode business-write prevention at the core sync entry point
- bounded direct detail requests, newest-window recursion, partial preservation, and pagination termination
- cursor safety, partial-row visibility, and tenant advisory-lock lifecycle
- production configuration and credential redaction
- MySQL 5.7-compatible baseline, upgrade SQL, runtime startup migration, and deployment gates

## External model review

- Gemini was attempted in parallel during the initial, post-fix, and final focused reviews. Every attempt failed with exit status 41 because `GEMINI_API_KEY` is not configured. No Gemini review is claimed.
- Claude initial review requested changes for upgrade completeness and partial-row ordering. Those findings were fixed with regression tests.
- Claude post-fix review found no Critical findings and approved merge after follow-up hygiene fixes.
- Claude final focused review of `bbcd31c..3deffbf` found no Critical findings and approved merge. Its remaining warning is that recursive capped reads can increase WeCom list API calls in dense windows, although detail calls are strictly bounded by `limit`.

## Fresh verification evidence

- `python -m compileall -q app`: exit 0
- `python -m pytest -q`: `133 passed, 2 skipped, 1 warning`
- `pnpm --dir admin-ui build`: exit 0
- `docker compose config -q`: exit 0
- `bash -n deploy/server_deploy.sh`: exit 0
- `git diff --check`: exit 0

The two skipped tests are opt-in live MCP smoke tests gated by `MCP_SMOKE_RUN=1`. The warning is the existing Starlette `httpx` deprecation warning.

## Residual non-blocking items

- This machine has no running Docker engine, MySQL client, or MySQL service, so `sql/004_gateway_hardening.sql` was not executed against a real MySQL 5.7 instance. Run it twice in pre-production and verify report, approval, and check-in reads/writes before production rollout.
- Dense direct windows may require multiple list API probes while recursively narrowing the newest time window. Detail calls remain bounded to 1..100; use narrower request windows for high-volume tenants.
- The admin UI production build reports an existing chunk-size warning (about 1.14 MB minified) but completes successfully.

## Verdict

Approved for merge, subject to the documented pre-production MySQL 5.7 migration exercise.
