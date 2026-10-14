# Streaming capability — completed

`Router.complete(..., stream=True)` emits provisional visible deltas and attempt resets;
legacy calls remain non-streaming. SSE auth fallback, completion validation, usage capture,
cache behavior, retries and disconnection accounting are covered by mocked tests.

Released v0.5.2; 135 tests, lint and secret scan pass. GitHub CI and release succeeded.
The yt-subs consumer uses an isolated server installation and the existing shared ledger.
Live product evidence and Mac acceptance live in vernikr/yt-subs:
`docs/worklog/archive/2026.10.14-streaming-summary/plan.md`.
