"""Key transports — one pool, every key generation.

Google issues two shapes of Gemini API key: the classic `AIzaSy…` (Standard, Cloud
Console) and `AQ.…` (Auth, AI Studio). Which transport a surface accepts depends on the
key generation and endpoint, so the client carries an ordered list of transports, starts
with the one that last worked for a given key, rotates on an auth-shaped refusal, and
remembers the winner. Shape is a guess; the provider's answer is a measurement.

Transports (same host, same endpoints):

    header   ``x-goog-api-key: <key>``        the default, common path
    query    ``?key=<key>``                   the documented REST alternative
    bearer   ``Authorization: Bearer <key>``  what some `AQ.…`-only surfaces expect

A key the provider blocked as leaked is never rotated: no transport resurrects a revoked
credential, and the 403 reaches the operator as itself.

One caveat on the query transport: it puts the key in the URL, where httpx's DEBUG log
prints it. It exists for keys whose surface demands it; while such a key is in the pool,
keep the gateway's log level at INFO (the default) and the key stays out of the log.
"""
from __future__ import annotations

import hashlib
import re

import httpx

TRANSPORTS: tuple[str, ...] = ("header", "query", "bearer")
DEFAULT_TRANSPORT = TRANSPORTS[0]

_CHOSEN: dict[str, str] = {}          # key fingerprint → the transport that last worked

_LEAKED_RX = re.compile(r"reported as leaked", re.I)
_AUTH_RX = re.compile(r"api[ _-]?key|credential|unauthenticated|authentication", re.I)


def fingerprint(key: str) -> str:
    """Same shape as `config.key_id`: 8 hex chars, enough to name a key in a log."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def attempts_for(key: str) -> tuple[str, ...]:
    """The transport that worked for this key first, then the rest. A healthy pool
    therefore behaves exactly as it did before transports existed: one header, no extra
    calls, no surprises."""
    chosen = _CHOSEN.get(fingerprint(key))
    if chosen in TRANSPORTS:
        return (chosen,) + tuple(t for t in TRANSPORTS if t != chosen)
    return TRANSPORTS


def remember(key: str, transport: str) -> None:
    _CHOSEN[fingerprint(key)] = transport


def chosen(key: str) -> str | None:
    return _CHOSEN.get(fingerprint(key))


def reset() -> None:
    """Forget every remembered transport (tests; operators rotating keys in-process)."""
    _CHOSEN.clear()


def merge_headers(base: dict | None, key: str, transport: str) -> dict:
    h = {"content-type": "application/json", **(base or {})}
    h.pop("Authorization", None)         # never carry a stale Authorization into a retry
    if transport == "header":
        h["x-goog-api-key"] = key
    elif transport == "bearer":
        h["Authorization"] = f"Bearer {key}"
    return h


def merge_params(base: dict | None, key: str, transport: str) -> dict:
    p = dict(base or {})
    if transport == "query":
        p["key"] = key
    return p


def leaked(status: int, text: str) -> bool:
    """The provider's leaked-key block. Its own class of answer: unfixable by retrying."""
    return status in (400, 401, 403) and bool(_LEAKED_RX.search(text or ""))


def auth_failure(status: int, text: str) -> bool:
    """A refusal about the credential rather than the request. 401 always is one; 400/403
    count only when the body talks about keys or credentials — a model-permission 403 must
    keep falling through to the model-switch logic untouched."""
    if status == 401:
        return True
    return status in (400, 403) and bool(_AUTH_RX.search(text or ""))


def probe_key(key: str, base_url: str | None = None, *, timeout: float = 20.0) -> dict:
    """Free liveness check for CLIs (`keys add`, `keys list --check`): listModels, trying
    transports until one answers. Returns what the daemon will find — which transport
    works, or why none does — from the same module the daemon uses, so the operator's view
    and the router's behaviour cannot drift apart.

    Result: ``{"ok": bool, "transport": str | None, "status": int | None, "reason": str}``
    with `reason` in the shapes the CLI prints: ``ok``, ``rejected (HTTP n)``,
    ``unreachable (…)``, ``HTTP n``, or the leaked-key sentence."""
    url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/") + "/models"
    last: dict = {"ok": False, "transport": None, "status": None, "reason": "not tried"}
    with httpx.Client(timeout=timeout) as client:
        for transport in attempts_for(key):
            try:
                r = client.get(url, params=merge_params({"pageSize": 1}, key, transport),
                               headers=merge_headers(None, key, transport))
            except httpx.HTTPError as e:
                last = {"ok": False, "transport": transport, "status": None,
                        "reason": f"unreachable ({e!r})"}
                continue
            if r.status_code == 200:
                remember(key, transport)
                return {"ok": True, "transport": transport, "status": 200, "reason": "ok"}
            if leaked(r.status_code, r.text):
                return {"ok": False, "transport": transport, "status": r.status_code,
                        "reason": "reported as leaked — delete the key in the console, then "
                                  "add a fresh one"}
            if not auth_failure(r.status_code, r.text):
                last = {"ok": False, "transport": transport, "status": r.status_code,
                        "reason": f"HTTP {r.status_code}"}
                break        # not an auth problem — another transport cannot help
            last = {"ok": False, "transport": transport, "status": r.status_code,
                    "reason": f"rejected (HTTP {r.status_code})"}
    return last
