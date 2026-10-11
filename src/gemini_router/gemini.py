"""Async Gemini API client (v1beta REST). Gemini 3.x rules (docs/01 §3.1):
thinkingConfig.thinkingLevel instead of thinking_budget; temperature/top_p stripped for
3.x models. Key goes in the x-goog-api-key header, never in the URL (log hygiene)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from .config import Config
from .errors import (
    KEY_REJECT_STATUS,
    MODEL_SWITCH_STATUS,
    RETRYABLE_STATUS,
    GeminiHTTPError,
    RouterError,
)


@dataclass(slots=True)
class GenResult:
    text: str
    tokens_in: int | None
    tokens_out: int | None
    finish_reason: str | None
    tokens_thought: int | None = None        # 3.x thinking tokens (count toward limits)


def is_gemini3(model: str) -> bool:
    m = model.lower()
    if m.startswith("gemma"):
        return False
    g = re.match(r"gemini-(\d+)", m)
    return bool(g and int(g.group(1)) >= 3)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


class GeminiClient:
    def __init__(self, cfg: Config, *, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, read=cfg.retry.provider_timeout_s))

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _headers(key: str) -> dict:
        return {"x-goog-api-key": key, "content-type": "application/json"}

    # ------------------------------------------------------------------
    async def list_models(self, key: str) -> set[str]:
        models: set[str] = set()
        page_token = None
        for _ in range(5):
            params: dict = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            r = await self._client.get(f"{self.cfg.base_url}/models",
                                       headers=self._headers(key), params=params)
            if r.status_code >= 400:
                raise RouterError(f"listModels HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            models.update(m.get("name", "").removeprefix("models/")
                          for m in data.get("models", []) if m.get("name"))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return models

    async def count_tokens(self, key: str, model: str, prompt: str) -> int:
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        r = await self._client.post(f"{self.cfg.base_url}/models/{model}:countTokens",
                                    headers=self._headers(key), json=payload)
        if r.status_code >= 400:
            raise self._classify(r)
        return int(r.json().get("totalTokens", 0))

    async def generate(self, key: str, model: str, *, prompt: str, system: str | None = None,
                       thinking_level: str | None = "medium", output_format: str = "text",
                       max_output_tokens: int = 8192) -> GenResult:
        gen: dict = {"maxOutputTokens": max_output_tokens}
        if output_format == "json":
            gen["responseMimeType"] = "application/json"
        if is_gemini3(model):
            if thinking_level:
                gen["thinkingConfig"] = {"thinkingLevel": thinking_level}
        elif self.cfg.temperature is not None:
            gen["temperature"] = self.cfg.temperature
        payload: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                         "generationConfig": gen}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        r = await self._client.post(f"{self.cfg.base_url}/models/{model}:generateContent",
                                    headers=self._headers(key), json=payload)
        if r.status_code >= 400:
            raise self._classify(r)
        data = r.json()
        candidates = data.get("candidates") or []
        if not candidates:
            block = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise RouterError(f"response blocked/empty: {block}")
        c = candidates[0]
        parts = (c.get("content") or {}).get("parts", [])
        # 3.x thinking: internal thought parts (thought=true) never join the visible text
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        usage = data.get("usageMetadata") or {}
        return GenResult(text=text,
                         tokens_in=usage.get("promptTokenCount"),
                         tokens_out=usage.get("candidatesTokenCount"),
                         finish_reason=c.get("finishReason"),
                         tokens_thought=usage.get("thoughtsTokenCount"))

    # ------------------------------------------------------------------
    @staticmethod
    def _classify(resp: httpx.Response) -> GeminiHTTPError:
        status = resp.status_code
        body = resp.text[:300]
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        quota_day = quota_minute = False
        if status == 429:
            low = resp.text.lower()
            quota_day = "perday" in low or "per_day" in low
            quota_minute = not quota_day
            if retry_after is None:
                retry_after = 60.0
        key_rejected = status in KEY_REJECT_STATUS
        retryable = status in RETRYABLE_STATUS or (status == 429 and quota_minute)
        switch = (status in MODEL_SWITCH_STATUS or (status == 429 and quota_day)
                  or key_rejected)
        return GeminiHTTPError(f"HTTP {status}: {body}", status=status, retryable=retryable,
                               switch_model=switch, retry_after=retry_after,
                               quota_day=quota_day, quota_minute=quota_minute,
                               key_rejected=key_rejected)


def extract_json(text: str):
    """Tolerant JSON extractor (ported from tldr-digest src/llm.py; when several
    candidates match, the one starting earliest in the text wins)."""
    if text is None:
        raise RouterError("empty model response")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    found: list[tuple[int, object]] = []
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        found.append((start, json.loads(text[start:i + 1])))
                    except json.JSONDecodeError:
                        pass
                    break
    if found:
        return min(found, key=lambda p: p[0])[1]
    raise RouterError(f"could not parse JSON from response: {text[:300]}")
