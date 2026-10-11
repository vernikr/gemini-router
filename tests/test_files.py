"""Files API capability (SPEC §8): resumable upload, ACTIVE wait, measured tokens, the
upload ledger row, and a request that carries `file_data`.

respx-mocked throughout — no network, no real keys (AGENTS.md)."""
from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from conftest import gen_resp, make_config
from gemini_router import (
    FileNotReady,
    FileUploadFailed,
    QuotaExhaustedError,
    Router,
    RouterRequest,
    key_id,
)
from gemini_router.gemini import CHUNK

UPLOAD_BASE = "https://generativelanguage.googleapis.com/upload/v1beta"
BASE = "https://generativelanguage.googleapis.com/v1beta"
UP_URL = "https://upload.example.test/session/1"

FILE_INFO = {"name": "files/abc123", "uri": f"{BASE}/files/abc123",
             "mimeType": "application/pdf", "sizeBytes": "12", "state": "ACTIVE",
             "expirationTime": "2026-10-13T00:00:00Z"}


def _mock_upload(respx_mock, *, state: str = "ACTIVE", body: dict | None = None):
    """The two-step lifecycle: start → (maybe PROCESSING) → ACTIVE."""
    respx_mock.post(f"{UPLOAD_BASE}/files").mock(return_value=httpx.Response(
        200, headers={"x-goog-upload-url": UP_URL}, json={"file": {"name": "pending"}}))
    put = respx_mock.put(UP_URL).mock(return_value=httpx.Response(
        200, json={"file": body or {**FILE_INFO, "state": state}}))
    return put


def _router(tmp_path, **over) -> Router:
    return Router(make_config(tmp_path, **over))


def _blob(tmp_path, size: int = 12) -> str:
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"P" * size)
    return str(p)


# ------------------------------------------------------------------ the upload itself
async def test_upload_streams_the_blob_and_returns_the_provider_record(mock_api, tmp_path):
    put = _mock_upload(mock_api)
    router = _router(tmp_path, cache_ttl_h=1)
    up = await router.upload_file(_blob(tmp_path), mime="application/pdf", source="mcp")
    assert (up.name, up.state, up.mime) == ("files/abc123", "ACTIVE", "application/pdf")
    assert up.uri.endswith("/files/abc123") and up.size == 12
    assert up.key_id == key_id("k1")                    # the id, never the raw key
    assert len(up.sha256) == 64                         # cache key's file half
    req = put.calls[0].request
    assert req.headers["x-goog-upload-command"] == "upload, finalize"
    assert req.headers["x-goog-upload-offset"] == "0"
    assert int(req.headers["content-length"]) == 12     # explicit: no chunked encoding
    assert req.content == b"P" * 12
    start = mock_api.calls[0].request
    assert start.headers["x-goog-upload-protocol"] == "resumable"
    assert json.loads(start.content)["file"]["display_name"] == "doc.pdf"


async def test_upload_streams_files_larger_than_the_read_window(mock_api, tmp_path):
    """A 2.5-chunk blob must arrive complete: the reader is the only thing holding it."""
    size = CHUNK * 2 + 7
    put = _mock_upload(mock_api)
    router = _router(tmp_path)
    up = await router.upload_file(_blob(tmp_path, size), mime="application/pdf")
    assert len(put.calls[0].request.content) == size and up.size == 12   # provider's number


async def test_processing_state_is_waited_out(mock_api, tmp_path):
    """Small files are ACTIVE in the finalize answer; large ones are not, and only then
    does the poll loop run."""
    _mock_upload(mock_api, state="PROCESSING")
    states = ["PROCESSING", "PROCESSING", "ACTIVE"]
    mock_api.get(f"{BASE}/files/abc123").mock(
        side_effect=lambda request: httpx.Response(
            200, json={**FILE_INFO, "state": states.pop(0)}))
    router = _router(tmp_path)
    up = await router.upload_file(_blob(tmp_path))
    assert up.state == "ACTIVE" and not states


