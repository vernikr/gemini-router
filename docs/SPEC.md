# gemini-router · SPEC

The router's own contract. Companion docs live in `vernikr/gemini-mcp/docs/02` + `docs/03`
(system-wide requirements, architecture, plan).

## 1. What it is

Quota-aware Gemini Flash router shared by Python, CLI and HTTP consumers:

- cascading model chain with runtime `listModels` validation (the configurable
  default lives in `src/gemini_router/config.py:DEFAULT_CHAIN`);
- per **key×model** budgets: RPD (America/Los_Angeles day + rolling 24 h), RPM and TPM
  (sliding 60 s) — enforced pre-flight, accounted post-fact from `usageMetadata`;
- key pool (`GEMINI_API_KEYS` / `GEMINI_API_KEY_1..10`); selection prefers the pair with
  the largest remaining budget;
- persistent multi-process ledger (SQLite WAL) — shared by connected consumers on
  the same machine (`gemini-mcp`, `tldr-digest`, CLI, HTTP clients);
- 429/5xx/4xx taxonomy (`RETRYABLE_STATUS`, `MODEL_SWITCH_STATUS`, `Retry-After`,
  backoff+jitter, provider deadline);
- opt-in overflow tiers (`gemini-3.5-flash-lite`, best `gemma-*`) — never automatic;
- request cache (hash of model+params+prompt, TTL) — hits spend 0 RPD;
- event stream (`on_event`) for progress reporting by frontends;
- `thinking_level` (low|medium|high) per request; 3.x rules applied (no
  temperature/top_p for Gemini 3.x; `thinking_level` not `thinking_budget`);
- **job/trace audit**: every call, hop, provider verdict and annotation belongs to a
  target activity; schema v4 `jobs` aggregates them (§13);
- CLI: `quota`, `models`, `calls`, `note`, `notes`, `jobs`, `job`, `job-start`,
  `job-finish`, `prune`, `doctor`, `proxy`; a blocking facade serves synchronous callers.

Zero MCP/web-framework deps: `httpx`, `pydantic`, `pyyaml`, `rich`, stdlib `sqlite3`.
Python ≥3.12. The npm package `gemini-router` is a zero-dependency HTTP client helper.

## 2. Public API

```python
from gemini_router import (Router, RouterRequest, RouterResult, RouterEvent,
                          QuotaExhaustedError, GeminiProxy)   # GeminiProxy: §11

router = Router.from_env()            # env + optional GEMINI_ROUTER_CONFIG yaml
res: RouterResult = await router.complete(
    RouterRequest(
        prompt=str, system=str | None = None,
        thinking_level="low|medium|high" = "medium",
        output_format="text|json" = "text",
        max_output_tokens=8192,
        kind="single|chunk|consolidate|count" = "single",
        request_id=None, source="cli", overflow=None,   # off|lite|lite+gemma;
                                      # None → cfg.overflow_default (FR-C8: that default is "off")
        initiator=None, comment="",   # stored + logged; no routing effect
        job=None, trace_id=None, step="",  # one target activity, its run, the stage (§13)
        wait=True,                    # wait out minute-scale gates (RPM/TPM/short cooldown,
                                      # ≤ wait.max_s), countdown via `waiting` events;
                                      # daily RPD resets are NEVER waited on
        model=None),                  # pin → skips chain fallback
    on_event=callable | None)         # RouterEvent(stage, pct, model, key_id, wait_s, detail, ts)
                                      # stages: routing|waiting|pacing|dispatch|retry|
                                      #         switch|overflow|ok|quota_exhausted

res.text, res.model, res.key_id, res.tokens_in, res.tokens_out
res.latency_s, res.cached, res.overflow_tier, res.events, res.quota_after
res.request_id, res.row_id, res.finish_reason, res.tokens_thought
await router.count_tokens(text, model=None) -> int

# One text segment; audio is returned as `TTSResult.audio` (provider WAV).
audio = await router.synthesize(text, voice=None, style=None, wait=None)

# UTF-8 TXT/Markdown file → quota-aware chunks → one MP3 path.
file = await router.synthesize_file(path, output_path=None, voice=None, style=None,
                                    bitrate_kbps=128, wait=None)
file.path, file.chunks, file.tokens_in, file.key_ids
await router.quota_table() -> list[QuotaRow]   # key_id, model, rpd_left, rpm_wait_s,
                                               # tpm_wait_s, cooldown_until, recommended
router.complete_sync(...)                      # blocking facade for synchronous callers
```

### File input (Files API, §8)

