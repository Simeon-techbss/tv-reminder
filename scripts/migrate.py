#!/usr/bin/env python3
"""
Run once to set up the Neon database schema and seed it with your shows.

Usage:
    DATABASE_URL="postgresql://..." python scripts/migrate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.db import get_conn

MIGRATION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations", "001_initial.sql"
)

def main():
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL environment variable is not set.")
        sys.exit(1)

    print("Connecting to database...")
    sql = open(MIGRATION_FILE, "r").read()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        print("Migration complete. Tables created and shows seeded.")

if __name__ == "__main__":
    main()
