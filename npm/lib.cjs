"use strict";

/**
 * gemini-router HTTP client helpers.
 *
 * The Python service owns routing and quota accounting. This module only builds the
 * correlation headers the proxy understands and calls its local bookkeeping endpoints.
 * No runtime dependencies: everything uses the platform fetch/URL/Headers/crypto.
 */

const CLIENT_CONTEXT_HEADERS = Object.freeze([
  "x-router-initiator",
  "x-router-source",
  "x-router-comment",
  "x-router-note",
  "x-router-request-id",
  "x-router-span",
  "x-router-job",
  "x-router-job-id",
  "x-router-trace",
  "x-router-trace-id",
  "x-router-step",
  "x-router-stage",
  "x-router-parent-span",
  "traceparent",
  "tracestate",
  "baggage",
]);

const CONTROLS = /[\x00-\x1f\x7f]/g;
const HEX32 = /^[0-9a-f]{32}$/i;
const HEX16 = /^[0-9a-f]{16}$/i;

function clean(value, limit) {
  if (value === undefined || value === null || value === false) return "";
  return String(value).replace(CONTROLS, " ").trim().slice(0, limit);
}

/** HTTP headers are ASCII; the router percent-decodes values in its audit trail. */
function asciiHeader(value) {
  const text = String(value);
  // eslint-disable-next-line no-control-regex
  return /^[\x20-\x7e]*$/.test(text) ? text : encodeURIComponent(text);
}

