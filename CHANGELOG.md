# Changelog

## 0.11.0 — provider truth, bounded walks, request identity

### Fixed
- A confirmed daily 429 now blocks the `(key, model)` pair until the next Pacific-time
  reset. Previously `min(midnight, now + Retry-After + 5)` reduced it to ~65 seconds.
- Upstream `Retry-After` (delta-seconds **and** HTTP-date) is preserved; `retryDelay`
  and quota metadata are parsed from the error body; `quota_id` lands in the audit row.
- The pass-through walk no longer re-selects pairs already tried in the same request.
  When every pair is benched by the provider it answers locally with
  `429 + Retry-After` instead of replaying refusals (2026-10-21: 48/10 s → ≤12 total).
- The proxy now respects minute-scale waits (`proxy.walk_wait`) instead of firing at a
  cooling pair; a `2xx` clears the provider block for that pair.
- `503/502` overloads no longer count as RPD spend — an overload storm no longer fakes
  an exhausted day in `gemini-router quota`.
- The library path applies the same provider truth on 429 (no more in-place retries of
  an unexplained refusal) while still waiting out a confirmed short minute quota.

### Added
- `feedback.py`: `parse_provider_signal`, `ProviderSignal`, `QuotaTruth` — one place
  where a provider answer becomes durable ledger state. `pair_state` (schema v3) is the
  authority over the counters; `cooldowns` is mirrored for older readers.
- `context.py`: `RequestContext`, logfmt `audit_line`, `router_version`. Every audit row
  and log line carries version, initiator and an arbitrary behaviour-free comment.
- `X-Router-Initiator` / `X-Router-Comment` / `X-Router-Request-Id` headers (stripped
  before upstream), `RouterRequest.initiator` / `.comment`, `calls --wide`,
  `calls --initiator`.
- `gemini-router note` / `notes` CLI and `POST /v1/note`, `GET /v1/notes`: annotations
  with no provider call and no quota effect. Example: `gemini-router note "начинаем
  тестирование новой версии" --initiator ops-audit`.
- `event=startup/provider_feedback/note/pool_throttled/request` structured logs;
  `/healthz` reports the version.
- Config: `feedback.*` (throttle floors/ceilings, promotion, overload cooldown),
  `proxy.default_initiator`, `proxy.walk_wait`,
  `proxy.pool_circuit_breaker`. See `config.example.yaml`.

### Notes
- Schema v3 is additive and backwards compatible: v1/v2 databases migrate in place,
  old library builds keep reading `calls` and `cooldowns`.
- 220 tests pass (23 new). All HTTP is mocked; no real keys are used in CI.
