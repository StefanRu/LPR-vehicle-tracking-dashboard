# LPR Dashboard for UniFi Protect

A small Flask app that pulls license-plate detection events from your UDM-SE /
UNVR running UniFi Protect and shows a daily log of who came in, when they
left, and how long they stayed. Known plates are mapped to names from a YAML
file you control.

## Setup

1. **Get a Protect integration API key** — Open the Protect application →
   gear icon → *Control Plane* → *Integrations* → create an API key. Save it.
   This is the same "access token for Protect" you already have.

2. **Find your console's local IP** — Easiest is the IP of your UDM-SE on
   your management VLAN. The cloud URL
   `https://unifi.ui.com/consoles/.../unifi-api/protect` works too if you set
   things up to talk to the cloud proxy, but local-LAN access is faster and
   doesn't depend on Ubiquiti's cloud being up.

3. **Configure**

   ```bash
   cp .env.example .env
   cp known_plates.example.yaml known_plates.yaml
   $EDITOR .env known_plates.yaml
   ```

4. **Run it** — either with Docker Compose:

   ```bash
   docker compose up -d --build
   ```

   or directly:

   ```bash
   pip install -r requirements.txt
   set -a && source .env && set +a
   python app.py
   ```

5. Open <http://localhost:8080>.

## Discovering camera IDs (for entry / exit direction)

Hit `http://localhost:8080/api/cameras` once it's running — you'll get a JSON
list of cameras with their Protect ids and names. Pick the ids of the cameras
that face arriving cars and put them in `ENTRY_CAMERA_IDS`, and the ones that
face leaving cars in `EXIT_CAMERA_IDS`.

If you only have one camera covering both directions, leave both vars empty.
The app will use a "first sighting today = arrival, last sighting = departure"
heuristic, which is fine for a residential gate where cars don't typically
re-enter several times a day.

## Notes on the Protect API

UniFi released an official integration API in Protect 5.3. It uses
`X-API-KEY: <token>` and is mounted at `/proxy/protect/integration/v1`. There
are still a few rough edges:

- The list-events endpoint returns events but often without `metadata`, so the
  app fetches each event by id to get the plate text. (The legacy
  `/proxy/protect/api/events` endpoint, which the app also tries as a
  fallback, returns the metadata in the list result on some firmwares.)
- The integration WebSocket for events doesn't include the resolved plate
  string at all, only that `licensePlate` was detected — which is why this app
  polls every 30 seconds instead of subscribing to a WebSocket.

If a future Protect release adds a clean WebSocket payload for LPR, the
polling loop in `api/events` is the only thing that needs to change.

## Files

- `app.py` — Flask backend, Protect client, classification logic
- `templates/index.html` — single-page dark-mode UI, polls `/api/events`
- `known_plates.yaml` — your plate ↔ name map (edit-able without restart)
- `.env` — config
- `Dockerfile`, `docker-compose.yml` — containerisation
