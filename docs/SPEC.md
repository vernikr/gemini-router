# gemini-router · SPEC

The router's own contract. Companion docs live in `vernikr/gemini-mcp/docs/02` + `docs/03`
(system-wide requirements, architecture, plan).

## 1. What it is

Quota-aware Gemini Flash router extracted from `tldr-digest/src/llm.py`, generalized:

- cascading model chain with runtime `listModels` validation (the configurable
  default lives in `src/gemini_router/config.py:DEFAULT_CHAIN`);
- per **key×model** budgets: RPD (America/Los_Angeles day + rolling 24 h), RPM and TPM
  (sliding 60 s) — enforced pre-flight, accounted post-fact from `usageMetadata`;
- key pool (`GEMINI_API_KEYS` / `GEMINI_API_KEY_1..10`); selection prefers the pair with
  the largest remaining budget;
- persistent multi-process ledger (SQLite WAL) — shared by connected consumers on
  the same machine (MCP daemon, stdio sessions, CLI); other programs can join it,
  but the `tldr-digest` migration is not complete;
- 429/5xx/4xx taxonomy inherited from tldr-digest (`RETRYABLE_STATUS`,
  `MODEL_SWITCH_STATUS`, `Retry-After`, backoff+jitter, provider deadline);
- opt-in overflow tiers (`gemini-3.5-flash-lite`, best `gemma-*`) — never automatic;
- request cache (hash of model+params+prompt, TTL) — hits spend 0 RPD;
- event stream (`on_event`) for progress reporting by frontends;
- `thinking_level` (low|medium|high) per request; 3.x rules applied (no
  temperature/top_p for Gemini 3.x; `thinking_level` not `thinking_budget`);
- CLI: `quota`, `models`, `calls`, `prune`, `doctor`; sync facade for legacy callers.

Zero MCP/web-framework deps: `httpx`, `pydantic`, `rich`, stdlib `sqlite3`. Python ≥3.12.

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
        wait=True,                    # wait out minute-scale gates (RPM/TPM/short cooldown,
                                      # ≤ wait.max_s), countdown via `waiting` events;
                                      # daily RPD resets are NEVER waited on
        model=None),                  # pin → skips chain fallback
    on_event=callable | None)         # RouterEvent(stage, pct, model, key_id, wait_s, detail, ts)
                                      # stages: routing|waiting|pacing|dispatch|retry|
                                      #         switch|overflow|ok|quota_exhausted

res.text, res.model, res.key_id, res.tokens_in, res.tokens_out
res.latency_s, res.cached, res.overflow_tier, res.events, res.quota_after
await router.count_tokens(text, model=None) -> int
await router.quota_table() -> list[QuotaRow]   # key_id, model, rpd_left, rpm_wait_s,
                                               # tpm_wait_s, cooldown_until, recommended
router.complete_sync(...)                      # blocking facade (tldr-digest migration)
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

Tables `calls`, `cooldowns`, `cache`, `meta` — full DDL and accounting rules:
gemini-mcp `docs/03-architecture.md` §3 (single canonical definition).
Rules summary: WAL + busy_timeout 5 s; TPM pre-flight writes a `status='planned'` row,
upgraded after the call; RPD counts `ok|quota|http5xx` attempts (conservative),
not client-validation 4xx/network; PT-midnight + rolling-24h dual gate; retention prune
via `gemini-router prune` and by frontends' daily task.

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
`limits_per_key_model {rpd,rpm,tpm}`, `overflow_limits {rpd,rpm,tpm}`, `limits_overrides`,
`quota_day_tz`, `retry {max,backoff_base_s,jitter_max_s,provider_timeout_s,request_delay_s}`,
`wait {enabled_default,max_s,poll_s}` (FR-C13 semantics), `window_s`, `planned_ttl_s`,
`models_refresh_h`, `cache_ttl_h`, `state_path`, `base_url`, `temperature` (pre-3.x models
only), `max_output_tokens_default`, `chars_per_token` (unset = script-aware estimate),
`proxy {bind,port,token_env,max_upload_bytes,request_timeout_s,body_inspect_bytes,audit,
model_policy,file_search_models}` and `file_search_key` (§11). Defaults per gemini-mcp `docs/03-architecture.md` §8. Secrets never logged
(`key_id = sha256(key)[:8]`).

## 5. Integration guide (external consumers)

```bash
uv add gemini-router      # PyPI
```

Service-style consumption (no Python): REST `POST /v1/complete`, `GET /v1/quota` on the
gemini-mcp daemon (localhost:8790, bearer) — same ledger, same rules.

## 6. Extraction mapping from tldr-digest/src/llm.py

| tldr-digest | router | Change |
|---|---|---|
| `DEFAULT_MODEL_PRIORITY`/`llm_model_priority` | `config.chain` | Gemini-only in v1; groq/openrouter stay in tldr until a provider abstraction is justified |
| `_resolve_models`/`_list_models`/`_best_available` | `routing.resolve_chain` | + refresh interval, + preview-slot heuristics |
| `RETRYABLE_STATUS`/`MODEL_SWITCH_STATUS`/`_parse_retry_after`/backoff | `routing` | ported as-is |
| `Provider.can_call/register_call` (in-memory) | `ledger` queries | persistent, per key×model, + TPM |
| `_call_gemini` | `gemini.generate` | async httpx, thinking_level, usageMetadata accounting |
| `extract_json` | `gemini.extract_json` | ported as-is |
| `complete_json` | `complete(output_format="json")` | superset |

## 7. Versioning & CI

