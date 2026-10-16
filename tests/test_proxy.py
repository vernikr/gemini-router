"""Native-Gemini pass-through (SPEC §11): the surface
github.com/mikesmitty/file-search drives through google.golang.org/genai.

Everything here is mocked (respx) against the real proxy process-bound server: no
network, no provider keys. Fixtures carry the DUMMY marker the secrets guard requires.
"""
from __future__ import annotations

import threading

import httpx
import pytest

from gemini_router import Config, Ledger
from gemini_router.proxy import GeminiProxy

TOKEN = "DUMMY-router-token"
K1 = "DUMMY-upstream-key-1"
K2 = "DUMMY-upstream-key-2"
UPSTREAM = "https://generativelanguage.googleapis.com"
API = f"{UPSTREAM}/v1beta"
UPLOAD = f"{UPSTREAM}/upload/v1beta"


def kid(k: str) -> str:
    from gemini_router.config import key_id
    return key_id(k)


def make_cfg(tmp_path, **over) -> Config:
    data = dict(
        chain=["gemini-3.8-flash", "gemini-3.6-flash"],
        api_keys=[K1, K2],
        state_path=str(tmp_path / "state.db"),
        limits_per_key_model={"rpd": 20, "rpm": 5, "tpm": 250_000},
        retry={"max": 2, "backoff_base_s": 0.01, "jitter_max_s": 0.01,
               "provider_timeout_s": 5, "request_delay_s": 0},
        proxy={"bind": "127.0.0.1", "port": 0, "token_env": "GEMINI_ROUTER_PROXY_TOKEN",
               "request_timeout_s": 5.0},
    )
    data.update(over)
    return Config.model_validate(data)


@pytest.fixture
def proxy(tmp_path, respx_mock):
    # The proxy's own listener is real HTTP on loopback: only the upstream calls are
    # mocked, so the test's client must pass straight through respx to reach it.
    respx_mock.route(host="127.0.0.1").pass_through()
    cfg = make_cfg(tmp_path)
    p = GeminiProxy(cfg, ledger=Ledger(cfg.state_path), token=TOKEN)
    httpd = p.server()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    p.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield p
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        p.close()


def client(proxy, token: str = TOKEN) -> httpx.Client:
    return httpx.Client(base_url=proxy.base, headers={"x-goog-api-key": token},
                        timeout=10.0)


def store_body(name: str = "fileSearchStores/demo") -> dict:
    return {"name": name, "displayName": "demo"}


# ------------------------------------------------------------------ availability
def test_healthz_is_open(proxy):
    with httpx.Client(base_url=proxy.base) as c:
        r = c.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["keys"] == 2


def test_unauthenticated_is_401_with_www_authenticate(proxy):
    with httpx.Client(base_url=proxy.base) as c:
        r = c.get("/v1beta/fileSearchStores", params={"pageSize": 1})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Bearer realm="gemini-router"'
    assert r.json()["error"]["code"] == 401


def test_wrong_token_is_401(proxy):
    with client(proxy, "DUMMY-wrong") as c:
        r = c.get("/v1beta/files")
    assert r.status_code == 401


