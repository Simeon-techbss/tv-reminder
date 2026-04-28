"""
Database helper functions.

Phase 4 additions:
- upsert_show_metadata  : stores TVMaze metadata when a show is first added
- get_show_by_name      : reads cached metadata for the details modal
- add_show_for_user     : links a show to a user (split out from add_show)
- upsert_episode_cache  : bulk upsert of schedule rows from the cron job
- get_upcoming_from_cache : replaces live TVMaze calls in /api/upcoming
- already_fetched_today : idempotency check for the cron job
- record_schedule_fetch : audit log entry after each cron run
- get_active_regions    : which regions need fetching
- get_users_for_email_fanout : all users + tracked shows in one query
"""
from __future__ import annotations

import os
from datetime import date
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import psycopg2
from psycopg2.extras import RealDictCursor


def _clean_db_url(url: str) -> str:
    """Strip parameters psycopg2 doesn't understand (e.g. Neon's channel_binding)."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params.pop("channel_binding", None)
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))


def get_conn():
    """Open a new database connection. One per request is fine on serverless."""
    return psycopg2.connect(_clean_db_url(os.environ["DATABASE_URL"]))


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def register_user(email: str, password_hash: str, display_name: str | None = None) -> dict:
    """First user in the table gets is_admin=TRUE automatically."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM users")
            is_admin = cur.fetchone()["cnt"] == 0
            cur.execute(
                """
                INSERT INTO users (email, password_hash, display_name, is_admin)
                VALUES (%s, %s, %s, %s)
                RETURNING id, email, display_name, is_admin
                """,
                (email, password_hash, display_name, is_admin),
            )
            return dict(cur.fetchone())


def get_user_by_email(email: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, email, display_name, password_hash, is_admin
                FROM users
                WHERE email = %s AND is_active = TRUE
                """,
                (email,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def update_last_login(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET last_login_at = NOW() WHERE id = %s",
                (user_id,),
            )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_config(user_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT region, days_ahead FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            return {"region": row["region"], "days_ahead": row["days_ahead"]}


def update_config(user_id: int, region: str, days_ahead: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET region = %s, days_ahead = %s WHERE id = %s",
                (region, days_ahead, user_id),
            )


# ---------------------------------------------------------------------------
# Shows — metadata cache
# ---------------------------------------------------------------------------

def upsert_show_metadata(name: str, meta: dict) -> int:
    """
    Insert or update a show's TVMaze metadata. Returns the show's DB id.

    The DO UPDATE only fires when meta_fetched_at IS NULL (i.e. the row was
    seeded before Phase 4 and has no metadata yet). If a show already has
    metadata, this is a no-op — we don't overwrite fresh data on every add.
    Weekly re-fetches are handled by setting meta_fetched_at = NULL via the
    cron job when the data is stale (> 7 days old).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO shows (
                    name, tvmaze_id, status, network, image_url,
                    description, rating, genres, premiered, language,
                    imdb_id, meta_fetched_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                ON CONFLICT (name) DO UPDATE
                  SET tvmaze_id       = EXCLUDED.tvmaze_id,
                      status          = EXCLUDED.status,
                      network         = EXCLUDED.network,
                      image_url       = EXCLUDED.image_url,
                      description     = EXCLUDED.description,
                      rating          = EXCLUDED.rating,
                      genres          = EXCLUDED.genres,
                      premiered       = EXCLUDED.premiered,
                      language        = EXCLUDED.language,
                      imdb_id         = EXCLUDED.imdb_id,
                      meta_fetched_at = NOW()
                  WHERE shows.meta_fetched_at IS NULL
                     OR shows.meta_fetched_at < NOW() - INTERVAL '7 days'
                RETURNING id
                """,
                (
                    name,
                    meta.get("tvmaze_id"),
                    meta.get("status"),
                    meta.get("network"),
                    meta.get("image_url"),
                    meta.get("description"),
                    meta.get("rating"),
                    meta.get("genres") or [],
                    meta.get("premiered"),
                    meta.get("language"),
                    meta.get("imdb_id"),
                ),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            # Row existed and was not updated (fresh data) — just get the id
            cur.execute("SELECT id FROM shows WHERE name = %s", (name,))
            return cur.fetchone()[0]


def get_show_by_name(name: str) -> dict | None:
    """Read cached show metadata for the details modal."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, tvmaze_id, status, network, image_url,
                       description, rating, genres, premiered, language,
                       imdb_id, meta_fetched_at
                FROM shows WHERE name = %s
                """,
                (name,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def add_show_for_user(user_id: int, show_id: int) -> None:
    """Link a show to a user. No-op if already linked."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_shows (user_id, show_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, show_id),
            )


def get_show_names(user_id: int) -> list[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.name
                FROM shows s
                JOIN user_shows us ON us.show_id = s.id
                WHERE us.user_id = %s
                ORDER BY s.name
                """,
                (user_id,),
            )
            return [row[0] for row in cur.fetchall()]


def ensure_uk_platform_column() -> None:
    """Add uk_platform to shows table if not present. Safe to call repeatedly."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE shows ADD COLUMN IF NOT EXISTS uk_platform TEXT"
            )


def get_all_tracked_shows() -> list[dict]:
    """All distinct shows tracked by any active user, with TVMaze metadata."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT s.id, s.name, s.tvmaze_id, s.network,
                       s.imdb_id, s.uk_platform
                FROM shows s
                JOIN user_shows us ON us.show_id = s.id
                JOIN users u ON u.id = us.user_id
                WHERE u.is_active = TRUE AND s.tvmaze_id IS NOT NULL
                """
            )
            return [dict(r) for r in cur.fetchall()]


