"""
LPR Vehicle Tracking Dashboard
==============================

A small FastAPI app that polls a UniFi Protect NVR for license-plate
detection events and displays a daily log of vehicles arriving and leaving,
with known plates mapped to names.

Built on the `uiprotect` library (the same client that powers the Home
Assistant UniFi Protect integration), so Protect's API quirks, firmware
changes, and auth flow are handled upstream — we just ask for events.

Configuration (all via environment variables, typically loaded from .env):

    PROTECT_HOST            UDM-SE hostname or IP, e.g. "192.168.0.1"
                            (do NOT include "https://" or a port)
    PROTECT_PORT            usually 443
    PROTECT_USERNAME        a LOCAL Protect admin account (not an SSO account)
    PROTECT_PASSWORD        its password — 2FA must be disabled on this account
    PROTECT_VERIFY_SSL      "true" or "false" (default false — UDMs ship with
                            a self-signed cert)

    ENTRY_CAMERA_IDS        comma-separated Protect camera ids that see cars
                            COMING IN. Optional.
    EXIT_CAMERA_IDS         same, for cars LEAVING. Optional.
                            If both are empty, the app uses a first-seen /
                            last-seen heuristic on whatever camera fired.

    KNOWN_PLATES_FILE       path to a YAML file mapping plates to names
                            (default: ./known_plates.yaml). Re-read on every
                            request, so editing is instant — no restart.

    LOOKBACK_HOURS          how far back to fetch events (default 24)
    TIMEZONE                IANA tz name for display, e.g. "Europe/Paris"
    PORT                    HTTP port to listen on (default 8080)

Run:
    uvicorn app:app --host 0.0.0.0 --port 8080
or:
    python app.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
import aiohttp
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# uiprotect is async and pulls in its own aiohttp session
from uiprotect import ProtectApiClient
from uiprotect.data import EventType, SmartDetectObjectType
from uiprotect.exceptions import NotAuthorized, NvrError

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("lpr")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
PROTECT_HOST = os.environ.get("PROTECT_HOST", "").strip()
PROTECT_PORT = int(os.environ.get("PROTECT_PORT", "443"))
PROTECT_USERNAME = os.environ.get("PROTECT_USERNAME", "").strip()
PROTECT_PASSWORD = os.environ.get("PROTECT_PASSWORD", "")
PROTECT_VERIFY_SSL = os.environ.get("PROTECT_VERIFY_SSL", "false").lower() == "true"

ENTRY_CAMERA_IDS = {
    c.strip() for c in os.environ.get("ENTRY_CAMERA_IDS", "").split(",") if c.strip()
}
EXIT_CAMERA_IDS = {
    c.strip() for c in os.environ.get("EXIT_CAMERA_IDS", "").split(",") if c.strip()
}

KNOWN_PLATES_FILE = Path(
    os.environ.get("KNOWN_PLATES_FILE", "./known_plates.yaml")
).expanduser()

LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))
DISPLAY_TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Paris"))
PORT = int(os.environ.get("PORT", "8080"))

# Strip accidental scheme from PROTECT_HOST — uiprotect wants just the host.
if PROTECT_HOST.startswith(("http://", "https://")):
    PROTECT_HOST = PROTECT_HOST.split("://", 1)[1]
PROTECT_HOST = PROTECT_HOST.rstrip("/")

if not PROTECT_HOST:
    log.error("PROTECT_HOST must be set (e.g. 192.168.0.1)")
    sys.exit(1)
if not (PROTECT_USERNAME and PROTECT_PASSWORD):
    log.error("PROTECT_USERNAME and PROTECT_PASSWORD must be set")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Helpers: known plates, plate normalisation
# --------------------------------------------------------------------------- #
def normalise_plate(plate: str) -> str:
    """Upper-case, strip spaces / dashes. Done consistently everywhere so a
    user can write 'AB-123-CD' in the YAML and match 'ab 123 cd' from Protect."""
    return "".join(ch for ch in plate.upper() if ch.isalnum())


def load_known_plates() -> dict[str, str]:
    """Load {plate: name}. Re-read every request so edits are instant.
    Never raises — returns an empty dict on any error."""
    try:
        with KNOWN_PLATES_FILE.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return {normalise_plate(str(k)): str(v) for k, v in raw.items()}
    except FileNotFoundError:
        log.warning("known_plates file %s not found", KNOWN_PLATES_FILE)
        return {}
    except Exception:
        log.exception("failed to load known plates")
        return {}


# --------------------------------------------------------------------------- #
# Protect client lifecycle
# --------------------------------------------------------------------------- #
#
# `uiprotect` expects a single long-lived ProtectApiClient. We create it once
# at app startup, keep it alive for the lifetime of the FastAPI process, and
# close it on shutdown. `update()` loads the bootstrap (cameras + NVR info)
# and opens the Protect websocket — even though we don't use the websocket
# for event delivery, it keeps the session warm so subsequent REST calls
# don't re-authenticate every 30 seconds.
#
# State is stashed on `app.state` so the request handlers can reach it.
# --------------------------------------------------------------------------- #
class ProtectState:
    """Small holder for the live Protect client + derived camera map.

    Also enforces a minimum interval between reconnection attempts so that
    a failing auth config doesn't get retried on every request. The UDM's
    login endpoint has a rate limiter and we must not get stuck in a loop
    that triggers it.
    """

    # Minimum seconds between reconnect attempts after a failure.
    # The UDM returns 429 "Too Many Requests" after ~5 failed logins in
    # quick succession, and the ban window is a few minutes. 60s of
    # back-off between attempts keeps us comfortably under that.
    RECONNECT_COOLDOWN_S = 60

    def __init__(self) -> None:
        self.client: ProtectApiClient | None = None
        self.camera_names: dict[str, str] = {}
        self._last_failure_ts: float = 0.0
        self._last_failure_msg: str = ""

    async def connect(self) -> None:
        log.info("connecting to Protect at %s:%s as %s",
                 PROTECT_HOST, PROTECT_PORT, PROTECT_USERNAME)

        # Build our own aiohttp session with a browser User-Agent.
        #
        # Some UDM firmwares (observed on newer builds) reject non-browser
        # User-Agents on /api/auth/login with a 403, as a bot-protection
        # measure. uiprotect's default UA ("Python/3.x aiohttp/3.x") trips
        # this; curl with its default UA does not. Passing our own session
        # lets us override it.
        #
        # We also relax TLS because UDM ships with a self-signed cert.
        ssl_ctx = False if not PROTECT_VERIFY_SSL else None
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        custom_session = aiohttp.ClientSession(
            connector=connector,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            },
        )

        client = ProtectApiClient(
            host=PROTECT_HOST,
            port=PROTECT_PORT,
            username=PROTECT_USERNAME,
            password=PROTECT_PASSWORD,
            verify_ssl=PROTECT_VERIFY_SSL,
            session=custom_session,
            # Disable session caching. With it on (the default), uiprotect
            # persists the TOKEN cookie to disk and reloads it into the
            # aiohttp cookie jar on every startup. Re-POSTing to
            # /api/auth/login with a stale Cookie header makes some UDM
            # firmwares reject the request with 403 (observed on
            # eastparkserver). We'd rather pay the cost of a fresh login
            # on every app start than deal with poisoned cached cookies.
            store_sessions=False,
        )
        try:
            # update() logs in, fetches bootstrap, starts the websocket.
            await client.update()
        except Exception as e:
            # Mark the failure time so ensure_connected() won't try again
            # for at least RECONNECT_COOLDOWN_S seconds.
            import time as _t
            self._last_failure_ts = _t.monotonic()
            self._last_failure_msg = str(e)
            # Close the session we just made so we don't leak sockets.
            try:
                await custom_session.close()
            except Exception:
                pass
            raise
        self.client = client
        self._last_failure_ts = 0.0
        self._last_failure_msg = ""
        self._refresh_camera_map()
        log.info("connected; %d cameras in bootstrap", len(self.camera_names))

    def _refresh_camera_map(self) -> None:
        if self.client and self.client.bootstrap:
            self.camera_names = {
                cam.id: cam.name or cam.id
                for cam in self.client.bootstrap.cameras.values()
            }

    def cooldown_remaining(self) -> float:
        """Seconds remaining in the reconnect back-off. 0 if ready to try."""
        if self._last_failure_ts == 0.0:
            return 0.0
        import time as _t
        elapsed = _t.monotonic() - self._last_failure_ts
        return max(0.0, self.RECONNECT_COOLDOWN_S - elapsed)

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.close_session()
            except Exception:
                log.exception("error closing Protect session")
            self.client = None


state = ProtectState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Connect to Protect when the app starts, disconnect on shutdown.
    If the initial connection fails we DON'T crash — we log and let the
    /api/events endpoint surface the error. This matters for `systemctl
    start lpr` when Protect happens to be momentarily unreachable."""
    try:
        await state.connect()
    except Exception as e:
        log.error("initial Protect connection failed: %s (will retry on request)", e)
    yield
    await state.close()


