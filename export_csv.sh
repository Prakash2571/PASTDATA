#!/usr/bin/env bash
#
# Export the stock-futures collection to a CSV in the same format as the
# NSE bhavcopy settlement files:
#   DATE, INSTRUMENT, UNDERLYING, EXPIRY DATE, MTM SETTLEMENT PRICE
#   02-JUL-2025,FUTSTK,RELIANCE,31-JUL-2025,1320.50
#
# Also exports a "full" version with all OHLC/OI fields if you want more detail.
#
# Usage:
#   ./export_csv.sh                          # -> stock_futures_YYYYMMDD.csv (bhavcopy style)
#   ./export_csv.sh my_futures.csv           # custom output name
#
set -euo pipefail

DB="${MONGO_DB:-nse_fno}"
COL="${MONGO_COLLECTION:-stock_futures}"
OUT="${1:-stock_futures_$(date +%Y%m%d).csv}"
OUT_FULL="${OUT%.csv}_full.csv"

echo "Exporting $DB.$COL -> $OUT (NSE bhavcopy style) ..."

# Use mongosh to format dates as DD-MMM-YYYY (like NSE does)
docker compose exec -T mongo mongosh --quiet "$DB" --eval '
const months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
function fmtDate(d) {
  if (!d) return "";
  let dt = new Date(d);
  let dd = String(dt.getUTCDate()).padStart(2,"0");
  let mmm = months[dt.getUTCMonth()];
  let yyyy = dt.getUTCFullYear();
  return dd + "-" + mmm + "-" + yyyy;
}
print("DATE,INSTRUMENT,UNDERLYING,EXPIRY DATE,MTM SETTLEMENT PRICE");
db.stock_futures.find({}).sort({trading_date:1, symbol:1, expiry:1}).forEach(doc => {
  print([
    fmtDate(doc.trading_date),
    doc.instrument || "FUTSTK",
    doc.symbol,
    fmtDate(doc.expiry),
    (doc.settle_price !== null && doc.settle_price !== undefined) ? doc.settle_price.toFixed(2) : "0.00"
  ].join(","));
});
' > "$OUT"

ROWS=$(($(wc -l < "$OUT") - 1))
SIZE=$(du -h "$OUT" | cut -f1)
echo "Done. Wrote $OUT ($SIZE, $ROWS data rows)."

# Also export a full version with all fields
echo ""
echo "Exporting full version -> $OUT_FULL (all OHLC/OI fields) ..."

docker compose exec -T mongo mongosh --quiet "$DB" --eval '
const months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
function fmtDate(d) {
  if (!d) return "";
  let dt = new Date(d);
  let dd = String(dt.getUTCDate()).padStart(2,"0");
  let mmm = months[dt.getUTCMonth()];
  let yyyy = dt.getUTCFullYear();
  return dd + "-" + mmm + "-" + yyyy;
}
function n(v) { return (v !== null && v !== undefined) ? v : 0; }
print("DATE,INSTRUMENT,UNDERLYING,EXPIRY DATE,OPEN,HIGH,LOW,CLOSE,SETTLE PRICE,CONTRACTS,VALUE LAKH,OPEN INTEREST,CHANGE IN OI");
db.stock_futures.find({}).sort({trading_date:1, symbol:1, expiry:1}).forEach(doc => {
  print([
    fmtDate(doc.trading_date),
    doc.instrument || "FUTSTK",
    doc.symbol,
    fmtDate(doc.expiry),
    n(doc.open).toFixed(2),
    n(doc.high).toFixed(2),
    n(doc.low).toFixed(2),
    n(doc.close).toFixed(2),
    n(doc.settle_price).toFixed(2),
    n(doc.contracts),
    n(doc.value_lakh).toFixed(2),
    n(doc.open_interest),
    n(doc.change_in_oi)
  ].join(","));
});
' > "$OUT_FULL"

ROWS2=$(($(wc -l < "$OUT_FULL") - 1))
SIZE2=$(du -h "$OUT_FULL" | cut -f1)
echo "Done. Wrote $OUT_FULL ($SIZE2, $ROWS2 data rows)."
echo ""
echo "Copy to your machine:"
echo "  scp -i vivek.pem ubuntu@<ip>:~/PASTDATA/$OUT ."
echo "  scp -i vivek.pem ubuntu@<ip>:~/PASTDATA/$OUT_FULL ."