def test_bearer_form_is_accepted(proxy, respx_mock):
    respx_mock.get(f"{API}/files").mock(return_value=httpx.Response(200, json={"files": []}))
    with httpx.Client(base_url=proxy.base,
                      headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        assert c.get("/v1beta/files").status_code == 200


def test_unknown_path_is_json_404(proxy):
    with client(proxy) as c:
        r = c.get("/v1beta/cachedContents")
    assert r.status_code == 404 and r.json()["error"]["code"] == 404


def test_well_known_is_json_not_text(proxy):
    """A text/plain 404 crashed the client's JSON parser on the OAuth discovery ladder."""
    with httpx.Client(base_url=proxy.base) as c:
        r = c.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


# --------------------------------------------------------------------- passthrough
def test_list_stores_forwards_path_query_and_headers(proxy, respx_mock):
    route = respx_mock.get(f"{API}/fileSearchStores").mock(
        return_value=httpx.Response(200, json={"fileSearchStores": [store_body()]}))
    with client(proxy) as c:
        r = c.get("/v1beta/fileSearchStores", params={"pageSize": 10, "pageToken": "p2"})
    assert r.status_code == 200
    sent = route.calls.last.request
    assert dict(sent.url.params) == {"pageSize": "10", "pageToken": "p2"}
    assert sent.headers["x-goog-api-key"] in (K1, K2)


def test_client_credential_never_reaches_upstream(proxy, respx_mock):
    route = respx_mock.get(f"{API}/files").mock(return_value=httpx.Response(200, json={}))
    with client(proxy) as c:
        c.get("/v1beta/files")
    raw = route.calls.last.request.read()
    request_text = str(route.calls.last.request.url) + str(
        route.calls.last.request.headers) + raw.decode()
    assert TOKEN not in request_text
    assert route.calls.last.request.headers["x-goog-api-key"] != TOKEN


def test_error_body_is_passed_through_byte_for_byte(proxy, respx_mock):
    """The SDK parses `{"error":{...}}` and expects `sizeBytes` as a string —
    re-serialising the body on the way through breaks it."""
    body = (b'{"error":{"code":404,"message":"FileSearchStore not found",'
            b'"status":"NOT_FOUND","details":[{"@type":"x","sizeBytes":"12"}]}}')
    respx_mock.get(f"{API}/fileSearchStores/gone").mock(
        return_value=httpx.Response(404, content=body,
                                    headers={"content-type": "application/json"}))
    with client(proxy) as c:
        r = c.get("/v1beta/fileSearchStores/gone")
    assert r.status_code == 404
    assert r.content == body
    assert r.json()["error"]["status"] == "NOT_FOUND"


def test_create_store_binds_affinity(proxy, respx_mock):
    respx_mock.post(f"{API}/fileSearchStores").mock(
        return_value=httpx.Response(200, json=store_body()))
    with client(proxy) as c:
        r = c.post("/v1beta/fileSearchStores", json={"displayName": "demo"})
    assert r.status_code == 200
    owner = proxy.ledger.fs_owner("fileSearchStores/demo")
    assert owner in (kid(K1), kid(K2))


def test_store_request_is_pinned_to_its_owner(proxy, respx_mock):
    """Rotation must not move a store to another Google project: the owner wins even
    when the other key has more budget."""
    owner = kid(K2)
    proxy.ledger.fs_bind("fileSearchStores/demo", owner)
    # make K1 strictly richer, so the ordinary rotation would pick it
    proxy.ledger.plan(key_id=kid(K1), model="gemini-3.8-flash", kind="single",
                      est_tokens=0, request_id=None, source="t")
    route = respx_mock.get(f"{API}/fileSearchStores/demo/documents").mock(
        return_value=httpx.Response(200, json={"documents": []}))
    with client(proxy) as c:
        c.get("/v1beta/fileSearchStores/demo/documents")
    assert route.calls.last.request.headers["x-goog-api-key"] == K2


def test_generate_content_reads_the_store_from_the_body(proxy, respx_mock):
    proxy.ledger.fs_bind("fileSearchStores/demo", kid(K2))
    proxy.cfg.proxy.model_policy = "passthrough"   # this test is about the body's store
    route = respx_mock.post(f"{API}/models/gemini-2.5-flash:generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": [
            {"content": {"parts": [{"text": "ANSWER"}]}, "finishReason": "STOP"}]}))
    with client(proxy) as c:
        r = c.post("/v1beta/models/gemini-2.5-flash:generateContent", json={
            "contents": [{"role": "user", "parts": [{"text": "q"}]}],
            "tools": [{"fileSearch": {"fileSearchStoreNames":
                                      ["fileSearchStores/demo"]}}]})
    assert r.status_code == 200
    assert route.calls.last.request.headers["x-goog-api-key"] == K2


