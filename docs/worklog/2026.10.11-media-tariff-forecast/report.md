# Wave 1 library half closed: tariff forecast, drift detection, scheduled check

Released as **gemini-router 0.2.0** (feat → MINOR). Plan in this folder; the gateway
consumption half is `vernikr/gemini-mcp → docs/worklog/2026.10.11-tariff-drift-signal/`.

## What changed

- `src/gemini_router/tariffs.py` — the single copy of the media prices
  (`MEDIA_TARIFFS`, dated provenance per entry), `DEFAULT_LEVEL` (image = high,
  pdf = medium, both measured), `forecast_media_tokens`, `check_drift`
  (threshold max(5 %, 50 tokens)), `media_class`, `TariffNotMeasured`. audio/video carry
  the provider-published figure in the refusal message and stay unmeasured on purpose.
- `Ledger.record_drift` / `drift_recent`: kind='drift' joins `NON_SPENDING_KINDS`, the
  detail (class/level/predicted) rides as JSON in `quota_error`; the 24 h window is what
  the CLI reads.
- `doctor` and `quota` print one yellow line while forecast misses are recent
  (shared helper; the gateway CLI inherits both automatically).
- SPEC: new §2 subsection; the `media_resolution` paragraph no longer restates the
  numbers — it points at the table.
- Layer 3: `.github/workflows/tariff-drift.yml` + `scripts/tariff_check.py` — weekly and
  on release, three generated fixtures (an 8 px image, a 384 px image at the old tile
  boundary, one text PDF page) go through the real Files API and `countTokens` at the
  provider default level; a mismatch fails the run and mails the numbers over Gmail SMTP
  when those secrets exist. The model id is the script's own canary pin.

## Evidence

- Tests 89 → 96: the table is mirrored by the forecast for every class×level; the 5 % /
  50-token boundaries on both sides of zero; the drift row is spent by no gate (RPD and
  the minute windows read exactly one real call around it) and falls out of the 24 h
  window; unmeasured classes refuse with the published figure attached.
- Battery green (`pytest`, `ruff check .`, `scripts/check_secrets.py`), tag `v0.2.0`:
  release run green, PyPI served 0.2.0.
- The gateway side proved the whole chain against the published wheel: gemini-mcp 0.21.0
  ran its suite `UV_NO_SOURCES=1` against router 0.2.0 from PyPI and released green.
- Repo secret `GEMINI_API_KEY` set (a spare pool key from the VPS — the check draws no
  RPD). The first manual `workflow_dispatch` run went green against the live provider:
  8 px image 1089, 384 px image 1089 (flatness holds), one text PDF page 560 — the
  table and the provider agree on the day this shipped.

## Owner action owed

The Gmail secrets (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `MAIL_TO`) live only in the
`tldr-digest` repo's secret store and cannot be read back. Until they are copied into
this repo's settings the mail channel is skipped and the red run is the signal —
by design the fallback, not the plan.
