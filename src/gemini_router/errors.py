"""Error taxonomy. Ported status sets from tldr-digest src/llm.py, extended with
free-tier quota semantics (day vs minute 429) per gemini-mcp docs/02 FR-C4/C8/C13."""
from __future__ import annotations

RETRYABLE_STATUS = {408, 409, 500, 502, 503, 504}
MODEL_SWITCH_STATUS = {400, 403, 404, 405, 422}
KEY_REJECT_STATUS = {401}   # credentials refused → the key is dead, not the model


class RouterError(Exception):
    """Base error. Non-retryable by default."""


class GeminiHTTPError(RouterError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False,
                 switch_model: bool = False, retry_after: float | None = None,
                 quota_day: bool = False, quota_minute: bool = False,
                 key_rejected: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.switch_model = switch_model
        self.retry_after = retry_after
        self.quota_day = quota_day      # daily RPD exhausted → cooldown until PT reset
        self.quota_minute = quota_minute  # RPM/TPM gate → waitable (FR-C13)
        self.key_rejected = key_rejected  # this key must not be retried in this request


class QuotaExhaustedError(RouterError):
    """All candidates blocked by quota and overflow not requested/available (FR-B8)."""

    def __init__(self, per_model: list[dict], reset_eta_pt: str, *, hint: str = "",
                 waited: bool = False, failures: list[str] | None = None):
        summary = "; ".join(f"{m['model']}: rpd_left={m['rpd_left']}" for m in per_model)
        super().__init__(f"quota exhausted: {summary}" if summary else "quota exhausted")
        self.per_model = per_model
        self.reset_eta_pt = reset_eta_pt
        self.hint = hint
        self.waited = waited
        self.failures = failures or []

    def to_dict(self) -> dict:
        return {"error": "quota_exhausted", "per_model": self.per_model,
                "reset_eta_pt": self.reset_eta_pt, "hint": self.hint,
                "waited": self.waited, "failures": self.failures}
