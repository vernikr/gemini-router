# Router default-chain docs hygiene · 2026-10-11

README and SPEC described a hardcoded default chain containing deprecated alias
slots, unlike `src/gemini_router/config.py:DEFAULT_CHAIN`. The default now has one
canonical location in code; evergreen prose links to it instead of copying
volatile ids. The same docs also implied `tldr-digest` was already sharing the
ledger, though its migration is unfinished. Corrected the claim to distinguish
current connected consumers from possible future ones. No library/runtime
behavior changed; docs pushed to main. The gateway's own stale tool description
is fixed separately: see gemini-mcp
`docs/worklog/2026.10.11-model-help-hygiene/`.
