# Capability plan: file input through the provider Files API

The router's half of the feature whose product rules live in
`vernikr/gemini-mcp → docs/worklog/2026.10.11-binary-file-support/plan.md`. This file states
the capability and its interface **once**; the package plan states how the gateway uses it.

## Interface the package builds on

```python
# gemini_router (library)
up = await router.upload_file(path, mime="application/pdf", model="gemini-3.8-flash",
                              source="mcp")          # → UploadedFile
# UploadedFile: name ("files/<id>"), uri, mime, size, state, expires_at, key_id

n = await router.count_file_tokens("gemini-3.8-flash", up)         # exact, FR-C7-free
res = await router.complete_file(req, up, req.files)               # key pinned to up.key_id
```

- `upload_file` picks the serving key first (same order `complete()` uses), uploads with
  that key and records the ledger row. The returned `key_id` is what the package pins when
  it sends the analysis request — a file is visible only to the key's own project.
- `complete_file` adds the file part ahead of the text (the provider's own guidance for
  single-file prompts) and never selects another key: switching keys would invalidate the
  reference. On a key-scoped failure the package re-uploads under the next key, which is
  why the blob stays in the package's inbox rather than the library's memory.
- `count_file_tokens` measures a file before it is sent, so a pack of files can be planned
  the way text chunks are planned. It is a `countTokens` call and does not draw on RPD.

## Rules the capability enforces

- **The blob is streamed, never buffered twice.** Upload sends the file object through
  httpx; nothing base64-encodes it and nothing holds two copies in RAM.
- **Ledger rows for uploads, in their own kind.** `kind='upload'` with the byte count, the
  key and the latency — visible in `router_logs`, excluded from RPD/RPM/TPM and from the
  pacing "last call" clock, because an upload is not a `generateContent` call. Accounting
  rules now name non-spending kinds in one place instead of a bare `kind != 'count'`.
- **Cache keys never contain the provider URI** (package decision, honoured here): the
  request hash mixes in the file's content hash, so a hit survives re-uploads and travels
  across keys.
- **Expiry is data, not an error.** `expires_at` rides on `UploadedFile`; deciding to
  re-upload is the caller's rule. The library also exposes `delete_file` for tidy-up when
  the package prunes its own blob.

## Errors

Same taxonomy as the rest of the client: `_classify` maps HTTP to retryable / switch /
key-rejected, plus two file-specific ones — `file_failed` (provider marked the upload
FAILED) and `file_not_ready` (still PROCESSING after the wait budget). Both carry the
provider name and are neither retried blindly nor counted as quota spend.

## Config

`upload_base_url` (default `https://generativelanguage.googleapis.com/upload/v1beta`),
separate from `base_url`, because the resumable upload lives on a different host path than
`generateContent`.

## Tests

respx mocks for the three-step lifecycle (start → upload+finalize → state poll), the token
count with a file part, the ledger row landing in a non-spending kind, and the request
payload carrying `file_data` ahead of the text. No network, no real keys.
