"""Version / initiator / comment travel with every request — and nowhere else."""
from __future__ import annotations

import json
import logging

import httpx
from tests.test_proxy import API, K1, TOKEN, client, spawn

from gemini_router import RequestContext, audit_line, router_version


def test_context_from_headers_and_defaults():
    ctx = RequestContext.from_headers({
        "X-Router-Initiator": "tldr-digest",
        "X-Router-Comment": "starting new version testing",
        "X-Router-Request-Id": "req-42",
    }, default_initiator="filesearch")
    assert ctx.initiator == "tldr-digest"
    assert ctx.comment == "starting new version testing"
    assert ctx.request_id == "req-42"
    assert ctx.version == router_version()
    assert RequestContext.from_headers({}, default_initiator="mcp").initiator == "mcp"


def test_cyrillic_comment_can_travel_percent_encoded():
    from urllib.parse import quote
    text = "начинаем тестирование новой версии"
    ctx = RequestContext.from_headers({"X-Router-Comment": quote(text)})
    assert ctx.comment == text


def test_audit_line_quotes_free_text():
    line = audit_line("note", v="0.12.0", comment="hello world = test", initiator="ops")
    assert "event=note" in line and "v=0.12.0" in line and "initiator=ops" in line
    assert '"hello world = test"' in line


def test_proxy_rows_carry_version_initiator_and_comment(tmp_path, respx_mock):
    route = respx_mock.post(f"{API}/models/gemini-3.8-flash:generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": [
            {"content": {"parts": [{"text": "A"}]}, "finishReason": "STOP"}]}))
    with spawn(tmp_path, respx_mock) as proxy:
        with client(proxy) as c:
            r = c.post("/v1beta/models/gemini-3.8-flash:generateContent",
                       json={"contents": [{"parts": [{"text": "q"}]}]},
                       headers={"X-Router-Initiator": "tldr-digest",
                                "X-Router-Comment": "starting new version testing",
                                "X-Router-Request-Id": "digest-20261028"})
        assert r.status_code == 200
        row = proxy.ledger.calls_tail(1)[0]
        assert row["version"] == router_version()
        assert row["initiator"] == "tldr-digest" and row["source"] == "tldr-digest"
        assert row["comment"] == "starting new version testing"
        assert row["request_id"] == "digest-20261028"
        # Context headers are consumed locally: they must never reach Google.
        upstream = route.calls.last.request.headers
        for h in ("x-router-initiator", "x-router-comment", "x-router-request-id"):
            assert h not in upstream


def test_audit_line_contains_identity_fields(tmp_path, respx_mock, caplog):
    respx_mock.post(f"{API}/models/gemini-3.8-flash:generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": [
            {"content": {"parts": [{"text": "A"}]}, "finishReason": "STOP"}]}))
    with spawn(tmp_path, respx_mock) as proxy:
        with caplog.at_level(logging.INFO, logger="gemini_router.proxy"):
            with client(proxy) as c:
                c.post("/v1beta/models/gemini-3.8-flash:generateContent",
                       json={"contents": [{"parts": [{"text": "q"}]}]},
                       headers={"X-Router-Initiator": "arena-agent",
                                "X-Router-Comment": "smoke"})
    assert "event=request" in caplog.text
    assert f"v={router_version()}" in caplog.text
    assert "initiator=arena-agent" in caplog.text
    assert "comment=smoke" in caplog.text
    assert TOKEN not in caplog.text and K1 not in caplog.text


def test_healthz_reports_the_running_version(tmp_path, respx_mock):
    with spawn(tmp_path, respx_mock) as proxy:
        with httpx.Client(base_url=proxy.base) as c:
            body = c.get("/healthz").json()
    assert body["version"] == router_version()


def test_note_endpoint_writes_only_a_note(tmp_path, respx_mock):
    with spawn(tmp_path, respx_mock) as proxy:
        with client(proxy) as c:
            r = c.post("/v1/note", json={"comment": "начинаем тестирование новой версии",
                                         "initiator": "ops-audit"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert r.json()["version"] == router_version()
        note = proxy.ledger.notes_tail(1)[0]
        assert note["comment"] == "начинаем тестирование новой версии"
        assert note["initiator"] == "ops-audit" and note["source"] == "http"
        # No upstream route was registered: any provider call would have raised in respx.
        assert proxy.ledger.calls_tail(10) == []
        with client(proxy) as c:
            listing = c.get("/v1/notes?n=5").json()
        assert listing["ok"] and listing["notes"][0]["comment"].startswith("начинаем")


def test_note_endpoint_needs_a_comment(tmp_path, respx_mock):
    with spawn(tmp_path, respx_mock) as proxy:
        with client(proxy) as c:
            assert c.post("/v1/note", json={}).status_code == 400


def test_note_cli_is_annotation_only(tmp_path, monkeypatch, capsys):
    from gemini_router.cli import main
    monkeypatch.setenv("GEMINI_API_KEYS", "k1")
    monkeypatch.setenv("GEMINI_ROUTER_STATE", str(tmp_path / "state.db"))
    assert main(["note", "начинаем тестирование новой версии", "--initiator", "ops"]) == 0
    out = capsys.readouterr().out
    assert "event=note" in out and f"v={router_version()}" in out
    assert "ops" in out
    assert main(["notes", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["initiator"] == "ops"
