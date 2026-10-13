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
    assert monitor.classify(expected, status=status, answer=answer,
                            proof="readable") == verdict


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
        mime = kwargs["file_parts"][0]["file_data"]["mime_type"]
        self.calls.append((model, mime))
        status = self.statuses[len(self.calls) - 1]
        if status:
            raise GeminiHTTPError("test response", status=status)
        return GenResult(text=(monitor.PDF_MARKER if model == monitor.FLASH or
                               mime == "application/pdf" else monitor.IMAGE_COLOR),
                         tokens_in=1, tokens_out=1, finish_reason="STOP")


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
    case = monitor.Case("lite/pdf", "lite", "pdf", "blocked", monitor.PDF_MARKER)
    code, body = monitor.summarize([(case, "drift", "answer present")])
    assert code == 1 and "lite/pdf" in body


def test_generic_answer_does_not_prove_media_reading():
    assert monitor.classify("blocked", status=None, answer="I see a PDF",
                            proof=monitor.PDF_MARKER) == "inconclusive"
    assert monitor.classify("ok", status=None, answer="Zebra 417",
                            proof=monitor.PDF_MARKER) == "match"
    assert monitor.classify("blocked", status=None, answer="GREEN",
                            proof=monitor.IMAGE_COLOR) == "drift"


def test_missing_proof_never_counts_generic_http_200_as_success():
    assert monitor.classify("blocked", status=None, answer="sure") == "inconclusive"


def test_owner_mail_names_project_verdict_action_and_run():
    case = monitor.Case("lite/pdf", "gemini-3.5-flash-lite", "pdf", "blocked",
                        monitor.PDF_MARKER)
    subject, body = monitor.alert_message(
        [(case, "drift", "content verified")], 1, "lite/pdf: content verified",
        event="schedule", run_id="12345")
    assert "Gemini Router" in subject and "НУЖНО РЕШЕНИЕ" in subject
    assert "Flash-Lite прочитала PDF" in subject
    assert "Что делать:" in body and "Автоматически маршрутизация не изменена" in body
    assert "https://github.com/vernikr/gemini-router/actions/runs/12345" in body
    assert monitor.PDF_MARKER not in body  # no fixture secret in the owner mail


def test_429_mail_distinguishes_scheduled_action_from_manual_wait():
    case = monitor.cases()[0]
    rows = [(case, "inconclusive", "HTTP 429")]
    scheduled_subject, scheduled_body = monitor.alert_message(
        rows, 2, "flash/pdf: HTTP 429", event="schedule")
    manual_subject, manual_body = monitor.alert_message(
        rows, 2, "flash/pdf: HTTP 429", event="workflow_dispatch")
    assert "НУЖНО ДЕЙСТВИЕ" in scheduled_subject
    assert "GEMINI_API_KEY" in scheduled_body and "рабочего VPS" in scheduled_body
    assert "ПОКА ЖДАТЬ" in manual_subject
    assert "понедельник" in manual_body and "Не повторяйте" in manual_body
    assert "НЕ установила" in scheduled_body and "НЕ установила" in manual_body


def test_non_quota_inconclusive_mail_does_not_claim_support():
    case = monitor.cases()[0]
    subject, body = monitor.alert_message(
        [(case, "inconclusive", "HTTP 503")], 2, "flash/pdf: HTTP 503",
        event="schedule")
    assert "ПРОВЕРИТЬ" in subject
    assert "Поддержка файлов не установлена" in body
    assert "Не меняйте маршрутизацию" in body


def test_weekly_probe_follows_both_pacific_midnight_resets():
    """PT midnight is 07:00 UTC in summer and 08:00 UTC in winter."""
    import re

    workflow = (Path(__file__).resolve().parents[1] /
                ".github/workflows/capability-check.yml").read_text()
    match = re.search(r'cron:\s*"(\d+) (\d+) \* \* 1"', workflow)
    assert match, "weekly Monday cron is required"
    minute, hour = map(int, match.groups())
    assert 0 <= minute < 60 and 9 <= hour <= 23, "run AFTER the PT day reset"
