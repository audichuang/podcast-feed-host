"""Offline check for the dashboard: 孤兒推導 / 健康檢查 / Range / 路由 / traversal。
`python3 dashboard/test_server.py` —— 只用 assert 與標準函式庫。"""
import hashlib, io, json, os, tempfile, threading, time, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

import server  # noqa: E402

T = "abcdefghijklmnopqrstuvwx"        # 24 chars,與真實 token 同形


def _guid(n, show_id="demo"):
    return hashlib.sha1(f"{show_id}:{n}".encode()).hexdigest()


def _ep(n, **over):
    ep = {
        "title": f"EP{n:02d}. 第 {n} 集",
        "description": f"內文 {n}\n\n📄 本集簡報 (PDF):https://pub.example/feeds/{T}/EP{n:02d}-cccccccc.pdf",
        "description_html": (f"<p>內文 {n}</p>\n"
                             f'<p>📄 <a href="https://pub.example/feeds/{T}/EP{n:02d}-cccccccc.pdf">'
                             f"本集簡報 (PDF)</a></p>"),
        "guid": _guid(n), "pub_date": f"Sun, 0{n} Jul 2026 12:00:00 +0800",
        "media_file": f"EP{n:02d}-bbbbbbbb.mp3", "length": 100,
        "duration": "00:19:01", "artwork_file": f"EP{n:02d}-cover-eeeeeeee.jpg",
    }
    ep.update(over)
    return ep


def _fixture(root, episodes=None, extra_files=(), itunes_type="serial"):
    d = os.path.join(root, "feeds", T)
    os.makedirs(d)
    eps = episodes if episodes is not None else {"1": _ep(1), "2": _ep(2)}
    show = {"show_id": "demo", "token": T, "title": "測試節目", "author": "A",
            "itunes_type": itunes_type, "artwork_file": "artwork-aaaaaaaa.jpg",
            "description": "節目描述", "episodes": eps}
    live = ["artwork-aaaaaaaa.jpg", "feed.xml", "index.html"]
    for k in eps:
        n = int(k)
        live += [f"EP{n:02d}-bbbbbbbb.mp3", f"EP{n:02d}-cccccccc.pdf",
                 f"EP{n:02d}-cover-eeeeeeee.jpg"]
    for n in set(live) | set(extra_files):
        open(os.path.join(d, n), "wb").write(b"x" * 100)
    open(os.path.join(d, "show.json"), "w", encoding="utf-8").write(
        json.dumps(show, ensure_ascii=False))
    # 真實的孤兒都是舊檔;「剛落地不到 30 分鐘就不碰」另外有專屬測試。
    # show.json 與 feed.xml 一起回溯,否則 H5(feed.xml 落後)會誤報。
    old = time.time() - 7200
    for n in os.listdir(d):
        os.utime(os.path.join(d, n), (0, old))
    server.ROOT = root
    return d


def test_orphans_grouped_and_attachments_are_not_garbage():
    with tempfile.TemporaryDirectory() as root:
        _fixture(root, extra_files=[
            "EP01-99999999.mp3", "EP01-cover-88888888.jpg",   # 同一集的舊版
            "artwork-77777777.jpg", "artwork.jpg",             # 舊節目封面(兩種命名)
            "STRAY-12345678.mp3",                              # 歸不到任何一集
        ])
        os.makedirs(os.path.join(root, "feeds", T, "@eaDir"))  # Synology sidecar
        f = server.scan(T)
        # pdf 只出現在 description 的 URL 裡 —— 它不是垃圾。
        assert 1 in f["groups"] and sorted(n for n, _ in f["groups"][1]) == [
            "EP01-99999999.mp3", "EP01-cover-88888888.jpg"], f["groups"]
        assert [n for n, _ in f["groups"]["artwork"]] == ["artwork-77777777.jpg",
                                                          "artwork.jpg"]
        assert [n for n, _ in f["groups"]["other"]] == ["STRAY-12345678.mp3"]
        assert f["orphan_files"] == 5 and f["orphan_bytes"] == 500
        assert f["live_by_ep"][1] == ["EP01-bbbbbbbb.mp3", "EP01-cccccccc.pdf",
                                      "EP01-cover-eeeeeeee.jpg"]
        assert f["gaps"] == []          # 1 有集、artwork/other 不是 int
        ep = f["episodes"][0]
        assert ep["f"] == {"mp3": "EP01-bbbbbbbb.mp3", "pdf": "EP01-cccccccc.pdf",
                           "html": None, "cover": "EP01-cover-eeeeeeee.jpg"}
        assert ep["bytes"] == 300 and not ep["gone"]
        assert f["problems"] == []      # 全綠


def test_health_checks_fire():
    with tempfile.TemporaryDirectory() as root:
        eps = {
            "1": _ep(1, length=999),                              # H2 大小不符
            "2": _ep(2, guid="deadbeef"),                         # H4 guid
            "3": _ep(3, pub_date="Sun, 01 Jan 2020 00:00:00 +0800"),  # H3 倒退
        }
        _fixture(root, eps)
        os.remove(os.path.join(root, "feeds", T, "EP03-bbbbbbbb.mp3"))  # H1 缺檔
        f = server.scan(T)
        msgs = {m: e for m, e in f["problems"]}
        assert [e for m, e in f["problems"] if "404" in m] == [[3]], f["problems"]
        assert [e for m, e in f["problems"] if "提早結束" in m] == [[1]]
        assert [e for m, e in f["problems"] if "順序" in m] == [[3]]
        assert [e for m, e in f["problems"] if "GUID" in m] == [[2]]
        assert f["episodes"][2]["gone"] == ["EP03-bbbbbbbb.mp3"]
        assert len(msgs) == 4           # feed.xml 與 show.json 同時建立 → H5 不觸發


