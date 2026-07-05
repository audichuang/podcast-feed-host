#!/usr/bin/env python3
"""Token-gated WRITE endpoint for the podcast feed directory. LAN-only: the MCP
hosts share the NAS's internal network and PUT here directly (this port is NOT in
the Cloudflare Tunnel — only Caddy's read port is). Caddy serves /srv read-only;
this PUTs into /srv.

Deliberately tiny: single user, one shared bearer token, stdlib only. Every write
is temp->fsync->os.replace so Caddy never serves a half-written file. Never deletes
-> content-addressed mp3 URLs stay valid forever (immutable, no 404 for cached
clients)."""
from __future__ import annotations

import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("FEEDS_ROOT", "/srv")
TOKEN = os.environ.get("UPLOAD_TOKEN", "")
if not TOKEN:                                            # empty token = no auth at all
    raise RuntimeError("UPLOAD_TOKEN is required and must be non-empty")
PORT = int(os.environ.get("PORT", "80"))
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))  # 500 MB/file

# Path = /feeds/<token>/<name>. Both segments are a trust boundary — allowlist.
#   token: base32 lower of 15 bytes = exactly 24 chars of [a-z2-7]
#   name : exactly the set publish_series renders (EP\d{2} => <=99 episodes/feed)
_TOKEN = r"[a-z2-7]{24}"
_NAME = r"(?:feed\.xml|index\.html|show\.json|artwork\.(?:png|jpg)|EP\d{2}-[0-9a-f]{8}\.(?:mp3|pdf|html)|EP\d{2}-cover-[0-9a-f]{8}\.(?:jpg|png))"
_PATH_RE = re.compile(rf"^/feeds/({_TOKEN})/({_NAME})$")
_CHUNK = 1 << 20


def atomic_write(dst: str, rfile, length: int) -> None:
    directory = os.path.dirname(dst)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(
        directory, f".tmp-{os.path.basename(dst)}-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        remaining = length
        with open(tmp, "wb") as f:
            while remaining > 0:
                chunk = rfile.read(min(_CHUNK, remaining))
                if not chunk:
                    raise IOError("short read from client")
                f.write(chunk)
                remaining -= len(chunk)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Directory fsync makes the rename itself durable, but it's a nicety: once
    # os.replace returned, the write is committed. Some network FS reject dir
    # fsync -> best-effort, never turn a succeeded write into a 500.
    try:
        dfd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


# Marker on EVERY response, so the client's precheck can prove it reached THIS
# uploader and not the read-only Caddy (whose /healthz also returns 200). Caddy
# never sets this -> a mis-pointed PODCAST_UPLOAD_URL fails the precheck instead
# of passing then dying mid-mp3.
_MARKER = ("X-Podcast-Uploader", "1")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header(*_MARKER)
        self.send_header("Content-Length", str(len(body)))
        if code >= 400:
            # Don't keep-alive a connection whose request body we may not have
            # drained — it would desync the next request on the socket.
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path != "/healthz":
            return self._reply(404)
        auth = self.headers.get("Authorization")
        if auth is None:
            return self._reply(200, b"ok\n")              # docker healthcheck (200 only)
        ok = secrets.compare_digest(auth, f"Bearer {TOKEN}")  # client token probe
        return self._reply(200 if ok else 401)            # marker header set above

    def do_PUT(self):
        if not secrets.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {TOKEN}"
        ):
            return self._reply(401)
        m = _PATH_RE.match(self.path)
        if not m:
            return self._reply(404)
        try:
            length = int(self.headers["Content-Length"])
        except (KeyError, TypeError, ValueError):
            return self._reply(411)
        if not 0 <= length <= MAX_BYTES:
            return self._reply(413)
        dst = os.path.join(ROOT, "feeds", m.group(1), m.group(2))
        try:
            atomic_write(dst, self.rfile, length)
        except Exception:
            return self._reply(500)
        self._reply(201)

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
