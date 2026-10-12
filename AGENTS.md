# AGENTS.md — podcast-feed-host

在 NAS 上把 podcast RSS feed 目錄對外服務的四個容器,前面再加一層 Cloudflare Worker + R2(`worker/`)。**架構、安裝、驗收指令、目錄結構、
dashboard 的刪除判準,全部在 [README.md](README.md)** ——本檔只寫 README 與程式碼給不了的
意圖與地雷。工作根 `../AGENTS.md` 的跨專案規則(secrets → Doppler、commit/push 要明講)照舊生效。

## 核心不變式:讀寫分離

四個 service 掛**同一個** feed 目錄,靠掛載模式與綁定介面分權,不靠自律:

| service | 掛載 | 綁定 | 角色 |
|---|---|---|---|
| caddy | `:ro` | host port,經 tunnel(`podcast-nas`)對外 | R2 沒有的檔由 Worker 回源到它 |
| uploader | rw(無後綴=預設) | **LAN IP only**,token 保護 | 舊的發布寫入口(2026-10-12 起發布改走 Worker,保留當回滾) |
| dashboard | `:rw` | **LAN IP only**,無登入 | 管理後端(刪除功能由掛載決定,見下) |
| r2-backup | rw(寫備份) | 無 port | 每日把 R2 備份進來、補回被聽的舊集、修剪 R2(見 `worker/README.md`) |

**2026-10-12 起 `podcast.audichuang.app` 是 Cloudflare Worker**(`worker/`):R2 為主,R2 沒有才經
`podcast-nas.audichuang.app` 回到這台 caddy。發布器的 `PODCAST_UPLOAD_URL` 已改成 Worker,
**uploader 不再收到發布**(保留當回滾路徑)。dashboard 看的是 NAS 備份,新集要等隔天備份才出現;
在 dashboard 刪檔不會從 R2 刪,隔天又會被備份拉回來。

**新增任何 service,先回答兩題:掛 `:ro` 還是 `:rw`、綁 `0.0.0.0` 還是 LAN IP。** 答不出來就別加。

- **8086 絕不加進 Cloudflare Tunnel ingress。** uploader 是寫入端,沒有任何要對外的理由。
- **8087 只准在 Cloudflare Access 後面對外。** dashboard 會把**每一個 feed token 列出來**,
  而 token(`HMAC(salt, show_id)`、不輪替)就是那些未公開列出的 feed 唯一的存取控制 ——
  一次外洩等於永久公開。所以那條 ingress 要成立必須**同時**有這三樣,少一樣就撤掉 ingress:
  ①該 hostname 有 Access application + Allow 政策(email OTP);②dashboard 的
  `ALLOWED_HOSTS` 只列那一個 hostname;③`_host_ok` 對白名單網域強制要 Access 的
  `Cf-Access-Jwt-Assertion` 標頭(Access 被誤刪時原點自己 fail closed)。
  **驗收看的是行為**:未登入打進去要被 Access 轉走(指令見 README),不是看設定畫面對不對。
  要緊急斷掉對外:**清空容器的 `ALLOWED_HOSTS` 再 restart** —— 原點退回只收 IP 字面值,
  Cloudflare 那邊完全不用動(跟下面 `:ro` 關掉刪除同一種槓桿)。
- **dashboard 的刪除能力 = `/srv` 的掛載模式**(`writable()` 就是 `os.access(ROOT, W_OK)`)。
  要緊急關掉全部破壞性動作:compose 改回 `:ro` + restart,不必動程式。

## 哪個服務斷了會怎樣

- **Worker 斷** → 全部訂閱者抓不到、發布也失敗。回滾(拿掉 route)讓流量直接回 tunnel → caddy。
- **caddy 斷** → 只有「R2 裡沒有」的檔抓不到:目前是大部分舊集的 mp3。每個節目的 feed.xml、
  index.html、show.json、封面都已在 R2(2026-10-12 從 NAS 補進去),feed 照常可訂閱。
