"""gemini-router — quota-aware Gemini Flash router with a persistent ledger.

Public API (docs/SPEC.md §2):
    Router, RouterRequest, RouterResult, RouterEvent,
    RouterError, GeminiHTTPError, QuotaExhaustedError, FileUploadFailed, FileNotReady,
    Config, Limits, Ledger, GeminiClient, GenResult, UploadedFile,
    extract_json, file_parts, key_id, estimate_tokens,
    forecast_media_tokens, check_drift, media_class, TariffNotMeasured
"""
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .config import Config, Limits, RetryCfg, WaitCfg, estimate_tokens, key_id
from .errors import (
    FileNotReady,
    FileUploadFailed,
    GeminiHTTPError,
    QuotaExhaustedError,
    RouterError,
)
from .events import RouterEvent
from .gemini import GeminiClient, GenResult, UploadedFile, extract_json, file_parts, is_gemini3
from .ledger import Ledger, iso, pt_midnight_ts
from .routing import Router, RouterRequest, RouterResult, cache_hash, gates_for, pick_gemma
from .tariffs import (
    DEFAULT_LEVEL,
    MEDIA_TARIFFS,
    TariffNotMeasured,
    check_drift,
    forecast_media_tokens,
    media_class,
)

try:                                   # single source of truth = pyproject version
    __version__ = _pkg_version("gemini-router")
except PackageNotFoundError:           # unpackaged run from a source checkout
    __version__ = "0.0.0.dev0"

__all__ = [
    "Router", "RouterRequest", "RouterResult", "RouterEvent",
    "RouterError", "GeminiHTTPError", "QuotaExhaustedError",
    "FileUploadFailed", "FileNotReady", "UploadedFile", "file_parts",
    "Config", "Limits", "RetryCfg", "WaitCfg", "Ledger", "GeminiClient", "GenResult",
    "extract_json", "is_gemini3", "key_id", "estimate_tokens", "cache_hash",
    "gates_for", "pick_gemma", "pt_midnight_ts", "iso",
    "forecast_media_tokens", "check_drift", "media_class", "TariffNotMeasured",
    "MEDIA_TARIFFS", "DEFAULT_LEVEL", "__version__",
]
