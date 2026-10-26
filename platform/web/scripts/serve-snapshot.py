"""Serve the read-only web snapshot on http://127.0.0.1:8080 (python serve.py [port]).

A service worker needs an http origin, so the folder cannot be opened as a file.
Unknown paths fall back to index.html, so a deep link such as /workbench/<run_id> also loads on reload.
Standard library only.
"""

import http.server
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript", ".gz": "application/octet-stream"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def send_head(self):
        path = self.translate_path(self.path)
        if not os.path.exists(path) and not self.path.startswith(("/snapshot/", "/assets/")):
            self.path = "/index.html"
        return super().send_head()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Snapshot at http://127.0.0.1:{port}/ (Ctrl+C stops it)")
        server.serve_forever()
