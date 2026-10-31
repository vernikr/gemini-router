"""Text-to-speech helpers: conservative paragraph-first chunking and WAV assembly.

The provider emits WAV per request. This module combines compatible WAV streams and
uses the system ffmpeg executable to encode MP3 without blocking the event loop.
"""
from __future__ import annotations

import asyncio
import io
import re
import shutil
import wave
from pathlib import Path

from .config import estimate_tokens
from .errors import RouterError

MODEL = "gemini-3.8-flash-tts"
MAX_INPUT_TOKENS = 8192

# Split at sentence boundaries, retaining punctuation. This covers common Latin and
# Cyrillic punctuation while avoiding a dependency on a language-specific NLP package.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])(?:[\"'»)]*)\s+(?=[\"'«(\w\d])")


def split_text(text: str, *, max_tokens: int = MAX_INPUT_TOKENS) -> list[str]:
    """Pack the largest whole paragraphs that fit; split oversized paragraphs at
    sentence boundaries, then at whitespace only when one sentence itself is too big.

    `estimate_tokens` is script-aware and conservative for preflight. The provider's
    measured input-token count replaces this estimate in the persistent ledger.
    """
    if not text or not text.strip():
        raise ValueError("TTS input file is empty")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    pieces: list[str] = []

    def pack_units(units: list[str], separator: str) -> None:
        current = ""
        for unit in units:
            candidate = f"{current}{separator if current else ''}{unit}" if current else unit
            if estimate_tokens(candidate) <= max_tokens:
                current = candidate
                continue
            if current:
                pieces.append(current)
                current = ""
            if estimate_tokens(unit) <= max_tokens:
                current = unit
                continue
            # A single paragraph does not fit: retry at sentence boundaries.
            sentences = [s.strip() for s in _SENTENCE_BOUNDARY.split(unit) if s.strip()]
            if len(sentences) <= 1:
                sentences = [unit]
            for sentence in sentences:
                if estimate_tokens(sentence) <= max_tokens:
                    candidate = f"{current} {sentence}" if current else sentence
                    if estimate_tokens(candidate) <= max_tokens:
                        current = candidate
                    else:
                        if current:
                            pieces.append(current)
                        current = sentence
                    continue
                # Last-resort safeguard for a single exceptionally long sentence.
                words = sentence.split()
                word_buf = ""
                for word in words:
                    candidate = f"{word_buf} {word}" if word_buf else word
                    if estimate_tokens(candidate) > max_tokens:
                        if word_buf:
                            if current:
                                pieces.append(current)
                                current = ""
                            pieces.append(word_buf)
                        word_buf = word
                    else:
                        word_buf = candidate
                if word_buf:
                    candidate = f"{current} {word_buf}" if current else word_buf
                    if estimate_tokens(candidate) <= max_tokens:
                        current = candidate
                    else:
                        if current:
                            pieces.append(current)
                        current = word_buf
        if current:
            pieces.append(current)

    # Paragraphs that fit are packed together first. Oversized paragraphs are split
    # internally while preserving their sentence order.
    pack_units(paragraphs, "\n\n")
    return pieces


class WavAssembler:
    """Incrementally append provider WAV chunks to a temporary WAV file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer: wave.Wave_write | None = None
        self._params: tuple[int, int, int, int, str, str] | None = None
        self.frames = 0

    def append(self, wav_bytes: bytes) -> None:
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as src:
                params = src.getparams()
                signature = (params.nchannels, params.sampwidth, params.framerate,
                             params.nframes, params.comptype, params.compname)
                if self._writer is None:
                    self._params = signature
                    self._writer = wave.open(str(self.path), "wb")
                    self._writer.setnchannels(params.nchannels)
                    self._writer.setsampwidth(params.sampwidth)
                    self._writer.setframerate(params.framerate)
                    self._writer.setcomptype(params.comptype, params.compname)
                elif signature[:3] != self._params[:3] or signature[4:] != self._params[4:]:
                    raise RouterError("TTS chunks returned incompatible WAV formats")
                while block := src.readframes(16_384):
                    self._writer.writeframesraw(block)
                    self.frames += len(block)
        except (wave.Error, EOFError) as exc:
            raise RouterError(f"TTS provider returned invalid WAV audio: {exc}") from exc

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if not self.frames:
            raise RouterError("TTS provider returned no audio frames")


async def encode_mp3(wav_path: str | Path, mp3_path: str | Path, *,
                     bitrate_kbps: int = 128) -> None:
    """Encode WAV to MP3 in a child process so CPU work does not block asyncio."""
    if not 32 <= bitrate_kbps <= 320:
        raise ValueError("MP3 bitrate must be between 32 and 320 kbps")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RouterError("MP3 encoding requires ffmpeg on PATH")
    target = Path(mp3_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(wav_path), "-vn", "-codec:a", "libmp3lame", "-b:a",
        f"{bitrate_kbps}k", "-ac", "1", str(target),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode:
        target.unlink(missing_ok=True)
        raise RouterError(f"ffmpeg MP3 encoding failed: {stderr.decode(errors='replace')[-500:]}")
