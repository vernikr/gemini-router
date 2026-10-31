from __future__ import annotations

import base64
import io
import json
import shutil
import wave

import httpx
import pytest

from conftest import BASE, make_config
from gemini_router import GeminiClient, Router, RouterError, routing
from gemini_router.tts import WavAssembler, split_text


def wav_data(frames: bytes = b"\x00\x00" * 100) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24_000)
        w.writeframes(frames)
    return out.getvalue()


def test_split_text_prefers_paragraphs_then_sentences():
    text = ("A" * 1800 + "\n\n" + "B" * 1800 + "\n\n" +
            "First sentence. Second sentence. Third sentence.")
    chunks = split_text(text, max_tokens=500)
    assert len(chunks) > 1
    assert "First sentence." in " ".join(chunks)
    assert "Third sentence." in " ".join(chunks)
    assert all(chunks)


def test_wav_assembler_concatenates_frames(tmp_path):
    path = tmp_path / "combined.wav"
    assembler = WavAssembler(path)
    assembler.append(wav_data(b"\x01\x00" * 10))
    assembler.append(wav_data(b"\x02\x00" * 20))
    assembler.close()
    with wave.open(str(path), "rb") as result:
        assert result.getnframes() == 30
        assert result.getframerate() == 24_000


async def test_client_tts_uses_interactions_and_optional_controls(respx_mock, tmp_path):
    cfg = make_config(tmp_path)
    audio = base64.b64encode(wav_data()).decode()
    seen = {}

    def response(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={
            "output_audio": {"data": audio, "mime_type": "audio/wav"},
            "usage": {"input_tokens": 7, "output_tokens": 12},
        })

    respx_mock.post(f"{BASE}/interactions").mock(side_effect=response)
    client = GeminiClient(cfg)
    result = await client.synthesize("k1", "gemini-3.8-flash-tts", "Hello",
                                    voice="Kore", style="warm")
    await client.aclose()
    payload = seen["payload"]
    assert payload["model"] == "gemini-3.8-flash-tts"
    assert payload["response_format"] == {"type": "audio"}
    assert payload["generation_config"]["speech_config"] == [{"voice": "Kore"}]
    assert payload["input"][0]["content"][0]["annotations"] == [
        {"type": "speech_metadata", "style": "warm"}]
    assert result.audio == base64.b64decode(audio)
    assert result.tokens_in == 7 and result.tokens_out == 12


async def test_router_synthesize_uses_tts_quota_and_records_audio(mock_api, tmp_path):
    cfg = make_config(tmp_path, api_keys=["tts-key"],
                       tts_limits={"rpd": 10, "rpm": 3, "tpm": 10_000})
    mock_api.post(f"{BASE}/models/gemini-3.8-flash-tts:countTokens").mock(
        return_value=httpx.Response(200, json={"totalTokens": 4}))
    audio = base64.b64encode(wav_data()).decode()
    mock_api.post(f"{BASE}/interactions").mock(return_value=httpx.Response(200, json={
        "output_audio": {"data": audio, "mime_type": "audio/wav"},
        "usage": {"input_tokens": 4, "output_tokens": 18},
    }))
    router = Router(cfg)
    try:
        result = await router.synthesize("Hello there", voice="Kore")
        assert result.audio == base64.b64decode(audio)
        assert result.key_id
        rows = router.ledger.calls_tail(10)
        tts = next(row for row in rows if row["kind"] == "single")
        assert tts["model"] == "gemini-3.8-flash-tts"
        assert tts["tokens_in"] == 4 and tts["tokens_out"] == 18
        assert tts["response_bytes"] == len(base64.b64decode(audio))
    finally:
        await router.aclose()


async def test_router_file_creates_one_mp3_and_preflights_quota(
        mock_api, tmp_path, monkeypatch):
    cfg = make_config(tmp_path, api_keys=["tts-key"])
    mock_api.post(f"{BASE}/models/gemini-3.8-flash-tts:countTokens").mock(
        return_value=httpx.Response(200, json={"totalTokens": 4}))
    audio = base64.b64encode(wav_data()).decode()
    mock_api.post(f"{BASE}/interactions").mock(return_value=httpx.Response(200, json={
        "output_audio": {"data": audio, "mime_type": "audio/wav"},
        "usage": {"input_tokens": 4, "output_tokens": 18},
    }))

    monkeypatch.setattr(routing.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    async def fake_encode(wav_path, mp3_path, *, bitrate_kbps=128):
        shutil.copyfile(wav_path, mp3_path)

    monkeypatch.setattr(routing, "encode_mp3", fake_encode)
    source = tmp_path / "script.txt"
    source.write_text("Hello there.", encoding="utf-8")
    target = tmp_path / "result.mp3"
    router = Router(cfg)
    events = []
    try:
        result = await router.synthesize_file(source, output_path=target,
                                              on_event=events.append)
        assert result.path == target and result.chunks == 1
        assert result.tokens_in == 4 and target.exists()
        progress = [event.pct for event in events if event.stage == "file_progress"]
        assert progress == [0, 0, 95, 95, 100]
        dispatch = next(event for event in events if event.stage == "dispatch")
        assert dispatch.message().startswith("1/1 · dispatch")
        assert not target.with_name(f".{target.name}.wav.tmp").exists()
    finally:
        await router.aclose()


async def test_router_rejects_oversized_single_segment(mock_api, tmp_path):
    cfg = make_config(tmp_path)
    mock_api.post(f"{BASE}/models/gemini-3.8-flash-tts:countTokens").mock(
        return_value=httpx.Response(200, json={"totalTokens": 8200}))
    router = Router(cfg)
    try:
        with pytest.raises(RouterError, match="Use synthesize_file"):
            await router.synthesize("long input")
    finally:
        await router.aclose()
