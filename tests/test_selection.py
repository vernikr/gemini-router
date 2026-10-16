"""The single selection procedure every entry point shares (SPEC §11.6): `tier_plan`,
`keys_sorted`, `pick_pair`, and the `overflow_default` config key that feeds them.

Owner's rule: the parameter logic must be identical regardless of the entry door and
must not be duplicated — these tests pin the shared walk itself; the proxy-side tests
in test_proxy.py pin that the pass-through actually consumes it.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from conftest import MODELS, gen_resp, make_config, route
from gemini_router import Config, Ledger, Router, RouterRequest
from gemini_router.config import key_id
from gemini_router.routing import keys_sorted, pick_pair, tier_plan

M8, M7 = "gemini-3.8-flash", "gemini-3.7-flash"
LITE = "gemini-3.5-flash-lite"


def drain(ledger: Ledger, key: str, model: str, n: int = 1) -> None:
    for _ in range(n):
        row = ledger.plan(key_id=key_id(key), model=model, kind="single",
                          est_tokens=0, request_id=None, source="test")
        ledger.resolve(row, "ok", http_status=200, latency_ms=1)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return make_config(tmp_path, api_keys=["k1", "k2"],
                       limits_per_key_model={"rpd": 1, "rpm": 5, "tpm": 250_000})


@pytest.fixture
def ledger(cfg) -> Ledger:
    return Ledger(cfg.state_path)


# ------------------------------------------------------------------- tier_plan
def test_tier_plan_off_is_chain_only(cfg):
    assert tier_plan(cfg, overflow="off") == [(0, [M8, M7])]


def test_tier_plan_lite_appends_literal_overflow(cfg):
    assert tier_plan(cfg, overflow="lite") == [(0, [M8, M7]), (1, [LITE])]


def test_tier_plan_gemma_is_a_placeholder_for_the_caller(cfg):
    assert tier_plan(cfg, overflow="lite+gemma")[-1] == (2, ["gemma-auto"])


def test_tier_plan_model_pin_replaces_the_chain(cfg):
    assert tier_plan(cfg, model_pin=M7, overflow="off") == [(0, [M7])]


# ------------------------------------------------------------------ keys_sorted
def test_keys_sorted_prefers_the_larger_remaining_budget(cfg, ledger):
    drain(ledger, "k1", M8)
    assert keys_sorted(cfg, ledger, M8) == ["k2", "k1"]


def test_keys_sorted_narrows_to_the_given_pool(cfg, ledger):
    assert keys_sorted(cfg, ledger, M8, keys=["k2"]) == ["k2"]


# -------------------------------------------------------------------- pick_pair
def test_pick_pair_fresh_pool_takes_the_chain_head(cfg, ledger):
    c = pick_pair(cfg, ledger)
    assert (c.model, c.key, c.reason, c.tier) == (M8, "k1", "ok", 0)


def test_pick_pair_rotates_to_the_budgeted_key(cfg, ledger):
    drain(ledger, "k1", M8)
    c = pick_pair(cfg, ledger)
    assert (c.model, c.key) == (M8, "k2")


def test_pick_pair_walks_the_chain_when_a_model_is_spent_everywhere(cfg, ledger):
    drain(ledger, "k1", M8)
    drain(ledger, "k2", M8)
    c = pick_pair(cfg, ledger)
    assert (c.model, c.key) == (M7, "k1")


def test_pick_pair_reaches_overflow_only_when_asked(cfg, ledger):
    for k in ("k1", "k2"):
        for m in (M8, M7):
            drain(ledger, k, m)
    assert pick_pair(cfg, ledger, overflow="off").reason == "exhausted"
    c = pick_pair(cfg, ledger, overflow="lite")
    assert (c.model, c.tier, c.reason) == (LITE, 1, "ok")


def test_pick_pair_chain_override_replaces_tier_zero(cfg, ledger):
    c = pick_pair(cfg, ledger, chain_override=[M7])
    assert c.model == M7


def test_pick_pair_avail_filter_skips_a_model_a_key_cannot_serve(cfg, ledger):
    avail = {key_id("k1"): {M7}, key_id("k2"): {M7}}
    c = pick_pair(cfg, ledger, avail=avail)
    assert c.model == M7


# -------------------------------------------------------------- overflow_default
def test_overflow_default_is_off_and_validated():
    assert Config.model_validate({"api_keys": ["k1"]}).overflow_default == "off"
    with pytest.raises(ValidationError):
        Config.model_validate({"api_keys": ["k1"], "overflow_default": "always"})


async def test_complete_inherits_overflow_default(mock_api, tmp_path):
    """A request that says nothing about overflow walks the configured default —
    the same single config key the proxy walks (no per-entry forks)."""
    cfg = make_config(tmp_path, overflow_default="lite",
                      limits_per_key_model={"rpd": 1, "rpm": 5, "tpm": 250_000})
    route(mock_api, M8, gen_resp("a"))
    route(mock_api, M7, gen_resp("b"))
    route(mock_api, LITE, gen_resp("lite-answer"))
    r = Router(cfg)
    await r.complete(RouterRequest(prompt="p1"))
    await r.complete(RouterRequest(prompt="p2"))
    res = await r.complete(RouterRequest(prompt="p3"))      # no overflow in the request
    assert res.model == LITE and res.overflow_tier == 1


async def test_explicit_off_beats_the_config_default(mock_api, tmp_path):
    from gemini_router import QuotaExhaustedError
    cfg = make_config(tmp_path, overflow_default="lite",
                      limits_per_key_model={"rpd": 1, "rpm": 5, "tpm": 250_000})
    route(mock_api, M8, gen_resp("a"))
    route(mock_api, M7, gen_resp("b"))
    r = Router(cfg)
    await r.complete(RouterRequest(prompt="p1"))
    await r.complete(RouterRequest(prompt="p2"))
    with pytest.raises(QuotaExhaustedError):
        await r.complete(RouterRequest(prompt="p3", overflow="off"))


def test_models_fixture_still_lists_the_tiers_used_here():
    """Guards the fixture assumption the tests above lean on."""
    assert {M8, M7, LITE} <= set(MODELS)
