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
- [x] Phase 1.5 — PAP scraper (`pipeline/scrapers/pap.py`) — parsing logic built + unit-tested; **live bypass blocked, see note below**
- [x] Phase 1.6 — SeLoger + LeBonCoin scrapers (`pipeline/scrapers/seloger.py`, `leboncoin.py`) — parsing logic built + unit-tested; **live bypass blocked, see note below**
- [x] Phase 1.7 — Orchestration/scheduling (`pipeline/run_all.py`, `infra/cron/scrape.cron`, live cron on VPS)
- [x] Phase 2.3 — Value/cost-effectiveness scoring model (`pipeline/scoring/value.py`) — hedonic regression on DVF, verified on 159k+ real transactions
- [x] Phase 2 — Transit LINE ranking + property score (`pipeline/scoring/transit_lines.py`, `transit_property.py`) — replaces the earlier "time to Châtelet" approach per Ben's direction: ranks every Metro/RER/Tram line by ridership + documented quality adjustments, then sums nearby-good-line scores per property (full credit ≤12min walk, half credit 12-20min)
- [x] Phase 2 — Feature-impact regression (`pipeline/scoring/feature_impact.py`) — quantifies % price impact of floor, elevator, ground floor, balcony, parking, building age, DPE, controlling for DVF zone baseline
- [x] Phase 2 — Zone popularity scoring (`pipeline/scoring/zone_popularity.py`) — listing velocity (demand proxy), studio/family household-mix profile, relative price tier, split by buy vs rent
- [x] Phase 2 — News watcher (`pipeline/scoring/news_watcher.py`) — rule-based RSS (Le Journal du Grand Paris + Google News search) + French keyword matching + BAN geocoding, no LLM, live-verified finding real Grand Paris Express / urban renewal articles
- [ ] Phase 3 — Frontend map

### ⚠️ Known blocker: PAP / SeLoger / LeBonCoin anti-bot walls

All three sit behind Cloudflare (PAP) or DataDome (SeLoger, LeBonCoin) managed challenges that
**block headless Chromium outright, regardless of IP** — confirmed by live testing both from the
dev sandbox AND from Ben's VPS (23.88.42.87) after fixing an unrelated font-rendering crash there
(headless Chromium needs a full font stack — `fontconfig` + real font files + a `fonts.conf` that
actually points at them — or it SIGTRAPs on any page with real text, which is a separate gotcha
worth remembering for any future headless-browser deployment on a minimal VPS). Once that was
fixed, PAP/SeLoger/LeBonCoin still never resolved past their challenge pages even from the VPS's
own IP — this matches what commercial scraping services (Apify actors, etc.) document: they
require **residential/mobile proxies** for these three sites specifically (Bien'ici is the
outlier that works from datacenter IPs with no proxy at all, which is why it was built and
verified first, and is now running end-to-end against the live production DB).

The scraper *parsing logic* for all three (`_to_listing_from_card` / `_to_listing`, JSON-LD and
`__NEXT_DATA__` extraction) is complete and unit-tested against realistic fixtures — only the
"get past the bot wall" step is blocked, and it's now confirmed to need a paid residential/mobile
proxy service (e.g. Apify's proxy, Bright Data) since VPS IP reputation didn't help. `base.py`-style
scrapers were written with an eye toward dropping in a proxy config later (see
`iter_listings_from_browser` — takes a `browser_context` the caller controls, so a proxied context
is a drop-in change, no scraper logic rewrite needed).

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
cp .env.example .env         # edit password before any real deployment
docker compose up -d         # starts Postgres/PostGIS on 127.0.0.1:5432
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

`docker-compose.yml` and `.env.example` live at the repo root (required by the deploy bot).
Cron schedule reference for the scrape/import jobs: `infra/cron/scrape.cron`.

### VPS environment notes (no-root setup)

The `botop` deploy user has no sudo, so Python/Playwright dependencies not covered by `uv sync`
are installed userspace-only via `apt-get download` + `dpkg -x` (no root needed — see the
`linux-no-root-toolchain-setup` pattern). This matters for two things:

1. **Playwright's Chromium/headless-shell binaries** need several system `.so` libs
   (`libnspr4`, `libnss3`, X11 libs, `libavahi-*`, etc.) that aren't present on a minimal Debian
   VPS. Download + extract them into a local prefix and export `LD_LIBRARY_PATH` to point at it.
2. **Font rendering** — headless Chromium hard-crashes (`SIGTRAP`, Skia `SkFontMgr` fatal error)
   on any page with real visible text if there's no fontconfig setup at all, not just a missing
   font. Fix: extract `fontconfig-config` + `fonts-dejavu-core` (or similar) the same way, write a
   minimal `fonts.conf` pointing `<dir>` at the extracted font path (the system default
   `/etc/fonts/fonts.conf` points at absolute paths like `/usr/share/fonts` that don't exist in
   this setup), and export `FONTCONFIG_PATH` to that directory.

With both fixed, headless Chromium runs stably on the VPS — proven by successfully rendering
Wikipedia and Bien'ici. It still cannot get past PAP/SeLoger/LeBonCoin's bot walls (see above);
that is a fingerprint/IP-reputation block, unrelated to this environment setup.
