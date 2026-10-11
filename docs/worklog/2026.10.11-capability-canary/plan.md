# Files API capability canary · 2026-10-11

The gateway's remaining-work plan §E called for a scheduled test of the empirical
model×media support matrix: countTokens accepted PDFs that generateContent rejected.
This check belongs in the router repo (provider client, capability rules). The gateway
worklog points here rather than copying the implementation.

## Contract and cost boundary

- GitHub Actions runs weekly/on manual dispatch using the existing repo canary
  `GEMINI_API_KEY`. Two small generated fixtures (PDF and PNG) are uploaded on that
  key, then one `generateContent` per case, pinned to a model with no fallback/retry.
  Four cases maximum on a green run: positive controls (Flash/PDF, Lite/PNG), then
  previously refused cases (Lite/PDF, Gemma/PNG). A failed positive control aborts
  immediately. Uploads/deletions do not spend generation RPD; the four probes do.
  Prefer a dedicated monitoring account: if this key is shared with production, the
  weekly run consumes up to four RPD there. The workflow never contacts the VPS or
  its ledger and never prints credentials or response text.
- `countTokens` is **not** the capability verdict. HTTP 400/500 on an expected-blocked
  case matches the measured baseline, success changes it; 400 on an expected-allowed
  case is drift. An allowed-case 500, any 429/503/404/auth/network error, or an empty
  answer is *inconclusive*, not evidence of changed capability. A red CI run and
  Gmail notice (when configured) are the fallback alert for drift/inconclusive runs.
- The model ids are explicit canary pins with optional `CAP_*_MODEL` overrides, not
  router defaults. Retirement of a canary id produces a red inconclusive check and
  must be reviewed, not silently skipped. No automatic edits to the capability gates.
- The fixture builder/mail function is shared with `scripts/tariff_check.py`; the two
  independent jobs test different facts (price via countTokens; usability via generate).

## Validation

Unit tests fake each provider outcome, short-circuit on a broken control, enforce the
four-call cap and cleanup. Full pytest + ruff + secrets guard before patch release
0.3.1. After publication: manually dispatch the workflow once, verify its green run
or log an actionable inconclusive result without guessing. The next weekly run is
automatic. This is CI-only monitoring; no public library API or daemon deployment
changes.
