"""Provider truth (SPEC §12): parsing, durable pair state, promotion, RPD honesty."""
from __future__ import annotations

import time

from tests.conftest import make_config

from gemini_router import (
    Ledger,
    ProviderSignal,
    QuotaTruth,
    RequestContext,
    parse_provider_signal,
)
from gemini_router.config import key_id
from gemini_router.routing import gates_for

# 2026-10-21 15:00 UTC = 08:00 PDT: the next PT reset is ~16 h away.
NOW = 1792594800.0
DAY_BODY = b'''{"error":{"code":429,"message":"You exceeded your current quota",
 "status":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.QuotaFailure",
 "violations":[{"quotaMetric":"generativelanguage.googleapis.com/generate_content_free_tier_requests",
 "quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier","quotaValue":"20"}]}]}}'''
MINUTE_BODY = b'''{"error":{"code":429,"message":"quota exceeded",
 "details":[{"metadata":{"quota_id":"GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}}]}}'''


def cfg(tmp_path, **over):
    return make_config(tmp_path, **over)


def test_day_quota_is_a_hard_block_until_pt_reset(tmp_path):
    c = cfg(tmp_path)
    s = parse_provider_signal(429, {"Retry-After": "42"}, DAY_BODY, c, now=NOW)
    assert s.kind == "day" and s.reason == "rpd_429"
    assert s.quota_id and "PerDay" in s.quota_id
    # The old implementation did min(midnight, now + retry + 5) => 47 seconds.
    assert s.until_ts - NOW > 10 * 3600


def test_retry_after_header_and_http_date_are_honoured(tmp_path):
    c = cfg(tmp_path)
    s = parse_provider_signal(429, {"Retry-After": "42"}, MINUTE_BODY, c, now=NOW)
    assert s.kind == "minute" and s.retry_after_s == 42.0
    assert 46 < s.until_ts - NOW < 48
    http_date = parse_provider_signal(
        429, {"Retry-After": "Wed, 21 Oct 2026 15:01:00 GMT"}, MINUTE_BODY, c, now=NOW)
    assert 59 <= http_date.retry_after_s <= 61


def test_retry_delay_is_parsed_from_the_body(tmp_path):
    c = cfg(tmp_path)
    body = b'{"error":{"message":"Please retry in 12.5s"}}'
    s = parse_provider_signal(429, {}, body, c, now=NOW)
    assert s.retry_after_s == 12.5
    assert s.unconfirmed and s.reason == "throttle_429"


def test_confirmed_minute_quota_is_not_buried_under_the_floor(tmp_path):
    c = cfg(tmp_path)
    s = parse_provider_signal(429, {"Retry-After": "0.2"}, MINUTE_BODY, c, now=NOW)
    assert not s.unconfirmed
    assert s.retry_after_s == 0.2
    assert s.until_ts - NOW < 10          # not a 60 s bench for a 0.2 s hint


def test_503_is_overload_and_never_rpd_spend(tmp_path):
    c = cfg(tmp_path)
    led = Ledger(c.state_path)
    s = parse_provider_signal(503, {}, b'{"error":{"message":"overloaded"}}', c, now=NOW)
    assert s.kind == "overload" and s.until_ts - NOW == c.feedback.overload_cooldown_s
    rid = led.plan(key_id="k", model="m", kind="single", est_tokens=1,
                   request_id=None, source="test")
    led.resolve(rid, "http5xx", http_status=503)
    assert led.rpd_spent("k", "m", 0, 0, None) == (0, 0)
    led.close()


def test_provider_state_overrides_fresh_counters(tmp_path):
    c = cfg(tmp_path, api_keys=["key-one"])
    led = Ledger(c.state_path)
    truth = QuotaTruth(led, c, now_fn=lambda: NOW)
    before = gates_for(c, led, c.chain[0], "key-one", 0, NOW)
    assert before["rpd_left"] == before["rpd_limit"]
    signal = parse_provider_signal(429, {}, DAY_BODY, c, now=NOW)
    kid = key_id("key-one")
    truth.apply(signal, key_id=kid, model=c.chain[0],
                context=RequestContext(initiator="test", version="0.11.0"))
    after = gates_for(c, led, c.chain[0], "key-one", 0, NOW)
    assert after["rpd_left"] == 0
    assert after["provider_blocked"] == "day"
    assert led.provider_state(kid, c.chain[0], NOW)["kind"] == "day"
    # Old readers (gemini-mcp) only know `cooldowns`; the verdict is mirrored there.
    assert led.cooldown(kid, c.chain[0], NOW) is not None
    led.close()


