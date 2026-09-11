# Paris Real Estate Deal-Finder — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task, starting with Phase 1 (Data).

**Goal:** Build a personal (extensible to friends/family) app that scrapes/ingests Paris & Île-de-France real-estate listings, scores each property on cost-effectiveness/investment value using DVF sold-price baselines + transit accessibility + neighborhood trends, and visualizes it as heatmaps on a map.

**Architecture:** Three decoupled layers sharing one Postgres/PostGIS database on Ben's existing VPS (23.88.42.87):
1. **Data layer** — scrapers (Bien'ici, PAP, SeLoger, LeBonCoin) write raw+normalized listings to Postgres; a separate one-time/periodic DVF importer loads historical transactions into a reference table used ONLY for valuation calibration (never surfaced as a listing).
2. **Brain layer** — batch jobs compute per-property and per-zone scores (value rating, transit rating, trend rating) and write them back to Postgres; a lightweight news/projects watcher (RSS + keyword + geocoding, no LLM) updates a "future upside" signal per zone.
3. **Frontend** — a map (Leaflet/MapLibre) reading from a small API (FastAPI) serving GeoJSON tiles/points for heatmap layers.

**Tech stack:** Python 3.12, PostgreSQL 16 + PostGIS, Playwright (for JS-heavy sites) + httpx/requests (for API-backed sites like Bien'ici), APScheduler or cron for periodic jobs, FastAPI for the backend API, MapLibre GL JS + a static/React frontend, Docker Compose for deployment on the VPS.

---

## Phase 0 — Decisions locked in from planning conversation

- Legal stance: personal project, broad scraping accepted (Bien'ici, PAP, SeLoger, LeBonCoin). Respect basic courtesy (rate limit, realistic UA, don't hammer) but no proxy-rotation arms race needed initially.
- DVF (Demandes de Valeurs Foncières, data.gouv.fr) is the *ground truth* for "what did similar properties actually sell for here" — used to calibrate the value/cost-effectiveness score. It is never displayed as if it were a live listing.
- Infra: reuse existing VPS at 23.88.42.87 (already running tpof-server in Docker). New stack goes in its own docker-compose project/network so it can be torn down independently (`docker compose down -v`) per Ben's infra preference.
- News/projects signal: rule-based RSS + keyword matching + geocoding against known official sources (Grand Paris Express, IDF Mobilités, Paris mairie/arrondissement announcements) — no LLM in the initial version. Leave a hook to add an LLM classifier later only if the rule-based approach proves too noisy/sparse.
- Build order: Data → Brain → Frontend. Each phase should produce something independently testable before moving to the next.

---

## Phase 1 — Data Layer

### Task 1.1: Provision Postgres + PostGIS on the VPS

**Files:**
- Create: `infra/docker-compose.yml` (new project dir on VPS, e.g. `/opt/paris-deals/`)
- Create: `infra/.env.example`

**Steps:**
1. SSH to VPS, create `/opt/paris-deals/infra/`.
2. Write `docker-compose.yml` with a `postgis/postgis:16-3.4` service, named volume `pgdata`, exposed only on localhost (bind `127.0.0.1:5432:5432`) — DB should not be internet-facing; API layer proxies access.
3. `docker compose up -d`, then verify: `docker exec -it paris-deals-db psql -U postgres -c '\dx'` shows `postgis` extension after `CREATE EXTENSION postgis;`.

**Verification:** `psql -h 127.0.0.1 -U postgres -c 'SELECT PostGIS_Version();'` returns a version string.

### Task 1.2: Define the core schema

**Files:**
- Create: `db/schema.sql`
- Create: `db/migrations/` (use Alembic if the project grows; raw SQL is fine to start)

**Schema (initial):**
```sql
CREATE TABLE listings (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,               -- 'bienici' | 'pap' | 'seloger' | 'leboncoin'
  source_id TEXT NOT NULL,            -- site's own listing id
  url TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_active BOOLEAN NOT NULL DEFAULT true,  -- false once delisted
  transaction_type TEXT NOT NULL,     -- 'sale' | 'rent'
  price NUMERIC,
  price_per_m2 NUMERIC,               -- computed
  surface_m2 NUMERIC,
  rooms INT,
  bedrooms INT,
  property_type TEXT,                 -- 'appartement' | 'maison' | 'parking' | ...
  floor INT,                          -- null if unknown; 0 = rez-de-chaussée
  floor_count INT,                    -- total floors in building, if known
  has_elevator BOOLEAN,
  in_residence BOOLEAN,
  construction_year INT,
  is_furnished BOOLEAN,
  has_balcony BOOLEAN,
  has_parking BOOLEAN,
  dpe_class TEXT,                     -- energy rating, useful bonus signal
  address_raw TEXT,
  postal_code TEXT,
  city TEXT,
  insee_code TEXT,
  geom GEOGRAPHY(POINT, 4326),        -- lon/lat
  raw_json JSONB,                     -- full scraped payload for reprocessing
  UNIQUE(source, source_id)
);
CREATE INDEX listings_geom_idx ON listings USING GIST(geom);
CREATE INDEX listings_active_idx ON listings(is_active);

CREATE TABLE dvf_transactions (      -- reference-only, never shown as a listing
  id BIGSERIAL PRIMARY KEY,
  mutation_date DATE,
  price NUMERIC,
  surface_m2 NUMERIC,
  rooms INT,
  property_type TEXT,
  insee_code TEXT,
  geom GEOGRAPHY(POINT, 4326)
);
CREATE INDEX dvf_geom_idx ON dvf_transactions USING GIST(geom);

CREATE TABLE zones (                 -- IRIS or custom grid cells for aggregation
  id BIGSERIAL PRIMARY KEY,
  name TEXT,
  insee_code TEXT,
  geom GEOGRAPHY(POLYGON, 4326)
);

CREATE TABLE zone_scores (
  zone_id BIGINT REFERENCES zones(id),
  as_of DATE NOT NULL,
  median_price_m2 NUMERIC,
  price_trend_3m NUMERIC,             -- % change
  popularity_buy_score NUMERIC,       -- 0-100
  popularity_rent_score NUMERIC,
  transit_score NUMERIC,              -- 0-100, avg accessibility
  time_to_chatelet_min NUMERIC,       -- minutes, representative point
  upside_score NUMERIC,               -- from news/projects signal
  PRIMARY KEY (zone_id, as_of)
);

CREATE TABLE listing_scores (
  listing_id BIGINT REFERENCES listings(id) PRIMARY KEY,
  value_score NUMERIC,                -- cost-effectiveness vs DVF-calibrated expected price
  transit_score NUMERIC,
  time_to_chatelet_min NUMERIC,
  overall_score NUMERIC,
  computed_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE news_signals (
  id BIGSERIAL PRIMARY KEY,
  source TEXT,
  title TEXT,
  url TEXT,
  published_at TIMESTAMPTZ,
  keywords_matched TEXT[],
  geom GEOGRAPHY(POINT, 4326),        -- geocoded location if resolvable
  insee_code TEXT,
  impact_weight NUMERIC DEFAULT 1.0,
  raw_text TEXT
);
```

**Verification:** `psql -f db/schema.sql` runs clean; `\dt` lists all 6 tables.

### Task 1.3: DVF ingestion script (reference data)

**Files:**
- Create: `pipeline/dvf_import.py`

**Steps:**
1. Download the latest "DVF géolocalisées" CSV.gz for Île-de-France departments (75, 77, 78, 91, 92, 93, 94, 95) from data.gouv.fr (`https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees`).
2. Parse with pandas/polars, filter to `type_local IN ('Appartement','Maison')`, drop rows with null price/surface/lat/lon.
3. Bulk-insert into `dvf_transactions` via `psycopg2.extras.execute_values` (batch of 5–10k rows).
4. Schedule this as a twice-yearly cron job (matches DVF's April/October update cadence).

**Verification:** `SELECT count(*) FROM dvf_transactions;` returns a nontrivial number (expect 100k+ rows for IDF); spot check a known arrondissement's median price/m² looks sane (~€9-11k/m² central Paris as of recent years).

### Task 1.4: Bien'ici scraper (start here — API-backed, no proxy, lowest friction)

**Files:**
- Create: `pipeline/scrapers/bienici.py`
- Create: `pipeline/scrapers/base.py` (shared `Listing` dataclass + upsert-to-DB helper)
- Test: `pipeline/scrapers/test_bienici.py`

**Steps:**
1. Bien'ici exposes a documented-by-reverse-engineering JSON search endpoint (`https://www.bienici.com/realEstateAds.json?filters=...`). Confirm current shape by inspecting network calls (Playwright headless, capture the XHR) since it can change.
2. Write `fetch_page(filters, page)` returning parsed JSON; map fields to the shared `Listing` schema (price, surface, rooms, floor, elevator, balcony, parking, construction date if present, DPE).
3. Implement pagination + polite rate limiting (1 request per 2-3s, randomized).
4. Upsert into `listings` table on `(source, source_id)`; update `last_seen_at`; mark listings not seen in the latest full pass as `is_active = false`.
5. Write a unit test using a saved fixture JSON response (no live network) to verify field mapping.

**Verification:** Run `python pipeline/scrapers/bienici.py --city paris --max-pages 2` locally; confirm rows appear in `listings` with `source='bienici'` and non-null price/surface/geom.

### Task 1.5: PAP scraper

Same structure as 1.4, targeting pap.fr (particulier-only, HTML scraping with BeautifulSoup/selectolax since no public API — lower volume but zero agency noise).

### Task 1.6: SeLoger + LeBonCoin scrapers

**Files:**
- Create: `pipeline/scrapers/seloger.py`
- Create: `pipeline/scrapers/leboncoin.py`

**Steps:**
1. Both sites use heavier anti-bot (Datadome/Cloudflare-style challenges) — use Playwright with a persistent browser context (real Chromium, stealth patches via `playwright-stealth` or `undetected-playwright`) rather than raw HTTP.
2. LeBonCoin has a semi-documented internal API (`api.leboncoin.fr/finder/search`) used by several open-source aggregators — try that first before falling back to page scraping, it's much cheaper.
3. SeLoger: scrape the rendered listing search pages; extract from embedded `__NEXT_DATA__`/JSON-LD blocks where present rather than fragile CSS selectors.
4. Keep concurrency low (1-2 browsers), randomized delays 3-8s, rotate a small pool of realistic user agents. No need for residential proxies at personal-project volume, but structure the code so a proxy can be dropped in later (`base.py` should take an optional proxy config).
5. Expect these to break periodically — wrap each scraper run in try/except per-listing so one bad page doesn't kill the batch, and log failures to a `scrape_errors` table/file for triage.

**Verification:** Same as 1.4 — confirm rows land with correct `source` tag; spot-check 5 listings against the live site for field accuracy.

### Task 1.7: Orchestration & scheduling

**Files:**
- Create: `pipeline/run_all.py`
- Create: `infra/cron/scrape.cron` (or APScheduler service in docker-compose)

**Steps:**
1. `run_all.py` runs all 4 scrapers sequentially (to avoid overloading the VPS), then a "mark stale" pass (listings unseen for >X days → `is_active=false`), then triggers Phase 2 scoring jobs.
2. Schedule via cron on the VPS: e.g. every 6 hours for Bien'ici/PAP (light), once daily for SeLoger/LeBonCoin (heavier, higher block risk — don't over-hit them).
3. Add basic alerting: if a scraper returns 0 new listings for 3 consecutive runs, log/flag (likely site changed or got blocked) — surface this via Discord webhook or simple log file Ben can check.

**Verification:** Let it run unattended for 24-48h; check `listings` growth and `is_active` counts look reasonable, no runaway errors in logs.

---

## Phase 2 — Brain / Analytics Layer

### Task 2.1: Zone definitions

Use IRIS units (INSEE's ~2000-person statistical zones, official shapefiles free on data.gouv.fr / INSEE) as the base geography for aggregation — finer than arrondissement, coarse enough to have enough listings per zone for stats. Load into `zones` table.

### Task 2.2: Transit accessibility scoring

**Files:** `pipeline/scoring/transit.py`

1. Use IDF Mobilités PRIM API (free API key, register at prim.iledefrance-mobilites.fr) — either the isochrone endpoint or the journey-planner (Navitia-based) endpoint to compute travel time from each zone centroid (and eventually each listing point) to Châtelet-Les Halles at a fixed reference time (e.g. weekday 8:30am).
2. Cache results per zone (travel times don't change often) — recompute monthly or on schedule changes, not per listing.
3. `transit_score` = normalized (0-100) inverse function of time-to-Châtelet + density of nearby stops (walk time to nearest metro/RER/bus, from GTFS stops.txt).

### Task 2.3: Value / cost-effectiveness score

**Files:** `pipeline/scoring/value.py`

1. For each active listing, find comparable DVF transactions (same INSEE code or zone, same `type_local`, similar surface ±20%, sold within last ~3 years).
2. Fit a simple hedonic regression (price ~ surface + rooms + floor + zone + construction_year) per zone or city-wide using DVF data — start with a straightforward linear/gradient-boosted regression (scikit-learn), not deep learning; this is tabular data with a handful of features, LLMs add nothing here.
3. `expected_price = model.predict(listing_features)`; `value_score = (expected_price - listing.price) / expected_price` normalized to 0-100 (higher = better deal). This naturally corrects for ground-floor discount, size, etc. since those are model features.
4. Store in `listing_scores.value_score`.

### Task 2.4: Zone popularity & trend scoring

**Files:** `pipeline/scoring/popularity.py`

1. "Most popular to buy/rent" ≈ listing velocity: how fast do sale/rental listings in a zone go inactive (proxy for demand) + volume of new listings (proxy for supply/turnover). Compute rolling 30-day stats per zone from `listings` (time between `first_seen_at` and `is_active` flip to false).
2. "For who" segmentation: cluster zones/listings by property profile (studio/1BR = young singles/students; 3BR+ = families; presence of nearby schools vs. nightlife density, if you want to enrich later with POI data from OpenStreetMap Overpass API — free, no LLM needed).
3. Price trend: `zone_scores.price_trend_3m` from rolling DVF + active-listing price/m² medians over time.

### Task 2.5: News/projects "upside" signal (no LLM, rule-based)

**Files:** `pipeline/scoring/news_watcher.py`

1. Poll RSS/announcement pages from: Société du Grand Paris (Grand Paris Express line/station openings), IDF Mobilités news, Paris.fr and key suburb mairie announcement pages (start with the ones covering areas Ben cares about, expand later).
2. Keyword match against a curated list (French): "nouvelle station", "mise en service", "extension de ligne", "rénovation urbaine", "ZAC", "quartier prioritaire", etc.
3. Geocode matched articles (mentioned street/station/commune name) via the free BAN (Base Adresse Nationale) geocoding API — no LLM needed, deterministic.
4. Write to `news_signals`; a scheduled job aggregates recent signals per zone into `zone_scores.upside_score` with a decay function (recent news weighted higher).
5. Leave an explicit extension point (a `classify_article(text) -> topics` function) where an LLM call could later replace/augment step 2 if the keyword approach proves too brittle — but ship without it.

**Verification for Phase 2:** Pick 3 known Paris neighborhoods with different profiles (e.g. 16th arr. wealthy/quiet, 11th arr. trendy/dense, a Grand Paris Express station suburb) and sanity-check their computed scores match intuition before trusting the model broadly.

---

## Phase 3 — Frontend (Map)

### Task 3.1: Minimal API layer

**Files:** `api/main.py` (FastAPI)

Endpoints:
- `GET /zones/heatmap?metric=value_score|transit_score|price_trend|upside_score` → GeoJSON FeatureCollection of zone polygons with the chosen metric as a property.
- `GET /listings?bbox=...&min_score=...` → GeoJSON points for the current map viewport, for a listing pin layer.
- `GET /listings/{id}` → full detail for a clicked property.

### Task 3.2: Map frontend

**Files:** `frontend/` (Vite + React + MapLibre GL JS, or plain HTML+JS if Ben wants something lighter)

1. Base map centered on Île-de-France; MapLibre choropleth layer colored by the selected `zones/heatmap` metric, with a dropdown to switch metrics (value / transit / trend / upside).
2. Listing pin layer on top, clustered at low zoom, clickable for detail popup (price, m², rooms, floor, score breakdown).
3. Filter panel: price range, surface, rooms, property type, min value_score.

**Verification:** Load in browser, confirm heatmap renders over IDF with plausible color gradients, clicking a zone/pin shows correct data from the API.

---

## Deployment notes

- Everything (Postgres, scrapers/cron, FastAPI, static frontend) ships as its own `docker-compose.yml` under `/opt/paris-deals/` on the VPS, isolated network from tpof-server, fully torn-down with `docker compose down -v` per Ben's infra preference.
- Compiled/scraped data stays on the VPS DB — nothing proprietary gets pushed to a public GitHub repo; only source code (scrapers, scoring, API, frontend) is public if Ben wants a repo for it, mirroring the pokemon-firered-badtranslation convention of "source public, artifacts local-only."

## Open questions for Ben (non-blocking, can decide during implementation)

1. Repo name/visibility for this project's source code?
2. Which suburbs beyond central Paris matter most initially, to scope the first news-watcher source list and IRIS zone set (start smaller, expand)?
3. Any specific property criteria (e.g. min 2 rooms, budget cap) to bake into default scraper filters, to avoid pulling in the entire IDF market from day one?

## Risks / tradeoffs

- SeLoger/LeBonCoin scrapers are the most likely to break (anti-bot changes) — budget for ongoing maintenance; Bien'ici/PAP are far more stable.
- DVF data lags reality by months (published semi-annually) — fine for calibrating "fair value" but not for real-time trend detection; active-listing price movement fills that gap.
- Rule-based news signal will miss nuanced stories; explicitly designed to be upgraded to LLM-assisted classification later without a schema change.
