"""Async Gemini API client (v1beta REST). Gemini 3.x rules (docs/01 §3.1):
thinkingConfig.thinkingLevel instead of thinking_budget; temperature/top_p stripped for
3.x models. Key goes in the x-goog-api-key header, never in the URL (log hygiene).

File input (spec: docs/SPEC.md §8): resumable upload to `upload_base_url`, then a
`file_data` part in generateContent. The blob is streamed in chunks and never held twice
in memory — the gateway runs on a 1 GB box."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config
from .errors import (
    KEY_REJECT_STATUS,
    MODEL_SWITCH_STATUS,
    RETRYABLE_STATUS,
    FileNotReady,
    FileUploadFailed,
    GeminiHTTPError,
    RouterError,
)

CHUNK = 1 << 20          # upload read size; small enough for a 1 GB box, big enough for LAN


@dataclass(slots=True)
class UploadedFile:
    """A provider-side file. `sha256` is the local blob's hash — it is what the cache
    keys on, so a hit survives the provider forgetting the file; `key_id`, not the key
    itself, is what callers pin a request to."""

    name: str                     # "files/<id>" as the provider names it
    uri: str                      # what a request references
    mime: str
    size: int
    state: str                    # ACTIVE when usable
    expires_at: str | None = None
    sha256: str | None = None
    key_id: str | None = None

    @classmethod
    def from_api(cls, info: dict, *, mime: str, fallback_size: int = 0) -> UploadedFile:
        return cls(name=info.get("name", ""), uri=info.get("uri", ""),
                   mime=info.get("mimeType") or mime,
                   size=int(info.get("sizeBytes") or fallback_size),
                   state=info.get("state", ""), expires_at=info.get("expirationTime"))


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

    # ------------------------------------------------------------- files (SPEC §8)
    async def upload_file(self, key: str, path: str | Path, *, mime: str | None = None,
                          display_name: str | None = None,
                          timeout_s: float | None = None) -> UploadedFile:
        """Resumable upload: start (metadata) → one PUT carrying the bytes → finalize.
        Streams the blob in chunks with an explicit Content-Length — no base64, no second
        copy in RAM. Returns the provider's record; the caller waits for ACTIVE."""
        path = Path(path)
        size = path.stat().st_size
        mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start = await self._client.post(
            f"{self.cfg.upload_base_url}/files", headers=self._headers(key) | {
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": mime},
            json={"file": {"display_name": display_name or path.name}}, timeout=timeout_s)
        if start.status_code >= 400:
            raise self._classify(start)
        url = start.headers.get("x-goog-upload-url")
        if not url:
            raise FileUploadFailed("upload start returned no x-goog-upload-url")
        done = await self._client.put(
            url, headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                          "X-Goog-Upload-Command": "upload, finalize",
                          "content-type": "application/octet-stream"},
            content=_chunks(path), timeout=timeout_s)
        if done.status_code >= 400:
            raise self._classify(done)
        info = (done.json() or {}).get("file") or {}
        if not info.get("uri"):
            raise FileUploadFailed(f"upload finalize returned no file record: {done.text[:200]}")
        return UploadedFile.from_api(info, mime=mime, fallback_size=size)

    async def file_state(self, key: str, name: str) -> UploadedFile:
        r = await self._client.get(f"{self.cfg.base_url}/files/{_file_id(name)}",
                                   headers=self._headers(key))
        if r.status_code >= 400:
            raise self._classify(r)
        info = (r.json() or {}) or {}
        return UploadedFile.from_api(info, mime=info.get("mimeType", ""))

    async def wait_file_active(self, key: str, name: str, *, poll_s: float = 1.0,
                               timeout_s: float = 30.0) -> UploadedFile:
        """The provider processes asynchronously; small files are usually ACTIVE at once,
        so the first check usually wins and the loop costs nothing."""
        deadline = time.monotonic() + timeout_s
        while True:
            up = await self.file_state(key, name)
            if up.state == "ACTIVE":
                return up
            if up.state == "FAILED":
                raise FileUploadFailed(f"{name}: provider marked the upload FAILED")
            if time.monotonic() >= deadline:
                raise FileNotReady(f"{name}: still {up.state or 'unknown'} after "
                                   f"{timeout_s:.0f}s")
            await asyncio.sleep(poll_s)

    async def count_file_tokens(self, key: str, model: str, uri: str, mime: str) -> int:
        """Exact token cost of a file, before anything is sent. A countTokens call: it
        plans TPM and never draws on RPD (FR-C7)."""
        r = await self._client.post(
            f"{self.cfg.base_url}/models/{model}:countTokens", headers=self._headers(key),
            json={"contents": [{"parts": [{"file_data": {"mime_type": mime,
                                                         "file_uri": uri}}]}]})
        if r.status_code >= 400:
            raise self._classify(r)
        return int((r.json() or {}).get("totalTokens") or 0)

    async def delete_file(self, key: str, name: str) -> None:
        """Best-effort tidy-up when the caller drops its blob. A missing file is a
        success: the provider expires them on its own."""
        r = await self._client.delete(f"{self.cfg.base_url}/files/{_file_id(name)}",
                                      headers=self._headers(key))
        if r.status_code >= 400 and r.status_code != 404:
            raise self._classify(r)

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
                       max_output_tokens: int = 8192,
                       file_parts: list[dict] | None = None) -> GenResult:
        gen: dict = {"maxOutputTokens": max_output_tokens}
        if output_format == "json":
            gen["responseMimeType"] = "application/json"
        if is_gemini3(model):
            if thinking_level:
                gen["thinkingConfig"] = {"thinkingLevel": thinking_level}
        elif self.cfg.temperature is not None:
            gen["temperature"] = self.cfg.temperature
        parts = list(file_parts or []) + [{"text": prompt}]   # files first (provider guide)
        payload: dict = {"contents": [{"role": "user", "parts": parts}],
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


def file_parts(files) -> list[dict]:
    """`file_data` parts for a request, in the order given."""
    return [{"file_data": {"mime_type": f.mime, "file_uri": f.uri}} for f in files]


def _file_id(name: str) -> str:
    """The API's resource path is `files/<id>`; accept either form from callers."""
    return name.rsplit("/", 1)[-1]


async def _chunks(path: Path, size: int = CHUNK):
    """Chunked reader: httpx accepts an async iterable with an explicit Content-Length,
    which keeps the body streamed (verified against respx: header preserved, no chunked
    transfer-encoding, body reassembles byte-for-byte)."""
    with path.open("rb") as fh:
        while (block := fh.read(size)):
            yield block


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
