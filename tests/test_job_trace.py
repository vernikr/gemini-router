"""Job/trace correlation (SPEC §13): headers, schema v4, ledger, HTTP and CLI."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from urllib.parse import quote

import pytest
from tests.conftest import make_config
from tests.test_proxy import API, K1, client, gen_ok, spawn

from gemini_router import (
    CLIENT_CONTEXT_HEADERS,
    Config,
    Ledger,
    QuotaTruth,
    RequestContext,
    make_traceparent,
    parse_provider_signal,
    parse_traceparent,
)
from gemini_router.cli import main

JOB = "digest-2026-10-28"
TRACE = "run-7f3a9c2e"
NOW = 1792594800.0
MINUTE_BODY = b'''{"error":{"code":429,"message":"quota exceeded",
 "details":[{"metadata":{"quota_id":"GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}}]}}'''


def ctx_headers(**over) -> dict:
    h = {"X-Router-Job": JOB, "X-Router-Trace": TRACE, "X-Router-Step": "prefilter",
         "X-Router-Initiator": "tldr-digest", "X-Router-Request-Id": "req-1",
         "X-Router-Comment": quote("утренняя сборка"),
         "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
    h.update(over)
    return h


# ------------------------------------------------------------------- context
def test_context_headers_carry_the_whole_identity():
    ctx = RequestContext.from_headers(ctx_headers(), default_initiator="mcp")
    assert ctx.job == JOB and ctx.trace_id == TRACE and ctx.step == "prefilter"
    assert ctx.initiator == "tldr-digest" and ctx.request_id == "req-1"
    assert ctx.comment == "утренняя сборка"
    assert ctx.parent_span == "00f067aa0ba902b7"
    fields = ctx.log_fields()
    assert fields["job"] == JOB and fields["trace"] == TRACE and fields["step"] == "prefilter"
    assert fields["initiator"] == "tldr-digest" and fields["comment"] == "утренняя сборка"
    assert "x-router-parent-span" in CLIENT_CONTEXT_HEADERS
    assert "traceparent" in CLIENT_CONTEXT_HEADERS
    for name in ("X-Router-Job", "X-Router-Trace", "X-Router-Step", "traceparent"):
        assert name.lower() in CLIENT_CONTEXT_HEADERS


def test_default_job_groups_the_initiators_utc_day():
    ctx = RequestContext.from_headers({}, default_initiator="tldr-digest")
    assert re.fullmatch(r"tldr-digest@\d{4}-\d{2}-\d{2}", ctx.job)
    assert ctx.trace_id == ctx.job
    assert ctx.step == ""


def test_traceparent_is_always_valid_w3c():
    for trace, span in ((TRACE, "span"), ("", ""),
                        ("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7")):
        tp = make_traceparent(trace, span)
        assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", tp)
        parsed = parse_traceparent(tp)
        assert parsed and parsed[2] and len(parsed[0]) == 32
    invalid = "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert parse_traceparent(invalid) == ("", "", "")


# -------------------------------------------------------------------- ledger
def test_schema_v4_migrates_an_older_database(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE calls(id INTEGER PRIMARY KEY, ts REAL, ts_iso TEXT, request_id TEXT,
            source TEXT, kind TEXT, key_id TEXT, model TEXT, status TEXT);
        CREATE TABLE notes(id INTEGER PRIMARY KEY, ts REAL, ts_iso TEXT, comment TEXT);
        CREATE TABLE pair_state(key_id TEXT, model TEXT, kind TEXT, until_ts REAL,
            first_ts REAL, updated_ts REAL);
        CREATE TABLE cooldowns(key_id TEXT, model TEXT, until_ts REAL);
        CREATE TABLE cache(hash TEXT PRIMARY KEY, response TEXT);
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE fs_affinity(resource TEXT PRIMARY KEY, key_id TEXT, ts REAL);
        INSERT INTO calls(ts,ts_iso,source,kind,key_id,model,status)
            VALUES(1,'2026-10-01T00:00:00+00:00','cli','single','k1','m','ok');
    """)
    con.commit()
    con.close()

    led = Ledger(path)
    version = led._conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()[0]
    call_cols = {r["name"] for r in led._conn.execute("PRAGMA table_info(calls)")}
    note_cols = {r["name"] for r in led._conn.execute("PRAGMA table_info(notes)")}
    tables = {r[0] for r in led._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert version == "4"
    assert "jobs" in tables and "pair_state" in tables
    for col in ("job", "trace_id", "step", "hop", "parent_span", "request_bytes",
                "response_bytes", "method", "path", "provider_reason"):
        assert col in call_cols
    for col in ("job", "trace_id", "step", "event"):
        assert col in note_cols
    row = led.calls_tail(1)[0]
    assert row["job"] == "cli@2026-10-01" and row["trace_id"] == row["job"]
    led.close()


def test_job_aggregates_calls_notes_and_lifecycle(tmp_path):
    led = Ledger(tmp_path / "state.db")
    led.job_start(JOB, initiator="tldr-digest", version="0.12.0", comment="start",
                  trace_id=TRACE, step="init")
    ok = led.plan(key_id="k1", model="gemini-3.8-flash", kind="single", est_tokens=10,
                  request_id="req-1", source="test", initiator="tldr-digest",
                  version="0.12.0", job=JOB, trace_id=TRACE, step="prefilter", hop=0,
                  requested_model="gemini-2.5-flash", request_bytes=120,
                  method="POST", path="/v1beta/models/gemini-3.8-flash:generateContent")
    led.resolve(ok, "ok", http_status=200, response_bytes=340)
    refused = led.plan(key_id="k2", model="gemini-3.6-flash", kind="single", est_tokens=10,
                       request_id="req-2", source="test", job=JOB, trace_id=TRACE,
                       step="analysis", hop=1)
    led.resolve(refused, "quota", http_status=429, quota_id="GenerateRequestsPerDay")
    led.add_note("prefilter done", initiator="tldr-digest", version="0.12.0",
                 job=JOB, trace_id=TRACE, step="prefilter")
    led.job_finish(JOB, status="done", initiator="tldr-digest", version="0.12.0",
                   comment="ok", step="finish")

    summary = led.job_summary(JOB)
    assert summary["status"] == "done" and summary["finished_ts"]
    assert summary["calls"] == 2 and summary["ok"] == 1
    assert summary["refusals"] == 1 and summary["quota_429"] == 1
    assert summary["models"] == ["gemini-3.6-flash", "gemini-3.8-flash"]
    assert summary["keys"] == ["k1", "k2"]
    assert summary["traces"] == [TRACE]
    assert summary["steps"] == ["analysis", "prefilter"]
    events = led.notes_tail(10, job=JOB)
    assert [e["event"] for e in reversed(events)] == ["job_start", "note", "job_finish"]
    assert events[1]["step"] == "prefilter"
    assert len(led.job_calls(JOB, 100)) == 2
    assert led.jobs_tail()[0]["job"] == JOB
    prefilter = led.calls_tail(10, job=JOB, step="prefilter")
    assert len(prefilter) == 1 and prefilter[0]["request_bytes"] == 120
    assert prefilter[0]["response_bytes"] == 340 and prefilter[0]["hop"] == 0
    assert prefilter[0]["trace_id"] == TRACE
    assert led.calls_tail(10, trace_id=TRACE, job=JOB)
    assert led.calls_tail(10, job="does-not-exist") == []
    led.close()


def test_provider_feedback_log_and_pair_state_carry_the_job(tmp_path, caplog):
    cfg = make_config(tmp_path)
    led = Ledger(cfg.state_path)
    ctx = RequestContext(initiator="tldr-digest", version="0.12.0",
                         job=JOB, trace_id=TRACE, step="prefilter")
    signal = parse_provider_signal(429, {"Retry-After": "60"}, MINUTE_BODY, cfg, now=NOW)
    with caplog.at_level(logging.INFO, logger="gemini_router.feedback"):
        QuotaTruth(led, cfg, now_fn=lambda: NOW).apply(
            signal, key_id="k1", model="gemini-3.8-flash", context=ctx)
    state = led.provider_state("k1", "gemini-3.8-flash", NOW)
    assert state["job"] == JOB and state["trace_id"] == TRACE
    assert "event=provider_feedback" in caplog.text
    assert f"job={JOB}" in caplog.text and f"trace={TRACE}" in caplog.text
    assert "step=prefilter" in caplog.text
    led.close()


# ---------------------------------------------------------------------- HTTP
def test_proxy_job_end_to_end(tmp_path, respx_mock, caplog):
    route = respx_mock.post(f"{API}/models/gemini-3.8-flash:generateContent").mock(
        return_value=gen_ok())
    with spawn(tmp_path, respx_mock) as proxy:
        with client(proxy) as c:
            h = ctx_headers()
            with caplog.at_level(logging.INFO, logger="gemini_router.proxy"):
                start = c.post("/v1/job", json={"action": "start", "job": JOB,
                                                "comment": "start"}, headers=h)
                generated = c.post("/v1beta/models/gemini-3.8-flash:generateContent",
                                   json={"contents": [{"parts": [{"text": "q"}]}]},
                                   headers=h)
                note = c.post("/v1/note", json={"comment": "stage done",
                                                "step": "prefilter"}, headers=h)
            assert start.status_code == 200 and start.json()["summary"]["status"] == "running"
            assert generated.status_code == 200 and note.status_code == 200
            assert generated.headers["x-router-job"] == JOB
            assert generated.headers["x-router-trace"] == TRACE
            assert generated.headers["x-router-request-id"] == "req-1"
            assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01",
                                generated.headers["x-router-traceparent"])
            assert generated.headers["x-router-version"]

            listing = c.get("/v1/jobs", params={"n": 10}).json()
            assert any(j["job"] == JOB for j in listing["jobs"])
            one = c.get("/v1/job", params={"id": JOB, "n": 50}).json()
            assert one["summary"]["calls"] == 1 and one["summary"]["ok"] == 1
            assert len(one["events"]) == 2 and len(one["calls"]) == 1
            assert {e["event"] for e in one["events"]} == {"job_start", "note"}
            filtered = c.get("/v1/calls", params={"job": JOB, "step": "prefilter",
                                                  "trace": TRACE}).json()
            assert len(filtered["calls"]) == 1 and filtered["calls"][0]["job"] == JOB
            notes = c.get("/v1/notes", params={"job": JOB, "n": 10}).json()
            assert len(notes["notes"]) == 2 and notes["notes"][0]["step"] == "prefilter"
            finished = c.post("/v1/job", json={"action": "finish", "job": JOB,
                                               "status": "done"}, headers=h)
            assert finished.status_code == 200
            assert finished.json()["summary"]["status"] == "done"

        row = proxy.ledger.calls_tail(1)[0]
        assert row["job"] == JOB and row["trace_id"] == TRACE
        assert row["step"] == "prefilter" and row["request_id"] == "req-1"
        assert row["hop"] == 0 and row["parent_span"] == "00f067aa0ba902b7"
        assert row["requested_model"] == "gemini-3.8-flash"
        assert row["method"] == "POST" and row["path"].endswith(":generateContent")
        assert row["request_bytes"] > 0 and row["response_bytes"] > 0
        assert row["initiator"] == "tldr-digest" and row["comment"] == "утренняя сборка"
        # Router-only headers never reach Google; the upstream key is a pool key.
        upstream = route.calls.last.request.headers
        for name in CLIENT_CONTEXT_HEADERS:
            assert name not in upstream
        assert upstream["x-goog-api-key"] == K1
        assert "event=job" in caplog.text and f"job={JOB}" in caplog.text
        assert "event=request" in caplog.text and "hop=" in caplog.text


