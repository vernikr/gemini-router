"""Weekly Files API capability canary — bounded, standalone, no production ledger.

Unlike countTokens, generateContent actually proves whether a model can read a file.
One independent credential, two small generated fixtures, exactly one generation
per case; no router fallback/retry and no key/response text in logs. This is not a
substitute for the gateway's operational ledger: prefer a separate canary account.
A shared account would lose at most four daily requests on the weekly run.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.tariff_check import one_page_pdf, png, send_mail

from gemini_router.config import Config
from gemini_router.errors import GeminiHTTPError, RouterError
from gemini_router.gemini import GeminiClient, UploadedFile, file_parts

# These are explicit CANARY model IDs, not library defaults. A renamed model is a red
# check needing review, rather than a silent skip (provenance: product worklog §E).
FLASH = os.environ.get("CAP_FLASH_MODEL", "gemini-3.6-flash")
LITE = os.environ.get("CAP_LITE_MODEL", "gemini-3.5-flash-lite")
GEMMA = os.environ.get("CAP_GEMMA_MODEL", "gemma-4-31b-it")
PDF_MARKER = "ZEBRA-417"  # appears in the fixture, NEVER in the model prompt
IMAGE_COLOR = "green"    # fixture color, NEVER in the model prompt


@dataclass(frozen=True)
class Case:
    label: str
    model: str
    media: str
    expected: str                    # "ok" or "blocked"
    proof: str                       # must occur in answer for a successful read


# Positive controls FIRST, then the documented-vs-measured disagreements. Up to four
# generateContent calls / week on the dedicated canary key (no repeats on 5xx/429).
def cases() -> list[Case]:
    return [Case("flash/pdf", FLASH, "pdf", "ok", PDF_MARKER),
            Case("lite/png", LITE, "png", "ok", IMAGE_COLOR),
            Case("lite/pdf", LITE, "pdf", "blocked", PDF_MARKER),
            Case("gemma/png", GEMMA, "png", "blocked", IMAGE_COLOR)]


def classify(expected: str, *, status: int | None, answer: str = "",
             proof: str = "") -> str:
    """Separate a capability change from an outage or exhausted probe credential.

    Only 400/500 are established capability refusals; any 401/403/404/429/503 or
    network failure means 'inconclusive', not evidence of changed media support.
    """
    if status is None and answer.strip():
        # An HTTP 200 with a generic reply does NOT prove the file was read.
        if not proof:
            return "inconclusive"
        import re
        plain = re.sub(r"[^a-z0-9]", "", answer.casefold())
        expected_text = re.sub(r"[^a-z0-9]", "", proof.casefold())
        if expected_text and expected_text not in plain:
            return "inconclusive"
        return "match" if expected == "ok" else "drift"
    if status == 400:
        return "match" if expected == "blocked" else "drift"
    if status == 500:
        return "match" if expected == "blocked" else "inconclusive"
    return "inconclusive"


async def probe_case(client: GeminiClient, key: str, case: Case,
                     files: dict[str, UploadedFile]) -> tuple[str, str]:
    up = files[case.media]
    # countTokens is intentionally NOT the verdict: it accepted a PDF on lite even
    # though generateContent rejected that very file. One real generation per case.
    prompt = ("Read the code PRINTED on the PDF page. Return only the code."
              if case.media == "pdf" else
              "What is the dominant color filling this image? Reply with one word.")
    try:
        result = await client.generate(key, case.model,
                                       prompt=prompt,
                                       thinking_level="low", max_output_tokens=1024,
                                       file_parts=file_parts([up]))
    except GeminiHTTPError as e:
        return classify(case.expected, status=e.status), f"HTTP {e.status}"
    except RouterError:
        return "inconclusive", "provider error (not a capability verdict)"
    verdict = classify(case.expected, status=None, answer=result.text,
                       proof=case.proof)
    return verdict, ("content verified" if verdict != "inconclusive" else
                     "answer did not verify file content")


async def check(client: GeminiClient, key: str, fixture_dir: Path,
                matrix: list[Case]) -> list[tuple[Case, str, str]]:
    files: dict[str, UploadedFile] = {}
    fixture_dir.mkdir(parents=True, exist_ok=True)
    fixtures = {"png": ("image/png", png(64, 64, rgb=(0, 160, 0))),
                "pdf": ("application/pdf", one_page_pdf(PDF_MARKER))}
    try:
        for media in dict.fromkeys(c.media for c in matrix):
            mime, blob = fixtures[media]
            path = fixture_dir / f"canary.{media}"
            path.write_bytes(blob)
            up = await client.upload_file(key, path, mime=mime)
            files[media] = up
            files[media] = await client.wait_file_active(key, up.name)
        results = []
        for case in matrix:
            verdict, detail = await probe_case(client, key, case, files)
            results.append((case, verdict, detail))
            if case.expected == "ok" and verdict != "match":
                break  # do not spend more quota when a positive control is broken
        return results
    finally:
        for up in files.values():
            try:
                await client.delete_file(key, up.name)
            except RouterError:
                pass  # deletion is best-effort; provider TTL cleans up on its own


def summarize(results: list[tuple[Case, str, str]]) -> tuple[int, str]:
    lines = [f"{case.label} ({case.model}): expected={case.expected}, "
             f"observed={detail}, verdict={verdict}"
             for case, verdict, detail in results]
    body = "\n".join(lines)
    if any(verdict == "inconclusive" for _, verdict, _ in results):
        return 2, body  # do not call provider instability a capability drift
    if any(verdict == "drift" for _, verdict, _ in results):
        return 1, body
    return 0, body


def alert_message(results: list[tuple[Case, str, str]], code: int, details: str,
                  *, event: str = "", run_id: str = "") -> tuple[str, str]:
    """Owner-facing mail: state the product, verdict, impact and concrete next step."""
    prefix = "[Gemini Router | файлы]"
    changed = [case.label for case, verdict, _ in results if verdict == "drift"]
    quota = any("HTTP 429" in detail for _, _, detail in results)
    names = {
        "flash/pdf": "Flash не прочитала PDF",
        "lite/png": "Flash-Lite не прочитала PNG",
        "lite/pdf": "Flash-Lite прочитала PDF",
        "gemma/png": "Gemma прочитала PNG",
    }
    if code == 1:
        what = names.get(changed[0], "изменилась поддержка файлов") if len(changed) == 1 else (
            "изменилась поддержка файлов у нескольких моделей")
        subject = f"{prefix} НУЖНО РЕШЕНИЕ: {what}"
        outcome = "Подтверждено чтение содержимого файла там, где раньше был отказ."
        action = ("Сравните результат с независимыми ключами и решите, разрешать ли "
                  "изменившейся модели обработку этих файлов. Если да — сначала "
                  "включите ограниченный резервный маршрут с предупреждением ⚠️. "
                  "Автоматически маршрутизация не изменена.")
    elif quota and event == "schedule":
        subject = f"{prefix} НУЖНО ДЕЙСТВИЕ: лимит ключа остановил проверку"
        outcome = "Проверка НЕ установила, изменилась ли поддержка файлов: HTTP 429."
        action = ("Выделите отдельный ключ для мониторинга в секрете GitHub Actions "
                  "GEMINI_API_KEY либо согласуйте его замену. Пока ключ недоступен, "
                  "еженедельная проверка не даёт ответа. Не запускайте её повторно "
                  "сразу и не переносите ключ с рабочего VPS без отдельного решения.")
    elif quota:
        subject = f"{prefix} ПОКА ЖДАТЬ: проверку остановил лимит ключа"
        outcome = "Проверка НЕ установила, изменилась ли поддержка файлов: HTTP 429."
        action = ("Сейчас ничего менять не нужно. Дождитесь планового прогона в "
                  "понедельник после сброса суточной квоты (09:20 UTC). Если и он "
                  "получит 429, понадобится отдельный ключ мониторинга. "
                  "Не повторяйте запросы сразу.")
    else:
        subject = f"{prefix} ПРОВЕРИТЬ: тест чтения файлов не завершён"
        outcome = "Поддержка файлов не установлена: контроль или соединение не прошли."
        action = ("Проверьте журнал запуска и доступность ключа/модели; повторяйте "
                  "тест только после устранения причины. Не меняйте маршрутизацию "
                  "по этому письму.")
    date = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    link = (f"https://github.com/vernikr/gemini-router/actions/runs/{run_id}"
            if run_id.isdigit() else "GitHub Actions → vernikr/gemini-router → capability-check")
    body = (f"Gemini Router — еженедельная проверка чтения PDF и PNG моделями.\n"
            f"Дата: {date}\nРезультат: {outcome}\n\n"
            f"Что делать: {action}\n\n"
            "Важно: это проверка на ключе мониторинга; рабочий gateway не переключался. "
            "Ответ HTTP 200 без чтения скрытого содержимого не считается поддержкой.\n\n"
            f"Подробности:\n{details}\n\nЗапуск: {link}\n")
    return subject, body


async def main() -> int:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        print("::error::Dedicated GEMINI_API_KEY secret is missing; cannot run "
              "the capability canary.")
        return 2
    cfg = Config(api_keys=[key], chain=[FLASH], state_path=":memory:")
    client = GeminiClient(cfg)
    results: list[tuple[Case, str, str]] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            results = await check(client, key, Path(tmp), cases())
        code, body = summarize(results)
    except Exception as e:  # noqa: BLE001 — unexpected provider/transport failure
        # Do not print the exception: HTTP library messages can contain URLs/headers.
        code, body = 2, f"Canary did not finish ({type(e).__name__}); review run logs."
    finally:
        await client.aclose()
    print(body)
    if code:
        subject, owner_body = alert_message(
            results, code, body, event=os.environ.get("GITHUB_EVENT_NAME", ""),
            run_id=os.environ.get("GITHUB_RUN_ID", ""))
        print("::error::" + subject)
        try:
            send_mail(subject, owner_body)
        except Exception as e:  # noqa: BLE001 — CI red is the fallback alert
            print(f"mail failed ({type(e).__name__}); red run remains the alert")
    else:
        print("capability matrix matches the measured baseline")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