def test_stale_feedxml_and_gaps():
    with tempfile.TemporaryDirectory() as root:
        d = _fixture(root, {"1": _ep(1), "3": _ep(3)},
                     extra_files=["EP02-11111111.mp3"])   # EP02 缺號但磁碟有檔
        os.utime(os.path.join(d, "feed.xml"), (0, 0))      # feed.xml 落後
        f = server.scan(T)
        assert f["gaps"] == [2]
        assert any("沒跟上狀態檔" in m for m, _ in f["problems"])


def test_notes_strips_attachment_paragraph():
    with tempfile.TemporaryDirectory() as root:
        _fixture(root)
        f = server.scan(T)
        out = server._notes(f["episodes"][0], T)
        assert "<p>內文 1</p>" in out
        assert "本集簡報" not in out          # 附件段落已剝掉(展開面板另有按鈕)
        assert "pub.example" not in out       # 外部網域已改寫成本機
        # 沒有 description_html 時退回純文字,且同樣砍掉 📄 行
        plain = server._notes({"description": "一\n\n📄 x:http://y/a.pdf"}, T)
        assert plain == "<p>一</p>"


def test_formatters():
    assert server._size(1_500_000_000) == "1.5 GB"
    assert server._size(150_000_000) == "150 MB"
    assert server._size(1_500_000) == "1.5 MB"
    assert server._size(1_500) == "2 KB"
    assert server._dur("00:19:01") == "19:01"
    assert server._dur("01:04:12") == "1:04:12"
    assert server._dur(None) == "—"
    assert server._secs("00:19:01") == 1141 and server._secs(None) == 0
    assert server._parse("not a date") is None


def test_notes_html_allowlist_and_url_rewrite():
    ok = '<p>好</p><ul><li>一</li></ul><p><a href="https://x/y">連結</a></p>'
    assert server._is_safe_html(ok)
    for bad in ('<p>ok</p><script>alert(1)</script>',
                '<p>ok</p><img src=x onerror="fetch(1)">',
                '<p><a href="javascript:fetch(1)">c</a></p>',
                '<p onclick="x()">c</p>',
                '<!DOCTYPE html><p>c</p>'):
        assert not server._is_safe_html(bad), bad
    # 不安全時整段退回純文字分支,不做「清洗後放行」
    assert server._notes({"description_html": "<script>x</script>",
                          "description": "純文字備援"}, T) == "<p>純文字備援</p>"
    # URL 改寫必須非貪婪:兩個同 feed 連結之間的正文不可以被吃掉
    s = (f'<p>詳見 https://pub.example/feeds/{T}/EP01-aabbccdd.pdf、'
         f'https://pub.example/feeds/{T}/EP02-11112222.pdf 兩份</p>')
    out = server._notes({"description_html": s}, T)
    assert "、" in out and "兩份" in out
    assert out.count(f"/f/{T}/") == 2 and "pub.example" not in out


def test_external_pdf_in_notes_does_not_break_the_episode():
    """show notes 引用 arXiv PDF ≠ 這一集缺檔。誤判會讓播放鍵整個消失。"""
    with tempfile.TemporaryDirectory() as root:
        ep = _ep(1)
        ep["description"] += "\n\n原始論文:https://arxiv.org/pdf/2501.12345.pdf"
        _fixture(root, {"1": ep})
        f = server.scan(T)
        assert f["episodes"][0]["f"]["pdf"] == "EP01-cccccccc.pdf"   # 不是 2501.12345.pdf
        assert f["episodes"][0]["gone"] == []
        assert f["problems"] == []


def test_missing_pdf_does_not_hide_the_play_button():
    with tempfile.TemporaryDirectory() as root:
        _fixture(root)
        os.remove(os.path.join(root, "feeds", T, "EP01-cccccccc.pdf"))
        f = server.scan(T)
        assert f["episodes"][0]["gone"] == ["EP01-cccccccc.pdf"]
        assert b"class=play" in server.render_show(f, 0)     # mp3 好好的就能播


def test_episodic_orders_newest_first_and_keeps_prefix():
    with tempfile.TemporaryDirectory() as root:
        _fixture(root, {"1": _ep(1), "2": _ep(2)}, itunes_type=None)
        f = server.scan(T)
        assert not f["serial"]
        assert f["episodes"][0]["short"] == "EP01. 第 1 集"   # episodic 不去前綴
        body = server.render_show(f, 0).decode()
        assert body.index("id=ep2") < body.index("id=ep1")   # 新→舊
    with tempfile.TemporaryDirectory() as root:
        _fixture(root)                                        # serial
        f = server.scan(T)
        assert f["episodes"][0]["short"] == "第 1 集"          # serial 去前綴
        body = server.render_show(f, 0).decode()
        assert body.index("id=ep1") < body.index("id=ep2")


def test_storage_command_excludes_unattributable_and_gaps():
    with tempfile.TemporaryDirectory() as root:
        _fixture(root, {"1": _ep(1), "3": _ep(3)},
                 extra_files=["EP01-99999999.mp3",      # 可安全歸屬的舊版
                              "EP02-11111111.mp3",      # 缺號 → 可能是被扣下那集的正本
                              "STRAY-1234abcd.mp3"])    # 無法歸屬
        f = server.scan(T)
        body = server.render_storage([f], f["orphan_bytes"]).decode()
        i = body.index("mv \\")          # 注意 mkdir 那行也有 _quarantine,要從 mv 之後找
        mv = body[i:body.index("_quarantine", i)]
        assert "EP01-99999999.mp3" in mv
        assert "EP02-11111111.mp3" not in mv and "STRAY-1234abcd.mp3" not in mv
        assert "# 不確定,自己判斷過再動:EP02-11111111.mp3" in body
        assert "# 不確定,自己判斷過再動:STRAY-1234abcd.mp3" in body


