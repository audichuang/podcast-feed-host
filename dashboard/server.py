#!/usr/bin/env python3
"""Podcast feed 管理後端 —— 「我到底有什麼?」

掛載 caddy 對外服務的**同一個目錄**(唯讀),從每個 `feeds/<token>/show.json`
(發布器自己寫的權威狀態檔)推導出全部節目與單集,渲染成一個看起來像真的 podcast
App 的管理介面:封面牆 → 節目頁 → 單集列表 → show notes → 底部常駐播放器。
沒有資料庫,不解析 feed.xml —— show.json 已經是權威,不需要任何人記得同步。

**LAN-ONLY。** 這個頁面把每一個 feed token 列出來,而 token 就是那些未公開列出的
feed 的唯一存取控制。掛上 Cloudflare Tunnel 等於一次公開全部。像 uploader 一樣
綁 NAS 的 LAN IP。

**唯讀。** uploader 永不刪檔,所以同一集重生後,舊的 content-hash 版本會留在磁碟上、
仍然公開可讀、但沒有人訂閱得到。這裡把它們數出來、列出來、產生 `mv` 到隔離區的
指令文字 —— 但不代刪。孤兒是用「檔名有沒有出現在 show.json 全文」推導的,推導會錯,
所以給 `mv` 不給 `rm`。
"""
from __future__ import annotations

import functools
import hashlib
import html
import ipaddress
import io
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:                                   # 唯一的非標準函式庫相依,而且是選配的
    from PIL import Image
except ImportError:
    Image = None

ROOT = os.environ.get("FEEDS_ROOT", "/srv")
BASE = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "80"))
#: 節目封面是 Apple 規格的 3000×3000(每張 0.5–8.7 MB),而畫面上最大只用到 160 CSS px。
#: 首頁 16 張原圖 = 24 MB 傳輸 + **529 MB 解碼**,實測瀏覽器直接放棄畫後面幾張。
#: 320 = 160 的 2 倍,給 retina 用;16 張縮圖合計約 0.4 MB。
_THUMB_PX = 320
#: 垃圾桶。整個節目是「搬進來」不是刪掉 —— 那個動作不可逆,而 NAS 上這份是唯一一份
#: 隨時能上線的拷貝(重建要靠 podcast-lab 的 manifest + attempts 重發)。放在 feeds/
#: 之外,所以 scan_all() 不會把它當成 feed。
_TRASH = "_trash"
#: 剛落地的檔案一律不碰。發布器是「先 PUT 全部媒體、最後才 PUT show.json」,所以
#: 發布進行中的新 mp3 在 show.json 更新前看起來就是孤兒 —— 這時候刪掉,feed 上線
#: 當下就指向 404。半小時的緩衝讓這個競態實務上不會發生,而 26 GB 全是舊檔,不受影響。
_GRACE_S = 1800

