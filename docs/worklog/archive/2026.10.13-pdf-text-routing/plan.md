# PDF routing aligned with text · 2026-10-13

Owner confirmed that two independent content-verified Lite/PDF keys suffice; do NOT
spend another request on a third key. Owner chose **the full text-style model/key
routing policy for PDF**, including key rotation with a local re-upload when needed.
This supersedes the 2026-10-11 historical no-overflow file gate.

Router changes:

- A PDF-containing request walks the same configured chain, opt-in Lite/Gemma
  overflow tiers, per-model/key budgets and minute-wait gates as a text request;
  non-PDF-only file jobs retain the previous chain-only policy. Optional per-media
  allowlists still restrict candidates, and a model/key that actually refuses that
  media with HTTP 400/500 is learned and skipped on later calls. Refusals are per
  **media × model × key**, not a fleet-wide ban.
- A Files API URI is project/key-local. Callers may provide matching local
  `file_paths` to `complete`: when a different key is chosen, the router streams
  the entire file/pack to that key once, records each upload in the shared ledger,
  preserves the media resolution and returns the actual serving upload(s) in
  `RouterResult.files_used`. Day-exhausted keys are not re-upload targets; partial
  group uploads are cleaned up best-effort. Without local paths, old safe pinning
  remains: never send a foreign URI under another key.
- The cache remains based on local SHA-256, not the provider URI. No file bytes or
  raw API keys are logged. Existing upload safety limits and provider measurement
  stay unchanged.

Tests cover Lite fallback after primary quota, rollover across two keys with
key-specific provider file URIs, whole-pack re-upload, per-key capability learning,
source count validation and historical no-source pinning. Gateway passes local
paths for single, grouped and split PDFs; its own worklog documents those changes.
No live capability re-test is part of this rollout; the owner's final verification
is deferred per standing instruction.
