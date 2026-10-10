"""Ledger semantics: planned/resolve, dual RPD gate, windows, cooldowns, cache, prune,
cross-process sharing (FR-C3/C7/C10)."""
from __future__ import annotations

import time
from pathlib import Path

from conftest import make_config
from gemini_router import Ledger, pt_midnight_ts
from gemini_router.config import Limits, key_id
from gemini_router.routing import gates_for


def test_plan_counts_in_window_not_rpd(tmp_path):
    led = Ledger(tmp_path / "s.db")
    now = time.time()
    rid = led.plan(key_id="ab", model="m", kind="single", est_tokens=100,
                   request_id=None, source="t", now=now)
    day = pt_midnight_ts("America/Los_Angeles", now)
    # without a planned_min cutoff nothing is treated as in flight
    assert led.rpd_spent("ab", "m", day, now - 86400) == (0, 0)
    # with one, the in-flight row is spend — the request is already on its way
    assert led.rpd_spent("ab", "m", day, now - 86400, now - 300) == (1, 1)
    calls, toks, oldest = led.window_usage("ab", "m", now - 60, now - 300)
    assert (calls, toks) == (1, 100)                                   # planned blocks TPM
    led.resolve(rid, "ok", http_status=200, tokens_in=90, tokens_out=7, latency_ms=5)
    assert led.rpd_spent("ab", "m", day, now - 86400, now - 300) == (1, 1)   # not double
    calls, toks, _ = led.window_usage("ab", "m", now - 60, now - 300)
    assert (calls, toks) == (1, 90)                                    # upgraded in place


def test_stale_planned_expires_from_window(tmp_path):
    led = Ledger(tmp_path / "s.db")
    old = time.time() - 400
    led.plan(key_id="ab", model="m", kind="single", est_tokens=100,
             request_id=None, source="t", now=old)
    calls, toks, _ = led.window_usage("ab", "m", time.time() - 60, time.time() - 300)
    assert (calls, toks) == (0, 0)


def test_count_kind_never_spends(tmp_path):
    led = Ledger(tmp_path / "s.db")
    now = time.time()
    rid = led.plan(key_id="ab", model="m", kind="count", est_tokens=0,
                   request_id=None, source="count", now=now)
    led.resolve(rid, "ok", tokens_in=555)
    day = pt_midnight_ts("America/Los_Angeles", now)
    assert led.rpd_spent("ab", "m", day, now - 86400) == (0, 0)
    assert led.window_usage("ab", "m", now - 60, now - 300) == (0, 0, None)


def test_rpd_dual_gate_day_and_roll(tmp_path):
    led = Ledger(tmp_path / "s.db")
    now = time.time()
    day_start = pt_midnight_ts("America/Los_Angeles", now)
    rid = led.plan(key_id=key_id("k"), model="m", kind="single", est_tokens=1,
                   request_id=None, source="t", now=now)
    led.resolve(rid, "ok")
    d, r = led.rpd_spent(key_id("k"), "m", day_start, now - 86400)
    assert d == 1 and r == 1


def test_cooldown_roundtrip_and_expiry(tmp_path):
    led = Ledger(tmp_path / "s.db")
    now = time.time()
    led.set_cooldown("ab", "m", now + 10, "rpd_429")
    assert led.cooldown("ab", "m", now) == now + 10
    led.set_cooldown("ab", "m", now + 5, "x")          # MAX keeps the longer one
    assert led.cooldown("ab", "m", now) == now + 10
    assert led.cooldown("ab", "m", now + 11) is None    # expired → purged


def test_cache_ttl_and_request_id(tmp_path):
    led = Ledger(tmp_path / "s.db")
    now = time.time()
    led.cache_put("h1", "req-1", "m", "resp", 10, 5, now=now - 100)
    assert led.cache_get("h1", None, ttl_s=50, now=now) is None       # expired
    assert led.cache_get("h1", None, ttl_s=200, now=now)["response"] == "resp"
    assert led.cache_get(None, "req-1", ttl_s=200, now=now)["hash"] == "h1"
    assert led.cache_get("nope", "nope", ttl_s=200, now=now) is None


def test_prune_audit_and_cache(tmp_path):
    led = Ledger(tmp_path / "s.db")
    now = time.time()
    old = now - 40 * 86400
    rid = led.plan(key_id="ab", model="m", kind="single", est_tokens=1,
                   request_id=None, source="t", now=old)
    led.resolve(rid, "ok")
    led.cache_put("h", None, "m", "r", 1, 1, now=old)
    dry = led.prune(audit_keep_days=30, cache_ttl_s=24 * 3600, now=now, dry=True)
    assert dry["calls_would_prune"] == 1 and dry["calls_pruned"] == 0
    res = led.prune(audit_keep_days=30, cache_ttl_s=24 * 3600, now=now)
    assert res["calls_pruned"] == 1 and res["cache_pruned"] == 1
    assert led.calls_tail(10) == []


