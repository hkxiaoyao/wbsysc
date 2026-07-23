# Final service-token frontend implementation

## Scope delivered

- Added one shared tenant/admin optional `datetime-local` expiry control.
- Normalized valid local instants to whole-second canonical UTC `Z` values and used the stable `expires_at: null` contract when omitted.
- Added strict, fail-closed timestamp parsing for reveal eligibility and canonical metadata display.
- Split reveal and revoke eligibility so expired tokens cannot reveal/copy but remain revocable.
- Rendered distinct active, expired, and revoked tags plus canonical created, expiry, and last-used metadata.
- Preserved request-generation and abort guards; expiry, label, and raw-token state are cleared on close, service switch, successful issue, and unmount lifecycle.
- Kept raw secret handling isolated to explicit issue/reveal responses. Prefix and metadata are never treated as copyable token material.
- Confirmed generic backend audit failures render only the fixed local fallback, without response secrets or exception text.

## Validation

- `node --test src/pages/servicesView.test.js`: PASS, 25/25 tests.
- `node --test`: PASS, 114/114 tests.
- `npm run build`: PASS, 4105 modules transformed.
- `git diff --check` on owned source/test files: PASS.

## Residual warning

Vite reports its existing large-chunk advisory for the production JavaScript bundle (about 1.49 MB minified, 474 KB gzip). This is a non-blocking bundle-splitting warning and is outside the service-token lifecycle scope.
