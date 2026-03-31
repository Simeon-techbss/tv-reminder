-- Phase 2 initial schema
-- Designed multi-user-ready so Phase 3 (auth) needs no schema changes.

CREATE TABLE IF NOT EXISTS users (
    id                 SERIAL PRIMARY KEY,
    email              TEXT UNIQUE NOT NULL,
    password_hash      TEXT NOT NULL DEFAULT 'placeholder',
    display_name       TEXT,
    region             TEXT NOT NULL DEFAULT 'GB',
    days_ahead         INTEGER NOT NULL DEFAULT 7,
    notification_email TEXT,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Canonical show records (one row per unique show, shared across all users)
CREATE TABLE IF NOT EXISTS shows (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    tvmaze_id  INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Which users track which shows
CREATE TABLE IF NOT EXISTS user_shows (
    id       SERIAL PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    show_id  INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, show_id)
);
CREATE INDEX IF NOT EXISTS idx_user_shows_user ON user_shows(user_id);

-- Sent reminder tracking (replaces state.json)
CREATE TABLE IF NOT EXISTS sent_reminders (
    id        SERIAL PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cache_key TEXT NOT NULL,
    sent_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, cache_key)
);
CREATE INDEX IF NOT EXISTS idx_sent_reminders_user ON sent_reminders(user_id);

-- ----------------------------------------------------------------
-- Seed data (migrated from shows.yaml)
-- ----------------------------------------------------------------

INSERT INTO users (email, display_name, region, days_ahead)
VALUES ('admin@example.com', 'Admin', 'GB', 7)
ON CONFLICT (email) DO NOTHING;

INSERT INTO shows (name) VALUES
    ('Ted Lasso'),
    ('Department Q'),
    ('Silent Witness'),
    ('Star Trek: Strange New Worlds'),
    ('Doctor Who'),
    ('The Mandalorian'),
    ('Ludwig'),
    ('Strike'),
    ('Gone'),
    ('Professor T'),
    ('Pluribus'),
    ('The Day of the Jackal'),
    ('AI Confidential with Hannah Fry'),
    ('Bridgerton'),
    ('Good Omens'),
    ('Star Trek: Starfleet Academy'),
    ('Grace'),
    ('Reacher'),
    ('Scrubs'),
    ('LOL: Last One Laughing UK'),
    ('The Rookie'),
    ('The Good Doctor')
ON CONFLICT (name) DO NOTHING;

-- Link all shows to the admin user
INSERT INTO user_shows (user_id, show_id)
SELECT 1, id FROM shows
ON CONFLICT DO NOTHING;