app = FastAPI(title="LPR Dashboard", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Business logic: collapse raw events into one row per plate
# --------------------------------------------------------------------------- #
def extract_plate(event) -> str | None:
    """Pull the plate string from a uiprotect Event.

    The library exposes event.get_detected_thumbnail() which returns the
    "best" EventDetectedThumbnail — the one marked clock_best_wall. On a
    license-plate event, the plate string lives in:

        thumbnail.group.matched_name      (preferred, UFP 6.x+)
        thumbnail.name                    (fallback, some firmwares)

    Return None if neither is populated (which happens: LPR is not always
    confident enough to commit a plate string, even when the vehicle was
    detected).
    """
    try:
        thumb = event.get_detected_thumbnail()
    except Exception:
        thumb = None

    # Fallback: walk detected_thumbnails directly if the helper didn't
    # find one (e.g. none had clock_best_wall set).
    if thumb is None:
        md = getattr(event, "metadata", None)
        thumbs = getattr(md, "detected_thumbnails", None) or []
        for t in thumbs:
            t_type = getattr(t, "type", None)
            if hasattr(t_type, "value"):
                t_type = t_type.value
            if t_type == "licensePlate":
                thumb = t
                break

    if thumb is None:
        return None

    group = getattr(thumb, "group", None)
    if group is not None:
        matched = getattr(group, "matched_name", None)
        if matched:
            return normalise_plate(matched)
        # Some firmwares put the raw plate in group.name instead
        name = getattr(group, "name", None)
        if name:
            return normalise_plate(name)

    # Final fallback: the top-level thumbnail name field
    name = getattr(thumb, "name", None)
    if name:
        return normalise_plate(name)

    return None


def classify_direction(camera_id: str) -> str:
    if camera_id in ENTRY_CAMERA_IDS:
        return "in"
    if camera_id in EXIT_CAMERA_IDS:
        return "out"
    return "unknown"


def build_vehicle_log(events: list, known: dict[str, str]) -> list[dict]:
    """Collapse raw events to one row per plate with arrival + departure.

    Two modes, picked based on whether you've configured ENTRY/EXIT cameras:

      * Camera-based: plate seen on an ENTRY_CAMERA → arrival,
        on an EXIT_CAMERA → departure. Cleanest, requires two cameras.
      * Heuristic: first sighting of a plate in the window = arrival,
        last sighting = departure, only if >60s apart (so a single blip
        doesn't count as both arrival and departure).
    """
    by_plate: dict[str, dict] = {}
    has_direction_setup = bool(ENTRY_CAMERA_IDS or EXIT_CAMERA_IDS)

    for ev in events:
        plate = extract_plate(ev)
        if not plate:
            continue
        cam_id = ev.camera_id or ""
        cam_name = state.camera_names.get(cam_id, cam_id or "unknown")
        ts = ev.start  # tz-aware datetime from uiprotect
        if ts is None:
            continue
        ts_ms = int(ts.timestamp() * 1000)

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

        if has_direction_setup:
            direction = classify_direction(cam_id)
            if direction == "in":
                if row["arrived_at"] is None or ts_ms < row["arrived_at"]:
                    row["arrived_at"] = ts_ms
                    row["arrival_camera"] = cam_name
            elif direction == "out":
                if row["left_at"] is None or ts_ms > row["left_at"]:
                    row["left_at"] = ts_ms
                    row["exit_camera"] = cam_name
            # unknown-direction cameras are ignored in this mode
        else:
            # heuristic: any camera counts; first = arrival, last = departure
            if row["arrived_at"] is None or ts_ms < row["arrived_at"]:
                row["arrived_at"] = ts_ms
                row["arrival_camera"] = cam_name
            # departure only if far enough from arrival
            if (
                row["arrived_at"]
                and ts_ms - row["arrived_at"] > 60_000
                and (row["left_at"] is None or ts_ms > row["left_at"])
            ):
                row["left_at"] = ts_ms
                row["exit_camera"] = cam_name

    rows = list(by_plate.values())
    # still-on-site (no departure) first, then most-recently-arrived first
    rows.sort(key=lambda r: (r["left_at"] is not None, -(r["arrived_at"] or 0)))
    return rows


def format_row(row: dict) -> dict:
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
# Connection resilience
# --------------------------------------------------------------------------- #
async def ensure_connected() -> None:
    """If the initial connect failed or the session was lost, reconnect —
    but respect the back-off window so we don't hammer Protect's login
    endpoint when something is permanently wrong (bad credentials, 2FA
    still on, the user doesn't exist, etc.)."""
    if state.client is not None and state.client.bootstrap is not None:
        return

    remaining = state.cooldown_remaining()
    if remaining > 0:
        raise RuntimeError(
            f"Protect auth cooling down ({int(remaining)}s left): "
            f"{state._last_failure_msg}"
        )

    if state.client is None:
        await state.connect()
    else:
        # Client exists but bootstrap is empty — re-run update()
        await state.client.update()
        state._refresh_camera_map()


async def fetch_lpr_events(hours: int) -> list:
    """Fetch smart-detect LPR events from the last `hours` hours.

    We ask Protect to filter server-side by passing both `types` (we only
    want smart-detect) and `smart_detect_types` (only license plate). This
    is much cheaper than fetching everything and filtering in Python.

    `descriptions=True` is the library default and is what populates
    `event.metadata.detected_thumbnails` — don't set it to False or the
    plate text will be missing.
    """
    assert state.client is not None
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(hours=hours)
    events = await state.client.get_events(
        start=start,
        end=end,
        limit=2000,
        types=[EventType.SMART_DETECT],
        smart_detect_types=[SmartDetectObjectType.LICENSE_PLATE],
    )
    return events


# --------------------------------------------------------------------------- #
# HTTP routes
# --------------------------------------------------------------------------- #
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>LPR Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --bg: #0f1115; --panel: #181b22; --border: #262a33;
      --text: #e6e8eb; --muted: #8a93a0; --accent: #4cc38a;
      --warn: #e5a03a; --unknown: #777; --error: #e36363;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text);
    }
    header {
      padding: 1.5rem 2rem 1rem;
      border-bottom: 1px solid var(--border);
      display: flex; justify-content: space-between;
      align-items: baseline; flex-wrap: wrap; gap: 1rem;
    }
    header h1 { margin: 0; font-size: 1.4rem; font-weight: 600; }
    header .meta { color: var(--muted); font-size: 0.85rem; }
    main { padding: 1.5rem 2rem; }
    table {
      width: 100%; border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
    }
    th, td {
      padding: 0.75rem 1rem; text-align: left;
      border-bottom: 1px solid var(--border);
    }
    th {
      font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.04em;
      color: var(--muted); font-weight: 600; background: #13161c;
    }
    tr:last-child td { border-bottom: none; }
    .plate {
      font-family: "SF Mono", Menlo, Consolas, monospace;
      font-weight: 700; letter-spacing: 0.05em;
      background: #222733; padding: 0.2rem 0.5rem;
      border-radius: 4px; display: inline-block;
    }
    .plate.known { background: #1d3a2a; color: var(--accent); }
    .name { color: var(--accent); font-weight: 500; }
    .name.unknown { color: var(--unknown); font-style: italic; }
    .status.on-site { color: var(--accent); }
    .status.left { color: var(--muted); }
    .empty { text-align: center; color: var(--muted); padding: 2rem; }
    .error { text-align: center; color: var(--error); padding: 2rem; }
    .dash { color: var(--muted); }
  </style>
</head>
<body>
  <header>
    <h1>License plate activity</h1>
    <div class="meta">
      <span id="generated">loading…</span> &middot;
      <span id="counts"></span>
    </div>
  </header>
  <main>
    <table>
      <thead>
        <tr>
          <th>Plate</th><th>Name</th><th>Arrived</th><th>Left</th>
          <th>Duration</th><th>Status</th><th>Sightings</th>
        </tr>
      </thead>
      <tbody id="rows">
        <tr><td colspan="7" class="empty">Loading…</td></tr>
      </tbody>
    </table>
  </main>
  <script>
    async function refresh() {
      try {
        const r = await fetch("/api/events");
        const data = await r.json();
        if (data.error) {
          document.getElementById("rows").innerHTML =
            `<tr><td colspan="7" class="error">Error: ${data.error}</td></tr>`;
          document.getElementById("generated").textContent = "error";
          return;
        }
        document.getElementById("generated").textContent =
          "updated " + new Date(data.generated_at).toLocaleTimeString();
        document.getElementById("counts").textContent =
          `${data.vehicles.length} vehicles · ${data.raw_event_count} raw events · ${data.known_plate_count} known plates · last ${data.lookback_hours}h`;

        const tbody = document.getElementById("rows");
        if (!data.vehicles.length) {
          tbody.innerHTML = '<tr><td colspan="7" class="empty">No license plate events in this window.</td></tr>';
          return;
        }
        tbody.innerHTML = data.vehicles.map(v => `
          <tr>
            <td><span class="plate ${v.known ? 'known' : ''}">${v.plate}</span></td>
            <td class="name ${v.known ? '' : 'unknown'}">${v.name || 'unknown'}</td>
            <td>${v.arrived_at_str || '<span class="dash">—</span>'}</td>
            <td>${v.left_at_str || '<span class="dash">—</span>'}</td>
            <td>${v.duration_str || '<span class="dash">—</span>'}</td>
            <td class="status ${v.status === 'on site' ? 'on-site' : 'left'}">${v.status}</td>
            <td>${v.sightings}</td>
          </tr>
        `).join("");
      } catch (e) {
        document.getElementById("rows").innerHTML =
          `<tr><td colspan="7" class="error">Error: ${e.message}</td></tr>`;
      }
    }
    refresh();
    setInterval(refresh, 30000);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/healthz")
async def healthz():
    """Liveness probe for monitoring.
    Returns 200 if Protect is reachable and bootstrap is loaded."""
    ok = state.client is not None and state.client.bootstrap is not None
    return JSONResponse(
        {"ok": ok, "cameras": len(state.camera_names)},
        status_code=200 if ok else 503,
    )


@app.get("/api/cameras")
async def api_cameras():
    """Helper: returns id + name of every camera in the Protect bootstrap.
    Use this to find the camera ids to put in ENTRY_CAMERA_IDS / EXIT_CAMERA_IDS."""
    try:
        await ensure_connected()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return [
        {"id": cam_id, "name": name}
        for cam_id, name in sorted(
            state.camera_names.items(), key=lambda kv: kv[1].lower()
        )
    ]


@app.get("/api/events")
async def api_events():
    """Main endpoint: returns the vehicle activity log for the configured
    lookback window. Never raises; errors come back in the JSON body."""
    try:
        await ensure_connected()
    except NotAuthorized as e:
        log.error("Protect auth failed: %s", e)
        return JSONResponse(
            {"error": f"authentication failed: {e}"}, status_code=200
        )
    except NvrError as e:
        log.error("Protect unreachable: %s", e)
        return JSONResponse(
            {"error": f"protect unreachable: {e}"}, status_code=200
        )
    except Exception as e:
        log.exception("unexpected connection error")
        return JSONResponse({"error": str(e)}, status_code=200)

    try:
        raw = await fetch_lpr_events(LOOKBACK_HOURS)
    except Exception as e:
        log.exception("fetching events failed")
        return JSONResponse({"error": str(e)}, status_code=200)

    known = load_known_plates()
    rows = [format_row(r) for r in build_vehicle_log(raw, known)]

    now = datetime.now(tz=timezone.utc).astimezone(DISPLAY_TZ)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "lookback_hours": LOOKBACK_HOURS,
        "known_plate_count": len(known),
        "raw_event_count": len(raw),
        "vehicles": rows,
    }


# --------------------------------------------------------------------------- #
# Entry point for `python app.py` (systemd will use uvicorn/gunicorn directly)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
