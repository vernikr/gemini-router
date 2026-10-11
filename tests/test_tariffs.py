"""Tariff forecast + drift signal (wave 1): the table is the single source of truth,
the forecast is planning-only, and a miss is a recorded signal, never spend."""
from __future__ import annotations

import time

import pytest

from gemini_router import (
    MEDIA_TARIFFS,
    TariffNotMeasured,
    check_drift,
    forecast_media_tokens,
    media_class,
    pt_midnight_ts,
)
from gemini_router.ledger import Ledger


def test_forecast_matches_the_table():
    """The table is the single source of truth; the forecast must be its mirror."""
    for cls, levels in MEDIA_TARIFFS.items():
        mime = "application/pdf" if cls == "pdf" else f"{cls}/x-test"
        for lvl, price in levels.items():
            got = forecast_media_tokens(mime, pages=2, level=lvl)
            assert got == (price * 2 if cls == "pdf" else price)


def test_media_class():
    assert media_class("application/pdf") == "pdf"
    assert media_class("image/png") == "image"
    assert media_class("IMAGE/JPEG") == "image"
    assert media_class("audio/mpeg") == "audio"
    assert media_class("video/mp4") == "video"
    assert media_class("text/plain") == "text"
    assert media_class(None) == "other"
    assert media_class("garbage") == "other"


def test_forecast_image_flat_per_level():
    assert forecast_media_tokens("image/png") == 1089              # default = high
    assert forecast_media_tokens("image/jpeg", level="") == 1089
    assert forecast_media_tokens("image/webp", level="low") == 256
    assert forecast_media_tokens("image/png", level="medium") == 529
    assert forecast_media_tokens("image/png", level="high") == 1089
    assert forecast_media_tokens("image/png", pages=9) == 1089      # pages: pdf only


def test_forecast_pdf_per_page():
    assert forecast_media_tokens("application/pdf") == 560          # default = medium
    assert forecast_media_tokens("application/pdf", pages=3, level="low") == 840
    assert forecast_media_tokens("application/pdf", pages=3, level="high") == 3360


def test_forecast_refuses_unmeasured_and_bad_level():
    for mime, published in (("audio/mpeg", "25 tokens/s"), ("video/mp4", "70 tokens/frame")):
        with pytest.raises(TariffNotMeasured) as e:
            forecast_media_tokens(mime)
        assert published in str(e.value)                 # the known figure travels with the no
    with pytest.raises(TariffNotMeasured):
        forecast_media_tokens("text/plain")
    with pytest.raises(ValueError):
        forecast_media_tokens("image/png", level="ultra")


def test_check_drift_thresholds():
    assert check_drift(1089, 1089) is None
    assert check_drift(1000, 1050) is None               # exactly 5 % is not a miss
    d = check_drift(1000, 1051)
    assert d == {"predicted": 1000, "measured": 1051, "pct": 5.1}
    assert check_drift(100, 150) is None                 # 50-token floor: 50 <= 50
    assert check_drift(100, 151) is not None
    d = check_drift(1120, 560)                           # misses run both ways
    assert d["pct"] == -50.0


def test_drift_row_spends_nothing_and_reads_back(tmp_path):
    led = Ledger(tmp_path / "s.db")
    now = time.time()
    led.record_drift(key_id="ab", model="m", media_cls="image", level="high",
                     predicted=1089, measured=2000, source="mcp", now=now)
    rows = led.drift_recent(now=now)
    assert rows[0] == {"ts": now, "key_id": "ab", "model": "m", "measured": 2000,
                       "class": "image", "level": "high", "predicted": 1089}
    assert led.drift_recent(now=now + 25 * 3600) == []            # the 24 h window closes
    # no gate counts the row: spend is exactly what one real call would be
    rid = led.plan(key_id="ab", model="m", kind="single", est_tokens=100,
                   request_id=None, source="t", now=now)
    led.resolve(rid, "ok", tokens_in=100)
    day = pt_midnight_ts("America/Los_Angeles", now)
    assert led.rpd_spent("ab", "m", day, now - 86400, now - 300) == (1, 1)
    calls, toks, _ = led.window_usage("ab", "m", now - 60, now - 300)
    assert (calls, toks) == (1, 100)
