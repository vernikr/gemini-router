"""Token estimation: script-aware ratios, mixed-language documents, sampling of large
inputs, and the explicit-divisor escape hatch."""
from __future__ import annotations

from gemini_router.config import estimate_tokens

# ------------------------------------------------- script-aware token estimate
RU = "аналитический отчёт показатели выручка маржа регион итог квартал договор счёт"
EN = "analytical report metrics revenue margin region total quarter contract invoice"
DIGITS = "1234567890 " * 200
JSONISH = '{"id": 1, "название": "Отчёт", "значения": [1.5, 2.25], "готово": true} ' * 200


def test_estimate_is_script_aware_not_one_divisor():
    """Cyrillic runs ~1.7 chars/token and Latin ~5.4, so a single divisor cannot serve
    both: it under-estimates Russian by ~40%, and under-estimating is the direction
    that turns into a surprise 429 from the provider."""
    ru, en = estimate_tokens(RU * 200), estimate_tokens(EN * 200)
    # comparable lengths, very different token counts — and in the right order
    assert abs(len(RU * 200) - len(EN * 200)) < 0.02 * len(RU * 200)
    assert ru > en * 2
    # the old single divisor under-estimated Russian badly; the estimate must not
    assert ru > len(RU * 200) // 4
    assert en < len(EN * 200) // 4 + len(EN * 200) // 8


def test_estimate_handles_mixed_language_documents():
    """The point of the change: a document that switches scripts is estimated as a
    whole, landing between its pure-script endpoints rather than at one of them."""
    mixed = estimate_tokens((RU + " " + EN + " ") * 150)
    pure_ru = estimate_tokens(RU * 300)
    pure_en = estimate_tokens(EN * 300)
    assert pure_en < mixed < pure_ru
    # digits and punctuation-heavy payloads are their own class, not "other"
    assert estimate_tokens(DIGITS) > len(DIGITS) // 4
    assert estimate_tokens(JSONISH) > len(JSONISH) // 4


def test_estimate_samples_large_inputs_at_constant_cost():
    """Above the sample window the estimate scales a head+tail sample, so a 15 MB
    document costs the same as a 200 KB one and still lands close."""
    import time

    unit = RU + " " + EN + " 12345 — см. таблицу.\n"
    small = unit * 2_000
    big = unit * 120_000
    assert len(big) > len(small) * 50
    ratio = estimate_tokens(big) / estimate_tokens(small)
    assert abs(ratio - len(big) / len(small)) / (len(big) / len(small)) < 0.05

    t0 = time.perf_counter()
    estimate_tokens(big)
    big_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    estimate_tokens(unit * 2_100)
    small_ms = (time.perf_counter() - t0) * 1000
    # not a tight bound — CI machines vary — just proof it is not scanning 15 MB
    assert big_ms < max(200.0, small_ms * 20), (big_ms, small_ms)


def test_estimate_explicit_divisor_restores_the_old_behaviour():
    text = RU * 50
    assert estimate_tokens(text, 4) == len(text) // 4
    assert estimate_tokens(text, 2) == len(text) // 2
    assert estimate_tokens("") == 1
    assert estimate_tokens("abc") >= 1