def test_unknown_store_probes_other_keys_and_remembers(proxy, respx_mock):
    """An affinity lost to a wiped state file must heal, not 404 forever."""
    first = respx_mock.get(f"{API}/fileSearchStores/demo").mock(side_effect=[
        httpx.Response(404, json={"error": {"code": 404, "message": "not this project"}}),
        httpx.Response(200, json=store_body()),
    ])
    with client(proxy) as c:
        r = c.get("/v1beta/fileSearchStores/demo")
    assert r.status_code == 200, "a read of an unknown store must try the other projects"
    assert first.call_count == 2, "one attempt per project, then it stops"
    assert proxy.ledger.fs_owner("fileSearchStores/demo") in (kid(K1), kid(K2))


def test_create_is_never_retried(proxy, respx_mock):
    """A second POST could create a second store: one attempt, then the truth."""
    route = respx_mock.post(f"{API}/fileSearchStores").mock(
        return_value=httpx.Response(503, json={"error": {"code": 503, "message": "down"}}))
    with client(proxy) as c:
        r = c.post("/v1beta/fileSearchStores", json={"displayName": "demo"})
    assert r.status_code == 503 and route.call_count == 1


# ------------------------------------------------------------------- upload flow
def test_upload_start_rewrites_the_upload_url(proxy, respx_mock):
    respx_mock.post(f"{UPLOAD}/fileSearchStores/demo:uploadToFileSearchStore").mock(
        return_value=httpx.Response(
            200, json={}, headers={
                "x-goog-upload-url": f"{UPLOAD}/fileSearchStores/demo"
                                     f":uploadToFileSearchStore?upload_id=UP1"
                                     f"&upload_protocol=resumable",
                "x-goog-upload-status": "active"}))
    with client(proxy) as c:
        r = c.post("/upload/v1beta/fileSearchStores/demo:uploadToFileSearchStore",
                   headers={"X-Goog-Upload-Protocol": "resumable",
                            "X-Goog-Upload-Command": "start"},
                   json={"mimeType": "text/plain"})
    assert r.status_code == 200
    url = r.headers["x-goog-upload-url"]
    assert url.startswith(proxy.base + "/upload/v1beta/")
    assert "upload_id=UP1" in url and "router_k=" in url and "router_s=" in url


def test_full_resumable_upload_cycle(proxy, respx_mock):
    """start → chunked continuation → finalize: the continuation is authenticated and
    lands in the same Google project as the start."""
    start = respx_mock.post(
        f"{UPLOAD}/fileSearchStores/demo:uploadToFileSearchStore").mock(
        side_effect=[
            httpx.Response(200, json={}, headers={
                "x-goog-upload-url": f"{UPLOAD}/fileSearchStores/demo"
                                     f":uploadToFileSearchStore?upload_id=UP1"
                                     f"&upload_protocol=resumable",
                "x-goog-upload-status": "active"}),
            httpx.Response(200, json={"name": "fileSearchStores/demo/operations/op-1",
                                      "done": True},
                           headers={"x-goog-upload-status": "final"}),
        ])
    with client(proxy) as c:
        r0 = c.post("/upload/v1beta/fileSearchStores/demo:uploadToFileSearchStore",
                    headers={"X-Goog-Upload-Protocol": "resumable",
                             "X-Goog-Upload-Command": "start"},
                    json={"mimeType": "text/plain"})
        url = r0.headers["x-goog-upload-url"]
        r1 = c.post(url, content=b"x" * 2048,
                    headers={"X-Goog-Upload-Command": "upload, finalize",
                             "X-Goog-Upload-Offset": "0"})
    assert r1.status_code == 200 and r1.json()["done"] is True
    assert r1.headers["x-goog-upload-status"] == "final"
    cont = start.calls.last.request
    assert "router_k" not in str(cont.url) and "router_s" not in str(cont.url)
    assert "upload_id=UP1" in str(cont.url)
    assert start.calls[0].request.headers["x-goog-api-key"] == \
        cont.headers["x-goog-api-key"], "a continuation must not change project mid-upload"


