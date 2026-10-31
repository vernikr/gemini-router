"""File Search routing rules for the native-Gemini pass-through (SPEC §11).

A File Search store — and every Files API object, document and long-running operation
under it — belongs to the Google project of the key that created it. The router's
ordinary rotation picks the key with the largest remaining budget, which is right for
generateContent (a file can be re-uploaded under another key) and wrong here: the second
request to a store would land in a different project and answer 404.

So this module adds ONE mapping on top of the existing pool and ledger, never a second
key system:

    resource ('fileSearchStores/<id>', 'files/<id>')  ->  key_id

The mapping lives in the shared SQLite ledger, because every process that serves File
Search on this machine must agree on it, and because a daemon restart must not orphan
every store. Everything else — which keys exist, how much budget each has, which
transport a key speaks, how an upstream refusal is classified — is reused as is.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx

from . import keyauth
from .config import TTS_MODEL, Config, key_id
from .ledger import Ledger
from .routing import gates_for, pick_pair


@dataclass(frozen=True, slots=True)
class Selection:
    """The full verdict of the shared walk, not just a (key, model) pair.

    `state` lets the caller distinguish "send now" from "wait a minute" and from
    "every pair is blocked by the provider — answer locally with Retry-After".
    """
    key_id: str
    model: str | None
    state: str = "ok"                  # ok | wait | throttled | exhausted
    retry_after_s: float | None = None
    wait_s: float = 0.0

# ---------------------------------------------------------------- path shapes
STORE_RX = re.compile(r"/v1beta/(fileSearchStores/[^/:?]+)")
FILE_RX = re.compile(r"/v1beta/(files/[^/:?]+)")
UPLOAD_STORE_RX = re.compile(r"/upload/v1beta/(fileSearchStores/[^/:?]+):uploadToFileSearchStore$")

# What the pass-through serves. Everything else is 404 in the provider's own shape —
# not an artificial limit, but the boundary of what this proxy is for: the calls
# github.com/mikesmitty/file-search actually makes.
ALLOWED = (
    re.compile(r"^/v1beta/fileSearchStores(/[^/]+(/documents(/[^/]+)?)?|/[^/]+/operations/[^/]+"
               r"|:importFile)?$"),
    re.compile(r"^/v1beta/files(/[^/]+)?$"),
    re.compile(r"^/v1beta/models/[^/:]+(:(generateContent|countTokens))?$"),
    re.compile(r"^/v1beta/models$"),
    re.compile(r"^/upload/v1beta/files$"),
    re.compile(r"^/upload/v1beta/fileSearchStores/[^/:?]+:uploadToFileSearchStore$"),
    # Interactions API (generateContent remains supported): create, retrieve,
    # delete, cancel. Server-side conversation state is project-scoped exactly
    # like a File Search store, so the same resource->key binding carries it.
    re.compile(r"^/v1beta/interactions$"),
    re.compile(r"^/v1beta/interactions/[^/:?]+(:cancel)?$"),
)

GENERATE_RX = re.compile(r"^/v1beta/models/([^/:]+):generateContent$")
UPLOAD_PATHS = ("/upload/v1beta/files",)
INTERACTIONS_CREATE = "/v1beta/interactions"
INTERACTION_RX = re.compile(r"^/v1beta/interactions/([^/:?]+)")


def allowed_path(path: str) -> bool:
    """True for the four request families the pass-through exists for."""
    return any(rx.match(path) for rx in ALLOWED)


def is_upload_start(path: str, method: str, query: str) -> bool:
    """The resumable handshake: no upload_id yet, so upstream hands out a session."""
    return (method == "POST" and path.startswith("/upload/")
            and "upload_id" not in query)


def is_upload_chunk(path: str, query: str) -> bool:
    """A continuation: it carries upstream's upload_id and nothing else identifies it."""
    return path.startswith("/upload/") and "upload_id" in query


def is_create(path: str, method: str) -> bool:
    """A request that creates a project-scoped resource — never blindly retried."""
    return method == "POST" and (path == "/v1beta/fileSearchStores"
                                 or ":importFile" in path
                                 or UPLOAD_STORE_RX.match(path) is not None
                                 or path == INTERACTIONS_CREATE)


