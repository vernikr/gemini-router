# gemini-router (npm)

Zero-dependency JavaScript client for the Python service
[`gemini-router`](https://pypi.org/project/gemini-router/). It builds the correlation
headers the native Gemini pass-through proxy understands and wraps its local
bookkeeping endpoints. It does **not** route requests or hold API keys — the service does.

Works in Node ≥18, Deno, Bun and browsers with `fetch`, `URL` and `Headers`.

```bash
npm install gemini-router
```

## One digest build, fully traceable

```js
import {
  createContext, routerFetch, note, jobStart, jobFinish, jobGet,
} from "gemini-router";

const base = "http://127.0.0.1:8787";
const token = process.env.GEMINI_ROUTER_PROXY_TOKEN;
const ctx = createContext({
  job: "digest-2026-10-28",
  initiator: "tldr-digest",
  step: "prefilter",
  comment: "сборка дайджеста за сегодня",   // non-ASCII is percent-encoded for headers
});

await jobStart(base, token, ctx);

// Any native Gemini call goes through the same headers.
const response = await routerFetch(
  base, token, ctx.withStep("analysis"),
  "/v1beta/models/gemini-3.8-flash:generateContent",
  { method: "POST", body: { contents: [{ parts: [{ text: "Summarize today" }] }] } },
);
console.log(response.headers.get("x-router-job"), response.headers.get("x-router-trace"));
const payload = await response.json();

await note(base, token, ctx, "analysis done");
const report = await jobGet(base, token, ctx, { n: 200 });
console.log(report.summary.calls, report.calls.length);
await jobFinish(base, token, ctx, { status: "done" });
```

CommonJS works too:

```js
const { createContext, headers } = require("gemini-router");
const ctx = createContext({ job: "batch-42", initiator: "worker" });
console.log(headers(process.env.GEMINI_ROUTER_PROXY_TOKEN, ctx));
```

## What the service records

Every request, upstream hop, provider verdict, cooldown and annotation carries `job`,
`trace`, `step`, `requestId` and the router version. A retry uses
`ctx.withTrace()` to get a new execution id while staying in the same job;
`ctx.withStep("dedup")` changes only the stage. Read one activity back with:

```bash
gemini-router job digest-2026-10-28 --json   # CLI from the Python package
curl -s 'http://127.0.0.1:8787/v1/job?id=digest-2026-10-28&n=200'
```

## API

| export | purpose |
|---|---|
| `createContext(options)` | immutable `RouterContext` with `job`, `trace`, `step`, `initiator`, `comment`, `requestId`, `spanId`, `traceparent` |
| `ctx.headers()` | router-only headers; `ctx.withStep()`, `ctx.withTrace()`, `ctx.withComment()`, `ctx.child()` derive new contexts |
| `headers(token, ctx, extra)` | `x-goog-api-key` + context headers, ready for `fetch()` |
| `routerFetch(base, token, ctx, path, init)` | fetch wrapper for any proxy path; JSON-encodes plain objects, returns the raw `Response` |
| `requestJSON(base, token, ctx, path, opts)` | JSON request to a bookkeeping endpoint; throws `RouterHttpError` on non-2xx |
| `jobStart`, `jobFinish` | `POST /v1/job` lifecycle events (`status: "done" \| "failed"`) |
| `jobs`, `jobGet` | `GET /v1/jobs`, `GET /v1/job` — counters and the full timeline |
| `note`, `notes` | `POST /v1/note`, `GET /v1/notes` — annotations with no provider call |
| `calls` | `GET /v1/calls` with `job`, `trace`, `step`, `model`, `status`, `source`, `requestId`, `n` filters |
| `health` | `GET /healthz` (no token) |
| `makeTraceparent`, `parseTraceparent` | W3C Trace Context helpers |
| `CLIENT_CONTEXT_HEADERS` | the header names the proxy consumes locally |
| `makeURL`, `randomHex`, `RouterContext`, `RouterHttpError` | low-level helpers and classes |

All helpers accept an optional `fetch` implementation and an `AbortSignal` in their
options object. The package ships TypeScript declarations (`index.d.ts`).

## Headers

| header | meaning |
|---|---|
| `X-Router-Job` | one target activity; defaults to `<initiator>@<UTC date>` |
| `X-Router-Trace` | one execution of the activity (a 32-hex W3C trace id by default) |
| `X-Router-Step` | stage label, e.g. `prefilter`, `analysis`, `dedup` |
| `X-Router-Initiator` | who started the work |
| `X-Router-Request-Id` | one logical request |
| `X-Router-Comment` | free text; stored, never used for routing |
| `traceparent` | emitted when the trace id is a valid 32-hex W3C field |
| `x-goog-api-key` | the proxy token (the client credential) |

Responses carry `x-router-version`, `x-router-job`, `x-router-trace`,
`x-router-request-id` and `x-router-traceparent`.

## Tests

```bash
npm test
```

The tests run a local `node:http` server; no network, tokens or Gemini keys required.

## License

MIT — see [LICENSE](LICENSE).
