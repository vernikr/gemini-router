"""Config: pydantic model + YAML/env loading (SPEC §4). Defaults mirror
gemini-mcp docs/03 §8. Secrets come only from env; never from YAML."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

# Deprecated ids are left out even though the API still lists them: gemini-3.5-flash
# and gemini-3.7-flash are aliases now, every call redirects to 3.6 and 3.8. Routing to
# an alias means paying for a model nobody can select.
DEFAULT_CHAIN = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview"]
DEFAULT_OVERFLOW = ["gemini-3.5-flash-lite", "gemma-auto"]


class Limits(BaseModel):
    rpd: int = 20
    rpm: int = 5
    tpm: int = 250_000


class RetryCfg(BaseModel):
    max: int = 3
    backoff_base_s: float = 5.0
    jitter_max_s: float = 2.0
    provider_timeout_s: float = 120.0
    request_delay_s: float = 2.0


class WaitCfg(BaseModel):
    enabled_default: bool = True   # FR-C13: wait out minute-scale gates by default
    max_s: float = 65.0
    poll_s: float = 2.0


class ProxyCfg(BaseModel):
    """Settings of the native-Gemini pass-through (`gemini-router proxy`, SPEC §11).

    It holds no secrets: the client token is read from the environment named by
    `token_env`, never from YAML.
    """

    bind: str = "127.0.0.1"                 # loopback only — reach it through a tunnel
    port: int = 8787
    token_env: str = "GEMINI_ROUTER_PROXY_TOKEN"
    max_upload_bytes: int = 2 * 1024 ** 3   # the provider's own per-file ceiling
    request_timeout_s: float = 180.0        # long enough for an 8 MiB chunk on a slow link
    # Bodies up to this size are buffered, which is what lets the proxy read the store
    # name out of a generateContent body. Anything larger (upload chunks) is streamed
    # and never held twice in memory — the gateway box has 1 GB.
    body_inspect_bytes: int = 1 << 20
    audit: bool = True                      # one JSONL line per request, key_id only
    # What to do with the model the caller asked for (SPEC §11.6).
    #   auto        keep it when the chosen key can serve it, else rewrite
    #   strict      always rewrite to the best chain/overflow model with budget
    #   passthrough never rewrite; account only
    model_policy: str = "auto"
    file_search_models: list[str] | None = None   # None = cfg.chain for rewriting


class Config(BaseModel):
    chain: list[str] = Field(default_factory=lambda: list(DEFAULT_CHAIN))
    overflow_tiers: list[str] = Field(default_factory=lambda: list(DEFAULT_OVERFLOW))
    limits_per_key_model: Limits = Field(default_factory=Limits)
    overflow_limits: Limits = Field(default_factory=lambda: Limits(rpd=500, rpm=15, tpm=250_000))
    limits_overrides: dict[str, dict[str, int]] = Field(default_factory=dict)
    quota_day_tz: str = "America/Los_Angeles"
    retry: RetryCfg = Field(default_factory=RetryCfg)
    wait: WaitCfg = Field(default_factory=WaitCfg)
    window_s: float = 60.0                     # sliding RPM/TPM window
    planned_ttl_s: float = 300.0               # stale 'planned' rows stop counting after this
    models_refresh_h: float = 12.0
    cache_ttl_h: float = 24.0
    state_path: str = "state.db"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    upload_base_url: str = "https://generativelanguage.googleapis.com/upload/v1beta"
    api_keys: list[str] = Field(default_factory=list)
    # Models that accept Files API parts in generateContent. None (default) = "learn":
    # a file job stays on the chain and a model that answers 400/500 to file input is
    # remembered as incapable for that media class. A flat list applies to every class;
    # a dict narrows per class ({"pdf": [...], "image": [...]}). Historical measurements
    # differ by key/date: 2026-10-11 Lite/PDF refused, 2026-10-12 new VPS and CI
    # keys read the hidden PDF code on Lite. PDF-containing tiers now match text;
    # this optional allowlist and per-key learned refusals still limit incompatibility.
    # Evidence: docs/worklog/archive/2026.10.11-capability-canary/.
    file_capable_models: list[str] | dict[str, list[str]] | None = None
    temperature: float | None = None           # applied to pre-3.x models only
    max_output_tokens_default: int = 32768   # cap only; 3.x thinking tokens count toward it
    # Estimate divisor for the TPM pre-flight gate. None (default) = script-aware
    # estimate; set a number to force the old single-divisor behaviour.
    chars_per_token: int | None = None
    proxy: ProxyCfg = Field(default_factory=ProxyCfg)
    # key_id of the project that owns File Search by default: where stores are created
    # and where unscoped calls (list stores, Files API list) are answered from. None =
    # pick the key with the largest remaining budget once, then keep it (SPEC §11.4).
    file_search_key: str | None = None

    @classmethod
    def load(cls, path: str | Path | None = None, **over) -> Config:
        data: dict = {}
        p = path or os.environ.get("GEMINI_ROUTER_CONFIG")
        if p and Path(p).exists():
            raw = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
            data = raw.get("router", raw) if isinstance(raw.get("router"), dict) else raw
        keys = _env_keys()
        if keys:
            data["api_keys"] = keys
        if os.environ.get("GEMINI_ROUTER_STATE"):
            data["state_path"] = os.environ["GEMINI_ROUTER_STATE"]
        data.update({k: v for k, v in over.items() if v is not None})
        return cls.model_validate(data)

    def limits_for(self, model: str) -> Limits:
        base = (self.limits_per_key_model if model in self.chain else self.overflow_limits)
        merged = base.model_dump() | self.limits_overrides.get(model, {})
        return Limits(**merged)


def _env_keys() -> list[str]:
    keys: list[str] = []
    v = os.environ.get("GEMINI_API_KEYS", "")
    keys += [k.strip() for k in v.split(",") if k.strip()]
    for i in range(1, 11):
        k = os.environ.get(f"GEMINI_API_KEY_{i}", "").strip()
        if k:
            keys.append(k)
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if k:
        keys.append(k)
    return list(dict.fromkeys(keys))


def key_id(key: str) -> str:
    """Masked key identity for logs/ledger (FR-E8): never store raw keys."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


