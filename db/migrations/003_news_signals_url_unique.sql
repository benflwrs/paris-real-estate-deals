-- Guard against duplicate news signal inserts (belt-and-suspenders on top
-- of the Python-side URL dedup in run_news_watcher.py).
CREATE UNIQUE INDEX IF NOT EXISTS news_signals_url_unique_idx
    ON news_signals (url) WHERE url IS NOT NULL;