async def test_failed_upload_is_named_and_never_recorded(mock_api, tmp_path):
    _mock_upload(mock_api, state="FAILED")
    mock_api.get(f"{BASE}/files/abc123").mock(return_value=httpx.Response(
        200, json={**FILE_INFO, "state": "FAILED"}))
    router = _router(tmp_path)
    with pytest.raises(FileUploadFailed):
        await router.upload_file(_blob(tmp_path))
    assert not [r for r in router.ledger.calls_tail(10) if r["kind"] == "upload"]


async def test_a_stuck_processing_file_times_out_as_not_ready(mock_api, tmp_path):
    _mock_upload(mock_api, state="PROCESSING")
    mock_api.get(f"{BASE}/files/abc123").mock(return_value=httpx.Response(
        200, json={**FILE_INFO, "state": "PROCESSING"}))
    router = _router(tmp_path)
    with pytest.raises(FileNotReady):
        await router.upload_file(_blob(tmp_path), ready_timeout_s=0.05)


async def test_key_rejection_moves_the_upload_to_the_next_key(mock_api, tmp_path):
    """Files belong to a key's own project, so a dead key must not burn the blob."""
    mock_api.post(f"{UPLOAD_BASE}/files").mock(side_effect=[
        httpx.Response(401, json={"error": {"message": "API key not valid"}}),
        httpx.Response(200, headers={"x-goog-upload-url": UP_URL},
                       json={"file": {"name": "pending"}})])
    mock_api.put(UP_URL).mock(return_value=httpx.Response(200, json={"file": FILE_INFO}))
    router = _router(tmp_path, api_keys=["dead", "live"])
    up = await router.upload_file(_blob(tmp_path))
    assert up.key_id != "dead" and up.state == "ACTIVE"


# ------------------------------------------------------------------ token measurement
async def test_count_file_tokens_sends_a_file_part(mock_api, tmp_path):
    seen = {}

    def cap(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"totalTokens": 4321})

    mock_api.post(f"{BASE}/models/gemini-3.8-flash:countTokens").mock(side_effect=cap)
    router = _router(tmp_path)
    n = await router.count_file_tokens("gemini-3.8-flash", FILE_INFO["uri"],
                                       "application/pdf")
    assert n == 4321
    part = seen["contents"][0]["parts"][0]["file_data"]
    assert part == {"mime_type": "application/pdf", "file_uri": FILE_INFO["uri"]}


# ------------------------------------------------------------------ the ledger row
async def test_upload_is_recorded_but_never_draws_on_the_day(mock_api, tmp_path):
    """Two mechanisms keep an upload out of the model gates: the row's sentinel model and
    the non-spending kind. The first one is what a per-model query sees, so it is the one
    a careless change (storing a real model id) would break — hence the assertions below
    are on the queries a gate actually runs, for every configured model."""
    from gemini_router.ledger import FILE_ROW_MODEL, NON_SPENDING_KINDS, SPENDS

    _mock_upload(mock_api)
    router = _router(tmp_path)
    up = await router.upload_file(_blob(tmp_path))
    rows = [r for r in router.ledger.calls_tail(5) if r["kind"] == "upload"]
    assert len(rows) == 1 and rows[0]["bytes"] == 12 and rows[0]["status"] == "ok"
    assert rows[0]["key_id"] == up.key_id and rows[0]["model"] == FILE_ROW_MODEL
    assert FILE_ROW_MODEL not in router.cfg.chain + router.cfg.overflow_tiers
    for model in router.cfg.chain + router.cfg.overflow_tiers:
        assert router.ledger.rpd_spent(up.key_id, model, 0, 0) == (0, 0)
        assert router.ledger.last_call_ts(up.key_id, model) is None
    # the accounting contract itself: every non-spending kind is excluded everywhere
    assert "upload" in NON_SPENDING_KINDS
    assert all(f"'{k}'" in SPENDS for k in NON_SPENDING_KINDS)


# ------------------------------------------------------------------ requests with files
async def _prepared(mock_api, tmp_path, **over):
    _mock_upload(mock_api)
    router = _router(tmp_path, **over)
    up = await router.upload_file(_blob(tmp_path), mime="application/pdf")
    return router, up


