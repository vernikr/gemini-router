"""Router: cascading chain, key pool, budget gates, wait policy (FR-C13), overflow
tiers, cache. Budget checks are ledger queries (D1); statuses taxonomy ported from
tldr-digest src/llm.py."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx

from .config import Config, estimate_tokens, key_id
from .errors import (
    FileNotReady,
    GeminiHTTPError,
    QuotaExhaustedError,
    RouterError,
)
from .events import RouterEvent
from .gemini import GeminiClient, UploadedFile, file_parts
from .ledger import Ledger, iso, pt_midnight_ts
from .tariffs import media_class

Emitter = Callable[[RouterEvent], None]


@dataclass(slots=True)
class RouterRequest:
    prompt: str
    system: str | None = None
    thinking_level: str | None = "medium"   # low|medium|high; None omits thinkingConfig
    output_format: str = "text"             # text|json
    max_output_tokens: int | None = None    # None → cfg.max_output_tokens_default
    kind: str = "single"                    # single|chunk|consolidate|count
    request_id: str | None = None           # idempotency / cache lookup
    source: str = "cli"                     # mcp|cli|rest|tldr
    overflow: str = "off"                   # off|lite|lite+gemma (FR-C8, never automatic)
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
    finish_reason: str | None = None         # v0.1.1: live-smoke finding (starvation diag)
    tokens_thought: int | None = None
    events: list[RouterEvent] = field(default_factory=list)
    quota_after: list[dict] = field(default_factory=list)
    request_id: str | None = None
    row_id: int | None = None
    files_used: list[UploadedFile] = field(default_factory=list)  # actual key's uploads


def gates_from(cfg: Config, model: str, kid: str, spent: tuple[int, int],
               window: tuple[int, int, float | None], cd: float | None,
               est_tokens: int, now: float) -> dict:
    """The gate arithmetic (FR-C2), fed either by the per-pair ledger accessors or by
    a Ledger.gate_snapshot. wait_s>0 means a minute-scale gate (RPM/TPM/cooldown) —
    waitable per FR-C13; rpd_left==0 means day-scale — never waited."""
    lim = cfg.limits_for(model)
    spent_day, spent_roll = spent
    calls, toks, oldest = window
    expire_in = max(0.0, oldest + cfg.window_s - now) if oldest is not None else 0.0
    rpm_wait = expire_in if (lim.rpm and calls >= lim.rpm) else 0.0
    tpm_wait = expire_in if (est_tokens and lim.tpm and toks + est_tokens > lim.tpm) else 0.0
    cd_wait = max(0.0, cd - now) if cd else 0.0
    waits = {"rpm": rpm_wait, "tpm": tpm_wait, "cooldown": cd_wait}
    wait_s = max(waits.values())
    return {"key_id": kid, "model": model,
            "rpd_left": max(0, lim.rpd - max(spent_day, spent_roll)),
            "rpd_left_day": max(0, lim.rpd - spent_day),
            "rpd_left_roll": max(0, lim.rpd - spent_roll),
            "rpd_limit": lim.rpd, "rpm_used": calls, "rpm_limit": lim.rpm,
            "tpm_used": toks, "tpm_limit": lim.tpm,
            "rpm_wait_s": round(rpm_wait, 1), "tpm_wait_s": round(tpm_wait, 1),
            "cooldown_until": cd, "wait_s": round(wait_s, 1),
            "reason": max(waits, key=waits.get) if wait_s > 0 else None}


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
                      ledger.cooldown(kid, model, now), est_tokens, now)


def cache_hash(model: str, req: RouterRequest, mot: int,
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
        """key_id → live model set (None = listModels failed → trust config, b04).

        One call, not one per key: the model list is a property of the account tier, and
        every key on one plan gets the same answer, so asking N times buys nothing and
        costs N round trips on a path that runs per request. The result is still keyed
        per key so callers keep their shape, and a key whose own liveness matters
        (setup, doctor) can pass `per_key=True`.
        """
        return await self._availability(per_key=False)

    async def availability_per_key(self) -> dict[str, set[str] | None]:
        """The same probe against every key separately — for checking that each key
        works at all, which is a different question from what models exist."""
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
                          snap["cooldown"].get((kid, model)), 0, now)

    def quota_table(self, now: float | None = None) -> list[dict]:
        """Per key×model budget rows for chain + literal overflow tiers (BR3)."""
        now = now or self._now()
        models = list(dict.fromkeys(
            self.cfg.chain + [m for m in self.cfg.overflow_tiers if m != "gemma-auto"]))
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

    async def count_tokens(self, prompt: str, model: str | None = None) -> int:
        """countTokens pre-flight; recorded with kind='count' → never spends RPD (FR-C7)."""
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
                               request_id=None, source="count")
        self.ledger.resolve(row, "ok", tokens_in=n, tokens_out=0)
        return n

    # ------------------------------------------------------------------ main
    async def complete(self, req: RouterRequest, on_event: Emitter | None = None,
                       files: list[UploadedFile] | None = None,
                       file_paths: list[str | Path] | None = None, *,
                       stream: bool = False) -> RouterResult:
        """Route files like text when local sources permit re-upload on key switch.

        A provider file is visible only to its uploading key. With matching local
        `file_paths`, re-upload once per new key after quota/availability switching;
        without them keep the historical pin, never send a foreign URI under a key.
        The gateway supplies local sources for both single and packed PDFs."""
        events: list[RouterEvent] = []

        def emit(stage: str, **kw) -> None:
            ev = RouterEvent(stage=stage, **kw)
            if stage != "text":
                events.append(ev)
            if on_event:
                on_event(ev)

        cfg = self.cfg
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
        mot = req.max_output_tokens or cfg.max_output_tokens_default
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
                                    source=req.source, key=key, sha256=original.sha256)
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
                        failures, dead_keys, serving if files else None, fhashes, stream)
                    if res is not None:
                        res.events = events
                        res.quota_after = self.model_budgets(tried)
                        res.files_used = serving or []
                        return res
                    if outcome in ("quota", "wait_skip"):
                        quota_blocked = True

        if dead_keys and len(dead_keys) == len({key_id(k) for k in self.cfg.api_keys}):
            raise RouterError(
                "every configured API key was rejected by the provider (HTTP 401, or a "
                "leaked-key block) — delete the dead keys in the console and add fresh "
                "ones: `gemini-mcp keys` shows the pool, `gemini-mcp keys add <key>` "
                "validates and installs one without touching the unit file by hand")
        # A file job that exhausted its file-capable candidates is not a key problem, so
        # this must be read before the pin clause below — and the pin clause itself must
        # not fire on a quota block, or a 429-drained chain tells the user to re-upload a
        # file under a key that was never the issue (found by test on 2026-10-11).
        if files and not quota_blocked and any(self._file_incapable.get(cls)
                                                for cls in (fclasses or ())):
            raise RouterError(
                "no configured model/key can read this file: " + self._incapable_note() +
                " answered 400/500 to a file request. Capability is per model × media "
                "kind and can change over time; Lite/PDF was content-verified on two "
                "keys in 2026-10. Use a capable chain/overflow model or set "
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
        """Blocking facade for legacy sync callers (tldr-digest migration, SPEC §2)."""
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
                          overflow: str = "off", sha256: str | None = None,
                          ready_timeout_s: float = 30.0) -> UploadedFile:
        """Upload a blob and hand back the provider record, pinned to the key that will
        serve the analysis. The key is chosen the way `complete` would choose it, minus
        keys whose day is spent or which sit in cooldown — uploading a file for a key
        that cannot then answer spends an upload for nothing.

        For a PDF, pass the same `overflow` policy as the generation request: if
        every chain-head key is day-drained, select a key with Lite/Gemma budget
        instead of refusing the upload before text-style routing can start.
        `sha256` is the local blob's hash for the cache. The file stays ACTIVE."""
        cfg = self.cfg
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
            self.ledger.record_upload(key_id=kid, nbytes=up.size, source=source,
                                      latency_ms=ms)
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
        chain = [req.model] if req.model else list(dict.fromkeys(self.cfg.chain))
        tiers = [(0, chain)]
        if req.overflow in ("lite", "lite+gemma"):
            tiers.append((1, [m for m in self.cfg.overflow_tiers if m != "gemma-auto"]))
        if req.overflow == "lite+gemma":
            union: set[str] = set()
            for s in avail.values():
                if s:
                    union |= s
            g = pick_gemma(union)
            if g:
                tiers.append((2, [g]))
        if file_classes:
            # PDF-containing jobs use text's tier order. Other media keep the
            # historical chain-only policy; Gemma still refuses PNG file_data.
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
        now = self._now()
        return sorted(self.cfg.api_keys,
                      key=lambda k: -gates_for(self.cfg, self.ledger, model, k, 0, now)["rpd_left"])

    def _gates(self, model: str, key: str, est_tokens: int = 0) -> dict:
        return gates_for(self.cfg, self.ledger, model, key, est_tokens, self._now())

    async def _try_candidate(self, req: RouterRequest, tier: int, model: str, key: str,
                             est: int, mot: int, wait_enabled: bool, deadline: float,
                             emit: Emitter, failures: list[str], dead_keys: set[str],
                             files: list[UploadedFile] | None = None,
                             fhashes: list[str] | None = None, stream: bool = False):
        cfg = self.cfg
        kid = key_id(key)
        g = self._gates(model, key, est)
        if g["rpd_left"] <= 0:                                   # day gate — never waited (D9)
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
                                    request_id=req.request_id), "ok"

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
                                  request_id=req.request_id, source=req.source,
                                  overflow_tier=tier)
        emit("dispatch", model=model, key_id=kid, detail={"row_id": row_id, "est_tokens": est})
        attempt = 0
        t0 = self._now()
        while True:
            extra = {}
            if stream:
                emit("stream_start", model=model, key_id=kid, detail={"attempt": attempt})
                extra["on_text"] = lambda text: emit(
                    "text", model=model, key_id=kid, detail={"text": text})
            try:
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
                if e.quota_day:                                  # FR-C4: live daily 429
                    self.ledger.resolve(row_id, "quota", http_status=e.status, latency_ms=ms,
                                        quota_error=str(e), retry_after_s=e.retry_after)
                    until = pt_midnight_ts(cfg.quota_day_tz, days_ahead=1)
                    if e.retry_after is not None:
                        until = min(until, self._now() + e.retry_after + 5)
                    self.ledger.set_cooldown(kid, model, until, "rpd_429")
                    emit("switch", model=model, key_id=kid, detail={"reason": "rpd_429"})
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
                            request_id=req.request_id, source=req.source, overflow_tier=tier)
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
                        self.ledger.resolve(row_id, "interrupted", latency_ms=ms,
                                            quota_error="stream transport interrupted")
                        gate = self._gates(model, key, est)
                        if gate["rpd_left"] <= 0 or gate["wait_s"] > 0:
                            emit("switch", model=model, key_id=kid,
                                 detail={"reason": "stream_retry_gate"})
                            return None, "quota"
                        row_id = self.ledger.plan(
                            key_id=kid, model=model, kind=req.kind, est_tokens=est,
                            request_id=req.request_id, source=req.source, overflow_tier=tier)
                    await self._sleep(backoff)
                    continue
                self.ledger.resolve(row_id, "interrupted" if stream else "network",
                                    latency_ms=ms, quota_error=type(e).__name__)
                failures.append(f"{model}: network {type(e).__name__}")
                emit("switch", model=model, key_id=kid, detail={"reason": "network"})
                return None, "network"
            except asyncio.CancelledError:
                self.ledger.resolve(row_id, "interrupted" if stream else "network",
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

            ms = int((self._now() - t0) * 1000)
            tin = gr.tokens_in if gr.tokens_in is not None else est
            self.ledger.resolve(row_id, "ok", http_status=200, tokens_in=tin,
                                tokens_out=gr.tokens_out, latency_ms=ms)
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
                                tokens_thought=gr.tokens_thought), "ok"

    def _resolve_err(self, row_id: int, e: GeminiHTTPError, ms: int) -> None:
        status = ("http5xx" if (e.status or 0) >= 500 else
                  "quota" if e.status == 429 else "http4xx")
        self.ledger.resolve(row_id, status, http_status=e.status, latency_ms=ms,
                            quota_error=str(e)[:500], retry_after_s=e.retry_after)
