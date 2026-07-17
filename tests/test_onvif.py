from anpr.dahua.onvif import (
    direction_from_boxes,
    normalize_onvif_object,
    parse_onvif_metadata,
)


def test_direction_from_boxes():
    # box grows (area up ~4x) -> approaching
    grow = [(100, 100, 120, 120), (100, 100, 200, 200)]
    assert direction_from_boxes(grow) == "Approaching"
    # box shrinks -> departing
    shrink = [(100, 100, 200, 200), (100, 100, 120, 120)]
    assert direction_from_boxes(shrink) == "Departing"
    # near-constant size -> unknown (dead-band)
    steady = [(0, 0, 100, 100), (0, 0, 104, 104)]
    assert direction_from_boxes(steady) == ""
    # not enough data
    assert direction_from_boxes([(0, 0, 50, 50)]) == ""
    assert direction_from_boxes([]) == ""

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
    '</tt:Appearance>'
    '<tt:Behaviour><tt:Speed>42</tt:Speed></tt:Behaviour>'
    '<tt:Extension><Properties>'
    '<Property name="Direction">Approaching</Property>'
    '<Property name="Lane">2</Property></Properties></tt:Extension>'
    '</tt:Object></tt:Frame></tt:VideoAnalytics>'
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
    # the full scene/vehicle image is used as the capture picture
    assert o["image_b64"] == "/9j/VEHICLE"
    assert o["vehicle_image_b64"] == "/9j/VEHICLE"
    assert o["plate_image_b64"] == "/9j/PLATE"
    # direction/lane from the extension properties
    assert o["direction"] == "Approaching"
    assert o["lane"] == "2"


def test_normalize_onvif_direction_lane():
    o = parse_onvif_metadata(PLATE_XML)[0]
    fields = normalize_onvif_object(o)
    assert fields["direction"] == "Approaching"
    assert fields["lane"] == 2


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


# Newer ITC firmware (e.g. ITC413) puts the capture image directly under
# <Appearance>, reports vehicle colour as an RGB ColorCluster, and fills
# unknown attributes with the literal "Unknown".
NEW_FW_XML = (
    '<tt:MetadataStream xmlns:tt="http://www.onvif.org/ver10/schema">'
    '<tt:VideoAnalytics><tt:Frame UtcTime="2026-07-17T08:25:54Z">'
    '<tt:Object ObjectId="0"><tt:Appearance>'
    '<tt:Shape><tt:BoundingBox left="1" top="1" right="1" bottom="1"/></tt:Shape>'
    '<tt:Class><tt:Type Likelihood="0.9">LicensePlate</tt:Type></tt:Class>'
    '<tt:VehicleInfo><tt:Type Likelihood="0.9">Unknown</tt:Type>'
    '<tt:Brand Likelihood="0.9">Unknown</tt:Brand>'
    '<tt:Color><tt:ColorCluster>'
    '<tt:Color X="255" Y="255" Z="255" Colorspace="rgb"/>'
    '</tt:ColorCluster></tt:Color></tt:VehicleInfo>'
    '<tt:LicensePlateInfo><tt:PlateNumber Likelihood="0.9">EKG10</tt:PlateNumber>'
    '<tt:CountryCode Likelihood="0.9">Unknown</tt:CountryCode>'
    '</tt:LicensePlateInfo>'
    '<tt:Image>/9j/SCENE</tt:Image>'
    '</tt:Appearance></tt:Object></tt:Frame></tt:VideoAnalytics>'
    '</tt:MetadataStream>'
)


def test_parse_onvif_new_firmware_format():
    objs = parse_onvif_metadata(NEW_FW_XML)
    assert len(objs) == 1
    o = objs[0]
    assert o["plate"] == "EKG10"
    # image now lives directly under <Appearance>, not under VehicleInfo
    assert o["image_b64"] == "/9j/SCENE"
    # colour derived from the RGB ColorCluster (255,255,255 -> White)
    assert o["vehicle_color"] == "White"
    # placeholder "Unknown" labels are blanked out
    assert o["vehicle_type"] == ""
    assert o["country"] == ""
    # the degenerate 1/1/1/1 placeholder box is ignored
    assert o["bbox"] is None
