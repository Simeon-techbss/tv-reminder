#!/usr/bin/env python3
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from difflib import get_close_matches

import requests
from flask import Flask, jsonify, request, render_template, g, make_response
from flask_cors import CORS

from tv_reminder import (
    tvmaze_schedule_for_region,
    pick_matching_episodes,
    keep_next_episode_per_show,
    reminder_key,
    format_subject_body,
    send_email,
    tvmaze_search_show_id,
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


def validate_show_name(show_name: str) -> dict:
    try:
        show_id = tvmaze_search_show_id(show_name)
        if show_id:
            r = requests.get(f"https://api.tvmaze.com/shows/{show_id}", timeout=20)
            r.raise_for_status()
            correct_name = r.json().get("name", show_name)
            return {"valid": True, "name": correct_name, "suggestions": [],
                    "message": f"Found on TVMaze as '{correct_name}'"}

        popular_shows = [
            "The Mandalorian", "Breaking Bad", "Game of Thrones", "The Office",
            "Friends", "Stranger Things", "The Crown", "Chernobyl", "The Witcher",
            "Sherlock", "The Last of Us", "House of the Dragon", "The Bear",
            "Wednesday", "Andor", "The Boys", "Succession", "Ted Lasso",
            "The Marvelous Mrs Maisel", "Ozark",
        ]
        close_matches = get_close_matches(show_name, popular_shows, n=2, cutoff=0.5)
        if close_matches:
            for suggested in close_matches:
                sid = tvmaze_search_show_id(suggested)
                if sid:
                    r = requests.get(f"https://api.tvmaze.com/shows/{sid}", timeout=20)
                    r.raise_for_status()
                    correct_name = r.json().get("name", suggested)
                    return {"valid": False, "name": show_name, "suggestions": [correct_name],
                            "message": f"Did you mean '{correct_name}'?", "correctable": True}

        user_id = g.current_user["sub"]
        current_shows = db.get_show_names(user_id)
        current_close = get_close_matches(show_name, current_shows, n=2, cutoff=0.6)
        if current_close:
            return {"valid": False, "name": show_name, "suggestions": current_close,
                    "message": f"Not found on TVMaze. Did you mean: {', '.join(current_close)}?"}

        return {"valid": False, "name": show_name, "suggestions": [],
                "message": f"'{show_name}' not found. Please check the spelling."}
    except Exception as e:
        return {"valid": False, "name": show_name, "suggestions": [],
                "message": f"Could not validate against TVMaze: {str(e)}"}


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
        "display_name": user["display_name"], "is_admin": user["is_admin"]
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
        "id": u["sub"], "email": u["email"], "is_admin": u.get("is_admin", False)
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
# Show routes (all protected)
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
                "needs_confirmation": bool(validation["suggestions"])
            }), 400

        final_name = validation["name"]
        db.add_show(user_id, final_name)
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
    try:
        # Use the search endpoint directly — it returns full show data in one call
        r = requests.get("https://api.tvmaze.com/search/shows", params={"q": show_name}, timeout=15)
        r.raise_for_status()
        results = r.json()
        if not results:
            return jsonify({"error": "Show not found on TVMaze"}), 404
        show_data = results[0]["show"]
        details = {
            "name": show_data.get("name", show_name),
            "status": show_data.get("status", "Unknown"),
            "year": show_data.get("premiered", "").split("-")[0] if show_data.get("premiered") else "Unknown",
            "genres": show_data.get("genres", []),
            "network": None,
            "language": show_data.get("language", "Unknown"),
            "description": show_data.get("summary", "").replace("<p>", "").replace("</p>", ""),
            "rating": show_data.get("rating", {}).get("average", "N/A"),
            "tvmaze_url": show_data.get("officialSite", ""),
            "image": show_data.get("image", {}).get("medium", ""),
            "imdb_id": show_data.get("externals", {}).get("imdb", ""),
        }
        network = show_data.get("network")
        web_channel = show_data.get("webChannel")
        if network:
            details["network"] = network.get("name")
        elif web_channel:
            details["network"] = web_channel.get("name")
        if details["imdb_id"]:
            details["imdb_url"] = f"https://www.imdb.com/title/{details['imdb_id']}/"
        return jsonify(details)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        schedule_eps = tvmaze_schedule_for_region(region, start, end)
        reminders = keep_next_episode_per_show(pick_matching_episodes(tracked_names, schedule_eps))
        upcoming = [{
            "show": r.show_name, "season": r.season, "number": r.number,
            "airdate": r.airdate.isoformat(), "airtime": r.airtime,
            "network": r.network_or_platform, "url": r.url, "key": reminder_key(r)
        } for r in reminders]
        return jsonify({"upcoming": upcoming, "count": len(upcoming)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/check", methods=["POST"])
@require_auth
def trigger_check():
    try:
        user_id = g.current_user["sub"]
        cfg = db.get_config(user_id)
        region = cfg["region"].upper()
        days_ahead = cfg["days_ahead"]
        tracked_names = db.get_show_names(user_id)
        if not tracked_names:
            return jsonify({"success": False, "message": "No shows tracked"}), 400
        start = date.today()
        end = start + timedelta(days=days_ahead)
        schedule_eps = tvmaze_schedule_for_region(region, start, end)
        reminders = keep_next_episode_per_show(pick_matching_episodes(tracked_names, schedule_eps))
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5003, debug=False)
