FROM python:3.11-slim

WORKDIR /app

# Install deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY fno_futures_backfill.py .

# Runs the serial backfill; extra args (e.g. --start/--end) pass through.
ENTRYPOINT ["python", "fno_futures_backfill.py"]
