#!/bin/sh
# Daily on the NAS, R2 as a cache in front of the NAS backup. Everything goes through the
# Worker's token-gated admin API, so the only credential is the existing upload token.
#   1. back up: fetch every R2 object the NAS lacks (episode media by size; small files
#      such as feed.xml / index.html / show.json are re-fetched every run, they change)
#   2. promote: episodes someone played from the NAS (_promote/<path> markers left by the
#      Worker) are re-uploaded, which makes them the newest objects
#   3. evict: trim R2 under CAP_BYTES, oldest upload first, only what the NAS holds
#
# Usage: r2-backup.sh [once|loop]   (loop = run daily at BACKUP_AT, container entrypoint)
set -eu

BASE=${BASE:-https://podcast.audichuang.app}
DEST=${DEST:-/data}
CAP_BYTES=${CAP_BYTES:-7000000000} # 70% of the 10 GB R2 free tier
LOCK=${LOCK:-/tmp/r2-backup.lock}       # one run at a time (manual `once` vs the loop)
LAST_OK=${LAST_OK:-/tmp/r2-backup.last-ok} # touched on success; the compose healthcheck reads it

# Only episode media is evictable; feed.xml / index.html / artwork always stay in R2.
MEDIA_RE='\|feeds/[a-z2-7]{24}/EP[0-9]{2}-[0-9a-f]{8}\.(mp3|pdf|html)$'
# Keys come off the network: only ever write inside DEST/feeds/<token>/<plain name>.
KEY_RE='^feeds/[a-z2-7]{24}/[A-Za-z0-9][A-Za-z0-9._-]*$'

# pick_evictions <listing "time|size|path"> <backup dir> <cap bytes>
# Prints paths to delete, oldest first, until the total fits under the cap.
pick_evictions() {
  total=$(awk -F'|' '{ s += $2 } END { printf "%.0f", s }' "$1")
  sort "$1" | grep -E "$MEDIA_RE" | while IFS='|' read -r _time size path; do
    [ "$total" -le "$3" ] && break
    # Never evict what the backup doesn't hold byte-for-byte (size check).
    [ "$(stat -c%s "$2/$path" 2>/dev/null)" = "$size" ] || continue
    echo "$path"
    total=$((total - size))
  done
}

# The token goes in through a 0600 header file, never argv (argv is visible in ps).
api() {
  curl -fsS --retry 3 --retry-all-errors --max-time 900 -H "@$work/auth" "$@"
}

backup() { # $1 listing
  grep -v '|_promote/' "$1" | while IFS='|' read -r _time size key; do
    if ! echo "$key" | grep -qE "$KEY_RE"; then
      echo "skip unexpected key: $key" >&2
      continue
    fi
    dst=$DEST/$key
    if echo "|$key" | grep -qE "$MEDIA_RE" && [ "$(stat -c%s "$dst" 2>/dev/null)" = "$size" ]; then
      continue # content-addressed: same name + size = same bytes
    fi
    mkdir -p "${dst%/*}"
    if ! api -o "$dst.part" "$BASE/_admin/object/$key"; then
      rm -f "$dst.part" # never leave a partial file in the directory caddy serves
      return 1
    fi
    if [ "$(stat -c%s "$dst.part")" != "$size" ]; then
      rm -f "$dst.part"
      echo "size mismatch: $key" >&2
      return 1
    fi
    mv -f "$dst.part" "$dst"
  done
}

promote() { # $1 listing
  grep '|_promote/' "$1" | while IFS='|' read -r _time _size key; do
    path=${key#_promote/}
    # Only episode media: an old feed.xml on the NAS must never overwrite R2's newer one.
    if echo "|$path" | grep -qE "$MEDIA_RE" && [ -f "$DEST/$path" ]; then
      # A network-level failure is "try again tomorrow", not "abort the run".
      code=$(curl -sS -o /dev/null -w '%{http_code}' --retry 3 --max-time 900 -X PUT \
        -H "@$work/auth" -T "$DEST/$path" "$BASE/$path") || code=000
      # 413 = over the Worker's 100 MB body limit (old 256k uploads): stays NAS-only.
      case $code in
        201 | 413) ;;
        *)
          echo "promote $path -> HTTP $code, marker kept for next run" >&2
          continue
          ;;
      esac
    fi
    api -X DELETE "$BASE/_admin/object/$key" >/dev/null
  done
}

main() {
  : "${UPLOAD_TOKEN:?}"
  exec 9>"$LOCK"
  flock -n 9 || {
    echo "another run holds $LOCK" >&2
    return 1
  }
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  (umask 077 && printf 'Authorization: Bearer %s\n' "$UPLOAD_TOKEN" >"$work/auth")
  api "$BASE/_admin/list" >"$work/listing"
  backup "$work/listing"
  promote "$work/listing"
  api "$BASE/_admin/list" >"$work/listing" # promotion changed upload times
  pick_evictions "$work/listing" "$DEST" "$CAP_BYTES" >"$work/evict"
  while read -r key; do
    api -X DELETE "$BASE/_admin/object/$key" >/dev/null
  done <"$work/evict"
  touch "$LAST_OK"
  echo "$(date -u +%FT%TZ) ok: $(wc -l <"$work/listing") objects in R2, evicted $(wc -l <"$work/evict")"
}

if [ "${R2_BACKUP_LIB:-}" != 1 ]; then
  case "${1:-once}" in
    once) main ;;
    loop)
      touch "$LAST_OK" # container start counts as healthy until the first run is due
      # Each run is its own process: `main || ...` would switch off set -e inside main,
      # and a failed backup must never fall through to eviction.
      while :; do
        if [ "$(date -u +%H:%M)" = "${BACKUP_AT_UTC:-20:30}" ]; then
          sh "$0" once || echo "$(date -u +%FT%TZ) FAILED" >&2
        fi
        sleep 60
      done
      ;;
    *)
      echo "usage: $0 [once|loop]" >&2
      exit 2
      ;;
  esac
fi
