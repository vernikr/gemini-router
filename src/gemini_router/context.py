"""Request identity and structured logging.

Every hop through the router carries the same three facts, whether it came from the
pass-through proxy, the Python library, a CLI command or a plain SQL row:

* **version** — which build produced the record (so a mixed fleet is diagnosable);
* **initiator** — a free-form string supplied by whoever started the work
  (``tldr-digest``, ``gemini-mcp``, ``ops-audit`` ...). This is deliberately *not* the
  transport name: ``source`` stays the entry point (``filesearch``/``cli``/``mcp``) for
  backwards compatibility;
* **comment** — arbitrary human text with **no behaviour attached**. It is stored and
  logged, nothing more: no routing decision, no quota effect, no header sent upstream.

The log format is one-line ``key=value`` (logfmt). journald, sqlite and grep can all
read it, and a quoted value keeps spaces/comments intact.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any
from urllib.parse import unquote

#: Header names consumed by the proxy. They never reach Google (see
#: ``proxy.CLIENT_CONTEXT_HEADERS``) — they are metadata for the router only.
INITIATOR_HEADERS = ("x-router-initiator", "x-router-source")
COMMENT_HEADERS = ("x-router-comment", "x-router-note")
REQUEST_ID_HEADER = "x-router-request-id"

MAX_COMMENT_CHARS = 500
MAX_INITIATOR_CHARS = 64


def router_version() -> str:
    """Single source of truth for the version string (pyproject.toml via metadata)."""
    try:
        return _pkg_version("gemini-router")
    except PackageNotFoundError:          # unpackaged run from a source checkout
        return "0.0.0.dev0"


_PCT = re.compile(r"%[0-9A-Fa-f]{2}")


def _clean(value: str | None, limit: int) -> str:
    if not value:
        return ""
    # HTTP headers are ASCII-only. A Cyrillic comment (or any non-ASCII text) can be
    # sent percent-encoded; decode it here so the audit trail keeps the real text.
    if _PCT.search(value):
        value = unquote(value, encoding="utf-8", errors="replace")
    return "".join(c if c.isprintable() else " " for c in value).strip()[:limit]


def _first(headers: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        v = headers.get(name)
        if v:
            return str(v)
    return ""


@dataclass(slots=True)
class RequestContext:
    """Identity of one logical request, threaded through every attempt of a walk."""

    initiator: str = ""
    comment: str = ""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: str = "cli"                  # transport/entry point, kept for old readers
    version: str = field(default_factory=router_version)
    started: float = field(default_factory=time.time)

    @classmethod
    def from_headers(cls, headers: Mapping[str, str], *, default_initiator: str = "",
                     source: str = "filesearch") -> RequestContext:
        # BaseHTTPRequestHandler headers are case-insensitive; a plain dict in tests is
        # not, so look up both spellings explicitly.
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        return cls(
            initiator=_clean(_first(h, INITIATOR_HEADERS) or default_initiator,
                             MAX_INITIATOR_CHARS),
            comment=_clean(_first(h, COMMENT_HEADERS), MAX_COMMENT_CHARS),
            request_id=_clean(h.get(REQUEST_ID_HEADER, ""), 64) or uuid.uuid4().hex[:12],
            source=source,
        )

    def child(self, **over: Any) -> RequestContext:
        return RequestContext(**{**self.as_dict(), **over})

    def as_dict(self) -> dict:
        return {"initiator": self.initiator, "comment": self.comment,
                "request_id": self.request_id, "source": self.source,
                "version": self.version, "started": self.started}


def audit_line(event: str, **fields: Any) -> str:
    """logfmt string: ``event=... key=value ...`` with values quoted when needed."""
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        text = str(value)
        if any(c.isspace() for c in text) or '"' in text or "=" in text:
            text = json.dumps(text, ensure_ascii=False)
        parts.append(f"{key}={text}")
    return " ".join(parts)
