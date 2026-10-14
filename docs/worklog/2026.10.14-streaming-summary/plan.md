# Streaming summary

Add opt-in SSE generation to `Router.complete`, retaining routing, cache, accounting, auth transports and non-streaming compatibility. Chunks are provisional; reset on each provider attempt. Test mocked streaming, retries, cache, truncation and cancellation. Product integration: vernikr/yt-subs.

Implemented and locally validated: 133 mocked tests pass, including streaming/auth/cache/retry/cancellation; ruff and secret scan pass. Release v0.5.0 supplies the isolated yt-subs worker.
