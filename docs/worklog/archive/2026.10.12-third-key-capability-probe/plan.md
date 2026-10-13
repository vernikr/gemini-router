# Third-key capability probe · 2026-10-12

Owner authorized a one-off run using the recently added third VPS key. No key was
copied to GitHub Actions, no production service or VPN configuration was modified.
The probe used the published content-verifying canary on the VPS, exactly one
`generateContent` per case, no retries. It wrote every attempt to the SAME router
SQLite ledger as the daemon (`source=capability_probe`) to keep its quota view
honest. No raw key or provider response text was logged.

| Case | Observed | Verdict against the 2026-10-11 baseline |
|---|---|---|
| Flash/PDF positive control | HTTP 200; hidden printed code reproduced | match |
| Lite/PNG positive control | HTTP 200; generated image color identified | match |
| Lite/PDF, previously refused | HTTP 200; hidden printed code reproduced | **verified drift** |
| Gemma/PNG, previously refused | HTTP 500 | match |

Exactly four provider generations were recorded in the shared ledger: three `ok`
and one `http5xx` for key fingerprint `0386d1ae`. Fixture upload/delete and other
non-generation requests were not counted as RPD. The proof string was in the PDF
but not the prompt; generic text cannot satisfy the test. This corroborates the
*Lite/PDF* half of the first provisional CI email on a new account. The first email's
*Gemma/PNG* claim was not corroborated — its earlier nonempty reply had not proved
content reading. The second CI email was an independent HTTP 429 on the old CI key.

**Scope/decision:** one new key and one short PDF do not justify turning on Lite/PDF
for every key/model or changing the production router's conservative file gates.
Keep the rollout stable. After quota reset, the scheduled CI canary (which uses a
DIFFERENT repo secret) can test a second account; if it verifies the hidden code,
review a per-key capability policy or a narrowly opt-in Lite/PDF fallback with an
agent-visible ⚠️. Do not automatically rewrite the matrix based on HTTP 200 alone.

Published as documentation/comment-only router patch 0.3.4: 115 tests, ruff and
secrets guard green; release test/PyPI jobs green. The gateway's deployed routing
behavior is unchanged.

**2026-10-13 addendum:** the scheduled run on 2026-10-12 independently verified
Lite/PDF on the older CI key and saw the same Gemma/PNG HTTP 500. The original
one-key limitation above is now historical; the routing decision remains open.
See `docs/worklog/archive/2026.10.11-capability-canary/` for the CI evidence.
