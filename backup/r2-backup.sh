#!/bin/sh
# Daily on the NAS, R2 as a cache in front of the NAS backup:
#   1. back up R2 -> NAS
#   2. promote: episodes someone played from the NAS (Worker left _promote/<path>
#      markers) are copied back into R2, which makes them the newest objects
#   3. evict: trim R2 under CAP_BYTES, oldest upload first, only what the NAS holds
set -eu

REMOTE=${REMOTE:-r2:podcast-feeds}
DEST=${DEST:-/data}
CAP_BYTES=${CAP_BYTES:-7000000000} # 70% of the 10 GB R2 free tier

# Only episode media is evictable; feed.xml / index.html / artwork always stay in R2.
MEDIA_RE='\|feeds/[a-z2-7]{24}/EP[0-9]{2}-[0-9a-f]{8}\.(mp3|pdf|html)$'

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

main() {
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  # copy, never sync: deletions in R2 must not propagate to the backup.
  rclone copy "$REMOTE" "$DEST" --exclude "_promote/**"

  rclone lsf -R --files-only "$REMOTE/_promote" >"$work/promote" 2>/dev/null || :
  while read -r path; do
    # rcat (stream), not copyto: copyto carries the file's old mtime along, so the
    # promoted episode would sort as oldest and be evicted in step 3. rcat stamps "now".
    [ -f "$DEST/$path" ] && rclone rcat "$REMOTE/$path" <"$DEST/$path"
    rclone deletefile "$REMOTE/_promote/$path"
  done <"$work/promote"

  # --use-server-modtime = upload time. Without it rclone reports the file's original
  # mtime, so a just-promoted old episode would look oldest and be evicted at once.
  rclone lsf -R --files-only --use-server-modtime --format tsp --separator '|' \
    --exclude "_promote/**" "$REMOTE" >"$work/listing"
  pick_evictions "$work/listing" "$DEST" "$CAP_BYTES" >"$work/evict"
  if [ -s "$work/evict" ]; then
    echo "evicting $(wc -l <"$work/evict") objects from $REMOTE"
    rclone delete "$REMOTE" --files-from "$work/evict"
  fi
}

[ "${R2_BACKUP_LIB:-}" = 1 ] || main