def resource_of(path: str, body: bytes | None = None) -> str | None:
    """The project-scoped resource a request touches, or None when it has none.

    For `generateContent` the store is named in the BODY, not the path
    (`tools[].fileSearch.fileSearchStoreNames[]`), so the body has to be read too —
    that is the one reason the proxy buffers small request bodies.
    """
    m = STORE_RX.search(path) or UPLOAD_STORE_RX.search(path)
    if m:
        return m.group(1)
    m = FILE_RX.search(path)
    if m:
        return m.group(1)
    m = INTERACTION_RX.match(path)
    if m:
        return f"interactions/{m.group(1)}"
    if body:
        if path == INTERACTIONS_CREATE:
            # A chained turn must land in the project that holds the previous
            # turn's server-side state — the whole point of the chain.
            prev = str((_body_json(body) or {}).get("previous_interaction_id") or "")
            return f"interactions/{prev}" if prev else None
        g = GENERATE_RX.match(path)
        if g:
            data = _body_json(body)
            if data is None:
                return None
            for tool in data.get("tools") or []:
                names = (tool.get("fileSearch") or {}).get("fileSearchStoreNames") or []
                if names:
                    return names[0]
    return None


def _body_json(body: bytes) -> dict | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def model_of(path: str) -> str | None:
    m = re.match(r"^/v1beta/models/([^/:]+):", path)
    return m.group(1) if m else None


def rewrite_model(body: bytes, model: str) -> bytes:
    """Swap the model in a generateContent request path — done on the path, so the body
    needs no change; this helper exists for callers that carry the model in the body
    (countTokens shares the shape)."""
    return body


def interactions_model(body: bytes | None) -> str | None:
    """The model named in an interactions create body — the Interactions API moved
    the model out of the URL path and into the request body."""
    if not body:
        return None
    data = _body_json(body)
    model = str((data or {}).get("model") or "")
    return model or None


def interactions_text(body: bytes | None) -> str:
    """Extract only the text portions of a single-speaker TTS interaction for the
    pre-flight TPM estimate. Other Interactions calls naturally return an empty string.
    """
    data = _body_json(body or b"") or {}
    pieces: list[str] = []
    for turn in data.get("input") or []:
        content = turn.get("content") or [] if isinstance(turn, dict) else []
        if isinstance(content, str):
            pieces.append(content)
        elif isinstance(content, list):
            pieces.extend(str(item.get("text") or "") for item in content
                          if isinstance(item, dict) and item.get("type") == "text")
    return "\n".join(part for part in pieces if part)


def set_interactions_model(body: bytes, model: str) -> bytes:
    """The same body asking for `model` instead. Only the one field changes; an
    unparseable body comes back untouched — forwarding a client's exact bytes is
    always safer than guessing."""
    data = _body_json(body)
    if data is None or not data.get("model"):
        return body
    data["model"] = model
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------- upload tickets
class UploadTickets:
    """Binds a resumable-upload session to the key that started it.

    Google answers the handshake with an absolute `X-Goog-Upload-Url` on ITS own host.
    The Gen AI SDK rewrites that URL's scheme+host back to the base URL it was given —
    which is exactly why the continuation comes back through this proxy instead of
    bypassing it — and copies the query string verbatim, so two extra parameters ride
    along for free. They are the session's whole identity:

        router_k  the key_id that owns the session (project affinity, survives a restart)
        router_s  HMAC(token, "<upload_id>|<key_id>|upload") — the authentication

    Stateless: no in-memory table to lose, and a daemon restart cannot orphan a
    half-finished upload.
    """

    def __init__(self, token: str, origin: str):
        self._token = token
        self.origin = origin.rstrip("/")

    @staticmethod
    def _sig(token: str, payload: str) -> str:
        return hmac.new(token.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]

    def verify(self, upload_id: str, kid: str, sig: str) -> bool:
        return bool(upload_id) and bool(kid) and bool(sig) and hmac.compare_digest(
            sig, self._sig(self._token, f"{upload_id}|{kid}|upload"))

    def rewrite(self, upstream_url: str, kid: str) -> str:
        """Point the upload URL back at this proxy and stamp it with the session."""
        up = urlparse(upstream_url)
        q = dict(parse_qsl(up.query))
        upload_id = q.get("upload_id", "")
        q["router_k"] = kid
        q["router_s"] = self._sig(self._token, f"{upload_id}|{kid}|upload")
        return f"{self.origin}{up.path}?" + urlencode(list(q.items()))

    def strip(self, query: str) -> str:
        """The query to send upstream: ours removed, everything else preserved."""
        q = [(k, v) for k, v in parse_qsl(query) if k not in ("router_k", "router_s")]
        return urlencode(q)


