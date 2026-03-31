#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from difflib import get_close_matches

import requests
import yaml
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS

# Import functions from tv_reminder
from tv_reminder import (
    tvmaze_schedule_for_region,
    pick_matching_episodes,
    keep_next_episode_per_show,
    reminder_key,
    format_subject_body,
    send_email,
    load_yaml,
    load_state,
    save_state,
    tvmaze_search_show_id,
)

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
YAML_PATH = os.path.join(BASE_DIR, "shows.yaml")
STATE_PATH = os.path.join(BASE_DIR, "state.json")


def validate_show_name(show_name: str) -> dict:
    """
    Validate a show name by checking if it exists on TVMaze.
    Returns: {"valid": bool, "name": str, "suggestions": list}
    """
    try:
        # Try to find the show on TVMaze
        show_id = tvmaze_search_show_id(show_name)
        
        if show_id:
            # Show found! Get the correct name from TVMaze
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
            # Show not found - try fuzzy matching on popular show names
            # This handles typos like "mandorlien" -> "The Mandalorian"
            popular_shows = [
                "The Mandalorian",
                "Breaking Bad",
                "Game of Thrones",
                "The Office",
                "Friends",
                "Stranger Things",
                "The Crown",
                "Chernobyl",
                "The Witcher",
                "Sherlock",
                "The Last of Us",
                "House of the Dragon",
                "The Bear",
                "Wednesday",
                "Andor",
                "The Boys",
                "Succession",
                "Ted Lasso",
                "The Marvelous Mrs Maisel",
                "Ozark",
            ]
            
            # Find close matches against popular shows
            close_matches = get_close_matches(show_name, popular_shows, n=2, cutoff=0.5)
            
            # If found a close match, try searching TVMaze again with the suggestion
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
            
            # Also check current tracked shows for close matches
            cfg = load_yaml(YAML_PATH)
            shows = cfg.get("shows", [])
            current_shows = [
                s["name"] if isinstance(s, dict) else str(s) 
                for s in shows
            ]
            
            current_close_matches = get_close_matches(show_name, current_shows, n=2, cutoff=0.6)
            
            if current_close_matches:
                return {
                    "valid": False,
                    "name": show_name,
                    "suggestions": current_close_matches,
                    "message": f"Not found on TVMaze. Did you mean: {', '.join(current_close_matches)}?"
                }
            else:
                return {
                    "valid": False,
                    "name": show_name,
                    "suggestions": [],
                    "message": f"'{show_name}' not found. Please check the spelling."
                }
    except Exception as e:
        # If there's an error, still allow the show but warn
        return {
            "valid": False,
            "name": show_name,
            "suggestions": [],
            "message": f"Could not validate against TVMaze: {str(e)}"
        }


@app.route("/")
def index():
    """Serve the HTML frontend."""
    return render_template("index.html")


