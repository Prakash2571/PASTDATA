#!/usr/bin/env bash
#
# Prints a snapshot of ingest progress straight from MongoDB.
# Used by `make monitor` and `make watch`.
#
set -euo pipefail

DB="${MONGO_DB:-nse_fno}"

docker compose exec -T mongo mongosh --quiet "$DB" --eval '
  var c = db.stock_futures.countDocuments({});
  var d = db.ingest_progress.countDocuments({});
  if (c > 0) {
    var a = db.stock_futures.aggregate([
      {$group: {_id: null, min: {$min: "$trading_date"}, max: {$max: "$trading_date"}}}
    ]).toArray()[0];
    print("futures docs  : " + c);
    print("days ingested : " + d);
    print("date range    : " + a.min.toISOString().slice(0,10) + "  ->  " + a.max.toISOString().slice(0,10));
    print("distinct syms : " + db.stock_futures.distinct("symbol").length);
  } else {
    print("No documents yet. Is the backfill running?  (make logs)");
  }
'
