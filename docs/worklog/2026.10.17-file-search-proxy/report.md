# File Search MCP compatibility — report

## Measured, not assumed
Every claim about the SDK was checked by running `google.golang.org/genai v1.75.0`
against a local fake upstream (`scripts/e2e_filesearch.py` drives the real proxy with the
real Go probe):

- the full File Search flow — create store, list, get, resumable upload of a 9 MB file
  (2 chunks), operation polling, documents, import, Files API, `generateContent` with a
  `fileSearch` tool, delete — completes through the proxy;
- the SDK rewrites the upload URL's scheme+host onto the base URL, which is why the
  continuation comes back through the router — and why it **drops** any path prefix;
- the client token never reaches upstream; upload ids carrying `:` / `+` survive the
  round trip (the signature is checked against the decoded value).

## Changed
- `src/gemini_router/proxy.py` — new: HTTP service, client auth, allowlist, streaming
  forward, upload-URL rewriting, quota accounting.
- `src/gemini_router/filesearch.py` — new: scope resolution, resource→key affinity,
  upload tickets, model policy.
- `src/gemini_router/ledger.py` — additive `fs_affinity` table + `fs_*` accessors;
  `filesearch` added to `NON_SPENDING_KINDS`.
- `src/gemini_router/config.py` — additive `proxy:` block and `file_search_key:`.
- `src/gemini_router/cli.py`, `__init__.py` — `proxy` subcommand, new exports.
- `tests/test_proxy.py`, `scripts/e2e_filesearch.py`, `docs/SPEC.md` §11,
  `docs/file-search-wiring.md`, `packaging/*`.

## Not changed
`routing.Router`, `gemini.GeminiClient`, the public API surface, the rotation itself.

## Risks accepted
- One Google project owns File Search by default (a persisted pin); rotation still
  applies to ordinary `generateContent`.
- Unscoped reads of an unknown resource probe every key once — bounded, and it heals a
  lost affinity.
- Only the File Search surface is served; everything else is a JSON 404.
