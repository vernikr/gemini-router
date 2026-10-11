# Capability closed: file input through the provider Files API

Released as **gemini-router 0.1.12**. This is the library half of the feature; the product
half — what the gateway does with the capability and how it was verified live — is released
as gemini-mcp 0.18.0 / 0.18.1 and recorded in
`vernikr/gemini-mcp → docs/worklog/2026.10.11-binary-file-support/report.md`.

## What changed

- `upload_file(path, *, mime=None, model=…)` → `UploadedFile{name, uri, mime, size, state,
  expires_at, sha256, key_id}`. It picks the serving key with the same order `complete()`
  uses and streams the blob through httpx — nothing base64-encodes it and nothing holds two
  copies in RAM.
- `count_file_tokens(model, uri, mime, *, key_id=None)` → the exact token count for a
  staging-file reference. A `countTokens` call, so it draws no RPD — the property that makes
  packing a group of files possible at all.
- `complete(req, …, files=[…])` — the file parts go ahead of the text, the candidate keys
  are pinned to the keys the files were uploaded under, and `est_tokens` must be supplied.
  The library never silently measures: a measured number in, a planned request out.
- `delete_file(name, key_id=None)` — best-effort tidy-up for the caller that prunes its own
  blob.
- **Cache keys never contain the provider URI**: the request hash mixes in the file's content
  hash, so a cache hit survives a re-upload and travels across keys.
- Ledger: `kind='upload'` rows carry bytes and latency, name the key, and are excluded from
  RPD/RPM/TPM and from the pacing clock through a named `NON_SPENDING_KINDS` set instead of a
  bare `kind != 'count'` check. The row keeps a readable model value through the
  `FILE_ROW_MODEL` sentinel. `SCHEMA_VERSION = "2"`, migrated on open.
- Two new error classes join the taxonomy: `file_failed` (the provider marked the upload
  FAILED) and `file_not_ready` (still PROCESSING after the wait budget). Neither is retried
  blindly and neither counts as spend.
- `upload_base_url` config (default `…/upload/v1beta`), separate from `base_url` because the
  resumable upload lives on a different host path.

Two implementation notes worth keeping, both found the hard way:

- httpx `AsyncClient` rejects a sync file object ("Attempted to send an sync request with an
  AsyncClient instance"); the upload passes an async generator plus an explicit
  `Content-Length`. Verified under respx: the header survives and no chunked encoding is
  added back.
- `wait_file_active` is not called when the finalize response already reports ACTIVE — the
  wait would only add a poll for nothing.

## Evidence

- Tests 58 → 73. respx mocks the three-step lifecycle (start → upload+finalize → state poll),
  the token count with a file part, the ledger row landing in a non-spending kind, and the
  request payload carrying `file_data` ahead of the text. No network, no real keys.
- Mutation check 3/3 caught: the sentinel/`SPENDS`/`NON_SPENDING_KINDS` contract and the
  file-hash in the `cache_hash` mix — each single-line break failed the suite (the lessons of
  the earlier ledger work applied: assert the contract, not a real model id).
- Release run on tag `v0.1.12` green; `uvx --from gemini-router==0.1.12` installs from PyPI
  and exposes `UploadedFile`, `FileNotReady` and the file parts builder.

## What the package half exercised, for the record

Gateway 0.18.0 calls `router.complete(req, on_event=…, files=[up])` with `up.key_id` pinned
and `est_tokens = file_tokens + len(task) // 4`. The live probe on the production VPS
confirmed the whole chain end to end — measured counts (560 tokens for a 765-byte PDF, 1089
for a 99-byte PNG), one pinned request, and reuse of the same uploaded file on a second call.
Numbers and timings: the package report named above.
