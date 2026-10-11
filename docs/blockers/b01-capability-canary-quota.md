# b01 · Canary verification inconclusive: CI key answered HTTP 429

**Status:** open 2026-10-11 · Owner: agent (re-check), user only if the monitoring
credential needs replacement/dedication.

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

**Resolution:** let the CI key's provider quota reset. The weekly Monday run is now
scheduled after Pacific midnight in either season; no immediate manual re-run is
needed. It must either (1) match the established matrix (archive this blocker),
or (2) show verified content-backed drift with enough evidence to revise the matrix.
If 429 recurs after reset on a quiet day, use a dedicated monitoring API key for
repo secret `GEMINI_API_KEY`;
never switch to a production key merely to make the check green. No repeated
immediate dispatches: the probe itself spends up to four RPD when healthy.
