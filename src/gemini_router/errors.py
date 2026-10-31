"""Provider error classification and quota-aware retry semantics."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .feedback import ProviderSignal

RETRYABLE_STATUS = {408, 409, 500, 502, 503, 504}
MODEL_SWITCH_STATUS = {400, 403, 404, 405, 422}
KEY_REJECT_STATUS = {401}   # credentials refused → the key is dead, not the model


class RouterError(Exception):
    """Base error. Non-retryable by default."""


class GeminiHTTPError(RouterError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False,
                 switch_model: bool = False, retry_after: float | None = None,
                 quota_day: bool = False, quota_minute: bool = False,
                 key_rejected: bool = False, signal: ProviderSignal | None = None):
        super().__init__(message)
        self.signal = signal                # parsed provider truth (quota id, reset, ...)
        self.status = status
        self.retryable = retryable
        self.switch_model = switch_model
        self.retry_after = retry_after
        self.quota_day = quota_day      # daily RPD exhausted → cooldown until PT reset
        self.quota_minute = quota_minute  # RPM/TPM gate → waitable (FR-C13)
        self.key_rejected = key_rejected  # this key must not be retried in this request


class KeyLeakedError(GeminiHTTPError):
    """The provider refused the key because it was reported as leaked. No transport helps
    and no retry helps: delete the key in the console and add a fresh one. The router
    treats it as a key rejection, so failover moves on to the next key. If every key is
    refused, the operator sees the provider's actual cause instead of a generic failure."""

    def __init__(self, message: str, **kw):
        kw.setdefault("key_rejected", True)
        kw.setdefault("switch_model", True)
        super().__init__(message, **kw)


class FileUploadFailed(RouterError):
    """The provider marked the upload FAILED, or answered the lifecycle incoherently."""


class FileNotReady(RouterError):
    """Still PROCESSING after the wait budget — the caller decides to wait or re-upload."""


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
