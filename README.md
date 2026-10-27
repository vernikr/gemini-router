# gemini-router

Quota-aware **Gemini Flash router** with one persistent SQLite WAL ledger. A key pool,
cascading model chain, per-`key×model` RPD/RPM/TPM budgets, provider-truth feedback and
structured job/trace audit are shared by the Python library, the CLI, the native Gemini
API pass-through proxy and any HTTP client.

- Per **key×model** budgets: RPD (Pacific-time day + rolling 24 h), RPM, TPM (sliding
  60 s). Pre-flight enforced, accounted from `usageMetadata`. Each key belongs to a
  separate Google account; budgets are tracked per key and model.
- Cascading chain with runtime `listModels` validation; opt-in overflow tiers
  (`flash-lite → gemma-auto`) behind `overflow_default`, applied identically by the
  library and the proxy.
- Key pool (`GEMINI_API_KEYS` / `GEMINI_API_KEY_1..10`), budget-maximizing selection,
  request cache (0-RPD hits), event stream for progress frontends.
- Per-request wait policy for minute-scale gates (RPM/TPM/short cooldowns): default
  **wait** with countdown events (`waiting`, remaining seconds); `wait=False` skips to the
  next candidate or returns wait ETAs in the structured error. Daily resets are not waited
  on.
- **Provider truth**: 429/503 answers parse `Retry-After` (delta-seconds or HTTP-date),
  `retryDelay` and `quotaId` from the body; the exact `(key, model)` pair is benched in
  `pair_state`. A confirmed `PerDay` verdict lasts until the next Pacific-time reset;
  repeated unexplained 429s escalate to a day block; `2xx` clears a block; `503` is
  overload, not RPD spend. Each walk remembers the pairs it visited.
- **Job/trace audit**: one target activity (`digest-2026-10-28`, `mcp-session-…`) spans
  requests, hops, provider signals, waits, cooldowns and annotations. Schema v4 stores
  `jobs` and job/trace/step/hop fields on `calls`, `notes` and `pair_state`; CLI and HTTP
  endpoints read them back.