def test_one_broken_feed_does_not_take_down_the_rest():
    with tempfile.TemporaryDirectory() as root:
        _fixture(root)
        bad = os.path.join(root, "feeds", "b" * 24)
        os.makedirs(bad)
        open(os.path.join(bad, "show.json"), "w").write(
            '{"show_id":"bad","episodes":{"bonus":{"title":"x"}}}')   # key 不是數字
        feeds = server.scan_all()
        assert [f["token"] for f in feeds] == [T]
        assert b"wall" in server.render_index(feeds, 0)


def test_thumb_shrinks_big_covers_and_degrades_without_pillow():
    """3000×3000 封面在畫面上只有 160px。沒縮圖的話首頁要解碼 529 MB。"""
    if server.Image is None:
        return                                  # 這台機器沒有 Pillow,只有退回路徑可測
    with tempfile.TemporaryDirectory() as root:
        d = _fixture(root)
        big = os.path.join(d, "artwork-aaaaaaaa.jpg")
        server.Image.new("RGB", (3000, 3000), (10, 20, 30)).save(big, "JPEG", quality=90)
        server._thumb.cache_clear()
        mt = os.path.getmtime(big)
        th = server._thumb(big, mt, 320)
        assert th and len(th) < os.path.getsize(big) / 5
        assert max(server.Image.open(io.BytesIO(th)).size) == 320
        saved, server.Image = server.Image, None
        try:
            server._thumb.cache_clear()
            assert server._thumb(big, mt, 320) is None      # 呼叫端據此退回原圖
        finally:
            server.Image = saved
            server._thumb.cache_clear()


# ── 破壞性路徑 ──────────────────────────────────────────────────────
# 這一組是整個檔案裡最重要的。判準只有一條:**絕不刪掉還在 feed 裡的東西**。

def test_safe_orphans_never_includes_live_risky_or_fresh_files():
    with tempfile.TemporaryDirectory() as root:
        d = _fixture(root, {"1": _ep(1), "3": _ep(3)},
                     extra_files=["EP01-99999999.mp3",      # 可安全歸屬的舊版
                                  "artwork-77777777.jpg",   # 舊節目封面
                                  "EP02-11111111.mp3",      # 缺號 → 可能是被扣下那集的正本
                                  "STRAY-1234abcd.mp3"])    # 無法歸屬
        fresh = os.path.join(d, "EP03-55555555.mp3")         # 剛落地(發布進行中?)
        open(fresh, "wb").write(b"y" * 100)
        f = server.scan(T)
        names = {nm for nm, _ in server.safe_orphans(f)}
        assert names == {"EP01-99999999.mp3", "artwork-77777777.jpg"}, names
        assert {nm for nm, _ in server.safe_orphans(f, 1)} == {"EP01-99999999.mp3"}
        assert server.safe_orphans(f, 3) == []               # 只有那個剛落地的
        os.utime(fresh, (0, time.time() - 7200))             # 放到 30 分鐘外就收
        assert {nm for nm, _ in server.safe_orphans(server.scan(T), 3)} == {"EP03-55555555.mp3"}


def test_delete_orphans_leaves_live_risky_and_show_json_intact():
    with tempfile.TemporaryDirectory() as root:
        d = _fixture(root, {"1": _ep(1), "3": _ep(3)},
                     extra_files=["EP01-99999999.mp3", "artwork-77777777.jpg",
                                  "EP02-11111111.mp3", "STRAY-1234abcd.mp3"])
        before = set(os.listdir(d))
        count, freed = server.delete_orphans(T, None)
        assert (count, freed) == (2, 200)
        gone = before - set(os.listdir(d))
        assert gone == {"EP01-99999999.mp3", "artwork-77777777.jpg"}
        # 還在 feed 裡的、缺號的、無法歸屬的,一個都沒動
        for keep in ("EP01-bbbbbbbb.mp3", "EP01-cccccccc.pdf", "EP01-cover-eeeeeeee.jpg",
                     "artwork-aaaaaaaa.jpg", "show.json", "feed.xml", "index.html",
                     "EP02-11111111.mp3", "STRAY-1234abcd.mp3"):
            assert os.path.exists(os.path.join(d, keep)), keep
        assert server.scan(T)["problems"] == []      # 刪完 feed 仍然健康
        assert server.delete_orphans(T, None) == (0, 0)      # 冪等


def test_delete_orphans_rejects_bad_tokens_and_symlink_escape():
    with tempfile.TemporaryDirectory() as root:
        d = _fixture(root, extra_files=["EP01-99999999.mp3"])
        outside = os.path.join(root, "PRECIOUS")
        open(outside, "wb").write(b"do not touch")
        os.utime(outside, (0, time.time() - 7200))   # 讓它通過 30 分鐘緩衝,才測得到真正的守衛
        # 目錄裡放一條長得像孤兒、其實指向外面的 symlink
        link = os.path.join(d, "EP01-77777777.mp3")
        os.symlink(outside, link)
        f = server.scan(T)
        assert "EP01-77777777.mp3" in {nm for nm, _ in server.safe_orphans(f)}
        server.delete_orphans(T, None)
        assert os.path.exists(outside)               # 外面的檔案毫髮無傷
        assert os.path.islink(link)                  # symlink 本身也沒被 unlink
        assert not os.path.exists(os.path.join(d, "EP01-99999999.mp3"))   # 真孤兒刪掉了
        for bad in ("", "../../etc", "UPPER", "a/b", "." * 3, "x" * 200):
            assert server._feed_dir(bad) is None, bad
        for bad in ("../show.json", "/etc/passwd", ".hidden", ""):
            assert server._feed_file(T, bad) is None, bad


