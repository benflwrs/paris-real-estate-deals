# Paris Real Estate Deal-Finder

Personal project (Ben) to find the best real-estate deals in Paris & Île-de-France:
scrapes live listings, scores them for cost-effectiveness against official
sold-price data, factors in transit accessibility and upcoming urban projects,
and visualizes everything as heatmaps on a map.

**Full implementation plan:** [`.hermes/plans/2026-09-11_paris-real-estate-deals-app.md`](.hermes/plans/2026-09-11_paris-real-estate-deals-app.md)

## Status

- [x] Phase 1.1 — Postgres/PostGIS docker-compose stack (`infra/`)
- [x] Phase 1.2 — Core DB schema (`db/schema.sql`)
- [x] Phase 1.3 — DVF (official sold-transaction) importer (`pipeline/dvf_import.py`) — verified against real 2024 Paris data (median ~€10.1k/m², matches known market figures)
- [x] Phase 1.4 — Bien'ici scraper (`pipeline/scrapers/bienici.py`) — verified live against bienici.com
- [ ] Phase 1.5 — PAP scraper
- [ ] Phase 1.6 — SeLoger + LeBonCoin scrapers (Playwright-based)
- [ ] Phase 1.7 — Orchestration/scheduling
- [ ] Phase 2 — Brain/analytics layer (value scoring, transit scoring, popularity, news watcher)
- [ ] Phase 3 — Frontend map

## Data sources

- **Live listings**: scraped from Bien'ici (public JSON API, no proxy needed), PAP, SeLoger, LeBonCoin.
  This is a personal project; scraping risk is accepted but scrapers are built to be polite
  (rate-limited, realistic headers) and resilient (per-listing error handling).
- **DVF** (Demandes de Valeurs Foncières, data.gouv.fr / files.data.gouv.fr/geo-dvf): official
  government record of *closed sale transactions*. Used ONLY as reference data to calibrate the
  "fair value" model — never displayed as if it were a live listing (copyright/ToS-safe, and
  it's the ground truth for computing whether a listing is a good deal for its area/size/floor).

## Local development

### Prerequisites
- Python 3.12, [uv](https://github.com/astral-sh/uv) installed.
- Docker + Docker Compose (for Postgres/PostGIS). Note: this repo's dev sandbox did not have a
  running Docker daemon, so DB-touching code is designed to be testable without a live DB
  (unit tests use fixtures/mocks); integration testing against a real Postgres should happen
  wherever Docker is available (e.g. the target VPS).

### Setup
```bash
cd paris-real-estate
uv sync                      # installs deps into .venv/
cp infra/.env.example infra/.env   # edit password before any real deployment
cd infra && docker compose up -d   # starts Postgres/PostGIS on 127.0.0.1:5432
```

### Run tests
```bash
source .venv/bin/activate
python -m pytest pipeline/ -v
```

### Run the Bien'ici scraper manually
```bash
source .venv/bin/activate
python -m pipeline.scrapers.bienici --place "Paris" --transaction-type buy --max-results 50
```

### Import DVF reference data (Ile-de-France)
```bash
source .venv/bin/activate
python -m pipeline.dvf_import --years 2024 2025 --departments 75 92 93 94
```

## Deployment

Target: Ben's existing VPS (23.88.42.87), as its own docker-compose project, isolated network
from other services on that box, fully tearable down with `docker compose down -v`.
Compiled data / DB contents stay on the VPS — only source code is pushed to this repo.
