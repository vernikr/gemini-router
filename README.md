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

Conventions: English in files, Conventional Commits, SemVer tags `v*` (gemini-mcp pins
a tag), CI = ruff + pytest with mocked HTTP (see `AGENTS.md`).
