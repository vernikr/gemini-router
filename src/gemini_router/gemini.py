"""Async Gemini API client (v1beta REST). Gemini 3.x rules (docs/01 §3.1):
thinkingConfig.thinkingLevel instead of thinking_budget; temperature/top_p stripped for
3.x models. Keys travel over the transport that suits them (`keyauth`): the standard
x-goog-api-key header by default — never in the URL, for log hygiene — with the query and
bearer forms as fallbacks for key generations whose surface demands them.

File input (spec: docs/SPEC.md §8): resumable upload to `upload_base_url`, then a
`file_data` part in generateContent. The blob is streamed in chunks and never held twice
in memory — the gateway runs on a 1 GB box."""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from . import keyauth
from .config import Config
from .errors import (
    KEY_REJECT_STATUS,
    MODEL_SWITCH_STATUS,
    RETRYABLE_STATUS,
    FileNotReady,
    FileUploadFailed,
    GeminiHTTPError,
    KeyLeakedError,
    RouterError,
)
from .feedback import parse_provider_signal
from .feedback import parse_retry_after as _parse_retry_seconds

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
    media_resolution: str | None = None   # "low" | "medium" | "high"; None = provider default

    def with_resolution(self, level: str | None) -> UploadedFile:
        """A copy that asks the provider to process this media at `level` (None = inherit
        the provider default). The level is a request-side choice, so it is set here and
        carried into both the measurement and the analysis request — measuring at one
        level and sending at another would make the gate and the ledger disagree."""
        resolution_field(level)          # validate even for None-adjacent typos
        return replace(self, media_resolution=level)

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


@dataclass(slots=True)
class TTSResult:
    audio: bytes
    mime_type: str = "audio/wav"
    tokens_in: int | None = None
    tokens_out: int | None = None
    key_id: str | None = None


def is_gemini3(model: str) -> bool:
    m = model.lower()
    if m.startswith("gemma"):
        return False
    g = re.match(r"gemini-(\d+)", m)
    return bool(g and int(g.group(1)) >= 3)


def _parse_retry_after(value: str | None) -> float | None:
    """Backwards-compatible shim: Retry-After may be seconds *or* an HTTP-date."""
    return _parse_retry_seconds(value)