- **r2-backup 斷** → 新集只剩 R2 一份;healthcheck 36 小時沒有成功就 unhealthy,看門狗會推播。
- **uploader** 已不在發布路徑上(只是回滾用),動它不影響發布。
- 改 Worker 前確認沒有發布在跑:Worker 部署是原子切換,但發布中途換版仍可能讓某個 PUT 失敗;
  發布器的順序是「全部媒體 → show.json → feed.xml」,feed.xml 寫完才算完成。

## NAS 上的部署(2026-10-06 起由 Komodo 管)

- **audinas 上這個 stack 由 Komodo 部署,正本在 ops repo**(`~/Project/nas-ops`,Gitea
  `audichuang/nas-ops`)的 `hosts/audinas/podcast-feed-host/`:`compose.yaml`、
  `Caddyfile`、`manifest.md`。交接紀錄 `operator/komodo/podcast-komodo-2026-10-06.md`。
  **改 repo 這份 `docker-compose.yml` 不會生效**,它是給別人照 `.env` 用的範本。
- **不變式:兩份的 service / 掛載 / port 結構必須一致。** 值本來就不同(範本走 `.env`、ops 那份
  內嵌),但「有哪些 service、各自掛什麼、綁哪個介面」不准分歧。改任一邊都要同步另一邊。
  唯一刻意的差別:ops 那份**沒有 watchtower**,image 一律 `sha-<commit>@sha256:<digest>` 釘住。
- **改設定或換 image = 改 ops repo → commit → 在 Komodo 鎖定該 SHA → DeployStack。**
  不用 SSH `compose up`、不用 Container Manager(CM 專案已刪;`/volume5/docker/podcast` 只剩
  回滾副本與資料,放了 `MANAGED-BY-KOMODO`)。流程細節走 `nas-container` skill 的 Komodo 參考檔。
- **更新 image 沒有自動化。** CI 照樣每次 push 建三個 image 推 GHCR(`latest` + `sha-<short>`),
  但沒有東西會自己拉。要上線:取 CI 推的 `sha-<short>` tag 的 digest,改 ops repo 的 `image:`。
  **uploader 只在沒有發布在跑時更新**,部署時 `services` 只列要換的那個。
- `UPLOAD_TOKEN` 在 audinas Periphery `[secrets]`(`PODCAST_UPLOAD_TOKEN`),不在任何檔案裡。
- **`Caddyfile` 生效的是掛載那份**(ops repo 的 `Caddyfile`,從 Komodo clone 唯讀掛進 caddy);
  image 裡也 `COPY` 了一份當預設,兩份保持一致。單檔 bind 綁的是容器啟動時的 inode,只改
  Caddyfile 的部署之後要 restart caddy。
- **三個 image 都由 CI 建、NAS 只 pull。** 沒有本機 build、沒有 bind-mount 程式碼。
  `dashboard/server.py` 是烤進 image 的 —— **改了程式要 push 讓 CI 建**,再照上面換 digest。
- **對外那條 tunnel 是 remote-managed(`tunnel run --token`):ingress 在 Cloudflare 端,NAS 上
  沒有 `config.yml` 可改,加 hostname 也不用重啟 cloudflared。** 帳號上四顆 tunnel 只有
  `audinas` 連得到這台(其餘的 origin 是別台機器);而
  `PUT …/cfd_tunnel/<id>/configurations` **是整份覆蓋** —— 先 GET 存一份再改,少貼一條就把
  `podcast.audichuang.app` 那條跟其他七條一起抹掉。cloudflared 本身也是 Komodo stack `cloudflare`。

## 改完必跑

```bash
python3 uploader/test_server.py     # 5 條
python3 dashboard/test_server.py    # 31 條
```

兩支都只用標準函式庫、離線可跑、無 fixture。**改了任一支 `server.py` 就跑對應那支**,不要只
靠讀。dashboard 另有模組級規則:見 [dashboard/AGENTS.md](dashboard/AGENTS.md)。

## 權限邊界

- **使用者點名任務即授權該任務所需的部署**(例:「加一個 dashboard」含建容器),照上面的
  Komodo 流程做;事後回報影響半徑與回滾路徑,並貼出非目標容器 ID 未變的證據。
- 沒被點名的服務、`git commit`/`push`、改發布身分或 tunnel 設定 —— 一律先問。
- NAS 上的操作分層(看／彈／改／炸)走 `nas-container` skill,本檔不重複。