```python
up: UploadedFile = await router.upload_file(
    path, mime=None,             # default from the suffix
    model=None,                  # whose serving key to pick; None → chain head
    source="cli", display_name=None, key=None,
    overflow="lite+gemma",     # for PDF: allow a key with overflow quota if Flash is spent
    sha256=None, ready_timeout_s=30.0)
# UploadedFile: name, uri, mime, size, state, expires_at, sha256, key_id,
#               media_resolution ("low" | "medium" | "high" | None = provider default)
up  = up.with_resolution("high")            # pins the level for measurement AND request

n   = await router.count_file_tokens(model, up.uri, up.mime, key_id=up.key_id,
                                     media_resolution=up.media_resolution)   # free (FR-C7)
res = await router.complete(RouterRequest(prompt, est_tokens=…), files=[up],
                            file_paths=[path], …)  # permit key rotation
# res.files_used names the actual provider upload(s) that served the answer.
await router.delete_file(up.name)                                 # best-effort
```

`media_resolution` sets how hard the provider looks at the media, independently of the
model's thinking level, and it is the cost lever: the per-level prices, the provider
defaults and their dated provenance live in ONE table — `gemini_router.tariffs.MEDIA_TARIFFS`
(flat per level, independent of pixel count and byte size; the default differs per class).
Docs restate none of the numbers. Pass the SAME
level to the count and to the request: measuring at one level and sending at another makes
the TPM gate and the ledger disagree, which is why `with_resolution()` exists rather than a
loose keyword.

`complete(..., files=[…])` requires `est_tokens`: the TPM gate includes the measured
file tokens. A provider file belongs to its uploading key. Supplying matching local
`file_paths` lets the router stream one new upload per alternate key as needed, then
walk the same model/key tiers as a text request. Pass the same `overflow` policy to
`upload_file` so it can start even when only a Lite/Gemma key has quota; counting
file tokens stays free. Gateway single, group and split-PDF
paths all supply these sources. The result's `files_used` records the *serving* uploads.
If the caller does not supply local paths, the router safely retains historical
single-key pinning and never sends a foreign URI. The request carries `file_data`
parts ahead of text; the cache hashes local `sha256`, not a key-specific URI. A failed
multi-file re-upload discards that candidate's partial bundle.

### TTS (Gemini 3.8 Flash)

`Router.synthesize()` sends one text segment to the Interactions API and returns WAV
bytes. `Router.synthesize_file()` accepts UTF-8 `.txt` / `.md`, greedily packs the largest
paragraph-first chunks under the model's 8,192 input-token request limit, falls back to
sentence boundaries, and only splits within a sentence as a last resort. It checks the
full request count against the daily budget before generating, uses `countTokens` for
preflight when available, and records provider-measured usage into the same ledger. Per
project defaults are `tts_limits: {rpd: 10, rpm: 3, tpm: 10000}`; Google defines TPM as
input tokens, while generated audio tokens are an output/billing measure and do not enter
the input TPM gate. Output WAV chunks are appended to disk and asynchronously encoded to
MP3 using system `ffmpeg` (128 kbps mono by default). It supports one voice per file;
voice and style are optional. No audio is returned as a text completion or streamed.

Errors: `QuotaExhaustedError` carries the machine-readable payload
(per-model rpd_left/rpm_wait_s/tpm_wait_s/cooldown_until, reset_eta_pt, hint) — frontends
surface it as-is. `RouterError` base for everything else (network, http 4xx, parse).

### Tariff forecast and drift (wave 1)

`forecast_media_tokens(mime, *, pages=1, level=None)` is the planning number BEFORE any
provider round-trip — feasibility, packing math, cost previews. The prices come from the
single `MEDIA_TARIFFS` table (dated provenance in the module); `level` None/"" means the
provider default (`DEFAULT_LEVEL`, per class); audio/video raise `TariffNotMeasured` until
measured. The forecast never replaces measurement: `countTokens` is free and every
provider-path request keeps measuring.

`check_drift(predicted, measured)` → `{predicted, measured, pct}` when the miss exceeds
max(5 %, 50 tokens), else None. Hosts turn that into a `meta.cost.drift` field and a
`Ledger.record_drift(...)` row — kind='drift', counted by no gate, read back via
`drift_recent()`; `doctor` and `quota` print one line while misses are recent. A miss is
the signal to re-measure the table, never an error: the request ran on the measured number.

## 3. Ledger (SQLite, `GEMINI_ROUTER_STATE`, default `/opt/apps/shared/gemini-router/state.db`)

