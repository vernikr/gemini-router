"""Shared fixtures. All HTTP mocked via respx (b01: no real keys/network in tests)."""
from __future__ import annotations

import httpx
import pytest

from gemini_router import Config, keyauth

BASE = "https://generativelanguage.googleapis.com/v1beta"
MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
          "gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-2.5-flash",
          "gemma-3-27b-it", "gemma-4-12b-it", "gemini-3.8-flash-tts"]


def make_config(tmp_path, **over) -> Config:
    data = dict(
        chain=["gemini-3.8-flash", "gemini-3.7-flash"],
        api_keys=["k1"],
        state_path=str(tmp_path / "state.db"),
        limits_per_key_model={"rpd": 20, "rpm": 5, "tpm": 250_000},
        retry={"max": 2, "backoff_base_s": 0.01, "jitter_max_s": 0.01,
               "provider_timeout_s": 5, "request_delay_s": 0},
        wait={"enabled_default": True, "max_s": 2.5, "poll_s": 0.05},
        window_s=1.0, planned_ttl_s=300, models_refresh_h=0, cache_ttl_h=24,
    )
    data.update(over)
    return Config.model_validate(data)


def gen_resp(text: str = "ok", tin: int = 10, tout: int = 5) -> httpx.Response:
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": tin, "candidatesTokenCount": tout,
                          "totalTokenCount": tin + tout}})


def r429_day() -> httpx.Response:
    return httpx.Response(429, json={"error": {
        "message": "You exceeded your current quota list",
        "details": [{"reason": "RATE_LIMIT_EXCEEDED",
                     "metadata": {"quota_metric": "generativelanguage.googleapis.com/"
                                                  "generate_content_free_tier_requests",
                                  "quota_id": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                  "quota_value": "20"}}]}})


def r429_minute(retry_after: str = "0.2") -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": retry_after}, json={"error": {
        "message": "quota exceeded",
        "details": [{"reason": "RATE_LIMIT_EXCEEDED",
                     "metadata": {"quota_id":
                                  "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}}]}})


def route(respx_mock, model: str, resp):
    r = respx_mock.post(f"{BASE}/models/{model}:generateContent")
    r.mock(side_effect=resp if isinstance(resp, list) else lambda request: resp)
    return r


@pytest.fixture(autouse=True)
def _fresh_transport_memory():
    """keyauth remembers the transport that worked per key fingerprint — process-wide
    state by design (the daemon benefits), so tests must not inherit it."""
    keyauth.reset()
    yield
    keyauth.reset()


@pytest.fixture
def mock_api(respx_mock):
    respx_mock.get(f"{BASE}/models").mock(return_value=httpx.Response(
        200, json={"models": [{"name": f"models/{m}"} for m in MODELS]}))
    return respx_mock
