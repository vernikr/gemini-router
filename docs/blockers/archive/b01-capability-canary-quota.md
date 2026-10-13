# b01 · Canary verification inconclusive: CI key answered HTTP 429

**Status:** resolved 2026-10-12 · The CI monitoring key worked after the reset;
no key replacement is needed. This archived blocker records the earlier 429.

The new weekly Files API capability canary shipped in router 0.3.1; its first manual
run returned nonempty answers on the formerly blocked Lite/PDF and Gemma/PNG paths.
That provisional run could not prove the models *read the file*, yet it sent a drift
email. Patch 0.3.2 changed the verdict to require a hidden PDF code or image color
absent from the prompt (generic answers are inconclusive). Its immediate re-run
stopped at the very first positive control with **HTTP 429**, spending no further
generation calls. It reported `inconclusive` and emailed that fact; no capability
change was confirmed by the CI run. A separate, owner-authorized one-off probe on
the new VPS key later verified Lite/PDF reading (hidden code reproduced) but NOT
Gemma/PNG support (HTTP 500); see
`docs/worklog/archive/2026.10.12-third-key-capability-probe/`. Do not change the router's
fleet-wide conservative file gates until another key independently confirms it.

**Resolution:** scheduled GitHub Actions run `38295177978` on 2026-10-12
(19:04 UTC, late relative to the 09:20 UTC cron) completed the content-verifying
matrix on the original CI key: Flash/PDF and Lite/PNG positive controls passed;
Lite/PDF reproduced the hidden code (verified drift); Gemma/PNG returned HTTP 500
(baseline match). This independently corroborates the new VPS key's Lite/PDF result.
The run is red for **verified capability change**, not for quota exhaustion; archive
this quota blocker. Serving/routing policy is a separate owner decision, not
an automatic side effect of a monitoring email.

If future scheduled runs hit HTTP 429 after reset, consider a dedicated monitoring
API key for the repo secret `GEMINI_API_KEY`; never switch to a production key merely
to make the check green. Do not repeat immediate dispatches: a healthy probe spends
up to four RPD.
