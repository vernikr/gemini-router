import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { after, before, test } from "node:test";

import {
  CLIENT_CONTEXT_HEADERS,
  RouterContext,
  RouterHttpError,
  calls,
  createContext,
  headers,
  health,
  jobFinish,
  jobGet,
  jobStart,
  jobs,
  makeTraceparent,
  makeURL,
  note,
  notes,
  parseTraceparent,
  routerFetch,
} from "../index.js";

let server;
let base;
const seen = [];

before(async () => {
  server = createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const url = new URL(req.url, "http://localhost");
      const raw = Buffer.concat(chunks).toString("utf8");
      seen.push({ method: req.method, path: url.pathname,
        query: Object.fromEntries(url.searchParams), headers: req.headers,
        body: raw ? JSON.parse(raw) : null });
      res.setHeader("content-type", "application/json");
      res.setHeader("x-router-job", "digest-2026-10-28");
      res.setHeader("x-router-trace", "abc");
      if (url.pathname === "/v1/job" && req.method === "GET" &&
          url.searchParams.get("id") === "missing") {
        res.statusCode = 404;
        res.end(JSON.stringify({ error: { message: "job not found" } }));
        return;
      }
      res.end(JSON.stringify({ ok: true, version: "0.12.0",
        path: url.pathname, method: req.method }));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  await new Promise((resolve) => server.close(resolve));
});

test("createContext: defaults, W3C trace id and header shape", () => {
  const ctx = createContext({ initiator: "tldr-digest" });
  assert.ok(ctx instanceof RouterContext);
  assert.match(ctx.job, /^tldr-digest@\d{4}-\d{2}-\d{2}$/);
  assert.match(ctx.trace, /^[0-9a-f]{32}$/);
  assert.equal(ctx.traceId, ctx.trace);
  assert.match(ctx.spanId, /^[0-9a-f]{16}$/);
  assert.match(ctx.requestId, /^[0-9a-f]{12}$/);
  const h = ctx.headers();
  assert.equal(h["X-Router-Job"], ctx.job);
  assert.equal(h["X-Router-Trace"], ctx.trace);
  assert.equal(h["X-Router-Initiator"], "tldr-digest");
  assert.deepEqual(parseTraceparent(h.traceparent).traceId, ctx.trace);
  assert.ok(CLIENT_CONTEXT_HEADERS.includes("x-router-job"));
});

test("createContext: traceparent seeds trace and parent span", () => {
  const tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";
  const ctx = createContext({ job: "digest-2026-10-28", initiator: "worker", traceparent: tp });
  assert.equal(ctx.trace, "4bf92f3577b34da6a3ce929d0e0e4736");
  assert.equal(ctx.parentSpan, "00f067aa0ba902b7");
  assert.equal(ctx.headers()["X-Router-Parent-Span"], "00f067aa0ba902b7");
  assert.equal(ctx.toJSON().job, "digest-2026-10-28");
});

test("headers: non-ASCII text is encoded, extra options win", () => {
  const ctx = createContext({ job: "digest-2026-10-28", initiator: "tldr-digest",
    step: "analysis", comment: "утренняя сборка" });
  const h = headers("TOKEN", ctx, { "content-type": "application/json", "X-Router-Step": "dedup" });
  assert.equal(h["x-goog-api-key"], "TOKEN");
  assert.equal(h["X-Router-Step"], "dedup");
  assert.equal(h["X-Router-Comment"], encodeURIComponent("утренняя сборка"));
  assert.equal(h["content-type"], "application/json");
});

test("context derivation: step, retry trace, child span", () => {
  const ctx = createContext({ job: "digest-2026-10-28", initiator: "tldr-digest",
    step: "prefilter", trace: "7f3a9c2e" });
  assert.equal(ctx.headers().traceparent, undefined);   // not a 32-hex W3C id
  const analysis = ctx.withStep("analysis");
  assert.equal(analysis.step, "analysis");
  assert.equal(analysis.job, ctx.job);
  assert.equal(analysis.trace, ctx.trace);
  const retry = ctx.withTrace();
  assert.match(retry.trace, /^[0-9a-f]{32}$/);
  assert.equal(retry.job, ctx.job);
  const child = ctx.child();
  assert.equal(child.parentSpan, ctx.spanId);
  assert.notEqual(child.requestId, ctx.requestId);
  assert.equal(ctx.withComment("done").comment, "done");
});

