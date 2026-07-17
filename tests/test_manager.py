from anpr.dahua.manager import (
    ONVIF_FLUSH_SECONDS,
    CameraWorker,
    _flush_onvif,
    _merge_onvif,
)
from anpr.models import Camera


def test_accumulator_infers_direction_from_boxes():
    worker = CameraWorker(Camera(id=1, name="c", host="h"), db=None, hub=None)
    pending = {}
    # two tracking frames with a growing box, then the capture (image) frame
    worker._accumulate_onvif(
        {"object_id": "5", "plate": "", "bbox": (100, 100, 120, 120)}, pending, 1.0)
    worker._accumulate_onvif(
        {"object_id": "5", "plate": "", "bbox": (100, 100, 200, 220)}, pending, 1.1)
    out = worker._accumulate_onvif(
        {"object_id": "5", "plate": "ABC123", "image_b64": "/9j/PIC",
         "bbox": (100, 100, 260, 300)}, pending, 1.2)
    assert len(out) == 1
    assert out[0]["plate"] == "ABC123"
    assert out[0]["direction"] == "Approaching"


def test_resolve_direction_by_plate():
    import asyncio
    worker = CameraWorker(Camera(id=1, name="c", host="h"), db=None, hub=None)
    now = asyncio.get_event_loop().time()
    worker._dir_by_plate["ABC123"] = ("Departing", now)
    # ONVIF capture has no direction; resolved from the plate's event direction
    assert worker._resolve_direction("", "ABC 123") == "Departing"
    # a fixed camera direction still wins
    worker.camera.direction_mode = "Approaching"
    assert worker._resolve_direction("", "ABC123") == "Approaching"


def test_resolve_direction_recent_fallback():
    import asyncio
    worker = CameraWorker(Camera(id=1, name="c", host="h"), db=None, hub=None)
    worker._last_direction = ("Approaching", asyncio.get_event_loop().time())
    # unknown plate, but a fresh "last direction" is used as fallback
    assert worker._resolve_direction("", "ZZZ999") == "Approaching"


def test_accumulator_keeps_camera_direction_if_present():
    worker = CameraWorker(Camera(id=1, name="c", host="h"), db=None, hub=None)
    pending = {}
    out = worker._accumulate_onvif(
        {"object_id": "9", "plate": "X", "image_b64": "/9j/P",
         "direction": "Departing", "bbox": (0, 0, 400, 400)}, pending, 1.0)
    # a real direction from the camera is never overwritten by inference
    assert out[0]["direction"] == "Departing"


def test_merge_onvif_first_nonempty_wins():
    dest = {}
    # plate-only frame
    _merge_onvif(dest, {"plate": "ABC123", "country": "", "image_b64": None,
                        "vehicle_type": ""})
    assert dest["plate"] == "ABC123"
    # later frame brings the image and attributes
    _merge_onvif(dest, {"plate": "", "country": "DEU", "image_b64": "/9j/PIC",
                        "vehicle_type": "Sedan", "direction": "Approaching"})
    assert dest["plate"] == "ABC123"          # not overwritten
    assert dest["country"] == "DEU"
    assert dest["image_b64"] == "/9j/PIC"
    assert dest["vehicle_type"] == "Sedan"
    assert dest["direction"] == "Approaching"


def test_flush_onvif_emits_stale_plate_only_once():
    pending = {"1": {"data": {"plate": "XYZ"}, "last": 0.0, "emitted": False}}
    out = _flush_onvif(pending, ONVIF_FLUSH_SECONDS + 1)
    assert len(out) == 1 and out[0]["plate"] == "XYZ"
    assert pending["1"]["emitted"] is True
    # already emitted -> not emitted again
    assert _flush_onvif(pending, ONVIF_FLUSH_SECONDS + 2) == []


def test_flush_onvif_keeps_recent():
    pending = {"1": {"data": {"plate": "XYZ"}, "last": 100.0, "emitted": False}}
    assert _flush_onvif(pending, 101.0) == []   # seen recently, keep waiting
    assert "1" in pending
