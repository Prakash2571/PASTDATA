.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup build run attach logs monitor watch export stop down status purge

help:  ## Show available commands
	@echo "NSE F&O stock-futures backfill — available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  make %-9s %s\n", $$1, $$2}'

setup:  ## Install Docker+Compose+screen and add swap (run once on a fresh instance)
	@bash setup.sh

build:  ## Build the backfill image
	docker compose build

run:  ## Start MongoDB + launch the 7-year backfill in a detached screen session
	docker compose up -d mongo
	@screen -dmS fno bash -c 'docker compose run --rm backfill 2>&1 | tee -a backfill.log'
	@echo ""
	@echo "Backfill started in screen session 'fno'."
	@echo "  make logs       # follow progress (Ctrl-C to stop watching, job keeps running)"
	@echo "  make monitor    # one-time DB snapshot"
	@echo "  make watch      # auto-refreshing DB snapshot"
	@echo "  screen -r fno   # attach to the live session (Ctrl-A then D to detach)"

attach:  ## Attach to the live screen session
	screen -r fno

logs:  ## Follow the backfill log
	@touch backfill.log && tail -f backfill.log

monitor:  ## One-time snapshot of ingest progress from MongoDB
	@bash monitor.sh

watch:  ## Live-refresh ingest progress every 30s
	watch -n 30 'bash monitor.sh'

export:  ## Dump the whole dataset to a portable .archive.gz
	@./export_data.sh

stop:  ## Stop MongoDB (data on disk is kept)
	docker compose stop

down:  ## Stop and remove containers (data in ./data/mongo is kept)
	docker compose down

status:  ## Show container status
	docker compose ps

purge:  ## DANGER: remove containers AND delete all downloaded data (./data)
	docker compose down -v
	sudo rm -rf ./data