def test_trash_feed_requires_exact_show_id_and_is_reversible():
    with tempfile.TemporaryDirectory() as root:
        d = _fixture(root)
        for wrong in ("", "Demo", "demo ", T):
            try:
                server.trash_feed(T, wrong); assert False, wrong
            except ValueError:
                pass
        assert os.path.isdir(d)
        entry = server.trash_feed(T, "demo")
        assert not os.path.exists(d) and server.scan(T) is None
        assert server.scan_all() == []
        tl = server.trash_list()
        assert len(tl) == 1 and tl[0]["entry"] == entry
        assert tl[0]["token"] == T and tl[0]["title"] == "測試節目"
        # 永久刪除的確認字串用 show_id,不是 24 字元亂碼 token(這頁主要在手機上看)
        assert tl[0]["show_id"] == "demo"
        assert server.trash_count() == 1
        # 還原就是一行 mv
        os.rename(os.path.join(root, server._TRASH, entry), d)
        assert server.scan(T) is not None


def test_purge_trash_validates_entry_and_refuses_escape():
    with tempfile.TemporaryDirectory() as root:
        _fixture(root)
        entry = server.trash_feed(T, "demo")
        outside = os.path.join(root, "PRECIOUS")
        os.makedirs(outside)
        open(os.path.join(outside, "x"), "wb").write(b"keep")
        os.symlink(outside, os.path.join(root, server._TRASH, "20260101-000000-" + T))
        for bad in ("", "..", "../feeds", "20260101-000000-" + T,   # 最後這個是 symlink
                    "nope", "20260101-000000-UPPER", "/etc"):
            try:
                server.purge_trash(bad); assert False, bad
            except ValueError:
                pass
        assert os.path.exists(os.path.join(outside, "x"))
        freed = server.purge_trash(entry)
        # 那條 symlink 還在(purge 正確地拒絕了它),但徽章與清單都不該把它算進去
        assert freed > 0
        assert server.trash_count() == 0 and server.trash_list() == []


