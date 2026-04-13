"""
UniFi Protect LPR Dashboard
---------------------------
A small Flask app that queries the UniFi Protect API for license-plate
recognition (LPR) events and renders a daily log of vehicles "in / out",
mapping known plates to names.

Configuration via environment variables (see .env.example):

    PROTECT_HOST            e.g. https://192.168.1.1   (your UDM-SE)
    PROTECT_API_KEY         Protect integration API key ("access token")
    PROTECT_VERIFY_TLS      "true" or "false"  (default false; UDM uses a self-signed cert)
    ENTRY_CAMERA_IDS        comma-separated Protect camera ids that see cars COMING IN
    EXIT_CAMERA_IDS         comma-separated Protect camera ids that see cars LEAVING
                            (leave EXIT_CAMERA_IDS empty if you only have one camera;
                            the app will use first-seen / last-seen heuristic)
    KNOWN_PLATES_FILE       path to a YAML file mapping plates to names
                            (default: /data/known_plates.yaml)
    LOOKBACK_HOURS          how far back to fetch events for "today" (default 24)
    TIMEZONE                IANA tz name for display, e.g. "Europe/Paris"
"""
from __future__ import annotations

import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
import urllib3
import yaml
from flask import Flask, jsonify, render_template

# UDM-SE uses a self-signed cert by default; suppress the noisy warning when
# PROTECT_VERIFY_TLS is false.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("lpr")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
PROTECT_HOST = os.environ.get("PROTECT_HOST", "").rstrip("/")
PROTECT_API_KEY = os.environ.get("PROTECT_API_KEY", "")
PROTECT_VERIFY_TLS = os.environ.get("PROTECT_VERIFY_TLS", "false").lower() == "true"

ENTRY_CAMERA_IDS = {
    c.strip() for c in os.environ.get("ENTRY_CAMERA_IDS", "").split(",") if c.strip()
}
EXIT_CAMERA_IDS = {
    c.strip() for c in os.environ.get("EXIT_CAMERA_IDS", "").split(",") if c.strip()
}

KNOWN_PLATES_FILE = os.environ.get("KNOWN_PLATES_FILE", "/data/known_plates.yaml")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))
DISPLAY_TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Paris"))

if not PROTECT_HOST or not PROTECT_API_KEY:
    log.error("PROTECT_HOST and PROTECT_API_KEY must be set")
    sys.exit(1)


def load_known_plates() -> dict[str, str]:
    """Load the {plate: name} map. Re-read on every request so editing the file
    doesn't require a restart."""
    try:
        with open(KNOWN_PLATES_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # normalise plates to upper-case, no spaces
        return {
            str(k).upper().replace(" ", "").replace("-", ""): str(v)
            for k, v in data.items()
        }
    except FileNotFoundError:
        log.warning("known_plates file %s not found", KNOWN_PLATES_FILE)
        return {}
    except Exception as e:
        log.exception("failed to load known plates: %s", e)
        return {}


def normalise_plate(plate: str) -> str:
    return plate.upper().replace(" ", "").replace("-", "")


# --------------------------------------------------------------------------- #
# UniFi Protect client
# --------------------------------------------------------------------------- #
class ProtectClient:
    """Minimal client for the UniFi Protect integration API.

    The integration API (released in Protect 5.3) uses the header
    `X-API-KEY: <token>` and is rooted at /proxy/protect/integration/v1.

    We also fall back to the legacy /proxy/protect/api/events endpoint for
    the same token, because some Protect firmwares expose richer event
    metadata there. If the legacy endpoint rejects the key, the app still
    works with whatever the integration endpoint returns.
    """

    def __init__(self, host: str, api_key: str, verify: bool = False):
        self.host = host
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-API-KEY": api_key,
                "Accept": "application/json",
            }
        )
        self.session.verify = verify

    def _get(self, path: str, **params) -> Any:
        url = f"{self.host}{path}"
        r = self.session.get(url, params=params, timeout=15)
        if r.status_code >= 400:
            log.warning("GET %s -> %s: %s", url, r.status_code, r.text[:300])
        r.raise_for_status()
        return r.json()

    def list_cameras(self) -> list[dict]:
        """Return the list of cameras. Useful for discovering camera ids."""
        try:
            return self._get("/proxy/protect/integration/v1/cameras")
        except Exception:
            # Legacy shape: /proxy/protect/api/cameras exists but usually
            # requires cookie auth; we swallow failures and return empty.
            return []

    def list_events(self, start_ms: int, end_ms: int) -> list[dict]:
        """Return smart-detect events in the window [start_ms, end_ms].

        Tries the integration endpoint first, then the legacy one. Both
        return a list of event dicts; we only keep those that include
        `licensePlate` in `smartDetectTypes`.
        """
        events: list[dict] = []

        # ---- 1. Official integration API -------------------------------- #
        try:
            data = self._get(
                "/proxy/protect/integration/v1/events",
                start=start_ms,
                end=end_ms,
                # some firmwares accept a type filter; harmless if ignored
                types="smartDetectZone,smartDetectLine",
            )
            if isinstance(data, list):
                events = data
        except Exception as e:
            log.info("integration /events failed (%s); trying legacy", e)

        # ---- 2. Legacy API (richer metadata on older firmwares) --------- #
        if not events:
            try:
                data = self._get(
                    "/proxy/protect/api/events",
                    start=start_ms,
                    end=end_ms,
                )
                if isinstance(data, list):
                    events = data
            except Exception as e:
                log.warning("legacy /events also failed: %s", e)

        # Keep only LPR events
        lpr_events = [
            e
            for e in events
            if "licensePlate" in (e.get("smartDetectTypes") or [])
        ]
        return lpr_events

    def get_event(self, event_id: str) -> dict | None:
        """Fetch a single event by id (needed to get the plate text on the
        integration API, which omits metadata in list results)."""
        for path in (
            f"/proxy/protect/integration/v1/events/{event_id}",
            f"/proxy/protect/api/events/{event_id}",
        ):
            try:
                return self._get(path)
            except Exception:
                continue
        return None


