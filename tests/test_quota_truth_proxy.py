"""The 2026-10-21 incident, pinned as tests.

Three client requests, every key×model pair answering an unexplained 429. Before
provider truth was authoritative the proxy re-walked the same exhausted pairs inside
each request: 48 upstream refusals in ten seconds, 3 of them client-visible. Now the
first request discovers the truth once, and the next two are answered locally.
"""
from __future__ import annotations

import httpx
from tests.test_proxy import API, K1, K2, client, kid, spawn

CHAIN = ["gemini-3.8-flash", "gemini-3.6-flash"]
LITE = "gemini-3.5-flash-lite"
MODELS = CHAIN + [LITE]


def _unexplained_429():
    # No quotaId, no Retry-After: exactly what the free tier sent during the blip.
    return httpx.Response(429, json={"error": {"code": 429, "message":
                                               "You exceeded your current quota"}})


def _ok():
    return httpx.Response(200, json={"candidates": [
        {"content": {"parts": [{"text": "A"}]}, "finishReason": "STOP"}]})


def _mock_all(respx_mock, responder):
    for m in MODELS:
        respx_mock.post(f"{API}/models/{m}:generateContent").mock(side_effect=responder)


def test_429_storm_is_bounded_and_never_retries_a_pair(tmp_path, respx_mock):
    calls: list[tuple[str, str]] = []

    def upstream(request):
        model = request.url.path.split("/models/")[1].split(":")[0]
        calls.append((request.headers.get("x-goog-api-key"), model))
        return _unexplained_429()

    _mock_all(respx_mock, upstream)
    with spawn(tmp_path, respx_mock, chain=CHAIN, overflow_default="lite",
               overflow_tiers=[LITE]) as p:
        statuses = []
        with client(p) as c:
            for _ in range(3):
                r = c.post("/v1beta/models/gemini-3.8-flash:generateContent",
                           json={"contents": [{"parts": [{"text": "q"}]}]})
                statuses.append((r.status_code, r.headers.get("retry-after")))
        distinct_pairs = len({kid(k) for k in (K1, K2)}) * len(MODELS)

    assert len(calls) == distinct_pairs, "one discovery hop per pair, not 16 per request"
    assert len(set(calls)) == len(calls), "no pair is retried inside the walk"
    assert all(s == 429 for s, _ in statuses)
    assert statuses[1][1] and statuses[2][1], "later callers get a local Retry-After"


def test_a_second_request_does_not_touch_the_provider_again(tmp_path, respx_mock):
    n = []

    def upstream(request):
        n.append(1)
        return _unexplained_429()

    _mock_all(respx_mock, upstream)
    with spawn(tmp_path, respx_mock, chain=CHAIN, overflow_default="lite",
               overflow_tiers=[LITE]) as p:
        with client(p) as c:
            first = c.post("/v1beta/models/gemini-3.8-flash:generateContent",
                           json={"contents": [{"parts": [{"text": "q"}]}]})
            before = len(n)
            second = c.post("/v1beta/models/gemini-3.8-flash:generateContent",
                            json={"contents": [{"parts": [{"text": "q"}]}]})
    assert first.status_code == second.status_code == 429
    assert len(n) == before, "circuit breaker answered locally, no new upstream call"


def test_confirmed_day_429_blocks_until_the_pt_reset(tmp_path, respx_mock):
    from gemini_router.ledger import pt_midnight_ts
    body = {"error": {"message": "You exceeded your current quota", "details": [
        {"metadata": {"quota_id": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                      "quota_value": "20"}}]}}

    def upstream(request):
        return httpx.Response(429, headers={"Retry-After": "42"}, json=body)

    _mock_all(respx_mock, upstream)
    with spawn(tmp_path, respx_mock, chain=CHAIN, overflow_default="off") as p:
        with client(p) as c:
            r = c.post("/v1beta/models/gemini-3.8-flash:generateContent",
                       json={"contents": [{"parts": [{"text": "q"}]}]})
        assert r.status_code == 429
        row = p.ledger.provider_state(kid(K1), CHAIN[0])
        assert row and row["kind"] == "day"
        assert row["until_ts"] > pt_midnight_ts(p.cfg.quota_day_tz) + 3600
        # The Retry-After header survived all the way into the audit row.
        calls = [c for c in p.ledger.calls_tail(20) if c["http_status"] == 429]
        assert calls and any(c["quota_id"] and "PerDay" in c["quota_id"] for c in calls)


def test_a_success_clears_an_earlier_provider_block(tmp_path, respx_mock):
    seen: list[int] = []

    def upstream(request):
        seen.append(1)
        return _unexplained_429() if len(seen) == 1 else _ok()

    _mock_all(respx_mock, upstream)
    with spawn(tmp_path, respx_mock, chain=CHAIN, overflow_default="off") as p:
        with client(p) as c:
            r = c.post("/v1beta/models/gemini-3.8-flash:generateContent",
                       json={"contents": [{"parts": [{"text": "q"}]}]})
        assert r.status_code == 200
        served = (r.headers["x-router-key-id"], r.headers["x-router-model"])
        alive = p.ledger.pair_states()
    assert served not in alive, "the pair that just served 200 has no block left"
