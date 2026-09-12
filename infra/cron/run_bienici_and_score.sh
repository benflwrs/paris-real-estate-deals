#!/bin/bash
# Runs the Bien'ici scraper (no proxy needed, no anti-bot issues) and
# refreshes value scores. PAP/SeLoger/LeBonCoin are excluded here -- they
# need a residential proxy, see README.md "Known blocker" section.
set -e
cd /home/botop/paris-deals/repo
export PATH=$HOME/.local/bin:$PATH
source .venv/bin/activate
git pull --ff-only >> /home/botop/paris-deals/logs/scrape.log 2>&1 || true
python -m pipeline.scrapers.bienici --place "Paris" --transaction-type buy --max-results 500 >> /home/botop/paris-deals/logs/scrape.log 2>&1
python -m pipeline.scoring.run_value_scoring --insee-prefix 75 >> /home/botop/paris-deals/logs/scrape.log 2>&1
python -m pipeline.scoring.run_transit_scoring >> /home/botop/paris-deals/logs/scrape.log 2>&1
