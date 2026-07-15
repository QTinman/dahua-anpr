from anpr.dahua.onvif import normalize_onvif_object, parse_onvif_metadata

PLATE_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<tt:MetadataStream xmlns:tt="http://www.onvif.org/ver10/schema">'
    '<tt:VideoAnalytics><tt:Frame UtcTime="2026-07-15T04:48:11Z">'
    '<tt:Object ObjectId="7"><tt:Appearance>'
    '<tt:Class><tt:Type>LicensePlate</tt:Type></tt:Class>'
    '<tt:VehicleInfo><tt:Type>Sedan</tt:Type><tt:Brand>Audi</tt:Brand>'
    '<tt:Color>Gray</tt:Color><tt:Speed>42</tt:Speed><tt:Size>Light</tt:Size>'
    '<tt:Image>/9j/VEHICLE</tt:Image></tt:VehicleInfo>'
    '<tt:LicensePlateInfo><tt:PlateNumber>DKL70</tt:PlateNumber>'
    '<tt:CountryCode>DEU</tt:CountryCode><tt:Color>White</tt:Color>'
    '<tt:Image>/9j/PLATE</tt:Image></tt:LicensePlateInfo>'
    '</tt:Appearance></tt:Object></tt:Frame></tt:VideoAnalytics>'
    '</tt:MetadataStream>'
)

EMPTY_FRAME_XML = (
    '<tt:MetadataStream xmlns:tt="http://www.onvif.org/ver10/schema">'
    '<tt:VideoAnalytics><tt:Frame UtcTime="2026-07-15T04:48:11Z">'
    '<tt:Object ObjectId="0"><tt:Appearance>'
    '<tt:LicensePlateInfo><tt:PlateNumber></tt:PlateNumber></tt:LicensePlateInfo>'
    '</tt:Appearance></tt:Object></tt:Frame></tt:VideoAnalytics>'
    '</tt:MetadataStream>'
)


def test_parse_onvif_plate_and_images():
    objs = parse_onvif_metadata(PLATE_XML)
    assert len(objs) == 1
    o = objs[0]
    assert o["plate"] == "DKL70"
    assert o["country"] == "DEU"
    assert o["vehicle_type"] == "Sedan"
    assert o["vehicle_brand"] == "Audi"
    assert o["vehicle_color"] == "Gray"
    assert o["speed"] == 42.0
    # plate cutout is preferred for the thumbnail
    assert o["image_b64"] == "/9j/PLATE"
    assert o["vehicle_image_b64"] == "/9j/VEHICLE"


def test_parse_onvif_skips_empty_frames():
    assert parse_onvif_metadata(EMPTY_FRAME_XML) == []
    assert parse_onvif_metadata("") == []
    assert parse_onvif_metadata("not xml") == []


def test_parse_onvif_handles_bom():
    objs = parse_onvif_metadata("﻿" + PLATE_XML)
    assert len(objs) == 1
    assert objs[0]["plate"] == "DKL70"


def test_normalize_onvif_object():
    o = parse_onvif_metadata(PLATE_XML)[0]
    fields = normalize_onvif_object(o)
    assert fields["plate"] == "DKL70"
    assert fields["country"] == "DEU"
    assert fields["vehicle_brand"] == "Audi"
    assert fields["event_time"] == "2026-07-15T04:48:11Z"
