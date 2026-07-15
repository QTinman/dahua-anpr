import json

from anpr.dahua.parser import (
    MultipartEventParser,
    extract_image_b64,
    normalize_traffic_event,
    normalize_traffic_record,
    parse_event_body,
    parse_find_records,
    parse_finder_object,
)

TRAFFIC_JSON = {
    "PicName": "pic.jpg",
    "TrafficCar": {
        "PlateNumber": "ABC123",
        "PlateColor": "Yellow",
        "PlateType": "Normal",
        "Country": "SE",
        "VehicleColor": "White",
        "VehicleType": "Light",
        "VehicleSign": "Volvo",
        "Speed": 63,
        "Lane": 2,
        "Direction": "1",
    },
}


def make_part(body: bytes, ctype: str = "text/plain") -> bytes:
    return (
        b"--myboundary\r\n"
        + f"Content-Type: {ctype}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


def test_parse_event_body_with_json_data():
    body = "Code=TrafficJunction;action=Start;index=0;data=" + json.dumps(TRAFFIC_JSON)
    event = parse_event_body(body)
    assert event["code"] == "TrafficJunction"
    assert event["action"] == "Start"
    assert event["data"]["TrafficCar"]["PlateNumber"] == "ABC123"


def test_parse_event_body_heartbeat_returns_none():
    assert parse_event_body("Heartbeat") is None
    assert parse_event_body("Code=Heartbeat;action=Pulse;index=0") is None
    assert parse_event_body("") is None


def test_parse_event_body_no_data_section():
    event = parse_event_body("Code=VideoMotion;action=Start;index=0")
    assert event["code"] == "VideoMotion"
    assert event["data"] == {}


def test_multipart_parser_single_event():
    body = ("Code=TrafficJunction;action=Pulse;index=0;data="
            + json.dumps(TRAFFIC_JSON)).encode()
    parser = MultipartEventParser("myboundary")
    parts = list(parser.feed(make_part(body)))
    assert len(parts) == 1
    assert parts[0].kind == "event"
    assert parts[0].event["data"]["TrafficCar"]["Speed"] == 63


def test_multipart_parser_split_across_chunks():
    body = ("Code=TrafficJunction;action=Start;index=0;data="
            + json.dumps(TRAFFIC_JSON)).encode()
    raw = make_part(body)
    parser = MultipartEventParser("myboundary")
    parts = []
    # Feed one byte at a time - worst case fragmentation.
    for i in range(0, len(raw), 7):
        parts.extend(parser.feed(raw[i:i + 7]))
    assert len(parts) == 1
    assert parts[0].event["code"] == "TrafficJunction"


def test_multipart_parser_event_then_image():
    event_body = ("Code=TrafficJunction;action=Start;index=0;data="
                  + json.dumps(TRAFFIC_JSON)).encode()
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 50 + b"\xff\xd9"
    raw = make_part(event_body) + make_part(jpeg, "image/jpeg")
    parser = MultipartEventParser("myboundary")
    parts = list(parser.feed(raw))
    kinds = [p.kind for p in parts]
    assert kinds == ["event", "image"]
    assert parts[1].image == jpeg


def test_multipart_parser_without_content_length():
    part = (b"--myboundary\r\nContent-Type: text/plain\r\n\r\n"
            b"Code=TrafficJunction;action=Start;index=0\r\n"
            b"--myboundary\r\nContent-Type: text/plain\r\n\r\nHeartbeat\r\n"
            b"--myboundary")
    parser = MultipartEventParser("myboundary")
    parts = list(parser.feed(part))
    assert [p.kind for p in parts] == ["event", "heartbeat"]


def test_normalize_traffic_event():
    fields = normalize_traffic_event("TrafficJunction", TRAFFIC_JSON)
    assert fields["plate"] == "ABC123"
    assert fields["plate_color"] == "Yellow"
    assert fields["country"] == "SE"
    assert fields["vehicle_brand"] == "Volvo"
    assert fields["speed"] == 63.0
    assert fields["lane"] == 2
    assert fields["direction"] == "Approaching"


def test_normalize_traffic_event_top_level_fields():
    data = {"PlateNumber": "XYZ789", "VehicleColor": "Red", "Speed": "45.5"}
    fields = normalize_traffic_event("TrafficJunction", data)
    assert fields["plate"] == "XYZ789"
    assert fields["vehicle_color"] == "Red"
    assert fields["speed"] == 45.5


def test_normalize_traffic_event_empty_data():
    fields = normalize_traffic_event("TrafficJunction", {})
    assert fields["plate"] == ""
    assert fields["speed"] is None
    assert fields["lane"] is None


def test_extract_image_b64_from_nested_metadata():
    jpeg_b64 = "/9j/" + "A" * 600  # looks like a base64 JPEG
    data = {"TrafficCar": {"PlateNumber": "ABC123"},
            "Picture": {"Content": jpeg_b64}}
    assert extract_image_b64(data) == jpeg_b64


def test_extract_image_b64_absent():
    data = {"TrafficCar": {"PlateNumber": "ABC123"}, "PicName": "snap.jpg"}
    assert extract_image_b64(data) is None
    # short strings that merely start like a jpeg are ignored
    assert extract_image_b64({"x": "/9j/short"}) is None


# ---------------------------- RecordFinder history parsing ----------------

FIND_RESPONSE = """found=3
records[0].PlateNumber=SSE00
records[0].Time=2026-07-15 11:18:00
records[0].PlateColor=White
records[0].TrafficCar.VehicleColor=White
records[0].TrafficCar.VehicleType=Sedan
records[0].Speed=42
records[0].Lane=1
records[1].PlateNumber=TT020
records[1].Time=2026-07-15 11:10:00
records[1].Country=DEU
records[2].PlateNumber=
records[2].Time=2026-07-15 11:09:00
records[2].Country=Unknown
"""


def test_parse_finder_object():
    assert parse_finder_object("result=1234\r\n") == "1234"
    assert parse_finder_object("567") == "567"
    assert parse_finder_object("error") is None


def test_parse_find_records_groups_by_index():
    records = parse_find_records(FIND_RESPONSE)
    assert len(records) == 3
    assert records[0]["PlateNumber"] == "SSE00"
    assert records[0]["TrafficCar.VehicleColor"] == "White"
    assert records[1]["PlateNumber"] == "TT020"
    assert records[2]["PlateNumber"] == ""


def test_parse_find_records_items_prefix():
    text = "items[0].PlateNumber=ABC123\nitems[0].Time=2026-07-15 10:00:00"
    records = parse_find_records(text)
    assert len(records) == 1
    assert records[0]["PlateNumber"] == "ABC123"


def test_normalize_traffic_record_nested_and_flat():
    records = parse_find_records(FIND_RESPONSE)
    first = normalize_traffic_record(records[0])
    assert first["plate"] == "SSE00"
    assert first["plate_color"] == "White"
    assert first["vehicle_color"] == "White"   # from nested TrafficCar.*
    assert first["vehicle_type"] == "Sedan"
    assert first["speed"] == 42.0
    assert first["lane"] == 1
    assert first["event_time"] == "2026-07-15 11:18:00"


def test_normalize_traffic_record_unix_time():
    rec = {"PlateNumber": "XYZ", "Time": "1752574680"}
    fields = normalize_traffic_record(rec)
    assert fields["plate"] == "XYZ"
    # Epoch converted to an ISO string (contains the date separator).
    assert "T" in fields["event_time"]


def test_normalize_traffic_record_unlicensed():
    records = parse_find_records(FIND_RESPONSE)
    third = normalize_traffic_record(records[2])
    assert third["plate"] == ""
    assert third["event_time"] == "2026-07-15 11:09:00"