test("makeTraceparent always produces valid W3C fields", () => {
  for (const trace of ["run-7f3a9c2e", "4bf92f3577b34da6a3ce929d0e0e4736", ""]) {
    const tp = makeTraceparent(trace, "span-1");
    assert.match(tp, /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
    assert.equal(parseTraceparent(tp).sampled, true);
  }
  assert.equal(parseTraceparent("ff-00000000000000000000000000000000-0000000000000000-01"), null);
  assert.equal(parseTraceparent("nope"), null);
});

test("makeURL builds bare-origin URLs with a query", () => {
  const u = makeURL("http://127.0.0.1:8787/", "/v1/job", { id: "digest-2026-10-28", n: 10, skip: "" });
  assert.equal(u.pathname, "/v1/job");
  assert.equal(u.searchParams.get("id"), "digest-2026-10-28");
  assert.equal(u.searchParams.get("n"), "10");
  assert.equal(u.searchParams.has("skip"), false);
});

test("job lifecycle helpers send the right method, body and headers", async () => {
  const ctx = createContext({ job: "digest-2026-10-28", initiator: "tldr-digest",
    step: "prefilter" });
  assert.equal((await jobStart(base, "TOKEN", ctx, { comment: "start" })).ok, true);
  assert.equal((await jobFinish(base, "TOKEN", ctx, { status: "failed" })).ok, true);
  const start = seen.find((r) => r.path === "/v1/job" && r.method === "POST" &&
    r.body.action === "start");
  const finish = seen.find((r) => r.path === "/v1/job" && r.method === "POST" &&
    r.body.action === "finish");
  assert.equal(start.body.job, "digest-2026-10-28");
  assert.equal(start.body.comment, "start");
  assert.equal(finish.body.status, "failed");
  assert.equal(start.headers["x-router-job"], "digest-2026-10-28");
  assert.equal(start.headers["x-goog-api-key"], "TOKEN");
});

test("read helpers build filters and surface HTTP errors", async () => {
  const ctx = createContext({ job: "digest-2026-10-28", initiator: "tldr-digest" });
  assert.equal((await jobs(base, "TOKEN", ctx, 5)).ok, true);
  assert.equal((await jobGet(base, "TOKEN", ctx, { n: 100 })).ok, true);
  assert.equal((await note(base, "TOKEN", ctx, "prefilter done")).ok, true);
  assert.equal((await notes(base, "TOKEN", ctx, { n: 7 })).ok, true);
  assert.equal((await calls(base, "TOKEN", ctx,
    { job: "digest-2026-10-28", step: "prefilter", requestId: "rid-1", n: 3 })).ok, true);
  assert.equal((await health(base)).version, "0.12.0");

  const jobsReq = seen.find((r) => r.path === "/v1/jobs");
  assert.equal(jobsReq.query.n, "5");
  const callsReq = seen.find((r) => r.path === "/v1/calls");
  assert.equal(callsReq.query.step, "prefilter");
  assert.equal(callsReq.query.request_id, "rid-1");
  const noteReq = seen.find((r) => r.path === "/v1/note");
  assert.equal(noteReq.body.comment, "prefilter done");
  assert.equal(noteReq.body.job, "digest-2026-10-28");

  await assert.rejects(
    () => jobGet(base, "TOKEN", ctx, { job: "missing" }),
    (err) => err instanceof RouterHttpError && err.status === 404 &&
      /job not found/.test(err.message),
  );
});

test("routerFetch JSON-encodes objects and returns the raw response", async () => {
  const ctx = createContext({ job: "digest-2026-10-28", initiator: "tldr-digest" });
  const res = await routerFetch(base, "TOKEN", ctx,
    "/v1beta/models/gemini-3.8-flash:generateContent",
    { method: "POST", body: { contents: [{ parts: [{ text: "hi" }] }] } });
  assert.equal(res.headers.get("x-router-job"), "digest-2026-10-28");
  assert.equal(res.headers.get("x-router-trace"), "abc");
  assert.equal((await res.json()).ok, true);
  const req = seen.find((r) => r.path.includes(":generateContent"));
  assert.equal(req.headers["content-type"], "application/json");
  assert.equal(req.body.contents[0].parts[0].text, "hi");
  assert.equal(req.headers["x-router-job"], "digest-2026-10-28");
});

test("CommonJS entry exports the same implementation", () => {
  const require = createRequire(import.meta.url);
  const cjs = require("../index.cjs");
  assert.equal(typeof cjs.createContext, "function");
  const ctx = cjs.createContext({ job: "batch-42", initiator: "worker" });
  assert.equal(ctx.job, "batch-42");
  assert.equal(cjs.headers("TOKEN", ctx)["X-Router-Job"], "batch-42");
});
