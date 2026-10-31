"""Key transports (keyauth): one pool serves every key generation.

The provider's answer decides, not the key's shape — these tests pin that behaviour:
a healthy key keeps the standard header and costs exactly one request; a key whose surface
refuses the header rotates to the next transport and is remembered; a leaked key is never
rotated (no transport resurrects a revoked credential) and arrives as KeyLeakedError; a
model-permission 403 is not an auth failure and must not trigger rotation.
"""
from __future__ import annotations

import httpx
import pytest

from conftest import BASE, gen_resp, make_config
from gemini_router import GeminiClient, keyauth
from gemini_router.errors import GeminiHTTPError, KeyLeakedError

UPLOAD = "https://generativelanguage.googleapis.com/upload/v1beta"


def _transport_of(request: httpx.Request) -> str:
    if "x-goog-api-key" in request.headers:
        return "header"
    if "key" in request.url.params:
        return "query"
    if request.headers.get("authorization", "").startswith("Bearer "):
        return "bearer"
    return "none"


async def _generate(cfg, key: str, model: str = "gemini-3.8-flash"):
    client = GeminiClient(cfg)
    try:
        return await client.generate(key, model, prompt="p")
    finally:
        await client.aclose()


async def test_healthy_key_keeps_one_header_request(mock_api, tmp_path):
    """The ordinary path is untouched: standard header, no probing, no extra calls."""
    cfg = make_config(tmp_path, api_keys=["AIzaSyDUMMYfake_key_for_tests_0"])
    seen: list[str] = []
    mock_api.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(
        side_effect=lambda request: (seen.append(_transport_of(request)), gen_resp("ok"))[1])
    await _generate(cfg, cfg.api_keys[0])
    assert seen == ["header"]
    assert keyauth.chosen(cfg.api_keys[0]) == "header"


async def test_key_refused_on_the_header_rotates_to_the_query_and_is_remembered(
        mock_api, tmp_path):
    """An AQ.-style key whose surface wants ?key= works after one rotation, and the
    second call goes straight there — the pool self-heals instead of reporting a dead key."""
    key = "AQ.DUMMYquery_transport_key_0000000000"
    cfg = make_config(tmp_path, api_keys=[key])
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        t = _transport_of(request)
        seen.append(t)
        if t == "header":
            return httpx.Response(401, json={"error": {"message": "Request had invalid "
                                                                 "authentication credentials."}})
        return gen_resp("ok")

    mock_api.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(side_effect=answer)
    await _generate(cfg, key)
    assert seen == ["header", "query"]                     # rotated once, then worked
    seen.clear()
    await _generate(cfg, key)
    assert seen == ["query"]                               # remembered: no wasted 401
    assert keyauth.chosen(key) == "query"


async def test_bearer_transport_is_also_a_candidate(mock_api, tmp_path):
    """Some AQ. surfaces expect the key as a bearer token; the same rotation finds it."""
    key = "AQ.DUMMYbearer_transport_key_000000000"
    cfg = make_config(tmp_path, api_keys=[key])
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        t = _transport_of(request)
        seen.append(t)
        if t == "bearer":
            return gen_resp("ok")
        return httpx.Response(403, json={"error": {"message": "The request is missing a "
                                                              "valid API key."}})

    mock_api.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(side_effect=answer)
    await _generate(cfg, key)
    assert seen == ["header", "query", "bearer"]
    assert keyauth.chosen(key) == "bearer"


async def test_leaked_key_is_not_rotated_and_names_itself(mock_api, tmp_path):
    """A leaked-key response is reported clearly and does not trigger transport retries."""
    key = "AIzaSyDUMMYleaked_key_for_tests_00000"
    cfg = make_config(tmp_path, api_keys=[key])
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(_transport_of(request))
        return httpx.Response(403, json={"error": {"message":
                              "Your API key was reported as leaked. Please use another "
                              "API key.", "status": "PERMISSION_DENIED"}})

    mock_api.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(side_effect=answer)
    with pytest.raises(KeyLeakedError) as e:
        await _generate(cfg, key)
    assert calls == ["header"]                             # exactly one attempt
    assert e.value.key_rejected and e.value.switch_model    # failover moves to the next key
    assert "keys add" in str(e.value)                       # and the fix is spelt out


