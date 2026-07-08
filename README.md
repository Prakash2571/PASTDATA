# NSE F&O Stock-Futures Bhavcopy → MongoDB

Backfills **NSE Futures & Options *stock-futures* bhavcopy** data for the last 7 years
(configurable) into MongoDB, one trading day at a time.

For each F&O stock it stores every futures contract trading that day — the **near, next,
and far month** expiries — with OHLC, settlement price, volume, open interest, and change
in OI. That's roughly **950,000 documents** across ~1,800 trading days.

- **Serial & polite** — downloads one day at a time with a delay between requests, so it
  never overloads NSE (avoids rate-limiting/blocks) or MongoDB.
- **Handles both NSE formats** — the old `FUTSTK` bhavcopy (via `jugaad-data`, pre-2024-07-08)
  and the new UDiFF `STF` format (from 2024-07-08), normalized into one schema.
- **Resumable & idempotent** — a progress collection + a unique index mean you can stop and
  rerun any time; completed days are skipped and no duplicates are created.
- **Self-contained** — MongoDB runs in Docker; no external database or connection string
  needed. Export the whole dataset to a single portable file when done.

---

## Quick start (serial commands)

Run these **in order** on a fresh Ubuntu/Debian instance (1 vCPU, 2 GB RAM, 10 GB disk is plenty):

```bash
# 1. Get the code
git clone https://github.com/Prakash2571/PASTDATA.git
cd PASTDATA

# 2. Install everything (Docker, Compose, screen, make) + add swap. Run once.
bash setup.sh

# 3. Enable docker without sudo (or just log out and back in)
newgrp docker

# 4. Build the backfill image
make build

# 5. Start MongoDB + launch the 7-year backfill (runs in a detached 'screen' session)
make run

# 6. Watch it work (any of these; safe to run in a second SSH tab)
make logs        # follow the live log
make monitor     # one-time DB snapshot: docs, days done, date range
make watch       # auto-refreshing snapshot every 30s

# 7. When it finishes, export the whole dataset to one portable file
make export      # -> fno_dump_YYYYMMDD.archive.gz
```

Then, **from your local machine**, copy it down and restore into your local MongoDB:

```bash
scp <user>@<instance-ip>:~/PASTDATA/fno_dump_YYYYMMDD.archive.gz .
mongorestore --archive=fno_dump_YYYYMMDD.archive.gz --gzip
# data lands in the "nse_fno" database on your local mongodb://localhost:27017
```

---

## Ports & security

- **AWS Security Group (inbound):** only **SSH (TCP 22)** from your IP. Nothing else.
- **App ports:** none. MongoDB stays inside Docker's private network; the script only makes
  *outbound* HTTPS (443) calls to NSE. Do **not** expose port 27017 to the internet.

---

## How do I know it's working?

While `make run` is going, `make logs` shows one line per trading day:

```
INFO Backfilling 2018-07-09 -> 2025-07-08 | instruments=['STOCK_FUT'] | delay=1.5s
INFO 2018-07-09  stored  480 futures rows
INFO 2018-07-10  stored  482 futures rows
INFO 2018-07-14  no data (holiday/non-trading)
...
INFO Done. processed_days=1800 trading_days=1730 rows=940000 skipped(existing)=0
```

- `stored NNN futures rows` on weekdays (NNN ≈ 450–550) = **working correctly**.
- `no data` = market holiday, normal.
- Occasional `fetch error ... retry` = a transient hiccup, handled by built-in backoff.
- `make monitor` should show the document count and `date range` climbing toward today.

**Estimated time:** ~1.5–3 hours (bottleneck is NSE response time + the polite delay, not CPU/RAM).

---

## All `make` commands

| Command | Description |
|---|---|
| `make setup` | Install Docker + Compose + screen + make, and add swap (run once) |
| `make build` | Build the backfill image |
| `make run` | Start MongoDB + launch the backfill in a detached `screen` session |
| `make attach` | Attach to the live `screen` session (Ctrl-A then D to detach) |
| `make logs` | Follow the backfill log (`backfill.log`) |
| `make monitor` | One-time snapshot of ingest progress from MongoDB |
| `make watch` | Live-refresh the progress every 30s |
| `make export` | Dump the whole dataset to a portable `.archive.gz` |
| `make stop` | Stop MongoDB (data on disk is kept) |
| `make down` | Remove containers (data in `./data/mongo` is kept) |
| `make status` | Show container status |
| `make purge` | **DANGER:** remove containers and delete all data in `./data` |

If it stops for any reason (SSH drop, error, NSE block), just run `make run` again — it resumes
from where it left off.

---

## Configuration

Settings live in the `backfill` service's `environment:` block in
[`docker-compose.yml`](docker-compose.yml):

| Variable | Default | Meaning |
|---|---|---|
| `YEARS_BACK` | `7` | Years of history to pull (from today going back) |
| `REQUEST_DELAY` | `1.5` | Seconds to sleep between each day's download |
| `INSTRUMENTS` | `STOCK_FUT` | What to keep. Set `STOCK_FUT,INDEX_FUT` to also include index futures |
| `MONGO_DB` | `nse_fno` | Target database name |
| `MONGO_COLLECTION` | `stock_futures` | Target collection name |

You can also run a custom date range directly:

```bash
docker compose up -d mongo
docker compose run --rm backfill --start 2024-01-01 --end 2024-03-31
docker compose run --rm backfill --dry-run          # fetch + parse, no DB writes
```

---

## Data model

Each document is one futures contract on one trading day:

| Field | Description |
|---|---|
| `trading_date` | Trading day (date) |
| `symbol` | Underlying stock symbol (e.g. `RELIANCE`) |
| `instrument` | `FUTSTK` (old format) or `STF` (UDiFF) |
| `expiry` | Contract expiry date |
| `open`, `high`, `low`, `close` | OHLC prices |
| `settle_price` | Daily settlement price |
| `contracts` | Volume (contracts / quantity traded) |
| `value_lakh` | Traded value (in lakh) |
| `open_interest` | Open interest |
| `change_in_oi` | Change in open interest vs previous day |
| `num_trades`, `contract_name` | Extra fields (new UDiFF format only) |
| `source_format` | `old` or `udiff` — marks which NSE format the row came from |

**Unique key:** `(trading_date, symbol, expiry, instrument)`.

> **Note on units:** open-interest and volume are reported differently in the two NSE formats
> (the pre-2024 bhavcopy vs the newer UDiFF format). Values are stored as reported by NSE; use
> the `source_format` field to normalize on your side if needed.

---

## Notes

- **Data location on the instance:** raw MongoDB files persist at `./data/mongo`. For moving
  the dataset elsewhere, prefer `make export` — the `.archive.gz` is portable across machines
  and MongoDB versions, unlike raw data files.
- **NSE IP blocking:** NSE sometimes blocks non-Indian datacenter IPs. If the logs show
  *continuous* errors instead of `stored ... rows`, run the instance in the Mumbai region
  (`ap-south-1`) and rerun (it resumes automatically).
