"""GeminiClient: 3.x payload rules, error classification, extract_json (FR-C5, docs/03)."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from conftest import BASE, gen_resp, make_config
from gemini_router import GeminiClient, RouterError, extract_json, is_gemini3


async def _capture(respx_mock: respx.MockRouter, cfg, model: str, **kw) -> dict:
    seen = {}

    def cap(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return gen_resp("x")

    respx_mock.post(f"{BASE}/models/{model}:generateContent").mock(side_effect=cap)
    client = GeminiClient(cfg)
    await client.generate("k1", model, **{"prompt": "p", **kw})
    await client.aclose()
    return seen


async def test_gemini3_payload_rules(mock_api, tmp_path):
    cfg = make_config(tmp_path, temperature=0.4)
    seen = await _capture(mock_api, cfg, "gemini-3.8-flash",
                          thinking_level="high", output_format="json",
                          max_output_tokens=1234, system="sys")
    gen = seen["payload"]["generationConfig"]
    assert gen["thinkingConfig"] == {"thinkingLevel": "high"}      # 3.x: level, not budget
    assert "temperature" not in gen                                # 3.x: stripped
    assert gen["responseMimeType"] == "application/json"
    assert gen["maxOutputTokens"] == 1234
    assert seen["payload"]["systemInstruction"]["parts"][0]["text"] == "sys"
    assert seen["headers"]["x-goog-api-key"] == "k1"               # key in header, not URL


async def test_pre3x_payload_keeps_temperature(mock_api, tmp_path):
    cfg = make_config(tmp_path, temperature=0.4)
    seen = await _capture(mock_api, cfg, "gemini-2.5-flash", thinking_level="high")
    gen = seen["payload"]["generationConfig"]
    assert gen["temperature"] == 0.4
    assert "thinkingConfig" not in gen


def test_is_gemini3():
    assert is_gemini3("gemini-3.8-flash") and is_gemini3("gemini-3-flash-preview")
    assert not is_gemini3("gemini-2.5-flash") and not is_gemini3("gemma-3-27b-it")


def test_classify_429_day_vs_minute():
    day = GeminiClient._classify(httpx.Response(429, json={"error": {"message": ""}},
                                                content=b"GenerateRequestsPerDayPerProject x"))
    assert day.quota_day and not day.retryable and day.switch_model
    minute = GeminiClient._classify(httpx.Response(429, content=b"PerMinute rate limit"))
    assert minute.quota_minute and minute.retryable and minute.retry_after == 60.0


def test_classify_other_statuses():
    assert GeminiClient._classify(httpx.Response(404, content=b"x")).switch_model
    assert GeminiClient._classify(httpx.Response(503, content=b"x")).retryable
    e = GeminiClient._classify(httpx.Response(429, headers={"Retry-After": "12"},
                                              content=b"PerMinute"))
    assert e.retry_after == 12.0
    assert not GeminiClient._classify(httpx.Response(418, content=b"x")).retryable


async def test_generate_blocked_response_raises(mock_api, tmp_path):
    cfg = make_config(tmp_path)
    mock_api.post(f"{BASE}/models/gemini-3.8-flash:generateContent").mock(
        return_value=httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    client = GeminiClient(cfg)
    with pytest.raises(RouterError, match="SAFETY"):
        await client.generate("k1", "gemini-3.8-flash", prompt="p")
    await client.aclose()


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('sure! here you go: [{"x": 2}] hope that helps', [{"x": 2}]),
    ('noise {"a": {"b": [1,2]}} tail', {"a": {"b": [1, 2]}}),
    ('{"a": "has } brace"}', {"a": "has } brace"}),
])
def test_extract_json(raw, expected):
    assert extract_json(raw) == expected


def test_extract_json_garbage():
    with pytest.raises(RouterError):
        extract_json("no json here")
