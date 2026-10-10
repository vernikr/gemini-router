"""Router behavior: chain fallback, RPD/RPM/TPM gates, wait policy (FR-C13), cache,
overflow tiers, retries, live 429 cooldown, multi-key pool, quota payloads (FR-B8)."""
from __future__ import annotations

import httpx
import pytest

from conftest import BASE, gen_resp, make_config, r429_day, r429_minute, route
from gemini_router import QuotaExhaustedError, Router, RouterError, RouterRequest
from gemini_router.config import key_id

M8, M7, M6 = "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"
LITE, GEMMA = "gemini-3.5-flash-lite", "gemma-4-12b-it"  # pick_gemma prefers highest numbers


def req(prompt: str, **kw) -> RouterRequest:
    return RouterRequest(prompt=prompt, **kw)


async def test_happy_path(mock_api, tmp_path):
    route(mock_api, M8, gen_resp("hello", 11, 7))
    r = Router(make_config(tmp_path))
    evs = []
    res = await r.complete(req("p1"), on_event=evs.append)
    assert (res.text, res.model, res.tokens_in, res.tokens_out) == ("hello", M8, 11, 7)
    assert not res.cached and res.overflow_tier == 0
    stages = [e.stage for e in evs]
    assert stages[0] == "routing" and "dispatch" in stages and stages[-1] == "ok"
    assert res.quota_after[0]["model"] == M8 and res.quota_after[0]["rpd_left"] == 19
    assert [e.stage for e in res.events] == stages


async def test_fallback_on_404(mock_api, tmp_path):
    route(mock_api, M8, httpx.Response(404, content=b"not found"))
    route(mock_api, M7, gen_resp("via7"))
    r = Router(make_config(tmp_path))
    res = await r.complete(req("p"))
    assert res.model == M7 and res.text == "via7"
    rows = r.ledger.calls_tail(10)
    assert {row["model"]: row["status"] for row in rows} == {M8: "http4xx", M7: "ok"}


async def test_rpd_gate_routes_then_raises_structured_error(mock_api, tmp_path):
    cfg = make_config(tmp_path, limits_per_key_model={"rpd": 1, "rpm": 5, "tpm": 250_000})
    route(mock_api, M8, [gen_resp("a"), gen_resp("a2")])
    route(mock_api, M7, [gen_resp("b"), gen_resp("b2")])
    r = Router(cfg)
    assert (await r.complete(req("p1"))).model == M8
    assert (await r.complete(req("p2"))).model == M7          # M8 spent → next in chain
    with pytest.raises(QuotaExhaustedError) as ei:
        await r.complete(req("p3"))
    d = ei.value.to_dict()
    assert d["error"] == "quota_exhausted" and d["waited"] is True
    assert {m["model"]: m["rpd_left"] for m in d["per_model"]} == {M8: 0, M7: 0}
    assert "overflow" in d["hint"] and d["reset_eta_pt"]


async def test_rpm_wait_enabled_counts_down_and_succeeds(mock_api, tmp_path):
    cfg = make_config(tmp_path, limits_per_key_model={"rpd": 20, "rpm": 1, "tpm": 250_000},
                      window_s=1.0)
    route(mock_api, M8, [gen_resp("one"), gen_resp("two")])
    route(mock_api, M7, gen_resp("never"))
    r = Router(cfg)
    assert (await r.complete(req("p1"))).text == "one"
    evs = []
    res = await r.complete(req("p2"), on_event=evs.append)     # gated: waits ~1 s
    assert res.model == M8 and res.text == "two"
    waiting = [e for e in evs if e.stage == "waiting"]
    assert waiting and waiting[0].wait_s > 0 and waiting[0].detail["reason"] == "rpm"
    assert all(e.model == M8 for e in waiting)                 # countdown is attributable


async def test_rpm_wait_disabled_skips_to_next_model(mock_api, tmp_path):
    cfg = make_config(tmp_path, limits_per_key_model={"rpd": 20, "rpm": 1, "tpm": 250_000},
                      window_s=1.0)
    route(mock_api, M8, gen_resp("one"))
    route(mock_api, M7, gen_resp("via7"))
    r = Router(cfg)
    await r.complete(req("p1"))
    evs = []
    res = await r.complete(req("p2", wait=False), on_event=evs.append)
    assert res.model == M7 and res.text == "via7"
    assert not [e for e in evs if e.stage == "waiting"]
    assert any(e.stage == "switch" and e.detail.get("reason") == "rpm_wait_skip" for e in evs)


