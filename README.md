# podcast-feed-host

在 NAS 上把一個 podcast RSS feed 目錄以 HTTPS 靜態服務,讓 Apple Podcast(或任何
podcast app)用 feed URL 訂閱。對外走 **Cloudflare Tunnel**,所以 **NAS 不用開
router port、不用固定 IP、不用自己弄 TLS 憑證**。讀寫分離:**Caddy**(讀,唯讀掛載,
經 tunnel 對外)+ **uploader**(寫,token 保護,只綁內網 LAN IP,**絕不**進 tunnel)。

```
MCP publish_series ──內網 HTTP PUT(Bearer token)──→ uploader 容器(綁 ${UPLOAD_BIND}:${UPLOAD_PORT})
                                                        ↓ 原子寫
                                                     $FEEDS_ROOT_HOST/feeds/<token>/{feed.xml, *.mp3, artwork, index.html}
                                                        ↑ bind-mount(唯讀)
                                                     Caddy 容器(host:${HOST_PORT} → 容器內 :80)
                                                        ↑ http://<這台機器內網IP>:${HOST_PORT}
                                                     你既有的 cloudflared ──→ Cloudflare ──HTTPS──→ https://你的域名/feeds/<token>/feed.xml
```

> **為什麼還要 Caddy?** cloudflared 只負責「轉發」一個公開網址到內網位址,它本身不會
> serve 檔案。所以背後需要一個真的 web server 把 `feed.xml` / `*.mp3` 吐出來——Caddy
> 就是幹這個,而且原生支援 mp3 續播需要的 Range(HTTP 206)與正確 MIME / cache header。
>
> **為什麼另外一個 uploader?** Caddy 只唯讀掛載,不能接受寫入。`uploader/` 是一支
> stdlib-only 的 token 保護 HTTP 服務,專門接收 MCP 端的 PUT,寫進同一個目錄。
> **uploader 只綁 NAS 的 LAN IP、不要加進 Cloudflare Tunnel ingress、並在防火牆把該
> port 擋掉 WAN**——寫埠一旦外曝等於任何人都能覆寫你的 feed。

## 快速開始(NAS 端)

```bash
git clone https://github.com/audichuang/podcast-feed-host.git
cd podcast-feed-host
cp .env.example .env
# 編輯 .env:
#   FEEDS_ROOT_HOST=/volume1/podcasts   # NAS 上放 feed 的目錄
#   HOST_PORT=8085                      # Caddy 對外的 host port(被佔就改)
#   UPLOAD_BIND=192.168.x.x             # NAS 的 LAN IP(不要填 0.0.0.0)
#   UPLOAD_PORT=8086                    # uploader 對內的 host port
#   UPLOAD_TOKEN=                       # 與 Doppler PODCAST_UPLOAD_TOKEN 逐字元相同
docker compose up -d
curl -s http://localhost:${HOST_PORT:-8085}/healthz   # 回 200 即讀站服務正常
```

用的是 GHCR 上預先 build 好的 image,NAS **不需 build**。

**自動更新**:compose 內建一個 **watchtower** 服務,每 5 分鐘 poll GHCR,image 有新版就自動
pull + 重啟(只動貼了 `watchtower.enable` label 的 **caddy / dashboard**,不會誤動 NAS 上
其他 stack)。**`uploader` 刻意沒貼那個 label** —— 它是發布器唯一的寫入口,在發布中途被重建
就是那一季壞掉,而 CI matrix 每次 push 都重建三個 image(metadata 帶 commit SHA,digest 一定
不同),所以它可能因為一個跟它無關的改動被重建。更新它是手動的:
`docker compose pull uploader && docker compose up -d --no-deps uploader`,挑沒有發布在跑的時候。注意 `pull_policy: always` **本身不會**自動更新——它只在 `docker compose up` 當下 pull
一次,所以自動更新是靠 watchtower。想改手動就把 watchtower 服務刪掉,自己
`docker compose pull && docker compose up -d`。

## 接上你既有的 Cloudflare Tunnel

在你現有的 tunnel 加一條 ingress,指到這台機器的 `${HOST_PORT}`。依你 tunnel 的管理
方式二選一:

**A. Dashboard 管理(remote config)**
Zero Trust → Networks → Tunnels → 你的 tunnel → **Public Hostname → Add**:
- Subdomain/Domain:例 `podcast` + 你的域名 → 對外 `https://podcast.你的域名`
- Service:**HTTP**,URL `<這台機器內網IP>:8085`(= `HOST_PORT`)

