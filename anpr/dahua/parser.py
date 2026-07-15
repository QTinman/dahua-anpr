"""Parsing of the Dahua eventManager multipart event stream.

When you attach to ``/cgi-bin/eventManager.cgi?action=attach&codes=[...]``
the camera keeps the HTTP response open and pushes events as parts of a
``multipart/x-mixed-replace`` body::

    --myboundary
    Content-Type: text/plain
    Content-Length: 473

    Code=TrafficJunction;action=Pulse;index=0;data={
       "PicName" : "...",
       "TrafficCar" : {
          "PlateNumber" : "ABC123",
          ...
       }
    }

Heartbeat parts (``Heartbeat``) keep the connection alive. Some firmwares
(and the ``snapManager.cgi?action=attachFileProc`` endpoint) additionally
push ``image/jpeg`` parts carrying the plate snapshot.
"""

import json
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

_EVENT_LINE_RE = re.compile(
    r"Code=(?P<code>[^;]+);action=(?P<action>[^;]+)(?:;index=(?P<index>[^;]+))?"
    r"(?:;data=(?P<data>.*))?$",
    re.DOTALL,
)


class ParsedPart:
    """One decoded multipart part: either an event dict or a jpeg image."""

    __slots__ = ("kind", "event", "image")

    def __init__(self, kind: str, event: Optional[Dict[str, Any]] = None,
                 image: Optional[bytes] = None):
        self.kind = kind  # "event" | "image" | "heartbeat"
        self.event = event
        self.image = image


def parse_event_body(text: str) -> Optional[Dict[str, Any]]:
    """Parse a ``Code=...;action=...;data={...}`` body into a dict.

    Returns None for heartbeats / unparseable bodies.
    """
    text = text.strip()
    if not text or text.lower() == "heartbeat":
        return None
    match = _EVENT_LINE_RE.search(text)
    if not match:
        return None
    code = match.group("code").strip()
    if code.lower() == "heartbeat":
        return None
    data_raw = match.group("data")
    data: Dict[str, Any] = {}
    if data_raw:
        data_raw = data_raw.strip()
        try:
            parsed = json.loads(data_raw)
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            data = {}
    return {
        "code": code,
        "action": (match.group("action") or "").strip(),
        "index": (match.group("index") or "").strip(),
        "data": data,
    }


class MultipartEventParser:
    """Incremental parser for the multipart/x-mixed-replace event stream.

    Feed raw bytes with :meth:`feed`; it yields :class:`ParsedPart` objects
    as soon as complete parts are available. Handles both text event parts
    and binary jpeg parts, with or without Content-Length headers.
    """

    def __init__(self, boundary: str = "myboundary"):
        self._boundary = b"--" + boundary.encode()
        self._buffer = b""

    def feed(self, chunk: bytes) -> Iterator[ParsedPart]:
        self._buffer += chunk
        while True:
            part = self._extract_part()
            if part is None:
                return
            headers, body = part
            parsed = self._decode_part(headers, body)
            if parsed is not None:
                yield parsed

    def _extract_part(self) -> Optional[Tuple[Dict[str, str], bytes]]:
        start = self._buffer.find(self._boundary)
        if start < 0:
            # Keep the tail in case a boundary is split across chunks.
            if len(self._buffer) > len(self._boundary):
                self._buffer = self._buffer[-len(self._boundary):]
            return None
        head_start = start + len(self._boundary)
        header_end = self._buffer.find(b"\r\n\r\n", head_start)
        sep_len = 4
        if header_end < 0:
            header_end = self._buffer.find(b"\n\n", head_start)
            sep_len = 2
        if header_end < 0:
            return None
        raw_headers = self._buffer[head_start:header_end].decode("utf-8", "replace")
        headers: Dict[str, str] = {}
        for line in raw_headers.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()
        body_start = header_end + sep_len

        length = headers.get("content-length")
        if length is not None and length.isdigit():
            body_end = body_start + int(length)
            if len(self._buffer) < body_end:
                return None  # body not fully received yet
            body = self._buffer[body_start:body_end]
            self._buffer = self._buffer[body_end:]
            return headers, body

        # No Content-Length: body runs until the next boundary.
        next_boundary = self._buffer.find(self._boundary, body_start)
        if next_boundary < 0:
            return None
        body = self._buffer[body_start:next_boundary]
        self._buffer = self._buffer[next_boundary:]
        return headers, body

    @staticmethod
    def _decode_part(headers: Dict[str, str], body: bytes) -> Optional[ParsedPart]:
        ctype = headers.get("content-type", "text/plain").lower()
        if ctype.startswith("image/"):
            return ParsedPart("image", image=body)
        text = body.decode("utf-8", "replace")
        event = parse_event_body(text)
        if event is None:
            if "heartbeat" in text.lower():
                return ParsedPart("heartbeat")
            return None
        return ParsedPart("event", event=event)