def test_upload_id_needing_percent_encoding_survives_the_round_trip(proxy, respx_mock):
    """Google's upload ids carry ':' and '+': the id is url-encoded on the way out, so it
    must be decoded before the signature is checked or every continuation is rejected."""
    tricky = "UP:a+b/c="
    respx_mock.post(f"{UPLOAD}/files").mock(side_effect=[
        httpx.Response(200, json={}, headers={
            "x-goog-upload-url": f"{UPLOAD}/files?upload_id={tricky}"
                                 f"&upload_protocol=resumable",
            "x-goog-upload-status": "active"}),
        httpx.Response(200, json={"file": {"name": "files/f-1", "state": "ACTIVE"}},
                       headers={"x-goog-upload-status": "final"}),
    ])
    with client(proxy) as c:
        r0 = c.post("/upload/v1beta/files",
                    headers={"X-Goog-Upload-Protocol": "resumable",
                             "X-Goog-Upload-Command": "start"},
                    json={"file": {"mimeType": "text/plain"}})
        r1 = c.post(r0.headers["x-goog-upload-url"], content=b"payload")
    assert r1.status_code == 200, r1.text
    assert r1.headers["x-goog-upload-status"] == "final"


def test_forged_upload_session_is_rejected(proxy, respx_mock):
    with client(proxy) as c:
        r = c.post("/upload/v1beta/files?upload_id=UP1&upload_protocol=resumable"
                   f"&router_k={kid(K1)}&router_s=deadbeef", content=b"x")
    assert r.status_code == 403 and r.json()["error"]["code"] == 403


def test_upload_chunk_larger_than_the_inspect_limit_is_streamed(proxy, respx_mock):
    blob = bytes(range(256)) * 9000                      # ~2.3 MiB, above the buffer limit
    route = respx_mock.post(f"{UPLOAD}/files").mock(return_value=httpx.Response(
        200, json={"file": {"name": "files/f-1", "uri": "https://x/f",
                            "mimeType": "text/plain", "sizeBytes": str(len(blob)),
                            "state": "ACTIVE"}}, headers={"x-goog-upload-status": "final"}))
    with client(proxy) as c:
        r = c.post("/upload/v1beta/files?upload_id=UP9&upload_protocol=resumable"
                   f"&router_k={kid(K1)}"
                   f"&router_s={proxy.tickets._sig(TOKEN, f'UP9|{kid(K1)}|upload')}",
                   content=blob, headers={"X-Goog-Upload-Command": "upload, finalize"})
    assert r.status_code == 200
    assert route.calls.last.request.read() == blob, "the body must arrive unchanged"


# ------------------------------------------------------------------ policy, quota
def test_quota_429_sets_a_cooldown(proxy, respx_mock):
    proxy.ledger.fs_bind("fileSearchStores/demo", kid(K1))
    respx_mock.post(f"{API}/models/gemini-3.8-flash:generateContent").mock(
        return_value=httpx.Response(429, json={"error": {
            "message": "You exceeded your current quota",
            "details": [{"reason": "RATE_LIMIT_EXCEEDED",
                         "metadata": {"quota_id": "GenerateRequestsPerDayPerProject"
                                                  "PerModel-FreeTier"}}]}}))
    with client(proxy) as c:
        r = c.post("/v1beta/models/gemini-3.8-flash:generateContent", json={
            "contents": [{"parts": [{"text": "q"}]}],
            "tools": [{"fileSearch": {"fileSearchStoreNames":
                                      ["fileSearchStores/demo"]}}]})
    assert r.status_code == 429
    assert proxy.ledger.cooldown(kid(K1), "gemini-3.8-flash") is not None


def test_strict_is_the_default_policy(proxy, respx_mock):
    """The client's model id is a hardcoded default these keys cannot serve, so the
    chain is what answers — otherwise every File Search query fails."""
    assert Config.model_validate({"api_keys": [K1]}).proxy.model_policy == "strict"
    route = respx_mock.post(f"{API}/models/gemini-3.8-flash:generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": [
            {"content": {"parts": [{"text": "A"}]}, "finishReason": "STOP"}]}))
    with client(proxy) as c:
        c.post("/v1beta/models/gemini-2.5-flash:generateContent",
               json={"contents": [{"parts": [{"text": "q"}]}]})
    assert route.call_count == 1


