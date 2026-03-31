"""
Database helper functions.

All show/config/reminder functions now accept an explicit user_id parameter
instead of the Phase 2 ADMIN_USER_ID=1 constant. This is what makes the app
genuinely multi-user: each request passes g.current_user["sub"] as user_id.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import psycopg2
from psycopg2.extras import RealDictCursor


def _clean_db_url(url: str) -> str:
    """
    Strip parameters psycopg2 doesn't understand.
    Neon connection strings include 'channel_binding' which psycopg2 rejects.
    """
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
    """
    Insert a new user. The very first user in the table gets is_admin=TRUE
    automatically — no separate admin setup needed.
    """
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
# Config (region + days_ahead live on the user row)
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
# Shows
# ---------------------------------------------------------------------------

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


def add_show(user_id: int, name: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO shows (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id",
                (name,),
            )
            row = cur.fetchone()
            if row:
                show_id = row[0]
            else:
                cur.execute("SELECT id FROM shows WHERE name = %s", (name,))
                show_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_shows (user_id, show_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, show_id),
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
# Sent reminders (replaces state.json)
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
    """
    Returns counts and the shared-shows table that demonstrates caching value:
    shows tracked by 2+ users only need one TVMaze API call, not N calls.
    """
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
