#!/usr/bin/env python3
from __future__ import annotations

import os
from datetime import date, timedelta
from difflib import get_close_matches

import requests
from flask import Flask, jsonify, request, render_template, g, make_response
from flask_cors import CORS

from tv_reminder import (
    EpisodeReminder,
    tvmaze_schedule_for_region,
    reminder_key,
    format_subject_body,
    send_email,
)
from lib import db
from lib.auth import (
    hash_password,
    verify_password,
    create_token,
    require_auth,
    require_admin,
)

app = Flask(__name__)
CORS(app)

_IS_PROD = bool(os.environ.get("VERCEL"))


def _set_auth_cookie(response, token: str):
    response.set_cookie(
        "tv_token",
        token,
        httponly=True,
        secure=_IS_PROD,
        samesite="Lax",
        max_age=30 * 24 * 3600,
    )


def _build_meta_dict(show_data: dict) -> dict:
    """Convert a TVMaze show API response to our DB column shape."""
    network = None
    if show_data.get("network"):
        network = show_data["network"].get("name")
    elif show_data.get("webChannel"):
        network = show_data["webChannel"].get("name")
    summary = show_data.get("summary") or ""
    # Strip basic HTML tags TVMaze includes in summaries
    for tag in ["<p>", "</p>", "<b>", "</b>", "<i>", "</i>"]:
        summary = summary.replace(tag, "")
    return {
        "tvmaze_id":   show_data.get("id"),
        "status":      show_data.get("status"),
        "network":     network,
        "image_url":   (show_data.get("image") or {}).get("medium"),
        "description": summary,
        "rating":      (show_data.get("rating") or {}).get("average"),
        "genres":      show_data.get("genres") or [],
        "premiered":   show_data.get("premiered"),
        "language":    show_data.get("language"),
        "imdb_id":     (show_data.get("externals") or {}).get("imdb"),
    }


def _fetch_tvmaze_show(show_name: str) -> dict | None:
    """
    Search TVMaze for a show by name. Returns the raw TVMaze show dict or None.
    Uses singlesearch which returns the best match directly (one API call).
    """
    try:
        r = requests.get(
            "https://api.tvmaze.com/singlesearch/shows",
            params={"q": show_name},
            timeout=7,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def validate_show_name(show_name: str) -> dict:
    """
    Validates a show name against TVMaze. Returns a dict with:
      valid, name, tvmaze_data, suggestions, message
    """
    try:
        show_data = _fetch_tvmaze_show(show_name)
        if show_data:
            return {
                "valid": True,
                "name": show_data["name"],
                "tvmaze_data": show_data,
                "suggestions": [],
                "message": f"Found on TVMaze as '{show_data['name']}'",
            }

        # Try fuzzy matching against the user's existing shows
        user_id = g.current_user["sub"]
        current_shows = db.get_show_names(user_id)
        current_close = get_close_matches(show_name, current_shows, n=2, cutoff=0.6)
        if current_close:
            return {
                "valid": False, "name": show_name, "tvmaze_data": None,
                "suggestions": current_close,
                "message": f"Not found on TVMaze. Did you mean: {', '.join(current_close)}?",
            }

        return {
            "valid": False, "name": show_name, "tvmaze_data": None,
            "suggestions": [],
            "message": f"'{show_name}' not found on TVMaze. Please check the spelling.",
        }
    except Exception as e:
        return {
            "valid": False, "name": show_name, "tvmaze_data": None,
            "suggestions": [],
            "message": f"Could not validate: {str(e)}",
        }


def _normalise_episodes_for_cache(region: str, raw_eps: list) -> list[dict]:
    """Convert raw TVMaze schedule API rows to episode_cache insert dicts."""
    out = []
    for ep in raw_eps:
        show = ep.get("show") or {}
        show_name = show.get("name")
        airdate_str = ep.get("airdate")
        if not show_name or not airdate_str:
            continue
        network = None
        if show.get("network"):
            network = show["network"].get("name")
        elif show.get("webChannel"):
            network = show["webChannel"].get("name")
        out.append({
            "airdate":        airdate_str,
            "show_name":      show_name,
            "tvmaze_show_id": show.get("id"),
            "season":         ep.get("season"),
            "episode_number": ep.get("number"),
            "airtime":        ep.get("airtime"),
            "network":        network,
            "episode_url":    ep.get("url"),
        })
    return out


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    display_name = (data.get("display_name") or "").strip() or None

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if db.get_user_by_email(email):
        return jsonify({"error": "An account with this email already exists"}), 400

    user = db.register_user(email, hash_password(password), display_name)
    token = create_token(user["id"], user["email"], user["is_admin"])
    resp = make_response(jsonify({"user": user}))
    _set_auth_cookie(resp, token)
    return resp


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = db.get_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        return jsonify({"error": "Invalid email or password"}), 401

    db.update_last_login(user["id"])
    token = create_token(user["id"], user["email"], user["is_admin"])
    resp = make_response(jsonify({"user": {
        "id": user["id"], "email": user["email"],
        "display_name": user["display_name"], "is_admin": user["is_admin"],
    }}))
    _set_auth_cookie(resp, token)
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"success": True}))
    resp.delete_cookie("tv_token")
    return resp