async def test_complete_sends_the_file_ahead_of_the_text_and_pins_the_key(mock_api, tmp_path):
    router, up = await _prepared(mock_api, tmp_path)
    seen = {}

    def cap(request):
        seen["payload"] = json.loads(request.content)
        seen["key"] = request.headers["x-goog-api-key"]
        return gen_resp("analysis")

    mock_api.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(side_effect=cap)
    res = await router.complete(RouterRequest(prompt="what is this?", est_tokens=100),
                                files=[up])
    parts = seen["payload"]["contents"][0]["parts"]
    assert parts[0]["file_data"] == {"mime_type": "application/pdf", "file_uri": up.uri}
    assert parts[1]["text"] == "what is this?"          # provider guide: file first
    assert seen["key"] == "k1" and res.text == "analysis"


async def test_files_require_a_measured_estimate(mock_api, tmp_path):
    """A text-only estimate would under-count the TPM gate on the one request where being
    wrong costs a 429 mid-job."""
    router, up = await _prepared(mock_api, tmp_path)
    with pytest.raises(Exception) as e:
        await router.complete(RouterRequest(prompt="x"), files=[up])
    assert "est_tokens is required" in str(e.value)


async def test_cache_keys_on_file_bytes_not_on_the_provider_uri(mock_api, tmp_path):
    """The URI changes on every re-upload (48-hour expiry, another key); the bytes do not.
    Keying on the URI would miss for work already done."""
    router, up = await _prepared(mock_api, tmp_path)
    calls = []
    mock_api.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(
        side_effect=lambda request: (calls.append(request), gen_resp("once"))[1])

    first = await router.complete(RouterRequest(prompt="q", est_tokens=50), files=[up])
    # same bytes, a fresh upload record with another URI — still the same work
    again = await router.complete(RouterRequest(prompt="q", est_tokens=50),
                                  files=[replace(up, uri=BASE + "/files/other")])
    assert first.cached is False and again.cached is True
    # and the other direction: a DIFFERENT file under the same prompt must not hit it
    other = await router.complete(RouterRequest(prompt="q", est_tokens=50),
                                  files=[replace(up, sha256="0" * 64)])
    assert other.cached is False
    assert other.text == "once" and len(calls) == 2


async def test_no_key_behind_the_upload_refuses_with_the_reason(mock_api, tmp_path):
    """The pin is a promise: when the uploading key cannot serve the model, the caller
    must hear 're-upload', not watch the request fail against an invisible file."""
    router, up = await _prepared(mock_api, tmp_path)
    with pytest.raises(Exception) as e:
        await router.complete(RouterRequest(prompt="q", est_tokens=5, model="gemma-4-31b-it"),
                              files=[up])
    assert "re-upload" in str(e.value)


async def test_upload_refuses_when_no_key_can_serve_the_target(mock_api, tmp_path):
    """Spending an upload for a key that cannot then answer is pure waste."""
    router = _router(tmp_path, limits_per_key_model={"rpd": 0, "rpm": 5, "tpm": 1000})
    with pytest.raises(QuotaExhaustedError) as e:
        await router.upload_file(_blob(tmp_path))
    assert "no key can serve" in e.value.hint


async def test_delete_file_is_best_effort(mock_api, tmp_path):
    mock_api.delete(f"{BASE}/files/abc123").mock(return_value=httpx.Response(404))
    router = _router(tmp_path)
    await router.delete_file("files/abc123")            # 404 = already gone, not an error


