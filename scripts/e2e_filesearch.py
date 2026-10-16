#!/usr/bin/env python3
"""End-to-end check of the pass-through against the REAL google.golang.org/genai SDK.

Not part of `pytest` (it needs a Go toolchain and a local port): run it by hand when the
File Search surface changes. It starts the fake upstream, starts the real proxy pointed
at it, and lets the Go probe drive the whole File Search flow through
GOOGLE_GEMINI_BASE_URL — the same way github.com/mikesmitty/file-search does.

    python3 scripts/e2e_filesearch.py [path/to/go/probe]

Keys are DUMMY: the upstream is a local fake, nothing reaches Google.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- fake upstream
SESSIONS: dict[str, int] = {}
LOG: list[dict] = []


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200, extra=None):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def route(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        u = urlparse(self.path)
        p, q, m = u.path, u.query, self.command
        LOG.append({"method": m, "path": p, "query": q, "bytes": len(body),
                    "key": self.headers.get("x-goog-api-key")})

        if p.startswith("/upload/") and "upload_id" not in q:
            sess = "UP-" + p.rsplit("/", 1)[-1][:12]
            SESSIONS[sess] = 0
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            # Google answers with its own absolute URL — the SDK must bring it back.
            self.send_header("X-Goog-Upload-Url",
                             "https://generativelanguage.googleapis.com"
                             f"{p}?upload_id={sess}&upload_protocol=resumable")
            self.send_header("X-Goog-Upload-Status", "active")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if p.startswith("/upload/") and "upload_id" in q:
            sess = q.split("upload_id=")[1].split("&")[0]
            SESSIONS[sess] = SESSIONS.get(sess, 0) + 1
            last = "finalize" in (self.headers.get("x-goog-upload-command") or "")
            payload = ({"name": "fileSearchStores/demo/operations/op-1", "done": last}
                       if ":uploadToFileSearchStore" in p else
                       {"file": {"name": "files/f-1", "uri": "https://x/files/f-1",
                                 "mimeType": "text/plain", "sizeBytes": str(len(body)),
                                 "state": "ACTIVE"}})
            self._json(payload, extra={"X-Goog-Upload-Status": "final" if last else "active"})
            return
        if p == "/v1beta/fileSearchStores" and m == "POST":
            return self._json({"name": "fileSearchStores/demo", "displayName": "demo"})
        if p == "/v1beta/fileSearchStores" and m == "GET":
            return self._json({"fileSearchStores": [{"name": "fileSearchStores/demo",
                                                     "displayName": "demo"}]})
        if p.startswith("/v1beta/fileSearchStores/"):
            if m == "DELETE":
                return self._json({})
            if "/operations/" in p:
                return self._json({"name": p[len('/v1beta/'):], "done": True, "response": {
                    "parent": "fileSearchStores/demo",
                    "documentName": "fileSearchStores/demo/documents/doc-1"}})
            if p.endswith(":importFile"):
                return self._json({"name": "fileSearchStores/demo/operations/import-1",
                                   "done": False})
            if p.endswith("/documents"):
                return self._json({"documents": [
                    {"name": "fileSearchStores/demo/documents/doc-1",
                     "displayName": "doc.txt", "state": "ACTIVE", "sizeBytes": "12"}]})
            return self._json({"name": p[len("/v1beta/"):], "displayName": "demo"})
        if p == "/v1beta/files" and m == "GET":
            return self._json({"files": [{"name": "files/f-1", "displayName": "p.txt",
                                          "uri": "https://x/files/f-1",
                                          "mimeType": "text/plain", "sizeBytes": "12",
                                          "state": "ACTIVE"}]})
        if p.startswith("/v1beta/files/"):
            return self._json({} if m == "DELETE" else {
                "name": p[len("/v1beta/"):], "displayName": "p.txt",
                "uri": "https://x/files/f-1", "mimeType": "text/plain",
                "sizeBytes": "12", "state": "ACTIVE"})
        if p.endswith(":generateContent"):
            return self._json({"candidates": [{"content": {"role": "model", "parts": [
                {"text": "ANSWER"}]}, "finishReason": "STOP"}]})
        if p == "/v1beta/models":
            return self._json({"models": [{"name": "models/gemini-2.5-flash"}]})
        return self._json({"error": {"code": 404, "message": f"unhandled {m} {p}",
                                     "status": "NOT_FOUND"}}, status=404)

    do_GET = do_POST = do_DELETE = do_PUT = route


def main() -> int:
    probe = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    sys.path.insert(0, str(HERE.parent / "src"))
    from gemini_router import Config
    from gemini_router.proxy import GeminiProxy

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    up_port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    state = tmp / "e2e-proxy-state.db"
    state.unlink(missing_ok=True)
    cfg = Config.model_validate({
        "chain": ["gemini-3.8-flash"],
        "api_keys": ["DUMMY-upstream-key-1", "DUMMY-upstream-key-2"],
        "state_path": str(state),
        "base_url": f"http://127.0.0.1:{up_port}/v1beta",
        "upload_base_url": f"http://127.0.0.1:{up_port}/upload/v1beta",
        "proxy": {"bind": "127.0.0.1", "port": 0, "request_timeout_s": 30.0,
                  "token_env": "GEMINI_ROUTER_PROXY_TOKEN"},
    })
    token = "DUMMY-router-token"
    proxy = GeminiProxy(cfg, token=token)
    httpd = proxy.server()
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"proxy  http://127.0.0.1:{port} -> http://127.0.0.1:{up_port}", flush=True)

    if probe is None:
        print("no Go probe given; proxy is up. Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    env = {**os.environ,
           "GOOGLE_GEMINI_BASE_URL": f"http://127.0.0.1:{port}",
           "GEMINI_API_KEY": token,
           "PATH": os.environ.get("PATH", "")}
    print(f"--- go probe: {probe}", flush=True)
    rc = subprocess.run(["go", "run", "."], cwd=str(probe), env=env).returncode

    print("\n--- upstream saw ---")
    for e in LOG:
        print(f"  {e['method']:6s} {e['path']}"
              f"{'?' + e['query'] if e['query'] else ''} [{e['bytes']}B] key={e['key']}")
    leaked = [e for e in LOG if token in (e["key"] or "")]
    print(f"\nrequests carrying the CLIENT token upstream: {len(leaked)} (must be 0)")
    print(f"upload sessions used: {SESSIONS}")
    from gemini_router import Ledger
    led = Ledger(str(state))
    print(f"affinity learned: {led.fs_affinity()}")
    print(f"ledger kinds: {sorted({r['kind'] for r in led.calls_tail(50)})}")
    return rc or (1 if leaked else 0)


if __name__ == "__main__":
    raise SystemExit(main())