client = ProtectClient(PROTECT_HOST, PROTECT_API_KEY, PROTECT_VERIFY_TLS)


# --------------------------------------------------------------------------- #
# Business logic
# --------------------------------------------------------------------------- #
def extract_plate_text(event: dict) -> str | None:
    """Pull the plate string out of an event, handling both the integration
    and the legacy shapes."""
    md = event.get("metadata") or {}
    lp = md.get("licensePlate")
    if isinstance(lp, dict) and lp.get("name"):
        return normalise_plate(lp["name"])
    # Some firmwares nest it under smartDetectEvents
    for sd in event.get("smartDetectEvents") or []:
        if sd.get("type") == "licensePlate" and sd.get("name"):
            return normalise_plate(sd["name"])
    # Top-level fallback
    if event.get("licensePlate"):
        return normalise_plate(event["licensePlate"])
    return None


def classify_direction(camera_id: str) -> str:
    """Return 'in', 'out' or 'unknown' based on which camera saw the plate."""
    if camera_id in ENTRY_CAMERA_IDS:
        return "in"
    if camera_id in EXIT_CAMERA_IDS:
        return "out"
    return "unknown"


def build_vehicle_log(events: list[dict], known: dict[str, str]) -> list[dict]:
    """Collapse raw events into one row per plate with arrival and departure.

    Strategy:
      * If we have camera-based direction (entry vs exit cams), use that.
      * Otherwise fall back to: first sighting = arrival, last = departure,
        but only if the span is > 60s (avoids flagging a single event as both).
    """
    by_plate: dict[str, dict] = {}

    for ev in events:
        plate = extract_plate_text(ev)
        if not plate:
            continue
        ts_ms = ev.get("start") or ev.get("timestamp") or 0
        cam = ev.get("device") or ev.get("camera") or ""
        direction = classify_direction(cam)

        row = by_plate.setdefault(
            plate,
            {
                "plate": plate,
                "name": known.get(plate),
                "known": plate in known,
                "arrived_at": None,
                "left_at": None,
                "arrival_camera": None,
                "exit_camera": None,
                "sightings": 0,
            },
        )
        row["sightings"] += 1

        if direction == "in":
            if row["arrived_at"] is None or ts_ms < row["arrived_at"]:
                row["arrived_at"] = ts_ms
                row["arrival_camera"] = cam
        elif direction == "out":
            if row["left_at"] is None or ts_ms > row["left_at"]:
                row["left_at"] = ts_ms
                row["exit_camera"] = cam
        else:
            # Unknown direction: treat first as arrival, later as departure
            if row["arrived_at"] is None or ts_ms < row["arrived_at"]:
                row["arrived_at"] = ts_ms
                row["arrival_camera"] = cam
            if row["left_at"] is None or ts_ms > row["left_at"]:
                # only count as a distinct departure if > 60s later
                if row["arrived_at"] and ts_ms - row["arrived_at"] > 60_000:
                    row["left_at"] = ts_ms
                    row["exit_camera"] = cam

    rows = list(by_plate.values())
    # Sort: still-present first (arrived but not left), then by arrival desc
    rows.sort(
        key=lambda r: (r["left_at"] is not None, -(r["arrived_at"] or 0))
    )
    return rows


def format_row_for_display(row: dict) -> dict:
    def fmt(ms: int | None) -> str | None:
        if not ms:
            return None
        return (
            datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .astimezone(DISPLAY_TZ)
            .strftime("%H:%M:%S")
        )

    out = dict(row)
    out["arrived_at_str"] = fmt(row["arrived_at"])
    out["left_at_str"] = fmt(row["left_at"])
    # Duration on site (if both known)
    if row["arrived_at"] and row["left_at"]:
        dur_s = (row["left_at"] - row["arrived_at"]) / 1000
        h, rem = divmod(int(dur_s), 3600)
        m, _ = divmod(rem, 60)
        out["duration_str"] = f"{h}h{m:02d}" if h else f"{m}m"
    else:
        out["duration_str"] = None
    out["status"] = "on site" if row["left_at"] is None else "left"
    return out


# --------------------------------------------------------------------------- #
# Flask app
# --------------------------------------------------------------------------- #
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/events")
def api_events():
    known = load_known_plates()
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(hours=LOOKBACK_HOURS)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    raw = client.list_events(start_ms, end_ms)

    # On the integration API, list results often omit metadata. Fetch details
    # for any event missing a plate text.
    enriched = []
    for ev in raw:
        if not extract_plate_text(ev):
            detail = client.get_event(ev["id"]) if ev.get("id") else None
            if detail:
                ev = {**ev, **detail}
        enriched.append(ev)

    rows = build_vehicle_log(enriched, known)
    rows = [format_row_for_display(r) for r in rows]

    return jsonify(
        {
            "generated_at": now.astimezone(DISPLAY_TZ).isoformat(timespec="seconds"),
            "lookback_hours": LOOKBACK_HOURS,
            "known_plate_count": len(known),
            "raw_event_count": len(raw),
            "vehicles": rows,
        }
    )


@app.route("/api/cameras")
def api_cameras():
    """Helper: list cameras + ids, so you can fill ENTRY_CAMERA_IDS / EXIT_CAMERA_IDS."""
    cams = client.list_cameras()
    return jsonify(
        [{"id": c.get("id"), "name": c.get("name"), "type": c.get("type")} for c in cams]
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
