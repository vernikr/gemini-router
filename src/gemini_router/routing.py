"""Router: cascading chain, key pool, ledger-backed budget gates, wait policy (FR-C13),
overflow tiers and request cache. Job/trace identity flows into every ledger row."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx

from .config import TTS_MODEL, Config, estimate_tokens, key_id
from .context import RequestContext, router_version
from .errors import (
    FileNotReady,
    GeminiHTTPError,
    QuotaExhaustedError,
    RouterError,
)
from .events import RouterEvent
from .feedback import QuotaTruth
from .gemini import GeminiClient, TTSResult, UploadedFile, file_parts
from .ledger import Ledger, iso, pt_midnight_ts
from .tariffs import media_class
from .tts import MAX_INPUT_TOKENS, WavAssembler, encode_mp3, split_text

Emitter = Callable[[RouterEvent], None]
_TTS_PROGRESS_INTERVAL_S = 1.0
_TTS_FALLBACK_TOKENS_PER_SECOND = 32.0


@dataclass(slots=True)
class RouterRequest:
    prompt: str
    system: str | None = None
    thinking_level: str | None = "medium"   # low|medium|high; None omits thinkingConfig
    output_format: str = "text"             # text|json
    max_output_tokens: int | None = None    # None → no cap sent to the provider
    kind: str = "single"                    # single|chunk|consolidate|count
    request_id: str | None = None           # idempotency / cache lookup
    source: str = "cli"                     # transport/entry point: mcp|cli|rest|filesearch
    # Who actually asked. Free-form, supplied by the initiator itself; defaults to
    # `source` when omitted. Stored in every ledger row.
    initiator: str | None = None
    # Arbitrary operator text. Logged and stored, never used in a routing decision.
    comment: str = ""
    # Target activity (e.g. "job-42"), one execution of it, and the
    # client's stage inside that execution. All rows of the job share `job`.
    job: str | None = None
    trace_id: str | None = None
    step: str = ""
    overflow: str | None = None             # off|lite|lite+gemma; None → cfg.overflow_default
                                            # (FR-C8 stays opt-in: that default is "off")
    wait: bool | None = None                # None → cfg.wait.enabled_default (FR-C13)
    model: str | None = None                # pin → skips chain fallback
    est_tokens: int | None = None           # caller-provided countTokens result


@dataclass(slots=True)
class RouterResult:
    text: str
    model: str
    key_id: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_s: float = 0.0
    cached: bool = False
    overflow_tier: int = 0
    finish_reason: str | None = None         # empty-answer diagnostics
    tokens_thought: int | None = None
    events: list[RouterEvent] = field(default_factory=list)
    quota_after: list[dict] = field(default_factory=list)
    request_id: str | None = None
    row_id: int | None = None
    job: str | None = None
    trace_id: str | None = None
    step: str = ""
    files_used: list[UploadedFile] = field(default_factory=list)  # actual key's uploads


@dataclass(slots=True)
class TTSFileResult:
    path: Path
    model: str
    chunks: int
    tokens_in: int
    key_ids: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    mime_type: str = "audio/mpeg"


def gates_from(cfg: Config, model: str, kid: str, spent: tuple[int, int],
               window: tuple[int, int, float | None], cd: float | None,
               est_tokens: int, now: float, *, provider_state: dict | None = None) -> dict:
    """The gate arithmetic (FR-C2), fed either by the per-pair ledger accessors or by
    a Ledger.gate_snapshot. wait_s>0 means a minute-scale gate (RPM/TPM/cooldown) —
    waitable per FR-C13; rpd_left==0 means day-scale — never waited.

    `provider_state` is the authority: a live 429 with a PerDay quota id sets
    rpd_left=0 no matter how fresh our counters look. A minute/overload verdict is a
    waitable cooldown (it also lands in `cooldowns` for older readers).
    """
    lim = cfg.limits_for(model)
    spent_day, spent_roll = spent
    calls, toks, oldest = window
    state = provider_state or {}
    kind = state.get("kind")
    state_until = state.get("until_ts") or 0.0
    live = state_until > now
    day_block = bool(live and kind == "day")
    expire_in = max(0.0, oldest + cfg.window_s - now) if oldest is not None else 0.0
    rpm_wait = expire_in if (lim.rpm and calls >= lim.rpm) else 0.0
    tpm_wait = expire_in if (est_tokens and lim.tpm and toks + est_tokens > lim.tpm) else 0.0
    cd_wait = max(0.0, cd - now) if cd else 0.0
    if live and kind in ("minute", "overload"):
        cd_wait = max(cd_wait, state_until - now)
    waits = {"rpm": rpm_wait, "tpm": tpm_wait, "cooldown": cd_wait}
    wait_s = max(waits.values())
    return {"key_id": kid, "model": model,
            "rpd_left": 0 if day_block else max(0, lim.rpd - max(spent_day, spent_roll)),
            "rpd_left_day": max(0, lim.rpd - spent_day),
            "rpd_left_roll": max(0, lim.rpd - spent_roll),
            "rpd_limit": lim.rpd, "rpm_used": calls, "rpm_limit": lim.rpm,
            "tpm_used": toks, "tpm_limit": lim.tpm,
            "rpm_wait_s": round(rpm_wait, 1), "tpm_wait_s": round(tpm_wait, 1),
            "cooldown_until": max([x for x in (cd, state_until if live else None) if x],
                                 default=None),
            "wait_s": round(wait_s, 1),
            "reason": max(waits, key=waits.get) if wait_s > 0 else None,
            "provider_blocked": kind if live and not day_block else ("day" if day_block else None),
            "provider_reason": state.get("reason") if live else None,
            "provider_until": state_until if live else None,
            "provider_hits": state.get("hits") if live else None}


def gates_for(cfg: Config, ledger: Ledger, model: str, key: str,
              est_tokens: int = 0, now: float | None = None) -> dict:
    """Budget gates for one key×model pair, read straight from the ledger."""
    now = now or time.time()
    kid = key_id(key)
    return gates_from(cfg, model, kid,
                      ledger.rpd_spent(kid, model, pt_midnight_ts(cfg.quota_day_tz, now),
                                       now - 86400, now - cfg.planned_ttl_s),
                      ledger.window_usage(kid, model, now - cfg.window_s,
                                          now - cfg.planned_ttl_s),
                      ledger.cooldown(kid, model, now), est_tokens, now,
                      provider_state=ledger.provider_state(kid, model, now))


def cache_hash(model: str, req: RouterRequest, mot: int | None,
               file_hashes: list[str] | None = None) -> str:
    """Content only. File bytes enter as their local sha256, never as the provider URI:
    a URI changes on every re-upload (after expiry, or under another key) and would turn
    every hit into a miss for work already done."""
    payload = json.dumps([model, req.thinking_level, req.output_format, mot,
                          req.system, req.prompt, file_hashes], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _sha256(path: str | Path) -> str:
    """One pass over the blob, streamed: the cache key's file half."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while (block := fh.read(1 << 20)):
            h.update(block)
    return h.hexdigest()


def tier_plan(cfg: Config, *, model_pin: str | None = None,
              overflow: str = "off") -> list[tuple[int, list[str]]]:
    """The model tiers every entry point walks — defined once (owner's rule: parameter
    logic must not depend on which door a request came through).

    Tier 0 is the chain (or the pinned model), tier 1 the literal overflow models,
    tier 2 the `"gemma-auto"` placeholder — the caller resolves it against listModels
    (`pick_gemma`), because availability knowledge lives with the caller, not here.
    """
    chain = [model_pin] if model_pin else list(dict.fromkeys(cfg.chain))
    tiers = [(0, chain)]
    if overflow in ("lite", "lite+gemma"):
        tiers.append((1, [m for m in cfg.overflow_tiers if m != "gemma-auto"]))
    if overflow == "lite+gemma":
        tiers.append((2, ["gemma-auto"]))
    return tiers


