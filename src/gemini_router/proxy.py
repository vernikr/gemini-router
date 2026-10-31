"""Native-Gemini pass-through proxy (SPEC §11).

`GOOGLE_GEMINI_BASE_URL` for the Google Gen AI SDK: it lets a client that speaks the
*native* Gemini API (github.com/mikesmitty/file-search and its
`google.golang.org/genai` SDK) run against this router's key pool instead of a single
key, with the client's own token as the only credential it ever sees.

The SDK builds its URLs and reacts to provider-specific status codes, headers and
error bodies. Re-serialising any of it breaks the contract — `sizeBytes` arrives as a
string and the SDK's struct tags demand exactly that — so this layer forwards requests
and responses byte-for-byte and adds only what the router is for:

    * client authentication (the client's key never reaches Google);
    * upstream key selection, with File Search resources pinned to the project that
      owns them (filesearch.py);
    * a resumable-upload session bound to that key, so a continuation cannot bypass
      authentication or land in another project;
    * quota accounting and job/trace correlation into the shared ledger.

Two properties of the SDK shape the design and were measured, not assumed:

1. `GOOGLE_GEMINI_BASE_URL` must be a bare origin (scheme://host[:port]). The SDK
   prepends the API version itself, and while it does copy the base URL's scheme+host
   onto the upload URL Google hands back, it keeps only the PATH — so a base URL with a
   path prefix silently loses it on the upload continuation.
2. Chunks are POSTed in 8 MiB pieces with `X-Goog-Upload-Offset`; the answer must carry
   `X-Goog-Upload-Status`. Those headers are forwarded untouched.

HTTP is stdlib `http.server` on purpose: the library stays dependency-light
(httpx/pydantic/rich/stdlib) and the whole service fits the memory budget of the box it
runs on.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

import httpx

from .config import TTS_MODEL, Config, estimate_tokens, key_id
from .context import (
    CLIENT_CONTEXT_HEADERS,
    RequestContext,
    audit_line,
    make_traceparent,
    router_version,
)
from .feedback import ProviderSignal, QuotaTruth, parse_provider_signal
from .filesearch import (
    INTERACTIONS_CREATE,
    FileSearchRouting,
    Selection,
    UploadTickets,
    allowed_path,
    interactions_model,
    interactions_text,
    is_create,
    is_upload_chunk,
    is_upload_start,
    model_of,
    resource_of,
    set_interactions_model,
)
from .ledger import FILE_ROW_MODEL, Ledger

log = logging.getLogger("gemini_router.proxy")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "content-length",
              "host", "accept-encoding", "date", "server", "content-encoding"}
# The client's credential is the router's token, never an upstream key: it is consumed
# here and must not travel a hop further.
CLIENT_AUTH_HEADERS = {"x-goog-api-key", "authorization", "cookie"}
# Router-only metadata (identity, job/trace, annotations) is stripped before upstream:
# a free-form comment or trace header must never change a provider request.
CHUNK = 1 << 20          # 1 MiB: streamed, so a 2 GB upload never lands in RAM twice
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _usage_counts(payload: bytes) -> tuple[int | None, int | None]:
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None, None
    usage = data.get("usage") or data.get("usageMetadata") or {}
    tin = (usage.get("input_tokens") or usage.get("promptTokenCount") or
           usage.get("inputTokens"))
    tout = (usage.get("output_tokens") or usage.get("candidatesTokenCount") or
            usage.get("outputTokens"))
    return (int(tin) if tin is not None else None,
            int(tout) if tout is not None else None)


def google_error(status: int, message: str, *, code: int | None = None,
                 detail: str | None = None) -> bytes:
    """An error body the Gen AI SDK parses (`newAPIError` reads `{"error": {...}}`)."""
    payload = {"error": {"code": code or status, "message": message,
                         "status": _STATUS_NAME.get(status, "UNKNOWN")}}
    if detail:
        payload["error"]["details"] = [{"@type": "type.googleapis.com/google.rpc.DebugInfo",
                                        "detail": detail}]
    return json.dumps(payload).encode()


_STATUS_NAME = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED",
                404: "NOT_FOUND", 409: "ALREADY_EXISTS", 413: "RESOURCE_EXHAUSTED",
                429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 502: "UNAVAILABLE",
                503: "UNAVAILABLE", 504: "DEADLINE_EXCEEDED"}


class GeminiProxy:
    """The pass-through service: one process, one port, the shared ledger."""

    #: Hard cap on further candidates one request may walk after a live 429/503.
    #: The effective bound is every key × every tier (see `_walk_bound`) — the day
    #: the whole chain is drained out-of-band, the walk must still reach the
    #: overflow tier within ONE request; a failed hop costs ~0.3 s.
    WALK_RETRIES = 16
    #: A 503 is the model overloaded right now, not a budget verdict — cool the pair
    #: briefly so the walk moves on, and the pair comes back on its own.
    OVERLOAD_COOLDOWN_S = 60.0

    def __init__(self, cfg: Config, *, ledger: Ledger | None = None,
                 token: str | None = None, origin: str | None = None):
        if not cfg.api_keys:
            raise ValueError("no API keys: set GEMINI_API_KEYS or GEMINI_API_KEY_1..10")
        self.cfg = cfg
        self.ledger = ledger or Ledger(cfg.state_path)
        self.token = token if token is not None else os.environ.get(
            cfg.proxy.token_env, "")
        if not self.token:
            raise ValueError(f"{cfg.proxy.token_env} is empty: the proxy refuses to "
                             "start without a client token (it is the only thing "
                             "standing between the port and the key pool)")
        self.origin = origin or f"http://{cfg.proxy.bind}:{cfg.proxy.port}"
        self.tickets = UploadTickets(self.token, self.origin)
        self.routing = FileSearchRouting(cfg, self.ledger)
        self.truth = QuotaTruth(self.ledger, cfg)
        self.version = router_version()
        self._client = httpx.Client(timeout=cfg.proxy.request_timeout_s)

    # ------------------------------------------------------------------ serving
    def server(self) -> ThreadingHTTPServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "gemini-router-proxy"
            sys_version = ""

            def log_message(self, fmt, *args):      # journald captures the daemon's stdout
                log.debug("http %s", fmt % args)

            def _dispatch(self):
                outer.handle(self)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _dispatch

        httpd = ThreadingHTTPServer((self.cfg.proxy.bind, self.cfg.proxy.port), Handler)
        httpd.daemon_threads = True
        # bind=0 (tests, parallel deploys): the upload URLs must point at the port that
        # was actually taken, so the session ticket is re-sealed against the real one.
        host, port = httpd.server_address[:2]
        self.origin = f"http://{host}:{port}"
        self.tickets = UploadTickets(self.token, self.origin)
        return httpd

    def serve_forever(self) -> None:
        httpd = self.server()
        log.info(audit_line(
            "startup", v=self.version, component="proxy",
            endpoint=f"http://{self.cfg.proxy.bind}:{self.cfg.proxy.port}",
            upstream=self.cfg.base_url, keys=len(self.cfg.api_keys),
            overflow=self.cfg.overflow_default, policy=self.cfg.proxy.model_policy,
            state=self.ledger.path, initiator="system", job="system@startup",
            trace="system@startup",
            comment="gemini-router proxy started"))
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
            self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ handling
    def handle(self, h: BaseHTTPRequestHandler) -> None:
        """One request. Every failure is answered in the provider's error shape: a
        machine client must never have to parse a traceback."""
        started = time.time()
        path = query = ""
        try:
            u = urlparse(h.path)
            path, query, method = u.path, u.query, h.command
            if path == "/healthz":
                return self._send(h, 200, b"application/json",
                                  json.dumps({"ok": True, "service": "gemini-router-proxy",
                                              "version": self.version,
                                              "keys": len(self.cfg.api_keys)}).encode(), {})
            if path.startswith("/.well-known/") or path in ("/register", "/authorize",
                                                            "/token"):
                # JSON, never text/plain: clients probe this ladder and parse it.
                return self._send(h, 404, b"application/json", google_error(
                    404, "static token auth; no authorization server"), {})

            if not self._authorized(h):
                log.warning("proxy: rejected unauthenticated %s %s", method, path)
                return self._send(h, 401, b"application/json", google_error(
                    401, f"unauthorized: send the router token in x-goog-api-key or "
                         f"Authorization: Bearer (see {self.cfg.proxy.token_env})"),
                    {"WWW-Authenticate": 'Bearer realm="gemini-router"'})
            ctx = RequestContext.from_headers(
                h.headers, default_initiator=self.cfg.proxy.default_initiator,
                source="filesearch")
            # Annotations are local-only: they write one audit row and change nothing
            # else. They deliberately bypass allowed_path() and never reach Google.
            if path == "/v1/note" and method == "POST":
                return self._handle_note(h, ctx)
            if path == "/v1/notes" and method == "GET":
                return self._handle_notes(h, ctx, query)
            if path == "/v1/job" and method == "POST":
                return self._handle_job(h, ctx)
            if path == "/v1/jobs" and method == "GET":
                return self._handle_jobs(h, ctx, query)
            if path == "/v1/job" and method == "GET":
                return self._handle_job_get(h, ctx, query)
            if path == "/v1/calls" and method == "GET":
                return self._handle_calls(h, ctx, query)

            if not allowed_path(path):
                return self._send(h, 404, b"application/json", google_error(
                    404, f"{path} is not served by this pass-through: only File Search, "
                         "Files API, models and resumable upload"), {})

            self._proxy(h, path, query, method, started, ctx)
        except Exception as exc:                     # noqa: BLE001 — never 200-with-error
            log.exception("proxy: unhandled failure on %s %s", h.command, path)
            try:
                self._send(h, 500, b"application/json", google_error(
                    500, "proxy failure", detail=f"{type(exc).__name__}"), {})
            except OSError:
                pass

    # ------------------------------------------------------------------ notes
    def _handle_note(self, h, ctx: RequestContext) -> None:
        """Record a free-form comment. No upstream call, no quota, no side effect."""
        length = int(h.headers.get("content-length") or 0)
        raw = h.rfile.read(min(length, 64 << 10)) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        comment = str(data.get("comment") or ctx.comment or "")[:2000].strip()
        if not comment:
            return self._send(h, 400, b"application/json",
                              google_error(400, 'note needs a non-empty "comment"'), {})
        initiator = str(data.get("initiator") or ctx.initiator or "")[:64]
        job = str(data.get("job") or ctx.job or "")[:128]
        trace = str(data.get("trace") or ctx.trace_id or "")[:64]
        step = str(data.get("step") or ctx.step or "")[:64]
        ctx.job, ctx.trace_id, ctx.step = job, trace, step
        note_id = self.ledger.add_note(comment, initiator=initiator,
                                       version=self.version, source="http",
                                       job=job or None, trace_id=trace or job,
                                       step=step or None)
        fields = ctx.log_fields()
        fields.update(initiator=initiator or "-", note_id=note_id, comment=comment)
        log.info(audit_line("note", **fields))
        self._send(h, 200, b"application/json",
                   json.dumps({"ok": True, "note_id": note_id, "version": self.version,
                               "initiator": initiator, "job": job}).encode(), {})

    def _handle_notes(self, h, ctx: RequestContext, query: str) -> None:
        q = dict(parse_qsl(query))
        try:
            n = max(1, min(int(q.get("n", "20")), 200))
        except ValueError:
            n = 20
        self._send(h, 200, b"application/json",
                   json.dumps({"ok": True, "version": self.version,
                               "notes": self.ledger.notes_tail(
                                   n, job=q.get("job"))}).encode(), {})

    # -------------------------------------------------------------------- jobs
    @staticmethod
    def _body(h, cap: int = 64 << 10) -> dict:
        length = int(h.headers.get("content-length") or 0)
        raw = h.rfile.read(min(length, cap)) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _handle_job(self, h, ctx: RequestContext) -> None:
        """Start or finish a target activity. Local bookkeeping only."""
        data = self._body(h)
        job = str(data.get("job") or ctx.job or "")[:128].strip()
        action = str(data.get("action") or "start").lower()
        if not job:
            return self._send(h, 400, b"application/json",
                              google_error(400, 'job needs a non-empty "job"'), {})
        comment = str(data.get("comment") or ctx.comment or "")[:2000]
        initiator = str(data.get("initiator") or ctx.initiator or "")[:64]
        status = str(data.get("status") or "done").lower()
        ctx.job = job
        ctx.trace_id = str(data.get("trace") or ctx.trace_id or "")[:64] or job
        ctx.step = str(data.get("step") or ctx.step or "")[:64]
        if action in ("start", "begin"):
            result = self.ledger.job_start(job, initiator=initiator,
                                           version=self.version, comment=comment,
                                           trace_id=ctx.trace_id, step=ctx.step or None)
        elif action in ("finish", "end", "stop"):
            if status not in ("done", "failed"):
                status = "done"
            result = self.ledger.job_finish(job, status=status, initiator=initiator,
                                            version=self.version, comment=comment,
                                            step=ctx.step or None)
        else:
            return self._send(h, 400, b"application/json", google_error(
                400, "action must be start or finish"), {})
        fields = ctx.log_fields()
        fields.update(job_action=action, job_status=result["status"],
                      comment=comment or "-")
        log.info(audit_line("job", **fields))
        self._send(h, 200, b"application/json",
                   json.dumps({"ok": True, "job": job, "action": action,
                               "version": self.version,
                               "summary": result}, default=str).encode(), {})

    def _handle_jobs(self, h, ctx: RequestContext, query: str) -> None:
        q = dict(parse_qsl(query))
        try:
            n = max(1, min(int(q.get("n", "20")), 200))
        except ValueError:
            n = 20
        self._send(h, 200, b"application/json",
                   json.dumps({"ok": True, "version": self.version,
                               "jobs": self.ledger.jobs_tail(n)}, default=str).encode(), {})

    def _handle_job_get(self, h, ctx: RequestContext, query: str) -> None:
        q = dict(parse_qsl(query))
        job = q.get("id") or q.get("job") or ctx.job
        summary = self.ledger.job_summary(job) if job else None
        if not summary:
            return self._send(h, 404, b"application/json",
                              google_error(404, f"job {job!r} not found"), {})
        try:
            n = max(1, min(int(q.get("n", "50")), 500))
        except ValueError:
            n = 50
        self._send(h, 200, b"application/json", json.dumps({
            "ok": True, "version": self.version, "job": job, "summary": summary,
            "events": self.ledger.notes_tail(n, job=job),
            "calls": self.ledger.job_calls(job, n)}, default=str).encode(), {})

    def _handle_calls(self, h, ctx: RequestContext, query: str) -> None:
        """Read the audit rows of one job/trace/step without touching the provider."""
        q = dict(parse_qsl(query))
        try:
            n = max(1, min(int(q.get("n", "50")), 500))
        except ValueError:
            n = 50
        rows = self.ledger.calls_tail(
            n, model=q.get("model"), status=q.get("status"), source=q.get("source"),
            request_id=q.get("request_id"), initiator=q.get("initiator"),
            job=q.get("job"), trace_id=q.get("trace"), step=q.get("step"))
        self._send(h, 200, b"application/json",
                   json.dumps({"ok": True, "version": self.version, "calls": rows},
                              default=str).encode(), {})

    def _local_throttle(self, selection: Selection,
                        ctx: RequestContext) -> tuple[int, dict, bytes, None]:
        """Answer 429 locally when provider truth says the whole pool is blocked."""
        retry = max(1, int(selection.retry_after_s or 60))
        log.info(audit_line("pool_throttled", **ctx.log_fields(),
                            retry_after_s=retry, state=selection.state))
        return (429, {"Retry-After": str(retry)},
                google_error(429, "all router key/model pairs are quota-blocked by the "
                                  "provider; retry after the advertised delay",
                             detail=f"state={selection.state}"), None)

    # --------------------------------------------------------------------- steps
    def _authorized(self, h: BaseHTTPRequestHandler) -> bool:
        import hmac as _hmac
        supplied = (h.headers.get("x-goog-api-key")
                    or (h.headers.get("Authorization") or "").removeprefix("Bearer "))
        return bool(supplied) and _hmac.compare_digest(supplied, self.token)

    def _proxy(self, h, path: str, query: str, method: str, started: float,
               ctx: RequestContext | None = None) -> None:
        ctx = ctx or RequestContext.from_headers(
            h.headers, default_initiator=self.cfg.proxy.default_initiator,
            source="filesearch")
        length = int(h.headers.get("content-length") or 0)
        limit = self.cfg.proxy.body_inspect_bytes
        cfg = self.cfg

        # Small bodies are buffered (that is what lets a generateContent body name its
        # store); large ones — upload chunks — are streamed straight through.
        if length and length <= limit:
            body, stream = h.rfile.read(length), None
        elif length:
            body, stream = None, self._stream(h, length)
        else:
            body, stream = b"", None

        # --- continuation of a resumable upload: authenticate and pin by ticket ----
        if is_upload_chunk(path, query):
            # parse_qsl, not a hand-rolled split: upload ids carry ':' and '+', which
            # urlencode escapes and a naive split would compare un-decoded.
            q = dict(parse_qsl(query))
            kid = q.get("router_k", "")
            if not self.tickets.verify(q.get("upload_id", ""), kid, q.get("router_s", "")):
                log.warning("proxy: rejected upload continuation with a bad session")
                return self._send(h, 403, b"application/json", google_error(
                    403, "invalid upload session: resume through the URL this proxy "
                         "returned from the upload start"), {})
            key = self.routing._resolve(kid)
            if key is None:
                return self._send(h, 500, b"application/json", google_error(
                    500, "upload session names a key that is no longer in the pool"), {})
            resource, pinned, upstream_query = None, True, self.tickets.strip(query)
        else:
            if is_upload_start(path, method, query):
                # The store is named in the upload path itself, so affinity is known
                # before the bytes move and the session is bound to the right project.
                resource = resource_of(path, None)
            else:
                resource = resource_of(path, body)
            kid, resource, pinned = self.routing.select(path, query, body)
            key = self.routing._resolve(kid)
            upstream_query = query
            if key is None:
                return self._send(h, 500, b"application/json", google_error(
                    500, "no configured key matches the selected key id"), {})

        model = model_of(path)
        # The Interactions API names the model in the BODY, not the path — the same
        # policy walk applies, only the rewrite lands in different bytes.
        model_in_body = (path == INTERACTIONS_CREATE and method == "POST"
                         and body is not None)
        if model_in_body and not model:
            model = interactions_model(body)
        requested_model = model
        tts_tokens = estimate_tokens(interactions_text(body)) if model == TTS_MODEL else 0
        # Keep a small allowance for speech metadata and the selected voice in the
        # serialized request; usage metadata corrects the estimate after the response.
        if model == TTS_MODEL and body:
            try:
                request_data = json.loads(body)
                tts_tokens += estimate_tokens(json.dumps(
                    {"generation_config": request_data.get("generation_config")},
                    ensure_ascii=False))
            except (ValueError, TypeError):
                pass
        selection: Selection | None = None
        if model and (model == TTS_MODEL or cfg.proxy.model_policy != "passthrough"):
            # Key and model come from the shared selection walk (SPEC §11.6): resource
            # affinity pins the key and only the model walks; an unbound model call
            # (plain generateContent/countTokens) walks both — the pool rotates by
            # remaining budget instead of riding the File Search pin.
            selection = self.routing.choose(model, kid if resource else None,
                                            est_tokens=tts_tokens)
            new_kid, chosen = selection.key_id, selection.model
            if chosen and chosen != model:
                if model_in_body:
                    body = set_interactions_model(body, chosen)
                    length = len(body)
                else:
                    path = path.replace(f"/models/{model}:", f"/models/{chosen}:", 1)
                model = chosen
            if not resource and new_kid != kid:
                rotated = self.routing._resolve(new_kid)
                if rotated is not None:
                    kid, key = new_kid, rotated

        headers = self._upstream_headers(h)
        headers["x-goog-api-key"] = key
        url = self._upstream_url(path, upstream_query)
        spending = method == "POST" and (path.endswith(":generateContent")
                                         or path == INTERACTIONS_CREATE)

        hop = 0

        def attempt() -> tuple[int, dict, bytes, ProviderSignal | None]:
            # Every upstream try is planned and resolved: a retried attempt spent real
            # provider quota and must not vanish from the ledger. Non-spending kinds
            # (store listings, uploads) are recorded but excluded from every gate.
            # Identity (version, initiator, job, trace, step, hop) rides on the row so
            # the whole activity can be reconstructed from one query.
            nonlocal hop
            this_hop = hop
            hop += 1
            row_id = self.ledger.plan(
                key_id=key_id(key), model=model or FILE_ROW_MODEL,
                kind="single" if spending else "filesearch", est_tokens=tts_tokens,
                request_id=ctx.request_id, source=ctx.initiator or "filesearch",
                initiator=ctx.initiator or "filesearch", comment=ctx.comment or None,
                version=self.version, job=ctx.job, trace_id=ctx.trace_id,
                step=ctx.step or None, hop=this_hop, requested_model=requested_model,
                request_bytes=length if body is not None else None,
                method=method, path=path, parent_span=ctx.parent_span or None
            ) if (spending or cfg.proxy.audit) else None
            st, hdrs_out, pl, signal = self._forward(
                method, url, headers, body, stream, length, path, resource, pinned,
                kid, key, model=model, ctx=ctx)
            if row_id is not None:
                input_tokens, output_tokens = (
                    _usage_counts(pl) if model == TTS_MODEL else (None, None))
                self.ledger.resolve(
                    row_id, "ok" if st < 400 else self._status_name(st),
                    http_status=st, tokens_in=input_tokens, tokens_out=output_tokens,
                    latency_ms=int((time.time() - started) * 1000),
                    quota_error=signal.detail if signal and signal.blocks_pair else None,
                    retry_after_s=signal.retry_after_s if signal else None,
                    quota_id=signal.quota_id if signal else None,
                    signal_kind=signal.kind if signal and signal.blocks_pair else None,
                    response_bytes=len(pl),
                    provider_reason=signal.reason if signal and signal.blocks_pair else None)
            return st, hdrs_out, pl, signal

        # --- circuit breaker: provider truth already says the pool is out -----------
        # Concurrent callers share the provider verdict instead of each re-walking
        # the same blocked pairs.
        if (spending and model and stream is None and cfg.proxy.pool_circuit_breaker
                and selection is not None and selection.state == "throttled"):
            status, out_headers, payload, _ = self._local_throttle(selection, ctx)
        else:
            if (spending and model and stream is None and cfg.proxy.walk_wait
                    and selection is not None and selection.state == "wait"
                    and selection.wait_s <= cfg.wait.max_s):
                time.sleep(min(selection.wait_s, cfg.wait.max_s))
            status, out_headers, payload, _ = attempt()

        # --- keep walking on a live quota/overload answer (SPEC §11.6 / §12) -------
        # Every 429/503 is applied to the ledger BEFORE the next hop (`_observe`), so
        # the pair that just refused is hard-skipped for this request (`visited`) and
        # for later ones (`pair_state`). Forwarding the first failure would push this
        # discovery onto every caller. Bounded; only buffered model calls are retried.
        if spending and model and stream is None and (not resource or model_in_body):
            visited = {(kid, model)}
            for _ in range(self._walk_bound()):
                if status not in (429, 503):
                    break
                sel = self.routing.choose(model, kid if resource else None, exclude=visited,
                                          est_tokens=tts_tokens)
                if sel.state == "throttled" and cfg.proxy.pool_circuit_breaker:
                    status, out_headers, payload, _ = self._local_throttle(sel, ctx)
                    break
                if sel.state == "exhausted":
                    break                         # no new candidate from the shared walk
                if sel.state == "wait":
                    if cfg.proxy.walk_wait and sel.wait_s <= cfg.wait.max_s:
                        time.sleep(min(sel.wait_s, cfg.wait.max_s))
                    else:
                        status, out_headers, payload, _ = self._local_throttle(sel, ctx)
                        break
                next_kid, next_model = sel.key_id, sel.model
                next_key = self.routing._resolve(next_kid)
                if next_key is None or (next_kid, next_model) in visited:
                    break                         # the walk has nothing new to offer
                visited.add((next_kid, next_model))
                if next_model and next_model != model:
                    if model_in_body:
                        body = set_interactions_model(body, next_model)
                        length = len(body)
                    else:
                        path = path.replace(f"/models/{model}:", f"/models/{next_model}:", 1)
                    model = next_model
                kid, key = next_kid, next_key
                headers["x-goog-api-key"] = key
                url = self._upstream_url(path, upstream_query)
                status, out_headers, payload, _ = attempt()

        # --- learn what a create produced, so the next call needs no probing -------
        if is_create(path, method):
            self.routing.learn(path, method, status, payload, kid)
            self.routing.learn_interaction(path, method, status, payload, kid)

        if cfg.proxy.audit:
            log.info(audit_line(
                "request", **ctx.log_fields(), method=method, path=path,
                status=status, key=kid, resource=resource or "-",
                pinned=int(bool(pinned)), bytes=length,
                latency_ms=int((time.time() - started) * 1000),
                model=model or "-", requested_model=requested_model or "-",
                hop=max(0, hop - 1), response_bytes=len(payload)))
        if model:
            # Who actually served a model call — so a caller's own run log can record
            # the pair without correlating against this proxy's audit stream by time.
            out_headers["x-router-model"] = model
            out_headers["x-router-key-id"] = kid
        out_headers["x-router-version"] = self.version
        out_headers["x-router-job"] = ctx.job
        out_headers["x-router-trace"] = ctx.trace_id
        out_headers["x-router-request-id"] = ctx.request_id
        out_headers["x-router-traceparent"] = make_traceparent(ctx.trace_id, ctx.span_id)
        self._send(h, status, out_headers.pop("content-type", b"application/json"),
                   payload, out_headers)

    def _forward(self, method, url, headers, body, stream, length, path, resource,
                 pinned, kid, key, model=None, ctx: RequestContext | None = None
                 ) -> tuple[int, dict, bytes, ProviderSignal | None]:
        """Send upstream, retrying only where that is safe (SPEC §11.7).

        A non-idempotent POST (create a store, import a file) is never repeated once it
        may have taken effect, and a consumed body stream cannot be replayed at all —
        so both get exactly one attempt. Idempotent reads of a resource whose owner is
        unknown may try the other keys once, which is also how a lost affinity heals.

        Every 429/503 is handed to `QuotaTruth` *with the real upstream headers*,
        including `Retry-After` (delta-seconds or HTTP-date).
        """
        ctx = ctx or RequestContext(source="filesearch")
        attempts = [(url, headers, body, stream, kid, key)]
        if method in ("GET", "DELETE", "HEAD") and not pinned and resource:
            for other in self.routing.probe_order(resource, model or model_of(path))[1:]:
                k = self.routing._resolve(other)
                if k is not None:
                    attempts.append((url, {**headers, "x-goog-api-key": k}, body,
                                     None, other, k))
        Result = tuple[int, dict, bytes, ProviderSignal | None]
        last: Result = (502, {}, google_error(502, "upstream unreachable"), None)
        for i, (u, hdrs, b, st, cur_kid, _cur_key) in enumerate(attempts):
            try:
                status, out, payload = self._send_upstream(
                    method, u, hdrs, b, st, length, cur_kid)
            except httpx.HTTPError as exc:
                log.warning("proxy: upstream transport error on %s: %s", path, exc)
                last = (502, {}, google_error(
                    502, f"upstream unreachable: {type(exc).__name__}"), None)
                if st is not None or b:
                    return last
                continue
            signal = self._observe(status, out, payload, cur_kid,
                                   model or model_of(path), ctx)
            if status < 400 and (model or model_of(path)):
                # Truth is symmetric: a success clears any earlier provider block.
                self.ledger.clear_pair_state(cur_kid, model or model_of(path))
            if status in (403, 404) and resource and i + 1 < len(attempts):
                continue                                  # another project may own it
            if status == 429:
                if not pinned and i + 1 < len(attempts):
                    continue
                last = (status, out, payload, signal)
                continue
            if resource and status < 400 and not pinned:
                self.ledger.fs_bind(resource, cur_kid)     # self-healed: remember it
            return status, out, payload, signal
        return last

    def _observe(self, status: int, out: dict, payload: bytes, kid: str,
                 model: str | None, ctx: RequestContext) -> ProviderSignal | None:
        """Fold one provider answer into the shared truth (SPEC §12)."""
        if not model or status not in (429, 502, 503):
            return None
        signal = parse_provider_signal(status, out, payload, self.cfg)
        if signal.blocks_pair:
            self.truth.apply(signal, key_id=kid, model=model, context=ctx)
        return signal

    def _send_upstream(self, method, url, headers, body, stream, length,
                       kid) -> tuple[int, dict, bytes]:
        content: object = b"" if not length else (body if body is not None else stream)
        req_headers = dict(headers)
        if length:
            req_headers["content-length"] = str(length)
        resp = self._client.request(method, url, content=content, headers=req_headers)
        out = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
        payload = resp.content
        # Hand the upload URL back pointing at this proxy, stamped with the session.
        if "x-goog-upload-url" in out:
            out["x-goog-upload-url"] = self.tickets.rewrite(out["x-goog-upload-url"], kid)
        return resp.status_code, out, payload

    def _walk_bound(self) -> int:
        """Every key x every tier, capped: the walk must be able to cross the whole
        chain into overflow when the entire chain is drained out-of-band."""
        tiers = len(self.cfg.chain) + len(self.cfg.overflow_tiers)
        return min(self.WALK_RETRIES, max(1, len(self.cfg.api_keys) * tiers))

    # ------------------------------------------------------------------ plumbing
    def _upstream_headers(self, h: BaseHTTPRequestHandler) -> dict:
        return {k: v for k, v in h.headers.items()
                if k.lower() not in HOP_BY_HOP
                and k.lower() not in CLIENT_AUTH_HEADERS
                and k.lower() not in CLIENT_CONTEXT_HEADERS}

    def _upstream_url(self, path: str, query: str) -> str:
        """Route the request to the configured upstream, keeping the provider's split
        between the API host and the upload host."""
        if path.startswith("/upload/v1beta/"):
            base = self.cfg.upload_base_url.rstrip("/")
            rest = path[len("/upload/v1beta/"):]
        elif path.startswith("/v1beta/"):
            base = self.cfg.base_url.rstrip("/")
            rest = path[len("/v1beta/"):]
        else:
            base, rest = self.cfg.base_url.rstrip("/"), path.lstrip("/")
        return f"{base}/{rest}" + (f"?{query}" if query else "")

    @staticmethod
    def _stream(h: BaseHTTPRequestHandler, length: int) -> Iterator[bytes]:
        remaining = length
        while remaining > 0:
            block = h.rfile.read(min(CHUNK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block

    @staticmethod
    def _status_name(status: int) -> str:
        if status == 429:
            return "quota"
        if status >= 500:
            return "http5xx"
        return "http4xx"

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, ctype: bytes, body: bytes,
              headers: dict) -> None:
        h.send_response(status)
        h.send_header("Content-Type", ctype.decode() if isinstance(ctype, bytes) else ctype)
        for k, v in headers.items():
            if k.lower() not in HOP_BY_HOP and k.lower() != "content-type":
                h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if body and h.command != "HEAD":
            h.wfile.write(body)


def serve(cfg: Config | None = None, *, config_path: str | None = None) -> None:
    """CLI entry: `gemini-router proxy`."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    GeminiProxy(cfg or Config.load(config_path)).serve_forever()
