"""Provider truth: turn an upstream answer into a durable quota decision.

The ledger's counters are a *prediction*. Google is the authority. When it answers 429
or 503, that answer must immediately change what the router believes about the exact
``(key, model)`` pair — otherwise the same exhausted pair is selected again and again
inside a single request (this is how 48 refusals happened in ten seconds on 2026-10-21).

This module owns the whole round trip:

    status + headers + body  ->  ProviderSignal  ->  ledger.pair_state
                                                 ->  cooldowns (mirror for old readers)
                                                 ->  one structured audit line

There are two important asymmetries:

* A **confirmed daily** quota answer (``...PerDay...`` in ``quotaId``) blocks the pair
  until the next Pacific-time reset. No ``min(..., now + 65)`` — that cap was the bug
  that made a day-long exhaustion expire in a minute.
* An **unconfirmed** 429 (no quota details in the body — what the free tier actually
  sends during a capacity blip) only benches the pair for the provider's Retry-After.
  Repeated unconfirmed answers inside a short window are *promoted* to a day block:
  the router learns the truth from reality instead of guessing it once.

503 is overload, not a budget verdict: short bench, never counted as RPD spend.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

from .config import Config
from .context import RequestContext, audit_line
from .ledger import Ledger, pt_midnight_ts

log = logging.getLogger("gemini_router.feedback")

Kind = Literal["none", "day", "minute", "overload"]

_RETRY_IN = re.compile(r"(?:retry|please\s+try)\D{0,24}(\d+(?:\.\d+)?)\s*(ms|s|m)?", re.I)
_RETRY_DELAY_KEY = re.compile(r"retryDelay|retry_after|retryAfter|retryDelaySeconds", re.I)
_QUOTA_KEYS = frozenset({"quotaid", "quotametric", "quotametricname", "quotavalue",
                         "quotaunit", "quotadimension", "quotadimensions"})
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")


@dataclass(frozen=True, slots=True)
class ProviderSignal:
    """What the provider just told us about one ``(key, model)`` pair."""

    kind: Kind
    until_ts: float
    retry_after_s: float | None
    reason: str
    status: int | None = None
    unconfirmed: bool = False
    quota_id: str | None = None
    quota_metric: str | None = None
    quota_value: str | None = None
    detail: str = ""

    @property
    def blocks_pair(self) -> bool:
        return self.kind != "none"


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """RFC 7231 Retry-After: either delta-seconds or an HTTP-date."""
    if not value:
        return None
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return max(0.0, dt.timestamp() - (now if now is not None else time.time()))
    except (TypeError, ValueError, OverflowError):
        return None


def _flatten_metadata(obj: Any, out: dict[str, Any]) -> None:
    """Google puts quota facts in several slightly different shapes; walk them all."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower().replace("_", "")
            if kl in _QUOTA_KEYS:
                out.setdefault(kl, v)
            if _RETRY_DELAY_KEY.search(str(k)) and isinstance(v, (str, int, float)):
                out.setdefault("retry_delay", v)
            if kl == "message" and isinstance(v, str):
                out.setdefault("message", v)
            _flatten_metadata(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _flatten_metadata(item, out)


def body_facts(body: bytes | str | None) -> dict[str, Any]:
    """Parse an error body without ever trusting it to be JSON or complete."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    text = body or ""
    facts: dict[str, Any] = {"message": "", "raw": text[:1000]}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if parsed is not None:
        _flatten_metadata(parsed, facts)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            facts.setdefault("message", err["message"])
    else:
        facts["message"] = text[:500]

    delay = facts.get("retry_delay")
    if delay is None:
        m = _RETRY_IN.search(facts.get("message", "") or "") or _RETRY_IN.search(text)
        if m:
            n = float(m.group(1))
            unit = (m.group(2) or "s").lower()
            delay = n / 1000 if unit == "ms" else n * 60 if unit == "m" else n
    elif isinstance(delay, str):
        m = _NUMBER.search(delay.replace(",", "."))
        delay = float(m.group(1)) if m else None
        if delay and delay > 1000:       # milliseconds expressed as a bare number
            delay /= 1000
    facts["retry_delay"] = float(delay) if isinstance(delay, (int, float)) else None

    def s(key: str) -> str | None:
        v = facts.get(key)
        return str(v) if v is not None else None

    return {"quota_id": s("quotaid"), "quota_metric": s("quotametric") or s("quotametricname"),
            "quota_value": s("quotavalue"), "message": facts.get("message", ""),
            "retry_delay": facts["retry_delay"], "raw": facts["raw"]}


def parse_provider_signal(status: int, headers: Mapping[str, str] | None,
                          body: bytes | str | None, cfg: Config,
                          now: float | None = None) -> ProviderSignal:
    """The single classifier for upstream answers. Pure: no I/O, no ledger writes."""
    now = now if now is not None else time.time()
    fb = cfg.feedback
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    facts = body_facts(body)
    detail = " ".join((facts["message"] or facts["raw"]).split())[:220]
    retry = parse_retry_after(h.get("retry-after"), now)
    if retry is None:
        retry = facts["retry_delay"]
    if status == 503 or status == 502:
        return ProviderSignal(kind="overload", until_ts=now + fb.overload_cooldown_s,
                              retry_after_s=retry, reason=f"unavailable_{status}",
                              status=status, detail=detail)
    if status != 429:
        return ProviderSignal(kind="none", until_ts=now, retry_after_s=retry,
                              reason="ok", status=status, detail=detail)

    qid = (facts["quota_id"] or "").lower()
    metric = (facts["quota_metric"] or "").lower()
    message = (facts["message"] or "").lower()
    day = any("perday" in x or "per_day" in x for x in (qid, metric, message))
    minute = any("perminute" in x or "per_minute" in x for x in (qid, metric, message))
    if day:
        # A day verdict lasts until the reset. No min() with Retry-After: a
        # "retry in 42s" hint inside a daily-quota body is a red herring.
        until = pt_midnight_ts(cfg.quota_day_tz, now, days_ahead=1) + fb.reset_buffer_s
        return ProviderSignal(kind="day", until_ts=until, retry_after_s=retry,
                              reason="rpd_429", status=status,
                              quota_id=facts["quota_id"], quota_metric=facts["quota_metric"],
                              quota_value=facts["quota_value"], detail=detail)
    # A confirmed minute quota carries a real Retry-After — honour it exactly (plus a
    # small buffer). Only an unconfirmed capacity blip gets the conservative floor.
    floor = 1.0 if minute else fb.throttle_min_s
    seconds = retry if retry is not None else fb.throttle_min_s
    seconds = min(max(seconds + fb.buffer_s, floor), fb.throttle_max_s)
    return ProviderSignal(kind="minute", until_ts=now + seconds,
                          retry_after_s=retry if retry is not None else seconds,
                          reason="rpm_429" if minute else "throttle_429",
                          status=status, unconfirmed=not minute,
                          quota_id=facts["quota_id"], quota_metric=facts["quota_metric"],
                          quota_value=facts["quota_value"], detail=detail)


class QuotaTruth:
    """Apply provider signals to the shared ledger, for every entry point."""

    def __init__(self, ledger: Ledger, cfg: Config, *, now_fn=None):
        self.ledger = ledger
        self.cfg = cfg
        self._now = now_fn or time.time

    def apply(self, signal: ProviderSignal, *, key_id: str, model: str,
              context: RequestContext | None = None) -> dict:
        """Upsert the pair's provider state; return the row that was written."""
        if not signal.blocks_pair or not model:
            return {}
        now = self._now()
        fb = self.cfg.feedback
        ctx = context or RequestContext()
        previous = self.ledger.provider_state(key_id, model, now)
        hits, first_ts = 1, now
        if previous and previous["kind"] in ("minute", "overload"):
            if now - previous["first_ts"] <= fb.promote_window_s:
                hits, first_ts = previous["hits"] + 1, previous["first_ts"]

        kind, until, reason = signal.kind, signal.until_ts, signal.reason
        promoted = False
        if (signal.unconfirmed and hits >= fb.promote_hits):
            # The provider keeps refusing without telling us why. Stop treating it as
            # a minute-scale hiccup: this pair is out until the daily reset.
            kind = "day"
            until = pt_midnight_ts(self.cfg.quota_day_tz, now, days_ahead=1) + fb.reset_buffer_s
            reason = "rpd_429_promoted"
            promoted = True
        if previous and previous["kind"] == "day" and previous["until_ts"] > until:
            # Never shorten a confirmed day block because of a later minute answer.
            kind, until = "day", previous["until_ts"]
            reason = previous["reason"] or reason

        row = {"key_id": key_id, "model": model, "kind": kind, "until_ts": until,
               "retry_after_s": signal.retry_after_s, "quota_id": signal.quota_id,
               "quota_metric": signal.quota_metric, "quota_value": signal.quota_value,
               "reason": reason, "hits": hits, "first_ts": first_ts, "updated_ts": now,
               "initiator": ctx.initiator, "version": ctx.version, "detail": signal.detail}
        self.ledger.set_pair_state(row)
        # Old library builds (gemini-mcp, an older CLI) only read `cooldowns`. Mirror
        # the decision so a mixed fleet still agrees on which pairs are out.
        self.ledger.set_cooldown(key_id, model, until, reason)
        log.info(audit_line(
            "provider_feedback", v=ctx.version, rid=ctx.request_id,
            initiator=ctx.initiator or "-", comment=ctx.comment or "-",
            key=key_id, model=model, http=signal.status, kind=kind, reason=reason,
            hits=hits, promoted=int(promoted),
            until=datetime.fromtimestamp(until, UTC).isoformat(timespec="seconds"),
            retry_after_s=round(signal.retry_after_s or 0, 1),
            quota_id=signal.quota_id or "-", detail=signal.detail or "-"))
        return row