**B. 本地 `config.yml`(local config)**
在 `ingress:` 加一條(放在 `service: http_status:404` 那條**之前**):
```yaml
ingress:
  - hostname: podcast.你的域名
    service: http://<這台機器內網IP>:8085
  - service: http_status:404
```
改完 `cloudflared` 重啟。

> ⚠️ **別用 `localhost`**:如果你的 cloudflared 是跑在 docker 容器裡,`localhost` 指的是
> 它自己、連不到 Caddy。用**這台機器的內網 IP**(例 `192.168.x.x:8085`)最不會錯,不管
> cloudflared 是裝在主機還是容器都通。

## 部署驗收

**讀(經 tunnel)**:
```bash
BASE=https://podcast.你的域名
curl -sI "$BASE/feeds/<token>/feed.xml" | grep -i content-type   # application/rss+xml
MP3="$BASE/feeds/<token>/EP01-xxxxxxxx.mp3"
curl -sI "$MP3" | grep -i content-type                           # audio/mpeg
curl -sI -r 0-1 "$MP3" | head -1                                 # 206 Partial Content(續播關鍵)
curl -s "$BASE/healthz"                                          # 200
curl -sI "$BASE/feeds/<token>/show.json"                         # 403(內部稽核狀態,不對外)
```

**寫(內網直打,不經 tunnel)**:
```bash
WRITE=http://<NAS的LAN IP>:8086
TOK=<UPLOAD_TOKEN>

curl -sI "$WRITE/healthz" | grep -i x-podcast-uploader        # 不帶 token → 200,含 marker
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOK" "$WRITE/healthz"
#   帶正確 Bearer → 200
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer wrong" "$WRITE/healthz"
#   帶錯 Bearer → 401
curl -s -o /dev/null -w "%{http_code}\n" -X PUT "$WRITE/feeds/<token>/feed.xml"  # 無 token PUT → 401
```

## 目錄結構(產生器要寫成這樣)

```
$FEEDS_ROOT_HOST/
  feeds/
    <token>/
      feed.xml          # RSS 2.0 + iTunes namespace
      index.html        # 節目頁(RSS <link> 指向)
      artwork.png|.jpg  # 節目封面
      EP01-<hash8>.mp3  # 內容版本化檔名(immutable)
      EP02-<hash8>.mp3
```

`<token>` 是不可猜的隨機 slug,讓 feed 不公開列出但可直接訂閱。

## 管理後端(dashboard)

`http://<NAS的LAN IP>:8087/` —— 把同一個 feeds 目錄唯讀掛進來,從每個
`feeds/<token>/show.json` 推導出全部節目與單集,渲染成像真的 podcast App 的介面:
封面牆 → 節目頁 → 單集列表 → show notes → 底部常駐播放器(原生 `<audio>`,支援拖曳)。

```
/                 節目牆 + 全站統計 + 最近發布 12 集
/s/<token>        節目頁:hero / 健康橫幅 / 單集列表(展開看 show notes 與附件)
/storage          可回收空間:被取代的舊版檔,依集分組,可直接刪除(見下)
/trash            垃圾桶:被刪掉的整個節目,可還原或永久刪除
/f/<token>/<name> 位元組(支援 HTTP Range,拖曳進度條要靠它)
/t/<token>/<name> 同一張圖的 320px 縮圖(封面都走這條,見下)
/healthz          200 ok
```

**⚠️ 這頁把每一個 feed token 列出來,而 token 就是那些未公開列出的 feed 的唯一存取
控制。** 所以 port 只綁 NAS 的 LAN IP,而它要對外**只有一條合法路徑:Cloudflare Access
後面的 tunnel hostname**(見下)。Host 必須是 IP 字面值(擋 DNS rebinding),要用網域名
得設 `ALLOWED_HOSTS` —— 而白名單上的網域名額外強制要帶 Access 的 JWT 標頭。

### 經 Cloudflare Tunnel + Access 對外

三樣缺一不可,順序也別反(先 Access,再開白名單 —— 中間那段空窗原點是 403,不是敞開):

1. **Access application**(Zero Trust → Access → Applications → Self-hosted):
   domain = 該 hostname,政策 `Allow` + Include `Emails` = 你的信箱,登入方式
   **One-time PIN**(email OTP,不需要接任何 IdP)。