SemVer; tags `v*`; gemini-mcp pins a tag. CI: ruff + pytest (respx mocks, no network) on
push/PR. Conventional Commits.

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
  Lite/Gemma overflow, quota and wait rules. Image/audio/video-only jobs retain the
  historical chain-only policy. Known 400/500 file refusals are remembered per
  media × model × key and skipped on later requests. An optional
  `file_capable_models:` allowlist may narrow models by media class. One key's
  refusal never erases the evidence that another key read a PDF.

  **Two sources, deliberately kept apart** (this repo's rule, owner, 2026-10-11):

  *Practice* — measured 2026-10-11 on two keys, matrix in
  gemini-mcp `docs/worklog/archive/2026.10.11-remaining-work/plan.md` §E: PDF via `file_data` →
  HTTP 500 on `gemini-3.5-flash-lite`, HTTP 400 on `gemini-3-flash-preview`, OK on
  `gemini-3.6-flash`; PNG via `file_data` → OK on lite and preview, 400 on
  `gemma-4-31b-it`, whose inline-image path works. Historical gate excluded
  all file overflow, but the owner later chose **PDF routing like text**: same
  chain, Lite/Gemma overflow preference, per-key quotas and wait rules for PDFs.
  Other file-only requests retain their former tier policy. Explicit allowlists
  and per-key learned 400/500 refusals still narrow incompatible candidates.

  *Official* — `ai.google.dev` model pages (fetched 2026-10-11) list PDF as a supported
  input for `gemini-3.5-flash-lite`; the Gemma 4 card lists text and images. The docs and
  practice disagreed for lite + PDF on 2026-10-11, and both dated statements stay
  here on purpose. On 2026-10-12 **two independent keys** (new VPS and existing CI)
  each read the hidden PDF code on Lite, while Gemma/PNG returned HTTP 500 on both.
  This is content-verified drift. The owner subsequently approved PDF using the
  normal text routing policy, with no more probing of the third key. The gateway
  still marks unrequested Lite/Gemma with ⚠️ when they serve a request. Evidence:
  `docs/worklog/archive/2026.10.12-third-key-capability-probe/` and
  `docs/worklog/archive/2026.10.11-capability-canary/`.

  Corollary for measurement: **`countTokens` is lenient** — it accepted a PDF part for lite
  and preview while `generateContent` refused it, so a successful count is a price, never a
  capability proof. The scheduled `capability-check.yml` therefore runs a bounded
  generateContent canary on a CI key and verifies a hidden PDF marker/image color in
  the answer (not just HTTP 200), separate from the free `tariff-drift.yml` count.
  Drift is an alert for review, never an automatic change to the production gates;
  see this repo's `docs/worklog/archive/2026.10.11-capability-canary/` for its case matrix.

  Companion fix (same day): a quota-drained chain no longer tells the caller to re-upload
  the file under another key — that message now appears only when the key itself cannot
  serve the file-capable models.
- **Token estimate is script-aware.** `estimate_tokens` classifies characters and divides
  each class by its own ratio, because one divisor cannot serve mixed text: Latin prose
  runs ~5.4 chars/token, Cyrillic ~1.7, digits ~1.8. The old `len // 4` under-estimated
  Russian by ~40%, and under-estimating is what lets the TPM gate pass and the provider
  answer 429. Inputs above `sample_chars` are estimated from a scaled head+tail sample,
  so cost stays flat at ~5 ms regardless of document size. Setting `chars_per_token`
  restores the single-divisor behaviour.
- **One `listModels` call, not one per key.** The model list is a property of the
  account tier, so every key on one plan gets the same answer; `availability()` probes
  the first key and applies the answer to the rest. `availability_per_key()` is the
  separate question "does *this* key work", used by `doctor`, `models` and the setup
  wizard. A key is never marked dead by a failed `listModels` — refusing a key is
  `generateContent`'s job, and one key's failure says nothing about the others.
- Ledger statuses: `planned|ok|quota|http4xx|http5xx|network|blocked|skipped`;
  RPD-conservative set = `ok,quota,http5xx,blocked` plus in-flight `planned` rows (D3).
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

Google issues two shapes of Gemini API key today — classic `AIzaSy…` (Standard, Cloud
Console) and newer `AQ.…` (Auth, AI Studio) — and has changed more than once which surface
accepts which shape or transport. The library therefore does not assume: every provider
call goes through `keyauth`, which carries an ordered transport list and starts with the
one that last worked for that key.

| transport | wire form | when |
|---|---|---|
| `header` (default) | `x-goog-api-key: <key>` | everything the library did before — the common path adds no calls |
| `query` | `?key=<key>` | fallback for surfaces that refuse the header |
| `bearer` | `Authorization: Bearer <key>` | fallback for surfaces that expect a token |

Rules:
- **The provider's answer decides, not the key's shape.** A 401 always triggers the next
  transport; a 400/403 does so only when its body talks about keys/credentials, so a
  model-permission 403 still means "switch model" and costs nothing extra.
- **The winner is remembered per key** (process-local, keyed by fingerprint): after one
  rotation a key costs exactly one request per call, as before.
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
Cancellation propagates after resolving its reservation. Non-streaming APIs are unchanged.

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

`strict` is the default because of what the client actually sends: `file-search` asks for
its own hardcoded `gemini-2.5-flash`, which the router's keys cannot serve, so honouring
the request would fail every query. The chain exists precisely to answer "which model can
take this request right now", and the caller's model id is a default, not a requirement.

Every model-path response carries `x-router-model` and `x-router-key-id`, so a caller's
own run log can record which pair served it without correlating against this proxy's
audit stream by time.

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
