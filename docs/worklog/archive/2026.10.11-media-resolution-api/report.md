# 0.1.13 — `media_resolution` becomes part of the library's contract

Owed worklog, written on 2026-10-11 at the end of the day; the change shipped the same
morning. The product half (the HIGH default for every media type, and the CLI's use of it)
is recorded in `gemini-mcp → docs/worklog/archive/2026.10.11-media-and-cli/report.md`.

## What the provider offers, and why it had to be an input

Gemini 3.x accepts `media_resolution` per media part (`MEDIA_RESOLUTION_LOW|MEDIUM|HIGH`),
and the token price depends on it: measured 2026-10-11 on `gemini-3.6-flash`, an image
costs 256/529/1089 tokens and a PDF page 280/560/1120 at low/medium/high. So a level is
not decoration — it decides the cost of the request, which means it must be part of the
measurement as well as the send.

## API (SPEC §8)

- `RESOLUTION_LEVELS` (word → provider enum) and `resolution_field(level)`: `{"level":
  "MEDIA_RESOLUTION_HIGH"}` or `None` for "inherit the provider default".
- `UploadedFile.media_resolution` + `with_resolution(level)`: a copy that carries the
  level; validated even when the value is None-adjacent, so a typo fails at the caller.
- `file_parts()` adds the field only when a level is set — a request that says nothing
  keeps the provider default, which is what `""` in the product config means.
- `count_file_tokens(..., media_resolution=None)`: the same level the analysis will use,
  or the count and the send disagree.

Nothing else changed: transport, retries, ledger semantics, and the pinned-key rule for
files are untouched. Tests 73 → 76, including the mutation check that the field is absent
when no level is set (a silent default would have hidden itself in cost, not in errors).
