import sys
import os

# Add project root to path so app.py and tv_reminder.py are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app  # noqa: F401 — Vercel detects the WSGI `app` object