@app.route("/api/auth/me", methods=["GET"])
@require_auth
def me():
    u = g.current_user
    return jsonify({"user": {
        "id": u["sub"], "email": u["email"], "is_admin": u.get("is_admin", False),
    }})


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.route("/api/admin/stats", methods=["GET"])
@require_admin
def admin_stats():
    try:
        return jsonify(db.get_admin_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Show routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/shows", methods=["GET"])
@require_auth
def get_shows():
    try:
        user_id = g.current_user["sub"]
        cfg = db.get_config(user_id)
        show_names = db.get_show_names(user_id)
        return jsonify({"shows": show_names, "region": cfg["region"], "days_ahead": cfg["days_ahead"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows", methods=["POST"])
@require_auth
def add_show():
    try:
        data = request.json or {}
        show_name = data.get("name", "").strip()
        force_add = data.get("force", False)
        if not show_name:
            return jsonify({"error": "Show name is required"}), 400

        user_id = g.current_user["sub"]
        if show_name in db.get_show_names(user_id):
            return jsonify({"error": "Show already tracked"}), 400

        validation = validate_show_name(show_name)
        if not validation["valid"] and not force_add:
            return jsonify({
                "error": validation["message"],
                "suggestions": validation["suggestions"],
                "attempted_name": show_name,
                "needs_confirmation": bool(validation["suggestions"]),
            }), 400

        final_name = validation["name"]

        # Store TVMaze metadata — no-op if another user already added this show
        tvmaze_data = validation.get("tvmaze_data") or {}
        meta = _build_meta_dict(tvmaze_data) if tvmaze_data else {}
        show_id = db.upsert_show_metadata(final_name, meta)

        db.add_show_for_user(user_id, show_id)

        resp = {"success": True, "show": final_name, "message": validation["message"]}
        if final_name != show_name:
            resp["corrected"] = True
            resp["original_name"] = show_name
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows/<show_name>", methods=["DELETE"])
@require_auth
def remove_show(show_name):
    try:
        found = db.remove_show(g.current_user["sub"], show_name)
        if not found:
            return jsonify({"error": f"Show not found: '{show_name}'"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows/remove", methods=["POST"])
@require_auth
def remove_show_post():
    try:
        show_name = (request.json or {}).get("name", "").strip()
        if not show_name:
            return jsonify({"error": "Show name is required"}), 400
        found = db.remove_show(g.current_user["sub"], show_name)
        if not found:
            return jsonify({"error": f"Show not found: '{show_name}'"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows/<show_name>/details", methods=["GET"])
@require_auth
def get_show_details(show_name):
    """
    Read show metadata from the DB cache (Phase 4 — no live TVMaze call).
    Falls back to a live TVMaze call only for shows seeded before Phase 4
    that have no metadata yet (meta_fetched_at IS NULL).
    """
    try:
        show = db.get_show_by_name(show_name)

        # Lazy backfill for pre-Phase4 seeded shows with no metadata
        if show and show.get("meta_fetched_at") is None:
            show_data = _fetch_tvmaze_show(show_name)
            if show_data:
                db.upsert_show_metadata(show_name, _build_meta_dict(show_data))
                show = db.get_show_by_name(show_name)

        if not show:
            return jsonify({"error": "Show not found"}), 404

        details = {
            "name":        show["name"],
            "status":      show.get("status") or "Unknown",
            "year":        (show.get("premiered") or "").split("-")[0] or "Unknown",
            "genres":      show.get("genres") or [],
            "network":     show.get("network"),
            "language":    show.get("language") or "Unknown",
            "description": show.get("description") or "",
            "rating":      show.get("rating") or "N/A",
            "image":       show.get("image_url") or "",
            "imdb_id":     show.get("imdb_id") or "",
        }
        if details["imdb_id"]:
            details["imdb_url"] = f"https://www.imdb.com/title/{details['imdb_id']}/"
        return jsonify(details)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Upcoming episodes — reads from episode_cache (no live TVMaze)
# ---------------------------------------------------------------------------

@app.route("/api/upcoming", methods=["GET"])
@require_auth
def get_upcoming():
    try:
        user_id = g.current_user["sub"]
        cfg = db.get_config(user_id)
        region = cfg["region"].upper()
        days_ahead = cfg["days_ahead"]
        tracked_names = db.get_show_names(user_id)
        if not tracked_names:
            return jsonify({"upcoming": [], "count": 0})

        start = date.today()
        end = start + timedelta(days=days_ahead)

        cached_eps = db.get_upcoming_from_cache(region, tracked_names, start, end)

        # Keep only the next episode per show (cache is sorted by airdate)
        seen: set[str] = set()
        upcoming = []
        for ep in cached_eps:
            sname = ep["show_name"]
            if sname not in seen:
                seen.add(sname)
                s = ep["season"]
                n = ep["episode_number"]
                upcoming.append({
                    "show":    sname,
                    "season":  s,
                    "number":  n,
                    "airdate": ep["airdate"].isoformat(),
                    "airtime": ep["airtime"],
                    "network": ep["network"],
                    "url":     ep["episode_url"],
                    "key":     f"{sname}|S{s}E{n}|{ep['airdate'].isoformat()}",
                })

        return jsonify({"upcoming": upcoming, "count": len(upcoming)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Manual reminder check (sends email for current user)
# ---------------------------------------------------------------------------

@app.route("/api/check", methods=["POST"])
@require_auth
def trigger_check():
    try:
        user_id = g.current_user["sub"]
        cfg = db.get_config(user_id)
        days_ahead = cfg["days_ahead"]
        tracked_names = db.get_show_names(user_id)
        if not tracked_names:
            return jsonify({"success": False, "message": "No shows tracked"}), 400

        start = date.today()
        end = start + timedelta(days=days_ahead)
        region = cfg["region"].upper()

        cached_eps = db.get_upcoming_from_cache(region, tracked_names, start, end)
        seen: set[str] = set()
        reminders = []
        for ep in cached_eps:
            sname = ep["show_name"]
            if sname not in seen:
                seen.add(sname)
                reminders.append(EpisodeReminder(
                    show_name=sname,
                    season=ep["season"],
                    number=ep["episode_number"],
                    airdate=ep["airdate"],
                    airtime=ep["airtime"],
                    network_or_platform=ep["network"],
                    url=ep["episode_url"],
                ))

        sent_keys = db.get_sent_keys(user_id)
        new_reminders = [r for r in reminders if reminder_key(r) not in sent_keys]
        if not new_reminders:
            return jsonify({"success": True, "message": "No new reminders to send", "count": 0})

        subject, body = format_subject_body(new_reminders, days_ahead)
        send_email(subject, body, to=g.current_user["email"])
        for r in new_reminders:
            db.mark_sent(user_id, reminder_key(r))
        return jsonify({"success": True, "message": f"Sent {len(new_reminders)} reminder(s)", "count": len(new_reminders)})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@app.route("/api/config", methods=["GET"])
@require_auth
def get_config():
    try:
        return jsonify(db.get_config(g.current_user["sub"]))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
@require_auth
def update_config():
    try:
        data = request.json or {}
        user_id = g.current_user["sub"]
        cfg = db.get_config(user_id)
        region = data.get("region", cfg["region"]).upper()
        days_ahead = int(data.get("days_ahead", cfg["days_ahead"]))
        db.update_config(user_id, region, days_ahead)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Cron job — called by Vercel Cron at 07:00 UTC daily
# ---------------------------------------------------------------------------

@app.route("/api/cron/daily", methods=["GET", "POST"])
def cron_daily():
    # Vercel sends Authorization: Bearer <CRON_SECRET>
    auth = request.headers.get("Authorization", "")
    secret = auth.replace("Bearer ", "") or request.headers.get("X-Cron-Secret", "")
    if not secret or secret != os.environ.get("CRON_SECRET"):
        return jsonify({"error": "Forbidden"}), 403

    today = date.today()
    results = []
    errors = []

    # 1. Purge past episodes first (keeps the table small)
    purged = db.purge_old_episodes()
    results.append({"action": "purge_old_episodes", "rows_deleted": purged})

    # 2. Fetch schedule for every active region
    regions = db.get_active_regions()
    for region in regions:
        if db.already_fetched_today(region, today):
            results.append({"region": region, "skipped": True, "reason": "already_fetched_today"})
            continue
        try:
            # Fetch 8 days ahead so users with days_ahead=7 always have full coverage
            end = today + timedelta(days=8)
            raw_eps = tvmaze_schedule_for_region(region, today, end)
            normalised = _normalise_episodes_for_cache(region, raw_eps)
            count = db.upsert_episode_cache(region, normalised)
            db.record_schedule_fetch(region, today, episode_count=count, success=True)
            results.append({"region": region, "episodes_cached": count})
        except Exception as e:
            db.record_schedule_fetch(region, today, success=False, error_msg=str(e))
            errors.append({"region": region, "error": str(e)})

    # 3. Fan out reminder emails to all users
    email_results = _cron_send_emails(today)
    results.extend(email_results)

    status = 500 if errors else 200
    return jsonify({"date": today.isoformat(), "results": results, "errors": errors}), status


def _cron_send_emails(today: date) -> list[dict]:
    """Send reminder emails to every active user who has unsent upcoming episodes."""
    out = []
    users = db.get_users_for_email_fanout()
    for user in users:
        tracked = user.get("tracked_shows") or []
        if not tracked:
            continue
        region = user["region"].upper()
        days_ahead = user["days_ahead"]
        end = today + timedelta(days=days_ahead)

        cached_eps = db.get_upcoming_from_cache(region, tracked, today, end)

        seen: set[str] = set()
        reminders = []
        for ep in cached_eps:
            sname = ep["show_name"]
            if sname not in seen:
                seen.add(sname)
                reminders.append(EpisodeReminder(
                    show_name=sname,
                    season=ep["season"],
                    number=ep["episode_number"],
                    airdate=ep["airdate"],
                    airtime=ep["airtime"],
                    network_or_platform=ep["network"],
                    url=ep["episode_url"],
                ))

        sent_keys = db.get_sent_keys(user["id"])
        new_reminders = [r for r in reminders if reminder_key(r) not in sent_keys]
        if not new_reminders:
            continue
        try:
            subject, body = format_subject_body(new_reminders, days_ahead)
            send_email(subject, body, to=user["email"])
            for r in new_reminders:
                db.mark_sent(user["id"], reminder_key(r))
            out.append({"user_id": user["id"], "emails_sent": len(new_reminders)})
        except Exception as e:
            out.append({"user_id": user["id"], "error": str(e)})
    return out


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5003, debug=False)
