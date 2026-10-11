# TODO: migrate from `generateContent` to the Interactions API

Lives in this repo because the change is the API client's, not the gateway's. Raised
2026-10-11 by the owner; the gateway-side plan only gains a conversational tool because of
it, and that tool's design is separate work. Not scheduled — this file exists so the
decision is made once, on verified facts, when it is worth making.

## Verified state of the world (ai.google.dev, 2026-10-11)

- Interactions API is **Generally Available since June 2026** and recommended for all new
  projects. `generateContent` is called **legacy** but remains fully supported, and a
  migration guide exists.
- New models, multimodal capabilities, tools and agent features launch **on Interactions
  first**. That is the strategic reason to move, not any single feature.
- Server-side state: the default `store=true` keeps the interaction and lets the next call
  reference it with `previous_interaction_id`. Retention: **paid 55 days, free tier 1 day**.
  `store=false` opts out, but is **incompatible with background execution** and with
  chaining.
- Per-turn parameters (`system_instruction`, `tools`, `generation_config` including
  `thinking_level`) are interaction-scoped and must be re-sent every turn even in stateful
  mode — relevant to how requests are built today.
- **Background execution** (`background=true`) for long-running tasks.
- Not yet available in Interactions: Batch API, **explicit caching**, custom safety
  settings, remote MCP for Gemini 3. Implicit caching works and is what stateful
  conversations benefit from.
- Model support covers the current chains (`gemini-3.8-flash`, `3.6-flash`,
  `3.5-flash-lite`, `3-flash-preview`, `gemma-4-31b-it`).

## What it buys us

Today's tools are single-shot: every chunk, consolidation and single call is an independent
prompt with no history, and the model never calls our tools. On that profile the wins are:

| Win | Verdict |
|---|---|
| **New features launch there first** | Applies — the main reason |
| **Background execution** | Applies — may replace the gateway's hand-rolled ticket-and-poll pattern for long jobs |
| **Multi-turn conversation** | Applies to a tool we do not have yet and plausibly will (owner, 2026-10-11): a local agent asking follow-ups about a previous answer. Server-side state is what makes that cheap — the client sends only the new turn, and the provider's implicit caching covers the history it keeps |
| Client stops resending history / higher cache hits | Only through the conversational tool above. What the docs claim is *more cache hits and lower cost*, not a lower request size; our own content-hash cache already covers the single-shot case and we control its TTL |
| Automatic function-calling loop | Does not apply — we do not use function calling; the tools run on our side |
| Fewer retry/recovery paths | Marginal — the ledger, the taxonomy and the ticket pattern already exist |

## Prerequisites before scheduling

1. A spike on the client: `countTokens`, file references and the error taxonomy must map
   onto the Interactions shapes without losing the ledger's per-model truth.
2. A decision on `store`: statelessness (`store=false`) suits retry-ready single-shot work,
   but forbids background execution and chaining. The conversational tool argues the other
   way — the two features may be mutually exclusive per call and the design must say which
   calls use which.
3. Free-tier availability of `background=true` and file references — verify live, record
   the finding here.
4. A conversational surface needs its own ledger story: one interaction per turn, or one
   row per conversation? Count per provider call, as today.

## Trigger to schedule

- The conversational tool enters the roadmap, **or**
- background execution removes a real piece of our own machinery, **or**
- a wanted feature ships on Interactions only, **or**
- Google starts marking `generateContent` deprecated rather than legacy.
