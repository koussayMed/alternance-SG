-- City suggestion (geocoding) cache shared by all replicas, so new replicas do not
-- call Open-Meteo again for the same texts (bursts caused 429s in the load test).
CREATE TABLE IF NOT EXISTS geocode_cache (
    query      TEXT PRIMARY KEY,          -- normalized text (collapsed spaces, lowercase)
    results    JSONB NOT NULL,            -- list of results (may be empty)
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
