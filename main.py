import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from google.auth import default as google_auth_default
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from notify import send_status_email

# ── Logging — stdout + rotating file ─────────────────────────────────────────
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)

_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_root = logging.getLogger()
_root.setLevel(logging.INFO)

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
_root.addHandler(_sh)

_fh = RotatingFileHandler(_log_dir / "sync.log", maxBytes=1_000_000, backupCount=3)
_fh.setFormatter(_fmt)
_root.addHandler(_fh)

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
LL2_BASE = "https://ll.thespacedevs.com/2.2.0"
LL2_API_KEY = os.environ.get("LL2_API_KEY", "")          # optional — raises rate limit
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
DEFAULT_DURATION_HOURS = 2


def get_calendar_service():
    creds, _ = google_auth_default(scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def fetch_upcoming_launches() -> list[dict]:
    """Fetch all upcoming SpaceX launches from Launch Library 2."""
    headers = {"Authorization": f"Token {LL2_API_KEY}"} if LL2_API_KEY else {}
    url = f"{LL2_BASE}/launch/upcoming/"
    params = {"lsp__name": "SpaceX", "format": "json", "limit": 100}
    launches: list[dict] = []

    while url:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        launches.extend(data.get("results", []))
        url = data.get("next")
        params = {}  # pagination URL already includes params

    return launches


def get_existing_spacex_events(service) -> dict[str, dict]:
    events: dict[str, dict] = {}
    page_token = None
    while True:
        result = (
            service.events()
            .list(
                calendarId=CALENDAR_ID,
                privateExtendedProperty="spacex_sync=true",
                maxResults=250,
                pageToken=page_token,
                showDeleted=False,
            )
            .execute()
        )
        for event in result.get("items", []):
            spacex_id = (
                event.get("extendedProperties", {}).get("private", {}).get("spacex_id")
            )
            if spacex_id:
                events[spacex_id] = event
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return events


def _precision_name(launch: dict) -> str:
    """Return the net_precision name in lowercase, defaulting to 'hour'."""
    prec = launch.get("net_precision") or {}
    return (prec.get("name") or "Hour").lower()


def build_description(launch: dict) -> str:
    lines: list[str] = []

    # Rocket
    rocket_cfg = (launch.get("rocket") or {}).get("configuration") or {}
    rocket_name = rocket_cfg.get("full_name") or rocket_cfg.get("name")
    if rocket_name:
        lines.append(f"Rocket: {rocket_name}")

    # Launch pad
    pad = launch.get("pad") or {}
    pad_name = pad.get("name", "")
    loc_name = (pad.get("location") or {}).get("name", "")
    site = ", ".join(p for p in [pad_name, loc_name] if p)
    if site:
        lines.append(f"Launch Site: {site}")

    # Status + probability
    status_name = (launch.get("status") or {}).get("name")
    if status_name:
        lines.append(f"Status: {status_name}")

    probability = launch.get("probability")
    if probability is not None:
        lines.append(f"Launch Probability: {probability}%")

    # Mission description
    mission = launch.get("mission") or {}
    if mission.get("description"):
        lines.append("")
        lines.append(mission["description"])

    # Video + info URLs
    link_lines: list[str] = []
    for v in (launch.get("vidURLs") or [])[:2]:
        u = v.get("url") if isinstance(v, dict) else v
        title = (v.get("title") or "Webcast") if isinstance(v, dict) else "Webcast"
        if u:
            link_lines.append(f"{title}: {u}")
    for i in (launch.get("infoURLs") or [])[:2]:
        u = i.get("url") if isinstance(i, dict) else i
        title = (i.get("title") or "Info") if isinstance(i, dict) else "Info"
        if u:
            link_lines.append(f"{title}: {u}")
    if link_lines:
        lines.append("")
        lines.append("Links:")
        lines.extend(f"  {l}" for l in link_lines)

    # Precision note for non-exact dates
    prec = _precision_name(launch)
    if prec in ("day", "month", "quarter", "year"):
        lines.append("")
        lines.append(f"Note: Launch date is approximate (precision: {prec}).")

    lines.append("")
    lines.append(f"Launch Library ID: {launch['id']}")

    return "\n".join(lines)


def build_event(launch: dict) -> dict:
    net: str = launch.get("net", "")
    window_start: str = launch.get("window_start") or net
    window_end: str = launch.get("window_end") or ""
    prec = _precision_name(launch)

    # Launch site for the location field
    pad = launch.get("pad") or {}
    pad_name = pad.get("name", "")
    loc_name = (pad.get("location") or {}).get("name", "")
    location = ", ".join(p for p in [pad_name, loc_name] if p) or None

    if prec in ("hour", "minute", "second"):
        start = {"dateTime": window_start, "timeZone": "UTC"}
        if window_end and window_end != window_start:
            end = {"dateTime": window_end, "timeZone": "UTC"}
        else:
            end_dt = (
                datetime.fromisoformat(window_start.replace("Z", "+00:00"))
                + timedelta(hours=DEFAULT_DURATION_HOURS)
            )
            end = {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": "UTC"}
    else:
        # Day / month / year precision — all-day event
        date_only = net[:10]
        start = {"date": date_only}
        end_date = (
            datetime.strptime(date_only, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        end = {"date": end_date}

    event: dict = {
        "summary": f"SpaceX: {launch['name']}",
        "description": build_description(launch),
        "start": start,
        "end": end,
        "extendedProperties": {
            "private": {
                "spacex_id": launch["id"],
                "spacex_sync": "true",
            }
        },
    }
    if location:
        event["location"] = location

    return event


def sync_launches():
    logger.info("SpaceX calendar sync started")

    service = get_calendar_service()
    launches = fetch_upcoming_launches()
    existing = get_existing_spacex_events(service)
    logger.info(
        f"Fetched {len(launches)} upcoming SpaceX launches; "
        f"{len(existing)} events already in calendar"
    )

    created = updated = deleted = errors = 0

    for launch in launches:
        try:
            event = build_event(launch)
            spacex_id = launch["id"]

            if spacex_id in existing:
                service.events().patch(
                    calendarId=CALENDAR_ID,
                    eventId=existing[spacex_id]["id"],
                    body=event,
                ).execute()
                updated += 1
                logger.info(f"Updated:  {launch['name']}")
            else:
                service.events().insert(
                    calendarId=CALENDAR_ID,
                    body=event,
                ).execute()
                created += 1
                logger.info(f"Created:  {launch['name']}")

        except HttpError as e:
            logger.error(f"Calendar API error for {launch.get('name')}: {e}")
            errors += 1
        except Exception as e:
            logger.error(f"Unexpected error for {launch.get('name')}: {e}")
            errors += 1

    # Remove calendar events for launches no longer in the upcoming list
    # (cancelled, scrubbed, or already flown). Guard against mass-deletion
    # if the API returned nothing.
    if launches:
        fetched_ids = {launch["id"] for launch in launches}
        for spacex_id, event in existing.items():
            if spacex_id not in fetched_ids:
                try:
                    service.events().delete(
                        calendarId=CALENDAR_ID,
                        eventId=event["id"],
                    ).execute()
                    deleted += 1
                    logger.info(
                        f"Deleted:  {event.get('summary', spacex_id)} "
                        f"(no longer in upcoming list)"
                    )
                except HttpError as e:
                    logger.error(f"Delete error for {spacex_id}: {e}")
                    errors += 1

    logger.info(
        f"Sync complete — created: {created}, updated: {updated}, "
        f"deleted: {deleted}, errors: {errors}"
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    status = "ERRORS" if errors else "OK"
    send_status_email(
        f"[SpaceX Calendar] Sync {status} — {now}",
        "\n".join([
            "SpaceX Calendar Sync",
            now,
            "",
            f"  Fetched:  {len(launches)} upcoming launches",
            f"  Created:  {created}",
            f"  Updated:  {updated}",
            f"  Deleted:  {deleted}",
            f"  Errors:   {errors}",
        ]),
    )

    return errors


if __name__ == "__main__":
    sys.exit(sync_launches())
