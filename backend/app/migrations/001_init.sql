-- Followed cities. external_id = Open-Meteo (geonames) id: prevents duplicates.
CREATE TABLE cities (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_id BIGINT           NOT NULL UNIQUE,
    name        TEXT             NOT NULL,
    country     TEXT,
    latitude    DOUBLE PRECISION NOT NULL,
    longitude   DOUBLE PRECISION NOT NULL,
    created_at  TIMESTAMPTZ      NOT NULL DEFAULT now()
);

-- Last known weather per city: cache (TTL) and fallback for degraded mode.
CREATE TABLE weather_cache (
    city_id       BIGINT PRIMARY KEY REFERENCES cities (id) ON DELETE CASCADE,
    temperature_c DOUBLE PRECISION NOT NULL,
    wind_kmh      DOUBLE PRECISION NOT NULL,
    weather_code  INTEGER,
    observed_at   TIMESTAMPTZ      NOT NULL,
    fetched_at    TIMESTAMPTZ      NOT NULL
);
