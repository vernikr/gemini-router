"""Scheduled tariff-drift check (wave 1, layer 3).

Uploads three generated fixtures and calls countTokens on each — both free, no RPD —
then diffs the live prices against MEDIA_TARIFFS. A miss fails the run (GitHub notifies
on red runs) and, when the Gmail secrets exist, mails the numbers (the same path
tldr-digest uses). The model id is this script's own canary pin, deliberately NOT the
library's chain (volatile model ids are why this check exists).
"""
from __future__ import annotations

import asyncio
import os
import smtplib
import struct
import sys
import tempfile
import zlib
from email.message import EmailMessage
from pathlib import Path

from gemini_router import DEFAULT_LEVEL, MEDIA_TARIFFS
from gemini_router.config import Config
from gemini_router.gemini import GeminiClient

MODEL = os.environ.get("DRIFT_MODEL", "gemini-3.6-flash")


# ------------------------------------------------------------- fixtures (generated)
def png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def one_page_pdf(text: str) -> bytes:
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    return out + (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
                  f"startxref\n{xref}\n%%EOF\n").encode()


FIXTURES = [   # (name, bytes, mime) — expected prices come from the table, at default level
    ("tiny.png", png(8, 8), "image/png"),
    ("edge.png", png(384, 384), "image/png"),        # the 2.x tile boundary: flatness canary
    ("page.pdf", one_page_pdf("tariff drift check"), "application/pdf"),
]


def send_mail(subject: str, body: str) -> None:
    addr = os.environ.get("GMAIL_ADDRESS")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO")
    if not (addr and pwd and to):
        print("mail skipped: GMAIL_ADDRESS / GMAIL_APP_PASSWORD / MAIL_TO not all set")
        return
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = addr, to, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(addr, pwd)
        s.send_message(msg)
    print(f"mail sent to {to}")


async def main() -> int:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        print("::warning::GEMINI_API_KEY secret not set — tariff check skipped. "
              "Add one API key (a spare free-tier account is fine) to the repo secrets.")
        return 0
    cfg = Config(api_keys=[key], chain=[MODEL], state_path=":memory:")
    client = GeminiClient(cfg)
    misses, lines = [], []
    try:
        with tempfile.TemporaryDirectory() as td:
            for name, blob, mime in FIXTURES:
                p = Path(td) / name
                p.write_bytes(blob)
                up = await client.upload_file(key, p, mime=mime)
                up = await client.wait_file_active(key, up.name)
                measured = await client.count_file_tokens(key, MODEL, up.uri, mime)
                await client.delete_file(key, up.name)
                cls = "pdf" if mime == "application/pdf" else "image"
                level = DEFAULT_LEVEL[cls]
                predicted = MEDIA_TARIFFS[cls][level]
                mark = "ok" if measured == predicted else "DRIFT"
                lines.append(f"{name}: {cls}/{level} predicted {predicted}, "
                             f"measured {measured} [{mark}]")
                if measured != predicted:
                    misses.append(lines[-1])
    finally:
        await client.aclose()
    print("\n".join(lines))
    if misses:
        subject = f"tariff drift: gemini-router table stale ({len(misses)} miss(es))"
        body = ("The scheduled tariff check measured prices that disagree with "
                f"MEDIA_TARIFFS on {MODEL}. Re-measure and update "
                "src/gemini_router/tariffs.py.\n\n" + "\n".join(lines))
        print("::error::" + subject)
        try:
            send_mail(subject, body)
        except Exception as e:  # noqa: BLE001 — the red run is the fallback signal
            print(f"mail failed ({e}); the red run is the fallback signal")
        return 1
    print("tariffs match the table")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
