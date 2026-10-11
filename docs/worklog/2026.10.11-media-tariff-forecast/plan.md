# Capability plan: media tariff forecast + drift detection

Owner's ask (2026-10-11): since image/PDF pricing turned out to be simple and deterministic,
should the server keep its own tariff table for pre-computed estimates — and if so, how do we
get an explicit signal when the tariff changes and the hardcode starts missing?

**Status: planned, not implemented.** C2 (group packing) is the first consumer.

## What "the tariff" is, measured

Measured 2026-10-11 on the production key (`gemini-3.6-flash`; probes in
`vernikr/gemini-mcp → docs/worklog/2026.10.11-binary-file-support/probes/`):

| media | default | low | medium | high |
|---|---|---|---|---|
| image (any size tested, 2 KB…7 MB, 300×300…4096×4096) | **1089** (= high) | 256 | 529 | 1089 |
| PDF page | **560** (= medium) | 280 | 560 | 1120 |

Audio/video stay provider-published until measured: 25 tokens/s, 70 tokens/frame at 1 fps —
recorded with provenance, not treated as verified.

The count is flat per resolution level; byte size never predicts it (a 2 KB and a 7 MB image
cost the same). `countTokens` and `usageMetadata.promptTokenCount` agree, and 3 PDF pages
measure exactly 3 × 560.

## What the forecast is for — and what it must never do

The forecast is a **planning number**: pre-flight feasibility before an upload, packing math
for C2, and cost previews the agent can show the owner. It must never replace measurement:
`count_file_tokens` is free (no RPD), so every provider-path request keeps measuring, and
`meta.file.tokens` stays a measured fact. The forecast exists because planning wants an
answer *before* any provider round-trip, not because measurement is expensive.

API sketch (library owns cost semantics; the gateway consumes):

```python
from gemini_router import forecast_media_tokens as f
f("image/png")                       # → 1089 (default level)
f("application/pdf", pages=3, level="low")   # → 840
```

## Drift detection: the table is wrong the day Google changes it

Because measurement is free and present on every file call, the forecast can be checked at
~zero cost every time it is used. Design, three layers from instant-and-cheap to durable:

1. **In-band, agent-visible (instant).** The provider-path result carries
   `meta.cost.drift = {predicted, measured, pct}` whenever `|measured − predicted|` exceeds
   `max(5 %, 50 tokens)`, plus one instruction line asking the host agent to tell the owner in
   plain words ("router's image-cost forecast missed: predicted 1089, measured 1300"). This
   reaches the person who is actually in the conversation, needs no infrastructure, and costs
   nothing when there is no drift (the field is simply absent).
2. **Server-side flag (on demand).** A per-(class, level, model) rolling counter in the
   ledger (`kind='drift'`, non-spending); `gemini-mcp doctor` and `quota` print one line when
   the recent miss rate crosses a threshold. This is what a debugging session reads.
3. **Scheduled check with a real email (durable), run from GitHub Actions.** A small workflow
   (`schedule:` weekly + on release) uploads three fixtures — tiny image, 384 px image, 1-page
   text PDF — and calls `countTokens` on each (free, no RPD), then diffs against the table and
   fails on drift. Two notification channels, both already proven in the owner's setup:
   **the mail path is Gmail SMTP from GitHub Actions** — the same one `tldr-digest` uses
   (`GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` / `MAIL_TO` secrets, cron in a workflow), so the
   drift alert is an ordinary email with the numbers in the subject line; and independently,
   **a red workflow run** is itself the fallback signal, since GitHub notifies on failures.
   Correction (owner, 2026-10-11): an earlier draft of this plan said "no mail path exists" —
   that was wrong. The *VPS* has no MTA (verified: no Postfix/exim unit, nothing listening on
   25/587), but mail never needed the VPS: it goes out from Actions over Gmail SMTP. The
   check needs one API key as a repo secret (a dedicated spare account is fine; the `keys`
   command can manage its pool like any other host).

## A second input for the packer (measured 2026-10-11 evening)

Packing groups of files needs to know **which models can read which media**, because the
budget is per key × model and capability turned out to be per model × modality × transport:
measured 2026-10-11 (full matrix in gemini-mcp `docs/worklog/2026.10.11-remaining-work/plan.md`
§E), PDF via `file_data` is refused by `gemini-3.5-flash-lite` (500) and
`gemini-3-flash-preview` (400) while serving on the 3.6/3.8 flash models; PNG serves on lite
and preview, and `gemma-4-31b-it` refuses `file_data` but accepts inline images. Consequence
for this design: the forecast table is only half of the prediction — the packer must also
predict **which model will serve the group**, and its candidate list is the capable set for
the media in question, not the whole chain. The library enforces the conservative version
since 0.1.15 (`file_capable_models`, plus a learned per-process set); the per-modality
refinement is a recorded plan item in the product repo.

## Rules kept

- The table lives in the library (one copy, with a dated provenance comment), never in docs
  prose and never in the gateway.
- Forecast miss is a *signal*, not an error: requests keep working on measured numbers.
- Ledger accounting does not change: uploads and counts stay in non-spending kinds; drift
  rows spend nothing.
- Operational lesson recorded 2026-10-11: provider calls made outside the daemon (ad-hoc
  probes) do not reach the ledger, so the ledger's quota view stays optimistic until the
  first 429 — which the router absorbs by marking the key×model exhausted and stepping to the
  next key. Probe tooling should therefore stick to free calls (`countTokens`, `listModels`).

## Open questions

- Whether the MCP surface should expose `media_resolution` per call or per media type.
  Owner's current call: images stay at the provider default (high) — the per-image cost is
  small — while the table remains the packing lever for C2.
- Where the fixtures for the scheduled check live (repo `tests/fixtures/` vs generated by the
  workflow script; generated keeps the repo clean and the inputs verifiable).
