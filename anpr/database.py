"""SQLite persistence layer.

Uses the stdlib sqlite3 module guarded by a lock: the workload (a handful of
cameras inserting a few rows per second at most) does not justify a heavier
dependency, and every call is cheap enough to run on the event loop thread.
"""

import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from .models import AnprEvent, Camera, CameraCreate

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 80,
    username TEXT NOT NULL DEFAULT 'admin',
    password TEXT NOT NULL DEFAULT '',
    use_https INTEGER NOT NULL DEFAULT 0,
    channel INTEGER NOT NULL DEFAULT 1,
    event_codes TEXT NOT NULL DEFAULT 'TrafficJunction',
    snapshot_on_event INTEGER NOT NULL DEFAULT 1,
    use_onvif INTEGER NOT NULL DEFAULT 0,
    rtsp_port INTEGER NOT NULL DEFAULT 554,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id INTEGER NOT NULL,
    camera_name TEXT NOT NULL DEFAULT '',
    event_code TEXT NOT NULL DEFAULT '',
    plate TEXT NOT NULL DEFAULT '',
    plate_color TEXT NOT NULL DEFAULT '',
    plate_type TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    vehicle_type TEXT NOT NULL DEFAULT '',
    vehicle_color TEXT NOT NULL DEFAULT '',
    vehicle_brand TEXT NOT NULL DEFAULT '',
    vehicle_size TEXT NOT NULL DEFAULT '',
    speed REAL,
    direction TEXT NOT NULL DEFAULT '',
    lane INTEGER,
    event_time TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    image_b64 TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_plate ON events(plate);
CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at);
CREATE INDEX IF NOT EXISTS idx_events_camera ON events(camera_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS whitelist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate TEXT NOT NULL,
    plate_norm TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_whitelist_norm ON whitelist(plate_norm);
"""


def normalize_plate(plate: str) -> str:
    """Normalise a plate for comparison: upper-case, alphanumeric only."""
    return "".join(ch for ch in (plate or "").upper() if ch.isalnum())


class Database:
    def __init__(self, path: str = "anpr.db"):
        # Resolve to an absolute path so the database is always the same file
        # regardless of the process working directory. This avoids the
        # "records/cameras disappeared after a restart" trap where the server
        # is launched from a different directory and silently creates a fresh,
        # empty anpr.db next to it.
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        existing = {row["name"] for row in
                    self._conn.execute("PRAGMA table_info(cameras)")}
        additions = {
            "use_onvif": "INTEGER NOT NULL DEFAULT 0",
            "rtsp_port": "INTEGER NOT NULL DEFAULT 554",
            "snapshot_on_event": "INTEGER NOT NULL DEFAULT 1",
            "event_codes": "TEXT NOT NULL DEFAULT 'TrafficJunction'",
        }
        for column, ddl in additions.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE cameras ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def counts(self) -> Dict[str, int]:
        with self._lock:
            cameras = self._conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
            events = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {"cameras": cameras, "events": events}

    # ------------------------------------------------------------- cameras

    def list_cameras(self) -> List[Camera]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM cameras ORDER BY id").fetchall()
        return [self._row_to_camera(r) for r in rows]

    def get_camera(self, camera_id: int) -> Optional[Camera]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cameras WHERE id = ?", (camera_id,)
            ).fetchone()
        return self._row_to_camera(row) if row else None

    def add_camera(self, cam: CameraCreate) -> Camera:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO cameras
                   (name, host, port, username, password, use_https, channel,
                    event_codes, snapshot_on_event, use_onvif, rtsp_port, enabled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cam.name, cam.host, cam.port, cam.username, cam.password,
                    int(cam.use_https), cam.channel, cam.event_codes,
                    int(cam.snapshot_on_event), int(cam.use_onvif),
                    cam.rtsp_port, int(cam.enabled),
                ),
            )
            self._conn.commit()
            camera_id = cur.lastrowid
        return Camera(id=camera_id, **cam.model_dump())

    def update_camera(self, camera_id: int, fields: Dict[str, Any]) -> Optional[Camera]:
        if not fields:
            return self.get_camera(camera_id)
        allowed = {
            "name", "host", "port", "username", "password", "use_https",
            "channel", "event_codes", "snapshot_on_event", "use_onvif",
            "rtsp_port", "enabled",
        }
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if isinstance(value, bool):
                value = int(value)
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return self.get_camera(camera_id)
        values.append(camera_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE cameras SET {', '.join(sets)} WHERE id = ?", values
            )
            self._conn.commit()
        return self.get_camera(camera_id)

    def delete_camera(self, camera_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
            self._conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row_to_camera(row: sqlite3.Row) -> Camera:
        return Camera(
            id=row["id"],
            name=row["name"],
            host=row["host"],
            port=row["port"],
            username=row["username"],
            password=row["password"],
            use_https=bool(row["use_https"]),
            channel=row["channel"],
            event_codes=row["event_codes"],
            snapshot_on_event=bool(row["snapshot_on_event"]),
            use_onvif=bool(row["use_onvif"]),
            rtsp_port=row["rtsp_port"],
            enabled=bool(row["enabled"]),
        )

    # -------------------------------------------------------------- events

    def add_event(self, ev: AnprEvent) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO events
                   (camera_id, camera_name, event_code, plate, plate_color,
                    plate_type, country, vehicle_type, vehicle_color,
                    vehicle_brand, vehicle_size, speed, direction, lane,
                    event_time, received_at, image_b64)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ev.camera_id, ev.camera_name, ev.event_code, ev.plate,
                    ev.plate_color, ev.plate_type, ev.country, ev.vehicle_type,
                    ev.vehicle_color, ev.vehicle_brand, ev.vehicle_size,
                    ev.speed, ev.direction, ev.lane, ev.event_time,
                    ev.received_at, ev.image_b64,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def event_exists(self, camera_id: int, plate: str, event_time: str) -> bool:
        """Used to avoid importing history records already in the database."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE camera_id = ? AND plate = ? "
                "AND event_time = ? LIMIT 1",
                (camera_id, plate, event_time),
            ).fetchone()
        return row is not None

    def set_event_image(self, event_id: int, image_b64: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET image_b64 = ? WHERE id = ?", (image_b64, event_id)
            )
            self._conn.commit()

    def search_events(
        self,
        plate: str = "",
        camera_id: Optional[int] = None,
        date_from: str = "",
        date_to: str = "",
        limit: int = 100,
        offset: int = 0,
        with_images: bool = False,
    ) -> Dict[str, Any]:
        where, params = self._event_filters(plate, camera_id, date_from, date_to)
        columns = "*" if with_images else (
            "id, camera_id, camera_name, event_code, plate, plate_color, "
            "plate_type, country, vehicle_type, vehicle_color, vehicle_brand, "
            "vehicle_size, speed, direction, lane, event_time, received_at, "
            "(image_b64 IS NOT NULL) AS has_image"
        )
        sql = f"SELECT {columns} FROM events{where} ORDER BY id DESC LIMIT ? OFFSET ?"
        count_sql = f"SELECT COUNT(*) FROM events{where}"
        with self._lock:
            total = self._conn.execute(count_sql, params).fetchone()[0]
            rows = self._conn.execute(sql, params + [limit, offset]).fetchall()
        return {"total": total, "events": [dict(r) for r in rows]}

    def iter_events_for_export(
        self,
        plate: str = "",
        camera_id: Optional[int] = None,
        date_from: str = "",
        date_to: str = "",
    ) -> List[Dict[str, Any]]:
        where, params = self._event_filters(plate, camera_id, date_from, date_to)
        sql = (
            "SELECT id, camera_name, plate, plate_color, country, vehicle_type, "
            "vehicle_color, vehicle_brand, speed, direction, lane, event_time, "
            f"received_at FROM events{where} ORDER BY id"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def events_for_day(self, day: str, limit: int = 3000) -> List[Dict[str, Any]]:
        """All events on a given YYYY-MM-DD, oldest first (for playback)."""
        sql = (
            "SELECT id, camera_id, camera_name, event_code, plate, plate_color, "
            "plate_type, country, vehicle_type, vehicle_color, vehicle_brand, "
            "vehicle_size, speed, direction, lane, event_time, received_at, "
            "(image_b64 IS NOT NULL) AS has_image FROM events "
            "WHERE received_at LIKE ? ORDER BY received_at ASC, id ASC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (day + "%", limit)).fetchall()
        return [dict(r) for r in rows]

    def event_day_counts(self, month: str) -> Dict[str, int]:
        """Number of events per day within a YYYY-MM month."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT substr(received_at, 1, 10) AS day, COUNT(*) AS n "
                "FROM events WHERE received_at LIKE ? GROUP BY day",
                (month + "%",),
            ).fetchall()
        return {r["day"]: r["n"] for r in rows}

    def get_event_image(self, event_id: int) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT image_b64 FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return row["image_b64"] if row else None

    @staticmethod
    def _event_filters(
        plate: str, camera_id: Optional[int], date_from: str, date_to: str
    ):
        clauses, params = [], []
        if plate:
            clauses.append("plate LIKE ?")
            params.append(f"%{plate}%")
        if camera_id is not None:
            clauses.append("camera_id = ?")
            params.append(camera_id)
        if date_from:
            clauses.append("received_at >= ?")
            params.append(date_from)
        if date_to:
            # Make an inclusive day filter: '2026-07-15' matches the whole day.
            clauses.append("received_at <= ?")
            params.append(date_to + ("￿" if len(date_to) == 10 else ""))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    # ------------------------------------------------------------ settings

    # ----------------------------------------------------------- whitelist

    def list_whitelist(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, plate, label, created_at FROM whitelist "
                "ORDER BY plate"
            ).fetchall()
        return [dict(r) for r in rows]

    def add_whitelist(self, plate: str, label: str, created_at: str) -> Dict[str, Any]:
        norm = normalize_plate(plate)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO whitelist (plate, plate_norm, label, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(plate_norm) DO UPDATE SET plate=excluded.plate, "
                "label=excluded.label",
                (plate.strip(), norm, label.strip(), created_at),
            )
            row = self._conn.execute(
                "SELECT id, plate, label, created_at FROM whitelist "
                "WHERE plate_norm = ?", (norm,)
            ).fetchone()
            self._conn.commit()
        return dict(row)

    def delete_whitelist(self, entry_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM whitelist WHERE id = ?", (entry_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def whitelist_contains(self, plate: str) -> bool:
        norm = normalize_plate(plate)
        if not norm:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM whitelist WHERE plate_norm = ? LIMIT 1", (norm,)
            ).fetchone()
        return row is not None

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
            self._conn.commit()
