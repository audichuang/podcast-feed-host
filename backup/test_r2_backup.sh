#!/bin/sh
# Offline check of the eviction picker. Run: sh backup/test_r2_backup.sh
set -eu
R2_BACKUP_LIB=1 . "$(dirname "$0")/r2-backup.sh"
d=$(mktemp -d); trap 'rm -rf "$d"' EXIT
T=abcdefghijklmnopqrstuvwx
mkdir -p "$d/nas/feeds/$T"
mk() { head -c "$2" /dev/zero >"$d/nas/feeds/$T/$1"; }
mk EP01-00000001.mp3 400; mk EP02-00000002.mp3 300; mk EP04-00000004.mp3 200
mk EP03-00000003.mp3 999 # backup has a different size -> must be skipped
cat >"$d/listing" <<L
2026-01-05 00:00:00|50|1767571200|feeds/$T/feed.xml
2026-01-01 00:00:00|400|1767225600|feeds/$T/EP01-00000001.mp3
2026-01-02 00:00:00|300|1767312000|feeds/$T/EP02-00000002.mp3
2026-01-03 00:00:00|300|1767398400|feeds/$T/EP03-00000003.mp3
2026-01-04 00:00:00|200|1767484800|feeds/$T/EP04-00000004.mp3
2026-01-06 00:00:00|100|1767657600|feeds/$T/EP05-00000005.mp3
L
# total 1350, cap 700: drop EP01 (950), then EP02 (650 <= 700), stop.
got=$(pick_evictions "$d/listing" "$d/nas" 700 | tr '\n' ' ')
[ "$got" = "feeds/$T/EP01-00000001.mp3 feeds/$T/EP02-00000002.mp3 " ] || { echo "FAIL oldest-first: $got"; exit 1; }
# cap 400: EP03 is skipped (size mismatch), EP05 not in backup; feed.xml never listed.
got=$(pick_evictions "$d/listing" "$d/nas" 400 | tr '\n' ' ')
[ "$got" = "feeds/$T/EP01-00000001.mp3 feeds/$T/EP02-00000002.mp3 feeds/$T/EP04-00000004.mp3 " ] || { echo "FAIL skip-unbacked: $got"; exit 1; }
# under cap: nothing.
[ -z "$(pick_evictions "$d/listing" "$d/nas" 5000)" ] || { echo "FAIL under-cap"; exit 1; }
echo "ok: 3 checks"