# Characters per token, by script class. One divisor for the whole string is wrong by
# a wide margin: measured against the reference tokenizer, Latin prose runs ~5.4
# chars/token while Cyrillic runs ~1.7 and digits ~1.8, so a single "len // 4"
# under-estimates Russian text by ~40% and over-estimates English by ~60%. The
# under-estimate is the expensive direction — the TPM gate passes and the provider
# answers 429. Fitted on mixed-language documents; median error +3%, worst +61%,
# against −39% median and 275/300 under-estimates for the single divisor.
_CHARS_PER_TOKEN = {"wide": 1.7, "latin": 5.4, "digit": 1.8, "other": 2.2}
_ASCII_LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_DIGITS = "0123456789"
# Beyond this many characters, sample the head and tail instead of scanning the whole
# document. Script mix is a property of a document rather than of a position in it, so
# the estimate holds; the cost stops growing with the input. Both ends are sampled
# because they are the parts most likely to differ — front matter, data tables,
# appendices.
_SAMPLE_CHARS = 200_000


def _script_counts(text: str) -> tuple[float, float, float, float]:
    """(wide, latin, digit, other) character counts.

    `str.count` per ASCII letter is measurably the cheapest accurate route: a
    per-character Python loop costs ~1.0 s on 11 M characters and a regex sweep ~1.0 s,
    against ~0.37 s here. Non-ASCII is treated as one wide class — splitting Cyrillic
    from other non-ASCII scripts did not improve the fit enough to pay for the pass.
    """
    latin = sum(text.count(c) for c in _ASCII_LETTERS)
    digit = sum(text.count(c) for c in _ASCII_DIGITS)
    wide = len(text) - len(text.encode("ascii", "ignore"))
    return wide, latin, digit, len(text) - latin - digit - wide


def estimate_tokens(text: str, chars_per_token: int | None = None,
                    sample_chars: int = _SAMPLE_CHARS) -> int:
    """Token estimate for a prompt, used for the TPM pre-flight gate.

    Script-aware: it classifies characters and divides each class by its own ratio, so
    a document mixing Russian, English and numbers is estimated as a whole rather than
    through one divisor. Inputs longer than `sample_chars` are estimated from a
    head-and-tail sample scaled up, which keeps the cost flat.

    Passing `chars_per_token` restores the old single-divisor behaviour for callers
    that configured one.
    """
    if chars_per_token:
        return max(1, len(text) // max(1, chars_per_token))
    n = len(text)
    if n <= sample_chars:
        w, la, d, o = _script_counts(text)
    else:
        half = sample_chars // 2
        a = _script_counts(text[:half])
        b = _script_counts(text[-half:])
        scale = n / sample_chars
        w, la, d, o = ((a[i] + b[i]) * scale for i in range(4))
    r = _CHARS_PER_TOKEN
    return max(1, int(w / r["wide"] + la / r["latin"] + d / r["digit"] + o / r["other"]))
