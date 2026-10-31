"""Request identity, job/trace correlation and structured logging.

Every record produced by the router carries the same identity:

* **version** — the build that produced it;
* **initiator** — a free-form string supplied by the caller; ``source`` remains the
  entry point (``filesearch`` / ``cli`` / ``mcp``);
* **job** — the ID of one *target activity*, e.g. ``job-42``. All requests,
  hops, provider signals, cooldown decisions and notes belonging to that activity share
  it. When a client sends no job, ``<initiator>@<UTC date>`` groups the day;
* **trace** — one execution of the job. It comes from ``X-Router-Trace``, from a W3C
  ``traceparent`` header, or equals the job. Retries of the same activity get a new
  trace while staying inside the same job;
* **request_id / span_id** — one logical request and the local span for it. ``hop``
  counts upstream attempts inside the selection walk; ``step`` names the client's
  stage (``prefilter``, ``analysis``, ``dedup``, ``subject``, ``gemini-search`` ...);
* **comment** — arbitrary human text with **no behaviour attached**. It is stored and
  logged, never used for routing and never sent upstream.

Logs are single-line logfmt, so journald, sqlite and grep all read them.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any
from urllib.parse import unquote

INITIATOR_HEADERS = ("x-router-initiator", "x-router-source")
COMMENT_HEADERS = ("x-router-comment", "x-router-note")
REQUEST_ID_HEADERS = ("x-router-request-id", "x-router-span")
JOB_HEADERS = ("x-router-job", "x-router-job-id")
TRACE_HEADERS = ("x-router-trace", "x-router-trace-id")
STEP_HEADERS = ("x-router-step", "x-router-stage")
PARENT_SPAN_HEADERS = ("x-router-parent-span",)

#: Router-only metadata. Consumed locally; never forwarded to the provider.
CLIENT_CONTEXT_HEADERS = frozenset({
    "x-router-initiator", "x-router-source", "x-router-comment", "x-router-note",
    "x-router-request-id", "x-router-span", "x-router-job", "x-router-job-id",
    "x-router-trace", "x-router-trace-id", "x-router-step", "x-router-stage",
    "x-router-parent-span", "traceparent", "tracestate", "baggage",
})

MAX_COMMENT_CHARS = 500
MAX_ID_CHARS = 128
_PCT = re.compile(r"%[0-9A-Fa-f]{2}")
_TRACEPARENT = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(?:-.*)?$", re.I)


def router_version() -> str:
    """The version string, sourced from pyproject.toml via installed metadata."""
    try:
        return _pkg_version("gemini-router")
    except PackageNotFoundError:          # unpackaged run from a source checkout
        return "0.0.0.dev0"


def _clean(value: str | None, limit: int) -> str:
    if not value:
        return ""
    # HTTP headers are ASCII-only. Percent-encoded text (e.g. a Cyrillic comment) is
    # decoded here so the audit trail keeps the original characters.
    if _PCT.search(value):
        value = unquote(value, encoding="utf-8", errors="replace")
    return "".join(c if c.isprintable() else " " for c in value).strip()[:limit]


def _first(headers: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        v = headers.get(name)
        if v:
            return str(v)
    return ""


def parse_traceparent(value: str) -> tuple[str, str, str]:
    """W3C Trace Context → ``(trace_id, parent_span_id, version)``."""
    m = _TRACEPARENT.match((value or "").strip())
    if not m or m.group(1).lower() == "ff":
        return "", "", ""
    return m.group(2).lower(), m.group(3).lower(), m.group(1).lower()


def _hex_id(value: str, size: int) -> str:
    """A W3C-compatible hex field: pass valid hex through, hash anything else."""
    raw = (value or "").replace("-", "").lower()
    if len(raw) >= size and all(c in "0123456789abcdef" for c in raw[:size]):
        return raw[:size]
    return hashlib.blake2b((value or "").encode(), digest_size=size // 2).hexdigest()


def make_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    """Emit a W3C ``traceparent``; free-form IDs are hashed into valid hex fields."""
    return f"00-{_hex_id(trace_id, 32)}-{_hex_id(span_id, 16)}-{'01' if sampled else '00'}"


@dataclass(slots=True)
class RequestContext:
    """Identity of one logical request, shared by every hop of its walk."""

    initiator: str = ""
    comment: str = ""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: str = "cli"
    version: str = field(default_factory=router_version)
    started: float = field(default_factory=time.time)
    job: str = ""
    trace_id: str = ""
    step: str = ""
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span: str = ""

    @classmethod
    def from_headers(cls, headers: Mapping[str, str], *, default_initiator: str = "",
                     source: str = "filesearch") -> RequestContext:
        # BaseHTTPRequestHandler headers are case-insensitive; a plain dict in tests is
        # not, so normalise explicitly.
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        initiator = _clean(_first(h, INITIATOR_HEADERS) or default_initiator, 64)
        tp_trace, parent, _ = parse_traceparent(h.get("traceparent", ""))
        job = _clean(_first(h, JOB_HEADERS), MAX_ID_CHARS)
        if not job:
            day = datetime.now(UTC).date().isoformat()
            job = f"{initiator or 'anonymous'}@{day}"
        trace = _clean(_first(h, TRACE_HEADERS), 64) or tp_trace or job
        return cls(
            initiator=initiator,
            comment=_clean(_first(h, COMMENT_HEADERS), MAX_COMMENT_CHARS),
            request_id=_clean(_first(h, REQUEST_ID_HEADERS), 64) or uuid.uuid4().hex[:12],
            source=source, job=job, trace_id=trace,
            step=_clean(_first(h, STEP_HEADERS), 64),
            parent_span=_clean(_first(h, PARENT_SPAN_HEADERS), 64) or parent)

    def as_dict(self) -> dict:
        return {"initiator": self.initiator, "comment": self.comment,
                "request_id": self.request_id, "source": self.source,
                "version": self.version, "started": self.started,
                "job": self.job, "trace_id": self.trace_id, "step": self.step,
                "span_id": self.span_id, "parent_span": self.parent_span}

    @property
    def traceparent(self) -> str:
        return make_traceparent(self.trace_id, self.span_id)

    def log_fields(self) -> dict:
        """Identity fields shared by every structured log line."""
        return {"v": self.version, "rid": self.request_id, "job": self.job,
                "trace": self.trace_id, "span": self.span_id,
                "initiator": self.initiator or "-", "step": self.step or "-",
                "comment": self.comment or "-"}


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