def test_cross_instance_sharing(tmp_path):
    path = tmp_path / "s.db"
    a = Ledger(path)
    now = time.time()
    rid = a.plan(key_id="ab", model="m", kind="single", est_tokens=5,
                 request_id=None, source="daemon", now=now)
    a.resolve(rid, "ok", tokens_in=5, tokens_out=2)
    b = Ledger(path)                                                   # second process
    day = pt_midnight_ts("America/Los_Angeles", now)
    assert b.rpd_spent("ab", "m", day, now - 86400) == (1, 1)
    assert b.calls_tail(5)[0]["source"] == "daemon"


def test_vacuum_reclaims_file_space(tmp_path):
    """DELETE alone leaves the file at full size — the retention cap needs VACUUM."""
    led = Ledger(tmp_path / "s.db")
    now = time.time()
    for _ in range(4000):
        rid = led.plan(key_id="k", model="m", kind="single", est_tokens=10,
                       request_id=None, source="mcp", now=now - 90 * 86400)
        led.resolve(rid, "ok", tokens_in=10, tokens_out=10)
    before = Path(led.path).stat().st_size
    led.prune(audit_keep_days=1, now=now)
    assert led.stats()["calls"] == 0
    assert Path(led.path).stat().st_size == before          # rows gone, file unchanged
    led.vacuum()
    assert Path(led.path).stat().st_size < before / 4       # space actually reclaimed


def test_gate_snapshot_matches_the_per_pair_accessors(tmp_path):
    """The two paths run different SQL, so they can drift; the budget tables must
    report exactly what the per-pair accessors would."""
    import random

    led = Ledger(tmp_path / "s.db")
    random.seed(7)
    now = time.time()
    models = ["m1", "m2", "m3"]
    keys = [key_id(f"k{i}") for i in range(1, 5)]
    for _ in range(300):
        ts = now - random.uniform(0, 90 * 86400)
        rid = led.plan(key_id=random.choice(keys), model=random.choice(models),
                       kind=random.choice(["single", "count"]),
                       est_tokens=random.randint(1, 500), request_id=None,
                       source="mcp", now=ts)
        led.resolve(rid, random.choice(["ok", "error_5xx", "quota_rpm"]),
                    tokens_in=random.randint(1, 500), tokens_out=random.randint(0, 90))
    for k in keys[:3]:                      # live and already-expired cooldowns
        for m in models[:2]:
            led.set_cooldown(k, m, now + random.choice([-3600, 30, 600]), "test")

    day_start, roll_start = now - 43200, now - 86400
    win_start, planned_min = now - 60, now - 300
    snap = led.gate_snapshot(day_start=day_start, roll_start=roll_start,
                             win_start=win_start, planned_min=planned_min, now=now)
    checked = 0
    for k in keys:
        for m in models:
            assert snap["spent"].get((k, m), (0, 0)) == led.rpd_spent(
                k, m, day_start, roll_start, planned_min)
            assert snap["window"].get((k, m), (0, 0, None)) == led.window_usage(
                k, m, win_start, planned_min)
            assert snap["cooldown"].get((k, m)) == led.cooldown(k, m, now)
            checked += 1
    assert checked == 12
    # a pair with no history at all is absent from the snapshot, and reads as zeros
    assert snap["spent"].get((key_id("nobody"), "m1"), (0, 0)) == (0, 0)


def test_in_flight_rows_stop_a_second_caller_from_overshooting_the_day(tmp_path):
    """The race 2.6 was about: two callers share one database, the first is mid-flight,
    and the second must see that request as spent rather than pass the same gate."""
    cfg = make_config(tmp_path, state_path=str(tmp_path / "s.db"),
                      limits_per_key_model=Limits(rpd=2, rpm=100, tpm=10_000))
    led = Ledger(cfg.state_path)
    key, model = cfg.api_keys[0], cfg.chain[0]   # in the chain, so rpd=2 applies
    now = time.time()

    def spent() -> int:
        g = gates_for(cfg, led, model, key, now=now)
        return g["rpd_limit"] - g["rpd_left"]

    assert spent() == 0
    led.plan(key_id=key_id(key), model=model, kind="single", est_tokens=10,
             request_id=None, source="first", now=now)
    assert spent() == 1                        # in flight, and already counted
    led.plan(key_id=key_id(key), model=model, kind="single", est_tokens=10,
             request_id=None, source="second", now=now)
    assert spent() == 2
    g = gates_for(cfg, led, model, key, now=now)
    assert g["rpd_left"] == 0                  # a third caller is now blocked


def test_an_abandoned_attempt_stops_counting_against_the_day(tmp_path):
    """A row stuck in `planned` is a caller that died before reaching the provider.
    It must not eat a day's budget forever — it ages out after planned_ttl_s."""
    led = Ledger(tmp_path / "s.db")
    now = time.time()
    old = now - 400
    led.plan(key_id="ab", model="m", kind="single", est_tokens=100,
             request_id=None, source="dead", now=old)
    day = pt_midnight_ts("America/Los_Angeles", now)
    assert led.rpd_spent("ab", "m", day, now - 86400, now - 300) == (0, 0)
