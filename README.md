# LPR Vehicle Tracking Dashboard

A small FastAPI web app that shows which license plates have been seen by
UniFi Protect cameras in the last 24 hours, with known plates mapped to
names, and arrival / departure times per vehicle.

Built on [uiprotect](https://github.com/uilibs/uiprotect) — the same Python
library that powers the Home Assistant UniFi Protect integration. All the
messy Protect API details (auth, bootstrap, websocket, firmware quirks) are
handled upstream; this app just asks for events and renders them.

## Requirements

- A UniFi Protect NVR running **version 6 or later** (UDM Pro / SE / Max,
  UNVR, CloudKey Gen2+, etc.)
- **Python 3.11+** on Linux. Windows is not supported by `uiprotect`; use
  WSL or a Linux VM if you're developing on Windows.
- A **local** Protect admin account (Ubiquiti SSO accounts do not work).
  2FA must be disabled on this account.
- At least one camera with License Plate Recognition enabled.

## Quick start (development, on your laptop or the park box)

```bash
git clone https://github.com/StefanRu/LPR-vehicle-tracking-dashboard.git lpr
cd lpr

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

cp .env.example .env
cp known_plates.example.yaml known_plates.yaml
$EDITOR .env                 # fill in PROTECT_HOST, credentials, timezone
$EDITOR known_plates.yaml    # your real plates

set -a && source .env && set +a
.venv/bin/python app.py
```

Then open <http://localhost:8080>.

## Creating the Protect user

1. UniFi OS → **Admins & Users** → **Add Admin**
2. Role: Limited Admin → at minimum, View access to UniFi Protect
3. Username: `lpr-app`
4. Password: a long random string (e.g.
   `python -c 'import secrets; print(secrets.token_urlsafe(32))'`)
5. Disable 2FA for this user
6. Put the credentials in `.env`

## Finding your camera IDs

Once the app is running, hit `http://<host>:8080/api/cameras` — it returns
a JSON array of `{id, name}` for every camera on the NVR. Copy the IDs of
the cameras that watch arriving cars into `ENTRY_CAMERA_IDS` and the ones
watching departing cars into `EXIT_CAMERA_IDS`, then restart the app.

If you only have one camera covering both directions, leave both empty and
the app will use a first-seen / last-seen heuristic.

## Production deployment

See `PARK_SETUP.md` for the full Ubuntu Server + systemd + Tailscale runbook.
Short version:

```bash
# after git pull on the server:
cd /opt/lpr
.venv/bin/pip install -r requirements.txt
sudo systemctl restart lpr
```

## Endpoints

| Path           | What                                                |
|----------------|-----------------------------------------------------|
| `/`            | The dashboard (auto-refreshes every 30 s)           |
| `/api/events`  | JSON of the vehicle log for the lookback window     |
| `/api/cameras` | JSON list of cameras — use for finding IDs          |
| `/healthz`     | 200 if connected to Protect, 503 otherwise          |

## How it works

1. On startup, `uiprotect.ProtectApiClient.update()` logs in and loads the
   bootstrap (cameras + NVR state). The app caches a `camera_id → name`
   map from it.
2. Every time `/api/events` is hit, the app calls
   `client.get_events(start, end)` for the configured lookback window.
3. It filters to events whose `smart_detect_types` contain
   `LICENSE_PLATE`, pulls the plate string from
   `event.metadata.license_plate.name`, normalises it (uppercase,
   alphanumeric only), and looks it up in `known_plates.yaml`.
4. Events are grouped by plate. Depending on whether you've configured
   entry/exit cameras, arrival and departure are determined either by
   which camera saw the plate, or by first/last sighting in the window.
5. The frontend re-polls `/api/events` every 30 seconds.

If the Protect connection drops, the app reconnects on the next
`/api/events` request. No supervisor loop, no websocket plumbing in the
app code — `uiprotect` handles it.

## Why FastAPI + uiprotect and not Flask + requests

An earlier version of this project rolled its own Protect client using
`requests` and Flask. That works fine on firmwares where the legacy REST
API is accessible, but:

- Protect's API moves between firmware versions and the hand-rolled client
  needed patching
- No websocket means events take longer to appear
- License plate metadata lives in different places in different firmware
  versions

`uiprotect` is maintained against the latest Protect firmware and handles
all of that. Using an async library pulled FastAPI in as the logical web
framework choice. The result is a smaller, easier-to-maintain codebase
with fewer surprises when Ubiquiti ships a Protect update.

## License

MIT.
