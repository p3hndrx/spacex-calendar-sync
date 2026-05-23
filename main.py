import logging
import os
from datetime import datetime, timedelta, timezone

import functions_framework
import requests
from google.auth import default as google_auth_default
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SPACEX_API_URL = "https://api.spacexdata.com/v5/launches/query"
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Default event duration when no launch window is provided (2 hours)
DEFAULT_WINDOW_SECONDS = 7200


def get_calendar_service():
    creds, _ = google_auth_default(scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def fetch_upcoming_launches() -> list[dict]:
    """Fetch all upcoming SpaceX launches with rocket and launchpad details populated."""
    payload = {
        "query": {"upcoming": True},
        "options": {
            "populate": [
                {"path": "rocket", "select": {"name": 1}},
                {
                    "path": "launchpad",
                    "select": {"name": 1, "full_name": 1, "locality": 1, "region": 1},
                },
            ],
            "sort": {"date_utc": "asc"},
            "limit": 200,
            "pagination": False,
        },
    }
    resp = requests.post(SPACEX_API_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # API returns {"docs": [...]} when pagination=False is honoured, or the raw list
    if isinstance(data, dict):
        return data.get("docs", [])
    return data


def get_existing_spacex_events(service) -> dict[str, dict]:
    """Return a map of {spacex_id: calendar_event} for all events we previously created."""
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


def build_description(launch: dict) -> str:
    lines: list[str] = []

    rocket = launch.get("rocket")
    if isinstance(rocket, dict) and rocket.get("name"):
        lines.append(f"Rocket: {rocket['name']}")

    launchpad = launch.get("launchpad")
    if isinstance(launchpad, dict):
        parts = [
            launchpad.get("full_name"),
            launchpad.get("locality"),
            launchpad.get("region"),
        ]
        location_str = ", ".join(p for p in parts if p)
        if location_str:
            lines.append(f"Launch Site: {location_str}")

    flight_number = launch.get("flight_number")
    if flight_number:
        lines.append(f"Flight: #{flight_number}")

    details = launch.get("details")
    if details:
        lines.append("")
        lines.append(details)

    links = launch.get("links", {})
    link_lines: list[str] = []

    webcast = links.get("webcast")
    youtube_id = links.get("youtube_id")
    if webcast:
        link_lines.append(f"Webcast: {webcast}")
    elif youtube_id:
        link_lines.append(f"Webcast: https://www.youtube.com/watch?v={youtube_id}")

    if links.get("wikipedia"):
        link_lines.append(f"Wikipedia: {links['wikipedia']}")

    reddit = links.get("reddit") or {}
    if isinstance(reddit, dict) and reddit.get("launch"):
        link_lines.append(f"Reddit Discussion: {reddit['launch']}")

    if links.get("presskit"):
        link_lines.append(f"Press Kit: {links['presskit']}")

    if links.get("article"):
        link_lines.append(f"Article: {links['article']}")

    if link_lines:
        lines.append("")
        lines.append("Links:")
        lines.extend(f"  {l}" for l in link_lines)

    precision = launch.get("date_precision", "")
    if precision in ("month", "quarter", "half", "year"):
        lines.append("")
        lines.append(f"Note: Launch date is approximate (precision: {precision}).")

    lines.append("")
    lines.append(f"SpaceX ID: {launch['id']}")

    return "\n".join(lines)


def build_event(launch: dict) -> dict:
    date_utc: str = launch.get("date_utc", "")
    precision: str = launch.get("date_precision", "hour")
    window_seconds: int = launch.get("window") or DEFAULT_WINDOW_SECONDS

    launchpad = launch.get("launchpad")
    location = None
    if isinstance(launchpad, dict):
        parts = [
            launchpad.get("full_name"),
            launchpad.get("locality"),
            launchpad.get("region"),
        ]
        location = ", ".join(p for p in parts if p) or None

    if precision in ("hour", "minute"):
        start = {"dateTime": date_utc, "timeZone": "UTC"}
        end_dt = datetime.fromisoformat(date_utc.replace("Z", "+00:00")) + timedelta(
            seconds=max(window_seconds, DEFAULT_WINDOW_SECONDS)
        )
        end = {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": "UTC"}
    else:
        # day / month / quarter / half / year — use an all-day event
        date_only = date_utc[:10]
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


@functions_framework.http
def sync_launches(request):
    logger.info("SpaceX calendar sync started")

    try:
        service = get_calendar_service()
        launches = fetch_upcoming_launches()
        existing = get_existing_spacex_events(service)
        logger.info(
            f"Fetched {len(launches)} upcoming launches; "
            f"{len(existing)} events already in calendar"
        )

        created = updated = errors = 0

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
                    logger.info(f"Updated: {launch['name']}")
                else:
                    service.events().insert(
                        calendarId=CALENDAR_ID,
                        body=event,
                    ).execute()
                    created += 1
                    logger.info(f"Created: {launch['name']}")

            except HttpError as e:
                logger.error(f"Calendar API error for {launch.get('name')}: {e}")
                errors += 1
            except Exception as e:
                logger.error(f"Unexpected error for {launch.get('name')}: {e}")
                errors += 1

        summary = f"Sync complete — created: {created}, updated: {updated}, errors: {errors}"
        logger.info(summary)
        return summary, 200

    except Exception as e:
        logger.exception("Fatal error during sync")
        return f"Error: {e}", 500