- **Native Gemini API pass-through** (`gemini-router proxy`, loopback): point
  `GOOGLE_GEMINI_BASE_URL` at it to run a Google Gen AI SDK client on the key pool
  ([SPEC §11](docs/SPEC.md#11-native-gemini-api-pass-through-gemini-router-proxy),
  wiring: [docs/file-search-wiring.md](docs/file-search-wiring.md)).
- Python deps: `httpx`, `pydantic`, `pyyaml`, `rich` + stdlib — no MCP/web-framework
  coupling. The npm package `gemini-router` is a zero-dependency client helper for the
  HTTP surface.

Contract: [`docs/SPEC.md`](docs/SPEC.md) · changes: [`CHANGELOG.md`](CHANGELOG.md).

## Install

```bash
uv add gemini-router                  # Python (PyPI)
npm install gemini-router             # JS helpers for the HTTP surface (npm)
gemini-router quota                   # RPD left / RPM & TPM windows / cooldowns
gemini-router --version
```

## How to call from another project

### 1. HTTP REST API via `gemini-mcp` daemon (`127.0.0.1:8790`)

Language-agnostic. Ensure the SSH tunnel is active
(`packaging/com.vernikr.gemini-tunnel.plist` forwards `:8790` and `:8787` from the VPS):

```bash
curl -s http://127.0.0.1:8790/healthz
curl -s http://127.0.0.1:8790/v1/quota -H "Authorization: Bearer $GEMINI_MCP_TOKEN"
curl -s -X POST http://127.0.0.1:8790/v1/complete \
  -H "Authorization: Bearer $GEMINI_MCP_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":"Summarize these messages...","system":"You are a concise analyst.",
       "route":"auto","thinking_level":"medium","overflow":"lite"}'
# {"report": "...", "result": "<text>", "meta": {"model": "...", ...}}
```

### 2. Native Gemini v1beta pass-through proxy (`127.0.0.1:8787`)

For Google Gen AI SDKs or clients that call `/v1beta/models/{model}:generateContent`:

```bash
GEMINI_ROUTER_PROXY_TOKEN=$(openssl rand -hex 32) gemini-router proxy   # 127.0.0.1:8787
```

On the client machine set `GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8787` (bare origin,
no path prefix) and `GEMINI_API_KEY=$GEMINI_ROUTER_PROXY_TOKEN` (sent as
`x-goog-api-key`). See [SPEC §11](docs/SPEC.md#11-native-gemini-api-pass-through-gemini-router-proxy).

### 3. Python library (`gemini_router`)

```python
from gemini_router import Config, Router, RouterRequest

cfg = Config.load(state_path="~/.config/gemini-mcp/state.db")
router = Router(cfg)
try:
    res = await router.complete(
        RouterRequest(
            prompt="...", system="...",
            thinking_level="medium",   # "low" | "medium" | "high" | None
            overflow="lite",           # "off" | "lite" | "lite+gemma"
            initiator="tldr-digest",
            job="digest-2026-10-28", trace_id="run-7f3a", step="analysis",
            comment="утренняя сборка",
        ),
        on_event=lambda ev: print(ev.message(), flush=True),
    )
    print(res.text, res.model, res.latency_s)
finally:
    await router.aclose()
```

Pass `stream=True` to `Router.complete`. `on_event` receives `stream_start` before each
provider attempt (discard the previous provisional text), then `text` events with
`detail["text"]` deltas. `RouterResult.text` is authoritative; cache hits return it
without deltas. Text events are not retained in `result.events`. Truncated, blocked and
interrupted responses are never cached.

## Job / trace correlation

A **job** is the ID of one target activity — a full digest build for today, one MCP
session, one batch. All requests, hops, provider signals, cooldown decisions and notes
that belong to it carry the same `job`. A **trace** is one execution of that job;
retries get a new trace and stay inside the job. `step` names the client's stage
(`prefilter`, `analysis`, `dedup`, `subject`, `gemini-search`, …), `hop` counts upstream
attempts inside one request's selection walk.

### HTTP headers (consumed locally, never forwarded upstream)

```
X-Router-Initiator: tldr-digest            # who started the work (alias X-Router-Source)
X-Router-Job: digest-2026-10-28            # target activity (alias X-Router-Job-Id)
X-Router-Trace: 7f3a9c2e                   # one run (alias X-Router-Trace-Id)
X-Router-Step: prefilter                   # stage (alias X-Router-Stage)
X-Router-Request-Id: digest-20261028-001   # one logical request (alias X-Router-Span)
X-Router-Comment: утренняя сборка          # free text (alias X-Router-Note)
X-Router-Parent-Span: <16 hex>             # link a child request to its parent
traceparent: 00-<32 hex>-<16 hex>-01       # W3C Trace Context, also accepted
```

Header values are ASCII; percent-encode non-ASCII text. When no job is supplied the
router groups work as `<initiator>@<UTC date>`; when no trace is supplied the job is the
trace. Forwarded responses carry `x-router-version`, `x-router-job`, `x-router-trace`,
`x-router-request-id`, `x-router-traceparent`, `x-router-model` and `x-router-key-id`.

### Lifecycle endpoints

```bash
curl -s -X POST http://127.0.0.1:8787/v1/job \
  -H "x-goog-api-key: $GEMINI_ROUTER_PROXY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"start","job":"digest-2026-10-28","initiator":"tldr-digest",
       "trace":"7f3a9c2e","comment":"сборка дайджеста за сегодня"}'

# ... all generateContent calls use X-Router-Job / X-Router-Trace / X-Router-Step ...

curl -s -X POST http://127.0.0.1:8787/v1/job \
  -H "x-goog-api-key: $GEMINI_ROUTER_PROXY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"finish","job":"digest-2026-10-28","status":"done","comment":"ok"}'

curl -s 'http://127.0.0.1:8787/v1/jobs?n=20'                 # job list with counters
curl -s 'http://127.0.0.1:8787/v1/job?id=digest-2026-10-28&n=200'  # summary + events + calls
curl -s 'http://127.0.0.1:8787/v1/notes?job=digest-2026-10-28&n=50'
```

`POST /v1/note` accepts `job`, `trace`, `step`, `initiator` and `comment` and writes an
annotation with no provider call and no quota effect. `GET /v1/calls` filters by `job`,
`trace`, `step`, `initiator`, `model`, `status`, `source` and `request_id`.

### CLI

```bash
gemini-router job-start digest-2026-10-28 --initiator tldr-digest --trace 7f3a9c2e
gemini-router note "prefilter done" --job digest-2026-10-28 --trace 7f3a9c2e
gemini-router jobs -n 20                        # status, calls, ok, refusals, 429, models
gemini-router job digest-2026-10-28 --json     # full timeline of one activity
gemini-router job-finish digest-2026-10-28 --status done
gemini-router calls --job digest-2026-10-28 --step prefilter --wide
gemini-router notes --job digest-2026-10-28
```

### npm helper

```js
import { createContext, headers, note, jobStart, jobFinish, jobGet } from "gemini-router";

const base = "http://127.0.0.1:8787";
const token = process.env.GEMINI_ROUTER_PROXY_TOKEN;
const ctx = createContext({ job: "digest-2026-10-28", initiator: "tldr-digest",
                            step: "prefilter" });   // generates a trace id
await jobStart(base, token, ctx, { comment: "сборка дайджеста за сегодня" });
const res = await fetch(`${base}/v1beta/models/gemini-3.8-flash:generateContent`, {
  method: "POST", headers: { ...headers(token, ctx), "content-type": "application/json" },
  body: JSON.stringify({ contents: [{ parts: [{ text: "hi" }] }] }),
});
console.log(res.headers.get("x-router-job"), res.headers.get("x-router-trace"));
await note(base, token, ctx.withStep("analysis").withTrace(), "prefilter done");
console.log(await jobGet(base, token, ctx.job));
await jobFinish(base, token, ctx, { status: "done" });
```

The package has no runtime dependencies and works in Node ≥18, Deno and browsers that
provide `fetch`. See [`npm/README.md`](npm/README.md).

## Provider truth and request identity

Ledger counters are a *prediction*; the provider's answer is the *measurement*. Every
429/503 answer updates `pair_state` for its exact `(key, model)` pair:

- `429 + quotaId: ...PerDay...` blocks the pair until the next Pacific-time reset;
  `Retry-After` is recorded but never caps a daily verdict. `...PerMinute...` and
  unexplained 429s bench the pair for the advertised delay (bounded by
  `feedback.throttle_min_s` / `throttle_max_s`); `promote_hits` unexplained 429s inside
  `promote_window_s` escalate to a day block.
- `503/502` is a short overload bench and never counts as RPD spend. `cooldowns` mirrors
  the decision for older readers; `2xx` clears a provider block.
- A walk skips pairs already visited in the same request. With `proxy.walk_wait` it waits
  out minute-scale gates; with `proxy.pool_circuit_breaker` it answers locally with
  `429 + Retry-After` when every candidate is benched.

Every audit row and log line carries the router **version**, **initiator**, **job**,
**trace**, **step**, **request id** and an arbitrary behaviour-free **comment** that is
never used for routing and never sent to Google. Structured logs are one-line logfmt
(`event=request v=0.12.0 rid=... job=... trace=... step=... key=... model=...
status=...`). Startup emits `event=startup`, provider verdicts `event=provider_feedback`,
annotations `event=note`, job events `event=job`. `/healthz` reports the version.

Config (all optional, see [`config.example.yaml`](config.example.yaml)):
`feedback.throttle_min_s`, `throttle_max_s`, `buffer_s`, `reset_buffer_s`,
`overload_cooldown_s`, `promote_hits`, `promote_window_s`; `proxy.default_initiator`,
`walk_wait`, `pool_circuit_breaker`. Schema v4 is additive: `jobs`, job/trace fields on
`calls`, `notes` and `pair_state`; older readers keep reading `calls` and `cooldowns`.

Full design: [SPEC §12](docs/SPEC.md#12-provider-truth-and-request-identity) and
[SPEC §13](docs/SPEC.md#13-job--trace-correlation).

Conventions: English in files, Conventional Commits, SemVer tags `v*` (`gemini-mcp` pins
a tag), CI = ruff + pytest with mocked HTTP (see [`AGENTS.md`](AGENTS.md)).