def keys_sorted(cfg: Config, ledger: Ledger, model: str, *,
                keys: list[str] | None = None, now: float | None = None) -> list[str]:
    """The key order every entry point uses: largest remaining day budget first.
    `keys` narrows the pool (resource affinity); None means the whole pool."""
    now = now or time.time()
    pool = list(keys) if keys is not None else list(cfg.api_keys)
    return sorted(pool,
                  key=lambda k: -gates_for(cfg, ledger, model, k, 0, now)["rpd_left"])


@dataclass(slots=True)
class PairChoice:
    """One (model, key) answer from the shared walk, for single-shot callers."""
    model: str | None
    key: str | None
    gates: dict | None
    tier: int = 0
    reason: str = "exhausted"               # ok | wait | throttled | exhausted
    retry_after_s: float | None = None      # for `throttled`: shortest provider reset


def pick_pair(cfg: Config, ledger: Ledger, *, overflow: str = "off",
              chain_override: list[str] | None = None, keys: list[str] | None = None,
              avail: dict[str, set[str] | None] | None = None, est_tokens: int = 0,
              now: float | None = None,
              exclude: set[tuple[str, str]] | None = None) -> PairChoice:
    """The same walk `Router.complete` makes — tiers from `tier_plan`, keys from
    `keys_sorted`, gates from `gates_for` — reduced to one up-front answer for callers
    that forward a single request instead of running an attempt loop (the pass-through
    proxy, SPEC §11.6).

    `chain_override` substitutes tier 0 (the proxy's `file_search_models`); `keys`
    narrows the pool (resource affinity); `avail` is an optional key_id → listModels
    filter; `exclude` are pairs already tried **in this request** — without it a walk
    can re-select the same exhausted pair on every hop, which is how 48 identical 429s
    were produced in ten seconds. Preference: a pair that can run now ("ok"); else the
    budgeted pair with the shortest minute-scale wait ("wait"); else "throttled" when
    provider feedback blocked everything (with the shortest reset), else "exhausted".
    """
    now = now or time.time()
    pool = list(keys) if keys is not None else list(cfg.api_keys)
    best_wait: PairChoice | None = None
    provider_resets: list[float] = []
    clean_pair_seen = False
    tiers = tier_plan(cfg, overflow=overflow)
    if chain_override:
        tiers[0] = (0, list(dict.fromkeys(chain_override)))
    for tier, models in tiers:
        if models == ["gemma-auto"]:
            union: set[str] = set()
            for s in (avail or {}).values():
                if s:
                    union |= s
            g = pick_gemma(union)
            models = [g] if g else []
        for model in models:
            for key in keys_sorted(cfg, ledger, model, keys=pool, now=now):
                pair = (key_id(key), model)
                if exclude and pair in exclude:
                    continue
                s = (avail or {}).get(key_id(key))
                if s is not None and model not in s:
                    continue
                gates = gates_for(cfg, ledger, model, key, est_tokens, now)
                if gates["provider_until"]:
                    provider_resets.append(gates["provider_until"] - now)
                if gates["rpd_left"] <= 0:
                    continue
                if not gates["provider_blocked"]:
                    clean_pair_seen = True
                if gates["wait_s"] <= 0:
                    return PairChoice(model, key, gates, tier, "ok")
                if best_wait is None or gates["wait_s"] < best_wait.gates["wait_s"]:
                    best_wait = PairChoice(model, key, gates, tier, "wait",
                                           retry_after_s=max(0.0, gates["wait_s"]))
    if not clean_pair_seen and provider_resets:
        # Every surviving pair is benched by the provider itself. Re-asking Google
        # would only replay the same 429s; hand the caller a bounded Retry-After.
        return PairChoice(None, None, None, 0, "throttled",
                          retry_after_s=max(1.0, min(provider_resets)))
    if best_wait is not None:
        return best_wait
    if provider_resets:
        return PairChoice(None, None, None, 0, "throttled",
                          retry_after_s=max(1.0, min(provider_resets)))
    return PairChoice(None, None, None, 0, "exhausted")


def pick_gemma(models: Iterable[str]) -> str | None:
    cands = [m for m in models if m.lower().startswith("gemma")]
    cands = [m for m in cands if m.lower().endswith("-it")] or cands
    if not cands:
        return None
    return max(cands, key=lambda m: tuple(int(x) for x in re.findall(r"\d+", m)))