def test_proxy_job_get_returns_404_for_unknown_job(tmp_path, respx_mock):
    with spawn(tmp_path, respx_mock) as proxy:
        with client(proxy) as c:
            r = c.get("/v1/job", params={"id": "missing"})
    assert r.status_code == 404 and r.json()["error"]["code"] == 404


def test_proxy_rejects_unknown_job_action(tmp_path, respx_mock):
    with spawn(tmp_path, respx_mock) as proxy:
        with client(proxy) as c:
            r = c.post("/v1/job", json={"action": "pause", "job": JOB})
    assert r.status_code == 400


# ----------------------------------------------------------------------- CLI
@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "k1")
    monkeypatch.setenv("GEMINI_ROUTER_STATE", str(tmp_path / "state.db"))
    monkeypatch.delenv("GEMINI_ROUTER_CONFIG", raising=False)
    return tmp_path


@pytest.mark.usefixtures("cli_env")
def test_cli_job_lifecycle_and_audit(capsys):
    assert main(["job-start", JOB, "--initiator", "tldr-digest", "--trace", TRACE,
                 "--step", "init", "--comment", "start"]) == 0
    assert "event=job" in capsys.readouterr().out
    assert main(["note", "prefilter done", "--job", JOB, "--trace", TRACE,
                 "--step", "prefilter"]) == 0
    capsys.readouterr()
    assert main(["jobs", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["job"] == JOB
    assert main(["job", JOB, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["job"] == JOB and len(report["events"]) == 2
    assert main(["notes", "--job", JOB, "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert main(["calls", "--job", JOB, "--step", "prefilter", "--wide", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["job-finish", JOB, "--status", "done", "--step", "finish"]) == 0
    capsys.readouterr()
    assert main(["jobs", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "done"


def test_cli_missing_job_returns_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEYS", "k1")
    monkeypatch.setenv("GEMINI_ROUTER_STATE", str(tmp_path / "state.db"))
    assert main(["job", "missing", "--json"]) == 1


def test_config_defaults_keep_provider_truth_enabled():
    cfg = Config.model_validate({"api_keys": ["k1"], "state_path": "state.db"})
    assert cfg.feedback.promote_hits == 3 and cfg.proxy.walk_wait is True
    assert cfg.proxy.pool_circuit_breaker is True
    assert cfg.proxy.default_initiator == "filesearch"
