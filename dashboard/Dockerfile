# 管理後端。服務本身是**一個只用標準函式庫的檔案**;這個 image 多裝的只有 Pillow,
# 唯一用途是把 3000×3000 的節目封面縮成 320px —— 首頁 16 張原圖要傳 24 MB、
# 解碼 529 MB,瀏覽器會直接放棄畫後面幾張,而標準函式庫沒有 JPEG decoder。
# musllinux wheel(amd64 與 arm64 都有)現成,不編譯。
# Pillow 若不在,server.py 會自動退回原圖:頁面仍然正確,只是很重。
FROM python:3-alpine
RUN pip install --no-cache-dir --only-binary=:all: pillow
WORKDIR /app
# 程式烤進 image,不 bind-mount:更新走 CI → GHCR → watchtower,與 caddy/uploader
# 同一條路。掛載進來的話,NAS 上一份過期的檔案會靜默蓋掉 CI 建出來的版本。
COPY server.py .
CMD ["python3", "/app/server.py"]