class Router:
    def __init__(self, cfg: Config, *, ledger: Ledger | None = None,
                 client: GeminiClient | None = None,
                 sleep: Callable[[float], object] | None = None,
                 now: Callable[[], float] | None = None):
        if not cfg.api_keys:
            raise RouterError("no API keys: set GEMINI_API_KEYS or GEMINI_API_KEY_1..10")
        self.cfg = cfg
        self.ledger = ledger or Ledger(cfg.state_path)
        self.client = client or GeminiClient(cfg)
        self.truth = QuotaTruth(self.ledger, cfg, now_fn=now or time.time)
        self._sleep = sleep or asyncio.sleep
        self._now = now or time.time
        self._avail: tuple[float, dict[str, set[str] | None]] | None = None
        # A refusal is per media class × model × key: another key may serve the same
        # model/file combination. Never turn one account's 400/500 into a global ban.
        self._file_incapable: dict[str, set[tuple[str, str]]] = {}

    @classmethod
    def from_env(cls, config_path: str | None = None, **over) -> Router:
        return cls(Config.load(config_path, **over))

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ info
    async def availability(self) -> dict[str, set[str] | None]:
        """key_id → that project's live model set (None = probe failed; trust config).

        API keys may belong to different Google projects and tiers, so each key is
        checked independently. Results are cached for `models_refresh_h`.
        """
        return await self._availability(per_key=True)

    async def availability_per_key(self) -> dict[str, set[str] | None]:
        """Explicit alias for callers that want to emphasize per-project probing."""
        return await self._availability(per_key=True)

    async def _availability(self, *, per_key: bool) -> dict[str, set[str] | None]:
        now = self._now()
        if self._avail and now - self._avail[0] < self.cfg.models_refresh_h * 3600:
            return self._avail[1]
        if not self.cfg.api_keys:
            self._avail = (now, {})
            return self._avail[1]
        per: dict[str, set[str] | None] = {}
        keys = self.cfg.api_keys if per_key else self.cfg.api_keys[:1]
        models: set[str] | None = None
        for key in keys:
            try:
                models = await self.client.list_models(key)
            except Exception:  # noqa: BLE001 — offline is survivable (D-b04)
                models = None
            per[key_id(key)] = models
            if models is not None and not per_key:
                break
        if not per_key:
            # Every other key gets the same answer: same account tier, same list. A key
            # is not marked dead here — refusing a key is generateContent's job (2.1),
            # and listModels failing on one says nothing about the others.
            for key in self.cfg.api_keys:
                per.setdefault(key_id(key), models)
        self._avail = (now, per)
        return per

    async def _safe_avail(self) -> dict[str, set[str] | None]:
        try:
            return await self.availability()
        except Exception:  # noqa: BLE001
            return {}

    def _snapshot(self, now: float) -> dict:
        return self.ledger.gate_snapshot(
            day_start=pt_midnight_ts(self.cfg.quota_day_tz, now), roll_start=now - 86400,
            win_start=now - self.cfg.window_s, planned_min=now - self.cfg.planned_ttl_s,
            now=now)

    def _gates_from_snap(self, snap: dict, model: str, key: str, now: float) -> dict:
        kid = key_id(key)
        return gates_from(self.cfg, model, kid,
                          snap["spent"].get((kid, model), (0, 0)),
                          snap["window"].get((kid, model), (0, 0, None)),
                          snap["cooldown"].get((kid, model)), 0, now,
                          provider_state=snap.get("pair_state", {}).get((kid, model)))

    def quota_table(self, now: float | None = None) -> list[dict]:
        """Per key×model budget rows for chain + literal overflow tiers (BR3)."""
        now = now or self._now()
        models = list(dict.fromkeys(
            self.cfg.chain + [m for m in self.cfg.overflow_tiers if m != "gemma-auto"]
            + [TTS_MODEL]))
        snap = self._snapshot(now)
        rows = [self._gates_from_snap(snap, m, k, now)
                for m in models for k in self.cfg.api_keys]
        rec = next((r for r in rows if r["rpd_left"] > 0 and r["wait_s"] <= 0), None)
        for r in rows:
            r["recommended"] = rec is not None and r is rec
        return rows

    def model_budgets(self, models: list[str] | None = None,
                      now: float | None = None) -> list[dict]:
        """Per-model aggregation across keys — for result meta and FR-B8 payloads."""
        now = now or self._now()
        out, snap = [], None
        for model in dict.fromkeys(models or self.cfg.chain):
            snap = self._snapshot(now) if snap is None else snap
            gs = [self._gates_from_snap(snap, model, k, now) for k in self.cfg.api_keys]
            out.append({"model": model, "rpd_left": sum(g["rpd_left"] for g in gs),
                        "rpm_wait_s": min((g["rpm_wait_s"] for g in gs), default=0),
                        "tpm_wait_s": min((g["tpm_wait_s"] for g in gs), default=0),
                        "cooldown_until": min((g["cooldown_until"] for g in gs
                                               if g["cooldown_until"]), default=None),
                        "best_wait_s": min((g["wait_s"] for g in gs if g["rpd_left"] > 0),
                                           default=None)})
        return out

    async def count_tokens(self, prompt: str, model: str | None = None, *,
                           job: str | None = None, trace_id: str | None = None,
                           step: str | None = None, initiator: str | None = None,
                           request_id: str | None = None, comment: str | None = None) -> int:
        """countTokens pre-flight; kind='count' never spends RPD (FR-C7)."""
        avail = await self._safe_avail()
        chosen = None
        for m in ([model] if model else self.cfg.chain):
            for key in self.cfg.api_keys:
                s = avail.get(key_id(key))
                if s is None or m in s:
                    chosen = (m, key)
                    break
            if chosen:
                break
        if not chosen:
            raise RouterError("no available model/key for countTokens")
        m, key = chosen
        n = await self.client.count_tokens(key, m, prompt)
        row = self.ledger.plan(key_id=key_id(key), model=m, kind="count", est_tokens=n,
                               request_id=request_id, source="count",
                               initiator=initiator or "count", comment=comment,
                               version=router_version(), job=job or "count",
                               trace_id=trace_id, step=step)
        self.ledger.resolve(row, "ok", tokens_in=n, tokens_out=0)
        return n

    async def synthesize(self, text: str, *, voice: str | None = None,
                         style: str | None = None, wait: bool | None = None,
                         source: str = "cli", initiator: str | None = None,
                         request_id: str | None = None, job: str | None = None,
                         trace_id: str | None = None, step: str = "tts",
                         on_event: Emitter | None = None,
                         _input_tokens: int | None = None) -> TTSResult:
        """Synthesize one bounded text segment with the shared model/key ledger.

        TTS has its own fixed model and quota table, but reuses the router's key order,
        gates, provider feedback, wait policy, retries and SQLite audit.
        """
        if not text or not text.strip():
            raise ValueError("TTS text must not be empty")
        model = TTS_MODEL
        today = iso(self._now())[:10]
        default_job = f"{initiator or source}@{today}"
        ctx = RequestContext(initiator=initiator or source, source=source,
                             request_id=request_id or uuid.uuid4().hex[:12],
                             job=job or default_job,
                             trace_id=trace_id or job or default_job, step=step)

        def emit(stage: str, **kwargs) -> None:
            if on_event:
                on_event(RouterEvent(stage=stage, model=model, **kwargs))

        # countTokens is the provider's input-token count and is recorded as a
        # non-spending ledger event. TPM gates count input only; audio tokens are output.
        if _input_tokens is not None:
            est = _input_tokens
        else:
            try:
                est = await self.count_tokens(text, model=model, job=ctx.job,
                                              trace_id=ctx.trace_id, step=f"{step}:count",
                                              initiator=ctx.initiator, request_id=ctx.request_id)
            except (GeminiHTTPError, RouterError, httpx.HTTPError):
                est = estimate_tokens(text)
        if est > MAX_INPUT_TOKENS:
            raise RouterError(f"TTS segment is {est} input tokens; the model limit is "
                              f"{MAX_INPUT_TOKENS}. Use synthesize_file() to split it.")

        available = await self._safe_avail()
        keys = self._keys_sorted(model)
        if available:
            keys = [k for k in keys if available.get(key_id(k)) is None
                    or model in (available.get(key_id(k)) or set())]
        if not keys:
            raise RouterError(f"{model} is unavailable for every configured project")
        wait_enabled = self.cfg.wait.enabled_default if wait is None else wait
        deadline = self._now() + self.cfg.wait.max_s + self.cfg.retry.provider_timeout_s
        visited: set[str] = set()
        dead_keys: set[str] = set()
        failures: list[str] = []
        while True:
            candidates = [k for k in keys if key_id(k) not in visited
                          and key_id(k) not in dead_keys]
            if not candidates:
                # A gate/provider cooldown is the only reason to revisit. Permanently
                # rejected credentials are excluded for the rest of this logical call.
                candidates = [k for k in keys if key_id(k) not in dead_keys]
                visited.clear()
            if not candidates:
                raise RouterError("every configured Gemini API key was rejected")
            gates = [(k, self._gates(model, k, est)) for k in candidates]
            usable = [(k, g) for k, g in gates if g["rpd_left"] > 0 and g["wait_s"] <= 0]
            if usable:
                key, gate = usable[0]
            else:
                minute = [(k, g) for k, g in gates if g["rpd_left"] > 0 and g["wait_s"] > 0]
                if not minute:
                    payload = self.model_budgets([model])
                    eta = iso(pt_midnight_ts(self.cfg.quota_day_tz, days_ahead=1))
                    raise QuotaExhaustedError(
                        payload, eta, hint="TTS daily request quota exhausted across the key pool",
                        waited=False, failures=failures)
                key, gate = min(minute, key=lambda item: item[1]["wait_s"])
                if not wait_enabled or gate["wait_s"] > self.cfg.wait.max_s:
                    payload = self.model_budgets([model])
                    eta = iso(pt_midnight_ts(self.cfg.quota_day_tz, days_ahead=1))
                    raise QuotaExhaustedError(payload, eta,
                                              hint=f"TTS minute quota; retry in about "
                                                   f"{gate['wait_s']:.0f}s",
                                              waited=False, failures=failures)
                emit("waiting", key_id=key_id(key), wait_s=gate["wait_s"],
                     detail={"reason": gate["reason"]})
                await self._sleep(min(gate["wait_s"], max(0.05, deadline - self._now())))
                if self._now() >= deadline:
                    raise RouterError("TTS quota wait exceeded the request deadline")
                continue

            kid = key_id(key)
            visited.add(kid)
            gap = max(self.cfg.retry.request_delay_s,
                      self.cfg.window_s / max(1, self.cfg.limits_for(model).rpm))
            last = self.ledger.last_call_ts(kid, model)
            if last is not None and (self._now() - last) < gap:
                delay = gap - (self._now() - last)
                emit("pacing", key_id=kid, wait_s=delay,
                     detail={"reason": "tts_rpm_smoothing"})
                await self._sleep(delay)

            row_id = self.ledger.plan(key_id=kid, model=model, kind="single", est_tokens=est,
                                      request_id=ctx.request_id, source=source,
                                      initiator=ctx.initiator, version=ctx.version,
                                      job=ctx.job, trace_id=ctx.trace_id, step=step)
            emit("dispatch", key_id=kid, detail={"row_id": row_id, "est_tokens": est})
            t0 = self._now()
            try:
                result = await self.client.synthesize(key, model, text,
                                                     voice=voice, style=style)
            except GeminiHTTPError as exc:
                elapsed = int((self._now() - t0) * 1000)
                if exc.status in (429, 503):
                    signal = exc.signal or self.truth_fallback(exc)
                    self.ledger.resolve(row_id, "quota" if exc.status == 429 else "http5xx",
                                        http_status=exc.status, latency_ms=elapsed,
                                        quota_error=str(exc), retry_after_s=signal.retry_after_s,
                                        quota_id=signal.quota_id, signal_kind=signal.kind,
                                        provider_reason=signal.reason)
                    self.truth.apply(signal, key_id=kid, model=model, context=ctx)
                    failures.append(f"{kid}: HTTP {exc.status}: {exc}")
                    emit("switch", key_id=kid, detail={"reason": signal.reason or exc.status})
                    if signal.kind == "minute" and wait_enabled:
                        visited.discard(kid)
                    continue
                self._resolve_err(row_id, exc, elapsed)
                failures.append(f"{kid}: {exc}")
                if exc.key_rejected:
                    dead_keys.add(kid)
                if exc.switch_model or exc.retryable or exc.key_rejected:
                    emit("switch", key_id=kid, detail={"reason": f"http_{exc.status}"})
                    continue
                raise
            except httpx.HTTPError as exc:
                self.ledger.resolve(row_id, "network", latency_ms=int((self._now() - t0) * 1000),
                                    quota_error=type(exc).__name__)
                failures.append(f"{kid}: network {type(exc).__name__}")
                emit("switch", key_id=kid, detail={"reason": "network"})
                continue
            except BaseException as exc:
                self.ledger.resolve(row_id, "blocked", latency_ms=int((self._now() - t0) * 1000),
                                    quota_error=type(exc).__name__)
                raise

            measured = result.tokens_in if result.tokens_in is not None else est
            elapsed = int((self._now() - t0) * 1000)
            self.ledger.resolve(row_id, "ok", http_status=200, tokens_in=measured,
                                tokens_out=result.tokens_out, latency_ms=elapsed,
                                response_bytes=len(result.audio))
            self.ledger.clear_pair_state(kid, model)
            result.key_id = kid
            emit("ok", key_id=kid, detail={"tokens_in": measured,
                 "tokens_out": result.tokens_out, "latency_s": round(elapsed / 1000, 1)})
            return result

    async def synthesize_file(self, path: str | Path, *, output_path: str | Path | None = None,
                              voice: str | None = None, style: str | None = None,
                              bitrate_kbps: int = 128, wait: bool | None = None,
                              source: str = "cli", initiator: str | None = None,
                              request_id: str | None = None, job: str | None = None,
                              trace_id: str | None = None,
                              on_event: Emitter | None = None) -> TTSFileResult:
        """Read UTF-8 TXT/Markdown, split at paragraphs then sentences, synthesize in
        order, concatenate WAV pieces on disk, and encode one MP3 asynchronously."""
        def report_progress(pct: float, label: str, **detail) -> None:
            if on_event:
                on_event(RouterEvent(stage="file_progress", pct=pct,
                                     detail={"label": label, **detail}))

        report_progress(0, "Preparing audio · checking input", phase="preflight")
        source_path = Path(path)
        if source_path.suffix.lower() not in {".txt", ".md"}:
            raise ValueError("TTS file input supports only .txt and .md")
        try:
            text = await asyncio.to_thread(source_path.read_text, encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("TTS input must be UTF-8 text") from exc
        if not shutil.which("ffmpeg"):
            raise RouterError("MP3 encoding requires ffmpeg on PATH (no TTS quota spent)")
        if not 32 <= bitrate_kbps <= 320:
            raise ValueError("MP3 bitrate must be between 32 and 320 kbps")
        chunks = split_text(text)
        measured_chunks: list[tuple[str, int]] = []
        pending = list(chunks)
        while pending:
            chunk = pending.pop(0)
            try:
                count = await self.count_tokens(chunk, model=TTS_MODEL,
                                                job=job, trace_id=trace_id,
                                                step="tts:preflight", initiator=initiator,
                                                request_id=request_id)
            except (GeminiHTTPError, RouterError, httpx.HTTPError):
                count = estimate_tokens(chunk)
            if count <= MAX_INPUT_TOKENS:
                measured_chunks.append((chunk, count))
                continue
            smaller = split_text(chunk, max_tokens=max(1, int(MAX_INPUT_TOKENS * 0.8)))
            if len(smaller) <= 1:
                raise RouterError("could not split an oversized TTS sentence below the "
                                  f"{MAX_INPUT_TOKENS}-token request limit")
            pending = smaller + pending
        chunks = [chunk for chunk, _ in measured_chunks]
        avail = await self._safe_avail()
        eligible = [k for k in self.cfg.api_keys
                    if avail.get(key_id(k)) is None
                    or TTS_MODEL in (avail.get(key_id(k)) or set())]
        remaining_requests = sum(self._gates(TTS_MODEL, k, 0)["rpd_left"] for k in eligible)
        if remaining_requests < len(chunks):
            payload = self.model_budgets([TTS_MODEL])
            eta = iso(pt_midnight_ts(self.cfg.quota_day_tz, days_ahead=1))
            raise QuotaExhaustedError(
                payload, eta,
                hint=f"file needs {len(chunks)} TTS requests; only "
                     f"{remaining_requests} daily requests remain across usable projects",
                waited=False)
        chunk_sizes = [max(1, count) for _, count in measured_chunks]
        total_work = sum(chunk_sizes)
        estimated_tokens_per_second = _TTS_FALLBACK_TOKENS_PER_SECOND
        observed_chunks = 0
        completed_work = 0
        last_progress = 0.0
        initial_eta = sum(
            max(8.0, size / estimated_tokens_per_second) for size in chunk_sizes
        )
        report_progress(
            0, f"TTS · 0/{len(chunks)}", phase="synthesis",
            completed_chunks=0, total_chunks=len(chunks),
            eta_s=max(1, round(initial_eta)),
        )
        target = Path(output_path) if output_path else Path(tempfile.gettempdir()) / (
            f"gemini-tts-{uuid.uuid4().hex}.mp3")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(f"TTS output already exists: {target}")
        wav_path = target.with_name(f".{target.name}.wav.tmp")
        assembler = WavAssembler(wav_path)
        used_keys: list[str] = []
        total_tokens = 0
        started = self._now()
        chunk_count = len(chunks)
        try:
            for i, (chunk, token_count) in enumerate(measured_chunks, 1):
                chunk_started = time.monotonic()
                chunk_index = i
                chunk_size = chunk_sizes[i - 1]
                later_sizes = chunk_sizes[i:]
                later_work = sum(later_sizes)
                chunk_state = {"waiting_started": None, "waiting_duration": 0.0}

                async def publish_chunk_progress(
                    state=chunk_state,
                    started_at=chunk_started,
                    current_size=chunk_size,
                    throughput=estimated_tokens_per_second,
                    future_sizes=later_sizes,
                    index=chunk_index,
                    done_work=completed_work,
                    work_total=total_work,
                    chunks_total=chunk_count,
                ) -> None:
                    nonlocal last_progress
                    while True:
                        await asyncio.sleep(_TTS_PROGRESS_INTERVAL_S)
                        if state["waiting_started"] is not None:
                            continue
                        elapsed = max(
                            0.05,
                            time.monotonic() - started_at - state["waiting_duration"],
                        )
                        expected_s = max(8.0, current_size / throughput)
                        fraction = min(0.9, 0.75 * elapsed / expected_s)
                        active_rate = max(
                            0.1,
                            min(throughput, current_size * fraction / elapsed),
                        )
                        remaining_current = current_size * (1.0 - fraction)
                        future_eta = sum(
                            max(8.0, size / throughput) for size in future_sizes
                        )
                        eta_s = max(1, round(remaining_current / active_rate + future_eta))
                        pct = min(
                            94.9,
                            95.0 * (done_work + current_size * fraction) / work_total,
                        )
                        last_progress = max(last_progress, pct)
                        report_progress(
                            last_progress, f"TTS · {index}/{chunks_total}",
                            phase="synthesis", completed_chunks=index - 1,
                            total_chunks=chunks_total,
                            active_chunk_pct=round(fraction * 100, 1), eta_s=eta_s,
                        )

                ticker = (
                    asyncio.create_task(publish_chunk_progress()) if on_event else None
                )

                def chunk_event(event: RouterEvent, current_chunk: int = i,
                                total_chunks: int = len(chunks), state=chunk_state) -> None:
                    if event.stage == "waiting":
                        if state["waiting_started"] is None:
                            state["waiting_started"] = time.monotonic()
                    elif state["waiting_started"] is not None:
                        state["waiting_duration"] += (
                            time.monotonic() - state["waiting_started"]
                        )
                        state["waiting_started"] = None
                    if on_event:
                        event.detail.setdefault("chunk", current_chunk)
                        event.detail.setdefault("chunks", total_chunks)
                        on_event(event)

                try:
                    res = await self.synthesize(
                        chunk, voice=voice, style=style, wait=wait, source=source,
                        initiator=initiator,
                        request_id=f"{request_id or uuid.uuid4().hex[:12]}-{i}",
                        job=job, trace_id=trace_id, step=f"tts:{i}/{len(chunks)}",
                        on_event=chunk_event if on_event else None, _input_tokens=token_count)
                finally:
                    if ticker is not None:
                        ticker.cancel()
                        try:
                            await ticker
                        except asyncio.CancelledError:
                            pass

                chunk_elapsed = max(
                    0.1,
                    time.monotonic() - chunk_started - chunk_state["waiting_duration"],
                )
                measured_rate = chunk_size / chunk_elapsed
                estimated_tokens_per_second = max(
                    0.1,
                    min(
                        120.0,
                        measured_rate if observed_chunks == 0
                        else 0.35 * estimated_tokens_per_second + 0.65 * measured_rate,
                    ),
                )
                observed_chunks += 1
                await asyncio.to_thread(assembler.append, res.audio)
                total_tokens += res.tokens_in or estimate_tokens(chunk)
                if res.key_id and res.key_id not in used_keys:
                    used_keys.append(res.key_id)
                completed_work += chunk_size
                completed_pct = 95.0 * completed_work / total_work
                last_progress = max(last_progress, completed_pct)
                future_eta = (
                    max(1, round(sum(
                        max(8.0, size / estimated_tokens_per_second)
                        for size in later_sizes
                    )))
                    if later_work else None
                )
                report_progress(
                    last_progress, f"TTS · {i}/{len(chunks)}",
                    phase="synthesis", completed_chunks=i, total_chunks=len(chunks),
                    eta_s=future_eta,
                )
            assembler.close()
            report_progress(95, "Encoding MP3", phase="encoding",
                            completed_chunks=len(chunks), total_chunks=len(chunks))

            encode_started = time.monotonic()

            def encode_progress(fraction: float) -> None:
                nonlocal last_progress
                fraction = max(0.0, min(1.0, float(fraction)))
                pct = 95.0 + 4.0 * fraction
                last_progress = max(last_progress, pct)
                elapsed = max(0.05, time.monotonic() - encode_started)
                eta_s = (
                    max(1, round(elapsed * (1.0 - fraction) / fraction))
                    if 0.03 <= fraction < 1.0 else (0 if fraction >= 1.0 else None)
                )
                report_progress(
                    last_progress, "Encoding MP3", phase="encoding",
                    completed_chunks=len(chunks), total_chunks=len(chunks),
                    encoding_pct=round(fraction * 100, 1), eta_s=eta_s,
                )

            await encode_mp3(
                wav_path, target, bitrate_kbps=bitrate_kbps,
                on_progress=encode_progress if on_event else None,
            )
            target.chmod(0o600)
            report_progress(100, "Audio complete", phase="complete",
                            completed_chunks=len(chunks), total_chunks=len(chunks))
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            if assembler._writer is not None:
                assembler._writer.close()
            wav_path.unlink(missing_ok=True)
        return TTSFileResult(path=target, model=TTS_MODEL, chunks=len(chunks),
                             tokens_in=total_tokens, key_ids=used_keys,
                             latency_s=self._now() - started)

    # ------------------------------------------------------------------ main
    async def complete(self, req: RouterRequest, on_event: Emitter | None = None,
                       files: list[UploadedFile] | None = None,
                       file_paths: list[str | Path] | None = None, *,
                       stream: bool = False) -> RouterResult:
        """Route files like text when local sources permit re-upload on key switch.

        A provider file is visible only to its uploading key. With matching local
        `file_paths`, re-upload once per new key after quota/availability switching;
        without them keep the pinned URI, never send a foreign URI under a key.
        The gateway supplies local sources for both single and packed PDFs."""
        events: list[RouterEvent] = []
        ctx = RequestContext(initiator=req.initiator or req.source, comment=req.comment,
                             source=req.source, version=router_version(),
                             job=req.job or f"{req.initiator or req.source}@"
                                            f"{iso(self._now())[:10]}",
                             trace_id=req.trace_id or req.job or f"{req.initiator or req.source}@"
                                                                  f"{iso(self._now())[:10]}",
                             step=req.step)
        if req.request_id:
            ctx.request_id = req.request_id

        def emit(stage: str, **kw) -> None:
            ev = RouterEvent(stage=stage, **kw)
            if stage != "text":
                events.append(ev)
            if on_event:
                on_event(ev)

        cfg = self.cfg
        if req.overflow is None:
            # One config key sets the overflow policy for every entry point; an
            # explicit per-request value (any string, including "off") wins.
            req = replace(req, overflow=cfg.overflow_default)
        files = list(files or [])
        if files:
            # The TPM gate must include the file tokens, and only the caller can measure
            # them (count_file_tokens). A text-only estimate would under-count the gate
            # on the one request where being wrong costs a 429 mid-job.
            if not req.est_tokens:
                raise RouterError("est_tokens is required with files: measure the request "
                                  "with count_file_tokens and pass the total")
            if any(f.key_id is None for f in files):
                raise RouterError("files must come from Router.upload_file — without the "
                                  "key_id the request cannot be pinned to their project")
        if file_paths is not None:
            if len(file_paths) != len(files) or not files:
                raise RouterError("file_paths must match the uploaded files one-for-one")
            if any(not Path(p).is_file() for p in file_paths):
                raise RouterError("local file source missing; cannot switch upload key")
        pins = {f.key_id for f in files} if file_paths is None else set()
        uploads_by_key: dict[str, list[UploadedFile]] = {}
        if len({f.key_id for f in files}) == 1:
            uploads_by_key[files[0].key_id] = files
        fhashes = [f.sha256 or "" for f in files] or None
        wait_enabled = req.wait if req.wait is not None else cfg.wait.enabled_default
        mot = req.max_output_tokens          # None means unlimited; nothing substitutes
        est = req.est_tokens or estimate_tokens(req.prompt, cfg.chars_per_token)
        ttl = cfg.cache_ttl_h * 3600

        if req.request_id and req.kind != "count":          # FR-F5 idempotency
            row = self.ledger.cache_get(None, req.request_id, ttl)
            if row:
                emit("ok", model=row["model"], key_id="cache", detail={"cached": True,
                                                                       "by": "request_id"})
                return RouterResult(text=row["response"], model=row["model"], key_id="cache",
                                    tokens_in=row["tokens_in"], tokens_out=row["tokens_out"],
                                    cached=True, events=events, request_id=req.request_id,
                                    job=ctx.job, trace_id=ctx.trace_id, step=ctx.step,
                                    quota_after=self.model_budgets())

        avail = await self._safe_avail()
        emit("routing", detail={"est_tokens": est, "wait": wait_enabled,
                                "pinned": req.model, "overflow": req.overflow})
        t0 = self._now()
        deadline = t0 + cfg.retry.provider_timeout_s + (cfg.wait.max_s if wait_enabled else 0)
        failures: list[str] = []
        quota_blocked = False
        unavailable: set[str] = set()
        dead_keys: set[str] = set()
        failed_upload_keys: set[str] = set()
        tried: list[str] = []

        fclasses = {media_class(f.mime) for f in files} if files else None
        for tier, models in self._tier_plan(req, avail, fclasses):
            if tier > 0:
                emit("overflow", detail={"tier": tier, "models": models})
            for model in models:
                tried.append(model)
                for key in self._keys_sorted(model):
                    kid = key_id(key)
                    if kid in dead_keys or kid in failed_upload_keys or (pins and kid not in pins):
                        continue
                    if fclasses and any((model, kid) in self._file_incapable.get(cls, ())
                                        for cls in fclasses):
                        emit("switch", model=model, key_id=kid,
                             detail={"reason": "file_refused"})
                        continue
                    s = avail.get(kid)
                    if s is not None and model not in s:
                        if model not in unavailable:
                            unavailable.add(model)
                            emit("switch", model=model, key_id=kid,
                                 detail={"reason": "unavailable"})
                        continue
                    serving = uploads_by_key.get(kid) if files else None
                    if files and serving is None and file_paths is not None:
                        # Do not upload for a day-drained key. One copy per key serves
                        # all later model candidates; uploads are free but audited.
                        if self._gates(model, key, est)["rpd_left"] <= 0:
                            quota_blocked = True
                            emit("switch", model=model, key_id=kid,
                                 detail={"reason": "rpd"})
                            continue
                        serving = []
                        try:
                            for original, path in zip(files, file_paths, strict=True):
                                up = await self.upload_file(
                                    path, mime=original.mime, model=model,
                                    source=req.source, key=key, sha256=original.sha256,
                                    context=ctx)
                                serving.append(up.with_resolution(original.media_resolution)
                                               if original.media_resolution else up)
                        except (RouterError, httpx.HTTPError):
                            # A partial multi-file upload must not be used. Best effort
                            # cleanup; the provider TTL handles any failed deletion.
                            failed_upload_keys.add(kid)
                            for up in serving:
                                try:
                                    await self.client.delete_file(key, up.name)
                                except (RouterError, httpx.HTTPError):
                                    pass
                            failures.append(f"{model}: re-upload failed on key {kid}")
                            emit("switch", model=model, key_id=kid,
                                 detail={"reason": "reupload_failed"})
                            continue
                        uploads_by_key[kid] = serving
                        emit("uploading", model=model, key_id=kid,
                             detail={"reason": "key_switch", "files": len(serving)})
                    res, outcome = await self._try_candidate(
                        req, tier, model, key, est, mot, wait_enabled, deadline, emit,
                        failures, dead_keys, serving if files else None, fhashes, stream,
                        context=ctx)
                    if res is not None:
                        res.events = events
                        res.quota_after = self.model_budgets(tried)
                        res.files_used = serving or []
                        res.job = ctx.job
                        res.trace_id = ctx.trace_id
                        res.step = ctx.step
                        res.request_id = res.request_id or ctx.request_id
                        return res
                    if outcome in ("quota", "wait_skip"):
                        quota_blocked = True

        if dead_keys and len(dead_keys) == len({key_id(k) for k in self.cfg.api_keys}):
            raise RouterError(
                "every configured API key was rejected by the provider (HTTP 401, or a "
                "leaked-key block) — delete the rejected keys in the console and update "
                "the configured key source. In a gateway install, `gemini keys` lists the "
                "pool and `gemini keys add <key>` validates a replacement.")
        # A file job that exhausted its file-capable candidates is not a key problem, so
        # this is read before the pin clause; a quota block must not ask for a re-upload
        # under a key that was never the issue.
        if files and not quota_blocked and any(self._file_incapable.get(cls)
                                                for cls in (fclasses or ())):
            raise RouterError(
                "no configured model/key can read this file: " + self._incapable_note() +
                " answered 400/500 to a file request. Capability is per model × media "
                "kind and key. Use a capable chain/overflow model or set "
                "`file_capable_models:` to narrow the candidates.")
        if pins and not failures and not quota_blocked:
            raise RouterError(
                f"no request pinning a file could run: key(s) {sorted(pins)} cannot serve "
                f"{tried[-1] if tried else 'the chain'} now — re-upload under a key that can")
        if req.model and unavailable and not failures:
            raise RouterError(f"pinned model {req.model} is unavailable for all configured keys")
        if quota_blocked or not failures:
            payload = self.model_budgets(tried or None)
            eta = pt_midnight_ts(cfg.quota_day_tz, days_ahead=1)
            waits = [r["best_wait_s"] for r in payload if r["best_wait_s"] and r["rpd_left"] > 0]
            hint = (f"minute gate: retry in ~{min(waits):.0f}s or send wait_for_minute_limits=true"
                    if waits else
                    f"daily quota resets ~{iso(eta)} (PT); opt in with overflow='lite' or "
                    f"'lite+gemma' to continue on weaker models")
            if files and self._file_incapable:
                # Text and file jobs share tiers, but a model that refused this media
                # is remembered and skipped on subsequent requests in this process.
                hint += (" · skipped file-refusing candidates (" +
                         self._incapable_note() + " answered 400/500 to file input)")
            emit("quota_exhausted", detail={"hint": hint})
            raise QuotaExhaustedError(payload, iso(eta), hint=hint, waited=wait_enabled,
                                      failures=failures)
        raise RouterError("all candidates failed: " + "; ".join(failures[-5:]))

    def complete_sync(self, req: RouterRequest, on_event: Emitter | None = None) -> RouterResult:
        """Blocking facade for synchronous callers (SPEC §2)."""
        async def _run() -> RouterResult:
            client = GeminiClient(self.cfg)      # fresh client: bound to this event loop
            saved, self.client = self.client, client
            try:
                return await self.complete(req, on_event)
            finally:
                self.client = saved
                await client.aclose()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_run())
        raise RouterError("complete_sync called inside a running event loop — use await complete()")

    async def upload_file(self, path: str | Path, *, mime: str | None = None,
                          model: str | None = None, source: str = "cli",
                          display_name: str | None = None, key: str | None = None,
                          overflow: str | None = None, sha256: str | None = None,
                          ready_timeout_s: float = 30.0,
                          context: RequestContext | None = None) -> UploadedFile:
        """Upload a blob and hand back the provider record, pinned to the key that will
        serve the analysis. The key is chosen the way `complete` would choose it, minus
        keys whose day is spent or which sit in cooldown — uploading a file for a key
        that cannot then answer spends an upload for nothing.

        For a PDF, pass the same `overflow` policy as the generation request: if
        every chain-head key is day-drained, select a key with Lite/Gemma budget
        instead of refusing the upload before text-style routing can start.
        `sha256` is the local blob's hash for the cache. The file stays ACTIVE."""
        cfg = self.cfg
        if overflow is None:
            overflow = cfg.overflow_default
        target = model or cfg.chain[0]
        if sha256 is None:
            sha256 = _sha256(path)
        if key:
            cands = [key]
        else:
            targets = [target]
            if overflow in ("lite", "lite+gemma"):
                targets += [m for m in cfg.overflow_tiers if m != "gemma-auto"]
            if overflow == "lite+gemma":
                avail = await self._safe_avail()
                union = set().union(*(s for s in avail.values() if s))
                if gemma := pick_gemma(union):
                    targets.append(gemma)
            cands = []
            for m in dict.fromkeys(targets):
                cands = [k for k in self._keys_sorted(m)
                         if (g := self._gates(m, k))["rpd_left"] > 0
                         and not g["cooldown_until"]]
                if cands:
                    break
            if not cands:
                rows = [self._gates(m, k) for m in dict.fromkeys(targets)
                        for k in self._keys_sorted(m)]
                eta = iso(pt_midnight_ts(cfg.quota_day_tz, days_ahead=1))
                raise QuotaExhaustedError(
                    rows, eta, hint="no key can serve the requested chain/overflow "
                    "models now; retry after the reset or free a key")
        last_err: Exception | None = None
        for k in cands:
            t0 = self._now()
            try:
                up = await self.client.upload_file(k, path, mime=mime,
                                                   display_name=display_name)
                if up.state != "ACTIVE":     # finalize usually already says ACTIVE: no poll
                    up = await self.client.wait_file_active(k, up.name,
                                                            timeout_s=ready_timeout_s)
            except GeminiHTTPError as e:
                last_err = e
                if e.key_rejected or e.switch_model or e.retryable:
                    continue                      # next key, or the same one tried again
                raise
            except FileNotReady:
                raise
            kid = key_id(k)
            ms = int((self._now() - t0) * 1000)
            ctx = context or RequestContext(initiator=source, source=source,
                                            version=router_version())
            self.ledger.record_upload(key_id=kid, nbytes=up.size,
                                      source=ctx.initiator or source, latency_ms=ms,
                                      initiator=ctx.initiator, comment=ctx.comment or None,
                                      version=ctx.version, job=ctx.job,
                                      trace_id=ctx.trace_id, step=ctx.step or None)
            return replace(up, sha256=sha256, key_id=kid)
        raise RouterError(f"no key accepted the upload: {last_err}")

    async def count_file_tokens(self, model: str, uri: str, mime: str, *,
                               key_id: str | None = None,
                               media_resolution: str | None = None) -> int:
        """Exact cost of a file for a model. Free (FR-C7) and key-agnostic, so any live
        key answers; a rejected one moves the call to the next. Pass the SAME
        `media_resolution` the analysis request will use: the count depends on it."""
        for k in self._candidates(1 if key_id else None, key_id, model):
            try:
                return await self.client.count_file_tokens(k, model, uri, mime,
                                                           media_resolution)
            except GeminiHTTPError as e:
                if e.key_rejected or e.switch_model:
                    continue
                raise
        raise RouterError("no key could count file tokens")

    async def delete_file(self, name: str, *, key_id: str | None = None) -> None:
        """Drop a provider-side file. Best-effort: expiry is the provider's own job."""
        for k in self._candidates(1 if key_id else None, key_id, self.cfg.chain[0]):
            try:
                await self.client.delete_file(k, name)
                return
            except GeminiHTTPError as e:
                if e.key_rejected or e.switch_model:
                    continue
                raise
        raise RouterError("no key could delete the file")

    def _candidates(self, limit: int | None, key_id_: str | None, model: str) -> list[str]:
        keys = self.cfg.api_keys
        if key_id_:
            keys = [k for k in keys if key_id(k) == key_id_] or keys
        elif limit:
            keys = self._keys_sorted(model)[:limit]
        return list(keys)

    # ------------------------------------------------------------------ internals
    def _incapable_note(self) -> str:
        """Model/class/masked-key triples for learned file refusals, stable order."""
        return ", ".join(f"{model} ({cls}, key {kid})"
                         for cls, pairs in sorted(self._file_incapable.items())
                         for model, kid in sorted(pairs))

    def _tier_plan(self, req: RouterRequest, avail: dict,
                   file_classes: set[str] | None = None) -> list[tuple[int, list[str]]]:
        # The tier order itself comes from the shared `tier_plan` (one walk for every
        # entry point); this method only resolves the gemma placeholder against the
        # availability this Router holds, and narrows by file class.
        tiers = []
        for tier, models in tier_plan(self.cfg, model_pin=req.model,
                                      overflow=req.overflow or "off"):
            if models == ["gemma-auto"]:
                union: set[str] = set()
                for s in avail.values():
                    if s:
                        union |= s
                g = pick_gemma(union)
                if g:
                    tiers.append((tier, [g]))
                continue
            tiers.append((tier, models))
        if file_classes:
            # PDF-containing jobs use text's tier order. Other media keep the
            # chain-only policy; Gemma refuses PNG file_data.
            # Allowlist narrowing is global; learned refusals are per key.
            if "pdf" not in file_classes:
                tiers = tiers[:1]
            for cls in sorted(file_classes):
                allow = self.cfg.file_capable_models
                allow = allow.get(cls) if isinstance(allow, dict) else allow
                if allow:
                    tiers = [(tier, [m for m in models if m in allow])
                             for tier, models in tiers]
        return tiers

    def _keys_sorted(self, model: str) -> list[str]:
        return keys_sorted(self.cfg, self.ledger, model, now=self._now())

    def _gates(self, model: str, key: str, est_tokens: int = 0) -> dict:
        return gates_for(self.cfg, self.ledger, model, key, est_tokens, self._now())

    def truth_fallback(self, e: GeminiHTTPError):
        """Old/foreign exceptions carry no parsed signal — rebuild one from the text."""
        from .feedback import parse_provider_signal
        return parse_provider_signal(e.status or 429, {}, str(e), self.cfg,
                                     now=self._now())

    async def _try_candidate(self, req: RouterRequest, tier: int, model: str, key: str,
                             est: int, mot: int | None, wait_enabled: bool, deadline: float,
                             emit: Emitter, failures: list[str], dead_keys: set[str],
                             files: list[UploadedFile] | None = None,
                             fhashes: list[str] | None = None, stream: bool = False,
                             context: RequestContext | None = None):
        cfg = self.cfg
        ctx = context or RequestContext(source=req.source, version=router_version())
        kid = key_id(key)
        g = self._gates(model, key, est)
        if g["rpd_left"] <= 0:                                   # daily limits cannot be waited out
            emit("switch", model=model, key_id=kid, detail={"reason": "rpd"})
            return None, "quota"

        h = cache_hash(model, req, mot, fhashes) if req.kind != "count" else None
        if h:                                                    # FR-C11: cache hit = 0 spend
            row = self.ledger.cache_get(h, None, cfg.cache_ttl_h * 3600)
            if row:
                emit("ok", model=model, key_id=kid, detail={"cached": True})
                return RouterResult(text=row["response"], model=model, key_id=kid,
                                    tokens_in=row["tokens_in"], tokens_out=row["tokens_out"],
                                    cached=True, overflow_tier=tier,
                                    request_id=req.request_id, job=ctx.job,
                                    trace_id=ctx.trace_id, step=ctx.step), "ok"

        waited_total = 0.0
        while g["wait_s"] > 0:                                   # FR-C13 wait loop
            if not wait_enabled or g["wait_s"] > cfg.wait.max_s:
                emit("switch", model=model, key_id=kid, wait_s=g["wait_s"],
                     detail={"reason": f"{g['reason']}_wait_skip"})
                return None, "wait_skip"
            emit("waiting", model=model, key_id=kid, wait_s=g["wait_s"],
                 detail={"reason": g["reason"]})
            step = min(cfg.wait.poll_s, g["wait_s"], max(0.05, deadline - self._now()))
            await self._sleep(step)
            waited_total += step
            if waited_total > cfg.wait.max_s or self._now() >= deadline:
                emit("switch", model=model, key_id=kid, detail={"reason": "wait_timeout"})
                return None, "wait_skip"
            g = self._gates(model, key, est)

        gap = cfg.retry.request_delay_s                          # RPM pacing between calls
        last = self.ledger.last_call_ts(kid, model)
        if gap > 0 and last is not None and (self._now() - last) < gap:
            d = gap - (self._now() - last)
            emit("pacing", model=model, key_id=kid, wait_s=round(d, 2))
            await self._sleep(d)

        row_id = self.ledger.plan(key_id=kid, model=model, kind=req.kind, est_tokens=est,
                                  request_id=ctx.request_id, source=req.source,
                                  overflow_tier=tier, initiator=ctx.initiator,
                                  comment=ctx.comment or None, version=ctx.version,
                                  job=ctx.job, trace_id=ctx.trace_id,
                                  step=ctx.step or None, requested_model=req.model,
                                  parent_span=ctx.parent_span)
        emit("dispatch", model=model, key_id=kid, detail={"row_id": row_id, "est_tokens": est})
        attempt = 0
        t0 = self._now()
        while True:
            extra = {}
            try:
                if stream:
                    emit("stream_start", model=model, key_id=kid, detail={"attempt": attempt})
                    extra["on_text"] = lambda text: emit(
                        "text", model=model, key_id=kid, detail={"text": text})
                gr = await self.client.generate(
                    key, model, prompt=req.prompt, system=req.system,
                    thinking_level=req.thinking_level, output_format=req.output_format,
                    max_output_tokens=mot,
                    file_parts=file_parts(files) if files else None, **extra)
            except GeminiHTTPError as e:
                ms = int((self._now() - t0) * 1000)
                if files and e.status in (400, 500):
                    # Cannot read (at least) one of these classes: remember per class.
                    for cls in {media_class(f.mime) for f in files}:
                        self._file_incapable.setdefault(cls, set()).add((model, kid))
                if e.status == 429:                    # FR-C4 / SPEC §12: live 429
                    signal = e.signal or self.truth_fallback(e)
                    self.ledger.resolve(
                        row_id, "quota", http_status=e.status, latency_ms=ms,
                        quota_error=str(e), retry_after_s=signal.retry_after_s,
                        quota_id=signal.quota_id, signal_kind=signal.kind,
                        provider_reason=signal.reason)
                    self.truth.apply(signal, key_id=kid, model=model, context=ctx)
                    # A *confirmed* minute quota with a short, explicit Retry-After is
                    # worth waiting out on the same pair (FR-C13). Everything else —
                    # a daily verdict or an unexplained capacity blip — switches away.
                    if (signal.kind == "minute" and not signal.unconfirmed
                            and wait_enabled and attempt < cfg.retry.max
                            and (signal.retry_after_s or 0) <= cfg.wait.max_s
                            and self._now() < deadline):
                        attempt += 1
                        backoff = min(signal.retry_after_s or 0.0,
                                      max(0.0, deadline - self._now()))
                        emit("waiting", model=model, key_id=kid, wait_s=round(backoff, 1),
                             detail={"reason": "http_429", "attempt": attempt})
                        if stream:
                            row_id = self.ledger.plan(
                                key_id=kid, model=model, kind=req.kind, est_tokens=est,
                                request_id=ctx.request_id, source=req.source,
                                overflow_tier=tier, initiator=ctx.initiator,
                                comment=ctx.comment or None, version=ctx.version,
                                job=ctx.job, trace_id=ctx.trace_id,
                                step=ctx.step or None, requested_model=req.model)
                        await self._sleep(backoff)
                        continue
                    emit("switch", model=model, key_id=kid,
                         detail={"reason": signal.reason, "signal_kind": signal.kind})
                    # Never hammer the same pair in place: the provider just told us it
                    # is out. The walk moves to another key/model instead.
                    return None, "quota"
                if e.key_rejected:                # dead credentials: drop this key only
                    self._resolve_err(row_id, e, ms)
                    dead_keys.add(kid)
                    failures.append(f"{model}: key {kid} rejected (HTTP {e.status})")
                    emit("switch", model=model, key_id=kid,
                         detail={"reason": f"key_rejected_{e.status}"})
                    return None, "key_rejected"
                if e.retryable and attempt < cfg.retry.max and self._now() < deadline:
                    attempt += 1
                    jitter = random.uniform(0, cfg.retry.jitter_max_s)
                    backoff = (e.retry_after if e.retry_after is not None else
                               cfg.retry.backoff_base_s * (2 ** (attempt - 1)) + jitter)
                    backoff = min(backoff, max(0.0, deadline - self._now()))
                    if not wait_enabled and backoff > min(5.0, cfg.wait.max_s):
                        self._resolve_err(row_id, e, ms)
                        emit("switch", model=model, key_id=kid, wait_s=round(backoff, 1),
                             detail={"reason": "retry_wait_skip"})
                        return None, "wait_skip"
                    emit("waiting" if e.retry_after is not None else "retry",
                         model=model, key_id=kid, wait_s=round(backoff, 1),
                         detail={"reason": f"http_{e.status}", "attempt": attempt})
                    if stream:
                        self._resolve_err(row_id, e, ms)
                        gate = self._gates(model, key, est)
                        if gate["rpd_left"] <= 0 or gate["wait_s"] > 0:
                            emit("switch", model=model, key_id=kid,
                                 detail={"reason": "stream_retry_gate"})
                            return None, "quota"
                        row_id = self.ledger.plan(
                            key_id=kid, model=model, kind=req.kind, est_tokens=est,
                            request_id=ctx.request_id, source=req.source, overflow_tier=tier,
                            initiator=ctx.initiator, comment=ctx.comment or None,
                            version=ctx.version, job=ctx.job, trace_id=ctx.trace_id,
                            step=ctx.step or None, requested_model=req.model)
                    await self._sleep(backoff)
                    continue
                self._resolve_err(row_id, e, ms)
                if e.switch_model or e.retryable:      # retries exhausted → next candidate
                    failures.append(f"{model}: {e}")
                    emit("switch", model=model, key_id=kid, detail={"reason": f"http_{e.status}"})
                    return None, "http"
                raise RouterError(f"{model}: {e}") from e
            except httpx.HTTPError as e:
                ms = int((self._now() - t0) * 1000)
                if attempt < cfg.retry.max and self._now() < deadline:
                    attempt += 1
                    backoff = min(cfg.retry.backoff_base_s * (2 ** (attempt - 1))
                                  + random.uniform(0, cfg.retry.jitter_max_s),
                                  max(0.0, deadline - self._now()))
                    emit("retry", model=model, key_id=kid, wait_s=round(backoff, 1),
                         detail={"reason": "network", "attempt": attempt})
                    if stream:
                        self.ledger.resolve(row_id, "blocked", latency_ms=ms,
                                            quota_error="stream transport interrupted")
                        gate = self._gates(model, key, est)
                        if gate["rpd_left"] <= 0 or gate["wait_s"] > 0:
                            emit("switch", model=model, key_id=kid,
                                 detail={"reason": "stream_retry_gate"})
                            return None, "quota"
                        row_id = self.ledger.plan(
                            key_id=kid, model=model, kind=req.kind, est_tokens=est,
                            request_id=ctx.request_id, source=req.source, overflow_tier=tier,
                            initiator=ctx.initiator, comment=ctx.comment or None,
                            version=ctx.version, job=ctx.job, trace_id=ctx.trace_id,
                            step=ctx.step or None, requested_model=req.model)
                    await self._sleep(backoff)
                    continue
                self.ledger.resolve(row_id, "blocked" if stream else "network",
                                    latency_ms=ms, quota_error=type(e).__name__)
                failures.append(f"{model}: network {type(e).__name__}")
                emit("switch", model=model, key_id=kid, detail={"reason": "network"})
                return None, "network"
            except asyncio.CancelledError:
                self.ledger.resolve(row_id, "blocked" if stream else "network",
                                    latency_ms=int((self._now() - t0) * 1000),
                                    quota_error="request cancelled")
                raise
            except RouterError as e:                             # blocked/empty response
                self.ledger.resolve(row_id, "blocked",
                                    latency_ms=int((self._now() - t0) * 1000),
                                    quota_error=str(e)[:200])
                failures.append(f"{model}: {e}")
                emit("switch", model=model, key_id=kid, detail={"reason": "blocked"})
                return None, "blocked"
            except Exception:
                if stream:
                    self.ledger.resolve(row_id, "blocked",
                                        latency_ms=int((self._now() - t0) * 1000),
                                        quota_error="stream consumer disconnected")
                raise

            ms = int((self._now() - t0) * 1000)
            tin = gr.tokens_in if gr.tokens_in is not None else est
            self.ledger.resolve(row_id, "ok", http_status=200, tokens_in=tin,
                                tokens_out=gr.tokens_out, latency_ms=ms)
            # Provider truth is symmetric: a success clears an earlier 429/503 block.
            self.ledger.clear_pair_state(kid, model)
            if h:
                self.ledger.cache_put(h, req.request_id, model, gr.text, tin, gr.tokens_out)
            emit("ok", model=model, key_id=kid,
                 detail={"tokens_in": tin, "tokens_out": gr.tokens_out,
                         "tokens_thought": gr.tokens_thought,
                         "latency_s": round(ms / 1000, 1), "finish": gr.finish_reason})
            return RouterResult(text=gr.text, model=model, key_id=kid, tokens_in=tin,
                                tokens_out=gr.tokens_out, latency_s=ms / 1000,
                                overflow_tier=tier, request_id=req.request_id,
                                row_id=row_id, finish_reason=gr.finish_reason,
                                tokens_thought=gr.tokens_thought, job=ctx.job,
                                trace_id=ctx.trace_id, step=ctx.step), "ok"

    def _resolve_err(self, row_id: int, e: GeminiHTTPError, ms: int) -> None:
        status = ("http5xx" if (e.status or 0) >= 500 else
                  "quota" if e.status == 429 else "http4xx")
        signal = e.signal
        self.ledger.resolve(row_id, status, http_status=e.status, latency_ms=ms,
                            quota_error=str(e)[:500], retry_after_s=e.retry_after,
                            quota_id=signal.quota_id if signal else None,
                            signal_kind=signal.kind if signal else None,
                            provider_reason=signal.reason if signal else None)
