# Changelog

## 0.14.2 — live TTS progress and ETA

### Added
- TTS events include monotonic pseudo-progress inside the active chunk, weighted by preflight token counts, with an ETA adapted from completed chunk throughput.
- MP3 encoding reports ffmpeg output-time progress and a remaining-time estimate.
- Router and gateway CLIs render a compact one-line indicator; JSON output remains machine-readable.


## 0.14.1 — REST TTS response parsing

### Fixed
- Read audio from `steps[].content[]` in the raw Interactions API REST response as well as SDK-shaped responses.

## 0.14.0 — CLI updates and deterministic TTS progress

### Added
- The `gemini-router` CLI checks PyPI at launch and runs the newest published release
  when available. `--no-update` and `GEMINI_ROUTER_AUTO_UPDATE=0` keep the installed
  version; offline checks fall back to the installed CLI. The long-running `proxy`
  service remains deployment-managed.
- TTS file progress events now include deterministic overall percentage, phase, completed
  chunks, and total chunks. The Gemini gateway CLI renders these events as a terminal
  progress bar; waits and retries do not invent time-based progress.

### Changed
- Provider events include per-file TTS progress and chunk context for terminal frontends.
- Evergreen documentation and source comments describe the current interfaces and behavior.

## 0.13.0 — single-speaker Gemini 3.8 Flash TTS

### Added
- `Router.synthesize()` for one audio segment and `Router.synthesize_file()` for UTF-8
  TXT/Markdown → paragraph/sentence-aware chunks → one MP3 file.
- Dedicated TTS request path through the Gemini Interactions API; voice and style are
  optional; chunks are appended as WAV on disk and encoded to MP3 by asynchronous ffmpeg.
- Per-project TTS quota defaults (3 RPM, 10,000 input TPM, 10 RPD) in the shared SQLite
  ledger, preflight token measurement, quota-aware project rotation/waiting, provider
  feedback, and per-project `listModels` validation.
- CLI `gemini-router tts`; TTS quota rows in `gemini-router quota`.

### Docs
- README and SPEC describe the input-only TPM limit, 8,192-token request ceiling,
  supported input formats, MP3/ffmpeg requirement and API examples.

## 0.12.0 — job/trace correlation, npm client

### Added
- `context.RequestContext` carries `job`, `trace_id`, `step`, `span_id`, `parent_span`
  and W3C `traceparent` support. Client headers: `X-Router-Job`, `X-Router-Trace`,
  `X-Router-Step`, `X-Router-Initiator`, `X-Router-Request-Id`, `X-Router-Comment`,
  `traceparent` (aliases listed in `CLIENT_CONTEXT_HEADERS`).
- Schema v4: `jobs` table with per-activity counters/status; `job`, `trace_id`, `step`,
  `hop`, `requested_model`, `request_bytes`, `response_bytes`, `method`, `path` and
  `parent_span` on `calls`; job/trace/step on `notes` and `pair_state`. All columns are
  additive, with in-place migration of existing databases.
- `POST /v1/job` (`action=start|finish`, `status=done|failed`), `GET /v1/jobs?n=`,
  `GET /v1/job?id=…&n=…`; job-aware `POST /v1/note`, `GET /v1/notes`, `GET /v1/calls`.
  Response headers include `x-router-version`, `x-router-job`, `x-router-trace`,
  `x-router-request-id`, `x-router-traceparent`.
- CLI: `jobs`, `job`, `job-start`, `job-finish`; `note --job --trace`, `notes --job`,
  `calls --job --trace --step --wide`.
- Library: `RouterRequest.job`, `.trace_id`, `.step`; job/trace fields in provider
  feedback logs and `pair_state`.
- npm package `gemini-router`: zero-dependency ESM/CJS helpers — `createContext`,
  `headers`, `note`, `jobStart`, `jobFinish`, `jobs`, `jobGet`, `calls`, `routerFetch`.
  Published from [`npm/`](npm/).
- `event=job` structured log lines, `requested_model`, request/response byte counts and
  hop counters in the audit trail.

### Docs
- `README.md`, `docs/SPEC.md`, `config.example.yaml` and module comments describe the
  current behaviour.

## 0.11.0 — provider truth, bounded walks, request identity

### Added
- `feedback.py`: `parse_provider_signal`, `ProviderSignal`, `QuotaTruth`. A provider
  answer becomes durable `pair_state`; `cooldowns` mirrors it for older readers.
- `context.py`: `RequestContext`, logfmt `audit_line`, `router_version`. Audit rows and
  logs carry version, initiator and a behaviour-free comment.
- `X-Router-Initiator` / `X-Router-Comment` / `X-Router-Request-Id` headers (stripped
  before upstream), `RouterRequest.initiator` / `.comment`, `calls --wide`,
  `calls --initiator`.
- `gemini-router note` / `notes` CLI and `POST /v1/note`, `GET /v1/notes`: annotations
  with no provider call and no quota effect.
- `event=startup/provider_feedback/note/pool_throttled/request` structured logs;
  `/healthz` reports the version.
- Config: `feedback.*`, `proxy.default_initiator`, `proxy.walk_wait`,
  `proxy.pool_circuit_breaker`.

### Behaviour
- A confirmed daily 429 blocks the `(key, model)` pair until the next Pacific-time
  reset. `Retry-After` (delta-seconds and HTTP-date), `retryDelay` and quota metadata
  are parsed; `quota_id` lands in the audit row.
- A pass-through walk skips pairs already visited in the same request, waits out
  minute-scale gates and answers locally with `429 + Retry-After` when every pair is
  benched. `2xx` clears a provider block.
- `503/502` overloads are not RPD spend. The library path applies the same provider
  truth on 429 while waiting out confirmed short minute quotas.
- Schema v3 (`pair_state`, identity columns) is additive; v1/v2 databases migrate in
  place and older readers keep reading `calls` and `cooldowns`.