Tables `calls`, `cooldowns`, `pair_state`, `jobs`, `notes`, `cache`, `meta`,
`fs_affinity`, `drift` — the DDL in `ledger.py` is the canonical definition. Schema v4 is
additive, with in-place migration of older databases. Rules: WAL + busy_timeout 5 s; a TPM
pre-flight writes `status='planned'` and is upgraded after the call; RPD counts
`ok|quota|blocked` attempts (conservative), not 5xx overloads, client-validation 4xx or
network errors; PT-midnight + rolling-24h dual gate; retention via `gemini-router prune`.
`calls` carries job/trace/step/hop, request id, version, initiator, comment, model,
requested model, method/path and request/response byte counts (§13).

Non-spending provider calls are named once (`NON_SPENDING_KINDS`: `countTokens`, Files API
uploads) and excluded from every gate, from the pacing clock and from cooldowns. An upload
is recorded as `kind='upload'` with its byte count in `bytes` and a sentinel `model`, so
`router_logs` shows it while no per-model counter moves. The schema is additive: a new
nullable column is added in place when an older database is opened, because the production
file is shared with frontends that may run older code.

## 4. Config

Env: `GEMINI_API_KEYS`/`GEMINI_API_KEY_1..10`, `GEMINI_ROUTER_STATE`,
`GEMINI_ROUTER_CONFIG` (yaml). YAML keys: `chain`, `overflow_tiers`,
`overflow_default` (off|lite|lite+gemma — the overflow policy every entry point starts
from; a library request may override it per call, the pass-through proxy walks it as is),
`limits_per_key_model {rpd,rpm,tpm}`, `overflow_limits {rpd,rpm,tpm}`, `tts_limits {rpd,rpm,tpm}`
(default 10/3/10000 for `gemini-3.8-flash-tts`), `limits_overrides`,
`quota_day_tz`, `retry {max,backoff_base_s,jitter_max_s,provider_timeout_s,request_delay_s}`,
`wait {enabled_default,max_s,poll_s}` (FR-C13 semantics), `window_s`, `planned_ttl_s`,
`models_refresh_h`, `cache_ttl_h`, `state_path`, `base_url`, `temperature` (pre-3.x models
only), `max_output_tokens_default`, `chars_per_token` (unset = script-aware estimate),
`feedback {throttle_min_s,throttle_max_s,buffer_s,reset_buffer_s,overload_cooldown_s,
promote_hits,promote_window_s}`, `proxy {bind,port,token_env,max_upload_bytes,
request_timeout_s,body_inspect_bytes,audit,model_policy,file_search_models,
default_initiator,walk_wait,pool_circuit_breaker}` and `file_search_key` (§11, §12).
Defaults live in `config.py` and `config.example.yaml`. Secrets never logged
(`key_id = sha256(key)[:8]`).

## 5. Integration guide (external consumers)

```bash
uv add gemini-router      # PyPI
```

Service-style consumption (no Python): REST `POST /v1/complete`, `GET /v1/quota` on the
gemini-mcp daemon (localhost:8790, bearer) — same ledger, same rules.

## 6. Scope and boundaries

The router serves the Gemini API only: model resolution (`listModels`), quota accounting,
key rotation, File Search and Files API pass-through. Other providers stay outside this
package. Status taxonomy, `Retry-After` parsing, backoff/jitter and `extract_json` are
implemented here; a provider abstraction is not part of the public API. The blocking
facade and the native-Gemini proxy let synchronous and non-Python callers use the same
ledger without duplicating routing rules.

## 7. Versioning & CI

SemVer; tags `v*`; `gemini-mcp` pins a tag. CI: ruff + pytest (respx mocks, no network) on
push/PR. Conventional Commits. Release artifacts: `gemini-router` on PyPI (library +
CLI) and `gemini-router` on npm (zero-dependency HTTP helper in `npm/`). Both publish from
the tagged commit; the tag, `pyproject.toml` and `npm/package.json` carry the same
version.

## 8. File input (Files API)

