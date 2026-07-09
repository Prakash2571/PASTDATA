#!/usr/bin/env bash
#
# Export the stock-futures collection to a flat CSV you can open in Excel.
# (932k rows fits in a single Excel sheet; limit is ~1,048,576 rows.)
#
# Usage:
#   ./export_csv.sh                          # -> stock_futures_YYYYMMDD.csv
#   ./export_csv.sh my_futures.csv           # custom output name
#
set -euo pipefail

DB="${MONGO_DB:-nse_fno}"
COL="${MONGO_COLLECTION:-stock_futures}"
OUT="${1:-stock_futures_$(date +%Y%m%d).csv}"

# Columns, in a sensible order for a spreadsheet.
FIELDS="trading_date,symbol,instrument,expiry,open,high,low,close,settle_price,contracts,value_lakh,open_interest,change_in_oi,num_trades,source_format"

echo "Exporting $DB.$COL -> $OUT (CSV, sorted by symbol then date) ..."
docker compose exec -T mongo mongoexport \
  --db="$DB" --collection="$COL" \
  --type=csv --fields="$FIELDS" \
  --sort='{"symbol":1,"trading_date":1}' \
  > "$OUT"

ROWS=$(($(wc -l < "$OUT") - 1))
SIZE=$(du -h "$OUT" | cut -f1)
echo "Done. Wrote $OUT ($SIZE, $ROWS data rows)."
echo
echo "Copy it to your machine and open in Excel:"
echo "  scp <user>@<instance-ip>:$(pwd)/$OUT ."
