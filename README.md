# podcast-feed-host

在 NAS 上把一個 podcast RSS feed 目錄透過 **Cloudflare Tunnel** 對外以 HTTPS 靜態
服務,讓 Apple Podcast(或任何 podcast app)用 feed URL 訂閱。**NAS 不用開 router
port、不用固定 IP、不用自己弄 TLS 憑證**——TLS 由 Cloudflare 終結。

Caddy 靜態服務(Caddyfile bake 進 image,由 GitHub Actions build 後推到 GHCR)+
官方 `cloudflared`,兩個容器一份 `docker compose`。

```
你的 feed 產生器寫檔 → $FEEDS_ROOT_HOST/feeds/<token>/{feed.xml, *.mp3, artwork, index.html}
                        ↑ bind-mount(唯讀)
                      Caddy(compose 內 :80,不對 host 開 port)
                        ↑ http://caddy:80
                      cloudflared ──出站──→ Cloudflare ──HTTPS──→ https://你的域名/feeds/<token>/feed.xml
```

> 這個 repo 只負責**托管**。feed 檔(`feed.xml` / `*.mp3` / 封面)由你的產生器寫進
> `FEEDS_ROOT_HOST`;本服務只讀不寫。

## 快速開始(NAS 端)

```bash
git clone https://github.com/audichuang/podcast-feed-host.git
cd podcast-feed-host
cp .env.example .env
# 編輯 .env:
#   FEEDS_ROOT_HOST=/volume1/podcasts   # NAS 上放 feed 的目錄
#   TUNNEL_TOKEN=eyJ...                  # 見下方 Cloudflare Tunnel
docker compose up -d
docker compose logs -f cloudflared      # 看到 "Registered tunnel connection" 即成功
```

compose 用的是 GHCR 上預先 build 好的 image(`pull_policy: always`),所以 NAS 端
**不需要 build**;之後更新只要 `docker compose pull && docker compose up -d`。

## Cloudflare Tunnel 設定(拿 TUNNEL_TOKEN)

1. Cloudflare **Zero Trust dashboard → Networks → Tunnels → Create a tunnel** →
   選 **Cloudflared** → 命名(例 `podcast`)。
2. 安裝畫面選 **Docker**,複製指令裡 `--token` 後那串,填進 `.env` 的 `TUNNEL_TOKEN`。
3. **Public Hostname → Add**:
   - Subdomain/Domain:例 `podcast` + 你的域名 → 對外 `https://podcast.你的域名`
   - Service:**HTTP** `caddy:80`(compose 服務名,cloudflared 走內部網路連得到)
   - 存檔。

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