@app.route("/api/shows", methods=["GET"])
def get_shows():
    """Get the list of tracked shows."""
    try:
        cfg = load_yaml(YAML_PATH)
        shows = cfg.get("shows", [])
        show_names = [
            s["name"] if isinstance(s, dict) else str(s) 
            for s in shows
        ]
        return jsonify({
            "shows": show_names,
            "region": cfg.get("region", "GB"),
            "days_ahead": cfg.get("days_ahead", 7)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows", methods=["POST"])
def add_show():
    """Add a new show to track."""
    try:
        data = request.json
        show_name = data.get("name", "").strip()
        force_add = data.get("force", False)  # Allow forced add with suggestion
        
        if not show_name:
            return jsonify({"error": "Show name is required"}), 400
        
        cfg = load_yaml(YAML_PATH)
        shows = cfg.get("shows", [])
        show_names = [
            s["name"] if isinstance(s, dict) else str(s) 
            for s in shows
        ]
        
        if show_name in show_names:
            return jsonify({"error": "Show already tracked"}), 400
        
        # Validate the show name
        validation = validate_show_name(show_name)
        
        if not validation["valid"] and not force_add:
            # Return validation error with suggestions
            return jsonify({
                "error": validation["message"],
                "suggestions": validation["suggestions"],
                "attempted_name": show_name,
                "needs_confirmation": bool(validation["suggestions"])
            }), 400
        
        # Use corrected name if validation found it, otherwise use as-is
        final_name = validation["name"]
        
        shows.append({"name": final_name})
        cfg["shows"] = shows
        
        with open(YAML_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False)
        
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
    """Remove a show from tracking."""
    try:
        cfg = load_yaml(YAML_PATH)
        shows = cfg.get("shows", [])
        
        # Filter out the show
        filtered_shows = [
            s for s in shows 
            if not (isinstance(s, dict) and s.get("name") == show_name) 
            and not (isinstance(s, str) and s == show_name)
        ]
        
        if len(filtered_shows) == len(shows):
            return jsonify({"error": "Show not found"}), 404
        
        cfg["shows"] = filtered_shows
        
        with open(YAML_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False)
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows/<show_name>/details", methods=["GET"])
def get_show_details(show_name):
    """Get details about a show from TVMaze."""
    try:
        # Search for the show
        show_id = tvmaze_search_show_id(show_name)
        
        if not show_id:
            return jsonify({"error": "Show not found on TVMaze"}), 404
        
        # Get full show details
        r = requests.get(f"https://api.tvmaze.com/shows/{show_id}", timeout=20)
        r.raise_for_status()
        show_data = r.json()
        
        # Extract relevant info
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
        
        # Get network/platform info
        network = show_data.get("network")
        web_channel = show_data.get("webChannel")
        if network:
            details["network"] = network.get("name")
        elif web_channel:
            details["network"] = web_channel.get("name")
        
        # Build IMDB URL if available
        if details["imdb_id"]:
            details["imdb_url"] = f"https://www.imdb.com/title/{details['imdb_id']}/"
        
        return jsonify(details)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upcoming", methods=["GET"])
def get_upcoming():
    """Get upcoming episodes for tracked shows."""
    try:
        cfg = load_yaml(YAML_PATH)
        region = (cfg.get("region") or "GB").upper()
        days_ahead = int(cfg.get("days_ahead", 7))
        shows = cfg.get("shows", [])
        
        tracked_names = [
            s["name"] if isinstance(s, dict) else str(s) 
            for s in shows
        ]
        tracked_names = [n.strip() for n in tracked_names if n and str(n).strip()]
        
        if not tracked_names:
            return jsonify({"upcoming": [], "count": 0})
        
        start = date.today()
        end = start + timedelta(days=days_ahead)
        
        schedule_eps = tvmaze_schedule_for_region(region, start, end)
        reminders = keep_next_episode_per_show(pick_matching_episodes(tracked_names, schedule_eps))

        # Convert to JSON-serializable format
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
    """Manually trigger a reminder check and send emails."""
    try:
        cfg = load_yaml(YAML_PATH)
        region = (cfg.get("region") or "GB").upper()
        days_ahead = int(cfg.get("days_ahead", 7))
        shows = cfg.get("shows", [])
        
        tracked_names = [
            s["name"] if isinstance(s, dict) else str(s) 
            for s in shows
        ]
        tracked_names = [n.strip() for n in tracked_names if n and str(n).strip()]
        
        if not tracked_names:
            return jsonify({
                "success": False, 
                "message": "No shows tracked"
            }), 400
        
        start = date.today()
        end = start + timedelta(days=days_ahead)
        
        schedule_eps = tvmaze_schedule_for_region(region, start, end)
        reminders = keep_next_episode_per_show(pick_matching_episodes(tracked_names, schedule_eps))

        # Load state and filter new reminders
        state = load_state(STATE_PATH)
        sent = state.get("sent", {})
        new_reminders = [r for r in reminders if not sent.get(reminder_key(r))]
        
        if not new_reminders:
            return jsonify({
                "success": True,
                "message": "No new reminders to send",
                "count": 0
            })
        
        subject, body = format_subject_body(new_reminders, days_ahead)
        send_email(subject, body)
        
        # Update state
        for r in new_reminders:
            sent[reminder_key(r)] = datetime.now().isoformat()
        
        state["sent"] = sent
        save_state(STATE_PATH, state)
        
        return jsonify({
            "success": True,
            "message": f"Sent {len(new_reminders)} reminder(s)",
            "count": len(new_reminders)
        })
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500


@app.route("/api/config", methods=["GET"])
def get_config():
    """Get configuration."""
    try:
        cfg = load_yaml(YAML_PATH)
        return jsonify({
            "region": cfg.get("region", "GB"),
            "days_ahead": cfg.get("days_ahead", 7)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
def update_config():
    """Update configuration."""
    try:
        data = request.json
        cfg = load_yaml(YAML_PATH)
        
        if "region" in data:
            cfg["region"] = data["region"].upper()
        if "days_ahead" in data:
            cfg["days_ahead"] = int(data["days_ahead"])
        
        with open(YAML_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False)
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5003, debug=False)
