"""Record-and-replay HTTP proxy for parity runs.

    python3 replay_proxy.py <cache dir> <plex port> <plex upstream>
        <tautulli port> <tautulli upstream>

Every GET goes to the upstream once; the answer is kept under <cache dir> and served to
every later identical request. A parity run's Python and Rust sides then read one
recorded Plex and Tautulli, so a library that changes between them cannot show as a
difference in the store.
"""

import hashlib
import http.server
import os
import socketserver
import sys
import threading
import urllib.parse
import urllib.request

cache, plex_port, plex_up, taut_port, taut_up = sys.argv[1:6]
os.makedirs(cache, exist_ok=True)
lock = threading.Lock()


def make(upstream, tag):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            norm = (
                parts.path
                + "?"
                + repr(sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True)))
            )
            key = hashlib.sha256((tag + norm).encode()).hexdigest()
            path = os.path.join(cache, key)
            if os.path.exists(path):
                status, body = open(path, "rb").read().split(b"\n", 1)
                code = int(status)
            else:
                req = urllib.request.Request(
                    upstream + self.path,
                    headers={
                        k: v
                        for k, v in self.headers.items()
                        if k.lower() in ("accept", "x-plex-token")
                    },
                )
                try:
                    resp = urllib.request.urlopen(req, timeout=120)
                    code, body = resp.status, resp.read()
                except urllib.error.HTTPError as err:
                    code, body = err.code, err.read()
                with lock:
                    open(path, "wb").write(str(code).encode() + b"\n" + body)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


for port, up, tag in ((plex_port, plex_up, "plex"), (taut_port, taut_up, "taut")):
    threading.Thread(
        target=S(("127.0.0.1", int(port)), make(up.rstrip("/"), tag)).serve_forever, daemon=True
    ).start()
threading.Event().wait()
