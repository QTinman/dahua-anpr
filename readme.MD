# Standalone Dahua ANPR Monitor

A web-based monitor for ANPR (Automatic Number Plate Recognition) events from
**Dahua** traffic cameras — no Milestone XProtect (or any other VMS) required.
This is the standalone, web-UI successor to the original ANPRMonitor Windows
Forms application.

Cameras are connected directly over their HTTP CGI API: the server attaches to
`/cgi-bin/eventManager.cgi?action=attach&codes=[TrafficJunction]` on each
camera (Digest authentication), parses the multipart event stream, stores
every capture in SQLite and pushes it live to the browser over a WebSocket.

## Features

- **Direct Dahua camera integration** — HTTP event subscription with Digest
  auth; no vendor SDK, no VMS dependency.
- **Multi-camera support** — each camera runs its own listener task with
  automatic reconnect and exponential backoff.
- **Live web dashboard** — real-time plate feed with vehicle attributes
  (plate, country, vehicle type/color/brand, speed, direction, lane) and
  per-camera connection status badges.
- **Plate images** — the picture included with the event (an embedded base64
  image in the metadata, or a jpeg part in the stream) is stored. Only when an
  event carries no picture at all does the server fetch a live snapshot
  (`snapshot.cgi`) as a fallback — toggled per camera by *Snapshot fallback*.
- **SQLite persistence** — every event is stored for historical queries.
- **History sync** — import the camera's own stored ANPR log (Dahua
  `RecordFinder`) so the monitor's history matches the camera's, filling any
  gaps from before the monitor was connected. Duplicates are skipped.
- **Search & filtering** — by plate (partial match), camera and date range,
  with pagination and CSV export.
- **Scheduled reports** — daily CSV report at a configurable time/directory,
  plus on-demand generation.
- **Camera management UI** — add/edit/delete cameras (IP, port, credentials,
  channel, event codes), test connection button, enable/disable per camera.

## Quick start

Requires Python 3.10+.

```bash
pip install -r requirements.txt
python -m anpr
```

**Recommended:** launch with the bundled script so the database always lives
next to it and never scatters across folders on restart:

```bash
./run.sh          # Linux / macOS
run.bat           # Windows (double-click or run from a terminal)
```

The startup log prints the database file in use, e.g.
`Using database /path/anpr.db (1 camera(s), 42 event(s))`; you can also open
`http://localhost:8080/api/diagnostics`. If the Cameras tab is empty but you
configured a camera before, the server is reading a different `anpr.db` than
last time — use the launch script (or set `ANPR_DB` to a fixed absolute
path) so it is always the same file.

Open http://localhost:8080, go to the **Cameras** tab and add your camera
(IP, port 80, username, password). The status badge turns green once the
event stream is attached; plates appear on the **Live** tab as vehicles pass.

### Demo mode (no camera needed)

To try the UI without a physical camera:

```bash
ANPR_DEMO=1 python -m anpr
```

A virtual "Demo Camera" generates a random plate event every few seconds.

### Configuration (environment variables)

| Variable    | Default   | Description                          |
|-------------|-----------|--------------------------------------|
| `ANPR_HOST` | `0.0.0.0` | HTTP bind address                    |
| `ANPR_PORT` | `8080`    | HTTP port                            |
| `ANPR_DB`   | `anpr.db` | SQLite database path                 |
| `ANPR_DEMO` | unset     | Set to `1` to enable the simulator   |

## How it talks to the camera

| Purpose            | Endpoint |
|--------------------|----------|
| Event stream       | `GET /cgi-bin/eventManager.cgi?action=attach&codes=[TrafficJunction]&heartbeat=5` |
| Connection test    | `GET /cgi-bin/magicBox.cgi?action=getDeviceType` |
| Snapshot fallback  | `GET /cgi-bin/snapshot.cgi?channel=N` |
| History import     | `GET /cgi-bin/recordFinder.cgi?action=factory.create&name=TrafficSnapEventInfo` (then `startFind`/`doFind`) |

> **Note on record counts.** The camera keeps its own on-device event log; the
> live stream only delivers events from the moment the monitor connects. If the
> monitor shows fewer records than the camera's web UI, use **Sync history** on
> the Cameras tab to import the stored log. To capture *Manual Snapshot* events
> too, set the camera's **Event codes** to `All`.

The event stream is a long-lived `multipart/x-mixed-replace` response. Each
part carries `Code=TrafficJunction;action=Start;index=0;data={...json...}`;
the JSON `TrafficCar` object contains the plate number and vehicle
attributes. Field names vary between firmware generations, so the parser
tries the known variants (`PlateNumber`/`Text`, `VehicleColor`/`CarColor`,
etc.). Heartbeat parts keep the connection alive; `image/jpeg` parts (sent
by some firmwares) are matched to the preceding event.

By default the app subscribes to `TrafficJunction`. Other codes (e.g.
`TrafficRedList`, `TrafficBlackList`) can be added per camera in the UI as a
comma-separated list.

## Architecture

```
anpr/
├── main.py            # FastAPI app, startup/shutdown wiring
├── api.py             # REST + WebSocket endpoints
├── models.py          # Pydantic models
├── database.py        # SQLite layer (cameras, events, settings)
├── ws.py              # WebSocket hub (live push to browsers)
├── reports.py         # Daily CSV report scheduler
├── simulator.py       # Demo event generator (ANPR_DEMO=1)
└── dahua/
    ├── client.py      # Digest-auth HTTP client, event stream, snapshots
    ├── parser.py      # Multipart stream parser + event normalisation
    └── manager.py     # Per-camera worker tasks, reconnect logic
web/                   # Single-page frontend (vanilla JS, no build step)
tests/                 # pytest suite (parser + API)
```

### REST API

| Method | Path | Description |
|--------|------|-------------|
| GET    | `/api/cameras` | List cameras with live status |
| POST   | `/api/cameras` | Add a camera |
| PUT    | `/api/cameras/{id}` | Update a camera (empty password = keep) |
| DELETE | `/api/cameras/{id}` | Delete a camera (events are kept) |
| POST   | `/api/cameras/test` | Test a connection without saving |
| POST   | `/api/cameras/{id}/sync` | Import the camera's stored ANPR history |
| GET    | `/api/diagnostics` | Which database file is in use, plus row counts |
| GET    | `/api/events` | Search events (`plate`, `camera_id`, `date_from`, `date_to`, `limit`, `offset`) |
| GET    | `/api/events/{id}/image` | Plate image (jpeg/png) |
| GET    | `/api/events/export.csv` | CSV export with the same filters |
| GET/PUT| `/api/settings/report` | Daily report configuration |
| POST   | `/api/reports/run` | Generate a report now |
| WS     | `/ws` | Live events + camera status push |

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Notes

- Camera credentials are stored in the SQLite database in plain text (as in
  the original WinForms app). Run the server on a trusted network, or put it
  behind a reverse proxy with authentication if it must be exposed.
- The web UI itself has no login; it is intended for LAN / operator-station
  use, matching the original desktop application.
