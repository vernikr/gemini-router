"""Config: pydantic model + YAML/env loading (SPEC §4). Defaults mirror
gemini-mcp docs/03 §8. Secrets come only from env; never from YAML."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_CHAIN = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
                 "gemini-3.5-flash", "gemini-3-flash-preview"]
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
    api_keys: list[str] = Field(default_factory=list)
    temperature: float | None = None           # applied to pre-3.x models only
    max_output_tokens_default: int = 8192
    chars_per_token: int = 4                   # estimate when countTokens not provided

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


def estimate_tokens(text: str, chars_per_token: int = 4) -> int:
    return max(1, len(text) // max(1, chars_per_token))