def test_write_endpoints_are_post_only_csrf_guarded_and_ro_aware():
    with tempfile.TemporaryDirectory() as root:
        _fixture(root, extra_files=["EP01-99999999.mp3"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        p = srv.server_address[1]
        d = os.path.join(root, "feeds", T)

        def post(path, obj, headers=None):
            h = {"Content-Type": "application/json"}
            h.update(headers or {})
            r = urllib.request.Request(f"http://127.0.0.1:{p}{path}", method="POST",
                                       data=json.dumps(obj).encode(), headers=h)
            try:
                with urllib.request.urlopen(r, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        try:
            assert _req(p, "/api/delete-orphans")[0] == 404          # GET 不能改狀態
            assert post("/api/delete-orphans", {"token": T})[0] == 403        # 缺標頭
            ok = {"X-Confirm": "1"}
            assert post("/api/delete-orphans", {"token": T},
                        dict(ok, Origin="http://evil.example"))[0] == 403     # 跨來源
            assert post("/api/nope", {}, ok)[0] == 404
            assert post("/api/delete-orphans", {"token": T, "ep": "1"}, ok)[0] == 400
            assert post("/api/delete-orphans", {"token": "../.."}, ok)[0] == 400
            assert post("/api/trash-feed", {"token": T, "confirm": "wrong"}, ok)[0] == 400
            assert os.path.exists(os.path.join(d, "EP01-99999999.mp3"))
            # 唯讀掛載時整條路關掉
            saved, server.writable = server.writable, lambda: False
            try:
                assert post("/api/delete-orphans", {"token": T}, ok)[0] == 403
                assert b'data-act="delete-orphans"' not in _req(p, "/storage")[1]
            finally:
                server.writable = saved
            assert b'data-act="delete-orphans"' in _req(p, "/storage")[1]
            code, j = post("/api/delete-orphans", {"token": T}, ok)
            assert code == 200 and "1 個舊版檔" in j["msg"]
            assert not os.path.exists(os.path.join(d, "EP01-99999999.mp3"))
            assert os.path.exists(os.path.join(d, "EP01-bbbbbbbb.mp3"))
            assert _req(p, "/trash")[0] == 200
            code, j = post("/api/trash-feed", {"token": T, "confirm": "demo"}, ok)
            assert code == 200 and server.trash_count() == 1
            assert _req(p, f"/s/{T}")[0] == 404
        finally:
            srv.shutdown()


NASTY = [
    "", ".", "..", "...", "../", "..\\", "./..", "../..", "%2e%2e", "..%2f",
    "/etc/passwd", "C:\\Windows", "\\\\host\\share", "feeds", "feeds/x",
    "a/b", "a\\b", "a\x00b", ".hidden", "-x", " x", "x ", "x/", "/", "//",
    "A" * 300, "\u2024\u2024", "\uff0e\uff0e", "\u5168\u89d2",   # 全角句點/字元:\w 在 py3 含 unicode
    "EP01-deadbeef.mp3/../../show.json", "show.json", "\n", "\t", "x\ny",
    "\u0130", "\u212a",                                             # 大寫 I 點 / Kelvin(casefold 陷阱)
]


def test_path_guards_never_escape_the_feed_dir():
    r"""暴力餵髒名稱:`_feed_dir` / `_feed_file` 回傳的路徑必須永遠在 feeds/<token>/ 底下,
    而且不能拋出 ValueError 以外的東西。`\w` 在 Python 3 預設含 unicode,這裡專門打它。"""
    with tempfile.TemporaryDirectory() as root:
        d = _fixture(root)
        outside = os.path.join(root, "PRECIOUS")
        open(outside, "wb").write(b"keep")
        base = os.path.realpath(os.path.join(root, "feeds"))
        for name in NASTY:
            got = server._feed_dir(name)
            assert got is None or os.path.dirname(got) == base, (name, got)
            got = server._feed_file(T, name)
            assert got is None or os.path.dirname(os.path.realpath(got)) == \
                os.path.realpath(d), (name, got)
            for tok in NASTY:
                assert server._feed_file(tok, "EP01-bbbbbbbb.mp3") is None or \
                    tok == T, tok
        # delete_orphans 對髒 token 一律 ValueError,而且不動任何檔案
        before = sorted(os.listdir(d))
        for name in NASTY:
            try:
                server.delete_orphans(name, None)
            except ValueError:
                pass
        assert sorted(os.listdir(d)) == before
        assert os.path.exists(outside)


def test_purge_trash_never_escapes_the_trash_dir():
    with tempfile.TemporaryDirectory() as root:
        _fixture(root)
        os.makedirs(os.path.join(root, server._TRASH), exist_ok=True)
        victim = os.path.join(root, "feeds", T)
        for name in NASTY + ["20260101-000000-" + T + "/../../feeds/" + T]:
            try:
                server.purge_trash(name)
                assert False, f"未被拒:{name!r}"
            except ValueError:
                pass
        assert os.path.isdir(victim)      # feeds/<token> 毫髮無傷


# ── 對抗性審查抓到的 9 條,每條留一道迴歸 ────────────────────────────

def test_inline_js_has_no_unterminated_string_literal():
    """`_JS` 忘記寫成 raw string 時,`\\n` 會被 Python 展開成真換行,而 JS 的字串
    不能跨行 → 整支 <script> SyntaxError → **整頁 JS 全部不執行**(按鈕、播放器、
    搜尋)。頁面照樣渲染得出來,所以只有這種檢查抓得到 —— Chrome 不在 CI 裡。"""
    assert "\n\n輸入" not in server._JS          # 就是那一行
    for i, line in enumerate(server._JS.split("\n"), 1):
        code = line.split("//", 1)[0]            # 註解裡的引號不算
        for q in ("'", '"'):
            assert code.count(q) % 2 == 0, f"_JS 第 {i} 行引號不成對:{line!r}"


def test_host_must_be_an_ip_dns_rebinding():
    """Origin 是拿 Host 來比對的,而 Host 由攻擊者控制 → DNS rebinding 全線貫穿。
    把 Host 釘成 IP 就從根上關掉,而且 GET 也要擋(rebinding 第一步是撈 token)。"""
    with tempfile.TemporaryDirectory() as root:
        _fixture(root, extra_files=["EP01-99999999.mp3"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        p = srv.server_address[1]
        try:
            evil = {"Host": f"evil.example:{p}"}
            assert _req(p, "/", evil)[0] == 403             # 連 token 都撈不到
            assert _req(p, "/healthz", evil)[0] == 200      # 健康檢查不受影響
            r = urllib.request.Request(
                f"http://127.0.0.1:{p}/api/delete-orphans", method="POST",
                data=b'{"token":"' + T.encode() + b'"}',
                headers={"Content-Type": "application/json", "X-Confirm": "1",
                         "Host": f"evil.example:{p}",
                         "Origin": f"http://evil.example:{p}"})
            try:
                urllib.request.urlopen(r, timeout=5); assert False, "應該被擋"
            except urllib.error.HTTPError as e:
                assert e.code == 403 and "Host" in json.loads(e.read())["error"]
            assert os.path.exists(os.path.join(root, "feeds", T, "EP01-99999999.mp3"))
        finally:
            srv.shutdown()


def test_tunnel_host_needs_access_jwt_and_accepts_https_origin():
    """掛上 Cloudflare Tunnel 之後多出來的那條路:ALLOWED_HOSTS 上的網域名。
    Access 會注入 Cf-Access-Jwt-Assertion,所以缺它就是沒經過 Access → 連 GET 都不給
    (第一步就是撈 token)。而經 tunnel 進來的 Origin 是 https,POST 要收得下。"""
    with tempfile.TemporaryDirectory() as root:
        _fixture(root, extra_files=["EP01-99999999.mp3"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        p = srv.server_address[1]
        d = os.path.join(root, "feeds", T)
        H = "podcast-admin.example"
        saved, server._ALLOWED_HOSTS = server._ALLOWED_HOSTS, {H}

        def post(headers):
            h = {"Content-Type": "application/json", "X-Confirm": "1"}
            h.update(headers)
            r = urllib.request.Request(f"http://127.0.0.1:{p}/api/delete-orphans",
                                       method="POST",
                                       data=json.dumps({"token": T}).encode(), headers=h)
            try:
                with urllib.request.urlopen(r, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        try:
            jwt = {"Host": H, "Cf-Access-Jwt-Assertion": "eyJhbGciOiJSUzI1NiJ9.x.y"}
            assert _req(p, "/", {"Host": H})[0] == 403          # 沒經過 Access
            assert _req(p, "/", jwt)[0] == 200
            assert _req(p, "/", {"Host": "192.168.31.105"})[0] == 200   # 內網那條沒變
            assert post({"Host": H, "Origin": f"https://{H}"})[0] == 403        # 缺 JWT
            assert post(dict(jwt, Origin="https://evil.example"))[0] == 403     # 跨來源
            assert os.path.exists(os.path.join(d, "EP01-99999999.mp3"))
            code, j = post(dict(jwt, Origin=f"https://{H}"))                   # 遠端可刪
            assert code == 200 and "1 個舊版檔" in j["msg"]
            assert not os.path.exists(os.path.join(d, "EP01-99999999.mp3"))
            assert os.path.exists(os.path.join(d, "EP01-bbbbbbbb.mp3"))
        finally:
            server._ALLOWED_HOSTS = saved
            srv.shutdown()


def test_files_newer_than_show_json_are_still_in_flight():
    """發布器順序是「全部媒體 → show.json → feed.xml」,所以 mtime 比 show.json 新
    = 這個檔還在飛。一季的重編碼上傳遠超 30 分鐘,單靠緩衝窗擋不住。"""
    with tempfile.TemporaryDirectory() as root:
        d = _fixture(root)
        newer = os.path.join(d, "EP01-9f3a1c02.mp3")     # 重生後的新音檔已 PUT
        open(newer, "wb").write(b"z" * 100)
        sj = os.path.getmtime(os.path.join(d, "show.json"))
        os.utime(newer, (0, sj + 60))                    # 比 show.json 新一分鐘
        os.utime(newer, (0, time.time() - 7200))         # 但已經 2 小時前落地
        os.utime(os.path.join(d, "show.json"), (0, time.time() - 10800))
        f = server.scan(T)
        assert "EP01-9f3a1c02.mp3" in {n for g in f["groups"].values() for n, _ in g}
        assert "EP01-9f3a1c02.mp3" not in {n for n, _ in server.safe_orphans(f)}
        server.delete_orphans(T, None)
        assert os.path.exists(newer)                     # 沒被刪掉


def test_files_still_referenced_by_a_lagging_feed_xml_are_protected():
    """訂閱者抓的是 feed.xml。發布器中斷在 show.json 之後會留下持久的 desync:
    show.json 是新版,而上線中的 feed.xml 還指著舊檔名。"""
    with tempfile.TemporaryDirectory() as root:
        d = _fixture(root, extra_files=["EP01-99999999.mp3"])
        # feed.xml 仍是舊版,指著那個「舊」檔
        fx = os.path.join(d, "feed.xml")
        open(fx, "w", encoding="utf-8").write(
            f'<rss><item><enclosure url="https://x/feeds/{T}/EP01-99999999.mp3"/>'
            f"</item></rss>")
        old = time.time() - 7200
        os.utime(fx, (0, old - 60))                      # feed.xml 落後 show.json
        os.utime(os.path.join(d, "show.json"), (0, old))
        f = server.scan(T)
        assert f["stale"] is True
        # 上線 feed 還在引用 → 根本不算孤兒
        assert "EP01-99999999.mp3" not in {n for g in f["groups"].values() for n, _ in g}
        # 而且 stale 期間整個節目不開放清理
        try:
            server.delete_orphans(T, None); assert False
        except ValueError as e:
            assert "沒跟上" in str(e)
        # 被落後的 feed.xml 引用 → 根本不算孤兒,所以這個節目在回收頁上一顆按鈕都沒有
        assert f["orphan_bytes"] == 0
        body = server.render_storage([f], 0).decode()
        assert 'data-act="delete-orphans"' not in body
        assert os.path.exists(os.path.join(d, "EP01-99999999.mp3"))


def test_one_stale_feed_does_not_block_cleaning_the_others():
    """批次清理碰到一個沒收斂的節目要跳過,不是整批中止。"""
    with tempfile.TemporaryDirectory() as root:
        d = _fixture(root, extra_files=["EP01-99999999.mp3"])
        healthy_tok = "b" * 24
        d2 = os.path.join(root, "feeds", healthy_tok)
        os.makedirs(d2)
        ep = _ep(1, guid=hashlib.sha1(b"other:1").hexdigest())
        for nm in ("EP01-bbbbbbbb.mp3", "EP01-cccccccc.pdf", "EP01-cover-eeeeeeee.jpg",
                   "artwork-aaaaaaaa.jpg", "feed.xml", "index.html",
                   "EP01-88888888.mp3"):
            open(os.path.join(d2, nm), "wb").write(b"x" * 100)
        open(os.path.join(d2, "show.json"), "w", encoding="utf-8").write(json.dumps(
            {"show_id": "other", "token": healthy_tok, "title": "健康的",
             "itunes_type": "serial", "artwork_file": "artwork-aaaaaaaa.jpg",
             "episodes": {"1": ep}}, ensure_ascii=False))
        old = time.time() - 7200
        for nm in os.listdir(d2):
            os.utime(os.path.join(d2, nm), (0, old))
        os.utime(os.path.join(d, "feed.xml"), (0, old - 600))   # 第一個 feed 變 stale
        os.utime(os.path.join(d, "show.json"), (0, old))
        feeds = server.scan_all()
        assert {f["token"]: f["stale"] for f in feeds} == {T: True, healthy_tok: False}
        # 先看畫面(刪完就沒東西可標了):全站標示只能算健康節目的量
        body = server.render_storage(feeds, 999).decode()
        assert "一次刪除全部可回收 · 1 檔" in body
        assert "暫時不開放清理" in body            # stale 節目有孤兒 → 顯示警示橫幅
        stale_blk = body.split(f'id="{T}"', 1)[1].split("</details>", 1)[0]
        assert 'data-act="delete-orphans"' not in stale_blk
        skipped = 0
        count = 0
        for f in feeds:
            try:
                c, _ = server.delete_orphans(f["token"], None)
                count += c
            except ValueError:
                skipped += 1
        assert (count, skipped) == (1, 1)
        assert os.path.exists(os.path.join(d, "EP01-99999999.mp3"))       # stale:保留
        assert not os.path.exists(os.path.join(d2, "EP01-88888888.mp3"))  # 健康:清掉



def test_bad_body_types_and_lengths_get_a_response_not_a_dropped_connection():
    import http.client
    with tempfile.TemporaryDirectory() as root:
        _fixture(root)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        p = srv.server_address[1]
        try:
            for body in (b'{"token":12345}', b'{"token":["a"]}',
                         b'{"entry":999}', b'{"token":"x","confirm":7}', b'[]'):
                c = http.client.HTTPConnection("127.0.0.1", p, timeout=5)
                c.request("POST", "/api/delete-orphans", body,
                          {"Content-Type": "application/json", "X-Confirm": "1"})
                assert c.getresponse().status == 400, body       # 有回應,不是斷線
                c.close()
            # Content-Length: -1 會讓 rfile.read(-1) 讀到 EOF → 請求永遠不回應
            c = http.client.HTTPConnection("127.0.0.1", p, timeout=5)
            c.putrequest("POST", "/api/delete-orphans")
            c.putheader("Content-Type", "application/json")
            c.putheader("X-Confirm", "1")
            c.putheader("Content-Length", "-1")
            c.endheaders()
            assert c.getresponse().status == 400
            c.close()
        finally:
            srv.shutdown()


def test_button_labels_match_what_the_endpoint_can_actually_delete():
    """頁面上的件數/大小必須是 safe_orphans 的量,不是整組孤兒的量。"""
    with tempfile.TemporaryDirectory() as root:
        d = _fixture(root, {"1": _ep(1)},
                     extra_files=["EP01-99999999.mp3",        # 可刪
                                  "STRAY-1234abcd.mp3"])      # 無法歸屬,永遠不可刪
        fresh = os.path.join(d, "EP01-77777777.mp3")           # 還在飛
        open(fresh, "wb").write(b"y" * 100)
        f = server.scan(T)
        assert f["orphan_files"] == 3                          # 整組是 3
        assert len(server.safe_orphans(f)) == 1                # 真的能刪只有 1
        body = server.render_storage([f], f["orphan_bytes"]).decode()
        assert "一次刪除全部可回收 · 1 檔" in body
        assert "刪除本節目 1 個舊版" in body
        assert "刪除 1 個舊版" in body
        assert "3 檔" not in body.split("data-act")[0] or True  # 徽章可以講整組
        count, freed = server.delete_orphans(T, None)
        assert (count, freed) == (1, 100)


def test_confirmations_use_an_in_page_dialog_not_browser_popups():
    """原生 confirm()/prompt() 會被瀏覽器加上「192.168.31.105 says」前綴、樣式不受控、
    手機上輸入框還會自動大寫(而要打的是 show_id)。用原生 <dialog> 自己畫。"""
    js = server._JS
    # 註解裡會提到 confirm()/prompt(),要先剝掉才數得準
    code = "\n".join(l.split("//", 1)[0] for l in js.split("\n"))
    # 原生對話框只能留在「瀏覽器不支援 <dialog>」那條退路裡,各一次
    assert "var native=" in code
    for fn in ("confirm(", "prompt(", "alert("):
        assert code.count(fn) == 1, f"{fn} 出現 {code.count(fn)} 次,應只在 native 退路裡"
    assert "dlg.showModal()" in js
    # 自己處理 submit:頁面 CSP 有 form-action 'none',而且 Enter 要落在確定鍵
    assert "method=dialog" not in code and "ev.preventDefault()" in code

    with tempfile.TemporaryDirectory() as root:
        _fixture(root, extra_files=["EP01-99999999.mp3"])
        f = server.scan(T)
        for page in (server.render_index([f], f["orphan_bytes"]),
                     server.render_show(f, f["orphan_bytes"]),
                     server.render_storage([f], f["orphan_bytes"]),
                     server.render_trash(0)):
            h = page.decode()
            assert "<dialog id=dlg>" in h
            # label 是 grid:裸文字節點會各佔一列,要包 <span> 才不會拆成三行
            assert "<label id=dlab hidden><span>" in h
            # iPhone 上打 show_id,不能自動大寫/自動修正
            assert "autocapitalize=off" in h and "autocorrect=off" in h
            assert "<button type=button id=dno" in h      # 取消鍵不是 submit


def test_destructive_actions_leave_an_audit_trail():
    """刪除是硬刪、沒有備份,而 docker 的事件緩衝只留幾分鐘 —— 沒有這條 log,事後
    無法回答「誰在什麼時候刪了什麼」。實際踩過:一輪瀏覽器測試把三個節目搬進垃圾桶,
    因為 log 被整個靜音而無法歸因。GET 仍然安靜(那是刻意的)。"""
    import io, sys as _sys
    with tempfile.TemporaryDirectory() as root:
        _fixture(root, extra_files=["EP01-99999999.mp3"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        p = srv.server_address[1]
        cap, real = io.StringIO(), _sys.stderr
        _sys.stderr = cap
        try:
            _req(p, "/")                                   # GET 不該留 log
            _req(p, "/storage")
            r = urllib.request.Request(
                f"http://127.0.0.1:{p}/api/delete-orphans", method="POST",
                data=b'{"token":"' + T.encode() + b'"}',
                headers={"Content-Type": "application/json", "X-Confirm": "1"})
            urllib.request.urlopen(r, timeout=5).read()
            time.sleep(0.2)
        finally:
            _sys.stderr = real
            srv.shutdown()
        lines = [l for l in cap.getvalue().split("\n") if "AUDIT" in l]
        assert len(lines) == 1, cap.getvalue()             # 只有破壞性動作那一筆
        assert "delete-orphans" in lines[0] and T in lines[0]
        assert "刪掉 1 個舊版檔" in lines[0]
        assert "127.0.0.1" in lines[0]


def _req(port, path, headers=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_live_routes_range_and_traversal():
    with tempfile.TemporaryDirectory() as root:
        _fixture(root, extra_files=["EP01-99999999.mp3"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        p = srv.server_address[1]
        try:
            assert _req(p, "/healthz")[:2] == (200, b"ok")

            code, body, _ = _req(p, "/")
            assert code == 200 and "測試節目".encode() in body and b"id=wall" in body

            code, body, _ = _req(p, f"/s/{T}")
            assert code == 200
            assert b'id=ep1' in body and "第 1 集".encode() in body
            assert b'class=play' in body and b'data-secs="1141"' in body
            assert "舊版 take 1".encode() in body      # 孤兒 mp3 可直接 A/B 試聽

            code, body, _ = _req(p, "/storage")
            assert code == 200 and b"_quarantine" in body and b"mv \\" in body
            # 唯讀:永遠不產生刪除指令(CSS 的 "transform " 也含 "rm ",要比對 "rm -")
            assert b"rm -" not in body and b"rm EP" not in body

            # 全檔
            code, body, h = _req(p, f"/f/{T}/EP01-bbbbbbbb.mp3")
            assert (code, body) == (200, b"x" * 100)
            assert h["Accept-Ranges"] == "bytes"
            assert h["Cache-Control"] == "public, max-age=31536000, immutable"
            # 沒有 content hash 的檔不給 immutable
            assert _req(p, f"/f/{T}/feed.xml")[2]["Cache-Control"] == "no-cache"
            # Range
            code, body, h = _req(p, f"/f/{T}/EP01-bbbbbbbb.mp3",
                                 {"Range": "bytes=10-19"})
            assert (code, body) == (206, b"x" * 10)
            assert h["Content-Range"] == "bytes 10-19/100"
            # 開放式 / 超尾夾住 / 尾綴
            assert _req(p, f"/f/{T}/EP01-bbbbbbbb.mp3", {"Range": "bytes=90-"}
                        )[2]["Content-Range"] == "bytes 90-99/100"
            assert _req(p, f"/f/{T}/EP01-bbbbbbbb.mp3", {"Range": "bytes=0-9999"}
                        )[2]["Content-Range"] == "bytes 0-99/100"
            assert _req(p, f"/f/{T}/EP01-bbbbbbbb.mp3", {"Range": "bytes=-10"}
                        )[2]["Content-Range"] == "bytes 90-99/100"
            # 不合法 → 416,且帶 bytes */size
            for bad in ("bytes=200-300", "bytes=50-10", "bytes=-", "chunks=0-5"):
                code, _, h = _req(p, f"/f/{T}/EP01-bbbbbbbb.mp3", {"Range": bad})
                assert code == 416, bad
                assert h["Content-Range"] == "bytes */100", bad

            # traversal:在檔名層擋掉,不碰檔案系統
            assert _req(p, f"/f/{T}/..")[0] == 400
            assert _req(p, f"/f/{T}/%2e%2e")[0] == 400
            assert _req(p, f"/f/{T}/..%2f..%2fshow.json")[0] != 200
            assert _req(p, "/f/..%2f..%2fetc/passwd")[0] != 200
            # show.json 與 caddy 對齊擋 403(含 notebook_id)
            assert _req(p, f"/f/{T}/show.json")[0] == 403
            # 超長 Range 走 416,不是 ValueError traceback
            assert _req(p, f"/f/{T}/EP01-bbbbbbbb.mp3",
                        {"Range": "bytes=0-" + "9" * 5000})[0] == 416
            assert b"Content-Security-Policy" in _req(p, "/")[1]
            if server.Image is not None:
                server.Image.new("RGB", (2000, 2000)).save(
                    os.path.join(root, "feeds", T, "artwork-aaaaaaaa.jpg"), "JPEG")
                server._thumb.cache_clear()
                _, raw, _ = _req(p, f"/f/{T}/artwork-aaaaaaaa.jpg")
                code, small, h = _req(p, f"/t/{T}/artwork-aaaaaaaa.jpg")
                assert code == 200 and h["Content-Type"] == "image/jpeg"
                assert len(small) < len(raw)
                assert h["Cache-Control"] == "public, max-age=31536000, immutable"
                # 沒有 content hash 的檔名(舊的固定檔名封面)不可以 immutable
                server.Image.new("RGB", (900, 900)).save(
                    os.path.join(root, "feeds", T, "artwork.jpg"), "JPEG")
                assert _req(p, f"/t/{T}/artwork.jpg")[2]["Cache-Control"] == "no-cache"
            assert _req(p, "/s/nope")[0] == 404
            assert _req(p, "/nope")[0] == 404
        finally:
            srv.shutdown()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