function randomHex(bytes) {
  const cryptoImpl = globalThis.crypto;
  if (cryptoImpl && typeof cryptoImpl.getRandomValues === "function") {
    const buffer = new Uint8Array(bytes);
    cryptoImpl.getRandomValues(buffer);
    return Array.from(buffer, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  let out = "";
  while (out.length < bytes * 2) out += Math.random().toString(16).slice(2);
  return out.slice(0, bytes * 2);
}

/** A valid W3C hex field: pass valid hex through, hash anything else. */
function hexField(value, size) {
  const raw = clean(value, 128).replace(/-/g, "").toLowerCase();
  if (raw.length >= size && [...raw.slice(0, size)].every((c) => "0123456789abcdef".includes(c))) {
    return raw.slice(0, size);
  }
  let out = "";
  for (let i = 0; out.length < size; i += 1) {
    let hash = (0x811c9dc5 ^ i) >>> 0;
    for (const ch of `${value ?? ""}:${i}`) {
      hash ^= ch.charCodeAt(0);
      hash = Math.imul(hash, 0x01000193) >>> 0;
    }
    out += hash.toString(16).padStart(8, "0");
  }
  return out.slice(0, size);
}

function makeTraceparent(traceId, spanId, sampled = true) {
  return `00-${hexField(traceId, 32)}-${hexField(spanId, 16)}-${sampled ? "01" : "00"}`;
}

function parseTraceparent(value) {
  const match = /^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(?:-.*)?$/i
    .exec(String(value || "").trim());
  if (!match || match[1].toLowerCase() === "ff") return null;
  return {
    version: match[1].toLowerCase(),
    traceId: match[2].toLowerCase(),
    parentSpanId: match[3].toLowerCase(),
    flags: match[4].toLowerCase(),
    sampled: (parseInt(match[4], 16) & 1) === 1,
  };
}

class RouterContext {
  constructor(fields) {
    this.job = fields.job;
    this.trace = fields.trace;
    this.step = fields.step;
    this.initiator = fields.initiator;
    this.comment = fields.comment;
    this.requestId = fields.requestId;
    this.spanId = fields.spanId;
    this.parentSpan = fields.parentSpan;
    Object.freeze(this);
  }

  get traceId() { return this.trace; }

  get traceparent() {
    return makeTraceparent(this.trace, this.spanId);
  }

  /** Plain object of router-only headers. Values with non-ASCII characters are encoded. */
  headers() {
    const out = {
      "X-Router-Job": asciiHeader(this.job),
      "X-Router-Trace": asciiHeader(this.trace),
      "X-Router-Request-Id": asciiHeader(this.requestId),
    };
    if (this.initiator) out["X-Router-Initiator"] = asciiHeader(this.initiator);
    if (this.step) out["X-Router-Step"] = asciiHeader(this.step);
    if (this.comment) out["X-Router-Comment"] = asciiHeader(this.comment);
    // Only emit W3C Trace Context when the trace id is a valid 32-hex field; otherwise
    // X-Router-Trace is the authoritative correlation id.
    if (HEX32.test(this.trace)) out.traceparent = this.traceparent;
    if (this.parentSpan) out["X-Router-Parent-Span"] = asciiHeader(this.parentSpan);
    return out;
  }

  /** Same activity and trace, different stage. */
  withStep(step) {
    return this.child({ step });
  }

  /** A new execution of the same job (use it for retries). */
  withTrace(trace = randomHex(16), overrides = {}) {
    return this.child({ ...overrides, trace: clean(trace, 64) || randomHex(16) });
  }

  withComment(comment) {
    return this.child({ comment });
  }

  /** New logical request with its own span; the parent span links it to this one. */
  child(overrides = {}) {
    const o = overrides || {};
    return new RouterContext({
      job: clean(o.job, 128) || this.job,
      trace: clean(o.trace ?? o.traceId, 64) || this.trace,
      step: clean(o.step, 64) || (o.step === "" ? "" : this.step),
      initiator: clean(o.initiator, 64) || this.initiator,
      comment: clean(o.comment, 500) || (o.comment === "" ? "" : this.comment),
      requestId: clean(o.requestId, 64) || randomHex(6),
      spanId: clean(o.spanId, 16) || randomHex(8),
      parentSpan: clean(o.parentSpan, 16) || this.spanId,
    });
  }

  toJSON() {
    return {
      job: this.job, trace: this.trace, step: this.step, initiator: this.initiator,
      comment: this.comment, requestId: this.requestId, spanId: this.spanId,
      parentSpan: this.parentSpan, traceparent: this.traceparent,
    };
  }
}

/**
 * One target activity. Every call, hop, note and job event carries this identity.
 * @param {object} [options]
 * @param {string} [options.job] e.g. "digest-2026-10-28"; defaults to initiator@UTC-date
 * @param {string} [options.trace] one execution of the job; defaults to a 32-hex id
 * @param {string} [options.step] stage label, e.g. "prefilter"
 * @param {string} [options.initiator] e.g. "tldr-digest"
 * @param {string} [options.comment] free text, stored but never used for routing
 */
function createContext(options = {}) {
  const o = options && typeof options === "object" ? options : {};
  const initiator = clean(o.initiator, 64);
  const day = new Date().toISOString().slice(0, 10);
  const base = {
    job: clean(o.job, 128) || `${initiator || "anonymous"}@${day}`,
    trace: clean(o.trace ?? o.traceId, 64) || randomHex(16),
    step: clean(o.step, 64),
    initiator,
    comment: clean(o.comment, 500),
    requestId: clean(o.requestId, 64) || randomHex(6),
    spanId: clean(o.spanId, 16) || randomHex(8),
    parentSpan: clean(o.parentSpan, 16),
  };
  if (o.traceparent) {
    const parsed = parseTraceparent(o.traceparent);
    if (parsed) {
      if (!o.trace && !o.traceId) base.trace = parsed.traceId;
      base.parentSpan = parsed.parentSpanId;
    }
  }
  return new RouterContext(base);
}

function toContext(value) {
  return value instanceof RouterContext ? value : createContext(value);
}

/** Auth + correlation headers, ready for fetch(). `extra` wins on conflicts. */
function headers(token, context, extra = {}) {
  const ctx = toContext(context);
  const out = { ...ctx.headers() };
  if (token) out["x-goog-api-key"] = String(token);
  for (const [key, value] of Object.entries(extra || {})) {
    if (value !== undefined && value !== null) out[key] = String(value);
  }
  return out;
}

function makeURL(base, path, query) {
  const root = String(base || "http://127.0.0.1:8787").replace(/\/+$/, "");
  const suffix = String(path || "/").startsWith("/") ? String(path) : `/${path}`;
  const url = new URL(root + suffix);
  for (const [key, value] of Object.entries(query || {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  return url;
}

class RouterHttpError extends Error {
  constructor(message, status, body, url) {
    super(message);
    this.name = "RouterHttpError";
    this.status = status;
    this.body = body;
    this.url = url;
  }
}

function pickFetch(fetchImpl) {
  const impl = fetchImpl || globalThis.fetch;
  if (typeof impl !== "function") {
    throw new Error("fetch is unavailable; pass a fetch implementation in options.fetch");
  }
  return impl;
}

async function readBody(response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

/** Low-level JSON request to a router bookkeeping endpoint. */
async function requestJSON(base, token, context, path, options = {}) {
  const o = options || {};
  const ctx = toContext(context);
  const url = makeURL(base, path, o.query);
  const hdr = headers(token, ctx, o.headers);
  let body;
  if (o.body !== undefined && o.body !== null) {
    hdr["content-type"] = hdr["content-type"] || "application/json";
    body = JSON.stringify(o.body);
  }
  const response = await pickFetch(o.fetch)(url, {
    method: o.method || (body ? "POST" : "GET"),
    headers: hdr,
    body,
    signal: o.signal,
  });
  const data = await readBody(response);
  if (!response.ok) {
    const detail = data && data.error && data.error.message ? data.error.message : JSON.stringify(data);
    throw new RouterHttpError(`gemini-router ${o.method || "GET"} ${path} → ${response.status}: ${detail}`,
      response.status, data, url.toString());
  }
  return data;
}

/**
 * fetch() wrapper for any proxy path (including /v1beta/...:generateContent).
 * Adds the token and correlation headers, JSON-encodes plain objects, returns the raw
 * Response so the caller can read provider statuses and x-router-* response headers.
 */
async function routerFetch(base, token, context, path, init = {}) {
  const { fetch: fetchImpl, query, ...rest } = init || {};
  const ctx = toContext(context);
  const hdr = new Headers(rest.headers || {});
  for (const [key, value] of Object.entries(headers(token, ctx))) hdr.set(key, value);
  let body = rest.body;
  const binary = body && (ArrayBuffer.isView(body) || body instanceof ArrayBuffer ||
    typeof body.arrayBuffer === "function" || typeof body.getReader === "function" ||
    (typeof FormData !== "undefined" && body instanceof FormData) ||
    (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams));
  if (body !== undefined && body !== null && typeof body === "object" && !binary) {
    body = JSON.stringify(body);
    if (!hdr.has("content-type")) hdr.set("content-type", "application/json");
  }
  return pickFetch(fetchImpl)(makeURL(base, path, query), { ...rest, headers: hdr, body });
}

function jobStart(base, token, context, options = {}) {
  const ctx = toContext(context);
  const o = options || {};
  return requestJSON(base, token, ctx, "/v1/job", {
    method: "POST", fetch: o.fetch, signal: o.signal,
    body: {
      action: "start", job: o.job || ctx.job, trace: o.trace || ctx.trace,
      initiator: o.initiator || ctx.initiator, step: o.step || ctx.step,
      comment: o.comment || ctx.comment || "",
    },
  });
}

function jobFinish(base, token, context, options = {}) {
  const ctx = toContext(context);
  const o = options || {};
  return requestJSON(base, token, ctx, "/v1/job", {
    method: "POST", fetch: o.fetch, signal: o.signal,
    body: {
      action: "finish", job: o.job || ctx.job,
      status: o.status === "failed" ? "failed" : "done",
      initiator: o.initiator || ctx.initiator, step: o.step || ctx.step,
      comment: o.comment || "",
    },
  });
}

function jobs(base, token, context, options = {}) {
  const o = typeof options === "number" ? { n: options } : (options || {});
  return requestJSON(base, token, context, "/v1/jobs", {
    query: { n: o.n || 20 }, fetch: o.fetch, signal: o.signal,
  });
}

function jobGet(base, token, context, options = {}) {
  const ctx = toContext(context);
  const o = typeof options === "string" ? { job: options } : (options || {});
  return requestJSON(base, token, ctx, "/v1/job", {
    query: { id: o.job || ctx.job, n: o.n || 50 }, fetch: o.fetch, signal: o.signal,
  });
}

function note(base, token, context, comment, options = {}) {
  const ctx = toContext(context);
  const o = options || {};
  return requestJSON(base, token, ctx, "/v1/note", {
    method: "POST", fetch: o.fetch, signal: o.signal,
    body: {
      comment: comment || ctx.comment, job: o.job || ctx.job,
      initiator: o.initiator || ctx.initiator, step: o.step || ctx.step,
    },
  });
}

function notes(base, token, context, options = {}) {
  const ctx = toContext(context);
  const o = typeof options === "number" ? { n: options } : (options || {});
  return requestJSON(base, token, ctx, "/v1/notes", {
    query: { n: o.n || 20, job: o.job || ctx.job }, fetch: o.fetch, signal: o.signal,
  });
}

function calls(base, token, context, options = {}) {
  const o = typeof options === "number" ? { n: options } : (options || {});
  return requestJSON(base, token, context, "/v1/calls", {
    query: {
      n: o.n || 50, job: o.job, trace: o.trace, step: o.step,
      initiator: o.initiator, model: o.model, status: o.status,
      source: o.source, request_id: o.requestId || o.request_id,
    },
    fetch: o.fetch, signal: o.signal,
  });
}

function ttsFile(base, token, context, uploadId, options = {}) {
  const o = options || {};
  const ctx = toContext(context);
  return requestJSON(base, token, ctx, "/v1/tts/file", {
    method: "POST", fetch: o.fetch, signal: o.signal,
    body: {
      upload_id: uploadId,
      voice: o.voice,
      style: o.style,
      wait_for_minute_limits: o.waitForMinuteLimits,
      request_id: o.requestId || ctx.requestId,
      wait_seconds: o.waitSeconds === undefined ? 25 : o.waitSeconds,
    },
  });
}

function ttsStatus(base, token, context, requestId, options = {}) {
  const path = `/v1/tts/${encodeURIComponent(requestId)}`;
  return requestJSON(base, token, context, path, {
    fetch: options.fetch, signal: options.signal,
  });
}

function ttsDownload(base, token, context, requestId, options = {}) {
  const path = `/v1/tts/${encodeURIComponent(requestId)}/download`;
  return routerFetch(base, token, context, path, {
    fetch: options.fetch, signal: options.signal,
  });
}

async function health(base, options = {}) {
  const o = options || {};
  const url = makeURL(base, "/healthz");
  const response = await pickFetch(o.fetch)(url, { signal: o.signal });
  const data = await readBody(response);
  if (!response.ok) throw new RouterHttpError(`healthz → ${response.status}`, response.status, data, url.toString());
  return data;
}

module.exports = {
  CLIENT_CONTEXT_HEADERS,
  RouterContext,
  RouterHttpError,
  createContext,
  headers,
  requestJSON,
  routerFetch,
  jobStart,
  jobFinish,
  jobs,
  jobGet,
  note,
  notes,
  calls,
  ttsFile,
  ttsStatus,
  ttsDownload,
  health,
  makeTraceparent,
  parseTraceparent,
  makeURL,
  randomHex,
};
