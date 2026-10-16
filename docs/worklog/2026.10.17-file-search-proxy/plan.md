# File Search MCP compatibility — plan

## Goal
Run `github.com/mikesmitty/file-search` (a local MCP server speaking the native Gemini
API via `google.golang.org/genai`) against the VPS key pool through
`GOOGLE_GEMINI_BASE_URL`, without touching the MCP's source and without duplicating the
key rotation.

## Approach
Add a pass-through surface (`src/gemini_router/proxy.py` + `src/gemini_router/filesearch.py`)
that speaks native `v1beta` and reuses the pool, `keyauth`, the ledger and the error
taxonomy. New: client auth, resource→key affinity, upload-session tickets.

## Decisions
- stdlib `http.server` (no new dependency, fits the 1 GB box, library stays MCP-agnostic)
- separate systemd unit on `127.0.0.1:8787`, reached through the existing ssh tunnel
- affinity in the shared ledger, so a restart cannot orphan stores
- `auto` model policy by default (keep what the key can serve, fall back to the chain)