# --------------------------------------------------------------------------
# Normalisation of TrafficJunction / TrafficRedList / ... payloads
# --------------------------------------------------------------------------

_DIRECTION_NAMES = {
    "0": "Unknown", "1": "Approaching", "2": "Leaving",
}


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _as_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_traffic_event(code: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract plate / vehicle attributes from a Dahua traffic event payload.

    Dahua firmwares vary: the interesting fields may live in
    ``data["TrafficCar"]``, ``data["Vehicle"]``, ``data["Object"]`` or at the
    top level, and key names differ between firmware generations. This tries
    the known variants for each attribute.
    """
    car = data.get("TrafficCar") if isinstance(data.get("TrafficCar"), dict) else {}
    vehicle = data.get("Vehicle") if isinstance(data.get("Vehicle"), dict) else {}
    obj = data.get("Object") if isinstance(data.get("Object"), dict) else {}
    comm = data.get("CommInfo") if isinstance(data.get("CommInfo"), dict) else {}

    def pick(*keys: str) -> Any:
        return _first(*(source.get(key)
                        for source in (car, vehicle, obj, data)
                        for key in keys))

    direction = pick("Direction", "DrivingDirection", "MovingDirection")
    if isinstance(direction, list):
        direction = ",".join(str(d) for d in direction)
    direction = str(direction) if direction is not None else ""
    direction = _DIRECTION_NAMES.get(direction, direction)

    event_time = _first(
        pick("SnapTime", "DeviceTime", "Time", "UTC", "LocalTime"),
        comm.get("Time") if isinstance(comm, dict) else None,
    )

    return {
        "event_code": code,
        "plate": str(pick("PlateNumber", "Text", "Plate") or ""),
        "plate_color": str(pick("PlateColor") or ""),
        "plate_type": str(pick("PlateType") or ""),
        "country": str(pick("Country", "PlateCountry") or ""),
        "vehicle_type": str(pick("VehicleType", "Category", "CarType") or ""),
        "vehicle_color": str(pick("VehicleColor", "CarColor") or ""),
        "vehicle_brand": str(pick("VehicleSign", "Brand", "VehicleBrand") or ""),
        "vehicle_size": str(pick("VehicleSize", "CarSize") or ""),
        "speed": _as_float(pick("Speed", "VehicleSpeed")),
        "direction": direction,
        "lane": _as_int(pick("Lane", "LaneNumber", "LaneID")),
        "event_time": str(event_time) if event_time is not None else "",
    }


def extract_image_b64(data: Any) -> Optional[str]:
    """Find a base64-encoded JPEG embedded anywhere in the event payload.

    Some Dahua firmwares include the plate/scene picture directly in the event
    metadata (rather than only as a separate multipart image part). A base64
    JPEG begins with the marker ``/9j/``; we scan strings recursively and
    return the first plausible match so the picture can be stored without a
    separate snapshot request.
    """
    def scan(obj: Any) -> Optional[str]:
        if isinstance(obj, str):
            s = obj.strip()
            if len(s) > 512 and s[:4] == "/9j/":
                return s
            return None
        if isinstance(obj, dict):
            for value in obj.values():
                found = scan(value)
                if found:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = scan(value)
                if found:
                    return found
        return None

    return scan(data)


def is_traffic_code(code: str, subscribed: List[str]) -> bool:
    """True when an event code is one we asked for (or a Traffic* event)."""
    if code in subscribed or "All" in subscribed:
        return True
    return code.startswith("Traffic")


# --------------------------------------------------------------------------
# RecordFinder history parsing (for importing the camera's stored ANPR log)
# --------------------------------------------------------------------------

_RECORD_KEY_RE = re.compile(r"^\w+\[(\d+)\]\.(.+)$")


def parse_find_records(text: str) -> List[Dict[str, str]]:
    """Parse a Dahua RecordFinder ``doFind`` response into a list of dicts.

    The response is a flat key=value list where each record is indexed::

        found=11
        records[0].PlateNumber=SSE00
        records[0].Time=2026-07-15 11:18:00
        records[0].TrafficCar.VehicleColor=White
        records[1].PlateNumber=TT020
        ...

    Firmwares use either ``records[i]`` or ``items[i]`` and may nest fields
    (``records[0].TrafficCar.PlateNumber``). We index by the integer in the
    brackets and keep the remainder of the key as the (possibly dotted) field
    name; normalisation flattens dotted names later.
    """
    groups: Dict[int, Dict[str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        match = _RECORD_KEY_RE.match(key.strip())
        if not match:
            continue
        idx = int(match.group(1))
        field = match.group(2).strip()
        groups.setdefault(idx, {})[field] = value.strip()
    return [groups[i] for i in sorted(groups)]


def parse_find_count(text: str) -> Optional[int]:
    """Extract the ``found=N`` / ``count=N`` total from a RecordFinder reply."""
    for line in text.splitlines():
        line = line.strip()
        for key in ("found=", "count=", "total=", "sn="):
            if line.lower().startswith(key):
                try:
                    return int(line.split("=", 1)[1].strip())
                except ValueError:
                    return None
    return None


def parse_finder_object(text: str) -> Optional[str]:
    """Extract the finder object id from a ``factory.create`` reply.

    Response looks like ``result=1234`` (older) or ``<id>`` on its own line.
    """
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("result="):
            return line.split("=", 1)[1].strip()
    stripped = text.strip()
    if stripped.isdigit():
        return stripped
    return None


def _flat_get(rec: Dict[str, str], *names: str) -> str:
    """Look up a field by exact key or by matching the last dotted segment."""
    for name in names:
        value = rec.get(name)
        if value not in (None, ""):
            return value
    for key, value in rec.items():
        if value in (None, ""):
            continue
        if key.split(".")[-1] in names:
            return value
    return ""


def _record_time(rec: Dict[str, str]) -> str:
    """Return the record timestamp as an ISO-ish string.

    Dahua stores time either as ``YYYY-MM-DD HH:MM:SS`` or as a Unix epoch.
    Epoch conversion is done without ``datetime.now`` so it stays pure.
    """
    raw = _flat_get(rec, "Time", "SnapTime", "UTC", "CreateTime", "DeviceTime")
    if not raw:
        return ""
    if raw.isdigit() and len(raw) >= 9:
        # Unix seconds -> UTC string, avoiding tz/locale surprises.
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(int(raw), tz=timezone.utc) \
                .astimezone().isoformat(timespec="seconds")
        except (ValueError, OSError, OverflowError):
            return raw
    return raw


def normalize_traffic_record(rec: Dict[str, str]) -> Dict[str, Any]:
    """Map a flat RecordFinder record to AnprEvent-style fields."""
    direction = _flat_get(rec, "Direction", "DrivingDirection")
    direction = _DIRECTION_NAMES.get(direction, direction)
    return {
        "event_code": "TrafficJunction",
        "plate": _flat_get(rec, "PlateNumber", "Text", "Plate"),
        "plate_color": _flat_get(rec, "PlateColor"),
        "plate_type": _flat_get(rec, "PlateType"),
        "country": _flat_get(rec, "Country", "PlateCountry", "Region"),
        "vehicle_type": _flat_get(rec, "VehicleType", "Category", "CarType"),
        "vehicle_color": _flat_get(rec, "VehicleColor", "CarColor"),
        "vehicle_brand": _flat_get(rec, "VehicleSign", "Brand", "VehicleBrand"),
        "vehicle_size": _flat_get(rec, "VehicleSize", "CarSize"),
        "speed": _as_float(_flat_get(rec, "Speed", "VehicleSpeed")),
        "direction": direction,
        "lane": _as_int(_flat_get(rec, "Lane", "LaneNumber", "LaneID")),
        "event_time": _record_time(rec),
    }
