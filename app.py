#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from datetime import datetime, date, timedelta
from difflib import get_close_matches

import requests
from flask import Flask, jsonify, request, render_template
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

app = Flask(__name__)
CORS(app)


def validate_show_name(show_name: str) -> dict:
    """
    Validate a show name by checking if it exists on TVMaze.
    Returns: {"valid": bool, "name": str, "suggestions": list}
    """
    try:
        show_id = tvmaze_search_show_id(show_name)

        if show_id:
            r = requests.get(f"https://api.tvmaze.com/shows/{show_id}", timeout=20)
            r.raise_for_status()
            correct_name = r.json().get("name", show_name)
            return {
                "valid": True,
                "name": correct_name,
                "suggestions": [],
                "message": f"Found on TVMaze as '{correct_name}'"
            }
        else:
            popular_shows = [
                "The Mandalorian", "Breaking Bad", "Game of Thrones",
                "The Office", "Friends", "Stranger Things", "The Crown",
                "Chernobyl", "The Witcher", "Sherlock", "The Last of Us",
                "House of the Dragon", "The Bear", "Wednesday", "Andor",
                "The Boys", "Succession", "Ted Lasso",
                "The Marvelous Mrs Maisel", "Ozark",
            ]
            close_matches = get_close_matches(show_name, popular_shows, n=2, cutoff=0.5)

            if close_matches:
                for suggested_name in close_matches:
                    suggested_id = tvmaze_search_show_id(suggested_name)
                    if suggested_id:
                        r = requests.get(f"https://api.tvmaze.com/shows/{suggested_id}", timeout=20)
                        r.raise_for_status()
                        correct_name = r.json().get("name", suggested_name)
                        return {
                            "valid": False,
                            "name": show_name,
                            "suggestions": [correct_name],
                            "message": f"Did you mean '{correct_name}'?",
                            "correctable": True
                        }

            current_shows = db.get_show_names()
            current_close = get_close_matches(show_name, current_shows, n=2, cutoff=0.6)
            if current_close:
                return {
                    "valid": False,
                    "name": show_name,
                    "suggestions": current_close,
                    "message": f"Not found on TVMaze. Did you mean: {', '.join(current_close)}?"
                }

            return {
                "valid": False,
                "name": show_name,
                "suggestions": [],
                "message": f"'{show_name}' not found. Please check the spelling."
            }
    except Exception as e:
        return {
            "valid": False,
            "name": show_name,
            "suggestions": [],
            "message": f"Could not validate against TVMaze: {str(e)}"
        }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/shows", methods=["GET"])
def get_shows():
    try:
        cfg = db.get_config()
        show_names = db.get_show_names()
        return jsonify({
            "shows": show_names,
            "region": cfg["region"],
            "days_ahead": cfg["days_ahead"]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows", methods=["POST"])
def add_show():
    try:
        data = request.json
        show_name = data.get("name", "").strip()
        force_add = data.get("force", False)

        if not show_name:
            return jsonify({"error": "Show name is required"}), 400

        show_names = db.get_show_names()
        if show_name in show_names:
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
        db.add_show(final_name)

        response = {
            "success": True,
            "show": final_name,
            "message": validation["message"]
        }
        if final_name != show_name:
            response["corrected"] = True
            response["original_name"] = show_name

        return jsonify(response)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows/<show_name>", methods=["DELETE"])
def remove_show(show_name):
    try:
        found = db.remove_show(show_name)
        if not found:
            return jsonify({"error": f"Show not found: '{show_name}'"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows/remove", methods=["POST"])
def remove_show_post():
    """POST-based delete — more reliable on serverless platforms than DELETE method."""
    try:
        show_name = request.json.get("name", "").strip()
        if not show_name:
            return jsonify({"error": "Show name is required"}), 400
        found = db.remove_show(show_name)
        if not found:
            return jsonify({"error": f"Show not found: '{show_name}'"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows/<show_name>/details", methods=["GET"])
def get_show_details(show_name):
    try:
        show_id = tvmaze_search_show_id(show_name)
        if not show_id:
            return jsonify({"error": "Show not found on TVMaze"}), 404

        r = requests.get(f"https://api.tvmaze.com/shows/{show_id}", timeout=20)
        r.raise_for_status()
        show_data = r.json()

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
def get_upcoming():
    try:
        cfg = db.get_config()
        region = cfg["region"].upper()
        days_ahead = cfg["days_ahead"]
        tracked_names = db.get_show_names()

        if not tracked_names:
            return jsonify({"upcoming": [], "count": 0})

        start = date.today()
        end = start + timedelta(days=days_ahead)

        schedule_eps = tvmaze_schedule_for_region(region, start, end)
        reminders = keep_next_episode_per_show(pick_matching_episodes(tracked_names, schedule_eps))

        upcoming = []
        for r in reminders:
            upcoming.append({
                "show": r.show_name,
                "season": r.season,
                "number": r.number,
                "airdate": r.airdate.isoformat(),
                "airtime": r.airtime,
                "network": r.network_or_platform,
                "url": r.url,
                "key": reminder_key(r)
            })

        return jsonify({"upcoming": upcoming, "count": len(upcoming)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/check", methods=["POST"])
def trigger_check():
    try:
        cfg = db.get_config()
        region = cfg["region"].upper()
        days_ahead = cfg["days_ahead"]
        tracked_names = db.get_show_names()

        if not tracked_names:
            return jsonify({"success": False, "message": "No shows tracked"}), 400

        start = date.today()
        end = start + timedelta(days=days_ahead)

        schedule_eps = tvmaze_schedule_for_region(region, start, end)
        reminders = keep_next_episode_per_show(pick_matching_episodes(tracked_names, schedule_eps))

        sent_keys = db.get_sent_keys()
        new_reminders = [r for r in reminders if reminder_key(r) not in sent_keys]

        if not new_reminders:
            return jsonify({"success": True, "message": "No new reminders to send", "count": 0})

        subject, body = format_subject_body(new_reminders, days_ahead)
        send_email(subject, body)

        for r in new_reminders:
            db.mark_sent(reminder_key(r))

        return jsonify({
            "success": True,
            "message": f"Sent {len(new_reminders)} reminder(s)",
            "count": len(new_reminders)
        })
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500


@app.route("/api/config", methods=["GET"])
def get_config():
    try:
        cfg = db.get_config()
        return jsonify(cfg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
def update_config():
    try:
        data = request.json
        cfg = db.get_config()

        region = data.get("region", cfg["region"]).upper()
        days_ahead = int(data.get("days_ahead", cfg["days_ahead"]))

        db.update_config(region, days_ahead)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5003, debug=False)
