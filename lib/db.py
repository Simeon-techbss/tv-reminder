"""
Database helper functions.

Phase 2: single admin user (ADMIN_USER_ID = 1).
Phase 3: replace ADMIN_USER_ID with the authenticated user's ID from JWT.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import psycopg2
from psycopg2.extras import RealDictCursor

# Phase 2 only — removed in Phase 3 when real auth is added
ADMIN_USER_ID = 1


def _clean_db_url(url: str) -> str:
    """
    Strip parameters psycopg2 doesn't understand.
    Neon connection strings include 'channel_binding' which is a Postgres
    protocol option, not a libpq keyword — psycopg2 rejects it.
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
# Config (region + days_ahead live on the user row)
# ---------------------------------------------------------------------------

def get_config() -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT region, days_ahead FROM users WHERE id = %s",
                (ADMIN_USER_ID,),
            )
            row = cur.fetchone()
            return {"region": row["region"], "days_ahead": row["days_ahead"]}


def update_config(region: str, days_ahead: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET region = %s, days_ahead = %s WHERE id = %s",
                (region, days_ahead, ADMIN_USER_ID),
            )


# ---------------------------------------------------------------------------
# Shows
# ---------------------------------------------------------------------------

def get_show_names() -> list[str]:
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
                (ADMIN_USER_ID,),
            )
            return [row[0] for row in cur.fetchall()]


def add_show(name: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Insert show if it doesn't exist, then get its id
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
            # Link to user
            cur.execute(
                "INSERT INTO user_shows (user_id, show_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (ADMIN_USER_ID, show_id),
            )


def remove_show(name: str) -> bool:
    """Returns True if a row was deleted, False if the show wasn't tracked."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM user_shows
                WHERE user_id = %s
                  AND show_id = (SELECT id FROM shows WHERE name = %s)
                """,
                (ADMIN_USER_ID, name),
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Sent reminders (replaces state.json)
# ---------------------------------------------------------------------------

def get_sent_keys() -> set[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cache_key FROM sent_reminders WHERE user_id = %s",
                (ADMIN_USER_ID,),
            )
            return {row[0] for row in cur.fetchall()}


def mark_sent(cache_key: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sent_reminders (user_id, cache_key)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (ADMIN_USER_ID, cache_key),
            )
