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
  case matches the measured baseline; HTTP 200 is only evidence when the answer
  correctly reads the hidden PDF marker or the image's color (neither appears in the
  prompt). A generic/empty answer, allowed-case 500, or any 429/503/404/auth/network
  error is *inconclusive*, not evidence of changed capability. A red CI run and Gmail
  notice (when configured) are the fallback alert for drift/inconclusive runs.
- The model ids are explicit canary pins with optional `CAP_*_MODEL` overrides, not
  router defaults. Retirement of a canary id produces a red inconclusive check and
  must be reviewed, not silently skipped. No automatic edits to the capability gates.
- The fixture builder/mail function is shared with `scripts/tariff_check.py`; the two
  independent jobs test different facts (price via countTokens; usability via generate).

## Validation

Unit tests fake each provider outcome, short-circuit on a broken control, enforce the
four-call cap and cleanup. Full pytest + ruff + secrets guard before patch release
0.3.1. The first manual dispatch on 0.3.1 returned HTTP-200 text for both formerly
blocked cases and sent a **provisional drift email**. That first script only checked
for non-empty text and could not prove it was grounded in the file. Patch 0.3.2
requires the reply to reproduce the unique marker printed in the PDF or identify
the generated image's color, with no marker/color in the prompt; generic answers
are inconclusive. Re-dispatch after publishing to distinguish a genuine provider
capability change from a hallucinated answer. The next weekly run is automatic.
This is CI-only monitoring; no public library API or daemon deployment changes.