2. **Tunnel public hostname**:該 hostname → `http://<NAS LAN IP>:8087`(DNS 那筆
   CNAME 指向 `<tunnel-id>.cfargotunnel.com`,proxied)。
3. **`DASH_ALLOWED_HOSTS=<該 hostname>`** 寫進 dashboard 的環境變數並 restart。

`_host_ok` 對白名單網域**強制要有 `Cf-Access-Jwt-Assertion`**(Access 注入的),所以
政策被誤刪時原點自己 fail closed。刪除功能在外網也能用:Origin 檢查同時收 `http://`
與 `https://`(經 tunnel 進來的是後者),比對的仍然是「Origin 的 host == Host」。

驗收(不帶 cookie 應該看到 Access 的 302,而不是頁面):

```bash
curl -sI https://<hostname>/ | head -3          # 期待 302 → *.cloudflareaccess.com
curl -s -o /dev/null -w '%{http_code}\n' https://<hostname>/healthz   # 302,連 /healthz 都被擋
```

實作是**單一 stdlib-only Python 檔**(`dashboard/server.py`),烤進 image,
更新走 CI → GHCR → watchtower(跟 caddy 同一條路;`uploader` 是手動的,見上)。
離線可測:`python3 dashboard/test_server.py`(31 個測試,無第三方相依)。

**怎麼確認 NAS 上跑的是哪一版程式?** 沒有版本端點,兩招都不會改到任何東西:打一次
`curl -s -X POST -H 'Host: evil.example' http://<NAS的LAN IP>:8087/api/delete-orphans`,看回傳的
guard 訊息是不是這一版的字串(它只會寫一行稽核 log,不碰檔案);要更硬就把首頁的 inline
`<style>`/`<script>` 抓下來,跟 `server.py` 的 `_CSS`/`_JS` 逐位元組比 hash —— 一致就是這份
code 烤出來的 image 在跑。

**每個破壞性動作都會寫一行稽核 log 到 container log**(時間、client IP、動作、body、結果),
含每一道被擋下的 guard;GET 一律安靜。刪除是硬刪、沒有備份,而 docker 的事件緩衝只留幾分鐘 ——
`docker logs podcast-feed-host-dashboard-1 | grep AUDIT` 是事後唯一能回答「誰刪了什麼」的地方。

唯一的相依是 **Pillow,而且只為了縮圖**:節目封面是 Apple 規格的 3000×3000
(每張 0.5–8.7 MB),畫面上最大只用到 160 px。首頁 16 張原圖 = 24 MB 傳輸 +
**529 MB 解碼**,實測瀏覽器會直接放棄畫後面幾張。所以 `/t/<token>/<name>` 會回
320px 的 JPEG(`lru_cache` 在記憶體,`Image.draft()` 走 JPEG 的 1/8 DCT 尺度解碼),
首頁降到 **0.3 MB / 6.5 MB**。**Pillow 不在時 server.py 自動退回原圖**,頁面仍然
正確,只是很重 —— 所以它是選配相依,不是硬需求。

它會做五項健康檢查(全部只用已經做過的 `stat`,不讀媒體位元組):引用的檔案在不在磁碟上、
`enclosure` 長度對不對、`pubDate` 有沒有隨集號遞增、GUID 對不對得上 `show_id`、
`feed.xml` 有沒有跟上 `show.json`。

### 刪除

確認對話框是**頁面內的 `<dialog>` 元件**,不是 `confirm()`/`prompt()` —— 原生的會被瀏覽器
加上「192.168.31.105:8087 says」前綴、樣式不受控,而且 iPhone 上輸入框會自動大寫(要打的是
`zz-test-v060` 這種 show_id)。backdrop、ESC 關閉、焦點鎖定都是 `<dialog>` 給的,沒有函式庫;
`showModal` 不存在時才退回原生。需要輸入確認的動作,**打對字串之前確定鍵按不下去**。

**能不能刪由掛載決定**:`/srv` 掛 `:rw` 才有按鈕,掛 `:ro` 就整組不渲染、端點也直接回 403。
要緊急關掉刪除功能,把 compose 改回 `:ro` 再 restart 就好,不用動程式。

| 動作 | 可逆? | 閘 |
|---|---|---|
| 刪某一集的舊版檔 / 某節目全部舊版 | 否(直接 unlink) | `confirm()` |
| 一次刪除全站可回收 | 否 | 要打 `刪除全部` |
| 刪除整個節目 | **是** —— 搬進 `_trash/`,還原是一行 `mv` | 要打該節目的 `show_id` |
| 垃圾桶裡永久刪除 | 否 | 要打該節目的 `show_id` |

