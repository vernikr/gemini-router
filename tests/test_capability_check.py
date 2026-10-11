"""Weekly capability monitor: verdict needs generateContent, not countTokens."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The monitor is a repo workflow script, not a distributable library module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import capability_check as monitor  # noqa: E402

from gemini_router.errors import GeminiHTTPError
from gemini_router.gemini import GenResult, UploadedFile


@pytest.mark.parametrize(("expected", "status", "answer", "verdict"), [
    ("ok", None, "readable", "match"),
    ("blocked", None, "readable", "drift"),
    ("blocked", 400, "", "match"),
    ("blocked", 500, "", "match"),   # historical lite/PDF behavior
    ("ok", 400, "", "drift"),
    ("ok", 500, "", "inconclusive"),  # may be provider instability
    ("ok", 503, "", "inconclusive"),
    ("blocked", 429, "", "inconclusive"),
    ("blocked", 404, "", "inconclusive"),
    ("ok", None, "", "inconclusive"),
])
def test_capability_verdict(expected, status, answer, verdict):
    assert monitor.classify(expected, status=status, answer=answer) == verdict


class DummyClient:
    def __init__(self, statuses):
        self.statuses = statuses
        self.uploads = []
        self.calls = []
        self.deleted = []

    async def upload_file(self, key, path, *, mime):
        self.uploads.append((key, Path(path).name, mime))
        return UploadedFile(name=f"files/{len(self.uploads)}", uri="https://unused.test/file",
                            mime=mime, size=Path(path).stat().st_size, state="ACTIVE")

    async def wait_file_active(self, key, name):
        return UploadedFile(name=name, uri="https://unused.test/file",
                            mime=self.uploads[int(name.rsplit("/", 1)[-1]) - 1][2],
                            size=100, state="ACTIVE")

    async def delete_file(self, key, name):
        self.deleted.append(name)

    async def generate(self, key, model, **kwargs):
        self.calls.append((model, kwargs["file_parts"][0]["file_data"]["mime_type"]))
        status = self.statuses[len(self.calls) - 1]
        if status:
            raise GeminiHTTPError("test response", status=status)
        return GenResult(text="visible", tokens_in=1, tokens_out=1,
                         finish_reason="STOP")


async def test_two_fixtures_four_generations_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "FLASH", "flash")
    monkeypatch.setattr(monitor, "LITE", "lite")
    monkeypatch.setattr(monitor, "GEMMA", "gemma")
    client = DummyClient([None, None, 500, 400])
    result = await monitor.check(client, "DUMMY_KEY", tmp_path, monitor.cases())
    assert [row[1] for row in result] == ["match"] * 4
    assert len(client.uploads) == 2 and len(client.deleted) == 2
    assert client.calls == [("flash", "application/pdf"), ("lite", "image/png"),
                            ("lite", "application/pdf"), ("gemma", "image/png")]
    assert monitor.summarize(result)[0] == 0


async def test_positive_control_failure_stops_before_weak_models(tmp_path):
    client = DummyClient([503])
    result = await monitor.check(client, "DUMMY_KEY", tmp_path, monitor.cases())
    code, body = monitor.summarize(result)
    assert code == 2 and "inconclusive" in body
    assert len(client.calls) == 1  # no quota drain on an outage
    assert len(client.deleted) == 2


def test_changed_capability_is_a_red_drift():
    case = monitor.Case("lite/pdf", "lite", "pdf", "blocked")
    code, body = monitor.summarize([(case, "drift", "answer present")])
    assert code == 1 and "lite/pdf" in body
