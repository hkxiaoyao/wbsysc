# Console Task 5 operations report

## Delivered

- Reworked `tests/test_smoke_client.py` into import-safe local contract tests plus a default-skipped live runner.
- Required explicit mode/base URL/declared production host, two connection plus real/wrong service IDs and exact endpoints, four distinct Tokens, exact aliases, and controlled call aliases/JSON arguments.
- Required production targets to use a syntactically public HTTPS host and provide both the exact production opt-in and written-authorization acknowledgement; reachability is never authorization.
- Rejects template hosts/IDs/Tokens, endpoint mismatches, edge-whitespace credentials, and non-distinct IDs/Tokens before network access.
- Added encoded endpoint construction, exact alias-set checks, controlled calls, bad/cross-connection/cross-service/wrong-service rejection checks, and bounded output redaction. Only recognized MCP/HTTP 401/403 counts as rejection; transport, timeout, protocol, server, programming, and cancellation failures cannot pass.
- Production hosts reject `example.com`, `example.net`, `example.org`, all their subdomains, and other special-use/test forms; malformed ports and IPv6 are converted to a fixed configuration code.
- Resource IDs must be canonical persisted UUID v4/v5 values. Connection/service Tokens must match the platform's exact `mcp_`/`mcp_svc_` plus 43 URL-safe character generator shapes; separate category-correct bad Tokens are required and all five Tokens must differ.
- The fixed smoke trace now covers both bad-token categories, both service-to-connection checks, both connection-to-service checks, bidirectional connection isolation, and wrong-service rejection in a tested stable order.
- The CLI boundary contains cancellation, exception groups, and other base exceptions with fixed non-secret output and nonzero status; interrupt/exit semantics remain bounded and no traceback is emitted.
- Token suffixes must canonically decode to 32 bytes and pass a conservative obvious-placeholder preflight for low diversity, short periods, and arithmetic sequences. UUIDs receive similarly narrow payload checks. These gates do not claim to prove cryptographic entropy or issuance.
- The stable trace contains exactly 3 accepted checks and 11 rejected checks, including bad connection Token against connection 2 and bad service Token against the wrong service.
- `SystemExit` preserves only integer statuses `1..125`; zero, negative, oversized, boolean, and non-integer codes collapse to fixed status `1` without secret output.
- Periodic payload preflight now checks every proper period up to half the payload length, covering 10-byte motifs repeated with truncation and 16-byte motifs repeated twice without treating a one-off suffix match as periodic.
- UUID v4/v5 inputs must already be canonical lowercase; uppercase and mixed-case originals are rejected rather than normalized into acceptance.
- Updated operations, deployment, and README guidance for three-key handling, exact `004` through `008` migration order, staged enablement, false rollback, default-service backfill, tenant password/session effects, Token visibility, and exact-ID reversible cleanup.
- Corrected all HMAC rotation guidance for the current single-key implementation: inventory old token IDs, switch/restart during a maintenance window, then issue/distribute/verify under the new key and confirm old IDs are invalid.
- Explicitly enabled `MCP_SERVICE_ENABLED` in the service-API test fixture without changing the production default-off behavior.

Production smoke was not run and remains blocked pending the documented written authorization, production target, disposable resources, credentials, maintenance window, backup/restore point, named operator/reviewer, and cleanup approval.

## Validation

- `python -m pytest tests/test_smoke_client.py tests/test_mcp_service_api.py tests/test_server_deploy_script.py tests/test_main_service_flag.py -q`: PASS (`113 passed, 1 skipped`; live smoke skipped).
- `python -m ruff check tests/test_smoke_client.py tests/test_mcp_service_api.py`: PASS.
- `python -m compileall -q tests/test_smoke_client.py tests/test_mcp_service_api.py`: PASS.
- Safety-critical documentation `rg` checks: PASS.
- `python -m pytest -q`: PASS (`1245 passed, 1 skipped`).

## Safety notes

- No production request was sent.
- Output paths never print raw Tokens, cookies, Authorization headers, tool bodies, or exception messages.
- Cleanup documentation prohibits `LIKE`, prefixes, wildcard schemas, and unverified deletes; it requires recorded exact IDs, ownership/count assertions, child-first deletion in one transaction, separately proven exact schema deletion, settings restoration, and zero-row verification.
