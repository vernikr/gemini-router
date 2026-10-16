# Wiring `mikesmitty/file-search` to the router (Freebuff)

The MCP server runs **locally** (spawned by the client) and speaks the **native Gemini
API** through `google.golang.org/genai`. Two environment variables are all it takes —
no change to the MCP's source:

| variable | value | who reads it |
|---|---|---|
| `GOOGLE_GEMINI_BASE_URL` | `http://127.0.0.1:8787` | the Gen AI SDK, at client start |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | the router token | file-search (`viper`), then the SDK as `x-goog-api-key` |

**`GOOGLE_GEMINI_BASE_URL` must be a bare origin** — no path prefix, no `/v1beta`. The
SDK prepends the version itself, and on the upload continuation it keeps only the base
URL's scheme and host, so a prefix is silently dropped and uploads break (SPEC §11.2).

## 1. VPS: the service

```bash
# unit + token (reuses the gateway's key pool and state.db)
cp packaging/gemini-router-proxy.service /etc/systemd/system/
printf 'GEMINI_ROUTER_PROXY_TOKEN=%s\n' "$(openssl rand -hex 32)" >> /opt/apps/gemini-mcp/.env
systemctl daemon-reload && systemctl enable --now gemini-router-proxy
systemctl is-active gemini-router-proxy && curl -sf http://127.0.0.1:8787/healthz
```

The unit listens on `127.0.0.1:8787` only. Port `8787` is free on the host and is never
published; nothing in the firewall changes.

## 2. Mac: the tunnel

Reuse the existing agent — add a second `LocalForward` rather than a second launchd job:

```bash
cp packaging/com.vernikr.gemini-tunnel.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.vernikr.gemini-tunnel.plist
launchctl load   ~/Library/LaunchAgents/com.vernikr.gemini-tunnel.plist
curl -sf http://127.0.0.1:8787/healthz && echo OK      # log: /tmp/gemini-tunnel.log
```

If `8787` is taken locally, change the FIRST number of `-L 8787:127.0.0.1:8787` and use
that port in the base URL below.

## 3. Freebuff: `~/.agents/mcp.json`

Home-level only — project-level configs are cached unreliably. Restart Freebuff fully
after editing; the SDK reads the base URL when the process starts.

```jsonc
{
  "mcpServers": {
    "file-search": {
      "command": "/usr/local/bin/file-search",
      "args": ["mcp"],
      "env": {
        "GEMINI_API_KEY": "<GEMINI_ROUTER_PROXY_TOKEN>",
        "GOOGLE_GEMINI_BASE_URL": "http://127.0.0.1:8787",
        "MCP_TOOLS": "all"
      }
    }
  }
}
```

Never copy the token by hand (a stray character cost days of 401s once):

```bash
ssh root@127.0.0.1 'grep ^GEMINI_ROUTER_PROXY_TOKEN /opt/apps/gemini-mcp/.env \
  | cut -d= -f2 | tr -d "\n"' | pbcopy
```

`args: ["mcp"]` with `MCP_TOOLS=all` exposes every tool; `MCP_TOOLS=query_knowledge_base`
is the read-only minimum.

## 4. Verify

```bash
# transport + auth
curl -s http://127.0.0.1:8787/healthz                                   # {"ok":true,...}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/v1beta/files   # 401 (no token)
curl -s -o /dev/null -w '%{http_code}\n' -H "x-goog-api-key: <token>" \
     http://127.0.0.1:8787/v1beta/files                                  # 200
```

Then in Freebuff: *create a store, upload a file, wait for indexing, query it*. On the
VPS:

```bash
journalctl -u gemini-router-proxy -n 50 --no-pager     # one line per request, key_id only
gemini-router calls -n 20                              # kind=filesearch | single
gemini-router quota                                    # the query spent one RPD
```

**After a Mac reboot:** `launchctl list | grep gemini-tunnel` (should be loaded), then
`curl -sf http://127.0.0.1:8787/healthz`. If it fails, `tail /tmp/gemini-tunnel.log` —
the usual cause is a stale ssh key or a dead WiFi before `ServerAlive` gives up.

## 5. Notes and limits

- The tunnel is transport, **not** authorization: the router still requires the token on
  every request, including upload continuations.
- Stores live in the Google project of the key that created them, so the router pins
  every later request for a store to that key (SPEC §11.4). `list_stores` therefore shows
  the stores of ONE project — create stores through the router, not in the console under
  a different key, or they will not be visible.
- Only the File Search surface is served: stores, documents, operations, Files API,
  models and resumable upload. Anything else is a JSON 404 (SPEC §11.1).
- The model in `query_knowledge_base` is honoured as-is when the key can serve it
  (`proxy.model_policy: auto`); set `strict` in the router config to force the chain.
