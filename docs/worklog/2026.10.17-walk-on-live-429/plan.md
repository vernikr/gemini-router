# Walk continues on live 429/503 (proxy)

## Why
First production consumer (browser-agent vision loop) hit it immediately: keys shared
with an out-of-band consumer were day-drained at the provider while the ledger showed
them fresh. The proxy forwarded the first 429 and the caller had no way to reach the
fresh tiers below (3.6-flash, lite) short of re-sending blind.

## What
`GeminiProxy._proxy`: after the first attempt of an unbound, buffered generateContent,
while the answer is 429/503 — cool the pair (429: existing taxonomy; 503: brief
OVERLOAD_COOLDOWN_S) and re-run `routing.choose`; stop when the walk repeats itself,
the pool resolves nothing, or WALK_RETRIES hops are spent; forward the last provider
error otherwise. Every attempt gets its own ledger plan/resolve row.

## Done when
- One request served by the next key/tier after a live daily 429 (test).
- 503 benches the pair briefly, not until midnight (test).
- Tier crossover under full live drain of the head model (test).
- Fully dead pool still answers with the provider's own 429, bounded (test).
- SPEC §11.6 updated.
