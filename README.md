# podcast-feed-host

在 NAS 上把一個 podcast RSS feed 目錄以 HTTPS 靜態服務,讓 Apple Podcast(或任何
podcast app)用 feed URL 訂閱。對外走 **Cloudflare Tunnel**,所以 **NAS 不用開
router port、不用固定 IP、不用自己弄 TLS 憑證**。

這個 repo 只有一個 **Caddy** 靜態服務(Caddyfile bake 進 image,由 GitHub Actions
build 後推到 GHCR)。它假設**你已經在跑自己的 `cloudflared`**——本服務只要被你既有的
tunnel 指過來即可,不自帶 cloudflared。

```
你的 feed 產生器寫檔 → $FEEDS_ROOT_HOST/feeds/<token>/{feed.xml, *.mp3, artwork, index.html}
                        ↑ bind-mount(唯讀)
                      Caddy 容器(host:${HOST_PORT} → 容器內 :80)
                        ↑ http://<這台機器內網IP>:${HOST_PORT}
                      你既有的 cloudflared ──→ Cloudflare ──HTTPS──→ https://你的域名/feeds/<token>/feed.xml
```

> **為什麼還要 Caddy?** cloudflared 只負責「轉發」一個公開網址到內網位址,它本身不會
> serve 檔案。所以背後需要一個真的 web server 把 `feed.xml` / `*.mp3` 吐出來——Caddy
> 就是幹這個,而且原生支援 mp3 續播需要的 Range(HTTP 206)與正確 MIME / cache header。
>
> 這個 repo 只負責**托管**;feed 檔由你的產生器寫進 `FEEDS_ROOT_HOST`,本服務只讀不寫。

## 快速開始(NAS 端)

```bash
git clone https://github.com/audichuang/podcast-feed-host.git
cd podcast-feed-host
cp .env.example .env
# 編輯 .env:
#   FEEDS_ROOT_HOST=/volume1/podcasts   # NAS 上放 feed 的目錄
#   HOST_PORT=8080                      # Caddy 對外的 host port(被佔就改)
docker compose up -d
curl -s http://localhost:${HOST_PORT:-8080}/healthz   # 回 200 即服務正常
```

用的是 GHCR 上預先 build 好的 image(`pull_policy: always`),NAS **不需 build**;
更新只要 `docker compose pull && docker compose up -d`。

## 接上你既有的 Cloudflare Tunnel

在你現有的 tunnel 加一條 ingress,指到這台機器的 `${HOST_PORT}`。依你 tunnel 的管理
方式二選一:

**A. Dashboard 管理(remote config)**
Zero Trust → Networks → Tunnels → 你的 tunnel → **Public Hostname → Add**:
- Subdomain/Domain:例 `podcast` + 你的域名 → 對外 `https://podcast.你的域名`
- Service:**HTTP**,URL `<這台機器內網IP>:8080`

**B. 本地 `config.yml`(local config)**
在 `ingress:` 加一條(放在 `service: http_status:404` 那條**之前**):
```yaml
ingress:
  - hostname: podcast.你的域名
    service: http://<這台機器內網IP>:8080
  - service: http_status:404
```
改完 `cloudflared` 重啟。

> ⚠️ **別用 `localhost`**:如果你的 cloudflared 是跑在 docker 容器裡,`localhost` 指的是
> 它自己、連不到 Caddy。用**這台機器的內網 IP**(例 `192.168.x.x:8080`)最不會錯,不管
> cloudflared 是裝在主機還是容器都通。

## 部署驗收

```bash
BASE=https://podcast.你的域名
curl -sI "$BASE/feeds/<token>/feed.xml" | grep -i content-type   # application/rss+xml
MP3="$BASE/feeds/<token>/EP01-xxxxxxxx.mp3"
curl -sI "$MP3" | grep -i content-type                           # audio/mpeg
curl -sI -r 0-1 "$MP3" | head -1                                 # 206 Partial Content(續播關鍵)
curl -s "$BASE/healthz"                                          # 200
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

## CI/CD

`.github/workflows/build.yml`:改到 `Dockerfile` / `Caddyfile` push 到 `main`(或手動
`workflow_dispatch`)→ 多架構(amd64 + arm64)build → 推 `ghcr.io/audichuang/podcast-feed-host:latest`。

> **一次性**:首次 build 後,到 GitHub → repo → Packages → `podcast-feed-host` →
> Package settings 把 visibility 設為 **Public**,NAS 才能免登入 `docker compose pull`。
> (或在 NAS 上 `docker login ghcr.io` 用個人 PAT。)

## License

MIT
