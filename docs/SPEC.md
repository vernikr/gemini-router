# gemini-router · SPEC

The router's own contract. Companion docs live in `vernikr/gemini-mcp/docs/02` + `docs/03`
(system-wide requirements, architecture, plan).

## 1. What it is

Quota-aware Gemini Flash router extracted from `tldr-digest/src/llm.py`, generalized:

- cascading model chain with runtime `listModels` validation (default
  `gemini-3.8-flash → 3.7 → 3.6 → 3.5 → 3-flash-preview-slot`);
- per **key×model** budgets: RPD (America/Los_Angeles day + rolling 24 h), RPM and TPM
  (sliding 60 s) — enforced pre-flight, accounted post-fact from `usageMetadata`;
- key pool (`GEMINI_API_KEYS` / `GEMINI_API_KEY_1..10`); selection prefers the pair with
  the largest remaining budget;
- persistent multi-process ledger (SQLite WAL) — the single source of quota truth shared
  by every consumer on the machine (MCP daemon, stdio sessions, cron jobs, foreign scripts);
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
from gemini_router import Router, RouterRequest, RouterResult, RouterEvent, QuotaExhaustedError

router = Router.from_env()            # env + optional GEMINI_ROUTER_CONFIG yaml
res: RouterResult = await router.complete(
    RouterRequest(
        prompt=str, system=str | None = None,
        thinking_level="low|medium|high" = "medium",
        output_format="text|json" = "text",
        max_output_tokens=8192,
        kind="single|chunk|consolidate|count" = "single",
        request_id=None, source="cli", overflow="off|lite|lite+gemma",
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
    source="cli", display_name=None, key=None, sha256=None, ready_timeout_s=30.0)
# UploadedFile: name, uri, mime, size, state, expires_at, sha256, key_id,
#               media_resolution ("low" | "medium" | "high" | None = provider default)
up  = up.with_resolution("high")            # pins the level for measurement AND request

n   = await router.count_file_tokens(model, up.uri, up.mime, key_id=up.key_id,
                                     media_resolution=up.media_resolution)   # free (FR-C7)
res = await router.complete(RouterRequest(prompt, est_tokens=…), files=[up], …)
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

`complete(..., files=[…])` pins the candidate keys to those the files were uploaded under
(a file lives in its key's own project) and refuses without `est_tokens`: the TPM gate must
include the measured file tokens, since a text-only estimate under-counts on the one
request where being wrong costs a 429 mid-job. The request carries `file_data` parts ahead
of the text, and the cache hash mixes in the file's `sha256` — never the URI, which changes
on every re-upload. A key rejected mid-upload moves to the next key; a request whose pinned
key cannot serve says so, and the caller re-uploads from its own blob.

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
`limits_per_key_model {rpd,rpm,tpm}`, `overflow_limits {rpd,rpm,tpm}`, `limits_overrides`,
`quota_day_tz`, `retry {max,backoff_base_s,jitter_max_s,provider_timeout_s,request_delay_s}`,
`wait {enabled_default,max_s,poll_s}` (FR-C13 semantics), `window_s`, `planned_ttl_s`,
`models_refresh_h`, `cache_ttl_h`, `state_path`, `base_url`, `temperature` (pre-3.x models
only), `max_output_tokens_default`, `chars_per_token` (unset = script-aware estimate). Defaults per gemini-mcp `docs/03-architecture.md` §8. Secrets never logged
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
- **A file job stays on the chain.** The overflow tiers exist for quota reasons on text; a
  file job sent there buys a wasted call and a misleading error, so a request carrying
  `files` never enters a tier, a model that answers 400/500 to a file request is remembered
  as incapable *for that media class* (capability is per model × modality), and a
  file-blind candidate set produces its own error instead of talking about quota.
  `file_capable_models:` states the capable models explicitly when an operator prefers
  that to learning — a flat list applies to every media class, a map narrows per class.

  **Two sources, deliberately kept apart** (this repo's rule, owner, 2026-10-11):

  *Practice* — measured 2026-10-11 on two keys, matrix in
  gemini-mcp `docs/worklog/2026.10.11-remaining-work/plan.md` §E: PDF via `file_data` →
  HTTP 500 on `gemini-3.5-flash-lite`, HTTP 400 on `gemini-3-flash-preview`, OK on
  `gemini-3.6-flash`; PNG via `file_data` → OK on lite and preview, 400 on
  `gemma-4-31b-it`, whose inline-image path works. The router enforces this per modality:
  a request carrying only images keeps lite/preview as candidates, while a PDF narrows
  the chain to the models that actually read PDFs.

  *Official* — `ai.google.dev` model pages (fetched 2026-10-11) list PDF as a supported
  input for `gemini-3.5-flash-lite`; the Gemma 4 card lists text and images. The docs and
  practice disagree for lite + PDF at the moment of writing, and both statements stay here
  on purpose.

  Corollary for measurement: **`countTokens` is lenient** — it accepted a PDF part for lite
  and preview while `generateContent` refused it, so a successful count is a price, never a
  capability proof. The scheduled `capability-check.yml` therefore runs a bounded
  generateContent canary on a CI key and verifies a hidden PDF marker/image color in
  the answer (not just HTTP 200), separate from the free `tariff-drift.yml` count.
  Drift is an alert for review, never an automatic change to the production gates;
  see this repo's `docs/worklog/2026.10.11-capability-canary/` for its case matrix.

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
