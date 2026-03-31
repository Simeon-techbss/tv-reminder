-- Phase 4: TVMaze metadata caching
-- Extends shows table with rich metadata, adds episode cache and cron audit log.

-- 1. Extend shows table with TVMaze metadata
ALTER TABLE shows
  ADD COLUMN IF NOT EXISTS tvmaze_id       INTEGER,
  ADD COLUMN IF NOT EXISTS status          TEXT,
  ADD COLUMN IF NOT EXISTS network         TEXT,
  ADD COLUMN IF NOT EXISTS image_url       TEXT,
  ADD COLUMN IF NOT EXISTS description     TEXT,
  ADD COLUMN IF NOT EXISTS rating          NUMERIC(3,1),
  ADD COLUMN IF NOT EXISTS genres          TEXT[],
  ADD COLUMN IF NOT EXISTS premiered       TEXT,
  ADD COLUMN IF NOT EXISTS language        TEXT,
  ADD COLUMN IF NOT EXISTS imdb_id         TEXT,
  ADD COLUMN IF NOT EXISTS meta_fetched_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_shows_tvmaze_id ON shows(tvmaze_id);

-- 2. Episode schedule cache
--    One row per episode per region. Refreshed daily by the cron job.
--    Rows older than today are deleted each cron run (no point keeping past episodes).
CREATE TABLE IF NOT EXISTS episode_cache (
    id              SERIAL PRIMARY KEY,
    region          TEXT NOT NULL,
    airdate         DATE NOT NULL,
    show_name       TEXT NOT NULL,
    tvmaze_show_id  INTEGER,
    season          INTEGER,
    episode_number  INTEGER,
    airtime         TEXT,
    network         TEXT,
    episode_url     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_cache_unique
    ON episode_cache(region, airdate, show_name, COALESCE(season, -1), COALESCE(episode_number, -1));

CREATE INDEX IF NOT EXISTS idx_episode_cache_lookup
    ON episode_cache(region, airdate);

-- 3. Cron audit log — one row per region per day
--    UNIQUE(region, fetch_date) prevents the cron from double-fetching if Vercel
--    retries or runs the job twice in one day.
CREATE TABLE IF NOT EXISTS schedule_fetches (
    id            SERIAL PRIMARY KEY,
    region        TEXT NOT NULL,
    fetch_date    DATE NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    episode_count INTEGER NOT NULL DEFAULT 0,
    success       BOOLEAN NOT NULL DEFAULT TRUE,
    error_msg     TEXT,
    UNIQUE (region, fetch_date)
);
