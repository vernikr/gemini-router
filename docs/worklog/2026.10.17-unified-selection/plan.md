# Unified selection for every entry point

## Why

The owner's rule (2026-10-17): the router's parameter logic must be identical
regardless of the entry door, and must not be duplicated. The pass-through proxy
violated both halves:

- its `resolve_model` walked only the chain — the overflow tiers (`flash-lite`
  at 500 RPD/key) were unreachable through the proxy while the library honored them;
- an unbound `generateContent` rode the File Search pin key regardless of budgets,
  so one key drained while the rest of the pool idled;
- the walk itself (tiers × keys × gates) existed twice, slightly differently.

Driver: `vernikr/browser-agent` is being moved onto this proxy for its vision loop
(screenshot + prompt per step) and must inherit the lite overflow and pool rotation
without growing its own router logic back.

## What

1. `routing.tier_plan` / `routing.keys_sorted` / `routing.pick_pair` — the walk,
   defined once; `Router._tier_plan` / `_keys_sorted` now delegate to it.
2. `Config.overflow_default` (off|lite|lite+gemma, validated) — the single knob both
   entries start from; a library request may still override per call
   (`RouterRequest.overflow=None` now means "inherit").
3. `FileSearchRouting.choose` replaces `resolve_model`: policy maps inputs, the walk
   decides; resource affinity pins the key (only the model walks), an unbound model
   call walks both. Exhausted pool → head candidate forwarded, provider answers.
4. Proxy responses on model paths carry `x-router-model` / `x-router-key-id`.
5. `proxy.body_inspect_bytes` default 1 MiB → 4 MiB: vision bodies carry an inline
   base64 screenshot and must keep the buffered path (retry semantics, SPEC §11.7).

## Done when

- test_selection.py pins the shared walk; test_proxy.py pins that the proxy obeys it
  (rotation, overflow reach, affinity, exhausted forward, vision-sized body buffered).
- SPEC §2/§4/§11.6 and README reflect the contract. Full suite + ruff green.
