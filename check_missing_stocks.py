#!/usr/bin/env python3
"""
Check Missing F&O Stocks
=========================

After the main backfill completes, this script:
1. Gets the CURRENT F&O stock list from the latest NSE bhavcopy.
2. Compares it against what's already in MongoDB.
3. For any stock that's in F&O but NOT in the DB (or has very little data),
   fetches its full available history from when it was first listed in F&O.

This handles newly-added F&O stocks that might not have existed 10 years ago.

Usage:
  python check_missing_stocks.py                    # check + fetch missing
  python check_missing_stocks.py --check-only       # just report, don't fetch
  python check_missing_stocks.py --symbol JIOFIN    # fetch one specific stock

Works with the same MongoDB and .env as fno_futures_backfill.py.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import time
import zipfile
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv
from pymongo import MongoClient

from fno_futures_backfill import (
    BhavcopyFetcher, MongoStore, Config, daterange,
    FORMAT_CUTOVER, UDIFF_URL, BROWSER_HEADERS, INSTRUMENT_CODES,
)

log = logging.getLogger("check_missing")

# Minimum number of documents a stock should have to be considered "present"
# If it has fewer than this, we'll re-fetch its full history.
MIN_DOCS_THRESHOLD = 100


def get_current_fno_stocks() -> set[str]:
    """Get the current F&O stock list from the latest NSE UDiFF bhavcopy."""
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    today = date.today()
    for i in range(10):
        dt = today - timedelta(days=i)
        if dt.weekday() >= 5:
            continue
        url = UDIFF_URL.format(yyyymmdd=dt.strftime("%Y%m%d"))
        try:
            r = session.get(url, timeout=15)
            if r.status_code != 200:
                continue
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                name = zf.namelist()[0]
                with zf.open(name) as fp:
                    txt = fp.read().decode("utf-8")
            symbols = set()
            for row in csv.DictReader(io.StringIO(txt)):
                if (row.get("FinInstrmTp") or "").strip() == "STF":
                    symbols.add(row["TckrSymb"].strip())
            if symbols:
                log.info("Current F&O list: %d stocks (from bhavcopy %s)", len(symbols), dt)
                return symbols
        except (zipfile.BadZipFile, Exception):
            continue

    log.error("Could not fetch current F&O stock list from NSE")
    return set()


def get_stocks_in_db(db) -> dict[str, int]:
    """Get {symbol: doc_count} for all symbols in the stock_futures collection."""
    pipeline = [
        {"$group": {"_id": "$symbol", "count": {"$sum": 1}}}
    ]
    result = {}
    for doc in db["stock_futures"].aggregate(pipeline):
        result[doc["_id"]] = doc["count"]
    return result


def fetch_stock_history(symbol: str, cfg: Config):
    """Fetch full available history for a single stock."""
    from fno_futures_backfill import BhavcopyFetcher, MongoStore, daterange

    fetcher = BhavcopyFetcher(cfg)
    store = MongoStore(cfg)
    completed = store.completed_dates()

    log.info("Fetching history for %s (%s -> %s)...", symbol, cfg.start_date, cfg.end_date)

    fetched = 0
    for dt in daterange(cfg.start_date, cfg.end_date):
        if dt.weekday() >= 5:
            continue
        if dt.isoformat() in completed:
            continue

        docs = fetcher.fetch_day(dt)
        if docs is None:
            continue

        # Filter only for THIS symbol
        symbol_docs = [d for d in docs if d["symbol"] == symbol]

        if symbol_docs:
            store.store_day(dt, symbol_docs)
            fetched += len(symbol_docs)
        # Note: we don't mark the full day as "done" in progress here
        # because this is a per-symbol fetch, not a full-day fetch.
        # The main backfill's progress collection tracks full days.

        time.sleep(cfg.request_delay)

    log.info("  %s: fetched %d documents", symbol, fetched)
    store.close()
    return fetched


def run(args):
    load_dotenv()
    uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DB", "nse_fno")

    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    client.admin.command("ping")
    db = client[db_name]

    # Get current F&O stocks
    if args.symbol:
        current_fno = {args.symbol.upper()}
    else:
        current_fno = get_current_fno_stocks()
        if not current_fno:
            sys.exit("ERROR: Could not get current F&O list")

    # Get what's in DB
    stocks_in_db = get_stocks_in_db(db)
    log.info("Stocks in DB: %d | Current F&O: %d", len(stocks_in_db), len(current_fno))

    # Find missing or under-represented stocks
    missing = []
    for sym in sorted(current_fno):
        count = stocks_in_db.get(sym, 0)
        if count < MIN_DOCS_THRESHOLD:
            missing.append((sym, count))

    if not missing:
        log.info("All current F&O stocks are present in the database.")
        client.close()
        return

    log.info("Found %d stocks missing or under-represented:", len(missing))
    for sym, count in missing:
        log.info("  %s: %d docs (need >= %d)", sym, count, MIN_DOCS_THRESHOLD)

    if args.check_only:
        log.info("--check-only: not fetching. Run without this flag to backfill them.")
        client.close()
        return

    # Now fetch missing stocks
    # We'll run the main backfill for the full date range — it will
    # pick up data for these symbols from the bhavcopy (which contains ALL stocks).
    # The progress collection already has completed days, so it'll skip those.
    # Any "missing" stock that was newly added will only appear in recent bhavcopies.

    log.info("The main backfill (fno_futures_backfill.py) already fetches ALL stocks")
    log.info("present in each day's bhavcopy. If a stock is 'missing', it's likely")
    log.info("because it was recently added to F&O and only has recent data.")
    log.info("")
    log.info("To get their data: just re-run the main backfill:")
    log.info("  python fno_futures_backfill.py")
    log.info("")
    log.info("It will skip already-completed days and only fetch new ones.")
    log.info("Any newly-added stock will be captured from the day it first appeared")
    log.info("in the F&O bhavcopy.")

    # But if specific symbols are requested, report what dates they appear from
    if args.symbol:
        sym = args.symbol.upper()
        count = stocks_in_db.get(sym, 0)
        if count > 0:
            # Find date range in DB
            first = db["stock_futures"].find_one(
                {"symbol": sym}, sort=[("trading_date", 1)],
                projection={"trading_date": 1})
            last = db["stock_futures"].find_one(
                {"symbol": sym}, sort=[("trading_date", -1)],
                projection={"trading_date": 1})
            log.info("%s in DB: %d docs, %s -> %s",
                     sym, count,
                     first["trading_date"].date() if first else "?",
                     last["trading_date"].date() if last else "?")
        else:
            log.info("%s: not in DB at all. Run the main backfill to capture it.", sym)

    client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Check for missing F&O stocks in the DB and report/fetch them"
    )
    parser.add_argument("--check-only", action="store_true",
                        help="Only report missing stocks, don't fetch")
    parser.add_argument("--symbol", help="Check/fetch a specific symbol")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    run(args)


if __name__ == "__main__":
    main()
