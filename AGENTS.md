# AGENTS.md — gemini-router

Follow the owner's global AGENTS.md (English in files, Russian in chat, PM-style reports,
Conventional Commits, SemVer tags `v*`, push after every successful step, docs updated
with functional changes). Repo-specific rules:

## Context
- Contract: `docs/SPEC.md`; system-wide requirements/architecture/plan live in
  `vernikr/gemini-mcp/docs/02-requirements.md` + `docs/03-architecture.md`. Keep
  SPEC.md, README.md and module docstrings true to the code, in present tense; release
  history belongs in `CHANGELOG.md`.
- This library MUST stay dependency-light (`httpx`, `pydantic`, `rich`, stdlib) and
  MCP-agnostic — it is consumed by gemini-mcp, tldr-digest (sync facade) and outsiders.
- Mixed-version consumers share the ledger: use spending statuses understood by older
  deployed readers, or coordinate a migration before emitting new statuses.
- All quota truth = SQLite ledger (`GEMINI_ROUTER_STATE`); no in-memory-only counters.
  Multi-process safety (WAL, busy_timeout) is a hard requirement.
- Model IDs are volatile (docs/blockers b04 in gemini-mcp): resolve via `listModels`,
  never hardcode availability; every list config-overridable.

## Commands
- `uv sync` · `uv run pytest` · `uv run ruff check src tests`
- For native GitHub sub-issues, ensure `.github/workflows/issue-parent-sync.yml` exists with `issues: [opened, edited]`, `permissions: issues: write`, and a job calling `vernikr/mother/.github/workflows/issue-parent-sync.yml@v1` (create the caller workflow if missing); then put `<!-- issue-parent: #N -->` on its own line in the child Issue body, or use `<!-- issue-parent: none -->` to detach.
- `uv run gemini-router quota|models|calls|jobs|job|note|notes|prune|doctor`

## Testing & secrets
- respx-mocked HTTP only (no network, no real keys in CI/fixtures).
- Never log or persist raw API keys — `key_id = sha256[:8]`.
- Ported tldr-digest logic keeps its tested taxonomy (RETRYABLE_STATUS,
  MODEL_SWITCH_STATUS, KEY_REJECT_STATUS, Retry-After, extract_json) — extend, don't
  reinvent.

## Docs
- Worklog lives in the repo that owns the change. A feature spanning both repos splits
  into a capability plan here and a product plan in the package (or the reverse), each
  stating its own half once and cross-linking the other.
- Closed iterations move to `docs/worklog/archive/` — history, not contract. Live plans
  point there by full path; an archived folder keeps its name.
- No counters in evergreen docs: no version numbers, dates, test counts or "N green"
  claims. They go stale silently and every one of them is a fact somebody must guard.
  Keep only what a reader cannot get from `pyproject.toml`, `--version` or `pytest`.

## Secrets

Never write a real key or token into any file here — not even a test fixture, not even
"just for a moment": this repository is public and its sdist ships `tests/`. Fixtures use
real-looking shapes carrying the marker word `DUMMY`; `scripts/check_secrets.py` runs
before lint in CI and in the release pipeline and fails on any credential-shaped literal
without that marker. npm/PyPI/GitHub tokens are passed only through the environment of
the publishing shell; they never appear in source, config examples or release notes.
