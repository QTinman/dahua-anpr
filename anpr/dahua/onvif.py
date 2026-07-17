"""ONVIF metadata support.

Dahua ITC/ANPR cameras publish their richest ANPR data - including the
plate and vehicle images - on the ONVIF metadata stream (delivered over
RTSP), not on the HTTP CGI event API. This is the same source the original
Milestone integration consumed via ``MetadataLiveSource``.

A metadata document looks like::

    <tt:MetadataStream ...>
      <tt:VideoAnalytics>
        <tt:Frame UtcTime="2026-07-15T04:48:11Z">
          <tt:Object ObjectId="0">
            <tt:Appearance>
              <tt:Class><tt:Type>LicensePlate</tt:Type></tt:Class>
              <tt:VehicleInfo>
                <tt:Type>LargeTruck</tt:Type>
                <tt:Color>Silver</tt:Color>
                <tt:Image>/9j/4AAQ...(base64 jpeg)...</tt:Image>
              </tt:VehicleInfo>
              <tt:LicensePlateInfo>
                <tt:PlateNumber>ABC123</tt:PlateNumber>
                <tt:CountryCode>ROU</tt:CountryCode>
                <tt:Image>/9j/...(plate cutout)...</tt:Image>
              </tt:LicensePlateInfo>
            </tt:Appearance>
          </tt:Object>
        </tt:Frame>
      </tt:VideoAnalytics>
    </tt:MetadataStream>

This module only parses the XML; the RTSP transport that delivers it lives
in :mod:`anpr.dahua.rtsp`.
"""

import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

TT = "{http://www.onvif.org/ver10/schema}"


def _text(el: Optional[ET.Element], tag: str) -> str:
    if el is None:
        return ""
    child = el.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def _as_float(value: str) -> Optional[float]:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _as_int(value: str) -> Optional[int]:
    try:
        return int(value) if value not in ("", None) else None
    except (ValueError, TypeError):
        return None


def _prop(props: Dict[str, str], *names: str) -> str:
    for name in names:
        value = props.get(name)
        if value:
            return value
    return ""


def _bounding_box(appearance) -> Optional[tuple]:
    """Return (left, top, right, bottom) from the Shape, if it has real area."""
    shape = appearance.find(f"{TT}Shape")
    if shape is None:
        return None
    box = shape.find(f"{TT}BoundingBox")
    if box is None:
        return None
    try:
        coords = (float(box.get("left", 0)), float(box.get("top", 0)),
                  float(box.get("right", 0)), float(box.get("bottom", 0)))
    except (TypeError, ValueError):
        return None
    # Snapshot frames carry placeholder boxes (all-zero, or all-ones like the
    # 1/1/1/1 the ITC413 sends) with no real area - ignore them.
    if coords[2] <= coords[0] or coords[3] <= coords[1]:
        return None
    return coords


# Basic colour palette for mapping an ONVIF RGB ColorCluster to a name. Newer
# firmwares report the dominant vehicle colour as RGB rather than a text label.
_COLOR_NAMES = (
    ("White", (255, 255, 255)), ("Black", (0, 0, 0)), ("Gray", (128, 128, 128)),
    ("Silver", (192, 192, 192)), ("Red", (200, 30, 30)), ("Blue", (40, 70, 200)),
    ("Green", (30, 140, 60)), ("Yellow", (225, 210, 40)), ("Brown", (120, 70, 30)),
)


def _rgb_to_name(r: float, g: float, b: float) -> str:
    best, best_dist = "", None
    for name, (cr, cg, cb) in _COLOR_NAMES:
        dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if best_dist is None or dist < best_dist:
            best, best_dist = name, dist
    return best


def _vehicle_color(vinfo) -> str:
    """Vehicle colour, from a text label (old firmware) or an RGB ColorCluster."""
    if vinfo is None:
        return ""
    label = _text(vinfo, f"{TT}Color")
    if label and label.lower() != "unknown":
        return label
    cluster = vinfo.find(f"{TT}Color/{TT}ColorCluster/{TT}Color")
    if cluster is not None:
        try:
            return _rgb_to_name(float(cluster.get("X", 0)),
                                float(cluster.get("Y", 0)),
                                float(cluster.get("Z", 0)))
        except (TypeError, ValueError):
            return ""
    return ""


def _clean(value: str) -> str:
    """Drop placeholder 'Unknown' labels so the UI shows a blank, not noise."""
    return "" if value.strip().lower() == "unknown" else value


# Direction inference from the bounding-box trajectory. A vehicle approaching
# the camera grows in apparent size; one leaving shrinks. The thresholds leave
# a dead-band so near-constant sizes stay "Unknown".
_DIR_GROW = 1.15
_DIR_SHRINK = 0.87


