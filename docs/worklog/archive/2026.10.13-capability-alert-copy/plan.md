# Capability alert copy · 2026-10-13

Owner asked to read three capability emails and make future subjects/body self-explanatory,
with an explicit action when one is needed. The mailbox had:

1. 2026-10-11 first manual canary: provisional nonempty answers, **not** proof of reading.
   It incorrectly claimed both Lite/PDF and Gemma/PNG drift.
2. 2026-10-11 second manual canary: HTTP 429 at the first positive control;
   inconclusive, no capability verdict.
3. 2026-10-12 scheduled run `38295177978`: content-backed Lite/PDF change; both
   controls passed and Gemma/PNG returned HTTP 500. It began at 19:04 UTC despite
   the 09:20 UTC cron. A separate new VPS key agreed on all four cases.

The old mail subjects `capability drift` and `capability canary inconclusive` gave
neither product context nor next action. New mail only (old sent mail cannot be
changed) has a Gemini Router/file-reading prefix, a readable result and an explicit
urgency cue:

- Verified drift: **НУЖНО РЕШЕНИЕ**; name the changed model/file combination and
  tell the owner to compare independent keys, decide on a guarded rollout and
  keep current routing until then.
- Scheduled HTTP 429: **НУЖНО ДЕЙСТВИЕ**; the GitHub monitoring key needs attention
  (dedicated key or approved replacement). Never silently copy a production VPS key.
- Manual HTTP 429: **ПОКА ЖДАТЬ**; wait for the after-reset scheduled check, do not
  immediately repeat a four-request probe.
- Other inconclusive outcomes: **ПРОВЕРИТЬ**; inspect run logs, fix the cause and
  do not treat HTTP 200 without content proof as support.

The body includes timestamp, short result, a `Что делать` section, the unchanged
production-routing boundary, per-case evidence and the GitHub run link. This is
mail copy only: no API key, cron, routing gate, or model-serving behavior changes.
Unit tests cover drift, both 429 paths and generic inconclusive mail; no message
is sent in tests or for a healthy run. The tariff monitor's mail is a separate
workflow and was not modified by this capability-mail request.

Published as router 0.3.6 (`c6a5c5f`): 118 tests, ruff and secrets guard passed;
GitHub release `38432688132` test and PyPI succeeded. No live canary was
redispatched, so no quota-consuming request or test email was sent. The next
scheduled mail (if any) uses the new copy.
