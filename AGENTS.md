# AGENTS.md — gemini-router

Follow the owner's global AGENTS.md (English in files, Russian in chat, PM-style reports,
Conventional Commits, SemVer tags `v*`, push after every successful step, docs updated
with functional changes). Repo-specific rules:

## Context
- Contract: `docs/SPEC.md`; system-wide requirements/architecture/plan live in
  `vernikr/gemini-mcp/docs/02-requirements.md` + `docs/03-architecture.md`. Keep SPEC.md true to the code.
- This library MUST stay dependency-light (`httpx`, `pydantic`, `rich`, stdlib) and
  MCP-agnostic — it is consumed by gemini-mcp, tldr-digest (sync facade) and outsiders.
- All quota truth = SQLite ledger (`GEMINI_ROUTER_STATE`); no in-memory-only counters.
  Multi-process safety (WAL, busy_timeout) is a hard requirement.
- Model IDs are volatile (docs/blockers b04 in gemini-mcp): resolve via `listModels`,
  never hardcode availability; every list config-overridable.

## Commands
- `uv sync` · `uv run pytest` · `uv run ruff check src tests`
- `uv run gemini-router quota|models|calls|prune|doctor`

## Testing & secrets
- respx-mocked HTTP only (no network, no real keys in CI/fixtures).
- Never log or persist raw API keys — `key_id = sha256[:8]`.
- Ported tldr-digest logic keeps its tested taxonomy (RETRYABLE_STATUS,
  MODEL_SWITCH_STATUS, KEY_REJECT_STATUS, Retry-After, extract_json) — extend, don't
  reinvent.

## Docs
- No counters in evergreen docs: no version numbers, dates, test counts or "N green"
  claims. They go stale silently and every one of them is a fact somebody must guard.
  Keep only what a reader cannot get from `pyproject.toml`, `--version` or `pytest`.
