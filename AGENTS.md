# AGENTS.md — podcast-feed-host

在 NAS 上把 podcast RSS feed 目錄對外服務的三個容器。**架構、安裝、驗收指令、目錄結構、
dashboard 的刪除判準,全部在 [README.md](README.md)** ——本檔只寫 README 與程式碼給不了的
意圖與地雷。工作根 `../AGENTS.md` 的跨專案規則(secrets → Doppler、commit/push 要明講)照舊生效。

## 核心不變式:讀寫分離

三個 service 掛**同一個** feed 目錄,靠掛載模式與綁定介面分權,不靠自律:

| service | 掛載 | 綁定 | 角色 |
|---|---|---|---|
| caddy | `:ro` | host port,經 tunnel 對外 | 訂閱者實際抓 feed 的那一個 |
| uploader | rw(無後綴=預設) | **LAN IP only**,token 保護 | 發布器唯一的寫入口 |
| dashboard | `:rw` | **LAN IP only**,無登入 | 管理後端(刪除功能由掛載決定,見下) |

**新增任何 service,先回答兩題:掛 `:ro` 還是 `:rw`、綁 `0.0.0.0` 還是 LAN IP。** 答不出來就別加。

- **8086 / 8087 絕不加進 Cloudflare Tunnel ingress。** uploader 是寫入端;dashboard 會把
  **每一個 feed token 列出來**,而 token(`HMAC(salt, show_id)`、不輪替)就是那些未公開列出的
  feed 唯一的存取控制。一次外洩等於永久公開。
- **dashboard 的刪除能力 = `/srv` 的掛載模式**(`writable()` 就是 `os.access(ROOT, W_OK)`)。
  要緊急關掉全部破壞性動作:compose 改回 `:ro` + restart,不必動程式。

## caddy 與 uploader 是既有生產服務

- caddy 斷 → 訂閱者抓不到;uploader 斷 → **正在跑的那一季發布會壞掉**。
- **動它們之前先確認沒有發布在跑**:比對每個 feed 的 `feed.xml` mtime 與該目錄最新媒體檔的
  mtime,有檔案比 `feed.xml` 新就是還在飛。(發布器的順序是「全部媒體 → show.json → feed.xml」。)
- **改 `compose.yaml` 這個動作本身可能讓 DSM Container Manager 重新套用整個專案。**
  2026-08-23 實際觀察到 uploader 在一次 compose 編輯後重啟(docker 事件緩衝已滾掉、未能證實
  元兇,`RestartCount=0` + `/proc/1/stat` 證實行程真的換過)。所以**改 compose 挑沒有發布在跑的時候**。

## NAS 上的部署與同步不變式

- NAS 跑的是 `/volume5/docker/podcast/compose.yaml` —— 它是 repo 這份 `docker-compose.yml` 的
  「值已內嵌」版本(port、絕對路徑、`UPLOAD_TOKEN` 都寫死,沒有 `.env`)。
  **改 repo 這份不會生效**,反之亦然。
- **不變式:兩份的 service / 掛載 / port 結構必須一致。** 值本來就每台不同(走各自的 `.env`
  或內嵌),但「有哪些 service、各自掛什麼、綁哪個介面」不准分歧 —— 一旦分歧,下一個人會照
  repo 這份推理生產環境。改任一邊都要同步另一邊。
- 未收斂項:讓 NAS 直接用 repo 的檔 + 一份 `.env`。那需要重建整個 stack(公開讀取短暫中斷),
  還沒做。
- **`Caddyfile` 生效的是掛載那份**(`compose` 的 `./Caddyfile:/etc/caddy/Caddyfile:ro`);
  image 裡也 `COPY` 了一份當預設。改 Caddyfile → 同步到 NAS + `restart caddy`,不必等 CI。
  兩份保持一致。
- **三個 image 都由 CI 建、NAS 只 pull。** 沒有本機 build、沒有 bind-mount 程式碼。
  `dashboard/server.py` 是烤進 image 的 —— **改了程式要 push 讓 CI 建**,在 NAS 上覆蓋
  檔案不會生效(也不該掛進去:一份過期的本機檔會靜默蓋掉 CI 建出來的版本)。
- **但只有 caddy 與 dashboard 貼 watchtower label,uploader 刻意沒貼。** CI matrix 每次
  push 都重建三個 image,而 metadata 帶 commit SHA → digest 一定不同 → 貼了 label 的服務
  會被無人值守地重建。caddy 重建是一秒的讀取空窗、dashboard 只有你在看,都無所謂;
  **uploader 在發布中途被重建就是那一季壞掉**,而它可能因為一個跟它無關的改動被重建。
  更新 uploader 是手動的:`compose pull uploader && up -d --no-deps uploader`,挑沒有發布
  在跑的時候。(這就是上面那條「動它之前先確認沒有發布在跑」的必然結論。)

## 改完必跑

```bash
python3 uploader/test_server.py     # 5 條
python3 dashboard/test_server.py    # 29 條
```

兩支都只用標準函式庫、離線可跑、無 fixture。**改了任一支 `server.py` 就跑對應那支**,不要只
靠讀。dashboard 另有模組級規則:見 [dashboard/AGENTS.md](dashboard/AGENTS.md)。

## 權限邊界

- **使用者點名任務即授權該任務所需的部署**(例:「加一個 dashboard」含建容器)。但必須:
  ①先 `cp -a` 備份 NAS 的 `compose.yaml`;②`up -d --no-deps <指名的 service>`,
  **不加 `--remove-orphans`**;③事後回報影響半徑與回滾路徑,並貼出既有容器 ID 未變的證據。
- 沒被點名的服務、`git commit`/`push`、改發布身分或 tunnel 設定 —— 一律先問。
- NAS 上的操作分層(看／彈／改／炸)與 sudo 範圍走 `synology-container` skill,本檔不重複。
