#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml
from dotenv import load_dotenv


TVMAZE_SEARCH = "https://api.tvmaze.com/search/shows"
TVMAZE_SCHEDULE = "https://api.tvmaze.com/schedule"
TVMAZE_SCHEDULE_WEB = "https://api.tvmaze.com/schedule/web"
TVMAZE_SHOW = "https://api.tvmaze.com/shows/{id}"


@dataclass
class EpisodeReminder:
    show_name: str
    season: Optional[int]
    number: Optional[int]
    airdate: date
    airtime: Optional[str]
    network_or_platform: Optional[str]
    url: Optional[str]


def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"sent": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, state: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True, default=str)
    os.replace(tmp, path)


def tvmaze_search_show_id(show_name: str) -> Optional[int]:
    r = requests.get(TVMAZE_SEARCH, params={"q": show_name}, timeout=20)
    r.raise_for_status()
    results = r.json()
    if not results:
        return None
    # Take the top result - usually correct for well-known shows.
    return results[0]["show"]["id"]


def tvmaze_show_details(show_id: int) -> Dict[str, Any]:
    r = requests.get(TVMAZE_SHOW.format(id=show_id), timeout=20)
    r.raise_for_status()
    return r.json()


def tvmaze_schedule_for_region(region: str, start: date, end: date) -> List[Dict[str, Any]]:
    # Fetch both broadcast and streaming schedules per day.
    episodes: List[Dict[str, Any]] = []
    cur = start
    while cur <= end:
        date_str = cur.isoformat()
        # Broadcast TV (country-specific)
        r = requests.get(TVMAZE_SCHEDULE, params={"country": region, "date": date_str}, timeout=20)
        r.raise_for_status()
        episodes.extend(r.json())
        # Streaming/web (global, show nested under _embedded)
        r2 = requests.get(TVMAZE_SCHEDULE_WEB, params={"date": date_str}, timeout=20)
        r2.raise_for_status()
        for ep in r2.json():
            # Normalise structure to match broadcast episodes
            if "_embedded" in ep and "show" in ep["_embedded"]:
                ep = dict(ep)
                ep["show"] = ep["_embedded"]["show"]
            episodes.append(ep)
        cur += timedelta(days=1)
    return episodes


def normalise_title(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum() or ch.isspace()).strip()


def pick_matching_episodes(
    tracked_names: List[str],
    schedule_eps: List[Dict[str, Any]],
) -> List[EpisodeReminder]:
    tracked_norm = {normalise_title(n): n for n in tracked_names}
    out: List[EpisodeReminder] = []

    for ep in schedule_eps:
        show = ep.get("show") or {}
        show_name = show.get("name") or ""
        if not show_name:
            continue

        if normalise_title(show_name) not in tracked_norm:
            continue

        airdate_str = ep.get("airdate")
        if not airdate_str:
            continue

        airdate = datetime.strptime(airdate_str, "%Y-%m-%d").date()
        season = ep.get("season")
        number = ep.get("number")
        airtime = ep.get("airtime")  # often local time for that country
        url = ep.get("url")

        # Network or platform label
        network = None
        if ep.get("show", {}).get("network"):
            network = ep["show"]["network"].get("name")
        elif ep.get("show", {}).get("webChannel"):
            network = ep["show"]["webChannel"].get("name")

        out.append(
            EpisodeReminder(
                show_name=tracked_norm[normalise_title(show_name)],
                season=season,
                number=number,
                airdate=airdate,
                airtime=airtime,
                network_or_platform=network,
                url=url,
            )
        )

    # Sort by date then episode number so the earliest comes first
    out.sort(key=lambda x: (x.airdate, x.show_name, x.season or 0, x.number or 0))
    return out


def keep_next_episode_per_show(reminders: List[EpisodeReminder]) -> List[EpisodeReminder]:
    """Return only the earliest upcoming episode for each show."""
    seen: set = set()
    out: List[EpisodeReminder] = []
    for r in reminders:
        if r.show_name not in seen:
            seen.add(r.show_name)
            out.append(r)
    return out


def reminder_key(r: EpisodeReminder) -> str:
    # Unique key for "this episode on this date"
    s = r.season if r.season is not None else "?"
    n = r.number if r.number is not None else "?"
    return f"{r.show_name}|S{s}E{n}|{r.airdate.isoformat()}"


def format_subject_body(reminders: List[EpisodeReminder], days_ahead: int) -> Tuple[str, str]:
    load_dotenv("config.env")
    dashboard_url = os.environ.get("DASHBOARD_URL", "https://tv-reminder.vercel.app")

    if not reminders:
        return ("TV reminders - nothing due", "No tracked episodes due in the next window.")

    n = len(reminders)
    subject = f"📺 TV Reminders — {n} episode{'s' if n != 1 else ''} this week"
    lines: List[str] = []
    lines.append(subject)
    lines.append("=" * len(subject))
    lines.append("")

    today = date.today()
    for r in reminders:
        if r.airdate == today:
            when = "Today"
        elif r.airdate == today + timedelta(days=1):
            when = "Tomorrow"
        else:
            when = r.airdate.strftime("%A %-d %b")

        epcode = ""
        if r.season is not None and r.number is not None:
            epcode = f" — S{int(r.season):02d}E{int(r.number):02d}"
        elif r.season is not None:
            epcode = f" — S{int(r.season):02d}"

        at = f" at {r.airtime}" if r.airtime else ""
        via = f" · {r.network_or_platform}" if r.network_or_platform else ""

        lines.append(f"• {r.show_name}{epcode}")
        lines.append(f"  {when}{at}{via}")
        if r.url:
            lines.append(f"  {r.url}")
        lines.append("")

    lines.append("-" * 40)
    lines.append(f"View your dashboard: {dashboard_url}")
    return subject, "\n".join(lines)


def send_email(subject: str, body: str, to: str) -> None:
    load_dotenv("config.env")

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ["SMTP_USERNAME"]
    app_password = os.environ["SMTP_APP_PASSWORD"]
    email_from = os.environ.get("EMAIL_FROM", username)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = to
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(username, app_password)
        server.send_message(msg)


def main() -> int:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    yaml_path = os.path.join(base_dir, "shows.yaml")
    state_path = os.path.join(base_dir, "state.json")

    cfg = load_yaml(yaml_path)
    region = (cfg.get("region") or "GB").upper()
    days_ahead = int(cfg.get("days_ahead") or 7)
    shows = cfg.get("shows") or []
    tracked_names = [s["name"] if isinstance(s, dict) else str(s) for s in shows]
    tracked_names = [n.strip() for n in tracked_names if n and str(n).strip()]

    if not tracked_names:
        print("No shows found in shows.yaml")
        return 2

    start = date.today()
    end = start + timedelta(days=days_ahead)

    # Pull schedule and filter to tracked shows
    schedule_eps = tvmaze_schedule_for_region(region, start, end)
    reminders = keep_next_episode_per_show(pick_matching_episodes(tracked_names, schedule_eps))

    # De-dupe based on what we've already emailed
    state = load_state(state_path)
    sent = state.get("sent", {})
    new_reminders = []
    for r in reminders:
        k = reminder_key(r)
        if sent.get(k):
            continue
        new_reminders.append(r)

    if not new_reminders:
        print("No new reminders to send (either nothing due, or already notified).")
        return 0

    subject, body = format_subject_body(new_reminders, days_ahead)
    send_email(subject, body)

    # Mark as sent
    for r in new_reminders:
        sent[reminder_key(r)] = datetime.now(timezone.utc).isoformat()

    state["sent"] = sent
    save_state(state_path, state)

    print(f"Sent {len(new_reminders)} reminder(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())