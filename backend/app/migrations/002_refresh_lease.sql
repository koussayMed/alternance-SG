-- Refresh lease: when the cache expires, only one request (across replicas) calls the weather API.
ALTER TABLE weather_cache ADD COLUMN refresh_lease_until TIMESTAMPTZ;
