# File capability per media class · done 2026-10-11, shipped v0.3.0

Capability gates (`file_capable_models`) and 400/500 learning now key by media
class (pdf/image/audio/video from `media_class(mime)`) instead of one shared set,
because the measured capability matrix differs per class (e.g. gemma rejects image
`file_data` but accepts it for pdf). Config accepts a list or a per-class dict.

- Design and measured matrix: gemini-mcp `docs/worklog/2026.10.11-binary-file-support/`
- Contract: `docs/SPEC.md` (files capability section)
- Tests: `tests/test_files.py`
