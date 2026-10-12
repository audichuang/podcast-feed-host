#!/bin/sh
# End-to-end check of r2-backup.sh against fake_worker.py. Needs python3, curl, flock.
# Run: sh backup/test_r2_backup_e2e.sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
d=$(mktemp -d)
pid=""
trap 'kill $pid 2>/dev/null || :; rm -rf "$d"' EXIT
T=abcdefghijklmnopqrstuvwx
port=$((20000 + $$ % 10000))

serve() { # $1 mode
  kill $pid 2>/dev/null || :
  port=$((port + 1))
  : >"$d/log"
  python3 "$here/fake_worker.py" "$port" "$d/log" "$1" &
  pid=$!
  i=0
  until curl -s -o /dev/null "http://127.0.0.1:$port/_admin/list"; do
    i=$((i + 1))
    [ $i -lt 50 ] || { echo "fake worker did not start"; exit 1; }
    sleep 0.1
  done
  : >"$d/log"
}
run() { # $1 cap
  BASE=http://127.0.0.1:$port DEST=$d/dest UPLOAD_TOKEN=dummy CAP_BYTES=$1 \
    LOCK=$d/lock LAST_OK=$d/ok sh "$here/r2-backup.sh" once >"$d/out" 2>&1
}
fail() { echo "FAIL: $*"; cat "$d/out"; exit 1; }
gets() { grep -c "^GET /_admin/object/" "$d/log" || :; }

mkdir -p "$d/dest/feeds/$T"
head -c 50 /dev/zero >"$d/dest/feeds/$T/EP02-00000002.mp3"
echo stale >"$d/dest/feeds/$T/feed.xml.0123456789abcdef.part"

serve ok
run 99999999 || fail "first run"
[ "$(stat -c %Y "$d/dest/feeds/$T/feed.xml")" = 1767225602 ] || fail "feed.xml mtime is not the R2 upload time"
[ "$(stat -c %Y "$d/dest/feeds/$T/show.json")" = 1767225601 ] || fail "show.json mtime is not the R2 upload time"
[ -z "$(find "$d/dest" -name '*.part')" ] || fail "stale .part not removed"
grep -q "skip unexpected key" "$d/out" || fail "key with '|' was not skipped"
grep -q "^PUT /feeds/$T/EP02-00000002.mp3" "$d/log" || fail "marked episode not promoted"
[ -e "$d/ok" ] || fail "last-ok not touched"

serve ok
run 99999999 || fail "second run"
[ "$(gets)" = 0 ] || fail "second run re-downloaded unchanged objects ($(gets) GETs)"

rm -rf "$d/dest/feeds/$T/EP01-00000001.mp3" "$d/ok"
serve cut_media
if run 0; then fail "cut download did not abort the run"; fi
grep -q "^DELETE /_admin/object/feeds/" "$d/log" && fail "trimmed R2 after a failed backup"
[ -z "$(find "$d/dest" -name '*.part')" ] || fail ".part left after a cut download"
[ -e "$d/ok" ] && fail "last-ok touched by a failed run"

serve drop_put
run 99999999 || fail "dropped promote PUT aborted the run"
grep -q "marker kept" "$d/out" || fail "marker not kept after a dropped PUT"
grep -q "^DELETE /_admin/object/_promote/" "$d/log" && fail "marker deleted after a dropped PUT"

echo "ok: e2e"
