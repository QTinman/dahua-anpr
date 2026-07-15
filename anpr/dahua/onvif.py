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
            vinfo = appearance.find(f"{TT}VehicleInfo")
            lpinfo = appearance.find(f"{TT}LicensePlateInfo")

            plate = _text(lpinfo, f"{TT}PlateNumber")
            vehicle_image = _text(vinfo, f"{TT}Image")
            plate_image = _text(lpinfo, f"{TT}Image")

            # Skip empty tracking frames (no plate, no picture, no vehicle).
            if not plate and not vehicle_image and not plate_image \
                    and vinfo is None:
                continue

            class_el = appearance.find(f"{TT}Class")
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
                "speed": _as_float(_text(vinfo, f"{TT}Speed")),
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
        "direction": "",
        "lane": None,
        "event_time": obj.get("utc", ""),
    }