def test_repeated_unexplained_429_promotes_to_a_day_block(tmp_path):
    c = cfg(tmp_path)
    led = Ledger(c.state_path)
    truth = QuotaTruth(led, c, now_fn=lambda: NOW)
    signal = ProviderSignal(kind="minute", until_ts=NOW + 65, retry_after_s=60.0,
                            reason="throttle_429", status=429, unconfirmed=True)
    for expected_hits in (1, 2, 3):
        row = truth.apply(signal, key_id="k", model="m", context=RequestContext())
        assert row["hits"] == expected_hits
    assert row["kind"] == "day" and row["reason"] == "rpd_429_promoted"
    led.close()


def test_success_clears_the_block_again(tmp_path):
    c = cfg(tmp_path)
    led = Ledger(c.state_path)
    truth = QuotaTruth(led, c, now_fn=lambda: NOW)
    s = ProviderSignal(kind="minute", until_ts=NOW + 65, retry_after_s=60.0,
                       reason="throttle_429", status=429, unconfirmed=True)
    truth.apply(s, key_id="k", model="m")
    assert led.provider_state("k", "m", NOW)
    led.clear_pair_state("k", "m")
    assert led.provider_state("k", "m", NOW) is None
    assert led.cooldown("k", "m", NOW) is None
    led.close()


def test_notes_are_annotations_only(tmp_path):
    c = cfg(tmp_path)
    led = Ledger(c.state_path)
    before = len(led.calls_tail(50))
    nid = led.add_note("начинаем тестирование новой версии", initiator="ops-audit",
                       version="0.11.0", source="cli")
    assert nid and len(led.calls_tail(50)) == before
    row = led.notes_tail(1)[0]
    assert row["comment"] == "начинаем тестирование новой версии"
    assert row["initiator"] == "ops-audit" and row["version"] == "0.11.0"
    led.close()


def test_v2_database_migrates_additively(tmp_path):
    import sqlite3
    path = tmp_path / "v2.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE calls(id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_iso TEXT NOT NULL,
          request_id TEXT, source TEXT NOT NULL DEFAULT 'cli', kind TEXT NOT NULL DEFAULT 'single',
          key_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, http_status INTEGER,
          tokens_in INTEGER, tokens_out INTEGER, quota_error TEXT, retry_after_s REAL,
          latency_ms INTEGER, overflow_tier INTEGER NOT NULL DEFAULT 0, bytes INTEGER);
        INSERT INTO calls(ts,ts_iso,source,key_id,model,status) VALUES
          (1,'1970-01-01T00:00:01+00:00','mcp','k','m','ok');
        CREATE TABLE cooldowns(key_id TEXT NOT NULL, model TEXT NOT NULL,
          until_ts REAL NOT NULL, reason TEXT, PRIMARY KEY(key_id,model));
        CREATE TABLE cache(hash TEXT PRIMARY KEY, request_id TEXT, model TEXT NOT NULL,
          ts REAL NOT NULL, response TEXT NOT NULL, tokens_in INTEGER, tokens_out INTEGER);
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE fs_affinity(resource TEXT PRIMARY KEY, key_id TEXT NOT NULL, ts REAL NOT NULL);
    """)
    con.commit()
    con.close()
    led = Ledger(path)
    assert led.calls_tail(1)[0]["initiator"] == "mcp"   # backfilled from source
    assert led.provider_state("k", "m", time.time()) is None
    led.add_note("ok", version="0.11.0")
    assert led.notes_tail(1)[0]["comment"] == "ok"
    led.close()


def test_legacy_rpd_429_cooldown_is_read_as_provider_truth(tmp_path):
    """gemini-mcp still ships v0.1.1 and writes only `cooldowns`. Its daily verdict
    must still be a hard block for the new proxy — a mixed fleet shares one truth."""
    c = cfg(tmp_path)
    led = Ledger(c.state_path)
    kid = key_id("legacy-key")
    led.set_cooldown(kid, c.chain[0], NOW + 3600, "rpd_429")
    state = led.provider_state(kid, c.chain[0], NOW)
    assert state and state["kind"] == "day"
    g = gates_for(c, led, c.chain[0], "legacy-key", 0, NOW)
    assert g["rpd_left"] == 0 and g["provider_blocked"] == "day"
    snap = led.gate_snapshot(day_start=NOW - 3600, roll_start=NOW - 86400,
                             win_start=NOW - 60, planned_min=NOW - 300, now=NOW)
    assert snap["pair_state"][(kid, c.chain[0])]["kind"] == "day"
    led.close()
