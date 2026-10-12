# worker/ — R2 為主、NAS 為備援

**狀態(2026-10-12 上線)**:`podcast.audichuang.app/*` 已是這個 Worker(zone route);R2 bucket
`podcast-feeds`;NAS 回源走 `podcast-nas.audichuang.app`(tunnel → caddy :8085);發布器
`PODCAST_UPLOAD_URL`(Doppler `notebooklm` dev/stg/prd)已改成 `https://podcast.audichuang.app`;
NAS 每日備份是 nas-ops 的 `r2-backup` service。**回滾**:拿掉 `cloudflare.config.ts` 的 route 重新部署
(流量立刻回 tunnel),Doppler 改回舊的 uploader 網址(本機備份 `~/tunnel-backups/`),**並停掉
`r2-backup`**——它每天用 R2 的 `feed.xml` 等小檔覆蓋 NAS,回滾後 R2 的版本會比 NAS 舊。

```bash
cd worker && npm install
npm test && npm run typecheck && npm run lint   # 15 條離線測試 + oxlint
npm run build && npm run deploy  # cf deploy --prebuilt;UPLOAD_TOKEN 是 secret,設一次即可
```

```
發布器 ──PUT /feeds/<token>/<name>(Bearer)──→ Worker ──→ R2 podcast-feeds
訂閱者 ──GET/HEAD──→ Worker ──R2 有──→ 回 R2(Range 206 / ETag 304)
                            └─R2 沒有─→ NAS Caddy(podcast-nas.audichuang.app,經 tunnel)
NAS 每日 04:30 r2-backup:R2 → NAS 備份、補回被聽的舊集、把 R2 修剪到 7 GB 以下
                         (全經 Worker 的 /_admin API,憑證只有同一把 UPLOAD_TOKEN)
```

**R2 是快取,NAS 是正本。** `backup/r2-backup.sh`(nas-ops `r2-backup` service,curl 映像):
1. **備份**:`GET /_admin/list` 列出 R2,NAS 缺的單集媒體(同名同大小即相同,檔名含內容 hash)下載;
   `feed.xml`/`index.html`/`show.json` 等小檔每次重抓(會變)。只寫 `DEST/feeds/<token>/<檔名>`,
   key 不合格式就跳過。**NAS 上從不刪**。
2. **補回**:Worker 回源 NAS 且**真的送出 ≥ 1 MB 的 GET** 時,在 R2 留空標記 `_promote/<path>`
   (HEAD、`bytes=0-1` 這類爬蟲驗證不算——不然每天所有舊集都會被標記,快取來回震盪;上線前實測踩到)。
   腳本把標記的集從 NAS 重新 PUT 進 R2,上傳時間變最新;超過 100 MB 的舊 256k 檔會 413,留在 NAS。
3. **修剪**:重新列出 R2,總量超過 `CAP_BYTES`(預設 7 GB)就從上傳時間最舊的開始刪,只刪
   `EPxx-*.mp3/pdf/html` 且 NAS 有同大小的檔。任何一步失敗(`set -e`,每輪獨立行程)就不會走到修剪。

管理 API(`Authorization: Bearer <UPLOAD_TOKEN>`):`GET /_admin/list`(`uploaded|size|key`)、
`GET|DELETE /_admin/object/<key>`。這把 token 本來就能覆寫每個 feed,刪除沒有增加破壞面。
測試:`sh backup/test_r2_backup.sh`(busybox 也過)。手動跑一輪:
`docker exec podcast-feed-host-r2-backup-1 sh /r2-backup.sh once`。

- **寫入介面與 `uploader/server.py` 相同**:路徑白名單、Bearer、每個回應帶 `X-Podcast-Uploader: 1`、成功 201。
  PUT 在讀取的 deny 規則**之前**處理(發布器每次都 PUT `show.json`;上線前實測曾被 403 擋)。
- **讀取規則與 `Caddyfile` 相同**:`show.json` 403、`/_trash/`、`/_promote/` 404,在查 R2 / 回 NAS 之前擋。
  Content-Type / Cache-Control 由副檔名決定,不信 R2 metadata。
- **NAS 備援只在「R2 沒有這個物件」時走**;R2 本身出錯回 500(並寫 log),不靜默改走 NAS。等 NAS 回
  header 最多 30 秒(忙碌或硬碟喚醒時實測單筆到 19 秒)。回源請求 `cache: "no-store"`:Caddy 把媒體標成
  一年 immutable,若讓邊緣快取,下架的集會在 `podcast-nas` 的快取裡多活一年,每個機房也會各從 NAS 拉一份。
  `If-Range` 轉送給 NAS,轉址(`/feeds/<t>` → `/feeds/<t>/`)原樣交給客戶端。
- **R2 路徑的 HTTP 語意對齊 Caddy**:只認單一 Range(多段 / 不合法 → 200 全檔);超出 EOF → 416 帶
  `Content-Range: bytes */size`;`If-Range` 不符 → 200 全檔;`If-None-Match` → 304(GET 與 HEAD);
  `If-Match` / `If-Unmodified-Since` 不成立 → 412;帶 `Last-Modified`。同一集在 R2 與 NAS 的 ETag 不同
  (MD5 vs Caddy),搬家時客戶端最多重抓一次。
- **100 MB 的 413 是 Cloudflare 邊緣回的**,不經 Worker,所以那個回應沒有 `X-Podcast-Uploader`。
- **上傳上限 100 MB**(zone 方案的 request body 上限,Workers Paid 不會放寬)。96k mono ≈ 2.3 小時;
  發布器先傳 mp3 才寫 `feed.xml`,所以超限是安全失敗。
- 測試:`node --test worker/test/index.test.ts`(15 條,離線,Node 24 原生跑 TS);lint:`npm run lint`(oxlint,correctness / suspicious / perf 全設 error)。

## 待決定

- **清空間要在 R2 清**:NAS 是備份(只增不刪),dashboard 在 NAS 刪的檔隔天又會被拉回來。
  dashboard 之後是改成「NAS 備份的唯讀檢視」,還是加 R2 刪除功能?
- uploader 容器要不要在切換穩定後下線(或保留當 R2 掛掉時的手動備援寫入口)。