class GeminiClient:
    def __init__(self, cfg: Config, *, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, read=cfg.retry.provider_timeout_s))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _authed(self, method: str, url: str, *, key: str,
                      headers: dict | None = None, params: dict | None = None,
                      **kw) -> httpx.Response:
        """One request as `key`, over whichever transport that key speaks.

        Starts with the transport that last worked for this key (for the ordinary
        AIza… key that is the standard header, so nothing about the common path changes),
        and rotates only when the provider answers an auth-shaped refusal — then the
        winner is remembered for the process. A leaked-key block ends the rotation
        immediately: no transport resurrects a revoked credential, and the caller's
        classifier turns it into KeyLeakedError."""
        resp: httpx.Response | None = None
        for transport in keyauth.attempts_for(key):
            resp = await self._client.request(
                method, url,
                headers=keyauth.merge_headers(headers, key, transport),
                params=keyauth.merge_params(params, key, transport), **kw)
            if resp.status_code < 400:
                keyauth.remember(key, transport)
                return resp
            if keyauth.leaked(resp.status_code, resp.text):
                return resp                      # a revoked key: the classifier says so
            if not keyauth.auth_failure(resp.status_code, resp.text):
                return resp
        return resp

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
        start = await self._authed(
            "POST", f"{self.cfg.upload_base_url}/files", key=key,
            headers={"X-Goog-Upload-Protocol": "resumable",
                     "X-Goog-Upload-Command": "start",
                     "X-Goog-Upload-Header-Content-Length": str(size),
                     "X-Goog-Upload-Header-Content-Type": mime},
            json={"file": {"display_name": display_name or path.name}}, timeout=timeout_s)
        if start.status_code >= 400:
            raise self._error(start)
        url = start.headers.get("x-goog-upload-url")
        if not url:
            raise FileUploadFailed("upload start returned no x-goog-upload-url")
        done = await self._client.put(
            url, headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                          "X-Goog-Upload-Command": "upload, finalize",
                          "content-type": "application/octet-stream"},
            content=_chunks(path), timeout=timeout_s)
        if done.status_code >= 400:
            raise self._error(done)
        info = (done.json() or {}).get("file") or {}
        if not info.get("uri"):
            raise FileUploadFailed(f"upload finalize returned no file record: {done.text[:200]}")
        return UploadedFile.from_api(info, mime=mime, fallback_size=size)

    async def file_state(self, key: str, name: str) -> UploadedFile:
        r = await self._authed("GET", f"{self.cfg.base_url}/files/{_file_id(name)}", key=key)
        if r.status_code >= 400:
            raise self._error(r)
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

    async def count_file_tokens(self, key: str, model: str, uri: str, mime: str,
                                media_resolution: str | None = None) -> int:
        """Exact token cost of a file, before anything is sent. A countTokens call: it
        plans TPM and never draws on RPD (FR-C7). `media_resolution` must be the level the
        analysis request will use — the count changes with it (measured: 256/529/1089 for
        an image at low/medium/high)."""
        part: dict = {"file_data": {"mime_type": mime, "file_uri": uri}}
        res = resolution_field(media_resolution)
        if res:
            part["media_resolution"] = res
        r = await self._authed("POST", f"{self.cfg.base_url}/models/{model}:countTokens",
                               key=key, json={"contents": [{"parts": [part]}]})
        if r.status_code >= 400:
            raise self._error(r)
        return int((r.json() or {}).get("totalTokens") or 0)

    async def delete_file(self, key: str, name: str) -> None:
        """Best-effort tidy-up when the caller drops its blob. A missing file is a
        success: the provider expires them on its own."""
        r = await self._authed("DELETE", f"{self.cfg.base_url}/files/{_file_id(name)}", key=key)
        if r.status_code >= 400 and r.status_code != 404:
            raise self._error(r)

    # ------------------------------------------------------------------
    async def list_models(self, key: str) -> set[str]:
        models: set[str] = set()
        page_token = None
        for _ in range(5):
            params: dict = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            r = await self._authed("GET", f"{self.cfg.base_url}/models", key=key, params=params)
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
        r = await self._authed("POST", f"{self.cfg.base_url}/models/{model}:countTokens",
                               key=key, json=payload)
        if r.status_code >= 400:
            raise self._error(r)
        return int(r.json().get("totalTokens", 0))

    async def generate(self, key: str, model: str, *, prompt: str, system: str | None = None,
                       thinking_level: str | None = "medium", output_format: str = "text",
                       max_output_tokens: int | None = None,
                       file_parts: list[dict] | None = None,
                       on_text: Callable[[str], None] | None = None) -> GenResult:
        # No cap unless the caller asked for one. The field is OMITTED rather than sent
        # as null when unset: measured against the live free tier, an omitted
        # maxOutputTokens let one call return 29,899 candidate tokens with
        # finishReason=STOP, so the provider treats "absent" as unlimited here, not as
        # the 8,192 the docs describe as the default. A direct caller of this client
        # therefore gets either its own cap or none at all; routing.py always passes a
        # value explicitly.
        gen: dict = {}
        if max_output_tokens is not None:
            gen["maxOutputTokens"] = max_output_tokens
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
        if on_text is not None:
            return await self._generate_stream(key, model, payload, on_text)
        r = await self._authed("POST", f"{self.cfg.base_url}/models/{model}:generateContent",
                               key=key, json=payload)
        if r.status_code >= 400:
            raise self._error(r)
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

    async def synthesize(self, key: str, model: str, text: str, *,
                         voice: str | None = None, style: str | None = None) -> TTSResult:
        """Create one single-speaker WAV through the Gemini Interactions API.

        TTS is a distinct operation from generateContent: the response is binary audio,
        not text. Optional speech controls are omitted entirely when unset.
        """
        content: dict = {"type": "text", "text": text}
        if style:
            content["annotations"] = [{"type": "speech_metadata", "style": style}]
        payload: dict = {
            "model": model,
            "input": [{"type": "user_input", "content": [content]}],
            "response_format": {"type": "audio"},
        }
        if voice:
            payload["generation_config"] = {"speech_config": [{"voice": voice}]}
        r = await self._authed("POST", f"{self.cfg.base_url}/interactions",
                               key=key, json=payload)
        if r.status_code >= 400:
            raise self._error(r)
        data = r.json()
        audio = (data.get("output_audio") or {}).get("data")
        if not audio:
            # Be tolerant of the list-shaped output used by SDK response wrappers.
            for item in data.get("output") or []:
                if item.get("type") == "audio":
                    audio = item.get("data")
                    break
        if not audio:
            raise RouterError("TTS response contained no audio data")
        try:
            raw = base64.b64decode(audio, validate=True)
        except (ValueError, TypeError) as exc:
            raise RouterError("TTS response contained invalid base64 audio") from exc
        usage = data.get("usage") or data.get("usageMetadata") or {}
        tokens_in = (usage.get("input_tokens") or usage.get("promptTokenCount") or
                     usage.get("inputTokens"))
        tokens_out = (usage.get("output_tokens") or usage.get("candidatesTokenCount") or
                      usage.get("outputTokens"))
        return TTSResult(audio=raw,
                         mime_type=(data.get("output_audio") or {}).get("mime_type") or
                                   "audio/wav",
                         tokens_in=int(tokens_in) if tokens_in is not None else None,
                         tokens_out=int(tokens_out) if tokens_out is not None else None)

    async def _generate_stream(self, key: str, model: str, payload: dict,
                               on_text: Callable[[str], None]) -> GenResult:
        url = f"{self.cfg.base_url}/models/{model}:streamGenerateContent"
        for transport in keyauth.attempts_for(key):
            async with self._client.stream(
                "POST", url, json=payload,
                headers=keyauth.merge_headers(None, key, transport),
                params=keyauth.merge_params({"alt": "sse"}, key, transport),
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    if (not keyauth.leaked(response.status_code, response.text)
                            and keyauth.auth_failure(response.status_code, response.text)):
                        failure = self._error(response)
                        continue
                    raise self._error(response)
                keyauth.remember(key, transport)
                chunks: list[str] = []
                usage: dict = {}
                finish = None

                def consume(lines: list[str], usage=usage, chunks=chunks) -> None:
                    nonlocal finish
                    if not lines or "\n".join(lines) == "[DONE]":
                        return
                    try:
                        data = json.loads("\n".join(lines))
                        if not isinstance(data, dict) or "error" in data:
                            raise ValueError("invalid provider event")
                        candidates = data.get("candidates") or []
                        candidate = next((c for c in candidates if c.get("index", 0) == 0), {})
                        text = "".join(p.get("text", "") for p in
                                       (candidate.get("content") or {}).get("parts", [])
                                       if not p.get("thought"))
                        finish = candidate.get("finishReason") or finish
                        usage.update(data.get("usageMetadata") or {})
                    except (ValueError, TypeError, AttributeError) as exc:
                        raise RouterError("invalid streaming response") from exc
                    if text:
                        chunks.append(text)
                        on_text(text)

                event: list[str] = []
                async for line in response.aiter_lines():
                    if not line:
                        consume(event)
                        event = []
                    elif line.startswith("data:"):
                        event.append(line[5:].removeprefix(" "))
                consume(event)
                if not finish:
                    raise httpx.RemoteProtocolError("stream ended without finishReason")
                text = "".join(chunks)
                if finish != "STOP" or not text.strip():
                    raise RouterError(f"stream incomplete/blocked: {finish}")
                return GenResult(text=text, tokens_in=usage.get("promptTokenCount"),
                                 tokens_out=usage.get("candidatesTokenCount"),
                                 finish_reason=finish,
                                 tokens_thought=usage.get("thoughtsTokenCount"))
        raise failure

    # ------------------------------------------------------------------
    def _error(self, resp: httpx.Response) -> GeminiHTTPError:
        """Instance wrapper: attach the parsed provider signal using this client's cfg."""
        return self._classify(resp, cfg=self.cfg, now=time.time())

    @staticmethod
    def _classify(resp: httpx.Response, *, cfg: Config | None = None,
                  now: float | None = None) -> GeminiHTTPError:
        status = resp.status_code
        body = resp.text[:300]
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        signal = parse_provider_signal(status, dict(resp.headers), resp.content,
                                       cfg or Config(), now=now)
        quota_day = quota_minute = False
        if status == 429:
            low = resp.text.lower()
            quota_day = ("perday" in low or "per_day" in low or signal.kind == "day")
            quota_minute = not quota_day
            if retry_after is None:
                retry_after = 60.0
        if keyauth.leaked(status, resp.text):
            return KeyLeakedError(
                f"HTTP {status}: {body} — the provider has this key on its leaked list; "
                f"delete it in the console (or AI Studio) and add a fresh one: "
                f"`gemini-mcp keys add <new key>`", status=status)
        key_rejected = status in KEY_REJECT_STATUS
        retryable = status in RETRYABLE_STATUS or (status == 429 and quota_minute)
        switch = (status in MODEL_SWITCH_STATUS or (status == 429 and quota_day)
                  or key_rejected)
        return GeminiHTTPError(f"HTTP {status}: {body}", status=status, retryable=retryable,
                               switch_model=switch, retry_after=retry_after,
                               quota_day=quota_day, quota_minute=quota_minute,
                               key_rejected=key_rejected,
                               signal=signal if signal.blocks_pair else None)


# What the provider accepts under a media part. Measured 2026-10-11 on gemini-3.6-flash:
# an image costs 256/529/1089 tokens at low/medium/high, a PDF page 280/560/1120, and the
# default differs per media type (images high, PDFs medium) — so pinning a level is what
# makes cost predictable. One word in, the provider's constant out.
RESOLUTION_LEVELS = {"low": "MEDIA_RESOLUTION_LOW",
                     "medium": "MEDIA_RESOLUTION_MEDIUM",
                     "high": "MEDIA_RESOLUTION_HIGH"}


def resolution_field(level: str | None) -> dict | None:
    """The `media_resolution` part field, or None when the level is inherited."""
    if level is None or level == "":
        return None
    try:
        return {"level": RESOLUTION_LEVELS[level]}
    except KeyError:
        raise ValueError(f"media resolution must be one of "
                         f"{sorted(RESOLUTION_LEVELS)} (got {level!r})") from None


def file_parts(files) -> list[dict]:
    """`file_data` parts for a request, in the order given."""
    parts = []
    for f in files:
        part: dict = {"file_data": {"mime_type": f.mime, "file_uri": f.uri}}
        res = resolution_field(getattr(f, "media_resolution", None))
        if res:
            part["media_resolution"] = res
        parts.append(part)
    return parts


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
