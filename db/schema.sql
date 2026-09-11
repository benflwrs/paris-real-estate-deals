CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS listings (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,               -- 'bienici' | 'pap' | 'seloger' | 'leboncoin'
  source_id TEXT NOT NULL,            -- site's own listing id
  url TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_active BOOLEAN NOT NULL DEFAULT true,
  transaction_type TEXT NOT NULL,     -- 'sale' | 'rent'
  price NUMERIC,
  price_per_m2 NUMERIC,
  surface_m2 NUMERIC,
  rooms INT,
  bedrooms INT,
  property_type TEXT,
  floor INT,
  floor_count INT,
  has_elevator BOOLEAN,
  in_residence BOOLEAN,
  construction_year INT,
  is_furnished BOOLEAN,
  has_balcony BOOLEAN,
  has_parking BOOLEAN,
  dpe_class TEXT,
  address_raw TEXT,
  postal_code TEXT,
  city TEXT,
  insee_code TEXT,
  geom GEOGRAPHY(POINT, 4326),
  raw_json JSONB,
  UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS listings_geom_idx ON listings USING GIST(geom);
CREATE INDEX IF NOT EXISTS listings_active_idx ON listings(is_active);

CREATE TABLE IF NOT EXISTS dvf_transactions (
  id BIGSERIAL PRIMARY KEY,
  mutation_date DATE,
  price NUMERIC,
  surface_m2 NUMERIC,
  rooms INT,
  property_type TEXT,
  insee_code TEXT,
  geom GEOGRAPHY(POINT, 4326)
);
CREATE INDEX IF NOT EXISTS dvf_geom_idx ON dvf_transactions USING GIST(geom);

CREATE TABLE IF NOT EXISTS zones (
  id BIGSERIAL PRIMARY KEY,
  name TEXT,
  insee_code TEXT,
  geom GEOGRAPHY(POLYGON, 4326)
);

CREATE TABLE IF NOT EXISTS zone_scores (
  zone_id BIGINT REFERENCES zones(id),
  as_of DATE NOT NULL,
  median_price_m2 NUMERIC,
  price_trend_3m NUMERIC,
  popularity_buy_score NUMERIC,
  popularity_rent_score NUMERIC,
  transit_score NUMERIC,
  time_to_chatelet_min NUMERIC,
  upside_score NUMERIC,
  PRIMARY KEY (zone_id, as_of)
);

CREATE TABLE IF NOT EXISTS listing_scores (
  listing_id BIGINT REFERENCES listings(id) PRIMARY KEY,
  value_score NUMERIC,
  transit_score NUMERIC,
  time_to_chatelet_min NUMERIC,
  overall_score NUMERIC,
  computed_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS news_signals (
  id BIGSERIAL PRIMARY KEY,
  source TEXT,
  title TEXT,
  url TEXT,
  published_at TIMESTAMPTZ,
  keywords_matched TEXT[],
  geom GEOGRAPHY(POINT, 4326),
  insee_code TEXT,
  impact_weight NUMERIC DEFAULT 1.0,
  raw_text TEXT
);

CREATE TABLE IF NOT EXISTS scrape_errors (
  id BIGSERIAL PRIMARY KEY,
  source TEXT,
  occurred_at TIMESTAMPTZ DEFAULT now(),
  error_text TEXT,
  context JSONB
);
