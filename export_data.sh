#!/usr/bin/env bash
#
# Dump the whole nse_fno database to a single portable, compressed archive
# that you can copy off the instance and restore anywhere.
#
# Usage:
#   ./export_data.sh                       # -> fno_dump_YYYYMMDD.archive.gz
#   ./export_data.sh my_backup.archive.gz  # custom output name
#
# Restore on your local machine (needs mongodb-database-tools installed):
#   mongorestore --archive=fno_dump_YYYYMMDD.archive.gz --gzip
#   # data lands in the "nse_fno" database on your local mongodb://localhost:27017
#
set -euo pipefail

DB="${MONGO_DB:-nse_fno}"
OUT="${1:-fno_dump_$(date +%Y%m%d).archive.gz}"

echo "Dumping database '$DB' -> $OUT ..."
# -T keeps the binary stream clean (no TTY); write archive to stdout, redirect to host file.
docker compose exec -T mongo mongodump --db="$DB" --archive --gzip > "$OUT"

SIZE=$(du -h "$OUT" | cut -f1)
echo "Done. Wrote $OUT ($SIZE)."
echo
echo "Copy it to your local machine, e.g.:"
echo "  scp <user>@<instance-ip>:$(pwd)/$OUT ."
echo "Then restore locally with:"
echo "  mongorestore --archive=$OUT --gzip"
