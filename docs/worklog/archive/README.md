# Completed worklogs

Moved into this archive on 2026-10-12:

- `2026.10.11-file-capability-per-modality/` — published modality-aware gates.
- `2026.10.11-media-tariff-forecast/` — published library tariff and drift check.
- `2026.10.11-model-doc-hygiene/` — corrected README/SPEC model-help drift.
- `2026.10.12-third-key-capability-probe/` — one-off, content-verified run on the
  new VPS key; **Lite/PDF worked on that key**, but a fleet-wide route change
  remains unapproved pending an independent-key check.

The scheduled CI canary is **not** archived: its GitHub key returned HTTP 429 and
its independent result is still pending in `docs/worklog/2026.10.11-capability-canary/`
and `docs/blockers/b01-capability-canary-quota.md`. The trigger-gated Interactions API
migration also remains in the active worklog.

Archival patch 0.3.5: 115 tests, lint and secrets guard passed; GitHub release
`38176826461` completed successfully (test and PyPI). No router behavior changed.