async def test_model_permission_403_still_switches_models(mock_api, tmp_path):
    """A 403 about a model, not the credential, is not an auth failure: it must not burn
    two extra round trips, and it must keep its model-switch semantics."""
    key = "AIzaSyDUMMYmodel_perm_key_for_tests_000"
    cfg = make_config(tmp_path, api_keys=[key])
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(_transport_of(request))
        return httpx.Response(403, json={"error": {"message": "Permission denied on the "
                                                              "model for this project."}})

    mock_api.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(side_effect=answer)
    with pytest.raises(GeminiHTTPError) as e:
        await _generate(cfg, key)
    assert calls == ["header"]
    assert e.value.switch_model and not e.value.key_rejected


async def test_upload_start_uses_the_same_transport_logic(mock_api, tmp_path):
    """Uploads are where the header-only assumption hurt most: the start call must rotate
    exactly like generateContent, or a query-only key could never ingest a file."""
    key = "AQ.DUMMYupload_transport_key_0000000000"
    cfg = make_config(tmp_path, api_keys=[key])
    blob = tmp_path / "three.pdf"
    blob.write_bytes(b"%PDF-1.4\nDUMMY\n")
    seen: list[str] = []

    def start(request: httpx.Request) -> httpx.Response:
        t = _transport_of(request)
        seen.append(t)
        if t != "query":
            return httpx.Response(401, json={"error": {"message": "invalid credentials"}})
        return httpx.Response(200, headers={"x-goog-upload-url": f"{UPLOAD}/session/1"})

    mock_api.post(f"{UPLOAD}/files").mock(side_effect=start)
    mock_api.put(f"{UPLOAD}/session/1").mock(return_value=httpx.Response(200, json={
        "file": {"name": "files/f1", "uri": "https://x/files/f1", "mimeType": "application/pdf",
                 "sizeBytes": str(blob.stat().st_size), "state": "ACTIVE"}}))
    client = GeminiClient(cfg)
    try:
        up = await client.upload_file(key, blob, mime="application/pdf")
    finally:
        await client.aclose()
    assert seen == ["header", "query"]
    assert up.uri == "https://x/files/f1"
    assert keyauth.chosen(key) == "query"


def test_probe_key_reports_what_the_daemon_will_find(respx_mock):
    """`keys add` must show the same truth the router acts on — including the transport."""
    key = "AQ.DUMMYprobe_transport_key_0000000000"
    respx_mock.get(f"{BASE}/models").mock(side_effect=lambda request: (
        httpx.Response(200, json={"models": []})
        if _transport_of(request) == "query"
        else httpx.Response(401, json={"error": {"message": "invalid credentials"}})))

    res = keyauth.probe_key(key, BASE)
    assert res["ok"] and res["transport"] == "query" and res["reason"] == "ok"
    assert keyauth.chosen(key) == "query"                  # the CLI's finding is reusable


def test_probe_key_speaks_plainly_about_a_leaked_key(respx_mock):
    respx_mock.get(f"{BASE}/models").mock(return_value=httpx.Response(
        403, json={"error": {"message": "Your API key was reported as leaked."}}))
    res = keyauth.probe_key("AIzaSyDUMMYleaked_probe_key_0000000", BASE)
    assert not res["ok"] and "leaked" in res["reason"] and "delete" in res["reason"]


def test_probe_key_gives_up_immediately_on_a_non_auth_answer(respx_mock):
    """A 404 means the endpoint, not the credential: rotating transports would be noise."""
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(_transport_of(request))
        return httpx.Response(404, json={"error": {"message": "not found"}})

    respx_mock.get(f"{BASE}/models").mock(side_effect=answer)
    res = keyauth.probe_key("AIzaSyDUMMYnotfound_probe_key_00000", BASE)
    assert calls == ["header"] and res["reason"] == "HTTP 404"
