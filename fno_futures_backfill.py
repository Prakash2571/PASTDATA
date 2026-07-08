#!/usr/bin/env python3
"""
Backfill NSE F&O *stock futures* bhavcopy data into MongoDB.

Fetches the NSE Futures & Options bhavcopy for every trading day over the last
N years (default 7) and stores the stock-futures rows in MongoDB.

Design goals
------------
* SERIAL, one trading day at a time, with a polite delay between requests so we
  never hammer NSE (avoids getting rate-limited / blocked) and never dump
  everything into MongoDB at once.
* Resumable / idempotent: a `progress` collection records completed dates, and a
  unique index on (trading_date, symbol, expiry) means re-running never creates
  duplicates.
* Handles BOTH bhavcopy formats:
    - Old format  (< 2024-07-08): INSTRUMENT == 'FUTSTK', fetched via jugaad-data.
    - New UDiFF   (>= 2024-07-08): FinInstrmTp == 'STF', downloaded directly.
  If the expected format 404s near the boundary, the other format is tried.

Usage
-----
    cp .env.example .env      # then edit .env and paste your MONGODB_URI
    pip install -r requirements.txt
    python3 fno_futures_backfill.py                 # full 7-year backfill
    python3 fno_futures_backfill.py --start 2024-01-01 --end 2024-01-31
    python3 fno_futures_backfill.py --dry-run       # fetch+parse, no DB writes
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv
from pymongo import MongoClient, ReplaceOne
from pymongo.errors import BulkWriteError

# jugaad-data is used for the OLD bhavcopy format (it manages NSE session/headers)
from jugaad_data.nse import NSEArchives

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# NSE switched the F&O bhavcopy to the UDiFF format in July 2024.
FORMAT_CUTOVER = date(2024, 7, 8)

# New UDiFF F&O bhavcopy URL template.
UDIFF_URL = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)

# Logical instrument -> (old code, new UDiFF code)
INSTRUMENT_CODES = {
    "STOCK_FUT": {"old": "FUTSTK", "new": "STF"},
    "INDEX_FUT": {"old": "FUTIDX", "new": "IDF"},
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/zip,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

log = logging.getLogger("fno_backfill")


@dataclass
class Config:
    mongodb_uri: str
    db_name: str
    collection: str
    progress_collection: str
    start_date: date
    end_date: date
    request_delay: float
    max_retries: int
    instruments: list[str]      # logical names, e.g. ["STOCK_FUT"]
    dry_run: bool


# --------------------------------------------------------------------------- #
# Helpers: numeric / date parsing
# --------------------------------------------------------------------------- #

def _to_float(value) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if s == "" or s.lower() in {"na", "nan", "-"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(value) -> int | None:
    f = _to_float(value)
    return int(f) if f is not None else None


def _parse_expiry(value: str) -> datetime | None:
    s = (value or "").strip()
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #

class BhavcopyFetcher:
    """Fetches and normalizes F&O bhavcopy for a single trading date."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self.archives = NSEArchives()  # old-format fetcher (jugaad-data)
        # Which raw codes to keep, per format.
        self.old_codes = {INSTRUMENT_CODES[i]["old"] for i in cfg.instruments}
        self.new_codes = {INSTRUMENT_CODES[i]["new"] for i in cfg.instruments}

    # -- raw fetchers ------------------------------------------------------- #

    def _fetch_old_raw(self, dt: date) -> str | None:
        """Old FUTSTK-style CSV text via jugaad-data. Returns None if not found."""
        try:
            return self.archives.bhavcopy_fo_raw(dt)
        except Exception as exc:  # jugaad raises on 404/other
            msg = str(exc).lower()
            if "404" in msg or "not found" in msg:
                return None
            raise

    def _fetch_new_raw(self, dt: date) -> str | None:
        """New UDiFF CSV text via direct download. Returns None if not found."""
        url = UDIFF_URL.format(yyyymmdd=dt.strftime("%Y%m%d"))
        resp = self.session.get(url, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as fp:
                return fp.read().decode("utf-8")

    # -- normalizers -------------------------------------------------------- #

    def _parse_old(self, text: str, dt: date) -> list[dict]:
        import csv

        docs: list[dict] = []
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            instr = (row.get("INSTRUMENT") or "").strip()
            if instr not in self.old_codes:
                continue
            symbol = (row.get("SYMBOL") or "").strip()
            expiry = _parse_expiry(row.get("EXPIRY_DT"))
            docs.append({
                "trading_date": datetime(dt.year, dt.month, dt.day),
                "symbol": symbol,
                "instrument": instr,
                "expiry": expiry,
                "open": _to_float(row.get("OPEN")),
                "high": _to_float(row.get("HIGH")),
                "low": _to_float(row.get("LOW")),
                "close": _to_float(row.get("CLOSE")),
                "settle_price": _to_float(row.get("SETTLE_PR")),
                "contracts": _to_int(row.get("CONTRACTS")),
                "value_lakh": _to_float(row.get("VAL_INLAKH")),
                "open_interest": _to_int(row.get("OPEN_INT")),
                "change_in_oi": _to_int(row.get("CHG_IN_OI")),
                "source_format": "old",
            })
        return docs

    def _parse_new(self, text: str, dt: date) -> list[dict]:
        import csv

        docs: list[dict] = []
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            instr = (row.get("FinInstrmTp") or "").strip()
            if instr not in self.new_codes:
                continue
            symbol = (row.get("TckrSymb") or "").strip()
            expiry = _parse_expiry(row.get("XpryDt"))
            # value is turnover in currency; keep as value_lakh for consistency
            turnover = _to_float(row.get("TtlTrfVal"))
            docs.append({
                "trading_date": datetime(dt.year, dt.month, dt.day),
                "symbol": symbol,
                "instrument": instr,
                "expiry": expiry,
                "open": _to_float(row.get("OpnPric")),
                "high": _to_float(row.get("HghPric")),
                "low": _to_float(row.get("LwPric")),
                "close": _to_float(row.get("ClsPric")),
                "settle_price": _to_float(row.get("SttlmPric")),
                "contracts": _to_int(row.get("TtlTradgVol")),
                "value_lakh": (turnover / 100000.0) if turnover is not None else None,
                "open_interest": _to_int(row.get("OpnIntrst")),
                "change_in_oi": _to_int(row.get("ChngInOpnIntrst")),
                "num_trades": _to_int(row.get("TtlNbOfTxsExctd")),
                "contract_name": (row.get("FinInstrmNm") or "").strip() or None,
                "source_format": "udiff",
            })
        return docs

    # -- public ------------------------------------------------------------- #

    def fetch_day(self, dt: date) -> list[dict] | None:
        """
        Returns a list of normalized futures docs for `dt`, an empty list if the
        day is a valid non-trading day (holiday), or None only on hard failure
        after retries (so the caller can decide to stop).
        """
        use_new_first = dt >= FORMAT_CUTOVER
        # (fetcher, parser) ordered by which to try first
        primary = (self._fetch_new_raw, self._parse_new) if use_new_first \
            else (self._fetch_old_raw, self._parse_old)
        secondary = (self._fetch_old_raw, self._parse_old) if use_new_first \
            else (self._fetch_new_raw, self._parse_new)

        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                fetch, parse = primary
                text = fetch(dt)
                if text is None:
                    # try the other format (boundary/holiday tolerance)
                    fetch2, parse2 = secondary
                    text = fetch2(dt)
                    if text is None:
                        return []  # genuine non-trading day / no file
                    return parse2(text, dt)
                return parse(text, dt)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                backoff = self.cfg.request_delay * (2 ** (attempt - 1))
                log.warning("  fetch error for %s (attempt %d/%d): %s -> retry in %.1fs",
                            dt, attempt, self.cfg.max_retries, exc, backoff)
                time.sleep(backoff)
        log.error("  giving up on %s after %d attempts: %s",
                  dt, self.cfg.max_retries, last_exc)
        return None


# --------------------------------------------------------------------------- #
# MongoDB
# --------------------------------------------------------------------------- #

class MongoStore:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = MongoClient(cfg.mongodb_uri, serverSelectionTimeoutMS=15000)
        # Fail fast if the URI is bad.
        self.client.admin.command("ping")
        self.db = self.client[cfg.db_name]
        self.col = self.db[cfg.collection]
        self.progress = self.db[cfg.progress_collection]
        self._ensure_indexes()

    def _ensure_indexes(self):
        self.col.create_index(
            [("trading_date", 1), ("symbol", 1), ("expiry", 1), ("instrument", 1)],
            unique=True,
            name="uniq_contract_day",
        )
        self.col.create_index([("symbol", 1), ("trading_date", 1)], name="symbol_date")

    def completed_dates(self) -> set[str]:
        return {d["_id"] for d in self.progress.find({"status": "done"}, {"_id": 1})}

    def store_day(self, dt: date, docs: list[dict]) -> int:
        if docs:
            ops = [
                ReplaceOne(
                    {
                        "trading_date": d["trading_date"],
                        "symbol": d["symbol"],
                        "expiry": d["expiry"],
                        "instrument": d["instrument"],
                    },
                    d,
                    upsert=True,
                )
                for d in docs
            ]
            try:
                self.col.bulk_write(ops, ordered=False)
            except BulkWriteError as bwe:
                # ignore duplicate-key races, re-raise anything else
                non_dupes = [e for e in bwe.details.get("writeErrors", [])
                             if e.get("code") != 11000]
                if non_dupes:
                    raise
        self.progress.replace_one(
            {"_id": dt.isoformat()},
            {"_id": dt.isoformat(), "status": "done",
             "count": len(docs), "ingested_at": datetime.utcnow()},
            upsert=True,
        )
        return len(docs)

    def close(self):
        self.client.close()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def load_config(args) -> Config:
    load_dotenv()
    uri = os.getenv("MONGODB_URI")
    if not uri:
        sys.exit("ERROR: MONGODB_URI is not set. Copy .env.example to .env and "
                 "paste your MongoDB connection string.")

    end = (datetime.strptime(args.end, "%Y-%m-%d").date() if args.end
           else _env_date("END_DATE") or date.today())
    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
    else:
        start = _env_date("START_DATE")
        if start is None:
            years = int(os.getenv("YEARS_BACK", "7"))
            try:
                start = end.replace(year=end.year - years)
            except ValueError:  # Feb 29
                start = end.replace(year=end.year - years, day=28)

    instruments = [s.strip().upper() for s in
                   os.getenv("INSTRUMENTS", "STOCK_FUT").split(",") if s.strip()]
    for i in instruments:
        if i not in INSTRUMENT_CODES:
            sys.exit(f"ERROR: unknown instrument '{i}'. "
                     f"Valid: {list(INSTRUMENT_CODES)}")

    return Config(
        mongodb_uri=uri,
        db_name=os.getenv("MONGO_DB", "nse_fno"),
        collection=os.getenv("MONGO_COLLECTION", "stock_futures"),
        progress_collection=os.getenv("MONGO_PROGRESS_COLLECTION", "ingest_progress"),
        start_date=start,
        end_date=end,
        request_delay=float(os.getenv("REQUEST_DELAY", "1.5")),
        max_retries=int(os.getenv("MAX_RETRIES", "4")),
        instruments=instruments,
        dry_run=args.dry_run,
    )


def _env_date(key: str) -> date | None:
    v = os.getenv(key)
    return datetime.strptime(v, "%Y-%m-%d").date() if v else None


def run(cfg: Config):
    fetcher = BhavcopyFetcher(cfg)
    store = None
    completed: set[str] = set()
    if not cfg.dry_run:
        store = MongoStore(cfg)
        completed = store.completed_dates()
        log.info("Resuming: %d dates already ingested will be skipped.", len(completed))

    log.info("Backfilling %s -> %s | instruments=%s | delay=%.1fs",
             cfg.start_date, cfg.end_date, cfg.instruments, cfg.request_delay)

    total_days = total_rows = trading_days = skipped = 0
    for dt in daterange(cfg.start_date, cfg.end_date):
        # Skip weekends (NSE is closed Sat/Sun) without hitting the network.
        if dt.weekday() >= 5:
            continue
        if dt.isoformat() in completed:
            skipped += 1
            continue

        total_days += 1
        docs = fetcher.fetch_day(dt)

        if docs is None:
            log.error("Stopping: unrecoverable error on %s. Re-run to resume.", dt)
            break
        if docs:
            trading_days += 1
            if store:
                n = store.store_day(dt, docs)
            else:
                n = len(docs)
            total_rows += n
            log.info("%s  stored %4d futures rows", dt, n)
        else:
            # holiday / no file — record it so we don't refetch on resume
            if store:
                store.progress.replace_one(
                    {"_id": dt.isoformat()},
                    {"_id": dt.isoformat(), "status": "done", "count": 0,
                     "note": "no-data", "ingested_at": datetime.utcnow()},
                    upsert=True,
                )
            log.info("%s  no data (holiday/non-trading)", dt)

        # Be polite: serial, one request at a time, spaced out.
        time.sleep(cfg.request_delay)

    log.info("Done. processed_days=%d trading_days=%d rows=%d skipped(existing)=%d",
             total_days, trading_days, total_rows, skipped)
    if store:
        store.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill NSE F&O stock futures into MongoDB")
    parser.add_argument("--start", help="Start date YYYY-MM-DD (overrides .env/YEARS_BACK)")
    parser.add_argument("--end", help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and parse but do not write to MongoDB")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = load_config(args)
    try:
        run(cfg)
    except KeyboardInterrupt:
        log.warning("Interrupted by user. Progress is saved; re-run to resume.")


if __name__ == "__main__":
    main()
