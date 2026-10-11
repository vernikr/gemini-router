"""Media token tariffs and drift detection (SPEC §2, wave 1 of the cross-repo wave plan).

The table below is the single source of truth for media prices; docs point here and never
restate the numbers. A forecast is a planning number for BEFORE any provider round-trip —
countTokens is free and stays the measured fact packing and accounting run on.
"""
from __future__ import annotations

# Tokens per media part by media_resolution level.
# image: per image, flat across 300x300..4096x4096 px and 2 KB..7 MB
#   [measured 2026-10-11, gemini-3.6-flash, countTokens + usageMetadata agree,
#    vernikr/gemini-mcp docs/worklog/2026.10.11-binary-file-support/report.md §0.18.2]
# pdf: per page, text PDF [measured 2026-10-11, same key and sources]
MEDIA_TARIFFS: dict[str, dict[str, int]] = {
    "image": {"low": 256, "medium": 529, "high": 1089},
    "pdf": {"low": 280, "medium": 560, "high": 1120},
}

# The level the provider charges when the request sends none (""):
# the "default" column measured the same day (image = high, pdf = medium).
DEFAULT_LEVEL: dict[str, str] = {"image": "high", "pdf": "medium"}

# Provider-published, not yet measured — forecast refuses until a call verifies them.
# [docs, ai.google.dev, fetched 2026-10-11]
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
    """Predicted token cost before any provider call. `level` in low/medium/high, or
    None/"" for the provider default (DEFAULT_LEVEL). `pages` matters for pdf only."""
    cls = media_class(mime)
    table = MEDIA_TARIFFS.get(cls)
    if table is None:
        note = f" ({PUBLISHED_ONLY[cls]})" if cls in PUBLISHED_ONLY else ""
        raise TariffNotMeasured(
            f"no measured tariff for {cls!r}{note} — measure with countTokens; "
            f"forecasts cover {'/'.join(MEDIA_TARIFFS)} only")
    lvl = level or DEFAULT_LEVEL[cls]
    if lvl not in LEVELS:
        raise ValueError(f"media_resolution must be one of {'/'.join(LEVELS)} or '' "
                         f"— got {lvl!r}")
    per = table[lvl]
    return per * pages if cls == "pdf" else per


def check_drift(predicted: int, measured: int) -> dict | None:
    """{'predicted','measured','pct'} when the miss exceeds max(5 %, 50 tokens): a signal
    to re-measure the table, never an error — the request ran on the measured number."""
    if abs(measured - predicted) <= max(predicted * 0.05, 50):
        return None
    return {"predicted": predicted, "measured": measured,
            "pct": round((measured - predicted) / predicted * 100, 1)}
