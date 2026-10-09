#!/usr/bin/env bash
# Online, consistent copy of the SQLite database; keep 7 days.
set -euo pipefail
DB=/var/lib/helopath/helopath.sqlite3
OUT=/var/backups/helopath
mkdir -p "$OUT"
sqlite3 "$DB" ".backup '$OUT/helopath-$(date -u +%F).sqlite3'"
find "$OUT" -name 'helopath-*.sqlite3' -mtime +6 -delete