# ------------------------------------------------------------------ schema migration
def test_an_existing_v1_database_migrates_in_place(tmp_path):
    """The production database is shared (daemon, stdio sessions, tldr-digest) and older
    code opens it too: adding a column must be additive and must keep every row."""
    import sqlite3

    from gemini_router.ledger import SCHEMA_VERSION, Ledger

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE calls(
          id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_iso TEXT NOT NULL, request_id TEXT,
          source TEXT NOT NULL DEFAULT 'cli', kind TEXT NOT NULL DEFAULT 'single',
          key_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, http_status INTEGER,
          tokens_in INTEGER, tokens_out INTEGER, quota_error TEXT, retry_after_s REAL,
          latency_ms INTEGER, overflow_tier INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE cooldowns(key_id TEXT NOT NULL, model TEXT NOT NULL, until_ts REAL NOT NULL,
          reason TEXT, PRIMARY KEY(key_id, model));
        CREATE TABLE cache(hash TEXT PRIMARY KEY, request_id TEXT, model TEXT NOT NULL,
          ts REAL NOT NULL, response TEXT NOT NULL, tokens_in INTEGER, tokens_out INTEGER);
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        INSERT INTO meta VALUES('schema_version', '1');
        INSERT INTO calls(ts, ts_iso, key_id, model, status, tokens_in)
          VALUES(1.0, '1970-01-01T00:00:01+00:00', 'k1', 'gemini-3.8-flash', 'ok', 7);
    """)
    conn.commit()
    conn.close()

    led = Ledger(path)
    assert led.rpd_spent("k1", "gemini-3.8-flash", 0, 0) == (1, 1)     # the row survived
    row_id = led.record_upload(key_id="k1", nbytes=99)
    assert row_id
    cols = {r[1] for r in led._conn.execute("PRAGMA table_info(calls)")}
    assert "bytes" in cols
    assert led._conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()[0] \
        == SCHEMA_VERSION
    led.close()


# ------------------------------------------------------------- media resolution level
async def test_the_level_rides_on_the_part_and_the_measurement(mock_api, tmp_path):
    """A level pinned on the file must appear in BOTH calls: the count that feeds the TPM
    gate and the request that spends. Measuring at one level and sending at another makes
    the gate and the ledger disagree — the exact bug this test exists to prevent."""
    router, up = await _prepared(mock_api, tmp_path)
    up = up.with_resolution("high")
    counted, sent = {}, {}

    mock_api.post(f"{BASE}/models/gemini-3.8-flash:countTokens").mock(
        side_effect=lambda r: (counted.update(json.loads(r.content)),
                               httpx.Response(200, json={"totalTokens": 1120}))[1])
    mock_api.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(
        side_effect=lambda r: (sent.update(json.loads(r.content)), gen_resp("ok"))[1])

    n = await router.count_file_tokens("gemini-3.8-flash", up.uri, up.mime,
                                       key_id=up.key_id, media_resolution=up.media_resolution)
    await router.complete(RouterRequest(prompt="q", est_tokens=n + 10), files=[up])

    want = {"level": "MEDIA_RESOLUTION_HIGH"}
    assert n == 1120
    assert counted["contents"][0]["parts"][0]["media_resolution"] == want
    assert sent["contents"][0]["parts"][0]["media_resolution"] == want
    assert sent["contents"][0]["parts"][1]["text"] == "q"


async def test_no_level_means_no_field_and_the_maximum_is_available(mock_api, tmp_path):
    """Inheriting the provider default stays possible (None/""), and the top level is
    reachable — the owner's call for PDFs is high, which doubles their per-page cost."""
    from gemini_router.gemini import RESOLUTION_LEVELS, file_parts, resolution_field

    assert resolution_field(None) is None and resolution_field("") is None
    assert resolution_field("high") == {"level": "MEDIA_RESOLUTION_HIGH"}
    assert sorted(RESOLUTION_LEVELS) == ["high", "low", "medium"]

    router, up = await _prepared(mock_api, tmp_path)
    sent = {}
    mock_api.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(
        side_effect=lambda r: (sent.update(json.loads(r.content)), gen_resp("ok"))[1])
    await router.complete(RouterRequest(prompt="q", est_tokens=50), files=[up])
    assert "media_resolution" not in sent["contents"][0]["parts"][0]
    assert file_parts([up.with_resolution("low")])[0]["media_resolution"] == {
        "level": "MEDIA_RESOLUTION_LOW"}


async def test_an_unknown_level_fails_before_any_request(mock_api, tmp_path):
    router, up = await _prepared(mock_api, tmp_path)
    with pytest.raises(ValueError) as e:
        up.with_resolution("huge")
    assert "media resolution must be one of" in str(e.value)
    with pytest.raises(ValueError):
        await router.count_file_tokens("gemini-3.8-flash", up.uri, up.mime,
                                       media_resolution="huge")
