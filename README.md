# gemini-router

Quota-aware **Gemini Flash router** as a standalone, reusable module — extracted and
generalized from [`tldr-digest/src/llm.py`](https://github.com/vernikr/tldr-digest).

One persistent ledger (SQLite WAL) lets the [gemini-mcp](https://github.com/vernikr/gemini-mcp)
gateway and CLI calls share quota truth on a machine. Other programs can join the same
ledger; `tldr-digest` has **not** been migrated yet.

- Cascading chain with runtime `listModels` validation (configurable; the current
  default lives only in `src/gemini_router/config.py:DEFAULT_CHAIN`).
- Per **key×model** budgets: RPD (Pacific-time day + rolling 24 h), RPM, TPM (sliding
  60 s) — pre-flight enforced, accounted from `usageMetadata`; conservative 429 handling
  with cooldowns and `Retry-After`.
- Key pool (`GEMINI_API_KEYS` / `GEMINI_API_KEY_1..10`), budget-maximizing selection.
- `thinking_level` per request (Gemini 3.x rules), request cache (0-RPD hits), opt-in
  overflow tiers (`flash-lite → gemma-auto`) behind one `overflow_default` config key that
  every entry point (library and proxy) walks identically, event stream for progress
  frontends.
- Per-request wait policy for minute-scale gates (RPM/TPM/short cooldowns): default
  **wait** with countdown events (`waiting`, remaining seconds); `wait=False` skips to
  the next candidate or returns wait ETAs in the structured error. Daily resets are
  never waited on.
- CLI: `gemini-router quota | models | calls | prune | doctor | proxy` (rich tables).
- **Native Gemini API pass-through** (`gemini-router proxy`, loopback): point
  `GOOGLE_GEMINI_BASE_URL` at it to run a Google Gen AI SDK client — e.g. the
  `mikesmitty/file-search` MCP — on this key pool. Model **and key** of a model call come
  from the same selection walk the library uses (chain, overflow, budgets — SPEC §11.6);
  File Search resources stay pinned to the Google project that owns them ([SPEC §11](docs/SPEC.md#11-native-gemini-api-pass-through-gemini-router-proxy),
  wiring: [docs/file-search-wiring.md](docs/file-search-wiring.md)).
- Deps: `httpx`, `pydantic`, `rich`, stdlib `sqlite3` — **no MCP/web-framework coupling**.

Contract: [`docs/SPEC.md`](docs/SPEC.md).

## Install

```bash
uv add gemini-router          # PyPI
gemini-router quota           # RPD left / RPM & TPM windows / cooldowns / recommendation
```

## How to Call from Another Project (3 Ways)

### 1. HTTP REST API via `gemini-mcp` daemon (`127.0.0.1:8790`) — Language-Agnostic

Ensure the SSH tunnel is active (`packaging/com.vernikr.gemini-tunnel.plist` forwards `:8790` and `:8787` from the VPS):

```bash
# Health check (no auth):
curl -s http://127.0.0.1:8790/healthz

# Quota table:
curl -s http://127.0.0.1:8790/v1/quota \
  -H "Authorization: Bearer $GEMINI_MCP_TOKEN"

# Single-shot completion:
curl -s -X POST http://127.0.0.1:8790/v1/complete \
  -H "Authorization: Bearer $GEMINI_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Summarize these messages...",
    "system": "You are a concise analyst.",
    "route": "auto",
    "thinking_level": "medium",
    "overflow": "lite"
  }'
# Returns: {"report": "...", "result": "<text>", "meta": {"model": "...", "latency_s": 2.1, "progress_trace": [...]}}
```

### 2. Native Gemini v1beta Pass-Through Proxy (`127.0.0.1:8787`)

For standard Google Gen AI SDKs or clients calling `/v1beta/models/{model}:generateContent`:

```bash
GEMINI_ROUTER_PROXY_TOKEN=$(openssl rand -hex 32) gemini-router proxy   # 127.0.0.1:8787
```

On the client machine, set `GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8787` (bare origin, no path prefix) and `GEMINI_API_KEY=$GEMINI_ROUTER_PROXY_TOKEN` (sent as `x-goog-api-key`). See [SPEC §11](docs/SPEC.md#11-native-gemini-api-pass-through-gemini-router-proxy).

### 3. Python Library (`gemini_router`)

```python
from gemini_router import Config, Router, RouterRequest

cfg = Config.load(state_path="~/.config/gemini-mcp/state.db")
router = Router(cfg)
try:
    res = await router.complete(
        RouterRequest(
            prompt="...",
            system="...",
            thinking_level="medium",   # "low" | "medium" | "high" | None
            overflow="lite",           # "off" | "lite" | "lite+gemma"
        ),
        on_event=lambda ev: print(ev.message(), flush=True),
    )
    print(res.text, res.model, res.latency_s)
finally:
    await router.aclose()
```

### Streaming

Pass `stream=True` to `Router.complete`. `on_event` receives `stream_start` before
each provider attempt (discard the previous provisional text), then `text` events
with `detail["text"]` deltas. The returned `RouterResult.text` is authoritative;
cache hits return it without deltas. Text events are not retained in `result.events`.
Truncated, blocked and interrupted responses are never cached. Existing calls stay
non-streaming. Both paths share keys, model selection, auth fallback and the ledger.

## Provider truth, request identity and annotations (v0.11.0)

The counters in the ledger are a **prediction**; Google is the authority. Before v0.11
a live 429 only benched a pair for ~65 seconds, and the pass-through re-selected pairs
from scratch on every hop of a walk. On 2026-10-21 three client requests therefore
produced **48 identical 429s in ten seconds**: three walks × 16 hops, cycling the same
exhausted `key × model` pairs, with only three refusals reaching the client.

Each API key belongs to a **separate Google account** (per-key×model isolation is
correct — there is no shared project bucket to model). The incident was an
amplification bug plus a short provider-side capacity blip, not a shared quota.

What v0.11 changes:

- **One `feedback` module** parses every 429/503 (real `Retry-After` header, HTTP-date,
  `retryDelay`/`quotaId`/`quotaValue` in the body) and writes a `pair_state` row.
  A confirmed `PerDay` verdict blocks the pair until the next **Pacific-time reset** —
  no more `min(midnight, now + 65)`. An unexplained 429 benches it for the advertised
  delay; three such hits inside five minutes are promoted to a day block.
- **The walk never repeats a pair.** `visited` is threaded through `pick_pair`; when
  every candidate is benched, the proxy answers locally with `429 + Retry-After`
  (circuit breaker) instead of replaying the same refusals upstream.
- **The proxy waits** for minute-scale gates (`proxy.walk_wait`) instead of firing at a
  cooling pair. `2xx` clears any earlier provider block — truth is symmetric.
- `503` is **not** RPD spend any more; counting overloads as daily usage used to make a
  capacity storm look like an exhausted day. `cooldowns` is still written (mirrored) so
  older library builds sharing the database agree on blocked pairs.

### Identity and free-form comments

Every audit row and every log line carries the router **version**, the **initiator**
(free-form string supplied by the caller) and an arbitrary **comment** that has no
behavioural effect whatsoever — it is stored and logged, never used for routing and
never sent to Google.

```bash
# HTTP clients (headers are ASCII; percent-encode non-ASCII text)
curl -s -X POST http://127.0.0.1:8787/v1beta/models/gemini-3.8-flash:generateContent \
  -H "x-goog-api-key: $GEMINI_ROUTER_PROXY_TOKEN" \
  -H "X-Router-Initiator: tldr-digest" \
  -H "X-Router-Comment: starting new version testing" \
  -H "X-Router-Request-Id: digest-20261028" -H 'Content-Type: application/json' \
  -d '{"contents":[{"parts":[{"text":"hi"}]}]}'

# Python library
RouterRequest(prompt="...", initiator="tldr-digest",
              comment="начинаем тестирование новой версии")

# Standalone annotation: no provider call, no quota effect
gemini-router note "начинаем тестирование новой версии" --initiator ops-audit
gemini-router notes -n 20
curl -s -X POST http://127.0.0.1:8787/v1/note \
  -H "x-goog-api-key: $GEMINI_ROUTER_PROXY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"comment":"начинаем тестирование новой версии","initiator":"ops-audit"}'

# Inspect: version / initiator / comment / signal are in the audit rows
gemini-router calls -n 50 --wide
gemini-router calls --initiator tldr-digest --json
curl -s http://127.0.0.1:8787/healthz          # includes "version"
```

Structured logs are one-line logfmt (`event=request v=0.11.0 rid=... initiator=...
comment=... key=... model=... status=...`). Startup emits `event=startup`,
provider verdicts emit `event=provider_feedback`, annotations emit `event=note`.

New config keys (all optional, see `config.example.yaml`): `feedback.throttle_min_s`,
`throttle_max_s`, `buffer_s`, `reset_buffer_s`, `overload_cooldown_s`, `promote_hits`,
`promote_window_s`; `proxy.default_initiator`, `walk_wait`, `pool_circuit_breaker`. Schema v3 is purely additive — older readers keep working.

Full design: [SPEC §12](docs/SPEC.md#12-provider-truth-and-request-identity-v011).

Conventions: English in files, Conventional Commits, SemVer tags `v*` (gemini-mcp pins
a tag), CI = ruff + pytest with mocked HTTP (see `AGENTS.md`).