# ---------------------------------------------------------------- key selection
class FileSearchRouting:
    """Picks the pool key that must serve a request, and learns new bindings.

    Pure bookkeeping over `Config.api_keys` and `Ledger`: no new pool, no new quota
    arithmetic — `gates_for` (already the router's own gate function) ranks candidates.
    """

    def __init__(self, cfg: Config, ledger: Ledger, *, now=None):
        self.cfg = cfg
        self.ledger = ledger
        self._now = now
        # listModels cache: key_id -> (ts, models). Process-local like Router._avail —
        # a stale model list costs one failed request at worst, and the ledger stays the
        # single source of truth for everything that must be shared.
        self._avail: dict[str, tuple[float, set[str] | None]] = {}

    # ------------------------------------------------------------- default project
    def pin(self) -> str:
        """The key_id that owns File Search by default.

        Persisted on first use: two different keys would show two different sets of
        stores, so `list_stores` must not drift between calls.
        """
        forced = self.cfg.file_search_key
        if forced:
            return forced
        saved = self.ledger.fs_pin()
        if saved and self._resolve(saved):
            return saved
        kid = self._richest(self.cfg.chain[0] if self.cfg.chain else None)
        self.ledger.fs_set_pin(kid)
        return kid

    # ------------------------------------------------------------------ selection
    def select(self, path: str, query: str, body: bytes | None) -> tuple[str, str | None, bool]:
        """(key_id, resource, pinned) for a non-continuation request."""
        resource = resource_of(path, body)
        if resource:
            owner = self.ledger.fs_owner(resource)
            if owner and self._resolve(owner):
                return owner, resource, True
        return self.pin(), resource, False

    def probe_order(self, resource: str, model: str | None) -> list[str]:
        """Keys to try for a resource whose owner is unknown or lost, best budget first.

        Only ever used for idempotent reads/deletes, and bounded by the pool size — an
        unknown store is usually one created outside this router, and asking every key
        once is cheap and self-healing.
        """
        keys = [key_id(k) for k in self._ranked(model)]
        known = self.ledger.fs_owner(resource)
        return ([known] if known else []) + [k for k in keys if k != known]

    def _ranked(self, model: str | None) -> list[str]:
        if not model or model not in self.cfg.chain:
            return list(self.cfg.api_keys)
        return sorted(self.cfg.api_keys,
                      key=lambda k: -gates_for(self.cfg, self.ledger, model, k, 0,
                                               self._now() if self._now else None)["rpd_left"])

    def _richest(self, model: str | None) -> str:
        ranked = self._ranked(model)
        return key_id(ranked[0]) if ranked else ""

    def _resolve(self, kid: str) -> str | None:
        """The raw key for a key_id, or None when the pool no longer holds it."""
        for k in self.cfg.api_keys:
            if key_id(k) == kid:
                return k
        return None

    # ------------------------------------------------------------------- learning
    def learn(self, path: str, method: str, status: int, body: bytes, kid: str) -> None:
        """Remember what a successful create produced, so the next request is pinned."""
        if not is_create(path, method) or status >= 300 or not body:
            return
        try:
            name = (json.loads(body) or {}).get("name") or ""
        except ValueError:
            return
        if not isinstance(name, str):
            return
        resource = None
        if name.startswith("fileSearchStores/"):
            resource = name.split("/operations/")[0]
        elif name.startswith("files/"):
            resource = name
        if resource:
            self.ledger.fs_bind(resource, kid)

    def learn_interaction(self, path: str, method: str, status: int,
                          body: bytes, kid: str) -> None:
        """Remember which key served a created interaction: the next turn carries
        this id as `previous_interaction_id`, and its server-side history exists
        only in that key's project."""
        if path != INTERACTIONS_CREATE or method != "POST" or status >= 300 or not body:
            return
        data = _body_json(body)
        iid = str((data or {}).get("id") or "")
        if iid:
            self.ledger.fs_bind(f"interactions/{iid}", kid)

    # --------------------------------------------------------------------- models
    def available_models(self, key: str, timeout: float = 15.0) -> set[str] | None:
        """`listModels` for one key, over the transport that key speaks (keyauth).

        Cached for `models_refresh_h` — the same reason `Router._availability` caches:
        the model list is a property of the account tier, so asking on every request
        buys nothing and costs a round trip on a path that runs per query. A failed
        probe is cached briefly instead, so an offline upstream cannot turn every
        request into a timeout.
        """
        now = time.time()
        kid = key_id(key)
        hit = self._avail.get(kid)
        if hit and now - hit[0] < self.cfg.models_refresh_h * 3600 and hit[1] is not None:
            return hit[1]
        if hit and now - hit[0] < 60 and hit[1] is None:
            return None
        models = self._fetch_models(key, timeout)
        self._avail[kid] = (now, models)
        return models

    def _fetch_models(self, key: str, timeout: float) -> set[str] | None:
        url = f"{self.cfg.base_url.rstrip('/')}/models"
        try:
            with httpx.Client(timeout=timeout) as cli:
                for transport in keyauth.attempts_for(key):
                    r = cli.get(url, params=keyauth.merge_params({"pageSize": 1000}, key,
                                                                  transport),
                                headers=keyauth.merge_headers(None, key, transport))
                    if r.status_code < 400:
                        keyauth.remember(key, transport)
                        return {m.get("name", "").removeprefix("models/")
                                for m in (r.json() or {}).get("models", []) if m.get("name")}
                    if (keyauth.leaked(r.status_code, r.text)
                            or not keyauth.auth_failure(r.status_code, r.text)):
                        break
        except httpx.HTTPError:
            return None
        return None

    def choose(self, requested: str | None, kid_pin: str | None = None,
               exclude: set[tuple[str, str]] | None = None,
               est_tokens: int = 0) -> Selection:
        """Selection for a model-path request, per `proxy.model_policy` (SPEC §11.6).

        The walk itself is `routing.pick_pair` — the single selection procedure every
        entry point shares (tiers incl. `overflow_default`, keys by remaining budget,
        ledger gates). This method only maps the proxy's policy onto its inputs:

        - `kid_pin` carries resource affinity — the key is not negotiable, only the
          model walks; without it both the key and the model are chosen by budget.
        - `auto` keeps the caller's model when a key can serve it and falls back to
          the shared walk otherwise; `strict` always walks; `passthrough` never
          rewrites and keeps the key the caller landed on.
        - `exclude` carries pairs already tried in this request, so a 429 walk cannot
          keep re-selecting the same exhausted pair.
        """
        policy = self.cfg.proxy.model_policy
        if requested == TTS_MODEL:
            # TTS is a separate, fixed-model operation: never rewrite it to a text model,
            # even when the proxy's text policy is `strict` or `passthrough`.
            pinned_key = self._resolve(kid_pin) if kid_pin else None
            if kid_pin and pinned_key is None:
                return Selection(kid_pin, TTS_MODEL)
            pool = [pinned_key] if pinned_key else list(self.cfg.api_keys)
            avail = {key_id(k): self.available_models(k) for k in pool}
            choice = pick_pair(self.cfg, self.ledger, overflow="off",
                               chain_override=[TTS_MODEL], keys=pool, avail=avail,
                               est_tokens=est_tokens, exclude=exclude,
                               now=self._now() if self._now else None)
            if choice.key is not None:
                return Selection(key_id(choice.key), TTS_MODEL,
                                 "wait" if choice.reason == "wait" else "ok",
                                 choice.retry_after_s,
                                 choice.gates["wait_s"] if choice.gates else 0.0)
            fallback_kid = kid_pin or self.pin()
            return Selection(fallback_kid, TTS_MODEL, choice.reason,
                             choice.retry_after_s or 60.0)
        if policy == "passthrough" or not requested:
            return Selection(kid_pin or self.pin(), requested)
        keys: list[str] | None = None
        if kid_pin:
            key = self._resolve(kid_pin)
            if key is None:
                return Selection(kid_pin, requested)  # proxy answers the lost key itself
            keys = [key]
        if policy == "auto":
            if requested in self.cfg.chain:
                # Already a router model: nothing to gain from a rewrite.
                return Selection(kid_pin or self._richest(requested), requested)
            probe_kid = kid_pin or self._richest(self.cfg.chain[0] if self.cfg.chain else None)
            probe = self._resolve(probe_kid)
            if probe is not None:
                available = self.available_models(probe)
                if available is None or requested in available:
                    return Selection(probe_kid, requested)  # unknown availability must not break
        choice = pick_pair(self.cfg, self.ledger,
                           overflow=self.cfg.overflow_default,
                           chain_override=self.cfg.proxy.file_search_models,
                           keys=keys, exclude=exclude,
                           now=self._now() if self._now else None)
        if choice.model is None or choice.key is None:
            cands = self.cfg.proxy.file_search_models or list(self.cfg.chain)
            head = cands[0] if cands else requested
            if choice.reason == "throttled":
                return Selection(kid_pin or self.pin(), head, "throttled",
                                 retry_after_s=choice.retry_after_s or 60.0)
            # No provider verdict, just spent counters: still ask the provider once —
            # it is the authority and may have reset out of band.
            return Selection(kid_pin or self.pin(), head, "exhausted")
        if choice.reason == "wait":
            return Selection(key_id(choice.key), choice.model, "wait",
                             retry_after_s=choice.retry_after_s,
                             wait_s=choice.gates["wait_s"] if choice.gates else 0.0)
        return Selection(key_id(choice.key), choice.model, "ok")
