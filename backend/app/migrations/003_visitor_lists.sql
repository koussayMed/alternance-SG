-- One city list per visitor; places and their weather cache stay shared
-- (100 visitors following Paris = one "Paris" row and one API call per period).
ALTER TABLE cities RENAME TO places;

CREATE TABLE favorites (
    visitor_id TEXT        NOT NULL,
    place_id   BIGINT      NOT NULL REFERENCES places (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (visitor_id, place_id)
);

CREATE INDEX favorites_place_id_idx ON favorites (place_id);
