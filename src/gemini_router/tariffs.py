"""Media token tariffs and drift detection (SPEC §2).

Forecasts are planning estimates before provider dispatch. `countTokens` supplies the
measured request value used for preflight and accounting; drift checks compare the
forecast with provider usage metadata.
"""
from __future__ import annotations

# Tokens per image or PDF page at each media-resolution level.
# The values are measured estimates; request accounting uses provider usage metadata.
MEDIA_TARIFFS: dict[str, dict[str, int]] = {
    "image": {"low": 256, "medium": 529, "high": 1089},
    "pdf": {"low": 280, "medium": 560, "high": 1120},
}

# Levels used when a request omits media_resolution.
DEFAULT_LEVEL: dict[str, str] = {"image": "high", "pdf": "medium"}

# Published reference rates; forecasts remain disabled until verified by a live count.
PUBLISHED_ONLY: dict[str, str] = {"audio": "25 tokens/s", "video": "70 tokens/frame at 1 fps"}

LEVELS = ("low", "medium", "high")


class TariffNotMeasured(ValueError):
    """No measured tariff for this media class — measure with countTokens first."""


def media_class(mime: str | None) -> str:
    """image / pdf / audio / video / other — the tariff and capability unit."""
    m = (mime or "").lower()
    if m == "application/pdf":
        return "pdf"
    return m.split("/", 1)[0] if "/" in m else "other"


def forecast_media_tokens(mime: str | None, *, pages: int = 1,
                          level: str | None = None) -> int:
    """Predict token cost before dispatch; ``pages`` applies to PDFs only."""
    cls = media_class(mime)
    table = MEDIA_TARIFFS.get(cls)
    if table is None:
        note = f" ({PUBLISHED_ONLY[cls]})" if cls in PUBLISHED_ONLY else ""
        raise TariffNotMeasured(
            f"no measured tariff for {cls!r}{note} — measure with countTokens; "
            f"forecasts cover {'/'.join(MEDIA_TARIFFS)} only")
    chosen = level or DEFAULT_LEVEL[cls]
    if chosen not in LEVELS:
        raise ValueError(f"media_resolution must be one of {'/'.join(LEVELS)} or '' "
                         f"— got {chosen!r}")
    per = table[chosen]
    return per * pages if cls == "pdf" else per


def check_drift(predicted: int, measured: int) -> dict | None:
    """Return a drift record when the miss exceeds max(5 %, 50 tokens).

    Drift is an observability signal, not an error: the request is accounted using the
    provider's measured usage.
    """
    if abs(measured - predicted) <= max(predicted * 0.05, 50):
        return None
    return {"predicted": predicted, "measured": measured,
            "pct": round((measured - predicted) / predicted * 100, 1)}