def direction_from_boxes(boxes: List[tuple]) -> str:
    """Infer 'Approaching' / 'Departing' from a sequence of bounding boxes."""
    areas = []
    for b in boxes:
        w = max(0.0, b[2] - b[0])
        h = max(0.0, b[3] - b[1])
        if w > 0 and h > 0:
            areas.append(w * h)
    if len(areas) < 2:
        return ""
    ratio = areas[-1] / areas[0]
    if ratio >= _DIR_GROW:
        return "Approaching"
    if ratio <= _DIR_SHRINK:
        return "Departing"
    return ""


def parse_onvif_metadata(xml_text: str) -> List[Dict[str, Any]]:
    """Parse an ONVIF metadata document into a list of object dicts.

    Each dict carries the normalised ANPR fields plus, when present, the
    base64 vehicle and plate images. Objects with neither a plate nor an
    image (empty tracking frames) are skipped.
    """
    if not xml_text:
        return []
    # Some cameras prepend a BOM or whitespace.
    xml_text = xml_text.lstrip("﻿ \r\n\t")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    results: List[Dict[str, Any]] = []
    for frame in root.iter(f"{TT}Frame"):
        utc = frame.get("UtcTime", "")
        for obj in frame.findall(f"{TT}Object"):
            appearance = obj.find(f"{TT}Appearance")
            if appearance is None:
                continue
            # Vendor attributes (direction, lane, etc.) are commonly carried as
            # <Property name="X">value</Property> under <tt:Extension>. Collect
            # them namespace-agnostically.
            props: Dict[str, str] = {}
            for el in obj.iter():
                if el.tag.rsplit("}", 1)[-1] == "Property":
                    name = el.get("name") or el.get("Name")
                    if name:
                        props[name] = (el.text or "").strip()
            vinfo = appearance.find(f"{TT}VehicleInfo")
            lpinfo = appearance.find(f"{TT}LicensePlateInfo")

            plate = _text(lpinfo, f"{TT}PlateNumber")
            # The capture image location moved between firmwares: newer ITC
            # firmware puts a single scene image directly under <Appearance>;
            # older firmware nested separate images under VehicleInfo and
            # LicensePlateInfo. Accept whichever is present.
            appearance_image = _text(appearance, f"{TT}Image")
            vehicle_image = _text(vinfo, f"{TT}Image")
            plate_image = _text(lpinfo, f"{TT}Image")
            capture_image = appearance_image or vehicle_image or plate_image or None
            bbox = _bounding_box(appearance)

            # Skip frames that carry nothing useful. Keep box-only tracking
            # frames: their bounding-box trajectory is used to infer direction.
            if (not plate and capture_image is None
                    and vinfo is None and bbox is None):
                continue

            class_el = appearance.find(f"{TT}Class")
            behaviour = obj.find(f"{TT}Behaviour")
            # Direction / lane may be a vendor Property, a Behaviour child, or a
            # dedicated element - try the known spellings.
            direction = (_prop(props, "Direction", "MovingDirection",
                               "CaptureDirection", "DrivingDirection")
                         or _text(behaviour, f"{TT}Direction"))
            lane = (_prop(props, "Lane", "LaneNo", "LaneNumber")
                    or _text(vinfo, f"{TT}Lane"))
            results.append({
                "utc": utc,
                "object_id": obj.get("ObjectId", ""),
                "class_type": _text(class_el, f"{TT}Type"),
                "plate": plate,
                "plate_type": _clean(_text(lpinfo, f"{TT}PlateType")),
                "country": _clean(_text(lpinfo, f"{TT}CountryCode")),
                "plate_color": _clean(_text(lpinfo, f"{TT}Color")),
                "plate_list": _text(lpinfo, f"{TT}ListType"),
                "vehicle_type": _clean(_text(vinfo, f"{TT}Type")),
                "vehicle_brand": _clean(_text(vinfo, f"{TT}Brand")),
                "vehicle_color": _vehicle_color(vinfo),
                "vehicle_size": _text(vinfo, f"{TT}Size"),
                "direction": direction,
                "lane": lane,
                "properties": props,
                "speed": _as_float(_text(vinfo, f"{TT}Speed")),
                "bbox": bbox,
                # The capture picture (full scene, matching the camera UI);
                # falls back to the plate cutout if that is all there is.
                "image_b64": capture_image,
                "vehicle_image_b64": appearance_image or vehicle_image or None,
                "plate_image_b64": plate_image or None,
            })
    return results


def normalize_onvif_object(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Map a parsed ONVIF object to AnprEvent-style fields."""
    return {
        "event_code": "ONVIF",
        "plate": obj.get("plate", ""),
        "plate_color": obj.get("plate_color", ""),
        "plate_type": obj.get("plate_type", ""),
        "country": obj.get("country", ""),
        "vehicle_type": obj.get("vehicle_type", ""),
        "vehicle_color": obj.get("vehicle_color", ""),
        "vehicle_brand": obj.get("vehicle_brand", ""),
        "vehicle_size": obj.get("vehicle_size", ""),
        "speed": obj.get("speed"),
        "direction": obj.get("direction", ""),
        "lane": _as_int(obj.get("lane", "")),
        "event_time": obj.get("utc", ""),
    }
