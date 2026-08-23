# AGENTS.md — dashboard

唯讀管理後端(現在也能刪),單一 stdlib-only 檔 + Pillow 只用於縮圖。**它是什麼、路由、
刪除的三層可逆性分級,在 [../README.md](../README.md) 的「管理後端」章節。** repo 層規則見
[../AGENTS.md](../AGENTS.md);本檔只寫「改 `server.py` 時不知道就會出事」的那幾條。

## 孤兒推導:方向性是刻意的

刪除目標來自 `safe_orphans()`,而它建立在 `scan()` 的 `referenced` 上。

- **`referenced` 刻意寬鬆** —— 用 `_MEDIA_RE` 掃 `show.json` **全文**,不比對欄位。
  寧可漏報孤兒,**絕不可把還活著的檔案報成垃圾**。看起來像「可以收窄成只看欄位」的最佳化,
  實際會把只出現在 description URL 裡的簡報／講義判成垃圾 —— **不准收窄它**。
- **`referenced` 必須同時含 `feed.xml` 與 `index.html` 的全文。** 訂閱者抓的是 `feed.xml`,
  不是 `show.json`;發布中斷在 `show.json` 之後會留下持久 desync(`show.json` 已新版、
  上線的 `feed.xml` 還指舊檔名),只看 `show.json` 就會刪掉上線中的 enclosure。
- **mtime 比 `show.json` 新 = 這個檔還在飛。** 發布器的順序是「全部媒體 → `show.json`
  → `feed.xml`」,所以這是精確判準,不是啟發法。30 分鐘緩衝只是第二道 —— 一季 50 集的
  上傳遠超 30 分鐘,實測有檔案落地 31 分鐘後才被 `show.json` 引用。
- **`stale`(H5:`feed.xml` 落後 `show.json`)的節目不開放清理**,批次清理要跳過它而不是中止。
- 「無法歸屬」與「缺號」兩類**永遠不進刪除集合** —— 缺號那類很可能是被扣下那一集的正本。

## 破壞性端點

- **前端指定不了檔名。** 只送 `token`(+集號/確認字串),要刪哪些一律在伺服器端用
  `safe_orphans()` 重新推導。新增任何破壞性端點都要沿用這個形狀。
- POST-only + `X-Confirm` 自訂標頭 + **Host 必須是 IP**(擋 DNS rebinding —— 只比對
  `Origin == http://{Host}` 等於自己跟自己比)。這一關對 GET 也生效。
- **每條路徑都要呼叫 `_audit()`** —— 成功與每一道 guard 拒絕都要。刪除是硬刪、沒有備份,
  而 docker 的事件緩衝只留幾分鐘,這是事後唯一能回答「誰在什麼時候刪了什麼」的地方。
  `log_message` 把 GET 全靜音是刻意的(一次載入十幾個請求),但**破壞性動作不准跟著靜音**。
  IP 欄位是 docker bridge gateway 不是 LAN 來源機器(port publishing 會 SNAT)。

## `_JS` / `_CSS` 必須是 raw string

裡面的 `\n` 是要給 **JavaScript** 的逸出序列。少了 `r`,Python 會先展開成真換行,而 JS 的
字串不能跨行 → 整支 `<script>` SyntaxError → **整頁 JS 不執行,但頁面照樣渲染得出來**
(按鈕、播放器、搜尋全是死的,肉眼看不出)。`test_inline_js_has_no_unterminated_string_literal`
就是為此而留 —— Chrome 不在 CI 裡,只有它守得住。

## 破壞性路徑的瀏覽器測試只准打沙箱

2026-08-23 用 Playwright 驗對話框時,`button[data-typed]` 的 `.first()` 選到了全站
「一次刪除全部可回收」,腳本自動填入確認字串 + Enter → **在正式資料上硬刪 1159 檔 / 28.3 GB**。
防護沒失效,是自動化腳本會「正確地」通過每一道為手誤設計的閘。

- 任何會 `unlink` / `rename` / `rmtree` 的路徑,瀏覽器測試**只指向沙箱**。
- 沙箱做法:真實 `show.json` 逐位元組複製 + 同名同 mtime 的 1-byte 假媒體檔
  (16 個 feed 只要 9 MB,推導只看檔名與 `show.json`,所以保真)。
- 腳本裡**不要用 `.first()` 挑破壞性按鈕** —— 用明確選擇器,否則「第一顆」會隨版面改變
  而指到殺傷力最大的那顆。
- 驗收用同一條不變量:**每個 feed 的 `show.json` + `feed.xml` 全文提到的檔名都必須還在磁碟上。**
