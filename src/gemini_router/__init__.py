"""gemini-router — quota-aware Gemini Flash router with a persistent ledger.

Public API (docs/SPEC.md §2):
    Router, RouterRequest, RouterResult, RouterEvent,
    RouterError, GeminiHTTPError, QuotaExhaustedError, FileUploadFailed, FileNotReady,
    Config, Limits, Ledger, GeminiClient, GenResult, UploadedFile,
    extract_json, file_parts, key_id, estimate_tokens,
    forecast_media_tokens, check_drift, media_class, TariffNotMeasured
"""
from .config import (
                     Config,
                     FeedbackCfg,
                     Limits,
                     ProxyCfg,
                     RetryCfg,
                     WaitCfg,
                     estimate_tokens,
                     key_id,
)
from .context import (
                     CLIENT_CONTEXT_HEADERS,
                     RequestContext,
                     audit_line,
                     make_traceparent,
                     parse_traceparent,
                     router_version,
)
from .errors import (
                     FileNotReady,
                     FileUploadFailed,
                     GeminiHTTPError,
                     QuotaExhaustedError,
                     RouterError,
)
from .events import RouterEvent
from .feedback import ProviderSignal, QuotaTruth, body_facts, parse_provider_signal
from .gemini import GeminiClient, GenResult, UploadedFile, extract_json, file_parts, is_gemini3
from .ledger import Ledger, iso, pt_midnight_ts
from .proxy import GeminiProxy
from .routing import (
                     Router,
                     RouterRequest,
                     RouterResult,
                     cache_hash,
                     gates_for,
                     pick_gemma,
                     pick_pair,
)
from .tariffs import (
                     DEFAULT_LEVEL,
                     MEDIA_TARIFFS,
                     TariffNotMeasured,
                     check_drift,
                     forecast_media_tokens,
                     media_class,
)

# Single source of truth = pyproject version via importlib.metadata (with a fallback
# for an unpackaged source checkout). Every audit row and startup line carries it.
__version__ = router_version()

__all__ = [
    "Router", "RouterRequest", "RouterResult", "RouterEvent",
    "RouterError", "GeminiHTTPError", "QuotaExhaustedError",
    "FileUploadFailed", "FileNotReady", "UploadedFile", "file_parts",
    "Config", "Limits", "RetryCfg", "WaitCfg", "ProxyCfg", "FeedbackCfg", "Ledger",
    "GeminiClient", "GenResult", "GeminiProxy",
    "RequestContext", "ProviderSignal", "QuotaTruth", "parse_provider_signal",
    "body_facts", "router_version", "audit_line", "CLIENT_CONTEXT_HEADERS",
    "parse_traceparent", "make_traceparent",
    "extract_json", "is_gemini3", "key_id", "estimate_tokens", "cache_hash",
    "gates_for", "pick_pair", "pick_gemma", "pt_midnight_ts", "iso",
    "forecast_media_tokens", "check_drift", "media_class", "TariffNotMeasured",
    "MEDIA_TARIFFS", "DEFAULT_LEVEL", "__version__",
]
