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
        thinking_level="low|medium|high" = "high",
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

Errors: `QuotaExhaustedError` carries the machine-readable payload
(per-model rpd_left/rpm_wait_s/tpm_wait_s/cooldown_until, reset_eta_pt, hint) — frontends
surface it as-is. `RouterError` base for everything else (network, http 4xx, parse).

## 3. Ledger (SQLite, `GEMINI_ROUTER_STATE`, default `/opt/apps/shared/gemini-router/state.db`)

Tables `calls`, `cooldowns`, `cache`, `meta` — full DDL and accounting rules:
gemini-mcp `docs/03-architecture.md` §3 (single canonical definition).
Rules summary: WAL + busy_timeout 5 s; TPM pre-flight writes a `status='planned'` row,
upgraded after the call; RPD counts `ok|quota|http5xx` attempts (conservative),
not client-validation 4xx/network; PT-midnight + rolling-24h dual gate; retention prune
via `gemini-router prune` and by frontends' daily task.

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

## 8. Semantics worth knowing

- **Thinking-token starvation:** in 3.x models `maxOutputTokens` counts thinking tokens,
  so a tiny cap yields empty visible text and no `candidatesTokenCount`. Therefore
  thought parts (`thought=true`) never join the visible text, `GenResult`/`RouterResult`
  carry `finish_reason` + `tokens_thought`, and `max_output_tokens_default` is a cap
  only (no spend implication).
- **Refused keys:** HTTP 401 marks the *key* dead, not the model — the key is dropped for
  the rest of the request and the pool continues; if every configured key is refused the
  error says so and names the remedy.
- `extract_json`: when several JSON candidates match, the earliest starting in the text
  wins (fixes nested `{…[…]…}` extraction).
- **Token estimate is script-aware.** `estimate_tokens` classifies characters and divides
  each class by its own ratio, because one divisor cannot serve mixed text: Latin prose
  runs ~5.4 chars/token, Cyrillic ~1.7, digits ~1.8. The old `len // 4` under-estimated
  Russian by ~40%, and under-estimating is what lets the TPM gate pass and the provider
  answer 429. Inputs above `sample_chars` are estimated from a scaled head+tail sample,
  so cost stays flat at ~5 ms regardless of document size. Setting `chars_per_token`
  restores the single-divisor behaviour.
- Ledger statuses: `planned|ok|quota|http4xx|http5xx|network|blocked|skipped`;
  RPD-conservative set = `ok,quota,http5xx,blocked` plus in-flight `planned` rows (D3).
  An in-flight request counts as spent so processes sharing one state file cannot all
  clear the day's gate; rows older than `planned_ttl_s` are abandoned attempts and stop
  counting, so a caller that died mid-request does not eat a day's budget forever.
- CLI exit codes: `0` ok · `2` config/usage error · `doctor` names its failure so a
  script can act without parsing prose: `3` no API keys, `4` state file unusable,
  `5` no configured chain model reachable on any key. `gemini-mcp` passes these through
  and adds `6` for an unwritable log directory.