舊版檔是**直接硬刪不進垃圾桶** —— 進了就等於沒回收到空間,而它們本來就不在任何 feed 裡。
安全性靠的是這幾條,不是靠「小心一點」:

- **前端指定不了檔名。** 只送 `token`(+集號),要刪哪些一律由 `safe_orphans()` 在
  伺服器端重新推導。只收「該集還在 `show.json` 的舊版」與「舊節目封面」兩類;
  **「無法歸屬」與「缺號」永遠不收** —— 缺號那類很可能是被扣下那一集的正本。
- **還在飛的檔案不碰。** 發布器是「先 PUT 全部媒體、最後才 PUT `show.json`」,所以發布
  進行中的新檔在 `show.json` 更新前看起來就是孤兒。判準有兩條:30 分鐘緩衝,**以及
  「mtime 比 `show.json` 新」** —— 後者才是精確的那條(一季 50 集的重編碼上傳遠超
  30 分鐘,對抗性測試實測有檔案落地 31 分鐘後才被 `show.json` 引用,只靠緩衝窗會刪掉它)。
  (寫這段的當下 NAS 上剛好在跑一次 Audicast 重發,216 個檔案在 30 分鐘內落地 ——
  這個競態是真的。)
- **訂閱者抓的是 `feed.xml`,不是 `show.json`。** 發布器是三個獨立 PUT(媒體 →
  `show.json` → `feed.xml`),中斷在 `show.json` 之後會留下持久的 desync:`show.json`
  已是新版,而上線中的 `feed.xml` 還指著舊檔名。所以 `feed.xml` 與 `index.html` 的全文
  也一起餵進同一條 regex —— 被保護的正好就是訂閱者實際會抓的東西。
  而且這種狀態(健康檢查 H5)下**整個節目不開放清理**,回收頁會直接說明原因;
  批次清理會跳過它、不會因為它中止其他節目。
- **Host 必須是 IP。** 只比對 `Origin == http://{Host}` 是不夠的 —— DNS rebinding 下
  Host 由攻擊者控制,等於自己跟自己比對。釘成 IP 字面值就從根上關掉那條路,連 GET
  都擋(rebinding 的第一步是用 GET 把 16 個 token 撈走)。要用網域名設 `ALLOWED_HOSTS`。
- **沒有「刪除單集」。** 單集還在 `feed.xml` 裡,直接刪檔會讓訂閱者的 enclosure 指向 404。
  要下架某一集請走 publisher(扣下該集 + 重發),那會同時改寫 `show.json` 與 `feed.xml`。
- POST-only、`X-Confirm` 自訂標頭(逼出 CORS preflight,而這支 server 沒有 `do_OPTIONS`,
  跨來源的 preflight 一定失敗)、`Origin` 比對。這頁沒有登入態,不能讓別的網站 drive-by。
- `_feed_file` 拒絕 symlink 與非普通檔案;`purge_trash` 是全程式唯一的遞迴刪除,
  entry 名稱形狀、realpath 父目錄、非 symlink 三道都驗。

驗收方式是拿**真實的 16 份 `show.json`** 配上同名的 1-byte 假媒體檔造沙箱(9 MB),
在沙箱上跑全站刪除,然後驗一條不變量:**`show.json` 全文提到的每一個檔案都必須還在**。
實測 2075 → 916 個檔案,915 個被引用的一個都沒少。

## CI/CD

`.github/workflows/build.yml`:改到 `Dockerfile` / `Caddyfile` / `uploader/**` / `dashboard/**` push 到
`main`(或手動 `workflow_dispatch`)→ matrix **三個** image、各自多架構(amd64 + arm64)
build → 推 `ghcr.io/audichuang/podcast-feed-host:latest`(讀)、
`ghcr.io/audichuang/podcast-feed-uploader:latest`(寫)與
`ghcr.io/audichuang/podcast-feed-dashboard:latest`(管理後端)。

> **一次性(三個 package 都要做)**:首次 build 後,到 GitHub → repo → Packages →
> `podcast-feed-host`、`podcast-feed-uploader`、`podcast-feed-dashboard` → 各自的
> Package settings 把 visibility 設為 **Public**,NAS 才能免登入 `docker compose pull`。
> (或在 NAS 上 `docker login ghcr.io` 用個人 PAT。)

## License

MIT
