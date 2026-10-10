"""gemini-router — quota-aware Gemini Flash router with a persistent ledger.

Public API (docs/SPEC.md §2):
    Router, RouterRequest, RouterResult, RouterEvent,
    RouterError, GeminiHTTPError, QuotaExhaustedError,
    Config, Limits, Ledger, GeminiClient, extract_json, key_id, estimate_tokens
"""
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .config import Config, Limits, RetryCfg, WaitCfg, estimate_tokens, key_id
from .errors import GeminiHTTPError, QuotaExhaustedError, RouterError
from .events import RouterEvent
from .gemini import GeminiClient, GenResult, extract_json, is_gemini3
from .ledger import Ledger, iso, pt_midnight_ts
from .routing import Router, RouterRequest, RouterResult, cache_hash, gates_for, pick_gemma

try:                                   # single source of truth = pyproject version
    __version__ = _pkg_version("gemini-router")
except PackageNotFoundError:           # unpackaged run from a source checkout
    __version__ = "0.0.0.dev0"

__all__ = [
    "Router", "RouterRequest", "RouterResult", "RouterEvent",
    "RouterError", "GeminiHTTPError", "QuotaExhaustedError",
    "Config", "Limits", "RetryCfg", "WaitCfg", "Ledger", "GeminiClient", "GenResult",
    "extract_json", "is_gemini3", "key_id", "estimate_tokens", "cache_hash",
    "gates_for", "pick_gemma", "pt_midnight_ts", "iso", "__version__",
]
