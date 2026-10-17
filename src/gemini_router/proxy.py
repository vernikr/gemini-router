"""Native-Gemini pass-through proxy (SPEC §11).

`GOOGLE_GEMINI_BASE_URL` for the Google Gen AI SDK: it lets a client that speaks the
*native* Gemini API (github.com/mikesmitty/file-search and its
`google.golang.org/genai` SDK) run against this router's key pool instead of a single
key, with the client's own token as the only credential it ever sees.

Why a pass-through and not another `/v1/complete`-shaped endpoint: the SDK builds the
URLs itself and reacts to provider-specific status codes, headers and error bodies.
Re-serialising any of it breaks the contract — `sizeBytes` arrives as a string and the
SDK's struct tags demand exactly that — so this layer forwards requests and responses
byte-for-byte and adds only what the router is for:

    * client authentication (the client's key never reaches Google);
    * upstream key selection, with File Search resources pinned to the project that
      owns them (filesearch.py);
    * a resumable-upload session bound to that key, so a continuation cannot bypass
      authentication or land in another project;
    * quota accounting into the shared ledger.

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

from .config import Config, key_id
from .errors import GeminiHTTPError
from .filesearch import (
    INTERACTIONS_CREATE,
    FileSearchRouting,
    UploadTickets,
    allowed_path,
    interactions_model,
    is_create,
    is_upload_chunk,
    is_upload_start,
    model_of,
    resource_of,
    set_interactions_model,
)
from .gemini import GeminiClient
from .ledger import FILE_ROW_MODEL, Ledger

log = logging.getLogger("gemini_router.proxy")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "content-length",
              "host", "accept-encoding", "date", "server", "content-encoding"}
# The client's credential is the router's token, never an upstream key: it is consumed
# here and must not travel a hop further.
CLIENT_AUTH_HEADERS = {"x-goog-api-key", "authorization", "cookie"}
CHUNK = 1 << 20          # 1 MiB: streamed, so a 2 GB upload never lands in RAM twice
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


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
        log.info("gemini-router proxy on http://%s:%d -> %s (keys=%d)",
                 self.cfg.proxy.bind, self.cfg.proxy.port, self.cfg.base_url,
                 len(self.cfg.api_keys))
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
            if not allowed_path(path):
                return self._send(h, 404, b"application/json", google_error(
                    404, f"{path} is not served by this pass-through: only File Search, "
                         "Files API, models and resumable upload"), {})

            self._proxy(h, path, query, method, started)
        except Exception as exc:                     # noqa: BLE001 — never 200-with-error
            log.exception("proxy: unhandled failure on %s %s", h.command, path)
            try:
                self._send(h, 500, b"application/json", google_error(
                    500, "proxy failure", detail=f"{type(exc).__name__}"), {})
            except OSError:
                pass

    # --------------------------------------------------------------------- steps
    def _authorized(self, h: BaseHTTPRequestHandler) -> bool:
        import hmac as _hmac
        supplied = (h.headers.get("x-goog-api-key")
                    or (h.headers.get("Authorization") or "").removeprefix("Bearer "))
        return bool(supplied) and _hmac.compare_digest(supplied, self.token)

    def _proxy(self, h, path: str, query: str, method: str, started: float) -> None:
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
        if model and cfg.proxy.model_policy != "passthrough":
            # Key and model come from the shared selection walk (SPEC §11.6): resource
            # affinity pins the key and only the model walks; an unbound model call
            # (plain generateContent/countTokens) walks both — the pool rotates by
            # remaining budget instead of riding the File Search pin.
            new_kid, chosen = self.routing.choose(model, kid if resource else None)
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

        def attempt() -> tuple[int, dict, bytes]:
            # A generateContent call spends the model's quota, so EVERY upstream try is
            # planned and resolved like any other router call — a retried attempt spent
            # real provider quota and must not vanish from the ledger. Everything else
            # is recorded but excluded from every gate (NON_SPENDING_KINDS), so `quota`
            # and `calls` stay truthful without ever blocking a store listing on a day
            # when the text budget is gone.
            row_id = self.ledger.plan(
                key_id=key_id(key), model=model or FILE_ROW_MODEL,
                kind="single" if spending else "filesearch", est_tokens=0,
                request_id=None,
                source="filesearch") if (spending or cfg.proxy.audit) else None
            st, hdrs_out, pl = self._forward(
                method, url, headers, body, stream, length, path, resource, pinned,
                kid, key, model=model)
            if row_id is not None:
                self.ledger.resolve(row_id,
                                    "ok" if st < 400 else self._status_name(st),
                                    http_status=st,
                                    latency_ms=int((time.time() - started) * 1000))
            return st, hdrs_out, pl

        status, out_headers, payload = attempt()

        # --- keep walking on a live quota/overload answer (SPEC §11.6) -------------
        # The ledger only sees traffic that went through the router; a key drained by
        # another consumer looks fresh until the provider answers 429. That answer
        # (or a 503 overload) cools the pair down, so re-running the same selection
        # walk now lands on the next candidate — another key, or the next tier.
        # Forwarding the first failure would push this walk onto every caller.
        # Bounded; only a buffered model call is retried (a streamed body cannot be
        # replayed). A resource-pinned path-model call (File Search generateContent)
        # stays put as before; an interactions chain is pinned to its KEY but may
        # still walk MODELS on that key — the model is interaction-scoped, only the
        # history belongs to the project.
        if spending and model and stream is None and (not resource or model_in_body):
            for _ in range(self._walk_bound()):
                if status not in (429, 503):
                    break
                if status == 503:                 # 429 cooled down inside _forward
                    self.ledger.set_cooldown(kid, model,
                                             time.time() + self.OVERLOAD_COOLDOWN_S,
                                             "unavailable_503")
                next_kid, next_model = self.routing.choose(model, kid if resource else None)
                next_key = self.routing._resolve(next_kid)
                if next_key is None or (next_kid, next_model) == (kid, model):
                    break                         # the walk has nothing new to offer
                if next_model != model:
                    if model_in_body:
                        body = set_interactions_model(body, next_model)
                        length = len(body)
                    else:
                        path = path.replace(f"/models/{model}:", f"/models/{next_model}:", 1)
                    model = next_model
                kid, key = next_kid, next_key
                headers["x-goog-api-key"] = key
                url = self._upstream_url(path, upstream_query)
                status, out_headers, payload = attempt()

        # --- learn what a create produced, so the next call needs no probing -------
        if is_create(path, method):
            self.routing.learn(path, method, status, payload, kid)
            self.routing.learn_interaction(path, method, status, payload, kid)

        if cfg.proxy.audit:
            log.info("proxy %s %s -> %d key=%s resource=%s pinned=%s bytes=%d %.0fms",
                     method, path, status, kid, resource or "-", pinned, length,
                     (time.time() - started) * 1000)
        if model:
            # Who actually served a model call — so a caller's own run log can record
            # the pair without correlating against this proxy's audit stream by time.
            out_headers["x-router-model"] = model
            out_headers["x-router-key-id"] = kid
        self._send(h, status, out_headers.pop("content-type", b"application/json"),
                   payload, out_headers)

    def _forward(self, method, url, headers, body, stream, length, path, resource,
                 pinned, kid, key, model=None) -> tuple[int, dict, bytes]:
        """Send upstream, retrying only where that is safe (SPEC §11.7).

        A non-idempotent POST (create a store, import a file) is never repeated once it
        may have taken effect, and a consumed body stream cannot be replayed at all —
        so both get exactly one attempt. Idempotent reads of a resource whose owner is
        unknown may try the other keys once, which is also how a lost affinity heals.
        """
        attempts = [(url, headers, body, stream, kid, key)]
        if method in ("GET", "DELETE", "HEAD") and not pinned and resource:
            for other in self.routing.probe_order(resource, model or model_of(path))[1:]:
                k = self.routing._resolve(other)
                if k is not None:
                    attempts.append((url, {**headers, "x-goog-api-key": k}, body,
                                     None, other, k))
        last: tuple[int, dict, bytes] = (502, {}, google_error(502, "upstream unreachable"))
        for i, (u, hdrs, b, st, cur_kid, _cur_key) in enumerate(attempts):
            try:
                status, out, payload = self._send_upstream(
                    method, u, hdrs, b, st, length, cur_kid)
            except httpx.HTTPError as exc:
                log.warning("proxy: upstream transport error on %s: %s", path, exc)
                last = (502, {}, google_error(
                    502, f"upstream unreachable: {type(exc).__name__}"))
                if st is not None or b:
                    return last
                continue
            if status in (403, 404) and resource and i + 1 < len(attempts):
                continue                                  # another project may own it
            if status == 429:
                self._quota_cooldown(cur_kid, model or model_of(path), payload)
                if not pinned and i + 1 < len(attempts):
                    continue
                last = (status, out, payload)
                continue
            if resource and status < 400 and not pinned:
                self.ledger.fs_bind(resource, cur_kid)     # self-healed: remember it
            return status, out, payload
        return last

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

    def _quota_cooldown(self, kid: str, model: str | None, payload: bytes) -> None:
        """A 429 from the provider is the router's own signal: reuse its taxonomy and
        its cooldown table instead of inventing a second one."""
        if not model:
            return
        err = GeminiClient._classify(httpx.Response(429, content=payload,
                                                    headers={"content-type":
                                                             "application/json"}))
        if isinstance(err, GeminiHTTPError) and (err.quota_day or err.quota_minute):
            from .ledger import pt_midnight_ts
            until = pt_midnight_ts(self.cfg.quota_day_tz, days_ahead=1)
            if err.retry_after is not None:
                until = min(until, time.time() + err.retry_after + 5)
            self.ledger.set_cooldown(kid, model, until,
                                     "rpd_429" if err.quota_day else "rpm_429")

    # ------------------------------------------------------------------ plumbing
    def _upstream_headers(self, h: BaseHTTPRequestHandler) -> dict:
        return {k: v for k, v in h.headers.items()
                if k.lower() not in HOP_BY_HOP and k.lower() not in CLIENT_AUTH_HEADERS}

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
