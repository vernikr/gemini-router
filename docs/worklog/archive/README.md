# Completed worklogs

Moved into this archive on 2026-10-12:

- `2026.10.11-file-capability-per-modality/` — published modality-aware gates.
- `2026.10.11-media-tariff-forecast/` — published library tariff and drift check.
- `2026.10.11-model-doc-hygiene/` — corrected README/SPEC model-help drift.
- `2026.10.12-third-key-capability-probe/` — one-off, content-verified run on the
  new VPS key; **Lite/PDF worked on that key**. The later independent CI result
  agrees, but a fleet-wide route change still requires an explicit decision.

- `2026.10.11-capability-canary/` — scheduled CI check shipped and its independent
  key verified Lite/PDF on 2026-10-12. The quota blocker is resolved in
  `docs/blockers/archive/b01-capability-canary-quota.md`. Routing policy is NOT
  automatically changed by the monitoring result.
- `2026.10.13-capability-alert-copy/` — human-readable future email subjects,
  explicit next action and run link; old messages are unchanged.
- `2026.10.13-pdf-text-routing/` — PDF shares text's model/key/overflow tiers;
  key switches re-upload from local sources and report the serving file.

The trigger-gated Interactions API migration remains in the active worklog.

Archival patch 0.3.5: 115 tests, lint and secrets guard passed; GitHub release
`38176826461` completed successfully (test and PyPI). No router behavior changed.