def update_show_uk_platform(show_id: int, uk_platform: str) -> None:
    """Store the UK streaming/broadcast platform name for a show."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE shows SET uk_platform = %s WHERE id = %s",
                (uk_platform, show_id),
            )


def remove_show(user_id: int, name: str) -> bool:
    """Returns True if a row was deleted."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM user_shows
                WHERE user_id = %s
                  AND show_id = (SELECT id FROM shows WHERE name = %s)
                """,
                (user_id, name),
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Episode cache
# ---------------------------------------------------------------------------

def upsert_episode_cache(region: str, episodes: list[dict]) -> int:
    """
    Bulk upsert schedule rows from the cron job.
    Returns the number of rows inserted or updated.
    """
    if not episodes:
        return 0
    count = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for ep in episodes:
                cur.execute(
                    """
                    INSERT INTO episode_cache (
                        region, airdate, show_name, tvmaze_show_id,
                        season, episode_number, airtime, network, episode_url
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (region, airdate, show_name,
                                 COALESCE(season,-1), COALESCE(episode_number,-1))
                    DO UPDATE SET
                        airtime     = EXCLUDED.airtime,
                        network     = EXCLUDED.network,
                        episode_url = EXCLUDED.episode_url,
                        created_at  = NOW()
                    """,
                    (
                        region,
                        ep["airdate"],
                        ep["show_name"],
                        ep.get("tvmaze_show_id"),
                        ep.get("season"),
                        ep.get("episode_number"),
                        ep.get("airtime"),
                        ep.get("network"),
                        ep.get("episode_url"),
                    ),
                )
                count += cur.rowcount
    return count


def get_upcoming_from_cache(
    region: str,
    tracked_names: list[str],
    start: date,
    end: date,
) -> list[dict]:
    """
    Return cached episodes for the user's tracked shows in the given date window.
    Results are sorted by airdate then show name so the caller can trivially
    take the first occurrence of each show.
    """
    if not tracked_names:
        return []
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT show_name, season, episode_number, airdate,
                       airtime, network, episode_url
                FROM episode_cache
                WHERE (region = %s OR region = 'GLOBAL')
                  AND airdate BETWEEN %s AND %s
                  AND show_name = ANY(%s)
                ORDER BY airdate, show_name, season NULLS LAST, episode_number NULLS LAST
                """,
                (region, start, end, tracked_names),
            )
            return [dict(r) for r in cur.fetchall()]


