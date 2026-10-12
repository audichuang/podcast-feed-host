# worker/ — R2 為主、NAS 為備援

**狀態(2026-10-12)**:並行上線在 workers.dev,R2 bucket `podcast-feeds`,線上驗收 15 項通過。
`NAS_ORIGIN` 暫時指正式的 `podcast.audichuang.app`(Caddy);發布器與正式網域**都還沒切**。

```bash
cd worker && npm install
npm test && npm run typecheck   # 8 條離線測試
npm run build && npm run deploy  # cf deploy --prebuilt;UPLOAD_TOKEN 是 secret,設一次即可
```

```
發布器 ──PUT /feeds/<token>/<name>(Bearer)──→ Worker ──→ R2 podcast-feeds
訂閱者 ──GET/HEAD──→ Worker ──R2 有──→ 回 R2(Range 206 / ETag 304)
                            └─R2 沒有─→ NAS Caddy(podcast-nas.audichuang.app,經 tunnel)
NAS 每日 backup/r2-backup.sh:rclone copy R2 → NAS(完整備份,copy 不 sync)
                              再把 R2 修剪到 7 GB 以下(刪最舊的單集媒體)
```

**R2 是快取,NAS 是正本。** `backup/r2-backup.sh` 備份成功後,若 R2 總量超過 `CAP_BYTES`
(預設 7 GB = 免費額度 7 成),從最舊的開始刪,直到降回門檻以下。三道防線:只刪 `EPxx-*.mp3/pdf/html`
(`feed.xml`/`index.html`/封面永遠留);NAS 上沒有同大小的檔就跳過;備份失敗(`set -e`)就不刪。
被刪的檔由 Worker 回 NAS 拿,同時在 R2 留空標記 `_promote/<path>`(Worker 對外 404);隔天腳本
把它從 NAS **`rclone rcat`** 回 R2(不用 `copyto`:它帶著舊 mtime,剛補回就會被當最舊刪掉——本機模擬實測),
補回的就是最新的,接著才修剪 —— 等於「有人聽的舊集回到快取,擠掉更舊的」。修剪依 R2 上傳時間
(`--use-server-modtime`),所以一直有人聽但很久沒重傳的集也會被擠出去,下次有人聽再補回來(最多慢一天)。
測試:`sh backup/test_r2_backup.sh`(busybox 也過)。

- **介面與 `uploader/server.py` 相同**:路徑白名單、Bearer、每個回應帶 `X-Podcast-Uploader: 1`、成功 201。
  發布器只要把 `PODCAST_UPLOAD_URL` 改成 `https://podcast.audichuang.app`,程式不用動。
- **讀取規則與 `Caddyfile` 相同**:`show.json` 403、`/_trash/` 404,而且**在查 R2 / 回 NAS 之前**擋。
  Content-Type / Cache-Control 由副檔名決定,不信 R2 metadata(rclone 搬進來的物件 type 不可靠)。
- **NAS 備援只在「R2 沒有這個物件」時走**;R2 本身出錯回 500,不靜默改走 NAS。等 NAS 回 header 最多 10 秒。
- **上傳上限 100 MB**(zone 方案的 request body 上限,Workers Paid 不會放寬)。96k mono ≈ 2.3 小時,
  超過才需要 presigned URL;發布器先傳 mp3 才寫 `feed.xml`,所以超限是安全失敗。
- 測試:`node --test worker/test/index.test.ts`(8 條,離線,Node 24 原生跑 TS)。

## 上線步驟(每一步都要先問)

1. ✅ 建 R2 bucket `podcast-feeds`;`UPLOAD_TOKEN` 與 Doppler `notebooklm` 的 `PODCAST_UPLOAD_TOKEN` 相同(首次部署用 `--secrets-file` 帶入)。
2. (切換前)Tunnel 加 `podcast-nas.audichuang.app → http://<NAS LAN IP>:8085`。
   ⚠️ `PUT …/cfd_tunnel/<id>/configurations` 是**整份覆蓋**:先 GET 存檔、再加一條。
3. ✅ **不需要搬舊檔**:R2 是快取,沒有的檔 Worker 會回 NAS 拿;之後發布的才進 R2。
   (舊集重發時會照 96k 重壓、換新檔名,自然進 R2。)
4. ✅ 部署 Worker 到 workers.dev 先驗:Range 206、HEAD、feed.xml type、show.json 403、R2 刪一個檔看 NAS 備援。
5. `NAS_ORIGIN` 改成 `https://podcast-nas.audichuang.app` 並重新部署,**然後**把 `podcast.audichuang.app` 從 tunnel 切到 Worker custom domain(順序反了會讓 Worker 回源打到自己)。回滾 = 改回 tunnel 那條。
6. 發布機的 `PODCAST_UPLOAD_URL`(Doppler `notebooklm`)改成新網址;舊 uploader 保留到確認沒問題。
7. NAS 加 `backup/r2-backup.sh` 每日排程(rclone 容器,走 nas-ops + Komodo;R2 金鑰放 Periphery `[secrets]`)。

## 待決定

- **清空間要在 R2 清**:NAS 是 `rclone copy` 的備份,dashboard 在 NAS 刪的檔隔天又會被拉回來。
  dashboard 之後是改成「NAS 備份的唯讀檢視」,還是加 R2 刪除功能?
- uploader 容器要不要在切換穩定後下線(或保留當 R2 掛掉時的手動備援寫入口)。