def test_strict_policy_rewrites_the_model(proxy, respx_mock):
    proxy.cfg.proxy.model_policy = "strict"
    route = respx_mock.post(f"{API}/models/gemini-3.8-flash:generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": [
            {"content": {"parts": [{"text": "A"}]}, "finishReason": "STOP"}]}))
    with client(proxy) as c:
        c.post("/v1beta/models/gemini-2.5-flash:generateContent",
               json={"contents": [{"parts": [{"text": "q"}]}]})
    assert route.call_count == 1


def test_auto_policy_keeps_a_model_the_key_can_serve(proxy, respx_mock):
    """`auto` is the opt-out: keep a model the key can actually serve."""
    proxy.cfg.proxy.model_policy = "auto"
    respx_mock.get(f"{UPSTREAM}/v1beta/models").mock(return_value=httpx.Response(
        200, json={"models": [{"name": "models/gemini-2.5-flash"}]}))
    route = respx_mock.post(f"{API}/models/gemini-2.5-flash:generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": [
            {"content": {"parts": [{"text": "A"}]}, "finishReason": "STOP"}]}))
    with client(proxy) as c:
        c.post("/v1beta/models/gemini-2.5-flash:generateContent",
               json={"contents": [{"parts": [{"text": "q"}]}]})
    assert route.call_count == 1


# ------------------------------------------------------------------------ ledger
def test_generate_content_spends_and_management_calls_do_not(proxy, respx_mock):
    respx_mock.get(f"{API}/files").mock(return_value=httpx.Response(200, json={"files": []}))
    respx_mock.post(f"{API}/models/gemini-3.8-flash:generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": [
            {"content": {"parts": [{"text": "A"}]}, "finishReason": "STOP"}]}))
    with client(proxy) as c:
        c.get("/v1beta/files")
        c.post("/v1beta/models/gemini-3.8-flash:generateContent",
               json={"contents": [{"parts": [{"text": "q"}]}]})
    tail = proxy.ledger.calls_tail(20)
    kinds = {row["kind"] for row in tail}
    assert "filesearch" in kinds and "single" in kinds
    spent = proxy.ledger.rpd_spent(kid(K1), "gemini-3.8-flash", 0, -86400, -1e9)
    assert spent[0] <= 1, "a management call must not draw on the model's day budget"


def test_missing_token_refuses_to_start(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(ValueError, match="token"):
        GeminiProxy(cfg, ledger=Ledger(cfg.state_path), token="")


def test_no_keys_refuses_to_start(tmp_path):
    cfg = make_cfg(tmp_path, api_keys=[])
    with pytest.raises(ValueError, match="no API keys"):
        GeminiProxy(cfg, ledger=Ledger(cfg.state_path), token=TOKEN)


def test_audit_line_carries_no_secret(proxy, respx_mock, caplog):
    import logging

    respx_mock.get(f"{API}/files").mock(return_value=httpx.Response(200, json={"files": []}))
    with caplog.at_level(logging.INFO, logger="gemini_router.proxy"):
        with client(proxy) as c:
            c.get("/v1beta/files")
    text = caplog.text
    assert "key=" in text
    assert K1 not in text and K2 not in text and TOKEN not in text


def test_affinity_survives_a_new_process(tmp_path):
    """The mapping lives in the shared ledger, so a restart cannot orphan every store."""
    cfg = make_cfg(tmp_path)
    Ledger(cfg.state_path).fs_bind("fileSearchStores/demo", kid(K2))
    again = Ledger(cfg.state_path)
    assert again.fs_owner("fileSearchStores/demo") == kid(K2)
    assert again.fs_affinity() == {"fileSearchStores/demo": kid(K2)}