def get_next_episode(show_name: str) -> dict | None:
    """Return the earliest upcoming episode for a show, or None."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT season, episode_number, airdate::text AS airdate, airtime, network
                FROM episode_cache
                WHERE show_name = %s AND airdate >= CURRENT_DATE
                ORDER BY airdate, season NULLS LAST, episode_number NULLS LAST
                LIMIT 1
                """,
                (show_name,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def purge_old_episodes() -> int:
    """Delete episodes with an airdate before today. Called by the cron job."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM episode_cache WHERE airdate < CURRENT_DATE"
            )
            return cur.rowcount


# ---------------------------------------------------------------------------
# Cron job helpers
# ---------------------------------------------------------------------------

def already_fetched_today(region: str, fetch_date: date) -> bool:
    """Idempotency guard — returns True if the schedule was already fetched today."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM schedule_fetches
                WHERE region = %s AND fetch_date = %s AND success = TRUE
                """,
                (region, fetch_date),
            )
            return cur.fetchone() is not None


def record_schedule_fetch(
    region: str,
    fetch_date: date,
    episode_count: int = 0,
    success: bool = True,
    error_msg: str | None = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schedule_fetches
                    (region, fetch_date, episode_count, success, error_msg)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (region, fetch_date) DO UPDATE
                  SET fetched_at    = NOW(),
                      episode_count = EXCLUDED.episode_count,
                      success       = EXCLUDED.success,
                      error_msg     = EXCLUDED.error_msg
                """,
                (region, fetch_date, episode_count, success, error_msg),
            )


def get_active_regions() -> list[str]:
    """Distinct regions used by active users — tells the cron what to fetch."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT region FROM users WHERE is_active = TRUE"
            )
            return [row[0] for row in cur.fetchall()]


def get_users_for_email_fanout() -> list[dict]:
    """
    All active users with their tracked show names in a single query.
    Used by the cron job to build per-user reminder emails without N+1 queries.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT u.id, u.email, u.region, u.days_ahead,
                       ARRAY_AGG(s.name) FILTER (WHERE s.name IS NOT NULL) AS tracked_shows
                FROM users u
                LEFT JOIN user_shows us ON us.user_id = u.id
                LEFT JOIN shows s ON s.id = us.show_id
                WHERE u.is_active = TRUE
                GROUP BY u.id, u.email, u.region, u.days_ahead
                """
            )
            return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Sent reminders
# ---------------------------------------------------------------------------

def get_sent_keys(user_id: int) -> set[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cache_key FROM sent_reminders WHERE user_id = %s",
                (user_id,),
            )
            return {row[0] for row in cur.fetchall()}


def mark_sent(user_id: int, cache_key: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sent_reminders (user_id, cache_key)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (user_id, cache_key),
            )


# ---------------------------------------------------------------------------
# Admin stats
# ---------------------------------------------------------------------------

def get_admin_stats() -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE is_active = TRUE")
            users = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM shows")
            shows_cached = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM user_shows")
            total_tracked = cur.fetchone()["cnt"]

            cur.execute(
                """
                SELECT s.name,
                       COUNT(us.user_id)     AS user_count,
                       COUNT(us.user_id) - 1 AS api_calls_saved
                FROM shows s
                JOIN user_shows us ON us.show_id = s.id
                GROUP BY s.id, s.name
                HAVING COUNT(us.user_id) >= 2
                ORDER BY user_count DESC, s.name
                """
            )
            shared_shows = [dict(r) for r in cur.fetchall()]

    total_saved = sum(r["api_calls_saved"] for r in shared_shows)
    return {
        "users": users,
        "shows_cached": shows_cached,
        "total_tracked": total_tracked,
        "shared_shows": shared_shows,
        "total_api_calls_saved": total_saved,
    }