async def test_wait_cap_forces_skip(mock_api, tmp_path):
    cfg = make_config(tmp_path, limits_per_key_model={"rpd": 20, "rpm": 1, "tpm": 250_000},
                      window_s=5.0, wait={"enabled_default": True, "max_s": 0.2, "poll_s": 0.05})
    route(mock_api, M8, gen_resp("one"))
    route(mock_api, M7, gen_resp("via7"))
    r = Router(cfg)
    await r.complete(req("p1"))
    res = await r.complete(req("p2"))                          # wait_s≈5 > cap 0.2 → skip
    assert res.model == M7


async def test_tpm_gate(mock_api, tmp_path):
    cfg = make_config(tmp_path, limits_per_key_model={"rpd": 20, "rpm": 5, "tpm": 50})
    route(mock_api, M8, gen_resp("one", tin=40))
    route(mock_api, M7, gen_resp("via7"))
    r = Router(cfg)
    await r.complete(req("p1", est_tokens=40))
    evs = []
    res = await r.complete(req("p2", est_tokens=40, wait=False), on_event=evs.append)
    assert res.model == M7                                     # 40+40 > 50 → gated → skip
    assert any(e.detail.get("reason") == "tpm_wait_skip" for e in evs)


async def test_cache_hit_spends_nothing(mock_api, tmp_path):
    g = route(mock_api, M8, gen_resp("cached-answer"))
    r = Router(make_config(tmp_path))
    a = await r.complete(req("same prompt"))
    b = await r.complete(req("same prompt"))
    assert a.text == b.text == "cached-answer" and b.cached and not a.cached
    assert g.call_count == 1
    assert r.quota_table()[0]["rpd_left"] == 19                # one spend total


async def test_request_id_idempotency(mock_api, tmp_path):
    g = route(mock_api, M8, gen_resp("first"))
    r = Router(make_config(tmp_path))
    await r.complete(req("prompt A", request_id="rid-1"))
    b = await r.complete(req("totally different prompt", request_id="rid-1"))
    assert b.text == "first" and b.cached and b.key_id == "cache"
    assert g.call_count == 1


async def test_overflow_lite_opt_in(mock_api, tmp_path):
    cfg = make_config(tmp_path, limits_per_key_model={"rpd": 1, "rpm": 5, "tpm": 250_000})
    route(mock_api, M8, gen_resp("a"))
    route(mock_api, M7, gen_resp("b"))
    route(mock_api, LITE, gen_resp("lite-answer"))
    r = Router(cfg)
    await r.complete(req("p1"))
    await r.complete(req("p2"))
    with pytest.raises(QuotaExhaustedError):
        await r.complete(req("p3"))                            # default: hard stop
    evs = []
    res = await r.complete(req("p3", overflow="lite"), on_event=evs.append)
    assert res.model == LITE and res.overflow_tier == 1 and res.text == "lite-answer"
    assert any(e.stage == "overflow" and e.detail["tier"] == 1 for e in evs)
    assert r.ledger.calls_tail(1)[0]["overflow_tier"] == 1


async def test_overflow_gemma_second_tier(mock_api, tmp_path):
    cfg = make_config(tmp_path, limits_per_key_model={"rpd": 1, "rpm": 5, "tpm": 250_000})
    route(mock_api, M8, gen_resp("a"))
    route(mock_api, M7, gen_resp("b"))
    route(mock_api, LITE, r429_day())                          # lite day-quota dead too
    route(mock_api, GEMMA, gen_resp("gemma-answer"))
    r = Router(cfg)
    await r.complete(req("p1"))
    await r.complete(req("p2"))
    res = await r.complete(req("p3", overflow="lite+gemma"))
    assert res.model == GEMMA and res.overflow_tier == 2       # pick_gemma: highest numbers
    assert res.text == "gemma-answer"


async def test_retry_on_500_then_success(mock_api, tmp_path):
    route(mock_api, M8, [httpx.Response(500, content=b"boom"), gen_resp("recovered")])
    r = Router(make_config(tmp_path))
    evs = []
    res = await r.complete(req("p"), on_event=evs.append)
    assert res.text == "recovered"
    assert any(e.stage == "retry" for e in evs)
    assert r.ledger.calls_tail(1)[0]["status"] == "ok"         # one row, upgraded