# 任何在 show.json 全文出現過的媒體檔名都算「有被引用」。有些附件(簡報 PDF /
# 研讀講義)只存在於單集 description 的 URL 裡,從來沒有自己的欄位 —— 只比對欄位
# 會把活著的檔案報成垃圾。
_MEDIA_RE = re.compile(r"[A-Za-z0-9][\w.-]*\.(?:mp3|jpe?g|png|pdf|html)")
_KEEP = {"show.json", "feed.xml", "index.html"}
# 附件只認 uploader allowlist 保證的形狀。用「任何 .pdf/.html 檔名」去猜會誤中
# show notes 正文引用的外部連結(arXiv PDF、規格書 HTML),那一集會被判成缺檔、
# 連播放鍵都消失 —— 而它的 mp3 好好的。
_ATTACH_F = {ext: re.compile(r"^EP\d{2}-[0-9a-f]{8}\.%s$" % ext) for ext in ("pdf", "html")}
_SAFE_NAME = re.compile(r"^[\w][\w.-]*$")      # 無 "/"、無前導點 → 擋 traversal
_EP_FILE = re.compile(r"^EP0*(\d+)-")
_OLD_ART = re.compile(r"^artwork[-.]")
_SERIAL_PREFIX = re.compile(r"^EP\d+[.．、]\s*")
# 只有 content-addressed 的檔名才是 immutable。show.json / feed.xml / index.html
# 沒有 hash,給它們一年快取會讓重發後整整一年讀到舊狀態。
_HASHED = re.compile(r"-[0-9a-f]{8}\.")
_TOKEN_RE = re.compile(r"^[a-z2-7]{2,64}$")
_TRASH_ENTRY = re.compile(r"^\d{8}-\d{6}-[a-z2-7]{2,64}$")
#: Host 白名單。預設只收 IP 字面值與 localhost —— 見 `Handler._host_ok`。
#: 白名單裡的**網域名**額外要求 Cloudflare Access 的 JWT 標頭(同一個地方)。
_ALLOWED_HOSTS = {h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
# 發布器把附件拼成 `<p>{emoji} <a href="…">{label}</a></p>` 附在 notes body 尾端
# (notebooklm_mcp/publish/notes_html.py)。展開面板另外有按鈕,這裡剝掉避免重複。
_ATTACH_P = re.compile(
    r'<p>[^<]{0,6}<a href="[^"]*/feeds/[a-z2-7]{24}/EP\d+-[0-9a-f]{8}\.(?:pdf|html)">'
    r'[^<]*</a></p>\s*')
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

# `description_html` 是**發布器渲染好的 HTML**,這裡原樣插進頁面。它的最上游是
# 任意第三方網頁(mediumscrapper 抓什麼餵什麼),而發布器的 tag allowlist 是
# 2026-08-08 才加的 —— 在那之前發布的集數,磁碟上的 HTML 只過了逐行 escape,
# markdown 自己產出的 `<img src=外部網域>` / `javascript:` 連結會原樣留著。
# 這頁把 16 個 feed token 全部列出來,而 token 是那些 feed 唯一的存取控制,
# 所以同源 JS 一跑就是全部外洩、而且 token 不輪替。不依賴上游,這裡自己驗一次。
_OK_TAGS = frozenset("p ul ol li h1 h2 h3 h4 h5 h6 a code pre em strong blockquote "
                     "table thead tbody tr th td hr br sup sub dl dt dd abbr del ins "
                     "div span b i u s small".split())
_OK_ATTRS = frozenset("href title id class colspan rowspan start".split())
_OK_SCHEME = re.compile(r"^(?:https?:|mailto:|/|#|[^:]*$)", re.I)


class _Allowlist(HTMLParser):
    """標籤/屬性白名單。任何一項不合就整段判不安全 —— 不做「清洗後放行」,
    因為部分清洗的失敗模式是靜默放行,而這裡的代價是全部 feed token。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ok = True

    def handle_starttag(self, tag, attrs):
        if tag not in _OK_TAGS:
            self.ok = False
            return
        for k, v in attrs:
            if k not in _OK_ATTRS or (k == "href" and not _OK_SCHEME.match((v or "").strip())):
                self.ok = False

    handle_startendtag = handle_starttag

    def handle_decl(self, _):        # <!DOCTYPE …>
        self.ok = False

    def unknown_decl(self, _):       # <![CDATA[ … ]]>
        self.ok = False


def _is_safe_html(raw: str) -> bool:
    guard = _Allowlist()
    try:
        guard.feed(raw)
        guard.close()
    except Exception:
        return False
    return guard.ok


# ─────────────────────────── 格式化 ───────────────────────────

def _size(n: int) -> str:
    if n >= 1e9:
        return f"{n/1e9:.1f} GB"
    if n >= 1e8:
        return f"{n/1e6:.0f} MB"
    if n >= 1e6:
        return f"{n/1e6:.1f} MB"
    return f"{n/1e3:.0f} KB"


def _dur(v: str | None) -> str:
    """"00:19:01" → "19:01";"01:04:12" → "1:04:12"。"""
    if not v:
        return "—"
    parts = v.split(":")
    if len(parts) == 3 and parts[0] in ("00", "0"):
        parts = parts[1:]
    return ":".join([parts[0].lstrip("0") or "0"] + parts[1:])


def _secs(v: str | None) -> int:
    try:
        h, m, s = (int(x) for x in (v or "").split(":"))
        return h * 3600 + m * 60 + s
    except ValueError:
        return 0


def _when(ts: datetime | None, raw: str = "") -> str:
    if ts is None:
        return html.escape(raw[:22]) or "—"
    now = datetime.now(timezone.utc)
    days = (now - ts).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 7:
        return f"{days} 天前"
    if ts.year == now.year:
        return f"{ts.month}月{ts.day}日"
    return f"{ts.year}年{ts.month}月{ts.day}日"


def _parse(raw: str | None) -> datetime | None:
    try:
        dt = parsedate_to_datetime(raw or "")
    except (TypeError, ValueError):
        return None
    return dt if dt and dt.tzinfo else (dt.replace(tzinfo=timezone.utc) if dt else None)


def _hue(seed: str) -> int:
    return int(hashlib.sha1(seed.encode()).hexdigest()[:2], 16) * 360 // 256


@functools.lru_cache(maxsize=512)
def _thumb(path: str, mtime: float, size: int) -> bytes | None:
    """縮圖 bytes;沒有 Pillow 就回 None,呼叫端退回原圖(頁面照常,只是重)。

    `mtime` 只是快取 key 的一部分 —— 檔名多半是 content-addressed 不會變,但
    `artwork.jpg` 這種舊的固定檔名會被覆蓋,不進 key 就會一直吐舊縮圖。

    `draft()` 是這裡的重點:JPEG 可以用 1/2 1/4 1/8 的 DCT 尺度直接解碼,
    3000×3000 → 320 只需要真的解出 375×375,省掉九成以上的解碼成本。
    """
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            im.draft("RGB", (size, size))
            im = im.convert("RGB")
            im.thumbnail((size, size))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82, optimize=True)
            return buf.getvalue()
    except Exception:
        return None


def _cover(token: str, name: str | None, seed: str, px: int, radius: int,
           lazy: bool = True, cls: str = "") -> str:
    """封面 <img>;缺檔時退回決定性漸層方塊 —— 絕不出現破圖。"""
    style = (f"width:{px}px;height:{px}px;flex:none;border-radius:{radius}px;"
             f"object-fit:cover;background:var(--line)")
    if name:
        attrs = ' loading="lazy" decoding="async"' if lazy else ""
        return (f'<img class="{cls}" src="/t/{token}/{html.escape(name)}" alt="" '
                f'width="{px}" height="{px}" style="{style}"{attrs}>')
    h = _hue(seed)
    return (f'<div class="{cls}" style="{style};display:flex;align-items:center;'
            f'justify-content:center;color:#fff;font-weight:700;font-size:{px//3}px;'
            f'background:linear-gradient(135deg,hsl({h},44%,54%),hsl({(h+28)%360},44%,38%))">'
            f'{html.escape((seed[:1] or "?").upper())}</div>')


# ─────────────────────────── 掃描 ───────────────────────────

def scan(token: str) -> dict | None:
    """一個 feed 目錄 → 節目 + 單集 + 健康 + 孤兒。只做 scandir/stat,不讀媒體位元組。"""
    d = os.path.join(ROOT, "feeds", token)
    try:
        raw = open(os.path.join(d, "show.json"), encoding="utf-8").read()
        show = json.loads(raw)
    except (OSError, ValueError):
        return None

    sizes: dict[str, int] = {}
    with os.scandir(d) as it:
        for e in it:
            if e.is_file():                    # 跳過 Synology 的 @eaDir
                sizes[e.name] = e.stat().st_size
    # **訂閱者抓的是 feed.xml,不是 show.json。** 發布器是三個獨立 PUT
    # (媒體 → show.json → feed.xml),中斷在 show.json 之後會留下一個持久狀態:
    # show.json 已是新版,而**上線中的 feed.xml 還指著舊檔名**。只看 show.json 推導,
    # 那些舊檔名就成了「孤兒」—— 刪掉等於把上線的 enclosure 打成 404。
    # 兩份都餵進同一條 regex:被保護的正好就是訂閱者實際會抓的東西。
    side = ""
    for extra in ("feed.xml", "index.html"):
        try:
            side += open(os.path.join(d, extra), encoding="utf-8", errors="replace").read()
        except OSError:
            pass
    referenced = set(_MEDIA_RE.findall(raw + side)) | _KEEP
    total = sum(sizes.values())

    show_id = show.get("show_id") or token
    serial = show.get("itunes_type") == "serial"
    eps, missing_files, len_bad, guid_bad = [], [], [], []
    for k in sorted(show.get("episodes", {}), key=int):
        n = int(k)
        ep = dict(show["episodes"][k], n=n)
        files = _MEDIA_RE.findall(json.dumps(ep, ensure_ascii=False))
        mp3 = ep.get("media_file")
        ep["f"] = {
            "mp3": mp3,
            "pdf": next((f for f in files if _ATTACH_F["pdf"].match(f)), None),
            "html": next((f for f in files if _ATTACH_F["html"].match(f)), None),
            "cover": ep.get("artwork_file"),
        }
        used = {f for f in ep["f"].values() if f}
        ep["bytes"] = sum(sizes.get(f, 0) for f in used)
        gone = [f for f in used if f not in sizes]
        if gone:
            missing_files.append(n)
        ep["gone"] = gone
        actual = sizes.get(mp3 or "")
        ep["actual"] = actual
        ep["len_bad"] = bool(mp3 and actual is not None
                             and ep.get("length") and actual != ep["length"])
        if ep["len_bad"]:
            len_bad.append(n)
        want_guid = hashlib.sha1(f"{show_id}:{n}".encode()).hexdigest()
        ep["guid_bad"] = bool(ep.get("guid")) and ep["guid"] != want_guid
        if ep["guid_bad"]:
            guid_bad.append(n)
        ep["ts"] = _parse(ep.get("pub_date"))
        ep["short"] = _SERIAL_PREFIX.sub("", ep.get("title") or "") if serial \
            else (ep.get("title") or "")
        eps.append(ep)

    # pubDate 必須隨集號嚴格遞增,否則播放器排出來的順序跟你以為的不一樣。
    # 這個 repo 真的出過事:檢查已落磁碟、server 還是舊進程,錯序 feed 就這樣上線,
    # 而且「發布成功、read-back 全綠」。磁碟上這些集從沒被最新判準回頭掃過。
    order_bad = [b["n"] for a, b in zip(eps, eps[1:])
                 if a["ts"] and b["ts"] and b["ts"] <= a["ts"]]

    # 孤兒:依集號分組,並列出該集現行檔名當安全裝置。
    live_by_ep: dict[int, list[str]] = {}
    for ep in eps:
        live_by_ep[ep["n"]] = [f for f in ep["f"].values() if f]
    groups: dict[object, list[tuple[str, int]]] = {}
    for name in sorted(sizes):
        if name in referenced:
            continue
        m = _EP_FILE.match(name)
        # `artwork.jpg`(content-addressed 之前的舊固定檔名)與 `artwork-<hash>.jpg`
        # 都是舊節目封面,良性;歸不到的才是紅字 —— 那類長相就是跨節目撞名事故。
        key: object = int(m.group(1)) if m else (
            "artwork" if _OLD_ART.match(name) else "other")
        groups.setdefault(key, []).append((name, sizes[name]))
    orphan_bytes = sum(s for g in groups.values() for _, s in g)

    have = {ep["n"] for ep in eps}
    gaps = sorted(k for k in groups
                  if isinstance(k, int) and k not in have)

    feedxml = os.path.join(d, "feed.xml")
    mtime = os.path.getmtime(feedxml) if os.path.exists(feedxml) else 0
    stale = (not mtime) or mtime < os.path.getmtime(os.path.join(d, "show.json")) - 2

    problems = []
    if missing_files:
        problems.append(("引用的檔案不在磁碟上,enclosure 會 404", missing_files))
    if len_bad:
        problems.append(("enclosure 長度與檔案不符,播放器會提早結束", len_bad))
    if order_bad:
        problems.append(("pubDate 沒有隨集號遞增,聽眾看到的順序是亂的", order_bad))
    if guid_bad:
        problems.append(("GUID 與 show_id 對不上,重發會被當成新的一集", guid_bad))
    if stale:
        problems.append(("feed.xml 沒跟上狀態檔 —— 上次發布可能中斷", []))

    return {
        "token": token, "show": show, "show_id": show_id, "serial": serial,
        "episodes": eps, "bytes": total, "files": len(sizes), "mtime": mtime,
        "groups": groups, "live_by_ep": live_by_ep, "orphan_bytes": orphan_bytes,
        "orphan_files": sum(len(g) for g in groups.values()),
        "gaps": gaps, "problems": problems, "stale": stale,
        "nproblem": sum(len(e) or 1 for _, e in problems),
    }


def writable() -> bool:
    """有沒有刪除能力,由**掛載**決定,不由設定檔決定 —— 想關掉就把 compose 的
    `/srv` 改回 `:ro`,不必動程式。唯讀時按鈕整個不渲染,端點也直接拒絕。"""
    return os.access(ROOT, os.W_OK)


def safe_orphans(f: dict, ep: int | None = None) -> list[tuple[str, int]]:
    """**可以安全刪掉**的舊版檔。這是刪除端點唯一的檔名來源 —— 前端只送
    token(+集號),要刪哪些一律在伺服器端重新推導,不接受前端指定檔名。

    只收兩類:該集還在 show.json 的舊版、舊的節目封面。「無法歸屬」與「缺號」
    兩類永遠不收 —— 缺號那類很可能是被扣下那一集的正本。
    """
    out = []
    d = os.path.join(ROOT, "feeds", f["token"])
    now = time.time()
    # **show.json 永遠是最後寫的**(發布器:全部媒體 → show.json → feed.xml)。所以
    # 「mtime 比 show.json 新」是「這個檔還在飛」的精確判準,不是啟發法。時間常數
    # 猜不準 —— 一季 50 集的重編碼上傳遠超 30 分鐘,實測有檔案落地 31 分鐘後才被
    # show.json 引用,只靠緩衝窗會把它刪掉、feed 上線當下就 404。
    try:
        sj = os.path.getmtime(os.path.join(d, "show.json"))
    except OSError:
        return out
    for k, g in f["groups"].items():
        if ep is not None and k != ep:
            continue
        if not (k == "artwork" or (isinstance(k, int) and k in f["live_by_ep"])):
            continue
        for nm, sz in g:
            try:
                mt = os.path.getmtime(os.path.join(d, nm))
            except OSError:
                continue
            if now - mt < _GRACE_S or mt > sj + 2:      # +2 沿用 H5 的同一個容差
                continue
            out.append((nm, sz))
    return out


def _feed_dir(token: str) -> str | None:
    """這個 token 的 feed 目錄絕對路徑,不合法就回 None。

    `realpath` 之後**父目錄必須剛好是 feeds/** —— 這一條同時擋掉路徑跳脫與
    「feeds/<token> 其實是一條指到別處的 symlink」。"""
    if not _TOKEN_RE.match(token or ""):
        return None
    base = os.path.realpath(os.path.join(ROOT, "feeds"))
    d = os.path.realpath(os.path.join(base, token))
    return d if os.path.dirname(d) == base and os.path.isdir(d) else None


def _feed_file(token: str, name: str) -> str | None:
    """該 feed 目錄底下的一個**普通檔案**;是目錄、是 symlink、或名稱形狀不對都回 None。"""
    d = _feed_dir(token)
    if d is None or not _SAFE_NAME.match(name or ""):
        return None
    pth = os.path.join(d, name)
    return pth if os.path.isfile(pth) and not os.path.islink(pth) else None


def delete_orphans(token: str, ep: int | None) -> tuple[int, int]:
    """硬刪掉可安全歸屬的舊版檔。這些檔案**不在任何 feed 裡**,刪掉沒有任何訂閱者
    看得到差別 —— 所以這一個動作不進垃圾桶(進了就等於沒回收到空間)。"""
    f = scan(token)
    if not f:
        raise ValueError("找不到這個 feed")
    if f["stale"]:
        # feed.xml 落後 show.json = 上次發布沒收斂。這種狀態下磁碟上哪些檔案還被
        # 上線的 feed 引用是不確定的,不該在這時候動刀。
        raise ValueError("feed.xml 沒跟上 show.json(上次發布沒收斂),先把發布跑完再清理")
    count = freed = 0
    for nm, _sz in safe_orphans(f, ep):
        pth = _feed_file(token, nm)
        if pth is None:
            continue
        try:
            sz = os.path.getsize(pth)
            os.unlink(pth)
        except OSError:
            continue
        count += 1
        freed += sz
    return count, freed


def trash_feed(token: str, confirm: str) -> str:
    """整個節目搬進垃圾桶。**不是刪除** —— 這個動作不可逆,而 NAS 上這份是唯一
    一份隨時能上線的拷貝。搬完 feed.xml 就 404,訂閱者那邊等同下架。"""
    f = scan(token)
    if not f:
        raise ValueError("找不到這個 feed")
    if confirm != f["show_id"]:
        raise ValueError(f"確認字串與 show_id 不符(要打 {f['show_id']})")
    src = _feed_dir(token)
    if src is None:
        raise ValueError("feed 路徑不合法")
    entry = datetime.now().strftime("%Y%m%d-%H%M%S-") + token
    os.makedirs(os.path.join(ROOT, _TRASH), exist_ok=True)
    os.rename(src, os.path.join(ROOT, _TRASH, entry))
    return entry


def trash_list() -> list[dict]:
    base = os.path.join(ROOT, _TRASH)
    out = []
    for e in sorted(os.listdir(base), reverse=True) if os.path.isdir(base) else []:
        d = os.path.join(base, e)
        if not (_TRASH_ENTRY.match(e) and os.path.isdir(d) and not os.path.islink(d)):
            continue
        nfile = nbyte = 0
        for r, _x, fs in os.walk(d):
            for fn in fs:
                try:
                    nbyte += os.path.getsize(os.path.join(r, fn))
                    nfile += 1
                except OSError:
                    pass
        title = show_id = None
        try:
            sj = json.load(open(os.path.join(d, "show.json"), encoding="utf-8"))
            title, show_id = sj.get("title"), sj.get("show_id")
        except (OSError, ValueError, AttributeError):
            pass
        out.append({"entry": e, "token": e[16:], "bytes": nbyte, "files": nfile,
                    "title": title, "show_id": show_id,
                    "mtime": os.path.getmtime(d)})
    return out


def trash_count() -> int:
    """導覽列的徽章。判準要與 `trash_list()` 一致(名稱形狀 + 真的是目錄 + 不是
    symlink),否則會出現「徽章說有 1 項、點進去是空的」。"""
    base = os.path.join(ROOT, _TRASH)
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    return sum(1 for e in names if _TRASH_ENTRY.match(e)
               and os.path.isdir(os.path.join(base, e))
               and not os.path.islink(os.path.join(base, e)))


def purge_trash(entry: str) -> int:
    """永久刪除垃圾桶裡的一項。**這是全程式唯一的遞迴刪除**,所以驗到底:名稱形狀、
    realpath 的父目錄必須剛好是垃圾桶、必須是目錄且不是 symlink。"""
    if not _TRASH_ENTRY.match(entry or ""):
        raise ValueError("垃圾桶項目名稱不合法")
    base = os.path.realpath(os.path.join(ROOT, _TRASH))
    d = os.path.realpath(os.path.join(base, entry))
    if os.path.dirname(d) != base or not os.path.isdir(d) or os.path.islink(d):
        raise ValueError("垃圾桶項目不合法")
    freed = sum(os.path.getsize(os.path.join(r, fn))
                for r, _x, fs in os.walk(d) for fn in fs)
    shutil.rmtree(d)
    return freed


def scan_all() -> list[dict]:
    base = os.path.join(ROOT, "feeds")
    out = []
    for t in sorted(os.listdir(base)):
        if not os.path.isdir(os.path.join(base, t)):
            continue
        # 一個壞掉的 feed 目錄不可以連坐其他 15 個。scan() 讀的是磁碟上的
        # 外部狀態,`int(k)` 之類的假設隨時可能被一份手改過的 show.json 打破。
        try:
            f = scan(t)
        except Exception:
            continue
        if f:
            out.append(f)
    return sorted(out, key=lambda f: -f["mtime"])


# ─────────────────────────── 版型 ───────────────────────────

_CSS = """
:root{--bg:#FFF;--bg-elev:#F6F6F8;--bg-hover:#F0F0F3;--fg:#17171C;--fg-2:#5C5C66;
--fg-3:#6C6C74;--line:#E7E7EC;--accent:#6E28D9;--accent-fg:#FFF;
--warn:#9A4A08;--warn-soft:#FEF3C7;--err:#B3261E;--err-soft:#FDECEB;
--shadow:0 6px 20px rgba(0,0,0,.14)}
@media(prefers-color-scheme:dark){:root{--bg:#0E0E11;--bg-elev:#17171B;--bg-hover:#1F1F25;
--fg:#F2F2F5;--fg-2:#A0A0AB;--fg-3:#84848C;--line:#26262C;--accent:#C4B5FD;
--accent-fg:#17171C;--warn:#FBBF24;--warn-soft:#3A2E10;
--err:#F87171;--err-soft:#2E1616;--shadow:0 6px 20px rgba(0,0,0,.5)}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);padding-bottom:96px;
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang TC","Noto Sans TC",
"Microsoft JhengHei",system-ui,sans-serif;font-variant-numeric:tabular-nums}
a{color:inherit;text-decoration:none}
summary{scroll-margin-top:104px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(prefers-reduced-motion:reduce){*{transition:none!important;transform:none!important}}
.wrap{margin:0 auto;padding:24px}
@media(max-width:700px){.wrap{padding:16px}}
nav{position:sticky;top:0;z-index:20;height:48px;display:flex;align-items:center;gap:12px;
padding:0 24px;background:var(--bg-elev);border-bottom:1px solid var(--line)}
@supports(backdrop-filter:blur(1px)){nav{background:color-mix(in srgb,var(--bg-elev) 88%,transparent);
-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px)}}
nav .sp{flex:1}
h1{font-size:26px;line-height:1.35;margin:0}
h2{font-size:17px;line-height:1.35;margin:32px 0 8px}
.dim{color:var(--fg-2);font-size:13px}.dim3{color:var(--fg-3);font-size:12px}
.pill{display:inline-flex;gap:4px;align-items:center;padding:2px 9px;border-radius:999px;
font-size:12.5px;line-height:1.5;white-space:nowrap;border:1px solid transparent;
background:var(--bg-hover);color:var(--fg-2)}
.pill.err{color:var(--err);background:var(--err-soft);border-color:var(--err)}
.pill.warn{color:var(--warn);background:var(--warn-soft)}
input[type=search],select{height:32px;padding:0 10px;border:1px solid var(--line);
border-radius:8px;background:var(--bg-elev);color:inherit;font:inherit;font-size:14px}
.wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:20px 16px}
.card{display:block}
.card:hover .ct{color:var(--accent)}
.card:hover img,.card:hover .ph{transform:scale(1.02);box-shadow:var(--shadow)}
.card img,.card .ph{transition:transform .12s ease,box-shadow .12s ease;width:100%!important;
height:auto!important;aspect-ratio:1}
.ct{font-size:15px;font-weight:600;line-height:1.35;margin-top:8px;display:-webkit-box;
-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:2.7em}
.clamp3{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
ol.plain{list-style:none;margin:0;padding:0}
.rec{display:grid;grid-template-columns:40px 1fr 88px;gap:12px;align-items:center;
padding:8px 4px;border-bottom:1px solid var(--line)}
.rec:hover{background:var(--bg-hover)}
.hero{display:grid;grid-template-columns:160px 1fr;gap:20px;padding:24px 0}
@media(max-width:700px){.hero{grid-template-columns:1fr}.hero>*:first-child{max-width:160px}}
.fu{width:100%;font:12px """ + _MONO + """;padding:6px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--bg-elev);color:var(--fg-2)}
.banner{background:var(--err-soft);border-left:3px solid var(--err);border-radius:8px;
padding:12px 16px;margin-bottom:16px;font-size:14px}
.banner div{display:flex;gap:12px;justify-content:space-between}
.banner a{color:var(--err);text-decoration:underline}
.tools{position:sticky;top:48px;z-index:10;height:44px;background:var(--bg);
border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
details.ep>summary{display:grid;grid-template-columns:36px 36px 1fr auto;gap:12px;
align-items:center;padding:10px 8px;border-bottom:1px solid var(--line);cursor:pointer;
list-style:none}
details.ep>summary::-webkit-details-marker{display:none}
details.ep>summary:hover{background:var(--bg-hover)}
li.cur details.ep>summary{box-shadow:inset 3px 0 0 var(--accent)}
.play{width:36px;height:36px;border-radius:999px;border:0;cursor:pointer;font-size:13px;
background:var(--accent);color:var(--accent-fg);display:flex;align-items:center;
justify-content:center;padding:0}
.numbox{width:36px;height:36px;border-radius:8px;background:var(--bg-elev);display:flex;
align-items:center;justify-content:center;font-size:12px;font-weight:600;color:var(--fg-2)}
.et{font-size:15px;font-weight:600;line-height:1.35}
.prog{display:none;height:2px;background:var(--line);margin-top:5px;border-radius:2px}
.prog>i{display:block;height:2px;background:var(--accent);border-radius:2px}
.panel{padding:16px 16px 24px 56px;background:var(--bg-elev);
border-left:3px solid var(--accent);display:grid;grid-template-columns:96px 1fr;gap:16px}
@media(max-width:700px){.panel{grid-template-columns:1fr;padding-left:16px}}
.notes{max-width:680px}.notes p{margin:0 0 .8em}.notes a{color:var(--accent);
text-decoration:underline}.notes ul,.notes ol{padding-left:1.2em;margin:0 0 .8em}
.btn{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 12px;
border:1px solid var(--line);border-radius:8px;font-size:13px;background:var(--bg);
color:var(--fg-2);cursor:pointer}
.btn:hover{border-color:var(--fg-3);color:var(--fg)}
.btn.danger{color:var(--err);border-color:var(--err)}
.btn.danger:hover{background:var(--err);color:#fff;border-color:var(--err)}
.btn.sm{height:26px;padding:0 10px;font-size:12.5px}
.danger-box{border:1px solid var(--err);background:var(--err-soft);border-radius:8px;
padding:14px 16px;margin-top:16px;font-size:13.5px}
.mono{font:12px """ + _MONO + """;color:var(--fg-3);word-break:break-all}
.kv{display:grid;grid-template-columns:120px 1fr;gap:4px 12px;font-size:13px;margin-top:12px}
.kv b{font-weight:400;color:var(--fg-3);font-size:12px}
#bar{position:fixed;bottom:0;left:0;right:0;height:64px;z-index:30;display:none;
align-items:center;gap:12px;padding:0 16px;background:var(--bg-elev);
border-top:1px solid var(--line);box-shadow:var(--shadow)}
#bar audio{flex:1;height:36px;min-width:120px}
@media(max-width:700px){#bar{height:auto;flex-wrap:wrap;padding:8px 12px}
#bar audio{order:3;width:100%;flex:none}body{padding-bottom:140px}}
.stor{display:grid;grid-template-columns:1fr 92px;gap:4px 12px;font:12px """ + _MONO + """;
color:var(--fg-2);padding:2px 0}
textarea{width:100%;font:12px """ + _MONO + """;padding:10px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--fg-2);resize:vertical}
footer{margin:40px 0 0;font-size:12px;color:var(--fg-3);text-align:center}
dialog{border:0;padding:0;border-radius:14px;max-width:min(440px,92vw);
color:var(--fg);background:var(--bg-elev);box-shadow:var(--shadow)}
dialog::backdrop{background:rgba(0,0,0,.55)}
dialog form{padding:20px;display:grid;gap:14px}
#dmsg{font-size:14.5px;line-height:1.6;white-space:pre-line}
#dlab{font-size:13px;color:var(--fg-2);display:grid;gap:6px}
#dlab b{color:var(--fg);font-size:14px;font-family:""" + _MONO + """}
#din{height:40px;padding:0 12px;border:1px solid var(--line);border-radius:8px;
background:var(--bg);color:var(--fg);font:inherit;font-size:15px}
#din:focus{outline:2px solid var(--accent);outline-offset:1px}
dialog menu{display:flex;gap:8px;justify-content:flex-end;margin:0;padding:0}
dialog menu button{height:38px;padding:0 16px;font-size:14px}
dialog menu button[disabled]{opacity:.35;cursor:not-allowed}
"""

# raw string:裡面的 `\n` 是要給 **JavaScript** 的逸出序列,不是 Python 的。
# 少了這個 r,Python 會先把它展開成真的換行,而 JS 的單引號字串不能跨行
# → 整支 <script> SyntaxError,**整頁的 JS 全部不執行**(按鈕、播放器、搜尋)。
_JS = r"""
var box=document.getElementById('wall')||document.getElementById('eps');
var q=document.getElementById('q'),sel=document.getElementById('sort'),
    cnt=document.getElementById('count');
function apply(){
  if(!box)return;
  var kids=[].slice.call(box.children),term=(q&&q.value||'').toLowerCase().trim();
  if(sel){var k=sel.value;
    kids.sort(function(a,b){return k==='title'
      ? a.dataset.title.localeCompare(b.dataset.title)
      : Number(b.dataset[k])-Number(a.dataset[k]);});
    kids.forEach(function(c){box.appendChild(c);});}
  var n=0;
  kids.forEach(function(c){var hit=!term||c.dataset.title.indexOf(term)>=0;
    c.hidden=!hit;if(hit)n++;});
  if(cnt)cnt.textContent='顯示 '+n+' / '+kids.length;
}
if(q)q.addEventListener('input',apply);
if(sel)sel.addEventListener('change',apply);

function hms(s){s=Math.floor(s);var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
  return (h?h+':'+('0'+m).slice(-2):m)+':'+('0'+x).slice(-2);}

// 展開時才載入單集封面。抽成函式是因為 `openHash()` 用程式碼設 `open` 時,
// `toggle` 事件是**非同步**派發的 —— 直接呼叫一次就不必依賴那個時序。
function lazyimg(root){
  [].forEach.call(root.querySelectorAll('img[data-src]'),function(i){
    i.src=i.dataset.src;i.removeAttribute('data-src');});
}
document.addEventListener('toggle',function(e){
  if(e.target.tagName==='DETAILS'&&e.target.open)lazyimg(e.target);
},true);

var bar=document.getElementById('bar'),au=document.getElementById('au'),cur=null,last=0;
document.addEventListener('click',function(e){
  var b=e.target.closest?e.target.closest('.play'):null;
  if(!b)return;
  e.preventDefault();e.stopPropagation();
  var d=b.dataset;
  au.src=d.src;
  document.getElementById('bt').textContent=d.title;
  document.getElementById('bs').textContent=d.show;
  var bc=document.getElementById('bc');
  if(d.cover){bc.src=d.cover;bc.style.display='block';}else{bc.style.display='none';}
  bar.style.display='flex';
  var p=0;try{p=Number(localStorage.getItem('pos:'+d.guid)||0);}catch(_){}
  cur=d.guid;last=0;
  au.play().then(function(){if(p>5&&p<au.duration-5)au.currentTime=p;},function(){});
  [].forEach.call(document.querySelectorAll('li.cur'),function(x){x.classList.remove('cur');});
  var li=b.closest('li');if(li)li.classList.add('cur');
});
if(au)au.addEventListener('timeupdate',function(){
  if(!cur||au.currentTime-last<5)return;
  last=au.currentTime;
  try{localStorage.setItem('pos:'+cur,Math.floor(au.currentTime));}catch(_){}
});
var bx=document.getElementById('bx');
if(bx)bx.onclick=function(){au.pause();bar.style.display='none';};

[].forEach.call(document.querySelectorAll('.play[data-secs]'),function(b){
  var p=0;try{p=Number(localStorage.getItem('pos:'+b.dataset.guid)||0);}catch(_){}
  var s=Number(b.dataset.secs||0);
  if(!p||!s)return;
  b.title='繼續 '+hms(p);
  if(p/s>=.95){b.textContent='✓';return;}
  var li=b.closest('li'),pr=li&&li.querySelector('.prog');
  if(pr){pr.style.display='block';pr.firstChild.style.width=Math.min(100,p/s*100)+'%';}
});

// getElementById 而不是 querySelector:真實 token 有數字開頭的
// (`6ia36wl6…`),`#6ia…` 不是合法 CSS 選擇器,querySelector 會直接 throw,
// 連帶殺掉這支 script 後面所有東西。
function openHash(){
  var id=location.hash&&decodeURIComponent(location.hash.slice(1));
  var t=id&&document.getElementById(id);
  if(!t)return;
  if(t.tagName==='DETAILS'){t.open=true;lazyimg(t);}
  (t.querySelector('summary')||t).scrollIntoView({block:'start'});
}
openHash();
addEventListener('hashchange',openHash);

// 頁面內的確認對話框。原生 confirm()/prompt() 不能用:瀏覽器會加上
// 「192.168.31.105:8087 says」這種前綴、樣式完全不受控、手機上輸入框還會自動大寫,
// 而這裡要打的是 `zz-test-v060` 這種 show_id。用**原生 <dialog>** ——
// backdrop、ESC 關閉、焦點鎖定都是平台給的,不必自己寫,也不需要任何函式庫。
var dlg=document.getElementById('dlg'),dmsg=document.getElementById('dmsg'),
    dlab=document.getElementById('dlab'),dexp=document.getElementById('dexp'),
    din=document.getElementById('din'),dok=document.getElementById('dok'),
    dno=document.getElementById('dno'),dform=document.getElementById('dform');
var native=!dlg||typeof dlg.showModal!=='function';   // 太舊的瀏覽器才退回原生
if(dno)dno.onclick=function(){dlg.close('');};
if(dform)dform.addEventListener('submit',function(ev){
  // 自己處理 submit,不靠 method=dialog:頁面 CSP 有 form-action 'none',
  // 而且這樣 Enter 一定落在確定鍵、不會落在取消鍵。
  ev.preventDefault();
  if(!dok.disabled)dlg.close('ok');
});
function ask(msg,typed,then){
  if(native){
    if(typed){if(prompt(msg+'\n\n輸入 '+typed+' 確認:')!==typed)return;}
    else if(!confirm(msg)){return;}
    return then();
  }
  dmsg.textContent=msg;
  dlab.hidden=!typed;dexp.textContent=typed||'';din.value='';
  dno.hidden=false;
  dok.className='btn danger';dok.textContent=typed?'刪除':'確定刪除';
  dok.disabled=!!typed;                      // 打對字串之前按不下去
  dlg.returnValue='';
  dlg.onclose=function(){if(dlg.returnValue==='ok')then();};
  din.oninput=function(){dok.disabled=din.value.trim()!==typed;};
  dlg.showModal();
  if(typed)din.focus();
}
function say(msg,then){
  if(native){alert(msg);if(then)then();return;}
  dmsg.textContent=msg;
  dlab.hidden=true;dno.hidden=true;
  dok.className='btn';dok.textContent='好';dok.disabled=false;
  dlg.returnValue='';
  dlg.onclose=function(){if(then)then();};
  dlg.showModal();
}

// 破壞性動作。前端只送 token(+集號/確認字串);**要刪哪些檔案一律由伺服器端
// 重新推導**,前端指定不了檔名。X-Confirm 這顆自訂標頭會逼出 CORS preflight,
// 跨來源的頁面送不出去 —— 這頁沒有登入態,不能讓別的網站 drive-by 打進來。
document.addEventListener('click',function(e){
  var b=e.target.closest?e.target.closest('[data-act]'):null;
  if(!b)return;
  e.preventDefault();e.stopPropagation();
  var d=b.dataset;
  ask(d.msg,d.typed||'',function(){
    var label=b.textContent;
    b.disabled=true;b.textContent='處理中…';
    fetch('/api/'+d.act,{method:'POST',headers:{'Content-Type':'application/json',
          'X-Confirm':'1'},body:d.body})
      .then(function(r){return r.json().then(function(j){return{s:r.status,j:j};});})
      .then(function(x){
        if(x.s!==200){
          say('失敗:'+(x.j.error||x.s),function(){location.reload();});return;}
        say(x.j.msg,function(){
          location.href=d.then||location.pathname+location.search;});
      })
      .catch(function(err){
        b.disabled=false;b.textContent=label;say('失敗:'+err);});
  });
});
"""


def _shell(title: str, body: str, orphan_total: int, width: int) -> bytes:
    recycle = (f'<a href="/storage" class="pill warn">♻ 回收 {_size(orphan_total)}</a>'
               if orphan_total else "")
    ntrash = trash_count()
    if ntrash:
        recycle += f' <a href="/trash" class=pill>🗑 垃圾桶 {ntrash}</a>'
    if not writable():
        recycle += ' <span class=pill title="/srv 是唯讀掛載">🔒 唯讀</span>' 
    return (
        "<!doctype html><html lang=zh-Hant><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<meta http-equiv=Content-Security-Policy content=\"default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self'; media-src 'self'; connect-src 'self'; form-action 'none'\">"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style>"
        f"<nav><a href='/' style='font-weight:600'>◉ 我的節目</a><span class=sp></span>"
        f"{recycle}</nav>"
        f"<div class=wrap style='max-width:{width}px'>{body}"
        "<footer>本頁列出全部 feed token · 僅限 LAN · 不可掛上 tunnel</footer></div>"
        "<div id=bar><img id=bc alt='' width=40 height=40 "
        "style='width:40px;height:40px;border-radius:6px;object-fit:cover'>"
        "<div style='width:240px;overflow:hidden'>"
        "<div id=bt style='font-size:14px;font-weight:600;white-space:nowrap;"
        "overflow:hidden;text-overflow:ellipsis'></div>"
        "<div id=bs class=dim3 style='white-space:nowrap;overflow:hidden;"
        "text-overflow:ellipsis'></div></div>"
        "<audio id=au controls preload=none></audio>"
        "<button id=bx class=btn aria-label=關閉>✕</button></div>"
        # 確認對話框。原生 <dialog> —— backdrop / ESC / 焦點鎖定都是平台給的。
        "<dialog id=dlg><form id=dform>"
        "<div id=dmsg></div>"
        "<label id=dlab hidden><span>輸入 <b id=dexp></b> 確認</span>"
        "<input id=din autocomplete=off autocapitalize=off autocorrect=off "
        "spellcheck=false enterkeyhint=go></label>"
        "<menu><button type=button id=dno class=btn>取消</button>"
        "<button id=dok class='btn danger'>刪除</button></menu>"
        "</form></dialog>"
        f"<script>{_JS}</script></html>"
    ).encode("utf-8")


# ─────────────────────────── 視圖 ───────────────────────────

def render_index(feeds: list[dict], orphan_total: int) -> bytes:
    tot = sum(f["bytes"] for f in feeds)
    neps = sum(len(f["episodes"]) for f in feeds)
    pct = f"{orphan_total/tot*100:.0f}%" if tot else "0%"

    cards = []
    for f in feeds:
        s = f["show"]
        badges = ""
        if f["serial"]:
            badges += '<span class=pill>serial</span> '
        if f["problems"]:
            badges += f'<span class="pill err">✕ {f["nproblem"]} 個問題</span> '
        if f["orphan_bytes"]:
            badges += f'<span class="pill warn">♻ {_size(f["orphan_bytes"])}</span>'
        title = s.get("title") or f["token"]
        cards.append(
            f'<a class=card href="/s/{f["token"]}" '
            f'data-title="{html.escape((title + " " + f["show_id"]).lower(), True)}" '
            f'data-eps="{len(f["episodes"])}" data-bytes="{f["bytes"]}" '
            f'data-orphan="{f["orphan_bytes"]}" data-mtime="{int(f["mtime"])}">'
            + _cover(f["token"], s.get("artwork_file"), f["show_id"], 160, 12, cls="ph")
            + f'<div class=ct>{html.escape(title)}</div>'
            f'<div class=dim>{len(f["episodes"])} 集 · {_size(f["bytes"])}</div>'
            f'<div class=dim3>最後發布 '
            f'{_when(datetime.fromtimestamp(f["mtime"], timezone.utc)) if f["mtime"] else "從未"}'
            f'</div><div style="margin-top:4px">{badges}</div></a>')

    recent = sorted(
        ((ep, f) for f in feeds for ep in f["episodes"] if ep["ts"]),
        key=lambda p: p[0]["ts"], reverse=True)[:12]
    rows = []
    for ep, f in recent:
        rows.append(
            f'<li><a class=rec href="/s/{f["token"]}#ep{ep["n"]}">'
            + _cover(f["token"], f["show"].get("artwork_file"), f["show_id"], 40, 8)
            + f'<div style="min-width:0"><div class=et style="white-space:nowrap;'
            f'overflow:hidden;text-overflow:ellipsis">{html.escape(ep["short"])}</div>'
            f'<div class=dim3>{html.escape(f["show"].get("title") or f["show_id"])} · '
            f'{_when(ep["ts"])}</div></div>'
            f'<div class=dim style="text-align:right">{_dur(ep.get("duration"))}</div>'
            f'</a></li>')

    orphan_line = (f' · <a href="/storage" style="color:var(--warn);'
                   f'text-decoration:underline dotted">其中 {_size(orphan_total)} '
                   f'是被取代的舊版({pct})</a>') if orphan_total else ""
    body = (
        f"<h1>我的節目</h1>"
        f'<div class=dim style="margin:4px 0 20px">{len(feeds)} 個節目 · {neps} 集 · '
        f'{_size(tot)}{orphan_line}</div>'
        f'<div style="display:flex;gap:8px;align-items:center;margin-bottom:20px">'
        f'<input type=search id=q placeholder="搜尋節目" style="width:240px">'
        f'<select id=sort aria-label="排序"><option value=mtime>最後發布</option>'
        f'<option value=eps>集數</option><option value=bytes>佔用大小</option>'
        f'<option value=orphan>可回收</option><option value=title>名稱</option></select>'
        f'<span class=sp style="flex:1"></span>'
        f'<span class=dim3 id=count>顯示 {len(feeds)} / {len(feeds)}</span></div>'
        f'<div class=wall id=wall>{"".join(cards)}</div>'
        f'<h2>最近發布</h2><div class=dim3 style="margin-bottom:8px">'
        f'依單集 pub_date(發布器寫入的生成完成時間)排序,重發舊集不會浮上來</div>'
        f'<ol class=plain>{"".join(rows)}</ol>')
    return _shell("我的節目", body, orphan_total, 1240)


def _notes(ep: dict, token: str) -> str:
    raw = ep.get("description_html")
    if raw and _is_safe_html(raw):
        out = _ATTACH_P.sub("", raw)
        # 離線 LAN 讀不到公開網域,notes 內的連結改指本機。
        # **非貪婪**,而且排除 `<>"'`:貪婪版會從第一個 `https://` 一路吃到整段
        # 裡**最後**一個 `/feeds/<token>/`,把兩個連結之間的正文整段吞掉。
        out = re.sub(r'https?://[^\s"\'<>]*?/feeds/' + re.escape(token) + "/",
                     f"/f/{token}/", out)
    else:
        text = ep.get("description") or ""
        keep = [ln for ln in text.split("\n") if not ln.lstrip()[:1] in ("📄", "📖")]
        out = "".join(f"<p>{html.escape(p.strip())}</p>"
                      for p in "\n".join(keep).split("\n\n") if p.strip())
    return out or '<p class=dim3><i>(沒有 show notes)</i></p>'


def render_show(f: dict, orphan_total: int) -> bytes:
    s, tok = f["show"], f["token"]
    title = s.get("title") or tok
    art = s.get("artwork_file")

    banner = ""
    if f["problems"]:
        lines = "".join(
            f'<div><span>✕ {html.escape(msg)}</span><span class=mono>'
            + " ".join(f'<a href="#ep{n}">EP{n:02d}</a>' for n in eps[:12])
            + ("…" if len(eps) > 12 else "") + "</span></div>"
            for msg, eps in f["problems"])
        banner = f'<div class=banner>{lines}</div>'

    eps = f["episodes"] if f["serial"] else sorted(
        f["episodes"], key=lambda e: (e["ts"] or datetime.min.replace(
            tzinfo=timezone.utc)), reverse=True)
    rows = []
    for ep in eps:
        n, fl = ep["n"], ep["f"]
        pills = ""
        if fl["pdf"]:
            pills += '<span class=pill>PDF</span> '
        if fl["html"]:
            pills += '<span class=pill>講義</span> '
        if ep["len_bad"]:
            pills += '<span class="pill err">✕ 大小不符</span> '
        if ep["gone"]:
            pills += '<span class="pill err">✕ 檔案遺失</span> '
        playable = bool(fl["mp3"]) and fl["mp3"] not in ep["gone"]
        play = (f'<button class=play data-src="/f/{tok}/{html.escape(fl["mp3"] or "", True)}" '
                f'data-title="{html.escape(ep.get("title") or "", True)}" '
                f'data-show="{html.escape(title, True)}" '
                f'data-cover="{("/t/" + tok + "/" + html.escape(fl["cover"], True)) if fl["cover"] else ""}" '
                f'data-guid="{html.escape(ep.get("guid") or str(n), True)}" '
                f'data-secs="{_secs(ep.get("duration"))}" '
                f'aria-label="播放 EP{n:02d}">▶</button>') if playable else \
               '<span class=numbox style="background:transparent">—</span>'

        att = "".join(
            f'<a class=btn href="/f/{tok}/{html.escape(v, True)}" target=_blank rel=noopener>{lbl}</a>'
            for lbl, v in (("📄 本集簡報 PDF", fl["pdf"]), ("📖 研讀講義", fl["html"]),
                           (f'⬇ mp3 {_size(ep["actual"] or 0)}', fl["mp3"])) if v)
        old = [(nm, sz) for nm, sz in f["groups"].get(n, []) if nm.endswith(".mp3")]
        for i, (nm, sz) in enumerate(old, 1):
            att += (f'<button class=play data-src="/f/{tok}/{html.escape(nm, True)}" '
                    f'data-title="{html.escape((ep.get("title") or "") + f" (舊版 {i})", True)}" '
                    f'data-show="{html.escape(title, True)}" data-cover="" '
                    f'data-guid="old:{nm}" data-secs="0" '
                    f'style="width:auto;height:32px;padding:0 12px;border-radius:8px;'
                    f'background:var(--bg);border:1px solid var(--line);color:var(--fg-2);'
                    f'font-size:13px">▶ 舊版 take {i} · {_size(sz)} · 未在 feed</button>')

        n_old = len(safe_orphans(f, n)) if n in f["live_by_ep"] else 0
        if n_old:
            att += _act("delete-orphans", {"token": tok, "ep": n},
                        f"刪掉 EP{n:02d} 的 {n_old} 個舊版檔。這些不在 feed 裡,"
                        f"訂閱者看不到差別,但刪了就沒了。",
                        f"✕ 刪除 {n_old} 個舊版", cls="btn danger")

        mismatch = (f' <span style="color:var(--err)">(show.json 記 '
                    f'{ep["length"]:,})</span>') if ep["len_bad"] else ""
        rows.append(
            f'<li data-title="{html.escape((ep.get("title") or "").lower(), True)}">'
            f'<details class=ep id=ep{n}><summary>{play}'
            f'<span class=numbox>{n}</span>'
            f'<div style="min-width:0"><div class=et>{html.escape(ep["short"])}</div>'
            f'<div class=dim>{_when(ep["ts"], ep.get("pub_date") or "")} · '
            f'{_dur(ep.get("duration"))} · {_size(ep["bytes"])}</div>'
            f'<div class=prog><i></i></div></div>'
            f'<div style="text-align:right">{pills}</div></summary>'
            f'<div class=panel>'
            + (f'<img data-src="/t/{tok}/{html.escape(fl["cover"], True)}" alt="" width=96 height=96 '
               f'style="width:96px;height:96px;border-radius:8px;object-fit:cover;'
               f'background:var(--line)">' if fl["cover"] else "<div></div>")
            + f'<div><div class=notes>{_notes(ep, tok)}</div>'
            f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 12px">{att}</div>'
            f'<div class=mono>{html.escape(fl["mp3"] or "—")} · '
            f'{(ep["actual"] or 0):,} B{mismatch} · guid '
            f'{html.escape((ep.get("guid") or "")[:4])}…{html.escape((ep.get("guid") or "")[-4:])}'
            f' · {html.escape(ep.get("pub_date") or "")}</div></div></div></details></li>')

    feed_url = f"{BASE}/feeds/{tok}/feed.xml" if BASE else f"/feeds/{tok}/feed.xml"
    info = "".join(f"<b>{k}</b><span>{html.escape(str(v)) if v not in (None, '') else '— 未設定'}</span>"
                   for k, v in (("show_id", s.get("show_id")), ("token", tok),
                                ("author", s.get("author")),
                                ("owner_name", s.get("owner_name")),
                                ("owner_email", s.get("owner_email")),
                                ("language", s.get("language")),
                                ("category", s.get("category")),
                                ("explicit", s.get("explicit")),
                                ("itunes_type", s.get("itunes_type")),
                                ("artwork_file", art)))

    gap = ""
    if f["gaps"]:
        gap = ('<div class=dim3 style="margin-top:12px">缺號 '
               + "、".join(f"EP{n:02d}" for n in f["gaps"])
               + ":show.json 沒有這些集,但磁碟上有對應檔案 —— 扣下或發布失敗(推導)</div>")
    orph = (f'<div style="margin-top:20px"><a href="/storage#{tok}" '
            f'style="color:var(--warn);font-size:13px">♻ {f["orphan_files"]} 個被取代的'
            f'舊版檔 · {_size(f["orphan_bytes"])} —— 仍然公開可讀 →</a></div>'
            ) if f["orphan_bytes"] else ""

    body = (
        f'<div class=hero>'
        + _cover(tok, art, f["show_id"], 160, 12, lazy=False)
        + f'<div><h1 style="font-size:22px">{html.escape(title)}</h1>'
        f'<div class=dim style="margin:4px 0 8px">'
        f'{html.escape(s.get("author") or "—")} · {html.escape(s.get("category") or "—")} · '
        f'{html.escape(s.get("language") or "—")}</div>'
        f'<div style="margin-bottom:8px">'
        f'{"<span class=pill>serial</span> " if f["serial"] else ""}'
        f'{"<span class=pill>explicit</span>" if s.get("explicit") else ""}</div>'
        f'<div class=clamp3 style="margin-bottom:12px">'
        f'{html.escape(s.get("description") or "")}</div>'
        f'<input class=fu readonly onclick="this.select()" '
        f'value="{html.escape(feed_url, True)}">'
        f'<div style="margin:6px 0 8px;font-size:12px">'
        f'<a href="/f/{tok}/feed.xml" target=_blank rel=noopener class=dim>feed.xml ↗</a> · '
        f'<a href="/f/{tok}/index.html" target=_blank rel=noopener class=dim>index.html ↗</a></div>'
        f'<div class=dim3>{len(f["episodes"])} 集 · {_size(f["bytes"])} · '
        f'{f["files"]} 個檔案 · 最後發布 '
        f'{_when(datetime.fromtimestamp(f["mtime"], timezone.utc)) if f["mtime"] else "從未"}</div>'
        f'<details><summary class=dim style="cursor:pointer;margin-top:12px">節目資訊'
        f'與刪除</summary><div class=kv>{info}</div>'
        + (f'<div class=danger-box><b>刪除整個節目</b> —— '
           f'{len(f["episodes"])} 集、{_size(f["bytes"])}、{f["files"]} 個檔案會搬進'
           f'垃圾桶,<code>feed.xml</code> 立刻 404,訂閱者那邊等同下架。'
           f'還原是一行 <code>mv</code>;要真的釋出空間再去垃圾桶按永久刪除。'
           f'<div style="margin-top:10px">'
           + _act("trash-feed", {"token": tok, "confirm": f["show_id"]},
                  f'把「{title}」整個節目刪掉:{len(f["episodes"])} 集 · '
                  f'{_size(f["bytes"])}。feed 會立刻下架。',
                  "✕ 刪除整個節目", typed=f["show_id"], then="/", cls="btn danger")
           + '</div></div>' if writable() else "")
        + f'<div class=dim3 style="margin-top:10px">沒有「刪除單集」:單集還在 '
        f'<code>feed.xml</code> 裡,直接刪檔會讓訂閱者的 enclosure 指向 404。'
        f'要下架某一集請走 publisher(扣下該集 + 重發),那會同時改寫 '
        f'<code>show.json</code> 與 <code>feed.xml</code>。上面刪的是<b>已經不在 '
        f'feed 裡</b>的舊版檔。</div></details></div></div>'
        + banner
        + f'<div class=tools><input type=search id=q placeholder="搜尋單集" '
        f'style="width:240px"><span style="flex:1"></span>'
        f'<span class=dim3 id=count>顯示 {len(eps)} / {len(eps)}</span></div>'
        f'<ol class=plain id=eps>{"".join(rows)}</ol>{gap}{orph}')
    return _shell(title, body, orphan_total, 900)


def _act(act: str, body: dict, msg: str, label: str, typed: str = "",
         then: str = "", cls: str = "btn danger sm") -> str:
    """破壞性按鈕。`/srv` 唯讀時整顆不渲染 —— 不給一顆按下去只會失敗的按鈕。"""
    if not writable():
        return ""
    return (f'<button class="{cls}" data-act="{act}" '
            f'data-body="{html.escape(json.dumps(body, ensure_ascii=False), True)}" '
            f'data-msg="{html.escape(msg, True)}"'
            + (f' data-typed="{html.escape(typed, True)}"' if typed else "")
            + (f' data-then="{html.escape(then, True)}"' if then else "")
            + f">{label}</button>")


def render_trash(orphan_total: int) -> bytes:
    rows = []
    total = 0
    for e in trash_list():
        total += e["bytes"]
        restore = (f"# 還原(<資料根> 就是 caddy 掛成 /srv 的那個目錄)\n"
                   f"mv <資料根>/{_TRASH}/{e['entry']} <資料根>/feeds/{e['token']}")
        rows.append(
            f'<div style="border-bottom:1px solid var(--line);padding:14px 0">'
            f'<div style="display:flex;gap:12px;align-items:baseline">'
            f'<span style="font-size:15px;font-weight:600;flex:1">'
            f'{html.escape(e["title"] or e["token"])}</span>'
            f'<span class=dim>{e["files"]} 檔 · {_size(e["bytes"])} · '
            f'{_when(datetime.fromtimestamp(e["mtime"], timezone.utc))}丟進來</span>'
            + _act("purge-trash", {"entry": e["entry"]},
                   f'永久刪除「{e["title"] or e["token"]}」的 {e["files"]} 個檔案'
                   f'({_size(e["bytes"])})。這一步救不回來。',
                   "✕ 永久刪除", typed=e["show_id"] or e["token"], then="/trash")
            + f'</div><div class=mono style="margin-top:6px">{_TRASH}/{html.escape(e["entry"])}</div>'
            f'<textarea readonly rows=2 onclick="this.select()" '
            f'style="margin-top:8px">{html.escape(restore)}</textarea></div>')
    body = ('<h1>垃圾桶</h1>'
            '<div class=dim style="margin:4px 0 20px">整個節目被刪除時會先搬到這裡,'
            f'還原只要一行 <code>mv</code>。永久刪除才會真的釋出空間。目前 {_size(total)}。'
            '</div>'
            + ("".join(rows) or '<div class=dim3>垃圾桶是空的。</div>'))
    return _shell("垃圾桶", body, orphan_total, 900)


def render_storage(feeds: list[dict], orphan_total: int) -> bytes:
    # 全站按鈕的標示必須是**端點真的刪得到的量**。`orphan_total` 還包含缺號、
    # 無法歸屬、以及還在飛的新檔 —— 那些永遠不會被刪,標上去就是高估。
    all_safe = [x for f in feeds if not f["stale"] for x in safe_orphans(f)]
    safe_bytes = sum(s for _, s in all_safe)
    blocks = []
    for i, f in enumerate(sorted(feeds, key=lambda x: -x["orphan_bytes"])):
        if not f["orphan_bytes"]:
            continue
        tok = f["token"]
        parts = []
        keys = sorted((k for k in f["groups"] if isinstance(k, int)))
        keys += [k for k in ("artwork", "other") if k in f["groups"]]
        for k in keys:
            g = f["groups"][k]
            gsize = sum(s for _, s in g)
            if k == "artwork":
                head = (f'<div class=dim3 style="margin-top:12px">舊節目封面 · '
                        f'{len(g)} 檔 · {_size(gsize)} —— 良性</div>')
            elif k == "other":
                head = (f'<div style="margin-top:12px;color:var(--err);font-size:13px;'
                        f'font-weight:600">無法歸屬 · {len(g)} 檔 · {_size(gsize)}'
                        f' —— 檔名前綴對不上這個節目任何一集</div>')
            else:
                live = f["live_by_ep"].get(k)
                cur = ("現行:" + "、".join(live)) if live else \
                    "⚠ show.json 沒有這一集"
                btn = ""
                if live and not f["stale"]:
                    ok = safe_orphans(f, k)
                    # 件數與大小**都要**取安全集:`gsize` 是整組(含還在飛的新檔),
                    # 混在同一句話裡會讓對話框說 12.5 GB、實刪 3.5 GB。
                    if ok:
                        btn = _act("delete-orphans", {"token": tok, "ep": k},
                                   f"刪掉 EP{k:02d} 的 {len(ok)} 個舊版檔"
                                   f"({_size(sum(s for _, s in ok))})。這些檔案不在 "
                                   f"feed 裡,訂閱者看不到差別,但刪了就沒了。",
                                   f"✕ 刪除 {len(ok)} 個舊版")
                head = (f'<div style="margin-top:12px;font-size:13px;font-weight:600;'
                        f'display:flex;justify-content:space-between;gap:12px;'
                        f'align-items:center">'
                        f'<span>EP{k:02d} · {len(g)} 個舊版 · {_size(gsize)}</span>'
                        f'<span class=mono style="flex:1;text-align:right">'
                        f'{html.escape(cur)}</span>{btn}</div>')
            parts.append(head + "".join(
                f'<div class=stor><span>{html.escape(nm)}</span>'
                f'<span style="text-align:right">{_size(sz)}</span><span></span></div>'
                for nm, sz in g))

        # 指令與按鈕**必須看到同一組檔案**,否則使用者會以為兩條路等價。兩邊都走
        # `safe_orphans`:只收該集還在 show.json 的舊版與舊節目封面,而且跳過剛落地的。
        # 「無法歸屬」與「缺號」兩類永遠不進 —— 缺號那類很可能是被扣下那一集的正本。
        n_safe = safe_orphans(f)
        safe = [nm for nm, _ in n_safe]
        attributable = sum(len(g) for k, g in f["groups"].items()
                           if k == "artwork" or (isinstance(k, int) and k in f["live_by_ep"]))
        skipped = attributable - len(safe)
        risky = [nm for k, g in f["groups"].items()
                 if k == "other" or (isinstance(k, int) and k not in f["live_by_ep"])
                 for nm, _ in g]
        cmd = (f"# 介面產生的文字,執行前自己看過\n"
               f"cd <你 shell 看到的 feeds 目錄>/{tok}\n"
               f"mkdir -p ../../_quarantine/{tok}\n"
               + ("mv \\\n  " + " \\\n  ".join(safe)
                  + f" \\\n  ../../_quarantine/{tok}/\n" if safe
                  else "# (這個節目沒有可安全歸屬的舊版)\n")
               + (f"# 跳過 {skipped} 個剛落地不到 30 分鐘的檔案(可能是發布進行中的新檔)\n"
                  if skipped else "")
               + "".join(f"# 不確定,自己判斷過再動:{nm}\n" for nm in risky))
        blocks.append(
            f'<details id="{tok}"{" open" if i < 3 else ""} '
            f'style="border-bottom:1px solid var(--line)">'
            f'<summary style="display:flex;align-items:center;gap:12px;height:56px;'
            f'cursor:pointer;list-style:none">'
            + _cover(tok, f["show"].get("artwork_file"), f["show_id"], 32, 8)
            + f'<span style="font-size:15px;font-weight:600;flex:1">'
            f'{html.escape(f["show"].get("title") or tok)}</span>'
            f'<span style="color:var(--warn);font-size:13px">'
            f'{_size(f["orphan_bytes"])} · {f["orphan_files"]} 檔</span></summary>'
            f'<div style="padding:0 0 20px 44px">'
            + (f'<div class=banner style="margin:0 0 12px">✕ feed.xml 沒跟上 '
               f'show.json —— 上次發布沒收斂,這個節目暫時不開放清理(先把發布跑完)。'
               f'</div>' if f["stale"] else "")
            + f'{"".join(parts)}'
            + (f'<div style="margin-top:16px">'
               + _act("delete-orphans", {"token": tok},
                      f'刪掉「{f["show"].get("title") or tok}」全部 {len(n_safe)} 個'
                      f'可安全歸屬的舊版檔({_size(sum(s for _, s in n_safe))})。'
                      f'不在 feed 裡的才會被刪,「無法歸屬」與「缺號」兩類不動。',
                      f'✕ 刪除本節目 {len(n_safe)} 個舊版 · {_size(sum(s for _, s in n_safe))}',
                      cls="btn danger")
               + '</div>' if n_safe and not f["stale"] else "")
            + f'<textarea readonly rows=6 onclick="this.select()" '
            f'style="margin-top:16px">{html.escape(cmd)}</textarea></div></details>')

    body = (
        '<h1>可回收空間</h1>'
        '<div style="background:var(--warn-soft);border-left:4px solid var(--warn);'
        'border-radius:8px;padding:14px 16px;font-size:14px;margin:16px 0 24px">'
        f'<b>{_size(orphan_total)} 是被取代的舊版。</b> 發布器永不刪檔,這些檔案沒有進 '
        'feed、沒有人訂閱得到,但<b>網址仍然公開可讀</b>。刪除是<b>直接硬刪</b>,'
        '不進垃圾桶 —— 進了就等於沒回收到空間;下面也附了等效的 shell 指令'
        '文字。孤兒是用「檔名有沒有出現在 show.json 全文」推導的,所以每一組都並列了'
        '還活著的那個檔名 —— 按下去之前自己看一眼。'
        '<b>「無法歸屬」與「缺號」兩類永遠沒有按鈕</b> —— 缺號那類很可能是被扣下'
        '那一集的正本,只列出來讓你自己判斷。剛落地不到 30 分鐘的檔案也一律跳過'
        '(可能是發布進行中的新檔)。</div>'
        + (f'<div style="margin-bottom:20px">'
           + _act("delete-orphans", {"token": "*"},
                  f"刪掉全部 {len(feeds)} 個節目裡可安全歸屬的 {len(all_safe)} 個舊版檔"
                  f"({_size(safe_bytes)})。這一步救不回來。",
                  f"✕ 一次刪除全部可回收 · {len(all_safe)} 檔 · {_size(safe_bytes)}",
                  typed="刪除全部", cls="btn danger")
           + '</div>' if all_safe else "")
        + "".join(blocks))
    return _shell("可回收空間", body, orphan_total, 1080)


# ─────────────────────────── HTTP ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "podcast-dashboard"
    protocol_version = "HTTP/1.1"

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def handle(self):
        """對端把 keep-alive 連線直接 reset 時,`BaseHTTPRequestHandler` 會在讀下一個
        request line 時噴一整份 traceback 到 stderr。那不是錯誤(瀏覽器與 curl 都會
        這樣關連線),但會灌滿 container log,把真正的錯誤淹掉。"""
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def _host_ok(self) -> bool:
        """Host 只能是 IP 字面值(或 localhost)。

        DNS rebinding 的前提是攻擊者控制一個**網域名**,讓它解析到這台的 LAN IP;
        瀏覽器那時送的 Host 與 Origin 都是攻擊者的網域,於是「Origin == http://{Host}」
        變成自己跟自己比對、必然通過。把 Host 釘成 IP 就從根上關掉這條路,而且不需要
        任何設定檔知道自己的 IP 是什麼。真要用網域名就設 ALLOWED_HOSTS。

        **對 GET 也生效** —— rebinding 的第一步是用 GET 把 16 個 feed token 撈走。
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host in _ALLOWED_HOSTS:
            # 白名單上的網域名只有一條進來的路:Cloudflare Tunnel + Access。Access 會
            # 注入這顆標頭,所以「沒有它」就是「這個請求沒經過 Access」—— 政策被誤刪
            # 或設錯 host 時,原點自己 fail closed,而不是把 16 個 feed token 攤開。
            # ponytail: 只檢查標頭在不在,沒驗簽章。守的是設定錯誤這個真實故障模式;
            # 要擋「自己組一顆假標頭」的攻擊者,得抓 team domain 的 JWKS 驗 RS256。
            return bool(self.headers.get("Cf-Access-Jwt-Assertion"))
        if host == "localhost":
            return True
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    def _jout(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    #: 破壞性端點。全部 POST,全部要 X-Confirm 標頭,目標一律伺服器端重新推導。
    _API = {"delete-orphans", "trash-feed", "purge-trash"}

    def do_POST(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        parts = [p for p in path.split("/") if p]
        if len(parts) != 2 or parts[0] != "api" or parts[1] not in self._API:
            self._audit(path, {}, "拒絕:不存在的端點")
            return self._jout(404, {"error": "不存在的端點"})
        # CSRF:這頁沒有登入態,而 POST 可以刪檔。自訂標頭會逼出 CORS preflight,
        # 跨來源頁面送不出去;Origin 再比對一次。兩道都便宜。
        for bad, why in (
            (not self._host_ok(),
             "Host 必須是 IP,或白名單網域帶 Access JWT(見 ALLOWED_HOSTS)"),
            (self.headers.get("X-Confirm") != "1", "缺少確認標頭"),
            # 兩種 scheme 都收:內網直打是 http,經 tunnel 進來瀏覽器送的是 https。
            # 比對的仍然是「Origin 的 host 必須等於 Host」,擋跨來源的那一刀沒有變鈍。
            (bool(self.headers.get("Origin"))
             and self.headers.get("Origin") not in (
                 f"http://{self.headers.get('Host', '')}",
                 f"https://{self.headers.get('Host', '')}"),
             "跨來源請求"),
            (not writable(), "/srv 是唯讀掛載,這台不開放刪除"),
        ):
            if bad:
                self._audit(parts[1], {}, "拒絕:" + why)
                return self._jout(403, {"error": why})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            # 負數會讓 rfile.read(-1) 一路讀到 EOF —— 請求永遠不回應、執行緒卡住。
            if not 0 <= length <= 65536:
                raise ValueError("body 長度不合法")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body 必須是物件")
            # 非字串會讓 os.path.join / re.match 拋 TypeError —— 那不在 except 網裡,
            # 會冒到 socket 層變成「連線被關掉、完全沒有回應」。
            for key in ("token", "confirm", "entry"):
                if key in body and not isinstance(body[key], str):
                    raise ValueError(f"{key} 必須是字串")
            if parts[1] == "delete-orphans":
                ep = body.get("ep")
                if ep is not None and (isinstance(ep, bool) or not isinstance(ep, int)):
                    raise ValueError("ep 必須是整數")
                if body.get("token") == "*":
                    count = freed = skipped = 0
                    for f in scan_all():
                        try:
                            c, b = delete_orphans(f["token"], None)
                        except ValueError:
                            # 一個沒收斂的節目不該擋住其他 15 個。跳過並回報。
                            skipped += 1
                            continue
                        count += c
                        freed += b
                    msg = f"刪掉 {count} 個舊版檔,釋出 {_size(freed)}"
                    if skipped:
                        msg += f"({skipped} 個節目因為 feed.xml 沒跟上而跳過)"
                else:
                    count, freed = delete_orphans(body.get("token") or "", ep)
                    msg = f"刪掉 {count} 個舊版檔,釋出 {_size(freed)}"
            elif parts[1] == "trash-feed":
                entry = trash_feed(body.get("token") or "", body.get("confirm") or "")
                msg = f"整個節目已搬進垃圾桶:{_TRASH}/{entry}"
            else:
                msg = f"已永久刪除,釋出 {_size(purge_trash(body.get('entry') or ''))}"
        except ValueError as e:
            self._audit(parts[1], locals().get("body") or {}, f"拒絕:{e}")
            return self._jout(400, {"error": str(e)})
        except OSError as e:
            self._audit(parts[1], locals().get("body") or {}, f"檔案系統錯誤:{e}")
            return self._jout(500, {"error": f"檔案系統錯誤:{e}"})
        self._audit(parts[1], body, msg)
        return self._jout(200, {"msg": msg})

    def _file(self, tok: str, name: str, thumb: bool = False):
        """送檔,支援 HTTP Range。沒有 Range 就沒辦法拖進度條,而且每次請求會把
        37 MB 讀進 RAM —— 播放器是這個介面的主軸,這段不能省。"""
        if not (_SAFE_NAME.match(tok) and _SAFE_NAME.match(name)):
            return self._send(400, b"bad name", "text/plain")
        # 與 caddy 的 `respond /feeds/*/show.json 403` 對齊:它含 notebook_id。
        # 這頁沒有任何地方連到它,擋掉不會少功能。
        if name == "show.json":
            return self._send(403, b"internal state", "text/plain")
        path = os.path.join(ROOT, "feeds", tok, name)
        if not os.path.isfile(path):
            return self._send(404, b"not found", "text/plain")
        if thumb:
            data = _thumb(path, os.path.getmtime(path), _THUMB_PX)
            if data is not None:
                # 與下面同一條規則:只有 content-addressed 的檔名才 immutable。
                # 有 feed 的 live `artwork_file` 就是舊的固定檔名 `artwork.jpg`,
                # 給它一年快取 = 換了封面之後整整一年看到舊的。
                return self._send(200, data, "image/jpeg", {"Cache-Control": (
                    "public, max-age=31536000, immutable" if _HASHED.search(name)
                    else "no-cache")})
            # 沒有 Pillow:退回原圖。頁面仍然正確,只是首頁會很重。
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        cc = ("public, max-age=31536000, immutable" if _HASHED.search(name)
              else "no-cache")
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        partial = False
        if rng:
            # 位數上限:5000 個 9 會讓 int() 撞上 CPython 4300 位限制而拋
            # ValueError,每次請求灌一份 traceback 進 container log。
            m = re.fullmatch(r"bytes=(\d{0,19})-(\d{0,19})", rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                return self._send(416, b"", ctype,
                                  {"Content-Range": f"bytes */{size}"})
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:                                   # bytes=-N → 最後 N bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                return self._send(416, b"", ctype,
                                  {"Content-Range": f"bytes */{size}"})
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cc)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                # 分塊寫,不把整支 mp3 讀進記憶體。拖進度條會讓瀏覽器直接斷連,
                # 那是正常的,不是錯誤 —— 不接住就整頁 traceback。
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                if remaining:
                    # 檔案在 header 送出後被換短(發布中的 feed.xml 就會)。
                    # 框架已經對不上,別讓這條 keep-alive 連線被重用。
                    self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] == "healthz":
            return self._send(200, b"ok", "text/plain")
        if not self._host_ok():          # rebinding 的第一步是 GET 撈 token
            return self._send(403, b"bad host", "text/plain")
        if parts and parts[0] in ("f", "t") and len(parts) == 3:
            return self._file(parts[1], parts[2], thumb=parts[0] == "t")

        feeds = scan_all()
        orphan_total = sum(f["orphan_bytes"] for f in feeds)
        if not parts:
            return self._send(200, render_index(feeds, orphan_total))
        if parts[0] == "storage" and len(parts) == 1:
            return self._send(200, render_storage(feeds, orphan_total))
        if parts[0] == "trash" and len(parts) == 1:
            return self._send(200, render_trash(orphan_total))
        if parts[0] == "s" and len(parts) == 2:
            f = next((x for x in feeds if x["token"] == parts[1]), None)
            if f:
                return self._send(200, render_show(f, orphan_total))
        return self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        # GET 一律安靜:這頁自己一次載入就是十幾個請求,逐條記只會把真的錯誤淹掉。
        # 破壞性動作走 `_audit`,那個一定要留下軌跡 —— 見它的 docstring。
        pass

    def _audit(self, action: str, body: dict, outcome: str) -> None:
        """把破壞性動作寫進 container log。

        **這是唯一的稽核軌跡。** 舊版檔的刪除是硬刪、沒有備份,而 docker 的事件緩衝
        只留幾分鐘 —— 事後要回答「誰在什麼時候刪了什麼」只剩這裡。實際踩過:一輪
        瀏覽器測試把三個節目搬進垃圾桶,而因為沒有這條 log,無法逐一歸因。
        `confirm` 不記(那是使用者打進去的字串,沒有稽核價值)。

        **IP 那一欄不要當成 LAN 上的來源機器。** docker 的 port publishing 會 SNAT,
        容器看到的是 bridge gateway(例如 192.168.96.1),不是打進來的那台。有價值的是
        「什麼時候、對什麼、做了什麼、結果」;要真的來源 IP 得改 host network,而那會
        破壞「只綁 LAN IP」這個模型 —— 不值得。
        """
        sys.stderr.write("%s AUDIT %s %s %s -> %s\n" % (
            datetime.now().isoformat(timespec="seconds"),
            self.client_address[0] if self.client_address else "-", action,
            json.dumps({k: v for k, v in body.items() if k != "confirm"},
                       ensure_ascii=False, sort_keys=True), outcome))
        sys.stderr.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