Resumable upload: `POST {upload_base_url}/files` with the start headers → `PUT` to the
returned `x-goog-upload-url` with `upload, finalize`. The blob streams in 1 MiB chunks with
an explicit Content-Length (no base64, no second copy in RAM — the gateway's box has 1 GB).
The provider processes asynchronously: `state` is usually `ACTIVE` in the finalize answer,
otherwise the client polls with a bounded wait and raises `FileNotReady` on expiry of the
budget, `FileUploadFailed` when the provider marks it FAILED. `upload_base_url` is separate
from `base_url` because the upload endpoint lives on another host path.

Provider facts the capability leans on: 2 GB per file, 20 GB per project, 48-hour retention,
no download back, free of charge, and a file is visible only to the project of the key that
uploaded it. Document-level ceilings (PDF size and page count) are enforced by the provider
at analysis time and are the frontend's pre-flight concern, not this library's.

## 9. Semantics worth knowing

- **Thinking-token starvation:** in 3.x models `maxOutputTokens` counts thinking tokens,
  so a tiny cap yields empty visible text and no `candidatesTokenCount`. Therefore
  thought parts (`thought=true`) never join the visible text, `GenResult`/`RouterResult`
  carry `finish_reason` + `tokens_thought`, and `max_output_tokens_default` is a cap
  only (no spend implication).
- **Refused keys:** HTTP 401 marks the *key* dead, not the model — the key is dropped for
  the rest of the request and the pool continues; if every configured key is refused the
  error says so and names the remedy. A key the provider blocked as *leaked* arrives as
  `KeyLeakedError` (a `GeminiHTTPError` subclass, same failover semantics): retrying or
  switching transports cannot help a revoked credential, and the operator needs the cause
  in front of them, not a generic upload failure.
- **PDF-containing jobs use text's model/key tiers.** This includes opt-in
  Lite/Gemma overflow, quota and wait rules. Image/audio/video-only jobs use the
  chain-only policy. Known 400/500 file refusals are remembered per
  media × model × key and skipped on later requests. An optional
  `file_capable_models:` allowlist may narrow models by media class. One key's
  refusal never erases the evidence that another key read a PDF.

  Capability facts are per media × model × key: PDF via `file_data` follows the normal
  text routing policy (chain, Lite/Gemma overflow, per-key quotas and waits), while
  image/audio/video-only requests stay on the chain. An optional
  `file_capable_models:` allowlist narrows models by media class; learned 400/500
  refusals narrow incompatible candidates. `countTokens` measures price, not capability:
  a successful count does not prove `generateContent` accepts the part. The scheduled
  `capability-check.yml` runs a bounded `generateContent` canary and verifies content in
  the answer; `tariff-drift.yml` only counts. Drift raises an alert for review and does
  not change production gates automatically. A quota-drained chain reports the key that
  cannot serve file-capable models instead of asking for a re-upload.

- **Token estimate is script-aware.** `estimate_tokens` classifies characters and divides
  each class by its own ratio, because one divisor cannot serve mixed text: Latin prose
  runs ~5.4 chars/token, Cyrillic ~1.7, digits ~1.8. A single `len // 4` divisor
  under-estimates Russian, which lets the TPM gate pass and the provider answer 429.
  Inputs above `sample_chars` are estimated from a scaled head+tail sample,
  so cost stays flat at ~5 ms regardless of document size. Setting `chars_per_token`
  restores the single-divisor behaviour.
- **One `listModels` call, not one per key.** The model list is a property of the
  account tier, so every key on one plan gets the same answer; `availability()` probes
  the first key and applies the answer to the rest. `availability_per_key()` is the
  separate question "does *this* key work", used by `doctor`, `models` and the setup
  wizard. A key is never marked dead by a failed `listModels` — refusing a key is
  `generateContent`'s job, and one key's failure says nothing about the others.
- Ledger statuses: `planned|ok|quota|http4xx|http5xx|network|blocked|skipped`;
  RPD-conservative set = `ok,quota,blocked` plus in-flight `planned` rows (D3);
  `http5xx` is overload and does not spend RPD.
  An in-flight request counts as spent so processes sharing one state file cannot all
  clear the day's gate; rows older than `planned_ttl_s` are abandoned attempts and stop
  counting, so a caller that died mid-request does not eat a day's budget forever.
- **Deprecated model ids stay out of the chain.** `gemini-3.5-flash` redirects to
  `gemini-3.6-flash` and `gemini-3.7-flash` to `gemini-3.8-flash`; both aliases are
  still listed by `listModels`, so a chain built from that list would route to models
  nobody can select and charge the alias's own budget for traffic that lands elsewhere.
  The chain is the account's real options, newest first.
- CLI exit codes: `0` ok · `2` config/usage error · `doctor` names its failure so a
  script can act without parsing prose: `3` no API keys, `4` state file unusable,
  `5` no configured chain model reachable on any key. `gemini-mcp` passes these through
  and adds `6` for an unwritable log directory.

## 10. Keys and transports (`keyauth`)

Google issues two shapes of Gemini API key — classic `AIzaSy…` (Standard, Cloud Console)
and `AQ.…` (Auth, AI Studio) — and the accepted transport depends on the surface. The
library does not assume a shape: every provider
call goes through `keyauth`, which carries an ordered transport list and starts with the
one that last worked for that key.

| transport | wire form | when |
|---|---|---|
| `header` (default) | `x-goog-api-key: <key>` | the common path; adds no calls |
| `query` | `?key=<key>` | fallback for surfaces that refuse the header |
| `bearer` | `Authorization: Bearer <key>` | fallback for surfaces that expect a token |

Rules:
- **The provider's answer decides, not the key's shape.** A 401 always triggers the next
  transport; a 400/403 does so only when its body talks about keys/credentials, so a
  model-permission 403 still means "switch model" and costs nothing extra.
- **The winner is remembered per key** (process-local, keyed by fingerprint): after one
  rotation a key costs exactly one request per call.
- **A leaked-key block is never rotated** — see §9 — and `probe_key()` reports it plainly.
- `probe_key(key, base_url)` (sync, free: `listModels`) is the same logic for CLIs, so
  `keys add` / `keys list --check` show the transport the daemon will use. No second
  implementation to drift.
- Log hygiene: the default transport keeps keys out of URLs; while a `query`-transport key
  is in the pool, keep the frontend's log level at INFO (httpx prints URLs at DEBUG).

## Streaming completion

`Router.complete(..., stream=True)` uses `streamGenerateContent?alt=sse` with the same
request payload and quota path. `stream_start` resets provisional output before every
provider attempt; `text` delivers visible deltas in `detail.text` (thought parts excluded).
Deltas are callback-only, absent from the retained event trace. A cache hit emits `ok`
without deltas; always use the final result as the authoritative answer.

Only a nonempty stream terminated with `STOP` succeeds. Missing completion markers are
transport errors; blocked/truncated answers are candidate failures and cannot enter cache.
Stream retries reserve separate calls and recheck gates. Interrupted/cancelled streamed
attempts count conservatively as `blocked` in daily and sliding-window quotas, with an
interruption note. This existing status keeps older processes sharing the ledger accurate.
Cancellation propagates after resolving its reservation; non-streaming calls use the
same ledger rules.

## 11. Native Gemini API pass-through (`gemini-router proxy`)

A second surface next to the routing library: an HTTP service that speaks the **native
Gemini `v1beta` API**, so a client using the Google Gen AI SDK can point
`GOOGLE_GEMINI_BASE_URL` at this router and run on its key pool. It exists for clients
that build their own URLs and read provider-specific statuses, headers and error bodies
— `github.com/mikesmitty/file-search` and its `google.golang.org/genai` SDK — where a
`POST /v1/complete`-shaped endpoint cannot serve, because every re-serialisation on the
way through breaks the contract (`sizeBytes` is a string in that SDK's struct tags).

### 11.1 What it forwards

| family | requests |
|---|---|
| File Search stores | `POST/GET/DELETE /v1beta/fileSearchStores[/{id}]`, `…/{id}:importFile`, `…/{id}/documents[/{doc}]`, `…/{id}/operations/{op}` |
| Files API | `GET/DELETE /v1beta/files[/{id}]` |
| models | `POST /v1beta/models/{model}:generateContent`, `:countTokens`, `GET /v1beta/models[/{id}]` |
| resumable upload | `POST /upload/v1beta/files`, `POST /upload/v1beta/fileSearchStores/{id}:uploadToFileSearchStore` (start and chunks) |

Everything else is a JSON 404 in the provider's own error shape. Requests and responses
are forwarded byte-for-byte; only what the router is for is added (§11.3–11.6).

### 11.2 The base URL format is a hard constraint

`GOOGLE_GEMINI_BASE_URL` must be a **bare origin**: `scheme://host[:port]`, with no path
prefix and no `/v1beta`. The SDK prepends the API version itself. Measured against the
real SDK: with a base URL of `http://host/proxy`, ordinary calls go to
`/proxy/v1beta/…` but the **upload continuation goes to `/upload/v1beta/…`** — the SDK
copies only the base URL's scheme and host onto the upload URL Google returned, and
drops the prefix. A path prefix therefore breaks uploads and nothing else, which is the
worst kind of failure.

### 11.3 Client authentication

The client's key is the router's token: `x-goog-api-key: <token>` (what the Gen AI SDK
sends from `GEMINI_API_KEY`/`GOOGLE_API_KEY`) or `Authorization: Bearer <token>`.
Compared in constant time; refused with a Google-shaped 401 carrying
`WWW-Authenticate`. The client credential is consumed here and never forwarded: upstream
only ever sees a pool key, and only through the `header` transport of `keyauth`, so no
key ever appears in a URL. `/healthz` is open; every `/.well-known/*` rung answers JSON,
never `text/plain`.

### 11.4 Resource affinity — File Search is per Google project

A store, a Files API object, a document and an operation under it are visible only to
the project of the key that created them. The router's ordinary rotation (largest
remaining budget) is right for `generateContent`, where a file can be re-uploaded under
another key, and wrong here: the second request to a store would answer 404.

The pass-through therefore adds one mapping on top of the existing pool — never a second
key system:

* `fs_affinity(resource → key_id)` in the shared ledger, learned from a successful
  create and consulted on every later request. It lives in the database because every
  process serving File Search on the machine must agree on it and a restart must not
  orphan every store.
* a persisted **default project** (`meta.fs_pin`, or `file_search_key:` in config), used
  for creates and for unscoped calls such as `list_stores` — two different keys would
  otherwise show two different worlds of stores.
* a **self-healing probe**: a read or delete of a resource with no known owner tries the
  other keys once, in budget order, and remembers the winner.

For `generateContent` the store is named in the **body**
(`tools[].fileSearch.fileSearchStoreNames[]`), so small bodies are buffered to read it;
large ones (upload chunks) are streamed and never held twice in memory.

### 11.5 Resumable upload

Google answers the handshake with an absolute `X-Goog-Upload-Url` on its own host. The
proxy rewrites it to itself and stamps it with two parameters, which the SDK copies
verbatim on every chunk:

```
router_k=<key_id>                                    # the project that owns the session
router_s=HMAC(token, "<upload_id>|<key_id>|upload")  # the authentication
```

Stateless and restart-safe: a continuation cannot bypass authentication, cannot be
forged, and cannot land in a different project mid-upload. The two parameters are
stripped before the request goes upstream. Chunks are POSTed in 8 MiB pieces with
`X-Goog-Upload-Offset`; `X-Goog-Upload-Status` and `X-Goog-Upload-Url` are forwarded
untouched.

### 11.6 Model policy and the single selection procedure

Both the model **and the key** of a model-path request come from `routing.pick_pair` —
the same walk `Router.complete` makes (tiers from `tier_plan` including the
`overflow_default` tiers, keys ranked by remaining day budget, ledger gates). The owner's
rule this encodes: parameter logic is identical regardless of the entry door and is not
duplicated — the proxy only maps its policy onto the walk's inputs:

- **resource affinity pins the key** (§11.4): for a store-bound call only the model
  walks; an unbound `generateContent`/`countTokens` walks both, so the pool rotates by
  budget instead of riding the File Search pin.
- `proxy.model_policy`: `strict` (default) always walks; `auto` keeps the caller's model
  when a key can serve it and falls back to the walk otherwise; `passthrough` never
  rewrites and keeps the caller's landing key.
- when every pair is day-drained the head candidate is forwarded anyway — the provider
  stays the source of truth and its own 429 body reaches the client untranslated.
- **a live 429/503 updates provider truth and continues the same walk.** A 429/503
  benches its exact `(key, model)` pair in `pair_state` (§12) and the walk picks the next
  unvisited candidate — next key, then next tier — up to `_walk_bound()` further
  candidates. Minute-scale gates are waited out; when every pair is benched the proxy
  returns a local `429 + Retry-After`. Only an unbound, buffered `generateContent` walks
  on (a pinned call has no alternative; a streamed body cannot be replayed). Each
  upstream attempt is planned and resolved in the ledger.

`strict` is the default because of what the client actually sends: `file-search` asks for
its own hardcoded `gemini-2.5-flash`, which the router's keys cannot serve, so honouring
the request would fail every query. The chain exists precisely to answer "which model can
take this request right now", and the caller's model id is a default, not a requirement.

Every response carries `x-router-model`, `x-router-key-id` (model paths),
`x-router-version`, `x-router-job`, `x-router-trace`, `x-router-request-id` and
`x-router-traceparent`, so a caller's own run log can record the serving pair and its
correlation identity without matching the proxy's audit stream by time (§13).

### 11.7 Idempotency and retries

| request | retries |
|---|---|
| `GET`/`DELETE`/`HEAD` | yes — same key first, then the other projects once; 429 sets a cooldown and moves on |
| upload chunks | yes, on transport errors, always under the session's own key |
| `POST` create / `importFile` / upload start | **no** repeat once a response may have taken effect — a second create makes a second store |
| `generateContent` | retried only when the request is not bound to a store |

A consumed request stream cannot be replayed, so a streamed body always gets one attempt.

### 11.8 Accounting

`generateContent` is planned and resolved like any other router call and draws on the
model's RPD/RPM/TPM. Every other File Search call is recorded as `kind='filesearch'`,
which is in `NON_SPENDING_KINDS`: visible in `calls`/`router_logs`, excluded from every
gate — a store listing must not fail because the text budget is spent.

### 11.9 Operating it

```bash
GEMINI_ROUTER_PROXY_TOKEN=$(openssl rand -hex 32) gemini-router proxy     # 127.0.0.1:8787
```

Loopback only: reach it through an SSH tunnel. The service is stateless apart from the
shared ledger, so several processes (and the CLI) may run against one `state.db`.
Sizing: stdlib `http.server`, no new dependencies, `MemoryMax=128M` is ample.

## 12. Provider truth and request identity

### 12.1 The principle

`calls` is a *forecast* of quota usage. The provider's answer is the *measurement*. On
any 429/503 the measurement outranks the forecast for the exact `(key_id, model)` pair,
for the current request and for every future request until the provider's own reset time.
Every key belongs to a separate Google account, so isolation is per key × model.

### 12.2 `feedback.py` — one parser, one writer

`parse_provider_signal(status, headers, body, cfg, now)` is a pure function:

| Input | Result |
|---|---|
| 429 + `quotaId: ...PerDay...` | `kind="day"`, `until_ts` = next PT midnight + 30 s. `Retry-After` is recorded and never caps the reset. |
| 429 + `quotaId: ...PerMinute...` | `kind="minute"`, bench = `Retry-After + buffer` (floor 1 s). |
| 429, no quota details | `kind="minute"`, `unconfirmed=True`, bench = `max(Retry-After + buffer, throttle_min_s=60)`, capped at `throttle_max_s=900`. |
| 503/502 | `kind="overload"`, bench = `overload_cooldown_s=60`; no RPD spend. |
| anything else | `kind="none"`. |

`Retry-After` accepts delta-seconds and HTTP-dates; `retryDelay` / "Please retry in Ns"
and `quotaId`/`quotaMetric`/`quotaValue` are extracted from arbitrary JSON shapes.

`QuotaTruth.apply()` upserts `pair_state` (including `job`, `trace_id`, `initiator` and
`version`) and mirrors the decision into `cooldowns`, which older readers use. Repeated
unexplained 429s (`promote_hits=3` inside `promote_window_s=300`) escalate to a day
block. A `2xx` calls `clear_pair_state()`; success proves the pair is available.
`RPD_STATUSES = ("ok", "quota", "blocked")`.

### 12.3 Selection, waiting and the circuit breaker

- `gates_from(..., provider_state=...)` sets `rpd_left=0` for a live `day` verdict and a
  waitable `cooldown`/`wait_s` for `minute`/`overload`. Provider truth is read from the
  same `gate_snapshot` query as the counters.
- `pick_pair(..., exclude=visited)` skips pairs already tried in this request. When no
  clean candidate remains and provider verdicts exist, it returns `reason="throttled"`
  with the shortest reset.
- `FileSearchRouting.choose()` returns a `Selection` (`state` = `ok` / `wait` /
  `throttled` / `exhausted`).
- The proxy waits out `state=="wait"` when `wait_s <= wait.max_s` (`proxy.walk_wait`)
  and answers locally with `429 + Retry-After` on `state=="throttled"` when
  `proxy.pool_circuit_breaker` is on. A purely counter-based `exhausted` state still
  asks the provider once — it is the authority.

### 12.4 Identity and annotations

`context.RequestContext` is threaded through the proxy and the library. Every `calls` row
carries `version`, `initiator`, `comment`, `request_id`, `job`, `trace_id`, `step`,
`quota_id`, `signal_kind` and the hop/byte/method/path fields. `notes` stores standalone
annotations and job lifecycle events. `note` (CLI + `POST /v1/note`), `notes`
(`GET /v1/notes`) and the job endpoints (§13) make no provider call and have no quota
effect. A `comment` is arbitrary text: stored and logged, never used for routing and
never sent upstream.

`CLIENT_CONTEXT_HEADERS` lists every router-only header; they are stripped before an
upstream request. Audit output is one-line logfmt:
`event=request v=<version> rid=... job=... trace=... step=... initiator=... comment=...
key=... model=... status=...`. `event=startup` carries the version, config and state
path; `event=provider_feedback` the parsed signal; `event=note` an annotation;
`event=job` a lifecycle change; `event=pool_throttled` a locally answered 429.
`/healthz` and all CLI tables show the running version.

## 13. Job / trace correlation

### 13.1 Model

A **job** identifies one target activity (`digest-2026-10-28`, `mcp-session-…`). A
**trace** identifies one execution of that job; retries use a new trace and stay in the
job. `step` names a stage inside the execution, `request_id`/`span_id` identify one
logical request, and `hop` counts upstream attempts inside its selection walk. All
records of an activity — calls, hops, provider verdicts, cooldowns, notes and lifecycle
events — share `job`. A client that sends no job gets `<initiator>@<UTC date>`; a client
that sends no trace uses the job as its trace.

### 13.2 Wire format

Request headers, consumed locally and never forwarded upstream:

```
X-Router-Initiator: tldr-digest          (aliases: X-Router-Source)
X-Router-Job: digest-2026-10-28          (aliases: X-Router-Job-Id)
X-Router-Trace: 7f3a9c2e                 (aliases: X-Router-Trace-Id)
X-Router-Step: prefilter                 (aliases: X-Router-Stage)
X-Router-Request-Id: digest-20261028-001 (aliases: X-Router-Span)
X-Router-Comment: free text              (aliases: X-Router-Note)
X-Router-Parent-Span: <16 hex>           (link a child request to its parent)
traceparent: 00-<32 hex>-<16 hex>-01     (W3C Trace Context)
tracestate / baggage: forwarded to logs only
```

Values are ASCII; percent-encoded text is decoded for the audit trail, identifiers are
capped at 128 characters and comments at 500. `RequestContext.from_headers()` parses
`traceparent` into `trace_id`/`parent_span`; `make_traceparent()` emits the response
header. Python callers set `RouterRequest.job`, `.trace_id`, `.step`, `.initiator`,
`.comment`.

Response headers on every forwarded path: `x-router-version`, `x-router-job`, `x-router-trace`,
`x-router-request-id`, `x-router-traceparent`; model paths also carry `x-router-model`
and `x-router-key-id`.

### 13.3 Storage (schema v4)

`jobs(job PK, first_ts, last_ts, finished_ts, status, initiator, trace_id, version,
comment, calls, ok, refusals, quota_429, models JSON, keys JSON)` is created and updated
by `_job_touch()` on every call, by `job_start()` / `job_finish()` and by `_job_count()`
on resolution. `calls` gains `job`, `trace_id`, `step`, `hop`, `requested_model`,
`request_bytes`, `response_bytes`, `method`, `path`, `parent_span`; `notes` gains `job`,
`trace_id`, `step`, `event` (`note` | `job_start` | `job_finish`); `pair_state` gains `job`,
`trace_id`. Indexes cover `(job, ts)`, `(trace_id, ts)`, `(step, ts)`, `(initiator, ts)`,
`request_id` and `notes(job, ts)`. All columns are nullable and added in place; existing v1–v3
databases migrate on open. Read APIs: `calls_tail(...)`, `notes_tail(...)`,
`job_summary()`, `jobs_tail()`, `job_calls()`.

### 13.4 HTTP endpoints

| endpoint | method | purpose |
|---|---|---|
| `/v1/job` | POST | `{"action":"start|finish","job":…,"status":"done|failed","trace":…,"comment":…}` — lifecycle event + counters |
| `/v1/jobs` | GET | `?n=` — recent activities with status and counters |
| `/v1/job` | GET | `?id=…&n=` — summary, lifecycle events and every call of one activity |
| `/v1/note` | POST | `{"comment":…,"job":…,"initiator":…}` — annotation, no provider call |
| `/v1/notes` | GET | `?n=&job=` — annotations and job events |
| `/v1/calls` | GET | `?n=&job=&trace=&step=&initiator=&model=&status=&source=&request_id=` — audit rows |

All require the proxy token. `/healthz` is open and reports the version.

### 13.5 CLI and npm

```bash
gemini-router job-start digest-2026-10-28 --initiator tldr-digest --trace 7f3a9c2e
gemini-router jobs -n 20
gemini-router job digest-2026-10-28 --json
gemini-router job-finish digest-2026-10-28 --status done
gemini-router calls --job digest-2026-10-28 --step prefilter --wide
gemini-router notes --job digest-2026-10-28
```

The npm package `gemini-router` exports `createContext`, `headers`, `note`, `jobStart`,
`jobFinish`, `jobs`, `jobGet`, `calls` and `routerFetch`. It has no runtime dependencies
and only calls the HTTP endpoints above; see `npm/README.md`.
