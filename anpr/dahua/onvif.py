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
    """Return (left, top, right, bottom) from the Shape, if non-zero."""
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
    # All-zero boxes appear on snapshot frames and carry no position info.
    return coords if any(coords) else None


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
            vehicle_image = _text(vinfo, f"{TT}Image")
            plate_image = _text(lpinfo, f"{TT}Image")
            bbox = _bounding_box(appearance)

            # Skip frames that carry nothing useful. Keep box-only tracking
            # frames: their bounding-box trajectory is used to infer direction.
            if (not plate and not vehicle_image and not plate_image
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
                "plate_type": _text(lpinfo, f"{TT}PlateType"),
                "country": _text(lpinfo, f"{TT}CountryCode"),
                "plate_color": _text(lpinfo, f"{TT}Color"),
                "plate_list": _text(lpinfo, f"{TT}ListType"),
                "vehicle_type": _text(vinfo, f"{TT}Type"),
                "vehicle_brand": _text(vinfo, f"{TT}Brand"),
                "vehicle_color": _text(vinfo, f"{TT}Color"),
                "vehicle_size": _text(vinfo, f"{TT}Size"),
                "direction": direction,
                "lane": lane,
                "properties": props,
                "speed": _as_float(_text(vinfo, f"{TT}Speed")),
                "bbox": bbox,
                # Use the full scene/vehicle image as the capture picture (it
                # shows the vehicle with its plate, matching the camera UI);
                # fall back to the plate cutout if that is all there is.
                "image_b64": vehicle_image or plate_image or None,
                "vehicle_image_b64": vehicle_image or None,
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
