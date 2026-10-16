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

## Usage

```python
from gemini_router import Router, RouterRequest
router = Router.from_env()
res = await router.complete(RouterRequest(prompt=..., thinking_level="high"),
                            on_event=print)
```

### Streaming

Pass `stream=True` to `Router.complete`. `on_event` receives `stream_start` before
each provider attempt (discard the previous provisional text), then `text` events
with `detail["text"]` deltas. The returned `RouterResult.text` is authoritative;
cache hits return it without deltas. Text events are not retained in `result.events`.
Truncated, blocked and interrupted responses are never cached. Existing calls stay
non-streaming. Both paths share keys, model selection, auth fallback and the ledger.

Service-style (no Python): REST `/v1/complete` + `/v1/quota` on the gemini-mcp daemon.

### Native Gemini API pass-through

```bash
GEMINI_ROUTER_PROXY_TOKEN=$(openssl rand -hex 32) gemini-router proxy   # 127.0.0.1:8787
```

Then, on the client machine, `GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8787` (through an
SSH tunnel) plus `GEMINI_API_KEY=<the token>` — no change to the client's code. The base
URL must be a bare origin with no path prefix; see SPEC §11.

Conventions: English in files, Conventional Commits, SemVer tags `v*` (gemini-mcp pins
a tag), CI = ruff + pytest with mocked HTTP (see `AGENTS.md`).
