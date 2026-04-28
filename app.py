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


def _fetch_justwatch_uk_platform(show_name: str) -> str | None:
    """Look up UK streaming/broadcast platform via JustWatch GraphQL (no API key needed)."""
    query = """
    query SearchTitles($searchQuery: String!, $country: Country!, $language: Language!) {
      popularTitles(country: $country, first: 1, filter: { searchQuery: $searchQuery, objectTypes: [SHOW] }) {
        edges {
          node {
            ... on Show {
              content(country: $country, language: $language) { title }
              offers(country: $country, platform: WEB) {
                monetizationType
                package { clearName }
              }
            }
          }
        }
      }
    }
    """
    try:
        r = requests.post(
            "https://apis.justwatch.com/graphql",
            json={
                "query": query,
                "variables": {"searchQuery": show_name, "country": "GB", "language": "en"},
            },
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=7,
        )
        if not r.ok:
            return None
        edges = r.json().get("data", {}).get("popularTitles", {}).get("edges", [])
        if not edges:
            return None
        offers = edges[0]["node"].get("offers") or []
        for mtype in ["FLATRATE", "FREE", "ADS"]:
            for offer in offers:
                if offer.get("monetizationType") == mtype:
                    return offer["package"]["clearName"]
        return offers[0]["package"]["clearName"] if offers else None
    except Exception:
        return None


def _normalise_show_episodes(
    show_name: str, tvmaze_show_id: int, network: str | None, raw_eps: list
) -> list[dict]:
    """Convert TVMaze /shows/{id}/episodes response to episode_cache insert dicts."""
    out = []
    for ep in raw_eps:
        airdate_str = ep.get("airdate")
        if not airdate_str:
            continue
        out.append({
            "airdate":        airdate_str,
            "show_name":      show_name,
            "tvmaze_show_id": tvmaze_show_id,
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


@app.route("/api/admin/refresh-cache", methods=["POST"])
@require_admin
def admin_refresh_cache():
    """Manually trigger the episode cache refresh — same logic as the cron."""
    today = date.today()
    results = []
    errors = []

    db.ensure_uk_platform_column()
    purged = db.purge_old_episodes()
    results.append({"action": "purge_old_episodes", "rows_deleted": purged})

    shows = db.get_all_tracked_shows()
    end = today + timedelta(days=30)
    total_count = 0
    for show in shows:
        try:
            r = requests.get(
                f"https://api.tvmaze.com/shows/{show['tvmaze_id']}/episodes",
                timeout=7,
            )
            if r.status_code != 200:
                errors.append({"show": show["name"], "error": f"HTTP {r.status_code}"})
                continue
            eps = r.json()
            upcoming = [
                ep for ep in eps
                if ep.get("airdate") and today <= date.fromisoformat(ep["airdate"]) <= end
            ]
            display_network = show.get("uk_platform") or show.get("network")
            normalised = _normalise_show_episodes(
                show["name"], show["tvmaze_id"], display_network, upcoming
            )
            count = db.upsert_episode_cache("GLOBAL", normalised)
            total_count += count
            results.append({"show": show["name"], "episodes_cached": count})
        except Exception as e:
            errors.append({"show": show["name"], "error": str(e)})

    db.record_schedule_fetch("GLOBAL", today, episode_count=total_count, success=not bool(errors))
    return jsonify({
        "success": not bool(errors),
        "episodes_total": total_count,
        "results": results,
        "errors": errors,
    })


@app.route("/api/admin/refresh-platforms", methods=["POST"])
@require_admin
def admin_refresh_platforms():
    """Look up UK streaming platform for each tracked show via JustWatch."""
    db.ensure_uk_platform_column()
    shows = db.get_all_tracked_shows()
    results = []
    errors = []

    for show in shows:
        try:
            platform = _fetch_justwatch_uk_platform(show["name"])
            if platform:
                db.update_show_uk_platform(show["id"], platform)
                results.append({"show": show["name"], "uk_platform": platform})
            else:
                results.append({"show": show["name"], "uk_platform": None, "note": "not found on JustWatch GB"})
        except Exception as e:
            errors.append({"show": show["name"], "error": str(e)})

    return jsonify({
        "success": not bool(errors),
        "updated": len([r for r in results if r.get("uk_platform")]),
        "results": results,
        "errors": errors,
    })


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


@app.route("/api/shows/find-platform", methods=["POST"])
@require_auth
def find_show_platform():
    """Look up UK streaming/broadcast platform for a show via JustWatch. Stores result in DB."""
    show_name = (request.json or {}).get("name", "").strip()
    if not show_name:
        return jsonify({"error": "name required"}), 400

    db.ensure_uk_platform_column()
    show = db.get_show_by_name(show_name)
    if not show:
        return jsonify({"error": "Show not found"}), 404

    platform = _fetch_justwatch_uk_platform(show_name)
    if platform:
        db.update_show_uk_platform(show["id"], platform)

    return jsonify({"show": show_name, "uk_platform": platform})


@app.route("/api/show-details", methods=["GET"])
@require_auth
def get_show_details():
    """Read show metadata from the DB cache."""
    show_name = request.args.get("name", "").strip()
    if not show_name:
        return jsonify({"error": "name required"}), 400
    try:
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
    # Only enforce auth if CRON_SECRET is configured in env vars
    cron_secret = os.environ.get("CRON_SECRET")
    if cron_secret:
        auth = request.headers.get("Authorization", "")
        secret = auth.replace("Bearer ", "") or request.headers.get("X-Cron-Secret", "")
        if not secret or secret != cron_secret:
            return jsonify({"error": "Forbidden"}), 403

    today = date.today()
    results = []
    errors = []

    # 1. Purge past episodes first (keeps the table small)
    purged = db.purge_old_episodes()
    results.append({"action": "purge_old_episodes", "rows_deleted": purged})

    # 2. Fetch upcoming episodes for every tracked show (by tvmaze_id)
    # This approach works for all channels — broadcast, streaming, US, UK, etc.
    if db.already_fetched_today("GLOBAL", today):
        results.append({"skipped": True, "reason": "already_fetched_today"})
    else:
        shows = db.get_all_tracked_shows()
        end = today + timedelta(days=30)
        total_count = 0
        for show in shows:
            try:
                r = requests.get(
                    f"https://api.tvmaze.com/shows/{show['tvmaze_id']}/episodes",
                    timeout=7,
                )
                if r.status_code != 200:
                    errors.append({"show": show["name"], "error": f"HTTP {r.status_code}"})
                    continue
                eps = r.json()
                upcoming = [
                    ep for ep in eps
                    if ep.get("airdate") and today <= date.fromisoformat(ep["airdate"]) <= end
                ]
                display_network = show.get("uk_platform") or show.get("network")
                normalised = _normalise_show_episodes(
                    show["name"], show["tvmaze_id"], display_network, upcoming
                )
                count = db.upsert_episode_cache("GLOBAL", normalised)
                total_count += count
                results.append({"show": show["name"], "episodes_cached": count})
            except Exception as e:
                errors.append({"show": show["name"], "error": str(e)})

        db.record_schedule_fetch("GLOBAL", today, episode_count=total_count, success=not bool(errors))

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
