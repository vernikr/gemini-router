# AGENTS.md — gemini-router

Follow the owner's global AGENTS.md: write repository files in English, discuss in Russian,
use Conventional Commits and SemVer tags, and update documentation with behavior changes.

## Context
- `docs/SPEC.md` is the API and behavior contract. Keep it, `README.md`, and module
  docstrings aligned with the implementation; release notes belong in `CHANGELOG.md`.
- Keep the Python library dependency-light (`httpx`, `pydantic`, `rich`, stdlib) and
  independent of any MCP framework. Consumers include the Gemini gateway, Python apps,
  the CLI, and HTTP clients.
- Quota truth is the SQLite ledger (`GEMINI_ROUTER_STATE`); do not add in-memory-only
  counters. Preserve WAL and multi-process safety.
- Resolve model availability with `listModels`; model IDs can change and every configured
  model list must remain overridable.
- The CLI checks PyPI for updates before user-facing commands. `--no-update` or
  `GEMINI_ROUTER_AUTO_UPDATE=0` disables the check. The long-running `proxy` service uses
  its deployment-managed version and does not self-update.
- TTS progress is monotonic: completed chunks are measured, the active chunk uses capped
  time-based pseudo-progress, ETA adapts from observed throughput, and ffmpeg reports
  actual MP3 encoding progress.

## Commands
- `uv sync` · `uv run pytest` · `uv run ruff check src tests`
- `uv run gemini-router quota|models|calls|jobs|job|note|notes|prune|doctor|tts`
- For native GitHub sub-issues, keep `.github/workflows/issue-parent-sync.yml` configured
  to call `vernikr/mother/.github/workflows/issue-parent-sync.yml@v1`; then use
  `<!-- issue-parent: #N -->` on its own line in the child Issue body, or
  `<!-- issue-parent: none -->` to detach.

## Testing and secrets
- CI tests provider behavior with mocked HTTP responses; do not use live credentials or
  spend provider quota in automated tests.
- Never log or persist raw API keys; identify them by `key_id = sha256[:8]` only.
- Run `scripts/check_secrets.py` before publishing. Test credential-shaped fixtures must
  include the `DUMMY` marker.
- Publishing credentials belong in the publishing environment or GitHub Actions secrets,
  never in source, config examples, or release notes.

## Documentation
- Evergreen docs describe current interfaces and behavior. Keep release history in
  `CHANGELOG.md`; avoid implementation diaries, migration provenance, stale dates, and
  duplicated version or test-count claims.