async def test_live_429_day_sets_cooldown_and_skips_model(mock_api, tmp_path):
    g8 = route(mock_api, M8, r429_day())
    g7 = route(mock_api, M7, gen_resp("via7"))
    r = Router(make_config(tmp_path))
    res = await r.complete(req("p1"))
    assert res.model == M7
    assert r.ledger.cooldown(key_id("k1"), M8) is not None
    res2 = await r.complete(req("p2"))
    assert res2.model == M7                                    # M8 cooled down → straight to M7
    assert g8.call_count == 1 and g7.call_count == 2


async def test_minute_429_waited_when_enabled(mock_api, tmp_path):
    route(mock_api, M8, [r429_minute("0.2"), gen_resp("after-minute-429")])
    r = Router(make_config(tmp_path))
    evs = []
    res = await r.complete(req("p"), on_event=evs.append)
    assert res.text == "after-minute-429" and res.model == M8
    assert any(e.stage == "waiting" and e.detail.get("reason") == "http_429" for e in evs)


async def test_multi_key_pool_prefers_fresh_key(mock_api, tmp_path):
    cfg = make_config(tmp_path, api_keys=["k1", "k2"],
                      limits_per_key_model={"rpd": 1, "rpm": 5, "tpm": 250_000})
    route(mock_api, M8, [gen_resp("first"), gen_resp("second")])
    r = Router(cfg)
    a = await r.complete(req("p1"))
    b = await r.complete(req("p2"))
    assert a.model == b.model == M8                            # same model, different keys
    assert {a.key_id, b.key_id} == {key_id("k1"), key_id("k2")}
    rows = r.quota_table()
    assert sum(row["rpd_left"] for row in rows if row["model"] == M8) == 0


async def test_count_tokens_does_not_spend_rpd(mock_api, tmp_path):
    mock_api.post(f"{BASE}/models/{M8}:countTokens").mock(side_effect=[
        httpx.Response(200, json={"totalTokens": 123}),
        httpx.Response(200, json={"totalTokens": 124}),
    ])
    r = Router(make_config(tmp_path))
    assert await r.count_tokens("abc") == 123
    assert await r.count_tokens("abcd") == 124
    row = next(row for row in r.quota_table() if row["model"] == M8)
    assert row["rpd_left"] == row["rpd_limit"] == 20


async def test_pinned_model_ok(mock_api, tmp_path):
    route(mock_api, M6, gen_resp("pinned"))
    r = Router(make_config(tmp_path))
    res = await r.complete(req("p", model=M6))
    assert res.model == M6 and res.text == "pinned"


async def test_pinned_unavailable_raises(mock_api, tmp_path):
    r = Router(make_config(tmp_path))
    with pytest.raises(RouterError, match="unavailable"):
        await r.complete(req("p", model="gemini-9.9-flash"))


async def test_refused_key_falls_through_to_the_next_key(mock_api, tmp_path):
    """A 401 means the KEY is dead, not the model: the pool keeps going and the dead
    key is not retried for later models either."""
    seen: list[str] = []

    def gen(request):
        key = request.headers["x-goog-api-key"]
        seen.append(key)
        if key == "k1":
            return httpx.Response(401, json={"error": {"message": "API key not valid"}})
        return gen_resp("ok")

    mock_api.post(url__regex=r".*:generateContent").mock(side_effect=gen)
    r = Router(make_config(tmp_path, api_keys=["k1", "k2"], chain=[M8, M7]))
    res = await r.complete(req("hi", wait=False))
    assert res.text == "ok" and res.key_id == key_id("k2")
    assert seen == ["k1", "k2"]              # k1 refused once, never retried on M7
    await r.aclose()


async def test_whole_pool_refused_names_the_fix(mock_api, tmp_path):
    def gen(request):
        return httpx.Response(401, json={"error": {"message": "API key not valid"}})

    mock_api.post(url__regex=r".*:generateContent").mock(side_effect=gen)
    r = Router(make_config(tmp_path, api_keys=["k1", "k2"], chain=[M8]))
    with pytest.raises(RouterError, match="every configured API key was rejected"):
        await r.complete(req("hi", wait=False))
    await r.aclose()


def test_quota_exhausted_message_has_no_dangling_separator():
    assert str(QuotaExhaustedError([], "eta")) == "quota exhausted"
