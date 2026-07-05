"""Offline check: path allowlist + atomic write + live handler (auth/marker/413/traversal).
`python3 uploader/test_server.py` — asserts, no third-party deps."""
import io, os, tempfile, threading, urllib.request, urllib.error
os.environ.setdefault("UPLOAD_TOKEN", "testtoken")
import server  # noqa: E402

T = "abcdefghijklmnopqrstuvwx"  # 24 chars in [a-z2-7]

def test_path_allowlist():
    ok = [f"/feeds/{T}/feed.xml", f"/feeds/{T}/index.html", f"/feeds/{T}/show.json",
          f"/feeds/{T}/artwork.png", f"/feeds/{T}/artwork.jpg", f"/feeds/{T}/EP01-deadbeef.mp3",
          f"/feeds/{T}/EP01-cover-deadbeef.jpg", f"/feeds/{T}/EP12-cover-0a1b2c3d.png"]
    bad = [f"/feeds/{T}/../../etc/passwd", f"/feeds/{T}/evil.sh", "/feeds/SHORT/feed.xml",
           f"/feeds/{T}/feed.xml/..", f"/{T}/feed.xml", f"/feeds/{T}/EP1-deadbeef.mp3",
           f"/feeds/{T}/EP100-deadbeef.mp3", f"/feeds/{T}/EP01-cover-deadbeef.gif",
           f"/feeds/{T}/EP01-cover-deadbeef.mp3"]
    assert all(server._PATH_RE.match(p) for p in ok)
    assert not any(server._PATH_RE.match(p) for p in bad)

def test_atomic_write_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        dst = os.path.join(d, "feeds", T, "feed.xml")
        payload = b"<rss/>" * 1000
        server.atomic_write(dst, io.BytesIO(payload), len(payload))
        assert open(dst, "rb").read() == payload
        assert not any(n.startswith(".tmp-") for n in os.listdir(os.path.dirname(dst)))

def _serve():
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"

def _req(method, url, data=None, token=None, extra_len=None):
    r = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        r.add_header("Authorization", f"Bearer {token}")
    if extra_len is not None:
        r.add_header("Content-Length", str(extra_len))
    try:
        resp = urllib.request.urlopen(r, timeout=5)
        return resp.status, resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers

def test_handler_end_to_end():
    import server as s
    old_root = s.ROOT
    with tempfile.TemporaryDirectory() as d:
        s.ROOT = d
        httpd, base = _serve()
        try:
            # healthz: 帶對 token → 200 + marker;錯 token → 401
            code, hdr = _req("GET", f"{base}/healthz", token="testtoken")
            assert code == 200 and hdr.get("X-Podcast-Uploader") == "1"
            code, _ = _req("GET", f"{base}/healthz", token="WRONG")
            assert code == 401
            # 合法 PUT → 201 且落地
            code, _ = _req("PUT", f"{base}/feeds/{T}/feed.xml", data=b"<rss/>", token="testtoken")
            assert code == 201
            assert open(os.path.join(d, "feeds", T, "feed.xml"), "rb").read() == b"<rss/>"
            # 無 token → 401;白名單外 → 404
            code, _ = _req("PUT", f"{base}/feeds/{T}/feed.xml", data=b"x")
            assert code == 401
            code, _ = _req("PUT", f"{base}/feeds/{T}/evil.sh", data=b"x", token="testtoken")
            assert code == 404
            # 超限 → 413(謊報 Content-Length 超過 MAX)
            big = s.MAX_BYTES + 1
            code, _ = _req("PUT", f"{base}/feeds/{T}/feed.xml", data=b"x", token="testtoken", extra_len=big)
            assert code == 413
        finally:
            httpd.shutdown(); s.ROOT = old_root

def test_put_accepts_pdf_and_html():
    """單集附件(簡報 PDF / 研讀講義 HTML)走跟 mp3 一樣的 EP<n>-<hash8> 命名,應收 201。"""
    import server as s
    old_root = s.ROOT
    with tempfile.TemporaryDirectory() as d:
        s.ROOT = d
        httpd, base = _serve()
        try:
            for name in ("EP01-deadbeef.pdf", "EP01-0a1b2c3d.html"):
                code, _ = _req("PUT", f"{base}/feeds/{T}/{name}", data=b"x", token="testtoken")
                assert code == 201, name
        finally:
            httpd.shutdown(); s.ROOT = old_root

def test_put_still_rejects_non_whitelisted():
    """放寬只加 pdf/html 副檔名,信任邊界不鬆:非白名單副檔名、錯 EP 格式仍 404。"""
    import server as s
    old_root = s.ROOT
    with tempfile.TemporaryDirectory() as d:
        s.ROOT = d
        httpd, base = _serve()
        try:
            for name in ("notes.txt", "evil.pdf", "EP1-deadbeef.pdf"):
                code, _ = _req("PUT", f"{base}/feeds/{T}/{name}", data=b"x", token="testtoken")
                assert code == 404, name
        finally:
            httpd.shutdown(); s.ROOT = old_root

if __name__ == "__main__":
    test_path_allowlist(); test_atomic_write_roundtrip(); test_handler_end_to_end()
    test_put_accepts_pdf_and_html(); test_put_still_rejects_non_whitelisted()
    print("ok")
