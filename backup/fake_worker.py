"""Stand-in for the Worker's admin API, for test_r2_backup_e2e.sh. Stdlib only.

python3 fake_worker.py <port> <request-log> [mode]
  mode ok          : normal
  mode cut_media   : the mp3 download is cut short (curl must fail, run must abort)
  mode drop_put    : the promote PUT connection is dropped (marker kept, run continues)
"""

import email.utils
import http.server
import sys

T = "abcdefghijklmnopqrstuvwx"
MODE = sys.argv[3] if len(sys.argv) > 3 else "ok"
# key -> (bytes, upload epoch); publish order: media, then show.json, then feed.xml
OBJS = {
    f"feeds/{T}/EP01-00000001.mp3": (b"a" * 100, 1767225600),
    f"feeds/{T}/show.json": (b"{}", 1767225601),
    f"feeds/{T}/feed.xml": (b"<rss/>", 1767225602),
    f"feeds/{T}/we|ird.png": (b"x", 1767225603),  # only an out-of-band write could make this
    f"_promote/feeds/{T}/EP02-00000002.mp3": (b"", 1767225604),
}
LOG = open(sys.argv[2], "a")


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - signature of the base class
        pass

    def _send(self, code, body=b"", headers=()):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        LOG.write(f"GET {self.path}\n")
        LOG.flush()
        if self.path == "/_admin/list":
            iso = lambda e: email.utils.formatdate(e, usegmt=True)  # noqa: E731 - any sortable text works
            lines = [f"{iso(e)}|{len(b)}|{e}|{k}\n" for k, (b, e) in OBJS.items()]
            return self._send(200, "".join(lines).encode())
        key = self.path[len("/_admin/object/") :]
        if key not in OBJS:
            return self._send(404)
        body, epoch = OBJS[key]
        if MODE == "cut_media" and key.endswith(".mp3"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:10])
            self.connection.close()
            return
        self._send(200, body, [("Last-Modified", email.utils.formatdate(epoch, usegmt=True))])

    def do_PUT(self):
        LOG.write(f"PUT {self.path}\n")
        LOG.flush()
        if MODE == "drop_put":
            self.connection.close()
            return
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send(201)

    def do_DELETE(self):
        LOG.write(f"DELETE {self.path}\n")
        LOG.flush()
        self._send(204)


http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
